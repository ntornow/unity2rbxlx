"""Pin the cold-e2e nightly job's AI-availability gate steps + their order.

Regression guard for the 2026-06-20 nightly: the self-hosted runner's job PATH
omitted ~/.local/bin (where the ``claude`` CLI lives), so the "AI-on" cold
convert ran with zero AI transpiles and shipped an all-stub conversion that
surfaced misleadingly as ``mouse_moves_view=false`` at the bind step. The fix
adds three steps to the ``cold-e2e`` job:

  1. put ~/.local/bin on ``$GITHUB_PATH`` (so the convert resolves the claude CLI),
  2. a fast precondition assert that an AI backend exists, and
  3. a LOAD-BEARING post-convert guard asserting ``scripts.ai_transpiled > 0``.

``$GITHUB_PATH`` affects only SUBSEQUENT steps, so step order is load-bearing:
PATH-export must precede both the precondition assert and the convert. This is a
smoke pin (existence + relative order); the faithful ``workflow_dispatch`` run on
the self-hosted runner remains the real end-to-end gate.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "test.yml"


def _cold_e2e_steps() -> list[dict]:
    assert _WORKFLOW.is_file(), f"workflow not found: {_WORKFLOW}"
    doc = yaml.safe_load(_WORKFLOW.read_text())
    jobs = doc["jobs"]
    assert "cold-e2e" in jobs, "cold-e2e job missing from test.yml"
    steps = jobs["cold-e2e"]["steps"]
    assert isinstance(steps, list) and steps, "cold-e2e has no steps"
    return steps


def _index_where(steps: list[dict], predicate) -> int:
    """Index of the first step matching ``predicate(name, run)``; -1 if none."""
    for i, step in enumerate(steps):
        name = str(step.get("name", ""))
        run = str(step.get("run", ""))
        if predicate(name, run):
            return i
    return -1


def test_path_export_step_present_and_targets_local_bin() -> None:
    steps = _cold_e2e_steps()
    i = _index_where(
        steps,
        lambda name, run: "GITHUB_PATH" in run and ".local/bin" in run,
    )
    assert i >= 0, "cold-e2e is missing the step that puts ~/.local/bin on $GITHUB_PATH"


def test_ai_backend_precondition_assert_present() -> None:
    steps = _cold_e2e_steps()
    i = _index_where(
        steps,
        lambda name, run: "command -v claude" in run and "ANTHROPIC_API_KEY" in run,
    )
    assert i >= 0, "cold-e2e is missing the AI-backend precondition assert step"


def test_post_convert_ai_transpiled_guard_present() -> None:
    steps = _cold_e2e_steps()
    i = _index_where(
        steps,
        lambda name, run: "assert_ai_transpiled.py" in run and "conversion_report.json" in run,
    )
    assert i >= 0, "cold-e2e is missing the post-convert ai_transpiled>0 guard step"


def test_gate_step_ordering_is_load_bearing() -> None:
    """PATH-export < precondition-assert < convert < ai_transpiled guard.

    $GITHUB_PATH only affects subsequent steps, so the export must precede the
    steps that depend on claude being resolvable.
    """
    steps = _cold_e2e_steps()
    path_export = _index_where(
        steps, lambda name, run: "GITHUB_PATH" in run and ".local/bin" in run
    )
    precondition = _index_where(
        steps, lambda name, run: "command -v claude" in run and "ANTHROPIC_API_KEY" in run
    )
    convert = _index_where(
        steps, lambda name, run: "u2r.py convert" in run
    )
    guard = _index_where(
        steps, lambda name, run: "assert_ai_transpiled.py" in run and "conversion_report.json" in run
    )
    assert -1 not in (path_export, precondition, convert, guard), (
        f"a required cold-e2e step is missing: "
        f"path_export={path_export} precondition={precondition} "
        f"convert={convert} guard={guard}"
    )
    assert path_export < precondition < convert < guard, (
        "cold-e2e step order must be PATH-export < precondition < convert < guard; "
        f"got path_export={path_export} precondition={precondition} "
        f"convert={convert} guard={guard}"
    )
