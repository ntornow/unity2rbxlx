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
    """
    if character_attached:
        return "character_attached"
    if is_loader:
        return "loader"
    if script_class in ("Script", "LocalScript"):
        return "auto_run"
    # ModuleScript path AND any unrecognised class. ``"requireable"`` is
    # the safe default — the runtime never auto-instantiates a
    # ``"requireable"`` row, so an excluded / helper module that lands
    # here won't accidentally boot.
    return "requireable"


__all__ = (
    "LIFECYCLE_ROLES",
    "LifecycleRole",
    "derive_module_lifecycle_role",
)
