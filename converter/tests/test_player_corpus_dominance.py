"""Slice 2.4 (adapted, Phase 5) — the LOAD-BEARING D8/D-jump corpus
C-dominance proof, BY EXECUTION, WITHOUT paradigm A.

This is the proof the whole player-embodiment effort rests on: it does NOT
string-match a modeled ``"POST"`` bracket (that is the Step-1a substrate proof,
``test_player_shape_corpus.py``). Phase 5 DELETED paradigm A
(``movement_facet_lowering.py`` whole module + the camera-facet PLAYER path), so
in production BOTH player fixtures are genuinely **native (un-lowered)** — C is
their only camera/move/jump writer. The mid-pass competitor is therefore the
fixture's OWN NATIVE raw writes (the writes A used to neutralize), not a lowered
shape. The proof:

  1. Loads each fixture's NATIVE (un-lowered) Luau as the mid-pass competitor and
     RUNS it under bus-backed mocks (modeled on ``test_player_shape_corpus.py``),
     driving the REAL native competing writes AND the REAL ``_playerPreTick`` /
     ``_playerPostTick`` brackets that ``SceneRuntime:_tick`` runs around the
     component ``pairs()`` loop. The native ``Rotate`` / ``Move`` methods are
     wired to the runtime's ``Update`` / ``LateUpdate`` lifecycle so they run
     INSIDE the real ``_tick`` component pass, bracketed by the REAL host
     authority.

  2. Asserts C wins NON-VACUOUSLY by READING the FINAL recorded bus/log values
     (the ordered ``CurrentCamera.CFrame`` write log, the ordered
     ``Humanoid:Move`` log, the ordered ``Humanoid.Jump`` write log) — NOT by
     string-matching the source. Non-vacuity is proven by giving C a distinctive
     yaw (advanced via the E2E channel C acks first) and confirming (a) the
     native competing write actually LANDED mid-pass (its entry is present in the
     ordered log) and (b) the FINAL entry is C's.

  3. PER-FIXTURE surface (P1-b — verified against the fixture bytes; the
     competitor is NOT uniform):
       * ``cold3a59`` natively writes a raw ``cam.CFrame =`` (its ``Rotate``)
         but its ``Move`` is a rig ``:PivotTo`` — it has NO ``Humanoid:Move`` /
         no ``Jump`` to dominate. So cold3a59 proves CAMERA-surface dominance
         ONLY (C is the last writer of the native ``cam.CFrame``).
       * ``dde248`` natively writes a raw ``cam.CFrame =`` + ``humanoid:Move(``
         + ``humanoid.Jump = true``. So dde248 proves CAMERA + MOVE + JUMP
         dominance. The move/jump dominance proof RIDES dde248.

  4. AC4b raw-camera coverage: EXECUTES a SYNTHETIC raw-camera-writer component
     (its ``Update`` does ``workspace.CurrentCamera.CFrame = <junk>`` +
     ``CameraType = <junk>``) inside the REAL ``_tick`` ``pairs()`` loop with the
     REAL brackets, asserting C's write is the last writer of BOTH ``CFrame`` and
     ``CameraType``. (Unchanged — it never used the lowerers.)

C-dominance over the camera surface on BOTH shapes, AND over move+jump on the
move/jump-bearing shape (dde248), is proven WITHOUT A — this is the strangler-fig
invariant: deleting A leaves no responsibility implicitly A-owned.

The luau-sim classes skip cleanly without ``luau``.
"""

from __future__ import annotations

import shutil
import textwrap
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
_FIXTURES = Path(__file__).parent / "fixtures" / "player_shapes"


def _luau_available() -> bool:
    return shutil.which("luau") is not None


pytestmark = pytest.mark.skipif(
    not _luau_available()
    or not CAMERA_INPUT_PATH.exists()
    or not HOST_RUNTIME_PATH.exists(),
    reason="needs standalone luau interpreter + runtime files",
)


# --------------------------------------------------------------------------- #
# NATIVE fixture loading — Phase 5 deleted paradigm A, so the competitor is the
# fixture's OWN un-lowered raw writes (no lowering pass).
# --------------------------------------------------------------------------- #


class _Script:
    """Minimal ``_HasLuauSource`` holder (kept for callers that wrap a fixture
    source as a script-like object; reads/writes ``luau_source`` only)."""

    def __init__(self, source: str) -> None:
        self.luau_source = source


def _native_fixture_source(name: str) -> str:
    """Return the NATIVE (un-lowered) fixture source — the production shape after
    paradigm A's deletion (C is its only camera/move/jump writer)."""
    return (_FIXTURES / f"{name}_player.luau").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Extra mock surface PORTED onto the camera-input harness (design §2.4 MINOR):
#   * Vector3 Magnitude / Unit (C + native fixture read both in locomotion);
#   * CFrame VectorToWorldSpace (getYawBasis():VectorToWorldSpace(...) — both
#     the native Move and C's _playerDriveLocomotion);
#   * Enum KeyCode (W/A/S/D/Space) + HumanoidStateType + Material;
#   * a RECORDING CurrentCamera: every ``.CFrame`` / ``.CameraType`` write is
#     appended to an ordered log (via __newindex on a backing table — NOT
#     rawset, per the 2.2 harness gotcha) so "who wrote last" is read off the
#     log, not a single mutable cell;
#   * LocalPlayer.Character with a recording Humanoid (.Jump via a sibling
#     _jumpValue field — NOT rawset, per the 2.2 gotcha — + an ordered :Move /
#     .Jump / :ChangeState log) and a HumanoidRootPart (so C's eye is
#     character-bound, non-vacuous). The recording Humanoid PROXY is exposed as
#     ``workspace._humProxy`` so a native fixture whose ``Move`` reads
#     ``self.control`` (the CharacterController/Humanoid) records onto the SAME
#     ordered logs C writes to;
#   * ReplicatedStorage:WaitForChild("SceneCameraInput") returning the REAL
#     loaded SceneCameraInput module;
#   * Space-down UIS (IsKeyDown(Space) true, W/D true so WASD is non-trivial).
# The extra setup runs AFTER the base mocks and BEFORE the camera module loads.
# --------------------------------------------------------------------------- #


def _dominance_extra_setup(*, keys_down: list[str]) -> str:
    """Return the ``extra_mock_setup`` snippet wiring the locomotion +
    recording-bus surface onto the live camera-input harness globals.

    ``keys_down`` is the set of KeyCode leaf names (e.g. ``["W", "D",
    "Space"]``) ``UserInputService:IsKeyDown`` returns true for.
    """
    keys_literal = "{" + ", ".join(f'["{k}"] = true' for k in keys_down) + "}"
    template = textwrap.dedent("""\
        -- ---- Vector3 Magnitude / Unit (locomotion reads both) -------------
        do
            local _v3mt = getmetatable(Vector3.new(0, 0, 0))
            _v3mt.__index = function(t, k)
                if k == "Magnitude" then
                    return math.sqrt(t.X * t.X + t.Y * t.Y + t.Z * t.Z)
                elseif k == "Unit" then
                    return t
                end
                return nil
            end
        end

        -- ---- CFrame VectorToWorldSpace (getYawBasis():VectorToWorldSpace) --
        -- A REAL yaw rotation (NOT identity): rotate the local WASD vector about
        -- Y by the basis CFrame's accumulated ``_yaw``. This makes the native
        -- fixture's move (basis = the rig pivot, yaw 0) NUMERICALLY DISTINCT from
        -- C's move (basis = CFrame.Angles(0, p._yaw, 0) with C's E2E-advanced
        -- non-zero yaw). The rotated vector also carries ``_srcYaw`` = the basis
        -- yaw that produced it, so the scenario can read WHICH basis (the
        -- native fixture's 0 vs C's cYaw) wrote each ordered :Move entry.
        do
            local _cfmt = getmetatable(CFrame.new(Vector3.new(0, 0, 0)))
            -- __index is the metatable itself (CFramemt), so a method added
            -- here is reachable on every CFrame instance.
            _cfmt.VectorToWorldSpace = function(_self, v)
                local yaw = _self._yaw or 0
                local c, s = math.cos(yaw), math.sin(yaw)
                -- Roblox VectorToWorldSpace for CFrame.Angles(0, yaw, 0):
                --   x' =  x*cos + z*sin ;  z' = -x*sin + z*cos
                local out = Vector3.new(
                    v.X * c + v.Z * s, v.Y, -v.X * s + v.Z * c)
                out._srcYaw = yaw
                return out
            end
        end

        -- ---- Enum: extend with KeyCode / HumanoidStateType / Material -----
        Enum.KeyCode = setmetatable({}, {__index = function(_, k)
            return "KeyCode." .. tostring(k)
        end})
        Enum.HumanoidStateType = setmetatable({}, {__index = function(_, k)
            return "HumanoidStateType." .. tostring(k)
        end})
        Enum.Material = setmetatable({}, {__index = function(_, k)
            return "Material." .. tostring(k)
        end})

        -- ---- UserInputService: WASD/Space IsKeyDown ----------------------
        local _keysDown = __KEYS_LITERAL__
        function UserInputService:IsKeyDown(code)
            -- code is "KeyCode.W" etc.; key off the leaf name.
            local leaf = tostring(code):match("%.([%a%d]+)$") or tostring(code)
            return _keysDown[leaf] == true
        end

        -- ---- Recording CurrentCamera (ordered write log; __newindex) ------
        -- Replace the base CurrentCamera cell with a proxy whose .CFrame /
        -- .CameraType writes append to workspace._camWrites in order. The
        -- backing store is a sibling table so __newindex fires on EVERY write
        -- (rawset'ing the key itself would only log the first - 2.2 gotcha).
        do
            local _backing = {CFrame = CFrame.new(Vector3.new(0, 0, 0)),
                              CameraType = nil}
            workspace._camWrites = {}
            local camProxy = setmetatable({}, {
                __index = function(_t, k) return _backing[k] end,
                __newindex = function(_t, k, v)
                    _backing[k] = v
                    if k == "CFrame" or k == "CameraType" then
                        table.insert(workspace._camWrites,
                            {key = k, value = v})
                    end
                end,
            })
            workspace.CurrentCamera = camProxy
        end

        -- ---- LocalPlayer.Character + recording Humanoid + HRP -------------
        -- The recording Humanoid logs :Move(dir) (ordered), .Jump writes
        -- (ordered, via a sibling _jumpValue field - NOT rawset, 2.2 gotcha),
        -- and :ChangeState(state) (ordered). FloorMaterial set Grass so a
        -- grounded check (the native Move reads FloorMaterial ~= Air) passes.
        local humanoid = {FloorMaterial = "Material.Grass", _jumpValue = nil}
        humanoid._moveLog = {}
        humanoid._jumpLog = {}
        humanoid._stateLog = {}
        local _humBacking = humanoid
        function humanoid:Move(dir, rel)
            -- Record the FULL move vector (its components + the basis yaw that
            -- rotated it) so the scenario can prove WHICH writer (the native
            -- fixture's yaw-0 basis vs C's yaw-cYaw basis) is the LAST :Move --
            -- not merely count moves.
            table.insert(_humBacking._moveLog, {
                x = dir.X, y = dir.Y, z = dir.Z, srcYaw = dir._srcYaw,
            })
        end
        function humanoid:ChangeState(state)
            table.insert(_humBacking._stateLog, state)
        end
        function humanoid:IsA(c) return c == "Humanoid" end
        local humProxy = setmetatable({}, {
            __index = function(_t, k)
                if k == "Jump" then return _humBacking._jumpValue end
                return _humBacking[k]
            end,
            __newindex = function(_t, k, v)
                if k == "Jump" then
                    _humBacking._jumpValue = v
                    table.insert(_humBacking._jumpLog, v)
                else
                    _humBacking[k] = v
                end
            end,
        })
        -- expose the backing (logs + Move/ChangeState) AND the proxy (so a
        -- native fixture whose Move reads self.control records here too).
        workspace._humanoid = humanoid
        workspace._humProxy = humProxy

        local hrp = {Position = Vector3.new(10, 0, 20)}
        local character = {}
        function character:FindFirstChild(n)
            if n == "HumanoidRootPart" then return hrp end
            return nil
        end
        function character:FindFirstChildOfClass(c)
            if c == "Humanoid" then return humProxy end
            return nil
        end
        function character:GetDescendants() return {} end
        character.DescendantAdded = {Connect = function(_, _fn)
            return {Disconnect = function() end}
        end}
        -- Bind the character onto the harness LocalPlayer (game:GetService
        -- "Players".LocalPlayer - the SAME object C reads via
        -- self._services.players).
        do
            local lp = game:GetService("Players").LocalPlayer
            lp.Character = character
        end

        -- ---- ReplicatedStorage -> the REAL SceneCameraInput module --------
        do
            local realGetService = game.GetService
            local RS = {}
            function RS:WaitForChild(n)
                if n == "SceneCameraInput" then
                    return {__sceneCameraInputModule = true}
                end
                return nil
            end
            game.GetService = function(self, name)
                if name == "ReplicatedStorage" then return RS end
                return realGetService(self, name)
            end
        end
    """)
    return template.replace("__KEYS_LITERAL__", keys_literal)


# --------------------------------------------------------------------------- #
# Scenario body builder: load the NATIVE fixture, register it as a component
# whose Update -> native Update+Rotate (camera) and LateUpdate -> native Move
# (rig pivot for cold3a59; humanoid + jump for dde248), build the real
# authority, drive _tick frame(s).
# --------------------------------------------------------------------------- #


def _embed(source: str, tag: str) -> str:
    delim = "=="
    while f"]{delim}]" in source or f"[{delim}[" in source:
        delim += "="
    return f"[{delim}[\n{source}\n]{delim}]"


def _dominance_body(*, native_source: str, frames: int) -> str:
    """Build the scenario body that drives ``frames`` real ``_tick`` calls with
    the NATIVE fixture component bracketed by the real authority."""
    fixture_lit = _embed(native_source, "fixture")
    template = textwrap.dedent("""\
        -- Make require() on the SceneCameraInput placeholder return the REAL
        -- loaded module (SceneCameraInput is the local the camera-service
        -- loader exposed before this body runs).
        local _origRequire = require
        require = function(target)
            if type(target) == "table" and target.__sceneCameraInputModule then
                return SceneCameraInput
            end
            return _origRequire(target)
        end

        -- STUDS_PER_METER: the cold3a59 native Move scales by it (line 46). It
        -- is a transpiler-emitted module constant in production; seed it here.
        if STUDS_PER_METER == nil then STUDS_PER_METER = 1 end

        -- Load the NATIVE fixture (returns the Player class table). It closes
        -- over the ambient Vector3/CFrame/Enum/workspace globals.
        local PlayerChunk = assert(loadstring(
            "return (function() " .. __FIXTURE_LIT__ .. " end)()",
            "native_fixture"))
        local NativePlayer = PlayerChunk()
        assert(type(NativePlayer) == "table",
            "native fixture must return its module table")

        -- The native fixture instance: a fixture ``self`` carrying the
        -- gameObject (rig), uis, the look-math fields the native Rotate reads,
        -- and ``control`` = the recording Humanoid proxy (the native dde248 Move
        -- reads self.control as the Humanoid; cold3a59 ignores it for moves).
        local rig = {}
        function rig:GetPivot() return CFrame.new(Vector3.new(0, 0, 0)) end
        function rig:PivotTo(_cf) end
        local aComp = setmetatable({
            gameObject = rig,
            uis = game:GetService("UserInputService"),
            control = workspace._humProxy,
            cam = workspace.CurrentCamera,
            sensitivity = 1.0,
            speed = 1.0,
            minAngle = -80.0,
            maxAngle = 80.0,
            camRotation = Vector3.new(0, 0, 0),
            camRotationX = 0,
            moveDirection = Vector3.new(0, 0, 0),
            pendingMouse = Vector3.new(0, 0, 0),
        }, NativePlayer)

        -- Map the native fixture's look/move methods onto the runtime lifecycle
        -- the _tick pairs() loop drives: Update -> (native Update +) Rotate (the
        -- native camera competitor), LateUpdate -> Move (native rig-pivot move
        -- for cold3a59 / native humanoid :Move + .Jump for dde248). cold3a59 has
        -- its own Update (the split-read cache); run it first so its cache
        -- populates before Rotate consumes it.
        local classTable = {}
        function classTable.Update(self, dt)
            if NativePlayer.Update then NativePlayer.Update(self, dt) end
            NativePlayer.Rotate(self, dt)
        end
        function classTable.LateUpdate(self, dt)
            NativePlayer.Move(self, dt)
        end

        -- Build the real SceneRuntime over a ONE-CC-module plan so
        -- _initPlayerAuthority binds self._player; inject the client services.
        local plan = {modules = {
            player = {stem = "Player", runtime_bearing = true,
                      has_character_controller = true},
        }}
        local services = servicesFor(plan, {}, {})
        services.isClient = true
        services.userInputService = game:GetService("UserInputService")
        services.players = game:GetService("Players")
        services.cameraAdvance = SceneCameraInput._advance
        services.cameraComposeLook = SceneCameraInput._composeLook
        local engine = SceneRuntime.new(services, plan)
        engine:_initPlayerAuthority()
        assert(engine._player ~= nil, "authority must bind on the CC module")

        -- Register the native component into the real component map so the real
        -- _tick pairs() loop drives it (Update + LateUpdate), bracketed by the
        -- REAL _playerPreTick / _playerPostTick.
        engine._meta[aComp] = {
            classTable = classTable, scriptId = "player",
            activeInHierarchy = true, enabled = true,
        }

        -- Drive ``frames`` real ticks. Before each, push the E2E channel so C's
        -- _playerReadInput (pre-Update) acks the seq FIRST and C's yaw advances
        -- by a clearly non-zero amount.
        local FRAMES = __FRAMES__
        local SEQ = 0
        for f = 1, FRAMES do
            SEQ = SEQ + 1
            workspace:SetAttribute("E2EMouseSeq", SEQ)
            workspace:SetAttribute("E2EMouseDeltaX", 1000.0)
            workspace:SetAttribute("E2EMouseDeltaY", 0.0)
            engine:_tick(0.016)
        end

        -- Read the recorded logs. The final CurrentCamera.CFrame write is the
        -- last entry whose key == "CFrame".
        local camWrites = workspace._camWrites
        local lastCamYaw, anyCamWrite = nil, false
        local cYaw = engine._player._yaw
        for _, w in ipairs(camWrites) do
            if w.key == "CFrame" then
                anyCamWrite = true
                lastCamYaw = w.value._yaw
            end
        end

        local hum = workspace._humanoid
        local moveLog = hum._moveLog
        local jumpLog = hum._jumpLog
        local stateLog = hum._stateLog

        print("CYAW=" .. tostring(cYaw))
        print("ANYCAMWRITE=" .. tostring(anyCamWrite))
        print("LASTCAMYAW=" .. tostring(lastCamYaw))
        print("MOVES=" .. tostring(#moveLog))
        local lastMove = moveLog[#moveLog]
        local firstMove = moveLog[1]
        if lastMove then
            print("LASTMOVESRCYAW=" .. tostring(lastMove.srcYaw))
            print("LASTMOVEX=" .. tostring(lastMove.x))
            print("LASTMOVEZ=" .. tostring(lastMove.z))
        end
        if firstMove then
            print("FIRSTMOVESRCYAW=" .. tostring(firstMove.srcYaw))
        end
        print("JUMPS=" .. tostring(#jumpLog))
        print("FIRSTJUMP=" .. tostring(jumpLog[1]))
        print("LASTJUMP=" .. tostring(jumpLog[#jumpLog]))
        print("STATES=" .. tostring(#stateLog))
        for i = 1, #jumpLog do
            print("JUMPLOG[" .. tostring(i) .. "]=" .. tostring(jumpLog[i]))
        end
    """)
    return (template
            .replace("__FIXTURE_LIT__", fixture_lit)
            .replace("__FRAMES__", str(frames)))


def _run_dominance(*, name: str, keys_down: list[str], frames: int):
    """Run the real-authority dominance scenario over the NATIVE fixture as the
    mid-pass competitor. Returns (rc, out, err)."""
    native_source = _native_fixture_source(name)
    preamble = camera_input_preamble(
        mouse_deltas=[(0.0, 0.0)] * (frames + 2),
        extra_mock_setup=_dominance_extra_setup(keys_down=keys_down),
    )
    body = _dominance_body(native_source=native_source, frames=frames)
    rc, out, err = run_camera_scenario(preamble, body)
    return rc, out, err


# --------------------------------------------------------------------------- #
# Corpus C-dominance on BOTH NATIVE shapes, PER-FIXTURE surface (P1-b):
#   * cold3a59 -> CAMERA-surface dominance only (no native Humanoid:Move/Jump);
#   * dde248   -> CAMERA + MOVE + JUMP dominance.
# --------------------------------------------------------------------------- #


class TestCorpusDominanceByExecution:

    @pytest.mark.parametrize("name", ("cold3a59", "dde248"))
    def test_C_dominates_camera(self, name: str) -> None:
        # Camera-surface dominance on BOTH native shapes. WASD held so the native
        # Move (rig pivot for cold3a59 / Humanoid:Move for dde248) runs alongside
        # the native camera write. C dominates the camera CFrame BY EXECUTION,
        # read off the ordered camera-write log.
        rc, out, err = _run_dominance(
            name=name, keys_down=["W", "D"], frames=1)
        assert rc == 0, f"{name}: luau failed: {err}\n{out}"

        # Non-vacuity: the native camera write ACTUALLY ran and wrote the camera
        # (its write is in the ordered log) mid-pass.
        assert "ANYCAMWRITE=true" in out, f"{name}: no camera write logged\n{out}"

        cyaw = _grab(out, "CYAW=")
        # C's yaw is non-zero (advanced by the E2E delta C acked) so the
        # last-writer-is-C assertion is NON-VACUOUS (not 0==0).
        assert float(cyaw) != 0.0, (
            f"{name}: C's yaw must advance via the E2E channel C acks first; "
            f"got {cyaw} (D9 ACK race not exercised)\n{out}"
        )

        # CAMERA dominance: the FINAL camera CFrame is C's (its E2E-advanced
        # yaw), not the native raw write (rig-pivot yaw 0).
        last = _grab(out, "LASTCAMYAW=")
        assert float(last) == float(cyaw), (
            f"{name}: final CurrentCamera.CFrame must be C's post-write "
            f"(yaw={cyaw}); got last-writer yaw={last}\n{out}"
        )

    def test_C_dominates_move_dde248(self) -> None:
        # MOVE dominance rides dde248 (P1-b): its native Move calls
        # ``humanoid:Move(`` so the native competitor is a real Humanoid:Move.
        # cold3a59 has NO native Humanoid:Move (its move is a rig :PivotTo), so
        # there is no move surface to dominate there.
        rc, out, err = _run_dominance(
            name="dde248", keys_down=["W", "D"], frames=1)
        assert rc == 0, f"dde248: luau failed: {err}\n{out}"

        # The native Humanoid:Move ran mid-pass AND C's post-bracket move ran ->
        # 2 ordered :Move entries (native mid-pass + C post-bracket).
        assert "MOVES=2" in out, (
            f"dde248: expect 2 Humanoid:Move per frame (native mid-pass + C "
            f"post-bracket); native Move active + C present\n{out}"
        )

        cyaw = _grab(out, "CYAW=")
        assert float(cyaw) != 0.0, (
            f"dde248: C's yaw must advance via the E2E channel\n{out}"
        )

        # MOVE dominance, last-writer BY VALUE: the native :Move runs mid-pass
        # with the rig-pivot yaw basis (yaw 0), so its move vector carries
        # srcYaw=0 (the FIRST entry). C's _playerDriveLocomotion runs in the post
        # bracket with CFrame.Angles(0, cYaw, 0), so its move vector carries
        # srcYaw=cYaw (non-zero). Asserting the FINAL :Move entry's basis yaw ==
        # cYaw PROVES C is the LAST move writer.
        first_move_yaw = _grab(out, "FIRSTMOVESRCYAW=")
        assert float(first_move_yaw) == 0.0, (
            f"dde248: the native mid-pass :Move must use the rig-pivot yaw basis "
            f"(0) — it is the FIRST move entry\n{out}"
        )
        last_move_yaw = _grab(out, "LASTMOVESRCYAW=")
        assert float(last_move_yaw) == float(cyaw), (
            f"dde248: final Humanoid:Move must be C's post-bracket move "
            f"(basis yaw={cyaw}), NOT the native mid-pass move (basis yaw=0); "
            f"got last-move basis yaw={last_move_yaw}\n{out}"
        )
        # And the final move vector is NUMERICALLY different from the native
        # one. For WASD W+D the local vector is (1,0,-1); native (yaw 0) -> X=1,
        # C (yaw=cYaw) -> X = cos(cYaw) + (-1)*sin(cYaw) != 1 for any cYaw not a
        # multiple of 2pi.
        last_move_x = float(_grab(out, "LASTMOVEX="))
        assert last_move_x != 1.0, (
            f"dde248: C's move vector must differ NUMERICALLY from the native's "
            f"(native X=1 at yaw 0); got last-move X={last_move_x} — "
            f"VectorToWorldSpace must be a real yaw rotation, not identity\n{out}"
        )

    def test_C_dominates_jump_held_space_multiframe_dde248(self) -> None:
        # JUMP dominance rides dde248 (P1-b): its native Move writes
        # ``humanoid.Jump = true`` on Space. cold3a59 has NO native jump writer.
        # Hold Space across TWO frames. The native Move writes Jump=true
        # mid-LateUpdate EVERY frame; C's _playerDriveLocomotion (post-LateUpdate)
        # is the LAST Jump writer each frame: frame 1 ends Jump=true (C's rising
        # edge), frame 2 ends Jump=false (held, past the edge — C still the last
        # writer over the native mid-pass true). Read off the ordered Jump log.
        rc, out, err = _run_dominance(
            name="dde248", keys_down=["W", "Space"], frames=2)
        assert rc == 0, f"dde248: luau failed: {err}\n{out}"

        # 4 Jump writes total: [native_f1=true, C_f1=true, native_f2=true,
        # C_f2=false]. Native Move active (its true is present), C last each frame.
        jumps = int(_grab(out, "JUMPS="))
        assert jumps == 4, (
            f"dde248: expect 4 Jump writes (native+C each of 2 frames); got "
            f"{jumps}\n{out}"
        )
        # The native Move wrote true mid-pass (non-vacuous: native jump active).
        assert _grab(out, "JUMPLOG[1]=") == "true", (
            f"dde248: native Move must write Jump=true mid-pass frame 1\n{out}"
        )
        assert _grab(out, "JUMPLOG[3]=") == "true", (
            f"dde248: native Move must write Jump=true mid-pass frame 2\n{out}"
        )
        # C is the LAST writer each frame: frame 1 -> true (rising edge),
        # frame 2 -> false (held past the edge). The frame-2 false PROVES C wins
        # even after the edge while the native Move is still writing true.
        assert _grab(out, "JUMPLOG[2]=") == "true", (
            f"dde248: C must be the last Jump writer frame 1 = true (edge)\n{out}"
        )
        assert _grab(out, "JUMPLOG[4]=") == "false", (
            f"dde248: C must be the last Jump writer frame 2 = false (held past "
            f"the edge, dominating the native mid-pass true)\n{out}"
        )
        # ChangeState(Jumping) fires ONCE (the single rising edge), not every
        # frame — correct one-jump-per-press UX.
        assert _grab(out, "STATES=") == "1", (
            f"dde248: ChangeState(Jumping) must fire once on the rising edge "
            f"(one jump per held press)\n{out}"
        )


def _grab(out: str, prefix: str) -> str:
    """Return the value printed for ``prefix`` (first match)."""
    for line in out.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    raise AssertionError(f"no line {prefix!r} in:\n{out}")


# --------------------------------------------------------------------------- #
# AC4b — C dominates an ACTUAL raw CurrentCamera write (the §3 abstain case),
# via a SYNTHETIC raw-camera-writer component in the real _tick pairs() loop.
# This never used the lowerers, so it is UNCHANGED by Phase 5.
# --------------------------------------------------------------------------- #


class TestRawCameraDominance:

    def test_C_dominates_synthetic_raw_camera_writer(self) -> None:
        # A synthetic component whose Update STOMPS workspace.CurrentCamera
        # .CFrame (junk yaw) AND .CameraType (junk). Driven inside the REAL _tick
        # pairs() loop with the REAL brackets; C's post-write must be the LAST
        # writer of BOTH CFrame and CameraType.
        preamble = camera_input_preamble(
            mouse_deltas=[(0.0, 0.0)],
            extra_mock_setup=_dominance_extra_setup(keys_down=[]),
        )
        body = textwrap.dedent("""\
            local plan = {modules = {
                player = {stem = "Player", runtime_bearing = true,
                          has_character_controller = true},
            }}
            local services = servicesFor(plan, {}, {})
            services.isClient = true
            services.userInputService = game:GetService("UserInputService")
            services.players = game:GetService("Players")
            services.cameraAdvance = SceneCameraInput._advance
            services.cameraComposeLook = SceneCameraInput._composeLook
            local engine = SceneRuntime.new(services, plan)
            engine:_initPlayerAuthority()
            assert(engine._player ~= nil, "authority must bind")

            -- Synthetic raw-camera-writer: a junk CFrame (sentinel yaw 7.0) +
            -- a junk CameraType, written in BOTH Update and LateUpdate.
            local JUNK = "JUNK_CAMERA_TYPE"
            local function stomp()
                local c = CFrame.new(Vector3.new(0, 0, 0))
                c._yaw = 7.0
                workspace.CurrentCamera.CFrame = c
                workspace.CurrentCamera.CameraType = JUNK
            end
            local Comp = {}
            function Comp.Update(_self, _dt) stomp() end
            function Comp.LateUpdate(_self, _dt) stomp() end
            local comp = setmetatable({}, {__index = Comp})
            engine._meta[comp] = {
                classTable = Comp, scriptId = "raw",
                activeInHierarchy = true, enabled = true,
            }

            -- Drive an E2E delta so C's yaw is a distinctive non-7.0 value.
            workspace:SetAttribute("E2EMouseSeq", 1)
            workspace:SetAttribute("E2EMouseDeltaX", 500.0)
            workspace:SetAttribute("E2EMouseDeltaY", 0.0)
            engine:_tick(0.016)

            local cYaw = engine._player._yaw
            local camWrites = workspace._camWrites
            local sawJunkCFrame, sawJunkType = false, false
            local lastCFrameYaw, lastType = nil, nil
            for _, w in ipairs(camWrites) do
                if w.key == "CFrame" then
                    lastCFrameYaw = w.value._yaw
                    if w.value._yaw == 7.0 then sawJunkCFrame = true end
                elseif w.key == "CameraType" then
                    lastType = w.value
                    if w.value == JUNK then sawJunkType = true end
                end
            end
            print("CYAW=" .. tostring(cYaw))
            print("SAWJUNKCFRAME=" .. tostring(sawJunkCFrame))
            print("SAWJUNKTYPE=" .. tostring(sawJunkType))
            print("LASTCFRAMEYAW=" .. tostring(lastCFrameYaw))
            print("LASTTYPE=" .. tostring(lastType))
        """)
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"luau failed: {err}\n{out}"
        # Non-vacuity: the synthetic raw writes ACTUALLY landed mid-pass.
        assert "SAWJUNKCFRAME=true" in out, (
            f"the synthetic raw CFrame stomp (yaw 7.0) must land mid-pass\n{out}"
        )
        assert "SAWJUNKTYPE=true" in out, (
            f"the synthetic raw CameraType stomp must land mid-pass\n{out}"
        )
        # C dominates BOTH surfaces: the final CFrame is C's (its E2E-advanced
        # yaw, NOT the junk 7.0) and the final CameraType is Scriptable (C's
        # re-assert), NOT the junk.
        cyaw = _grab(out, "CYAW=")
        assert float(cyaw) != 0.0 and float(cyaw) != 7.0, (
            f"C's yaw must be a distinctive E2E-advanced value; got {cyaw}\n{out}"
        )
        assert float(_grab(out, "LASTCFRAMEYAW=")) == float(cyaw), (
            f"C must be the last CFrame writer (yaw={cyaw}), not the raw junk\n{out}"
        )
        assert _grab(out, "LASTTYPE=") == "Scriptable", (
            f"C must be the last CameraType writer (Scriptable), not junk\n{out}"
        )
