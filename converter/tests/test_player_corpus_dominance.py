"""Slice 2.4 — the LOAD-BEARING D8/D-jump corpus C-dominance proof, BY EXECUTION.

This is the proof the whole player-embodiment effort rests on: it does NOT
string-match a modeled ``"POST"`` bracket (that is the Step-1a substrate proof,
``test_player_shape_corpus.py``). Instead it:

  1. Applies the REAL ``lower_camera_facet`` + ``lower_movement_facet`` passes to
     BOTH ``cold3a59`` AND ``dde248`` THE PRODUCTION WAY
     (``follow_character_paths=[the player script]`` — the only invocation
     production uses, ``contract_pipeline.py:446,500-501``; NEVER without the
     follow-set, which is the non-production strict-locator path the design
     corrected). It ASSERTS each fixture's empirical lower-count as a
     PRE-CONDITION (both → camera=1 AND move=1: both lower their look method to
     ``self._cam:step``→``_readDelta`` and their WASD body to ``hum:Move`` +
     ``hum.Jump = true``). A future lowerer change that silently flips a shape's
     A behavior fails the precondition LOUDLY rather than passing vacuously.

  2. ``loadstring``s + RUNS each lowered fixture's Luau under bus-backed mocks
     (modeled on ``test_player_shape_corpus.py``), driving the REAL lowered A
     competitor (``self._cam:step`` camera write + ``hum:Move`` / ``hum.Jump =
     true`` mid-frame) AND the REAL ``_playerPreTick`` / ``_playerPostTick``
     brackets that ``SceneRuntime:_tick`` runs around the component ``pairs()``
     loop. The lowered ``Rotate`` / ``Move`` methods are wired to the runtime's
     ``Update`` / ``LateUpdate`` lifecycle so they run INSIDE the real ``_tick``
     component pass, bracketed by the REAL host authority.

  3. Asserts C wins camera + move + jump NON-VACUOUSLY on BOTH shapes BY READING
     the FINAL recorded bus/log values (the ordered ``CurrentCamera.CFrame``
     write log, the ordered ``Humanoid:Move`` log, the ordered ``Humanoid.Jump``
     write log) — NOT by string-matching the lowered source. Non-vacuity is
     proven by giving C a distinctive yaw (advanced via the E2E channel C acks
     first) and confirming (a) the lowered A write actually LANDED mid-pass
     (its entry is present in the ordered log) and (b) the FINAL entry is C's.
     Including the held-Space frame-2 jump last-writer (AC6.b): C's post-write
     makes frame-2 ``Jump=false`` even though A wrote ``true`` mid-frame.

  4. AC4b raw-camera coverage: EXECUTES a SYNTHETIC raw-camera-writer component
     (its ``Update`` does ``workspace.CurrentCamera.CFrame = <junk>`` +
     ``CameraType = <junk>``) inside the REAL ``_tick`` ``pairs()`` loop with the
     REAL brackets, asserting C's write is the last writer of BOTH ``CFrame`` and
     ``CameraType`` — recovering the §3 raw-``CurrentCamera``-survives coverage
     WITHOUT omitting ``follow_character_paths``.

  5. AC7: paradigm A is still ACTIVE (the lowered competitor actually runs — its
     writes appear in the ordered logs; Phase 2 added NO suppression of the
     movement/camera facet lowering).

The luau-sim classes skip cleanly without ``luau``; the pure-Python lower-count
precondition is asserted inside each luau test (it gates the scenario), so the
empirical fact is checked whenever the proof runs.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from converter.camera_facet_lowering import lower_camera_facet
from converter.movement_facet_lowering import lower_movement_facet
from tests._camera_input_harness import (
    CAMERA_INPUT_PATH,
    camera_input_preamble,
    run_camera_scenario,
)

HOST_RUNTIME_PATH = (
    Path(__file__).parent.parent / "runtime" / "scene_runtime.luau"
)
_FIXTURES = Path(__file__).parent / "fixtures" / "player_shapes"
_FIXTURE_NAMES = ("cold3a59", "dde248")


def _luau_available() -> bool:
    return shutil.which("luau") is not None


pytestmark = pytest.mark.skipif(
    not _luau_available()
    or not CAMERA_INPUT_PATH.exists()
    or not HOST_RUNTIME_PATH.exists(),
    reason="needs standalone luau interpreter + runtime files",
)


# --------------------------------------------------------------------------- #
# REAL lowering — production path (follow_character_paths=[the player script]).
# --------------------------------------------------------------------------- #


class _Script:
    """Minimal ``_HasLuauSource`` for the lowerers (they read/write
    ``luau_source`` only)."""

    def __init__(self, source: str) -> None:
        self.luau_source = source


def _lower_fixture_production(name: str) -> tuple[str, int, int]:
    """Apply the REAL camera + movement lowerers to a fixture THE PRODUCTION
    WAY and return ``(lowered_source, camera_count, move_count)``.

    Production (``contract_pipeline.py:499-501``) passes
    ``follow_character_paths=players`` to ``lower_camera_facet`` and the player
    script to ``lower_movement_facet`` — BOTH applied to the SAME script object,
    so the returned source is the fully-lowered shape the runtime ships. NEVER
    omits ``follow_character_paths`` (the non-production strict-locator path).
    """
    src = (_FIXTURES / f"{name}_player.luau").read_text(encoding="utf-8")
    s = _Script(src)
    camera = lower_camera_facet([s], follow_character_paths=[s])
    move = lower_movement_facet([s])
    return s.luau_source, camera, move


# --------------------------------------------------------------------------- #
# Extra mock surface PORTED onto the camera-input harness (design §2.4 MINOR):
#   * Vector3 Magnitude / Unit (C + lowered A read both in locomotion);
#   * CFrame VectorToWorldSpace (getYawBasis():VectorToWorldSpace(...) — both
#     the lowered A Move and C's _playerDriveLocomotion);
#   * Enum KeyCode (W/A/S/D/Space) + HumanoidStateType + Material;
#   * a RECORDING CurrentCamera: every ``.CFrame`` / ``.CameraType`` write is
#     appended to an ordered log (via __newindex on a backing table — NOT
#     rawset, per the 2.2 harness gotcha) so "who wrote last" is read off the
#     log, not a single mutable cell;
#   * LocalPlayer.Character with a recording Humanoid (.Jump via a sibling
#     _jumpValue field — NOT rawset, per the 2.2 gotcha — + an ordered :Move /
#     .Jump / :ChangeState log) and a HumanoidRootPart (so C's eye is
#     character-bound, non-vacuous);
#   * ReplicatedStorage:WaitForChild("SceneCameraInput") returning the REAL
#     loaded SceneCameraInput module (so the lowered A's require + :step +
#     getYawBasis run the REAL singleton, consuming the E2E channel);
#   * Space-down UIS (IsKeyDown(Space) true, W/D true so WASD is non-trivial).
# The extra setup runs AFTER the base mocks and BEFORE the camera module loads,
# so SceneCameraInput's module-load game:GetService still resolves the base
# RunService/UIS/Players, and the lowered A's runtime require resolves the REAL
# loaded module. KeyCodes are set true via a closure the test toggles per frame.
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
        do
            local _cfmt = getmetatable(CFrame.new(Vector3.new(0, 0, 0)))
            -- __index is the metatable itself (CFramemt), so a method added
            -- here is reachable on every CFrame instance.
            _cfmt.VectorToWorldSpace = function(_self, v) return v end
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
        -- grounded check (if any survives lowering) passes.
        local humanoid = {FloorMaterial = "Material.Grass", _jumpValue = nil}
        humanoid._moveLog = {}
        humanoid._jumpLog = {}
        humanoid._stateLog = {}
        local _humBacking = humanoid
        function humanoid:Move(dir, rel)
            table.insert(_humBacking._moveLog, dir)
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
        -- expose the backing (logs + Move/ChangeState) to the scenario.
        workspace._humanoid = humanoid

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
        -- Wire ReplicatedStorage:WaitForChild("SceneCameraInput") to a
        -- placeholder; the scenario body overrides ``require`` so require() on
        -- it returns the REAL loaded SceneCameraInput module (its :step /
        -- getYawBasis / _readDelta then run for real, consuming the E2E
        -- channel). Nothing in the base module-load path touches
        -- ReplicatedStorage (SceneCameraInput resolves only RunService / UIS /
        -- Players at load).
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
# Scenario body builder: load the lowered fixture, register it as a component
# whose Update -> lowered Rotate (camera) and LateUpdate -> lowered Move
# (humanoid + jump), build the real authority, drive _tick frame(s).
# --------------------------------------------------------------------------- #


def _embed(source: str, tag: str) -> str:
    delim = "=="
    while f"]{delim}]" in source or f"[{delim}[" in source:
        delim += "="
    return f"[{delim}[\n{source}\n]{delim}]"


def _dominance_body(*, lowered_source: str, frames: int) -> str:
    """Build the scenario body that drives ``frames`` real ``_tick`` calls with
    the lowered fixture component bracketed by the real authority."""
    fixture_lit = _embed(lowered_source, "fixture")
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

        -- Load the lowered fixture (returns the Player class table). It closes
        -- over the ambient Vector3/CFrame/Enum/workspace globals.
        local PlayerChunk = assert(loadstring(
            "return (function() " .. __FIXTURE_LIT__ .. " end)()",
            "lowered_fixture"))
        local LoweredPlayer = PlayerChunk()
        assert(type(LoweredPlayer) == "table",
            "lowered fixture must return its module table")

        -- The lowered A controller instance: a fixture ``self`` carrying the
        -- gameObject (the rig the camera service configures) + uis.
        local rig = {}
        function rig:GetPivot() return CFrame.new(Vector3.new(0, 0, 0)) end
        function rig:PivotTo(_cf) end
        local aComp = setmetatable({gameObject = rig,
                                    uis = game:GetService("UserInputService")},
                                   LoweredPlayer)

        -- Map the lowered fixture's look/move methods onto the runtime
        -- lifecycle the _tick pairs() loop drives: Update -> Rotate (the
        -- lowered camera _cam:step competitor), LateUpdate -> Move (the lowered
        -- humanoid :Move + .Jump competitor). cold3a59 also has its own
        -- Update (the split-read cache); run it first so its cache populates.
        local classTable = {}
        function classTable.Update(self, dt)
            if LoweredPlayer.Update then LoweredPlayer.Update(self, dt) end
            LoweredPlayer.Rotate(self, dt)
        end
        function classTable.LateUpdate(self, dt)
            LoweredPlayer.Move(self, dt)
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

        -- Register the lowered A component into the real component map so the
        -- real _tick pairs() loop drives it (Update + LateUpdate), bracketed
        -- by the REAL _playerPreTick / _playerPostTick.
        engine._meta[aComp] = {
            classTable = classTable, scriptId = "player",
            activeInHierarchy = true, enabled = true,
        }

        -- Drive ``frames`` real ticks. Before each, push the E2E channel so C's
        -- _playerReadInput (pre-Update) acks the seq FIRST and the lowered A's
        -- _cam:step->_readDelta (mid-Update) sees seq==ack and adds 0 (D9 race).
        local FRAMES = __FRAMES__
        local SEQ = 0
        for f = 1, FRAMES do
            SEQ = SEQ + 1
            workspace:SetAttribute("E2EMouseSeq", SEQ)
            -- A big deltaX so C's yaw advances by a clearly non-zero amount,
            -- distinguishing C's camera write from A's (A's singleton yaw stays
            -- 0 because it never wins the ack).
            workspace:SetAttribute("E2EMouseDeltaX", 1000.0)
            workspace:SetAttribute("E2EMouseDeltaY", 0.0)
            engine:_tick(0.016)
        end

        -- Read the recorded logs. The final CurrentCamera.CFrame write is the
        -- last entry whose key == "CFrame".
        local camWrites = workspace._camWrites
        local lastCamYaw, sawNonCYaw, anyCamWrite = nil, false, false
        local cYaw = engine._player._yaw
        for _, w in ipairs(camWrites) do
            if w.key == "CFrame" then
                anyCamWrite = true
                lastCamYaw = w.value._yaw
                -- A's singleton wrote a CFrame whose yaw is NOT C's (0 vs cYaw).
                if w.value._yaw ~= cYaw then sawNonCYaw = true end
            end
        end

        local hum = workspace._humanoid
        local moveLog = hum._moveLog
        local jumpLog = hum._jumpLog
        local stateLog = hum._stateLog

        print("CYAW=" .. tostring(cYaw))
        print("ANYCAMWRITE=" .. tostring(anyCamWrite))
        print("SAWNONCYAW=" .. tostring(sawNonCYaw))
        print("LASTCAMYAW=" .. tostring(lastCamYaw))
        print("MOVES=" .. tostring(#moveLog))
        print("JUMPS=" .. tostring(#jumpLog))
        print("FIRSTJUMP=" .. tostring(jumpLog[1]))
        print("LASTJUMP=" .. tostring(jumpLog[#jumpLog]))
        print("STATES=" .. tostring(#stateLog))
        -- Per-frame jump log slice: with N frames, A writes Jump=true once per
        -- frame mid-LateUpdate and C writes once per frame post-LateUpdate, so
        -- the log is [A_f1, C_f1, A_f2, C_f2, ...]. Print the C entries (even
        -- indices) so the multi-frame last-writer is checkable.
        for i = 1, #jumpLog do
            print("JUMPLOG[" .. tostring(i) .. "]=" .. tostring(jumpLog[i]))
        end
    """)
    return (template
            .replace("__FIXTURE_LIT__", fixture_lit)
            .replace("__FRAMES__", str(frames)))


def _run_dominance(*, name: str, keys_down: list[str], frames: int):
    """Lower a fixture the production way, ASSERT the lower-count precondition,
    then run the real-authority dominance scenario. Returns (rc, out, err,
    camera_count, move_count)."""
    lowered_source, camera, move = _lower_fixture_production(name)
    preamble = camera_input_preamble(
        mouse_deltas=[(0.0, 0.0)] * (frames + 2),
        extra_mock_setup=_dominance_extra_setup(keys_down=keys_down),
    )
    body = _dominance_body(lowered_source=lowered_source, frames=frames)
    rc, out, err = run_camera_scenario(preamble, body)
    return rc, out, err, camera, move


# --------------------------------------------------------------------------- #
# AC4 + AC5 + AC6 + AC7 — corpus C-dominance on BOTH real post-lowering shapes.
# --------------------------------------------------------------------------- #


class TestCorpusDominanceByExecution:

    @pytest.mark.parametrize("name", _FIXTURE_NAMES)
    def test_lower_count_precondition(self, name: str) -> None:
        # AC4 precondition (P1-1): on the PRODUCTION path BOTH fixtures lower
        # IDENTICALLY — camera=1 (look method -> self._cam:step) AND move=1
        # (WASD body -> hum:Move + hum.Jump=true). A future lowerer change that
        # flips a shape's A behavior fails HERE, not vacuously downstream.
        _src, camera, move = _lower_fixture_production(name)
        assert (camera, move) == (1, 1), (
            f"{name}: production-path lower-count must be camera=1/move=1 "
            f"(both fixtures fully A-bound); got camera={camera} move={move}"
        )

    @pytest.mark.parametrize("name", _FIXTURE_NAMES)
    def test_C_dominates_camera_and_move(self, name: str) -> None:
        # AC4 (camera) + AC5 (move) + AC7 (A active): one frame, WASD held so the
        # lowered A Move runs locomotion. C dominates BOTH the camera CFrame and
        # the Humanoid move-intent BY EXECUTION, read off the ordered logs.
        rc, out, err, camera, move = _run_dominance(
            name=name, keys_down=["W", "D"], frames=1)
        assert (camera, move) == (1, 1), f"{name}: precondition drift"
        assert rc == 0, f"{name}: luau failed: {err}\n{out}"

        # AC7 / non-vacuity: the lowered A camera competitor ACTUALLY ran and
        # wrote the camera (its write is in the ordered log), AND it wrote a yaw
        # that is NOT C's (A's singleton never won the E2E ack -> its yaw stayed
        # 0, distinct from C's E2E-advanced yaw). A's :Move also ran.
        assert "ANYCAMWRITE=true" in out, f"{name}: no camera write logged\n{out}"
        assert "SAWNONCYAW=true" in out, (
            f"{name}: the lowered A camera write (a yaw != C's) must be present "
            f"mid-pass — proves A is ACTIVE + dominance non-vacuous\n{out}"
        )
        assert "MOVES=2" in out, (
            f"{name}: expect 2 Humanoid:Move per frame (lowered A mid-pass + C "
            f"post-bracket); A active + C present\n{out}"
        )

        # AC4 (camera dominance): the FINAL camera CFrame is C's (its E2E-
        # advanced yaw), not A's mid-pass write.
        cyaw = _grab(out, "CYAW=")
        last = _grab(out, "LASTCAMYAW=")
        assert float(last) == float(cyaw), (
            f"{name}: final CurrentCamera.CFrame must be C's post-write "
            f"(yaw={cyaw}); got last-writer yaw={last}\n{out}"
        )
        # C's yaw is non-zero (advanced by the E2E delta C acked) so the
        # last==C assertion is NON-VACUOUS (not 0==0 with A coincidentally 0).
        assert float(cyaw) != 0.0, (
            f"{name}: C's yaw must advance via the E2E channel C acks first; "
            f"got {cyaw} (D9 ACK race not exercised)\n{out}"
        )

    @pytest.mark.parametrize("name", _FIXTURE_NAMES)
    def test_C_dominates_jump_held_space_multiframe(self, name: str) -> None:
        # AC6 (a)+(b) — the load-bearing jump proof. Hold Space across TWO
        # frames. The lowered A Move writes Jump=true mid-LateUpdate EVERY frame;
        # C's _playerDriveLocomotion (post-LateUpdate) is the LAST Jump writer
        # each frame: frame 1 ends Jump=true (C's rising edge), frame 2 ends
        # Jump=false (held, past the edge — C still the last writer over A's
        # mid-pass true). Read off the ordered Jump log, NOT a string match.
        rc, out, err, camera, move = _run_dominance(
            name=name, keys_down=["W", "Space"], frames=2)
        assert (camera, move) == (1, 1), f"{name}: precondition drift"
        assert rc == 0, f"{name}: luau failed: {err}\n{out}"

        # 4 Jump writes total: [A_f1=true, C_f1=true, A_f2=true, C_f2=false].
        # A active (its true is present), C last each frame.
        jumps = int(_grab(out, "JUMPS="))
        assert jumps == 4, (
            f"{name}: expect 4 Jump writes (A+C each of 2 frames); got {jumps}\n{out}"
        )
        # A wrote true mid-pass (non-vacuous: A active).
        assert _grab(out, "JUMPLOG[1]=") == "true", (
            f"{name}: lowered A must write Jump=true mid-pass frame 1\n{out}"
        )
        assert _grab(out, "JUMPLOG[3]=") == "true", (
            f"{name}: lowered A must write Jump=true mid-pass frame 2\n{out}"
        )
        # C is the LAST writer each frame: frame 1 -> true (rising edge),
        # frame 2 -> false (held past the edge). The frame-2 false PROVES C wins
        # even after the edge while A is still writing true (AC6.b).
        assert _grab(out, "JUMPLOG[2]=") == "true", (
            f"{name}: C must be the last Jump writer frame 1 = true (edge)\n{out}"
        )
        assert _grab(out, "JUMPLOG[4]=") == "false", (
            f"{name}: C must be the last Jump writer frame 2 = false (held past "
            f"the edge, dominating A's mid-pass true) — AC6.b\n{out}"
        )
        # ChangeState(Jumping) fires ONCE (the single rising edge), not every
        # frame — correct one-jump-per-press UX.
        assert _grab(out, "STATES=") == "1", (
            f"{name}: ChangeState(Jumping) must fire once on the rising edge "
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
# --------------------------------------------------------------------------- #


class TestRawCameraDominance:

    def test_C_dominates_synthetic_raw_camera_writer(self) -> None:
        # AC4b: a synthetic component whose Update STOMPS workspace.CurrentCamera
        # .CFrame (junk yaw) AND .CameraType (junk) — the §3 case the camera
        # lowerer would ABSTAIN on. Driven inside the REAL _tick pairs() loop
        # with the REAL brackets; C's post-write must be the LAST writer of BOTH
        # CFrame and CameraType. Recovered WITHOUT omitting follow_character_paths.
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
