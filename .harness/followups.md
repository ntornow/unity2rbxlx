
## Run main-20260604-201520 (2026-06-04)

# Follow-ups — drive run main-20260604-201520

- **[recut docs stale] Update `scene-runtime-pr5-8-recut-plan.md` + `scene-runtime-and-refactor-execution.md`:**
  Slice H's premise ("HudControl `domain="excluded"` → dead HUD; fix the `module_domain.py` rule;
  output LocalScript") is STALE. Empirically (current main, fresh generic SimpleFPS), HudControl
  already classifies `domain="client"`, `script_class="ModuleScript"`, `container="ReplicatedStorage"`,
  `runtime_bearing`, `requireable`, no fail-close. The require-fallback strip + `instance_owner_is_ui`
  strong-client signal already landed. Mark Slice H done; the "LocalScript" requirement is wrong
  (host require()s it as a ModuleScript).

- **[contract_verifier shadow → fail-closed]** The task assumed Slice H "must pass the fail-closed
  contract_verifier," but the verifier runs in SHADOW mode on current main (1 info-level
  consumer_compliance violation: dead water module `Displace`). Flipping it to fail-closed is its
  own decision/PR (likely part of PR6/PR8 territory). Track separately.

- **[possible HUD runtime-binding]** IF the Studio canary shows the HUD is dead at runtime despite
  correct client classification, the real root cause is runtime boot/binding (SceneRuntimeClient
  not constructing the requireable client module, or `self.gameObject` not binding to the HUD
  ScreenGui) — a distinct, smaller fix than the doc's classifier change. Capture with its real
  root cause; do NOT fix blind in this run.

- **[fixture hygiene]** `tests/fixtures/topology/simplefps_minimal.json` encodes HudControl=client
  (now the correct production behavior). Ensure Slice-adjacent tests exercise the real classifier,
  not just assert against this fixture (green-test-for-the-wrong-reason guard).

- **[Pause panel]** `HUD.Pause` not converted (`_pauseMenu()` nil) — UI-conversion completeness,
  pre-existing, out of scope.

## Run hudbind-20260604-223428 (2026-06-05)

# Followups (out of scope for the run)

## Slice 1.1 — UI-host deferred resolution
- **Respawn re-clone rebind (ResetOnSpawn=true GUIs).** Slice 1.1 binds
  `instance.gameObject` once at initial boot. The confirmed HUD is
  ResetOnSpawn=false so its clone persists across respawns and no rebind is
  needed. A ResetOnSpawn=true GUI gets a NEW PlayerGui clone on respawn that
  no component rebinds to today (true regardless of this slice). Rebinding
  deferred UI components on respawn is a separate concern — out of scope here.
  (Marked with a code comment in `scene_runtime.luau:_resolveDeferredUiInstances`.)
- **Inbound references to a deferred UI component.** RESOLVED in fix-round 1
  (codex BLOCKING #2). The synchronous `_wireReferences` pass now records
  inbound refs whose target is a deferred instance (`_inboundRefsToDeferred`);
  `_completeDeferredBatch` back-patches the stored field on each source once the
  deferred target is built (including prefab-side `externalRefs`). Inherent
  residual: a source that already CACHED the value in its own Awake won't
  re-read it — the field is populated, but a consumer that snapshotted nil in
  Awake keeps nil. Not fixable without a re-Awake of the source (out of scope).
- **awaitUiHost connect-vs-scan gap (MAJOR #5) is only structurally tested.**
  The connect-first fix is exercised by a coroutine test (initial-scan hit,
  DescendantAdded-after-miss, timeout→nil), but the microscopic real-Roblox
  window where a clone lands between connect and scan cannot be reproduced
  deterministically under standalone luau. The Studio canary (acceptance 4) is
  the real-environment guard for that window.

## --- Run: generic-converter-step1-player-20260607T091314 (Phase 1 / Gate 0) ---
# Followups — generic-converter Step 1 (player-embodiment authority)

- Propagate D7+D8 into the authoritative converter/docs/design/player-embodiment-authority-design.md IN THE WORKTREE before Phase 2/3 detailed-design runs (update-design-doc-before-implementation).
- Phase 2 /drive-design: pin the EXACT after-LateUpdate scheduling slot for driveLocomotion vs the post-camera re-assert (tail of _tick / later heartbeat priority / RenderStepped), and Gate 0 should test that exact slot (codex r3 MINOR + claude r3 MINOR).
- Phase 2/3 /drive-design: state recoil-on-A-hit is knowingly degraded until A deleted in Phase 5; the A-hit Studio fixture asserts look/move, NOT recoil (claude r3 MINOR).
- codex r3 MINOR "verify_hook.py/REQUIRE_PLAYER_BIND not found" was a FALSE ALARM (codex read the pre-#184 working tree). Files exist at base ref 2cbed06. No action; noted so it isn't re-raised.
- Phase 2 /drive-design: the E2E single-read must fire at the HEAD of _tick (before the pairs() Update loop at scene_runtime.luau:2794) — tail-of-_tick won't do for the PRE read. Confirm nothing else advances E2EMouseAckSeq earlier in the frame (claude r4 MINOR).
- Phase 1 Gate-0 (d): assert C's snapshot read HAPPENS-BEFORE an A-style in-band _readDelta (ordering), not merely "consumed once" — so a future refactor moving C's read into the component loop FAILS Gate 0 rather than passing on lucky pairs() order (claude r4 MINOR).

## From Step-1b (player-embodiment, 2026-06-10)
- **door-visual cold-e2e (PRE-EXISTING, documented pr148-followups)** — `door_opens_with_key` logic passes (`open` attribute flips) but the visual tween never plays: Door is `domain=client` and sets `open` client-side, while `Anim_Door` ships as a server `Script` (the animation_routing "safe fallback" for an UNRESOLVED driver — Door uses dynamic `transform.parent.Find("door").GetComponent<Animator>()`, no serialized Animator ref). Client→server attribute writes don't replicate → no tween. Latent on `main`; orthogonal to player-embodiment. FIX = the deferred "Phase-2 source-narrowing" (resolve the driver by which MB writes the clip's `observed_attribute` → route Anim client-side) OR widen the PlayerSetSharedFlag RemoteEvent bridge to generic Animator-attribute writes. **To be its own focused PR (per jiazou).**
- **turret damage (PRE-EXISTING, PR #145)** — turrets fire but TurretBullet damage no-ops because runtime-spawned prefab clones lack `_SceneRuntimeId`. Out of any Step-1b scope.
- **recoil cross-surface sign** — `host.player:applyRecoil(deg)` kicks UP for +deg (Studio-verified); the drone/turret `SceneCameraInput:applyRecoil(-math.rad(2))` lowering uses the opposite sign for the same Unity shape. Audit the drone/turret recoil direction in Studio; pinned pre-existing, non-player.
- **claude_cli-backend reject E2E seam** + the Phase-2/3/4 P2/P3 test-strength followups remain (see the RUN_DIR followups.md).

## Run: pr5-generic-canary-20260611T160829 (Phase 1 — turret canary) 2026-06-12

# Follow-ups — out-of-scope discoveries (PR5 canary PLAN stage)

- PR6 completion guard (`_check_runtime_playability_guard` + `--allow-nonplayable-output`), PR7 default flip
  `legacy`→`auto`, PR8 retirement (`scaffolding/fps.py` + 4 camera/control FPS packs) — downstream of the PR5
  canary per the recut-plan; not in this run.
- Slice H HudControl: domain classification FIXED (§8). Any runtime-dead HUD is a host-runtime binding issue
  (`self.gameObject`→ScreenGui) or the Pause-panel gap — separate UI-host work.
- Pause panel not converted (`HUD.Pause` absent → `_pauseMenu()` nil) — UI-conversion completeness.
- Boot warning `[scene_runtime] connectGameObjectSignal: no touch part on nil` — component bound to a nil
  gameObject; fold into a canary sweep.
- Relation #8 projectile-vs-recoil — indistinguishable at output level (§7.1); needs its own design before the
  host projectile primitive (LOAD-BEARING TOKEN) can be wired. Until then, a coverage gap.
- Other unresolved animation drivers beyond the door (#9): `HostilePlane:Flying` autoplay-loop with empty
  `observed_attribute`; cross-prefab `PlaneHolder` REF gap (§7.4) — each its own slice; do not over-generalize.
- §5.3 single obligations-blob producer: must be named before facts re-scatter as #1/#5/#6 consumers arrive
  (carried as Phase 1 DESIGN open question).
- Relation #1 (turret OnTriggerStay) DURABLE replacement: INJECT + fail-closed verifier, retiring the rung-2b
  comment-keyed `trigger_stay_lowering` (and `trigger_stay_polling` v1/v2). §6 names it as the turret proof's
  second mode (#2 + #1). Deferred from this run (#1 currently works fragilely for the corpus). Cheap once the
  Phase-1 §5 threading lands. Fold into a turret-#1 slice or its own phase next. NB Phase-1 DESIGN: add a
  `callback_kind` field to the same `child_ref_resolver` per-script record (the named single producer) — #1's
  INJECT then rides the existing ChildRefMap threading.

## Phase 1 DESIGN stage discoveries (2026-06-12)

- Backstop check D needs a TWO-LINE factored-shape detector: the existing `_GETCHILDREN_INDEX_RE` only matches
  `recv:GetChildren()[n]` ADJACENT, but the original turret symptom factors it across two lines
  (`local v = X:GetChildren()` then `v[n]`). Slice T adds this detector to `child_ordinal_survivor` so an
  UNRESOLVED ordinal in the factored shape still fails closed. Widen further if new factored shapes appear.
- `ChildRefMap` / `build_child_ref_map` is the seed for the #5/#6 contract obligations (add `shared_flags` /
  `event_channels` fields to the same producer when those phases land) — keeps §5.3's single-producer promise
  as new consumers arrive.
- Check D scope: fires generic-only. RESOLVED at design-review (2026-06-12) — `_run_contract_verifier` →
  `verify_contract` is reached only inside the topology branch, gated `scene_runtime_mode != "legacy"`
  (pipeline.py:4722-4723); legacy never feeds the verifier, so no extra generic flag is needed. Slice T's test
  still asserts a legacy `RbxScript` with a surviving `GetChildren()[n]` is never fed to `verify_contract` in
  the legacy path.

## Phase 1 DESIGN — design-review revision (2026-06-12)

- ChildRefMap canonical key must stay `str(path.resolve())` as #1/#5/#6 add fields to the same record — every
  future consumer of the map looks up by script path, so the resolved/raw dual-key normalization is a
  single-producer invariant, not a per-field concern. If a future producer keys on something other than
  `info.path`, re-verify the lookup side matches.
- F now also owns flipping `test_child_index_lowering.py::TestPipelineInvocation` (generic no longer lowers).
  When relation #1's durable replacement lands later and removes the `trigger_stay_lowering` rung, check whether
  any remaining `lower_child_index` legacy-only assertions need a parallel flip.

## Phase 1 DESIGN — round-2 review revision (2026-06-12)

- Single-scene `all_parsed_scenes` fallback is a pipeline-wide idiom (plan_scene_runtime / extract_assets /
  convert_animations all carry `all_parsed_scenes or [parsed_scene]`). If a future refactor populates
  `all_parsed_scenes` on the single-scene path too, these fallbacks become redundant — single-point cleanup;
  don't scatter more copies.
- Slice FT's corpus-fixture regen needs `CONTRACT_CORPUS_PROJECTS_ROOT` pointed at a checkout with the
  `SimpleFPS` submodule populated (empty in a worktree) + a warm `LLM_CACHE_DIR` (~25 min cold). Run regen
  AFTER the pre-rewrite edits are in the tree; the tool refuses to write if any `child_ordinal_survivor`
  warning survives — that refusal is the proof the pre-rewrite cleaned the turret. Commit the regenerated
  `fixture.json` in the FT diff. Re-run `regen … MiniNet` to confirm check D adds no rows there.

## Phase 1 DESIGN — RESOLUTION A revision (IMPLEMENT-stage divergence, 2026-06-12)
- Player/rifle cam child ref is the Phase-2 #2-dropped coverage gap that check D now ABSTAINS on:
  `weaponSlot = cam.GetChild(0)`, `cam = Camera.main.transform` (foreign object, not the host) → no resolver
  fact → `self.cam:GetChildren()[1]` survives as an unflagged `child_ordinal_coverage_gap` (info, never
  promoted). When Phase 2 lands #2's foreign-receiver handling (resolve `Camera.main` → runtime camera node),
  this graduates to a resolved fact; until then a tracked gap, NOT a corpus failure.
- Fact-based check D mixed-script coverage limitation: a script mixing resolved + unresolved GetChild sites
  (`0 < resolved_total < getchild_total`) is not fully-resolved → check D abstains for the WHOLE script,
  losing the backstop on the site it DID resolve. Accepted (the pre-rewrite already made that site
  construction-safe). If a future corpus script mixes, tighten to a PER-SITE fact (emit the resolved site's
  expected named lookup, assert IT survived). Current corpus doesn't hit it (Turret 3/3, Player 0/1).
- New `RbxScript.child_ref_resolution: ChildRefResolution | None` field (core/roblox_types.py) carries the
  resolver's per-script {getchild_total, resolved_total} tally — the fact check D and the regen'd fixture read.
  As #1/#5/#6 consumers arrive, keep this field on RbxScript the single carrier of per-script resolution facts;
  don't reintroduce a side map the corpus replay can't see.

## Slice FT review P2s (non-blocking, 2026-06-12)
- Corpus no longer WITNESSES the abstain-on-real-survivor path: this regen the AI dropped Player's
  cam.GetChild(0) ordinal, so Player {1,0} has no surviving ordinal in the committed fixture. The
  abstain→info-not-promoted path is still UNIT-covered (test_contract_verifier_child_ordinal) but the corpus
  gate no longer guards mis-promotion of a coverage_gap. Re-add a corpus witness (or a dedicated fixture)
  when Phase 2 (#2-dropped rifle) lands and stabilizes the cam ref.
- Check D is per-SCRIPT not per-SITE: a future {n,n} script with a legit unrelated :GetChildren()[k]
  (non-child-ref positional access) would false-positive. Documented limitation (design §1.3/§6); the SimpleFPS
  corpus doesn't hit it. Tighten to per-site (track which sites were resolved) if a real case appears.

## Slice FT check-D residual heuristics (codex r3, accepted non-blocking)
- _receiver_roots_at_engine_global (contract_verifier.py): a Luau LOCAL named workspace/game/script/Players
  shadowing the engine global is wrongly excluded from the per-site survivor count (false-negative). Pathological
  (a local literally named `workspace`); not in the SimpleFPS corpus. Tighten with Luau-local-shadow awareness if
  a real case appears.
- The survivor regex doesn't match argumented method-call receivers (game:GetService("Players"):GetChildren()[n]),
  so they're non-counted by accident. Make the method-receiver match argument-aware if needed.

## Slice FT resolver completeness residual (codex r4, accepted non-blocking)
- Non-transform-rooted GetChild that _CS_GETCHILD_RE doesn't match (GetComponent<Transform>().GetChild(0),
  xs[0].GetChild(0)) bypasses BOTH getchild_total and the backstop — the resolver only sees transform-rooted
  chains by design, and check D's per-site rule keys on the resolution tally. A {n,n} script with an
  unmatched-shape GetChild could ship an ordinal. Out of the SimpleFPS corpus; tighten check D to also scan for
  ANY positional GetChildren survivor independent of the tally if a real case appears.

## Slice FT shadow model — safe-direction over-abstain residual (codex r6, accepted)
The whole-script shadow model (_declares_shadow) is conservative: one shadow declaration anywhere in a script
suppresses the gameObject/transform alias for the ENTIRE script (e.g. a shadow in method A over-abstains
method B's legit host refs; this.gameObject.transform over-abstains under a gameObject shadow). This is the SAFE
direction (over-abstain → positional ordinal → check D coverage_gap; no mis-resolve), and out of the SimpleFPS
corpus. Make the shadow model method/scope-local (not file-global) if a real coverage loss appears.

## DEFERRED to separate /drive runs (user decision 2026-06-12)
Phase 1 (turret canary) shipped on its own; R and D each get a dedicated focused run because their enforcement
rules are unresolved and deserve their own design + review budget:
- **Phase 2 — Rifle #2-dropped** (rifle `weaponSlot = cam.GetChild(0)` dropped → rifle on far body). Resolve §7.2:
  WHICH refs get a deterministic by-name rebind, and the declarative binding-present record the verifier asserts
  against. Builds on Phase 1's threading + the child_ref_resolver/check-D surface (now on main once this PR lands).
- **Phase 3 — Door #9** (dynamic `GetComponent<Animator>()` → server-placed → client `open` write dead). Resolve
  §7.4: BUILD the driver-MATCH check (driver matched to the right writer, not merely placement-coherent); reduce
  #9's composite signal (dynamic GetComponent + C# param-writes + clip observed_attribute) to one. Door-scoped —
  do NOT over-generalize to the other animation drivers (HostilePlane:Flying autoplay; PlaneHolder cross-prefab REF gap).
