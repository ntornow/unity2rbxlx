# Task #8 handoff — Roblox-dead module routing

PR: `fix/roblox-dead-module-routing` → ntornow/unity2rbxlx (base `main`).
Independent PR (not stacked). Closes TODO #8 (last slice-7-audit followup; #9 = PR #162).

## What shipped

Generic detection of **Roblox-dead** Unity modules (rendering/shader/camera-effect
helpers with no Roblox equivalent) + routing them out of `ServerStorage` + a
closure-safe prune pass. Fix shape **C (hybrid)**, definition **D3 (both-agree) +
hard veto** — user-signed-off after a Claude+Codex arch-review pair.

- **Detector** `converter/roblox_dead_modules.py` — `classify_module_dead`: dead iff
  INPUT prior (C# API surface dominated by unmapped APIs; `-- no equivalent` stubs
  count as unmapped) AND OUTPUT inert (post-coherence Luau has no genuine Roblox
  effect) AND no hard veto. NO game-specific class names.
- **Heuristic replacement** `code_transpiler._is_visual_only_script` — the hardcoded
  `waterbase/displace/...` name list is gone; delegates to generic `is_input_side_dead`
  (positive rendering-API signal + no gameplay veto + dead-leaning coverage). The
  transpile-time gate is best-effort; the post-coherence detector is authoritative.
- **Routing consumer** `storage_classifier.py` — dead ModuleScripts reroute to
  `ReplicatedStorage` in BOTH `_decide_script_container_from_topology` AND
  `_decide_script_container_legacy` (the legacy reason text is what the cached
  symptom showed).
- **Prune pass** `pipeline.py:_subphase_prune_dead_module_closures` — drops a dead
  module only when its FULL TRANSITIVE require-closure (from emitted Luau, all
  `FindFirstChild`/`WaitForChild`/`FindFirstDescendant` shapes) is also dead; a dead
  module with any live requirer stays inert (not pruned).
- New analysis pass `_subphase_analyze_dead_modules` between cohere and classify;
  `dead_modules` persisted to `conversion_context.json` and REVALIDATED against the
  rehydrated Luau on no-transpile resume (don't clobber hand-edits, don't lose reroute).

## Locked decisions (don't revert without an arch review)
- D3 + hard veto; replace (not extend) the hardcoded heuristic; closure from emitted
  Luau (not `dependency_map`); never prune a module with a live requirer; both routing
  paths; transpile-time gate is best-effort, post-coherence detector authoritative.
- Geometry safety: `scene_converter` builds MeshPart geometry/material independently of
  scripts — pruning never removes geometry. SimpleFPS water prefabs are unplaced orphans.

## Empirical validation
- SimpleFPS real AI run: detector flags exactly the 9 visual-cluster modules dead
  (all 6 named water modules, by BEHAVIOR), zero false positives across 36 modules.
  GF1 symptom (`WaterBase` → ServerStorage "server-side callers") reproduced + fixed.
- Generic: a renamed `OceanShimmer` rendering helper is flagged dead; a `MenuController`
  (PlayerPrefs/SceneManager/Application.Quit) is NOT flagged/stubbed.

## Review history (Claude + Codex pair, 3 rounds)
- R1 review: 3×P1 (transpile-time over-stubbing of portable scripts; transitive-prune
  crash; WaitForChild edge miss) + 2×P2 (resume loses reroute; chained prop-write veto).
- R2 review: 2 regressions from R1 fix (resume clobbers hand-edits; render allowlist
  too narrow). R3 fixed both via revalidation + broadened generic render signal.
- Each fix landed with a regression test proven to fail pre-fix.

## Gates at ship
- `pytest -m "not slow"`: 2365 passed, 45 skipped. `tools/check_no_any.sh`: pass.

## Deferred / carry-forward
- The mapped-API-call veto is documented as subsumed by the structural body veto +
  output-inertness (deliberately doesn't consult API_CALL_MAP/TYPE_MAP).
- Strategy gate (`_DECISIVE_STRATEGIES={"ai","stub"}`) is presently a forward-looking
  no-op — `rule_based`/`hybrid` never reach storage today; safe belt for degraded runs.
- TODO carry-forwards from #9 (P1 shared-flag name sanitization; P2 post-coherence
  script_type authority) remain open — unrelated to this PR.
