"""Phase 4.4 — method-completeness diagnostic for transpiled scripts.

Compares the C# source's method list against the AI-produced Luau
output and flags methods that disappear silently — neither present as a
Luau function nor marked with a ``-- UNCONVERTED`` / ``-- TODO`` comment.

The old regex-based ``luau_validator.py`` was deleted 2026-04-18 and is
NOT resurrected here. This is a pure diagnostic — it emits warnings that
surface in the JSON conversion report, and it neither rewrites nor
validates Luau syntax. Syntax validation stays with ``luau-analyze``
plus the reprompt loop in ``code_transpiler._luau_syntax_check``.
"""

from __future__ import annotations

import re


# C# comments + string literals must be stripped before method-name
# extraction — otherwise ``// public void Foo()`` or ``"public void
# Bar()"`` inside a log string registers as a real method declaration.
_CSHARP_COMMENT_OR_STRING = re.compile(
    r"""
    //[^\n]*
    | /\*.*?\*/
    | @"(?:[^"]|"")*"
    | "(?:\\.|[^"\\])*"
    | '(?:\\.|[^'\\])*'
    """,
    re.DOTALL | re.VERBOSE,
)

# C# method declaration. Must come right after a statement boundary
# (start-of-source, ``;``, ``{``, or ``}``) so we don't match identifiers
# inside expressions. Modifiers are optional — default-private methods
# inside a class (``void Helper()``, ``IEnumerator Run()``) qualify too.
# Method-level generics (``TOut Map<TIn>(...)``) are recognized via the
# trailing ``<...>`` after the captured name. The outer-most capture
# group is the method name.
_CSHARP_METHOD_RE = re.compile(
    r"(?:^|[;{}])\s*"
    r"(?:\[[^\]]*\]\s*)*"
    r"(?:(?:public|private|protected|internal|static|override|virtual"
    r"|abstract|async|sealed|new|partial|extern|unsafe|readonly|volatile)\s+)*"
    r"(?:[\w]+(?:<[^()<>]*>)?(?:\[\])?\??)\s+"
    r"(\w+)"
    r"\s*(?:<[\w,\s]*>)?"
    r"\s*\(",
    re.MULTILINE,
)


# C# keywords that look method-shaped but aren't (control flow,
# language constructs). When the regex captures one of these as a
# "method name", drop it.
_CSHARP_KEYWORD_NAMES: frozenset[str] = frozenset({
    "if", "for", "while", "return", "using", "switch", "do", "catch",
    "else", "foreach", "lock", "try", "throw", "yield", "new",
    "ref", "out", "in", "is", "as", "case", "break", "continue",
    "goto", "default", "fixed", "checked", "unchecked", "where",
    "typeof", "sizeof", "stackalloc", "delegate", "operator",
    "void", "var",
})


# Statement-starter keywords that mean "what follows is an expression,
# not a declaration." If one of these appears in the text between the
# matched statement boundary and the captured method name, the match
# is almost certainly a method CALL in RHS (e.g. ``return
# GetComponent<X>();`` inside a property body) rather than a
# definition. Filter those out.
_CALL_SITE_PRECEDING_KEYWORDS = re.compile(
    r"\b(?:return|throw|yield|await|new)\b"
)


# Luau function definition forms — both `function`-keyword declarations
# and assignment-style exports. The repo's transpiled scripts use both:
#   function Class:Method(...)
#   function Class.Method(...)
#   function name(...)
#   local function name(...)
#   Class.method = function(...)
#   Class:method = function(...)
#   _G.Class.method = function(...)
#   bare `name = function(...)` at module top
_LUAU_FUNC_FORMS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bfunction\s+(?:[\w.]+[.:])?(\w+)\s*\(", re.MULTILINE),
    re.compile(r"(?:^|[;{}\s])(?:[\w.]+\.)?(\w+)\s*=\s*function\s*\(", re.MULTILINE),
)


# Unity lifecycle hooks + event-style hooks that the transpiler
# idiomatically lowers into top-level code, RunService connections,
# or part.Touched / part.MouseClick:Connect listeners — never named
# Luau functions. Exempt so check_method_completeness doesn't yell
# at correctly-converted scripts.
#
# See ``api_mappings.LIFECYCLE_MAP`` for the complete set the
# transpiler rewrites; the entries below are the ones that survive
# the regex (i.e. use a method-style C# declaration).
_LIFECYCLE_EXEMPT: frozenset[str] = frozenset({
    # Frame / start hooks (top-level / RunService.Heartbeat)
    "Awake", "Start", "Update", "FixedUpdate", "LateUpdate",
    "OnEnable", "OnDisable", "OnDestroy", "OnApplicationQuit",
    "OnValidate", "OnDrawGizmos", "OnDrawGizmosSelected",
    "Reset", "OnGUI", "Main",
    # Collider hooks → part.Touched / TouchEnded
    "OnCollisionEnter", "OnCollisionStay", "OnCollisionExit",
    "OnTriggerEnter", "OnTriggerStay", "OnTriggerExit",
    "OnCollisionEnter2D", "OnCollisionStay2D", "OnCollisionExit2D",
    "OnTriggerEnter2D", "OnTriggerStay2D", "OnTriggerExit2D",
    # Mouse hooks → ClickDetector / MouseButton1Click
    "OnMouseDown", "OnMouseUp", "OnMouseEnter", "OnMouseExit",
    "OnMouseOver", "OnMouseDrag", "OnMouseUpAsButton",
    # Application / focus
    "OnApplicationFocus", "OnApplicationPause",
    # Particle / animator events
    "OnParticleCollision", "OnParticleTrigger",
    "OnAnimatorIK", "OnAnimatorMove",
})


def _strip_comments_and_strings(source: str) -> str:
    """Collapse comments + string/char literals to a space so they don't
    contribute method names. Same technique used by PR 4's dep-graph
    extractor; duplicated here so this module stays standalone.
    """
    return _CSHARP_COMMENT_OR_STRING.sub(" ", source)


def check_method_completeness(
    csharp_source: str,
    luau_source: str,
    source_name: str = "<script>",
) -> list[str]:
    """Return a warning per C# method missing from the Luau output.

    A method is considered "converted" when either:
      - Its exact name appears as a Luau function definition, OR
      - Its name appears in a ``-- UNCONVERTED …`` or ``-- TODO …``
        comment (intentional drop, honoured per the prompt rule).

    Unity lifecycle hooks (Awake/Start/Update/…) are exempt because
    the transpiler idiomatically lowers them into top-level code or
    ``RunService`` connections rather than named Luau functions.

    Pure function, no I/O.
    """
    if not csharp_source or not luau_source:
        return []

    clean_cs = _strip_comments_and_strings(csharp_source)
    csharp_methods: set[str] = set()
    for match in _CSHARP_METHOD_RE.finditer(clean_cs):
        name = match.group(1)
        # Drop control-flow keywords the regex incidentally captures
        # (``if (cond)`` looks like ``<type> if(...)`` to the engine
        # without context). Drop ``void`` / ``var`` / etc. too —
        # those are return-type tokens, never method names.
        if name in _CSHARP_KEYWORD_NAMES:
            continue
        if name in _LIFECYCLE_EXEMPT:
            continue
        # Filter method CALLS misread as declarations. In
        # ``return GetComponent<X>();`` the regex sees ``return`` as
        # return-type and ``GetComponent`` as the method name.
        # Scanning the text between the statement boundary and the
        # captured name for statement-starter keywords catches this.
        pre = clean_cs[match.start():match.start(1)]
        if _CALL_SITE_PRECEDING_KEYWORDS.search(pre):
            continue
        csharp_methods.add(name)
    if not csharp_methods:
        return []

    # Collect Luau function names case-insensitively. The AI transpiler
    # routinely applies Luau conventions (camelCase) to methods that
    # were PascalCase in C# — ``Shoot`` becomes ``shoot``. That's a
    # naming transform, not a drop. Matching case-insensitively avoids
    # flooding the report with "missing" methods that are actually
    # present under their camelCase sibling.
    luau_functions: set[str] = set()
    for pat in _LUAU_FUNC_FORMS:
        for match in pat.finditer(luau_source):
            luau_functions.add(match.group(1).lower())

    # Methods that the AI explicitly marked as unconverted via comment.
    # Accept both `-- UNCONVERTED: foo` and `-- TODO: foo` idioms.
    # Match case-insensitively so the AI's camelCase comment refs line
    # up with PascalCase C# method names.
    commented_ci: set[str] = set()
    for line in luau_source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("--"):
            continue
        upper = stripped.upper()
        if "UNCONVERTED" not in upper and "TODO" not in upper:
            continue
        lower_line = stripped.lower()
        for method in csharp_methods:
            if method.lower() in lower_line:
                commented_ci.add(method.lower())

    missing = sorted(
        m for m in csharp_methods
        if m.lower() not in luau_functions and m.lower() not in commented_ci
    )
    return [
        f"[{source_name}] C# method '{m}' missing from Luau output "
        f"(neither a function definition nor an UNCONVERTED / TODO comment)"
        for m in missing
    ]
