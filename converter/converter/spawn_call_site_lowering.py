"""Post-transpile lowering: rewrite dynamic Addressables SPAWN call sites.

The AI transpiler leaves every ``Addressables.InstantiateAsync(...)`` /
``LoadAssetAsync<GameObject>(...)`` / ``AssetReference.InstantiateAsync(...)`` /
direct ``Instantiate(...)``+``:Clone()`` spawn call UNCONVERTED — it emits a dead
sentinel (``local v = nil`` / a ``:Clone()`` on a prefab-id string) preceded by a
DETERMINISTIC origin comment naming the original C# call. Nothing instantiates, so
gameplay content (track segments, obstacles, the premium collectible, parallax
clouds) never spawns. This module rewrites each such site to instantiate by
prefab-id via the host API.

WHY a deterministic span-rewrite (not regex-on-AI-semantics): the TRIGGER is the
DETERMINISTIC UPSTREAM fact — the transpiler's origin COMMENT marking the call site
(``code_transpiler`` emits one per unconverted Instantiate/LoadAsset call) — NOT a
single downstream guard shape. Empirically, the downstream sentinel DIVERGES across
sites (segment ``local v=nil``+warn-abort; obstacle inverted ``if obj~=nil``;
premium bare ``toUse=nil``+warn-abort; cloud ``:Clone()`` on a string), so keying on
one guard shape silently abstains on the others (D-P4-6). Anchoring on the comment +
requiring an EXACT, ADJACENT sentinel shape gates identity on a compiler-emitted
marker and only fires when the structure also matches — the failure mode is a
fail-soft ABSTAIN, never a silent mis-rewrite.

The rewrite target is ``self.host.instantiatePrefab(<prefabIdExpr>, <parent>,
<cframe>)`` (scene_runtime.luau dotted accessor), which resolves a ``"<guid>:<path>"``
prefab-id STRING to the emitted Template clone — NOT ``PrefabSpawner.spawn``, which
keys on the bare collision-resolved Template NAME the themeData object does not carry
(D-P4-8). ``<prefabIdExpr>`` is recovered STRUCTURALLY per site from the prefab-id
string already in scope (the themeData object / the function parameter / the
``:Clone()`` receiver).

FAIL-CLOSED over guess (D-P3-2 precedent): a site is rewritten ONLY when its
prefab-id source is PROVABLY a prefab-id string at that site. The consumable site
(``Addressables.InstantiateAsync(consumable name)``) is DEFERRED — its source
``consumableDatabase.consumbales[picked]`` is accessed as a struct (``.canBeSpawned`` /
``.gameObject``) but the ``Consumables`` SO materialized it as a bare prefab-id string
list, so no struct-shaped prefab-id expr is available; force-rewriting would convert a
loud, diagnosable SO-materialization mismatch into a silent host-call mis-resolution
(D-P4-11). The consumable origin comment is detected so the deferral is COUNTED and
loud, not silent.

Idempotent: the rewrite removes the sentinel (the ``= nil`` reassign / the
``:Clone()``), so a second pass re-anchors on the (preserved) origin comment, finds no
sentinel in the adjacency window, and abstains — byte-stable.

Pure except the documented lowering side effect: it mutates only the ``source`` of the
scripts it is handed (the sibling-pass convention shared with ``lower_so_db_consumers``
/ ``lower_roster_consumers``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class _HasSource(Protocol):
    source: str


@dataclass(frozen=True)
class SpawnRewriteResult:
    """Outcome of one ``lower_spawn_call_sites`` pass over one module."""

    rewritten: int          # sites rewritten to instantiatePrefab
    deferred: int           # sites detected but fail-closed (consumable shape)


# A located rewrite: the half-open char span ``[start, end)`` to replace and the
# replacement text. ``None`` from a per-shape locator means "abstain".
@dataclass(frozen=True)
class _Span:
    start: int
    end: int
    replacement: str


# Each origin comment marker is the GENERIC Unity-API name the AI transpiler is
# prompted to name on the ``-- ...`` origin line above an unconverted spawn call
# (code_transpiler instructs ``-- UNCONVERTED: <Unity API> ...``). We anchor on
# the Unity-API substring ONLY — never the game's argument/method names that
# follow it (``premiumCollectible``/``consumable``/``cloud``/``SpawnFromAssetReference``
# are Trash-Dash identifiers; keying on them would abstain on every other game).
# Premium vs consumable share the SAME ``Addressables.InstantiateAsync`` marker
# and the SAME downstream shape; they are disambiguated STRUCTURALLY (positive
# prefab-id evidence), not by the C# arg name — see ``_locate_instantiate_async``.
_C_SEGMENT = "AssetReference.InstantiateAsync"
_C_OBSTACLE = "Addressables.LoadAssetAsync<GameObject>"
_C_INSTANTIATE_ASYNC = "Addressables.InstantiateAsync"  # premium AND consumable
_C_DIRECT_INSTANTIATE = "Instantiate("  # direct GameObject Instantiate -> :Clone() (cloud)


# --- Per-shape span matchers (anchored, ADJACENT, capture the prefab-id expr) ---

# SEGMENT: ``local <v> = nil`` + ``if <v> == nil then warn("Unable to load segment")
# return end``. The prefab-id list + index are read just above.
_RE_SEGMENT = re.compile(
    r"(?P<ind>[ \t]*)local (?P<var>\w+) = nil\n"
    r"[ \t]*if (?P=var) == nil then\n"
    r"[ \t]*warn\(string\.format\(\"Unable to load segment[^\n]*\n"
    r"[ \t]*return\n"
    r"[ \t]*end"
)
_RE_SEGMENT_ZONE = re.compile(r"local (\w+) = [^\n]*\.zones\[[^\n]*\]\n")
_RE_SEGMENT_INDEX = re.compile(r"local (\w+) = math\.random\(0, \w+ - 1\)")

# OBSTACLE (inverted): ``local <v> = nil`` + ``if <v> ~= nil then``. The prefab-id
# is the enclosing function's FIRST parameter (the AssetReference the call loaded);
# recovered STRUCTURALLY from the function signature / the ``local _ = <param>``
# discard line the transpiler emits, NOT a game-specific method name.
_RE_OBSTACLE = re.compile(
    r"(?P<ind>[ \t]*)local (?P<var>\w+) = nil\n"
    r"(?P<rest>[ \t]*if (?P=var) ~= nil then\n)"
)
# Generic first-parameter recovery: the function whose body holds the obstacle
# sentinel. We anchor on the ``local _ = <param>`` discard the transpiler emits for
# the unused asset-reference param, falling back to the nearest preceding function
# signature's first parameter. Neither names a game-specific method.
_RE_PARAM_DISCARD = re.compile(r"^[ \t]*local _ = (\w+)\s*$", re.MULTILINE)
_RE_FUNC_FIRST_PARAM = re.compile(r"^[ \t]*function [\w.:]+\((\w+)", re.MULTILINE)

# INSTANTIATE-ASYNC (premium + consumable share this): bare ``<v> = nil`` reassign +
# ``if <v> == nil then warn(...) return end``. Both have the SAME origin marker and
# shape; they are disambiguated by the prefab-id expr the warn/tostring path exposes
# (positive evidence — see ``_extract_prefab_id_expr``), NOT by the C# arg name.
_RE_INSTANTIATE_ASYNC = re.compile(
    r"(?P<ind>[ \t]*)(?P<var>\w+) = nil\n"
    r"[ \t]*if (?P=var) == nil then\n"
    r"[ \t]*warn\(string\.format\((?P<warn>\"Unable to load[^\n]*\n"
    r"[ \t]*tostring\((?P<expr>[^\n]*?)\)\)\))\n"
    r"[ \t]*return\n"
    r"[ \t]*end"
)

# CLOUD (direct ``Instantiate`` -> ``:Clone()``): ``local <v> = <expr>:Clone()``
# (expr is a prefab-id string; ``:Clone()`` on a string errors — D-P4-7), then
# ``<v>.Parent = <parent>``. The clone shape ALONE is generic to any pooler, so the
# rewrite is gated on a ``-- ... Instantiate( ...`` origin comment in the contiguous
# comment block immediately above the clone (``_instantiate_comment_above``).
_RE_CLOUD = re.compile(
    r"(?P<ind>[ \t]*)local (?P<var>\w+) = (?P<expr>[\w.]+):Clone\(\)\n"
    r"(?P<parentind>[ \t]*)(?P=var)\.Parent = (?P<parent>[^\n]+)\n"
)
# Adjacency window: how many contiguous comment/blank lines above the clone shape we
# scan for the ``Instantiate(`` origin marker before giving up (codex tweak: only a
# comment block ATTACHED to the clone counts, so an unrelated earlier comment cannot
# leak down).
_CLOUD_COMMENT_LOOKBACK = 3


def _comment_present(source: str, marker: str) -> bool:
    """True iff ``marker`` appears on an origin-comment line (``-- ...``)."""
    for line in source.splitlines():
        s = line.lstrip()
        if s.startswith("--") and marker in s:
            return True
    return False


def _locate_segment(source: str) -> _Span | None:
    if not _comment_present(source, _C_SEGMENT):
        return None
    m = _RE_SEGMENT.search(source)
    if m is None:
        return None
    pre = source[: m.start()]
    zone_m = list(_RE_SEGMENT_ZONE.finditer(pre))
    idx_m = list(_RE_SEGMENT_INDEX.finditer(pre))
    if not zone_m or not idx_m:
        logger.warning(
            "[spawn-lowering] segment origin present but zone/index locals "
            "not located; fail-closed (no rewrite)."
        )
        return None
    zone = zone_m[-1].group(1)
    idx = idx_m[-1].group(1)
    ind = m.group("ind")
    var = m.group("var")
    # Unity Random.Range(0,len) is 0-based -> +1 for the 1-based Luau list.
    expr = f"{zone}.prefabList[{idx} + 1]"
    replacement = (
        f"{ind}local {var} = self.host.instantiatePrefab("
        f"{expr}, self.gameObject, nil)\n"
        f"{ind}if {var} == nil then\n"
        f"{ind}    warn(string.format(\"Unable to load segment %d.\", {idx}))\n"
        f"{ind}    return\n"
        f"{ind}end"
    )
    return _Span(m.start(), m.end(), replacement)


def _recover_obstacle_ref(source: str, before: int) -> str | None:
    """Recover the obstacle prefab-id expr (the loaded AssetReference) STRUCTURALLY.

    The unconverted ``Addressables.LoadAssetAsync<GameObject>(<ref>)`` is the first
    parameter of the enclosing function; the transpiler emits ``local _ = <ref>`` to
    keep it referenced. Prefer that discard line (closest to the sentinel); else fall
    back to the nearest preceding function signature's first parameter. Returns the
    expr or ``None`` (fail-soft). Never keys on a game-specific method name.
    """
    pre = source[:before]
    discard = list(_RE_PARAM_DISCARD.finditer(pre))
    if discard:
        return discard[-1].group(1)
    # Fallback: the enclosing function's first parameter. In a dot-form method the
    # first listed param can be the explicit ``self``/context (Lua method ``:``
    # syntax hides ``self``, but ``function T.m(self, ref)`` lists it), which is NOT
    # the asset-reference; fail-closed rather than bind the wrong symbol.
    sig = list(_RE_FUNC_FIRST_PARAM.finditer(pre))
    if sig and sig[-1].group(1) != "self":
        return sig[-1].group(1)
    return None


def _locate_obstacle(source: str) -> _Span | None:
    if not _comment_present(source, _C_OBSTACLE):
        return None
    m = _RE_OBSTACLE.search(source)
    if m is None:
        return None
    ref = _recover_obstacle_ref(source, m.start())
    if ref is None:
        logger.warning(
            "[spawn-lowering] obstacle origin present but the asset-reference "
            "parameter could not be recovered; fail-closed (no rewrite)."
        )
        return None
    ind = m.group("ind")
    var = m.group("var")
    rest = m.group("rest")  # ``if <var> ~= nil then`` line — kept (body uses var)
    replacement = (
        f"{ind}local {var} = self.host.instantiatePrefab("
        f"{ref}, segment.gameObject, nil)\n"
        f"{rest}"
    )
    return _Span(m.start(), m.end(), replacement)


# A struct-flattened access (e.g. ``…[i].gameObject.Name``) names a GameObject
# sub-field, NOT a prefab-id holder: there is no stable prefab-id expr to pass to
# instantiatePrefab, so the site is DEFERRED (D-P4-11). A direct ``<EXPR>.name`` on a
# prefab field IS a prefab-id holder and is rewritten. Generic, structural — keys on
# the access SHAPE, not the C# arg name.
_RE_STRUCT_GAMEOBJECT = re.compile(r"\.gameObject\b")


def _extract_prefab_id_expr(tostring_inner: str) -> str | None:
    """Positive-evidence prefab-id extraction from the warn ``tostring(<X>)`` body.

    Returns the prefab-id expr iff ``<X>`` is ``<EXPR>.name``/``<EXPR>.Name`` where
    ``<EXPR>`` is a direct prefab-id holder (a field/index access NOT routed through a
    ``.gameObject`` struct sub-field). Otherwise returns ``None`` → DEFER. Never
    classifies "consumable-like"; it only rewrites on positive prefab-id evidence.
    """
    inner = tostring_inner.strip()
    m = re.fullmatch(r"(?P<expr>.+?)\.[Nn]ame", inner)
    if m is None:
        return None
    expr = m.group("expr").strip()
    if _RE_STRUCT_GAMEOBJECT.search(expr):
        return None  # struct-flattened (…gameObject…) — no prefab-id, defer
    return expr


def _instantiate_async_outcomes(source: str) -> tuple[list[_Span], int]:
    """Classify EVERY ``Addressables.InstantiateAsync`` spawn site in ``source``.

    Premium and consumable share the marker AND the ``<v> = nil``+warn-abort shape and
    BOTH appear in the same module (consumable first in source order), so we scan ALL
    matches — not just the first — and split them by positive prefab-id evidence:
    a site whose warn path exposes a stable prefab-id expr is REWRITTEN (premium); one
    that cannot (struct-flattened ``…gameObject.Name`` — consumable) is DEFERRED.

    Returns ``(rewrite_spans, deferred_count)``. Empty/zero if the origin marker is
    absent (the shape alone, without the marker, is not our site).
    """
    if not _comment_present(source, _C_INSTANTIATE_ASYNC):
        return [], 0
    spans: list[_Span] = []
    deferred = 0
    matches = list(_RE_INSTANTIATE_ASYNC.finditer(source))
    if not matches:
        logger.warning(
            "[spawn-lowering] Addressables.InstantiateAsync origin present but no "
            "<v>=nil+warn-abort spawn shape located; fail-closed (no rewrite)."
        )
        return [], 0
    for m in matches:
        expr = _extract_prefab_id_expr(m.group("expr"))
        if expr is None:
            deferred += 1  # no prefab-id expr → DEFER (consumable), counted/loud
            continue
        ind = m.group("ind")
        var = m.group("var")
        warn = m.group("warn")
        # ``warn`` already captures the three closing parens that close
        # ``tostring(``, ``string.format(`` AND ``warn(`` in the original — so the
        # replacement re-emits ONLY the ``warn(string.format(`` prefix and must NOT
        # add a trailing paren (doing so over-closes -> a Luau syntax error that
        # fails the whole module to compile).
        replacement = (
            f"{ind}{var} = self.host.instantiatePrefab({expr}, segment.gameObject, nil)\n"
            f"{ind}if {var} == nil then\n"
            f"{ind}    warn(string.format({warn}\n"
            f"{ind}    return\n"
            f"{ind}end"
        )
        spans.append(_Span(m.start(), m.end(), replacement))
    return spans, deferred


def _instantiate_comment_above(source: str, clone_start: int) -> bool:
    """True iff a ``-- … Instantiate( …`` origin comment sits in the contiguous
    comment block ATTACHED immediately above the clone shape at ``clone_start``.

    Scans up to ``_CLOUD_COMMENT_LOOKBACK`` preceding lines; only comment (``--``) and
    blank lines may intervene. A non-comment code line ends the block (so an unrelated
    earlier comment cannot leak down to a generic pooler clone). This is the identity
    gate that distinguishes the cloud spawn site from a generic ``:Clone()`` pooler.
    """
    pre = source[:clone_start]
    lines = pre.splitlines()
    scanned = 0
    for line in reversed(lines):
        s = line.strip()
        if s == "":
            continue  # blank lines inside the attached block are allowed
        if not s.startswith("--"):
            return False  # code line — block ends, gate fails
        if _C_DIRECT_INSTANTIATE in s:
            return True
        scanned += 1
        if scanned >= _CLOUD_COMMENT_LOOKBACK:
            return False
    return False


def _has_direct_instantiate_comment(source: str) -> bool:
    """True iff any origin comment line names a direct ``Instantiate(`` call."""
    return _comment_present(source, _C_DIRECT_INSTANTIATE)


def _locate_cloud(source: str) -> _Span | None:
    if ":Clone()" not in source:
        return None
    m = _RE_CLOUD.search(source)
    if m is None:
        return None
    if not _instantiate_comment_above(source, m.start()):
        # No attached ``Instantiate(`` origin comment → a generic pooler clone; abstain
        # (do NOT corrupt it into an instantiatePrefab call — the P1-1 Pooler.luau fix).
        return None
    ind = m.group("ind")
    var = m.group("var")
    expr = m.group("expr")
    parentind = m.group("parentind")
    parent = m.group("parent")
    replacement = (
        f"{ind}local {var} = self.host.instantiatePrefab({expr}, {parent}, nil)\n"
        f"{parentind}if {var} ~= nil then\n"
        f"{parentind}    {var}.Parent = {parent}\n"
        f"{parentind}end\n"
    )
    return _Span(m.start(), m.end(), replacement)


# Single-span site locators (segment/obstacle/cloud — one site per shape per module);
# the InstantiateAsync sites (premium + consumable) are handled by
# ``_instantiate_async_outcomes`` (multi-span + deferral split).
_LOCATORS = (_locate_segment, _locate_obstacle, _locate_cloud)


def lower_spawn_call_sites(source: str) -> tuple[str, SpawnRewriteResult]:
    """Rewrite the dynamic spawn call sites in one transpiled module ``source``.

    PURE: returns ``(new_source, SpawnRewriteResult)``; does NOT mutate the input.
    Each active site (segment/obstacle/premium/cloud) is located by its
    origin-comment-anchored, adjacency-bounded shape and rewritten to
    ``self.host.instantiatePrefab(<prefabIdExpr>, …)``. The consumable site is
    DETECTED and COUNTED as deferred (fail-closed, D-P4-11) but never rewritten.

    Sites whose origin comment is present but whose adjacent shape / prefab-id
    expr cannot be located ABSTAIN (fail-soft, logged) — never a guessed rewrite.

    Idempotent: a rewritten span no longer carries its sentinel, so a re-run
    re-anchors, finds no shape, and returns byte-identical source.
    """
    # Collect non-overlapping spans, then splice right-to-left so earlier offsets
    # stay valid. The single-span locators return at most one span each; the
    # InstantiateAsync handler returns all premium rewrite spans + the consumable
    # deferral count.
    spans: list[_Span] = []
    cloud_span: _Span | None = None
    for locate in _LOCATORS:
        span = locate(source)
        if span is not None:
            spans.append(span)
            if locate is _locate_cloud:
                cloud_span = span
    # Cloud fail-soft (uniform with segment/obstacle/instantiate-async): a direct
    # ``Instantiate(`` origin comment + a live ``:Clone()`` shape, but no located cloud
    # span, is a transpiler-shape drift — abstain LOUDLY. (``:Clone()`` is absent after
    # a successful rewrite, so the idempotent re-run does not trip this.)
    if (
        cloud_span is None
        and ":Clone()" in source
        and _has_direct_instantiate_comment(source)
    ):
        logger.warning(
            "[spawn-lowering] direct Instantiate( origin present with a live "
            ":Clone() but no cloud spawn shape located; fail-closed (no rewrite)."
        )
    async_spans, deferred = _instantiate_async_outcomes(source)
    spans.extend(async_spans)
    rewritten = len(spans)
    spans.sort(key=lambda s: s.start, reverse=True)
    new_source = source
    for span in spans:
        new_source = new_source[: span.start] + span.replacement + new_source[span.end:]
    return new_source, SpawnRewriteResult(rewritten=rewritten, deferred=deferred)


def lower_spawn_call_sites_in_scripts(scripts: list[_HasSource]) -> SpawnRewriteResult:
    """Apply ``lower_spawn_call_sites`` to every script in place, returning the
    aggregate result.

    PURE except the documented in-place ``source`` mutation (the sibling-pass
    convention). A script whose source carries no spawn site is untouched.
    """
    total_rewritten = 0
    total_deferred = 0
    for script in scripts:
        src = getattr(script, "source", None)
        if not isinstance(src, str):
            continue
        new_src, result = lower_spawn_call_sites(src)
        if new_src != src:
            script.source = new_src
        total_rewritten += result.rewritten
        total_deferred += result.deferred
    return SpawnRewriteResult(rewritten=total_rewritten, deferred=total_deferred)
