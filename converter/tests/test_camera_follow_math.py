"""GREEN deterministic units pinning the camera follow-MATH (Phase 1, Layer 1).

These exercise the REAL ``runtime/scene_camera_input.luau`` math on the small,
faithfully-mockable CFrame/Vector3/UserInputService surface via the kept
camera-input rig (``_camera_input_harness``). They never boot a full client
``Player.luau`` and never assert a runtime camera *write* authority (absent at
baseRef — design §2.1 NOTE / DP1): they pin ``_readDelta`` drain, the E2E mouse
channel add-once/ack, ``_composeLook`` (world-yaw ∘ local-pitch + pitch clamp
via ``_advance``), and ``step``'s eye = position + eyeHeight.

Skips cleanly when ``luau`` is absent (the repo idiom — design edge case E6).
"""

from __future__ import annotations

import shutil

import pytest

from tests._camera_input_harness import (
    CAMERA_INPUT_PATH,
    camera_input_preamble,
    run_camera_scenario,
)


def _luau_available() -> bool:
    return shutil.which("luau") is not None


pytestmark = pytest.mark.skipif(
    not _luau_available() or not CAMERA_INPUT_PATH.exists(),
    reason="needs standalone luau interpreter + scene_camera_input.luau",
)


def test_read_delta_drains_queued_mouse_delta() -> None:
    """``_readDelta`` pops successive ``GetMouseDelta`` entries, then reads
    ~(0, 0) once the queue is exhausted (drain semantics — design §2.1 (b))."""
    preamble = camera_input_preamble(mouse_deltas=[(3.0, -4.0), (1.0, 2.0)])
    body = """
        local cam = SceneCameraInput.acquire()
        local dx1, dy1 = cam:_readDelta()
        local dx2, dy2 = cam:_readDelta()
        local dx3, dy3 = cam:_readDelta()
        print(string.format("D1=%.3f,%.3f", dx1, dy1))
        print(string.format("D2=%.3f,%.3f", dx2, dy2))
        print(string.format("D3=%.3f,%.3f", dx3, dy3))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "D1=3.000,-4.000" in out, out
    assert "D2=1.000,2.000" in out, out
    assert "D3=0.000,0.000" in out, out


def test_e2e_mouse_channel_additive_one_frame() -> None:
    """Driving ``E2EMouseSeq`` / ``E2EMouseDeltaX/Y`` on ``workspace`` adds one
    frame of delta to ``_readDelta``, then acks the seq so the SAME seq is a
    no-op next frame (the channel contract — docs/E2E_INPUT_CHANNEL.md)."""
    preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0), (0.0, 0.0)])
    body = """
        local cam = SceneCameraInput.acquire()
        workspace:SetAttribute("E2EMouseDeltaX", 5.0)
        workspace:SetAttribute("E2EMouseDeltaY", -7.0)
        workspace:SetAttribute("E2EMouseSeq", 1)
        local dx1, dy1 = cam:_readDelta()
        local ack = workspace:GetAttribute("E2EMouseAckSeq")
        -- Same seq again: already acked => additive term suppressed.
        local dx2, dy2 = cam:_readDelta()
        print(string.format("INJ=%.3f,%.3f", dx1, dy1))
        print(string.format("ACK=%d", ack))
        print(string.format("NOOP=%.3f,%.3f", dx2, dy2))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "INJ=5.000,-7.000" in out, out
    assert "ACK=1" in out, out
    assert "NOOP=0.000,0.000" in out, out


def test_compose_look_yaw_then_pitch_order() -> None:
    """``_composeLook(eyePos, yaw, pitch)`` composes world-yaw ∘ local-pitch in
    the documented order; the mock CFrame surfaces the composed yaw/pitch via
    ``ToEulerAnglesYXZ`` (design §2.1 (b))."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = """
        local eye = Vector3.new(0, 0, 0)
        local cf = SceneCameraInput._composeLook(eye, 0.5, -0.25)
        local pitch, yaw = cf:ToEulerAnglesYXZ()
        print(string.format("YAW=%.3f", yaw))
        print(string.format("PITCH=%.3f", pitch))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "YAW=0.500" in out, out
    assert "PITCH=-0.250" in out, out


def test_advance_accumulates_and_clamps_pitch() -> None:
    """``_advance`` integrates a raw mouse delta into yaw/pitch and clamps pitch
    to the production bound (no over-the-top flip — design §2.1 (b))."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = """
        -- sensitivity 0.01; minPitch/maxPitch +/- 1.0 rad. A huge dy (+10000)
        -- would drive pitch far past maxPitch (dy is subtracted -> pitch down to
        -- minPitch); assert it clamps to -1.0, and yaw integrates dx unbounded.
        local yaw, pitch = SceneCameraInput._advance(
            0.0, 0.0, 50.0, 10000.0, 0.01, -1.0, 1.0)
        print(string.format("YAW=%.3f", yaw))
        print(string.format("PITCH=%.3f", pitch))
        -- A second advance the other way reaches +1.0 (clamps at max too).
        local _, pitch2 = SceneCameraInput._advance(
            0.0, 0.0, 0.0, -10000.0, 0.01, -1.0, 1.0)
        print(string.format("PITCH2=%.3f", pitch2))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # yaw = 0 - 50*0.01 = -0.5 (unbounded); pitch clamps to the -1.0 floor.
    assert "YAW=-0.500" in out, out
    assert "PITCH=-1.000" in out, out
    assert "PITCH2=1.000" in out, out


def test_step_eye_tracks_position_input() -> None:
    """``step(dt)`` places the eye at ``rigPivot.Position + eyeHeight`` for the
    seeded rig, with no mouse delta (the HRP-eye math in isolation; this does NOT
    assert a runtime camera-write authority, absent at baseRef — design §2.1
    NOTE / acceptance criterion 1)."""
    preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
    body = """
        local cam = SceneCameraInput.acquire()
        -- Seed a rig at a known position so the eye = pivot.Position + eyeHeight.
        local pivot = CFrame.new(Vector3.new(7, 11, -3))
        local rig = {}
        function rig:GetPivot() return pivot end
        cam:configure({rig = rig, eyeHeight = 1.5})
        cam:step(0.016)
        local eye = workspace.CurrentCamera.CFrame.Position
        print(string.format("EYE=%.3f,%.3f,%.3f", eye.X, eye.Y, eye.Z))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # eyeHeight 1.5 added to Y; X/Z track the pivot exactly.
    assert "EYE=7.000,12.500,-3.000" in out, out
