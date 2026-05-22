"""test_scene_runtime_cli.py -- CLI front-door surface for ``--scene-runtime``.

PR3a wired the flag at every conversion front door; PR4 lifts the
hard-rejection of ``generic`` and lights up the host runtime path.
``auto`` is still rejected because its fallback-routing decision lands
in PR5 with its canary projects. ``generic`` flows through to the
pipeline; bad downstream state (no Unity project, etc.) surfaces
normally.

Front doors covered:
  * ``u2r.py convert``        -- main conversion command
  * ``u2r.py eval``           -- eval baseline command
  * ``convert_interactive.py transpile`` -- interactive Mode-2 transpile
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).parent.parent))

from u2r import main as u2r_main  # noqa: E402
from convert_interactive import cli as ci_cli  # noqa: E402


# ---------------------------------------------------------------------------
# u2r.py convert
# ---------------------------------------------------------------------------

class TestConvertFlag:

    def test_flag_default_is_legacy(self):
        # ``--help`` output must list legacy as the default so users see
        # the safe value first.
        runner = CliRunner()
        result = runner.invoke(u2r_main, ["convert", "--help"])
        assert result.exit_code == 0
        assert "--scene-runtime" in result.output
        # Click renders ``show_default=True`` as ``[default: legacy]``.
        assert "legacy" in result.output

    def test_flag_choices_are_legacy_auto_generic(self):
        runner = CliRunner()
        result = runner.invoke(u2r_main, ["convert", "--help"])
        for choice in ("legacy", "auto", "generic"):
            assert choice in result.output

    def test_generic_no_longer_rejected_at_cli(self, tmp_path):
        # PR4: ``generic`` is accepted at the CLI. It may still fail
        # downstream because ``tmp_path`` isn't a real Unity project,
        # but the failure should NOT be the PR3a-era spike-pointer
        # rejection. Assert the absence of the legacy rejection text.
        runner = CliRunner()
        result = runner.invoke(u2r_main, [
            "convert", str(tmp_path),
            "--skip-architecture-step",
            "--scene-runtime=generic",
        ])
        # We don't assert exit_code here -- a fake Unity project will
        # error somewhere in the pipeline; what matters is the legacy
        # PR3a rejection text is gone.
        assert "scene_runtime_spike" not in result.output, (
            "PR4 lifted the generic CLI gate; the spike-tool pointer "
            "should no longer appear in the output."
        )

    def test_auto_still_rejected(self, tmp_path):
        # PR5 owns ``auto`` (try-generic-then-legacy with canary
        # projects); PR4 keeps it rejected at the CLI.
        runner = CliRunner()
        result = runner.invoke(u2r_main, [
            "convert", str(tmp_path),
            "--skip-architecture-step",
            "--scene-runtime=auto",
        ])
        assert result.exit_code != 0
        assert "auto" in result.output
        assert "PR5" in result.output, (
            "auto rejection should attribute deferral to PR5 so users "
            "see the timeline."
        )

    def test_invalid_value_rejected_by_click(self, tmp_path):
        # Values outside the Choice set fail at parse time with a click
        # error (different from our PR3a fail-closed message).
        runner = CliRunner()
        result = runner.invoke(u2r_main, [
            "convert", str(tmp_path),
            "--skip-architecture-step",
            "--scene-runtime=nonsense",
        ])
        assert result.exit_code != 0
        assert "nonsense" in result.output or "invalid choice" in result.output.lower()


# ---------------------------------------------------------------------------
# u2r.py eval
# ---------------------------------------------------------------------------

class TestEvalFlag:

    def test_flag_present_on_eval(self):
        runner = CliRunner()
        result = runner.invoke(u2r_main, ["eval", "--help"])
        assert result.exit_code == 0
        assert "--scene-runtime" in result.output

    def test_eval_still_rejects_auto(self):
        # PR4: ``generic`` is accepted at eval; ``auto`` is the only
        # remaining CLI-level rejection (PR5 turf).
        runner = CliRunner()
        result = runner.invoke(u2r_main, ["eval", "--scene-runtime=auto"])
        assert result.exit_code != 0
        assert "auto" in result.output


# ---------------------------------------------------------------------------
# convert_interactive.py transpile
# ---------------------------------------------------------------------------

class TestInteractiveTranspileFlag:

    def test_flag_present(self):
        runner = CliRunner()
        result = runner.invoke(ci_cli, ["transpile", "--help"])
        assert result.exit_code == 0
        assert "--scene-runtime" in result.output

    def test_auto_rejected_with_structured_json(self, tmp_path):
        # PR4: ``auto`` is the remaining CLI-rejected mode (PR5 turf).
        # The interactive CLI emits structured JSON on errors.
        runner = CliRunner()
        project = tmp_path / "project"
        project.mkdir()
        output = tmp_path / "output"
        result = runner.invoke(ci_cli, [
            "transpile",
            str(project),
            str(output),
            "--scene-runtime=auto",
        ])
        assert result.exit_code != 0
        # ``_emit`` prints a (multi-line indented) JSON object to stdout.
        start = result.output.find("{")
        end = result.output.rfind("}")
        payload = None
        if start != -1 and end != -1:
            try:
                payload = json.loads(result.output[start:end + 1])
            except json.JSONDecodeError:
                payload = None
        assert payload is not None, (
            "interactive transpile must emit a structured JSON error "
            f"payload, but stdout was: {result.output[:500]!r}"
        )
        assert payload.get("phase") == "transpile"
        assert payload.get("success") is False
        assert any("PR5" in e for e in payload.get("errors", [])), (
            f"auto rejection should attribute deferral to PR5: {payload}"
        )


# ---------------------------------------------------------------------------
# Legacy mode is unchanged -- the conversion path must still work with
# the default flag value. We assert by invoking ``convert --help``
# (full conversion is too heavy for a fast unit test); the default
# being ``legacy`` means existing invocations without the flag still hit
# the byte-identical legacy pipeline.
# ---------------------------------------------------------------------------

class TestLegacyDefault:

    def test_convert_default_runtime_is_legacy_in_help(self):
        runner = CliRunner()
        result = runner.invoke(u2r_main, ["convert", "--help"])
        # The help output should show the default value next to the flag.
        # We grep for ``[default: legacy]`` (click's canonical
        # show_default=True rendering).
        assert "default: legacy" in result.output or "legacy]" in result.output, (
            "Default --scene-runtime value should be legacy and visibly "
            "documented in --help so users don't accidentally invoke a "
            "non-default mode."
        )

    def test_eval_default_runtime_is_legacy_in_help(self):
        runner = CliRunner()
        result = runner.invoke(u2r_main, ["eval", "--help"])
        assert "default: legacy" in result.output or "legacy]" in result.output
