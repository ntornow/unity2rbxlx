"""
test_studio_behavior_driver.py -- unit tests for the driver CLI's pure
transforms. No MCP, no Studio, no subprocess.

The mouse-action mapping is the load-bearing one: it surfaced as a live
bug on 2026-05-22 when the skill sent the fixture's raw
``{button: "left"}`` shape to ``user_mouse_input`` and MCP rejected it
with "Unknown mouse action". The driver now translates fixture mouse
vocabulary into the MCP action enum so the skill never has to.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.studio_behavior_driver import (  # noqa: E402
    _fixture_to_plan,
    _mouse_action_to_mcp,
)


class TestMouseActionMapping:
    def test_mouse_click_maps_to_mcp(self):
        out = _mouse_action_to_mcp({"kind": "mouse_click", "button": "left"})
        assert out == {"action": "mouseButtonClick", "mouse_button": "left"}

    def test_mouse_click_defaults_to_left(self):
        out = _mouse_action_to_mcp({"kind": "mouse_click"})
        assert out["action"] == "mouseButtonClick"
        assert out["mouse_button"] == "left"

    def test_right_click(self):
        out = _mouse_action_to_mcp({"kind": "mouse_click", "button": "right"})
        assert out["mouse_button"] == "right"

    def test_mouse_move_maps_to_moveTo_with_coords(self):
        out = _mouse_action_to_mcp({"kind": "mouse_move", "x": 100, "y": 200})
        assert out == {"action": "moveTo", "x": 100, "y": 200}

    def test_click_carries_coordinates(self):
        out = _mouse_action_to_mcp({"kind": "mouse_click", "button": "left", "x": 400, "y": 300})
        assert out["action"] == "mouseButtonClick"
        assert out["x"] == 400 and out["y"] == 300

    def test_already_mcp_shaped_passes_through(self):
        out = _mouse_action_to_mcp({"action": "scrollUp"})
        assert out["action"] == "scrollUp"


class TestFixtureToPlan:
    def _preamble(self):
        return "local x = 1"

    def test_shoot_fixture_emits_mcp_ready_click(self):
        fixture = {
            "id": "shoot",
            "assert_luau": "return true",
            "expect": True,
            "input_sequence": [{"kind": "mouse_click", "button": "left"}],
        }
        plan = _fixture_to_plan(fixture, self._preamble())
        assert len(plan["input_sequence"]) == 1
        entry = plan["input_sequence"][0]
        assert entry["type"] == "mouse"
        # The action must be directly usable by mcp user_mouse_input.
        assert entry["action"]["action"] == "mouseButtonClick"
        assert entry["action"]["mouse_button"] == "left"

    def test_keyboard_fixture_passes_through(self):
        fixture = {
            "id": "wasd",
            "assert_luau": "return true",
            "expect": True,
            "input_sequence": [{"kind": "keyboard", "action": "keyDown", "key_code": "W"}],
        }
        plan = _fixture_to_plan(fixture, self._preamble())
        entry = plan["input_sequence"][0]
        assert entry["type"] == "keyboard"
        # Keyboard already matches mcp shape; the 'kind' tag is stripped.
        assert entry["action"] == {"action": "keyDown", "key_code": "W"}

    def test_assert_timeout_carried_through(self):
        fixture = {
            "id": "mouse_yaw",
            "assert_luau": "return true",
            "expect": True,
            "assert_timeout_seconds": 2.0,
        }
        plan = _fixture_to_plan(fixture, self._preamble())
        assert plan["assert_timeout_seconds"] == 2.0
