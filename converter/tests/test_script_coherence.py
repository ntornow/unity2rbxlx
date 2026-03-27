"""
test_script_coherence.py -- Tests for cross-script consistency fixes.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript
from converter.script_coherence import fix_require_classifications


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
