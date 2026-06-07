"""Phase 1 slice 1.2: shape-variance corpus + host-C-dominance (AC6, AC7).

Two checked-in ``Player.luau`` shapes live under ``fixtures/player_shapes/``:
  * ``dde248_player.luau`` — the A-HIT shape (native cam-write + ``humanoid:Move``
    + helper-wrapped WASD); paradigm A neutralizes its writes.
  * ``cold3a59_player.luau`` — the A-MISS shape (``GetMouseDelta`` cached in
    Update, cam-write in Rotate, rig ``PivotTo`` move — NOT ``Humanoid:Move``);
    paradigm A misses it.

Two responsibilities, BOTH deterministic, NEITHER a runtime matcher:

  1. **Shape-fact guard (AC6, pure Python, always runs — no luau):** assert each
     fixture's load-bearing shape facts so the fixture can't silently drift from
     the real transpiler shape it stands in for (edge E3). The cold3a59 A-miss
     contract is enforced WITHOUT a brittle file-global negative substring:
     the "no Humanoid locomotion move" invariant is checked SCOPED to the
     ``Move`` method body only. These are assertions ABOUT INPUTS (corpus
     invariants); they NEVER feed binding/classification.

  2. **C-dominance (AC7, luau-sim):** load and EXECUTE the fixture's REAL
     competing code — instantiate the fixture module with a ``self`` whose
     ``cam``/``gameObject``/``control(humanoid)``/``uis`` point at bus-backed
     mocks, call its REAL ``Update``/``Rotate``/``Move`` mid-frame, then apply
     the modeled host "C" post bracket and assert C wins by last-writer-wins —
     NON-VACUOUSLY (the fixture's competing write must have actually LANDED).
     For cold3a59 the test also pins the SPLIT-READ contract behaviorally:
     ``Update`` caches the mouse delta into ``self.pendingMouse`` and ``Rotate``
     CONSUMES that cache (a variant that clears the cache before ``Rotate``
     yields a different — zero-rotation — camera, so a ``Rotate`` that ignored
     the cache FAILS). Drift in the fixture (or a fixture that didn't write)
     FAILS the test.

AC7 is SELF-CONTAINED: it loads + executes the real fixture ``.luau`` modules
under its OWN 1.2-local bus-backed mock surface (``_MOCKS`` / ``_fixture_scenario``).
It depends only on the pre-existing base-ref luau-sim helpers ``_run_scenario`` /
``_luau_available`` — it does NOT consume any slice-1.1 ``bus.*`` shared-mock
surface (1.1's ``_two_component_preamble`` boots Writer/Reader PLAN modules, not
fixture modules, so it cannot execute these fixtures; cf. design §2.5).

The luau-sim AC7 class skips cleanly where ``luau`` is absent (edge E6); the
pure-Python AC6 guard runs regardless (the luau skip is scoped to the AC7 class,
NOT the module).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

# Reuse the pre-existing base-ref luau-sim helpers read-only (``_run_scenario`` /
# ``_luau_available`` predate this phase; not a slice-1.1 deliverable). The
# fixture-loading + bus-backed mock surface below is 1.2-local (1.1's
# ``_two_component_preamble`` boots Writer/Reader PLAN modules, not the corpus
# fixture modules — so AC7 cannot reuse it to execute the real fixtures).
from tests.test_scene_runtime_host_behavior import _luau_available, _run_scenario

_FIXTURES = Path(__file__).parent / "fixtures" / "player_shapes"
_DDE248 = _FIXTURES / "dde248_player.luau"
_COLD3A59 = _FIXTURES / "cold3a59_player.luau"


# --------------------------------------------------------------------------- #
# AC6 — corpus shape-fact guard (pure Python, ALWAYS runs, no luau).
# INPUT-validation corpus invariants; NEVER a runtime classification matcher.
# --------------------------------------------------------------------------- #


def _method_body(src: str, header: str) -> str:
    """Return the text from ``header`` to the next top-level ``function `` decl
    (or end of file). Coarse but sufficient to confine a substring search to one
    method body for the AC6 INPUT-validation guard (NOT a runtime matcher)."""
    start = src.find(header)
    if start < 0:
        return ""
    nxt = src.find("\nfunction ", start + len(header))
    return src[start:] if nxt < 0 else src[start:nxt]


def test_dde248_is_A_hit_shape() -> None:
    """The A-HIT fixture natively contains all three writes paradigm A
    neutralizes: a direct ``cam.CFrame =`` camera write, a real Humanoid
    ``:Move(`` locomotion CALL (not the method header), and ``_axis(``-style
    helper-wrapped WASD USED at a call site (not the helper definition).

    The ``:Move(`` / ``_axis(`` checks are scoped to LOAD-BEARING call sites:
    a file-global substring would be satisfied by the ``function Player:Move``
    header and the ``local function _axis`` / ``function Player:_axis``
    definition, so a fixture that dropped the real ``humanoid:Move(...)``
    locomotion call or stopped USING helper-wrapped WASD would still pass.
    """
    src = _DDE248.read_text(encoding="utf-8")
    assert "cam.CFrame =" in src, "A-hit fixture must contain a direct camera write"

    # (a) A REAL Humanoid move CALL inside the Move method body, not the
    # ``function Player:Move`` header. Drop the header line, then require a
    # ``:Move(`` call on a humanoid-like receiver (``humanoid:Move(`` here).
    move_body = _method_body(src, "function Player:Move")
    assert move_body, "A-hit fixture must define a Player:Move method"
    move_calls = move_body.split("\n", 1)[1] if "\n" in move_body else ""
    assert "humanoid:Move(" in move_calls, (
        "the A-hit Move body must make a real Humanoid move call "
        f"(humanoid:Move(...)), not just the method header; got:\n{move_body}"
    )
    # Guard the receiver claim: there is a ``:Move(`` call that is NOT the
    # ``function Player:Move`` declaration (i.e. a call, not a header).
    assert ":Move(" in move_calls, (
        "the A-hit Move body must contain a Humanoid :Move( CALL distinct from "
        f"the method header; got:\n{move_body}"
    )

    # (b) helper-wrapped WASD is actually USED IN the Move body — an ``_axis(``
    # CALL site (e.g. ``self:_axis(...)``), distinct from the ``function _axis``
    # / ``local function _axis`` / ``function Player:_axis`` DEFINITION header.
    # Scope to the Move body (codex harden P2): a whole-file substring would
    # false-pass on a stray ``_axis(`` in another method/comment/string and would
    # NOT prove helper-wrapped WASD drives locomotion in Player:Move.
    axis_call_lines = [
        ln for ln in move_calls.splitlines()
        if "_axis(" in ln and "function" not in ln
    ]
    assert axis_call_lines, (
        "the A-hit fixture must USE helper-wrapped WASD at a CALL site "
        "(self:_axis(...)) INSIDE the Move body, not merely define the helper or "
        "call it elsewhere; no non-def _axis( call line found in Player:Move"
    )


def test_cold3a59_is_A_miss_shape() -> None:
    """The A-MISS fixture's load-bearing contract:

      * a CAMERA write specifically (``cam.CFrame`` / ``CurrentCamera``), not
        any ``CFrame =``;
      * the SPLIT READ: ``GetMouseDelta`` cached into ``self.pendingMouse`` in
        the ``Update`` method, and the cached ``self.pendingMouse`` CONSUMED in
        ``Rotate`` (which does NOT itself re-read raw ``GetMouseDelta``). This
        split is the load-bearing A-miss contract — a drift where ``Rotate``
        stops consuming the cache (or re-reads raw mouse) must FAIL this guard;
      * locomotion is a rig ``:PivotTo(`` and NOT a Humanoid ``:Move(`` —
        enforced SCOPED to the ``Move`` method body only, so it is not brittle
        against an incidental ``:Move(`` elsewhere (comments / a future
        unrelated method).
    """
    src = _COLD3A59.read_text(encoding="utf-8")

    # A CAMERA write specifically (not any `CFrame =`).
    assert ("cam.CFrame =" in src) or ("CurrentCamera" in src), (
        "A-miss fixture must contain a camera write (cam.CFrame / CurrentCamera)"
    )
    assert "cam.CFrame =" in src, "the camera write must be a cam.CFrame assignment"

    # Split read, half 1: GetMouseDelta is CACHED into self.pendingMouse in the
    # Update method body (NOT consumed there).
    update_body = _method_body(src, "function Player:Update")
    assert update_body, "A-miss fixture must define a Player:Update method"
    assert "GetMouseDelta" in update_body, (
        "GetMouseDelta must be cached in the Update method (the A-miss shape)"
    )
    assert "self.pendingMouse" in update_body, (
        "Update must cache the mouse delta into self.pendingMouse (the split read)"
    )

    # Split read, half 2: Rotate CONSUMES the cached self.pendingMouse and does
    # NOT itself re-read raw GetMouseDelta. A drift where Rotate re-reads raw
    # mouse (or stops consuming the cache) breaks the load-bearing A-miss shape.
    rotate_body = _method_body(src, "function Player:Rotate")
    assert rotate_body, "A-miss fixture must define a Player:Rotate method"
    assert "self.pendingMouse" in rotate_body, (
        "Rotate must CONSUME the cached self.pendingMouse (the split-read shape)"
    )
    assert "GetMouseDelta" not in rotate_body, (
        "Rotate must NOT re-read raw GetMouseDelta — it consumes the Update-cached "
        f"self.pendingMouse instead (the A-miss split read); got Rotate body:\n{rotate_body}"
    )

    # Locomotion: scoped to the Move method body — a rig :PivotTo(, NO Humanoid
    # :Move(. Scoping makes the negative non-brittle (only the move method, not
    # the whole file).
    move_body = _method_body(src, "function Player:Move")
    assert move_body, "A-miss fixture must define a Player:Move method"
    # Drop the method-declaration line so its own ``Player:Move(`` header is not
    # mistaken for a Humanoid ``:Move(`` call.
    move_calls = move_body.split("\n", 1)[1] if "\n" in move_body else ""
    assert ":PivotTo(" in move_calls, (
        "the A-miss move method must drive a rig :PivotTo( locomotion"
    )
    assert ":Move(" not in move_calls, (
        "the A-miss move method must NOT call a Humanoid :Move( (paradigm A "
        f"misses this shape); got move body:\n{move_body}"
    )


# --------------------------------------------------------------------------- #
# AC7 — corpus host-C-dominance (luau-sim; the load-bearing one).
#
# Executes the fixture's REAL competing code (NOT a hand-coded surrogate): loads
# the fixture module under bus-backed mocks, drives its REAL methods mid-frame,
# then the modeled host "C" wins by last-writer-wins — NON-VACUOUSLY (the
# fixture's competing write must have LANDED). Drift in the fixture FAILS this.
#
# The luau skip is scoped to the AC7 class ONLY (decorator below), NOT the
# module: the pure-Python AC6 drift guards above MUST run regardless of luau
# (edge E6).
# --------------------------------------------------------------------------- #


# Bus-backed mock surface (1.2-local). Minimal Roblox-ish ``Vector3`` / ``CFrame``
# / ``Enum`` / ``workspace`` + a ``gameObject`` (``GetPivot`` / ``PivotTo``
# recorder), a ``uis`` (drainable ``GetMouseDelta`` channel + ``IsKeyDown``), and
# a ``humanoid`` (``Move`` recorder) — enough to run the REAL fixture methods.
# Returns a table ``M`` the scenario binds onto the fixture's ``self`` fields.
_MOCKS = textwrap.dedent("""\
    local bus = {cam = {CFrame = "INIT"}, moves = {}, pivots = {}}

    -- Vector3 with real fields + Magnitude/Unit (the fixtures read both).
    local VMT = {}
    local function V3(x, y, z)
        return setmetatable({X = x or 0, Y = y or 0, Z = z or 0}, VMT)
    end
    VMT.__index = function(t, k)
        if k == "Magnitude" then
            return math.sqrt(t.X * t.X + t.Y * t.Y + t.Z * t.Z)
        elseif k == "Unit" then
            return t
        end
        return nil
    end
    VMT.__add = function(a, b) return V3(a.X + b.X, a.Y + b.Y, a.Z + b.Z) end
    VMT.__mul = function(a, s)
        if type(s) == "number" then return V3(a.X * s, a.Y * s, a.Z * s) end
        return a
    end
    local Vector3 = {new = V3, zero = V3(0, 0, 0)}

    -- CFrame: arithmetic returns a CFrame; carries Position + VectorToWorldSpace
    -- (called on the result of gameObject:GetPivot()). ``_angle`` records the
    -- first numeric component a CFrame.Angles call was built from, and CFrame
    -- multiplication PROPAGATES it, so a camera write built from
    -- ``CFrame.Angles(rad(camRotationX), …)`` carries the rotation that drove it
    -- (lets AC7 prove the camera write reflects the Update-CACHED delta).
    local CMT = {}
    local function CF(tag, angle)
        return setmetatable(
            {_tag = tag, _angle = angle or 0, Position = V3(0, 0, 0)}, CMT)
    end
    CMT.__index = function(t, k)
        if k == "VectorToWorldSpace" then
            return function(_self, v) return v end
        end
        return nil
    end
    CMT.__mul = function(a, b)
        -- propagate whichever operand carries a non-zero rotation angle
        local angle = (a._angle ~= 0 and a._angle) or (b._angle or 0)
        return CF("mul", angle)
    end
    CMT.__add = function(a, b) return CF("add", a._angle) end
    CMT.__sub = function(a, b) return CF("sub", a._angle) end
    local CFrame = {
        new = function(...) return CF("new") end,
        Angles = function(x, ...) return CF("ang", x or 0) end,
    }

    local Enum = setmetatable({}, {__index = function(_, k)
        return setmetatable({}, {__index = function(_, kk)
            return tostring(k) .. "." .. tostring(kk)
        end})
    end})

    local workspace = {CurrentCamera = bus.cam}
    local STUDS_PER_METER = 3

    -- gameObject: GetPivot returns a CFrame; PivotTo records the rig move.
    local function newGO()
        local go = {}
        function go:GetPivot() return CF("pivot") end
        function go:PivotTo(cf) table.insert(bus.pivots, cf) end
        return go
    end

    -- uis: a drainable GetMouseDelta channel + IsKeyDown (WASD true so the
    -- locomotion branch runs).
    local mouseQueue = {}
    local function pushMouse(x, y) table.insert(mouseQueue, V3(x, y, 0)) end
    local uis = {}
    function uis:GetMouseDelta()
        return table.remove(mouseQueue, 1) or V3(0, 0, 0)
    end
    function uis:IsKeyDown(code)
        return code == "KeyCode.W" or code == "KeyCode.D"
    end

    -- humanoid: a Move recorder (the dde248 A-hit shape calls it).
    local humanoid = {FloorMaterial = "Material.Grass"}
    function humanoid:Move(dir, rel) table.insert(bus.moves, dir) end
    function humanoid:IsA(c) return c == "Humanoid" end

    return {
        bus = bus, Vector3 = Vector3, CFrame = CFrame, Enum = Enum,
        workspace = workspace, STUDS_PER_METER = STUDS_PER_METER,
        newGO = newGO, uis = uis, humanoid = humanoid, pushMouse = pushMouse,
    }
""")


def _fixture_scenario(fixture_path: Path, drive_body: str) -> str:
    """Build a luau scenario that loads the REAL fixture module under the
    bus-backed mocks (injected as upvalues so the readonly ``_G`` is untouched —
    the standalone-luau constraint slice 1.1 documents) and runs ``drive_body``,
    which sees ``M`` (the mock table), ``bus``, and ``Player`` (the real fixture
    module table)."""
    src = fixture_path.read_text(encoding="utf-8")
    delim = "=="
    while f"]{delim}]" in src or f"[{delim}[" in src:
        delim += "="
    fixture_lit = f"[{delim}[\n{src}\n]{delim}]"
    return textwrap.dedent("""\
        local M = (function()
        {mocks}
        end)()
        local bus = M.bus
        local Vector3, CFrame, Enum, workspace, STUDS_PER_METER =
            M.Vector3, M.CFrame, M.Enum, M.workspace, M.STUDS_PER_METER

        -- Load the REAL fixture as a chunk closing over the injected globals
        -- (passed in as varargs -> the fixture's own _axis/_getAxis helpers come
        -- for free; nothing is stubbed out of the fixture).
        local fixtureSrc = {fixture_lit}
        local PlayerChunk = assert(loadstring(
            "local Vector3, CFrame, Enum, workspace, STUDS_PER_METER = ...\\n"
            .. "return (function() " .. fixtureSrc .. " end)()",
            "fixture"))
        local Player = PlayerChunk(
            Vector3, CFrame, Enum, workspace, STUDS_PER_METER)
        assert(type(Player) == "table", "fixture must return its module table")
    """).format(mocks=_MOCKS, fixture_lit=fixture_lit) + textwrap.dedent(drive_body)


@pytest.mark.skipif(
    not _luau_available(), reason="needs standalone luau interpreter"
)
class TestCorpusCDominance:
    def test_C_dominates_dde248_camera_and_move(self) -> None:
        """AC7 (dde248, A-hit): drive the REAL fixture's ``Rotate`` (camera write)
        and ``Move`` (Humanoid:Move) mid-frame; the camera write LANDS FROM ROTATE
        (sampled BETWEEN Rotate and Move so it is attributed to Rotate
        specifically — codex P1#2) and the Humanoid move is recorded, THEN
        modeled-host C's POST wins BOTH surfaces by last-writer-wins. The
        fixture's actual code competes — not a surrogate; a fixture that stops
        writing the camera in Rotate (or drops the Humanoid:Move) FAILS this."""
        scenario = _fixture_scenario(_DDE248, """\
            local go = M.newGO()
            local player = setmetatable({}, Player)
            player.gameObject = go
            player.uis = M.uis
            player.cam = M.workspace.CurrentCamera   -- dde248 writes CurrentCamera
            player.control = M.humanoid
            player.sensitivity = 0.2
            player.camRotation = Vector3.new(0, 0, 0)
            player.minAngle = -80
            player.maxAngle = 80
            player.moveDirection = Vector3.new(0, 0, 0)
            M.pushMouse(5, 2)   -- a mouse delta so Rotate writes the camera

            -- Mid-frame competing writes: the fixture's REAL methods. Sample the
            -- camera cell BETWEEN Rotate and Move so the camera write is
            -- attributed to Rotate SPECIFICALLY (a drift moving the camera write
            -- out of Rotate into Move would leave afterRotate at INIT and FAIL),
            -- mirroring the cold3a59 pivot-from-Move attribution / AC3 afterFire.
            player:Rotate(0.016)
            local afterRotate = bus.cam.CFrame   -- Rotate's camera write
            player:Move(0.016)
            local camLanded = (type(afterRotate) == "table")  -- a real CFrame from Rotate
            local moveCount = #bus.moves

            -- Host C POST bracket (OUTSIDE any component pass): C wins both.
            bus.cam.CFrame = "POST"
            M.humanoid:Move("POST")

            print("camLanded=" .. tostring(camLanded))
            print("moveCount=" .. tostring(moveCount))
            print("finalCam=" .. tostring(bus.cam.CFrame))
            print("finalMove=" .. tostring(bus.moves[#bus.moves]))
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        # Non-vacuity: the fixture's REAL camera write landed FROM ROTATE (a
        # CFrame sampled BETWEEN Rotate and Move, not the INIT string) and its
        # Humanoid:Move was recorded. Sampling between Rotate and Move attributes
        # the camera write to Rotate specifically — a drift moving it into Move
        # would leave afterRotate at INIT and FAIL.
        assert "camLanded=true" in out, (
            f"fixture's REAL Rotate must write a CFrame to the camera "
            f"(attributed to Rotate, sampled before Move); got {out!r}"
        )
        assert "moveCount=1" in out, (
            f"fixture's REAL Move must record a Humanoid:Move; got {out!r}"
        )
        # Last-writer-wins: host C wins BOTH surfaces.
        assert "finalCam=POST" in out, f"host C must win the camera; got {out!r}"
        assert "finalMove=POST" in out, (
            f"host C must win the Humanoid move-intent; got {out!r}"
        )

    def test_C_dominates_cold3a59_camera_pivot_benign(self) -> None:
        """AC7 (cold3a59, A-miss): drive the REAL fixture's ``Update`` (cache
        GetMouseDelta into self.pendingMouse), ``Rotate`` (CONSUME the cache +
        camera write), and ``Move`` (rig PivotTo) mid-frame. The camera write
        LANDS and >=1 rig PivotTo fires, and NO Humanoid:Move occurs (the A-miss
        shape makes none — only the camera C actor is exercised). THEN host C's
        camera POST wins; the PivotTo drift is present-but-not-contesting-the-
        camera (benign, deferred to Phase 3 U1).

        SPLIT-READ non-vacuity (codex P1#2): the fixture caches a NON-ZERO mouse
        delta in Update and consumes the cache in Rotate. We push EXACTLY ONE
        mouse delta, so by the time Rotate runs the raw GetMouseDelta queue is
        DRAINED (returns zero) — if Rotate re-read raw mouse it would see zero
        and the camera angle would NOT reflect the Update-cached delta. We prove
        the cache is LOAD-BEARING for the camera result by running two variants:
          * variant A — pendingMouse left as Update cached it -> camera angle
            reflects the cached delta (NON-ZERO);
          * variant B — pendingMouse CLEARED to zero just before Rotate -> camera
            angle is ZERO.
        If Rotate ignored pendingMouse (re-read raw / used a constant), both
        variants would yield the SAME angle and this test FAILS — exactly the
        drift codex P1#2 requires us to catch."""
        scenario = _fixture_scenario(_COLD3A59, """\
            -- A fresh player bound to the shared mock surface. pushDelta controls
            -- whether a non-zero raw mouse delta is queued for Update to cache;
            -- clearCache wipes pendingMouse right before Rotate (variant B).
            local function runFrame(pushDelta, clearCache)
                local go = M.newGO()
                local player = setmetatable({}, Player)
                player.gameObject = go
                player.uis = M.uis
                player.cam = M.workspace.CurrentCamera
                player.control = M.humanoid
                player.sensitivity = 0.2
                player.camRotationX = 0
                player.minAngle = -80
                player.maxAngle = 80
                player.speed = 5
                player.pendingMouse = Vector3.new(0, 0, 0)
                if pushDelta then M.pushMouse(5, 2) end

                -- Split read: Update caches the (drained-once) raw delta; Rotate
                -- CONSUMES the cache. Exactly one delta is queued, so a re-read of
                -- raw mouse in Rotate would get ZERO.
                player:Update(0.016)
                if clearCache then player.pendingMouse = Vector3.new(0, 0, 0) end
                player:Rotate(0.016)
                return player
            end

            -- Variant A: cache consumed (pendingMouse non-zero from Update).
            local pA = runFrame(true, false)
            local camAngleA = bus.cam.CFrame._angle
            local camLanded = (type(bus.cam.CFrame) == "table")
            local pivotsAfterRotate = #bus.pivots

            -- Drive Move on variant A (rig PivotTo; NO Humanoid:Move).
            local pivotsBeforeMove = #bus.pivots
            pA:Move(0.016)
            local pivotsFromMove = #bus.pivots - pivotsBeforeMove
            local humanoidMoves = #bus.moves

            -- Variant B: pendingMouse cleared before Rotate -> if Rotate consumes
            -- the cache, the camera angle is ZERO (different from variant A).
            local pB = runFrame(true, true)
            local camAngleB = bus.cam.CFrame._angle

            -- Host C POST: ONLY the camera (no Humanoid:Move actor for this shape).
            bus.cam.CFrame = "POST"

            print("camLanded=" .. tostring(camLanded))
            print("moveContributedPivot=" .. tostring(pivotsFromMove >= 1))
            print("humanoidMoves=" .. tostring(humanoidMoves))
            print("cacheConsumed=" ..
                tostring(camAngleA ~= 0 and camAngleB == 0))
            print("camAngleA=" .. tostring(camAngleA))
            print("camAngleB=" .. tostring(camAngleB))
            print("finalCam=" .. tostring(bus.cam.CFrame))
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        # Non-vacuity: the fixture's REAL camera write landed and its rig PivotTo
        # fired (attributed to Move specifically — Rotate also PivotTos).
        assert "camLanded=true" in out, (
            f"fixture's REAL Rotate must write a CFrame to the camera; got {out!r}"
        )
        assert "moveContributedPivot=true" in out, (
            f"the fixture's REAL Move must contribute a rig PivotTo (attributed to "
            f"Move specifically, since Rotate also PivotTos); got {out!r}"
        )
        # The A-miss shape makes NO Humanoid:Move (so we exercise no move actor).
        assert "humanoidMoves=0" in out, (
            f"the A-miss shape must make NO Humanoid:Move; got {out!r}"
        )
        # Split-read consume proof: the camera angle reflects the Update-CACHED
        # delta (non-zero with cache, zero when cache cleared before Rotate). A
        # Rotate that re-read raw mouse / ignored pendingMouse would give the same
        # angle in both variants -> cacheConsumed=false -> FAIL (codex P1#2).
        assert "cacheConsumed=true" in out, (
            f"Rotate must CONSUME the Update-cached self.pendingMouse: the camera "
            f"angle must reflect the cached delta (non-zero) and collapse to zero "
            f"when the cache is cleared before Rotate; got {out!r}"
        )
        # Last-writer-wins on the camera: host C wins. The PivotTo drift is
        # present but did NOT touch the camera cell — benign/vestigial,
        # deferred to Phase 3 U1.
        assert "finalCam=POST" in out, f"host C must win the camera; got {out!r}"
