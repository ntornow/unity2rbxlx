"""
test_script_coherence.py -- Tests for cross-script consistency fixes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript
from converter.script_coherence import fix_require_classifications, _break_circular_requires


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
