"""
Parse Roblox Studio log files to extract script output and errors.

Studio writes versioned logs to ~/Library/Logs/Roblox/ on macOS.
Luau print() output appears as [FLog::Output] entries.
Script and engine errors appear as [FLog::Error] entries.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path


LOG_DIR = Path.home() / "Library" / "Logs" / "Roblox"

_TIMESTAMP_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T[\d:.]+Z),[\d.]+,\w+,\d+"
)
_FLOG_OUTPUT_RE = re.compile(
    r"\[FLog::Output\]\s*(.*)"
)
_FLOG_ERROR_RE = re.compile(
    r",(?:Error|Critical)\s+\[FLog::\w+\]\s*(.*)"
)
_SMOKE_RESULT_RE = re.compile(
    r"\[SMOKE_TEST_RESULT\]\s*(.*)"
)
_SMOKE_ERROR_RE = re.compile(
    r"\[SMOKE_TEST_ERROR\]\s*(.*)"
)
_SMOKE_CLIENT_RESULT_RE = re.compile(
    r"\[SMOKE_TEST_CLIENT_RESULT\]\s*(.*)"
)


@dataclass
class StudioLogResult:
    """Parsed results from a Studio log session."""
    log_path: Path | None = None
    smoke_test_started: bool = False
    smoke_test_done: bool = False
    smoke_test_result: dict | None = None
    smoke_test_errors: list[str] = field(default_factory=list)
    client_result: dict | None = None
    input_window_opened: bool = False
    flog_errors: list[str] = field(default_factory=list)
    flog_output_lines: list[str] = field(default_factory=list)
    studio_crashed: bool = False


def get_studio_log_files(log_dir: Path | None = None) -> list[Path]:
    """Return all versioned Studio log files sorted by mtime (newest first)."""
    d = log_dir or LOG_DIR
    if not d.exists():
        return []
    return sorted(
        d.glob("0.*Studio*_last.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def find_new_log(before_logs: set[Path], log_dir: Path | None = None, timeout: float = 120) -> Path | None:
    """Wait for a new Studio log file to appear that wasn't in before_logs.

    Polls every 2 seconds up to timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = get_studio_log_files(log_dir)
        for log_path in current:
            if log_path not in before_logs:
                return log_path
        time.sleep(2)
    return None


def wait_for_marker(
    log_path: Path,
    marker: str,
    timeout: float = 180,
    poll_interval: float = 3,
) -> bool:
    """Poll a log file until a line containing marker appears, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(errors="replace")
            if marker in text:
                return True
        time.sleep(poll_interval)
    return False


def wait_for_place_loaded(
    log_path: Path,
    timeout: float = 60,
    poll_interval: float = 2,
) -> bool:
    """Poll until Studio finishes opening a place file (vs. Start Page only).

    Looks for ``StartSoloSession`` registrations, ``EditDataModel`` creation,
    or the first ``[FLog::Output]`` from a user script — any of which indicate
    the DataModel has been populated with the place.
    """
    markers = [
        "OpenPlaceSuccess",
        "open place (identifier",
        "saveDataModelToLocalFile succeeded",
        "auto-recovery file was created",
        "PlaceOpenFailed",
    ]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.exists():
            text = log_path.read_text(errors="replace")
            for m in markers:
                if m in text:
                    return True
        time.sleep(poll_interval)
    return False


def parse_log(log_path: Path) -> StudioLogResult:
    """Parse a Studio log file and extract smoke test results and errors."""
    result = StudioLogResult(log_path=log_path)

    if not log_path.exists():
        return result

    text = log_path.read_text(errors="replace")

    for line in text.splitlines():
        # Extract FLog::Output lines (Luau print output)
        m = _FLOG_OUTPUT_RE.search(line)
        if m:
            output_text = m.group(1).strip()
            result.flog_output_lines.append(output_text)

            if "[SMOKE_TEST_START]" in output_text:
                result.smoke_test_started = True

            if "[SMOKE_TEST_DONE]" in output_text:
                result.smoke_test_done = True

            if "[SMOKE_TEST_INPUT_WINDOW_OPEN]" in output_text:
                result.input_window_opened = True

            sm = _SMOKE_RESULT_RE.search(output_text)
            if sm:
                try:
                    result.smoke_test_result = json.loads(sm.group(1))
                except json.JSONDecodeError:
                    pass

            cm = _SMOKE_CLIENT_RESULT_RE.search(output_text)
            if cm:
                try:
                    result.client_result = json.loads(cm.group(1))
                except json.JSONDecodeError:
                    pass

            em = _SMOKE_ERROR_RE.search(output_text)
            if em:
                result.smoke_test_errors.append(em.group(1))

        # Extract error lines
        em = _FLOG_ERROR_RE.search(line)
        if em:
            error_text = em.group(1).strip()
            # Skip known benign Studio errors
            if _is_benign_error(error_text):
                continue
            result.flog_errors.append(error_text)

    # Detect crash indicators
    if "SIGSEGV" in text or "SIGABRT" in text or "fatal error" in text.lower():
        result.studio_crashed = True

    return result


def _is_benign_error(error_text: str) -> bool:
    """Filter out known benign Studio errors that aren't script-related."""
    benign_patterns = [
        "Redundant Flag ID:",
        "version fetch failed",
        "interruptWithApplicationUpdateIfAvailable",
        "HTTP 401 (Unauthorized)",
        "Place does not exist",
        "Potential graphics modes:",
        "ribbon layout file does not exist",
        "gridSizeToFourAction",
        "Failed to parse local secrets",
        "ModerationController failed",
        "Failed to load sound",
    ]
    return any(p in error_text for p in benign_patterns)
