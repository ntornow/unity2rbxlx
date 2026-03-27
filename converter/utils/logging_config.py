"""
Structured logging configuration for the Unity-to-Roblox converter.

Sets up the root logger with a consistent format and optionally adds a file
handler.  Third-party library loggers are quieted to WARNING level.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

_LOG_FORMAT = "[%(asctime)s %(levelname)s %(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that should be quieted
_NOISY_LOGGERS = (
    "urllib3",
    "PIL",
    "PIL.PngImagePlugin",
    "PIL.Image",
    "httpx",
    "httpcore",
    "anthropic",
)


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str | Path] = None,
) -> None:
    """Configure the root logger for the converter.

    Args:
        level: Log level name (e.g. ``"DEBUG"``, ``"INFO"``, ``"WARNING"``).
        log_file: Optional path to a log file.  If provided, a
            :class:`~logging.FileHandler` is added in addition to the
            default ``stderr`` stream handler.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Remove any existing handlers to avoid duplicate output on repeated calls
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # Console handler (stderr)
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(numeric_level)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    # Optional file handler
    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
        file_handler.setLevel(numeric_level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Quiet noisy third-party loggers
    for logger_name in _NOISY_LOGGERS:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
