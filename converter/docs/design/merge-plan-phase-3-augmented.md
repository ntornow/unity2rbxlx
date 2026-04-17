# Merge Plan Phase 3 — Augmented (2026-04-17)

Source: `https://github.com/jiazou/unity-roblox-game-converter/blob/main/MERGE_PLAN.md`.

The Phase 3 plan has 12 items. The port in `a65f331` brought in modules
for items 2–7 but never wired them; items 8–11 were never ported.
Item 1 is superseded by `docs/design/inline-over-runtime-wrappers.md`.

## Per-item decisions

| # | Plan item | Decision |
|---|---|---|
| 1 | Bridge injection | **Superseded** by `inline-over-runtime-wrappers.md`. |
| 2 | Vertex color baking | **Deferred**. Needs a test project that actually uses vertex-color-only materials before wiring. |
| 3 | Sprite extraction | **Landed.** `extract_assets` slices spritesheets into `<output>/sprites/`; ctx carries the GUID -> file map. |
| 4 | Mesh splitting | **Superseded** by `scene_converter`'s sub-mesh hierarchy path. Module deleted. |
| 5 | ScriptableObject conversion | **Landed.** `.asset` YAML -> Luau ModuleScripts under `scripts/scriptable_objects/`, attached on both fresh-transpile and rehydration paths. |
| 6 | Binary writer | **Landed.** `write_output` emits a sibling `.rbxl`; `upload_place` picks Content-Type from the extension. |
| 7 | Report generator | **Landed.** `pipeline.write_output` + `convert_interactive.report()` both write the same structured shape. |
| 8 | `generate_bootstrap_script()` | **Superseded.** Lifecycle scripts come from `GameServerManager` auto-injection + FPS detection + runtime module injection. |
| 9 | `extract_serialized_field_refs()` | **Deferred** with item 10. |
| 10 | `generate_prefab_packages()` | **Deferred.** Needs an architecture pass — no test project requires runtime prefab spawning yet. |
| 11 | Disk rewrite for new subdirs | **Partial.** Item 5's `scripts/scriptable_objects/` works via the existing rglob. `packages/` waits for item 10. |
| 12 | Lossless rehydration | **Landed.** Rehydration reads `conversion_plan.json` for `script_type` and `parent_path`; content heuristics are the fallback. |

## Out of scope

Items 1, 4, 8 — closed.
Items 2, 9, 10, 11 — deferred; see `TODO.md`.
