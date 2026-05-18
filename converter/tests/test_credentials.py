"""
test_credentials.py -- Tests for credential resolution precedence (CLI/env/file).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.credentials import resolve_credential


class TestCliValue:
    def test_literal_cli_value(self, tmp_path):
        result = resolve_credential("my-secret", "ENV_VAR", "key.txt", tmp_path)
        assert result == "my-secret"

    def test_cli_value_is_stripped(self, tmp_path):
        result = resolve_credential("  spaced  \n", "ENV_VAR", "key.txt", tmp_path)
        assert result == "spaced"

    def test_cli_value_as_file_path(self, tmp_path):
        key_file = tmp_path / "secret.txt"
        key_file.write_text("  file-credential\n")
        result = resolve_credential(str(key_file), "ENV_VAR", "key.txt", tmp_path)
        assert result == "file-credential"

    def test_cli_takes_precedence_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBLOX_KEY", "from-env")
        result = resolve_credential("from-cli", "ROBLOX_KEY", "key.txt", tmp_path)
        assert result == "from-cli"

    def test_cli_takes_precedence_over_file(self, tmp_path):
        (tmp_path / "key.txt").write_text("from-discovered-file")
        project = tmp_path / "project"
        project.mkdir()
        result = resolve_credential("from-cli", "ROBLOX_KEY", "key.txt", project)
        assert result == "from-cli"


class TestEnvVar:
    def test_env_var_used_when_no_cli(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBLOX_KEY", "env-credential")
        result = resolve_credential(None, "ROBLOX_KEY", "key.txt", tmp_path)
        assert result == "env-credential"

    def test_env_var_is_stripped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBLOX_KEY", "  padded-env  ")
        result = resolve_credential(None, "ROBLOX_KEY", "key.txt", tmp_path)
        assert result == "padded-env"

    def test_empty_env_var_falls_through_to_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBLOX_KEY", "")
        (tmp_path / "key.txt").write_text("from-file")
        project = tmp_path / "project"
        project.mkdir()
        result = resolve_credential(None, "ROBLOX_KEY", "key.txt", project)
        assert result == "from-file"

    def test_env_takes_precedence_over_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ROBLOX_KEY", "env-wins")
        (tmp_path / "key.txt").write_text("file-loses")
        project = tmp_path / "project"
        project.mkdir()
        result = resolve_credential(None, "ROBLOX_KEY", "key.txt", project)
        assert result == "env-wins"


class TestFileDiscovery:
    def test_discovers_in_parent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBLOX_KEY", raising=False)
        # project_path.parent holds the key file.
        project = tmp_path / "project"
        project.mkdir()
        (tmp_path / "key.txt").write_text("  parent-key  \n")
        result = resolve_credential(None, "ROBLOX_KEY", "key.txt", project)
        assert result == "parent-key"

    def test_discovers_in_parent_parent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBLOX_KEY", raising=False)
        grandparent = tmp_path
        parent = grandparent / "p"
        project = parent / "proj"
        project.mkdir(parents=True)
        (grandparent / "key.txt").write_text("grandparent-key")
        result = resolve_credential(None, "ROBLOX_KEY", "key.txt", project)
        assert result == "grandparent-key"

    def test_discovers_in_cwd(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBLOX_KEY", raising=False)
        cwd = tmp_path / "workdir"
        cwd.mkdir()
        (cwd / "key.txt").write_text("cwd-key")
        monkeypatch.chdir(cwd)
        # project_path is isolated elsewhere so parent/parent.parent miss.
        isolated = tmp_path / "elsewhere" / "deep" / "project"
        isolated.mkdir(parents=True)
        result = resolve_credential(None, "ROBLOX_KEY", "key.txt", isolated)
        assert result == "cwd-key"

    def test_returns_none_when_nothing_found(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ROBLOX_KEY", raising=False)
        isolated = tmp_path / "a" / "b" / "project"
        isolated.mkdir(parents=True)
        empty_cwd = tmp_path / "emptycwd"
        empty_cwd.mkdir()
        monkeypatch.chdir(empty_cwd)
        result = resolve_credential(None, "ROBLOX_KEY", "key.txt", isolated)
        assert result is None
