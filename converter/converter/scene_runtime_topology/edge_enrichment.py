"""edge_enrichment -- component-ref (Class 1) bridge-member enrichment.

Runs inside ``Pipeline._maybe_run_topology_prepass`` AFTER the structural
producer (``compute_cross_domain_edges``) emits its raw rows. Enriches
ONLY the Class-1 component-ref edges; the Class-2 dynamic shared-flag
channel is recorded separately in ``shared_flag_channels.py``.

The enrichment fills ``bridge_member_scripts`` on every component-ref
edge, branching on ``edge["from_domain"]`` (the producer's domain) so the
emitted caller / listener roles match the bridge direction:

  - **client -> server** (``from_domain == "client"``):
      - ``client_caller`` = ``edge["from_script"]``
      - ``server_listener`` = ``synthesize_listener_id(
          event_name, direction="client_to_server")``
        The listener subscribes via ``OnServerEvent``.
  - **server -> client** (``from_domain == "server"``):
      - ``server_caller`` = ``edge["from_script"]``
      - ``client_listener`` = ``synthesize_listener_id(
          event_name, direction="server_to_client")``
        The listener subscribes via ``OnClientEvent``.
  - **anything else** (``from_domain`` is ``""``, ``helper``,
    ``excluded``, ``legacy``, or any non-runtime value): the row is
    downgraded to ``resolution.strategy == "excluded"`` and
    ``bridge_member_scripts`` is left EMPTY. The producer normally drops
    these rows via ``NON_RUNTIME_DOMAINS``; the defensive downgrade here
    catches upstream-input edges (on-disk plans, direct callers without a
    ``domains_override``) that slipped through.

  An ``anim_listener`` member is also emitted (= ``edge["to_script"]``).
  The animation receiver script is domain-independent: whichever
  direction the bridge flows, the consumer script that ``edge["to_script"]``
  names is the same.

The enrichment is a pure function over its inputs: it does not mutate the
input rows, it returns new ``CrossDomainEdge`` rows with the
``bridge_member_scripts`` field replaced.
"""

from __future__ import annotations

import logging

from converter.scene_runtime_topology.bridge_emit import (
    BridgeDirection,
    synthesize_listener_id,
)
from converter.scene_runtime_topology.cross_domain_edges import (
    BridgeMember,
    CrossDomainEdge,
    PayloadSpec,
    ResolutionSpec,
)

_LOGGER = logging.getLogger(__name__)


def _direction_for_from_domain(
    from_domain: str,
) -> BridgeDirection | None:
    """Map ``edge["from_domain"]`` to the bridge direction, or ``None``
    if the producer's domain is not a real runtime domain.

    The two real-domain branches are mutually exclusive; everything else
    (``""``, ``helper``, ``excluded``, ``legacy``, future spellings)
    returns ``None`` so the caller can downgrade the row to ``excluded``.
    ``compute_cross_domain_edges`` already filters ``NON_RUNTIME_DOMAINS``
    so this should not fire on producer output -- it's a defensive
    fallthrough for direct callers (resume, tests, on-disk artifacts).
    """
    if from_domain == "client":
        return "client_to_server"
    if from_domain == "server":
        return "server_to_client"
    return None


def _bridge_roles_for_direction(
    direction: BridgeDirection,
) -> tuple[str, str]:
    """Return ``(caller_role, listener_role)`` for the bridge direction.

    Closed enum -- the four role names are documented in
    ``BridgeMember.role``.
    """
    if direction == "client_to_server":
        return "client_caller", "server_listener"
    # direction == "server_to_client" by ``BridgeDirection`` exhaustion.
    return "server_caller", "client_listener"


def enrich_cross_domain_edges(
    *,
    edges: list[CrossDomainEdge],
) -> list[CrossDomainEdge]:
    """Populate ``bridge_member_scripts`` on every component-ref edge,
    direction-aware.

    Each row's ``from_domain`` selects the bridge direction:
      - ``"client"`` -> ``client_caller`` + ``server_listener`` pair +
        ``anim_listener`` (= ``to_script``).
      - ``"server"`` -> ``server_caller`` + ``client_listener`` pair +
        ``anim_listener``.
      - anything else (``""``, ``helper``, ``excluded``, ``legacy``,
        future spellings) -> the row is downgraded to
        ``resolution.strategy == "excluded"`` and
        ``bridge_member_scripts`` is left empty. A DEBUG-level log line
        records the drop.

    Pure function: returns a new list of new ``CrossDomainEdge`` rows; the
    input list + rows are NOT mutated.
    """
    enriched_edges: list[CrossDomainEdge] = []
    for edge in edges:
        event_name = edge["resolution"]["event_name"]
        direction = _direction_for_from_domain(edge["from_domain"])
        if direction is None:
            _LOGGER.debug(
                "edge_enrichment: dropping component-ref edge with "
                "unknown from_domain %r (event_name=%r, from_script=%r); "
                "downgrading resolution.strategy to 'excluded'.",
                edge["from_domain"], event_name, edge["from_script"],
            )
            enriched_edges.append(_excluded(edge))
            continue
        caller_role, listener_role = _bridge_roles_for_direction(direction)
        bridge_members: list[BridgeMember] = [
            BridgeMember(
                role=caller_role,
                ref=edge["from_script"],
            ),
            BridgeMember(
                role=listener_role,
                ref=synthesize_listener_id(event_name, direction=direction),
            ),
            BridgeMember(
                role="anim_listener",
                ref=edge["to_script"],
            ),
        ]
        enriched_edges.append(_replace_bridge_members(edge, bridge_members))

    return enriched_edges


def _replace_bridge_members(
    edge: CrossDomainEdge, new_members: list[BridgeMember],
) -> CrossDomainEdge:
    """Return a new ``CrossDomainEdge`` with ``bridge_member_scripts``
    replaced; all other fields verbatim.

    Pure: input ``edge`` is NOT mutated.
    """
    return CrossDomainEdge(
        id=edge["id"],
        kind=edge["kind"],
        from_instance=edge["from_instance"],
        to_instance=edge["to_instance"],
        from_script=edge["from_script"],
        to_script=edge["to_script"],
        field=edge["field"],
        from_domain=edge["from_domain"],
        to_domain=edge["to_domain"],
        owner_kind=edge["owner_kind"],
        owner_ref=edge["owner_ref"],
        resolution=ResolutionSpec(
            strategy=edge["resolution"]["strategy"],
            event_name=edge["resolution"]["event_name"],
        ),
        bridge_member_scripts=new_members,
        payload=PayloadSpec(
            attribute_name=edge["payload"]["attribute_name"],
            schema=edge["payload"]["schema"],
        ),
    )


def _excluded(edge: CrossDomainEdge) -> CrossDomainEdge:
    """Return a new ``CrossDomainEdge`` with ``resolution.strategy``
    downgraded to ``"excluded"`` and ``bridge_member_scripts`` cleared.

    Used when ``edge["from_domain"]`` does not resolve to a known bridge
    direction (``client`` / ``server``). The ``event_name`` is preserved
    so dumps keep their debug-triage signal.
    """
    return CrossDomainEdge(
        id=edge["id"],
        kind=edge["kind"],
        from_instance=edge["from_instance"],
        to_instance=edge["to_instance"],
        from_script=edge["from_script"],
        to_script=edge["to_script"],
        field=edge["field"],
        from_domain=edge["from_domain"],
        to_domain=edge["to_domain"],
        owner_kind=edge["owner_kind"],
        owner_ref=edge["owner_ref"],
        resolution=ResolutionSpec(
            strategy="excluded",
            event_name=edge["resolution"]["event_name"],
        ),
        bridge_member_scripts=[],
        payload=PayloadSpec(
            attribute_name=edge["payload"]["attribute_name"],
            schema=edge["payload"]["schema"],
        ),
    )


__all__ = (
    "enrich_cross_domain_edges",
)
