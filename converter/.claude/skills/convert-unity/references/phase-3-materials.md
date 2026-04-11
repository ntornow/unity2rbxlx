# Phase 3: Material Mapping

Resolves every Unity material to a Roblox `MaterialMapping` (Color3 + optional SurfaceAppearance). Handles PBR textures, vertex-color baking, and per-sub-mesh materials.

## Command

```bash
python3 convert_interactive.py materials <unity_project_path> <output_dir> 2>/dev/null
```

Implementation: `converter/material_mapper.py` reads each `.mat` file (Standard or URP Lit), resolves texture GUIDs via the GUID index, and produces a `MaterialMapping` per material.

## Decision: unconvertible or partial materials

**Question:** What to do when a Unity material cannot be fully mapped to Roblox?

**Factors:**
- How much of the material's visual identity is carried by the albedo vs. by shader effects (emission, parallax, refraction).
- Whether the material is used on gameplay-critical surfaces (character, key props) or on background geometry.
- The material's rendering mode. Opaque materials with only Color3 are easy wins; custom shaders are not.

**Options:**
- **Accept the partial mapping.** Default. The pipeline uses albedo + roughness/metalness where available; other effects are dropped.
- **Provide manual override.** For critical materials, override the `MaterialMapping` entry with hand-chosen Color3/SurfaceAppearance values in `conversion_context.json` and re-run from the assemble phase.
- **Skip the material.** The mesh using it falls back to default gray. Only for test assets or debug props.

**Escape hatch:** Read the materials JSON output for the per-material `notes` field — it lists the specific shader features that couldn't be mapped.

## SurfaceAppearance rules

**SurfaceAppearance without a ColorMap makes the part white.** SurfaceAppearance completely overrides `Part.Color3` for rendering. A material with metalness/roughness textures but no albedo produces an all-white part — Color3 is ignored.

**Rule:** Only create SurfaceAppearance when `mapping.color_map_path` is present. Materials with only metalness/roughness rely on Color3 alone. Enforced in `converter/material_mapper.py` and consumed by `converter/scene_converter.py`.

**Missing SurfaceAppearance on vertex-color meshes.** Vertex-color-only materials create an empty `MaterialMapping` that depends on baking to fill the `color_map_path` later. If baking fails or the mesh path doesn't match the bake lookup key, the MeshPart ends up with no SurfaceAppearance at all.

**Diagnostic:** After assembly, scan the `.rbxlx` for MeshParts that have a `MeshId` but no SurfaceAppearance child. If the Unity source had an albedo for that mesh's material, the SurfaceAppearance was dropped and must be restored manually.

## Opaque-mode alpha bug

Unity's Standard shader discards `_Color.a` in Opaque mode (`_Mode = 0`), and many opaque materials ship with `a=0`. If the pipeline blindly applies `1.0 - _Color.a` as `Transparency`, textured MeshParts become fully invisible despite having a valid renderer.

**Rule:** Only apply alpha transparency when `parsed.render_mode != 0`. Enforced in `converter/material_mapper.py`.

**Symptom:** MeshPart has SurfaceAppearance with textures but is still invisible → check the source `.mat` file for `_Mode: 0` and `_Color: {... a: 0}`.

## Per-sub-mesh materials

FBX assets with multiple sub-meshes (one per material) are handled in `converter/scene_converter.py` and `converter/mesh_processor.py`. The pipeline preserves auto-materials and per-sub-mesh colors so chain prefabs (multiple parts sharing one FBX) render with the correct surface appearance per part.

**Diagnostic:** If a multi-material mesh renders with one material on every part, check the sub-mesh resolution in `mesh_processor.py` — the fileID-to-sub-mesh mapping may have failed and fallen back to the first material.
