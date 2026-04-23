"""
Smoke test: open a converted .rbxlx in Roblox Studio, enter Play mode,
capture script output and errors from Studio logs, take a screenshot,
and produce a pass/fail report.

Requires:
- macOS with Roblox Studio installed and logged in
- A converted .rbxlx place file

Usage via CLI:
    python u2r.py smoke-test path/to/converted_place.rbxlx

Or programmatically:
    from smoke_test import run_smoke_test
    report = run_smoke_test("path/to/converted_place.rbxlx")
"""

from __future__ import annotations

import json
import logging
import platform
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from roblox.health_check_injector import inject_health_check, HEALTH_CHECK_SCRIPT_NAME
from roblox.studio_log_parser import (
    StudioLogResult,
    find_new_log,
    get_studio_log_files,
    parse_log,
    wait_for_marker,
    wait_for_place_loaded,
)

logger = logging.getLogger(__name__)

STUDIO_LOAD_WAIT = 20  # seconds to wait after Studio opens before entering Play
PLAY_MODE_SETTLE = 30  # seconds to wait after entering Play mode for scripts to run
SMOKE_TEST_TIMEOUT = 180  # max seconds to wait for [SMOKE_TEST_DONE] marker

STUDIO_AUTOSAVES_DIR = Path.home() / "Library" / "Application Support" / "Roblox" / "RobloxStudio" / "AutoSaves"


@dataclass
class SmokeTestReport:
    """Results of a smoke test run."""
    rbxlx_path: str = ""
    status: str = "unknown"  # pass, fail, error, timeout
    studio_opened: bool = False
    play_mode_entered: bool = False
    health_check_started: bool = False
    health_check_done: bool = False
    health_check_result: dict | None = None
    script_errors: list[str] = field(default_factory=list)
    studio_errors: list[str] = field(default_factory=list)
    studio_crashed: bool = False
    screenshot_path: str | None = None
    log_path: str | None = None
    duration_seconds: float = 0
    error_message: str | None = None


def run_smoke_test(
    rbxlx_path: str | Path,
    timeout: float = SMOKE_TEST_TIMEOUT,
    screenshot: bool = True,
    output_dir: str | Path | None = None,
    keep_injected: bool = False,
) -> SmokeTestReport:
    """Run a full smoke test on a converted .rbxlx place file.

    1. Inject health-check Script into the rbxlx
    2. Open Studio with the modified place
    3. Wait for Studio to load the place
    4. Enter Play mode via osascript
    5. Wait for the health-check script to print results to the Studio log
    6. Take a screenshot
    7. Kill Studio
    8. Parse logs and generate report
    """
    rbxlx_path = Path(rbxlx_path).resolve()
    report = SmokeTestReport(rbxlx_path=str(rbxlx_path))
    t0 = time.monotonic()

    if output_dir is None:
        output_dir = rbxlx_path.parent / "smoke_test"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not rbxlx_path.exists():
        report.status = "error"
        report.error_message = f"File not found: {rbxlx_path}"
        return report

    if platform.system() != "Darwin":
        report.status = "error"
        report.error_message = "Smoke test requires macOS with Roblox Studio"
        return report

    # Step 1: Inject health check
    logger.info("Injecting health-check script into %s", rbxlx_path)
    injected_path = output_dir / "place_smoketest.rbxlx"
    try:
        inject_health_check(rbxlx_path, injected_path)
    except Exception as exc:
        report.status = "error"
        report.error_message = f"Failed to inject health check: {exc}"
        return report

    # Step 2: Record existing logs so we can find the new one
    pre_logs = set(get_studio_log_files())

    # Step 3: Kill any existing Studio instances (waits for exit)
    _kill_studio()

    # Step 3b: Clear auto-recovery files + stale lock so Studio doesn't show
    # the "An auto-recovery file was found" dialog.
    _clear_autosaves_and_locks(injected_path)

    # Step 4: Open Studio with the injected place
    logger.info("Opening Roblox Studio with %s", injected_path)
    opened = _open_studio(injected_path)
    if not opened:
        report.status = "error"
        report.error_message = "Failed to open Roblox Studio"
        _cleanup(injected_path, keep_injected)
        return report
    report.studio_opened = True

    # Step 5: Wait for Studio to load the place
    logger.info("Waiting %ds for Studio to load the place...", STUDIO_LOAD_WAIT)
    time.sleep(STUDIO_LOAD_WAIT)

    # Step 6: Find the new Studio log
    log_path = find_new_log(pre_logs, timeout=30)
    if log_path:
        report.log_path = str(log_path)
        logger.info("Found Studio log: %s", log_path)
    else:
        logger.warning("Could not find new Studio log file")

    # Step 7: Wait for Studio to actually open the place (not just sit on Start Page)
    if log_path:
        logger.info("Waiting for Studio to finish opening the place...")
        loaded = wait_for_place_loaded(log_path, timeout=60)
        if not loaded:
            logger.warning(
                "Studio did not reach EditDataModel — Start Page likely still showing; "
                "Play mode will probably be a no-op"
            )

    # Step 8: Enter Play mode
    logger.info("Entering Play mode via osascript...")
    play_ok = _enter_play_mode()
    report.play_mode_entered = play_ok
    if not play_ok:
        logger.warning("osascript Play mode failed — continuing anyway")

    # Step 9: Wait for smoke test completion
    if log_path:
        logger.info("Waiting up to %ds for smoke test results...", timeout)
        found = wait_for_marker(log_path, "[SMOKE_TEST_DONE]", timeout=timeout)
        if not found:
            logger.warning("Timed out waiting for [SMOKE_TEST_DONE]")
    else:
        logger.info("No log path — waiting %ds blindly", PLAY_MODE_SETTLE)
        time.sleep(PLAY_MODE_SETTLE)

    # Step 10: Take screenshot
    if screenshot:
        ss_path = output_dir / "studio_screenshot.png"
        logger.info("Taking screenshot...")
        took = _take_screenshot(ss_path)
        if took:
            report.screenshot_path = str(ss_path)

    # Step 11: Parse logs
    if log_path:
        log_result = parse_log(log_path)
        report.health_check_started = log_result.smoke_test_started
        report.health_check_done = log_result.smoke_test_done
        report.health_check_result = log_result.smoke_test_result
        report.script_errors = log_result.smoke_test_errors
        report.studio_errors = log_result.flog_errors
        report.studio_crashed = log_result.studio_crashed

    # Step 12: Kill Studio
    logger.info("Stopping Studio...")
    _kill_studio()

    # Step 13: Determine status
    report.duration_seconds = round(time.monotonic() - t0, 1)

    if report.studio_crashed:
        report.status = "fail"
        report.error_message = "Studio crashed"
    elif not report.health_check_done:
        report.status = "timeout"
        report.error_message = "Health check did not complete"
    elif report.health_check_result:
        error_count = report.health_check_result.get("scriptErrorCount", 0)
        if error_count == 0:
            report.status = "pass"
        else:
            report.status = "fail"
            report.error_message = f"{error_count} script error(s) during play"
    else:
        report.status = "fail"
        report.error_message = "Health check completed but no result data"

    # Save report
    report_path = output_dir / "smoke_test_report.json"
    report_path.write_text(json.dumps(asdict(report), indent=2))
    logger.info("Report saved: %s", report_path)

    _cleanup(injected_path, keep_injected)

    return report


STUDIO_BINARY = Path("/Applications/RobloxStudio.app/Contents/MacOS/RobloxStudio")


def _open_studio(rbxlx_path: Path) -> bool:
    """Open Roblox Studio with a .rbxlx file.

    Invokes the Studio binary directly with the path as a positional arg.
    This bypasses macOS ``open -a``, which routes through Studio's URL
    protocol handler (``-protocolString file:// -protocolHandlerLaunch``)
    and opens Studio on the Start Page instead of the target place.
    """
    if not STUDIO_BINARY.exists():
        logger.error("Roblox Studio binary not found at %s", STUDIO_BINARY)
        return False
    try:
        subprocess.Popen(
            [str(STUDIO_BINARY), str(rbxlx_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        return True
    except Exception as exc:
        logger.error("Failed to open Studio: %s", exc)
        return False


def _enter_play_mode() -> bool:
    """Send F5 keystroke to Roblox Studio via osascript to enter Play mode.

    Blocks until Studio is actually the frontmost app before sending the
    keystroke — otherwise 'tell ... to activate' can race with whatever
    terminal/editor was focused and the F5 goes to the wrong window.

    Also sends Escape (cancel auto-recovery dialog → don't load old version)
    and Return (click default "Continue" on the Lighting Technology Migration
    dialog) to clear any modal dialogs that would otherwise eat the F5.
    """
    script = '''
    tell application "RobloxStudio" to activate
    tell application "System Events"
        repeat 80 times
            if (exists process "RobloxStudio") then
                if frontmost of process "RobloxStudio" then exit repeat
            end if
            delay 0.25
        end repeat
        delay 0.5
        tell process "RobloxStudio"
            -- Dismiss common startup dialogs (auto-recovery, lighting migration)
            key code 53
            delay 0.3
            key code 36
            delay 0.3
            key code 53
            delay 0.3
            -- F5 = Play
            key code 96
        end tell
    end tell
    '''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning("osascript stderr: %s", result.stderr.strip())
        return result.returncode == 0
    except Exception as exc:
        logger.error("osascript failed: %s", exc)
        return False


def _clear_autosaves_and_locks(injected_path: Path) -> None:
    """Remove stale auto-recovery files and Studio file locks.

    Studio shows a modal 'auto-recovery' dialog on startup if it finds *any*
    recovery file in the AutoSaves directory — not just one matching the
    place we're opening. That dialog blocks every keystroke including F5,
    leaving the smoke test waiting for a Play mode that never starts.

    Globbing only the injected place's stem misses recovery files from
    prior unrelated Studio sessions (e.g. ``Place_AutoRecovery_*.rbxl``
    from a default-named place that was killed). Sweep them all — Studio
    creates them as crash insurance, and any session reaching this point
    is being killed and restarted from a freshly injected rbxlx anyway.
    """
    if STUDIO_AUTOSAVES_DIR.exists():
        for f in STUDIO_AUTOSAVES_DIR.glob("*_AutoRecovery_*.rbxl"):
            try:
                f.unlink()
                logger.info("Removed stale auto-recovery file: %s", f.name)
            except OSError:
                pass
    lock_path = injected_path.with_suffix(injected_path.suffix + ".lock")
    if lock_path.exists():
        try:
            lock_path.unlink()
            logger.info("Removed stale lock file: %s", lock_path)
        except OSError:
            pass


def _take_screenshot(output_path: Path) -> bool:
    """Take a screenshot of the entire screen via macOS screencapture."""
    try:
        result = subprocess.run(
            ["screencapture", "-x", str(output_path)],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0 and output_path.exists()
    except Exception as exc:
        logger.error("Screenshot failed: %s", exc)
        return False


def _kill_studio(wait_seconds: float = 10) -> None:
    """Kill all Roblox Studio processes and wait for them to exit.

    Studio ignores SIGTERM in some states — send SIGKILL. Match on the full
    ``RobloxStudio.app`` path (not just ``RobloxStudio``) so we don't match
    our own shell command line.
    """
    pattern = "RobloxStudio.app/Contents/MacOS/RobloxStudio"
    try:
        subprocess.run(
            ["pkill", "-KILL", "-f", pattern],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        return

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:  # no matches
            return
        time.sleep(0.5)
    logger.warning("Studio processes still running after %.0fs", wait_seconds)


def _cleanup(injected_path: Path, keep: bool) -> None:
    """Remove the injected rbxlx if not keeping it."""
    if not keep and injected_path.exists():
        injected_path.unlink()


def format_report(report: SmokeTestReport) -> str:
    """Format a smoke test report as human-readable text."""
    lines = []
    status_icon = {"pass": "PASS", "fail": "FAIL", "error": "ERROR", "timeout": "TIMEOUT", "unknown": "???"}
    lines.append(f"Smoke Test: {status_icon.get(report.status, report.status)}")
    lines.append(f"  Place: {report.rbxlx_path}")
    lines.append(f"  Duration: {report.duration_seconds}s")
    lines.append(f"  Studio opened: {report.studio_opened}")
    lines.append(f"  Play mode: {report.play_mode_entered}")
    lines.append(f"  Health check: started={report.health_check_started} done={report.health_check_done}")

    if report.health_check_result:
        r = report.health_check_result
        lines.append(f"  Instances: {r.get('totalInstances', '?')}")
        lines.append(f"  Parts: {r.get('parts', '?')} (mesh: {r.get('meshParts', '?')})")
        lines.append(f"  Scripts: {r.get('scripts', '?')}")
        lines.append(f"  Script errors: {r.get('scriptErrorCount', '?')}")

    if report.script_errors:
        lines.append(f"  Top script errors ({len(report.script_errors)}):")
        for err in report.script_errors[:10]:
            lines.append(f"    - {err}")

    if report.studio_errors:
        lines.append(f"  Studio errors ({len(report.studio_errors)}):")
        for err in report.studio_errors[:5]:
            lines.append(f"    - {err}")

    if report.error_message:
        lines.append(f"  Error: {report.error_message}")

    if report.screenshot_path:
        lines.append(f"  Screenshot: {report.screenshot_path}")

    if report.log_path:
        lines.append(f"  Log: {report.log_path}")

    return "\n".join(lines)
