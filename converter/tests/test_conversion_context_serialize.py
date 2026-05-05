"""Tests for ConversionContext.save / load roundtrip.

Pause/resume relies on save() producing a JSON file that load() reads back
into an equivalent ConversionContext. A regression in either direction
silently corrupts resumed conversions — the user re-runs `u2r.py convert
--phase X` and gets stale state.

The asymmetric round-trip (write → load fresh → write → byte-equal)
catches divergences load() might silently fix and save() would re-emit
differently.
"""
from __future__ import annotations

from pathlib import Path

import json

import pytest

from core.conversion_context import ConversionContext

# `_SENSITIVE_FIELDS` is annotated as a tuple at module load but `dataclass`
# treats it as a regular field because there's no `ClassVar` marker. After a
# JSON roundtrip the tuple becomes a list. This is a known wart, not a
# correctness regression — exclude it from field-by-field comparison.
_NON_DATA_FIELDS = {"_SENSITIVE_FIELDS"}


def _populated_ctx() -> ConversionContext:
    """Build a ConversionContext touching every field that survives JSON."""
    return ConversionContext(
        unity_project_path="/tmp/SomeProject",
        scene_paths=["Assets/Main.unity", "Assets/Menu.unity"],
        selected_scene="Assets/Main.unity",
        completed_phases=["parse", "extract_assets", "transpile_scripts"],
        current_phase="convert_scene",
        uploaded_assets={"Assets/foo.png": "rbxassetid://1234"},
        asset_upload_errors=["Assets/bad.psd: rejected"],
        universe_id=42,
        place_id=99,
        experience_name="My Game",
        total_game_objects=100,
        converted_parts=87,
        total_scripts=12,
        transpiled_scripts=11,
        total_materials=20,
        converted_materials=18,
        total_animations=5,
        converted_animations=5,
        warnings=["[parse] empty scene root"],
        errors=[],
        mesh_native_sizes={"Assets/m.fbx": [1.0, 2.0, 3.0]},
        mesh_texture_ids={"Assets/m.fbx": "rbxassetid://5678"},
        mesh_hierarchies={
            "Assets/m.fbx": [
                {
                    "name": "SubMesh0",
                    "meshId": "rbxassetid://9999",
                    "size": [1.0, 1.0, 1.0],
                    "position": [0.0, 0.0, 0.0],
                    "textureId": "rbxassetid://5678",
                },
            ],
        },
        fbx_bounding_boxes={"Assets/m.fbx": [10.0, 20.0, 30.0]},
        scenes_metadata={
            "Assets/Main.unity": {"parts": 87, "scripts": 11, "game_objects": 100},
        },
    )


class TestRoundTrip:
    def test_save_load_preserves_all_fields(self, tmp_path: Path) -> None:
        original = _populated_ctx()
        ctx_path = tmp_path / "ctx.json"

        original.save(ctx_path)
        loaded = ConversionContext.load(ctx_path)

        # Every data field survives the round trip.
        for fname in original.__dataclass_fields__:
            if fname in _NON_DATA_FIELDS:
                continue
            assert getattr(loaded, fname) == getattr(original, fname), (
                f"field {fname!r} mismatch after load: "
                f"{getattr(original, fname)!r} -> {getattr(loaded, fname)!r}"
            )

    def test_asymmetric_roundtrip_byte_equal(self, tmp_path: Path) -> None:
        """save → load fresh → save again → byte-equal. Catches divergences
        where load() silently fixes up data that save() re-emits differently."""
        original = _populated_ctx()
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"

        original.save(first)
        reloaded = ConversionContext.load(first)
        reloaded.save(second)

        assert first.read_bytes() == second.read_bytes()

    def test_empty_context_roundtrips(self, tmp_path: Path) -> None:
        """Default-constructed context with all defaults must roundtrip."""
        original = ConversionContext()
        ctx_path = tmp_path / "empty.json"
        original.save(ctx_path)
        ConversionContext.load(ctx_path)  # must not raise

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        ctx = ConversionContext()
        nested = tmp_path / "deep" / "nested" / "ctx.json"
        ctx.save(nested)
        assert nested.exists()


class TestSanitizedSave:
    """save_sanitized strips creator-identifying fields. Verified at the
    JSON level — the file is intended for sharing in bug reports, not
    necessarily for re-loading via ConversionContext.load()."""

    def test_sensitive_fields_blanked_at_json_level(self, tmp_path: Path) -> None:
        original = _populated_ctx()
        out = tmp_path / "redacted.json"
        original.save_sanitized(out)
        data = json.loads(out.read_text())

        assert data["universe_id"] is None
        assert data["place_id"] is None
        # save_sanitized blanks scalars to None regardless of original type
        assert data["experience_name"] is None
        assert data["uploaded_assets"] == {}
        assert data["mesh_native_sizes"] == {}
        assert data["mesh_hierarchies"] == {}
        assert data.get("_sanitized") is True

    def test_non_sensitive_fields_preserved_in_json(self, tmp_path: Path) -> None:
        """Sanitization must NOT strip fields useful for debugging."""
        original = _populated_ctx()
        out = tmp_path / "redacted.json"
        original.save_sanitized(out)
        data = json.loads(out.read_text())

        assert data["unity_project_path"] == original.unity_project_path
        assert data["completed_phases"] == original.completed_phases
        assert data["warnings"] == original.warnings
        assert data["scenes_metadata"] == original.scenes_metadata

    @pytest.mark.xfail(
        reason="save_sanitized adds a `_sanitized` marker that load() rejects "
        "with TypeError. Fix is a one-line change in load() to drop unknown keys, "
        "but it's out of scope for the test-safety-net PR. See PR thread.",
        strict=True,
    )
    def test_sanitized_file_can_be_loaded(self, tmp_path: Path) -> None:
        """KNOWN BROKEN: load() can't handle files written by save_sanitized.
        Pinned via xfail so when the production fix lands, this test starts
        passing and pytest forces the xfail marker to be removed."""
        original = _populated_ctx()
        out = tmp_path / "redacted.json"
        original.save_sanitized(out)
        ConversionContext.load(out)  # currently raises TypeError
