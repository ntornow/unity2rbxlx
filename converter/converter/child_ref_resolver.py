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
_CS_GETCHILD_LOCAL_RE = re.compile(  # Transform x = recv.GetChild(0); | var x = ...
    r"\b(?:Transform|var)\s+([A-Za-z_]\w*)\s*=\s*"
    r"([A-Za-z_]\w*)\.GetChild\(\s*(\d+)\s*\)"
)
_CS_GETCHILD_GETTER_BLOCK_RE = re.compile(  # Transform x { get { return recv.GetChild(0); } }
    r"\bTransform\s+([A-Za-z_]\w*)\s*\{\s*get\s*\{\s*return\s+"
    r"([A-Za-z_]\w*)\.GetChild\(\s*(\d+)\s*\)\s*;\s*\}"
)
_CS_GETCHILD_GETTER_EXPR_RE = re.compile(  # Transform x => recv.GetChild(0);
    r"\bTransform\s+([A-Za-z_]\w*)\s*=>\s*"
    r"([A-Za-z_]\w*)\.GetChild\(\s*(\d+)\s*\)"
)


def _cs_pos_is_code(source: str, pos: int) -> bool:
    """True if char index ``pos`` is real C# code, not inside a string literal
    or a ``//`` line comment. Scans from the start of ``pos``'s line, tracking
    single/double-quoted strings (with backslash escapes) and ``//`` comments —
    the forms the textual matchers must avoid firing inside."""
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
        elif ch == "/" and i + 1 < pos and source[i + 1] == "/":
            return False  # rest of the line (incl. pos) is a comment
        i += 1
    return quote is None


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


def _build_symbol_table(source: str, host_node: HostNode) -> dict[str, HostNode]:
    """Build the per-script Transform-symbol table by local dataflow.

    Seed ``transform -> host_node``, then resolve to a fixpoint over the three
    C# definition shapes (local-var assignment, block-bodied getter,
    expression-bodied getter): each ``<sym> = <recv>.GetChild(n)`` is a pending
    edge resolved once ``<recv>`` is in the table. A guard failure (E1–E3) drops
    the edge — its ``<sym>`` stays unresolved, so any later site on it abstains.
    """
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
            pending.append((m.group(1), m.group(2), int(m.group(3))))

    table: dict[str, HostNode] = {"transform": host_node}
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
    table = _build_symbol_table(source, host_node)

    facts: list[ChildRefFact] = []
    getchild_total = 0
    for m in _CS_GETCHILD_RE.finditer(source):
        if not _cs_pos_is_code(source, m.start()):
            continue
        getchild_total += 1
        recv, ordinal = m.group(1), int(m.group(2))
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
