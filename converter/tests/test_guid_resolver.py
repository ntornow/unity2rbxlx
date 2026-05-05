"""Tests for guid_resolver.build_guid_index — silent-corruption edge cases.

The GUID index is built once early in the pipeline and consulted by every
subsequent phase. A regression that silently misses a duplicate or fails
to record an orphan would let later phases reach the wrong asset without
any warning surfacing in the conversion report.
"""
from __future__ import annotations

from pathlib import Path

from unity.guid_resolver import build_guid_index


def _write_meta(path: Path, guid: str, *, folder: bool = False) -> None:
    body = f"fileFormatVersion: 2\nguid: {guid}\n"
    if folder:
        body += "folderAsset: yes\n"
    path.write_text(body)


def _make_project(tmp_path: Path) -> Path:
    (tmp_path / "Assets").mkdir()
    return tmp_path


class TestBuildGuidIndexBasics:
    def test_single_asset_indexed(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        (proj / "Assets" / "thing.png").write_bytes(b"\x89PNG")
        _write_meta(proj / "Assets" / "thing.png.meta", "a" * 32)

        idx = build_guid_index(proj)

        assert idx.total_resolved == 1
        assert idx.guid_to_entry["a" * 32].asset_path.name == "thing.png"
        assert not idx.duplicate_guids
        assert not idx.orphan_metas

    def test_folder_meta_marked_directory(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        (proj / "Assets" / "Models").mkdir()
        _write_meta(proj / "Assets" / "Models.meta", "b" * 32, folder=True)

        idx = build_guid_index(proj)

        entry = idx.guid_to_entry["b" * 32]
        assert entry.is_directory
        assert entry.kind == "directory"


class TestDuplicateGuidDetection:
    """Two assets sharing a GUID must be detected — silently picking one
    causes downstream references to point at the wrong asset."""

    def test_duplicate_recorded_in_duplicate_guids(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        guid = "c" * 32

        (proj / "Assets" / "a.png").write_bytes(b"\x89PNG")
        _write_meta(proj / "Assets" / "a.png.meta", guid)

        (proj / "Assets" / "b.png").write_bytes(b"\x89PNG")
        _write_meta(proj / "Assets" / "b.png.meta", guid)

        idx = build_guid_index(proj)

        assert guid in idx.duplicate_guids, "duplicate must be flagged"
        assert len(idx.duplicate_guids[guid]) == 2

    def test_first_wins_on_duplicate(self, tmp_path: Path) -> None:
        """Current contract: the first .meta file scanned wins, others go
        to duplicate_guids. Lock this in so a future change doesn't
        silently flip which asset gets resolved."""
        proj = _make_project(tmp_path)
        guid = "d" * 32

        (proj / "Assets" / "first.png").write_bytes(b"\x89PNG")
        _write_meta(proj / "Assets" / "first.png.meta", guid)
        (proj / "Assets" / "second.png").write_bytes(b"\x89PNG")
        _write_meta(proj / "Assets" / "second.png.meta", guid)

        idx = build_guid_index(proj)

        resolved = idx.resolve(guid)
        assert resolved is not None
        # one of the two is recorded as the canonical entry; the other in duplicates
        assert resolved.name in {"first.png", "second.png"}
        dup_names = {p.name for p in idx.duplicate_guids[guid]}
        assert {"first.png", "second.png"} == dup_names


class TestOrphanMetaHandling:
    """A .meta file without its accompanying asset is an orphan. Unity
    leaves these around when assets are deleted outside the editor; the
    pipeline must record them, not crash."""

    def test_orphan_meta_recorded(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        # .meta but no companion asset file
        _write_meta(proj / "Assets" / "ghost.png.meta", "e" * 32)

        idx = build_guid_index(proj)

        assert len(idx.orphan_metas) == 1
        assert idx.orphan_metas[0].name == "ghost.png.meta"

    def test_orphan_folder_meta_skipped_from_entries(self, tmp_path: Path) -> None:
        """Orphan folder metas (Unity legacy state) should be recorded as
        orphans but NOT added as resolvable entries."""
        proj = _make_project(tmp_path)
        _write_meta(proj / "Assets" / "DeletedFolder.meta", "f" * 32, folder=True)

        idx = build_guid_index(proj)

        assert idx.orphan_metas
        assert "f" * 32 not in idx.guid_to_entry

    def test_orphan_non_folder_still_indexed(self, tmp_path: Path) -> None:
        """Non-folder orphans are recorded AND indexed — references in
        scenes can still resolve to the orphan path even though the
        asset file is missing on disk."""
        proj = _make_project(tmp_path)
        _write_meta(proj / "Assets" / "missing.png.meta", "0" * 32)

        idx = build_guid_index(proj)

        assert idx.orphan_metas
        assert "0" * 32 in idx.guid_to_entry


class TestParseErrorHandling:
    def test_malformed_meta_recorded_as_parse_error(self, tmp_path: Path) -> None:
        proj = _make_project(tmp_path)
        # No guid: line at all
        (proj / "Assets" / "broken.png.meta").write_text("fileFormatVersion: 2\n")
        (proj / "Assets" / "broken.png").write_bytes(b"\x89PNG")

        idx = build_guid_index(proj)

        assert idx.parse_errors, "malformed meta must surface a parse error"
        assert idx.total_resolved == 0
