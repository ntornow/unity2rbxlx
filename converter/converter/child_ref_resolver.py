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
from pathlib import Path

# Verified import paths — these match serialized_field_extractor.py exactly.
# ``converter/`` is on sys.path, so the package prefix is ``core.``, and
# GuidIndex / ParsedScene / PrefabLibrary all live in core.unity_types.
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


# Per-script resolution outcome, keyed on the canonical .cs path key. Carries
# the resolved facts (what the pre-rewrite rewrites) AND the per-script
# resolved/total tally (what the backstop asserts against). A script with >= 1
# GetChild site is present; a script with 0 GetChild sites is absent from the map.
@dataclass(frozen=True)
class ChildRefScript:
    facts: tuple[ChildRefFact, ...]  # one per RESOLVED site (rewrite targets)
    getchild_total: int  # all GetChild SITES seen in this script
    resolved_total: int  # len(facts) == sites that produced a fact


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


def _canon_key(path: Path) -> str:
    """The canonical .cs path key: ``str(path.resolve())`` when resolvable, else
    the raw string (a synthetic test path that doesn't exist on disk)."""
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _resolve_script(source: str, host_node: HostNode) -> ChildRefScript | None:
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
        # getchild_total, produces no fact) so the backstop treats it as a
        # coverage gap.
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

    if getchild_total == 0:
        return None
    return ChildRefScript(
        facts=tuple(facts),
        getchild_total=getchild_total,
        resolved_total=len(facts),
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
        entry = _resolve_script(source, hosts[0])
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
