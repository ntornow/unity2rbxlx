# Slice 9a handoff — topology_inputs plumbing + followup #10 fold-in

Slice 9a delivers the unambiguous portion of slice 9 (plumbing +
followup #10). Slice 9b defers the recompute of
`reachability_forced_container` and `module_path` pending the Option A
vs Option B decision (see "What slice 9b inherits" below).

## Architecture decisions made

- **`topology_inputs` plumbed** through `_build_and_apply_topology`
  (`pipeline.py:4614`) → `_build_modules_block`. Caller passes it from
  prepass output; defense-in-depth `assert topology_inputs is not None`
  documents the invariant.
- **Followup #10 closed**: `_build_modules_block:557-571` joins on
  `script_id` via `script_by_sid` (inverted from
  `build_script_id_by_name`) when present, instead of `class_name` only
  via `scripts_by_class`. Mirrors slice 7 R4 fix pattern.
- **Byte-equivalence preserved** for `reachability_forced_container` +
  `module_path` — still read from stamped `modules[*]` rows. Slice 9b
  will decide whether to recompute (Option A: accept "rule applies"
  semantic shift; Option B: add `hoist_fired` raw fact for
  byte-equivalence).
- **Persistence rule preserved**: `topology_inputs` is NOT persisted
  to StoragePlan. Always recomputed via prepass.

## What slice 9b inherits + the open A/B question

- All plumbing slice 9b needs is in place; `_build_modules_block`
  already receives `topology_inputs`.
- The recompute question is purely structural: does the recomputed
  `reachability_forced_container` preserve today's "rule fired"
  semantics (Option B, larger LOC) or shift to "rule applies"
  (Option A, smaller LOC)?
- The slice-9a arch synthesis at
  `/tmp/topology/slice-9-arch-synth.md` has the verification details.
- Claude R1 flagged a degenerate-fixture corner: `rbx_place.scripts ==
  []` with non-empty `modules` could fire the new assert before the
  `rbx_place is None` early-return at `pipeline.py:4670`. Slice 9b
  should either hoist the scripts check above the assert OR drop the
  prepass's extra gate. Cheap fix; fold in alongside the recompute.

## What slice 10 inherits

- After slice 9b, `_stamp_container_and_path` at
  `module_domain.py:880,886,891` becomes deletable. Slice 10 deletes
  those call sites.

## Process notes for slice 9b

- The A vs B decision is the key gate. Both reviewers verified slice
  9a doesn't constrain it.
- Memory rule: when slice 9b runs, fixtures must be constructed by
  running real producers (not pre-stamping outputs). Slice 7 R1
  lesson.
