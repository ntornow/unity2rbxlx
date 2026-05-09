"""DEPRECATED — re-exports from the new module layout.

The contents of this module were split:

- FPS-specific emitters (``detect_fps_game``, ``inject_fps_scripts``,
  ``generate_fps_client_script``, ``generate_hud_screen_gui``,
  ``generate_hud_client_script``, ``_has_client_fps_controller``,
  ``_has_hud_screen_gui``) live in :mod:`converter.scaffolding.fps`.
- Generic autogen scripts (``generate_game_server_script``,
  ``generate_collision_group_script``,
  ``generate_collision_fidelity_recook_script``) live in
  :mod:`converter.autogen`.

This shim preserves SYMBOL ACCESS for callers that still
``import converter.fps_client_generator``. New code should import
from the canonical locations directly.

**Behaviour caveat:** ``inject_fps_scripts`` is the same function
object as ``converter.scaffolding.fps.inject_fps_scripts`` — i.e. it
no longer self-gates on ``detect_fps_game``. PR #68 made the caller
the gatekeeper (:class:`converter.pipeline.Pipeline` checks
``"fps" in self.scaffolding`` before invoking it). External callers
that relied on the pre-PR-#68 auto-detection contract should either:

  - call ``detect_fps_game(place)`` themselves and gate the inject
    on the result, or
  - migrate to the canonical
    ``converter.scaffolding.fps.inject_fps_scripts`` import and
    document the explicit-call expectation.

The shim does NOT replicate the historic auto-detection — that gate
is intentionally gone repo-wide. Slated for removal once all internal
callers have migrated and any external users (none known) have had a
release cycle to update.
"""
from __future__ import annotations

# Re-exports — keep this list in sync with the original module's public
# surface. ``__all__`` is the single source of truth.
from converter.autogen import (
    generate_collision_fidelity_recook_script,
    generate_collision_group_script,
    generate_game_server_script,
)
from converter.scaffolding.fps import (
    _has_client_fps_controller,
    _has_hud_screen_gui,
    detect_fps_game,
    generate_fps_client_script,
    generate_hud_client_script,
    generate_hud_screen_gui,
    inject_fps_scripts,
)

__all__ = [
    "_has_client_fps_controller",
    "_has_hud_screen_gui",
    "detect_fps_game",
    "generate_collision_fidelity_recook_script",
    "generate_collision_group_script",
    "generate_fps_client_script",
    "generate_game_server_script",
    "generate_hud_client_script",
    "generate_hud_screen_gui",
    "inject_fps_scripts",
]
