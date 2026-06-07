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


# --------------------------------------------------------------------------- #
# Phase 1 slice 1.1 primitives.
# --------------------------------------------------------------------------- #


def test_same_frame_multi_read_drains_to_zero() -> None:
    """Primitive (a) / AC1: two same-frame reads of the mouse delta DRAIN — the
    first read returns the queued delta, the second returns (0, 0) (the real
    ``UserInputService:GetMouseDelta`` drains; a later phase must not assume a
    second same-frame read returns the same delta — design §2 E1/AC1)."""
    preamble = camera_input_preamble(mouse_deltas=[(3.0, -4.0)])
    body = """
        local cam = SceneCameraInput.acquire()
        local dx1, dy1 = cam:_readDelta()
        local dx2, dy2 = cam:_readDelta()
        print(string.format("R1=%.3f,%.3f", dx1, dy1))
        print(string.format("R2=%.3f,%.3f", dx2, dy2))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "R1=3.000,-4.000" in out, out
    assert "R2=0.000,0.000" in out, out


def test_e2e_channel_single_consumer_C_before_A_inband() -> None:
    """Primitive (d) / AC5: the ONE E2E mouse channel is consumed exactly once,
    happens-before-ordered, and ownership-attributed.

    Inject ``E2EMouseSeq=1, DeltaX=5, DeltaY=-7`` plus a NONZERO base
    ``mouse_deltas=[(2, 1)]`` so A's bare ``GetMouseDelta`` is distinguishable
    from the injected delta. Reader C (scenario luau, ``_currentActor="C"``)
    HAND-ROLLS the consume-once protocol (read ``E2EMouseSeq``; if
    ``> E2EMouseAckSeq`` then ack + read ``E2EMouseDeltaX``/``Y``) and feeds the
    result to the PURE ``SceneCameraInput._advance`` — C NEVER calls
    ``_readDelta`` (design §1.1(iii) / D9). THEN (``_currentActor="A"``) the real
    ``cam:step``/``_readDelta`` runs. Asserts:
      (d-i)  C's advance produced a NON-ZERO yaw/pitch (C consumed the injected
             delta);
      (d-ii) exactly ONE ``E2EMouseAckSeq``->1 transition, FIRST writer == "C"
             (C happens-before A);
      (d-iii) A's REAL in-band ``cam:step(dt)`` path (which internally calls
             ``_readDelta`` — the actual paradigm-A camera_facet emits
             ``_cam:step``, NOT a bare ``_readDelta``) advances by EXACTLY the
             raw base ``(2, 1)`` — ZERO injected (A saw the already-acked
             channel, proving the channel ran). The post-C-ack
             ``step``->``_readDelta`` branch is therefore actually exercised; a
             ``step`` regression that re-acks / re-reads the same seq would make
             A advance by base+injected and FAIL.
    """
    preamble = camera_input_preamble(mouse_deltas=[(2.0, 1.0)])
    body = """
        -- Seed the injected E2E channel under no particular actor.
        workspace:SetAttribute("E2EMouseDeltaX", 5.0)
        workspace:SetAttribute("E2EMouseDeltaY", -7.0)
        workspace:SetAttribute("E2EMouseSeq", 1)

        -- Reader C: hand-rolled consume-once against the ONE channel, tagged "C",
        -- then feed the pure _advance. C NEVER calls _readDelta.
        workspace._currentActor = "C"
        local cdx, cdy = 0.0, 0.0
        local seq = workspace:GetAttribute("E2EMouseSeq") or 0
        if seq > (workspace:GetAttribute("E2EMouseAckSeq") or 0) then
            workspace:SetAttribute("E2EMouseAckSeq", seq)
            cdx = cdx + (workspace:GetAttribute("E2EMouseDeltaX") or 0)
            cdy = cdy + (workspace:GetAttribute("E2EMouseDeltaY") or 0)
        end
        local cyaw, cpitch = SceneCameraInput._advance(
            0.0, 0.0, cdx, cdy, 0.01, -10.0, 10.0)
        print(string.format("CADV=%.3f,%.3f", cyaw, cpitch))

        -- Reader A: the REAL in-band path, tagged "A". Drive the REAL
        -- ``cam:step(dt)`` (the actual paradigm-A camera_facet path, which
        -- internally calls _readDelta) -- NOT a bare _readDelta -- so the
        -- post-C-ack step()->_readDelta branch is exercised. With a known
        -- sensitivity (0.01), step's advance turns the delta A sees into a
        -- yaw/pitch we can read off the composed look CFrame. A must see the
        -- channel already consumed -> ONLY the raw base (2, 1).
        workspace._currentActor = "A"
        local cam = SceneCameraInput.acquire()
        cam:configure({sensitivity = 0.01, minPitch = -10.0, maxPitch = 10.0})
        cam._yaw = 0.0
        cam._pitch = 0.0
        cam:step(0.016)
        local look = workspace.CurrentCamera.CFrame
        local apitch, ayaw = look:ToEulerAnglesYXZ()
        print(string.format("ALOOK=%.3f,%.3f", ayaw, apitch))

        -- Scan the ordered ack log for E2EMouseAckSeq transitions to seq 1.
        local ackCount = 0
        local firstActor = "NONE"
        for _, w in ipairs(workspace._attrWrites) do
            if w.name == "E2EMouseAckSeq" and w.value == 1 then
                ackCount = ackCount + 1
                if firstActor == "NONE" then firstActor = w.actor end
            end
        end
        print(string.format("ACKCOUNT=%d", ackCount))
        print("FIRSTACTOR=" .. tostring(firstActor))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # (d-i) C consumed the injected delta -> non-zero yaw/pitch.
    # yaw = 0 - 5*0.01 = -0.05; pitch = clamp(0 - (-7)*0.01) = 0.07.
    assert "CADV=-0.050,0.070" in out, out
    assert "CADV=0.000,0.000" not in out, out
    # (d-iii) A's REAL step()->_readDelta advances by ONLY the raw base (2, 1) --
    # ZERO injected. With sens 0.01: yaw = 0 - 2*0.01 = -0.02; pitch =
    # clamp(0 - 1*0.01) = -0.01. Had step re-acked/re-read the same seq, A would
    # have seen base+injected (7, -6) -> ALOOK=-0.070,0.060 -> FAIL.
    assert "ALOOK=-0.020,-0.010" in out, out
    assert "ALOOK=-0.070,0.060" not in out, out
    # (d-ii) exactly one ack transition, FIRST writer is C (happens-before).
    assert "ACKCOUNT=1" in out, out
    assert "FIRSTACTOR=C" in out, out
