# Slice 11 handoff (also: Phase 2a CLOSEOUT)

Slice 11 is the last slice of Phase 2a. This handoff doubles as the Phase 2a
closeout document — the next agent picking up the topology effort starts at the
Phase 2b boundary (code_transpiler bridge emission), not at a new slice within
Phase 2a.

---

## Slice 11 architecture decisions

- **Test cleanup: 2 P3 pin-test gaps closed.**
  - P3 #1 refactored to call through `classify_storage()` with legacy-reason
    assertion (rather than asserting on internal helper output).
  - P3 #2 deleted as tautological + replaced by a test that exercises the real
    `_maybe_run_topology_prepass` producer (R2 fix from review — fault-injection
    at `pipeline.py:4634` flipping the conditional to `is None` makes the test
    fail at the producer assertion, confirming the pin actually binds the
    producer/consumer contract).
- **Dead code: `_apply_reachability_rule` deleted** at
  `module_domain.py:1623-1753` (~130 LOC). Function was already dead since
  slice 6 per its own docstring; nothing reads it. Shim import at
  `scene_runtime_domain.py:47` removed.
- **Design doc consistency patches at 5+ sites:**
  - Regex helpers (`_scripts_with_client_apis`, `_scripts_with_server_apis`)
    documented as RESTORED-as-fallback-only (post-slice-7-R3).
  - `_stamp_container_and_path` documented as writes-retired-function-live
    (post-slice-10): audit-signal writes are gone, but the function still
    writes `container`/`module_path` via `finalize_topology_containers`.

## Phase 2a complete summary (9 stacked PRs)

`script_storage.py` is now a bound consumer of topology facts. Delivered
across 9 stacked PRs (#150-157 + slice 11 PR):

- **Slices 1-4 (PR #150):** foundation — planner helpers, canonical
  `class_name` helpers, `caller_graph`, schema-compat fixture.
- **Slice 5 (PR #151):** immutable `RbxScript.intrinsic_script_type` stamped
  at transpile time.
- **Slice 6 (PR #152):** pipeline split — `infer_module_domains` /
  `derive_reachability_requirements` / `finalize_topology_containers`
  extracted; `topology_inputs` kwarg threaded.
- **Slice 7 (PR #153):** `_decide_script_container` rewrite consuming
  `topology_inputs`. Server-only ModuleScript → ServerStorage. Regex helpers
  RESTORED as fallback-only.
- **Slice 8 (PR #154):** new `materialize_and_classify` phase. Lifts emit +
  cohere + classify out of `write_output`.
- **Slice 9a (PR #155):** plumbing `topology_inputs` into
  `_build_and_apply_topology` + folded in followup #10 (script_id_by_name
  collision fix).
- **Slice 9b (PR #156):** dropped `reachability_forced_container` from
  `TopologyModuleEntry` (Option C, dual-audit verified).
- **Slice 10 (PR #157):** switched read site to
  `topology_inputs.reachability_requirements[sid]` via normalization. Retired
  audit-signal writes.
- **Slice 11 (this PR):** test cleanup + dead code deletion + design doc
  patches.

## Phase 2a's key architectural principles (carry forward to Phase 2b/3)

1. **Persistence rule:** save raw facts, recompute conclusions. `caller_graph`
   is the explicit exception (too expensive to recompute).
2. **Classifier respects upstream analysis:** server-only ModuleScript →
   ServerStorage (not RS-default-with-phantom-predicate).
3. **`transpile_ran: bool` on TopologyInputs:** raw fact distinguishing
   no-transpile resume from real bug.
4. **Unconstrained-helper fallback contract:** when `transpile_ran=False`
   AND `reachability_requirements[sid]` absent → legacy six-rule path runs
   for that script only.
5. **Regex helpers ARE fallback-path-only — do not delete.** Still
   load-bearing for the unconstrained-helper fallback.
6. **`_stamp_container_and_path` writes are retired for audit signals,
   but the function is still live** for `container` / `module_path` writes
   via `finalize_topology_containers`.

## Phase 2b prep (next phase per design doc)

Phase 2b = `code_transpiler` bridge emission + retire
`pickup_remote_event_server` pack. Independent of Phase 2a's internal
cleanups. Work tracker: pickup task #2.

## Phase 3 prep

Phase 3 = contract verifier (shadow → fail-closed). Work tracker: task #3.

## Outstanding high-priority followups

- **Task #8:** investigate upstream input classification of Roblox-dead
  modules (water rendering helpers etc).
- **Task #9:** investigate transpiler false-positive require injection
  (Plane → GameManager case).

## Session lessons saved to memory (also relevant to Phase 2b)

8+ memory entries on review discipline, evidence labeling, design-doc
updates, and presenting choices with English explanations. The
**"anchor on design intent not current behavior when rewriting"** rule
was critical for slice 7's server-only routing decision and will matter
again in Phase 2b when deciding what bridge emission should look like
(don't replicate current ad-hoc bridge synthesis — bind to the documented
contract).
