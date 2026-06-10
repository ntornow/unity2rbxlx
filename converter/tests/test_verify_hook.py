"""Unit tests for the post-conversion ``--verify`` hook (slice 1.6).

Covers the shared ``verify_hook`` decision/resolution/verdict logic that both
``u2r.py convert`` and ``convert_interactive.py assemble`` route through. Pure
unit tests — no Studio launch, ``run_smoke_test`` is never invoked here (the
platform/Studio gate is exercised via monkeypatch).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import config
import verify_hook

CONVERTER_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _real_studio_available(monkeypatch):
    """Override the suite-wide ``_disable_auto_verify`` stub for THIS module.

    The shared conftest forces ``verify_hook.studio_available`` OFF (env var +
    in-process stub) so unrelated ``convert`` tests never launch Studio. These
    tests instead exercise the genuine platform/Studio resolution via
    monkeypatched ``platform`` / ``config.STUDIO_PATH``, so undo BOTH overrides:
    unset the env var (else ``studio_available`` short-circuits to False) and
    reload the module to restore the real ``studio_available``.
    """
    monkeypatch.delenv("U2R_DISABLE_AUTO_VERIFY", raising=False)
    importlib.reload(verify_hook)


def _set_platform(monkeypatch, system: str) -> None:
    monkeypatch.setattr(verify_hook.platform, "system", lambda: system)


def _set_studio_path(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(verify_hook.config, "STUDIO_PATH", path)


class TestStudioAvailable:
    def test_false_off_macos_even_with_real_path(self, monkeypatch, tmp_path):
        studio = tmp_path / "RobloxStudio"
        studio.write_text("binary")
        _set_platform(monkeypatch, "Linux")
        _set_studio_path(monkeypatch, studio)
        assert verify_hook.studio_available() is False

    def test_false_on_macos_when_studio_path_missing(self, monkeypatch, tmp_path):
        _set_platform(monkeypatch, "Darwin")
        _set_studio_path(monkeypatch, tmp_path / "does-not-exist")
        assert verify_hook.studio_available() is False

    def test_true_on_macos_with_resolvable_studio(self, monkeypatch, tmp_path):
        studio = tmp_path / "RobloxStudio"
        studio.write_text("binary")
        _set_platform(monkeypatch, "Darwin")
        _set_studio_path(monkeypatch, studio)
        assert verify_hook.studio_available() is True


class TestShouldVerify:
    def test_no_verify_always_false(self, monkeypatch, tmp_path):
        # Even on a fully-capable Studio host, --no-verify is off.
        studio = tmp_path / "RobloxStudio"
        studio.write_text("binary")
        _set_platform(monkeypatch, "Darwin")
        _set_studio_path(monkeypatch, studio)
        assert verify_hook._should_verify(tmp_path, verify=False) is False

    def test_explicit_verify_always_true(self, monkeypatch, tmp_path):
        # Explicit --verify resolves on regardless of Studio (the caller does
        # the hard-fail fast-path for "requested but no Studio").
        _set_platform(monkeypatch, "Linux")
        _set_studio_path(monkeypatch, tmp_path / "missing")
        assert verify_hook._should_verify(tmp_path, verify=True) is True

    def test_auto_off_when_no_studio(self, monkeypatch, tmp_path):
        # E1: headless Linux / plain CLI — auto resolves OFF and does NOT call
        # run_smoke_test (the platform gate is in _should_verify via
        # studio_available).
        _set_platform(monkeypatch, "Linux")
        _set_studio_path(monkeypatch, tmp_path / "missing")
        assert verify_hook._should_verify(tmp_path, verify=None) is False

    def test_auto_off_on_macos_without_studio_binary(self, monkeypatch, tmp_path):
        _set_platform(monkeypatch, "Darwin")
        _set_studio_path(monkeypatch, tmp_path / "missing")
        assert verify_hook._should_verify(tmp_path, verify=None) is False

    def test_auto_on_with_macos_and_studio(self, monkeypatch, tmp_path):
        studio = tmp_path / "RobloxStudio"
        studio.write_text("binary")
        _set_platform(monkeypatch, "Darwin")
        _set_studio_path(monkeypatch, studio)
        assert verify_hook._should_verify(tmp_path, verify=None) is True


class TestResolveVerifyTarget:
    def test_main_rbxlx_is_default_when_present(self, tmp_path):
        (tmp_path / "main.rbxlx").write_text("x")
        (tmp_path / "menu.rbxlx").write_text("x")
        target = verify_hook.resolve_verify_target(tmp_path, verify_scene=None)
        assert target == tmp_path / "main.rbxlx"

    def test_single_produced_rbxlx_when_no_main(self, tmp_path):
        (tmp_path / "converted_place.rbxlx").write_text("x")
        target = verify_hook.resolve_verify_target(tmp_path, verify_scene=None)
        assert target == tmp_path / "converted_place.rbxlx"

    def test_none_when_multiple_and_no_main(self, tmp_path):
        (tmp_path / "menu.rbxlx").write_text("x")
        (tmp_path / "level1.rbxlx").write_text("x")
        assert verify_hook.resolve_verify_target(tmp_path, verify_scene=None) is None

    def test_none_when_no_rbxlx(self, tmp_path):
        assert verify_hook.resolve_verify_target(tmp_path, verify_scene=None) is None

    def test_verify_scene_override_by_bare_name(self, tmp_path):
        (tmp_path / "main.rbxlx").write_text("x")
        (tmp_path / "menu.rbxlx").write_text("x")
        target = verify_hook.resolve_verify_target(tmp_path, verify_scene="menu")
        assert target == tmp_path / "menu.rbxlx"

    def test_verify_scene_override_with_extension(self, tmp_path):
        (tmp_path / "main.rbxlx").write_text("x")
        (tmp_path / "menu.rbxlx").write_text("x")
        target = verify_hook.resolve_verify_target(
            tmp_path, verify_scene="menu.rbxlx",
        )
        assert target == tmp_path / "menu.rbxlx"

    def test_verify_scene_override_missing_returns_none(self, tmp_path):
        (tmp_path / "main.rbxlx").write_text("x")
        assert (
            verify_hook.resolve_verify_target(tmp_path, verify_scene="ghost")
            is None
        )

    def test_verify_scene_override_wins_over_main(self, tmp_path):
        (tmp_path / "main.rbxlx").write_text("x")
        (tmp_path / "boss.rbxlx").write_text("x")
        target = verify_hook.resolve_verify_target(tmp_path, verify_scene="boss")
        assert target == tmp_path / "boss.rbxlx"


class TestBootHealthVerdict:
    def test_pass_is_not_a_failure(self):
        assert verify_hook.boot_health_failed("pass") is False

    def test_non_pass_statuses_are_failures(self):
        for status in ("fail", "error", "timeout", "unknown"):
            assert verify_hook.boot_health_failed(status) is True


def test_player_bind_required_after_phase5_flip():
    # Phase-5 contract (Step-1b): paradigm C binds the player, so the player-bind
    # axis is REQUIRED. Flipped True here alongside REQUIRE_PLAYER_BIND=1 in CI.
    assert verify_hook.REQUIRE_PLAYER_BIND is True


class TestStudioAvailableMatchesLaunchedBinary:
    """P1 #3: ``studio_available()`` must gate on the EXACT binary
    ``smoke_test.run_smoke_test`` launches. ``smoke_test.STUDIO_BINARY`` aliases
    ``config.STUDIO_PATH`` and ``smoke_test._open_studio`` launches that live
    value, so an overridden Studio path can never make the availability check
    disagree with the launch.
    """

    def test_studio_binary_aliases_config_studio_path(self):
        import smoke_test

        assert Path(smoke_test.STUDIO_BINARY) == Path(config.STUDIO_PATH)

    def test_available_reflects_the_binary_open_studio_launches(
        self, monkeypatch, tmp_path,
    ):
        # Override config.STUDIO_PATH (as ROBLOX_STUDIO_PATH would) to a path
        # that exists -> studio_available True AND _open_studio targets the same
        # binary. Then point it at a missing path -> both go False.
        import smoke_test

        studio = tmp_path / "OverriddenStudio"
        studio.write_text("binary")
        _set_platform(monkeypatch, "Darwin")
        _set_studio_path(monkeypatch, studio)
        assert verify_hook.studio_available() is True

        launched: list[str] = []
        monkeypatch.setattr(
            smoke_test.subprocess, "Popen",
            lambda args, **kw: launched.append(args[0]),
        )
        smoke_test._open_studio(tmp_path / "place.rbxlx")
        assert launched == [str(studio)], (
            "open_studio must launch the same binary studio_available gated on"
        )

        # Missing override -> availability False (and _open_studio would refuse).
        _set_studio_path(monkeypatch, tmp_path / "gone")
        assert verify_hook.studio_available() is False
        launched.clear()
        assert smoke_test._open_studio(tmp_path / "place.rbxlx") is False
        assert launched == []


class TestEnvVarDisable:
    """P1 #2: ``U2R_DISABLE_AUTO_VERIFY=1`` forces ``studio_available`` False
    even on a fully-capable Studio host. This env var inherits into subprocess
    CLI tests, so it is the load-bearing suite-wide disable.
    """

    def test_env_var_forces_unavailable_on_capable_host(
        self, monkeypatch, tmp_path,
    ):
        studio = tmp_path / "RobloxStudio"
        studio.write_text("binary")
        _set_platform(monkeypatch, "Darwin")
        _set_studio_path(monkeypatch, studio)
        # Without the env var: available.
        assert verify_hook.studio_available() is True
        # With it: forced off -> auto resolves OFF.
        monkeypatch.setenv("U2R_DISABLE_AUTO_VERIFY", "1")
        assert verify_hook.studio_available() is False
        assert verify_hook._should_verify(tmp_path, verify=None) is False

    def test_env_var_is_visible_in_a_child_process(self, tmp_path):
        """The load-bearing claim: ``U2R_DISABLE_AUTO_VERIFY`` inherits into a
        SUBPROCESS (covering the subprocess CLI tests a monkeypatch can't reach).
        Spawn a child on a host where Studio WOULD otherwise resolve (a real
        existing binary path via ROBLOX_STUDIO_PATH) and assert auto resolves OFF
        purely because the env var crossed the process boundary.
        """
        studio = tmp_path / "RobloxStudio"
        studio.write_text("binary")
        env = dict(os.environ)
        env["ROBLOX_STUDIO_PATH"] = str(studio)
        # Force the Darwin branch in the child regardless of the host OS, so the
        # only thing turning auto OFF is the env-var disable.
        prog = (
            "import platform; platform.system = lambda: 'Darwin';"
            "import verify_hook;"
            "print('SHOULD_VERIFY', "
            "verify_hook._should_verify(__import__('pathlib').Path('.'), "
            "verify=None))"
        )

        def _child(disable: str | None) -> str:
            e = dict(env)
            if disable is None:
                e.pop("U2R_DISABLE_AUTO_VERIFY", None)
            else:
                e["U2R_DISABLE_AUTO_VERIFY"] = disable
            out = subprocess.run(
                [sys.executable, "-c", prog],
                cwd=CONVERTER_ROOT, env=e, capture_output=True, text=True,
                check=True,
            )
            return out.stdout

        # Sanity: with the env var ABSENT, the capable (faked-Darwin, real
        # binary) child resolves auto ON — proving the disable is what flips it.
        assert "SHOULD_VERIFY True" in _child(None)
        # With the env var set, the child resolves auto OFF (no Studio launch).
        assert "SHOULD_VERIFY False" in _child("1")


class TestRunVerifyHookCliWiring:
    """The ``u2r.py convert`` side (``_run_verify_hook``) folds the shared
    verdict into the convert EXIT STATUS. Its branches are distinct from the
    assemble JSON-fold path tested in ``test_convert_interactive`` and were
    otherwise uncovered: the explicit-``--verify``-no-Studio fast-fail (E4),
    the unresolvable-target fast-fail, the boot/health fatal exit, and the
    documented-red bind pass (boot/health green, bind absent, non-fatal).
    ``run_smoke_test`` is stubbed — no Studio launch.
    """

    def _stub_report(self, **kw):
        from smoke_test import SmokeTestReport

        return SmokeTestReport(**kw)

    def test_explicit_verify_without_studio_exits_nonzero(
        self, monkeypatch, tmp_path,
    ):
        import u2r

        monkeypatch.setattr(verify_hook, "studio_available", lambda: False)
        called = {"smoke": False}
        monkeypatch.setattr(
            "smoke_test.run_smoke_test",
            lambda **kw: called.__setitem__("smoke", True),
        )
        with pytest.raises(SystemExit) as exc:
            u2r._run_verify_hook(tmp_path, verify=True, verify_scene=None)
        assert exc.value.code == 1
        # E4 fast-fail must NOT have launched the smoke test.
        assert called["smoke"] is False

    def test_unresolvable_target_exits_nonzero(self, monkeypatch, tmp_path):
        import u2r

        monkeypatch.setattr(verify_hook, "studio_available", lambda: True)
        # Multi-scene with no main.rbxlx -> resolve_verify_target returns None.
        (tmp_path / "a.rbxlx").write_text("x")
        (tmp_path / "b.rbxlx").write_text("x")
        called = {"smoke": False}
        monkeypatch.setattr(
            "smoke_test.run_smoke_test",
            lambda **kw: called.__setitem__("smoke", True),
        )
        with pytest.raises(SystemExit) as exc:
            u2r._run_verify_hook(tmp_path, verify=True, verify_scene=None)
        assert exc.value.code == 1
        assert called["smoke"] is False

    def test_boot_health_failure_exits_nonzero(self, monkeypatch, tmp_path):
        import u2r

        monkeypatch.setattr(verify_hook, "studio_available", lambda: True)
        (tmp_path / "main.rbxlx").write_text("x")
        monkeypatch.setattr(
            "smoke_test.run_smoke_test",
            lambda **kw: self._stub_report(status="fail"),
        )
        with pytest.raises(SystemExit) as exc:
            u2r._run_verify_hook(tmp_path, verify=True, verify_scene=None)
        assert exc.value.code == 1

    def test_required_bind_absent_exits_when_boot_health_green(
        self, monkeypatch, tmp_path,
    ):
        import u2r

        # Phase-5 contract: bind REQUIRED. Boot/health green but bind ABSENT
        # -> SystemExit(1) (the acceptance gate; never a masked boot error, E3).
        assert verify_hook.REQUIRE_PLAYER_BIND is True
        monkeypatch.setattr(verify_hook, "studio_available", lambda: True)
        (tmp_path / "main.rbxlx").write_text("x")
        monkeypatch.setattr(
            "smoke_test.run_smoke_test",
            lambda **kw: self._stub_report(
                status="pass", wasd_works=False, mouse_moves_view=False,
            ),
        )
        with pytest.raises(SystemExit) as exc:
            u2r._run_verify_hook(tmp_path, verify=True, verify_scene=None)
        assert exc.value.code == 1

    def test_required_bind_present_passes_when_boot_health_green(
        self, monkeypatch, tmp_path,
    ):
        import u2r

        # Companion to the above (AC5.8): boot/health green AND bind PRESENT
        # -> NO SystemExit (a fully C-bound player is the accepted state).
        assert verify_hook.REQUIRE_PLAYER_BIND is True
        monkeypatch.setattr(verify_hook, "studio_available", lambda: True)
        (tmp_path / "main.rbxlx").write_text("x")
        monkeypatch.setattr(
            "smoke_test.run_smoke_test",
            lambda **kw: self._stub_report(
                status="pass", wasd_works=True, mouse_moves_view=True,
            ),
        )
        u2r._run_verify_hook(tmp_path, verify=True, verify_scene=None)

    def test_auto_off_does_not_launch_or_exit(self, monkeypatch, tmp_path):
        import u2r

        # AUTO + no Studio: skip cleanly (no smoke test, no exit).
        monkeypatch.setattr(verify_hook, "studio_available", lambda: False)
        called = {"smoke": False}
        monkeypatch.setattr(
            "smoke_test.run_smoke_test",
            lambda **kw: called.__setitem__("smoke", True),
        )
        u2r._run_verify_hook(tmp_path, verify=None, verify_scene=None)
        assert called["smoke"] is False


class TestConftestDisableGuardsConvert:
    """The suite-wide ``_disable_auto_verify`` autouse fixture must actually
    prevent a real ``convert``/``assemble`` AUTO run from reaching
    ``run_smoke_test`` on a capable host. Without this guard a macOS dev box
    with Studio installed would launch Studio on every convert test. This pins
    that the fixture (env var + in-process stub) makes the AUTO decision resolve
    OFF even when the platform + binary would otherwise say ON.
    """

    def test_auto_verify_is_disabled_under_the_suite_fixture(
        self, monkeypatch, tmp_path,
    ):
        # NOTE: this module's own ``_real_studio_available`` autouse fixture
        # UNDOES the suite disable, so re-arm it here to test the suite default.
        studio = tmp_path / "RobloxStudio"
        studio.write_text("binary")
        _set_platform(monkeypatch, "Darwin")
        _set_studio_path(monkeypatch, studio)
        monkeypatch.setenv("U2R_DISABLE_AUTO_VERIFY", "1")
        # Even on a fully-capable host, the env-var disable forces AUTO OFF.
        assert verify_hook._should_verify(tmp_path, verify=None) is False
