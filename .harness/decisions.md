
## Run main-20260604-201520 — scene-runtime recut + refactor execution (2026-06-04)

# Decisions — drive run main-20260604-201520

Task: scene-runtime recut + mega-file refactor → execution (validate-and-re-baseline, NOT redesign).
Design of record: PR #177 (`docs/scene-runtime-refactor-execution-plan`). 9 locked decisions stand.

## Plan stage

- **Track-0 re-baseline (2026-06-04):** LOC figures (6495/5553/5373) confirmed exact. Refreshes:
  packs 27→29 registered (24 after PR8, NOT 22); `_ctx()` 58→66 (grep-driven, harmless);
  PR-D phase table confirmed stale (missing `plan_scene_runtime` + `materialize_and_classify`,
  both live). **No locked decision invalidated.** Figure refreshes land in their follow-on runs.

- **D1 — Run scope = PR5 canary (Slice T + Slice H) only**, one PR. Follow-ons: PR-B, PR6/7/8,
  Track 2 (PR-G/PR-H), Track 3 (PR-C/D, PR-E0/E/F). Classification: User-Challenge → Gate A.

- **D2 — Slice T ‖ Slice H** (disjoint owns: `contract_pipeline.py`+new lowering file vs
  `module_domain.py`; no data dep). Classification: Mechanical.

- **D3 — autoplan reduced to validate-against-locked-decisions** per task directive; dual-voice
  design review (Claude + codex, the owed second voice) is the substantive gate. Classification: Taste.

- **D4 — Slice T = GENERAL structure-gated child-index rule** (skip injected non-spatial children
  for any GetChild), not turret-name-gated (no-hardcoding). Classification: Mechanical.

## Empirical re-baseline (the load-bearing finding)
Fresh `u2r.py convert SimpleFPS --scene-runtime generic --no-upload --clean` @ current main:
- **Slice H STALE:** HudControl already `domain="client"`, `ModuleScript`, `ReplicatedStorage`,
  `runtime_bearing`, `requireable`, no fail-close → classifier rule is a no-op. → **D5: drop Slice H.**
- **Slice T LIVE:** Turret/Turret(1)/Turret(2) Parts have Sound at child[0], Base at child[1];
  Turret.luau does `GetChildren()[1]:GetPivot()` → Sound:GetPivot() crash. → fix as designed.
- **D6: contract_verifier in SHADOW mode** (1 info-level dead-module violation; no HudControl trip).

Design review: round 1 FINDINGS (both voices flagged Slice H stale) → round 2 CONVERGED
(empirical repro resolved all P1s by removing the no-op slice). Codex = the owed second voice,
obtained this run (266k tokens, repo-grounded).

## Slice review round 1 (codex caught what the passive reviewer missed)
- Claude reviewer: CONVERGED (missed all of codex's findings — asserted receiver-parse + string-strip
  "hold up"; codex produced counterexamples to both). Adversarial voice is load-bearing.
- codex: 2 BLOCKING (over-broad scope clobbers camera weaponSlot; nested-chain span corruption) + 1 MAJOR
  (backtick strings unstripped) + 1 MINOR (depth-0 whitespace receiver parse).
- **D7 — Reuse the existing legacy pack `_fix_unity_transform_child_index`, don't rebuild.** It already
  solves this (the `__unityChild` helper + simple-receiver regex + `_luau_pos_is_code`), is in the frozen
  legacy baseline, and its simple-receiver regex + non-overlapping `re.sub` structurally avoid codex's
  nested-chain corruption (BLOCKING #2) and the whitespace mis-parse (MINOR #4). codex BLOCKING #1 is MOOT:
  legacy already rewrites all `GetChildren()[n]` incl. weaponSlot (proven, in-baseline) → generic parity is
  correct. Extract the shared logic into a helper both the pack + generic path call (owns += script_coherence_packs.py).
  Classification: Mechanical (forced by "reuse don't rebuild" + the existing-pack-search project rule).

## Slice 1.1 fix round 1 (implementer)
- Extracted shared lowering logic (`_UNITY_CHILD_HELPER`, `_GETCHILDREN_INDEX_RE`,
  `_luau_pos_is_code`, `rewrite_child_index_source`, `source_has_child_index`) into
  `child_index_lowering.py`; `script_coherence_packs.py` re-imports + re-exports them, legacy
  pack behavior byte-identical (test_unity_transform_child_index.py + full fast suite green).
- `contract_pipeline.py` in owns but UNCHANGED: the existing `lower_child_index(transpilation.scripts)`
  wiring (generic-only) already works with the rewritten helper on `luau_source`; no edit needed.

## Run hudbind-20260604-223428 — generic UI gameObject boot-race / HUD fix (2026-06-05)

# Decisions — drive run hudbind-20260604-223428

Task: generic-mode UI-controller gameObject binding (HUD fix). Branched off upstream/main 519a965
(has the merged turret fix PR #178).

## Root-cause spike (plan stage)
HudControl `self.gameObject` nil → dead HUD. NOT classifier (correct), NOT turret fix. 3-link gap:
1. planner doesn't link UI-controller → host UI GameObject (`game_object_id: None`).
2. UI hosts not SRI-stamped (0/32 UI items carry `_SceneRuntimeId`; ui_translator stamp path gated, not firing).
3. host `workspaceFind` (autogen.py:676) already scans PlayerGui by SRI — has nothing to hit until 1+2.

## Scoping checkpoint (waiting on user)
Fix spans scene_runtime_planner/topology + ui_translator + autogen/host — a full design→review→
implement→harden→Studio cycle, not the one-liner the task implied. Surfaced for direction.

## Slice 1.1 implementation deviations
- **Deferral, not in-loop staging.** The design offered two options (stage UI
  resolution before Start arms, OR defer construction out of the synchronous
  pass). I chose full deferral: a UI-owned instance whose `workspaceFind`
  misses is NOT built during the synchronous boot at all — it is collected and
  completed in its own late lifecycle batch from a spawned coroutine after the
  synchronous boot finishes (after `self._crossDomainEdges` is set). This is
  the only option that both (a) keeps the `start()`-never-yields invariant for
  every synchronously-built component intact and (b) is compatible with an
  event-driven (yielding) `awaitUiHost`. An "in-loop stage that completes
  before Start arms" is incompatible with an event-driven wait, since the wait
  is inherently async and Start is armed via `task.defer` at end of the
  synchronous frame.
- **Shared `_applyPlannerFlagsAndTag` helper.** Extracted the duplicated
  active/enabled/tag registration from the scene loop and prefab loop into one
  method so the synchronous and deferred-UI paths stay byte-identical (no
  behavior change for the existing paths; existing tests stay green).
- **`awaitUiHost` host-surface helper** added to the generic CLIENT entrypoint
  only (`autogen.py`). Server entrypoint + legacy emit untouched. Engine guards
  `if self._services.awaitUiHost` so the server partition (no PlayerGui, no UI
  instances in its domain) is unaffected. Event-driven via
  `PlayerGui.DescendantAdded` + initial scan; 10s timeout = fail-closed
  diagnostics (engine `warn`s the id, never binds nil).

## Slice 1.1 fix-round 1 (dual-voice review: 2 BLOCKING + 2 MAJOR + Claude MAJOR)
Reworked the per-component deferral into a correct BATCHED deferral. Changes:
- **Batched lifecycle (BLOCKING #1).** Replaced the per-entry
  `_runAwakeEnableStart({comp})`-as-host-resolves with a barrier: spawn one
  resolver coroutine per deferred entry (event-driven waits run concurrently),
  and when ALL hosts have resolved, run `_completeDeferredBatch` — build/inject/
  wire the whole resolved set ordered by the planner `lifecycle_order`, then run
  `_runAwakeEnableStart` over the WHOLE batch once. The batch's Awake/OnEnable
  all precede its Start; intra-batch `lifecycle_order` and same-GO GetComponent
  in Awake hold. Lateness vs the synchronous batch is inherent + accepted.
- **Inbound-ref back-patch (BLOCKING #2).** `_wireReferences` now records refs
  whose component-kind target is a deferred (not-yet-built) instance into
  `_inboundRefsToDeferred`; `_completeDeferredBatch` sets the stored field on the
  source once the target builds (incl. prefab `externalRefs`). New
  `_deferredInstanceIds` set tells the sync pass which targets are deferred.
- **Server/no-resolver safety (MAJOR #3).** `_resolveDeferredUiInstances` now
  gates on `services.awaitUiHost`: absent → one-shot `workspaceFind` per entry
  then `_completeDeferredBatch` builds even with nil gameObject (pre-slice
  behaviour), NEVER defers-then-never-builds.
- **Runtime-prefab-spawn orphan (Claude MAJOR).** Added a `deferUiMisses` param
  to `_constructPrefabClone`: only the scene/prefab BOOT placement loop passes
  `true`; `instantiatePrefab` (runtime spawn) leaves it false so a runtime-
  spawned UI host resolves one-shot against its LOCAL clone (the right path —
  it doesn't race PlayerGui) instead of being deferred into a queue that
  `instantiatePrefab` never drains.
- **`awaitUiHost` connect-first (MAJOR #5).** Connect `DescendantAdded` BEFORE
  the initial `GetDescendants` scan so a clone landing in the gap can't be
  missed; scan-hit returns inline (thread not yet suspended), event/timeout
  resume the yielded waiter.
- **Tests (#6).** Added batched-deferral + inbound-back-patch test (FAILS
  pre-fix), server/no-resolver one-shot test (FAILS pre-fix), and a
  coroutine-driven `awaitUiHost` test (initial-scan hit / DescendantAdded-after-
  miss / timeout→nil) driving the REAL emitted resolver body.

## Slice 1.1 r3 (commit 3f799c1)
- **Registry key is placement-scoped (BLOCKING).** `_componentByInstanceId`,
  `_deferredInstanceIds`, and the inbound back-patch `target_ref` all key by
  `_idWithPlacement(placement_id, instance_id)` so multi-placed prefab clones
  don't collide. Scene ids stay raw (already globally unique). `_unregister`
  clears the EXACT key (`meta.registryKey`) only when it still points at this
  comp (a later placement/runtime-spawn legitimately overwrites the key).
- **Pass 3b back-patch now resolves against the engine union map** (scoped)
  instead of the batch-local `builtByInstanceId` (raw) — so a cross-host
  inbound ref binds the correct placement's clone and resolves on whichever
  batch builds the target, not only the current one. Safe because the only
  reader runs once at end of `start()` (boot-only).
- **Dependency-aware deferred batching (MAJOR).** Inter-group dependency edges
  (a group's outbound `component`-ref to a deferred component on another host)
  drive (1) a Kahn topological spawn order (deps first) and (2) a per-group
  `waitForDeps` before `_completeDeferredBatch`. KEY: a group waits ONLY on
  groups it references — unrelated never-resolving hosts never gate it (r2
  BLOCKING preserved).
- **TRADEOFF — cycle / never-resolving dependency resolution.** A dependency
  cycle is broken by appending the cycle remnant to the topo order in stable
  order; the runtime `waitForDeps` loop then times out on the unresolved dep
  and proceeds with nil + warn (fail-closed, no hang). The wait is a bounded
  poll: real-time `DEP_TIMEOUT=10s` via `now()` for production PLUS a
  `DEP_MAX_ITERS=200000` hard cap (the unit harness' `task.wait` returns
  without advancing the mock clock, so a clock-only deadline would spin
  forever there — the iteration cap guarantees termination). Polling (vs a
  fully event-driven dependency wait) was chosen as the pragmatic, testable
  shape; an event-driven rewrite is a possible future refinement, not needed
  for correctness.

## --- Run: generic-converter-step1-player-20260607T091314 (Phase 1 / Gate 0) ---
# Decisions — generic-converter Step 1 (player-embodiment authority)

## D0 — precondition: untracked scratch in main tree (Mechanical)
`git status --porcelain` non-empty: only untracked `.claude/` and `apikey_run`
(no tracked modifications). A worktree-based /drive run creates the feature branch
as a pure ref off `upstream/main` and does all work under $RUN_DIR — the main
working tree is never checked out into or mutated. → PROCEED; untracked scratch is
not at-risk work. Rationale logged per the precondition's own intent ("don't disturb
the user's uncommitted work").

## D1 — base ref (Mechanical)
Branch off `upstream/main` @ 2cbed06 (includes PR #184 / Step-0 net), NOT local
`main` or the fork's `origin/main` (lag risk per memory). featureBranch =
drive/generic-converter-step1-player-20260607T091314.

## D2 — acceptance target is concrete (Mechanical)
Step-1 acceptance = the player-bind authority works on a FRESH cold conversion, THEN
flip the Step-0 net:
  - converter/verify_hook.py:35  REQUIRE_PLAYER_BIND False -> True
  - .github/workflows/test.yml:434  REQUIRE_PLAYER_BIND "0" -> "1"
  - converter/tests/test_verify_hook.py:166,342 assertions False -> True
  - test_behavior_fixture_contract.py bind fixtures must pass under the flip
The flip is the LAST act, only after C (host authority) demonstrably binds.

## D3 — design.md is a distillation, not a re-derivation (Mechanical)
Wrote $RUN_DIR/design.md as a faithful high-level restatement of the CONVERGED
authoritative doc converter/docs/design/player-embodiment-authority-design.md.
Did NOT re-derive or re-open any resolved decision.

## D4 — 5-phase mapping of the doc's Gate0 + Slices 1-4 (Mechanical)
Mapped the doc's "Slicing" + "Sequencing (strangler-fig)" 1:1 onto 5 strictly
sequential phases: P1=Gate0 (primitives proof + reusable host-harness + shape-variance
corpus), P2=Slice1 (host authority, A as fallback), P3=Slice2 (U1 + aim-read +
respawn/teleport), P4=Slice3 (B backstop), P5=Slice4 (delete A + FLIP REQUIRE_PLAYER_BIND
0->1 as the LAST act). No parallelization; each phase relies on the prior. This matches
the prompt's suggested mapping and the doc's authoritative sequencing.

## D5 — file-path nesting confirmed (Mechanical)
Cited anchors live under a doubly-nested layout in base ref 2cbed06: pipeline code under
converter/converter/ (scene_runtime_planner.py, autogen.py, movement_facet_lowering.py,
camera_facet_lowering.py), runtime under converter/runtime/ (scene_runtime.luau,
scene_camera_input.luau), and verify_hook.py + tests under converter/. The Step-0 net
substrate (verify_hook.py REQUIRE_PLAYER_BIND=False, _camera_input_harness.py,
test_camera_follow_math.py, test_tick_substrate.py) exists in 2cbed06 but NOT on the
local HEAD 177e21d (pre-#184). The /drive run must branch off upstream/main @2cbed06 per D1.

## D6 — zero open questions (Taste)
Raised ZERO open questions. The doc records all six original open questions as RESOLVED,
eng-review is CLEAR (0 new P1), and all cited anchors were confirmed present in 2cbed06.
No gap found between the design doc and current upstream/main warranting a close call.

## D7 — respawn narrowing; teleport non-load-bearing (Taste/architecture)
Resolves codex BLOCKING (Phase 3 respawn not host-authoritative under U1 shadow-sync —
the AI's respawn `PivotTo` is overwritten next frame, making correctness lean on
paradigm B teaching the AI to call `teleport`, a forbidden rung-2b dependency). RESOLUTION:
respawn is ALREADY server/engine-owned and deterministic (autogen `GameServer`
CharacterAdded spawn at autogen.py:105 + engine SpawnLocation re-spawn on death — this
path exists independent of the AI shape). C's respawn responsibility narrows to the
per-player LIFECYCLE RESYNC: re-acquire the new character on `CharacterAdded` and resync
camera/yaw/eye-follow — C-owned, deterministic, NO dependency on the AI's `PivotTo` and
NO dependency on B. Programmatic mid-game `teleport(cf)` is a host helper that stays
client-request/server-apply but is explicitly NON-LOAD-BEARING (a fidelity nicety):
if the AI never calls it the stated goal still holds because respawn is server-owned.
Phase 3 cold proof becomes "Shoot hits the crosshair; on death the CHARACTER respawns at
the spawn point (server-owned) and the camera/yaw resyncs to the new character" — NOT
"the AI's TakeDamage PivotTo moves the character."

## D8 (revised) — Phase-2 proves C DOMINATES with A active; NO suppression (Taste/architecture)
Resolves codex round-1 MAJOR (double-authority, only A-miss tested) AND codex round-2
BLOCKING. My first cut (D8 v1) suppressed player-path A keyed on `has_character_controller`.
Codex r2 showed that is WRONG: A is the mechanism that NEUTRALIZES the raw AI camera/move
writes on A-hit shapes (the `dde248` raw output natively contains a direct camera write +
`humanoid:Move(` — test_movement_facet_lowering.py:153/181). Suppressing A would EXPOSE
those raw writes during Phases 2-3, before B's lexical rejects land in Phase 4 — so C would
coexist with the RAW AI writes, not be single-authority. This also DEVIATED from the
authoritative doc's own §Sequencing intent: "C proves it dominates on a cold shape WITH A
still present, then A is removed." REVISED RESOLUTION: leave A ACTIVE (no suppression).
Phase 1 Gate 0 proves C's pre/post camera writes + post-component `Humanoid:Move` dominate
a competing mid-Update write by last-writer-wins. Phase 2 adds C with A active and proves C
structurally DOMINATES on BOTH the A-miss `cold3a59` and the A-hit `dde248` shapes (final
camera CFrame + Humanoid walk vector are C's each frame), whether the competing write is
A's lowered call or the raw AI call. NOT a single-writer claim — multiple writes, C wins by
ordering (downstream of Gate 0, so no new unproven assumption). B stays non-load-bearing
(C dominates structurally, not via B). A's code is DELETED in Phase 5. This is codex's own
"coexistence-proof on A-hit" fix option (the doc-faithful one).

## D9 — C owns the SINGLE E2E mouse-channel read; reuse pure advance() not _readDelta (Taste/architecture)
Resolves Claude round-3 MAJOR. SceneCameraInput._readDelta CONSUMES the E2E-channel ACK
(consume-once on workspace attrs). With A active driving the singleton's step->_readDelta
in-band on A-hit shapes, exactly one of {A,C} consumes the injected delta/frame; if A wins
the ACK race C's yaw advances by 0 -> the A-hit dde248 dominance fixture flakes on pairs()
order = green/red for the wrong reason. RESOLUTION: C reuses the PURE advance() helper
(scene_camera_input.luau:53-61), NEVER step/_readDelta; C owns the SINGLE per-frame channel
read in its pre-Update input snapshot. Add Gate-0 primitive (d): two readers in one frame,
assert the channel is consumed exactly once and C is the consumer. No change to paradigm C /
phase count / ordering. Also folded codex r3 MINOR: dominance claim scoped to
CurrentCamera.CFrame + the character's Humanoid move-intent (raw rig PivotTo drift vestigial
until Phase 3 U1); recoil-on-A-hit knowingly degraded until Phase 5 (fidelity floor).

## Phase 1 detailed-design decisions (design-phase1.md)

### PH1-D1 — extend existing harnesses, don't fork (Mechanical)
Primitives (a),(b),(d) extend tests/_camera_input_harness.py; (b),(c) ride
test_tick_substrate.py's _two_component_preamble. Only new harness vocabulary: an optional
`extra_mock_setup` kwarg on camera_input_preamble + an always-present ordered `_attrWrites`
log on workspace:SetAttribute (to assert "E2E acked exactly once, in order"). Default-empty
=> the 5 existing test_camera_follow_math.py tests stay byte-identical (AC0 guards). Per the
prompt's explicit "extend THAT harness's vocabulary" instruction.

### PH1-D2 — model "reader C" as pure-advance channel snapshot, not a 2nd acquire() (Taste)
SceneCameraInput.acquire() returns the sticky module singleton, so a 2nd acquire() can't model
two independent readers. (d) models C as scenario-luau that snapshots the ONE workspace E2E
channel + feeds SceneCameraInput._advance(...), and races it against the REAL cam:step/_readDelta
(A). Anchors C's modeled behavior on the deterministic channel semantics (D9) and asserts
HAPPENS-BEFORE (C precedes A) + OWNERSHIP (C advances; A sees zero) per followups Gate-0(d).

### PH1-D3 — corpus shapes as checked-in fixture files (Taste)
The cold3a59 raw Player.luau is NOT committed anywhere (git ls-files converter/output/ = 0
rows; output/ is git-ignored). dde248 lives only as inline fragments in
test_movement_facet_lowering.py. RESOLUTION: carry BOTH as permanent fixture files under
tests/fixtures/player_shapes/ (dde248 assembled from the inline fragments; cold3a59
reconstructed from the documented shape design-doc:31-33,203) + a README provenance + a
pure-Python shape-fact guard (AC6) so the fixtures can't silently drift from the live
transpiler shape (the source artifact can't be auto-re-derived).

### PH1-D4 — _tick line-number correction (Mechanical, divergence)
Real _tick is at scene_runtime.luau:2792 (Update pass pairs() at :2794), NOT :2656 as the
design doc cites. followups.md's :2794 reference is correct. design-phase1.md cites the real
lines.

### PH1-D5 — shared-mock surface + actor-aware ack log (Structural, resolves dual-voice review)
The tick harness (`_two_component_preamble`) installs NO `workspace.CurrentCamera` and NO
Humanoid mock and takes only `writer_methods`/`reader_methods` — so AC2/AC3 (camera cell) and
AC4 (Humanoid recorder) had no home, and §1.2's "no new helper / no signature change" claim
was FALSE about there being no surface to specify. Resolutions (all applied to design-phase1.md):

- SHARED-MOCK SURFACE (owned by slice 1.1, consumed by 1.2): declared in the scenario body
  appended AFTER `_two_component_preamble` (verified `bus` is reachable from both the component
  method strings and the trailing scenario luau in the real harness), so NO signature change.
  AC2/AC3 use a plain shared `bus.cam = {CFrame = <init>}` cell — NOT real
  `workspace.CurrentCamera` (the tick harness doesn't load the camera module; a bare table
  models last-writer-wins). AC4 uses `bus.humanoid = {Move = function(self,v) table.insert(
  bus.moves, v) end}` + `bus.moves = {}`. Component writes are mid-Update; host pre/post writes
  are scenario luau OUTSIDE `heartbeat:fire`.

- ACTOR-AWARE ACK LOG (AC5): `_attrWrites` entries become `{name, value, actor}`, stamped from
  a chunk-level `workspace._currentActor` the scenario sets to "C" before C's manual ack and to
  "A" before firing A's in-band `cam:step`. AC5(d-ii) asserts the FIRST `E2EMouseAckSeq` entry
  has actor=="C". Reader C HAND-ROLLS the consume-once protocol (`_advance` is PURE, never
  touches the channel; C replicates `_readDelta`'s read-seq/ack/read-delta then feeds
  `_advance`; C never calls `_readDelta`). AC5(d-iii) seeds a NONZERO base mouse delta so A's
  later `_readDelta` returns EXACTLY that base — proving A saw zero INJECTED delta, not merely
  zero motion. C-before-A is deterministic by SOURCE ORDER (camera harness has no `pairs()`).

- AC7 NON-VACUITY: mirror AC3/AC4 paired assertions — record the fixture's competing writes
  RAN (dde248: mid cam-write == fixture-MID and `bus.humanoid:Move(fixture-MID)` recorded;
  cold3a59: cam-write happened + ≥1 PivotTo) THEN assert C's POST wins. cold3a59 reuses ONLY
  the camera-bracket actor, NOT the Humanoid:Move actor (the shape makes no Humanoid:Move).

- §4: dropped "MAY run in parallel"; disjoint file ownership but HARD build-order 1.1 → 1.2.

- cold3a59 MINORs: dropped the brittle negative ":Move( absent" guard (positives — PivotTo +
  cam-write + GetMouseDelta-in-Update — are load-bearing); recorded that cold3a59's fidelity to
  the lost original is UNVERIFIABLE (output/ git-ignored, artifact gone; AC6 guards post-hoc
  edits only), and only the GENERIC PivotTo+cam-write-is-C-dominated shape is load-bearing.

## D10 — dependent slice 1.2 branches from 1.1's converged tip, not phaseBaseSha (Mechanical)
Slice 1.2's corpus test (test_player_shape_corpus.py, AC7) IMPORTS slice 1.1's harness extension
(the bus.cam/bus.humanoid surface + extra_mock_setup/_attrWrites), and the phase design pins a HARD
1.1->1.2 sequence (not parallel). Branching 1.2 from bare phaseBaseSha (2cbed06) would lack 1.1's
harness changes -> 1.2's slice-local tests couldn't run green. So 1.2's worktree branches from the
slice/.../1.1 converged tip. At assembly, merging 1.1 then 1.2 is clean (1.2 descends from 1.1).
This is the correct flow for a hard-sequential dep whose later slice consumes the earlier's code.

## D11 — phase-1 integration review reconciliation (Mechanical + 2 code P1s to slice 1.2)
Phase-1 dual-voice integration review surfaced 4 findings. CLAUDE (doc-divergence, MAJOR x2): the
design's "1.2 consumes 1.1's bus.* surface / hard 1.1->1.2 sequence" became stale when the AC7 fix
made the corpus test load+execute the REAL fixtures (self-contained mock surface). Resolved by a doc
edit (design-phase1.md §2.5 reconciliation): slices are independent; 1.1's bus.* is tick-internal.
CODEX (2 real code P1s, routed to slice 1.2):
  (1) test_player_shape_corpus.py pytestmark skipif is MODULE-level -> skips the pure-Python AC6 drift
      guards when luau absent (design E6 requires AC6 always-on). Claude misread this as class-scoped;
      verified against the artifact that codex is right. FIX: scope the skip to the AC7 class only.
  (2) cold3a59 split-read contract unpinned: AC6 only checks GetMouseDelta appears in Update; AC7 only
      checks net camera write. A drift where Rotate stops consuming pendingMouse still passes. FIX:
      AC6 assert Rotate consumes self.pendingMouse + does not itself call GetMouseDelta; AC7 prove the
      camera reflects the Update-cached delta (cache consumed, not raw-re-read).

## D12 — ship Phase 1 alone as its own PR; defer phases 2-5 to a fresh /drive (User)
User chose "Ship Phase 1 now as its own PR" at the phase-1 checkpoint. Phase 1 (Gate 0 — the
build-time durability net: host-harness primitives a-d + shape-variance corpus dde248/cold3a59 +
C-dominance proofs) is hardened on featureBranch, full fast suite 2706 passed, no-Any clean,
REQUIRE_PLAYER_BIND still 0 (no product binding yet). This PR ships ONLY that net (test infra; no
runtime/product code; no Studio needed to merge). Phases 2-5 (the self.host.player product authority,
needing cold-Studio e2e on a fresh conversion) become a SEPARATE /drive run, building from the
design refinements D7 (server-owned respawn; teleport non-load-bearing), D8 (prove C dominates with A
ACTIVE, no suppression), D9 (C owns the single E2E read via pure advance()) — which must be propagated
into converter/docs/design/player-embodiment-authority-design.md at the start of that run
(update-design-doc-before-implementation). Carryforwards in followups.md.

---

## Step-1b — self.host.player product authority (phases 2-5) — /drive run generic-converter-step1b-20260607T103041 (2026-06-07 → 2026-06-10)

The §7 generic-converter Step-1 player-embodiment effort: paradigm C (`self.host.player`) becomes the load-bearing player binding, anchored on the deterministic upstream `_HasCharacterController` signal (§3 fault-line: never a fingerprint of non-deterministic AI output), and paradigm A (the match/rewrite locators) is deleted.

- **D7** respawn is SERVER-owned (autogen GameServer CharacterAdded + SpawnLocation); C only does per-player lifecycle resync; `teleport(cf)` is a NON-load-bearing client-request/server-apply helper.
- **D8 strangler-fig** — C lands WITH paradigm A still ACTIVE (proven to DOMINATE by last-writer on both A-hit `dde248` + A-miss `cold3a59` shapes); A deleted LAST (Phase 5). Suppressing A early would expose the raw AI writes A neutralizes.
- **D9** C owns the SINGLE per-frame E2E channel read via the pure `advance()` helper (consume-once ACK).
- **slice 2.6** — neutralized the #182 A-locator fail-closeds (`player_move/look_unbound`) that aborted on a field-aliased camera; C binds on `has_character_controller` regardless.
- **D13** — the GerstnerDisplace cold-conversion fail-close was a PRE-EXISTING out-of-scope dead-module bug (empty subclass of a stubbed dead base); fixed as a SEPARATE prereq PR (ntornow#186, merged) rather than folded into Step-1b.
- **Phase 5** deleted `movement_facet_lowering.py` + the camera_facet PLAYER path (drone/turret strict path kept byte-identical) + the #182 locator residue; re-sourced the kept `player_unresolved` fail-close on the post-transpile `player_controller_paths ∩ emitted` intersection (D-P5-1); adapted the C-dominance corpus tests to drive the NATIVE un-lowered fixture per-fixture (cold3a59=camera, dde248=camera+move+jump) (D-P5-5); excluded the player from camera_facet lowering at the call site (D-P5-2 revised); FLIPPED `REQUIRE_PLAYER_BIND` 0→1 as the LAST act (slice 5.4) after a fresh cold-Studio conversion proved camera+WASD+jump+shoot+respawn all C-owned with A deleted.
- **Cold-Studio verify caught 2 real player bugs** the build-time suite couldn't: `host.player:applyRecoil` advertised by the directive but never implemented (missing-method throw); then radians-vs-degrees (the AI's `applyRecoil(2)` slammed the camera to the 80° clamp). Both fixed (applyRecoil takes DEGREES; SceneCameraInput stays radians as its caller is the deterministic lowering pass).
- Full effort dual-reviewed throughout (design 2 rounds caught 4 codex P1s; phase review caught a vacuous-camera-proof P1; the adversarial codex voice was load-bearing repeatedly). Full fast suite green at the flipped value (2868).

## Run: pr5-generic-canary-20260611T160829 (Phase 1 — turret canary) 2026-06-12

# Decisions — PR5 generic-mode SimpleFPS canary (PLAN stage)

- Phase ordering T → R → D, Net folded into Phase 1 (not a standalone phase). §6 "Net first" + task pins
  T-bullet and Net into the same phase as T; T is the only admitted/enforced relation, R/D are §3a candidates
  with unresolved rules, so the durable foundation lands first.
  Classification: Taste

- T-bullet (autogen `clonePrefabTemplate` nil-parent→workspace) rides inside Phase 1 rather than its own phase
  — required (with T) for the turret to actually fire; small, self-contained, no threading dependency.
  Classification: Mechanical

- The §2 PRE-REWRITE REPLACES the existing post-transpile `child_index_lowering.lower_child_index` output-shape
  matcher; the matcher degrades to the fail-closed surviving-ordinal backstop. Keeping the matcher would
  recreate the rung-2b fragility the enforcement contract forbids.
  Classification: Taste

- R (#2-dropped) and D (#9) sequenced after T because they consume Phase 1's threading + verifier surface and
  each must resolve its own unresolved enforcement rule (§7.2, §7.4) in its own DESIGN stage — not resolved at
  PLAN time.
  Classification: Mechanical

- The obligations blob is a normalization of already-derived parse/topology/analyzer facts threaded into the
  prompt (§5), NOT a new semantic compiler IR (umbrella fences that off).
  Classification: Mechanical

- (design-review r1, codex BLOCKING) Relation #1 (turret OnTriggerStay) durable replacement DEFERRED from this
  run. #1 is already satisfied for the corpus by the existing comment-keyed `trigger_stay_lowering` (turret
  fires once Slice T fixes the crash; symptom catalog reproduces no trigger-stay failure). Task explicitly
  scoped first slice to #2 + T-bullet. Durable #2+#1 proof (§6 INJECT + verifier, retiring the rung-2b lowering)
  = tracked follow-on. SURFACED at Gate A (absorb-now vs defer is the human's call).
  Classification: User-Challenge
- (design-review r1, codex MAJOR) Backstop verifier framing tightened: fails the phase on ANY surviving ordinal
  / unresolved child-ref path, not only the literal `GetChildren()[<int>]` shape; pre-rewrite defines §7.2
  fallback for collisions/unnamed/past-end. Detailed rules at Phase 1 DESIGN.
  Classification: Mechanical

## Phase 1 DESIGN stage (2026-06-12)

- (Phase 1 DESIGN, OQ1) Single obligations-blob producer: thread RAW parsed_scene/prefab_library/guid_index
  only as far as the CONTRACT layer (`transpile_with_contract`), which calls the new single producer
  `child_ref_resolver.build_child_ref_map(...)` and threads a flat typed `ChildRefMap`
  (`dict[cs_path, dict[ordinal, child_name]]`) into `transpile_scripts` via a new `child_ref_map` kwarg
  (mirrors `serialized_field_refs`). Keeps the transpiler free of Unity-parse-type imports; names the single
  producer NOW (§5.3) without a from-scratch normalizer IR (umbrella-fenced). #1/#5/#6 add fields to the same
  record later. Confirmed against real code: state.parsed_scene/prefab_library/guid_index all set before the
  transpile phase; `csharp_source` is both the AI input and a cache-key field, so a pre-rewrite that mutates it
  re-keys the cache automatically.
  Classification: Taste

- (Phase 1 DESIGN, OQ2) Backstop surviving-ordinal check lives in `contract_verifier.py` as a 4th check
  `child_ordinal_survivor`, added to `FAIL_CLOSED_CHECKS` (promotes via the existing `fail_closed_errors` →
  `ctx.errors` → `success=False`). It's the established fail-closed home, runs only on the generic path, and
  operates on the right artifact (`RbxScript.source`). Detector reuses `child_index_lowering.source_has_child_index`
  (code-position-aware) repurposed read-only fail-loud + a NEW two-line factored-shape detector. Adjacent-to-
  pre-rewrite was rejected (would re-implement the verifier's promotion/stash plumbing).
  Classification: Mechanical

- (Phase 1 DESIGN) Pack disposition: retire the GENERIC-path role ONLY — remove the `lower_child_index` call in
  `transpile_with_contract`; KEEP `child_index_lowering.py` and the two legacy packs (`unity_transform_child_index`,
  `turret_canonical_spatial_child`) intact (legacy-only; generic skips run_packs). The schema §3 "Retires" column
  = generic enforcement-of-record supersedes the fragile mechanism, NOT legacy code deletion (deleting regresses
  legacy, forbidden).
  Classification: Mechanical

- (Phase 1 DESIGN) Pre-rewrite substitutes `transform.Find("<name>")` into the C# (the Unity API the AI already
  transpiles to a Roblox named lookup), not hand-emitted Luau — keeps the pass a pure C# text substitution and
  lets the AI own the C#→Luau mapping.
  Classification: Taste

## Phase 1 DESIGN — design-review revision (2026-06-12)

- (Phase 1 DESIGN, review fix) F/T ownership split + F ∥ T parallelism. Slice F owns ALL of
  `contract_pipeline.py` (incl. the `lower_child_index`-call removal at lines 565-566), `child_ref_resolver.py`
  (both `build_child_ref_map` AND `prerewrite_child_index`), `code_transpiler.py`, `pipeline.py`, and the two
  pinned-test updates (`test_pipeline_transpile_wiring.py` generic kwarg-set + `test_child_index_lowering.py`
  `TestPipelineInvocation` flip). Slice T owns ONLY `contract_verifier.py` (check D) +
  `test_contract_verifier_child_ordinal.py`. The earlier F→T-sequential framing (removal in T) was unsound
  under the frozen-phaseBaseSha slice model (T's slice-local tests wouldn't contain F's removal). Since
  `verify_contract(topology, scripts)` imports none of F's resolver and operates purely on `RbxScript.source`
  (contract_verifier.py:58-83), T's backstop test is self-contained over synthetic RbxScripts → T no longer
  deps F. New graph: `{F ∥ T ∥ T-bullet} → N`.
  Classification: Mechanical

- (Phase 1 DESIGN, review fix) Resolver consumes the FULL-FIDELITY `prefab_library.prefabs`
  (`list[PrefabTemplate]`, walked from `template.root` over `.children`) and `parsed_scenes`
  (`list[ParsedScene]` = `state.all_parsed_scenes`), NOT `prefab_library.by_name` — `by_name` is "last wins if
  collision" (prefab_parser.py:542) and would drop duplicate-named prefabs. Mirrors
  `extract_serialized_field_refs` (serialized_field_extractor.py:111-128). Imports are
  `from core.unity_types import GuidIndex, ParsedScene, PrefabLibrary, PrefabNode, PrefabTemplate, SceneNode`
  (package prefix `core.`, not `converter.core.`; GuidIndex lives in core.unity_types).
  Classification: Mechanical

- (Phase 1 DESIGN, review fix) ChildRefMap canonical key = `str(path.resolve())` on both producer
  (guid_index.resolve() already returns resolved asset_path) and the §1.2 hook lookup, with a raw
  `str(script_path)` fallback (mirrors `_build_serialized_field_context`'s dual lookup,
  code_transpiler.py:744-746). Prevents a silent key-space miss (info.path is rglob-derived, un-resolved) that
  would skip the pre-rewrite and leave the turret unfixed even though the backstop fails loud.
  Classification: Mechanical

- (Phase 1 DESIGN, review fix) Check D is generic-only WITHOUT a new flag: `_run_contract_verifier` →
  `verify_contract` is reached only inside the topology branch, gated `scene_runtime_mode != "legacy"`
  (pipeline.py:4722-4723), so legacy never feeds the verifier. Confirmed against real code (supersedes the
  earlier "gate on a generic flag if needed" open item).
  Classification: Mechanical

- (Phase 1 DESIGN, review fix) AC(f) re-pointed: the A/B/C(+D) gate is testable only via
  `test_contract_corpus.py` + `test_contract_verifier.py` (the tests that actually invoke `verify_contract`).
  The earlier `TestGenericConversionEndToEnd` citation was green-for-the-wrong-reason — that e2e never calls
  `_run_contract_verifier`/`verify_contract`.
  Classification: Mechanical

## Phase 1 DESIGN — round-2 review revision (2026-06-12)

- (BLOCKING 1) Resolver call site threads `self.state.all_parsed_scenes or [self.state.parsed_scene]`, NOT bare
  `all_parsed_scenes`. Confirmed against real code: single-scene parse (pipeline.py:875) leaves
  `all_parsed_scenes` EMPTY by design (pipeline.py:138 docstring); the SimpleFPS turret lives in a single
  scene, so the bare form would miss every scene-hosted script. The fallback is the established pipeline-wide
  idiom (plan_scene_runtime:1017-1022, extract_assets:979, convert_animations:2114). Resolver skips `None`
  scene entries so the pathological `[None]` (all-parse-failed) case is inert, not a crash. Prefab side reads
  `prefab_library.prefabs` independently — unaffected.
  Classification: Mechanical

- (BLOCKING 2 / D5) MERGE Slice F (pre-rewrite/threading) + Slice T (backstop check D) + the SimpleFPS
  corpus-fixture regen into ONE atomic Slice FT. Corpus investigation (real code): `test_contract_corpus.py`
  loads `fixture.json`'s captured Luau `source` directly into `RbxScript` and drives the REAL `verify_contract`
  (no re-transpile) — so check D fires on the frozen fixture `source`, and the committed SimpleFPS Turret/Player
  entries carry surviving `GetChildren()[1]`. Adding check D + flipping `FAIL_CLOSED_CHECKS` reds the corpus gate
  on the frozen phaseBaseSha. The fixture is regenerated by `tools/regen_contract_corpus.py`, which runs a REAL
  generic conversion (run_all, line 108) and captures the transpiled `source` — so a clean fixture needs the
  pre-rewrite live, and the regen tool itself refuses to write a fixture with any surviving check-D warning
  (lines 125-142). Pre-rewrite ⟂ check D ⟂ regen are a three-way mutual dependency through the shared committed
  corpus source; not slice-local green if split, and two slices editing the corpus/verifier risk conflict. New
  graph: `{FT ∥ T-bullet} → N`. The earlier F∥T framing missed that the COMMITTED corpus test (not just T's
  synthetic-RbxScript unit test) drives `verify_contract`.
  Classification: Mechanical (forced by the frozen-base slice model + the proven corpus-gate coupling)

## IMPLEMENT-stage divergence (Phase 1, slice FT) — turret chained GetChild + check-D scope
Surfaced when FT met the REAL turret. Turret.cs: tBase=transform.GetChild(0); tWeapon=tBase.GetChild(0);
tOrigin=tWeapon.GetChild(0) (chained property getters). Prefab nesting confirmed: Turret→Base→Weapon→Origin.
Two gaps the 3 design-review rounds missed (they verified the mechanism, not the real turret/multi-phase corpus):
1. Pre-rewrite resolves only hop 1 (transform.GetChild) — hops 2-3 abstain (non-transform receiver) → ordinals survive.
2. Check D fail-closed-on-ANY-ordinal would RED the SimpleFPS corpus on BOTH the turret's chained hops AND
   Player.cs `self.cam:GetChildren()[1]` (weaponSlot=cam.GetChild(0); cam=Camera.main.transform → Phase 2 #2-dropped).
RECOMMENDED RESOLUTION (A): (a) extend the pre-rewrite to resolve the transform-ROOTED chain via local dataflow
(turret Base/Weapon/Origin all → named lookups); (b) scope check D to ASSERT AGAINST THE IR FACT — fail-closed
only on a surviving ordinal the resolver produced a fact for (turret hops guarded); ABSTAIN where no fact
(Player cam ref → Phase 2 coverage gap, tracked not failed). Keeps §2 "loud-check-against-the-fact"; reverses
codex r1's "fail on any ordinal" breadth (which false-positives on Phase 2/3 refs). PENDING user sign-off.

USER SIGN-OFF (Gate, AskUserQuestion): RESOLUTION A chosen — chained/dataflow resolution (transform-rooted
chain → named lookups) + check D ASSERTS THE IR FACT (fail-closed only where a resolved fact exists; abstain
where no fact → Phase 2/3 coverage gaps tracked not failed). Classification: User-Challenge (resolved by user).

### D6 (design revised against real code, post-sign-off) — RESOLUTION A mechanism locked
Verified against the populated test_projects/SimpleFPS (submodule empty in the worktree — read main checkout):
Turret.cs:37-48 chained block-bodied getters tBase=transform.GetChild(0)/tWeapon=tBase.GetChild(0)/
tOrigin=tWeapon.GetChild(0); Turret.prefab Transform graph reconstructed = Turret→{Base→{Weapon→{Origin}},
Collider}; ALL 3 hops resolve to Base/Weapon/Origin. Player.cs:90-91 cam=Camera.main.transform; weaponSlot=
cam.GetChild(0) → foreign receiver → ABSTAIN.
- Chained resolution: per-script Transform symbol table seeded transform↦host, fixpoint over local-var +
  block-bodied + expression-bodied getter defs `<recv>.GetChild(n)`; receiver preserved, only .GetChild(n)→
  .Find("<name>"). Tractable TEXTUALLY (no Roslyn) against the real block-getter syntax — confirmed.
- Fact-based check D: resolver stamps {getchild_total,resolved_total} onto a NEW RbxScript.child_ref_resolution
  field (the robust mechanism — verify_contract + the frozen-fixture corpus replay see ONLY RbxScript, so the
  fact must ride on it; keyed on the deterministic resolver tally, NOT a fragile C#-symbol→Luau-name match).
  fully_resolved := resolved_total==getchild_total>0. Fail-closed child_ordinal_survivor (warning) only in a
  fully-resolved script (regression); abstain child_ordinal_coverage_gap (info, never promoted) otherwise.
  Reverses codex-r1's "fail on ANY ordinal" breadth (false-positives on the shared corpus's Phase-2 cam ref).
- Corpus stays GREEN after FT regen: Turret = named lookups + {3,3}; Player cam ordinal survives but UNFLAGGED
  via {1,0}. Regen write-refusal keyed on `warning` only (fail_closed_errors filters severity), so the Player
  info gap does not block the write.
- Coverage limitation (accepted): a mixed resolved+unresolved script → check D abstains for the whole script
  (loses backstop on the resolved site, already construction-safe). Corpus doesn't hit it.
- Slice graph UNCHANGED {FT ∥ T-bullet}→N; FT now also owns core/roblox_types.py (the new field).
  Supersedes the "fail on ANY surviving ordinal" MAJOR above. Classification: User-Challenge.
