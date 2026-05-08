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
