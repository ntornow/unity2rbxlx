"""test_scene_runtime_cli.py -- CLI front-door surface for ``--scene-runtime``.

PR3a wires the flag at every conversion front door so PR3b/PR4 can flip
on the contract pipeline without re-touching CLI plumbing. The host
runtime doesn't exist yet, so 'auto' and 'generic' must be rejected
at the CLI with a clear error message pointing at the spike tool.

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

    def test_generic_rejected_with_spike_pointer(self, tmp_path):
        # Need an existing directory for the positional ``unity_project``.
        # The CLI rejects before any project parsing happens; tmp_path
        # is sufficient as a placeholder.
        runner = CliRunner()
        result = runner.invoke(u2r_main, [
            "convert", str(tmp_path),
            "--skip-architecture-step",
            "--scene-runtime=generic",
        ])
        assert result.exit_code != 0, (
            "--scene-runtime=generic should be rejected at the CLI until "
            "PR4 lands the host runtime."
        )
        assert "scene_runtime_spike" in result.output, (
            "Rejection message must point at the spike tool so users can "
            "still exercise the contract verifier."
        )

    def test_auto_rejected(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(u2r_main, [
            "convert", str(tmp_path),
            "--skip-architecture-step",
            "--scene-runtime=auto",
        ])
        assert result.exit_code != 0
        assert "auto" in result.output

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

    def test_eval_rejects_generic(self):
        # ``eval`` accepts no positional args; the flag check happens
        # before any test_projects parsing.
        runner = CliRunner()
        result = runner.invoke(u2r_main, ["eval", "--scene-runtime=generic"])
        assert result.exit_code != 0
        assert "scene_runtime_spike" in result.output


# ---------------------------------------------------------------------------
# convert_interactive.py transpile
# ---------------------------------------------------------------------------

class TestInteractiveTranspileFlag:

    def test_flag_present(self):
        runner = CliRunner()
        result = runner.invoke(ci_cli, ["transpile", "--help"])
        assert result.exit_code == 0
        assert "--scene-runtime" in result.output

    def test_generic_rejected_with_structured_json(self, tmp_path):
        # The interactive CLI emits structured JSON on errors. Verify
        # the rejection lands in that channel rather than as raw stderr.
        runner = CliRunner()
        # Need a directory for unity_project_path; tmp_path will do --
        # the flag check runs before any project parsing.
        project = tmp_path / "project"
        project.mkdir()
        output = tmp_path / "output"
        result = runner.invoke(ci_cli, [
            "transpile",
            str(project),
            str(output),
            "--scene-runtime=generic",
        ])
        assert result.exit_code != 0
        # ``_emit`` prints a (multi-line indented) JSON object to stdout.
        # Parse the whole output buffer between the first ``{`` and the
        # last ``}``.
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
        assert any("scene_runtime_spike" in e for e in payload.get("errors", [])), (
            f"rejection error message must point at the spike tool: {payload}"
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
