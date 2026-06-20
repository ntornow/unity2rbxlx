"""
test_script_cache.py -- Tests for the on-disk transpiled-scripts cache check.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.script_cache import count_top_level_scripts, scripts_cache_intact


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


class TestCountTopLevelScripts:
    def test_missing_dir_is_zero(self, tmp_path):
        assert count_top_level_scripts(tmp_path) == 0

    def test_empty_dir_is_zero(self, tmp_path):
        (tmp_path / "scripts").mkdir()
        assert count_top_level_scripts(tmp_path) == 0

    def test_counts_only_top_level_luau(self, tmp_path):
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "a.luau").write_text("x")
        (scripts / "b.luau").write_text("y")
        (scripts / "n.txt").write_text("z")  # non-luau ignored
        sub = scripts / "animations"
        sub.mkdir()
        (sub / "c.luau").write_text("w")  # nested ignored
        assert count_top_level_scripts(tmp_path) == 2


class TestPostPruneCacheIntegrity:
    def test_post_prune_count_keeps_clean_cache_intact(self, tmp_path):
        """Regression: the dead-module prune deletes top-level scripts from disk
        AFTER ``transpiled_scripts`` (the pre-prune total) is recorded. Comparing
        the cache against the pre-prune total wrongly re-transpiles a clean
        cache; comparing against the recorded post-prune count keeps it intact."""
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        # 4 scripts survive on disk after pruning 2 dead modules (pre-prune 6).
        for i in range(4):
            (scripts / f"s{i}.luau").write_text("-- script")
        assert count_top_level_scripts(tmp_path) == 4
        # The bug: comparing against the pre-prune total fails a clean cache.
        assert scripts_cache_intact(tmp_path, 6) is False
        # The fix: comparing against the post-prune count passes.
        assert scripts_cache_intact(tmp_path, 4) is True


class TestExpectedCachedScriptCount:
    """ConversionContext.expected_cached_script_count uses -1 as the 'unset'
    sentinel so a legitimate post-prune count of 0 is distinguishable from
    'never recorded' (the latter falls back to the pre-prune total)."""

    def test_unset_falls_back_to_transpiled(self):
        from core.conversion_context import ConversionContext
        c = ConversionContext(transpiled_scripts=60)  # cached default -1
        assert c.expected_cached_script_count() == 60

    def test_recorded_zero_is_used_not_fallback(self):
        from core.conversion_context import ConversionContext
        c = ConversionContext(transpiled_scripts=60, cached_script_count=0)
        assert c.expected_cached_script_count() == 0

    def test_recorded_count_is_used(self):
        from core.conversion_context import ConversionContext
        c = ConversionContext(transpiled_scripts=60, cached_script_count=59)
        assert c.expected_cached_script_count() == 59
