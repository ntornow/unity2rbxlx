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

These tests pin the split surface so a future refactor that drops a
public symbol fails loudly.
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


