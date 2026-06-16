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

import hashlib
import re
from pathlib import Path
from typing import TypedDict, cast

# Loader-name regex is owned by storage_classifier (its original consumer);
# the planner imports it so the `is_loader` field stamped on each module
# row stays in lock-step with storage_classifier's parallel decision path.
# Phase 2a slice 2 promoted the regex from `_REPLICATED_FIRST_HINTS` to
# the public `REPLICATED_FIRST_HINTS` name to enable this share.
from converter.storage_classifier import REPLICATED_FIRST_HINTS

from core.roblox_types import RbxScript
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
    # Phase 2a slice 2: lifecycle-role inputs available pre-RbxScript. Both
    # are REQUIRED on every ``runtime_bearing`` row — build_topology
    # invariant 7 fails closed when either is absent (catches external-
    # provenance scene_runtime artifacts that bypass the planner). The
    # planner stamps both at module-row construction (line ~1045) so
    # downstream consumers (build_topology, slice 5's storage_classifier
    # rewrite) read a single canonical surface rather than re-deriving.
    #
    # ``character_attached``: True when the script is attached to the
    # player-character prefab (matches `derive_module_lifecycle_role`'s
    # ``character_attached`` param exactly). Slice 2 stamps False
    # everywhere; slice 5 plumbs the real signal from scene_converter's
    # player-character-prefab walk into the planner. False default is
    # behavior-neutral because storage_classifier's parallel
    # `character_script_names` parameter still supplies the same signal
    # to the legacy decision path.
    #
    # ``is_loader``: True when the script's stem matches the
    # ``REPLICATED_FIRST_HINTS`` regex (loaders / splash / boot scripts
    # destined for `ReplicatedFirst`). The planner imports the regex
    # from `storage_classifier` so both producers share a single source
    # of truth. Slice 5 will read this field instead of re-deriving the
    # regex in storage_classifier's decision tree.
    character_attached: bool
    is_loader: bool
    # ``has_character_controller``: True when one or more of this script's
    # placed instances is co-located on a GameObject that also carries a Unity
    # ``CharacterController`` component (the engine-level "this is the player
    # avatar" signal -- the same upstream fact scene_converter materializes as
    # the ``_HasCharacterController`` Part attribute). Derived from the
    # deterministic Unity object graph at topology-walk time, NOT from the
    # non-deterministic transpiled output, so generic player identity is robust
    # to AI transpile-shape variance. ``contract_pipeline`` consumes it to pick
    # the player controller for the movement/camera facet lowerings
    # (unique-and-exclusive: exactly one such script => the player; 0 or >1 =>
    # fail closed). Default-absent on pre-existing artifacts; consumers read it
    # with ``.get(..., False)`` (no invariant gates on it, so no backfill).
    has_character_controller: bool
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


class SceneRuntimeStaticChannel(TypedDict):
    """One C#-derived ``static event`` lowered to a shared module-table field.

    A C# ``public static event`` is a TYPE-level entity that must exist before
    any instance ``Awake``. The converter lowers it to a BindableEvent stored on
    the module table FIELD (``Player.AmmoUpdate``) that the producer fires and the
    consumer reads. Because the producer's prefab-batch ``Awake`` can run AFTER a
    consumer's scene-batch ``Awake``, the runtime pre-sets this field before any
    ``Awake`` batch (``SceneRuntime:_ensureStaticEventChannels``) so the channel
    instance is shared regardless of order. Every field here is derived from the
    DETERMINISTIC C# ``static event`` declaration, never the AI-emitted Luau.

    ``module_id``: the canonical script id (``.cs`` GUID or project-relative
        source path) of the declaring class. UNIQUE per module (it is the
        ``modules`` dict key), so it is the structured identity the channel
        hierarchy keys on.
    ``field_name``: the C# event member name = the Luau module-table field.
    ``channel_name``: the BindableEvent INSTANCE name — the BARE ``field_name``.
        The instance is made unique not by mangling this name but by parenting it
        under a per-module ``Folder`` (``module_folder``), so two classes' same-
        named static events get DISTINCT instances under DISTINCT folders (no
        cross-class aliasing) AND no flat-concat keyspace collision (a flat
        ``<stem>_<field>`` aliases ``stem="A_B",field="C"`` with
        ``stem="A",field="B_C"``). The consumer reads the module FIELD
        (``field_name``), never the BindableEvent name or its location, so the
        structured location does not affect the rendezvous.
    ``module_folder``: the OPAQUE, dot-free, collision-resistant token of a per-
        module ``Folder`` created under ``parent_path``. Derived from the UNIQUE
        ``module_id`` via a stable hash (NOT a lossy sanitization of the id, which
        could re-collapse two distinct source-path ids — see
        ``_module_channel_folder``). The runtime find-or-creates this one Folder
        under the (strictly-resolved, must-already-exist) ``parent_path`` and
        parents the BindableEvent under it.
    ``parent_path``: dotted DataModel path of the (already-existing) container the
        per-module Folder is created under (the declaring module's own container,
        e.g. ``ReplicatedStorage`` for a client cross-script signal). The runtime
        resolves this STRICTLY (fail-closed nil if any segment is missing) and only
        creates the terminal ``module_folder`` Folder beneath it.
    ``module_path``: dotted DataModel path the runtime ``require``s to reach the
        module table whose field is set.
    ``domain``: ``"client"`` | ``"server"`` — the channel is a same-domain
        BindableEvent field on the shared VM; cross-domain signals route via
        RemoteEvents and are NOT emitted here.
    """

    module_id: str
    field_name: str
    channel_name: str
    module_folder: str
    parent_path: str
    module_path: str
    domain: str


class SceneRuntimeArtifact(TypedDict, total=False):
    modules: dict[str, SceneRuntimeModule]
    scenes: dict[str, SceneRuntimeScene]
    prefabs: dict[str, SceneRuntimePrefab]
    # C#-static-event channels the runtime pre-sets before any Awake batch so the
    # producer + consumer share the BindableEvent field regardless of scene-vs-
    # prefab Awake order. Populated by ``_subphase_inject_scene_runtime`` (generic
    # mode, write_output) — AFTER storage-classify has stamped each module's final
    # ``domain`` / ``container`` / ``module_path``. Same-domain only.
    static_channels: list["SceneRuntimeStaticChannel"]
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
    character_controller_scripts: set[str],
) -> SceneRuntimeScene:
    """Build the per-scene block. DFS through scene roots so
    ``lifecycle_order`` follows the hierarchy.

    ``character_controller_scripts`` accumulates the script ids of every
    MonoBehaviour co-located on a GameObject that also carries a Unity
    ``CharacterController`` -- the deterministic upstream player-avatar signal
    (scene instances are always placed/live, so scene evidence always counts).
    """
    # Canonical CharacterController type set -- single source of truth shared
    # with scene_converter (which stamps ``_HasCharacterController`` from the
    # same set). Local import avoids a module-load cycle (scene_converter is a
    # heavy importer); sys.modules caches it after the first walk.
    from converter.scene_converter import _CHARACTER_CONTROLLER_TYPES
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
    # GameObjects carrying a Unity CharacterController -- their co-located
    # MonoBehaviours are player-controller candidates (see
    # ``character_controller_scripts``).
    cc_go_fids: set[str] = set()
    for go_fid, n in scene.all_nodes.items():
        for c in n.components:
            comp_fid_to_go_fid[c.file_id] = go_fid
            comp_fid_to_type[c.file_id] = c.component_type
            if c.component_type == "MonoBehaviour":
                mono_component_fids.add(c.file_id)
            if c.component_type in _CHARACTER_CONTROLLER_TYPES:
                cc_go_fids.add(go_fid)

    def _visit(node: SceneNode) -> None:
        for comp in node.components:
            if comp.component_type != "MonoBehaviour":
                continue
            script_id = _script_id_for(comp.properties, guid_index)
            if not script_id:
                continue
            runtime_bearing.add(script_id)
            if node.file_id in cc_go_fids:
                character_controller_scripts.add(script_id)
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
    character_controller_scripts: set[str],
    collect_cc: bool,
) -> SceneRuntimePrefab:
    """Build the per-prefab block. Same shape as ``_walk_scene`` but the
    namespace is the stable prefab id, instances are prefab-local, and
    intra-prefab refs resolve against the prefab's own nodes.

    ``collect_cc`` gates ``character_controller_scripts`` accumulation: True
    ONLY for prefab templates actually PLACED in a scene. An unplaced library
    template never boots a player, so its CharacterController evidence must not
    select a player (codex fail-open guard) -- counting it could spuriously
    trip the unique-and-exclusive gate and abstain the real player.
    """
    from converter.scene_converter import _CHARACTER_CONTROLLER_TYPES
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
    cc_go_fids: set[str] = set()
    for go_fid, n in template.all_nodes.items():
        for c in n.components:
            comp_fid_to_go_fid[c.file_id] = go_fid
            comp_fid_to_type[c.file_id] = c.component_type
            if c.component_type == "MonoBehaviour":
                mono_component_fids.add(c.file_id)
            if c.component_type in _CHARACTER_CONTROLLER_TYPES:
                cc_go_fids.add(go_fid)

    def _visit(node: PrefabNode) -> None:
        for comp in node.components:
            if comp.component_type != "MonoBehaviour":
                continue
            script_id = _script_id_for(comp.properties, guid_index)
            if not script_id:
                continue
            runtime_bearing.add(script_id)
            if collect_cc and node.file_id in cc_go_fids:
                character_controller_scripts.add(script_id)
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
    character_controller_scripts: set[str],
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
            # Phase 2a slice 2: stamp lifecycle-role inputs at planner
            # construction. `is_loader` derives from the public
            # `REPLICATED_FIRST_HINTS` regex (single source — owned by
            # storage_classifier, read here). `character_attached` is
            # always False today; slice 5 wires the real signal from
            # scene_converter's player-character walk.
            "is_loader": bool(REPLICATED_FIRST_HINTS.search(stem)),
            "character_attached": False,
            "has_character_controller": script_guid in character_controller_scripts,
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
                # Empty stem can't match the loader regex.
                "is_loader": False,
                "character_attached": False,
                "has_character_controller": script_id in character_controller_scripts,
            }

    return modules


# ---------------------------------------------------------------------------
# Class-name collision detection + script-by-class-name join
# (Phase 2a slice 4 rounds 3-5).
# ---------------------------------------------------------------------------

def compute_class_name_collisions(
    modules: dict[str, "SceneRuntimeModule | dict[str, object]"],
) -> frozenset[str]:
    """Return the set of ``class_name`` values that appear on more
    than one ``SceneRuntimeModule`` row.

    Phase 2a slice 4 round 5 review (Claude P1.1 + P1.2): the
    class_name keyspace is degraded whenever two modules share a
    class_name (e.g. two ``Utils.cs`` files declaring ``class Utils``
    in different folders). Multiple call sites — the topology's
    caller_graph collision detection, the planner's reachability
    rule, the script-by-class-name join — need to make consistent
    routing decisions for these classes. This helper is the SINGLE
    canonical source of truth so all sites read the same set.

    Pre-round-5 each consumer had its own collision walk with
    different policies (caller_graph: fail closed if dep_map
    touched; scripts_by_class join: unconditional exclude; reach-
    ability rule: silent first-write-wins). The asymmetry was a
    drift surface; collapsing it to a single set keeps the
    contract explicit + maintainable.

    The empty-class_name case is ignored (rows without a
    class_name have nothing to collide on).
    """
    seen: set[str] = set()
    collisions: set[str] = set()
    for _script_id, module in modules.items():
        if not isinstance(module, dict):
            continue
        cn_obj = module.get("class_name", "")
        cn = cn_obj if isinstance(cn_obj, str) else ""
        if not cn:
            continue
        if cn in seen:
            collisions.add(cn)
        else:
            seen.add(cn)
    return frozenset(collisions)


def _compute_stem_collisions(
    modules: dict[str, "SceneRuntimeModule | dict[str, object]"],
) -> frozenset[str]:
    """Return the set of ``stem`` values that appear on more than one
    ``SceneRuntimeModule`` row.

    Phase 2a slice 5 round 3 (Codex P3): the stem keyspace was the
    fallback join channel in ``build_script_id_by_name`` but had no
    collision-exclusion gate — ``setdefault`` silently picked the
    first writer when two modules shared a stem, violating the
    docstring's degraded-service contract ("colliding class_names
    exclude BOTH rows"). This helper mirrors
    ``compute_class_name_collisions`` for the stem keyspace so the
    contract is uniform across both join channels.

    Only counts stems that DIFFER from their row's ``class_name``
    (matching stems are already covered by the class_name index).
    Empty stems are ignored.
    """
    seen: set[str] = set()
    collisions: set[str] = set()
    for _script_id, module in modules.items():
        if not isinstance(module, dict):
            continue
        cn_obj = module.get("class_name", "")
        cn = cn_obj if isinstance(cn_obj, str) else ""
        stem_obj = module.get("stem", "")
        stem = stem_obj if isinstance(stem_obj, str) else ""
        if not stem or stem == cn:
            continue
        if stem in seen:
            collisions.add(stem)
        else:
            seen.add(stem)
    return frozenset(collisions)


# Legacy heading retained — refactored in round 5 to consume the
# unified collision helper above. See the helper's docstring for the
# rationale.

def build_scripts_by_class_name(
    scripts: list[RbxScript],
    modules: dict[str, "SceneRuntimeModule | dict[str, object]"],
) -> dict[str, RbxScript]:
    """Build a class_name-keyed RbxScript index via primary-then-
    fallback join. EXCLUDES colliding class_names per the slice-3
    degraded-service contract.

    For each ``SceneRuntimeModule`` row's ``class_name``, find the
    matching ``RbxScript`` by:
      1. Primary join: ``script.name == class_name`` (the typical
         case where C# class name and emitted file name match).
      2. Fallback join: ``script.name == module.stem`` (the case
         where a C# file's declared class name differs from its
         file stem — e.g. ``Bootstrap.cs`` containing
         ``class GameInit``).

    Misses on BOTH joins mean we cannot link the script to the
    module — the entry is omitted.

    **Collision exclusion** (Phase 2a slice 4 round 4 review,
    Claude P1.1): when two ``SceneRuntimeModule`` rows share a
    ``class_name``, the class_name-keyed join is fundamentally
    ambiguous. Following the same degraded-service policy
    ``_detect_caller_graph_collisions`` uses (slice 3 round 2):
    exclude the colliding class_name from the index entirely.
    Both module rows' downstream lookups will fall through to
    safe defaults (``script_class="ModuleScript"`` in
    ``_build_modules_block``; orphan-routing in the reachability
    rule); the alternative (first-write-wins) would stamp the
    WRONG script's metadata onto the second row.

    Phase 2a slice 4 round 2 + round 3 review (Claude P1.B):
    single source of truth for the join used by both
    ``module_domain.classify_scene_runtime_domains`` (the planner's
    reachability rule) and ``pipeline._build_and_apply_topology``
    (the topology orchestrator).

    Behavior note: scripts with empty ``name`` are skipped (they
    can't be addressed via the join). Scripts present in ``scripts``
    but with no corresponding ``modules`` row are also omitted —
    pre-slice-4 the planner's reachability rule would iterate them
    but the closure check would filter them out anyway (orphan
    scripts aren't in client_classes or server_classes), so this
    is behavior-neutral for production.
    """
    script_by_name: dict[str, RbxScript] = {}
    for s in scripts:
        if s.name:
            script_by_name.setdefault(s.name, s)

    # Phase 2a slice 4 round 5 review (Claude P1.1): consume the
    # unified class_name collision set so all class-name-keyed
    # producers share ONE source of truth.
    colliding_class_names = compute_class_name_collisions(modules)

    out: dict[str, RbxScript] = {}
    for _script_id, module in modules.items():
        if not isinstance(module, dict):
            continue
        cn_obj = module.get("class_name", "")
        cn = cn_obj if isinstance(cn_obj, str) else ""
        if not cn:
            continue
        if cn in colliding_class_names:
            # Degraded-service exclusion: ambiguous class_name →
            # consumers fall through to safe defaults.
            continue
        # Primary join: script.name == class_name.
        joined = script_by_name.get(cn)
        if joined is None:
            # Fallback join: script.name == module.stem.
            stem_obj = module.get("stem", "")
            stem = stem_obj if isinstance(stem_obj, str) else ""
            if stem:
                joined = script_by_name.get(stem)
        if joined is not None:
            out[cn] = joined  # no setdefault — collisions already excluded
    return out


# ---------------------------------------------------------------------------
# Intrinsic script_class derivation (Phase 2a slice 5 round 2).
#
# The cycle this breaks: pre-slice-5, ``build_topology._build_modules_block``
# read ``RbxScript.script_type`` AFTER ``storage_classifier.classify_storage``
# had mutated it (the LocalScript-in-SSS coercion + StarterPlayerScripts /
# StarterCharacterScripts post-pass at ``storage_classifier.py:185-194``).
# That made topology a DOWNSTREAM consumer of storage routing — but the
# design contract has topology AUTHORITY over storage.
#
# Round 1 tried to fix this by reordering the pipeline so build_topology
# ran BEFORE classify_storage; review consensus rejected that approach
# (see the revert commit message). Round 2 instead captures the
# pre-classifier ``script_type`` in a NEW IMMUTABLE field
# ``RbxScript.intrinsic_script_type`` stamped at construction time
# (transpile / animation-gen / scriptable-object emit). This helper
# reads through that field so the artifact's ``script_class`` reflects
# the C# code-analysis decision regardless of the build_topology call
# site's position in the pipeline.
# ---------------------------------------------------------------------------

def derive_intrinsic_script_class(script: RbxScript | None) -> str:
    """Return the intrinsic Roblox script class for ``script``.

    "Intrinsic" means the value determined at the script's birth —
    by ``code_transpiler._classify_script_type`` for transpiled
    scripts, or by the producing module (animation generators,
    ScriptableObject emitter) for non-transpiled scripts. This value
    is stamped ONCE into ``RbxScript.intrinsic_script_type`` at
    construction time and NEVER mutated afterward.

    Returns the script_class as determined at transpile time; never
    reflects post-transpile mutations like ``classify_storage``'s
    ``LocalScript`` coercion or ``_build_and_apply_topology``'s
    animation_drivers ``Script→LocalScript`` flip. Both pass through
    the mutable ``script_type``; neither touches
    ``intrinsic_script_type``.

    Pre-classifier mutators robustness
    -------------------------------------------------------------
    The intrinsic value is specifically robust against the two
    pre-classifier mutators that today are GATED OFF from the
    topology consumer:

      (a) ``classify_storage``'s ``Script→LocalScript`` routing
          coercion (still gated by ``build_topology``'s consumer
          design — topology reads intrinsic, not the mutable field).
      (b) ``_subphase_cohere_scripts``'s
          ``fix_require_classifications`` ``Script→ModuleScript``
          rewrite, gated by generic-mode early-return (see
          ``pipeline.py:2837-2843``); ``build_topology`` consumption
          is itself gated to generic-mode (see
          ``pipeline.py:4057-4058``).

    If either gate is ever lifted, re-stamp
    ``intrinsic_script_type`` after the relevant mutator runs so the
    immutable-field contract holds at the topology consumption site.

    Returns ``"ModuleScript"`` when ``script`` is ``None`` (the
    require-target / orphan-module case at
    ``build_topology._build_modules_block``).

    Fallback for non-transpiled / pre-field-introduction paths
    -------------------------------------------------------------
    When ``intrinsic_script_type`` is ``None`` (set neither by the
    transpiler nor by an animation/ScriptableObject path), we fall
    back to ``script.script_type``. In practice this happens for:

      1. **Rehydration** (``_rehydrate_scripts_from_disk``): the
         stored ``script_type`` reflects the post-classifier value
         from the prior conversion. A resumed conversion has no
         fresher signal — preserving the post-classifier reading is
         the only honest option. The persisted topology artifact's
         ``script_class`` is preserved verbatim on resume, so this
         fallback only affects the modest set of rebuilt rows.
      2. **Scaffolding / coherence-pack synthesized scripts** that
         pre-date this field's introduction and have not been
         migrated to stamp it. New construction paths SHOULD stamp
         ``intrinsic_script_type`` so this fallback narrows over time.

    The fallback is acknowledged-impure but bounded; the immutable-
    field path is the canonical contract.
    """
    if script is None:
        return "ModuleScript"
    intrinsic = script.intrinsic_script_type
    if intrinsic:
        return intrinsic
    # Fallback (see docstring): non-transpiled / pre-field-introduction
    # construction paths fall back to the mutable ``script_type``.
    fallback = script.script_type
    if not fallback:
        return "ModuleScript"
    return fallback


# ---------------------------------------------------------------------------
# Topology join helper: RbxScript → script_id (Phase 2a slice 5 step 3).
#
# Slice 6 will need to look up a ``TopologyModuleEntry`` from an
# ``RbxScript`` (the storage-classifier decision tree iterates RbxScripts
# but topology is keyed by script_id). The class_name keyspace is the
# canonical join channel (same one ``build_scripts_by_class_name`` uses
# in reverse). This helper builds the FORWARD index so slice 6 can do
# ``script_id_by_name[s.name]`` → topology entry in one hop.
# ---------------------------------------------------------------------------

def build_script_id_by_name(
    scripts: list[RbxScript],
    modules: dict[str, "SceneRuntimeModule | dict[str, object]"],
) -> dict[str, str]:
    """Build a ``script.name -> script_id`` index via the canonical
    class_name join (primary-then-fallback), with collision exclusion.

    Mirrors ``build_scripts_by_class_name`` (which produces the reverse
    direction). Both consume ``compute_class_name_collisions`` so the
    degraded-service contract stays uniform: a class_name shared by
    two modules excludes BOTH from the index. The consumer (slice 6's
    ``_decide_script_container_from_topology``) falls through to its
    orphan-routing branch when ``script_id_by_name.get(script.name)``
    returns ``None``.

    The join uses the SAME primary-then-fallback rule as
    ``build_scripts_by_class_name``:
      1. Primary join: ``script.name == module.class_name``.
      2. Fallback join: ``script.name == module.stem`` (C# class name
         differs from file stem — e.g. ``Bootstrap.cs`` contains
         ``class GameInit``).

    The stem fallback honors a parallel ``colliding_stems`` set built
    the same way as ``compute_class_name_collisions``: when two modules
    expose the same stem (a stem that, by itself, would be ambiguous
    as a join key), BOTH rows are excluded from the index — same
    degraded-service contract the class_name keyspace uses. Without
    this gate the prior ``setdefault`` silently picked the first
    writer, violating the contract spelled out in this docstring.

    Scripts with empty ``name`` are skipped (cannot be addressed via
    the join). Scripts not present in any ``modules`` row are also
    omitted — the consumer treats them as orphans.
    """
    # Forward index from class_name + stem to script_id, with the same
    # collision-exclusion contract on BOTH keyspaces.
    colliding_class_names = compute_class_name_collisions(modules)
    colliding_stems = _compute_stem_collisions(modules)
    script_id_by_class_name: dict[str, str] = {}
    script_id_by_stem: dict[str, str] = {}
    for script_id, module in modules.items():
        if not isinstance(module, dict):
            continue
        cn_obj = module.get("class_name", "")
        cn = cn_obj if isinstance(cn_obj, str) else ""
        if cn and cn not in colliding_class_names:
            # FIRST-WRITE wins — the colliding-class set has already
            # excluded ambiguous keys, so any remaining duplicates are
            # benign (e.g. multiple references to the same canonical row).
            script_id_by_class_name.setdefault(cn, script_id)
        stem_obj = module.get("stem", "")
        stem = stem_obj if isinstance(stem_obj, str) else ""
        if stem and stem != cn and stem not in colliding_stems:
            # Stem-fallback index. Skip when stem == class_name (the
            # primary index already covers it). Skip colliding stems
            # — both rows excluded per the degraded-service contract.
            script_id_by_stem.setdefault(stem, script_id)

    out: dict[str, str] = {}
    for s in scripts:
        if not s.name:
            continue
        # Primary lookup: script.name as class_name.
        sid = script_id_by_class_name.get(s.name)
        if sid is None:
            # Fallback lookup: script.name as stem.
            sid = script_id_by_stem.get(s.name)
        if sid is not None:
            out[s.name] = sid
    return out


# ---------------------------------------------------------------------------
# Artifact migration: pre-slice-2 plans lack `character_attached` /
# `is_loader` on their `runtime_bearing` rows. Applied to on-disk plans
# on first read after slice 2 lands. Idempotent.
# ---------------------------------------------------------------------------

def backfill_lifecycle_role_inputs(scene_runtime: dict[str, object]) -> int:
    """Stamp `character_attached` + `is_loader` on every runtime-bearing
    module row that lacks them. Returns the count of rows mutated.

    The use case is a user resuming a conversion whose
    `conversion_context.json` was written by a pre-slice-2 converter.
    Without this backfill, `build_topology`'s invariant 7 would
    hard-abort the resume on every runtime-bearing module row that
    lacks the two new keys (Claude review 2026-05-28 P1).

    Migration semantics (single-source-of-truth with the planner):
      - `is_loader` is derived from ``REPLICATED_FIRST_HINTS`` on the
        stem — the same rule ``_build_modules_table`` uses at planner
        construction time. Drift between this backfill and the planner
        would mean a resumed run's modules disagree with a freshly
        replanned run on identical stems.
      - `character_attached` defaults to False. Slice 5 plumbs the
        real signal from scene_converter's player-character walk; in
        the meantime, False is the same value the planner stamps for
        every row today, so the backfill is behavior-neutral relative
        to a planner re-run on the same project.

    Non-runtime-bearing rows are exempt from invariant 7 and are not
    touched. Already-stamped rows are not touched (idempotent —
    re-running this backfill yields 0).
    """
    modules_obj = scene_runtime.get("modules", {})
    if not isinstance(modules_obj, dict):
        return 0
    count = 0
    for module in modules_obj.values():
        if not isinstance(module, dict):
            continue
        if not bool(module.get("runtime_bearing", False)):
            continue
        mutated = False
        if "is_loader" not in module:
            stem_obj = module.get("stem", "")
            stem = stem_obj if isinstance(stem_obj, str) else ""
            module["is_loader"] = bool(
                REPLICATED_FIRST_HINTS.search(stem),
            )
            mutated = True
        if "character_attached" not in module:
            module["character_attached"] = False
            mutated = True
        if mutated:
            count += 1
    return count


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


def _module_channel_folder(module_id: str, module_path: str) -> str:
    """OPAQUE, dot-free, collision-resistant Folder name for a module's static-
    event channels, keyed on the UNIQUE ``module_id``.

    ``module_id`` is the canonical script id — a ``.cs`` GUID OR a project-relative
    source path (per the scene-runtime contract), so it may contain ``/``, ``.``,
    spaces. A LOSSY sanitization (replace each illegal char with ``_``) could
    collapse two DISTINCT ids onto one Folder and re-introduce the cross-class
    aliasing this folder structure exists to prevent. So the uniqueness is carried
    by a full-id hash (never a sanitized prefix of the id); a readable stem prefix
    is added purely for debuggability and does NOT carry identity.

    The result contains no ``.`` (the runtime splits ``parent_path`` on ``.``) and
    is a valid Roblox Instance ``Name``. Deterministic over ``module_id``.
    """
    digest = hashlib.sha1(module_id.encode("utf-8")).hexdigest()[:12]
    stem = module_path.rsplit(".", 1)[-1]
    # Readable, dot-free prefix (identity is the hash, so a collision here is
    # harmless). Strip to alphanumerics/underscore; cap length.
    safe_stem = re.sub(r"[^0-9A-Za-z_]", "", stem)[:32]
    return f"sec_{safe_stem}_{digest}" if safe_stem else f"sec_{digest}"


def build_static_event_channels(
    modules: dict[str, "SceneRuntimeModule | dict[str, object]"],
    static_events_by_script_id: dict[str, list[str]],
) -> list[SceneRuntimeStaticChannel]:
    """Build the ``static_channels`` plan list from the deterministic C#
    ``static event`` enumeration.

    Pure function: for each script id that declares one or more C# static events
    (``static_events_by_script_id``, surfaced by ``script_analyzer``), emit one
    channel row per event member. Each channel pre-sets the module-table FIELD the
    producer fires and the consumer reads, so the runtime can share the
    BindableEvent instance before any ``Awake`` runs (regardless of scene-vs-
    prefab order).

    GATING (fail-closed, same-domain only):
      * The module must be runtime-bearing and resolved to a concrete same-VM
        domain (``client`` / ``server``) with a stamped ``module_path`` +
        ``container``. A helper/excluded/unstamped module is SKIPPED — a static
        field on an un-booted module has no consumer rendezvous here.
      * ``parent_path`` = the module's own container (the BindableEvent lives
        beside the module's ModuleScript). Cross-domain signals must route via
        RemoteEvents (out of scope) — gating to a single resolved domain keeps the
        pre-pass from masking a missing cross-domain bridge.

    Deterministic over (modules, static_events_by_script_id): callable twice with
    identical output (idempotent planning).
    """
    channels: list[SceneRuntimeStaticChannel] = []
    for script_id in sorted(static_events_by_script_id):
        events = static_events_by_script_id[script_id]
        if not events:
            continue
        module = modules.get(script_id)
        if not isinstance(module, dict):
            continue
        domain = module.get("domain")
        if domain not in ("client", "server"):
            continue
        if not module.get("runtime_bearing"):
            continue
        module_path = module.get("module_path")
        container = module.get("container")
        if not isinstance(module_path, str) or not module_path:
            continue
        if not isinstance(container, str) or not container:
            continue
        # The BindableEvent INSTANCE identity must be unique PER MODULE, not per
        # field name, OR two different classes' same-named static events alias onto
        # one shared event (``Player.AmmoUpdate`` and ``Enemy.AmmoUpdate`` both in
        # ReplicatedStorage → unrelated events silently cross-wired). A flat
        # ``<stem>_<field>`` concat is NOT a safe keyspace: it collides for two
        # modules with the same stem AND has delimiter ambiguity
        # (``stem="A_B",field="C"`` vs ``stem="A",field="B_C"`` both →
        # ``A_B_C``). Use a STRUCTURED identity instead: each module's channels
        # live under a per-module ``Folder`` keyed on the UNIQUE ``module_id`` (the
        # ``modules`` dict key), and the BindableEvent is named the BARE
        # ``field_name``. Distinct folders ⇒ distinct instances, with no concat
        # keyspace. The Luau module FIELD stays the C# member name (``field_name``);
        # the consumer reads the field, never the BindableEvent name or location.
        module_folder = _module_channel_folder(script_id, module_path)
        for field_name in events:
            channels.append({
                "module_id": script_id,
                "field_name": field_name,
                "channel_name": field_name,
                "module_folder": module_folder,
                "parent_path": container,
                "module_path": module_path,
                "domain": domain,
            })
    return channels


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
    # Script ids co-located with a Unity CharacterController on a PLACED
    # GameObject (scene-live or placed-prefab) -- the deterministic upstream
    # player-avatar signal consumed by contract_pipeline. See
    # ``SceneRuntimeModule.has_character_controller``.
    character_controller_scripts: set[str] = set()

    for scene in parsed_scenes:
        namespace = _scene_namespace(scene, unity_project_root)
        scenes_block[namespace] = _walk_scene(
            scene, namespace, guid_index, by_guid,
            unity_project_root, runtime_bearing, character_controller_scripts,
        )
        # Bug B tier 1: pre-placed prefab instances. The subplans they bind
        # to are built below; placements only need the stable prefab_id,
        # which ``_prefab_stable_id`` computes deterministically.
        placements_block.extend(_walk_scene_prefab_placements(
            scene, namespace, guid_index, by_guid, unity_project_root,
        ))

    # Prefab CharacterController evidence counts ONLY for placed prefabs (codex
    # fail-open guard): an unplaced library template never boots a player.
    placed_prefab_ids = {p["prefab_id"] for p in placements_block}
    if prefab_library is not None:
        for template in prefab_library.prefabs:
            prefab_id = _prefab_stable_id(
                template, guid_index, by_guid, unity_project_root,
            )
            prefabs_block[prefab_id] = _walk_prefab(
                template, prefab_id, guid_index, by_guid,
                unity_project_root, runtime_bearing,
                character_controller_scripts,
                collect_cc=prefab_id in placed_prefab_ids,
            )

    modules_block = _build_modules_table(
        guid_index, runtime_bearing, character_controller_scripts,
    )

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
