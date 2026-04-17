# Phase 4c.2: Bootstrap Emission

> **Last verified:** 2026-04-16. Cross-check against current `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Emit `GameBootstrap.lua` (and its peer LocalScript headers/initializers) **against the real shape of transpiled modules**. Input: `bootstrap_shape` from `conversion_plan.json` (4a.4), `storage_plan` (4a.5), and the actual files under `<output_dir>/scripts/`.

## Output location

Bootstrap is a **LocalScript in StarterPlayerScripts** (per 4a.5 storage classification). Write to `<output_dir>/scripts/GameBootstrap.lua`. Script type detection in `roblox/rbxlx_writer.py` routes files ending with `return <ident>` → ModuleScript; files using client APIs → LocalScript. Bootstrap uses `Players.LocalPlayer` → LocalScript.

## Core contract

The bootstrap:

- Creates instances of each module — **always pass `{}` even if no config is needed**; constructors expect a table, not nil.
- Wires cross-references **after** construction (components first, then links — matches Inspector drag-and-drop).
- Registers states with the state machine and starts it with the initial state from `bootstrap_shape.initial_state`.
- Contains **no** game logic — pure wiring.
- Reads the `.unity` scene file for serialized field references (e.g., `characterController: {fileID: XXXX}` → ManagerA needs a reference to ControllerB).
- Implements the platform divergence decisions from `phase-4a-divergence-and-scale.md`.

## Verify method names — CRITICAL

Before writing any cross-module call, grep the target module for the exact method name. Luau silently returns `nil` for missing methods. Common mismatches: `UpdateX` vs `SetX`, `OnTriggerEnter` vs `HandleTrigger`, `GetComponent` vs direct property access. Fix mismatches in ALL callers — never assume the transpiled name is correct without checking.

## Module export unwrapping — CRITICAL

Inspect each module's `return` statement before writing `require()` calls. Some return the class directly, others wrap in a table. Use a defensive helper:

```lua
local function unwrap(mod, name)
    if type(mod) == "table" and mod[name] then return mod[name] end
    return mod
end
local Foo = unwrap(require(ReplicatedStorage:WaitForChild("Foo")), "Foo")
```

Without this, `Foo.new()` on a wrapper table produces "attempt to call a nil value" — silent until runtime.

## Character and avatar setup

Use the player's Roblox avatar when appropriate (on-rails, platformers). Wait for `player.Character`, get `HumanoidRootPart`, disable default movement (`WalkSpeed=0, JumpPower=0, JumpHeight=0`).

- If `divergence_overrides.scale.strategy == "char_down"`, call `character:ScaleTo(SCALE)` **before** anchoring. Scaling requires a brief physics settle (`task.wait(0.1)`).
- **Never call `Humanoid:ApplyDescription()` or `ApplyDescriptionReset()` from a LocalScript** — server-only APIs; they error on the client and crash the bootstrap.
- Then anchor HRP, set initial `CFrame` using the computed `GROUND_Y`, and pass both transform and groundY to the character controller.
- Only create a placeholder Part if the game uses a non-humanoid avatar.

## Input wiring

Via `UserInputService.InputBegan` — the transpiler does NOT create input bindings. Map Unity's `Input.GetKeyDown` keycodes to Roblox `Enum.KeyCode` and dispatch keys to controller methods. The mapping comes from `divergence_overrides.input` in the plan.

## Collision signal wiring

For any module that defines `OnTriggerEnter/Exit` or `OnCollisionEnter/Exit`:

- **Physics-driven parts** (unanchored, moved by forces): use `.Touched` / `.TouchEnded`.
- **CFrame-driven parts** (anchored, moved by setting CFrame): `.Touched` is **unreliable** — Roblox's physics engine doesn't fire touch events for CFrame-moved parts. Use `workspace:GetPartsInPart(part, overlapParams)` in a per-frame Heartbeat loop.

For the per-frame overlap pattern: use an `alreadyHit` set to prevent duplicate triggers, filter out character's own parts via `OverlapParams.FilterDescendantsInstances`. **Skip fully transparent parts** (`Transparency >= 1.0`) — prefab shadow planes and invisible collision boxes don't have colliders in Unity but `GetPartsInPart` picks them up. Without this filter, the player takes damage from invisible geometry.

**The bootstrap only wires the signal — the transpiled method decides what to do.** Never add game-specific collision filtering in the bootstrap.

## Player spawn disambiguation

Unity scenes often have both a player *prefab* (character model with camera, slot children) AND a *spawn marker* part sharing a similar name. The pipeline places both in Workspace.

- The player **prefab Model** is at or near the origin, contains child objects, is a Model (not a BasePart).
- The player **spawn marker** is a BasePart at the actual starting position.
- `workspace:FindFirstChild("Player")` returns whichever comes first — often the prefab Model at origin. Use `workspace:GetDescendants()` with filtering: skip any BasePart whose parent is also a Model of the same name — that's the prefab.
- **Never let the controller independently search for spawn position.** The bootstrap is the single source of truth; it finds the spawn marker and passes it to the controller via a dedicated setter. The controller falls back to `character:GetPivot()` only if no spawn was explicitly set.

## Decision point

For each rewritten module, the agent reviews:

- Which Unity C# class(es) it was derived from.
- The ownership graph: what it references, what references it.
- Which runtime modules it uses.
- Any timing model decisions (world-distance vs time-based).

The agent decides Accept / Edit / Regenerate based on whether the module preserves Unity semantics and passes the rules in `phase-4b-universal-rules.md` and `phase-4c-residual-gaps.md`.

## Key principles

- **Faithful port over workarounds** — never substitute a Unity runtime system with a static Roblox-side workaround.
- **Architecture preservation over code translation** — the goal is a Roblox game wired the same way as the Unity game.
- **Port the system, not the symptom** — trace back to what Unity system produces the missing output and port that. A missing floor means the spawner needs porting, not a baseplate.
- Runtime modules (`runtime/`) are reusable — never modify them for one game.
- Game-specific scripts are output artifacts — they live in `<output_dir>/scripts/`, not in this repo.
- Focus on the 3–5 scripts that define the core game loop; leave utilities as-is from transpilation.
- When in doubt about a design decision, check what the Unity code actually does.
