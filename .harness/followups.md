
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


## Run rifle-dropped-ref-20260612T064620 (2026-06-14/15) — followups

## Followups surfaced during DESIGN v3 (PATH CORRECTION — binding-present verifier added)
- [CLOSES the rounds-2/3 non-ordinal-drift silent-miss residual for the DROPPED/RESHAPED case] CHANGE 1 adds a
  binding-present FAIL-CLOSED verifier (`_check_rig_binding_present`, in FAIL_CLOSED_CHECKS) keyed on a NEW
  `rig_binding` carrier (the resolver's deterministic field/child + the lowering's `present` discharge truth, NOT a
  Luau grep). A dropped/reshaped/abstained binding now stamps present=False and fails LOUD — superseding the prior
  "no binding-IR producer exists → logged not closed" residual below (the carrier IS that producer). REMAINING
  residual: only the truly-unanchorable case where the rig FACT never produced (edges 1-3 abstain — correct, the
  upstream signal is genuinely absent).
- [Genuinely DROPPED named line, no assignment LHS at all] still out of scope (the AI omits the binding entirely;
  the lowering abstains, stamps present=False → the verifier now fail-closes on it too, but the fix path — a
  per-script binding-IR that RE-EMITS the missing line — is a larger scope, deferred). Today the verifier makes it
  LOUD rather than silent, which is the correct floor.

## Followups surfaced during PLAN — #2-dropped
- Broader tagged-receiver resolution: this slice admits only Camera.main→MainCamera. A future slice could
  generalize to any uniquely-tagged/GameObject.Find-rooted receiver — needs its own admission pass.
- Fixture shape-variance for dropped refs: the on-main SimpleFPS corpus fixture captures the surviving-ordinal
  shape (self.cam:GetChildren()[1]), not the DROPPED shape. The net should add a dropped-binding witness so the
  binding-present verifier is exercised against the actual failure mode.
- Other animation drivers (HostilePlane:Flying, PlaneHolder cross-prefab REF gap) — distinct slices (§7.4).
- [v2] Retire the legacy fps_rifle_pickup/WeaponMount pack once the generic RETARGET lands + is corpus-verified —
  it is now dead code in generic (pipeline.py:2982 skips run_packs) and superseded by the resolver retarget in
  generic; keep it only as long as legacy mode still ships, then delete with its WEAPON_MOUNTS registry.
- [v2 — supersedes v1's "dropped-binding witness" followup] The v1 binding-present verifier is dropped, so no
  dropped-shape fixture witness is needed. If a future game exhibits a genuinely DROPPED named line (not a
  surviving ordinal), that is a NEW failure mode check D cannot see (0 survivors) — revisit only with a real
  per-script binding-IR producer (§5.2), never an output-shape grep.

## Followups surfaced during PLAN v3 (post review round 2)
- [v3 — SUPERSEDES v2's retarget-emit framing] The retarget emits POST-transpile (rifle_rig_retarget_lowering on
  luau_source), NOT via prerewrite_child_index. When implementing, ensure the new pass is wired into
  transpile_with_contract alongside lower_camera_facet/lower_trigger_stay AND that the resolver does NOT feed the
  rig-rooted fact to prerewrite_child_index (only host-rooted .Find() facts go pre-AI).
- [v3] Emitted lazy shape (OQ1-v3): per-phase DESIGN must read the REAL transpiled consumer sites
  (weaponSlot.position L268, SetParent(weaponSlot) L269) before finalizing the __index proxy vs method-getter
  shape — confirm the proxy's slot[k] member-forward covers every consumer access pattern the AI emits.
- [v3] Idempotency: rifle_rig_retarget_lowering must be twice-call safe (re-running on an already-lowered script
  is a no-op — the rewritten self.weaponSlot = _resolveWeaponSlot() has no GetChild ordinal and the field-anchor
  must not re-fire / re-wrap, nor re-inject the resolver helper). Add a twice-call unit test (the run_packs
  idempotency-bug pattern).
- [v3] Check D is a SHAPE floor, not a rig-lookup-correctness witness: a non-ordinal-but-wrong emit passes green.
  Acceptable only because the emit is deterministic post-AI construction. If the lowering ever gains an AI-shaped
  branch, this assumption breaks — keep the emit fully deterministic.

## Followups surfaced during PLAN v4 (post review round 3)

- [v4 — RESIDUAL, fact-anchor non-ordinal drift] The retarget lowering is keyed on the deterministic field_name
  but LOCATES the site via the AI's `self.weaponSlot =` LHS rendering. On the OBSERVED shape (dde248_player.luau:9,
  LHS present + RHS adjacent ordinal) the anchor fires and check D backstops. But if the AI drifts the LHS to a
  non-`self.<field> =` form (e.g. self["weaponSlot"] =, renamed/split assignment) AND the surviving RHS is
  NON-ordinal, the lowering abstains and check D (which only counts surviving GetChildren()[n] ordinals — adjacent
  contract_verifier.py:567 + factored :580) sees 0 survivors → GREEN with a silently-wrong binding (rifle on the
  wrong body, no crash — the safe direction). This is a RARE, SAFE-DIRECTION silent miss, logged honestly NOT
  closed. No binding-present verifier is added (§4 forbids a behavioral verifier on reference facts; no per-script
  binding-IR producer exists, §5.2; a present-named-lookup grep = pack-in-disguise). Revisit ONLY when a real
  binding-IR producer lands — then a binding-present check can bind loudness to the deterministic fact instead of
  to the AI-dependent presence of an ordinal.

- [v4 — SUPERSEDES v3's "__index proxy" emit] The emit binds a REAL rig-child Instance, NOT a proxy table:
  weaponSlot is consumed as rifle.Parent = self.weaponSlot (SetParent, api_mappings.py:54), the host
  instantiatePrefab nil-guard (scene_runtime.luau:2025), and weaponSlot:GetPivot() — all require an Instance. Port
  the legacy pack's real-Instance lookup (rig:FindFirstChild("WeaponSlot", true), script_coherence_packs.py:551) +
  CameraRigFollower's 30×0.1s bounded retry (autogen.py:539), memoized. Per-phase DESIGN must read the REAL
  transpiled GetRifle() consumer + assignment sites (the dde248_player.luau fixture is Awake-only and lacks them)
  to fix the yield-site binding point: the retry yields, so the resolve must run in a yielding scope (the pack
  resolved at equip-time in GetRifle), or use a single non-yielding sweep at a non-yielding assignment relying on
  the already-injected CameraRigFollower retry. Confirm the chosen binding yields a real Instance for BOTH the
  member-read consumer (weaponSlot.position L268) and the parent consumer (SetParent L269).

---

## S1 review-1.1-3 residuals (2026-06-12, non-blocking — P2/P3)

- **[P3] Shadowed-`self` guard misses the `for ... self in ...` loop-variable form.**
  `_self_is_shadowed_at` detects a `local self` and a `function(... self ...)` parameter shadow,
  but NOT a `for _, self in pairs(t) do ... self.<field> ... end` loop-control-variable shadow.
  A read inside such a loop is rewritten to `self:_resolve<Child>()` (wrong-object bind). Verified
  empirically (probe 4). Pathological for AI-transpiled Unity output (the transpiler never names a
  loop var `self`), and outside the design's enumerated shadow forms (design-phase1.md:169 lists only
  `local self` / `self`-parameter), so non-blocking. Close by adding `for`-control-var shadow tracking
  (introduce a shadow at depth+1 when a `for ... self ... in/=` header names `self`) to
  `_self_is_shadowed_at`, mirrored in both `_rewrite_field_reads` and `_has_surviving_field_read`.

- **[P2] `_neutralize_assignment` still emits a doubled trailing comment** when the camera-child write
  already carried one: `self.weaponSlot = nil -- rig-retargeted: ... -- camera child`. Carried over
  from review-1.1-2 (MINOR, valid Luau, no correctness impact — the RHS span stops before the original
  `--`). Verified still present (probe). Close by extending the replaced span to end-of-line (strip a
  trailing same-line comment) when neutralizing.

## [MINOR/P2] Member-dot seed rejection misses whitespace-separated member access (review 1.1-7)
`_canonical_receiver` (child_ref_resolver.py:711) rejects a member-access seed only
when the IMMEDIATELY preceding char is `.`. A whitespace/newline-separated member
write `other . cam = Camera.main.transform` (or `other.\ncam = ...`) slips through
and is treated as a bare-local seed for the bare `cam` read at GetChild — verified
to admit `Camera.main.transform`. Fails OPEN (wrong admit), not fail-closed. Trigger
is contrived (needs an unbound bare local `cam` read + a spaced member write; not
corpus-realistic Unity C#), hence non-blocking for S1. Robust fix: walk whitespace
(and comments, via `_skip_ws_and_comments_back`) backward from the binding match
start and reject when the nearest preceding code char is `.`.

## S1 accepted residual (user-blessed 2026-06-12) — foreign-member seed false-admit
- `_canonical_receiver` (child_ref_resolver.py) false-admits a rig fact for the CONTRIVED shape
  `other.cam = Camera.main.transform; weaponSlot = cam.GetChild(0)` (a foreign `.cam` member-write
  PLUS a separate bare `cam` symbol used in GetChild), incl. whitespace/comment variants
  (`other . cam`, `other./*x*/cam`). Not corpus-realistic (real FPS code uses a bare
  `cam = Camera.main.transform` or direct `Camera.main.transform.GetChild(n)`).
- DECISION (user): accept as residual; the round-6 member-dot regex fix was net-negative
  (partial close + a `this.cam` legit-seed regression) and is reverted to last-good. The
  fail-closed S1b INDEPENDENT verifier is the designed net for a mis-admitted/dropped binding.
- FORWARD PRINCIPLE (user): do NOT solve this with C# parsing / more complex regex — that path
  does not converge. If seed/receiver classification ever becomes load-bearing beyond this
  contrived residual, solve it by RELYING ON AI for the semantic C# classification (which
  variable holds the camera), keeping the deterministic layer on the OUTPUT lowering + the
  fail-closed verifier, NOT on C# input parsing.

## Followup surfaced during S1b review (slice 1.2)
- [OUT-OF-SCOPE for S1b — lowering side] `rifle_rig_retarget_lowering._neutralize_assignment` neutralizes only ONE
  camera-child write per script (the best/first camera-rooted match). A script with a SECOND camera-child ordinal
  write of the same field (e.g. a `Rebuild()` that re-runs `self.weaponSlot = self.cam and self.cam:GetChildren()[1]`)
  leaves that second write surviving yet stamps `present=True`. At runtime that surviving write re-clobbers the field
  back to a camera child. The verifier S1b SHOULD be the independent floor that catches it, but its
  `_rig_has_surviving_ordinal_write` is single-line-span-limited and misses a MULTILINE surviving write (see
  review-1.2-1.md MAJOR). The lowering's own neutralizer would also need to handle the multi-write case to fully
  close this. Narrow AI shape (corpus has one single-line write); flagged for a future hardening slice.

## [P2] S1b ordinal-write tail regex: `.GetChildren()[n]` member form escapes (review 1.2-4)
`_RIG_ORDINAL_WRITE_TAIL_RE` (contract_verifier.py:808) matches `:GetChildren()[n]`
(colon method-call only) but `[:.]GetChild(n)` (colon OR dot). A surviving write in
the `<recv>.GetChildren()[n]` member-access form is therefore NOT detected ->
fail-OPEN under a forged `present=True`. Practical risk is low: `GetChildren` is a
colon-method in Luau and neither the lowering nor idiomatic transpiled AI output
emits the `.`-form indexed-children shape; the colon form (the real corpus shape)
is detected. If hardened, mirror the `GetChild` alternation: `[:.]GetChildren()[n]`.
Not a regression; documented fail-closed bias is about not MISSING the camera-child
shape the lowering actually neutralizes, which it does.

## [P2] S1 Path A Tier-2 neutralize misses multiline-RHS single write (codex S1-PathA r2)
`_neutralize_assignment` skips a provable single camera-child write when the RHS starts on the
next line after `=` (`self.weaponSlot =\n    self.cam:GetChildren()[1]` → empty RHS span → no
neutralize). HARMLESS: Path A discharge is decoupled and still lands `present=True` (reads
rerouted); only the best-effort Tier-2 hygiene cleanup misses this formatted variant. Fix if
hardened: span the RHS across a leading newline (mirror S1b's continuation-aware span).

## Followups surfaced during REVIEW 1.2 (S1b Path A re-anchor) — verifier discharge-scan blind spots (P2/residual, NOT P1)
- [Whitespace/newline dot-form read NOT detected] `_rig_has_surviving_field_consumption`'s dot-read regex is `self\.<field>` (no `\s*` around the `.`), so a surviving raw read written `self . weaponSlot` (spaces) or `self.\n  weaponSlot` (newline-split) is MISSED → `_rig_binding_discharged` returns True with a real un-rerouted read present (fail-OPEN). NOT a Path A regression: (i) SYMMETRIC with the lowering's `_rewrite_field_reads`, which uses the identical `self\.<field>` regex and likewise leaves it un-rerouted; (ii) PRE-EXISTING at 5da0eab (verified: the old gate also returned True). The AI does not emit whitespace/newline around member-access dots in practice. Residual — would close by code-projecting the dot-junction whitespace (mirroring the round-3 ordinal-write projection) on BOTH the lowering and verifier together so they stay symmetric.
- [Computed-key bracket read NOT detected] `_rig_bracket_read_re` matches only a LITERAL string key `self["weaponSlot"]`; a computed/variable key `local k="weaponSlot"; self[k]` survives un-rerouted yet discharges True (fail-OPEN). Genuinely undetectable by any field-name-anchored scan (the key is not lexically the field); same family as the spec's explicitly-scoped literal-bracket boundary. AI does not emit computed-key self-indexing. Residual, not a blocker — the real `GetRifle` uses dot-form.

## [P3] `_rig_has_surviving_ordinal_write` is now fully DEAD code (review 1.2-3)
After the Path A re-anchor (commit 7b59488) `_rig_has_surviving_ordinal_write`
(contract_verifier.py:1205) is no longer called from `_rig_binding_discharged` — the
discharge gate is now `_rig_has_surviving_field_consumption` (the READ reroute). The
function is referenced ONLY in a docstring ("best-effort SECONDARY DIAGNOSTIC"), but
nothing invokes it. NIT/cleanup: either wire it as the promised secondary diagnostic
or delete it. Note: this RETIRES the premise of the two earlier P2 ordinal-write
followups (`_rig_has_surviving_ordinal_write` single-line-span / `.GetChildren()[n]`
member-form escapes) — those describe a function that no longer gates anything, so a
forged `present=True` with a surviving ordinal write is now caught (if at all) only by
the READ-reroute gate, not the ordinal scan. Not blocking; no behavior depends on the
dead function.

## [P2] rig binding-present: a long-bracket string table key reading the field is not caught (fail-open, exotic)
**Where** `converter/converter/contract_verifier.py` `_rig_has_surviving_field_consumption` / `_rig_pos_is_real_code` (long-bracket blanking).
**Found** review-1.2-4 (slice 1.2 / S1b Path A round 4, reviewed-sha d51ae90).
**Issue** A genuine field READ written as a long-bracket string table key — `self[ [[weaponSlot]] ]` or `self[ [=[weaponSlot]=] ]` — is NOT detected as a surviving consumption: `_rig_pos_is_real_code` blanks long-bracket interiors (the intentional guard against a fake `_resolve` token inside `[[ ]]`), so the `weaponSlot` token inside the key is treated as non-code and the read survives invisibly. With a real resolver method + call present, `_rig_binding_discharged` then returns True despite an un-rerouted raw field read → the verifier would PASS a binding that ships a wrong/nil rifle for that read site.
**Why not blocking** Exotic, non-load-bearing: a long-bracket string used as a *table key* is not idiomatic Luau and is not a shape the C#→Luau transpiler emits (it emits dot-form `self.field`; the clean-string-key and dot paths ARE caught). No occurrence in the corpus (`grep -rE '\[\s*\[\[' tests/fixtures/contract_corpus/` is empty). It is the same blank-the-long-bracket-interior guard that (correctly, load-bearingly) defeats a forged `_resolve` decoy inside `[[ ]]`; closing this edge would require treating long-bracket interiors as code for the field-key scan, in tension with that guard.
**Fix (if ever needed)** In the string-key scan only, additionally scan long-bracket string-literal interiors for a key exactly equal to `<field>` (a constant string key), while keeping the code-token blanking for the resolver-decoy guard. Gate on evidence the transpiler ever emits a long-bracket table key.

## [P2] S1b _rig_decode_short_string: \0-before-\ddd decimal-escape edge (codex S1b-PathA r5)
`_rig_decode_short_string` treats `\0` as a simple escape before the `\ddd` decimal path, so a
decimal escape with a leading zero (e.g. `\083`) misdecodes — `self["wea\112on\083lot"]` (decodes
to weaponSlot) false-passes discharge. EXOTIC / non-transpiler-emittable (the C#→Luau transpiler
emits dot-form `self.field`, never hex/decimal-escaped bracket-string field keys). Both voices
agree the boundary is sound for realistic transpiler output. Fix if hardened: parse `\ddd` (1–3
decimal digits) before the `\0` simple-escape special-case.

## Followups surfaced during S3 (1.4 — NET / E2E witness)
- [LOGGED, not closed — the aliased-read residual (decisions.md:690)] The verifier's fail-closed
  boundary catches a non-`self` receiver read (`owner.<field>`, receiver-alias `local p = self;
  p.<field>`) ONLY when it survives ALONGSIDE the rerouted dot-form reads (the boundary scan finds
  the surviving non-`self` read). A FULLY-aliased consumer — where the ONLY read is via an alias
  (`local p = self; ... p.<field>`) with NO surviving `self.<field>` dot-form AND no rerouted call —
  can discharge True while the alias holds the stale init-write value (the resolver method + a call
  may be injected by the inject step even when no dot-read was rerouted). Real `Player:GetRifle()`
  uses the dot-form, so the corpus is unaffected; closing this needs a per-script binding-IR producer
  (§5.2) that re-emits the missing reroute, not an output-shape grep. Exotic / not transpiler-emitted.
- [E2E Studio gameplay witness owed to the main session] S3 asserts on the converted OUTPUT artifacts
  (the deterministic luau race harness + the captured-fixture conversion-output proof + the
  end-to-end fail-closed boundary through the real pipeline hook). The LIVE Roblox Studio playtest
  witness (the rifle visibly seats under `_MainCameraRig`'s `WeaponSlot` in a running place) is run
  SEPARATELY from the main session (Roblox MCP unavailable in subagents — e2e-studio-half-needs-main-session).

## [PHASE-1 REVIEW r1 — P1 routed to slice 1.2, codex BLOCKING, empirically confirmed]
check-D (`_check_surviving_child_ordinal`, contract_verifier.py) fails closed `child_ordinal_survivor`
on a DISCHARGED rig binding's dead init-write ordinal (`self.cam:GetChildren()[1]` left intact when the
Tier-2 neutralize SKIPS, e.g. single-line-if). Confirmed on REAL `lower_rifle_rig_retarget` output:
`_rig_binding_discharged`=True, `rig_binding_present`=GREEN, yet check-D reds → a correct Path A
conversion fails the ship gate. Budget {getchild_total:1, resolved_total:1} is the real accounting
(camera GetChild stamps getchild_total=1; resolved_total=0 facts+1 rig_fact). Fix: make check-D
rig-aware — exempt a discharged rig binding's write-LHS ordinal from the survivor count (the dead
`_rig_has_surviving_ordinal_write` diagnostic already detects this shape). Regression test belongs in
test_rig_binding_present.py (full verify_contract, not just the rig-binding row).

## [PHASE-1 REVIEW r1 — codex MINOR, P2, logged]
Acceptance tests reconstruct the resolver method name as raw `_resolve{child}` (test_contract_corpus.py:232,
test_rifle_rig_retarget_e2e.py:842/943) but production sanitizes/hash-suffixes non-identifier child names
(rifle_rig_retarget_lowering.py:94, contract_verifier.py:759). A non-identifier rig child ("Weapon Slot",
"Muzzle.01") would false-red the TEST despite correct production behavior. Test-robustness only; harden when
those test files are next touched (owned by slices 1.3/1.4).

## [SLICE 1.2 r3 — ACCEPTED RESIDUAL: data-flow dynamic self-index reads]
`_rig_has_surviving_dynamic_self_index_read` (contract_verifier.py) now normalizes the index
receiver — peels balanced parens + whitespace (comments are blanked in the code projection) — so
ALL SYNTACTIC self-index variants fail closed: `self[k]`, `(self)[k]`, `( self )["weapon"..suffix]`,
`((self))[k]`, `self [k]`, `self --c\n[k]`. NOT detected (accepted residual, logged not silently
ignored): fully DATA-FLOW dynamic reads — receiver ALIASING (`local s = self; s[k]`),
`rawget(self, k)`, and metatable `__index` indirection. Accepted because (a) the deterministic
C#→Luau transpiler emits dot-form `self.<field>` field access and NEVER a computed/aliased field
read, so these are non-reachable from real conversions (the corpus is dot-form), and (b) proving
"the field is never read" over arbitrary aliased Luau is beyond static text analysis. The discharge
check is best-effort-conservative on the syntactic forms. The aliasing residual has an explicit
xfail marker (test_pathA_r3_aliased_self_dynamic_read_residual_is_documented) making the boundary
visible. If a real conversion ever surfaces an aliased/rawget field read (it should not), promote to
a data-flow check.

## [S3 1.4 r3 review — codex P2 residuals (non-blocking, logged)]
- [MEDIUM] The 1.4 e2e real-chain witness sets the check-D `child_ref_resolution` {1,1} budget by hand rather than capturing it from the real pipeline (the real tally IS covered by slice 1.3's corpus fixture). Test-fidelity nicety; harden if the e2e is reworked.
- [LOW] One survivor-site ordinal in an e2e helper is baked rather than read from `fact.ordinal`. Minor; the real-chain witness reads anchors from the fact, so promotion-regression is still caught.

## [phase1 r3 review — codex MINOR, P2, logged (slice 1.3 owned, not fixed this round)]
test_contract_corpus.py:~252 hardcodes the resolver method name as `_resolve{child}`, but production uses `_method_suffix(child)` (hashes non-identifier names) — a rig child like "Weapon Slot" would false-red the test. Test-robustness only; harden when test_contract_corpus.py is next touched.

## From review-1.1-5 (P2, non-blocking)
- [doc] rifle_rig_retarget_lowering.py:765 docstring cites a non-existent test name
  `test_f2_no_mutual_mask_lowering_at_least_as_lenient`; actual is
  `test_f2_no_mutual_mask_against_verifier_or_conservatism`. Update the reference.
- [hardening, fail-closed-safe residual] The lowering's reroute + both discharge
  detectors (`_rewrite_field_reads`, `_has_surviving_field_read`,
  `_has_unrewritable_boundary_read` dot-branch) use a literal-dot `self\.<field>`
  regex, so spaced/newline-dot consumer reads (`self . weaponSlot`, `self.\nweaponSlot`)
  escape the reroute AND the detectors → lowering can stamp present=True with a surviving
  read. The slice-1.2 verifier (trivia-robust + `present AND discharged` gate) catches it
  and fails closed, so this is the documented lowering-lenient/verifier-strict residual
  (one more form than FINDING 2 enumerates), NOT silent-wrong. Optional: give the three
  lowering regexes `\s*\.\s*` tolerance so the lowering abstains on these forms too.

## [1.1 r5 — codex mutual-mask claim REFUTED + Claude P2s]
codex r5 flagged concat-bracket/spaced-dot/paren-dot field reads as mutual-masks (silent-wrong). EMPIRICALLY REFUTED at integration: real lowering output (control discharged=True) + real r3 verifier _rig_binding_discharged → all three return discharged=False (CAUGHT). The lowering is lenient (present=True hint) but the verifier (authority, FIX-1) fires → fail-closed-safe. codex's claim was an isolated-1.1-branch artifact (the r3 verifier hardening lives on slice 1.2, absent on 1.1's base).
P2 (Claude, non-blocking): (a) a _binding_discharged docstring cites a since-renamed test; (b) the lowering's literal-dot reroute regex doesn't rewrite `self . weaponSlot` spaced-dot reads — harmless (verifier catches; non-reachable transpiler shape).

## [P2] Re-run exact corpus Player raw source through lowering (phase1 harden-regress r2)
The corpus/e2e tests pin rig_binding discharge from the pre-stamped fixture source +
verify_contract re-derivation, not by re-running lower_rifle_rig_retarget over the
pre-injection source. Mitigated by test_h1_elseif_* (drives real lowering on the
corpus elseif shape). Optional hardening: add a committed test that reconstructs the
raw (pre-injection) corpus Player source and re-runs the lowering, asserting
present=True. reviewed-sha dcc7660.

## Run: turret-projectile8-20260614T235948 (2026-06-15)


## Run: turret-projectile8-20260614T235948 (2026-06-15)


- (codex phasedesign1-r2 #5) TODO.md turret entry (:17-51) still states the OLD pre-pivot diagnosis
  (projectile semantics: ApplyImpulse(60)+anti-gravity/raycast + damage surface). The run pivoted to
  the unit-level root cause (mass 187 → AssemblyMass==m_Mass + force×SPM; see design.md + physics-spike.md).
  Update the TODO turret entry to the unit-level diagnosis at ship (the ledger-promotion step).

- (slice 1.1 harden) Add test coverage for the frozen-position-constraints anchored path
  (m_Constraints & 0b111 == 0b111 -> not stamped) and assert unrelated attributes remain on the
  outer Model after the geometry wrap. Codex MINOR on slice 1.1.

- (Phase 3 — deferred from turret-projectile8, user-chosen) Scale-consistent gravity for dynamic
  objects: unanchored non-character rigidbodies experience Unity_gravity × STUDS_PER_METER via a
  per-ASSEMBLY runtime correction (keyed on stamped anchored/UseGravity/GravityScale), leaving the
  player Humanoid on Roblox 196. Independent faithfulness (slow/long-range projectile arcs); the fast
  turret bullet does NOT need it (live-verified). WIDE blast radius (every dynamic object, every game)
  → its own design + review + corpus/canary regression + slow/long-range-arc acceptance. Design notes:
  per-assembly (not per-part, to avoid fighting welded CoM); verify resting/sleeping behavior. See
  design.md Phase 3 + archive for the original analysis.

## ── Run: gravity-scale-correction-20260615T093547 (scale-consistent gravity, 2026-06-15) ──
# Follow-ups — discovered during gravity-scale-correction planning

- Converter never parses Unity's DynamicsManager project gravity — hardcodes 9.81/196.2 throughout
  (component_converter.py:783, script_coherence_packs.py:3067). For full faithfulness on non-default
  project gravity, add real DynamicsManager parsing to source the project-g constant. This design
  ships a 9.81 default constant; mechanism is identical for a parsed value.

- CharacterBridge:SetGravity (physics_bridge.luau:96) is effectively dead — zero runtime callers;
  reachable only if transpiled C# calls it. Its workspace.Gravity = |g|*20 (196.2 at default) is
  itself scale-unfaithful for dynamic bodies — the very bug this design fixes. Audit/remove or wire
  properly in a separate cleanup.

- Legacy (non-generic) scene-runtime mode may share the dynamic-body fall path with the same
  ~5.6x-too-fast problem. This design is generic-only. If a later check confirms legacy is affected,
  scope a parallel legacy correction (likely via coherence packs, since generic skips packs).

## Phase-1 detailed-design notes (from design-review r1 Claude MINORs — P2, not blocking)
- Pin the ACTUAL server-only gate in Phase-1 design: "gated like the placement-binding loop" is
  imprecise — that loop is domain-filtered (runs both client+server). The real server-only lever is
  the SERVER entrypoint (SceneRuntimeServer in ServerScriptService) running the sweep, OR an explicit
  `domain == "server"` guard. Phase-1 design must specify the exact gate so the sweep runs ONCE.
- The "server VectorForce replicates to clients" pillar is the one cross-context claim on engine
  knowledge alone — confirm it in the Phase-1 resting/auto-sleep Studio spike (which runs anyway).

## Phase-1 detailed-design note (design-review r2 Claude MINOR — P2)
- The emit gate (dynamic-body scan that decides whether to emit the standalone gravity script) must
  scan `replicated_templates` RECURSIVELY, not just `workspace_parts` — a spawn-only dynamic prop
  has no workspace presence at emit time. Otherwise the runtime-clone gap reopens one level down
  (script never emitted → DescendantAdded hook never installed). Pair with the DescendantAdded hook.

## Follow-up: client-LOCAL dynamic prefab clones (deferred from gravity-correction)
Client-script `instantiatePrefab` clones that never replicate to the server fall at raw Roblox
gravity (the server-only correction can't see them). Narrow edge (client-spawned physics already
doesn't replicate). If corpus evidence shows it matters: add a TARGETED hook on the client
`instantiatePrefab`/`clonePrefabTemplate` path that corrects ONLY client-local instances (avoids the
replication race a blanket client DescendantAdded would create). Do NOT reintroduce a blanket
dual-domain DescendantAdded (net-negative: replication race + ownership double-force).

## Phase-1 detailed-design notes (design-review r4 Claude P2s — not blocking)
- DescendantAdded may fire before a multi-part clone's welds/AssemblyMass settle → defer the
  per-clone correction a frame / wait for the assembly to settle before reading AssemblyMass.
- A runtime anchor/weld transition can leave a stale correction force (e.g. a body later anchored
  still carries its VectorForce). Consider re-checking on the relevant property changes, or accept
  as a rare edge — decide in Phase-1 design.
  RESOLVED (Phase-1 design): ACCEPT as a documented edge, no re-check wired — anchored parts ignore
  forces in Roblox so the stale force is inert (D-P1.9). Revisit only if Phase-2 corpus shows drift.

## Out-of-scope discoveries (Phase-1 detailed design)
- The Phase-1 client clone-site hook adds ONE new plan field `gravityDesiredBaseStuds`
  (generate_scene_runtime_plan_module). If a future phase wants the standalone server script to also
  read from the plan (instead of baking its own constant), the field is already there — but do NOT
  couple the server script to the plan (the decoupling is load-bearing for no-runtime games).
- `project_gravity.py` parses ONLY abs(m_Gravity.y). The rest of the converter's 9.81/196.2 hardcodes
  (component_converter.py:783, script_coherence_packs.py:3067) still assume default g — a project with
  non-default gravity will now free-fall faithfully but other 9.81-derived conversions remain frozen.
  Full sweep of the 9.81 hardcodes is a separate follow-up (already noted at top of this file).
- Non-uniform gravity (m_Gravity.x/z != 0) and Physics2D gravity remain OOS; the parser warns + fails
  open to abs(y). If a corpus game needs a non-down gravity vector, scope a vector-force variant.

## Phase-1 IMPLEMENT notes (phasedesign review r2 — P2/MINOR, not blocking)
- P2: a welded CROSS-Model assembly can resolve the wrong "owning Model" for GravityScale/UseGravity
  lookup. Dominant single-body mesh-wrap case is correct; handle/》note the cross-Model case in the
  helper (walk to the assembly root's owning Model, or read facts from the _UnityMass-stamped part's
  nearest ancestor Model). Decide in IMPLEMENT.
- MINOR: structural tests asserting the baked float must match Python repr() formatting (rbxlx_writer
  serializes floats via repr at :587) — assert the repr form, not a hand-written literal.
- MINOR: if resume rehydration of scene_runtime["gravityDesiredBaseStuds"] is relied on, pin it with an AC.

## Phase-2 detailed-design out-of-scope discoveries
- **Unify the `contract_corpus` capture to span `write_output` artifacts.** Today
  `tools/regen_contract_corpus.py` aborts at `_run_contract_verifier` (captures only `{topology,
  scripts}`), so it cannot snapshot `workspace_parts`/`replicated_templates` or emitted gravity/runtime
  scripts. A future shared corpus surface that captures the post-`write_output` instance tree would let
  the gravity canary (and others) replay a real end-to-end emit instead of a constructed place. Larger
  infra change; out of scope for the gravity safety net.
- **A real AI-driven end-to-end conversion of a dynamic-body game as a committed canary** (slow,
  AI-cache dependent) is deferred. The constructed-place canary + the Studio acceptance cover the
  emitted delta and the behavior without an AI run. Scope a real-conversion fixture only if a future
  regression is traced to AI-output shape interacting with the gravity emit.
- **Populate/initialize the SimpleFPS submodule (or add a small bundled dynamic-body project) for CI.**
  SimpleFPS is an unpopulated submodule so `conftest.simplefps_project` skips — any SimpleFPS-keyed
  gravity assertion would be vacuous in CI. The Phase-2 canary side-steps this with a constructed place;
  if a future test genuinely needs a real dynamic-body scene in CI, populate the submodule or add a
  tiny `corpus_projects/GravityProps` project (Slice 2.2, currently conditional).

## Phase-1 detailed-design re-scope (dual-voice review — Model-carrier dynamics is a SEPARATE converter concern)
- **Make no-mesh-parent / multi-sub-mesh Unity Rigidbodies actually DYNAMIC in the converter.**
  Today a Rigidbody on a no-mesh parent with children (scene_converter.py:1886) or a multi-sub-mesh
  lowering (1923) becomes a `Model` CARRIER: the `_UnityMass` + GravityScale/UseGravity/anchored facts
  are stamped on the outer Model (2024 + 2789-2814), but the Model's descendant BaseParts have no own
  Rigidbody so they are emitted **Anchored** (RbxPart.anchored default True, core/roblox_types.py:178).
  The converted body is therefore effectively STATIC — it does not free-fall regardless of gravity.
  FIX (separate converter change, NOT this feature): lower the parent-Rigidbody state (unanchored +
  the mass) onto a representative descendant BasePart so the carrier's assembly actually simulates.
  IMPACT on gravity correction: the scale-faithful gravity feature is already CLASS-AGNOSTIC in its
  scan, so once such carriers become dynamic (their representative part unanchored) the existing
  skip-if-anchored rule stops firing and the helper corrects them AUTOMATICALLY — no change to the
  gravity feature is required. The gravity feature does NOT regress these carriers today (they are
  static with or without it; the helper correctly skips them and does NOT tag them, so they remain
  correctable the moment the converter makes them dynamic).

## Phase-1 HARDEN targets (slice-1.1 review P2s — non-blocking, address in Stage 4.5)
- project_gravity.py `_GRAVITY_LINE_RE`: `[^}]` matches newlines → constrain to `[^}\r\n]*` so a
  malformed m_Gravity line (missing `}`) falls back to the 9.81 default instead of mis-parsing; add a
  malformed-line regression test.
- test_gravity_plan_field_emit.py (AC16): add the no-key companion case — assert `gravityDesiredBaseStuds`
  is ABSENT from the emitted plan .source when not stashed (pins the deterministic fallback).

## Phase-1 HARDEN targets (slice-1.2 review P2s — non-blocking, Stage 4.5)
- test_gravity_correction_emit.py AC8c/AC8e: strengthen so BOTH scan surfaces (boot sweep + DescendantAdded)
  are asserted guarded with the _UnityMass/_Rigidbody2D predicate, and the scan is NOT gated by IsA("BasePart")
  (assert the predicate appears twice / class-agnostic). Pins against a BasePart-only or dropped-2D-check regression.
- test_gravity_correction_emit.py AC16b: drive plan_scene_runtime() on the same Pipeline instance before
  _subphase_inject_gravity_correction (real producer→consumer hop), not a manually-seeded dict; add a
  context save/load round-trip if AC16b is meant to cover resume rehydration.

## Phase-1 HARDEN target (slice-1.4 review P2 — non-blocking, Stage 4.5)
- test_gravity_correction_parity.py: make the mirror-extraction anchors whitespace-insensitive (locate
  open/close sentinels after splitlines()/strip() or via an indent-insensitive regex) so a wrapper-only
  reindent doesn't trip the hard-error path before the helper-body comparison runs.

## Phase-2 HARDEN target (slice-2.1 review P2 — non-blocking, Stage 4.5)
- test_gravity_correction_canary.py _s4_welded_assembly: model the REAL welded-assembly representation
  (RbxConstraint/WeldConstraint, whatever the converter emits for Unity FixedJoint) and assert the weld
  relation in the anti-vacuity check, so the S4 shape is genuinely covered (not just two co-located parts).

## Residuals after finalize (non-blocking, both voices agree no P1)
- Aggregate test: add one CI test driving the full client chain (plan_scene_runtime stash →
  generate_scene_runtime_plan_module emit → assert gravityDesiredBaseStuds present for the client
  _correctClonedDynamics consumer). Currently covered in pieces + Studio acceptance.
- Dead defensive `gravityScaleAttr or 1.0` read in the shared helper (both mirrored sites): dead for
  in-scope 3D (GravityScale is 2D-only/excluded). Kept defensively; remove only if it ever earns its keep
  (removal churns both parity sites + the parity token list).
- S3/S6 Model-carrier dynamic bodies: anchored/static in today's converter output → not corrected (no
  free-fall to correct). Making no-mesh-parent / multi-sub-mesh rigidbodies dynamic is separate converter work.

## HANDOFF (cross-run contracts introduced this run — land in converter/docs/design before next run)
HANDOFF: [gravity] SceneGravityCorrection — a standalone generic SERVER script emitted to
ServerScriptService whenever the converted Workspace (scene ∪ prefab templates) has a dynamic 3D
_UnityMass body; boot workspace:GetDescendants() sweep + deferred DescendantAdded; client
instantiatePrefab clone-site hook in scene_runtime.luau. Shared correctDynamicAssembly helper
(token-identical across autogen._GRAVITY_CORRECTION_HELPER_LUAU and the scene_runtime.luau mirror,
pinned by test_gravity_correction_parity.py). Force.Y = AssemblyMass*(workspace.Gravity -
|unity_g|*gravityScale*STUDS_PER_METER); _ScaleGravityCorrected tag = idempotency. Document in the
scene-runtime contract doc before a future run touches the host runtime.
HANDOFF: [gravity] _Rigidbody2D attribute — stamped unconditionally at the Rigidbody2D branch
(scene_converter.py 2D branch) AND added to the mesh-wrap move-list so it co-locates with _UnityMass;
2D bodies are EXCLUDED from gravity correction (Physics2D out of scope). Future 2D work keys off this.
HANDOFF: [gravity] gravityDesiredBaseStuds — plan field (in _PLAN_KEYS_FOR_HOST) carrying
|unity_g|*STUDS_PER_METER, parsed early in plan_scene_runtime from DynamicsManager m_Gravity (abs(y),
9.81 default). Consumed by the client clone-site hook; baked into the server script.
