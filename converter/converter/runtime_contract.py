"""runtime_contract.py -- Lexical verifier for generic-runtime ModuleScripts.

A ``ModuleScript`` emitted under ``--scene-runtime=generic`` must return a
pure class table with **no side-effects at module scope**. This verifier
enforces that contract before the script is shipped to the host runtime.
See ``converter/docs/design/scene-runtime-contract.md`` Piece 1 for the
rules; this module implements rules (a) through (f).

The verifier is **conservative lexical, not a parser** -- best-effort
backstop intended to catch the side-effect / lifecycle / Unity-callback
shapes the contract explicitly forbids. The reprompt loop handles false
positives (e.g. pure top-level calls that the rule (a) check over-rejects).
A still-failing module fails closed: the whole conversion drops back to
clean ``legacy`` per the design doc's ``auto`` semantics (wired in PR3b).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "LIFECYCLE_METHODS",
    "UNITY_MESSAGE_CALLBACKS",
    "Violation",
    "VerificationResult",
    "verify_module",
]


# ---------------------------------------------------------------------------
# Contract constants
# ---------------------------------------------------------------------------

# Method names the host runtime calls on each class instance. Bound on the
# class table as ``function Class:Name(...) ... end`` (or one of the
# equivalent shapes). RHS must be a function -- rule (c).
LIFECYCLE_METHODS: frozenset[str] = frozenset({
    "Awake", "OnEnable", "Start",
    "Update", "FixedUpdate", "LateUpdate",
    "OnDisable", "OnDestroy",
    "new",  # constructor; rule (e) additionally checks its body
})

# Unity message callbacks the host does NOT dispatch by name. Modules must
# wire them via ``self.host:connect(signal, fn)`` in ``Awake`` instead
# (Piece 6). Rule (f) rejects any class-table binding shape.
UNITY_MESSAGE_CALLBACKS: frozenset[str] = frozenset({
    "OnTriggerEnter", "OnTriggerExit", "OnTriggerStay",
    "OnCollisionEnter", "OnCollisionExit", "OnCollisionStay",
    "OnMouseDown", "OnMouseUp", "OnMouseEnter",
    "OnMouseExit", "OnMouseOver", "OnMouseDrag",
})


@dataclass(frozen=True)
class Violation:
    """One rule violation in a verified module.

    ``rule`` is the single-letter rule id from the design doc (``"a"`` ..
    ``"f"``). ``line`` is the 1-based source line. ``message`` is a
    one-sentence summary the reprompt feeds back to the AI.
    """

    rule: str
    line: int
    message: str


@dataclass(frozen=True)
class VerificationResult:
    """Aggregate verification outcome. ``ok`` is True iff ``violations`` is
    empty. Iterating ``violations`` walks them in source order (rule order
    is not stable -- the reprompt cares about *what* is wrong, not which
    rule fired first)."""

    ok: bool
    violations: tuple[Violation, ...]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def verify_module(source: str) -> VerificationResult:
    """Verify a Luau ModuleScript against the generic-runtime contract.

    Args:
        source: The full Luau source as a single string.

    Returns:
        ``VerificationResult`` with ``ok=True`` and no violations when the
        source is contract-compliant; otherwise ``ok=False`` and a tuple
        of ``Violation`` rows the caller can hand to the reprompt path.
    """
    stripped = _strip_strings_and_comments(source)
    statements = list(_iter_top_level_statements(stripped, source))

    violations: list[Violation] = []
    violations.extend(_check_top_level_statements(statements, stripped))
    violations.extend(_check_module_return(statements, source))
    violations.extend(_check_lifecycle_assignments(statements, stripped))
    violations.extend(_check_constructor_purity(stripped, source))
    violations.extend(_check_unity_message_callbacks(statements, stripped, source))

    # Source order for reprompt readability. Tie-break on rule letter so
    # output is deterministic across runs.
    violations.sort(key=lambda v: (v.line, v.rule))
    return VerificationResult(ok=not violations, violations=tuple(violations))


# ---------------------------------------------------------------------------
# String / comment stripping
#
# Replace every char inside a string literal or comment with a space so:
#  - line numbers are preserved (newlines stay newlines);
#  - keyword scans don't see ``"function"`` inside a string or ``-- end``
#    inside a comment.
# ---------------------------------------------------------------------------

_LONG_OPEN = re.compile(r"\[(=*)\[")  # ``[[`` or ``[=[`` or ``[==[``...


def _strip_strings_and_comments(source: str) -> str:
    """Replace every char inside a string literal or comment with a space.

    Preserves newlines and total length so violation line numbers map
    one-to-one with the original source. Handles:

      - line comments  ``-- ...\\n``
      - block comments ``--[[ ... ]]``    (any level: ``--[=[ ... ]=]``)
      - short strings  ``"..."`` / ``'...'`` (with ``\\`` escapes)
      - long strings   ``[[ ... ]]``       (any level: ``[==[ ... ]==]``)
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]

        # ---- line / block comment ----
        if c == "-" and i + 1 < n and source[i + 1] == "-":
            # Block comment ``--[[`` or ``--[=[``?
            if i + 2 < n and source[i + 2] == "[":
                m = _LONG_OPEN.match(source, i + 2)
                if m:
                    close = "]" + m.group(1) + "]"
                    end = source.find(close, m.end())
                    end = n if end == -1 else end + len(close)
                    out.append(_blank_keep_newlines(source[i:end]))
                    i = end
                    continue
            # Line comment.
            nl = source.find("\n", i + 2)
            end = n if nl == -1 else nl
            out.append(_blank_keep_newlines(source[i:end]))
            i = end
            continue

        # ---- long string ``[[`` / ``[=[`` ----
        if c == "[":
            m = _LONG_OPEN.match(source, i)
            if m:
                close = "]" + m.group(1) + "]"
                end = source.find(close, m.end())
                end = n if end == -1 else end + len(close)
                out.append(_blank_keep_newlines(source[i:end]))
                i = end
                continue

        # ---- short string ``"`` / ``'`` ----
        if c in ('"', "'"):
            quote = c
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == quote:
                    j += 1
                    break
                if source[j] == "\n":
                    # Unterminated short string -- safest to bail at EOL.
                    break
                j += 1
            out.append(_blank_keep_newlines(source[i:j]))
            i = j
            continue

        out.append(c)
        i += 1

    return "".join(out)


def _blank_keep_newlines(text: str) -> str:
    """Replace every char with ' ', except keep ``\\n`` so line counts hold."""
    return "".join("\n" if ch == "\n" else " " for ch in text)


# ---------------------------------------------------------------------------
# Top-level statement extraction
#
# Walks ``stripped`` token-by-token tracking block / paren / brace depth.
# Yields ``(start, end, text)`` triples for every span where all three
# depths are zero, delimited by newline + semicolon.
#
# Block opens: ``function`` ``do`` ``if`` ``repeat``
# Block closes: ``end`` ``until``
# ``then`` / ``elseif`` / ``else`` are internal punctuation inside an
# ``if`` block and do NOT change depth. The single ``if`` at the start
# pushes once; the single ``end`` at the bottom pops once -- regardless
# of how many ``elseif`` / ``then`` clauses appear in between.
#
# This rule was a SPIKE-discovered fix from PR3a's compliance run:
# the original ``then``+1 / ``end``-1 scheme inflated depth on
# multi-clause ``if/elseif/elseif/end`` chains (extra ``then`` per
# clause without matching ``end``) and silently dropped every
# subsequent top-level statement -- producing rule-(d) false positives
# on the largest modules (Mine, Pickup, Player, etc.). Test file
# ``test_runtime_contract.py`` carries the regression fixture.
#
# Known limitation (documented, not fixed): a top-level Luau
# *if-expression* ``local x = if c then a else b`` opens an ``if`` that
# never closes with ``end``. Subsequent statements get dropped. Not
# worth fixing -- AI output rarely emits if-expressions and the
# fail-mode is over-rejection, which the reprompt path handles.
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    (?P<word>[A-Za-z_][A-Za-z_0-9]*)
    | (?P<num>\d[\w.]*)
    | (?P<punc>[(){}\[\];,])
    | (?P<op>::|\.\.\.|\.\.|->|==|~=|<=|>=|<<|>>|[+\-*/%^#=<>:.@])
    | (?P<ws>\s+)
    | (?P<other>.)
    """,
    re.VERBOSE,
)

_BLOCK_OPEN = frozenset({"function", "do", "if", "repeat"})
_BLOCK_CLOSE = frozenset({"end", "until"})


@dataclass(frozen=True)
class _Statement:
    """A top-level statement span. ``text`` is the stripped (comment/string
    blanked) form; the caller uses ``start``/``end`` to slice the ORIGINAL
    source when it needs the literal text (e.g. for the message)."""

    start: int   # char offset of first non-whitespace char
    end: int     # char offset just past last char (statement terminator excluded)
    line: int    # 1-based line of ``start``
    text: str    # stripped slice ``stripped[start:end]``


def _iter_top_level_statements(stripped: str, original: str):
    """Yield ``_Statement`` rows for each top-level statement.

    A statement boundary at top level is a ``;`` or a newline that's not
    preceded by a partial expression. We use the simpler approximation:
    split on ``;`` and on newlines when block / paren / brace depths are
    all zero. Per-statement text is the slice between boundaries.
    """
    block = paren = brace = 0
    stmt_start: int | None = None
    n = len(stripped)

    def _flush(start: int, end: int):
        if start is None or start >= end:
            return None
        text = stripped[start:end]
        if not text.strip():
            return None
        # Find first non-ws char for canonical ``start``.
        m = re.search(r"\S", text)
        if m is None:
            return None
        real_start = start + m.start()
        line = original.count("\n", 0, real_start) + 1
        return _Statement(real_start, end, line, text)

    for m in _TOKEN_RE.finditer(stripped):
        tok = m.group()
        kind = m.lastgroup

        # Statement boundary: top-level ``;`` or newline.
        if kind == "punc" and tok == ";" and block == paren == brace == 0:
            if stmt_start is not None:
                stmt = _flush(stmt_start, m.start())
                if stmt is not None:
                    yield stmt
                stmt_start = None
            continue
        if kind == "ws" and "\n" in tok and block == paren == brace == 0:
            if stmt_start is not None:
                stmt = _flush(stmt_start, m.start())
                if stmt is not None:
                    yield stmt
                stmt_start = None
            continue

        # Start a fresh statement at the first non-whitespace token at
        # top level. This happens BEFORE the depth update so the token
        # itself counts as part of its own statement -- otherwise
        # ``function NAME(...)`` would bump block depth on the
        # ``function`` keyword and then never get captured as the start
        # of a statement.
        if kind != "ws" and stmt_start is None and block == paren == brace == 0:
            stmt_start = m.start()

        # Track depth changes.
        if kind == "word":
            if tok in _BLOCK_OPEN:
                block += 1
            elif tok in _BLOCK_CLOSE:
                # ``end`` / ``until`` -- never let it go negative.
                if block > 0:
                    block -= 1
        elif kind == "punc":
            if tok == "(":
                paren += 1
            elif tok == ")":
                if paren > 0:
                    paren -= 1
            elif tok == "{":
                brace += 1
            elif tok == "}":
                if brace > 0:
                    brace -= 1
            elif tok == "[":
                brace += 1
            elif tok == "]":
                if brace > 0:
                    brace -= 1

    if stmt_start is not None:
        stmt = _flush(stmt_start, n)
        if stmt is not None:
            yield stmt


# ---------------------------------------------------------------------------
# Rule (a) -- forbidden top-level statements.
# Rule (b) -- top-level ``local x = EXPR`` with non-side-effect-free RHS.
# ---------------------------------------------------------------------------

# A top-level statement is allowed when it matches one of:
#  * ``local function NAME(...)``
#  * ``function NAME:METHOD(...)`` / ``function NAME.METHOD(...)``
#  * ``local NAME = EXPR`` with side-effect-free EXPR (rule b)
#  * ``return EXPR``
# Anything else is rule (a).

_RE_LOCAL_FUNC = re.compile(r"^\s*local\s+function\s+[\w.]+\s*\(")
_RE_TOP_FUNC = re.compile(r"^\s*function\s+[\w.]+\s*[:.]\s*\w+\s*\(")
_RE_TOP_FUNC_BARE = re.compile(r"^\s*function\s+\w+\s*\(")  # ``function FOO(...)``
_RE_LOCAL_ASSIGN = re.compile(
    r"^\s*local\s+([\w,\s]+?)\s*=\s*(.*)$",
    re.DOTALL,
)
# ``Class.__index = Class`` / ``Module.SomeFlag = true`` style table-field
# assignment. Note the ``\.`` -- a bare ``NAME = EXPR`` (no dot) is a global
# write at top level, which IS rule (a). Rule (c) and (f) catch the
# lifecycle / Unity-callback sub-cases independently.
_RE_TABLE_FIELD_ASSIGN = re.compile(
    r"^\s*[\w.]+\.\w+\s*=\s*(.*)$",
    re.DOTALL,
)
_RE_RETURN = re.compile(r"^\s*return\b")


def _check_top_level_statements(statements, stripped: str) -> list[Violation]:
    out: list[Violation] = []
    for stmt in statements:
        text = stmt.text
        if _RE_LOCAL_FUNC.match(text):
            continue
        if _RE_TOP_FUNC.match(text) or _RE_TOP_FUNC_BARE.match(text):
            continue
        if _RE_RETURN.match(text):
            continue
        m = _RE_LOCAL_ASSIGN.match(text)
        if m:
            rhs = m.group(2).strip()
            if _is_side_effect_free(rhs):
                continue
            out.append(Violation(
                rule="b",
                line=stmt.line,
                message=(
                    "top-level ``local`` binds a non-side-effect-free "
                    "expression (calls, ``:Connect``, ``:Clone()``, or a "
                    "table constructor containing one). Move into a "
                    "function body."
                ),
            ))
            continue
        m = _RE_TABLE_FIELD_ASSIGN.match(text)
        if m:
            rhs = m.group(1).strip()
            if _is_side_effect_free(rhs):
                # Rule (c) handles lifecycle-key non-function assignment;
                # rule (f) handles Unity callback bindings. Both run as
                # independent passes -- if this RHS is side-effect-free
                # and not lifecycle / callback, accept here.
                continue
            out.append(Violation(
                rule="b",
                line=stmt.line,
                message=(
                    "top-level table-field assignment binds a "
                    "non-side-effect-free expression. The RHS of a "
                    "module-scope assignment must be a literal, function, "
                    "``require``, or side-effect-free table."
                ),
            ))
            continue
        out.append(Violation(
            rule="a",
            line=stmt.line,
            message=(
                "module-scope statement that is not a comment, a "
                "function definition, a side-effect-free ``local``, or "
                "the final ``return``. Move into ``Awake`` / ``Start``."
            ),
        ))
    return out


def _is_side_effect_free(expr: str) -> bool:
    """Conservative check for rule (b).

    Side-effect-free RHS shapes:
      * literal (number, string, boolean, nil)
      * function definition ``function(...) ... end``
      * ``require("...")`` (contract-compliant module reference)
      * table constructor ``{ ... }`` whose every value is recursively
        side-effect-free
      * a simple identifier or dotted lookup ``foo`` / ``foo.bar``
      * arithmetic / concatenation of side-effect-free pieces

    Over-rejects on borderline calls; the reprompt resolves those.
    """
    expr = expr.strip()
    if not expr:
        return True
    # Literal.
    if re.match(r"^(?:nil|true|false|-?\d[\w.]*)$", expr):
        return True
    # String literal RHS. The stripper has already blanked the contents
    # of every string, so a string-literal RHS lands here as pure
    # whitespace -- and the ``expr.strip()`` at function entry returned
    # ``""`` (handled by the ``if not expr: return True`` branch above).
    # ``function(...) ... end`` -- whole RHS is a function literal.
    if re.match(r"^function\s*\(", expr) and expr.rstrip().endswith("end"):
        return True
    # ``require("@scene_runtime/Foo")`` etc. After string stripping the
    # call becomes ``require(    )`` with whitespace where the string was.
    # The require-resolution pass (a separate post-transpile step) audits
    # the actual string content; here we only confirm the call shape.
    if re.match(r"^require\s*\(\s*\)$", expr):
        return True
    # Plain identifier / dotted lookup, no parens.
    if re.match(r"^[A-Za-z_][\w.]*$", expr):
        return True
    # Table constructor.  Recurse over each value position.
    if expr.startswith("{") and expr.rstrip().endswith("}"):
        inner = expr[1:expr.rfind("}")]
        return _table_values_are_side_effect_free(inner)
    # Arithmetic / concat of pieces -- best effort split on operators.
    # Split on ``+``/``-``/``*``/``/``/``..`` at the top level (no parens).
    pieces = _split_top_level(expr, ["+", "-", "*", "/", "%", ".."])
    if len(pieces) > 1:
        return all(_is_side_effect_free(p) for p in pieces)
    # Anything else (calls, ``:Method(...)``, ``something:Connect``) is a
    # potential side-effect.
    return False


def _table_values_are_side_effect_free(inner: str) -> bool:
    """For a table constructor ``{ k1 = v1, k2 = v2, ... }``, every value
    position must be side-effect-free. Keys are simple names by
    convention; we only audit values.
    """
    # Split on top-level commas (not inside nested ``{ }`` / ``(...)``).
    depth = 0
    pieces: list[str] = []
    last = 0
    for i, ch in enumerate(inner):
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            if depth > 0:
                depth -= 1
        elif ch == "," and depth == 0:
            pieces.append(inner[last:i])
            last = i + 1
    pieces.append(inner[last:])
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        # ``k = v`` -- audit ``v``. Bare positional entry -- audit whole.
        m = re.match(r"^[\w.\[\]\"']+\s*=\s*(.*)$", piece, re.DOTALL)
        value = m.group(1).strip() if m else piece
        if not _is_side_effect_free(value):
            return False
    return True


def _split_top_level(expr: str, ops: list[str]) -> list[str]:
    """Split ``expr`` on any operator in ``ops`` at zero paren/brace depth."""
    pieces: list[str] = []
    last = 0
    depth = 0
    i = 0
    while i < len(expr):
        ch = expr[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth > 0:
                depth -= 1
        elif depth == 0:
            for op in ops:
                if expr.startswith(op, i):
                    # Avoid binary-vs-unary ambiguity for ``-``/``+``: only
                    # split when there's a non-operator on the left.
                    left = expr[last:i].strip()
                    if left and not re.match(r"[+\-*/%.]$", left):
                        pieces.append(left)
                        last = i + len(op)
                        i += len(op) - 1
                    break
        i += 1
    pieces.append(expr[last:])
    return pieces


# ---------------------------------------------------------------------------
# Rule (c) -- a lifecycle key assigned a non-function value.
#
# Two shapes to flag:
#   ``Class.Awake = "string"``     -- top-level (or any) assignment
#   ``Class.Awake = SomeIdent``    -- assignment whose RHS isn't a fn literal
# ``function Class:Awake(...) ... end`` is the canonical good shape and
# is already accepted by the top-level pass.
# ---------------------------------------------------------------------------

_RE_LIFECYCLE_DOT_ASSIGN = re.compile(
    r"^\s*([\w.]+)\.(\w+)\s*=\s*(.*)$",
    re.DOTALL,
)


def _check_lifecycle_assignments(statements, stripped: str) -> list[Violation]:
    out: list[Violation] = []
    for stmt in statements:
        m = _RE_LIFECYCLE_DOT_ASSIGN.match(stmt.text)
        if not m:
            continue
        method = m.group(2)
        if method not in LIFECYCLE_METHODS:
            continue
        rhs = m.group(3).strip().rstrip(";").rstrip()
        # ``Class.Awake = function(...) ... end`` -- the only legal RHS shape.
        if re.match(r"^function\s*\(", rhs) and rhs.endswith("end"):
            continue
        out.append(Violation(
            rule="c",
            line=stmt.line,
            message=(
                f"lifecycle key ``{method}`` assigned a non-function value. "
                f"Define it as ``function Class:{method}(...)`` or assign "
                f"a ``function(...) ... end`` literal."
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# Rule (d) -- missing top-level table return.
# ---------------------------------------------------------------------------

def _check_module_return(statements, source: str) -> list[Violation]:
    for stmt in statements:
        if _RE_RETURN.match(stmt.text):
            return []
    line = source.count("\n") + 1
    return [Violation(
        rule="d",
        line=line,
        message=(
            "ModuleScript has no top-level ``return`` statement. Every "
            "scene-runtime module must end with ``return <class table>``."
        ),
    )]


# ---------------------------------------------------------------------------
# Rule (e) -- constructor purity. ``new(self, config)`` body cannot read
# ``self.host`` or call ``self:GetComponent(...)``: the host surface isn't
# bound until after ``new()`` returns.
# ---------------------------------------------------------------------------

# Match ``function CLASS:new(...)`` / ``function CLASS.new(self, ...)`` /
# ``new = function(self, ...)`` (table-literal entry). Capture the body
# slice up to the matching ``end``.
_RE_CONSTRUCTOR_METHOD = re.compile(
    r"function\s+[\w.]+\s*[:.]\s*new\s*\(",
)
_RE_CONSTRUCTOR_LITERAL = re.compile(
    r"\bnew\s*=\s*function\s*\(",
)

_FORBIDDEN_IN_NEW = [
    (re.compile(r"\bself\.host\b"),
     "constructor body reads ``self.host``"),
    (re.compile(r"\bself\s*:\s*GetComponent\b"),
     "constructor body calls ``self:GetComponent``"),
]


def _check_constructor_purity(stripped: str, source: str) -> list[Violation]:
    out: list[Violation] = []
    for pat in (_RE_CONSTRUCTOR_METHOD, _RE_CONSTRUCTOR_LITERAL):
        for m in pat.finditer(stripped):
            body, body_start = _extract_function_body(stripped, m.end())
            if body is None:
                continue
            for bad_re, message in _FORBIDDEN_IN_NEW:
                inner_match = bad_re.search(body)
                if inner_match is None:
                    continue
                abs_offset = body_start + inner_match.start()
                line = source.count("\n", 0, abs_offset) + 1
                out.append(Violation(
                    rule="e",
                    line=line,
                    message=(
                        f"{message}. The host surface isn't bound until "
                        f"after ``new()`` returns -- move this access "
                        f"into ``Awake`` or later."
                    ),
                ))
    return out


def _extract_function_body(stripped: str, args_start: int):
    """Given a position just after ``function ... (``, return
    ``(body_text, body_start_offset)`` -- the body slice between the
    closing ``)`` and the matching ``end``. ``(None, None)`` on parse
    failure (unbalanced).
    """
    # Skip past the argument list ``...)``.
    depth = 1
    i = args_start
    while i < len(stripped):
        ch = stripped[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    if depth != 0:
        return None, None
    body_start = i
    # Now walk until matching ``end``. Track block depth -- ``function`` /
    # ``do`` / ``then`` / ``repeat`` open, ``end`` / ``until`` close.
    block = 1
    for tok_match in _TOKEN_RE.finditer(stripped, i):
        kind = tok_match.lastgroup
        if kind != "word":
            continue
        tok = tok_match.group()
        if tok in _BLOCK_OPEN:
            block += 1
        elif tok in _BLOCK_CLOSE:
            block -= 1
            if block == 0:
                return stripped[body_start:tok_match.start()], body_start
    return None, None


# ---------------------------------------------------------------------------
# Rule (f) -- Unity-message-callback binding on the class table.
#
# Three shapes to reject:
#   1. ``function CLASS:OnTriggerEnter(...)``
#   2. ``CLASS.OnTriggerEnter = function(...)``
#   3. ``return { OnTriggerEnter = function(...) ... end }`` -- a
#      table-literal entry on the returned class table.
# The reprompt teaches: wire via ``self.host:connect(signal, fn)`` in
# ``Awake`` instead (Piece 6).
# ---------------------------------------------------------------------------

# Shape 1: ``function CLASS:NAME(...)`` -- CLASS is any identifier; we
# don't try to verify it's the module's class table (a class-method-shape
# binding of a Unity callback name is wrong regardless of whose method
# table it lands on).
_RE_F_METHOD = re.compile(
    r"function\s+[\w.]+\s*[:.]\s*(\w+)\s*\(",
)

# Shape 2: ``CLASS.NAME = function(...)``  (or any ident as LHS table)
_RE_F_DOT_ASSIGN = re.compile(
    r"[\w.]+\s*\.\s*(\w+)\s*=\s*function\s*\(",
)


def _check_unity_message_callbacks(statements, stripped: str, source: str) -> list[Violation]:
    out: list[Violation] = []
    seen: set[tuple[int, str]] = set()

    # Shapes 1 + 2 -- scan the whole stripped source. These fire wherever
    # the binding appears (top-level, nested doesn't really matter --
    # Roblox doesn't name-dispatch these regardless).
    for pat in (_RE_F_METHOD, _RE_F_DOT_ASSIGN):
        for m in pat.finditer(stripped):
            name = m.group(1)
            if name not in UNITY_MESSAGE_CALLBACKS:
                continue
            line = source.count("\n", 0, m.start()) + 1
            key = (line, name)
            if key in seen:
                continue
            seen.add(key)
            out.append(Violation(
                rule="f",
                line=line,
                message=(
                    f"Unity message callback ``{name}`` is bound on the "
                    f"class table -- Roblox does not name-dispatch these. "
                    f"Wire the equivalent via ``self.host:connect(...)`` "
                    f"inside ``Awake`` instead."
                ),
            ))

    # Shape 3 -- a returned table literal ``return { Name = function(...) ... end }``.
    out.extend(_check_returned_table_literal(statements, stripped, source, seen))
    return out


def _check_returned_table_literal(statements, stripped: str, source: str, seen: set[tuple[int, str]]) -> list[Violation]:
    out: list[Violation] = []
    for stmt in statements:
        if not _RE_RETURN.match(stmt.text):
            continue
        # Look for a ``{ ... }`` table constructor inside the return.
        slice_text = stripped[stmt.start:stmt.end]
        brace = slice_text.find("{")
        if brace == -1:
            continue
        # Walk keys -- ``NAME = function`` inside the constructor.
        body = slice_text[brace + 1:slice_text.rfind("}")]
        for m in re.finditer(r"\b(\w+)\s*=\s*function\s*\(", body):
            name = m.group(1)
            if name not in UNITY_MESSAGE_CALLBACKS:
                continue
            abs_offset = stmt.start + brace + 1 + m.start()
            line = source.count("\n", 0, abs_offset) + 1
            key = (line, name)
            if key in seen:
                continue
            seen.add(key)
            out.append(Violation(
                rule="f",
                line=line,
                message=(
                    f"Unity message callback ``{name}`` is bound on the "
                    f"returned table -- Roblox does not name-dispatch "
                    f"these. Wire the equivalent via "
                    f"``self.host:connect(...)`` inside ``Awake`` instead."
                ),
            ))
    return out
