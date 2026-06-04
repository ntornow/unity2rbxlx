"""child_index_lowering.py -- generic-allowlist child-index lowering pass.

A deterministic, structure-gated lowering on the generic scene-runtime
allowlist (called from ``contract_pipeline.transpile_with_contract``). It
rewrites the transpiler's flattened ``transform.GetChild(n)`` emission --
``<receiver>:GetChildren()[N]`` (N is the 1-based ``n+1`` index) -- into an
inline resolution that skips injected NON-SPATIAL children (the converter
injects an ``AudioSource`` -> ``Sound`` as child index 0 of a Part, so the
naive ``GetChildren()[1]`` returns the ``Sound`` and a subsequent
``:GetPivot()`` crashes with "GetPivot is not a valid member of Sound").

This is a *lowering pass*, NOT a coherence pack: it is deterministic, gated
on a STRUCTURAL fingerprint (the ``:GetChildren()[<literal>]`` emission
shape -- never ``s.name`` / per-game identity), and the resolution is a
GENERAL rule: pick the N-th SPATIAL child for ANY GetChild site, skipping
injected non-spatial children (Sound, etc.). It is never turret-specific.

The resolution is two-tier, mirroring the design's ordering:
  1. prefer the N-th ``_SceneRuntimeId``-stamped child (the converter
     stamps real scene/prefab descendants; injected Sounds are not
     stamped);
  2. fall back to the N-th ``BasePart`` / ``Model`` child.
If there is no N-th match under either tier, the expression returns ``nil``
-- the same shape the naive index would yield on a missing child, so the
existing ``if base then`` guards in the emitted code still handle it (the
structure-gate ABSTAINS rather than crashing; design edge case 1).

Generic-only -> legacy output stays byte-identical (the pass is wired only
in the generic ``transpile_with_contract`` path).

Idempotent: after lowering, the ``:GetChildren()[<literal>]`` fingerprint is
gone (replaced by the resolver IIFE), so a re-run finds nothing to rewrite.
"""

from __future__ import annotations

import re
from typing import Protocol

from converter.runtime_contract import _strip_strings_and_comments


class _HasLuauSource(Protocol):
    luau_source: str


# The transpiler lowers Unity ``transform.GetChild(n)`` to
# ``<receiver>:GetChildren()[<N>]`` where ``<N>`` is the 1-based ``n + 1``
# literal. We match the ``:GetChildren()[<N>]`` suffix on the stripped
# source (so an occurrence inside a string/comment is never a signal), then
# walk LEFT from the ``:`` over a balanced receiver expression on the RAW
# source. ``<N>`` must be an integer literal -- a variable index
# (``GetChildren()[i]``) is a genuine dynamic lookup the lowering must NOT
# touch (it is not a flattened ``GetChild(constant)``).
_GET_CHILDREN_INDEX_RE = re.compile(
    r":GetChildren\(\)\s*\[\s*(?P<idx>[0-9]+)\s*\]",
)

# Characters that can legally end a receiver expression immediately before
# the ``:GetChildren()`` call: a closing bracket/paren, an identifier char,
# or a quote (a string-literal receiver, e.g. unusual but valid). We walk
# left over a *balanced* run so ``a:b():GetChildren()[1]`` keeps the whole
# ``a:b()`` receiver, not just ``b()``.
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_]")


def _receiver_span(src: str, colon_pos: int) -> int | None:
    """Return the start offset of the receiver expression that ends at
    ``colon_pos`` (the ``:`` of ``:GetChildren()``), or ``None`` if no
    well-formed receiver precedes it.

    Walks LEFT from ``colon_pos - 1`` over a balanced run of brackets and
    identifier/access chars (``a.b``, ``a:b()``, ``foo[bar]``, ``x()``).
    Stops at the first char that cannot be part of a prefix expression
    (whitespace at depth 0, ``=``, ``(`` at depth 0, a binary operator,
    etc.). The returned span is the minimal contiguous receiver; it is
    spliced verbatim into the resolver IIFE so any side-effect-free prefix
    expression round-trips exactly."""
    i = colon_pos - 1
    depth = 0
    while i >= 0:
        c = src[i]
        if c in ")]}":
            depth += 1
            i -= 1
            continue
        if c in "([{":
            if depth == 0:
                break
            depth -= 1
            i -= 1
            continue
        if depth > 0:
            # Inside a bracketed run -- consume everything (including
            # whitespace and operators) until the run closes.
            i -= 1
            continue
        if _IDENT_CHAR.match(c) or c in ".:":
            i -= 1
            continue
        # Depth 0, not part of an access chain -> receiver starts at i+1.
        break
    start = i + 1
    # Reject a degenerate / leading-dot receiver (nothing to splice).
    receiver = src[start:colon_pos]
    if not receiver or receiver[0] in ".:":
        return None
    return start


def _resolver_expr(receiver: str, n: int) -> str:
    """An inline IIFE that resolves the N-th SPATIAL child of ``receiver``,
    skipping injected non-spatial children. Two-tier: prefer the N-th
    ``_SceneRuntimeId``-stamped child, else the N-th ``BasePart``/``Model``
    child. Returns ``nil`` when there is no N-th match (abstain)."""
    return (
        "(function(__p) "
        "if not __p then return nil end "
        "local __n = 0 "
        "for _, __c in __p:GetChildren() do "
        'if __c:GetAttribute("_SceneRuntimeId") ~= nil then '
        f"__n += 1 if __n == {n} then return __c end "
        "end "
        "end "
        "__n = 0 "
        "for _, __c in __p:GetChildren() do "
        'if __c:IsA("BasePart") or __c:IsA("Model") then '
        f"__n += 1 if __n == {n} then return __c end "
        "end "
        "end "
        f"return nil end)({receiver})"
    )


def lower_child_index(scripts: list[_HasLuauSource]) -> int:
    """Rewrite every flattened ``transform.GetChild(n)`` emission
    (``<receiver>:GetChildren()[<literal>]``) in ``scripts`` to a
    structure-gated N-th-spatial-child resolver. Returns the number of
    scripts modified.

    GENERAL rule (acceptance 2): keyed on the ``:GetChildren()[<literal>]``
    STRUCTURE, never on ``s.name`` -- it applies to any GetChild site, not
    just the turret. A variable index (``[i]``) is left untouched (it is a
    genuine dynamic lookup, not a flattened constant GetChild)."""
    changed = 0
    for s in scripts:
        src = s.luau_source or ""
        stripped = _strip_strings_and_comments(src)
        # Collect rewrite spans on the STRIPPED source (so matches inside
        # strings/comments are skipped), then apply right-to-left on the RAW
        # source so earlier offsets stay valid.
        spans: list[tuple[int, int, str]] = []
        for m in _GET_CHILDREN_INDEX_RE.finditer(stripped):
            colon_pos = m.start()  # position of the ':' in ':GetChildren'
            start = _receiver_span(src, colon_pos)
            if start is None:
                continue
            receiver = src[start:colon_pos]
            n = int(m.group("idx"))
            spans.append((start, m.end(), _resolver_expr(receiver, n)))
        if not spans:
            continue
        spans.sort(key=lambda t: t[0], reverse=True)
        new_src = src
        for start, end, repl in spans:
            new_src = new_src[:start] + repl + new_src[end:]
        if new_src != src:
            s.luau_source = new_src
            changed += 1
    return changed
