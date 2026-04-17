# Merge Plan Phase 3 — Augmented (2026-04-17)

Source: `https://github.com/jiazou/unity-roblox-game-converter/blob/main/MERGE_PLAN.md`.

The original Phase 3 plan had 12 items. Of the 12:
 - Modules for items 2–7 were ported (commit `a65f331`) but never wired.
 - Items 8–11 were never ported.
 - Item 1 was superseded by policy.
 - Item 12 is half-done (`storage_classifier` writes `conversion_plan.json`, but
   rehydration still re-infers script type heuristically).

This document captures the revised Phase 3 plan as it should apply against
current `main`, with per-item decisions and implementation order.

## Per-item decisions

| # | Plan item | Decision | Action |
|---|---|---|---|
| 1 | Bridge injection | **Superseded** by `docs/design/inline-over-runtime-wrappers.md`. Guard in `tests/test_no_rejected_bridges.py`. | Close — no action |
| 2 | Vertex color baking | **Deferred**. Real gap (phase-3-materials.md lines 35-37 document the regression), but needs a discovery pass first: which of the 9 test projects actually have vertex-color-only materials? Wiring without a test project that exercises it is unsafe. | Add TODO entry with the discovery-first requirement |
| 3 | Sprite extraction | **In scope**. Wire `extract_sprites()` into `extract_assets`. Persist a `sprites/` dir in the output plus `sprite_guid_to_file` mapping on the ctx. SpriteRenderer consumption of the mapping (to set real ImageLabel.Image) is a follow-up; wiring + persistence lands now. | Implement |
| 4 | Mesh splitting | **Superseded**. `scene_converter` handles multi-material FBX via sub-mesh hierarchy + `mesh_hierarchies` lookup. `mesh_splitter.py` is dead code. | Delete module, mirror the `bridge_injector` deletion pattern |
| 5 | ScriptableObject conversion | **In scope**. Wire `convert_asset_files()` in `extract_assets`. Persist each `ConvertedAsset` to `{output_dir}/scripts/{asset_name}.luau` so the rehydration path picks them up. Add to `rbx_place.scripts` as ModuleScript in `write_output`. | Implement |
| 6 | Binary writer | **In scope**. (a) Add `lz4>=4.0` to `pyproject.toml` + CI. (b) Call `xml_to_binary(rbxlx_path)` in `write_output`. (c) Auto-detect Content-Type in `upload_place` (`.rbxl` → `application/octet-stream`). Do NOT wire into interactive `upload` — binary is for future direct Open Cloud place-upload. | Implement |
| 7 | Report generator | **In scope**. Replace the inline dict in `pipeline.write_output` (~L1555) with `ConversionReport` + `generate_report()`. Do the same in `convert_interactive.py:report()`. Keep a fallback path only for environments where the import fails. | Implement |
| 8 | `generate_bootstrap_script()` | **Superseded**. Current pipeline auto-injects `GameServerManager`, detects FPS games, and injects runtime modules — we build lifecycle scripts via a different path. Phase 4c-bootstrap-emit.md documents the new approach. | Close — no action |
| 9 | `extract_serialized_field_refs()` | **Deferred**. Was a dependency of item 10. If 10 lands, this lands with it. | TODO |
| 10 | `generate_prefab_packages()` | **Deferred**. Current approach uses in-memory `prefab_library` + inline expansion. The per-prefab-packages output (for ReplicatedStorage/Templates cloning) is still wanted for runtime prefab spawning, but needs a design pass — not part of this phase. | TODO |
| 11 | Extend `write_output` disk rewrite | **Partial**. Rewrite must add `scripts/` ScriptableObject path under item 5. `animation_data/` and `packages/` wait for items 9/10. | Land the ScriptableObject slice as part of item 5 |
| 12 | `script_manifest.json` / lossless rehydration | **In scope**. `storage_classifier` writes `conversion_plan.json` (pipeline.py:1771-1774) but rehydration (pipeline.py:1143-1148) still uses content heuristics + `luau_path.stem`, ignoring it. Modify rehydration to read the previous run's `conversion_plan.json` first and use its `parent_path` + `script_type` decisions; fall back to heuristic only for unclassified scripts. | Implement |

## Implementation order

Ordered by risk/independence; each step lands as its own commit with tests.

1. **Item 6 — binary writer + lz4 dep + content-type** (3 small pieces, one file each)
2. **Item 7 — report generator adoption** (two call sites, fallback preserved)
3. **Item 3 — sprite extraction wiring** (extract_assets + ctx persistence)
4. **Item 5 — ScriptableObject conversion** (extract_assets + disk write + write_output attach)
5. **Item 12 — rehydration reads `conversion_plan.json`** (closes the classifier-rehydrator loop)
6. **Item 4 — delete `mesh_splitter.py`** (cleanup + regression guard)

Each step ends with `pytest tests/ -m "not slow"` green. Full suite run after step 6.

## Out of scope

Items 1, 8 — closed as superseded.
Items 2, 9, 10, 11 (minus the item-5 slice) — opened as TODOs.
