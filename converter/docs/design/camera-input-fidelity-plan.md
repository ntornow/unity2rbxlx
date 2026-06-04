# Camera/input fidelity plan — generic-mode first-person, by construction

## The one goal
Make converted Unity first-person games behave correctly **in generic
scene-runtime mode** (the path meant to replace legacy) — concretely, the
camera must yaw *and* pitch — **without** an ad-hoc coherence pack, by giving
the first-person camera/input cluster a deterministic home: a small
scope-capped **host-runtime camera/input service** reached by a deterministic
**camera-facet lowering pass** on the generic allowlist.

## Why this shape (condensed; full reasoning in the session + design doc)
- The bug (FPS can pitch, not yaw) is a **semantic-fidelity** gap: the
  transpiled controller rebuilds `CurrentCamera.CFrame` pitch-only (yaw=0).
  Roblox's camera is a **singleton** not in the workspace tree, so the Unity
  camera-child-of-player yaw inheritance can't be expressed as nesting and is
  re-derived (wrongly) in emitted code.
- The generic verifier is **structural only** — no behavioral oracle — so this
  bug is structurally invisible; "compliance by construction" cannot catch it.
- Generic deliberately runs **no coherence packs** (`run_packs`). But it *does*
  run a permanent **deterministic lowering layer** (asset-rewrite,
  require-resolution). The boundary is *ad-hoc identity-gated repair* (a pack)
  vs *deterministic spec-owned lowering* (allowlisted) — **not** "touches
  emitted Luau." (Design doc updated to record this.)
- Therefore the fix is a deterministic, generic (structural-fingerprint-gated,
  never per-game) lowering pass that routes the camera facet to a hand-written
  runtime service — the same category as the existing allowlist passes, not a
  coherence pack. Reviewed by Claude (3 threads) + Codex (3 rounds); converged.

## Scope cap (load-bearing — do NOT cross)
The runtime service owns ONLY: camera pose composition (world-yaw ∘
local-pitch), pitch clamp, recoil/pitch API, default-controls-off + body-hide
+ spawn-snap, rig/viewmodel slot anchoring, the E2E mouse channel, and
**read-only** `CurrentCamera.CFrame` for game logic. It must **not** own WASD
translation, weapon logic, CharacterController emulation, or shooting. "Once it
owns locomotion, you're rebuilding Unity." Clean line: the service owns
**turning** (look yaw/pitch, and yawing the player rig so the body faces the
view); the controller keeps **translation** (WASD), reading the service's yaw
basis.

## Components

### C1 — Camera/input runtime service (`runtime/`, hand-written Luau)
A deterministic module the host wires on the client. Responsibilities per the
scope cap. **Frozen controller↔service API + ordering contract (pinned in
Slice A, before either side is built):**
- `service:configure({sensitivity, minPitch, maxPitch})`
- `service:step(dt)` — reads mouse delta (+E2E channel), updates yaw+pitch,
  yaws the player rig, composes `CurrentCamera.CFrame`.
- `service:applyRecoil(deltaPitch)`  ← recoil from Shoot
- `service:getYawBasis()` / `service:getLookCFrame()`  ← movement basis / raycast
- `service:onRespawn(cframe)` / `snapToRig()`  ← TakeDamage teleports the rig;
  the service must be told or yaw-state desyncs from the rig after respawn.
- Production-safe E2E channel (no-op when unset).

**Ordering contract (the headline risk — see Risks).** `step(dt)` is called
**in-band, where `Rotate` ran** in the controller's `Update`, so the existing
per-frame order is preserved: `Shoot` (recoil → `applyRecoil`) → `step` (yaw
applied to rig) → `Move` (reads `getYawBasis()`/the rig pivot). The service
must NOT run a separate `RenderStepped`/`Heartbeat` loop that races the
controller's `Update` (`SceneRuntime:_tick`) — that reintroduces one-frame
yaw/move skew. Any RenderStepped smoothing for *rendering only* must not be
the authoritative yaw source. The existing `CameraRigFollower` (rig→camera
pivot, RenderStepped) stays for rig mirroring; authoritative look state is set
in-band by `step`. Note: `CameraType = Scriptable` is set by the bootstrap
(`pipeline.py:3594`), not the controller — the service/splice must not assume
it owns that.

### C2 — Camera-facet lowering pass (generic allowlist, `contract_pipeline.py`)
Deterministic, structural-fingerprint-gated (NOT `s.name`). Method-scoped
recognize-and-splice (Codex's third seam — not whole-class replace, not a bare
host API the AI may misuse):
- Detect the camera-controller facet: binds `workspace.CurrentCamera` + the
  yaw-only `PivotTo` body turn + the pitch-only `CurrentCamera.CFrame` rebuild
  + pitch-state field.
- **Exact cut points (the controller is NOT left "untouched" — earlier draft
  was self-contradictory):**
  - `Rotate` look-math (yaw `PivotTo` + the pitch-only camera CFrame write +
    the mouse-delta pitch accumulation/clamp) → replace with `self._cam:step(dt)`.
  - `Shoot`'s recoil write (`self.camRotationX = self.camRotationX - 2`) →
    `self._cam:applyRecoil(...)`. So `Shoot` IS touched, at that one line.
  - **KEEP** the `Awake` `self.cam = workspace.CurrentCamera` bind as a
    read-only alias — `Shoot`'s raycast reads `self.cam.CFrame` and
    `weaponSlot = self.cam:GetChildren()[1]` resolves from it. Only the
    controller-side camera *writes* are harmful; the read alias stays. (The
    `weaponSlot`/camera-child resolution is a separate object-ref lowering
    problem — `unity_instantiate`, FUTURE_IMPROVEMENTS — not this pass.)
  - Leave `Move`, the raycast, ammo, events otherwise intact; `Move` reads
    `self._cam:getYawBasis()` (or the rig pivot the service yawed).
- Idempotent; twice-call + fires-on-real-shape + negative + **a
  movement-facing test** (WASD basis still tracks look yaw after the splice).

## Slices
Slices **A–D land in PR5** (the camera/input service is a prerequisite of the
PR5→PR7 "play correctly" gate — see scene-runtime-contract.md). **E is PR8**
(retirement only).
- **A — Doc + decisions + frozen contract (this).** Design-doc edits (done:
  lowering-layer principle + PR5/PR8 timing resolution) + this plan +
  legacy-pack decision + scope-cap sign-off **+ FREEZE the controller↔service
  API and the in-band ordering contract and the `onRespawn` hook** (pulled
  forward from B — C can't be designed without the API frozen). **Gate A:
  Codex + Claude plan review, then user.**
- **B — Runtime service (C1).** Build against the frozen API + host-side tests
  (pose composition, clamp, recoil, respawn, E2E channel, in-band `step`).
- **C — Lowering pass (C2).** Structural detection + method-scoped splice at
  the exact cut points + unit tests (incl. the movement-facing test); wire into
  the generic allowlist in `contract_pipeline.py` (the allowlist orchestrator —
  NOT `contract_verifier.py`).
- **D — Integration + verify.** Generic SimpleFPS routes through the service;
  `/e2e-test SimpleFPS --generic` drives `mouse_yaw_rotates_camera` +
  `mouse_pitch_rotates_camera` and they PASS. Cache-based assemble — no API
  auth needed (verified workable this session). Update memory
  [[converted-fps-camera-yaw-lost]].
- **E — Retirement (= PR8 scope, separate effort).** Retire the **four** FPS
  packs (`fps_camera_yaw_from_player_pivot`, `fps_camera_pitch_inversion`,
  `fps_default_controls_off`, `fps_e2e_mouse_channel`) + `converter/scaffolding/`
  + `detect_fps_game`/`is_fps_game` — their jobs already live in the PR5
  service; rewrite skill 4a/4c as plan overrides.

This effort = **A–D** (fix generic properly + the service + the pass). **E** is
the larger roadmap retirement, gated on A–D proving the seam on SimpleFPS.

## Decision: the committed legacy pack
`fps_camera_yaw_from_player_pivot` (branch `fix/fps-camera-yaw`, commits
`ef62ed7`+`1c1be36`) is a **legacy-mode** coherence pack — generic-gated by
structural fingerprint (the good kind), but in the `run_packs` layer.
- **Recommendation: keep it for legacy** (legacy is the default until PR7 and
  the post-PR7 escape hatch; it's the only thing fixing yaw for users *today*),
  **but fix its stale docstring** — the "transpiler flattens the hierarchy"
  claim is wrong (Codex-flagged); the cause is the singleton-camera re-derivation.
  Mark it explicitly "legacy-only; superseded in generic by the camera/input
  service (Slice E retires it)."
- Alternative: drop it now and accept legacy yaw stays broken until retirement.

## Verification & risks (ranked)
- **Oracle:** the e2e gameplay fixtures are the behavioral net (no build-time
  behavioral check exists by design). `mouse_yaw` / `mouse_pitch` are the gates.
- **Auth:** the lowering pass + service are cache-/auth-neutral (deterministic,
  post-transpile + hand-written runtime); cache-based `assemble` builds + e2e
  verifies without the (currently 403) cold-transpile API. Only prompt-teaching
  would need auth — and we are deliberately not relying on prompt for this.
- **Risk #1 — frame-order / temporal coupling (the headline technical risk).**
  Today yaw is applied in `Rotate` *before* `Move` reads the pivot, *in the same
  Update frame*, and recoil is applied *before* the raycast. A service on its
  own loop would race that. Mitigation: the in-band `step(dt)` ordering contract
  (C1) + a movement-facing test (C) + a recoil-before-raycast test. This
  outranks detection generality — get the ordering contract right first.
- **Risk #2 — seam creep:** the service must not grow into locomotion/weapons.
  Guard: the scope cap + a test asserting the service module's public API
  surface stays within the capped list.
- **Risk #3 — detection generality:** the lowering pass must fire across
  child-camera FPS shapes without false-positives on non-FPS scripts; gate on
  the multi-signal structural fingerprint, lexer-blanked, never `s.name`.
