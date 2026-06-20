"""Resolve ``<recv>.GetChild(n)`` against the parsed Unity hierarchy.

Pre-transpile pass for generic mode: before a C# script reaches the AI, each
transform-ROOTED ``<recv>.GetChild(n)`` site is resolved to the n-th authored
child's NAME and rewritten ``<recv>.GetChild(n)`` -> ``<recv>.Find("<name>")``
(the receiver symbol is preserved). The AI then transpiles ``transform.Find("X")``
to the named ``:FindFirstChild("X")`` lookup it already knows, instead of a
fragile positional ``GetChildren()[n+1]`` ordinal.

The resolution follows the local CHAIN of Transform symbols rooted at the host
node: a per-script symbol table seeded ``transform -> host_node`` is grown to a
fixpoint over local-var assignments + block-bodied / expression-bodied property
getters whose definition is ``<resolved-recv>.GetChild(n)``. A receiver that
never roots at ``transform`` (e.g. ``cam = Camera.main.transform``) abstains: it
is counted in ``getchild_total`` but produces no fact, so the script is not
fully-resolved and the downstream backstop ABSTAINS on it (coverage gap, not a
failure).

Pure: reads sources + the parsed hierarchy, returns values. No game-specific
names. The C# is raw text (regex analyzer, no Roslyn AST), so matching is
code-position-aware (comments/strings are skipped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from converter.cs_text_scan import _canon_key, _cs_pos_is_code
from core.unity_types import (
    GuidIndex,
    ParsedScene,
    PrefabLibrary,
    PrefabNode,
    SceneNode,
)
from unity.script_analyzer import ScriptInfo
from unity.yaml_parser import ref_guid

# A parsed hierarchy node is either a scene node or a prefab node; both carry
# ``.name`` + a parse-order ``.children`` list, which is all the resolver needs.
HostNode = SceneNode | PrefabNode


# One resolved child-ref obligation for a single GetChild SITE.
@dataclass(frozen=True)
class ChildRefFact:
    site: str  # exact receiver-rooted text rewritten ("transform.GetChild(0)")
    receiver: str  # the receiver symbol ("transform" | a Transform local/getter)
    ordinal: int  # the n in <receiver>.GetChild(n)
    child_name: str  # resolved authored child name (resolved-node.children[n].name)


@dataclass(frozen=True)
class RigRootedRetargetFact:
    """One Camera.main-rooted child-ref RETARGET obligation, consumed POST-transpile.

    Distinct from ChildRefFact (host-rooted, receiver-PRESERVING, pre-AI): this
    fact's receiver (Camera.main.transform) points at the LIVE camera, but the
    resolved child lives under the converted _MainCameraRig Model at runtime, so
    the receiver is DISCARDED and the binding is retargeted to the rig. Consumed
    by the post-transpile rifle_rig_retarget_lowering, NOT by prerewrite_child_index.

    ``cam_receiver`` carries the EXACT camera receiver expression text the
    admission resolved (the C# group-2 receiver of ``<field> = <camrecv>.GetChild(n)``,
    e.g. ``cam`` for a seeded symbol or ``Camera.main.transform`` for the direct
    form). It is the deterministic upstream anchor for check D's dead-write
    exemption (stamped into the carrier as ``cam_receiver``/``cam_ordinal``), but is
    NOT a discharge condition: for an admitted fact it is NEVER "".
    """
    field_name: str  # the assignment LHS field ("weaponSlot"), from `<field> = cam.GetChild(n)`
    child_name: str  # resolved authored child name under the MainCamera node ("WeaponSlot"), E1-E3 guarded
    cam_receiver: str = ""  # the C# group-2 camera receiver text (the lowering's RECEIVER ANCHOR)
    ordinal: int = 0  # the n in the credited GetChild(n); -> carrier cam_ordinal
    # --- NEW equip obligation (Phase 1) — populated ONLY on the unambiguous
    #     held-prefab-equip shape (Instantiate(prefab)+SetParent(field) in ONE C#
    #     method). Default "" means "no equip obligation" (ABSTAIN — D8/D11): the
    #     equip lowering/verifier do not fire for that script and the existing
    #     rig-retarget path is untouched. The discriminator is
    #     ``equip_method != "" and prefab_field != ""``.
    equip_method: str = ""  # C# method hosting Instantiate(prefab)+SetParent(slot)
    prefab_field: str = ""  # the C# prefab field instantiated (e.g. "riflePrefab")
    # The UNIFORM display scale the C# equip method applies to the instantiated
    # prefab (``rifle.transform.localScale = Vector3.one * 0.2f`` -> 0.2). ``None``
    # means "no scale CAPTURED" — distinct from an explicit ``1.0`` — so the bridge
    # can collision-check a real explicit 1.0 against a conflicting capture instead
    # of silently dropping it: the script set no localScale on the equip object, or
    # set a NON-uniform / non-positive scale (deliberately not lowered, since the
    # runtime Model:ScaleTo is uniform-only). Carried to runtime so the welded weapon
    # is sized as the source game displayed it (NOT hardcoded).
    equip_scale: float | None = None


# Per-script resolution outcome, keyed on the canonical .cs path key. Carries
# the resolved facts (what the pre-rewrite rewrites) AND the per-script
# resolved/total tally (what the backstop asserts against). A script with >= 1
# GetChild site is present; a script with 0 GetChild sites is absent from the map.
@dataclass(frozen=True)
class ChildRefScript:
    facts: tuple[ChildRefFact, ...]  # one per RESOLVED site (rewrite targets)
    getchild_total: int  # all GetChild SITES seen in this script
    resolved_total: int  # len(facts) + len(rig_facts) == sites that produced a fact
    rig_facts: tuple[RigRootedRetargetFact, ...] = ()  # Camera.main-rooted retarget facts


ChildRefMap = dict[str, ChildRefScript]  # {canonical_cs_path_str: ChildRefScript}


# C# matchers --------------------------------------------------------------

# Site matcher — ANY receiver symbol .GetChild(n). The receiver is resolved
# against the per-script symbol table; only one that resolved to a
# transform-rooted node produces a fact.
_CS_GETCHILD_RE = re.compile(r"\b([A-Za-z_]\w*)\.GetChild\(\s*(\d+)\s*\)")

# Definition matchers — discover the symbol-table edges (<sym> <- <recv>.GetChild(n)).
# The receiver capture (group 2) is the LAST identifier before ``.GetChild``; an
# optional NON-captured dotted qualifier prefix (``gameObject.``, ``Camera.main.``)
# is consumed before it so ``<host-self>.transform.GetChild(n)`` definitions seed
# the chain. Group 2's start sits at that final identifier, so
# ``_receiver_is_member_access`` (called on m.start(2)) applies the SAME host-self
# allowlist as the site matcher: a host-self prefix resolves, a foreign one abstains.
_CS_RECV = r"(?:[A-Za-z_][\w.]*\.)?([A-Za-z_]\w*)"
_CS_GETCHILD_LOCAL_RE = re.compile(  # Transform x = recv.GetChild(0); | var x = ...
    r"\b(?:Transform|var)\s+([A-Za-z_]\w*)\s*=\s*"
    + _CS_RECV + r"\.GetChild\(\s*(\d+)\s*\)"
)
_CS_GETCHILD_GETTER_BLOCK_RE = re.compile(  # Transform x { get { return recv.GetChild(0); } }
    r"\bTransform\s+([A-Za-z_]\w*)\s*\{\s*get\s*\{\s*return\s+"
    + _CS_RECV + r"\.GetChild\(\s*(\d+)\s*\)\s*;\s*\}"
)
_CS_GETCHILD_GETTER_EXPR_RE = re.compile(  # Transform x => recv.GetChild(0);
    r"\bTransform\s+([A-Za-z_]\w*)\s*=>\s*"
    + _CS_RECV + r"\.GetChild\(\s*(\d+)\s*\)"
)

# Camera.main-rooted RETARGET assignment matcher (the rig path). Binds the WRITE
# ``<field> = <camrecv>.GetChild(n)`` where ``<camrecv>`` chains to
# ``Camera.main.transform``:
#   group 1 = assignment LHS field (the bare ``<field>``; an optional ``this.``
#             qualifier is consumed but NOT captured; a foreign member-access LHS
#             like ``x.weaponSlot`` is rejected — see _resolve_rig_facts).
#   group 2 = the receiver expression text (resolved to a canonical chain).
#   group 3 = the ordinal n.
# The LHS allows ONLY a bare field or a ``this.``-qualified field; a dotted
# member-access LHS (``x.weaponSlot``) is excluded by requiring the char before
# the field (after the optional ``this.``) to not be a foreign ``.`` qualifier —
# enforced structurally in _resolve_rig_facts (the regex captures the tail field,
# the resolver checks the head).
_CS_CAM_GETCHILD_RE = re.compile(
    r"([A-Za-z_][\w.]*)\s*=\s*"
    r"([A-Za-z_][\w.]*)\.GetChild\(\s*(\d+)\s*\)"
)


# --- Equip-obligation matchers (Phase 1) ----------------------------------
# A first ``Instantiate`` arg that is a literal / call / foreign member access
# (``Instantiate(new GameObject())``, ``Instantiate(a.b)``) is NOT a lone bare /
# ``this.``-field prefab -> the bind/chain matchers below do not capture it as a
# prefab field -> ABSTAIN (D11).
#
# A ``<receiver>.SetParent(<slot>)`` call. Group 1 is the FULL receiver chain text
# preceding ``.SetParent`` (e.g. ``r``, ``r.transform``, ``this.r.transform``);
# group 2 is the ``<slot>`` argument text. The resolver (a) compares the slot
# against the recognized rig ``field`` AND (b) binds the receiver back to the
# Instantiate result symbol — proving the PARENTED object IS the instantiated one
# The receiver class is captured greedily as a dotted/`this.` chain.
_CS_SETPARENT_RE = re.compile(
    r"\b((?:this\.)?[A-Za-z_][\w.]*?)\.SetParent\s*\(\s*([A-Za-z_][\w.]*)\s*\)"
)

# A directly-chained ``Instantiate(<prefab>)[.transform].SetParent(<slot>)`` —
# the result symbol is implicit (never bound to a var). Group 1 = optional
# ``this.`` on the prefab, group 2 = the bare prefab field, group 3 = the slot arg.
# The optional ``.transform`` between the call and ``.SetParent`` is allowed (and
# nothing else — a foreign member access breaks the chain -> no match).
_CS_INSTANTIATE_CHAIN_SETPARENT_RE = re.compile(
    r"\bInstantiate\s*\(\s*(this\.)?([A-Za-z_]\w*)\s*\)"
    r"(?:\.transform)?\.SetParent\s*\(\s*([A-Za-z_][\w.]*)\s*\)"
)

# The Instantiate-result binding ``[var] <sym> = Instantiate(<prefab>, …)`` —
# binds the spawned object to a local/field ``<sym>`` so a later
# ``<sym>[.transform].SetParent(<slot>)`` can be proven to parent THAT object.
# Group 1 = ``<sym>`` (the bound symbol), group 2 = optional ``this.`` on the
# prefab, group 3 = the bare prefab field. ``var``/a type prefix is optional.
_CS_INSTANTIATE_BIND_RE = re.compile(
    r"\b(?:var\s+|[A-Za-z_][\w.<>]*\s+)?"
    r"(this\.[A-Za-z_]\w*|[A-Za-z_]\w*)\s*=\s*"
    r"Instantiate\s*\(\s*(this\.)?([A-Za-z_]\w*)\s*(?:[,)])"
)

# A ``<recv>.transform.localScale = <rhs>;`` assignment on the instantiated
# prefab object inside the equip method (e.g. ``rifle.transform.localScale =
# Vector3.one * 0.2f;``). Group 1 = the receiver chain preceding ``.localScale``
# (``rifle``, ``rifle.transform``, ``this.rifle.transform``); group 2 = the RHS
# expression up to the statement terminator. The resolver matches the receiver
# BASE symbol against the proven Instantiate result symbol and parses the RHS as a
# UNIFORM scale (Unity Vector3) — a non-uniform / unparseable RHS yields no factor
# (scale stays the None "no-capture" sentinel), since the runtime ScaleTo is uniform-only.
_CS_LOCALSCALE_RE = re.compile(
    r"\b((?:this\.)?[A-Za-z_][\w.]*?)\.localScale\s*=\s*([^;]+);"
)
# A C# float literal: optional sign, integer/fraction/exponent forms (``0.2``,
# ``.2``, ``2``, ``2e-1``). The numeric VALUE is captured WITHOUT the trailing
# ``f``/``F`` suffix (matched separately, outside the group) so ``float()`` parses
# the captured text directly.
_CS_FLOAT = r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?"
# RHS uniform-scale forms: ``Vector3.one * f`` / ``f * Vector3.one`` /
# ``new Vector3(f, f, f)`` (uniform only). A trailing ``f``/``F`` float suffix and
# an optional sign are tolerated. Anchored to the whole RHS (after strip) so a
# partial/compound expression does not falsely match.
_CS_SCALE_ONE_MUL_RE = re.compile(
    r"\A(?:Vector3\.one\s*\*\s*(" + _CS_FLOAT + r")[fF]?"
    r"|(" + _CS_FLOAT + r")[fF]?\s*\*\s*Vector3\.one)\Z"
)
_CS_SCALE_NEW_VECTOR3_RE = re.compile(
    r"\Anew\s+Vector3\s*\(\s*(" + _CS_FLOAT + r")[fF]?\s*,\s*"
    r"(" + _CS_FLOAT + r")[fF]?\s*,\s*(" + _CS_FLOAT + r")[fF]?\s*\)\Z"
)


def _parse_uniform_scale_rhs(rhs: str) -> float | None:
    """Parse a C# ``localScale`` RHS as a POSITIVE UNIFORM scale factor, else None.

    Recognizes ``Vector3.one * f``, ``f * Vector3.one`` and a ``new Vector3(f,f,f)``
    whose three components are EQUAL. Returns None (ABSTAIN) for a non-uniform
    ``new Vector3(x,y,z)`` (the runtime ``Model:ScaleTo`` is uniform-only, so we do
    not distort), any other/identifier expression, AND a NON-POSITIVE factor
    (``<= 0`` — a pathological scale ScaleTo would reject; abstain rather than ship a
    Plan entry that is dead on arrival)."""
    expr = rhs.strip()
    m = _CS_SCALE_ONE_MUL_RE.match(expr)
    if m is not None:
        factor = float(m.group(1) if m.group(1) is not None else m.group(2))
        return factor if factor > 0 else None
    m = _CS_SCALE_NEW_VECTOR3_RE.match(expr)
    if m is not None:
        x, y, z = float(m.group(1)), float(m.group(2)), float(m.group(3))
        if x == y == z and x > 0:
            return x
        return None  # non-uniform / non-positive -> abstain
    return None

# A C# method declaration header ``<modifiers/ret> <Name>(…)`` immediately
# followed (modulo whitespace/comments) by an opening ``{``. Used to locate the
# nearest enclosing method body for a code position. The method NAME is group 1.
# The pattern is intentionally permissive on the return-type/modifier prefix (it
# only needs the ``<Name>(`` then a ``{`` body) — a control-flow head
# (``if (…) {``) is excluded because its keyword is not captured as a method name
# (we reject C# keywords in ``_enclosing_cs_method``).
_CS_METHOD_DECL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{")

# C# control-flow / statement keywords that the method-decl regex could match as a
# "method name" (``if (cond) {``). Rejected so a method scope never resolves to a
# control-flow block.
_CS_NOT_A_METHOD_NAME: frozenset[str] = frozenset({
    "if", "for", "foreach", "while", "switch", "do", "else", "lock", "using",
    "fixed", "catch", "try", "return", "in", "is", "as", "new",
})


# Host-self aliases: in Unity C#, ``transform``, ``this.transform``,
# ``gameObject.transform``, ``base.transform`` and ``this.gameObject.transform``
# all refer to the HOST's own transform. The qualifier chain immediately
# preceding ``.transform`` is host-rooted iff it is EXACTLY one of these
# single/double-token forms — anchored so the ENTIRE head (after stripping
# surrounding whitespace) is the alias, not a member of a longer foreign chain
# (``enemy.gameObject.transform`` -> head ``enemy.gameObject`` matches none).
# ``(?:^|...)`` anchors the start so ``mygameObject`` / ``a.gameObject`` are
# rejected; ``\Z`` anchors the end at the receiver. Order longest-first so the
# two-token ``this.gameObject`` form is recognized before a bare ``gameObject``.
#
# Split into two groups by SHADOWABILITY: ``this`` and ``base`` are C# keywords
# (cannot be shadowed by a local/param) so their forms are ALWAYS host-self;
# ``gameObject`` is an inherited MonoBehaviour MEMBER that a local/parameter can
# shadow, so the ``gameObject``-bearing forms (bare ``gameObject`` and
# ``this.gameObject``) only count as host-self when the script does NOT declare a
# shadowing local/param named ``gameObject``.
_HOST_SELF_KEYWORD_HEAD_RES = (
    re.compile(r"(?:^|[^\w.])\s*this\s*\Z"),
    re.compile(r"(?:^|[^\w.])\s*base\s*\Z"),
)
_HOST_SELF_GAMEOBJECT_HEAD_RES = (
    re.compile(r"(?:^|[^\w.])\s*this\s*\.\s*gameObject\s*\Z"),
    re.compile(r"(?:^|[^\w.])\s*gameObject\s*\Z"),
)

# Shadow-binding matchers for an inherited member that a local/parameter can
# shadow. ``{ident}`` is interpolated per identifier (``gameObject`` |
# ``transform``). Each matches a binding-INTRODUCTION context TEXTUALLY (code
# positions only, via ``_cs_pos_is_code``) — every C# form that introduces a
# local symbol named ``ident`` shadowing the inherited member:
#   - typed/`var` local, field, param-with-initializer, param before ``,``/``)``
#     (``GameObject gameObject =`` | ``var gameObject =`` | ``Transform transform;``)
#   - ``foreach`` binding (``foreach (var gameObject in xs)``)
#   - lambda parameter (single ``gameObject =>`` | parenthesized ``(gameObject) =>``
#     / ``(GameObject gameObject) =>`` / ``(a, gameObject) =>``)
#   - tuple deconstruction (``var (gameObject, i) =`` | ``(var gameObject, var i) =``)
#   - declaration pattern (``case GameObject gameObject:`` | ``is GameObject gameObject``)
# Conservative: a single matching binding ANYWHERE disables the alias for the
# whole script (bias to the safe ABSTAIN direction). The patterns are anchored so
# a ``return transform.GetChild(0)`` / ``return transform;`` (the ``return``
# keyword before ``transform`` is NOT a binding) does NOT false-match, and a bare
# ``transform.GetChild(0)`` site is untouched.
#
# ``_TYPE`` is a (possibly dotted/generic/array) C# type token used where a type
# precedes the bound identifier; ``_NESTED`` allows nested parens inside a tuple
# deconstruction target so ``var ((a, gameObject), c) =`` is seen.
# A (possibly dotted/generic/array) C# type token preceding a bound identifier.
_TYPE = r"[\w.<>\[\],\s]*?[\w>\]]"

# C# keywords that can syntactically sit in the ``<word> IDENT`` slot where a TYPE
# is expected but DON'T introduce a binding of ``IDENT``. A bare-word typed-decl
# pattern (``<word> transform [;)]``) false-matches these (``return transform;`` —
# a super-common getter idiom; ``in transform)`` — ``transform`` as a foreach
# COLLECTION, not a binding), over-abstaining the whole script. The type-token
# immediately preceding the shadowed identifier must NOT be one of these. ``var``
# is deliberately EXCLUDED — it is the contextual type in ``var gameObject =`` and
# DOES introduce a binding (kept via its own dedicated pattern), so it is not a
# keyword for this purpose.
_CS_KEYWORDS_NOT_TYPE: frozenset[str] = frozenset({
    "return", "in", "is", "as", "new", "out", "ref", "await", "yield", "throw",
    "case", "when", "where", "select", "from", "let", "by", "on", "equals",
    "into", "group", "orderby", "do", "else", "typeof", "sizeof", "nameof",
    "default", "checked", "unchecked", "stackalloc", "and", "or", "not",
})


def _ident_in_group(ident: str, body: str) -> bool:
    """True if ``ident`` appears as a whole word token in ``body``."""
    return re.search(r"(?:^|[^\w])" + re.escape(ident) + r"(?:[^\w]|$)", body) is not None


@dataclass(frozen=True)
class _ShadowPat:
    """One binding-context matcher.

    ``body``: the pattern captures a parenthesized list in group ``body_group``
    REGARDLESS of ``ident`` (a lambda param list / ``var (...)`` deconstruction
    target); it counts only when ``ident`` is a whole token inside that group.
    ``type_group``: when set, the group holds the TYPE token immediately preceding
    the bound identifier — the match is NOT a binding when that token is a C#
    keyword (``return transform;`` / ``in transform)`` are not declarations)."""

    pat: re.Pattern[str]
    body: bool = False
    body_group: int = 1
    type_group: int | None = None


def _shadow_decl_res(ident: str) -> tuple[_ShadowPat, ...]:
    """Per-identifier binding-context matchers."""
    i = re.escape(ident)
    return (
        # typed local / field / param-with-initializer / param followed by , or )
        # The leading word (group 1) is the TYPE token: it must NOT be a C# keyword
        # (so ``return transform;`` / ``in transform)`` are not read as bindings).
        _ShadowPat(re.compile(r"\b([A-Za-z_]\w*)\s+" + i + r"\s*(?:[=;,)])"), type_group=1),
        # ``var <ident> =`` local
        _ShadowPat(re.compile(r"\bvar\s+" + i + r"\s*=")),
        # foreach binding: ``foreach (var <ident> in`` | ``foreach (T <ident> in``
        # ``<ident>`` is the BINDING (loop variable) here, immediately before ``in``;
        # the foreach COLLECTION tail (``foreach (var c in <ident>)``) is NOT a
        # binding and is correctly NOT matched by this position-anchored pattern.
        _ShadowPat(re.compile(r"\bforeach\s*\(\s*(?:var|" + _TYPE + r")\s+" + i + r"\s+in\b")),
        # lambda parameter, single unparenthesized: ``<ident> =>``
        _ShadowPat(re.compile(r"\b" + i + r"\s*=>")),
        # lambda parameter inside a parenthesized list immediately before ``=>``
        # (``(gameObject) =>``, ``(GameObject gameObject) =>``, ``(a, gameObject) =>``).
        _ShadowPat(re.compile(r"\(([^()]*)\)\s*=>"), body=True),
        # tuple deconstruction with a ``var ( ... )`` target.
        _ShadowPat(re.compile(r"\bvar\s*\(([^()]*)\)"), body=True),
        # mixed deconstruction ``(var <ident>, ...)`` / ``(..., var <ident>)``
        _ShadowPat(re.compile(r"\bvar\s+" + i + r"\s*(?=[,)])")),
        # declaration pattern: ``case T <ident>:`` | ``is T <ident>``
        _ShadowPat(re.compile(r"\b(?:case|is)\s+" + _TYPE + r"\s+" + i + r"\b")),
    )


def _declares_shadow(source: str, ident: str) -> bool:
    """True if ``source`` TEXTUALLY introduces a local/parameter binding named
    ``ident`` (in ANY C# binding context: typed/var local, field, out/ref param,
    using var, foreach, lambda param, tuple deconstruction, declaration pattern)
    that would shadow an inherited MonoBehaviour member. Scans code positions only
    (skips strings/comments). Conservative — one binding anywhere is enough;
    biases to the safe ABSTAIN direction."""
    for sp in _shadow_decl_res(ident):
        for m in sp.pat.finditer(source):
            if not _cs_pos_is_code(source, m.start()):
                continue
            if sp.body and not _ident_in_group(ident, m.group(sp.body_group)):
                continue
            if sp.type_group is not None and m.group(sp.type_group) in _CS_KEYWORDS_NOT_TYPE:
                continue  # the "type" is a C# keyword -> not a binding declaration
            return True
    return False


def _receiver_is_member_access(
    source: str, recv_start: int, *, gameobject_shadowed: bool
) -> bool:
    """True if the receiver token starting at ``recv_start`` is a FOREIGN member
    access (``X.recv.GetChild(n)``) rather than the host's own transform.

    The receiver is HOST-ROOTED (returns False) iff it is a bare symbol (no
    leading ``.``) OR it is ``<host-self>.transform`` where the qualifier chain is
    a host-self alias (``this``, ``base``, ``gameObject``, ``this.gameObject``).
    It is FOREIGN (returns True) when the qualifier before the receiver is
    anything else (``Camera.main.transform``, ``foo.transform``,
    ``enemy.gameObject.transform``). When ``gameobject_shadowed`` is True, the
    ``gameObject``-bearing aliases (``gameObject.transform`` /
    ``this.gameObject.transform``) are treated as FOREIGN too — a local/param
    named ``gameObject`` shadows the inherited member, so the receiver is the
    shadow's transform, not the host's. Looks only at the text BEFORE the
    receiver, code-position aware via the caller's match gate."""
    # Walk back over the immediate ``.`` (with surrounding whitespace) preceding
    # the receiver; if there is none, the receiver is bare (not a member access).
    k = recv_start
    while k > 0 and source[k - 1] in " \t":
        k -= 1
    if k == 0 or source[k - 1] != ".":
        return False  # no leading dot -> bare receiver, host-rooted
    # There IS a leading ``.`` -> a member access. Host-rooted only if the WHOLE
    # qualifier head is a host-self alias (``X.gameObject.transform`` is foreign:
    # the alias is itself a member of ``X``, so the anchored head won't match).
    head = source[: k - 1]
    for qre in _HOST_SELF_KEYWORD_HEAD_RES:
        if qre.search(head) is not None:
            return False  # this/base keyword qualifier -> always host-rooted
    if not gameobject_shadowed:
        for qre in _HOST_SELF_GAMEOBJECT_HEAD_RES:
            if qre.search(head) is not None:
                return False  # un-shadowed gameObject alias -> host-rooted
    return True  # foreign qualifier (Camera.main, a field, a shadowed gameObject)


def _resolve_child(node: HostNode, ordinal: int) -> HostNode | None:
    """Resolve ``node.GetChild(ordinal)`` to the child node, applying the §7.2
    edge guards: past-end (E3), unnamed (E2), and sibling-name collision (E1/E2)
    all ABSTAIN (return ``None``) so the ordinal stays in C# and the backstop
    catches any survivor — never silently rebind to the wrong child."""
    children = list(getattr(node, "children", []) or [])
    if ordinal < 0 or ordinal >= len(children):
        return None  # E3 — past end
    child = children[ordinal]
    name = getattr(child, "name", "") or ""
    if not name:
        return None  # E2 — unnamed
    # E1/E2 — name must be UNIQUE among siblings (a Find("<name>") would be
    # ambiguous otherwise).
    same_name = sum(1 for c in children if (getattr(c, "name", "") or "") == name)
    if same_name != 1:
        return None
    return child


def _build_symbol_table(
    source: str, host_node: HostNode, *, gameobject_shadowed: bool
) -> dict[str, HostNode]:
    """Build the per-script Transform-symbol table by local dataflow.

    Seed ``transform -> host_node`` (UNLESS a local/param shadows the inherited
    ``transform`` member, in which case the seed is withheld and every
    bare-``transform`` site abstains), then resolve to a fixpoint over the three
    C# definition shapes (local-var assignment, block-bodied getter,
    expression-bodied getter): each ``<sym> = <recv>.GetChild(n)`` is a pending
    edge resolved once ``<recv>`` is in the table. A guard failure (E1–E3) drops
    the edge — its ``<sym>`` stays unresolved, so any later site on it abstains.
    """
    transform_shadowed = _declares_shadow(source, "transform")
    # Collect pending edges (sym, recv, ordinal) from all three shapes, at code
    # positions only.
    pending: list[tuple[str, str, int]] = []
    for pat in (
        _CS_GETCHILD_LOCAL_RE,
        _CS_GETCHILD_GETTER_BLOCK_RE,
        _CS_GETCHILD_GETTER_EXPR_RE,
    ):
        for m in pat.finditer(source):
            if not _cs_pos_is_code(source, m.start()):
                continue
            # The receiver (group 2) must be a bare host symbol, not a member
            # access on a foreign expression (``a.tBase.GetChild(0)``).
            if _receiver_is_member_access(
                source, m.start(2), gameobject_shadowed=gameobject_shadowed
            ):
                continue
            pending.append((m.group(1), m.group(2), int(m.group(3))))

    # A local/param named ``transform`` shadows the inherited Component property,
    # so bare ``transform`` is NOT the host's transform -> withhold the seed and
    # let every bare-``transform`` site abstain (bias to safe abstain).
    table: dict[str, HostNode] = {} if transform_shadowed else {"transform": host_node}
    # Iterate to a fixpoint: each pass resolves any edge whose receiver is now
    # known. Stop when a full pass binds nothing new.
    remaining = list(pending)
    while True:
        progressed = False
        still: list[tuple[str, str, int]] = []
        for sym, recv, ordinal in remaining:
            if sym in table:
                # Already bound (duplicate definition) — drop, don't rebind.
                progressed = True
                continue
            recv_node = table.get(recv)
            if recv_node is None:
                still.append((sym, recv, ordinal))
                continue
            child = _resolve_child(recv_node, ordinal)
            if child is not None:
                table[sym] = child
            # Guard failure: the edge is consumed (dropped), sym stays unresolved.
            progressed = True
        remaining = still
        if not progressed or not remaining:
            break
    return table


def _walk_prefab(pnode: PrefabNode | None, out: list[PrefabNode]) -> None:
    if pnode is None:
        return
    out.append(pnode)
    for child in getattr(pnode, "children", []) or ():
        _walk_prefab(child, out)


def _node_by_cs_path(
    parsed_scenes: list[ParsedScene] | None,
    prefab_library: PrefabLibrary | None,
    guid_index: GuidIndex | None,
) -> dict[str, list[HostNode]]:
    """Map each ``.cs`` path (canonical ``str(path.resolve())`` key) to the
    hierarchy nodes that host it as a MonoBehaviour. Walks the FULL-fidelity
    collections exactly as ``serialized_field_extractor`` does: every scene's
    ``all_nodes`` + every prefab template's node tree (``prefabs``, the lossless
    list, NOT ``by_name`` which drops name-collisions)."""
    out: dict[str, list[HostNode]] = {}
    if guid_index is None:
        return out

    def _record(node: HostNode) -> None:
        for comp in getattr(node, "components", []) or ():
            if getattr(comp, "component_type", "") != "MonoBehaviour":
                continue
            script_ref = getattr(comp, "properties", {}).get("m_Script")
            guid = ref_guid(script_ref) or ""
            if not guid:
                continue
            cs_path = guid_index.resolve(guid)
            if cs_path is None or cs_path.suffix != ".cs":
                continue
            out.setdefault(_canon_key(cs_path), []).append(node)

    for scene in parsed_scenes or ():
        if scene is None:  # the [None] all-parse-failed fallback is inert
            continue
        for node in getattr(scene, "all_nodes", {}).values():
            _record(node)

    if prefab_library is not None:
        for template in getattr(prefab_library, "prefabs", []) or ():
            nodes: list[PrefabNode] = []
            _walk_prefab(getattr(template, "root", None), nodes)
            for node in nodes:
                _record(node)

    return out


_CAMERA_MAIN_TRANSFORM = "Camera.main.transform"

# C# control-flow keywords that, when they directly govern a braceless single
# statement, make that statement CONDITIONAL (it does not unconditionally execute
# on the straight-line path). A seed governed by one of these does NOT dominate a
# later use outside its branch.
_CS_CONDITIONAL_KEYWORDS: frozenset[str] = frozenset(
    {"if", "else", "while", "for", "foreach", "case", "do"}
)


def _seed_dominates_use(source: str, seed_start: int, use_pos: int) -> bool:
    """True iff the seed assignment starting at ``seed_start`` dominates the
    GetChild use at ``use_pos`` on the STRAIGHT-LINE path — i.e. the seed is in the
    same block or an unconditional enclosing scope, NOT buried in a
    conditional/dead branch that closes before the use.

    Conservative — ABSTAINS (returns False) whenever it cannot cheaply prove
    dominance, so a false-admitted fact is impossible (a missed fact is safe):
      (1) BRACE SCOPE: scanning code positions from ``seed_start`` to ``use_pos``,
          the running ``{``/``}`` depth (relative to the seed) must never go
          NEGATIVE — a ``}`` that closes a block open at the seed means the seed's
          block ended before the use (``if (c) { cam = ...; } use``;
          ``{ cam = ...; } use``).
      (2) BRACELESS GOVERNOR: the seed statement must not be the single braceless
          body of a control-flow keyword (``if (c) cam = ...; use``) — detected by
          looking at the token immediately preceding the seed's statement (after
          the governing ``)`` of an ``if``/``while``/``for`` header, or a bare
          ``else``/``do``)."""
    # (1) brace-depth scope check.
    depth = 0
    i = seed_start
    while i < use_pos:
        if not _cs_pos_is_code(source, i):
            i += 1
            continue
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False  # the seed's enclosing block closed before the use
        i += 1
    # (2) braceless single-statement governor check: walk back over whitespace AND
    # C# comments from the seed to the preceding statement boundary. If the seed
    # statement is directly governed by a braceless conditional, it does not
    # dominate.
    #
    # TRIVIA-ROBUST: the back-walk MUST skip ``//``/``/* */``
    # comments, not just whitespace. A comment between the governing header and the
    # seed (``if (c) /*x*/ cam = ...`` / ``if (c) // n\n cam = ...``) otherwise lands
    # ``prev`` on the comment delimiter, defeating the conditional detection and
    # FALSE-ADMITTING a conditional seed as dominating.
    k0 = _skip_ws_and_comments_back(source, seed_start)
    if k0 <= 0:
        return True  # start of file -> top-level straight-line
    prev = source[k0 - 1]
    if prev in ";{}":
        return True  # a clear statement/block boundary -> straight-line in scope
    if prev == ")":
        # The seed follows a ``)`` — it may be the braceless body of an
        # ``if (...)``/``while (...)``/``for (...)`` header. Find the matching
        # ``(`` and inspect the keyword before it (trivia-aware).
        bdepth = 0
        k = k0 - 1
        while k >= 0:
            if _cs_pos_is_code(source, k):
                c = source[k]
                if c == ")":
                    bdepth += 1
                elif c == "(":
                    bdepth -= 1
                    if bdepth == 0:
                        break
            k -= 1
        if k < 0:
            return False  # unbalanced -> cannot prove -> abstain
        if _preceding_governor_keyword(source, k) in _CS_CONDITIONAL_KEYWORDS:
            return False  # braceless conditional body -> not dominating
        return True
    if prev.isalpha() or prev == "_":
        # The seed follows a bare keyword (``else cam = ...;`` / ``do cam = ...``).
        if _preceding_governor_keyword(source, k0) in _CS_CONDITIONAL_KEYWORDS:
            return False
        return True
    return True


def _preceding_governor_keyword(source: str, pos: int) -> str | None:
    """The C# identifier/keyword token whose LAST char ends just before ``pos``
    (skipping intervening whitespace + ``//``/``/* */`` comments). Used to read the
    control-flow keyword that governs a braceless body — trivia between the keyword
    and its ``(`` / body (``if /*x*/ (c) ...``) must not hide it. None if no
    identifier token precedes ``pos``."""
    k = _skip_ws_and_comments_back(source, pos)
    end = k
    while k >= 1 and (source[k - 1].isalnum() or source[k - 1] == "_"):
        k -= 1
    token = source[k:end]
    return token or None


def _owning_node_collection(
    host_node: HostNode,
    parsed_scenes: list[ParsedScene] | None,
    prefab_library: PrefabLibrary | None,
) -> list[HostNode] | None:
    """The full node list of the scene OR prefab template the ``host_node`` lives
    in (matched by object identity). None if the host cannot be located in any
    parsed collection (the caller then abstains — it cannot scope uniqueness)."""
    for scene in parsed_scenes or ():
        if scene is None:
            continue
        nodes = list(getattr(scene, "all_nodes", {}).values())
        if any(node is host_node for node in nodes):
            return nodes
    if prefab_library is not None:
        for template in getattr(prefab_library, "prefabs", []) or ():
            nodes_p: list[PrefabNode] = []
            _walk_prefab(getattr(template, "root", None), nodes_p)
            if any(node is host_node for node in nodes_p):
                return list(nodes_p)
    return None


def _main_camera_node(
    host_node: HostNode,
    parsed_scenes: list[ParsedScene] | None,
    prefab_library: PrefabLibrary | None,
) -> HostNode | None:
    """The UNIQUE parsed node tagged ``MainCamera`` in the SCENE/PREFAB the host
    lives in (scoped by ``host_node``'s owning collection — codex MAJOR). Returns
    None if absent or non-unique (>1) WITHIN that scope — abstain
    (E-no-tag / E-non-unique).

    Filters on ``tag == "MainCamera"`` — the same upstream signal
    ``scene_converter`` uses to stamp ``_MainCameraRig`` (mode-independent). The
    uniqueness check is SCOPED to the host's owning scene/prefab, so an unrelated
    MainCamera-tagged node in a DIFFERENT scene/prefab no longer suppresses the
    fact (silent drop). If the host cannot be located in any parsed collection,
    abstain (no scope to assert uniqueness within)."""
    collection = _owning_node_collection(host_node, parsed_scenes, prefab_library)
    if collection is None:
        return None  # host not locatable -> cannot scope uniqueness -> abstain
    found = [n for n in collection if getattr(n, "tag", "") == "MainCamera"]
    if len(found) != 1:
        return None  # zero -> E-no-tag; >1 -> E-non-unique; both abstain
    return found[0]


_THIS_TOKEN_RE = re.compile(r"this\Z")


def _seed_lhs_is_bare_or_this(source: str, sym_start: int) -> bool:
    """True iff the symbol matched at ``sym_start`` is the LHS of a BARE write
    (``cam = ...``) or a ``this.``-qualified write (``this.cam = ...``), and NOT a
    FOREIGN member access (``other.cam = ...`` / ``a.b.cam = ...``).

    Mirrors the ``_lhs_is_bare_field`` "bare or ``this.`` only" discipline applied
    to the GetChild LHS: a seed assignment to a member field of a foreign object is
    NOT a binding of the bare symbol used at the GetChild, so it must not be admitted
    as a camera seed.

    TRIVIA-ROBUST: the "preceded by ``.``" member-access check skips ALL C# trivia —
    spaces, tabs, NEWLINES, and
    ``//``/``/* */`` comments — between the symbol and the preceding token, via
    ``_skip_ws_and_comments_back``. Without it a foreign member-LHS seed split by a
    comment or newline (``other.\ncam = ...`` / ``other./*c*/cam = ...``) false-admits
    a non-camera binding as a camera seed (a bogus fact the verifier does NOT catch —
    it ships a wrong retarget). Legit ``cam =`` / ``this.cam =`` / ``this . cam =`` /
    ``this./*c*/cam =`` still ADMIT."""
    # The nearest preceding CODE char before the symbol, skipping ALL trivia
    # (whitespace incl. newlines + ``//``/``/* */`` comments). A member access
    # ``obj./*c*/cam`` / ``obj.\ncam`` keeps the ``.`` as that preceding code token.
    k = _skip_ws_and_comments_back(source, sym_start)
    if k <= 0 or source[k - 1] != ".":
        return True  # no leading ``.`` -> bare symbol write
    # A dotted LHS: admit ONLY ``this.<sym>``. Walk back over the ``.`` (and any
    # trivia before it) to the preceding identifier and require it EXACTLY ``this``.
    k = _skip_ws_and_comments_back(source, k - 1)  # past the ``.`` to its code char
    ident_end = k
    while k >= 1 and (source[k - 1].isalnum() or source[k - 1] == "_"):
        k -= 1
    qualifier = source[k:ident_end]
    if _THIS_TOKEN_RE.match(qualifier) is None:
        return False  # ``other.cam`` / ``a.b.cam`` -> foreign member access
    # ``this`` itself must not be a member tail (``foo.this.cam`` -> foreign): skip
    # trivia before ``this`` and reject if a ``.`` precedes it.
    p = _skip_ws_and_comments_back(source, k)
    if p > 0 and source[p - 1] == ".":
        return False
    return True


def _canonical_receiver(source: str, recv: str, use_pos: int) -> str | None:
    """Resolve the receiver-expression text ``recv`` (group 2 of
    ``_CS_CAM_GETCHILD_RE``) to its canonical receiver chain for the rig path,
    AS SEEN AT the GetChild use site ``use_pos``.

    Returns the literal ``Camera.main.transform`` iff ``recv`` roots there —
    either:
      - the DIRECT form: ``recv`` is EXACTLY ``Camera.main.transform`` (so
        ``weaponSlot = Camera.main.transform.GetChild(0)`` is admitted even with
        no cam-symbol seed), OR
      - a per-script SYMBOL whose NEAREST PRECEDING binding before ``use_pos`` is
        EXACTLY ``<sym> = Camera.main.transform`` (one hop, anchored to the use
        site by scope/order — NOT any file-wide occurrence).
    Returns None for any foreign chain (``enemy.cam``, ``other.cam.transform``, a
    bare symbol with no such seed). EXACT match, NOT ``endswith``/substring.

    Anchored to ``use_pos``: a later/unrelated
    ``cam = Camera.main.transform`` after the GetChild does NOT admit when the
    binding live AT the GetChild is foreign (``cam = enemy.transform``). The seed
    is the LAST ``<sym> = <rhs>`` strictly before ``use_pos``; it admits iff that
    nearest preceding binding is exactly ``Camera.main.transform``."""
    if recv == _CAMERA_MAIN_TRANSFORM:
        return _CAMERA_MAIN_TRANSFORM
    # ``recv`` must be a bare symbol (no dot) to be seed-resolvable; a dotted
    # foreign chain (``enemy.cam``) is rejected outright.
    if "." in recv:
        return None
    # Find the NEAREST PRECEDING binding of ``recv`` before the use site — any
    # ``<recv> = <rhs>`` (not ``==``) at a code position with start < use_pos.
    # Whichever is last wins (it is the binding live at the GetChild line).
    #
    # SEED-LHS DISCIPLINE (mirror ``_lhs_is_bare_field``):
    # the binding's LHS symbol must be a BARE symbol or a ``this.<recv>`` write —
    # NOT a FOREIGN member access ``<other>.<recv>``. The ``\b`` after the ``.`` of
    # ``other.cam`` lets ``any_assign_re`` (``\bcam\s*=``) match the ``cam`` token
    # INSIDE ``other.cam``, so without this guard ``other.cam = Camera.main.transform``
    # is mis-read as a bare-``cam`` seed and false-admits a foreign field as the
    # camera receiver. A binding of a DIFFERENT lvalue (``other.cam``) is NOT a
    # binding of the bare ``cam`` symbol used at the GetChild — skip it.
    any_assign_re = re.compile(r"\b" + re.escape(recv) + r"\s*=(?!=)")
    nearest_start = -1
    for m in any_assign_re.finditer(source):
        if m.start() >= use_pos:
            break  # past the use site -> later bindings cannot be live here
        if not _cs_pos_is_code(source, m.start()):
            continue
        if not _seed_lhs_is_bare_or_this(source, m.start()):
            continue  # foreign member-access LHS (``other.cam =``) -> not this symbol
        nearest_start = m.start()
    if nearest_start == -1:
        return None  # no binding before the use site -> not seed-resolvable
    # Is that nearest preceding binding EXACTLY ``<recv> = Camera.main.transform``?
    # TRIVIA-ROBUST: the seed's
    # ``<recv>``, ``=``, and the ``Camera.main.transform`` literal may be separated by
    # ``//``/``/* */`` comments (``cam = /*c*/ Camera.main.transform``); skip trivia
    # forward between tokens so a comment-split LEGIT seed is not false-REJECTED (a
    # dropped rig fact -> the rifle silently fails to retarget). A non-camera RHS
    # still returns None.
    end = _match_exact_cam_seed_rhs(source, nearest_start, recv, use_pos)
    if end is None:
        return None  # the live binding is something else (e.g. enemy.transform)
    # The RHS must be EXACTLY Camera.main.transform (not a longer chain like
    # Camera.main.transform.parent). ``_match_exact_cam_seed_rhs`` already requires a
    # word boundary after the literal; reject a trailing ``.`` member (trivia-aware).
    tail = _skip_ws_and_comments_fwd(source, end)
    if tail < len(source) and source[tail] == ".":
        return None  # longer chain -> not the exact one-hop seed
    # SCOPE-AWARE: order-nearest is not enough — the seed
    # must DOMINATE the use site on the straight-line path. A seed buried in a
    # dead/conditional block (``if (false) { cam = Camera.main.transform; }``) does
    # NOT dominate ``weaponSlot = cam.GetChild(0)`` below it; abstain rather than
    # admit a fact whose real receiver isn't Camera.main.
    if not _seed_dominates_use(source, nearest_start, use_pos):
        return None
    return _CAMERA_MAIN_TRANSFORM


def _skip_ws_and_comments_back(source: str, pos: int) -> int:
    """Walk ``pos`` backward over whitespace and C# comments, returning the index
    just AFTER the nearest preceding CODE char (so ``source[k - 1]`` is that char,
    or ``k == 0`` at start of file). A comment char is identified authoritatively
    by ``_cs_pos_is_code`` (a from-start scan that correctly distinguishes a real
    ``//``/``/* */`` comment from ``//`` text inside a string literal)."""
    k = pos
    while k > 0:
        j = k - 1
        prev = source[j]
        if prev in " \t\r\n":
            k -= 1
            continue
        # An INNER comment char is non-code per ``_cs_pos_is_code`` -> skip it.
        if not _cs_pos_is_code(source, j):
            k -= 1
            continue
        # ``_cs_pos_is_code`` (a from-start scan) classifies the OPENING char of a
        # ``//``/``/*`` comment as code (it only enters the comment branch when
        # ``i < pos``), so an opener slips through above. Recognize it explicitly:
        # a ``/`` that starts a real comment (its inner char is non-code) is the
        # comment delimiter, not a code token -> keep walking back.
        if (
            prev == "/"
            and j + 1 < len(source)
            and source[j + 1] in "/*"
            and not _cs_pos_is_code(source, j + 1)
        ):
            k -= 1
            continue
        break
    return k


def _skip_ws_and_comments_fwd(source: str, pos: int) -> int:
    """Walk ``pos`` FORWARD over whitespace and C# ``//``/``/* */`` comments,
    returning the index of the next CODE char (or ``len(source)`` at end). A comment
    char is identified authoritatively by ``_cs_pos_is_code`` (a from-start scan that
    distinguishes a real comment from ``//`` inside a string literal)."""
    n = len(source)
    k = pos
    while k < n:
        ch = source[k]
        if ch in " \t\r\n":
            k += 1
            continue
        # A comment OPENER (``//`` / ``/*``) is classified as code by
        # ``_cs_pos_is_code`` at the opener itself; recognize it by the inner char
        # being non-code, then skip the whole comment body (non-code positions).
        if ch == "/" and k + 1 < n and source[k + 1] in "/*" and not _cs_pos_is_code(
            source, k + 1
        ):
            k += 1
            continue
        # An inner comment char (already inside a comment) is non-code -> skip.
        if not _cs_pos_is_code(source, k):
            k += 1
            continue
        break
    return k


def _match_exact_cam_seed_rhs(
    source: str, nearest_start: int, recv: str, use_pos: int
) -> int | None:
    """Match ``<recv>`` [trivia] ``=`` (not ``==``) [trivia]
    ``Camera.main.transform`` starting at ``nearest_start``, skipping ``//``/``/* */``
    comments and whitespace between every token. Returns the index just AFTER the
    literal on a match (so the caller can inspect a trailing ``.`` member), else
    None. The literal must end on a word boundary (no ``...transformX``)."""
    n = len(source)
    # ``<recv>`` token.
    i = nearest_start
    if source[i : i + len(recv)] != recv:
        return None
    i += len(recv)
    if i < n and (source[i].isalnum() or source[i] == "_"):
        return None  # ``recv`` is a prefix of a longer identifier
    # ``=`` (reject ``==``).
    i = _skip_ws_and_comments_fwd(source, i)
    if i >= n or source[i] != "=" or (i + 1 < n and source[i + 1] == "="):
        return None
    i += 1
    # ``Camera.main.transform`` literal.
    i = _skip_ws_and_comments_fwd(source, i)
    lit = _CAMERA_MAIN_TRANSFORM
    if source[i : i + len(lit)] != lit:
        return None
    end = i + len(lit)
    if end < n and (source[end].isalnum() or source[end] == "_"):
        return None  # ``...transformX`` -> not the exact literal
    if end > use_pos:
        return None  # the seed must complete before the use site
    return end


def _lhs_is_bare_field(source: str, field_start: int, full_lhs: str) -> str | None:
    """Given the matched LHS text ``full_lhs`` (group 1 of _CS_CAM_GETCHILD_RE)
    starting at ``field_start``, return the bare field name iff the LHS is a bare
    field WRITE or a ``this.``-qualified field write; else None.

    A bare field write (``weaponSlot = ...`` / ``this.weaponSlot = ...``) is the
    ONLY admitted shape; a foreign member-access LHS (``x.weaponSlot``) or a typed
    local declaration (``Transform weaponSlot = ...`` etc.) abstains.

    ALLOW-LIST BY TERMINATOR: a bare field write's preceding non-blank char in C# is
    always a STATEMENT TERMINATOR (``;``/``{``/``}``) or start-of-file; ANY other
    preceding char means a leading token (a type, control-flow head) precedes the
    field. This is the FAIL-CLOSED choice — admitting a typed local would flip
    ``resolved_total`` and false-fail-close valid code, while over-abstaining a rare
    conditional camera write is safe — and an allow-list avoids the unbounded
    type-token reject-list (``Transform?``, ``(T,int)`` escape it)."""
    parts = full_lhs.split(".")
    if len(parts) == 1:
        field = parts[0]
    elif len(parts) == 2 and parts[0] == "this":
        field = parts[1]
    else:
        return None  # x.weaponSlot / a.b.weaponSlot -> foreign member-access LHS
    # Walk back over whitespace AND C# comments to the preceding code char. A
    # comment between the prior statement and the field write (a ``// note`` line
    # or an inline ``foo(); /* note */``) must not be mistaken for a leading token;
    # otherwise a legitimate comment-preceded bare field write is wrongly rejected.
    k = _skip_ws_and_comments_back(source, field_start)
    if k <= 0:
        return field  # start of file -> bare write
    if source[k - 1] in ";{}":
        return field  # statement terminator precedes -> bare field write
    return None  # any leading token (a type, a control-flow head) -> abstain


def _enclosing_cs_method(source: str, pos: int) -> tuple[str, int, int] | None:
    """The nearest enclosing C# method ``<name>(…) { … }`` that CONTAINS ``pos``.

    Returns ``(method_name, body_open_idx, body_close_idx)`` where ``body_open_idx``
    is the index of the method's opening ``{`` and ``body_close_idx`` is the index
    just past its matching ``}`` (brace-balanced over code positions). Returns None
    if ``pos`` is not inside any method body.

    Code-position-aware (comments/strings skipped). A control-flow head
    (``if (cond) {``) is excluded — its keyword is rejected as a method name.
    Picks the INNERMOST method whose ``{ … }`` body brackets ``pos`` (so a nested
    local-function body wins over the outer method), which is what binds the equip
    obligation to one rewrite site."""
    best: tuple[str, int, int] | None = None
    for m in _CS_METHOD_DECL_RE.finditer(source):
        if not _cs_pos_is_code(source, m.start()):
            continue
        name = m.group(1)
        if name in _CS_NOT_A_METHOD_NAME:
            continue
        brace_open = source.index("{", m.end() - 1)
        body_end = _cs_block_end(source, brace_open)
        if body_end is None:
            continue
        if brace_open < pos < body_end:
            # Innermost wins: a later (textually-deeper) match that still brackets
            # ``pos`` is nested inside, so prefer the one with the LATEST open brace.
            if best is None or brace_open > best[1]:
                best = (name, brace_open, body_end)
    return best


def _cs_block_end(source: str, open_brace_idx: int) -> int | None:
    """The index just past the ``}`` matching the ``{`` at ``open_brace_idx``,
    brace-balanced over code positions only (strings/comments skipped). None if
    unbalanced (runs off the end)."""
    depth = 0
    i = open_brace_idx
    n = len(source)
    while i < n:
        if not _cs_pos_is_code(source, i):
            i += 1
            continue
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _setparent_receiver_base(receiver: str) -> str:
    """The base symbol of a SetParent receiver chain — strip a trailing
    ``.transform`` and a leading ``this.``, leaving the spawned-object symbol so it
    can be matched against the Instantiate-result binding. ``r`` / ``r.transform``
    / ``this.r.transform`` all reduce to ``r``."""
    base = receiver
    if base.endswith(".transform"):
        base = base[: -len(".transform")]
    if base.startswith("this."):
        base = base[len("this.") :]
    return base


def _resolve_equip_obligation(
    source: str, field: str
) -> tuple[str, str, float | None] | None:
    """For an admitted rig fact on C# field ``field``, return
    ``(equip_method, prefab_field, equip_scale)`` iff the script contains the
    UNAMBIGUOUS held-prefab-equip shape — else None (ABSTAIN, D8/D11).
    ``equip_scale`` is the UNIFORM ``localScale`` the method applies to the
    instantiated prefab (``Vector3.one * 0.2f`` -> 0.2), or ``None`` when no uniform
    positive scale was captured (no localScale / non-uniform / non-positive).

    Unambiguous shape (ALL required, code-position-aware, in the SAME C# method):
      1. an ``Instantiate(<prefabField>, …)`` whose first arg is a bare /
         ``this.``-field identifier (the prefab), bound to a result symbol
         ``<sym>`` (``[var] <sym> = Instantiate(<prefab>)``) OR directly chained
         (``Instantiate(<prefab>)[.transform].SetParent(...)``), AND
      2. a ``<receiver>.SetParent(<slot>)`` whose ``<slot>`` is EXACTLY the
         recognized rig ``field`` (bare or ``this.``-qualified) AND whose
         ``<receiver>`` base symbol IS that same ``<sym>`` — i.e. the PARENTED
         object is provably the INSTANTIATED one, AND
      3. both in the SAME enclosing method ``M``.

    ABSTAIN (None) on: no qualifying Instantiate; a SetParent whose slot is not
    ``field``; a SetParent whose receiver is NOT the Instantiate result (parents a
    DIFFERENT object); the two in DIFFERENT methods; ``>1`` distinct
    qualifying method (ambiguous — §Edge f); an unresolvable prefab arg (D11).
    Pure; code-position-aware; keyed entirely on the C# ``field`` already proven to
    be the MainCamera-child slot."""
    # Find every SetParent(slot) whose slot is EXACTLY ``field`` (the rig slot),
    # capturing the receiver base symbol + enclosing method.
    # method -> list of (receiver_base_symbol, body_open, body_close)
    setparent_hits: dict[str, list[tuple[str, int, int]]] = {}
    for m in _CS_SETPARENT_RE.finditer(source):
        if not _cs_pos_is_code(source, m.start()):
            continue
        slot = m.group(2)
        if slot != field and slot != f"this.{field}":
            continue  # SetParent onto a different slot -> not this obligation
        enc = _enclosing_cs_method(source, m.start())
        if enc is None:
            continue
        recv_base = _setparent_receiver_base(m.group(1))
        setparent_hits.setdefault(enc[0], []).append((recv_base, enc[1], enc[2]))
    if not setparent_hits:
        return None  # no SetParent onto the rig slot -> abstain

    candidates: list[tuple[str, str, float | None]] = []
    for method, hits in setparent_hits.items():
        body_open, body_close = hits[0][1], hits[0][2]
        # (A) Directly-chained Instantiate(prefab)[.transform].SetParent(field) —
        # the parented object IS the instantiate result by construction.
        chain_prefabs: set[str] = set()
        for cm in _CS_INSTANTIATE_CHAIN_SETPARENT_RE.finditer(
            source, body_open, body_close
        ):
            if not _cs_pos_is_code(source, cm.start()):
                continue
            cslot = cm.group(3)
            if cslot != field and cslot != f"this.{field}":
                continue
            chain_prefabs.add(cm.group(2))
        # (B) Symbol-bound: <sym> = Instantiate(prefab) where <sym> is the base
        # symbol parented by one of this method's SetParent(field) receivers.
        bind_prefabs: set[str] = set()
        receiver_bases = {h[0] for h in hits}
        for bm in _CS_INSTANTIATE_BIND_RE.finditer(source, body_open, body_close):
            if not _cs_pos_is_code(source, bm.start()):
                continue
            sym_base = _setparent_receiver_base(bm.group(1))
            if sym_base not in receiver_bases:
                continue  # this Instantiate binds a DIFFERENT symbol -> not it
            bind_prefabs.add(bm.group(3))
        method_prefabs = chain_prefabs | bind_prefabs
        if len(method_prefabs) != 1:
            # zero -> the parented object isn't a proven Instantiate result (the
            # over-broad false-positive this strictness avoids); >1 -> ambiguous. Abstain.
            continue
        prefab = next(iter(method_prefabs))
        # Uniform display scale (D17/Bug-2): the C# method may set the spawned
        # object's ``localScale`` (``rifle.transform.localScale = Vector3.one *
        # 0.2f``). Capture it from a localScale assignment in THIS method whose
        # receiver base IS the instantiate result symbol; UNIFORM only (ScaleTo is
        # uniform). Multiple uniform localScale writes on the object -> the LAST
        # wins (C# sequential assignment); none / non-uniform / non-positive ->
        # None (no capture — distinct from an explicit 1.0).
        scale: float | None = None
        for sm in _CS_LOCALSCALE_RE.finditer(source, body_open, body_close):
            if not _cs_pos_is_code(source, sm.start()):
                continue
            if _setparent_receiver_base(sm.group(1)) not in receiver_bases:
                continue  # localScale on a DIFFERENT object -> not the equip prefab
            parsed = _parse_uniform_scale_rhs(sm.group(2))
            if parsed is not None:
                scale = parsed
        candidates.append((method, prefab, scale))

    # >1 distinct qualifying (method, prefab) -> ambiguous obligation -> ABSTAIN
    # (the single carrier holds one obligation; bias to the recognizer's abstain).
    distinct = {(m, p) for m, p, _ in candidates}
    if len(distinct) != 1:
        return None
    method_name = candidates[0][0]
    # OVERLOAD COLLAPSE (D8 abstain-on-ambiguity): the obligation is keyed by bare
    # method NAME, but C# permits overloads sharing a name (``GetRifle(int)`` /
    # ``GetRifle(Transform)``). If >1 C# method declaration shares the resolved
    # ``equip_method`` name, the Luau-side lowering/verifier cannot disambiguate the
    # rewrite site, so ABSTAIN rather than bind one arbitrary obligation.
    if _count_cs_methods_named(source, method_name) > 1:
        return None
    return candidates[0]


def _count_cs_methods_named(source: str, name: str) -> int:
    """The number of distinct C# method DECLARATIONS named ``name`` (code positions
    only). Used to ABSTAIN on overload collapse — two ``GetRifle(…)`` overloads
    collapse to one bare-name obligation, so the recognizer must not bind either."""
    count = 0
    for m in _CS_METHOD_DECL_RE.finditer(source):
        if not _cs_pos_is_code(source, m.start()):
            continue
        if m.group(1) in _CS_NOT_A_METHOD_NAME:
            continue
        if m.group(1) == name:
            count += 1
    return count


def _resolve_rig_facts(
    source: str,
    host_node: HostNode,
    parsed_scenes: list[ParsedScene] | None,
    prefab_library: PrefabLibrary | None,
) -> list[RigRootedRetargetFact]:
    """Resolve Camera.main-rooted ``<field> = <camrecv>.GetChild(n)`` RETARGET
    writes to ``RigRootedRetargetFact(field_name, child_name)``. Each is admitted
    iff the receiver roots EXACTLY at ``Camera.main.transform`` (host-XOR-rig,
    exact-match) AND the MainCamera-tagged node is unique AND the n-th child
    resolves under E1–E3. Pure; code-position-aware."""
    rig_facts: list[RigRootedRetargetFact] = []
    main_cam: HostNode | None = None
    main_cam_looked_up = False
    for m in _CS_CAM_GETCHILD_RE.finditer(source):
        if not _cs_pos_is_code(source, m.start()):
            continue
        field = _lhs_is_bare_field(source, m.start(1), m.group(1))
        if field is None:
            continue  # foreign member-access LHS -> abstain
        canon = _canonical_receiver(source, m.group(2), m.start())
        if canon != _CAMERA_MAIN_TRANSFORM:
            continue  # receiver does not root at Camera.main.transform -> abstain
        ordinal = int(m.group(3))
        if not main_cam_looked_up:
            main_cam = _main_camera_node(host_node, parsed_scenes, prefab_library)
            main_cam_looked_up = True
        if main_cam is None:
            continue  # E-no-tag / E-non-unique -> abstain
        child = _resolve_child(main_cam, ordinal)
        if child is None:
            continue  # E1–E3 -> abstain
        name = getattr(child, "name", "") or ""
        # NEW (Phase 1): does this rig fact ALSO carry an equip obligation? The
        # recognizer keys on the SAME proven slot ``field`` (receiver-agnostic, so
        # the seeded one-hop and direct forms behave identically). Abstain -> empty
        # equip fields -> the equip lowering/verifier do not fire (D8/D11).
        equip = _resolve_equip_obligation(source, field)
        equip_method = equip[0] if equip is not None else ""
        prefab_field = equip[1] if equip is not None else ""
        equip_scale = equip[2] if equip is not None else None
        rig_facts.append(
            RigRootedRetargetFact(
                field_name=field,
                child_name=name,
                cam_receiver=m.group(2),
                ordinal=ordinal,  # credited GetChild(n) -> carrier cam_ordinal
                equip_method=equip_method,
                prefab_field=prefab_field,
                equip_scale=equip_scale,
            )
        )
    return rig_facts


def _resolve_script(
    source: str,
    host_node: HostNode,
    *,
    parsed_scenes: list[ParsedScene] | None,
    prefab_library: PrefabLibrary | None,
) -> ChildRefScript | None:
    """Resolve one script's GetChild sites against ``host_node``. Returns
    ``None`` when the script has NO GetChild site at all (absent from the map)."""
    gameobject_shadowed = _declares_shadow(source, "gameObject")
    table = _build_symbol_table(
        source, host_node, gameobject_shadowed=gameobject_shadowed
    )

    facts: list[ChildRefFact] = []
    getchild_total = 0
    for m in _CS_GETCHILD_RE.finditer(source):
        if not _cs_pos_is_code(source, m.start()):
            continue
        getchild_total += 1
        recv, ordinal = m.group(1), int(m.group(2))
        # A receiver that is a MEMBER ACCESS on a foreign expression
        # (``Camera.main.transform.GetChild(0)``, ``foo.transform.GetChild(0)``,
        # or a ``gameObject.transform`` whose ``gameObject`` is shadowed by a
        # local/param) is NOT the script's own host — abstain (counts toward
        # getchild_total, produces no host-rooted fact). At exactly this abstain
        # point the rig path (below) admits the Camera.main -> MainCamera-tag
        # retarget, so a Camera.main site that drops here as a host fact is
        # re-captured as a rig fact (no double-count — edge 9/10).
        if _receiver_is_member_access(
            source, m.start(1), gameobject_shadowed=gameobject_shadowed
        ):
            continue  # foreign member-access receiver — abstain (E9)
        recv_node = table.get(recv)
        if recv_node is None:
            continue  # receiver not transform-rooted — abstain (E9)
        child = _resolve_child(recv_node, ordinal)
        if child is None:
            continue  # guard failure — abstain (E1–E3)
        name = getattr(child, "name", "") or ""
        facts.append(
            ChildRefFact(
                site=m.group(0),
                receiver=recv,
                ordinal=ordinal,
                child_name=name,
            )
        )

    rig_facts = _resolve_rig_facts(
        source, host_node, parsed_scenes, prefab_library
    )

    if getchild_total == 0:
        return None
    return ChildRefScript(
        facts=tuple(facts),
        getchild_total=getchild_total,
        resolved_total=len(facts) + len(rig_facts),
        rig_facts=tuple(rig_facts),
    )


def build_child_ref_map(
    *,
    script_infos: list[ScriptInfo],
    parsed_scenes: list[ParsedScene] | None,
    prefab_library: PrefabLibrary | None,
    guid_index: GuidIndex | None,
) -> ChildRefMap:
    """For every script attached (as a MonoBehaviour) to a scene/prefab node,
    resolve each transform-ROOTED ``<recv>.GetChild(n)`` site in its C# source —
    following the local chain of Transform symbols rooted at ``transform`` (the
    host node) via a per-script symbol table + fixpoint — to the resolved node's
    n-th authored child name. Records the resolved facts AND the per-script
    resolved/total tally. A site whose receiver does NOT root at the host node,
    or that hits a §7.2 edge guard, produces NO fact. A script with >= 1
    GetChild site is present in the map with its tally; a script with 0 GetChild
    sites is absent. Pure."""
    by_path = _node_by_cs_path(parsed_scenes, prefab_library, guid_index)
    result: ChildRefMap = {}
    for info in script_infos:
        path = getattr(info, "path", None)
        if path is None:
            continue
        key = _canon_key(path)
        hosts = by_path.get(key)
        # Resolve only when EXACTLY one host node maps to this script — an
        # ambiguous host has no single transform-rooted base to chain from.
        if not hosts or len(hosts) != 1:
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        entry = _resolve_script(
            source, hosts[0],
            parsed_scenes=parsed_scenes,
            prefab_library=prefab_library,
        )
        if entry is not None:
            result[key] = entry
    return result


def prerewrite_child_index(csharp_source: str, entry: ChildRefScript) -> tuple[str, int]:
    """Substitute each RESOLVED ``<receiver>.GetChild(n)`` SITE in
    ``csharp_source`` with ``<receiver>.Find("<child_name>")`` for every
    ``ChildRefFact`` in ``entry.facts``. The receiver symbol is PRESERVED
    (``transform`` stays ``transform``, ``tBase`` stays ``tBase``) — only
    ``.GetChild(n)`` -> ``.Find("<name>")``. Replaces by the exact ``fact.site``
    text (no positional regex re-derivation), code-position-aware (a site inside
    a C# comment/string is left untouched). A GetChild site NOT in
    ``entry.facts`` (resolution abstained) is left verbatim -> reaches the
    backstop. Returns (new_source, count_rewritten). Pure."""
    count = 0
    out = csharp_source
    for fact in entry.facts:
        replacement = f"{fact.receiver}.Find(\"{fact.child_name}\")"
        # Replace only code-position occurrences of this exact site text. Rebuild
        # the string left-to-right so positions stay valid as we substitute.
        search_from = 0
        rebuilt: list[str] = []
        while True:
            idx = out.find(fact.site, search_from)
            if idx == -1:
                rebuilt.append(out[search_from:])
                break
            if _cs_pos_is_code(out, idx):
                rebuilt.append(out[search_from:idx])
                rebuilt.append(replacement)
                count += 1
                search_from = idx + len(fact.site)
            else:
                rebuilt.append(out[search_from : idx + len(fact.site)])
                search_from = idx + len(fact.site)
        out = "".join(rebuilt)
    return out, count
