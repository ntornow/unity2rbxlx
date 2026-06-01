"""shared_flag_channels — Phase 2b reframe (2026-06-01), Class 2.

The empirical whole-plan review (design doc §"Phase 2b — cross-domain
authority (two bridge classes)") split cross-domain authority into two
bridge classes. This module records the **Class 2 — dynamic shared-flag**
channel: an attribute whose NAME is computed at runtime
(``"has" .. itemName``) and routed through ONE funnel RemoteEvent
(``PlayerSetSharedFlag``).

Topology cannot OWN each individual runtime write — the name does not
exist until runtime. So topology GATES + RECORDS the *channel*: the set
of literal flag names READ across the domain boundary, the domains that
read them, and the canonical store the funnel writes. The funnel itself
(the ``_GENERIC_RUNTIME_PROMPT`` ``mirrorFlag`` guidance + the autogen
``PlayerSetSharedFlag`` ``OnServerEvent`` listener) is the irreducible
runtime mechanism — it STAYS. This module produces the fact; step 2 /
slice 3 gates the autogen injection on it.

**Schema (one funnel = one record).**

::

    {
      "PlayerSetSharedFlag": {
        "read_names": [<literal flag names read cross-domain, sorted>],
        "reader_domains": [<domains that read a flag, sorted>],
        "canonical_stores": ["Character", "Player"],
        "present": <bool>
      }
    }

  - ``read_names``: literal ``:GetAttribute("...")`` reads whose reader
    resolves to a domain DIFFERENT from where the funnel writes. The
    funnel write is client-originated (``mirrorFlag`` fires from the
    client; the server's ``OnServerEvent`` listener sets the attribute),
    so the canonical cross-domain reader is server-domain. Sourced from
    the reusable reader scan (the ``shared_state_linter``-style
    ``:GetAttribute("...")`` scanner), NOT a new regex.
  - ``reader_domains``: the sorted set of domains that read a qualifying
    flag (today always ``["server"]`` when present).
  - ``canonical_stores``: the CONSTANT ``["Character", "Player"]`` (sorted)
    — what the funnel writes per ``autogen.py:174-176``. NOT a per-read
    derivation; recorded as the fixed funnel behavior.
  - ``present``: ``True`` iff a cross-domain shared-flag reader exists on
    a FRESH transpile. The gate (step 2) injects the funnel iff this is
    true.

**Recompute-only / NOT persisted.** Like ``caller_graph``'s recompute
path (and the cross-domain edge fields), this fact is recomputed every
run from the live ``transpilation_result`` reader scan. It rides on
``TopologyInputs`` and is forwarded into the artifact, but it is never
read back from a prior on-disk plan as authoritative — there is no
``preserved_shared_flag_channels`` path. (This is the fix for the
slice-2 drift where ``cross_domain_edge_candidates`` WAS persisted.)

**Resume — FAIL OPEN.** On a no-transpile resume
(``transpilation_result is None``) the reader scan source is ABSENT, so a
recompute would yield empty ``read_names`` → the step-2 gate would
WRONGLY disable a needed funnel. So on resume this module records
``present: True`` with empty ``read_names`` (the scan is unknown, not
empty), so the gate FAILS OPEN (falls back to today's unconditional
injection). The gate only narrows the funnel on a fresh transpile where
the scan is authoritative.
"""

from __future__ import annotations

import re
from typing import TypedDict

from converter.code_transpiler import TranspiledScript

# The single funnel RemoteEvent every dynamic shared-flag write rides
# through (``code_transpiler.py`` ``mirrorFlag`` prompt +
# ``autogen.py:162`` listener). One funnel => one channel record.
FUNNEL_EVENT_NAME = "PlayerSetSharedFlag"

# CONSTANT — what the funnel writes on the server side per
# ``autogen.py:174-176`` (it sets the attribute on BOTH the Player
# Instance and the character Model). Recorded sorted; NOT a per-read
# derivation.
CANONICAL_STORES: tuple[str, ...] = ("Character", "Player")

# The domain the funnel write originates from. ``mirrorFlag`` fires from
# the CLIENT (``...:FireServer``); the server's ``OnServerEvent``
# listener writes the attribute. A read qualifies as cross-domain when
# the reader's domain differs from this writer side — i.e. a
# server-domain reader (the canonical Door-reads-hasKey case).
_FUNNEL_WRITER_DOMAIN = "client"

# Match a ``<target>:GetAttribute("name")`` literal read. Mirrors the
# canonical scanner in ``shared_state_linter.py:31-34`` — identifier-safe
# attribute names, no extra args. Reusable scan, not a new invention.
_GET_ATTR_RE = re.compile(
    r":GetAttribute\(\s*['\"](?P<attr>[A-Za-z_][\w]*)['\"]\s*\)",
)


class SharedFlagChannel(TypedDict):
    """One funnel channel record. See module docstring for field
    semantics."""

    read_names: list[str]
    reader_domains: list[str]
    canonical_stores: list[str]
    present: bool


# The whole-artifact block: funnel event name -> channel record. One
# funnel today, so exactly one key.
SharedFlagChannels = dict[str, SharedFlagChannel]


def _script_id_for_transpiled(
    ts: TranspiledScript,
    script_id_by_name: dict[str, str],
) -> str:
    """Map a ``TranspiledScript`` row back to its planner ``script_id``.

    ``TranspiledScript.output_filename`` is the file stem with a
    ``.luau`` suffix; the planner's ``script_id_by_name`` index keys by
    the ``RbxScript.name`` (file stem, no extension). Strip the suffix to
    bridge the two. Returns ``""`` when no mapping exists.
    """
    out = ts.output_filename
    name = out[: -len(".luau")] if out.endswith(".luau") else out
    return script_id_by_name.get(name, "")


def compute_shared_flag_channels(
    *,
    transpiled_scripts: list[TranspiledScript] | None,
    script_id_by_name: dict[str, str],
    domains: dict[str, str],
) -> SharedFlagChannels:
    """Record the ``PlayerSetSharedFlag`` channel fact for this run.

    ``transpiled_scripts``: the post-transpile Luau sources to scan.
    ``None`` on a no-transpile resume (``transpilation_result is None``);
    see the resume FAIL-OPEN contract below.

    ``script_id_by_name``: ``RbxScript.name`` -> ``script_id`` (the
    canonical index ``build_script_id_by_name`` produces).

    ``domains``: ``script_id`` -> domain verdict (from
    ``infer_module_domains``). Used to resolve each reader's domain so a
    cross-domain read can be distinguished from a same-domain one.

    A literal ``:GetAttribute("name")`` read qualifies as cross-domain
    iff its reader script's domain differs from the funnel's writer side
    (``_FUNNEL_WRITER_DOMAIN`` == ``"client"``) — i.e. the reader is
    server-domain. Qualifying names land in ``read_names`` (sorted); the
    readers' domains land in ``reader_domains`` (sorted).

    **Resume FAIL OPEN.** When ``transpiled_scripts is None`` the scan
    source is absent. A recompute to empty ``read_names`` would let the
    step-2 gate WRONGLY disable a needed funnel, so this records
    ``present: True`` with empty ``read_names`` — the gate falls back to
    today's unconditional injection. The gate only narrows on a fresh
    transpile.

    Pure function. Recompute-only (never persisted / read back).
    """
    if transpiled_scripts is None:
        # Resume: scan source absent. Fail open so the step-2 gate keeps
        # the funnel rather than disabling it on missing evidence.
        return {
            FUNNEL_EVENT_NAME: SharedFlagChannel(
                read_names=[],
                reader_domains=[],
                canonical_stores=sorted(CANONICAL_STORES),
                present=True,
            ),
        }

    read_names: set[str] = set()
    reader_domains: set[str] = set()
    for ts in transpiled_scripts:
        if ":GetAttribute(" not in ts.luau_source:
            continue
        reader_sid = _script_id_for_transpiled(ts, script_id_by_name)
        if not reader_sid:
            continue
        reader_domain = domains.get(reader_sid, "")
        # Cross-domain iff the reader's domain differs from the funnel's
        # writer side. An empty / unknown reader domain is NOT
        # cross-domain (we cannot prove it crosses the boundary).
        if not reader_domain or reader_domain == _FUNNEL_WRITER_DOMAIN:
            continue
        for match in _GET_ATTR_RE.finditer(ts.luau_source):
            read_names.add(match.group("attr"))
            reader_domains.add(reader_domain)

    return {
        FUNNEL_EVENT_NAME: SharedFlagChannel(
            read_names=sorted(read_names),
            reader_domains=sorted(reader_domains),
            canonical_stores=sorted(CANONICAL_STORES),
            present=bool(read_names),
        ),
    }


__all__ = (
    "CANONICAL_STORES",
    "FUNNEL_EVENT_NAME",
    "SharedFlagChannel",
    "SharedFlagChannels",
    "compute_shared_flag_channels",
)
