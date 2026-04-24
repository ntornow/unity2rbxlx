# Converter Gap Analysis TODO

Comprehensive list of gaps between current converter capabilities and Unity/Roblox features.
Priority: P0 = blocking gameplay, P1 = significant quality, P2 = nice to have.

## P0 — Blocking Gameplay

- [x] **Terrain voxel encoding**: SmoothGrid+PhysicsGrid now encoded into rbxlx directly. FillBlock Luau script kept as fallback. Tested with encoder unit tests.
- [x] **Sub-mesh material inheritance**: Verified working — Base/Weapon parts correctly receive material base_color via Color3uint8. 185/330 have full textures, rest use material color.
- [x] **SSS scripts not loading from rbxlx**: Fixed by changing RunContext from 0 (Legacy) to 1 (Server). Removed Workspace duplication workaround.
- [x] **Content properties**: Format matches Studio's `<Content><url>rbxassetid://...</url></Content>`. MeshLoader script provides reliable fallback with InsertService:LoadAsset + CreateMeshPartAsync resolution. Studio may need time to download recently-uploaded assets.

## P1 — Significant Quality

- [x] **Physics joints**: FixedJoint, HingeJoint, SpringJoint, CharacterJoint, ConfigurableJoint → Roblox constraints (WeldConstraint, HingeConstraint, SpringConstraint, BallSocketConstraint)
- [x] **Rigidbody constraints**: freezePosition/freezeRotation axes not mapped
- [x] **Character controller**: CharacterController → attributes + capsule sizing (full Humanoid integration deferred)
- [x] **NavMesh/NavMeshAgent**: NavMeshAgent speed/stoppingDistance extracted as attributes. PathfindingService usage in scripts.
- [x] **LOD Groups**: LODGroup children named LOD1+ are filtered, keeping only LOD0 (highest detail)
- [x] **Trail/Line renderers**: TrailRenderer → Trail, LineRenderer → Beam
- [x] **Reflection probes**: ReflectionProbe → gracefully skipped (no direct Roblox equivalent; global reflections handled by Lighting)
- [x] **Post-processing stack**: Bloom→BloomEffect, ColorGrading→ColorCorrectionEffect, DepthOfField→DepthOfFieldEffect, SunShafts→SunRaysEffect, + Atmosphere
- [x] **Skeletal animation**: SkinnedMeshRenderer → Motor6D chain with bone attributes
- [x] **Blend shapes**: No Roblox equivalent — skipped gracefully
- [x] **Cloth simulation**: No Roblox equivalent — skipped gracefully
- [x] **Wind zones**: No Roblox equivalent — skipped gracefully
- [x] **Video player**: VideoPlayer component → VideoFrame (SurfaceGui-wrapped) in Roblox
- [x] **Cinemachine**: VirtualCamera/FreeLook/Brain → camera config attributes on parts
- [x] **Timeline**: PlayableDirector component silently skipped (no direct Roblox equivalent). API mappings translate Play/Stop/Pause to TweenService/BindableEvent patterns. Timeline track data not parsed (would need .playable asset parsing).

## P2 — Nice to Have

- [x] **2D physics**: Rigidbody2D, BoxCollider2D, CircleCollider2D, CapsuleCollider2D → thin Part-based approximation
- [x] **Sprites/2D**: SpriteRenderer → thin colored Part with sprite GUID attribute
- [x] **UI layout groups**: GridLayoutGroup, VerticalLayoutGroup, HorizontalLayoutGroup → UIListLayout/UIGridLayout
- [x] **Canvas scaler**: CanvasScaler reference resolution → ScreenGui attributes for runtime scaling
- [x] **Advanced particle features**: Shape module, emission rate, colorOverLifetime, sizeOverLifetime, forceOverLifetime, rotationOverLifetime (VFX Graph/SubEmitters still TODO)
- [x] **Audio reverb zones/filters**: AudioReverbZone/AudioReverbFilter → ReverbSoundEffect with preset mapping
- [x] **Lightmaps**: Using Future lighting (Technology=3) with EnvironmentDiffuseScale=1.0 and EnvironmentSpecularScale=1.0. Baked lightmap texture data can't be directly imported — Future mode + ambient settings provide good approximation.
- [x] **Occlusion culling**: OcclusionArea/OcclusionPortal → Roblox handles natively (no action needed)
- [x] **Prefab child overrides**: Per-instance modifications now routed to correct child nodes by target fileID. Disabled components, material overrides, and custom field overrides propagated through hierarchy.
- [x] **Prefab variants**: Variant chain resolution with property override merging
- [x] **Binary scene support**: Handled via UnityPy. PrefabInstanceData construction fixed for binary scenes.
- [x] **Custom shaders**: Unsupported shaders fall back to Standard shader property extraction (_Color, _MainTex, _Metallic, etc.). ShaderGraph node graphs are not parsed but the output material properties are still read. Roblox material inferred from name + metallic value.
- [x] **Networking**: API mappings handle [Command]→RemoteEvent:FireServer, [ClientRpc]→RemoteEvent:FireAllClients, [SyncVar]→SetAttribute. NetworkBehaviour→Script. Roblox's built-in replication handles most cases natively.
- [x] **Terrain material variety**: Height-based material assignment (Sand→Grass→Mud→Rock→Slate by elevation+slope)
- [x] **Terrain height mismatch**: RESOLVED — was hitting floating MeshParts, not terrain. Terrain FillBlock positioning is correct. SmoothGrid binary format still needs reverse-engineering for direct embedding (currently using FillBlock script fallback).
- [x] **Terrain splat maps**: Read alpha textures from SplatDatabase, extract per-channel weights, map dominant layer to Roblox material via _LAYER_NAME_TO_MATERIAL lookup. Falls back to height-based when splat data unavailable.
- [x] **Terrain details**: Detail prototypes parsed from TerrainData. No Roblox equivalent for terrain grass billboards. Tree instances would map to placed Models but no test projects have tree data. Terrain holes not supported (Roblox terrain is always solid).
- [x] **Material tiling/offset**: _MainTex_ST scale/offset extracted and stored as _TilingX/_TilingY/_OffsetX/_OffsetY attributes

## Recently Completed (2026-03-24)

- [x] **Script transpilation coverage**: Removed aggressive Standard Assets auto-stub. 22 scripts now properly transpiled instead of stubbed.
- [x] **Script type coherence**: Added client/server API detection to auto-reclassify scripts as LocalScript/Script/ModuleScript.
- [x] **Luau API mistake fixer**: Added fixes for semicolons, compound assignment (+=), new Vector3(), .Destroy() dot syntax, .gameObject, .transform.position.
- [x] **Unknown component warning**: Added `_SILENT_SKIP_TYPES` and `else` clause for logging unhandled component types.
- [x] **Validator class coverage**: Added all emitted Roblox classes to valid class lists (Trail, Beam, Attachment, constraints, effects, etc.)
- [x] **Scene path resolution**: Fixed `--scene` relative paths to resolve against project root, not CWD.
- [x] **Part.Shape capitalization**: Fixed `"shape"` → `"Shape"` in rbxlx XML output.
- [x] **Reflectance**: Added `reflectance` property to RbxPart, mapped from Unity _Metallic value.
- [x] **Input.GetButton mappings**: Added GetButton/GetButtonDown/GetButtonUp to API call map.
- [x] **LOD Group filtering**: LODGroup children LOD1+ skipped, keeping only LOD0 (highest detail).
- [x] **NavMeshAgent attributes**: Speed, stopping distance extracted as Roblox attributes.
- [x] **Animator controller GUID**: Controller GUID extracted for animation script targeting.
- [x] **SpriteRenderer**: Converted to thin colored Part with sprite GUID attribute.
- [x] **2D physics**: Rigidbody2D, BoxCollider2D, CircleCollider2D, CapsuleCollider2D handled.
- [x] **Constraint Part0 references**: WeldConstraint Part0 referent set from parent part.
- [x] **Future lighting**: Lighting.Technology set to Future (3) for best visual fidelity.
- [x] **UnityEvent mappings**: AddListener → Event:Connect, Invoke patterns mapped.
- [x] **MonoBehaviour script class**: Script class name resolved from m_Script GUID for part binding.
- [x] **Conversion report**: Added script type breakdown, terrain/GUI counts.
- [x] **Binary scene analysis**: Analyze command now parses binary scenes via UnityPy.
- [x] **Workspace.Gravity**: Set to 196.2 studs/s² (standard Roblox gravity).
- [x] **FallenPartsDestroyHeight**: Set to -500 studs to clean up fallen physics objects.
- [x] **StarterCharacterScripts**: Folder now created in rbxlx output.
- [x] **Default camera fallback**: Camera at (0,10,20) with 70° FOV when no Unity camera found.
- [x] **FPS camera mouse look**: Full first-person camera with mouse delta, pitch/yaw limits, scriptable camera.
- [x] **WASD movement**: Camera-relative movement direction via Humanoid:Move().
- [x] **Jump support**: Space bar triggers Humanoid.Jump.
- [x] **GameServerManager**: Auto-injected server script handles spawn points, character init, walk speed.
- [x] **MeshLoader += bug**: Fixed Luau `+=` syntax errors in MeshLoader script.
- [x] **Runtime script += bugs**: Fixed `+=` in nav_mesh_runtime.luau.
- [x] **Terrain SmoothGrid**: Disabled binary embedding (voxel order needs reverse-engineering). FillBlock TerrainGenerator script embedded in rbxlx instead.
- [x] **ParticleEmitter properties**: Added Drag, LockedToPart, Acceleration, VelocityInheritance extraction and serialization.
- [x] **Unity Layer → attribute**: Scene node layer extracted as UnityLayer attribute for CollisionGroup mapping.
- [x] **AI system prompt**: Added compound assignment warning, UnityEvent, PlayableDirector, NavMeshAgent, Rigidbody velocity patterns.
- [x] **CollisionGroup script**: Auto-injected when Unity layers detected, maps UnityLayer attributes to CollisionGroups.
- [x] **AI confidence scoring**: Penalizes residual C# (GetComponent<>, void, +=) instead of just boosting.
- [x] **Trail Attachments**: Trails now auto-create Attachment0/Attachment1 with proper Ref bindings.
- [x] **Beam Attachments**: Beams now auto-create Attachment0/Attachment1 with proper Ref bindings.

## Completed

- [x] **FBX fallback sizing**: When native sizes unavailable, uses FBX import_scale × unit_ratio × STUDS_PER_METER instead of fixed default
- [x] **PSD MIME type fix**: Added .psd and .tif to MIME type map in cloud_api.py
- [x] **Shader name resolution**: Material mapper now resolves shader GUIDs to file names (fixes water shader detection)
- [x] **Water shader detection refactored**: Extracted _is_water_node() helper, now works for both scene nodes and prefab instances
- [x] **Coordinate system verified**: Position Z-negation, quaternion X/Y-negation, ZXY euler order all correct
- [x] **Physics joints**: FixedJoint→WeldConstraint, HingeJoint→HingeConstraint, SpringJoint→SpringConstraint, CharacterJoint/ConfigurableJoint→BallSocketConstraint
- [x] **Post-processing stack**: Bloom→BloomEffect, ColorGrading→ColorCorrectionEffect, DepthOfField→DepthOfFieldEffect, SunShafts→SunRaysEffect, + Atmosphere
- [x] **Trail/Line renderers**: TrailRenderer→Trail, LineRenderer→Beam
- [x] **UI layout groups**: VerticalLayoutGroup→UIListLayout, HorizontalLayoutGroup→UIListLayout, GridLayoutGroup→UIGridLayout
- [x] **Rigidbody freeze constraints**: freezePosition/freezeRotation bitmask → anchored when all position frozen
- [x] **Character controller**: CharacterController → capsule sizing + attributes
- [x] **Canvas scaler**: Reference resolution extracted for UI scaling (P2)
- [x] **Graceful skip types**: ReflectionProbe, LightProbeGroup, OcclusionArea, Cloth, WindZone, LensFlare silently skipped
- [x] **Material name inference expanded**: Added gold/silver/bronze/copper/chrome, asphalt/road, rust/corrode, cobble, snow/mud/slate, leather/cloth/carpet + 15 more keywords
- [x] **Metallic-based material inference**: Materials with _Metallic > 0.5 → Metal, > 0.2 → SmoothPlastic (125 parts now correctly Metal)

## New Gaps (2026-03-26)

- [x] **SmoothGrid binary format**: Fully reverse-engineered — 6-bit material + optional occupancy + RLE, axis swap (SmoothGrid Z = world Y), all 22 material IDs confirmed. Encoder implemented in terrain_encoder.py. FillBlock script kept as fallback.
- [x] **Mesh InitialSize**: Handled via 3-tier sizing (Studio-resolved → trimesh FBX bbox → naive estimate) + headless place builder's `CreateMeshPartAsync` captures exact InitialSize server-side.
- [x] **Prefab hierarchy orphans**: FIXED — 0 orphans now. Added lazy containers for inactive scene nodes + stripped Transform ID registration + root-level PI handling.
- [x] **Parse performance**: Switched to CSafeLoader (C YAML parser). Gamekit3D: 65s→12s (81% faster). Test suite: 220s→92s (58% faster).
- [x] **Multi-scene conversion**: `--scene all` converts every scene to its own .rbxlx file with shared assets
- [x] **Nested project auto-detection**: Pipeline auto-finds Unity root when Assets/ is one level deep (ChopChop, PrefabWorkflows)
- [x] **Visual comparison automation**: Infrastructure exists — `u2r.py compare --visual` supports Unity/Roblox screenshot capture, camera position matching (`unity_camera_to_roblox`), viewport cropping, and SSIM computation with diff heatmap. The "automation" gap was about UX polish (auto-capture via MCP without manual screenshot), not missing functionality. Deferred as low priority.
- [x] **Play mode testing**: Partially addressed — playtest subagent integration works (verified in 2026-04-12 session), `playtest-gotchas.md` documents 7 caveats, `TestRiflePickupChainValidator` provides regression coverage. Full automated gameplay harness deferred.
- [x] **Rule-based transpiler receiver resolution**: Fixed 2026-04-12: standalone `transform.X` now resolves to `script.Parent.X` instead of bare `.X`, and `obj.transform.X` resolves to `obj.X` instead of `obj..X` (double dot). Same for `gameObject.X`. 4 new unit tests in `TestRuleBasedReceiverResolution`. Full rule-based quality for complex scripts still has gaps (bare `:` calls, missing function parentheses), but the receiver fix addresses the most impactful class of errors.
- [x] **Multi-sub-mesh FBX instances**: Fixed 2026-04-12 in both `_convert_node` (scene instances) and `_convert_fbx_prefab_instance` (FBX-as-prefab). Both paths now create a Model with child MeshParts when mesh_hierarchies has 2+ entries. Materials are propagated to each child. Fence renders correctly with chainlink texture.
- [x] **Alpha-mode transparency for chainlink/grid textures**: Investigated 2026-04-12. The fence's Unity material has `_Mode: 0` (Opaque) — the chain-link pattern comes from mesh geometry (polygon holes), not texture alpha. The material mapper already handles `_Mode: 1` (Cutout) → `AlphaMode=Overlay` and `_Mode: 2/3` (Fade/Transparent) → `AlphaMode=Transparency` correctly. The fence renders correctly after the multi-sub-mesh fix — the rust-colored texture is applied to mesh faces, and gaps between the diamond pattern are actual geometry holes. No additional alpha detection needed for this case.

## New Gaps (2026-03-28)

- [x] **Rigidbody physics properties**: Mass/drag/angularDrag extracted → CustomPhysicalProperties (density/friction/elasticity). Rigidbody2D m_LinearDrag + m_GravityScale also handled.
- [x] **MeshCollider CollisionFidelity**: m_Convex → Hull, non-convex → PreciseConvexDecomposition. Serialized in rbxlx.
- [x] **Silent PSD/TGA conversion errors**: Bare `except: pass` replaced with log.warning.
- [x] **Cinemachine camera runtime**: New cinemachine_runtime.luau — reads VCam attributes, does camera follow/look-at/FOV transitions. Auto-injected when CinemachineVCam detected.
- [x] **Test suite performance**: Slow tests (CLI subprocess + full Gamekit3D conversion) marked @slow. Fast suite: 872 tests in 9.4s. Full suite: 888 tests in 65s.

## Remaining Open Items

### Not Yet Implemented (genuine gaps)
- [x] **Tilemap/TilemapRenderer**: Tiles converted to thin Parts in a grid with cell sizing, tile colors, sprite GUIDs. TilemapRenderer properties extracted.
- [x] **write_output performance for script-heavy projects**: Fixed 2026-04-12. Root cause: catastrophic regex backtracking in `luau_validator.py:5334` — the if-expression paren-unwrapping pattern `(\((?:[^()]*|\([^()]*\))*\))` caused exponential backtracking on deeply-nested expressions. SanAndreasUnity: 13+ min → 9.1s. Gamekit3D: 30+ min → 20.6s. Also fixed terrain encoder inlining (SimpleFPS write_output 8.0s → 3.4s).
- [x] **Object pooling runtime shim**: Fixed 2026-04-12. New `runtime/object_pool.luau` module provides `ObjectPool.new(template, initialSize)`, `:Get()`, `:Release(obj)`, `:Clear()`. Auto-injected when pool patterns detected in transpiled scripts. API mappings added for `.Release()` and `ObjectPool.Get()`.
- [x] **TODO placeholders in transpiled scripts**: Fixed 2026-04-12. Eval framework now tracks `todo_placeholders` and `csharp_residue` per project. Results: 0 C# residue across all 9 projects. TODO counts: SimpleFPS=2, Gamekit3D=13, SanAndreasUnity=335 (GTA SA loader complexity). These are tracked in eval-diff as lower-is-better metrics so future improvements reduce the count.
- [x] **OnCollisionStay/OnCollisionExit**: Fixed 2026-04-12. OnCollisionStay/OnTriggerStay → part.Touched, OnCollisionExit/OnTriggerExit → part.TouchEnded. Both were already in api_mappings; Stay variants added. The per-frame semantic gap (Unity Stay fires every FixedUpdate, Roblox Touched fires once per contact) is inherent to the platform — noted in mapping comments.
- [ ] **Binary animation/controller parsing**: .anim and .controller files are skipped when binary-encoded. Affects ~40% of games with skeletal animation. Needs UnityPy integration or binary YAML parser.
- [ ] **Persistent prefab/asset cache**: Prefab library is in-memory only. SQLite or pickle cache keyed by (GUID, mtime) would halve pipeline time for multi-scene projects and large games.
- [x] **Font upload**: Roblox API limitation (not actionable). UI text uses default Roblox font. Users can manually upload fonts via Creator Dashboard.
- [x] **Video upload**: Roblox API limitation (not actionable). VideoFrame component emitted correctly but video ID must be set manually after upload via Creator Dashboard.
- [x] **Eval baseline for all 9 projects**: Fixed 2026-04-12. All 9 projects complete in 85s total. `eval_baseline.json` committed with per-project metrics. `u2r.py eval-diff` can gate future changes. **Open follow-up:** wire eval-diff into CI nightly job.

### Fixed (2026-03-28 continued)
- [x] **Skeletal animation bone resolution**: Motor6D now creates actual bone Parts with proper Part0/Part1 Ref links (was string-only names)
- [x] **Cross-scene constraint linking**: Pre-pass assigns referents from unity_file_id, constraints resolve Part1 via global mapping
- [x] **Animator state machine**: Controllers with 2+ states and transitions → unified state machine script with parameter-driven transitions, trigger reset, exit-time support
- [x] **VFX SubEmitters**: New sub_emitter_runtime.luau handles Birth/Death/Collision triggers with burst effects, auto-injected when _HasSubEmitters detected

### Fixed (2026-03-29)
- [x] **Sprite atlas cropping**: .meta sprite rects parsed (x,y,w,h), SurfaceGui+ImageLabel with ImageRectOffset/Size for atlas sprites. Decal fallback for full textures.
- [x] **API mappings**: 5 comment-only entries replaced with actual code (navMeshAgent.speed, SetDestination, isStopped, ResetTrigger, Quaternion.FromToRotation). 2 new utility functions (navMoveTo, quatFromToRotation).
- [x] **Animation data export**: export_controller_json + export_clip_keyframes wired into pipeline. Generates AnimationData_{name} ModuleScripts in ReplicatedStorage. Animator runtime has TweenService-based bone animation fallback.
- [x] **NavMeshObstacle**: Now extracted with shape/size/carve attributes instead of silently skipped.
- [x] **CanvasGroup**: Alpha → _GroupTransparency, Interactable → _GroupInteractable attributes.
- [x] **ContentSizeFitter**: HorizontalFit/VerticalFit stored as _AutoSizeH/_AutoSizeV attributes.
- [x] **AspectRatioFitter**: AspectRatio + AspectMode stored as attributes.
- [x] **PlayableDirector**: Timeline properties extracted (AutoPlay, Loop, Duration, AssetGuid) instead of silently skipped.
- [x] **Post-processing**: Vignette, AmbientOcclusion, MotionBlur, ChromaticAberration extracted from URP Volume settings.
- [x] **Emission materials**: _EmissionColor → Neon material + emission color applied to part.
- [x] **Roughness maps**: Standalone _RoughnessMap/_SmoothnessMap fallback in material mapper.
- [x] **Default SpawnLocation**: Auto-created (invisible) if no SpawnLocation exists in scene.
- [x] **CollectionService tags**: Unity m_TagString → Roblox Tags property (BinaryString).
- [x] **CollisionGroups**: Unity layer → Roblox CollisionGroup string (UnityLayerN).
- [x] **CastShadow**: Unity MeshRenderer m_CastShadows=0 → Roblox CastShadow=false.
- [x] **MonoBehaviour field extraction**: m_ prefix fields now extracted (was filtered), GameServerManager reads MaxHealth/maxHitPoints → Humanoid health.
- [x] **WheelCollider**: Converted to cylinder Part with radius-based sizing.
- [x] **Cross-script duplicate warning**: Logs when two scripts share a class name.
- [x] **YAML error logging**: Malformed documents now warn instead of silently dropping.

### Fixed (2026-03-29 continued)
- [x] **SmoothGrid terrain verified**: Loaded in Studio — terrain renders correctly with proper materials and height. 17 new byte-level validation tests added.
- [x] **Mesh InitialSize fallback**: trimesh-based FBX bounding box extraction. 3-tier sizing: Studio-resolved → FBX bbox → naive estimate.
- [x] **Visual comparison**: Pure-numpy SSIM (no skimage), camera coordinate matching, `compare --visual` CLI command. 13 tests.
- [x] **Multi-MonoBehaviour binding**: `_ScriptClass` now uses numbered attributes so all scripts on a GameObject get bound (was only keeping last one).
- [x] **Client-only require propagation**: Scripts requiring modules with client APIs auto-reclassified to LocalScript.
- [x] **Humanoid:Move()**: Removed incorrect validator rewrite of `:Move()` → `.MoveDirection =` (MoveDirection is read-only).
- [x] **Trailing comma after bare-var comments**: Fixed syntax error in `CFrame.Angles(x, y, -- [bare var] 0)` pattern.
- [x] **BasePart parent guards**: Unbound prefab scripts get `IsA("BasePart")` guard to prevent SSS crashes.
- [x] **SpawnPoint → SpawnLocation**: Unity SpawnPoint objects now convert to Roblox SpawnLocation class with correct positions.

### Remaining
- [x] **Prefab child MonoBehaviour binding**: Fixed — `_process_components()` now runs on all prefab child nodes via recursive `_convert_prefab_node`, setting `_ScriptClass` correctly (224 parts bound, 0 orphan scripts)

### Deferred (no Roblox equivalent)
- Cloth simulation → silently skipped
- Wind zones → silently skipped
- Blend shapes → silently skipped
- Reflection probes → silently skipped (Future lighting compensates)
- Light probes → silently skipped (Future lighting compensates)

## Open Gaps (2026-04-12 session)

Catalogued after the SimpleFPS rifle pickup end-to-end fix and PR #1 merge.
Priority: `P0` = blocks gameplay, `P1` = correctness / maintainability, `P2` = nice to have.

### Gameplay / runtime
- [x] **P0 — music1.mp3 HTTP 403 after upload.** Fixed 2026-04-12: new `probe_asset_availability()` in `cloud_api.py` hits the assets metadata endpoint with 429 retry/backoff and classifies results as `approved`/`rejected`/`unknown`. New `u2r.py audit-assets` CLI command sweeps every entry in `uploaded_assets`, throttling to 1.1s/call to avoid rate-limit misclassification, and writes `asset_audit.json` with a breakdown. Confirmed working against live SimpleFPS: found 2 real rejections (music1.mp3 = `rbxassetid://105677099883784`, prop_keycard_dff.tif = `rbxassetid://79373326136923`) out of 194 uploads. 8 new unit tests in `TestProbeAssetAvailability`. **Open follow-up:** wire audit into `upload_assets` phase so rejections are auto-stripped before the rbxlx is written.
- [x] **P1 — Pickup `Touched` spam.** Fixed 2026-04-12: validator now injects a `local _fired = false` debounce at script-init and short-circuits the fire handler on re-entry.
- [x] **P0 — Animation asset 404 audit not run.** Covered 2026-04-12 by the same `u2r.py audit-assets` command — it sweeps every entry in `uploaded_assets`, so animation uploads are checked alongside audio and textures. The SimpleFPS sweep found 2 rejections; none of them were animations. **Open follow-up:** bake the audit into the pipeline so rejected asset IDs never reach the rbxlx writer.
- [x] **P1 — No production-grade shoot verification path.** Fixed 2026-04-12: new `TestRiflePickupChainValidator` in `test_code_transpiler.py` hand-crafts the AI-transpiler output shape for Pickup.cs and Player.cs, runs it through `validate_and_fix`, and asserts every marker the runtime depends on (`_PICKUP_REMOTE_INIT`, `_fired` debounce, walk-up lookup, `_REMOTE_PICKUP_LISTENER`, `_SETUP_SOUNDS_BROAD`, `gotWeapon` early-return, no `_isMouseButtonDown` guard). Runs in the fast suite, so a regression in the validator would trip it in <1s.

### Pipeline / converter correctness
- [x] **P1 — FBX sub-mesh materials only resolve the *first* `m_Materials` entry.** Fixed 2026-04-12: new `_extract_prefab_material_map()` helper walks the prefab YAML with two regex passes (GameObject → fileID → name, MeshRenderer/SkinnedMeshRenderer → GO fileID → material guid) and the sub-mesh build loop looks up each mesh's material by name, falling back to the first-seen guid when a name isn't in the map. 3 new unit tests in `TestExtractPrefabMaterialMap`.
- [x] **P1 — `_material_mappings` module-level global.** Fixed 2026-04-12: now threaded as an explicit `material_mappings` kwarg on `_extract_monobehaviour_attributes`; the module-level global remains only as a fallback for legacy callers.
- [x] **P1 — `script:GetAttribute("X")` walk-up rewrite is over-broad.** Fixed 2026-04-12: validator now only rewrites matches of `^\s*local\s+\w+\s*=\s*script:GetAttribute("…")$` (top-of-script serialized-field reads), and explicitly skips any attribute whose name also appears in a `script:SetAttribute(...)` earlier in the file. 3 new unit tests in `TestScriptGetAttributeScoping`.
- [x] **P2 — `_project_paths.py` hardcodes `../unity-3d-simplefps` external fallback.** Fixed 2026-04-12: replaced the hardcoded per-project fallback with a `UNITY2RBXLX_TEST_PROJECTS_ROOT` env var. If set, the resolver checks `$UNITY2RBXLX_TEST_PROJECTS_ROOT/<Name>` as a fallback when the submodule is uninitialized; if unset, only the submodule path is consulted. Docstring updated with an example.
- [x] **P2 — `convert_interactive.py preflight` lists `lxml` and `lz4` as hard deps.** Fixed 2026-04-12: dropped both from the `required` dict. Comment explains the rule ("keep in sync with actual imports under real source").
- [x] **P2 — 12 phase-4.5 doc staleness items still unresolved.** Fixed 2026-04-12: added a "Last verified" blockquote header to all 8 `phase-4.5-*.md` files, pointing at commit `e19a342` and the audit in TODO.md. 3 of the 15 audit findings were directly corrected (2048 stud cap location, runtime module count, pickup_runtime removal); the remaining 12 are now flagged for readers via the header so they cross-check before acting.

### Infrastructure / observability
- [x] **P1 — CI doesn't run the slow suite.** Fixed 2026-04-12: added a `slow` job to `.github/workflows/test.yml` that runs on `schedule: "0 7 * * *"` (07:00 UTC nightly) and on `workflow_dispatch`. Fetches submodules recursively, 30-minute timeout, runs `pytest tests/ -v` (no marker filter). Doesn't run on every push/PR — the submodule clone is multi-GB and the suite is ~65s.
- [x] **P1 — `upload_audio` does not verify playability.** Fixed 2026-04-12: the `upload_assets` pipeline phase now calls `probe_asset_availability` on every newly-uploaded asset (audio, textures, meshes) after the upload loop finishes and strips any entries that come back rejected. Not inline in `upload_audio` itself — batch audit is faster and gives one unified code path for all asset kinds.
- [x] **P0 — No regression fixtures for the rifle pickup → equip → shoot chain.** Fixed 2026-04-12 via `TestRiflePickupChainValidator` (see P1 entry above). Tried a pipeline-level test first but rule-based transpilation produces a different script shape than the AI path, so the validator fixes only fire on the AI-transpiled input. The unit test hand-crafts that AI-shape fixture instead — cheaper and more targeted.
- [x] **P2 — Studio MCP `require()` module-instance caveat undocumented.** Fixed 2026-04-12: new `playtest-gotchas.md` reference file documents 7 hard-won caveats from the SimpleFPS session — `require()` module-instance separation, `user_mouse_input` coordinate confusion, Touched spam, live-Source doesn't reload closures, Studio disconnection, rbxlx reload recipe, `character_navigation` path-blocked workaround. Added to INDEX.md.

### Scope / project
- [x] **P1 — No second project end-to-end-verified this session.** Fixed 2026-04-12: ran eval across 8 projects (all except Gamekit3D). 6 completed successfully (3D-Platformer, BoatAttack, BossRoom, ChopChop, PrefabWorkflows, RedRunner) with 100% script transpilation rates. SanAndreasUnity and Gamekit3D have slow write_output phases being profiled. Initial eval baseline committed. SimpleFPS verified in Studio with gameplay testing.
- [x] **P2 — `converter/output/<project>/conversion_context.json` holds real IDs but is only protected by `.gitignore`.** Fixed 2026-04-12: new `ConversionContext.save_sanitized()` writes a redacted copy — strips `universe_id`, `place_id`, `experience_name`, `uploaded_assets`, `mesh_native_sizes`, `mesh_hierarchies` — and stamps a `_sanitized: true` marker. Preserves stats, phase completion, warnings, Unity project path. 2 new tests in `TestConversionContextSanitizedSave`. The regular `.save()` still writes everything for pause/resume.

## Rendering Differences (2026-04-12 visual comparison)

- [ ] **Mesh Z-axis mirroring**: Unity left-handed → Roblox right-handed coordinate conversion negates Z positions but doesn't mirror mesh geometry. All asymmetric features (text, door handles) render backwards. Fix options: (a) pre-rotate each mesh 180° around Y before upload, (b) apply negative Z scale on the CFrame (may not work for MeshParts), (c) re-export FBXes with mirrored geometry.
- [ ] **Wire/grid mesh opacity**: Chain-link fences and similar thin-geometry meshes render as opaque in Roblox because the mesh renderer fills sub-pixel gaps between wires. Unity renders these gaps correctly. The texture alpha (87.7% transparent for chainlink.psd) could compensate via AlphaMode=Transparency, but Unity's material has _Mode=0 (Opaque), so we'd be changing the intended rendering mode. Documenting as a platform rendering difference.

## Open Gaps (2026-04-14 session — inline-over-runtime-wrappers)

Deferred follow-ups from removing the seven rejected runtime bridges. See
`docs/design/inline-over-runtime-wrappers.md` for the governing policy and
the full list of what was removed.

- [x] **P1 — Consolidate animator runtime modules.** Fixed 2026-04-17:
  merged unique features from `animator_bridge.luau` (blend trees, getter
  methods, `Play()`, Any-state transitions, lazy track loading, `Destroy()`)
  into `animator_runtime.luau`. Deleted `animator_bridge.luau` (redundant
  state machine) and `TransformAnimator.luau` (redundant with
  `animation_converter.py` TweenService output). Regression guard in
  `test_no_rejected_bridges.py` extended to cover both deleted files +
  assert consolidated features remain.
- [x] **P1 — Rewrite phase-3 commit `10c786c` on `origin/merge-phase3` to
  drop bridge injection.** Closed 2026-04-17 by abandoning the branch
  and landing the six wanted pieces as fresh commits on main against
  current pipeline.py. See `docs/design/merge-plan-phase-3-augmented.md`
  for the full audit of Phase 3's 12 items. Landed: binary writer wiring
  + content-type auto-detect + `lz4` dep (item 6), report_generator
  adoption (item 7), sprite_extractor wiring (item 3),
  scriptable_object_converter wiring + disk persistence (item 5),
  rehydration reads `conversion_plan.json` (item 12), mesh_splitter
  deletion (item 4). The `merge-phase3` branch is now obsolete and
  should be deleted.
- [x] **P2 — `Input.GetSwipe` has no test-project coverage yet.** Accepted
  2026-04-17 as speculative infrastructure. The mapping + utility function +
  regression guard (`test_no_rejected_bridges.py` asserts
  `API_CALL_MAP["Input.GetSwipe"] == "getSwipe"` and
  `"getSwipe" in UTILITY_FUNCTIONS`) all exist. Real end-to-end coverage
  will come when a mobile Unity game enters the eval set.

## Phase 3 merge plan — deferred items (2026-04-17 session)

The Phase 3 plan at
`https://github.com/jiazou/unity-roblox-game-converter/blob/main/MERGE_PLAN.md`
has 12 items; 6 landed in this session, 2 were closed as superseded
(items 1 and 8), and the following 4 are deferred. See
`docs/design/merge-plan-phase-3-augmented.md` for the full audit.

- [ ] **P2 — Phase 3 item 2: Vertex color baking.** Module
  (`converter/vertex_color_baker.py`, 572 LOC) exists but is unwired.
  Real gap — vertex-color-only materials currently fall back to Color3
  or default gray (documented in `phase-3-materials.md` lines 35-37).
  Needs a discovery pass first: which of the 9 test projects actually
  have vertex-color-only materials? Without a project that exercises it,
  wiring is unsafe. Action: audit `.mat` files across test projects for
  `colors:` vertex-color references with no albedo texture; if any
  project hits, add a `uses_vertex_colors` flag to `MaterialMapping`
  and invoke `bake_vertex_colors_batch` after `convert_materials`.
- [ ] **P2 — Phase 3 item 9: `extract_serialized_field_refs()`.** Not
  ported. Was a dependency of item 10 (prefab packages). Defer with
  item 10.
- [ ] **P2 — Phase 3 item 10: `generate_prefab_packages()`.** Not
  ported. Current approach uses in-memory `prefab_library` + inline
  prefab expansion; per-prefab packages (for ReplicatedStorage/Templates
  cloning) would enable runtime prefab spawning but need an architecture
  pass — where do packages live, how does the spawner script reference
  them, how does cross-scene prefab reuse work. Not in Phase 3 scope;
  revisit when a test project requires runtime prefab spawning.
- [ ] **P3 — Phase 3 item 11: disk rewrite for `packages/`.**
  Closed for `animations/`, `animation_data/`, and
  `scriptable_objects/` by the 2026-04-24 `source_path` work: every
  `RbxScript` created by the fresh-write or rehydrate path records its
  relative disk location, and the final rewrite loop in `write_output`
  writes back via `source_path` instead of the old top-level +
  `animations/` heuristic. Only `packages/` remains, and it depends on
  item 10 (`generate_prefab_packages`) landing first.

## 2026-04-24 session — Codex review closures

Independent review by OpenAI Codex CLI surfaced 9 findings graded
P0 / P1 / P2. All shipped upstream via PRs #19, #20, #21.

- [x] **P0-1 — `transpile` → `validate` workflow broken.** `transpile`
  now persists Luau to `scripts/*.luau` so the subsequent `validate`
  command finds them. Commit `03e6bff`.
- [x] **P0-2 — `_make_pipeline` cross-project regression.** Deferred-fix
  C3 had regressed after the original landing in `86392e6`. Re-landed
  with a guard + three regression tests. Commit `ba560e2`.
- [x] **P0-3 — Rehydration not lossless across nested subdirs.** Added
  `RbxScript.source_path`; every fresh-write and rehydrate site now
  records it; final rewrite in `write_output` honors it instead of the
  top-level-plus-`animations/` heuristic. Closed Phase 3 item 11 for
  existing subdirs and item 12 entirely. Commit `0292f79`.
- [x] **P1-4 — `assemble` silently no-ops without creds + missed
  `moderate_assets`.** Pre-flight cred check + `_resolve_credential`
  auto-discovery + `moderate_assets` added to the phase list.
  Commit `ed6596d`.
- [x] **P1-5 — Phase 3 extractors swallowed exceptions.** Broadcast to
  `log.warning` + appended to `ctx.warnings` so the final report
  surfaces the failure instead of silent drop. Commit `217bbc3`.
- [x] **P1-6 — Missing regression tests.** Added `resume` vs
  `_run_through` parity (source-level + behavior-level) and three-flow
  phase-order parity. Commit `420b01e`. Three-flow rbx_place
  byte-equivalence still deferred — needs a real Unity fixture.
- [x] **P2-7a — Broad-catch on report JSON.** Narrowed and surfaced
  to stderr. Commit `c9bf537`.
- [x] **P2-7b — Multi-paragraph module docstrings.** `report_generator`
  and `scriptable_object_converter` compressed to one-line WHYs.
  Commit `7581421`.
- [x] **P2-7c — Dead `experience_manager` module.** Deleted the module
  and the `create_experience` shim (237 lines of unreachable code).
  Commit `5ea4c60`.
- [x] **Item 5 tail — ScriptableObject `source_path` miss.** Found
  while auditing the Codex plan-execution table. One-line fix on the
  fresh-write attach at `pipeline.py:1263`. Commit `bccb7a5` (PR #21).
- [x] **Item 7 tail — `convert_interactive.report` inline JSON.**
  Routed through a new `report_generator.augment_report(path, extras)`
  helper so both callers go through one reporting path. Four new tests
  in `TestAugmentReport`. Commit `79a517c` (PR #21).

Plus three prerequisite fixes from earlier in the same session:

- [x] **Open Cloud `create_experience` endpoint unreachable.** Stopped
  chasing `universes/v1/universes/create` (requires ROBLOSECURITY
  cookie + XSRF). `pipeline.resolve_assets` now emits actionable
  instructions directing users to pre-create a universe/place and pass
  `--universe-id` / `--place-id`. Commit `9ed7daa`.
- [x] **UnityPy undeclared dependency.** Added to `pyproject.toml` —
  fresh `pip install -e .` no longer silently ships empty terrain.
  Commit `11dc5c9`.
- [x] **`.context/` ungitignored.** Per-workstation AI-assistant state
  added to `.gitignore`. Commit `8fc45e1`.

## Phase 4 merge plan — execution log (2026-04-24 session)

Plan sources:
`https://github.com/jiazou/unity-roblox-game-converter/blob/main/MERGE_PLAN.md`
`https://github.com/jiazou/unity-roblox-game-converter/blob/main/MERGE_PLAN_PHASE4.md`

Rollback point tagged before PR 1: `phase4-rollback-point` (at
commit `385c669`). Six-PR sequence: PR 1 (4.1/4.6/4.7/4.11),
PR 2 (4.5 — animation routing), PR 3 (4.2/4.8 — materials +
vertex color), PR 4 (4.3/4.9), PR 5 (4.10), PR 6 (4.4 diagnostics).

### PR 1 — 4.1 / 4.6 / 4.7 / 4.11

Dest-drift audit (against `main` at `385c669`) shrank PR 1 from
the plan's ~260 lines to ~80. Scope decisions:

- **4.1 api_mappings — SKIPPED.** Dest (1071 lines) is the canonical
  superset; source (492 lines) adds no keys dest lacks, and its
  animator control entries (`animatorBridge:SetBool` etc.) violate
  the inline-over-runtime policy that dest's `:SetAttribute` mapping
  already honors. Regression guard stays in
  `test_no_rejected_bridges.py`.
- **4.6 ui_translator — partial port.** Added `_FONT_MAP` (Unity
  font name → Roblox `Enum.Font` label), `_TEXT_ANCHOR_X`/`_Y`
  (9-point TextAnchor split), MonoBehaviour UI-Image script-GUID
  fallback (`fe87c0e1...`), and partial-anchor mixed-stretch warning
  in `_extract_rect_transform`. `RbxUIElement` gained
  `text_x_alignment` / `text_y_alignment` / `font` fields;
  `rbxlx_writer` emits the corresponding tokens only when set.
  Y-inversion audit: dest's two-branch
  logic is semantically equivalent to source's anchor-center form;
  no behavior change.
- **4.7 scene_parser — 2 small additions.** New
  `ParsedScene.referenced_animator_controller_guids: set[str]` +
  `ParsedScene.parse_warnings: list[str]`. Pass 4 in
  `scene_parser.py` aggregates `m_Controller` GUIDs off Animator
  components (classID 95) so 4.5 can enumerate controllers in one
  pass instead of walking every part. `parse_documents` now takes
  an optional `warnings_out` list so scene YAML errors reach the
  final conversion report instead of being logger-only. Existing
  per-part `AnimatorController` attribute in `scene_converter.py`
  kept; the new set is additive.
- **4.11 disk rewrite — test-only.** The 2026-04-24 `source_path`
  work (commit `0292f79`) already made rehydration generic across
  any subdir, so `animation_data/` is covered. Added a focused
  round-trip test for that subdir; `packages/` emission remains a
  follow-up deferred until 4.10 actually writes to it.

### Deferred follow-ups from PR 1

- **4.11.packages** — `packages/` subdir emission + rehydration
  round-trip test. Deferred until PR 5 (4.10
  `generate_prefab_packages`) lands. Without 4.10, nothing writes
  there so the dir never exists.
- **TMP alignment** — `m_HorizontalAlignment` / `m_VerticalAlignment`
  bitfields on TextMeshPro components aren't split into
  `text_x_alignment` / `text_y_alignment` yet; only legacy
  `m_Alignment` (single 0..8 enum) is handled. Revisit if a test
  project exercises TMP-only text layout issues.

### PR 2a — 4.5 animation routing engine

Scope: the "core" half of Phase 4.5 (routing predicate, blend trees,
per-clip routing, persistence, scene-scoped naming, parsed_scenes
consumption). Robustness polish split into PR 2b.

- **UNITY_TO_R15_BONE_MAP + `AnimClip.is_transform_only` predicate.**
  20-entry Unity humanoid-bone → R15 part map, plus
  `AnimClip.bone_paths` populated on parse and an `is_transform_only`
  property that strips `Armature|` prefixes when testing paths.
- **BlendTree/BlendTreeEntry dataclasses + 1D parsing.** Replaced the
  "first-child fallback" hack in `parse_controller_file` with a
  proper `_parse_blend_tree` resolving `m_Childs` into
  `BlendTreeEntry`. 2D blend trees log a warning, emit nothing, and
  keep a first-leaf `clip_guid` on the state as runtime fallback.
  Nested blend trees inline their first leaf.
- **`export_controller_json` emits `blendTrees`** matching the schema
  `runtime/animator_runtime.luau` already consumes:
  `{name → {param, clips: [{clip, threshold}]}}`. Clip names resolve
  from GUIDs via a new `clip_name_by_guid` kwarg.
- **Per-clip routing + `AnimationConversionResult.routing`.** Each
  clip is routed once: humanoid → bundled in animator_runtime JSON;
  transform-only → inline TweenService Script. Orphan clips go
  inline. Routing + reason persisted to `conversion_plan.json`
  under `animation_routing`.
- **`parsed_scenes` consumption + scene-scoped naming.** When any
  parsed scene's `referenced_animator_controller_guids` set is
  non-empty, controllers are filtered and emitted as
  `AnimationData_{scene}_{controller}`. When every scene's set is
  empty (Animators live inside prefabs — the common case for
  SimpleFPS), fall back to unscoped emission.
- **No deleted-bridge requires.** New regression test in
  `test_no_rejected_bridges.py` asserts `generate_tween_script`
  output never references `AnimatorBridge` or `TransformAnimator`.

Verification: fast suite 591 passed; full SimpleFPS convert ran
clean in 1.9s — 7 transform-only inline scripts, 0 AnimationData
modules (no humanoid clips in SimpleFPS), `animation_routing`
populated with correct per-clip reasons, `u2r.py validate` 0 errors.

### Deferred from PR 2a → addressed in PR 2b or later

- **Prefab-scoped animator controller aggregation.** Scenes that
  only reach controllers through prefab instances have an empty
  `referenced_animator_controller_guids` set, so scene-scoped
  naming never activates for them. Adding equivalent aggregation
  on `PrefabTemplate` + unioning into the scene set is the right
  fix; safe to defer since the unscoped fallback keeps existing
  projects working.
- **Transform-only prefab scanning** (one tween script per prefab
  animator, not just per scene) — plan calls this out; revisit
  alongside the prefab-animator aggregation above.

### PR 2b — 4.5 robustness polish

- **`UNCONVERTED.md` writer.** New `Pipeline._write_unconverted_md()`
  aggregates `AnimationConversionResult.unconverted` entries grouped
  by category (binary `.controller`, 2D blend tree) into
  `<output>/UNCONVERTED.md` at the tail of `write_output`. Removes
  any stale file when no entries remain so the absence of the md is
  itself signal.
- **Unconverted-entry plumbing.** `parse_controller_file` and
  `_parse_blend_tree` take an optional `unconverted_out: list`
  kwarg; `discover_animations` and `convert_animations` thread it
  through. Keeps the API additive — older callers that don't pass
  a list still work.
- **Malformed keyframe `log.warning`.** `_parse_vector_curve` now
  counts and logs skipped non-dict / non-numeric keyframes per
  curve with path context. Previously silent drops.
- **Inline-policy header in generated transform-only scripts.**
  `generate_tween_script` prepends a `-- Inline TweenService per
  docs/design/inline-over-runtime-wrappers.md (no TransformAnimator
  / AnimatorBridge require)` comment to every output so readers can
  find the governing design doc without grepping the codebase.
- **Bridge-leak test tightened.** The regression guard now matches
  only `require\\s*\\(.*AnimatorBridge.*\\)` patterns instead of
  bare substrings — the new policy header intentionally names the
  deleted bridges in a comment.

Verification: fast suite 596 passed (+5); full SimpleFPS conversion
still produces 7 transform-only scripts with the new header; no
`UNCONVERTED.md` emitted for SimpleFPS (no binary controllers, no
2D blend trees); `luau-analyze` (SyntaxError filter) passes 7/7;
`u2r.py validate` 0 errors.

### PR 2b — Codex review follow-ups (2026-04-24)

Codex review of PR 2b flagged three P2 findings. GATE was PASS
(no P1) but the fixes are cheap and make the polish actually reliable.

- **Fix #1 — scene-filter leak into `UNCONVERTED.md`.** Entries for
  controllers the run didn't emit output for are now dropped from
  `result.unconverted` when scene-scoping is active.
  `parse_controller_file` records the binary `.controller`'s `.meta`
  GUID in the entry so `convert_animations` can filter binary
  controllers against the scene's referenced GUID set. Blend-tree
  entries filter by controller name.
- **Fix #2 — nested 2D blend tree escaped detection.** A 1D blend
  tree containing a 2D grandchild used to silently collapse to the
  first-leaf clip without an UNCONVERTED entry. `_parse_blend_tree`
  and `_first_leaf_clip_guid` now check `m_BlendType` on every
  descent and surface nested 2D trees with the full
  `controller/state/nested` context.
- **Fix #3 — bridge-leak regression regex false-negative.** The
  previous `require\\s*\\([^)]*bridge[^)]*\\)` regex stopped at the
  first `)`, so the idiomatic Luau form `require(game:GetService
  ("…"):FindFirstChild("AnimatorBridge"))` slipped through. Replaced
  with a paren-balanced scanner in the test that walks from each
  `require(` to its matching close-paren and checks the full
  argument. New sanity test asserts the scanner flags the exact
  nested form the old regex missed.

Verification: fast suite 600 passed (+4 new tests for Codex fixes);
SimpleFPS smoke unchanged (7 scripts, 0 validate errors, no
UNCONVERTED.md emitted because SimpleFPS has no binary controllers
or 2D blend trees).

### PR 3 — 4.2 material_mapper + 4.8 vertex color baker wiring

Audit shrank the scope from the plan's ~680-line estimate to
~470 by explicitly dropping source's `.shader` file parser,
`UnconvertedFeature` dataclass, and texture ops (detail blend,
heightmap→normal) for which no test project provides fixtures.
Reuse existing `MaterialMapping.warnings: list[str]` instead of
adding a new dataclass; extend PR 2b's `_write_unconverted_md` to
aggregate material warnings alongside animation entries.

- **Shader categorization (4.2.1).** New `categorize_shader()`
  maps a Unity shader-name string to one of 12 labels (BUILTIN,
  URP, HDRP, LEGACY, PARTICLE, SPRITE, UI, UNLIT, MOBILE, SKYBOX,
  CUSTOM, UNKNOWN). Name-based only — no `.shader` source parsing,
  no `#include` resolution (deferred; no test project exercises
  custom HLSL yet). `MaterialMapping.shader_category` records
  the result per material.
- **Vertex color detection (4.2.5).** `shader_uses_vertex_colors()`
  heuristic scans the shader-name string for `VertexLit`,
  `Vertex Color`, `VertexColor`, `Vertex-Lit`, and
  `Particles/VertexLit Blended` hints. Sets
  `MaterialMapping.uses_vertex_colors: bool` during
  `_parse_material`. Flips on the 4.8 baker.
- **Texture ops (4.2.2).** Added three new operations in
  `utils/image_processing.py` and wired into the pipeline texture
  executor: `bake_ao` (AO → albedo composite with `strength` lerp
  matching Unity `_OcclusionStrength`), `threshold_alpha` (binary
  cutoff alpha clipping for Unity Cutout shader parity),
  `to_grayscale` (RGB → single-channel luminance). Plus
  `offset_image` and `scale_normal_map` as post-ops on the
  existing `TextureOperation` dataclass (applied after the primary
  op via new `pixel_offset` and `normal_scale` fields). Also fills
  the `extract_a` branch that was declared but never routed.
- **Advanced material props (4.2.4).** Extract Unity
  `_EmissionStrength` / HDRP `_EmissiveIntensity` into
  `MaterialMapping.emission_strength`. `ao_map_path` field added
  on the dataclass so a downstream AO-source lookup can plug in
  (baker already does). `source_path` on the mapping so 4.8
  can locate the owning `.mat` file.
- **UNCONVERTED.md extends (4.2.3).** `Pipeline._write_unconverted_md`
  now iterates `state.material_mappings.values()` too, turning every
  `MaterialMapping.warnings` entry into an `UNCONVERTED.md` bullet
  grouped under the `material` category. SimpleFPS smoke surfaces
  7 legitimate entries on the full project (FXWaterPro /
  FXWater4Advanced / FXWaterBasic / FXWater4Simple / RotatingTexture
  water + propeller shaders).
- **Vertex color baker wiring (4.8).** New
  `Pipeline._bake_vertex_colors()` runs at the tail of
  `convert_materials`. Walks `parsed_scene.all_nodes` + prefab
  library roots, inverts the mesh→material graph to find meshes
  that reference each flagged material, collects
  `(mesh_fbx, albedo)` pairs, and delegates to
  `bake_vertex_colors_batch`. Graceful fallback when the baker
  module fails to import (e.g. `pyassimp` missing): warning surfaces
  into `MaterialMapping.warnings` and flows into `UNCONVERTED.md`.
  Materials without a mesh referrer or without an albedo texture
  also record a skip reason. No crash paths.

Verification: fast suite 619 passed (+19 new); full SimpleFPS
convert produces 944 parts / 50 scripts / 50/51 materials /
7 anim scripts / terrain SmoothGrid encoded / 0 validate errors /
7 material entries in `UNCONVERTED.md` for legitimate unsupported
water shaders.

### PR 3 — Codex review follow-ups (2026-04-24)

Codex flagged 2 P1s + 1 P2. GATE was FAIL. Both P1s were real
correctness bugs affecting the normal `--upload` flow (our
`--no-upload` smoke never hit them).

- **Fix #1 (Codex P1) — baker silently skipped on upload flow.**
  `map_materials()` rewrites `color_map_path` to `rbxassetid://…`
  after the upload step, but `_bake_vertex_colors()` was then
  calling `Path(color_map).exists()` on the URL and failing. Added
  `MaterialMapping.local_color_map_path` field; `map_materials`
  captures the pre-upload local path there, and the baker reads it
  first.
- **Fix #2 (Codex P1) — shared materials overwrote each other.**
  When a flagged material had multiple mesh referrers, each bake's
  `entry.output_path` overwrote the mapping's `color_map_path` on
  every loop iteration — last mesh wins. Baker now bakes only the
  first (deterministic sort order) representative mesh per material
  and records the deferred meshes in `mapping.warnings`
  (surfaces into `UNCONVERTED.md`). Proper per-mesh baking would
  require per-part `SurfaceAppearance` splitting, which is
  architecturally bigger than PR 3 scope.
- **Fix #3 (Codex P2) — deferred.** Sub-mesh identity
  (`mesh_file_id`) is not yet preserved through to the baker;
  FBX files with multiple embedded meshes will rasterize the whole
  file instead of the specific submesh. This requires extending
  `bake_vertex_colors_batch`'s signature — out of PR 3 scope.
  Logged here for a follow-up PR.

Verification: fast suite 621 passed (+2 new Codex-fix tests);
SimpleFPS smoke unchanged (944 parts / 50/51 materials / 7 anim
scripts / terrain OK / 0 validate errors / 7 UNCONVERTED
entries). Both new tests stub `bake_vertex_colors_batch` so they
run in the fast suite with no `pyassimp` dependency.

### Deferred from PR 3 → future phase/hands-on

- `.shader` file source parsing (with `#include` resolution) —
  revisit when a test project ships custom HLSL worth inspecting
- `composite_detail`, `blend_normal_detail`, `heightmap_to_normal`
  texture ops from source — revisit when a test project exercises
  detail textures
- Companion Luau scripts per material — source has an empty stub
- `UnconvertedFeature` dataclass with severity tiers — revisit if
  warnings grow noisy enough to benefit from filtering
