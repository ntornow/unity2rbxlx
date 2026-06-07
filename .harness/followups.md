
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
