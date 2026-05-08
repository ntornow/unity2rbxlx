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


class TestPickupRemoteEventServerAttr:
    """The ``pickup_remote_event_server`` pack rewrites
    ``character:SetAttribute("GetItem", itemName)`` to fire the
    ``PickupItemEvent`` RemoteEvent — and ALSO writes
    ``player:SetAttribute("has"..itemName, true)`` server-side so doors
    and other server scripts can read the gameplay flag. The combined
    invariant existed only inside ``_PICKUP_REPLACEMENT`` previously, and
    that template was gated behind a detector that ``pickup_remote_event_server``
    itself was disabling — so the server-attr write silently dropped.
    These tests pin both invariants on the canonical AI-transpile shape.
    """

    def _ai_transpiled_pickup(self) -> RbxScript:
        return RbxScript(
            name="Pickup",
            source=(
                'local Players = game:GetService("Players")\n'
                'local itemName = script:GetAttribute("itemName") or ""\n'
                'triggerPart.Touched:Connect(function(otherPart)\n'
                '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                '    if not character then return end\n'
                '    local player = Players:GetPlayerFromCharacter(character)\n'
                '    if not player then return end\n'
                '    character:SetAttribute("GetItem", itemName)\n'
                '    container:Destroy()\n'
                'end)\n'
            ),
            script_type="Script",
        )

    def test_fires_remote_event(self) -> None:
        s = self._ai_transpiled_pickup()
        packs_module._convert_pickup_to_remote_event([s])
        assert "PickupItemEvent" in s.source
        assert "FireClient(_pl, itemName)" in s.source
        # The original SetAttribute call must be gone — leaving it would
        # leak ``GetItem`` onto the character (server-side, but only the
        # client-side Player listener used to read it).
        assert 'SetAttribute("GetItem", itemName)' not in s.source

    def test_writes_server_side_player_attribute(self) -> None:
        """The pack must inject ``_pl:SetAttribute("has"..itemName, true)``
        BEFORE ``FireClient``. Server-side Player Object attribute writes
        replicate to the client; the client-side Player.luau read in
        ``hasKey`` was correct already, but the SERVER-side Door read of
        ``player:GetAttribute("hasKey")`` saw nil before this fix.
        """
        s = self._ai_transpiled_pickup()
        packs_module._convert_pickup_to_remote_event([s])
        assert '_pl:SetAttribute("has" .. itemName, true)' in s.source
        # Order matters: write the attribute, then fire the event.
        attr_idx = s.source.index('_pl:SetAttribute("has" .. itemName')
        fire_idx = s.source.index("FireClient(_pl, itemName)")
        assert attr_idx < fire_idx, (
            "server-attr write must precede FireClient so a server-side "
            "Door listener seeing a same-frame attribute read on the "
            "Touched signal observes the flag flip"
        )

    def test_skips_empty_itemname_at_runtime(self) -> None:
        """The injected SetAttribute is guarded by ``itemName ~= ""`` so
        a pickup with no itemName attribute doesn't write a useless
        ``has`` attribute (the empty-string concat would still produce
        ``"has"`` as the key)."""
        s = self._ai_transpiled_pickup()
        packs_module._convert_pickup_to_remote_event([s])
        assert 'itemName and itemName ~= ""' in s.source


class TestDoorGlobalPlayerToAttribute:
    """The ``door_global_player_to_attribute`` pack catches Door scripts
    where the AI transpiler emitted a server-incompatible
    ``_G.Player.hasKey`` lookup and rewrites it to read the replicated
    Player attribute that ``pickup_remote_event_server`` writes.

    Three transpile shapes seen in the wild are pinned here: helper that
    if-checks then returns, helper that returns truthy-and, and inline
    if-guards inside Touched. The fast detector flips on any
    ``_G.Player`` substring so every shape gets considered.
    """

    @staticmethod
    def _door_with_helper() -> RbxScript:
        return RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'local function getPlayerHasKey()\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        return _G.Player.hasKey()\n'
                '    end\n'
                '    return false\n'
                'end\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if getPlayerHasKey() then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )

    @staticmethod
    def _door_with_return_helper() -> RbxScript:
        return RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'local function getPlayerHasKey()\n'
                '    return _G.Player and _G.Player.hasKey or false\n'
                'end\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if getPlayerHasKey() then toggleDoor(true) end\n'
                'end)\n'
            ),
            script_type="Script",
        )

    @staticmethod
    def _door_with_inline_guard() -> RbxScript:
        return RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )

    def test_detector_matches_any_g_player_reference(self) -> None:
        for factory in (
            self._door_with_helper,
            self._door_with_return_helper,
            self._door_with_inline_guard,
        ):
            assert packs_module._detect_door_global_player_lookup(
                [factory()]
            ) is True

    def test_detector_skips_clean_doors(self) -> None:
        """A Door already reading ``player:GetAttribute("hasKey")`` must
        NOT trigger the rewrite — re-running the pack should be a no-op."""
        s = RbxScript(
            name="Door",
            source=(
                'local function getPlayerHasKey(part)\n'
                '    local m = part and part:FindFirstAncestorOfClass("Model")\n'
                '    local p = m and Players:GetPlayerFromCharacter(m)\n'
                '    return p and p:GetAttribute("hasKey")\n'
                'end\n'
            ),
            script_type="Script",
        )
        assert packs_module._detect_door_global_player_lookup([s]) is False

    def test_detector_skips_non_door_scripts(self) -> None:
        """Other scripts may legitimately use ``_G.Player`` — the pack
        is gated on ``s.name == "Door"`` to avoid touching them."""
        s = RbxScript(
            name="HudControl",
            source="_G.Player = { hasKey = false }",
            script_type="LocalScript",
        )
        assert packs_module._detect_door_global_player_lookup([s]) is False

    def test_helper_form_rewrites_to_player_attribute(self) -> None:
        s = self._door_with_helper()
        packs_module._fix_door_global_player_lookup([s])
        assert "_G.Player" not in s.source
        assert 'getPlayerHasKey(_part)' in s.source  # helper now takes a part
        assert 'GetPlayerFromCharacter(_model)' in s.source
        assert ':GetAttribute("hasKey")' in s.source
        # Call sites must pass ``other`` from the Touched callback.
        assert "getPlayerHasKey(other)" in s.source
        assert "getPlayerHasKey()" not in s.source

    def test_return_helper_form_rewrites_to_player_attribute(self) -> None:
        s = self._door_with_return_helper()
        packs_module._fix_door_global_player_lookup([s])
        assert "_G.Player" not in s.source
        assert ':GetAttribute("hasKey")' in s.source
        assert "getPlayerHasKey(other)" in s.source

    def test_inline_guard_rewrites_to_player_attribute(self) -> None:
        s = self._door_with_inline_guard()
        packs_module._fix_door_global_player_lookup([s])
        assert "_G.Player" not in s.source
        assert ':GetAttribute("hasKey")' in s.source
        # Inline guard becomes a self-invoking lambda; ``other`` is the
        # Touched callback's parameter.
        assert 'other and other:FindFirstAncestorOfClass("Model")' in s.source

    def test_idempotent(self) -> None:
        s = self._door_with_helper()
        packs_module._fix_door_global_player_lookup([s])
        first_pass = s.source
        packs_module._fix_door_global_player_lookup([s])
        assert s.source == first_pass

    def test_preserves_attribute_name_for_non_key_doors(self) -> None:
        """The pack captures ``has<X>`` from the original source — a door
        that reads ``hasMagicWand`` produces ``GetAttribute("hasMagicWand")``,
        not a hardcoded ``hasKey``."""
        s = RbxScript(
            name="Door",
            source=(
                'local function probe()\n'
                '    if _G.Player and _G.Player.hasMagicWand then\n'
                '        return _G.Player.hasMagicWand()\n'
                '    end\n'
                '    return false\n'
                'end\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        assert 'GetAttribute("hasMagicWand")' in s.source
        assert 'GetAttribute("hasKey")' not in s.source


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
