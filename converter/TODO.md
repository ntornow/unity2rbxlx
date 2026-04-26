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

- [ ] **P1 — Cross-script shared-state linter.** Validation of PR 4's
  dependency-aware context showed the AI transpiler still emits
  `character:GetAttribute("hasKey")` on reader scripts even when the writer
  exports a getter. Two prompt iterations failed to close it. Fix belongs in a
  post-transpile linter that walks generated `.luau`, finds `:GetAttribute("X")`
  calls with no matching `:SetAttribute("X")` and a matching exported getter,
  then either auto-rewrites to `require(Module).getX()` or surfaces an
  `UNCONVERTED.md` warning. See archive: "Cross-script shared-state gap —
  prompt iteration insufficient (2026-04-24)".
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

- [ ] **P1 — Eval-diff in CI nightly job.** `eval_baseline.json` exists and
  `u2r.py eval-diff` can gate, but the CI nightly job doesn't run it yet.
  See archive: "Eval baseline for all 9 projects" entry under Open Gaps
  (2026-04-12 session).
- [ ] **P2 — Three-flow `rbx_place` byte-equivalence test.** Codex review
  P1-6 deferred. Source-level + behavior-level parity is now tested
  (commit `420b01e`). Byte-equivalence requires a real Unity fixture.
- [ ] **P2 — Standalone `.rbxm` file output per prefab.** PR 5 deferred.
  Toolbox convenience; no runtime dependency on this format.

## Type-strictness debt (forward-only gate landed; cleanup separate)

The no-Any gate prevents new smuggling but doesn't fix existing offenders.
Each cleanup is a separate small PR that brings one file under the principle.

- [ ] **P1 — `core/conversion_context.py` Any-erasure.** L50 `mesh_native_sizes`,
  L57 `mesh_hierarchies`, L65 `scenes_metadata`, L68 `comparison_scores`,
  L73 `storage_plan`. The `storage_plan` field is the worst — `StoragePlan`
  IS already a dataclass at `converter/storage_classifier.py:90`.
- [ ] **P1 — `pipeline.py` `PipelineState` fields.** `transpilation_result`,
  `animation_result`, `prefab_library`, `scriptable_objects`, `sprite_result`,
  `material_mappings` all `Any`. Each has a real type already in the codebase.
- [ ] **P1 — Ported-module signatures.** Original audit findings:
  - `serialized_field_extractor.py:92-95` — `parsed_scenes: list[Any]`,
    `prefab_library: Any`, `guid_index: Any`
  - `prefab_packages.py:101-107` — `prefab_library: Any`, `guid_index: Any`,
    `material_mappings: dict[str, Any]`
  - `animation_converter.py:1543-1545` — `guid_index: Any`,
    `parsed_scenes: list[Any]`
  - `material_mapper.py:805` — internal `_extract_shader_name(..., guid_index: Any)`
- [ ] **P2 — `core/unity_types.py:121` `raw_documents: list[dict]`.** Trivial
  fix to `list[dict[str, Any]]` once the dict shape is decided.
- [ ] **P2 — `roblox/rbxlx_writer.py` Any in return + internal state.**
  L1390 `-> dict[str, Any]` (writer return), L1581 `_container_by_path:
  dict[str, Any]` (ET element map).

---

For platform limitations, Unity features with no Roblox equivalent, and Open
Cloud API limits, see [`docs/UNSUPPORTED.md`](docs/UNSUPPORTED.md). For
architectural debt and bug-shaped gaps, see [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md).
For long-horizon strategic work, see [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md).
