"""Tests for fbx_binary.read_fbx / write_fbx / mirror_fbx_handedness.

Mesh corruption from a regression here is silent — the converted .rbxlx
contains valid MeshIds but the geometry comes back wrong (mirror image,
inside-out faces, missing normals). Roblox renders the result without
warning. The only catch is visual diff during conversion review.

Tests run against a real FBX in the SimpleFPS test project. If the
fixture is missing (e.g. SimpleFPS submodule not initialized), the
tests skip with a clear message rather than failing.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from converter.fbx_binary import (
    FbxNode,
    FbxProperty,
    _find_geometry_nodes,
    _negate_axis,
    mirror_fbx_handedness,
    read_fbx,
    write_fbx,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FBX_FIXTURE = REPO_ROOT / "test_projects" / "SimpleFPS" / "Assets" / "Models" / "prop_keycard.fbx"


def _fbx_fixture_unavailable() -> bool:
    # Nightly CI inits the SimpleFPS submodule with GIT_LFS_SKIP_SMUDGE=1
    # because the upstream LFS quota is exhausted, so the .fbx path exists
    # but holds an LFS pointer (~130 bytes of ASCII) instead of the real
    # binary. read_fbx() then fails with "Not an FBX binary file". Treat
    # both missing-file and LFS-pointer cases as unavailable.
    if not FBX_FIXTURE.exists():
        return True
    try:
        head = FBX_FIXTURE.read_bytes()[:64]
    except OSError:
        return True
    return head.startswith(b"version https://git-lfs.github.com/")


requires_fbx = pytest.mark.skipif(
    _fbx_fixture_unavailable(),
    reason=f"FBX fixture missing or LFS pointer: {FBX_FIXTURE}",
)


def _vertex_arrays(roots) -> list[list[float]]:
    """Extract every Vertices array from the FBX node tree."""
    out = []
    for geom in _find_geometry_nodes(roots):
        for child in geom.children:
            if child.name == b"Vertices" and child.properties:
                if child.properties[0].type_code == "d":
                    out.append(child.properties[0].value)
    return out


class TestNegateAxis:
    """The axis negation primitive is the foundation of mirror_fbx_handedness;
    a bug here propagates into every converted mesh."""

    def test_negate_x_only(self) -> None:
        # 2 vertices: (1, 2, 3), (4, 5, 6) → flat (x,y,z, x,y,z)
        out = _negate_axis([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], axis=0)
        assert out == [-1.0, 2.0, 3.0, -4.0, 5.0, 6.0]

    def test_negate_y_preserves_x_and_z(self) -> None:
        out = _negate_axis([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], axis=1)
        assert out == [1.0, -2.0, 3.0, 4.0, -5.0, 6.0]

    def test_double_negate_is_identity(self) -> None:
        original = [1.0, -2.5, 3.0, 4.0, 5.0, -6.0]
        twice = _negate_axis(_negate_axis(original, 0), 0)
        assert twice == original


@requires_fbx
class TestRoundTripPreservesStructure:
    """read → write → read must produce structurally equivalent output.
    Byte-equality is too brittle for a binary format — use field-by-field."""

    def test_read_returns_version_and_roots(self) -> None:
        ver, roots, footer = read_fbx(FBX_FIXTURE)
        assert ver > 0
        assert len(roots) > 0
        assert len(footer) > 0
        assert _find_geometry_nodes(roots), "fixture must contain a Geometry node"

    def test_write_roundtrip_preserves_vertex_count(self, tmp_path: Path) -> None:
        ver, roots, footer = read_fbx(FBX_FIXTURE)
        original_vert_arrays = _vertex_arrays(roots)
        assert original_vert_arrays, "fixture must have a Vertices array"

        out = tmp_path / "roundtrip.fbx"
        write_fbx(out, ver, roots, footer=footer)

        ver2, roots2, _ = read_fbx(out)
        assert ver2 == ver
        new_vert_arrays = _vertex_arrays(roots2)
        assert len(new_vert_arrays) == len(original_vert_arrays)
        for orig, new in zip(original_vert_arrays, new_vert_arrays):
            assert len(new) == len(orig)


@requires_fbx
class TestMirrorHandedness:
    """The fix for the asymmetric-doors bug: negate FBX X and Y so Roblox
    renders meshes the same orientation as Unity. This is a 180° rotation
    around the vertical, NOT a single-axis mirror — winding must NOT flip."""

    def test_mirror_writes_output(self, tmp_path: Path) -> None:
        out = tmp_path / "mirrored.fbx"
        ok = mirror_fbx_handedness(FBX_FIXTURE, out)
        assert ok
        assert out.exists()
        assert out.stat().st_size > 0

    def test_mirror_negates_x_and_y_preserves_z(self, tmp_path: Path) -> None:
        """The most important check: every vertex's X and Y are negated;
        Z is untouched. Verifying via numpy across the entire vertex array
        catches partial-array bugs (e.g. only first chunk negated)."""
        ver, original_roots, footer = read_fbx(FBX_FIXTURE)
        original_verts = _vertex_arrays(original_roots)
        assert original_verts

        out = tmp_path / "mirrored.fbx"
        assert mirror_fbx_handedness(FBX_FIXTURE, out)

        _, mirrored_roots, _ = read_fbx(out)
        mirrored_verts = _vertex_arrays(mirrored_roots)
        assert len(mirrored_verts) == len(original_verts)

        for orig_flat, mir_flat in zip(original_verts, mirrored_verts):
            orig = np.array(orig_flat, dtype=np.float64).reshape(-1, 3)
            mir = np.array(mir_flat, dtype=np.float64).reshape(-1, 3)
            np.testing.assert_allclose(mir[:, 0], -orig[:, 0], atol=1e-9)
            np.testing.assert_allclose(mir[:, 1], -orig[:, 1], atol=1e-9)
            np.testing.assert_allclose(mir[:, 2], orig[:, 2], atol=1e-9)

    def test_mirror_preserves_winding(self, tmp_path: Path) -> None:
        """Two-axis negation is det = +1 (rotation), so PolygonVertexIndex
        arrays MUST be unchanged. A regression that adds a winding flip
        here makes faces render inside-out."""
        ver, original_roots, _ = read_fbx(FBX_FIXTURE)
        # Capture original PolygonVertexIndex arrays
        orig_indices = []
        for geom in _find_geometry_nodes(original_roots):
            for child in geom.children:
                if child.name == b"PolygonVertexIndex" and child.properties:
                    orig_indices.append(list(child.properties[0].value))

        out = tmp_path / "mirrored.fbx"
        assert mirror_fbx_handedness(FBX_FIXTURE, out)

        _, mirrored_roots, _ = read_fbx(out)
        mir_indices = []
        for geom in _find_geometry_nodes(mirrored_roots):
            for child in geom.children:
                if child.name == b"PolygonVertexIndex" and child.properties:
                    mir_indices.append(list(child.properties[0].value))

        assert mir_indices == orig_indices, "winding must be preserved by 2-axis negation"

    def test_mirror_idempotent_when_run_twice(self, tmp_path: Path) -> None:
        """Mirror twice → original. (-1)*(-1) = 1 on each axis."""
        once = tmp_path / "once.fbx"
        twice = tmp_path / "twice.fbx"
        assert mirror_fbx_handedness(FBX_FIXTURE, once)
        assert mirror_fbx_handedness(once, twice)

        _, original_roots, _ = read_fbx(FBX_FIXTURE)
        _, twice_roots, _ = read_fbx(twice)

        for orig, after in zip(_vertex_arrays(original_roots), _vertex_arrays(twice_roots)):
            np.testing.assert_allclose(after, orig, atol=1e-9)


class TestFbx7500SixtyFourBit:
    """FBX 7500+ (FBX 2016+) uses 64-bit offsets. read_fbx used to reject these
    outright with NotImplementedError, so every modern Unity FBX silently
    degraded to face-decal rendering (no handedness mirror, no bbox). The
    early gate is gone; these guard the 64-bit read/write path with no external
    fixture so they run even when the SimpleFPS submodule is absent."""

    def _build_geometry_tree(self) -> FbxNode:
        verts = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        geom = FbxNode(
            name=b"Geometry",
            properties=[FbxProperty("L", 1234)],
            children=[FbxNode(name=b"Vertices", properties=[FbxProperty("d", verts)])],
        )
        return FbxNode(name=b"Objects", children=[geom])

    def test_read_does_not_reject_7500(self, tmp_path: Path) -> None:
        out = tmp_path / "v7500.fbx"
        write_fbx(out, 7500, [self._build_geometry_tree()])
        ver, _roots, _footer = read_fbx(out)  # no NotImplementedError
        assert ver == 7500

    def test_roundtrip_preserves_structure_and_vertices(self, tmp_path: Path) -> None:
        out = tmp_path / "v7500.fbx"
        write_fbx(out, 7500, [self._build_geometry_tree()])

        _ver, roots, _footer = read_fbx(out)
        geoms = _find_geometry_nodes(roots)
        assert len(geoms) == 1
        vchild = next(c for c in geoms[0].children if c.name == b"Vertices")
        assert list(vchild.properties[0].value) == [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

    def test_mirror_handedness_degrades_on_truncated_7500(self, tmp_path: Path) -> None:
        """A malformed/truncated 7500 file must degrade to False (raw upload),
        not crash the upload phase. With the early reject gate gone, the 64-bit
        header unpack runs off the end of the buffer and raises struct.error;
        mirror_fbx_handedness must absorb it like any unsupported file."""
        full = tmp_path / "full7500.fbx"
        write_fbx(full, 7500, [self._build_geometry_tree()])
        # Lop off most of the body so the 64-bit node header unpack overruns.
        raw = full.read_bytes()
        truncated = tmp_path / "trunc7500.fbx"
        truncated.write_bytes(raw[:30])  # header + version + a few bytes only
        dst = tmp_path / "out.fbx"
        assert mirror_fbx_handedness(truncated, dst) is False
        assert not dst.exists()

    def test_mirror_handedness_roundtrips_7500(self, tmp_path: Path) -> None:
        src = tmp_path / "src7500.fbx"
        dst = tmp_path / "dst7500.fbx"
        write_fbx(src, 7500, [self._build_geometry_tree()])

        assert mirror_fbx_handedness(src, dst)
        ver, roots, _footer = read_fbx(dst)
        assert ver == 7500
        # X and Y negated, Z preserved (the 2-axis handedness flip)
        vchild = next(
            c for c in _find_geometry_nodes(roots)[0].children if c.name == b"Vertices"
        )
        assert list(vchild.properties[0].value) == [
            0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0, 0.0,
        ]
