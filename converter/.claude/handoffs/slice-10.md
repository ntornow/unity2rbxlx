# Slice 10 → Slice 11 Handoff

## Architecture decisions made

- **Read site switched** at `build_topology._build_modules_block:629` from planner-row `domain_signals["reachability_forced_container"]` to `topology_inputs.reachability_requirements[sid]` with normalization through the late-hoist predicate (4 cases: None→"", `__excluded__`→"", RS+container-in-gated→RS, RS+container-out-of-gated→"").
- **Writes retired** at `module_domain.py:955` and `:1743`. The second was confirmed dead since slice 6.
- **External tests migrated** PRESERVE-ADAPT (3 external + 2 collateral, all renamed/updated; 0 deleted).
- **No-transpile resume semantics documented + pin-tested**: `reachability_required_container` regenerates to `""` for all modules on resume; acceptable because storage classifier reads `reachability_requirements` directly + invariant-10 short-circuits on `""` + no production consumer of the artifact field.
- **`transpile_ran` plumbing** is load-bearing for `storage_classifier.py:577`'s slice-6 unconstrained-helper amendment (NOT speculative).

## What slice 11 inherits

- Phase 2a is structurally complete: all 10 prior slices' deliverables shipped. Slice 11 is the final test sweep + any remaining cleanup.
- **Codex R2 flagged 2 P3 pin-test gaps to address**:
  - `test_resume_helper_routes_via_caller_domain` calls inner helper bypassing `_decide_script_container`'s `transpile_ran` short-circuit at `storage_classifier.py:575-587`. Refactor to exercise the full gate.
  - `test_transpile_ran_flag_is_plumbed...` is tautological (build_topology has no `transpile_ran` kwarg). Either remove or rewrite to assert something meaningful.
- **External test cleanup opportunity**: any tests still asserting pre-Phase-2a patterns (regex-based domain detection, late stamping internals, etc.) — sweep + retire.
- **Documentation pass**: verify the design doc's "current state" sections reflect the shipped reality.

## What slice 11 should NOT do

- Don't reopen any earlier slice's decisions.
- Don't add new behavior; final sweep is cleanup-only.
