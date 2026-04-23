# Unity -> Roblox Game Converter

## Autonomous Work Plan (2026-03-24)

Work autonomously with no questions — just churn forever making the converter comprehensive.

**CRITICAL: NEVER STOP WORKING. NEVER present a summary and wait. After completing ANY task, IMMEDIATELY start the next one. The loop is: improve → test → convert → compare → identify gaps → improve. This loop runs FOREVER with zero pauses. If you find yourself about to write a "summary" or "here's what was accomplished" — STOP and instead start the next improvement.**

1. **Identify ALL gaps** between converter capability and Unity/Roblox features → write to TODO.md
2. **Build proper test harness** for rbxlx loading in Studio (osascript, command line, MCP verification)
3. **Work through TODO list** one by one, fixing each gap
4. **Once done, repeat from step 1** — re-analyze for remaining gaps

### Rules:
- No asking questions or permissions — just do the right thing
- Store progress in CLAUDE.md and TODO files
- Use osascript to control Studio (open files, close, reopen)
- Test harness should verify rbxlx files load correctly
- No hardcoded values — converter must be general-purpose for ALL Unity games
- ALWAYS verify `game.Name` before executing ANY MCP commands (critical safety check)
- Research first, build correctly, don't hack or guess
- Everything should be fully automatic by running `u2r.py`

### CRITICAL SAFETY: Agas Map of London
- **NEVER** connect to, send MCP commands to, or interact with the Studio instance titled "Agas Map of London"
- **NEVER** send osascript commands that could affect it
- Before ANY Studio/MCP operation, verify the target is the converter's place, NOT Agas Map
- This is a separate user project that must not be touched under any circumstances

### Progress tracking:
- See [TODO.md](TODO.md) for comprehensive gap analysis and task list

### Converter Status (as of 2026-04-12)

**1020 tests passing** (1020 fast in ~12s, 31 slow full-pipeline tests)
**9 test projects** converting and validating clean with zero errors:
- SimpleFPS (960 parts, 36 scripts), Gamekit3D (18,534 parts, 249 scripts)
- SanAndreasUnity (270 scripts), ChopChop (275 scripts), RedRunner (87 scripts)
- BoatAttack (55 scripts), BossRoom (195 scripts), 3D-Platformer (7 scripts), PrefabWorkflows (6,582 parts)

**Recent session (2026-04-11/12):**
- Rifle pickup end-to-end fix for SimpleFPS: script:GetAttribute walk-up lookup, RemoteEvent created at script-init (no race), getRifle idempotency, shoot() cleanup, cloud_api strict asset-ID validation.
- Rifle sub-mesh textures: prefab-referenced materials now applied as SurfaceAppearance to FBX sub-mesh MeshParts in `_extract_monobehaviour_attributes`.
- setupSounds broadening: ModuleScript-reclassified Player scripts now fall back to workspace search for a host Part's Sound children.
- Merged PR #1 (`/convert-unity` skill + phase-by-phase interactive CLI, +2,809 lines).
- CI wired (`.github/workflows/test.yml`), ANTHROPIC_API_KEY lazy binding, phase-4.5 doc staleness pass.

**Key milestones achieved:**
- P0/P1/P2: ALL resolved (terrain, scripts, content properties, sub-mesh materials, physics, UI, etc.)
- **Headless mesh resolution**: Luau Execution API → CreateMeshPartAsync + SavePlaceAsync. 328/328 meshes render as proper 3D geometry in Studio edit mode. Requires caller to supply a pre-created `--universe-id` / `--place-id` (Open Cloud does not support universe creation via API-key auth; see `roblox/cloud_api.create_experience`). After the first run, IDs are cached in `<output>/.roblox_ids.json` and the pipeline runs one-command.
- **One-command pipeline** (once IDs are cached): `u2r.py convert` → generates rbxlx + publishes to Roblox with proper meshes
- **Placement accuracy**: Per-sub-mesh vertical offsets, scene hierarchy composition for prefab children, all doors/turrets/pickups at correct positions. 176/176 scripts valid Luau syntax. Mixed collider handling (physical + trigger).
- **SimpleFPS gameplay verified**: Game starts clean, 0 script errors, water fills, terrain renders, HUD works, spawn points correct, all materials applied (0 default gray).
- **Performance**: Terrain encoding 2.4x faster via inlined _get_voxel (eliminated 13.8M function calls). SimpleFPS write_output: 8.0s→3.4s. Precomputed height grids + chunk skipping from prior session.
- SmoothGrid terrain: World-space chunk coordinates with Z inversion
- Luau place builder: 700KB script reconstructs entire place headlessly (parts, meshes, scripts, terrain, lighting, UI)
- SurfaceAppearance: Full PBR in rbxlx, Texture fallback for headless mode
- Luau validator: 6,950 lines, 50+ fix categories, format specifier preservation
- Script transpilation: 99.7% success rate across all projects
- Bone name resolution: Mixamo prefix stripping, case-insensitive matching

**See [TODO.md](TODO.md) for remaining open items.**

### Development History (2026-03-24 through 2026-03-28)

Detailed session-by-session progress is in git history. Key milestones:
- **2026-03-24**: Initial gap analysis, terrain SmoothGrid, SSS scripts, script coherence, LOD filtering, NavMesh. 250 tests.
- **2026-03-25**: Rule-based transpiler (try/catch, for loops, foreach), 50+ API mappings, video/reverb/cinemachine/particles, terrain splat maps, skeletal animation, prefab variants, CSafeLoader (7.5x faster YAML). 308 tests.
- **2026-03-26**: Prefab hierarchy orphans fixed (0 remaining), multi-scene conversion, nested project auto-detection, lambdas, collection initializers, Mathf/LINQ/DOTween utility functions, switch/case, out/ref params, null-conditional/coalescing, yield/coroutines, property get/set, type-check patterns. 374 tests.
- **2026-03-27 (30+ sessions)**: Massive Luau quality push — 200+ validator patterns added. Block balance (stack-based end tracking), cross-script dependency injection, CDATA wrapping fix, bare receiver fix, generic type stripping, Physics API fixes, Vector3 immutability, Color constants, comprehensive C# declaration/attribute/operator cleanup, Input axis mapping, property getter inlining, missing module return insertion, for-loop fallback to while. 888 tests.
- **2026-03-28**: SmoothGrid format fully reverse-engineered (6-bit material, occupancy, axis swap, 22 materials confirmed). Rigidbody physics (CustomPhysicalProperties), MeshCollider CollisionFidelity, Cinemachine camera runtime, test suite optimization (@slow markers). 888 tests.

### Full upload test (2026-03-25):
- SimpleFPS: 194 assets uploaded (0 errors), 26 Model IDs resolved, 328 meshes loaded
- Terrain + water + loaded meshes visible in Studio

## Overview
Converts Unity game projects into playable Roblox experiences. Handles scene hierarchy, materials, C# -> Luau transpilation, mesh processing, animation conversion, and asset upload.

## Entry Points

There are two CLIs that share the same `Pipeline` class and the same `conversion_context.json` on disk:

1. **`u2r.py` — non-interactive end-to-end CLI.** Use this for one-shot conversions, CI, batch jobs, anything that should run without human-in-the-loop. Subcommands: `convert`, `publish`, `analyze`, `validate`, `resolve`, `compare`. See `python u2r.py --help`.

2. **`convert_interactive.py` — phase-by-phase CLI for the `/convert-unity` Claude Code skill.** Each subcommand maps to a single skill phase, emits structured JSON to stdout, and persists state in `conversion_context.json`. Subcommands:

   | Skill phase | Pipeline phases run | Notes |
   |---|---|---|
   | `preflight`  | (none — env check)                | Validates Python, packages, Unity project |
   | `status`     | (none — reads ctx)                | Reports completed phases + next |
   | `discover`   | parse                             | Builds GUID index, picks scene |
   | `inventory`  | parse → extract_assets            | Builds asset manifest |
   | `materials`  | … → convert_materials             | Maps Unity .mat → SurfaceAppearance |
   | `transpile`  | … → transpile_scripts             | C# → Luau (rule-based + AI) |
   | `validate`   | (none — runs `luau-analyze`)      | Syntax-checks `<output_dir>/scripts/` with luau-analyze |
   | `assemble`   | upload_assets, resolve_assets, convert_animations, convert_scene, write_output | Produces `converted_place.rbxlx` |
   | `upload`     | parse → convert_scene + headless place builder | Publishes via `execute_luau` |
   | `report`     | (none — writes `conversion_report.json`) | Final summary |

   Each subcommand re-runs essential prerequisite phases on every invocation (matching `Pipeline.resume` semantics) so individual calls are self-contained — but state from previous calls is loaded from `conversion_context.json`.

3. **`/convert-unity` skill** — `converter/.claude/skills/convert-unity/SKILL.md` is the institutional knowledge layer that Claude Code follows when walking a user through an interactive conversion. It encodes the Unity↔Roblox semantic gaps (Step 4.5: architecture map, divergence analysis, module rewrite, bootstrap wiring) that the pipeline cannot automate.

   See also `converter/.claude/skills/convert-unity/references/upload-patching.md` for upload-strategy details.

   **Bug fix protocol:** when fixing a problem found in converted output, always fix BOTH the pipeline code (under `converter/`, `unity/`, `roblox/`, `runtime/`) AND the affected output scripts in `<output_dir>/scripts/`. A fix only to the output regresses on the next conversion; a fix only to the pipeline leaves the current game broken.

## Architecture

```
Unity Project --> [Parser] --> Scene Graph (IR) --> [Converter] --> Roblox Output
                                                        |
                                                   .rbxlx file + MCP Studio injection
                                                        |
                                                  [Comparison System]
```

### Pipeline Phases
1. **Parse**: scene_parser + prefab_parser + guid_resolver -- parse Unity YAML
2. **Extract Assets**: asset_extractor -- catalog and hash all assets (textures, meshes, audio)
3. **Upload Assets**: cloud_api -- upload to Roblox Open Cloud (textures as Decal, meshes as Model, audio)
4. **Resolve Assets** (Studio-required): InsertService:LoadAsset to get:
   - Mesh Model IDs → real MeshIds + sub-mesh hierarchy with sizes
   - Texture Decal IDs → Image IDs (SurfaceAppearance needs Image, not Decal)
5. **Materials**: material_mapper -- Unity .mat files → Roblox SurfaceAppearance with uploaded texture URLs
6. **Scripts**: code_transpiler -- C# -> Luau (rule-based + AI via Claude CLI)
7. **Animations**: animation_converter -- .anim/.controller → TweenService Luau scripts
8. **Convert Scene**: scene_converter + component_converter -- build Roblox data model
9. **Output**: rbxlx_writer -- generate .rbxlx XML

### Asset Resolution (Critical)
After uploading FBX meshes via Open Cloud, the returned IDs are **Model** IDs, not Mesh IDs.
To use them in MeshPart.MeshId, you must:
1. `InsertService:LoadAsset(modelId)` in Studio
2. Extract each MeshPart descendant's `.MeshId`, `.Size`, `.Position`, `.TextureID`
3. Store this data in `conversion_context.json` as `mesh_hierarchies`

Similarly, uploaded texture Decal IDs must be resolved to Image IDs:
1. `InsertService:LoadAsset(decalId)` → get Decal descendant
2. Extract Image ID from `decal.Texture` URL
3. Replace Decal IDs with Image IDs in `uploaded_assets`

Use `resolve_assets.py` to generate the Luau scripts for these resolutions.

### Mesh Sizing
Roblox MeshPart uses Size and InitialSize:
- `InitialSize` = mesh's native bounding box from Roblox (via LoadAsset)
- `Size` = desired visual size = `InitialSize × globalScale × unity_scale × STUDS_PER_METER`
- Roblox renders mesh scaled by `Size / InitialSize`

Where:
- `globalScale` = from FBX .meta file (converts FBX units to Unity meters)
- `unity_scale` = scene/prefab instance localScale
- `STUDS_PER_METER` = 3.571 (1 Roblox stud ≈ 0.28m)

### Key Design Principles
- Data flows linearly: each module's output is passed explicitly to the next
- No circular imports between modules
- State between phases stored in ConversionContext (JSON-serializable)
- Use actual data from Roblox (LoadAsset) for mesh sizes, not heuristics
- **Inline Unity → Roblox API translation over runtime wrappers.** Translate at
  transpile time via `api_mappings.py` / `UTILITY_FUNCTIONS`, with `luau-analyze`
  + AI reprompt loop catching any syntax errors in the generated output, not via
  `require`-able wrapper modules under `runtime/`. See
  `docs/design/inline-over-runtime-wrappers.md` for the rationale.

## Running Tests
```bash
cd converter
python -m pytest tests/ -m "not slow" -v   # Fast suite: 1020 tests in ~12s
python -m pytest tests/ -v                  # Full suite: 1029 tests in ~65s (includes CLI + Gamekit3D e2e)
```

## Running Conversion
```bash
cd converter
# Full conversion with upload
python u2r.py convert ../test_projects/SimpleFPS -o ./output/SimpleFPS --api-key-file ../apikey

# After upload, resolve assets via Studio MCP (required for proper mesh/texture IDs)
# Then regenerate rbxlx using the updated conversion_context.json
```

## Coordinate System
- Unity: left-handed Y-up, Z-forward
- Roblox: right-handed Y-up
- Position: (x, y, z)_unity -> (x, y, -z)_roblox
- Quaternion: (qx, qy, qz, qw)_unity -> (-qx, -qy, qz, qw)_roblox
- FBX mesh handedness: `fbx_binary.mirror_fbx_handedness()` negates X and Y in vertices/normals before upload (equivalent to 180° rotation around Z/vertical). This fixes asymmetric mesh features (text, logos) appearing on the wrong side in Roblox without affecting vertical positioning, triangle winding, or text orientation.

## Known Limitations
- FBX files with sub-mesh hierarchies use collider-based size fallback when Studio resolution not available
- PSD/TGA/BMP/TIF texture files are auto-converted to PNG for upload (requires PIL/Pillow)
- Animations are converted to TweenService scripts — works for property animations, skeletal uses Motor6D chain
- Terrain uses SmoothGrid binary encoding (reverse-engineered format, needs Studio verification) with FillBlock script fallback
- Uploaded textures return Decal IDs which must be resolved to Image IDs via Studio MCP
- Uploaded meshes return Model IDs which must be resolved to real MeshIds via Studio MCP
- Git LFS pointer files are detected and skipped (actual FBX data needs LFS pull)
- VFX Graph, particle SubEmitters, Tilemap, and Cloth have no Roblox equivalent (silently skipped)
- **Roblox API limitation — font/video upload**: The Open Cloud API only supports Image, Model (mesh), and Audio asset types. Font files and video files must be uploaded manually via the [Creator Dashboard](https://create.roblox.com) and their asset IDs pasted into the converted place. UI text falls back to Roblox's default font; VideoFrame components are emitted with an empty video ID placeholder
- Cross-scene constraint Part0/Part1 linking may fail for constraints spanning different scene roots

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

## CLI Commands
```bash
# Convert a Unity project to Roblox
python u2r.py convert <unity_project> -o <output_dir> [--api-key-file <key>] [--no-upload] [--no-ai] [--scene <scene.unity>|all] [--phase <phase>]

# Analyze a Unity project without converting
python u2r.py analyze <unity_project>

# Generate Studio resolution scripts after upload
python u2r.py resolve <output_dir>

# Validate a generated .rbxlx file
python u2r.py validate <rbxlx_file>

# Resume from a specific phase (reuses uploaded assets from context)
python u2r.py convert <unity_project> -o <output_dir> --phase convert_scene
```

## Post-Upload Resolution (requires Studio MCP)
After uploading, run these steps via Studio MCP `execute_luau`:
1. Resolve mesh Model IDs → real MeshIds (use `roblox/studio_resolver.py`)
2. Resolve texture Decal IDs → Image IDs
3. Update `conversion_context.json` with resolved IDs
4. Regenerate the .rbxlx

## Supported Features

### Scene & Asset Parsing
- Text YAML + binary scene parsing (binary scenes + terrain `.asset` files parsed via UnityPy)
- Both Standard and URP (Universal Render Pipeline) Lit shaders
- Both old (data:/first:/second:) and new (list-of-dicts) Unity YAML formats
- Prefab instance hierarchy with world-space transform composition
- Prefab variant chain resolution with property override merging
- FBX-as-prefab instances (Model Prefabs used directly in scenes)
- Multi-mesh FBX sub-mesh resolution via fileID mapping
- Multi-scene conversion (`--scene all`)
- Nested project auto-detection (Assets/ one level deep)
- Git LFS pointer detection and skip
- CSafeLoader for fast YAML parsing (7.5x speedup)

### Asset Upload & Processing
- Pre-upload asset safety moderation (`moderate_assets` phase) — screens filenames, scripts, and audio against Roblox Community Standards; auto-blocklists violations
- FBX mesh handedness fix — negates X+Y in vertices before upload to correct left-handed/right-handed mirror
- PSD, BMP, TGA, TIF texture auto-conversion to PNG
- FBX and OBJ mesh upload via Open Cloud API
- MP3, OGG, WAV, FLAC audio upload
- Prefab library caching (30%+ pipeline speedup)
- `resolve` CLI command for post-upload Studio asset resolution

### Components
- BoxCollider/SphereCollider/CapsuleCollider/MeshCollider (with CollisionFidelity)
- Rigidbody/Rigidbody2D → Anchored/CanCollide + CustomPhysicalProperties (mass/drag/friction)
- Physics joints: FixedJoint→WeldConstraint, HingeJoint→HingeConstraint, SpringJoint, CharacterJoint, ConfigurableJoint
- Light, Sound (with RollOff distances), ParticleSystem (shape, emission, color/size/force over lifetime)
- TrailRenderer → Trail, LineRenderer → Beam (with Attachments)
- VideoPlayer → VideoFrame (SurfaceGui-wrapped)
- AudioReverbZone/AudioReverbFilter → ReverbSoundEffect
- Post-processing: Bloom, ColorGrading, DepthOfField, SunShafts, Atmosphere
- Cinemachine VirtualCamera/FreeLook/Brain → camera config attributes + runtime script
- NavMeshAgent → PathfindingService runtime module
- SkinnedMeshRenderer → Motor6D bone chain
- 2D physics: Rigidbody2D, BoxCollider2D, CircleCollider2D, CapsuleCollider2D
- SpriteRenderer → thin colored Part with sprite GUID attribute

### Terrain
- SmoothGrid binary encoding (6-bit material + occupancy + RLE, axis swap)
- 22 terrain materials with height-based biome model and splat map support
- FillBlock script fallback for runtime terrain generation
- Water region detection and sizing

### Scripts
- C# → Luau transpilation (AI via Claude CLI with on-disk cache, 99.7% success)
- `luau-analyze` syntax check + AI reprompt loop on transpile output (replaces the former regex-based `luau_validator.py`, removed 2026-04-18)
- Client/Server/Module script auto-detection and reclassification
- Cross-script dependency injection (require() calls auto-inserted)
- Utility function auto-injection (Mathf, LINQ, Vector3 helpers)
- DOTween → TweenService code generation
- 5 runtime Luau modules auto-injected (animator, nav mesh, event system, physics bridge, cinemachine)

### UI & Scene
- Canvas/UI → ScreenGui with UDim2 layout, UIListLayout/UIGridLayout
- Button onClick wiring (UIEventWiring LocalScript)
- Skybox material → Roblox Sky with 6-face textures
- Directional light rotation → Roblox ClockTime
- Material type inference from names (33 material types)
- Metallic-based material inference (_Metallic > 0.5 → Metal)
- LODGroup filtering (keeps LOD0 only)
- Single-child Model flattening
- Part size capping at 2048 studs
- Auto-generated FPS controller, HUD, spawn management
- Animation .anim/.controller → TweenService Luau scripts
- RemoteEvent auto-creation from script analysis
- Conversion report JSON output
