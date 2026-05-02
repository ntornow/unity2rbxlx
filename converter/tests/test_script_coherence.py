"""
test_script_coherence.py -- Tests for cross-script consistency fixes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript
from converter.script_coherence import (
    fix_require_classifications,
    inject_require_calls,
    _break_circular_requires,
    _fix_clone_visibility,
    _fix_prefab_lookups,
    _remove_stale_player_requires,
    _disable_default_controls_in_fps_scripts,
)


class TestRequireReclassification:
    def test_required_script_becomes_module(self):
        scripts = [
            RbxScript(name="Main", source='local m = require(ReplicatedStorage:FindFirstChild("Utils"))', script_type="Script"),
            RbxScript(name="Utils", source="local M = {} function M.foo() end return M", script_type="Script"),
        ]
        fixes = fix_require_classifications(scripts)
        assert scripts[1].script_type == "ModuleScript"
        assert fixes >= 1

    def test_return_statement_becomes_module(self):
        scripts = [
            RbxScript(name="Config", source="local Config = {speed = 10}\nreturn Config", script_type="Script"),
        ]
        fixes = fix_require_classifications(scripts)
        assert scripts[0].script_type == "ModuleScript"

    def test_return_statement_does_not_demote_fps_controller(self):
        """An FPS controller that exposes a Player table at the top and
        ends with ``return Player`` would otherwise be demoted to a
        ModuleScript by the trailing-return rule. ModuleScripts only
        execute when something requires them — but the controller's
        RenderStepped/InputBegan listeners need to run on character
        spawn. Force LocalScript when client-only APIs are present.
        """
        src = (
            'local Players = game:GetService("Players")\n'
            'local UIS = game:GetService("UserInputService")\n'
            'local Player = {}\n'
            'function Player.move() UIS.MouseBehavior = Enum.MouseBehavior.LockCenter end\n'
            'Players.LocalPlayer.CharacterAdded:Connect(Player.move)\n'
            'return Player\n'
        )
        scripts = [RbxScript(name="Player", source=src, script_type="Script")]
        fix_require_classifications(scripts)
        # Pass 3 (_fix_client_server_classification) promotes the script
        # back to LocalScript on its client-API signature; it must NOT
        # have been frozen as ModuleScript by Pass 2.
        assert scripts[0].script_type == "LocalScript", (
            f"client-API FPS controller with `return Player` ended as "
            f"{scripts[0].script_type!r}; downstream FPS-controls injection "
            f"only runs on LocalScripts, so demoting here breaks the chain."
        )


class TestClientServerClassification:
    def test_local_player_becomes_local_script(self):
        scripts = [
            RbxScript(
                name="PlayerController",
                source='local Players = game:GetService("Players")\nlocal player = Players.LocalPlayer',
                script_type="Script",
            ),
        ]
        fixes = fix_require_classifications(scripts)
        assert scripts[0].script_type == "LocalScript"
        assert fixes >= 1

    def test_user_input_service_becomes_local_script(self):
        scripts = [
            RbxScript(
                name="InputHandler",
                source='local UIS = game:GetService("UserInputService")\nUIS.InputBegan:Connect(function() end)',
                script_type="Script",
            ),
        ]
        fixes = fix_require_classifications(scripts)
        assert scripts[0].script_type == "LocalScript"

    def test_server_only_stays_script(self):
        scripts = [
            RbxScript(
                name="DataHandler",
                source='local DSS = game:GetService("DataStoreService")\nlocal ds = DSS:GetDataStore("test")',
                script_type="Script",
            ),
        ]
        fixes = fix_require_classifications(scripts)
        assert scripts[0].script_type == "Script"

    def test_mixed_client_server_stays_put(self):
        """Scripts with both client AND server APIs shouldn't be reclassified."""
        scripts = [
            RbxScript(
                name="MixedScript",
                source='local p = Players.LocalPlayer\nremote.OnServerEvent:Connect(function() end)',
                script_type="Script",
            ),
        ]
        fix_require_classifications(scripts)
        # Should stay as Script since it has server-only patterns too
        assert scripts[0].script_type == "Script"

    def test_module_script_not_reclassified(self):
        """ModuleScripts should never be reclassified by client/server detection."""
        scripts = [
            RbxScript(
                name="ClientModule",
                source='local M = {}\nlocal p = Players.LocalPlayer\nreturn M',
                script_type="ModuleScript",
            ),
        ]
        fix_require_classifications(scripts)
        assert scripts[0].script_type == "ModuleScript"

    def test_current_camera_becomes_local(self):
        scripts = [
            RbxScript(
                name="CameraScript",
                source="local camera = workspace.CurrentCamera\ncamera.CFrame = CFrame.new(0,10,0)",
                script_type="Script",
            ),
        ]
        fix_require_classifications(scripts)
        assert scripts[0].script_type == "LocalScript"

    def test_already_local_script_unchanged(self):
        scripts = [
            RbxScript(
                name="AlreadyLocal",
                source='local p = Players.LocalPlayer',
                script_type="LocalScript",
            ),
        ]
        fixes = fix_require_classifications(scripts)
        assert scripts[0].script_type == "LocalScript"
        # Should not count as a fix since it was already correct
        assert fixes == 0


class TestBreakCircularRequires:
    def test_breaks_direct_cycle(self):
        """A requires B, B requires A -- one direction gets lazy proxy."""
        scripts = [
            RbxScript(
                name="ModA",
                source=(
                    'local ModB = require(game:GetService("ReplicatedStorage")'
                    ':FindFirstChild("ModB", true))\n'
                    'local ModA = {}\n'
                    'function ModA.foo() return ModB.bar() end\n'
                    'return ModA\n'
                ),
                script_type="ModuleScript",
            ),
            RbxScript(
                name="ModB",
                source=(
                    'local ModA = require(game:GetService("ReplicatedStorage")'
                    ':FindFirstChild("ModA", true))\n'
                    'local ModB = {}\n'
                    'function ModB.bar() return 42 end\n'
                    'return ModB\n'
                ),
                script_type="ModuleScript",
            ),
        ]
        fixes = _break_circular_requires(scripts)
        assert fixes == 1
        # Exactly one script should have a lazy proxy
        has_proxy = [s for s in scripts if 'setmetatable' in s.source and '__index' in s.source]
        assert len(has_proxy) == 1
        # The other references to the module name should NOT have been replaced
        proxy_script = has_proxy[0]
        if proxy_script.name == "ModA":
            # ModA had `ModB.bar()` -- that should still say ModB, not _get_ModB()
            assert 'ModB.bar()' in proxy_script.source
            assert '_get_ModB' not in proxy_script.source
        else:
            assert 'ModA.' in proxy_script.source or 'ModA)' in proxy_script.source

    def test_does_not_break_non_cycle(self):
        """A requires B, B does not require A -- no changes."""
        scripts = [
            RbxScript(
                name="ModA",
                source=(
                    'local ModB = require(game:GetService("ReplicatedStorage")'
                    ':FindFirstChild("ModB", true))\n'
                    'return {}\n'
                ),
                script_type="ModuleScript",
            ),
            RbxScript(
                name="ModB",
                source='local ModB = {}\nreturn ModB\n',
                script_type="ModuleScript",
            ),
        ]
        fixes = _break_circular_requires(scripts)
        assert fixes == 0

    def test_only_breaks_one_direction(self):
        """Should not break both directions of a cycle."""
        scripts = [
            RbxScript(
                name="X",
                source=(
                    'local Y = require(game:GetService("ReplicatedStorage")'
                    ':FindFirstChild("Y", true))\n'
                    'local X = {}\nreturn X\n'
                ),
                script_type="ModuleScript",
            ),
            RbxScript(
                name="Y",
                source=(
                    'local X = require(game:GetService("ReplicatedStorage")'
                    ':FindFirstChild("X", true))\n'
                    'local Y = {}\nreturn Y\n'
                ),
                script_type="ModuleScript",
            ),
        ]
        fixes = _break_circular_requires(scripts)
        assert fixes == 1  # Only one direction broken



class TestFixCloneVisibility:
    """The injected clone-visibility helper used to read `clone.PrimaryPart`
    unconditionally. PrimaryPart only exists on Model — a bare-Part clone
    raises 'PrimaryPart is not a valid member of Part' at runtime, even when
    the read is the LHS of an `or`. The fix wraps the access in an IsA
    branch so the helper works for both Part and Model clones.

    Regression for the SimpleFPS HostilePlane error surfaced via Studio
    smoke test on 2026-04-27.
    """

    def _wrap(self, clone_block: str) -> str:
        # _fix_clone_visibility scans for a `local X = Y:Clone()` line
        # followed within 20 lines by `X.Parent`. Provide both anchors.
        return (
            "local templates = ReplicatedStorage:FindFirstChild(\"Templates\")\n"
            "local prefab = templates:FindFirstChild(\"Bullet\")\n"
            f"{clone_block}\n"
            "    bullet.Parent = workspace\n"
            "end\n"
        )

    def test_unguarded_primarypart_access_is_replaced(self):
        scripts = [
            RbxScript(
                name="Shooter",
                source=self._wrap("if prefab then\n    local bullet = prefab:Clone()"),
                script_type="Script",
            ),
        ]
        fixes = _fix_clone_visibility(scripts)
        assert fixes == 1
        # The bare `bullet.PrimaryPart or ...` pattern must NOT appear.
        assert "local _primary = bullet.PrimaryPart or" not in scripts[0].source
        # The IsA-guarded branch should be present.
        assert "if bullet:IsA(\"BasePart\") then" in scripts[0].source
        assert "elseif bullet:IsA(\"Model\") then" in scripts[0].source

    def test_part_branch_resets_visibility_on_the_clone_itself(self):
        # When the clone IS a BasePart, the helper must reset Transparency,
        # Anchored, CanCollide on the clone itself (descendants don't include it).
        scripts = [
            RbxScript(
                name="Shooter",
                source=self._wrap("if prefab then\n    local bullet = prefab:Clone()"),
                script_type="Script",
            ),
        ]
        _fix_clone_visibility(scripts)
        src = scripts[0].source
        # Locate the BasePart branch and verify all three resets are inside it.
        part_branch_start = src.index("if bullet:IsA(\"BasePart\") then")
        part_branch_end = src.index("elseif bullet:IsA(\"Model\") then")
        part_branch = src[part_branch_start:part_branch_end]
        assert "bullet.Transparency = 0" in part_branch
        assert "bullet.Anchored = false" in part_branch
        assert "bullet.CanCollide = false" in part_branch
        assert "_primary = bullet" in part_branch

    def test_model_branch_resolves_primary_from_primarypart_or_descendant(self):
        scripts = [
            RbxScript(
                name="Shooter",
                source=self._wrap("if prefab then\n    local bullet = prefab:Clone()"),
                script_type="Script",
            ),
        ]
        _fix_clone_visibility(scripts)
        src = scripts[0].source
        model_branch_start = src.index("elseif bullet:IsA(\"Model\") then")
        model_branch_end = src.index("end\n", model_branch_start)
        model_branch = src[model_branch_start:model_branch_end]
        # Reading PrimaryPart inside an IsA("Model") branch is safe.
        assert "_primary = bullet.PrimaryPart or bullet:FindFirstChildWhichIsA(\"BasePart\")" in model_branch

    def test_idempotent_on_repeat_application(self):
        # If the fix has already been applied (marker in source), don't
        # re-inject — would produce duplicate _primary declarations.
        scripts = [
            RbxScript(
                name="Shooter",
                source=self._wrap("if prefab then\n    local bullet = prefab:Clone()"),
                script_type="Script",
            ),
        ]
        first = _fix_clone_visibility(scripts)
        assert first == 1
        second = _fix_clone_visibility(scripts)
        assert second == 0
        assert scripts[0].source.count("Fix clone visibility and weld") == 1


class TestInjectRequireCallsFallback:
    """The storage classifier may park a server-only ModuleScript in
    ServerStorage instead of ReplicatedStorage. Hardcoding the require
    target to ReplicatedStorage produces ``require(nil)`` at runtime.

    The injected require must search both ReplicatedStorage AND
    ServerStorage so it survives whichever container the classifier picks.

    Regression for the SimpleFPS GerstnerDisplace error surfaced via Studio
    smoke test on 2026-04-27 — Displace was parented under ServerStorage
    but GerstnerDisplace's require looked in ReplicatedStorage.
    """

    def test_emitted_require_searches_both_storages(self):
        scripts = [
            RbxScript(name="Caller", source="-- empty\n", script_type="Script"),
            RbxScript(name="Helper", source="local M = {}\nreturn M\n", script_type="Script"),
        ]
        injected = inject_require_calls(scripts, {"Caller": ["Helper"]})
        assert injected == 1
        src = scripts[0].source
        assert 'local Helper = require(' in src
        assert 'ReplicatedStorage' in src
        assert 'ServerStorage' in src
        assert ' or ' in src  # fallback chain glue

    def test_no_injection_for_already_present_require(self):
        # If the caller already requires Helper, don't double-inject.
        existing = (
            'local Helper = require(game:GetService("ReplicatedStorage")'
            ':FindFirstChild("Helper", true))\n'
        )
        scripts = [
            RbxScript(name="Caller", source=existing, script_type="Script"),
            RbxScript(name="Helper", source="local M = {}\nreturn M\n", script_type="Script"),
        ]
        injected = inject_require_calls(scripts, {"Caller": ["Helper"]})
        assert injected == 0
        # Original require line is preserved verbatim.
        assert existing.strip() in scripts[0].source


class TestFixPrefabLookupsTemplatesExemption:
    """The prefab_packages emit puts gameplay templates under
    ReplicatedStorage.Templates. _fix_prefab_lookups must NOT rewrite
    `local templates = ReplicatedStorage:FindFirstChild("Templates")` to
    a workspace search — that breaks every transpiled script that clones
    prefabs (rifle pickup, plane spawning, ammo, etc.).
    """

    def test_templates_lookup_is_preserved(self):
        scripts = [
            RbxScript(
                name="Player",
                source='local templates = ReplicatedStorage:FindFirstChild("Templates")\n',
                script_type="LocalScript",
            ),
        ]
        _fix_prefab_lookups(scripts)
        # Must keep the ReplicatedStorage path (not redirect to workspace)
        assert 'ReplicatedStorage:FindFirstChild("Templates")' in scripts[0].source
        assert 'workspace:FindFirstChild("Templates"' not in scripts[0].source

    def test_non_templates_prefab_lookup_still_redirected(self):
        # Sanity: the function still redirects unfamiliar names so other
        # transpiler-emitted prefab lookups continue to be fixed.
        scripts = [
            RbxScript(
                name="Spawner",
                source='local rifle = ReplicatedStorage:FindFirstChild("Rifle")\n',
                script_type="Script",
            ),
        ]
        _fix_prefab_lookups(scripts)
        assert 'workspace:FindFirstChild("Rifle"' in scripts[0].source


class TestRemoveStalePlayerRequires:
    """When the AI transpiler emits Player-as-module references but Player
    is actually a LocalScript, the rewrite must:
    1. Only run on client-side (LocalScript) scripts — server-side has no
       LocalPlayer, so injecting Players.LocalPlayer:WaitForChild crashes.
    2. Coherently rewrite the binding line, the require, AND any follow-up
       varname:WaitForChild uses — leaving an orphan was the original bug.
    """

    def _make_player_local(self):
        return RbxScript(name="Player", source="local M={};return M", script_type="LocalScript")

    def test_does_not_touch_server_scripts(self):
        # Codex P1: server Script with `:WaitForChild("Player")` must NOT
        # be rewritten to use Players.LocalPlayer — that crashes server-side.
        server_src = (
            'local s = workspace.Foo\n'
            'local playerScript = s:WaitForChild("Player")\n'
            'local Player = require(playerScript)\n'
        )
        scripts = [
            self._make_player_local(),
            RbxScript(name="ServerThing", source=server_src, script_type="Script"),
        ]
        _remove_stale_player_requires(scripts)
        # Server script untouched
        assert scripts[1].source == server_src

    def test_does_not_touch_module_scripts(self):
        mod_src = 'local p = parent:WaitForChild("Player")\n'
        scripts = [
            self._make_player_local(),
            RbxScript(name="Mod", source=mod_src, script_type="ModuleScript"),
        ]
        _remove_stale_player_requires(scripts)
        assert scripts[1].source == mod_src

    def test_coherent_rewrite_of_local_script(self):
        # All three idioms must be rewritten together: the binding, the
        # require, and follow-up varname:WaitForChild lines.
        client_src = (
            'local playerScript = ReplicatedStorage:WaitForChild("Player")\n'
            'local Player = require(playerScript)\n'
            'local healthUpdate = playerScript:WaitForChild("HealthUpdate")\n'
        )
        scripts = [
            self._make_player_local(),
            RbxScript(name="HudControl", source=client_src, script_type="LocalScript"),
        ]
        _remove_stale_player_requires(scripts)
        out = scripts[1].source
        # Binding redirected to LocalPlayer.PlayerScripts (not script.Parent —
        # that would trigger the BasePart-guard heuristic in pipeline)
        assert 'LocalPlayer:WaitForChild("PlayerScripts"):WaitForChild("Player")' in out
        # require(playerScript) stubbed out, not left orphan
        assert 'require(playerScript)' not in out
        assert 'local Player = nil' in out
        # Follow-up access still works because varname is now bound
        assert 'playerScript:WaitForChild("HealthUpdate")' in out

    def test_no_op_when_player_is_not_local(self):
        # If Player is a ModuleScript (e.g. classified by the require pass),
        # the rewrite should not run at all.
        scripts = [
            RbxScript(name="Player", source="return {}", script_type="ModuleScript"),
            RbxScript(
                name="Other",
                source='local p = ReplicatedStorage:WaitForChild("Player")\n',
                script_type="LocalScript",
            ),
        ]
        fixes = _remove_stale_player_requires(scripts)
        assert fixes == 0


class TestDisableDefaultControlsInFpsScripts:
    """The pass that prepends a PlayerModule:GetControls():Disable() block
    to every LocalScript that sets MouseBehavior=LockCenter.
    """

    def test_prepends_disable_block_to_fps_local_script(self):
        scripts = [
            RbxScript(
                name="FpsController",
                source='UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n',
                script_type="LocalScript",
            ),
        ]
        fixes = _disable_default_controls_in_fps_scripts(scripts)
        assert fixes == 1
        assert "disable default PlayerModule controls" in scripts[0].source
        assert "_applyFpsMouseState" in scripts[0].source
        # The disable must come BEFORE the original source
        assert scripts[0].source.endswith(
            'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
        )

    def test_idempotent_on_repeat_application(self):
        # Running the pass twice must not double-prepend the block.
        scripts = [
            RbxScript(
                name="FpsController",
                source='UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n',
                script_type="LocalScript",
            ),
        ]
        _disable_default_controls_in_fps_scripts(scripts)
        first_pass_len = len(scripts[0].source)
        _disable_default_controls_in_fps_scripts(scripts)
        # Second pass should be a no-op (marker line already present)
        assert len(scripts[0].source) == first_pass_len

    def test_does_not_touch_non_local_scripts(self):
        # A server Script that happens to set MouseBehavior (rare, but
        # possible in transpiled output) shouldn't get the client-only
        # PlayerModule disable injected — Players.LocalPlayer is nil.
        server_src = 'UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n'
        scripts = [
            RbxScript(name="ServerWeird", source=server_src, script_type="Script"),
            RbxScript(name="ModWeird", source=server_src, script_type="ModuleScript"),
        ]
        _disable_default_controls_in_fps_scripts(scripts)
        assert scripts[0].source == server_src
        assert scripts[1].source == server_src

    def test_skips_scripts_without_lockcenter(self):
        scripts = [
            RbxScript(
                name="OtherClient",
                source='print("hello")\n',
                script_type="LocalScript",
            ),
        ]
        fixes = _disable_default_controls_in_fps_scripts(scripts)
        assert fixes == 0
        assert scripts[0].source == 'print("hello")\n'

    def test_first_person_hide_block_present(self):
        """The setup block must hide character body parts and accessories
        for first-person view, with a WeaponSlot exemption so the held
        weapon stays visible. Roblox loads accessories asynchronously
        after CharacterAdded, so DescendantAdded must wire up first
        (otherwise an accessory delivered between snapshot capture and
        Connect slips past both passes).
        """
        scripts = [
            RbxScript(
                name="FpsController",
                source='UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n',
                script_type="LocalScript",
            ),
        ]
        _disable_default_controls_in_fps_scripts(scripts)
        src = scripts[0].source
        assert "_isInWeaponSlot" in src
        assert "LocalTransparencyModifier = 1" in src
        # Connect must come BEFORE GetDescendants so accessories added
        # in the gap aren't missed. Indices guard the order: a future
        # edit that swaps them will trip this.
        connect_idx = src.find("char.DescendantAdded:Connect(_hidePart)")
        iterate_idx = src.find("for _, part in char:GetDescendants()")
        assert connect_idx > 0 and iterate_idx > 0
        assert connect_idx < iterate_idx, (
            "DescendantAdded must connect before GetDescendants iterate; "
            "swapping them lets late-loaded accessories slip past."
        )

    def test_spawn_floor_snap_block_present(self):
        """The setup block must include a runtime snap-to-floor pass so a
        character that respawns over a gap (Unity SpawnPoint with no
        physical floor under it) gets reseated on the surface below.
        Verbatim assertions lock down the three properties Codex round-1
        found broken in the first cut: ray origin must be at HRP (not
        above — overhead bridges would hit first), the threshold must
        compare to the SNAP TARGET (else a normally-grounded character
        with HRP ~3 studs above the floor re-snaps every spawn), and
        the snap must preserve rotation (CFrame.new(pos) would zero it).
        """
        scripts = [
            RbxScript(
                name="FpsController",
                source='UserInputService.MouseBehavior = Enum.MouseBehavior.LockCenter\n',
                script_type="LocalScript",
            ),
        ]
        _disable_default_controls_in_fps_scripts(scripts)
        src = scripts[0].source

        # Ray origin must be HRP itself (filter-excluded), not HRP + offset.
        assert "workspace:Raycast(hrp.Position, Vector3.new(0, -200, 0)" in src
        assert "hrp.Position + Vector3.new(0, 5, 0)" not in src, (
            "ray origin starting above HRP can hit overhead geometry "
            "first and snap the player upward onto a ceiling"
        )

        # Threshold must compare HRP to the snap TARGET, not the raw hit.
        # Comparing to hit means a normal character (HRP ~3 studs above
        # floor) always re-snaps on every CharacterAdded.
        assert "(hrp.Position - target).Magnitude > 2" in src
        assert "(hrp.Position - hit.Position).Magnitude > 2" not in src

        # Snap must preserve CFrame rotation (the character's facing
        # direction). CFrame.new(pos) resets to identity.
        assert "hrp.CFrame = hrp.CFrame + (target - hrp.Position)" in src
        assert "hrp.CFrame = CFrame.new(hit.Position" not in src

        # Must fire on initial character AND every respawn, not once.
        assert src.count("_snapToFloor(") >= 2
