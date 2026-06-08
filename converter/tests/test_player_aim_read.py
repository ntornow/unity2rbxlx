"""Slice 3.2 — aim-read PROOF (test-only; NO production change).

Phase 3's aim-read is ALREADY satisfied by Phase 2's pre-write: the dominant AI
player shape aliases the live camera to a field in ``Awake`` — ``self.cam =
workspace.CurrentCamera`` (fieldcam_player.luau:29 / cold3a59_player.luau:7 /
dde248_player.luau:8 — the SAME object C drives). Its ``Shoot`` raycast reads its
look through that alias (``self.cam.CFrame``). C's ``_playerPreTick`` runs
``_playerWriteCamera`` BEFORE the component ``Update`` pass (scene_runtime.luau:
3047-3052), so a same-frame raw ``self.cam.CFrame`` read DURING ``Update`` already
returns C's live look. No Shoot rewrite, no production change (D-P3-aim).

This module PROVES that same-frame correctness BY EXECUTION under the REAL
``_playerPreTick`` bracket (driven through the REAL ``SceneRuntime:_tick``), not by
string-match:

  * AC2 — seed a non-trivial yaw/pitch on ``self._player``, drive one real
    ``_tick``, and from a synthetic component standing in for Shoot read
    ``self.cam.CFrame`` MID-``Update`` (``self.cam`` captured as an alias of
    ``workspace.CurrentCamera`` in the component's setup, exactly like the AI's
    ``Awake``). Assert that mid-``Update`` aim read == C's pre-write look ==
    ``_composeLook(eye, yaw, pitch)`` == ``host.player:getLookCFrame()``. The aim
    the raycast would use IS C's live look, not a stale value.

  * Stale-aim guard (non-vacuous) — a yaw set on ``self._player`` BEFORE the tick
    is reflected in the same-frame aim read (the pre-write carried it through).

  * Mutation (non-vacuity) — with the camera left STALE (yaw 0) and the pre-write
    suppressed (an empty ``_playerPreTick`` override), the mid-``Update`` alias
    read sees the stale 0, NOT C's live yaw. This is the RED the pre-write turns
    GREEN: it proves the assertion is load-bearing on C's pre-write running.

The REAL ``SceneCameraInput._composeLook`` pure helper + the mock Roblox surface
come from the shared camera-input harness (reuse-not-rebuild). Skips cleanly
without ``luau``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests._camera_input_harness import (
    CAMERA_INPUT_PATH,
    camera_input_preamble,
    run_camera_scenario,
)

HOST_RUNTIME_PATH = (
    Path(__file__).parent.parent / "runtime" / "scene_runtime.luau"
)


def _luau_available() -> bool:
    return shutil.which("luau") is not None


pytestmark = pytest.mark.skipif(
    not _luau_available()
    or not CAMERA_INPUT_PATH.exists()
    or not HOST_RUNTIME_PATH.exists(),
    reason="needs standalone luau interpreter + runtime files",
)


# ---------------------------------------------------------------------------
# Shared scenario prelude: build a real SceneRuntime over an empty plan with the
# injected client services + the REAL composeLook/advance pure helpers, seed a
# bound ``self._player`` at a KNOWN yaw/pitch, and register an "aim reader"
# component standing in for the AI's Shoot. The component aliases
# ``workspace.CurrentCamera`` to ``self.cam`` (mirroring the AI's
# ``self.cam = workspace.CurrentCamera`` in Awake) and records ``self.cam.CFrame``
# mid-``Update`` (= where Shoot's raycast would read the aim). ``pre_override`` is
# inserted right before the ``_tick`` call so the mutation case can suppress the
# pre-write.
# ---------------------------------------------------------------------------

_SEED_YAW = "1.5"
_SEED_PITCH = "0.3"
_EYE_HEIGHT = "1.5"
_HRP_Y = "20"


def _aim_read_body(*, pre_override: str = "", seed_camera_stale: bool = True) -> str:
    stale = (
        """
            -- STALE seed: the camera starts at yaw 0 / pitch 0. ONLY C's
            -- pre-write can move it to the seeded look; a mid-Update alias read
            -- that sees the seeded yaw therefore PROVES the pre-write ran.
            do
                local c = CFrame.new(Vector3.new(0, 0, 0))
                c._yaw = 0
                c._pitch = 0
                workspace.CurrentCamera.CFrame = c
            end
        """
        if seed_camera_stale
        else ""
    )
    return f"""
        local plan = {{modules = {{}}}}
        local services = servicesFor(plan, {{}}, {{}})
        services.isClient = true
        services.userInputService = game:GetService("UserInputService")
        services.players = game:GetService("Players")
        services.cameraAdvance = SceneCameraInput._advance
        services.cameraComposeLook = SceneCameraInput._composeLook
        local engine = SceneRuntime.new(services, plan)

        -- Bind a player at a KNOWN, non-trivial yaw/pitch. No mouse delta this
        -- frame (the harness GetMouseDelta returns 0,0 and no E2E channel is
        -- pushed), so _playerReadInput leaves yaw/pitch unchanged -> the pre-write
        -- look is exactly _composeLook(eye, {_SEED_YAW}, {_SEED_PITCH}).
        engine._player = {{
            _yaw = {_SEED_YAW}, _pitch = {_SEED_PITCH},
            _booted = true, _jumpHeld = false,
            _sensitivity = 0.0045,
            _minPitch = math.rad(-80), _maxPitch = math.rad(80),
            _eyeHeight = {_EYE_HEIGHT},
        }}

        -- Give the player a character + HRP so the eye follows the body (the
        -- live, character-bound look the aim reads), NOT the degenerate
        -- camera-position fallback. Eye = HRP.Position + (0, eyeHeight, 0).
        do
            local hrp = {{Position = Vector3.new(10, {_HRP_Y}, 30)}}
            local character = {{}}
            function character:FindFirstChild(n)
                if n == "HumanoidRootPart" then return hrp end
                return nil
            end
            -- No Humanoid -> _playerDriveLocomotion (post bracket) no-ops (E1);
            -- this test is about the camera aim, not locomotion.
            function character:FindFirstChildOfClass(_c) return nil end
            game:GetService("Players").LocalPlayer.Character = character
        end

        {stale}

        -- Synthetic "Shoot" component: in its setup it ALIASES
        -- workspace.CurrentCamera to self.cam (EXACTLY the AI's Awake
        -- ``self.cam = workspace.CurrentCamera``), then mid-Update reads
        -- self.cam.CFrame -- the look its raycast origin/direction derive from.
        local observed = {{}}
        local Shoot = {{}}
        Shoot.__index = Shoot
        function Shoot:Update(_dt)
            -- self.cam was aliased to workspace.CurrentCamera at construction;
            -- this is the same object C drives -> the read sees C's pre-write.
            local aim = self.cam.CFrame
            observed.yaw = aim._yaw
            observed.pitch = aim._pitch
            observed.eyeY = aim.Position.Y
        end
        local shoot = setmetatable(
            {{cam = workspace.CurrentCamera}}, Shoot)
        engine._meta[shoot] = {{
            classTable = Shoot, scriptId = "shoot",
            activeInHierarchy = true, enabled = true,
        }}

        {pre_override}

        engine:_tick(0.016)

        -- The aim the mid-Update read captured (= what a raycast would use).
        print("AIM_YAW=" .. tostring(observed.yaw))
        print("AIM_PITCH=" .. tostring(observed.pitch))
        print("AIM_EYEY=" .. tostring(observed.eyeY))

        -- C's live look via the explicit host read surface (host.player) AND the
        -- raw camera object, evaluated AFTER the tick (post-write == pre-write,
        -- idempotent same-frame). The aim read above must equal BOTH.
        local host = engine:_makeHostSurface({{}})
        local look = host.player:getLookCFrame()
        print("LOOK_YAW=" .. tostring(look._yaw))
        print("LOOK_PITCH=" .. tostring(look._pitch))
        print("LOOK_EYEY=" .. tostring(look.Position.Y))

        local rawCam = workspace.CurrentCamera.CFrame
        print("RAW_YAW=" .. tostring(rawCam._yaw))

        -- The independently-computed expected look: _composeLook(eye, yaw, pitch)
        -- with eye = HRP.Position + (0, eyeHeight, 0). Proves the aim == the pure
        -- camera math over C's state, not merely "some non-stale value".
        local eye = Vector3.new(10, {_HRP_Y} + {_EYE_HEIGHT}, 30)
        local expected = SceneCameraInput._composeLook(
            eye, {_SEED_YAW}, {_SEED_PITCH})
        print("EXP_YAW=" .. tostring(expected._yaw))
        print("EXP_PITCH=" .. tostring(expected._pitch))
        print("EXP_EYEY=" .. tostring(expected.Position.Y))
    """


def _grab(out: str, prefix: str) -> str:
    for line in out.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    raise AssertionError(f"no line {prefix!r} in:\n{out}")


class TestAimReadIsCLiveLook:

    def test_mid_update_aim_read_equals_C_prewrite_look(self) -> None:
        # AC2: drive the REAL _tick. C's _playerPreTick (pre-Update) writes the
        # camera to the seeded look; the synthetic Shoot reads self.cam.CFrame
        # (aliased to workspace.CurrentCamera) mid-Update. Assert the aim read
        # equals C's live look on ALL THREE surfaces:
        #   (1) host.player:getLookCFrame()  (the explicit host read surface),
        #   (2) the raw workspace.CurrentCamera.CFrame, and
        #   (3) _composeLook(eye, yaw, pitch) computed independently,
        # for yaw, pitch, AND the character-bound eye Y. The aim IS C's live look.
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        rc, out, err = run_camera_scenario(preamble, _aim_read_body())
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"

        aim_yaw = _grab(out, "AIM_YAW=")
        aim_pitch = _grab(out, "AIM_PITCH=")
        aim_eyey = _grab(out, "AIM_EYEY=")

        # The mid-Update aim read saw C's PRE-write look (non-vacuous: the camera
        # was seeded STALE at yaw 0, so a non-zero seeded yaw here can ONLY come
        # from C's pre-write running before the Update pass).
        assert float(aim_yaw) == float(_SEED_YAW), (
            f"mid-Update aim yaw must be C's pre-write look ({_SEED_YAW}); "
            f"got {aim_yaw} (a stale 0 means the pre-write did not run)\n{out}"
        )
        assert float(aim_pitch) == float(_SEED_PITCH), (
            f"mid-Update aim pitch must be C's pre-write look ({_SEED_PITCH}); "
            f"got {aim_pitch}\n{out}"
        )
        # The eye Y is the character HRP eye (HRP.Position.Y + eyeHeight), proving
        # the aim follows the live BODY, not the degenerate camera-pos fallback.
        assert float(aim_eyey) == float(_HRP_Y) + float(_EYE_HEIGHT), (
            f"mid-Update aim eye Y must follow the character HRP "
            f"({float(_HRP_Y) + float(_EYE_HEIGHT)}); got {aim_eyey}\n{out}"
        )

        # The aim read == host.player:getLookCFrame() (the explicit host surface
        # B/Phase 4 teaches) == the raw camera == the pure _composeLook math.
        for surface, key in (("host getLookCFrame", "LOOK"), ("expected _composeLook", "EXP")):
            assert float(_grab(out, f"{key}_YAW=")) == float(aim_yaw), (
                f"{surface} yaw must equal the mid-Update aim yaw\n{out}"
            )
            assert float(_grab(out, f"{key}_PITCH=")) == float(aim_pitch), (
                f"{surface} pitch must equal the mid-Update aim pitch\n{out}"
            )
            assert float(_grab(out, f"{key}_EYEY=")) == float(aim_eyey), (
                f"{surface} eye Y must equal the mid-Update aim eye Y\n{out}"
            )
        assert float(_grab(out, "RAW_YAW=")) == float(aim_yaw), (
            f"the raw workspace.CurrentCamera.CFrame yaw (what self.cam aliases) "
            f"must equal the mid-Update aim yaw\n{out}"
        )

    def test_mutation_no_prewrite_leaves_aim_stale(self) -> None:
        # NON-VACUITY / mutation: override _playerPreTick to a NO-OP (the
        # pre-write does NOT run). The camera stays at the STALE seed (yaw 0), so
        # the mid-Update alias read sees 0 -- NOT C's seeded look ({_SEED_YAW}).
        # This is the RED the real pre-write turns GREEN: it proves the AC2
        # assertion above is load-bearing on _playerPreTick's pre-write, not a
        # tautology that would pass even if the pre-write never ran.
        pre_override = """
            -- Suppress C's pre-write for this frame: an empty _playerPreTick.
            function engine:_playerPreTick(_dt) end
        """
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        rc, out, err = run_camera_scenario(
            preamble, _aim_read_body(pre_override=pre_override))
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"

        aim_yaw = _grab(out, "AIM_YAW=")
        # Without the pre-write, the aim read is the stale 0 -- distinct from the
        # seeded look. (If this read were NON-stale here, the GREEN test would be
        # passing for the wrong reason -- a fixture-seeded camera rather than C.)
        assert float(aim_yaw) == 0.0, (
            f"with the pre-write suppressed, the mid-Update aim read must see the "
            f"STALE camera (yaw 0), proving the GREEN case depends on C's "
            f"pre-write; got {aim_yaw}\n{out}"
        )
        assert float(aim_yaw) != float(_SEED_YAW), (
            f"the mutation must NOT reproduce C's live look\n{out}"
        )
