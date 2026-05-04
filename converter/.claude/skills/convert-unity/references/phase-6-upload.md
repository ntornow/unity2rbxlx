# Phase 6: Upload & Publish

u2r publishes the place **headlessly** via the Roblox Open Cloud Luau Execution API. No Studio interaction required for the common case.

## Command

```bash
python3 convert_interactive.py upload <output_dir> \
  --api-key ../apikey --universe-id <uid> --place-id <pid> 2>/dev/null
```

The `--universe-id` and `--place-id` are cached in `conversion_context.json` after the first call, so subsequent runs can omit them.

## How it works (Strategy A — Headless place builder)

`roblox/luau_place_builder.py` generates a Luau script (~700 KB for a typical project) that, when executed via Open Cloud `execute_luau` against the user's universe/place:

1. Calls `CreateMeshPartAsync(rbxassetid://…)` for every uploaded mesh, which returns a real MeshPart with the geometry baked in.
2. Reconstructs the entire place — parts, scripts, terrain, lighting, UI — using the loaded MeshParts.
3. Calls `SavePlaceAsync()` to commit the result back to Roblox.

Because the place is reconstructed server-side via the Luau Execution API, the resulting place has proper 3D geometry visible in Studio edit mode. **No runtime loader script is required.** The script is split into chunks if it exceeds the 4 MB Luau Execution API limit.

For full details and the runtime MeshLoader fallback (Strategy B), read `references/upload-patching.md`.

## What gets published

**The interactive `upload` subcommand publishes a fresh rebuild, not the local `.rbxlx`.** Upload re-runs `parse → … → convert_scene` in-memory and feeds the rebuilt `rbx_place` into the headless place builder. The runtime warning emitted in the upload JSON output (`convert_interactive.py:1011`) surfaces this to the user.

Why: there is no `.rbxlx` reader on the dest side; the pipeline only writes rbxlx, never reads it. Adding a reader is roadmapped in `converter/docs/FUTURE_IMPROVEMENTS.md`.

What this means in practice:
- Hand-edits to `<output>/converted_place.rbxlx` between `assemble` and `upload` are silently dropped on republish.
- If you want to publish a hand-edited `.rbxlx`, open it in Studio and use **File → Publish to Roblox** (the manual route under "Decision: upload failures" below).
- If you want to re-publish the assembled state without re-running the converter, prefer `python u2r.py publish <output_dir>` from the non-interactive CLI: it replays `<output>/place_builder_chunks.json` if cached (preserving the assembled state byte-for-byte), and only falls back to a fresh Pipeline rebuild when the cache is missing. See `converter/CLAUDE.md` § Upload semantics for the side-by-side comparison.

## Prerequisites

The target place must already exist. The user creates it once in Roblox Studio (File > New > Save to Roblox), then provides the universe ID and place ID.

If the API rejects uploads with `place_not_published`, the user must open the place in Studio at least once and publish an initial version before the API can accept further updates.

## Decision: upload failures

**Question:** If the headless upload fails, what should the agent do?

**Factors:**
- Error type. The pipeline returns `error_type` for known failure modes:
  - `place_not_published` — terminal until the user publishes once via Studio. Stop and tell the user.
  - `auth_failure` — invalid or expired Roblox API key. Stop and ask the user to verify.
  - `script_too_large` — the headless place builder exceeds 4 MB. Fall back to opening the local `.rbxlx` in Studio manually, or split into a smaller scene.
  - Transient (network, rate-limit) — retry once with backoff.
- How much of the place uploaded successfully. The Luau Execution API runs the chunks sequentially; partial completion may leave the place in a half-built state.

**Options:**
- **Retry.** Transient errors only.
- **Manual Studio publish.** For `script_too_large` or persistent `execute_luau` failures: open the local `.rbxlx` in Studio, then File > Publish to Roblox. Mesh assets are already embedded as `rbxassetid://` URLs in the rbxlx, so Studio sees them directly without a runtime loader.
- **Abort.** Permanent failures (`auth_failure`, `place_not_published`).

**Escape hatch:** `python3 u2r.py publish <output_dir> --universe-id <uid> --place-id <pid>` re-runs only the publish step using the cached `place_builder.luau` from the previous conversion. Use this when you want to retry upload without re-running the full pipeline.

## Asset ID patching (already done by assemble)

The `assemble` skill phase has already uploaded assets and embedded `rbxassetid://` URLs into the `.rbxlx`. Phase 6 only publishes the file. If you see `rbxassetid://` placeholders or `-- TODO: upload` comments in the rbxlx after assemble, that's a bug in the assemble phase — see `references/upload-patching.md` for the patching strategies.
