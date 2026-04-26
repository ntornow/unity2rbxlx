# Converter TODO

Active work items only. Completed work + PR execution logs live in `TODO_archive.md`.

Priority: **P0** = blocks gameplay, **P1** = significant quality, **P2** = nice to have.

---

## Pipeline / runtime gaps

- [ ] **P1 — Binary animation/controller parsing.** `.anim` and `.controller`
  files are skipped when binary-encoded. Affects ~40% of games with skeletal
  animation. Needs UnityPy integration or a binary YAML parser.
- [ ] **P2 — Persistent prefab/asset cache.** Prefab library is in-memory only.
  SQLite or pickle cache keyed by `(GUID, mtime)` would halve pipeline time
  for multi-scene projects and large games.

## Cross-script transpilation

- [ ] **P2 — 4.3.2 C# pattern warnings.** Skipped in PR 4. Pre-flight
  diagnostics for LINQ / networking / async patterns. Revisit when a project
  ships a class of error that warrants pre-flight surfacing.
- [ ] **P2 — 4.3.4 `_classify_script_type` harmonization.** Dest defaults to
  `Script`; source defaulted to `ModuleScript`. Revisit if a project ships
  cross-classified scripts that need source's behavior.

## Materials & meshes

- [ ] **P2 — Sub-mesh identity in vertex-color baking.** PR 3 deferred. FBX
  files with multiple embedded meshes currently rasterize the whole file
  instead of the specific sub-mesh — `mesh_file_id` is not yet preserved
  through `bake_vertex_colors_batch`'s signature.
- [ ] **P2 — Full SurfaceAppearance round-trip through templates.** PR 5
  deferred. The smoke ran with `--no-upload` so real asset IDs never wired
  through `ReplicatedStorage.Templates`. Verify on a full upload run.
- [ ] **P2 — Per-prefab variant-chain preservation in templates.** PR 5
  currently emits the flattened resolved form; variant-chain reapplication
  at runtime is not preserved.

## Animation routing

- [ ] **P2 — Prefab-scoped animator controller GUID aggregation.** PR 2a
  deferred. Scenes that only reach controllers through prefab instances have
  an empty `referenced_animator_controller_guids` set, so scene-scoped naming
  never activates for them. Add equivalent aggregation on `PrefabTemplate`
  and union into the scene set. Unscoped fallback keeps existing projects
  working.
- [ ] **P2 — Transform-only prefab scanning.** PR 2a deferred. One tween
  script per prefab animator (not just per scene). Revisit alongside the
  prefab-animator aggregation above.

## UI

- [ ] **P2 — TMP alignment.** PR 1 deferred. `m_HorizontalAlignment` and
  `m_VerticalAlignment` bitfields on TextMeshPro components aren't split
  into `text_x_alignment` / `text_y_alignment` yet; only legacy `m_Alignment`
  (single 0..8 enum) is handled. Revisit if a test project exercises
  TMP-only text layout issues.

## Infrastructure

- [ ] **P2 — Three-flow `rbx_place` byte-equivalence test.** Codex review
  P1-6 deferred. Source-level + behavior-level parity is now tested
  (commit `420b01e`). Byte-equivalence requires a real Unity fixture.
- [ ] **P2 — Standalone `.rbxm` file output per prefab.** PR 5 deferred.
  Toolbox convenience; no runtime dependency on this format.

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
