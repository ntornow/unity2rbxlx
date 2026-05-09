"""Tests for the converter.fps_client_generator module split.

PR #2 of the P1 TODO ("Remove FPS-specific logic from the converter")
moved emitters out of the historic ``fps_client_generator.py`` into
two purpose-specific modules:

- :mod:`converter.scaffolding.fps` — FPS-specific scaffolding gated
  by ``--scaffolding=fps`` (FPS controller, HUD ScreenGui, HUD client
  listener).
- :mod:`converter.autogen` — generic autogen scripts emitted by
  every conversion regardless of genre (GameServerManager,
  CollisionGroupSetup, CollisionFidelityRecook).

The original module name is preserved as a thin shim that re-exports
both for backward compatibility. These tests pin the split surface so
a future refactor that drops a public symbol fails loudly, and the
shim's pass-through stays accurate.
"""
from __future__ import annotations


class TestScaffoldingFpsSurface:
    """``converter.scaffolding.fps`` exposes the FPS-specific
    emitters and detectors. The opt-in gate
    (:meth:`Pipeline._subphase_inject_autogen_scripts`) imports from
    here, so changes to the public surface should be deliberate."""

    def test_fps_module_exposes_detector_and_helpers(self) -> None:
        from converter.scaffolding import fps

        assert callable(fps.detect_fps_game)
        assert callable(fps.inject_fps_scripts)
        assert callable(fps.generate_fps_client_script)
        assert callable(fps.generate_hud_screen_gui)
        assert callable(fps.generate_hud_client_script)
        # Internal helpers that the gate consults — kept public-ish
        # so the pipeline doesn't have to monkey with private names.
        assert callable(fps._has_client_fps_controller)
        assert callable(fps._has_hud_screen_gui)


class TestAutogenSurface:
    """``converter.autogen`` exposes the generic autogen scripts
    that every conversion considers emitting. Genre-agnostic — the
    pipeline calls each one based on scene-state predicates, not
    genre detection."""

    def test_autogen_module_exposes_three_generic_emitters(self) -> None:
        from converter import autogen

        assert callable(autogen.generate_game_server_script)
        assert callable(autogen.generate_collision_group_script)
        assert callable(autogen.generate_collision_fidelity_recook_script)


class TestLegacyShim:
    """``converter.fps_client_generator`` is now a shim that
    re-exports both new modules. Existing callers that haven't
    migrated keep working through the shim until a future cleanup
    removes it."""

    def test_shim_reexports_match_canonical_modules(self) -> None:
        from converter import autogen
        from converter import fps_client_generator as shim
        from converter.scaffolding import fps

        # FPS-specific symbols come from scaffolding.fps.
        assert shim.detect_fps_game is fps.detect_fps_game
        assert shim.inject_fps_scripts is fps.inject_fps_scripts
        assert shim.generate_hud_client_script is fps.generate_hud_client_script
        assert shim.generate_hud_screen_gui is fps.generate_hud_screen_gui
        assert shim.generate_fps_client_script is fps.generate_fps_client_script
        # Generic autogen symbols come from converter.autogen.
        assert shim.generate_game_server_script is autogen.generate_game_server_script
        assert shim.generate_collision_group_script is (
            autogen.generate_collision_group_script
        )
        assert shim.generate_collision_fidelity_recook_script is (
            autogen.generate_collision_fidelity_recook_script
        )

    def test_shim_dunder_all_lists_full_surface(self) -> None:
        """``__all__`` documents the shim's public surface. If a
        consumer does ``from fps_client_generator import *`` they
        get every name listed here. New symbols added to the
        canonical modules should be added here too if they're meant
        to be public."""
        from converter import fps_client_generator as shim

        assert "detect_fps_game" in shim.__all__
        assert "inject_fps_scripts" in shim.__all__
        assert "generate_game_server_script" in shim.__all__
        assert "generate_collision_fidelity_recook_script" in shim.__all__
        # Sorted, no duplicates.
        assert shim.__all__ == sorted(set(shim.__all__))
