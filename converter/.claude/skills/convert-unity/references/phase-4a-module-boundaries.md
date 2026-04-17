# Phase 4a.4: Module Boundaries & Bootstrap Shape

> **Last verified:** 2026-04-16. Cross-check against current `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Decide how Unity classes map to Luau modules **before** transpile runs. 4b emits modules to these boundaries; 4c emits a bootstrap that wires them. Storage classification (4a.5) uses the module boundaries to decide which container each module belongs in.

## Module-per-component rule

For each major game system, plan a **separate Luau module** that mirrors its Unity counterpart. Mapping:

| Unity role | Roblox module | Runtime module |
|---|---|---|
| Central manager + state subclasses | State modules + bootstrap | (game-specific state machine helper) |
| World/level manager | Dedicated manager module | game-specific |
| Input/character controller | Dedicated controller module | game-specific |
| Game-specific MonoBehaviours | One module per behaviour | game-specific |
| Legacy Animation on non-skeletal objects | Auto-generated animator config | `runtime/animator_runtime.luau` |
| Mecanim Animator on skinned meshes | Auto-generated root-motion config | `runtime/animator_runtime.luau` |
| ParticleSystem (burst effects) | Emitter with `Enabled=false` + `BurstCount` | scripts call `:Emit()` |
| NavMeshAgent / pathfinding | Game-specific controller | `runtime/nav_mesh_runtime.luau` |
| Cinemachine virtual cameras | Camera config attributes + runtime | `runtime/cinemachine_runtime.luau` |

**Rules for every module:**

- Preserve the same public API shape as the Unity class (methods, properties).
- Inspector fields → config table passed to the constructor.
- `GetComponent<T>()` and singleton access → explicit references passed during wiring.
- Component-to-component references → set during bootstrap, same as Unity's Inspector drag-and-drop.
- **Never merge two Unity classes into one Luau module.** If they were separate in Unity, they stay separate.

## Timing model preservation

- If Unity uses a world-distance counter to measure progress, the Roblox port must too.
- If Unity scales durations by a speed ratio, the Roblox port must too.
- Do NOT simplify world-distance timing into time-based — it changes gameplay feel.

## Output location decision

Every module needs a planned destination. The decision drives 4a.5 storage classification:

- **Bootstrap** (`GameBootstrap`) → LocalScript in StarterPlayerScripts. Always.
- **Controllers** reading `UserInputService`, `LocalPlayer`, camera → LocalScript.
- **Managers** with server authority (scoring, spawning on server) → Script.
- **State modules** — depends on whether the state machine runs client-side (per-player state) or server-side (authoritative state). Most Unity games are single-player; state runs client-side.
- **Data modules** (`_Data.lua` ScriptableObject exports) → ModuleScript, ReplicatedStorage (shared).
- **Game logic modules** required by both client controllers and server managers → ModuleScript, ReplicatedStorage.
- **Server-only modules** (e.g., admin tools, authoritative physics resolution) → ModuleScript, ServerStorage.

4a.5 formalizes this via cross-reference analysis; 4a.4 documents the intent per module.

## Bootstrap shape

`GameBootstrap` — the entry point that wires everything. Plan its shape here, emit in 4c.

The bootstrap:

- Creates instances of each module — **always pass `{}` even if no config is needed**; constructors expect a table, not nil.
- Wires cross-references **after** construction (same as Unity's Inspector: components first, then links).
- Registers states with the state machine and starts it with the initial state.
- Contains **no** game logic — pure wiring.
- Reads the `.unity` scene file for serialized field references (e.g., `characterController: {fileID: XXXX}` tells you ManagerA needs a reference to ControllerB).
- Implements the platform divergence decisions from 4a.2.

## Init order

Derive from the ownership graph in 4a.1. Rules:

1. Data modules (`_Data.lua`) load first — every other module may reference them.
2. Runtime modules (`runtime/*.luau`) load second — they provide engines.
3. Singletons in dependency order (leaf singletons first, aggregate singletons last).
4. State modules construct with their dependencies injected.
5. State machine starts last with the initial state.

## Verify method names — CRITICAL

The transpiler converts files independently; method names may diverge between modules. Luau silently returns `nil` for missing methods — no error, no warning. **Before writing any cross-module call**, grep the target module for the exact method name. Common mismatches: `UpdateX` vs `SetX`, `OnTriggerEnter` vs `HandleTrigger`, `GetComponent` vs direct property access. Fix mismatches in ALL callers.

## Module export unwrapping — CRITICAL

The transpiler is inconsistent about module exports. Some return the class directly (`return MyClass`), others wrap in a table (`return { MyClass = MyClass, SomeEnum = SomeEnum }`). **Inspect each module's return statement before writing `require()` calls.** Use a defensive helper:

```lua
local function unwrap(mod, name)
    if type(mod) == "table" and mod[name] then return mod[name] end
    return mod
end
local Foo = unwrap(require(ReplicatedStorage:WaitForChild("Foo")), "Foo")
```

## Output

`module_boundaries` in `conversion_plan.json`:

```
module_boundaries:
  - module_name: "GameplayState"
    source_classes: ["GameplayState", "GameplayStateHelper"]
    suggested_type: "ModuleScript"
    suggested_container: "ReplicatedStorage"
    public_api: ["new", "Enter", "Exit", "Tick"]
    dependencies: ["WorldManager", "_Data_LevelConfig"]
bootstrap_shape:
  init_order: [list of modules in dependency order]
  initial_state: "Loading"
  cross_wires: [{from, to, config_key}]
```
