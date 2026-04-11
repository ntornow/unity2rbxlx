# Phase 2: Asset Inventory

Catalogs every asset in `<unity_project_path>`, resolves GUIDs, and builds a dependency graph. Produces a JSON inventory that downstream phases key off of.

## Command

```bash
python3 convert_interactive.py inventory <unity_project_path> <output_dir> 2>/dev/null
```

This runs the pipeline's `extract_assets` step — discovers every asset (texture, mesh, audio), builds the asset manifest, and pre-computes FBX bounding boxes (used as `InitialSize` when Studio mesh resolution is unavailable).

## Decision: duplicate GUIDs

**Question:** What to do when two assets share a GUID?

**Factors:**
- How the duplicates happened. Unity can duplicate GUIDs when an artist copies `.meta` files instead of re-importing. Rare but legal.
- Whether the duplicates are actually different files (content hash mismatch) or the same file in two locations.
- Whether either copy is referenced by the primary scene or its prefabs.

**Options:**
- **Keep first, log the rest.** Most duplicates are stale copies — pick the one in the canonical asset folder and log the others.
- **Keep the referenced one.** If only one is reachable from the primary scene, keep that one regardless of file order.
- **Abort.** Two content-different assets share a GUID AND both are reachable. Downstream phases will produce inconsistent output.

**Escape hatch:** Inspect `unity/guid_resolver.py` resolution order if the heuristic picks the wrong copy.

## Decision: orphaned assets

**Question:** Should orphaned assets (not referenced by any scene or prefab) be included?

**Factors:**
- Asset count. A few orphans is normal (unused test assets); thousands indicate a broken inventory.
- Whether the orphans include scripts. Orphaned `.cs` files that are part of a namespace used elsewhere are not truly orphaned — they're compile-time-only references.

**Options:**
- **Skip orphans.** Default. Reduces upload load and output size.
- **Include orphans.** Only when the project has known runtime-loaded assets (Resources folder, Addressables) that the static inventory cannot trace.

**Escape hatch:** Check for a `Resources/` folder under `Assets/`. Anything there is runtime-loaded and may look orphaned to a static scan.
