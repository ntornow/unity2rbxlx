"""Regenerate the vendored Roblox callable-member corpus from the official
Roblox API dump.

The corpus is a flat union of every CALLABLE (method) name exposed by any
Roblox class. Downstream code uses it to tell a real Roblox method from a
hallucinated one (e.g. ``FindFirstChildOfType`` does not exist).

Source of truth (two HTTP GETs):
  1. ``https://setup.rbxcdn.com/versionQTStudio`` -> a version string like
     ``version-XXXX``.
  2. ``https://setup.rbxcdn.com/<version>-API-Dump.json`` -> the dump.

Filter predicate (EMPIRICALLY VERIFIED against version-d0e8cfcd943d4ae2 — do
NOT change): keep members whose ``MemberType == "Function"``. In this dump,
instance methods are tagged ``Function``; the literal ``Method`` yields ZERO.
Properties are ``Property``, events ``Event``, callbacks ``Callback``.

Output: ``converter/converter/data/roblox_callable_members.json`` with shape
    {
      "apiDumpVersion": "<version>",
      "generatedFrom": "setup.rbxcdn.com API-Dump.json",
      "callableMembers": ["...sorted unique..."]
    }

The tool is idempotent and prints an add/removed diff vs the existing snapshot.

Usage:
    python3 tools/refresh_roblox_corpus.py

Offline fallback: if the live fetch fails, the tool falls back to a cached dump
at the WORKTREE ROOT (``.cached_api_dump.json``), logging which source was used.
That cache file is a developer convenience and is NOT a committed deliverable.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

VERSION_URL = "https://setup.rbxcdn.com/versionQTStudio"
DUMP_URL_TEMPLATE = "https://setup.rbxcdn.com/{version}-API-Dump.json"
GENERATED_FROM = "setup.rbxcdn.com API-Dump.json"

# converter/tools/refresh_roblox_corpus.py -> repo root is parents[2].
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SNAPSHOT_PATH = (
    Path(__file__).resolve().parent.parent
    / "converter"
    / "data"
    / "roblox_callable_members.json"
)
_CACHED_DUMP_PATH = _REPO_ROOT / ".cached_api_dump.json"

# A class member with MemberType == this is a callable (method).
_CALLABLE_MEMBER_TYPE = "Function"


class CorpusFetchError(RuntimeError):
    """Neither the live fetch nor the cached fallback produced a usable dump."""


def _http_get(url: str, *, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "unity2rbxlx-corpus-refresh"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https hosts)
        return response.read()


def _fetch_live() -> tuple[str, dict[str, object]]:
    """Resolve the Studio version, then fetch and parse that version's dump.

    Returns (version, parsed_dump). Raises urllib/JSON errors on failure so the
    caller can decide whether to fall back.
    """
    version = _http_get(VERSION_URL).decode("utf-8").strip()
    dump_bytes = _http_get(DUMP_URL_TEMPLATE.format(version=version))
    dump = json.loads(dump_bytes.decode("utf-8"))
    if not isinstance(dump, dict):
        raise CorpusFetchError("live API dump did not parse to a JSON object")
    return version, dump


def _load_cached_dump() -> tuple[str, dict[str, object]]:
    """Load the worktree-root cached dump as an offline fallback.

    The cached dump carries the version under its top-level ``Version`` key when
    present; otherwise we tag it ``cached-unknown`` so the snapshot still records
    a non-empty version string.
    """
    if not _CACHED_DUMP_PATH.exists():
        raise CorpusFetchError(
            f"no cached dump at {_CACHED_DUMP_PATH}; live fetch was the only source and it failed"
        )
    dump = json.loads(_CACHED_DUMP_PATH.read_text(encoding="utf-8"))
    if not isinstance(dump, dict):
        raise CorpusFetchError("cached API dump did not parse to a JSON object")
    raw_version = dump.get("Version")
    version = str(raw_version) if isinstance(raw_version, (str, int)) else "cached-unknown"
    return version, dump


def extract_callable_members(dump: dict[str, object]) -> list[str]:
    """Flat, deduped, sorted union of every callable member name in the dump.

    Pure: derives the result solely from ``dump`` (no I/O, no mutation).
    """
    classes = dump.get("Classes")
    if not isinstance(classes, list):
        raise CorpusFetchError("API dump has no 'Classes' list")
    names: set[str] = set()
    for cls in classes:
        if not isinstance(cls, dict):
            continue
        members = cls.get("Members")
        if not isinstance(members, list):
            continue
        for member in members:
            if not isinstance(member, dict):
                continue
            if member.get("MemberType") != _CALLABLE_MEMBER_TYPE:
                continue
            name = member.get("Name")
            if isinstance(name, str) and name:
                names.add(name)
    return sorted(names)


def _read_existing_members() -> list[str]:
    if not _SNAPSHOT_PATH.exists():
        return []
    existing = json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    members = existing.get("callableMembers") if isinstance(existing, dict) else None
    if isinstance(members, list):
        return [m for m in members if isinstance(m, str)]
    return []


def _print_diff(old: list[str], new: list[str]) -> None:
    old_set, new_set = set(old), set(new)
    added = sorted(new_set - old_set)
    removed = sorted(old_set - new_set)
    if not added and not removed:
        print("diff vs existing snapshot: no changes")
        return
    print(f"diff vs existing snapshot: +{len(added)} added, -{len(removed)} removed")
    for name in added:
        print(f"  + {name}")
    for name in removed:
        print(f"  - {name}")


def refresh() -> Path:
    """Fetch the dump (live, else cached), write the snapshot, print a diff.

    Returns the snapshot path written.
    """
    try:
        version, dump = _fetch_live()
        print(f"source: live fetch (version {version})")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as live_err:
        print(f"live fetch failed ({live_err!r}); falling back to cached dump", file=sys.stderr)
        version, dump = _load_cached_dump()
        print(f"source: cached dump at {_CACHED_DUMP_PATH} (version {version})")

    members = extract_callable_members(dump)
    print(f"extracted {len(members)} unique callable members")

    old_members = _read_existing_members()
    _print_diff(old_members, members)

    snapshot: dict[str, object] = {
        "apiDumpVersion": version,
        "generatedFrom": GENERATED_FROM,
        "callableMembers": members,
    }
    _SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SNAPSHOT_PATH.write_text(
        json.dumps(snapshot, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    print(f"wrote {_SNAPSHOT_PATH}")
    return _SNAPSHOT_PATH


def main() -> int:
    try:
        refresh()
    except CorpusFetchError as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
