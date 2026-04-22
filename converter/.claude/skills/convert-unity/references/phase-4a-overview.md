# Phase 4a: Plan — Overview

> **Last verified:** 2026-04-16. Cross-check `api_mappings.py` and `code_transpiler.py` before acting on prescriptions.

Read the Unity project, emit `conversion_plan.json`. Phase 4b consumes it. Phase 4c handles only what needs emitted Luau to exist.

## Sub-phases (read in order)

| File | Topic |
|---|---|
| `phase-4a-architecture-map.md` | State machines, ownership graph, timing model |
| `phase-4a-divergence-and-scale.md` | Character/camera/input overrides, scale, mesh facing |
| `phase-4a-runtime-plan.md` | Animation approach, templates manifest, movement model |
| `phase-4a-module-boundaries.md` | Module-per-component plan, bootstrap shape |
| `phase-4a-storage-classification.md` | Server / client / replicated container assignments |

## DAG

```
architecture-map ─┐
divergence/scale ─┼─► storage-classification ─► conversion_plan.json
runtime-plan ─────┤
module-boundaries ┘
```

Storage classification consumes every other 4a output.

## Conditional reads

| File | Read when |
|---|---|
| `architecture-map` | Always |
| `divergence-and-scale` | Custom controller, fixed/rail camera, non-standard input, or mismatched scale |
| `runtime-plan` | Project has `.anim`/`.controller` or runtime-spawned prefabs |
| `module-boundaries` | Always |
| `storage-classification` | Always |

## Plan artifact

`conversion_plan.json`:

- `architecture_map` — state machine, ownership graph, timing model
- `divergence_overrides` — character/camera/input/scale
- `templates_manifest` — prefab templates + metadata
- `module_boundaries` — modules + public API
- `storage_plan` — per-script + per-template containers (the 3 networking questions)

## The override

**Faithful port over workarounds.** If Unity generates content at runtime, port the generation system. Don't substitute a static Roblox-side workaround. Port the system, not the symptom.
