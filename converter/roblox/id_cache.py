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

    On migrated output directories both ``.roblox_ids.json`` and
    ``resolve_ids.json`` may exist — older runs of ``u2r publish`` /
    interactive ``upload`` updated only the legacy file after a retarget,
    while ``resolve_assets`` only ever wrote the canonical one. Returning
    the canonical entry unconditionally would silently route publishes to
    the previous experience, so we pick the file with the most recent
    mtime when both are valid.
    """
    # (mtime, canonical_pref, uid, pid). canonical_pref=1 wins ties so an
    # equal-mtime case (e.g. after zip/unzip normalizes timestamps across
    # files) is broken deterministically by filename, not by numeric IDs.
    candidates: list[tuple[float, int, int, int]] = []
    for name in (CANONICAL, LEGACY):
        path = output_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            mtime = path.stat().st_mtime
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("id_cache: could not read %s: %s", path.name, exc)
            continue
        # Treat any unexpected shape (list, scalar, dict with wrong keys
        # or non-numeric values) the same as missing — the inline readers
        # this helper replaced silently fell through to the next source.
        if not isinstance(data, dict):
            log.warning("id_cache: %s has unexpected shape (%s)", path.name, type(data).__name__)
            continue
        uid = data.get("universe_id")
        pid = data.get("place_id")
        try:
            uid_int = int(uid) if uid else 0
            pid_int = int(pid) if pid else 0
        except (TypeError, ValueError):
            log.warning("id_cache: %s has non-numeric uid/pid", path.name)
            continue
        if uid_int and pid_int:
            canonical_pref = 1 if name == CANONICAL else 0
            candidates.append((mtime, canonical_pref, uid_int, pid_int))

    if not candidates:
        return None, None
    # Higher mtime wins; at equal mtime, canonical (1) beats legacy (0).
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    return candidates[0][2], candidates[0][3]


def write_ids(output_dir: Path, universe_id: int, place_id: int) -> Path:
    """Persist IDs to the canonical cache file. Returns the written path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / CANONICAL
    path.write_text(
        json.dumps({"universe_id": int(universe_id), "place_id": int(place_id)}),
        encoding="utf-8",
    )
    return path
