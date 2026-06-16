"""AC9 -- parse abs(m_Gravity.y) from Unity's DynamicsManager.asset.

The scale-faithful gravity correction (relation #8) anchors its target accel on
the REAL project gravity, parsed from ``ProjectSettings/DynamicsManager.asset``
(NOT a frozen 9.81). The asset is Unity-flavoured YAML with a ``%TAG`` header
that breaks ``yaml.safe_load``, so the parser does a targeted line extraction of
the inline ``m_Gravity: {x, y, z}`` map and returns ``abs(y)``.

Pure-Python; no luau interpreter. Asserts ``repr()``-formatted floats where the
exact emitted value is load-bearing.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.project_gravity import (
    DEFAULT_UNITY_GRAVITY_Y,
    parse_project_gravity_y,
)


# A real-shaped DynamicsManager.asset, including the %TAG header that breaks
# yaml.safe_load. m_Gravity sits among the sibling fields (incl. m_ClothGravity,
# which must NOT collide with the m_Gravity anchor).
_ASSET_TEMPLATE = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!55 &1
PhysicsManager:
  m_ObjectHideFlags: 0
  serializedVersion: 13
  m_Gravity: {{x: {x}, y: {y}, z: {z}}}
  m_DefaultMaterial: {{fileID: 0}}
  m_BounceThreshold: 2
  m_DefaultMaxDepenetrationVelocity: 10
  m_SleepThreshold: 0.005
  m_ClothGravity: {{x: 0, y: -9.81, z: 0}}
"""


def _write_asset(root: Path, *, x: object, y: object, z: object) -> Path:
    settings = root / "ProjectSettings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "DynamicsManager.asset").write_text(
        _ASSET_TEMPLATE.format(x=x, y=y, z=z), encoding="utf-8"
    )
    return root


def _silent(_msg: str) -> None:
    pass


def test_parse_default_negative_y(tmp_path: Path) -> None:
    """The Unity default ``{x: 0, y: -9.81, z: 0}`` -> abs(y) == 9.81."""
    _write_asset(tmp_path, x=0, y=-9.81, z=0)
    g = parse_project_gravity_y(tmp_path, warn=_silent)
    assert repr(g) == repr(9.81)


def test_parse_abs_of_negative(tmp_path: Path) -> None:
    """A non-default magnitude is returned as abs(y)."""
    _write_asset(tmp_path, x=0, y=-20.0, z=0)
    g = parse_project_gravity_y(tmp_path, warn=_silent)
    assert repr(g) == repr(20.0)


def test_parse_positive_y_is_abs(tmp_path: Path) -> None:
    """A (pathological) positive y is still returned as a magnitude."""
    _write_asset(tmp_path, x=0, y=9.81, z=0)
    g = parse_project_gravity_y(tmp_path, warn=_silent)
    assert repr(g) == repr(9.81)


def test_parse_zero_gravity(tmp_path: Path) -> None:
    """Zero gravity (a valid project) -> 0.0, NOT the default."""
    _write_asset(tmp_path, x=0, y=0, z=0)
    g = parse_project_gravity_y(tmp_path, warn=_silent)
    assert repr(g) == repr(0.0)


def test_default_when_file_absent(tmp_path: Path) -> None:
    """No DynamicsManager.asset -> the 9.81 default (with a warning)."""
    warnings: list[str] = []
    g = parse_project_gravity_y(tmp_path, warn=warnings.append)
    assert repr(g) == repr(DEFAULT_UNITY_GRAVITY_Y)
    assert warnings, "absent file should warn"


def test_default_when_field_absent(tmp_path: Path) -> None:
    """A DynamicsManager.asset with no m_Gravity line -> the default."""
    settings = tmp_path / "ProjectSettings"
    settings.mkdir(parents=True)
    (settings / "DynamicsManager.asset").write_text(
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!55 &1\nPhysicsManager:\n  m_BounceThreshold: 2\n",
        encoding="utf-8",
    )
    warnings: list[str] = []
    g = parse_project_gravity_y(tmp_path, warn=warnings.append)
    assert repr(g) == repr(DEFAULT_UNITY_GRAVITY_Y)
    assert warnings, "absent field should warn"


def test_nonuniform_warns_and_fails_open_to_abs_y(tmp_path: Path) -> None:
    """Non-zero x/z (non-uniform gravity, OOS) warns + fails open to abs(y)."""
    _write_asset(tmp_path, x=3.0, y=-9.81, z=0)
    warnings: list[str] = []
    g = parse_project_gravity_y(tmp_path, warn=warnings.append)
    assert repr(g) == repr(9.81)
    assert any("non-uniform" in w for w in warnings), "non-uniform x must warn"


def test_nonzero_z_warns(tmp_path: Path) -> None:
    """A non-zero z component also triggers the non-uniform warning."""
    _write_asset(tmp_path, x=0, y=-9.81, z=2.5)
    warnings: list[str] = []
    g = parse_project_gravity_y(tmp_path, warn=warnings.append)
    assert repr(g) == repr(9.81)
    assert any("non-uniform" in w for w in warnings)


def test_malformed_unclosed_line_falls_back_to_default(tmp_path: Path) -> None:
    """A malformed m_Gravity line (missing closing ``}``) must NOT span newlines
    and mis-parse into the next inline map; it falls back to the 9.81 default.

    Pre-fix the body matcher was ``[^}]*`` which spans newlines, so an unclosed
    m_Gravity line would consume through to the first ``}`` on the following
    ``m_ClothGravity`` line and return abs of THAT line's y (-9.81 here, but the
    regression is the wrong-line read; we choose a distinctive cloth y so the
    pre-fix code returns the cloth magnitude, not the default).
    """
    settings = tmp_path / "ProjectSettings"
    settings.mkdir(parents=True)
    # m_Gravity is missing its closing brace; m_ClothGravity below has a distinct
    # magnitude. The fixed single-line matcher must NOT match the unclosed line
    # (no ``}`` on it) -> falls back to the default.
    (settings / "DynamicsManager.asset").write_text(
        "%YAML 1.1\n"
        "%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!55 &1\n"
        "PhysicsManager:\n"
        "  m_Gravity: {x: 0, y: -20.0, z: 0\n"
        "  m_ClothGravity: {x: 0, y: -77.0, z: 0}\n",
        encoding="utf-8",
    )
    g = parse_project_gravity_y(tmp_path, warn=_silent)
    # Fixed code: single-line matcher misses the unclosed line -> default.
    # Pre-fix code: [^}]* spans the newline, captures "x: 0, y: -20.0, z: 0\n
    # m_ClothGravity: {x: 0, y: -77.0, z: 0" -> first y in body is -20.0 ->
    # would return 20.0 (mis-parse), NOT the default.
    assert repr(g) == repr(DEFAULT_UNITY_GRAVITY_Y)


def test_does_not_pick_up_cloth_gravity(tmp_path: Path) -> None:
    """m_ClothGravity (a sibling) must NOT be read as m_Gravity."""
    # m_Gravity has a distinctive magnitude; m_ClothGravity stays at -9.81.
    _write_asset(tmp_path, x=0, y=-42.0, z=0)
    g = parse_project_gravity_y(tmp_path, warn=_silent)
    assert repr(g) == repr(42.0)
