"""Post-transpile lowering: route a held-prefab camera-mount equip to a server request.

The pre-transpile ``child_ref_resolver`` records, on each Camera.main-rooted
``RigRootedRetargetFact``, an OPTIONAL equip obligation (``equip_method`` +
``prefab_field``) when the C# script has the UNAMBIGUOUS held-prefab-equip shape:
``Instantiate(<prefab>, …)`` + ``<x>.SetParent(<rig-slot-field>)`` in ONE method.
This module CONSUMES that obligation POST-AI: it rewrites the AI's emitted Luau
equip site

    local <capvar> = [self.host.]instantiatePrefab(self.<prefab>, <slot>, …)
    if <capvar> then … end                 -- the client-side weld/placement guard

into a client->server equip REQUEST (the server now owns the replicated weld +
placement — Phase 2):

    -- _EQUIP_REQUEST_<prefab> (auto: camera-mount equip lowered to server request)
    local _equipRemote = self._services and self._services.equipWeaponRemote
    if _equipRemote and _equipRemote.FireServer then
        _equipRemote:FireServer("<prefab>")
    end

The single string argument is the DETERMINISTIC ``prefab_field`` name (a stable
upstream key Phase 2's handler maps to ``ReplicatedStorage.Templates``), NEVER the
AI output token. The nil-guard keeps the Phase-1 output a safe no-op until Phase 2
declares the ``EquipWeapon`` RemoteEvent.

Scaffold mirrors ``rifle_rig_retarget_lowering``: an own-emit marker (idempotent
re-run), a ``present=False`` fail-closed default carrier stamped for EVERY equip
obligation BEFORE any edit, the REUSED ``_luau_syntax_ok`` revert gate (a parse
failure restores the pre-edit source, carrier stays ``present=False``), and
``present`` re-derived from the FINAL committed source.

FAIL CLOSED (``present=False``, edit NOTHING) on:
  * ``dangling_capvar`` — a ``<capvar>`` read survives OUTSIDE the excised region
    (removing the binding would leave a nil-global read that parses fine but breaks
    silently);
  * ``multi_site`` — ``>1`` ``instantiatePrefab(self.<prefab>)`` assignment in the
    recognized ``equip_method`` (the AI duplicated the call).

Pure (mutates only the script objects' ``luau_source`` / ``equip_binding`` it is
handed, the documented lowering side effect — like ``lower_rifle_rig_retarget``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from converter.child_ref_resolver import ChildRefMap, RigRootedRetargetFact
from converter.rifle_rig_retarget_lowering import _luau_syntax_ok
from converter.trigger_stay_lowering import (
    _luau_pos_in_long_bracket,
    _luau_pos_is_code,
)

# The client ``_services`` handle the request fires through, resolving the
# RemoteEvent named ``EquipWeapon`` under ReplicatedStorage. Phase 2 declares the
# RemoteEvent + server handler + the ``_services`` injection (autogen.py teleport
# precedent); Phase 1 owns ONLY the request-emission contract. The verifier imports
# both so producer + checker share the literals.
EQUIP_REMOTE_SERVICE = "equipWeaponRemote"  # client _services handle
EQUIP_REMOTE_NAME = "EquipWeapon"           # ReplicatedStorage RemoteEvent .Name (Phase-2 target)


class _HasLuauSourceAndPath(Protocol):
    luau_source: str
    source_path: str
    equip_binding: dict[str, object] | None


# A code-level ``function <Class>:<method>(`` / ``function <Class>.<method>(``
# declaration. Used to locate the recognized ``equip_method``'s Luau body.
_FUNCTION_METHOD_RE = re.compile(r"\bfunction\s+([A-Za-z_]\w*)[:.]([A-Za-z_]\w*)\s*\(")

# Block keywords for the Luau method-body span (mirrors the verifier's
# ``_rig_method_body_end`` grammar): ``function``/``do``/``then``/``repeat`` OPEN a
# scope; ``end``/``until`` CLOSE it; ``elseif`` is a CLOSER that cancels its own
# upcoming ``then`` (net 0 for an if/elseif chain).
_BLOCK_OPENER_RE = re.compile(r"\b(function|do|then|repeat)\b")
_BLOCK_CLOSER_RE = re.compile(r"\b(end|until|elseif)\b")


def lower_camera_mount_equip(
    scripts: list[_HasLuauSourceAndPath],
    child_ref_map: ChildRefMap,
) -> int:
    """For each script whose rig fact carries an equip obligation
    (``equip_method``/``prefab_field`` non-empty), rewrite the Luau equip site
    ``[self.host.]instantiatePrefab(self.<prefab>, …)`` (+ its following
    ``if <capvar> then … end`` guard) into a client->server equip REQUEST
    (``equipWeaponRemote:FireServer("<prefab>")``, nil-guarded), and STAMP the
    ``equip_binding`` carrier (default ``present=False``; flipped ``True`` only
    after re-deriving the request from the FINAL source). Returns #scripts
    modified."""
    modified = 0
    for script in scripts:
        obligations = _equip_obligations_for(script, child_ref_map)
        if not obligations:
            continue
        # D8 abstain-on-ambiguity (mirrors the ``multi_site`` fail-close below): a
        # single script with >1 distinct equip obligation (e.g. two camera-mounted
        # weapon slots / two prefab fields) cannot be disambiguated to ONE request,
        # so edit NOTHING and stamp a fail-closed carrier -> the verifier fires.
        if len(obligations) > 1:
            script.equip_binding = {
                "prefab": "",
                "method": "",
                "remote": EQUIP_REMOTE_SERVICE,
                "present": False,
                "multi_obligation": True,
            }
            continue
        fact = obligations[0]
        prefab = fact.prefab_field
        method = fact.equip_method
        changed = False
        original = script.luau_source
        new_source, present, flags = _lower_one(original, prefab, method)
        if present and new_source != original and _luau_syntax_ok(new_source):
            script.luau_source = new_source
            changed = True
        elif present and new_source == original:
            # Idempotent re-run: the request is already present (own-emit marker
            # recognized). Source is unchanged; do NOT count as modified.
            pass
        else:
            # Either nothing to discharge / fail-closed sub-flag set, OR the rewrite
            # produced unparseable Luau -> restore the pre-edit source. The carrier
            # below re-derives present from the (now committed) source.
            script.luau_source = original
        # Re-derive present from the FINAL committed source (independent of the
        # in-flight rewrite): True only when the request actually landed in the
        # method body. Idempotent on a second call (the request is already present).
        final_present = _equip_request_present(script.luau_source, prefab, method)
        carrier: dict[str, object] = {
            "prefab": prefab,
            "method": method,
            "remote": EQUIP_REMOTE_SERVICE,
            "present": final_present,
            # D17/Bug-2: uniform display scale captured from the C# equip method
            # (``localScale = Vector3.one * 0.2f`` -> 0.2; ``None`` = nothing captured,
            # distinct from an explicit 1.0). The bridge keys it by prefab_id for the
            # runtime weld-time ScaleTo and collision-checks across carriers.
            "scale": fact.equip_scale,
        }
        # Carry the fail-closed sub-flags so the verifier detail / a resume carrier
        # records WHY the obligation was not discharged.
        if not final_present:
            if flags.get("multi_site"):
                carrier["multi_site"] = True
            if flags.get("dangling_capvar"):
                carrier["dangling_capvar"] = True
        script.equip_binding = carrier
        if changed:
            modified += 1
    return modified


def _equip_obligations_for(
    script: _HasLuauSourceAndPath, child_ref_map: ChildRefMap
) -> tuple[RigRootedRetargetFact, ...]:
    """The rig facts on ``script`` that carry an equip obligation (both
    ``equip_method`` and ``prefab_field`` non-empty). Joined by canonical path
    (resolved-first, raw fallback — mirrors the rig lowering's lookup)."""
    raw = script.source_path
    try:
        canon = str(Path(raw).resolve())
    except OSError:
        canon = raw
    entry = child_ref_map.get(canon) or child_ref_map.get(raw)
    if entry is None:
        return ()
    return tuple(
        f for f in entry.rig_facts if f.equip_method and f.prefab_field
    )


def _method_body_span(source: str, method: str) -> tuple[int, int] | None:
    """The (body_start, body_end) char span of the Luau method ``method``'s body —
    from the char just past its ``function <Class>:<method>(…)`` header through the
    char just past its matching closing ``end``. None if the method is not found.

    Block-keyword balanced over code positions (long-bracket strings/comments
    skipped wholesale by ``_rig``-style scanning). ``elseif`` cancels its own
    upcoming ``then`` so an if/elseif chain does not overrun the span.

    FAIL CLOSED (return None) on ``>1`` code-position ``function …:<method>(``
    declaration sharing ``method``'s name (the Luau-side analog of the C# overload
    collapse): the rewrite site cannot be disambiguated, so the lowering edits
    nothing (present=False) and the verifier scan does not discharge."""
    matches: list[re.Match[str]] = []
    for m in _FUNCTION_METHOD_RE.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _luau_pos_in_long_bracket(source, m.start()):
            continue
        if m.group(2) != method:
            continue
        matches.append(m)
    if len(matches) != 1:
        return None
    m = matches[0]
    body_end = _luau_block_end(source, m.start())
    return (m.end(), body_end)


def _luau_block_end(source: str, decl_start: int) -> int:
    """The index just past the closing ``end`` of the ``function`` declared at
    ``decl_start``, block-keyword balanced over code positions. EOF if unterminated.
    Mirrors ``contract_verifier._rig_method_body_end``."""
    i = decl_start
    n = len(source)
    block = 0
    seen_open = False
    while i < n:
        if not _luau_pos_is_code(source, i) or _luau_pos_in_long_bracket(source, i):
            i += 1
            continue
        ch = source[i]
        if ch.isalpha() or ch == "_":
            prev_alnum = i > 0 and (source[i - 1].isalnum() or source[i - 1] == "_")
            om = _BLOCK_OPENER_RE.match(source, i)
            if om and not prev_alnum:
                block += 1
                seen_open = True
                i = om.end()
                continue
            cm = _BLOCK_CLOSER_RE.match(source, i)
            if cm and not prev_alnum:
                block -= 1
                i = cm.end()
                if seen_open and block == 0:
                    return i
                continue
            # advance past the whole identifier (so ``send``/``endpoint`` never
            # match a keyword substring).
            k = i
            while k < n and (source[k].isalnum() or source[k] == "_"):
                k += 1
            i = k
            continue
        i += 1
    return n


# The instantiate assignment within the equip method body. Group 1 = the bound
# local ``<capvar>``; the call is ``[self.host.]instantiatePrefab(self.<prefab>``
# / ``[self.host.]instantiatePrefab(<prefab>``. The first arg must be the prefab
# field (bare or ``self.``-qualified) — keyed on the deterministic ``prefab``.
def _instantiate_assign_re(prefab: str) -> re.Pattern[str]:
    p = re.escape(prefab)
    return re.compile(
        r"\blocal\s+([A-Za-z_]\w*)\s*(=)\s*"
        r"(?:self\.host\.)?instantiatePrefab\s*\(\s*(?:self\.)?" + p + r"\b"
    )


def _lower_one(
    source: str, prefab: str, method: str
) -> tuple[str, bool, dict[str, bool]]:
    """Rewrite the equip site in ``method``'s body. Returns
    ``(new_source, present, flags)`` where ``present`` is True iff the rewrite was
    applied AND the request landed, and ``flags`` carries fail-closed sub-flags
    (``multi_site``/``dangling_capvar``). On any fail-closed condition the source is
    returned UNCHANGED with ``present=False`` and the flag set."""
    flags: dict[str, bool] = {}
    span = _method_body_span(source, method)
    if span is None:
        return source, False, flags  # the method is absent (AI reshaped it) -> abstain/fail loud
    body_start, body_end = span

    # Idempotent re-run: the lowering's own marker already present in the method
    # body -> no-op (return as-is, present re-derived by the caller).
    marker = _own_emit_marker(prefab)
    if _span_code_contains(source, body_start, body_end, marker):
        return source, True, flags

    assign_re = _instantiate_assign_re(prefab)
    sites = [
        m
        for m in assign_re.finditer(source, body_start, body_end)
        if _luau_pos_is_code(source, m.start())
        and not _luau_pos_in_long_bracket(source, m.start())
    ]
    if len(sites) > 1:
        flags["multi_site"] = True
        return source, False, flags  # AI duplicated the equip call -> fail closed
    if not sites:
        return source, False, flags  # no equip assignment in the method -> fail loud

    m = sites[0]
    capvar = m.group(1)
    assign_start = m.start()
    # Scan the statement from just past the ``=`` (group 2) so the RHS bracket
    # balance starts at depth 0 with the call's own ``(`` counted in.
    assign_end = _statement_end(source, m.end(2))

    # Span the immediately-following ``if <capvar> then … end`` guard, if present
    # and it consumes ``<capvar>`` (the client-side weld/placement, now obsolete).
    region_end = _maybe_span_following_guard(source, assign_end, body_end, capvar)

    # FAIL CLOSED on a surviving ``<capvar>`` read outside the excised region: a
    # later ``self.currentRifle = <capvar>`` / ``<capvar>:Destroy()`` would become a
    # nil-global read that parses fine but breaks silently.
    if _capvar_read_survives(source, capvar, body_start, body_end, assign_start, region_end):
        flags["dangling_capvar"] = True
        return source, False, flags

    indent = _leading_indent(source, assign_start)
    # Span from the assignment's LINE START so the replacement owns the whole line
    # (its own ``indent`` then provides clean indentation, no double-indent).
    line_start = source.rfind("\n", 0, assign_start) + 1
    replacement = _request_block(prefab, indent)
    new_source = source[:line_start] + replacement + source[region_end:]
    present = _equip_request_present(new_source, prefab, method)
    return new_source, present, flags


def _own_emit_marker(prefab: str) -> str:
    """The lowering's OWN deterministic marker comment for ``prefab``."""
    return (
        f"-- _EQUIP_REQUEST_{prefab} "
        f"(auto: camera-mount equip lowered to server request)"
    )


def _request_block(prefab: str, indent: str) -> str:
    """The nil-guarded server-equip-request statement sequence (no
    ``local … = do…end``). ``indent`` is the leading whitespace of the replaced
    assignment, applied to every emitted line for stable formatting."""
    return (
        f"{indent}{_own_emit_marker(prefab)}\n"
        f"{indent}local _equipRemote = self._services and self._services.{EQUIP_REMOTE_SERVICE}\n"
        f"{indent}if _equipRemote and _equipRemote.FireServer then\n"
        f'{indent}    _equipRemote:FireServer("{prefab}")\n'
        f"{indent}end\n"
    )


def _leading_indent(source: str, pos: int) -> str:
    """The leading whitespace of the line containing ``pos``."""
    line_start = source.rfind("\n", 0, pos) + 1
    i = line_start
    while i < len(source) and source[i] in " \t":
        i += 1
    return source[line_start:i]


def _statement_end(source: str, rhs_start: int) -> int:
    """The index just past the end of the assignment statement whose RHS begins at
    ``rhs_start`` — balanced across (), [], {} and short strings, terminating at the
    end of the logical line (a code-level newline at bracket depth 0, or EOF)."""
    i = rhs_start
    n = len(source)
    depth = 0
    while i < n:
        ch = source[i]
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            depth -= 1
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                c = source[i]
                if c == "\\":
                    i += 2
                    continue
                if c == quote or c == "\n":
                    break
                i += 1
            i += 1
            continue
        if ch == "\n" and depth == 0:
            return i + 1  # include the newline so the next statement starts clean
        i += 1
    return n


def _maybe_span_following_guard(
    source: str, after_assign: int, body_end: int, capvar: str
) -> int:
    """If the next statement after the assignment is the TRIVIAL obsolete weld guard
    ``if <capvar> then … end`` — condition EXACTLY the captured local, NOTHING ELSE —
    return the index just past that guard's closing ``end`` (so the whole obsolete
    client-side weld/placement block is excised). A COMPOUND condition
    (``if <capvar> and …``, ``if <capvar> ~= nil then``, etc.) is NOT spanned: it may
    carry real post-equip logic (``if <capvar> and X then self.currentRifle = <capvar>
    end``), so leaving it intact leaves a ``<capvar>`` read OUTSIDE the excised region
    → the dangling_capvar check fail-closes (edit nothing). Otherwise return
    ``after_assign`` unchanged (only the assignment is replaced)."""
    j = after_assign
    n = len(source)
    while j < n and source[j] in " \t\r\n":
        j += 1
    if j >= body_end:
        return after_assign
    # EXACT trivial guard only: ``if <capvar> then`` (condition == capvar alone). The
    # ``then`` immediately following the bare capvar is the discriminator — a compound
    # condition has another token (``and``/``~=``/``or``/``==``/``.``/``:`` …) where
    # ``then`` would be, so it does not match and falls through to dangling_capvar.
    guard_re = re.compile(r"if\s+" + re.escape(capvar) + r"\s+then\b")
    gm = guard_re.match(source, j)
    if gm is None or not _luau_pos_is_code(source, j):
        return after_assign
    # Balance from the ``if`` through its matching ``end``.
    guard_end = _luau_if_block_end(source, j, body_end)
    if guard_end is None:
        return after_assign
    return guard_end


def _luau_if_block_end(source: str, if_start: int, limit: int) -> int | None:
    """The index just past the ``end`` matching the ``if`` at ``if_start`` (its
    ``then`` opens the block). None if unbalanced before ``limit``. ``elseif``
    cancels its own ``then``."""
    i = if_start
    n = min(len(source), limit)
    block = 0
    seen_then = False
    while i < n:
        if not _luau_pos_is_code(source, i) or _luau_pos_in_long_bracket(source, i):
            i += 1
            continue
        ch = source[i]
        if ch.isalpha() or ch == "_":
            prev_alnum = i > 0 and (source[i - 1].isalnum() or source[i - 1] == "_")
            om = _BLOCK_OPENER_RE.match(source, i)
            if om and not prev_alnum:
                if om.group(1) == "then":
                    seen_then = True
                block += 1
                i = om.end()
                continue
            cm = _BLOCK_CLOSER_RE.match(source, i)
            if cm and not prev_alnum:
                block -= 1
                i = cm.end()
                if seen_then and block == 0:
                    return i
                continue
            k = i
            while k < n and (source[k].isalnum() or source[k] == "_"):
                k += 1
            i = k
            continue
        i += 1
    return None


def _capvar_read_survives(
    source: str,
    capvar: str,
    body_start: int,
    body_end: int,
    region_start: int,
    region_end: int,
) -> bool:
    """True if a code-position READ of the bare ``<capvar>`` token survives in the
    method body OUTSIDE the excised region ``[region_start, region_end)`` — a read
    NOT preceded by ``.`` / ``:`` (a member access on another receiver) and NOT an
    assignment LHS (a fresh ``local <capvar> =`` rebind, which is harmless). Any
    such survivor means removing the binding leaves a dangling nil-global read."""
    pattern = re.compile(r"\b" + re.escape(capvar) + r"\b")
    for m in pattern.finditer(source, body_start, body_end):
        start = m.start()
        if region_start <= start < region_end:
            continue  # inside the excised region -> fine
        if not _luau_pos_is_code(source, start):
            continue
        if _luau_pos_in_long_bracket(source, start):
            continue
        # A member-access tail ``x.capvar`` / ``x:capvar`` is a DIFFERENT symbol.
        j = start - 1
        while j >= 0 and source[j] in " \t":
            j -= 1
        if j >= 0 and source[j] in ".:":
            continue
        # A fresh ``local <capvar> =`` rebind is a new binding, not a dangling read.
        line_start = source.rfind("\n", 0, start) + 1
        prefix = source[line_start:start]
        if re.search(r"\blocal\s+$", prefix):
            continue
        return True
    return False


def _span_code_contains(
    source: str, span_start: int, span_end: int, token: str
) -> bool:
    """True if ``token`` appears at a code position within
    ``source[span_start:span_end]``."""
    idx = source.find(token, span_start)
    while idx != -1 and idx < span_end:
        if _luau_pos_is_code(source, idx) and not _luau_pos_in_long_bracket(source, idx):
            return True
        idx = source.find(token, idx + 1)
    return False


def _equip_request_present(source: str, prefab: str, method: str) -> bool:
    """INDEPENDENT, code-position-aware re-derivation (the lowering's own
    re-derive, mirroring the verifier's ``_equip_request_discharged``): is the
    equip request discharged within ``method``'s body? See
    ``equip_request_discharged_in_span`` for the exact predicate. Restricting to
    the method body makes the (1)-(4) clauses precise — a same-prefab spawn in
    another method neither satisfies nor breaks this obligation."""
    if not prefab or not method:
        return False
    span = _method_body_span(source, method)
    if span is None:
        return False
    return equip_request_discharged_in_span(source, span[0], span[1], prefab)


# The alias-binding statement ``local <alias> = self._services and
# self._services.<remote>`` the request fires through. Group 1 = the bound alias
# local; the binding MUST resolve ``self._services.<remote>`` (the carrier's own
# remote), so a request fired on an alias bound to a DIFFERENT service does NOT
# count (a remote-agnostic ``FireServer("<prefab>")`` would otherwise false-pass).
def _alias_binding_re(remote: str) -> re.Pattern[str]:
    r = re.escape(remote)
    return re.compile(
        r"\blocal\s+([A-Za-z_]\w*)\s*=\s*"
        r"self\._services\s+and\s+self\._services\." + r + r"\b"
    )


def _marked_block_span(
    source: str, body_start: int, body_end: int, prefab: str
) -> tuple[int, int] | None:
    """The CONTIGUOUS own-emitted request block — from the own-emit marker through
    the matching ``end`` of the nil-guard ``if`` the lowering writes (see
    ``_request_block``). Returns ``(marker_start, block_end)`` at code positions, or
    None if the marker or its guard ``end`` is absent. Anchoring clauses (2)/(3) to
    THIS span (not the whole method body) is what closes the alias-shadow bypass: a
    later ``local <alias> = self._services.<foreign>`` rebind + foreign fire lives
    OUTSIDE the marked block and so cannot satisfy the binding+fire requirement."""
    marker = _own_emit_marker(prefab)
    marker_start = source.find(marker, body_start)
    while marker_start != -1 and marker_start < body_end:
        if _luau_pos_is_code(source, marker_start) and not _luau_pos_in_long_bracket(
            source, marker_start
        ):
            break
        marker_start = source.find(marker, marker_start + 1)
    if marker_start == -1 or marker_start >= body_end:
        return None
    # The block's nil-guard ``if`` opens on the line(s) after the binding; find the
    # first code-position ``if`` token after the marker and balance to its ``end``.
    if_re = re.compile(r"\bif\b")
    for im in if_re.finditer(source, marker_start, body_end):
        if not _luau_pos_is_code(source, im.start()):
            continue
        if _luau_pos_in_long_bracket(source, im.start()):
            continue
        block_end = _luau_if_block_end(source, im.start(), body_end)
        if block_end is None:
            return None
        return marker_start, block_end
    return None


def equip_request_discharged_in_span(
    source: str, body_start: int, body_end: int, prefab: str,
    remote: str = EQUIP_REMOTE_SERVICE,
) -> bool:
    """The shared discharge predicate over an explicit method-body span (so the
    verifier can reuse the EXACT producer logic, scoping with its own
    ``_rig``-style span helper). True IFF, all at code positions:
      (1) the own-emit marker ``-- _EQUIP_REQUEST_<prefab>`` is present (within the
          method body), AND — WITHIN that marker's CONTIGUOUS emitted block
          ``[marker, guard-if's end]`` (NOT scattered across the body,
          so a shadowing rebind to a foreign remote + foreign fire OUTSIDE the block
          cannot satisfy these) —
      (2) an alias is bound to the carrier's OWN remote — ``local <alias> =
          self._services and self._services.<remote>`` (``remote`` defaults to
          ``EQUIP_REMOTE_SERVICE``; the verifier passes the carrier's recorded
          ``remote`` so a mismatched-remote carrier is checked against ITS remote),
          AND
      (3) that SAME ``<alias>`` fires the request ``<alias>:FireServer("<prefab>")``
          (so firing the prefab on a DIFFERENT remote/alias does NOT discharge),
          AND
      (4) NO surviving ``instantiatePrefab(<prefab>)`` equip call anywhere in the
          method body (the request REPLACED it, not added alongside)."""
    if not remote:
        return False
    block = _marked_block_span(source, body_start, body_end, prefab)
    if block is None:
        return False
    block_start, block_end = block

    # (2) the alias(es) bound to self._services.<remote> WITHIN the marked block.
    bound_aliases: set[str] = set()
    for bm in _alias_binding_re(remote).finditer(source, block_start, block_end):
        if not _luau_pos_is_code(source, bm.start()):
            continue
        if _luau_pos_in_long_bracket(source, bm.start()):
            continue
        bound_aliases.add(bm.group(1))
    if not bound_aliases:
        return False

    # (3) require <alias>:FireServer("<prefab>") on one of THOSE aliases, WITHIN the
    # marked block — tie the FireServer call to the carrier's own-remote binding in
    # the same contiguous block (not any FireServer anywhere in the method).
    fired = False
    for alias in bound_aliases:
        call_re = re.compile(
            r"\b" + re.escape(alias) + r"\s*:\s*FireServer\s*\(\s*"
            r'"' + re.escape(prefab) + r'"\s*\)'
        )
        for cm in call_re.finditer(source, block_start, block_end):
            if _luau_pos_is_code(source, cm.start()) and not _luau_pos_in_long_bracket(
                source, cm.start()
            ):
                fired = True
                break
        if fired:
            break
    if not fired:
        return False

    # (4) no surviving camera-mount instantiate equip call (full method body).
    inst_re = re.compile(
        r"\binstantiatePrefab\s*\(\s*(?:self\.)?" + re.escape(prefab) + r"\b"
    )
    for m in inst_re.finditer(source, body_start, body_end):
        if _luau_pos_is_code(source, m.start()) and not _luau_pos_in_long_bracket(
            source, m.start()
        ):
            return False
    return True
