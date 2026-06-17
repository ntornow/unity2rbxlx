"""Post-transpile lowering: re-lower an Addressables-label-roster CONSUMER.

Phase 2 of the Unit-4 roster feature. Phase 1 (the producer) emits, into the
place, a roster of clonable, ``CollectionService``-tagged, ``characterName``-
attributed instances under a dedicated container in ``ReplicatedStorage`` (the
``by_label`` surface). This module is the CONSUMER half: it rewrites the
transpiled roster-consumer module (e.g. Trash-Dash's ``CharacterDatabase``,
identified STRUCTURALLY — never by a game literal) so its
``LoadDatabase``/``GetCharacter``/``dictionary``/``loaded`` methods return the
``Character`` object graph the downstream ``LoadoutState`` dereferences,
resolved from Phase 1's tagged surface.

WHY a deterministic re-lowering (not an AI fingerprint): the AI transpiler has
drifted the consumer across at least three shapes (``WaitForChild`` folder,
``FindFirstChild`` folder, ``CollectionService:GetTagged``). Keying on the AI
output misfires across those shapes and on any other label loader. Instead the
TRIGGER is the DETERMINISTIC UPSTREAM fact — the module whose ORIGINAL C# calls
``Addressables.LoadAssetsAsync<T>("<L>", ...)`` for an ``<L>`` present in
``scene_runtime["addressables"]["by_label"]`` (D-P2-1) — and the REWRITE
whole-region-replaces the four public methods with a canonical, game-agnostic
body built from the LOCATED receiver-table name ``<N>`` (the C# class/file stem,
read from the ``function <N>.LoadDatabase`` anchor) + the fact + the Phase-1
contract constants. No ``CharacterDatabase`` / ``characters`` literal lives in
this module or in the emitted template (D-P2-2 / AC8).

The emitted body (D-P2-2..D-P2-5):
  * reads the tag surface ``CollectionService:GetTagged("<L>")``, scoped to
    ``IsDescendantOf(ReplicatedStorage)`` and sorted by name (D-P2-5: GetTagged
    is global + unordered);
  * binds ``c.gameObject`` to the SCRIPT-BEARING
    ``ReplicatedStorage.Templates.<characterName>`` child, NOT the script-
    stripped roster member (D-P2-4), falling back to the tagged member;
  * builds ``<C>.new({ characterName = ... })`` with ``.accessories = {}``
    (the spike's minimal non-crashing shape; populated accessories are a
    follow-on);
  * OWNS its own ``<DICT>``/``<LOADED>`` module-locals (never assumes the AI's
    ``m_CharactersDict``/``m_Loaded`` upvalues survive).

The lowering STAMPS the ``roster_binding`` carrier on each re-lowered script so
the dead-module analysis (whose inert-body classifier would otherwise re-flag
the canonical body dead and reroute it to an inert stub — NEW-FINDING-B) exempts
it deterministically, mirroring the ``rig_binding`` precedent.

Pure except the documented lowering side effect: it mutates only the
``luau_source`` / ``roster_binding`` of the scripts it is handed (the sibling-
pass convention shared with ``lower_trigger_stay`` / ``lower_rifle_rig_retarget``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol


class _HasSourceAndCarrier(Protocol):
    source_path: str
    luau_source: str
    roster_binding: dict[str, object] | None


@dataclass(frozen=True)
class RosterConsumerFact:
    """One module that label-loads a prefab roster via Addressables.

    Derived from the module's ORIGINAL C# source (deterministic upstream), NOT
    the transpiled Luau. ``label`` is a key present in
    ``addressables.by_label``; ``component_type`` and ``index_key`` come from
    the ``LoadAssetsAsync`` callback body (``op.GetComponent<T>()`` / the dict
    key) when statically present, else ``None`` (the canonical body falls back).
    """

    source_path: str            # the .cs path (== TranspiledScript.source_path)
    label: str                  # the by_label label the module loads
    component_type: str | None  # T in op.GetComponent<T>(), e.g. "Character"
    index_key: str | None       # the dict key attribute, e.g. "characterName"


# ``Addressables.LoadAssetsAsync<T>("<label>", ...)`` — STRING-LITERAL first arg
# only. A non-literal label (a variable / field) cannot be mapped to by_label
# deterministically -> abstain (never fuzzy-match). ``LoadAsset(s)Async`` tolerates
# both the single- and multi-asset spellings.
_RE_LOAD_ASSETS = re.compile(
    r"""Addressables\s*\.\s*LoadAssets?Async\s*<\s*[\w.]+\s*>\s*\(\s*"([^"]+)"\s*,"""
)
# ``op.GetComponent<T>()`` inside the callback — best-effort component-type capture.
_RE_GET_COMPONENT = re.compile(r"""\.\s*GetComponent\s*<\s*([\w.]+)\s*>\s*\(""")
# ``<dict>.Add(c.<key>, ...)`` — best-effort index-key capture.
_RE_DICT_KEY = re.compile(r"""\.\s*Add\s*\(\s*\w+\s*\.\s*(\w+)\s*,""")


def find_roster_consumers(
    csharp_by_path: dict[str, str],
    by_label: dict[str, list[str]],
) -> dict[str, RosterConsumerFact]:
    """Return ``{source_path -> RosterConsumerFact}`` for each module that calls
    ``Addressables.LoadAssetsAsync<T>("<L>", ...)`` for an ``<L>`` present in
    ``by_label``.

    PURE. Abstains (the path is absent from the result) when:
      * ``by_label`` is empty (non-roster game);
      * the module's label literal is not a ``by_label`` key;
      * the label arg is not a string literal (no LoadAssetsAsync match);
      * the module loads >1 distinct in-``by_label`` label (ambiguous within
        one module — never first-match-wins).

    The project-level guards (>1 distinct module per label / stale artifact)
    live in the orchestrator's ``_roster_fail_closed`` (this finder cannot see
    them from a single module).
    """
    out: dict[str, RosterConsumerFact] = {}
    if not by_label:
        return out
    label_keys = set(by_label.keys())
    for path, source in csharp_by_path.items():
        if not isinstance(path, str) or not isinstance(source, str):
            continue
        # All in-by_label labels this module loads (distinct, first-seen order).
        labels: list[str] = []
        for m in _RE_LOAD_ASSETS.finditer(source):
            lbl = m.group(1)
            if lbl in label_keys and lbl not in labels:
                labels.append(lbl)
        if not labels:
            continue
        if len(labels) > 1:
            # A single module loading >1 distinct in-by_label label is ambiguous
            # -> abstain (the canonical body keys on exactly one label).
            continue
        label = labels[0]
        comp = _RE_GET_COMPONENT.search(source)
        key = _RE_DICT_KEY.search(source)
        out[path] = RosterConsumerFact(
            source_path=path,
            label=label,
            component_type=comp.group(1) if comp else None,
            index_key=key.group(1) if key else None,
        )
    return out


def csharp_label_loader_paths(csharp_by_path: dict[str, str]) -> list[str]:
    """Return the source paths whose ORIGINAL C# calls
    ``Addressables.LoadAssetsAsync<T>("<literal>", ...)`` -- the DETERMINISTIC
    "a roster was expected" signal, independent of any ``by_label`` block.

    Reuses the SAME ``_RE_LOAD_ASSETS`` regex ``find_roster_consumers`` keys on,
    so there is ONE source of the "roster expected" truth (no second regex). This
    is the C#-side signal the orchestrator uses to FAIL CLOSED when a roster IS
    expected but the ``scene_runtime`` carries no ``addressables`` block at all
    (a stale artifact predating the Unit-4 surface): in that case
    ``find_roster_consumers`` -- gated on a non-empty ``by_label`` -- silently
    returns ``{}``, so this independent C# fact is what makes the gap loud.

    PURE. Sorted for a deterministic order. A module with no string-literal
    LoadAssetsAsync call is absent (a non-literal label cannot be a roster the
    by_label surface would have planned).
    """
    paths: list[str] = []
    for path, source in csharp_by_path.items():
        if not isinstance(path, str) or not isinstance(source, str):
            continue
        if _RE_LOAD_ASSETS.search(source) is not None:
            paths.append(path)
    return sorted(paths)


# A code-level ``function <N>.<method>(`` declaration for one of the four public
# roster methods. ``<N>`` (group 1) is the receiver-table name — the C# class /
# file stem, which survives transpilation verbatim (verified against the real
# unit1 + unit2-proper outputs). This is a STRUCTURAL anchor, not an AI-body
# fingerprint.
_ROSTER_METHODS: tuple[str, ...] = ("dictionary", "loaded", "GetCharacter", "LoadDatabase")
_RE_ROSTER_METHOD = re.compile(
    r"^[ \t]*function\s+([A-Za-z_]\w*)\s*\.\s*(dictionary|loaded|GetCharacter|LoadDatabase)\s*\(",
    re.MULTILINE,
)
# The module-local state declarations that lead the four methods, e.g.
# ``local m_CharactersDict = nil`` / ``local m_Loaded = false``. The receiver-
# table declaration ``local <N> = {}`` precedes them but is KEPT (the module
# epilogue ``return <N>`` needs it); the canonical body re-declares only its own
# dict/loaded locals.

# The pass-OWNED module-local names (NOT the AI's m_CharactersDict / m_Loaded).
_DICT_LOCAL = "_roster_dict"
_LOADED_LOCAL = "_roster_loaded"


def _canonical_region(
    receiver: str,
    label: str,
    component_type: str | None,
    index_key: str | None,
) -> str:
    """Render the canonical re-lowered region for receiver-table ``receiver``.

    The output depends ONLY on ``receiver`` + the fact + the Phase-1 contract
    constants — identical bytes for a given ``receiver`` across all AI drift
    shapes (AC2) and deterministic across runs (AC7). No game/module literal is
    typed here: ``receiver`` is the located ``<N>``, the dict/loaded locals are
    pass-OWNED, and ``<C>``/``<K>``/``<L>`` are substituted from the fact.
    """
    comp = component_type or "Character"
    key = index_key or "characterName"
    return (
        f"local {_DICT_LOCAL} = nil\n"
        f"local {_LOADED_LOCAL} = false\n"
        f"\n"
        f"function {receiver}.dictionary()\n"
        f"\treturn {_DICT_LOCAL}\n"
        f"end\n"
        f"\n"
        f"function {receiver}.loaded()\n"
        f"\treturn {_LOADED_LOCAL}\n"
        f"end\n"
        f"\n"
        f"function {receiver}.LoadDatabase()\n"
        f"\tif {_DICT_LOCAL} == nil then\n"
        f"\t\t{_DICT_LOCAL} = {{}}\n"
        f"\t\tlocal CollectionService = game:GetService(\"CollectionService\")\n"
        f"\t\tlocal ReplicatedStorage = game:GetService(\"ReplicatedStorage\")\n"
        f"\t\tlocal {comp} = require(script.Parent.{comp})\n"
        f"\t\tlocal _templates = ReplicatedStorage:FindFirstChild(\"Templates\")\n"
        f"\t\t-- GetTagged is global + unordered: keep only members under\n"
        f"\t\t-- ReplicatedStorage and sort by name for a deterministic roster.\n"
        f"\t\tlocal _members = {{}}\n"
        f"\t\tfor _, op in CollectionService:GetTagged(\"{label}\") do\n"
        f"\t\t\tif op:IsDescendantOf(ReplicatedStorage) then\n"
        f"\t\t\t\ttable.insert(_members, op)\n"
        f"\t\t\tend\n"
        f"\t\tend\n"
        f"\t\ttable.sort(_members, function(a, b) return a.Name < b.Name end)\n"
        f"\t\tfor _, op in _members do\n"
        f"\t\t\tlocal _key = op:GetAttribute(\"{key}\") or op.Name\n"
        f"\t\t\t-- .gameObject binds the SCRIPT-BEARING Templates child (clonable\n"
        f"\t\t\t-- with its component scripts) so host.getComponent resolves later;\n"
        f"\t\t\t-- fall back to the tagged member when no Templates match.\n"
        f"\t\t\tlocal _go = (_templates and _templates:FindFirstChild(_key)) or op\n"
        f"\t\t\tlocal c = {comp}.new({{ {key} = _key }})\n"
        f"\t\t\tc.gameObject = _go\n"
        f"\t\t\tc.accessories = {{}}\n"
        f"\t\t\tif _key ~= nil then\n"
        f"\t\t\t\t{_DICT_LOCAL}[_key] = c\n"
        f"\t\t\tend\n"
        f"\t\tend\n"
        f"\t\t{_LOADED_LOCAL} = true\n"
        f"\tend\n"
        f"end\n"
        f"\n"
        f"function {receiver}.GetCharacter(type)\n"
        f"\tif {_DICT_LOCAL} == nil then\n"
        f"\t\treturn nil\n"
        f"\tend\n"
        f"\treturn {_DICT_LOCAL}[type]\n"
        f"end"  # NO trailing newline: the located span ends at the final ``end``
        # (``_method_block_end`` returns just past it), so a trailing newline here
        # would accumulate one blank line per run and break idempotency.
    )


def _locate_region(
    source: str,
) -> tuple[str, int, int] | None:
    """Locate the receiver-table name ``<N>`` and the contiguous char span to
    replace — from the FIRST module-local state declaration / first public roster
    method through the END of the LAST public roster method.

    Returns ``(receiver, start, end)`` or ``None`` when the LoadDatabase AND
    GetCharacter anchors cannot BOTH be located (fail-closed: the caller raises
    ``roster_unresolved``). All four method anchors must share one receiver name
    (the C# class/file stem) — a mismatch -> abstain.
    """
    matches = list(_RE_ROSTER_METHOD.finditer(source))
    if not matches:
        return None
    receivers = {m.group(1) for m in matches}
    if len(receivers) != 1:
        return None  # ambiguous receiver names -> abstain
    receiver = matches[0].group(1)
    seen = {m.group(2) for m in matches}
    # Both load-bearing anchors must be present, else fail closed.
    if "LoadDatabase" not in seen or "GetCharacter" not in seen:
        return None

    # Start: the receiver-private state decls (``local m_*``) that lead the first
    # method, if present, else the first method declaration. Find the line start
    # of the FIRST roster method, then walk backwards over immediately-preceding
    # ``local <ident> = ...`` lines and blank lines that are the state block.
    first_method_start = min(m.start() for m in matches)
    region_start = first_method_start
    # Walk back over preceding state-declaration lines (``local m_... = ...``)
    # and blank lines. Stop at the receiver-table decl ``local <N> = {}`` (KEEP
    # it — the module epilogue ``return <N>`` needs it) or any non-state line.
    lines_before = source[:first_method_start].split("\n")
    # Reconstruct char offsets walking backwards.
    consumed = 0
    state_decl_re = re.compile(r"^[ \t]*local\s+([A-Za-z_]\w*)\s*=")
    receiver_decl_re = re.compile(
        r"^[ \t]*local\s+" + re.escape(receiver) + r"\s*="
    )
    idx = len(lines_before) - 1
    # ``lines_before[-1]`` is the (empty) partial line just before the method;
    # iterate full lines above it.
    candidate_start = first_method_start
    while idx >= 0:
        line = lines_before[idx]
        line_len = len(line) + 1  # +1 for the newline
        if idx == len(lines_before) - 1:
            # The partial line directly before the method (its own indentation):
            # skip — it is consumed by first_method_start already.
            consumed += len(line)
            idx -= 1
            continue
        stripped = line.strip()
        if stripped == "":
            candidate_start -= line_len
            idx -= 1
            continue
        if receiver_decl_re.match(line):
            break  # keep the receiver-table decl
        if state_decl_re.match(line):
            candidate_start -= line_len
            idx -= 1
            continue
        # A comment line directly attached to the state block is included.
        if stripped.startswith("--"):
            candidate_start -= line_len
            idx -= 1
            continue
        break
    region_start = candidate_start

    # End: the closing ``end`` of the LAST public roster method. Find the last
    # method declaration, then its matching block close at module scope.
    last_method = max(matches, key=lambda m: m.start())
    end = _method_block_end(source, last_method.start())
    if end is None:
        return None
    return receiver, region_start, end


def _method_block_end(source: str, decl_start: int) -> int | None:
    """The char index just past the closing ``end`` of the ``function`` whose
    declaration begins at ``decl_start``. Tracks block-keyword nesting from the
    declaration's ``function`` keyword. ``None`` if unbalanced.

    Comment/string-aware enough for the deterministically-transpiled bodies (no
    ``end`` token appears inside a string in these modules); a long-bracket or
    short string containing ``end`` would mis-count, but the roster bodies do
    not carry one.
    """
    # Only ``function``/``do``/``then``/``repeat`` OPEN a block closed by
    # ``end``/``until``. ``if``/``for``/``while`` headers do NOT open the block
    # themselves -- their ``then``/``do`` does (counting the header too would
    # double-count). ``elseif`` cancels its own upcoming ``then`` (an
    # ``if .. then .. elseif .. then .. end`` chain has 2 ``then`` openers but
    # 1 ``end``).
    block_re = re.compile(
        r"\b(function|do|then|repeat|until|end|elseif)\b"
    )
    depth = 0
    i = decl_start
    n = len(source)
    started = False
    while i < n:
        # Skip a line comment.
        if source[i] == "-" and i + 1 < n and source[i + 1] == "-":
            nl = source.find("\n", i)
            i = n if nl == -1 else nl
            continue
        # Skip a short string.
        if source[i] in ("'", '"'):
            quote = source[i]
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\":
                    i += 2
                    continue
                i += 1
            i += 1
            continue
        m = block_re.match(source, i)
        if m is None:
            i += 1
            continue
        word = m.group(1)
        if word in ("function", "do", "then", "repeat"):
            depth += 1
            started = True
        elif word == "elseif":
            depth -= 1  # cancels its own upcoming ``then``
        elif word in ("end", "until"):
            depth -= 1
            if started and depth == 0:
                return m.end()
            if depth < 0:
                return None
        i = m.end()
    return None


class RosterUnresolved(Exception):
    """Raised when a located roster-consumer fact's LoadDatabase/GetCharacter
    anchors cannot be located in the transpiled body (E-P2-2). The orchestrator
    drains this into a ``roster_unresolved`` ``FailClosed`` row so the conversion
    errors rather than shipping an empty loadout."""

    def __init__(self, source_path: str, label: str) -> None:
        super().__init__(
            f"roster consumer {source_path!r} (label {label!r}) is a located "
            f"Addressables-label loader but its LoadDatabase/GetCharacter "
            f"methods could not be located in the transpiled body; the roster "
            f"consumer cannot be re-lowered."
        )
        self.source_path = source_path
        self.label = label


def lower_roster_consumers(
    scripts: list[_HasSourceAndCarrier],
    facts: dict[str, RosterConsumerFact],
    container_name: str,
) -> int:
    """Whole-region-replace the four public roster methods of each consumer in
    ``facts`` with the canonical object-graph body (reading Phase 1's tagged
    surface), and STAMP the ``roster_binding`` carrier.

    Returns the number of modules re-lowered.

    ``container_name`` is diagnostic only (the discovery key is the
    CollectionService tag, not the container name) — it is NOT embedded in the
    emitted body.

    Raises ``RosterUnresolved`` for a located fact whose LoadDatabase/GetCharacter
    anchors cannot be located (fail-closed, E-P2-2).

    PURE except the documented in-place ``luau_source`` / ``roster_binding``
    mutation on the scripts in ``facts`` (the sibling-pass convention). Modules
    not in ``facts`` are untouched (generality gate, E-P2-6). Idempotent: a
    second run re-locates the same anchors (the canonical block re-declares the
    four methods) and re-renders byte-identical output (AC7).
    """
    lowered = 0
    for script in scripts:
        fact = facts.get(script.source_path)
        if fact is None:
            continue
        located = _locate_region(script.luau_source)
        if located is None:
            raise RosterUnresolved(script.source_path, fact.label)
        receiver, start, end = located
        region = _canonical_region(
            receiver, fact.label, fact.component_type, fact.index_key
        )
        new_source = script.luau_source[:start] + region + script.luau_source[end:]
        script.luau_source = new_source
        script.roster_binding = {
            "label": fact.label,
            "receiver": receiver,
            "lowered": True,
        }
        lowered += 1
    return lowered
