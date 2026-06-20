"""Post-transpile lowering: re-lower an Addressables-label SO-STORE database
CONSUMER (the keyed-dictionary sibling of ``roster_consumer_lowering.py``).

The roster lowering handles a prefab ``<GameObject>`` roster (the source surface
is ``CollectionService``-tagged instances). THIS module handles the
keyed-dictionary ScriptableObject-store shape — a database whose ORIGINAL C#
calls ``Addressables.LoadAssetsAsync<SOType>("<label>", op => dict.Add(op.KEY,
op))`` and whose transpiled ``LoadDatabase`` body is a CLOSURE-PRIVATE keyed dict
(a ``local <dict> = nil`` upvalue, populated by a ``addTheme(op)`` closure that is
never called). No external shim can reach a closure-private local, so the DB
module must own its own keyed write: we WHOLE-REGION-REPLACE the contiguous
``LoadDatabase``/getter/``dictionnary``/``loaded`` block with a canonical body
that ``require``s the emitted SO modules and writes ``dict[so[<key>]] = so``.

WHY a deterministic re-lowering (not an AI fingerprint): the TRIGGER is the
DETERMINISTIC UPSTREAM fact — the module whose ORIGINAL C# issues a
``LoadAssetsAsync<SOType>("<L>", ...)`` for an ``<L>`` present in the SO-seed
surface AND whose ``<SOType>`` resolves to ≥1 emitted SO module — and the REWRITE
whole-region-replaces the four public methods with a canonical body built from the
LOCATED receiver-table name ``<N>`` (the C# class/file stem, read from the
``function <N>.LoadDatabase`` anchor — never a per-game literal like
``GetThemeData``/``dictionnary``). The accessor / dict-getter / loaded-getter
NAMES are LOCATED from the transpiled region, not hardcoded.

The ``<T>`` bind is load-bearing for DISJOINTNESS with the roster lowering: a
Unity Addressables *label* is many-to-many with assets, so a label applied to
both a prefab and an SO appears in both surfaces. ``find_so_db_consumers``
captures ``<T>`` and produces a fact ONLY when the label's SO-surface guids
resolve to a non-empty ``so_guid_to_module_path`` set (i.e. ``<T>`` is an
emitted-SO type); a ``<GameObject>`` load on a shared label abstains. A
belt-and-suspenders ``roster_claimed_paths`` exclusion stops a double-rewrite even
in a pathological both-resolve case.

Pure except the documented lowering side effect: it mutates only the ``source``
of the scripts it is handed (the sibling-pass convention).
"""

from __future__ import annotations

import re
from typing import NamedTuple, Protocol


class _HasSourcePath(Protocol):
    source_path: str | None
    source: str


class SoDbConsumerFact(NamedTuple):
    """One module that label-loads a keyed-dictionary SO store via Addressables.

    Derived from the module's ORIGINAL C# source (deterministic upstream), NOT
    the transpiled Luau. ``label`` is a key present in the SO-seed surface;
    ``so_type`` is the ``<T>`` type arg (last dotted segment); ``key_field`` is
    the ``Add(op.<key>, op)`` key (``None`` -> key-by-module-stem fallback,
    edge 4); ``so_module_paths`` are the dotted plan paths of the owned SO
    modules.
    """

    source_path: str
    label: str
    so_type: str
    key_field: str | None
    so_module_paths: tuple[str, ...]


class SoDbUnresolved(Exception):
    """Raised when a located SO-DB-consumer fact's LoadDatabase/getter anchors
    cannot be located in the transpiled body (edge 7). The orchestrator drains
    this into a fail-closed row so the conversion errors rather than shipping an
    empty keyed dictionary silently (symmetric with ``RosterUnresolved``)."""

    def __init__(self, source_path: str, label: str) -> None:
        super().__init__(
            f"SO-DB consumer {source_path!r} (label {label!r}) is a located "
            f"Addressables-label SO loader but its LoadDatabase/getter methods "
            f"could not be located in the transpiled body; the SO-store database "
            f"cannot be re-lowered."
        )
        self.source_path = source_path
        self.label = label


# ``Addressables.LoadAssetsAsync<T>("<label>", ...)`` — captures BOTH the ``<T>``
# type arg (load-bearing for disjointness, group ``sotype``) AND the
# STRING-LITERAL first arg (group ``label``). A non-literal label cannot be
# mapped to the surface deterministically -> no match -> abstain.
_RE_LOAD_ASSETS = re.compile(
    r"""Addressables\s*\.\s*LoadAssets?Async\s*<\s*(?P<sotype>[\w.]+)\s*>\s*"""
    r"""\(\s*"(?P<label>[^"]+)\"""",
)
# ``<dict>.Add(op.<key>, ...)`` — best-effort store-key capture.
_RE_DICT_KEY = re.compile(r"""\.\s*Add\s*\(\s*\w+\s*\.\s*(?P<field>\w+)\s*,""")


def _last_segment(dotted: str) -> str:
    """Last dotted segment of a (possibly namespaced) C# type — Roblox modules
    are emitted by their stem, so ``My.Game.ThemeData`` -> ``ThemeData``."""
    return dotted.split(".")[-1]


def find_so_db_consumers(
    csharp_by_path: dict[str, str],
    so_addr_by_label: dict[str, list[str]],
    so_addr_by_address: dict[str, list[str]],
    so_guid_to_module_path: dict[str, str],
    roster_claimed_paths: frozenset[str] = frozenset(),
) -> dict[str, SoDbConsumerFact]:
    """Return ``{source_path -> SoDbConsumerFact}`` for each module whose ORIGINAL
    C# calls ``LoadAssetsAsync<SOType>("<L>", ...)`` for an ``<L>`` present in the
    SO surface (``so_addr_by_label`` U ``so_addr_by_address``) AND whose ``<L>``
    SO-surface guids resolve to >=1 emitted SO module.

    PURE. Abstains (the path is absent from the result) when:
      * the SO surface is empty (no SO-store DB game);
      * the label arg is not a string literal (no match);
      * the module's label literal is not in the SO surface;
      * the module loads >1 distinct in-surface label (ambiguous);
      * ``<SOType>`` resolves to ZERO emitted SO modules for the label (edge 5 —
        a ``<GameObject>`` roster load on a shared label routes to the roster
        lowering, not here);
      * ``source_path in roster_claimed_paths`` (layer-(a) disjointness guard:
        the roster lowering already rewrote this module THIS run).
    """
    out: dict[str, SoDbConsumerFact] = {}
    if not so_addr_by_label and not so_addr_by_address:
        return out
    for path, source in csharp_by_path.items():
        if not isinstance(path, str) or not isinstance(source, str):
            continue
        if path in roster_claimed_paths:
            continue  # layer (a): the roster lowering already claimed this module
        # All in-surface labels this module loads, with the <T> seen at each.
        labels: list[str] = []
        sotype_by_label: dict[str, str] = {}
        for m in _RE_LOAD_ASSETS.finditer(source):
            lbl = m.group("label")
            in_surface = lbl in so_addr_by_label or lbl in so_addr_by_address
            if in_surface and lbl not in labels:
                labels.append(lbl)
                sotype_by_label[lbl] = m.group("sotype")
        if not labels:
            continue
        if len(labels) > 1:
            # A single module loading >1 distinct in-surface label is ambiguous
            # -> abstain (the canonical body keys on exactly one label).
            continue
        label = labels[0]
        # <T>-resolves-to-SO gate (layer (b), primary): the label's SO-surface
        # guids must resolve to >=1 emitted SO module. A <GameObject> roster load
        # on a shared label resolves to NO emitted SO modules -> abstain.
        surface_guids: list[str] = []
        _seen: set[str] = set()
        for guid in (
            list(so_addr_by_label.get(label) or [])
            + list(so_addr_by_address.get(label) or [])
        ):
            if guid not in _seen:
                _seen.add(guid)
                surface_guids.append(guid)
        module_paths: list[str] = []
        _seen_paths: set[str] = set()
        for guid in surface_guids:
            mod = so_guid_to_module_path.get(guid)
            if mod and mod not in _seen_paths:
                _seen_paths.add(mod)
                module_paths.append(mod)
        if not module_paths:
            continue
        key_m = _RE_DICT_KEY.search(source)
        out[path] = SoDbConsumerFact(
            source_path=path,
            label=label,
            so_type=_last_segment(sotype_by_label[label]),
            key_field=key_m.group("field") if key_m else None,
            so_module_paths=tuple(module_paths),
        )
    return out


# A code-level ``function <N>.<method>(`` declaration for one of the four public
# SO-DB methods. ``<N>`` (group 1) is the receiver-table name — the C# class /
# file stem, which survives transpilation verbatim. ``<method>`` (group 2) is
# LOCATED (the accessor / getter names are read here, not hardcoded). The four
# methods are: the dict getter, the loaded getter, the keyed accessor, and the
# load method — anchored by SHAPE, not by a per-game name. Because the accessor /
# getter NAMES are game-specific (``GetThemeData`` / ``dictionnary``), the anchor
# matches ANY ``function <N>.<ident>(`` at module scope; the receiver ``<N>`` must
# be single and the located block must contain the C#-derived load method.
_RE_DB_METHOD = re.compile(
    r"^[ \t]*function\s+([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)


def _method_block_end(source: str, decl_start: int) -> int | None:
    """The char index just past the closing ``end`` of the ``function`` whose
    declaration begins at ``decl_start``. Tracks block-keyword nesting; comment/
    string-aware. ``None`` if unbalanced. (Mirrors the roster locator's logic.)"""
    block_re = re.compile(r"\b(function|do|then|repeat|until|end|elseif)\b")
    depth = 0
    i = decl_start
    n = len(source)
    started = False
    while i < n:
        if source[i] == "-" and i + 1 < n and source[i + 1] == "-":
            nl = source.find("\n", i)
            i = n if nl == -1 else nl
            continue
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
            depth -= 1
        elif word in ("end", "until"):
            depth -= 1
            if started and depth == 0:
                return m.end()
            if depth < 0:
                return None
        i = m.end()
    return None


# The pass-OWNED module-local dict/loaded names (NOT the AI's themeDataList /
# m_Loaded upvalues, which the canonical body re-owns).
_DICT_LOCAL = "_so_db_dict"
_LOADED_LOCAL = "_so_db_loaded"


def _locate_region(
    source: str, load_method: str,
) -> tuple[str, str, str, int, int] | None:
    """Locate the receiver-table name ``<N>`` + the dict-getter + loaded-getter +
    the contiguous char span to replace — from the FIRST module-local state
    declaration / first public method through the END of the LAST public method.

    Returns ``(receiver, dict_getter, loaded_getter, start, end)`` or ``None``
    when the load method anchor (or a single shared receiver) cannot be located
    (fail-closed: the caller raises ``SoDbUnresolved``).

    The dict getter is the method whose body is ``return <dictlocal>``; the loaded
    getter is the method whose body returns a bool local. They are LOCATED
    structurally so the canonical body can re-emit them under their original
    (game-specific) names so callers like ``ThemeDatabase.dictionnary()`` keep
    resolving.
    """
    matches = list(_RE_DB_METHOD.finditer(source))
    if not matches:
        return None
    receivers = {m.group(1) for m in matches}
    if len(receivers) != 1:
        return None  # ambiguous receiver names -> abstain
    receiver = matches[0].group(1)
    method_names = [m.group(2) for m in matches]
    if load_method not in method_names:
        return None  # the C#-derived load method must be present, else abstain

    # Locate the dict getter (``return <local>`` returning the keyed dict) and the
    # loaded getter (``return <local>`` returning the bool flag). The transpiled
    # shape declares ``local <dict> = nil`` and ``local <loaded> = false`` upvalues
    # and getters that ``return`` them. Identify each getter by the local name it
    # returns and that local's initializer.
    state_local_init: dict[str, str] = {}
    for sm in re.finditer(
        r"^[ \t]*local\s+([A-Za-z_]\w*)\s*=\s*(nil|false|true)\s*$",
        source, re.MULTILINE,
    ):
        state_local_init[sm.group(1)] = sm.group(2)

    dict_getter: str | None = None
    loaded_getter: str | None = None
    for m in matches:
        name = m.group(2)
        end = _method_block_end(source, m.start())
        if end is None:
            continue
        body = source[m.end():end]
        ret = re.search(r"return\s+([A-Za-z_]\w*)\s*(?:\n|end|$)", body)
        if ret is None:
            continue
        returned = ret.group(1)
        init = state_local_init.get(returned)
        if init == "nil" and dict_getter is None:
            dict_getter = name
        elif init in ("false", "true") and loaded_getter is None:
            loaded_getter = name
    if dict_getter is None or loaded_getter is None:
        return None

    # Region span: from the receiver-private state decls (``local <x> = nil`` /
    # ``= false``) leading the first method, through the END of the last method.
    first_method_start = min(m.start() for m in matches)
    lines_before = source[:first_method_start].split("\n")
    state_decl_re = re.compile(
        r"^[ \t]*local\s+[A-Za-z_]\w*\s*=\s*"
        r"(?:nil|false|true|\{\s*\}|-?\d[\d.eExXaAbBcCdDfF]*)\s*$"
    )
    receiver_decl_re = re.compile(
        r"^[ \t]*local\s+" + re.escape(receiver) + r"\s*="
    )
    idx = len(lines_before) - 1
    candidate_start = first_method_start
    while idx >= 0:
        line = lines_before[idx]
        line_len = len(line) + 1
        if idx == len(lines_before) - 1:
            idx -= 1
            continue
        stripped = line.strip()
        if stripped == "":
            candidate_start -= line_len
            idx -= 1
            continue
        if receiver_decl_re.match(line):
            break  # keep the receiver-table decl (module epilogue returns it)
        if state_decl_re.match(line):
            candidate_start -= line_len
            idx -= 1
            continue
        if stripped.startswith("--"):
            candidate_start -= line_len
            idx -= 1
            continue
        break
    region_start = candidate_start

    last_method = max(matches, key=lambda m: m.start())
    end = _method_block_end(source, last_method.start())
    if end is None:
        return None
    return receiver, dict_getter, loaded_getter, region_start, end


def _canonical_region(
    receiver: str,
    load_method: str,
    dict_getter: str,
    loaded_getter: str,
    accessor: str,
    key_field: str | None,
    so_module_paths: tuple[str, ...],
) -> str:
    """Render the canonical re-lowered region for receiver-table ``receiver``.

    The output depends ONLY on ``receiver`` + the fact + the located method names
    — identical bytes for a given fact across runs (idempotent, edge 8). The
    lowered ``LoadDatabase`` ``require``s each SO module (resolving its dotted plan
    path from the DataModel root) and writes ``dict[so[<key>]] = so``. When
    ``key_field`` is ``None`` (edge 4) it keys by the SO module STEM so the dict is
    still NON-EMPTY (``pairs``-iterable). Edge 6: a nil key is skipped.
    """
    # State locals are emitted as bare ``local x = nil`` / ``= false`` lines that
    # the region locator's walk-back absorbs on a re-run, so the span is byte-
    # stable (idempotent, edge 8). The path resolver is INLINED into LoadDatabase
    # (no module-level ``local function`` helper — that would be a STOP boundary
    # for the walk-back and break idempotency).
    if key_field is not None:
        key_expr = f"so[{key_field!r}]"
    else:
        # edge 4: no Add(op.F, op) key — key by the SO module stem so the dict is
        # non-empty (pairs-iterable). The stem is the last dotted segment.
        key_expr = "_stem"
    lines: list[str] = [
        f"local {_DICT_LOCAL} = nil",
        f"local {_LOADED_LOCAL} = false",
        "",
        f"function {receiver}.{dict_getter}()",
        f"\treturn {_DICT_LOCAL}",
        "end",
        "",
        f"function {receiver}.{loaded_getter}()",
        f"\treturn {_LOADED_LOCAL}",
        "end",
        "",
        f"function {receiver}.{load_method}()",
        f"\tif {_DICT_LOCAL} == nil then",
        f"\t\t{_DICT_LOCAL} = {{}}",
        "\t\tlocal _paths = {",
    ]
    for path in so_module_paths:
        lines.append(f"\t\t\t{path!r},")
    lines += [
        "\t\t}",
        "\t\tfor _, _p in ipairs(_paths) do",
        # Resolve the dotted plan path to its required module table (first segment
        # is a service, the rest are FindFirstChild descendants); _stem is the
        # last segment (the edge-4 fallback key).
        "\t\t\tlocal _segs = string.split(_p, \".\")",
        "\t\t\tlocal _stem = _segs[#_segs]",
        "\t\t\tlocal _node = game:GetService(_segs[1])",
        "\t\t\tfor _i = 2, #_segs do",
        "\t\t\t\tif _node == nil then break end",
        "\t\t\t\t_node = _node:FindFirstChild(_segs[_i])",
        "\t\t\tend",
        "\t\t\tlocal so = nil",
        "\t\t\tif _node ~= nil then",
        "\t\t\t\tlocal _ok, _mod = pcall(require, _node)",
        "\t\t\t\tif _ok then so = _mod end",
        "\t\t\tend",
        "\t\t\tif type(so) == \"table\" then",
        f"\t\t\t\tlocal _key = {key_expr}",
        "\t\t\t\tif _key ~= nil then",  # edge 6: skip nil keys
        f"\t\t\t\t\t{_DICT_LOCAL}[_key] = so",
        "\t\t\t\tend",
        "\t\t\tend",
        "\t\tend",
        f"\t\t{_LOADED_LOCAL} = true",
        "\tend",
        "end",
        "",
        f"function {receiver}.{accessor}(type)",
        f"\tif {_DICT_LOCAL} == nil then",
        "\t\treturn nil",
        "\tend",
        f"\treturn {_DICT_LOCAL}[type]",
        "end",
    ]
    return "\n".join(lines)


def lower_so_db_consumers(
    scripts: list[_HasSourcePath],
    facts: dict[str, SoDbConsumerFact],
    load_method_by_path: dict[str, str],
    accessor_by_path: dict[str, str] | None = None,
) -> int:
    """Whole-region-replace the public methods of each SO-store DB consumer in
    ``facts`` with the canonical keyed-dict body draining the owned SO modules.

    ``load_method_by_path`` maps each fact's ``source_path`` to the C#-derived load
    method name (so the locator anchors on the right method). ``accessor_by_path``
    (optional) maps to the keyed accessor name (e.g. ``GetThemeData``); when a path
    is absent the accessor is LOCATED as the public method that is neither getter
    nor the load method.

    Returns the number of modules re-lowered. Raises ``SoDbUnresolved`` for a
    located fact whose load/getter anchors cannot be located (fail-closed).

    PURE except the documented in-place ``source`` mutation. Modules not in
    ``facts`` are untouched. Idempotent: a second run re-locates the same anchors
    and re-renders byte-identical output.
    """
    accessor_by_path = accessor_by_path or {}
    lowered = 0
    for script in scripts:
        sp = script.source_path
        if sp is None:
            continue
        fact = facts.get(sp)
        if fact is None:
            continue
        load_method = load_method_by_path.get(sp)
        if not load_method:
            raise SoDbUnresolved(sp, fact.label)
        located = _locate_region(script.source, load_method)
        if located is None:
            raise SoDbUnresolved(sp, fact.label)
        receiver, dict_getter, loaded_getter, start, end = located
        accessor = accessor_by_path.get(sp) or _locate_accessor(
            script.source, load_method, dict_getter, loaded_getter,
        )
        if accessor is None:
            raise SoDbUnresolved(sp, fact.label)
        region = _canonical_region(
            receiver, load_method, dict_getter, loaded_getter,
            accessor, fact.key_field, fact.so_module_paths,
        )
        script.source = script.source[:start] + region + script.source[end:]
        lowered += 1
    return lowered


def _locate_accessor(
    source: str, load_method: str, dict_getter: str, loaded_getter: str,
) -> str | None:
    """The keyed accessor is the public method that is neither getter nor the load
    method (e.g. ``GetThemeData``). Returns its name, or ``None`` when no such
    distinct method exists (fail-closed)."""
    for m in _RE_DB_METHOD.finditer(source):
        name = m.group(2)
        if name not in (load_method, dict_getter, loaded_getter):
            return name
    return None
