"""animation_routing — per-animation driver-edge resolution + domain
inheritance.

NEW in Phase 1. Today's animation_converter routes every Anim_* script
to ServerScriptService unconditionally (see comment at
``animation_converter.py:1275``). That's wrong when the C# driver is
client-side (the SimpleFPS Door bug): the animation script ends up in a
domain where it can't observe the attribute the driver writes.

This module owns the rule: an animation script INHERITS the domain of
its driver. The driver is the MonoBehaviour whose serialized Animator
field points at the GameObject owning the clip's Animator. We resolve
it from ``scene_runtime``'s already-walked serialized-reference graph
(``target_component_type == "Animator"``), not from a regex on the
post-transpile Luau body.

Phase 1 driver-detection scope:
  - Same-scope match only (driver is in the same prefab template or the
    same scene as the animator). Cross-prefab drivers (HostilePlane
    pattern, where the player prefab drives a HostilePlane animator)
    fall back to today's server placement until Phase 2's C#-source
    narrowing pass arrives.
  - First candidate in lexicographic order wins. Multiple drivers in
    one scope is rare today; Phase 2's narrowing pass scans the
    candidate's C# for ``Animator.SetBool/SetTrigger/Play`` matching
    the clip's parameters and picks the actual writer.

Public surface:
  - ``compute_stable_id``: produce the artifact key
    ``<scope>:<ctrl_key|__none__>:<clip_disp>``.
  - ``resolve_driver``: walk the scope's references and pick the first
    Animator-referencing MonoBehaviour.
  - ``build_animation_driver_entry``: assemble the artifact entry from
    a resolved driver + clip + scope.
  - ``ORPHAN_SCOPE`` / ``NO_CTRL_KEY``: sentinel strings consumers must
    use when no controller / scope applies. Keeping them named avoids
    drift between writer and reader.
"""

from __future__ import annotations

from typing import Literal, TypedDict, cast
from urllib.parse import quote, unquote

from converter.scene_runtime_planner import (
    SceneRuntimeArtifact,
    SceneRuntimePrefab,
    SceneRuntimeReference,
    SceneRuntimeScene,
)
from converter.scene_runtime_topology.lifecycle_roles import (
    LifecycleRole,
    derive_module_lifecycle_role,
)


# Sentinels. Exported as named constants so writer and reader code in
# different modules can't drift.
ORPHAN_SCOPE: str = "__orphans__"
NO_CTRL_KEY: str = "__none__"


# Routing status enum. Replaces the older ``__orphan__`` magic-string
# sentinel that codex flagged as fail-OPEN: stamping every unresolved
# driver lookup as "orphan" hid resolver bugs behind invariant skips.
# With an explicit status:
#   - ``resolved``: a driver was found in this scope and routes the
#     animation. Invariants 1 + 6 enforce its module + domain.
#   - ``unresolved``: same-scope resolution found 0 or 2+ candidates
#     (Phase 1 narrowing limitation). Invariants 1 + 6 skip; the
#     animation falls back to today's server placement; Phase 2's
#     C#-source narrowing pass will reduce these.
#   - ``orphan``: deliberately orphan (project-wide orphan clip with
#     no controller / no scope). Invariants 1 + 6 skip.
AnimationRoutingStatus = Literal["resolved", "unresolved", "orphan"]


# Domains an animation script is allowed to land in. ``"helper"`` and
# ``"excluded"`` are NOT valid for animation scripts (they're auto-run,
# can't be helpers; if their driver is excluded, the animation can't run
# either). Phase 1 invariant 6 (build_topology.py) rejects animations
# whose driver domain falls outside this set.
AnimationDomain = Literal["client", "server"]
AnimationScriptClass = Literal["Script", "LocalScript"]


class AnimationObservedTarget(TypedDict, total=False):
    """Where an animation actually animates, relative to its host.

    Populated from the clip's curve paths:
      - kind ``"self"``: every curve has empty path → host IS the target.
      - kind ``"child"``: every curve shares a single non-empty,
        no-slash path → target is a direct child.
      - kind ``"descendant"``: paths have slashes or differ among curves
        → target is reached via a nested ``FindFirstChild(..., true)``.

    ``name`` is the (first) path; ``scope`` describes how the animation
    script reaches the target (today always ``"self.gameObject"`` for
    prefab-scoped clones and ``"workspace"`` for the global flat path —
    matches the existing animation_converter emission shape).
    """

    kind: Literal["self", "child", "descendant"]
    name: str
    scope: str


class AnimationDriverEntry(TypedDict, total=False):
    """One entry in the topology artifact's ``animation_drivers`` block.

    All fields are required at emit time (Phase 1 invariants reject
    missing fields), but the TypedDict is ``total=False`` so partial
    construction during assembly is allowed.

    ``domain``, ``script_class``, ``lifecycle_role`` are tightly
    coupled (build_topology invariant 4) — ``script_class == "Script"``
    iff ``domain == "server"``; ``"LocalScript"`` iff ``"client"``; and
    ``lifecycle_role == "auto_run"`` in Phase 1 (no animations are
    requireable / loaders / character-attached / bridge listeners).

    ``bridge_group_id`` is always ``None`` in Phase 1: the animation
    script's domain is the driver's domain by inheritance, so writer
    and reader are co-resident and no bridge is needed. Phase 2b
    populates this when the writer's lifecycle moves to a different
    domain (e.g. an animation listening for a server-authoritative
    attribute writeshared from a client-driven peer).
    """

    stable_id: str
    routing_status: AnimationRoutingStatus
    driver_module_guid: str
    domain: AnimationDomain
    script_class: AnimationScriptClass
    lifecycle_role: LifecycleRole
    observed_attribute: str
    observed_target: AnimationObservedTarget
    bridge_group_id: str | None


# Characters reserved for the stable_id grammar. Percent-encoded inside
# each segment so a Unity name containing ``:`` or ``%`` (rare but legal)
# can't collide with the separator and produce a non-injective mapping
# (codex W6).
_STABLE_ID_RESERVED = ":%"


def _escape_segment(value: str) -> str:
    """Percent-encode the stable_id grammar reserved chars in one segment.

    Reserved: ``:`` (separator) and ``%`` (escape marker). Unity allows
    both in asset names (uncommon but legal). Percent-encoding makes the
    `compute_stable_id` mapping injective: two distinct segment tuples
    can NEVER produce the same string.
    """
    return quote(value, safe="", encoding="utf-8")


def _unescape_segment(value: str) -> str:
    """Inverse of ``_escape_segment``. Exposed for diagnostic / report
    code that wants to render a stable_id back to its display form.
    """
    return unquote(value, encoding="utf-8")


def compute_stable_id(
    scope: str, ctrl_key: str | None, clip_disp: str,
) -> str:
    """Build the artifact key for an emitted animation script.

    Format: ``<scope>:<ctrl_key|__none__>:<clip_disp>`` with each segment
    percent-encoded against the reserved chars ``:`` and ``%``. The
    encoding is injective — distinct segment tuples never produce the
    same string (codex W6 fix).

    ``scope`` is the planner-stable scope identifier the topology
    consumes (a scene namespace like ``Assets/Scenes/Main.unity`` or a
    stable prefab id like ``<guid>:Assets/Prefabs/Door.prefab``).
    Display-only strings (bare prefab names) belong in EmittedAnimation
    `scope_display`, not here — see build_topology's `EmittedAnimation`
    docstring for the contract.

    Uniqueness of the (ctrl_key, clip_disp) pair within a scope is
    enforced upstream by ``_disambiguate_by_source`` in
    animation_converter (which appends a sha8 suffix on Unity-name
    collisions). The package's invariant 3 in build_topology is the
    backstop that catches any drift.

    For orphan clips (no controller), the caller passes ``ctrl_key=None``
    and the sentinel ``__none__`` is substituted. For project-wide
    orphans the caller also passes ``scope=ORPHAN_SCOPE``.
    """
    ck = ctrl_key if ctrl_key else NO_CTRL_KEY
    return (
        f"{_escape_segment(scope)}"
        f":{_escape_segment(ck)}"
        f":{_escape_segment(clip_disp)}"
    )


def derive_observed_target(
    curve_paths: list[str], prefab_scoped: bool,
) -> AnimationObservedTarget:
    """Inspect a clip's curve paths to describe where it animates.

    Phase 1 collapses multi-path clips to ``descendant`` + the first
    path; phase 2 may expand to a list when a use case demands per-path
    granularity. ``scope`` mirrors animation_converter's existing
    runtime emission: prefab-scoped scripts look up via
    ``self.gameObject``-equivalent (``script.Parent`` in the emitted
    Luau); scene-scoped scripts use ``workspace``.
    """
    unique = sorted({p for p in curve_paths if p is not None})
    # Trim empty path off the unique set for kind selection so a clip
    # with one empty + one non-empty path classifies on the non-empty.
    nonempty = [p for p in unique if p]
    scope_str = "self.gameObject" if prefab_scoped else "workspace"
    if not nonempty:
        return {"kind": "self", "name": "", "scope": scope_str}
    first = nonempty[0]
    if len(nonempty) == 1 and "/" not in first:
        return {"kind": "child", "name": first, "scope": scope_str}
    return {"kind": "descendant", "name": first, "scope": scope_str}


# ---------------------------------------------------------------------------
# Driver resolution
# ---------------------------------------------------------------------------

def _iter_scope_references(
    scene_runtime: SceneRuntimeArtifact,
    scope_kind: str,
    scope_ref: str,
) -> tuple[list[SceneRuntimeReference], dict[str, str]]:
    """Return ``(references, instance_to_script)`` for one scope.

    ``scope_kind`` is ``"prefab"`` or ``"scene"``. For prefabs,
    ``scope_ref`` is the stable prefab_id (the key into
    ``scene_runtime.prefabs``). For scenes, it's the scene namespace.

    Orphan animations have no scope; callers pass through
    ``resolve_driver(scope_kind="orphan")`` which short-circuits before
    reaching this helper.
    """
    if scope_kind == "prefab":
        prefabs = cast(
            dict[str, SceneRuntimePrefab], scene_runtime.get("prefabs", {}),
        )
        prefab = prefabs.get(scope_ref)
        if prefab is None:
            return [], {}
        return (
            list(prefab.get("references", [])),
            {i["instance_id"]: i["script_id"] for i in prefab.get("instances", [])},
        )
    if scope_kind == "scene":
        scenes = cast(
            dict[str, SceneRuntimeScene], scene_runtime.get("scenes", {}),
        )
        scene = scenes.get(scope_ref)
        if scene is None:
            return [], {}
        return (
            list(scene.get("references", [])),
            {i["instance_id"]: i["script_id"] for i in scene.get("instances", [])},
        )
    return [], {}


def resolve_driver(
    scene_runtime: SceneRuntimeArtifact,
    *,
    scope_kind: str,
    scope_ref: str,
) -> tuple[str, str] | None:
    """Find the first MonoBehaviour in ``scope_ref`` that serializes an
    Animator reference. Returns ``(driver_module_guid, driver_domain)``
    or ``None`` when no candidate exists.

    Phase 1 minimal logic (per W5-deprioritized, B1-corrected algorithm):
      - Walk every reference in the scope.
      - Keep refs with ``target_component_type == "Animator"``.
      - Sort by ``(from_instance, field, target_ref)`` for determinism.
      - First survivor's owning instance is the driver candidate.
      - That instance's ``script_id`` IS the driver_module_guid.
      - Driver domain = ``modules[driver_guid].domain``.

    Phase 2 will narrow candidates by scanning the candidate's C#
    source for ``Animator.SetBool/SetTrigger/Play`` calls matching the
    animator clip's parameters — the structurally-right algorithm
    against today's converter shape (per the subagent + codex review).

    Returns ``None`` (driver-not-found fallback) when:
      - ``scope_kind == "orphan"`` (no scope to walk),
      - the scope's references don't include any Animator-typed ref,
      - or the candidate's module has a non-runtime domain
        (``"helper"`` / ``"excluded"`` / unresolvable).
    """
    if scope_kind == "orphan":
        return None
    references, instance_to_script = _iter_scope_references(
        scene_runtime, scope_kind, scope_ref,
    )
    if not references:
        return None
    modules = cast(
        dict[str, dict[str, object]], scene_runtime.get("modules", {}),
    )
    candidates: list[tuple[str, str, str]] = []
    for ref in references:
        target_component_type = cast(
            dict[str, object], ref,
        ).get("target_component_type", "")
        if target_component_type != "Animator":
            continue
        candidates.append(
            (ref.get("from", ""), ref.get("field", ""), ref.get("target_ref", "")),
        )
    if not candidates:
        return None
    candidates.sort()
    for from_instance, _field, _target_ref in candidates:
        driver_guid = instance_to_script.get(from_instance, "")
        if not driver_guid:
            continue
        driver_module = modules.get(driver_guid, {})
        driver_domain_obj = driver_module.get("domain", "")
        driver_domain = driver_domain_obj if isinstance(driver_domain_obj, str) else ""
        if driver_domain in ("client", "server"):
            return driver_guid, driver_domain
        # Non-runtime domain — skip, try next candidate.
    return None


# ---------------------------------------------------------------------------
# Artifact assembly
# ---------------------------------------------------------------------------

def _script_class_for_domain(domain: AnimationDomain) -> AnimationScriptClass:
    """``Script`` for server, ``LocalScript`` for client. Phase 1 invariant
    enforces this 1:1."""
    return "Script" if domain == "server" else "LocalScript"


def build_animation_driver_entry(
    *,
    stable_id: str,
    routing_status: AnimationRoutingStatus,
    driver_module_guid: str,
    domain: AnimationDomain,
    observed_attribute: str,
    observed_target: AnimationObservedTarget,
) -> AnimationDriverEntry:
    """Compose a fully-populated ``animation_drivers`` entry.

    ``routing_status`` distinguishes:
      - ``"resolved"``: ``driver_module_guid`` is a real module guid
        and invariants 1 + 6 will enforce the link.
      - ``"unresolved"``: same-scope driver couldn't be picked
        deterministically (no candidate, or 2+ candidates — Phase 2's
        C# narrowing pass will improve this). ``driver_module_guid``
        is empty; the animation falls back to today's server placement;
        invariants 1 + 6 skip.
      - ``"orphan"``: deliberately orphan (project-wide orphan clip).
        ``driver_module_guid`` is empty; invariants skip.

    Phase 1 hardcodes ``lifecycle_role = "auto_run"`` (every animation
    script runs on load — no requireable / loader / character-attached
    animations in Phase 1) and ``bridge_group_id = None`` (animation
    inherits driver's domain → writer + reader co-resident → no bridge).
    """
    script_class: AnimationScriptClass = _script_class_for_domain(domain)
    lifecycle_role: LifecycleRole = derive_module_lifecycle_role(
        domain=domain,
        script_class=script_class,
        character_attached=False,
        is_loader=False,
    )
    return {
        "stable_id": stable_id,
        "routing_status": routing_status,
        "driver_module_guid": driver_module_guid,
        "domain": domain,
        "script_class": script_class,
        "lifecycle_role": lifecycle_role,
        "observed_attribute": observed_attribute,
        "observed_target": observed_target,
        "bridge_group_id": None,
    }


__all__ = (
    "AnimationDomain",
    "AnimationDriverEntry",
    "AnimationObservedTarget",
    "AnimationRoutingStatus",
    "AnimationScriptClass",
    "NO_CTRL_KEY",
    "ORPHAN_SCOPE",
    "build_animation_driver_entry",
    "compute_stable_id",
    "derive_observed_target",
    "resolve_driver",
)
