"""
test_script_cache.py -- Tests for the on-disk transpiled-scripts cache check.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.script_cache import scripts_cache_intact


class TestExpectedCountGuard:
    def test_zero_expected_count_is_not_intact(self, tmp_path):
        # An expected count of 0 means there is nothing to rehydrate; treat
        # the cache as not-intact so callers retranspile.
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        assert scripts_cache_intact(tmp_path, 0) is False

    def test_negative_expected_count_is_not_intact(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        assert scripts_cache_intact(tmp_path, -3) is False


class TestMissingDirectory:
    def test_missing_scripts_dir_is_not_intact(self, tmp_path):
        assert scripts_cache_intact(tmp_path, 1) is False

    def test_scripts_path_is_a_file_not_dir(self, tmp_path):
        # A file named "scripts" is not a directory -> not intact.
        (tmp_path / "scripts").write_text("not a dir")
        assert scripts_cache_intact(tmp_path, 1) is False


class TestTopLevelCounting:
    def test_exact_count_match(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        for i in range(3):
            (scripts / f"s{i}.luau").write_text("-- script")
        assert scripts_cache_intact(tmp_path, 3) is True

    def test_more_than_expected_is_intact(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        for i in range(5):
            (scripts / f"s{i}.luau").write_text("-- script")
        assert scripts_cache_intact(tmp_path, 3) is True

    def test_fewer_than_expected_is_not_intact(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        for i in range(2):
            (scripts / f"s{i}.luau").write_text("-- script")
        assert scripts_cache_intact(tmp_path, 3) is False

    def test_empty_scripts_dir_is_not_intact(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        assert scripts_cache_intact(tmp_path, 1) is False


class TestSubdirectoriesIgnored:
    def test_nested_luau_files_do_not_count(self, tmp_path):
        # Only top-level *.luau counts -- scripts buried in animations/,
        # packages/ etc. must not be counted toward the expected total.
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        for sub in ("animations", "packages", "scriptable_objects"):
            d = scripts / sub
            d.mkdir()
            (d / "nested.luau").write_text("-- nested")
        # No top-level scripts at all -> partially-archived dir, not intact.
        assert scripts_cache_intact(tmp_path, 1) is False

    def test_top_level_counts_but_nested_does_not(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "top.luau").write_text("-- top")
        nested = scripts / "animations"
        nested.mkdir()
        (nested / "a.luau").write_text("-- a")
        (nested / "b.luau").write_text("-- b")
        # Only 1 top-level despite 3 total .luau files.
        assert scripts_cache_intact(tmp_path, 1) is True
        assert scripts_cache_intact(tmp_path, 2) is False


class TestNonLuauIgnored:
    def test_non_luau_extensions_excluded(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "real.luau").write_text("-- real")
        (scripts / "readme.txt").write_text("notes")
        (scripts / "data.json").write_text("{}")
        assert scripts_cache_intact(tmp_path, 1) is True
        assert scripts_cache_intact(tmp_path, 2) is False
