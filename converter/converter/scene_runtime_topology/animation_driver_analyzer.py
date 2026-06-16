"""animation_driver_analyzer — extract Animator parameter writes from C#
source for Phase-2 driver narrowing.

NEW in Phase 1 (door run). The animation resolver
(``animation_routing.resolve_driver``) needs to find WHICH same-scope
MonoBehaviour drives a clip when no serialized Animator ref pins it (the
SimpleFPS Door case: ``private Animator doorAnim { get { return
transform.parent.Find("door").GetComponent<Animator>(); } }`` — a runtime
getter, no serialized field). The narrowing key is the clip's
``observed_attribute`` (the first Bool/Int/Trigger controller param):
the driver is the MB whose C# writes that param name via
``Set{Bool,Trigger,Float,Integer}("<param>", …)`` on an Animator-bound
receiver.

This module owns that extraction. It is a TWO-STEP, receiver-bound match
(D13), NOT a flat param-name regex:
  1. Bind the set of identifiers declared as ``Animator`` in the source
     (field / ``[SerializeField]`` field / property / local-var
     ``GetComponent<Animator>()`` assignment).
  2. Collect param names from ``<bound>.Set*("name", …)`` writes ONLY
     when ``<bound>`` is in the bound set. Writes on any other receiver
     are ignored.

Input contract: comment-stripped, **string-literal-preserving** C#
source (from ``module_domain._load_cs_source_preserving_strings``).
Input is deterministic Unity C# SOURCE, not LLM output, so the
regex-on-AI-output fragility rule does not apply (D13).
"""

from __future__ import annotations

import re

# Animator method names that take a parameter NAME as their first string
# arg. Validated against the captured method group in step 2.
_ANIMATOR_PARAM_SETTERS: frozenset[str] = frozenset(
    {"SetBool", "SetTrigger", "SetFloat", "SetInteger"}
)

# C# keywords that must never be bound as an Animator-typed identifier
# (codex PD-F1): ``where T : Animator`` would otherwise capture ``class``
# from ``public class Foo where T : Animator``. A keyword bind is harmless
# in practice (it needs a same-named ``.Set*("<observed>")`` write to
# mislead, and keywords never have one), but reject it anyway.
_CS_KEYWORDS: frozenset[str] = frozenset({
    "abstract", "as", "base", "bool", "break", "byte", "case", "catch",
    "char", "checked", "class", "const", "continue", "decimal", "default",
    "delegate", "do", "double", "else", "enum", "event", "explicit",
    "extern", "false", "finally", "fixed", "float", "for", "foreach",
    "goto", "if", "implicit", "in", "int", "interface", "internal", "is",
    "lock", "long", "namespace", "new", "null", "object", "operator",
    "out", "override", "params", "private", "protected", "public",
    "readonly", "ref", "return", "sbyte", "sealed", "short", "sizeof",
    "stackalloc", "static", "string", "struct", "switch", "this", "throw",
    "true", "try", "typeof", "uint", "ulong", "unchecked", "unsafe",
    "ushort", "using", "virtual", "void", "volatile", "while",
    # contextual keywords that can appear in declaration-adjacent spots
    "var", "where", "get", "set", "value", "async", "await", "yield",
})

# Step 1a — field / [SerializeField] field / property declaration.
# Anchored on the whole word ``Animator`` (so ``AnimatorController`` /
# ``MyAnimator`` are not matched) followed by the declared identifier.
# Captures ``doorAnim`` from both ``private Animator doorAnim {`` (property)
# and ``[SerializeField] Animator anim;`` (field).
_ANIMATOR_DECL_RE = re.compile(
    r"(?:\[SerializeField\][^;{}]*)?"
    r"\b(?:public|private|protected|internal|static|readonly|\s)*"
    r"\bAnimator\b\s+([A-Za-z_]\w*)\b"
)

# Step 1b — local-var via GetComponent<Animator>().
# Captures ``a`` from ``var a = GetComponent<Animator>();`` and
# ``Animator a = something.GetComponent<Animator>();``.
_ANIMATOR_LOCAL_RE = re.compile(
    r"\b(?:var|Animator)\s+([A-Za-z_]\w*)\s*=\s*[^;]*"
    r"GetComponent<\s*Animator\s*>\s*\(\)"
)


def _bind_animator_identifiers(cs_source: str) -> frozenset[str]:
    """Step 1 (D13): bind the set of identifiers declared as ``Animator``.

    Unions field / property / ``[SerializeField]`` field declarations with
    local-var ``GetComponent<Animator>()`` assignments. C# keywords are
    rejected (codex PD-F1: ``where T : Animator`` must not bind ``class``).
    """
    names: set[str] = set()
    for m in _ANIMATOR_DECL_RE.finditer(cs_source):
        name = m.group(1)
        if name not in _CS_KEYWORDS:
            names.add(name)
    for m in _ANIMATOR_LOCAL_RE.finditer(cs_source):
        name = m.group(1)
        if name not in _CS_KEYWORDS:
            names.add(name)
    return frozenset(names)


def extract_animator_param_writes(cs_source: str) -> frozenset[str]:
    """Given comment-stripped, **string-literal-preserving** C# source
    (from ``_load_cs_source_preserving_strings``), return the set of
    Animator parameter NAMES written via ``Set{Bool,Trigger,Float,
    Integer}("<param>", …)`` on an Animator-bound receiver.

    Two-step, receiver-bound (D13):
      1. Bind the set of identifiers declared as ``Animator`` in this
         source (field, ``[SerializeField]`` field, property, local-var
         ``GetComponent<Animator>()`` assignment).
      2. Return param names from ``<bound>.Set*("name", …)`` writes ONLY
         when ``<bound>`` is in the bound set. Writes on any other
         receiver are ignored.

    Empty input → empty set. Param-by-hash / param-by-variable
    (``SetBool(Animator.StringToHash("open"), …)`` /
    ``SetBool(OpenParam, …)``) are out of scope (FU1) and yield no entry
    (degrade to server fallback). Anonymous-receiver writes
    (``GetComponent<Animator>().SetBool("x")``, no bound identifier) are
    intentionally not matched (OO3).
    """
    if not cs_source:
        return frozenset()
    bound = _bind_animator_identifiers(cs_source)
    if not bound:
        return frozenset()
    setter_alt = "|".join(sorted(_ANIMATOR_PARAM_SETTERS))
    params: set[str] = set()
    for name in bound:
        write_re = re.compile(
            r"\b" + re.escape(name) + r"\s*\.\s*"
            r"(" + setter_alt + r")\s*\(\s*\"([^\"]*)\"",
            re.DOTALL,
        )
        for m in write_re.finditer(cs_source):
            method = m.group(1)
            param = m.group(2)
            if method in _ANIMATOR_PARAM_SETTERS and param:
                params.add(param)
    return frozenset(params)
