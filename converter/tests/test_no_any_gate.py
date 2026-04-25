"""Tests for tools/check_no_any.sh.

The gate runs against a `git diff <base>...HEAD` and fails when added lines
introduce `Any` annotations in non-allowlisted files. These tests build
self-contained git repositories in tmp dirs so the gate's diff logic can be
exercised without polluting the real working tree.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GATE_SCRIPT = REPO_ROOT / "converter" / "tools" / "check_no_any.sh"
ALLOWLIST_FILE = REPO_ROOT / "converter" / "tools" / "no-any-allowlist.txt"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo_with_baseline(tmp_path: Path) -> Path:
    """Create a fresh git repo with the gate's tools/ vendored in and a baseline commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    # Vendor the gate script + allowlist so the gate can find them relative
    # to the repo root (the script resolves SCRIPT_DIR/../.. = repo root).
    tools = repo / "converter" / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(GATE_SCRIPT, tools / "check_no_any.sh")
    shutil.copy2(ALLOWLIST_FILE, tools / "no-any-allowlist.txt")
    os.chmod(tools / "check_no_any.sh", 0o755)

    # Seed with an empty Python file in each gated directory so future diffs
    # against this baseline have somewhere to land.
    for d in ("converter/core", "converter/converter", "converter/unity", "converter/roblox"):
        (repo / d).mkdir(parents=True, exist_ok=True)
        (repo / d / "__init__.py").write_text("")
        (repo / d / "seed.py").write_text("# seed\n")

    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _run_gate(repo: Path, base: str = "main") -> subprocess.CompletedProcess[str]:
    """Run the gate from a feature branch in the repo, comparing against base."""
    return subprocess.run(
        ["bash", "converter/tools/check_no_any.sh", base],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def _commit_change(repo: Path, branch: str, files: dict[str, str]) -> None:
    """Create a feature branch off main, write files, and commit."""
    _git(repo, "checkout", "-q", "-b", branch)
    for relpath, content in files.items():
        target = repo / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"changes for {branch}")


# ---------- T1: allowlist file is well-formed ----------

def test_t1_allowlist_parses() -> None:
    """Every non-comment line in the allowlist matches `<path> | <reason>`
    and the path resolves to an existing file."""
    text = ALLOWLIST_FILE.read_text()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert " | " in line, f"L{lineno}: expected ' | ' separator, got: {raw!r}"
        path, reason = line.split(" | ", 1)
        path = path.strip()
        reason = reason.strip()
        assert path, f"L{lineno}: empty path"
        assert reason, f"L{lineno}: empty reason"
        full = REPO_ROOT / path
        assert full.is_file(), f"L{lineno}: allowlisted file does not exist: {path}"


# ---------- T2: clean diff passes ----------

def test_t2_clean_diff_passes(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-clean", {
        "converter/converter/new_module.py": (
            "def process(name: str, count: int) -> bool:\n"
            "    return count > 0\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


# ---------- T3: `: Any` in non-allowlisted file fails ----------

def test_t3_param_any_fails(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-any-param", {
        "converter/converter/new_module.py": (
            "from typing import Any\n"
            "def process(scene: Any) -> None:\n"
            "    pass\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "scene: Any" in result.stderr or "scene: Any" in result.stdout


# ---------- T4: `dict[str, Any]` field fails (the conversion_context.py:73 pattern) ----------

def test_t4_dict_any_field_fails(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-dict-any", {
        "converter/core/new_ctx.py": (
            "from typing import Any\n"
            "from dataclasses import dataclass, field\n"
            "@dataclass\n"
            "class Ctx:\n"
            "    storage_plan: dict[str, Any] = field(default_factory=dict)\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "dict[str, Any]" in (result.stderr + result.stdout)


# ---------- T5: `-> Any` return annotation fails ----------

def test_t5_return_any_fails(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-ret-any", {
        "converter/converter/new_module.py": (
            "from typing import Any\n"
            "def fetch() -> Any:\n"
            "    return None\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "-> Any" in (result.stderr + result.stdout)


# ---------- T6: `: Any` in allowlisted boundary file passes ----------

def test_t6_any_in_allowlisted_file_passes(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-boundary", {
        "converter/unity/yaml_parser.py": (
            "from typing import Any\n"
            "def ref_file_id(ref: Any) -> str | None:\n"
            "    return None\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


# ---------- T7: `from typing import Any` import line is not flagged ----------

def test_t7_import_line_not_flagged(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-just-import", {
        "converter/converter/new_module.py": (
            "from typing import Any  # noqa: F401\n"
            "def process(name: str) -> str:\n"
            "    return name\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


# ---------- T8: `Any` inside a comment is not flagged ----------

def test_t8_comment_not_flagged(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-comment", {
        "converter/converter/new_module.py": (
            "def process(name: str) -> str:\n"
            "    # WARNING: do not use Any here\n"
            "    return name\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


# ---------- Bonus: list[Any] is flagged ----------

def test_bonus_list_any_fails(tmp_path: Path) -> None:
    """The exact smuggling pattern from the audit: `parsed_scenes: list[Any]`."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-list-any", {
        "converter/converter/new_module.py": (
            "from typing import Any\n"
            "def f(parsed_scenes: list[Any]) -> None:\n"
            "    pass\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "list[Any]" in (result.stderr + result.stdout)


# ---------- Bonus: `typing.Any` is flagged (regression for codex P1) ----------

def test_bonus_typing_dot_any_fails(tmp_path: Path) -> None:
    """Don't let `typing.Any` slip past the bare-token regex."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-typing-any", {
        "converter/converter/new_module.py": (
            "import typing\n"
            "def f(scene: typing.Any) -> None:\n"
            "    pass\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "typing.Any" in (result.stderr + result.stdout)


# ---------- Bonus: `x: Any = ...` is flagged (regression for codex round 2 P1) ----------

def test_bonus_any_with_assignment_fails(tmp_path: Path) -> None:
    """Class field / module variable annotations followed by `=` must fail.
    Earlier regex required the right-of-Any to be `]`, `,`, `|`, etc., so
    `value: Any = None` slipped through. Now using a non-identifier-char
    boundary on the right side."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-any-assign", {
        "converter/converter/new_module.py": (
            "from typing import Any\n"
            "value: Any = None\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "value: Any" in (result.stderr + result.stdout)


def test_bonus_typing_any_with_assignment_fails(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-typing-any-assign", {
        "converter/converter/new_module.py": (
            "import typing\n"
            "value: typing.Any = None\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1


def test_bonus_union_with_any_and_assignment_fails(tmp_path: Path) -> None:
    """`int | Any = 0` was passing under the prior regex."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-union-any-assign", {
        "converter/converter/new_module.py": (
            "from typing import Any\n"
            "value: int | Any = 0\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1


# ---------- Bonus: multi-statement import bypass (regression for codex round 3 P3) ----------

def test_bonus_multistatement_import_bypass_fails(tmp_path: Path) -> None:
    """`import typing; value: typing.Any = None` must not bypass the gate.
    Older versions had `next` clauses that skipped any line starting with
    a typing import, letting the rest of the multi-statement line through."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-multistmt", {
        "converter/converter/new_module.py": (
            "import typing; value: typing.Any = None\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "typing.Any" in (result.stderr + result.stdout)


def test_bonus_multistatement_from_import_bypass_fails(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-from-multistmt", {
        "converter/converter/new_module.py": (
            "from typing import Any; x: Any = None\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1


# ---------- Bonus: string literals containing `Any` are not flagged (codex round 3 P2) ----------

def test_bonus_string_literal_with_any_not_flagged(tmp_path: Path) -> None:
    """Strings like `message = "expected ': Any' here"` must not false-positive."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-string-any", {
        "converter/converter/new_module.py": (
            'def f() -> None:\n'
            '    message = "expected \': Any\' here"\n'
            '    print(message)\n'
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


def test_bonus_double_quoted_string_with_any_annotation_pattern_not_flagged(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-double-string-any", {
        "converter/converter/new_module.py": (
            'def f() -> str:\n'
            '    return "x: Any"\n'
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


# ---------- Bonus: aliased Any imports are blocked (codex round 4 P2) ----------

def test_bonus_any_aliased_via_from_import_fails(tmp_path: Path) -> None:
    """`from typing import Any as Dyn` is a bypass; block it."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-any-alias", {
        "converter/converter/new_module.py": (
            "from typing import Any as Dyn\n"
            "def f(x: Dyn) -> None:\n"
            "    pass\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "aliasing Any" in (result.stderr + result.stdout)


def test_bonus_aliased_typing_any_in_annotation_fails(tmp_path: Path) -> None:
    """`import typing as t` is allowed (typing has many legitimate names),
    but `t.Any` at the use site is caught via the broadened module-prefix
    pattern in the main annotation regex."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-typing-alias", {
        "converter/converter/new_module.py": (
            "import typing as t\n"
            "def f(x: t.Any) -> None:\n"
            "    pass\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 1
    assert "t.Any" in (result.stderr + result.stdout)


def test_bonus_typing_alias_used_for_non_any_passes(tmp_path: Path) -> None:
    """`import typing as t` with `t.Self` (or any non-Any usage) must pass."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-typing-alias-self", {
        "converter/converter/new_module.py": (
            "import typing as t\n"
            "class Foo:\n"
            "    def clone(self) -> t.Self:\n"
            "        return self\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


def test_bonus_unrelated_typing_import_alias_passes(tmp_path: Path) -> None:
    """Aliasing a non-Any name (e.g. TypeVar) must not be blocked."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-typevar-alias", {
        "converter/converter/new_module.py": (
            "from typing import TypeVar as TV\n"
            "T = TV(\"T\")\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


# ---------- Bonus: dict/list literals containing "Any" are not flagged ----------

def test_bonus_list_literal_with_any_string_passes(tmp_path: Path) -> None:
    """`labels = ["Any"]` is a list literal, not an annotation."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-list-literal", {
        "converter/converter/new_module.py": (
            'labels = ["Any", "Specific"]\n'
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


def test_bonus_dict_literal_with_any_string_passes(tmp_path: Path) -> None:
    """`opts = {"mode": "Any"}` is a dict literal, not an annotation."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-dict-literal", {
        "converter/converter/new_module.py": (
            'opts = {"mode": "Any", "level": 1}\n'
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


# ---------- Bonus: missing base ref fails loudly (regression for codex P1) ----------

def test_bonus_missing_base_ref_fails_loudly(tmp_path: Path) -> None:
    """If the base ref isn't reachable, the gate must fail with a clear error,
    not silently exit 0 because git diff returned nothing."""
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-needs-base", {
        "converter/converter/new_module.py": (
            "def process(name: str) -> str:\n    return name\n"
        ),
    })
    result = _run_gate(repo, base="origin/never-fetched")
    assert result.returncode == 2
    assert "not reachable" in (result.stderr + result.stdout)


# ---------- Bonus: `Any` as a word inside identifiers is not flagged ----------

def test_bonus_word_in_identifier_not_flagged(tmp_path: Path) -> None:
    repo = _make_repo_with_baseline(tmp_path)
    _commit_change(repo, "feat-identifier", {
        "converter/converter/new_module.py": (
            "def has_anything(values: list[str]) -> bool:\n"
            "    return bool(values)\n"
        ),
    })
    result = _run_gate(repo)
    assert result.returncode == 0, f"unexpected fail: {result.stdout}\n{result.stderr}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
