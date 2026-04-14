"""
material_mapper.py -- Unity material -> Roblox SurfaceAppearance mapping.

Parses Unity .mat YAML files, identifies shaders, maps texture slots to
Roblox PBR channels, handles transparency modes, and queues texture
operations (channel extraction, inversion).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.unity_types import GuidIndex

log = logging.getLogger(__name__)

# Supported Unity shader names.
_SUPPORTED_SHADERS = frozenset({
    "Standard",
    "Standard (Specular setup)",
    "Universal Render Pipeline/Lit",
    "Universal Render Pipeline/Simple Lit",
    "Lit",
    "SimpleLit",
    # HDRP shaders
    "HDRP/Lit",
    "HDRP/LitTessellation",
    "HDRenderPipeline/Lit",
    "HDRenderPipeline/LitTessellation",
})

# Unity transparency mode values (_Mode property).
_ALPHA_OPAQUE = 0
_ALPHA_CUTOUT = 1
_ALPHA_FADE = 2
_ALPHA_TRANSPARENT = 3

# Unity built-in shader fileIDs that are INHERENTLY cutout/transparent,
# regardless of the _Mode property. The legacy Transparent/Cutout/*
# shaders (used for chain-link fences, foliage, etc.) don't have a
# _Mode toggle — the shader variant itself encodes the transparency
# behaviour. Discovered by inspecting Unity's built-in shader YAML
# descriptors in builtin_extra.
_BUILTIN_CUTOUT_SHADER_IDS = frozenset({
    51,   # Transparent/Cutout/Diffuse
    52,   # Transparent/Cutout/Vertex-Lit
    53,   # Transparent/Cutout/Bumped Diffuse
    54,   # Transparent/Cutout/Bumped Specular
    55,   # Transparent/Cutout/Specular
    56,   # Transparent/Cutout/Soft Edge Unlit
    57,   # Nature/Tree Soft Occlusion Leaves
    200,  # Legacy Unlit/Transparent Cutout
})
_BUILTIN_TRANSPARENT_SHADER_IDS = frozenset({
    30,   # Transparent/Diffuse
    31,   # Transparent/Vertex-Lit
    32,   # Transparent/Bumped Diffuse
    33,   # Transparent/Bumped Specular
    34,   # Transparent/Specular
    202,  # Unlit/Transparent
})


@dataclass
class TextureOperation:
    """A queued texture processing operation."""
    source_path: str
    output_path: str
    operation: str  # "copy", "extract_r", "extract_a", "invert", "invert_a"
    channel: str = ""  # "R", "G", "B", "A"


@dataclass
class MaterialMapping:
    """Result of mapping a Unity material to Roblox SurfaceAppearance channels."""
    color_map_path: str | None = None
    normal_map_path: str | None = None
    metalness_map_path: str | None = None
    roughness_map_path: str | None = None
    base_color: tuple[float, float, float] = (0.63, 0.63, 0.63)
    transparency: float = 0.0
    alpha_mode: str = "Overlay"
    warnings: list[str] = field(default_factory=list)
    texture_operations: list[TextureOperation] = field(default_factory=list)
    shader_name: str = ""
    material_name: str = ""
    roblox_material: str = "Plastic"  # Inferred Roblox material enum name
    metallic: float = 0.0  # Unity _Metallic value (0-1)
    tiling: tuple[float, float] | None = None  # (scaleX, scaleY) from _MainTex_ST
    offset: tuple[float, float] | None = None  # (offsetX, offsetY) from _MainTex_ST
    emission_color: tuple[float, float, float] | None = None  # Unity _EmissionColor (r,g,b)
    is_emissive: bool = False  # True if material has emission


def _resolve_texture_url(
    local_path: str | None,
    uploaded_assets: dict[str, str],
) -> str | None:
    """Resolve a local texture path to an rbxassetid:// URL if uploaded.

    Tries multiple key formats (absolute, relative, forward/back slashes).
    Returns the rbxassetid URL if found, otherwise the original local path.
    """
    if local_path is None:
        return None
    candidates = [local_path]
    # Try both slash directions
    candidates.append(local_path.replace("\\", "/"))
    candidates.append(local_path.replace("/", "\\"))
    # Try just the filename
    p = Path(local_path)
    candidates.append(p.name)
    # Try relative "Assets/..." form
    parts = p.parts
    for i, part in enumerate(parts):
        if part == "Assets":
            candidates.append(str(Path(*parts[i:])))
            candidates.append("/".join(parts[i:]))
            break
    for key in candidates:
        if key in uploaded_assets:
            return uploaded_assets[key]
    return local_path  # fallback to local path


def map_materials(
    unity_project_path: str | Path,
    guid_index: GuidIndex | None,
    referenced_guids: set[str],
    output_dir: str | Path,
    uploaded_assets: dict[str, str] | None = None,
) -> dict[str, MaterialMapping]:
    """Map referenced Unity materials to Roblox SurfaceAppearance definitions.

    Args:
        unity_project_path: Root of the Unity project.
        guid_index: GUID -> path resolver.
        referenced_guids: Set of material GUIDs referenced by the scene.
        output_dir: Directory for processed texture output.
        uploaded_assets: Dict mapping local asset paths to rbxassetid:// URLs.

    Returns:
        Dict mapping material GUID -> MaterialMapping.
    """
    project = Path(unity_project_path).resolve()
    out = Path(output_dir).resolve()
    textures_dir = out / "textures"
    textures_dir.mkdir(parents=True, exist_ok=True)
    ua = uploaded_assets or {}

    results: dict[str, MaterialMapping] = {}

    for guid in referenced_guids:
        mat_path = _resolve_material_path(guid, guid_index)
        if mat_path is None:
            log.warning("Material GUID %s could not be resolved to a file", guid)
            continue

        if not mat_path.exists():
            log.warning("Material file not found: %s", mat_path)
            continue

        mapping = _parse_material(mat_path, guid, guid_index, textures_dir)

        # Resolve local texture paths to uploaded rbxassetid:// URLs.
        if ua:
            mapping.color_map_path = _resolve_texture_url(mapping.color_map_path, ua)
            mapping.normal_map_path = _resolve_texture_url(mapping.normal_map_path, ua)
            mapping.metalness_map_path = _resolve_texture_url(mapping.metalness_map_path, ua)
            mapping.roughness_map_path = _resolve_texture_url(mapping.roughness_map_path, ua)

        results[guid] = mapping

    log.info("Mapped %d / %d referenced materials", len(results), len(referenced_guids))
    return results


# ---------------------------------------------------------------------------
# Material parsing
# ---------------------------------------------------------------------------

def _resolve_material_path(
    guid: str, guid_index: GuidIndex | None,
) -> Path | None:
    """Resolve a material GUID to its .mat file path."""
    if guid_index is None:
        return None
    return guid_index.resolve(guid)


def _parse_material(
    mat_path: Path,
    guid: str,
    guid_index: GuidIndex | None,
    textures_dir: Path,
) -> MaterialMapping:
    """Parse a single .mat YAML file and produce a MaterialMapping.

    Handles Standard, Standard (Specular), and URP Lit shaders.
    """
    mapping = MaterialMapping(material_name=mat_path.stem)

    try:
        raw = mat_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        mapping.warnings.append(f"Could not read material file: {exc}")
        return mapping

    # Parse the material YAML (simplified -- Unity .mat files use YAML).
    mat_data = _parse_mat_yaml(raw)
    if mat_data is None:
        mapping.warnings.append("Could not parse material YAML")
        return mapping

    # Identify shader.
    shader_name = _extract_shader_name(mat_data, guid_index)
    mapping.shader_name = shader_name

    if shader_name not in _SUPPORTED_SHADERS:
        # Shader Graph materials often use custom names but still have
        # standard PBR properties. Accept any shader and try to extract
        # properties — the texture slot lookups below use multiple
        # fallback names (Standard + URP + HDRP) so they work on most
        # shader variants regardless of the shader name.
        if not shader_name.startswith(("Shader Graphs/", "Custom/")):
            mapping.warnings.append(f"Unsupported shader: {shader_name}")

    # Extract saved properties.
    saved_props = mat_data.get("m_SavedProperties", {})
    tex_envs = _normalize_tex_envs(saved_props.get("m_TexEnvs", []))
    floats = _normalize_floats(saved_props.get("m_Floats", []))
    colors = _normalize_colors(saved_props.get("m_Colors", []))

    # -- Base color (_Color or _BaseColor for HDRP) --
    base_color = colors.get("_Color") or colors.get("_BaseColor", {})
    if base_color:
        mapping.base_color = (
            float(base_color.get("r", 0.63)),
            float(base_color.get("g", 0.63)),
            float(base_color.get("b", 0.63)),
        )

    # -- Transparency mode --
    # Legacy built-in shaders encode cutout/transparent behaviour in the
    # shader variant itself (no _Mode); Standard/URP/HDRP use the _Mode
    # property. Check the shader fileID first.
    shader_ref = mat_data.get("m_Shader", {})
    shader_file_id = 0
    if isinstance(shader_ref, dict):
        try:
            shader_file_id = int(shader_ref.get("fileID", 0))
        except (ValueError, TypeError):
            shader_file_id = 0

    if shader_file_id in _BUILTIN_CUTOUT_SHADER_IDS:
        mapping.alpha_mode = "Transparency"
        mapping.transparency = 0.0
    elif shader_file_id in _BUILTIN_TRANSPARENT_SHADER_IDS:
        mapping.alpha_mode = "Transparency"
        alpha = float(base_color.get("a", 1.0)) if base_color else 1.0
        mapping.transparency = 1.0 - alpha
    else:
        mode = int(floats.get("_Mode", _ALPHA_OPAQUE))
        if mode == _ALPHA_OPAQUE:
            mapping.alpha_mode = "Overlay"
            mapping.transparency = 0.0
        elif mode == _ALPHA_CUTOUT:
            mapping.alpha_mode = "Transparency"
            mapping.transparency = 0.0
        elif mode == _ALPHA_FADE:
            mapping.alpha_mode = "Transparency"
            alpha = float(base_color.get("a", 1.0)) if base_color else 1.0
            mapping.transparency = 1.0 - alpha
        elif mode == _ALPHA_TRANSPARENT:
            mapping.alpha_mode = "Transparency"
            alpha = float(base_color.get("a", 1.0)) if base_color else 1.0
            mapping.transparency = 1.0 - alpha

    # -- Texture mapping --
    # ColorMap: _MainTex (Standard), _BaseMap (URP), or _BaseColorMap (HDRP).
    color_tex = tex_envs.get("_MainTex") or tex_envs.get("_BaseMap") or tex_envs.get("_BaseColorMap")
    if color_tex:
        color_map_path = _resolve_texture(color_tex, guid_index)
        if color_map_path:
            mapping.color_map_path = str(color_map_path)
        # Extract tiling/offset (m_Scale, m_Offset)
        tex_scale = color_tex.get("m_Scale", {})
        tex_offset = color_tex.get("m_Offset", {})
        if isinstance(tex_scale, dict):
            sx = float(tex_scale.get("x", 1.0))
            sy = float(tex_scale.get("y", 1.0))
            if sx != 1.0 or sy != 1.0:
                mapping.tiling = (sx, sy)
        if isinstance(tex_offset, dict):
            ox = float(tex_offset.get("x", 0.0))
            oy = float(tex_offset.get("y", 0.0))
            if ox != 0.0 or oy != 0.0:
                mapping.offset = (ox, oy)

    # NormalMap: _BumpMap (Standard/URP) or _NormalMap (HDRP).
    bump_tex = tex_envs.get("_BumpMap") or tex_envs.get("_NormalMap")
    if bump_tex:
        normal_path = _resolve_texture(bump_tex, guid_index)
        if normal_path:
            mapping.normal_map_path = str(normal_path)

    # MetalnessMap: _MetallicGlossMap (Standard/URP) or _MaskMap (HDRP).
    # HDRP _MaskMap packs: R=metallic, G=AO, B=detail mask, A=smoothness.
    # Same channel layout as Standard's _MetallicGlossMap for R and A.
    metallic_tex = tex_envs.get("_MetallicGlossMap") or tex_envs.get("_MaskMap")
    if metallic_tex:
        metallic_path = _resolve_texture(metallic_tex, guid_index)
        if metallic_path:
            # Queue R channel extraction for metalness.
            metal_out = textures_dir / f"{mat_path.stem}_metalness.png"
            mapping.metalness_map_path = str(metal_out)
            mapping.texture_operations.append(TextureOperation(
                source_path=str(metallic_path),
                output_path=str(metal_out),
                operation="extract_r",
                channel="R",
            ))

            # RoughnessMap: _MetallicGlossMap A channel, inverted.
            # Unity stores smoothness in A; Roblox uses roughness (inverted).
            rough_out = textures_dir / f"{mat_path.stem}_roughness.png"
            mapping.roughness_map_path = str(rough_out)
            mapping.texture_operations.append(TextureOperation(
                source_path=str(metallic_path),
                output_path=str(rough_out),
                operation="invert_a",
                channel="A",
            ))

    # Standalone roughness/smoothness maps (some shaders use these separately)
    if not metallic_tex:
        for rough_key in ("_RoughnessMap", "_RoughnessTex", "_SmoothnessMap"):
            rough_tex = tex_envs.get(rough_key)
            if rough_tex:
                rough_path = _resolve_texture(rough_tex, guid_index)
                if rough_path:
                    rough_out = textures_dir / f"{mat_path.stem}_roughness.png"
                    # Smoothness maps need inversion; roughness maps are direct copy
                    op = "invert_a" if "Smoothness" in rough_key else "copy"
                    mapping.roughness_map_path = str(rough_out)
                    mapping.texture_operations.append(TextureOperation(
                        source_path=str(rough_path),
                        output_path=str(rough_out),
                        operation=op,
                    ))
                    break

    # Fallback: if no metallic map, check for _Metallic float.
    metallic_val = floats.get("_Metallic", 0.0)
    mapping.metallic = float(metallic_val)
    if not metallic_tex:
        smoothness_val = floats.get("_Glossiness", 0.5)
        # No texture ops needed; values can be baked into uniform textures.
        if float(metallic_val) > 0.01:
            mapping.warnings.append(
                f"Metallic={metallic_val} (uniform); consider generating a flat texture"
            )

    # Specular workflow fallback.
    if shader_name == "Standard (Specular setup)":
        spec_tex = tex_envs.get("_SpecGlossMap")
        if spec_tex:
            spec_path = _resolve_texture(spec_tex, guid_index)
            if spec_path:
                mapping.warnings.append(
                    "Specular workflow: _SpecGlossMap found but Roblox uses metalness. "
                    "Manual review recommended."
                )

    # Emission: _EmissionColor (Standard/URP) or _EmissiveColor (HDRP)
    colors = mat_data.get("m_Colors", {})
    if isinstance(colors, dict):
        emission = colors.get("_EmissionColor") or colors.get("_EmissiveColor", {})
        if isinstance(emission, dict):
            er = float(emission.get("r", 0.0))
            eg = float(emission.get("g", 0.0))
            eb = float(emission.get("b", 0.0))
            if er > 0.01 or eg > 0.01 or eb > 0.01:
                mapping.emission_color = (er, eg, eb)
                mapping.is_emissive = True
    # Also check normalized colors structure
    norm_colors = _normalize_colors(mat_data.get("m_SavedProperties", {}).get("m_Colors", []))
    if "_EmissionColor" in norm_colors and not mapping.is_emissive:
        em = norm_colors["_EmissionColor"]
        er, eg, eb = float(em.get("r", 0)), float(em.get("g", 0)), float(em.get("b", 0))
        if er > 0.01 or eg > 0.01 or eb > 0.01:
            mapping.emission_color = (er, eg, eb)
            mapping.is_emissive = True

    # Infer Roblox material from material name keywords
    mapping.roblox_material = _infer_roblox_material(mapping.material_name)

    # Emissive materials → Neon (overrides other material inference)
    if mapping.is_emissive:
        mapping.roblox_material = "Neon"

    # If the name-based inference didn't find anything, use metallic value
    if mapping.roblox_material == "Plastic":
        metallic_val = float(floats.get("_Metallic", 0.0))
        if metallic_val > 0.5:
            mapping.roblox_material = "Metal"
        elif metallic_val > 0.2:
            mapping.roblox_material = "SmoothPlastic"

    return mapping


# Keyword -> Roblox material name mapping (checked in order, first match wins)
_MATERIAL_KEYWORDS: list[tuple[str, str]] = [
    ("concrete", "Concrete"),
    ("stone", "Concrete"),
    ("brick", "Brick"),
    ("wood", "Wood"),
    ("plank", "WoodPlanks"),
    ("beam", "Wood"),
    ("crate", "Wood"),
    ("pallet", "Wood"),
    ("metal", "Metal"),
    ("steel", "DiamondPlate"),
    ("iron", "Metal"),
    ("gold", "Metal"),
    ("silver", "Metal"),
    ("bronze", "Metal"),
    ("copper", "Metal"),
    ("chrome", "Metal"),
    ("aluminum", "Metal"),
    ("titanium", "Metal"),
    ("chainlink", "Metal"),
    ("barbed", "Metal"),
    ("fence", "Metal"),
    ("glass", "Glass"),
    ("ice", "Ice"),
    ("sand", "Sand"),
    ("grass", "Grass"),
    ("dirt", "Ground"),
    ("gravel", "Pebble"),
    ("marble", "Marble"),
    ("granite", "Granite"),
    ("fabric", "Fabric"),
    ("neon", "Neon"),
    ("glow", "Neon"),
    ("water", "Glass"),
    ("tile", "SmoothPlastic"),
    ("asphalt", "Asphalt"),
    ("road", "Asphalt"),
    ("pavement", "Concrete"),
    ("rubber", "SmoothPlastic"),
    ("plastic", "SmoothPlastic"),
    ("rust", "CorrodedMetal"),
    ("corrode", "CorrodedMetal"),
    ("foil", "Foil"),
    ("cobble", "Cobblestone"),
    ("snow", "Snow"),
    ("mud", "Mud"),
    ("slate", "Slate"),
    ("limestone", "Limestone"),
    ("sandstone", "Sandstone"),
    ("leather", "Fabric"),
    ("cloth", "Fabric"),
    ("carpet", "Fabric"),
]


def _infer_roblox_material(material_name: str) -> str:
    """Infer a Roblox material enum name from a Unity material name."""
    name_lower = material_name.lower()
    for keyword, roblox_mat in _MATERIAL_KEYWORDS:
        if keyword in name_lower:
            return roblox_mat
    return "Plastic"


# ---------------------------------------------------------------------------
# YAML helpers (lightweight, avoids full YAML dependency for .mat files)
# ---------------------------------------------------------------------------

def _parse_mat_yaml(raw: str) -> dict[str, Any] | None:
    """Parse a Unity .mat YAML file into a dict.

    Uses PyYAML for newer format materials, then patches the result with
    regex-based parsing for the older data:/first:/second: format that
    PyYAML can't handle (duplicate keys get overwritten).
    """
    import re

    # Strip the Unity YAML header line.
    lines = raw.split("\n")
    cleaned_lines = []
    for line in lines:
        if line.startswith("%YAML") or line.startswith("%TAG") or line.startswith("---"):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)

    result: dict[str, Any] | None = None

    try:
        import yaml  # type: ignore[import-untyped]
        docs = list(yaml.safe_load_all(cleaned))
        if docs:
            doc = docs[0]
            if isinstance(doc, dict):
                result = doc.get("Material", doc)
    except ImportError:
        log.debug("PyYAML not available; using fallback parser")
    except Exception as exc:
        log.debug("YAML parse failed: %s; trying fallback", exc)

    if result is None:
        result = _fallback_parse_mat(raw)
    if result is None:
        return None

    # Fix m_TexEnvs: the older Unity format uses repeated 'data:' keys
    # which PyYAML collapses. Parse tex envs with regex for reliability.
    tex_envs_list = _regex_parse_tex_envs(raw)
    if tex_envs_list:
        if "m_SavedProperties" not in result:
            result["m_SavedProperties"] = {}
        result["m_SavedProperties"]["m_TexEnvs"] = tex_envs_list

    # Similarly fix m_Floats
    floats_list = _regex_parse_floats(raw)
    if floats_list:
        if "m_SavedProperties" not in result:
            result["m_SavedProperties"] = {}
        result["m_SavedProperties"]["m_Floats"] = floats_list

    # Similarly fix m_Colors
    colors_list = _regex_parse_colors(raw)
    if colors_list:
        if "m_SavedProperties" not in result:
            result["m_SavedProperties"] = {}
        result["m_SavedProperties"]["m_Colors"] = colors_list

    return result


def _regex_parse_tex_envs(raw: str) -> list[dict[str, Any]]:
    """Parse m_TexEnvs from raw Unity YAML using regex.

    Handles both old format (data:/first:name:/second:) and new format
    (- _MainTex:).
    """
    import re
    result = []

    # Old format: repeated data:/first:/second: blocks
    # Pattern: name: _XXX followed by m_Texture: {fileID: N, guid: HEX, type: N}
    pattern = r"name:\s*(_\w+)\s*\n\s*second:\s*\n\s*m_Texture:\s*\{([^}]*)\}"
    for m in re.finditer(pattern, raw):
        prop_name = m.group(1)
        tex_ref_str = m.group(2)

        # Parse the texture reference
        guid_m = re.search(r"guid:\s*([a-f0-9]+)", tex_ref_str)
        file_id_m = re.search(r"fileID:\s*(\d+)", tex_ref_str)

        tex_ref = {}
        if guid_m:
            tex_ref["guid"] = guid_m.group(1)
        if file_id_m:
            tex_ref["fileID"] = int(file_id_m.group(1))

        result.append({prop_name: {"m_Texture": tex_ref}})

    # New format: - _MainTex: \n m_Texture: {fileID: N, guid: HEX} \n m_Scale: {x: N, y: N} \n m_Offset: {x: N, y: N}
    if not result:
        pattern2 = r"-\s*(_\w+):\s*\n\s*m_Texture:\s*\{([^}]*)\}"
        for m in re.finditer(pattern2, raw):
            prop_name = m.group(1)
            tex_ref_str = m.group(2)
            guid_m = re.search(r"guid:\s*([a-f0-9]+)", tex_ref_str)
            file_id_m = re.search(r"fileID:\s*(\d+)", tex_ref_str)
            tex_ref = {}
            if guid_m:
                tex_ref["guid"] = guid_m.group(1)
            if file_id_m:
                tex_ref["fileID"] = int(file_id_m.group(1))
            entry: dict[str, Any] = {"m_Texture": tex_ref}
            # Look for m_Scale and m_Offset after this tex entry
            after = raw[m.end():]
            scale_m = re.match(r"\s*\n\s*m_Scale:\s*\{([^}]*)\}", after)
            if scale_m:
                sx_m = re.search(r"x:\s*([0-9.eE+-]+)", scale_m.group(1))
                sy_m = re.search(r"y:\s*([0-9.eE+-]+)", scale_m.group(1))
                if sx_m and sy_m:
                    entry["m_Scale"] = {"x": float(sx_m.group(1)), "y": float(sy_m.group(1))}
                offset_after = after[scale_m.end():]
                offset_m = re.match(r"\s*\n\s*m_Offset:\s*\{([^}]*)\}", offset_after)
                if offset_m:
                    ox_m = re.search(r"x:\s*([0-9.eE+-]+)", offset_m.group(1))
                    oy_m = re.search(r"y:\s*([0-9.eE+-]+)", offset_m.group(1))
                    if ox_m and oy_m:
                        entry["m_Offset"] = {"x": float(ox_m.group(1)), "y": float(oy_m.group(1))}
            result.append({prop_name: entry})

    return result


def _regex_parse_floats(raw: str) -> list[dict[str, float]]:
    """Parse m_Floats from raw Unity YAML using regex."""
    import re
    result = []

    # Old format: name: _XXX \n second: VALUE
    for m in re.finditer(r"name:\s*(_\w+)\s*\n\s*second:\s*([0-9.eE+-]+)", raw):
        try:
            result.append({m.group(1): float(m.group(2))})
        except ValueError:
            pass

    # New format: - _XXX: VALUE
    if not result:
        for m in re.finditer(r"-\s*(_\w+):\s*([0-9.eE+-]+)\s*$", raw, re.MULTILINE):
            try:
                result.append({m.group(1): float(m.group(2))})
            except ValueError:
                pass

    return result


def _regex_parse_colors(raw: str) -> list[dict[str, dict[str, float]]]:
    """Parse m_Colors from raw Unity YAML using regex."""
    import re
    result = []

    # Old format: name: _Color \n second: \n r: V \n g: V \n b: V \n a: V
    pattern = r"name:\s*(_\w+)\s*\n\s*second:\s*\n\s*r:\s*([0-9.eE+-]+)\s*\n\s*g:\s*([0-9.eE+-]+)\s*\n\s*b:\s*([0-9.eE+-]+)\s*\n\s*a:\s*([0-9.eE+-]+)"
    for m in re.finditer(pattern, raw):
        result.append({
            m.group(1): {
                "r": float(m.group(2)),
                "g": float(m.group(3)),
                "b": float(m.group(4)),
                "a": float(m.group(5)),
            }
        })

    # New format: - _Color: {r: V, g: V, b: V, a: V}
    if not result:
        pattern2 = r"-\s*(_\w+):\s*\{r:\s*([0-9.eE+-]+),\s*g:\s*([0-9.eE+-]+),\s*b:\s*([0-9.eE+-]+),\s*a:\s*([0-9.eE+-]+)\}"
        for m in re.finditer(pattern2, raw):
            result.append({
                m.group(1): {
                    "r": float(m.group(2)),
                    "g": float(m.group(3)),
                    "b": float(m.group(4)),
                    "a": float(m.group(5)),
                }
            })

    return result


def _fallback_parse_mat(raw: str) -> dict[str, Any] | None:
    """Minimal fallback parser for Unity .mat files when PyYAML is unavailable."""
    import re

    result: dict[str, Any] = {}

    # Extract shader name.
    m = re.search(r"m_Shader:\s*\{.*?guid:\s*(\w+)", raw)
    if m:
        result["_shader_guid"] = m.group(1)

    # Extract m_Name.
    m = re.search(r"m_Name:\s*(.+)", raw)
    if m:
        result["m_Name"] = m.group(1).strip()

    # Extract m_SavedProperties section marker.
    if "m_SavedProperties" in raw:
        result["m_SavedProperties"] = {
            "m_TexEnvs": [],
            "m_Floats": [],
            "m_Colors": [],
        }

    return result if result else None


def _extract_shader_name(mat_data: dict[str, Any], guid_index: Any = None) -> str:
    """Extract the shader name from parsed material data.

    Resolves shader GUIDs via guid_index to get the actual shader file name.
    This is critical for detecting water shaders (FXWaterPro, etc.).
    """
    # Direct shader name if available.
    shader = mat_data.get("m_Shader", {})
    if isinstance(shader, dict):
        # Sometimes stored as m_Name in the shader reference.
        name = shader.get("m_Name", "")
        if name:
            return name
        # Resolve shader GUID to file path to get shader name
        shader_guid = shader.get("guid", "")
        if shader_guid and guid_index:
            shader_path = guid_index.resolve(shader_guid)
            if shader_path:
                # Use the shader file stem as the shader name
                shader_stem = shader_path.stem
                return shader_stem
    # Check for string shader reference.
    if isinstance(shader, str):
        return shader
    # Fallback to material name hints.
    mat_name = mat_data.get("m_Name", "")
    if "URP" in mat_name or "Lit" in mat_name:
        return "Universal Render Pipeline/Lit"
    return "Standard"


def _normalize_tex_envs(tex_envs: Any) -> dict[str, dict[str, Any]]:
    """Normalize m_TexEnvs list-of-dicts into a flat name -> texture dict."""
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(tex_envs, list):
        return result
    for entry in tex_envs:
        if not isinstance(entry, dict):
            continue
        for name, value in entry.items():
            if isinstance(value, dict):
                result[name] = value
    return result


def _normalize_floats(floats: Any) -> dict[str, float]:
    """Normalize m_Floats list-of-dicts into a flat name -> value dict."""
    result: dict[str, float] = {}
    if not isinstance(floats, list):
        return result
    for entry in floats:
        if not isinstance(entry, dict):
            continue
        for name, value in entry.items():
            try:
                result[name] = float(value)
            except (TypeError, ValueError):
                pass
    return result


def _normalize_colors(colors: Any) -> dict[str, dict[str, float]]:
    """Normalize m_Colors list-of-dicts into a flat name -> {r,g,b,a} dict."""
    result: dict[str, dict[str, float]] = {}
    if not isinstance(colors, list):
        return result
    for entry in colors:
        if not isinstance(entry, dict):
            continue
        for name, value in entry.items():
            if isinstance(value, dict):
                result[name] = value
    return result


def _resolve_texture(
    tex_data: dict[str, Any],
    guid_index: GuidIndex | None,
) -> Path | None:
    """Resolve a texture reference to a file path via its GUID."""
    if guid_index is None:
        return None

    texture_ref = tex_data.get("m_Texture", {})
    if not isinstance(texture_ref, dict):
        return None

    guid = texture_ref.get("guid", "")
    if not guid:
        return None

    return guid_index.resolve(guid)
