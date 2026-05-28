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
Coherence policy (Phase 2a slice 3 round 2) — NOT a fail-closed
invariant: when ``scene_runtime.modules`` contains a ``class_name``
collision (two scripts sharing a class_name) AND that name is
touched by ``dependency_map``, ``_detect_caller_graph_collisions``
excludes it from ``caller_graph`` translation. The colliding scripts
appear as orphan rows (no callers) in the curated view; slice 5's
decision tree routes them to ReplicatedStorage per its orphan-module
rule. A WARNING is logged per collision. The legacy pipeline
silently mis-routed these projects pre-slice-3; this policy keeps
them converting while preventing slice 5 from receiving lossy edges.
The deep fix (promote dependency_map's keyspace to script_id) lives
in a future slice.
"""

from __future__ import annotations

import logging
from typing import TypedDict, cast

from core.roblox_types import RbxScript
from core.unity_types import GuidIndex

from converter.scene_runtime_planner import SceneRuntimeArtifact
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
from converter.scene_runtime_topology.lifecycle_roles import (
    LIFECYCLE_ROLES,
    LifecycleRole,
    derive_module_lifecycle_role,
)

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
    cross_domain_edges: list[CrossDomainEdge]
    caller_graph: dict[str, list[str]]


# ---------------------------------------------------------------------------
# Invariant errors
# ---------------------------------------------------------------------------

class TopologyInvariantError(RuntimeError):
    """Raised when build_topology detects a topology artifact that
    violates one of the 6 emit-time invariants. The message includes
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
) -> TopologyArtifact:
    """Assemble the topology artifact + enforce 6 emit-time invariants.

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

    Returns the artifact dict ready to merge into
    ``scene_runtime["topology"]``.

    Aborts (raises ``TopologyInvariantError``) on any invariant
    violation. No warnings, no soft-fails — the design doc commits to
    fail-closed on emit.
    """
    # Detect class_name collisions in scene_runtime.modules that are
    # touched by dependency_map BEFORE block-building. Codex slice 3
    # round 2 P1 flagged the prior fail-closed invariant 9 as a
    # regression for projects with two scripts sharing a class_name
    # (the legacy pipeline silently processes these — lossy but
    # ships). The structural choice here is DEGRADED SERVICE: detect
    # the collisions, log a structured warning, and let
    # `_build_caller_graph_block` exclude those class_names entirely
    # from the curated view. Slice 5's consumer sees partial-but-
    # truthful data for the colliding scripts (no callers known →
    # ReplicatedStorage fallback per `_decide_script_container`'s
    # orphan-module rule), never lossy mis-routed data.
    #
    # The deep fix is to key dependency_map by script_id at planner
    # construction (eliminates the keyspace ambiguity entirely);
    # tracked for a future slice.
    colliding_classes = _detect_caller_graph_collisions(
        scene_runtime, dependency_map,
    )

    modules_block = _build_modules_block(
        scene_runtime, scripts_by_class, guid_index,
    )
    edges_block = compute_cross_domain_edges(scene_runtime)
    animation_drivers_block = _build_animation_drivers_block(
        scene_runtime, emitted_animations,
    )
    caller_graph_block = _build_caller_graph_block(
        scene_runtime, dependency_map, excluded_class_names=colliding_classes,
    )

    artifact: TopologyArtifact = {
        "modules": modules_block,
        "animation_drivers": animation_drivers_block,
        "cross_domain_edges": edges_block,
        "caller_graph": caller_graph_block,
    }

    # Invariants run AFTER assembly so they can cross-reference blocks.
    # (Invariant 9 is the exception — it's an INPUT validator and ran
    # before block-building via _validate_caller_graph_inputs.)
    _enforce_invariants(
        artifact,
        emitted_animations=emitted_animations,
        scene_runtime=scene_runtime,
    )
    return artifact


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------

def _build_modules_block(
    scene_runtime: SceneRuntimeArtifact,
    scripts_by_class: dict[str, RbxScript],
    guid_index: GuidIndex | None,
) -> dict[str, TopologyModuleEntry]:
    """One TopologyModuleEntry per ``scene_runtime.modules`` row.

    ``script_class`` reads off ``RbxScript.script_type`` when an emitted
    script exists for the module's class_name. Helpers without an
    emitted script (the planner records them but storage_classifier
    didn't synthesize a body) default to ``"ModuleScript"`` — they're
    require-target shape.

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

        script = scripts_by_class.get(class_name)
        if script is not None and script.script_type:
            script_class = script.script_type
        else:
            script_class = "ModuleScript"

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

        entry: TopologyModuleEntry = {
            "stem": stem,
            "domain": domain,
            "script_class": script_class,
            "lifecycle_role": lifecycle_role,
            "character_attached": character_attached,
            "is_loader": is_loader,
            "bridge_group_id": None,
            "provenance": provenance,
        }
        out[script_id] = entry
    return out


def _build_animation_drivers_block(
    scene_runtime: SceneRuntimeArtifact,
    emitted_animations: list[EmittedAnimation],
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

        observed_attribute = row.get("observed_attribute", "")
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
    scene_runtime: SceneRuntimeArtifact,
    dependency_map: dict[str, list[str]] | None,
    *,
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
    """
    if not dependency_map:
        return {}

    modules_in = cast(
        dict[str, dict[str, object]], scene_runtime.get("modules", {}),
    )
    class_to_script_id: dict[str, str] = {}
    for script_id, module in modules_in.items():
        class_name_obj = module.get("class_name", "")
        if not isinstance(class_name_obj, str) or not class_name_obj:
            continue
        if class_name_obj in excluded_class_names:
            # Colliding class names are excluded from the translation
            # index — degraded-service contract. The scripts still
            # appear in the modules block (built independently);
            # caller_graph just doesn't reference them.
            continue
        # First-write wins among non-colliding rows — consistent with
        # `_build_modules_block`'s `scripts_by_class.setdefault`
        # policy. (For non-colliding class names there's only one
        # script_id, so first-write and last-write are the same.)
        class_to_script_id.setdefault(class_name_obj, script_id)

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
) -> frozenset[str]:
    """Detect ``class_name`` collisions in ``scene_runtime.modules``
    that are touched by ``dependency_map``. Returns the colliding
    class_names so the caller_graph builder can EXCLUDE them from
    translation (degraded-service contract — see ``build_topology``
    docstring for rationale).

    Logs a structured WARNING listing each collision: class_name +
    the competing script_ids + an explicit "promote dependency_map's
    keyspace to script_id" remediation hint. Operators see the
    warning in the build log; the conversion proceeds with
    partial-but-truthful caller_graph data for the colliding scripts
    (they appear in the graph as ZERO-caller modules — slice 5's
    decision tree falls back to ReplicatedStorage, the orphan-module
    rule's safe default).

    Lives OUTSIDE ``_enforce_invariants`` (which validates artifact
    outputs) because this check operates on INPUTS and informs the
    derivation. Running it pre-derivation keeps the producer
    structurally aware of the excluded class_names (Claude review
    slice 3 round 2 P1).

    Empty return when ``dependency_map`` is None/empty or no
    collisions exist.
    """
    if not dependency_map:
        return frozenset()
    modules_in = cast(
        dict[str, dict[str, object]], scene_runtime.get("modules", {}),
    )
    class_to_script_ids: dict[str, list[str]] = {}
    for script_id, module in modules_in.items():
        cn_obj = module.get("class_name", "")
        if isinstance(cn_obj, str) and cn_obj:
            class_to_script_ids.setdefault(cn_obj, []).append(script_id)

    # Find class_names with > 1 script_ids AND touched by dep_map.
    touched_classes: set[str] = set(dependency_map.keys())
    for callees in dependency_map.values():
        for c in callees:
            if isinstance(c, str):
                touched_classes.add(c)

    colliding: set[str] = set()
    for cn, sids in class_to_script_ids.items():
        if len(sids) <= 1:
            continue
        if cn not in touched_classes:
            continue
        colliding.add(cn)

    if colliding:
        # One log entry per collision so operators can grep + count.
        for cn in sorted(colliding):
            sids = sorted(class_to_script_ids[cn])
            log.warning(
                "[scene_runtime_topology] class_name %r maps to %d "
                "script_ids %r AND appears in dependency_map; "
                "excluding from caller_graph (lossy class_name "
                "keyspace). slice 5 consumer will see these as "
                "orphan modules and route to ReplicatedStorage. "
                "Promote dependency_map's keyspace to script_id at "
                "planner construction or split the colliding class "
                "to restore precise routing.",
                cn, len(sids), sids,
            )

    return frozenset(colliding)


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------

_VALID_RUNTIME_DOMAINS: frozenset[str] = frozenset({"client", "server"})


def _enforce_invariants(
    artifact: TopologyArtifact,
    *,
    emitted_animations: list[EmittedAnimation],
    scene_runtime: SceneRuntimeArtifact,
) -> None:
    """Apply the post-derivation emit-time invariants. Raises on any
    violation.

    Each invariant is a single ``for`` loop with a clear failure
    message. The errors are catchable (``TopologyInvariantError``) so
    test code can assert specific invariant numbers without scraping
    log output.

    Invariant 9 (the input-collision check) lives in
    ``_validate_caller_graph_inputs`` and runs pre-derivation, NOT
    here — see that helper's docstring for rationale.
    """
    modules_block = artifact.get("modules", {})
    animation_drivers = artifact.get("animation_drivers", {})
    edges = artifact.get("cross_domain_edges", [])

    # Invariant 1 + 6: applied ONLY to resolved entries. Unresolved /
    # orphan entries have empty ``driver_module_guid`` by design (Phase
    # 1 narrowing limitation / deliberate orphan) and skip both checks.
    # The tautological "anim_domain == driver_domain" check from the
    # earlier draft is dropped — the entry's domain is built FROM the
    # driver's domain in ``_build_animation_drivers_block``, so the
    # equality could never fail by construction (subagent finding #1).
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

    # Invariant 3: every emitted Anim_* script has exactly one
    # animation_drivers entry. We check both directions: each emission
    # must map to a stable_id present in animation_drivers (no script
    # without an entry), and each animation_drivers key must originate
    # from an emission (no spurious entries).
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

    # Invariant 9 lives in `_validate_caller_graph_inputs` (called
    # pre-derivation from `build_topology`). It's an INPUT validator,
    # not an output one — keeping it here would let a future producer
    # added between derivation and output validation leak the lossy
    # data before invariant 9 fires (Claude review slice 3 round 2 P1).


__all__ = (
    "EmittedAnimation",
    "TopologyArtifact",
    "TopologyInvariantError",
    "TopologyModuleEntry",
    "TopologyProvenance",
    "build_topology",
    "callers_of",
)
