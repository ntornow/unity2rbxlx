"""Canonical shared-flag name sanitization.

The cross-script shared-flag attribute name is built at runtime as
``"has" .. itemName``. ``itemName`` derives from a Unity prefab/item name
with no sanitization, so a name like ``"Red Key"`` produces ``"hasRed Key"``
— which Roblox's ``SetAttribute`` rejects (spaces/hyphens are not valid
attribute-name characters) and the ``PlayerSetSharedFlag`` funnel listener
drops (``^[%w_]+$``). The result is a silent cross-domain gameplay break.

Sanitization happens at the RUNTIME ``"has" .. name`` concat site only (the
emitted Luau gsub from :func:`luau_flag_sanitize_expr`). It is deliberately
NOT applied to the ``itemName`` / ``ItemType`` attribute VALUES in
``scene_converter``: those are gameplay payloads (forwarded raw to
``GetItem`` / ``pickup_runtime`` dispatch), so sanitizing them would corrupt
dirty-name pickups. Sanitizing only the derived flag keeps the raw value
intact for gameplay while the cross-script flag stays ``[%w_]``-valid.

:func:`sanitize_flag_stem` is the Python REFERENCE MIRROR of the emitted
Luau gsub — identical bytes on ASCII input. It is the executable spec used by
parity tests; production sanitization is the emitted Luau, not this function.

**Sanitizer spec (ASCII-explicit, Python + Luau byte-identical for ASCII):**
Replace each contiguous run of ``[^A-Za-z0-9_]`` with a single ``_``.

- Python MUST use ``re.sub(r"[^A-Za-z0-9_]+", "_", name)`` — NOT a
  ``\\w`` charset, because Python 3 ``\\w`` is Unicode-aware and would
  diverge from Lua's byte-oriented ``%w``.
- Luau (emitted): ``(<expr>:gsub("[^%w_]+", "_"))``.
- No case change. A no-op on clean identifiers (``sanitize_flag_stem("Key")
  == "Key"``) so SimpleFPS's existing literal ``GetAttribute("hasKey")``
  readers keep matching.

**Degenerate edges (consistent writer/reader, documented not guarded):** the
runtime gsub cannot "skip", so (a) a name whose ``"has" + stem`` exceeds the
funnel's 64-char cap is dropped by the funnel listener (``autogen.py``) —
consistently, the flag just doesn't mirror cross-domain; (b) an all-symbol
name (``"+++"``) collapses to the single flag ``"has_"``, so distinct
all-symbol items would share it. Both require pathological item names; writer
and reader still agree (no split-brain). A runtime guard is intentionally NOT
added — it would add fragile inline conditionals to the AI prompt for a
vanishingly rare input.
"""
from __future__ import annotations

import re

# Single canonical Python sanitizer regex. ASCII-explicit by design: a
# Unicode-aware ``\w`` would diverge from Lua's byte-oriented ``%w``.
_FLAG_TOKEN_RE = re.compile(r"[^A-Za-z0-9_]+")

# The ONE canonical inline Luau sanitizer expression, as a ``str.format``
# template. Every emitted runtime site (transpiler prompt + coherence
# packs writers + dynamic reader) MUST build its sanitized name from this
# constant so all emitted call sites are byte-identical. ``%%`` is NOT
# needed because this is a ``.format`` template (plain ``%`` is a literal),
# but Lua-pattern ``%w`` is a literal ``%w`` here regardless.
#
# Lua mirror of the Python regex: ``gsub("[^%w_]+", "_")`` replaces each
# run of non-(alphanumeric-or-underscore) with a single ``_``, matching
# ``re.sub(r"[^A-Za-z0-9_]+", "_", ...)`` byte-for-byte on ASCII input.
# The OUTER parens truncate ``gsub``'s second return value (the match
# count) so the expression yields exactly one string.
_LUAU_FLAG_SANITIZE = '({expr}:gsub("[^%w_]+", "_"))'


def sanitize_flag_stem(name: str) -> str:
    """Python reference mirror of the emitted Luau flag sanitizer.

    Replaces each contiguous run of ``[^A-Za-z0-9_]`` with a single ``_``.
    No case change; a no-op on clean identifiers. Byte-identical to
    ``(name:gsub("[^%w_]+", "_"))`` for ASCII input. Not called in
    production (the runtime Luau is authoritative) — this is the executable
    spec used by parity tests.
    """
    return _FLAG_TOKEN_RE.sub("_", name)


def luau_flag_sanitize_expr(expr: str) -> str:
    """Return the canonical inline Luau ``gsub`` sanitizer wrapping ``expr``.

    ``expr`` is the Luau expression yielding the raw name (e.g. ``itemName``
    or ``name``). The emitted shape is ``(<expr>:gsub("[^%w_]+", "_"))`` —
    the byte-identical mirror of :func:`sanitize_flag_stem` for ASCII.
    """
    return _LUAU_FLAG_SANITIZE.format(expr=expr)
