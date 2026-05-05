"""Shared Luau-source emitters used by the Studio bridge and place builder.

These helpers serialize converter data classes into Luau constructor strings.
Duplicating them invites silent drift between the bridge and the headless
place builder, so any new emit helper goes here.
"""
from __future__ import annotations

from core.coordinate_system import quaternion_to_rotation_matrix
from core.roblox_types import RbxCFrame


def cframe_ctor(cf: RbxCFrame) -> str:
    """Return ``CFrame.new(x, y, z, R00..R22)`` for an RbxCFrame."""
    mat = quaternion_to_rotation_matrix(cf.qx, cf.qy, cf.qz, cf.qw)
    if isinstance(mat, (list, tuple)) and isinstance(mat[0], (list, tuple)):
        flat = [mat[r][c] for r in range(3) for c in range(3)]
    else:
        flat = list(mat[:9])
    args = ", ".join(str(v) for v in [cf.x, cf.y, cf.z] + flat)
    return f"CFrame.new({args})"


def color3_ctor(color: tuple | list) -> str:
    """Return ``Color3.new(...)`` for a 0..1 float triple, else ``Color3.fromRGB(...)``."""
    r, g, b = color[:3]
    if all(isinstance(c, float) and c <= 1.0 for c in (r, g, b)):
        return f"Color3.new({r}, {g}, {b})"
    return f"Color3.fromRGB({int(r)}, {int(g)}, {int(b)})"


def vector3_ctor(x: float, y: float, z: float) -> str:
    return f"Vector3.new({x}, {y}, {z})"


def escape_luau_string(s: str) -> str:
    """Escape a string for embedding inside Luau double-quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
