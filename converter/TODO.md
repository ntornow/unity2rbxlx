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
- [ ] **Visual comparison automation**: Integrate viewport cropping + matched camera positions for accurate SSIM
- [x] **Play mode testing**: Partially addressed — playtest subagent integration works (verified in 2026-04-12 session), `playtest-gotchas.md` documents 7 caveats, `TestRiflePickupChainValidator` provides regression coverage. Full automated gameplay harness deferred.
- [x] **Rule-based transpiler receiver resolution**: Fixed 2026-04-12: standalone `transform.X` now resolves to `script.Parent.X` instead of bare `.X`, and `obj.transform.X` resolves to `obj.X` instead of `obj..X` (double dot). Same for `gameObject.X`. 4 new unit tests in `TestRuleBasedReceiverResolution`. Full rule-based quality for complex scripts still has gaps (bare `:` calls, missing function parentheses), but the receiver fix addresses the most impactful class of errors.
- [ ] **Multi-sub-mesh scene instances**: Fixed for fences specifically in `cd6dabd`, but the general pattern (any FBX with 2+ sub-meshes used as a direct scene instance) needs a broader scan. The fix creates a Model with child MeshParts when mesh_hierarchies has 2+ entries, but only when mesh_file_id is NOT set. Prefab instances that specify a particular fileID still use the single-MeshPart path.

## New Gaps (2026-03-28)

- [x] **Rigidbody physics properties**: Mass/drag/angularDrag extracted → CustomPhysicalProperties (density/friction/elasticity). Rigidbody2D m_LinearDrag + m_GravityScale also handled.
- [x] **MeshCollider CollisionFidelity**: m_Convex → Hull, non-convex → PreciseConvexDecomposition. Serialized in rbxlx.
- [x] **Silent PSD/TGA conversion errors**: Bare `except: pass` replaced with log.warning.
- [x] **Cinemachine camera runtime**: New cinemachine_runtime.luau — reads VCam attributes, does camera follow/look-at/FOV transitions. Auto-injected when CinemachineVCam detected.
- [x] **Test suite performance**: Slow tests (CLI subprocess + full Gamekit3D conversion) marked @slow. Fast suite: 872 tests in 9.4s. Full suite: 888 tests in 65s.

## Remaining Open Items

### Not Yet Implemented (genuine gaps)
- [x] **Tilemap/TilemapRenderer**: Tiles converted to thin Parts in a grid with cell sizing, tile colors, sprite GUIDs. TilemapRenderer properties extracted.
- [ ] **Font upload**: Not supported by Roblox Open Cloud API. UI text uses default Roblox font.
- [ ] **Video upload**: Not supported by Roblox Open Cloud API. VideoFrame component works but needs manual video ID.

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
- [ ] **P1 — No second project end-to-end-verified this session.** All fixes are in general-purpose code paths but were only validated against SimpleFPS. Gamekit3D / BossRoom / ChopChop may have regressions. Fix: run the slow suite against all 9 test projects after each gameplay-impacting change.
- [x] **P2 — `converter/output/<project>/conversion_context.json` holds real IDs but is only protected by `.gitignore`.** Fixed 2026-04-12: new `ConversionContext.save_sanitized()` writes a redacted copy — strips `universe_id`, `place_id`, `experience_name`, `uploaded_assets`, `mesh_native_sizes`, `mesh_hierarchies` — and stamps a `_sanitized: true` marker. Preserves stats, phase completion, warnings, Unity project path. 2 new tests in `TestConversionContextSanitizedSave`. The regular `.save()` still writes everything for pause/resume.
