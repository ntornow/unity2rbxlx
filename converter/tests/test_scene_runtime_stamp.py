"""PR3b: ``.scene-runtime-mode`` stamp + mismatch guard tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.scene_runtime_stamp import (  # noqa: E402
    STAMP_BASENAME,
    SceneRuntimeModeMismatch,
    apply_clean_directive,
    guard_or_clean_output_dir,
    read_scene_runtime_stamp,
    write_scene_runtime_stamp,
)


class TestStampRoundTrip:
    def test_write_then_read_returns_value(self, tmp_path: Path) -> None:
        write_scene_runtime_stamp(tmp_path, "generic")
        assert read_scene_runtime_stamp(tmp_path) == "generic"

    def test_absent_stamp_defaults_to_legacy(self, tmp_path: Path) -> None:
        # Pre-PR3b output dirs predate the stamp; treat as legacy.
        assert read_scene_runtime_stamp(tmp_path) == "legacy"

    def test_malformed_stamp_falls_back_to_legacy(self, tmp_path: Path) -> None:
        (tmp_path / STAMP_BASENAME).write_text("not-a-mode", encoding="utf-8")
        assert read_scene_runtime_stamp(tmp_path) == "legacy"

    def test_write_creates_missing_parent(self, tmp_path: Path) -> None:
        new_dir = tmp_path / "fresh"
        assert not new_dir.exists()
        write_scene_runtime_stamp(new_dir, "auto")
        assert read_scene_runtime_stamp(new_dir) == "auto"


class TestGuardOrCleanOutputDir:
    def test_fresh_dir_writes_stamp(self, tmp_path: Path) -> None:
        # Caller treats this as the first run; the guard stamps the dir.
        target = tmp_path / "out"
        guard_or_clean_output_dir(target, "legacy")
        assert read_scene_runtime_stamp(target) == "legacy"

    def test_matching_stamp_no_op(self, tmp_path: Path) -> None:
        write_scene_runtime_stamp(tmp_path, "generic")
        # Idempotent: same mode is fine.
        guard_or_clean_output_dir(tmp_path, "generic")
        assert read_scene_runtime_stamp(tmp_path) == "generic"

    def test_mismatch_raises(self, tmp_path: Path) -> None:
        write_scene_runtime_stamp(tmp_path, "generic")
        with pytest.raises(SceneRuntimeModeMismatch) as excinfo:
            guard_or_clean_output_dir(tmp_path, "legacy")
        assert excinfo.value.stamped == "generic"
        assert excinfo.value.requested == "legacy"
        # The message points at --clean so the operator knows what to do.
        assert "--clean" in str(excinfo.value)

    def test_legacy_request_on_pre_pr3b_dir_upgrades_in_place(
        self, tmp_path: Path,
    ) -> None:
        # Pre-PR3b dir: directory exists but no stamp file. Operator
        # running the default --scene-runtime=legacy should succeed and
        # the stamp file should be written so future runs are guarded.
        (tmp_path / "scripts").mkdir()
        assert not (tmp_path / STAMP_BASENAME).exists()
        guard_or_clean_output_dir(tmp_path, "legacy")
        assert (tmp_path / STAMP_BASENAME).is_file()
        assert read_scene_runtime_stamp(tmp_path) == "legacy"

    def test_generic_request_on_pre_pr3b_dir_raises(
        self, tmp_path: Path,
    ) -> None:
        # Pre-PR3b dirs are legacy by definition; switching to generic
        # without --clean must refuse.
        (tmp_path / "scripts").mkdir()
        with pytest.raises(SceneRuntimeModeMismatch) as excinfo:
            guard_or_clean_output_dir(tmp_path, "generic")
        assert excinfo.value.stamped == "legacy"

    def test_clean_wipes_and_restamps(self, tmp_path: Path) -> None:
        # Seed with legacy + some children, then re-run with --clean +
        # generic. Children gone, new stamp present.
        write_scene_runtime_stamp(tmp_path, "legacy")
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "Foo.luau").write_text("return 1")
        (tmp_path / "conversion_plan.json").write_text("{}")

        guard_or_clean_output_dir(tmp_path, "generic", clean=True)

        # Children are gone; the stamp now matches the new mode.
        assert read_scene_runtime_stamp(tmp_path) == "generic"
        assert not (tmp_path / "scripts").exists()
        assert not (tmp_path / "conversion_plan.json").exists()

    def test_clean_creates_dir_if_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "freshly-created"
        guard_or_clean_output_dir(target, "generic", clean=True)
        assert target.is_dir()
        assert read_scene_runtime_stamp(target) == "generic"

    def test_apply_clean_directive_rejects_non_directory(
        self, tmp_path: Path,
    ) -> None:
        f = tmp_path / "not-a-dir"
        f.write_text("x")
        with pytest.raises(NotADirectoryError):
            apply_clean_directive(f, "legacy")
