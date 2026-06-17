"""shared_flag_channels — record the dynamic shared-flag (Class 2) channel.

A shared flag is an attribute whose NAME is computed at runtime
(``"has" .. itemName``) and routed through ONE funnel RemoteEvent
(``PlayerSetSharedFlag``). Topology cannot own each individual runtime
write (the name doesn't exist until runtime), so it GATES + RECORDS the
*channel*: the literal flag names READ across the domain boundary, the
reader domains, and the canonical store the funnel writes. The funnel
itself (the ``mirrorFlag`` prompt guidance + the autogen
``PlayerSetSharedFlag`` ``OnServerEvent`` listener) is the irreducible
runtime mechanism and STAYS; this module produces the fact that step 2 /
slice 3 gates the autogen injection on.

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

  - ``read_names``: ``:GetAttribute("...")`` reads — AND
    ``:GetAttributeChangedSignal("...")`` signal subscriptions (a reader
    whose only shared-flag access is waiting for the flag to change is
    still a cross-domain reader; Phase 3 deliverable #2 lists this form)
    — whose reader resolves to a domain DIFFERENT from where the funnel
    writes. The
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

**Recompute-only / NOT persisted.** Recomputed every run from the live
``transpilation_result`` reader scan; forwarded into the artifact but
never read back from a prior on-disk plan as authoritative (there is no
``preserved_shared_flag_channels`` path).

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
from converter.scene_runtime_topology.cross_domain_edges import (
    NON_RUNTIME_DOMAINS,
)

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

# Match a ``<target>:GetAttribute("name")`` literal read. The attr scan
# must match the RUNTIME ALLOWLIST: the ``PlayerSetSharedFlag`` server
# listener in ``autogen.py`` does
# ``if #flagName > 64 or not string.match(flagName, "^[%w_]+$") then return end``
# — it DROPS any flag name with chars outside ``[%w_]`` (alphanumeric +
# underscore) or longer than 64. That mirrors Roblox's own attribute-name
# constraint: ``"hasRed Key"`` (space) and ``"hasKey-A"`` (hyphen) are NOT
# valid Roblox attribute names and the runtime cannot deliver them.
# Empirically every ``GetAttribute`` name in the live transpile cache is
# identifier-safe. So the capture is the ASCII-explicit ``[A-Za-z0-9_]+`` —
# NOT ``\w``: Python 3 ``\w`` is Unicode-aware (matches accented letters,
# other-script digits, etc.), so it would over-capture names that Lua's
# byte-oriented ``%w`` (and therefore the funnel's ``^[%w_]+$`` gate and
# Roblox's attribute charset) would reject. ``[A-Za-z0-9_]+`` allows a
# leading digit, exactly matching ``^[%w_]+$`` on ASCII. The length cap
# (<=64) is enforced below. The closing quote is back-referenced to the
# opening one so mismatched quote styles don't span across args.
#
# Also matches ``:GetAttributeChangedSignal("name")`` — a server reader
# whose ONLY shared-flag access is the *signal* form (waits for the flag
# to change rather than reading its current value) is just as much a
# cross-domain reader as the literal ``:GetAttribute("name")`` form. The
# optional ``ChangedSignal`` group keeps one regex for both; the charset +
# <=64 cap + quote backreference are unchanged in intent.
_GET_ATTR_RE = re.compile(
    r""":GetAttribute(?:ChangedSignal)?\(\s*(?P<q>['"])(?P<attr>[A-Za-z0-9_]+)(?P=q)\s*\)""",
)

# Mirror the funnel listener's length cap (``#flagName > 64``): a captured
# name longer than this is dropped by the runtime, so the fact must not
# record it.
_MAX_FLAG_NAME_LEN = 64


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
    # Fail-open signal for Fix 3 (below): a reader that HAS a qualifying
    # GetAttribute read but whose script_id can't be resolved. We can't
    # attribute its domain, but a missing mapping must not become NEGATIVE
    # evidence that disables the funnel.
    fail_open_present = False
    for ts in transpiled_scripts:
        # Early skip: ``:GetAttribute`` is the shared prefix of BOTH the
        # literal read (``:GetAttribute(``) and the signal form
        # (``:GetAttributeChangedSignal(``) the regex now matches. Using
        # the prefix (no trailing ``(``) keeps both in scope.
        if ":GetAttribute" not in ts.luau_source:
            continue
        reader_sid = _script_id_for_transpiled(ts, script_id_by_name)
        if not reader_sid:
            # Fail open on unmappable reader.
            # ``build_script_id_by_name`` deliberately omits ambiguous
            # names (class/stem collisions), so this is reachable on real
            # projects. Silently dropping a script that HAS a qualifying
            # read turns missing-mapping into negative evidence → if it's
            # the only server-side reader, ``present`` would wrongly be
            # False. Mirror the resume fail-open: keep the funnel. We do
            # NOT add to ``read_names`` (no domain to attribute it to), so
            # the name set stays unpolluted.
            #
            # Fail open on ANY unmappable reader with a qualifying read; do
            # NOT use ``TranspiledScript.script_type`` to suppress it — that
            # type is PRE-COHERENCE and NOT authoritative (coherence can flip
            # LocalScript→Script), so trusting it could wrongly drop a real
            # server reader. Fail-open is conservative: it keeps the funnel
            # (harmless-if-unused), which is exactly today's unconditional
            # behavior (strictly no worse).
            if _GET_ATTR_RE.search(ts.luau_source):
                fail_open_present = True
            continue
        reader_domain = domains.get(reader_sid, "")
        # Cross-domain iff the reader's domain differs from the funnel's
        # writer side. A NON_RUNTIME reader domain (``""``/``"helper"``/
        # ``"excluded"``/``"legacy"``) is never emitted at runtime, so it
        # must NOT pollute ``read_names`` or set ``present`` — it cannot
        # actually read the flag at runtime. (``not reader_domain`` is
        # subsumed since ``"" in NON_RUNTIME_DOMAINS``; equivalent.)
        if (reader_domain in NON_RUNTIME_DOMAINS
                or reader_domain == _FUNNEL_WRITER_DOMAIN):
            continue
        for match in _GET_ATTR_RE.finditer(ts.luau_source):
            attr = match.group("attr")
            # Mirror the funnel's ``#flagName > 64`` cap: a longer name is
            # dropped by the runtime listener, so the fact must not record
            # it.
            if len(attr) > _MAX_FLAG_NAME_LEN:
                continue
            read_names.add(attr)
            reader_domains.add(reader_domain)

    return {
        FUNNEL_EVENT_NAME: SharedFlagChannel(
            read_names=sorted(read_names),
            reader_domains=sorted(reader_domains),
            canonical_stores=sorted(CANONICAL_STORES),
            # ``present`` from mapped readers OR the fail-open signal for
            # an unmappable-but-qualifying reader.
            present=bool(read_names) or fail_open_present,
        ),
    }


__all__ = (
    "CANONICAL_STORES",
    "FUNNEL_EVENT_NAME",
    "SharedFlagChannel",
    "SharedFlagChannels",
    "compute_shared_flag_channels",
)
