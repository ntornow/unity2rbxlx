# Architecture

## Overview

Converts Unity game projects into playable Roblox experiences. Handles scene hierarchy, materials, C# -> Luau transpilation, mesh processing, animation conversion, and asset upload.

```
Unity Project --> [Parser] --> Scene Graph (IR) --> [Converter] --> Roblox Output
                                                        |
                                                   .rbxlx file + MCP Studio injection
                                                        |
                                                  [Comparison System]
```

## Pipeline Phases

1. **Parse**: scene_parser + prefab_parser + guid_resolver -- parse Unity YAML
2. **Extract Assets**: asset_extractor -- catalog and hash all assets (textures, meshes, audio)
3. **Upload Assets**: cloud_api -- upload to Roblox Open Cloud (textures as Decal, meshes as Model, audio)
4. **Resolve Assets** (Studio-required): InsertService:LoadAsset to get:
   - Mesh Model IDs -> real MeshIds + sub-mesh hierarchy with sizes
   - Texture Decal IDs -> Image IDs (SurfaceAppearance needs Image, not Decal)
5. **Materials**: material_mapper -- Unity .mat files -> Roblox SurfaceAppearance with uploaded texture URLs
6. **Scripts**: code_transpiler -- C# -> Luau (rule-based + AI via Claude CLI)
7. **Animations**: animation_converter -- .anim/.controller -> TweenService Luau scripts
8. **Convert Scene**: scene_converter + component_converter -- build Roblox data model
9. **Output**: rbxlx_writer -- generate .rbxlx XML

## Asset Resolution (Critical)

After uploading FBX meshes via Open Cloud, the returned IDs are **Model** IDs, not Mesh IDs.
To use them in MeshPart.MeshId, you must:
1. `InsertService:LoadAsset(modelId)` in Studio
2. Extract each MeshPart descendant's `.MeshId`, `.Size`, `.Position`, `.TextureID`
3. Store this data in `conversion_context.json` as `mesh_hierarchies`

Similarly, uploaded texture Decal IDs must be resolved to Image IDs:
1. `InsertService:LoadAsset(decalId)` -> get Decal descendant
2. Extract Image ID from `decal.Texture` URL
3. Replace Decal IDs with Image IDs in `uploaded_assets`

Use `u2r.py resolve` to generate the Luau scripts for these resolutions.

## Mesh Sizing

Roblox MeshPart uses Size and InitialSize:
- `InitialSize` = mesh's native bounding box from Roblox (via LoadAsset)
- `Size` = desired visual size = `InitialSize * globalScale * unity_scale * STUDS_PER_METER`
- Roblox renders mesh scaled by `Size / InitialSize`

Where:
- `globalScale` = from FBX .meta file (converts FBX units to Unity meters)
- `unity_scale` = scene/prefab instance localScale
- `STUDS_PER_METER` = 3.571 (1 Roblox stud = 0.28m)

## Design Principles

- Data flows linearly: each module's output is passed explicitly to the next
- No circular imports between modules
- State between phases stored in ConversionContext (JSON-serializable)
- Use actual data from Roblox (LoadAsset) for mesh sizes, not heuristics

## Coordinate System

- Unity: left-handed Y-up, Z-forward
- Roblox: right-handed Y-up
- Position: `(x, y, z)` Unity -> `(x, y, -z)` Roblox
- Quaternion: `(qx, qy, qz, qw)` Unity -> `(-qx, -qy, qz, qw)` Roblox

## Test Projects (../test_projects/)

- **SimpleFPS**: 2 scenes (TEXT YAML), 37+ scripts, 87+ prefabs -- primary test project
- **3D-Platformer**: 1 scene (BINARY), 6 scripts, 6 prefabs -- simplest but needs text export
- **BoatAttack**: URP demo with water/boats (Git LFS textures)
- **BossRoom**: Unity networking sample (binary scenes, minimal content)
- **Gamekit3D**: Action game kit (18k+ parts, largest test)
- **RedRunner**: 8 scenes (BINARY), 82+ scripts -- 2D platformer
- **ChopChop**: 50 scenes, 275+ scripts -- nested project (UOP1_Project/), auto-detected
- **PrefabWorkflows**: 467 parts -- nested project, auto-detected
- **SanAndreasUnity**: GTA SA loader -- no bundled assets (runtime-loaded from GTA files)

## Supported Features

- Text YAML scene parsing (binary requires UnityPy)
- Both Standard and URP (Universal Render Pipeline) Lit shaders
- Both old (data:/first:/second:) and new (list-of-dicts) Unity YAML formats
- PSD, BMP, TGA, TIF texture auto-conversion to PNG
- FBX and OBJ mesh upload; MP3, OGG, WAV audio upload
- Prefab instance hierarchy with world-space transform composition
- FBX-as-prefab instances (Model Prefabs used directly in scenes)
- Multi-mesh FBX sub-mesh resolution via fileID mapping
- BoxCollider/SphereCollider/CapsuleCollider/MeshCollider conversion
- Trigger collider detection (CanCollide=false, no size inflation)
- Light, Sound (with RollOff distances), ParticleSystem component conversion
- Directional light rotation -> Roblox ClockTime (sun position)
- Animator component detection (HasAnimator attribute)
- MonoBehaviour serialized field extraction as Roblox attributes
- Per-instance field overrides from prefab modifications
- Terrain component -> flat ground Part (filtered from regular conversion)
- Skybox material -> Roblox Sky with 6-face textures
- Canvas/UI -> ScreenGui with UDim2 layout (auto-sizing text for Best Fit)
- C# -> Luau script transpilation (AI-powered via Claude CLI)
- Client script detection (UnityEngine.UI, Input, Camera -> LocalScript)
- Animation .anim/.controller -> TweenService Luau scripts (controller name targeting)
- Auto-generated FPS controller, HUD, pickup detection
- C# remnant cleanup (null->nil, this.->script.Parent., True/False->true/false)
- Material type inference from names (concrete->Concrete, metal->Metal, wood->Wood)
- VideoPlayer -> VideoFrame (SurfaceGui-wrapped)
- AudioReverbZone/AudioReverbFilter -> ReverbSoundEffect
- Cinemachine VirtualCamera/FreeLook/Brain -> camera config attributes
- CanvasScaler -> ScreenGui scaling attributes
- Enhanced ParticleSystem: shape, emission, color/size/rotation/force over lifetime
- RemoteEvent auto-creation from script analysis
- Git LFS pointer detection and skip
- Prefab library caching (30%+ pipeline speedup)
- Conversion report JSON output
