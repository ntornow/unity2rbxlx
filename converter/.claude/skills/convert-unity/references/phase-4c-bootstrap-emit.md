# Phase 4c.2: Bootstrap Emission

> **Last verified:** 2026-04-16. Cross-check `api_mappings.py` and `code_transpiler.py` before acting on prescriptions.

Emit `GameBootstrap.lua` against the real shape of transpiled modules. Inputs: `bootstrap_shape` and `storage_plan` from `conversion_plan.json`, plus actual files under `<output_dir>/scripts/`.

## Output location

LocalScript in StarterPlayerScripts (per 4a.5). Write to `<output_dir>/scripts/GameBootstrap.lua`. `roblox/rbxlx_writer.py` routes by `parent_path`; bootstrap uses `Players.LocalPlayer` so type detection picks LocalScript.

## Contract

The bootstrap:

- Creates each module — **always pass `{}` even with no config**; constructors expect a table.
- Wires cross-references **after** construction.
- Registers states; starts the state machine with `bootstrap_shape.initial_state`.
- Contains **no game logic** — pure wiring.
- Reads `.unity` YAML for serialized field references.
- Implements 4a.2 divergence decisions.

## Verify method names

Before any cross-module call, grep the target. Luau silently returns `nil` for missing methods. Common renames: `UpdateX` ↔ `SetX`, `OnTriggerEnter` ↔ `HandleTrigger`. Fix in ALL callers.

## Module export unwrapping

Inspect each module's `return` before writing `require()`. Some return the class, others wrap in a table. Defensive helper:

```lua
local function unwrap(mod, name)
    if type(mod) == "table" and mod[name] then return mod[name] end
    return mod
end
local Foo = unwrap(require(ReplicatedStorage:WaitForChild("Foo")), "Foo")
```

Without this, `Foo.new()` on a wrapper table produces "attempt to call a nil value" — silent until runtime.

## Character + avatar

Use the player's Roblox avatar when appropriate (on-rails, platformers). Wait for `player.Character`, get HumanoidRootPart, disable default movement (`WalkSpeed=0, JumpPower=0, JumpHeight=0`).

- If `divergence_overrides.scale.strategy == "char_down"`, `character:ScaleTo(SCALE)` **before** anchoring. `task.wait(0.1)` for physics.
- **Never call `Humanoid:ApplyDescription()` from a LocalScript** — server-only; errors and crashes the bootstrap.
- Anchor HRP, set CFrame from `GROUND_Y`, pass transform + groundY to the controller.
- Only create a placeholder Part for non-humanoid avatars.

## Input wiring

Via `UserInputService.InputBegan` — the transpiler doesn't create bindings. Map Unity `Input.GetKeyDown` keycodes to `Enum.KeyCode`, dispatch to controller methods. Mapping comes from `divergence_overrides.input`.

## Collision signals

For modules with `OnTriggerEnter/Exit` or `OnCollisionEnter/Exit`:

- **Physics-driven** (unanchored, force-moved): `.Touched`/`.TouchEnded`.
- **CFrame-driven** (anchored, set per frame): `.Touched` is **unreliable** — Roblox doesn't fire touch for CFrame-moved parts. Use `workspace:GetPartsInPart(part, overlapParams)` in a Heartbeat loop.

For overlap: `alreadyHit` set to dedupe, `OverlapParams.FilterDescendantsInstances` to filter character parts. **Skip `Transparency >= 1.0`** — invisible prefab shadow planes have no Unity colliders but `GetPartsInPart` picks them up. Without this filter, the player takes damage from invisible geometry.

**Bootstrap wires the signal; the transpiled method decides what to do.** No game-specific collision filtering in the bootstrap.

## Player spawn disambiguation

Unity scenes often have both a player *prefab* (Model with camera, slot children) and a *spawn marker* (BasePart). Both end up in Workspace. `workspace:FindFirstChild("Player")` returns whichever is first — usually the prefab Model at origin, not the spawn marker.

Use `workspace:GetDescendants()` with filtering: skip BaseParts whose parent is a Model of the same name. **Bootstrap is single source of truth** for spawn position; the controller falls back to `character:GetPivot()` only if no spawn was set.

## Decision per module

For each rewritten module, review:

- Source class(es).
- Ownership graph: refs in/out.
- Runtime modules used.
- Timing model (world-distance vs time).

Decide Accept / Edit / Regenerate based on whether Unity semantics are preserved and the rules in `phase-4b-universal-rules.md` and `phase-4c-residual-gaps.md` pass.

## Principles

- **Faithful port over workarounds** — no static Roblox-side substitutes for Unity runtime systems.
- **Architecture preservation over code translation.**
- **Port the system, not the symptom** — missing floor means the spawner needs porting, not a baseplate.
- Runtime modules in `runtime/` are reusable — never modify for one game.
- Game-specific scripts live in `<output_dir>/scripts/`.
- Focus on the 3–5 scripts that define the core loop; leave utilities alone.
- When in doubt about a design decision, check what Unity actually does.
