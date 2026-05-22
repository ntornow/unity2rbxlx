"""
Roblox Studio launcher and process manager.

Provides utilities to launch Roblox Studio, check if it is running, and
wait for it to become ready.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from config import STUDIO_PATH

logger = logging.getLogger(__name__)

# Match the Studio EDITOR process only. A bare ``RobloxStudio`` pattern also
# matches ``.../Contents/MacOS/StudioMCP`` (the proxy that bridges the MCP
# client <-> Studio) and ``.../MacOS/RobloxCrashHandler``, so pgrep/pkill on it
# reported the proxy as "Studio" and — worse — tore the MCP connection down on
# every teardown. The editor binary path is ``.../MacOS/RobloxStudio`` while the
# proxy is ``.../MacOS/StudioMCP``, so this narrower fragment hits the editor
# alone and leaves the MCP proxy alive across close/relaunch.
_EDITOR_PROC_PATTERN = "MacOS/RobloxStudio"


def launch_studio(
    place_id: int | None = None,
    rbxlx_path: str | Path | None = None,
) -> subprocess.Popen | None:
    """Launch Roblox Studio, optionally opening a place.

    Parameters
    ----------
    place_id:
        If provided, Studio opens the given place by ID from Roblox cloud.
    rbxlx_path:
        If provided, Studio opens the given local ``.rbxlx`` file.
        Ignored if *place_id* is also supplied.

    Returns
    -------
    subprocess.Popen | None
        A handle to the Studio process, or ``None`` if launch failed.
    """
    studio_exe = Path(STUDIO_PATH)
    if not studio_exe.exists():
        logger.error("Roblox Studio not found at configured path: %s", studio_exe)
        return None

    cmd: list[str] = [str(studio_exe)]

    if place_id is not None:
        cmd.extend(["-placeId", str(place_id)])
    elif rbxlx_path is not None:
        resolved = Path(rbxlx_path).resolve()
        if not resolved.exists():
            logger.warning("Place file does not exist: %s", resolved)
        cmd.append(str(resolved))

    logger.info("Launching Roblox Studio: %s", " ".join(cmd))

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        logger.info("Roblox Studio launched (PID %d)", process.pid)
        return process
    except FileNotFoundError:
        logger.error("Could not find Studio executable: %s", studio_exe)
        return None
    except PermissionError:
        logger.error("Permission denied launching Studio: %s", studio_exe)
        return None
    except OSError as exc:
        logger.error("OS error launching Studio: %s", exc)
        return None


def is_studio_running() -> bool:
    """Check whether a Roblox Studio process is currently running.

    Uses ``tasklist`` on Windows and ``pgrep`` on other platforms.
    """
    import platform

    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq RobloxStudioBeta.exe", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return "RobloxStudioBeta.exe" in result.stdout
        else:
            result = subprocess.run(
                ["pgrep", "-f", _EDITOR_PROC_PATTERN],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("Could not determine if Studio is running")
        return False


def wait_for_studio_ready(timeout: float = 60) -> bool:
    """Block until Roblox Studio appears to be running, or until *timeout* seconds elapse.

    This polls ``is_studio_running()`` at 2-second intervals.

    Parameters
    ----------
    timeout:
        Maximum seconds to wait.

    Returns
    -------
    bool
        ``True`` if Studio was detected within the timeout window.
    """
    deadline = time.monotonic() + timeout
    poll_interval = 2.0

    logger.info("Waiting up to %.0fs for Roblox Studio to become ready...", timeout)

    while time.monotonic() < deadline:
        if is_studio_running():
            logger.info("Roblox Studio is running.")
            return True
        time.sleep(poll_interval)

    logger.warning("Timed out waiting for Roblox Studio after %.0fs", timeout)
    return False


class StudioCloseError(RuntimeError):
    """Raised when ``close_running_studio_or_fail`` could not terminate
    every running Studio process within the allotted timeout."""


def close_running_studio_or_fail(timeout: float = 30) -> None:
    """Force-close any running Roblox Studio instance.

    Used by the ``/e2e-test`` skill's ``--close-and-relaunch`` flag
    (Codex finding #1: re-opening a regenerated rbxlx in an already-running
    Studio doesn't actually reload the DataModel; the only honest signal
    that a fresh rbxlx is loaded is a fresh Studio process). Defaults to
    a no-op if Studio isn't running.

    Parameters
    ----------
    timeout:
        Max seconds to wait for the process to actually exit after the
        kill signal. Raises ``StudioCloseError`` on timeout so the
        caller can surface a real failure instead of silently
        continuing with a stale Studio.
    """
    import platform

    if not is_studio_running():
        logger.info("close_running_studio_or_fail: no Studio process detected")
        return

    system = platform.system()

    def _send(args: list[str]) -> None:
        try:
            subprocess.run(args, capture_output=True, text=True, timeout=10)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise StudioCloseError(f"close signal failed: {exc}") from exc

    # One overall budget. The SIGTERM grace and the post-kill wait are both
    # carved out of this single deadline (not charged the full timeout twice).
    deadline = time.monotonic() + timeout

    if system == "Windows":
        # ``taskkill /F`` is already a forceful terminate.
        _send(["taskkill", "/F", "/IM", "RobloxStudioBeta.exe"])
    else:
        # ``pkill -f`` matches the same editor pattern ``pgrep -f`` finds —
        # crucially NOT the StudioMCP proxy (see ``_EDITOR_PROC_PATTERN``), so
        # closing Studio no longer severs the MCP connection. Send SIGTERM
        # first (graceful), but Studio traps SIGTERM to raise a "save
        # changes?" dialog and stays alive — so if it survives a short
        # grace period, escalate to SIGKILL, which a dialog cannot catch.
        _send(["pkill", "-f", _EDITOR_PROC_PATTERN])
        grace_deadline = min(time.monotonic() + 5.0, deadline)
        while time.monotonic() < grace_deadline:
            if not is_studio_running():
                logger.info("close_running_studio_or_fail: Studio exited on SIGTERM")
                return
            time.sleep(0.5)
        logger.warning(
            "Studio survived SIGTERM (likely a save dialog); escalating to SIGKILL"
        )
        _send(["pkill", "-9", "-f", _EDITOR_PROC_PATTERN])

    # Wait for the process to actually leave the table — kill is
    # asynchronous on macOS especially.
    while time.monotonic() < deadline:
        if not is_studio_running():
            logger.info("close_running_studio_or_fail: Studio process exited")
            return
        time.sleep(0.5)

    raise StudioCloseError(
        f"Studio still running after {timeout:.0f}s — terminate manually and retry"
    )
