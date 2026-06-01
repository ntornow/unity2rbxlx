"""cross_domain_edges — enumerate every static component-ref cross-domain
edge (Class 1) in the plan.

Relocated in Phase 1 from ``scene_runtime_domain.compute_cross_domain_edges``.
Schema extended with the design doc's ``id`` field (the
``deterministic_edge_id`` used as ``bridge_group_id`` by Phase 2b
consumers). The id is populated deterministically in Phase 1 so Phase 2b
can read it without a schema migration.

**Phase 2b reframe (2026-06-01).** The empirical whole-plan review split
cross-domain authority into TWO bridge classes (design doc §"Phase 2b —
cross-domain authority (two bridge classes)"):

  - **Class 1 — static component-ref** (THIS module). A serialized
    peer-MonoBehaviour reference in scene data (Door → Animator's
    ``open`` field). The bridge is per-edge, derivable, owned + recorded
    by topology. ``compute_cross_domain_edges`` enumerates these; the
    ``CrossDomainEdge`` schema below carries them. UNCHANGED by the
    reframe.
  - **Class 2 — dynamic shared-flag** (NOT this module any more). An
    attribute name computed at runtime (``"has" .. itemName``) routed
    through ONE funnel RemoteEvent (``PlayerSetSharedFlag``). The
    slices-1-2 seed (``SHARED_ATTRIBUTE_SEEDS`` /
    ``compute_shared_attribute_candidates`` / ``producer_domain``)
    mis-modeled this dynamic class AS the static class and was RETIRED in
    the reframe. The channel is now recorded as a distinct
    ``shared_flag_channels`` fact (``shared_flag_channels.py``) and gated
    in step 2 / slice 3; the funnel itself stays.

This module carries the Class-1 schema + producer only:
  - ``kind: "attribute_write"`` (closed-enum discriminator).
  - ``resolution`` (strategy + event_name): the resolution metadata Phase
    2b consumes. ``resolution.strategy`` is ``"remote_event_bridge"``
    and ``resolution.event_name`` is derived from ``<owner>_Set<Field>``.
  - ``bridge_member_scripts``: the bridge unit (caller, listener, anim
    listener) populated by ``edge_enrichment.enrich_cross_domain_edges``.
  - ``payload`` (attribute_name + schema): the field name + its schema.

The CrossDomainEdge schema is intentionally KEPT FLAT (no nested
``producer{}/consumer{}`` sub-objects). The design doc's example
(L228-251) shows a nested shape; that restructure is deferred
indefinitely — every consumer reads ``from_*`` / ``to_*`` today.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypedDict, cast

from converter.scene_runtime_planner import (
    SceneRuntimeArtifact,
    SceneRuntimeReference,
)


class BridgeMember(TypedDict):
    """One script (or RemoteEvent) participating in a Class-1
    component-ref bridge.

    ``role`` values are a CLOSED enum: ``client_caller``,
    ``server_caller``, ``client_listener``, ``server_listener``,
    ``anim_listener``.

    Direction-aware caller/listener pairing (component-ref edges):
      - ``"client_caller"`` + ``"server_listener"``: a client-originated
        bridge (``from_domain == "client"``). The synthesized listener
        subscribes via ``OnServerEvent``.
      - ``"server_caller"`` + ``"client_listener"``: a server-originated
        bridge (``from_domain == "server"``). The listener subscribes
        via ``OnClientEvent``.

    Per-role contracts:
      - ``"client_caller"`` / ``"server_caller"``: the script_id whose
        ``SetAttribute`` the bridge rewrites — ``edge["from_script"]``.
      - ``"server_listener"`` / ``"client_listener"``: a SYNTHESIZED
        script_id (NOT a real module row id) for the listener, derived
        via ``bridge_emit.synthesize_listener_id(event_name,
        direction=...)``.
      - ``"anim_listener"``: the receiver script that consumes the
        bridged attribute. Equals ``edge["to_script"]``.
        Direction-independent (the consumer script is the same
        regardless of which side fires).

    ``ref`` is the consumer-readable identifier the bridge emitter will
    dereference: a script_id for caller / anim_listener roles, a
    synthesized id for the listener role.
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
    listener subscribes to. For component-ref edges it is derived
    deterministically from ``<owner>_Set<Field>``.
    """

    strategy: Literal["remote_event_bridge", "same_domain_no_bridge", "excluded"]
    event_name: str


class PayloadSpec(TypedDict):
    """What the bridge transmits.

    ``attribute_name`` is the Roblox Instance Attribute the listener
    writes on the target. Component-ref edges set this to the C# field
    name verbatim.

    ``schema``:
      - ``"unknown"``: the default for component-ref edges; a future
        pass may sharpen via type analysis.
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

    Phase 2b adds ``kind`` / ``resolution`` / ``bridge_member_scripts``
    / ``payload`` per the design doc Path C architecture. The
    producer/consumer restructure (nested objects per design doc
    L232-235) is intentionally NOT done.
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

    Phase 2b:
      - Iterates ``scenes`` and ``prefabs`` in SORTED key order so the
        output is stable across runs even when the upstream planner
        reorders its dict insertions.
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


__all__ = (
    "BridgeMember",
    "CrossDomainEdge",
    "NON_RUNTIME_DOMAINS",
    "PayloadSpec",
    "ResolutionSpec",
    "compute_cross_domain_edges",
    "deterministic_edge_id",
)
