# Unsupported Conversions & Limitations

What the Unity → Roblox converter cannot handle and why. Covers permanent
Roblox platform restrictions, Unity features with no Roblox equivalent,
and converter-side gaps where the conversion is partial. For active work
items, see [`TODO.md`](../TODO.md). For architectural debt and bugs, see
[`KNOWN_ISSUES.md`](KNOWN_ISSUES.md).

---

## Quick Reference: What works

The converter handles these features end-to-end with no manual steps.
This is the inverse of the unsupported lists below — included so readers
can confirm a feature IS supported before assuming a gap. The
materials/shaders coverage is the most detailed; for component-level
support see also `CLAUDE.md` § Supported Features.

### Materials and shaders

| Feature | Notes |
|---|---|
| Albedo (`_MainTex` / `_BaseMap`) | Direct copy to `SurfaceAppearance.ColorMap` |
| Color tint (`_Color` / `_BaseColor`) | Maps to `SurfaceAppearance.Color` |
| Normal maps (`_BumpMap`) | Direct copy (OpenGL format) |
| Metallic maps | R channel extracted from `_MetallicGlossMap` |
| Roughness (smoothness inversion) | A channel of `_MetallicGlossMap`, inverted |
| Emission maps + color | Grayscale mask + tint + strength |
| Render mode (opaque/cutout/transparent) | `_Mode` / `_Surface`+`_AlphaClip` → `AlphaMode` |
| Occlusion maps | Baked into ColorMap via multiply |
| Texture offset (pixel shift) | UV offset applied via pixel shifting |
| Normal map scale | `_BumpScale` baked into normal pixels |
| Smoothness from albedo alpha | `_SmoothnessTextureChannel=1` support |
| Detail albedo / detail normal | Composite into ColorMap / NormalMap with tiling + mask |
| Height map → normal detail | Sobel filter conversion |
| Vertex color baking | Rasterized to UV texture, multiplied into albedo (OBJ/PLY/GLB) |
| URP Lit/Unlit | Property name normalization |
| HDRP Lit (MaskMap) | R=Metal, G=AO, A=Smooth extraction |
| Legacy shaders | Diffuse / Bumped / Specular all mapped |
| Custom shader identification | Source parsing + `#include` resolution; falls back to standard property names |

### Scene and components

| Feature | Notes |
|---|---|
| Scene hierarchy (parent/child) | Preserved in `.rbxlx` |
| Transform position / scale / rotation | Full quaternion → CFrame |
| Prefab instantiation + variants | Resolved from `PrefabLibrary`, with property override merging |
| Point / spot / directional lights | `PointLight` / `SpotLight` / `Lighting` |
| Audio sources | `Sound` instances with volume, pitch, loop, RollOff |
| Particle systems | `ParticleEmitter` with shape, emission, color/size/force-over-lifetime |
| Trail / Line renderers | `Trail` / `Beam` with auto-attached `Attachment0/1` |
| Video player | `VideoFrame` (SurfaceGui-wrapped) — see API limits below |
| Skybox material | 6-face textures → `Sky` in `Lighting` |
| Cinemachine VirtualCamera | Camera config attributes + runtime script |
| Post-processing (Bloom, ColorGrading, DoF, SunShafts, Atmosphere) | Roblox effect counterparts |
| Reverb zones / filters | `ReverbSoundEffect` |
| Camera → `Workspace.CurrentCamera` | FOV, CFrame, near/far clip |

### Physics

| Feature | Notes |
|---|---|
| Box / Sphere / Capsule / MeshCollider | Part shape + sizing + `CollisionFidelity` |
| Rigidbody | `Anchored` + `CustomPhysicalProperties` (mass/drag/friction) |
| Rigidbody2D | Thin Part approximation |
| Joints (Fixed / Hinge / Spring / Character / Configurable) | `WeldConstraint` / `HingeConstraint` / `SpringConstraint` / `BallSocketConstraint` |
| CharacterController | Capsule sizing + attributes |
| 2D physics | Rigidbody2D / BoxCollider2D / CircleCollider2D / CapsuleCollider2D |

### UI

| Feature | Notes |
|---|---|
| Canvas / UI elements | `ScreenGui` with UDim2 layout |
| Layout groups | `UIListLayout` / `UIGridLayout` |
| Canvas scaler | Reference resolution → `ScreenGui` attributes |
| Sprites / SpriteRenderer | Thin colored Part with sprite GUID attribute |
| Sprite atlas cropping | `ImageRectOffset/Size` for atlas sprites |
| Button onClick wiring | UIEventWiring LocalScript |

### Animation

| Feature | Notes |
|---|---|
| Property animations | TweenService scripts |
| Skeletal animation (R15-mappable) | Motor6D bone chain via `character_animator.luau` |
| Animator state machine | Unified state machine script with parameter-driven transitions |
| 1D blend trees | Linear interpolation between thresholds |
| TransformAnimator (CFrame/Size curves) | Inline TweenService Scripts (per `inline-over-runtime-wrappers.md`) |

### Terrain

| Feature | Notes |
|---|---|
| SmoothGrid binary encoding | 6-bit material + occupancy + RLE, axis swap, 22 materials |
| Splat maps | Per-channel weights → dominant Roblox material |
| Height-based biome inference | Sand → Grass → Mud → Rock → Slate by elevation |
| Water region detection | Auto-sized water voxel block |
| FillBlock script fallback | Backup when SmoothGrid binary unavailable |

### Scripts

| Feature | Notes |
|---|---|
| C# → Luau transpilation | Claude AI (rule-based fallback for simple cases) |
| Client/Server/Module classification | Auto-detected from API usage |
| Cross-script dependency injection | `require()` calls auto-inserted in topological order |
| RemoteEvent auto-creation | From script analysis |
| Runtime modules auto-injected | animator, nav mesh, event system, event dispatch, physics bridge, cinemachine, object pool, pickup, sub-emitter |

---

## Roblox engine-level limitations (permanent)

These are platform-level restrictions. The converter cannot work around them.

| Limitation | Impact | Why permanent |
|---|---|---|
| No custom shaders | Vertex shader effects (world curve, wave) cannot be replicated | Roblox engine restriction |
| 1 material per MeshPart | Multi-material meshes are split into a sub-mesh hierarchy (handled by `scene_converter`) | Roblox data model |
| UV0 only | Secondary UV channels (lightmaps on UV1) are lost | Roblox uses single UV |
| No height/displacement mapping | Parallax effects lost | Engine renderer |
| No SSS / anisotropy / iridescence / clear coat | HDRP advanced materials simplified | No PBR extension API |
| No per-material cubemap reflections | Legacy reflective shaders lose custom reflections | Engine uses global probes |
| Max 4096×4096 texture | Larger textures get downscaled | Platform limit |
| `SurfaceAppearance` on `MeshPart` only | Primitive shapes (Part) can't use PBR textures | By design |
| No runtime `SurfaceAppearance` changes | Material property animation requires `BasePart.Color` workaround | PluginSecurity |
| No `SurfaceAppearance` tiling/offset | Repeating textures need pre-tiling or UV modification | Open feature request |
| 10,000 face limit per `MeshPart` | High-poly meshes need decimation | Platform limit |
| No vertex color reading from mesh data | Vertex colors in mesh data are ignored by `SurfaceAppearance` | Workaround: bake to texture |

### HDRP advanced features (no Roblox equivalent)

These Unity HDRP material properties are silently dropped:

| Property | What it does in Unity |
|---|---|
| `_SubsurfaceMask` / `_SubsurfaceMaskMap` | Subsurface scattering |
| `_Thickness` / `_ThicknessMap` | Translucency through thin geometry |
| `_Anisotropy` / `_AnisotropyMap` | Anisotropic highlights |
| `_IridescenceThickness` / map | Thin-film interference |
| `_CoatMask` / `_CoatMaskMap` | Clear coat layer |
| `_BentNormalMap` | Bent normals for indirect lighting |

---

## Unity features with no Roblox equivalent (silently skipped)

These Unity components are detected, logged, and skipped. Not bugs — Roblox
has no counterpart primitive.

| Unity feature | Roblox status | Workaround |
|---|---|---|
| **Cloth simulation** | No cloth physics primitive | Static mesh approximation |
| **Wind zones** | No wind volume primitive | None |
| **Blend shapes** | No morph target system on `MeshPart` | None |
| **Reflection probes** | Global reflections only | Future lighting compensates |
| **Light probes** | Global indirect lighting only | Future lighting compensates |
| **VFX Graph** | No node-graph VFX primitive | `ParticleEmitter` approximation where possible |
| **Particle SubEmitters** | Approximated via `sub_emitter_runtime.luau` | Auto-injected when `_HasSubEmitters` detected |
| **Tilemap (2D)** | Converted to thin Parts in a grid | Limited; see `component_converter` |
| **Inverse kinematics** | No IK solver | Out of scope (would need full IK in Luau) |

---

## Converter-side gaps (partial conversion)

Areas where the converter handles the common case but loses fidelity in edge cases.

### UV tiling ≠ (1, 1)

`SurfaceAppearance` has no tiling or offset properties. Textures map 1:1 to UV0.

The converter handles tiling via pre-tiling the texture image:

| Tiling factor | Strategy | Quality |
|---|---|---|
| (1, 1) | No action needed | Perfect |
| ≤ (4, 4) | Pre-tile the texture image | Good (loses resolution per tile) |
| > (4, 4) | Logged to `UNCONVERTED.md` for manual mesh-UV editing | — |

### Custom Shader Graph

`.shadergraph` files are not parsed (only `.shader` source). For Shader Graph
materials, the converter falls back to checking if standard property names
(`_BaseMap`, `_Color`) exist in the material's saved properties. Node graphs
are not analyzed.

### Multi-material mesh splitting (FBX edge cases)

`scene_converter` builds a sub-mesh hierarchy from the per-submesh material
list, emitting one `MeshPart` per submesh material under a parent `Model`.
The common case (GLB / well-formed FBX) works. Edge cases:

- FBX files where `scene_converter` can't resolve per-submesh materials
  from the prefab's `m_Materials` list (e.g. material count mismatch with
  the FBX sub-mesh count) — falls back to first-material-only with the
  unmatched materials surfaced via `UNCONVERTED.md`.
- Sub-mesh material ordering when `m_Materials` and mesh sub-mesh indices
  don't align — surfaced via `UNCONVERTED.md`.

### Vertex colors (FBX)

Full per-vertex color baking is supported for OBJ / PLY / GLB via `vertex_color_baker.py`.
For FBX, the converter falls back to dominant-color extraction:

- The average vertex color is extracted from the FBX `LayerElementColor` section.
- `BasePart.Color3` is set to that flat color.
- Better than default gray for environment meshes (roads, buildings) but loses
  per-vertex variation.

### Animation completeness

The animator runtime handles common cases but several Unity features are
unimplemented:

| Feature | Status |
|---|---|
| Simple state transitions | Implemented |
| 1D blend trees | Implemented |
| 2D blend trees (freeform) | Logged to `UNCONVERTED.md`; first-leaf clip used as fallback |
| Animation layers | Not supported (Roblox has no per-bone masking) |
| Avatar masks | Not supported |
| Root motion extraction | Not supported |
| Inverse kinematics | Not supported |
| Binary `.controller` / `.anim` | Surfaced to `UNCONVERTED.md`; needs UnityPy or binary YAML parser |

---

## Roblox API limitations

### Open Cloud asset upload

The Open Cloud API only supports Image, Model (mesh), and Audio asset types.
The converter emits the corresponding Roblox component, but the asset must
be uploaded manually via the [Creator Dashboard](https://create.roblox.com)
and the asset ID pasted into the converted place.

| Asset type | Limit |
|---|---|
| Fonts | Open Cloud upload not supported. UI text falls back to Roblox default font. |
| Video files | Open Cloud upload not supported. `VideoFrame` emitted with empty video ID placeholder. |

### Universe / place creation

Open Cloud does not expose universe or place creation under API-key auth (it
needs a `ROBLOSECURITY` cookie + XSRF token). The pipeline emits actionable
instructions directing users to pre-create a universe/place via the Creator
Dashboard and pass `--universe-id` / `--place-id` to `u2r.py convert`.

### Mesh / texture asset resolution

Uploaded meshes return Model IDs (not Mesh IDs); uploaded textures return
Decal IDs (not Image IDs). Both must be resolved via Studio MCP
(`InsertService:LoadAsset`) before the rbxlx is finalized:

- Mesh Model IDs → real `MeshIds` + sub-mesh hierarchy
- Texture Decal IDs → Image IDs (`SurfaceAppearance` needs Image, not Decal)

The pipeline runs this via `roblox/studio_resolver.py` when `--universe-id` /
`--place-id` are supplied. Otherwise it generates resolution scripts (see
`u2r.py resolve`).

---

## Cross-scene constraints

Constraint `Part0` / `Part1` linking may fail for constraints spanning different
scene roots. The converter resolves referents within a single scene's Part graph.
Roblox places have no Unity-style multi-scene composition model, so cross-scene
Part references can land unconnected.

---

## Platform-rendering differences

### Mesh Z-axis mirroring

Unity is left-handed (Z-forward); Roblox is right-handed. The converter negates
Z positions so objects land in the correct world location, but mesh geometry
itself is not mirrored — only the position. Asymmetric features inside meshes
(text, door handles, logos, signage) render backwards in Roblox.

Possible fixes (none obviously correct):

- Pre-rotate each mesh 180° around Y before upload (changes FBX content; may
  affect skinning/animation rigs).
- Apply negative Z scale on the CFrame (does not work reliably for `MeshPart`s
  — Roblox interprets negative scale as a rendering artifact rather than a mirror).
- Re-export FBXes from Unity with mirrored geometry (out of scope for an
  automated converter).

Documented as a known visual difference rather than auto-fixed.

### Wire/grid mesh opacity

Chain-link fences and similar thin-geometry meshes render as opaque in Roblox
because the mesh renderer fills sub-pixel gaps between wires. Unity renders
those gaps correctly because its renderer treats sub-pixel coverage differently.

Texture alpha (e.g. 87.7% transparent for `chainlink.psd`) could compensate via
`AlphaMode=Transparency`, but the source Unity material is typically `_Mode=0`
(Opaque), so changing it would alter the intended rendering mode of the
original asset.

Documented as a platform rendering difference. Not auto-fixed.

---

## Pipeline / asset processing limits

| Limitation | Detail |
|---|---|
| FBX sub-mesh hierarchies | Use collider-based size fallback when Studio resolution unavailable |
| PSD / TGA / BMP / TIF | Auto-converted to PNG for upload (requires Pillow) |
| Git LFS pointer files | Detected and skipped (actual FBX data needs `git lfs pull`) |
