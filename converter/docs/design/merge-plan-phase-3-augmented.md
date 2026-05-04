# Merge Plan Phase 3 — Augmented (2026-04-17, refreshed 2026-05-02 for Phase 6 closeout)

Source: `https://github.com/jiazou/unity-roblox-game-converter/blob/main/MERGE_PLAN.md`.

The Phase 3 plan has 12 items. The port in `a65f331` brought in modules
for items 2–7 but never wired them; items 8–11 were never ported.
Item 1 is superseded by `docs/design/inline-over-runtime-wrappers.md`.

The four items originally deferred from Phase 3 (2, 9, 10, 11) all closed
in Phase 4. The table below reflects the post-Phase-4 status.

## Per-item decisions

| # | Plan item | Decision |
|---|---|---|
| 1 | Bridge injection | **Superseded** by `inline-over-runtime-wrappers.md`. |
| 2 | Vertex color baking | **Landed in Phase 4.8** (`pipeline.py:1024`). MaterialMapping `uses_vertex_colors` flag from 4.2 drives the wiring; `bake_vertex_colors_batch` runs between `convert_materials` and `convert_scene`. |
| 3 | Sprite extraction | **Landed.** `extract_assets` slices spritesheets into `<output>/sprites/`; ctx carries the GUID -> file map. |
| 4 | Mesh splitting | **Superseded** by `scene_converter`'s sub-mesh hierarchy path. Module deleted. |
| 5 | ScriptableObject conversion | **Landed.** `.asset` YAML -> Luau ModuleScripts under `scripts/scriptable_objects/`, attached on both fresh-transpile and rehydration paths. |
| 6 | Binary writer | **Landed.** `write_output` emits a sibling `.rbxl`; `upload_place` picks Content-Type from the extension. |
| 7 | Report generator | **Landed.** `pipeline.write_output` + `convert_interactive.report()` both write the same structured shape. |
| 8 | `generate_bootstrap_script()` | **Superseded.** Lifecycle scripts come from `GameServerManager` auto-injection + FPS detection + runtime module injection. |
| 9 | `extract_serialized_field_refs()` | **Landed in Phase 4.9.** Persisted to `conversion_context.json`; consumed by 4.10. |
| 10 | `generate_prefab_packages()` | **Landed in Phase 4.10.** Per-prefab `packages/` emission with variant-chain preservation (5.13). |
| 11 | Disk rewrite for new subdirs | **Landed in Phase 4.11.** Disk rewrite covers `animation_data/`, `packages/`, and scriptable-object module paths in addition to the original `scripts/` + `scripts/animations/`. |
| 12 | Lossless rehydration | **Landed.** Rehydration reads `conversion_plan.json` for `script_type` and `parent_path`; content heuristics are the fallback. |

## Out of scope

All Phase 3 items closed: 1, 4, 8 superseded; 2, 3, 5, 6, 7, 9, 10, 11, 12 landed.

## Source docs not ported (and why)

Two docs from the source repo's `docs/` directory were named in the original
Phase 6 port list but are intentionally not ported. Recorded here so a reviewer
can answer "why isn't `GAME_LOGIC_PORTING.md` in dest?" without git archaeology.

- **`GAME_LOGIC_PORTING.md`** — describes a 9-module Unity bridge layer
  (`MonoBehaviour`, `Input`, `Time`, `GameObjectUtil`, `Physics`, `Coroutine`,
  `StateMachine`, `AnimatorBridge`, `TransformAnimator`). Eight of those modules
  were deleted on dest in 2026-04 per the `inline-over-runtime-wrappers.md`
  policy; the ninth (`AnimatorBridge`) was merged into `animator_runtime.luau`.
  Porting the doc verbatim would re-introduce stale architectural narrative.
  The Step 4.5 game-logic-porting playbook now lives in
  `.claude/skills/convert-unity/references/phase-4b-*.md`.
- **`MODULE_STATUS.md`** — 2026-04-07 status snapshot listing 14 modules with
  per-module gaps. Most gaps closed in Phase 4. Status snapshots should not be
  duplicated as canonical docs; the live equivalents are `TODO.md` (active
  PR-scoped work) and per-PR conversion reports in `TODO_archive.md`.
