"""
test_script_coherence.py -- Tests for cross-script consistency fixes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript
from converter.script_coherence import (
    fix_require_classifications,
    _break_circular_requires,
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
