# Phase 3 — Slice 0 handoff (contract-verifier skeleton)

**Branch:** `phase3-slice-0-skeleton` (off `upstream/main` #164). **Status:**
shipped to PR (shadow-mode skeleton only — no real checks yet).

## What shipped

- **`converter/contract_verifier.py`** (new) — the Phase 3 verifier module.
  `ContractViolation` (frozen: `check`/`severity`/`script`/`detail`/`identity`),
  `ContractVerifierResult` (`violations`, `total()`, `counts_by_check()`),
  `verify_contract(topology, scripts, *, mode="shadow")`, plus
  `violation_to_dict` / `stash_violations` (identity-dedup within a result).
  Slice 0 runs ONE input-gated **smoke check** (fires iff `topology` lacks
  `modules`) — proves the data reaches the verifier; checks A/B/C are slices 1-3.
- **`converter/pipeline.py`** — `_run_contract_verifier(scene_runtime)` hooked
  in `_classify_storage` right after `_build_and_apply_topology` (inside the
  non-legacy / `topology_inputs is not None` gate). Reads topology from the
  **passed merged dict**, not `ctx`. Stashes to
  `ctx.scene_runtime["contract_check_violations"]`. `_build_conversion_report`
  populates the metric.
- **`converter/report_generator.py`** — `ConversionReport.contract_check_violations:
  int` + `contract_violations_by_check: dict[str,int]`.
- **`converter/tests/test_contract_verifier.py`** — 16 tests.
- **`docs/design/scene-runtime-architecture-ir.md`** — §Phase 3 rewritten to the
  locked plan (5-slice breakdown, corrected facts).

## Locked decisions (do not relitigate)

- **Home = new `contract_verifier.py`, NOT `contract_pipeline.py`** (the latter is
  a transpile-time orchestrator; verifier needs post-materialize data).
- **Hook = after `_build_and_apply_topology`**, reading the in-scope merged
  `scene_runtime` (topology is NOT mirrored to `ctx.scene_runtime`).
- **Generic-mode only** (whole topology path gated `!= "legacy"`).
- **Each run REPLACES its violation rows** (not append) — `ctx.scene_runtime`
  persists+reloads across resume, so append kept stale rows (codex R1 P2).
- Build order **A→B→C**; **per-check** independent shadow→fail-closed flip;
  check B trusts the **runtime `_UNITY_TO_ROBLOX_CLASS`** (runtime/scene_runtime.luau:71),
  NOT Python `TYPE_MAP` (they disagree on CharacterController).

## Carry-forward for later slices

- **Slice 1 (check A):** the artifact has NO final `container` field and
  `script_class` is intrinsic (not final `script_type`) — stamp the storage
  classifier's final container into the artifact, then check `parent_path ==
  stamp`. Fold in the domain-consistency invariant (server module ≠ LocalScript;
  storage_classifier.py:719-720).
- **Slice 2 (check B):** key off runtime `_UNITY_TO_ROBLOX_CLASS` + peer-module
  set; EXCLUDE the peer `self:GetComponent(name)` form; check only the resolved
  `:FindFirstChildWhichIsA("Y")` sites.
- **Slice 3 (check C):** extend the reader scan to capture the reader's OWN store
  (shared_flag_channels records WRITER funnel dests Character+Player, not the
  reader's). SimpleFPS has 0 Class-1 cross_domain_edges; door state is Class-2.
- **Slice 4 (flip):** the fail-closed PROMOTION gate + its env hatch evaluation
  point land here. Corpus = bundled projects that complete a generic-mode cold
  conversion (AI transpile required to exercise B/C; check A works on stubs);
  `log()` excluded projects. The shadow metric is recomputed-not-persisted by
  design (the verifier re-derives each run); if a cross-process-resume-durable
  metric is ever needed, persist it then.

## Env hatch

`U2R_CONTRACT_VERIFIER_DISABLE` (1/true/yes) early-returns the verifier. Inert
in slice 0 (verifier is already non-fatal); becomes meaningful at the slice-4
fail-closed gate.

## Reviews

Claude (clean, 1 P3 fixed) + `codex review --base origin/main` (1 P2 fixed: the
resume-stale-row bug, with 2 regression tests). Full fast suite: 2385 passed;
no-Any clean.
