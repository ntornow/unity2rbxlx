"""Tests for mesh_processor.decimate_mesh, get_mesh_info, needs_decimation.

A mesh whose face count exceeds the Roblox cap (currently 20,000 faces per
mesh) silently fails upload. Decimation is the only path to ship those
meshes; a regression that produces an oversized output, a degenerate
mesh, or a mesh with inverted normals corrupts the converted scene.

Tests synthesize meshes via trimesh primitives so they don't depend on
any binary fixture file.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

import config
from converter.mesh_processor import (
    decimate_mesh,
    get_mesh_info,
    needs_decimation,
)

trimesh = pytest.importorskip("trimesh")


class TestRobloxFaceCap:
    """Bug 1: the per-mesh face cap and the quality-floor clamp.

    The floor (``MESH_QUALITY_FLOOR``) can raise the decimation target;
    without a clamp it can push the target above what Roblox accepts.
    """

    def test_cap_constant_is_roblox_limit(self) -> None:
        # AC 1: the documented Roblox per-mesh triangle cap is 20,000.
        assert config.MESH_ROBLOX_MAX_FACES == 20_000
        # Sibling constants are unchanged (locked decision).
        assert config.MESH_TARGET_FACES == 8_000
        assert config.MESH_QUALITY_FLOOR == 0.6

    def test_floor_above_cap_clamps_to_cap(self) -> None:
        # AC 2: when original*FLOOR exceeds the cap, the clamp pins to the cap.
        # Mirrors the production expression min(max(target, floor), cap).
        original_faces = 200_000  # 200k * 0.6 = 120k floored target, well over cap
        target_faces = config.MESH_TARGET_FACES
        floored = int(original_faces * config.MESH_QUALITY_FLOOR)
        effective = min(max(target_faces, floored), config.MESH_ROBLOX_MAX_FACES)
        assert floored > config.MESH_ROBLOX_MAX_FACES  # precondition: floor would overshoot
        assert effective == config.MESH_ROBLOX_MAX_FACES

    def test_floor_below_cap_leaves_floored_value(self) -> None:
        # AC 3: ordinary case — floored target is under the cap, clamp is inert.
        original_faces = 20_000  # 20k * 0.6 = 12k floored target, under the cap
        target_faces = config.MESH_TARGET_FACES  # 8_000 < floored
        floored = int(original_faces * config.MESH_QUALITY_FLOOR)
        effective = min(max(target_faces, floored), config.MESH_ROBLOX_MAX_FACES)
        assert floored < config.MESH_ROBLOX_MAX_FACES  # precondition
        assert effective == floored == 12_000

    def test_clamp_is_wired_into_production_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # AC 2 (regression guard, backend-INDEPENDENT): the clamp must actually
        # be applied to the value decimate_mesh hands the backend, not just be a
        # truth recomputed in the test. Spy on simplify_quadric_decimation to
        # capture the target it receives -- no quadric backend needed (the spy
        # raises to take the export-original fallback). Pre-fix code (max only,
        # no min-cap) would pass the floored target (overshoots the cap) and
        # FAIL this assertion; post-fix passes the cap.
        import converter.mesh_processor as mp

        sphere = _high_poly_sphere(2000)
        original_faces = len(sphere.faces)
        src = tmp_path / "clamp_wired.obj"
        sphere.export(str(src))

        cap = int(original_faces * 0.5)  # 50% < 60% floor -> floored overshoots cap
        floored = int(original_faces * mp.MESH_QUALITY_FLOOR)
        assert floored > cap  # precondition: floor would overshoot the cap
        monkeypatch.setattr(mp, "MESH_ROBLOX_MAX_FACES", cap)

        captured: dict[str, int] = {}

        def _spy(self: "trimesh.Trimesh", face_count: int) -> "trimesh.Trimesh":
            # Mirror the production keyword call simplify_quadric_decimation(
            # face_count=...). A positional-only spy would mask a regression to
            # the broken positional call (which binds to `percent` and raises).
            captured["target"] = face_count
            raise RuntimeError("backend disabled for spy")

        monkeypatch.setattr(trimesh.Trimesh, "simplify_quadric_decimation", _spy)

        # target tiny -> ratio below floor -> floor branch fires -> clamp to cap.
        decimate_mesh(src, target_faces=10)
        assert captured["target"] == cap  # clamped, not the floored overshoot


def _high_poly_sphere(face_count: int) -> "trimesh.Trimesh":
    """A sphere with at least `face_count` faces (uses icosphere subdivision)."""
    sub = 1
    sphere = trimesh.creation.icosphere(subdivisions=sub)
    while len(sphere.faces) < face_count:
        sub += 1
        sphere = trimesh.creation.icosphere(subdivisions=sub)
    return sphere


def _quadric_decimation_available() -> bool:
    """fast_simplification is the optional backend trimesh uses for
    quadric decimation. Without it, decimate_mesh falls back to copying
    the original. Tests that actually exercise decimation gate on this."""
    try:
        import fast_simplification  # noqa: F401
        return True
    except ImportError:
        return False


requires_decimator = pytest.mark.skipif(
    not _quadric_decimation_available(),
    reason="fast_simplification not installed; decimate_mesh falls back to copy",
)


class TestNeedsDecimation:
    def test_under_threshold_returns_false(self) -> None:
        assert needs_decimation(face_count=100, max_faces=10_000) is False

    def test_at_threshold_returns_false(self) -> None:
        assert needs_decimation(face_count=10_000, max_faces=10_000) is False

    def test_over_threshold_returns_true(self) -> None:
        assert needs_decimation(face_count=10_001, max_faces=10_000) is True


class TestGetMeshInfo:
    def test_returns_face_and_vertex_counts(self, tmp_path: Path) -> None:
        m = trimesh.creation.box(extents=(1.0, 2.0, 3.0))
        path = tmp_path / "box.obj"
        m.export(str(path))

        info = get_mesh_info(path)

        assert info["face_count"] == 12  # box has 12 triangles
        assert info["vertex_count"] > 0
        # Bounding box matches the extents (within float tolerance)
        bbox = info["bounding_box"]
        assert bbox[0] == pytest.approx(1.0, abs=1e-5)
        assert bbox[1] == pytest.approx(2.0, abs=1e-5)
        assert bbox[2] == pytest.approx(3.0, abs=1e-5)
        assert info["file_size"] > 0

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        info = get_mesh_info(tmp_path / "does_not_exist.obj")
        assert info == {}


class TestDecimateMeshFallbackPaths:
    """Exercises behavior that does NOT depend on the optional quadric
    backend: the small-mesh copy fast path and output path handling."""

    def test_decimation_below_target_copies_unchanged(self, tmp_path: Path) -> None:
        """When the mesh is already under the target, decimate_mesh just
        copies the file. Verifies no degradation on already-small meshes."""
        small = trimesh.creation.icosphere(subdivisions=1)  # 80 faces
        src = tmp_path / "small.obj"
        small.export(str(src))

        out = decimate_mesh(src, target_faces=10_000)
        result = trimesh.load(str(out), force="mesh")

        assert len(result.faces) == len(small.faces)

    def test_decimation_writes_to_explicit_output_path(self, tmp_path: Path) -> None:
        m = trimesh.creation.icosphere(subdivisions=2)
        src = tmp_path / "src.obj"
        dst = tmp_path / "dst.obj"
        m.export(str(src))

        out = decimate_mesh(src, target_faces=50, output_path=dst)
        assert out == dst
        assert dst.exists()

    def test_decimation_default_output_suffix(self, tmp_path: Path) -> None:
        m = trimesh.creation.icosphere(subdivisions=2)
        src = tmp_path / "model.obj"
        m.export(str(src))

        out = decimate_mesh(src, target_faces=50)
        assert out.stem == "model_decimated"
        assert out.parent == src.parent

    def test_decimation_returns_path_for_oversized_mesh_without_backend(
        self, tmp_path: Path,
    ) -> None:
        """Even when fast_simplification is unavailable, decimate_mesh
        produces an output file (falls back to exporting the original).
        The pipeline relies on always getting a path back, never None
        or an exception."""
        sphere = _high_poly_sphere(500)
        src = tmp_path / "oversized.obj"
        sphere.export(str(src))

        out = decimate_mesh(src, target_faces=100)
        assert out.exists()
        assert out.stat().st_size > 0


@requires_decimator
class TestDecimateMeshWithBackend:
    """Decimation behavior that requires the fast_simplification backend.
    Skipped when the backend is missing — better than testing fallback
    output and pretending it's decimation."""

    def test_decimation_reduces_face_count(self, tmp_path: Path) -> None:
        sphere = _high_poly_sphere(2000)
        original_faces = len(sphere.faces)
        src = tmp_path / "high_poly.obj"
        sphere.export(str(src))

        # Stay above the MESH_QUALITY_FLOOR (60% retention) to avoid clamping
        target = int(original_faces * 0.65)
        out = decimate_mesh(src, target_faces=target)

        decimated = trimesh.load(str(out), force="mesh")
        assert len(decimated.faces) <= target * 1.1
        assert len(decimated.faces) < original_faces

    def test_decimation_preserves_bounding_box(self, tmp_path: Path) -> None:
        """Bounding box dimensions survive within ~10%. Catches a regression
        where decimation collapses the mesh to a point."""
        sphere = _high_poly_sphere(2000)
        sphere.apply_scale([2.0, 3.0, 5.0])
        src = tmp_path / "scaled_sphere.obj"
        sphere.export(str(src))

        original_extents = sphere.extents
        target = int(len(sphere.faces) * 0.65)
        out = decimate_mesh(src, target_faces=target)
        decimated = trimesh.load(str(out), force="mesh")

        np.testing.assert_allclose(decimated.extents, original_extents, rtol=0.10)

    def test_floor_clamp_caps_output_through_real_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC 2 (end-to-end): when the quality floor would raise the target
        above the cap, decimate_mesh clamps the effective target to the cap so
        the output never exceeds what Roblox accepts. Patches the cap small so a
        modestly-sized mesh trips the floor>cap branch without a huge fixture."""
        import converter.mesh_processor as mp

        sphere = _high_poly_sphere(2000)
        original_faces = len(sphere.faces)
        src = tmp_path / "floor_clamp.obj"
        sphere.export(str(src))

        # Cap below the floored target so floor>cap and the clamp pins to cap.
        cap = int(original_faces * 0.5)  # 50% < 60% floor -> floored target overshoots cap
        monkeypatch.setattr(mp, "MESH_ROBLOX_MAX_FACES", cap)
        floored = int(original_faces * mp.MESH_QUALITY_FLOOR)
        assert floored > cap  # precondition: floor would overshoot the cap

        # target tiny -> ratio below floor -> floor branch fires -> clamp to cap.
        out = decimate_mesh(src, target_faces=10)
        decimated = trimesh.load(str(out), force="mesh")
        # The fast_simplification backend honors face_count exactly (it is a
        # reduction, never adds faces), so the clamped request bounds the output
        # strictly at the cap -- no overshoot slack. A positional-arg regression
        # (which raises and exports the oversized original) fails this.
        assert len(decimated.faces) <= cap
        assert len(decimated.faces) < floored

    def test_no_inverted_normals_post_decimate(self, tmp_path: Path) -> None:
        """A decimated convex shape's face normals should still all point
        outward. A regression that flips winding produces inside-out meshes
        that render invisibly in Roblox."""
        sphere = _high_poly_sphere(2000)
        src = tmp_path / "convex.obj"
        sphere.export(str(src))

        target = int(len(sphere.faces) * 0.65)
        out = decimate_mesh(src, target_faces=target)
        decimated = trimesh.load(str(out), force="mesh")

        centroid = decimated.vertices.mean(axis=0)
        face_centers = decimated.vertices[decimated.faces].mean(axis=1)
        outward_vectors = face_centers - centroid
        dots = np.einsum("ij,ij->i", decimated.face_normals, outward_vectors)
        assert np.mean(dots > 0) > 0.95, (
            f"only {np.mean(dots > 0)*100:.1f}% of faces point outward; "
            "decimation may have inverted winding"
        )
