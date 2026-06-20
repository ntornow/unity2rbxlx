"""Tests for the embedded-mesh quarantine in resolve_assets (Bug 2).

A synthetic embedded-mesh key (``<path>.prefab|.asset#<file_id>``) is produced
by ``unity.embedded_mesh_extractor.synthesize_fbx`` and MUST resolve to exactly
one sub-mesh. When the FBX template-cleanup leaks extra Geometry nodes the key
resolves to !=1 sub-mesh, and ``sub_meshes[0]`` would bind to non-deterministic
geometry. ``_quarantine_bad_embedded_meshes`` evicts such keys from every table
the MeshId binding reads (mesh_hierarchies, mesh_native_sizes, uploaded_assets —
all slash variants) and records them in asset_upload_errors, so the node falls
through to the crash-free no-MeshId / face-decal fallback.

These tests drive the pure helper directly (no Open Cloud round-trip), plus one
downstream assertion that ``scene_converter._resolve_mesh_id`` returns None
crash-free for a quarantined key.
"""
from __future__ import annotations

from pathlib import Path

from converter.pipeline import _quarantine_bad_embedded_meshes


def _sub(name: str) -> dict[str, str]:
    """A minimal mesh-hierarchy sub-mesh entry (MeshHierarchyEntry shape)."""
    return {"name": name, "meshId": f"rbxassetid://{name}"}


class TestQuarantineHelper:
    def test_two_sub_mesh_embedded_key_dropped_from_all_tables(self) -> None:
        # AC 4: an embedded key resolving to 2 sub-meshes is removed from
        # mesh_hierarchies, mesh_native_sizes, AND uploaded_assets, and appended
        # to asset_upload_errors.
        key = "Assets/Weapons/Rifle.prefab#11400000"
        hierarchies = {key: [_sub("a"), _sub("b")]}
        sizes = {key: [1.0, 2.0, 3.0]}
        uploaded = {key: "rbxassetid://999"}
        errors: list[str] = []

        bad = _quarantine_bad_embedded_meshes(hierarchies, sizes, uploaded, errors)

        assert bad == [key]
        assert key not in hierarchies
        assert key not in sizes
        assert key not in uploaded
        assert key in errors

    def test_zero_sub_mesh_embedded_key_dropped(self) -> None:
        # AC 4 (edge, len==0): a 0-sub-mesh embedded entry is also != 1 -> dropped.
        key = "Assets/Foo.asset#4300000"
        hierarchies: dict[str, list[dict[str, str]]] = {key: []}
        sizes = {key: [1.0, 1.0, 1.0]}
        uploaded = {key: "rbxassetid://1"}
        errors: list[str] = []

        bad = _quarantine_bad_embedded_meshes(hierarchies, sizes, uploaded, errors)

        assert bad == [key]
        assert key not in hierarchies
        assert key not in uploaded
        assert key in errors

    def test_slash_variant_evicted_from_uploaded_assets(self) -> None:
        # AC 5: hierarchies keyed with forward slashes, uploaded_assets keyed
        # with the backslash (Windows-cached) variant -> still evicted.
        fwd_key = "Assets/Weapons/Rifle.prefab#11400000"
        back_key = "Assets\\Weapons\\Rifle.prefab#11400000"
        hierarchies = {fwd_key: [_sub("a"), _sub("b")]}
        sizes = {fwd_key: [1.0, 2.0, 3.0]}
        uploaded = {back_key: "rbxassetid://999"}
        errors: list[str] = []

        bad = _quarantine_bad_embedded_meshes(hierarchies, sizes, uploaded, errors)

        assert bad == [fwd_key]
        assert back_key not in uploaded
        assert fwd_key in errors

    def test_preseeded_ctx_key_evicted_post_merge(self) -> None:
        # AC 6: a bad key already present in the (merged) ctx dicts from a prior
        # force-rerun is discovered and evicted even though this run's fresh
        # parse did not re-supply it. The helper operates on the merged dicts, so
        # a dict simulating {**existing_bad, **fresh_good} is what it sees.
        bad_key = "Assets/Old.prefab#11400000"      # pre-seeded, leaked geometry
        good_key = "Assets/New.prefab#11400002"      # this run's healthy parse
        hierarchies = {
            bad_key: [_sub("a"), _sub("b")],
            good_key: [_sub("c")],
        }
        sizes = {bad_key: [1.0, 1.0, 1.0], good_key: [2.0, 2.0, 2.0]}
        uploaded = {bad_key: "rbxassetid://1", good_key: "rbxassetid://2"}
        errors: list[str] = []

        bad = _quarantine_bad_embedded_meshes(hierarchies, sizes, uploaded, errors)

        assert bad == [bad_key]
        assert bad_key not in hierarchies and bad_key not in uploaded
        # The healthy key survives untouched.
        assert good_key in hierarchies and good_key in uploaded
        assert errors == [bad_key]

    def test_non_embedded_multi_sub_mesh_key_not_quarantined(self) -> None:
        # AC 7 (false-positive guard): a real multi-mesh FBX (no '#' -> not an
        # embedded key) with >=2 sub-meshes is NOT quarantined.
        fbx_key = "Assets/Models/Character.fbx"
        hierarchies = {fbx_key: [_sub("body"), _sub("head"), _sub("arm")]}
        sizes = {fbx_key: [4.0, 4.0, 4.0]}
        uploaded = {fbx_key: "rbxassetid://555"}
        errors: list[str] = []

        bad = _quarantine_bad_embedded_meshes(hierarchies, sizes, uploaded, errors)

        assert bad == []
        assert fbx_key in hierarchies
        assert fbx_key in sizes
        assert fbx_key in uploaded
        assert errors == []

    def test_healthy_single_sub_mesh_is_noop(self) -> None:
        # AC 8: a healthy embedded key (exactly 1 sub-mesh) and a non-embedded
        # key are both kept; no append, helper returns []. Pure no-op.
        emb_key = "Assets/Good.prefab#11400000"
        fbx_key = "Assets/Models/Thing.fbx"
        hierarchies = {emb_key: [_sub("only")], fbx_key: [_sub("solo")]}
        sizes = {emb_key: [1.0, 1.0, 1.0], fbx_key: [2.0, 2.0, 2.0]}
        uploaded = {emb_key: "rbxassetid://1", fbx_key: "rbxassetid://2"}
        errors: list[str] = []

        bad = _quarantine_bad_embedded_meshes(hierarchies, sizes, uploaded, errors)

        assert bad == []
        assert hierarchies == {emb_key: [_sub("only")], fbx_key: [_sub("solo")]}
        assert emb_key in uploaded and fbx_key in uploaded
        assert errors == []

    def test_already_in_errors_not_duplicated(self) -> None:
        # AC 8 (dedup edge): a bad key already in asset_upload_errors is not
        # appended twice.
        key = "Assets/Foo.prefab#11400000"
        hierarchies = {key: [_sub("a"), _sub("b")]}
        sizes = {key: [1.0, 1.0, 1.0]}
        uploaded = {key: "rbxassetid://1"}
        errors = [key]

        bad = _quarantine_bad_embedded_meshes(hierarchies, sizes, uploaded, errors)

        assert bad == [key]
        assert errors == [key]  # not duplicated


class TestDownstreamCrashFree:
    def test_resolve_mesh_id_returns_none_after_quarantine(self) -> None:
        # AC 9: after quarantining a key, scene_converter._resolve_mesh_id for
        # that guid/file_id returns None WITHOUT raising — the node falls through
        # to the face-decal / no-MeshId fallback.
        import converter.scene_converter as sc
        from core.unity_types import GuidEntry, GuidIndex

        guid = "abc123"
        rel = Path("Assets/Weapons/Rifle.prefab")
        abs_path = Path("/proj/Assets/Weapons/Rifle.prefab")
        file_id = "11400000"
        key = f"{rel.as_posix()}#{file_id}"  # "Assets/Weapons/Rifle.prefab#11400000"

        guid_index = GuidIndex(project_root=Path("/proj"))
        guid_index.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=abs_path,
            relative_path=rel,
            kind="prefab",
        )

        # Pre-quarantine state had the key in both tables; run the quarantine
        # (2 sub-meshes -> bad), then assert the post-quarantine end-state.
        hierarchies = {key: [_sub("a"), _sub("b")]}
        sizes = {key: [1.0, 1.0, 1.0]}
        uploaded = {key: "rbxassetid://999"}
        errors: list[str] = []
        _quarantine_bad_embedded_meshes(hierarchies, sizes, uploaded, errors)

        # Drive the real resolver with a _current_ctx whose mesh_hierarchies
        # lacks the key (post-quarantine).
        prev_ctx = sc._current_ctx
        sc._current_ctx = sc.SceneConversionContext(mesh_hierarchies=hierarchies)
        try:
            result = sc._resolve_mesh_id(
                guid, guid_index, uploaded, mesh_file_id=file_id,
            )
        finally:
            sc._current_ctx = prev_ctx

        assert result is None
