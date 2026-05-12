"""Gameplay-adapter detectors.

A :class:`Detector` classifies a Unity scene node into a
:class:`~converter.gameplay.capabilities.Behavior`. The protocol is
deliberately two-layered AND component-bound:

  - ``primary(node, component) -> bool`` is composition-only. It must
    NOT inspect the C# source. ``component`` is the specific
    MonoBehaviour-shaped :class:`ComponentData` the dispatcher is
    asking about — passing it explicitly stops cross-component
    confirm bleed (a Door MonoBehaviour on the same node as an
    unrelated MonoBehaviour can't have the unrelated component's
    body accidentally confirm-match the door detector).
  - ``confirm(node, component, source) -> bool`` is source-aware.
    Operates on the source of THIS component's class only. Can ONLY
    REJECT; it cannot promote an infeasible primary result to feasible
    (the dispatch in :func:`detect` never calls it unless
    ``primary()`` returned True).
  - ``behavior(node, component, source) -> Behavior`` is invoked once
    classification has settled and produces the per-instance IR.

Multi-detector resolution: if two detectors both pass primary AND
confirm against the same (node, component), :class:`AmbiguousDetectionError`
is raised with both candidate names. Operator override is via the
per-output ``.gameplay_deny.txt``.

PR #73a ships the :class:`DoorDetector` only. PR #73b adds
TurretBullet / PlaneBullet.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, TYPE_CHECKING, runtime_checkable

from converter.gameplay.capabilities import (
    Behavior,
    ContainerResolver,
    LifetimePersistent,
    MovementAttributeDrivenTween,
    TriggerOnBoolAttribute,
)

if TYPE_CHECKING:
    from core.unity_types import ComponentData, SceneNode


@runtime_checkable
class Detector(Protocol):
    """Three-phase classifier — see module docstring."""

    name: str

    def primary(
        self, node: "SceneNode", component: "ComponentData",
    ) -> bool: ...

    def confirm(
        self,
        node: "SceneNode",
        component: "ComponentData",
        source_csharp: str,
    ) -> bool: ...

    def behavior(
        self,
        node: "SceneNode",
        component: "ComponentData",
        source_csharp: str,
    ) -> Behavior: ...


class AmbiguousDetectionError(RuntimeError):
    """Raised when two or more detectors classify the same scene node.

    Resolution is operator-driven via the per-output deny-list. The
    caller (the transpile-phase integration) catches this, logs it to
    ``conversion_report.json``, and falls back to the AI path so the
    conversion does not hard-fail.
    """

    def __init__(
        self,
        node_file_id: str,
        component_file_id: str,
        detector_names: list[str],
    ) -> None:
        self.node_file_id = node_file_id
        self.component_file_id = component_file_id
        self.detector_names = list(detector_names)
        super().__init__(
            f"Scene node {node_file_id!r} component {component_file_id!r} "
            f"matches multiple detectors: {sorted(self.detector_names)}. "
            f"Add a deny-list entry to disambiguate."
        )


# ---------------------------------------------------------------------------
# Door detector
# ---------------------------------------------------------------------------

# Unity Y → Roblox Y stud conversion is applied at runtime (the
# Movement.AttributeDrivenTween capability reads ``target_offset_unity``
# in meters and the Lua side multiplies by ``STUDS_PER_METER``). The
# 4m vertical offset and 1s open/close durations match the canonical
# SciFi_Door anim clip — same values the legacy ``door_tween_open``
# coherence pack hard-coded. Per-instance override (reading the actual
# anim clip's ``m_PositionCurves``) is roadmapped for a follow-up;
# every SciFi_Door instance shares one shape today.
_DOOR_OPEN_OFFSET_UNITY: tuple[float, float, float] = (0.0, 4.0, 0.0)
_DOOR_OPEN_DURATION_SECONDS: float = 1.0
_DOOR_CLOSE_DURATION_SECONDS: float = 1.0
_DOOR_TRIGGER_ATTRIBUTE: str = "open"


# Substring guards used by ``confirm()``. Source-only — never used by
# ``primary()``. Names chosen for stability against the AI-output
# variations the legacy pack chased across PR #71's 12 review rounds.
_DOOR_SETBOOL_OPEN_RE = re.compile(r'SetBool\(\s*["\']open["\']\s*,')
_DOOR_ONTRIGGERENTER_RE = re.compile(r'\bOnTriggerEnter\s*\(')


def _component_script_class(component: "ComponentData") -> str:
    """Return the script class name attached to a MonoBehaviour
    component, or ``""`` if not a MonoBehaviour or unresolved.

    Composition-only: reads the synthetic ``_script_class_name`` field
    populated by :mod:`converter.gameplay.integration` against
    guid_index. Never touches C# source. The contract test feeds an
    empty source to detector calls and asserts primary results stay
    stable.
    """
    if component.component_type != "MonoBehaviour":
        return ""
    return str(component.properties.get("_script_class_name", ""))


class DoorDetector:
    """Detects Unity ``Door.cs``-style trigger doors.

    Primary signal (composition-only): the component is a MonoBehaviour
    whose script class name is ``Door``.

    Confirm signal (source-aware rejector): the C# body of THIS
    component's class contains both ``SetBool("open", ...)`` AND
    ``OnTriggerEnter``. A node whose script happens to be named Door
    but lacks the trigger-driven SetBool flow is rejected.
    """

    name: str = "door"

    def primary(
        self, node: "SceneNode", component: "ComponentData",
    ) -> bool:
        return _component_script_class(component) == "Door"

    def confirm(
        self,
        node: "SceneNode",
        component: "ComponentData",
        source_csharp: str,
    ) -> bool:
        return bool(
            _DOOR_SETBOOL_OPEN_RE.search(source_csharp)
            and _DOOR_ONTRIGGERENTER_RE.search(source_csharp)
        )

    def behavior(
        self,
        node: "SceneNode",
        component: "ComponentData",
        source_csharp: str,
    ) -> Behavior:
        # SciFi_Door's MonoBehaviour lives on the trigger volume but
        # the visible mesh is a sibling named "door" (matches the
        # legacy ``door_tween_open`` coherence pack's sibling lookup,
        # see ``Door.cs:transform.parent.Find("door")``). Encode that
        # as per-instance container resolver metadata so the family
        # runtime stays uncoupled.
        return Behavior(
            unity_file_id=node.file_id,
            diagnostic_name="Door",
            capabilities=(
                TriggerOnBoolAttribute(name=_DOOR_TRIGGER_ATTRIBUTE),
                MovementAttributeDrivenTween(
                    target_offset_unity=_DOOR_OPEN_OFFSET_UNITY,
                    open_duration=_DOOR_OPEN_DURATION_SECONDS,
                    close_duration=_DOOR_CLOSE_DURATION_SECONDS,
                ),
                LifetimePersistent(),
            ),
            container_resolver=ContainerResolver(
                kind="ascend_then_child",
                child_name="door",
            ),
        )


# ---------------------------------------------------------------------------
# Multi-detector dispatch
# ---------------------------------------------------------------------------

# PR #73a registers only the door detector; #73b adds the bullet
# detectors. New entries land here so the deny-list and dispatch share
# a single source of truth.
ALL_DETECTORS: tuple[Detector, ...] = (
    DoorDetector(),
)


def detect(
    node: "SceneNode",
    component: "ComponentData",
    source_csharp: str,
    *,
    detectors: tuple[Detector, ...] = ALL_DETECTORS,
    deny_list: frozenset[str] = frozenset(),
) -> Behavior | None:
    """Classify a single (*node*, *component*) pair against *detectors*
    and return its :class:`Behavior`, or ``None`` if no detector matches.

    Raises :class:`AmbiguousDetectionError` when more than one detector
    passes both layers against the same component. Deny-list entries
    can target either the node file_id OR the component file_id —
    matching either suppresses the detection. Per-component IDs let
    operators silence one component on a multi-component node without
    losing detection on the other components.
    """
    if node.file_id in deny_list or component.file_id in deny_list:
        return None
    candidates: list[Detector] = []
    for det in detectors:
        if not det.primary(node, component):
            continue
        if not det.confirm(node, component, source_csharp):
            continue
        candidates.append(det)
    if not candidates:
        return None
    if len(candidates) > 1:
        raise AmbiguousDetectionError(
            node.file_id, component.file_id, [d.name for d in candidates]
        )
    winner = candidates[0]
    return winner.behavior(node, component, source_csharp)


# ---------------------------------------------------------------------------
# Deny-list loader
# ---------------------------------------------------------------------------

DENY_LIST_FILENAME: str = ".gameplay_deny.txt"


def load_deny_list(output_dir: str) -> frozenset[str]:
    """Load ``<output_dir>/.gameplay_deny.txt`` — one Unity file_id per
    line. Blank lines and ``#`` comments are skipped. Returns an empty
    frozenset if the file is absent.
    """
    from pathlib import Path

    path = Path(output_dir) / DENY_LIST_FILENAME
    if not path.exists():
        return frozenset()
    entries: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.add(line)
    return frozenset(entries)
