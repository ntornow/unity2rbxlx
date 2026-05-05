"""Tests for the script_coherence_packs registry and the FPS pickup pack.

Catches three classes of regression:
  1. Registry plumbing — duplicate names raise, dependency cycles raise,
     unknown ``after=`` references raise.
  2. Pack ordering — a pack declaring after=("X",) runs after X.
  3. Behavior — the FPS rifle pack injects the correct Luau into a synthetic
     script matching the post-AI-transpile stub, AND does NOT inject into
     scripts from a non-FPS project (Gamekit3D-style).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.roblox_types import RbxScript
from converter import script_coherence_packs as packs_module
from converter.script_coherence_packs import (
    PatchPack,
    _topological_order,
    patch_pack,
    run_packs,
)

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def isolated_registry() -> "Generator[list[PatchPack], None, None]":
    """Snapshot/restore the module-level registry so tests can register
    ad-hoc packs without leaking into other tests."""
    saved = list(packs_module._REGISTRY)
    packs_module._REGISTRY.clear()
    try:
        yield packs_module._REGISTRY
    finally:
        packs_module._REGISTRY.clear()
        packs_module._REGISTRY.extend(saved)


def _stub_apply(scripts: list[RbxScript]) -> int:
    return 0


class TestRegistry:
    def test_decorator_registers_pack(self, isolated_registry) -> None:
        @patch_pack(name="my_pack", detect=lambda _s: False)
        def my_apply(scripts: list[RbxScript]) -> int:
            return 0

        assert any(p.name == "my_pack" for p in isolated_registry)

    def test_duplicate_name_raises(self, isolated_registry) -> None:
        @patch_pack(name="dup", detect=lambda _s: False)
        def first(scripts: list[RbxScript]) -> int:
            return 0

        with pytest.raises(ValueError, match="already registered"):
            @patch_pack(name="dup", detect=lambda _s: False)
            def second(scripts: list[RbxScript]) -> int:
                return 0


class TestTopologicalOrder:
    def test_no_deps_preserves_registration_order(self) -> None:
        a = PatchPack("a", "", (), lambda _: False, _stub_apply)
        b = PatchPack("b", "", (), lambda _: False, _stub_apply)
        result = _topological_order([a, b])
        assert [p.name for p in result] == ["a", "b"]

    def test_after_constrains_order(self) -> None:
        # b declared first but says it must run after a
        a = PatchPack("a", "", (), lambda _: False, _stub_apply)
        b = PatchPack("b", "", ("a",), lambda _: False, _stub_apply)
        result = _topological_order([b, a])
        order = [p.name for p in result]
        assert order.index("a") < order.index("b")

    def test_unknown_dependency_raises(self) -> None:
        a = PatchPack("a", "", ("nonexistent",), lambda _: False, _stub_apply)
        with pytest.raises(ValueError, match="no such pack is registered"):
            _topological_order([a])

    def test_cycle_raises(self) -> None:
        a = PatchPack("a", "", ("b",), lambda _: False, _stub_apply)
        b = PatchPack("b", "", ("a",), lambda _: False, _stub_apply)
        with pytest.raises(ValueError, match="cycle"):
            _topological_order([a, b])


class TestRunPacksGating:
    """run_packs() respects detect(), enabled, and disabled."""

    def test_detector_gates_pack(self, isolated_registry) -> None:
        """If detect() returns False, apply() is never called."""
        called = []

        @patch_pack(name="gated", detect=lambda _s: False)
        def my_apply(scripts: list[RbxScript]) -> int:
            called.append(True)
            return 1

        run_packs([])
        assert not called

    def test_detector_true_runs_pack(self, isolated_registry) -> None:
        called = []

        @patch_pack(name="ungated", detect=lambda _s: True)
        def my_apply(scripts: list[RbxScript]) -> int:
            called.append(True)
            return 7

        total = run_packs([])
        assert called
        assert total == 7

    def test_disabled_skips_even_when_detector_true(
        self, isolated_registry,
    ) -> None:
        called = []

        @patch_pack(name="optional", detect=lambda _s: True)
        def my_apply(scripts: list[RbxScript]) -> int:
            called.append(True)
            return 1

        total = run_packs([], disabled={"optional"})
        assert not called
        assert total == 0

    def test_enabled_overrides_detector(self, isolated_registry) -> None:
        """When `enabled` is given, listed packs run regardless of detect()."""
        called = []

        @patch_pack(name="forced", detect=lambda _s: False)
        def my_apply(scripts: list[RbxScript]) -> int:
            called.append(True)
            return 3

        total = run_packs([], enabled={"forced"})
        assert called
        assert total == 3

    def test_unknown_enabled_name_raises(self, isolated_registry) -> None:
        @patch_pack(name="exists", detect=lambda _s: False)
        def my_apply(scripts: list[RbxScript]) -> int:
            return 0

        with pytest.raises(ValueError, match="unknown patch_pack name"):
            run_packs([], enabled={"typo_name"})

    def test_run_order_respects_after(self, isolated_registry) -> None:
        order = []

        @patch_pack(name="first_pack", detect=lambda _s: True)
        def first(scripts: list[RbxScript]) -> int:
            order.append("first")
            return 0

        @patch_pack(
            name="second_pack",
            after=("first_pack",),
            detect=lambda _s: True,
        )
        def second(scripts: list[RbxScript]) -> int:
            order.append("second")
            return 0

        run_packs([])
        assert order == ["first", "second"]


class TestFpsRifleDetector:
    """The fps_rifle_inject pack must auto-enable on FPS projects and
    auto-disable on non-FPS projects. Both directions are critical:
    enabling on Gamekit3D would inject FPS code into RPG scripts."""

    def test_detects_simplefps_pattern(self) -> None:
        scripts = [
            RbxScript(
                name="Player",
                source="local function GetRifle() end",
                script_type="LocalScript",
            ),
        ]
        assert packs_module._detect_fps_rifle_pickup(scripts) is True

    def test_detects_riflePrefab_reference(self) -> None:
        scripts = [
            RbxScript(
                name="Other",
                source='workspace:FindFirstChild("riflePrefab")',
                script_type="Script",
            ),
        ]
        assert packs_module._detect_fps_rifle_pickup(scripts) is True

    def test_does_not_detect_on_non_fps(self) -> None:
        """Gamekit3D-style scripts should not trigger the pack."""
        scripts = [
            RbxScript(
                name="EnemyAI", source="local hp = 100", script_type="Script",
            ),
            RbxScript(
                name="Pickup", source="local m = {} return m",
                script_type="ModuleScript",
            ),
        ]
        assert packs_module._detect_fps_rifle_pickup(scripts) is False


class TestFpsRifleInjection:
    """The pack rewrites a stub GetRifle into the working version. Without
    this, the SimpleFPS rifle is invisible/broken — same regression that
    motivated the original Pass 14 in script_coherence."""

    def _stub_player_script(self) -> RbxScript:
        """Synthetic script matching the post-AI-transpile stub the
        injector targets."""
        return RbxScript(
            name="Player",
            source=(
                "local character = nil\n"
                "local gotWeapon = false\n"
                "local controls = {\n"
                "    GetRifle = function()\n"
                "        -- AI stub does not actually clone the rifle\n"
                "        gotWeapon = true\n"
                "    end,\n"
                "}\n"
                "RunService.RenderStepped:Connect(function(dt)\n"
                "end)\n"
                "function getItem(name) end\n"
            ),
            script_type="LocalScript",
        )

    def test_injection_adds_rifle_clone_logic(self) -> None:
        s = self._stub_player_script()
        fixes = packs_module._inject_fps_rifle_system([s])
        assert fixes == 1
        assert '_fpsRifle' in s.source
        assert 'rp:Clone' in s.source
        assert 'PivotTo(workspace.CurrentCamera.CFrame' in s.source

    def test_injection_marker_prevents_double_apply(self) -> None:
        s = self._stub_player_script()
        first = packs_module._inject_fps_rifle_system([s])
        second = packs_module._inject_fps_rifle_system([s])
        assert first == 1
        assert second == 0  # marker prevents re-injection

    def test_run_packs_invokes_pack_on_simplefps_fixture(self) -> None:
        """End-to-end through run_packs: the pack must auto-detect and
        actually mutate the stub."""
        s = self._stub_player_script()
        fixes = run_packs([s])
        assert fixes >= 1
        assert '_fpsRifle' in s.source

    def test_run_packs_does_not_inject_on_unrelated_scripts(self) -> None:
        """No FPS-shaped content → no mutation. This is the protection
        against polluting other projects."""
        s = RbxScript(
            name="GameLogic",
            source="local enemies = {} return enemies",
            script_type="ModuleScript",
        )
        original = s.source
        run_packs([s])
        assert s.source == original


class TestPickupVisualTargetPack:
    def test_detects_pickup_with_rotation_pattern(self) -> None:
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    "local rotationSpeed = 100\n"
                    "local function moveDown() end\n"
                    "Touched:Connect(function() GetItem('x') end)\n"
                ),
                script_type="Script",
            ),
        ]
        assert packs_module._detect_pickup_visual_target(scripts) is True

    def test_does_not_replace_unrelated_pickup_scripts(self) -> None:
        """A script named Pickup but missing the AI-stub markers must NOT
        be rewritten."""
        s = RbxScript(
            name="Pickup",
            source="local m = {}; return m",
            script_type="ModuleScript",
        )
        original = s.source
        packs_module._fix_pickup_visual_target([s])
        assert s.source == original


class TestFpsDefaultControlsPack:
    """The fps_default_controls_off pack auto-enables when any LocalScript
    locks the mouse — the unmistakable signature of an FPS controller."""

    def test_detects_lock_center(self) -> None:
        scripts = [
            RbxScript(
                name="FpsController",
                source="UIS.MouseBehavior = Enum.MouseBehavior.LockCenter",
                script_type="LocalScript",
            ),
        ]
        assert packs_module._detect_fps_default_controls(scripts) is True

    def test_does_not_match_server_scripts(self) -> None:
        """Lock-center on a Server script is meaningless — the pack should
        still skip if no LocalScript matches."""
        scripts = [
            RbxScript(
                name="ServerCode",
                source="UIS.MouseBehavior = Enum.MouseBehavior.LockCenter",
                script_type="Script",
            ),
        ]
        assert packs_module._detect_fps_default_controls(scripts) is False

    def test_does_not_match_non_fps_localscripts(self) -> None:
        scripts = [
            RbxScript(
                name="MenuClient",
                source="local x = 1\nprint('hi')",
                script_type="LocalScript",
            ),
        ]
        assert packs_module._detect_fps_default_controls(scripts) is False

    def test_inject_prepends_setup_block(self) -> None:
        s = RbxScript(
            name="FpsController",
            source=(
                "local UIS = game:GetService('UserInputService')\n"
                "UIS.MouseBehavior = Enum.MouseBehavior.LockCenter\n"
            ),
            script_type="LocalScript",
        )
        fixes = packs_module._disable_default_controls_in_fps_scripts([s])
        assert fixes == 1
        assert "-- u2r: disable default PlayerModule controls" in s.source
        assert s.source.endswith(
            "UIS.MouseBehavior = Enum.MouseBehavior.LockCenter\n"
        )

    def test_inject_idempotent(self) -> None:
        s = RbxScript(
            name="FpsController",
            source="UIS.MouseBehavior = Enum.MouseBehavior.LockCenter\n",
            script_type="LocalScript",
        )
        first = packs_module._disable_default_controls_in_fps_scripts([s])
        second = packs_module._disable_default_controls_in_fps_scripts([s])
        assert first == 1
        assert second == 0  # marker prevents re-injection


class TestTriggerStayPollingPack:
    """The trigger_stay_polling pack auto-enables on the converter-emitted
    turret AI pattern (triggerCollider + getTBase + sightRadius)."""

    def _turret_script(self) -> RbxScript:
        return RbxScript(
            name="TurretAI",
            source=(
                "local triggerCollider = script.Parent\n"
                "function getTBase() return script.Parent end\n"
                "function getSightRadius() return 50 end\n"
                "function startEngaged(t) end\n"
                "if angle < 55 then startEngaged(target) end\n"
            ),
            script_type="Script",
        )

    def test_detects_turret_pattern(self) -> None:
        assert packs_module._detect_trigger_stay_polling(
            [self._turret_script()],
        ) is True

    def test_does_not_detect_unrelated_scripts(self) -> None:
        s = RbxScript(name="Util", source="local x = 1", script_type="Script")
        assert packs_module._detect_trigger_stay_polling([s]) is False

    def test_does_not_detect_partial_match(self) -> None:
        """A script with triggerCollider but missing the helper functions
        must NOT trigger — avoids polluting non-turret scripts that
        happen to mention triggerCollider."""
        s = RbxScript(
            name="Other",
            source="local triggerCollider = nil\nif angle < 90 then ... end",
            script_type="Script",
        )
        assert packs_module._detect_trigger_stay_polling([s]) is False

    def test_inject_appends_polling_loop(self) -> None:
        s = self._turret_script()
        fixes = packs_module._add_trigger_stay_polling([s])
        assert fixes == 1
        assert "-- __TRIGGER_STAY_POLL__" in s.source
        assert "RunService.Heartbeat:Connect" in s.source

    def test_inject_idempotent(self) -> None:
        s = self._turret_script()
        first = packs_module._add_trigger_stay_polling([s])
        second = packs_module._add_trigger_stay_polling([s])
        assert first == 1
        assert second == 0
