# Phase 4c: Reactive — Overview

> **Last verified:** 2026-04-16. Cross-check `api_mappings.py` and `code_transpiler.py` before acting on prescriptions.

Bootstrap emission, spawner wiring, animator-on-clone bindings, residual transpiler gaps — work that needs emitted Luau to exist.

**4c stays small.** Anything reliably automatable should move to 4b. The 4c list shrinks over time.

## Files

| File | Topic |
|---|---|
| `phase-4c-runtime-emit.md` | Spawner code, animator wiring on clones, particle `:Emit()` |
| `phase-4c-bootstrap-emit.md` | Emit `GameBootstrap.lua` against transpiled modules |
| `phase-4c-residual-gaps.md` | Symptom-indexed debugging for unmigrated transpiler misses |

## Conditional reads

| File | Read when |
|---|---|
| `runtime-emit` | Project has runtime-spawned templates OR animations |
| `bootstrap-emit` | Always |
| `residual-gaps` | `luau-analyze` flags issues OR converted game has visible bugs matching the symptom index |

## Inputs

- `conversion_plan.json` (4a)
- Transpiled Luau under `<output_dir>/scripts/` (4b)
- `luau-analyze` output from `convert_interactive.py validate`

## Outputs

`<output_dir>/scripts/`:
- `GameBootstrap.lua`
- Spawner edits in manager modules
- Residual gap fixes in module files
