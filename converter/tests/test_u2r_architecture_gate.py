"""
test_u2r_architecture_gate.py -- ``u2r convert`` CLI gates / arg validation.

1. Step-4.5 acknowledgement gate: ``u2r.py convert`` runs only
   ``Pipeline.PHASES``, which never includes the client/server architecture
   split ("Step 4.5"). A ``--phase`` resume does not escape this: ``resume``
   re-runs every incomplete prerequisite, so ``--phase parse`` on a fresh
   output dir is a full conversion too. Every ``convert`` invocation therefore
   skips Step 4.5, so the CLI requires ``--skip-architecture-step`` on *every*
   ``convert`` run, ``--phase`` included.

2. ``--scene all`` and ``--phase`` are mutually exclusive: ``--scene all``
   converts every scene from scratch via ``run_all_scenes()``, which ignores
   ``--phase``. The CLI rejects the combination rather than silently dropping
   ``--phase``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from u2r import main


@pytest.fixture
def fake_unity_project(tmp_path: Path) -> Path:
    """Minimal Unity-shaped directory tree."""
    project = tmp_path / "FakeProject"
    (project / "Assets").mkdir(parents=True)
    return project


class _GateStubPipeline:
    """Stand-in for Pipeline: raises a sentinel the moment a run method is
    reached, so a test can prove the gate was passed without doing real work."""

    def __init__(self, **_kwargs):
        from core.conversion_context import ConversionContext
        self.context = ConversionContext(unity_project_path="x")
        self.ctx = self.context

    def _reached(self, *_a, **_k):
        raise RuntimeError("REACHED_PIPELINE")

    run_all = run_all_scenes = resume = _reached


def test_full_convert_without_ack_is_blocked(fake_unity_project, tmp_path):
    """A full conversion with no acknowledgement flag is refused, exit non-zero,
    and no pipeline work starts."""
    result = CliRunner().invoke(
        main, ["convert", str(fake_unity_project), "-o", str(tmp_path / "out")]
    )
    assert result.exit_code != 0
    assert "Step 4.5" in result.output
    assert "/convert-unity" in result.output


def test_phase_resume_without_ack_is_blocked(fake_unity_project, tmp_path):
    """Regression: --phase does NOT exempt the gate. `--phase parse` on a fresh
    output dir is a full conversion (resume re-runs every prerequisite), so it
    must still require the acknowledgement."""
    result = CliRunner().invoke(
        main,
        ["convert", str(fake_unity_project), "-o", str(tmp_path / "out"),
         "--phase", "parse"],
    )
    assert result.exit_code != 0
    assert "Step 4.5" in result.output


def test_ack_flag_bypasses_gate(fake_unity_project, tmp_path, monkeypatch):
    """--skip-architecture-step lets a run proceed past the gate."""
    monkeypatch.setattr("converter.pipeline.Pipeline", _GateStubPipeline)
    result = CliRunner().invoke(
        main,
        ["convert", str(fake_unity_project), "-o", str(tmp_path / "out"),
         "--skip-architecture-step"],
    )
    assert "Step 4.5" not in result.output           # gate did not fire
    assert isinstance(result.exception, RuntimeError)  # reached the pipeline
    assert "REACHED_PIPELINE" in str(result.exception)


def test_scene_all_with_phase_errors(fake_unity_project, tmp_path):
    """--scene all and --phase are mutually exclusive — the CLI rejects the
    combination instead of silently discarding --phase."""
    result = CliRunner().invoke(
        main,
        ["convert", str(fake_unity_project), "-o", str(tmp_path / "out"),
         "--scene", "all", "--phase", "convert_scene",
         "--skip-architecture-step"],
    )
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output
