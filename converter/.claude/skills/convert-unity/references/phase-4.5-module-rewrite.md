# Phase 4.5h/i: Module-per-Component Rewrite & Bootstrap

> **Last verified:** 2026-04-12 against commit `e19a342`. Some prescriptions may be stale — cross-check against the current `luau_validator.py` and `api_mappings.py` before acting on them. See the 2026-04-12 audit in TODO.md for known discrepancies.

## Module-per-component rule

For each major game system, write a **separate Luau module** that mirrors its Unity counterpart. Mapping:

| Unity role | Roblox module | Runtime modules |
|---|---|---|
| Central manager + state subclasses | State modules + bootstrap wiring | (game-specific state machine helper) |
| World/level manager | Dedicated manager module | game-specific helpers |
| Input/character controller | Dedicated controller module | game-specific helpers |
| Game-specific MonoBehaviours | One module per behaviour | game-specific helpers |
| Legacy Animation on non-skeletal objects | Auto-generated animator config | `runtime/animator_runtime.luau` |
| Mecanim Animator on skinned meshes | Auto-generated root-motion config | `runtime/animator_runtime.luau` |
| ParticleSystem (burst effects) | Emitter with `Enabled=false` + `BurstCount` | Game scripts call `:Emit()` |
| NavMeshAgent / pathfinding | Game-specific controller | `runtime/nav_mesh_runtime.luau` |
| Cinemachine virtual cameras | Camera config attributes + runtime | `runtime/cinemachine_runtime.luau` |

**Rules for every module:**

- Preserve the same public API shape as the Unity class (methods, properties).
- Inspector fields → config table passed to the constructor.
- `GetComponent<T>()` and singleton access → explicit references passed in during wiring.
- Component-to-component references → set during bootstrap, same as Unity's Inspector drag-and-drop.
- **Never merge two Unity classes into one Luau module.** If they were separate in Unity, they stay separate.

## Timing model preservation

- If Unity uses a world-distance counter to measure progress, the Roblox port must too.
- If Unity scales durations dynamically (e.g., by a speed ratio), the Roblox port must too.
- Do NOT simplify world-distance timing into time-based timing — it changes gameplay feel.

## Output location

Write all scripts to `<output_dir>/scripts/`:

- One file per module (e.g., `WorldManager.lua`, `CharacterController.lua`, `GameplayState.lua`).
- `GameBootstrap.lua` — the entry point that wires everything.
- These replace the raw transpiled versions for core systems.
- **Script type detection** is automatic in `roblox/rbxlx_writer.py`: files ending with `return <identifier>` → ModuleScript; files using client APIs (`Players.LocalPlayer`, `UserInputService`) → LocalScript; otherwise Script. Override via `_meta.json` only when auto-detection is wrong.

## Bootstrap (`GameBootstrap.lua`) — LocalScript in StarterPlayerScripts

The bootstrap:

- Creates instances of each module — **always pass `{}` even if no config is needed**; constructors expect a table, not nil.
- Wires cross-references **after** construction (same as Unity's Inspector references — components first, then links).
- Registers states with the game's state machine and starts it with the initial state.
- Contains **no** game logic — pure wiring.
- Reads the `.unity` scene file for serialized field references (e.g., `characterController: {fileID: XXXX}` tells you ManagerA needs a reference to ControllerB).
- Implements the platform divergence decisions from `phase-4.5-divergence-and-scale.md`.

### Verify method names — CRITICAL

The transpiler converts each C# file independently, so method names may diverge between modules. Luau silently returns `nil` for missing methods — no error, no warning. The call appears to succeed but does nothing.

**Before writing any cross-module call**, grep the target module for the exact method name. Common mismatches: `UpdateX` vs `SetX`, `OnTriggerEnter` vs `HandleTrigger`, `GetComponent` vs direct property access. Fix mismatches in ALL callers — never assume the transpiled name is correct without checking.

### Module export unwrapping — CRITICAL

The transpiler is inconsistent about how modules export classes. Some return the class directly (`return MyClass`), others wrap it in a table (`return { MyClass = MyClass, SomeEnum = SomeEnum }`). The bootstrap **must not assume** which style a module uses. Before writing `require()` calls, inspect each module's `return` statement. Use a defensive helper:

```lua
local function unwrap(mod, name)
    if type(mod) == "table" and mod[name] then return mod[name] end
    return mod
end

local SomeModule = unwrap(require(ReplicatedStorage:WaitForChild("SomeModule")), "SomeModule")
```

Without this, if you write `local Foo = require(...)` and the module returns `{ Foo = Foo }`, then `Foo.new()` calls the wrapper table (which has no `.new`), producing "attempt to call a nil value" — silent until runtime.

### Character and avatar setup

Use the player's Roblox avatar as the game character when appropriate (e.g., on-rails games, platformers). Wait for `player.Character`, get `HumanoidRootPart`, disable default movement (`WalkSpeed=0, JumpPower=0, JumpHeight=0`).

- If the scale decision was "scale character down", call `character:ScaleTo(SCALE)` **before** anchoring. Scaling requires a brief physics settle (`task.wait(0.1)`).
- **Never call `Humanoid:ApplyDescription()` or `ApplyDescriptionReset()` from a LocalScript** — server-only APIs; they error on the client and crash the bootstrap.
- Then anchor HRP, set initial `CFrame` using the computed `GROUND_Y`, and pass both transform and groundY to the character controller.
- Only create a placeholder Part if the game uses a non-humanoid avatar.

### Input wiring

Via `UserInputService.InputBegan` — the transpiler does NOT create input bindings. Map Unity's `Input.GetKeyDown` keycodes to Roblox `Enum.KeyCode` and dispatch keys to controller methods.

### Collision signal wiring

For any module that defines `OnTriggerEnter/Exit` or `OnCollisionEnter/Exit`, Roblox requires explicit wiring. **Choose the mechanism based on how the part moves:**

- **Physics-driven parts** (unanchored, moved by forces): use `.Touched` / `.TouchEnded`.
- **CFrame-driven parts** (anchored, moved by setting CFrame each frame): `.Touched` is **unreliable** — Roblox's physics engine doesn't fire touch events for parts moved via CFrame. Use `workspace:GetPartsInPart(part, overlapParams)` in a per-frame Heartbeat loop instead. This is the common case for converted games where the character controller sets position directly.

For the per-frame overlap pattern: use an `alreadyHit` set to prevent duplicate triggers per object, and filter out the character's own parts via `OverlapParams.FilterDescendantsInstances`. **Skip fully transparent parts** (`Transparency >= 1.0`) — prefab shadow planes and invisible collision boxes don't have colliders in Unity but `GetPartsInPart` picks them up. Without this filter, the player takes damage from invisible geometry. **The bootstrap only wires the signal — the transpiled method decides what to do.** Never add game-specific collision filtering in the bootstrap.

### Player spawn disambiguation

Unity scenes often have both a player *prefab* (character model with camera, slot children, etc.) AND a *spawn marker* part sharing a similar or identical name. The pipeline places both in Workspace. When resolving the spawn position:

- The player **prefab Model** is typically at or near the origin and contains child objects. It's a Model, not a BasePart.
- The player **spawn marker** is a BasePart at the actual starting position.
- `workspace:FindFirstChild("Player")` returns whichever comes first in the tree — often the prefab Model at origin, not the spawn marker. Use `workspace:GetDescendants()` with filtering: skip any BasePart whose parent is also a Model of the same name — that's the prefab.
- **Never let the controller independently search for spawn position.** The bootstrap is the single source of truth: it finds the correct spawn marker and passes it to the controller via a dedicated setter. The controller should only fall back to `character:GetPivot()` if no spawn was explicitly set.

### Scene object classification — menu vs gameplay environment

Unity scenes contain objects meant for different contexts: menu backgrounds, editor-only preview instances, and gameplay environment (props, terrain). The pipeline places non-prefab scene objects into Workspace. Prefab instances (nodes resolved against templates) are automatically excluded from Workspace — they already exist as template Models in ReplicatedStorage/Templates and are cloned at runtime.

- **Menu/UI scene objects** (title backdrops, menu cameras, preview platforms): hide by setting `Transparency=1, CanCollide=false` on all descendant BaseParts. Identify by name patterns from the scene hierarchy (common substrings: "Menu", "UI", "Background", "Title"). Confirm against the `.unity` YAML before hiding.
- **Editor preview instances** (prefabs placed in the scene for editor viewing but spawned at runtime): the pipeline auto-excludes these. Any that slip through (non-prefab copies) must be hidden manually.
- **Gameplay environment** (buildings, terrain, decorations along the play area): keep visible. Decoration positions are baked into prefabs — preserve them faithfully (see `phase-4.5-divergence-and-scale.md`).
- **Broken visual artifacts** (objects that render as white boxes or gray rectangles due to missing textures, failed meshes, or stripped effects): remove from both Workspace and Templates to prevent them appearing in spawned segments.

The bootstrap should hide by **known name list**, not broad pattern matching that could catch gameplay objects.

## Decision point

For each rewritten module, the agent reviews:

- Which Unity C# class(es) it was derived from.
- The ownership graph: what it references, what references it.
- Which runtime modules it uses.
- Any timing model decisions (world-distance vs time-based).

The agent decides Accept / Edit / Regenerate based on whether the module preserves the Unity semantics and passes the rules in `phase-4.5-universal-rules.md` and `phase-4.5-transpiler-gaps.md`.

## Key principles

- **Faithful port over workarounds** — never substitute a Unity runtime system with a static Roblox-side workaround.
- **Architecture preservation over code translation** — the goal is a Roblox game wired the same way the Unity game was.
- **Port the system, not the symptom** — trace back to what Unity system produces the missing output and port that system. A missing floor means the spawner needs porting, not a baseplate.
- Runtime modules (`runtime/`) are reusable — never modify them for one game.
- Game-specific scripts are output artifacts — they live in `<output_dir>/scripts/`, not in this repo.
- Focus on the 3–5 scripts that define the core game loop; leave utilities as-is from transpilation.
- When in doubt about a design decision, check what the Unity code actually does.
