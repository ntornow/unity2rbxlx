
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

# === Run hud-values-generic-20260616T161253 (2026-06-16) ===

## Phase-1 out-of-scope followups
- Respawn / ResetOnSpawn re-clone rebind of instance.gameObject (scene_runtime.luau:2861-2863) — pull into
  scope only if §5 instrumentation proves it is the lead blank-Total cause.
- StarterGui-template-fallback observability: emit a warn if a PlayerGui-nil template write happens silently.
- Cross-domain (server-producer / client-consumer) BindableEvent channels must route via RemoteEvents; the
  event-channel pre-pass is gated to same-domain signals and must NOT mask a missing cross-domain bridge.

## Slice 1.1 residuals (static-event channel identity) — non-blocking, dual-voice findings

- **Verifier alias-read false-POSITIVE (fail-open noise).** `contract_verifier._check_static_event_rendezvous` accepts a read only in the direct forms `<M>.<F>.Event` / `:Connect` / `:Fire` / `if <M>.<F> then`. A valid aliased read `local ev = Player.AmmoUpdate; if ev then ev:Connect(...) end` warns spuriously. Fail-OPEN (a visible warning, not a blocked build); all real SimpleFPS shapes use direct reads. Broadening the read matcher risks re-opening the string-literal bypass — defer until a real project trips it.
- **Multi-declarator `static event` surfaces only the last name.** `script_analyzer._RE_STATIC_EVENT` on `public static event H A, B;` captures `B` only; `A`'s channel is silently never pre-set. Rare C# idiom; a comma-split would fix it. No real test project uses it.
- **`findOrCreateChannel` wrong-class collision (now mitigated, residual edge).** Hardened to scan children for an existing BindableEvent of the right name+class before creating. If a non-BindableEvent of the same name persists alongside, the channel BindableEvent coexists (no duplicate BindableEvent), but the AI producer's own `ensureEvent`-under-RS could still pick a different instance if its parent differs from the module's container. The cross-domain/cross-container divergence (AI parents under RS by name; pre-pass parents under the module container) is a Slice-1.2-or-later generality concern; SimpleFPS modules are in RS so they coincide.
- **Slice 1.2 (host-resolution / blank-Total)** remains probe-gated per design §5/§6 — NOT in 1.1.

## [slice 1.1 r3] Generic RS-name-lookup consumer + folderized channels
The round-2 folderization moves a static-event BindableEvent under a per-module Folder. The real SimpleFPS
consumer reads the module FIELD (unaffected), but a HYPOTHETICAL generic consumer that resolves the event by
NAME under ReplicatedStorage (e.g. `ReplicatedStorage:WaitForChild("AmmoUpdate")`) would break. No corpus
instance. Harden: extend the rendezvous verifier to also fail-closed on RS-name-lookup consumers of a
folderized channel, or keep the channel discoverable by a stable name. (Also: regen the SimpleFPS contract
corpus fixture — codex flagged it may diverge from the real emitted HudControl shape.)

## [scope B] Health-bar fill (Unity Slider visual binding) — deferred
Same lowering class as the Phase-2 Toggle→Checkmark binding: UpdatePlayerHealth sets SetAttribute("value")/
("Value") with nothing resizing the Health fill ImageLabel. Build the Slider→fill side of the generic UI-control
visual binding when health/damage is back in scope. (User deprioritized damage 2026-06-16.)

## [phase2 design] Subagent authored against wrong base (concurrent-session branch move)
The Phase-2 design subagent ran in the MAIN repo cwd (moved by a concurrent session to addressables@2892b00),
NOT the design2 worktree (drive/RUNID@babdbcf). ui_translator.py (main fix site) is IDENTICAL across bases, so
the core design holds; but scene_runtime.luau/autogen.py differ — the runtime-integration citations
(_applyToggleGraphicBindings site, _PLAN_KEYS_FOR_HOST, workspaceFind SRI resolver) must be re-anchored against
drive/RUNID at implement. LESSON: always pass subagents the explicit worktree path AND have them verify HEAD.

## [FOLLOW-ON RUN] Item-pickup checkmark — generic Unity Toggle isOn->Checkmark visual binding
Scoped out of this run (user chose A: ship Phase 1 alone). The detailed design + dual-voice review are ready:
- Design: design-phase2.md (in this RUN_DIR). Core mechanism sound: capture the Toggle's serialized graphic
  GameObject SceneRuntimeId + m_IsOn into a plan key; runtime pass resolves via workspaceFind SRI resolver,
  connects GetAttributeChangedSignal("isOn")->graphic.Visible, applies initial m_IsOn. Anchored on the Unity
  Toggle component + serialized graphic ref (generic, no node names). ui_translator.py:738 currently emits only
  ToggleIsOn and drops the graphic ref.
- Review findings to address (review-phasedesign2-1.md + codex-review-phasedesign2.md):
  1. [BLOCKING] Need a NEW component-fileID->GameObject resolver in the parser (onClick m_Target stores
     target_file_id RAW with zero readers; no component->GameObject map exists). Build the index from the parsed
     scene; thread into _convert_ui_element. This is a real slice seam — NOT one slice.
  2. [P1 codex] Binding must apply initial isOn->visibility BEFORE the Awake/OnEnable batches (the proposed
     end-of-start() site is too late); hook before _runAwakeEnableStart in _completeDeferredBatch.
  3. [P1 codex] Late-clone coverage isn't generic — a Toggle in a late-cloned ScreenGui with no deferred UI
     component on its host never rebinds. Need a generic mechanism (e.g. PlayerGui.DescendantAdded watch).
  4. Re-anchor all scene_runtime.luau/autogen.py line citations against the run base (base-drift).
  5. Re-decode the SRI values from the real rbxlx before pinning acceptance tests.
- Also covers health-fill (Slider) via the same lowering when health is back in scope.
- FIRST: confirm the checkmark actually fails in Studio (collect an item) before building — not yet playtest-confirmed.
## Run turret-bullet-damage-20260616T101918 (2026-06-16T08:27:13Z)

self.gameObject.Parent:X() under-detection edge (self. skip precedes field promotion) — minor, design lists only self.gameObject. Consider hardening if a real conversion exercises it.

- [Goodhart residual] validator proves a method EXISTS, not that it's semantically right; a real-but-wrong Roblox method passes. Out of scope; monitor.
## Run addressables-unit1-20260615T133903 (Addressables Unit 1) — 2026-06-15T10:05:21Z

## For the per-phase design (phase 1) — review pointers (not blocking)
- Real collision locus: `autogen.py:_resolveTemplate` keys instantiatePrefab on the
  unique prefab_id but COLLAPSES to bare `template_name` for the Templates lookup. The
  unique-key fix must re-key BOTH the lookup AND the on-disk Templates child name as a
  coupled pair (the one seam where the fix can diverge).
- [P3] Stale comment `autogen.py:825` claims the prefab_id path already disambiguates
  colliding names — it doesn't. Fix the comment when re-keying.
- Resolver return shape (verified): `by_address: dict[str, list[str]]`; tests prove
  duplicate addresses -> multi-element lists, and `"Trash Cat" -> ["<catguid>:.../Cat/character.prefab"]`
  (a singleton resolving to Cat).

## Out-of-scope discoveries (from phase-1 detailed design, 2026-06-15)
- **[design.md correction] `by_guid` is NOT in the plan `addressables` block.** design.md
  lists `by_address`/`by_label`/`by_guid`, but the resolver's `PrefabAddressables` has no
  `by_guid` (only the raw guid-keyed `AddressablesIndex` does, which the host never reads).
  Detailed design drops it (decision D7). Reconcile design.md if it's re-read downstream.
- **[Unit 2] `by_label` is emitted into the plan but not consumed by `instantiatePrefab`.**
  Unit 1 wires only the address path (the character spike is address-only). Unit 2's
  `LoadAssetsAsync<GameObject>("characters")` host path must consume `addressables.by_label`.
- **[low-prob, deferred] guid6 suffix collision.** Two distinct prefab guids whose first 6
  hex chars match AND same base name → identical Templates child name → real on-disk
  collision + wrong-template clone. Slice 1.3 logs a WARNING on post-suffix collision among
  emitted templates (fail-loud); full resolution (widen to guid8/full guid on collision) is
  deferred — not load-bearing for the Cat spike (Cat/Raccoon guids differ in the first 6).
- **[prefab_id format parity] resolver vs planner separator.** Planner uses `.as_posix()`;
  resolver used `str(Path)` (OS-native). Slice 1.2 normalizes the resolver to `.as_posix()`
  so the two prefab_id producers are byte-identical (latent Windows-only mismatch otherwise).
- **[verify checkpoint, NOT a slice] TrackManager/Character/CharacterCollider domain.** If the
  regenerated generic output mis-routes any of these server-side, the spawned client collider
  never boots — that's a domain-classifier fix OUTSIDE Unit 1's boundary; scope it as its own
  phase, don't bolt it into a Slice 1.x.
## Carry-forward for slices 1.2/1.3 (from 1.1 implement)
- `select_emitted_prefab_ids` gained an optional `guid_index: GuidIndex | None = None` kwarg
  (PrefabLibrary carries no guid_index). The planner (1.2) + emitter (1.3) MUST pass
  `guid_index=` so the derived prefab_ids match `artifact["prefabs"]` keys; omitting it falls
  back to guid-less posix paths (won't match). `resolve_template_child_names` is collision-
  conditional over the EMITTED input set only.
## HARDEN targets (phase 1) — from slice 1.1 review (P2, non-blocking)
- _guid6_of (prefab_packages.py): harden against a leading path segment that is accidentally
  all-hex before a colon (e.g. drive letter / "face:") — only treat as guid if it matches a
  real Unity guid shape. Add a test for the path-with-colon case.
- Add a selector test for the duplicate-base referenced case: a bare ref to "character"
  selects BOTH Cat and Raccoon prefab_ids (pins D14 emitted-set behavior).
## HARDEN target (phase 1) — from slice 1.2 r2 (P2)
- AC14 parity tests: project_root=None leg uses a SimpleNamespace stub for the resolver and
  the no-guid leg skips the resolver. Use a REAL GuidIndex in every leg so the "3-way via real
  GuidIndex" claim is literal.
## HARDEN targets (phase 1) — from slice 1.3 (P2)
- D13 variant-parent WARN (prefab_packages.py:496): narrow `_colliding_variant_parents` to
  emitted variant CHILDREN only (currently warns even when variant_chains is {}).
- guid-less colliding animation bypass: now covered by the D12 colliding-base-membership fix.
- `_colliding_emitted_bare_bases` duplicates the emitter's collision count (dedupe/perf).
- manifest `addressable_referenced` can exceed `emitted_count` (cosmetic stat clarity).
## HARDEN target (phase 1) — from phase-integration review (P2)
- Stale comment in test_prefab_packages.py:1380-1389 ("xfail markers below / ids drift today")
  now contradicts the unified code (markers removed). Fix/remove the comment.

## slop (deferred to finalize)
converter/converter/prefab_packages.py:135-223 — helper docstrings read like pasted design-doc prose (design fact / D14 rationale blocks)
converter/converter/pipeline.py:653-660 — long process-history comment in implementation
converter/converter/pipeline.py:1081-1108 — long process-history comment in implementation
converter/converter/scene_runtime_planner.py — verify _prefab_stable_id docstring not overlong (harden audit area)
converter/tests/test_prefab_packages.py:1541-1740 — test docstrings mirror the spec/decision log verbatim

## Run addressables-unit2-20260615T193738 (2026-06-16) — Unit 2 followups

# Unit 2 — out-of-scope discoveries / follow-ups

- **Theme registration is dead (Unit 3).** `ThemeDatabase.Register` never called;
  themeData SOs carry `prefabList`/`collectiblePrefab`/`cloudPrefabs` as unresolved
  refs. The Unit-2 primitive is the dependency; the registration rewrite is Unit 3.

- **Consumable prototype materialization + CharacterDatabase roster (Unit 4).**
  by_label["characters"] roster load and Consumable serialized-field materialization
  are reserved for Unit 4. by_label is emitted in the plan but NOT consumed at runtime
  yet (runtime/scene_runtime.luau:2029-2030) — Unit 4 will wire it.

- **SO emitter drops ALL non-m_Script object refs to nil** (scriptable_object_converter.py
  _value_to_lua lines 82-87). Unit 2 fixes the prefab-ref subset; sprite/audio/material
  refs in SO data remain nil (acceptable fail-soft; revisit if a unit needs them).

- **Stripped prefab-instance component refs are a general gap, not just missionPopup.**
  The planner emits `target_kind="component"` for any scene ref to a stripped MB on a
  prefab instance whose owning GO/instance_id isn't registered. Trash Dash likely has
  more than the 2 missionPopup rows. The repro fix should be general (resolve stripped
  prefab-instance refs), not missionPopup-specific. Audit the full count of unresolvable
  `component`-kind rows at phase-design time.

- **Respawn re-clone rebind out of scope** (runtime/scene_runtime.luau:2507-2509 notes
  refs bound once at boot). Not a Unit-2 concern.

## From Unit 2 plan review
- **Sub-asset fileID disambiguation** (D3 limitation): a `{guid,fileID}` ref pointing INSIDE
  a prefab (not the root) resolves to the prefab root because the primitive ignores fileID.
  Out of Unit-2 scope; Units 3-4 must watch for this if any themeData/consumable ref is a
  sub-asset ref.
- **Other stripped prefab-instance component refs**: there are more `target_kind=component`
  unresolvable rows than just the two `missionPopup` rows; Phase 3's general fix should cover
  the class, but enumerate them during Phase 3 design to confirm coverage.

## slop (deferred to finalize)
converter/unity/prefab_ref.py:27 — review GuidIndexLike Protocol docstring/comment verbosity
converter/unity/prefab_ref.py:46 — review comment/docstring density
converter/unity/prefab_ref.py:69 — review comment/docstring density
converter/tests/test_prefab_ref.py:1 — review module-docstring/comment verbosity
converter/tests/test_prefab_ref.py:74 — review comment verbosity
converter/tests/test_prefab_ref.py:93 — review comment verbosity
converter/unity/prefab_ref.py — docstring says "never raises" but does not type-validate a malformed GuidIndexLike; scope the claim honestly (do NOT add defensive code — keep byte-identical to the original closure)
converter/tests/test_scene_runtime_planner.py:1483,1526 — stale `prefab_id_for` docstring mentions (nested closure renamed to module-level prefab_id_for_guid)

## From Unit 2 phase-1 harden (P2, non-blocking)
- converter/unity/addressables_resolver.py:155-158 — add a targeted by_label regression test for a label with mixed prefab+non-prefab guids and a label where nothing resolves (key disappears). Non-criterion; rewire is byte-identical so low risk.

## slop (deferred to finalize) — phase 2 harden
converter/tests/test_scriptable_object_converter.py:306 — over-explanatory helper docstring (internal narrative: "edge case 9b", "would pass for the wrong reason")
converter/tests/test_scriptable_object_converter.py:444 — arm-number/rhetorical test commentary ("takes the arm (:120)", "Genuinely...")
converter/tests/test_scriptable_object_converter.py:496 — "belt-and-suspenders" banner + author-machine-specific absolute path (~:501) in the real-project test

## Phase 3 follow-ups (out of scope; missionPopup repro IS fixed by Phase 3)
- Binary-scene stripped-MB resolution (no YAML → `stripped_components` stays empty → fail-soft).
- `Awake`-time reads of stripped refs still see nil (pre-placement); needs placement-before-scene-
  Awake boot reorder, a larger rework.
- Sub-asset fileID disambiguation beyond `m_CorrespondingSourceObject.fileID` (Unit-1 keys one id
  per `.prefab` file).
converter/unity/yaml_parser.py — soften the 'len(docs)==len(doc_headers)+1' pairing comment (overstated for bare-trailing-separator files; else-fallback covers it)
converter/runtime/scene_runtime.luau — pre-existing _inboundRefsToDeferred drain (~:2664) does not re-check cross-domain: a cross-domain ref to a UI-deferred SCENE-LOCAL target could rebind. Predates Unit 2; not a Phase-3 regression. Followup ticket.
converter/tests/test_scene_runtime_stripped_refs.py — add coverage for a SOURCE component that is itself UI-deferred (Phase-3 stripped refs have scene-boot sources, so currently out-of-class); + tidy imprecise test comment ~:629

## From Unit 2 phase-3 integration (codex P1 — generality, deferred)
- converter/scene_runtime_topology/cross_domain_edges.py — compute_cross_domain_edges() indexes only raw instance_ids; a cross-domain STRIPPED ref's placement-scoped target_ref (<placement_id>:<prefab_id>:<src_fid>) produces no cross-domain edge → no build-time RemoteEvent bridge. Extend it to recognize placement-scoped stripped refs (resolve target domain via the stamped target_script_id) so a cross-domain stripped ref gets a bridge. NOT a regression (pre-Phase-3 stripped refs were unresolvable → also no edge) and no such ref in Trash-Dash (all 3 are client→client); runtime fail-safes (nil + edge recorded). Out of Unit-2 scope.
- Add an automated Python-plan→Luau-runtime e2e for the stripped-ref path (planner output → runtime bind), complementing the Studio e2e.

## slop (deferred to finalize) — phase 3 harden
converter/tests/test_scene_runtime_stripped_refs.py:1 — module docstring/narrative verbosity
converter/tests/test_scene_runtime_stripped_refs.py:46 — review comment verbosity
converter/tests/test_scene_runtime_stripped_refs.py:184 — "EMPIRICAL ANSWER"/round-rationale narration in test
converter/tests/test_scene_runtime_stripped_refs.py:486 — duplicated codex-round rationale comment
converter/tests/test_scene_runtime_planner.py:1891 — review-narration comment
converter/tests/test_yaml_parser.py:311 — comment verbosity
converter/runtime/scene_runtime.luau (queue site ~:1300) — self-contradictory/over-explanatory comment at the pending-queue site

## From Unit 2 phase-3 harden (WARN-timing residual — reverted net-negative patch)
- converter/runtime/scene_runtime.luau — the residual "stripped ref never resolved" WARN has imperfect timing across MULTIPLE async UI-deferred host groups: it can fire PREMATURELY (a ref that a later group will bind) — that's the last-good behavior we kept. A harden attempt to gate it on a _deferredGroupsPending counter + a final drain was REVERTED because it introduced the opposite bug (WARN SUPPRESSED forever when the last group completes as an empty batch — codex). The WARN is DIAGNOSTIC-ONLY; binding is correct either way (cross-domain refs never queued; same-domain incl. missionPopup bind via the original post-placement + per-batch drains). Getting the WARN's exact multi-group async timing right is deferred (no real game exercises it: Trash-Dash's 3 stripped refs are client→client and their targets register). Fix when a real case appears.

<!-- ==== /drive run: checkmark-toggle-binding-20260617T075020 ==== -->
# Followups — checkmark-toggle-binding-20260617T075020

- **Structurally canonicalize the Toggle `isOn` writer attribute** (r2 attr-fingerprint residual). The binding reads
  the `"isOn"` attribute the AI-transpiled writer emits; on a future game whose transpile emits a different
  name/casing it would bind-but-never-update. Make the converter own the attribute on BOTH writer (lowering) and
  reader (binding) sides so `attr_name` stops being an AI-output fingerprint. Interim guards: A4 convention pin
  (RED at converter-build time) + the structural source. (D-4 keeps the AI writer untouched in this slice.)
- **Graphic-only re-clone with a surviving toggle does not re-bind** (E11 residual, OVERRULED-as-unreachable). The
  per-row `_boundRows` marker keys re-bind off the toggle instance; a checkmark graphic re-created while its toggle
  survives would keep the stale binding. UNREACHABLE in converter output (a converted checkmark is a child of the
  toggle's ScreenGui → re-clones with it). The `(toggle,graphic)`-pair marker was tried + reverted (it leaked the
  listener on the surviving toggle — net-negative). If a future binding kind decouples the endpoints, the proper
  fix is a pair marker that ALSO tracks+disconnects the per-row connection on re-bind.
- **PlayerGui one-time capture never recovers after a 10s WaitForChild timeout** (phase MINOR). The client host
  captures `PlayerGui` once; if that times out, the toggle watch (like every UI service that closes over the same
  `PlayerGui`) never installs for the session. Pre-existing shared host infra, not toggle-specific; pathological
  (>10s PlayerGui). Fix belongs in the host's PlayerGui acquisition, not this feature.
- **Slider→fill (health) visual binding** — DEFERRED; build as `ui_slider_bindings` reusing this mechanism.
- **Particle/transparency-based hide** (vs `.Visible`) — revisit only if e2e shows a graphic that must dim.

<!-- ==== /drive run: checkmark-toggle-binding-20260617T075020 (e2e-found addendum) ==== -->
## 2026-06-17 — e2e-found (dispatch)
- **Broader UI-widget dead-dispatch audit.** `_convert_ui_element` dispatches `Slider`/`InputField`/`Dropdown`/`ScrollRect`
  on LITERAL component_type names, but real Unity serializes these as `MonoBehaviour` (m_Script GUID) — so their property
  extraction is likely dead on real scenes too (same class as the Toggle bug fixed here). `Button` and now `Toggle` have
  MonoBehaviour heuristics; the rest need the same audit. (Slider is already deferred as `ui_slider_bindings`.)
- **`m_IsOn` Toggle discriminator precision.** Detection is `MonoBehaviour + m_IsOn` (mirrors Button `m_OnClick`). A
  non-Toggle MonoBehaviour with an `m_IsOn` field could false-positive, but harm is bounded (a spurious binding row needs
  ALSO a resolvable `graphic`; otherwise just a benign stray `ToggleIsOn` attribute). If a definitive m_Script-GUID→type
  resolution is added later, key the dispatch off it instead of the field heuristic.

## ── /drive run addressables-unit3-themes-20260617T080323 — Addressables Unit 3 (Themes) — 2026-06-17T04:00:11Z ──


## Plan-stage note (Unit 3 themes)
- Codex second opinion on the Register approach was attempted twice (medium + low effort) and BOTH hit a
  transient WSS/connection outage (chatgpt.com responses endpoint, os error 54). Treated as flakiness per
  prior sessions, not a DOWN. A Claude design subagent DID review and its two load-bearing findings are
  integrated (converter-owned register contract; no themeData/themeName hardcodes; foundation/consumer
  split). The drive-review DESIGN gate will supply the independent voice on this doc. Re-run codex on the
  design at the review gate when the endpoint recovers.

## General SO-addressable ownership/routing model (deferred from Unit 3)
- Unit 3 routes ONLY the theme database (label→owning-DB derived from the consuming load method, keyed by
  the DB's index field). Generalizing this derived routing to EVERY SO-addressable consumer
  (CharacterDatabase and future SO-loaded databases) is a larger, generic ownership model deferred out of
  this unit. Pick it up when a second SO-addressable consumer needs registry seeding. (D6.)

## Bootstrap-order / LoadDatabase-clobber as a generic seeding concern
- The converted `LoadDatabase` resets its store to `{}` then sets `m_Loaded=true`, and IS invoked
  (PlayerData.luau:191 / ShopUI.luau:30). The seed-vs-load ordering must be pinned per-DB so the seed
  isn't clobbered. If multiple SO-loaded DBs are seeded later, factor this ordering guarantee generically
  rather than per-DB. (P2; spiked at Phase 2 detailed design for the theme DB.)

## Planned guid-fallback abstain test (P3)
- Add a test: an SO lacking the identifying field -> the seed ABSTAINS (best-effort guid/stem fallback or
  skip), does NOT crash. Note the fallback entry is best-effort (consumer never retrieves it by that key),
  not a reachability guarantee.

## Design-review round-1 residuals (P2/P3 — Phase-2 detailed-design constraints, non-blocking)
- [P2] Isolatable-acceptance probe must read via the CONSUMER's lookup-key path (the field/path the
  converted consumer actually indexes the registry by), NOT echo the seed's own derived key — else the
  acceptance check is self-satisfying (asserts the seed wrote what the seed computed). Pin at Phase-2
  detailed design.
- [P3] (claude design reviewer) minor doc nit folded; codex flagged none beyond the deferred spike.

## Phase-1 detailed-design followups (AssetReference resolution)
- [FU-P1-a] Sub-asset `fileID`/`m_SubObjectName` disambiguation of AssetReferences is out of scope — one
  id per `.prefab`; a sub-object-name reference resolves to the parent prefab id (matcher tolerates
  `m_SubObjectName` but ignores its value). Matches Unit 2's documented scope. Not exercised by trash-dash
  (0 `m_SubObjectName` occurrences). Revisit only when a real game references a sub-asset within a `.prefab`.
- [FU-P1-b] The AssetReference key-set allowed by the matcher is `{m_AssetGUID, m_CachedAsset,
  m_SubObjectName}` (the full set Unity emits, pinned empirically). If a future game serializes an
  as-yet-unseen Unity AssetReference field, the subset matcher correctly declines it (falls through to the
  generic-dict branch). Re-enumerate the real key-set against that input before widening `_ASSETREF_KEYS`;
  do NOT widen speculatively.

## Slice 1.1 review P2s (codex, non-blocking → HARDEN targets)
- [P2] test_object_ref_arm_unaffected asserts only the prefab POSITIVE case; add an explicit
  regression assertion for the `nil --[[(Unity object reference)]]` nil-marker path (AC-6/AC-7).
- [P2] AC-1 prefabList list test proves SOME resolved string appears + AssetGUID/CachedAsset absent
  globally, but not that EVERY list element collapsed to a string (a mixed {"id", nil} would pass).
  Tighten to assert each element is a string.

## slop (deferred to finalize)
converter/converter/scriptable_object_converter.py:70 — verbose docstring (Phase-1 AssetReference branch)
converter/converter/scriptable_object_converter.py:76 — verbose predicate docstring/comment
converter/converter/scriptable_object_converter.py:120 — redundant inline comment
converter/tests/test_scriptable_object_converter.py:583 — verbose test docstring
converter/tests/test_scriptable_object_converter.py:614 — verbose test comment
converter/tests/test_scriptable_object_converter.py:813 — verbose test comment

## Phase-2 detailed-design followups (theme registration data-seeding)
- [FU-P2-a] Generalize the derived label→owning-DB routing to EVERY SO-addressable consumer (CharacterDatabase
  has the identical `LoadAssetsAsync<CharacterData>` shape + a `loaded()` gate). The `addressable_db_seeds`
  plan list + `SceneRuntime.seedAddressableDatabases` shim are already generic; deferred work is wiring the 2nd+
  DB through the same derivation. (D6 / design.md Out-of-scope.)
- [FU-P2-b] The single residual AI-name dependency is the write-surface ingress, now drain-bound + fail-loud
  (D16): a public appender is accepted only if its `table.insert` target == the field `LoadDatabase` drains,
  else the drain field is seeded directly, else the seed ABSTAINS + WARNS (AC-7). If a re-transpile emits a
  theme DB where nothing binds to the drain, loud abstain (registry empty, observable). Revisit only if a real
  re-transpile trips it; do not pre-harden.
- [FU-P2-c] The shim relies on the AI `LoadDatabase` body's `Add(op.keyField, op)` drain to index correctly. A
  future re-transpile that indexes `LoadDatabase` by a different key would key the registry wrong (detected only
  at runtime/AC-1). Consider a build-time assertion that the transpiled `LoadDatabase` indexes by the derived
  key field; deferred (not exercised by trash-dash).
- [FU-P2-d] Spike artifacts `/tmp/theme_seed_spike.luau` + `/tmp/theme_boot_order_spike.luau` validated the
  mechanism + ordering + abstain + loaded()-coupling (both PASS); not committed. Fold their assertions into the
  AC-5 host test at implement.
- [FU-P2-e] [IMPL-OPEN] Exact ownership-extraction surface in pipeline.py. RESOLVED sourcing (D17, resume-safe):
  owned label + key field from the DB's C# source re-read from `self.unity_project_path` (deterministic
  upstream, `ThemeDatabase.cs:32`/`:36-37`); DB-module identity + drain/appender bind from the transpiled body
  via `rbx_place.scripts[*].source` (rehydrated by ESSENTIAL `materialize_and_classify` on resume). The
  remaining impl choice is parser shape (regex span vs light AST) over those two sources; the DATA is derivable.

## Design-review round-2: P1 (drain-bind) + P2 (resume persistence) addressed (Phase-2 detailed design)
- [P1 RESOLVED — D16] Write-surface detector is now BOUND to the list `LoadDatabase` drains (`DRAIN_FIELD` from
  the C#-derived `LoadDatabase` body) and FAILS LOUD + abstains on a miss, instead of name-matching any
  appender. Cross-check: appender-target-field == LoadDatabase-drained-field == store-build field. Verified on
  real output by `/tmp/drain_link_probe.py` (not committed): `_pendingThemeData` on both sides, `Register`
  binds. New AC-7 (fail-loud) / AC-9 (mismatch-reject); edges 7/8.
- [P2 RESOLVED — D17] `addressable_db_seeds` derived in `write_output` after `_build_scriptable_object_module_map`
  and persisted on `scene_runtime` (rides `conversion_plan.json` + `ctx.scene_runtime` like the SO map). All
  inputs rehydrated by ESSENTIAL phases on resume; NOT gated on transient `transpilation_result`. New AC-10: a
  no-retranspile `--phase=write_output`/`assemble` resume still emits the seed.
- [P3 RESOLVED] C# citation corrected `LoadAssetsAsync` `:35`→`ThemeDatabase.cs:32`; once-only-guard wording
  tightened (the drain executes exactly once, guarded on `themeDataList == nil`).

## Slice 2.1 review residuals (P2/P3 — non-blocking → HARDEN/finalize candidates)
- [P3] (claude r1) unused-but-symmetric `guid_index` param on a resolver helper; no test for a
  non-default-container DB path.
- [P3] (claude r2) the literal-aware paren matcher in _derive_cs_load_ownership doesn't skip C# `//` or
  `/* */` comments; only effect is a safe early-abstain on an optional key field (fail-soft), non-blocking.

## slop (deferred to finalize) — phase 2 harden round 1
converter/converter/pipeline.py:155 — over-narrated comment
converter/converter/pipeline.py — triple-stated recompute comment
converter/converter/pipeline.py — _AMBIGUOUS_APPENDER sentinel over-abstraction
converter/converter/pipeline.py — seeded_db_paths guard the code itself calls impossible (redundant)
converter/converter/autogen.py:651 — over-narrated comment
converter/runtime/scene_runtime.luau — over-long rationale comments
converter/tests/test_theme_seed_plan.py:361 — over-narrated codex/pre-fix/post-fix comments

### Architectural follow-up (from finalize)
- pipeline.py (theme-seed DB discovery: _find_cs_source_for_module :7169, source_by_name :7241,
  _module_plan_path :7357) — DB discovery is NAME-HEURISTIC (keys off basename / script.name only), so
  duplicate module names or non-`<Module>.cs` project layouts can bind the wrong C# source, collapse
  distinct DBs, or seed the wrong runtime module path. Out of scope for this run (Unit 3 ships the
  theme-seed feature on the SimpleFPS/Trash-Dash corpus where names are unique); a durable fix anchors DB
  discovery on the artifact identity (guid / emitted-module path) rather than the basename. Generic-converter
  robustness follow-up.
## From Unit 4 phase-2 harden (addressables consumer re-lowering) — 2026-06-17
- **[P3] `roster_signal_absent` covers TOTAL stale but not PARTIAL stale.** `_roster_fail_closed` (contract_pipeline.py) fail-closes when a C# `Addressables.LoadAssetsAsync` loader exists but the `scene_runtime` carries NO `addressables` block at all (total stale artifact). It does NOT fire when the `addressables` block IS present but is MISSING the specific label a C# loader requests (partial stale) — `find_roster_consumers` then silently abstains for that loader (its label is not a `by_label` key) and the consumer ships un-relowered (empty loadout) with no fail-closed row. This mirrors the player-binding precedent (`player_signal_absent` is also total-only) and is pre-existing behavior, not introduced by Phase 2. Out of Phase-2 scope; do not expand the mechanism. Fix: extend the guard to diff the C#-requested labels (`csharp_label_loader_paths` already enumerates the loaders; would need per-loader label extraction) against `by_label` keys and emit `roster_signal_absent` (or a new `roster_label_absent`) for the gap.

## Run addressables-unit4-20260616T084409 (Addressables Unit 4 — roster) — 2026-06-17

# Follow-ups / out-of-scope discoveries — Addressables Unit 4 (roster)

- **Consumables prototype materialization** (the other half of design-doc Unit 4): resolve `Consumables.asset` refs to usable `Consumable` objects carrying `.gameObject` (prefab id) AND the serialized component fields (`canBeSpawned`, type, price, icon) read from the referenced prefab's `Consumable`-derived component. Consumers: `TrackManager.luau:557` (`.canBeSpawned`), `ShopItemList.luau:48` / `LoadoutState.luau:357` (`GetConsumableType`/`GetPrice`/icon). Separate effort.

- **`CharacterDatabase.luau` AI-output drift is a generic risk signal.** The transpiled consumer emitted 3 different roster-access shapes across outputs (WaitForChild folder / FindFirstChild folder / CollectionService:GetTagged). Any future load-bearing surface whose ACCESS the AI transpiler emits should be anchored on a deterministic upstream contract, not on the emitted string — consider a general roster/label-load re-lowering rule beyond `characters`.

- **`RbxPart` has no `tags` field** (`core/roblox_types.py:169`); CollectionService Tags currently ride a separate writer path from `m_TagString` (`rbxlx_writer.py:942`). If the roster emit needs to tag instances at the data-model level (not via that path), a `tags` field on `RbxPart` may be warranted — evaluate at detailed-design (keep it minimal; don't gold-plate).

- **LoadoutState accessories sub-roster** (`c.accessories`, `c.gameObject:Clone()` at `LoadoutState.luau:~315`): determine empirically whether the boot needs accessory rosters wired or whether a tagged character instance + `characterName` + clonable prefab unblocks it. May be a thin extension of this phase or a follow-on.

---
## New discoveries from dual-voice review revision — 2026-06-16

- **Generic roster/label-load re-lowering pass is broadly applicable beyond `characters`.** The consumer re-lowering built here (normalize an Addressables label-load consumer to read the deterministic tagged roster + wrap members in their component object graph) generalizes to any `Addressables.LoadAssetsAsync<T>(label)` consumer. Once Unit 4 lands, evaluate promoting it as a general contract_pipeline facet covering all labels, not just the boot-gating roster.

- **`RbxPlace` lacks a generic ReplicatedStorage tree** (`core/roblox_types.py`, only `replicated_templates` at ~498). Unit 4 adds a roster-specific channel; if more generic RS-children emit needs accrue (themes, consumables, other label rosters), consider a single generic RS-children data-model channel rather than per-feature channels.

- **Writer Tags path is single-tag from `m_TagString`** (`rbxlx_writer.py:942-948`) and there is no `tags` field on `RbxPart` (`core/roblox_types.py:169`). Unit 4 adds an explicit roster-tag path; a general multi-tag (null-delimited Tags list) capability on the data model is a candidate cleanup if more tagging needs arise.

- **Accessories sub-roster (`c.accessories`, `c.gameObject:Clone()` ~LoadoutState.luau:315/291/363):** if the Unit-4 spike shows accessories are dereferenced only AFTER boot (not on the boot path), wiring the per-character accessory rosters becomes a follow-on rather than part of this effort. Carry forward pending the spike result.

---
## Phase-1 detailed-design discoveries (post-spike) — 2026-06-16
- **Populated accessory sub-roster is a FOLLOW-ON (spike-confirmed).** The boot path reads `#c.accessories` (LoadoutState.luau:291) but only ENTERS the loop when `#c.accessories > 0`; `.accessories = {}` clears the boot crash. The CharacterAccessories object materialization (each member's `.accessoryName`/`.accessoryIcon`/`.gameObject`) is only exercised via `ChangeAccessory` (LoadoutState.luau:226) AFTER boot. NOT in Phase 1 (producer) NOR boot-blocking in Phase 2 (Phase 2's wrapper sets `.accessories = {}`); the populated sub-roster is a separate effort.
- **Existing single-tag `Tags` writer path is plain-text, not base64 NUL-delimited (rbxlx_writer.py:947).** Real Roblox `Tags` BinaryString is base64 of a NUL-delimited tag list. The roster-tag path emits the correct form; the EXISTING single-tag path is left untouched in Phase 1 but is a latent fidelity bug — evaluate a general `_emit_collection_tags` migration of the old path once the roster path proves the format in Studio.
- **luau_place_builder emits ZERO CollectionService tags (spike finding).** The headless publish path has no tag parity even for the existing single-tag path. Phase 1 adds `CollectionService:AddTag` for the roster; a general tag-parity pass for the existing single-tag `attributes["Tag"]` path on the headless builder is a candidate cleanup.
- **`_collect_character_names()` reads prefab Character-component config from `scene_runtime.prefabs[pid].instances[*].config`.** If more per-member component fields are needed later (icon, cost — cf. consumables), generalize to a `_collect_member_attributes(component_type, fields)` reader rather than per-field helpers.

---
## Phase-1 detailed-design REVISION follow-ups (review-phasedesign1-1.md) — 2026-06-16
- **Tags wire-encoding decision is a VERIFY-stage task.** Phase 1 locks ONE codec + ONE `name="Tags"` element via the merged `_emit_collection_tags` helper, but the exact wire bytes (plain text vs base64 NUL-delimited) are chosen empirically in the cold-Studio VERIFY boot — whichever Studio's loader actually `GetTagged`-discovers. Because there is one codec in one place, flipping the encoding later is a one-line change. (Was P2 / E8.)
- **Folding the existing single-`Tag` path into `_emit_collection_tags` now covers Unity-tagged non-roster parts too.** This Phase-1 merge (mandatory, P2) means the general `_emit_collection_tags` migration previously listed as a deferred cleanup is effectively done for the rbxlx writer; verify in VERIFY that pre-existing single-Tag parts still emit correctly under the merged codec, and that the headless builder's new single AddTag-site also covers the existing `attributes["Tag"]` parts (closes the prior "luau emits ZERO CollectionService tags" gap for non-roster parts too — confirm no regression).
- **`class_name`-based tiebreak in the characterName selector is best-effort.** `modules[script_id]["class_name"]` is populated best-effort (can be `""`, scene_runtime_planner.py:1083-1084). The selector tolerates this (field-presence is primary; first-in-lifecycle + warning is the final fallback), but if a game legitimately has multiple `characterName`-bearing instances per prefab, evaluate a stronger disambiguator (e.g. an explicit component-type tag on the planner row) as a follow-on rather than relying on lifecycle order.

---
## Phase-1 detailed-design REVISION 2 follow-ups (review-phasedesign1-2.md) — 2026-06-16
- **General referent re-key helper for ANY deep-copied RbxPart subtree.** Phase-1 adds a per-copy unity_file_id→referent remap for roster members; if other features later materialize a second copy of a template/prefab subtree in the rbxlx writer (themes, variant spawns, multi-instance placement), extract the re-key (fresh fids + intra-subtree connected_body_file_id rewrite + pre-pass walk) into a shared helper rather than duplicating it. Today only roster members need it.
- **Dedicated RS container is a candidate generalization point.** The roster container Folder (collision-checked name added to _reserved_rs_names on both writers) is the first non-Templates RS child the converter emits via the data model. If more RS-children channels accrue (D1 followup), fold the container-naming + reserved-name registration into the same generic RS-children emit path so each new channel automatically participates in the reserved-name dedup on BOTH writers.
- **`_RosterTag` marker-attribute convention.** Phase 1 uses an attributes["_RosterTag"] marker (root-only, stripped from AttributesSerialize) to drive root-only tagging through the merged tag helper. If future features need other root-vs-descendant emit distinctions on a copied subtree (e.g. a root-only Anchored override), consider a small typed "copy-emit directives" struct rather than a growing set of underscore-prefixed marker attributes.
- **Reserved-RS-name logic is duplicated across the two writers.** rbxlx_writer.py:1685-1701 and luau_place_builder.py:617-629 build the reserved-name set independently (and now BOTH must include the roster container name). A shared pure helper computing the reserved set + the disambiguated container name from an RbxPlace would remove the parity-drift risk between the two writers — candidate cleanup, not in Phase 1.

---
## Phase-1 detailed-design REVISION 3 follow-ups (review-phasedesign1-3.md) — 2026-06-16
- **SUPERSEDED — "Tags wire-encoding decision is a VERIFY-stage task" (REVISION-1 followup above).** No longer a VERIFY task. P1-D4 revision 3 SPECIFIES the canonical codec (base64 of the \0-joined dedup'd tag list) in Phase 1 and asserts it offline (AC4/AC11 decode the BinaryString). ONLY the live GetTagged boot remains a VERIFY confirmation (AC6). Disregard the earlier "plain vs base64 chosen empirically in VERIFY" item.
- **General `_emit_collection_tags` migration for non-roster Unity-tagged parts (now effectively done for rbxlx, confirm in VERIFY).** The Phase-1 fold makes `_emit_collection_tags` the sole name="Tags" writer for ALL parts (Unity-Tag + roster-label), lifted to the common tail so even Model-rooted Unity-tagged parts emit their tag (a pre-existing drop, now closed). VERIFY should confirm pre-existing single-Tag parts still emit correctly under the merged base64 codec, and that the headless builder's new single AddTag-site also covers existing attributes["Tag"] parts (closes the "luau emits ZERO CollectionService tags" gap for non-roster parts too — confirm no regression).
- **Studio-loader codec confirmation (VERIFY, narrowed).** The wire bytes are no longer an open choice; VERIFY only needs to confirm Studio's loader actually GetTagged-discovers the base64-NUL-encoded Tags on a Model-rooted member at cold boot. If (and only if) the loader rejects the canonical encoding, that is a single-helper one-line change — but the canonical Roblox codec is the locked default, not a hypothesis to be picked in VERIFY.

---
## Phase-2 detailed-design follow-ups (consumer re-lowering) — 2026-06-16
- **Generic `.gameObject` bind via prefab_id when characterName != template_name (D-P2-4 residual).** The Phase-2 canonical body binds c.gameObject to ReplicatedStorage.Templates:FindFirstChild(characterName), which assumes characterName == the template's child name (the prefab stem). Phase 1 permits characterName != template_name (characterName from the component field, template_name from the prefab stem keyed on prefab_id). A fully-generic bind would carry the member->template prefab_id relation through the roster surface (e.g. a member attribute) so the consumer resolves the exact Templates child regardless of name divergence. Not boot-blocking for Trash-Dash (characterName == template stem there). Verify in VERIFY; generalize if a game diverges.
- **Promote the roster re-lowering to ALL by_label labels, not just the boot-gating roster.** Phase 2 re-lowers any module that LoadAssetsAsync<...>(L) for L in by_label, but the canonical body returns a Character-shaped graph tuned to the boot consumer. A general label-load re-lowering facet (each label -> its component object graph) covers other Addressables.LoadAssetsAsync<T>(label) consumers; evaluate once Unit 4 lands (cf. the existing followup on the generic roster/label-load re-lowering rule).
- **Populated accessories sub-roster (still a follow-on).** Phase 2 sets .accessories = {} (spike: clears the boot crash). The CharacterAccessories object materialization (.accessoryName/.accessoryIcon/.gameObject per member), exercised only via ChangeAccessory AFTER boot, remains a separate effort (consumer reads LoadoutState.luau:342-343 / Character:SetupAcessory).

---
## Phase-2 detailed-design REVISION 2 follow-up (review-phasedesign2-2.md) — 2026-06-17
- **Reconcile post-boot character-object consumers with the re-lowered Character-wrapper return (D-P2-9).** The re-lowering changes GetCharacter/dictionary() from returning a raw tagged Instance to a `Character.new` WRAPPER TABLE (required+correct for the boot — LoadoutState derefs c.gameObject/c.accessories/c.characterName). Two POST-boot, NON-boot-critical consumers expect a raw Instance and DIVERGE (verified against converter/output/trash-dash-unit2-proper-20260616/scripts/ in the main repo):
  - TrackManager.luau:204→:209 (unit2-proper): `local charPrefab = CharacterDatabase.GetCharacter(charName)` then `self.host.instantiatePrefab(charPrefab, …)` passes the return DIRECTLY to a clone-an-Instance host call → wrapper table → player fails to spawn on Run.
  - ShopCharacterList.luau:39 + :46-49: `local dict = CharacterDatabase.dictionary()` then per value `c:GetAttribute("icon"/"characterName"/"cost"/"premiumCost")` → wrapper table has no :GetAttribute → error on shop open.
  - (ShopAccessoriesList.luau:55-56 reads `#c.accessories` off dict values → wrapper carries .accessories = {}, so it is incidentally SATISFIED — member skipped, no error.)
  Resolution: either re-lower those modules (instantiatePrefab(GetCharacter(...)) → resolve the wrapper's .gameObject/prefab_id; dictionary()-value :GetAttribute → read the wrapper's typed fields) OR define an Instance-compatible wrapper (e.g. a proxy carrying both the dotted props AND :GetAttribute / clonability). SHAPE-SPECIFIC: unit2-proper affected; unit1 TrackManager.luau:152 passes the NAME STRING (`_charName`) straight to instantiatePrefab and never calls GetCharacter → unaffected. Per the tier-problems rule this is downstream-conditional (post-boot), scoped out of the boot-only Phase 2 (AC10).

### D-P2-9 followup — enumeration accuracy notes (from phasedesign2 round-3 review)
When reconciling post-boot character-object consumers, the §1.6 enumeration must also reflect:
- `CharacterDatabase.loaded()` is consumed by `LoadoutState:Tick` (unit2:184 / unit1:158) as the boot-gate `interactable = ThemeDatabase.loaded() and CharacterDatabase.loaded()`. The re-lowered `loaded()` returns the pass-owned boolean (bool→bool unchanged) → SATISFIED; add it to the table for completeness.
- `ShopCharacterList` is SHAPE-SPECIFIC: unit2-proper uses `c:GetAttribute(...)` (table has no method → ERRORS); unit1 derefs dotted table fields `c.icon/.cost/.premiumCost` (Character.new defaults cost=0/premiumCost=0, .icon nil → DEGRADED, NON-crashing). The table currently conflates both under "errors on shop open" — split them. Both post-boot, both in this followup's scope.

## FU — TrackManager transpiles to an inert no-return stub; cascades into its requirers (converter quality, NOT Unit-4)
Observed in the Unit-4 e2e cold-boot (2026-06-17), but PRE-EXISTING + roster-unrelated (roster pass is identity-gated; TrackManager carries zero roster markers):
- TrackManager.cs transpiled to a body ending in `-- TODO: implement Update logic` + `print("TrackManager loaded")` with NO `return <classTable>` (return-count 0) → at runtime "module did not return a table". AI-transpile NON-DETERMINISM: TrackManager was LIVE (returned a table) in the pre-Unit-4 base conversion (reobs24) and inert here — two independent cold transpiles produced different bodies; the dead-module classifier then (correctly) flags the inert one dead.
- CASCADE: CharacterInputController (line 1) and CharacterCollider (line 2) both `require(TrackManager)` at the top, so TrackManager returning no table makes BOTH fail "Module code did not return exactly one value". One root (TrackManager), three console errors.
- Two follow-on angles: (a) transpile robustness — a complex MonoBehaviour (TrackManager) intermittently transpiles to a TODO-stub with no class-table return; (b) require-resilience — the emitted `require(X "..." ) or ServerStorage...` pattern does not guard against X loading to nil/non-table, so one inert module cascades into every requirer. Consider a fail-soft require wrapper or a post-transpile "module returns a class table" verifier.
- Also: TrackManager is the D-P2-9 scoped-out post-boot consumer; its failure here is the stub/no-return issue, NOT the wrapper-vs-Instance concern (which only bites with a live body).
