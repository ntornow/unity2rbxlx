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
      "callableMembers": ["...sorted unique..."],
      "signatures": {"<name>": "<name>(p: T, ...): ReturnT", ...}
    }

``signatures`` maps each callable name to a single human-readable signature
string. When a name appears on multiple classes with differing signatures, the
first one (by ascending class name) is kept as a representative.

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

# RBXScriptSignal callable members. These belong to the RBXScriptSignal DATA
# TYPE, which the API dump exposes outside the ``Classes`` array (the section
# this tool reads), so the MemberType=="Function" filter never captures them.
# They are stable, well-documented signal methods; vendored here so a valid
# event connection (``workspace.DescendantAdded:Connect(...)``) is not a false
# positive. (Disconnect/Fire already arrive via Classes e.g. BindableEvent.)
_RBXSCRIPTSIGNAL_MEMBERS: tuple[str, ...] = (
    "Connect", "Once", "Wait", "ConnectParallel",
)
_RBXSCRIPTSIGNAL_SIGNATURES: dict[str, str] = {
    "Connect": "Connect(handler: function): RBXScriptConnection",
    "Once": "Once(handler: function): RBXScriptConnection",
    "Wait": "Wait(): Tuple",
    "ConnectParallel": "ConnectParallel(handler: function): RBXScriptConnection",
}


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


def _format_type(type_obj: object) -> str:
    """Human-readable type name from a dump ``Type``/``ReturnType`` object.

    The dump tags void returns with ``Name == "null"``; surface that as ``void``.
    """
    if isinstance(type_obj, dict):
        name = type_obj.get("Name")
        if isinstance(name, str) and name:
            return "void" if name == "null" else name
    return "unknown"


def _format_signature(member: dict[str, object]) -> str:
    """Build ``name(p1: T1, p2: T2): ReturnT`` from a Function member.

    Pure: derives the string solely from ``member``.
    """
    name = member.get("Name")
    name_str = name if isinstance(name, str) else "unknown"

    params: list[str] = []
    raw_params = member.get("Parameters")
    if isinstance(raw_params, list):
        for param in raw_params:
            if not isinstance(param, dict):
                continue
            pname = param.get("Name")
            pname_str = pname if isinstance(pname, str) and pname else "_"
            params.append(f"{pname_str}: {_format_type(param.get('Type'))}")

    return_t = _format_type(member.get("ReturnType"))
    return f"{name_str}({', '.join(params)}): {return_t}"


def extract_signatures(dump: dict[str, object]) -> dict[str, str]:
    """Map each callable name to one representative human-readable signature.

    When a name appears on multiple classes with differing signatures, the
    first by ascending class name wins. Pure: derives the result solely from
    ``dump`` (no I/O, no mutation).
    """
    classes = dump.get("Classes")
    if not isinstance(classes, list):
        raise CorpusFetchError("API dump has no 'Classes' list")

    # (class_name, member_name) ordering makes the representative deterministic.
    seen: dict[str, str] = {}
    ordered: list[tuple[str, str, dict[str, object]]] = []
    for cls in classes:
        if not isinstance(cls, dict):
            continue
        cls_name = cls.get("Name")
        cls_name_str = cls_name if isinstance(cls_name, str) else ""
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
                ordered.append((cls_name_str, name, member))

    for cls_name_str, name, member in sorted(ordered, key=lambda t: (t[1], t[0])):
        if name not in seen:
            seen[name] = _format_signature(member)
    return seen


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

    members = sorted(set(extract_callable_members(dump)) | set(_RBXSCRIPTSIGNAL_MEMBERS))
    print(f"extracted {len(members)} unique callable members")

    signatures = extract_signatures(dump)
    # Merge the RBXScriptSignal signatures; do NOT overwrite a name already
    # present from ``Classes``.
    for name, sig in _RBXSCRIPTSIGNAL_SIGNATURES.items():
        signatures.setdefault(name, sig)
    print(f"extracted {len(signatures)} unique signatures")

    old_members = _read_existing_members()
    _print_diff(old_members, members)

    snapshot: dict[str, object] = {
        "apiDumpVersion": version,
        "generatedFrom": GENERATED_FROM,
        "callableMembers": members,
        "signatures": signatures,
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
