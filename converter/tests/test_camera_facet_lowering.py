"""Tests for the camera-facet lowering pass (generic allowlist).

Routes a flattened first-person controller's look facet onto the
SceneCameraInput runtime service. Structure-gated, deterministic, idempotent;
leaves Move / Shoot-raycast / the read-only self.cam alias intact.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.camera_facet_lowering import lower_camera_facet  # noqa: E402


class _S:
    """Minimal TranspiledScript stand-in (carries ``luau_source``)."""

    def __init__(self, src: str) -> None:
        self.luau_source = src


# Canonical flattened-FPS controller (the shape the transpiler emits).
_CONTROLLER = textwrap.dedent("""\
    local Player = {}
    Player.__index = Player

    function Player:Awake()
        self.cam = workspace.CurrentCamera
        self.weaponSlot = self.cam and self.cam:GetChildren()[1]
    end

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

    function Player:Move(dt)
        local md = self.gameObject:GetPivot():VectorToWorldSpace(Vector3.new(1, 0, 0))
        self.gameObject:PivotTo(self.gameObject:GetPivot() + md)
    end

    function Player:Shoot(dt)
        self.camRotationX = self.camRotationX - 2
        local origin = self.cam.CFrame.Position
        local look = self.cam.CFrame.LookVector
    end

    return Player
""")


class TestLowering:
    def test_fires_and_routes_look_to_service(self) -> None:
        s = _S(_CONTROLLER)
        assert lower_camera_facet([s]) == 1
        src = s.luau_source
        # Rotate body becomes lazy-acquire + step; the buggy camera math is gone.
        assert 'require(game:GetService("ReplicatedStorage"):WaitForChild("SceneCameraInput"))' in src
        assert ":step(dt)" in src
        assert "self._cam:configure({rig = self.gameObject})" in src
        assert "CFrame.new(pos) * CFrame.Angles(math.rad(self.camRotationX), 0, 0)" not in src

    def test_recoil_routed_with_sign(self) -> None:
        s = _S(_CONTROLLER)
        lower_camera_facet([s])
        # Shoot's ``camRotationX = camRotationX - 2`` -> applyRecoil(-rad(2)).
        assert "self._cam:applyRecoil(-math.rad(2))" in s.luau_source
        assert "self.camRotationX = self.camRotationX - 2" not in s.luau_source

    def test_recoil_plus_sign(self) -> None:
        src = _CONTROLLER.replace(
            "self.camRotationX = self.camRotationX - 2",
            "self.camRotationX = self.camRotationX + 3",
        )
        s = _S(src)
        lower_camera_facet([s])
        assert "self._cam:applyRecoil(math.rad(3))" in s.luau_source

    def test_leaves_move_and_raycast_and_alias_intact(self) -> None:
        s = _S(_CONTROLLER)
        lower_camera_facet([s])
        src = s.luau_source
        # Awake read-alias preserved (Shoot raycast + weaponSlot depend on it).
        assert "self.cam = workspace.CurrentCamera" in src
        assert "self.weaponSlot = self.cam and self.cam:GetChildren()[1]" in src
        # Shoot's raycast reads stay; Move untouched.
        assert "local origin = self.cam.CFrame.Position" in src
        assert "self.gameObject:GetPivot():VectorToWorldSpace(Vector3.new(1, 0, 0))" in src

    def test_idempotent(self) -> None:
        s = _S(_CONTROLLER)
        lower_camera_facet([s])
        once = s.luau_source
        assert lower_camera_facet([s]) == 0
        assert s.luau_source == once
        assert once.count(":step(") == 1
        assert once.count("applyRecoil") == 1


class TestNegative:
    def test_no_op_without_pitch_only_camera(self) -> None:
        # Yaw turn present, but the camera rebuild has a real (non-zero) yaw
        # term -> not the flattened fingerprint.
        src = _CONTROLLER.replace(
            "self.cam.CFrame = CFrame.new(pos) * CFrame.Angles(math.rad(self.camRotationX), 0, 0)",
            "self.cam.CFrame = CFrame.new(pos) * CFrame.Angles(math.rad(self.camRotationX), yaw, 0)",
        )
        s = _S(src)
        assert lower_camera_facet([s]) == 0
        assert s.luau_source == src

    def test_no_op_without_body_yaw(self) -> None:
        src = _CONTROLLER.replace(
            "self.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.Angles(0, -math.rad(yaw), 0))",
            "-- (no body yaw here)",
        )
        s = _S(src)
        assert lower_camera_facet([s]) == 0

    def test_no_op_when_fingerprint_in_string_or_comment(self) -> None:
        src = textwrap.dedent("""\
            local M = {}
            function M:tick(dt)
                -- self.gameObject:PivotTo(self.gameObject:GetPivot() * CFrame.Angles(0, x, 0))
                local doc = "self.cam.CFrame = CFrame.new(p) * CFrame.Angles(q, 0, 0)"
                return doc
            end
            return M
        """)
        s = _S(src)
        assert lower_camera_facet([s]) == 0
        assert s.luau_source == src

    def test_no_op_on_plain_module(self) -> None:
        s = _S("local M = {}\nreturn M\n")
        assert lower_camera_facet([s]) == 0
