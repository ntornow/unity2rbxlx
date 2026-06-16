"""build_topology — single orchestration entry point.

Assembles the topology artifact (the
``scene_runtime["topology"]`` block) from the planner output + domain
classifier output + animation emission output, and enforces emit-time
invariants. Failure on any invariant ABORTS the build with the offending
row + the violated invariant — the design doc's "fail closed" rule.

This module is a pure assembler. The decision logic lives in:
  - ``module_domain`` (relocated from scene_runtime_domain in a later
    slice; for Phase 1 we still consume the legacy
    ``classify_scene_runtime_domains`` entry point)
  - ``cross_domain_edges.compute_cross_domain_edges``
  - ``animation_routing.resolve_driver`` /
    ``animation_routing.build_animation_driver_entry``
  - ``lifecycle_roles.derive_module_lifecycle_role``

The coordinator is the SINGLE call site downstream consumers use; direct
dict access to the artifact is discouraged (a future relocation to
``topology_plan.json`` should be a one-file change).

Phase 1 invariants (per design doc §"topology artifact" + the review's
6th add) + Phase 2a slice 2's 7th invariant:

  1. Every ``animation_drivers[*].driver_module_guid`` resolves to a
     ``modules`` entry; the animation's ``domain`` matches the driver's.
  2. Every ``cross_domain_edges[*]`` has producer + consumer with
     defined runtime domains (``client``/``server``).
  3. Every ``Anim_*`` script in the planned output corresponds to
     exactly ONE ``animation_drivers`` entry (no duplicates; structural
     via ``stable_id``).
  4. Every ``lifecycle_role`` is in the closed enum (typed via the
     Literal already; runtime check is belt-and-suspenders for
     external-provenance data).
  5. Every ``bridge_group_id`` in ``modules`` or ``animation_drivers``
     refers to an existing ``cross_domain_edges[*].id``.
  6. Every ``animation_drivers[*].driver_module_guid``'s module domain
     is in ``{"client", "server"}`` (helpers / excluded modules cannot
     drive animations).
  7. Every ``runtime_bearing`` planner row carries both
     ``character_attached`` and ``is_loader`` booleans (Phase 2a slice
     2). Catches external-provenance ``scene_runtime`` artifacts that
     bypass the planner — without it, lifecycle_role derivation
     silently defaults the missing inputs to False and the topology
     row misrepresents the underlying script.
  8. ``lifecycle_role`` is consistent with the gated inputs on every
     ``modules`` entry (Phase 2a slice 2 round 3). One-way implications:
       - ``lifecycle_role == "loader"`` ⇒ ``is_loader == True`` AND
         ``script_class in {"Script", "LocalScript"}`` AND
         ``domain == "client"`` (matches the gate inside
         ``derive_module_lifecycle_role``).
       - ``lifecycle_role == "character_attached"`` ⇒
         ``character_attached == True`` AND ``domain == "client"``.
     The opposite direction is NOT required: ``is_loader=True`` may
     legitimately coexist with ``lifecycle_role != "loader"`` when one
     of the gates fired (e.g. a server-domain loader-named script is
     auto_run, not loader — the bool preserves the raw planner
     observation while the role is the gated decision). Slice 2 round
     3 added this invariant so a future change to
     ``derive_module_lifecycle_role`` can't silently produce a
     "loader" role on a server-domain module.
 10. **Reachability ``module_path`` ↔ container coherence** (Phase
     2a slice 4; narrowed by slice 9b which dropped the parallel
     ``reachability_forced_container`` mirror) — for every
     ``modules`` entry with non-empty
     ``reachability_required_container``, ``module_path`` MUST either
     equal that container (the bare top-level row) or start with that
     container plus a dot (the strict child case — e.g.
     ``"ReplicatedStorage.HudControl"``). ``module_path`` is the
     dotted DataModel path the host runtime requires, so it has to
     point at the actually-hoisted container; the pre-slice-4 codex
     P1.1 fix at module_domain.py:1266-1278 codified this lockstep
     between container + module_path. Catches both hand-edited
     artifacts and any future refactor that splits the planner
     triple-write without keeping ``container`` + ``module_path`` in
     lockstep.

Coherence policy (Phase 2a slice 3 rounds 2-3) — NOT a fail-closed
invariant: when ``scene_runtime.modules`` contains a ``class_name``
collision (two scripts sharing a class_name) AND that name is
touched by ``dependency_map``, ``_detect_caller_graph_collisions``
excludes it from ``caller_graph`` translation. The colliding scripts
appear as orphan rows (no callers) in the curated view. An ERROR
is logged per collision (slice 3 round 3 P1.3 — bumped from WARNING
because the placement change is material and operators routinely
filter WARNINGs out of batch logs).

The "orphan → ReplicatedStorage" outcome is FORWARD-LOOKING (slice 3
round 3 P1.1): slice 5's storage_classifier rewrite is the consumer
that reads ``caller_graph`` + applies the orphan-fallback rule.
Pre-slice-5 storage_classifier continues its own lossy class_name
routing for the same scripts — slice 3 ships a topology that
describes how slice 5 WILL route them, not how the current build
routes them. The legacy pipeline silently mis-routed these projects
pre-slice-3; this policy keeps them converting (no hard regression)
while preventing slice 5 from receiving lossy edges. The deep fix
(promote dependency_map's keyspace to script_id) lives in a future
slice.
"""

from __future__ import annotations

import logging
from typing import TypedDict, cast

from core.roblox_types import RbxScript
from core.unity_types import GuidIndex

# Phase 2a slice 5 step 1: script_class derivation is centralized in
# ``scene_runtime_planner.derive_intrinsic_script_class`` so the helper's
# docstring documents the "intrinsic = pre-classifier" contract. Pre-
# slice-5 the inline derivation read ``RbxScript.script_type`` directly,
# which silently coupled topology to the classifier's coercion pass.
from converter.scene_runtime_planner import (
    SceneRuntimeArtifact,
    derive_intrinsic_script_class,
)
from converter.scene_runtime_topology.animation_routing import (
    AnimationDomain,
    AnimationDriverEntry,
    AnimationObservedTarget,
    AnimationRoutingStatus,
    ORPHAN_SCOPE,
    build_animation_driver_entry,
    compute_stable_id,
    derive_observed_target,
    resolve_driver,
)
from converter.scene_runtime_topology.cross_domain_edges import (
    CrossDomainEdge,
    compute_cross_domain_edges,
)
from converter.scene_runtime_topology.shared_flag_channels import (
    SharedFlagChannels,
)
from converter.scene_runtime_topology.lifecycle_roles import (
    LIFECYCLE_ROLES,
    LifecycleRole,
    derive_module_lifecycle_role,
)
# Phase 2a slice 10: import the predicate gate sibling
# ``finalize_topology_containers`` uses to decide whether to fire the
# late hoist. The topology read site reproduces the same gate so the
# new ``reachability_required_container`` value matches the dropped
# audit signal byte-for-byte in all four normalization cases (see
# ``_normalize_reachability_requirement`` for the case enumeration).
from converter.scene_runtime_topology.module_domain import (
    _SERVER_CONTAINERS_FOR_REACHABILITY,
)
from converter.storage_classifier import REPLICATED_STORAGE

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input shape: one emitted animation script per row. animation_converter
# (slice 5) accumulates this list as it emits; the coordinator consumes
# it to populate animation_drivers entries.
# ---------------------------------------------------------------------------

class EmittedAnimation(TypedDict, total=False):
    """One emitted Anim_* script's emission metadata.

    The data animation_converter ALREADY has at emit time, packaged for
    consumption by the topology coordinator. No new analysis required —
    every field is something the existing converter computes per emission.

    Field contract (codex B3 fix — scope_ref vs scope_display):

    ``scope_kind``: ``"prefab"`` / ``"scene"`` / ``"orphan"``. Drives
        which slice of ``scene_runtime`` ``resolve_driver`` walks.

    ``scope_ref``: the PLANNER-STABLE scope identifier. MUST be:
          - for ``scope_kind=="prefab"``: the stable prefab_id
            (``"<guid>:<project-relative path>"``) used as the key into
            ``scene_runtime["prefabs"]``,
          - for ``scope_kind=="scene"``: the scene namespace
            (``"Assets/Scenes/Main.unity"``) used as the key into
            ``scene_runtime["scenes"]``,
          - for ``scope_kind=="orphan"``: empty string.
        Passing a bare ``prefab.name`` or ``scene_path.stem`` here will
        NOT resolve through scene_runtime keying — ``resolve_driver``
        returns ``None`` and the animation lands ``routing_status =
        "unresolved"``. This is the only acceptable shape.

    ``scope_display``: display-only label kept for diagnostic / report
        rendering. Today's animation_converter bakes this into the
        emitted Anim_* script name. NOT used for stable_id keying
        (codex B4 fix — stable_id must key on stable scope identity,
        not display labels, otherwise two distinct prefabs with the
        same bare name collide in animation_drivers).

    ``ctrl_key``: disambiguated controller name from
        ``_disambiguate_by_source`` in animation_converter. The
        disambiguator appends a sha8 suffix on Unity-name collisions
        so two distinct ``.controller`` files with the same ``m_Name``
        produce distinct ctrl_keys (and distinct stable_ids).

    ``clip_disp``: disambiguated clip name (same disambiguator pass).

    ``script_name``: the emitted ``generated_scripts`` row key. Used
        for invariant 3's emission-to-driver 1:1 check + log diagnostics.

    ``observed_attribute``: the controller's primary parameter name
        (first bool / int / trigger param). Empty when the clip is
        autoplay (no parameters).

    ``curve_paths``: union of curve paths in the clip, for
        ``derive_observed_target``.

    ``prefab_scoped``: convenience flag for observed_target's
        ``scope`` field ("self.gameObject" vs "workspace").
    """

    scope_kind: str
    scope_ref: str
    scope_display: str
    ctrl_key: str
    clip_disp: str
    script_name: str
    observed_attribute: str
    curve_paths: list[str]
    prefab_scoped: bool


# ---------------------------------------------------------------------------
# Output shape: the artifact.
# ---------------------------------------------------------------------------

class TopologyModuleEntry(TypedDict, total=False):
    """One ``modules`` row in the topology artifact.

    Distinct from ``SceneRuntimeModule`` (the planner's per-module row
    on ``scene_runtime.modules``): planner row is structural facts about
    the script's instances and dependencies; topology row is the
    placement decision plus its provenance.

    **Field-semantic contract (Phase 2a slice 2 round 3):**

    ``character_attached`` and ``is_loader`` on the topology entry are
    **raw planner hints**, mirrored verbatim from the planner row. They
    are the OBSERVATIONS the planner made (a name match against
    `REPLICATED_FIRST_HINTS`, a scene_converter character-attachment
    flag in slice 5+). They are NOT post-derivation effective values.

    ``lifecycle_role`` is the **gated decision** the topology layer
    produces by feeding the bools + ``domain`` + ``script_class`` into
    ``derive_module_lifecycle_role``. Three gates apply:
      - ``character_attached`` honored only when ``domain == "client"``
      - ``is_loader`` honored only when ``domain == "client"`` AND
        ``script_class in {"Script", "LocalScript"}``
      - Both fall through to the class-driven default when their gate
        fires (``auto_run`` for Script/LocalScript, ``requireable`` for
        ModuleScript)

    Consequence: a topology row CAN have ``is_loader=True`` but
    ``lifecycle_role`` other than ``"loader"`` — e.g. a
    server-domain ``BootstrapServer.cs`` will have
    ``is_loader=True, lifecycle_role="auto_run"``. This is **deliberate
    and documented**: the bool preserves the audit trail of what the
    planner observed; lifecycle_role is what the runtime should do.
    The divergence is a feature, not a bug — slice 5's storage_classifier
    rewrite reads ``lifecycle_role`` for placement, NOT the raw bools.

    Invariant 8 (slice 2 round 3) pins the one-way implication so a
    future derivation change can't silently break the relationship:
    ``lifecycle_role == "loader"`` implies the gated bools were all
    truthy at derivation time, and ditto for
    ``lifecycle_role == "character_attached"``.

    Phase 1 populates: ``stem``, ``domain``, ``script_class``,
    ``lifecycle_role``, ``provenance``. Phase 2a slice 2 adds
    ``character_attached`` + ``is_loader``. ``bridge_group_id`` is
    populated when the module participates in a cross-domain edge;
    otherwise ``None``.
    """

    stem: str
    domain: str
    script_class: str
    lifecycle_role: LifecycleRole
    character_attached: bool
    is_loader: bool
    bridge_group_id: str | None
    provenance: "TopologyProvenance"
    # Phase 2a slice 4 — reachability pair. The planner's
    # ``_apply_reachability_rule`` (module_domain.py:1202-1284) writes
    # coordinated values atomically when a client-required helper gets
    # hoisted out of a server container:
    #   - ``script.parent_path`` on the RbxScript (the live container);
    #   - ``module_row["container"]`` + ``module_row["module_path"]``
    #     on the planner row (the canonical placement).
    # Topology mirrors the planner-side two as fields here so slice
    # 5's storage_classifier rewrite reads ONE canonical surface for
    # the decision input (``reachability_required_container``).
    # Invariant 10 enforces ``module_path`` ↔ container coherence.
    #
    # Slice 9b dropped a redundant ``reachability_forced_container``
    # mirror that previously carried the audit-trail "rule fired"
    # signal: no production code branched on it (only a tautological
    # invariant-10 lockstep check and a trivial copy at this build
    # site read it), and ``reachability_required_container`` already
    # carries the full semantic ("this module needs to be in container
    # X"). See revision-history entry 2026-05-30 and the
    # scene-runtime-architecture-ir slice plan for the dual-audit
    # rationale.
    #
    # When reachability did NOT fire for a module, both fields are
    # empty strings. When it fired, both are present and
    # ``module_path`` starts with the required container.
    reachability_required_container: str
    module_path: str


class TopologyProvenance(TypedDict, total=False):
    """Source-of-truth pointer for the topology decision. Lets a
    downstream consumer (contract verifier, debugger) trace a topology
    fact back to the C# file that produced it.

    Phase 1 emits ``source_path`` (project-relative when a guid_index
    is available); ``source_span`` is reserved for Phase 2 once we
    have line-level provenance.
    """

    source_path: str
    source_span: list[int]


class TopologyArtifact(TypedDict, total=False):
    """The persisted topology block.

    Lives under ``scene_runtime["topology"]`` in conversion_plan.json
    per design doc open-question D4 (option b). A future relocation to
    a sibling file is a one-file change provided consumers go through
    the package's accessor surface (not direct dict indexing).

    ``caller_graph`` (Phase 2a slice 3) is the curated dependency-graph
    view: for each runtime-bearing module's ``script_id``, the list of
    script_ids that ``require()`` it. INCOMING edges only — the
    inverse of ``state.dependency_map``'s outgoing form. Slice 5's
    ``script_storage`` rewrite reads this to satisfy the design doc's
    ``callers_of(script, structural_inputs.caller_graph)`` decision-
    tree term WITHOUT re-deriving graph shape from source. Empty in
    legacy mode (build_topology is gated on
    ``scene_runtime_mode != "legacy"`` at the call site); for legacy
    mode `storage_classifier`'s parallel source-scan path remains the
    single source of caller info.
    """

    modules: dict[str, TopologyModuleEntry]
    animation_drivers: dict[str, AnimationDriverEntry]
    # Phase 2b Class 1: fully-resolved static component-ref edges; every
    # row has runtime ``from_domain`` + ``to_domain`` and passes
    # invariant 2.
    cross_domain_edges: list[CrossDomainEdge]
    # Phase 2b Class 2 (reframe 2026-06-01): the ``PlayerSetSharedFlag``
    # funnel channel fact. Replaces the retired
    # ``cross_domain_edge_candidates`` bucket (the
    # ``compute_shared_attribute_candidates`` fan-out that mis-modeled the
    # dynamic shared-flag class as the static component-ref class).
    # Recompute-only, ``caller_graph``-style: produced fresh from the
    # live reader scan in ``_maybe_run_topology_prepass`` every run,
    # forwarded here, never read back from a prior on-disk plan as
    # authoritative (no preserve path). See ``shared_flag_channels.py``.
    shared_flag_channels: SharedFlagChannels
    caller_graph: dict[str, list[str]]


# ---------------------------------------------------------------------------
# Invariant errors
# ---------------------------------------------------------------------------

class TopologyInvariantError(RuntimeError):
    """Raised when build_topology detects a topology artifact that
    violates one of the 8 emit-time invariants. The message includes
    the violated invariant number and the offending row (so a build
    failure is debuggable from the log alone).
    """


def _abort(invariant: int, msg: str, *, row: object) -> None:
    raise TopologyInvariantError(
        f"topology invariant {invariant} violated: {msg} (offending row: {row!r})"
    )


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

def build_topology(
    *,
    scene_runtime: SceneRuntimeArtifact,
    emitted_animations: list[EmittedAnimation],
    scripts_by_class: dict[str, RbxScript],
    guid_index: GuidIndex | None = None,
    dependency_map: dict[str, list[str]] | None = None,
    preserved_animation_drivers: dict[str, AnimationDriverEntry] | None = None,
    preserved_caller_graph: dict[str, list[str]] | None = None,
    script_by_sid: dict[str, RbxScript] | None = None,
    reachability_requirements: dict[str, str] | None = None,
    cross_domain_edges_input: list[CrossDomainEdge] | None = None,
    shared_flag_channels_input: SharedFlagChannels | None = None,
) -> TopologyArtifact:
    """Assemble the topology artifact + enforce 8 emit-time invariants.

    ``scene_runtime`` must already carry classified domains on
    ``modules[*].domain`` (i.e. ``classify_scene_runtime_domains`` has
    run). ``scripts_by_class`` maps a module's ``class_name`` to its
    emitted ``RbxScript`` (Phase 1 needs this for ``script_type`` and
    ``parent_path``). ``emitted_animations`` is the per-emission rowset
    animation_converter produces; for Phase 1 callers that haven't
    migrated yet, pass an empty list and the animation_drivers block
    stays empty (Phase 1 invariant 3 still applies).

    ``dependency_map`` is the planner's class_name → required class_names
    map (the same source ``classify_storage`` already uses). Phase 2a
    slice 3 curates it into the ``caller_graph`` artifact field
    (inverted + class_name → script_id translated) so slice 5's
    ``script_storage`` rewrite reads a single canonical caller surface.
    ``None`` (the default) emits an empty ``caller_graph`` — back-compat
    for Phase 1 callers that pre-date slice 3 and for legacy-mode
    invocations.

    ``preserved_animation_drivers`` (Phase 2a slice 3 round 4) supports
    the resume path where ``convert_animations`` did NOT run this build
    but a prior conversion's ``animation_drivers`` block lives on the
    rehydrated ``scene_runtime.topology``. Pass the prior block via
    this parameter and ``emitted_animations=[]``; build_topology
    uses the preserved drivers verbatim instead of rebuilding from
    (empty) emissions. Invariant 3 (emission ↔ driver 1:1) is SKIPPED
    in this mode because we don't have the original emissions to
    cross-check. Invariants 1 + 4 + 5 + 6 still run on the preserved
    block; if the persisted artifact was valid when first written,
    those still hold (or the build aborts with a clear message). The
    canonical caller for this mode is
    ``pipeline._build_and_apply_topology`` when
    ``self.state.animation_result is None``.

    ``preserved_caller_graph`` (Phase 2a slice 3 round 5) supports the
    assemble-without-retranspile path where ``transpile_scripts`` did
    NOT run this build (``--no-retranspile`` or cached-script
    workflows). In that case ``state.dependency_map`` is empty (it's
    rebuilt only inside ``transpile_scripts``), so re-deriving
    caller_graph from it would silently emit ``{}`` and overwrite the
    prior caller_graph that came from a real transpile run (codex
    review slice 3 round 4 P2). Pass the prior block here and
    ``dependency_map=None`` (or the empty dict); build_topology uses
    the preserved graph verbatim. Invariant 9 is skipped (no
    dependency_map to detect collisions on); the preserved graph's
    correctness derives from when it was first computed.

    ``script_by_sid`` (Phase 2a slice 9a, followup task #10 fold-in):
    optional ``script_id -> RbxScript`` map built from the canonical
    ``build_script_id_by_name`` helper (inverted: ``s.name -> sid``
    becomes ``sid -> s``). When provided, ``_build_modules_block``
    joins on ``script_id`` directly instead of the class_name-only
    ``scripts_by_class`` lookup at ``_build_modules_block:529``. This
    closes the asymmetric-join hole slice 7 round 4 already fixed at
    the prepass boundary: two modules whose ``class_name`` collides
    but whose ``stem`` is distinct pass the prepass / classify_storage
    join via the stem fallback, but were silently dropped to
    ``script_class="ModuleScript"`` at this site because the class_name
    keyspace excludes them. The slice-3 degraded-service contract on
    BOTH keyspaces still holds — modules whose class_name + stem both
    collide (or both miss) are absent from ``script_by_sid`` and fall
    through to the same safe-default outcome as before.
    ``None`` (the default) preserves the legacy class_name-only join,
    keeping back-compat for callers that don't carry topology_inputs.

    ``reachability_requirements`` (Phase 2a slice 10): the
    ``{script_id: "ReplicatedStorage" | "__excluded__"}`` map
    ``derive_reachability_requirements`` produced earlier in the prepass
    (lives on ``TopologyInputs.reachability_requirements``). Slice 10
    switched the topology entry's ``reachability_required_container``
    source from the planner-row audit signal
    (``domain_signals["reachability_forced_container"]``) to this raw
    analysis fact, per slice 6's "save raw facts, recompute conclusions"
    rule. Normalized through the same late-hoist predicate
    ``finalize_topology_containers`` uses (``current_container in
    _SERVER_CONTAINERS_FOR_REACHABILITY``) so the emitted value is still
    exactly ``"ReplicatedStorage"`` (gate fired) or ``""`` (gate did
    not fire / no requirement). ``None`` (the default) makes the read
    fall back to the legacy audit-signal read site for back-compat with
    callers that don't carry the prepass output.

    Returns the artifact dict ready to merge into
    ``scene_runtime["topology"]``.

    Aborts (raises ``TopologyInvariantError``) on any invariant
    violation. No warnings, no soft-fails — the design doc commits to
    fail-closed on emit.
    """
    # caller_graph: build fresh from dependency_map, OR use the
    # preserved block on assemble-no-retranspile workflows where the
    # planner's dependency_map is empty for legitimate reasons (codex
    # round 4 P2). Slice 6 lifted the build / preserve logic into the
    # public ``resolve_caller_graph`` helper so the early prepass
    # (pipeline.py:_maybe_run_topology_prepass) shares ONE derivation
    # with this late assembly — if the two diverged silently, slice 7's
    # storage decision tree would route on a stale or wrong graph.
    caller_graph_block = resolve_caller_graph(
        scene_runtime, dependency_map,
        preserved_caller_graph=preserved_caller_graph,
    )

    modules_block = _build_modules_block(
        scene_runtime, scripts_by_class, guid_index,
        script_by_sid=script_by_sid,
        reachability_requirements=reachability_requirements,
    )
    # Phase 2b: ``build_topology`` is pure-assembly for the cross-domain
    # facts. The producer (``compute_cross_domain_edges``) + the
    # ``edge_enrichment`` pass + ``compute_shared_flag_channels`` MOVED
    # into ``Pipeline._maybe_run_topology_prepass`` so they share scope
    # with ``transpilation_result`` (used by the reader scan) +
    # ``script_id_by_name``. The enriched component-ref edges arrive via
    # ``cross_domain_edges_input``; the funnel channel fact via
    # ``shared_flag_channels_input``.
    #
    # Reframe (2026-06-01): the slices-1-2
    # ``cross_domain_edge_candidates`` bucket (the
    # ``compute_shared_attribute_candidates`` fan-out) was RETIRED — it
    # mis-modeled the dynamic shared-flag class as the static
    # component-ref class. Class 2 is now the ``shared_flag_channels``
    # fact. Invariant 2 stays narrow on the edges bucket.
    #
    # Back-compat path: when ``cross_domain_edges_input`` is not supplied
    # (legacy callers / unit-test fixtures invoking ``build_topology``
    # directly without the prepass), fall back to running the producer
    # in-line. This preserves byte-equivalence for the component-ref
    # bucket. NOTE: the back-compat path does NOT run enrichment (no
    # ``transpilation_result`` in scope), so emitted rows carry empty
    # ``bridge_member_scripts``; and it has no reader scan, so the
    # ``shared_flag_channels`` block FAILS OPEN (``present: True`` with
    # empty ``read_names``) exactly as the resume path does — the gate
    # never disables the funnel on missing evidence.
    if cross_domain_edges_input is not None:
        edges_block = cross_domain_edges_input
    else:
        edges_block = compute_cross_domain_edges(scene_runtime)
    if shared_flag_channels_input is not None:
        shared_flag_channels_block = shared_flag_channels_input
    else:
        # No prepass / no reader scan in hand: fail open (same shape the
        # ``transpilation_result is None`` resume path records).
        from converter.scene_runtime_topology.shared_flag_channels import (
            compute_shared_flag_channels,
        )
        shared_flag_channels_block = compute_shared_flag_channels(
            transpiled_scripts=None,
            script_id_by_name={},
            domains={},
        )
    if preserved_animation_drivers is not None:
        # Resume path: caller supplied prior animation_drivers. Skip
        # the build_from_emissions step. Invariant 3 will be skipped
        # in _enforce_invariants because we don't have emissions to
        # cross-check (see `_skip_invariant_3` flag passed below).
        animation_drivers_block = preserved_animation_drivers
    else:
        animation_drivers_block = _build_animation_drivers_block(
            scene_runtime, emitted_animations, guid_index=guid_index,
        )

    artifact: TopologyArtifact = {
        "modules": modules_block,
        "animation_drivers": animation_drivers_block,
        "cross_domain_edges": edges_block,
        "shared_flag_channels": shared_flag_channels_block,
        "caller_graph": caller_graph_block,
    }

    # Invariants run AFTER assembly so they can cross-reference blocks.
    # (Invariant 9 is the exception — it's an INPUT validator and ran
    # before block-building via _detect_caller_graph_collisions.)
    _enforce_invariants(
        artifact,
        emitted_animations=emitted_animations,
        scene_runtime=scene_runtime,
        skip_invariant_3=preserved_animation_drivers is not None,
    )
    return artifact


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------

def _normalize_reachability_requirement(
    requirement: str | None, current_container: str,
) -> str:
    """Phase 2a slice 10: project the raw analysis output
    ``reachability_requirements[sid]`` onto the same surface the
    dropped audit signal ``domain_signals["reachability_forced_container"]``
    used to carry, so the topology entry's
    ``reachability_required_container`` value is byte-equivalent to
    today's read across all four cases:

    1. ``requirement is None`` (missing / non-helper / unconstrained
       helper) -> ``""`` -- the audit signal was never written.
    2. ``requirement == "__excluded__"`` (helper reached by BOTH client
       and server) -> ``""`` -- the conflict path in
       ``finalize_topology_containers`` does NOT stamp
       ``reachability_forced_container`` (only ``fail_closed_reason``).
    3. ``requirement == "ReplicatedStorage"`` AND
       ``current_container in _SERVER_CONTAINERS_FOR_REACHABILITY``
       (hoist gate fires) -> ``"ReplicatedStorage"`` -- mirrors the
       late-hoist stamp at module_domain.py:947-955.
    4. ``requirement == "ReplicatedStorage"`` AND helper is already
       outside the gated containers (e.g. already in ReplicatedStorage
       or Workspace) -> ``""`` -- the late hoist gate at
       module_domain.py:939 short-circuits, so the audit signal stayed
       unset. Today's read site sees ``""``; we preserve that here so
       slice 10 is an internal-source swap, not a semantic change.

    The gate predicate is intentionally re-applied here rather than
    encoded into the raw map: ``reachability_requirements`` is the
    raw analysis fact (pure over ``domain_results`` +
    ``dependency_map``); the audit signal is the legacy CONCLUSION
    of "raw fact AND gate fired". Slice 6's persistence rule says
    save raw facts and recompute conclusions, so the gate lives at
    the read site that consumes the conclusion.
    """
    if requirement is None:
        return ""
    if requirement == "__excluded__":
        return ""
    if requirement == REPLICATED_STORAGE:
        if current_container in _SERVER_CONTAINERS_FOR_REACHABILITY:
            return REPLICATED_STORAGE
        return ""
    # Any future requirement values we don't recognize collapse to
    # the empty default. Today's universe is {REPLICATED_STORAGE,
    # "__excluded__"} per ``derive_reachability_requirements``.
    return ""


def _build_modules_block(
    scene_runtime: SceneRuntimeArtifact,
    scripts_by_class: dict[str, RbxScript],
    guid_index: GuidIndex | None,
    *,
    script_by_sid: dict[str, RbxScript] | None = None,
    reachability_requirements: dict[str, str] | None = None,
) -> dict[str, TopologyModuleEntry]:
    """One TopologyModuleEntry per ``scene_runtime.modules`` row.

    ``script_class`` reads through
    ``scene_runtime_planner.derive_intrinsic_script_class`` (Phase 2a
    slice 5 round 2): the intrinsic (transpile-time) C# code-analysis
    signal stamped into the immutable
    ``RbxScript.intrinsic_script_type`` field at construction time. The
    helper reads that field directly so the artifact's ``script_class``
    is invariant to post-construction mutations like
    ``storage_classifier.classify_storage``'s ``Script→LocalScript``
    coercion. Helpers without an emitted script (the planner records
    them but storage_classifier didn't synthesize a body) default to
    ``"ModuleScript"`` — they're require-target shape.

    Phase 2a slice 9a (followup task #10 fold-in): when
    ``script_by_sid`` is provided, the script-row join uses
    ``script_id`` (the loop variable, i.e. the ``modules_in`` dict
    key) instead of the class_name-only ``scripts_by_class`` lookup.
    Identical to the slice 7 round 4 fix at the prepass boundary —
    closes the same asymmetric-join hole here on the late-assembly
    side. When ``script_by_sid`` is ``None`` the legacy class_name
    join still runs (back-compat for callers that haven't migrated).

    ``lifecycle_role`` derives from domain + script_class +
    character_attached + is_loader via ``derive_module_lifecycle_role``.
    Phase 2a slice 2 reads ``character_attached`` + ``is_loader`` from
    the planner row (stamped by `scene_runtime_planner._build_modules`
    using the public `storage_classifier.REPLICATED_FIRST_HINTS` regex
    for `is_loader`; `character_attached` defaults False until slice 5
    plumbs the real signal). Invariant 7 in ``_enforce_invariants``
    fails closed when a runtime-bearing planner row omits either
    field — catches external-provenance scene_runtime artifacts that
    bypass the planner.
    """
    modules_in = cast(
        dict[str, dict[str, object]], scene_runtime.get("modules", {}),
    )
    out: dict[str, TopologyModuleEntry] = {}
    for script_id, module in modules_in.items():
        class_name_obj = module.get("class_name", "")
        class_name = class_name_obj if isinstance(class_name_obj, str) else ""
        stem_obj = module.get("stem", "")
        stem = stem_obj if isinstance(stem_obj, str) else ""
        domain_obj = module.get("domain", "")
        domain = domain_obj if isinstance(domain_obj, str) else ""

        # Phase 2a slice 5 round 2: read ``script_class`` through the
        # centralized helper. The helper consults the immutable
        # ``RbxScript.intrinsic_script_type`` field stamped at
        # construction time, so the answer is independent of the
        # mutable ``script_type`` field that ``classify_storage`` and
        # the topology animation_drivers apply phase reassign.
        #
        # Phase 2a slice 9a (#10 fold-in): when the caller supplied
        # ``script_by_sid`` (built from the canonical
        # ``build_script_id_by_name`` helper), use the script_id-keyed
        # join. The two-keyspace (class_name + stem) collision-excluded
        # producer behind ``build_script_id_by_name`` admits the
        # colliding-class_name-but-distinct-stems case that the
        # class_name-only ``scripts_by_class`` excludes.
        if script_by_sid is not None:
            script = script_by_sid.get(script_id)
        else:
            script = scripts_by_class.get(class_name)
        script_class = derive_intrinsic_script_class(script)

        # Phase 2a slice 2: read lifecycle-role inputs from the planner
        # row. `bool(module.get(..., False))` is defensive against
        # external-provenance artifacts that bypass the planner (e.g. an
        # on-disk plan from an earlier converter version); invariant 7
        # at assembly end is the fail-closed guard for that case.
        character_attached = bool(module.get("character_attached", False))
        is_loader = bool(module.get("is_loader", False))
        lifecycle_role = derive_module_lifecycle_role(
            domain=domain,
            script_class=script_class,
            character_attached=character_attached,
            is_loader=is_loader,
        )

        provenance: TopologyProvenance = {}
        if guid_index is not None and script_id:
            # ``resolve_relative`` returns the project-relative path
            # (which is what the TopologyProvenance docstring promises).
            # ``resolve`` returns the absolute path — earlier drafts
            # used it and shipped the wrong contract (codex W5). Falls
            # back to absolute when the relative form is unavailable.
            try:
                rel = guid_index.resolve_relative(script_id)
            except Exception:
                rel = None
            if rel is not None:
                provenance["source_path"] = rel.as_posix()
            else:
                try:
                    abs_path = guid_index.resolve(script_id)
                except Exception:
                    abs_path = None
                if abs_path is not None:
                    provenance["source_path"] = abs_path.as_posix()

        # Phase 2a slice 4 — read the reachability pair from the
        # planner row's mutation-driven surface. ``module_path`` is
        # the host-runtime require target stamped by
        # ``_stamp_container_and_path`` + the late hoist arm of
        # ``finalize_topology_containers``.
        module_path_obj = module.get("module_path", "")
        module_path = module_path_obj if isinstance(module_path_obj, str) else ""

        # Phase 2a slice 10: ``reachability_required_container``
        # SOURCE switched from the planner-row audit signal
        # ``domain_signals["reachability_forced_container"]`` to the
        # raw analysis fact ``reachability_requirements[sid]`` (lives
        # on ``TopologyInputs.reachability_requirements``, produced by
        # ``derive_reachability_requirements``). Normalized through
        # ``_normalize_reachability_requirement`` so the emitted value
        # is byte-equivalent to today's read across all four cases
        # (None / "__excluded__" / RS-needs-hoist / RS-already-outside-
        # gated-containers). Per slice 6's persistence rule we save
        # raw facts and recompute conclusions at the consumer; the
        # late-hoist predicate gate
        # ``current_container in _SERVER_CONTAINERS_FOR_REACHABILITY``
        # is the "recompute" step.
        #
        # ``reachability_requirements is None`` is the back-compat
        # fallback for callers that haven't migrated to pass
        # ``TopologyInputs`` through (legacy test paths, future
        # external embedders). In that case we read the legacy audit
        # signal so build_topology still emits a coherent entry. The
        # pipeline call site (``_build_and_apply_topology``) always
        # passes the prepass output, so production runs go through the
        # primary path.
        #
        # No-transpile resume semantics (slice 10 R2, Option Y --
        # accept + document + test-pin):
        #
        # On a ``--phase=write_output`` resume the pipeline runs
        # ``_maybe_run_topology_prepass`` (essential, see slice 6) but
        # ``state.dependency_map`` is empty (it is populated only inside
        # ``transpile_scripts``). ``derive_reachability_requirements``
        # short-circuits ``if not dependency_map: return {}`` (its
        # module_domain.py:782-783 contract), so the prepass hands us
        # an EMPTY ``reachability_requirements`` dict (not ``None``).
        # The primary branch below therefore takes the ``is not None``
        # path with every ``.get(sid)`` returning ``None`` -- and the
        # normalization helper collapses ``None`` to ``""``. As a
        # result, on no-transpile resume, ``reachability_required_container``
        # regenerates to ``""`` for ALL modules, REGARDLESS of whether
        # the late-hoist rule would have fired during a fresh run.
        # This is a documented, accepted regression vs the pre-slice-10
        # audit signal (which the planner persisted across resumes via
        # ``domain_signals``).
        #
        # Why it's accepted:
        #   1. The storage classifier reads
        #      ``topology_inputs["reachability_requirements"]`` DIRECTLY
        #      (``storage_classifier.py:645``), not via the topology
        #      entry's ``reachability_required_container``. Storage
        #      routing decisions are therefore unaffected -- the
        #      classifier sees the same empty dict on resume that the
        #      pre-slice-10 classifier saw, and falls back to the
        #      legacy "unconstrained helper" decision path codified
        #      by slice 6's amendment (``storage_classifier.py:569-587``,
        #      gated on ``not topology_inputs["transpile_ran"]``).
        #   2. Invariant 10 (``build_topology.py:1474-1487``) short-
        #      circuits on ``if required and ...`` so an empty
        #      ``required`` value does NOT trip the module_path / required-
        #      container coherence abort.
        #   3. Slice 9b's dual independent audit (Claude Explore + Codex
        #      exec) verified no production code consumes the artifact
        #      field for behavior; the only previous reader was the
        #      invariant-10 lockstep check, which slice 9b deleted.
        #
        # The trade is consistent with slice 6's "empty reqs on no-
        # transpile resume is acceptable" precedent (same trade slice
        # 3's preserved_caller_graph also accepts on the caller_graph
        # side -- raw facts are saved, conclusions are recomputed where
        # producible). If a future consumer needs the regenerated
        # signal on resume, the lever is to revive the
        # ``transpile_ran=False`` branch here and either (a) persist
        # ``reachability_requirements`` as a raw fact (Option X) or
        # (b) read the legacy ``domain_signals`` audit signal back
        # (the previous design). ``TopologyInputs.transpile_ran`` is
        # plumbed through (pipeline.py:4622) and unused by THIS read
        # site by design -- kept for that potential future use. See
        # ``slice-10-r1-decision.md`` for the synthesis.
        reachability_required: str
        if reachability_requirements is not None:
            # Determine the helper's CURRENT container -- the script
            # row's ``parent_path``, which ``classify_storage`` and
            # the late hoist arm have already stamped at this point in
            # the pipeline. Falls back to the module row's
            # ``container`` when the script lookup miss (e.g. modules
            # whose class_name + stem both collide / both miss are
            # absent from ``script_by_sid`` AND ``scripts_by_class``).
            # The fallback mirrors the legacy "the planner stamps
            # ``container`` in lockstep with the audit signal"
            # contract.
            current_container = ""
            if script is not None:
                pp = script.parent_path or ""
                current_container = pp
            else:
                container_obj = module.get("container", "")
                if isinstance(container_obj, str):
                    current_container = container_obj
            reachability_required = _normalize_reachability_requirement(
                reachability_requirements.get(script_id),
                current_container,
            )
        else:
            # Back-compat fallback: read the legacy audit signal. Used
            # by legacy callers and a handful of unit-test paths that
            # don't carry a TopologyInputs through. Retire when the
            # last such caller migrates.
            signals_obj = module.get("domain_signals", {})
            signals = signals_obj if isinstance(signals_obj, dict) else {}
            rfc_obj = signals.get("reachability_forced_container", "")
            reachability_required = (
                rfc_obj if isinstance(rfc_obj, str) else ""
            )

        entry: TopologyModuleEntry = {
            "stem": stem,
            "domain": domain,
            "script_class": script_class,
            "lifecycle_role": lifecycle_role,
            "character_attached": character_attached,
            "is_loader": is_loader,
            "bridge_group_id": None,
            "provenance": provenance,
            "reachability_required_container": reachability_required,
            "module_path": module_path,
        }
        out[script_id] = entry
    return out


def _build_animation_drivers_block(
    scene_runtime: SceneRuntimeArtifact,
    emitted_animations: list[EmittedAnimation],
    *,
    guid_index: GuidIndex | None = None,
) -> dict[str, AnimationDriverEntry]:
    """One AnimationDriverEntry per EmittedAnimation row, with explicit
    ``routing_status`` distinguishing resolved / unresolved / orphan.

    Replaces the older ``__orphan__`` magic-string sentinel (codex B1)
    that conflated "deliberately orphan" with "driver lookup failed".
    Each row now carries:
      - ``routing_status="resolved"`` + a real ``driver_module_guid``
        (invariants 1 + 6 enforce the link), OR
      - ``routing_status="unresolved"`` + empty ``driver_module_guid``
        + fallback ``domain="server"`` (Phase 2's C#-source narrowing
        pass will turn unresolved → resolved as it lands).
      - ``routing_status="orphan"`` + empty ``driver_module_guid`` +
        ``domain="server"`` (deliberately orphan project-wide clip).

    Unresolved rows are LOGGED as a structured warning (one line per
    build) so the operator + the CI metric see when Phase 2 narrowing
    would have helped — failing visible, not silently.
    """
    out: dict[str, AnimationDriverEntry] = {}
    unresolved_rows: list[str] = []
    for row in emitted_animations:
        ctrl_key = row.get("ctrl_key", "")
        clip_disp = row.get("clip_disp", "")
        scope_kind = row.get("scope_kind", "")
        scope_ref = row.get("scope_ref", "")
        # stable_id keys on scope_ref (planner-stable identity), NOT
        # scope_display, so two prefabs with identical bare names don't
        # collide in animation_drivers (codex B4 fix). Orphan rows use
        # the ORPHAN_SCOPE sentinel since scope_ref is empty.
        scope_segment = scope_ref if scope_ref else ORPHAN_SCOPE
        stable_id = compute_stable_id(scope_segment, ctrl_key, clip_disp)
        # Hoisted above resolve_driver: the Phase-2 source-narrowing pass
        # matches this clip param against each scope MB's Animator writes
        # (D10/D12). Reused for build_animation_driver_entry below.
        observed_attribute = row.get("observed_attribute", "")

        routing_status: AnimationRoutingStatus
        driver_module_guid: str
        domain: AnimationDomain
        if scope_kind == "orphan":
            routing_status = "orphan"
            driver_module_guid = ""
            domain = "server"
        else:
            resolved = resolve_driver(
                scene_runtime, scope_kind=scope_kind, scope_ref=scope_ref,
                guid_index=guid_index, observed_attribute=observed_attribute,
            )
            if resolved is None:
                routing_status = "unresolved"
                driver_module_guid = ""
                domain = "server"
                unresolved_rows.append(stable_id)
            else:
                driver_module_guid, driver_domain = resolved
                # ``resolve_driver`` guarantees domain ∈ {"client","server"}
                # (filters out helper / excluded modules); the cast below
                # is type-narrowing for mypy + the Literal contract.
                if driver_domain not in ("client", "server"):
                    # Defense in depth — should never fire because
                    # resolve_driver already filtered, but if it does
                    # mark unresolved rather than silently rewrite to
                    # server (codex W8 / domain Literal erasure).
                    routing_status = "unresolved"
                    driver_module_guid = ""
                    domain = "server"
                    unresolved_rows.append(stable_id)
                else:
                    routing_status = "resolved"
                    domain = cast(AnimationDomain, driver_domain)

        curve_paths_obj = row.get("curve_paths", [])
        curve_paths = (
            list(curve_paths_obj) if isinstance(curve_paths_obj, list) else []
        )
        prefab_scoped = bool(row.get("prefab_scoped", False))
        observed_target: AnimationObservedTarget = derive_observed_target(
            curve_paths, prefab_scoped=prefab_scoped,
        )

        entry = build_animation_driver_entry(
            stable_id=stable_id,
            routing_status=routing_status,
            driver_module_guid=driver_module_guid,
            domain=domain,
            observed_attribute=observed_attribute,
            observed_target=observed_target,
        )
        out[stable_id] = entry

    if unresolved_rows:
        log.warning(
            "[scene_runtime_topology] %d animation(s) routed to fallback "
            "server domain (no same-scope driver). Phase 2 narrowing will "
            "improve this. First few: %s",
            len(unresolved_rows), unresolved_rows[:5],
        )
    return out


def _build_caller_graph_block(
    dependency_map: dict[str, list[str]] | None,
    *,
    class_to_script_id: dict[str, str],
    excluded_class_names: frozenset[str] = frozenset(),
) -> dict[str, list[str]]:
    """Curate the planner's outgoing dependency_map into the topology's
    incoming ``caller_graph`` view.

    Input ``dependency_map`` shape: ``class_name -> [required class_names]``
    — the same outgoing form ``classify_storage`` consumes today
    (pipeline.py:1942-1950 builds it during transpile_scripts).

    Output shape: ``script_id -> [list of caller script_ids]`` — INCOMING
    edges, keyed by canonical id (the planner's GUID-keyed
    ``scene_runtime.modules`` key). Slice 5's ``_decide_script_container``
    rewrite reads ``callers_of(script, caller_graph)`` per the design
    doc decision tree (lines 290-306) — keyed by script_id matches the
    rest of the topology artifact's canonical id contract.

    **Contract — what's included** (slice 3 round 1 review):
    Keys and values are script_ids of ANY module with a non-empty
    ``class_name`` in ``scene_runtime.modules``, INCLUDING non-
    runtime-bearing helpers. Helpers ARE valid require() targets and
    callers (a runtime-bearing client script can require a helper, and
    the helper's transitive caller domains influence its placement).
    The consumer (slice 5's decision tree) reads ``module.domain`` on
    each caller and filters by domain — a helper-domain caller doesn't
    match the {client, server} branches, so including helpers in the
    graph is informationally complete without changing slice 5's
    decision for runtime-bearing modules.

    **Translation step** (class_name -> script_id): walk
    ``scene_runtime.modules.items()`` and build a class_name index
    using ``setdefault`` — FIRST-WRITE wins on class_name collisions,
    matching the ``scripts_by_class`` policy in ``_build_modules_block``
    (codex review of slice 3 P3: divergent semantics across the topology
    layer were inconsistent and caller_graph edges silently went to the
    wrong script_id under last-write-wins). Modules with empty
    ``class_name`` are skipped at indexing time — they have no callable
    identity.

    **Class_name collision handling** (slice 3 round 2 review,
    codex P1 + Claude P1): when two modules share a class_name AND
    the dependency_map names that class_name (caller or callee), the
    translation would be FUNDAMENTALLY LOSSY because dependency_map's
    keyspace (class_name) doesn't uniquely identify a script_id.
    Rather than fail closed (which would regress projects with
    duplicate class names that the legacy pipeline silently
    processes), this builder applies a DEGRADED-SERVICE contract:
    ``excluded_class_names`` (computed by
    ``_detect_caller_graph_collisions`` pre-derivation) is the set
    of colliding class_names. Both the caller and callee paths skip
    them entirely, leaving the colliding scripts as orphan rows in
    the curated view. Slice 5's ``_decide_script_container`` falls
    back to ReplicatedStorage for callerless ModuleScripts (its
    explicit "orphan module" rule). The operator sees a warning per
    collision in the build log and can split the colliding class to
    restore precise routing. The deep fix lives in a future slice
    that promotes dependency_map's keyspace to script_id.

    Returns empty dict when ``dependency_map`` is None or empty
    (back-compat for Phase 1 callers + legacy-mode invocations).

    ``class_to_script_id`` is the dedup'd index produced by
    ``_detect_caller_graph_collisions`` in the SAME walk that
    detected the collisions. This builder no longer rebuilds the
    index — round-3 P1.2 (computing twice with parallel filters
    invited silent divergence). ``excluded_class_names`` is the
    paired exclusion set; defensive re-check below catches any
    caller/callee class_name that's in the exclusion set (kept
    explicit so a future change to the dedup pass that's
    inconsistent with this read still fails closed).
    """
    if not dependency_map:
        return {}

    callers_by_script_id: dict[str, list[str]] = {}
    for caller_class, callee_classes in dependency_map.items():
        if caller_class in excluded_class_names:
            # Colliding caller_class: we can't determine which actual
            # script_id authored the edge. Skip entirely (degraded
            # service per the docstring).
            continue
        caller_id = class_to_script_id.get(caller_class)
        if caller_id is None:
            # Caller class isn't in the planner's modules — it doesn't
            # have a topology row to reference. Skip; the dependency
            # is unobservable from the topology's script_id keyspace.
            continue
        for callee_class in callee_classes:
            if callee_class in excluded_class_names:
                # Colliding callee: same degraded-service rule.
                continue
            callee_id = class_to_script_id.get(callee_class)
            if callee_id is None:
                continue
            # Append the caller's script_id to the callee's incoming list.
            # Determinism: classes are walked in dependency_map's
            # iteration order (insertion order in Py 3.7+); deduplicate
            # so a class that requires another twice doesn't appear
            # twice on the callee's list.
            existing = callers_by_script_id.setdefault(callee_id, [])
            if caller_id not in existing:
                existing.append(caller_id)

    return callers_by_script_id


def resolve_caller_graph(
    scene_runtime: SceneRuntimeArtifact,
    dependency_map: dict[str, list[str]] | None,
    *,
    preserved_caller_graph: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Build the script_id-keyed ``caller_graph`` view OR return the
    preserved block on assemble-no-retranspile workflows.

    Lifted from the duplicate logic that previously lived inline in
    ``build_topology`` (the fresh-build branch) and at the pipeline
    boundary (``_build_and_apply_topology`` preserve logic). Slice 6
    plumbs the SAME helper into the early prepass so the prepass and
    the late assembly share ONE caller_graph derivation -- without
    this, the prepass would re-derive the graph and any future
    divergence in the duplicated detection logic would silently leak
    into storage decisions.

    Behavior:
      - ``preserved_caller_graph is not None`` -- return it verbatim.
        Use this path when ``state.transpilation_result is None``
        (assemble-no-retranspile workflows where ``dependency_map``
        is empty for legitimate reasons -- the resume preserves the
        prior block).
      - Otherwise -- detect class_name collisions on the modules /
        dependency_map combination, then build the incoming-edge
        ``caller_graph`` block under the degraded-service contract.

    Returns ``{}`` when ``dependency_map`` is empty/None AND no
    preserved graph is provided.
    """
    if preserved_caller_graph is not None:
        return preserved_caller_graph
    colliding_classes, class_to_script_id = (
        _detect_caller_graph_collisions(scene_runtime, dependency_map)
    )
    return _build_caller_graph_block(
        dependency_map,
        class_to_script_id=class_to_script_id,
        excluded_class_names=colliding_classes,
    )


def callers_of(
    script_id: str, caller_graph: dict[str, list[str]],
) -> list[str]:
    """Return the list of script_ids that ``require()`` ``script_id``.

    Public accessor (per design doc decision tree at lines 290-306) so
    slice 5's ``_decide_script_container`` reads through this function
    rather than indexing the dict directly — keeps the surface
    refactorable if the curated view ever moves to a different shape.
    Returns an empty list when ``script_id`` has no callers (orphan
    module or absent from the graph entirely — both treated the same
    by the slice 5 decision tree).
    """
    return list(caller_graph.get(script_id, ()))


# ---------------------------------------------------------------------------
# Pre-derivation collision detection (Phase 2a slice 3 round 2)
# ---------------------------------------------------------------------------

def _detect_caller_graph_collisions(
    scene_runtime: SceneRuntimeArtifact,
    dependency_map: dict[str, list[str]] | None,
) -> tuple[frozenset[str], dict[str, str]]:
    """Detect ``class_name`` collisions in ``scene_runtime.modules``
    that are touched by ``dependency_map``, AND return the
    deduplicated class_name → script_id translation index.

    Returns ``(excluded_class_names, class_to_script_id)``:
      - ``excluded_class_names``: colliding class_names to exclude
        from ``caller_graph`` translation (degraded-service contract;
        see ``build_topology`` docstring).
      - ``class_to_script_id``: the dedup'd index for the non-
        colliding rows. ``_build_caller_graph_block`` consumes this
        directly instead of re-walking ``scene_runtime.modules`` —
        Claude review slice 3 round 3 P1.2 flagged the two-walk
        design as vulnerable to silent divergence (if a future change
        tightens one filter without the other, the exclusion set
        wouldn't match the translation set). Computing once
        eliminates the drift surface structurally.

    For each collision, logs an ERROR-level entry (slice 3 round 3
    P1.3 — WARNING was too soft for a placement-changing event).
    The conversion proceeds; the colliding scripts appear as orphan
    rows in the curated view. Once slice 5's storage_classifier
    rewrite reads ``caller_graph``, the orphan-fallback routes them
    to ReplicatedStorage. Until then the legacy storage_classifier
    continues its own lossy class_name-keyed routing — slice 5 is
    where the degraded-service promise becomes user-visible (slice 3
    round 3 P1.1: the policy is forward-looking, not current
    behavior).

    Lives OUTSIDE ``_enforce_invariants`` (which validates artifact
    outputs) because this check operates on INPUTS and informs the
    derivation. Running it pre-derivation keeps the producer
    structurally aware of the excluded class_names (Claude review
    slice 3 round 2 P1).

    Empty return tuple components when ``dependency_map`` is
    None/empty.
    """
    if not dependency_map:
        return frozenset(), {}
    modules_in = cast(
        dict[str, dict[str, object]], scene_runtime.get("modules", {}),
    )
    # Phase 2a slice 4 round 5 (Claude P1.1): consume the unified
    # ``compute_class_name_collisions`` set so this site shares one
    # canonical collision policy with the planner's
    # ``build_scripts_by_class_name`` and (round 5) the reachability
    # rule's ``class_to_script_id`` index. The dep_map-touched filter
    # below is the SUBSET that affects caller_graph — the underlying
    # collision detection is shared.
    from converter.scene_runtime_planner import (
        compute_class_name_collisions,
    )
    all_collisions = compute_class_name_collisions(modules_in)
    # Still build per-class script_id lists so the log message can
    # name the competing script_ids for operators.
    class_to_script_ids: dict[str, list[str]] = {}
    for script_id, module in modules_in.items():
        cn_obj = module.get("class_name", "")
        if isinstance(cn_obj, str) and cn_obj:
            class_to_script_ids.setdefault(cn_obj, []).append(script_id)

    # Class_names referenced as caller or callee in dep_map.
    touched_classes: set[str] = set(dependency_map.keys())
    for callees in dependency_map.values():
        for c in callees:
            if isinstance(c, str):
                touched_classes.add(c)

    excluded: set[str] = set()
    class_to_script_id: dict[str, str] = {}
    for cn, sids in class_to_script_ids.items():
        # The dep-map-touched filter is what makes a collision matter
        # for caller_graph — exclude only those.
        if cn in all_collisions and cn in touched_classes:
            excluded.add(cn)
            continue
        # Non-colliding (or untouched-collision): keep the first
        # script_id. setdefault preserves first-write semantics,
        # consistent with _build_modules_block's scripts_by_class.
        class_to_script_id[cn] = sids[0]

    if excluded:
        # One log entry per collision so operators can grep + count.
        for cn in sorted(excluded):
            sids = sorted(class_to_script_ids[cn])
            log.error(
                "[scene_runtime_topology] class_name %r maps to %d "
                "script_ids %r AND appears in dependency_map; "
                "excluding from caller_graph (lossy class_name "
                "keyspace). Slice 5's storage_classifier rewrite "
                "(when it lands) will see these as orphan modules "
                "and route to ReplicatedStorage. Today's legacy "
                "storage_classifier continues its own lossy "
                "class_name routing for the same scripts. Promote "
                "dependency_map's keyspace to script_id at planner "
                "construction or split the colliding class to "
                "restore precise routing.",
                cn, len(sids), sids,
            )

    return frozenset(excluded), class_to_script_id


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

_VALID_RUNTIME_DOMAINS: frozenset[str] = frozenset({"client", "server"})


def _enforce_invariants(
    artifact: TopologyArtifact,
    *,
    emitted_animations: list[EmittedAnimation],
    scene_runtime: SceneRuntimeArtifact,
    skip_invariant_3: bool = False,
) -> None:
    """Apply the post-derivation emit-time invariants. Raises on any
    violation.

    Each invariant is a single ``for`` loop with a clear failure
    message. The errors are catchable (``TopologyInvariantError``) so
    test code can assert specific invariant numbers without scraping
    log output.

    Invariant 9 (the input-collision check) lives in
    ``_detect_caller_graph_collisions`` and runs pre-derivation, NOT
    here — see that helper's docstring for rationale.

    ``skip_invariant_3`` (Phase 2a slice 3 round 4) bypasses the
    emission ↔ driver 1:1 cross-check when the caller supplied
    ``preserved_animation_drivers``. We don't have the original
    emissions to cross-check against (the persisted artifact dropped
    them when it was first written). Invariants 1, 4, 5, 6 still run
    on the preserved drivers block — if the prior build was valid
    those still hold; if the persisted artifact has been hand-edited
    into an invalid state, those catch it.
    """
    modules_block = artifact.get("modules", {})
    animation_drivers = artifact.get("animation_drivers", {})
    edges = artifact.get("cross_domain_edges", [])

    # Invariant 1 + 6: applied ONLY to resolved entries. Unresolved /
    # orphan entries have empty ``driver_module_guid`` by design (Phase
    # 1 narrowing limitation / deliberate orphan) and skip both checks.
    # The anim.domain == driver_module.domain equality check was
    # tautological at fresh-build time (the entry's domain is BUILT
    # FROM the driver's domain in ``_build_animation_drivers_block``).
    # Slice 3 round 4 introduced ``preserved_animation_drivers``, which
    # copies entries verbatim while ``modules_block`` is rebuilt from
    # current classifier output — that breaks the by-construction
    # tautology. Codex review slice 3 round 6 P1 flagged the gap: a
    # ``domain_overrides`` / ``networking`` edit between runs moves a
    # driver module's domain, but the preserved animation driver row
    # keeps the old domain. Re-add the equality check so the topology
    # cannot ship a self-contradictory ``animation_drivers[*].domain``
    # vs ``modules[driver_guid].domain`` pair. On the fresh-build path
    # the check remains tautological (no perf cost beyond a dict
    # lookup).
    for stable_id, anim in animation_drivers.items():
        status = anim.get("routing_status", "")
        if status != "resolved":
            # ``unresolved`` / ``orphan`` rows skip both invariants by
            # design. The status field is itself the audit trail —
            # absence of "resolved" means downstream consumers should
            # treat the placement as best-effort.
            continue
        driver_guid = anim.get("driver_module_guid", "")
        if not driver_guid or driver_guid not in modules_block:
            _abort(
                1,
                f"resolved animation driver {stable_id!r} references unknown "
                f"module guid {driver_guid!r}",
                row=anim,
            )
        driver_module = modules_block[driver_guid]
        driver_domain = driver_module.get("domain", "")
        if driver_domain not in _VALID_RUNTIME_DOMAINS:
            _abort(
                6,
                f"resolved animation driver {stable_id!r}'s module "
                f"{driver_guid!r} has non-runtime domain {driver_domain!r} "
                f"(must be client or server)",
                row=anim,
            )
        anim_domain = anim.get("domain", "")
        if anim_domain != driver_domain:
            _abort(
                1,
                f"resolved animation driver {stable_id!r} has "
                f"domain={anim_domain!r} but its driver module "
                f"{driver_guid!r} now has domain={driver_domain!r} "
                f"— stale preserved animation_drivers on a build "
                f"whose classifier output changed. Re-run from "
                f"convert_animations or clear the cached topology "
                f"to refresh the routing.",
                row=anim,
            )

    # Invariant 2: every edge has producer + consumer with defined
    # runtime domains. ``compute_cross_domain_edges`` already filters out
    # non-runtime domains, so a violation here means the function's
    # contract slipped — defense-in-depth.
    for edge in edges:
        from_domain = edge.get("from_domain", "")
        to_domain = edge.get("to_domain", "")
        if (from_domain not in _VALID_RUNTIME_DOMAINS
                or to_domain not in _VALID_RUNTIME_DOMAINS):
            _abort(
                2,
                f"cross-domain edge has non-runtime domain "
                f"(from={from_domain!r}, to={to_domain!r})",
                row=edge,
            )

    # Invariant 2a (Phase 2b): no SEMANTIC collision on
    # ``resolution.event_name`` across the component-ref ``edges``
    # bucket. A "semantic collision" is two edges sharing the same
    # ``event_name`` but with DIFFERENT ``payload.attribute_name`` (or
    # ``kind``) — i.e. two distinct cross-domain writes that would route
    # through the same RemoteEvent at emit time.
    #
    # Reframe (2026-06-01): the retired ``cross_domain_edge_candidates``
    # bucket is no longer scanned here (it was Class-2 mis-modeling). The
    # check is scoped to ``cross_domain_edges`` only.
    #
    # Intentional reuse is allowed: multiple cross-domain ``Door.open``
    # component-ref edges all derive ``"Door_SetOpen"`` from
    # ``<owner>_Set<Field>`` without an instance qualifier — the SAME
    # logical bridge instantiated multiple times, not a collision. The
    # check groups rows by ``event_name`` and only fires when a group
    # has heterogeneous ``payload.attribute_name`` (or ``kind``).
    by_event_name: dict[str, list[dict[str, object]]] = {}
    for row in edges:
        resolution = row.get("resolution", {})
        if not isinstance(resolution, dict):
            continue
        ev = resolution.get("event_name", "")
        if not isinstance(ev, str) or not ev:
            continue
        by_event_name.setdefault(ev, []).append(cast("dict[str, object]", row))
    for ev, rows in by_event_name.items():
        if len(rows) <= 1:
            continue
        # Defensive depth: every row in the group must share BOTH
        # ``payload.attribute_name`` and ``kind``. A mismatch on
        # either is a true semantic collision.
        attr_names: set[str] = set()
        kinds: set[str] = set()
        for r in rows:
            payload = r.get("payload", {})
            if isinstance(payload, dict):
                an = payload.get("attribute_name", "")
                if isinstance(an, str):
                    attr_names.add(an)
            k = r.get("kind", "")
            if isinstance(k, str):
                kinds.add(k)
        if len(attr_names) > 1 or len(kinds) > 1:
            _abort(
                2,
                f"semantic collision on cross-domain event_name {ev!r}: "
                f"two distinct cross-domain writes (different "
                f"attribute_name={sorted(attr_names)!r} or "
                f"kind={sorted(kinds)!r}) share event_name -- this "
                f"is a name collision, not intentional reuse. The "
                f"``<owner>_Set<Field>`` derivation may have collided "
                f"with another edge's name by coincidence.",
                row=rows[0],
            )

    # Invariant 2c (Phase 2b reframe): the shared-flag channel fact, when
    # ``present``, must carry a non-empty ``canonical_stores`` (the
    # funnel always writes Player + Character per autogen.py:174-176). A
    # ``present`` channel with no canonical store would let the step-2
    # gate inject a funnel that writes nowhere. Cheap + meaningful.
    shared_flag_channels = artifact.get("shared_flag_channels", {})
    if isinstance(shared_flag_channels, dict):
        for ev_name, channel in shared_flag_channels.items():
            if not isinstance(channel, dict):
                continue
            if channel.get("present", False) and not channel.get(
                "canonical_stores",
            ):
                _abort(
                    2,
                    f"shared_flag_channels[{ev_name!r}] is present=True "
                    f"but has empty canonical_stores — the funnel would "
                    f"write nowhere",
                    row=channel,
                )

    # Invariant 3: every emitted Anim_* script has exactly one
    # animation_drivers entry. We check both directions: each emission
    # must map to a stable_id present in animation_drivers (no script
    # without an entry), and each animation_drivers key must originate
    # from an emission (no spurious entries).
    #
    # Skipped on the preserved-drivers resume path: we don't have
    # original emissions to cross-check (see ``skip_invariant_3``).
    # Invariants 4-8 below still run — they validate the preserved
    # block's internal shape, which guards against on-disk tampering.
    if not skip_invariant_3:
        expected_stable_ids: dict[str, str] = {}
        for row in emitted_animations:
            # Mirror the keying choice in _build_animation_drivers_block:
            # scope_ref (planner-stable) for the segment, ORPHAN_SCOPE
            # sentinel when empty.
            scope_ref = row.get("scope_ref", "")
            scope_segment = scope_ref if scope_ref else ORPHAN_SCOPE
            sid = compute_stable_id(
                scope_segment,
                row.get("ctrl_key", "") or None,
                row.get("clip_disp", ""),
            )
            if sid in expected_stable_ids:
                _abort(
                    3,
                    f"two emitted animations collide on stable_id {sid!r}: "
                    f"{expected_stable_ids[sid]!r} and {row.get('script_name', '')!r} "
                    f"— upstream disambiguator failed",
                    row=row,
                )
            expected_stable_ids[sid] = row.get("script_name", "")
        for sid in animation_drivers:
            if sid not in expected_stable_ids:
                _abort(
                    3,
                    f"animation driver {sid!r} has no corresponding emission",
                    row=animation_drivers[sid],
                )
        for sid, script_name in expected_stable_ids.items():
            if sid not in animation_drivers:
                _abort(
                    3,
                    f"emitted animation {script_name!r} has no driver entry "
                    f"(stable_id {sid!r})",
                    row={"script_name": script_name, "stable_id": sid},
                )

    # Invariant 4: every lifecycle_role in the closed enum. The Literal
    # type makes this true at the type system level; we still check at
    # runtime to catch external-provenance data (on-disk plans from
    # outside the current process).
    valid_roles = set(LIFECYCLE_ROLES)
    for guid, entry in modules_block.items():
        role = entry.get("lifecycle_role", "")
        if role not in valid_roles:
            _abort(
                4,
                f"module {guid!r} has lifecycle_role {role!r} not in "
                f"closed enum {sorted(valid_roles)!r}",
                row=entry,
            )
    for sid, entry in animation_drivers.items():
        role = entry.get("lifecycle_role", "")
        if role not in valid_roles:
            _abort(
                4,
                f"animation driver {sid!r} has lifecycle_role {role!r} not in "
                f"closed enum {sorted(valid_roles)!r}",
                row=entry,
            )

    # Invariant 5: every bridge_group_id refers to an existing edge id.
    edge_ids = {edge.get("id", "") for edge in edges}
    for guid, entry in modules_block.items():
        bgid = entry.get("bridge_group_id", None)
        if bgid is not None and bgid not in edge_ids:
            _abort(
                5,
                f"module {guid!r} has bridge_group_id {bgid!r} not found in "
                f"cross_domain_edges ids {sorted(edge_ids)!r}",
                row=entry,
            )
    for sid, entry in animation_drivers.items():
        bgid = entry.get("bridge_group_id", None)
        if bgid is not None and bgid not in edge_ids:
            _abort(
                5,
                f"animation driver {sid!r} has bridge_group_id {bgid!r} not "
                f"found in cross_domain_edges ids {sorted(edge_ids)!r}",
                row=entry,
            )

    # Invariant 8 (Phase 2a slice 2 round 3): lifecycle_role
    # consistent with gated inputs. One-way implication: if the role
    # is "loader" or "character_attached", the inputs that produced it
    # must satisfy the gates inside derive_module_lifecycle_role.
    # The OPPOSITE direction is NOT enforced — `is_loader=True` may
    # coexist with `lifecycle_role != "loader"` when a gate fired
    # (this is the deliberate raw-hint-vs-gated-decision divergence
    # documented on TopologyModuleEntry).
    for guid, mod_entry in modules_block.items():
        role = mod_entry.get("lifecycle_role", "")
        if role == "loader":
            if not mod_entry.get("is_loader", False):
                _abort(
                    8,
                    f"module {guid!r} has lifecycle_role='loader' but "
                    f"is_loader=False — derive_module_lifecycle_role's "
                    f"loader branch requires is_loader=True",
                    row=mod_entry,
                )
            sc = mod_entry.get("script_class", "")
            if sc not in ("Script", "LocalScript"):
                _abort(
                    8,
                    f"module {guid!r} has lifecycle_role='loader' but "
                    f"script_class={sc!r} — only Script/LocalScript "
                    f"can be loaders",
                    row=mod_entry,
                )
            if mod_entry.get("domain", "") != "client":
                _abort(
                    8,
                    f"module {guid!r} has lifecycle_role='loader' but "
                    f"domain={mod_entry.get('domain', '')!r} — "
                    f"loaders are always client-domain",
                    row=mod_entry,
                )
        elif role == "character_attached":
            if not mod_entry.get("character_attached", False):
                _abort(
                    8,
                    f"module {guid!r} has "
                    f"lifecycle_role='character_attached' but "
                    f"character_attached=False",
                    row=mod_entry,
                )
            if mod_entry.get("domain", "") != "client":
                _abort(
                    8,
                    f"module {guid!r} has "
                    f"lifecycle_role='character_attached' but "
                    f"domain={mod_entry.get('domain', '')!r} — "
                    f"character-attached scripts are always "
                    f"client-domain",
                    row=mod_entry,
                )

    # Invariant 7 (Phase 2a slice 2): every runtime-bearing planner row
    # must carry both `character_attached` and `is_loader` booleans.
    # Reads the PLANNER input (scene_runtime["modules"]) rather than the
    # topology output because the goal is to catch a planner artifact
    # that came in WITHOUT these fields — _build_modules_block defaults
    # them to False on read, so a check against the output is
    # tautological. The fail-closed case is an on-disk plan from a
    # pre-slice-2 converter version, or a test fixture that hand-rolls
    # `scene_runtime["modules"]` rows without going through the planner.
    planner_modules = cast(
        dict[str, dict[str, object]], scene_runtime.get("modules", {}),
    )
    for script_id, planner_module in planner_modules.items():
        if not bool(planner_module.get("runtime_bearing", False)):
            # Helpers and non-instance-backed rows don't have a
            # lifecycle role to derive, so the inputs aren't required.
            continue
        ca = planner_module.get("character_attached", None)
        if not isinstance(ca, bool):
            _abort(
                7,
                f"runtime-bearing module {script_id!r} is missing "
                f"`character_attached: bool` on the planner row (got "
                f"{ca!r}); Phase 2a slice 2 requires every runtime_bearing "
                f"row to carry it",
                row=planner_module,
            )
        il = planner_module.get("is_loader", None)
        if not isinstance(il, bool):
            _abort(
                7,
                f"runtime-bearing module {script_id!r} is missing "
                f"`is_loader: bool` on the planner row (got {il!r}); "
                f"Phase 2a slice 2 requires every runtime_bearing row "
                f"to carry it",
                row=planner_module,
            )

    # Invariant 9 lives in `_detect_caller_graph_collisions` (called
    # pre-derivation from `build_topology`). It's an INPUT validator,
    # not an output one — keeping it here would let a future producer
    # added between derivation and output validation leak the lossy
    # data before invariant 9 fires (Claude review slice 3 round 2 P1).

    # Invariant 10 (Phase 2a slice 4; narrowed by slice 9b):
    # reachability ``module_path`` ↔ container coherence. The
    # planner's ``_apply_reachability_rule`` rewrites ``container`` +
    # ``module_path`` together (codex P1.1 atomicity); this invariant
    # enforces the mirrored coherence on the topology entry. Slice
    # 9b dropped the prior ``reachability_forced_container`` lockstep
    # arm — same-loop-sets-both-fields-from-same-source made it
    # tautological and the field itself was removed (no production
    # branch read it).
    for guid, mod_entry in modules_block.items():
        required = mod_entry.get("reachability_required_container", "")
        module_path_v = mod_entry.get("module_path", "")
        # Slice 4 round 1 review (Claude P1.2): accept BOTH
        # ``module_path == required`` (the container itself, no
        # module suffix — e.g. a top-level container row) AND
        # ``module_path.startswith(f"{required}.")`` (the strict
        # child case). The pre-fix `startswith(f"{required}.")`
        # rejected the legitimate exact-match case AND a bare
        # `startswith(required)` would false-positive on a
        # sibling-container prefix like ``ReplicatedStorageOther.X``.
        if required and module_path_v != required and not (
            module_path_v.startswith(f"{required}.")
        ):
            _abort(
                10,
                f"module {guid!r} has reachability_required_container="
                f"{required!r} but module_path={module_path_v!r} does "
                f"not equal the container nor start with that "
                f"container plus a dot — the planner's codex P1.1 fix "
                f"rewrites module_path together with the rule's "
                f"container so the host's resolveModule call lands at "
                f"the actually-hoisted location",
                row=mod_entry,
            )


__all__ = (
    "EmittedAnimation",
    "TopologyArtifact",
    "TopologyInvariantError",
    "TopologyModuleEntry",
    "TopologyProvenance",
    "build_topology",
    "callers_of",
    "resolve_caller_graph",
)
