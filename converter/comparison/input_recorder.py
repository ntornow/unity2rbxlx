"""
Record and persist input event sequences.

Input events are stored as a JSON array of timestamped actions (key presses,
mouse movements, clicks) that can later be replayed in either Unity or Roblox.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class InputEvent:
    """A single recorded input event."""

    timestamp: float
    """Seconds since the start of the recording."""

    event_type: str
    """One of ``key_down``, ``key_up``, ``mouse_move``, ``mouse_click``."""

    key: str
    """Key or button identifier (e.g. ``W``, ``Mouse1``)."""

    position: Optional[Tuple[float, float]] = None
    """Screen-space ``(x, y)`` for mouse events, or ``None`` for key events."""


@dataclass
class InputSequence:
    """An ordered sequence of input events with a total duration."""

    events: List[InputEvent] = field(default_factory=list)
    """Chronologically ordered list of :class:`InputEvent` instances."""

    duration: float = 0.0
    """Total duration of the recording in seconds."""


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _event_to_dict(event: InputEvent) -> dict:
    """Convert an :class:`InputEvent` to a JSON-friendly dict."""
    d: dict = {
        "timestamp": event.timestamp,
        "type": event.event_type,
        "key": event.key,
    }
    if event.position is not None:
        d["position"] = list(event.position)
    return d


def _dict_to_event(d: dict) -> InputEvent:
    """Reconstruct an :class:`InputEvent` from a dict."""
    pos = d.get("position")
    if pos is not None:
        pos = (float(pos[0]), float(pos[1]))
    return InputEvent(
        timestamp=float(d["timestamp"]),
        event_type=d["type"],
        key=d.get("key", ""),
        position=pos,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def record_from_file(file_path: str | Path) -> InputSequence:
    """Load a recorded input sequence from a JSON file.

    The expected format is a JSON array of objects, each containing at least
    ``timestamp``, ``type``, and ``key`` fields.  An optional ``position``
    field (``[x, y]``) is used for mouse events.

    Args:
        file_path: Path to the JSON file.

    Returns:
        An :class:`InputSequence` populated from the file.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    file_path = Path(file_path)
    raw = json.loads(file_path.read_text(encoding="utf-8"))

    if not isinstance(raw, list):
        raise ValueError(f"Expected a JSON array, got {type(raw).__name__}")

    events = [_dict_to_event(entry) for entry in raw]
    events.sort(key=lambda e: e.timestamp)

    duration = events[-1].timestamp if events else 0.0

    logger.info("Loaded %d input events (%.2fs) from %s", len(events), duration, file_path)
    return InputSequence(events=events, duration=duration)


def save_to_file(sequence: InputSequence, file_path: str | Path) -> None:
    """Persist an :class:`InputSequence` to a JSON file.

    Args:
        sequence: The input sequence to save.
        file_path: Destination path for the JSON file.
    """
    file_path = Path(file_path)
    file_path.parent.mkdir(parents=True, exist_ok=True)

    data = [_event_to_dict(e) for e in sequence.events]
    file_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    logger.info("Saved %d input events to %s", len(sequence.events), file_path)
