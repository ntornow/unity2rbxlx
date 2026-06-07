"""Phase 1 (redesign) slice 1.1: pin the REAL ``_tick``
cross-pass-visibility substrate in ``converter/runtime/scene_runtime.luau``.

This is the substrate the Phase-2 camera authority rides on: the host
runs, per heartbeat, an UNORDERED Update pass, a fixed-step FixedUpdate
pass, then an UNORDERED LateUpdate pass (``scene_runtime.luau:_tick``).
The Phase-2 host-player will write ``CurrentCamera`` *around* these
passes (pre-Update / post-LateUpdate). These tests pin the cross-pass
guarantees that property depends on, using only primitives that EXIST at
baseRef — they boot NO client ``Player.luau`` and touch NO product code.

Reuses ``_run_scenario`` / ``_luau_available`` from the existing host
harness (no copy; the two-component plan shape mirrors
``TestTwoMonoBehaviourScene``). Skips cleanly when ``luau`` is absent.

GREEN substrate pins:
  * a value component A writes in its Update is visible to component B's
    LateUpdate in the SAME frame (cross-pass intra-frame visibility);
  * every component's Update for a frame precedes every component's
    LateUpdate for that frame (cross-pass ordering, independent of the
    intra-pass ``pairs`` hash order);
  * the cross-pass write is re-published each frame, not leaked across
    frames (the substrate is per-frame, the Phase-2 camera write target);
  * FixedUpdate fires on the fixed-step accumulator between the Update and
    LateUpdate passes, not per heartbeat.
"""

from __future__ import annotations

import textwrap

from tests.test_scene_runtime_host_behavior import (
    _luau_available,
    _run_scenario,
)

import pytest

pytestmark = pytest.mark.skipif(
    not _luau_available(),
    reason="needs standalone luau interpreter",
)


# A two-component plan in the shape of ``TestTwoMonoBehaviourScene``:
# ``Writer`` (instance A:1, go1) and ``Reader`` (instance A:2, go2),
# both runtime-bearing, same domain, authored ``lifecycle_order`` A:1<A:2.
# The scenario body wires module behaviour per test; this builds the plan
# + services + engine boilerplate identically across tests.
def _two_component_preamble(writer_methods: str, reader_methods: str) -> str:
    return textwrap.dedent("""\
        -- ``_G`` is readonly under standalone luau; use a shared local
        -- table the component methods (same chunk) close over as the
        -- cross-pass bus / event log.
        local bus = {{}}

        local Writer = {{}} ; Writer.__index = Writer
        function Writer.new(_) return setmetatable({{}}, Writer) end
        {writer_methods}

        local Reader = {{}} ; Reader.__index = Reader
        function Reader.new(_) return setmetatable({{}}, Reader) end
        {reader_methods}

        local plan = {{
            modules = {{
                writer = {{stem = "Writer", runtime_bearing = true,
                           module_path = "x"}},
                reader = {{stem = "Reader", runtime_bearing = true,
                           module_path = "y"}},
            }},
            scenes = {{
                A = {{
                    instances = {{
                        {{instance_id = "A:1", script_id = "writer",
                          game_object_id = "go1", active = true,
                          enabled = true, config = {{}}}},
                        {{instance_id = "A:2", script_id = "reader",
                          game_object_id = "go2", active = true,
                          enabled = true, config = {{}}}},
                    }},
                    references = {{}},
                    lifecycle_order = {{"A:1", "A:2"}},
                }},
            }},
            prefabs = {{}},
            domain_overrides = {{}},
        }}
        local services = servicesFor(plan, {{writer = Writer, reader = Reader}}, {{
            go1 = {{Name = "Go1", _sceneRuntimeId = "go1", _children = {{}}}},
            go2 = {{Name = "Go2", _sceneRuntimeId = "go2", _children = {{}}}},
        }})
        local engine = SceneRuntime.new(services, plan)
        engine:start(nil)
        runDeferred()  -- flush Start so the next heartbeat runs Update/LateUpdate
    """).format(writer_methods=writer_methods, reader_methods=reader_methods)


class TestCrossPassVisibility:
    """The Phase-2 camera authority writes a value (the look CFrame) during
    one pass and a later pass reads it in the SAME frame. Pin that
    cross-pass intra-frame visibility on the real ``_tick``: a shared
    bus written by Writer.Update is read by Reader.LateUpdate same frame."""

    def test_update_write_visible_to_lateupdate_same_frame(self):
        scenario = _two_component_preamble(
            writer_methods=(
                "function Writer:Update(dt) bus.value = 'written-in-update' end"
            ),
            reader_methods=(
                "function Reader:LateUpdate(dt)\n"
                "    bus.seenByLate = bus.value\n"
                "end"
            ),
        ) + textwrap.dedent("""\
            bus.value = nil
            bus.seenByLate = "UNSET"
            services.heartbeat:fire(0.016)  -- one frame: Update then LateUpdate
            print("seenByLate=" .. tostring(bus.seenByLate))
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "seenByLate=written-in-update" in out, (
            "a value written in Update must be visible to LateUpdate in the "
            f"same frame; got {out!r}"
        )


class TestCrossPassOrdering:
    """Every component's Update for frame N precedes every component's
    LateUpdate for frame N — independent of the intra-pass ``pairs`` hash
    order. This is the pre/post-pass boundary the Phase-2 host-player
    writes the camera around (pre-Update read, post-LateUpdate write)."""

    def test_all_updates_precede_all_lateupdates_in_one_frame(self):
        # Both components log into a shared sequence from BOTH passes. The
        # assertion is cross-pass (all *.Update before all *.LateUpdate),
        # so it holds whatever order ``pairs`` visits the two components.
        scenario = _two_component_preamble(
            writer_methods=(
                "function Writer:Update(dt) table.insert(bus.seq, 'W.Update') end\n"
                "function Writer:LateUpdate(dt) "
                "table.insert(bus.seq, 'W.Late') end"
            ),
            reader_methods=(
                "function Reader:Update(dt) table.insert(bus.seq, 'R.Update') end\n"
                "function Reader:LateUpdate(dt) "
                "table.insert(bus.seq, 'R.Late') end"
            ),
        ) + textwrap.dedent("""\
            bus.seq = {}
            services.heartbeat:fire(0.016)
            for _, x in ipairs(bus.seq) do print(x) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        events = [l for l in lines if l.endswith(".Update") or l.endswith(".Late")]
        # Both components fired in both passes (4 events total).
        assert sorted(events) == ["R.Late", "R.Update", "W.Late", "W.Update"], (
            f"expected both components in both passes; got {events}"
        )
        last_update = max(i for i, e in enumerate(events) if e.endswith(".Update"))
        first_late = min(i for i, e in enumerate(events) if e.endswith(".Late"))
        assert last_update < first_late, (
            "every Update must precede every LateUpdate in the frame, "
            f"independent of hash order; got {events}"
        )


class TestPerFrameRepublish:
    """The cross-pass value is republished each frame, not stale-carried.
    The Phase-2 camera write target is per-frame; a second frame must see
    the second frame's Update write in its LateUpdate, not the first."""

    def test_lateupdate_reads_current_frame_write_not_prior(self):
        scenario = _two_component_preamble(
            writer_methods=(
                "function Writer:Update(dt)\n"
                "    bus.frame = (bus.frame or 0) + 1\n"
                "    bus.value = bus.frame\n"
                "end"
            ),
            reader_methods=(
                "function Reader:LateUpdate(dt)\n"
                "    table.insert(bus.seenByLate, bus.value)\n"
                "end"
            ),
        ) + textwrap.dedent("""\
            bus.frame = 0
            bus.value = nil
            bus.seenByLate = {}
            services.heartbeat:fire(0.016)
            services.heartbeat:fire(0.016)
            print("f1=" .. tostring(bus.seenByLate[1]))
            print("f2=" .. tostring(bus.seenByLate[2]))
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "f1=1" in out, f"first frame LateUpdate must see frame-1 write; {out!r}"
        assert "f2=2" in out, (
            "second frame LateUpdate must see the second frame's write, "
            f"not the stale prior value; got {out!r}"
        )


class TestFixedUpdateBetweenPasses:
    """FixedUpdate fires on the fixed-step accumulator (default 0.02s),
    between the Update and LateUpdate passes — not once per heartbeat.
    Pins the third pass of the substrate so the Phase-2 authority can rely
    on the Update -> FixedUpdate* -> LateUpdate frame structure."""

    def test_fixed_update_fires_on_accumulator_between_passes(self):
        scenario = _two_component_preamble(
            writer_methods=(
                "function Writer:Update(dt) "
                "table.insert(bus.seq, 'Update') end\n"
                "function Writer:FixedUpdate(dt) "
                "table.insert(bus.seq, 'Fixed') end\n"
                "function Writer:LateUpdate(dt) "
                "table.insert(bus.seq, 'Late') end"
            ),
            reader_methods="",
        ) + textwrap.dedent("""\
            bus.seq = {}
            -- fixedStep is 0.02 (servicesFor default). One 0.05s heartbeat
            -- accumulates 2 fixed steps; Update + LateUpdate fire once.
            services.heartbeat:fire(0.05)
            for _, x in ipairs(bus.seq) do print(x) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        seq = [l for l in lines if l in ("Update", "Fixed", "Late")]
        assert seq.count("Update") == 1, f"Update fires once per heartbeat; {seq}"
        assert seq.count("Late") == 1, f"LateUpdate fires once per heartbeat; {seq}"
        # 0.05 / 0.02 = 2 fixed steps on the accumulator.
        assert seq.count("Fixed") == 2, (
            f"FixedUpdate fires on the fixed-step accumulator (2 steps); got {seq}"
        )
        # Frame structure: Update pass, then FixedUpdate pass(es), then
        # LateUpdate pass.
        assert seq.index("Update") < seq.index("Fixed"), seq
        assert seq.index("Fixed") < seq.index("Late"), seq


# --------------------------------------------------------------------------- #
# Phase 1 slice 1.1 primitives (b)/(c): the host's pre-Update / post-LateUpdate
# camera + Humanoid-move bracket dominates a competing mid-Update component
# write by last-writer-wins. The shared-mock surface (``bus.cam`` /
# ``bus.humanoid`` / ``bus.moves``) is declared in the SCENARIO BODY appended
# after ``_two_component_preamble`` (NOT inside its format template, to avoid
# brace-escaping); the component method strings close over ``bus`` (design §1.2).
# This surface is tick-substrate-internal — it serves AC2-AC4 here; slice 1.2's
# corpus test is SELF-CONTAINED and does NOT consume it (design §2.5).
# --------------------------------------------------------------------------- #


class TestHostCameraBracketDominates:
    """The host writes ``bus.cam.CFrame`` BEFORE and AFTER the component pass
    (scenario luau OUTSIDE ``heartbeat:fire`` — deterministic by source order,
    edge E2). A pre-write must survive to a mid-Update raw read; a post-write
    must win the frame over a competing mid-Update camera write."""

    def test_prewrite_survives_to_mid_update_read(self):
        """AC2 / primitive (b)-pre: a host pre-write is visible to a mid-Update
        component read (the same-frame raw ``self.cam.CFrame`` read pattern)."""
        scenario = _two_component_preamble(
            writer_methods=(
                "function Writer:Update(dt) bus.seenInUpdate = bus.cam.CFrame end"
            ),
            reader_methods="",
        ) + textwrap.dedent("""\
            bus.cam = {CFrame = "INIT"}
            bus.seenInUpdate = "UNSET"
            bus.cam.CFrame = "PRE"        -- host pre-Update write
            services.heartbeat:fire(0.016)
            print("seenInUpdate=" .. tostring(bus.seenInUpdate))
            print("final=" .. tostring(bus.cam.CFrame))
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        # The mid-Update component read the host's pre-write (non-vacuous: not
        # the stale INIT/UNSET value).
        assert "seenInUpdate=PRE" in out, (
            f"mid-Update read must see the host pre-write; got {out!r}"
        )

    def test_postwrite_wins_over_mid_update_camera_write(self):
        """AC3 / primitive (b)-post: a host post-LateUpdate write wins the frame
        over a competing mid-Update component camera write (non-vacuous: the mid
        write actually ran — edge E5)."""
        scenario = _two_component_preamble(
            writer_methods=(
                "function Writer:Update(dt)\n"
                "    bus.cam.CFrame = 'MID'   -- competing mid-Update write\n"
                "    bus.midRan = true\n"
                "end"
            ),
            reader_methods="",
        ) + textwrap.dedent("""\
            bus.cam = {CFrame = "INIT"}
            bus.midRan = false
            services.heartbeat:fire(0.016)
            local afterFire = bus.cam.CFrame   -- competing mid-write landed here
            bus.cam.CFrame = "POST"        -- host post-LateUpdate write
            print("midRan=" .. tostring(bus.midRan))
            print("afterFire=" .. tostring(afterFire))
            print("final=" .. tostring(bus.cam.CFrame))
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        # Non-vacuity: the competing mid-Update write actually ran.
        assert "midRan=true" in out, f"competing mid write must run; {out!r}"
        # Non-vacuity (stronger): the competing write actually landed in the
        # shared camera cell before the host overwrote it.
        assert "afterFire=MID" in out, (
            f"mid-Update camera write must land in bus.cam.CFrame; got {out!r}"
        )
        # Last-writer-wins: the host post-write is the final value.
        assert "final=POST" in out, (
            f"host post-LateUpdate write must win the frame; got {out!r}"
        )


class TestHostHumanoidMoveBracketDominates:
    """A mock Humanoid records every ``:Move(v)``. A component calls
    ``bus.humanoid:Move(MID)`` in Update; the host calls
    ``bus.humanoid:Move(POST)`` after the LateUpdate pass. The host's
    post-component move is the last move-intent for the frame (non-vacuous: the
    mid move was recorded first — edge E5)."""

    def test_post_component_humanoid_move_overrides_mid_update_move(self):
        """AC4 / primitive (c): post-component Humanoid:Move overrides the
        mid-Update Move; ``bus.moves`` last == POST and MID was recorded first."""
        scenario = _two_component_preamble(
            writer_methods=(
                "function Writer:Update(dt) bus.humanoid:Move('MID') end"
            ),
            reader_methods="",
        ) + textwrap.dedent("""\
            bus.moves = {}
            bus.humanoid = {Move = function(self, v) table.insert(bus.moves, v) end}
            services.heartbeat:fire(0.016)
            bus.humanoid:Move("POST")      -- host post-LateUpdate move
            for i, v in ipairs(bus.moves) do
                print(string.format("MOVE%d=%s", i, tostring(v)))
            end
            print("LAST=" .. tostring(bus.moves[#bus.moves]))
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        # Non-vacuity: the mid-Update move was recorded first.
        assert "MOVE1=MID" in out, f"mid-Update Move must run first; {out!r}"
        # Last-call-wins: the host's post-component move is the final intent.
        assert "LAST=POST" in out, (
            f"host post-component Move must be the last move-intent; got {out!r}"
        )
