# Phase 4c: Reactive — Overview

> **Last verified:** 2026-04-16. Cross-check against current `luau_validator.py` and `api_mappings.py` before acting on prescriptions.

Phase 4c handles the work that **only makes sense after transpiled Luau exists**: bootstrap emission, spawner wiring, animator-on-clone bindings, and residual transpiler gaps that the 4b upfront rules couldn't cover.

**4c is intentionally small.** Anything reactive that can be moved to 4b (upfront rules) should be. The 4c list shrinks as the transpiler learns more.

## Sub-phase files

| Sub-phase | File | Topic |
|---|---|---|
| 4c.1 | `phase-4c-runtime-emit.md` | Spawner code, animator wiring on clones, particle `:Emit()` calls |
| 4c.2 | `phase-4c-bootstrap-emit.md` | Emit `GameBootstrap.lua` against real transpiled modules |
| 4c.3 | `phase-4c-residual-gaps.md` | Symptom-indexed debugging for transpiler misses not yet migrated to 4b |

## Conditional loading

| File | Read when |
|---|---|
| `phase-4c-runtime-emit.md` | Project has runtime-spawned templates OR animations |
| `phase-4c-bootstrap-emit.md` | **Always** — every converted game needs a bootstrap |
| `phase-4c-residual-gaps.md` | Validator flags issues OR the converted game has visible bugs matching the symptom index |

## Inputs

4c consumes:
- `conversion_plan.json` (from 4a) — knows module boundaries, storage plan, templates, bootstrap shape
- Transpiled Luau under `<output_dir>/scripts/` (from 4b) — the real methods and exports to wire against
- Validator output (from `luau_validator.py`)

## Outputs

4c writes to `<output_dir>/scripts/`:
- `GameBootstrap.lua` (the wiring entry point)
- Spawner integration edits into existing manager modules
- Residual gap fixes into individual module files
