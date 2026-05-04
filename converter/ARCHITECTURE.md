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

## Entry Points

The converter exposes the same `Pipeline` class through two CLIs that share the same `conversion_context.json` on disk. A conversion can be started in one mode and finished in the other.

### Mode 1 — `u2r.py` (non-interactive)

End-to-end CLI for one-shot conversions, CI/CD, batch jobs. No human in the loop. Subcommands: `convert`, `publish`, `analyze`, `validate`, `resolve`, `compare`. See `python u2r.py --help`.

### Mode 2 — `convert_interactive.py` + `/convert-unity` skill (phase-by-phase)

`convert_interactive.py` is a Click CLI where each subcommand corresponds to one phase of the user-facing skill workflow. Each subcommand emits structured JSON to stdout, persists state in `conversion_context.json`, and can be invoked independently — prerequisite phases are re-run as needed (matching `Pipeline.resume` semantics).

| Skill phase | Pipeline phases run | Notes |
|---|---|---|
| `preflight`  | (none — env check)                | Validates Python, packages, Unity project |
| `status`     | (none — reads ctx)                | Reports completed phases + next phase |
| `discover`   | parse                             | Builds GUID index, picks scene |
| `inventory`  | parse → extract_assets            | Builds asset manifest |
| `materials`  | … → convert_materials             | Maps Unity .mat → SurfaceAppearance |
| `transpile`  | … → transpile_scripts             | C# → Luau (rule-based + AI) |
| `validate`   | (none — runs `luau-analyze`)      | Syntax-checks `<output_dir>/scripts/` with luau-analyze |
| `assemble`   | upload_assets, resolve_assets, convert_animations, convert_scene, write_output | Produces `converted_place.rbxlx` |
| `upload`     | parse → convert_scene + headless place builder | Publishes via `execute_luau` |
| `report`     | (none — writes `conversion_report.json`) | Final summary |

The `/convert-unity` skill (`converter/.claude/skills/convert-unity/SKILL.md`) drives the interactive workflow. It pauses at each phase for human review (scene selection, material review, script review, scale strategy, etc.) and contains the Step 4.5 game-logic-porting playbook (architecture map, Unity↔Roblox divergence analysis, module-per-component rewrite, bootstrap wiring) that the pipeline cannot automate.

Upload publishing has two paths with different semantics: interactive `upload` rebuilds `rbx_place` from source on every run; `u2r.py publish` replays cached chunks at `<output>/place_builder_chunks.json` and falls back to a fresh rebuild only on cache miss. Manual `.rbxlx` publishing is via Roblox Studio (File → Publish to Roblox). See `CLAUDE.md` § Upload semantics for the full comparison; the skill-internal upload-strategy detail lives in the skill's `references/upload-patching.md`.

### Architecture diagram

```
┌─────────────────────────────────────────────────────────┐
│  /convert-unity  (Claude Code skill — human in loop)    │
└──────────────┬──────────────────────────────────────────┘
               │ invokes per-phase subcommands
               ▼
┌─────────────────────────────┐    ┌──────────────────────┐
│  convert_interactive.py     │    │  u2r.py (one-shot)   │
│  (10 Click subcommands,     │    │  convert/publish/…   │
│   JSON to stdout)           │    │                      │
└──────────────┬──────────────┘    └──────────┬───────────┘
               │                              │
               └─────────────┬────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  Pipeline  (converter/       │
              │   pipeline.py)               │
              │  ┌────────────────────────┐  │
              │  │ ConversionContext      │  │
              │  │ (conversion_context.   │  │
              │  │  json — persisted)     │  │
              │  └────────────────────────┘  │
              └──────────────────────────────┘
```

## Pipeline Phases

1. **Parse**: scene_parser + prefab_parser + guid_resolver -- parse Unity YAML (text + binary via UnityPy)
2. **Extract Assets**: asset_extractor -- catalog and hash all assets (textures, meshes, audio)
3. **Moderate Assets** (`moderate_assets`): asset_moderator -- screen filenames, scripts, and audio against Roblox Community Standards before upload; auto-blocklist violations
4. **Upload Assets**: cloud_api -- upload to Roblox Open Cloud (textures as Decal, meshes as Model, audio)
5. **Resolve Assets** (Studio-required): InsertService:LoadAsset to get:
   - Mesh Model IDs -> real MeshIds + sub-mesh hierarchy with sizes
   - Texture Decal IDs -> Image IDs (SurfaceAppearance needs Image, not Decal)
6. **Materials**: material_mapper -- Unity .mat files -> Roblox SurfaceAppearance with uploaded texture URLs
7. **Scripts**: code_transpiler -- C# -> Luau (rule-based + AI via Claude CLI), syntax-gated by `luau-analyze` + AI reprompt loop (replaces the former `luau_validator.py`, removed 2026-04-18)
8. **Animations**: animation_converter -- .anim/.controller -> TweenService Luau scripts (transform-only) or animator_runtime (humanoid)
9. **Convert Scene**: scene_converter + component_converter -- build Roblox data model
10. **Output**: rbxlx_writer -- generate .rbxlx XML; optional sibling .rbxl via `rbxl_binary_writer`

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

## Design Decisions

- **Inline over runtime wrappers** — Unity APIs are translated to Luau at transpile time via `api_mappings.py` / `UTILITY_FUNCTIONS`, not via `require()`-able runtime modules. Only stateful runtimes survive (`animator_runtime`, `nav_mesh_runtime`, `event_system`, `physics_bridge`, `cinemachine_runtime`, plus feature runtimes for object pooling, pickups, sub-emitters). Nine runtime bridges were deleted in 2026-04. See `docs/design/inline-over-runtime-wrappers.md`.
- **Conversion plan rehydration** — `conversion_plan.json` records `{script_type, parent_path}` for every transpiled script so the rehydration path (interactive `assemble --no-retranspile`, `upload` rebuild) reconstructs the exact same Roblox script-container layout as the fresh-transpile path. Phase 3 design.
- **Upload publishes a rebuild, not the on-disk `.rbxlx`** — there is no `.rbxlx` reader; both publish paths reconstruct `rbx_place` rather than reading the file. See `CLAUDE.md` § Upload semantics. Reader is roadmapped in `docs/FUTURE_IMPROVEMENTS.md`.

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

- Text YAML + binary scene parsing (binary via UnityPy, including terrain `.asset` files)
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
