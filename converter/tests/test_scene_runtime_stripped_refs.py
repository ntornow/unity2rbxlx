"""Phase 3 / slice 3.3: runtime load-order back-patch for stripped-prefab-instance
component refs (the ``LoadoutState.missionPopup`` repro).

These tests drive the REAL host runtime (``converter/runtime/scene_runtime.luau``)
through the standalone ``luau`` interpreter, reusing the shared harness from
``test_scene_runtime_host_behavior.py`` (preamble + ``servicesFor`` mock surface,
``_run_scenario``). The real ``_wireReferences`` / ``_drainPendingPlacementRefs``
functions execute -- nothing is stubbed.

Acceptance (design-phase3.md §4, slice 3.3):
  * AC3 -- a scene component ref to a stripped MB on a PLACED prefab instance
    (``target_kind="component"``, ``target_ref="<placement>:<prefab>:<src_fid>"``)
    is nil during scene-ref wiring (pre-placement) and bound to the cloned
    component AFTER the placement-boot drain. RED-proven: a pre-fix variant with
    NO drain leaves the field nil.
  * AC4 -- a normal component ref resolves synchronously, no pending record;
    ``_pendingPlacementRefs`` nil/empty for a plan with no stripped refs.
  * AC5 -- a stripped ref whose target never registers stays nil + WARN, no crash.
  * AC7 -- a cross-domain (client->server) stripped ref is nil-injected at wire
    time, NOT queued into ANY back-patch list, and STAYS nil after the drain runs,
    with a cross-domain edge recorded. RED-proven: a variant that queues policy-nil'd
    refs would rebind it after the drain. Also: real client->client refs ARE bound.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

# Reuse the shared luau harness (preamble, servicesFor, _run_scenario) and the
# luau-availability skip from the sibling host-behavior test module. conftest
# adds the converter root to sys.path but not the tests dir, so add it here for
# the sibling import.
sys.path.insert(0, str(Path(__file__).parent))
from test_scene_runtime_host_behavior import _run_scenario, pytestmark  # noqa: E402,F401


# ---------------------------------------------------------------------------
# AC3 -- the repro fix: scene ref to a placed-prefab stripped MB binds after
# the placement-boot drain.
# ---------------------------------------------------------------------------

class TestStrippedRefBindsAfterPlacementDrain:

    def _plan_scenario(self, *, expect_bound: bool, drive_drain: bool) -> str:
        # ``drive_drain`` mirrors the production call: start() runs the
        # placement-boot loop then calls _drainPendingPlacementRefs. The RED
        # variant models the PRE-FIX runtime by shadowing the drain method with
        # a no-op on the instance (an instance field shadows the metatable
        # method) BEFORE start() runs -- so the drain calls inside start() /
        # _completeDeferredBatch do nothing, exactly as if the drain step never
        # existed. The SAME real start()/_wireReferences run either way; only
        # the drain is disabled. The field then stays nil end-to-end.
        red_clear = (
            "engine._drainPendingPlacementRefs = function() end  -- pre-fix: no drain\n"
            if not drive_drain else ""
        )
        # The engine-union key the placement boot registers under is
        # ``<placement_id>:<inst.instance_id>``; inst.instance_id is
        # ``<prefab_id>:<src_fid>``. The scene ref's resolved target_ref equals
        # that full key (what slice 3.2's planner emits).
        return textwrap.dedent("""\
            local boundField = "UNREAD"
            local fieldDuringWire = "UNREAD"

            -- Source: a scene MonoBehaviour (LoadoutState) holding the ref.
            local Loadout = {} ; Loadout.__index = Loadout
            function Loadout.new(_) return setmetatable({}, Loadout) end
            function Loadout:Awake()
                -- Scene refs wire BEFORE Awake; the placement hasn't booted yet
                -- so the field is still nil here (the inherent ordering limit
                -- for Awake; Start sees the bound value).
                fieldDuringWire = self.missionPopup
            end
            function Loadout:Start()
                boundField = self.missionPopup
            end

            -- Target: a stripped MissionUI on a placed prefab instance.
            local MissionUI = {} ; MissionUI.__index = MissionUI
            function MissionUI.new(_) return setmetatable({mark="MUI"}, MissionUI) end

            local PLACEMENT = "Main:1822972501"
            local PREFAB = "guidA:Assets/Prefabs/UI/MissionPopup.prefab"
            local SRC_FID = "114000011972273750"
            local PREFAB_LOCAL = PREFAB .. ":" .. SRC_FID            -- inst.instance_id
            local ENGINE_KEY = PLACEMENT .. ":" .. PREFAB_LOCAL      -- target_ref

            local plan = {
                modules = {
                    loadout = {stem = "Loadout", runtime_bearing = true,
                               module_path = "x", domain = "client"},
                    missionui = {stem = "MissionUI", runtime_bearing = true,
                                 module_path = "y", domain = "client"},
                },
                scenes = {
                    Main = {
                        instances = {
                            {instance_id = "Main:869760749", script_id = "loadout",
                             game_object_id = "sgo", active = true,
                             enabled = true, config = {}},
                        },
                        references = {
                            {["from"] = "Main:869760749", field = "missionPopup",
                             index = nil, target_kind = "component",
                             target_ref = ENGINE_KEY,
                             target_script_id = "missionui",
                             target_is_ui = false},
                        },
                        lifecycle_order = {"Main:869760749"},
                    },
                },
                prefabs = {
                    [PREFAB] = {
                        name = "MissionPopup",
                        instances = {{instance_id = PREFAB_LOCAL,
                                      script_id = "missionui",
                                      game_object_id = PREFAB_LOCAL,
                                      active = true, enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {PREFAB_LOCAL},
                    },
                },
                scene_prefab_placements = {
                    {placement_id = PLACEMENT, prefab_id = PREFAB,
                     active = true, enabled = true},
                },
                domain_overrides = {},
            }
            -- ``sgo`` is the scene source GO; the placed clone descendant is
            -- found by the boot path via workspaceFind keyed on the namespaced
            -- prefab-local id (``<placement>:<prefab>:<src_fid>``).
            local instances = {
                sgo = {Name = "Loadout", _sceneRuntimeId = "sgo", _children = {}},
                [ENGINE_KEY] = {Name = "MissionPopup", _sceneRuntimeId = ENGINE_KEY,
                                _children = {}},
            }
            local services = servicesFor(plan, {loadout = Loadout, missionui = MissionUI}, instances)
            local engine = SceneRuntime.new(services, plan)
            __RED_CLEAR__
            engine:start("client")
            runDeferred()  -- flush Start

            assert(fieldDuringWire == nil,
                "field must be nil during scene wire (pre-placement); got " ..
                tostring(fieldDuringWire))
            print("WIRE_NIL_OK")
            if boundField == nil then
                print("FIELD_NIL")
            elseif type(boundField) == "table" and boundField.mark == "MUI" then
                print("FIELD_BOUND")
            else
                print("FIELD_OTHER:" .. tostring(boundField))
            end
            print("DONE")
        """).replace("__RED_CLEAR__", red_clear)

    def test_field_binds_to_cloned_component_after_drain(self):
        # GREEN: real drain binds the field to the placed clone.
        rc, out, err = _run_scenario(
            self._plan_scenario(expect_bound=True, drive_drain=True))
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "WIRE_NIL_OK" in out, out
        assert "FIELD_BOUND" in out, f"field must bind after drain; got:\n{out}"

    def test_red_pre_fix_no_drain_leaves_field_nil(self):
        # RED: without the drain step (pre-fix runtime) the field stays nil.
        rc, out, err = _run_scenario(
            self._plan_scenario(expect_bound=False, drive_drain=False))
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "WIRE_NIL_OK" in out, out
        assert "FIELD_NIL" in out, (
            f"pre-fix (no drain) must leave field nil (RED proof); got:\n{out}")


# ---------------------------------------------------------------------------
# AC7 -- cross-domain stripped ref: nil-injected, NOT queued, stays nil
# through the drain, with the 3-real-client->client case still binding.
# ---------------------------------------------------------------------------

class TestCrossDomainStrippedRefHoldsThroughDrain:

    def _scenario(self, *, queue_policy_nils: bool) -> str:
        # ``queue_policy_nils`` simulates the PRE-FIX design where a policy
        # nil'd ref WAS queued: we set the crossDomainNil gate to a no-op by
        # NOT gating the pending record. We can't toggle the production gate
        # from Lua, so the RED variant manually injects the cross-domain rec
        # into _pendingPlacementRefs after start() to prove that IF such a rec
        # existed, the drain WOULD rebind it -- which is exactly what the gate
        # prevents.
        red_inject = textwrap.dedent("""\
            -- RED variant: pre-fix design would have queued the policy-nil'd
            -- ref. Inject it manually, then re-drain: the drain rebinds it
            -- (which is exactly what the NOT-cross-domain queue gate prevents).
            engine._pendingPlacementRefs = {
                {source = srcComp, field = "peer", index = nil,
                 target_ref = "Srv:9:pfbS:222"},
            }
            engine:_drainPendingPlacementRefs()
        """) if queue_policy_nils else ""
        return textmod_dedent_scenario(red_inject)

    def test_cross_domain_stays_nil_and_not_queued(self):
        scenario = self._scenario(queue_policy_nils=False)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "INJECT_NIL_OK" in out, f"cross-domain ref must inject nil; got:\n{out}"
        assert "NOT_QUEUED_OK" in out, (
            f"cross-domain ref must NOT be queued in any back-patch list; got:\n{out}")
        assert "STAYS_NIL_AFTER_DRAIN" in out, (
            f"cross-domain ref must stay nil after drain; got:\n{out}")
        assert "EDGE_RECORDED" in out, f"cross-domain edge must be recorded; got:\n{out}"
        assert "SAME_DOMAIN_BOUND" in out, (
            f"a real client->client placed ref must still bind; got:\n{out}")

    def test_red_queueing_policy_nils_would_rebind(self):
        scenario = self._scenario(queue_policy_nils=True)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        # The RED variant proves the gate is load-bearing: if a policy-nil'd
        # ref WERE queued, the drain rebinds it.
        assert "RED_REBOUND" in out, (
            f"RED proof: queueing a policy-nil'd ref must rebind after drain; "
            f"got:\n{out}")


def textmod_dedent_scenario(red_inject: str) -> str:
    # Cross-domain client->server stripped ref (peer) plus a same-domain
    # client->client stripped ref (peerOk) on the SAME scene source. Both
    # targets are placed prefabs. The cross-domain one must inject nil + record
    # an edge + NOT queue; the same-domain one must queue + bind on drain.
    return textwrap.dedent("""\
        local Src = {} ; Src.__index = Src
        function Src.new(_) return setmetatable({}, Src) end
        local Srv = {} ; Srv.__index = Srv
        function Srv.new(_) return setmetatable({mark="SRV"}, Srv) end
        local Cli = {} ; Cli.__index = Cli
        function Cli.new(_) return setmetatable({mark="CLI"}, Cli) end

        local plan = {
            modules = {
                src = {stem = "Src", runtime_bearing = true,
                       module_path = "x", domain = "client"},
                srv = {stem = "Srv", runtime_bearing = true,
                       module_path = "y", domain = "server"},
                cli = {stem = "Cli", runtime_bearing = true,
                       module_path = "z", domain = "client"},
            },
            scenes = {
                Main = {
                    instances = {
                        {instance_id = "Main:1", script_id = "src",
                         game_object_id = "sgo", active = true,
                         enabled = true, config = {}},
                    },
                    references = {
                        -- cross-domain (client source -> server target prefab)
                        {["from"] = "Main:1", field = "peer", index = nil,
                         target_kind = "component", target_ref = "Srv:9:pfbS:222",
                         target_script_id = "srv", target_is_ui = false},
                        -- same-domain (client source -> client target prefab)
                        {["from"] = "Main:1", field = "peerOk", index = nil,
                         target_kind = "component", target_ref = "Cli:8:pfbC:333",
                         target_script_id = "cli", target_is_ui = false},
                    },
                    lifecycle_order = {"Main:1"},
                },
            },
            prefabs = {
                ["pfbS"] = {
                    name = "Srv",
                    instances = {{instance_id = "pfbS:222", script_id = "srv",
                                  game_object_id = "pfbS:222", active = true,
                                  enabled = true, config = {}}},
                    references = {}, lifecycle_order = {"pfbS:222"},
                },
                ["pfbC"] = {
                    name = "Cli",
                    instances = {{instance_id = "pfbC:333", script_id = "cli",
                                  game_object_id = "pfbC:333", active = true,
                                  enabled = true, config = {}}},
                    references = {}, lifecycle_order = {"pfbC:333"},
                },
            },
            scene_prefab_placements = {
                {placement_id = "Srv:9", prefab_id = "pfbS",
                 active = true, enabled = true},
                {placement_id = "Cli:8", prefab_id = "pfbC",
                 active = true, enabled = true},
            },
            domain_overrides = {},
        }
        local instances = {
            sgo = {Name = "Src", _sceneRuntimeId = "sgo", _children = {}},
            ["Srv:9:pfbS:222"] = {Name = "Srv", _sceneRuntimeId = "Srv:9:pfbS:222",
                                  _children = {}},
            ["Cli:8:pfbC:333"] = {Name = "Cli", _sceneRuntimeId = "Cli:8:pfbC:333",
                                  _children = {}},
        }
        local services = servicesFor(plan, {src = Src, srv = Srv, cli = Cli}, instances)
        local engine = SceneRuntime.new(services, plan)
        -- Run BOTH domains so the server target IS constructed/registered
        -- locally -- the drain therefore COULD rebind it if it were queued.
        -- This is the strong form of the gate test: even with the placed
        -- server component present in _componentByInstanceId, the gate holds.
        local edges = engine:start(nil)
        runDeferred()

        local srcComp = engine._componentByInstanceId["Main:1"]
        assert(srcComp ~= nil, "source component must exist")

        -- (a) cross-domain ref injected nil at wire time.
        if srcComp.peer == nil then print("INJECT_NIL_OK") end

        -- (b) NOT queued into ANY back-patch list.
        local queuedCross = false
        for _, rec in ipairs(engine._pendingPlacementRefs or {}) do
            if rec.field == "peer" then queuedCross = true end
        end
        for _, rec in ipairs(engine._inboundRefsToDeferred or {}) do
            if rec.field == "peer" then queuedCross = true end
        end
        if not queuedCross then print("NOT_QUEUED_OK") end

        -- (c) STAYS nil after the drain runs (start() already drained; drain
        -- again to be explicit -- idempotent + the placed server comp exists).
        engine:_drainPendingPlacementRefs()
        if srcComp.peer == nil then print("STAYS_NIL_AFTER_DRAIN") end

        -- edge recorded for the cross-domain ref.
        local edgeOk = false
        for _, e in ipairs(edges) do
            if e.field == "peer" and e.from_domain == "client"
               and e.to_domain == "server" then edgeOk = true end
        end
        if edgeOk then print("EDGE_RECORDED") end

        -- the same-domain client->client placed ref IS bound by the drain.
        if type(srcComp.peerOk) == "table" and srcComp.peerOk.mark == "CLI" then
            print("SAME_DOMAIN_BOUND")
        end

        __RED_INJECT__

        -- RED assertion: if the policy-nil'd ref had been queued, the manual
        -- inject + re-drain above rebinds it.
        if srcComp.peer ~= nil and srcComp.peer.mark == "SRV" then
            print("RED_REBOUND")
        end
        print("DONE")
    """).replace("__RED_INJECT__", red_inject)


# ---------------------------------------------------------------------------
# AC4 -- normal component refs: synchronous resolve, no pending record.
# ---------------------------------------------------------------------------

class TestNoRegressionNormalRefs:

    def test_peer_ref_resolves_sync_no_pending_record(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                assert(self.peer ~= nil, "peer ref must wire synchronously")
            end
            local plan = {
                modules = {
                    foo = {stem = "Foo", runtime_bearing = true,
                           module_path = "x", domain = "client"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "foo",
                             game_object_id = "g1", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "foo",
                             game_object_id = "g2", active = true,
                             enabled = true, config = {}},
                        },
                        references = {
                            {["from"] = "A:1", field = "peer", index = nil,
                             target_kind = "component", target_ref = "A:2",
                             target_is_ui = false},
                        },
                        lifecycle_order = {"A:1", "A:2"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g1 = {Name = "G1", _sceneRuntimeId = "g1", _children = {}},
                g2 = {Name = "G2", _sceneRuntimeId = "g2", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start("client")
            runDeferred()
            assert(engine._pendingPlacementRefs == nil,
                "no stripped refs => _pendingPlacementRefs must stay nil")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out, out


# ---------------------------------------------------------------------------
# AC5 -- a stripped ref whose target never registers: stays nil + WARN, no crash.
# ---------------------------------------------------------------------------

class TestFailSoftWarnWhenTargetNeverRegisters:

    def test_unresolved_stripped_ref_stays_nil_and_warns(self):
        scenario = textwrap.dedent("""\
            local Loadout = {} ; Loadout.__index = Loadout
            function Loadout.new(_) return setmetatable({}, Loadout) end
            function Loadout:Start()
                -- field must be readable + nil (no crash).
                assert(self.missionPopup == nil, "absent target => nil")
            end
            -- A placement EXISTS (so the ref is placement-scoped and queued),
            -- but its prefab subplan has NO instance for the target src_fid, so
            -- the placement boot never registers the engine key -> drain leaves
            -- the field nil + WARN.
            local PLACEMENT = "Main:777"
            local PREFAB = "guidB:Assets/Prefabs/Ghost.prefab"
            local ENGINE_KEY = PLACEMENT .. ":" .. PREFAB .. ":999"
            local plan = {
                modules = {
                    loadout = {stem = "Loadout", runtime_bearing = true,
                               module_path = "x", domain = "client"},
                },
                scenes = {
                    Main = {
                        instances = {
                            {instance_id = "Main:1", script_id = "loadout",
                             game_object_id = "sgo", active = true,
                             enabled = true, config = {}},
                        },
                        references = {
                            {["from"] = "Main:1", field = "missionPopup",
                             index = nil, target_kind = "component",
                             target_ref = ENGINE_KEY,
                             target_script_id = "loadout",
                             target_is_ui = false},
                        },
                        lifecycle_order = {"Main:1"},
                    },
                },
                prefabs = {
                    [PREFAB] = {
                        name = "Ghost",
                        instances = {},  -- nothing to boot/register
                        references = {}, lifecycle_order = {},
                    },
                },
                scene_prefab_placements = {
                    {placement_id = PLACEMENT, prefab_id = PREFAB,
                     active = true, enabled = true},
                },
                domain_overrides = {},
            }
            local services = servicesFor(plan, {loadout = Loadout}, {
                sgo = {Name = "Loadout", _sceneRuntimeId = "sgo", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start("client")
            runDeferred()
            local src = engine._componentByInstanceId["Main:1"]
            assert(src.missionPopup == nil, "field must stay nil")
            local warned = false
            for _, line in ipairs(logs) do
                if string.find(line, "stripped%-prefab ref") then warned = true end
            end
            assert(warned, "must WARN on a never-resolved stripped ref")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out, out


# ---------------------------------------------------------------------------
# Round-1 P1 (codex) -- the UI-DEFERRED placed target variant of AC7.
#
# AC7 above covers a cross-domain scene->placed-prefab ref whose placed target
# is NON-deferred (it routes through the gated ``_pendingPlacementRefs`` queue).
# The OPEN question codex raised: the OLDER ``_inboundRefsToDeferred`` branch in
# ``_wireReferences`` (~:1299) is UNCONDITIONAL (no ``not crossDomainNil`` gate)
# and ``_completeDeferredBatch`` Pass-3b (~:2664) rebinds it with NO domain
# recheck. Could a cross-domain (client->server) scene->placed-prefab stripped
# ref whose placed target is UI-DEFERRED leak into ``_inboundRefsToDeferred`` and
# get REBOUND, defeating the cross-domain policy?
#
# EMPIRICAL ANSWER (this test): NO. The ``_inboundRefsToDeferred`` branch only
# fires when ``self._deferredInstanceIds[scopedTarget]`` is set at scene-ref wire
# time. But ``start()`` wires ALL scene refs FIRST (the scene loop, :2759-2849)
# and only THEN boots placements (:2925-2980), where a placed UI miss calls
# ``_deferUiInstance`` -> sets the deferred marker. So at the moment the scene
# cross-domain ref is wired, the placed target's marker is NOT yet set -> the
# ``_inboundRefsToDeferred`` branch CANNOT fire for this ref class. It instead
# hits the ``elseif not crossDomainNil ...`` branch, which is gated, so the
# policy-nil'd ref enters NEITHER queue. When the deferred batch later completes
# and drains, the ref is queued nowhere -> nothing to rebind -> the field stays
# nil. The bypass is refuted-at-integration; no production change is warranted.
#
# This test drives the REAL host (no stubbing of _wireReferences /
# _drainPendingPlacementRefs / _completeDeferredBatch): a UI-deferred placed
# SERVER prefab + a scene CLIENT source holding a cross-domain ref to it, plus a
# same-domain (client->client) UI-deferred placed ref on the same source to prove
# the deferred binding path itself still works (regression guard).
# ---------------------------------------------------------------------------

class TestCrossDomainUiDeferredPlacedTargetHoldsThroughDeferredDrain:

    def _scenario(self) -> str:
        # Placement ``Main:9`` of server prefab ``pfbS`` (instance ``pfbS:222``,
        # host goid ``pfbS:host``). Its engine registry key when the deferred
        # batch builds it is ``_idWithPlacement("Main:9","pfbS:222")`` =
        # ``Main:9:pfbS:222`` -- exactly the scene ref's resolved ``target_ref``.
        # The deferred resolver is keyed on the namespaced host goid:
        # ``_idWithPlacement("Main:9","pfbS:host")`` = ``Main:9:pfbS:host``.
        #
        # A second placement ``Main:8`` of CLIENT prefab ``pfbC`` (same shape) is
        # the same-domain control: its client->client ref MUST bind through the
        # deferred path, proving the UI-deferred placed binding works for a
        # legitimate same-domain ref.
        return textwrap.dedent("""\
            -- Scene CLIENT source holding two refs: ``srvPeer`` (cross-domain ->
            -- server placed UI-deferred) and ``cliPeer`` (same-domain -> client
            -- placed UI-deferred).
            local Src = {} ; Src.__index = Src
            function Src.new(_) return setmetatable({}, Src) end
            -- Placed SERVER UI component (cross-domain target).
            local Srv = {} ; Srv.__index = Srv
            function Srv.new(_) return setmetatable({mark="SRV"}, Srv) end
            function Srv:Awake() assert(self.gameObject ~= nil, "Srv.go bound") end
            -- Placed CLIENT UI component (same-domain target).
            local Cli = {} ; Cli.__index = Cli
            function Cli.new(_) return setmetatable({mark="CLI"}, Cli) end
            function Cli:Awake() assert(self.gameObject ~= nil, "Cli.go bound") end

            local SRV_KEY = "Main:9:pfbS:222"   -- scene ref target_ref (engine key)
            local CLI_KEY = "Main:8:pfbC:333"

            local plan = {
                modules = {
                    src = {stem = "Src", runtime_bearing = true,
                           module_path = "x", domain = "client"},
                    srv = {stem = "Srv", runtime_bearing = true,
                           module_path = "y", domain = "server"},
                    cli = {stem = "Cli", runtime_bearing = true,
                           module_path = "z", domain = "client"},
                },
                scenes = {
                    Main = {
                        instances = {
                            {instance_id = "Main:1", script_id = "src",
                             game_object_id = "sgo", active = true,
                             enabled = true, config = {}},
                        },
                        references = {
                            -- cross-domain (client -> server placed UI-deferred)
                            {["from"] = "Main:1", field = "srvPeer", index = nil,
                             target_kind = "component", target_ref = SRV_KEY,
                             target_script_id = "srv", target_is_ui = false},
                            -- same-domain (client -> client placed UI-deferred)
                            {["from"] = "Main:1", field = "cliPeer", index = nil,
                             target_kind = "component", target_ref = CLI_KEY,
                             target_script_id = "cli", target_is_ui = false},
                        },
                        lifecycle_order = {"Main:1"},
                    },
                },
                prefabs = {
                    ["pfbS"] = {
                        name = "Srv",
                        instances = {{instance_id = "pfbS:222", script_id = "srv",
                                      game_object_id = "pfbS:host", active = true,
                                      enabled = true, config = {},
                                      instance_owner_is_ui = true}},
                        references = {}, lifecycle_order = {"pfbS:222"},
                    },
                    ["pfbC"] = {
                        name = "Cli",
                        instances = {{instance_id = "pfbC:333", script_id = "cli",
                                      game_object_id = "pfbC:host", active = true,
                                      enabled = true, config = {},
                                      instance_owner_is_ui = true}},
                        references = {}, lifecycle_order = {"pfbC:333"},
                    },
                },
                scene_prefab_placements = {
                    {placement_id = "Main:9", prefab_id = "pfbS",
                     active = true, enabled = true},
                    {placement_id = "Main:8", prefab_id = "pfbC",
                     active = true, enabled = true},
                },
                domain_overrides = {},
            }
            -- Only the scene source host exists in workspace at boot. NO clone
            -- for either placement (so _findUnboundClonePerPrefab returns nil and
            -- workspaceFind misses for the placed hosts) -> each placed UI-owned
            -- instance DEFERS via _deferUiInstance during the placement boot loop.
            local instances = {
                sgo = {Name = "Src", _sceneRuntimeId = "sgo", _children = {}},
            }
            local services = servicesFor(plan, {src = Src, srv = Srv, cli = Cli}, instances)
            -- The deferred batch resolves each placed host clone LATE (clone has
            -- landed by then). Keyed on the namespaced host goid.
            local srvClone = {Name = "SrvHost", _sceneRuntimeId = "Main:9:pfbS:host",
                              _children = {}}
            local cliClone = {Name = "CliHost", _sceneRuntimeId = "Main:8:pfbC:host",
                              _children = {}}
            services.awaitUiHost = function(id)
                if id == "Main:9:pfbS:host" then return srvClone end
                if id == "Main:8:pfbC:host" then return cliClone end
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            -- Run BOTH domains so the placed SERVER component IS constructed +
            -- registered locally -- the deferred drain therefore COULD rebind the
            -- cross-domain ref if it had been queued. This is the strong form of
            -- the gate test.
            local edges = engine:start(nil)
            runDeferred()  -- flush deferred Starts + late UI batch

            local srcComp = engine._componentByInstanceId["Main:1"]
            assert(srcComp ~= nil, "source component must exist")

            -- (a) cross-domain ref injected nil at scene-wire time.
            if srcComp.srvPeer == nil then print("INJECT_NIL_OK") end

            -- (b) the cross-domain ref is in NEITHER back-patch queue (it was
            -- never queued: the _inboundRefsToDeferred branch couldn't fire at
            -- scene-wire time because the placement hadn't booted/deferred yet,
            -- and the _pendingPlacementRefs branch is gated on not-cross-domain).
            local queuedCross = false
            for _, rec in ipairs(engine._pendingPlacementRefs or {}) do
                if rec.field == "srvPeer" then queuedCross = true end
            end
            for _, rec in ipairs(engine._inboundRefsToDeferred or {}) do
                if rec.field == "srvPeer" then queuedCross = true end
            end
            if not queuedCross then print("NOT_QUEUED_OK") end

            -- (c) STAYS nil after the deferred batch built + registered the
            -- placed SERVER component and re-ran the drain. Drain once more to be
            -- explicit (idempotent; the placed server comp now exists in the
            -- registry, so a leaked queue entry WOULD bind it).
            engine:_drainPendingPlacementRefs()
            assert(engine._componentByInstanceId[SRV_KEY] ~= nil,
                "placed server component must be registered (drain could rebind)")
            if srcComp.srvPeer == nil then print("STAYS_NIL_AFTER_DRAIN") end

            -- cross-domain edge recorded.
            local edgeOk = false
            for _, e in ipairs(edges) do
                if e.field == "srvPeer" and e.from_domain == "client"
                   and e.to_domain == "server" then edgeOk = true end
            end
            if edgeOk then print("EDGE_RECORDED") end

            -- (d) the SAME-DOMAIN client->client UI-deferred placed ref DID bind
            -- through the deferred drain -- proving the deferred binding path
            -- itself works and the gate doesn't over-block legitimate refs.
            if type(srcComp.cliPeer) == "table" and srcComp.cliPeer.mark == "CLI" then
                print("SAME_DOMAIN_BOUND")
            end
            print("DONE")
        """)

    def test_cross_domain_ui_deferred_placed_target_stays_nil(self):
        rc, out, err = _run_scenario(self._scenario())
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "INJECT_NIL_OK" in out, (
            f"cross-domain ref must inject nil at scene-wire; got:\n{out}")
        assert "NOT_QUEUED_OK" in out, (
            f"cross-domain ref must NOT be queued in any back-patch list "
            f"(neither _pendingPlacementRefs nor _inboundRefsToDeferred); "
            f"got:\n{out}")
        assert "STAYS_NIL_AFTER_DRAIN" in out, (
            f"cross-domain ref must stay nil after the UI-deferred placed target "
            f"is built + registered and the drain re-runs; got:\n{out}")
        assert "EDGE_RECORDED" in out, (
            f"cross-domain edge must be recorded; got:\n{out}")
        assert "SAME_DOMAIN_BOUND" in out, (
            f"a same-domain client->client UI-deferred placed ref must still bind "
            f"through the deferred drain; got:\n{out}")
