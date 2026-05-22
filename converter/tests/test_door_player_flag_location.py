"""Tests for the door_player_flag_location coherence pack.

The pickup writes the ``has<Item>`` flag onto the Character + Player, but the
AI transpiles Door.cs to read it from the HumanoidRootPart — a location nobody
writes — so the door never opens. The pack rewrites playerWithKey() to read
from the Player (the canonical, pickup-written store).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript
from converter.script_coherence_packs import run_packs

PACK = {"door_player_flag_location"}

# Mirrors the real transpiled Door.luau playerWithKey shape.
def _door_source(flag: str = "hasKey") -> str:
    return (
        'local Players = game:GetService("Players")\n'
        "local function playerWithKey(otherPart)\n"
        "\tlocal model = otherPart and otherPart.Parent\n"
        "\tif not model then return false end\n"
        "\tlocal player = Players:GetPlayerFromCharacter(model)\n"
        "\tif not player then return false end\n"
        '\tlocal rootPart = model:FindFirstChild("HumanoidRootPart")\n'
        "\tif not rootPart then return false end\n"
        f'\treturn rootPart:GetAttribute("{flag}") == true\n'
        "end\n"
        "-- trailing door logic below stays intact\n"
        "local trigger = nil\n"
    )


def _run(source: str, name: str = "Door") -> RbxScript:
    s = RbxScript(name=name, source=source, script_type="Script")
    run_packs([s], enabled=PACK)
    return s


class TestRewrite:
    def test_reads_from_player_not_rootpart(self):
        s = _run(_door_source("hasKey"))
        assert 'player:GetAttribute("hasKey") == true' in s.source
        assert "HumanoidRootPart" not in s.source
        assert "rootPart:GetAttribute" not in s.source

    def test_resolves_character_via_ancestor(self):
        # Bonus fix: accessory/nested-part touches must still find the Model.
        s = _run(_door_source())
        assert 'otherPart:FindFirstAncestorOfClass("Model")' in s.source
        assert "otherPart and otherPart.Parent" not in s.source

    def test_flag_name_not_hardcoded(self):
        # A different game's flag must be preserved, not replaced with hasKey.
        s = _run(_door_source("hasGreenKeycard"))
        assert 'player:GetAttribute("hasGreenKeycard") == true' in s.source
        assert "hasKey" not in s.source

    def test_trailing_logic_preserved(self):
        s = _run(_door_source())
        assert "-- trailing door logic below stays intact" in s.source
        assert "local trigger = nil" in s.source


class TestScoping:
    def test_non_door_script_untouched(self):
        src = _door_source()
        s = _run(src, name="NotADoor")
        assert s.source == src

    def test_earlier_unrelated_getattribute_not_used_as_flag(self):
        # A probe before the HumanoidRootPart read must NOT be mistaken for the
        # key flag — the flag is the one read off the HumanoidRootPart.
        src = (
            'local Players = game:GetService("Players")\n'
            "local function playerWithKey(otherPart)\n"
            "\tlocal model = otherPart and otherPart.Parent\n"
            "\tif not model then return false end\n"
            '\tif otherPart:GetAttribute("Locked") then return false end\n'
            "\tlocal player = Players:GetPlayerFromCharacter(model)\n"
            "\tif not player then return false end\n"
            '\tlocal rootPart = model:FindFirstChild("HumanoidRootPart")\n'
            "\tif not rootPart then return false end\n"
            '\treturn rootPart:GetAttribute("hasKey") == true\n'
            "end\n"
        )
        s = _run(src)
        assert 'player:GetAttribute("hasKey") == true' in s.source
        assert 'GetAttribute("Locked")' not in s.source

    def test_door_without_hrp_read_untouched(self):
        # Already-correct door (reads player) must not be rewritten.
        src = (
            'local Players = game:GetService("Players")\n'
            "local function playerWithKey(otherPart)\n"
            '\tlocal model = otherPart and otherPart:FindFirstAncestorOfClass("Model")\n'
            "\tif not model then return false end\n"
            "\tlocal player = Players:GetPlayerFromCharacter(model)\n"
            "\tif not player then return false end\n"
            '\treturn player:GetAttribute("hasKey") == true\n'
            "end\n"
        )
        s = _run(src)
        assert s.source == src


class TestIdempotency:
    def test_running_twice_equals_once(self):
        s = RbxScript(name="Door", source=_door_source(), script_type="Script")
        run_packs([s], enabled=PACK)
        once = s.source
        run_packs([s], enabled=PACK)
        assert s.source == once
