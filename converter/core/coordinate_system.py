"""
coordinate_system.py -- Unity <-> Roblox coordinate transforms.

Unity: left-handed Y-up, Z-forward
Roblox: right-handed Y-up

Position: (x, y, z)_unity -> (x, y, -z)_roblox
Quaternion: (qx, qy, qz, qw)_unity -> (-qx, -qy, qz, qw)_roblox
"""

from __future__ import annotations

import math


def unity_to_roblox_pos(
    x: float, y: float, z: float,
) -> tuple[float, float, float]:
    """Convert Unity position (meters) to Roblox position (studs, Z-negated).

    Applies STUDS_PER_METER scaling (1 Unity meter ≈ 3.571 studs).
    """
    from config import STUDS_PER_METER
    return (x * STUDS_PER_METER, y * STUDS_PER_METER, -z * STUDS_PER_METER)


def _quat_multiply(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Multiply two quaternions (ax,ay,az,aw) * (bx,by,bz,bw)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


# Inverse of the -90° X rotation that FBX applies to convert Z-up meshes to Y-up.
# Unity stores scene rotations that include this pre-rotation, but Roblox applies
# it automatically during FBX import. We must strip it to avoid double-rotation.
_FBX_PREROT_INV = (math.sin(math.radians(45)), 0.0, 0.0, math.cos(math.radians(45)))


def needs_fbx_prerotation_strip(
    qx: float, qy: float, qz: float, qw: float,
) -> bool:
    """Detect whether a Unity quaternion includes the FBX -90° X pre-rotation.

    When a Z-up FBX mesh is imported by Unity, the rotation maps the mesh's
    local Z axis to world Y (up).  We detect this by checking whether the
    rotation's forward (local-Z) direction has a strong Y component.
    """
    # Forward Y = the Y component of where local-Z ends up in world space
    forward_y = abs(2.0 * (qy * qz - qx * qw))
    return forward_y > 0.9


def strip_fbx_prerotation(
    qx: float, qy: float, qz: float, qw: float,
) -> tuple[float, float, float, float]:
    """Remove the FBX -90° X pre-rotation from a Unity quaternion.

    FBX meshes authored in Z-up tools (3ds Max, Blender default) include a
    -90° X pre-rotation in the FBX root node to convert to Y-up.  Unity bakes
    this into the scene rotation, but Roblox applies it during mesh import.
    Stripping it avoids double-rotation (e.g. planes pointing nose-down).

    Only strips when the rotation actually contains the pre-rotation
    (detected by local-Z mapping to world-Y).
    """
    if not needs_fbx_prerotation_strip(qx, qy, qz, qw):
        return (qx, qy, qz, qw)
    return _quat_multiply((qx, qy, qz, qw), _FBX_PREROT_INV)


def unity_quat_to_roblox_quat(
    qx: float, qy: float, qz: float, qw: float,
) -> tuple[float, float, float, float]:
    """Convert Unity quaternion to Roblox quaternion.

    Negates X and Y to flip handedness while preserving Z and W.
    """
    return (-qx, -qy, qz, qw)


def quaternion_to_rotation_matrix(
    qx: float, qy: float, qz: float, qw: float,
) -> tuple[float, float, float,
           float, float, float,
           float, float, float]:
    """Convert quaternion to 3x3 rotation matrix (row-major: R00..R22).

    Normalizes the quaternion first to avoid numerical drift.
    Returns 9 floats: R00, R01, R02, R10, R11, R12, R20, R21, R22.
    """
    # Normalize
    mag = math.sqrt(qx * qx + qy * qy + qz * qz + qw * qw)
    if mag < 1e-10:
        return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    qx /= mag
    qy /= mag
    qz /= mag
    qw /= mag

    # Rotation matrix from quaternion
    xx = qx * qx
    yy = qy * qy
    zz = qz * qz
    xy = qx * qy
    xz = qx * qz
    yz = qy * qz
    wx = qw * qx
    wy = qw * qy
    wz = qw * qz

    r00 = 1.0 - 2.0 * (yy + zz)
    r01 = 2.0 * (xy - wz)
    r02 = 2.0 * (xz + wy)
    r10 = 2.0 * (xy + wz)
    r11 = 1.0 - 2.0 * (xx + zz)
    r12 = 2.0 * (yz - wx)
    r20 = 2.0 * (xz - wy)
    r21 = 2.0 * (yz + wx)
    r22 = 1.0 - 2.0 * (xx + yy)

    return (r00, r01, r02, r10, r11, r12, r20, r21, r22)


def unity_transform_to_roblox_cframe(
    pos: tuple[float, float, float],
    quat: tuple[float, float, float, float],
) -> tuple[float, float, float,
           float, float, float, float,
           float, float, float, float,
           float, float, float]:
    """Convert Unity position+quaternion to Roblox CFrame components.

    Returns (X, Y, Z, R00..R22) — 12 floats total for RBXLX serialization.
    """
    rx, ry, rz = unity_to_roblox_pos(*pos)
    rqx, rqy, rqz, rqw = unity_quat_to_roblox_quat(*quat)
    mat = quaternion_to_rotation_matrix(rqx, rqy, rqz, rqw)
    return (rx, ry, rz, *mat)


def unity_scale_to_roblox_size(
    scale: tuple[float, float, float],
    base_size: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> tuple[float, float, float]:
    """Convert Unity scale to Roblox Part size.

    Multiplies each axis of scale by the base size.
    No Z-negation needed for size (it's a magnitude).
    """
    return (
        abs(scale[0] * base_size[0]),
        abs(scale[1] * base_size[1]),
        abs(scale[2] * base_size[2]),
    )


def euler_to_quaternion(
    x_deg: float, y_deg: float, z_deg: float,
) -> tuple[float, float, float, float]:
    """Convert Euler angles (degrees, Unity order: ZXY) to quaternion."""
    x = math.radians(x_deg)
    y = math.radians(y_deg)
    z = math.radians(z_deg)

    cx, sx = math.cos(x / 2), math.sin(x / 2)
    cy, sy = math.cos(y / 2), math.sin(y / 2)
    cz, sz = math.cos(z / 2), math.sin(z / 2)

    # Unity uses ZXY rotation order
    qw = cx * cy * cz + sx * sy * sz
    qx = sx * cy * cz + cx * sy * sz
    qy = cx * sy * cz - sx * cy * sz
    qz = cx * cy * sz - sx * sy * cz

    return (qx, qy, qz, qw)
