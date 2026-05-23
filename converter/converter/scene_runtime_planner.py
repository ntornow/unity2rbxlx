"""scene_runtime_planner.py — Deterministic plan of every runtime-bearing
MonoBehaviour the host runtime must instantiate.

Built once per conversion before ``transpile_scripts``. Walks every scene
and every prefab template, resolves each MonoBehaviour's ``m_Script`` to a
canonical script id, and emits a per-instance / per-reference snapshot of
what the Unity runtime would do at scene load — minus the engine.

The output is the ``scene_runtime`` block persisted into
``conversion_plan.json``. PR1 commits the schema and emits the structural
fields (``stem``, ``class_name``, ``runtime_bearing``, instance / reference
rows, stable ``prefab_id``s). Execution-domain fields (``domain``,
``container``, ``module_path``) are added by PR3b — leaving them off the
PR1 entries keeps the persistence merge in ``_classify_storage`` honest:
old keys survive untouched, new keys fold in later.

See ``converter/docs/design/scene-runtime-contract.md`` for the contract
this module implements.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict, cast

from core.unity_types import (
    ComponentData,
    GuidIndex,
    ParsedScene,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
    SceneNode,
)
from unity.yaml_parser import ref_file_id, ref_guid


# ---------------------------------------------------------------------------
# Persisted schema (one ``scene_runtime`` block per conversion).
#
# Functional-form ``TypedDict`` is used for ``SceneRuntimeReference`` so the
# ``from`` key (a Python keyword) can survive a JSON round-trip with the
# spelling the contract pins. ``total=False`` on ``SceneRuntimeModule`` is
# deliberate: PR3b will add ``domain`` / ``container`` / ``module_path``
# without invalidating PR1-shaped artifacts on disk.
# ---------------------------------------------------------------------------

class SceneRuntimeModule(TypedDict, total=False):
    """One row in ``scene_runtime.modules``. Keyed by canonical script id.

    PR1 fields: ``stem``, ``class_name``, ``runtime_bearing``. PR3b /
    classifier-v2 add ``domain`` / ``container`` / ``module_path`` /
    ``domain_signals`` once the storage classifier has run.

    ``domain`` values: ``"client"`` | ``"server"`` | ``"helper"`` |
    ``"excluded"``. (``"legacy"`` is REMOVED in classifier-v2 — see the
    scene-runtime-domain-signals design doc. Existing on-disk artifacts
    carrying ``"legacy"`` are migrated to ``"excluded"`` on first read.)
    ``container`` mirrors the storage_classifier parent_path
    (``"ReplicatedStorage"`` / ``"ServerStorage"`` /
    ``"ServerScriptService"`` / ``"StarterPlayer.StarterPlayerScripts"`` /
    ``"StarterPlayer.StarterCharacterScripts"`` / ``"ReplicatedFirst"``).
    ``module_path`` is the dotted DataModel path the host runtime
    requires (``"ReplicatedStorage.Foo"``). ``domain_signals`` records
    why the classifier chose what it did so the conversion report can
    attribute decisions to the operator.
    """

    stem: str
    class_name: str
    runtime_bearing: bool
    # ``is_component_class`` is True when the script is a Unity component
    # (extends MonoBehaviour / NetworkBehaviour directly OR transitively
    # through a project-local base). It is BROADER than ``runtime_bearing``:
    # a component spawned only at runtime (Instantiate) is a component class
    # but is not instance-backed in any walked scene/prefab, so it is NOT
    # runtime_bearing. The generic transpile contract (ModuleScript target +
    # generic prompt + verifier) keys off THIS flag, not placement, because
    # in Unity every component runs host-bound (``self.gameObject``) whether
    # authored or Instantiate()-spawned. ``runtime_bearing`` stays
    # placement-based so it still drives only what the host boots at start.
    is_component_class: bool
    domain: str
    container: str
    module_path: str
    domain_signals: "SceneRuntimeDomainSignals"


class SceneRuntimeDomainSignals(TypedDict, total=False):
    """Per-module audit trail for the v2 domain classifier. All fields
    optional — only the ones that fired show up in the persisted artifact.

    ``api_surface``: ``"client"`` | ``"server"`` | ``"both"`` | ``"neither"``
        — verdict from the post-transpile Luau pattern scan (legacy
        signal channel; kept for back-compat reads).
    ``ui_signal``: True if at least one instance's references resolved
        into a converted Canvas / UI subtree (``target_is_ui`` aggregation).
    ``strong_client`` / ``strong_server`` / ``moderate_client`` /
    ``moderate_server``: signal counts (v2 classifier). Each is the
        number of distinct signal kinds that fired on this module
        (deduped — multiple `Input.Get*` matches still count as one).
    ``cs_signals``: list of signal kind names that fired from the C#
        source channel. e.g. ``["using_UnityEngine_UI", "Input_Get",
        "SerializeField_Text"]``.
    ``luau_signals``: list of signal kind names that fired from the
        post-transpile Luau channel. e.g. ``["LocalPlayer", "OnServerEvent"]``.
    ``instance_signals``: list of signal kind names that fired from
        per-instance evidence. e.g. ``["instance_owner_is_ui",
        "target_is_ui"]``.
    ``rule_applied``: which rule (1..7) determined the verdict before
        any override. Useful for operator triage.
    ``intra_class_conflict``: True if instances of this script produce
        conflicting per-instance UI evidence AND the API surface was
        ambiguous (``"neither"`` or contradicted by overrides).
    ``override_applied``: True if the final ``domain`` came from
        ``scene_runtime.domain_overrides`` rather than classifier inference.
    ``override_rejected``: True if an override was supplied but
        rejected (Rule-1 excluded → only ``"excluded"`` accepted).
    ``low_confidence``: True when the verdict is the zero-signal
        fallback path. Flagged so operators see what the classifier
        wasn't sure about.
    ``reachability_forced_container``: ``"ReplicatedStorage"`` if the
        reachability rule moved this module from ServerStorage to RS
        to satisfy a client require; absent otherwise.
    ``fail_closed_reason``: ``"both_side_api"`` (Rule-1) |
        ``"moderate_only_ambiguity"`` (Rule-4) |
        ``"intra_class_conflict"`` |
        ``"reachability_conflict"`` — present only when ``domain`` is
        ``"excluded"``.
    """

    api_surface: str
    ui_signal: bool
    strong_client: int
    strong_server: int
    moderate_client: int
    moderate_server: int
    cs_signals: list[str]
    luau_signals: list[str]
    instance_signals: list[str]
    rule_applied: int
    intra_class_conflict: bool
    override_applied: bool
    override_rejected: bool
    low_confidence: bool
    reachability_forced_container: str
    fail_closed_reason: str


class SceneRuntimeInstance(TypedDict):
    """One MonoBehaviour instance attached to a scene or prefab GameObject."""

    instance_id: str
    script_id: str
    game_object_id: str
    active: bool
    enabled: bool
    # Scalar serialized fields only — non-scalar (refs) move to ``references``.
    # Vector/Color-shaped structs are preserved as nested dicts so downstream
    # consumers can read components without re-parsing.
    config: dict[str, object]


# Optional fields appended to a SceneRuntimeInstance row. Kept separate from
# the ``total=True`` shape so PR1-shaped artifacts on disk stay valid; PR4
# reads ``parent_game_object_id`` to walk ancestors when computing
# ``activeInHierarchy`` (Unity's "AND every ancestor's activeSelf" gate).
# Scene roots get NO key here (TypedDict total=False allows missing); the
# runtime treats a missing key as "this GO has no parent in the planner
# graph" and stops the upward walk. This mirrors the
# ``SceneRuntimeReferenceExtra`` pattern.
#
# Classifier-v2 (domain signals redesign) appends
# ``instance_owner_is_ui``: ``True`` iff the instance's host GameObject
# lives under (or owns) a Canvas. The planner already computes
# ``ui_go_fids`` per scene/prefab; this surfaces the per-instance
# verdict so the domain classifier can fold "script attached to a UI
# GameObject" into the strong-client signal pool without re-walking
# the scene hierarchy. Default ``False`` when key missing.
class SceneRuntimeInstanceExtra(TypedDict, total=False):
    parent_game_object_id: str
    instance_owner_is_ui: bool


SceneRuntimeReference = TypedDict(
    "SceneRuntimeReference",
    {
        "from": str,
        "field": str,
        "index": int | None,
        # "component" | "gameobject" | "prefab" | "scriptable_object" | "asset"
        # NOTE: ``component`` is reserved for peer-MonoBehaviour references --
        # the only "components" PR2/PR4 give stable lookup surface to. Refs
        # to built-in components (Rigidbody / Button / Collider / etc.)
        # resolve to ``"gameobject"`` with the owning GO's id and an
        # accompanying ``target_component_type`` so the host can search
        # the GO for the right Roblox class. See the PR1 P1 codex finding.
        "target_kind": str,
        "target_ref": str,
        "target_is_ui": bool,
    },
    total=True,
)


# Optional fields appended to a SceneRuntimeReference row. Kept separate from
# the ``total=True`` shape so PR2/PR3a don't see schema breakage; PR4 reads
# this when ``target_kind == "gameobject"`` and the field type was a built-in
# component (Rigidbody, Button, AudioSource, etc.). Values are the Unity
# component type name as it appears in the YAML (``Rigidbody``,
# ``BoxCollider``, ``Button``, ``RectTransform``, ...).
class SceneRuntimeReferenceExtra(TypedDict, total=False):
    target_component_type: str


class SceneRuntimeScene(TypedDict):
    instances: list[SceneRuntimeInstance]
    references: list[SceneRuntimeReference]
    lifecycle_order: list[str]


class SceneRuntimePrefab(TypedDict):
    name: str
    # R2-P1.2: bare template name as emitted under
    # ``ReplicatedStorage.Templates`` by ``prefab_packages``. The stable
    # ``prefab_id`` (the key in ``scene_runtime.prefabs``) carries the
    # GUID + path; the host runtime can't feed that to
    # ``Templates:FindFirstChild(...)`` directly, so the planner persists
    # the bare name here. Always equals ``PrefabTemplate.name``.
    template_name: str
    instances: list[SceneRuntimeInstance]
    references: list[SceneRuntimeReference]
    lifecycle_order: list[str]


class SceneRuntimeScenePrefabPlacement(TypedDict):
    """One pre-placed prefab instance authored directly into a scene.

    Tier-1 boot row (Bug B). The runtime resolves the placement's live
    clone via the prefab subplan's stamped ``_SceneRuntimeId`` ids and
    runs the prefab's component lifecycle once at ``start()``.

    ``placement_id``: stable per-placement id, ``"<scene_ns>:<pi_fid>"``,
        namespaced exactly like scene ``game_object_id``s.
    ``prefab_id``: the stable prefab id (key into ``scene_runtime.prefabs``),
        computed via ``_prefab_stable_id`` so it matches the subplan key.
    ``active`` / ``enabled``: default ``True`` for tier 1 (per-placement
        ``m_Modifications`` overrides are tier 3; read straight through
        only when cleanly available).
    """

    placement_id: str
    prefab_id: str
    active: bool
    enabled: bool


# Optional fields appended to a placement row (total=False so on-disk
# artifacts without the key stay valid). ``parent_game_object_id`` is the
# scene GameObject the placement is parented under, derived by mapping
# ``PrefabInstanceData.transform_parent_file_id`` through
# ``scene.transform_fid_to_go_fid``. Omitted for root placements (no
# resolvable parent) — mirrors the ``SceneRuntimeInstanceExtra`` pattern.
class SceneRuntimeScenePrefabPlacementExtra(TypedDict, total=False):
    parent_game_object_id: str


class SceneRuntimeDisplacedInstance(TypedDict):
    """One row in ``scene_runtime.displaced_instances``: an instance whose
    domain disagrees with the class's final ``domain`` (operator pinned the
    class via ``domain_overrides`` despite intra-class conflict). PR4's
    conversion-time report enumerates these so the operator sees which
    instances won't execute their lifecycle on the chosen side.

    ``owner_kind``: ``"scene"`` or ``"prefab"`` -- which planner block
        the instance lives in.
    ``owner_ref``: the scene path (``owner_kind == "scene"``) or stable
        prefab id (``owner_kind == "prefab"``).
    ``scene``: legacy alias for ``owner_ref`` (PR3b shipped only this
        field; PR4 split it into ``owner_kind`` + ``owner_ref`` so the
        report can render the two cases distinctly without re-parsing
        the value). Kept populated for one release; readers should
        migrate to the split pair.
    ``instance_id``: the per-instance id from PR1's planner.
    ``game_object_id``: the host GameObject's stable id.
    ``script_id``: which class the instance belongs to.
    ``effective_domain``: what the class was forced to (``"client"`` or
        ``"server"``).
    ``inferred_domain``: what this individual instance's evidence
        suggested (``"client"`` / ``"server"`` / ``"neither"``).
    """

    owner_kind: str
    owner_ref: str
    scene: str
    instance_id: str
    game_object_id: str
    script_id: str
    effective_domain: str
    inferred_domain: str


class SceneRuntimeArtifact(TypedDict, total=False):
    modules: dict[str, SceneRuntimeModule]
    scenes: dict[str, SceneRuntimeScene]
    prefabs: dict[str, SceneRuntimePrefab]
    # Operator-set ``script_id → "client" | "server"`` overrides. Read by
    # PR3b's domain classifier; PR1 just preserves whatever's there.
    domain_overrides: dict[str, str]
    # PR3b populates these post-classify_storage. Optional so PR1/PR2/PR3a
    # artifacts persisted to disk don't fail validation on resume.
    displaced_instances: list[SceneRuntimeDisplacedInstance]
    # Low-confidence verdicts (``script_id``s the operator may want to
    # pin via ``domain_overrides``).
    low_confidence_modules: list[str]
    # R2-P1.3: Unity GUID -> dotted DataModel path for emitted SO
    # ModuleScripts. Populated by ``_subphase_inject_scene_runtime`` once
    # the SO converter has produced its asset list and storage_classifier
    # has chosen each module's container. The host runtime's
    # ``scriptable_object`` ref resolver looks the persisted GUID up in
    # this map and ``require``s the resulting module path.
    scriptable_objects: dict[str, str]
    # Bug B tier 1: pre-placed prefab instances authored into scenes. The
    # runtime boots these at ``start()`` (the scene loop only walks logical
    # scene GameObjects, never PrefabInstance documents — so prefab gameplay
    # scripts never ran). YAML scenes only: binary scenes don't populate
    # ``transform_fid_to_go_fid`` so they emit no placements (tracked gap).
    scene_prefab_placements: list[SceneRuntimeScenePrefabPlacement]


# ---------------------------------------------------------------------------
# Properties skipped during config / reference extraction. Mirrors the
# ``serialized_field_extractor`` list — these are engine-internal keys, never
# author-visible serialized data.
# ---------------------------------------------------------------------------

_MONO_INTERNAL_PROPS: frozenset[str] = frozenset({
    "m_ObjectHideFlags",
    "m_CorrespondingSourceObject",
    "m_PrefabInstance",
    "m_PrefabAsset",
    "m_GameObject",
    "m_Enabled",
    "m_EditorHideFlags",
    "m_Script",
    "m_Name",
    "m_EditorClassIdentifier",
})

# ScriptableObject-flavoured assets that surface from a serialized ref.
_SCRIPTABLE_OBJECT_SUFFIX: str = ".asset"


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def _scene_namespace(scene: ParsedScene, unity_project_root: Path | None) -> str:
    """Stable identifier for a scene, used as the prefix for instance ids.

    Project-relative path when computable, absolute path otherwise. Always a
    forward-slashed string so JSON round-trips match across platforms.
    """
    return _relative_path_string(scene.scene_path, unity_project_root)


def _relative_path_string(
    path: Path, unity_project_root: Path | None,
) -> str:
    """Convert ``path`` to a forward-slashed project-relative string when
    possible, falling back to a forward-slashed absolute string when the
    path lives outside the project root or no root was supplied.
    """
    try:
        if unity_project_root is not None:
            rel = path.resolve().relative_to(unity_project_root.resolve())
            return rel.as_posix()
    except ValueError:
        pass
    return path.as_posix()


def _prefab_stable_id(
    template: PrefabTemplate,
    guid_index: GuidIndex | None,
    by_guid: dict[str, PrefabTemplate],
    unity_project_root: Path | None,
) -> str:
    """Stable ``prefab_id`` = ``"<guid>:<project-relative-path>"``.

    Bare name collides across folders (the design doc's whole reason for
    this id shape); the GUID disambiguates while the path keeps the id
    legible in dumps.
    """
    guid = ""
    if guid_index is not None:
        guid = guid_index.guid_for_path(template.prefab_path) or ""
    if not guid:
        # Fall back to the library's by_guid index (variants populate it
        # before the library indexes meta GUIDs).
        for g, t in by_guid.items():
            if t is template:
                guid = g
                break
    rel = _relative_path_string(template.prefab_path, unity_project_root)
    return f"{guid}:{rel}" if guid else rel


def _script_id_for(
    mono_props: dict[str, object],
    guid_index: GuidIndex | None,
) -> str:
    """Resolve a MonoBehaviour's ``m_Script`` to a canonical script id.

    Prefers the .cs GUID (stable across project moves); falls back to the
    project-relative ``.cs`` path when the GUID isn't resolvable. Returns
    ``""`` when neither survives — caller is expected to skip the
    MonoBehaviour rather than emit a row with no script attached.
    """
    script_ref = mono_props.get("m_Script")
    if not isinstance(script_ref, dict):
        return ""
    guid = ref_guid(script_ref) or ""
    if not guid:
        return ""
    if guid_index is not None:
        path = guid_index.resolve(guid)
        if path is not None and path.suffix == ".cs":
            return guid
        return ""
    return guid


# ---------------------------------------------------------------------------
# UI detection — a target is "UI" when it (or an ancestor) carries a Canvas.
# ---------------------------------------------------------------------------

def _has_canvas(components: list[ComponentData]) -> bool:
    for comp in components:
        if comp.component_type == "Canvas":
            return True
    return False


def _scene_ui_go_fids(scene: ParsedScene) -> set[str]:
    """All GO fileIDs in the scene whose subtree is rooted at (or under) a
    Canvas. PR1 marks reference rows that resolve into this set as
    ``target_is_ui: true`` so PR3b's classifier can fold UI-bearing
    instances into the client-domain signal without re-walking the scene.
    """
    ui: set[str] = set()

    def _walk(node: SceneNode, in_canvas: bool) -> None:
        is_canvas_subtree = in_canvas or _has_canvas(node.components)
        if is_canvas_subtree:
            ui.add(node.file_id)
        for child in node.children:
            _walk(child, is_canvas_subtree)

    for root in scene.roots:
        _walk(root, False)
    return ui


def _prefab_ui_go_fids(template: PrefabTemplate) -> set[str]:
    """Same logic as ``_scene_ui_go_fids`` but for a prefab template."""
    ui: set[str] = set()

    def _prefab_has_canvas(node: PrefabNode) -> bool:
        for comp in node.components:
            if comp.component_type == "Canvas":
                return True
        return False

    def _walk(node: PrefabNode, in_canvas: bool) -> None:
        is_canvas_subtree = in_canvas or _prefab_has_canvas(node)
        if is_canvas_subtree:
            ui.add(node.file_id)
        for child in node.children:
            _walk(child, is_canvas_subtree)

    if template.root is not None:
        _walk(template.root, False)
    return ui


# ---------------------------------------------------------------------------
# Reference / config classification.
# ---------------------------------------------------------------------------

def _is_object_ref(value: object) -> bool:
    """Return True when a YAML value matches Unity's object-reference shape
    — a dict carrying ``fileID`` (plus optional ``guid``). The zero-GUID
    sentinel is still a reference; it just points inside the current file.
    """
    if not isinstance(value, dict):
        return False
    return "fileID" in value


def _classify_reference(
    value: dict[str, object],
    guid_index: GuidIndex | None,
    local_namespace: str,
    local_go_fids: set[str],
    ui_go_fids: set[str],
    comp_fid_to_go_fid: dict[str, str],
    mono_component_fids: set[str],
    comp_fid_to_type: dict[str, str],
    by_guid: dict[str, PrefabTemplate] | None,
    unity_project_root: Path | None,
) -> tuple[str, str, bool, str] | None:
    """Resolve a single ref into
    ``(target_kind, target_ref, target_is_ui, target_component_type)``.

    Returns ``None`` when the ref is the zero-fileID null sentinel (Unity's
    "unassigned" shape) — callers should skip those rows entirely so the
    plan doesn't drown in nulls.

    ``target_component_type`` is the empty string for every kind except
    "gameobject"-from-built-in-component (Rigidbody / Button / RectTransform
    / Collider / AudioSource / ...). It tells the host which component to
    look up on the resolved GO. For peer-MonoBehaviour refs the kind stays
    ``"component"`` and the host registry resolves the instance_id directly,
    so the type field stays empty there too.
    """
    file_id_raw = value.get("fileID")
    if file_id_raw in (0, "0", None):
        # Unity's null ref — both fileID and guid are 0. Some fields also
        # store ``{fileID: 0}`` for "deliberately empty"; either way nothing
        # to wire.
        guid = ref_guid(value) or ""
        if not guid or guid == "0" * 32:
            return None
    file_id = str(file_id_raw) if file_id_raw is not None else ""

    guid = ref_guid(value) or ""
    if guid and guid != "0" * 32:
        # Cross-asset reference.
        if guid_index is not None:
            path = guid_index.resolve(guid)
            if path is not None:
                suffix = path.suffix.lower()
                if suffix == ".prefab":
                    template = (by_guid or {}).get(guid)
                    if template is not None:
                        prefab_id = _prefab_stable_id(
                            template, guid_index, by_guid or {},
                            unity_project_root,
                        )
                    else:
                        # Prefab GUID we can't resolve to a parsed template
                        # (parse failure earlier in the pipeline). Use a
                        # GUID-only id; PR3a's resolver fails closed.
                        prefab_id = (
                            f"{guid}:"
                            f"{_relative_path_string(path, unity_project_root)}"
                        )
                    return ("prefab", prefab_id, False, "")
                if suffix == _SCRIPTABLE_OBJECT_SUFFIX:
                    return ("scriptable_object", guid, False, "")
                return ("asset", guid, False, "")
        # GUID present but unresolvable — record as asset with the raw guid.
        return ("asset", guid, False, "")

    # Local ref. ``file_id`` may identify either a GameObject (when it's a
    # direct GO fid) or a Component (Transform / RectTransform / Button /
    # MonoBehaviour / etc.). The UI signal lives at the GameObject level —
    # a Canvas-subtree marker — so component refs must resolve through
    # ``comp_fid_to_go_fid`` to the owning GO before the UI check, or any
    # ``[SerializeField] Button quitBtn`` style field would be silently
    # mislabeled ``target_is_ui=False``.
    if not file_id:
        return None
    if file_id in local_go_fids:
        # Direct GameObject reference. The schema's ``gameobject`` kind;
        # no component type to attach.
        owning_go = file_id
        target_is_ui = owning_go in ui_go_fids
        return ("gameobject", f"{local_namespace}:{file_id}", target_is_ui, "")
    # Component reference. The schema only gives PR2/PR4 stable lookup for
    # PEER MONOBEHAVIOURS via ``instance_id``; built-in components like
    # ``Rigidbody`` / ``Button`` / ``RectTransform`` / ``Collider`` /
    # ``AudioSource`` have no host-side instance id, so the host has to
    # resolve them by walking the owning GameObject for the component
    # class. Codex P1 finding: the prior implementation lumped both into
    # ``target_kind="component"`` with a ``<namespace>:<component_fid>``
    # ref the host couldn't resolve for built-in types.
    owning_go = comp_fid_to_go_fid.get(file_id, "")
    target_is_ui = owning_go in ui_go_fids if owning_go else False
    if file_id in mono_component_fids:
        # Peer MonoBehaviour: ref is the peer's instance_id (host registry
        # looks it up directly). No component-type field needed.
        return (
            "component",
            f"{local_namespace}:{file_id}",
            target_is_ui,
            "",
        )
    # Built-in component (or any non-MB component): resolve to the owning
    # GameObject's id. The host walks the GO for the recorded component
    # type. When ``comp_fid_to_go_fid`` can't resolve (cross-prefab ref to
    # a component whose owner isn't in this walk's scope), fall back to
    # the historical "component" kind so we don't silently drop the ref;
    # the contract verifier will then surface the unresolvable target.
    if not owning_go:
        return (
            "component",
            f"{local_namespace}:{file_id}",
            False,
            comp_fid_to_type.get(file_id, ""),
        )
    return (
        "gameobject",
        f"{local_namespace}:{owning_go}",
        target_is_ui,
        comp_fid_to_type.get(file_id, ""),
    )


def _split_config_and_refs(
    mono_props: dict[str, object],
    namespace: str,
    component_file_id: str,
    guid_index: GuidIndex | None,
    local_go_fids: set[str],
    ui_go_fids: set[str],
    comp_fid_to_go_fid: dict[str, str],
    mono_component_fids: set[str],
    comp_fid_to_type: dict[str, str],
    by_guid: dict[str, PrefabTemplate] | None,
    unity_project_root: Path | None,
) -> tuple[dict[str, object], list[SceneRuntimeReference]]:
    """Walk one MonoBehaviour's serialized fields. Scalars and structs go
    into ``config``; object refs (single or array) become reference rows.
    Returns ``(config, references)`` for that one component.
    """
    config: dict[str, object] = {}
    refs: list[SceneRuntimeReference] = []
    instance_id = f"{namespace}:{component_file_id}"

    for key, value in mono_props.items():
        if key in _MONO_INTERNAL_PROPS:
            continue

        if isinstance(value, list):
            # Array field — may mix refs and plain values. Track the
            # element index so the AI can rewire the array slot-by-slot.
            # Null array slots are DROPPED rather than recorded under a
            # synthetic ``target_kind: "null"`` — the contract enumerates
            # five kinds (component/gameobject/prefab/scriptable_object/
            # asset) and anything else violates the schema PR1 commits to.
            # The AI sees the gap as a hole in the index sequence; the
            # host treats it the same way Unity would (unassigned slot).
            list_emitted_ref = False
            scalar_elements: list[object] = []
            for idx, element in enumerate(value):
                if _is_object_ref(element):
                    classified = _classify_reference(
                        cast(dict[str, object], element), guid_index,
                        namespace, local_go_fids, ui_go_fids,
                        comp_fid_to_go_fid,
                        mono_component_fids, comp_fid_to_type,
                        by_guid, unity_project_root,
                    )
                    if classified is None:
                        # Null sentinel — leave it out of refs entirely.
                        list_emitted_ref = True
                        continue
                    target_kind, target_ref, target_is_ui, comp_type = classified
                    row: SceneRuntimeReference = {
                        "from": instance_id,
                        "field": key,
                        "index": idx,
                        "target_kind": target_kind,
                        "target_ref": target_ref,
                        "target_is_ui": target_is_ui,
                    }
                    if comp_type:
                        # SceneRuntimeReferenceExtra surface — added only
                        # when the planner observed the built-in component
                        # type and wants the host to know what to search
                        # for on the resolved GameObject.
                        cast(dict[str, object], row)["target_component_type"] = comp_type
                    refs.append(row)
                    list_emitted_ref = True
                else:
                    scalar_elements.append(element)
            if not list_emitted_ref:
                # Scalar-only array — keep it in config.
                config[key] = scalar_elements
            continue

        if _is_object_ref(value):
            classified = _classify_reference(
                cast(dict[str, object], value), guid_index,
                namespace, local_go_fids, ui_go_fids,
                comp_fid_to_go_fid,
                mono_component_fids, comp_fid_to_type,
                by_guid, unity_project_root,
            )
            if classified is None:
                continue
            target_kind, target_ref, target_is_ui, comp_type = classified
            row = {
                "from": instance_id,
                "field": key,
                "index": None,
                "target_kind": target_kind,
                "target_ref": target_ref,
                "target_is_ui": target_is_ui,
            }
            if comp_type:
                cast(dict[str, object], row)["target_component_type"] = comp_type
            refs.append(cast(SceneRuntimeReference, row))
            continue

        # Scalar or struct (Vector3 / Color / etc.) — stash verbatim.
        config[key] = value

    return config, refs


# ---------------------------------------------------------------------------
# Scene walk
# ---------------------------------------------------------------------------

def _walk_scene(
    scene: ParsedScene,
    namespace: str,
    guid_index: GuidIndex | None,
    by_guid: dict[str, PrefabTemplate],
    unity_project_root: Path | None,
    runtime_bearing: set[str],
) -> SceneRuntimeScene:
    """Build the per-scene block. DFS through scene roots so
    ``lifecycle_order`` follows the hierarchy."""
    instances: list[SceneRuntimeInstance] = []
    references: list[SceneRuntimeReference] = []
    lifecycle: list[str] = []

    local_go_fids = set(scene.all_nodes.keys())
    ui_go_fids = _scene_ui_go_fids(scene)
    # ``comp_fid -> owning_go_fid`` so component refs (Transform / Button /
    # RectTransform / sibling MonoBehaviours) propagate the UI signal
    # through the owning GameObject — the lookup the contract relies on
    # for ``target_is_ui``.
    comp_fid_to_go_fid: dict[str, str] = {}
    # ``comp_fid -> Unity component type name``. Populated alongside the
    # owning-GO map; the codex P1 fix uses this to tell the host which
    # built-in component (``Rigidbody`` / ``Button`` / ``BoxCollider`` /
    # ...) to search for on the resolved GameObject when a serialized
    # field references a non-MonoBehaviour component.
    comp_fid_to_type: dict[str, str] = {}
    # ``set(comp_fid)`` for components that ARE MonoBehaviours. Refs
    # into this set keep ``target_kind="component"`` because PR2 stamps
    # them with stable instance ids the host can look up directly.
    mono_component_fids: set[str] = set()
    for go_fid, n in scene.all_nodes.items():
        for c in n.components:
            comp_fid_to_go_fid[c.file_id] = go_fid
            comp_fid_to_type[c.file_id] = c.component_type
            if c.component_type == "MonoBehaviour":
                mono_component_fids.add(c.file_id)

    def _visit(node: SceneNode) -> None:
        for comp in node.components:
            if comp.component_type != "MonoBehaviour":
                continue
            script_id = _script_id_for(comp.properties, guid_index)
            if not script_id:
                continue
            runtime_bearing.add(script_id)
            enabled_raw = comp.properties.get("m_Enabled", 1)
            enabled = bool(enabled_raw) if isinstance(enabled_raw, (int, bool)) else True

            config, refs = _split_config_and_refs(
                comp.properties, namespace, comp.file_id, guid_index,
                local_go_fids, ui_go_fids, comp_fid_to_go_fid,
                mono_component_fids, comp_fid_to_type,
                by_guid, unity_project_root,
            )

            instance_id = f"{namespace}:{comp.file_id}"
            inst_row: SceneRuntimeInstance = {
                "instance_id": instance_id,
                "script_id": script_id,
                "game_object_id": f"{namespace}:{node.file_id}",
                "active": bool(node.active),
                "enabled": enabled,
                "config": config,
            }
            # R5-P1.2: parent edge so the host runtime can walk ancestors
            # when computing ``activeInHierarchy``. Scene roots get no
            # key (the SceneRuntimeInstanceExtra TypedDict is total=False,
            # so a missing key means "no parent in the planner graph"
            # and the runtime stops the upward walk).
            if node.parent_file_id is not None:
                cast(dict[str, object], inst_row)["parent_game_object_id"] = (
                    f"{namespace}:{node.parent_file_id}"
                )
            # Classifier-v2: stamp ``instance_owner_is_ui`` when the host
            # GameObject sits inside the Canvas subtree. This is the
            # strongest available client-side signal — domain-signals
            # design doc names it as a strong-client signal on par with
            # ``[SerializeField] Text`` C# annotations.
            if node.file_id in ui_go_fids:
                cast(dict[str, object], inst_row)["instance_owner_is_ui"] = True
            instances.append(inst_row)
            references.extend(refs)
            lifecycle.append(instance_id)
        for child in node.children:
            _visit(child)

    for root in scene.roots:
        _visit(root)

    return {
        "instances": instances,
        "references": references,
        "lifecycle_order": lifecycle,
    }


# ---------------------------------------------------------------------------
# Prefab walk
# ---------------------------------------------------------------------------

def _walk_prefab(
    template: PrefabTemplate,
    prefab_id: str,
    guid_index: GuidIndex | None,
    by_guid: dict[str, PrefabTemplate],
    unity_project_root: Path | None,
    runtime_bearing: set[str],
) -> SceneRuntimePrefab:
    """Build the per-prefab block. Same shape as ``_walk_scene`` but the
    namespace is the stable prefab id, instances are prefab-local, and
    intra-prefab refs resolve against the prefab's own nodes."""
    instances: list[SceneRuntimeInstance] = []
    references: list[SceneRuntimeReference] = []
    lifecycle: list[str] = []

    local_go_fids = set(template.all_nodes.keys())
    ui_go_fids = _prefab_ui_go_fids(template)
    # Same comp_fid -> owning_go_fid map as in _walk_scene; mirrors the
    # mapping prefab_parser builds when attaching components to PrefabNodes.
    comp_fid_to_go_fid: dict[str, str] = {}
    comp_fid_to_type: dict[str, str] = {}
    mono_component_fids: set[str] = set()
    for go_fid, n in template.all_nodes.items():
        for c in n.components:
            comp_fid_to_go_fid[c.file_id] = go_fid
            comp_fid_to_type[c.file_id] = c.component_type
            if c.component_type == "MonoBehaviour":
                mono_component_fids.add(c.file_id)

    def _visit(node: PrefabNode) -> None:
        for comp in node.components:
            if comp.component_type != "MonoBehaviour":
                continue
            script_id = _script_id_for(comp.properties, guid_index)
            if not script_id:
                continue
            runtime_bearing.add(script_id)
            enabled_raw = comp.properties.get("m_Enabled", 1)
            enabled = bool(enabled_raw) if isinstance(enabled_raw, (int, bool)) else True

            config, refs = _split_config_and_refs(
                comp.properties, prefab_id, comp.file_id, guid_index,
                local_go_fids, ui_go_fids, comp_fid_to_go_fid,
                mono_component_fids, comp_fid_to_type,
                by_guid, unity_project_root,
            )

            instance_id = f"{prefab_id}:{comp.file_id}"
            inst_row: SceneRuntimeInstance = {
                "instance_id": instance_id,
                "script_id": script_id,
                "game_object_id": f"{prefab_id}:{node.file_id}",
                "active": bool(node.active),
                "enabled": enabled,
                "config": config,
            }
            # R5-P1.2: parent edge for the host runtime's ancestor walk.
            # Prefab roots get no key (total=False).
            if node.parent_file_id is not None:
                cast(dict[str, object], inst_row)["parent_game_object_id"] = (
                    f"{prefab_id}:{node.parent_file_id}"
                )
            # Classifier-v2: per-instance UI ownership signal (see
            # _walk_scene for rationale).
            if node.file_id in ui_go_fids:
                cast(dict[str, object], inst_row)["instance_owner_is_ui"] = True
            instances.append(inst_row)
            references.extend(refs)
            lifecycle.append(instance_id)
        for child in node.children:
            _visit(child)

    if template.root is not None:
        _visit(template.root)

    return {
        "name": template.name,
        # R2-P1.2: bare template name resolves prefab_id ->
        # ReplicatedStorage.Templates[name] for runtime lookup.
        "template_name": template.name,
        "instances": instances,
        "references": references,
        "lifecycle_order": lifecycle,
    }


# ---------------------------------------------------------------------------
# Pre-placed prefab instances (Bug B tier 1).
# ---------------------------------------------------------------------------

def _walk_scene_prefab_placements(
    scene: ParsedScene,
    namespace: str,
    guid_index: GuidIndex | None,
    by_guid: dict[str, PrefabTemplate],
    unity_project_root: Path | None,
) -> list[SceneRuntimeScenePrefabPlacement]:
    """Emit one placement row per ``PrefabInstance`` authored into the scene.

    Each row binds a placement to its prefab subplan via the same stable
    ``prefab_id`` the subplan is keyed under (``_prefab_stable_id``). The
    runtime boots the placement's clone using that subplan's instances.

    ``parent_game_object_id`` derivation: ``PrefabInstanceData`` carries a
    ``transform_parent_file_id`` (a Transform fileID), NOT a GameObject id.
    Map it through ``scene.transform_fid_to_go_fid`` (populated by the YAML
    parser) to recover the parent GameObject id. Binary scenes leave that
    map empty, so binary placements emit without a parent key (tracked gap).

    Tier 1 reads ``active``/``enabled`` as ``True`` defaults: per-placement
    ``m_Modifications`` (including ``m_IsActive``) is tier-3 work.
    """
    placements: list[SceneRuntimeScenePrefabPlacement] = []
    fid_to_go = scene.transform_fid_to_go_fid or {}

    for pi in scene.prefab_instances:
        guid = pi.source_prefab_guid
        if not guid:
            continue
        template = by_guid.get(guid)
        if template is None:
            # No subplan to boot against — skip silently (matches the
            # converter's "unresolvable source prefab" skip at
            # scene_converter._convert_prefab_instance).
            continue
        prefab_id = _prefab_stable_id(
            template, guid_index, by_guid, unity_project_root,
        )
        row: SceneRuntimeScenePrefabPlacement = {
            "placement_id": f"{namespace}:{pi.file_id}",
            "prefab_id": prefab_id,
            "active": True,
            "enabled": True,
        }
        # Map the PrefabInstance's parent Transform fileID to the owning
        # scene GameObject id. Omit the key when the map lacks the entry
        # (root placement, or binary scene with no transform map).
        parent_go_fid = fid_to_go.get(pi.transform_parent_file_id)
        if parent_go_fid:
            cast(dict[str, object], row)["parent_game_object_id"] = (
                f"{namespace}:{parent_go_fid}"
            )
        placements.append(row)

    return placements


# ---------------------------------------------------------------------------
# Modules table (project-wide require graph).
# ---------------------------------------------------------------------------

def _build_modules_table(
    guid_index: GuidIndex | None,
    runtime_bearing: set[str],
) -> dict[str, SceneRuntimeModule]:
    """One row per ``.cs`` file in the project, keyed by canonical script
    id (the .cs GUID). Stem-keyed lookup happens at the resolver level
    (PR3a) — the source of truth here is GUID, which is what each
    MonoBehaviour's ``m_Script`` points at.

    ``class_name`` is filled best-effort via ``analyze_script``; helpers
    without a parseable ``class`` keyword keep ``class_name=""`` and rely on
    stem-keyed resolution. ``runtime_bearing`` is True for every script id
    seen attached to a scene or prefab MonoBehaviour.
    """
    from unity.script_analyzer import analyze_script  # local — heavy regex compile

    modules: dict[str, SceneRuntimeModule] = {}
    if guid_index is None:
        return modules

    cs_entries = guid_index.filter_by_kind("script")
    # First pass: analyze every .cs once and remember its immediate base
    # class so the SECOND pass can resolve component-ness across a project-
    # local inheritance chain (analyze_script only records the immediate
    # base). e.g. ``Turret : Weapon`` + ``Weapon : MonoBehaviour`` ⇒ Turret
    # is a component even though its immediate base is ``Weapon``.
    # guid -> (stem, class_name, base_class, has_lifecycle_hook)
    analyzed: dict[str, tuple[str, str, str, bool]] = {}
    base_by_class: dict[str, str] = {}
    for script_guid, entry in cs_entries.items():
        if entry.asset_path.suffix != ".cs":
            continue
        info = analyze_script(entry.asset_path)
        analyzed[script_guid] = (
            entry.asset_path.stem, info.class_name, info.base_class,
            bool(info.lifecycle_hooks),
        )
        if info.class_name:
            base_by_class[info.class_name] = info.base_class

    for script_guid, (stem, class_name, base_class, has_hook) in analyzed.items():
        # A class is a component if either:
        #   (1) it extends a known Unity component base (directly or
        #       transitively via the project-local inheritance walk), OR
        #   (2) it overrides a Unity lifecycle hook AND has a non-empty
        #       immediate base class -- a base class the resolver couldn't
        #       prove is a component (external DLL like Photon's
        #       ``MonoBehaviourPunCallbacks``, Mirror's ``NetworkBehaviour``
        #       in an unwalkable package) but the presence of inheritance
        #       plus a lifecycle hook is strong enough evidence to route
        #       through the host contract.
        #
        # The ``base_class != ""`` guard is what blocks the original false-
        # positive from the first pass (Codex P2): plain helper classes
        # like ``class Stopwatch { void Start() {} }`` -- no base, just a
        # method that happens to be named ``Start`` -- were being forced
        # through the host-bound generic contract. Requiring inheritance
        # rules them out without giving up the external-base case.
        is_component = (
            _resolves_to_component(class_name, base_class, base_by_class)
            or (has_hook and base_class != "")
        )
        modules[script_guid] = {
            "stem": stem,
            "class_name": class_name,
            "runtime_bearing": script_guid in runtime_bearing,
            "is_component_class": is_component,
        }

    # Scripts attached at runtime but absent from the guid index (e.g.,
    # outside Assets/, dynamically loaded). Record them with empty stem
    # so PR3a's resolver fails closed on the stem mismatch. They are
    # instance-backed (in ``runtime_bearing``), so treat them as components.
    for script_id in runtime_bearing:
        if script_id not in modules:
            modules[script_id] = {
                "stem": "",
                "class_name": "",
                "runtime_bearing": True,
                "is_component_class": True,
            }

    return modules


# Unity component base classes. A script extending any of these (directly
# or transitively) is a component and must convert host-bound, never legacy.
# ``NetworkBehaviour`` covers Mirror / legacy UNet networked components.
_COMPONENT_BASE_CLASSES = frozenset({"MonoBehaviour", "NetworkBehaviour"})


def _resolves_to_component(
    class_name: str,
    base_class: str,
    base_by_class: dict[str, str],
) -> bool:
    """True when ``class_name`` extends a Unity component base directly or
    through a project-local chain.

    Walks ``base_class -> its base -> ...`` using ``base_by_class`` (the
    project's class->immediate-base map). Stops at a known component base,
    at an unknown/external base (not in the map), or on a cycle. The chain
    is project-bounded, so external bases like ``NetworkBehaviour`` (defined
    in a package, not in ``base_by_class``) are caught by the direct
    ``_COMPONENT_BASE_CLASSES`` membership check on each hop.
    """
    seen: set[str] = set()
    current = base_class
    while current and current not in seen:
        if current in _COMPONENT_BASE_CLASSES:
            return True
        seen.add(current)
        current = base_by_class.get(current, "")
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def plan_scene_runtime(
    parsed_scenes: list[ParsedScene],
    prefab_library: PrefabLibrary | None,
    guid_index: GuidIndex | None,
    unity_project_root: Path | None = None,
) -> SceneRuntimeArtifact:
    """Build the project-level ``scene_runtime`` artifact.

    Pure function over parsed inputs — safe to call from both single- and
    multi-scene drivers. The returned dict is JSON-serializable and lands
    verbatim in ``conversion_plan.json`` under the ``scene_runtime`` key.
    """
    by_guid: dict[str, PrefabTemplate] = (
        dict(prefab_library.by_guid) if prefab_library is not None else {}
    )

    scenes_block: dict[str, SceneRuntimeScene] = {}
    prefabs_block: dict[str, SceneRuntimePrefab] = {}
    placements_block: list[SceneRuntimeScenePrefabPlacement] = []
    runtime_bearing: set[str] = set()

    for scene in parsed_scenes:
        namespace = _scene_namespace(scene, unity_project_root)
        scenes_block[namespace] = _walk_scene(
            scene, namespace, guid_index, by_guid,
            unity_project_root, runtime_bearing,
        )
        # Bug B tier 1: pre-placed prefab instances. The subplans they bind
        # to are built below; placements only need the stable prefab_id,
        # which ``_prefab_stable_id`` computes deterministically.
        placements_block.extend(_walk_scene_prefab_placements(
            scene, namespace, guid_index, by_guid, unity_project_root,
        ))

    if prefab_library is not None:
        for template in prefab_library.prefabs:
            prefab_id = _prefab_stable_id(
                template, guid_index, by_guid, unity_project_root,
            )
            prefabs_block[prefab_id] = _walk_prefab(
                template, prefab_id, guid_index, by_guid,
                unity_project_root, runtime_bearing,
            )

    modules_block = _build_modules_table(guid_index, runtime_bearing)

    return {
        "modules": modules_block,
        "scenes": scenes_block,
        "prefabs": prefabs_block,
        "scene_prefab_placements": placements_block,
        "domain_overrides": {},
    }


# ---------------------------------------------------------------------------
# Stem-keyed require graph (consumed by PR3a's require-resolution pass).
#
# The graph is a derivative view of the modules table — kept as a separate
# helper so PR3a can call it without re-walking scenes. Stem collisions are
# detected here and reported to the caller; PR3a is what actually
# fail-closes on them.
# ---------------------------------------------------------------------------

class RequireGraph(TypedDict):
    """Stem-keyed view of the modules table. ``by_stem[<stem>]`` maps to a
    single script id when the stem is unique, and ``collisions`` lists the
    stems that appear on more than one module. PR3a's resolver consults
    ``by_stem`` for ``require("@scene_runtime/<stem>")`` and fails closed
    when the stem is in ``collisions``.
    """

    by_stem: dict[str, str]
    collisions: dict[str, list[str]]


def build_require_graph(modules: dict[str, SceneRuntimeModule]) -> RequireGraph:
    """Build the stem-keyed lookup. Collisions surface here; the enforcement
    decision (fail closed under ``--scene-runtime=generic``) belongs to
    PR3a so PR1 stays inert by default."""
    by_stem: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    seen: dict[str, list[str]] = {}
    for script_id, mod in modules.items():
        stem = mod.get("stem") or ""
        if not stem:
            continue
        seen.setdefault(stem, []).append(script_id)
    for stem, ids in seen.items():
        if len(ids) == 1:
            by_stem[stem] = ids[0]
        else:
            collisions[stem] = sorted(ids)
    return {"by_stem": by_stem, "collisions": collisions}
