"""Automated transform audit tests.

Verifies that converted Roblox parts match Unity scene positions and rotations
within tolerance. Catches systematic rotation/position bugs that break placement.

Unit tests for coordinate math run always. Integration tests run as part of
the slow suite against any conversion output.
"""
import math
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.transform_audit import (
    parse_rbxlx,
    parse_unity_scene_transforms,
    compare_transforms,
    quat_angle_diff,
    quat_multiply,
    quat_rotate,
    unity_to_roblox_pos,
    roblox_to_unity_pos,
)


# ---------------------------------------------------------------------------
# Unit tests for coordinate math
# ---------------------------------------------------------------------------

class TestCoordinateMath:
    def test_unity_to_roblox_pos_z_negation(self):
        rx, ry, rz = unity_to_roblox_pos(1.0, 2.0, 3.0)
        assert rz < 0

    def test_roblox_to_unity_roundtrip(self):
        orig = (5.0, 10.0, -3.0)
        rbx = unity_to_roblox_pos(*orig)
        back = roblox_to_unity_pos(*rbx)
        for a, b in zip(orig, back):
            assert abs(a - b) < 0.001

    def test_quat_identity_angle(self):
        assert quat_angle_diff((0,0,0,1), (0,0,0,1)) < 0.01

    def test_quat_90_degree_diff(self):
        q1 = (0, 0, 0, 1)
        q2 = (0, math.sin(math.radians(45)), 0, math.cos(math.radians(45)))
        assert abs(quat_angle_diff(q1, q2) - 90.0) < 1.0

    def test_quat_rotate_identity(self):
        result = quat_rotate((0,0,0,1), (1,2,3))
        for a, b in zip((1,2,3), result):
            assert abs(a - b) < 0.001

    def test_quat_multiply_identity(self):
        q = (0.1, 0.2, 0.3, 0.9)
        result = quat_multiply(q, (0,0,0,1))
        for a, b in zip(q, result):
            assert abs(a - b) < 0.001


class TestStripFbxPrerotation:
    def test_identity_not_stripped(self):
        from core.coordinate_system import needs_fbx_prerotation_strip
        assert not needs_fbx_prerotation_strip(0, 0, 0, 1)

    def test_90deg_x_detected(self):
        from core.coordinate_system import needs_fbx_prerotation_strip
        assert needs_fbx_prerotation_strip(-0.7071068, 0, 0, 0.7071068)

    def test_arbitrary_y_rotation_not_detected(self):
        from core.coordinate_system import needs_fbx_prerotation_strip
        q = (0, math.sin(math.radians(22.5)), 0, math.cos(math.radians(22.5)))
        assert not needs_fbx_prerotation_strip(*q)

    def test_strip_preserves_when_not_needed(self):
        from core.coordinate_system import strip_fbx_prerotation
        q = (0, 0, 0, 1)
        result = strip_fbx_prerotation(*q)
        for a, b in zip(q, result):
            assert abs(a - b) < 0.001


# ---------------------------------------------------------------------------
# Generic conversion verification
# ---------------------------------------------------------------------------

def verify_conversion_transforms(
    unity_scene_path: str | Path,
    rbxlx_path: str | Path,
    rot_threshold_deg: float = 10.0,
    max_allowed_rot_errors: int = 0,
) -> list[dict]:
    """Verify transforms for ANY conversion. Returns list of rotation errors.

    This is the core function that can be called from any test or the pipeline
    itself to verify placement accuracy.
    """
    roblox_data = parse_rbxlx(str(rbxlx_path))
    unity_data = parse_unity_scene_transforms(str(unity_scene_path))
    discrepancies = compare_transforms(
        unity_data, roblox_data,
        pos_threshold=999999,  # focus on rotation
        rot_threshold=rot_threshold_deg,
    )
    rot_errors = [d for d in discrepancies if d['rot_error_deg'] > rot_threshold_deg]
    return rot_errors


# ---------------------------------------------------------------------------
# Integration tests — discover and test all available conversions
# ---------------------------------------------------------------------------

def _find_conversion_outputs():
    """Find all conversion outputs that have both a Unity scene and rbxlx."""
    output_dir = Path(__file__).parent.parent / "output"
    test_projects = Path(__file__).parent.parent.parent / "test_projects"
    conversions = []

    if not output_dir.exists():
        return conversions

    for project_dir in sorted(output_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        rbxlx = project_dir / "converted_place.rbxlx"
        if not rbxlx.exists():
            continue

        # Find the matching Unity scene
        unity_project = test_projects / project_dir.name
        if not unity_project.exists():
            continue

        # Search for .unity scene files
        scenes = list(unity_project.rglob("Assets/Scenes/*.unity"))
        if not scenes:
            continue

        # Use the first non-library scene
        scene = None
        for s in scenes:
            if "Library" not in str(s) and "PackageCache" not in str(s):
                scene = s
                break
        if scene:
            conversions.append((project_dir.name, scene, rbxlx))

    return conversions


_CONVERSIONS = _find_conversion_outputs()


@pytest.mark.slow
@pytest.mark.parametrize(
    "project_name,unity_scene,rbxlx",
    _CONVERSIONS,
    ids=[c[0] for c in _CONVERSIONS],
)
class TestConversionTransforms:
    """Verify transforms for all available conversion outputs."""

    def test_rbxlx_has_parts(self, project_name, unity_scene, rbxlx):
        data = parse_rbxlx(str(rbxlx))
        total = sum(len(v) for v in data.values())
        assert total > 0, f"{project_name}: rbxlx has no parts"

    def test_zero_placement_errors(self, project_name, unity_scene, rbxlx):
        """Every object must have correct position and rotation — zero errors."""
        roblox_data = parse_rbxlx(str(rbxlx))
        unity_data = parse_unity_scene_transforms(str(unity_scene))
        # 0.01° rotation, 0.01m position tolerance (floating point only)
        discrepancies = compare_transforms(
            unity_data, roblox_data,
            pos_threshold=0.01, rot_threshold=0.01,
        )
        assert len(discrepancies) == 0, (
            f"{project_name}: {len(discrepancies)} objects have placement errors:\n"
            + "\n".join(
                f"  {d['name']}: pos={d['pos_error_m']:.2f}m rot={d['rot_error_deg']:.2f}° ({d.get('path','')})"
                for d in discrepancies[:20]
            )
        )
