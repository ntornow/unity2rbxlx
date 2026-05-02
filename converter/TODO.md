# Converter TODO

Active work items only. Completed work + PR execution logs live in `TODO_archive.md`.

Priority: **P0** = blocks gameplay, **P1** = significant quality, **P2** = nice to have.

---

## Pipeline / runtime gaps

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
- [ ] **P1 — Attach prefab-scoped animation scripts under
  `ReplicatedStorage.Templates.<Prefab>` (Phase 5.9 deep follow-up).**
  The current 5.9 emission renames `Anim_<Prefab>_<Ctrl>_<Clip>` so the
  script names dedupe across scene instances, but `write_output()` still
  parents every generated animation script in a global container, so
  cloning the prefab from `Templates` doesn't carry the animation
  driver. Real fix: emit the script as a child of the corresponding
  `RbxPart` template in `prefab_packages` (or thread a `parent_path`
  attribute through `storage_classifier`). Codex final-pass [P1].

## Animation correctness gaps

- [ ] **P1 — Same-name AnimatorController collisions in
  `convert_animations`.** `scenes_per_controller`, `prefabs_per_controller`,
  `result.routing`, and the script-name format `Anim_{prefix}{ctrl.name}_…`
  all key on `AnimatorController.name`. Two distinct .controller files with
  the same internal name (common in projects where each prefab ships its
  own "AnimController") collapse into one bucket: scope/filter decisions,
  routing entries, and emitted script names collide and overwrite each
  other. Fix: key the maps by controller GUID (or source path), and
  disambiguate the script name with a stable suffix when names repeat.
  Add a regression test that defines two same-named controllers in
  separate prefabs and asserts both emit independent scripts.
- [ ] **P1 — Same-name AnimationClip collisions in `keyframes` export.**
  In `convert_animations`, the per-controller `keyframes` dict is keyed
  on `clip.name`: `keyframes = { clip.name: export_clip_keyframes(clip)
  for clip in humanoid_clips }`. If a controller references two clips
  with the same name (separate `clip_guid`s, identical user-given names),
  the dict-comprehension silently keeps the last one and the runtime
  state machine plays the wrong asset. Fix: detect the collision, log
  it as UNCONVERTED, and key by something stable (clip GUID, or a
  disambiguated suffix). Add a regression test mirroring the controller
  same-name case.

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
