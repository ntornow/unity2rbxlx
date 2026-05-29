"""scene_runtime_domain — BACK-COMPAT SHIM (Phase 1).

The classifier guts moved to
``converter.scene_runtime_topology.module_domain`` and the cross-domain
edge enumeration moved to
``converter.scene_runtime_topology.cross_domain_edges`` in Phase 1 of
PR #148 (the scene-runtime topology authority refactor).

This file remains as a thin re-export surface so pipeline.py + tests
that still import from ``converter.scene_runtime_domain`` keep working
without a flag-day update. Phase 2a will update those importers and
delete this shim.

If you are writing NEW code, import directly from
``converter.scene_runtime_topology.*`` — the package surface is the
intended-permanent home.
"""

from __future__ import annotations

from converter.scene_runtime_topology.cross_domain_edges import (
    NON_RUNTIME_DOMAINS as _NON_RUNTIME_DOMAINS,
    CrossDomainEdge,
    compute_cross_domain_edges,
)
from converter.scene_runtime_topology.module_domain import (
    DEFAULT_NETWORKING_MODE,
    DomainClassifierReport,
    NETWORKING_MODES,
    NetworkingMode,
    _CLIENT_RX,
    _CS_MODERATE_CLIENT,
    _CS_MODERATE_CLIENT_RX,
    _CS_MODERATE_SERVER,
    _CS_MODERATE_SERVER_RX,
    _CS_STRONG_CLIENT,
    _CS_STRONG_CLIENT_RX,
    _CS_STRONG_SERVER,
    _CS_STRONG_SERVER_RX,
    _GENERIC_CLIENT_API_PATTERNS,
    _GENERIC_SERVER_API_PATTERNS,
    _NETWORK_BEHAVIOUR_REACHABLE,
    _RE_NETWORK_BEHAVIOUR_CLASS,
    _RE_USING_MIRROR,
    _SERVER_CONTAINERS_FOR_REACHABILITY,
    _SERVER_RX,
    _apply_reachability_rule,
    _apply_rule_table,
    _build_displaced_rows,
    _check_mirror_adoption,
    _classify_api_surface,
    _classify_module,
    _collect_signals,
    _compile_cs_table,
    _compute_network_behaviour_reachable,
    _closure,
    _gather_per_instance_evidence,
    _InstanceEvidence,
    _load_cs_source,
    _SignalCounts,
    _stamp_container_and_path,
    _strip_cs_noise,
    _using_rx,
    classify_scene_runtime_domains,
    derive_reachability_requirements,
    infer_module_domains,
    migrate_legacy_domain_values,
)


__all__ = (
    "classify_scene_runtime_domains",
    "compute_cross_domain_edges",
    "derive_reachability_requirements",
    "infer_module_domains",
    "migrate_legacy_domain_values",
    "DomainClassifierReport",
    "CrossDomainEdge",
    "NetworkingMode",
    "NETWORKING_MODES",
    "DEFAULT_NETWORKING_MODE",
    "_GENERIC_CLIENT_API_PATTERNS",
    "_GENERIC_SERVER_API_PATTERNS",
)
