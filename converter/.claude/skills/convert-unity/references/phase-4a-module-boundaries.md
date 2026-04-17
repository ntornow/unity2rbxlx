# Phase 4a.4: Module Boundaries & Bootstrap Shape

> **Last verified:** 2026-04-16. Cross-check `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Decide Unity → Luau module mapping **before** transpile. 4b emits to these boundaries; 4c emits a bootstrap that wires them. Storage classification (4a.5) reads boundaries to pick containers.

## Mapping

One Luau module per major Unity system:

| Unity role | Roblox module | Runtime |
|---|---|---|
| Central manager + state subclasses | State modules + bootstrap | game-specific state machine |
| World/level manager | Manager module | game-specific |
| Input/character controller | Controller module | game-specific |
| Per-MonoBehaviour | One module each | game-specific |
| Legacy `.anim` on non-skeletal | Auto-gen animator config | `runtime/animator_runtime.luau` |
| Mecanim on skinned | Auto-gen root-motion config | `runtime/animator_runtime.luau` |
| ParticleSystem (burst) | Emitter `Enabled=false` + `BurstCount` | scripts call `:Emit()` |
| NavMeshAgent | Controller module | `runtime/nav_mesh_runtime.luau` |
| Cinemachine | Camera attributes + runtime | `runtime/cinemachine_runtime.luau` |

**Rules per module:**
- Preserve the Unity public API (methods, properties).
- Inspector fields → constructor config table.
- `GetComponent<T>()` and singletons → explicit refs at wiring time.
- Component-to-component links → set during bootstrap (matches Inspector drag-and-drop).
- **Never merge two Unity classes into one Luau module.**

## Timing model preservation

If Unity uses world-distance counters or speed-ratio duration scaling, the port must too. Don't simplify to time-based — it changes feel.

## Output location per module

Drives 4a.5 storage classification:

- **Bootstrap** → LocalScript in StarterPlayerScripts. Always.
- **Controllers** using `UserInputService`, `LocalPlayer`, camera → LocalScript.
- **Managers** with server authority → Script.
- **State modules** — usually client-side (most Unity games are single-player).
- **Data modules** (`_Data.lua`) → ModuleScript, ReplicatedStorage.
- **Modules required by both client and server** → ModuleScript, ReplicatedStorage.
- **Server-only modules** → ModuleScript, ServerStorage.

## Bootstrap shape

`GameBootstrap` (LocalScript). The bootstrap:

- Creates each module — **always pass `{}` even with no config**; constructors expect a table.
- Wires cross-references **after** construction.
- Registers states, starts the state machine.
- Contains **no game logic** — pure wiring.
- Reads `.unity` YAML for serialized field references (`characterController: {fileID: XXXX}`).
- Implements 4a.2 divergence decisions.

**Init order** (from the ownership graph):

1. Data modules (`_Data.lua`) — referenced by everything.
2. Runtime modules (`runtime/*.luau`) — engines.
3. Singletons in dependency order (leaf first).
4. State modules with dependencies injected.
5. State machine starts last.

## Two failure modes to call out

**Method-name drift.** The transpiler converts files independently; method names diverge. Luau silently returns `nil` for missing methods. Before any cross-module call, grep the target. Common renames: `UpdateX` ↔ `SetX`, `OnTriggerEnter` ↔ `HandleTrigger`. Fix in ALL callers.

**Export shape drift.** Some modules return the class (`return MyClass`), others wrap (`return { MyClass = MyClass, SomeEnum = SomeEnum }`). Inspect each `return` before writing `require()`. Use:

```lua
local function unwrap(mod, name)
    if type(mod) == "table" and mod[name] then return mod[name] end
    return mod
end
```

## Output

```
module_boundaries:
  - module_name, source_classes, suggested_type, suggested_container,
    public_api, dependencies

bootstrap_shape:
  init_order: [modules in dependency order]
  initial_state
  cross_wires: [{from, to, config_key}]
```
