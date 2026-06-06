"""trigger_stay_lowering.py -- deterministic OnTriggerStay->Stay lowering.

The generic transpiler collapses Unity ``OnTriggerEnter``/``OnTriggerExit``/
``OnTriggerStay`` (and the collision variants) all onto a single ``.Touched``
**edge** signal:

    -- OnTriggerStay(other): ...
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        ...
    end)

That is faithful for the *Enter* phase, but ``OnTriggerStay`` in Unity fires
**every physics frame** the collider overlaps -- a player standing inside a
turret's sight volume (no fresh ``.Touched`` edge) is never detected. Slice 1.1
added a host primitive ``connectGameObjectSignalStay(comp, go, fn)`` (a throttled
``GetPartsInPart`` poll, the per-frame STAY analog). This pass rewrites the
*specific* ``connectGameObjectSignal(<go>, "Touched", <fn>)`` binding whose
immediately-preceding origin comment marks ``OnTriggerStay`` into

    self.host:connectGameObjectSignalStay(self.gameObject, function(other)
        ...
    end)

dropping the ``"Touched", `` argument and preserving ``<go>`` + the function
expression and everything after.

This runs in GENERIC mode only, inside ``contract_pipeline.transpile_with_contract``
on the RAW ``TranspiledScript.luau_source`` BEFORE comment-stripping -- which is
why the ``-- OnTriggerStay`` origin comment (mandated by the generic contract
doc) is still present and usable as the lowering key. Generic mode deliberately
skips the legacy coherence packs, so this is generic's own re-expression of the
turret STAY-poll behaviour.

EXACT, BINDING-LOCAL match (BLOCKING design requirement):

  * The key is the comment on the line **immediately preceding** the specific
    ``connectGameObjectSignal(...,"Touched",...)`` binding. Not any "Stay"
    substring anywhere in the method; not an earlier method comment.
  * The token is the **exact** ``OnTriggerStay`` -- it must NOT match
    ``OnCollisionStay`` (which also maps to ``.Touched``) nor ``OnTriggerEnter``.
  * In an ``Awake`` with multiple bindings (turret ``OnTriggerStay``->Touched
    AND ``OnTriggerExit``->TouchEnded, or a body mixing Enter+Stay), only the
    binding whose own immediately-preceding comment is ``OnTriggerStay`` is
    rewritten.
  * Robust to ``self.host:`` and ``self.host.`` forms and to leading whitespace.
  * Idempotent: the rewritten call has no ``"Touched"`` literal and no longer
    matches, so a second pass is a no-op.

The matcher is string-span / structural (consistent with the other facet
transforms). A line-local regex finds only the call HEAD
(``self.host:connectGameObjectSignal(``); ``_find_touched_separator`` then scans
forward for the first CODE-LEVEL ``, "Touched",`` separator -- skipping any
``"Touched"`` buried inside the go expression's own short string, Luau
long-bracket string, or ``--`` comment -- so ``<go>`` is the whole first
argument (a name, an index, or a call -- not just ``self.gameObject``, and may
itself carry a long-string literal) and is preserved verbatim. The captured
``<go>`` is balance-checked (``_is_balanced_first_arg``: type-matched bracket
stack + short/long-string aware) and the rewrite ABSTAINS on any unbalanced /
mismatched fragment -- the safe degrade, never a corrupt rewrite.

Two further abstain guards stop a rewrite whose HEAD itself sits inside a Lua
string/comment: ``_luau_pos_is_code`` skips short quoted strings and ``--``
line comments (line-local), and ``_luau_pos_in_long_bracket`` skips multi-line
long-bracket strings (``[[ ... ]]``/``[=[ ... ]=]``) and block comments
(``--[[ ... ]]``) opened on an earlier line.
"""

from __future__ import annotations

import re
from typing import Protocol


class _HasLuauSource(Protocol):
    luau_source: str


# Match the call HEAD up to (and including) the open paren of a GameObject-
# signal binding:  ``self.host:connectGameObjectSignal(``
#
# Groups:
#   indent = leading indentation on the call's own line (used to find line start)
#   head   = receiver+method+open-paren, e.g.
#       ``self.host:connectGameObjectSignal(``
#
# The ``<go>`` first argument and the ``, "Touched",`` separator are NO LONGER
# matched by the regex -- they are found STRUCTURALLY by ``_find_touched_separator``
# scanning from the open paren. A naive non-greedy ``.+? , "Touched",`` regex
# anchors on the *first textual* ``"Touched"``, which can fall INSIDE the go
# expression's own string literal (e.g. a long-bracket string ``[[foo,
# "Touched", bar]]``) and truncate the go mid-string -> false-abstain. The
# structural scan skips ``"Touched"`` occurrences that sit inside a short or
# long string and anchors on the first CODE-LEVEL separator instead.
#
# Both ``:`` (method) and ``.`` (field) call forms are accepted.
_CONNECT_HEAD_RE = re.compile(
    r"""(?P<indent>[ \t]*)
        (?P<head>
            self\.host[:.]connectGameObjectSignal
            \s*\(\s*
        )
    """,
    re.VERBOSE,
)

# The ``"Touched"`` (single or double quoted) separator literal, used by the
# structural scan to test a candidate code-level anchor.
_TOUCHED_LITERAL_RE = re.compile(r"""['"]Touched['"]""")

# The exact origin-comment token that gates the rewrite. Anchored to the start
# of the comment body so ``OnTriggerStay`` matches but ``OnCollisionStay`` and
# ``OnTriggerEnter`` do not. A trailing word-boundary stops it from matching a
# hypothetical ``OnTriggerStayLonger``.
_STAY_COMMENT_RE = re.compile(r"^--\s*OnTriggerStay\b")


# The closer expected on top of the bracket stack for each opener -- a
# TYPE-MATCHED match, so a ``(`` cancelled by a ``]`` (``(1]``) is unbalanced.
_BRACKET_CLOSER = {"(": ")", "[": "]", "{": "}"}


def _is_balanced_first_arg(go: str) -> bool:
    """True if ``go`` is a *complete, balanced* first argument: all ``()``,
    ``[]``, ``{}`` are balanced WITH MATCHING TYPES, every short quote is closed
    (no unterminated string), and no Luau long-bracket string is left open --
    delimiters inside string literals (short OR long) ignored.

    The widened ``.+?`` go-capture anchors on the *first* ``, "Touched",`` it
    sees. If the go expression itself contains an internal ``, "Touched",``
    (e.g. ``self:pick("foo", "Touched", x)``), the non-greedy capture stops at
    the INTERNAL anchor and over-captures a short, UNBALANCED fragment
    (``self:pick("foo"``). Rewriting that fragment drops the wrong ``"Touched"``
    and corrupts the call. Guarding on balance lets the real captures
    (``self.gameObject``, ``self.parts[1]``, ``self:getTriggerPart()``,
    ``self:pick("Touched", x)``, ``self:pick([[foo, "Touched", bar]], x)``)
    through while the pathological internal-anchor fragment -- which is
    necessarily unbalanced -- ABSTAINS (bias-to-abstain: the safe degrade,
    never corrupt).

    Two correctness properties beyond a naive depth counter:
      * Long-string aware: a Luau long-bracket literal (``[[ ... ]]`` and
        leveled ``[=[ ... ]=]``) is a STRING -- its contents (including any
        ``, "Touched",`` or stray brackets) are skipped, reusing
        ``_long_bracket_open_level`` so a balanced go carrying a long-string
        does not false-abstain. An UNCLOSED long string is unbalanced.
      * Type-matched stack: each closer must match the most-recent opener's
        type, so ``(1]`` is unbalanced rather than netting to depth zero.
    """
    stack: list[str] = []  # expected closers, innermost last
    quote: str | None = None  # open short-quote char, or None
    i = 0
    n = len(go)
    while i < n:
        ch = go[i]
        if quote is not None:
            # Inside a short string: only a backslash-escape or the matching
            # close quote is meaningful; bracket chars are literal text.
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "[":
            # A ``[`` may open a Luau long-bracket string (``[[``/``[=[``); if
            # so, skip its whole payload. A bare ``[`` (indexing) falls through
            # to the bracket-stack push below.
            level = _long_bracket_open_level(go, i)
            if level is not None:
                close = go.find("]" + "=" * level + "]", i + level + 2)
                if close == -1:
                    return False  # unterminated long string -> unbalanced
                i = close + level + 2
                continue
        if ch in ("(", "[", "{"):
            stack.append(_BRACKET_CLOSER[ch])
        elif ch in (")", "]", "}"):
            if not stack or stack.pop() != ch:
                return False  # close with no/mismatched open
        i += 1
    return not stack and quote is None


def _luau_pos_is_code(source: str, pos: int) -> bool:
    """True if char index ``pos`` is real code, not inside a string or a
    ``--`` comment.

    Scans from the start of ``pos``'s line, tracking single/double-quoted
    strings (with backslash escapes) and ``--`` line comments -- the only forms
    the transpiler emits. Mirrors ``child_index_lowering._luau_pos_is_code``.
    """
    i = source.rfind("\n", 0, pos) + 1
    quote: str | None = None
    while i < pos:
        ch = source[i]
        if quote is not None:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "-" and i + 1 < pos and source[i + 1] == "-":
            return False  # rest of the line (incl. pos) is a comment
        i += 1
    return quote is None


def _luau_pos_in_long_bracket(source: str, pos: int) -> bool:
    """True if char index ``pos`` is inside an OPEN Luau long-bracket string
    (``[[ ... ]]``, ``[=[ ... ]=]`` and higher level forms) or long block
    comment (``--[[ ... ]]``) opened earlier in ``source`` and not yet closed.

    ``_luau_pos_is_code`` only scans from the start of ``pos``'s own line, so a
    binding inside a MULTI-LINE long-string/comment payload (opened on an
    earlier line) looks like live code to it and would be corrupted. This scans
    the WHOLE source up to ``pos`` and reports whether we are inside such a
    span, biasing the rewrite to ABSTAIN (the safe degrade -- never corrupt a
    string). Single/double-quoted strings and ``--`` line comments are handled
    by ``_luau_pos_is_code``; this only adds the long-bracket forms.
    """
    i = 0
    n = len(source)
    while i < pos:
        ch = source[i]
        # ``--`` may begin a line comment or, if immediately followed by a long
        # bracket, a long block comment. A long block comment shares the same
        # ``[=*[ ... ]=*]`` delimiters as a long string.
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            j = i + 2
            level = _long_bracket_open_level(source, j)
            if level is not None:
                # ``--[=*[`` long block comment: skip to its closing ``]=*]``.
                close = source.find("]" + "=" * level + "]", j)
                if close == -1 or close >= pos:
                    return True  # pos is inside this open block comment
                i = close + level + 2
                continue
            # Plain ``--`` line comment: skip to end of line (or pos).
            nl = source.find("\n", j)
            if nl == -1 or nl >= pos:
                return False  # line comment runs through pos -> not a long span
            i = nl + 1
            continue
        if ch in ("'", '"'):
            # Short quoted string: skip to its matching close (honor escapes).
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
        if ch == "[":
            level = _long_bracket_open_level(source, i)
            if level is not None:
                # Long string ``[=*[``: skip to its closing ``]=*]``.
                close = source.find("]" + "=" * level + "]", i + level + 2)
                if close == -1 or close >= pos:
                    return True  # pos is inside this open long string
                i = close + level + 2
                continue
        i += 1
    return False


def _long_bracket_open_level(source: str, i: int) -> int | None:
    """If ``source[i:]`` begins a Luau long-bracket opener ``[=*[``, return the
    number of ``=`` signs (the bracket level: 0 for ``[[``, 1 for ``[=[`` ...).
    Otherwise return ``None``."""
    if i >= len(source) or source[i] != "[":
        return None
    j = i + 1
    while j < len(source) and source[j] == "=":
        j += 1
    if j < len(source) and source[j] == "[":
        return j - (i + 1)
    return None


def _find_touched_separator(source: str, start: int) -> int | None:
    """Scan ``source`` from ``start`` (the char just after the call's open paren,
    i.e. the first char of ``<go>``) for the first CODE-LEVEL ``, "Touched",``
    argument separator. Return the index of the opening quote of that ``"Touched"``
    literal, or ``None`` if no code-level separator is found on the binding's
    physical line.

    "Code-level" means NOT inside a short string, a Luau long-bracket string, or
    a ``--`` comment (line OR ``--[=*[`` block). A naive first-textual-``"Touched"``
    anchor (the old non-greedy regex) lands on a ``"Touched"`` buried inside the
    go expression's own string/comment payload (e.g. a long-string first arg
    ``[[foo, "Touched", bar]]``) and truncates the go mid-literal -> false-abstain
    or, worse, the wrong separator. Skipping string/comment spans anchors on the
    real separator instead.

    The scan is LINE-LOCAL (stops at the first newline): the transpiler always
    emits the ``connect...(<go>, "Touched",`` head on a single physical line, so
    a separator on a later line would belong to a different binding.

    NOTE: bracket depth is intentionally NOT tracked. The FIRST code-level
    ``, "Touched",`` is taken as the separator even when it sits inside a nested
    call (``self:pick(a, "Touched", x)``); the resulting prefix go is then
    unbalanced and the caller's balance guard ABSTAINS. This bias-to-abstain on
    an ambiguous nested ``, "Touched",`` is the safe degrade -- never a corrupt
    rewrite -- and keeps the round-2 short-string case abstaining.
    """
    i = start
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "\n":
            return None  # separator must be on the binding's own line
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            # ``--`` comment: a ``--[=*[`` block comment skips to its close;
            # a plain line comment runs to end of line (-> no separator).
            level = _long_bracket_open_level(source, i + 2)
            if level is not None:
                close = source.find("]" + "=" * level + "]", i + 2)
                if close == -1:
                    return None  # unterminated block comment -> no separator
                i = close + level + 2
                continue
            return None  # line comment to EOL -> no code-level separator
        if ch in ("'", '"'):
            # Short quoted string: a CODE-LEVEL separator candidate is a quote
            # that opens ``"Touched"`` immediately preceded by ``, ``. Test the
            # separator pattern here before treating it as an opaque string.
            sep = _TOUCHED_LITERAL_RE.match(source, i)
            if sep is not None and _is_separator_context(source, start, i, sep.end()):
                return i
            # Otherwise skip the whole short string (honor escapes / EOL).
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
        if ch == "[":
            level = _long_bracket_open_level(source, i)
            if level is not None:
                close = source.find("]" + "=" * level + "]", i + level + 2)
                if close == -1:
                    return None  # unterminated long string -> no separator
                i = close + level + 2
                continue
        i += 1
    return None


def _is_separator_context(source: str, go_start: int, quote_pos: int, after: int) -> bool:
    """True if the ``"Touched"`` literal at ``quote_pos..after`` is a genuine
    ``, "Touched",`` ARGUMENT separator: preceded by ``,`` (after optional
    whitespace, but not at the very start of ``<go>``) and followed by ``,``
    (after optional whitespace). A bare ``"Touched"`` that is the WHOLE first
    arg or a sub-argument (``pick("Touched", x)`` -> preceded by ``(``) is NOT a
    separator."""
    j = quote_pos - 1
    while j >= go_start and source[j] in (" ", "\t"):
        j -= 1
    if j < go_start or source[j] != ",":
        return False
    k = after
    while k < len(source) and source[k] in (" ", "\t"):
        k += 1
    return k < len(source) and source[k] == ","


def _preceding_comment_line(source: str, line_start: int) -> str | None:
    """Return the stripped text of the LITERAL immediately-preceding physical
    line (the one ending at the newline just before ``line_start``), or ``None``
    if there is no preceding line.

    "Immediately preceding" is STRICT: it does NOT skip blank/whitespace-only
    lines. The design contract emits the ``-- OnTriggerStay`` origin comment
    directly above its binding, so requiring the comment on the literal previous
    line is both correct and safe. A blank line OR a non-blank statement between
    the comment and the binding therefore returns that (blank/statement) line --
    not the comment -- and the binding is left as an edge.
    """
    if line_start == 0:
        return None
    # ``line_start - 1`` is the newline terminating the previous physical line.
    # (If the char before the binding's line isn't a newline, ``line_start``
    # wasn't a line start -- but the matcher always anchors on a line start.)
    prev_nl = source.rfind("\n", 0, line_start - 1)
    prev_line = source[prev_nl + 1:line_start - 1]
    return prev_line.strip()


def rewrite_trigger_stay_source(source: str) -> tuple[str, int]:
    """Rewrite each ``connectGameObjectSignal(<go>, "Touched", <fn>)`` binding
    whose immediately-preceding comment is ``-- OnTriggerStay...`` into
    ``connectGameObjectSignalStay(<go>, <fn>)``.

    Returns ``(new_source, count)`` where ``count`` is the number of bindings
    rewritten (0 -> ``source`` returned unchanged)."""
    count = 0
    out: list[str] = []
    cursor = 0  # index in ``source`` up to which output has been emitted

    for m in _CONNECT_HEAD_RE.finditer(source):
        head_start = m.start("head")
        # Skip heads inside a short string literal / ``--`` line comment (scanned
        # line-locally) OR inside a multi-line long-bracket string / block comment
        # opened on an earlier line (scanned from source start). Either => leave
        # untouched, never corrupt the payload.
        if not _luau_pos_is_code(source, head_start):
            continue
        if _luau_pos_in_long_bracket(source, head_start):
            continue
        # The origin comment must be on the line immediately above the binding.
        comment = _preceding_comment_line(source, m.start("indent"))
        if comment is None or not _STAY_COMMENT_RE.match(comment):
            continue
        # Find the first CODE-LEVEL ``, "Touched",`` separator structurally (the
        # regex no longer guesses it): ``go`` is everything between the open paren
        # and that separator.
        go_start = m.end("head")
        sep_quote = _find_touched_separator(source, go_start)
        if sep_quote is None:
            continue  # no code-level "Touched" separator -> not this binding
        go = source[go_start:sep_quote].rstrip()
        # Trim the trailing ``,`` + whitespace that precedes the separator quote
        # (``<go>, ``) so the balance check sees just the go expression.
        go = go[:-1].rstrip() if go.endswith(",") else go
        # Only rewrite when the captured go is a balanced, complete first arg;
        # otherwise ABSTAIN (leave the binding unchanged) -- never corrupt.
        if not _is_balanced_first_arg(go):
            continue
        # Consume the separator: ``"Touched"`` + following ``,\s*`` so the rewrite
        # drops the literal AND its separator, leaving the function expression as
        # the (now first) trailing argument.
        sep_end = _TOUCHED_LITERAL_RE.match(source, sep_quote)
        assert sep_end is not None  # _find_touched_separator only returns a "Touched" pos
        k = sep_end.end()
        while k < len(source) and source[k] in (" ", "\t"):
            k += 1
        if k < len(source) and source[k] == ",":
            k += 1
        while k < len(source) and source[k] in (" ", "\t"):
            k += 1
        # Emit everything up to the head, the Stay-renamed head, then ``<go>``
        # and its ``, `` separator (``source[go_start:sep_quote]``) verbatim, and
        # resume the cursor AFTER the dropped ``"Touched", ``.
        new_head = m.group("head").replace(
            "connectGameObjectSignal", "connectGameObjectSignalStay", 1,
        )
        out.append(source[cursor:m.start("indent")])
        out.append(m.group("indent"))
        out.append(new_head)
        out.append(source[go_start:sep_quote])  # <go> + its ``, `` separator
        cursor = k  # resume after the dropped ``"Touched", ``
        count += 1

    if count == 0:
        return source, 0
    out.append(source[cursor:])
    return "".join(out), count


def lower_trigger_stay(scripts: list[_HasLuauSource]) -> int:
    """Rewrite each ``OnTriggerStay``-origin ``connectGameObjectSignal(go,
    "Touched", fn)`` binding on every script's ``luau_source`` to the Stay-poll
    host method ``connectGameObjectSignalStay(go, fn)``. Returns the number of
    scripts modified.

    GENERAL rule: keyed on the guaranteed ``-- OnTriggerStay`` origin comment
    immediately above the binding, NEVER on ``s.name`` -- it applies to any
    OnTriggerStay binding, not just the turret. OnTriggerEnter/Exit and the
    OnCollision* variants keep their ``.Touched`` edge semantics (their origin
    comments don't match)."""
    changed = 0
    for s in scripts:
        src = s.luau_source or ""
        new_src, count = rewrite_trigger_stay_source(src)
        if count and new_src != src:
            s.luau_source = new_src
            changed += 1
    return changed
