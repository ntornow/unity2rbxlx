"""Slice 3.3 — per-player lifecycle resync on ``CharacterAdded`` (D7).

Respawn ITSELF is server-owned (autogen GameServer ``CharacterAdded`` spawn +
engine ``SpawnLocation``). C's job narrows to the CLIENT re-acquire: on a new
character, reseed ``p._yaw`` from the new character's facing, zero ``p._pitch``,
and re-assert the camera so it follows the NEW HRP — and do the yaw/camera part
IMMEDIATELY, BEFORE the existing ``task.wait()`` yield, so there is no stale-yaw
frame (D-P3-resync-ordering / P1-b). Only controls-off + body-hide stay after the
yield (they legitimately need PlayerScripts/descendants to populate).

These tests EXECUTE the real ``_playerBoot`` CharacterAdded handler +
``_playerResyncToCharacter`` under the camera harness. The yaw helper is the REAL
exported ``SceneCameraInput._yawOf`` injected as ``services.cameraYawOf`` (D-P3-
resync-helper). Per D-P3-resync-test-character-alias, the tests set
``LocalPlayer.Character = char`` BEFORE firing the harness ``CharacterAdded``
signal, because ``_playerWriteCamera`` follows ``_playerCharacterHRP()`` (which
re-resolves ``LocalPlayer.Character``), NOT the callback's ``char`` arg.

Acceptance: AC3a, AC3a-immediate, AC3a-boot, AC3b, AC4, AC3-boot-green (the last
lives in ``test_player_authority.py``), AC3-services-pin (in
``test_autogen_player_services.py``).
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from tests._camera_input_harness import (
    CAMERA_INPUT_PATH,
    camera_input_preamble,
    run_camera_scenario,
)
from tests.test_player_authority import _build_authority_runtime

sys.path.insert(0, str(Path(__file__).parent.parent))

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


# A known non-zero respawn facing the new HRP carries (radians). cameraYawOf
# (== SceneCameraInput._yawOf) extracts it via the harness CFrame's
# ToEulerAnglesYXZ Y component.
RESPAWN_YAW = 1.2345

# A SECOND, distinct respawn facing for the superseding character (char B) in the
# fast-respawn race test — must differ from RESPAWN_YAW so a stale char-A reseed
# is detectable.
SUPERSEDE_YAW = 1.1


# ---------------------------------------------------------------------------
# Lifecycle provisioning: a fireable LocalPlayer.CharacterAdded + a recording
# ``services.task.wait`` (records the yaw/camera state AT the yield point, for
# the immediate-before-yield proof) + a new character whose HRP faces
# ``RESPAWN_YAW``. Appended AFTER ``_build_authority_runtime`` (which builds the
# engine + a fresh self._player + injects services.cameraYawOf).
# ---------------------------------------------------------------------------

def _lifecycle_setup(*, hide_avatar: bool = False) -> str:
    avatar_descendants = ""
    if hide_avatar:
        # A provisioned avatar part (BasePart) records its
        # LocalTransparencyModifier write so AC3b proves body-hide runs on a
        # REAL part AFTER the yield (not a no-op'd mock — D-P3-AC3b-verify).
        avatar_descendants = """
            local function mkPart()
                local part = {}
                function part:IsA(c) return c == "BasePart" end
                setmetatable(part, {
                    __newindex = function(t, k, v)
                        if k == "LocalTransparencyModifier" then
                            _resyncRecord.hidden = v
                        end
                        rawset(t, k, v)
                    end,
                })
                return part
            end
            _resyncAvatarPart = mkPart()
"""
    descendants_field = (
        "GetDescendants = function() return {_resyncAvatarPart} end,"
        if hide_avatar
        else "GetDescendants = function() return {} end,"
    )
    return f"""
        _resyncRecord = {{}}
{avatar_descendants}
        -- The new character whose HRP faces RESPAWN_YAW. The HRP CFrame carries
        -- BOTH a Position (eye-follow) and the yaw (cameraYawOf reads it).
        local newHrp = {{
            CFrame = CFrame.new(Vector3.new(20, 0, 30))
                * CFrame.Angles(0, {RESPAWN_YAW}, 0),
            Position = Vector3.new(20, 0, 30),
        }}
        _resyncNewHrp = newHrp
        local newChar = {{
            FindFirstChild = function(_, name)
                if name == "HumanoidRootPart" then return newHrp end
                return nil
            end,
            {descendants_field}
            DescendantAdded = {{Connect = function() return {{Disconnect = function() end}} end}},
        }}
        _resyncNewChar = newChar

        -- A fireable CharacterAdded on the harness LocalPlayer (the default mock
        -- stub never fires). ``Fire(char)`` invokes every connected handler.
        local lp = game:GetService("Players").LocalPlayer
        do
            local handlers = {{}}
            lp.CharacterAdded = {{
                Connect = function(_, fn)
                    handlers[#handlers + 1] = fn
                    return {{Disconnect = function() end}}
                end,
                Fire = function(_, char)
                    for _, fn in ipairs(handlers) do fn(char) end
                end,
            }}
        end

        -- A recording task.wait: at the yield point it snapshots p._yaw + the
        -- camera CFrame yaw, so AC3a-immediate can prove the resync ran STRICTLY
        -- before the yield (no stale-yaw frame).
        engine._services.task = {{
            wait = function()
                _resyncRecord.yawAtYield = engine._player._yaw
                local cam = workspace.CurrentCamera
                local _, camYaw = cam.CFrame:ToEulerAnglesYXZ()
                _resyncRecord.camYawAtYield = camYaw
                _resyncRecord.yielded = true
                return 0
            end,
        }}
    """


# ---------------------------------------------------------------------------
# AC3a — CharacterAdded re-acquires the new character: yaw resyncs, pitch=0,
# camera follows the NEW HRP eye.
# ---------------------------------------------------------------------------

def test_character_added_resyncs_yaw_pitch_and_camera() -> None:
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + f"""
        -- Stale pre-death facing/pitch the resync must overwrite.
        p._yaw = -9.0
        p._pitch = 0.77

        engine:_playerBoot()    -- installs the CharacterAdded connection.

        -- D-P3-resync-test-character-alias: point LocalPlayer.Character at the
        -- new char BEFORE firing, so _playerWriteCamera (-> _playerCharacterHRP)
        -- follows the NEW HRP.
        local lp = game:GetService("Players").LocalPlayer
        lp.Character = _resyncNewChar
        lp.CharacterAdded:Fire(_resyncNewChar)

        print(string.format("YAW=%.6f", p._yaw))
        print(string.format("PITCH=%.6f", p._pitch))
        local cam = workspace.CurrentCamera
        local pos = cam.CFrame.Position
        print(string.format("EYE=%.1f,%.1f,%.1f", pos.X, pos.Y, pos.Z))
        local _, camYaw = cam.CFrame:ToEulerAnglesYXZ()
        print(string.format("CAMYAW=%.6f", camYaw))
        print("YIELDED=" .. tostring(_resyncRecord.yielded))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # Yaw reseeded from the new HRP facing (== cameraYawOf(newHRP.CFrame)).
    assert f"YAW={RESPAWN_YAW:.6f}" in out, out
    assert "PITCH=0.000000" in out, out
    # Camera eye = new HRP position + eyeHeight (1.5), proving it follows the
    # NEW HRP, not the stale one.
    assert "EYE=20.0,1.5,30.0" in out, out
    assert f"CAMYAW={RESPAWN_YAW:.6f}" in out, out
    # The handler ran its full body (the yield happened after the resync).
    assert "YIELDED=true" in out, out


# ---------------------------------------------------------------------------
# AC3a-immediate (P1-b) — the yaw reseed + camera re-assert are observable the
# SAME frame, STRICTLY BEFORE the task.wait yield (no stale-yaw frame).
# ---------------------------------------------------------------------------

def test_resync_runs_before_yield_no_stale_yaw_frame() -> None:
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + f"""
        p._yaw = -9.0
        p._pitch = 0.5

        engine:_playerBoot()
        local lp = game:GetService("Players").LocalPlayer
        lp.Character = _resyncNewChar
        lp.CharacterAdded:Fire(_resyncNewChar)

        -- The recorder snapshots state AT the yield point. If the resync ran
        -- before the yield (the fix), these already equal the new facing; if it
        -- ran AFTER (the pre-fix ordering) they'd still be the stale -9.0.
        print(string.format("YAW_AT_YIELD=%.6f", _resyncRecord.yawAtYield))
        print(string.format("CAMYAW_AT_YIELD=%.6f", _resyncRecord.camYawAtYield))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # Non-vacuous: with the resync placed AFTER the yield these would be -9.0.
    assert f"YAW_AT_YIELD={RESPAWN_YAW:.6f}" in out, out
    assert f"CAMYAW_AT_YIELD={RESPAWN_YAW:.6f}" in out, out


def test_resync_before_yield_is_load_bearing_static_guard() -> None:
    """Mutation-proof the ordering at the SOURCE: in ``_playerBoot``'s
    CharacterAdded handler the ``_playerResyncToCharacter`` call must precede the
    ``task.wait()`` yield (else respawn renders one frame at the stale facing).
    Pairs with the dynamic yield-point proof above."""
    src = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    boot = src[src.index("function SceneRuntime:_playerBoot()") :]
    boot = boot[: boot.index("\nfunction SceneRuntime:")]
    # Scope to the CharacterAdded handler body.
    handler = boot[boot.index("CharacterAdded:Connect(function(char)") :]
    resync_at = handler.index("_playerResyncToCharacter(char)")
    wait_at = handler.index("task.wait()")
    assert resync_at < wait_at, (
        "the immediate yaw/camera resync must run BEFORE the task.wait yield "
        "(D-P3-resync-ordering / no stale-yaw frame)"
    )


# ---------------------------------------------------------------------------
# AC3a-boot (P2-a) — facing correct from frame 0: if lp.Character already EXISTS
# at boot, _playerBoot runs the same immediate resync once (no CharacterAdded
# event needed).
# ---------------------------------------------------------------------------

def test_boot_with_existing_character_seeds_facing() -> None:
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + """
        -- _yaw starts at the init 0; a character is ALREADY present at boot.
        local lp = game:GetService("Players").LocalPlayer
        lp.Character = _resyncNewChar

        engine:_playerBoot()

        print(string.format("YAW=%.6f", p._yaw))
        print(string.format("PITCH=%.6f", p._pitch))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # Non-vacuous: without the boot-if-present reseed, p._yaw would stay 0.0.
    assert f"YAW={RESPAWN_YAW:.6f}" in out, out
    assert "PITCH=0.000000" in out, out


# ---------------------------------------------------------------------------
# AC3b — the DELAYED body-hide runs AFTER the yield on the new character (a REAL
# provisioned avatar part, not a no-op'd mock). Static guard for the controls
# path (which standalone luau cannot execute — require() unsupported).
# ---------------------------------------------------------------------------

def test_delayed_body_hide_runs_after_yield_on_new_character() -> None:
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup(hide_avatar=True)
        + """
        engine:_playerBoot()
        local lp = game:GetService("Players").LocalPlayer
        lp.Character = _resyncNewChar
        lp.CharacterAdded:Fire(_resyncNewChar)

        print("YIELDED=" .. tostring(_resyncRecord.yielded))
        print("HIDDEN=" .. tostring(_resyncRecord.hidden))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "YIELDED=true" in out, out
    # Body-hide ran on a REAL provisioned avatar part AFTER the yield.
    assert "HIDDEN=1" in out, out


def test_post_yield_body_calls_apply_controls_static_guard() -> None:
    """AC3b (static, D-P3-AC3b-verify) — the post-yield handler body re-applies
    controls-off. ``controls:Disable()`` can't EXECUTE under standalone luau
    (require unsupported), so pin the source shape: after the ``task.wait()``
    yield the CharacterAdded handler calls ``applyControls()`` (and the
    ``applyControls`` closure reaches ``GetControls():Disable()``)."""
    src = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    boot = src[src.index("function SceneRuntime:_playerBoot()") :]
    boot = boot[: boot.index("\nfunction SceneRuntime:")]
    handler = boot[boot.index("CharacterAdded:Connect(function(char)") :]
    wait_at = handler.index("task.wait()")
    post_yield = handler[wait_at:]
    assert "applyControls()" in post_yield, (
        "the post-yield handler body must re-apply controls-off"
    )
    assert "hideChar(char)" in post_yield, (
        "the post-yield handler body must hide the new character's avatar"
    )
    # applyControls reaches the real default-controls disable.
    assert ":GetControls()" in boot and "controls:Disable()" in boot, boot


# ---------------------------------------------------------------------------
# AC4 — respawn resync has NO dependency on the AI's PivotTo / paradigm B: with
# the AI doing NOTHING, a CharacterAdded still resyncs the camera. PLUS a
# build-time assertion that respawn itself is server-owned (autogen.py).
# ---------------------------------------------------------------------------

def test_resync_independent_of_ai_update() -> None:
    """The resync drives camera/yaw purely from the new HRP + the injected pure
    helper — no AI ``PivotTo``, no paradigm B. Firing CharacterAdded with NO
    component Update having run still follows the new HRP."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + f"""
        p._yaw = -3.0

        engine:_playerBoot()
        local lp = game:GetService("Players").LocalPlayer
        lp.Character = _resyncNewChar
        -- No component Update / no PivotTo runs; fire the respawn directly.
        lp.CharacterAdded:Fire(_resyncNewChar)

        print(string.format("YAW=%.6f", p._yaw))
        local cam = workspace.CurrentCamera
        local pos = cam.CFrame.Position
        print(string.format("EYE=%.1f,%.1f,%.1f", pos.X, pos.Y, pos.Z))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert f"YAW={RESPAWN_YAW:.6f}" in out, out
    assert "EYE=20.0,1.5,30.0" in out, out


def test_respawn_is_server_owned_in_autogen() -> None:
    """AC4 (build-time) — respawn is SERVER-OWNED, not in the runtime authority:
    the autogen GameServer connects ``player.CharacterAdded`` and spawn-teleports
    the new character's HRP, UNCONDITIONALLY (no dependency on the AI shape or on
    the client lifecycle resync)."""
    from converter.autogen import generate_game_server_script  # noqa: PLC0415

    server_src = generate_game_server_script().source
    assert "player.CharacterAdded:Connect" in server_src, server_src
    # The spawn-teleport sets the new character's HRP CFrame (server-owned move).
    assert "hrp.CFrame = spawnCFrame" in server_src, server_src


def test_resync_noops_without_player() -> None:
    """``_playerResyncToCharacter`` no-ops when ``self._player == nil`` (the
    server / no-player key — the resync never runs server-side, E6)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + """
        engine._player = nil
        local ok = pcall(function()
            engine:_playerResyncToCharacter(_resyncNewChar)
        end)
        print("NILOK=" .. tostring(ok))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "NILOK=true" in out, out


def test_resync_character_without_hrp_zeros_pitch_only() -> None:
    """E9 — a character with no HRP yet leaves ``p._yaw`` unchanged but still
    zeroes pitch + re-asserts the camera (no crash)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + """
        p._yaw = 2.5
        p._pitch = 0.6
        local noHrpChar = {FindFirstChild = function(_, _name) return nil end}
        local ok = pcall(function()
            engine:_playerResyncToCharacter(noHrpChar)
        end)
        print("OK=" .. tostring(ok))
        print(string.format("YAW=%.6f", p._yaw))
        print(string.format("PITCH=%.6f", p._pitch))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "OK=true" in out, out
    # Yaw unchanged (no HRP to read), pitch zeroed.
    m = re.search(r"YAW=([\-\d.]+)", out)
    assert m and float(m.group(1)) == pytest.approx(2.5, abs=1e-4), out
    assert "PITCH=0.000000" in out, out


# ---------------------------------------------------------------------------
# D-P3-late-hrp — late-HRP fallback: when a character's HumanoidRootPart has not
# replicated at the immediate resync, the yaw reseed is skipped and (since
# ``_playerBoot`` is run-once) would stay STALE for that whole life. The immediate
# resync now returns false on a missing HRP, and ``_playerBoot`` arms a one-shot
# ``char.DescendantAdded`` retry that reseeds the yaw the moment the HRP appears.
# ---------------------------------------------------------------------------

# A character whose HRP is ABSENT at resync time and arrives LATER via a fireable
# ``DescendantAdded``. ``Fire(desc)`` invokes every connected handler;
# ``FindFirstChild("HumanoidRootPart")`` returns nil until the HRP is published
# (matching the real replication-lag shape the bug fires on).
def _late_hrp_setup() -> str:
    return f"""
        _lateRecord = {{}}
        -- The HRP that will arrive LATE; faces RESPAWN_YAW. Name drives the
        -- retry's HumanoidRootPart match.
        local lateHrp = {{
            Name = "HumanoidRootPart",
            CFrame = CFrame.new(Vector3.new(20, 0, 30))
                * CFrame.Angles(0, {RESPAWN_YAW}, 0),
            Position = Vector3.new(20, 0, 30),
            -- A real descendant is an Instance: hideChar (also connected to this
            -- DescendantAdded by _playerBoot) calls :IsA on every descendant.
            IsA = function(_, c) return c == "BasePart" end,
        }}
        _lateHrp = lateHrp

        -- The character: NO HRP at first (FindFirstChild returns nil), with a
        -- fireable DescendantAdded the retry connects to.
        local lateChar
        do
            local handlers = {{}}
            local hrpPublished = false
            lateChar = {{
                FindFirstChild = function(_, name)
                    if name == "HumanoidRootPart" and hrpPublished then
                        return lateHrp
                    end
                    return nil
                end,
                GetDescendants = function() return {{}} end,
                DescendantAdded = {{
                    Connect = function(_, fn)
                        handlers[#handlers + 1] = fn
                        return {{Disconnect = function()
                            _lateRecord.disconnects = (_lateRecord.disconnects or 0) + 1
                        end}}
                    end,
                    -- Publish the HRP, then fire the signal (real ordering: the
                    -- descendant exists before DescendantAdded reports it).
                    Fire = function(_, desc)
                        if desc == lateHrp then hrpPublished = true end
                        for _, fn in ipairs(handlers) do fn(desc) end
                    end,
                }},
            }}
        end
        _lateChar = lateChar
    """


def test_character_added_late_hrp_reseeds_yaw_when_hrp_arrives() -> None:
    """(a) CharacterAdded with the HRP ABSENT, then arriving late: the yaw stays
    unchanged at the immediate resync (HRP nil) but reseeds to RESPAWN_YAW the
    moment the late HRP fires DescendantAdded. RED without the one-shot retry."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + _late_hrp_setup()
        + f"""
        p._yaw = -9.0
        p._pitch = 0.77

        engine:_playerBoot()    -- installs the CharacterAdded connection.

        local lp = game:GetService("Players").LocalPlayer
        lp.Character = _lateChar
        lp.CharacterAdded:Fire(_lateChar)

        -- HRP absent at the immediate resync: yaw NOT reseeded yet (still -9.0),
        -- pitch zeroed.
        print(string.format("YAW_IMMEDIATE=%.6f", p._yaw))
        print(string.format("PITCH_IMMEDIATE=%.6f", p._pitch))

        -- The HRP replicates: fire DescendantAdded. The one-shot retry reseeds.
        _lateChar.DescendantAdded:Fire(_lateHrp)
        print(string.format("YAW_AFTER=%.6f", p._yaw))
        print("DISCONNECTS=" .. tostring(_lateRecord.disconnects or 0))

        -- The retry is ONE-SHOT: a second DescendantAdded (a NON-HRP descendant)
        -- must NOT re-reseed (the connection is gone). Set a sentinel yaw and
        -- confirm it survives.
        p._yaw = 0.5
        _lateChar.DescendantAdded:Fire({{
            Name = "SomethingElse",
            IsA = function(_, _c) return false end,
        }})
        print(string.format("YAW_AFTER_DISCONNECT=%.6f", p._yaw))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # Immediate resync: HRP nil → yaw untouched, pitch zeroed (E9 shape).
    assert "YAW_IMMEDIATE=-9.000000" in out, out
    assert "PITCH_IMMEDIATE=0.000000" in out, out
    # Late HRP arrival reseeds the yaw (RED without the retry: stays -9.0).
    assert f"YAW_AFTER={RESPAWN_YAW:.6f}" in out, out
    # The one-shot retry disconnected exactly once.
    assert "DISCONNECTS=1" in out, out
    # And does NOT fire again after disconnect (sentinel 0.5 survives).
    assert "YAW_AFTER_DISCONNECT=0.500000" in out, out


def test_boot_with_existing_character_late_hrp_reseeds_yaw() -> None:
    """(b) boot-with-existing-character whose HRP has not replicated at boot: the
    boot reseed leaves yaw at the init value (HRP nil) but the one-shot retry
    corrects it when the HRP arrives. RED without the boot-path retry arming."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + _late_hrp_setup()
        + f"""
        -- _yaw starts at the init 0; the character is ALREADY present at boot but
        -- its HRP has not replicated.
        p._yaw = -4.0
        local lp = game:GetService("Players").LocalPlayer
        lp.Character = _lateChar

        engine:_playerBoot()

        -- Boot reseed: HRP nil → yaw untouched (still -4.0), pitch zeroed.
        print(string.format("YAW_BOOT=%.6f", p._yaw))
        print(string.format("PITCH_BOOT=%.6f", p._pitch))

        -- HRP replicates: the boot-armed one-shot retry reseeds.
        _lateChar.DescendantAdded:Fire(_lateHrp)
        print(string.format("YAW_AFTER=%.6f", p._yaw))
        print("DISCONNECTS=" .. tostring(_lateRecord.disconnects or 0))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # Boot reseed: HRP nil → yaw untouched, pitch zeroed.
    assert "YAW_BOOT=-4.000000" in out, out
    assert "PITCH_BOOT=0.000000" in out, out
    # Late HRP arrival reseeds the yaw (RED without the boot-path retry arming).
    assert f"YAW_AFTER={RESPAWN_YAW:.6f}" in out, out
    assert "DISCONNECTS=1" in out, out


def test_late_hrp_retry_noops_when_player_cleared() -> None:
    """The one-shot retry no-ops if ``self._player`` was cleared before the HRP
    arrives (the player left mid-replication): no crash, no reseed attempt.

    Mutation-proving: prove a retry was ACTUALLY armed (``_lateHRPConn`` is live
    before the player clears) AND that firing the late HRP after the clear takes
    the no-player early-return path — it does NOT crash and does NOT reseed a
    sentinel yaw written just before the fire. A version with no retry armed (or
    one that reseeds despite the cleared authority) fails these assertions."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + _late_hrp_setup()
        + """
        engine:_playerBoot()
        local lp = game:GetService("Players").LocalPlayer
        lp.Character = _lateChar
        lp.CharacterAdded:Fire(_lateChar)

        -- A retry WAS armed (HRP absent at the immediate resync): the connection
        -- is tracked live on the player authority.
        print("ARMED=" .. tostring(engine._player._lateHRPConn ~= nil))

        -- Sentinel yaw the cleared-authority retry must NOT overwrite.
        engine._player._yaw = 0.5
        -- Authority gone before the HRP replicates.
        local stalePlayer = engine._player
        engine._player = nil
        local ok = pcall(function()
            _lateChar.DescendantAdded:Fire(_lateHrp)
        end)
        print("OK=" .. tostring(ok))
        -- The captured (now-detached) player's yaw was NOT reseeded — the retry
        -- saw self._player == nil and bailed before the cameraYawOf reseed.
        print(string.format("STALE_YAW=%.6f", stalePlayer._yaw))
        -- One-shot still disconnected (the HRP-name match disconnects before the
        -- no-player early return).
        print("DISCONNECTS=" .. tostring(_lateRecord.disconnects or 0))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "ARMED=true" in out, out
    assert "OK=true" in out, out
    # The cleared-authority retry bailed before the reseed: sentinel survives.
    assert "STALE_YAW=0.500000" in out, out
    assert "DISCONNECTS=1" in out, out


# ---------------------------------------------------------------------------
# Harden P1 — fast-respawn lifecycle race: a late-HRP retry armed for char A must
# NOT stomp a SUPERSEDING char B's yaw. char A spawns WITHOUT an HRP (arms a
# retry) → char B respawns WITH an HRP at SUPERSEDE_YAW and becomes the current
# LocalPlayer.Character → char A's HRP finally arrives LATE → the stale char-A
# retry must NOT overwrite char B's yaw. RED against 9aeee38 (the retry is not
# tied to the current character and is never cancelled on re-arm).
# ---------------------------------------------------------------------------

def test_stale_previous_character_late_hrp_does_not_stomp_superseding_yaw() -> None:
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _lifecycle_setup()
        + _late_hrp_setup()
        + f"""
        -- char B: a SUPERSEDING character that respawns WITH its HRP present,
        -- facing SUPERSEDE_YAW. Its own DescendantAdded is an inert stub (no late
        -- HRP needed — the HRP is already there).
        local charBHrp = {{
            Name = "HumanoidRootPart",
            CFrame = CFrame.new(Vector3.new(5, 0, 5))
                * CFrame.Angles(0, {SUPERSEDE_YAW}, 0),
            Position = Vector3.new(5, 0, 5),
            IsA = function(_, c) return c == "BasePart" end,
        }}
        local charB = {{
            FindFirstChild = function(_, name)
                if name == "HumanoidRootPart" then return charBHrp end
                return nil
            end,
            GetDescendants = function() return {{}} end,
            DescendantAdded = {{Connect = function() return {{Disconnect = function() end}} end}},
        }}

        p._yaw = -9.0
        engine:_playerBoot()
        local lp = game:GetService("Players").LocalPlayer

        -- char A spawns WITHOUT an HRP → immediate resync skips the yaw reseed and
        -- arms a one-shot late-HRP retry for char A.
        lp.Character = _lateChar
        lp.CharacterAdded:Fire(_lateChar)
        print("ARMED_A=" .. tostring(engine._player._lateHRPConn ~= nil))

        -- char B respawns WITH its HRP → it becomes the current character and the
        -- immediate resync reseeds the yaw to SUPERSEDE_YAW. The CharacterAdded
        -- handler also CANCELS char A's pending retry (re-arm cancellation).
        lp.Character = charB
        lp.CharacterAdded:Fire(charB)
        print(string.format("YAW_AFTER_B=%.6f", engine._player._yaw))

        -- char A's HRP FINALLY arrives, late. The stale char-A retry must NOT
        -- reseed (char A is no longer LocalPlayer.Character) — yaw STAYS char B's.
        _lateChar.DescendantAdded:Fire(_lateHrp)
        print(string.format("YAW_FINAL=%.6f", engine._player._yaw))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # char A armed a retry (HRP absent at its immediate resync).
    assert "ARMED_A=true" in out, out
    # char B's immediate resync set the yaw to SUPERSEDE_YAW.
    assert f"YAW_AFTER_B={SUPERSEDE_YAW:.6f}" in out, out
    # The decisive assertion (RED against 9aeee38): char A's late HRP did NOT
    # stomp char B's yaw — it stays SUPERSEDE_YAW, NOT RESPAWN_YAW.
    assert f"YAW_FINAL={SUPERSEDE_YAW:.6f}" in out, out
    assert f"YAW_FINAL={RESPAWN_YAW:.6f}" not in out, out
