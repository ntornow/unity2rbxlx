"""
test_coordinate_system.py -- Tests for coordinate system transforms.
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.coordinate_system import (
    unity_to_roblox_pos,
    roblox_to_unity_pos,
    unity_quat_to_roblox_quat,
    roblox_quat_to_unity_quat,
    quaternion_to_rotation_matrix,
    rotation_matrix_to_quat,
    unity_transform_to_roblox_cframe,
    unity_scale_to_roblox_size,
    euler_to_quaternion,
)


class TestUnityToRobloxPos:
    def test_z_negation(self):
        from config import STUDS_PER_METER as S
        x, y, z = unity_to_roblox_pos(1.0, 2.0, 3.0)
        assert abs(x - 1.0 * S) < 0.001
        assert abs(y - 2.0 * S) < 0.001
        assert abs(z - (-3.0 * S)) < 0.001

    def test_zero(self):
        assert unity_to_roblox_pos(0, 0, 0) == (0, 0, 0)

    def test_negative_z(self):
        from config import STUDS_PER_METER as S
        x, y, z = unity_to_roblox_pos(1, 2, -5)
        assert abs(x - 1 * S) < 0.001
        assert abs(z - 5 * S) < 0.001

    def test_scales_to_studs(self):
        from config import STUDS_PER_METER as S
        x, y, z = unity_to_roblox_pos(10, 20, 30)
        assert abs(x - 10 * S) < 0.001
        assert abs(y - 20 * S) < 0.001


class TestUnityQuatToRobloxQuat:
    def test_identity(self):
        result = unity_quat_to_roblox_quat(0, 0, 0, 1)
        assert result == (0, 0, 0, 1)

    def test_negates_xy(self):
        result = unity_quat_to_roblox_quat(0.5, 0.5, 0.5, 0.5)
        assert result == (-0.5, -0.5, 0.5, 0.5)

    def test_preserves_zw(self):
        _, _, qz, qw = unity_quat_to_roblox_quat(0.1, 0.2, 0.3, 0.4)
        assert qz == 0.3
        assert qw == 0.4


class TestQuaternionToRotationMatrix:
    def test_identity(self):
        mat = quaternion_to_rotation_matrix(0, 0, 0, 1)
        # Identity matrix
        assert abs(mat[0] - 1.0) < 1e-6  # R00
        assert abs(mat[4] - 1.0) < 1e-6  # R11
        assert abs(mat[8] - 1.0) < 1e-6  # R22
        # Off-diagonals should be ~0
        assert abs(mat[1]) < 1e-6  # R01
        assert abs(mat[2]) < 1e-6  # R02
        assert abs(mat[3]) < 1e-6  # R10

    def test_90_deg_y_rotation(self):
        # 90 degrees around Y axis
        angle = math.pi / 2
        qw = math.cos(angle / 2)
        qy = math.sin(angle / 2)
        mat = quaternion_to_rotation_matrix(0, qy, 0, qw)
        # R00 should be ~0, R02 should be ~1
        assert abs(mat[0]) < 1e-6       # R00 ≈ 0
        assert abs(mat[2] - 1.0) < 1e-6  # R02 ≈ 1
        assert abs(mat[4] - 1.0) < 1e-6  # R11 ≈ 1

    def test_zero_quaternion_gives_identity(self):
        mat = quaternion_to_rotation_matrix(0, 0, 0, 0)
        assert mat == (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


class TestUnityTransformToRobloxCFrame:
    def test_identity_transform(self):
        result = unity_transform_to_roblox_cframe(
            (0, 0, 0), (0, 0, 0, 1)
        )
        # Position should be (0, 0, 0)
        assert result[0] == 0  # X
        assert result[1] == 0  # Y
        assert result[2] == 0  # Z
        # Rotation should be identity
        assert abs(result[3] - 1.0) < 1e-6  # R00

    def test_z_negation_in_cframe(self):
        from config import STUDS_PER_METER as S
        result = unity_transform_to_roblox_cframe(
            (1, 2, 3), (0, 0, 0, 1)
        )
        assert abs(result[0] - 1.0 * S) < 0.001  # X scaled
        assert abs(result[1] - 2.0 * S) < 0.001  # Y scaled
        assert abs(result[2] - (-3.0 * S)) < 0.001  # Z negated + scaled


class TestUnityScaleToRobloxSize:
    def test_simple_scale(self):
        assert unity_scale_to_roblox_size((2, 3, 4)) == (2, 3, 4)

    def test_with_base_size(self):
        assert unity_scale_to_roblox_size((2, 3, 4), (1, 2, 0.5)) == (2, 6, 2)

    def test_negative_scale_abs(self):
        result = unity_scale_to_roblox_size((-1, 2, -3))
        assert result[0] == 1  # abs
        assert result[2] == 3  # abs


class TestEulerToQuaternion:
    def test_zero_euler(self):
        q = euler_to_quaternion(0, 0, 0)
        assert abs(q[3] - 1.0) < 1e-6  # w ≈ 1
        assert abs(q[0]) < 1e-6         # x ≈ 0

    def test_90_deg_y(self):
        q = euler_to_quaternion(0, 90, 0)
        # qy should be sin(45°) ≈ 0.7071
        assert abs(q[1] - math.sin(math.radians(45))) < 1e-4


class TestRobloxToUnityPos:
    """roblox_to_unity_pos is the inverse of unity_to_roblox_pos."""

    def test_round_trip(self):
        for p in [(1.0, 2.0, 3.0), (-5.0, 0.0, 7.5), (0.0, 0.0, 0.0),
                  (-1.25, 9.0, -4.0)]:
            back = roblox_to_unity_pos(*unity_to_roblox_pos(*p))
            for a, b in zip(p, back):
                assert abs(a - b) < 1e-9

    def test_z_negation(self):
        # Roblox->Unity flips Z, like the forward transform.
        rx, ry, rz = unity_to_roblox_pos(0.0, 0.0, 1.0)
        ux, uy, uz = roblox_to_unity_pos(rx, ry, rz)
        assert abs(uz - 1.0) < 1e-9


class TestRobloxQuatToUnityQuat:
    """roblox_quat_to_unity_quat is the inverse of unity_quat_to_roblox_quat."""

    def test_involution(self):
        q = (0.1, 0.2, 0.3, 0.9)
        assert roblox_quat_to_unity_quat(*unity_quat_to_roblox_quat(*q)) == q

    def test_negates_xy(self):
        assert roblox_quat_to_unity_quat(1.0, 2.0, 3.0, 4.0) == (-1.0, -2.0, 3.0, 4.0)


class TestRotationMatrixToQuat:
    """rotation_matrix_to_quat is the inverse of quaternion_to_rotation_matrix."""

    @staticmethod
    def _matrices_close(m1, m2, tol=1e-6):
        return all(abs(a - b) < tol for a, b in zip(m1, m2))

    def test_round_trip_various(self):
        # Each quaternion is fed forward to a matrix and back; the recovered
        # quaternion must produce the same matrix (avoids q/-q sign ambiguity).
        quats = [
            (0.0, 0.0, 0.0, 1.0),                         # identity (trace>0 branch)
            (0.0, math.sin(math.radians(45)), 0.0,
             math.cos(math.radians(45))),                 # 90° about Y
            (1.0, 0.0, 0.0, 0.0),                         # 180° about X (r00 branch)
            (0.0, 0.0, 1.0, 0.0),                         # 180° about Z (r22 branch)
            # 0.2,0.9,0.3,0.1 lands in the r11-largest branch with r10 != r12 —
            # the exact case that caught a typo in the old transform_audit copy.
            (0.2, 0.9, 0.3, 0.1),
            (-0.4, 0.55, -0.6, 0.42),                     # arbitrary, unnormalized
        ]
        for q in quats:
            m = quaternion_to_rotation_matrix(*q)
            m2 = quaternion_to_rotation_matrix(*rotation_matrix_to_quat(m))
            assert self._matrices_close(m, m2), f"round-trip failed for {q}"

    def test_identity(self):
        x, y, z, w = rotation_matrix_to_quat(
            (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        )
        assert abs(x) < 1e-9 and abs(y) < 1e-9 and abs(z) < 1e-9
        assert abs(abs(w) - 1.0) < 1e-9
