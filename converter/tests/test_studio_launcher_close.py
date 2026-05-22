"""Tests for roblox.studio_launcher.close_running_studio_or_fail.

Studio traps SIGTERM to raise a "save changes?" dialog and survive, so the
close helper escalates SIGTERM -> SIGKILL. These tests pin that escalation,
the graceful fast-path, the Windows path, and the give-up failure mode —
all without spawning real processes (subprocess + clock are mocked).
"""

import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from roblox import studio_launcher
from roblox.studio_launcher import (
    StudioCloseError,
    _EDITOR_PROC_PATTERN,
    close_running_studio_or_fail,
)


def test_kill_pattern_targets_editor_not_mcp_proxy():
    # The pgrep/pkill pattern must match the Studio editor process but NOT the
    # StudioMCP proxy (killing it severs the MCP connection) or the crash
    # handler. Substring containment mirrors how ``pkill -f`` matches.
    editor = "/Applications/RobloxStudio.app/Contents/MacOS/RobloxStudio /tmp/place.rbxlx"
    proxy = "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP"
    crash = "/Applications/RobloxStudio.app/Contents/MacOS/RobloxCrashHandler --studioPid 1"
    assert _EDITOR_PROC_PATTERN in editor
    assert _EDITOR_PROC_PATTERN not in proxy
    assert _EDITOR_PROC_PATTERN not in crash


class _FakeClock:
    """Deterministic monotonic clock; sleep advances it instead of blocking."""

    def __init__(self) -> None:
        self._t = 0.0

    def monotonic(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += seconds


def _cmds(run_mock) -> list[list[str]]:
    return [call.args[0] for call in run_mock.call_args_list]


@mock.patch("platform.system", return_value="Darwin")
@mock.patch("roblox.studio_launcher.subprocess.run")
def test_noop_when_not_running(run_mock, _sys):
    with mock.patch.object(studio_launcher, "is_studio_running", return_value=False):
        close_running_studio_or_fail()
    run_mock.assert_not_called()


@mock.patch("platform.system", return_value="Darwin")
@mock.patch("roblox.studio_launcher.subprocess.run")
def test_graceful_sigterm_no_escalation(run_mock, _sys):
    # Running at the initial check, gone by the first grace poll.
    states = iter([True, False])
    with mock.patch.object(studio_launcher, "time", _FakeClock()), mock.patch.object(
        studio_launcher, "is_studio_running", side_effect=lambda: next(states)
    ):
        close_running_studio_or_fail()
    assert _cmds(run_mock) == [["pkill", "-f", "MacOS/RobloxStudio"]]  # SIGTERM only


@mock.patch("platform.system", return_value="Darwin")
@mock.patch("roblox.studio_launcher.subprocess.run")
def test_escalates_to_sigkill_when_sigterm_trapped(run_mock, _sys):
    # Survives the whole grace window (SIGTERM trapped) and only dies once
    # SIGKILL (the 2nd signal) lands.
    def alive_until_sigkill():
        return len(run_mock.call_args_list) < 2

    with mock.patch.object(studio_launcher, "time", _FakeClock()), mock.patch.object(
        studio_launcher, "is_studio_running", side_effect=alive_until_sigkill
    ):
        close_running_studio_or_fail(timeout=30)
    cmds = _cmds(run_mock)
    assert ["pkill", "-f", "MacOS/RobloxStudio"] in cmds
    assert ["pkill", "-9", "-f", "MacOS/RobloxStudio"] in cmds


@mock.patch("platform.system", return_value="Darwin")
@mock.patch("roblox.studio_launcher.subprocess.run")
def test_raises_when_process_never_dies(run_mock, _sys):
    with mock.patch.object(studio_launcher, "time", _FakeClock()), mock.patch.object(
        studio_launcher, "is_studio_running", return_value=True
    ):
        with pytest.raises(StudioCloseError):
            close_running_studio_or_fail(timeout=2)
    # It still escalated to SIGKILL before giving up.
    assert ["pkill", "-9", "-f", "MacOS/RobloxStudio"] in _cmds(run_mock)


@mock.patch("platform.system", return_value="Windows")
@mock.patch("roblox.studio_launcher.subprocess.run")
def test_windows_uses_taskkill(run_mock, _sys):
    states = iter([True, False])
    with mock.patch.object(studio_launcher, "time", _FakeClock()), mock.patch.object(
        studio_launcher, "is_studio_running", side_effect=lambda: next(states)
    ):
        close_running_studio_or_fail()
    assert _cmds(run_mock) == [["taskkill", "/F", "/IM", "RobloxStudioBeta.exe"]]
