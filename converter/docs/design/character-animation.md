# Character / Skeletal Animation

**Status:** Planned 2026-05-17. Engineering plan reviewed (`/plan-eng-review`
+ Codex outside voice). Not yet implemented. Supersedes the "Animation system
completion" Phase 3-5 framing in [`../FUTURE_IMPROVEMENTS.md`](../FUTURE_IMPROVEMENTS.md).

---

## Problem

Transform animation works. Skeletal / character animation does not — and it
is not merely *unwired*, it is *unfinished*. Three pieces exist, none complete:

1. **`runtime/animator_runtime.luau`** — a full Unity Animator Controller
   runtime (state machine, parameters, transitions, 1D blend trees, plus
   `_playKeyframeAnimation` per-bone CFrame tweening). Injected into output
   when `HasAnimator` is set (`pipeline.py:3799`) but **never `require`d or
   instantiated** by any generated script. Dead code in every converted
   project. Internally inconsistent too: `_transitionTo` plays via
   `_lazyLoadTrack` (Roblox `AnimationTrack`, needs uploaded `Animation`
   assets the converter never produces), while `_playKeyframeAnimation`
   (the only real per-bone code) is defined but never called.

2. **`animation_converter.py:generate_state_machine_script`** — emits a
   self-contained per-controller Luau `Script`, but only tweens **one Part's
   `Position`** (lines 1707-1731). Not a skeletal animator. Gated on
   `humanoid_clips and has_transitions and len(states) >= 2`; no test project
   currently triggers it.

3. **`AnimationData_*` JSON modules** — controller graph + bone keyframes
   (`export_controller_json` / `export_clip_keyframes`). Emitted for humanoid
   clips, consumed by nothing.

What *does* work and is **out of scope to change**: transform-only clips via
`generate_tween_script` → inline `Anim_*` `TweenService` scripts.

---

## Architecture

One state-machine runtime. A per-character **backend** fork — native vs.
tween — chosen at conversion time. Unity's AnimatorController state machine
(states / transitions / parameters / blend trees) has no Roblox equivalent
and must be emulated regardless of backend; only *clip playback* differs.

```
Unity project
   │ parse
   ▼
AnimatorController + .anim clips + rig (SkinnedMeshRenderer / Avatar / bone names)
   │
   ▼
[detect_rig_backend]  per character ─► confidence score
   │  native        │  tween            │ low confidence
   │                │                   ▼
   │                │             ask author (/convert-unity prompt)
   ▼                ▼
KeyframeSequence    AnimationData_* JSON
+ Motor6D + Animator  (controller graph + bone keyframes)
   └───────┬────────┘
           ▼
   generated per-controller bootstrap Script
   require(CharacterAnimator).new(controllerData, rig); Heartbeat → :Update(dt)
           ▼
   CharacterAnimator  ── single state machine: states / transitions / params / blend trees
           │  plays each active state's clip via a backend-agnostic track handle:
           ├─ native backend → AnimationTrack:Play
           └─ tween backend  → keyframe-tween adapter (see PR2)
```

The fork is narrow **only if** the tween backend is wrapped in a track-like
adapter (see PR2, risk R2). Without that adapter the two backends are two
playback models and the state machine cannot stay backend-agnostic.

---

## PR sequencing

Strangler-fig. Each PR ships something testable. The per-character fork —
the user's requested end-state — lands **last**, after both backends it
routes between actually exist.

### PR1 — Rename + retire the redundant generator

- `runtime/animator_runtime.luau` → `runtime/character_animator.luau`;
  symbol `AnimatorRuntime` → `CharacterAnimator`; injection key in
  `pipeline.py:3796-3799`; doc references in `CLAUDE.md` and
  `docs/design/inline-over-runtime-wrappers.md`.
- Retire `generate_state_machine_script`; reroute its callers.

**Not a pure refactor.** `generate_state_machine_script` *does* emit for
controllers with humanoid clips + transitions + >= 2 states — the 9 fixtures
just never hit that branch. Removing it changes generated output for real
projects. Treat as a behavior change: add characterization tests pinning the
current output of that branch *before* deleting it.

### PR2 — Tween backend, end-to-end

The substantive PR. Makes the `CharacterAnimator` path actually run for one
rig. Work items:

- **Motion-key contract fix.** `export_controller_json` writes
  `state.motion = state.name`, but keyframes are keyed by clip *display name*
  (`animation_converter.py:1842, 2383`). `_transitionTo` calling
  `_playKeyframeAnimation(state.motion)` would miss whenever state name !=
  clip name. Align the contract: carry the clip key on the state.
- **Track-like adapter.** Wrap `_playKeyframeAnimation` in a handle exposing
  `:Stop()`, weight, and exit-time progress, so `_transitionTo`,
  `_checkTransitions` (exit-time gating reads `currentTrack`), and blend
  trees stay backend-agnostic.
- **Wire `_transitionTo`** to use the tween adapter when no `AnimationTrack`
  is available.
- **Generated per-controller bootstrap Script** — `require`s
  `CharacterAnimator`, instantiates with the `AnimationData_*` JSON + the
  rig, ticks `:Update(dt)` on `Heartbeat`.
- **Script-side integration.** Transpiled gameplay code does not call a
  runtime instance today — `api_mappings.py:224-235` maps `Animator.SetBool`
  → `:SetAttribute` and `SetTrigger/Play/CrossFade` → placeholder
  `AnimationTrack:Play()`. Decide and implement how user scripts reach the
  `CharacterAnimator` instance (attribute bridge vs. direct handle).
- **Bootstrap placement / scoping.** Animation scripts go into a flat list;
  prefab-scoped ones are copied onto templates (`pipeline.py:2264, 3369,
  3561`). The bootstrap must fit the scene-baked-vs-cloned-prefab topology or
  it will miss instances or double-drive them.

### PR3 — Native backend (`KeyframeSequence`)

Larger than first scoped. No IR or `rbxlx_writer` support for
`KeyframeSequence` / `Animation` assets exists; no animation upload / asset-id
plumbing; scene conversion never instantiates an `Animator` (`HasAnimator` is
only an attribute). Work: `.anim` → `KeyframeSequence` export, IR + writer
support, `Motor6D` + `Animator` rig instantiation (`SkinnedMeshRenderer` →
`Motor6D` conversion already exists), `Animator:LoadAnimation` →
`AnimationTrack` wiring.

### PR4 — Detector + per-character fork + author prompt

Last, once both backends are real. A `detect_rig_backend` step analyzes the
Unity rig (humanoid `Avatar`? standard / Mixamo bone names? → native; else
tween) with a confidence score; low confidence → interactive author prompt
via the `/convert-unity` skill. No `Avatar` / humanoid metadata parsing
exists today — PR4 adds it. Must apply identically on the `u2r.py convert`
and `convert_interactive.py` paths.

---

## Test strategy

Contract-level coverage, not just golden projects.

- **PR1:** characterization tests pinning `generate_state_machine_script`
  output before deletion; regression check that the 9 projects' output is
  otherwise unchanged.
- **PR2:** unit tests for the motion-key contract, the track adapter
  (`:Stop()` / exit-time / weight), `_transitionTo` fallback, bootstrap
  scoping (scene-baked vs. prefab-cloned); one non-standard-rig e2e fixture.
- **PR3:** `KeyframeSequence` XML round-trip tests; native-rig e2e fixture
  (candidate: Gamekit3D / Ellen — confirm it has usable `.anim` +
  `AnimatorController` data first).
- **PR4:** detector unit tests across rig shapes; the ambiguous → prompt
  path; `u2r.py` vs. `convert_interactive.py` parity.

Two new Unity test projects are required (non-standard rig, humanoid-mappable
rig) — no current fixture exercises skeletal animation.

---

## NOT in scope

- 2D blend trees, animation layers / avatar masks, root motion, IK — remain
  in `FUTURE_IMPROVEMENTS.md` Phase 5.
- Bulk-uploading animations as cloud assets — `KeyframeSequence` stays
  embedded in the rbxlx unless PR3 surfaces a concrete reason.

---

## Open risks

- **R1 — no skeletal test coverage exists.** Mitigated by the two new
  fixtures; until they land, every claim about skeletal output is unverified.
- **R2 — backend divergence.** If the tween backend is not given a real
  track-like adapter, the "one runtime" claim collapses into two playback
  models with different state semantics. PR2 must deliver the adapter.
- **R3 — PR3 scope.** The native backend touches the IR, the writer, and
  asset plumbing; it may itself need to split across more than one PR.
