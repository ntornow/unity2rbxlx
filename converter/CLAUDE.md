# Unity -> Roblox Game Converter

## Safety

Non-negotiable for any Studio/MCP operation:

- **Pin the target Studio explicitly.** Any code that issues MCP calls
  while another Studio may be open must call `set_active_studio` on the
  id it intends to target — never rely on MCP's active-Studio heuristic.
  The `/e2e-test` skill snapshots pre-launch ids, picks the new id post-
  launch, and pins it (Step 4 of `converter/.claude/skills/e2e-test/SKILL.md`).
- **Stay general-purpose.** No hardcoded, game-specific values: the converter must work for ALL Unity games, not just the bundled test projects.

> Test counts, converter status, and session-by-session history are intentionally
> not kept in this file — those snapshots go stale. Active work lives in
> [TODO.md](TODO.md); completed work and per-PR execution logs live in
> [TODO_archive.md](TODO_archive.md); detailed history is in git.

## Overview
Converts Unity game projects into playable Roblox experiences. Handles scene hierarchy, materials, C# -> Luau transpilation, mesh processing, animation conversion, and asset upload.

## Entry Points

There are two CLIs that share the same `Pipeline` class and the same `conversion_context.json` on disk:

1. **`u2r.py` — non-interactive end-to-end CLI.** Subcommands — conversion: `convert`, `publish`, `analyze`, `validate`, `resolve`, `compare`; tooling/eval: `snapshot-ids`, `audit-assets`, `eval`, `eval-diff`, `visual-compare`, `smoke-test`. See `python u2r.py --help`. **`convert` does NOT perform Step 4a (client/server split)** — it requires `--skip-architecture-step` and ships server-crashing UI modules. For a complete game conversion use the `/convert-unity` skill (entry 2); reserve `u2r.py` for individual phases, `--phase` resumes, and CI.

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
   | `assemble`   | full pipeline → write_output (skips `transpile_scripts` if cache intact and `--retranspile` not set) | Produces `converted_place.rbxlx`; cloud phases force-rerun |
   | `upload`     | full pipeline → write_output (skips moderate/upload/resolve_assets always; also skips `transpile_scripts` when its cache is intact) → headless place builder | Publishes via `execute_luau` |
   | `report`     | (none — writes `conversion_report.json`) | Final summary |

   Each subcommand re-runs essential prerequisite phases on every invocation (matching `Pipeline.resume` semantics) so individual calls are self-contained — but state from previous calls is loaded from `conversion_context.json`.

3. **`/convert-unity` skill** — `converter/.claude/skills/convert-unity/SKILL.md` is the institutional knowledge layer that Claude Code follows when walking a user through an interactive conversion. It encodes the Unity↔Roblox semantic gaps (Steps 4a-4c: architecture map, divergence analysis, module rewrite, bootstrap wiring) that the pipeline cannot automate.

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
3. **Plan Scene Runtime** (`plan_scene_runtime`): scene_runtime_planner -- plan the scene-runtime artifact off parsed scenes/prefabs before any script-touching phase; inert unless `--scene-runtime=generic` opts in (only the planner data lands in `conversion_plan.json`)
4. **Moderate Assets**: pre-upload safety screen -- filenames, scripts, audio vs. Roblox Community Standards
5. **Upload Assets**: cloud_api -- upload to Roblox Open Cloud (textures as **Image**, meshes as Model, audio)
6. **Resolve Assets**: headless via the Open Cloud Luau Execution API (`cloud_api.resolve_meshes_headless` → `execute_luau`) -- resolves mesh Model IDs → real MeshIds + sub-mesh hierarchy with sizes. No Decal→Image step (textures upload directly as `Image`). No-ops under `--no-upload` or when no API key/creator id is available.
7. **Materials**: material_mapper -- Unity .mat files → Roblox SurfaceAppearance with uploaded texture URLs
8. **Scripts**: code_transpiler -- C# → Luau (rule-based + AI; Claude CLI backend preferred, Anthropic API fallback when `--api-key` is passed)
9. **Animations**: animation_converter -- .anim/.controller → TweenService Luau scripts
10. **Convert Scene**: scene_converter + component_converter -- build Roblox data model
11. **Materialize & Classify** (`materialize_and_classify`): emit scripts to disk, run the post-transpile coherence pass, and compute the storage `StoragePlan` -- lifted out of `write_output` so it consumes a persisted plan + populated `rbx_place.scripts`. Runs after `convert_scene` (which creates `rbx_place`)
12. **Output**: rbxlx_writer -- generate .rbxlx XML

The scene-runtime-topology / contract-verifier subsystem (`contract_pipeline.py`, `scene_runtime_planner.py`) runs within these phases for generic-mode conversions.

Authoritative ordering lives in `converter/converter/pipeline.py:PHASES`.

### Asset Resolution (Critical)
After uploading FBX meshes via Open Cloud, the returned IDs are **Model** IDs, not Mesh IDs.
The `resolve_assets` phase resolves them to MeshPart.MeshId **headlessly** via the Open Cloud Luau
Execution API (`cloud_api.resolve_meshes_headless` → `execute_luau` /
`luau-execution-session-tasks`) — no interactive Studio session required:
1. `InsertService:LoadAsset(modelId)` runs inside a headless Luau task on a published place
2. Each MeshPart descendant's `.MeshId`, `.Size`, `.Position`, `.TextureID` is extracted
3. The data is stored in `conversion_context.json` as `mesh_hierarchies`

Textures need **no** resolution step: they upload directly as the `Image` asset type
(`cloud_api.upload_image`), so SurfaceAppearance gets a usable Image ID without the legacy
Decal→Image round-trip.

`python u2r.py resolve <output_dir>` (backed by `roblox/studio_resolver.py`) can still emit
Studio-side resolution scripts for manual/legacy flows.

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
python -m pytest tests/ -m "not slow" -v   # Fast suite (run before every PR)
python -m pytest tests/ -v                  # Full suite (adds the slow e2e tests)
```
**Run the full fast suite (`pytest -m "not slow"`) before any PR, not just touched files** — shared-constant edits break tests elsewhere (e.g. a frozen cache-key prompt).

## Workflow Discipline
- **Set the goal before the structure.** Restate the user's goal in one sentence and confirm it, then spike the riskiest unknown first. Splitting one goal into separately-named sub-deliverables before validating it is the warning sign.
- **Never pipe long-running commands to `tail`/`head`** — output buffers and looks hung. Redirect to a file.

## Running Conversion
```bash
cd converter
# Full conversion with upload
python u2r.py convert ../test_projects/SimpleFPS -o ./output/SimpleFPS --api-key ../apikey --skip-architecture-step

# After upload, resolve assets via Studio MCP (required for proper mesh/texture IDs)
# Then regenerate rbxlx using the updated conversion_context.json
```

## Coordinate System
- Unity: left-handed Y-up, Z-forward
- Roblox: right-handed Y-up
- Position: (x, y, z)_unity -> (x, y, -z)_roblox
- Quaternion: (qx, qy, qz, qw)_unity -> (-qx, -qy, qz, qw)_roblox
- FBX mesh handedness: `fbx_binary.mirror_fbx_handedness()` negates X and Y in vertices/normals before upload (equivalent to 180° rotation around Z/vertical). This fixes asymmetric mesh features (text, logos) appearing on the wrong side in Roblox without affecting vertical positioning, triangle winding, or text orientation.

## Upload semantics

Two paths publish to Roblox; they have different "what gets published" semantics. Pick the right one for your workflow.

| Path | What it publishes | When to use |
|---|---|---|
| **Interactive `upload`** (`convert_interactive.py upload`) | A **fresh rebuild** of `rbx_place` from source. Re-runs `parse → … → convert_scene` in-memory and feeds the result to the headless place builder. **Hand-edits to `converted_place.rbxlx` between `assemble` and `upload` are silently dropped.** A runtime warning (the `upload` command in `convert_interactive.py`) surfaces this. | Skill-driven flow when you want a one-shot publish after assemble; or when source has changed and you want a fresh build. |
| **`u2r.py publish`** | **Replays cached chunks** — `<output>/place_builder_chunks.json` first, then `<output>/place_builder.luau` for older conversions (`roblox/place_publisher.py:publish_cached_chunks`, two-tier fallback). Both shapes preserve the assembled state byte-for-byte. Falls back to a fresh Pipeline rebuild **only when both cache shapes are missing**. | When you want to re-publish the assembled state without re-running the converter (e.g., after a transient upload failure), or when the Unity project has been moved/archived. See the `publish` command in `u2r.py` and `roblox/place_publisher.py:publish_cached_chunks`. |
| **Studio manual publish** | Whatever is in the local `converted_place.rbxlx` (you edit it in Studio first). | When you want to publish a hand-edited `.rbxlx`. There is no `.rbxlx` reader on the dest side, so this is the only path that publishes the reviewed file directly. Roadmapped in `docs/FUTURE_IMPROVEMENTS.md`. |

The known limitation — interactive `upload` publishes a fresh rebuild, not the reviewed `.rbxlx` — is addressed by this documentation + the runtime warning + the `u2r.py publish` cached-chunks fast path. Implementing an `.rbxlx` reader is roadmap work.

## Reference Documentation

User-facing documentation lives outside this file:

- [`docs/UNSUPPORTED.md`](docs/UNSUPPORTED.md) — what the converter cannot do (platform limits, Unity features with no Roblox equivalent, API restrictions, rendering differences)
- [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md) — architectural debt and bug-shaped concerns
- [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md) — long-horizon, multi-PR strategic work
- [`TODO.md`](TODO.md) — active PR-scoped work
- [`TODO_archive.md`](TODO_archive.md) — historical work + per-phase PR execution logs
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — internal pipeline architecture
- [`docs/design/`](docs/design/) — design decisions (inline-over-runtime policy, merge plan)

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
python u2r.py convert <unity_project> -o <output_dir> --skip-architecture-step [--api-key <key|path>] [--no-upload] [--no-ai] [--scene <scene.unity>|all] [--phase <phase>]

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
- C# → Luau transpilation (AI: Claude CLI backend preferred, Anthropic API fallback when `--api-key` is set; on-disk cache)
- `luau-analyze` syntax check + AI reprompt loop on transpile output (replaces the former regex-based `luau_validator.py`, removed 2026-04-18)
- Client/Server/Module script auto-detection and reclassification
- Cross-script dependency injection (require() calls auto-inserted)
- Utility function auto-injection (Mathf, LINQ, Vector3 helpers)
- DOTween → TweenService code generation
- Stateful runtime Luau modules auto-injected as needed (scene_runtime, scene_camera_input, nav_mesh, event_system, event_dispatch, physics_bridge, cinemachine, object_pool, pickup, sub_emitter); the legacy character_animator runtime was retired (skeletal animation unsupported)

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
