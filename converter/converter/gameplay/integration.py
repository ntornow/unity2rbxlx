"""Pipeline-level glue: run the gameplay detectors against parsed scene
nodes, build :class:`Behavior` records, emit per-instance stub bodies,
and create first-class :class:`TranspiledScript` entries that REPLACE
the matched classes in the AI input list.

Two-stage flow:

  1. :func:`classify_scripts` walks ``parsed_scene.all_nodes``, runs
     the detectors, records per-(node, component) Behaviors, and
     returns a per-class :class:`ClassMatch` (with all node Behaviors
     for that class). The caller uses the returned ``skip_paths`` to
     remove matched classes from the AI input list BEFORE the AI runs.
  2. :func:`adapter_transpiled_scripts` turns matched classes into
     ready-to-append :class:`TranspiledScript` records. Codex round-1
     on PR #73a flagged that the prior shape ran AFTER AI and silently
     dropped matches when AI failed; the pre-AI shape closes that hole.

Equivalence check: within a single class, all per-node Behaviors must
share the same "shape" (capabilities tuple + container_resolver).
If two nodes carrying the same C# class need DIFFERENT capability
params (e.g., per-prefab open offset), the converter raises rather
than silently coalescing — per-instance emission is a follow-up
beyond PR #73a.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from converter.gameplay.capabilities import Behavior
from converter.gameplay.composer import (
    BehaviorCompositionError,
    CompositionReport,
    emit_behavior_stub,
)
from converter.gameplay.detectors import (
    ALL_DETECTORS,
    AmbiguousDetectionError,
    Detector,
)

if TYPE_CHECKING:
    from core.unity_types import (
        ComponentData,
        GuidIndex,
        ParsedScene,
        PrefabLibrary,
        SceneNode,
    )
    from converter.code_transpiler import TranspiledScript
    from unity.script_analyzer import ScriptInfo

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public match record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class NodeBinding:
    """One detector hit against one (node, component) pair."""

    unity_file_id: str          # scene node file_id
    component_file_id: str      # MonoBehaviour component file_id
    node_name: str              # SceneNode.name — operator-friendly
    detector_name: str
    behavior: Behavior


@dataclass(frozen=True)
class GameplayMatch:
    """One TranspiledScript-shaped artifact the adapter pipeline emits
    in lieu of an AI transpile. Surfaces enough metadata that
    ``conversion_report.json`` can be inspected and a deny-list line
    written without re-reading the converter.
    """

    unity_file_id: str
    detector_name: str
    diagnostic_name: str
    target_class_name: str
    capability_kinds: tuple[str, ...]
    emitted_luau: str
    node_name: str
    node_file_id: str
    component_file_id: str
    # Absolute path to the matched .cs source on disk. Named
    # ``script_path`` (not "relative") because the resolved scene/
    # script paths the converter handles are absolute throughout the
    # pipeline. Codex PR #73a-round-2 caught a "_relative_" misnomer
    # that masked the actual shape.
    script_path: str


@dataclass
class ClassMatch:
    """All bindings for one matched script class, after equivalence
    has been verified.

    ``bindings`` carries every (node, component) binding the detector
    matched; ``representative_behavior`` is the canonical Behavior
    used for emit (verified equal across every binding's shape).
    """

    class_name: str
    script_path: Path
    detector_name: str
    bindings: list[NodeBinding] = field(default_factory=list)
    representative_behavior: Behavior | None = None


# ---------------------------------------------------------------------------
# Script-class resolution
# ---------------------------------------------------------------------------

def _build_guid_to_script(
    script_infos: list["ScriptInfo"],
    guid_index: "GuidIndex",
) -> dict[str, "ScriptInfo"]:
    """Map every script GUID (from ``.cs.meta``) to its ScriptInfo.

    Scripts whose .meta files are missing or unresolved are skipped
    silently — they can't be referenced by MonoBehaviour components
    anyway.
    """
    out: dict[str, "ScriptInfo"] = {}
    for info in script_infos:
        guid = guid_index.guid_for_path(Path(info.path))
        if guid is None:
            continue
        out[guid] = info
    return out


def _resolve_mono_class(
    component_properties: dict[str, object],
    guid_to_script: dict[str, "ScriptInfo"],
) -> "ScriptInfo | None":
    """Resolve a MonoBehaviour component's ``m_Script`` GUID to its
    :class:`ScriptInfo`. Returns ``None`` if unresolved.
    """
    m_script = component_properties.get("m_Script")
    if not isinstance(m_script, dict):
        return None
    guid = m_script.get("guid")
    if not isinstance(guid, str):
        return None
    return guid_to_script.get(guid.lower())


def _enrich_components(
    node: "SceneNode",
    guid_to_script: dict[str, "ScriptInfo"],
) -> list[tuple["ComponentData", "ScriptInfo"]]:
    """For every MonoBehaviour on *node*, populate the synthetic
    ``_script_class_name`` property and return matched
    ``(component, script_info)`` pairs.

    Idempotent: a previously enriched component keeps its class_name.
    """
    pairs: list[tuple["ComponentData", "ScriptInfo"]] = []
    for comp in node.components:
        if comp.component_type != "MonoBehaviour":
            continue
        # Reuse a cached resolution if we ran enrichment before.
        cached = comp.properties.get("_script_class_name")
        info: "ScriptInfo | None" = None
        if isinstance(cached, str) and cached:
            # Look up by class name. Walk values once — script_infos
            # is small.
            for cand in guid_to_script.values():
                if cand.class_name == cached:
                    info = cand
                    break
        if info is None:
            info = _resolve_mono_class(comp.properties, guid_to_script)
            if info is not None:
                comp.properties["_script_class_name"] = info.class_name
            else:
                comp.properties.setdefault("_script_class_name", "")
        if info is None:
            continue
        pairs.append((comp, info))
    return pairs


# ---------------------------------------------------------------------------
# Equivalence
# ---------------------------------------------------------------------------

def _behavior_shape(b: Behavior) -> tuple[object, ...]:
    """Return a comparable "shape" of *b* — the parts that must match
    across every binding of the same class for per-class emission to
    be safe. Excludes :attr:`Behavior.unity_file_id` (per-instance by
    construction).
    """
    return (
        b.diagnostic_name,
        b.capabilities,         # frozen tuple of frozen dataclasses
        b.container_resolver,   # frozen dataclass
    )


class DivergentBehaviorsError(RuntimeError):
    """Raised when one C# class matches multiple scene nodes but the
    per-node Behaviors aren't equivalent. PR #73a emits one stub per
    class; divergent matches would silently coalesce. The pipeline
    routes divergent classes to AI via ``ClassificationResult.divergent``
    without re-raising — this type still inherits Exception so
    legacy callers / tests can catch it.

    ``binding_a`` and ``binding_b`` are structured :class:`NodeBinding`
    records so a downstream report can render node_name +
    component_file_id without re-parsing a message string. Codex
    PR #73a-round-3 flagged the prior free-form-string carrier as
    operator-hostile.
    """

    def __init__(
        self,
        class_name: str,
        binding_a: "NodeBinding",
        binding_b: "NodeBinding",
    ) -> None:
        self.class_name = class_name
        self.binding_a = binding_a
        self.binding_b = binding_b
        super().__init__(
            f"Class {class_name!r} matches multiple scene nodes with "
            f"divergent behaviour shapes: "
            f"{binding_a.node_name}@{binding_a.unity_file_id} (component "
            f"{binding_a.component_file_id}) vs "
            f"{binding_b.node_name}@{binding_b.unity_file_id} (component "
            f"{binding_b.component_file_id}). Per-instance emit isn't "
            f"supported in PR #73a — open a deny-list entry on one of "
            f"the bindings or wait for the per-instance variant in #73b."
        )


# ---------------------------------------------------------------------------
# Classification (pre-AI)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DivergentClassRecord:
    """One class that matched multiple bindings with non-equivalent
    Behavior shapes. Recorded per-class so the pipeline can fall back
    to AI for THIS class only — codex PR #73a-round-2 flagged that
    raising the error from :func:`classify_scripts` dropped every
    matched class in the same pass.
    """

    class_name: str
    script_path: Path
    error: "DivergentBehaviorsError"


@dataclass(frozen=True)
class ClassificationResult:
    """Output of :func:`classify_scripts`.

    ``matches`` is keyed by absolute script path (matches the
    ``ScriptInfo.path`` shape) so the caller can filter the AI input
    list by membership. ``divergent`` captures classes that matched
    bindings but had to be dropped because the per-node Behaviors
    weren't equivalent — the pipeline lets AI handle those.
    """

    matches: dict[Path, ClassMatch]
    divergent: tuple["DivergentClassRecord", ...] = ()

    @property
    def skip_paths(self) -> set[Path]:
        """Script paths to remove from the AI input list."""
        return set(self.matches.keys())


def _walk_prefab_nodes(root) -> "list":  # type: ignore[no-untyped-def]
    """Flatten a prefab's node tree into a list. Returns empty when
    *root* is None — prefabs with no resolved root (variant parse
    failures, e.g.) are silently skipped because they can't carry a
    classifiable MonoBehaviour anyway.
    """
    if root is None:
        return []
    out = [root]
    stack = list(getattr(root, "children", []) or [])
    while stack:
        node = stack.pop()
        out.append(node)
        stack.extend(list(getattr(node, "children", []) or []))
    return out


def classify_scripts(
    *,
    parsed_scene: "ParsedScene | None",
    guid_index: "GuidIndex | None",
    script_infos: list["ScriptInfo"],
    deny_list: frozenset[str] = frozenset(),
    detectors: tuple[Detector, ...] = ALL_DETECTORS,
    prefab_library: "PrefabLibrary | None" = None,
) -> ClassificationResult:
    """Walk the parsed scene AND prefab library, run each detector
    against every MonoBehaviour component, and aggregate matches by
    script class.

    Returns an empty result when ``parsed_scene`` or ``guid_index`` is
    None — the caller hasn't parsed yet, nothing to classify.

    Prefab traversal: many Unity behaviours (doors, bullets, pickups)
    live exclusively on .prefab roots and are instantiated at runtime
    by other scripts (``Turret.Instantiate(bulletPrefab)``); the scene
    file itself carries no MonoBehaviour for them. Walking
    ``prefab_library.prefabs[].root`` is how the adapter actually fires
    on runtime-spawned prefab instances. The matched .luau is attached
    to the prefab template part in ReplicatedStorage.Templates, so
    every runtime clone inherits the adapter stub.

    Per-class divergence: if two bindings of the same class produce
    non-equivalent Behaviors, that class is dropped from ``matches``
    and recorded in ``divergent`` so the pipeline can fall back to AI
    for THIS class only. Codex PR #73a-round-2 caught that raising
    here zeroed out unrelated matched classes in the same pass.
    """
    if parsed_scene is None or guid_index is None:
        return ClassificationResult(matches={})

    guid_to_script = _build_guid_to_script(script_infos, guid_index)
    # Phase 1: collect every binding without rejecting anything.
    # Equivalence check runs as Phase 2 so divergence is per-class.
    candidates: dict[Path, ClassMatch] = {}

    # Build the unified node walk: scene-placed nodes first, then
    # every prefab template's tree. Ordering matters only for
    # determinism in the report — equivalence check is symmetric.
    nodes_to_walk: list = list(parsed_scene.all_nodes.values())
    if prefab_library is not None:
        for prefab in getattr(prefab_library, "prefabs", []):
            nodes_to_walk.extend(
                _walk_prefab_nodes(getattr(prefab, "root", None))
            )

    for node in nodes_to_walk:
        for comp, info in _enrich_components(node, guid_to_script):
            try:
                source_csharp = Path(info.path).read_text(
                    encoding="utf-8", errors="replace",
                )
            except OSError as exc:
                log.warning(
                    "[gameplay-adapters] could not read %s: %s",
                    info.path, exc,
                )
                continue

            from converter.gameplay.detectors import detect
            try:
                behavior = detect(
                    node, comp, source_csharp,
                    detectors=detectors,
                    deny_list=deny_list,
                )
            except AmbiguousDetectionError as exc:
                log.warning(
                    "[gameplay-adapters] %s — falling back to AI path. "
                    "Add %r (or component %r) to .gameplay_deny.txt to "
                    "suppress one of the candidates.",
                    exc, exc.node_file_id, exc.component_file_id,
                )
                continue
            if behavior is None:
                continue

            # Identify which detector won — re-walk so the
            # AmbiguousDetectionError path doesn't have to plumb the
            # detector name through.
            detector_name = ""
            for det in detectors:
                if det.primary(node, comp) and det.confirm(
                    node, comp, source_csharp,
                ):
                    detector_name = det.name
                    break

            binding = NodeBinding(
                unity_file_id=node.file_id,
                component_file_id=comp.file_id,
                node_name=node.name,
                detector_name=detector_name,
                behavior=behavior,
            )

            key = Path(info.path).resolve()
            entry = candidates.get(key)
            if entry is None:
                entry = ClassMatch(
                    class_name=info.class_name,
                    script_path=Path(info.path),
                    detector_name=detector_name,
                    representative_behavior=behavior,
                )
                candidates[key] = entry
            entry.bindings.append(binding)

    # Phase 2: per-class equivalence. Divergent classes drop out;
    # other classes are unaffected.
    matches: dict[Path, ClassMatch] = {}
    divergent: list[DivergentClassRecord] = []
    for key, entry in candidates.items():
        assert entry.representative_behavior is not None
        rep_shape = _behavior_shape(entry.representative_behavior)
        divergent_binding: NodeBinding | None = None
        for b in entry.bindings[1:]:
            if _behavior_shape(b.behavior) != rep_shape:
                divergent_binding = b
                break
        if divergent_binding is not None:
            head = entry.bindings[0]
            err = DivergentBehaviorsError(
                entry.class_name,
                head,
                divergent_binding,
            )
            divergent.append(DivergentClassRecord(
                class_name=entry.class_name,
                script_path=entry.script_path,
                error=err,
            ))
            log.warning(
                "[gameplay-adapters] %s — falling back to AI for this "
                "class only; other matched classes are unaffected.",
                err,
            )
            continue
        matches[key] = entry

    return ClassificationResult(
        matches=matches, divergent=tuple(divergent),
    )


# ---------------------------------------------------------------------------
# Emission (post-classify, pre-write)
# ---------------------------------------------------------------------------

def adapter_transpiled_scripts(
    *,
    classification: ClassificationResult,
    transpiled_script_cls: type,
) -> tuple[list, list[GameplayMatch]]:
    """Turn matched classes into :class:`TranspiledScript`-shaped
    records ready to append onto ``TranspilationResult.scripts``.

    Returns ``(scripts, gameplay_matches)``. The ``scripts`` list has
    one entry per matched class; the ``gameplay_matches`` list carries
    one entry per node binding so the conversion report can surface
    multi-instance coverage.

    ``transpiled_script_cls`` is passed in (rather than imported at
    module level) to break the dataclass import cycle and keep
    integration tests free of code_transpiler.
    """
    scripts: list = []
    gameplay_matches: list[GameplayMatch] = []

    for path, entry in classification.matches.items():
        assert entry.representative_behavior is not None
        try:
            luau = emit_behavior_stub(entry.representative_behavior)
        except BehaviorCompositionError:
            # Detector returned a malformed Behavior — converter bug.
            # Re-raise so tests catch it instead of falling back.
            raise

        # Build the TranspiledScript artifact. The shape mirrors the
        # AI path: same path / output_filename pattern, same
        # script_type heuristic (Door.cs is a Script — server-side
        # MonoBehaviour).
        # Stem stays case-sensitive so the converter's downstream
        # name-based matching (script binding to scene parts, runtime
        # require resolution) keeps working.
        ts = transpiled_script_cls(
            source_path=str(entry.script_path),
            output_filename=entry.script_path.stem + ".luau",
            csharp_source=entry.script_path.read_text(
                encoding="utf-8", errors="replace",
            ),
            luau_source=luau,
            strategy="gameplay_adapter",
            confidence=1.0,
        )
        scripts.append(ts)

        for binding in entry.bindings:
            gameplay_matches.append(GameplayMatch(
                unity_file_id=binding.unity_file_id,
                detector_name=binding.detector_name,
                diagnostic_name=binding.behavior.diagnostic_name,
                target_class_name=entry.class_name,
                capability_kinds=tuple(
                    cap.kind for cap in binding.behavior.capabilities
                ),
                emitted_luau=luau,
                node_name=binding.node_name,
                node_file_id=binding.unity_file_id,
                component_file_id=binding.component_file_id,
                script_path=str(entry.script_path),
            ))

    return scripts, gameplay_matches


def reports_for_matches(
    matches: list[GameplayMatch],
) -> list[CompositionReport]:
    """Convert internal :class:`GameplayMatch` records to the public
    :class:`CompositionReport` shape consumed by ``conversion_report.json``.
    """
    return [
        CompositionReport(
            unity_file_id=m.unity_file_id,
            diagnostic_name=m.diagnostic_name,
            detector_name=m.detector_name,
            capabilities=m.capability_kinds,
        )
        for m in matches
    ]
