"""Tests for FBX binary Z-mirror and sub-mesh preservation."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.fbx_binary import (
    mirror_fbx_z_inplace,
    read_fbx,
    _find_geometry_nodes,
    _child,
    _flip_winding,
    _negate_z,
)


FBX_PATH = Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS" / "Assets" / "AssetPack" / "HornetRifle" / "Model" / "HornetRifle.fbx"


@pytest.mark.skipif(not FBX_PATH.exists(), reason="SimpleFPS test project not available")
class TestMirrorPreservesStructure:
    """Verify that Z-mirror preserves multi-sub-mesh structure."""

    def test_sub_mesh_count_preserved(self, tmp_path):
        dst = tmp_path / "mirrored.fbx"
        assert mirror_fbx_z_inplace(FBX_PATH, dst)

        _, roots_src = read_fbx(FBX_PATH)
        _, roots_dst = read_fbx(dst)
        g_src = _find_geometry_nodes(roots_src)
        g_dst = _find_geometry_nodes(roots_dst)
        assert len(g_src) == len(g_dst) == 14, "HornetRifle has 14 sub-meshes"

    def test_z_coordinates_negated(self, tmp_path):
        dst = tmp_path / "mirrored.fbx"
        mirror_fbx_z_inplace(FBX_PATH, dst)

        _, roots_src = read_fbx(FBX_PATH)
        _, roots_dst = read_fbx(dst)
        g_src = _find_geometry_nodes(roots_src)
        g_dst = _find_geometry_nodes(roots_dst)

        found_nonzero = False
        for gs, gd in zip(g_src, g_dst):
            vs = _child(gs, b"Vertices")
            vd = _child(gd, b"Vertices")
            if vs and vd:
                z_src = sum(vs.properties[0].value[2::3])
                z_dst = sum(vd.properties[0].value[2::3])
                # X and Y should be unchanged
                x_src = sum(vs.properties[0].value[0::3])
                x_dst = sum(vd.properties[0].value[0::3])
                assert abs(x_src - x_dst) < 0.001
                if abs(z_src) > 0.01:
                    assert abs(z_src + z_dst) < 0.001, "Z should be negated"
                    found_nonzero = True
        assert found_nonzero, "Expected at least one sub-mesh with non-zero Z"


class TestFlipWinding:
    """Unit tests for polygon winding flip."""

    def test_triangle_flip(self):
        # Single triangle: indices [0, 1, -3] (vertex 2 with end marker)
        result = _flip_winding([0, 1, -3])
        # Reversed: [2, 1, 0] → end marker on last: [2, 1, -1]
        assert result == [2, 1, -1]

    def test_quad_flip(self):
        # Quad: [0, 1, 2, -4]
        result = _flip_winding([0, 1, 2, -4])
        # Reversed: [3, 2, 1, 0] → [3, 2, 1, -1]
        assert result == [3, 2, 1, -1]

    def test_multiple_polygons(self):
        # Two triangles: [0,1,-3, 3,4,-6]
        result = _flip_winding([0, 1, -3, 3, 4, -6])
        # First reversed: [2,1,0] → [2,1,-1]
        # Second reversed: [5,4,3] → [5,4,-4]
        assert result == [2, 1, -1, 5, 4, -4]


class TestNegateZ:
    """Unit tests for Z negation helper."""

    def test_negates_every_third(self):
        result = _negate_z([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        assert result == [1.0, 2.0, -3.0, 4.0, 5.0, -6.0]

    def test_preserves_length(self):
        vals = [float(i) for i in range(30)]
        assert len(_negate_z(vals)) == 30
