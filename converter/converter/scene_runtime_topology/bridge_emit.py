"""bridge_emit -- post-transpile, pre-pack bridge emitter.

Owns ``synthesize_listener_id``: the enrichment pass (inside
``_maybe_run_topology_prepass``) and the later emitter both call this ONE
helper, so the id written into ``bridge_member_scripts[*].ref`` and the id
stamped onto the emitted Roblox ``Script`` instance agree by construction,
not by parallel reinvention.

Bridge direction matters. A client-originated edge needs a SERVER listener
(``:FireServer`` -> ``OnServerEvent``); a server-originated edge needs a
CLIENT listener (``:FireClient``/``:FireAllClients`` -> ``OnClientEvent``).
The helper takes an explicit ``direction`` so the emitter picks the correct
``RbxScript`` script_type + DataModel location and the
candidate-``ref``-validity invariant recognizes BOTH synthesized prefixes.
"""

from __future__ import annotations

from typing import Literal


# Distinct prefixes so the candidate-ref-validity invariant in
# ``build_topology._enforce_invariants`` can recognize a synthesized id
# without consulting the modules block. Real script ids never start with
# a leading underscore-block pattern in this codebase (they're either
# Unity GUIDs or planner-derived ``<stem>-<idx>`` strings), so collision
# with a legitimate script id is impossible by construction.
#
# Two distinct prefixes (one per direction) so the invariant can recognize
# either shape, and so dumps make the direction visible during triage.
SYNTHESIZED_SERVER_LISTENER_ID_PREFIX = "__bridge_listener_server__"
SYNTHESIZED_CLIENT_LISTENER_ID_PREFIX = "__bridge_listener_client__"

# All synthesized-listener prefixes the invariant must accept. Closed
# tuple so adding a future direction (if one ever exists) forces a code
# edit here rather than a silent drift.
SYNTHESIZED_LISTENER_ID_PREFIXES: tuple[str, ...] = (
    SYNTHESIZED_SERVER_LISTENER_ID_PREFIX,
    SYNTHESIZED_CLIENT_LISTENER_ID_PREFIX,
)


BridgeDirection = Literal["client_to_server", "server_to_client"]


def synthesize_listener_id(
    event_name: str,
    *,
    direction: BridgeDirection,
) -> str:
    """Return the deterministic script_id for the listener that the
    emitter will synthesize for ``event_name``.

    ``direction``:
      - ``"client_to_server"`` (client producer -> server listener):
        emitter rewrites ``SetAttribute`` to ``:FireServer(target, v)``;
        the synthesized listener subscribes via ``OnServerEvent``.
      - ``"server_to_client"`` (server producer -> client listener):
        emitter rewrites ``SetAttribute`` to ``:FireClient(target, v)``
        or ``:FireAllClients(v)``; the synthesized listener subscribes
        via ``OnClientEvent``.

    The id is stable across runs given the same ``(event_name,
    direction)`` pair; ``edge_enrichment`` writes this value into
    ``bridge_member_scripts[*].ref`` for the listener role of a
    component-ref edge, and a later emitter uses the same helper when it
    allocates the synthesized listener ``RbxScript`` -- so the artifact
    and the emitted script agree by construction, not by parallel
    reinvention.

    ``event_name`` is the derived ``<owner>_Set<Field>`` string for
    component-ref edges.

    Returns a string with the per-direction prefix so a consumer can
    recognize a synthesized id without consulting the modules block.
    """
    if direction == "client_to_server":
        return f"{SYNTHESIZED_SERVER_LISTENER_ID_PREFIX}{event_name}"
    # direction == "server_to_client" -- the only other allowed value.
    # The ``Literal`` type closes the set; this branch covers the
    # remaining variant explicitly so a future direction-string
    # extension fails the type-check rather than silently emitting one
    # of the two prefixes.
    return f"{SYNTHESIZED_CLIENT_LISTENER_ID_PREFIX}{event_name}"


__all__ = (
    "BridgeDirection",
    "SYNTHESIZED_CLIENT_LISTENER_ID_PREFIX",
    "SYNTHESIZED_LISTENER_ID_PREFIXES",
    "SYNTHESIZED_SERVER_LISTENER_ID_PREFIX",
    "synthesize_listener_id",
)
