# Design — host-owned player-embodiment authority (`self.host.player`)

Status: REVISED after dual-voice review (Claude design + Codex adversarial), then REFINED by the
Step-1a (Gate-0) run's three dual-voice-converged decisions **D7/D8/D9** (propagated 2026-06-07 —
see §Refinements after Step-1a below; the inline sections are updated to match). Open decisions are
now RESOLVED (U1, pre/post camera, A-last, B-demoted, server-OWNED respawn). **Gate 0 / Phase 1 is
SHIPPED (PR ntornow#185 — build-time durability net only, `REQUIRE_PLAYER_BIND=0`).** Remaining work
is Phases 2-5 (the `self.host.player` product authority). Supersedes the player-binding *mechanism*
of PR #182 (keeps its identity + fail-closed foundation).

## Refinements after Step-1a (D7/D8/D9 — propagated 2026-06-07; these REFINE the sections below)
Three dual-voice-converged decisions from the Step-1a run reshaped this design. They are summarized
here and folded into the affected sections (§Architecture C, §Ordering, §Server, §Slicing, §Residual
risks) so the doc has no stale contradiction:

- **D7 — respawn is SERVER-OWNED; `teleport(cf)` is NON-load-bearing.** Respawn is ALREADY
  server/engine-owned and deterministic (autogen `GameServer` `CharacterAdded` spawn at
  autogen.py:105 + engine `SpawnLocation` re-spawn on death — independent of the AI shape). C's
  respawn responsibility narrows to a per-player **LIFECYCLE RESYNC**: re-acquire the new character
  on `CharacterAdded` and resync camera/yaw/eye-follow — C-owned, deterministic, with **NO dependency
  on the AI's `PivotTo` and NO dependency on paradigm B**. Programmatic mid-game `teleport(cf)` stays
  a client-request/server-apply host helper but is **explicitly non-load-bearing** (a fidelity
  nicety): if the AI never calls it, the goal still holds because respawn is server-owned. This
  removes the forbidden rung-2b dependency (correctness leaning on B teaching the AI to call
  `teleport`) that the original §Architecture respawn bullet implied.

- **D8 (revised) — Phase 2 proves C DOMINATES with paradigm A still ACTIVE; NO suppression.** Do NOT
  suppress player-path A. A is the mechanism that NEUTRALIZES the raw AI camera/move writes on A-hit
  shapes (the `dde248` raw output natively contains a direct `CurrentCamera` write + `humanoid:Move(`
  — test_movement_facet_lowering.py:153/181); suppressing A would EXPOSE those raw writes during
  Phases 2-3, before B's lexical rejects land in Phase 4, so C would coexist with the RAW AI writes
  rather than be single-authority. Phase 1 / Gate 0 proved C's pre/post camera writes + post-component
  `Humanoid:Move` dominate a competing mid-Update write by last-writer-wins. **Phase 2 adds C with A
  active and proves C structurally DOMINATES on BOTH the A-miss `cold3a59` and the A-hit `dde248`
  shapes** (final `CurrentCamera.CFrame` + the character's `Humanoid` move-intent are C's each frame),
  whether the competing write is A's lowered call or the raw AI call. This is NOT a single-writer
  claim — multiple writes, C wins by ordering (downstream of Gate 0, so no new unproven assumption).
  B stays non-load-bearing. **A's code is DELETED in Phase 5.** Dominance is scoped to
  `CurrentCamera.CFrame` + the character's `Humanoid` move-intent (raw rig `PivotTo` drift is
  vestigial until Phase 3's U1; recoil-on-A-hit is knowingly degraded until Phase 5 — a fidelity
  floor).

- **D9 — C owns the SINGLE per-frame E2E mouse-channel read via the PURE `advance()` helper, never
  `step`/`_readDelta`.** `SceneCameraInput._readDelta` CONSUMES the E2E-channel ACK (consume-once on
  workspace attrs). With A active driving the singleton's `step`→`_readDelta` in-band on A-hit shapes,
  exactly one of {A,C} consumes the injected delta/frame; if A wins the ACK race C's yaw advances by 0
  and the A-hit dominance fixture flakes on `pairs()` order. So C reuses the PURE `advance()` helper
  (scene_camera_input.luau:53-61), **NEVER** `step`/`_readDelta`, and owns the **single** per-frame
  channel read in its pre-Update input snapshot. (Gate-0 primitive (d), shipped in Phase 1, proves
  two readers in one frame consume the channel exactly once and C is the consumer.)

## The one goal
A converted Unity FPS plays correctly in GENERIC scene-runtime mode — first-person camera bound to
the character, the character moves (WASD camera-relative + jump), shooting raycasts the player's
view, respawn/teleport moves the player — **for ANY valid AI transpilation of the player
controller**, verifiable at build time, not only by a lucky e2e run.

## The invariant (the test for "is this the last time we fix it")
> Given a unique upstream player identity, **authoritative player embodiment is driven only by
> deterministic runtime code and explicit helper contracts — never by inspecting or rewriting
> emitted methods, and never depending on the *shape* of the emitted output for correctness.**

If a proposed fix reads the transpiled shape to make the binding WORK, it breaks on the next AI
shape. This fails PR #182 (its locators read the shape) and the entire prior lineage; only a runtime
authority passes. **Corollary (both reviewers):** the authority's correctness must NOT secretly
depend on the AI emitting a particular call (e.g. a starvable `GetMouseDelta`) — see §Ordering.

## Why (history: 4 fixes, all the same doomed paradigm)
Camera/player binding has been "fixed" 4×, all by **matching/rewriting the non-deterministic AI
output**: transpile-fidelity → legacy coherence pack → facet lowering + `SceneCameraInput` service →
PR #182 (fixed IDENTITY upstream, but kept fingerprint LOCATORS for the rewrite sites). The design
docs admit the verifier is structural-only with no behavioral oracle (`scene-runtime-contract.md`),
the only behavioral net is the e2e fixtures, and prompt-teaching is "a hint, not enforcement"
(`camera-input-fidelity-plan.md`). So binding "passed" only when the e2e ran on a cached/lucky
shape. PROOF: a COLD (uncached) re-transpile of SimpleFPS factored WASD into a `_getAxis` helper +
drove the rig via `PivotTo` + cached `GetMouseDelta` in `Update` → both PR #182 locators abstained →
fail-closed, NO bind (output/e2e/2026-06-05T23-45-34-cold3a59/conversion/scripts/Player.luau).

Three paradigms: A=match/rewrite (doomed), B=constrain via prompt+verifier (can't guarantee — "hint
not enforcement", one-shot reprompt then fail-closed), C=own at runtime (only one not reading the
shape). **Decision: C is the authority; B is a conflict-reducing backstop; A is deleted LAST.**

## Scope: it's player EMBODIMENT, not just camera+movement (Codex)
The player controller couples game logic to embodiment in ≥4 places, all of which re-decouple the
rig from the character if left to the AI: (1) **look** — writes `CurrentCamera.CFrame`; (2) **move**
— reads WASD + drives the rig/`Humanoid`; (3) **aim-read** — `Shoot` raycasts `self.cam.CFrame`;
(4) **respawn/teleport** — `TakeDamage`/respawn `PivotTo`s the rig. The unit of authority is **player
embodiment** (the rig↔character relationship), exposed as ONE host object `self.host.player`.

## Architecture

### C (load-bearing) — `self.host.player`, a host-owned per-player embodiment authority
For the upstream-identified player component, the host owns an embodiment object driven from `_tick`
**independent of the AI's emitted methods**. NOTE (Codex): this is **NOT a thin wrapper over the
existing `SceneCameraInput` singleton** — that singleton carries sticky `_rig/_followChar/_seeded`
state and a one-acquire lifetime that's the wrong model here. **Reuse** its vetted *math* (pose
composition `composeLook`, `advance`, recoil clamp, the E2E mouse channel, HRP eye-follow) but own a
**clean per-player lifecycle** (init on character spawn, resync on respawn, no leaked singleton
state). Responsibilities:
- **input snapshot** — read mouse delta (+E2E channel) + WASD ONCE per frame, BEFORE the component
  Update pass. **The E2E-channel read is via the PURE `advance()` helper, NEVER `step`/`_readDelta`
  (D9)** — C is the single per-frame consumer of the consume-once ACK; never let A win the ACK race.
- **camera pose** — world-yaw ∘ local-pitch, eye = character HRP + eyeHeight (the prompt's
  HRP+1.5 anti-bob rule). Written **TWICE** per frame (see §Ordering).
- **locomotion** — WASD → camera-yaw-relative → `Humanoid:Move`/jump on `LocalPlayer.Character`,
  AFTER the component passes.
- **read-only look** — `self.host.player:getLookCFrame()` for raycasts (Shoot).
- **respawn (lifecycle resync, D7)** — respawn itself is SERVER-OWNED (autogen `GameServer`
  `CharacterAdded` spawn + engine `SpawnLocation`). C's job narrows to per-player **lifecycle
  resync**: re-acquire the new character on `CharacterAdded` and resync camera/yaw/eye-follow — NO
  dependency on the AI's `PivotTo`, NO dependency on B. `self.host.player:teleport(cf)` remains a
  client-request/server-apply helper for programmatic mid-game teleports but is **explicitly
  non-load-bearing** (a fidelity nicety, not a correctness path).
- **boot** — default-controls-off + avatar-hide + Scriptable camera (folds `_ensureInit`), gated
  `RunService:IsClient()`.

### Ordering (the headline risk — failure-#5 vector; both reviewers)
`_tick` iterates components UNORDERED (scene_runtime.luau:2656) and the cold player runs
`Shoot→Rotate→Move` inside one `Update`. The prior design's Risk #1 is the in-band ordering
(yaw-before-move, recoil-before-raycast same frame). So:
- The host runs OUTSIDE the component loop: `readInput()` + **camera write** BEFORE the Update pass;
  `driveCamera()` (re-assert) + `driveLocomotion()` AFTER the LateUpdate pass. **TWO camera writes
  per frame** (pre + post), both idempotent (last-writer-wins).
- **Why pre-write (the fix both voices required):** `Shoot` reads raw `self.cam.CFrame` during the
  Update pass; a post-only host write leaves aim 1 frame stale. The pre-Update write makes
  same-frame raw reads correct. The post-LateUpdate write folds in this frame's locomotion-driven
  eye position.
- **This removes the `GetMouseDelta`-drain dependence from CORRECTNESS.** The host overwrites the
  camera both pre and post, so the AI's vestigial `Rotate` write is dominated **regardless** of
  whether the engine drains the AI's `GetMouseDelta` to ~0. The drain is a *nicety* (the AI's write
  is also a near-no-op if drained), not load-bearing — it must NOT be a correctness assumption
  (Gate 0 proves the multi-read semantics empirically before we lean on it for anything).
- **Recoil-before-raycast** is preserved ONLY if `Shoot` cooperates via `applyRecoil`/`getLookCFrame`
  (paradigm B). Without B: recoil is cosmetic-lost (acceptable; the shot still raycasts the correct
  pre-write look). State this as a known fidelity floor, not a blocker.

### Rig↔character unification — U1 (shadow-sync). RESOLVED: reject U2.
**U1 (chosen):** each frame, BEFORE the component Update pass, the host sets `rig.CFrame :=
character HRP CFrame`. The component still lives on the authored rig object, so all host identity
machinery is preserved: `GetComponent` (closed over the original `gameObjectId`/instance in `_meta`,
scene_runtime.luau:668/712/774), `_SceneRuntimeId` child lookup, active/registry maps,
`CameraRigFollower` (follows `CurrentCamera`, not the rig). The AI's `gameObject:GetPivot()` reads
see the character; its `PivotTo` writes are overwritten next frame (vestigial). **Caveat (Claude,
accepted):** the rig's *yaw* is no longer authoritative — but nothing authoritative reads it (the
host owns yaw via the camera and drives locomotion from camera-yaw, not rig-yaw; the AI's rig reads
are vestigial because the host does not consume them). The rig is a **positional shadow of the one
body (the character)**, NOT a second game-state store — so it is not the cross-script-state
anti-pattern.
**U2 (rejected, Codex):** rebinding `self.gameObject` to the character splits identity —
`self.gameObject` would point at the Roblox-spawned character while `GetComponent`/registries/
`_SceneRuntimeId` services still target the rig instance captured in `_meta`. Unifying *all* of those
is a large, hazardous blast radius for no gain over U1.

### B (backstop, NOT load-bearing) — keep the AI out of the way
Keyed on the upstream player identity (known at transpile time). Demoted per both reviewers:
- **per-script prompt directive**: "This script is the player controller. The host owns camera,
  movement, aim, and respawn via `self.host.player`. Do NOT write `workspace.CurrentCamera`, do NOT
  call `Humanoid:Move`. For aim use `self.host.player:getLookCFrame()`; for recoil `applyRecoil`; to
  teleport use `self.host.player:teleport(cf)`. Keep your game logic (shoot decision, ammo, pickups,
  pause)." (Requires NEW per-script prompt-context plumbing — the prompt is binary today.)
- **negative verifier rule (ONLY the lexically-decidable rejects)**: for the identified player
  script, reject a direct `workspace.CurrentCamera.CFrame =` write or a direct `Humanoid:Move(`
  call → one-shot reprompt. **DROP the "movement `PivotTo`" clause** — `PivotTo` is used for yaw,
  translate, AND respawn in the same script and is NOT lexically separable (both reviewers); the
  verifier is conservative-lexical, not semantic. (Requires wiring per-script identity into
  `verify_module`, which today takes only `source`.)
- B is a *conflict reducer + a cleaner-output nicety*, NEVER correctness. **C holds the binding even
  if the AI ignores B entirely** — because the body write-surface (camera pre/post, Humanoid on the
  real character, rig shadow-synced) is host-owned structurally.

### A (delete — LAST, after C dominates)
Delete `movement_facet_lowering` + the player path of `camera_facet_lowering` **in Phase 5**. **Only
after** U1 + aim-read + respawn land (Phases 2-3) and the cold-Studio checks pass (else Phase 2 would
ship the raw cold-shape `Shoot`/`TakeDamage` hazards with no fallback — both reviewers flagged the
original A-first ordering; D8 keeps A active until Phase 5).
**Non-player cameras (drone/turret rigs): KEEP the strict `camera_facet` path** — out of scope for
the player authority (resolved boundary, not an open question).

## Server / multiplayer boundary (RESOLVED — Codex)
`self.host.player` owns **client** embodiment: camera, local look, baseline locomotion (gated
`IsClient()`). The **server stays authoritative** for spawn/respawn/**teleport application** and shot
validation — the autogen `GameServer` already spawns on `CharacterAdded` and validates shot origin
near the character before the server raycast (autogen.py:104/216). **Respawn is server-OWNED and
deterministic** (the `CharacterAdded` spawn + engine `SpawnLocation` re-spawn fire independent of the
AI shape); C only does the per-player **lifecycle resync** on the new character (D7). So `teleport(cf)`
is a client **request**; the server applies it, and it is **non-load-bearing** — the goal holds even
if the AI never calls it, because respawn is server-owned. Do NOT specify teleport as purely
client-owned. No parallel server *movement* authority is built (matches Roblox defaults; documented
boundary, not gold-plated).

## Movement fidelity (RESOLVED)
Host-driven WASD→`Humanoid:Move` discards game-specific movement (dash/double-jump/var-speed). The
current `movement_facet` ALREADY discards it (KNOWN_ISSUES.md) — **no regression**; generic
locomotion is the accepted permanent generic-mode contract. A future opt-in
`self.host.player:setMoveIntent(vel)/jump()` hook could preserve more intent — a SEPARATE fidelity
layer, not this fix.

## Don't widen the CharacterController dual-map
`scene_runtime` maps CharacterController→BasePart; `TYPE_MAP`→Humanoid; `physics_bridge` is a third
path. `self.host.player` is the SINGLE authority for "the player's body"; do NOT add a 4th
per-instance `GetComponent` override. `self.control` reads (if any survive) route through the helper.

## PR #182 disposition
KEEP: upstream identity (`has_character_controller`) — the host needs it; the require-service fix;
the fail-closed *infrastructure* (re-purposed to "host couldn't find a character", which shouldn't
fire once C is authoritative). DELETE (in the final slice): the locators + their `player_move/look_
unbound` rows. #182 is the identity FOUNDATION, not the destination.

## Phasing — Gate 0 SHIPPED; Phases 2-5 remain (each verified on a FRESH cold-transpile in Studio)
The original 5-slice ordering (Gate 0 → Slices 1-4) is now expressed as Phases 1-5. **Gate 0 = Phase 1
is SHIPPED (PR ntornow#185)** — the build-time durability net only. Phases are SEQUENTIAL by dependency
(no worktree fan-out; each phase depends on the prior's structural fact). The §Slicing reorder both
reviewers required is preserved: C lands WITH A active (D8), A is deleted LAST.

- **Phase 1 / Gate 0 — prove the primitives. SHIPPED (PR #185).** Build-time harness/Studio proof of
  (a) same-frame multi-read `GetMouseDelta`; (b) pre-Update + post-LateUpdate camera-write semantics
  (pre-write survives to `Shoot`'s read; post-write wins the frame); (d) two readers in one frame
  consume the E2E channel exactly once and C is the consumer (D9). `REQUIRE_PLAYER_BIND` still 0. No
  product binding code rides on an unproven assumption.
- **Phase 2 — host authority, proving C DOMINATES with A still ACTIVE (D8).** `self.host.player`:
  input snapshot (pure `advance()` channel read, D9) + pre/post camera + host locomotion (WASD→`Humanoid:Move`
  **+ jump→`Humanoid.Jump`/`:ChangeState(Jumping)`**) + boot, from `_tick` OUTSIDE the component loop, keyed
  on the deterministic upstream `_HasCharacterController` identity carried into the runtime plan. **Prove C
  structurally dominates on BOTH the A-miss `cold3a59` AND the A-hit `dde248` shapes** (final
  `CurrentCamera.CFrame` + the character's `Humanoid` **move-intent AND jump-intent** are C's each frame) —
  A NOT suppressed, NOT deleted. **Jump is explicitly C-owned: add a build-time jump-dominance assertion to
  the shape corpus** so jump is never left implicitly riding the raw/legacy-A path that Phase 5 deletes (on
  A-hit `dde248` the AI natively emits `humanoid.Jump = true`). Cold-Studio: camera + WASD + jump on the
  cold shape. (Raw rig `PivotTo` drift vestigial until Phase 3; recoil-on-A-hit degraded until Phase 5.)
- **Phase 3 — U1 + aim-read + respawn lifecycle resync (D7).** Shadow-sync the rig to the character
  HRP; `getLookCFrame()` for `Shoot`; per-player **lifecycle resync** on `CharacterAdded` (re-acquire
  character + resync camera/yaw/eye-follow) — respawn itself stays server-owned; `teleport()` is the
  non-load-bearing helper. Cold-Studio: Shoot hits the crosshair; on death the CHARACTER respawns at
  the spawn point (server-owned) and the camera/yaw resyncs to the new character.
- **Phase 4 — B backstop (non-load-bearing).** Per-script prompt directive + the two lexical verifier
  rejects (direct `workspace.CurrentCamera.CFrame =` write / `Humanoid:Move(` call). Verify: a clean AI
  player script (no camera/move) AND a hand-broken script STILL bind (C dominates regardless of B).
- **Phase 5 — delete A + FLIP `REQUIRE_PLAYER_BIND` 0→1; retire dead tests; full cold e2e green.**
  Delete `movement_facet_lowering` + the player path of `camera_facet_lowering` and the #182 locators.
  Flip the acceptance gate (`verify_hook.py:35` + `test.yml:434` + `test_verify_hook.py` +
  `test_behavior_fixture_contract.py`) as the **LAST act, only after C binds on a FRESH cold Studio
  conversion**. Full cold e2e (turrets/doors/etc.) green.

## Verification discipline (the lesson this episode taught)
NEVER call this done off a cached assemble. Every slice: cold (uncached) transpile → real-mesh
conversion → Studio play → the camera/movement/shoot/respawn fixtures, on a FRESH AI shape.
Build-time signal = host-harness unit tests for `self.host.player` (deterministic) + the lexical
verifier rejects; e2e is the final net, not the only one.

## Residual risks (named, ranked)
1. **Frame ordering / same-frame aim** — THE failure-#5 vector. Mitigated by pre+post camera writes
   + Gate 0 proof. Must be Studio-verified uncached, not asserted.
2. **U1 yaw non-authority** — acceptable iff the host owns yaw+locomotion (it does); add a test that
   nothing authoritative reads rig yaw.
3. **B undecidability** — mitigated by demoting B to non-load-bearing + dropping the PivotTo clause.
4. **Respawn / teleport coupling** — respawn is server-OWNED + deterministic (D7); C only does the
   per-player lifecycle resync on `CharacterAdded`. `teleport(cf)` stays client-request/server-apply
   and is non-load-bearing, so no correctness path depends on the AI calling it or on B.

## Engineering-review additions (gstack plan-eng-review)

### Scope / minimal surface (Step-0 scope challenge)
The plan spans 5 files + a new authority + 2 deletions — over the 8-file/2-class "smell" line. Verdict:
the innovation token is WARRANTED (4 prior attempts in the cheaper paradigm all failed), BUT pin the
MINIMAL surface so this doesn't grow into a greenfield subsystem. **New code is exactly:** (1) a
WASD→`Humanoid:Move` driver, (2) the rig shadow-sync (U1), (3) pre/post camera *scheduling* in `_tick`,
(4) carrying the upstream identity into the runtime plan. **Everything else REUSES** `SceneCameraInput`'s
already-tested pose composition / recoil / E2E-channel / HRP-eye math (wrapped in a clean per-player
lifecycle, not the sticky singleton). If a slice starts adding camera-pose MATH, stop — that's a
regression into rebuilding what exists.

### Test strategy — the load-bearing addition (eng-review)
Build-time signal must catch the #1 failure mode (frame ordering) WITHOUT a Studio round-trip:
1. **Shape-variance regression corpus (NEW, mandatory).** Check in the TWO real `Player.luau` shapes
   that broke the locators — the cached `dde248` (helper `_axis` + extra-yaw camera) AND the cold
   `cold3a59` (helper `_getAxis` + rig-`PivotTo` move + `Update`-cached `GetMouseDelta`) — as host-harness
   fixtures, asserted to BIND under the authority (camera follows, WASD drives the Humanoid, **jump drives
   the Humanoid (C-owned jump-dominance, not the raw `humanoid.Jump`/legacy-A path)**, Shoot reads
   the live look). Every future "it broke on a new shape" adds its shape to the corpus. This is the
   regression guard the whole effort lacked (the e2e was the only net, run on lucky shapes).
2. **Pre+post camera-write assertion (host harness).** Assert the authority writes `CurrentCamera.CFrame`
   BEFORE the component pass AND re-asserts AFTER — so a regression that drops the pre-write (re-opening
   the stale-aim hole) fails a unit test, not just a cold-Studio aim check.
3. **`getLookCFrame()` stale-aim guard.** A test that Shoot's raw `self.cam.CFrame` read returns the
   SAME-frame look after the pre-write.
4. Cold-Studio e2e remains the FINAL net (camera/move/shoot/respawn on a FRESH AI shape), never the only one.

### Sequencing (strangler-fig — confirmed)
A-as-fallback (Phase 2) + delete-A-last (Phase 5) is the correct Fowler strangler-fig: C proves it
dominates on a cold shape with A still present (D8), then A is removed. Phases are SEQUENTIAL by
dependency (Phase 1/Gate 0 → 2 → 3 → 4 → 5); no parallelization (each phase depends on the prior's
structural fact). State it: no worktree fan-out for this effort.

### Failure modes (critical-gap audit)
- **Frame ordering** — silent if wrong, no existing test catches it. MITIGATED by Gate 0 (prove
  GetMouseDelta multi-read + pre/post camera) PRODUCING A REUSABLE host-harness test, + the pre+post
  assertion above. This is the one critical gap; it was made build-time-testable in Phase 1/Gate 0
  (SHIPPED, PR #185) before Phase 2's product code ships.
- **U1 rig-yaw non-authority** — add a test asserting nothing authoritative reads rig yaw.
- **Server teleport desync** — teleport is client-request/server-apply; test the round-trip.

## NOT in scope (explicitly deferred)
- Preserving game-specific movement (dash/double-jump/var-speed) — current code already discards it; an
  opt-in `setMoveIntent` hook is a separate future fidelity layer.
- Non-player cameras (drone/turret) — keep the existing strict `camera_facet` path; untouched.
- Server-authoritative MOVEMENT validation — no Roblox-default game has it; out of scope.
- A semantic (AST) verifier for "movement PivotTo" — undecidable lexically; B stays the two lexical
  rejects only.

## What already exists (reuse, don't rebuild)
- `SceneCameraInput` (runtime/scene_camera_input.luau): pose composition, recoil, E2E channel, HRP eye-
  follow, controls-off/avatar-hide, respawn-resync, `getLookCFrame`. REUSE the math; replace only the
  sticky-singleton lifetime with a per-player lifecycle.
- `has_character_controller` upstream identity (PR #182) + the autogen `GameServer` (CharacterAdded spawn,
  shot-origin validation). KEEP and consume.
- The `_tick` loop (scene_runtime.luau:2656). EXTEND with the pre/post host-player hooks.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Design (dual-voice) | Claude + Codex | Architecture adversarial | 1 | CONVERGED | 3 P1 fixed (pre/post camera, A-last, U-decision) |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 0 new P1; +regression corpus, +pre/post unit assertion, scope pinned |

- **CROSS-MODEL:** U1 vs U2 — Codex (U1, preserves GetComponent/registry closure) beat Claude (U2);
  folded in. "extend vs greenfield SceneCameraInput" — reconciled (reuse math, new lifetime).
- **UNRESOLVED:** none — all six original open questions are now resolved decisions.
- **VERDICT:** DESIGN CLEARED — durable iff the frame-ordering Gate 0 produces a reusable host-harness
  test before Phase 2. Gate 0 / Phase 1 SHIPPED (PR #185) with that net; Phases 2-5 implement off it.
- **POST-1a REFINEMENTS:** D7/D8/D9 (dual-voice-converged in the Step-1a run) propagated 2026-06-07 —
  see §Refinements after Step-1a. No new open questions; they tighten respawn ownership, A-coexistence
  proof, and the E2E channel read.
