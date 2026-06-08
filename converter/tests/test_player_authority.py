"""Slice 2.1 — unit tests for the host-owned player-embodiment authority methods
on ``SceneRuntime`` (``_playerBoot`` / ``_playerReadInput`` / ``_playerWriteCamera``
+ the public ``getLookCFrame``).

These construct a ``self._player`` state table DIRECTLY on a freshly-built
``SceneRuntime`` and call the methods — they do NOT exercise ``_tick`` or
``_initPlayerAuthority`` (Slice 2.3 owns those). The two pure camera helpers are
injected by loading the REAL ``scene_camera_input.luau`` and passing
``SceneCameraInput._advance`` / ``._composeLook`` through the runtime's
``services`` table (reuse-not-rebuild, D9 — the authority NEVER calls
``SceneCameraInput:step`` / ``:_readDelta``). ``userInputService`` / ``players``
are the camera-harness mocks; ``workspace`` / ``Enum`` / ``CFrame`` / ``Vector3``
are the camera-harness ambient globals (matching how production ``scene_runtime``
reads the ambient ``workspace`` singleton).

Acceptance (Slice 2.1):
  * AC3  — ``_playerReadInput`` is the SINGLE per-frame E2E-channel consumer:
           after one call ``E2EMouseAckSeq == E2EMouseSeq`` and ``_yaw`` advanced
           by the injected delta; a second same-frame reader adds 0. Asserts the
           runtime never references ``:step`` / ``:_readDelta`` (D9 / §3 guardrail:
           no AI-output substring matcher either).
  * AC8  — ``_playerBoot`` sets ``CameraType=Scriptable`` / ``MouseBehavior=
           LockCenter`` / ``MouseIconEnabled=false`` and is idempotent.
  * AC10 — ``_playerWriteCamera`` / no-character tolerance: with no
           ``LocalPlayer.Character`` the camera still composes from accumulated
           yaw/pitch (degenerate eye) and nothing crashes; plus the E1b nil
           ``CurrentCamera`` guard and twice-call idempotency (E9).

Skips cleanly when ``luau`` is absent (the repo idiom — design edge case E8).
"""

from __future__ import annotations

import re
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
# Shared luau snippet: build a SceneRuntime with the injected pure helpers +
# mock services, then attach a fresh ``self._player`` state table (the §1
# shape) directly. Returns ``engine`` + the live ``p`` (its ``_player``).
# ``charHrp`` (default false) attaches a mock LocalPlayer.Character with a
# HumanoidRootPart at a fixed position so the eye-follow path is exercised;
# left false to drive the no-character tolerance path (AC10 / E1).
# ---------------------------------------------------------------------------

def _build_authority_runtime(
    *, char_hrp: bool = False, boot_provision: bool = False
) -> str:
    char_setup = ""
    if char_hrp:
        char_setup = """
        do
            local hrp = {Position = Vector3.new(10, 0, -5)}
            local char = {
                FindFirstChild = function(_, name)
                    if name == "HumanoidRootPart" then return hrp end
                    return nil
                end,
            }
            -- LocalPlayer is the camera-harness mock; attach a Character.
            local players = game:GetService("Players")
            players.LocalPlayer.Character = char
        end
"""
    if boot_provision:
        # Provision the boot-path mocks AC8 exercises:
        #   (1) the default-controls-off RESOLUTION chain LocalPlayer ->
        #       PlayerScripts -> PlayerModule, instrumented so ``_bootRecord``
        #       records the FindFirstChild traversal _playerBoot performs;
        #   (2) a local avatar Character with BasePart/Decal descendants that
        #       record their LocalTransparencyModifier writes.
        #
        # NOTE on the controls ``Disable()``: ``_playerBoot`` reaches the
        # controls via ``pcall(require, pm)`` (mirroring scene_camera_input.luau).
        # Standalone luau has NO ``require`` ("not supported in this context")
        # AND gives each loadstring'd chunk its own readonly ``_G``, so the
        # outer chunk CANNOT inject a working ``require`` into the runtime chunk
        # — ``require(pm)`` always fails here, so ``controls:Disable()`` can
        # never EXECUTE under standalone luau. We therefore assert the boot
        # TRAVERSES the full resolution chain to the PlayerModule (the furthest
        # the harness can prove dynamically); a static source-shape check
        # (test_player_boot_source_disables_controls) guards that the
        # GetControls()/controls:Disable() block itself is not dropped.
        # ``_bootRecord`` is a chunk-level table the assertions read back.
        char_setup += """
        do
            _bootRecord = {resolved = {}, hiddenParts = {}}

            local playerModule = {Name = "PlayerModule"}
            local playerScripts = {
                Name = "PlayerScripts",
                FindFirstChild = function(_, name)
                    _bootRecord.resolved[#_bootRecord.resolved + 1] = "ps:" .. name
                    if name == "PlayerModule" then return playerModule end
                    return nil
                end,
            }

            -- Avatar descendants: BasePart + Decal record their hide writes;
            -- a non-BasePart/Decal stays untouched (proves the IsA filter).
            local function mkPart(class, key)
                local part = {Name = key}
                function part:IsA(c) return c == class end
                setmetatable(part, {
                    __newindex = function(t, k, v)
                        if k == "LocalTransparencyModifier" then
                            _bootRecord.hiddenParts[key] = v
                        end
                        rawset(t, k, v)
                    end,
                })
                return part
            end
            local nonVisual = {Name = "script"}
            function nonVisual:IsA(_) return false end
            local descendants = {
                mkPart("BasePart", "torso"),
                mkPart("Decal", "face"),
                nonVisual,
            }
            -- Boot-reseed (D-P3-boot-green): _playerBoot now calls
            -- _playerResyncToCharacter(lp.Character) at the end, which reads
            -- char:FindFirstChild("HumanoidRootPart") (via _playerCharacterHRP).
            -- Provision an HRP-returning FindFirstChild (carrying a .CFrame /
            -- .Position) so the boot-reseed neither nil-derefs nor changes the
            -- AC8 assertions above (yaw reseed is a side write on _player).
            local bootHrp = {
                CFrame = CFrame.new(Vector3.new(3, 0, 4)),
                Position = Vector3.new(3, 0, 4),
            }
            local char = {
                GetDescendants = function() return descendants end,
                DescendantAdded = {Connect = function() return {Disconnect = function() end} end},
                FindFirstChild = function(_, name)
                    if name == "HumanoidRootPart" then return bootHrp end
                    return nil
                end,
            }

            local lp = game:GetService("Players").LocalPlayer
            lp.Character = char
            lp.FindFirstChild = function(_, name)
                _bootRecord.resolved[#_bootRecord.resolved + 1] = "lp:" .. name
                if name == "PlayerScripts" then return playerScripts end
                return nil
            end
        end
"""
    return f"""
        -- Inject the REAL pure camera helpers (reuse-not-rebuild, D9).
        local services = servicesFor({{modules = {{}}}}, {{}}, {{}})
        services.userInputService = game:GetService("UserInputService")
        services.players = game:GetService("Players")
        services.cameraAdvance = SceneCameraInput._advance
        services.cameraComposeLook = SceneCameraInput._composeLook
        -- D-P3-boot-green: the boot-reseed (_playerResyncToCharacter) reads
        -- self._services.cameraYawOf; inject it alongside the other helpers so
        -- the existing AC8 boot test + the new boot-reseed are build-green.
        services.cameraYawOf = SceneCameraInput._yawOf

        local engine = SceneRuntime.new(services, {{modules = {{}}}})

        -- Attach the §1 player state table directly (Slice 2.3 builds it via
        -- _initPlayerAuthority; here we construct it to unit-test the methods).
        local p = {{
            _yaw = 0, _pitch = 0,
            _booted = false,
            _jumpHeld = false,
            _sensitivity = 0.0045,
            _minPitch = math.rad(-80),
            _maxPitch = math.rad(80),
            _eyeHeight = 1.5,
        }}
        engine._player = p
{char_setup}
    """


# ---------------------------------------------------------------------------
# AC8 — boot (controls-off / Scriptable camera / mouse lock) + idempotency.
# ---------------------------------------------------------------------------

def test_player_boot_takes_camera_and_mouse_control() -> None:
    """``_playerBoot`` sets ``CameraType=Scriptable``, ``MouseBehavior=
    LockCenter``, ``MouseIconEnabled=false``, TRAVERSES the default-controls-off
    resolution chain (LocalPlayer -> PlayerScripts -> PlayerModule), HIDES the
    local avatar (``LocalTransparencyModifier=1`` on every BasePart/Decal
    descendant, leaving non-visual descendants untouched) and marks ``_booted``
    (AC8). The PlayerScripts.PlayerModule + avatar descendants are provisioned in
    the mock so a regression that drops the controls-resolution OR the avatar-hide
    path fails here. (``controls:Disable()`` itself cannot EXECUTE under
    standalone luau — see the helper note + the static source-shape guard in
    ``test_player_boot_source_disables_controls``.)"""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime(boot_provision=True) + """
        engine:_playerBoot()
        local cam = workspace.CurrentCamera
        local uis = game:GetService("UserInputService")
        print("CAMTYPE=" .. tostring(cam.CameraType))
        print("MOUSEBEHAVIOR=" .. tostring(uis.MouseBehavior))
        print("ICON=" .. tostring(uis.MouseIconEnabled))
        print("BOOTED=" .. tostring(p._booted))
        print("RESOLVED=" .. table.concat(_bootRecord.resolved, ","))
        print("HIDE_TORSO=" .. tostring(_bootRecord.hiddenParts.torso))
        print("HIDE_FACE=" .. tostring(_bootRecord.hiddenParts.face))
        print("HIDE_SCRIPT=" .. tostring(_bootRecord.hiddenParts.script))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "CAMTYPE=Scriptable" in out, out
    assert "MOUSEBEHAVIOR=LockCenter" in out, out
    assert "ICON=false" in out, out
    assert "BOOTED=true" in out, out
    # AC8 controls-off: boot RESOLVES the controls chain LocalPlayer ->
    # PlayerScripts -> PlayerModule (the furthest the harness can prove
    # dynamically; require() can't run under standalone luau). A regression that
    # deletes/skips the controls block stops emitting these resolver calls.
    assert "RESOLVED=lp:PlayerScripts,ps:PlayerModule" in out, out
    # AC8 avatar-hide: every BasePart/Decal descendant got
    # LocalTransparencyModifier=1; the non-visual descendant is left untouched.
    assert "HIDE_TORSO=1" in out, out
    assert "HIDE_FACE=1" in out, out
    assert "HIDE_SCRIPT=nil" in out, out


def test_player_boot_source_disables_controls() -> None:
    """AC8 controls-off (static guard). ``controls:Disable()`` can never EXECUTE
    under standalone luau (``require`` is unsupported there), so the dynamic boot
    test can only prove the resolution chain is reached. This static check guards
    that the ``GetControls()`` / ``controls:Disable()`` block in ``_playerBoot``
    is not silently dropped — pinning the half the interpreter can't run."""
    src = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    boot = src[src.index("function SceneRuntime:_playerBoot()") :]
    boot = boot[: boot.index("\nfunction SceneRuntime:")]
    assert ":GetControls()" in boot, "boot must resolve default PlayerModule controls"
    assert "controls:Disable()" in boot, "boot must Disable() the default controls"
    assert 'FindFirstChild("PlayerModule")' in boot, boot


def test_player_boot_is_idempotent() -> None:
    """A second ``_playerBoot`` is a no-op: it must NOT re-run the body. We
    prove idempotency by drifting ``CameraType`` AFTER the first boot and
    confirming the second boot does NOT re-assert it (AC8 — re-entrant)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime() + """
        engine:_playerBoot()
        -- Drift the camera type; an idempotent second boot leaves it drifted.
        workspace.CurrentCamera.CameraType = "DRIFTED"
        engine:_playerBoot()
        print("AFTER2=" .. tostring(workspace.CurrentCamera.CameraType))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "AFTER2=DRIFTED" in out, out


def test_player_boot_nil_camera_does_not_crash() -> None:
    """E1b — ``_playerBoot`` with a nil ``CurrentCamera`` still locks the mouse
    and marks booted without nil-derefing the camera."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime() + """
        workspace.CurrentCamera = nil
        engine:_playerBoot()
        local uis = game:GetService("UserInputService")
        print("MOUSEBEHAVIOR=" .. tostring(uis.MouseBehavior))
        print("BOOTED=" .. tostring(p._booted))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "MOUSEBEHAVIOR=LockCenter" in out, out
    assert "BOOTED=true" in out, out


# ---------------------------------------------------------------------------
# AC3 — single per-frame E2E read via the injected ``cameraAdvance`` (D9).
# ---------------------------------------------------------------------------

def test_read_input_advances_yaw_and_acks_e2e_channel_once() -> None:
    """``_playerReadInput`` reads the raw mouse delta + the E2E channel ONCE:
    after the call ``E2EMouseAckSeq == E2EMouseSeq`` and ``_yaw`` advanced by
    ``-(dx)*sensitivity``; a second same-frame read adds 0 (the seq is already
    acked) so ``_yaw`` does not advance again from the channel (AC3 / D9)."""
    preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0), (0.0, 0.0)])
    body = _build_authority_runtime() + """
        workspace:SetAttribute("E2EMouseDeltaX", 100.0)
        workspace:SetAttribute("E2EMouseDeltaY", 0.0)
        workspace:SetAttribute("E2EMouseSeq", 1)

        local dx1, dy1 = engine:_playerReadInput()
        local ack = workspace:GetAttribute("E2EMouseAckSeq")
        local yaw1 = p._yaw

        -- Second read, SAME seq: the channel is already acked, so its additive
        -- term is suppressed -> yaw must not advance further from the channel.
        local dx2, dy2 = engine:_playerReadInput()
        local yaw2 = p._yaw

        print(string.format("INJ=%.3f,%.3f", dx1, dy1))
        print(string.format("ACK=%d", ack))
        print(string.format("YAW1=%.6f", yaw1))
        print(string.format("NOOP=%.3f,%.3f", dx2, dy2))
        print(string.format("YAW2=%.6f", yaw2))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # The injected dx=100 reaches the advance helper this frame.
    assert "INJ=100.000,0.000" in out, out
    assert "ACK=1" in out, out
    # yaw1 = 0 - 100 * 0.0045 = -0.45
    assert "YAW1=-0.450000" in out, out
    # Second read: channel suppressed -> raw (0,0) only -> yaw unchanged.
    assert "NOOP=0.000,0.000" in out, out
    assert "YAW2=-0.450000" in out, out


def test_read_input_uses_injected_advance_not_camera_step() -> None:
    """D9 / §3 guardrail (static): the runtime's player-authority methods NEVER
    call ``SceneCameraInput:step`` / ``:_readDelta`` / ``:acquire`` /
    ``:configure``, and the selection/read path carries no AI-output substring
    matcher. Grep the player-authority block of the production runtime source
    directly (scoped to ``_playerBoot``..``_tick`` so an unrelated future
    ``:configure(`` / ``:step(`` elsewhere in ``scene_runtime.luau`` cannot
    false-fail this D9 guard)."""
    src = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    # Scope to the player-authority methods (``_playerBoot`` through the method
    # just before ``_tick``); the same slicing idiom as the AC8 boot guard.
    block = src[
        src.index("function SceneRuntime:_playerBoot()") :
        src.index("function SceneRuntime:_tick(")
    ]
    # The authority must reuse the INJECTED pure helper, never the singleton.
    for forbidden in (":step(", ":_readDelta(", ":acquire(", ":configure("):
        assert forbidden not in block, (
            f"player authority must not call {forbidden!r} (D9)"
        )
    # The single per-frame channel read advances via the injected helper.
    assert "self._services.cameraAdvance(" in block, block
    assert "self._services.cameraComposeLook(" in block, block


# ---------------------------------------------------------------------------
# AC10 — no-character tolerance (E1) + camera-write idempotency (E9).
# ---------------------------------------------------------------------------

def test_write_camera_no_character_composes_degenerate_eye() -> None:
    """AC10 / E1 — with no ``LocalPlayer.Character`` (no HRP), ``_playerWriteCamera``
    does not crash: it composes from accumulated yaw/pitch with the degenerate
    (current-camera-position) eye, and the camera is left ``Scriptable``."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime() + """
        p._yaw = 0.5
        p._pitch = -0.25
        engine:_playerWriteCamera()
        local cam = workspace.CurrentCamera
        local pitch, yaw = cam.CFrame:ToEulerAnglesYXZ()
        print("CAMTYPE=" .. tostring(cam.CameraType))
        print(string.format("LOOK=%.4f,%.4f", yaw, pitch))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "CAMTYPE=Scriptable" in out, out
    # composeLook(eye, yaw=0.5, pitch=-0.25) -> mock CFrame carries yaw/pitch.
    assert "LOOK=0.5000,-0.2500" in out, out


def test_write_camera_follows_character_hrp_eye() -> None:
    """When ``LocalPlayer.Character.HumanoidRootPart`` exists, the eye = HRP
    position + eyeHeight (E1 happy path) and the look composes from yaw/pitch."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime(char_hrp=True) + """
        p._yaw = 0.0
        p._pitch = 0.0
        engine:_playerWriteCamera()
        local cam = workspace.CurrentCamera
        local pos = cam.CFrame.Position
        print(string.format("EYE=%.1f,%.1f,%.1f", pos.X, pos.Y, pos.Z))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # HRP at (10, 0, -5) + Vector3(0, eyeHeight=1.5, 0).
    assert "EYE=10.0,1.5,-5.0" in out, out


def test_write_camera_twice_idempotent() -> None:
    """E9 — two ``_playerWriteCamera`` calls with unchanged state yield the SAME
    composed pose (both compose from the same yaw/pitch; no accumulation bug)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime() + """
        p._yaw = 0.3
        p._pitch = -0.1
        engine:_playerWriteCamera()
        local pitch1, yaw1 = workspace.CurrentCamera.CFrame:ToEulerAnglesYXZ()
        engine:_playerWriteCamera()
        local pitch2, yaw2 = workspace.CurrentCamera.CFrame:ToEulerAnglesYXZ()
        print(string.format("W1=%.6f,%.6f", yaw1, pitch1))
        print(string.format("W2=%.6f,%.6f", yaw2, pitch2))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    m1 = re.search(r"W1=([\-\d.]+,[\-\d.]+)", out)
    m2 = re.search(r"W2=([\-\d.]+,[\-\d.]+)", out)
    assert m1 and m2, out
    assert m1.group(1) == m2.group(1), out


def test_write_camera_nil_camera_does_not_crash() -> None:
    """E1b — ``_playerWriteCamera`` with a nil ``workspace.CurrentCamera`` (the
    pre-materialize / no-camera context) must not nil-deref: the camera write is
    guarded, so the call no-ops cleanly with no character and no camera."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime() + """
        p._yaw = 0.4
        p._pitch = 0.1
        workspace.CurrentCamera = nil
        engine:_playerWriteCamera()
        print("CAM=" .. tostring(workspace.CurrentCamera))
        print("NILWRITEOK=true")
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "CAM=nil" in out, out
    assert "NILWRITEOK=true" in out, out


def test_get_look_cframe_returns_current_camera_pose() -> None:
    """``getLookCFrame`` mirrors ``CurrentCamera.CFrame`` whether or not the
    player is bound (Phase-3 aim-read attach point; E1b nil-guard)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime() + """
        p._yaw = 0.7
        p._pitch = 0.0
        engine:_playerWriteCamera()
        local look = engine:getLookCFrame()
        local _, yaw = look:ToEulerAnglesYXZ()
        print(string.format("LOOKYAW=%.4f", yaw))

        -- E1b: nil CurrentCamera -> getLookCFrame degrades to CFrame.new().
        workspace.CurrentCamera = nil
        local fallback = engine:getLookCFrame()
        print("FALLBACK=" .. tostring(fallback ~= nil))
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "LOOKYAW=0.7000" in out, out
    assert "FALLBACK=true" in out, out


# ---------------------------------------------------------------------------
# Slice 2.2 — locomotion + jump (``_playerDriveLocomotion``).
#
# AC5 (unit half) — WASD drives camera-yaw-relative ``Humanoid:Move``.
# AC6 (unit / D-jump / E7) — jump is the every-frame last-writer:
#   ``humanoid.Jump = risingEdge`` is written EVERY frame (true ONLY on the
#   press edge, false otherwise), ``:ChangeState(Jumping)`` on the edge only.
#   Held Space across 2 frames -> frame 1 Jump=true + ChangeState once, frame 2
#   Jump=false + NO second ChangeState. The frame-2 ``Jump=false`` proves C wins
#   the every-frame write contest, not just the edge.
# ---------------------------------------------------------------------------


def _locomotion_setup(*, keys_down: str = "") -> str:
    """Provision the locomotion mocks ``_playerDriveLocomotion`` reads:

      * a ``Humanoid`` (``:Move`` records an ORDERED log; ``.Jump`` writes +
        ``:ChangeState`` calls record into ordered logs) resolved off
        ``LocalPlayer.Character:FindFirstChildOfClass("Humanoid")``;
      * ``UserInputService:IsKeyDown`` keyed off a mutable ``_keysDown`` set the
        scenario flips between frames (held Space across frames);
      * ``Enum.KeyCode`` / ``Enum.HumanoidStateType`` + ``CFrame.Angles(...)
        :VectorToWorldSpace`` (the camera-yaw-relative move basis) extended onto
        the camera-harness mocks.

    ``keys_down`` is a comma list of KeyCode names (e.g. ``"W,Space"``) held at
    setup; a scenario mutates ``_keysDown[name]`` between frames.
    """
    initial = "".join(
        f'        _keysDown["{k.strip()}"] = true\n'
        for k in keys_down.split(",")
        if k.strip()
    )
    return (
        """
        -- KeyCode / HumanoidStateType the runtime reads (extend the harness Enum).
        Enum.KeyCode = {
            W = "W", A = "A", S = "S", D = "D", Space = "Space",
        }
        Enum.HumanoidStateType = {Jumping = "Jumping"}

        -- CFrame.Angles(0, yaw, 0):VectorToWorldSpace(v) -> rotate v about Y by
        -- yaw (the camera-yaw-relative move basis). Returns a Vector3 with a
        -- ``.Magnitude`` / ``.Unit`` the runtime branches on.
        local _origAngles = CFrame.Angles
        CFrame.Angles = function(x, y, z)
            local cf = _origAngles(x, y, z)
            cf._yawBasis = y or 0
            function cf:VectorToWorldSpace(vec)
                local yaw = self._yawBasis or 0
                local cy, sy = math.cos(yaw), math.sin(yaw)
                -- rotate (vx, vy, vz) about +Y by yaw.
                local wx = vec.X * cy + vec.Z * sy
                local wz = -vec.X * sy + vec.Z * cy
                local out = Vector3.new(wx, vec.Y, wz)
                local mag = math.sqrt(wx * wx + vec.Y * vec.Y + wz * wz)
                out.Magnitude = mag
                if mag > 0 then
                    out.Unit = Vector3.new(wx / mag, vec.Y / mag, wz / mag)
                else
                    out.Unit = Vector3.new(0, 0, 0)
                end
                return out
            end
            return cf
        end

        -- Mutable held-key set + IsKeyDown on the harness UIS.
        _keysDown = {}
"""
        + initial
        + """
        do
            local uis = game:GetService("UserInputService")
            function uis:IsKeyDown(code) return _keysDown[code] == true end
        end

        -- Humanoid with ordered Move / Jump / ChangeState logs.
        _humLog = {moves = {}, jumps = {}, states = {}}
        local humanoid = {}
        function humanoid:IsA(c) return c == "Humanoid" end
        function humanoid:Move(vec, relativeToCamera)
            _humLog.moves[#_humLog.moves + 1] =
                string.format("%.4f,%.4f,%.4f", vec.X, vec.Y, vec.Z)
        end
        function humanoid:ChangeState(state)
            _humLog.states[#_humLog.states + 1] = tostring(state)
        end
        -- ``.Jump`` is recorded via __newindex on EVERY write. We must NOT
        -- rawset it onto the table (that would let later writes hit the existing
        -- key and bypass __newindex) -- the every-frame-write assertion needs
        -- each write logged. Stash the live value in a sibling field instead.
        setmetatable(humanoid, {
            __newindex = function(t, k, val)
                if k == "Jump" then
                    _humLog.jumps[#_humLog.jumps + 1] = tostring(val)
                    rawset(t, "_jumpValue", val)
                else
                    rawset(t, k, val)
                end
            end,
        })
        do
            local char = {
                FindFirstChildOfClass = function(_, cls)
                    if cls == "Humanoid" then return humanoid end
                    return nil
                end,
            }
            game:GetService("Players").LocalPlayer.Character = char
        end
"""
    )


def test_drive_locomotion_wasd_is_camera_yaw_relative() -> None:
    """AC5 (unit) — WASD drives ``Humanoid:Move`` with a camera-yaw-relative
    direction. With yaw=0 and W held, the move dir is forward (-Z); with yaw
    rotated 90deg the SAME W input rotates the world move dir, proving the move
    basis follows the camera yaw (not a fixed world axis)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _locomotion_setup(keys_down="W")
        + """
        -- yaw = 0: W (v=+1) -> local (0,0,-1) -> world (0,0,-1) [forward].
        p._yaw = 0
        engine:_playerDriveLocomotion()
        print("MOVE0=" .. _humLog.moves[#_humLog.moves])

        -- yaw = +90deg: the SAME W input rotates the world move dir about +Y.
        p._yaw = math.rad(90)
        engine:_playerDriveLocomotion()
        print("MOVE90=" .. _humLog.moves[#_humLog.moves])
        print("NMOVES=" .. tostring(#_humLog.moves))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # yaw=0, W -> unit forward (0, 0, -1).
    assert "MOVE0=0.0000,0.0000,-1.0000" in out, out
    # yaw=90deg: rotating (0,0,-1) about +Y by 90deg -> (-1, 0, 0). The Z term
    # rounds to a signed zero ("0.0000" / "-0.0000"); accept either sign.
    m90 = re.search(r"MOVE90=(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)", out)
    assert m90, out
    assert float(m90.group(1)) == pytest.approx(-1.0, abs=1e-4), out
    assert float(m90.group(2)) == pytest.approx(0.0, abs=1e-4), out
    assert float(m90.group(3)) == pytest.approx(0.0, abs=1e-4), out
    assert "NMOVES=2" in out, out


def test_drive_locomotion_no_keys_moves_zero() -> None:
    """AC5 (unit) — with no WASD held, ``Humanoid:Move`` is called with the zero
    vector (the lowered A body's else-branch parity — the move is always issued,
    so the character halts rather than coasting)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _locomotion_setup()
        + """
        engine:_playerDriveLocomotion()
        print("MOVE=" .. _humLog.moves[#_humLog.moves])
        print("NMOVES=" .. tostring(#_humLog.moves))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "MOVE=0.0000,0.0000,0.0000" in out, out
    assert "NMOVES=1" in out, out


def test_drive_locomotion_jump_held_space_single_fire_every_frame_write() -> None:
    """AC6 (unit / D-jump / E7) — THE load-bearing assertion. Hold Space across
    TWO frames:

      * Frame 1 (rising edge): ``humanoid.Jump = true`` AND ``ChangeState
        (Jumping)`` fires exactly ONCE.
      * Frame 2 (Space STILL held, no longer the edge): ``humanoid.Jump = false``
        is WRITTEN (the every-frame last-writer — this is what dominates A's
        mid-pass ``hum.Jump = true``) and NO second ``ChangeState``.

    The frame-2 ``Jump=false`` write (not merely an absent edge) is the proof
    that C is the LAST jump writer EVERY frame, giving one-jump-per-press UX
    while still overwriting A's per-frame ``true``."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _locomotion_setup(keys_down="Space")
        + """
        -- Frame 1: Space held, rising edge (p._jumpHeld starts false).
        engine:_playerDriveLocomotion()
        print("JUMPS_F1=" .. table.concat(_humLog.jumps, ","))
        print("STATES_F1=" .. table.concat(_humLog.states, ","))
        print("HELD_F1=" .. tostring(p._jumpHeld))

        -- Frame 2: Space STILL held (NOT the edge). C must STILL write Jump
        -- (=false) and must NOT ChangeState again.
        engine:_playerDriveLocomotion()
        print("JUMPS_F2=" .. table.concat(_humLog.jumps, ","))
        print("STATES_F2=" .. table.concat(_humLog.states, ","))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # Frame 1: the edge -> Jump=true written, ChangeState fired once.
    assert "JUMPS_F1=true" in out, out
    assert "STATES_F1=Jumping" in out, out
    assert "HELD_F1=true" in out, out
    # Frame 2: Space still held but past the edge -> Jump=false WRITTEN
    # (every-frame last-writer), no SECOND ChangeState. Total jump writes = 2
    # (true then false); total ChangeState = 1.
    assert "JUMPS_F2=true,false" in out, out
    assert "STATES_F2=Jumping" in out, out


def test_drive_locomotion_jump_release_then_repress_fires_again() -> None:
    """AC6 (unit) — releasing Space and pressing it again is a NEW rising edge:
    a second ``ChangeState(Jumping)`` fires (one jump per press, repeatable).
    Frames: hold -> release -> hold. Edges at frame 1 and frame 3 only."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _locomotion_setup(keys_down="Space")
        + """
        engine:_playerDriveLocomotion()            -- frame 1: edge
        _keysDown["Space"] = false
        engine:_playerDriveLocomotion()            -- frame 2: released
        _keysDown["Space"] = true
        engine:_playerDriveLocomotion()            -- frame 3: NEW edge
        print("JUMPS=" .. table.concat(_humLog.jumps, ","))
        print("STATES=" .. table.concat(_humLog.states, ","))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # Every-frame write: true (edge), false (released), true (new edge).
    assert "JUMPS=true,false,true" in out, out
    # ChangeState only on the two rising edges (frames 1 and 3).
    assert "STATES=Jumping,Jumping" in out, out


def test_drive_locomotion_no_humanoid_noops() -> None:
    """AC5/E1 — with no character Humanoid resolvable, ``_playerDriveLocomotion``
    no-ops (no Move / Jump / ChangeState) and does not crash."""
    preamble = camera_input_preamble(mouse_deltas=[])
    # No _locomotion_setup -> LocalPlayer.Character stays nil (camera harness
    # default) so FindFirstChildOfClass is never reachable. Provide the Enum +
    # IsKeyDown the method reads BEFORE the humanoid resolution, to prove the
    # no-op is the missing-humanoid guard, not a nil-index earlier.
    body = (
        _build_authority_runtime()
        + """
        Enum.KeyCode = {W="W", A="A", S="S", D="D", Space="Space"}
        Enum.HumanoidStateType = {Jumping = "Jumping"}
        do
            local uis = game:GetService("UserInputService")
            function uis:IsKeyDown(_code) return false end
        end
        local ok = pcall(function() engine:_playerDriveLocomotion() end)
        print("NOHUM_OK=" .. tostring(ok))
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "NOHUM_OK=true" in out, out


def test_drive_locomotion_noop_when_player_nil() -> None:
    """``_playerDriveLocomotion`` no-ops when ``self._player == nil`` (the
    fail-closed server / no-player key)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _locomotion_setup(keys_down="W,Space")
        + """
        engine._player = nil
        engine:_playerDriveLocomotion()
        print("NILMOVES=" .. tostring(#_humLog.moves))
        print("NILJUMPS=" .. tostring(#_humLog.jumps))
        print("NILLOCOMOK=true")
    """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # player nil -> early return before resolving the humanoid -> zero writes.
    assert "NILMOVES=0" in out, out
    assert "NILJUMPS=0" in out, out
    assert "NILLOCOMOK=true" in out, out


def test_methods_noop_when_player_nil() -> None:
    """Every authority method no-ops when ``self._player == nil`` (the
    fail-closed server / no-player key — the brackets that gate this land in
    Slice 2.3, but the methods themselves must already tolerate nil)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = _build_authority_runtime() + """
        engine._player = nil
        engine:_playerBoot()
        local dx, dy = engine:_playerReadInput()
        engine:_playerWriteCamera()
        print(string.format("NILREAD=%.1f,%.1f", dx, dy))
        print("NILOK=true")
    """
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "NILREAD=0.0,0.0" in out, out
    assert "NILOK=true" in out, out
