"""cross_domain_edges — enumerate every client<->server serialized
reference in the plan.

Relocated in Phase 1 from ``scene_runtime_domain.compute_cross_domain_edges``.
Schema extended with the design doc's ``id`` field (the
``deterministic_edge_id`` used as ``bridge_group_id`` by Phase 2b
consumers). The id is populated deterministically in Phase 1 so Phase 2b
can read it without a schema migration.

Phase 1 has only one consumer of the ``id`` field: the topology artifact
builder, which uses it to link a module / animation driver to its bridge
edge. No code paths produce bridge code from the id yet — that's Phase 2b.
"""

from __future__ import annotations

from typing import TypedDict, cast

from converter.scene_runtime_planner import (
    SceneRuntimeArtifact,
    SceneRuntimeReference,
)


class CrossDomainEdge(TypedDict):
    """One client<->server reference identified at conversion time.

    ``id`` is added in Phase 1 (see module docstring). All other fields
    are byte-stable with the pre-Phase-1 CrossDomainEdge in
    ``scene_runtime_domain.py`` so on-disk plans + the cross-domain
    report writer keep working.

    Phase 2b extends this further with ``resolution`` +
    ``bridge_member_scripts`` once bridge code emission lands; those are
    intentionally absent here to avoid committing to a Phase 2b shape
    Phase 1 isn't ready to validate.
    """

    id: str
    from_instance: str
    to_instance: str
    from_script: str
    to_script: str
    field: str
    from_domain: str
    to_domain: str
    owner_kind: str
    owner_ref: str


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


def compute_cross_domain_edges(
    scene_runtime: SceneRuntimeArtifact,
) -> list[CrossDomainEdge]:
    """Enumerate every cross-domain serialized reference in the plan.

    A reference qualifies iff:
      - ``target_kind == "component"`` (peer-MonoBehaviour ref)
      - Both source and target instances resolve to modules with
        execution domains in ``{"client", "server"}``
      - The two domains differ

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
            src_mod = modules.get(src_sid, {})
            tgt_mod = modules.get(tgt_sid, {})
            src_domain_obj = src_mod.get("domain", "")
            tgt_domain_obj = tgt_mod.get("domain", "")
            src_domain = src_domain_obj if isinstance(src_domain_obj, str) else ""
            tgt_domain = tgt_domain_obj if isinstance(tgt_domain_obj, str) else ""
            if (src_domain in NON_RUNTIME_DOMAINS
                    or tgt_domain in NON_RUNTIME_DOMAINS):
                continue
            if src_domain == tgt_domain:
                continue
            field = ref.get("field", "")
            out.append(CrossDomainEdge(
                id=deterministic_edge_id(src_inst, field, tgt_inst),
                from_instance=src_inst,
                to_instance=tgt_inst,
                from_script=src_sid,
                to_script=tgt_sid,
                field=field,
                from_domain=src_domain,
                to_domain=tgt_domain,
                owner_kind=owner_kind,
                owner_ref=owner_ref,
            ))

    for key, scene in scenes.items():
        _scan("scene", key, scene.get("references", []))
    for key, prefab in prefabs.items():
        _scan("prefab", key, prefab.get("references", []))

    return out


__all__ = (
    "CrossDomainEdge",
    "NON_RUNTIME_DOMAINS",
    "compute_cross_domain_edges",
    "deterministic_edge_id",
)
