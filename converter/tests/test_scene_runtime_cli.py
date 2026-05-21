"""test_scene_runtime_cli.py -- CLI front-door surface for ``--scene-runtime``.

PR3a wired the flag at every conversion front door; PR4 lit up
``generic`` (host runtime); **PR5 lifts the ``auto`` rejection** so all
three modes route through the pipeline. ``auto`` routes through the
generic branch and surfaces fail-closed signals as structured errors
(see ``Pipeline._check_auto_fail_closed``).

Front doors covered:
  * ``u2r.py convert``        -- main conversion command
  * ``u2r.py eval``           -- eval baseline command
  * ``convert_interactive.py transpile`` -- interactive Mode-2 transpile
"""

from __future__ import annotations

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

    def test_auto_no_longer_rejected_at_cli(self, tmp_path):
        # PR5 lifts the ``auto`` rejection at the CLI; the conversion
        # routes through the generic pipeline. Like ``generic``, it may
        # still error downstream (tmp_path isn't a real Unity project),
        # but the failure should NOT be the PR4-era spike-pointer
        # rejection text -- that rejection has been removed.
        runner = CliRunner()
        result = runner.invoke(u2r_main, [
            "convert", str(tmp_path),
            "--skip-architecture-step",
            "--scene-runtime=auto",
        ])
        assert "auto is not yet supported" not in result.output, (
            "PR5 lifted the auto rejection; the PR4 deferral text should "
            "no longer appear in the output."
        )

    def test_help_text_no_longer_marks_auto_as_reserved(self):
        """R1-P3 (codex round 1): the ``--help`` text must reflect that
        ``auto`` is now user-reachable (PR5). Pre-PR5 the strings said
        "reserved for PR5" or "currently rejected"; those phrases now
        misleads operators reading the help on stable PR5+."""
        runner = CliRunner()
        for argv in (["convert", "--help"], ["eval", "--help"]):
            result = runner.invoke(u2r_main, argv)
            assert result.exit_code == 0
            assert "auto" in result.output
            assert "reserved for PR5" not in result.output, (
                f"{' '.join(argv)} --help still says auto is reserved"
            )
            assert "currently rejected" not in result.output, (
                f"{' '.join(argv)} --help still says auto is rejected"
            )
        for argv in (
            ["transpile", "--help"],
            ["assemble", "--help"],
            ["upload", "--help"],
        ):
            result = runner.invoke(ci_cli, argv)
            assert result.exit_code == 0
            assert "reserved for PR5" not in result.output, (
                f"convert_interactive {argv[0]} --help still says auto "
                "is reserved"
            )
            assert "auto' reserved" not in result.output, (
                f"convert_interactive {argv[0]} --help still says "
                "'auto reserved'"
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

    def test_eval_auto_no_longer_rejected(self):
        # PR5: ``auto`` is now lifted across all eval front doors as
        # well. Eval may still fail without a project arg, but the
        # PR4-era rejection text should be absent.
        runner = CliRunner()
        result = runner.invoke(u2r_main, ["eval", "--scene-runtime=auto"])
        assert "auto is not yet supported" not in result.output


# ---------------------------------------------------------------------------
# convert_interactive.py transpile
# ---------------------------------------------------------------------------

class TestInteractiveTranspileFlag:

    def test_flag_present(self):
        runner = CliRunner()
        result = runner.invoke(ci_cli, ["transpile", "--help"])
        assert result.exit_code == 0
        assert "--scene-runtime" in result.output

    def test_auto_no_longer_rejected_with_pr4_deferral(self, tmp_path):
        # PR5: the interactive transpile front door no longer rejects
        # ``auto``. It may still error downstream (no Unity project at
        # ``tmp_path``), but the PR4 "auto deferred to PR5" payload
        # should be absent.
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
        # PR5 rejection-attribute string should NOT appear: the old
        # error message linked the failure to "PR5 turf"; PR5 is now
        # in production. We don't assert exit_code (legitimate
        # downstream failures are still possible).
        assert "is not yet supported" not in result.output, (
            "PR5 lifted the auto deferral; the PR4 rejection envelope "
            "should no longer appear."
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
