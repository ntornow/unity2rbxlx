"""edge_enrichment -- Phase 2b slice 2 post-transpile enrichment.

Runs inside ``Pipeline._maybe_run_topology_prepass`` AFTER the two
structural producers (``compute_cross_domain_edges`` +
``compute_shared_attribute_candidates``) emit their raw rows.

Slice 2's enrichment fills ``bridge_member_scripts`` on every row,
branching on ``edge["from_domain"]`` (the producer's domain) so the
emitted caller / listener roles match the bridge direction slice 3
will actually emit:

  - **client -> server** (``from_domain == "client"``):
      - ``client_caller`` = ``edge["from_script"]``
      - ``server_listener`` = ``synthesize_listener_id(
          event_name, direction="client_to_server")``
        Slice 3 rewrites the producer's ``SetAttribute`` to
        ``<event>:FireServer(target, v)``; the listener subscribes
        via ``OnServerEvent``.
  - **server -> client** (``from_domain == "server"``):
      - ``server_caller`` = ``edge["from_script"]``
      - ``client_listener`` = ``synthesize_listener_id(
          event_name, direction="server_to_client")``
        Slice 3 rewrites to ``<event>:FireClient(target, v)`` or
        ``:FireAllClients(v)``; the listener subscribes via
        ``OnClientEvent``.
  - **anything else** (``from_domain`` is ``""``, ``helper``,
    ``excluded``, ``legacy``, or any non-runtime value): the row is
    downgraded to ``resolution.strategy == "excluded"`` and
    ``bridge_member_scripts`` is left EMPTY. Slice 3 skips excluded
    rows. The producers normally drop these rows via
    ``NON_RUNTIME_DOMAINS``; the defensive downgrade here catches
    upstream-input edges (e.g. on-disk plans, direct callers without
    a ``domains_override``) that slipped through.

  For **component-ref edges only**, an ``anim_listener`` member is also
  emitted (= ``edge["to_script"]``). The animation receiver script is
  domain-independent: whichever direction the bridge flows, the
  consumer script that ``edge["to_script"]`` names is the same.

  For **shared-attribute candidates** (fan-out): zero or more
  ``consumer`` rows -- one per ``script_id`` whose post-transpile Luau
  source contains ``:GetAttribute("has...")`` matching the seed's
  ``attribute_template``. Discovered via the same regex-walk pattern
  ``shared_state_linter.py:24,103`` uses for orphan GetAttribute
  detection.

The Luau-scan pass runs ONLY when
``state.transpilation_result is not None`` (fresh transpile this
invocation). On the assemble-no-retranspile resume path the scan
cannot run; resume rows leave ``consumer`` rows empty and slice 3's
emitter falls back to broadcast emission. The empty-on-resume
behavior is intentional and documented in
``BridgeMember.role`` docstring.

The enrichment is a pure function over its inputs: it does not mutate
the input rows, it returns new ``CrossDomainEdge`` rows with the
``bridge_member_scripts`` field replaced. The two structural producers
above remain unaware of enrichment -- this pass exists at the
prepass level where ``TopologyInputs`` data + the live
``transpilation_result`` are both in hand.
"""

from __future__ import annotations

import logging
import re

from converter.code_transpiler import TranspiledScript
from converter.scene_runtime_topology.bridge_emit import (
    BridgeDirection,
    synthesize_listener_id,
)
from converter.scene_runtime_topology.cross_domain_edges import (
    BridgeMember,
    CrossDomainEdge,
    PayloadSpec,
    ResolutionSpec,
    SHARED_ATTRIBUTE_SEEDS,
    SharedAttributeSeed,
)

_LOGGER = logging.getLogger(__name__)


def _direction_for_from_domain(
    from_domain: str,
) -> BridgeDirection | None:
    """Map ``edge["from_domain"]`` to the bridge direction slice 3
    will emit, or ``None`` if the producer's domain is not a real
    runtime domain.

    The two real-domain branches are mutually exclusive; everything
    else (``""``, ``helper``, ``excluded``, ``legacy``, future spellings)
    returns ``None`` so the caller can downgrade the row to
    ``excluded``. Slice 1's producers already filter
    ``NON_RUNTIME_DOMAINS`` so this should not fire on producer
    output -- it's a defensive fallthrough for direct callers (resume,
    tests, on-disk artifacts) feeding pre-built rows into the
    enrichment pass.
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
    ``BridgeMember.role`` and validated by slice 3's emitter contract.
    """
    if direction == "client_to_server":
        return "client_caller", "server_listener"
    # direction == "server_to_client" by ``BridgeDirection`` exhaustion.
    return "server_caller", "client_listener"


def _build_get_attribute_regex(attribute_template: str) -> re.Pattern[str]:
    """Compile a Luau-source regex matching every
    ``:GetAttribute("<prefix><suffix>")`` whose attribute name
    matches the seed template.

    ``attribute_template`` is the seed's per-instance template, e.g.
    ``"has<itemName>"``. The ``<...>`` slot is treated as a wildcard
    matching any identifier-safe characters. The literal prefix /
    suffix around the ``<...>`` slot is preserved verbatim.

    Built once per seed (caller caches in a dict) so the per-script
    scan is O(n_scripts) not O(n_scripts * n_seeds * each-compile).
    """
    # Split the template on the ``<placeholder>`` slot. Slice 1's
    # seed table has a single placeholder per template; if a future
    # seed introduces multiple placeholders this code must be
    # revisited (the assertion catches that case loudly rather than
    # producing a silent wildcard).
    parts = re.split(r"<[A-Za-z_][A-Za-z0-9_]*>", attribute_template)
    if len(parts) == 1:
        # No placeholder: literal match.
        prefix = re.escape(parts[0])
        body = prefix
    elif len(parts) == 2:
        prefix = re.escape(parts[0])
        suffix = re.escape(parts[1])
        # Match identifier-safe chars in the placeholder slot. The
        # actual per-instance values are Roblox attribute names
        # (e.g. ``hasKey``), so identifier-safe is the right class.
        body = prefix + r"[A-Za-z_][A-Za-z0-9_]*" + suffix
    else:
        # Multi-placeholder templates: refuse rather than guess.
        # Slice 1's seed table only has single-placeholder rows; a
        # future multi-placeholder row needs an explicit grammar
        # decision here.
        msg = (
            f"edge_enrichment: attribute_template {attribute_template!r} "
            "has more than one <placeholder> slot; multi-placeholder "
            "templates are not yet supported by the Luau-scan pass."
        )
        raise ValueError(msg)
    return re.compile(
        r":GetAttribute\(\s*['\"]" + body + r"['\"]\s*\)",
    )


def _seed_for_event_name(event_name: str) -> SharedAttributeSeed | None:
    """Reverse-lookup the seed row whose ``remote_event_name`` matches
    ``event_name``. Used by the candidate-row enrichment to recover
    the seed's ``attribute_template`` for the Luau scan.
    """
    for seed in SHARED_ATTRIBUTE_SEEDS:
        if seed.remote_event_name == event_name:
            return seed
    return None


def _script_id_for_transpiled(
    ts: TranspiledScript,
    script_id_by_name: dict[str, str],
) -> str:
    """Map a ``TranspiledScript`` row back to its planner ``script_id``.

    ``TranspiledScript.output_filename`` is the file stem with a
    ``.luau`` suffix; the planner's ``script_id_by_name`` index keys
    by the ``RbxScript.name`` (file stem, no extension). Strip the
    suffix to bridge the two.

    Returns ``""`` when no mapping exists -- the caller skips the row.
    Slice 6's ``build_script_id_by_name`` honors the
    degraded-service contract on class_name + stem collisions, so a
    missing entry here mirrors that contract (the script's
    container-decision likewise fell through to safe default).
    """
    out = ts.output_filename
    if out.endswith(".luau"):
        name = out[:-len(".luau")]
    else:
        name = out
    return script_id_by_name.get(name, "")


def _discover_consumers_for_template(
    *,
    attribute_template: str,
    transpiled_scripts: list[TranspiledScript],
    script_id_by_name: dict[str, str],
    producer_script_id: str,
) -> list[str]:
    """Return the script_ids whose post-transpile Luau source reads
    the bridged attribute via ``:GetAttribute("<template>")``.

    Filters out the producer's own script (a self-read is not a
    cross-script consumer of the bridge -- it's the same script
    reading what it just wrote, which doesn't need a bridge).

    Result is sorted for deterministic enrichment output across runs.
    """
    pattern = _build_get_attribute_regex(attribute_template)
    matched: set[str] = set()
    for ts in transpiled_scripts:
        if not pattern.search(ts.luau_source):
            continue
        consumer_sid = _script_id_for_transpiled(ts, script_id_by_name)
        if not consumer_sid:
            continue
        if consumer_sid == producer_script_id:
            continue
        matched.add(consumer_sid)
    return sorted(matched)


def enrich_cross_domain_edges(
    *,
    edges: list[CrossDomainEdge],
    candidates: list[CrossDomainEdge],
    transpiled_scripts: list[TranspiledScript] | None,
    script_id_by_name: dict[str, str],
) -> tuple[list[CrossDomainEdge], list[CrossDomainEdge]]:
    """Populate ``bridge_member_scripts`` on every row, direction-aware.

    Each row's ``from_domain`` selects the bridge direction:
      - ``"client"`` -> ``client_caller`` + ``server_listener`` pair;
        component-ref edges also carry ``anim_listener`` (=
        ``to_script``).
      - ``"server"`` -> ``server_caller`` + ``client_listener`` pair
        (matches the locked Pickup case per the
        ``pickup_remote_event_server`` pack contract); component-ref
        edges also carry ``anim_listener``.
      - anything else (``""``, ``helper``, ``excluded``, ``legacy``,
        future spellings) -> the row is downgraded to
        ``resolution.strategy == "excluded"`` and
        ``bridge_member_scripts`` is left empty; slice 3 skips
        ``excluded`` rows. A DEBUG-level log line records the drop.

    ``transpiled_scripts`` is ``None`` on the resume path
    (``state.transpilation_result is None``). On resume the
    Luau-scan pass cannot run; candidate rows receive only the
    caller + listener members (no ``consumer`` rows). Slice 3 falls
    back to broadcast emission when ``consumer`` rows are empty.

    Pure function: returns new lists of new ``CrossDomainEdge`` rows;
    the input lists + rows are NOT mutated.
    """
    # Component-ref edges: deterministic 3-member bridge unit when the
    # producer's ``from_domain`` resolves to a real direction; an
    # ``excluded`` downgrade with empty ``bridge_member_scripts`` when
    # it doesn't (defensive: producers already filter
    # ``NON_RUNTIME_DOMAINS``, but direct-caller / on-disk inputs may
    # supply pre-built rows with unknown domains).
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

    # Shared-attribute candidates: caller + listener (direction-aware) +
    # zero-or-more consumer rows from the Luau scan. ``Pickup`` is
    # server-originated per the existing ``pickup_remote_event_server``
    # pack contract (``script_coherence_packs.py:380-394``), so the
    # locked ``PickupItemEvent`` candidate is expected to land here as
    # ``server -> client`` once domain inference matches the pack
    # contract.
    enriched_candidates: list[CrossDomainEdge] = []
    for cand in candidates:
        event_name = cand["resolution"]["event_name"]
        direction = _direction_for_from_domain(cand["from_domain"])
        if direction is None:
            _LOGGER.debug(
                "edge_enrichment: dropping shared-attribute candidate "
                "with unknown from_domain %r (event_name=%r, "
                "from_script=%r); downgrading resolution.strategy to "
                "'excluded'.",
                cand["from_domain"], event_name, cand["from_script"],
            )
            enriched_candidates.append(_excluded(cand))
            continue
        caller_role, listener_role = _bridge_roles_for_direction(direction)
        bridge_members = [
            BridgeMember(
                role=caller_role,
                ref=cand["from_script"],
            ),
            BridgeMember(
                role=listener_role,
                ref=synthesize_listener_id(event_name, direction=direction),
            ),
        ]
        if transpiled_scripts is not None:
            seed = _seed_for_event_name(event_name)
            if seed is not None:
                consumer_sids = _discover_consumers_for_template(
                    attribute_template=seed.attribute_template,
                    transpiled_scripts=transpiled_scripts,
                    script_id_by_name=script_id_by_name,
                    producer_script_id=cand["from_script"],
                )
                for sid in consumer_sids:
                    bridge_members.append(BridgeMember(
                        role="consumer",
                        ref=sid,
                    ))
        enriched_candidates.append(
            _replace_bridge_members(cand, bridge_members),
        )

    return enriched_edges, enriched_candidates


def _replace_bridge_members(
    edge: CrossDomainEdge, new_members: list[BridgeMember],
) -> CrossDomainEdge:
    """Return a new ``CrossDomainEdge`` with ``bridge_member_scripts``
    replaced; all other fields verbatim.

    Pure: input ``edge`` is NOT mutated -- slice 1's producers return
    fresh ``CrossDomainEdge`` instances and this helper preserves
    that contract for the enrichment pass.
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

    Used when ``edge["from_domain"]`` does not resolve to a known
    bridge direction (``client`` / ``server``). Slice 3's emitter
    skips ``excluded`` rows. The ``event_name`` is preserved so dumps
    keep their debug-triage signal.
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
