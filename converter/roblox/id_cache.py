"""
id_cache.py -- Universe/place ID persistence for headless publish.

Single source of truth for the uid/pid cache. Three callers used to maintain
divergent cache filenames (``.roblox_ids.json`` vs ``resolve_ids.json``);
this module collapses that into one read/write contract:

* canonical filename: ``.roblox_ids.json`` — what ``pipeline.resolve_assets``
  already writes when it auto-resolves IDs
* legacy fallback on read: ``resolve_ids.json`` — older runs of
  ``u2r.py publish`` and ``convert_interactive.py upload`` wrote here
* always write to the canonical filename, never the legacy one
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

CANONICAL = ".roblox_ids.json"
LEGACY = "resolve_ids.json"


def read_ids(output_dir: Path) -> tuple[int | None, int | None]:
    """Return (universe_id, place_id) from the cache, or (None, None) if absent.

    Tries the canonical file first; falls back to the legacy filename so older
    output directories keep working until the next successful publish rewrites
    the cache to the canonical name.
    """
    for name in (CANONICAL, LEGACY):
        path = output_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("id_cache: could not read %s: %s", path.name, exc)
            continue
        uid = data.get("universe_id")
        pid = data.get("place_id")
        if uid and pid:
            return int(uid), int(pid)
    return None, None


def write_ids(output_dir: Path, universe_id: int, place_id: int) -> Path:
    """Persist IDs to the canonical cache file. Returns the written path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CANONICAL
    path.write_text(
        json.dumps({"universe_id": int(universe_id), "place_id": int(place_id)}),
        encoding="utf-8",
    )
    return path
