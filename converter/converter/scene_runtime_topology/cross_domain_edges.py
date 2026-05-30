"""cross_domain_edges — enumerate every cross-domain edge candidate in
the plan.

Relocated in Phase 1 from ``scene_runtime_domain.compute_cross_domain_edges``.
Schema extended with the design doc's ``id`` field (the
``deterministic_edge_id`` used as ``bridge_group_id`` by Phase 2b
consumers). The id is populated deterministically in Phase 1 so Phase 2b
can read it without a schema migration.

Phase 2b slice 1 extends this further with:
  - ``kind: "attribute_write"`` (closed-enum discriminator; today's only
    value, but slice 2+ may add more).
  - ``resolution`` (strategy + event_name): the resolution metadata Phase
    2b's bridge emitter (slice 3) consumes. Slice 1 produces the
    structural ``resolution.strategy`` (always ``"remote_event_bridge"``
    for slice 1's outputs) and a stable ``resolution.event_name`` derived
    from owner+field; slice 2's enrichment pass may revise the event_name
    or downgrade strategy to ``"same_domain_no_bridge"``.
  - ``bridge_member_scripts`` (list, EMPTY in slice 1): slice 2's
    enrichment populates this with the 4-script bridge unit (client
    caller, server listener, RemoteEvent, anim listener) per design doc.
  - ``payload`` (attribute_name + schema): the payload the bridge
    transmits. ``attribute_name`` is the field name (component-ref
    edges) or a per-instance template (shared-attribute candidates that
    resolve to a dynamic per-pickup attribute at slice 3 emit time).

Slice 1 also introduces a SECOND producer:
``compute_shared_attribute_candidates``. Walks scene/prefab instances
and, for each instance whose component class matches a seed in
``SHARED_ATTRIBUTE_SEEDS``, emits a candidate edge. This is the
structural pre-transpile equivalent of today's hardcoded
``PlayerSetSharedFlag`` prompt block at ``code_transpiler.py:1267-1288``
— lifted to a data table so the bridge emitter (slice 3) can consume it
instead of the prompt embedding the RemoteEvent name forever.

R1 wiring (codex P1, 2026-05-30): ``build_topology`` routes the two
producers to SEPARATE artifact buckets, not a concatenated list:

  - ``compute_cross_domain_edges`` outputs land in
    ``artifact["cross_domain_edges"]`` — fully resolved, every row has a
    runtime ``from_domain`` and ``to_domain`` and passes
    ``_enforce_invariants`` invariant 2.
  - ``compute_shared_attribute_candidates`` outputs land in
    ``artifact["cross_domain_edge_candidates"]`` — fan-out shape with
    empty ``to_*`` until slice 2 enrichment resolves consumers and
    populates ``bridge_member_scripts``. Invariant 2 does NOT iterate
    this bucket.

Slice 2 enrichment reads from ``cross_domain_edge_candidates``, resolves
domains + bridge members, and either promotes rows to
``cross_domain_edges`` or keeps the two-bucket separation indefinitely
(slice 2 decides). The two functions in this module remain pure producers
with no awareness of the artifact buckets — wiring lives in
``build_topology``.

The CrossDomainEdge schema is intentionally KEPT FLAT (no nested
``producer{}/consumer{}`` sub-objects) for slice 1. The design doc's
example (L228-251) shows a nested shape; that restructure is deferred
indefinitely — every consumer reads ``from_*`` / ``to_*`` today, and
restructuring is scope creep slice 1's brief explicitly excludes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypedDict, cast

from converter.scene_runtime_planner import (
    SceneRuntimeArtifact,
    SceneRuntimeReference,
)


class BridgeMember(TypedDict):
    """One script (or RemoteEvent) participating in a bridge.

    ``role`` values are a CLOSED enum (per slice 2 R2, 2026-05-31):
    ``client_caller``, ``server_caller``, ``client_listener``,
    ``server_listener``, ``anim_listener``, ``consumer``. Slice 3 may
    add ``remote_event`` (reserved -- see end of this docstring).

    Direction-aware caller/listener pairing (slice 2 R2):
      - ``"client_caller"`` + ``"server_listener"``: a client-originated
        bridge (``from_domain == "client"``). Slice 3's rewriter
        replaces the producer's ``SetAttribute`` with
        ``<event_name>:FireServer(target, v)``; the synthesized
        listener subscribes via ``OnServerEvent``.
      - ``"server_caller"`` + ``"client_listener"``: a server-originated
        bridge (``from_domain == "server"`` -- the locked Pickup
        candidate lives here, matching the
        ``pickup_remote_event_server`` pack contract at
        ``script_coherence_packs.py:380-394``). Slice 3 rewrites to
        ``<event_name>:FireClient(target, v)`` or
        ``:FireAllClients(v)``; the listener subscribes via
        ``OnClientEvent``.

    Per-role contracts:
      - ``"client_caller"`` / ``"server_caller"``: the script_id whose
        ``SetAttribute`` slice 3 will rewrite. For a component-ref edge
        this is ``edge["from_script"]``; for a shared-attribute
        candidate this is the producer-class script_id.
      - ``"server_listener"`` / ``"client_listener"``: a SYNTHESIZED
        script_id (NOT a real module row id) for the listener slice
        3 will emit. Derived via
        ``bridge_emit.synthesize_listener_id(event_name,
        direction=...)`` so the id slice 2 writes here and the id
        slice 3 stamps on the emitted ``RbxScript`` agree by
        construction. The candidate-`ref`-validity invariant in
        ``build_topology._enforce_invariants`` recognizes BOTH
        per-direction prefixes.
      - ``"anim_listener"``: for component-ref edges only -- the
        receiver script that consumes the bridged attribute. Equals
        ``edge["to_script"]``. Direction-independent (the consumer
        script is the same regardless of which side fires). Fan-out
        (shared-attribute) candidates DO NOT carry an
        ``anim_listener`` (consumers are dynamic at runtime via
        ``GetAttributeChangedSignal``; slice 2's Luau-scan pass
        discovers any STATIC readers and emits them under the
        ``"consumer"`` role).
      - ``"consumer"``: for shared-attribute candidates only -- one
        per ``script_id`` whose post-transpile Luau source reads the
        bridged attribute via ``:GetAttribute("has...")``. Slice 2's
        Luau-scan pass emits one row per matched script. Empty on
        the resume path (``state.transpilation_result is None``) --
        slice 3 falls back to broadcast emission when this list is
        empty.

    ``ref`` is the consumer-readable identifier the bridge emitter
    (slice 3) will dereference: a script_id for caller / anim_listener
    / consumer roles, a synthesized id for the listener role.
    ``remote_event`` role is reserved for slice 3's emitter to
    optionally stamp the dotted DataModel path of the synthesized
    ``RemoteEvent`` -- slice 2 does not emit ``remote_event`` rows
    (the path is fully derivable from ``event_name`` at slice 3 emit
    time).
    """

    role: str
    ref: str


class ResolutionSpec(TypedDict):
    """How the bridge emitter should resolve the producer's write.

    ``strategy``:
      - ``"remote_event_bridge"``: the emitter (slice 3) rewrites the
        producer's ``SetAttribute`` to ``<event_name>:FireServer(target,
        v)`` and synthesizes a server listener.
      - ``"same_domain_no_bridge"``: producer + consumer share a domain;
        the bridge is unnecessary. Slice 2 may downgrade an edge to this
        once enrichment confirms domains.
      - ``"excluded"``: the edge is structurally cross-domain but the
        operator opted out (or the consumer is itself non-runtime). The
        emitter skips these.

    ``event_name`` is the RemoteEvent name the producer fires and the
    listener subscribes to. For component-ref edges, slice 1 derives it
    deterministically from ``<owner>_Set<Field>``. For shared-attribute
    candidates, slice 1 sets it from the seed table (LOCKED — see
    ``PickupItemEvent`` in ``SHARED_ATTRIBUTE_SEEDS``).
    """

    strategy: Literal["remote_event_bridge", "same_domain_no_bridge", "excluded"]
    event_name: str


class PayloadSpec(TypedDict):
    """What the bridge transmits.

    ``attribute_name`` is the Roblox Instance Attribute the listener
    writes on the target. Component-ref edges set this to the C# field
    name verbatim. Shared-attribute candidates set it to a TEMPLATE
    (e.g. ``"has<itemName>"``) the bridge emitter (slice 3) resolves
    per-instance at emit time — slice 1 does NOT resolve the template
    because the producer pass doesn't know which instance the producer
    is writing at runtime.

    ``schema``:
      - ``"unknown"``: slice 1's default for component-ref edges; slice
        2 may sharpen via type analysis.
      - ``"bool"``: the slice 1 default for shared-attribute candidates,
        matching the hardcoded prompt at ``code_transpiler.py:1279``
        (``_plr:SetAttribute(_flag, true)``).
    """

    attribute_name: str
    schema: str


class CrossDomainEdge(TypedDict):
    """One cross-domain attribute-write edge identified at conversion
    time.

    ``id`` is added in Phase 1 (see module docstring). The original 10
    flat fields (``from_*``, ``to_*``, ``field``, ``owner_*``) are
    byte-stable with the pre-Phase-1 CrossDomainEdge so on-disk plans +
    the cross-domain report writer keep working.

    Phase 2b slice 1 adds ``kind`` / ``resolution`` /
    ``bridge_member_scripts`` / ``payload`` per the design doc Path C
    architecture. The producer/consumer restructure (nested objects per
    design doc L232-235) is intentionally NOT done — slice 1's brief
    excludes it.
    """

    id: str
    kind: Literal["attribute_write"]
    from_instance: str
    to_instance: str
    from_script: str
    to_script: str
    field: str
    from_domain: str
    to_domain: str
    owner_kind: str
    owner_ref: str
    resolution: ResolutionSpec
    bridge_member_scripts: list[BridgeMember]
    payload: PayloadSpec


# Domain values the cross-domain pass refuses to wire through.
# - Helpers don't run lifecycle, so they can't generate cross-domain refs.
# - Excluded modules aren't instantiated at all.
# - ``"legacy"`` is the pre-classifier-v2 spelling; preserved here as a
#   defensive skip for any on-disk plan that the migration pass hasn't
#   already rewritten.
NON_RUNTIME_DOMAINS: frozenset[str] = frozenset(
    {"", "helper", "excluded", "legacy"},
)


@dataclass(frozen=True)
class SharedAttributeSeed:
    """One canonical shared-attribute pattern the converter knows how to
    bridge.

    ``producer_class_name``: the Unity component class whose lifecycle
    writes the shared attribute. Matched against
    ``modules[script_id].class_name`` — instances whose ``script_id``
    resolves to a module with this class name are seeded.

    ``remote_event_name``: the RemoteEvent name the bridge fires.
    LOCKED for ``Pickup`` — see Mitigation α in slice 1's brief and the
    locked-name regression guard in test_pickup_item_event_name_locked.
    Three downstream sites in ``script_coherence_packs.py`` hardcode
    this literal string; Phase 2b deliberately does NOT migrate them
    because the bridge emitter (slice 3) re-produces the same byte
    shape.

    ``attribute_template``: the per-instance attribute name shape. For
    ``Pickup``, this is ``"has<itemName>"`` (literal template string;
    slice 3 resolves ``<itemName>`` per-instance at emit time from the
    instance's ``itemName`` config field). Captured as a raw template
    here because the producer pass walks the SCENE — it does not know
    the per-instance ``itemName`` config without re-resolving every
    instance, which would invert the candidate enumeration cost.
    """

    producer_class_name: str
    remote_event_name: str
    attribute_template: str


# Phase 2b slice 1: structural pre-transpile seed table. The table seeds
# the structural candidate pass so slice 3's bridge emitter can consume
# data, not regrep the prompt forever.
#
# SCOPE — narrower than the prompt it eventually replaces (Codex R2,
# 2026-05-30). This table matches the EXISTING
# ``pickup_remote_event_server`` pack's class-name detection scope
# (``Pickup`` only), NOT the broader prompt scope at
# ``code_transpiler.py:1271-1289``. That prompt is UNBOUNDED — it
# instructs the AI to fire ``PlayerSetSharedFlag`` for ANY MonoBehaviour
# writing shared state (``GetItem(itemName)``-style methods,
# ``RecoverHealth``, ``gotWeapon = true``, etc.). The slice 1 seed
# faithfully covers the one structural case the existing pack already
# handles; it does NOT cover the long tail the prompt covers.
#
# WARNING for slice 3 (Codex R2, 2026-05-30): before slice 3 deletes
# the hardcoded ``_GENERIC_RUNTIME_PROMPT`` ``PlayerSetSharedFlag``
# block, slice 3 must decide between:
#   (a) Require this table to be COMPREHENSIVE before deleting the
#       prompt path — i.e. statically enumerate every MonoBehaviour
#       class that writes shared player state across the converter's
#       supported corpus, and add a seed row per class. High up-front
#       cost; fully data-driven afterwards.
#   (b) Keep a FALLBACK that scans AI output for
#       ``PlayerSetSharedFlag:FireServer`` calls when no topology
#       candidate covers the producing script. Lower cost; preserves
#       coverage for non-Pickup shared-flag writers (e.g. a ``Player.cs``
#       controller that records ``has<X>`` without routing through a
#       Pickup instance) that the slice 1 seed misses.
# Deleting the prompt path without picking one regresses any non-Pickup
# shared-flag writer in real projects.
#
# IMPORTANT — Mitigation α (LOCKED PickupItemEvent): the Pickup seed's
# ``remote_event_name`` MUST stay the literal ``"PickupItemEvent"``.
# Three downstream sites continue to hardcode this string through Phase
# 2b:
#   - ``pickup_remote_event_client`` regex at
#     ``script_coherence_packs.py:780-783``
#   - ``pickup_visual_target`` template at
#     ``script_coherence_packs.py:1167-1189``
#   - listener pack at ``script_coherence_packs.py:1032-1079``
# Changing this string here breaks all three. The design doc explicitly
# locks it (Phase 2b deliverable 4).
SHARED_ATTRIBUTE_SEEDS: tuple[SharedAttributeSeed, ...] = (
    SharedAttributeSeed(
        producer_class_name="Pickup",
        remote_event_name="PickupItemEvent",
        attribute_template="has<itemName>",
    ),
)


def deterministic_edge_id(
    from_instance: str, field: str, to_instance: str,
) -> str:
    """Stable, debuggable edge id built from the 3 fields that already
    uniquely identify the edge (from_instance + field + to_instance).

    Phase 2b consumes this as ``bridge_group_id``. The format is
    intentionally human-readable — debugging a misrouted bridge means
    eyeballing edge ids in dumps, not decoding a SHA prefix.

    Two distinct edges sharing all three fields ARE the same edge by
    definition (one MonoBehaviour's one serialized field pointing at
    one peer); the id collapsing on that triple is correct, not a bug.
    """
    return f"{from_instance}::{field}::{to_instance}"


def shared_attribute_candidate_id(
    owner_ref: str, instance_id: str, event_name: str,
) -> str:
    """Stable id for a shared-attribute candidate.

    Distinct namespace (``shared_attr::``) from ``deterministic_edge_id``
    so the two producers can NEVER collide. The triple is the minimum
    that makes one Pickup instance distinguishable from another while
    the seed's ``event_name`` flags WHICH seed produced it (debug
    triage).
    """
    return f"shared_attr::{owner_ref}::{instance_id}::{event_name}"


def _derive_event_name_from_owner_field(
    owner_class: str, field: str,
) -> str | None:
    """Component-ref edges: deterministic ``<owner>_Set<Field>`` event
    name per design doc L239 (the ``Door_SetOpen`` example).

    ``field`` is capitalized only at its first character; multi-word
    camelCase fields (``isOpen`` → ``IsOpen``) keep internal casing so
    the name is human-recognizable.

    Phase 2b slice 2 (P3 carry-forward from slice 1): returns ``None``
    when ``field`` is empty. An empty-field component-ref edge has no
    semantic content (the producer has no field to write) -- emitting
    ``<owner>_Set`` would create a fragile event name + collide with
    every other empty-field edge from the same owner class. The
    structural-producer ``compute_cross_domain_edges`` skips edges
    whose event name is ``None``, so the empty-field row never reaches
    enrichment.
    """
    if not field:
        return None
    capitalized = field[0].upper() + field[1:]
    return f"{owner_class}_Set{capitalized}"


def compute_cross_domain_edges(
    scene_runtime: SceneRuntimeArtifact,
    *,
    domains_override: Mapping[str, str] | None = None,
) -> list[CrossDomainEdge]:
    """Enumerate every cross-domain serialized reference in the plan.

    A reference qualifies iff:
      - ``target_kind == "component"`` (peer-MonoBehaviour ref)
      - Both source and target instances resolve to modules with
        execution domains in ``{"client", "server"}``
      - The two domains differ

    Slice 1 stamps the new ``kind`` / ``resolution`` /
    ``bridge_member_scripts`` / ``payload`` fields on every emitted
    edge. The ``resolution.strategy`` is ``"remote_event_bridge"``
    unconditionally — slice 2's enrichment may downgrade to
    ``"same_domain_no_bridge"`` once it cross-checks finalized domains
    against the live module table. ``bridge_member_scripts`` stays
    empty until slice 2 fills it.

    Phase 2b slice 2 (P3 carry-forward from slice 1):
      - Iterates ``scenes`` and ``prefabs`` in SORTED key order so the
        combined output across both producers is stable across runs
        even when the upstream planner reorders its dict insertions.
        Matches ``compute_shared_attribute_candidates`` (which already
        sorted) so the two producers feed deterministically into the
        enrichment pass.
      - Skips edges whose ``field`` is empty (the producer has no
        attribute to write; ``_derive_event_name_from_owner_field``
        returns ``None`` on that input and the row is dropped here
        rather than emitting a fragile ``<owner>_Set`` event name).

    ``domains_override`` (Phase 2b slice 2 R1 P1-A fix): when supplied,
    consulted FIRST for each script_id's domain. Falls back to
    ``scene_runtime["modules"][sid]["domain"]`` only when
    ``domains_override.get(sid)`` is missing or empty. Required by the
    prepass call site (``pipeline.py`` ``_maybe_run_topology_prepass``)
    which runs BEFORE ``classify_scene_runtime_domains()`` stamps
    domains back onto ``scene_runtime`` — without the override, every
    edge on a fresh run sees ``""`` for both src + tgt domain and gets
    dropped by the ``NON_RUNTIME_DOMAINS`` filter. Direct callers
    (e.g. tests, the report writer) that pass already-stamped
    artifacts can omit the kwarg and the fallback path keeps working.

    Pure function; does not mutate ``scene_runtime``.
    """
    modules = cast(dict[str, dict[str, object]], scene_runtime.get("modules", {}))
    scenes = scene_runtime.get("scenes", {})
    prefabs = scene_runtime.get("prefabs", {})

    instance_to_script: dict[str, str] = {}
    for scene in scenes.values():
        for inst in scene.get("instances", []):
            instance_to_script[inst["instance_id"]] = inst["script_id"]
    for prefab in prefabs.values():
        for inst in prefab.get("instances", []):
            instance_to_script[inst["instance_id"]] = inst["script_id"]

    out: list[CrossDomainEdge] = []

    def _module_class_name(script_id: str) -> str:
        mod = modules.get(script_id, {})
        class_name = mod.get("class_name", "")
        if isinstance(class_name, str) and class_name:
            return class_name
        stem = mod.get("stem", "")
        return stem if isinstance(stem, str) else ""

    def _resolve_domain(script_id: str) -> str:
        # P1-A fix: consult ``domains_override`` first (the prepass's
        # already-inferred-but-not-yet-stamped domains), then fall
        # back to the on-row stamped value (direct callers / resume).
        if domains_override is not None:
            override_val = domains_override.get(script_id, "")
            if isinstance(override_val, str) and override_val:
                return override_val
        mod = modules.get(script_id, {})
        domain_obj = mod.get("domain", "")
        return domain_obj if isinstance(domain_obj, str) else ""

    def _scan(
        owner_kind: str,
        owner_ref: str,
        references: list[SceneRuntimeReference],
    ) -> None:
        for ref in references:
            if ref.get("target_kind") != "component":
                continue
            src_inst = ref.get("from", "")
            tgt_inst = ref.get("target_ref", "")
            src_sid = instance_to_script.get(src_inst, "")
            tgt_sid = instance_to_script.get(tgt_inst, "")
            if not src_sid or not tgt_sid:
                continue
            src_domain = _resolve_domain(src_sid)
            tgt_domain = _resolve_domain(tgt_sid)
            if (src_domain in NON_RUNTIME_DOMAINS
                    or tgt_domain in NON_RUNTIME_DOMAINS):
                continue
            if src_domain == tgt_domain:
                continue
            field = ref.get("field", "")
            owner_class = _module_class_name(src_sid)
            event_name = _derive_event_name_from_owner_field(
                owner_class, field,
            )
            if event_name is None:
                # Slice 2 P3 carry-forward: skip empty-field rows
                # rather than emit a fragile ``<owner>_Set`` event
                # name. The producer has no attribute to write here.
                continue
            out.append(CrossDomainEdge(
                id=deterministic_edge_id(src_inst, field, tgt_inst),
                kind="attribute_write",
                from_instance=src_inst,
                to_instance=tgt_inst,
                from_script=src_sid,
                to_script=tgt_sid,
                field=field,
                from_domain=src_domain,
                to_domain=tgt_domain,
                owner_kind=owner_kind,
                owner_ref=owner_ref,
                resolution=ResolutionSpec(
                    strategy="remote_event_bridge",
                    event_name=event_name,
                ),
                bridge_member_scripts=[],
                payload=PayloadSpec(
                    attribute_name=field,
                    schema="unknown",
                ),
            ))

    for key in sorted(scenes.keys()):
        scene = scenes[key]
        _scan("scene", key, scene.get("references", []))
    for key in sorted(prefabs.keys()):
        prefab = prefabs[key]
        _scan("prefab", key, prefab.get("references", []))

    return out


def compute_shared_attribute_candidates(
    scene_runtime: SceneRuntimeArtifact,
    *,
    domains_override: Mapping[str, str] | None = None,
) -> list[CrossDomainEdge]:
    """Enumerate every scene/prefab instance whose component class
    matches a ``SHARED_ATTRIBUTE_SEEDS`` row.

    This is the structural pre-transpile equivalent of today's
    hardcoded ``PlayerSetSharedFlag`` prompt block: walks scenes +
    prefabs, finds Pickup instances (and any future seed kinds), emits
    one candidate edge per match.

    Slice 1's candidates are intentionally FAN-OUT: ``to_*`` fields are
    empty strings and ``bridge_member_scripts`` is empty. Slice 2's
    enrichment pass walks the module graph to find consumers (the
    scripts that ``GetAttribute(has<itemName>)``) and populates
    ``bridge_member_scripts`` with them.

    Iteration order is deterministic: scenes (sorted by key, then
    instance list order), then prefabs (sorted by key, then instance
    list order). Phase 2b slice 2 unifies the two producers on this
    sorted-iteration shape (``compute_cross_domain_edges`` previously
    used dict-insertion order; both now sort). Stable across runs
    given the same input independent of upstream dict insertion order
    — required by the enrichment pass that feeds both producers'
    outputs into a single duplicate-event-name check.

    ``domains_override`` (Phase 2b slice 2 R1 P1-A fix): when supplied,
    consulted FIRST for each script_id's ``from_domain`` stamping.
    Falls back to ``scene_runtime["modules"][sid]["domain"]`` only
    when the override is missing or empty. Required by the prepass
    call site (``pipeline.py`` ``_maybe_run_topology_prepass``) which
    runs BEFORE ``classify_scene_runtime_domains()`` writes domains
    back onto ``scene_runtime`` — without the override, every
    candidate row would carry ``from_domain=""`` on fresh runs.

    Pure function; does not mutate ``scene_runtime``.
    """
    modules = cast(dict[str, dict[str, object]], scene_runtime.get("modules", {}))
    scenes = scene_runtime.get("scenes", {})
    prefabs = scene_runtime.get("prefabs", {})

    # Index seeds by class name for O(1) lookup. Slice 1 has exactly
    # one seed, but the lookup pattern stays the same as the table
    # grows.
    seeds_by_class: dict[str, SharedAttributeSeed] = {
        seed.producer_class_name: seed for seed in SHARED_ATTRIBUTE_SEEDS
    }
    if not seeds_by_class:
        return []

    out: list[CrossDomainEdge] = []

    def _module_class_name(script_id: str) -> str:
        mod = modules.get(script_id, {})
        class_name = mod.get("class_name", "")
        if isinstance(class_name, str) and class_name:
            return class_name
        stem = mod.get("stem", "")
        return stem if isinstance(stem, str) else ""

    def _module_domain(script_id: str) -> str:
        # P1-A fix: consult ``domains_override`` first (the prepass's
        # already-inferred-but-not-yet-stamped domains), then fall
        # back to the on-row stamped value (direct callers / resume).
        if domains_override is not None:
            override_val = domains_override.get(script_id, "")
            if isinstance(override_val, str) and override_val:
                return override_val
        mod = modules.get(script_id, {})
        domain = mod.get("domain", "")
        return domain if isinstance(domain, str) else ""

    def _emit_for_owner(
        owner_kind: str, owner_ref: str,
        instances: list[dict[str, object]],
    ) -> None:
        for inst in instances:
            script_id_obj = inst.get("script_id", "")
            script_id = script_id_obj if isinstance(script_id_obj, str) else ""
            if not script_id:
                continue
            class_name = _module_class_name(script_id)
            seed = seeds_by_class.get(class_name)
            if seed is None:
                continue
            instance_id_obj = inst.get("instance_id", "")
            instance_id = (
                instance_id_obj if isinstance(instance_id_obj, str) else ""
            )
            if not instance_id:
                continue
            from_domain = _module_domain(script_id)
            out.append(CrossDomainEdge(
                id=shared_attribute_candidate_id(
                    owner_ref, instance_id, seed.remote_event_name,
                ),
                kind="attribute_write",
                from_instance=instance_id,
                to_instance="",
                from_script=script_id,
                to_script="",
                field=seed.attribute_template,
                from_domain=from_domain,
                to_domain="",
                owner_kind=owner_kind,
                owner_ref=owner_ref,
                resolution=ResolutionSpec(
                    strategy="remote_event_bridge",
                    event_name=seed.remote_event_name,
                ),
                bridge_member_scripts=[],
                payload=PayloadSpec(
                    attribute_name=seed.attribute_template,
                    schema="bool",
                ),
            ))

    for key in sorted(scenes.keys()):
        scene = scenes[key]
        _emit_for_owner(
            "scene", key,
            cast(list[dict[str, object]], scene.get("instances", [])),
        )
    for key in sorted(prefabs.keys()):
        prefab = prefabs[key]
        _emit_for_owner(
            "prefab", key,
            cast(list[dict[str, object]], prefab.get("instances", [])),
        )

    return out


__all__ = (
    "BridgeMember",
    "CrossDomainEdge",
    "NON_RUNTIME_DOMAINS",
    "PayloadSpec",
    "ResolutionSpec",
    "SHARED_ATTRIBUTE_SEEDS",
    "SharedAttributeSeed",
    "compute_cross_domain_edges",
    "compute_shared_attribute_candidates",
    "deterministic_edge_id",
    "shared_attribute_candidate_id",
)
