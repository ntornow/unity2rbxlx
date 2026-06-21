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
# gap#4 acceptance (i) -- a scene comp's ref to a placed-prefab stripped MB is
# NON-nil (object + .gameObject) AT THE SCENE COMP'S OnEnable, because PASS 1
# constructs+registers the placement and the boot drain binds the ref BEFORE the
# scene lifecycle runs (the inverted contract: pre-reorder the drain ran AFTER
# the scene lifecycle, so the field was nil at Awake/OnEnable).
# ---------------------------------------------------------------------------

class TestStrippedRefExistsBeforeSceneLifecycle:

    # ``drain_mode``:
    #   "early"      -- production: boot drain runs in PASS 1, before lifecycle.
    #   "absent"     -- RED proof (a): drain shadowed to a no-op for the whole
    #                   run -> the ref never binds, nil at OnEnable. Proves the
    #                   DRAIN is load-bearing.
    #   "stays_late" -- RED proof (b): the boot drain is suppressed DURING
    #                   start() (so PASS 1's early drain is a no-op) and the real
    #                   drain is invoked only AFTER start() returns -- i.e. the
    #                   drain still runs, but after the scene lifecycle, exactly
    #                   the PRE-REORDER ordering. The field is nil at OnEnable
    #                   even though it binds post-start. Proves the EARLY
    #                   POSITION of the drain is load-bearing (distinct from (a)).
    def _plan_scenario(self, *, drain_mode: str) -> str:
        if drain_mode == "absent":
            setup = (
                "engine._drainPendingPlacementRefs = function() end"
                "  -- RED (a): drain disabled entirely\n"
            )
            post = ""
        elif drain_mode == "stays_late":
            # Capture the real method, no-op it during start(), restore + invoke
            # it after start() returns -> drain runs but AFTER the scene
            # lifecycle (the pre-reorder order). probeAtOnEnable was captured
            # synchronously during the scene OnEnable, so it reflects the
            # pre-drain state.
            setup = (
                "local _realDrain = engine._drainPendingPlacementRefs\n"
                "engine._drainPendingPlacementRefs = function() end"
                "  -- RED (b): suppress the early drain during start()\n"
            )
            post = (
                "engine._drainPendingPlacementRefs = _realDrain\n"
                "engine:_drainPendingPlacementRefs()  -- drain LATE, after lifecycle\n"
            )
        else:  # "early" -- production order
            setup = ""
            post = ""
        # The engine-union key the placement boot registers under is
        # ``<placement_id>:<inst.instance_id>``; inst.instance_id is
        # ``<prefab_id>:<src_fid>``. The scene ref's resolved target_ref equals
        # that full key (what slice 3.2's planner emits).
        return textwrap.dedent("""\
            -- Probes captured SYNCHRONOUSLY inside the scene comp's lifecycle,
            -- the faithful observation point (LoadoutState:Enter runs inside
            -- GameManager.OnEnable per the root cause).
            local probeAtAwake = "UNREAD"
            local probeAtOnEnable = "UNREAD"
            local goAtOnEnable = "UNREAD"
            local boundField = "UNREAD"

            -- Source: a scene MonoBehaviour (LoadoutState) holding the ref.
            local Loadout = {} ; Loadout.__index = Loadout
            function Loadout.new(_) return setmetatable({}, Loadout) end
            function Loadout:Awake()
                probeAtAwake = self.missionPopup
            end
            function Loadout:OnEnable()
                -- The live crash site reads ``self.missionPopup.gameObject``
                -- inside the scene OnEnable; the ref MUST exist here now.
                probeAtOnEnable = self.missionPopup
                if self.missionPopup ~= nil then
                    goAtOnEnable = self.missionPopup.gameObject
                end
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
            __SETUP__
            engine:start("client")
            __POST__
            runDeferred()  -- flush Start

            if probeAtOnEnable == nil then
                print("ONENABLE_NIL")
            elseif type(probeAtOnEnable) == "table" and probeAtOnEnable.mark == "MUI" then
                print("ONENABLE_NONNIL")
                if goAtOnEnable ~= nil and goAtOnEnable ~= "UNREAD" then
                    print("ONENABLE_GO_NONNIL")
                else
                    print("ONENABLE_GO_NIL")
                end
            else
                print("ONENABLE_OTHER:" .. tostring(probeAtOnEnable))
            end
            if probeAtAwake == nil then
                print("AWAKE_NIL")
            elseif type(probeAtAwake) == "table" and probeAtAwake.mark == "MUI" then
                print("AWAKE_NONNIL")
            else
                print("AWAKE_OTHER:" .. tostring(probeAtAwake))
            end
            print("DONE")
        """).replace("__SETUP__", setup).replace("__POST__", post)

    def test_field_nonnil_with_gameobject_at_scene_onenable(self):
        # GREEN (acceptance (i)): the early drain binds the ref BEFORE the scene
        # lifecycle, so the scene comp sees a non-nil missionPopup -- with a
        # non-nil .gameObject -- at its OnEnable (and even at Awake).
        rc, out, err = _run_scenario(self._plan_scenario(drain_mode="early"))
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "ONENABLE_NONNIL" in out, (
            f"missionPopup must be non-nil at the scene comp's OnEnable; got:\n{out}")
        assert "ONENABLE_GO_NONNIL" in out, (
            f"missionPopup.gameObject must be non-nil at OnEnable (the live crash "
            f"site reads it); got:\n{out}")
        assert "AWAKE_NONNIL" in out, (
            f"under the reorder the ref also exists at Awake (constructed+drained "
            f"before the whole scene lifecycle); got:\n{out}")

    def test_red_pre_fix_no_drain_leaves_field_nil(self):
        # RED proof (a): the DRAIN is load-bearing. With it disabled the ref
        # never binds, so it is nil at the scene OnEnable.
        rc, out, err = _run_scenario(self._plan_scenario(drain_mode="absent"))
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "ONENABLE_NIL" in out, (
            f"RED (a): without the drain the ref must be nil at OnEnable; got:\n{out}")

    def test_red_drain_stays_late_leaves_field_nil_at_onenable(self):
        # RED proof (b): the EARLY POSITION of the drain is load-bearing. The
        # drain still runs (so the field binds post-start), but only AFTER the
        # scene lifecycle -- the pre-reorder order -- so the ref is nil AT the
        # scene comp's OnEnable. Distinct from (a): here the drain is present.
        rc, out, err = _run_scenario(
            self._plan_scenario(drain_mode="stays_late"))
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "ONENABLE_NIL" in out, (
            f"RED (b): a late drain leaves the ref nil at the scene OnEnable; "
            f"got:\n{out}")


# ---------------------------------------------------------------------------
# gap#4 acceptance (ii) -- the global Awake-before-Start invariant still holds
# across the PASS-2 scene-then-placement batches: every comp's Awake/OnEnable
# (sync) precedes EVERY comp's Start (task.defer'd), so no Start marker may
# appear before all Awake/OnEnable markers in the combined log.
# ---------------------------------------------------------------------------

class TestAwakeBeforeStartInvariantAcrossBatches:

    def _scenario(self) -> str:
        # A scene comp + a scene-placed placement comp, each logging an ordered
        # marker in Awake/OnEnable (sync) and Start (deferred). After
        # runDeferred() flushes the deferred Starts, NO Start marker may precede
        # any Awake/OnEnable marker.
        return textwrap.dedent("""\
            local order = {}
            local function mark(s) table.insert(order, s) end

            local Scene = {} ; Scene.__index = Scene
            function Scene.new(_) return setmetatable({}, Scene) end
            function Scene:Awake() mark("scene_awake") end
            function Scene:OnEnable() mark("scene_onenable") end
            function Scene:Start() mark("scene_start") end

            local Placed = {} ; Placed.__index = Placed
            function Placed.new(_) return setmetatable({}, Placed) end
            function Placed:Awake() mark("placed_awake") end
            function Placed:OnEnable() mark("placed_onenable") end
            function Placed:Start() mark("placed_start") end

            local PLACEMENT = "Main:42"
            local PREFAB = "g:Assets/Prefabs/P.prefab"
            local PREFAB_LOCAL = PREFAB .. ":99"
            local ENGINE_KEY = PLACEMENT .. ":" .. PREFAB_LOCAL

            local plan = {
                modules = {
                    scene = {stem = "Scene", runtime_bearing = true,
                             module_path = "x", domain = "client"},
                    placed = {stem = "Placed", runtime_bearing = true,
                              module_path = "y", domain = "client"},
                },
                scenes = {
                    Main = {
                        instances = {
                            {instance_id = "Main:1", script_id = "scene",
                             game_object_id = "sgo", active = true,
                             enabled = true, config = {}},
                        },
                        references = {}, lifecycle_order = {"Main:1"},
                    },
                },
                prefabs = {
                    [PREFAB] = {
                        name = "P",
                        instances = {{instance_id = PREFAB_LOCAL, script_id = "placed",
                                      game_object_id = PREFAB_LOCAL, active = true,
                                      enabled = true, config = {}}},
                        references = {}, lifecycle_order = {PREFAB_LOCAL},
                    },
                },
                scene_prefab_placements = {
                    {placement_id = PLACEMENT, prefab_id = PREFAB,
                     active = true, enabled = true},
                },
                domain_overrides = {},
            }
            local instances = {
                sgo = {Name = "Scene", _sceneRuntimeId = "sgo", _children = {}},
                [ENGINE_KEY] = {Name = "P", _sceneRuntimeId = ENGINE_KEY,
                                _children = {}},
            }
            local services = servicesFor(plan, {scene = Scene, placed = Placed}, instances)
            local engine = SceneRuntime.new(services, plan)
            engine:start("client")
            runDeferred()  -- flush deferred Starts

            -- The combined order: no Start may appear before any Awake/OnEnable.
            local firstStartIdx, lastLifecycleIdx = nil, nil
            for i, m in ipairs(order) do
                if string.find(m, "_start") and firstStartIdx == nil then
                    firstStartIdx = i
                end
                if string.find(m, "_awake") or string.find(m, "_onenable") then
                    lastLifecycleIdx = i
                end
            end
            assert(firstStartIdx ~= nil, "a Start must have fired")
            assert(lastLifecycleIdx ~= nil, "an Awake/OnEnable must have fired")
            if firstStartIdx > lastLifecycleIdx then
                print("AWAKE_BEFORE_START_OK")
            else
                print("ORDER_VIOLATION:" .. table.concat(order, ","))
            end
            -- Today's relative order is preserved: scene batch lifecycle before
            -- placement batch lifecycle.
            local sa, pa = nil, nil
            for i, m in ipairs(order) do
                if m == "scene_awake" then sa = i end
                if m == "placed_awake" then pa = i end
            end
            if sa ~= nil and pa ~= nil and sa < pa then
                print("SCENE_BEFORE_PLACEMENT_OK")
            end
            print("DONE")
        """)

    def test_no_start_before_all_awake_onenable(self):
        rc, out, err = _run_scenario(self._scenario())
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "AWAKE_BEFORE_START_OK" in out, (
            f"no Start marker may precede any Awake/OnEnable across batches; "
            f"got:\n{out}")
        assert "SCENE_BEFORE_PLACEMENT_OK" in out, (
            f"PASS-2 must keep today's scene-batch-then-placement-batch order; "
            f"got:\n{out}")


# ---------------------------------------------------------------------------
# gap#4 acceptance (iii) -- runtime-spawn boundary + single-bind.
#   (a) a runtime ``instantiatePrefab`` still resolves its ``externalRefs`` and
#       is NOT skipped by the reorder (the spawn path is byte-unchanged).
#   (b) a scene-placed target binds ONCE at the early boot drain: after start()
#       the boot drain leaves ``_pendingPlacementRefs`` clear (nil) of it, so a
#       later ``_completeDeferredBatch`` drain has nothing to rebind.
# ---------------------------------------------------------------------------

class TestRuntimeSpawnBoundaryAndSingleBind:

    def test_runtime_instantiate_prefab_resolves_external_refs(self):
        # (a) The reorder edits only start(); instantiatePrefab is a separate
        # path that must still apply its externalRefs override.
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end

            local plan = {
                modules = {
                    foo = {stem = "Foo", runtime_bearing = true,
                           module_path = "x", domain = "client"},
                },
                scenes = {}, domain_overrides = {},
                prefabs = {
                    ["pf"] = {
                        name = "Foo",
                        instances = {{instance_id = "pf:1", script_id = "foo",
                                      game_object_id = "pf:host", active = true,
                                      enabled = true, config = {}}},
                        references = {}, lifecycle_order = {"pf:1"},
                    },
                },
            }
            -- Synthesize a clone so the spawn path proceeds (the default harness
            -- clonePrefabTemplate returns nil). The clone's root SRI matches the
            -- prefab-local host so resolveCloneChild finds the component.
            local cloneRoot = {Name = "FooClone", _sceneRuntimeId = "pf:host",
                               _children = {}}
            local services = servicesFor(plan, {foo = Foo}, {})
            services.clonePrefabTemplate = function(prefabId, parent, cframe)
                return cloneRoot
            end
            local engine = SceneRuntime.new(services, plan)
            engine:start("client")

            -- externalRefs the runtime spawn must apply onto the constructed
            -- comp. Shape is {instId = {field = value}}, keyed by the
            -- prefab-local instance_id.
            local marker = {mark = "EXT"}
            local clone = engine:instantiatePrefab("pf", nil, nil,
                {["pf:1"] = {someRef = marker}})
            assert(clone ~= nil, "spawn must produce a clone")
            runDeferred()

            -- Find the spawned component (registered under a minted _rt_ key).
            local spawned = nil
            for key, comp in pairs(engine._componentByInstanceId) do
                if string.find(tostring(key), "_rt_") then spawned = comp end
            end
            assert(spawned ~= nil, "runtime-spawned comp must be registered")
            if spawned.someRef == marker then
                print("EXTERNAL_REF_BOUND")
            else
                print("EXTERNAL_REF_MISSING:" .. tostring(spawned.someRef))
            end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "EXTERNAL_REF_BOUND" in out, (
            f"runtime instantiatePrefab must still apply its externalRefs after "
            f"the start() reorder; got:\n{out}")

    def test_scene_placed_target_binds_once_at_early_drain(self):
        # (b) The boot drain (now in PASS 1) binds the scene->placement ref and
        # REMOVES the record, so _pendingPlacementRefs is clear of it after
        # start(); a later _completeDeferredBatch drain finds nothing to rebind.
        scenario = textwrap.dedent("""\
            local Loadout = {} ; Loadout.__index = Loadout
            function Loadout.new(_) return setmetatable({}, Loadout) end

            local MissionUI = {} ; MissionUI.__index = MissionUI
            function MissionUI.new(_) return setmetatable({mark="MUI"}, MissionUI) end

            local PLACEMENT = "Main:7"
            local PREFAB = "g:Assets/Prefabs/MissionPopup.prefab"
            local PREFAB_LOCAL = PREFAB .. ":11"
            local ENGINE_KEY = PLACEMENT .. ":" .. PREFAB_LOCAL

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
                            {instance_id = "Main:1", script_id = "loadout",
                             game_object_id = "sgo", active = true,
                             enabled = true, config = {}},
                        },
                        references = {
                            {["from"] = "Main:1", field = "missionPopup",
                             index = nil, target_kind = "component",
                             target_ref = ENGINE_KEY,
                             target_script_id = "missionui",
                             target_is_ui = false},
                        },
                        lifecycle_order = {"Main:1"},
                    },
                },
                prefabs = {
                    [PREFAB] = {
                        name = "MissionPopup",
                        instances = {{instance_id = PREFAB_LOCAL, script_id = "missionui",
                                      game_object_id = PREFAB_LOCAL, active = true,
                                      enabled = true, config = {}}},
                        references = {}, lifecycle_order = {PREFAB_LOCAL},
                    },
                },
                scene_prefab_placements = {
                    {placement_id = PLACEMENT, prefab_id = PREFAB,
                     active = true, enabled = true},
                },
                domain_overrides = {},
            }
            local instances = {
                sgo = {Name = "Loadout", _sceneRuntimeId = "sgo", _children = {}},
                [ENGINE_KEY] = {Name = "MissionPopup", _sceneRuntimeId = ENGINE_KEY,
                                _children = {}},
            }
            local services = servicesFor(plan, {loadout = Loadout, missionui = MissionUI}, instances)
            local engine = SceneRuntime.new(services, plan)
            engine:start("client")
            runDeferred()

            local src = engine._componentByInstanceId["Main:1"]
            assert(src ~= nil, "scene source must exist")
            -- bound exactly once.
            if type(src.missionPopup) == "table" and src.missionPopup.mark == "MUI" then
                print("BOUND_ONCE")
            end
            -- the early boot drain removed the record -> the queue is clear.
            local stillQueued = false
            for _, rec in ipairs(engine._pendingPlacementRefs or {}) do
                if rec.field == "missionPopup" then stillQueued = true end
            end
            if not stillQueued then print("QUEUE_CLEAR") end
            -- a later drain (mirroring _completeDeferredBatch) finds nothing to
            -- rebind -- the value is unchanged.
            local before = src.missionPopup
            engine:_drainPendingPlacementRefs()
            if src.missionPopup == before then print("NO_DOUBLE_RESOLVE") end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "BOUND_ONCE" in out, (
            f"scene->placement ref must bind to the placed clone; got:\n{out}")
        assert "QUEUE_CLEAR" in out, (
            f"the early drain must remove the bound record from "
            f"_pendingPlacementRefs; got:\n{out}")
        assert "NO_DOUBLE_RESOLVE" in out, (
            f"a later drain must not rebind the already-bound ref; got:\n{out}")


# ---------------------------------------------------------------------------
# AC7 -- cross-domain stripped ref: nil-injected, NOT queued, stays nil
# through the drain, with the 3-real-client->client case still binding.
# ---------------------------------------------------------------------------

class TestCrossDomainStrippedRefHoldsThroughDrain:

    def _scenario(self, *, queue_policy_nils: bool) -> str:
        # ``queue_policy_nils`` models the pre-fix design where a policy-nil'd
        # ref WAS queued. The production gate can't be toggled from Lua, so the
        # RED variant manually injects the cross-domain rec into
        # _pendingPlacementRefs after start() to prove that if such a rec
        # existed the drain WOULD rebind it -- which the gate prevents.
        red_inject = textwrap.dedent("""\
            -- RED variant: inject the policy-nil'd ref the pre-fix design would
            -- have queued, then re-drain -- the drain rebinds it (which the
            -- NOT-cross-domain queue gate prevents).
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
# The UI-DEFERRED placed-target variant of AC7.
#
# AC7 covers a cross-domain scene->placed-prefab ref whose placed target is
# NON-deferred (routed through the gated ``_pendingPlacementRefs`` queue). The
# ``_inboundRefsToDeferred`` branch in ``_wireReferences`` is UNGATED and
# ``_completeDeferredBatch`` Pass-3b rebinds it with no domain recheck — could a
# cross-domain ref to a UI-DEFERRED placed target leak into it and get rebound?
#
# No: that branch only fires when ``_deferredInstanceIds[scopedTarget]`` is set
# at scene-ref wire time, but ``start()`` wires ALL scene refs BEFORE booting
# placements (which is where a placed UI miss sets the deferred marker). So at
# wire time the marker is not yet set; the ref hits the gated
# ``elseif not crossDomainNil`` branch instead and enters NEITHER queue, so the
# drain has nothing to rebind and the field stays nil.
#
# This test drives the REAL host (nothing stubbed): a UI-deferred placed SERVER
# prefab + a scene CLIENT source with a cross-domain ref to it, plus a
# same-domain (client->client) UI-deferred placed ref proving the deferred
# binding path still works (regression guard).
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
            -- cross-domain ref if it had been queued.
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


# ---------------------------------------------------------------------------
# AC7 -- the UI-DEFERRED *SOURCE* sub-case (the finalize BLOCKING fix).
#
# The class above covers a cross-domain ref whose SOURCE wires at SCENE-BOOT (so
# the target's ``_deferredInstanceIds`` marker is not yet set -> the ref takes
# the gated ``_pendingPlacementRefs`` branch). This class covers the OTHER timing:
# the SOURCE component is ITSELF UI-deferred, so its outbound refs wire LATE,
# inside ``_completeDeferredBatch`` Pass 3a -- at which point a peer's deferred
# marker CAN still be set. With two UI-deferred sources holding mutual
# cross-domain refs (a dependency CYCLE the topo-sort cannot order target-first),
# whichever group wires first sees the other's marker set and hits the
# ``_deferredInstanceIds -> _inboundRefsToDeferred`` branch. That branch was
# UNGATED, so a policy-nil'd cross-domain ref got recorded and Pass 3b
# (``_completeDeferredBatch``) replayed it with NO domain recheck -> cross-domain
# rebind. The fix gates that branch on ``not crossDomainNil`` (symmetric with the
# ``_pendingPlacementRefs`` gate) so the policy-nil'd ref enters NEITHER queue.
#
# Drives the REAL host through ``start()`` + the deferred batches (nothing
# stubbed). The same-domain (client->client) mutual-ref cycle is the regression
# guard: it must STILL bind through the deferred ``_inboundRefsToDeferred`` path.
# ---------------------------------------------------------------------------

class TestCrossDomainUiDeferredSourceHoldsThroughDeferredDrain:

    def _scenario(self, *, cross_domain: bool) -> str:
        # Two UI-owned SCENE instances whose host clones MISS at synchronous boot
        # (no entries in ``instances``) -> both DEFER. ``awaitUiHost`` lands each
        # host late, in DISTINCT host groups (keyed by ``gameObjectId``). The two
        # hold mutual ``component`` refs (``peer`` / ``back``) -> a dependency
        # cycle, so the deferred topo-sort cannot order one strictly before the
        # other; the group that wires first sees the other's deferred marker set.
        #
        # ``cross_domain``: when True the peer target is a SERVER module
        # (client->server, policy-nil'd). When False both are CLIENT (same-domain
        # control that MUST still bind through the deferred path).
        peer_domain = "server" if cross_domain else "client"
        peer_mark = "SRV" if cross_domain else "CLI"
        return textwrap.dedent("""\
            -- Source: a UI-deferred CLIENT scene component holding the ref.
            local Src = {} ; Src.__index = Src
            function Src.new(_) return setmetatable({mark="SRC"}, Src) end
            -- Peer: a UI-deferred component the source references (server in the
            -- cross-domain case, client in the same-domain control).
            local Peer = {} ; Peer.__index = Peer
            function Peer.new(_) return setmetatable({mark="__PEER_MARK__"}, Peer) end

            local plan = {
                modules = {
                    src = {stem = "Src", runtime_bearing = true,
                           module_path = "x", domain = "client"},
                    peer = {stem = "Peer", runtime_bearing = true,
                            module_path = "y", domain = "__PEER_DOMAIN__"},
                },
                scenes = {
                    Main = {
                        instances = {
                            {instance_id = "Main:1", script_id = "src",
                             game_object_id = "srcgo", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                            {instance_id = "Main:2", script_id = "peer",
                             game_object_id = "peergo", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {
                            -- the ref under test: source -> peer.
                            {["from"] = "Main:1", field = "peer", index = nil,
                             target_kind = "component", target_ref = "Main:2",
                             target_script_id = "peer", target_is_ui = false},
                            -- reverse ref -> makes a dependency CYCLE so the topo
                            -- sort cannot order ``peer``'s group before ``src``'s;
                            -- whichever wires first sees the other's marker set.
                            {["from"] = "Main:2", field = "back", index = nil,
                             target_kind = "component", target_ref = "Main:1",
                             target_script_id = "src", target_is_ui = false},
                        },
                        lifecycle_order = {"Main:1", "Main:2"},
                    },
                },
                prefabs = {}, scene_prefab_placements = {}, domain_overrides = {},
            }
            -- Neither host clone exists at boot -> both UI-defer.
            local instances = {}
            local services = servicesFor(plan, {src = Src, peer = Peer}, instances)
            -- Late host resolution: each host lands in its own group.
            local srcClone = {Name = "SrcHost", _sceneRuntimeId = "srcgo", _children = {}}
            local peerClone = {Name = "PeerHost", _sceneRuntimeId = "peergo", _children = {}}
            services.awaitUiHost = function(id)
                if id == "srcgo" then return srcClone end
                if id == "peergo" then return peerClone end
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            -- Run BOTH domains so the (server, in the cross-domain case) peer IS
            -- constructed + registered locally -- the deferred drain therefore
            -- COULD rebind the source's ref if it had been queued.
            local edges = engine:start(nil)
            runDeferred()  -- flush deferred Starts + late UI batches

            local srcComp = engine._componentByInstanceId["Main:1"]
            assert(srcComp ~= nil, "source component must exist")
            assert(engine._componentByInstanceId["Main:2"] ~= nil,
                "peer component must be registered (drain could rebind)")

            -- (b) the source's ref is in NEITHER back-patch queue when
            -- cross-domain (policy-nil'd refs must never be queued).
            local queued = false
            for _, rec in ipairs(engine._pendingPlacementRefs or {}) do
                if rec.field == "peer" then queued = true end
            end
            for _, rec in ipairs(engine._inboundRefsToDeferred or {}) do
                if rec.field == "peer" then queued = true end
            end

            -- Re-drain explicitly to prove idempotence + that a leaked queue
            -- entry WOULD rebind (the registered peer is present).
            engine:_drainPendingPlacementRefs()

            __ASSERT_BLOCK__
            print("DONE")
        """).replace("__PEER_MARK__", peer_mark) \
            .replace("__PEER_DOMAIN__", peer_domain)

    def test_cross_domain_ui_deferred_source_stays_nil(self):
        # Cross-domain (client->server): the policy nil-injects at wire time, the
        # ref is queued NOWHERE, and it stays nil after the deferred drain even
        # though the server peer is registered locally.
        scenario = self._scenario(cross_domain=True).replace(
            "__ASSERT_BLOCK__", textwrap.dedent("""\
                if srcComp.peer == nil then print("INJECT_NIL_OK") end
                if not queued then print("NOT_QUEUED_OK") end
                if srcComp.peer == nil then print("STAYS_NIL_AFTER_DRAIN") end
                local edgeOk = false
                for _, e in ipairs(edges) do
                    if e.field == "peer" and e.from_domain == "client"
                       and e.to_domain == "server" then edgeOk = true end
                end
                if edgeOk then print("EDGE_RECORDED") end
            """))
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "INJECT_NIL_OK" in out, (
            f"cross-domain ref must inject nil at wire; got:\n{out}")
        assert "NOT_QUEUED_OK" in out, (
            f"a policy-nil'd cross-domain ref from a UI-deferred SOURCE must NOT "
            f"be recorded in _inboundRefsToDeferred (nor _pendingPlacementRefs); "
            f"got:\n{out}")
        assert "STAYS_NIL_AFTER_DRAIN" in out, (
            f"cross-domain ref must stay nil after the deferred batch builds + "
            f"registers the peer and Pass 3b / drain re-runs; got:\n{out}")
        assert "EDGE_RECORDED" in out, (
            f"cross-domain edge must be recorded; got:\n{out}")

    def test_same_domain_ui_deferred_source_still_binds(self):
        # Regression guard: a SAME-domain (client->client) ref from a UI-deferred
        # source MUST still bind through the deferred _inboundRefsToDeferred /
        # Pass-3b path -- the gate must not over-block legitimate refs.
        scenario = self._scenario(cross_domain=False).replace(
            "__ASSERT_BLOCK__", textwrap.dedent("""\
                if type(srcComp.peer) == "table" and srcComp.peer.mark == "CLI" then
                    print("SAME_DOMAIN_BOUND")
                else
                    print("SAME_DOMAIN_UNBOUND:" .. tostring(srcComp.peer))
                end
            """))
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "SAME_DOMAIN_BOUND" in out, (
            f"a same-domain client->client ref from a UI-deferred source must "
            f"still bind through the deferred drain; got:\n{out}")


# ---------------------------------------------------------------------------
# Fix #3 -- a stripped ref in an ARRAY SLOT (non-nil ``index``) is recorded into
# ``_pendingPlacementRefs`` and bound via ``_assignFieldOnComponent`` at the
# correct array index after the placed component registers. All other tests use
# scalar (``index = nil``) refs, so the index-sensitive replay path was unguarded.
# ---------------------------------------------------------------------------

class TestStrippedArraySlotRefBindsAtIndex:

    def _scenario(self) -> str:
        return textwrap.dedent("""\
            local boundSlot = "UNREAD"
            local arrLen = -1

            local Loadout = {} ; Loadout.__index = Loadout
            function Loadout.new(_) return setmetatable({}, Loadout) end
            function Loadout:Start()
                -- ``popups`` is an ARRAY field; the stripped ref targets slot
                -- index 2 (0-based, planner convention) -> Lua slot 3.
                if type(self.popups) == "table" then
                    arrLen = #self.popups
                    boundSlot = self.popups[3]
                end
            end

            local MissionUI = {} ; MissionUI.__index = MissionUI
            function MissionUI.new(_) return setmetatable({mark="MUI"}, MissionUI) end

            local PLACEMENT = "Main:1822972501"
            local PREFAB = "guidA:Assets/Prefabs/UI/MissionPopup.prefab"
            local SRC_FID = "114000011972273750"
            local PREFAB_LOCAL = PREFAB .. ":" .. SRC_FID
            local ENGINE_KEY = PLACEMENT .. ":" .. PREFAB_LOCAL

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
                            -- ARRAY slot ref: index 2 (0-based).
                            {["from"] = "Main:869760749", field = "popups",
                             index = 2, target_kind = "component",
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
            local instances = {
                sgo = {Name = "Loadout", _sceneRuntimeId = "sgo", _children = {}},
                [ENGINE_KEY] = {Name = "MissionPopup", _sceneRuntimeId = ENGINE_KEY,
                                _children = {}},
            }
            local services = servicesFor(plan, {loadout = Loadout, missionui = MissionUI}, instances)
            local engine = SceneRuntime.new(services, plan)

            -- The array-slot ref must have been queued (non-nil index carried).
            engine:start("client")
            -- Inspect the queued record BEFORE the drain wiped it (the post-
            -- placement drain runs inside start()). Re-derive from the bound
            -- result instead: assert the value landed at the right slot.
            runDeferred()  -- flush Start

            if type(boundSlot) == "table" and boundSlot.mark == "MUI" then
                print("SLOT_BOUND")
            else
                print("SLOT_OTHER:" .. tostring(boundSlot))
            end
            -- The ``+1`` index bridge must place the value at Lua slot 3, NOT
            -- slot 1 (a scalar-treatment bug would corrupt slot 1).
            local src = engine._componentByInstanceId["Main:869760749"]
            if type(src.popups) == "table"
               and src.popups[1] == nil
               and type(src.popups[3]) == "table"
               and src.popups[3].mark == "MUI" then
                print("INDEX_CORRECT")
            else
                print("INDEX_WRONG")
            end
            print("DONE")
        """)

    def test_array_slot_ref_binds_at_correct_index(self):
        rc, out, err = _run_scenario(self._scenario())
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "SLOT_BOUND" in out, (
            f"array-slot stripped ref must bind to the placed clone; got:\n{out}")
        assert "INDEX_CORRECT" in out, (
            f"array-slot ref must land at index+1 (slot 3), leaving slot 1 nil; "
            f"got:\n{out}")


# ---------------------------------------------------------------------------
# Fix #5 -- ``_isPlacementScopedRef`` false-arm: a non-placement-scoped
# scene-local component ref that MISSES (resolves nil) at scene boot must NOT be
# queued into ``_pendingPlacementRefs``. The pending-queue branch is gated on
# ``_isPlacementScopedRef``; the existing no-regression test only covers the
# synchronous-RESOLVE path (peer exists), never the nil-MISS of a non-scoped ref.
# ---------------------------------------------------------------------------

class TestNonPlacementScopedNilRefNotQueued:

    def test_scene_local_nil_miss_is_not_queued(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Start()
                -- The genuinely-absent scene-local target leaves the field nil.
                assert(self.peer == nil, "absent scene-local target => nil")
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
                        },
                        -- ``A:999`` is a genuinely-absent scene-local component
                        -- id: NOT a placement-scoped engine-union key (there are
                        -- NO scene_prefab_placements at all). _isPlacementScopedRef
                        -- must reject it -> the nil miss is NOT queued.
                        references = {
                            {["from"] = "A:1", field = "peer", index = nil,
                             target_kind = "component", target_ref = "A:999",
                             target_is_ui = false},
                        },
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g1 = {Name = "G1", _sceneRuntimeId = "g1", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start("client")
            runDeferred()
            -- The load-bearing assertion: a non-placement-scoped nil miss is
            -- NOT over-queued (only genuine placement-scoped load-order misses
            -- enter the queue). _pendingPlacementRefs must stay nil/absent.
            assert(engine._pendingPlacementRefs == nil,
                "a non-placement-scoped scene-local nil ref must NOT be queued; "
                .. "_pendingPlacementRefs must stay nil")
            print("NOT_QUEUED_OK")
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "NOT_QUEUED_OK" in out, out


# ---------------------------------------------------------------------------
# gap#4 edge case §2.1 / decision D2 -- the PASS-1a/1b seed-all-before-
# construct-all split. A placement parented under a LATER-SORTED placement must
# still resolve ``activeInHierarchy`` correctly at CONSTRUCTION, because PASS 1a
# seeds EVERY placement's parent map (``_goParentId``/``_goActiveSelf``) BEFORE
# PASS 1b constructs ANY placement (``_injectHostSurface`` walks the parent map
# during ``_constructPrefabClone``).
#
# Two placements: P_child (placement_id "AChild:1", sorts FIRST) parents its
# prefab root under P_parent's namespaced root GO; P_parent (placement_id
# "ZParent:1", sorts LAST) is authored INACTIVE (``active = false``). Placements
# are sorted by ``placement_id`` ascending in start(), so "AChild:1" < "ZParent:1"
# forces P_parent to construct AFTER P_child.
#
# Because PASS 1a seeds P_parent's inactive ``_goActiveSelf`` entry before PASS 1b
# constructs P_child, P_child's ancestor walk reaches that entry and resolves
# ``activeInHierarchy = false`` -> its boot ``OnEnable`` does NOT fire.
#
# DISCRIMINATOR vs a fused per-placement seed-then-construct: a fused order would
# seed P_child then immediately construct it, BEFORE P_parent's (later-sorted)
# entry was seeded. P_parent's ``_goActiveSelf`` would still be nil at that point,
# defaulting to ``true`` in ``_computeActiveInHierarchyViaParentMap``, so P_child
# would wrongly resolve active and its OnEnable WOULD fire. Asserting
# ``CHILD_ONENABLE_SKIPPED`` (OnEnable did NOT fire) thus distinguishes the
# two-pass split from the fused order. A positive control -- the SAME P_child made
# a planner-root with its parent active -- proves the suppression is the ancestor
# walk reaching P_parent, not an unconditional skip.
# ---------------------------------------------------------------------------

class TestPlacementUnderLaterSortedPlacementActiveInHierarchy:

    def _scenario(self, *, child_under_parent: bool) -> str:
        # ``child_under_parent``:
        #   True  -- production case: P_child's prefab root parents under
        #            P_parent's namespaced root GO (P_parent inactive) -> the
        #            ancestor walk reaches the later-sorted inactive parent and
        #            P_child's OnEnable must be suppressed.
        #   False -- positive control: P_child's prefab root is a planner-root
        #            (no parent edge), so its activeInHierarchy is its own
        #            (active) selfFlag -> OnEnable MUST fire. Proves the
        #            suppression in the True case is the ancestor walk, not an
        #            unconditional skip.
        if child_under_parent:
            # P_parent's namespaced root GO id = "<placement>:<root goid>".
            child_parent_go = '"ZParent:1:pfParent:root"'
        else:
            # Planner-root: no parent edge, no inactive ancestor. (Keep the
            # value bare -- a trailing comment here would swallow the table's
            # field separator.)
            child_parent_go = "nil"
        return textwrap.dedent("""\
            local childOnEnableFired = false

            -- P_child component: logs whether its boot OnEnable fired (OnEnable
            -- is gated on the runtime's computed activeInHierarchy).
            local Child = {} ; Child.__index = Child
            function Child.new(_) return setmetatable({mark="CHILD"}, Child) end
            function Child:OnEnable() childOnEnableFired = true end

            -- P_parent component (authored inactive); presence only matters so
            -- the placement boots and seeds its inactive parent-map entry.
            local Parent = {} ; Parent.__index = Parent
            function Parent.new(_) return setmetatable({mark="PARENT"}, Parent) end

            -- placement_ids chosen so the deterministic placement_id sort places
            -- P_parent AFTER P_child: "AChild:1" < "ZParent:1".
            local CHILD_PLACEMENT  = "AChild:1"
            local PARENT_PLACEMENT = "ZParent:1"
            local CHILD_PREFAB  = "gC:Assets/Prefabs/Child.prefab"
            local PARENT_PREFAB = "gP:Assets/Prefabs/Parent.prefab"
            local CHILD_ROOT  = "pfChild:root"   -- prefab-local goid
            local PARENT_ROOT = "pfParent:root"
            -- namespaced host goids the placement boot registers / workspaceFinds.
            local CHILD_NS  = CHILD_PLACEMENT  .. ":" .. CHILD_ROOT
            local PARENT_NS = PARENT_PLACEMENT .. ":" .. PARENT_ROOT

            local plan = {
                modules = {
                    child = {stem = "Child", runtime_bearing = true,
                             module_path = "x", domain = "client"},
                    parent = {stem = "Parent", runtime_bearing = true,
                              module_path = "y", domain = "client"},
                },
                scenes = {
                    Main = {
                        instances = {}, references = {}, lifecycle_order = {},
                    },
                },
                prefabs = {
                    [CHILD_PREFAB] = {
                        name = "Child",
                        -- prefab root (parent_game_object_id nil) -> the placement
                        -- boot binds it to the placement's parent_game_object_id.
                        instances = {{instance_id = CHILD_PREFAB .. ":1",
                                      script_id = "child",
                                      game_object_id = CHILD_ROOT,
                                      parent_game_object_id = nil,
                                      active = true, enabled = true, config = {}}},
                        references = {}, lifecycle_order = {CHILD_PREFAB .. ":1"},
                    },
                    [PARENT_PREFAB] = {
                        name = "Parent",
                        instances = {{instance_id = PARENT_PREFAB .. ":1",
                                      script_id = "parent",
                                      game_object_id = PARENT_ROOT,
                                      parent_game_object_id = nil,
                                      active = true, enabled = true, config = {}}},
                        references = {}, lifecycle_order = {PARENT_PREFAB .. ":1"},
                    },
                },
                scene_prefab_placements = {
                    -- P_child parents under P_parent's namespaced root GO (the
                    -- production case) or is a planner-root (the control). Listed
                    -- child-first, but the placement_id sort is what fixes order.
                    {placement_id = CHILD_PLACEMENT, prefab_id = CHILD_PREFAB,
                     active = true, enabled = true,
                     parent_game_object_id = __CHILD_PARENT_GO__},
                    -- P_parent authored INACTIVE -> its namespaced root GO seeds
                    -- _goActiveSelf = false in PASS 1a.
                    {placement_id = PARENT_PLACEMENT, prefab_id = PARENT_PREFAB,
                     active = false, enabled = true},
                },
                domain_overrides = {},
            }
            -- No workspace clone (no GetDescendants) -> each placement resolves its
            -- host via workspaceFind on the NAMESPACED goid.
            local instances = {
                [CHILD_NS]  = {Name = "Child",  _sceneRuntimeId = CHILD_NS,  _children = {}},
                [PARENT_NS] = {Name = "Parent", _sceneRuntimeId = PARENT_NS, _children = {}},
            }
            local services = servicesFor(plan, {child = Child, parent = Parent}, instances)
            local engine = SceneRuntime.new(services, plan)
            engine:start("client")
            runDeferred()  -- flush deferred Starts

            -- The P_child component must be registered (its placement booted).
            local childKey = CHILD_PLACEMENT .. ":" .. CHILD_PREFAB .. ":1"
            local childComp = engine._componentByInstanceId[childKey]
            assert(childComp ~= nil, "P_child component must be constructed+registered")

            if childOnEnableFired then
                print("CHILD_ONENABLE_FIRED")
            else
                print("CHILD_ONENABLE_SKIPPED")
            end
            -- The computed activeInHierarchy for P_child's namespaced root GO --
            -- seeded in PASS 1a, walked through P_parent at construction. This is
            -- the gate the OnEnable decision above reads.
            print("CHILD_AIH:" .. tostring(engine._goActiveInHierarchy[CHILD_NS]))
            print("DONE")
        """).replace("__CHILD_PARENT_GO__", child_parent_go)

    def test_child_under_later_sorted_inactive_parent_suppresses_onenable(self):
        # Production / two-pass guard: P_child (sorts first) parents under
        # P_parent (sorts last, inactive). Because PASS 1a seeds ALL parent maps
        # before PASS 1b constructs ANY placement, P_child's construct-time
        # ancestor walk reaches P_parent's seeded inactive entry ->
        # activeInHierarchy = false -> OnEnable suppressed. A fused per-placement
        # seed-then-construct would construct P_child before P_parent's entry was
        # seeded and WRONGLY fire OnEnable.
        rc, out, err = _run_scenario(self._scenario(child_under_parent=True))
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "CHILD_ONENABLE_SKIPPED" in out, (
            f"P_child parents under a LATER-sorted INACTIVE placement; the "
            f"seed-all-before-construct-all split must let its construct-time "
            f"activeInHierarchy walk reach the seeded inactive parent so OnEnable "
            f"does NOT fire. A fused per-placement order would fire it. got:\n{out}")
        assert "CHILD_AIH:false" in out, (
            f"P_child's computed activeInHierarchy must resolve false at boot "
            f"(ancestor P_parent inactive); got:\n{out}")

    def test_positive_control_child_as_root_fires_onenable(self):
        # Positive control: same P_child made a planner-root (no inactive
        # ancestor) -> activeInHierarchy is its own active selfFlag -> OnEnable
        # MUST fire. Proves the suppression above is the ancestor walk reaching
        # P_parent, not an unconditional skip of later-listed placements.
        rc, out, err = _run_scenario(self._scenario(child_under_parent=False))
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "CHILD_ONENABLE_FIRED" in out, (
            f"with no inactive ancestor P_child's OnEnable MUST fire (control); "
            f"got:\n{out}")
        assert "CHILD_AIH:true" in out, (
            f"a planner-root active P_child must resolve activeInHierarchy true; "
            f"got:\n{out}")


# ---------------------------------------------------------------------------
# gap#4 finalize (codex): the SAME-DOMAIN UI-DEFERRED PLACED TARGET quadrant.
#
# The early boot drain (PASS 1, moved before the lifecycle) binds a scene->
# placement ref ONLY when the target placement is already registered in the
# engine union at drain time. But when the target's host clone hasn't landed at
# boot, its placement is UI-DEFERRED (``_deferUiInstance``) and is therefore NOT
# constructed/registered in PASS 1 -- so the early drain CANNOT bind the ref and
# leaves the record QUEUED in ``_pendingPlacementRefs``. The target registers
# later, inside ``_completeDeferredBatch`` (run by ``_resolveDeferredUiInstances``
# at the tail of ``start()``), whose own ``_drainPendingPlacementRefs`` call
# (scene_runtime.luau:3939) binds the queued ref EXACTLY ONCE. Same domain
# (client->client) so it SHOULD eventually bind -- distinct from the cross-domain
# stay-nil quadrant above.
#
# Discriminating-ness: ``_resolveDeferredUiInstances`` runs INSIDE ``start()``, so
# observing "still queued after the early drain" requires capturing state BEFORE
# that late batch runs. We shadow ``_resolveDeferredUiInstances`` to a no-op for
# the duration of ``start()`` (the established ``stays_late`` technique), capture
# the post-PASS-1 state (ref still queued, target not yet registered -> proves the
# early drain could NOT bind it), then restore + invoke the real late batch and
# assert the ref binds once. If the reorder dropped the queued record at the early
# drain (e.g. cleared ``_pendingPlacementRefs`` unconditionally), STILL_QUEUED
# would fail; if the late batch double-bound, NO_DOUBLE_RESOLVE would fail.
# ---------------------------------------------------------------------------

class TestSameDomainUiDeferredPlacedTargetBindsOnceLater:

    def _scenario(self) -> str:
        return textwrap.dedent("""\
            local Loadout = {} ; Loadout.__index = Loadout
            function Loadout.new(_) return setmetatable({}, Loadout) end

            local MissionUI = {} ; MissionUI.__index = MissionUI
            function MissionUI.new(_) return setmetatable({mark="MUI"}, MissionUI) end

            -- Placement of a CLIENT prefab whose single instance is UI-owned, so
            -- with no workspace clone at boot it UI-DEFERS (not registered in
            -- PASS 1). Its engine registry key when the deferred batch builds it
            -- is _idWithPlacement(PLACEMENT, PREFAB_LOCAL) = ENGINE_KEY -- exactly
            -- the scene ref's target_ref. The deferred resolver keys on the
            -- namespaced HOST goid (_idWithPlacement(PLACEMENT, HOST_GOID)).
            local PLACEMENT = "Main:7"
            local PREFAB = "g:Assets/Prefabs/UI/MissionPopup.prefab"
            local PREFAB_LOCAL = PREFAB .. ":11"
            local HOST_GOID = PREFAB .. ":host"
            local ENGINE_KEY = PLACEMENT .. ":" .. PREFAB_LOCAL
            local HOST_KEY = PLACEMENT .. ":" .. HOST_GOID

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
                            {instance_id = "Main:1", script_id = "loadout",
                             game_object_id = "sgo", active = true,
                             enabled = true, config = {}},
                        },
                        references = {
                            -- same-domain (client -> client placed UI-deferred)
                            {["from"] = "Main:1", field = "missionPopup",
                             index = nil, target_kind = "component",
                             target_ref = ENGINE_KEY,
                             target_script_id = "missionui",
                             target_is_ui = false},
                        },
                        lifecycle_order = {"Main:1"},
                    },
                },
                prefabs = {
                    [PREFAB] = {
                        name = "MissionPopup",
                        instances = {{instance_id = PREFAB_LOCAL,
                                      script_id = "missionui",
                                      game_object_id = HOST_GOID, active = true,
                                      enabled = true, config = {},
                                      instance_owner_is_ui = true}},
                        references = {}, lifecycle_order = {PREFAB_LOCAL},
                    },
                },
                scene_prefab_placements = {
                    {placement_id = PLACEMENT, prefab_id = PREFAB,
                     active = true, enabled = true},
                },
                domain_overrides = {},
            }
            -- Only the scene source host exists at boot. NO clone for the
            -- placement (so _findUnboundClonePerPrefab returns nil AND the placed
            -- UI-owned host workspaceFind misses) -> the placement UI-DEFERS.
            local instances = {
                sgo = {Name = "Loadout", _sceneRuntimeId = "sgo", _children = {}},
            }
            local services = servicesFor(plan,
                {loadout = Loadout, missionui = MissionUI}, instances)
            -- The deferred batch resolves the placed host LATE (clone has landed
            -- by then). Keyed on the namespaced host goid.
            local hostClone = {Name = "MissionPopupHost", _sceneRuntimeId = HOST_KEY,
                               _children = {}}
            services.awaitUiHost = function(id)
                if id == HOST_KEY then return hostClone end
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            -- Suppress the late UI batch DURING start() so we can observe the
            -- post-early-drain state: the target is UI-deferred (not registered),
            -- so the early drain left the ref QUEUED.
            local _realResolve = engine._resolveDeferredUiInstances
            engine._resolveDeferredUiInstances = function() end
            engine:start("client")

            local src = engine._componentByInstanceId["Main:1"]
            assert(src ~= nil, "scene source must exist")

            -- (a) the early drain could NOT bind it: the UI-deferred target is
            -- not yet registered, the field is nil, and the record is STILL in
            -- _pendingPlacementRefs.
            assert(engine._componentByInstanceId[ENGINE_KEY] == nil,
                "UI-deferred target must NOT be registered at the early drain")
            if src.missionPopup == nil then print("NIL_AFTER_EARLY_DRAIN") end
            local stillQueued = false
            for _, rec in ipairs(engine._pendingPlacementRefs or {}) do
                if rec.field == "missionPopup" then stillQueued = true end
            end
            if stillQueued then print("STILL_QUEUED") end

            -- (b) now run the real late UI batch (_completeDeferredBatch ->
            -- _drainPendingPlacementRefs at scene_runtime.luau:3939) -> the target
            -- registers and the queued ref binds.
            engine._resolveDeferredUiInstances = _realResolve
            engine:_resolveDeferredUiInstances()
            runDeferred()  -- flush the late batch's deferred Starts

            assert(engine._componentByInstanceId[ENGINE_KEY] ~= nil,
                "UI-deferred target must be registered after the late batch")
            if type(src.missionPopup) == "table" and src.missionPopup.mark == "MUI" then
                print("BOUND_AFTER_LATE_DRAIN")
            else
                print("BOUND_OTHER:" .. tostring(src.missionPopup))
            end
            -- bound exactly once: the late drain removed the record, so the queue
            -- is clear of it and a further drain finds nothing to rebind.
            local stillQueuedAfter = false
            for _, rec in ipairs(engine._pendingPlacementRefs or {}) do
                if rec.field == "missionPopup" then stillQueuedAfter = true end
            end
            if not stillQueuedAfter then print("QUEUE_CLEAR") end
            local before = src.missionPopup
            engine:_drainPendingPlacementRefs()
            if src.missionPopup == before then print("NO_DOUBLE_RESOLVE") end
            print("DONE")
        """)

    def test_same_domain_ui_deferred_target_binds_once_via_completed_batch(self):
        rc, out, err = _run_scenario(self._scenario())
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "NIL_AFTER_EARLY_DRAIN" in out, (
            f"a UI-deferred placed target is not registered at the early boot "
            f"drain, so the scene->placement ref must be nil there; got:\n{out}")
        assert "STILL_QUEUED" in out, (
            f"the early drain could not bind the UI-deferred target, so the record "
            f"must REMAIN in _pendingPlacementRefs (not dropped); got:\n{out}")
        assert "BOUND_AFTER_LATE_DRAIN" in out, (
            f"after _completeDeferredBatch registers the UI-deferred target, the "
            f"queued ref must bind to the placed clone (same domain); got:\n{out}")
        assert "QUEUE_CLEAR" in out, (
            f"the late drain must remove the bound record from "
            f"_pendingPlacementRefs; got:\n{out}")
        assert "NO_DOUBLE_RESOLVE" in out, (
            f"a further drain must not rebind the already-bound ref (exactly "
            f"once); got:\n{out}")
