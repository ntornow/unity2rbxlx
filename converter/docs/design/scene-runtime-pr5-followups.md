# PR5 follow-ups (deferred)

PR5 landed:

- ``--scene-runtime=auto`` lifted at every CLI / skill front door
  (``u2r convert/eval``, ``convert_interactive
  transpile/assemble/upload``). The PR4-era rejection text is gone.
- ``Pipeline._check_auto_fail_closed`` subphase runs after
  ``_classify_storage`` and before
  ``_subphase_inject_scene_runtime``. Aggregates the eight
  ``FailClosed`` kinds (``verifier``, ``require_missing``,
  ``require_collision``, ``runtime_bearing_collision``,
  ``stub_strategy``, ``both_side_api``, ``intra_class_conflict``,
  ``reachability_conflict``) from already-transpiled artifacts +
  classifier-stamped ``scene_runtime.modules`` rows. No AI work is
  re-driven.
- ``contract_pipeline.detect_fail_closed_signals`` public helper
  surfaces the same fail-closed list ``transpile_with_contract``
  would produce, but pure-function over existing artifacts.
- Routing semantics (PR5 conservative): no signals -> route to
  ``generic``; one or more signals -> route to ``legacy`` (host
  runtime emit suppressed, every trigger staged on
  ``ctx.scene_runtime["auto_fail_closed"]`` + logged at WARNING).
- R4-P1.1 absorbed: ``generate_scene_runtime_plan_module`` gains
  optional ``scene_namespace=`` kwarg; the multi-scene pipeline
  threads the active scene's planner key into the autogen step so
  each ``.rbxlx`` carries only its own scene's instances/references.
- New public ``compute_scene_namespace`` helper in
  ``scene_runtime_planner`` mirrors the planner-internal key
  derivation for cross-module reuse.

Test additions: ``test_scene_runtime_auto_mode.py`` (22 cases),
``test_scene_runtime_host_emit.py::TestPlanModulePerSceneScoping``
(5 cases) + ``TestComputeSceneNamespace`` (3 cases). Suite:
1811 -> 1841 passed; no-Any gate pass.

The carve-outs below are deferred from PR5. They are NOT merge-
blocking for PR5's auto-mode + per-place plan scoping landing.

## 1. Canary gate (MCP-driven five-project playtest)

**Where:** the design doc PR5 row + brief require five canary
projects to play under ``--scene-runtime=generic`` (and route through
``auto``'s generic branch with no fail-closed fallback) before PR7
is cut:

  1. trash-dash (state-machine, ~12 cross-domain edges expected)
  2. SimpleFPS (~6-8 edges)
  3. Cross-domain-heavy (UI-controller / server-gameplay split)
  4. Trigger/collision-driven (regresses rule-(f) + ``host:connect``
     in ``Awake``)
  5. Multi-scene project (exercises R4-P1.1)

**Why deferred:** the PR5 agent session does not have a Roblox
Studio instance running and cannot drive Studio MCP playtest from
the contracting agent context. Driving the canary verification
requires (a) per-canary AI transpile (~30 min each at PR3a's
33-module SimpleFPS rate; trash-dash is 33 MBs; cross-domain-heavy
+ trigger/collision canaries are synthetic and need first-time
construction), (b) Studio open + place opened per canary, (c) MCP
``screen_capture`` + ``execute_luau`` + ``start_stop_play`` to
verify each canary actually plays, (d) byte-equivalence comparison
against the legacy baseline for the regression invariant.

**Status as of PR5 head:** ``auto``-mode wiring + per-place scope
ship under unit/integration tests + the no-Any gate. The canary
gate is the PR5 -> PR7 cutover condition and remains the
escalation point: the PR5 codeable work is complete but the user
must drive the five-canary playtest manually (or in a separate
agent session with a Studio instance) before the PR7 default-flip
PR is cut.

**Carve-outs that may surface during the canary run:**

  - **Cross-domain-heavy + trigger/collision canary fixtures**:
    these need first-time authoring under
    ``test_projects/``. SimpleFPS is already in ``test_projects/``
    (presently empty -- see prior PR3a compliance spike); trash-dash
    is at ``/Users/jiazou/workspace/trash-dash``.
  - **Multi-scene canary**: needs at least two ``.unity`` scenes
    with cross-scene refs; the R4-P1.1 unit tests + the host
    runtime's parent-edge handling cover the contract, but the
    canary tests the real per-place plan emission.

## 2. PR3c arch-caveat decision (still deferred)

**Where:** PR4 followups item documents the open question --
``_classify_storage`` runs AFTER ``convert_scene``, so PR3c's
``module_row["domain"] == "legacy"`` guard in
``scene_converter._compute_serialized_field_child_suppressions``
is dead code today. Three options were documented:

  - **(a)** restructure ``convert_scene`` to mutate
    ``state.rbx_place`` in place (instead of returning a new one)
    and move emit/cohere/classify ahead of it.
  - **(b)** run a ``classify_storage``-equivalent against
    ``state.transpilation_result.scripts`` (TranspiledScript, not
    RbxScript) pre-convert; stamp domain onto
    ``ctx.scene_runtime``; keep the existing
    ``_classify_storage`` subphase for parent_path.
  - **(c)** have PR3c's guard read a pre-classify oracle (e.g.
    a thin "module is fail-closed" predicate computed pre-convert).

The PR5 brief speculated option (b) might come for free with the
auto-mode work because PR5 might naturally need a pre-convert
classify-equivalent. **It does not.** PR5's
``_check_auto_fail_closed`` runs in ``write_output`` AFTER
``convert_scene``, AFTER ``_classify_storage`` -- exactly the same
ordering as PR3c. The fail-closed scan reads the post-classify
``scene_runtime`` artifact + the post-transpile result; it does
NOT need a pre-classify oracle. The PR5 auto-mode work touches the
same artifacts at the same lifecycle stage as PR3c.

**Status as of PR5 head:** still deferred. Without canary
evidence (item 1 above), there is no concrete repro showing the
PR3c guard fails in practice. The PR3c comment already documents
the limitation: under generic, UI controllers whose modules
ultimately fail-closed lose their static subtree without host
re-population. The conservative fallback (``runtime_bearing``
predicate) protects most projects.

**When the canary playtest runs (item 1)** check whether any of
the five canaries has a fail-closed UI controller. If yes -> the
PR3c bug bites and the decision between (a)/(b)/(c) needs to be
made with that fixture as the test target. If no -> still deferred
with empirical evidence the rotation order is moot for the canary
set.

## 3. Byte-identical legacy re-route on auto-fallback

**Where:** ``Pipeline._check_auto_fail_closed`` flips
``ctx.scene_runtime_mode`` to ``"legacy"`` on fail-closed signals,
but by then:

  - The transpile phase already ran with
    ``runtime_mode="generic"`` (transpiler input parameter is
    ``runtime_mode``, NOT ``scene_runtime_mode``; the in-pipeline
    auto path threads ``"generic"`` through because the contract
    pipeline is the only thing that can produce the fail-closed
    signals). The emitted Luau still carries contract shapes
    (``self.host:connect``, ``require("@scene_runtime/...")``).
  - ``convert_scene`` already ran with ``scene_runtime_mode``
    visible as ``"auto"``. PR3c's scene_converter carve-outs
    (inactive retention, ``_compute_serialized_field_child_suppressions``)
    gate on ``== "generic"``, so they did NOT fire under auto --
    the .rbxlx is byte-identical to legacy for the scene-converter
    side. But UI translator carve-outs likewise.

**Why deferred:** the design doc says ``auto`` falls back to a
"clean legacy (mode-isolated output dir, no salvage)". Implementing
that means the pipeline must (i) detect the fail-closed signal
EARLIER (which we cannot -- most signals are post-transpile), or
(ii) wipe + re-run from ``extract_assets`` after fallback decision.
(ii) is technically possible but doubles conversion time for any
auto-mode run hitting a fail-closed signal.

**Status as of PR5 head:** PR5's fallback is "host emit suppressed
+ logged + auto_fail_closed payload on ctx". Byte-equivalence with
``--scene-runtime=legacy`` is NOT guaranteed on the fallback path
(transpile-side artifacts), though PR5's R1-P1 absorption snapshots
+ restores classifier-driven ``parent_path`` mutations on
fallback so storage routing matches legacy. Operators wanting
hard byte-equivalence rerun with ``--scene-runtime=legacy``. The
trade-off is honest in logs; the structured publish-summary
surface for ``auto_fail_closed`` is the item 5 follow-up.

## 4. Per-module fallback coexistence

**Where:** the brief's preferred semantics ("on any module
fail-close, fall back to legacy for THAT module ... both halves
coexist in the same conversion output -- host runtime handles its
modules, legacy bootstrap handles the rest"). The design doc says
project-level fallback. PR5 implemented project-level.

**Why deferred:** per-module fallback requires:

  - ``_subphase_inject_autogen_scripts`` to learn which scripts
    are host-runtime-managed and skip them in the legacy
    ``ClientBootstrap`` require loop.
  - The host runtime to ship even when SOME modules fail-closed
    (currently it ships only when ZERO modules fail-closed).
  - Per-module domain assignment to survive partial fallback.
  - The PR4 followups item 3 ("disable ClientBootstrap + legacy
    autogen under generic") to land FIRST -- coexistence cannot
    work cleanly when both bootstrap paths target the same
    scripts.

**Status as of PR5 head:** project-level fallback ships in PR5.
Per-module coexistence is a follow-up that should land alongside
PR4-followup item 3 (gating the legacy emit-side autogen path
per-script).

## 5. Auto-mode publish summary surface

**Where:** ``ctx.scene_runtime["auto_fail_closed"]`` is stashed
during ``_check_auto_fail_closed`` but no publish/report
formatter renders it. Operators today see the triggers via the
WARNING-level logs only.

**Why deferred:** the report surface lives in
``convert_interactive.py``'s ``_emit`` envelopes + ``u2r.py``'s
publish summary path. Adding a structured ``auto_fail_closed``
section is mechanical but requires touching both surfaces +
adding test coverage for the report shape. PR5 prioritizes the
routing decision + signal aggregation; the report formatting
layers on top without changing the routing behaviour.

**Status as of PR5 head:** triggers are stashed + logged; no
structured report surface. Follow-up should add a ``--report``
flag or ``conversion_summary.json`` entry.

## 6. Carry-overs from earlier PRs

- [ ] **PR4-followup**: implement ``scene_converter`` inactive
      retention (still deferred -- generic-only carve-out).
- [ ] **PR4-followup**: implement ``ui_translator`` serialized-
      field child suppression (still deferred).
- [ ] **PR4-followup**: scope a "generic emit-side off-switch" PR
      that decides per-autogen-script whether to keep or replace
      under generic.
- [ ] **PR4-followup**: measure plan size on the canary projects;
      implement sharding if any place overflows the publish
      budget. R4-P1.1 per-place scoping reduces the per-place
      plan size, which deflates the risk but does not eliminate
      it.
- [ ] **PR5**: remove the legacy ``scene`` alias on
      ``SceneRuntimeDisplacedInstance`` after one release cycle
      (PR4 introduced ``owner_kind`` + ``owner_ref``; the
      ``scene`` field is still emitted for back-compat).
- [ ] **PR5-followup**: plumb ``tag`` from Unity ``m_TagString``
      into the planner instance rows so
      ``host.findGameObjectsWithTag(tag)`` is non-empty under
      generic. PR4's runtime already reads ``inst.tag``; the
      planner just needs to emit it.
- [ ] **PR5-followup**: honor ``target_component_type`` for
      ``target_kind: gameobject`` refs (PR4 P2).
- [ ] **PR3c-arch-caveat (STILL DEFERRED)**: see item 2 above.

## Codex review absorption

Codex review of PR5 was capped at 2 rounds per
``[[review_loops_stay_high_level]]``. Per-round absorption +
deferrals will be appended below when the codex pass runs.
