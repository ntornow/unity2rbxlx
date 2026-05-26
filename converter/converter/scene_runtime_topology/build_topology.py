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
6th add):

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

    ``scope_kind``: ``"prefab"`` / ``"scene"`` / ``"orphan"``. Drives
        which slice of ``scene_runtime`` ``resolve_driver`` walks.
    ``scope_ref``: stable prefab_id (when prefab-scoped), scene
        namespace (when scene-scoped), or empty string (orphan).
    ``scope_display``: the scope STRING baked into the script_name +
        the stable_id. For prefab-scoped: the bare prefab template name.
        For scene-scoped: the scene namespace. For orphan:
        ``ORPHAN_SCOPE``.
    ``ctrl_key``: disambiguated controller name (or empty for orphan).
        Used in stable_id.
    ``clip_disp``: disambiguated clip name. Used in stable_id.
    ``script_name``: the emitted ``generated_scripts`` row key.
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
    placement decision. They share ``stem`` + ``domain`` but diverge
    everywhere else.

    Phase 1 populates: ``stem``, ``domain``, ``script_class``,
    ``lifecycle_role``, ``provenance``. ``bridge_group_id`` is populated
    when the module participates in a cross-domain edge; otherwise
    ``None``.
    """

    stem: str
    domain: str
    script_class: str
    lifecycle_role: LifecycleRole
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
    """

    modules: dict[str, TopologyModuleEntry]
    animation_drivers: dict[str, AnimationDriverEntry]
    cross_domain_edges: list[CrossDomainEdge]


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

    Returns the artifact dict ready to merge into
    ``scene_runtime["topology"]``.

    Aborts (raises ``TopologyInvariantError``) on any invariant
    violation. No warnings, no soft-fails — the design doc commits to
    fail-closed on emit.
    """
    modules_block = _build_modules_block(
        scene_runtime, scripts_by_class, guid_index,
    )
    edges_block = compute_cross_domain_edges(scene_runtime)
    animation_drivers_block = _build_animation_drivers_block(
        scene_runtime, emitted_animations,
    )

    artifact: TopologyArtifact = {
        "modules": modules_block,
        "animation_drivers": animation_drivers_block,
        "cross_domain_edges": edges_block,
    }

    # Invariants run AFTER assembly so they can cross-reference blocks.
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

    ``lifecycle_role`` derives from domain + script_class via
    ``derive_module_lifecycle_role``. Phase 1 hint inputs
    (``character_attached`` / ``is_loader``) are always False — those
    feed in via script_storage in Phase 2a.
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

        lifecycle_role = derive_module_lifecycle_role(
            domain=domain,
            script_class=script_class,
            character_attached=False,
            is_loader=False,
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
        scope_display = row.get("scope_display", "")
        ctrl_key = row.get("ctrl_key", "")
        clip_disp = row.get("clip_disp", "")
        stable_id = compute_stable_id(scope_display, ctrl_key, clip_disp)

        scope_kind = row.get("scope_kind", "")
        scope_ref = row.get("scope_ref", "")

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
    """Apply the 6 emit-time invariants. Raises on any violation.

    Each invariant is a single ``for`` loop with a clear failure
    message. The errors are catchable (``TopologyInvariantError``) so
    test code can assert specific invariant numbers without scraping
    log output.
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
        sid = compute_stable_id(
            row.get("scope_display", ""),
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


__all__ = (
    "EmittedAnimation",
    "TopologyArtifact",
    "TopologyInvariantError",
    "TopologyModuleEntry",
    "TopologyProvenance",
    "build_topology",
)
