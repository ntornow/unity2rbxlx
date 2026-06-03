"""
test_fps_camera_yaw_pack.py — Pin the ``fps_camera_yaw_from_player_pivot``
coherence pack.

Converted Unity FPS controllers can pitch up/down but not yaw left/right:
the C#->Luau transpiler flattens Unity's camera-child-of-player hierarchy,
so the controller yaws the player object via ``PivotTo`` but rebuilds the
camera's WORLD CFrame each frame as ``CFrame.new(pos) * CFrame.Angles(pitch,
0, 0)`` with the yaw component hard-set to 0. The camera never inherits the
player's yaw. ``mouse_yaw_rotates_camera`` is the known-failing e2e fixture.

The pack injects the yawing object's ``GetPivot().Rotation`` into the camera
CFrame so it inherits the world yaw, preserving position + pitch.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript  # noqa: E402
from converter import script_coherence_packs as packs  # noqa: E402

# The real flattened-FPS shape the transpiler emits for Player.cs (OOP /
# ``self.``-method form, as produced by a generic-mode conversion).
_FLATTENED_FPS = """\
local Player = {}
Player.__index = Player

function Player:Rotate(dt)
    local UIS = game:GetService("UserInputService")
    local delta = UIS:GetMouseDelta()

    local yaw = self.sensitivity * dt * delta.X
    self.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.Angles(0, -math.rad(yaw), 0))

    self.camRotationX = self.camRotationX - delta.Y * self.sensitivity * dt
    self.camRotationX = math.clamp(self.camRotationX, self.minAngle, self.maxAngle)

    if self.cam then
        local pos = self.cam.CFrame.Position
        self.cam.CFrame = CFrame.new(pos) * CFrame.Angles(math.rad(self.camRotationX), 0, 0)
    end
end

return Player
"""

_FIXED_CAMERA_LINE = (
    "self.cam.CFrame = CFrame.new(pos) * self.gameObject:GetPivot().Rotation"
    " * CFrame.Angles(math.rad(self.camRotationX), 0, 0)"
)


class TestDetect:
    def test_fires_on_flattened_fps(self) -> None:
        s = RbxScript(name="Player", source=_FLATTENED_FPS, script_type="ModuleScript")
        assert packs._detect_fps_camera_yaw_lost([s]) is True
        edits = packs._fps_yaw_camera_edits(s)
        assert len(edits) == 1
        assert edits[0][1] == "self.gameObject"

    def test_no_op_without_body_yaw(self) -> None:
        """A pitch-only camera rebuild alone (no body yaw in the same
        script) is not the flattened-FPS fingerprint."""
        src = (
            "local C = {}\n"
            "function C:tick()\n"
            "    cam.CFrame = CFrame.new(pos) * CFrame.Angles(math.rad(self.p), 0, 0)\n"
            "end\n"
            "return C\n"
        )
        s = RbxScript(name="C", source=src, script_type="ModuleScript")
        assert packs._detect_fps_camera_yaw_lost([s]) is False

    def test_no_op_on_three_axis_angles(self) -> None:
        """Real 3-axis CFrame.Angles(a, b, c) keyframes (animation files)
        are not pitch-only and must not match -- even alongside a body
        yaw."""
        src = (
            "local A = {}\n"
            "function A:f()\n"
            "    self.go:PivotTo(self.go:GetPivot() * CFrame.Angles(0, x, 0))\n"
            "    part.CFrame = CFrame.new(basePos) * "
            "CFrame.Angles(math.rad(83.24), math.rad(17.93), math.rad(-21.07))\n"
            "end\n"
            "return A\n"
        )
        s = RbxScript(name="A", source=src, script_type="ModuleScript")
        assert packs._detect_fps_camera_yaw_lost([s]) is False

    def test_no_op_on_body_turn_with_roll(self) -> None:
        """A body turn that also rolls (leaning/banking) --
        CFrame.Angles(0, yaw, roll) -- is NOT yaw-only; injecting its full
        .Rotation would leak roll into the camera, so it must not match."""
        src = (
            "local C = {}\n"
            "function C:f()\n"
            "    self.go:PivotTo(self.go:GetPivot() * CFrame.Angles(0, yaw, self.lean))\n"
            "    cam.CFrame = CFrame.new(pos) * CFrame.Angles(p, 0, 0)\n"
            "end\n"
            "return C\n"
        )
        s = RbxScript(name="C", source=src, script_type="ModuleScript")
        assert packs._detect_fps_camera_yaw_lost([s]) is False
        assert packs._fix_fps_camera_yaw_from_player_pivot([s]) == 0

    def test_string_and_comment_are_not_signals(self) -> None:
        """The camera fingerprint inside a string/comment must not trip
        detection -- it runs on the comment/string-blanked view."""
        src = (
            "local C = {}\n"
            "function C:f()\n"
            "    self.go:PivotTo(self.go:GetPivot() * CFrame.Angles(0, x, 0))\n"
            "    -- cam.CFrame = CFrame.new(pos) * CFrame.Angles(p, 0, 0)\n"
            '    local s = "cam.CFrame = CFrame.new(pos) * CFrame.Angles(p, 0, 0)"\n'
            "end\n"
            "return C\n"
        )
        s = RbxScript(name="C", source=src, script_type="ModuleScript")
        assert packs._detect_fps_camera_yaw_lost([s]) is False


class TestApply:
    def test_injects_player_yaw_into_camera(self) -> None:
        s = RbxScript(name="Player", source=_FLATTENED_FPS, script_type="ModuleScript")
        n = packs._fix_fps_camera_yaw_from_player_pivot([s])
        assert n == 1
        assert _FIXED_CAMERA_LINE in s.source
        # Pitch term and position are preserved unchanged.
        assert "CFrame.Angles(math.rad(self.camRotationX), 0, 0)" in s.source
        assert "local pos = self.cam.CFrame.Position" in s.source

    def test_idempotent(self) -> None:
        s = RbxScript(name="Player", source=_FLATTENED_FPS, script_type="ModuleScript")
        packs._fix_fps_camera_yaw_from_player_pivot([s])
        once = s.source
        n2 = packs._fix_fps_camera_yaw_from_player_pivot([s])
        assert n2 == 0
        assert s.source == once
        assert packs._detect_fps_camera_yaw_lost([s]) is False
        # Exactly one yaw injection -- no double-apply.
        assert s.source.count("GetPivot().Rotation") == 1

    def test_camera_uses_nearest_preceding_yaw_source(self) -> None:
        """With two yaw-only PivotTo calls (e.g. weapon/viewmodel sway
        BEFORE the player turn), the camera must inherit the yaw from the
        body turn nearest-preceding the camera write -- the player -- not a
        script-wide first match (the weapon)."""
        src = (
            "local C = {}\n"
            "function C:Rotate()\n"
            "    self.weapon:PivotTo(self.weapon:GetPivot() * CFrame.Angles(0, sway, 0))\n"
            "    self.player:PivotTo(self.player:GetPivot() * CFrame.Angles(0, yaw, 0))\n"
            "    self.cam.CFrame = CFrame.new(pos) * CFrame.Angles(p, 0, 0)\n"
            "end\n"
            "return C\n"
        )
        s = RbxScript(name="C", source=src, script_type="ModuleScript")
        edits = packs._fps_yaw_camera_edits(s)
        assert len(edits) == 1
        assert edits[0][1] == "self.player"
        packs._fix_fps_camera_yaw_from_player_pivot([s])
        assert (
            "self.cam.CFrame = CFrame.new(pos) * self.player:GetPivot().Rotation"
            " * CFrame.Angles(p, 0, 0)" in s.source
        )
        # The weapon sway PivotTo is untouched.
        assert "self.weapon:PivotTo(self.weapon:GetPivot() * CFrame.Angles(0, sway, 0))" in s.source

    def test_no_change_on_non_fps(self) -> None:
        src = "local M = {}\nreturn M\n"
        s = RbxScript(name="M", source=src, script_type="ModuleScript")
        assert packs._fix_fps_camera_yaw_from_player_pivot([s]) == 0
        assert s.source == src


class TestRegistration:
    def test_pack_is_registered(self) -> None:
        names = {p.name for p in packs._REGISTRY}
        assert "fps_camera_yaw_from_player_pivot" in names

    def test_runs_after_other_fps_packs(self) -> None:
        pack = next(
            p for p in packs._REGISTRY
            if p.name == "fps_camera_yaw_from_player_pivot"
        )
        assert "fps_camera_pitch_inversion" in pack.after
        assert "fps_e2e_mouse_channel" in pack.after

    def test_composes_through_run_packs(self) -> None:
        """run_packs (single pass) must both inject the E2E channel and
        restore yaw on the flattened-FPS shape."""
        s = RbxScript(name="Player", source=_FLATTENED_FPS, script_type="ModuleScript")
        packs.run_packs([s])
        assert "GetPivot().Rotation * CFrame.Angles(math.rad(self.camRotationX), 0, 0)" in s.source
        assert "E2EMouseSeq" in s.source
