"""bridge_emit -- Phase 2b post-transpile, pre-pack bridge emitter.

This module is the slice 3 deliverable site. Slice 2 lands ONE function
here (``synthesize_listener_id``) so the enrichment pass that runs inside
``_maybe_run_topology_prepass`` can stamp deterministic
``bridge_member_scripts[*].ref`` values for the synthesized server
listener role. Slice 3 will import THE SAME helper from inside the actual
emitter so the id slice 2 writes into the topology artifact and the id
slice 3 stamps onto the emitted Roblox ``Script`` instance agree by
construction -- not by parallel reinvention.

Per Claude arch review risk #2 (2026-05-30): if slice 2 invented its own
id shape and slice 3 invented a different one, the
``bridge_member_scripts[*].ref`` in the artifact would point at a script
that doesn't exist; the candidate-`ref`-validity invariant added in
slice 2 would catch the breakage on slice 3 builds, but only AFTER
silently shipping a stale artifact on slice 2-only builds. Centralizing
the synthesis here prevents that drift.

Slice 3 will add the rest of the module (the actual rewriter +
RemoteEvent/Script synthesizers). Slice 2 is intentionally minimal.
"""

from __future__ import annotations


# Distinct prefix so the candidate-ref-validity invariant in
# ``build_topology._enforce_invariants`` can recognize a synthesized id
# without consulting the modules block. Real script ids never start with
# a leading underscore-block pattern in this codebase (they're either
# Unity GUIDs or planner-derived ``<stem>-<idx>`` strings), so collision
# with a legitimate script id is impossible by construction.
SYNTHESIZED_LISTENER_ID_PREFIX = "__bridge_listener__"


def synthesize_listener_id(event_name: str) -> str:
    """Return the deterministic script_id for the server-side listener
    that ``slice 3``'s emitter will synthesize for ``event_name``.

    The id is stable across runs given the same ``event_name``; slice 2's
    enrichment pass writes this value into
    ``bridge_member_scripts[*].ref`` for the ``server_listener`` role,
    and slice 3's emitter uses the same helper when it allocates the
    synthesized listener ``RbxScript`` -- so the artifact and the
    emitted script agree by construction, not by parallel reinvention.

    ``event_name`` is the locked ``PickupItemEvent`` literal for
    shared-attribute candidates (see
    ``cross_domain_edges.SHARED_ATTRIBUTE_SEEDS``) or the derived
    ``<owner>_Set<Field>`` string for component-ref edges.

    Returns a string with the ``__bridge_listener__`` prefix so the
    candidate-`ref`-validity invariant in
    ``build_topology._enforce_invariants`` can recognize a synthesized
    id without consulting the modules block.
    """
    return f"{SYNTHESIZED_LISTENER_ID_PREFIX}{event_name}"


__all__ = (
    "SYNTHESIZED_LISTENER_ID_PREFIX",
    "synthesize_listener_id",
)
