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

def verify_module(
    source: str, *, is_player_controller: bool = False,
) -> VerificationResult:
    """Verify a Luau ModuleScript against the generic-runtime contract.

    Args:
        source: The full Luau source as a single string.
        is_player_controller: When ``True``, additionally run the two
            paradigm-B player rejects (rule ``p1`` -- a direct
            ``workspace.CurrentCamera.CFrame``/``CameraType`` write; rule
            ``p2`` -- a direct ``Humanoid:Move(`` call). These rules are
            NON-load-bearing: the host owns camera + locomotion via
            ``self.host.player`` (paradigm C), so a surviving player reject
            warns + fails OPEN (the caller tags it ``contract-verifier-player``),
            never fail-closed. Default ``False`` preserves every existing
            (non-player) call site -- only the 8 contract rules run.

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
    violations.extend(_check_constructor_purity(stripped, source, statements))
    violations.extend(_check_unity_message_callbacks(statements, stripped, source))
    violations.extend(_check_gameobject_touch(stripped, source))
    violations.extend(_check_script_parent(stripped, source))
    # Phase 1 (relation #8): runs for EVERY generic module (not gated on player), but is
    # NON-load-bearing -- a surviving ``im`` reject fails OPEN (tagged ``contract-verifier-impulse``).
    violations.extend(_check_raw_apply_impulse(stripped, source))
    # Slice 2.3 (relation #8): the narrow ``fc`` rule (nonexistent
    # ``FindFirstChildOfType``) is RETIRED -- it lived on this bypassable
    # transpile-time path (mode/cache/route) and shipped the bug as success=True.
    # The universal provenance-gated net
    # (``semantic_validators.nonexistent_roblox_method`` ->
    # ``roblox_call_validator.find_invalid_roblox_calls``) runs in ``write_output``
    # on EVERY final script and catches any hallucinated Roblox method, so it
    # subsumes ``fc``.
    if is_player_controller:
        violations.extend(_check_player_camera_write(stripped, source))
        violations.extend(_check_player_humanoid_move(stripped, source))

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

# Luau supports ``if`` as an EXPRESSION (``x = if cond then a else b``)
# which has NO closing ``end`` -- distinct from the ``if`` STATEMENT
# (``if cond then ... end``). Treating every ``if`` as a block-opener
# over-counts depth on every if-expression and corrupts every rule that
# consumes ``_iter_top_level_statements`` (rule d false-positives the
# top-level return, etc.). The AI emits if-expressions naturally on config
# defaults, so without this gate any module with one fails closed.
#
# An ``if`` is an EXPRESSION when the previous significant token is in an
# expression-introducing position: ``=`` / ``(`` / ``,`` / ``{`` / ``[``
# (RHS, argument, element), an arithmetic / comparison / concat operator,
# or one of ``return`` / ``and`` / ``or`` / ``not``. Anything else (block
# boundary, ``then`` / ``else`` / ``do`` keyword body, file start) starts
# an if-STATEMENT.
_EXPR_IF_PRECEDING_CHARS = frozenset("=,({[+*/%^<>~|")
_EXPR_IF_PRECEDING_WORDS = frozenset({"return", "and", "or", "not"})


def _is_expression_if(stripped: str, if_pos: int) -> bool:
    """True iff the ``if`` token at ``if_pos`` introduces a Luau
    if-EXPRESSION (no closing ``end``) rather than an if-statement."""
    i = if_pos - 1
    while i >= 0 and stripped[i].isspace():
        i -= 1
    if i < 0:
        return False
    ch = stripped[i]
    if ch in _EXPR_IF_PRECEDING_CHARS:
        return True
    # Word: walk back over identifier chars and check membership. ``-`` is
    # deliberately NOT in the char set: a unary ``-`` precedes an
    # if-expression but a statement-level binary subtraction can also
    # precede a fresh ``if`` statement on the next line; biasing toward
    # statement-if (over-count) on ``-`` matches valid code more often than
    # biasing toward expression-if (under-count would miss real if-blocks).
    if ch.isalnum() or ch == "_":
        j = i
        while j >= 0 and (stripped[j].isalnum() or stripped[j] == "_"):
            j -= 1
        word = stripped[j + 1:i + 1]
        return word in _EXPR_IF_PRECEDING_WORDS
    return False


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
                if tok == "if" and _is_expression_if(stripped, m.start()):
                    pass  # Luau if-EXPRESSION: no ``end`` to match.
                else:
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

# Match ``function CLASS.new(self, ...)`` (canonical dot form per spec) and
# ``new = function(self, ...)`` (table-literal entry). The host runtime calls
# ``module_table.new(config)`` (see scene_runtime.luau:_buildComponent); a
# colon-form ``function CLASS:new(config)`` would bind ``config`` to ``self``
# and silently lose the real config, so it's rejected as a SHAPE violation
# (``_RE_CONSTRUCTOR_COLON_FORM`` below) -- but ONLY when ``CLASS`` is the
# exported module table. Internal helper classes can use colon-form freely
# because the runtime never calls their ``.new`` as the module constructor.
_RE_CONSTRUCTOR_METHOD = re.compile(
    r"function\s+([\w.]+)\s*\.\s*new\s*\(",
)
_RE_CONSTRUCTOR_LITERAL = re.compile(
    r"\bnew\s*=\s*function\s*\(",
)
_RE_CONSTRUCTOR_COLON_FORM = re.compile(
    r"function\s+([\w.]+)\s*:\s*new\s*\(",
)
# Identify the exported class from the module's TERMINAL top-level
# return statement. Three recognized shapes:
#   - bare identifier:        ``return X``        / ``return Mod.Sub``
#   - 2-arg setmetatable OO:  ``return setmetatable(X, mt)`` / ``...(Mod.Sub, mt)``
#   - 1-arg setmetatable:     ``return setmetatable(X)``    (legal Lua: self-mt)
# Dotted names: the runtime's ``module_table.new(config)`` call binds on
# the rightmost segment's table (Lua method-binding semantics), so we
# strip the dotted prefix in ``_exported_class_name`` and compare against
# the same suffix produced from each colon-form constructor's class name.
# Anything else (table literal, factory call, multi-value) yields no
# name and we fall back to the conservative rule.
_RE_RETURN_BARE_IDENT = re.compile(
    r"^\s*return\s+([\w.]+)\s*$",
)
_RE_RETURN_SETMETATABLE = re.compile(
    r"^\s*return\s+setmetatable\s*\(\s*([\w.]+)\s*[,)]",
)

_FORBIDDEN_IN_NEW = [
    (re.compile(r"\bself\.host\b"),
     "constructor body reads ``self.host``"),
    (re.compile(r"\bself\s*:\s*GetComponent\b"),
     "constructor body calls ``self:GetComponent``"),
]


def _exported_class_name(statements) -> str | None:
    """Return the identifier the module exports via its terminal
    top-level ``return``, or ``None`` if the return shape doesn't
    surface a name (table literal, factory call, multi-value, etc.).

    Walks ``statements`` (the parsed top-level statement list, from
    ``_iter_top_level_statements``) so nested function bodies cannot
    inject a misleading ``return Helper`` that re-binds the export.

    For dotted names (``return Mod.Sub`` /
    ``return setmetatable(Mod.Sub, mt)``), returns the rightmost
    segment (``"Sub"``) -- that's what Lua binds the colon-form
    method on, and what the runtime's ``module_table.new(config)``
    call effectively addresses.
    """
    last_return_text: str | None = None
    for stmt in statements:
        if _RE_RETURN.match(stmt.text):
            last_return_text = stmt.text
    if last_return_text is None:
        return None
    m = _RE_RETURN_BARE_IDENT.match(last_return_text)
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    m = _RE_RETURN_SETMETATABLE.match(last_return_text)
    if m:
        return m.group(1).rsplit(".", 1)[-1]
    return None


def _check_constructor_purity(stripped: str, source: str, statements) -> list[Violation]:
    out: list[Violation] = []

    # Identify the exported class from the module's TERMINAL top-level
    # return -- inspected via the parsed statement list so a nested
    # function's own ``return Helper`` can't misanchor the check.
    exported_class = _exported_class_name(statements)

    # Conservative fallback used when the export is a shape we can't
    # name-anchor on (table literal, factory call, multi-value return).
    has_canonical_constructor = (
        _RE_CONSTRUCTOR_METHOD.search(stripped) is not None
        or _RE_CONSTRUCTOR_LITERAL.search(stripped) is not None
    )

    # Shape violation: colon-form constructors break the runtime's
    # ``module_table.new(config)`` call (config becomes self).
    for m in _RE_CONSTRUCTOR_COLON_FORM.finditer(stripped):
        cls_name = m.group(1)
        # Strip any dotted prefix (``Foo.Bar:new`` -> ``Bar``); colon-form
        # ``function A.B:new(...)`` is sugar for ``A.B.new(self, ...)``
        # which binds the method on the rightmost segment's table.
        cls_tail = cls_name.rsplit(".", 1)[-1]

        if exported_class is not None:
            # We know which class is exported. Flag ONLY when this
            # constructor IS the exported one. Helper classes with
            # colon-form constructors are intentional and never reached
            # by the runtime's ``module_table.new(config)`` call.
            if cls_tail != exported_class:
                continue
            # Edge case kept rejected: a module that declares BOTH
            # ``function X.new(config)`` AND ``function X:new(config)``
            # still has the colon-form OVERWRITE the dot-form on the
            # class table (Lua's later-write-wins). The runtime then
            # calls ``X.new(config)`` and ``config`` binds to ``self``
            # anyway. Fall through to emit the violation.
        else:
            # No name on the return -- fall back to the conservative
            # rule: allow colon-form when any canonical dot-form /
            # literal constructor exists (it may be the exported one).
            # Reject when colon-form is the only shape in sight.
            if has_canonical_constructor:
                continue

        line = source.count("\n", 0, m.start()) + 1
        out.append(Violation(
            rule="e",
            line=line,
            message=(
                "constructor declared with colon form ``Class:new(...)``. "
                "The host calls ``module_table.new(config)`` (dot form); "
                "a colon-form constructor receives ``config`` as ``self`` "
                "and silently drops the real config. Use "
                "``function Class.new(config)`` instead."
            ),
        ))
    # Purity violation: host surface isn't bound until after new() returns.
    # ALL three constructor shapes get the sweep -- a helper class's
    # colon-form ``Helper:new()`` can still read ``self.host`` (which is
    # nil at construction time) and crash at boot.
    for pat in (_RE_CONSTRUCTOR_METHOD, _RE_CONSTRUCTOR_LITERAL, _RE_CONSTRUCTOR_COLON_FORM):
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
            if tok == "if" and _is_expression_if(stripped, tok_match.start()):
                continue  # Luau if-expression: no ``end`` to match.
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


# ---------------------------------------------------------------------------
# Rule (g) -- GameObject ``.Touched`` / ``.TouchEnded`` on a raw GameObject.
#
# ``self.gameObject`` is frequently a Roblox ``Model`` (prefab placement);
# ``.Touched`` / ``.TouchEnded`` are ``BasePart``-only signals and throw on
# a Model (``Touched is not a valid member of Model``). The contract-
# compliant shape resolves a touch part through the host:
#
#   self.host:connectGameObjectSignal(self.gameObject, "Touched", fn)
#
# Flag any ``<expr>.gameObject.Touched`` / ``.TouchEnded`` member access
# (the broken idiom passed straight to ``host:connect``). The host helper
# uses a string signal name, so the compliant shape never names ``.Touched``
# as a member access -- this fires only on the broken pattern.
# ---------------------------------------------------------------------------

_RE_G_GAMEOBJECT_TOUCH = re.compile(
    r"\.\s*gameObject\s*\.\s*(Touched|TouchEnded)\b",
)


def _check_gameobject_touch(stripped: str, source: str) -> list[Violation]:
    out: list[Violation] = []
    for m in _RE_G_GAMEOBJECT_TOUCH.finditer(stripped):
        signal = m.group(1)
        line = source.count("\n", 0, m.start()) + 1
        out.append(Violation(
            rule="g",
            line=line,
            message=(
                f"``self.gameObject.{signal}`` accesses a BasePart-only "
                f"signal on a GameObject that may be a Model (it throws on "
                f"a Model). Use "
                f"``self.host:connectGameObjectSignal(self.gameObject, "
                f"\"{signal}\", fn)`` instead, which resolves a touch part."
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# Rule (h) -- ``script.Parent`` (the legacy Roblox idiom) in a host-bound
# component module.
#
# A scene-runtime module is a class table the host ``require``s and
# instantiates, handing it ``self.gameObject``. It has NO ``script.Parent``
# relationship to the instance it drives -- a component class is shared by
# every instance, and (when emitted as a detached ``Script``) ``script.Parent``
# is whatever container the file landed in, not the GameObject. The legacy
# transpile mode produces ``script.Parent`` / ``script.Parent.CFrame`` and
# those throw at runtime (e.g. ``CFrame`` is nil on ServerScriptService).
# A component MUST reach its instance through ``self.gameObject`` instead.
#
# ``\bscript\b`` anchors so identifiers like ``myscript.Parent`` don't match;
# runs on the stripped source so a ``script.Parent`` in a string/comment is
# not flagged. This is a BACKSTOP for the common legacy shape, not a complete
# static guarantee -- the primary enforcement is routing components through
# the generic prompt (which never emits ``script.Parent``). It is biased
# toward FEWER false positives (a false fail-closed would sink an otherwise
# good conversion now that fail-closed promotes to a hard error): if the
# module shadows ``script`` with a local of the same name, ``script.Parent``
# is field access on that local, not the Roblox global, so the check is
# skipped entirely.
# ---------------------------------------------------------------------------

# Scope-aware token scan: ``script`` shadowed by a ``local`` only suppresses
# violations within the SAME lexical scope (or an outer scope still in scope).
# A harmless ``local script = self.config`` inside one function must not mask
# a real ``script.Parent`` global access elsewhere in the module -- the
# original module-wide bail was the Codex P3 finding on Fix #15. The walker
# maintains a stack of scopes (one bool per enclosing block; True means
# ``script`` is shadowed at this scope) and pushes/pops on Lua block
# keywords.
#
# ``repeat...until`` has a special wrinkle: locals declared inside the
# ``repeat`` block remain in scope for the ``until`` condition expression.
# Popping on the ``until`` keyword itself would falsely flag
# ``repeat local script = ... until script.Parent`` (Codex R2 finding). We
# defer the pop to the first token whose position is past the end of the
# until-condition line -- in practice every realistic until-condition fits
# on a single line, and the safe direction (delaying the pop) errs toward
# fewer false positives, matching the rule's existing bias.
_RE_H_TOKENS = re.compile(
    r"\blocal\s+script\b"
    r"|\bscript\s*\.\s*Parent\b"
    r"|\bfunction\b"
    r"|\bdo\b"
    r"|\bthen\b"
    r"|\brepeat\b"
    r"|\belseif\b"
    r"|\belse\b"
    r"|\bend\b"
    r"|\buntil\b"
)


def _check_script_parent(stripped: str, source: str) -> list[Violation]:
    out: list[Violation] = []
    scopes: list[bool] = [False]  # module scope at index 0
    # Position past which a pending repeat-scope pop should fire. The pop
    # is triggered before processing the first token at or beyond this
    # position (effectively "after the until expression's line").
    pending_repeat_pop_at: int | None = None

    for m in _RE_H_TOKENS.finditer(stripped):
        if (pending_repeat_pop_at is not None
                and m.start() >= pending_repeat_pop_at):
            if len(scopes) > 1:
                scopes.pop()
            pending_repeat_pop_at = None

        tok = m.group(0)
        if tok.startswith("local"):
            scopes[-1] = True
        elif tok.startswith("script"):
            if any(scopes):
                continue
            line = source.count("\n", 0, m.start()) + 1
            out.append(Violation(
                rule="h",
                line=line,
                message=(
                    "``script.Parent`` is the legacy Roblox idiom and does not "
                    "exist for a host-instantiated component module: the class "
                    "table is shared across instances and has no parent edge to "
                    "the GameObject it drives. Reach the instance through "
                    "``self.gameObject`` (and its host helpers) instead."
                ),
            ))
        elif tok in ("function", "do", "then", "repeat"):
            scopes.append(False)
        elif tok == "end":
            if len(scopes) > 1:
                scopes.pop()
        elif tok == "until":
            # Defer pop -- locals declared in the repeat block remain in
            # scope through the until condition expression. The expression
            # is taken to end at the next newline (line-based heuristic).
            if len(scopes) > 1 and pending_repeat_pop_at is None:
                eol = stripped.find("\n", m.end())
                pending_repeat_pop_at = eol if eol != -1 else len(stripped)
        elif tok == "else":
            if len(scopes) > 1:
                scopes.pop()
            scopes.append(False)
        elif tok == "elseif":
            if len(scopes) > 1:
                scopes.pop()
            # The following ``then`` pushes the new branch scope.

    return out


# ---------------------------------------------------------------------------
# Phase 1 (relation #8) -- raw linear impulse (rule ``im``) -- NON-load-bearing.
#
# A raw ``part:ApplyImpulse(...)`` applies the AI's Unity force against the
# STUDS_PER_METER³-inflated Roblox mass, so a force-launched body barely moves.
# The faithful launch routes through ``self.host.applyImpulse(part, force)`` (the
# host applies ``Δv = (force / _UnityMass) * STUDS_PER_METER``). An unrouted
# impulse only DEGRADES launch faithfulness (it does not crash), so a surviving
# reject warns + fails OPEN (caller tags ``contract-verifier-impulse``) -- it
# reprompts but never knocks a module out of generic mode.
#
# Scope is LINEAR ApplyImpulse only: ``ApplyImpulseAtPosition`` /
# ``ApplyAngularImpulse`` have other chars after ``ApplyImpulse`` so the regex
# never matches them, and the host call ``self.host.applyImpulse`` is a dot /
# lowercase-``a`` form so it is never matched either.
# ---------------------------------------------------------------------------

# Whitespace-tolerant: Luau allows spaces around the ``:`` and before ``(``
# (``rb:ApplyImpulse(``, ``rb : ApplyImpulse (``). ``ApplyImpulseAtPosition`` /
# ``ApplyAngularImpulse`` have other chars after ``ApplyImpulse`` so ``\s*\(`` never matches them.
_RE_RAW_APPLY_IMPULSE = re.compile(r":\s*ApplyImpulse\s*\(")
# A method DEFINITION ``function X:ApplyImpulse(...)`` is not a call — don't flag it. The trailing
# ``\s*`` tolerates the whitespace-legal ``function C : ApplyImpulse`` form (prefix ends ``function C ``).
_RE_FUNC_DEF_PREFIX = re.compile(r"function\s+[\w.]*\s*$")


def _is_method_def_call(stripped: str, pos: int) -> bool:
    """True when the colon-call match at ``pos`` is actually a method DEFINITION
    (``function <receiver>:Name(...)``). Scans the WHOLE current line before the colon (not a fixed
    lookback) so a long receiver chain — e.g. ``function A.B.C:Name(...)`` — is still recognized."""
    line_start = stripped.rfind("\n", 0, pos) + 1
    return _RE_FUNC_DEF_PREFIX.search(stripped[line_start:pos]) is not None


def _check_raw_apply_impulse(stripped: str, source: str) -> list[Violation]:
    out: list[Violation] = []
    for m in _RE_RAW_APPLY_IMPULSE.finditer(stripped):
        if _is_method_def_call(stripped, m.start()):
            continue  # ``function Class:ApplyImpulse(...)`` definition, not a call
        line = source.count("\n", 0, m.start()) + 1
        out.append(Violation(
            rule="im",
            line=line,
            message=(
                "raw ``:ApplyImpulse(`` does not apply the Unity->Roblox launch-velocity "
                "scaling: a force-launched body barely moves against the inflated Roblox mass. "
                "Route the linear impulse through ``self.host.applyImpulse(part, force)`` (the host "
                "applies the faithful stud-scaled velocity); never call ``:ApplyImpulse(`` directly."
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# Paradigm-B player rejects (rules ``p1`` / ``p2``) -- NON-load-bearing.
#
# Run ONLY when ``verify_module(..., is_player_controller=True)``, i.e. for
# the script the deterministic upstream player identity
# (``has_character_controller``) selects. The host owns camera + locomotion
# via ``self.host.player`` (paradigm C, which dominates by last-writer
# ordering regardless), so a player controller that ALSO writes the camera
# or moves the Humanoid directly is a conflict the directive asks it to drop.
# A surviving reject warns + fails OPEN (caller tags ``contract-verifier-
# player``); C binds anyway. Both checks scan ``stripped`` (string/comment-
# blanked) so a match inside a literal/comment is not a false positive, and
# map line numbers via ``source`` -- identical to every other ``_check_*``.
#
# PivotTo is deliberately NOT rejected: it serves yaw / translate / respawn in
# the same script and is not lexically separable (a conservative-lexical
# verifier cannot tell a movement PivotTo from a respawn one).
# ---------------------------------------------------------------------------

# ``workspace.CurrentCamera.CFrame =`` (or ``.CameraType =``) -- a direct
# write to the host-owned camera. Whitespace-tolerant around the dots and the
# ``=``. The ``(?!=)`` negative lookahead excludes an equality test
# (``... CFrame == x``): a single ``=`` not followed by another ``=`` is an
# assignment; ``==`` is a comparison, not a write.
_RE_P1_CAMERA_WRITE = re.compile(
    r"workspace\s*\.\s*CurrentCamera\s*\.\s*(CFrame|CameraType)\s*=(?!=)",
)


def _check_player_camera_write(stripped: str, source: str) -> list[Violation]:
    out: list[Violation] = []
    for m in _RE_P1_CAMERA_WRITE.finditer(stripped):
        member = m.group(1)
        line = source.count("\n", 0, m.start()) + 1
        out.append(Violation(
            rule="p1",
            line=line,
            message=(
                f"Direct ``workspace.CurrentCamera.{member}`` write -- the "
                f"host owns the camera via ``self.host.player``; do not write "
                f"CurrentCamera in the player controller. For aim use "
                f"``self.host.player:getLookCFrame()``."
            ),
        ))
    return out


# ``Humanoid:Move(`` -- a direct locomotion call on the literal ``Humanoid``.
# Whitespace-tolerant around the ``:`` and the ``(``. A ``hum:Move(`` local
# alias is intentionally NOT matched (the directive's literal is
# ``Humanoid:Move``; B is non-load-bearing and C dominates the move write).
_RE_P2_HUMANOID_MOVE = re.compile(
    r"\bHumanoid\s*:\s*Move\s*\(",
)


def _check_player_humanoid_move(stripped: str, source: str) -> list[Violation]:
    out: list[Violation] = []
    for m in _RE_P2_HUMANOID_MOVE.finditer(stripped):
        line = source.count("\n", 0, m.start()) + 1
        out.append(Violation(
            rule="p2",
            line=line,
            message=(
                "Direct ``Humanoid:Move(`` call -- the host owns locomotion "
                "via ``self.host.player``; do not call ``Humanoid:Move`` in "
                "the player controller."
            ),
        ))
    return out
