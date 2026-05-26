"""scene_runtime_topology — the sole authority over deployment-affecting
decisions for every script the converter emits.

Phase 1 (this package's first cut) is authority ONLY for **animation
script placement**. The rule the package establishes — every downstream
emitter is a structurally bound consumer with no independent decision
authority over the fields topology owns — applies in phases:

  - Phase 1 (here): `animation_converter` consults `animation_routing`
    for `script_class` + container + cross-domain edges. Other consumers
    (`script_storage`, `code_transpiler`, `contract_pipeline`) keep
    today's behavior.
  - Phase 2a: `script_storage` becomes a bound consumer; storage
    mutations leave `scene_runtime_domain.py`.
  - Phase 2b: `code_transpiler` becomes a bound consumer; cross-domain
    bridge code emitted at transpile time.
  - Phase 3: `contract_pipeline` verifies every emitted artifact matches
    the topology decision.

See `converter/docs/design/scene-runtime-architecture-ir.md` for the
binding contract.

Sub-modules:
  - ``module_domain``: per-module domain classification (relocated from
    ``scene_runtime_domain``; Phase 1 keeps it byte-equivalent in
    behavior — just a structural move).
  - ``cross_domain_edges``: enumeration + deterministic edge id +
    resolution metadata (extended from today's compute_cross_domain_edges
    with the bridge_group_id schema fields).
  - ``animation_routing``: per-animation driver-edge resolution + domain
    inheritance from driver. NEW in Phase 1.
  - ``lifecycle_roles``: closed-enum derivation per module / animation.
    NEW in Phase 1.
  - ``build_topology``: coordinator. Assembles the artifact + enforces
    emit-time invariants. Single orchestration entry point.

The persisted artifact lands under ``scene_runtime["topology"]`` in
``conversion_plan.json``. Consumers read it through this package's
public surface; direct dict indexing into the artifact is discouraged
so a future relocation (e.g. ``topology_plan.json`` sidecar) is a
one-file change.
"""

from __future__ import annotations

__all__ = ()
