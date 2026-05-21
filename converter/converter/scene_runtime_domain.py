"""scene_runtime_domain.py -- PR3b: execution-domain classifier for the
contract pipeline.

Runs after Phase 4a's storage classifier and after the PR1 planner has
seeded ``scene_runtime.modules``. For every runtime-bearing module
this assigns one of:

  - ``"client"``  -- touches client-only API, or drives a converted
                     Canvas / UI subtree.
  - ``"server"``  -- touches server-only API, or touches neither side's
                     API and drives no UI (authoritative default per
                     the design doc; Roblox gameplay is server-auth).
  - ``"legacy"``  -- contract conflict (both-side API, intra-class
                     instance-domain disagreement without an operator
                     override, or a reachability conflict). The module
                     falls back to the legacy bootstrap path; the host
                     runtime never instantiates it.

The classification table is **generic-only** -- a new table defined in
this module. The legacy ``storage_classifier._CLIENT_ONLY_PATTERNS`` /
``script_coherence._CLIENT_ONLY_PATTERNS`` tables are intentionally
left byte-frozen; this table covers signals the converter emits today
that the legacy tables miss (``RenderStepped``, ``:FireServer(``,
``.OnClientEvent``, ``game.Workspace.CurrentCamera`` variants,
``StarterGui`` variants).

The classifier also folds in the PR1 planner's per-instance UI
reference signal (each reference row carries ``target_is_ui: bool``
populated when the ref resolves into a converted Canvas / UI subtree).
The class verdict aggregates over instances -- any UI-bearing ref in
any instance contributes the signal.

Intra-class instance-domain conflict: when instances of the same
class produce conflicting per-instance evidence (some UI-bearing,
some not + non-UI subtree) **and** the API surface scan doesn't pin
the class, the class is multi-context. Without
``scene_runtime.domain_overrides`` the class fails closed to
``"legacy"``. With an override the chosen side wins and the displaced
instances surface in ``scene_runtime.displaced_instances``.

See ``converter/docs/design/scene-runtime-contract.md`` Piece 4 and
the PR3b row of the PR table for the full contract.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, TypedDict, cast

from core.roblox_types import RbxScript

from converter.scene_runtime_planner import (
    SceneRuntimeArtifact,
    SceneRuntimeDisplacedInstance,
    SceneRuntimeDomainSignals,
    SceneRuntimeInstance,
    SceneRuntimeModule,
    SceneRuntimePrefab,
    SceneRuntimeReference,
    SceneRuntimeScene,
)
from converter.storage_classifier import (
    REPLICATED_FIRST,
    REPLICATED_STORAGE,
    SERVER_SCRIPT_SERVICE,
    SERVER_STORAGE,
    STARTER_CHARACTER_SCRIPTS,
    STARTER_PLAYER_SCRIPTS,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Generic-only API pattern tables -- distinct from the legacy
# ``storage_classifier._CLIENT_ONLY_PATTERNS`` and
# ``script_coherence._CLIENT_ONLY_PATTERNS`` (both of which stay byte-
# frozen per the design doc). These tables include signals the converter
# actually emits today that the legacy tables miss; they're scoped to
# the PR3b domain classifier only.
# ---------------------------------------------------------------------------

_GENERIC_CLIENT_API_PATTERNS: tuple[str, ...] = (
    # Local-player handles
    r"Players\.LocalPlayer\b",
    r'GetService\(\s*["\']Players["\']\s*\)\.LocalPlayer\b',
    r"\bLocalPlayer\.Character\b",
    r"\.PlayerGui\b",
    # Input
    r'GetService\(\s*["\']UserInputService["\']\s*\)',
    r"\bUserInputService\b",
    r'GetService\(\s*["\']ContextActionService["\']\s*\)',
    r'GetService\(\s*["\']GuiService["\']\s*\)',
    # Camera + render loop (client-only RunService signals)
    r"workspace\.CurrentCamera\b",
    r"game\.Workspace\.CurrentCamera\b",
    r"\bRenderStepped\b",
    r"\bBindToRenderStep\b",
    r"\bIsClient\(\)",
    # UI roots
    r'GetService\(\s*["\']StarterGui["\']\s*\)',
    r"\bStarterGui\b",
    # Network: client outbound + client inbound
    r":FireServer\(",
    r":InvokeServer\(",
    r"\.OnClientEvent\b",
    r"\.OnClientInvoke\b",
    # Mouse handles
    r"\bmouse\.Hit\b",
    r"\bmouse\.Target\b",
)

_GENERIC_SERVER_API_PATTERNS: tuple[str, ...] = (
    # Network: server-side dispatch
    r"\.OnServerEvent\b",
    r"\.OnServerInvoke\b",
    r":FireClient\(",
    r":FireAllClients\(",
    r":InvokeClient\(",
    # Server-only services
    r'GetService\(\s*["\']DataStoreService["\']\s*\)',
    r'GetService\(\s*["\']MessagingService["\']\s*\)',
    r'GetService\(\s*["\']ServerStorage["\']\s*\)',
    r'GetService\(\s*["\']ServerScriptService["\']\s*\)',
    r"\bIsServer\(\)",
)

# Compiled once. Module-level so verifier inspections of the table
# don't pay re-compile cost.
_CLIENT_RX = tuple(re.compile(p) for p in _GENERIC_CLIENT_API_PATTERNS)
_SERVER_RX = tuple(re.compile(p) for p in _GENERIC_SERVER_API_PATTERNS)


# ---------------------------------------------------------------------------
# Report payload returned to callers (the pipeline stamps it onto
# ``ctx.scene_runtime``; tests assert against it directly).
# ---------------------------------------------------------------------------

class DomainClassifierReport(TypedDict):
    """Side-channel output of ``classify_scene_runtime_domains``.

    Fields are merged onto ``scene_runtime`` in ``_classify_storage``
    so downstream consumers (PR4 host runtime, operator inspection)
    read everything from one place.
    """

    displaced_instances: list[SceneRuntimeDisplacedInstance]
    low_confidence_modules: list[str]
    fail_closed_modules: list[str]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def classify_scene_runtime_domains(
    scene_runtime: SceneRuntimeArtifact,
    scripts: Iterable[RbxScript],
    *,
    dependency_map: dict[str, list[str]] | None = None,
) -> DomainClassifierReport:
    """Populate ``domain`` / ``container`` / ``module_path`` /
    ``domain_signals`` on every runtime-bearing module in
    ``scene_runtime.modules`` (mutated in place).

    ``scripts`` must already carry their final ``parent_path`` (set by
    ``storage_classifier.classify_storage``). ``dependency_map`` is the
    same ``class_name -> [required_class_names]`` mapping
    ``classify_storage`` uses; PR3b reuses it for the client-reachability
    rule.

    The returned report enumerates:
      - ``displaced_instances`` -- instances disagreeing with their
        class's final domain (operator-pinned conflicts).
      - ``low_confidence_modules`` -- script_ids the classifier defaulted
        to ``"server"`` because neither side's API + no UI signal
        matched. Operator may want to pin these via
        ``domain_overrides``.
      - ``fail_closed_modules`` -- script_ids the classifier kicked back
        to ``"legacy"`` (both-side API, intra-class conflict, or
        reachability conflict).

    Pure function over its inputs except for the in-place mutation of
    ``scene_runtime.modules[*]``.
    """
    modules = scene_runtime.get("modules", {})
    scenes = scene_runtime.get("scenes", {})
    prefabs = scene_runtime.get("prefabs", {})
    overrides = scene_runtime.get("domain_overrides", {})

    scripts_by_class: dict[str, RbxScript] = {}
    for script in scripts:
        if script.name:
            scripts_by_class.setdefault(script.name, script)

    per_instance_evidence = _gather_per_instance_evidence(scenes, prefabs)

    displaced: list[SceneRuntimeDisplacedInstance] = []
    low_confidence: list[str] = []
    fail_closed: list[str] = []

    # First pass: API surface + UI aggregation + intra-class conflict +
    # override application. Reachability runs in pass 2 because it
    # needs every module's first-pass domain to be set.
    for script_id, module in modules.items():
        if not module.get("runtime_bearing"):
            continue
        verdict, signals, instance_rows = _classify_module(
            script_id, module, scripts_by_class,
            per_instance_evidence.get(script_id, []),
            overrides.get(script_id),
        )
        module["domain"] = verdict
        module["domain_signals"] = signals
        _stamp_container_and_path(module, scripts_by_class)
        if signals.get("low_confidence"):
            low_confidence.append(script_id)
        if verdict == "legacy":
            fail_closed.append(script_id)
        if signals.get("override_applied") and signals.get("intra_class_conflict"):
            for row in instance_rows:
                displaced.append(row)

    # Second pass: client-domain require-graph reachability.
    # Honors first-pass verdicts.
    if dependency_map:
        _apply_reachability_rule(
            modules, dependency_map, scripts_by_class, fail_closed,
        )

    return DomainClassifierReport(
        displaced_instances=displaced,
        low_confidence_modules=low_confidence,
        fail_closed_modules=fail_closed,
    )


# ---------------------------------------------------------------------------
# Per-module classification
# ---------------------------------------------------------------------------

def _classify_module(
    script_id: str,
    module: SceneRuntimeModule,
    scripts_by_class: dict[str, RbxScript],
    instance_evidence: list["_InstanceEvidence"],
    override: str | None,
) -> tuple[str, SceneRuntimeDomainSignals, list[SceneRuntimeDisplacedInstance]]:
    """Return ``(domain, signals, displaced_instances)`` for one module.

    Inputs:
      - ``script_id`` / ``module`` -- the row being classified.
      - ``scripts_by_class`` -- transpiled RbxScripts keyed by class name.
      - ``instance_evidence`` -- per-instance evidence collected by
        ``_gather_per_instance_evidence``.
      - ``override`` -- optional operator-set domain from
        ``scene_runtime.domain_overrides``.
    """
    class_name = module.get("class_name", "")
    script = scripts_by_class.get(class_name)
    source = script.source if script and script.source else ""

    api = _classify_api_surface(source)
    instance_ui = [ev for ev in instance_evidence if ev.has_ui_ref]
    instance_nonui = [ev for ev in instance_evidence if not ev.has_ui_ref]
    any_ui = bool(instance_ui)

    signals: SceneRuntimeDomainSignals = {
        "api_surface": api,
        "ui_signal": any_ui,
    }

    # Hard contract violation: both-side API → fail closed regardless
    # of override (operator can't override a code-disagrees-with-itself
    # case; the only fix is to split the class).
    if api == "both":
        signals["fail_closed_reason"] = "both_side_api"
        return "legacy", signals, []

    # Intra-class conflict: instances disagree about UI evidence AND
    # API surface didn't pin the class to one side.
    intra_conflict = (
        api == "neither"
        and len(instance_evidence) > 1
        and len(instance_ui) > 0
        and len(instance_nonui) > 0
    )
    if intra_conflict:
        signals["intra_class_conflict"] = True
        if not override:
            signals["fail_closed_reason"] = "intra_class_conflict"
            return "legacy", signals, []
        # Override resolves the conflict; emit displaced report for
        # whichever instances disagree with the chosen side.
        signals["override_applied"] = True
        displaced = _build_displaced_rows(
            script_id, override, instance_ui, instance_nonui,
        )
        return override, signals, displaced

    # Operator override always wins outside of contract-violation cases.
    if override:
        signals["override_applied"] = True
        return override, signals, []

    # API surface verdict.
    if api == "client":
        return "client", signals, []
    if api == "server":
        return "server", signals, []

    # API is "neither"; consult UI signal.
    if any_ui:
        return "client", signals, []

    # Neither signal — server-authoritative default, flagged low-conf.
    signals["low_confidence"] = True
    return "server", signals, []


def _classify_api_surface(source: str) -> str:
    """Return ``"client"`` / ``"server"`` / ``"both"`` / ``"neither"``
    for a single script's body. Uses PR3b's generic-only pattern tables.
    """
    if not source:
        return "neither"
    has_client = any(rx.search(source) for rx in _CLIENT_RX)
    has_server = any(rx.search(source) for rx in _SERVER_RX)
    if has_client and has_server:
        return "both"
    if has_client:
        return "client"
    if has_server:
        return "server"
    return "neither"


# ---------------------------------------------------------------------------
# Per-instance evidence -- walk the planner's scenes/prefabs blocks
# ---------------------------------------------------------------------------

class _InstanceEvidence:
    """One instance's contribution to the class's evidence pool.

    Slots-only for ergonomics in tight loops; the persisted artifact
    uses the ``SceneRuntimeDisplacedInstance`` TypedDict.
    """

    __slots__ = (
        "scene", "instance_id", "game_object_id", "script_id", "has_ui_ref",
    )

    def __init__(
        self,
        scene: str,
        instance_id: str,
        game_object_id: str,
        script_id: str,
        has_ui_ref: bool,
    ) -> None:
        self.scene = scene
        self.instance_id = instance_id
        self.game_object_id = game_object_id
        self.script_id = script_id
        self.has_ui_ref = has_ui_ref


def _gather_per_instance_evidence(
    scenes: dict[str, SceneRuntimeScene],
    prefabs: dict[str, SceneRuntimePrefab],
) -> dict[str, list[_InstanceEvidence]]:
    """Walk every instance in every scene + prefab; group by ``script_id``.

    For each instance we mark ``has_ui_ref=True`` iff at least one of its
    outgoing references carries ``target_is_ui=True`` (the PR1 planner
    stamps this when a ref resolves into a converted Canvas / UI subtree).
    """
    out: dict[str, list[_InstanceEvidence]] = {}

    def _scan(
        scene_key: str,
        instances: list[SceneRuntimeInstance],
        references: list[SceneRuntimeReference],
    ) -> None:
        ui_by_instance: dict[str, bool] = {}
        for ref in references:
            if ref.get("target_is_ui"):
                ui_by_instance[ref["from"]] = True
        for inst in instances:
            evidence = _InstanceEvidence(
                scene=scene_key,
                instance_id=inst["instance_id"],
                game_object_id=inst["game_object_id"],
                script_id=inst["script_id"],
                has_ui_ref=ui_by_instance.get(inst["instance_id"], False),
            )
            out.setdefault(inst["script_id"], []).append(evidence)

    for key, scene in scenes.items():
        _scan(key, scene.get("instances", []), scene.get("references", []))
    for key, prefab in prefabs.items():
        _scan(key, prefab.get("instances", []), prefab.get("references", []))
    return out


def _build_displaced_rows(
    script_id: str,
    effective_domain: str,
    ui_evidence: list[_InstanceEvidence],
    nonui_evidence: list[_InstanceEvidence],
) -> list[SceneRuntimeDisplacedInstance]:
    """Compose the report rows for instances that disagree with the
    operator-pinned ``effective_domain``.

    "Disagrees" means: the instance's local evidence pointed at the
    other side. UI-bearing instances expect ``"client"``; non-UI
    instances (under neither-signal API) expect ``"server"``.
    """
    rows: list[SceneRuntimeDisplacedInstance] = []
    for ev in ui_evidence:
        if effective_domain != "client":
            rows.append({
                "scene": ev.scene,
                "instance_id": ev.instance_id,
                "game_object_id": ev.game_object_id,
                "script_id": script_id,
                "effective_domain": effective_domain,
                "inferred_domain": "client",
            })
    for ev in nonui_evidence:
        if effective_domain != "server":
            rows.append({
                "scene": ev.scene,
                "instance_id": ev.instance_id,
                "game_object_id": ev.game_object_id,
                "script_id": script_id,
                "effective_domain": effective_domain,
                "inferred_domain": "server",
            })
    return rows


# ---------------------------------------------------------------------------
# container / module_path stamping
# ---------------------------------------------------------------------------

def _stamp_container_and_path(
    module: SceneRuntimeModule, scripts_by_class: dict[str, RbxScript],
) -> None:
    """Copy storage_classifier's parent_path onto the module row, plus
    a relative module_path the host runtime can require.

    The storage classifier has already routed scripts to their concrete
    containers. PR3b doesn't second-guess that decision; it just makes
    the choice visible in the artifact alongside ``domain``.
    """
    script = scripts_by_class.get(module.get("class_name", ""))
    if script is None:
        return
    container = script.parent_path or ""
    if container:
        module["container"] = container
    # Module path: scripts always land under ``scripts/`` per the
    # pipeline's emit phase. The stem is the canonical script id key,
    # but the *file* name follows the RbxScript.name (class name).
    if script.name:
        module["module_path"] = f"scripts/{script.name}.luau"


# ---------------------------------------------------------------------------
# Reachability rule (client require graph must not reach ServerStorage)
# ---------------------------------------------------------------------------

def _apply_reachability_rule(
    modules: dict[str, SceneRuntimeModule],
    dependency_map: dict[str, list[str]],
    scripts_by_class: dict[str, RbxScript],
    fail_closed: list[str],
) -> None:
    """For every client-domain module, walk its transitive require
    graph. Helpers required by client modules are forced to
    ``ReplicatedStorage``; a conflict (same helper required by both
    sides and the classifier wants ``ServerStorage``) fails the helper
    closed to legacy.

    Mutates ``modules`` in place; mutates the underlying RbxScripts'
    ``parent_path`` for routed helpers.
    """
    client_classes: set[str] = set()
    server_classes: set[str] = set()
    class_to_script_id: dict[str, str] = {}
    for script_id, module in modules.items():
        class_name = module.get("class_name", "")
        if not class_name:
            continue
        class_to_script_id.setdefault(class_name, script_id)
        verdict = module.get("domain")
        if verdict == "client":
            client_classes.add(class_name)
        elif verdict == "server":
            server_classes.add(class_name)

    # BFS each side's reachable helpers. A helper is "reached by X side"
    # when at least one class on X side != helper itself transitively
    # requires it. Excluding the helper from its own seed set captures
    # the "required by *another* server class" criterion that resolves
    # the test case where a server-classified helper is also required
    # by a client (both sides want it -> fail closed).

    for helper_class, script in scripts_by_class.items():
        client_seeds = client_classes - {helper_class}
        server_seeds = server_classes - {helper_class}
        helper_reached_by_client = (
            helper_class in _closure(client_seeds, dependency_map)
        )
        helper_reached_by_server = (
            helper_class in _closure(server_seeds, dependency_map)
        )
        if not helper_reached_by_client:
            continue
        # Helper module reached by client side.
        current_container = script.parent_path or ""
        if current_container == SERVER_STORAGE:
            if helper_reached_by_server:
                # Conflict: both sides want this helper but storage_classifier
                # placed it in ServerStorage. Fail closed.
                module_id = class_to_script_id.get(helper_class)
                if module_id and module_id in modules:
                    module_row = modules[module_id]
                    module_row["domain"] = "legacy"
                    signals = cast(
                        SceneRuntimeDomainSignals,
                        module_row.get("domain_signals", {}),
                    )
                    signals["fail_closed_reason"] = "reachability_conflict"
                    module_row["domain_signals"] = signals
                    if module_id not in fail_closed:
                        fail_closed.append(module_id)
                continue
            # Client-only-reach: hoist to ReplicatedStorage.
            script.parent_path = REPLICATED_STORAGE
            module_id = class_to_script_id.get(helper_class)
            if module_id and module_id in modules:
                module_row = modules[module_id]
                module_row["container"] = REPLICATED_STORAGE
                signals = cast(
                    SceneRuntimeDomainSignals,
                    module_row.get("domain_signals", {}),
                )
                signals["reachability_forced_container"] = REPLICATED_STORAGE
                module_row["domain_signals"] = signals


def _closure(
    seeds: set[str], dependency_map: dict[str, list[str]],
) -> set[str]:
    """Transitive closure of ``seeds`` under ``dependency_map``."""
    visited: set[str] = set()
    stack: list[str] = list(seeds)
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        for dep in dependency_map.get(cur, ()):
            if dep not in visited:
                stack.append(dep)
    return visited


# ---------------------------------------------------------------------------
# Containers exported for callers that want to inspect classifier policy
# without re-importing storage_classifier (the legacy table source).
# ---------------------------------------------------------------------------

__all__ = (
    "classify_scene_runtime_domains",
    "DomainClassifierReport",
    "_GENERIC_CLIENT_API_PATTERNS",
    "_GENERIC_SERVER_API_PATTERNS",
)
