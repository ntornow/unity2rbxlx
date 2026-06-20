
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


## Run rifle-dropped-ref-20260612T064620 (2026-06-14/15) — Rifle #2-dropped weaponSlot rebind (Path A + r3 receiver-anchored check-D exemption)

## PLAN-stage decisions — #2-dropped (rifle weaponSlot)

- D1 [Taste] — §7.2 rebind rule: a dropped/abstained child-ref gets a deterministic by-name rebind iff its
  receiver resolves to a UNIQUE parsed node via one upstream signal. Phase-1 covers transform-rooted; this slice
  adds exactly one rooting source: Camera.main.transform → the unique MainCamera-tagged node. Generic, no hardcoded names.
- D2 [Taste] — §2 mode: PRE-REWRITE-to-resolved (cam.GetChild(0)→cam.Find("WeaponSlot") via existing
  prerewrite_child_index) PLUS a FAIL-CLOSED binding-present verifier (check-D sibling; asserts the IR-declared
  binding's named lookup present in output; promoted via FAIL_CLOSED_CHECKS). Pre-rewrite = durable-by-construction
  primary; verifier = loud-on-dropped floor.
- D3 [Mechanical] — admission: ONE signal = MainCamera tag (unique, parse-time, already used at scene_converter
  _MainCameraRig); ONE retired mechanism = AI's non-deterministic dropped/ordinal handling of the foreign receiver;
  ONE real check = binding-present verifier. #2-dropped MEETS the umbrella admission criteria, modulo OQ1
  (inject-vs-verify boundary) and OQ2 (receiver-rooting scope).

---

## PLAN-stage decisions v2 — SUPERSEDES v1 above (re-grounded after dual-voice design review)

Both voices, verified against the real parse + on-`main` corpus fixture + PR#190 code, found the same three
spine flaws in v1: wrong runtime target (receiver-preserving rewrite → nil); mis-shaped (not dropped) symptom;
no separate verifier warranted (check D is the floor). v1's D1/D2/D3 are RETIRED; these replace them.

- D1-v2 [Taste] — §7.2 rule = RETARGET (not preserve). A child-ref gets a deterministic rebind iff its receiver
  resolves to a UNIQUE parsed node via one upstream signal. This slice adds exactly one rooting source:
  Camera.main.transform → the unique MainCamera-tagged node, AND retargets the receiver — the rewrite REPLACES
  the whole expression with a `_MainCameraRig`-rooted lookup, because `WeaponSlot` lives under the converted rig
  Model at runtime, not under `workspace.CurrentCamera` (what the preserved `cam` transpiles to, api_mappings:248).
  Multi-hop: cam → Camera.main.transform → MainCamera node → child[n] name → rig-rooted runtime lookup. Generic.
- D2-v2 [Taste] — §2 mode = PRE-REWRITE only, NO verifier. Emit the resolved rig-rooted Luau directly (§2 "emits
  the resolved Luau directly"), keyed on the deterministic _MainCameraRig/MainCamera-tag fact, not on AI output.
  v1's binding-present verifier DROPPED: reference facts get no verifier (§4); no binding-IR producer exists
  (§5.2); a present-named-lookup check = pack-in-disguise. Check D is the PRE-EXISTING regression floor —
  resolving the fact flips resolved_total 0→1 → budget 0 → any surviving ordinal fails closed, no new check.
- D3-v2 [Mechanical] — admission, corrected retired mechanism. ONE signal = MainCamera tag (stamps _MainCameraRig,
  mode-independent). ONE retired mechanism = the legacy fps_rifle_pickup/WeaponMount pack
  (script_coherence_packs.py:194/290/547) doing _MainCameraRig→FindFirstChild("WeaponSlot"), DISABLED in generic
  (pipeline.py:2982) — so generic has NO rifle repair today (the real gap). ONE mechanism = the child_ref_resolver
  RETARGET emit + check D (pre-existing) as floor. #2-mis-shaped (RETARGET variant) MEETS admission.

Investigation result: _MainCameraRig IS present + referenceable in generic mode (stamped scene_converter.py
2080/5126 on the MainCamera tag, mode-independent; CameraRigFollower injected pipeline.py:3486, mode-independent;
generic skips only run_packs at 2982). No STOP-worthy gap. OQ1 (retarget code home) + OQ2 (scope) remain open
close-calls for Gate A.

---

## PLAN-stage decisions v3 — SUPERSEDES v2 above (re-grounded after dual-voice design review round 2)

Both voices verified v2's TARGET solid (_MainCameraRig is a mode-independent attribute; WeaponSlot lands under
the rig; Camera.main abstains {1,0}; check D is the floor; D3 retired-mechanism = legacy WeaponMount pack). Both
found the SAME two EMIT flaws. v2's D1/D2 are RETIRED; these replace them. D3 carries forward unchanged.

- D1-v3 [Taste] — §7.2 rule = RETARGET emitted POST-transpile. A child-ref gets a deterministic rebind iff its
  receiver resolves to a UNIQUE parsed node via one upstream signal. This slice adds one rooting source:
  Camera.main.transform → the unique MainCamera-tagged node, AND retargets the receiver — but emitted as a
  POST-transpile lowering, not the v2 pre-rewrite. Multi-hop: cam → Camera.main.transform → MainCamera node →
  child[n] name (E1–E3) → field name (assignment LHS = weaponSlot) → post-transpile lowering replaces the RHS at
  that field's assignment with a _MainCameraRig-rooted lookup (WeaponSlot lives under the rig Model at runtime,
  not under workspace.CurrentCamera, api_mappings:248). Generic, no hardcoded names.

- D2-v3 [Taste] — §2 mode = FACT-KEYED POST-TRANSPILE LOWERING, NO verifier. CORRECTS v2's FIX 1: v2 emitted the
  rig lookup via prerewrite_child_index, which mutates csharp_source PRE-AI (code_transpiler.py:256) so the AI
  re-processes the injected Luau → construction-safety defeated. v3 emits in a NEW post-transpile pass
  (rifle_rig_retarget_lowering) wired into transpile_with_contract alongside lower_camera_facet (:564) /
  lower_trigger_stay (:599), operating on TranspiledScript.luau_source AFTER transpile_scripts returns — the same
  site where the retired lower_child_index ran (contract_pipeline.py:579-587) and where the legacy WeaponMount
  pack proved the lookup (script_coherence_packs.py:547-551). Construction-safe because the AI's RHS is DISCARDED
  + REPLACED deterministically post-AI (distinct from the turret's PRE-AI receiver-preserving pre-rewrite). The
  lowering FACT-ANCHORS on the FIELD NAME from the resolver fact (self.weaponSlot = <rhs>), NOT the ordinal
  output shape (not a pack-in-disguise). CORRECTS v2's FIX 2: the emitted RHS is a RACE-SAFE lazy getter
  (__index proxy that re-resolves+memoizes on access), NOT a one-shot Awake scan — _MainCameraRig streams in late
  (CameraRigFollower retries 30×0.1s, autogen.py:539; the pack looked up at equip-time, script_coherence_packs:547),
  so a one-shot scan leaves self.weaponSlot permanently nil on slow/streaming clients. NO binding-present verifier
  (reference facts get none, §4; no binding-IR producer, §5.2; present-named-lookup check = pack-in-disguise).
  Check D is the pre-existing SHAPE regression floor (resolving the fact flips resolved_total 0→1 → budget 0 →
  any surviving ordinal fails closed); it is a shape floor, not a rig-lookup-correctness witness — correctness
  rests on the deterministic post-AI emit (which FIX 1 now guarantees).

- D3 [Mechanical] — admission, UNCHANGED from v2. ONE signal = MainCamera tag (stamps _MainCameraRig,
  mode-independent). ONE retired mechanism = legacy fps_rifle_pickup/WeaponMount pack
  (script_coherence_packs.py:194/290/547), DISABLED in generic (pipeline.py:2982) — generic has NO rifle repair
  today (the real gap). ONE mechanism = child_ref_resolver rig-rooted fact + post-transpile retarget lowering +
  check D (pre-existing) as floor. #2-mis-shaped (RETARGET variant) MEETS admission.

OQ1 (v2 retarget-code-home) is RESOLVED: post-transpile lowering, fact-anchored on the field name. The remaining
open close-calls for Gate A: OQ1-v3 (emitted lazy shape — __index proxy vs method-getter+consumer-rewrite;
recommend proxy) and OQ2 (receiver-rooting scope — Camera.main only; recommend defer broader). Both non-blocking.

---

## PLAN-stage decisions v4 — SUPERSEDES v3 above (re-grounded after dual-voice design review round 3)

Both voices verified v3's TARGET + SITE + scope solid across three rounds (_MainCameraRig mode-independent
attribute; WeaponSlot under the rig; post-transpile lowering site in transpile_with_contract composes with no
ordering hazard; Camera.main abstains additively; check D is the floor; D3). Both found the SAME two EMIT flaws
in v3. v3's D1/D2 are RETIRED; these replace them. D3 carries forward unchanged.

- D1-v4 [Taste] — §7.2 rule = RETARGET emitted POST-transpile, binding a REAL Instance. Carries v3's rule (one
  rooting source Camera.main.transform → MainCamera-tagged node; receiver RETARGET emitted as a post-transpile
  lowering; multi-hop cam → Camera.main.transform → MainCamera node → child[n] name (E1–E3) → field name (LHS) →
  post-transpile RHS replace). Refines the emit: the replacement RHS calls an injected REAL-Instance resolver
  (not a proxy). Generic, no hardcoded names.

- D2-v4 [Taste] — §2 mode = FACT-KEYED POST-TRANSPILE LOWERING, REAL-INSTANCE bounded-retry emit, NO verifier.
  CORRECTS v3's FIX 1: v3 bound self.weaponSlot to a setmetatable({}, {__index=...}) PROXY TABLE — DROPPED, because
  weaponSlot flows into rifle.Parent = self.weaponSlot (SetParent, api_mappings.py:54), the host instantiatePrefab
  nil-guard clone.Parent = parent or workspace (scene_runtime.luau:2025), and weaponSlot:GetPivot(); a table is
  truthy-not-Instance → .Parent = <table> raises "Instance expected, got table", the nil-guard is defeated, and
  GetPivot gets the proxy as self. v4 ports the legacy pack's PROVEN real-Instance lookup (local slot = rig and
  rig:FindFirstChild("WeaponSlot", true), script_coherence_packs.py:551) wrapped in CameraRigFollower's bounded
  retry (for _ = 1, 30 do task.wait(0.1), autogen.py:539-547) + memoized, emitted as a resolver helper; the field
  is rebound to self.weaponSlot = _resolveWeaponSlot() (a real Instance or nil — both valid for .Parent / nil-guard
  / GetPivot). Yield context: the retry yields, so the resolve runs where the script can yield — the pack resolved
  at equip-time in GetRifle() (script_coherence_packs.py:551); per-phase DESIGN reads the REAL transpiled
  assignment/consumer sites and picks the yield-safe binding point. CORRECTS v3's FIX 2: REMOVE the over-strong
  "no path by which a wrong-but-non-ordinal lookup is emitted" claim. The fact-anchor is field-name-only; on an
  unexpected AI LHS shape the lowering abstains (safe); check D catches surviving ordinals (adjacent
  _GETCHILDREN_INDEX_ANY_RE :567 AND two-line factored :580) but a non-ordinal non-field drift is a rare,
  safe-direction SILENT miss (rifle on wrong body, no crash) — LOGGED honestly, not closed (no binding-present
  verifier: §4 forbids on reference facts; no binding-IR producer, §5.2; present-named-lookup = pack-in-disguise).
  Check D is the pre-existing SHAPE regression floor (resolved_total 0→1 → budget 0).

- D3 [Mechanical] — admission, UNCHANGED from v2/v3. ONE signal = MainCamera tag (stamps _MainCameraRig,
  mode-independent). ONE retired mechanism = legacy fps_rifle_pickup/WeaponMount pack
  (script_coherence_packs.py:194/290/547), DISABLED in generic (pipeline.py:2982) — generic has NO rifle repair
  today. ONE mechanism = child_ref_resolver rig-rooted fact + post-transpile retarget lowering + check D as floor.
  #2-mis-shaped (RETARGET variant) MEETS admission.

OQ1-v3 (emitted lazy shape) is RESOLVED: REAL rig-child Instance via bounded retry — the proxy is eliminated by a
hard correctness constraint (SetParent/.Parent/:GetPivot require an Instance), not a taste call. The only Gate-A
open question left is OQ2 (receiver-rooting scope — Camera.main only; recommend defer broader). The yield-site
binding point is a per-phase DESIGN detail (read the real transpiled consumer sites), not a Gate-A open question.

## GATE A APPROVED (2026-06-12)
Design converged (4 dual-voice rounds). Scope: Camera.main→MainCamera-tag ONLY (user did not override the
recommended narrow scope). Proceeding to implementation.

---

## DESIGN-stage decisions — Phase 1 (per-phase detailed design, against the real PR#190 surface)

Authored in DESIGN against the worktree tip + the corpus fixture. Settles the interface shapes, the
yield-safe bind site, and the slice breakdown for the single Phase-1. Full design: design-phase1.md.

- D-P1-a [Taste] — Rig-rooted retarget fact = a SIBLING frozen dataclass
  `RigRootedRetargetFact(field_name, child_name)`, NOT a `ChildRefFact` variant. ChildRefFact is
  receiver-PRESERVING + consumed PRE-AI by prerewrite_child_index; the rig fact is receiver-DISCARDING +
  consumed POST-AI by the lowering. Separate type keeps prerewrite_child_index from ever touching it (the
  construction-safety boundary v2 violated). `ChildRefScript` gains `rig_facts: tuple[...] = ()`;
  `resolved_total` counts host + rig facts so check D's budget flips Player {1,0}->{1,1}.

- D-P1-b [Taste] — Yield-safe bind site = the §2.1 `task.spawn` back-fill resolver, anchored on the Awake
  assignment. FORCED (not a toss-up): the AI placed `self.weaponSlot =` in Player:Awake() (fixture line 89);
  Awake MUST NOT yield (scene_runtime.luau:2066 `start() never yields`); consumers are equip-time (GetRifle
  :279/:282). The injected resolver does a NON-yielding first sweep at Awake and, on a miss, arms a
  task.spawn'd 30x0.1s retry that BACK-FILLS self.weaponSlot when the rig streams in. Honors BOTH the runtime
  invariant (Awake thread never yields) AND the SHARED CAVEAT (never "nil forever"; the back-fill IS the
  late-bind, not a one-shot trust-CameraRigFollower sweep). One shape, correct in either scope placement.
  The emitted binding is a REAL rig-child Instance (rig:FindFirstChild("WeaponSlot", true)) or nil — never a
  proxy table (FIX 1 honored: .Parent / instantiatePrefab nil-guard / pivotOf all type-safe).

- D-P1-c [Mechanical] — check D reused unchanged; fact-anchor residual logged not closed. No new verifier
  (§4 reference-fact boundary). Resolver flip + RHS replacement make check D green by construction. The
  non-ordinal-drift silent miss on the lowering's abstain path (edge 4b) is a rare safe-direction residual,
  logged in followups (closing needs a binding-IR producer, §5.2).

- D-P1-d [Mechanical] — Scope: Camera.main->MainCamera-tag ONLY (Gate A CONFIRMED, narrow scope not
  overridden). Mechanism keys solely on the MainCamera tag + parsed child name + C# field name — generic,
  no rifle/weaponSlot hardcode.

Slice breakdown (3, sequential S1->S2->S3): S1 RESOLVER+LOWERING (resolver admit + new
rifle_rig_retarget_lowering.py + wiring — one fact-lifecycle); S2 FIXTURE-REGEN + check-D greenness (atomic,
corpus gate couples lowering output to check D); S3 NET/E2E witness + residual followup. No parallel seam —
one producer->consumer->fixture->witness chain.

---

## DESIGN-stage decisions v2 — SUPERSEDES D-P1-b above (re-grounded after dual-voice phasedesign review)

Both voices (Claude + codex) BLOCKING-agree the v1 `task.spawn` back-fill resolver is a correctness
REGRESSION, not a fix: (1) it LOSES the equip race — the back-fill is a decoupled snapshot lagging the rig's
arrival; a slow-stream client equips before the next poll, GetRifle sees self.weaponSlot==nil, the rifle is
mis-placed for that one-shot equip, and the later back-fill cannot repair the already-spawned rifle; (2) the
module-level _rigSlotPending + single-captured-self back-fill BREAKS multiplayer — a 2nd host instance is
suppressed (pending==true) and back-filled into the first captured self, leaving instance-2 nil forever. The
legacy WeaponMount pack proves the correct shape: resolve INLINE at the consumer (script_coherence_packs.py
:547-562, immediately before PivotTo/Parent). Everything else in v1 stays CONFIRMED SOUND (sibling dataclass,
resolved_total {1,0}->{1,1} budget flip, check D unchanged, the lowering hook, strict typing, generic
projection, S1->S2->S3 acyclic + S2 atomic). D-P1-b is RETIRED; this replaces it. D-P1-a/c/d carry forward.

- D-P1-b-v2 [Classification: Taste] — Bind site = CONSUMER-SIDE lazy resolve, per-instance, mirroring the
  legacy WeaponMount pack. RETIRES the v1 task.spawn back-fill. The post-transpile lowering injects a
  per-instance memoized resolver METHOD (function <Class>:_resolve<ChildName>()) that does the rig lookup
  (scan workspace:GetDescendants() for the _MainCameraRig Model, then rig:FindFirstChild("<child>", true) — a
  REAL Instance) with the bounded 30x0.1s retry, and REWRITES the consumer READS of self.<field> to call it
  (self.<field> -> self:_resolve<ChildName>(), memoized on self._<field>Cache). The AI's Awake assignment RHS
  is neutralized to nil. Fact-anchored on the deterministic field_name (the read sites), abstain-safe on an
  unmatched AI shape (residual logged, not closed). RACE-SAFE AT USE: resolve+consume in one synchronous span
  in the yielding GetRifle (real fixture reads at :279/:282); the bounded retry waits for a late rig RIGHT
  THERE. PER-INSTANCE: cache on self, NO module-level _rigSlotPending/_rigSlotCache, NO cross-instance
  suppression -> multiplayer-correct. REAL Instance or nil (never a proxy): .Parent / instantiatePrefab
  nil-guard / pivotOf:GetPivot all type-safe. ADDS the crux acceptance test (codex MAJOR): a DETERMINISTIC
  RACE test forcing Awake-miss -> rig appears -> equip -> assert rifle on the rig's WeaponSlot (the schedule
  the back-fill failed and lazy-at-use passes by construction). Grounded: legacy pack :547-562; real fixture
  Player Awake write :89 (non-yielding), GetRifle reads :279/:282 (yielding); pivotOf :13 expects a real
  Instance. Collapses the v1 §1.3 yielding-Awake-helper and §2 dead-end alternatives -> ONE canonical emit.

## ARCHITECTURE PIVOT (user decision, 2026-06-12) — post-transpile lowering → LOAD-BEARING TOKEN
S1's implementation review (codex 3 BLOCKING + 2 MAJOR) confirmed the converged design's POST-TRANSPILE
STRING-REWRITE of AI Luau output is the rung-2b AI-output-fingerprint fragility the generic effort exists to
retire (every review round found another AI-output shape it mishandles). User chose the durable §2 LOAD-BEARING
TOKEN mode: PRE-REWRITE the C# (deterministic, before the AI) so weaponSlot reads become a host helper call
(e.g. host:rigChild("WeaponSlot")) the AI passes through verbatim, backed by ONE generic runtime helper that
resolves _MainCameraRig→FindFirstChild(name,true) (real Instance, retry) at the yielding use site. Moves all
string-manipulation to the deterministic C# INPUT (where the turret fix succeeded). Construction-safe; fails loud
if the token survives unexpanded. SUPERSEDES the post-transpile lowering decisions (D2 of design.md v4 +
design-phase1 consumer-side-lowering). Goal + target (_MainCameraRig WeaponSlot) UNCHANGED; only the §2 mechanism changes.
Classification: User-Challenge (resolved by user).

## PATH CORRECTION (user decision after codex token-vet, 2026-06-12)
Token approach ABANDONED (codex: not more durable — relocates AI-trust fragility C#-ward; no precedent for
trusting the AI to preserve a synthetic host call). REINSTATE the deterministic post-transpile lowering (the
right mechanism — the AI-not-trusted, deterministically-lowered OnTriggerStay precedent) + FIX S1's 6 impl bugs +
ADD a binding-present FAIL-CLOSED VERIFIER (schema §7.2/§3a "deterministic rebind-by-name + a verifier that the
IR-declared binding is present"). §4-boundary reconciliation: a DROPPED/foreign-receiver retarget DOES warrant a
verifier (unlike a host-rooted reference fact that's construction-safe via pre-rewrite) BECAUSE the lowering rides
on AI output the AI isn't trusted to preserve — the verifier makes a dropped/reshaped binding fail LOUD instead of
silently shipping a wrong rifle. This SUPERSEDES the design's rounds-2/3 "no verifier" decision.
Classification: User-Challenge (resolved by user + codex).

---

## DESIGN-stage decisions v3 — rewrites design-phase1.md for the PATH CORRECTION (CHANGE 1 + CHANGE 2)

Re-grounded against the worktree tip (contract_verifier.py check D + FAIL_CLOSED_CHECKS; trigger_stay_lowering.py;
child_ref_resolver.py; the child_ref_resolution carrier thread TranspiledScript:66 → pipeline:2903 →
RbxScript:152 → check D). The rounds-2/3 "no verifier" DESIGN decisions (D-P1-c v1, design-phase1 §1.5/§4) are
RETIRED; D-P1-a/b carry forward unchanged. Two changes land in design-phase1.md.

- D-P1-c-v2 [User-Challenge, resolved by user + codex] — CHANGE 1: ADD a binding-present FAIL-CLOSED VERIFIER
  `_check_rig_binding_present` in contract_verifier.py, added to FAIL_CLOSED_CHECKS. Asserts the IR-declared rig
  binding is DISCHARGED by reading a NEW `rig_binding` carrier (`{field, child, present}`) — the resolver's
  deterministic field/child projections + the lowering's `present` discharge truth — NOT a Luau-string grep (that
  would be a pack-in-disguise; mirrors how check D asserts against child_ref_resolution). Carrier thread:
  resolver-fact → lowering stamps TranspiledScript.rig_binding → pipeline copies to RbxScript.rig_binding →
  verifier (parallel to child_ref_resolution). §4-boundary reconciliation: a host-rooted reference fact is
  construction-safe via pre-rewrite (no verifier); the rifle's DROPPED/foreign-receiver retarget CANNOT be
  pre-rewritten construction-safe (Camera.main.transform points at the LIVE camera, not the rig Model holding
  WeaponSlot), so the lowering rides untrusted post-AI output → the lowering+verifier is the FLOOR. A
  dropped/reshaped/abstained binding (present=False) fail-closes LOUD. Closes the rounds-2/3 non-ordinal-drift
  silent-miss residual for the dropped/reshaped case. SUPERSEDES the rounds-2/3 no-verifier decision.

- D-P1-c-v2.coupling [Mechanical] — the neutralize/resolved_total DESYNC (codex-review-1.1 BLOCKING #3) is closed
  by the carrier, NOT check D: resolved_total flips 0→1 pre-transpile, ordinal-neutralization happens
  post-transpile in the lowering; a half-applied lowering would spuriously fire check D's child_ordinal_survivor.
  The binding-present verifier (not check D) is the authority on "discharged" — the lowering stamps present=False
  when it cannot anchor; the neutralize + discharge-stamp move TOGETHER, so a half-applied lowering can never
  present as silently-green and check D never misattributes.

- D-P1-d [Mechanical] — CHANGE 2: the 6 S1 impl bugs (codex-review-1.1) are now explicit interface/acceptance
  requirements. (1) inject resolver BEFORE `return <Class>` + post-inject Luau syntax re-check (abstain on fail) —
  never unloadable Luau. (2) Camera.main admission EXACT host-XOR-rig: direct Camera.main.transform.GetChild(0)
  admitted with no cam-symbol seed; enemy.cam / longer foreign chains / member-access LHS REJECTED (no endswith,
  no recv_tail-in-cam_symbols substring). (3) _neutralize_assignment fact-anchored to the camera-child write
  (field + camera-child RHS shape), multiline-aware, abstain-safe, resolved_total-coupled. (4) _rewrite_field_reads
  receiver/scope-aware (bare self.<field> only; not x.self.field, not shadowed self). (5) yield-context guard —
  rewrite reads ONLY in yielding methods (resolver has task.wait); abstain on non-yielding Awake/Start; yield-safety
  detected structurally. (6) idempotency guard = the injected method's actual presence, not `marker in source`.

- D-P1-f [Mechanical] — SLICES re-cut to 4: S1 resolver+lowering+carrier+6-robustness; S1b binding-present
  verifier (its OWN slice — distinct fail-closed-check concern with its own pre-fix-RED regression proof, deps S1's
  carrier); S2 fixture-regen (captures rig_binding, must pass the now-flipped gate); S3 net+race+
  dropped-binding-fails-loud(E2E)+residual. Strictly sequential S1→S1b→S2→S3 (producer→consumer→verifier→fixture→
  witness). New acceptance items: (e) verifier RED-on-absent/GREEN-on-present + pre-fix-RED proof + fact-anchored-
  not-grep; (h) the 6 robustness items incl. the desync-coupling test (h.3) and the yield-guard test (h.5) and the
  unloadable-Luau syntax-check (h.1).

## PAUSED at design→implement boundary (user, 2026-06-12) — API at-capacity
Design CONVERGED (4 plan rounds + ~6 phase-design rounds incl. the token-vet detour + 2 verifier-independence
corrections). Nothing implemented (S1's first attempt discarded at the path-pivot). Resume by re-running /drive
on runId rifle-dropped-ref-20260612T064620 when the API is freer. The phase design is converged + fully specified;
the next action is: freeze phaseBaseSha, dispatch S1 (resolver+lowering+carrier) per design-phase1.md §5.

---

## S1 IMPLEMENTATION notes (2026-06-12) — slice rifle-dropped-ref/1.1

- **D-S1-impl-a [Mechanical] — path-notation drift, no contract change.** The slice
  prompt's owned-file paths used a `converter/converter/...` prefix; the real tree is
  `converter/...` (cwd=`converter/`, package on sys.path). All seams the design cited
  matched the real code (`child_ref_resolver._resolve_script`/`build_child_ref_map`;
  `code_transpiler.TranspiledScript`; `contract_pipeline.py:599` lowering zone with
  `child_ref_map` in scope; `pipeline.py:2903` copy + the three child_ref_resolution
  rehydration seams SAVE/LOAD/RESTORE; `core/roblox_types.RbxScript:152`). No redesign.

- **D-S1-impl-b [Mechanical] — Luau loadability uses the project's real checker.** The
  post-inject syntax re-check (`_inject_resolver_method`) calls
  `utils.luau_analyze.syntax_errors_for_source` (the same `luau-analyze` the transpiler
  runs) when the binary is installed, falling back to a structural bracket/`function`-
  after-`return` balance proxy when it is not — so the after-`return` BLOCKING is caught
  in both environments (luau-analyze IS installed here; verified it reds the after-return
  splice). Acceptance c-i / h.1 tests skip the analyze assertion when the binary is absent.

- **D-S1-impl-c [Mechanical] — discharge re-derived from the FINAL committed source on
  EVERY call (incl. idempotent no-ops).** `lower_rifle_rig_retarget` stamps
  `rig_binding.present` from `_binding_discharged(final_source, field, child)` AFTER the
  edit attempt regardless of whether THIS call injected — so an idempotent second call
  re-stamps `present=True` identically (the method is already present), and a reverted
  inject never leaves `present=True`. `_has_surviving_field_read` excludes reads inside
  the non-yielding lifecycle methods (`Awake`/`Start`) — the yield-guard intentionally
  leaves those (they read the neutralized `nil` safely), so an abstained lifecycle read
  does NOT count against discharge. `_statement_rhs_end` is continuation-aware (spans a
  multiline `x and\n  y` RHS) for the multiline-neutralize requirement.

---

## S1 REVIEW-ROUND-2 fixes (2026-06-12) — slice rifle-dropped-ref/1.1 (5 P1s)

Addressed all 5 P1s from `codex-review-1.1.md` + `review-1.1-2.md` (union). Round-2 commit
on the slice branch.

- **D-S1-r2-a [Mechanical] — P1: final Luau syntax re-check AFTER all rewrites.** Moved the
  load-bearing syntax gate to the FINAL source (post inject + read-rewrite + neutralize), not
  just post-inject. `_binding_discharged` is shape-only, so a neutralize that corrupts the
  module (the design's `if self.cam then self.weaponSlot = ... end` — `_statement_rhs_end`
  swallows the inline `end` into the RHS span, dropping the `if`'s closer) would stamp
  present=True on unloadable Luau. The final gate reverts to original + stamps present=False.
  Test `test_p1_final_syntax_check_after_rewrites_stamps_false` PROVES post-inject parses but
  the FINAL source does not (so only the final gate catches it). NOTE: the `_statement_rhs_end`
  end-keyword-swallow is a latent RHS-span bug; the syntax gate makes it fail-CLOSED (abstain),
  which is correct — tightening the span to stop at a bare `end`/`then` keyword is a non-blocking
  followup (logged), not required for fail-closed correctness.

- **D-S1-r2-b [Mechanical] — P2: seed anchored to the GetChild use site.** Codex's repro
  REPRODUCED (verified: `cam=enemy.transform; weaponSlot=cam.GetChild(0); ...; cam=Camera.main.transform`
  admitted a rig fact pre-fix). `_canonical_receiver(source, recv, use_pos)` now resolves the
  symbol to its NEAREST PRECEDING binding strictly before the GetChild position (the binding live
  at the use site by scope/order), admitting iff that nearest binding is exactly
  `Camera.main.transform`. A later/unrelated `cam = Camera.main.transform` after the GetChild, or
  a rebind to a foreign receiver before it, no longer back-admits. Tests `test_p2_*`.

- **D-S1-r2-c [Mechanical] — P3: shadowed-`self` guard (h.4 half).** New `_self_is_shadowed_at`
  walks the lexical block structure from the enclosing `function <Class>:<method>(` to the read,
  tracking block depth (`function`/`do`/`then`/`repeat`...`end`/`until`); a `local self` or a
  `function(...self...)` parameter introduces a shadow at its depth that suppresses the rewrite
  while that depth is open. Wired into BOTH `_rewrite_field_reads` (abstain) and
  `_has_surviving_field_read` (a shadowed read is foreign, not a surviving consumer — so a mangle
  can never report discharged=True via a shadowed read). Tests `test_p3_*`.

- **D-S1-r2-d [Mechanical] — P4: MainCamera uniqueness scoped to the host's owning scene/prefab.**
  `_main_camera_node` previously enforced GLOBAL uniqueness (an unrelated MainCamera in another
  prefab/scene → >1 → silent drop). New `_owning_node_collection` locates the host's scene/prefab
  by object identity and scopes the uniqueness check to THAT collection only (consistent with how
  `_node_by_cs_path` scopes). If the host can't be located in any parsed collection, abstain (no
  scope to assert uniqueness) — no silent global drop. In-scope non-uniqueness still abstains
  (`test_p4_non_unique_within_owning_prefab_still_abstains`). Tests `test_p4_*`.

- **D-S1-r2-e [Taste] — P5: >1 rig fact per script FAILS CLOSED (single-dict carrier kept).**
  Per the 6 Decision Principles (minimal change, fail-closed, matches the design's §1.5 single-dict
  contract + one-fact-per-script assumption): a script bearing >1 rig fact does NOT silently keep
  the last fact's carrier. The lowering ABSTAINS on all edits and stamps a single overflow carrier
  `{field, child, present:False, multi_fact:True}`, so the binding-present verifier (S1b) fails
  LOUD on the unverifiable multi-binding script rather than shipping a partial discharge. Chose
  this over re-architecting to a list (out of envelope — the corpus + design frame multi-fact as a
  two-SCRIPTS case; a list carrier touches the verifier/regen/rehydration seams owned by S1b/S2).
  Documented assumption: ONE rig fact per script (the corpus shape); a genuinely multi-binding
  single script is fail-closed, not silently dropped. Test `test_p5_multi_rig_fact_per_script_fails_closed`.

## Slice S1 — round-3 codex review fixes (unifying root cause: FAIL CLOSED on uncertainty)

All four round-3 P1s shared one root: a decision point TRUSTED its own rewrite/admission instead of
abstaining/stamping `present=False` on a shape it could not handle with certainty. Each fix biases to
ABSTAIN (conservative — handles fewer shapes but never wrong); the corpus happy-path still works.

- **D-S1-r3-a [Mechanical] — P1: analyzer-absent fallback validates block-keyword balance.**
  `_structural_balance_ok` (the `_luau_syntax_ok` fallback when `luau-analyze` is absent) previously
  validated only bracket balance + the after-`return` splice, NOT `if/then/end`. A single-line-`if`
  neutralize whose RHS span swallows the closing `end` produced UNLOADABLE Luau yet stamped
  `present=True`. Now counts block openers `{function,do,then,repeat}` vs closers `{end,until}` (never
  negative, net zero), so a swallowed `end` fails-closed → revert → `present=False`. `elseif` is a
  closer too (it cancels its own upcoming `then`'s increment) so a well-formed `if/elseif/else/end`
  chain (TWO `then`, ONE `end`) is NOT false-rejected — caught in self-review as an over-abstain
  regression and fixed before commit. Tests `test_r3_fallback_validates_if_then_end_block_balance`,
  `test_r3_fallback_happy_path_still_loadable`, `test_r3_fallback_accepts_well_formed_if_elseif_else`.

- **D-S1-r3-b [Mechanical] — P2: `_canonical_receiver` is now scope-aware (dominance), not just order-aware.**
  New `_seed_dominates_use` gates seed admission: a `cam = Camera.main.transform` seed buried in a
  dead/conditional block (`if(false){ cam=...; }` / braceless `if(c) cam=...;` / a `switch` case) does
  NOT dominate `weaponSlot = cam.GetChild(0)` below it, so the resolver ABSTAINS (no rig fact). Proof:
  (1) brace-depth from seed→use never goes negative (a `}` that closes the seed's block ⇒ out of scope);
  (2) the seed statement is not the braceless single-statement body of a control-flow keyword
  (`if/else/while/for/foreach/case/do`). Conservative — abstains whenever it cannot cheaply prove
  dominance (a missed fact is safe; a false-admit is not). Straight-line happy path still admits.
  Tests `test_r3_seed_in_dead_conditional_block_abstains`, `test_r3_seed_in_braceless_if_abstains`,
  `test_r3_straight_line_seed_still_admits`.

- **D-S1-r3-c [Mechanical] — P3: `_self_is_shadowed_at` covers ALL `self`-binding forms.**
  Broadened beyond `local self` + `function(...self...)` param to also catch a function NAMED `self`
  (`local function self()` / `function self()` → shadows the enclosing scope) and a `for`-loop VARIABLE
  named `self` (`for self in` / `for _, self in` / `for self = ...` → shadows the loop body, registered
  at the `do` that opens it). Foreign `self` reads in these forms are no longer rewritten and no longer
  counted as discharged (mirror in `_has_surviving_field_read` inherited). Loop-var shadow is scoped to
  the body only — a real receiver read outside the loop still rewrites. Tests
  `test_r3_shadow_local_function_self_not_rewritten`, `test_r3_shadow_for_loop_self_var_not_rewritten`,
  `test_r3_for_loop_self_does_not_block_real_receiver_read`.

- **D-S1-r3-d [Mechanical] — P4: neutralize anchor requires an actual ordinal child access.**
  `_CAMERA_CHILD_RHS_RE` dropped the bare `\bself\.cam\b` alternative; it now matches ONLY a real
  ordinal child access (`[:.]GetChild(n)` / `[:.]GetChildren()[n]`). A boolean/config RHS that merely
  mentions `self.cam` (`self.weaponSlot = self.cam and self.defaultSlot`) is no longer neutralized to
  `nil` (which would be a false-green) — the neutralizer ABSTAINS, leaving the write for the verifier.
  The corpus write `self.cam and self.cam:GetChildren()[1]` still carries the ordinal, so it still
  neutralizes. Same regex backs `_has_camera_child_write`, so condition (2) of `_binding_discharged`
  stays consistent. Test `test_r3_neutralize_anchor_requires_ordinal_child_access`.

REVIEW NOTE: codex was non-functional this round (echoed the diff / read the full tree without producing
a verdict, then errored on a narrowed read-only prompt — matches the documented codex-outage pattern).
Substituted Claude self-adversarial review + a 20-case empirical sweep across all three fixed functions
(balance / dominance / shadow), all passing in the safe direction; the second independent codex voice is
recorded as OWED for this fix round.

## Slice S1 — round-4 codex review fixes (root cause: FAIL CLOSED + bind to the deterministic source, not a downstream fingerprint)

All four round-4 findings shared the round-3 root (trust a shape the code can't handle with
certainty) PLUS the converter kernel: a load-bearing identity/discharge gate keyed on a downstream
FINGERPRINT (a bare same-named method; a raw child-name splice) abstains silently or false-passes.
Each fix re-anchors on the deterministic signal and biases to abstain; the corpus happy-path
(SimpleFPS Player, child `WeaponSlot`) still discharges `present=True`.

- **D-S1-r4-a [Mechanical] — BLOCKING: raw `child_name` spliced into Luau identifiers.**
  A Roblox child name with a space/special char (`"Weapon Slot"`) raw-spliced into
  `function <Class>:_resolve<child>()` / `self:_resolve<child>()` yields invalid Luau, and the
  analyzer-absent `_structural_balance_ok` accepted it → `present=True` on broken code. New
  `_method_suffix(child)` derives a VALID-Luau-identifier method-name suffix: a valid identifier
  passes verbatim (happy-path + idempotency preserved); otherwise illegal chars → `_` plus an
  8-hex sha1 of the REAL name (collision-resistant). The suffix is threaded through inject /
  rewrite / discharge / neutralize-comment; the rig LOOKUP still uses the REAL `child` string in
  `rig:FindFirstChild("<real name>", true)`. `field` (a C# identifier) is also identifier-guarded
  (`_LUAU_IDENT_RE`) → abstain (`present=False`) if it can't yield a valid identifier. Tests
  `test_r4_special_char_*`.

- **D-S1-r4-b [Mechanical] — BLOCKING: preexisting same-named resolver false-discharges.**
  A preexisting foreign `function <Class>:_resolve<Child>()` (body `return nil`) + a call made the
  shape-only discharge report `present=True` with the lowering doing NO work this run (`modified=0`).
  Discharge now binds to the lowering's OWN emit: new `_has_own_resolver_method` requires the
  distinctive own-emit body marker (`m:GetAttribute("_MainCameraRig")`) inside the method body, not
  a bare same-named declaration. Used by BOTH the idempotency guard in `_inject_resolver_method` (a
  foreign method does NOT count as already-injected → the lowering re-injects its OWN) and
  `_binding_discharged` (1a). So the foreign method alone never discharges; the only `present=True`
  paths are (i) the lowering's own emit landed this run, or (ii) idempotent re-run over its own
  prior output. Dropped the now-orphaned `_has_resolver_method`. Tests
  `test_r4_preexisting_foreign_resolver_method_not_false_discharged`,
  `test_r4_foreign_resolver_alone_is_not_own_emit`.

- **D-S1-r4-c [Mechanical] — MAJOR: `_lhs_is_bare_field` admitted a typed local declaration.**
  `Transform weaponSlot = Camera.main.transform.GetChild(0);` (a C# typed LOCAL) was admitted as a
  rig fact (flipping `resolved_total`, a bogus fail-closed path for valid code). `_lhs_is_bare_field`
  now walks back over whitespace (incl. newlines); a preceding IDENTIFIER char means a leading type
  token (`Transform`/`var`) → typed local DECLARATION → abstain. A bare field write's preceding
  non-blank char is always a statement terminator (`;`/`{`/`}`) or start-of-file, so the real field
  write (and `this.`-qualified write) still admits. Tests
  `test_r4_typed_local_decl_abstains_field_write_admits`, `test_r4_var_local_decl_abstains`.

- **D-S1-r4-d [Mechanical] — MINOR: fallback tail scan false-rejected a trailing comment.**
  `_structural_balance_ok`'s post-`return` tail scan was raw-text, false-rejecting a valid source
  whose trailing comment after `return <Class>` carried `end`/`function` as prose. The scan is now
  code-position-aware (`_luau_pos_is_code` per keyword match), and `_RETURN_IDENT_RE` tolerates an
  optional trailing line-comment so a commented epilogue is still a valid splice point. A REAL
  code-level `function`/`end` after the return is still rejected (the after-`return` splice bug).
  Test `test_r4_fallback_tail_scan_allows_trailing_comment`.

VERIFY: `tests/test_rifle_rig_retarget.py` 54 green; full `-m "not slow"` suite 3032 passed / 45
skipped / 5 xfailed; no-Any gate pass. Each of the 7 new R4 tests proven RED against `2722219`
production. Corpus happy-path (`WeaponSlot`) discharge `present=True` confirmed (incl. analyzer-absent).

REVIEW NOTE: the independent second codex voice is recorded as OWED for this fix round (consistent
with the round-3 codex-outage note); substituted Claude self-adversarial review + targeted empirical
spot-checks (spaced-child idempotency/lookup, `_lhs_is_bare_field` four branches).

## Slice S1 — round-5 review fixes (STRUCTURAL fix: stop the regex whack-a-mole, anchor the fact on the camera RECEIVER)

Root cause across the 4 round-5 P1s + the dual-voice codex follow-ups: a load-bearing
neutralize/discharge gate keyed on field+ordinal SHAPE (not the exact camera-rooted assignment) and
analyzer-absent paths that fail-open. The structural fix carries the camera-receiver anchor ON THE
FACT and makes every camera-ness decision a PROVEN property of the final Luau, biasing to abstain.

- **D-S1-r5-a [Structural] — BLOCKING (lowering): neutralize/discharge now CAMERA-RECEIVER-anchored
  via the fact.** `RigRootedRetargetFact` gained `cam_receiver` (the C# group-2 receiver text the
  resolver admitted: a bare seeded symbol `cam`, or `Camera.main.transform`). The lowering builds a
  `_CamAnchor` and neutralizes ONLY a write whose WHOLE RHS value IS a camera-rooted ordinal access
  (`(<cam> and )* <camrecv>:GetChildren()[n]`, anchored `^...$`) whose `<camrecv>` is PROVABLY the
  camera. "Provably camera": a canonical literal (`workspace.CurrentCamera`), OR a seeded symbol whose
  NEAREST PRECEDING code-position binding is `<sym> = <canonical literal>` AND that binding REACHES the
  use (`_binding_reaches_use`: no enclosing do/then/function block of the binding closes before the
  use). A same-field ordinal on a non-camera receiver (`self.defaultSlots:GetChildren()[1]`), a
  disjunction (`X or self.cam:...`), a FOREIGN `and` guard (`self.defaultSlots and self.cam:...`), a
  param/loop-var `cam`, and a closed-scope / dead-branch seed all ABSTAIN -> present=False. Discharge
  (2a) additionally requires the lowering's OWN neutralize marker (positive proof the camera write was
  found+neutralized this run / idempotent over our own output), not merely "no camera write survives".
  Tests `test_r5_neutralize_anchored_on_camera_receiver_not_field_ordinal`,
  `test_r5_mixed_disjunction_rhs_is_not_neutralized`, `test_r5_camera_symbol_rebound_to_non_camera...`,
  `test_r5_camera_symbol_as_param_or_loopvar_abstains`,
  `test_r5_camera_binding_in_closed_or_dead_scope_does_not_dominate`.

- **D-S1-r5-b [Structural] — BLOCKING (lowering): `_has_own_resolver_method` = STRUCTURAL equality to
  the canonical emit, not a marker substring.** It compares the emitted method (its `function`-line
  through closing `end`, line-normalized) to the exact `_resolver_method_text`. A FOREIGN same-named
  method that merely uses `m:GetAttribute("_MainCameraRig")` as live code is NOT the own emit
  (re-injected/abstained); idempotency over the byte-identical own output still holds. Tests
  `test_r5_foreign_resolver_with_marker_is_not_own_emit`,
  `test_r5_has_own_resolver_rejects_foreign_marker_body`.

- **D-S1-r5-c [Structural] — MAJOR (resolver): `_lhs_is_bare_field` ALLOW-LIST by terminator.** A bare
  field write is admitted ONLY when the preceding non-blank char is `;`/`{`/`}` or start-of-file. This
  fail-closed choice ABSTAINS on EVERY typed-local form a single-char reject-list could not enumerate —
  simple/generic(`>`)/array(`]`)/qualified(`.`)/nullable(`?`)/tuple(`)`)/comment-separated(`/`) — rather
  than admit a bogus rig fact (a false fail-close on valid code). Over-abstaining a rare
  conditional/labeled camera write is fail-closed-safe; the corpus write is a plain statement, so the
  happy-path admits. Tests `test_r5_resolver_abstains_generic_array_qualified_typed_locals`,
  `test_r5_lhs_abstains_on_every_typed_local_form`.

- **D-S1-r5-d [Mechanical] — MAJOR (lowering): `_structural_balance_ok` skips long-bracket
  comments/strings + line comments wholesale, fail-CLOSED on unterminated.** The analyzer-absent
  fallback advances past `--[[ ]]` / `[[ ]]` / `[=[ ]=]` / `--` spans before counting brackets/keywords
  (the `]]` closer no longer leaks as a code-level `]`), and returns False on an UNTERMINATED
  long-bracket or a short string spanning a newline (invalid Luau). Tests
  `test_r5_structural_balance_skips_block_comment_brackets`,
  `test_r5_structural_balance_fails_closed_on_unterminated_long_bracket`.

REVIEW: dual-voice — Claude self-adversarial + THREE codex `codex exec` rounds (R1 found 4 edges
[receiver-name-only anchor, marker-substring own-emit, fallback fail-open, allow-list over-abstain]; R2
found 4 more [mixed-RHS disjunction, param/loopvar symbol, stale-marker, typed-local reject-list
escapes]; R3 found 3 [text-nearest no-dominance scan, foreign `and` guard, marker spoof]). Each round's
findings drove a structural tightening; re-reviewed the corrected artifact each round. Two accepted
RESIDUALS (pathological, not real-pipeline-reachable, the verifier S1b is the independent authority):
(i) `_has_neutralized_write` marker spoof requires the AI to emit our EXACT suffixed marker (our
deterministic post-transpile artifact the AI never emits) AND even then the output is a CORRECT retarget;
(ii) a `if <runtime-cond> then cam = <canonical> end` seed whose condition is genuinely runtime-true is
treated as reaching (only statically-closed/dead scopes abstain).

VERIFY: `tests/test_rifle_rig_retarget.py` 65 green; full `-m "not slow"` 3043 passed / 45 skipped /
5 xfailed; no-Any gate pass. The 4 original findings proven RED against `a16c964` at unit + full-pipeline
level (F1 neutralize non-camera ordinal, F2 marker-substring own-emit, F3 generic typed-local admit, F4
block-comment false-reject); the disjunction follow-up also RED. Corpus happy-path (`self.cam and
self.cam:GetChildren()[1]`, child `WeaponSlot`) discharges present=True + idempotent byte-identical in
BOTH analyzer-present and analyzer-absent modes.

## Round 6 (slice 1.1, S1) — 2 codex review fixes (child_ref_resolver.py)
- **P1 BLOCKING — `_canonical_receiver` member-access seed.** `\b<recv>\s*=` matched the tail
  of `other.cam = Camera.main.transform`, seeding the bare local `cam`. Fix: in the nearest-binding
  scan, reject a match whose immediately-preceding char is `.` (member-access dot). Bare-local seed
  still admits; `other.cam = ...` now abstains (resolved_total=0).
- **P1 MAJOR — `_lhs_is_bare_field` comment-blind walk-back.** It skipped only whitespace before the
  LHS, so a `// line` or `/* block */` comment between the prior statement terminator and a bare field
  write caused a false drop. Fix: new `_skip_ws_and_comments_back()` walks back over whitespace AND
  comments, classified authoritatively by `_cs_pos_is_code` (with an explicit comment-OPENER `/`+`/`|`*`
  recognizer, since the from-start scanner classifies an opener's first `/` as code). Comment-preceded
  bare field writes (`//` and `/* */`) now admit; round-5 typed-local allow-list NOT regressed
  (typed local after a comment still abstains).
- **Verify:** test_rifle_rig_retarget.py 70 GREEN; resolver/corpus/rifle/child_ref suite 186 GREEN;
  no-Any gate pass. 3 covering tests (member-write-seed, line-comment-admit, block-comment-admit)
  proven RED against `e540b7c`; 2 guard tests (bare-local-admits, typed-local-after-comment-abstains)
  green both pre/post. Corpus happy-path (SimpleFPS Player, `weaponSlot = self.cam and
  self.cam:GetChildren()[1]`, child `WeaponSlot`) discharges present=True (corpus suite green).
- D-S1-r7: revert net-negative round-6 member-dot seed filter (`_canonical_receiver`) per user; accept foreign-member (`other.cam`) false-admit as documented residual (xfailed test, netted by S1b independent verifier); restore legitimate `this.cam` field seed (regression fix). KEPT the round-6 comment-skip fix. No new C# regex/parsing.

---

## S1b IMPLEMENTATION notes (2026-06-12) — slice rifle-dropped-ref/1.2 (BINDING-PRESENT FAIL-CLOSED VERIFIER)

- **D-S1b-impl-a [Mechanical] — INDEPENDENT scan re-derives the suffix from `child` alone; no `cam_receiver`/`anchor` available to the verifier.** S1's authoritative discharge (`_binding_discharged`) takes `(source, field, child, suffix, anchor)` — but the `rig_binding` carrier the verifier reads is only `{field, child, present[, multi_fact]}` (NO `cam_receiver`). Per §1.6, the verifier's `_rig_binding_discharged(source, field, child)` is DELIBERATELY simpler/independent than the lowering's self-check: it does NOT require the lowering's own-emit markers (`_has_neutralized_write` / structural method equality / camera-anchor) — those are the lowering's defense-in-depth self-derive. The verifier's §1.6 discharge is: (1a) `function <Class>:_resolve<suffix>(` exists at code pos, (1b) ≥1 `self:_resolve<suffix>(` call, (1c) no surviving bare `self.<field>` consumer READ (Awake/Start lifecycle reads + LHS + member-tail excluded — mirrors the lowering's yield-guard so an intentionally-abstained lifecycle read does not count), (2) no surviving `self.<field> = <...GetChild(n)|GetChildren()[n]...>` ordinal write (RHS span-limited to end-of-line). `suffix` reconstructed by `_rig_method_suffix(child)`, a verbatim copy of the lowering's `_method_suffix` contract (valid-ident verbatim, else sanitize+sha1-8) — keyed on the deterministic IR `child`, NOT imported from the lowering (so the verifier stays an independent authority, not a consumer of lowering state).

- **D-S1b-impl-b [Mechanical] — `_luau_pos_is_code` is the only position guard imported (reuse, matches check D).** The verifier already imports `_luau_pos_is_code` from `child_index_lowering`; check D's `_count_surviving_child_ordinals` uses only that. I reused it (NOT `_luau_pos_in_long_bracket` from `trigger_stay_lowering`) to keep the verifier's import surface minimal and consistent with the existing checks. Sufficient: the transpiled module epilogue/method bodies are not inside long-bracket strings.

- **D-S1b-impl-c [Mechanical] — independence is BIDIRECTIONAL: PASS requires `discharged AND stamp`.** A stamp/scan disagreement in EITHER direction fails: forged `present=True` + undischarged source (the e.iv FIX-1 load-bearing case — a syntax-revert or stale-resume carrier) AND discharged source + mis-stamped `present=False`. The detail row names both (`discharged=<bool>, lowering-stamp=<bool>`).

- **D-S1b-coupling [Mechanical] — FAIL_CLOSED_CHECKS flip is admissible HERE; corpus stays GREEN (no S2 dependency to build).** Adding `rig_binding_present` to `FAIL_CLOSED_CHECKS` did NOT red the un-regenerated SimpleFPS corpus in this worktree: the committed pre-`rig_binding` fixture scripts have `rig_binding=None` → the check ABSTAINS (None → no row), so `test_contract_corpus.py` (9) stays green. The flip is correct per the design (S2's regen captures the discharged Player source + `rig_binding present=True`, EXERCISING the check green); no coordination needed — the flip is kept as the design wants and the corpus goes from ABSTAIN (now) to EXERCISE+GREEN (at S2). Verified: full `-m "not slow"` 3064 passed / 0 failed.

- **VERIFY:** `tests/test_rig_binding_present.py` 16 GREEN (e.i RED+promote, e.ii GREEN, e.iii ABSTAIN×2, e.iv independence BOTH directions, e.v pre-fix-RED proof [monkeypatch the check → dropped binding sails through; PROVEN end-to-end at verify_contract level], e.vi fact-anchored-not-grep + different-field/child + unrelated-script-abstains); contract/verifier/corpus/rifle/rig suite 504 passed / 1 xfailed (pre-existing S1 residual); full `-m "not slow"` 3064 passed / 45 skipped / 6 xfailed; no-Any gate clean (carrier read as `dict[str, object]` via `str(...)`/`is True`). GREEN/discharged test sources produced by running S1's REAL lowering on the on-corpus shape (cam_receiver bare symbol `cam`), not a synthetic guess.

## Slice 1.2 — dual-voice REVIEW round 1 (3 BLOCKING false-negatives addressed)

- **D-S1b-r1-f1 [BLOCKING fixed] — discharge now requires the resolver method BODY to be the RIG resolver, not a same-named method.** `_rig_binding_discharged` (1a) previously accepted ANY code-position `function <Class>:_resolve<suffix>(` declaration. A FOREIGN stub (`return nil` / wrong lookup) + a forged/stale `present=True` discharged silently (defeating the stale/forged-resume threat model). FIX: new `_rig_resolver_body_is_rig_lookup(source, suffix, child)` confirms the distinctive `_MainCameraRig` rig lookup as LIVE code INSIDE the method's block-balanced span — BOTH `:GetAttribute("_MainCameraRig")` AND `FindFirstChild("<child>", true)` (the real S1 emit, anchored on the deterministic `child`). New `_rig_method_body_end` block-keyword-balances the method span (long brackets/strings skipped) so a marker elsewhere in the file can't satisfy a foreign stub. Generic (no `weaponSlot`/`WeaponSlot` hardcode). Test `test_review_f1_foreign_resolver_stub_does_not_discharge` — RED vs 4a5aa84 (pre-fix returned discharged=True), GREEN post-fix; genuine corpus shape still discharges.

- **D-S1b-r1-f2 [BLOCKING fixed, both voices] — surviving-ordinal-write scan is now continuation-aware (multiline).** `_rig_has_surviving_ordinal_write` previously read the RHS only to the first `\n` (`source.find("\n")`), missing a multiline surviving write (`self.weaponSlot =\n  self.cam:GetChildren()[1]`) → discharged=True while a camera-child binding re-clobbers at runtime. FIX: replaced the single-line span with `_rig_statement_rhs_end`/`_rig_line_continues` (faithful re-impl of S1's `_statement_rhs_end`/`_line_continues` — bracket/string-balanced, `and`/`or`/`..`/operator continuation across newlines). Also skip leading whitespace/newlines after `=` so a value-on-the-next-line write is spanned (the `=` already establishes mid-statement). Test `test_review_f2_multiline_surviving_ordinal_write_is_detected` — RED vs 4a5aa84 (pre-fix `_rig_has_surviving_ordinal_write`→False), GREEN post-fix.

- **D-S1b-r1-f3 [BLOCKING — stated mechanism does NOT reproduce; defense-in-depth applied].** Finding #3 claimed declaration/call detection counts fake `_resolve...` tokens inside `[[...]]`/`[=[...]=]`/`--[[...]]` long-bracket literals. VERIFIED EMPIRICALLY (exhaustive enumeration of all long-bracket forms incl. level-1/2 and comment/string variants + nesting/level-mismatch cases): `child_index_lowering._luau_pos_is_code` — which the verifier ALREADY imports and uses on every rig scan — correctly returns False for ALL positions inside long-bracket strings/comments (it scans from file start tracking long-bracket level, child_index_lowering.py:94–146). No false-positive position found; the F3 covering test PASSES against pre-fix code, confirming the bug as stated does not exist via that path. STILL APPLIED defense-in-depth: imported `_luau_pos_in_long_bracket`/`_long_bracket_open_level` (deterministic position primitives, NOT lowering discharge-logic — keeps the verifier independent) and added a `_rig_pos_is_real_code = is_code AND NOT in_long_bracket` predicate used by every rig scan, plus `_rig_code_contains` long-bracket exclusion. Regression test `test_review_f3_resolver_only_in_long_bracket_does_not_discharge` locks the correct non-discharge behavior. (Per audit-todo-framing-is-a-hypothesis: the stated mechanism was a hypothesis; the verified cause is "already-handled — defense-in-depth only.")

- **VERIFY (r1):** `tests/test_rig_binding_present.py` 19 GREEN (16 original + 3 new); F1+F2 PROVEN RED against 4a5aa84 (pre-fix false-discharge), F3 passes both (mechanism doesn't reproduce); corpus happy-path STILL discharges (`test_real_lowering_discharges_corpus_shape` + driving S1's real `lower_rifle_rig_retarget` → `_rig_binding_discharged`=True). Subset `-k "contract or verifier or corpus or rifle or rig"` 507 passed / 1 xfailed; no-Any gate clean. Independence preserved (D-P1-c-v2): scan `script.source`, anchored on deterministic `field`/`child`, `present` cross-checked (PASS=discharged AND present), `rig_binding=None` abstains. Codex adversarial re-review of F3 dispatched (exit 0; prose verdict lost to a redirect rotation, but the structural question is fully settled by exhaustive enumeration).

- **D-S1b-r2-f1 [BLOCKING fixed] — close the POSTFIX-continuation CLASS (not just `[`).** The round-1 continuation scan (`_rig_line_continues`/`_RIG_CONTINUATION_HEAD_RE`) admitted operator-led + `:`-led heads but NOT a `[` index continuation, so a surviving camera-child write split BEFORE the index (`self.weaponSlot = self.cam:GetChildren()\n    [1]`) stopped the RHS span at the newline → `_rig_has_surviving_ordinal_write` missed the live clobber → `_rig_binding_discharged`=True (fail-closed false-NEGATIVE; the binding re-clobbers at runtime). FIX: admit the full Luau postfix-continuation head class in `_RIG_CONTINUATION_HEAD_RE` — `[` (index), `(` (call), `.`/`:` (member/method) — closing the class so `(`/`.` don't whack-a-mole next round. Also made `_RIG_ORDINAL_WRITE_TAIL_RE` whitespace-tolerant at each postfix junction (`GetChildren\s*\(\s*\)\s*\[`, `GetChild\s*\(`) so the spanned access matches across the split newline. Tests `test_review_r2_split_before_index_surviving_write_is_detected` + `test_review_r2_split_before_getchild_call_is_detected` — RED vs 1ca1cbb (scan→False), GREEN post-fix; member-split + over-reach added.
- **OVER-REACH TRADEOFF (flagged).** Admitting `(`/`[` as continuation heads risks a span swallowing a FOLLOWING statement that legitimately starts with `(`/`[` (the rare `a = b\n(f)()` ambiguity / a parenthesized-expression statement). GUARD: `_RIG_STATEMENT_BOUNDARY_RE` — a next line whose first token opens a new statement (`local`/`function`/`return`/`end`/`if`/`for`/`while`/`self.x =`/`name =`/...) stops the span. Where the boundary is genuinely ambiguous the scan KEEPS GOING by design — fail-closed: over-detecting a survivor → `discharged=False` → a violation row is the SAFE direction for this BINDING-floor verifier; a MISSED survivor is the unsafe direction. `test_review_r2_over_reach_guard_does_not_swallow_following_statement` locks that a neutralized `self.weaponSlot = nil` followed by `local other = somethingElse:GetChildren()[1]` STILL discharges True (the guard held, no false survivor).
- **VERIFY (r2):** `tests/test_rig_binding_present.py` 23 GREEN (19 original + 4 new); split-before-index + split-before-call PROVEN RED against 1ca1cbb. Corpus happy-path STILL discharges present=True (`test_real_lowering_discharges_corpus_shape` + `test_e2_green`, driving S1's real `lower_rifle_rig_retarget`). Subset `-k "contract or verifier or corpus or rifle or rig"` 511 passed / 1 xfailed; no-Any gate clean. Independence/None-abstain/present cross-check semantics intact; generic. Commit f4f1e63.

- **D-S1b-r3 [BLOCKING fixed] — STRUCTURAL code-projection normalization closes the surviving-write formatting CLASS.** Round-2 closed the split-before-postfix sub-class but left SIBLING members of the same surviving-ordinal-write detection class open (all fail-OPEN: `_rig_has_surviving_ordinal_write`→False / `_rig_binding_discharged`→True while a camera-child ordinal write survives — the load-bearing hole on the stale/forged `present=True` resume path where the source scan is the SOLE authority, acceptance e.iv): (a) whitespace at the receiver→`:`→method junction (`self.cam : GetChildren ( ) [ 1 ]`, `self.cam: GetChild(1)`) — the tail regex had no `\s*` there; (b) a `--` comment between tokens truncating the RHS span (`self.cam:GetChildren() -- gap\n [1]`, `self.cam -- gap\n :GetChildren()[1]`) — `_rig_statement_rhs_end` broke at `--`; (c) by implication string-key/`\r\n`. Per the user's directed approach (structural normalization over more non-converging junction regex): the scan's manual inline whitespace/comment/continuation handling has a long formatting tail. FIX: build a position-PRESERVING CODE PROJECTION of `script.source` (`_rig_code_projection`) — walk with the same state machine as `_luau_pos_is_code` (line/block comments, long strings, short strings) and blank every comment/string/long-bracket SPAN incl. its delimiters to spaces (newlines kept so the continuation + over-reach boundary machinery is unchanged); run the existing multiline-aware span on the projection, then whitespace-collapse the spanned RHS (`_rig_collapse_code_ws`) before a now-MINIMAL canonical tail pattern (`:GetChildren()[n]` / `:GetChild(n)`, no per-junction `\s*`). All whitespace/comment/line-split variants collapse to the one canonical access form → one match closes them all; tokens inside strings/comments are spaces → can't match. Kept `_RIG_STATEMENT_BOUNDARY_RE` fail-closed over-reach bias. INDEPENDENCE preserved: projection derived from `script.source`, anchored on deterministic `field`/`child`; `present` stays a cross-check only. CRITICAL non-regression: the legit resolver's INTERNAL `:GetChildren()`/`FindFirstChild`/`:GetAttribute("_MainCameraRig")` (inside `_resolveWeaponSlot`) + the neutralized `self.weaponSlot = nil` are NOT `self.<field> = <ordinal>` writes → NOT mis-detected; corpus happy-path still discharges True.
- **VERIFY (r3):** `tests/test_rig_binding_present.py` 30 GREEN (23 prior + 7 new). 5 newly-closed holes PROVEN RED against f4f1e63 (whitespace-at-`:`-junction x2, `--`-comment-between-tokens x2, ordinal-inside-string false-detect); `\r\n` + corpus-happy-path are regression guards (green both pre/post — `\r\n` was already in the whitespace set, corpus must never flag). Corpus happy-path STILL discharges (`_rig_has_surviving_ordinal_write(green,...)`=False, `_rig_binding_discharged`=True, driving S1's real `lower_rifle_rig_retarget`). All prior findings stay closed (foreign-stub, multiline/postfix splits, long-bracket fake tokens, single-line). Subset `-k "contract or verifier or corpus or rifle or rig"` 518 passed / 1 xfailed; no-Any gate clean. Generic, no hardcoded game strings.

## IMPLEMENT-stage decisions — slice 1.2 (S1b binding-present verifier)

- D-S1b-r4 [Bug] — `_rig_method_body_end` (contract_verifier.py) method-span detection counted every `then` as a
  fresh block opener but only `end`/`until` as closers, so an `if ... elseif ... elseif ... end` chain (multiple
  `then`, ONE `end`) over-counted openers and the span walked PAST a foreign `_resolve<suffix>` stub's closing `end`
  into later unrelated code — sweeping a later decoy helper carrying `:GetAttribute("_MainCameraRig")` +
  `FindFirstChild("<child>", true)` into the "method body", so `_rig_resolver_body_is_rig_lookup` returned True and a
  foreign stub false-discharged (REOPENED the round-1 foreign-stub closure). FIX: mirror S1's proven
  `_structural_balance_ok` block-keyword set — `elseif` is a CLOSER that cancels its own upcoming `then`'s increment
  (net 0 for the chain); `else` follows no `then` (pure +0, not a token). Audited the full block set: `function`/`do`/
  `then`/`repeat` open; `end`/`until`/`elseif` close; `repeat`→`until` balanced. RED proof against bb288b6:
  foreign elseif-stub + decoy false-discharged True; post-fix False. Corpus happy-path (real resolver body with
  `if`/`for` + `for ... do ... end`) STILL discharges True. 35 tests green (5 new r4 cases), no-Any pass.

## IMPLEMENT-stage decisions — slice 1.3 (S2 fixture regen) — REDESIGN

- **D-S2-REDESIGN [BLOCKING — reality diverges from the design's fixture ground-truth].** S2 cannot reach
  acceptance (f)+(g) against the REAL pipeline. Validated empirically (warm cache from main `.cache/llm`, TTL
  refreshed into `/tmp/warm_cache_refreshed`; `CONTRACT_CORPUS_PROJECTS_ROOT`=main test_projects; S1+S1b code in
  worktree). The `_FIELDS += "rig_binding"` edit landed and IS correct (the carrier now flows into capture). On the
  real run:
  - The resolver fact IS produced — `Player.rig_binding = {field:"weaponSlot", child:"WeaponSlot", present:False}`,
    `child_ref_resolution = {1,1}` (resolver flipped it from the C# `weaponSlot = cam.GetChild(0)`, deterministic,
    pre-transpile). Good.
  - BUT the LOWERING ABSTAINS (`present=False`) and `_rig_binding_discharged`=False → `rig_binding_present/Player`
    fires as a real (warning) violation → `regen_contract_corpus.py` REFUSES to write (correct fail-closed behavior).
  - ROOT CAUSE: the design's §30 "Fixture ground truth" (the AI-emitted `self.weaponSlot = self.cam and
    self.cam:GetChildren()[1]` camera-child write in Awake + 4 reads in GetRifle at :279/:282) is a STALE cache
    snapshot (entry `2d27aaf6`, 9.6 days old). The LIVE cache key the current Player.cs + frozen prompt resolves to
    is `28d60ee6` (0.6 days old, deterministically consulted+hit — verified by instrumenting `_load_cache`), whose
    AI output is a DIFFERENT shape: `self.weaponSlot = self.gameObject` (the AI already collapsed the Camera.main
    ordinal to a GameObject fallback, leaving only a `-- weaponSlot was cam.GetChild(0)` COMMENT and a single read
    `local slot = self.weaponSlot or self.gameObject`). There is ZERO camera-child ordinal in the live AI output
    code (the only `GetChild` token is inside a comment).
  - The lowering+verifier are keyed on the camera-child AI-output SHAPE (`_neutralize_assignment` anchors a
    `:GetChild(n)`/`:GetChildren()[n]`/`self.cam`-rooted RHS; the whole S1b discharge scan, hardened across rounds
    r2-r4, anchors the same). On the live `self.gameObject` shape there is nothing to anchor → correct abstain →
    fail-closed. This is the EXACT memory-documented fragility: *generic binding on non-deterministic AI-output
    fingerprints abstains silently/loudly on a valid-but-different AI shape* (5 distinct cached Player transpiles
    exist: `:GetChildren()[1]`, `FindFirstChildWhichIsA("BasePart") or gameObject`, `= gameObject`,
    `= camChildren[1]`, multi-step `child`). The mechanism (resolver fact keyed on deterministic upstream C#) is
    sound, but the lowering's discharge target is keyed on the AI output, which has drifted.
  - WHY NOT just re-transpile Player.cs fresh: the `apikey`/`apikey_run` files are the ROBLOX Open Cloud key (upload),
    NOT an Anthropic key; the regen tool runs cache-only by design (no Anthropic key passed). A fresh transpile would
    (a) need an Anthropic key the harness lacks and (b) re-emit a NON-deterministic shape (could be any of the 5),
    so it can't be relied on to produce the camera-child shape the lowering expects.
  - DECISION OWED (phase design / S1): either (i) extend the lowering to ALSO discharge the `self.weaponSlot =
    self.gameObject` degraded shape (anchor on the deterministic rig FACT + the `self.<field> = self.gameObject`
    fallback write that the resolver fact says was a Camera.main ordinal, retargeting it to the rig regardless of the
    AI's chosen fallback) — i.e. make the lowering's anchor the DETERMINISTIC FACT, not the AI ordinal shape; OR
    (ii) re-baseline the design's fixture ground-truth + lowering anchor onto the live `28d60ee6` shape; OR (iii)
    pin the corpus to a specific cached transpile (freeze entry `2d27aaf6` as the canonical SimpleFPS Player snapshot
    and gate the cache so the live key resolves to it). (i) is the generic, durable fix (anchor on the upstream
    signal, per the memory kernel); it is S1-scope, so S2 is blocked behind an S1 revision.
  - The `_FIELDS += "rig_binding"` edit is RETAINED (correct, load-bearing, independent of the above). NOT committing
    a fixture (would be a fabricated/non-real snapshot or a fail-closed-violating one).

## DESIGN-stage decisions — Phase 1 Path A re-anchor (resolves D-S2-REDESIGN)

These adopt option (i) from D-S2-REDESIGN — anchor the lowering's discharge on the deterministic fact, not the
AI write shape. Dual-voice design-reviewed 3 rounds (`claude-design-review-s2-r2.md`/`-r3.md`,
`codex-design-review-s2-r2.verdict.md`/`-r3.verdict.md`, proposal `design-s2-reanchor-proposal.md`) + user-approved
2026-06-13 (D-P1-PATHA.review). Supersede the relevant clauses of D-P1-b / D-P1-c-v2.coupling / D-P1-d(3),(5).

- **D-P1-PATHA.reanchor [Classification: User-Challenge → resolved] — RE-ANCHOR the rig retarget on the deterministic
  fact via the consumer-side READ reroute; discharge := no raw `self.<field>` dot-form READ survives.** The write-shape
  lowering anchored NEUTRALIZE + DISCHARGE on matching the camera-child ordinal WRITE in the AI output, but the real
  pipeline emits 5+ distinct write shapes for this one field (`:GetChildren()[1]`, `self.gameObject`,
  `__unityChild(self.cam,1)`, a multi-step `local children; children[1]`, a `FindFirstChild` fallback) — so it ABSTAINS
  on real output and the rifle is never rebound (the exact `D-S2-REDESIGN` blocker). FIX: make the LOAD-BEARING contract
  the fact-anchored consumer-READ reroute — inject the per-instance resolver (unchanged emit) and reroute every consumer
  READ of `self.<field>` (dot-form) → `self:_resolve<Child>()`, EXCEPT in non-yielding lifecycle methods (`Awake`/`Start`)
  where abstain. DISCHARGE := own-resolver present + ≥1 call + NO raw `self.<field>` dot-form read survives (outside
  Awake/Start). RATIONALE: the READ access keys on the AI-STABLE member name (the AI preserves class-member identity
  across all 5 shapes — empirically verified) where the old anchor keyed on the AI-VOLATILE value expression; this is the
  memory kernel (anchor on the deterministic upstream signal, not an AI-output fingerprint). RHS-agnostic → fires on all
  5 shapes. Empirically: on the real fixverify shape-3 output the read reroute discharges `present=True` (the old
  write-anchored lowering returned modified=0/present=False).

- **D-P1-PATHA.boundary [Taste — the generality mechanism, both voices required] — the verifier MUST FAIL CLOSED on the
  forms the read-reroute cannot safely rewrite.** Path A's `self.<field>` dot-form scope is honest for the current OO
  generic emitter ONLY if every unsupported form is loud, never silently wrong / fail-open. The verifier emits its OWN
  violation row (never a blanket exempt, never silent) on: a raw field read in a NON-yielding lifecycle method
  (`Awake`/`Start`) that would cache stale derived state; any NON-`self` receiver form (module-table `<Class>.<field>`,
  `owner.<field>`, receiver-alias `local p = self; p.<field>`); any bracket-index `self["<field>"]`; and a shadowed-`self`
  read (the verifier MIRRORS the lowering's shadow guard). This closes the codex fail-OPEN holes (`local p = self;
  foo(p.weaponSlot)` and `self["weaponSlot"]` rewrote 0 / verifier missed them) and the Claude/codex silent-lifecycle-miss.
  It is what makes Path A honestly GENERAL (proven fact-driven on `Hero`/`torchMount`/`TorchAnchor`) rather than
  SimpleFPS-only. Module-table `<Class>.<field>` is DEAD-LEGACY (the current `_GENERIC_RUNTIME_PROMPT` mandates OO
  `self.<field>`; the shape-4 artifact is 2026-05-02 pre-generic) — scoping it out loses nothing the current converter
  emits; a future module-table mode would fail closed, a logged boundary. Scope stays `Camera.main`-rooted child refs
  only (serialized Transform refs / non-main cameras / dynamic `GetChild(i)` / `Find()`-paths → out of scope, followups).
  The aliased-read residual (a fully-aliased read with no surviving dot-form discharges True while the alias holds the
  stale value) is a LOGGED followup, not a blocker — the real `GetRifle` does not alias.

- **D-P1-PATHA.cam_receiver [Mechanical — demotion] — `cam_receiver` / the camera-child-RHS shape-matching is DEMOTED to
  an optional Tier-2 refinement, NEVER required for success.** It is used ONLY to opportunistically pick the init-write to
  clean in Tier-2 hygiene; its absence (the AI collapsed the RHS) is NOT a failure. The rounds-5–7 cam_receiver hardening
  is no longer load-bearing and most of it is removed from S1. (The S1b ordinal-write scan likewise demotes to a best-effort
  SECONDARY DIAGNOSTIC; the S1b read-scan hardening — code-projection, long-bracket exclusion, block-balance, resolver-body
  verification — STAYS load-bearing.)

- **D-P1-PATHA.tier2 [Mechanical] — Tier-2 init-write neutralize = SKIP-on-ambiguity, decoupled from discharge.**
  Neutralize the init-write to `nil` ONLY when a unique dominating lifecycle init-write is deterministically provable; on
  ambiguity SKIP (do not neutralize, do not fail). HARMLESS because Tier-1 (the read reroute) leaves no raw read surviving,
  so the field's written value is never read — a leftover write is dead data (cosmetic AI-slop, one-line log). This
  resolves OPEN Q3 toward SKIP (both voices: codex preferred SKIP given a semantic Tier-1; Claude's "fail/count on
  ambiguity" concern is moot once the non-yielding-lifecycle read is itself a fail-closed boundary row — D-P1-PATHA.boundary
  — so the abstained read can never silently see the stale write). Discharge is fully decoupled from neutralize, so the
  BLOCKING #3 desync (D-P1-c-v2.coupling) is UNREACHABLE: discharge never touches the AI write shape or `resolved_total`.

- **D-P1-PATHA.review [Process] — dual-voice design-reviewed 3 rounds + user-approved 2026-06-13.** Path A was reviewed
  against Path B (B1 pre-rewrite token, B2 AI-instruction): B1 stays codex-vetoed (`codex-tokenvet-verdict.md` — relocates
  AI-trust fragility to C#, no precedent for preserving a synthetic host call); B2 rejected (the AI already proves it
  won't emit a deterministic binding — 5 cached shapes for one field — and the S1b verifier, shaped to the lowering's
  resolver-method emit, would fail-closed FALSE-NEGATIVE on a correct-but-natural AI binding, so B2 has no coherent
  backstop and pushes the correctness contract into non-deterministic AI output for every game). Both voices NET-recommend
  Path A as the only path that BOTH deterministically resolves the real current SimpleFPS output AND generalizes
  fact-drivenly. Slice impact: S1 SIMPLIFIED (read reroute load-bearing; neutralize Tier-2; cam_receiver optional), S1b
  RE-ANCHORED (discharge=no-raw-read + the fail-closed boundary; read-scan hardening stays), S2 now FIRES on the real
  shape (`present=True` RHS-agnostically), S3 e2e asserts the rifle seats on the rig slot on a REAL fresh conversion.

## S1 PATH-A re-anchor IMPLEMENTATION — slice rifle-dropped-ref/1.1 (2026-06-13)

- **D-S1-PATHA [Mechanical] — discharge DECOUPLED from neutralize; neutralize Tier-2 SKIP; cam_receiver
  demoted.** Modified the converged epoch-1 S1 (at `314ba85`) in place per design-phase1.md §1.2/§5 (Path A).
  - **CHANGE 1 — discharge re-anchored on the READ reroute.** `_binding_discharged(source, field, child,
    suffix)` (dropped the `anchor` param) is now `(1a)` own resolver method present + `(1b)` >=1
    `self:_resolve<suffix>(` call + `(1c)` NO raw `self.<field>` dot-form READ survives (outside Awake/Start).
    The two neutralize conditions `(2a)`/`(2b)` (own neutralize marker present + no surviving camera-child
    ordinal write) were REMOVED from the gate. So the 5 real AI write shapes (`:GetChildren()[1]`,
    `self.gameObject`, `__unityChild(...)`, multi-step local, `FindFirstChild` fallback) all discharge
    `present=True` via the read reroute — fixing the D-S2-REDESIGN abstain. PROVEN RED-vs-epoch-1: epoch-1
    (`314ba85`) returns `modified=0`/`present=False` on the live `self.weaponSlot = self.gameObject` shape;
    Path A returns `present=True` (empirically verified by loading the old module against the new test).
  - **CHANGE 2 — neutralize demoted to Tier-2 best-effort, SKIP-on-ambiguity, decoupled.**
    `_neutralize_assignment(source, field, suffix, cam_symbols)` neutralizes ONLY an unambiguous whole-RHS
    camera-child ordinal access (canonical camera literal or a recorded `cam` symbol form); on any other shape
    SKIP (no-op, never a failure, never affects `present`). The final `_luau_syntax_ok` gate still backstops a
    corrupting neutralize. The whole-RHS / same-`and`-guard checks stay (keep it from clobbering a mixed
    expression); the round-5–7 symbol-binding DOMINANCE proof was dropped (no longer load-bearing — a
    mis-neutralize can't false-discharge now).
  - **CHANGE 3 — cam_receiver / camera-RHS shape-matching demoted to optional.** `RigRootedRetargetFact.cam_receiver`
    is KEPT on the fact (still produced by the resolver) but is now consumed ONLY by the Tier-2 neutralize to
    opportunistically pick the write to clean.
  - **REMOVED code (no longer load-bearing):** `_CamAnchor` dataclass; `_camera_anchor` (replaced by the
    simpler `_camera_symbol_forms`); `_luau_symbol_is_camera` + `_binding_reaches_use` (the round-5 symbol
    camera-proof / dominance machinery); `_has_neutralized_write` + `_has_camera_child_write` (used only by the
    dropped discharge conditions 2a/2b); the `anchor` plumbing through the main loop and `_binding_discharged`.
    `_rhs_is_camera_child` simplified to take `cam_symbols: frozenset[str]` and drop the dominance proof.
    Confirmed zero external references to every removed symbol.
  - **Tests (`test_rifle_rig_retarget.py`):** ADDED `test_b_pathA_self_gameobject_shape_discharges_present_true`
    (the epoch-1-abstain case, RED-vs-epoch-1) + `test_b_neutralize_skipped_ambiguous_but_reads_rerouted_discharges`
    + `test_r5_discharge_is_rhs_agnostic_across_write_shapes` (replaced the 5 deleted dominance tests).
    REWROTE the coupled tests to the new contract: `test_h3_decoupling_present_always_equals_independent_scan`
    (was `test_h3_desync_coupling_..._stamps_false`), `test_p1_single_line_if_neutralize_skips_whole_rhs_guard_still_discharges`,
    `test_r3_fallback_validates_if_then_end_block_balance` tail. REMOVED the 5 epoch-1 r5 tests that asserted
    discharge REQUIRES the ordinal-write neutralize (`test_r5_neutralize_anchored_on_camera_receiver_not_field_ordinal`,
    `_camera_symbol_rebound_to_non_camera_in_luau_abstains`, `_mixed_disjunction_rhs_is_not_neutralized`,
    `_camera_symbol_as_param_or_loopvar_abstains`, `_camera_binding_in_closed_or_dead_scope_does_not_dominate`).
    KEPT the idempotency twice-call + yield-guard + shadowed-self + structural-balance + resolver-admission tests.
  - **VERIFY:** `tests/test_rifle_rig_retarget.py` 68 passed / 1 xfailed; `-k "resolver or corpus or rifle or
    child_ref"` 184 passed / 1 xfailed; full `-m "not slow"` 3046 passed / 45 skipped / 6 xfailed; no-Any gate
    pass. (S1b verifier `test_rig_binding_present.py` lives in the 1.2 worktree, not this slice.)

## S1 PATH-A review round 1 fix — slice rifle-dropped-ref/1.1 (2026-06-13)
- D-S1-PATHA-r1 [Mechanical] — Tier-2 SKIP-on-ambiguity: `_neutralize_assignment` now collects ALL code-position `self.<field> = ` writes and neutralizes ONLY when there is EXACTLY ONE (the unique dominating init-write is provable) AND it is the unambiguous camera-child shape; >1 same-field write → SKIP (neutralize nothing, never clobber a later legit write). Discharge UNAFFECTED (decoupled — read-reroute keys `present`), so SKIP does not change `present=True`. Single-unique-write happy path still neutralizes. New test `test_h3_neutralize_skips_on_ambiguity_two_same_field_writes` (RED vs fe6e5a0: pre-fix neutralized the camera write) + `test_h3_single_unique_camera_write_still_neutralizes`; replaced the old `test_h3_neutralize_fact_anchored_skips_unrelated_config` (encoded the pre-fix neutralize-first bug). Verify: test_rifle_rig_retarget.py 69 passed/1 xfailed; resolver|corpus|rifle|child_ref subset 185 passed/1 xfailed; no-Any pass.

## S1b PATH-A re-anchor — slice rifle-dropped-ref/1.2 (2026-06-13)
- **D-S1b-PATHA [Mechanical] — discharge re-anchored to NO-RAW-CONSUMPTION (drop the surviving-WRITE gate) + ADD the fail-closed boundary.** `_rig_binding_discharged(source, field, child)` was (1a) own rig-resolver body present + (1b) >=1 `self:_resolve<suffix>(` call + (1c) no surviving bare dot-form `self.<field>` READ (lifecycle/shadowed EXEMPTED) AND (2) no surviving `self.<field> = <...GetChild(n)|GetChildren()[n]...>` ordinal WRITE. **Path A drops clause (2) from the gate** — a surviving init-WRITE no longer fails discharge (it is dead data once reads are rerouted), so all 5 real AI write shapes (`:GetChildren()[1]`, `self.gameObject`, `__unityChild(...)`, multi-step local, `FindFirstChild` fallback) discharge `present=True` via the read reroute (fixes the D-S2-REDESIGN abstain). `_rig_has_surviving_ordinal_write` + its round-1/2/3 hardening (code-projection, postfix-continuation, whitespace/comment tolerance) is KEPT as a best-effort SECONDARY DIAGNOSTIC scan only (no longer a PASS/FAIL gate).
- **What CHANGED vs the surviving-write gate:** clause (1c)+(boundary) merged into a single load-bearing gate `_rig_has_surviving_field_consumption(source, field)` (replaces `_rig_has_surviving_field_read`). It now FAILS CLOSED on every form the dot-form read-reroute cannot safely rewrite — turning the would-be fail-OPEN holes into loud rows: (i) NON-`self` receiver read `<Class>.<field>` / `owner.<field>` / receiver-alias `p.<field>` (`_rig_nonself_read_re`, recv != "self"); (ii) bracket-index `self["<field>"]` (`_rig_bracket_read_re`); (iii) a raw `self.<field>` READ in a NON-yielding lifecycle method (`Awake`/`Start`) — the verifier does NOT inherit the lowering's lifecycle exemption (Path A: the init-write is only Tier-2-best-effort-neutralized, so a lifecycle read can cache stale state); (iv) a shadowed-`self` read — still textually `self.<field>`, so the dot-read scan (a) catches it (fails closed) without a separate shadow walk.
- **KEPT (reused unchanged):** the INDEPENDENCE discipline (scan `script.source`, anchored on deterministic `field`/`child`; `present` cross-check; stamp/scan disagreement fails; `rig_binding=None` abstains), the code-projection (`_rig_code_projection`), long-bracket exclusion, block-balance method-span (`_rig_method_body_end` with the round-4 elseif fix), resolver-body verification (`_rig_resolver_body_is_rig_lookup`), `FAIL_CLOSED_CHECKS` membership, strict no-`Any`, generic (no `weaponSlot`/`WeaponSlot` hardcode — keyed on the carrier's `field`/`child`).
- **Tests (`test_rig_binding_present.py`):** the 9 round-1/2/3 surviving-ordinal-WRITE tests REWRITTEN to the Path A contract — the write scan still DETECTS (`_rig_has_surviving_ordinal_write` is True, secondary diagnostic kept) but discharge is now True (dead write does not block). ADDED: `test_pathA_discharges_on_all_five_real_write_shapes` (parametrized × 5), `test_pathA_tier2_skipped_write_but_reads_rerouted_discharges_true`, the 4 boundary RED tests `test_e_boundary_{i_nonself_receiver_owner,i_receiver_alias,i_module_table,ii_bracket_index,iii_lifecycle_read,iv_shadowed_self_read}_fails_closed`, the generic `test_g_generic_different_game_torchmount_discharges_green`, and the narrator-INDEPENDENT pre-fix-RED proof `test_e_boundary_pre_fix_red_proof_against_real_5da0eab_blob` (loads the actual 5da0eab `contract_verifier.py` blob from git; non-self/bracket/lifecycle false-PASS there, fail closed under Path A). Shadowed-self proven a REGRESSION GUARD (`test_e_boundary_iv_shadowed_is_regression_guard_red_both_ways` — failed closed already at 5da0eab as a textual `self.<field>` dot read), NOT a newly-caught false-pass.
- **VERIFY:** `tests/test_rig_binding_present.py` 50 passed; `-k "contract or verifier or corpus or rifle or rig"` 537 passed / 1 xfailed (pre-existing S1 residual); full `-m "not slow"` 3097 passed / 45 skipped / 6 xfailed; no-Any gate clean.

## S1b PATH-A review round 1 fix — slice rifle-dropped-ref/1.2 (2026-06-13)
- **D-S1b-PATHA-r1 [Mechanical] — receiver-AGNOSTIC surviving-field-access-read boundary REPLACES the receiver-shape blacklist.** Round-1 dual-voice BLOCKING: the boundary gate `_rig_has_surviving_field_consumption` was a BLACKLIST (`_rig_nonself_read_re` = single-token `<ident>.field`; `_rig_bracket_read_re` = bare `self["field"]`), so EXOTIC receivers EVADED it and false-passed an undischarged source (fail-OPEN): `(owner).field`, `getOwner().field`, `owners[1].field`, `other.self.field`, `(self)["field"]`, plus the whitespace/newline-dot `self .field` / `self.\nfield` and computed bracket keys. Replaced with ONE receiver-agnostic whitelist: discharge fails on ANY surviving code-position field-access READ of the `<field>` token — the token preceded (modulo whitespace/comments, via the code projection) by a `.` (dot member) OR appearing as a string key inside `[ "..." ]` / `[ '...' ]` OR a computed/concatenated key reconstructing the field (`_rig_has_computed_field_key`) — REGARDLESS of receiver, with exactly two known-good exceptions: (1) an assignment LHS (`_rig_pos_is_assignment_lhs` — a Tier-2-skipped WRITE may survive; discharge decoupled from neutralize) and (2) the injected resolver's own internals (`_<field>Cache` memo + any position inside the `_resolve<child-suffix>` method body, via `_rig_is_resolver_internal_access`). The RECEIVER is ignored entirely — closes the whole receiver-form class with no enumeration. New helpers: `_rig_field_token_re` (word-bounded `\b<field>\b`), `_rig_string_key_read_re`, `_rig_has_computed_field_key`, `_rig_pos_is_assignment_lhs`, `_rig_is_resolver_internal_access`. DROPPED: `_rig_nonself_read_re`, `_rig_bracket_read_re`. `_rig_has_surviving_field_consumption(source, field, child)` gained `child` (to key the resolver-body exception on the child-derived suffix the lowering emits).
- **KEPT:** independence discipline, `_rig_code_projection` / `_rig_pos_is_real_code` / long-bracket / `_rig_method_body_end` block-balance, `_rig_resolver_body_is_rig_lookup`, the secondary-diagnostic `_rig_has_surviving_ordinal_write`, `FAIL_CLOSED_CHECKS`, strict no-`Any`, generic.
- **Happy path preserved (verified end-to-end on the REAL lowering):** the corpus `self.weaponSlot` shape, all 5 RHS write shapes, the Tier-2-skipped write, and the generic `torchMount`/`TorchAnchor` case still discharge `present=True` (after the reroute there is NO surviving `.field` READ — only `_weaponSlotCache` + the `_resolve<suffix>` body + possibly a skipped write LHS). Write-LHS exception proven receiver-agnostic (`owner.weaponSlot = 5` and `self["weaponSlot"] = x` both still discharge True).
- **Tests:** ADDED `test_pathA_r1_exotic_receiver_read_fails_closed` (parametrized × 8: the 5 exotic receivers + whitespace-dot + newline-dot + computed-bracket-key — each → discharge False + a loud row), the narrator-INDEPENDENT pre-fix-RED proof `test_pathA_r1_exotic_receivers_red_against_7b59488` (loads the ACTUAL 7b59488 blob from git; all 8 forms false-PASS there = discharged True, fail closed now), and the two write-LHS exception tests (`_nonself_write_lhs`, `_bracket_write_lhs`). KEPT all 50 prior tests.
- **VERIFY:** `tests/test_rig_binding_present.py` 61 passed; `-k "contract or verifier or corpus or rifle or rig"` 548 passed / 1 xfailed (pre-existing S1 residual); no-Any gate clean. New sha `56691bb`.
D-S1b-PATHA-r2: drop over-broad resolver-body-span access exemption; keep `_<field>Cache` only (decoy `_resolveWeaponSlot` body could hide a foreign `.<field>` read → fail-OPEN). RED-against-56691bb test added.
- D-S1b-PATHA-r3: computed-key = exact constant-fold + LHS-exempt + no dynamic-key false-fail (`_rig_has_computed_field_key` flags a concatenated bracket key ONLY when string-literals-only fold to EXACTLY `<field>`, honors the assignment-LHS exception, and never flags a non-foldable dynamic key; exact-fold READ still fails closed).
- D-S1b-PATHA-r4: full Luau string-literal DECODE for bracket keys + multi-assignment LHS.
  - FIX 1 [BLOCKING] — bracket-key detection was RAW-TEXT-ONLY, so encoded string-literal keys that resolve to the field bypassed the fail-closed gate (fail-OPEN): `self[[weaponSlot]]` / `self[=[weaponSlot]=]` (long-bracket keys), `self["wea\x70onSlot"]` (hex), `self["wea\x70on".."Slot"]` (escape+concat) all DISCHARGED True at d51ae90 despite being surviving READs. Fix: new `_rig_decode_luau_string_key(expr) -> str | None` decodes the FINITE Luau string-literal grammar — short strings with full escape processing (`\xHH`, `\ddd`, `\u{...}`, `\n`/`\t`/… , `\z`, line continuations), long-bracket strings `[[…]]`/`[=[…]=]` (RAW, first-newline stripped), and `..` concatenations of these — then a UNIFIED `_rig_has_decoded_field_bracket_read` finds every code-position `[ … ]` access (`_rig_bracket_key_spans`/`_rig_find_bracket_close`, string/long-bracket/comment aware), decodes its key, and flags iff it equals `<field>` EXACTLY (write-LHS exempt). Subsumes + removes the old clean-literal matcher (`_rig_string_key_read_re`) AND the old computed-key folder (`_rig_constant_folds_to`/`_rig_has_computed_field_key`). A dynamic/non-static key decodes to None → never flagged (no false-fail).
  - FIX 2 [MINOR] — write-LHS exception missed MULTI-ASSIGNMENT: `self.weaponSlot, other = …` / `self["weapon".."Slot"], other = …` have a `,` before the `=` → false-failed as READs. `_rig_pos_is_assignment_lhs` now also recognizes a multi-target list (`,`…bare-`=`, at bracket depth 0, not `==`) as an LHS WRITE.
  - No regression: dot-form receiver-agnostic reads (8 exotic), literal `self["weaponSlot"]` read, decoy resolver body, `_<field>Cache` exemption, the 5 RHS write shapes + generic `torchMount`, resolver-body floor, independence/None-abstain — all intact (80 rig tests pass).
  - Tests ADDED: `test_pathA_r4_encoded_key_read_fails_closed` (×4 encoded reads → fail closed), `_encoded_key_write_lhs_still_discharges`, `_unrelated_decoded_key_does_not_fail` (×2 — `someOther`, `preweaponSlotpost` substring), `_dynamic_bracket_key_does_not_false_fail`, `_multi_assignment_write_lhs_discharges` (×2 — dot + concat), and the narrator-INDEPENDENT `_encoded_reads_and_multi_writes_red_against_d51ae90` (loads the ACTUAL d51ae90 blob: encoded reads false-PASS there / fail closed now; multi-assignment writes false-FAIL there / discharge True now — both directions).
  - VERIFY: `tests/test_rig_binding_present.py` 80 passed; `-k "contract or verifier or corpus or rifle or rig"` 567 passed / 1 xfailed (pre-existing S1 residual); no-Any gate clean.

## S2 PATH-A fixture regen — slice rifle-dropped-ref/1.3 (2026-06-13) — DONE (resolves D-S2-REDESIGN)
- **D-S2-PATHA [Mechanical] — the regen now FIRES on the real shape; Player `rig_binding present=True` captured.** Re-ran `tools/regen_contract_corpus.py SimpleFPS` on the REAL Path A pipeline (S1 read-reroute + S1b no-raw-read discharge, rebased into the 1.3 worktree at `68f944b`; the `_FIELDS += rig_binding` edit was already in place). Cache approach: copied the main checkout's `.cache/llm` to `/tmp/warm_cache_s2_pathA/llm` and TTL-refreshed every entry's `cached_at` to now (231 entries) so the live Player key deterministically hits `28d60ee6` (the `self.weaponSlot = self.gameObject` degraded shape — the exact D-S2-REDESIGN blocker). `CONTRACT_CORPUS_PROJECTS_ROOT`=main `test_projects/SimpleFPS`. (A handful of un-warmed water/explosion effect scripts re-transpiled live via the local `claude` CLI — not gameplay-load-bearing; Player hit the cache.)
- **The Path A read-reroute DISCHARGED on the real `self.gameObject` shape** — exactly the abstain that D-S2-REDESIGN reported under the old write-anchored lowering. Captured Player: the consumer read `local slot = self.weaponSlot or self.gameObject` became `local slot = self:_resolveWeaponSlot() or self.gameObject`; the injected `function Player:_resolveWeaponSlot()` carries the `_MainCameraRig` scan + `_weaponSlotCache` memo; the init-write `self.weaponSlot = self.gameObject` survives as dead Tier-2 data (discharge keys on no-surviving-READ, RHS-agnostic). This is the proof the lowering anchored on the AI-STABLE member access, not the AI-VOLATILE write shape.
- **Captured fixture (`tests/fixtures/contract_corpus/SimpleFPS/fixture.json`, 41 scripts):** Player `child_ref_resolution={getchild_total:1,resolved_total:1}` + `rig_binding={field:"weaponSlot",child:"WeaponSlot",present:True}`; Turret UNCHANGED `{3,3}` + `rig_binding=None` (no rig fact → abstains). `expected_counts={consumer_compliance:1}` (info-only). Independently re-verified the verdict: **0 warning violations**, no `rig_binding_present` row (the `FAIL_CLOSED_CHECKS` flip exercised + discharged green), no `child_ordinal_coverage_gap` row (check D no row, resolved). The fixture is a REAL pipeline capture, not hand-edited.
- **VERIFY:** `tests/test_contract_corpus.py` 9 passed; `-k "corpus or contract or verifier or rifle or rig"` 567 passed / 1 xfailed (pre-existing S1 residual); `check_no_any.sh` pass. Committed to the slice branch as `c0c458e` (fixture only; `_FIELDS`/regen tool/`test_contract_corpus.py` needed no change).

## S2 PATH-A round-1 review fixes — slice rifle-dropped-ref/1.3 (2026-06-13) — DONE (2 MAJOR)
- **D-S2-PATHA-r1 [Mechanical] — committed fixture is now a Player-ONLY diff vs the pre-S2 baseline (d129b6a).** FINDING 1 (MAJOR claude): the prior c0c458e regen captured the WHOLE corpus fresh — 31 non-Player scripts' `source` drifted (non-deterministic transpile on un-warmed effect-script cache entries), shifting `coverage.getcomponent_sites` 23→20. FIX (option b): reconstructed the fixture as the d129b6a baseline with ONLY the Player entry updated to its REAL Path-A capture (source with the `self:_resolveWeaponSlot()` reroute + injected `function Player:_resolveWeaponSlot()` resolver + `_MainCameraRig` scan/`_weaponSlotCache` memo; `child_ref_resolution={1,1}`; `rig_binding={field:weaponSlot,child:WeaponSlot,present:true}`), `rig_binding=null` added to all other scripts (the intended new `_FIELDS` field, not drift). `expected_counts` + `coverage` were RE-DERIVED through the real `verify_contract` + `regen._coverage` (not hand-edited). Result: `getcomponent_sites=22` (the Player-only 2→1 delta from baseline 23, NOT the noisy 20), `expected_counts={consumer_compliance:1}` (the `child_ordinal_coverage_gap` legitimately drops because Player resolved {1,1}). `git diff d129b6a` now shows source/child_ref_resolution/non-null-rig_binding changes on Player ONLY; Turret unchanged `{3,3}`/null.
- **D-S2-PATHA-r1.vacuity [Mechanical] — corpus anti-vacuity gate now asserts the `rig_binding_present` exercise.** FINDING 2 (MAJOR codex): `test_contract_corpus.py` proved A/B/C coverage but never asserted a non-None `rig_binding`, so a future regen dropping the carrier would let `rig_binding_present` abstain green (vacuous). FIX: added `test_corpus_exercises_rig_binding_present_check` — asserts ≥1 corpus script carries `rig_binding present=True` AND (discharge-is-real) that its captured source contains the injected `function <Class>:_resolve<Child>(` resolver method + a rerouted `self:_resolve<Child>()` consumer read. Generic: derives the method/child names from the carrier itself (no weaponSlot/WeaponSlot hardcode). PROVEN to FAIL when the Player carrier is nulled (tier-1: "unexercised") and when the carrier is present but the resolver is stripped from source (tier-2: "carrier-present-but-undischarged"); restored fixture green.
- **VERIFY:** `tests/test_contract_corpus.py` 10 passed; `-k "corpus or contract or verifier or rifle or rig"` 568 passed / 1 xfailed (pre-existing S1 residual); `check_no_any.sh` pass. No game-specific hardcode in the regen tool (untouched). Committed to the slice branch (fixture + test only).

## S3 NET/E2E review fixes — slice rifle-dropped-ref/1.4 (2026-06-13) — DONE (2 BLOCKING + 2 MAJOR + 1 MINOR)
- **D-S3-r1.race [Mechanical] — the race witness now drives the REAL REWRITTEN CONSUMER, not the resolver in isolation.** P1 BLOCKING (both voices): the harness extracted only `Player:_resolveWeaponSlot()` and called it directly, so a regression that failed to reroute `GetRifle`'s `self.<field>` reads to `self:_resolveWeaponSlot()` still passed. FIX: `_lowered_resolver_and_consumer()` extracts BOTH the emitted resolver AND the REWRITTEN `GetRifle` (verbatim from the live lowering) and the luau harness now seats the rifle THROUGH the consumer's own rerouted reads (real `self.host.instantiatePrefab`/`pivotOf`/`PivotTo` stubs). Non-tautology PROVEN by `test_race_non_rerouted_consumer_fails_the_harness` (a hand-built raw-`self.weaponSlot` consumer reads nil → rifle never seats → returncode!=0).
- **D-S3-r2.loud [Mechanical] — boundary "fails-loud" now asserts the REAL pipeline surface (`ctx.errors` + `success`), not a recomputed value.** P1 BLOCKING (both voices): `_run_pipeline_gate` recomputed `fail_closed_errors(verify_contract(...))` out-of-band, so a hook that stopped promoting rows to `ctx.errors` still passed. FIX: the gate now returns `PipelineGate{errors, success, rows}` read OFF `pipeline.ctx.errors` (filtered by `CONTRACT_ERROR_PREFIX`) and `success = len(pipeline.ctx.errors)==0` (pipeline.py:4340) — the real promotion the hook performs. Applied to BOTH boundary tests. PROVEN non-tautological by a mutation: monkeypatching the hook to skip the `ctx.errors.extend(promotable)` promotion makes both boundary tests FAIL.
- **D-S3-r3.field [Mechanical] — `field` is now exercised parametrically (was hardcoded `weaponSlot`).** P1 MAJOR (codex): only `child` was behaviorally exercised; a field-name-hardcoding regression passed. FIX: a generic `gunSlot`/`GunMount` pair (distinct from SimpleFPS names) runs the SAME producer→lowering→verifier chain in `test_boundary_generic_field_factored_out_fails_closed_end_to_end` (RED, promoted error must contain THIS field+child) and `test_boundary_generic_discharged_binding_keys_on_field` (GREEN), plus the generic race variant. A field hardcode anywhere breaks one of the two (false RED or false PASS).
- **D-S3-r4.producer [Mechanical] — the real resolver chain is now exercised in the fast path (was hand-fabricated facts).** P1 MAJOR (codex): all fast helpers fabricated `RigRootedRetargetFact`/`ChildRefScript` directly. FIX: `_resolve_real_rig_fact()` drives the real `build_child_ref_map` → `_resolve_script` → `_resolve_rig_facts` over a real `.cs` file + parsed `PrefabLibrary`/`GuidIndex` (MainCamera-tagged node), and the two generic boundary tests lower using the RESOLVER-produced `ChildRefScript` (not a hand-built fact). The race/single-shape boundary helpers keep hand-built facts (acceptable — the producer END is now covered by the generic tests).
- **D-S3-r5.typing [Mechanical] — strict-typing gaps closed.** P1 MINOR: parameterized `subprocess.CompletedProcess[str]`; replaced the implicit-Any `_load_fixture_player() -> tuple[dict, dict]` with a `_PlayerFixture` TypedDict; the pipeline gate returns a `PipelineGate` TypedDict. `tools/check_no_any.sh` green.
- **VERIFY:** `tests/test_rifle_rig_retarget_e2e.py` 13 passed / 1 slow-skip; chain `test_rifle_rig_retarget.py`+`test_rig_binding_present.py`+`test_contract_corpus.py` 159 passed / 1 xfailed; full fast suite 3141 passed / 45 skipped / 6 xfailed; `check_no_any.sh` pass. Committed to the slice branch (owned file only).

---

## Slice 1.2 — PHASE-INTEGRATION P1 fix: check D false positive on discharged-rig dead init-write

**Classification:** Bug fix (fail-closed FALSE POSITIVE in a ship gate) — net-positive, scoped to slice-owned files only.

**The P1:** `_check_surviving_child_ordinal` (check D) fired `child_ordinal_survivor` on the dead positional init-write ordinal (`self.weaponSlot = self.cam:GetChildren()[1]`) that survives when Path A's Tier-2 neutralize SKIPS the single-line-if write shape (one of the 5 real AI shapes; blessed by `test_rifle_rig_retarget.py:766`). The binding is fully discharged via the READ reroute and `_check_rig_binding_present` is GREEN, but the rig fact bumped `resolved_total` to 1 against `getchild_total=1` -> 0 unresolved-site budget, so the surviving dead write (count 1) exceeded the budget and fired the warning -> a correctly-converted game blocked at the fail-closed ship gate. Confirmed on REAL lowered output via `$RUN_DIR/repro_checkD_falsepos.py`.

**The fix (rig-aware exemption, precise):**
- New count helper `_rig_discharged_ordinal_write_exempt_count(source, field)` counts surviving `self.<field> = <recv>:GetChildren()[n]` write-LHS ordinal SITES — restricted to the EXACT `GetChildren()[n]` adjacent shape check D counts (new `_RIG_GETCHILDREN_WRITE_TAIL_RE`; the `GetChild(n)` form that `_RIG_ORDINAL_WRITE_TAIL_RE` also tolerates is NOT counted by check D, so it is excluded here to never over-subtract). Reuses the proven `_rig_code_projection` / `_rig_statement_rhs_end` / `_rig_collapse_code_ws` machinery the discharge scan uses.
- In `_check_surviving_child_ordinal`, AFTER computing `survivors`, when the script's `rig_binding` is PRESENT (stamp `present is True`) AND `_rig_binding_discharged(source, field, child)` is True, subtract the exempt write count. A READ survivor, a non-/un-discharged-binding script, a factored chain, or any survivor beyond the dead write is NOT exempt and STILL fires. The dead write is "resolved-but-left-behind" (the rig fact already bumped resolved_total), not "unresolved" — the exemption reflects that.
- Gated on `_rig_binding_discharged` (the load-bearing independent authority), so it agrees with the discharge gate's own write-LHS-exempt logic.

**Generic:** keys on the carrier's `field`/`child`, no `weaponSlot`/`WeaponSlot` literals as load-bearing logic. Strict typing (no Any; gate green).

**Tests (`test_rig_binding_present.py`):** the row-only test at :423 never ran the full verifier interaction (the coverage gap codex flagged). Added:
- `test_checkD_no_false_positive_on_discharged_rig_dead_write` — drives REAL `lower_rifle_rig_retarget` on the skipped-neutralize single-line-if shape with REAL {getchild_total:1, resolved_total:1}, runs FULL `verify_contract` + check D directly, asserts present=True, NO `rig_binding_present`, NO `child_ordinal_survivor`.
- `test_checkD_pre_fix_red_proof` — rebuilds the d51ae90 pre-fix verifier from git, proves ITS check D FIRES on this exact input (not a tautology).
- Three precision guards: READ survivor + no rig STILL fires; READ survivor beyond the exempt dead write WITH a discharged rig STILL fires (exemption subtracts exactly one); UNDISCHARGED rig gets NO exemption.

**Verification:** targeted suites (test_rig_binding_present + test_rifle_rig_retarget + test_contract_corpus) green; full fast suite 3132 passed / 45 skipped / 6 xfailed; `tools/check_no_any.sh` pass. (`test_rifle_rig_retarget_e2e.py` named in the directive does not exist in this worktree — not a slice-1.2 file.)

## Slice 1.2 — round-6 P1 fix (site-aligned rig exemption)

**Classification:** BUGFIX (close fail-closed-gate bypass via structural refactor).

The round-5 rig exemption subtracted a SEPARATELY-computed count
(`_rig_discharged_ordinal_write_exempt_count` via `_RIG_GETCHILDREN_WRITE_TAIL_RE`)
from check D's survivor count. That counter applied NEITHER check D's engine-global
receiver filter NOR its receiver-shape constraint, and matched EVERY same-field
`GetChildren()[n]` write — so it could subtract a number larger than check D actually
counted, silently swallowing a SEPARATE real `child_ordinal_survivor` on the same
script (both adversarial voices confirmed end-to-end at 3a4231a).

**Fix (structural, NOT another divergent regex):** the exemption is now applied
SITE-ALIGNED *inside* `_count_surviving_child_ordinals(source, exempt_field=...)`.
During the SAME `_GETCHILDREN_INDEX_ANY_RE` walk, AFTER the identical
`_luau_pos_is_code` + `_receiver_roots_at_engine_global` filters that COUNT a site,
AT MOST ONE site whose match position lands inside a `self.<field> =` write-LHS RHS
span (`_rig_field_write_rhs_spans`) is skipped. There is no separately-computed
number to subtract, so `exempt ⊆ counted-survivors` holds structurally — a site can
only be skipped if it would otherwise have been counted; the `exempted < 1` cap
bounds it to the single dead init-write. Dead `_rig_discharged_ordinal_write_exempt_count`
+ `_RIG_GETCHILDREN_WRITE_TAIL_RE` removed. Gating (`present` stamp AND independent
`_rig_binding_discharged`) unchanged. Round-5 diagnostic
`_rig_has_surviving_ordinal_write` kept (still used by tests; not the gate).

**Tests added (real helpers/lowering, RED against 3a4231a):** engine-global dead
write + separate survivor still fires; bracket-indexed-receiver variant; second
same-field write still fires (only one exempt); round-6 pre-fix RED proof loading
the 3a4231a blob. Kept: original single-survivor false-positive guard stays GREEN.

**Verify:** targeted + full fast suite green (3136 passed); no-Any gate pass;
`repro_checkD_falsepos.py` still prints `cd=False` (false positive gone).

## Slice 1.2 — round-7 P1 fix (statement-anchored rig exemption)

**Classification:** BUGFIX (close two fail-closed-gate masking bypasses via a structural
re-anchor of the exemption's site identity — last fix round before the hard cap).

Round-7 (both voices) found the round-6 site-aligned exemption still identified the dead
init-write site by FORWARD TEXT-MATCHING from a loose `self.<field> =` substring
(`_rig_field_write_rhs_spans`), re-opening the masking class through two mechanisms:
(a) the RHS span ended at a depth-0 NEWLINE but not a Luau `;`, so `self.weaponSlot = nil ;
local stray = foo:GetChildren()[1]` swallowed the genuine survivor (Claude); (b) the
`self.<field> =` match was a SUBSTRING, so `myself.weaponSlot = ...` / `other.self.weaponSlot
= ...` were taken as the exempt write (codex). Same root: forward span from a loose LHS.

**Fix (structural inversion — exact statement-level identity, no divergent text-matcher):**
the exemption is now decided PER COUNTED SITE by parsing the single Luau statement that
PHYSICALLY CONTAINS that site. `_rig_statement_bounds` scans the position-preserving
`_rig_code_projection` outward from the site to its statement boundaries — backward to a
depth-0 `;` / non-continuation newline / un-matched bracket / block-opener keyword
(`then`/`do`/`else`/`repeat`), forward to a depth-0 `;` / non-continuation newline /
block-closer keyword (`end`/`else`/`elseif`/`until`), bracket-depth tracked, over the
string/comment-blanked projection. `_site_is_discharged_rig_dead_write` exempts ONLY if
that statement (1) OPENS with the STANDALONE lvalue `self.<field>` then a REAL `=`
(`_rig_exempt_lhs_re`: char-before-`self` not ident/`.` rejects `myself`/`a.self`; field
`(?!\w)` rejects the `weaponSlot`/`weaponSlotBackup` prefix collision; `(?<![<>~=])=(?!=)`
rejects `==`/`<=`/`>=`/`~=`), AND (2) the GetChildren site is the WHOLE RHS — receiver flush
at the RHS start (only whitespace before) and only whitespace to statement end (only
whitespace after). So an arbitrary RHS that merely CONTAINS a GetChildren after/within an
operand (`= nil or foo:Get...[1]`, `= a[1] + b[2]`) is NOT the dead write and is counted.
Removed the superseded `_rig_field_write_rhs_spans` + `_pos_in_any_span` (no divergent
text-matcher left to rot). Gating (`present` stamp AND independent `_rig_binding_discharged`)
and the `< 1` cap unchanged. **Ambiguous-site fallback:** any statement that fails either
gate (incl. an un-parseable shape, e.g. a one-line no-separator `... end`-bounded multi-stmt
or a paren-wrapped RHS) is NOT exempted — it is COUNTED (fail closed; a false-positive is
safe, a silent mask is not). Every uncertain case biases toward counting.

**Dual-voice on a NEW codex finding (re-review of corrected artifact) — CORRECT BY DESIGN,
no fix:** codex flagged that a discharged field's exempt shape in an UNRELATED method
(`Rifle:DebugRefresh(holder) self.weaponSlot = holder:GetChildren()[1] end`) is exempted.
The design-focused second voice confirmed this is correct by design: `_rig_binding_discharged`
guarantees NO raw `self.<field>` READ survives ANYWHERE, so every `self.<field> = ...` write
is dead data whose value nothing observes (consumers read `self._<field>Cache` via the
resolver). The design doc (design-phase1.md:194-197,427,436) explicitly treats a surviving
init-write as "dead data — harmless" and multiple same-field writes as the SKIP-on-ambiguity
case. Counting it would be a FALSE POSITIVE blocking a valid conversion; the `< 1` cap keeps
a SECOND same-field write firing. Not a slice-1.2 regression; design is the authority.

**Tests (`test_rig_binding_present.py`, real helpers/lowering):** `;`-separated survivor
still fires; `myself.`/`other.self.` substring-LHS still fire; `weaponSlot` vs
`weaponSlotBackup` word-boundary both directions; comparison `==`/`<=`/`>=`/`~=` not
exempted; two dead writes → only one exempt; codex RHS-identity guards (leading operand
`nil or ...` and multi-operand `a[1]+b[2]` both counted; bare dead-write shapes
simple/method/`if-then`/multiline still exempt); round-7 pre-fix RED proof loading the
de8e7f9 blob (proves it MASKED both bypasses). Kept GREEN: the real false-positive case +
SimpleFPS corpus.

**Verify:** targeted suites (rig_binding_present + rifle_rig_retarget + contract_corpus) 178
passed; full fast suite 3147 passed / 45 skipped / 6 xfailed; no-Any gate pass;
`repro_checkD_falsepos.py` prints `cd=False` (false positive gone).

---

## Slice 1.1 (S1) — REDESIGN r3 delta: carrier gains cam_receiver + cam_ordinal (amends bae88c5)

**What:** Promoted the resolver fact's deterministic identity into the `rig_binding` carrier so
slice 1.2's check-D dead-write exemption can anchor on the EXACT credited site (deterministic
upstream, not an AI-output fingerprint).

1. **`RigRootedRetargetFact.ordinal: int`** — new field on the dataclass
   (`child_ref_resolver.py`), populated from the in-hand `int(m.group(3))` at the single rig-fact
   construction site (the design's :838/:876/:900 refs are the matcher offsets; there is exactly
   ONE `RigRootedRetargetFact(...)` construction). `cam_receiver` was already present.
2. **Carrier `cam_receiver` + `cam_ordinal`** — stamped from the fact on EVERY rig-fact-bearing
   carrier (discharged `present=True`, abstained `present=False`, and the invalid-ident
   `present=False`). Deterministic projections, stamped regardless of discharge / write shape.
3. **multi-fact carrier → FULL 5-key** (`rifle_rig_retarget_lowering.py`): the `len(rig_facts)>1`
   abstain carrier now stamps `{field,child,cam_receiver,cam_ordinal}` from the FIRST fact +
   `present:False`+`multi_fact:True`, so it round-trips the 5-key LOAD validator and fails LOUD on
   the resume path (a 2-key carrier would have been dropped → silent abstain).
4. **Threading** — the `TranspiledScript→RbxScript` copy (`pipeline.py:2906`) copies the whole
   dict, so the new keys ride along (verified). SAVE/RESTORE carry the full dict. **LOAD widened**:
   `_load_rig_binding_for_rehydration` now reads+validates ALL FIVE keys
   (`field:str, child:str, present:bool, cam_receiver:str, cam_ordinal:int`, with the
   `bool`-is-not-`int` guard on cam_ordinal); a row missing/malformed on ANY key → dropped to
   `None` (verifier abstains, the safe default) — NEVER a partial carrier. The optional
   `multi_fact` flag is preserved when present. Docstrings updated on `TranspiledScript.rig_binding`
   and `RbxScript.rig_binding`.
5. **Tests** — updated all existing carrier-equality assertions to the 5-key shape; added 8 r3
   tests: fact carries ordinal (non-zero GetChild(1)); carrier stamps non-default
   cam_receiver/cam_ordinal from the fact; keys are RHS-agnostic across 3 write shapes; LOAD
   round-trips all 5 keys; a `present:False` 5-key (multi_fact) carrier survives rehydrate to fire
   loud; the pre-fix 3-key partial row is dropped to None; malformed cam_ordinal (str/bool/None/
   float) and non-str cam_receiver each drop the row.

**Verify:** `test_rifle_rig_retarget.py` 77 passed / 1 xfailed; full fast suite 3055 passed /
45 skipped / 6 xfailed; no-Any gate pass. The new keys thread through the pipeline-copy AND the
widened LOAD validator (both proven by tests).

**Scope note:** check D's exemption itself + `test_rig_binding_present.py` are slice 1.2's; this
slice only PROVIDES the anchor. 1.2's tests stayed green (they do not yet read the new keys).

---

## Slice 1.2 (S1b) — REDESIGN r3 rework: positively-anchored check-D dead-write exemption

**Goal:** rework check D's rig-aware exemption from the round-8 receiver-UNCONSTRAINED
statement-anchored skip (codex found it over-exempts a same-field write through a DIFFERENT
receiver and at a DIFFERENT ordinal) to a TIGHT POSITIVE match of the EXACT credited site,
anchored on the carrier's deterministic `cam_receiver` + `cam_ordinal` (slice 1.1 now provides
them). AMEND of the existing binding-present verifier + statement-anchored machinery — both KEPT.

**Decisions:**
1. **Exemption spec** — added `_RigDeadWriteExempt(field_name, cam_receiver, cam_ordinal)` (frozen
   dataclass). `_count_surviving_child_ordinals(source, exempt: _RigDeadWriteExempt | None)` replaces
   the bare `exempt_field: str | None`. The per-site decision passes the matched receiver
   (`m.group(1)`) + ordinal (`int(m.group(2)`) into the predicate — no re-parse.
2. **`_site_is_discharged_rig_dead_write`** gains `site_receiver`, `site_ordinal`, `cam_receiver`,
   `cam_ordinal`. The r7 statement identity (whole-RHS `self.<field> = <site>`) is RETAINED and
   ANDed with two NEW anchors: (receiver) `site_receiver.strip() == f"self.{cam_receiver}"` — the
   dot-form member ONLY, never a bare local; (ordinal) `site_ordinal == cam_ordinal + 1` (0-based
   GetChild → 1-based GetChildren). Empty `field`/`cam_receiver` → False (fail closed).
3. **Direct no-seed form is a SAFE false-positive** — `cam_receiver=="Camera.main.transform"` forms
   no valid `self.<member>` (the resolver never emits `self.Camera.main.transform`), so the rig's own
   write COUNTS (fail-closed, never a silent mask). Verified by test (vii).
4. **Entry-point gate (`_check_surviving_child_ordinal`)** builds the spec from the carrier ONLY when
   `field` AND `child` AND `cam_receiver` (non-empty) AND `cam_ordinal` is a real `int` (bool
   excluded) AND `present is True` AND the INDEPENDENT `_rig_binding_discharged` re-derivation holds.
   A 3-key (pre-fix / dropped-keys) carrier → no `cam_receiver` → NO exemption → fail closed.
   At-most-one + site-aligned (`exempt ⊆ counted-survivors`) preserved.

**How r3 closes the bypasses:** the receiver anchor closes codex r8 (a `self.muzzle:GetChildren()[2]`
write no longer matches `self.cam`); the ordinal anchor closes codex r3 (`self.cam:GetChildren()[2]`
≠ credited `[1]`); the bare-`cam` local is rejected (review MAJOR #3); the r7 `;`-span /
substring-LHS protections are RETAINED (ANDed, not replaced). Every receiver/ordinal/form mismatch
biases to COUNT.

**Tests:** all existing carrier-equality assertions updated to the 5-key shape via a `_carrier(...)`
helper; the r6/r7 statement-anchored unit tests updated to pass `_RigDeadWriteExempt` (receiver
`self.cam`, ordinal 0) so they isolate their original discriminator; added §4 (f-r3) (i)-(x) driving
the REAL helpers/`verify_contract` — exempt the credited write, STILL fire on different-receiver
(r8) / different-ordinal (r3) / bare-receiver / second-write / READ / non-rig / undischarged, SAFE
false-positive on the direct form, statement-anchored retained, a threading proof (rehydrated 5-key
carrier anchors; dropped-keys carrier does not exempt), and a pre-fix RED proof against the actual
91a19d4 receiver-blind r8 blob (masks ii+iii; r3 does not).

**Verify:** `test_rig_binding_present.py` + `test_rifle_rig_retarget.py` 189 passed / 1 xfailed; full
fast suite 3167 passed / 45 skipped / 6 xfailed; no-Any gate pass.

---

## Slice 1.2 (S1b) — check-D rig-exemption trust boundary (codex r3-review round 1, fix round; AMEND of caae810)

**Classification:** User-Challenge / Taste — the trust-vs-authenticate boundary.

**Finding (codex):** the check-D rig dead-write exemption anchors on the carrier's `cam_receiver`/`cam_ordinal`,
which it TRUSTS (it does not — cannot — re-derive them from the source; the source can't self-identify which
GetChild site the resolver credited, exactly as `field`/`child` are trusted anchors in FIX 1). Codex showed a
WELL-FORMED FORGED carrier (valid types, receiver+ordinal chosen to match a genuine survivor) could exempt that
survivor.

**Adjudication (NOT a behavioral silent-miss — PROVE + DOCUMENT the bound, do not add impossible authentication):**
the exemption only ever skips a site that BOTH (1) passes `_site_is_discharged_rig_dead_write` — the site is the
WHOLE RHS of a `self.<field> = ...` assignment (a WRITE to the rig field) — AND (2) is on a script whose binding is
INDEPENDENTLY discharged (`_rig_binding_discharged` -> no raw `self.<field>` READ survives). Therefore the masked
site is ALWAYS a write to a field that is never read -> dead code whose `:GetChildren()` result is discarded ->
functionally INERT. A forged carrier can mask only an inert dead write, NEVER a live child-ref regression. Forging
the carrier requires tampering the internal `conversion_plan.json` — out of the converter's threat model (the
converter writes that artifact itself; an attacker who can edit it can edit the output Luau directly).

**Decision:** do NOT change the exemption's matching logic (it is correct) and do NOT attempt to authenticate
`cam_receiver`/`cam_ordinal` against the source (impossible — the carrier is the only link to the fact). Instead:
bound + document.

**Changes:**
- `contract_verifier.py` — added a TRUST BOUNDARY comment at the exemption entry-gate documenting the trust of the
  carrier anchors as the resolver-fact proxy, the security bound (write-LHS gate + independent discharge -> worst
  case is masking an inert dead write to an already-discharged field), and the conversion_plan.json forging note
  (out of threat model).
- `test_rig_binding_present.py` — added §f-r3-INERT-BOUND (3 tests): (i) a forged carrier matching a genuine
  survivor can skip it ONLY when it is a `self.<field>` write on a discharged script (assert exempted site is a
  write-LHS to `self.weaponSlot` and discharge holds so no read survives -> inert); (ii) a forged carrier CANNOT
  mask a READ survivor (same receiver+ordinal as a read fails clause 1 -> COUNTED); (iii) a forged carrier CANNOT
  mask a write to a DIFFERENT lvalue (`self.muzzle = ...` fails the `self.<field>` LHS gate -> COUNTED).

**Verify:** `test_rig_binding_present.py` green; full fast suite no regressions; no-Any gate pass.

## Slice 1.2 — round-2 BLOCKING fix (discharge soundness gap on dynamic self-index read)

**Codex r2 BLOCKING (valid):** `_rig_binding_discharged` only caught STATIC reads (dot-form `self.<field>`
and decoded static-string bracket `self["<field>"]`). It MISSED a DYNAMIC `self[k]` where `k` is a computed
expression evaluating to the field name (a variable, a `..` concat with a non-literal operand, a call). Such a
script false-DISCHARGED (the field IS read at runtime, un-rerouted), and because check D's rig exemption is
GATED on discharge, the exemption then masked the surviving GetChildren WRITE to that field — a LIVE survivor
slipped through (`verify_contract` -> [] at a10c76a).

**Fix (FAIL-CLOSED on un-analyzable dynamic self-reads):** new `_rig_has_surviving_dynamic_self_index_read`
(+ `_rig_index_receiver_is_self`) added as discharge clause (3) in `_rig_binding_discharged`. Any surviving
code-position `self[<expr>]` index whose `<expr>` does NOT decode to a single static string literal
(`_rig_decode_luau_string_key` -> None) is treated as a POTENTIAL surviving read the reroute did not rewrite ->
discharge returns False. Scoped narrowly: receiver MUST be a standalone `self` (excludes `myself[k]` /
`a.self[k]` / unrelated `other[k]`); a static-string key is already handled by clause (2); an assignment LHS
(`self[k] = v`) is a WRITE, not a read, and does not fail discharge. Code-position-aware via
`_rig_bracket_key_spans` (matches inside strings/comments excluded).

**Why this is the right anchor:** strengthens the binding-present check (the real cause) AND, because the
exemption is gated on `_rig_binding_discharged`, automatically prevents the exemption from masking a survivor
on such a script — the INERT-BOUND invariant holds (discharge => no read in any analyzable form => the
exempted write is genuinely dead).

**Tests (test_rig_binding_present.py):** codex's exact repro (`self["weapon".."Slot"]` + var-bound
`local k = ...; self[k]`) now reds the full `verify_contract` path (rig_binding_present fires AND
child_ordinal_survivor fires -> violations != []); narrator-independent RED proof against the a10c76a blob
(var-bound key false-discharges + masks there, fires here); scope guard (non-`self` dynamic index discharges
True); corpus regression guard (real Player dot-form reads stay discharged + dead-write exemption still applies).
Updated the prior r3/r4 dynamic-key tests that asserted the now-superseded fail-OPEN stance to assert
fail-closed.

**Verify:** test_rig_binding_present.py 122 passed; corpus+retarget+binding 208 passed/1 xfailed; full fast
suite 3177 passed; no-Any gate pass.

## [SLICE 1.2 r3 fix — normalize self-index receiver; close all parenthesized variants]
Codex round-3 BLOCKING (valid): `_rig_has_surviving_dynamic_self_index_read` matched only a BARE
`self` token immediately before `[`, so a parenthesized receiver `(self)[k]` / `( self )["weapon"..suffix]`
(semantically identical to `self[k]` in Luau) passed discharge=True → a live dynamic field read slips →
the check-D rig exemption could mask a live survivor. DECISION: rather than add one more literal pattern
(per-form whack-a-mole), NORMALIZE the receiver in `_rig_index_receiver_is_self` — peel balanced
`( ... )` parenthesization + whitespace (comments are already blanked in the code projection) and test
whether the residual reduces to the keyword `self` at a word boundary. Closes `self[k]`, `(self)[k]`,
`( self )[k]`, `((self))[k]`, `self [k]`, `self --c\n[k]` in one robust check. Kept gated to a self
receiver: a call-suffix `getKey()[k]`, member `a.self[k]`, substring `myself[k]`, parenthesized non-self
`(notself)[k]`, and unrelated `other[k]`/`t[k]` all reduce to a non-`self` residual and do NOT trip
(no over-broadening). RESIDUAL (documented, not chased): fully data-flow dynamic reads — aliasing
(`local s = self; s[k]`), `rawget(self, k)`, metatable `__index` — are an accepted residual (transpiler
emits dot-form `self.<field>`, never aliased; static text analysis cannot prove "never read"). Logged in
followups.md + an explicit xfail marker. Narrator-independent pre-fix RED proof against 5f36a38 (the
bare-self matcher false-passed the parenthesized forms). All targeted suites + full fast suite green
(3188 passed); no-Any gate clean.

- slice 1.3 (S2 r3 fix): omit None-valued optional fields in regen capture (matching child_ref_resolution); dropped the 40 redundant `rig_binding: null` keys from the committed SimpleFPS fixture via deterministic null-key drop (full regen avoided per r1 FINDING 1 — drifts non-Player source). Fixture diff now Player-only; all corpus + fast suite green.

## Slice 1.4 (S3 — net/e2e witness) — REDESIGN r3 AMEND (2026-06-14)

Rebased the e2e witness onto the r3 5-key `rig_binding` carrier
(`{field,child,present,cam_receiver,cam_ordinal}`).

- Fixed the 3 failing carrier assertions (factored-out, generic factored-out,
  generic discharged) to the 5-key shape. Generic tests read `cam_receiver`/
  `cam_ordinal` OFF THE REAL FACT (`entry.rig_facts[0]`), not hardcoded, keeping
  the assertions generic.
- ADDED r3-specific end-to-end witnesses (new section 2b):
  - `test_r3_check_d_does_not_false_positive_on_discharged_rig_dead_write` — the
    ORIGINAL r1 BUG, proven GONE: a discharged binding whose credited dead ordinal
    write survives (Tier-2 skip on the ambiguous single-line `if self.cam then ...`)
    is EXEMPTED by check D (no `child_ordinal_survivor`, success stays clean).
  - `test_r3_check_d_still_fires_on_non_credited_ordinal_survivor` — different
    ordinal `[2]` (credited `[1]`) STILL fires (cam_ordinal anchor load-bearing).
  - `test_r3_check_d_still_fires_on_different_receiver_survivor` — different
    receiver `self.muzzle` STILL fires (cam_receiver anchor load-bearing, codex r8).
  - `test_r3_check_d_exemption_anchors_on_generic_receiver_and_ordinal` — generic
    `view`/ordinal 2 -> `[3]` exempt, stray `[4]` fires (no hardcoded cam/0).
  - `test_e2e_captured_player_passes_real_pipeline_gate_not_blocked` — captured
    Player carried with its REAL 5-key carrier + real budget through the REAL
    `_run_pipeline_gate`; not blocked (r1 bug gone on the real shape).
  - Captured-corpus carrier assertion widened to require non-empty str
    `cam_receiver` + int `cam_ordinal` (the deterministic check-D anchor survives
    the regen/rehydrate path).
- Race/boundary/fresh-conversion witnesses retained; `@pytest.mark.slow` kept on
  the full-conversion test. New driver `_lower_with_awake_write` runs the REAL
  lowering (non-tautological — drives `lower_rifle_rig_retarget` + `verify_contract`).

Verify: `test_rifle_rig_retarget_e2e.py` 18 pass / 1 slow-skip; chain
(rifle/rig_binding/corpus/e2e) 238 pass; full fast suite 3207 pass; no-Any gate pass.

## slice 1.4 (S3 e2e witness) — r3 fix round (round-3 BLOCKING)

**Finding:** the r3 check-D anchor tests HAND-FABRICATED the rig_binding carrier
(via `_lower_with_awake_write`'s hand-built `RigRootedRetargetFact`), so a
regression where the lowering stops promoting `cam_receiver`/`cam_ordinal` from the
resolver fact into the carrier would still PASS within the e2e (witness) slice.

**Fix:** added a REAL-CHAIN end-to-end witness (no hand-built carrier, no hardcoded
anchor) in `converter/tests/test_rifle_rig_retarget_e2e.py`:
- `_lower_via_real_producer_with_survivor` drives the REAL producer
  (`build_child_ref_map` -> `_resolve_rig_facts`) to compute the fact, then the REAL
  `lower_rifle_rig_retarget` to STAMP `cam_receiver`/`cam_ordinal` FROM that fact
  into the carrier, over an AI shape carrying a surviving credited dead write.
- `test_r3_real_chain_check_d_exempts_promoted_anchor_end_to_end`: exemption fires,
  survivor exempt, not blocked; asserts carrier anchor == fact values (promotion).
- `test_r3_real_chain_check_d_fires_when_survivor_ordinal_diverges_from_promotion`
  and `..._receiver_diverges...`: mutate the surviving site's ordinal/receiver away
  from the promoted anchor -> check D FIRES (anchor read from the real carrier
  discriminates).
- `test_r3_real_chain_exemption_dies_if_promotion_drops_anchor_keys`: non-tautology
  guard — strip the promoted keys -> exemption dies, check D fires.

**Verified the guard is real:** garbling the lowering's final `cam_ordinal` stamp
to `999` REDs the exemption witness (carrier != fact). Restored after.

All existing e2e tests kept green. Chain green
(`test_rifle_rig_retarget` + `test_rig_binding_present` + `test_contract_corpus` +
`test_rifle_rig_retarget_e2e`: 242 passed). Full fast suite: 3211 passed, no
regressions. no-Any gate: pass.

## Slice 1.1 FIX-ROUND — phase-integration findings (commit 4997dcb)

- F1 [BLOCKING fix] — seed qualification in `_canonical_receiver` (child_ref_resolver.py). The
  foreign member-LHS seed `other.cam = Camera.main.transform; weaponSlot = cam.GetChild(0)`
  false-admitted a rig fact: the `\b` after the `.` of `other.cam` let `any_assign_re`/`seed_re`
  match the inner `cam` token. FIX: new `_seed_lhs_is_bare_or_this(source, sym_start)` — the
  nearest-preceding-binding scan now admits a seed ONLY when its LHS is a BARE write or a
  `this.<sym>` write (mirrors `_lhs_is_bare_field`'s "bare or this. only" discipline); a foreign
  member access (`other.cam`, `a.b.cam`, `foo.this.cam`) is skipped — not a binding of the bare
  symbol used at the GetChild. Removed the accepted-xfail residual on
  `test_r6_member_write_does_not_seed_bare_cam`; it now asserts the fix. Added deep-member +
  foreign-this-tail a-neg cases; legit bare `cam=` and `this.cam=` seeds still ADMIT.

- F2 [MAJOR fix] — lowering↔verifier discharge parity (rifle_rig_retarget_lowering.py). The
  lowering stamped `present=True` on boundary-form scripts the slice-1.2 verifier
  (`_rig_binding_discharged`, design §1.6) fails closed on, leaving an edited source + a loud row
  instead of a clean abstain. FIX: new `_has_unrewritable_boundary_read(source, field)` mirrors
  the verifier's reject set — bracket `self["field"]`, dynamic `self[expr]` (incl. parenthesized),
  non-`self` receiver (`<Class>.<field>`/`owner.<field>`/alias `p.<field>`), and a raw
  `self.<field>` read in a non-yielding lifecycle method (Awake/Start). `_binding_discharged`
  consults it and returns False → the lowering ABSTAINS (`present=False`, source unedited for the
  reroute). Implemented FULL predicate parity (not just the minimal "abstain on any surviving
  non-dot-form"). SHADOWED-`self` is deliberately NOT a boundary form: `self` there is a foreign
  object, neutral, matching the verifier's shadow-guard mirror + the existing
  `_has_surviving_field_read`/`test_p3_shadowed_self_mirror`. Pure-integer `self[1]` and a
  different string key `self["other"]` are not boundary forms (no over-abstain). Clean dot-form
  corpus reads still discharge `present=True`.

- F2 test impact (1.1-owned) — `test_h5_yield_guard_abstains_in_awake_rewrites_in_getrifle` and
  `test_h5_start_is_non_yielding` encoded the SUPERSEDED "Awake/Start raw read sees a safe nil,
  still discharge" premise (design §1.6 / lines 194-198 dropped it). Updated both to assert the
  new contract: a raw lifecycle read makes the lowering abstain (`n==0`, `present=False`, source
  unedited). No 1.2/1.3/1.4-owned test needed changes — full fast suite green (3064 passed).

## Slice 1.1 — round-4 review fix round (seed-trivia false-admit + no-mutual-mask) — 2026-06-14

- **FINDING 1 (BLOCKING, UNSAFE — FIXED):** `_seed_lhs_is_bare_or_this` (child_ref_resolver.py)
  skipped only spaces/tabs when testing whether the seed symbol was preceded by a member-access
  `.`. A FOREIGN member-LHS seed split by a comment or newline (`other.\ncam = Camera.main.transform`
  / `other./*c*/cam = ...`) thus false-ADMITTED — a non-camera binding silently retargeted to
  `_MainCameraRig` (a bogus RigRootedRetargetFact the verifier does NOT catch → ships a wrong
  retarget; the unsafe direction). **Fix:** the member-access check now skips ALL C# trivia
  (spaces, tabs, NEWLINES, `//` line + `/* */` block comments) via the existing
  `_skip_ws_and_comments_back` helper (authoritative `_cs_pos_is_code` comment classification),
  both for the leading-`.` test AND the `this`-qualifier walk-back + its own member-tail
  (`foo.this.cam`) guard. Legit `cam =` / `this.cam =` / `this . cam =` / `this./*c*/cam =`
  still ADMIT. New tests: `test_f1_member_seed_{newline,block_comment,line_comment_and_newline,
  tab_newline}_before_sym_rejected`, `test_f1_this_{dot_block_comment,spaced_dot}_seed_still_admits`,
  and a direct-unit `test_f1_seed_lhs_helper_unit_trivia_forms` over the full trivia matrix.

- **FINDING 2 (discharge-predicate desync — DOCUMENTED, fail-closed-safe; per-form parity NOT
  pursued):** the lowering's `_binding_discharged` / `_has_unrewritable_boundary_read`
  (rifle_rig_retarget_lowering.py) is a BEST-EFFORT HINT; the slice-1.2 verifier's
  `_rig_binding_discharged` (run on the final output) is the SOLE discharge authority (design
  §1.6 / FIX 1). The two text-scanners can DESYNC on a few boundary forms (`self[<int>]`,
  shadowed-`self` dot-read, concat bracket key `self["weapon".."Slot"]`) where the lowering is
  LENIENT and the verifier FIRES — the FAIL-CLOSED-SAFE direction (verifier stricter → binding
  fails closed, never silent-wrong). All residual forms are NON-REACHABLE from the deterministic
  dot-form transpiler, so per-form parity (brittle two-scanner mirroring) is INTENTIONALLY not
  pursued. Documented in the `_binding_discharged` docstring.
  - **CRITICAL SAFETY CHECK — no mutual-mask:** empirically, the only UNSAFE state (BOTH discharged
    while a real surviving read exists) does NOT occur. Each boundary form either makes the
    lowering ABSTAIN (`present=False` — `self[1]`, concat-key, and the dynamic/string-key/non-self
    forms never produce a `self:_resolveWeaponSlot(` call, so discharge condition 1b fails) or, on
    the clean dot-form, both discharge with no surviving read. New `test_f2_no_mutual_mask_against_
    verifier_or_conservatism` drives the lowering over a 6-form boundary matrix and (when the
    verifier predicate is importable at phase integration) cross-checks `_rig_binding_discharged`
    asserting no mutual-mask + verifier-stricter desync; on this slice branch the verifier r3
    predicate is NOT yet present (it lands in slice 1.2), so the test SKIPS the cross-scanner check
    and instead asserts the lowering's own conservatism property (it can never be the masking party).

- **Verify:** `tests/test_rifle_rig_retarget.py` green (94 passed); full fast suite
  `pytest tests/ -m "not slow"` 3072 passed / 45 skipped / 5 xfailed; `tools/check_no_any.sh` pass.

## HARDEN-FIX phase 1 (rig_binding) — codex 4 P1s (2026-06-14)

Verified each of the 4 codex-harden P1s against the assembled phase; fixed the 3 that
reproduced + added the missing e2e test. Each bug-fix has a test proven RED pre-fix.

1. **`_last_module_return_span` not module-scope-aware (BLOCKING) — REPRODUCED + FIXED.**
   `rifle_rig_retarget_lowering.py`. Repro: a script whose ONLY `return Player` is method-local
   (`function Player.new() return Player end`) while the module epilogue returns a different
   symbol (`return M`) — the lowering spliced the resolver INSIDE `new` (still valid Luau, so the
   syntax re-check did NOT catch it) and stamped `present=True`. Fix: new `_return_is_inside_
   function_body` tracks `function`/`do`/`then`/`repeat` nesting from file start; any return inside
   an open function body is skipped, so only the MODULE-LEVEL trailing return is the splice point.
   Abstains (injected=False, present=False) when no module-scope return exists. Tests:
   `test_harden_method_local_return_class_not_chosen_as_splice` (+ epilogue-still-chosen +
   module-level-do-end-not-misclassified regression guards).

2. **`_rig_resolver_body_is_rig_lookup` not class-bound (BLOCKING) — claim A NOT reproduced,
   claim B REPRODUCED + FIXED.** `contract_verifier.py`. Claim A (`return nil` body): does NOT
   reproduce — the existing body check already requires BOTH `_MainCameraRig` + `FindFirstChild`
   markers as live code, so a stub body is rejected. Claim B (wrong-class resolver): REPRODUCED —
   a `function Helper:_resolveCamera()` carrying the real body false-greened (the decl regex matched
   ANY class), while the rerouted `self:_resolve...()` calls bind to the primary class. Fix: new
   `_rig_module_class` derives the module's primary class (first code-level method decl — the SAME
   rule the lowering's `_read_class_name` uses); the resolver decl regex is now anchored to that
   class. Test: `test_harden_wrong_class_resolver_does_not_discharge` (+ host-class-still-discharges).

3. **C# admission guards skip whitespace but not comments (BLOCKING) — REPRODUCED + FIXED.**
   `child_ref_resolver.py`. `_seed_dominates_use` (:552): a `//`/`/* */` comment between a braceless
   conditional header and its seed (`if (c) /*x*/ cam = ...`) defeated the conditional detection →
   FALSE-ADMITTED the conditional seed as dominating. Fix: route the back-walk through
   `_skip_ws_and_comments_back` + new `_preceding_governor_keyword` (trivia-aware keyword read).
   `_canonical_receiver` (:720): the exact-seed `\s*` regex FALSE-REJECTED a comment-split legit seed
   (`cam = /*c*/ Camera.main.transform`) → silently dropped the rig fact (fail-closed-safe but a
   correctness gap on valid input). Fix: new `_match_exact_cam_seed_rhs` + `_skip_ws_and_comments_fwd`
   match the seed RHS skipping trivia between tokens; foreign/longer-chain/`==` still return None.
   Removed now-unused `_CS_BRACELESS_GOVERNOR_RE`. Tests: 8 in `test_child_ref_resolver.py`
   (dominates × 4, canonical_receiver × 4).

4. **No e2e preserve/resume test for rig_binding persistence (MAJOR missing test) — ADDED.**
   `test_rig_binding_present.py::test_rig_binding_persistence_roundtrip_preserves_checkD_anchor`:
   builds a discharged carrier with a SURVIVING credited dead-write (exemption is load-bearing),
   SAVEs the rig_binding block to conversion_plan.json, LOADs via the production
   `Pipeline._load_rig_binding_for_rehydration`, RESTOREs onto a rehydrated script, asserts all 5
   keys round-trip AND the rehydrated carrier still anchors check-D's exemption (no false re-fire on
   resume). Proves a hypothetical 3-key LOAD filter (drops cam_receiver/cam_ordinal) loses the
   anchor → check-D fires.

**Verify:** full fast suite `pytest tests/ -m "not slow"` 3242 passed / 45 skipped / 6 xfailed;
`tools/check_no_any.sh` pass. 4 new bug-fix tests proven RED against pre-fix code (stash-out).
Slop deferred to /drive-finalize (no slop fixed here).

## HARDEN-FIX (round-2) — phase 1 — elseif-drift regression closed

- BLOCKING regression (both voices, end-to-end on the real SimpleFPS corpus Player, 7 `elseif`
  tokens): round-1 harden's `_return_is_inside_function_body` walked block depth without handling
  `elseif`. In Luau an `if … elseif … then … end` chain has MULTIPLE `then` openers but ONE `end`,
  so each `elseif`'s `then` incremented `block_depth` with no matching close — any method with an
  if/elseif chain stayed artificially "open", so a genuine module-trailing `return <Class>` after it
  was misclassified function-local and SKIPPED → resolver not injected → `present=False` on
  essentially every realistic transpiled controller. Pre-fix on the real corpus Player:
  `_last_module_return_span=None`, `modified=0`, `present=False`. Post-fix: span found, `modified=1`,
  resolver injected, reads rerouted, `present=True`.
- FIX: in `_return_is_inside_function_body`, `elseif` DECREMENTS `block_depth` to cancel its own
  upcoming `then` (net 0 for the if/elseif chain) and NEVER touches `fn_depth` — the same grammar
  the file's `_FALLBACK_BLOCK_CLOSERS` already uses for `elseif`. Verified `if/then/end`,
  `for/do/end`, `while/do/end`, `repeat/until`, nested `function`, and `else` stay balanced.
- TESTS (close the gap): `test_h1_elseif_chain_before_module_return_still_discharges` drives the REAL
  `lower_rifle_rig_retarget` on a Player whose methods carry `if/elseif/elseif/else/end` chains before
  the module return → asserts resolver injected before the return, reads rerouted, `present=True`;
  `test_h1_elseif_module_return_span_is_module_scope` is a focused unit test of
  `_return_is_inside_function_body`/`_last_module_return_span` over elseif-bearing input (module-scope
  return = False; method-local return after an elseif chain = True). Both proven RED against the
  pre-fix (round-1) code via stash-out, green after.
- SCOPE: fixed ONLY this regression + its tests; no other edits; slop deferred to /drive-finalize.
- Verify: full fast suite `pytest tests/ -m "not slow"` → 3244 passed / 45 skipped / 6 xfailed;
  `tools/check_no_any.sh` pass; new tests RED against pre-fix, green after. Changed files:
  `converter/converter/rifle_rig_retarget_lowering.py`, `converter/tests/test_rifle_rig_retarget.py`.
- 2026-06-14T08:42:05Z FINALIZE de-slop: removed verified-dead rig-ordinal-write chain from contract_verifier.py (_rig_has_surviving_ordinal_write + _rig_statement_rhs_end + _rig_collapse_code_ws + _rig_is_ident_char + _RIG_ORDINAL_WRITE_TAIL_RE; kept live _rig_code_projection/_rig_line_continues); pruned dead-only test + dead assertions from test_rig_binding_present.py; trimmed round-by-round counter-commentary docstrings (lowering header+_binding_discharged, contract_verifier exemption/discharge/bracket-key, pipeline rehydrate, regen_contract_corpus _FIELDS) and fixed stale test-name citation. No behavior change; 3243 passed/45 skipped, no-Any pass.

## Run: turret-projectile8-20260614T235948 (relation #8 — turret projectile/damage; 2026-06-15)


## Run: turret-projectile8-20260614T235948 (relation #8 — turret projectile/damage; 2026-06-15)


- **baseRef = upstream/main @ d624bd5** (latest, has the obligation-schema doc + 69 commits
  newer than stale local main). Classification: Mechanical. Rationale: implement #8 against
  current converter code; PRs target ntornow upstream.
- **Carried the uncommitted design analysis as the opening commit** (4741861) on the feature
  branch (TODO P0 turret entry + §8/§9 doc-row corrections). Classification: User-approved
  (AskUserQuestion). 
- **CORRECTED the damage mechanism from live evidence:** the live failure in the current
  generic conversion is an INVALID `FindFirstChildOfType` API crash, not `Player:TakeDamage`→
  curHealth (the analysis's variant). Both yield "no measured Humanoid damage". The damage fix
  must be DETERMINISTIC (handle the AI-output non-determinism), per the obligation-schema /
  upstream-source anchoring rule. Classification: Taste/architecture — confirm in design review.

- **PIVOT (2026-06-15, user-directed + empirically validated): drop the projectile-specific design;
  fix the GENERIC physics-unit mistranslation instead.** Root cause (live spike, physics-spike.md):
  the bullet drops because Roblox mass is ~45x inflated (density formula ignores STUDS_PER_METER^3
  volume scaling) + gravity is ~5.6x too strong for the scale (x20 vs geometry x3.571). The MOST
  FAITHFUL conversion is a uniform similarity transform (lengths xSPM [done], mass x1, forces xSPM,
  gravity xSPM). Chosen over mass-absorption variants because those distort mass (wrong momentum).
  Fits the existing arch: unit-scaling is already AI-taught (prompt) + validator-checked; we extend
  both to forces. No projectile classifier, no host primitive, no AI-output overwrite.
  Classification: Taste/architecture (user-directed). Supersedes the 4-round projectile design
  (archived in archive-projectile-v1/).

- **DECISION (2026-06-15, user-chosen at the density-floor blocker): faithful launch via a GENERIC
  HOST VELOCITY CORRECTION, not mass-fix-for-launch.** Roblox's density floor (0.01) makes
  AssemblyMass==Unity_mass unreachable for large light parts (bullet min ~2.67, not 1.0), and at that
  mass even SPM-scaled force gives only ~80 studs/s. So: a generic host pass corrects a force-launched
  body's post-impulse velocity to the faithful Unity_speed x STUDS_PER_METER, sidestepping the floor.
  Generic (all force-launched bodies, not bullet-specific); the AI still authors ApplyImpulse (host
  corrects the OUTCOME, not the code). Classification: Taste/architecture (user-directed).
  IMPLICATIONS for the design (REDESIGN needed):
  * This SUBSUMES Phase 3's force-scaling (AI emits RAW force; the host velocity correction handles
    the unit). Phase 3 likely reduces to the damage-API fix only.
  * Mass correction (old Phase 1) becomes BEST-EFFORT / collision-momentum-only (floor-limited),
    NOT the launch mechanism — lower priority, possibly deferred.
  * The NEW central mechanism: a generic host velocity-correction for force-launched bodies. Open
    design question for the redesign: how does the host detect "a body was just force-launched" and
    know the faithful target (needs Unity_mass stamped + the raw impulse OR observing v_actual and
    rescaling by (roblox_mass/Unity_mass)*SPM)? This is the riskiest unknown to spike.
  * Gravity correction (Phase 2) unchanged.
  => design.md (high-level) AND design-phase1.md must be reworked around this; re-run the dual-voice
     design review on the reworked approach.

- **REWORK to v3 (2026-06-15, fresh-session resume): faithful LAUNCH via a generic host
  velocity-correction (option B implemented), density mass-correction DROPPED.** Spike
  (physics-spike-v2-velocity-correction.md) proved the correction is MASS-AGNOSTIC
  (factor=(m_rbx/m_unity)*SPM applied to the launch delta => v_target = impulse*SPM/m_unity, the
  m_rbx cancels) so no density change is needed for the launch (corrected X=214.3 ≈ 60*SPM=214.26;
  flew 287.7 studs). Detection is LIFECYCLE-TIED (sample v_before/v_after around Start on the
  prefab-clone/Awake-Enable-Start seam), NOT a free-running jump watcher (spike: free-fall
  false-fires under variable dt). New phase structure: P1=host velocity-correction (launch),
  P2=damage-API fix (was P3's API half), P3=gravity (was P2, now fully independent). F16 GREEN =
  P1 + P2; all three phases independent. Only the _UnityMass STAMP is kept from old-P1; force-scaling
  (old-P3) subsumed by the host correction. Classification: Taste/architecture (user-directed B,
  empirically validated). Supersedes the v2 unit-scaling design (archive-unitscale-v2/).

- **Slice 1.5 dropped as a code slice (2026-06-15): `converter/output/` is GITIGNORED** (0 tracked
  files), so the "fix the output script" half of the bug-fix protocol is a LOCAL regenerated artifact,
  not committable code. The pipeline fix (slices 1.1-1.4) regenerates correct output on re-conversion.
  Slice 1.5's acceptance moves to: A6 (integration no-regression) at the phase-1 integration review;
  A7 (Studio e2e: bullet flies + damages) at the VERIFY stage — and full F16 needs Phase 2's damage-API
  fix too, so the F16 e2e runs after Phase 2. Classification: right-sizing (gitignore reality).

- **Phase 3 (scale-consistent gravity) SPLIT to a follow-up (2026-06-15, user-chosen at the Phase-3
  scope gate).** F16 (the task goal) is delivered by Phases 1+2 (host impulse seam + valid damage API),
  both hardened on the feature branch. Phase 3 is independent, off the F16 critical path, and the
  widest-blast change (per-object gravity for every dynamic body in every game). Ship Phases 1+2 now;
  Phase 3 becomes a separate effort with its own design/review/regression. Classification: User-approved
  (AskUserQuestion) scope reduction.

## ── Run: gravity-scale-correction-20260615T093547 (scale-consistent gravity, 2026-06-15) ──
# Decisions — scale-consistent gravity (generic mode)

- **Mechanism = per-assembly compensating upward VectorForce on the AssemblyRootPart**
  (ApplyAtCenterOfMass, World space; force = AssemblyMass * (workspace.Gravity − desiredStudsAccel)).
  Only realistic per-body lever; leaves characters and global gravity untouched.
  Classification: Mechanical

- **Per-ASSEMBLY, not per-part — one force per assembly root sized on AssemblyMass; skip welded
  non-root parts.** A welded body has a single COM/AssemblyMass; per-part forces double-count.
  Reuse the existing applyImpulse assembly-resolution precedent (scene_runtime.luau:1518-1532).
  Classification: Mechanical

- **Project gravity is EMITTED into the generic plan as a constant anchored on the deterministic
  upstream Unity project g (9.81 default), NOT recovered from workspace.Gravity.** Verified that
  generic mode never writes workspace.Gravity (grep of scene_runtime.luau + planner = zero); the
  *20 form only exists in CharacterBridge:SetGravity which has zero runtime callers. The
  recover-from-workspace formula is a frozen-9.81 trap that only coincidentally works at default g.
  Classification: Mechanical (verified against real code)

- **Boot-time correction loop lives in the SceneRuntime.new constructor** (scene_runtime.luau:153) —
  generic-mode entry where plan + stamped instances are both available.
  Classification: Taste

- **UseGravity=false ⇒ full cancellation; GravityScale scales the target; anchored + Humanoid/
  character assemblies skipped.** Reuses already-stamped facts (scene_converter.py:2790-2814); no
  new classification surface.
  Classification: Mechanical

- **Gate on a concrete fall-rate invariant (net vertical accel ≈ |unity_g|*gravityScale*
  STUDS_PER_METER within tolerance, mass-agnostic), not just a green unit suite.**
  Classification: Mechanical

- **Wide blast radius gets a dedicated Phase 2: corpus/canary regression + slow/long-range-arc
  acceptance + resting/sleep/auto-sleep verification.** Every dynamic body in every game is
  affected; green unit suite is insufficient.
  Classification: Taste

## [coordinator] Project-gravity source — parse DynamicsManager in Phase 1 (resolves design Open Question 1)
Decision: Phase 1 parses Unity `ProjectSettings/DynamicsManager.asset` `m_Gravity` for the gravity
magnitude (9.81 default when file/field absent) and emits it as a generic-plan constant, rather than
shipping a frozen 9.81-default constant with no parse.
Rationale: the task explicitly and repeatedly requires "the ACTUAL project gravity, not a frozen
9.81"; shipping a 9.81-default-only constant would violate that explicit directive. The parse is a
small bounded YAML read with a clean default fallback, and it makes the deterministic-upstream-source
principle real. Magnitude only (free fall is world-down); non-uniform/2D/runtime gravity out of scope.
Classification: User-Challenge (honors an explicit user directive over the planner's lean-to-defer).

## [autoplan eng-review, Claude voice] Three design corrections before coding
1. Placement (was BLOCKING): correction is a SERVER-ONLY post-assembly workspace:GetDescendants()
   sweep, NOT a SceneRuntime.new constructor loop. Real code: constructor has no instances;
   new()+start() run once per domain (double-apply risk); start() skips script-less dynamic props.
   Server-only so VectorForce+Attachment replicate to clients. Classification: Mechanical (corrects error).
2. Phasing (was MAJOR): resting/auto-sleep/drift Studio spike moves to PHASE 1 as a primitive-validity
   gate (a constant VectorForce can invalidate the mechanism; can't be a Phase-2 regression). Mechanical.
3. Gate honesty (was MAJOR): Phase-1 CI gate = STRUCTURAL (emitted Force.Y == formula); behavioral
   fall-rate = Studio acceptance run from MAIN (no pytest physics integrator; Studio half can't run in
   a subagent). Mechanical.
Minor caveats folded into Approach: no clamp when desired>g (negative force correct); read
workspace.Gravity (=196.2, rbxlx_writer.py:1464) at apply time; _UnityMass may not be on AssemblyRootPart.

## [design-review r1, Codex voice] Two P1 corrections (architecture shift)
- BLOCKING: gravity correction decoupled from the SceneRuntime host-runtime emit gate → a STANDALONE
  generic SERVER script emitted to ServerScriptService whenever Workspace has dynamic _UnityMass parts.
  Host runtime only emits with >=1 runtime-bearing module (pipeline.py:6295,6324); keying off it would
  silently skip games with dynamic props but no scripts. Scalar baked into the script at gen time.
  Classification: Mechanical (corrects error).
- MAJOR: boot sweep + idempotent workspace.DescendantAdded spawn hook (tag _ScaleGravityCorrected per
  AssemblyRootPart) to catch runtime-spawned prefab clones (instantiatePrefab). Mechanical.
- MINOR: scalar = abs(m_Gravity.y) (Y-axis force), warn+fail-open on non-zero x/z. Mechanical.
- MINOR: Phase 2 perf-budget failure may BOUNCE the mechanism back to Phase 1 (too-expensive
  per-assembly forces = primitive failure, not regression). Mechanical.

## [design-review r2, Codex voice] Two more P1 corrections (dual-domain + prefab-spanning emit)
- BLOCKING: emit gate keys on dynamic _UnityMass across scene workspace UNION prefab templates,
  emitted after _generate_prefab_packages() (write_output injects before replicated_templates
  materialize: pipeline.py:2694 vs :6000). A workspace-only gate skips prefab-clone-only games. Mechanical.
- MAJOR: correction emitted to BOTH domains (ServerScriptService + StarterPlayerScripts), tag-idempotent.
  Key insight: a VectorForce is a persistent CHILD applied by whoever SIMULATES the part → invariant is
  "exactly one tagged force per dynamic assembly". Server corrects+tags all it sees (replicates); client
  corrects only untagged client-local clones (instantiatePrefab not domain-gated, scene_runtime.luau:2104).
  Replaces the earlier server-only stance (left client-local clones at raw gravity). Mechanical.

## [design-review r3, Claude voice] REVERT dual-domain → provably-safe server-only
The dual-domain correction (server + client copies) added to fix r2's client-local MAJOR itself
introduced 2 MAJORs (Claude r3): a replication race (client sees a server-replicated body before its
tag lands → applies a 2nd force → ~2x too slow) and an unverified "whoever simulates = network owner"
primitive assumption. Net-negative forward fix → reverted to server-only.
Server-only is provably correct: a VectorForce is a persistent CHILD applied by whoever network-owns
the part; the server creates exactly ONE replicated tagged force per assembly → applied correctly
under server OR auto-assigned-client ownership, no race, no double-apply. KEPT from the rounds: the
emit-gate-spans-prefab-templates fix (r2 BLOCKING, domain-independent) and abs(m_Gravity.y).
Client-local clones → documented Out-of-scope limitation + follow-up (the ONE Gate-A confirm item).
Classification: Mechanical (reverts a net-negative fix per the operating rule).

## [design-review r4, Codex voice] Client-originated clones BROUGHT INTO SCOPE (race-free clone-site hook)
Codex r4 (well-evidenced): client-local clones are a DEFAULT path, not an edge — --networking=none
falls back to client (conversion_context.py:140) and corpus spawners (Turret/Mine/HostilePlane) are
client-classified. So server-only would miss the common single-player case's runtime-spawned dynamics.
Resolution: add a THIRD correction surface — a targeted hook at the runtime's client instantiatePrefab
clone-site that corrects ONLY the clones the client itself creates (client-local by construction →
never server-tagged → no race; this is the round-3 race avoided by keying on client-ORIGINATED clones,
not a blanket client DescendantAdded). Shared correctDynamicAssembly helper across all three surfaces
(server boot sweep, server DescendantAdded, client clone-site), tag-idempotent. Codex also CONFIRMED
(not P1): network-ownership claim sound; emit-after-prefab-packages feasible.
Classification: Mechanical (resolves a default-path P1 with the race-free targeted form).

## [phase-1 detailed design] Interfaces, slice plan, and three real-code divergences
- D-P1.1 Force = AssemblyMass*(workspace.Gravity − |g.y|*gravityScale*SPM), one VectorForce+Attachment
  per AssemblyRootPart, ApplyAtCenterOfMass+World, no clamp. Mass-independent net accel. Mechanical.
- D-P1.2 Project gravity parsed by NEW converter/converter/project_gravity.py via targeted m_Gravity-line
  extraction (NOT yaml.safe_load — Unity %TAG !u! headers break full YAML; abs(y) only; 9.81 default).
  Mechanical. (Divergence D2: no ProjectSettings parser exists today.)
- D-P1.3 Standalone server script BAKES its own constant; the client clone-site hook reads the SAME
  parsed value via a single NEW plan field `gravityDesiredBaseStuds`. Server script stays decoupled
  from the plan (load-bearing); client hook already requires the plan. Taste. (Resolves divergence D3.)
- D-P1.4 `_ScaleGravityCorrected` is a part Attribute on the AssemblyRootPart (not CollectionService) —
  replicates with the instance, matches _UnityMass/_SceneRuntimeId convention, single dedup authority.
  Mechanical.
- D-P1.5 New subphase `_subphase_inject_gravity_correction` runs AFTER `_generate_prefab_packages`,
  emits a `Script` to ServerScriptService (parent_path), idempotent via an autogen marker. Mechanical.
- D-P1.6 Skip/scale facts (GravityScale/UseGravity) read at the ASSEMBLY level, NOT off the _UnityMass
  part — mesh-wrap leaves these on the OUTER Model while only _UnityMass moves to the inner *_Mesh
  (scene_converter.py:2176-2183). Mechanical. (Resolves divergence D1.)
- D-P1.7 Canonical helper text = a Python constant `_GRAVITY_CORRECTION_HELPER_LUAU` in autogen.py,
  mirrored into scene_runtime.luau, guarded by a parity structural test (prevents drift). Taste.
- D-P1.8 Phase-1 slices run STRICTLY SERIAL (1.1→1.5): autogen.py + the subphase-order AST test are
  touched by multiple slices, so serial keeps per-slice `owns:` disjoint. Mechanical.
- D-P1.9 Stale-force-on-late-anchor accepted as a documented Phase-1 edge, no re-check wired —
  anchored parts ignore forces, so the stale force is inert; re-check is over-design. Taste.
- Divergence D4: a test (test_pipeline_write_output_subphases.py) pins write_output's AST call sequence
  == SUBPHASE_ORDER, so the new subphase must be added to BOTH in the same slice (1.2 owns that edit).

## [phase-1 detailed design, dual-voice review] Revisions for the unbuildable plan-field plumbing
Both review voices (Claude + Codex) converged on ONE root problem: the client
`gravityDesiredBaseStuds` plan-field plumbing was unbuildable as originally designed. Four corrections:

- **D-P1.3 REVISED (early parse + plan stash + allowlist) — was BLOCKING (ordering).** The scalar must
  be parsed EARLY in `plan_scene_runtime` (pipeline.py:998 — PHASE index 2, holds
  `self.unity_project_path`, writes `self.ctx.scene_runtime` at line 1048) and STASHED into
  `self.ctx.scene_runtime["gravityDesiredBaseStuds"]`, AND `"gravityDesiredBaseStuds"` added to
  `_PLAN_KEYS_FOR_HOST` (autogen.py:638). Reason: the SceneRuntimePlan ModuleScript is emitted in
  `_subphase_inject_scene_runtime` (pipeline.py:2694 → emit at 6451) via
  `generate_scene_runtime_plan_module`, which serializes ONLY keys already present in the dict AND in
  the `_PLAN_KEYS_FOR_HOST` allowlist (filter at line 654). A post-prefab gravity subphase parses the
  scalar AFTER that emit, so the field would never reach the plan → client always falls back to frozen
  9.81 (the exact D3 trap). The standalone server-script subphase reads the SAME stash to bake its
  literal — ONE parse, two consumers, server script stays decoupled.
  Classification: Mechanical (corrects a load-bearing ordering/allowlist bug; the cited early-parse
  site is verified real in the worktree).

- **D-P1.8 REVISED (re-slice to give the plan-field plumbing one owner) — was MAJOR (slice ownership).**
  ALL parse + plan-field plumbing — `project_gravity.py` + the early `plan_scene_runtime` stash in
  `pipeline.py` + the `_PLAN_KEYS_FOR_HOST` edit in `autogen.py` + the AC16 emit-survival test — is
  now owned by ONE slice (1.1). The old split left the early stash and the allowlist edit unowned (a
  required edit with no slice). `pipeline.py` and `autogen.py` are now each touched by exactly two
  serialized slices (1.1 then 1.2), disjoint regions, one writer at a time. The client hook (1.3) only
  READS the plan field, so it no longer touches `autogen.py` (removes the old 1.2/1.3 conflict).
  Serial chain 1.1→1.2→1.3→1.4→1.5, no cycle.
  Classification: Mechanical (corrects an ownership gap — a required edit was unowned).

- **AC16 added (plan-emit-survival structural test) — was MAJOR (missing structural test).** A pure-
  Python test asserts `generate_scene_runtime_plan_module({... "gravityDesiredBaseStuds": s ...})`
  output INCLUDES the field (i.e. it survives `_PLAN_KEYS_FOR_HOST`), mirroring
  `test_scene_runtime_host_emit.py::test_plan_module_embeds_scene_prefab_placements` and
  `test_plan_emits_player_signal.py`. This is the load-bearing plan-emission hop; AC15 (mock-plan hook)
  stubs the plan and does NOT cover the producer. Owned by slice 1.1.
  Classification: Mechanical (closes a coverage gap on the load-bearing hop).

- **D-P1.6 clarified (per-instance fact resolution) — was P2 (fact resolution).** The
  `correctDynamicAssembly` helper must read `GravityScale`/`UseGravity` off the OWNING Model/assembly
  instance (per D1: those facts do NOT travel to the inner `*_Mesh` BasePart; only `_UnityMass` does).
  The applyImpulse precedent (scene_runtime.luau:1518-1532) walks for `_UnityMass` ONLY, so the helper
  must NOT reuse that part as the fact source — bind `owningModel = root:FindFirstAncestorWhichIsA(
  "Model") or root` and read the scale/skip attributes off it (falling back to the root for
  non-mesh-wrapped bodies). `Anchored` stays read off `root` (a real BasePart property). AC8 extended
  to assert facts on the outer Model are honored.
  Classification: Mechanical (makes the fact-lookup instance explicit, per D1).

## [phase-1 detailed design, dual-voice review r2] Two MAJOR P1 fixes (both verified vs real code)

- **D-P1.6 RE-REVISED (fact resolution is ROOT-FIRST, not owning-Model-first) — was MAJOR (reads the
  wrong instance).** The prior revision resolved `GravityScale`/`UseGravity`/`anchored` via
  `root:FindFirstAncestorWhichIsA("Model") or root` (the parent CONTAINER Model). But the real code
  stamps ALL rigidbody facts on the BODY PART ITSELF (scene_converter.py:2791/2808/2810/2814); ONLY
  `_UnityMass` (+`_Scale*`/`_Mesh*`) moves to the inner `*_Mesh` on mesh-wrap (scene_converter.py:
  2176-2183); and a parent-with-children becomes a `Model` container holding NO rigidbody facts
  (scene_converter.py:1886). So a NON-wrapped dynamic part nested under a parent `Model` has its facts
  on `root` — the owning-Model-first lookup read the empty parent container and silently fell back to
  default gravity. FIX: read each fact off `root` FIRST; ONLY IF that attribute is absent on `root`,
  walk ancestor `Model`s for the first holder (or a `Humanoid`). Fallback is gated per-attribute on
  "absent on root", NOT on "owningModel lookup is nil". `Anchored` stays read off `root` (real
  BasePart property). Updated: §1.5 helper fact-lookup, §0 anchor table (split into part-stamping +
  Model-container + mesh-wrap-travel rows), D1 divergence note, AC8 (mesh-wrap shape), and ADDED AC8b
  (non-wrapped-under-Model: facts on the part, parent container empty, no default fall-back). The
  corrected rule covers BOTH shapes (facts on root for non-wrapped; on ancestor Model for mesh-wrap).
  Classification: Mechanical (corrects the fact-resolution direction against verified real stamping).

- **D-P1.10 Zero-gravity falsy bug in the server-side scalar consumer — was MAJOR.** The emit subphase
  read `self.ctx.scene_runtime.get("gravityDesiredBaseStuds") or default`. In Python `0.0` is falsy,
  so a valid project with `m_Gravity.y == 0` (zero gravity) fell through `or` to the 35.03 default and
  baked the WRONG constant into the server `SceneGravityCorrection` script — breaking the "one parse,
  two consumers" invariant (the client plan path preserves 0; the `or`-based server path would not).
  FIX: use an explicit `is None` check in the Python emit subphase (NOT `or`):
  `stashed = (...).get(key); desired = stashed if stashed is not None else default`. ADDED AC9 zero
  parse case (`m_Gravity.y==0 ⇒ 0.0`, not the default) and AC10b (stashed `0.0` survives into the
  emitted `DESIRED_G_STUDS_BASE == 0.0` constant — full-cancel, not 35.03). The Luau-side `or` (§1.6)
  is FINE and unchanged — in Luau `0` is truthy, so the emitted/stashed `0` survives. Also folded the
  prior round's non-blocking notes: assert `repr()`-formatted floats in structural tests (AC10/AC10b/
  AC16 — the emit serializes floats via `repr`), and ADDED AC16b pinning resume-rehydration of the
  stashed field across the producer→consumer subphase hop.
  Classification: Mechanical (fixes a falsy-default data bug + adds the structural guards).

## [phase-1 detailed design, dual-voice review r3] MAJOR P1: `_UnityMass` carrier can be a MODEL (S3), + full shape enumeration

- **D-P1.11 Seed scan is CLASS-AGNOSTIC; helper handles the Model-carrier shape — was MAJOR (missed
  carriers).** The three correction entrypoints (boot sweep, server `DescendantAdded`, client
  clone-walk) filtered `_UnityMass` on `IsA("BasePart")`. But a Rigidbody on a no-mesh parent with
  children becomes a `Model` CARRIER that carries `_UnityMass` AND `GravityScale`/`UseGravity`/
  `anchored` directly on the Model: `_convert_node` sets `class_name="Model"` at scene_converter.py:1886
  (children + no mesh) → `_process_components` (2024) stamps `_UnityMass` (2797), `GravityScale` (2808),
  `UseGravity` (2810/2814), `anchored` (2791) on that `part` → `_wrap_geometry_with_children_into_model`
  (2110) RETURNS EARLY at the 2141 guard (`_has_geometry` requires Part/MeshPart + mesh, 2137-2140), so
  NOTHING moves to an inner `*_Mesh` and the facts stay ON THE MODEL. A BasePart-only scan misses that
  assembly entirely → never corrected. FIX (verified vs the worktree real code at each step): (1) all
  three entrypoints select on `inst:GetAttribute("_UnityMass") ~= nil` regardless of class; (2)
  `correctDynamicAssembly` resolves a representative BasePart for a Model carrier (PrimaryPart, else
  first descendant BasePart; DEFER without tagging if the Model has no BasePart descendant yet) then
  that part's `AssemblyRootPart` (the applyImpulse precedent at scene_runtime.luau:1518-1532 assumes a
  BasePart arg, so a Model carrier needs the extra representative-part hop); (3) facts resolve
  CARRIER-FIRST then ancestor-`Model`-walk — for S3 the carrier Model holds both `_UnityMass` and facts;
  (4) the `_ScaleGravityCorrected` tag lands on the resolved root so dedup is keyed on the physical
  assembly. Added AC8c (Model-carrier server path) → slice 1.2, AC8d (Model-carrier client clone-site
  path) → slice 1.3; edge cases 14 (Model carrier) + 15 (multi-part scale heterogeneity); §0b explicit
  five-shape enumeration. Ownership map unchanged (1.2 owns the canonical helper text in autogen.py +
  the server emit; 1.3 owns the scene_runtime.luau mirror; disjoint, serial 1.1→1.5, acyclic).
  Classification: Mechanical (corrects a scan-class/root-resolution miss against verified real
  stamping).

- **Shape enumeration hardened (§0b) — addresses the recurring under-modeled-shape failure.** Every
  real shape a dynamic `_UnityMass` assembly takes in converter output is enumerated and each stage of
  the (scan → root-resolve → fact-resolve → skip-rules → tag) pipeline confirmed per shape, with the
  fact/`_UnityMass` location verified against scene_converter.py:
  - **S1 non-wrapped BasePart** — `_UnityMass` + facts BOTH on the BasePart (2797/2808/2810/2814; wrap
    guard 2141 not hit, no children-needing-wrap). Scan: yes. Facts: on root.
  - **S2 mesh-wrapped BasePart under Model** — `_UnityMass` MOVED to inner `*_Mesh` (2176-2183); facts
    STAY on outer Model. Scan: yes (inner part). Facts: ancestor-Model walk.
  - **S3 Model carrier** — `_UnityMass` + facts BOTH on the Model (1886 + 2024 + 2789-2814; wrap returns
    early at 2141). Scan: yes ONLY because class-agnostic. Root: representative-BasePart hop. Facts: on
    the carrier Model.
  - **S4 welded multi-part assembly** — each member carries its own `_UnityMass` (2797 per part); all
    resolve to one `AssemblyRootPart`; tag dedups to exactly one force (per-member scale heterogeneity a
    documented limitation, edge 15).
  - **S5 nested under a factless container Model** — inner carrier per S1/S3; the parent container (1886
    case (a)) carries NO facts, so carrier-first correctly never reads the empty container before the
    carrier.
  Classification: Mechanical (enumerates the real input space so coverage is reviewer-checkable).

## [phase-1 detailed design, dual-voice review r4] RE-SCOPE: correctable set = UNANCHORED BasePart-carriers; Model carriers correctly SKIPPED + test-harness fix

Four MAJOR findings resolved by ONE re-scoping insight (fixes 2 MAJORs) + two independent fixes.

- **THE RE-SCOPING INSIGHT (supersedes D-P1.11's "Model carrier IS corrected" framing).** A Model
  carrier (S3 no-mesh-parent at scene_converter.py:1886; S6 multi-sub-mesh at 1923) stamps
  `_UnityMass` + facts on the OUTER Model (2024 + 2789-2814), but its descendant BaseParts have NO own
  Rigidbody so they emit **Anchored** (RbxPart defaults anchored=True at core/roblox_types.py:178;
  Models emit no Anchored — luau_place_builder.py:478/553, rbxlx_writer.py:914-916; the no-rigidbody
  branch at scene_converter.py:2815-2816 also sets anchored=True). So a Model-carrier's converted body
  is **effectively STATIC — there is no free-fall to correct**, and the EXISTING skip-if-anchored rule
  already handles it (resolve representative part → Anchored → skip, no force, NOT tagged). NO
  Model-carrier correction machinery is needed or possible (correcting one needs a SEPARATE converter
  change lowering the parent-Rigidbody state onto a real unanchored descendant BasePart — followups).
  FIX: (1) re-scope the correctable set to UNANCHORED BasePart-carrier dynamic assemblies (S1/S2/S4/S5);
  keep the scan CLASS-AGNOSTIC (`GetAttribute("_UnityMass") ~= nil` — cheap, and admits the future
  follow-up automatically) but make explicit that a Model carrier (S3/S6) resolves an Anchored
  representative and is SKIPPED; (2) replaced the S3/AC8c/8d "corrected" claims with the accurate
  anchored-SKIP behavior (AC asserts a Model carrier gets NO force, on the structural/CI surface where
  checkable); (3) acknowledged BOTH Model-carrier provenances in §0b (S3 1886 AND S6 1923/1940), both
  stamping facts on the outer Model (2024/2789-2814) with anchored descendants → both skipped;
  (4) documented the converter dynamic-carrier follow-up. This feature does NOT regress these carriers
  (static today regardless of gravity).
  Classification: Mechanical (right-sizing / scope correction — stops over-reaching on a
  genuinely-separate edge case; verified against the real converter output shape).

- **FIX A (test harness) — was MAJOR.** The behavioral-force ACs (AC1-8/8b/8c/8d/15) assumed the helper
  could be EXECUTED in the repo's bare `luau` interpreter, but its only behavioral harness
  (test_scene_runtime_host_behavior.py::_run_scenario, _harness_preamble lines 86-276) mocks ONLY the
  host SERVICE surface (task/warn/workspaceFind/findFirstChildWhichIsA/signals/clone helpers) — NO
  `workspace.Gravity`, NO `Instance.new`, NO `Vector3`, NO `Enum`, no injection seam. The force ACs are
  thus unbuildable as behavioral tests, and no Roblox-API execution harness exists (must NOT be
  invented). FIX: reclassify the force ACs as STRUCTURAL SOURCE assertions over the emitted Luau text
  (force formula, class-agnostic `_UnityMass` scan, skip-rules, `_ScaleGravityCorrected` tag,
  one-force-per-root) in the repo's existing `assert "<token>" in source` style
  (test_scene_runtime_host_emit.py:70-98; test_plan_emits_player_signal.py). Pure-Python ACs (emit
  gate, scalar parse, plan-field survival, zero-gravity, subphase order, parity, idempotency) stay real
  pytest. Force-APPLICATION behavior (net accel, resting/auto-sleep, replication) moves to the Studio
  acceptance §3.2 (run from MAIN), per the high-level CI-structural-vs-Studio-behavioral split.
  Classification: Mechanical (corrects an unbuildable test classification against the real harness).

- **FIX B (defer path) — was MAJOR (Claude) + Codex note.** The S3 defer-without-tag "DescendantAdded
  re-fires for child parts" recovery was FALSE — child parts lack `_UnityMass` so the scan never
  re-triggers on them. Since `Clone()` yields a fully-materialized subtree BEFORE parenting
  (autogen.py:881/1035), the client clone-site hook resolves the representative immediately against the
  complete subtree, and the boot sweep runs against the fully-assembled tree — no defer needed for part
  PRESENCE (the deferred frame at edge #1 remains, but only for multi-part WELD settle). FIX: dropped
  the unsound defer-recovery contract; kept at most a defensive one-shot skip with no false "will
  retry" promise.
  Classification: Mechanical (removes an unsound recovery contract).

- **KEPT (verified clean, unchanged):** early stash in plan_scene_runtime; AC16 allowlist hop; Python
  is-None zero-gravity consumer + AC10b; abs(m_Gravity.y); SUBPHASE_ORDER pin; RbxScript.parent_path
  routing; the class-agnostic scan + Model→PrimaryPart/first-BasePart→AssemblyRootPart resolution;
  root-first-then-ancestor fact resolution for S1/S2; the conflict-free acyclic re-slice. Re-checked:
  the slice ownership map + no-cycle still hold — the re-scope REMOVES machinery and adds no files.

## [phase-1 detailed design, scope-correctness revision] Two P1 fixes (2D exclusion + AssemblyMass skip-vs-tag)

- **D-P1.12 (NEW) `Rigidbody2D` carriers EXCLUDED (Physics2D OOS); in-scope 3D contract is
  `UseGravity` on/off with `GravityScale` treated as 1.0 — was MAJOR (2D scope hole).** Verified vs the
  worktree: `_UnityMass` is stamped class-agnostically at scene_converter.py:2797 BEFORE the 2D/3D
  split (both `Rigidbody` and `Rigidbody2D` feed one `rigidbody_props` at line 2475
  `elif ct in ("Rigidbody", "Rigidbody2D")`), so the scan catches BOTH 2D and 3D bodies. The 2D/3D
  discriminator is `scene_converter.py:2805`: `if "m_GravityScale" in rigidbody_props:` is the
  **Rigidbody2D** branch (stamps `GravityScale` at 2808, `UseGravity=False` at 2810); the `else:` at
  **2811** is the **3D Rigidbody** branch (stamps ONLY `UseGravity=False` at 2814, NEVER `GravityScale`
  — source comment at 2804 confirms: "Rigidbody: m_UseGravity (bool), Rigidbody2D: m_GravityScale
  (float)"). So the design's advertised "GravityScale=k scales the target" was actually the 2D path; a
  3D DynamicsManager correction applied to a 2D body is unsound. FIX: (1) stamp `_Rigidbody2D=True` at
  the 2805 Rigidbody2D branch (scene_converter.py, owned by slice 1.1 — a NEW single-owner file); the
  3D `else` is left unflagged; (2) every runtime scan surface (boot sweep, server `DescendantAdded`,
  client `_correctClonedDynamics`) AND the Python emit-gate predicate
  (`_part_tree_has_dynamic_unitymass`) SKIP any `_Rigidbody2D` carrier (don't count it, don't correct
  it); (3) the in-scope (3D) force contract reads `UseGravity` on/off (gravityScale = 0 off / 1.0 on);
  the helper keeps a defensive `gravityScaleAttr or 1.0` read but it is DEAD for in-scope 3D (always
  nil). Updated: top correctable-set scope, §0 anchor table (new 2D/3D-split + `_Rigidbody2D`-stamp
  rows), §0b prose + per-stage coverage, §1.2 emit-gate predicate, §1.4 server scan surfaces, §1.5
  force formula + fact-read prose, §1.6 client hook, edges 4/15 (reframed) + new edge 16, ACs 8e (skip
  — server scan/emit-gate + client token) and 8f (the upstream stamp branch), AC7 (reframed to
  UseGravity on/off), Studio S2 (gravityScale=1.0), §6 divergence D1b, slice 1.1 owns + ownership map.
  Classification: Mechanical — scope correctness (keeps Physics2D genuinely out of scope; verified
  against the real 2D/3D stamping branch).

- **D-P1.13 (NEW) `AssemblyMass <= 0` is SKIP-WITHOUT-TAGGING (single behavior) — was minor
  inconsistency.** The prior design said early-return skip in one place and "still tagged" in another. A
  zero/negative-mass assembly yields `force = mass*(...)` = 0 (a harmless no-op), so applying gains
  nothing; skip-without-tag keeps the body correctable if its mass later becomes valid. Aligned: helper
  step 2, edge 11, and new AC5b — all to skip-without-tag. (The `UseGravity=false` full-cancel case,
  edge 3, IS still tagged — it is corrected with a full-cancel force — a distinct case.)
  Classification: Mechanical — resolves an internal inconsistency.

- **Ownership map re-confirmed acyclic with `scene_converter.py` added.** `scene_converter.py` is owned
  by exactly ONE slice (1.1) — single writer, no overlap; it adds NO new dependency edge (no consumer
  slice; the runtime reads the stamped `_Rigidbody2D` attribute at RUNTIME, not at build time). Serial
  chain unchanged: 1.1 → 1.2 → 1.3 → 1.4 → 1.5. `pipeline.py` + `autogen.py` still each touched by
  exactly two serialized slices (1.1 then 1.2). No cycle.

- **Phase-1 design revision (dual-voice P1): `_Rigidbody2D` added to the mesh-wrap move-list so the
  2D-exclusion discriminator co-locates with `_UnityMass` on the inner carrier.** Root cause: the
  per-carrier `_Rigidbody2D == nil` exclusion (every scan surface + the Python emit-gate) checks the
  SAME instance the scan keys on (`_UnityMass`), but the mesh-wrap move-list
  (`_wrap_geometry_with_children_into_model`, scene_converter.py:2176-2183) moved ONLY `_UnityMass`
  (plus `_Scale*`/`_MeshId`/`_MeshFileId`/`_TextureId`/`_FbxImportScale`) to the inner `*_Mesh`,
  leaving `_Rigidbody2D` on the outer Model. An S2 wrapped 2D body therefore presented an inner
  carrier with `_UnityMass` but NO `_Rigidbody2D` → boot sweep, server DescendantAdded, client clone
  correction, and `_part_tree_has_dynamic_unitymass` all misclassified it as in-scope 3D.
  Fix: add `"_Rigidbody2D"` to the literal move-list tuple at scene_converter.py:2177-2182 (verified
  contents: `_attr_key.startswith("_Scale")` PLUS `("_MeshId", "_MeshFileId", "_TextureId",
  "_FbxImportScale", "_UnityMass")`, popped to `inner.attributes` at 2183) — owned by slice 1.1 which
  already owns scene_converter.py (the `_Rigidbody2D` stamp), so no new ownership/cycle impact (single
  writer, serial chain 1.1→1.5 unchanged). Design updated: §0 anchor rows, §0b S2 row + scan-coverage
  bullet + correctable-set prose, §1.2 emit-gate docstring, edge 16, D1b, D-P1.12, AC8e (wrapped-2D
  coverage on every surface + the emit-gate's wrapped-2D tree), AC8f (move-list co-location
  regression-pin: wrap a 2D body, assert inner carries both `_UnityMass` and `_Rigidbody2D`), slice 1.1
  scope + ownership map.
  Classification: Mechanical

## [design-review r7, Claude voice] Pin _Rigidbody2D stamp UNCONDITIONAL in the 2D branch + default-scale AC fixture
The _Rigidbody2D stamp must be set UNCONDITIONALLY at the top of the `if "m_GravityScale" in
rigidbody_props:` branch (scene_converter.py:2805), NOT nested under `if gravity_scale != 1.0`. A
default-scale Rigidbody2D (m_GravityScale==1.0) stamps neither GravityScale nor UseGravity, so
_Rigidbody2D is its SOLE discriminator from a 3D body; nesting would leak default-scale 2D into the 3D
correction. AC8f's fixture must use m_GravityScale==1.0 (a !=1.0 fixture passes even with the buggy nested
placement — green-test-for-the-wrong-reason). Classification: Mechanical (spec precision; my codex pd7
said no-P1 but missed this — caught by the Claude voice).

## [slice 1.1 implementation] Project-gravity parse + 2D discriminator + plan stash (DONE)
- project_gravity.py: targeted m_Gravity-line regex anchored on `^\s*m_Gravity:` (does NOT collide
  with m_ClothGravity), per-axis component extraction; returns abs(y); 9.81 default on missing
  file/field; warn+fail-open to abs(y) on non-zero x/z. `warn` is an injectable callable (default
  log.warning) so tests assert warnings without capturing logging. Never raises.
- All cited real-code lines confirmed accurate in the worktree (scene_converter 2797/2805/2811,
  move-list 2176-2183, pipeline plan_scene_runtime ~998-1048 w/ self.unity_project_path:Path +
  self.ctx.scene_runtime at 1048, autogen _PLAN_KEYS_FOR_HOST 638, config.STUDS_PER_METER=3.571).
  No drift; no REDESIGN.
- config is imported in pipeline.py as `_config` (line 26); used `_config.STUDS_PER_METER`.
- _plan_to_luau encodes float via repr(); zero (0.0) survives because the key is present in the dict
  (the `if k in scene_runtime` filter passes) — verified by AC16 zero test.

## [slice 1.2 implementation] Server gravity-correction script + emit subphase + gate (DONE)
- autogen.py: `_GRAVITY_CORRECTION_HELPER_LUAU` canonical helper text constant + `generate_gravity_correction_server_script(desired_g_studs_base)` returning the baked Luau SOURCE STRING (boot sweep + DescendantAdded hook, class-agnostic _UnityMass scan with _Rigidbody2D==nil exclusion). The generator returns `str` (the pipeline wraps it in the RbxScript with parent_path) — design §1.4 signature is `-> str`; the RbxScript with parent_path="ServerScriptService" is built in the pipeline subphase per §1.3. Baked literal uses `repr(float(...))`.
- pipeline.py: `_part_tree_has_dynamic_unitymass` (staticmethod, recursive over children; numeric-and-not-bool _UnityMass + _Rigidbody2D is None) and `_subphase_inject_gravity_correction` (reads 1.1 stash with `is None` guard, falls back to project_gravity.DEFAULT_UNITY_GRAVITY_Y * STUDS_PER_METER; idempotent _replace_or_add-style dedup on the autogen marker; user-named-script guard). Added to SUBPHASE_ORDER + write_output immediately after _generate_prefab_packages. Imported RbxPart into pipeline for the predicate annotation.
- All cited anchors confirmed accurate in the worktree (SUBPHASE_ORDER 2557, write_output 2686, _generate_prefab_packages 5971, _subphase_inject_scene_runtime _replace_or_add marker pattern, RbxScript.parent_path/RbxPlace.scripts, scene_converter stamp@2820 + move-list@2181-2187, 1.1 stash@1060, _PLAN_KEYS_FOR_HOST@645). No drift; no REDESIGN.
- AC13: the existing subphase-order test auto-extracts the call sequence and would pass on its own; added an explicit positional test class (TestGravityCorrectionSubphasePosition) asserting the subphase sits immediately after _generate_prefab_packages in BOTH SUBPHASE_ORDER and write_output.
- Syntax smoke (AC10) uses utils.luau_analyze.syntax_errors_for_source (filters Roblox-API TypeErrors to SyntaxError only), skipif when luau-analyze absent.
- Slice-local 42 tests green; full non-slow suite 3338 passed / 0 failed; no-Any gate pass.

## [phase-2 detailed design] Canary surface, fixture strategy, Studio acceptance + perf bounce-back

Decisive grounding (verified vs real code/fixtures): (a) the existing `contract_corpus` infra captures
the WRONG artifact — `tools/regen_contract_corpus.py` aborts at `_run_contract_verifier` (BEFORE
`write_output`/the gravity subphase) and captures only `{topology, scripts}`, never `workspace_parts`/
`replicated_templates` or the `SceneGravityCorrection` script; (b) the bundled corpus has NO dynamic 3D
bodies (MiniNet = 0 Rigidbody; SimpleFPS = unpopulated, `conftest.simplefps_project` skips); (c) the
VectorForce+Attachment children are RUNTIME Luau instances, never serialized into the emitted place — so
the only EMITTED gravity delta is the script + the `_Rigidbody2D` stamp + the plan field.

- **D-P2.1 Gravity canary = a NEW targeted-delta pytest driving the REAL
  `_subphase_inject_gravity_correction` against a constructed representative `RbxPlace`, NOT a new
  `contract_corpus` fixture.** The contract-corpus capture point is upstream of the gravity emit (wrong
  surface). Classification: Mechanical (verified against the capture point).
- **D-P2.2 Canary fixture built IN-TEST (constructed post-stamp `RbxPlace`), not an on-disk Unity
  project/submodule.** Bundled corpus has no dynamic 3D bodies; the gravity delta is downstream of the
  deterministic `_UnityMass`/`_Rigidbody2D` stamping (already pinned upstream by Slice-1.1 tests), so a
  constructed place is faithful AND always-runs in CI (no skip, no AI run). Classification: Mechanical /
  right-sized.
- **D-P2.3 Targeted before/after-DELTA assertions, NOT a frozen whole-place snapshot.** A golden
  snapshot re-baselines on every unrelated converter change (review churn, over-design); targeted delta
  pins exactly the surprise-break classes (appears-when-shouldn't / vanishes / mutates-tree /
  wrong-scalar) at zero re-baseline cost. Classification: Taste / right-size.
- **D-P2.4 The VectorForce+Attachment "instance-tree delta" is asserted in STUDIO (S2/S4), not in the
  emitted place; CI pins the EMITTED delta (script + no-emit cases) AND that the part trees are
  UNCHANGED by the subphase.** Forces are runtime-only (never in the rbxlx). Inherits Phase-1's
  CI-emitted vs Studio-runtime blind spot. Classification: Mechanical.
- **D-P2.5 Studio fall-rate tolerance = ±10 % around 35.03 studs/s² (= 9.81*3.571), with a
  fall-distance cross-check; the ~5.6× corrected/uncorrected separation is the discriminator.** Tight
  enough to separate corrected from default 196.2, loose enough to absorb integrator/sampling jitter.
  Classification: Mechanical.
- **D-P2.6 Perf budget = ≥2000 simultaneously-falling assemblies, sustained server `dt ≤ 1/30 s`; a
  blown budget is a PRIMITIVE failure that BOUNCES the mechanism to Phase 1, NOT a Phase-2 patch.**
  Symmetric with the resting/sleep gate; patching in Phase 2 would mask an unviable lever.
  Classification: Mechanical (honors the phase boundary).
- **D-P2.7 Slice 2.2 (on-disk GravityProps real-scene fixture) is CONDITIONAL on review.** Default is
  the constructed place (2.1); the extra on-disk fixture is built only if review judges a constructed
  `RbxPlace` insufficiently real. Classification: Taste / right-size (build on evidence of need).
- **D-P2.8 No Phase-2 slice mutates a Phase-1 file** — the safety net reads Phase-1 producers only, so
  it cannot regress the mechanism it guards. Classification: Mechanical.

## Slice 1.3 (client clone-site gravity hook) — implementation decisions

- **D-1.3a Mirrored helper embedded in a `do ... end` block, captured into a module-scope upvalue
  `_gravityCorrectDynamicAssembly`.** scene_runtime.luau already has a module-level `_isBasePart`
  (line 373) whose definition DIFFERS from the canonical helper's `_isBasePart`. To keep the canonical
  text token-identical (slice 1.4 parity) without redefining/shadowing at module scope, the canonical
  `factOf` + `correctDynamicAssembly` (+ their local `_isBasePart` and `local TAG`) are pasted verbatim
  inside a `do` block; `correctDynamicAssembly` is captured into the upvalue the method calls. The
  module's pre-existing `_isBasePart` is untouched.
- **D-1.3b `_correctClonedDynamics` guards the clone's Instance API before indexing** (mirrors the
  applyImpulse:1515 indexability guard): `type(clone) ~= table/userdata or type(clone.GetAttribute) ~=
  function or type(clone.GetDescendants) ~= function -> return`. Without it the existing host-behavior
  harness (which returns a plain-table fake clone from `clonePrefabTemplate`) hard-errored on
  `clone:GetAttribute`. Production clones are always real Instances; this is defensive parity with the
  existing applyImpulse guard, not new behavior.
- **AC15/AC8d/AC8e/AC14 are STRUCTURAL source-token assertions** (per design FIX A — no Roblox-API
  execution seam in standalone luau); the test file also runs a luau loadstring syntax smoke test on
  the emitted scene_runtime.luau. Behavioral force application is the Studio acceptance (S2/S4).

## [slice 1.3 review fixes] Defer clone-site correction (weld-settle) + pin negative invariant
- **[MAJOR] Clone-site correction now DEFERRED.** `_correctClonedDynamics` routes each per-carrier
  correction through `self._services.task.defer(...)` (a local `correct(inst)` closure) instead of
  calling the helper synchronously. Mechanism CONFIRMED to match the server spawn-hook settle
  semantics: the server `SceneGravityCorrection` DescendantAdded handler emits
  `task.defer(function() correctDynamicAssembly(d, DESIRED_G_STUDS_BASE) end)`
  (autogen.py:1305-1310); `self._services.task.defer` is the runtime's established deferral primitive,
  already used by `_runAwakeEnableStart`'s Start-flush (scene_runtime.luau:1282) and mocked in the
  host-behavior harness (test_scene_runtime_host_behavior.py:101). The deferred frame lets welds settle
  on S4 multi-part clones before `AssemblyRootPart`/`AssemblyMass` are read; the `_ScaleGravityCorrected`
  tag on the resolved root makes correction exactly-once across the deferred clone-site path AND the
  server DescendantAdded path for a server-side instantiatePrefab clone (this file runs on both domains).
  Did NOT add an isClient gate (the bonus): the runtime exposes no clean client/domain flag at the clone
  site, and the defer + tag already yields exactly-once — the gate is unnecessary.
- **[P2] Negative invariant pinned.** Added `test_runtime_has_no_blanket_workspace_descendantadded_
  gravity_sweep` asserting no `DescendantAdded:Connect` handler in scene_runtime.luau sits near a
  gravity marker (`_gravityCorrectDynamicAssembly`/`_ScaleGravityCorrected`/`gravityDesiredBaseStuds`/
  `correctDynamicAssembly`). Scoped to actual `:Connect` WIRING (not comment mentions) and to gravity
  context, so it doesn't false-trigger on the unrelated PlayerGui/character DescendantAdded hooks.
- **[NIT, addressed]** Replaced the fragile `method_idx + 2000` test windows with a `_method_body()`
  helper bounding on the next `\nfunction SceneRuntime:` (or EOF), so assertions stay precise and don't
  bleed into the following method.
- Helper text kept token-identical to autogen's `_GRAVITY_CORRECTION_HELPER_LUAU` (1.4 parity untouched
  — only the method WRAPPING the helper call changed, not the helper). Luau loadstring smoke + the
  scene_runtime host/apply-impulse/emit suites all green; no-Any gate pass.

## [phase-2 detailed design, dual-voice review r1] Three MAJOR P1 fixes + right-size trim

- **D-P2.10 No-emit cases re-framed on `_UnityMass` PRESENCE, not on anchored — was MAJOR.** The emit
  gate (`_part_tree_has_dynamic_unitymass`, pipeline.py:6315) keys on numeric non-`_Rigidbody2D`
  `_UnityMass` and never reads `anchored`; `_UnityMass` is stamped ONLY on unanchored bodies
  (scene_converter.py:~2803, verified in the worktree). So anchored is a RUNTIME skip, not an emit-gate
  concern. FIX: the canary's NO-EMIT cases are (a) a place with NO `_UnityMass` anywhere (P-abstain-none
  — the all-anchored/static case is THIS) and (b) a `_Rigidbody2D`-only place (P-abstain-2d). An anchored
  body becomes an `_UnityMass`-absent POSITIVE-emit-context sibling in P-mixed, proving the emit subphase
  does NOT mutate the part tree. Removed all "all-anchored ⇒ no emit because anchored" framing.
  Classification: Mechanical (corrects the gate semantics against verified real code).

- **D-P2.11 Single representative fixture SPLIT into separate constructed places — was MAJOR
  (internally impossible).** One fixture cannot be both mixed-workspace-dynamics (S1/S2/S4/S5 in
  `workspace_parts`) AND prefab-template-only (dynamics only in `replicated_templates`, none in
  workspace). FIX: build SEPARATE constructed `RbxPlace`s in-test (the `test_gravity_correction_emit.py`
  pattern, NOT a shared fixture): P-mixed (multi-shape positive), P-prefab (prefab-template-only union,
  C5), P-abstain-none (no `_UnityMass`), P-abstain-2d (`_Rigidbody2D`-only incl. mesh-wrapped 2D). Each
  gets its own builder + assertion; no single `_assert_fixture_shapes(place)` proves all cases.
  Classification: Mechanical (resolves the contradictory fixture contract).

- **D-P2.12 Slice 2.2 (on-disk GravityProps real-scene fixture) DROPPED entirely + ownership fixed —
  was MAJOR (over-build + non-disjoint + mis-ordered).** Old 2.1 and 2.2 both owned
  `test_gravity_correction_canary.py` (not disjoint), and 2.2's `convert_scene → _subphase` re-prove
  mis-ordered the pipeline (prefab templates exist only after `_generate_prefab_packages`). FIX: drop
  2.2; the canary (2.1) driving `_subphase_inject_gravity_correction` directly with the
  prefab-template-only place covers the scene ∪ prefab union without a real on-disk `convert_scene`
  fixture. Phase 2 = {Slice 2.1 canary, Slice 2.2 Studio acceptance (renumbered from 2.3)}; ownership map
  is now single-owner-per-file, disjoint, acyclic (2.2 deps 2.1; no Phase-1 file mutated).
  Classification: Mechanical (corrects ownership/ordering) + Taste/right-size (drops the over-build).

- **D-P2.13 Canary trimmed to its genuinely-new wide-blast-radius value — right-size (P2).** C1/C2/C5/
  C6/C7/C8 substantially duplicated Phase-1's single-shape `test_gravity_correction_emit.py`. KEPT the
  new value: C3 (script-name symmetric diff = only `SceneGravityCorrection` added vs a no-gravity
  baseline), C4 (part-trees unchanged by the emit subphase), the multi-shape anti-vacuity assertion
  (S1/S2/S4/S5 present + corrected in one place), the prefab-template-only union case (C5), and the two
  `_UnityMass`-keyed no-emit cases. DROPPED the baked-scalar parse (C2), the mode gate (C7), and the
  idempotent re-run (C8) — already pinned by Phase-1. The canary's PURPOSE is the wide-blast-radius
  regression pin, not re-proving single-shape emit.
  Classification: Taste / right-size (keep the new pin, drop the re-test).

- **KEPT (verified true by both voices, unchanged):** corpus has no dynamic bodies (the canary brings
  constructed places); `contract_corpus` captures only pre-`write_output` `{topology,scripts}` (the
  canary drives the emit subphase directly); VectorForce/Attachment are runtime-Luau-only (CI pins the
  emitted SCRIPT delta, Studio covers the force tree); the CI-vs-Studio AC split; the perf
  bounce-to-Phase-1 boundary; and all Studio acceptance procedures (resting/auto-sleep gate, ±10% band
  around fall-rate ≈35.03, UseGravity=false float, server-force replication, ≥2000-body perf budget with
  bounce-to-Phase-1).

# === Run hud-values-generic-20260616T161253 (2026-06-16) ===

## [2026-06-16T08:19:46Z] Plan-stage review right-sized to dual-voice design review (not full autoplan gauntlet)
**Decision:** Run the load-bearing dual-voice design review (Claude reviewer + codex exec on design.md)
as the plan convergence gate; SKIP the full autoplan CEO→Design→DX product lenses.
**Classification:** Mechanical (proportionality).
**Rationale:** This is an internal converter bug-fix design (generic-mode HUD lifecycle + UI-control
lowering). The CEO (strategy/market), Design (product UX), and DX (developer onboarding) lenses have
near-zero signal for a pipeline fix; the Eng/soundness lens is what matters and is fully covered by the
dual-voice design review. Right-size-at-design: review rigor matched to stakes.

## [2026-06-16] Phase-1 detailed design — root-cause localization decisions
- **D1 — Reframe (d) from "duplicate BindableEvent instances" to "Awake ORDERING race over a shared field."**
  Both Player + HudControl are client-domain on one VM sharing the cached `Player` table (instances ARE shared);
  the bug is HudControl:Awake (scene batch, scene_runtime.luau:3079) running BEFORE Player:Awake (prefab batch,
  :3209) → reads nil `Player.*Update` → connects skipped. Kills Cur/Health/item updates. Root-cause correction.
- **D2 — Build the (d) channel-identity fix UNCONDITIONALLY; gate the construction/host fix on runtime
  instrumentation.** (d) decisive in code. Lead blank-`Total` (a static module, no event) is NOT static-decisive
  — 3 surviving sub-mechanisms (Awake-never-runs / clone-descendants-absent-at-Awake / second-clone-visible);
  one instrumented playtest (design-phase1.md §5) discriminates before writing host-resolution code.
- **D3 — Default ONE slice** (shared boot-order contract, small, no fan-out). 2nd slice opens only if §5 shows
  a respawn re-clone rebind (scene_runtime.luau:2861-2863, already a documented followup).
- **D4 — Prefer runtime pre-pass (`event_channels` + `_ensureSharedEventChannels`) over transpile-time hoist**
  unless implement step-0 finds the consumer reads the module FIELD (not an RS child), forcing producer-field
  pre-set or a module-scope channel hoist. Deterministic lever over the byte-frozen AI prompt.
- **Mechanism verdicts:** (b) clone-never-lands REFUTED (no warn); (a) module-never-constructs REFUTED static
  (Player in RS → require resolves); (c) Awake-on-template REFUTED static (PlayerGui present → workspaceFind
  never scans StarterGui); (d) ordering race CONFIRMED decisive.

## [2026-06-16T09:04:08Z] Phase-1 fix mechanism (post dual-voice r1)
- Reject runtime RS-child pre-pass (consumer reads module FIELD, not RS child — dead-end) AND naive module-scope hoist (generic contract forbids top-level side effects: code_transpiler.py:1325, runtime_contract.py).
- Adopt: lower C# static events into a generated static `__ensureStaticChannels()` via a DETERMINISTIC contract_pipeline.py span-move; runtime invokes it before the Awake batches (contract-legal — runtime holds game). Planner flags modules with static channels (new schema).
- Split Phase 1 into 2 slices: 1.1 channel-identity (offline-buildable now), 1.2 host-resolution/Total (gated on §5 Studio probe).

## [2026-06-16] Slice 1.1 implementation decisions
- **D1.1-a — C# static-event enumeration site:** `script_analyzer.analyze_script` (new `ScriptInfo.static_events: list[str]`), regex `_RE_STATIC_EVENT` over the decommented source. Member name = the last identifier in `public static event <HandlerType> <Member>;`. SimpleFPS Player.cs surfaces 4: HealthUpdate/AmmoUpdate/ItemUpdate/PauseEvent. Deterministic C# parse — never the AI Luau.
- **D1.1-b — `static_channels` computed in `_subphase_inject_scene_runtime` (write_output, generic-only), NOT in `plan_scene_runtime`.** `field_name`/`channel_name` come from the C# member; `module_path`/`domain`/`container`(→parent_path) are only FINAL after storage-classify, which the planner phase predates. Mirrors `_build_scriptable_object_module_map`. Gated to same-domain (the channel is a same-VM BindableEvent field; cross-domain routes via RemoteEvents, out of scope). parent_path = the module's own container (RS for client cross-script) so the BindableEvent lives beside the module.
- **D1.1-c — runtime find-or-create via a new `findOrCreateChannel(channelName, parentPath)` service** (autogen client+server tables), not direct `game` access in the runtime (the runtime is service-abstracted + harness-testable). `_ensureStaticEventChannels()` called from `start()` after Phase-0 module collection, before the scene loop. Sets `mod[field] = findOrCreateChannel(...)`; the AI's untouched `Player.X = Player.X or (...)` short-circuits. Idempotent: find-by-name returns the existing instance.
- **D1.1-d — fail-closed rendezvous verifier = a new check in `contract_verifier.verify_contract` (3rd param `static_events_by_script`), keyed on the C# static-event list, NOT the AI Luau.** Fed by `pipeline._static_events_by_emitted_name`, which applies the SAME same-domain/runtime-bearing/stamped-module_path gate as the channel builder (so it never demands a rendezvous for a channel the runtime won't pre-set).
- **D1.1-e — the verifier blesses ONLY the lazy-init GUARD shape `<Module>.<Field> = <Module>.<Field> or (...)` [post dual-voice r-impl].** EMPIRICAL (real LLM cache): the producer emission varies — `X = X or (IIFE)`, `X = X or ensureEvent("X")`, `X = ensureEvent("X")` (no `or`), `self:_playerEvent("X")` (local helper, no field assignment). The runtime field-pre-set is load-bearing ONLY when the producer's `or` SHORT-CIRCUITS onto the pre-set instance. An UNCONDITIONAL reassignment (`X = ensureEvent(...)` / `X = Instance.new(...)`) can OVERWRITE the pre-set instance with a fresh one and disconnect already-bound consumers (codex finding; `ensureEvent` happens to re-find under RS for SimpleFPS, but the verifier can't prove the create-expr re-finds the SAME name+parent generically) → fail-closed. The local-helper shape never assigns → fail-closed. The verifier ALSO strips string literals (not just comments) so a rendezvous inside `"..."` can't bypass (codex adversarial finding). Known lenient gaps (fail-OPEN noise, documented in followups, not fixed in 1.1): (1) an aliased read `local ev = X; ev:Connect(...)` warns spuriously; (2) a multi-declarator `static event H A, B;` surfaces only the last name.

## [2026-06-16T11:22:14Z] Slice 1.1 converged (3 review rounds); codex r3 P1 overruled at integration
Both round-2 P1s fixed (structured per-module channel identity + module-id-keyed verifier feed). codex r3
raised a new P1 (consumer RS-name-lookup breaks folderization) — REFUTED against the real rbxlx (consumer
reads the module field, not an RS lookup); overruled with evidence, residual logged to followups. Claude r3
CONVERGED. Classification: refute-adversarial-finding-at-integration.

## [2026-06-16T14:17:10Z] LIVE-PROBE root-cause refinement + scope decision (B)
- Instrumented Studio probe (SimpleFPS-HUDPROBE.rbxlx) showed: HudControl:Awake RUNS on the live PlayerGui
  clone; Module/Ammo/Total resolve; maxAmmo=250; Player.AmmoUpdate already set. User confirms **max ammo (Total)
  renders DETERMINISTICALLY** → the designed "blank-Total / host-resolution" bug (Slice 1.2) is REFUTED. **Slice
  1.2 DROPPED.** (Per OPERATING: reproduce the actual symptom live before building; found a real race but not
  THE Total symptom.)
- Confirmed-real remaining HUD bugs: (1) current-ammo (Cur) update path rides the scene-vs-prefab Awake
  ORDERING RACE (nondeterministic — probe won it this run; design proved it loses on others) → **Slice 1.1**
  (channel identity, CONVERGED) makes it deterministic. KEEP. (2) item-pickup **checkmarks never show**:
  UpdatePlayerItems sets SetAttribute("isOn") with no reader driving Checkmark visibility = Unity Toggle→no
  visual binding → **Phase 2**.
- **Scope = (B):** Phase 1 (Slice 1.1, current-ammo determinism) + Phase 2 (generic Toggle isOn→Checkmark
  visual binding). Health-fill (Slider→fill, same lowering class) DEFERRED to followups.
## Run turret-bullet-damage-20260616T101918 (2026-06-16T08:27:13Z) — turret bullets: damage instead of push

- **Reported bug:** turret/plane bullets hit the player but deal no damage and shove them.
- **Root cause:** AI transpiler emits a call to a NON-EXISTENT Roblox method
  (`char:FindFirstChildOfType("Humanoid")`), syntactically valid so the luau-analyze gate
  (syntax-only) misses it; it throws at runtime, aborting the Touched handler before both
  TakeDamage and host.destroy(bullet) → no damage + lingering bullet shoves the player.
- **Scope decision (user-driven):** fix the CLASS (hallucinated Roblox API calls), not just
  the one name. Rejected: (a) one-off rewrite map (whack-a-mole); (b) typed analyzer
  (spike: any-typed output defeats it, no luau-lsp/typedefs); (c) blunt name-matcher
  (false-positives on valid custom/component methods).
- **Chosen approach:** provenance-gated validator — a new semantic gate beside `_lint_and_fix`
  that validates a method name against a vendored callable-only Roblox API corpus ONLY when the
  receiver is provably a Roblox instance (workspace/game:GetService/Instance.new/.Parent/
  .Character/FindFirstChild* + local-alias propagation; self/self.host/require skipped).
  `.Parent`/`.Character` mark their result Roblox unconditionally (catches the bug). Invalid
  call on a proven receiver → auto-fix (known pairs) or hard reprompt, FAIL-CLOSED if it
  survives; unproven receiver → soft warning. Corpus = callable members only.
- **User affirmed** the "list of proven Roblox APIs" (the corpus) as the backbone, paired with
  the provenance gate for safety.
## Run addressables-unit1-20260615T133903 (Addressables Unit 1) — 2026-06-15T10:05:21Z

## Plan stage (2026-06-15)

- **D1: Unique prefab key = deterministic upstream prefab_id (`<guid>:<path>`)**, not a
  fingerprint of generated output. Classification: Taste (architecture). Rationale:
  anchors load-bearing identity on the stable input; resolver already keys
  scene_runtime.prefabs by it. On-disk Templates child name = sanitized `name__<guid6>`
  (mirrors existing duplicate class/SO `__<hash>` scheme).
- **D2: One drive phase, not three.** Classification: Mechanical (run structure). Unit 1
  is one coupled unit, not testable piecewise; integrate/review/harden as a unit. Slices
  (identity -> emission -> host resolve) defined by /drive-design phase 1.
- **D3: Scope = Unit 1 only.** Classification: User-Challenge (premise-stated). Units 2-4
  out of scope; Unit 1 is the validating spike.
- **D4: baseRef = feat/addressables-prefab-conversion @ a8add0b.** Run diff excludes the
  already-committed Phase-1 discovery parser. Classification: Mechanical.
- **D5: Right-sized the Plan stage — ran dual-voice design review (the P1 convergence
  gate), skipped the full gstack autoplan CEO/Design/Eng/DX ceremony.** Classification:
  Taste (process). Rationale: the source full-feature design was already CEO/Design/Eng-
  reviewed (2026-06-15); this run is a tight Unit-1 narrowing. Dual-voice review converged
  in 2 rounds (codex caught 2 P1s round 1: resolver->plan bridge missing from scope +
  by_address list semantics; both closed round 2). Surfaced at Gate A.

## Detailed-design stage — Phase 1 (2026-06-15) — ⚠️ SUPERSEDED by the REVISION section below

> D6 (unconditional) and D8 (self.state) below were OVERTURNED by dual-voice review
> rounds 1–2. The authoritative decisions are in the "Phase 1 REVISION" section. D7, D9,
> D10 carry forward unchanged. Kept here for the audit trail only.

- **D6: Unique Templates child-name = `f"{base}__{guid6}"`, unconditional, derived from the
  prefab_id's leading guid** (bare name only when guid empty). NOT the existing
  collision-conditional `resolve_unique_asset_names`/`_disambiguate_by_source` schemes.
  Classification: Taste (architecture). Rationale: two independent producers (prefab_packages
  emitter + planner `template_name`) must compute the IDENTICAL child name without sharing
  collision state; keying the suffix on the prefab's own guid makes the name a pure function
  of the single prefab. Mirrors the established `name__<hash6>` shape.
- **D7: Plan `addressables` block carries `by_address` + `by_label` only; NO `by_guid`.**
  Classification: User-Challenge (corrects design.md). Rationale: `PrefabAddressables`
  (resolver return) has no `by_guid`; the raw `AddressablesIndex.by_guid` is guid-keyed (not
  prefab_id) and the host never reads it. design.md's repeated `by_guid` mention is dropped.
- **D8: Resolver invoked in `pipeline.plan_scene_runtime`, parsed ONCE, `prefab_ids` stashed
  on `self.state` for the emitter.** Classification: Taste (architecture). Rationale: it's
  where guid_index + project path + artifact assembly already live; one parse feeds both the
  plan block and the emission target set.
- **D9: Multi-candidate address resolves to `ids[1]` (deterministic, resolver-ordered) +
  warn; empty/missing fails soft (nil + warn, never crash).** Classification: Taste.
  Rationale: the spike's `"Trash Cat"` is a singleton so the pick rule doesn't affect
  acceptance, but the ambiguous/absent contract must be defined.
- **D10: `by_label` is emitted into the plan but NOT consumed by `instantiatePrefab` in
  Unit 1** (labels = `LoadAssetsAsync`, a Unit-2 concern). Classification: Mechanical (scope).

## Detailed-design stage — Phase 1 REVISION (post dual-voice FINDINGS, 2026-06-15)

Review (Claude P1 + codex MAJOR) showed the UNCONDITIONAL rename of every Templates child to
`name__guid6` regresses currently-working paths: `template_name` / the bare prefab name is an
OVERLOADED shared join key (PrefabSpawner.spawn/variantChain, AI-emitted
`Templates:WaitForChild(name)`, the prefab-scoped animation attach, the `referenced_but_missing`
manifest, variant chains). Switched to collision-conditional disambiguation with a single source
of truth. Changed/new decisions:

- **D6 [REVISED → collision-conditional]: resolved Templates child name = the BARE base when that
  base is unique across the full prefab set; `base__guid6` ONLY when 2+ prefabs share a base.**
  Classification: User-Challenge (review-driven; corrects the prior unconditional rename).
  Rationale: keeps bare names (and every bare-name-keyed consumer) working for the non-colliding
  majority → ZERO regression; suffixes only colliding bases (character→character__473ffa /
  character__2ae64d) → fixes Cat/Raccoon. Mirrors `resolve_unique_asset_names` /
  `_disambiguate_by_source`.
- **D6b [NEW, refined R3]: SINGLE SOURCE OF TRUTH — `plan_scene_runtime` computes the resolved map
  over the EMITTED set (D14) and stores it in `prefabs[id].template_name`; the emitter sets
  `part.name` from that SAME stored value (read via `ctx.scene_runtime`), not an independent
  recompute. `_resolveTemplate` already reads `template_name` → no Luau change.** Classification:
  Taste (architecture). Rationale: kills the two-`_prefab_stable_id` divergence as a name-skew risk;
  resume-safe. Object-identity (`id(template)`) alignment REJECTED (in-memory ids don't survive
  resume).
- **D6c [NEW, refined R3]: reconcile ALL THREE `_prefab_stable_id` producers (planner +
  scene_converter + resolver) to ONE byte-identical form — `.as_posix()` rel + skip-on-outside-root/
  no-root (guid-less ids are always colon-free relative paths) + a coupled root arg. Emitter
  lookup-miss policy = WARN + SKIP for a colliding base, bare fallback for a unique base (NOT RAISE
  — see D15).** Classification: Taste (architecture) + fail-soft. Rationale: full 3-way unification
  makes a miss a defensive can't-happen path; the normalization also makes `partition(":")`
  guid-detection safe (no absolute `C:/…`). Flips AND removes the long-deferred xfail parity tests
  (`tests/test_prefab_packages.py:1364-1387`, pr2-followups §7).
- **D14 [NEW R3]: collision domain = the EMITTED set (referenced ∪ addressable), NOT the full
  library; selection factored into ONE shared `select_emitted_prefab_ids` predicate used by the
  planner (collision scope) AND the emitter (what to emit).** Classification: User-Challenge
  (codex R3). Rationale: full-library collision would suffix an emitted prefab over a non-emitted
  same-base sibling, breaking its working bare-name spawn. Computable at planner time
  (serialized_field_refs is set in `extract_assets` (phase 2), before `plan_scene_runtime`).
- **D15 [NEW R3]: emitter resolved-name MISS policy = WARN + skip (colliding base) / bare fallback
  (unique base), superseding D6c's RAISE.** Classification: fail-soft. Rationale: RAISE would crash
  a whole conversion on a defensive can't-happen miss; WARN+skip surfaces it without crashing and
  without cloning the wrong template. Also reframes AC8: with `plan_scene_runtime` essential, resume
  RECOMPUTES (not persistence read-back) — AC8 tests the recompute path.
- **D8 [REVISED]: resolver invoked in `pipeline.plan_scene_runtime`, parsed once; addressable
  `prefab_ids` + resolved map persisted in the artifact (`ctx.scene_runtime`), NOT transient
  `self.state`; `prefab_ids` kept OUTSIDE the Luau `_PLAN_KEYS_FOR_HOST` allowlist; AND
  `plan_scene_runtime` added to `ESSENTIAL_PHASES`.** Classification: Taste (architecture) +
  resume-correctness. Rationale: persistence alone is insufficient — a `--phase write_output`
  resume can pair a fresh `prefab_library` with a STALE persisted map (codex R2); making the
  planner essential forces a recompute ("save raw facts, recompute conclusions"). **The
  schema-version bump is DROPPED** — review rounds 1–2 confirmed there is NO scene_runtime
  schema/version field to bump (conversion_context.py); essential-recompute already prevents
  stale reuse, so no invalidation lever is needed.
- **D11 [NEW]: variant chains / `VariantParentTemplate` rekeying scoped OUT of Unit 1 with a
  documented limitation + a runtime uniqueness assertion (WARN if a variant-parent base collides).**
  Classification: Mechanical (scope). Rationale: variant metadata never carried prefab identity
  (codex Medium-4); fixtures + Trash Dash have no duplicate-base variant parents; Cat/Raccoon are
  not variants. Full fix (dual base+resolved fields) is a follow-up.
- **D12 [NEW → revised]: animation attach for a colliding base is UNSUPPORTED (skip + WARN), NOT
  over-attached.** Classification: User-Challenge (corrects an interim over-attach idea). Rationale:
  identity is lost upstream — `animation_converter` first-wins-collapses duplicate prefab names
  (`animation_converter.py:1734`) and `script_scopes` keeps only the bare scope, so no prefab_id
  survives by attach time (codex High-2). Attaching one prefab's driver to all colliding templates
  would mis-drive them. Non-colliding attach maps bare→resolved unchanged. Real fix (rekey animation
  scope on prefab_id upstream) is a follow-up.

## Execute stage — slice stacking (2026-06-15)
- **D16: Unit-1 slices are a LINEAR code-dependent chain (1.1→1.2→1.3→1.4), stacked — each
  slice branches from its dependency's converged tip, not phaseBaseSha; its review diff is the
  INCREMENTAL `<dep-tip>..slice/<id>`.** Classification: Mechanical (run structure). Rationale:
  1.2 imports 1.1's `resolve_template_child_names`/`select_emitted_prefab_ids`; 1.3 builds on
  1.2's planner block; 1.4 needs 1.3's emitted templates. Drive's "fresh-from-base" model
  assumes DISJOINT parallel slices; these share files + code deps, so stacking is correct.
  Assembly: phaseInt fast-forwards through the linear stack (1.4 contains 1.1-1.3).

## Run addressables-unit2-20260615T193738 (2026-06-16) — Unit 2: reference-resolution primitive

# Unit 2 — PLAN-stage decisions

## D1 — The missionPopup repro is integration point (b), NOT (a); its fix is NOT the guid→prefab-id primitive
Classification: User-Challenge
Empirically: `missionPopup: {fileID: 80306028}` in Main.unity is a same-scene-file
fileID-only ref (NO guid) to a STRIPPED MonoBehaviour on a prefab instance (MissionUI).
It is NOT a `{guid,fileID}` asset ref and NOT an SO field. It already flows through the
PLANNER scene-reference system (`_classify_reference`) and is recorded in the plan as
`target_kind="component", target_ref="Main.unity:80306028"` (conversion_plan.json
references[56]). It is nil at runtime because the host's `_wireReferences` can't resolve
a `component`-kind target that was never registered (stripped prefab-instance MB).
=> Unit 2 must deliver BOTH: (1) the task's named guid→prefab-id primitive (real, reused
by Units 3-4 for themeData/Consumables/segment AssetReferences), AND (2) the actual
repro fix on the planner/host scene-ref path. They are different integration points.
Rationale: the task explicitly told me to "determine which one missionPopup is, because
they have different integration points" and to scope Unit 2 to "the integration that
fixes the missionPopup repro". The evidence forces splitting the deliverable.

## D2 — Shared primitive lives in unity/ alongside prefab_id.py / addressables_resolver.py
Classification: Mechanical
A pure function (guid + fileID + guid_index + plan addressables/prefab tables → canonical
prefab_id | fail-soft None) belongs in `unity/` so it's importable by the SO emitter
(scriptable_object_converter.py) AND any scene-ref path with no converter→core cycle.
It reuses `unity/prefab_id.canonical_prefab_id` (the existing byte-identical join-key core).

## D3 — fileID is non-load-bearing for prefab-ref resolution; guid is sufficient
Classification: Taste
Unit-1's prefab_id scheme keys on `<guid>:<relative_path>` — one id per .prefab FILE.
A `{guid,fileID}` asset ref to a prefab resolves by guid alone (the fileID picks a
sub-object within the file, but the host clones the whole template). The primitive
records/ignores fileID for prefab refs; it stays available for non-prefab disambiguation
later if a unit needs it. (Verify against real themeData refs at phase-design time.)

## D4 — Fail-soft returns a sentinel distinct from a resolved nil
Classification: Mechanical
Non-resolvable refs (sprite/audio/SO/missing asset) must NOT crash emission and MUST be
distinguishable from "resolved to nothing". The SO emitter currently drops ALL object
refs to `nil --[[(Unity object reference)]]`; the primitive returning None preserves that
fail-soft string, while a resolved prefab id replaces it with the real id. Counters
(skipped vs resolved) logged, mirroring PrefabAddressables.skipped_non_prefab.

## Unit 2 plan decisions (design stage)
- **D1 (User-Challenge):** repro `missionPopup` is integration point (b) — a same-scene
  fileID-only ref to a STRIPPED prefab-instance MonoBehaviour (plan `target_kind=component`),
  NOT the guid→prefab-id primitive. Verified end-to-end (plan reference[56]; scene
  `&80306028 stripped`). Unit 2 = primitive (Units 3-4) + the repro fix, two tracks.
- **D2 (Mechanical):** EXTRACT existing `addressables_resolver.prefab_id_for` → shared
  module-level helper, rewire the resolver to call it. One impl (both reviewers flagged a
  parallel copy as DRY).
- **D3 (Taste):** fileID non-load-bearing for top-level prefab refs only; sub-asset fileID
  disambiguation = known primitive limitation (followup).
- **D5 (Mechanical, from review):** Phase 3 is INDEPENDENT of Phases 1-2 (disjoint paths);
  parallel tracks, repro fix is acceptance-critical.
- **D6 (Mechanical, codex BLOCKING verified):** Phase-3 fix starts at the PARSE layer
  (stripped docs dropped at `yaml_parser.py:290`), not "planner vs host". Resolves former Q2.
- **D4 (Mechanical):** fail-soft = distinct sentinel; SO emitter keeps `nil --[[(Unity object
  reference)]]` for non-prefab/missing; resolved/skipped counts logged.

## Phase 1 design decisions

## D7 — Shared primitive lives in a NEW module `unity/prefab_ref.py`
Classification: Mechanical
Not bolted onto `addressables_resolver.py` (Addressables-specific; SO/scene callers must
not import the group machinery to get a generic ref→id primitive) and not in
`prefab_id.py` (the zero-deps string-math core stays free of the `GuidIndex`/`AssetKind`
import). A dedicated `unity/prefab_ref.py` is the clean seam imported by
`addressables_resolver` (Phase 1) and `scriptable_object_converter` (Phase 2). No cycle:
`converter → unity` already exists (`scriptable_object_converter.py:17 from unity.guid_resolver import GuidIndex`).

## D8 — Fail-soft sentinel is `None`, never `""`
Classification: Mechanical
The primitive returns `str` on success and `None` for every fail-soft case. `None` (not
`""`) because the existing nested impl already returns `None` (byte-preserving rewire) and
because `canonical_prefab_id` overloads `""` to mean "skip stamping" — a `str` return
can't distinguish resolved-empty from skipped. The primitive normalises
`canonical_prefab_id`'s `""` to `None` (preserving the line-158 `return pid if pid else None`).

## D9 — Two entry points, one impl
Classification: Mechanical
`prefab_id_for_guid(guid, guid_index)` is the core (what `addressables_resolver` calls,
it has the bare guid). `prefab_id_for_ref(ref, guid_index)` is the `{guid, fileID, type?}`
front door for Phase 2 / Units 3-4 — extracts guid via `ref_guid` semantics (all-zero /
missing → None), ignores fileID/type for top-level prefab refs (D3), delegates to the
core. Phase 1 ships both but only wires the core into the resolver. The
`resolve_prefab_addressables` `guid_index: object` param is tightened to `GuidIndex`
(removes duck-typed `getattr`).

## Phase 2 design decisions

## D10 — Only `_value_to_lua` gains new params; the rest of the chain is unchanged
Classification: Mechanical
`guid_index: GuidIndex | None` ALREADY flows pipeline.py:910 → convert_asset_files →
convert_asset_file (consumed by `_resolve_script_class_stem`). The only missing hop is
convert_asset_file:231 → `_value_to_lua`. `_value_to_lua` gains
`guid_index: GuidIndexLike | None = None` + `counts: RefResolveCounts | None = None`
(both default None), threaded through its list-item and dict-value recursion.
convert_asset_files / convert_asset_file / pipeline.py keep their signatures. Smallest
correct change; every existing call site stays green by default.

## D11 — Counters are a module-private `RefResolveCounts` accumulator
Classification: Mechanical
A `RefResolveCounts` dataclass (`resolved`/`skipped`: int) is threaded as a mutable
accumulator through the recursion so `_value_to_lua`'s `-> str` contract is unchanged and
the whole asset's refs tally in one pass. Logged at INFO per asset, mirroring
`PrefabAddressables.skipped_non_prefab`.

## D12 — Resolved id emitted via the existing string-literal path
Classification: Mechanical
The resolved prefab id emits as `f'"{_lua_escape_string(pid)}"'` — the SAME path as every
other string field (:73-74), not hand-formatted. One place to change if Luau string
emission changes; identical quoting/escaping.

## D13 — Phase 2 adds no ref-shape guards of its own
Classification: Taste
Phase 2 trusts the Phase-1 `prefab_id_for_ref` contract (sentinel None for every fail-soft
case — non-dict, missing/all-zero guid, non-prefab, guid-not-in-index, "" → None). The
ONLY Phase-2 gate is `guid_index is not None`. Fail-soft responsibility stays in ONE place
(the primitive), not duplicated at the call site.

## D14 — CI-default acceptance test builds a minimal real GuidIndex (not a full project parse)
Classification: Taste
The byte-match gate constructs `core.unity_types.GuidIndex` with a real `project_root` +
one real `GuidEntry` for the pinned Pickup.prefab guid, so it runs in CI WITHOUT
/Users/jiazou/workspace/trash-dash on disk. An optional second test parses the real
project (path-exists-skipped) for belt-and-suspenders. A `project_root` under which the
prefab path lives is REQUIRED for the full `<guid>:<path>` form (else `canonical_prefab_id`
returns a bare guid). Acceptance fixture pinned + verified end-to-end:
`collectiblePrefab {guid:16cac8b68c4ca6448baecd0680e025f6}` →
`"16cac8b68c4ca6448baecd0680e025f6:Assets/Prefabs/Pickup.prefab"` (exact plan key);
themeIcon (sprite) / skyMesh (mesh) → None.

## Phase 3 detailed-design decisions (stripped-prefab-instance ref resolution)
- **D-P3-1 (Mechanical):** Preserve stripped docs via an additive `stripped_out` out-param on
  `parse_documents` (mirrors `warnings_out`); returned triples stay byte-identical. Contract
  "stripped filtered out" preserved by construction.
- **D-P3-2 (Mechanical, codex Major):** Resolution is a PLANNER POST-PASS over built
  scenes_block+prefabs_block (rewrites unresolvable rows in place), NOT inline in `_walk_scene`
  — only point where the subplan exists, so it can FAIL-CLOSE (subplan instance_id + script_guid
  match). Keeps `_classify_reference` untouched.
- **D-P3-3 (Mechanical, codex Major):** Dedicated `_pendingPlacementRefs` drained by ONE shared
  helper from BOTH the post-placement step AND the end of `_completeDeferredBatch` (placed target
  may be UI-deferred). Idempotent; distinct from `_inboundRefsToDeferred`.
- **D-P3-4 (Reversible):** Anchor `target_ref` on the deterministic `m_CorrespondingSourceObject.
  fileID` → prefab-local instance_id, never a generated-output fingerprint.
- **D-P3-5 (Reversible, fail-closed):** Keep the unresolvable fallback when any bridge link
  fails (no placement / no subplan instance / script_guid mismatch); runtime leaves field nil +
  WARN. Never invent a binding.
- **D-P3-6 (Reversible, codex Major 1):** Planner stamps `target_script_id` on rewritten rows so
  the runtime cross-domain policy classifies the target domain without finding it in scene-local
  `instanceById` (placement-scoped key never appears there). Closes the cross-domain bypass.

<!-- ==== /drive run: checkmark-toggle-binding-20260617T075020 ==== -->
# Decisions — checkmark-toggle-binding-20260617T075020

Generic Unity Toggle `isOn`→checkmark visual binding (follow-on to PR #202). Recorded per the 6 decision
principles. Design: `design.md` (re-anchored against worktree `1acd1bb`).

## 2026-06-17 — design revision (3 prior dual-voice findings addressed)

- **D-1 — Scope: Toggle→checkmark only; Slider→fill DEFERRED.** Build the generic `ui_*_bindings` mechanism so the
  Slider side slots in later as `ui_slider_bindings`; write NO Slider code now. (Right-size-at-design.)
- **D-2 — Anchor on the Unity Toggle component + its serialized `graphic` fileID, resolved to the checkmark's
  `_SceneRuntimeId`; never node names.** VERIFIED chain: Toggle `&264237065` `graphic`→component `250410366`→
  owning GameObject `250410364` "Checkmark" / SRI `...:250410364`; toggle GO SRI `...:264237063` (re-decoded from
  the real rbxlx this revision). Deterministic upstream source, not an AI-output fingerprint.
- **D-3 — The component-fileID→GameObject resolver is net-new work and its OWN slice (Slice 1).** Corrects the
  prior design's assumption that the onClick `m_Target` path already provides it — VERIFIED `target_file_id`
  (`ui_translator.py:371-376`) has ZERO readers, no such index exists anywhere. The binding (Slice 2) depends on
  it. (Adversarial BLOCKING reproduced against the REAL source → fix it, do not overrule.)
- **D-4 — Plan-driven binding (`ui_toggle_bindings`), consumed via `workspaceFind`; NOT a runtime workspace scan,
  NOT an edit to the AI-generated HudControl.** Mirrors `scene_prefab_placements`/`scriptable_objects`;
  deterministic + harness-testable lever. The `SetAttribute("isOn")` writer is left untouched; the runtime reads
  it.
- **D-5 — `attr_name` single-sourced via a new `_TOGGLE_ISON_ATTR = "isOn"` constant, asserted against the AI
  writer's literal.** No converter constant named the toggle attribute today (VERIFIED grep; `"isOn"` lives only
  in AI output / `fixture.json:73`). Introduce one + a pin test (A4) so a lowering-casing drift fails RED rather
  than silently inert.
- **D-6 — Initial visibility applied BEFORE Awake.** Deferred path hooks immediately before
  `engine:_runAwakeEnableStart(batch)` at `scene_runtime.luau:3040`; synchronous/late paths apply before their
  host's lifecycle. (codex P1 #2 — the prior end-of-`start()` hook was too late; every batch already ran Awake by
  then.)
- **D-7 — Generic late-clone coverage via a `PlayerGui.DescendantAdded` watch keyed on unbound binding records,
  reusing the client `awaitUiHost` primitive (`autogen.py:756-810`); client-only, fail-closed on the server.**
  Binds a Toggle in a late-cloned ScreenGui whose host has NO deferred runtime component (which never reaches
  `_completeDeferredBatch`/`_resolveDeferredUiInstances`). Makes "ANY Unity Toggle" actually hold. (codex P1 #3.)
- **D-8 — Idempotent pass run from hook A, hook B, and the late watch; exactly-once per toggle via a marker**
  (mirrors `boundClones` `scene_runtime.luau:3213`). Fail-closed on nil SRI resolution (E1/E7/E9).

### Slicing
- **TWO slices** (was ONE in the prior design). Slice 1 = `build_component_owner_index` resolver (no deps,
  independently RED-provable, no output change). Slice 2 = the binding (depends on Slice 1; converter capture +
  plan key + runtime consumer co-locate behind the shared `ToggleBinding` contract). Staged-risk fan-in seam, not
  a size cut.

### Out-of-scope (→ followups)
- Health-bar fill (Unity Slider→fill) — DEFERRED, build as `ui_slider_bindings` later.
- Transparency-based hide vs `.Visible` — revisit only if e2e shows a graphic that must dim.
- Resolving the long-dead onClick `target_file_id` via the new index — latent cleanup the resolver enables.

## 2026-06-17 — Execute-stage decision (graphic-only-reclone)
- **D-9 — Revert the `(toggle,graphic)`-pair marker; keep the toggle-only marker (`1abead9`).** The phase-integration
  review flagged that the toggle-only `_boundRows` marker won't re-bind a graphic re-cloned while its toggle survives
  (MAJOR). A pair marker was implemented (`66b7ba2`) but its own review found it LEAKS the change-signal listener
  (connected to the surviving toggle, not auto-disconnected) — a net-negative forward fix. The edge is UNREACHABLE in
  converter output (a converted checkmark is a child of the toggle's ScreenGui → clones/destroys with it). Per
  OPERATING.md (net-negative fix → revert to last-good + log residual; right-size: gate hardening on evidence the
  failure occurs), reverted to `1abead9` and carried the gap as a documented followup. Overruled-with-evidence, not
  silently dropped; surfaced at Gate B.

<!-- ==== /drive run: checkmark-toggle-binding-20260617T075020 (e2e-found addendum) ==== -->
## 2026-06-17 — verify-stage decision (dispatch root cause; e2e-found)
  silently dropped; surfaced at Gate B.

## 2026-06-17 — verify-stage decision (dispatch root cause; e2e-found)
- **D-10 — Detect Toggle as `MonoBehaviour + m_IsOn`, not `ct == "Toggle"`.** A live SimpleFPS conversion (the e2e the
  feature is for) emitted 0 binding rows for 4 real HUD toggles: real Unity UI Toggles serialize as `MonoBehaviour`
  (m_Script GUID), never a literal `"Toggle"` component_type, so the dispatch was dead on real scenes (and the
  pre-existing `ToggleIsOn` never fired either). Fix mirrors the Button `m_OnClick` heuristic. Re-verified live:
  4 rows incl Battery `toggle_sri=264237063 graphic_sri=250410364`. The whole 5-round design + slice reviews missed
  this by anchoring on the assumed canonical "Toggle" type — the real input space (MonoBehaviour+GUID) was never
  exercised until a live conversion. Lesson: enumerate the REAL serialization before locking a dispatch.

<!-- /drive checkmark-toggle-binding-20260617T075020 — verify-stage (faithful Mode-2 e2e) -->
## 2026-06-17 — verify-stage decision (premise correction; faithful Mode-2 e2e)
- **D-11 — Premise refined against the FAITHFUL conversion; feature confirmed live.** A faithful `/convert-unity`
  Mode-2 conversion (NOT u2r — `convert_interactive`, client/server split, AI transpile) revealed the AI `HudControl`
  ALREADY reveals the checkmark on pickup by node-NAME (`FindFirstChild("Checkmark")`). So the original premise
  ("UpdatePlayerItems only SetAttribute('isOn') with NO reader") is conversion-dependent/incomplete. The bug the
  binding UNIQUELY + DETERMINISTICALLY fixes: (1) **initial-state hide** — uncollected items show checkmarks at spawn
  (template `Visible=true`, m_IsOn=0); the AI reveal-on-pickup never hides them; and (2) the **generic, deterministic
  guarantee** — the AI's reveal is non-deterministic (it reasoned its way to adding it this run) + non-generic
  (hardcodes the name "Checkmark"); the binding keys on SRIs and always runs. **LIVE A9 (faithful build, Studio Play
  client) PASS:** all 4 checkmarks `Visible=false` at spawn; Battery `isOn=true`→`Visible=true`, `isOn=false`→`false`,
  driven by the binding's attr-change listener (set the attribute directly, isolating the binding from HudControl).
  smoke=pass, validator=pass, scriptErrorCount=0.
- **D-12 — e2e MUST use /convert-unity (or /e2e-test), NEVER u2r** (recorded as a durable lesson): u2r mandates
  `--skip-architecture-step` (no client/server split) → can't faithfully run the client-only binding. u2r is OK only
  for deterministic converter-unit checks (A6 plan emission).

## ── /drive run addressables-unit3-themes-20260617T080323 — Addressables Unit 3 (Themes) — 2026-06-17T04:00:11Z ──

# Decisions — Addressables Unit 3 (Themes)

## D1 — Fix dead theme registration via data-seeding through a converter-owned contract
Classification: Taste / Mechanical
Populate the theme registry with a deterministic build-time seed (off the `scriptable_objects` guid→path
map + addressables labels), NOT by re-lowering the AI's non-deterministic `LoadDatabase`/`Register`
emission. The seed must register through a contract the converter owns, not the AI's incidental
`Register`/`_pendingThemeData` shape (fingerprinting AI output is the anti-pattern this fix removes).

## D2 — Resolve AssetReference via the existing primitive + `_value_to_lua` seam
Classification: Mechanical
Extend `scriptable_object_converter._value_to_lua` object-ref recognition to match the AssetReference
`{m_AssetGUID, m_CachedAsset:{guid,fileID,type}}` shape and resolve via the Unit-2
`prefab_id_for_ref`/`prefab_id_for_guid` primitive. No new resolver, no duplication; the consumer
`host.instantiatePrefab(zone.prefabList[i])` is already wired to expect a prefab-id string.

## D3 — Two phases on the foundation→consumer staged-risk seam
Classification: Taste
Phase 1 (AssetReference resolution) is an independently-verifiable foundation; Phase 2 (theme
registration) end-to-end depends on Phase 1 being correct (a registered theme with unresolved prefabList
spawns nothing — failure would scatter, hiding the broken layer). Split rather than one linear phase.

## D4 — No game-specific hardcodes in the seed
Classification: Mechanical
Derive the owning label + key field from the consuming load method (not literal `themeData`/`themeName`);
dedupe by guid; abstain (don't crash) on SOs lacking the identifying field (best-effort fallback). Never
hardcode `themeData` or `themeName`. (Superseded the earlier "iterate ALL SO labels" framing — see D6.)

## D5 — Strike option (b) coherence-pack; committed converter-owned write-time surface
Classification: Mechanical
Round-1 review (P1-A) verified: coherence packs are SKIPPED in generic scene-runtime mode — the generic
`write_output` branch returns BEFORE `run_packs` (`converter/converter/pipeline.py:3139-3147`). The whole
feature is generic, so a "coherence-pack structural guarantee" can never fire — option (b) is STRUCK.
Also corrected: a PUBLIC `ThemeDatabase.Register(themeData)` DOES exist in captured output
(`ThemeDatabase.luau:52-59`); the honest reason not to call it is that its name/shape is a
non-deterministic AI emission (D1), NOT that "no write path exists." Committed direction: a
converter-owned, generic-mode-safe deterministic registration surface emitted at write-time alongside the
`scriptable_objects` map build (`pipeline.py:~6877-6925`), NOT fingerprinting the AI `Register`. The
remaining open item is the precise write mechanism, to be SPIKED at Phase 2 detailed design against the
real ThemeDatabase/PlayerData/TrackManager modules; if it forces a consumer registry-READ-site rewrite,
that is absorbed in Phase 2 scope/SLOC and the "zero consumer change" claim is qualified to Phase 1 only.

## D6 — Scope Unit 3 to the theme DB with a DERIVED ownership/routing model
Classification: Taste / User-Challenge
Round-1 review (P1-B): "retain all SO labels + key by identifying field" explains discovery but not which
consumer DB owns a label nor the correct key function. Blindly iterating ALL SO-addressable labels +
dedup-by-guid cross-registers unrelated SOs, and the guid/stem fallback creates entries the consumer never
retrieves (registry looks populated but is behaviorally dead). Decision: route each label to the database
whose converted load method requests it, keyed by the field that database indexes by — DERIVED from the
source (`ThemeDatabase.LoadDatabase` issues `LoadAssetsAsync<…>("themeData")`, indexed by `op.themeName`),
never hardcoded. Scope Unit 3 to the theme database only; a fully-general "every SO-addressable consumer"
ownership model (CharacterDatabase, future SO-loaded DBs) is deferred.

## D7 — Reframe Phase 2 acceptance to a directly-invoked criterion; name the Unit-1 dependency
Classification: Taste
Round-1 review (P1-C): "segments spawn" is gated on Unit 1 (character boot / client spawn-domain, "Error
2"), not Phase 1/2 alone — so it is not honestly isolatable. Reframed Phase 2's isolatable acceptance to:
`currentTheme` non-nil AND `host.instantiatePrefab(prefabList[i])` returns a real instance WHEN INVOKED
DIRECTLY (probe-driven, decoupled from live gameplay). The Unit-1 character/spawn-domain dependency is
named as an out-of-band precondition for the visible end-to-end (added to Out of scope / dependencies).

## D8 — AssetReference matcher: require m_CachedAsset present + subset-bound the rest (Phase 1 detailed design)
Classification: Mechanical
`_value_to_lua` matches a Unity AssetReference iff `{m_AssetGUID, m_CachedAsset} <= set(d.keys()) <=
{m_AssetGUID, m_CachedAsset, m_SubObjectName}` (pinned against `trash-dash/.../themeData.asset:22-31`;
verified 152/152 real occurrences are exactly `{m_AssetGUID, m_CachedAsset}`). REQUIRE both `m_AssetGUID`
AND `m_CachedAsset` (the real serialization always pairs them) and subset-bound the rest (only
`m_SubObjectName` optional). Anti-swallow tightening: requiring `m_CachedAsset` stops a bare `{m_AssetGUID}`
/ `{m_AssetGUID, m_SubObjectName}` one-field struct from being wrongly collapsed to a prefab-id string or
nil — it falls through to the generic-dict branch UNRESOLVED with its data intact (fail-soft, no
regression). Disjoint from the existing `set(keys) <= {fileID,guid,type}` object-ref arm.

## D9 — Resolve via existing primitives; zero edits to prefab_ref.py (Phase 1 detailed design)
Classification: Mechanical
Resolve precedence: `m_AssetGUID` (bare guid -> `prefab_id_for_guid`) first, `m_CachedAsset`
(`{fileID,guid,type}` -> `prefab_id_for_ref`) as fallback; both through the UNCHANGED shared `.prefab`
filter (`prefab_ref.py:54`). No new resolver, NO edit to `prefab_ref.py` — the REAL primitives already
suffice. [DIVERGENCE] design.md:159 allowed a "0-1" prefab_ref.py touch; the real code needs 0. Phase 1
lives entirely in `_value_to_lua` (+ a one-line import of `prefab_id_for_guid`).

## D10 — Unresolved AssetReference collapses to nil, not the old unresolved table (Phase 1)
Classification: Taste
A non-prefab/empty/dangling AssetReference emits `nil --[[(Unity AssetReference)]]`, NOT the prior
unresolved `{AssetGUID=…, CachedAsset=…}` table. The consumer indexes `prefabList[i]` as a STRING for
`host.instantiatePrefab`; a table there was already non-functional, so a uniform `nil` fail-soft is the
honest, consumer-guardable contract and matches the object-ref arm's `nil`. No data loss vs a usable
prior emit.

## D11 — Write mechanism: converter-owned boot shim feeds deterministic consumer-called LoadDatabase (Phase 2 detailed design / spike)
Classification: Mechanical / User-Challenge
SPIKED against real modules + dual-voice review. The shim (a new converter-owned module, NOT a coherence-pack
edit on AI output) seeds owned SO instances onto the DB's appendable write surface at boot; the deterministic,
consumer-called `LoadDatabase` (`ThemeDatabase.luau:30`, C#-derived name) does the dict-fill AND sets
`loaded()=true` — so the `LoadoutState.luau:184` `loaded()` UI gate works for free. STRUCK option (b) "rewrite
the C#-derived read bodies" (GetThemeData/dictionnary/loaded): body-rewrites on transpiled output are
coherence-pack-shaped and packs are SKIPPED in generic mode (the SAME constraint that killed the pack option in
D5) — codex overruled the design subagent's (b) recommendation at integration. STRUCK "converter fully emits
the DB module" (can't generically pick which AI module is the theme DB without fingerprinting D1/D6 forbid).
The AI-name dependency is confined to ONE structurally-detected appender ingress (prefer a public list-appender
fn, else a `*pending*` list field) with fail-soft no-op+warn. Consumer registry-READ-site rewrite is NOT
required — "zero consumer change" extends to Phase 2 (design.md:128 caveat does NOT trigger).

## D12 — Bootstrap order: entrypoint slot between SceneRuntime.new and engine:start (Phase 2)
Classification: Mechanical
The shim runs in `SceneRuntimeClient`/`Server` between `SceneRuntime.new(services, Plan)` and
`engine:start(domain)` (`autogen.py:960-961`/`:1115-1116`). Verified: every `PlayerData.Create()` call (which
fires `LoadDatabase` via `StartStaticCoroutine`, `PlayerData.luau:233-238`) is inside a lifecycle method
(GameManager:OnEnable, MusicPlayer/LicenceDisplayer/ShopUI:Start) that runs in `engine:start()`'s
`_runAwakeEnableStart` — strictly AFTER the shim slot. The `themeDataList == nil` guard
(`ThemeDatabase.luau:32`) makes a too-late seed permanently invisible (spike 2 "Order BAD"), so the slot is
load-bearing. The shim seeds the PENDING list (not the private store), so `LoadDatabase`'s `themeDataList={}`
reset is re-filled in the same call — no clobber.

## D13 — Ownership/routing derived generically; seed supplies instances, LoadDatabase owns the key (Phase 2)
Classification: Mechanical
Owned label derived from the C# `Addressables.LoadAssetsAsync<T>(LABEL, …)` call in the DB's load method
(`ThemeDatabase.cs:35` → "themeData"); key field from the callback's `dict.Add(op.FIELD, op)`
(`ThemeDatabase.cs:36-37` → `themeName`); owning DB = the module issuing the load; owned SO guids =
`addressables.by_label[label]` ∩ emitted SO guids (the two `m_Address: themeData` entries,
`Themes.asset:20-29`). NEVER hardcode `themeData`/`themeName`. The seed supplies SO INSTANCES only; the key
field is used SOLELY to abstain on a key-less SO, never to write the registry key (that stays `LoadDatabase`'s
job) — keeping AC-1 self-satisfaction-proof. Unit 3 scoped to the theme DB; CharacterDatabase/future deferred.

## D14 — Parallel SO-addressables surface gated on positive evidence; shared .prefab filter untouched (Phase 2)
Classification: Mechanical
New `ScriptableObjectAddressables` + `resolve_scriptable_object_addressables` in `addressables_resolver.py`,
PARALLEL to `resolve_prefab_addressables`. Retains label/address → SO guids gated on `so_guids` (an emitted SO
module EXISTS — positive evidence), NOT on the absence of a `.prefab` (which would also catch sprites/audio).
The shared `.prefab` filter (`prefab_ref.py:54`) and the existing prefab resolver are UNCHANGED (P2 hard
constraint upheld; AC-8 pins byte-identical prefab maps).

## D15 — Acceptance reads via the consumer's lookup path, not the seed's key (Phase 2; followups P2)
Classification: Taste
AC-1 reads `ThemeDatabase.GetThemeData(PlayerData.instance.themes[PlayerData.instance.usedTheme + 1])`
(verbatim `TrackManager.luau:228`), indexing by `PlayerData.themes[1] == "Day"` (`PlayerData.luau:271`), NOT
the seed's derived key. Self-satisfaction-proof because the seed never writes the registry key — `LoadDatabase`
extracts it from `op.themeName` and the probe looks it up via `PlayerData.themes`, two independent paths that
meet only if the registry is genuinely keyed right. Discharges followups P2.

## D16 — Write-surface detection BOUND to the LoadDatabase drain list + FAIL-LOUD on a miss (Phase 2; review P1)
Classification: Mechanical
The write-surface detector must prove the surface it seeds is the SAME list the converted `LoadDatabase`
drains into the private store — not merely that *some* appender to *some* list exists (a mismatched/false
detection silently leaves `GetThemeData`/`currentTheme` nil, the D1 fingerprint-fragility this approach exists
to avoid, with no consumer read-rewrite to compensate). Key the mechanism off the deterministic C#-derived
`LoadDatabase` (name from C# `ThemeDatabase.LoadDatabase`, stable across re-transpiles): derive `DRAIN_FIELD` =
the field its body iterates (`ipairs(<MODULE>.<DRAIN_FIELD>)` → `_pendingThemeData` on real output), accept a
public appender ONLY if its `table.insert` `TARGET_FIELD == DRAIN_FIELD` (else seed `DRAIN_FIELD` directly).
What is compared: appender-target-field == LoadDatabase-drained-field == the field the store is built from —
all derived from that one converted module, never a name match (no `Register`/`_pendingThemeData`/`themeData`
hardcode). If the linkage can't be proven (no recognizable drain, OR no surface binds), the seed records NO
entry for that DB, emits a converter WARNING, and ABSTAINS — loud abstain > silent dead registry. Verified on
the real `ThemeDatabase.luau` via `/tmp/drain_link_probe.py` (Register binds: `_pendingThemeData ==
_pendingThemeData`). (design-phase2 §1.4; AC-7/AC-9; edges 7/8.)

## D17 — Seed plan RECOMPUTED at emit time (not round-tripped through conversion_plan.json) so a no-retranspile resume keeps it (Phase 2; review P2)
Classification: Mechanical
`addressable_db_seeds` is **RECOMPUTED in `write_output` on every run** and set on the in-memory `scene_runtime`
dict in the window after `_build_scriptable_object_module_map` (`pipeline.py:6819`) and BEFORE
`generate_scene_runtime_plan_module` (`:6822`) — NOT persisted through `conversion_plan.json`. It does NOT rely
on the conversion_plan round-trip: `_classify_storage` writes conversion_plan.json during
`materialize_and_classify` (`:5056`/`:2798`) BEFORE this derivation point, and `_merge_scene_runtime` (`:5885`)
preserves only keys already on disk while `plan_scene_runtime` resets `ctx.scene_runtime` (`:1159`) — so a
late-added seed would NOT round-trip; recomputing each emit sidesteps the on-disk-only preservation entirely.
Every input is recomputed by an ESSENTIAL phase on resume, so it is NOT gated on a fresh transpile: the
`addressables` block via ESSENTIAL `plan_scene_runtime` → `_build_addressables_block`
(`pipeline.py:691`,`:1041-1124`); the transpiled DB module body (drain/appender bind) via ESSENTIAL
`materialize_and_classify` → `_rehydrate_scripts_from_disk` re-reading `<output>/scripts/*.luau` into
`rbx_place.scripts[*].source` (`pipeline.py:696`,`:3034`,`:3162`); the DB C# (label + key field) re-read from
`self.unity_project_path` by path. It does NOT read transient `transpilation_result` (`None` on a no-retranspile
resume, `pipeline.py:175`/`:2212`) — unlike the dead-modules pass that sources C# from
`transpilation_result.scripts` and abstains on resume. So a no-retranspile `--phase=write_output`/`assemble`
resume re-emits the `seedAddressableDatabases` call AND a non-empty seed list **into the emitted
`SceneRuntimePlan`**; the seed is NOT transpile-gated. (design-phase2 §2.2, §2.5; AC-10.)

## D18 — Allowlist the seed key in `_PLAN_KEYS_FOR_HOST` so it reaches the runtime-required plan; re-point AC at the emitted plan (Phase 2; review P1, both reviewers)
Classification: Mechanical
The embedded `SceneRuntimePlan` ModuleScript the runtime actually `require`s is built by
`generate_scene_runtime_plan_module` (`autogen.py:654`), which filters `scene_runtime` through the
`_PLAN_KEYS_FOR_HOST` allowlist (`:643-651`; real current contents: `modules, scenes, prefabs,
domain_overrides, scriptable_objects, scene_prefab_placements, addressables, gravityDesiredBaseStuds`). The
subset is `{k: scene_runtime.get(k) for k in _PLAN_KEYS_FOR_HOST if k in scene_runtime}` (`:662-666`) — a key
NOT in the allowlist is ELIDED from the emitted plan even when present on `scene_runtime`. So the D17-recomputed
seed (written to `scene_runtime`/`conversion_plan.json`) would be dropped from the plan the runtime requires →
`Plan.addressable_db_seeds == nil` → `seedAddressableDatabases` always sees `{}` → silent dead registry on a
FRESH convert (not just resume) — the exact failure the phase exists to prevent. FIX: add
`"addressable_db_seeds"` to `_PLAN_KEYS_FOR_HOST` (the SAME mechanism that carries
`addressables`/`gravityDesiredBaseStuds`; confirmed the correct lever — the filter is the single gate the
runtime-required plan passes through). Named as a fifth production TOUCH-POINT in the Interfaces + the slice
`owns` (one-line, small but LOAD-BEARING; cannot be split off — a slice that built the seed without it ships a
silently dead registry). AC-10 is RE-POINTED to assert the key is present in the EMITTED `SceneRuntimePlan`
ModuleScript (the filtered artifact the runtime requires), NOT only `conversion_plan.json`, so the AC actually
guards this seam; edge 9 covers "key on `scene_runtime` but absent from the emitted plan" (a test that drops the
allowlist key must turn AC-10 RED). (design-phase2 §2.2a, §4 touch-point #5, AC-10(P1), edge 9.)
## Run addressables-unit4-20260616T084409 (Addressables Unit 4 — roster) — 2026-06-17

# Decisions — Addressables Unit 4 (roster)

## D1 — Anchor roster on `addressables.by_label`, not the AI-emitted `CharacterDatabase` access string
Classification: Taste (architecture)
The transpiled consumer has drifted across 3 shapes (WaitForChild folder, FindFirstChild folder, CollectionService:GetTagged). Key the re-lowering on the deterministic upstream label block (canonical-contract rule). Output-fingerprinting reserved for the best-effort consumer-side rule only.

## D2 — Emit roster as CollectionService-tagged + attributed instances AND a named Folder
Classification: Taste (architecture)
Covers all three observed consumer shapes; a folder alone silently abstains on the current `GetTagged` shape. The tag is the canonical agreed key.

## D3 — Roster instances clone/reference the existing `ReplicatedStorage.Templates.<unique-key>` prefabs (Unit 1's emit)
Classification: Mechanical
Unit 1 already emits the roster prefabs as uniquely-keyed templates via `addressable_prefab_ids`; reuse avoids a second prefab→template conversion and keeps identity collision-free.

## D4 — Redirect the dead `scriptable_objects/<Name>.luau` ModuleScript for roster-shaped `.asset`s; trigger structurally (a `by_label` set of prefab refs), never on the name
Classification: User-Challenge
Touches the generic SO-emit path and the "stay general-purpose, no hardcoded game values" constraint. Review explicitly that roster-detection is structural and game-agnostic.

## D5 — One phase (emit + consume are one contract)
Classification: Mechanical
Default one phase; no fan-out and no staged-risk foundation present (Unit 1's identity foundation already landed/verified). Emit-without-consumer or consumer-without-surface each prove nothing; only the assembled boot proves the fix.

## Scope correction (recorded)
Design-doc "Unit 4" = consumables + roster. This task is the ROSTER HALF only (the boot-gating dep). Consumables deferred to followups.

---
## Revision after dual-voice design review (review-design-1.md) — 2026-06-16

D1–D5 below REVISE the originals above; D6–D9 are NEW. (See design.md for full wording.)

## D1 (revised) — Anchor BOTH halves on `addressables.by_label`, not the AI access string
Classification: Taste (architecture)
Consumer drifted across 3 access shapes; both producer and the consumer re-lowering key on the upstream label block.

## D2 (revised) — Tag-as-primary contract (CollectionService tag + attributes); named Folder OPTIONAL
Classification: Taste (architecture)
Tag is the canonical key and needs no folder name (also resolves the D6 collision). Folder added only if the spike shows a consumer shape that requires it; default tag-only.

## D3 (revised) — Roster→template lookup keys on prefab_id, not characterName/address
Classification: Mechanical
Templates child name is the prefab file-stem (guid-disambiguated). Keying on name/address mis-resolves. Reuse Unit-1 uniquely-keyed templates.

## D4 (revised, was the mis-tiered Open Question) — Deliverable is BOTH the roster surface AND the consumer resolved to the `Character` object graph
Classification: User-Challenge
LoadDatabase returns a raw tagged Instance; LoadoutState/Character dereference a rich Character OOP table (`c.gameObject:Clone()`, `c.accessories`, `c.characterName`, `getComponent(...,"Character")`). A tagged surface alone does NOT unblock the boot. Consumer lever is greenfield (no roster re-lowering scaffold in contract_pipeline.py). Exact shape settled by the SPIKE-FIRST directive.

## D5 (revised, was "characterName from address") — characterName from the Character component's serialized field; fall back to address/prefab-name only if absent
Classification: Mechanical
Consumer keys on `op:GetAttribute("characterName")` — a serialized component field, not the addressable address (`address ≠ characterName`).

## D6 (new) — Resolve the dead AssetGroup SO name-collision now (tag-primary and/or suppress the dead module), don't defer
Classification: User-Challenge
`scriptable_objects/<Group>.luau` is the parsed AssetGroup definition (dead — LoadDatabase never reads it); writer reserves RS names (~1685-1692). Tag-primary avoids the folder name; suppressing the dead module removes the collision. Trigger is structural (a by_label set of prefab refs), never the name.

## D7 (new) — Explicit roster-tag emit path; don't overload the single-tag m_TagString writer path
Classification: Mechanical
rbxlx_writer.py:942-948 emits one tag from m_TagString. Roster needs its own tag-emit path (writer + place builder) or a generalized null-delimited Tags list.

## D8 (new) — New/extended RbxPlace data-model channel for the roster, consumed identically by BOTH emit paths
Classification: Mechanical
RbxPlace has no generic RS tree, only `replicated_templates`. Both rbxlx_writer AND luau_place_builder must emit the roster, or publish-built places stay broken.

## D9 (new, supersedes the prior "one phase" D5) — Two-phase staged-risk split (producer foundation → greenfield consumer); spike at the front of detailed design
Classification: Taste (architecture)
Producer surface is an independently-verifiable foundation (parsed-rbxlx + headless place) the greenfield consumer lever depends on → foundation-before-dependents condition justifies the split. May re-merge to one phase post-spike if Phase 2 is thin; then carry heightened-review.

## Framing correction (recorded)
The fix is ADDITIVE (emit a new roster surface), NOT a "redirect" of the dead SO ModuleScript. The SO module matters only for the D6 name-collision. The prior doc's "redirect"/"dead ModuleScript" framing throughout was corrected.

---
## Phase-1 detailed-design decisions (post-spike) — 2026-06-16
Spike (spike-phase1.md) verdict: `c.accessories` + `c.gameObject:Clone()` ARE on the boot path → two-phase split (D9) STANDS. The following are Phase-1 (producer) decisions.

## P1-D1 — Roster channel is a typed `list[RbxRoster]` on RbxPlace, NOT a generic RS-children tree
Classification: Taste (architecture)
Right-sized to one feature; a generic RS-children tree is a followup if themes/consumables/other label rosters accrue. Self-describing for both emit paths (D8). New types RbxRoster{label, members} / RbxRosterMember{template_name, tag, attributes:dict[str,RbxAttrValue]} in core/roblox_types.py.

## P1-D2 — Roster members are deep-copied clones of the emitted Templates.<template_name> tree at the canonical RS surface; lookup keys on prefab_id→template_name (D3)
Classification: Mechanical
Reuses Unit-1 templates (no second conversion). prefab_id→template_name comes from scene_runtime.prefabs[pid]["template_name"] (already resolved at pipeline.py:6087-6091). Keying on prefab_id (not name/address) avoids D3 mis-resolution.

## P1-D3 — Tag-primary, NO folder by default (D2/D6); any container uses a fixed reserved non-colliding name added to _reserved_rs_names
Classification: User-Challenge
Avoids the dead-AssetGroup-SO name collision by construction (scriptable_objects/<Group>.luau ModuleScript name already reserved at rbxlx_writer.py:1689-1691). Trigger is structural (by_label set of prefab refs), never the name. Flag for game-agnostic review.

## P1-D4 — Dedicated roster-tag emit on BOTH writers; rbxlx emits correct base64 NUL-delimited Tags, headless emits CollectionService:AddTag (D7/D8)
Classification: Mechanical
Does NOT overload the existing single-tag path (rbxlx_writer.py:944, reads attributes.get("Tag"), writes plain text — a doc divergence: it is NOT m_TagString). Spike finding: luau_place_builder emits ZERO CollectionService tags today, so the roster-tag path is new on BOTH writers. Existing single-tag plain-text format left as-is; flagged as a VERIFY-stage (Studio boot) risk.

## P1-D5 — characterName from the prefab's Character-component serialized field (scene_runtime.prefabs[pid].instances[*].config), fallback address→stem, omit if absent (D5)
Classification: Mechanical
Matches consumer contract op:GetAttribute("characterName"). serialized_field_extractor strips the m_ prefix (serialized_field_extractor.py:73-74). address ≠ characterName.

## P1-D6 — Phase 1 is ONE slice (shared co-authored RbxPlace.rosters contract)
Classification: Taste (architecture)
The new channel is co-authored by the assembly producer + both emit consumers; the shared-contract rule forces one slice. No fan-out, no staged-risk seam inside Phase 1 (the staged-risk boundary is Phase 1→2). XML-only roster leaves headless places broken (D8), so the writers are not independently verifiable. NOTE: the merged-tag-encoder test (P1-D8 below) and cross-emitter parity test (P1-D2-rev) are FIRST-CLASS slice acceptance, not deferred to VERIFY.

---
## Phase-1 detailed-design REVISION after dual-voice review (review-phasedesign1-1.md) — 2026-06-16
P1-D2/P1-D4/P1-D5 below REVISE the originals above (verified against real worktree code this session). P1-D7/P1-D8 are NEW. (Full wording in design-phase1.md §5.)

## P1-D2 (revised) — Each emitter MATERIALIZES a SECOND instance; the canonical Templates child is NEVER tagged/mutated (P4)
Classification: Mechanical
rbxlx deep-copies the template RbxPart and re-emits under ReplicatedStorage (sibling of Templates); luau builds a second copy into an RS var via the same part-emit, then AddTag+SetAttribute on THAT var. Tagging the canonical child in place would make GetTagged()[i].Parent differ across emitters and corrupt the template. Parity AC asserts equal {parent-context, tag-set, attributes}. Inertness verified: RS scripts don't auto-run AND the generic runtime host scans `workspace`, not RS (converter/runtime/scene_runtime.luau binds via workspaceFind / workspace:GetPartsInPart / GetDescendants on workspace objects only).

## P1-D4 (revised) — ONE canonical Tags codec per writer; FOLD the existing single-Tag path in (P2)
Classification: Mechanical
rbxlx_writer.py:942-948 ALREADY writes <BinaryString name="Tags"> as plain text from attributes["Tag"]. A second base64/NUL-delimited Tags element = two incompatible encodings + two <Tags> elements on a member carrying both. Fix: `_emit_collection_tags(props, tags)` is the SOLE writer of name="Tags" — collect {attributes["Tag"]} ∪ {roster label tag}, dedup, emit ONE element. MANDATORY merge (no "leave untouched/optional" hedge). Wire encoding (plain vs base64 NUL-delimited) is a VERIFY-stage risk; single-codec/single-element is a Phase-1 requirement. Mirror the same single-AddTag-site invariant on the luau side.

## P1-D5 (revised) — characterName selected by FIELD-PRESENCE (config contains key `characterName`), tiebreak via modules[script_id].class_name (P1)
Classification: Mechanical
The planner's instances[*] rows (scene_runtime_planner.py:969-977) carry only script_id + opaque config — NO component-type tag — and the Cat prefab has 5 MonoBehaviour instances, so a class-based selector is not implementable. Select the instance whose config CONTAINS the key `characterName` (the consumer's GetAttribute contract key, NOT a "Character" class literal); tiebreak ONLY via scene_runtime["modules"][script_id]["class_name"]; on remaining ties pick first-in-lifecycle + warn. Fallback address→stem; omit if absent. CORRECTION: the round-1 "serialized_field_extractor strips m_ at :73-74" rationale is FALSE (verified — that extractor avoids a blanket m_ filter and only handles object refs; the planner does not strip m_ either). characterName is un-prefixed because Character.cs declares it `public string`.

## P1-D7 (new) — assemble_rosters dedups on (label, prefab_id); pipeline keeps `place.rosters.extend(...)` (P3)
Classification: Mechanical
by_label is built via setdefault().append() (addressables_resolver.py:128-129) with NO dedup → a prefab under two AddressableEntry rows duplicates the prefab_id → GetTagged returns the member twice. Dedup at assembly is the load-bearing guard. `extend` is SAFE (mirrors replicated_templates.extend; self.state.rbx_place is rebuilt fresh from convert_scene each conversion at pipeline.py:2601) — no set/replace hedge. Twice-call idempotency test kept as hygiene, re-anchored on the dedup.

## P1-D8 (new) — Explicit str-narrowing at EVERY scene_runtime/config read; non-str → skip-with-warning, fail-closed (P5)
Classification: Mechanical
config is dict[str, object]; template_name/by_label arrive untyped from persisted JSON; RbxAttrValue admits only scalars. Narrow with isinstance(v, str) (and the scalar set for attributes) before use; else skip-with-warning — never coerce (str(v)), never Any. A malformed member (non-str characterName) is still tagged + clonable; only the bad attribute is omitted. AC for this case.

---
## Phase-1 detailed-design REVISION 2 after dual-voice round 2 (review-phasedesign1-2.md) — 2026-06-16
P1-D2/P1-D3 below REVISE the round-1 revisions above. P1-D9/P1-D10 are NEW (the 2 round-2 BLOCKING findings). Verified against real worktree code this session. (Full wording in design-phase1.md §1.4/§1.4a/§1.5/§5.)

## P1-D2 (revision 2) — Deep-copy must RE-KEY referents/unity_file_ids fresh; members live under a dedicated container (P4, NEW-BLOCKING-a)
Classification: Mechanical
A naive copy.deepcopy of a template RbxPart retains its unity_file_id, and rbxlx_writer mints a part's referent FROM unity_file_id (pre-pass _pre_register at rbxlx_writer.py:1420-1429; _make_part at :854-857) → the copy and template get the SAME referent (duplicate referent IDs), and a constraint Part1 (resolved from connected_body_file_id via the global fid→referent map at :392-398) cross-links the copy to the TEMPLATE part. Fix: per-copy old_fid→new_fid remap assigning fresh-unique fids to every copied part, rewrite each constraint's intra-subtree connected_body_file_id through the remap (drop Part1 for external bodies), and extend the pre-pass to walk place.rosters copies so fresh fids get referents. luau path needs NO re-key (verified: it keys nothing on a global id — Instance.new per part; Motor6D Part0/Part1 by name via FindFirstChild(name,true) at :941-954; _emit_constraint at :858-872 sets no Part0/Part1). AC12 asserts every referent in the file is unique + intra-member Part1 resolves within the copy.

## P1-D3 (revision 2 — SUPERSEDES round-1 "tag-primary, NO folder") — Members live under ONE dedicated collision-checked container Folder (NEW-BLOCKING-b)
Classification: User-Challenge
Bare RS-root siblings named from template_name reintroduce the WaitForChild/auto-RemoteEvent shadowing both writers reserve against (rbxlx_writer.py:1683-1697 / :1685-1701; luau_place_builder.py:617-629). Fix: parent all roster members (across labels) under ONE Folder under ReplicatedStorage whose name passes the SAME reserved-RS-name dedup both writers build — default base RosterMembers, suffix-disambiguate on collision (Templates / a ModuleScript / a template / scriptable_objects/<Group>.luau), and add the chosen name to _reserved_rs_names on BOTH writers so no auto-created RemoteEvent shadows it. CollectionService tag (GetTagged) is the discovery key, so the container name is free to be disambiguated — it need NOT be "Characters" or the label. This ALSO resolves the round-1 dead-AssetGroup-SO module collision (no RS-root sibling named like the module) and keeps members clear of the host's RS:FindFirstChild("Templates")/Templates:FindFirstChild lookups (autogen.py:844-846). AC13 asserts container + member names pass the reserved-RS-name check on BOTH emitters.

## P1-D9 (new) — Roster label tag applied to the member ROOT only, carried via a dedicated marker attribute (_RosterTag) unioned by _emit_collection_tags / the AddTag site
Classification: Mechanical
The deep-copy/rebuild produces a whole subtree, but GetTagged(<label>) must return exactly the member ROOTS, not their descendants. Carry the label on the copy ROOT via attributes["_RosterTag"] (set when materializing the member, root only, never propagated to descendants); _emit_collection_tags (rbxlx) and the single AddTag site (luau) union {attributes["Tag"]} ∪ {attributes["_RosterTag"]} for that part and STRIP _RosterTag from the emitted AttributesSerialize / SetAttribute (it is a marker, not a real attribute). Descendants carry no label.

## P1-D10 (new) — Inertness rationale (P4) reworded: host NEVER enumerates RS, only NAMED lookups
Classification: Mechanical
Round-1 wording "nothing iterates RS" was imprecise. Verified: the host DOES named RS lookups (RS:FindFirstChild("Templates") at autogen.py:844-846; bootstrap WaitForChild) but NEVER enumerates RS children (no RS:GetChildren/GetDescendants); all component-binding scans run on workspace clones (scene_runtime.luau binds via workspaceFind / workspace:GetPartsInPart / clone:GetDescendants on workspace objects). So members under a dedicated container are invisible to host binding AND clear of the named Templates lookups.

---
## Phase-1 detailed-design REVISION 3 after dual-voice round 3 (review-phasedesign1-3.md) — 2026-06-16
Round 3 confirmed rounds 1-2 BLOCKERS fixed; both voices converged on ONE remaining coupled BLOCKING: the rbxlx Tags emission does not provably produce a working CollectionService:GetTagged. P1-D4 below is REVISED (supersedes "wire encoding deferred to VERIFY"). Verified against real worktree code this session: rbxlx_writer.py:942-948 is the SOLE name="Tags" producer in the whole codebase and sits INSIDE the `if part_class != "Model":` branch (859-995); AttributesSerialize at :1059-1063 is OUTSIDE that branch; `import base64` present at :10; scene_converter._wrap_geometry_with_children_into_model sets class_name="Model" at :2185 for any prefab node with geometry+child transforms (multi-part character prefabs ⇒ Model-rooted); no existing test asserts on the Tags element (grep tests/ for name="Tags"/GetTagged/CollectionService = none). (Full wording in design-phase1.md §1.5/E12/AC4/AC7/AC11/AC12/P1-D4.)

## P1-D4 (REVISION 3 — SUPERSEDES "wire encoding deferred to VERIFY") — Tags codec SPECIFIED + LIFTED in Phase 1 (round-3 Tags-contract BLOCKING)
Classification: Mechanical
The Tags emission is fixed in three coupled parts, all Phase-1 (offline), with ONLY the live GetTagged boot left to VERIFY:
1. LIFT — `_emit_collection_tags(props, tags)` is called in the COMMON tail next to AttributesSerialize (rbxlx_writer.py:1059-1063, verified OUTSIDE the `part_class != "Model"` branch that closes at :995), NOT inside the non-Model branch where the current emit sits. So every roster member root — Part- or Model-rooted — emits its Tags element; previously Model roots (the common multi-part character case, e.g. the Cat prefab) emitted none → GetTagged empty, and a single-Part fixture passed falsely.
2. CODEC — `_emit_collection_tags` emits exactly ONE `<BinaryString name="Tags">` whose content is the tag list joined by NUL (\0) as UTF-8, base64-encoded (the canonical Roblox CollectionService encoding). The existing plain-text emit (tags_elem.text = unity_tag, :948) is FOLDED IN, which also corrects that pre-existing wrong (raw-text) encoding. ONE codec for both the Unity-Tag path and the roster-label path.
3. ACs — AC4 decodes the BinaryString (base64 → split on \0) and asserts the label present, run on a MODEL-ROOTED multi-part fixture (the AC12 fixture, made the SHARED fixture so the Model-root path is actually exercised). AC11 asserts exactly one Tags element with the decoded union {Unity tag} ∪ {label}. AC7 cross-emitter parity PARSES the actual rbxlx (decode) vs the luau AddTag strings — never luau-vs-intended. Live GetTagged boot stays AC6/VERIFY.
luau path is encoding-INDEPENDENT (CollectionService:AddTag(var, tag) takes the plain string; the engine owns the in-memory store) — ONLY the rbxlx XML path needs the wire-codec fix.

## P1-D4 regression risk (folding the single-Tag plain-text path into the canonical codec) — ASSESSED: NONE on covered behavior
Classification: Mechanical
rbxlx_writer.py:947 is the ONLY name="Tags" producer in the codebase and NO existing test/output asserts on its content/encoding (grep of tests/ and roblox/ = no Tags assertion). So folding the untested plain-text emit into base64 regresses no covered behavior; it replaces an untested, likely-non-loading plain-text form with the proven Roblox codec. The design also UNIONS (not replaces) the Unity m_TagString tag with the roster label and dedups, so a part carrying a Unity tag keeps it (now correctly encoded) — and a Model carrying a non-Untagged m_TagString, which the old branch-scoped emit silently DROPPED, now emits its tag too (a pre-existing gap also closed by the lift). VERIFY confirms pre-existing single-Tag parts still emit correctly under the merged codec.

## Re-observation (live, on PR-#24 base) — 2026-06-16

- **Unit 4 CONFIRMED still needed.** Fresh generic conversion on the #24 base (af4a1c2 = Units 1+2 old + #24 require-closure fix), headless smoke: scriptErrorCount=0, boot_health_ok, wasd_works. BUT no roster surface exists — the only `Characters` in the rbxlx is the dead AssetGroup ModuleScript (line 17864), not a Folder of character instances. CharacterDatabase.LoadDatabase finds container=nil → m_CharactersDict={} → empty roster (no crash, but non-functional loadout). #24 shifted the symptom from boot-crash to silent-empty; the roster still needs populating. Classification: premise-confirmation.
- **NEW-FINDING-A (consumer drift shape):** this conversion's transpiled CharacterDatabase reads the FOLDER shape (ReplicatedStorage:FindFirstChild("Characters") + GetChildren + op:GetAttribute("characterName")), NOT GetTagged (unit2-proper). Confirms AI drift across the 3 shapes → validates anchoring on by_label and REQUIRES Phase 2's consumer re-lowering (the producer surface alone won't satisfy every shape). The current folder-shape consumer needs `ReplicatedStorage.Characters` to be a FOLDER of instances OR the re-lowering to normalize it to the tagged surface.
- **NEW-FINDING-B (dead-module interaction):** CharacterDatabase is flagged Roblox-dead ("inert output + dead-leaning input prior") — chicken-and-egg: inert because the roster is empty. Phase 2 must ensure the re-lowered, roster-backed CharacterDatabase is NOT dead-stubbed (or the dead-module classifier must see it as live once it has real data). Fold into Phase 2 detailed design.
- **BASE dependency:** this run is based on upstream/main (ntornow#196), which LACKS #24. A boot on this base would hit the #24 require cascade (masking). #24 is merged on the jiazou fork (into the superseded old Unit-2 branch) but NOT upstream. Unit 4 needs #24 in its base to verify/ship end-to-end.

---
## Phase-2 detailed-design decisions (consumer re-lowering) — 2026-06-16
Dual-voice design review (codex + Claude design subagent) both converged. Verified against real worktree code + the real transpiled output (unit1 == reobs24 FOLDER shape) + the C# source (CharacterDatabase.cs:33 LoadAssetsAsync<GameObject>("characters",...)) + an EMPIRICAL classify_module_dead run. Full wording in design-phase2.md §5.

## D-P2-1 — Identify the roster consumer by a DETERMINISTIC upstream fact, NOT a transpiled-body fingerprint
Classification: Taste (architecture)
The consumer module is the one whose ORIGINAL C# calls Addressables.LoadAssetsAsync<...>(L) for an L present in scene_runtime["addressables"]["by_label"] (keys on by_label per D1). New module roster_consumer_lowering.py + typed RosterConsumerFact{source_path,label,component_type,index_key}. Abstains on empty by_label / non-literal label arg; fail-closes (roster_ambiguous) on >1 consumer of one label and (roster_signal_absent) on a stale artifact — symmetric with the player-binding facts. Body-fingerprinting was REJECTED by both reviewers: it misfires across the 3 AI drift shapes (WaitForChild folder / FindFirstChild folder / GetTagged) and on other label loaders.

## D-P2-2 — Re-lowering WHOLE-REGION-REPLACES the four public methods + state decls with a GAME-AGNOSTIC canonical object-graph body; never patches the AI drift string
Classification: Taste (architecture)
REVISED (review-phasedesign2-1 P1): the canonical body contains NO per-game literal. The receiver-table name <N> is READ from the located `function <N>.LoadDatabase`/`function <N>.GetCharacter` anchor (the C# class/file stem; verified per-game — unit1 + unit2-proper both name the table after the stem) and substituted at render time, and the body OWNS its own <DICT>/<LOADED> module-locals + rewrites the dictionary()/loaded() accessors to read them, instead of assuming the AI's m_CharactersDict/m_Loaded upvalues survive. The body is built from <N> + the fact + Phase-1 contract constants (label, GetTagged surface, characterName attribute, Templates children) so it is identical for a given <N> across all 3 drift shapes — that IS the AI-drift reconciliation AND the idempotency mechanism (deterministic name substitution → twice-call byte-equal). No `CharacterDatabase` literal in the template or pass source → AC8 grep passes. Returns the spike minimal shape: .characterName (string), .gameObject (clonable), .accessories = {} (populated = follow-on), via <C>.new. Lives in contract_pipeline.transpile_with_contract beside camera-facet/trigger-stay/rifle-rig-retarget. GetTagged is RS-scoped (IsDescendantOf(ReplicatedStorage)) + name-sorted for determinism. Unlocatable LoadDatabase/GetCharacter anchor → roster_unresolved fail-closed.

## D-P2-3 — Keep the re-lowered module LIVE via a deterministic exemption carrier (roster_binding); do NOT rely on the incidental _PROP_WRITE veto (NEW-FINDING-B)
Classification: User-Challenge
EMPIRICALLY verified this session: the current empty-roster CharacterDatabase body classifies Roblox-dead (classify_module_dead: input prior 0.33 <= 0.49 ceiling, output inert) and a re-lowered body only escapes by ACCIDENT (c.gameObject = op is a dotted _PROP_WRITE that trips has_genuine_roblox_effect; Character.new({gameObject=op}) constructor-arg form would NOT veto -> dead-stubbed). Both reviewers: exempt via a carrier (option c), mirroring rig_binding EXACTLY across 6 sites — stamp on TranspiledScript (lower_roster_consumers); add roster_binding to RbxScript; copy at pipeline.py:3135; exempt before classify_module_dead in _subphase_analyze_dead_modules (:3350-3365); persist (:4965/:5135) + rehydrate (:4640/:4704/:4815) + resume-revalidation (:3304). classify_module_dead stays PURE/unchanged. Dead modules with live requirers get rerouted to an inert RS stub (storage_classifier) if not exempted — clobbering the re-lowering.

## D-P2-4 — c.gameObject binds the SCRIPT-BEARING ReplicatedStorage.Templates.<characterName> child, NOT the script-stripped RosterMembers member (codex BLOCKING)
Classification: Mechanical
Phase 1 strips Script children from roster members (clone-sources, roster_assembly.strip_member_scripts:55). LoadoutState:SetupAccessory calls host.getComponent(m_Character,"Character") on the cloned character; if .gameObject were the stripped member the clone carries no Character script -> getComponent nil -> crash. The roster member is discovery+identity (tag + characterName attribute); the clonable script-bearing object is the Templates child (scripts retained per Phase-1 §1.4a). Fallback to the tagged member when no Templates match. RESIDUAL: Phase 1 allows characterName != template_name, so FindFirstChild(characterName) can miss; a fully-generic bind keys on the member->template prefab_id relation -> followups (not boot-blocking for Trash-Dash where characterName == template stem).

## D-P2-5 — GetTagged(L) is global+unordered -> scope to IsDescendantOf(ReplicatedStorage) + sort by name (codex)
Classification: Mechanical
Prevents a future workspace clone carrying the tag, or non-deterministic ordering, from corrupting m_CharactersDict.

## D-P2-6 — ONE slice (the roster_binding carrier is a shared producer->consumer contract; the lowerer is unsafe without the exemption)
Classification: Taste (architecture)
Two responsibilities (re-lowering pass + dead-module exemption), one shared contract (roster_binding), not independently shippable (a re-lowered-but-dead-stubbed CharacterDatabase = empty loadout). Shared-contract rule -> one slice. No fan-out, no staged-risk seam inside Phase 2 (that boundary was the Phase-1->Phase-2 split).

---
## Phase-2 detailed-design REVISION 1 after dual-voice round 1 (review-phasedesign2-1.md) — 2026-06-17

## D-P2-7 — SPLIT the §1.3 orchestrator wiring: lowering + roster fail-closed COMPUTED at ~:618, AGGREGATED into fail_closed AFTER its definition at contract_pipeline.py:625
Classification: Mechanical (review P1)
fail_closed first exists at contract_pipeline.py:625 (`fail_closed: list[FailClosed] = list(player_fail_closed)`), AFTER the lowering passes at :567/:602/:615; a single-site `fail_closed.extend(...)` near :619 would NameError (verified this session). Site A (~:618) imports find_roster_consumers/lower_roster_consumers, derives by_label + csharp_by_path + roster_facts, computes roster_fail_closed via the new _roster_fail_closed helper, and runs lower_roster_consumers when no fail-closed. Site B (after :625) does `fail_closed.extend(roster_fail_closed)`. _roster_fail_closed mirrors the player_signal_absent guard shape at contract_pipeline.py:510: roster_signal_absent fires when by_label is non-empty but scene_runtime has no `addressables` block (artifact predates the Unit-4 surface, unrecomputable here); roster_ambiguous fires on >1 distinct module consuming one label. roster_unresolved (unlocatable method) is raised by the lowerer and drained through the same path. FailClosed.kind ∈ {roster_ambiguous, roster_signal_absent, roster_unresolved}.

## D-P2-8 — The roster_binding dead-set `continue` is a PURE exemption placed before the _DECISIVE_STRATEGIES gate (pipeline.py:3358); re-lowering does NOT change `strategy`. FOLDER-shape .gameObject is not boot-critical
Classification: Mechanical (review P2)
The continue is placed right after the existing `csharp is None` skip (pipeline.py:3357) and BEFORE the _DECISIVE_STRATEGIES gate at :3358 (frozenset({"ai","stub"})). lower_roster_consumers mutates luau_source + stamps roster_binding but never touches script.strategy, so a re-lowered module keeps "ai" and would still pass the strategy gate into classify_module_dead — i.e. it is NOT masked by the strategy gate; the carrier continue is the SOLE deterministic reason it is skipped. For the FOLDER drift shape (unit1 FindFirstChild / WaitForChild), .gameObject is not the boot-critical path: unit1 LoadoutState does its OWN Templates lookup keyed on .characterName, so the boot is satisfied by GetCharacter returning .characterName + .accessories = {} (clears the #c.accessories guard at LoadoutState:271). .gameObject is boot-critical only for the GetTagged/unit2-proper c.gameObject:Clone() shape (D-P2-4 binds it to the script-bearing Templates child). The re-lowering normalizes the DB module ONLY, never LoadoutState.

---
## Phase-2 detailed-design REVISION 2 after dual-voice round 2 (review-phasedesign2-2.md) — 2026-06-17

## D-P2-9 — The Character.new wrapper is the canonical GetCharacter/dictionary() return contract; post-boot Instance-expecting consumers (TrackManager, ShopCharacterList) are a KNOWN-DIVERGENT scoped FOLLOWUP, not silently shipped
Classification: Taste/scope (tier-problems discipline)
The re-lowering changes the return from a raw tagged Instance to a `Character.new` WRAPPER TABLE — REQUIRED+CORRECT for the boot: LoadoutState derefs c.gameObject/c.accessories/c.characterName, which a raw Instance cannot carry, and the wrapper matches Unity's Character OOP object the C# m_CharactersDict.Add(c.characterName, c) stored. The Gate-A ONE goal is the COLD BOOT (AC9), which the wrapper satisfies. Enumerated consumers (verified against converter/output/trash-dash-unit2-proper-20260616/scripts/ + trash-dash-unit1-20260615/scripts/ in the main repo):
  - LoadoutState:PopulateCharacters — boot-critical, SATISFIED (the wrapper is built for this deref).
  - Character.luau (Character.new) — the wrapper type itself, SATISFIED (Phase 2 untouched).
  - ShopAccessoriesList.luau:55-56 (#c.accessories off dict values) — incidentally SATISFIED (wrapper carries .accessories = {}; member skipped, no error).
  - TrackManager.luau:204→:209 (unit2-proper, instantiatePrefab(GetCharacter(charName))) — POST-boot, Instance-expecting → DIVERGENT (table → player fails to spawn on Run).
  - ShopCharacterList.luau:39/:46-49 (dictionary()-value :GetAttribute) — POST-boot → DIVERGENT (table has no :GetAttribute → error on shop open).
Per the tier-problems rule: root (boot) fixed in Phase 2; downstream-conditional (post-boot character-object consumers) scoped as a follow-on (followups.md). Phase 2 does NOT re-lower TrackManager/Shop and does NOT expand the mechanism, trigger, dead-module carrier, or .gameObject binding. SHAPE-SPECIFIC: unit2-proper affected; unit1 TrackManager.luau:152 passes the name string → unaffected. AC10 makes the boundary explicit so the divergence is disclosed, not a latent regression.

<!-- promoted from /drive run trash-dash-playable-20260617T192649 (2026-06-18) -->
# Decisions — trash-dash-playable-20260617T192649

- Structural validator fix over a regex/name denylist: key the proven-vs-not decision on
  whether the receiver resolves to a require()'d local module (deterministic upstream signal),
  not on a method-name list. A denylist abstains silently the moment the AI emits a valid-but-
  different shape.
  Classification: Mechanical

- Single-point fix in `roblox_call_validator.py`: it is the one source feeding both the
  contract verifier and the `_repair_invalid_roblox_calls` reprompt path; fixing it there
  corrects both without touching the reprompt loops.
  Classification: Mechanical

- Phase the downstream gameplay-spawn work behind a live Phase-1 verify: don't pre-scope
  Addressables fixes against a masked, unobservable state — re-convert first, then scope from
  the playtest.
  Classification: Mechanical

- Verify end-to-end via fresh `/convert-unity` conversion + Studio playtest, not unit tests
  alone: the bug only surfaces on the integrated transpile→reprompt→runtime path.
  Classification: Mechanical

- Hold the secondary reprompt-degradation guard (reject a degraded/truncated reprompt
  response, keep the original) as a design alternative, not a commitment; decide at Phase-1
  detailed design.
  Classification: Taste

## Plan-stage decisions (trash-dash-playable) — 2026-06-17
- Root cause anchored on a FRESH current-main conversion + live Studio playtest, NOT the stale unit2 build (which misled toward an already-merged storage fix). Classification: Mechanical.
- Primary fix = structural change in roblox_call_validator.py (key on require()-binding-RHS provenance, not a method-name denylist). Classification: Mechanical.
- Signal methods (:Connect/:Once/:Wait) recognized via a corpus addition, not a global validator special-case (avoids nonSignal:Connect bypass). Classification: Taste.
- Two phases, staged-risk: Phase 2 (downstream Addressables gameplay) scoped ONLY after the Phase-1 re-convert + playtest reveals what the revived state machine exposes. Classification: Mechanical.
- Phase-1 verification = cold-cache fresh conversion + Studio playtest; exit criterion is STRUCTURAL (4 state modules return valid class tables) + a run starts, not a lucky boot. Classification: Mechanical.
- Reprompt-degradation guard held as a lean-include hardening candidate; decided at Phase-1 detailed design. Classification: Taste.
- Verify END-TO-END (fresh /convert-unity-style conversion + Studio), not unit tests alone. Classification: Mechanical.
- NOT re-fixing ServerStorage misrouting (already fixed on main by #199/#200 reachability closure). Classification: Mechanical.

## Gate-A refinement (user challenge: "more elegant/general? prior work?") — 2026-06-17
- PRIOR WORK identified: PR #197 (c85fb6f) built the whole roblox_call_validator — a provenance-gated validator + vendored corpus + bounded repair-reprompt, with a zero-FP fixture proof (6c66a2f). The bug is a FALSE-POSITIVE HOLE in that system.
- MORE ELEGANT/GENERAL fix (replaces the original "new module track-state"): EXTEND #197's two existing mechanisms, don't add parallel machinery.
  (1) Provenance: bind a `require(...)` RHS to the EXISTING `component` non-Roblox-table state in `_rhs_provenance` (roblox_call_validator.py:384) — the `.Instance` promotion-suppression for `component` bases (`:292`) already exists. DRY reuse, ~1 branch. Classification: Mechanical (clearer, reuses existing).
  (2) Corpus: add RBXScriptSignal members (Connect/Once/Wait/ConnectParallel) via the EXISTING tools/refresh_roblox_corpus.py — data-driven, not a validator special-case. Classification: Mechanical.
  (3) Extend #197's zero-FP fixture set with the Module.Instance:Method() + event:Connect cases. Classification: Mechanical.
- Size dropped (~15-50 SLOC) vs the original framing because the fix reuses existing state rather than adding one. The user's challenge improved the plan.

## Gate-A evidence revision (user challenge: "what does Phase 1 actually solve? prove it") — 2026-06-17
- Cache forensics (.cache/llm, evidence-phase1-sufficiency.md) PARTIALLY REFUTED "validator FP fix alone -> full modules":
  the broken state modules are reprompt-acceptance DEGRADATIONS (kept a `...` fragment), and the full
  versions carry INDEPENDENT rule-a/b contract violations that fire the contract reprompt on their own.
- DECISION: promote the reprompt-degradation guard from optional-secondary to Phase-1 PRIMARY/load-bearing
  (keep the better of {original, reprompt}; never accept a structurally-worse/fragment output). The validator
  FP fix stays as a quality lever (fewer spurious reprompts). Classification: Mechanical (evidence-forced).
- DECISION: Phase-1 exit criterion is STRUCTURAL + empirical (4 modules return valid class tables + load on a
  COLD re-convert), explicitly NOT "a run starts." rule-a/b runtime correctness + gameplay spawning are
  surfaced by the playtest and (gameplay) deferred to Phase 2. Classification: Mechanical.
- What Phase 1 solves, bounded: state modules load -> state machine boots (the current hard blocker). It does
  NOT by itself guarantee rule-a/b runtime correctness or gameplay spawning. Classification: Mechanical.

## Phase-1 detailed-design decisions — 2026-06-17
- Reprompt-degradation guard lives in a SHARED pure helper (`_reprompt_is_structurally_safe`) that BOTH `_verify_and_reprompt` AND `_repair_invalid_roblox_calls` call — the repair path has the identical unconditional-accept shape/risk; one predicate closes both (DRY). Classification: Mechanical.
- "Structurally worse" defined CONSERVATIVELY: reject only on parse-loss (candidate fails parse while original parsed), top-level-return-loss, or size-collapse <50%; function-count-drop is a contributing signal only, never standalone. Biases to ACCEPT a smaller-but-correct reprompt (false-reject re-ships the original broken module = the costly error). Classification: Taste.
- Length floor 0.5 (stricter than the existing 0.3 at `_reprompt_fix:2656`): a contract/repair reprompt should return a corrected FULL module; 0.5 clears every legitimate smaller-correct case and catches the 0.07-ratio fragment. Classification: Taste.
- On guard rejection, re-surface the ORIGINAL violations as survivors reusing the existing failed/empty-reprompt branch shape; NO new warning tag (keeps the compliance-spike taxonomy stable; fails-closed at project level exactly as today). Classification: Mechanical.
- KEEP the `component` state name and broaden its docstring (do NOT rename to `non_roblox_table`): a require'd module is the same class as a host-component result; the existing `.Instance` promotion-suppression already handles any `component` base. Rename = pure churn (TrackLit + 5+ sites + literal-asserting tests) for zero behavior change. Classification: Taste.
- `_rhs_provenance` binds a `require(...)` RHS to `component` via a new `_is_require_call(rhs)` keyed on the BINDING RHS, not the variable name (a real Roblox var bound from `workspace:FindFirstChild` stays proven and is still checked; a require'd-local reassigned to a Roblox instance upgrades via the plain-reassign rule). Classification: Mechanical.
- RBXScriptSignal members (Connect/Once/Wait/ConnectParallel) added via a curated constant unioned into `tools/refresh_roblox_corpus.py` then regenerate — they live in the API dump's data-type section the tool's `Classes`/`MemberType==Function` filter cannot reach, so the tool MUST supply them; routing through the corpus keeps `Connect` under the provenance gate (no `nonSignal:Connect` blanket bypass). Classification: Mechanical.
- `nonSignal:Connect()` on a wrong-typed receiver is accepted-with-rationale / out of scope: the validator catches "nonexistent Roblox method NAME", not "method on wrong-typed receiver"; `Connect` is a real name. Classification: Taste.
- Phase 1 = two slices FAN-OUT/parallel (1.1 guard in code_transpiler.py; 1.2 validator FP + corpus) — disjoint files, no shared NEW contract (the `find_invalid_roblox_calls` signature is unchanged), so fan-out allowed. The COLD-cache `/convert-unity` re-convert + playtest is a phase-integration step (criteria 15/16), not a slice dep. Classification: Mechanical.
## Run door-binding-race (2026-06-17) — placement-order-robust Anim_* binding

# Decisions — door-binding-race run

## D1 — Root cause confirmed empirically (premise grounding)
The F10 door-never-opens bug is NOT a domain bug (PR #195 already fixed that). It is a
binding-PLACEMENT-ORDER race in the emitted `Anim_*` animation-driver LocalScript:
`generate_tween_script()` (converter/converter/animation_converter.py) emits a ONE-TIME
startup scan (`workspace:FindFirstChild(name,true)` + a one-pass `workspace:GetDescendants()`
fanout, no DescendantAdded/retry). Generic scene-runtime places the Door prefab at RUNTIME
(scene_runtime.luau instantiatePrefab/_constructPrefabClone) AFTER the LocalScript's scan ran,
so the script binds nothing and the door never tweens.
- Fix lever: the binding template in generate_tween_script() — deterministic Python, has tests.
- Fix approach (general): placement-order-robust binding — a workspace.DescendantAdded listener
  that wires late-arriving same-named targets into the param-driven tween, preserving the eager
  scan for parts that already exist at boot. Applies to ANY Animator-driven animation (generic).
- baseRef: upstream/main @ 5b3efaa (includes #207). PRs target ntornow upstream.

## D2 — Late-arrival mechanism: workspace.DescendantAdded (not retry-poll)
Event-driven binding is order-robust by construction and has no poll window/timeout
to tune; the runtime prefab placement is a discrete event the listener catches.
Classification: Taste (close call vs bounded poll; design recommends DescendantAdded).

## D3 — Ride the existing `not _ownerIsContainer` guard for the late-arrival path
The guard already separates the flat-list scene-scoped driver (door's case, late
listener REQUIRED) from the per-clone prefab-scoped driver (already binds its own
instance, MUST NOT start a workspace-wide listener). The late-arrival path has the
identical scoping need, so it emits under the same condition — keeps prefab-scoped
shape unregressed. Classification: Mechanical.

## D4 — Factor a single `bind(_t)` step shared by eager scan + late listener
One closure per param branch (bool keeps its `_isActive` open/close state machine;
int keeps its replay), called by both the boot loop and DescendantAdded, with a
`_bound` set for idempotency. Guarantees boot and late paths are semantically
identical. Classification: Mechanical.

## D5 — Apply current param state on late bind
A target placed after its param already flipped open should snap/tween to the active
state on bind, not stay closed until the next flip. Read attribute on bind; drive
playAnimation if active. Default-attribute write only when currently nil (don't
clobber a runtime-set value). Classification: Taste.

## D2 — Hoist late-arrival binding above the boot-target early-return (round-1 BLOCKING)
The DescendantAdded listener must NOT be nested under the `if not target then return end`
in generate_tween_script (animation_converter.py:1305-1308): for a door absent at boot the
script returns before reaching it. Fix: in the scene-scoped (`not _ownerIsContainer`) case,
don't hard-return on nil boot target — emit listeners + late-arrival path keyed on the
compile-time name constant (target_name/clip.name/curve_roots), not a runtime target.Name.
Classification: Mechanical (required for correctness).

## D3 — DescendantAdded resolves on ANY matching event, not only the Model's own (round-1 MAJOR)
A named Model can fire DescendantAdded before its PrimaryPart/BasePart exists. Listener matches
same-named Model OR same-named BasePart so the target binds whenever a usable instance arrives.
`_bound` weak-keyed / dropped on destroy to avoid unbounded retention. Classification: Mechanical.

## D4 — No target-dependent prologue on the nil-boot path (round-2 BLOCKING)
Removing the early-return is insufficient: Model-normalization (1310-1313) and `_initialTarget`
(1331) dereference `target` and throw on nil. Structural rule: guard ALL target-dependent prologue
inside `if target then ... end`; the param-listener + DescendantAdded emission runs unconditionally,
keyed on compile-time name constants. Boundary ~1263-1331. Classification: Mechanical.

## D5 — Reachability confirmed: generalize to the SHARED layer (all 4 trigger shapes)
Empirical evidence (conversion run 2026-06-17T09-17-02, SimpleFPS generic):
- The bug lives in the SHARED boot-scan + early-return (generate_tween_script:1305-1308), inherited by
  ALL trigger branches: bool param, int param, loop-autoplay (1388-1392 / 1628-1632), play-once (1393-1395 / 1633-1635).
- SECOND reachable symptom found: `Anim_HostilePlane_HostilePlane_Flying.luau` is a LOOP-autoplay anim
  targeting the `HostilePlane.prefab` — a RUNTIME-placed prefab (1 entry in scene_prefab_placements,
  same as Door's 6). Its script lives in ServerScriptService (global container) and does
  `workspace:FindFirstChild("HostilePlane")` + early-return → the flying loop never starts (currently broken).
DECISION: hoist the placement-order-robust binding to the SHARED layer so bool/int/loop/play-once ALL
bind runtime-placed targets via one scaffold (eager scan + DescendantAdded late-arrival, gated on runtime
`not _ownerIsContainer`); per-branch action varies trivially (connect listener / task.spawn loop / play once).
Side effect (improvement): loop/play-once gain multi-instance fanout (previously single-target).
Classification: Taste (scope width — user chose "general fix, verify reachability first"; reachability verified).

## D6 — Per-iteration yield floor on the loop (round-4 MAJOR)
The task.spawn loop emits `RunService.Heartbeat:Wait()` per iteration so a contentless clip
(curves all simplify <2 keyframes → playAnimation never yields) cannot tight-spin and freeze the
DataModel. Without it the fix would worsen "never animates" into "freezes" for a loop on a
runtime-placed prefab. RunService already imported (1251). Detailed design SHOULD also skip the loop
for no-content clips, but the yield floor is load-bearing. Classification: Mechanical.

## D7 — `_bound` is a weak-key table, not a strong set drained on destroy (detailed-design)
`setmetatable({}, {__mode="k"})`: a destroyed BasePart key is GC'd automatically → no unbounded
retention (the only `_bound` concern, design.md §4) and zero per-instance lifecycle code (no
`.Destroying` connect to remove keys). The loop branch's `while _t and _t.Parent do` guard
independently stops work on a destroyed instance, so weak-key vs strong-drop is not load-bearing for
correctness — choose the lower-code weak-key form. Classification: Mechanical.

## D8 — Factor ONE Python emit helper for the bindTarget/scan/listener scaffold (detailed-design)
`_emit_placement_robust_binding(lines, clip, match_names, action_kind, param_name)` emits the
byte-identical bindTarget closure + `_bound` weak table + eager `bindTarget(target)` + GetDescendants
fanout + DescendantAdded listener, dispatching only the per-shape ACTION body (bool/int/loop/once) and
the param name. Rationale: the scaffold is identical across all four shapes and the match-name
precedence (target_name → curve_roots → clip.name) must be computed ONCE and used in two emit sites
(fanout + listener) so they cannot drift; four inline copies would be ~100 duplicated emitted-template
lines and would fracture the shared contract. Strict typing: `list[str]` / `Literal[...]` / `str`, no
`Any`. Classification: Mechanical.

## D9 — Skip the loop AND keep the yield floor for no-content clips (detailed-design)
Emit BOTH guards (belt-and-suspenders, per design.md §1 step-1 loop bullet): the load-bearing guard is
the unconditional per-iteration `RunService.Heartbeat:Wait()` yield floor (cannot tight-spin even if the
content predicate is ever wrong); ADDITIONALLY, when the clip has no surviving tween content (every
curve simplifies to <2 keyframes — the same predicate `_generate_curves_code` uses at 1413-1415), emit a
single `playAnimation(_t)` instead of the `task.spawn` loop, since a contentless loop animates nothing.
The yield floor remains the authority; the content-skip is an optimization that also reads cleaner.
Classification: Mechanical.

## D10 — Hoist `_ownerIsContainer` to the shared prologue (detailed-design)
Currently `_ownerIsContainer` is computed twice inside `_generate_parameter_driven_playback` (bool 1575,
int 1609) and absent from the loop/play-once fallback. The unified scaffold needs it BEFORE the
early-return (to gate the return) and in all four shapes (to gate fanout/listener), so emit it ONCE in
the prologue (just after boot-resolve) and delete the two in-function copies. The early-return becomes
`if not target and _ownerIsContainer then return end`. Classification: Mechanical.

## D11 — Match set is a SUPERSET of the old resolved-name fanout (phase-design round 1, codex MAJOR)
NEW match keys on compile-time literal(s) PLUS the resolved target.Name (added at runtime when a boot
target exists). NEW >= OLD => never regresses which instances match (incl. the Model "Door" -> part
"door" case); the literal adds the nil-boot late-arrival capability. For real door/plane both reduce to
the same single name. Classification: Mechanical (no-regression hardening).

## D12 — AC12 covers test_generate_rotation_tween (phase-design round 1, Claude MAJOR)
test_generate_rotation_tween:1054 asserts the removed `while true do`; AC12 now requires updating EVERY
test asserting the removed shape (grep `while true do`/`_targets`/`_ownerIsContainer`). Classification: Mechanical.

<!-- ===== /drive run rifle-mount-diag-20260617T215229 (2026-06-18) ===== -->
# Decisions — rifle-mount-diag run

## Phase 2 (design-phase2.md) — player-alias receiver routing

- **D-P2-1 (interface placement).** Player-alias branch lives in `sendMessage`/`broadcastMessage`
  BEFORE `_resolveReceiverGoId`; the resolver stays single-id and byte-for-byte unchanged. Rationale:
  player alias maps to a LIST of goIds (single-return resolver is the wrong shape) + zero-diff
  guarantee for the Phase-1 non-player path (doors/fires/turrets). Classification: workflow/domain.
- **D-P2-2 (explicit IsA guard).** `_isPlayerAlias` uses an explicit `pcall`-wrapped
  `recv:IsA("Player")` plus `playerFromTouch(recv)`, never a duck-typed/field-sniff check — else the
  live `plr` (`Players`-service object) symptom persists. Classification: universal-candidate.
- **D-P2-3 (player goIds, not instances).** `_playerGoIds` mirrors `_playerRigInstances`'s walk
  (`meta.scriptId == _player._playerScriptId`, dedupe) but collects `meta.gameObjectId` (what
  `_dispatchToGameObject` keys on), NOT `meta.gameObjectInstance`. Classification: workflow/domain.
- **D-P2-4 (broadcast = flat per-goId, no subtree descent for a player alias).** A player-alias
  `broadcastMessage` dispatches to each player goId WITHOUT the descendant walk (logic singleton, no
  corpus player-rooted broadcast, walk would only re-hit the same components). Non-player broadcast
  keeps the Phase-1 descendant walk. Classification: workflow/domain.
- **D-P2-5 (>1 character-controller MODULES fail-close to no-route).** `_player == nil` →
  `_playerGoIds` returns {} → warn + no dispatch (= current `_initPlayerAuthority` behavior; one camera
  per client). Distinct from >1 rig INSTANCES of one module, which all dispatch (P2-F). Classification:
  workflow/domain.
- **D-P2-6 (live test is the gate).** Cold `/convert-unity` SimpleFPS + Studio Play (rifle mounts on
  camera WeaponSlot AND fires) is the acceptance authority — NOT the seeded `luau` unit tests
  (green-test-can-pass-for-the-wrong-reason). Classification: universal (already canonical).

## Phase 2 — round-1 dual-voice review fold-ins

- **D-P2-7 (nil-`_player` fail-closed — refutes codex round-1 BLOCKING).** `_isPlayerAlias` may match a
  live `Player` object while `self._player` is nil. This is SAFE at the doc level: `_playerGoIds()`
  guards `if not p or not p._playerScriptId then return {} end`, so it NEVER indexes
  `_player._playerScriptId` on a nil `_player` — it returns `{}`, and the caller treats empty as
  warn + no-route. A Player-object receiver under 0/>1-character-controller (nil `_player`) fail-closes
  to a no-op, never a runtime error. The BLOCKING is refuted in design (Interfaces section + edge case 4).
  Classification: workflow/domain.
- **D-P2-8 (`playerFromTouch` breadth — codex MAJOR #3).** Branch (b) `playerFromTouch(recv)` runs for
  non-player receivers too; it resolves ONLY a character/limb belonging to a player's character model
  and returns nil otherwise, so doors/fires/turrets (never parented under a player character) take the
  unchanged Phase-1 path. Boundary/documented limit: a non-player object deliberately parented inside a
  player character WOULD be treated as a player alias — not exercised by any corpus site. New edge case
  8. Classification: workflow/domain.
- **D-P2-9 (multi-goId fan-out / "multiple rifles" — codex MAJOR #4).** `>1` distinct goId →
  dispatch to each (intended Unity multi-GameObject semantics); single-embodiment (one Player module on
  one GameObject) → exactly one goId → exactly one dispatch (no duplicate rifle). New edge case 9 + an
  AC-1 assertion that dispatch count == `#_playerGoIds()` (== 1 in the single-embodiment fixture), so a
  double-registered player rig is caught. Classification: workflow/domain.

## D-FIN2 (finalize scope after v4 redesign) — Classification: workflow/domain
The v3 finalize CONVERGED at a3451a4 (de-slopped phase 1). The v4 redesign added phase 2 on top
(now drive tip 364699d). Re-running finalize's de-slop/logic lenses over the WHOLE c30c259..364699d
diff would re-audit already-finalized clean phase-1 code (review-churn). Scope this finalize's
code lenses to the NEW work since the converged v3 finalize: a3451a4..364699d (the phase-2 delta);
whole-run read context retained for aggregate awareness. The terminal review-finalize-2.md still
binds the full featureBranch tip 364699d (ship-gate R==tip holds).

## ---- /drive run mesh-fidelity-20260619T232452 (promoted 2026-06-19T16:22:45Z) ----

# Decisions — mesh-fidelity-20260619T232452

## PLAN-stage design choices

- **`MESH_ROBLOX_MAX_FACES = 20_000`** — documented Roblox per-mesh triangle limit since ~2021. Classification: Mechanical (matches platform spec the task cites).
- **Clamp semantics `min(max(target, floor), cap)`** — quality floor may raise the target; the hard cap always wins so output never exceeds what Roblox accepts. `MESH_TARGET_FACES = 8_000` left unchanged (only the cap was stale). Classification: Mechanical.
- **Quarantine at the single invariant site (`pipeline.py:2820–2851`)** — on an embedded-mesh key resolving to ≠1 sub-mesh, drop it from freshly-parsed `mesh_hierarchies` + `mesh_native_sizes` before merge, and append to `ctx.asset_upload_errors`. Keep the existing `log.warning` as the diagnostic. Classification: Taste (chose single-source-of-truth quarantine over a separate pass).
- **No `scene_converter.py` change** — its face-decal/no-mesh fallback already fires on key-absence from the hierarchy tables (read-confirmed in `_resolve_mesh_geometry`/`_resolve_sub_mesh`), so upstream quarantine is sufficient. Classification: Taste (avoid redundant downstream gate).

## Single phase
- One phase, one slice. Both fixes are independent, disjoint-file, ≲25 logic SLOC total → no fan-out and no staged-risk seam. Classification: Mechanical (default-to-one-phase).


## Design review r1 (2026-06-19) — BLOCKING: bug-2 binding gate is uploaded_assets, not hierarchy tables
Classification: Mechanical (root-caused against real code by the Claude voice; codex text-only missed it).
Revise design: bug-2 quarantine must also drop the key from `uploaded_assets` (the table _resolve_mesh_id
reads), detailed design to verify the mesh-less node reaches the face-decal/no-geometry path. Fold MINORs:
(1) note bug-1 decimation path is currently unwired (latent fix + test pin; don't claim real-output change);
(2) also pop the quarantined key from the merged/ctx dicts, not just the pre-merge fresh dicts.

## Revised decisions (post-r1, against real code under converter/converter/)

- **Bug-2 quarantine mechanism (REVISED)** — at the invariant site (`pipeline.py:2820–2851`), drop the
  offending embedded key from THREE table sets and append to `ctx.asset_upload_errors`:
  (1) `mesh_hierarchies` + `mesh_native_sizes`, (2) `ctx.uploaded_assets` (the MeshId binding gate read by
  `_resolve_mesh_id` at `scene_converter.py:2892–2896`), (3) the MERGED/`ctx` dicts, not just the pre-merge
  fresh dicts (merge is `{**existing, **fresh}` → a prior-rerun `ctx` key survives a pre-merge-only drop).
  Same `f"{rel}#{file_id}"` key shape in both tables (`pipeline.py:2014`), recognised by `is_embedded_mesh_key`.
  Classification: Mechanical (binding-gate root-caused against real code; the round-1 "hierarchy-tables-only,
  no scene_converter change" claim was FALSE — size-only, MeshId still bound to leaked geometry).

- **Verified end-state — NO `scene_converter.py` change needed** — with the key absent from `uploaded_assets`
  AND `mesh_hierarchies`, `_resolve_mesh_id` returns `None` via membership-guarded `in` checks (no KeyError):
  `_resolve_sub_mesh`→None, embedded-candidate lookup misses, `.prefab` path is not an `uploaded_assets` key.
  `part.mesh_id` is never set (`scene_converter.py:1889`); sizing falls to `_compute_mesh_size_from_fbx_bbox`
  (reads FBX, not the tables) / unity-scale fallback → MeshPart with no MeshId = existing colored-part /
  face-decal fallback, crash-free. Classification: Taste (skip a redundant downstream guard the trace proves
  unnecessary). If the trace had shown a KeyError, the fallback was a `scene_converter` guard on
  `asset_upload_errors`; it is not needed.

- **Bug-1 is a LATENT fix (MINOR folded)** — `decimate_mesh`/`needs_decimation` have NO production callers
  (grep-confirmed; test-only references). Meshes upload raw, Open Cloud decimates server-side. Cap bump
  (20_000) + clamp are correct and test-pinned, but the design/PR must NOT claim they change real conversion
  output today. Classification: Mechanical (latent correctness + spec-accuracy).

## Stage-1 autoplan skipped (proportionality)
Classification: Taste. Ran the dual-voice design review (the load-bearing P1 gate — it caught + fixed a
real BLOCKING). Skipped the full gstack autoplan (CEO/Design/Eng/DX) as disproportionate for a ~30-SLOC
internal mesh-pipeline correctness fix with no product/UX surface. Per OPERATING.md right-size-at-design.

## Phase-1 DETAILED-DESIGN choices (2026-06-19, against real code @ eb8f452)
- **File layout (verified):** `config.py` is at `converter/config.py` (NOT `converter/converter/`); constants at lines 173–175. Clamp line is `mesh_processor.py:168`. Quarantine site `pipeline.py`, ctx-merge at 2847–2851 (log.warning loop 2830–2841). `_resolve_mesh_id` 2882–2933, embedded lookup 2917–2921. Classification: Mechanical.
- **Bug-2 extracted as a pure module-level helper `_quarantine_bad_embedded_meshes(mesh_hierarchies, mesh_native_sizes, uploaded_assets, upload_errors)`** — mutates in place, returns quarantined keys, called once right after the ctx merge (post-merge). Single source of truth at the invariant site (honors locked "no second pass"); makes the quarantine unit-testable without the Open Cloud resolve round-trip. log.warning diagnostic kept. Classification: Taste.
- **Quarantine operates on `self.ctx.*` (merged dicts), pops all three slash directions of the embedded key from `uploaded_assets`** (mirrors `_embedded_key_candidates`), discovers offending keys via `mesh_hierarchies.items()` + `is_embedded_mesh_key` with `len(subs) != 1` (covers 0 and ≥2). Satisfies the post-merge + slash-variant pins. Classification: Mechanical.
- **Bug-1 existing tests survive the bump** — `test_mesh_processor_decimation.py` passes `max_faces=10_000` as an explicit arg, not the default constant; no test asserts the default value. Update the stale module docstring ("currently 21,844 faces" → 20_000). Classification: Taste.
- **Single slice confirmed at detailed design** — disjoint files, no fan-out / staged-risk, ~25–30 SLOC; the bug-2 helper + its single call site stay in one review unit (shared contract). Classification: Mechanical.

## Phase-1 integration review — proportionate (single-slice)
Classification: Taste. phaseInt is a clean FF of the already-dual-voice-reviewed slice (identical content).
Recorded CONVERGED on the integration suite (4014 passed, no-Any) + the slice dual-voice pass rather than
burning a duplicate full review round on byte-identical code. Per OPERATING.md right-size-at-design.

## Finalize r1 — bug-1 was deeper than framed: decimation was entirely non-functional (positional arg)
Classification: Mechanical (empirically root-caused). decimate_mesh passed the face count positionally to
trimesh's simplify_quadric_decimation(percent=…, face_count=…) → ValueError → except → un-decimated original.
So bug-1 is NOT merely a stale-constant/clamp latent fix: whenever a decimation backend IS present the path
was broken (no decimation, oversized meshes shipped). Fixed with face_count= keyword; output now strictly
bounded by the (clamped) cap. decimate_mesh still has no production caller today (impact latent until wired),
but the fix makes the path actually correct. Surface this corrected framing at Gate B.

## Verify stage skipped (no UI/URL; real-upload check low-yield)
Classification: Taste. The change is converter-internal (no UI/URL surface to qa-only/browse). The Gate-A
deferred 20k real-upload acceptance check is now low-yield (decimate_mesh has no production caller + no
decimation backend in-env, so a live conversion wouldn't exercise the clamp). Documented as a followup;
verification rests on the unit suite (4016 passed) + the strict <=cap real-path tests (active with a backend).


# Decisions — trash-dash-phase2 run

## Phase 3 — ThemeDatabase SO-seed population (gap #5a)

- **D-P3-1.** Fix the dict-store SO-DB by **rewriting the transpiled `LoadDatabase`**
  (consumer-lowering, Option A), NOT by extending the list-store seed+shim
  (`_build_theme_seed_plan` / `seedAddressableDatabases`). The store the consumer reads
  (`GetThemeData` → `themeDataList[type]`) is a **closure-private LOCAL upvalue** in the
  transpiled output, unreachable by any external shim — so the DB module itself must own
  its own keyed write. Endorsed by codex (xhigh) over options B (shim-into-public-setter)
  and C (generalize drain-bind to keyed-local stores). Direct precedent:
  `converter/converter/roster_consumer_lowering.py` already does exactly this region-rewrite
  for the prefab `<GameObject>` roster analogue. **Classification: converter-architecture.**

- **D-P3-2.** Key the lowering on the deterministic `LoadAssetsAsync<SOType>("<label>")` C#
  ownership + the SO-seed surface (`resolve_scriptable_object_addressables`), never the
  AI-output fingerprint or a per-game string. Fail-closed (`SoDbUnresolved` →
  `FailClosed(kind="so_db_unresolved")`) when the rewrite TARGET cannot be located in the
  AI output. Named blind spot: the trigger is upstream-deterministic, but the rewrite
  target lives in non-deterministic AI output, so target-location failure fails loud, not
  guesses. **Classification: converter-generality.**

- **D-P3-3.** Leave the list-store seed/shim path (`_build_theme_seed_plan`,
  `seedAddressableDatabases`) intact for genuine list-store DBs; the new lowering
  supersedes it ONLY for the keyed-dictionary SO-store shape. The gate-(b) regex fix
  (`_CS_METHOD_HEADER` control-keyword exclusion) benefits BOTH paths. **Classification:
  converter-architecture.**

- **D-P3-4 (phasedesign3 BLOCKING 1).** Place the SO-DB consumer-lowering in
  `pipeline.py`'s `write_output`, as a NEW method `_lower_so_db_consumers` SIBLING of
  `_build_theme_seed_plan` (call site right after it, pipeline.py:7236-7242), NOT in
  `contract_pipeline.transpile_with_contract` next to the roster call. Reason: the SO surface
  needs `so_map` (emitted-SO GUIDs from `_build_scriptable_object_module_map`), which does not
  exist until `write_output`; `transpile_with_contract` runs earlier (from `transpile_scripts`)
  and reads ONLY the plan-time-persisted prefab `by_label`. `_build_theme_seed_plan` is the one
  place `so_map`, `so_addr` (`resolve_scriptable_object_addressables`), the placed luau source
  (`source_by_name` ← `rbx_place.scripts[*].source`), and the per-module C#
  (`_find_cs_source_for_module`) all coexist, and it runs before the plan/entrypoints emit, so
  mutating `RbxScript.source` there reaches the emitted output. **`contract_pipeline.py` is no
  longer touched by this phase.** Verified against the detached Phase-1&2 worktree
  `…/wt/design3` (`transpile_with_contract` reads `by_label` only; `_build_theme_seed_plan` at
  pipeline.py:7372 has all four inputs in scope). Classification: **converter-architecture.**

- **D-P3-5 (phasedesign3 BLOCKING 2).** ENFORCE roster/SO-DB disjointness (a shared label can
  appear in BOTH the prefab `by_label` and the SO `by_label` — prefab narrowing drops SO *guids*,
  not the label *key*), with TWO deterministic guards in the NEW module + the new orchestration
  (NO edit to `roster_consumer_lowering.py`): (b) `find_so_db_consumers` BINDS `<T>` from
  `LoadAssetsAsync<T>(...)` and produces a fact ONLY when the label's SO-surface guids resolve
  to ≥1 emitted SO module — a `<GameObject>` load abstains; and (a) the SO finder abstains on any
  `source_path` the roster lowering already claimed this run (`roster_claimed_paths`), preventing
  a double-rewrite even in the pathological both-resolve case. The pre-existing roster-finder
  `<T>` gap is GUARDED-AROUND here, not fixed (fixing it edits roster code + re-reviews the live
  CharacterDatabase path — deferred to the filed followup). Keyed on the deterministic
  `<T>`/SO-map/claimed-set facts, never a per-game string. Classification: **converter-generality.**

## Slice 3.1 implementation notes (clarification, no design deviation)
- **D-P3-4 (clarify §3f layers).** Confirmed empirically: layer (b) (`<T>`/SO-surface
  gate) only distinguishes the roster vs SO DB when their labels resolve to DISJOINT
  guids (the GameObject label's guids are prefab guids absent from the SO map). For a
  TRULY-shared label (one label genuinely tagging an SO that both DBs load), both loads
  resolve to the same SO guids and layer (b) claims BOTH — exactly the pathological case
  the design names; layer (a) `roster_claimed_paths` is the enforcing guard there. This
  matches design §3f intent ("could in principle satisfy both finders … layer (a) prevents
  the roster finder's pre-existing gap from causing a double-rewrite"). Tests encode both.
- **Lowering details:** the path resolver is INLINED into the rewritten `LoadDatabase`
  (no module-level `local function` helper) so the region locator's state-decl walk-back
  stays byte-stable across re-runs (idempotence). The keyed accessor (`GetThemeData`) is
  LOCATED as the public method that is neither getter nor the load method; getters are
  located by the state-local they return (nil-init -> dict getter, bool-init -> loaded).
- **roster_claimed_paths re-derivation:** read-only via `find_roster_consumers` over the
  off-disk prefab-narrowed `by_label` in the write_output window (touches no roster code).

## Phase 3 HARDEN pass (drive-harden, 2026-06-18)
- **P2-1 (lens 1) — ACTED: added pipeline-orchestration coverage.** The diff unit-tested
  only the pure `so_db_consumer_lowering` functions + the gate-b regex; the production
  `Pipeline._lower_so_db_consumers` orchestration (real input derivation: parse_addressables
  → resolve_scriptable_object_addressables; per-module `.cs` re-read + `_derive_cs_load_ownership`
  → `load_method_by_path`; the read-only `find_roster_consumers` re-derivation →
  `roster_claimed_paths`; the fail-closed re-raise) was UNEXERCISED. Added
  `TestLowerSoDbConsumersOrchestration` (5 tests) to `tests/test_theme_seed_plan.py`,
  reusing the existing real `_make_project`/`_pipeline_with_state` harness. Drives the real
  method end-to-end on the keyed-dict Luau shape. Verified the orchestration test FAILS if the
  gate-b regex regresses (`load_method='if'` → `SoDbUnresolved`), so it is a genuine regression
  guard, not green-for-wrong-reason. Classification: **converter-architecture (test coverage)**.
  - Discovered while spiking: the orchestration keys `csharp_by_path` by `RbxScript.source_path`,
    which production stamps at pipeline.py:3334 (`source_path=ts.output_filename`). The existing
    `_pipeline_with_state` harness did NOT set it, so a naive reuse silently no-ops. The new
    `_lower_pipeline` helper sets `source_path` to mirror production; a `test_orchestration_noop_without_source_path`
    documents+guards that keying contract so the positive test can't pass for the wrong reason.
- **P2-2 (lens 2) — CLASSIFIED filed-followup, NOT an in-scope bug.** `roster_claimed_paths`
  cannot under-approximate relative to what the SO finder can act on: BOTH `find_roster_consumers`
  and `find_so_db_consumers` iterate the SAME `csharp_by_path` (keyed by `source_path`), so a
  module whose C# is unresolvable in the write_output window is invisible to BOTH finders — the
  SO finder cannot rewrite a module the roster re-derivation didn't see. The only residual is the
  pre-existing roster `<T>` gap (already filed; the §3f layer-(b)-is-tautological residual already
  filed in followups.md round 2). No fix in this phase: a real fix touches `roster_consumer_lowering.py`
  (scope expansion). Classification: **filed-followup, no current-input impact**.

## Harden phase 3 — adjudication
- **D-P3-6.** Overruled codex's harden lens-2 P1 (roster_claimed_paths under-approximation) as a
  false-positive at the real integrated path: pipeline.py:7621-7654 passes ONE csharp_by_path to both
  find_roster_consumers and find_so_db_consumers, so a module invisible to the roster re-derivation
  (C# unresolvable → skipped from the dict) is equally invisible to the SO finder → cannot be
  mis-routed/double-rewritten. Confirmed lens-1 P1 (orchestration test gap) and fixed it (5 tests).
  Classification: **review-adjudication** (adversarial BLOCKING reproduced against the integrated
  path, refuted, overruled WITH evidence — not silently dropped).

## Phase 4 — gameplay spawn closure + residual bindings (detailed design, 2026-06-19)

- **D-P4-1.** Gap #5 is THREE layers (re-diagnosed): L2 (theme prefab-id data) already works
  post-Phase-3; L1 (5 `nil`+abort InstantiateAsync call sites in TrackManager.luau) and a
  NEWLY-FOUND L0 (segment/obstacle prefabs never emitted as Templates — `addr_ids` only sources
  AddressableAssetsData groups, not SO `.asset` AssetReference arrays; `prefab_packages.py:402`)
  are both dead. L0 folded into this phase (slice 4.1) — L1 is unverifiable without it.
  **Classification: converter-architecture.**
- **D-P4-2.** L0 fix = a NEW SO-AssetReference prefab resolver in `addressables_resolver.py`
  (symmetric with `resolve_scriptable_object_addressables`, but prefab-typed), unioned into
  `addr_ids` at `pipeline._build_addressables_block`/`plan_scene_runtime` upstream of both
  `select_emitted_prefab_ids` and `_generate_prefab_packages`, and fed to
  `resolve_template_child_names`. Keyed on serialized `m_AssetGUID` + guid_index `.prefab`
  classification — never a label/AI fingerprint. **Classification: converter-generality.**
- **D-P4-3.** L1 fix = a coherence-pack-style call-site lowering (new
  `spawn_call_site_lowering.py`, orchestrated in `write_output` like `_lower_so_db_consumers`):
  identity-gated detection of `local v=nil`+`if v==nil then warn("Unable to load…") return end`
  with an InstantiateAsync/LoadAssetAsync origin comment → rewrite to
  `host.instantiatePrefab(<resolved prefab-id>, …)`. Fail-CLOSED + loud on shape/ref-resolution
  miss (rewrite target in non-deterministic AI output — D-P3-2 precedent). Idempotent (twice-call
  test). MUST key on the collision-resolved EMITTED template name, not the dangling registry
  `template_name`. **Classification: converter-architecture.**
- **D-P4-4.** Gaps #3 and #4 are the SAME defect class (planner emits a component/deferred row;
  placement prunes the inactive host) in DISJOINT files → fan-out slices. #3 fix = producer-side
  at `scene_converter._emit_dormant_holder` / planner (defer/resolve dormant-holder MonoBehaviour
  descendants; prefer DEFER over eager placement to avoid duplicating the runtime rig). #4 fix =
  producer-side at `ui_translator.py:394` (emit inactive UI subtree HIDDEN + keep recursing, so
  the `_SceneRuntimeId` host clone lands; planner-side suppress is WRONG — kills SetActive-driven
  popups). **Classification: converter-architecture.**
- **D-P4-5.** Slices: 4.1 (L0+L1 spawn closure, foundation, no dep — keep L0+L1 together for the
  shared prefab-id/Template-name contract); 4.2 (gap #3, fan-out, owns scene_converter/planner);
  4.3 (gap #4, fan-out, owns ui_translator.py). 4.2 ∥ 4.3 disjoint from 4.1 and each other.
  **Classification: decomposition (fan-out per design.md split commitment).**

## Phase 4 — design review revisions (dual-voice FINDINGS, 2026-06-19)

- **D-P4-6 (supersedes D-P4-3's detector premise — P1-1/P2-2).** The L1 detector is RE-ANCHORED
  on the deterministic transpiler ORIGIN COMMENT (`Instantiate*`/`LoadAssetAsync<GameObject>`),
  NOT a single `local v=nil`+warn-abort guard shape. Empirically against the REAL TrackManager.luau
  the guard shape matches ONLY the segment site (484); the other rewritable sites diverge —
  obstacle inverted `if obj~=nil` (547), consumable/premium bare `toUse=nil` reassign (599/612),
  cloud `:Clone()` on a string (307). There are SIX origin sites (not five): the 6th is the
  character site (168), which is already lowered to a scene-find degrade and EXCLUDED (no clonable
  prefab). Five sites enter the rewrite set (segment/obstacle/consumable/premium/cloud).
  **Classification: converter-generality (enumerate the real input space before locking the
  abstraction).**
- **D-P4-7 (P1-2).** The cloud site (`local obj = cloud:Clone()`, TrackManager.luau:307) is
  BROKEN, not "works": C# `ThemeData.cloudPrefabs` is `GameObject[]` but the converter serializes
  it as prefab-id STRINGS (themeData__86f154.luau), so `:Clone()` on a string errors at runtime.
  It is folded INTO the L1 rewrite set (instantiate by prefab-id via `host.instantiatePrefab`),
  not left as `:Clone()`. **Classification: converter-architecture.**
- **D-P4-8 (corrects D-P4-2's write point — P1-3).** L0 must union the SO-prefab ids into the
  PERSISTED `artifact["addressables"]` block (a new `so_prefab_ids` axis) BEFORE the freeze
  `self.ctx.scene_runtime = dict(artifact)` (pipeline.py:1458), AND extend BOTH emit-gate consumer
  loops (`pipeline.py:6500` and `:6752`) to read that axis. The emit gate re-walks the PERSISTED
  block, NOT the local `addr_ids` (pipeline.py:1418) — unioning only into the local makes L0
  half-work (names resolve via the :1438 name-pass, but Templates never emit). The local also gets
  the union (for the name pass). **L1 target API: `self.host.instantiatePrefab(prefab_id, parent,
  cframe)` (scene_runtime.luau:2846, dotted accessor :1067) — it resolves a `"<guid>:<path>"`
  prefab-id string; NOT `PrefabSpawner.spawn(name, …)` (prefab_packages.py:79), which keys on the
  bare Template NAME.** **Classification: converter-architecture.**
- **D-P4-9 (P2-1).** The L0 SO walk is GATED on the emitted-SO `so_guids` set (mirror
  `resolve_scriptable_object_addressables`, addressables_resolver.py:179-218 — positive evidence
  an SO module was emitted), NOT a walk of every SO in the project. **Classification:
  converter-generality.**
- **D-P4-10 (slice structure re-justified, unchanged).** All five findings (P1-1/P1-2/P1-3/
  P2-1/P2-2) land WITHIN slice 4.1; none introduces a new cross-slice contract. Structure stays
  **4.1 (foundation) + 4.2 ∥ 4.3 (fan-out)** — 4.2/4.3 owns (scene_converter/planner;
  ui_translator.py) remain disjoint from 4.1 and each other. **Classification: decomposition.**

## Slice 4.1 implementation — consumable site DEFERRED (dual-voice deviation, 2026-06-19)

- **D-P4-11 (deviation from AC2/AC6's literal 5-site set; consumable DEFERS).** The L1 lowering
  rewrites FOUR of the five sites (segment/obstacle/premium/cloud); the **consumable site is
  DETECTED but NOT rewritten** (fail-closed: skip + loud converter warning + filed followup).
  Evidence (real materialized output): segment (`zone.prefabList[...]`), obstacle (`reference`
  param), premium (`self.currentTheme.premiumCollectible` — materialized as a prefab-id STRING in
  `themeData__*.luau:49`), and cloud (`cloud` from `cloudPrefabs`) all carry a PROVABLE prefab-id
  STRING at the site. The **consumable** source `self.consumableDatabase.consumbales[picked]` is
  accessed by the surrounding transpiled code as a STRUCT (`.canBeSpawned`, `.gameObject.Name`),
  but the `Consumables` SO materialized `consumbales` as a bare prefab-id STRING list
  (`Consumables.luau:7-11`) — the C# `ConsumableData {gameObject, canBeSpawned}` struct was
  FLATTENED. So no prefab-id-string expression is provably available at that site, and the
  surrounding code is already semantically broken (a separate SO-materialization mismatch, out of
  this slice's scope). Force-rewriting would convert a loud, diagnosable mismatch into a SILENT
  host-call mis-resolution. **Both review voices converged: codex (adversarial) — "DEFER: if the
  recovered source is not provably a prefab-id string, rewriting mis-resolves; fail-closed is
  correct"; design-Claude — "detect-but-skip with a loud warning + filed followup for the SO
  flattening is the faithful reading of fail-closed-over-guess." The faithful reading of AC2/AC6 is
  that the rewrite applies where the prefab-id-string contract holds.** Filed the consumable
  SO-struct-flattening as the root-cause followup. **Classification: converter-generality
  (fail-closed over guess on AI output; enumerate the real input space).**

## Slice 4.2 — Gap #3 dormant-holder descendant trigger-nil (implement)
- **Fix site: planner `_walk_scene` (producer), not `_emit_dormant_holder`.** `SceneNode.active = bool(m_IsActive)` is SELF-active. The bug host (CharacterSlot) is self-active under an inactive parent (PlayerPivot). scene_converter never places descendants of an inactive node in ANY mode (legacy prunes the subtree; generic emits a childless dormant holder — `_emit_dormant_holder` does not recurse). So the deterministic build fact is "host has an INACTIVE ANCESTOR" → suppress its instance row + references + lifecycle entry. Threaded a `ancestor_inactive` bool down `_visit`; propagate `ancestor_inactive or not node.active` to children.
- **Self-inactive does NOT suppress.** An inactive-but-referenced node gets its own dormant holder, so its OWN MonoBehaviour rows resolve to it (preserves existing `test_disabled_monobehaviour_recorded_with_enabled_false`). Only DESCENDANTS of an inactive node are never-placed.
- **`runtime_bearing.add` kept before the suppression `continue`** (per design "keep runtime_bearing so the module still emits"). `_build_modules_table` keys on set membership, not instance existence — verified no consumer requires an instance row per runtime_bearing script (codex Q2 confirmed). The class may also run on a runtime-instantiated rig.
- **Prefab walk (`_walk_prefab`) left untouched.** `_convert_prefab_node` does NOT prune on `node.active` — inactive prefab descendants ARE emitted as parts. The dormant-holder non-recursion is scene-converter-specific, so the bug is scene-side only. Applying suppression to prefabs would over-suppress placed prefab content.
- **Codex (gpt-5.4, xhigh) review of the approach: confirmed all 4 questions** (correct boundary; runtime_bearing-kept safe; apply all modes; no over-suppression of placed triggers). Log: codex-raw-4.2.log.

## Slice 4.3 — Gap #4 inactive UI subtree emission (impl)
- Implemented exactly the design §B Gap #4 spec: removed `ui_translator.py:394`
  `if not node.active: return None`. The element is now created with the existing
  `visible=node.active` (ui_translator.py:464) → an inactive node lands hidden
  (`Visible=false`), its `_SceneRuntimeId` is stamped (:470), and child recursion
  (:535) proceeds, so the 3 named deferred-component host clones (ConfirmPopup
  1918594629, VisitUnityButton 1834564028, VisitGameChangerButton 375939466) land.
  No new code path needed — `visible` was already wired to active; pruning was the
  only blocker. Runtime `_applyPlannerFlagsAndTag` keeps the woken subtree inactive
  until a script `SetActive(true)`s it (no visible-at-boot regression).
- Tests: added `TestInactiveUiSubtreeEmission` (AC4 host-clone landing, AC8
  emitted-not-pruned, hidden-not-visible E6, active-subtree-unchanged, nested
  inactive-within-inactive, bug-guard). 4/5 new tests fail against the restored
  pre-fix prune (verified); active-subtree-unchanged correctly passes pre-fix.
  Full fast suite 4046 passed / 47 skipped; no-Any gate pass.

## Slice 4.1 review fixes (2026-06-19) — 2 P1 + P2s

- **P1-1 (cloud no-gate wrong-region rewrite):** `_locate_cloud` now gates the
  `:Clone()` rewrite on a `-- …Instantiate(…` origin comment in the contiguous
  comment block IMMEDIATELY above the `local v=expr:Clone()` shape
  (`_instantiate_comment_above`: blank/comment lines may intervene; a code line ends
  the block, so an unrelated earlier comment cannot leak down). REPRODUCED the
  corruption pre-fix: real Pooler.luau (`local obj = original:Clone(); obj.Parent =
  Workspace`, no comment) was rewritten; post-fix it is byte-identical. The other 4
  real `:Clone()` modules (AllLaneObstacle/PatrollingObstacle/GameState/LoadoutState)
  have no `.Parent=` on the next line so never matched, but Pooler did.
- **P1-2 (game-specific keys):** obstacle re-keyed off `SpawnFromAssetReference` →
  recovers the prefab-id STRUCTURALLY from the transpiler's `local _ = <param>`
  discard (else the enclosing function's first param, guarded against binding `self`
  for dot-form methods). Premium/consumable merged onto the shared
  `Addressables.InstantiateAsync` marker + `<v>=nil`+warn-abort shape; split by
  POSITIVE prefab-id evidence (`_extract_prefab_id_expr`: rewrite iff the warn
  `tostring(<EXPR>.[Nn]ame)` exposes an <EXPR> NOT routed through `.gameObject`;
  consumable's struct-flattened `…gameObject.Name` defers). Scans ALL InstantiateAsync
  matches (consumable precedes premium in source order) — a `.search`-only approach
  would have wrongly abstained premium.
- **P2 uniform fail-soft:** segment/obstacle/instantiate-async/cloud all LOG on
  origin-present-but-shape/expr-absent drift (cloud guarded so the idempotent re-run,
  which has the comment but no live `:Clone()`, does not trip it).
- **P2 green-for-wrong-reason:** extracted `prefab_packages.collect_addressable_prefab_ids`
  (the SINGLE persisted-axis reader both pipeline emit-gate arms now call); the
  persisted-axis test drives that REAL helper (proven RED when the helper drops the
  so_prefab_ids read). Real-TrackManager coverage moved to vendored real-shape
  fixtures (hermetic) + a `test_real_diag_outputs_when_present` cross-check that
  skips loudly only when the diag tree is absent.
- **P2 resolver typing:** `resolve_so_assetref_prefab_ids` param re-typed
  `GuidIndexLike` (drop the `object`+`cast`); two pre-existing siblings keep their
  `object`+cast (out of slice scope).
- Codex re-review (corrected artifact): obstacle `self`-misbind → hardened
  (fail-closed). Cloud "pooler inherits an Instantiate comment block" + premium "warn
  via temp/helper" findings are FALSE-POSITIVES on the real path (real Pooler has no
  Instantiate comment; real premium is literally `tostring(…name)`) — overruled with
  evidence, filed as output-fingerprint blind-spot residual (no speculative code).

## D-P4-6 (slice 4.2, phase-integration P1 fix)
- **Decision:** Narrowed 4.2 gap #3 ancestor-inactive row suppression with a UI carve-out: `if ancestor_inactive and node.file_id not in ui_go_fids: continue` (scene_runtime_planner.py `_walk_scene._visit`). UI hosts (under a Canvas) under an inactive ancestor KEEP their planner instance row so the 4.3 ui_translator deferred host clone binds via `awaitUiHost` (driven by the `instance_owner_is_ui` row). Gap #3 scene/world dormant-holder descendants stay suppressed.
- **Why:** un-gated suppression deleted the gap #4 deferred-UI-host rows (ConfirmPopup/OpenURL/missionPopup under inactive AboutPopup), re-introducing the planner-side UI suppression D-P4-4 declared WRONG and negating 4.3. `ui_go_fids = _scene_ui_go_fids(scene)` was already in `_visit` scope.
- **Test:** `test_inactive_ancestor_ui_host_under_canvas_keeps_row` (Canvas→inactive AboutPopup→active OpenURL button) — proven RED against 922b27f (row==[]), green with fix. Gap #3 tests stay green.

## Phase 4 HARDEN (drive-harden, 2026-06-19)

- **BUG FIXED (P1) — spawn lowering premium/consumable warn paren over-close.**
  `spawn_call_site_lowering._instantiate_async_outcomes` re-emitted the original
  `warn(...)` by splicing the captured `warn` regex group (which ALREADY closes
  `tostring(`+`string.format(`+`warn(` with three trailing `)`), then the replacement
  template added a FOURTH `)` via `warn(string.format({warn})`. On the real
  TrackManager.luau the premium rewrite produced `...premiumCollectible.name))))`
  (1 open / 4 close on the warn-args line; module 264→267 opens but 268 closes) — a
  Luau syntax error that fails the WHOLE TrackManager module to compile, killing all
  spawning (live AC i-b). Fix: drop the trailing `)` from the replacement template
  (the captured group already closes everything). Verified: real TrackManager now
  267/267 balanced, 4 rewrites + 1 defer intact. Bug-guard `test_premium_rewrite_warn
  _parens_balanced` added — FAILS pre-fix (`name))))` vs `name)))`), passes post-fix.
  This was a green-test-for-wrong-reason: `test_premium_rewrite_recovers_prefab_id_expr`
  only substring-checked "Unable to load collectable", never paren balance.
- **AUDIT — clean elsewhere.** L0 SO-AssetReference resolver + persisted `so_prefab_ids`
  axis + shared `collect_addressable_prefab_ids` reader: correct, well-tested (both
  consumer arms use the shared helper). Gaps #3/#4 (planner ancestor-inactive
  suppression + UI carve-out; ui_translator emit-hidden): correct, thorough tests incl.
  the cross-slice UI-host carve-out asserting `instance_owner_is_ui`. Segment index
  off-by-one (`+1`) correct; cloud passes the already-1-based value through.
