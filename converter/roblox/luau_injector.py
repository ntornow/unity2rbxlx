"""
Luau scene injection script builder.

Generates ordered Luau scripts that, when executed sequentially inside Roblox
Studio (via the MCP ``execute_luau`` tool), reconstruct a full scene from an
``RbxPlace`` intermediate representation.

Each script is kept under 50 KB to avoid MCP execution timeouts.
"""

from __future__ import annotations

import textwrap
from typing import Sequence

from core.roblox_types import (
    RbxPart,
    RbxCFrame,
    RbxLightingConfig,
    RbxCameraConfig,
    RbxPlace,
)
from core.coordinate_system import quaternion_to_rotation_matrix

# Maximum bytes per individual Luau script chunk.
_MAX_CHUNK_BYTES = 50_000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cframe_ctor(cf: RbxCFrame) -> str:
    mat = quaternion_to_rotation_matrix(cf.qx, cf.qy, cf.qz, cf.qw)
    if isinstance(mat, (list, tuple)) and isinstance(mat[0], (list, tuple)):
        flat = [mat[r][c] for r in range(3) for c in range(3)]
    else:
        flat = list(mat[:9])
    args = ", ".join(str(v) for v in [cf.x, cf.y, cf.z] + flat)
    return f"CFrame.new({args})"


def _color3_ctor(color: tuple | list) -> str:
    r, g, b = color[:3]
    if all(isinstance(c, float) and c <= 1.0 for c in (r, g, b)):
        return f"Color3.new({r}, {g}, {b})"
    return f"Color3.fromRGB({int(r)}, {int(g)}, {int(b)})"


def _vector3_ctor(x: float, y: float, z: float) -> str:
    return f"Vector3.new({x}, {y}, {z})"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _next_var(counter: list[int]) -> str:
    """Return a unique variable name and increment the counter."""
    idx = counter[0]
    counter[0] += 1
    return f"p{idx}"


# ---------------------------------------------------------------------------
# Part tree builder
# ---------------------------------------------------------------------------

def _build_part_tree_luau(
    part: RbxPart,
    parent_var: str,
    var_counter: list[int],
) -> list[str]:
    """Recursively generate Luau lines that create *part* and its children.

    Parameters
    ----------
    part:
        The part (or model) to create.
    parent_var:
        Luau variable name of the parent Instance.
    var_counter:
        Single-element list used as a mutable counter for unique var names.

    Returns
    -------
    list[str]
        Lines of Luau code.
    """
    class_name = getattr(part, "class_name", "Part")
    name = getattr(part, "name", "Part")
    var = _next_var(var_counter)

    lines: list[str] = []
    lines.append(f'local {var} = Instance.new("{class_name}")')
    lines.append(f'{var}.Name = "{_escape(name)}"')

    if class_name == "Model":
        if hasattr(part, "cframe") and part.cframe:
            lines.append(f"{var}.WorldPivot = {_cframe_ctor(part.cframe)}")
    else:
        if hasattr(part, "cframe") and part.cframe:
            lines.append(f"{var}.CFrame = {_cframe_ctor(part.cframe)}")
        if hasattr(part, "size") and part.size:
            lines.append(f"{var}.Size = {_vector3_ctor(*part.size)}")
        if hasattr(part, "color") and part.color:
            lines.append(f"{var}.Color = {_color3_ctor(part.color)}")
        if hasattr(part, "material") and part.material is not None:
            lines.append(f"{var}.Material = Enum.Material:FromValue({part.material})")
        if hasattr(part, "transparency") and part.transparency:
            lines.append(f"{var}.Transparency = {part.transparency}")

        anchored = getattr(part, "anchored", True)
        lines.append(f"{var}.Anchored = {'true' if anchored else 'false'}")

        if hasattr(part, "can_collide"):
            lines.append(f"{var}.CanCollide = {'true' if part.can_collide else 'false'}")

        if class_name == "MeshPart" and hasattr(part, "mesh_id") and part.mesh_id:
            lines.append(f'{var}.MeshId = "{_escape(part.mesh_id)}"')

    lines.append(f"{var}.Parent = {parent_var}")
    lines.append("")

    # Surface appearances
    for sa in getattr(part, "surface_appearances", None) or []:
        sa_var = _next_var(var_counter)
        lines.append(f'local {sa_var} = Instance.new("SurfaceAppearance")')
        if hasattr(sa, "color_map") and sa.color_map:
            lines.append(f'{sa_var}.ColorMap = "{_escape(sa.color_map)}"')
        if hasattr(sa, "metalness_map") and sa.metalness_map:
            lines.append(f'{sa_var}.MetalnessMap = "{_escape(sa.metalness_map)}"')
        if hasattr(sa, "normal_map") and sa.normal_map:
            lines.append(f'{sa_var}.NormalMap = "{_escape(sa.normal_map)}"')
        if hasattr(sa, "roughness_map") and sa.roughness_map:
            lines.append(f'{sa_var}.RoughnessMap = "{_escape(sa.roughness_map)}"')
        lines.append(f"{sa_var}.Parent = {var}")
        lines.append("")

    # Lights
    for light in getattr(part, "lights", None) or []:
        light_class = getattr(light, "light_type", "PointLight")
        l_var = _next_var(var_counter)
        lines.append(f'local {l_var} = Instance.new("{light_class}")')
        if hasattr(light, "brightness"):
            lines.append(f"{l_var}.Brightness = {light.brightness}")
        if hasattr(light, "color") and light.color:
            lines.append(f"{l_var}.Color = {_color3_ctor(light.color)}")
        if hasattr(light, "range"):
            lines.append(f"{l_var}.Range = {light.range}")
        if hasattr(light, "enabled"):
            lines.append(f"{l_var}.Enabled = {'true' if light.enabled else 'false'}")
        if hasattr(light, "shadows"):
            lines.append(f"{l_var}.Shadows = {'true' if light.shadows else 'false'}")
        lines.append(f"{l_var}.Parent = {var}")
        lines.append("")

    # Sounds
    for sound in getattr(part, "sounds", None) or []:
        s_var = _next_var(var_counter)
        lines.append(f'local {s_var} = Instance.new("Sound")')
        if hasattr(sound, "name") and sound.name:
            lines.append(f'{s_var}.Name = "{_escape(sound.name)}"')
        if hasattr(sound, "sound_id") and sound.sound_id:
            lines.append(f'{s_var}.SoundId = "{_escape(sound.sound_id)}"')
        if hasattr(sound, "volume"):
            lines.append(f"{s_var}.Volume = {sound.volume}")
        if hasattr(sound, "looped"):
            lines.append(f"{s_var}.Looped = {'true' if sound.looped else 'false'}")
        lines.append(f"{s_var}.Parent = {var}")
        lines.append("")

    # Recurse into children
    for child in getattr(part, "children", None) or []:
        lines.extend(_build_part_tree_luau(child, var, var_counter))

    return lines


# ---------------------------------------------------------------------------
# Lighting & Camera builders
# ---------------------------------------------------------------------------

def _build_lighting_luau(lighting: RbxLightingConfig) -> str:
    """Generate Luau code to configure the Lighting service."""
    lines: list[str] = [
        'local lighting = game:GetService("Lighting")',
    ]
    if hasattr(lighting, "brightness") and lighting.brightness is not None:
        lines.append(f"lighting.Brightness = {lighting.brightness}")
    if hasattr(lighting, "ambient") and lighting.ambient:
        lines.append(f"lighting.Ambient = {_color3_ctor(lighting.ambient)}")
    if hasattr(lighting, "outdoor_ambient") and lighting.outdoor_ambient:
        lines.append(f"lighting.OutdoorAmbient = {_color3_ctor(lighting.outdoor_ambient)}")
    if hasattr(lighting, "clock_time") and lighting.clock_time is not None:
        lines.append(f"lighting.ClockTime = {lighting.clock_time}")
    if hasattr(lighting, "geographic_latitude") and lighting.geographic_latitude is not None:
        lines.append(f"lighting.GeographicLatitude = {lighting.geographic_latitude}")
    if hasattr(lighting, "fog_color") and lighting.fog_color:
        lines.append(f"lighting.FogColor = {_color3_ctor(lighting.fog_color)}")
    if hasattr(lighting, "fog_start") and lighting.fog_start is not None:
        lines.append(f"lighting.FogStart = {lighting.fog_start}")
    if hasattr(lighting, "fog_end") and lighting.fog_end is not None:
        lines.append(f"lighting.FogEnd = {lighting.fog_end}")
    lines.append("")
    return "\n".join(lines)


def _build_camera_luau(camera: RbxCameraConfig) -> str:
    """Generate Luau code to configure the workspace camera."""
    lines: list[str] = [
        "local camera = workspace.CurrentCamera",
        "camera.CameraType = Enum.CameraType.Scriptable",
    ]
    if hasattr(camera, "cframe") and camera.cframe:
        lines.append(f"camera.CFrame = {_cframe_ctor(camera.cframe)}")
    if hasattr(camera, "field_of_view") and camera.field_of_view is not None:
        lines.append(f"camera.FieldOfView = {camera.field_of_view}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_injection_scripts(place: RbxPlace) -> list[str]:
    """Generate an ordered list of Luau scripts that, when executed
    sequentially in Roblox Studio, fully reconstruct the scene described
    by *place*.

    Each returned script is under 50 KB.

    Parameters
    ----------
    place:
        The intermediate place representation to inject.

    Returns
    -------
    list[str]
        Ordered Luau scripts.
    """
    all_scripts: list[str] = []

    # --- 1. Lighting configuration ----------------------------------------
    lighting_config: RbxLightingConfig | None = getattr(place, "lighting", None)
    if lighting_config:
        all_scripts.append(_build_lighting_luau(lighting_config))

    # --- 2. Camera configuration ------------------------------------------
    camera_config: RbxCameraConfig | None = getattr(place, "camera", None)
    if camera_config:
        all_scripts.append(_build_camera_luau(camera_config))

    # --- 3. Parts (chunked) -----------------------------------------------
    parts: list[RbxPart] = getattr(place, "parts", None) or []

    var_counter = [0]
    current_lines: list[str] = [
        "-- Scene parts injection",
        "",
    ]
    current_bytes = 0

    for part in parts:
        part_lines = _build_part_tree_luau(part, "workspace", var_counter)
        snippet = "\n".join(part_lines)
        snippet_bytes = len(snippet.encode("utf-8"))

        # If adding this part would exceed the limit, flush the current chunk.
        if current_bytes + snippet_bytes > _MAX_CHUNK_BYTES and len(current_lines) > 2:
            all_scripts.append("\n".join(current_lines))
            current_lines = ["-- Scene parts injection (continued)", ""]
            current_bytes = 0

        current_lines.extend(part_lines)
        current_bytes += snippet_bytes

    if len(current_lines) > 2:
        all_scripts.append("\n".join(current_lines))

    # --- 4. Standalone scripts (ServerScriptService etc.) -----------------
    scripts = getattr(place, "scripts", None) or []
    for script_obj in scripts:
        stype = getattr(script_obj, "script_type", "Script")
        name = getattr(script_obj, "name", stype)
        source = getattr(script_obj, "source", "")

        if stype == "Script":
            parent_path = 'game:GetService("ServerScriptService")'
        elif stype == "LocalScript":
            parent_path = 'game:GetService("StarterPlayer").StarterPlayerScripts'
        elif stype == "ModuleScript":
            parent_path = 'game:GetService("ReplicatedStorage")'
        else:
            parent_path = 'game:GetService("ServerScriptService")'

        escaped_source = source.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        injection = textwrap.dedent(f"""\
            local script_inst = Instance.new("{stype}")
            script_inst.Name = "{_escape(name)}"
            script_inst.Source = "{escaped_source}"
            script_inst.Parent = {parent_path}
        """)

        # If a single script source is very large, it becomes its own chunk.
        all_scripts.append(injection)

    return all_scripts
