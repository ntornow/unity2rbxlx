"""Tests for vertex_color_baker.py — Phase 5.7 sub-mesh identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.vertex_color_baker import (
    BakeResult,
    VertexColorBakeResult,
    _unity_file_id_to_submesh_index,
    bake_vertex_colors_batch,
)


class TestUnityFileIdToSubmeshIndex:
    """Phase 5.7: Unity FBX sub-mesh fileIDs map to assimp mesh indices.

    Convention: 4300000 → 0, 4300002 → 1, 4300004 → 2, ...
    """

    def test_first_submesh(self):
        assert _unity_file_id_to_submesh_index("4300000") == 0

    def test_second_submesh(self):
        assert _unity_file_id_to_submesh_index("4300002") == 1

    def test_third_submesh(self):
        assert _unity_file_id_to_submesh_index("4300004") == 2

    def test_eleventh_submesh(self):
        assert _unity_file_id_to_submesh_index("4300020") == 10

    def test_none_returns_none(self):
        assert _unity_file_id_to_submesh_index(None) is None

    def test_empty_string_returns_none(self):
        assert _unity_file_id_to_submesh_index("") is None

    def test_garbage_string_returns_none(self):
        assert _unity_file_id_to_submesh_index("not-an-int") is None

    def test_below_base_returns_none(self):
        # File IDs below the FBX base (e.g., scene-level fileIDs) map to None.
        assert _unity_file_id_to_submesh_index("100000") is None

    def test_zero_returns_none(self):
        assert _unity_file_id_to_submesh_index("0") is None


class TestBakeBatchSubmeshKeyedOutput:
    """Phase 5.7 acceptance: an FBX with three sub-meshes and three distinct
    vertex-color sets bakes to three distinct textures keyed by mesh_file_id.
    """

    def test_distinct_filenames_per_submesh(self, tmp_path: Path, monkeypatch):
        """Three (mesh, albedo, file_id) triples produce three distinct
        output PNG paths, each keyed by file_id.
        """
        from converter import vertex_color_baker as vcb

        mesh = tmp_path / "Vehicle.fbx"
        mesh.write_bytes(b"stub")
        albedo = tmp_path / "albedo.png"
        albedo.write_bytes(b"\x89PNG")
        out_dir = tmp_path / "vc"

        # Stub the per-mesh baker so we don't need a real FBX. Echo back the
        # output_path and mesh_file_id so the test can assert on them.
        called: list[dict] = []

        def _fake_into_albedo(
            mesh_path, albedo_path, output_path, resolution=None,
            mesh_file_id=None,
        ):
            called.append({
                "mesh": mesh_path,
                "albedo": albedo_path,
                "output": output_path,
                "mesh_file_id": mesh_file_id,
            })
            return BakeResult(
                mesh_path=mesh_path,
                output_path=output_path,
                baked=True,
                has_vertex_colors=True,
            )

        monkeypatch.setattr(vcb, "bake_vertex_colors_into_albedo", _fake_into_albedo)

        triples: list[tuple[Path, Path, str | None]] = [
            (mesh, albedo, "4300000"),
            (mesh, albedo, "4300002"),
            (mesh, albedo, "4300004"),
        ]
        result = bake_vertex_colors_batch(triples, out_dir)

        assert result.total == 3
        assert result.baked == 3
        # Three distinct output filenames, all keyed by file_id.
        out_names = sorted(call["output"].name for call in called)
        assert out_names == [
            "Vehicle_4300000_vc_baked.png",
            "Vehicle_4300002_vc_baked.png",
            "Vehicle_4300004_vc_baked.png",
        ]
        # The mesh_file_id propagates to the per-mesh baker.
        assert sorted(call["mesh_file_id"] for call in called) == [
            "4300000", "4300002", "4300004",
        ]

    def test_legacy_2tuple_input_still_works(self, tmp_path: Path, monkeypatch):
        """Backward compat: 2-tuples (mesh, albedo) without file_id keep
        producing the legacy filename so callers that don't track sub-meshes
        don't have to change.
        """
        from converter import vertex_color_baker as vcb

        mesh = tmp_path / "Prop.fbx"
        mesh.write_bytes(b"stub")
        albedo = tmp_path / "albedo.png"
        albedo.write_bytes(b"\x89PNG")
        out_dir = tmp_path / "vc"

        called: list[dict] = []

        def _fake_into_albedo(
            mesh_path, albedo_path, output_path, resolution=None,
            mesh_file_id=None,
        ):
            called.append({
                "output": output_path,
                "mesh_file_id": mesh_file_id,
            })
            return BakeResult(
                mesh_path=mesh_path,
                output_path=output_path,
                baked=True,
                has_vertex_colors=True,
            )

        monkeypatch.setattr(vcb, "bake_vertex_colors_into_albedo", _fake_into_albedo)
        result = bake_vertex_colors_batch([(mesh, albedo)], out_dir)

        assert result.total == 1
        assert called[0]["output"].name == "Prop_vc_baked.png"
        assert called[0]["mesh_file_id"] is None

    def test_none_file_id_falls_back_to_unkeyed_filename(
        self, tmp_path: Path, monkeypatch,
    ):
        """A 3-tuple with None as file_id keys outputs the same as a 2-tuple."""
        from converter import vertex_color_baker as vcb

        mesh = tmp_path / "Prop.fbx"
        mesh.write_bytes(b"stub")
        albedo = tmp_path / "albedo.png"
        albedo.write_bytes(b"\x89PNG")
        out_dir = tmp_path / "vc"

        called: list[dict] = []

        def _fake_into_albedo(
            mesh_path, albedo_path, output_path, resolution=None,
            mesh_file_id=None,
        ):
            called.append({
                "output": output_path,
                "mesh_file_id": mesh_file_id,
            })
            return BakeResult(
                mesh_path=mesh_path,
                output_path=output_path,
                baked=True,
                has_vertex_colors=True,
            )

        monkeypatch.setattr(vcb, "bake_vertex_colors_into_albedo", _fake_into_albedo)
        result = bake_vertex_colors_batch([(mesh, albedo, None)], out_dir)

        assert result.total == 1
        assert called[0]["output"].name == "Prop_vc_baked.png"
        assert called[0]["mesh_file_id"] is None
