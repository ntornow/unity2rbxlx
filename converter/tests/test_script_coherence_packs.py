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

    def test_injects_has_attr_when_unrelated_has_attribute_initialized(self) -> None:
        """Codex finding [P1] (round 7): a Pickup that initializes an
        unrelated has-flag (e.g. ``player:SetAttribute("hasKey", false)``
        for default state) should still get the dynamic-concat write
        injected before FireClient. The previous detector substring
        check ``'SetAttribute("has"' not in src`` would false-skip
        these Pickups.
        """
        s = RbxScript(
            name="Pickup",
            source=(
                'local _pe = game:GetService("ReplicatedStorage")'
                ':FindFirstChild("PickupItemEvent")\n'
                '-- Initialize default state (unrelated to the dynamic write):\n'
                'local function _resetPlayer(p) p:SetAttribute("hasKey", false) end\n'
                'triggerPart.Touched:Connect(function(otherPart)\n'
                '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                '    local player = game:GetService("Players"):GetPlayerFromCharacter(character)\n'
                '    if _pe and player then _pe:FireClient(player, itemName) end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        # Detector should still fire even though _resetPlayer mentions
        # SetAttribute("hasKey", false).
        assert packs_module._detect_pickup_setattribute_pattern([s]) is True
        packs_module._convert_pickup_to_remote_event([s])
        # The exact dynamic-concat pattern is injected.
        assert ':SetAttribute("has" .. itemName, true)' in s.source

    def test_injects_has_attr_into_direct_fireclient_pickups(self) -> None:
        """Codex finding [P1] (round 6): a Pickup that already uses
        ``PickupItemEvent:FireClient(...)`` directly (e.g. the
        canonical ``_PICKUP_REPLACEMENT`` body, or a hand-written
        Pickup) skips the legacy SetAttribute → FireClient rewrite.
        Without this fix, those Pickups never write the server-side
        ``has<X>`` flag, and ``door_global_player_to_attribute``
        rewrites Door to read an attribute nobody writes — every key
        door stays permanently locked.

        The fix injects the SetAttribute write before each FireClient
        call when the Pickup uses FireClient but doesn't already carry
        ``SetAttribute("has"...)``.
        """
        s = RbxScript(
            name="Pickup",
            source=(
                'local _pe = game:GetService("ReplicatedStorage")'
                ':FindFirstChild("PickupItemEvent")\n'
                'triggerPart.Touched:Connect(function(otherPart)\n'
                '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                '    local player = game:GetService("Players"):GetPlayerFromCharacter(character)\n'
                '    if _pe and player then _pe:FireClient(player, itemName) end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._convert_pickup_to_remote_event([s])
        assert ':SetAttribute("has" .. itemName, true)' in s.source
        # Order: SetAttribute write must precede FireClient.
        attr_idx = s.source.index('SetAttribute("has" .. itemName')
        fire_idx = s.source.index('FireClient(player, itemName)')
        assert attr_idx < fire_idx

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
        # Inline guard becomes a self-invoking lambda; the lambda must
        # reference whichever name the surrounding Touched callback used
        # (this fixture uses ``other``).
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
                'local Players = game:GetService("Players")\n'
                'local function probe()\n'
                '    if _G.Player and _G.Player.hasMagicWand then\n'
                '        return _G.Player.hasMagicWand()\n'
                '    end\n'
                '    return false\n'
                'end\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if probe() then return end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        assert 'GetAttribute("hasMagicWand")' in s.source
        assert 'GetAttribute("hasKey")' not in s.source

    # ------------------------------------------------------------------
    # Codex review findings — pin against regressions
    # ------------------------------------------------------------------

    def test_helper_call_site_uses_otherPart_when_callback_does(self) -> None:
        """Codex finding [P1]: api_mappings.py emits
        ``Connect(function(otherPart)`` for OnTrigger*/OnCollision*
        handlers. When the Door uses the otherPart convention, the
        rewritten ``getPlayerHasKey()`` call site must pass ``otherPart``,
        not the hardcoded ``other``. Otherwise ``_part`` is nil at runtime
        and the helper always returns false → door stays closed forever.
        """
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'local function getPlayerHasKey()\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        return _G.Player.hasKey()\n'
                '    end\n'
                '    return false\n'
                'end\n'
                'triggerPart.Touched:Connect(function(otherPart)\n'
                '    if getPlayerHasKey() then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        assert "getPlayerHasKey(otherPart)" in s.source
        assert "getPlayerHasKey(other)" not in s.source

    def test_inline_guard_uses_otherPart_when_callback_does(self) -> None:
        """Codex finding [P1]: same callback-name issue for inline guards.
        The IIFE must read ``otherPart`` when the surrounding callback
        uses that name; otherwise the lambda references an undefined
        variable, the condition is always falsy, and ``toggleDoor(...)``
        is unreachable.
        """
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'triggerPart.Touched:Connect(function(otherPart)\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        assert 'otherPart and otherPart:FindFirstAncestorOfClass("Model")' in s.source
        # Must not leak the wrong name into the IIFE.
        assert 'other and other:FindFirstAncestorOfClass' not in s.source

    def test_injects_players_service_binding_when_missing(self) -> None:
        """Codex finding [P1]: a Door that previously only used
        ``_G.Player.hasKey()`` may have skipped the
        ``local Players = game:GetService("Players")`` binding entirely.
        The rewrite calls ``Players:GetPlayerFromCharacter`` and would
        crash on the first Touched without the binding. The pack must
        prepend the binding when missing.
        """
        s = RbxScript(
            name="Door",
            source=(
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
        packs_module._fix_door_global_player_lookup([s])
        assert 'local Players = game:GetService("Players")' in s.source
        # And it must come before the helper that uses it — at file top.
        binding_idx = s.source.index('local Players = game:GetService("Players")')
        helper_idx = s.source.index('Players:GetPlayerFromCharacter')
        assert binding_idx < helper_idx

    def test_nested_players_binding_does_not_satisfy_top_level_check(self) -> None:
        """Codex finding [P2] (round 5): a Door that binds ``Players``
        only inside a nested function/callback shouldn't count as
        "already bound" — the outer helper rewrite calls
        ``Players:GetPlayerFromCharacter`` from outer scope, where the
        nested local isn't visible. The pack must inject a top-level
        binding even when a nested one exists.
        """
        s = RbxScript(
            name="Door",
            source=(
                'local function getPlayerHasKey()\n'
                '    -- Nested binding only — not visible at file scope.\n'
                '    local Players = game:GetService("Players")\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        return _G.Player.hasKey()\n'
                '    end\n'
                '    return false\n'
                'end\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if getPlayerHasKey() then toggleDoor(true) end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        # Top-level Players binding must be inserted: the rewritten
        # helper calls Players:GetPlayerFromCharacter at file scope.
        first_line = s.source.split('\n', 1)[0]
        assert first_line == 'local Players = game:GetService("Players")', (
            "top-level Players binding required even when a nested "
            "binding exists — the helper rewrite calls Players from "
            "outer scope"
        )

    def test_does_not_double_inject_players_service(self) -> None:
        """If Players is already bound (the SimpleFPS shape), don't
        prepend a second binding — that would shadow nothing harmful but
        adds a noise line and breaks idempotence guarantees."""
        s = self._door_with_helper()  # already has Players binding
        original_count = s.source.count('local Players = game:GetService("Players")')
        assert original_count == 1
        packs_module._fix_door_global_player_lookup([s])
        assert s.source.count('local Players = game:GetService("Players")') == 1


class TestPickupRemoteEventDetectorIsGenreAgnostic:
    """Codex finding [P2]: ``door_global_player_to_attribute`` only works
    if ``pickup_remote_event_server`` runs to write the replicated
    server-side ``has<X>`` attribute. The Pickup pack used to gate on
    ``_detect_fps_rifle_pickup``, which fires only on rifle markers
    (``riflePrefab``/``GetRifle``). A non-FPS project with key doors
    would have the Door rewritten to read ``GetAttribute("hasKey")`` on
    a flag nobody writes — leaving every key door permanently locked.

    The fix swaps the detector to ``_detect_pickup_setattribute_pattern``,
    which fires on the pattern the pack actually rewrites.
    """

    def test_detector_fires_without_rifle_markers(self) -> None:
        """A pickup project with a Pickup script and a key (no rifle
        anywhere) should still trigger the pack."""
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    'triggerPart.Touched:Connect(function(otherPart)\n'
                    '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                    '    character:SetAttribute("GetItem", itemName)\n'
                    'end)\n'
                ),
                script_type="Script",
            ),
            # No Player.luau, no riflePrefab — pure RPG-style pickup.
        ]
        assert packs_module._detect_pickup_setattribute_pattern(scripts) is True

    def test_detector_skips_non_pickup_setattribute_calls(self) -> None:
        """A non-Pickup script that happens to call ``SetAttribute``
        with similar shape must NOT trigger the pack — the rewrite is
        Pickup-specific."""
        scripts = [
            RbxScript(
                name="Inventory",
                source=(
                    'character:SetAttribute("GetItem", itemName)\n'
                ),
                script_type="Script",
            ),
        ]
        assert packs_module._detect_pickup_setattribute_pattern(scripts) is False

    def test_detector_skips_pickup_already_using_remote_event(self) -> None:
        """A Pickup that's already been processed (no SetAttribute call
        anymore, just FireClient) should not re-trigger — the pack is
        idempotent and re-running it must be a no-op."""
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    'pickupEvent:FireClient(player, itemName)\n'
                    'container:Destroy()\n'
                ),
                script_type="Script",
            ),
        ]
        assert packs_module._detect_pickup_setattribute_pattern(scripts) is False

    def test_client_listener_detector_fires_after_server_pack_runs(self) -> None:
        """Codex finding [P1] (round 2): broadening the server pack must
        broaden the client pack too. Detectors run lazily inside
        ``run_packs``, so by the time ``pickup_remote_event_client``'s
        detector fires, ``pickup_remote_event_server`` has already
        rewritten the Pickup to use ``PickupItemEvent``. The client
        detector must look for the post-rewrite shape, NOT the
        pre-rewrite ``SetAttribute`` pattern.
        """
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    'local _pe = game:GetService("ReplicatedStorage")'
                    ':FindFirstChild("PickupItemEvent")\n'
                    'if _pe and _pl then _pe:FireClient(_pl, itemName) end\n'
                ),
                script_type="Script",
            ),
        ]
        assert packs_module._detect_pickup_remote_event_in_use(scripts) is True

    def test_client_listener_skips_server_scripts(self) -> None:
        """Codex finding [P2] (round 3): ``OnClientEvent`` is client-only.
        A server ``Script`` that happens to define a ``GetItem`` helper
        for its own purposes must not get the listener installed —
        ``RemoteEvent.OnClientEvent:Connect`` would crash on the server.
        Restrict the install to scripts classified as ``LocalScript``.
        """
        scripts = [
            # Pre-rewrite Pickup so the server pack fires.
            RbxScript(
                name="Pickup",
                source=(
                    'triggerPart.Touched:Connect(function(otherPart)\n'
                    '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                    '    character:SetAttribute("GetItem", itemName)\n'
                    'end)\n'
                ),
                script_type="Script",
            ),
            # A SERVER script with its own GetItem helper — must be skipped.
            RbxScript(
                name="LootDispenser",
                source=(
                    'local function GetItem(name)\n'
                    '    -- server-side inventory dispatch, not a client controller\n'
                    '    return inventory[name]\n'
                    'end\n'
                ),
                script_type="Script",
            ),
        ]
        run_packs(scripts)
        loot = scripts[1]
        assert "PickupItemEvent" not in loot.source, (
            "client listener must not install in a server Script — "
            "OnClientEvent is client-only and would crash on the server"
        )

    def test_client_listener_skips_module_scripts(self) -> None:
        """Codex finding [P2] (round 3): same applies to shared
        ModuleScripts that happen to define ``getItem``. The runtime
        context for a ModuleScript depends on its caller, but
        OnClientEvent is unsafe to install unconditionally."""
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    'triggerPart.Touched:Connect(function(otherPart)\n'
                    '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                    '    character:SetAttribute("GetItem", itemName)\n'
                    'end)\n'
                ),
                script_type="Script",
            ),
            RbxScript(
                name="InventoryUtil",
                source='local M = {}\nfunction M.getItem(name) end\nreturn M\n',
                script_type="ModuleScript",
            ),
        ]
        run_packs(scripts)
        util = scripts[1]
        assert "PickupItemEvent" not in util.source

    def test_client_listener_installs_in_only_one_controller(self) -> None:
        """Codex finding [P2] (round 5): a project with multiple
        LocalScripts that match the ``getItem`` symbol must NOT get the
        listener installed in all of them — every pickup event would
        fire through every listener, double-applying item effects.

        The fix selects a single canonical target: prefer a script
        named ``Player``, otherwise the first LocalScript referencing
        ``LocalPlayer`` AND ``getItem``.
        """
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    'triggerPart.Touched:Connect(function(otherPart)\n'
                    '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                    '    character:SetAttribute("GetItem", itemName)\n'
                    'end)\n'
                ),
                script_type="Script",
            ),
            # Canonical Player controller — should get the listener.
            RbxScript(
                name="Player",
                source=(
                    'local Players = game:GetService("Players")\n'
                    'local LocalPlayer = Players.LocalPlayer\n'
                    'local function getItem(name)\n'
                    '    -- player controller dispatch\n'
                    'end\n'
                ),
                script_type="LocalScript",
            ),
            # Auxiliary UI script that also defines getItem — must be
            # skipped to avoid double-dispatch.
            RbxScript(
                name="InventoryUI",
                source=(
                    'local function GetItem(name)\n'
                    '    print("inventory got " .. name)\n'
                    'end\n'
                ),
                script_type="LocalScript",
            ),
        ]
        run_packs(scripts)
        player = scripts[1]
        ui = scripts[2]
        assert "PickupItemEvent" in player.source, (
            "canonical Player controller must get the listener"
        )
        assert "PickupItemEvent" not in ui.source, (
            "auxiliary UI script with getItem must NOT get a duplicate "
            "listener — would double-apply pickup effects"
        )

    def test_client_listener_picks_actual_controller_not_first_localplayer(self) -> None:
        """Codex finding [P2] (round 6): when no script is named
        ``Player`` and multiple LocalScripts mention ``LocalPlayer``,
        the previous tier-2 fallback returned the FIRST such script in
        registration order. A UI helper that happens to reference
        ``LocalPlayer`` (e.g. for player-name display) but doesn't
        define ``getItem`` would steal the listener install from the
        actual controller.

        The fix scores each candidate on player-controller signal
        density (LocalPlayer + Character + Humanoid + UserInputService)
        plus a strong boost for actually DEFINING ``getItem`` rather
        than just referencing it. The script that defines ``getItem``
        wins regardless of registration order.
        """
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    'triggerPart.Touched:Connect(function(otherPart)\n'
                    '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                    '    character:SetAttribute("GetItem", itemName)\n'
                    'end)\n'
                ),
                script_type="Script",
            ),
            # UI helper FIRST in registration order: references
            # LocalPlayer for display purposes and has a bare getItem
            # CALL but no DEFINITION.
            RbxScript(
                name="InventoryUI",
                source=(
                    'local LocalPlayer = game:GetService("Players").LocalPlayer\n'
                    'local function _refresh()\n'
                    '    print(LocalPlayer.Name, "has", getItem("count"))\n'
                    'end\n'
                ),
                script_type="LocalScript",
            ),
            # Actual controller SECOND: defines getItem and references
            # all the controller signals.
            RbxScript(
                name="PlayerClient",
                source=(
                    'local Players = game:GetService("Players")\n'
                    'local LocalPlayer = Players.LocalPlayer\n'
                    'local Character = LocalPlayer.Character\n'
                    'local Humanoid = Character and Character:FindFirstChildWhichIsA("Humanoid")\n'
                    'local UserInputService = game:GetService("UserInputService")\n'
                    'local function getItem(name)\n'
                    '    -- real controller dispatch\n'
                    'end\n'
                ),
                script_type="LocalScript",
            ),
        ]
        run_packs(scripts)
        ui = scripts[1]
        controller = scripts[2]
        assert "PickupItemEvent" in controller.source, (
            "actual controller (PlayerClient) must get the listener "
            "regardless of registration order"
        )
        assert "PickupItemEvent" not in ui.source, (
            "UI helper must NOT steal the listener even though it "
            "references LocalPlayer first"
        )

    def test_client_listener_skips_qualified_getitem_calls(self) -> None:
        """Codex finding [P1] (round 5): a LocalScript that only
        references getItem through a namespace (``inventory.getItem(``,
        ``self:getItem(``) doesn't define a bare ``getItem`` symbol.
        The injected listener body calls bare ``getItem(itemName)``,
        which would raise ``attempt to call a nil value`` on the first
        pickup event.

        The fix: ``_GETITEM_SYMBOL_RE`` rejects matches preceded by
        ``.`` or ``:``.
        """
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    'triggerPart.Touched:Connect(function(otherPart)\n'
                    '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                    '    character:SetAttribute("GetItem", itemName)\n'
                    'end)\n'
                ),
                script_type="Script",
            ),
            # Only references inventory.getItem — no bare getItem.
            RbxScript(
                name="InventoryClient",
                source=(
                    'local inventory = require(script.Parent.Inventory)\n'
                    'inventory.getItem("startKey")\n'
                ),
                script_type="LocalScript",
            ),
        ]
        run_packs(scripts)
        client = scripts[1]
        assert "PickupItemEvent" not in client.source, (
            "qualified inventory.getItem should not trigger listener "
            "install — the listener body calls bare getItem which "
            "doesn't exist in this script"
        )

    def test_client_listener_skips_substring_only_match(self) -> None:
        """Codex finding [P1] (round 4): a LocalScript that mentions
        ``getItemModule`` (or any identifier containing the substring
        ``getItem``) but never defines or calls ``getItem`` itself was
        being matched by the previous loose substring check. The
        listener body calls ``getItem(itemName)`` — which doesn't exist
        in such scripts — and crashes on the first pickup event.

        The fix: require ``getItem(`` or ``GetItem(`` as a real symbol
        (word-boundary match).
        """
        scripts = [
            RbxScript(
                name="Pickup",
                source=(
                    'triggerPart.Touched:Connect(function(otherPart)\n'
                    '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                    '    character:SetAttribute("GetItem", itemName)\n'
                    'end)\n'
                ),
                script_type="Script",
            ),
            # HudControl-style LocalScript — defines getItemModule, not getItem.
            RbxScript(
                name="HudControl",
                source=(
                    'local function getItemModule()\n'
                    '    return require(script.Parent.ItemModule)\n'
                    'end\n'
                    'getItemModule()\n'
                ),
                script_type="LocalScript",
            ),
        ]
        run_packs(scripts)
        hud = scripts[1]
        assert "PickupItemEvent" not in hud.source, (
            "loose substring match injected listener that calls "
            "getItem(itemName) — a function this script doesn't define"
        )
        assert ":WaitForChild(\"PickupItemEvent\"" not in hud.source

    def test_client_listener_installs_in_non_fps_player_script(self) -> None:
        """Codex finding [P1] (round 2): the client listener used to
        require BOTH ``getItem`` and ``getRifle``. Non-FPS projects with
        only ``GetItem`` would never get a listener, leaving the broadened
        server pack's FireClient unreachable on the client.

        The listener must install in any script with a ``getItem``/
        ``GetItem`` dispatch, not just FPS Player scripts. End-to-end
        check via ``run_packs`` so we exercise the lazy detector chain
        (server pack writes ``PickupItemEvent``, client pack detects it,
        installs the listener).
        """
        scripts = [
            # Pre-rewrite Pickup with the legacy SetAttribute pattern.
            # ``pickup_remote_event_server`` will rewrite this into
            # ``FireClient(_pl, itemName)`` + ``PickupItemEvent`` lookup,
            # at which point the client pack's detector should fire.
            RbxScript(
                name="Pickup",
                source=(
                    'triggerPart.Touched:Connect(function(otherPart)\n'
                    '    local character = otherPart:FindFirstAncestorOfClass("Model")\n'
                    '    character:SetAttribute("GetItem", itemName)\n'
                    'end)\n'
                ),
                script_type="Script",
            ),
            # RPG-style Player with a GetItem dispatch but no GetRifle.
            RbxScript(
                name="Player",
                source=(
                    'local function GetItem(name)\n'
                    '    print("got " .. tostring(name))\n'
                    'end\n'
                ),
                script_type="LocalScript",
            ),
        ]
        run_packs(scripts)
        player = scripts[1]
        assert "PickupItemEvent" in player.source, (
            "client listener must install in non-FPS Player scripts when "
            "the broadened server pack rewrites the Pickup"
        )
        assert ':WaitForChild("PickupItemEvent"' in player.source

    def test_per_handler_callback_resolution(self) -> None:
        """Codex finding [P2]: a Door with two touch handlers using
        different parameter names (``Touched(other)`` and
        ``TouchEnded(otherPart)``) must rewrite each handler's call site
        with that handler's own parameter name. A single global pick
        breaks the mismatched handler — the GetAttribute check evaluates
        falsy and the close branch never runs.
        """
        s = RbxScript(
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
                '    if getPlayerHasKey() then toggleDoor(true) end\n'
                'end)\n'
                'triggerPart.TouchEnded:Connect(function(otherPart)\n'
                '    if getPlayerHasKey() then toggleDoor(false) end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        # First handler rewrites to (other); second to (otherPart). One
        # site of each must exist; neither may be rewritten with the
        # wrong name.
        assert "getPlayerHasKey(other)" in s.source
        assert "getPlayerHasKey(otherPart)" in s.source
        # Make sure each call site appears in its own handler's body —
        # check the substring slices around each Connect block.
        touched_idx = s.source.index("Touched:Connect")
        touchended_idx = s.source.index("TouchEnded:Connect")
        touched_block = s.source[touched_idx:touchended_idx]
        touchended_block = s.source[touchended_idx:]
        assert "getPlayerHasKey(other)" in touched_block
        assert "getPlayerHasKey(otherPart)" in touchended_block
        assert "getPlayerHasKey(otherPart)" not in touched_block
        assert "getPlayerHasKey(other)" not in touchended_block.replace(
            "getPlayerHasKey(otherPart)", ""
        )

    def test_skips_helper_calls_outside_touch_handlers(self) -> None:
        """Codex finding [P1] (round 3): a Door variant that calls the
        helper during init (e.g. ``print(getPlayerHasKey())``) or from
        a non-Touched callback would have ``otherPart`` injected as an
        argument by the previous resolver — referencing an undefined
        variable. Outside-touch sites must be left at zero args; the
        rewritten helper's nil-arg path returns false cleanly.
        """
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'local function getPlayerHasKey()\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        return _G.Player.hasKey()\n'
                '    end\n'
                '    return false\n'
                'end\n'
                '-- Diagnostic call during init (no touch handler around it)\n'
                'print("init:", getPlayerHasKey())\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if getPlayerHasKey() then toggleDoor(true) end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        # The init-time call must NOT have an undefined variable injected.
        assert "print(\"init:\", getPlayerHasKey())" in s.source
        # The in-touch call site must still get its callback param.
        assert "if getPlayerHasKey(other) then" in s.source

    def test_touch_range_survives_nested_lua_blocks(self) -> None:
        """Codex finding [P1] (round 5): a Touched callback containing
        ordinary ``if``/``for``/``while`` blocks must keep its computed
        range open until the callback's own ``end``. The previous
        parser only counted ``function`` as opens against ``end`` as
        closes, so an inner ``if cond then ... end`` would prematurely
        close the callback's range and leave a later ``_G.Player``
        guard in the same handler treated as outside-scope.
        """
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if ready then\n'
                '        warmup()\n'
                '    end\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        # The hasKey guard inside the same callback (after an inner
        # ``if`` block) must be rewritten — it's still in scope.
        assert 'other and other:FindFirstAncestorOfClass' in s.source
        assert "_G.Player" not in s.source

    def test_touch_range_survives_standalone_do_blocks(self) -> None:
        """Codex finding [P3] (round 6): a Touched callback containing
        a standalone ``do ... end`` block must keep its computed range
        open until the callback's own ``end``. The previous parser
        omitted ``do`` from the open set, so a standalone do-block
        decremented depth on its inner ``end`` and prematurely closed
        the callback.
        """
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    do\n'
                '        local _scope = "guarded"\n'
                '    end\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        # The hasKey guard after the do-block is still in scope.
        assert 'other and other:FindFirstAncestorOfClass' in s.source
        assert "_G.Player" not in s.source

    def test_touch_range_survives_for_and_while_blocks(self) -> None:
        """Same scope rule for ``for`` and ``while`` loops inside the
        callback. Each closes with ``end``; counting only ``function``
        as opens would close the callback range early once the loop
        ends. The fix counts ``function``, ``if``, ``for``, ``while``
        as opens.
        """
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    for _, p in ipairs({1,2,3}) do\n'
                '        log(p)\n'
                '    end\n'
                '    while waiting do\n'
                '        task.wait(0.1)\n'
                '    end\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        assert 'other and other:FindFirstAncestorOfClass' in s.source
        assert "_G.Player" not in s.source

    def test_helper_calls_after_closed_touch_block_not_rewritten(self) -> None:
        """Codex finding [P1] (round 4): a helper call AFTER a touch
        callback's matching ``end`` is no longer in the callback's
        scope. The previous resolver's "closest preceding header" pick
        would borrow the now-out-of-scope ``other``, injecting an
        undefined variable. Scope-aware ranges check enclosure, not
        proximity.
        """
        s = RbxScript(
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
                '    if getPlayerHasKey() then toggleDoor(true) end\n'
                'end)\n'
                '-- This call is AFTER the touched block closed:\n'
                'print("debug:", getPlayerHasKey())\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        # The in-touch site is rewritten with ``other``.
        assert "if getPlayerHasKey(other) then" in s.source
        # The post-block call must NOT have ``other`` injected — that
        # would reference an out-of-scope variable.
        assert 'print("debug:", getPlayerHasKey())' in s.source

    def test_inline_guards_after_closed_touch_block_not_rewritten(self) -> None:
        """Same scope rule for inline guards: a guard after the touch
        callback's matching ``end`` should not borrow the closed
        callback's parameter."""
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
                '-- This guard is AFTER the touched block closed:\n'
                'if _G.Player and _G.Player.hasKey then\n'
                '    error("must not run before touched")\n'
                'end\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        # In-touch guard rewritten correctly.
        touched_idx = s.source.index("Touched:Connect")
        in_touch = s.source[touched_idx:s.source.index("end)")]
        assert 'other and other:FindFirstAncestorOfClass' in in_touch
        # Post-block guard left alone (the body still has _G.Player —
        # it's broken but not introducing an undefined-variable error).
        post_block = s.source[s.source.index("end)") + len("end)"):]
        assert 'other and other:FindFirstAncestorOfClass' not in post_block

    def test_skips_inline_guards_outside_touch_handlers(self) -> None:
        """Same outside-touch rule applies to inline ``_G.Player`` guards
        — a guard outside a touch handler has no ``other``/``otherPart``
        in scope to derive a player from. Leaving it unrewritten is the
        less-bad option (it stays broken, but doesn't introduce a new
        undefined-variable error). In practice, AI transpiles only
        place these guards inside Touched/TouchEnded.
        """
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                '-- Hypothetical init-time guard with no enclosing touch handler\n'
                'if _G.Player and _G.Player.hasKey then\n'
                '    error("must not run before touched")\n'
                'end\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        # Inline guard inside Touched is rewritten correctly.
        touched_idx = s.source.index("Touched:Connect")
        in_touch = s.source[touched_idx:]
        assert 'other and other:FindFirstAncestorOfClass' in in_touch

    def test_per_handler_inline_guard_resolution(self) -> None:
        """Same per-handler callback issue applies to inline guards.
        Each guard's IIFE must reference its own enclosing handler's
        parameter name."""
        s = RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'triggerPart.Touched:Connect(function(other)\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(true)\n'
                '    end\n'
                'end)\n'
                'triggerPart.TouchEnded:Connect(function(otherPart)\n'
                '    if _G.Player and _G.Player.hasKey then\n'
                '        toggleDoor(false)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )
        packs_module._fix_door_global_player_lookup([s])
        touched_idx = s.source.index("Touched:Connect")
        touchended_idx = s.source.index("TouchEnded:Connect")
        touched_block = s.source[touched_idx:touchended_idx]
        touchended_block = s.source[touchended_idx:]
        assert 'other and other:FindFirstAncestorOfClass' in touched_block
        assert 'otherPart and otherPart:FindFirstAncestorOfClass' in touchended_block


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


class TestDoorStripAiRotation:
    """The ``door_strip_ai_rotation`` pack removes the AI-invented
    ``tweenDoor`` / ``doorBaseCF`` rotation idiom from Door.luau.
    Unity Door.cs only flips an Animator parameter; the actual motion
    comes from the Animator clip (translated by the animation phase
    into ``Anim_*_door_open``). The AI's rotation tween on the same
    MeshPart fights the translation tween and the door visibly jitters.
    """

    @staticmethod
    def _door_with_ai_rotation() -> RbxScript:
        return RbxScript(
            name="Door",
            source=(
                'local TweenService = game:GetService("TweenService")\n'
                'local container = script.Parent\n'
                'local function findDoor()\n'
                '    local p = container.Parent\n'
                '    return p and p:FindFirstChild("door")\n'
                'end\n'
                'local doorBaseCF = nil\n'
                'local function captureDoorBase()\n'
                '    local door = findDoor()\n'
                '    if not door then return end\n'
                '    doorBaseCF = door.CFrame\n'
                'end\n'
                'captureDoorBase()\n'
                'local function tweenDoor(open)\n'
                '    local door = findDoor()\n'
                '    if not door or not doorBaseCF then return end\n'
                '    local target = doorBaseCF * CFrame.Angles(0, math.rad(open and 90 or 0), 0)\n'
                '    TweenService:Create(door, TweenInfo.new(0.5), {CFrame = target}):Play()\n'
                'end\n'
                'local function toggleDoor(value)\n'
                '    local door = findDoor()\n'
                '    if door then\n'
                '        door:SetAttribute("open", value)\n'
                '        tweenDoor(value)\n'
                '    end\n'
                'end\n'
            ),
            script_type="Script",
        )

    def test_detector_matches_ai_rotation_signature(self) -> None:
        assert packs_module._detect_door_ai_rotation(
            [self._door_with_ai_rotation()]
        ) is True

    def test_detector_skips_door_without_rotation(self) -> None:
        s = RbxScript(
            name="Door",
            source=(
                'local container = script.Parent\n'
                'local function toggleDoor(value)\n'
                '    container:SetAttribute("open", value)\n'
                'end\n'
            ),
            script_type="Script",
        )
        assert packs_module._detect_door_ai_rotation([s]) is False

    def test_detector_skips_non_door_script(self) -> None:
        s = RbxScript(
            name="Spinner",
            source='local cf = base * CFrame.Angles(0, math.rad(90), 0)\n',
            script_type="Script",
        )
        assert packs_module._detect_door_ai_rotation([s]) is False

    def test_apply_strips_rotation_and_preserves_setattribute(self) -> None:
        s = self._door_with_ai_rotation()
        fixes = packs_module._strip_door_ai_rotation([s])
        assert fixes == 1
        # Rotation idiom is gone
        assert "doorBaseCF" not in s.source
        assert "CFrame.Angles" not in s.source
        assert "tweenDoor" not in s.source
        assert "captureDoorBase" not in s.source
        # The attribute write — which the animation driver listens to — survives
        assert 'door:SetAttribute("open", value)' in s.source
        # toggleDoor itself survives
        assert "local function toggleDoor" in s.source

    def test_apply_is_idempotent(self) -> None:
        s = self._door_with_ai_rotation()
        first = packs_module._strip_door_ai_rotation([s])
        second = packs_module._strip_door_ai_rotation([s])
        assert first == 1
        assert second == 0


class TestDoorTweenOpen:
    """The ``door_tween_open`` pack appends a TweenService listener to
    Door.luau so the sibling ``door`` mesh actually slides on attribute
    change. Pre-pack: Door.cs's AI transpile sets ``open=true`` on the
    sibling but nothing animates it (Mecanim Animator translation is
    not implemented). Post-pack: open/close attribute change triggers a
    1-second Y +14.28 stud tween.
    """

    @staticmethod
    def _door_with_attr_set() -> RbxScript:
        return RbxScript(
            name="Door",
            source=(
                'local Players = game:GetService("Players")\n'
                'local container = script.Parent\n'
                'local function getDoorAnim()\n'
                '    local parent = container.Parent\n'
                '    return parent and parent:FindFirstChild("door")\n'
                'end\n'
                'local function toggleDoor(value)\n'
                '    local doorAnim = getDoorAnim()\n'
                '    if doorAnim then\n'
                '        doorAnim:SetAttribute("open", value)\n'
                '    end\n'
                'end\n'
            ),
            script_type="Script",
        )

    def test_detector_matches_door_with_open_setattribute(self) -> None:
        assert packs_module._detect_door_tween_target(
            [self._door_with_attr_set()]
        ) is True

    def test_detector_skips_already_injected(self) -> None:
        """Idempotency: if the marker is already present, detector
        returns False so re-running the pack is a no-op."""
        s = self._door_with_attr_set()
        s.source += "\n-- _AutoFpsDoorTweenInjected\n"
        assert packs_module._detect_door_tween_target([s]) is False

    def test_detector_skips_unrelated_scripts(self) -> None:
        """A non-Door script that happens to set 'open' attribute must
        not be touched. The pack only matches scripts named ``Door`` and
        also requires the sibling ``door`` lookup pattern."""
        s = RbxScript(
            name="Chest",
            source=(
                'local container = script.Parent\n'
                'container:SetAttribute("open", true)\n'
            ),
            script_type="Script",
        )
        assert packs_module._detect_door_tween_target([s]) is False

    def test_detector_skips_door_without_sibling_lookup(self) -> None:
        """A Door-named script that sets ``open`` but doesn't look up
        a sibling ``door`` mesh isn't the SciFi_Door pattern. Pack
        skips it rather than blindly appending a tween block that has
        nothing to anchor to."""
        s = RbxScript(
            name="Door",
            source=(
                'local container = script.Parent\n'
                'container:SetAttribute("open", true)\n'
            ),
            script_type="Script",
        )
        assert packs_module._detect_door_tween_target([s]) is False

    def test_apply_appends_tween_block(self) -> None:
        s = self._door_with_attr_set()
        fixes = packs_module._inject_door_tween([s])
        assert fixes == 1
        assert "_AutoFpsDoorTweenInjected" in s.source
        assert "TweenService" in s.source
        assert "GetAttributeChangedSignal" in s.source
        # Tween moves Y by 4 * STUDS_PER_METER (~14.28 studs)
        assert "4 * _STUDS_PER_METER" in s.source
        assert "Vector3.new(0, 4 * _STUDS_PER_METER, 0)" in s.source

    def test_apply_idempotent(self) -> None:
        """Re-running the pack on already-injected source is a no-op
        (the detector won't even let it run)."""
        s = self._door_with_attr_set()
        first = packs_module._inject_door_tween([s])
        second = packs_module._inject_door_tween([s])
        assert first == 1
        assert second == 0

    def test_apply_skips_non_door_named_scripts(self) -> None:
        """Pack only operates on scripts named ``Door``. A script with
        the same source under a different name is left alone — keeps
        the side effect scoped to the Door coherence concern.
        """
        s = self._door_with_attr_set()
        s.name = "OtherScript"
        fixes = packs_module._inject_door_tween([s])
        assert fixes == 0
        assert "_AutoFpsDoorTweenInjected" not in s.source

    def test_injected_body_always_wires_tween_with_studs_scaled_offset(
        self,
    ) -> None:
        """The earlier ``_hasAnimDriver`` runtime deferral was a false
        safety: animation_converter's auto-generated ``Anim_*_door_open``
        drivers tweened by an unscaled +4 studs (raw Unity meters,
        missing STUDS_PER_METER), and the companion ``Anim_*_door_close``
        drivers ship a (0,0,0) close offset. Deferring left doors with
        imperceptible motion (or none at all).

        New policy: tween wires unconditionally with a STUDS_PER_METER-
        scaled +4m open offset. The Anim driver's +4 stud overshoot is
        small enough that coexistence is a non-issue in practice.

        Pin: injected body has no deferral helper, has STUDS_PER_METER
        scaling, and wires the TweenService listener directly.
        """
        scripts = [
            self._door_with_attr_set(),
            RbxScript(
                name="Anim_Door_door_open",
                source="-- animation phase driver\n",
                script_type="Script",
            ),
        ]
        # Pack still fires when an anim driver is present (no deferral).
        assert packs_module._detect_door_tween_target(scripts) is True
        packs_module._inject_door_tween(scripts)
        assert "_AutoFpsDoorTweenInjected" in scripts[0].source
        # Regression guard: old deferral helpers must not reappear as
        # active code. The history-explainer comment in the injected
        # body mentions ``_hasAnimDriver`` by name, so check for the
        # function-call form ``_hasAnimDriver(`` rather than the bare
        # identifier.
        assert "_hasAnimDriver(" not in scripts[0].source
        assert "_parent:GetDescendants()" not in scripts[0].source
        # STUDS_PER_METER is applied to the +4m Unity-authored offset.
        assert "_STUDS_PER_METER = 3.571" in scripts[0].source
        assert "4 * _STUDS_PER_METER" in scripts[0].source
        # TweenService listener is wired directly on the door mesh.
        assert "TweenService:Create" in scripts[0].source
        assert 'GetAttributeChangedSignal("open")' in scripts[0].source

    def test_detector_fires_when_no_animation_driver(self) -> None:
        """Sanity check the negative case: when no Anim_*_door_*
        driver is present, the pack still fires for a Door that needs
        the tween.
        """
        scripts = [self._door_with_attr_set()]
        assert packs_module._detect_door_tween_target(scripts) is True

    def test_anim_name_match_tolerates_spaces_and_case(self) -> None:
        """Codex round-10 [P2]: ``animation_converter`` names drivers
        from raw prefab + controller + clip display names. Those can
        carry spaces, dashes, and mixed case. The Python ``_DOOR_
        EXISTING_ANIM_PATTERNS`` are case-insensitive and accept any
        non-newline characters. The injected runtime guard's Lua
        patterns are also case-insensitive (via ``string.lower``).

        Pin: representative variants all match.
        """
        for name in (
            "Anim_SciFi Door_door_Open",
            "Anim_SciFi-Door_door_close",
            "Anim_DOOR_door_OPEN",
            "Anim_door_close",
        ):
            assert any(
                p.match(name) for p in packs_module._DOOR_EXISTING_ANIM_PATTERNS
            ), f"pattern set must match {name!r}"

    def test_pack_still_fires_when_anim_driver_present(self) -> None:
        """Codex round-10 [P2]: ``animation_converter`` emits drivers
        per controller/scope. A project with one converted door
        driver and another Door script whose clips weren't emitted
        must still get the fallback for the uncovered door. Round-9's
        project-wide bail left those uncovered doors stuck closed.

        New policy: pack always fires when a Door needs the marker;
        the INJECTED body's runtime guard handles coexistence with
        any matching ``Anim_*_door_*`` driver at runtime.
        """
        scripts = [
            self._door_with_attr_set(),
            RbxScript(
                name="Anim_DoorPrefab_door_open",
                source="-- driver\n",
                script_type="Script",
            ),
            RbxScript(
                name="Anim_DoorPrefab_door_close",
                source="-- driver\n",
                script_type="Script",
            ),
        ]
        assert packs_module._detect_door_tween_target(scripts) is True
        fixes = packs_module._inject_door_tween(scripts)
        assert fixes == 1

    def test_pack_fires_when_no_driver_present(self) -> None:
        """Reciprocal: pack runs when no driver script is present in
        the project. Pins the negative case of the round-9 policy.
        """
        scripts = [self._door_with_attr_set()]
        assert packs_module._detect_door_tween_target(scripts) is True
        fixes = packs_module._inject_door_tween(scripts)
        assert fixes == 1
        assert "_AutoFpsDoorTweenInjected" in scripts[0].source


class TestBulletPhysicsRaycast:
    """The ``bullet_physics_raycast`` pack replaces AI-transpiled Unity
    bullet bodies (TurretBullet/PlaneBullet) with stud-space velocity
    + anti-gravity + raycast hit detection. Pre-pack: bullets fly too
    slow, nose-dive into terrain, and tunnel past targets at speed.
    """

    @staticmethod
    def _bullet_script(name="TurretBullet"):
        return RbxScript(
            name=name,
            source=(
                'local Debris = game:GetService("Debris")\n'
                'local container = script.Parent\n'
                'local rootPart = container\n'
                'if rootPart then\n'
                '    rootPart.Anchored = false\n'
                '    local impulseDir = container.CFrame.LookVector\n'
                '    rootPart:ApplyImpulse(impulseDir * 60 * rootPart.AssemblyMass)\n'
                'end\n'
                'rootPart.Touched:Connect(function(otherPart)\n'
                '    if otherPart.Parent then\n'
                '        otherPart.Parent:SetAttribute("TakeDamage", 10)\n'
                '    end\n'
                'end)\n'
            ),
            script_type="Script",
        )

    def test_detector_matches_turret_bullet(self) -> None:
        assert packs_module._detect_bullet_unity_transpile(
            [self._bullet_script("TurretBullet")]
        ) is True

    def test_detector_matches_plane_bullet(self) -> None:
        assert packs_module._detect_bullet_unity_transpile(
            [self._bullet_script("PlaneBullet")]
        ) is True

    def test_detector_skips_already_replaced(self) -> None:
        """Idempotency: marker presence short-circuits the detector."""
        s = self._bullet_script("TurretBullet")
        s.source += "\n-- _AutoBulletRaycastInjected\n"
        assert packs_module._detect_bullet_unity_transpile([s]) is False

    def test_detector_skips_unrelated_scripts(self) -> None:
        """Other scripts (e.g. a non-bullet that happens to ApplyImpulse)
        don't trigger — the pack is gated on the canonical bullet names."""
        s = RbxScript(
            name="Cannon",
            source=(
                'rootPart:ApplyImpulse(Vector3.new(0,1,0))\n'
                'rootPart.Touched:Connect(function() end)\n'
            ),
            script_type="Script",
        )
        assert packs_module._detect_bullet_unity_transpile([s]) is False

    def test_apply_replaces_bullet_body(self) -> None:
        s = self._bullet_script("TurretBullet")
        fixes = packs_module._replace_bullet_physics([s])
        assert fixes == 1
        # Marker present so detector skips on re-run
        assert "_AutoBulletRaycastInjected" in s.source
        # Stud-space velocity scaling
        assert "STUDS_PER_METER" in s.source
        assert "AssemblyLinearVelocity" in s.source
        # Anti-gravity VectorForce
        assert "VectorForce" in s.source
        assert "workspace.Gravity" in s.source
        # Raycast-based hit detection (segment from prevPos to curPos)
        assert "Heartbeat" in s.source
        assert "workspace:Raycast" in s.source
        # Visible trail for trajectory readability
        assert "Trail" in s.source

    def test_apply_idempotent(self) -> None:
        s = self._bullet_script("TurretBullet")
        first = packs_module._replace_bullet_physics([s])
        second = packs_module._replace_bullet_physics([s])
        assert first == 1
        assert second == 0

    def test_apply_skips_non_bullet_names(self) -> None:
        """Same source pattern under a different script name (e.g. a
        rocket prefab named ``Missile``) gets left alone — the pack
        scopes to canonical bullet names by design.
        """
        s = self._bullet_script("Missile")
        fixes = packs_module._replace_bullet_physics([s])
        assert fixes == 0
        assert "_AutoBulletRaycastInjected" not in s.source

    def test_replacement_preserves_unity_field_names(self) -> None:
        """``fadeTime``, ``force``, ``damage`` field names match Unity
        TurretBullet.cs / PlaneBullet.cs so prefab attribute overrides
        on the converted output keep working through serialized
        ``_force`` / ``_damage`` attribute reads.
        """
        s = self._bullet_script("TurretBullet")
        packs_module._replace_bullet_physics([s])
        assert "local fadeTime = " in s.source
        assert "local force = " in s.source
        assert "local damage = " in s.source

    def test_plane_bullet_uses_unity_planebullet_defaults(self) -> None:
        """Codex round-1 [P1]: ``PlaneBullet`` must NOT inherit
        ``TurretBullet``'s defaults. Unity ``PlaneBullet.cs`` has
        ``fadeTime=6``, ``force=200``, plus splash damage via
        ``OverlapSphere(2)``. The replacement must reflect that or
        hostile plane shots regress to slow direct-hit-only bullets.
        """
        s = self._bullet_script("PlaneBullet")
        fixes = packs_module._replace_bullet_physics([s])
        assert fixes == 1
        # Defaults are the fallback when the part has no inspector
        # override attribute (codex round-4 [P2] made the replacement
        # ``:GetAttribute`` the value first).
        assert 'GetAttribute("fadeTime") or 6' in s.source
        assert 'GetAttribute("force") or 200' in s.source
        # Splash damage branch present
        assert "applyAreaDamage" in s.source
        # Splash radius matches Unity Physics.OverlapSphere(..., 2)
        assert "2 * STUDS_PER_METER" in s.source

    def test_turret_bullet_keeps_direct_hit_only(self) -> None:
        """``TurretBullet`` (no splash in Unity source) must stay
        direct-hit only. The splash branch ships only for bullets that
        actually had ``Physics.OverlapSphere`` in their Unity source.
        """
        s = self._bullet_script("TurretBullet")
        packs_module._replace_bullet_physics([s])
        assert 'GetAttribute("fadeTime") or 3' in s.source
        assert 'GetAttribute("force") or 60' in s.source
        assert "applyAreaDamage" not in s.source

    def test_replacement_reads_serialized_inspector_overrides(self) -> None:
        """Codex round-4 [P2]: bullets carry inspector overrides
        (``fadeTime``, ``force``, ``damage``) as part attributes via
        ``_extract_monobehaviour_attributes``. The replacement must
        ``:GetAttribute`` those values at runtime, falling back to the
        Unity-canonical defaults only when absent. Without this, a
        prefab that tunes ``damage = 25`` in Unity silently regresses
        to the hardcoded 10.
        """
        s = self._bullet_script("TurretBullet")
        packs_module._replace_bullet_physics([s])
        assert 'GetAttribute("fadeTime") or' in s.source
        assert 'GetAttribute("force") or' in s.source
        assert 'GetAttribute("damage") or' in s.source

    def test_plane_bullet_direct_hit_does_not_double_damage(self) -> None:
        """Codex round-4 [P1]: ``PlaneBullet``'s direct-hit branch must
        NOT call ``SetAttribute("TakeDamage", damage)`` separately
        before ``applyAreaDamage`` — Unity's ``OnCollisionEnter`` runs
        ``OverlapSphere`` once and the directly-hit player is included
        in that sweep, so a separate direct-damage write would apply
        the damage twice (20 instead of 10).
        """
        s = self._bullet_script("PlaneBullet")
        packs_module._replace_bullet_physics([s])
        apply_idx = s.source.index("local function applyHit(model, impactPos)")
        end_idx = s.source.index("end\n", apply_idx) + 4
        apply_body = s.source[apply_idx:end_idx]
        assert "applyAreaDamage" in apply_body
        assert 'model:SetAttribute("TakeDamage", damage)' not in apply_body, (
            "Splash bullets must use applyAreaDamage as the sole "
            "damage source for direct hits."
        )

    def test_detector_matches_helper_local_names(self) -> None:
        """Codex round-2 [P1]: the real ``PlaneBullet.luau`` transpile
        uses ``rb:ApplyImpulse(...)`` and ``part.Touched:Connect(...)``,
        NOT ``rootPart``. Round-1's detector hard-coded the
        ``rootPart`` literal and skipped the pack on the actual output.
        The detector must match any local-variable name so PlaneBullet
        (and any future bullet that names its locals differently) gets
        replaced.
        """
        s = RbxScript(
            name="PlaneBullet",
            source=(
                'local function getPart() return script.Parent end\n'
                'local function getRb() return getPart() end\n'
                'local function start()\n'
                '    local rb = getRb()\n'
                '    if rb and not rb.Anchored then\n'
                '        rb:ApplyImpulse(Vector3.new(0, 0, 1) * 200)\n'
                '    end\n'
                'end\n'
                'local part = getPart()\n'
                'if part then\n'
                '    part.Touched:Connect(function(other) end)\n'
                'end\n'
            ),
            script_type="Script",
        )
        assert packs_module._detect_bullet_unity_transpile([s]) is True
        fixes = packs_module._replace_bullet_physics([s])
        assert fixes == 1, "real PlaneBullet transpile must be replaced"
        assert "_AutoBulletRaycastInjected" in s.source


class TestPlayerDamageRemoteEvent:
    """The ``player_damage_remote_event`` pack solves the
    LocalScript→server attribute-write replication gap. Player.luau's
    ``shoot()`` raycasts client-side and ``:SetAttribute("TakeDamage",
    true)`` on the hit instance, but that attribute write never
    reaches the server. The pack wires a ``DamageEvent`` RemoteEvent +
    server router so server-side ``GetAttributeChangedSignal`` listeners
    (Turret.luau, HostilePlane.luau) actually fire.
    """

    @staticmethod
    def _player_script_with_hit():
        return RbxScript(
            name="Player",
            source=(
                'local function shoot()\n'
                '    local result = workspace:Raycast(origin, dir, rp)\n'
                '    if result then\n'
                '        local hitInst = result.Instance\n'
                '        hitInst:SetAttribute("TakeDamage", true)\n'
                '        local model = hitInst:FindFirstAncestorOfClass("Model")\n'
                '        if model then model:SetAttribute("TakeDamage", true) end\n'
                '    end\n'
                'end\n'
            ),
            script_type="LocalScript",
        )

    def test_detector_matches_player_with_raycast_setattr(self) -> None:
        assert packs_module._detect_player_damage_attr_set(
            [self._player_script_with_hit()]
        ) is True

    def test_detector_skips_already_patched(self) -> None:
        s = self._player_script_with_hit()
        s.source += "\n-- _AutoDamageRemoteEventInjected\n"
        assert packs_module._detect_player_damage_attr_set([s]) is False

    def test_detector_skips_non_player_scripts(self) -> None:
        s = RbxScript(
            name="OtherScript",
            source='hitInst:SetAttribute("TakeDamage", true)\n',
            script_type="LocalScript",
        )
        assert packs_module._detect_player_damage_attr_set([s]) is False

    def test_apply_inserts_fireserver_after_setattribute(self) -> None:
        scripts = [self._player_script_with_hit()]
        fixes = packs_module._inject_player_damage_remote_event(scripts)
        assert fixes >= 1
        patched = scripts[0]
        # Marker comment present (idempotency anchor)
        assert "_AutoDamageRemoteEventInjected" in patched.source
        # FireServer call present with camera-origin payload
        assert "_de:FireServer(hitInst" in patched.source
        assert "_cam.CFrame.Position" in patched.source
        assert "_cam.CFrame.LookVector" in patched.source
        # Insertion lands AFTER both SetAttribute lines
        hit_idx = patched.source.index(
            'hitInst:SetAttribute("TakeDamage", true)'
        )
        model_idx = patched.source.index(
            'model:SetAttribute("TakeDamage", true)'
        )
        fire_idx = patched.source.index("_de:FireServer(hitInst")
        assert hit_idx < fire_idx, "FireServer must come after hitInst SetAttribute"
        assert model_idx < fire_idx, "FireServer must come after model SetAttribute"

    def test_apply_emits_damage_router_script(self) -> None:
        scripts = [self._player_script_with_hit()]
        packs_module._inject_player_damage_remote_event(scripts)
        router = next(
            (s for s in scripts if s.name == "_AutoDamageEventRouter"),
            None,
        )
        assert router is not None, "server router script must be appended"
        assert router.script_type == "Script"
        assert router.parent_path == "ServerScriptService"
        # Router creates DamageEvent if missing, listens for FireServer,
        # and propagates the attribute write to both the hit instance
        # and its enclosing Model.
        assert 'Name = "DamageEvent"' in router.source
        assert "OnServerEvent" in router.source
        assert ':SetAttribute("TakeDamage"' in router.source

    def test_apply_idempotent(self) -> None:
        """Re-running the pack must not double-insert the FireServer
        block or duplicate the router script.
        """
        scripts = [self._player_script_with_hit()]
        first = packs_module._inject_player_damage_remote_event(scripts)
        second = packs_module._inject_player_damage_remote_event(scripts)
        assert first >= 1
        assert second == 0, "second run must be a no-op"
        # Only one router script in the list
        routers = [s for s in scripts if s.name == "_AutoDamageEventRouter"]
        assert len(routers) == 1
        # Only one FireServer call in the patched player source
        patched_player = scripts[0].source
        assert patched_player.count("_de:FireServer(hitInst") == 1

    def test_router_validates_distance_and_line_of_sight(self) -> None:
        """Codex round-1 [P1]: the auto-generated server router must NOT
        trust a client-supplied ``hitInstance`` verbatim. A malicious
        ``DamageEvent:FireServer(<any enemy>)`` from a hacked client
        would otherwise apply damage to arbitrary world parts. Validate
        on the server by re-raycasting from the player's character and
        confirming the result matches the intended instance, plus a
        distance gate matching Unity Player.cs's effective range.
        """
        scripts = [self._player_script_with_hit()]
        packs_module._inject_player_damage_remote_event(scripts)
        router = next(s for s in scripts if s.name == "_AutoDamageEventRouter")
        src = router.source
        # Server re-raycasts from the player's character.
        assert "player.Character" in src
        assert "HumanoidRootPart" in src
        assert "workspace:Raycast" in src
        # Distance gate uses STUDS_PER_METER (matches Unity meters).
        assert "STUDS_PER_METER" in src
        assert "MAX_SHOOT_RANGE_STUDS" in src
        # Identity match — accept hit when raycast result matches the
        # intended instance OR a sibling part of the same Model (handles
        # slight client/server timing drift on moving targets).
        assert "_matchesIntendedHit" in src
        # Hostile-input guard: the FireServer payload must be a BasePart.
        assert ':IsA("BasePart")' in src

    def test_detector_skips_server_script_player(self) -> None:
        """Codex round-2 [P2]: storage-classifier sometimes leaves
        ``Player`` as a server ``Script`` instead of ``LocalScript``.
        The pack must NOT inject ``FireServer`` into a server script
        (would error at runtime). Detector skips Player when
        script_type != "LocalScript".
        """
        s = RbxScript(
            name="Player",
            source=(
                'local function shoot()\n'
                '    local result = workspace:Raycast(origin, dir, rp)\n'
                '    if result then\n'
                '        local hitInst = result.Instance\n'
                '        hitInst:SetAttribute("TakeDamage", true)\n'
                '    end\n'
                'end\n'
            ),
            script_type="Script",  # server, NOT LocalScript
        )
        assert packs_module._detect_player_damage_attr_set([s]) is False

    def test_apply_skips_server_script_player(self) -> None:
        """Defense in depth: even if the detector mis-classifies, the
        apply path must also gate on ``script_type == LocalScript`` so
        a server Player script never gets ``FireServer`` injected.
        """
        s = RbxScript(
            name="Player",
            source=(
                'local result = workspace:Raycast(origin, dir, rp)\n'
                'if result then\n'
                '    local hitInst = result.Instance\n'
                '    hitInst:SetAttribute("TakeDamage", true)\n'
                'end\n'
            ),
            script_type="Script",  # server
        )
        scripts = [s]
        packs_module._inject_player_damage_remote_event(scripts)
        # The Player source stays untouched
        assert "_AutoDamageRemoteEventInjected" not in s.source
        assert "_de:FireServer" not in s.source

    def test_router_type_guards_payload_before_instance_methods(self) -> None:
        """Codex round-2 [P2]: the server router must reject non-Instance
        payloads (``true``, ``{}``, strings) BEFORE calling
        ``IsDescendantOf`` / ``IsA`` on them. Otherwise a malicious or
        malformed ``FireServer`` throws at runtime instead of being
        cleanly dropped.

        Pin the type guard ordering: ``typeof(hitInstance) == "Instance"``
        check appears in source BEFORE the first Instance-method call.
        """
        scripts = [self._player_script_with_hit()]
        packs_module._inject_player_damage_remote_event(scripts)
        router = next(s for s in scripts if s.name == "_AutoDamageEventRouter")
        src = router.source

        # The typeof check exists
        type_idx = src.find('typeof(hitInstance) ~= "Instance"')
        assert type_idx >= 0, "typeof guard must be present"

        # The first Instance method call (IsDescendantOf / IsA) lives AFTER the typeof guard
        desc_idx = src.find(':IsDescendantOf(workspace)')
        isa_idx = src.find(':IsA("BasePart")')
        first_inst_method = min(i for i in (desc_idx, isa_idx) if i >= 0)
        assert type_idx < first_inst_method, (
            "typeof guard must precede any Instance method call so a "
            "non-Instance payload is rejected cleanly."
        )

    def test_detector_matches_arbitrary_hit_var_name(self) -> None:
        """Codex round-3 [P1]: AI transpile may name the raycast-result
        local ``hitPart``, ``hit``, ``instance``, etc. The detector
        must match any identifier, not just ``hitInst``. Without this,
        the pack silently skips real Player.luau outputs and the
        DamageEvent path stays unwired.
        """
        s = RbxScript(
            name="Player",
            source=(
                'local result = workspace:Raycast(origin, dir, rp)\n'
                'if result then\n'
                '    local hitPart = result.Instance\n'
                '    hitPart:SetAttribute("TakeDamage", true)\n'
                'end\n'
            ),
            script_type="LocalScript",
        )
        assert packs_module._detect_player_damage_attr_set([s]) is True

    def test_apply_uses_captured_hit_var_name(self) -> None:
        """Codex round-3 [P1]: when the AI transpile uses ``hitPart``,
        the inserted ``FireServer`` call must reference ``hitPart``, not
        a hard-coded ``hitInst``. Otherwise the patched source refers
        to an undefined variable and crashes at runtime.
        """
        s = RbxScript(
            name="Player",
            source=(
                'local result = workspace:Raycast(origin, dir, rp)\n'
                'if result then\n'
                '    local hitPart = result.Instance\n'
                '    hitPart:SetAttribute("TakeDamage", true)\n'
                'end\n'
            ),
            script_type="LocalScript",
        )
        packs_module._inject_player_damage_remote_event([s])
        assert "_de:FireServer(hitPart" in s.source, (
            "FireServer must reference the AI-captured hit variable "
            "name (here ``hitPart``), not a hard-coded ``hitInst``."
        )
        assert "FireServer(hitInst" not in s.source, (
            "Stale hard-coded hitInst would crash at runtime when the "
            "AI named the local ``hitPart``."
        )

    def test_router_replays_from_client_camera_origin(self) -> None:
        """Codex round-3 [P2]: server raycast must replay from the
        client-supplied camera origin/direction (not from
        HumanoidRootPart). Otherwise legitimate over-cover shots get
        rejected because the HRP→hitInstance line is occluded even
        though the camera could see the target.

        The router takes ``originPos`` and ``lookDir`` as RemoteEvent
        payload args and uses those in its ``workspace:Raycast`` call.
        """
        scripts = [self._player_script_with_hit()]
        packs_module._inject_player_damage_remote_event(scripts)
        router = next(s for s in scripts if s.name == "_AutoDamageEventRouter")
        src = router.source
        # Router signature accepts takeDamageValue + originPos + lookDir
        # (round-10 [P1]: server preserves the client's payload value).
        assert (
            "OnServerEvent:Connect(function(player, hitInstance, "
            "takeDamageValue, originPos, lookDir)"
        ) in src
        # Origin sanity bound vs the player's HRP (anti-teleport)
        assert "MAX_ORIGIN_DRIFT_STUDS" in src
        # Server replay uses originPos / lookDir, NOT hrp.Position
        assert "workspace:Raycast(\n        originPos," in src
        assert "lookDir.Unit" in src
        # Client-side patch injects camera origin/direction
        patched_player = scripts[0].source
        assert "workspace.CurrentCamera" in patched_player
        assert "_cam.CFrame.Position" in patched_player
        assert "_cam.CFrame.LookVector" in patched_player

    def test_plane_bullet_splash_fires_on_wall_impact(self) -> None:
        """PlaneBullet's splash damage must fire when the bullet hits
        ANY surface (terrain, wall, prop), not just a Humanoid model.

        Codex round-5 [P1]: splash centers on ``result.Position``
        (the actual raycast collision point), NOT
        ``rootPart.Position``. Tunneling at high force values can put
        rootPart 10+ studs past the impact — larger than the splash
        radius, missing the near-miss player entirely.
        """
        s = RbxScript(
            name="PlaneBullet",
            source=(
                'local rb = script.Parent\n'
                'rb:ApplyImpulse(Vector3.new(0, 0, 1) * 200)\n'
                'rb.Touched:Connect(function() end)\n'
            ),
            script_type="Script",
        )
        packs_module._replace_bullet_physics([s])
        # Non-character impact branch applies splash at the raycast
        # ``result.Position`` so wall/ground impacts beside a player
        # land inside the splash radius.
        non_char_branch = s.source[s.source.index("else"):]
        assert "applyAreaDamage(result.Position)" in non_char_branch, (
            "PlaneBullet's non-character impact branch must apply "
            "splash damage at the raycast result's position so "
            "near-miss shots damage nearby players."
        )

    def test_turret_bullet_non_char_branch_just_destroys(self) -> None:
        """Companion to the PlaneBullet splash test: TurretBullet has
        no splash in Unity, so its non-character impact branch should
        just destroy the bullet without trying to apply area damage.
        """
        s = RbxScript(
            name="TurretBullet",
            source=(
                'local rootPart = script.Parent\n'
                'rootPart:ApplyImpulse(Vector3.new(0, 0, 1) * 60)\n'
                'rootPart.Touched:Connect(function() end)\n'
            ),
            script_type="Script",
        )
        packs_module._replace_bullet_physics([s])
        # TurretBullet's non-character impact branch must NOT splash.
        # Slice from the ``else`` of the Humanoid-check (the non-char
        # branch) up to the next ``end``, and confirm splash is absent.
        non_char_branch = s.source[s.source.index("else"):]
        # Bound the slice to just the non-character ``else`` block so a
        # later ``applyAreaDamage`` defined elsewhere can't fool the test.
        end_idx = non_char_branch.find("end\n")
        non_char_branch = non_char_branch[:end_idx] if end_idx > 0 else non_char_branch
        assert "applyAreaDamage" not in non_char_branch, (
            "TurretBullet has no Unity splash damage — non-character "
            "impact must just destroy the bullet."
        )

    def test_detector_matches_incrementing_takedamage_form(self) -> None:
        """Codex round-4 [P2]: the AI transpile sometimes emits
        ``hitInst:SetAttribute("TakeDamage", (hitInst:GetAttribute
        ("TakeDamage") or 0) + 1)`` to force the change signal (a
        plain ``true`` write a second time doesn't fire
        GetAttributeChangedSignal). The detector must match this
        non-boolean shape too, otherwise affected projects silently
        skip the FireServer injection.
        """
        s = RbxScript(
            name="Player",
            source=(
                'local result = workspace:Raycast(origin, dir, rp)\n'
                'if result then\n'
                '    local hitInst = result.Instance\n'
                '    hitInst:SetAttribute("TakeDamage", '
                '(hitInst:GetAttribute("TakeDamage") or 0) + 1)\n'
                'end\n'
            ),
            script_type="LocalScript",
        )
        assert packs_module._detect_player_damage_attr_set([s]) is True
        scripts = [s]
        fixes = packs_module._inject_player_damage_remote_event(scripts)
        assert fixes >= 1
        assert "_de:FireServer(hitInst" in scripts[0].source

    def test_plane_bullet_splash_centers_on_raycast_impact(self) -> None:
        """Codex round-5 [P1]: at high force (PlaneBullet's
        ``force=200``), the bullet's ``rootPart.Position`` can be 10+
        studs past the raycast collision point on a tunneling frame
        — larger than the 7-stud splash radius. Splash must center on
        ``result.Position`` (the actual collision point) so wall/
        ground hits beside the player still land inside the splash.

        Pin: both apply_hit_body and non_char_impact_body use
        ``impactPos`` / ``result.Position``, never ``rootPart.Position``.
        """
        s = TestBulletPhysicsRaycast._bullet_script("PlaneBullet")
        packs_module._replace_bullet_physics([s])
        # ``applyHit`` signature accepts the impact position.
        assert "local function applyHit(model, impactPos)" in s.source
        # ``applyHit`` call site passes ``result.Position``.
        assert "applyHit(model, result.Position)" in s.source
        # Direct-hit splash centers on impactPos.
        apply_idx = s.source.index("local function applyHit(model, impactPos)")
        end_idx = s.source.index("end\n", apply_idx) + 4
        apply_body = s.source[apply_idx:end_idx]
        assert "applyAreaDamage(impactPos)" in apply_body
        # Non-character impact branch centers on result.Position.
        non_char_branch = s.source[s.source.index("else"):]
        assert "applyAreaDamage(result.Position)" in non_char_branch
        # And rootPart.Position is NOT used as the splash origin in
        # either branch (it's still fine for VectorForce/velocity).
        assert "applyAreaDamage(rootPart.Position)" not in s.source

    def test_plane_bullet_spawns_explosion_template(self) -> None:
        """Codex round-5 [P3]: Unity ``PlaneBullet.cs`` instantiates
        an ``explosion`` GameObject on every collision. The
        replacement must clone the ``ReplicatedStorage.Templates.
        Explosion`` template at the impact point so VFX/audio
        feedback survives the rewrite.
        """
        s = TestBulletPhysicsRaycast._bullet_script("PlaneBullet")
        packs_module._replace_bullet_physics([s])
        # Helper present
        assert "_spawnExplosionAt(originPos)" in s.source
        assert '_explosionTemplate' in s.source
        # Looked up from ReplicatedStorage.Templates.Explosion
        assert 'FindFirstChild("Templates")' in s.source
        assert 'FindFirstChild("Explosion")' in s.source
        # Spawn called from both hit branches at the impact point.
        apply_idx = s.source.index("local function applyHit(model, impactPos)")
        end_idx = s.source.index("end\n", apply_idx) + 4
        apply_body = s.source[apply_idx:end_idx]
        assert "_spawnExplosionAt(impactPos)" in apply_body
        non_char_branch = s.source[s.source.index("else"):]
        assert "_spawnExplosionAt(result.Position)" in non_char_branch

    def test_turret_bullet_does_not_spawn_explosion(self) -> None:
        """``TurretBullet`` has no explosion in Unity source — its
        replacement should NOT carry the spawnExplosion helper.
        """
        s = TestBulletPhysicsRaycast._bullet_script("TurretBullet")
        packs_module._replace_bullet_physics([s])
        assert "_spawnExplosionAt" not in s.source
        assert "_explosionTemplate" not in s.source

    def test_pack_re_emits_router_when_missing_from_patched_source(self) -> None:
        """Codex round-5 [P2]: on re-conversion from a rehydrated
        output, the Player script already carries the
        ``_AutoDamageRemoteEventInjected`` marker but the router
        script can be absent (pruned by intervening passes). The
        pack must detect that state and re-emit the router so
        client shots keep damaging server-side enemies.
        """
        # Player script is already patched (carries the marker) so
        # the per-Player FireServer-inject branch is a no-op.
        patched_player = RbxScript(
            name="Player",
            source=(
                'local result = workspace:Raycast(origin, dir, rp)\n'
                'if result then\n'
                '    local hitInst = result.Instance\n'
                '    hitInst:SetAttribute("TakeDamage", true)\n'
                '    -- _AutoDamageRemoteEventInjected: mirror client damage to server\n'
                '    local _de = game:GetService("ReplicatedStorage"):FindFirstChild("DamageEvent")\n'
                '    if _de then _de:FireServer(hitInst, Vector3.new(), Vector3.new()) end\n'
                'end\n'
            ),
            script_type="LocalScript",
        )
        scripts = [patched_player]
        # Detector must fire because the router is missing.
        assert packs_module._detect_player_or_router_present(scripts) is True
        fixes = packs_module._inject_player_damage_remote_event(scripts)
        assert fixes >= 1, "pack must run when router is missing"
        # Router is now present.
        router = next(
            (s for s in scripts if s.name == "_AutoDamageEventRouter"),
            None,
        )
        assert router is not None
        # Player source unchanged (already patched).
        assert patched_player.source.count("_de:FireServer") == 1

    def test_router_preserves_client_takedamage_value(self) -> None:
        """Codex round-10 [P1]: the server router must mirror the
        client's ``TakeDamage`` payload VERBATIM rather than
        synthesizing a counter. Listeners that read the attribute as
        the damage amount (e.g. unity-3d-simplefps) need the original
        value; a server-side counter discards that data.

        Pin: router writes ``takeDamageValue`` directly, with a
        nil/false coerce-to-``true`` for malformed client payloads.
        """
        scripts = [self._player_script_with_hit()]
        packs_module._inject_player_damage_remote_event(scripts)
        router = next(s for s in scripts if s.name == "_AutoDamageEventRouter")
        src = router.source
        # Server writes the client-supplied value, not a synthetic counter.
        assert 'SetAttribute("TakeDamage", takeDamageValue)' in src
        # Type guard before SetAttribute: malformed payloads (table,
        # Vector3, function) are coerced to ``true`` so SetAttribute
        # never receives an invalid value (codex round-11 [P2]).
        assert "typeof(takeDamageValue)" in src
        assert '"boolean"' in src and '"number"' in src and '"string"' in src
        # No counter helper or synthetic +1 logic.
        assert "_bumpTakeDamage" not in src
        assert "cur + 1" not in src
        # Client patch reads the value back via GetAttribute and sends it.
        patched_player = scripts[0].source
        assert 'GetAttribute("TakeDamage")' in patched_player
        assert "_de:FireServer(hitInst, _td" in patched_player

    def test_apply_handles_multiline_if_model_block(self) -> None:
        """Codex round-8 [P1]: the AI transpile sometimes formats the
        ``if model then`` model-SetAttribute as a multi-line block:

            if model then
                model:SetAttribute("TakeDamage", true)
            end

        The round-7 anchor only matched the single-line form, so
        scripts with the multi-line shape silently skipped the
        FireServer injection. Detector still fires, so the router
        gets emitted but the client side stays unpatched — hits stay
        client-only.

        Pin: the anchor handles both shapes.
        """
        s = RbxScript(
            name="Player",
            source=(
                'local result = workspace:Raycast(origin, dir, rp)\n'
                'if result then\n'
                '    local hitInst = result.Instance\n'
                '    hitInst:SetAttribute("TakeDamage", true)\n'
                '    local model = hitInst:FindFirstAncestorOfClass("Model")\n'
                '    if model then\n'
                '        model:SetAttribute("TakeDamage", true)\n'
                '    end\n'
                'end\n'
            ),
            script_type="LocalScript",
        )
        scripts = [s]
        fixes = packs_module._inject_player_damage_remote_event(scripts)
        assert fixes >= 1
        # FireServer present in the patched body
        assert "_de:FireServer(hitInst" in s.source
        # Inserted AFTER the multi-line ``if model then ... end`` block
        end_idx = s.source.index("        model:SetAttribute(\"TakeDamage\", true)")
        end_block = s.source.index("    end\n", end_idx) + len("    end\n")
        fire_idx = s.source.index("_de:FireServer(hitInst")
        assert fire_idx >= end_block, (
            "FireServer block must follow the multi-line ``if model then`` "
            "section, not be inserted inside or before it."
        )

    def test_pack_refreshes_stale_router_on_reconversion(self) -> None:
        """Codex round-11 [P2]: the detector returned False when a
        Player was already patched AND a router script existed,
        regardless of the router's source. Re-conversion of an
        already-patched output thus could keep a stale older-version
        router that lacked the round-to-round improvements
        (camera-origin replay, value preservation, type guard).

        Pin: when the router source diverges from the canonical pack
        version, the detector fires and the apply pass refreshes.
        """
        # Already-patched Player + a STALE router with the old body.
        patched_player = RbxScript(
            name="Player",
            source=(
                'local result = workspace:Raycast(origin, dir, rp)\n'
                'if result then\n'
                '    local hitInst = result.Instance\n'
                '    hitInst:SetAttribute("TakeDamage", true)\n'
                '    -- _AutoDamageRemoteEventInjected: mirror client damage to server\n'
                '    local _de = game:GetService("ReplicatedStorage"):FindFirstChild("DamageEvent")\n'
                '    if _de then _de:FireServer(hitInst) end\n'
                'end\n'
            ),
            script_type="LocalScript",
        )
        stale_router = RbxScript(
            name="_AutoDamageEventRouter",
            source="-- ancient router shape, missing new validation\n",
            script_type="Script",
        )
        scripts = [patched_player, stale_router]
        # Detector must fire so the router gets refreshed.
        assert packs_module._detect_player_or_router_present(scripts) is True
        fixes = packs_module._inject_player_damage_remote_event(scripts)
        assert fixes >= 1
        # Router source now matches the canonical pack version.
        refreshed = next(s for s in scripts if s.name == "_AutoDamageEventRouter")
        assert refreshed.source == packs_module._DAMAGE_ROUTER_SOURCE
        # Idempotent: re-run with fresh router → no further changes.
        fixes2 = packs_module._inject_player_damage_remote_event(scripts)
        assert fixes2 == 0

    def test_router_coerces_non_scalar_payloads(self) -> None:
        """Codex round-11 [P2]: a malicious client can fire the
        DamageEvent with any payload type. Roblox attributes only
        accept primitive scalars (bool/number/string + a few
        Vector/Color types); passing a table or function throws on
        ``SetAttribute``. The router must validate the payload type
        BEFORE calling SetAttribute, coercing non-canonical shapes to
        ``true`` (still useful for boolean listeners) so no
        SetAttribute call ever gets handed an invalid value.

        Pin: the typeof guard appears in source, scoped to the
        canonical shapes (boolean/number/string).
        """
        scripts = [self._player_script_with_hit()]
        packs_module._inject_player_damage_remote_event(scripts)
        router = next(s for s in scripts if s.name == "_AutoDamageEventRouter")
        src = router.source
        # Type guard before SetAttribute on takeDamageValue.
        assert "typeof(takeDamageValue)" in src
        # Canonical shapes accepted; anything else coerced.
        assert '"boolean"' in src
        assert '"number"' in src
        assert '"string"' in src
        # The guard appears BEFORE any SetAttribute call on hitInstance.
        guard_idx = src.index("typeof(takeDamageValue)")
        set_idx = src.index('hitInstance:SetAttribute("TakeDamage", takeDamageValue)')
        assert guard_idx < set_idx, (
            "Type guard must run before SetAttribute receives the payload."
        )


class TestProducerConsumerBindableEventGuard:
    """``producer_consumer_bindable_events`` publishes a producer's
    anonymous BindableEvent into ReplicatedStorage under the consumer's
    expected name. But if the consumer uses ``OnClientEvent`` /
    ``OnServerEvent`` (RemoteEvent-only API), the publish would wire a
    BindableEvent into a RemoteEvent-shaped consumer — silently breaking
    the bridge. The pack must skip in that case.
    """

    def test_skips_publish_when_consumer_uses_onclientevent(self) -> None:
        from converter.script_coherence_packs import run_packs

        producer = RbxScript(
            name="Producer",
            source=(
                'local healthUpdateEvent = Instance.new("BindableEvent")\n'
                'healthUpdateEvent:Fire(100)\n'
            ),
            script_type="Script",
        )
        consumer = RbxScript(
            name="Consumer",
            source=(
                'local ReplicatedStorage = game:GetService("ReplicatedStorage")\n'
                'local evt = ReplicatedStorage:WaitForChild("HealthUpdate")\n'
                'evt.OnClientEvent:Connect(function(value) print(value) end)\n'
            ),
            script_type="LocalScript",
        )
        run_packs(
            [producer, consumer],
            enabled={"producer_consumer_bindable_events"},
        )
        # Producer source must NOT have been rewritten to publish into
        # ReplicatedStorage — that would wire a BindableEvent into a
        # consumer that calls OnClientEvent (which BindableEvent lacks).
        assert "ReplicatedStorage" not in producer.source
        assert ".Name =" not in producer.source

    def test_publishes_when_consumer_uses_event_connect(self) -> None:
        """BindableEvent's ``.Event:Connect`` is the same-process API,
        so a producer/consumer pair using BindableEvent semantics SHOULD
        still be bridged — only the OnClientEvent/OnServerEvent shape
        is incompatible.
        """
        from converter.script_coherence_packs import run_packs

        producer = RbxScript(
            name="Producer",
            source=(
                'local pauseEvent = Instance.new("BindableEvent")\n'
                'pauseEvent:Fire()\n'
            ),
            script_type="Script",
        )
        consumer = RbxScript(
            name="Consumer",
            source=(
                'local ReplicatedStorage = game:GetService("ReplicatedStorage")\n'
                'local evt = ReplicatedStorage:WaitForChild("Pause")\n'
                'evt.Event:Connect(function() print("paused") end)\n'
            ),
            script_type="Script",
        )
        run_packs(
            [producer, consumer],
            enabled={"producer_consumer_bindable_events"},
        )
        # Producer must have been rewritten to publish under "Pause".
        assert 'pauseEvent.Name = "Pause"' in producer.source
        assert "ReplicatedStorage" in producer.source


class TestTouchCallbackRangeStringBlanking:
    """``_touch_callback_ranges`` walks block-open/end keyword tokens with
    a depth counter. Without blanking string literals, a Luau keyword
    appearing inside a string — ``error("function call failed")``,
    ``warn("expected end of input")`` — corrupts the depth count and the
    computed body end goes wrong, which can leave callers borrowing the
    callback parameter at out-of-scope sites.
    """

    def test_string_literal_with_function_keyword_does_not_open_block(self) -> None:
        # Single Touched handler; body contains a string literal carrying
        # the word ``function``. The handler closes at the literal ``end``
        # right before the trailing newline. If the string content were
        # treated as code, depth would be 2 when we hit ``end``, causing
        # the body_end to land far too late.
        from converter.script_coherence_packs import _touch_callback_ranges

        src = (
            'part.Touched:Connect(function(other)\n'
            '    error("function call failed")\n'
            'end)\n'
            'local after = "not in scope"\n'
        )
        ranges = _touch_callback_ranges(src)
        assert len(ranges) == 1
        body_start, body_end, var = ranges[0]
        assert var == "other"
        # ``body_end`` must be the position of the closing ``end`` for
        # this callback — it should appear BEFORE the trailing line.
        assert body_end < src.index('"not in scope"')

    def test_string_literal_with_end_keyword_does_not_close_block(self) -> None:
        from converter.script_coherence_packs import _touch_callback_ranges

        src = (
            'part.Touched:Connect(function(other)\n'
            '    if other then\n'
            '        warn("reached end of input")\n'
            '    end\n'
            'end)\n'
            'local tail = 1\n'
        )
        ranges = _touch_callback_ranges(src)
        assert len(ranges) == 1
        body_start, body_end, var = ranges[0]
        # The matching ``end`` is the LAST ``end`` of the snippet, not the
        # one inside the inner if. Without string blanking, the inner
        # ``end`` of input would balance the function depth too early.
        last_end = src.rfind("end")
        assert body_end == last_end

    def test_long_bracket_string_with_block_keywords_is_blanked(self) -> None:
        from converter.script_coherence_packs import _touch_callback_ranges

        src = (
            'part.Touched:Connect(function(other)\n'
            '    local snippet = [[\n'
            '        function fake() return end\n'
            '        if true then end\n'
            '    ]]\n'
            '    print(snippet)\n'
            'end)\n'
        )
        ranges = _touch_callback_ranges(src)
        assert len(ranges) == 1
        _, body_end, _ = ranges[0]
        # The ``end`` keywords inside ``[[ ... ]]`` must NOT be counted.
        # The real matching ``end`` is the last one in the snippet.
        last_end = src.rfind("end")
        assert body_end == last_end

    def test_comment_with_keywords_does_not_break_scan(self) -> None:
        from converter.script_coherence_packs import _touch_callback_ranges

        src = (
            'part.Touched:Connect(function(other)\n'
            '    -- function foo() end if then\n'
            '    print("hi")\n'
            'end)\n'
        )
        ranges = _touch_callback_ranges(src)
        assert len(ranges) == 1
        _, body_end, _ = ranges[0]
        last_end = src.rfind("end")
        assert body_end == last_end
