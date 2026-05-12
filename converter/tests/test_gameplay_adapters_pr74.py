"""Tests for PR #74 — default-on flip, ``--legacy-gameplay-packs``
opt-out, and the rehydration-aware prune pass for stale legacy
coherence-pack artifacts.

Covers:

  * ``ConversionContext.use_gameplay_adapters`` default is True.
  * ``Pipeline.__init__`` default and resume()'s post-rehydrate
    re-apply both honour the constructor's bidirectional flag.
  * ``_strip_legacy_door_tween_block`` removes the appended block
    cleanly and is idempotent.
  * ``_prune_legacy_gameplay_artifacts`` walks every script-bearing
    surface (global, workspace, replicated_templates) and the
    ``screen_guis`` list for ``_AutoFpsHud`` ScreenGuis.
  * The prune fires from ``write_output``'s gameplay-adapter
    runtime-injection branch BEFORE module emission.
"""
from __future__ import annotations

import types

import pytest

from converter.pipeline import (
    Pipeline,
    _LEGACY_DAMAGE_ROUTER_NAME,
    _LEGACY_DOOR_TWEEN_MARKER,
    _LEGACY_FPS_HUD_ATTR,
    _strip_legacy_door_tween_block,
)
from core.conversion_context import ConversionContext


class TestDefaultFlip:
    """PR #74: ``use_gameplay_adapters`` defaults True everywhere."""

    def test_conversion_context_default(self) -> None:
        ctx = ConversionContext(unity_project_path="/tmp/x")
        assert ctx.use_gameplay_adapters is True, (
            "ConversionContext.use_gameplay_adapters default flipped "
            "by PR #74 — pre-PR-#74 expectations should be migrated."
        )

    def test_pipeline_signature_default(self) -> None:
        import inspect

        sig = inspect.signature(Pipeline.__init__)
        param = sig.parameters["use_gameplay_adapters"]
        assert param.default is True, (
            "Pipeline.__init__ default flipped by PR #74 — must match "
            "ConversionContext default so a constructor with no flag "
            "and a fresh ctx don't disagree on adapter mode."
        )


class TestStripLegacyDoorTweenBlock:
    """Unit tests for the door-tween block strip helper."""

    def test_no_marker_is_no_op(self) -> None:
        src = "local foo = 1\nreturn foo\n"
        out, stripped = _strip_legacy_door_tween_block(src)
        assert stripped is False
        assert out == src

    def test_strips_block_with_marker(self) -> None:
        src = (
            "local foo = 1\n"
            "return foo\n"
            "\n"
            "-- _AutoFpsDoorTweenInjected: door coherence pack\n"
            "do\n"
            "    -- injected body\n"
            "end\n"
        )
        out, stripped = _strip_legacy_door_tween_block(src)
        assert stripped is True
        # The legitimate body must survive.
        assert "local foo = 1" in out
        assert "return foo" in out
        # The injected block must be gone.
        assert _LEGACY_DOOR_TWEEN_MARKER not in out
        assert "injected body" not in out
        # No trailing blank-line dangle from the marker's leading newline.
        assert not out.endswith("\n\n")

    def test_idempotent(self) -> None:
        src = (
            "local foo = 1\n"
            "-- _AutoFpsDoorTweenInjected: x\n"
            "do end\n"
        )
        once, _ = _strip_legacy_door_tween_block(src)
        twice, stripped_again = _strip_legacy_door_tween_block(once)
        assert stripped_again is False
        assert once == twice


class TestPruneLegacyGameplayArtifacts:
    """End-to-end tests against the Pipeline method using a duck-typed
    state harness (no real Unity project needed)."""

    def _harness(self) -> types.SimpleNamespace:
        place = types.SimpleNamespace(
            scripts=[],
            workspace_parts=[],
            replicated_templates=[],
            screen_guis=[],
        )
        state = types.SimpleNamespace(rbx_place=place)
        return types.SimpleNamespace(state=state)

    def test_removes_global_damage_router_script(self) -> None:
        h = self._harness()
        survivor = types.SimpleNamespace(name="OtherScript", source="local x=1")
        target = types.SimpleNamespace(
            name=_LEGACY_DAMAGE_ROUTER_NAME, source="-- legacy",
        )
        h.state.rbx_place.scripts = [survivor, target]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert h.state.rbx_place.scripts == [survivor], (
            "_AutoDamageEventRouter not removed from global scripts."
        )

    def test_removes_part_bound_damage_router_script(self) -> None:
        h = self._harness()
        target = types.SimpleNamespace(
            name=_LEGACY_DAMAGE_ROUTER_NAME, source="-- legacy",
        )
        survivor = types.SimpleNamespace(name="OK", source="local x=1")
        part = types.SimpleNamespace(
            name="Workspace.Door",
            scripts=[target, survivor],
            children=[],
        )
        h.state.rbx_place.workspace_parts = [part]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert part.scripts == [survivor], (
            "_AutoDamageEventRouter not removed from a part-bound "
            "script list."
        )

    def test_strips_door_tween_block_from_global_script(self) -> None:
        h = self._harness()
        door = types.SimpleNamespace(
            name="Door",
            source=(
                "-- AI-transpiled Door body\n"
                "local Door = {}\n"
                "return Door\n"
                "\n"
                "-- _AutoFpsDoorTweenInjected: door coherence pack\n"
                "do\n"
                "    -- legacy tween body\n"
                "end\n"
            ),
        )
        h.state.rbx_place.scripts = [door]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert _LEGACY_DOOR_TWEEN_MARKER not in door.source
        assert "local Door = {}" in door.source

    def test_strips_door_tween_block_from_template_bound_script(self) -> None:
        """Replicated-template walk: the rehydrate path attaches
        adapter stubs (and prior legacy artifacts) to prefab template
        scripts. The prune must reach them."""
        h = self._harness()
        door = types.SimpleNamespace(
            name="Door",
            source=(
                "local Door = {}\n"
                "-- _AutoFpsDoorTweenInjected\n"
                "do end\n"
            ),
        )
        template = types.SimpleNamespace(
            name="Templates.SciFi_Door",
            scripts=[door],
            children=[],
        )
        h.state.rbx_place.replicated_templates = [template]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert _LEGACY_DOOR_TWEEN_MARKER not in door.source

    def test_removes_auto_fps_hud_screen_gui(self) -> None:
        h = self._harness()
        survivor = types.SimpleNamespace(
            name="UserHUD", attributes={},
        )
        target = types.SimpleNamespace(
            name="HUD",
            attributes={_LEGACY_FPS_HUD_ATTR: True},
        )
        h.state.rbx_place.screen_guis = [survivor, target]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert h.state.rbx_place.screen_guis == [survivor], (
            "_AutoFpsHud ScreenGui not removed."
        )

    def test_aggregates_all_three_surfaces(self) -> None:
        """A single re-conversion can hit all three at once; pin the
        aggregated count + final state."""
        h = self._harness()
        router = types.SimpleNamespace(
            name=_LEGACY_DAMAGE_ROUTER_NAME, source="",
        )
        door = types.SimpleNamespace(
            name="Door",
            source=(
                "local Door = {}\n"
                "-- _AutoFpsDoorTweenInjected\n"
                "do end\n"
            ),
        )
        hud = types.SimpleNamespace(
            name="HUD",
            attributes={_LEGACY_FPS_HUD_ATTR: True},
        )
        h.state.rbx_place.scripts = [router, door]
        h.state.rbx_place.screen_guis = [hud]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 3, (
            "Expected one prune count per artifact (router + door "
            "block + HUD)."
        )
        assert h.state.rbx_place.scripts == [door], (
            "Door script must SURVIVE the prune — only its injected "
            "block is stripped."
        )
        assert _LEGACY_DOOR_TWEEN_MARKER not in door.source
        assert h.state.rbx_place.screen_guis == []

    def test_no_op_when_clean(self) -> None:
        h = self._harness()
        clean = types.SimpleNamespace(name="OK", source="local x=1")
        h.state.rbx_place.scripts = [clean]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 0
        assert h.state.rbx_place.scripts == [clean]

    def test_no_op_on_none_rbx_place(self) -> None:
        h = types.SimpleNamespace(
            state=types.SimpleNamespace(rbx_place=None),
        )
        # Must not raise.
        assert Pipeline._prune_legacy_gameplay_artifacts(h) == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
