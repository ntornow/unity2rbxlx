# Phase 5: Assembly

Uploads assets, resolves real MeshIds via the headless mesh resolver, converts the scene tree, and writes the `.rbxlx` with `rbxassetid://` URLs already embedded.

## Command

```bash
python3 convert_interactive.py assemble <unity_project_path> <output_dir> \
  --api-key ../apikey --creator-id ../creator_id 2>/dev/null
```

Use `--no-upload` for a dry-run that emits placeholder URLs, or `--no-resolve` to skip the headless mesh resolver step.

## Pipeline phases run

The `assemble` skill phase invokes these `Pipeline` phases in order:

1. **upload_assets** (`roblox/cloud_api.py`) — uploads textures (Image), meshes (Model), audio. Returns `rbxassetid://` URLs that the writer embeds directly into the .rbxlx.
2. **resolve_assets** (`roblox/studio_resolver.py`) — runs the headless mesh resolver via the Luau Execution API to call `CreateMeshPartAsync` and capture true `MeshId` + `InitialSize` + sub-mesh hierarchy from a real Roblox session.
3. **convert_animations** (`converter/animation_converter.py`) — converts `.anim` / `.controller` files into TweenService Luau scripts (transform animation) or animator config modules (skeletal). Auto-injects runtime modules from `runtime/` (animator, nav mesh, event system, physics bridge, cinemachine, sub-emitter, pickup).
4. **convert_scene** (`converter/scene_converter.py`) — walks the parsed scene tree and produces an `RbxPlace` typed model (`core/roblox_types.py`).
5. **write_output** (`roblox/rbxlx_writer.py`) — serialises the `RbxPlace` to `<output_dir>/converted_place.rbxlx`.

## Terrain handling

The pipeline auto-detects Unity TerrainData assets (`converter/terrain_converter.py` + `roblox/terrain_encoder.py`). The SmoothGrid binary encoding is fully reverse-engineered (6-bit material + occupancy + RLE, axis swap, 22 materials), so terrain renders directly in Studio without a runtime FillBlock loader for the common case.

If terrain extraction fails, a FillBlock script fallback is generated for runtime terrain generation.

## LFS requirement

If a terrain `.asset` file is a Git LFS pointer (starts with `version https://git-lfs`), the pipeline warns but cannot extract terrain data. The user must run `git lfs install && git lfs pull` to download the binary. The pipeline detects and skips LFS pointer files for textures, meshes, and audio as well.

## Decision: asset upload failures

**Question:** If some asset uploads fail mid-stage, what should the agent do?

**Factors:**
- Failure type. Rate-limit errors are transient; content-policy rejections are permanent.
- How critical the failing assets are. Hero meshes matter; background props don't.
- How many assets failed (percentage of total).

**Options:**
- **Retry.** Transient errors (network, rate limit) — wait and retry once.
- **Continue without.** Low failure rate on non-critical assets; the game still works.
- **Abort.** High failure rate or critical assets failing — investigate before proceeding.

**Escape hatch:** The uploader writes per-asset status into `conversion_context.json`. Inspect it before deciding. Re-running `assemble` reuses already-uploaded assets from the context.

## Decision: terrain verification

If terrain was found and processed, open the assembled `.rbxlx` in Studio and verify visually. The agent can proceed without Studio, but terrain issues (wrong scale, wrong materials, missing water) are much easier to catch at this stage than post-upload.
