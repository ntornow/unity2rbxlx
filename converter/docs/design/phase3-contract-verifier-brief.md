# Phase 3 — Contract verifier: design brief (for parallel arch review)

**Status:** brief for review (2026-06-01). NOT yet approved. Reviewers: Claude
design subagent + `codex exec`, same brief to both.

## The ONE goal

> **Add a build-time verifier that proves every emitted artifact (transpiled
> Luau, script placement) actually obeys the topology authority's decisions,
> and fails the build when it doesn't — so the door-visual / cross-domain /
> unmapped-GetComponent bug families cannot silently recur.**

This is the last phase of the scene-runtime topology effort
(`scene-runtime-architecture-ir.md` §"Phase 3"). The doc lists 5 deliverables:
(1) consumer-compliance check, (2) cross-domain attribute-access check,
(3) component-availability check, (4) shadow-mode rollout + metric, (5) corpus
audit before fail-closed flip.

## Grounding facts (VERIFY OR REFUTE each before reasoning)

The design doc has been imprecise 3× this effort. I verified the following
against current `upstream/main` (#164) code. **Each reviewer must independently
confirm or refute every fact with its own file:line read before using it.**

**GF1 — The topology layer runs ONLY in generic mode, not the default.**
`classify_scene_runtime_domains` + `_build_and_apply_topology` are gated on
`self.ctx.scene_runtime_mode != "legacy"` at `pipeline.py:4608-4612`. The
default mode is `"legacy"` (`scene_converter.py:192`; u2r/convert_interactive
default). The gate comment (pipeline.py:4594-4602) states this keeps "the
legacy emit path byte-identical." → The entire architecture-ir topology effort
(Phases 1/2a/2b) is generic-mode build-out, dormant on the default path.

**GF2 — `contract_pipeline.py` is wired and reachable, NOT spike-only.**
Its docstring (lines 18-29) says "the spike harness is the only caller" but
that is STALE. `pipeline.transpile_scripts` calls `transpile_with_contract`
when `ctx.scene_runtime_mode == "generic"` (pipeline.py:2015-2033).
`--scene-runtime=generic` is a real exposed CLI flag (u2r.py:318,
convert_interactive.py:595/805/962); only `auto` is rejected. It already
fail-closes on `require_missing` / `stem_collision` /
`runtime_bearing_collision` / `stub_strategy` / `verifier` and promotes each to
`ctx.errors` (pipeline.py:2041-2061). So it is the generic-mode transpile
orchestrator — but see GF3 for what it can and cannot see.

**GF3 — Ordering: contract_pipeline runs at TRANSPILE; the full topology
artifact is built LATER at materialize.** `transpile_with_contract` runs inside
`transpile_scripts` (pipeline.py:2015). The full `build_topology(...)` artifact
(modules with final domain/container/script_class, animation_drivers) is built
in `_classify_storage` via `_build_and_apply_topology` (pipeline.py:4730), which
is a later phase (materialize/write_output). The PREPASS
(`_maybe_run_topology_prepass`, pipeline.py:4562) computes `cross_domain_edges`
+ `shared_flag_channels` BEFORE transpile. So data availability is staged:
  - pre-transpile (prepass): `cross_domain_edges`, `shared_flag_channels`
  - post-transpile (`transpile_with_contract`): emitted Luau bodies
  - post-materialize (after `_build_and_apply_topology`): the FULL topology
    artifact at `scene_runtime["topology"]` + final RbxScript placements
    (`parent_path` / `script_type`) + persisted Luau.

**GF4 — Topology artifact shape.** `build_topology.py` emits a `TopologyArtifact`
TypedDict persisted at `scene_runtime["topology"]`:
`{modules, animation_drivers, cross_domain_edges, shared_flag_channels,
caller_graph}`. Per-module: `{stem, domain, script_class, lifecycle_role,
bridge_group_id, character_attached, is_loader, provenance,
reachability_required_container, module_path}`. `cross_domain_edges` is FLAT
(`from_instance/to_instance/from_script/to_script/field/from_domain/to_domain/
owner_kind/owner_ref/resolution{strategy,event_name}/bridge_member_scripts/
payload{attribute_name,schema}`) — NOT the nested producer/consumer shape the
doc sketches (lines 215-239); the flat shape is an intentional documented
deferral (cross_domain_edges.py:39-43).

**GF5 — 10 emit-time invariants already exist for ARTIFACT-INTERNAL
consistency.** `build_topology._enforce_invariants` raises `TopologyInvariantError`
and aborts the build on 10 invariants (e.g. driver→module resolution, edge
domain validity, stable_id uniqueness, lifecycle enum, bridge_group_id refs,
module_path↔container coherence). These check the artifact against ITSELF.
**None of them check the artifact against the EMITTED OUTPUT** — that gap is
exactly deliverables 1-3.

**GF6 — None of deliverables 1-3 exist today.** No GetComponent-vs-class-mapping
check, no cross-domain SetAttribute/GetAttributeChangedSignal pairing check, no
consumer-compliance (emitted placement matches topology) check. (Confirmed by
tree-wide search; the only adjacent thing is a TEST mirror of the animation
application logic in `test_topology_fixture_round_trip.py`, not a live gate.)

**GF7 — Deliverable #2's reader-store scan gap.**
`shared_flag_channels.compute_shared_flag_channels` scans transpiled Luau for
`:GetAttribute("name")` / `:GetAttributeChangedSignal("name")` and records
`read_names` + `reader_domains` + `canonical_stores` (the WRITER funnel
destinations, fixed to `["Character","Player"]`). It does NOT capture which
instance the READER reads FROM. Catching the door canonical-store-mismatch class
(reader reads HumanoidRootPart while writer wrote Character+Player) requires
EXTENDING the reader scan to capture the reader's own store membership.

**GF8 — `api_mappings.TYPE_MAP` is now LIVE (imported `roblox_dead_modules.py:55`),
and `CharacterController → "Humanoid"` (NOT "BasePart").** The doc's deliverable
#3 motivating bug ("CharacterController = 'BasePart' mapping bug", doc line 481)
is STALE — TODO.md has no such P0; the nearest live item is P2.a (gate
FPS-specific transpiler rules, TODO.md:121-129). Deliverable #3's *mechanism*
(verify `GetComponent("X"):Method()` resolves X via TYPE_MAP and Method is valid
on the mapped class) is still a legitimate generic check; only its motivating
anecdote is reframed. NOTE: GetComponent transpiles two ways — `self:GetComponent(name)`
(peer converted-MonoBehaviour, resolved at runtime against the host component
table) and `:FindFirstChildWhichIsA("T")` (Roblox-class fallback). The check
must not false-positive on the peer-module form.

**GF9 — No corpus harness cold-converts all bundled projects for a
topology-compliance metric.** `tools/scene_runtime_spike.py` measures the
generic-contract transpile pass-rate per project (manual), but does no
topology-compliance check and isn't a recurring corpus test. Shadow-mode's
"metric across every bundled project" (doc deliverable #4) needs new harness
work, AND requires that generic-mode cold conversion of those projects is even
runnable today (open question — see DQ4).

**GF10 — Reporting + telemetry surface exists.** `ConversionReport`
(report_generator.py) has `errors`/`warnings`/`scripts.flagged_*`;
`ctx.scene_runtime["contract_fail_closed"]` already carries contract telemetry
(pipeline.py:2041). A `contract_check_violations` metric (deliverable #4) would
plug into the same surface.

**GF11 — Latent generic-mode routing gap (carry-in from PR #166).** Generic mode
skips the client/server type-fix by contract; `_decide_script_container_from_topology`
routes a LocalScript by type before domain. A defensive warning was added in
#166; full reconciliation was deferred. The brief proposes Phase 3's verifier is
the natural place to SURFACE this class (a consumer-compliance sub-check).
(Reviewers: confirm the exact function name/location and whether this is
in-scope for deliverable #1 or a separate follow-on.)

## Decision options (vote on the SAME forks)

### DQ1 — Verifier hook point / home (load-bearing)

Given GF3 (staged data availability), where does the verifier run and where does
its code live?

- **A1 (recommended): single post-materialize pass.** One verifier entry point,
  called from the materialize/write_output phase AFTER
  `_build_and_apply_topology`, with the FULL artifact + final placements +
  persisted Luau in hand. All 3 checks in one place. Code lives in a new module
  (`contract_verifier.py`) OR as new functions in `contract_pipeline.py`.
  Pro: complete data for all 3 deliverables; deliverable #1 (placement
  compliance) is ONLY possible here. Con: a second "contract" surface distinct
  from `transpile_with_contract`.
- **A2: split hook points.** Luau-only checks (#3, and the Luau-scan half of #2)
  inside `transpile_with_contract` at transpile time; placement check (#1) +
  topology-edge-reconciliation half of #2 post-materialize. Pro: reuses existing
  fail-closed plumbing for the transpile-time half. Con: two surfaces, two
  metrics merge points, #2 split across phases.
- **A3: extend `transpile_with_contract` only, defer #1.** Ship #2/#3 at
  transpile time now; defer consumer-compliance (#1) to a later slice when a
  post-materialize hook is justified. Pro: smallest first slice. Con: #1 is the
  doc's headline deliverable (door bug); deferring it weakens the phase.

Reviewers: which hook architecture, and should the code live in
`contract_pipeline.py` (keep one "contract" file) or a new `contract_verifier.py`
(separate verify-vs-orchestrate concerns)?

### DQ2 — Slice breakdown (vote on ordering + boundaries)

Proposed (each an independently-reviewed stacked PR per the multi-slice workflow):

- **Slice 1:** Verifier skeleton + deliverable #3 (GetComponent-vs-TYPE_MAP),
  shadow-mode only (emit warnings + `contract_check_violations` metric + report
  field). Simplest, pure Luau scan; establishes the module, the metric, the
  shadow scaffold, the env-var escape hatch wiring (inert until flip).
- **Slice 2:** Deliverable #2 (cross-domain attribute pairing), INCLUDING the
  GF7 reader-store-scan extension. Reconciles each writer/reader pair against
  `cross_domain_edges` + `shared_flag_channels`. Higher risk.
- **Slice 3:** Deliverable #1 (consumer compliance: emitted placement/script_class
  matches topology), INCLUDING surfacing the GF11 type-before-domain class.
  Needs the post-materialize hook (DQ1).
- **Slice 4:** Corpus shadow-mode audit harness (GF9) + categorize/fix real
  offenders + flip to fail-closed behind the one-release env-var escape hatch.

Alternative orderings to weigh: should #1 (placement compliance, the headline
door-bug deliverable) come FIRST to validate the post-materialize hook before
the Luau-scan checks? Or is #3 (pure Luau scan, lowest risk) the right
skeleton-establishing first slice? Should the GF11 reconciliation be its own
slice rather than folded into slice 3?

### DQ3 — Shadow-vs-fail-closed sequencing

The doc mandates shadow first, flip to fail-closed only when the metric is clean
on a cold conversion of every bundled project + a one-release env-var escape
hatch (doc lines 483-492, 570). Options:

- **B1 (doc-default): per-phase flip.** Each check ships shadow; ALL flip
  together in slice 4 after one corpus-clean release.
- **B2: per-check independent flip.** Each check flips to fail-closed on its own
  cadence as its corpus metric goes clean (deliverable #3 may be clean
  immediately; #2 may take longer). Pro: ships enforcement sooner where safe.
  Con: more flip events to manage.
- **B3: fail-closed-from-start for #3 only.** GetComponent-vs-TYPE_MAP is a hard
  structural error (the place would error at runtime); arguably no shadow period
  needed. Keep shadow for #1/#2 (which may have legitimate generic-mode patterns
  the corpus doesn't cover).

Reviewers: which sequencing, and is there a check that warrants fail-closed
without a shadow period?

### DQ4 — Is generic-mode corpus conversion even runnable today? (blocking risk)

GF1 says topology+contract only run in generic mode; GF9 says no corpus harness
exists. The shadow-mode metric (deliverable #4) presupposes that
`--scene-runtime=generic` cold conversion of every bundled project
(SimpleFPS, Gamekit3D, RedRunner, 3D-Platformer, …) completes far enough to emit
Luau + a topology artifact. If generic mode currently fail-closes (via the
EXISTING `transpile_with_contract` gates) on most of those projects, the
verifier's corpus can't be exercised and the shadow→flip plan stalls.

Options:
- **C1:** Treat "generic-mode runs to completion on project X" as a precondition;
  scope the corpus to the subset that runs today (SimpleFPS at minimum), and
  `log()` which projects are excluded and why (no silent cap).
- **C2:** Run the verifier in shadow mode over whatever Luau IS emitted even on
  fail-closed runs (the transpile output exists before the fail-closed abort), so
  the metric covers partial conversions too.
- **C3:** Investigate generic-mode corpus runnability as slice 0 (spike) before
  committing to the shadow-mode rollout shape.

Reviewers: is DQ4 a real blocker, and which mitigation?

## What reviewers should return

For each of DQ1-DQ4: a vote (with the option letter) + one-paragraph rationale
grounded in verified file:line facts. Plus: any grounding fact you REFUTE (with
the contradicting file:line), and any 5th decision the brief missed. Flag any
deliverable that is mis-scoped given the verified code (e.g. if #1 is partially
already covered, or if #2's mechanism can't reconcile against the flat edge
shape).
