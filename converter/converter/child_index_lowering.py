"""child_index_lowering.py -- shared Unity child-index lowering logic.

The transpiler flattens Unity ``transform.GetChild(n)`` to
``<receiver>:GetChildren()[N]`` (N is the 1-based ``n + 1`` index). The
converter injects an ``AudioSource`` -> ``Sound`` as child index 0 of a Part,
so the naive ``GetChildren()[1]`` returns the ``Sound`` and a following
``:GetPivot()`` crashes with "GetPivot is not a valid member of Sound".

This module is the SINGLE source of the lowering logic, shared by two callers:

  1. the legacy coherence pack ``_fix_unity_transform_child_index`` in
     ``script_coherence_packs.py`` (operates on ``RbxScript.source``), and
  2. the generic scene-runtime path ``transpile_with_contract`` in
     ``contract_pipeline.py`` via ``lower_child_index`` (operates on
     ``TranspiledScript.luau_source``).

Both rewrite each ``<recv>:GetChildren()[N]`` site to ``__unityChild(recv, N)``,
where ``__unityChild`` is a Luau helper (``_UNITY_CHILD_HELPER``, injected once
per script) that resolves the N-th authored child: prefer the N-th
``_SceneRuntimeId``-stamped child, else the N-th ``Model``/``BasePart`` child,
else ``nil`` (abstain -- the existing ``if base then`` guards handle it).

The matcher is the SIMPLE-receiver regex ``_GETCHILDREN_INDEX_RE``: the
receiver group ``[A-Za-z_][A-Za-z0-9_.]*`` cannot match a receiver containing
``()``/``[]``, and ``re.sub`` is non-overlapping, so a nested chain
``a:GetChildren()[1]:GetChildren()[1]`` rewrites only the inner site to
``__unityChild(a, 1):GetChildren()[1]`` -- no corruption. ``_luau_pos_is_code``
skips matches inside single/double-quoted strings and ``--`` comments.
"""

from __future__ import annotations

import re
from typing import Protocol


class _HasLuauSource(Protocol):
    luau_source: str


# Match ``<recv>:GetChildren()[N]`` where ``<recv>`` is a simple dotted name
# (no ``()``/``[]``). N must be an integer literal -- a variable index
# (``GetChildren()[i]``) is a genuine dynamic lookup the lowering must NOT
# touch (it is not a flattened ``GetChild(constant)``).
_GETCHILDREN_INDEX_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_.]*):GetChildren\(\)\s*\[\s*(\d+)\s*\]"
)

_UNITY_CHILD_HELPER = """\
-- _AutoUnityTransformChild: Unity transform.GetChild(n) indexes only child
-- GameObjects (Transforms); Roblox GetChildren() also returns injected Sounds/
-- Scripts/etc., so raw GetChildren()[n] grabs the wrong instance. Authored
-- GameObject hosts carry a _SceneRuntimeId attribute -- index those (1-based,
-- mirroring the GetChildren()[n] call sites this replaces).
local function __unityChild(parent, i)
\tif not parent then return nil end
\tlocal n = 0
\tfor _, c in ipairs(parent:GetChildren()) do
\t\tif c:GetAttribute("_SceneRuntimeId") ~= nil then
\t\t\tn += 1
\t\t\tif n == i then return c end
\t\tend
\tend
\t-- Fallback for unstamped/legacy output: nth Model/BasePart child.
\tn = 0
\tfor _, c in ipairs(parent:GetChildren()) do
\t\tif c:IsA("Model") or c:IsA("BasePart") then
\t\t\tn += 1
\t\t\tif n == i then return c end
\t\tend
\tend
\treturn nil
end
"""


def _long_bracket_level(source: str, i: int) -> int | None:
    """If ``source[i]`` opens a Luau long bracket ``[=*[``, return its level
    (number of ``=``); else ``None``. ``i`` must point at the opening ``[``."""
    if source[i] != "[":
        return None
    j = i + 1
    while j < len(source) and source[j] == "=":
        j += 1
    if j < len(source) and source[j] == "[":
        return j - (i + 1)
    return None


def _luau_pos_is_code(source: str, pos: int) -> bool:
    """True if char index ``pos`` is real code, not inside a string or a comment.

    Scans from the START of the file (block comments / long strings can open on
    a prior line, so a per-line scan would miss them), tracking: ``--`` line
    comments, ``--[[ ]]`` / ``--[=[ ]=]`` block comments, ``[[ ]]`` / ``[=[ ]=]``
    long strings, and single/double-quoted short strings (``\\`` escapes).
    Backtick interpolation strings aren't modeled (the transpiler doesn't emit
    them here)."""
    i = 0
    n = len(source)
    while i < pos:
        ch = source[i]
        # Comment — line or block (``--`` then optional long bracket).
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            level = _long_bracket_level(source, i + 2)
            if level is not None:
                close = "]" + "=" * level + "]"
                end = source.find(close, i + 2)
                if end == -1 or end + len(close) > pos:
                    return False  # block comment encloses pos
                i = end + len(close)
                continue
            nl = source.find("\n", i)
            if nl == -1 or nl >= pos:
                return False  # line comment runs through pos
            i = nl + 1
            continue
        # Long string ``[[ ]]`` / ``[=[ ]=]`` (not a comment).
        level = _long_bracket_level(source, i)
        if level is not None:
            close = "]" + "=" * level + "]"
            end = source.find(close, i + level + 2)
            if end == -1 or end + len(close) > pos:
                return False  # long string encloses pos
            i = end + len(close)
            continue
        # Short string ``"..."`` / ``'...'`` (``\`` escapes).
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == ch:
                    break
                j += 1
            if j >= pos:
                return False
            i = j + 1
            continue
        i += 1
    return True


def source_has_child_index(source: str) -> bool:
    """True if ``source`` has at least one ``<recv>:GetChildren()[N]`` site in
    real code (not inside a string/comment) -- the lowering's detection gate."""
    return any(
        _luau_pos_is_code(source, m.start())
        for m in _GETCHILDREN_INDEX_RE.finditer(source)
    )


def rewrite_child_index_source(source: str) -> tuple[str, int]:
    """Rewrite every code-position ``<recv>:GetChildren()[N]`` site in
    ``source`` to ``__unityChild(recv, N)`` and inject ``_UNITY_CHILD_HELPER``
    once if any site was rewritten. Returns ``(new_source, count)`` where
    ``count`` is the number of sites rewritten (0 -> ``source`` unchanged)."""
    count = 0

    def _repl(m: "re.Match[str]") -> str:
        nonlocal count
        # Skip matches inside string literals / comments.
        if not _luau_pos_is_code(source, m.start()):
            return m.group(0)
        count += 1
        return f"__unityChild({m.group(1)}, {m.group(2)})"

    new_source = _GETCHILDREN_INDEX_RE.sub(_repl, source)
    if count == 0:
        return source, 0
    # Inject the helper unless it is already defined at a real code position
    # (a definition buried in a comment/string would leave ``__unityChild(...)``
    # call sites undefined at runtime).
    if not _has_helper_definition_at_code(new_source):
        new_source = _UNITY_CHILD_HELPER + "\n" + new_source
    return new_source, count


def _has_helper_definition_at_code(source: str) -> bool:
    """True if ``local function __unityChild(`` appears at a real code position
    (not inside a string/comment) -- the real helper definition."""
    idx = source.find("local function __unityChild(")
    while idx != -1:
        if _luau_pos_is_code(source, idx):
            return True
        idx = source.find("local function __unityChild(", idx + 1)
    return False


def lower_child_index(scripts: list[_HasLuauSource]) -> int:
    """Rewrite every flattened ``transform.GetChild(n)`` emission
    (``<recv>:GetChildren()[N]``) on each script's ``luau_source`` to the shared
    ``__unityChild`` resolver. Returns the number of scripts modified.

    Keyed on the ``:GetChildren()[<literal>]`` STRUCTURE (not ``s.name``); a
    variable index (``[i]``) is left untouched (genuine dynamic lookup)."""
    changed = 0
    for s in scripts:
        src = s.luau_source or ""
        new_src, count = rewrite_child_index_source(src)
        if count and new_src != src:
            s.luau_source = new_src
            changed += 1
    return changed
