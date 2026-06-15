"""project_gravity.py -- read Unity's project gravity from DynamicsManager.asset.

Relation #8 (scale-faithful gravity), Phase 1. The converter scales geometry at
``STUDS_PER_METER`` but dynamic bodies fall under Roblox's default 196.2 studs/s²;
the per-assembly gravity correction needs the REAL project gravity magnitude, not a
frozen 9.81. This module is the single deterministic-upstream source for that scalar.

Unity's ``ProjectSettings/DynamicsManager.asset`` is Unity-flavoured YAML with a
``%TAG !u! tag:unity3d.com,2011:`` header that breaks ``yaml.safe_load``. We do a
targeted single-line extraction of the ``m_Gravity: {x: 0, y: -9.81, z: 0}`` map
(NOT a full YAML load), mirroring how the converter handles Unity-YAML elsewhere.
The correction is a world-down Y-axis force, so the scalar is ``abs(m_Gravity.y)``.
Never raises -- a missing file / field, or a non-uniform vector, returns the 9.81
default (or fails open to ``abs(y)``) with a warning.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# |m_Gravity.y| Unity default (9.81 m/s²). Used when the asset / field is absent
# or unparseable, so the default path stays exercised even with no project file.
DEFAULT_UNITY_GRAVITY_Y: float = 9.81

# ``m_Gravity: {x: 0, y: -9.81, z: 0}`` -- a single inline-mapping line. The
# ``m_Cloth*``/``m_ClothGravity`` siblings start with a different prefix, so the
# bounded ``^\s*m_Gravity:`` anchor (start of the field name) does not collide.
_GRAVITY_LINE_RE = re.compile(r"^\s*m_Gravity:\s*\{(?P<body>[^}\r\n]*)\}", re.MULTILINE)


def _extract_component(body: str, axis: str) -> float | None:
    """Pull ``<axis>: <float>`` from the inline ``{x: .., y: .., z: ..}`` body."""
    m = re.search(rf"\b{axis}:\s*(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)", body)
    if m is None:
        return None
    return float(m.group(1))


def parse_project_gravity_y(
    unity_project_root: Path,
    warn: Callable[[str], None] = log.warning,
) -> float:
    """Return ``abs(m_Gravity.y)`` from ``ProjectSettings/DynamicsManager.asset``.

    9.81 default when the file or the ``m_Gravity`` field is absent/unparseable.
    Magnitude of the Y component ONLY (free fall is world-down). Warns + fails
    open to ``abs(y)`` when ``m_Gravity.x`` or ``.z`` is non-zero (non-uniform
    gravity = out of scope). Never raises.
    """
    asset = Path(unity_project_root) / "ProjectSettings" / "DynamicsManager.asset"
    try:
        text = asset.read_text(encoding="utf-8", errors="replace")
    except OSError:
        warn(
            f"[project_gravity] {asset} not readable; "
            f"defaulting gravity to {DEFAULT_UNITY_GRAVITY_Y}"
        )
        return DEFAULT_UNITY_GRAVITY_Y

    match = _GRAVITY_LINE_RE.search(text)
    if match is None:
        warn(
            f"[project_gravity] m_Gravity not found in {asset}; "
            f"defaulting gravity to {DEFAULT_UNITY_GRAVITY_Y}"
        )
        return DEFAULT_UNITY_GRAVITY_Y

    body = match.group("body")
    y = _extract_component(body, "y")
    if y is None:
        warn(
            f"[project_gravity] m_Gravity.y missing in {asset}; "
            f"defaulting gravity to {DEFAULT_UNITY_GRAVITY_Y}"
        )
        return DEFAULT_UNITY_GRAVITY_Y

    x = _extract_component(body, "x") or 0.0
    z = _extract_component(body, "z") or 0.0
    if x != 0.0 or z != 0.0:
        warn(
            f"[project_gravity] non-uniform gravity {{x: {x}, y: {y}, z: {z}}} "
            f"in {asset} (out of scope); failing open to abs(y)"
        )

    return abs(y)
