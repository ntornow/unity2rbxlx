"""lifecycle_roles — closed enum of how the runtime should treat each
emitted script, plus the derivation rule.

The role is what a downstream consumer (script_storage, the host runtime,
contract_pipeline) must obey when placing or invoking the script. The
enum is CLOSED in Phase 1; future phases that need a new role extend the
``LifecycleRole`` literal here, never inline.

Phase 1 populates the role on every ``modules[*]`` entry and every
``animation_drivers[*]`` entry of the topology artifact. Phase 1
consumes the role only for animation script placement; Phase 2a wires
``script_storage`` to consume it for every module.

Roles:
  - ``auto_run``: the runtime starts the script on load.
    Roblox class is ``Script`` (server) or ``LocalScript`` (client).
  - ``requireable``: a ModuleScript loaded on demand via ``require()``.
    The runtime never invokes it directly; another script does.
  - ``loader``: a top-of-startup splash / asset-loader script. Roblox
    places these under ``ReplicatedFirst`` so they run before the rest
    of the data model finishes replicating. Always client-domain.
  - ``character_attached``: a per-player-character script that the
    runtime injects into the player's Character model. Roblox places
    these under ``StarterPlayer.StarterCharacterScripts``. Always
    client-domain.
  - ``bridge_listener``: a server-side auto-generated bridge that
    listens for a client→server attribute write. **POPULATED IN PHASE
    2b**; the value exists in the enum so Phase 1's persisted artifacts
    are forward-compatible with Phase 2b consumers.
  - ``scene_entrypoint``: a top-of-scene gameplay entrypoint script
    (e.g. ``GameManager`` on the initial scene). Distinguished from
    ``auto_run`` so the host runtime can sequence entrypoints first.
    **POPULATED IN PHASE 2a**; in Phase 1 every auto-running script
    gets ``auto_run`` and the entrypoint distinction is deferred.

The exhaustive ``Literal`` makes mypy + ``check_no_any.sh`` reject any
ad-hoc role string at use sites.
"""

from __future__ import annotations

from typing import Literal


LifecycleRole = Literal[
    "auto_run",
    "requireable",
    "loader",
    "character_attached",
    "bridge_listener",
    "scene_entrypoint",
]


LIFECYCLE_ROLES: tuple[LifecycleRole, ...] = (
    "auto_run",
    "requireable",
    "loader",
    "character_attached",
    "bridge_listener",
    "scene_entrypoint",
)


def derive_module_lifecycle_role(
    *,
    domain: str,
    script_class: str,
    character_attached: bool,
    is_loader: bool,
) -> LifecycleRole:
    """Derive a module's lifecycle role from its topology + intent inputs.

    Priority order matches the script_storage decision tree the doc
    sketches at lines 282-316: hard-priority pinpoints (character /
    loader) first, then class-driven.

    ``domain``: ``"client" | "server" | "helper" | "excluded"`` from
        module_domain's classifier. ``"helper"`` and ``"excluded"``
        modules don't have a runtime role per se; we still return
        ``"requireable"`` for helpers (the host never instantiates them
        but they're require-target shape) and ``"requireable"`` for
        excluded modules (they don't run; the requireable role is the
        most innocuous default that won't make a downstream consumer
        try to auto-run them).
    ``script_class``: the eventual Roblox class
        (``"Script" | "LocalScript" | "ModuleScript"``).
    ``character_attached``: whether scene_converter found this script
        attached to the player character prefab. Strict superset of any
        name-pattern heuristic and structurally derivable.
    ``is_loader``: whether the script has loader intent (
        ReplicatedFirst-name hint from ``code_transpiler`` OR explicit
        loader pragma).

    Phase 1 callers always pass ``character_attached=False`` and
    ``is_loader=False`` for modules driven from the topology artifact
    (those hints come from script_storage's inputs, which Phase 2a
    wires in). The function still accepts them so Phase 2a doesn't
    need to add new parameters.

    Returns ``"auto_run"`` on the ``Script`` / ``LocalScript`` happy
    path, ``"requireable"`` for ``ModuleScript``, and the priority
    overrides above when they fire.

    Domain gates on ``"character_attached"`` + ``"loader"``: both
    roles are documented as "Always client-domain" in the role enum
    above (``character_attached`` → `StarterCharacterScripts`,
    ``loader`` → `ReplicatedFirst` — both client-side containers).
    A runtime-bearing module the domain classifier put on
    ``"server"`` cannot validly hold either role: a server module
    routed to a client-only container would silently fail at runtime
    or, worse, be silently demoted by the storage layer. So
    ``character_attached`` and ``is_loader`` are *only* honored when
    ``domain == "client"``; a server-domain module with either
    boolean True falls through to the class-driven default
    (auto_run / requireable). This matches storage_classifier's
    parallel decision tree and surfaces the inconsistency at the
    output rather than encoding it (codex review 2026-05-28 P2 on
    slice 2 round 1).

    ``is_loader`` is additionally gated by
    ``script_class != "ModuleScript"``: a ModuleScript by definition
    doesn't auto-run, so ReplicatedFirst placement is meaningless for
    it. A ModuleScript whose stem matches the loader-name heuristic
    (e.g. ``LoadingUtils`` required by a real Loader script) routes
    to ``"requireable"``, NOT ``"loader"`` — matches
    ``storage_classifier._decide_script_container``'s
    ``script_type != "ModuleScript"`` skip (codex review 2026-05-28
    P2 on slice 2 initial).

    ``character_attached`` is symmetrically gated by
    ``script_class != "ModuleScript"`` (slice 7 round 3, Claude P2
    finding): a ModuleScript placed in StarterCharacterScripts does
    not auto-run on character spawn — Roblox only auto-instantiates
    Script / LocalScript under StarterCharacterScripts. A
    ModuleScript whose name pattern triggered ``character_attached``
    falls through to ``"requireable"``, matching the parallel
    storage_classifier ModuleScript skip and preventing silently
    inert StarterCharacterScripts placements. The
    ``character_attached`` gate is now exactly symmetric to
    ``is_loader``.
    """
    if (
        character_attached
        and script_class in ("Script", "LocalScript")
        and domain == "client"
    ):
        return "character_attached"
    if (
        is_loader
        and script_class in ("Script", "LocalScript")
        and domain == "client"
    ):
        return "loader"
    if script_class in ("Script", "LocalScript"):
        return "auto_run"
    # ModuleScript path AND any unrecognised class. ``"requireable"`` is
    # the safe default — the runtime never auto-instantiates a
    # ``"requireable"`` row, so an excluded / helper module that lands
    # here won't accidentally boot. A ModuleScript with a loader-named
    # stem (``is_loader=True``) also lands here, matching
    # storage_classifier's "skip ModuleScript for the ReplicatedFirst
    # heuristic" rule.
    return "requireable"


__all__ = (
    "LIFECYCLE_ROLES",
    "LifecycleRole",
    "derive_module_lifecycle_role",
)
