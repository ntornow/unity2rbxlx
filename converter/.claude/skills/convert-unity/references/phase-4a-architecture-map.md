# Phase 4a.1: Architecture Map

> **Last verified:** 2026-04-16. Cross-check `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Read every C# script in `<unity_project_path>/Assets/Scripts/` and produce three artifacts. Feeds module boundaries (4a.4) and storage classification (4a.5).

## 1. State machine

Most Unity games have a central state machine (typically a `GameManager` singleton with state subclasses). Map:

- **States.** Use generic role labels: loading/splash, loadout/menu, gameplay, pause, terminal/game-over.
- **Transitions.** What triggers each switch, push, or pop?
- **Per-state behavior.** What does `Enter`/`Exit`/`Tick` (or framework equivalent) do?

## 2. Ownership graph

Map which MonoBehaviour owns references to which. Inspector references become explicit constructor wiring.

- **Singletons** (`static instance`) → module-level state. The transpiler often turns `Class.instance` into a *function*, not a value — see `phase-4c-residual-gaps.md`.
- **Inspector fields** (`public SomeClass field`) → config table at construction.
- **Scene references** (`public GameObject x`) → bootstrap finds in `workspace` by name. The transpiler can't, because the GUID-to-name mapping lives in the `.unity` YAML.

## 3. Timing model

How does the game measure durations? Wrong choice changes feel without changing logic.

- **Time-based** (`Time.deltaTime`) → `os.clock()` / `tick()`.
- **World-distance-based.** Durations measured by distance traveled. Common when speed varies. **Preserve it** — swapping to seconds breaks the feel at higher speeds.
- **Coroutine-based** (`StartCoroutine` + `yield return`) → `task.spawn` + `task.wait`. Signal callbacks can't yield — see `phase-4b-universal-rules.md`.

## Output

`architecture_map` in `conversion_plan.json`:

```
states: [{name, role, transitions, behavior}]
ownership_graph: { class: [refs] }
singletons: [classes]
inspector_refs: { class: [{field, target_type}] }
scene_refs: { class: [{field, scene_object_name}] }
timing_model: "time" | "world_distance" | "coroutine" | "mixed"
```

## Coherence check

- Every gameplay subsystem appears. Missing scoring or spawning means a script was skipped.
- Transition edges form a connected graph; orphan states are a red flag.
- Timing model is consistent within a subsystem.

**Escape hatch:** if the state machine is unclear, start from `Assets/Scenes/*.unity` — the GameManager's initial state field names the first state. Walk outward.
