"""
Studio bridge -- Luau code generator for Roblox Studio MCP interaction.

This module generates Luau source strings that, when executed inside Roblox
Studio via the MCP ``execute_luau`` tool, create, inspect, or manipulate
instances in the DataModel.  It does **not** call MCP tools directly; the
calling layer is responsible for passing the returned strings to the
appropriate MCP transport.
"""

from __future__ import annotations

import json
import textwrap
from typing import Sequence

from core.roblox_types import RbxPart, RbxCFrame

# Maximum bytes per script chunk sent to execute_luau (stay under 50 KB to
# avoid MCP timeout issues).
_MAX_CHUNK_BYTES = 50_000


# ---------------------------------------------------------------------------
# Internal helpers (canonical implementations live in roblox.luau_emit).
# ---------------------------------------------------------------------------

from roblox.luau_emit import (  # noqa: E402
    cframe_ctor as _cframe_ctor,
    color3_ctor as _color3_ctor,
    vector3_ctor as _vector3_ctor,
    escape_luau_string as _escape_luau_string,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_create_instance_luau(part: RbxPart) -> str:
    """Generate Luau code that creates a single Instance representing *part*.

    The generated code creates the part under ``workspace`` by default.
    """
    class_name = getattr(part, "class_name", "Part")
    name = getattr(part, "name", "Part")

    lines: list[str] = []
    var = "inst"
    lines.append(f'local {var} = Instance.new("{class_name}")')
    lines.append(f'{var}.Name = "{_escape_luau_string(name)}"')

    if hasattr(part, "cframe") and part.cframe:
        lines.append(f"{var}.CFrame = {_cframe_ctor(part.cframe)}")

    if hasattr(part, "size") and part.size:
        sx, sy, sz = part.size
        lines.append(f"{var}.Size = {_vector3_ctor(sx, sy, sz)}")

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
        lines.append(f'{var}.MeshId = "{_escape_luau_string(part.mesh_id)}"')

    lines.append(f"{var}.Parent = workspace")
    lines.append("")

    return "\n".join(lines)


def generate_scene_luau(parts: list[RbxPart]) -> list[str]:
    """Generate chunked Luau scripts that recreate all *parts* in Workspace.

    Each returned string is under 50 KB so it can be safely passed to the
    MCP ``execute_luau`` tool without timeout.
    """
    chunks: list[str] = []
    current_lines: list[str] = [
        "-- Auto-generated scene injection script",
        "",
    ]
    current_bytes = 0

    for part in parts:
        snippet = generate_create_instance_luau(part)
        snippet_bytes = len(snippet.encode("utf-8"))

        if current_bytes + snippet_bytes > _MAX_CHUNK_BYTES and current_lines:
            chunks.append("\n".join(current_lines))
            current_lines = ["-- Scene injection (continued)", ""]
            current_bytes = 0

        current_lines.append(snippet)
        current_bytes += snippet_bytes

    if current_lines:
        chunks.append("\n".join(current_lines))

    return chunks


def generate_state_dump_luau() -> str:
    """Generate Luau code that dumps all BasePart positions/sizes to JSON.

    The script prints a JSON array to the output console where each element
    contains ``{name, className, position, rotation, size}``.
    """
    return textwrap.dedent("""\
        local HttpService = game:GetService("HttpService")
        local results = {}
        for _, obj in ipairs(workspace:GetDescendants()) do
            if obj:IsA("BasePart") then
                local cf = obj.CFrame
                local pos = cf.Position
                local rx, ry, rz = cf:ToEulerAnglesYXZ()
                table.insert(results, {
                    name = obj.Name,
                    className = obj.ClassName,
                    position = {x = pos.X, y = pos.Y, z = pos.Z},
                    rotation = {x = math.deg(rx), y = math.deg(ry), z = math.deg(rz)},
                    size = {x = obj.Size.X, y = obj.Size.Y, z = obj.Size.Z},
                })
            end
        end
        print(HttpService:JSONEncode(results))
    """)


def generate_screenshot_luau(camera_cframe: RbxCFrame) -> str:
    """Generate Luau code that positions the current camera for a screenshot.

    The calling layer should invoke the MCP screenshot tool after executing
    this script and waiting a short time for the viewport to update.
    """
    cf_str = _cframe_ctor(camera_cframe)
    return textwrap.dedent(f"""\
        local camera = workspace.CurrentCamera
        camera.CameraType = Enum.CameraType.Scriptable
        camera.CFrame = {cf_str}
        -- Camera positioned; take screenshot via MCP after a short delay.
    """)
