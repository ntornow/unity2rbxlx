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
                ["pgrep", "-f", "RobloxStudio"],
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
