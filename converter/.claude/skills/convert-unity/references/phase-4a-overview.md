# Phase 4a: Plan — Overview

> **Last verified:** 2026-04-16. Cross-check against current `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Phase 4a is the **upfront architectural planning pass**. It reads the Unity project — C# scripts, scene YAML, prefab YAML, `.anim`/`.controller` files — and produces a `conversion_plan.json` artifact. Phase 4b (transpile) consumes the plan. Phase 4c (reactive) only handles things that require emitted Luau to exist.

**Why separate planning from transpilation.** Phase 4 used to transpile files independently and 4.5 would rewrite much of the output cross-file. That was wasteful. Making planning explicit lets the transpiler emit correctly-classified, correctly-wired code the first time.

## Sub-phase files (read in order)

| Sub-phase | File | Topic |
|---|---|---|
| 4a.1 | `phase-4a-architecture-map.md` | State machines, ownership graphs, timing model |
| 4a.2 | `phase-4a-divergence-and-scale.md` | Character/camera/input overrides, scale strategy, mesh facing |
| 4a.3 | `phase-4a-runtime-plan.md` | Animation approach, templates manifest, movement model |
| 4a.4 | `phase-4a-module-boundaries.md` | Module-per-component plan, bootstrap shape |
| 4a.5 | `phase-4a-storage-classification.md` | Server / client / replicated storage decisions |

## Dependency DAG

```
4a.1 architecture map ─┐
4a.2 divergence/scale ─┼─► 4a.5 storage classification ─► conversion_plan.json
4a.3 runtime plan ─────┤
4a.4 module boundaries ┘
```

4a.5 is the final consumer — it needs every upstream decision to produce a correct storage plan.

## Conditional loading

Not every file is needed for every project. Skip to save tokens:

| File | Read when |
|---|---|
| `phase-4a-architecture-map.md` | **Always** — every game has a state machine or ownership graph |
| `phase-4a-divergence-and-scale.md` | Project has a custom character controller, fixed/rail camera, non-standard input, or mismatched scale |
| `phase-4a-runtime-plan.md` | Project has `.anim`/`.controller` files, or spawns prefabs at runtime (object pooling, spawners, Addressables) |
| `phase-4a-module-boundaries.md` | **Always** — needed to decide module-per-component splits |
| `phase-4a-storage-classification.md` | **Always** — every game needs explicit server/client/replicated decisions |

## The one rule that overrides everything

**Faithful port over workarounds.** If Unity generates content at runtime, the Roblox port must generate it at runtime too. Never substitute a Unity runtime system with a static Roblox-side workaround. The game should work the same way — if there's no static ground in Unity, there should be no static ground in Roblox. Port the system, not the symptom.

## Plan artifact

4a emits `conversion_plan.json` in the output directory, containing:

- `architecture_map` — state machine, ownership graph, timing model
- `divergence_overrides` — character/camera/input/scale decisions
- `templates_manifest` — prefab templates + metadata
- `module_boundaries` — per-module source files + public API
- `storage_plan` — per-script + per-template container assignment (the 3 networking questions answered explicitly)

Phase 4b reads this artifact before transpiling. Phase 4c reads it for bootstrap + spawner emission.
