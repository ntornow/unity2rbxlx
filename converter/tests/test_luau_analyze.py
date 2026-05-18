"""Tests for utils.luau_analyze — the shared luau-analyze runner.

Tests monkeypatch shutil.which / subprocess.run so they do not depend on
luau-analyze actually being installed.
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import luau_analyze


class _FakeCompleted:
    def __init__(self, stdout="", stderr=""):
        self.stdout = stdout
        self.stderr = stderr


class TestLuauAnalyzePath:
    def test_returns_path_when_installed(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: "/x/luau-analyze")
        assert luau_analyze.luau_analyze_path() == "/x/luau-analyze"

    def test_returns_none_when_missing(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: None)
        assert luau_analyze.luau_analyze_path() is None


class TestSyntaxErrorsForFile:
    def test_no_analyzer_returns_empty(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: None)
        assert luau_analyze.syntax_errors_for_file("anything.luau") == []

    def test_filters_only_syntax_errors(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: "/fake/luau-analyze")
        out = ("foo.luau(1,1): SyntaxError: Expected ')'\n"
               "foo.luau(2,1): TypeError: Unknown global 'workspace'\n")
        monkeypatch.setattr(luau_analyze.subprocess, "run",
                            lambda *a, **k: _FakeCompleted(stdout=out))
        errs = luau_analyze.syntax_errors_for_file("foo.luau")
        assert len(errs) == 1
        assert "SyntaxError" in errs[0]
        assert all("TypeError" not in e for e in errs)

    def test_valid_file_returns_empty(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: "/fake/luau-analyze")
        monkeypatch.setattr(luau_analyze.subprocess, "run",
                            lambda *a, **k: _FakeCompleted())
        assert luau_analyze.syntax_errors_for_file("ok.luau") == []

    def test_timeout_returns_empty(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: "/fake/luau-analyze")

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="luau-analyze", timeout=10)

        monkeypatch.setattr(luau_analyze.subprocess, "run", boom)
        assert luau_analyze.syntax_errors_for_file("slow.luau") == []


class TestSyntaxErrorsForSource:
    def test_temp_path_rewritten_and_cleaned(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: "/fake/luau-analyze")
        captured = {}

        def fake_run(cmd, **k):
            captured["tmp"] = cmd[1]  # [analyzer, tmp_file]
            return _FakeCompleted(stdout=f"{cmd[1]}(1,1): SyntaxError: bad\n")

        monkeypatch.setattr(luau_analyze.subprocess, "run", fake_run)
        errs = luau_analyze.syntax_errors_for_source("local x = ")
        assert len(errs) == 1
        # temp path is rewritten to "script" for clean messages
        assert "script(1,1): SyntaxError" in errs[0]
        assert captured["tmp"] not in errs[0]
        # temp file is cleaned up afterwards
        assert not Path(captured["tmp"]).exists()

    def test_valid_source_returns_empty(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: "/fake/luau-analyze")
        monkeypatch.setattr(luau_analyze.subprocess, "run",
                            lambda *a, **k: _FakeCompleted())
        assert luau_analyze.syntax_errors_for_source("local x = 1") == []

    def test_no_analyzer_skips_temp_file(self, monkeypatch):
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: None)

        def fail(*a, **k):
            raise AssertionError("temp file must not be created without analyzer")

        monkeypatch.setattr(luau_analyze.tempfile, "NamedTemporaryFile", fail)
        assert luau_analyze.syntax_errors_for_source("local x = 1") == []

    def test_temp_file_cleaned_up_on_exception(self, monkeypatch):
        # The finally block must remove the temp file even when the analyzer
        # run raises before returning.
        monkeypatch.setattr(luau_analyze.shutil, "which", lambda _: "/fake/luau-analyze")
        captured = {}

        def boom(path, **k):
            captured["tmp"] = path
            raise RuntimeError("analyzer blew up")

        monkeypatch.setattr(luau_analyze, "syntax_errors_for_file", boom)
        with pytest.raises(RuntimeError, match="analyzer blew up"):
            luau_analyze.syntax_errors_for_source("local x = 1")
        assert "tmp" in captured
        assert not Path(captured["tmp"]).exists()
