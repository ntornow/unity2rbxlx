# Upload Patching Details

## Assembly Phase Internals

The assembly phase (`convert_interactive.py assemble`) drives the back end of the pipeline in this order:

1. `upload_assets` — uploads textures (Image), meshes (Model), audio via `roblox/cloud_api.py`. Returns `rbxassetid://` URLs that the writer embeds directly into the .rbxlx.
2. `resolve_assets` — runs the headless mesh resolver via `roblox/studio_resolver.py` (uses Luau Execution API to call `CreateMeshPartAsync` and capture true `MeshId` + `InitialSize` + sub-mesh hierarchy from a real Roblox session).
3. `convert_animations` — `converter/animation_converter.py` converts `.anim` / `.controller` files into TweenService Luau scripts (transform animation) or Motor6D bone chains (skeletal). Up to 6 runtime modules under `runtime/` are *conditionally* injected based on what the scene actually uses: `animator_runtime.luau`, `nav_mesh_runtime.luau`, `event_system.luau`, `physics_bridge.luau`, `cinemachine_runtime.luau`, `sub_emitter_runtime.luau`. The old `pickup_runtime.luau` is no longer injected — pickup scripts are propagated directly from base prefabs to variants in `_bind_scripts_to_parts`.
4. `convert_scene` — `converter/scene_converter.py` walks the parsed scene tree and produces an `RbxPlace` (typed, see `core/roblox_types.py`).
5. `write_output` — `roblox/rbxlx_writer.py` serialises the `RbxPlace` to `<output_dir>/converted_place.rbxlx`.

**Vertex-color handling.** Unity stylized assets (roads, buildings, sky, poles) often use per-vertex colors instead of textures (the `VCOL` material — `_MainTex: {fileID: 0}`). Roblox ignores FBX vertex colors entirely. u2r prefers two paths:

1. **Baking** (preferred). A vertex color baker rasterises per-vertex colors onto UV-mapped textures using barycentric interpolation. Requires `assimp` (`brew install assimp`) to load FBX vertex colors + UVs via `pyassimp`. The baked PNGs are uploaded as Image assets and applied via SurfaceAppearance.
2. **Flat-color fallback.** Extract the dominant vertex color from the FBX binary and set `Color3`. This prevents default gray but loses per-vertex detail.

## Local-to-World Transform Computation (Critical)

Unity stores all transforms as **local-space** (relative to parent). Roblox Parts use **world-space CFrame**. The converter MUST compute world transforms when flattening Unity's hierarchy into Roblox Parts.

The formula applied recursively through the scene tree:
```
world_position = parent_world_position + parent_world_rotation * local_position
world_rotation = parent_world_rotation * local_rotation
```

Without this, every child object ends up at its local offset from the world origin (0,0,0) instead of from its parent — causing all nested objects to collapse to the origin. Implemented in `converter/scene_converter.py` via the `node_to_part()` recursion that threads parent transforms through the tree. Root-level scene nodes use parent position (0,0,0) and identity rotation (0,0,0,1).

The Unity → Roblox axis flip lives in `core/coordinate_system.py`:
- Position: `(x, y, z)_unity → (x, y, -z)_roblox`
- Quaternion: `(qx, qy, qz, qw)_unity → (-qx, -qy, qz, qw)_roblox`

## Content Property XML Format

Roblox .rbxlx XML requires Content-type properties (MeshId, TextureId, SoundId, ColorMap) to use a `<url>` sub-element:
```xml
<Content name="MeshId">
  <url>rbxassetid://12345</url>
</Content>
```
Writing the value directly as text content (`<Content name="MeshId">rbxassetid://12345</Content>`) causes Roblox Studio to ignore the value, resulting in missing textures/meshes. Handled in `roblox/rbxlx_writer.py`.

## Mesh Loading: Headless vs Runtime

u2r supports two strategies for getting mesh geometry into the published place. The headless path is preferred and is the default when `convert_interactive.py upload` runs successfully.

### Strategy A — Headless place builder (preferred)

`roblox/luau_place_builder.py` generates a Luau script (~700 KB for a typical project) that, when executed via Open Cloud `execute_luau` against the user's universe/place:

1. Calls `CreateMeshPartAsync(rbxassetid://…)` for every uploaded mesh, which returns a real MeshPart with the geometry baked in.
2. Reconstructs the entire place — parts, scripts, terrain, lighting, UI — using the loaded MeshParts.
3. Calls `SavePlaceAsync()` to commit the result back to Roblox.

Because the place is reconstructed server-side via the Luau Execution API, the resulting place has proper 3D geometry visible in Studio edit mode. **No runtime loader script is required.** The script is split into chunks if it exceeds the 4 MB Luau Execution API limit.

### Strategy B — Runtime MeshLoader (fallback)

When the headless path is unavailable (script over 4 MB, no API access, etc.), the runtime MeshLoader pattern still works.

**`MeshId` is read-only at runtime.** Roblox does not allow scripts to set `MeshId` on a MeshPart — attempting it produces `"The current thread cannot write 'MeshId' (lacking capability NotAccessible)"`. This means you **cannot** create an empty MeshPart in the .rbxlx and fill in its MeshId later from a script.

**How mesh assets work in Roblox:**
- FBX files uploaded via Open Cloud become **Model assets** (not raw meshes)
- `InsertService:LoadAsset(assetId)` returns a Model containing MeshParts with their MeshId already set
- To get a mesh into the scene at runtime, you must **clone** the MeshPart from the loaded Model

**The MeshLoader pattern:**
1. Upload FBX files as Model assets → get asset IDs
2. At runtime, `InsertService:LoadAsset()` each asset → extract the first MeshPart descendant
3. Store the extracted MeshPart in ReplicatedStorage/Templates as a template
4. For scene placement: **clone the template and replace** placeholder Parts, copying CFrame/Name/Parent from the placeholder. Do NOT try to set MeshId on existing parts.

```lua
-- WRONG: MeshId is read-only at runtime
part.MeshId = sourceMeshPart.MeshId  -- ERROR: NotAccessible

-- RIGHT: Clone the loaded MeshPart and replace the placeholder
local replacement = sourceMeshPart:Clone()
replacement.Name = placeholder.Name
replacement.CFrame = placeholder.CFrame
replacement.Anchored = true
replacement.CanCollide = placeholder.CanCollide
replacement.Transparency = placeholder.Transparency
replacement.Color = placeholder.Color       -- preserve vertex-color fallback
replacement.Size = placeholder.Size         -- preserve converted size
-- Transfer children (SurfaceAppearance, etc.) from placeholder to clone
for _, child in ipairs(placeholder:GetChildren()) do
    child.Parent = replacement
end
replacement.Parent = placeholder.Parent
placeholder:Destroy()
```

**Critical details:**
- **Scan both Workspace AND ReplicatedStorage.** Scene objects live in Workspace, but prefab templates for runtime spawning live in ReplicatedStorage/Templates. If the MeshLoader only scans Workspace, all runtime-cloned content will have placeholder geometry instead of real meshes.
- **Transfer ALL properties from placeholder to clone.** The replacement must copy `CFrame`, `Anchored`, `CanCollide`, `Transparency`, `Color` (vertex-color fallback), and `Size` from the placeholder. It must also reparent all children (SurfaceAppearance with texture references). InsertService-loaded meshes have geometry but no SurfaceAppearance or custom Color3.
- **Batch InsertService calls.** Firing all `InsertService:LoadAsset()` calls simultaneously overwhelms Roblox's asset servers, causing `SslConnectFail` on most requests. Load in batches of ~10 with retries.
- **Game bootstrap MUST wait for MeshLoader to finish before entering gameplay.** MeshLoader loads assets asynchronously (typically 20-30s for ~175 meshes). Use polling on a `MeshLoaderDone` BoolValue in ReplicatedStorage — see Step 4.5e in the main SKILL.md.

## Asset Type for Texture Uploads

Roblox Open Cloud distinguishes between `Decal` and `Image` asset types. **SurfaceAppearance properties (ColorMap, NormalMap, MetalnessMap, RoughnessMap) require `Image` assets.** Uploading a texture as `Decal` and using the asset ID in a SurfaceAppearance produces: `Error: Asset type does not match requested type`. Use `Decal` only for UI sprites and legacy Decal instances. `roblox/cloud_api.py:upload_image` selects the right type by default.

## Creator ID Extraction

The Roblox Open Cloud Assets API requires a valid `creator.userId` in the upload request. This must match the API key's owner. The uploader auto-extracts the owner ID from the API key's JWT payload (`ownerID` claim). Using a wrong or stale creator ID produces `404: Creator User XXXXX is not found` on every asset upload.

## Upload Patching Strategies

The upload command handles everything automatically:

1. Uploads textures as `Image` assets and sprites as `Decal` assets (polls async operations for asset IDs).
2. Patches the .rbxlx with `rbxassetid://` URLs:
   - Replaces `rbxassetid://` placeholders and `-- TODO: upload` comments
   - Replaces local filesystem paths by matching filenames
   - Replaces bare texture filenames in SurfaceAppearance ColorMap values (e.g. `BrickWall_color.png` → `rbxassetid://12345`)
   - Injects new SurfaceAppearance on MeshParts by scanning the Unity project for mesh→material relationships
3. Optionally generates a Luau place builder and runs it via `execute_luau` (the headless path).

## Structured Error Types

The JSON output from `convert_interactive.py upload` may include `error_type` for known failure modes. Common cases:

- `place_not_published` — the user must open Roblox Studio, open the place, and publish an initial version before the API can accept further updates.
- `auth_failure` — invalid or expired Roblox API key.
- `script_too_large` — the headless place builder exceeds 4 MB; fall back to opening the local rbxlx in Studio or use `u2r.py publish` against a smaller scene.

## When to use `u2r.py` directly instead

The interactive CLI re-runs prerequisite pipeline phases on every invocation (matching `Pipeline.resume` semantics) so each subcommand is self-contained. This is convenient for the skill but expensive for one-shot conversions.

For non-interactive end-to-end runs, prefer:

```bash
python3 u2r.py convert <unity_project> -o <output_dir> --api-key ./apikey
```

For re-publishing an already-converted place without re-running the pipeline:

```bash
python3 u2r.py publish <output_dir> --universe-id <uid> --place-id <pid>
```

This reuses the cached `place_builder.luau` written during the previous conversion.
