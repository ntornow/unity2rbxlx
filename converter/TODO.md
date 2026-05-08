# Converter TODO

Active work items only. Completed work + PR execution logs live in `TODO_archive.md`.

Priority: **P0** = blocks gameplay, **P1** = significant quality, **P2** = nice to have.

---

## Pipeline / runtime gaps

- [ ] **P1 — Remove FPS-specific logic from the converter
  (`fps_client_generator.py`).** PR #66 generalized
  `script_coherence_packs.py` (the producer/consumer BindableEvent bridge
  and the Pickup RemoteEvent canonicalization) but
  `fps_client_generator.py` is still bolted into the pipeline as the only
  remaining genre-specific module. It auto-emits an FPS HUD ScreenGui, a
  WASD + mouse-look controller LocalScript, a HudControl LocalScript, and
  the `connectClient(evt, handler)` BindableEvent/RemoteEvent dispatch
  helper — all triggered when the converter detects an FPS-style
  controller pattern. Net effect: any non-FPS project that happens to
  match the detection heuristic gets unwanted UI / input scripts injected,
  and the converter carries genre-specific scaffolding instead of staying
  generic.

  Refactor goals:
  - Extract the genuinely-generic helpers (e.g. `connectClient` event
    dispatch) into a generic runtime/library module, since they apply
    regardless of game genre.
  - Replace the auto-emit-on-detection behavior with an explicit opt-in:
    either a per-project flag in `conversion_plan.json` (the user's
    `/convert-unity` skill review can flip it for FPS projects) or move
    the FPS-specific emitters into a project-scoped scaffolding folder
    that ships only when the user requests it.
  - Delete the FPS detection heuristic; the converter should default to
    no game-genre assumptions and only inject controllers/UI when the
    Unity scene already has the matching components (e.g. emit a
    Cinemachine bridge only when the scene actually has a
    CinemachineVirtualCamera).

  Acceptance test: convert a non-FPS project (Gamekit3D, BoatAttack,
  ChopChop, RedRunner) and confirm none of `fps_client_generator.py`'s
  outputs (HUD, controller, HudControl) are emitted unless explicitly
  opted in.

- [ ] **P2 — Persistent prefab/asset cache.** Prefab library is in-memory only.
  SQLite or pickle cache keyed by `(GUID, mtime)` would halve pipeline time
  for multi-scene projects and large games.

## Materials & meshes

- [ ] **P2 — Full SurfaceAppearance round-trip through templates.** PR 5
  deferred. The smoke ran with `--no-upload` so real asset IDs never wired
  through `ReplicatedStorage.Templates`. Verify on a full upload run.
## Infrastructure

- [ ] **P2 — Three-flow byte-equivalence: u2r.py vs convert_interactive.py
  divergence (Phase 5.1 follow-up).** The byte-equivalence test landed
  with `test_three_flows_produce_identical_rbxlx` xfailed because the
  in-memory u2r.py path inlines scripts via `_convert_prefab_node` while
  the cross-process interactive path goes through `rehydration_plan.py`,
  producing different sets of Script Items. Harmonize the two paths so
  the test flips from xfail to xpass.
- [ ] **P2 — Standalone `.rbxm` file output per prefab.** PR 5 deferred.
  Toolbox convenience; no runtime dependency on this format.
- [ ] **P2 — Visual-compare baseline screenshot (Phase 5.4 follow-up).**
  CI step is wired, gated on `eval_baseline_screenshots/SimpleFPS_main.png`
  existing. Commit a known-good baseline from the next clean smoke run
  to activate the SSIM 0.85 gate; until then the step warns and continues.
- [ ] **P2 — Real-upload smoke secrets (Phase 5.2b / 5.3 follow-up).**
  CI jobs `real-upload-smoke` and `ai-convert-matrix` skip cleanly until
  the repo secrets `ROBLOX_API_KEY`, `ROBLOX_UNIVERSE_ID`, `ROBLOX_PLACE_ID`,
  and `ANTHROPIC_API_KEY` are configured. Wire them when CI billing allows.
## Type-strictness debt (forward-only gate landed; cleanup separate)

The no-Any gate prevents new smuggling. Existing-offender cleanup has
landed in dedicated PRs (#10 gate, storage_plan, ported-module signatures
PR #34, PipelineState PR #36, trivial 3-fix + ConversionContext final 4).

Remaining items:

- [ ] **P2 — `scene_converter.py:180` `_mesh_hierarchies: dict[str, list[dict]]`.**
  Module-level cache; the bare `dict` is missing-type-arg, not Any.
  Tighten to `dict[str, list[MeshHierarchyEntry]]` for consistency
  with `ConversionContext.mesh_hierarchies` (the TypedDict is in
  `core/conversion_context.py`). Not flagged by the no-Any gate;
  cleanup-only.

---

For platform limitations, Unity features with no Roblox equivalent, and Open
Cloud API limits, see [`docs/UNSUPPORTED.md`](docs/UNSUPPORTED.md). For
architectural debt and bug-shaped gaps, see [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md).
For long-horizon strategic work, see [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md).
