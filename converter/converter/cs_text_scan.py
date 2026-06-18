"""Shared C# raw-source scan helpers for the pre-transpile fact producers.

These two helpers are used by every fact-producer that scans raw C# text
(``child_ref_resolver``, ``send_message_resolver``): they have no Roslyn AST, so
matching must be CODE-POSITION-AWARE (skip strings/comments/char-literals) and
keyed on a canonical path. Both producers import them from here so the logic
lives once.

Pure: read values, return values. No game-specific names.
"""

from __future__ import annotations

from pathlib import Path


def _canon_key(path: Path) -> str:
    """The canonical .cs path key: ``str(path.resolve())`` when resolvable, else
    the raw string (a synthetic test path that doesn't exist on disk)."""
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _cs_pos_is_code(source: str, pos: int) -> bool:
    """True if char index ``pos`` is real C# code, not inside a string literal
    or a comment. Scans from the START of the file (block comments and verbatim
    strings can open on a prior line, so a per-line scan would miss them),
    tracking: ``//`` line comments, ``/* */`` block comments, regular
    ``"..."`` strings (``\\`` escapes), verbatim ``@"..."`` strings (``""`` is an
    escaped quote, ``\\`` is literal), and ``'...'`` char literals — the forms
    the textual matchers must avoid firing inside."""
    i = 0
    n = len(source)
    while i < pos:
        ch = source[i]
        # Line comment — skip to end of line.
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            nl = source.find("\n", i)
            if nl == -1 or nl >= pos:
                return False  # comment runs through pos
            i = nl + 1
            continue
        # Block comment — skip to closing ``*/``.
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            if end == -1 or end + 2 > pos:
                return False  # block comment encloses pos
            i = end + 2
            continue
        # Verbatim string ``@"..."`` — ``""`` escapes a quote, ``\`` is literal.
        if ch == "@" and i + 1 < n and source[i + 1] == '"':
            j = i + 2
            while j < n:
                if source[j] == '"':
                    if j + 1 < n and source[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            if j >= pos:
                return False  # string encloses pos
            i = j + 1
            continue
        # Regular string ``"..."`` — ``\`` escapes.
        if ch == '"':
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == '"':
                    break
                j += 1
            if j >= pos:
                return False
            i = j + 1
            continue
        # Char literal ``'...'`` — ``\`` escapes.
        if ch == "'":
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "'":
                    break
                j += 1
            if j >= pos:
                return False
            i = j + 1
            continue
        i += 1
    return True
