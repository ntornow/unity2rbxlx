"""
Shared ``luau-analyze`` syntax-check runner.

Both the transpiler (checks in-memory generated Luau) and the interactive
``validate`` phase (checks files on disk) need to run ``luau-analyze`` and
extract its ``SyntaxError`` lines. This module is the single implementation.

Only ``SyntaxError`` lines are reported — ``TypeError``s for unknown
Roblox-specific globals are expected and intentionally filtered out.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


def luau_analyze_path() -> str | None:
    """Return the path to the ``luau-analyze`` binary, or None if not installed."""
    return shutil.which("luau-analyze")


def syntax_errors_for_file(path: str | Path, timeout: float = 10.0) -> list[str]:
    """Run ``luau-analyze`` on a file and return its ``SyntaxError`` lines.

    Returns an empty list when the file is syntactically valid, when
    ``luau-analyze`` is not installed, or when the run times out.
    """
    analyzer = luau_analyze_path()
    if not analyzer:
        return []
    try:
        result = subprocess.run(
            [analyzer, str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [
        line
        for line in (result.stdout.splitlines() + result.stderr.splitlines())
        if "SyntaxError" in line
    ]


def syntax_errors_for_source(source: str, timeout: float = 10.0) -> list[str]:
    """Run ``luau-analyze`` on an in-memory Luau source string.

    Writes the source to a temp file, checks it, and rewrites the temp path
    to ``"script"`` in the returned error lines for cleaner messages. Returns
    an empty list (without touching the filesystem) when ``luau-analyze`` is
    not installed. The temp file is always removed, even if the write or the
    analyzer run raises.
    """
    if not luau_analyze_path():
        return []
    # Capture tmp_path immediately after creation so the finally block can
    # always clean up, even if f.write(source) raises.
    f = tempfile.NamedTemporaryFile(
        mode="w", suffix=".luau", delete=False, encoding="utf-8"
    )
    tmp_path = f.name
    try:
        f.write(source)
        f.close()
        return [
            line.replace(tmp_path, "script")
            for line in syntax_errors_for_file(tmp_path, timeout=timeout)
        ]
    finally:
        f.close()  # idempotent if already closed
        Path(tmp_path).unlink(missing_ok=True)
