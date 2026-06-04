"""Slice 1.1: deferred UI-host gameObject resolution (generic boot race).

A UI-owned instance binds its host GameObject to a ScreenGui that Roblox
clones StarterGui->PlayerGui at spawn. At client boot the synchronous build
loop can run BEFORE that clone lands, so the one-shot ``workspaceFind``
returns nil and a UI controller (e.g. HudControl) would be constructed with
``self.gameObject == nil`` and crash.

These tests drive the production ``scene_runtime.luau`` through the shared
standalone-luau harness and assert:

  * A UI-owned instance whose ``workspaceFind`` MISSES is NOT built with a
    nil gameObject during the synchronous pass; instead it is deferred and
    completed via ``awaitUiHost`` (event-driven clone wait), so its
    ``Awake`` runs with a non-nil ``self.gameObject``.
  * The ``instance_owner_is_ui`` gate is strict: a NON-UI instance whose
    ``workspaceFind`` misses stays on the one-shot path (built immediately
    with a nil gameObject, ``awaitUiHost`` never called) -- no boot-time
    deferral / timeout penalty for the common path.

Regression guard: against the PRE-FIX ``start`` (one-shot build for every
instance) the first test FAILS -- the UI component is built synchronously
with a nil gameObject and its ``Awake`` assert blows up.
"""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path

from tests.test_scene_runtime_host_behavior import (  # noqa: F401
    _luau_available,
    _run_scenario,
    pytestmark,
)


def _await_ui_host_source() -> str:
    """Extract the ``awaitUiHost`` Luau function body from the emitted
    client entrypoint source, so the connect-first / timeout logic is
    tested as actually shipped (not a synchronous stub)."""
    from converter import autogen

    src = autogen._SCENE_RUNTIME_CLIENT_SOURCE
    start = src.index("local function awaitUiHost(")
    # The function ends at the first line that is exactly ``end`` at column 0
    # after the start (top-level ``local function``).
    rest = src[start:]
    end_marker = "\nend\n"
    end = rest.index(end_marker) + len(end_marker)
    return rest[:end]


# The harness ``servicesFor`` does not provide ``awaitUiHost``; each
# scenario appends it to the returned services table (this is exactly how a
# production client entrypoint injects the host-surface resolver).


class TestDeferredUiHostResolution:

    def test_ui_owned_miss_defers_and_binds_via_await(self):
        scenario = textwrap.dedent("""\
            local events = {}

            -- UI controller: crashes if gameObject is nil at Awake (the
            -- real HudControl:46 shape).
            local Hud = {} ; Hud.__index = Hud
            function Hud.new(_) return setmetatable({}, Hud) end
            function Hud:Awake()
                assert(self.gameObject ~= nil,
                    "Hud.gameObject must be bound before Awake")
                table.insert(events, "Hud.Awake go=" .. tostring(self.gameObject.Name))
            end
            function Hud:OnEnable() table.insert(events, "Hud.OnEnable") end
            function Hud:Start() table.insert(events, "Hud.Start") end

            local plan = {
                modules = {
                    hud = {stem = "Hud", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "hud",
                             game_object_id = "hudId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            -- The HUD clone instance -- present in PlayerGui, but the
            -- synchronous workspaceFind must NOT see it (simulate "clone
            -- hasn't landed in workspace yet at boot").
            local hudClone = {Name = "HUD", _sceneRuntimeId = "hudId", _children = {}}

            -- workspaceFind returns nil for the UI id at boot (the race).
            local services = servicesFor(plan, {hud = Hud}, {})
            local awaitCalls = {}
            services.awaitUiHost = function(id)
                table.insert(awaitCalls, id)
                -- The clone has landed by the time the deferred resolver runs.
                if id == "hudId" then return hudClone end
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()  -- flush deferred Starts (sync batches + late UI batch)

            print("AWAIT_CALLS=" .. tostring(#awaitCalls))
            for _, e in ipairs(events) do print(e) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # awaitUiHost was used to resolve the missed UI host.
        assert "AWAIT_CALLS=1" in lines, out
        # Awake ran with a non-nil gameObject bound to the landed clone.
        assert any(l.startswith("Hud.Awake go=HUD") for l in lines), out
        # Full late lifecycle batch ran (Awake -> OnEnable -> Start).
        assert "Hud.OnEnable" in lines, out
        assert "Hud.Start" in lines, out
        assert lines.index("Hud.Awake go=HUD") < lines.index("Hud.OnEnable")
        assert lines.index("Hud.OnEnable") < lines.index("Hud.Start")

    def test_non_ui_miss_stays_one_shot_no_await(self):
        scenario = textwrap.dedent("""\
            local events = {}

            -- Non-UI controller tolerant of a nil gameObject (the one-shot
            -- path builds it immediately even on a workspaceFind miss).
            local Logic = {} ; Logic.__index = Logic
            function Logic.new(_) return setmetatable({}, Logic) end
            function Logic:Awake()
                table.insert(events,
                    "Logic.Awake go=" .. tostring(self.gameObject))
            end

            local plan = {
                modules = {
                    logic = {stem = "Logic", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            -- No instance_owner_is_ui flag -> non-UI.
                            {instance_id = "A:1", script_id = "logic",
                             game_object_id = "missingId", active = true,
                             enabled = true, config = {}},
                        },
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            -- workspaceFind misses (empty instance table) -> one-shot nil.
            local services = servicesFor(plan, {logic = Logic}, {})
            local awaitCalls = {}
            services.awaitUiHost = function(id)
                table.insert(awaitCalls, id)
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()

            print("AWAIT_CALLS=" .. tostring(#awaitCalls))
            for _, e in ipairs(events) do print(e) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # The non-UI miss must NOT route through awaitUiHost (no deferral,
        # no timeout penalty) -- it stays one-shot.
        assert "AWAIT_CALLS=0" in lines, out
        # It was still built immediately (with a nil gameObject), proving the
        # one-shot path is unchanged for non-UI.
        assert "Logic.Awake go=nil" in lines, out


class TestBatchedDeferralAndBackPatch:
    """Fix-round-1 BLOCKING #1 (batched lifecycle) + #2 (inbound ref
    back-patch). A synchronous non-UI ``Controller`` holds a serialized ref
    to a deferred UI ``Hud``; the deferred batch must (a) back-patch that
    inbound ref and (b) run the whole deferred set as ONE batch so its
    Awake/OnEnable all precede its Start (intra-batch order)."""

    def test_inbound_ref_backpatched_and_intra_batch_order(self):
        scenario = textwrap.dedent("""\
            local events = {}

            -- Synchronous (non-UI) source. Its serialized ``hud`` ref targets
            -- the deferred UI component; pre-fix it stays nil forever.
            local Controller = {} ; Controller.__index = Controller
            function Controller.new(_) return setmetatable({hud = nil}, Controller) end
            function Controller:Awake() table.insert(events, "Controller.Awake") end
            function Controller:Start()
                table.insert(events,
                    "Controller.Start hud=" .. tostring(self.hud and self.hud._tag))
            end

            -- Two deferred UI components on DIFFERENT hosts. ``Hud`` is the
            -- inbound-ref target; ``Hud2`` is a second deferred peer to prove
            -- the batch runs all Awakes before any Start.
            local Hud = {} ; Hud.__index = Hud
            function Hud.new(_) return setmetatable({_tag = "HUD"}, Hud) end
            function Hud:Awake()
                assert(self.gameObject ~= nil, "Hud.go must be bound")
                table.insert(events, "Hud.Awake")
            end
            function Hud:Start() table.insert(events, "Hud.Start") end

            local Hud2 = {} ; Hud2.__index = Hud2
            function Hud2.new(_) return setmetatable({}, Hud2) end
            function Hud2:Awake()
                assert(self.gameObject ~= nil, "Hud2.go must be bound")
                table.insert(events, "Hud2.Awake")
            end
            function Hud2:Start() table.insert(events, "Hud2.Start") end

            local plan = {
                modules = {
                    ctl  = {stem = "Controller", runtime_bearing = true, module_path = "x"},
                    hud  = {stem = "Hud",  runtime_bearing = true, module_path = "x"},
                    hud2 = {stem = "Hud2", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:c", script_id = "ctl",
                             game_object_id = "ctlId", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:h", script_id = "hud",
                             game_object_id = "hudId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                            {instance_id = "A:h2", script_id = "hud2",
                             game_object_id = "hud2Id", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        -- Controller present in the workspace; both Huds miss.
                        references = {
                            {["from"] = "A:c", field = "hud", index = nil,
                             target_kind = "component", target_ref = "A:h"},
                        },
                        -- lifecycle_order: Hud2 BEFORE Hud, so the batch must
                        -- Awake Hud2 first (proves intra-batch ordering, not
                        -- defer/resolve order which is Hud then Hud2).
                        lifecycle_order = {"A:c", "A:h2", "A:h"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            -- Only the Controller host exists in workspace at boot.
            local ctlGo = {Name = "Ctl", _sceneRuntimeId = "ctlId", _children = {}}
            local services = servicesFor(plan, {ctl = Controller, hud = Hud, hud2 = Hud2}, {ctlId = ctlGo})

            -- awaitUiHost resolves both clones (they've landed by now).
            local hudClone  = {Name = "HUD",  _sceneRuntimeId = "hudId",  _children = {}}
            local hud2Clone = {Name = "HUD2", _sceneRuntimeId = "hud2Id", _children = {}}
            services.awaitUiHost = function(id)
                if id == "hudId" then return hudClone end
                if id == "hud2Id" then return hud2Clone end
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()  -- flush all Starts (sync batch + late UI batch)

            for _, e in ipairs(events) do print(e) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # BLOCKING #2: the inbound ref was back-patched -- Controller.Start
        # sees the built Hud (pre-fix: nil).
        assert "Controller.Start hud=HUD" in lines, out
        # BLOCKING #1: the deferred set ran as ONE batch -- both Awakes
        # precede both Starts.
        i_h_awake = lines.index("Hud.Awake")
        i_h2_awake = lines.index("Hud2.Awake")
        i_h_start = lines.index("Hud.Start")
        i_h2_start = lines.index("Hud2.Start")
        assert max(i_h_awake, i_h2_awake) < min(i_h_start, i_h2_start), out
        # BLOCKING #1: intra-batch lifecycle_order honored -- Hud2 (earlier in
        # lifecycle_order) Awakes before Hud.
        assert i_h2_awake < i_h_awake, out


class TestServerNoResolverOneShot:
    """Fix-round-1 MAJOR #3. When ``awaitUiHost`` is absent (server domain /
    any partition without the client host-surface helper), a UI-owned miss
    must NOT defer-then-never-build; it falls back to the pre-slice
    synchronous one-shot build (even with a nil gameObject)."""

    def test_no_resolver_builds_one_shot(self):
        scenario = textwrap.dedent("""\
            local events = {}

            local Hud = {} ; Hud.__index = Hud
            function Hud.new(_) return setmetatable({}, Hud) end
            function Hud:Awake()
                table.insert(events, "Hud.Awake go=" .. tostring(self.gameObject))
            end
            function Hud:Start() table.insert(events, "Hud.Start") end

            local plan = {
                modules = {
                    hud = {stem = "Hud", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "hud",
                             game_object_id = "hudId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            -- workspaceFind misses; NO awaitUiHost on services (server path).
            local services = servicesFor(plan, {hud = Hud}, {})
            -- Ensure no resolver is present.
            services.awaitUiHost = nil

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()

            for _, e in ipairs(events) do print(e) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # MAJOR #3: built one-shot with a nil gameObject (pre-slice
        # behaviour), NOT silently never-built.
        assert "Hud.Awake go=nil" in lines, out
        assert "Hud.Start" in lines, out


class TestDeferredR2Fixes:
    """Fix-round-2 findings (codex r2 + Claude r2):

      * BLOCKING: a deferred component's OUTBOUND ``component``-kind ref to a
        SYNCHRONOUSLY-built peer must resolve (mirror of the inbound bug). Pre
        r2 it stayed nil forever (only ``builtByInstanceId`` was consulted).
      * BLOCKING: per-host completion -- a host that never resolves must NOT
        delay a host that resolves promptly (the prior global barrier stalled
        every resolved peer up to the 10s timeout).
      * MAJOR: planner ``enabled=false`` / ``tag`` must be reapplied in the
        deferred path -- pre r2 an authored-disabled deferred UI woke ENABLED
        (ran OnEnable/Start) and its tag never entered ``_byTag``.
    """

    def test_deferred_outbound_ref_to_sync_peer_resolves(self):
        # A deferred UI ``Hud`` holds a serialized ref to a SYNCHRONOUSLY-built
        # non-UI ``Manager``. Pre r2 the deferred wire pass only saw the
        # batch-built set, so ``Hud.manager`` was nil at Start.
        scenario = textwrap.dedent("""\
            local events = {}

            local Manager = {} ; Manager.__index = Manager
            function Manager.new(_) return setmetatable({_tag = "MGR"}, Manager) end
            function Manager:Awake() table.insert(events, "Manager.Awake") end
            function Manager:Start() table.insert(events, "Manager.Start") end

            local Hud = {} ; Hud.__index = Hud
            function Hud.new(_) return setmetatable({manager = nil}, Hud) end
            function Hud:Awake()
                table.insert(events,
                    "Hud.Awake manager=" .. tostring(self.manager and self.manager._tag))
            end
            function Hud:Start()
                table.insert(events,
                    "Hud.Start manager=" .. tostring(self.manager and self.manager._tag))
            end

            local plan = {
                modules = {
                    mgr = {stem = "Manager", runtime_bearing = true, module_path = "x"},
                    hud = {stem = "Hud", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:m", script_id = "mgr",
                             game_object_id = "mgrId", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:h", script_id = "hud",
                             game_object_id = "hudId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        -- Hud (deferred) -> Manager (synchronous peer).
                        references = {
                            {["from"] = "A:h", field = "manager", index = nil,
                             target_kind = "component", target_ref = "A:m"},
                        },
                        lifecycle_order = {"A:m", "A:h"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            -- Only the Manager host exists at boot; the Hud host misses.
            local mgrGo = {Name = "Mgr", _sceneRuntimeId = "mgrId", _children = {}}
            local services = servicesFor(plan, {mgr = Manager, hud = Hud}, {mgrId = mgrGo})
            local hudClone = {Name = "HUD", _sceneRuntimeId = "hudId", _children = {}}
            services.awaitUiHost = function(id)
                if id == "hudId" then return hudClone end
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()

            for _, e in ipairs(events) do print(e) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # BLOCKING: the deferred component's outbound ref to the sync peer is
        # bound (pre r2: ``Hud.Awake manager=nil`` / ``Hud.Start manager=nil``).
        assert "Hud.Awake manager=MGR" in lines, out
        assert "Hud.Start manager=MGR" in lines, out

    def test_disabled_deferred_does_not_run_onenable_and_tag_registered(self):
        # A deferred UI component authored ``enabled=false`` with a ``tag``.
        # Pre r2 it woke ENABLED (ran OnEnable/Start) and its tag never entered
        # ``_byTag`` (findGameObjectsWithTag returned nothing).
        scenario = textwrap.dedent("""\
            local events = {}

            local Hud = {} ; Hud.__index = Hud
            function Hud.new(_) return setmetatable({}, Hud) end
            function Hud:Awake() table.insert(events, "Hud.Awake") end
            function Hud:OnEnable() table.insert(events, "Hud.OnEnable") end
            function Hud:Start() table.insert(events, "Hud.Start") end

            local plan = {
                modules = {
                    hud = {stem = "Hud", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:h", script_id = "hud",
                             game_object_id = "hudId", active = true,
                             enabled = false, tag = "HudTag", config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {},
                        lifecycle_order = {"A:h"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            local services = servicesFor(plan, {hud = Hud}, {})
            local hudClone = {Name = "HUD", _sceneRuntimeId = "hudId", _children = {}}
            services.awaitUiHost = function(id)
                if id == "hudId" then return hudClone end
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()

            local tagged = engine:findGameObjectsWithTag("HudTag")
            print("TAGCOUNT=" .. tostring(#tagged))
            for _, e in ipairs(events) do print(e) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # MAJOR: enabled=false honored -> Awake runs but OnEnable/Start do NOT.
        assert "Hud.Awake" in lines, out
        assert "Hud.OnEnable" not in lines, out
        assert "Hud.Start" not in lines, out
        # MAJOR: authored tag registered (pre r2: TAGCOUNT=0).
        assert "TAGCOUNT=1" in lines, out

    def test_resolved_host_completes_without_waiting_for_unresolved_peer(self):
        # Two deferred components on DIFFERENT hosts. Host A resolves
        # immediately; host B NEVER resolves (its resolver coroutine stays
        # parked, like awaitUiHost waiting on a clone that never lands until
        # the 10s timeout). With per-host completion, host A's component must
        # Awake/Start WITHOUT waiting for B. Against the pre-r2 global barrier
        # (remaining==0 across ALL pending), A would NOT complete until B's
        # coroutine finished -- so this assertion fails pre-fix.
        scenario = textwrap.dedent("""\
            local events = {}

            local HudA = {} ; HudA.__index = HudA
            function HudA.new(_) return setmetatable({}, HudA) end
            function HudA:Awake() table.insert(events, "HudA.Awake") end
            function HudA:Start() table.insert(events, "HudA.Start") end

            local HudB = {} ; HudB.__index = HudB
            function HudB.new(_) return setmetatable({}, HudB) end
            function HudB:Awake() table.insert(events, "HudB.Awake") end
            function HudB:Start() table.insert(events, "HudB.Start") end

            local plan = {
                modules = {
                    a = {stem = "HudA", runtime_bearing = true, module_path = "x"},
                    b = {stem = "HudB", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:a", script_id = "a",
                             game_object_id = "aId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                            {instance_id = "A:b", script_id = "b",
                             game_object_id = "bId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {},
                        lifecycle_order = {"A:a", "A:b"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            local services = servicesFor(plan, {a = HudA, b = HudB}, {})

            -- Real-coroutine task.spawn: each resolver runs in its own thread
            -- and may yield. Host B's resolver parks forever (never resumed),
            -- modelling a clone that never lands within the test window.
            local realSpawn = {}
            function realSpawn.spawn(fn, ...)
                local co = coroutine.create(fn)
                coroutine.resume(co, ...)
                return co
            end
            services.task = setmetatable(realSpawn, {__index = services.task})

            local aClone = {Name = "A", _sceneRuntimeId = "aId", _children = {}}
            services.awaitUiHost = function(id)
                if id == "aId" then return aClone end
                -- Host B: never resolves -- park this coroutine indefinitely.
                coroutine.yield()
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()

            for _, e in ipairs(events) do print(e) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # Host A completed its full lifecycle WITHOUT waiting on host B.
        assert "HudA.Awake" in lines, out
        assert "HudA.Start" in lines, out
        # Host B never resolved -> never built.
        assert "HudB.Awake" not in lines, out
        assert "HudB.Start" not in lines, out


class TestDeferredR3Fixes:
    """Fix-round-3 findings (codex r3 + Claude r3):

      * BLOCKING: the engine registry ``_componentByInstanceId`` was keyed by
        the RAW prefab-local instance_id, so two boot placements of the same
        UI prefab collided -- placement-1's deferred component could bind to
        placement-2's peer. AND ``_unregister`` never cleared registry entries
        (a destroyed component could be bound by a later deferred resolve).
        Fix: placement-scoped registry key + clear on unregister/destroy.
      * MAJOR: cross-host deferred->deferred refs only got eventually
        back-patched, so the earlier-completing host Awoke/Started with a nil
        ref. Fix: dependency-aware batching -- a group waits for the groups it
        references before running its lifecycle (KEEPING the r2 property that
        UNRELATED never-resolving hosts don't stall a resolved host); a
        never-resolving dependency times out -> proceed with nil + warn.
    """

    @staticmethod
    def _mknode_helper() -> str:
        # A prefab-clone descendant node with GetDescendants / Get/SetAttribute
        # mirroring the production attribute stamp into ``_sceneRuntimeId``.
        return textwrap.dedent("""\
            local function _mkNode(name, sri)
                local n = {Name = name, _sceneRuntimeId = sri,
                           _children = {}, _builtins = {}, Parent = nil}
                n.SetAttribute = function(self, k, v)
                    if k == "_SceneRuntimeId" then self._sceneRuntimeId = v end
                end
                n.GetAttribute = function(self, k)
                    if k == "_SceneRuntimeId" then return self._sceneRuntimeId end
                end
                n.GetDescendants = function(self)
                    local out = {}
                    local function walk(node)
                        for _, child in pairs(node._children or {}) do
                            table.insert(out, child)
                            walk(child)
                        end
                    end
                    walk(self)
                    return out
                end
                return n
            end
        """)

    def test_multi_placement_binds_each_to_its_own_clone_peer(self):
        # Two boot placements (PA, PB) of the SAME UI prefab. The prefab hosts
        # two UI-owned instances on DIFFERENT hosts: ``Hud`` (host
        # ``pfb:hudhost``, holds an intra-prefab component ref to ``Peer``) and
        # ``Peer`` (host ``pfb:peerhost``). Every host clone misses at boot so
        # all four deferred groups (PA/PB x Hud/Peer) wait on ``awaitUiHost``.
        #
        # To FORCE the placement-collision the fix targets, the resolution is
        # interleaved: BOTH Peer hosts resolve first (so the engine registry's
        # raw ``pfb:peer`` key ends up holding the LAST-registered Peer -- PB's
        # -- pre-fix), THEN both Hud hosts resolve and wire their ref. Pre-fix
        # (raw registry key) both Huds read the same raw key and bind PB's
        # Peer; with the placement-scoped key each Hud binds ITS OWN Peer.
        scenario = textwrap.dedent("""\
            local binds = {}
            local parked = {}   -- queue of parked Hud-host resolver threads

            local Hud = {} ; Hud.__index = Hud
            function Hud.new(_) return setmetatable({peer = nil}, Hud) end
            function Hud:Awake()
                local mine = self.gameObject and self.gameObject.Name
                local got = self.peer and self.peer.gameObject
                          and self.peer.gameObject.Name
                binds[tostring(mine)] = tostring(got)
            end

            local Peer = {} ; Peer.__index = Peer
            function Peer.new(_) return setmetatable({}, Peer) end

            local plan = {
                modules = {
                    hud  = {stem = "Hud",  runtime_bearing = true, module_path = "x"},
                    peer = {stem = "Peer", runtime_bearing = true, module_path = "x"},
                },
                scenes = {},
                prefabs = {
                    ["pfb"] = {
                        name = "Pf",
                        instances = {
                            {instance_id = "pfb:hud", script_id = "hud",
                             game_object_id = "pfb:hudhost", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                            {instance_id = "pfb:peer", script_id = "peer",
                             game_object_id = "pfb:peerhost", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {
                            {["from"] = "pfb:hud", field = "peer", index = nil,
                             target_kind = "component", target_ref = "pfb:peer"},
                        },
                        lifecycle_order = {"pfb:peer", "pfb:hud"},
                    },
                },
                scene_prefab_placements = {
                    {placement_id = "PA", prefab_id = "pfb",
                     active = true, enabled = true},
                    {placement_id = "PB", prefab_id = "pfb",
                     active = true, enabled = true},
                },
                domain_overrides = {},
            }

            -- Per-placement clones. ``_findUnboundClonePerPrefab`` resolves a
            -- distinct clone per placement (root_goid == the prefab root); the
            -- per-instance host descendants carry the prefab-local host SRIs.
            -- The prefab root_goid inferred by _findUnboundClonePerPrefab is
            -- the first instance with no parent -> ``pfb:hudhost`` (the Hud
            -- instance's host). So the clone ROOT carries that SRI; the Peer
            -- host is a sibling descendant. (resolveCloneChild is stubbed to
            -- miss so every instance defers regardless.)
            local function _mkClone(hudName, peerName)
                local hudHost = {Name = hudName, _sceneRuntimeId = "pfb:hudhost",
                                 _children = {}}
                local peerHost = {Name = peerName, _sceneRuntimeId = "pfb:peerhost",
                                  _children = {}}
                for _, n in ipairs({hudHost, peerHost}) do
                    n.SetAttribute = function(self, k, v)
                        if k == "_SceneRuntimeId" then self._sceneRuntimeId = v end
                    end
                    n.GetAttribute = function(self, k)
                        if k == "_SceneRuntimeId" then return self._sceneRuntimeId end
                    end
                    n.GetDescendants = function(self) return {} end
                end
                return hudHost, peerHost
            end
            local hudA, peerA = _mkClone("hudA", "peerA")
            local hudB, peerB = _mkClone("hudB", "peerB")
            -- workspace returns the two distinct placement roots (Hud hosts).
            local boundCount = 0
            workspace = {GetDescendants = function(self) return {hudA, hudB} end}

            local services = servicesFor(plan, {hud = Hud, peer = Peer}, {})
            services.resolveCloneChild = function(clone, goid) return nil end
            services.getInstanceId = function(inst) return inst and inst._sceneRuntimeId end

            -- Real-coroutine spawn so the Hud-host resolvers can park while the
            -- Peer hosts resolve first.
            local realSpawn = {}
            function realSpawn.spawn(fn, ...)
                local co = coroutine.create(fn)
                coroutine.resume(co, ...)
                return co
            end
            services.task = setmetatable(realSpawn, {__index = services.task})

            -- Peer hosts resolve immediately; Hud hosts PARK (queued) so both
            -- Peers register before either Hud wires.
            local cloneByHud = {["PA:pfb:hudhost"] = hudA, ["PB:pfb:hudhost"] = hudB}
            local cloneByPeer = {["PA:pfb:peerhost"] = peerA, ["PB:pfb:peerhost"] = peerB}
            services.awaitUiHost = function(scopedId)
                if cloneByPeer[scopedId] then return cloneByPeer[scopedId] end
                if cloneByHud[scopedId] then
                    table.insert(parked,
                        {thread = coroutine.running(), id = scopedId})
                    coroutine.yield()
                    return cloneByHud[scopedId]
                end
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            -- Both Peers have registered; now resume the parked Hud resolvers
            -- (each returns its host clone and completes its group).
            for _, p in ipairs(parked) do
                coroutine.resume(p.thread)
            end
            runDeferred()

            print("PA=" .. tostring(binds["hudA"]))
            print("PB=" .. tostring(binds["hudB"]))
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # Each placement's Hud bound to ITS OWN clone's Peer (no cross-
        # placement collision). Pre-fix both bound to peerB (raw-key last
        # writer wins) -> PA=peerB.
        assert "PA=peerA" in lines, out
        assert "PB=peerB" in lines, out

    def test_unregister_clears_registry_so_destroyed_comp_not_bound(self):
        # A deferred component A holds a ref to a deferred component B on a
        # DIFFERENT host. B is built (registers in the engine map), then
        # DESTROYED before A's group wires. Pre-fix the destroyed B lingered in
        # ``_componentByInstanceId`` and A bound the dead comp. With unregister
        # clearing the entry, A's ref to the destroyed B resolves to nil.
        scenario = textwrap.dedent("""\
            local events = {}

            local A = {} ; A.__index = A
            function A.new(_) return setmetatable({b = nil}, A) end
            function A:Awake()
                events.aBound = (self.b ~= nil)
            end

            local B = {} ; B.__index = B
            function B.new(_) return setmetatable({}, B) end

            local plan = {
                modules = {
                    a = {stem = "A", runtime_bearing = true, module_path = "x"},
                    b = {stem = "B", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    S = {
                        instances = {
                            {instance_id = "S:a", script_id = "a",
                             game_object_id = "aId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                            {instance_id = "S:b", script_id = "b",
                             game_object_id = "bId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {
                            {["from"] = "S:a", field = "b", index = nil,
                             target_kind = "component", target_ref = "S:b"},
                        },
                        lifecycle_order = {"S:b", "S:a"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            local services = servicesFor(plan, {a = A, b = B}, {})
            local aClone = {Name = "A", _sceneRuntimeId = "aId", _children = {}}
            local bClone = {Name = "B", _sceneRuntimeId = "bId", _children = {}}

            -- B's group resolves FIRST and builds + registers B; we then
            -- DESTROY B before A's group resolves. Drive ordering by hand: B's
            -- resolver returns its clone immediately; A's resolver destroys B
            -- (already built, since dependency-ordering completes B first) and
            -- then returns A's clone.
            services.awaitUiHost = function(id)
                if id == "bId" then return bClone end
                if id == "aId" then
                    -- A depends on B, so B's group has completed + registered
                    -- B by now. Destroy B's component; ``_unregister`` must
                    -- clear the engine registry entry so A's pending ref to B
                    -- resolves to nil rather than the dead component.
                    local found = engineRef:findObjectOfType("B")
                    if found then engineRef:destroy(found) end
                    return aClone
                end
                return nil
            end

            engineRef = SceneRuntime.new(services, plan)
            engineRef:start(nil)
            runDeferred()

            print("A_BOUND=" .. tostring(events.aBound))
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # A's ref to the DESTROYED B resolved to nil (registry entry cleared),
        # NOT to the dead component. Pre-fix: A_BOUND=true (bound the corpse).
        assert "A_BOUND=false" in lines, out

    def test_cross_host_deferred_dep_ordered_nonnil_unrelated_independent(self):
        # Three deferred UI components on THREE different hosts:
        #   A -> B (A holds a component ref to B; both deferred, diff hosts)
        #   C  is UNRELATED and NEVER resolves (parked resolver coroutine).
        # Dependency-aware batching must order B before A so A's Awake/Start
        # see B NON-NIL (codex r3 MAJOR). AND the r2 property must hold: C
        # (unrelated, never-resolving) must NOT stall A or B.
        scenario = textwrap.dedent("""\
            local events = {}

            local A = {} ; A.__index = A
            function A.new(_) return setmetatable({b = nil}, A) end
            function A:Awake()
                events.aAwakeB = self.b and self.b._tag or "nil"
            end
            function A:Start()
                events.aStartB = self.b and self.b._tag or "nil"
            end

            local B = {} ; B.__index = B
            function B.new(_) return setmetatable({_tag = "B"}, B) end
            function B:Awake() events.bAwake = true end
            function B:Start() events.bStart = true end

            local C = {} ; C.__index = C
            function C.new(_) return setmetatable({}, C) end
            function C:Awake() events.cAwake = true end

            local plan = {
                modules = {
                    a = {stem = "A", runtime_bearing = true, module_path = "x"},
                    b = {stem = "B", runtime_bearing = true, module_path = "x"},
                    c = {stem = "C", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    S = {
                        instances = {
                            {instance_id = "S:a", script_id = "a",
                             game_object_id = "aId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                            {instance_id = "S:b", script_id = "b",
                             game_object_id = "bId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                            {instance_id = "S:c", script_id = "c",
                             game_object_id = "cId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {
                            {["from"] = "S:a", field = "b", index = nil,
                             target_kind = "component", target_ref = "S:b"},
                        },
                        -- lifecycle_order puts A's host group first in defer
                        -- order; dependency ordering must still complete B
                        -- before A despite this.
                        lifecycle_order = {"S:a", "S:b", "S:c"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            local services = servicesFor(plan, {a = A, b = B, c = C}, {})

            -- Real-coroutine task.spawn so C's resolver can park indefinitely.
            local realSpawn = {}
            function realSpawn.spawn(fn, ...)
                local co = coroutine.create(fn)
                coroutine.resume(co, ...)
                return co
            end
            services.task = setmetatable(realSpawn, {__index = services.task})

            local aClone = {Name = "A", _sceneRuntimeId = "aId", _children = {}}
            local bClone = {Name = "B", _sceneRuntimeId = "bId", _children = {}}
            services.awaitUiHost = function(id)
                if id == "aId" then return aClone end
                if id == "bId" then return bClone end
                -- Host C: never resolves -- park forever.
                coroutine.yield()
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()

            print("A_AWAKE_B=" .. tostring(events.aAwakeB))
            print("A_START_B=" .. tostring(events.aStartB))
            print("B_AWAKE=" .. tostring(events.bAwake))
            print("C_AWAKE=" .. tostring(events.cAwake))
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "DONE" in lines, out
        # A depended on B -> B completed first -> A saw B non-nil at BOTH
        # Awake and Start. Pre-fix (eventual back-patch only): nil at Awake.
        assert "A_AWAKE_B=B" in lines, out
        assert "A_START_B=B" in lines, out
        assert "B_AWAKE=true" in lines, out
        # r2 property preserved: C (unrelated, never-resolving) did NOT stall
        # A or B; A and B completed. C itself never built.
        assert "C_AWAKE=nil" in lines, out

    def test_never_resolving_dependency_times_out_then_proceeds_nil(self):
        # A depends on B, but B's host NEVER resolves (parked resolver). The
        # dependency wait must TIME OUT and let A proceed with a nil ref + warn
        # -- no infinite hang. (Harness: the iteration cap bounds the wait
        # since the mock clock doesn't advance during task.wait.)
        scenario = textwrap.dedent("""\
            local events = {}

            local A = {} ; A.__index = A
            function A.new(_) return setmetatable({b = nil}, A) end
            function A:Awake() events.aAwakeB = self.b and "set" or "nil" end
            function A:Start() events.aStart = true end

            local B = {} ; B.__index = B
            function B.new(_) return setmetatable({_tag = "B"}, B) end
            function B:Awake() events.bAwake = true end

            local plan = {
                modules = {
                    a = {stem = "A", runtime_bearing = true, module_path = "x"},
                    b = {stem = "B", runtime_bearing = true, module_path = "x"},
                },
                scenes = {
                    S = {
                        instances = {
                            {instance_id = "S:a", script_id = "a",
                             game_object_id = "aId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                            {instance_id = "S:b", script_id = "b",
                             game_object_id = "bId", active = true,
                             enabled = true, config = {},
                             instance_owner_is_ui = true},
                        },
                        references = {
                            {["from"] = "S:a", field = "b", index = nil,
                             target_kind = "component", target_ref = "S:b"},
                        },
                        lifecycle_order = {"S:a", "S:b"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }

            local services = servicesFor(plan, {a = A, b = B}, {})

            local realSpawn = {}
            function realSpawn.spawn(fn, ...)
                local co = coroutine.create(fn)
                coroutine.resume(co, ...)
                return co
            end
            services.task = setmetatable(realSpawn, {__index = services.task})
            -- Shrink the wait so the harness iteration cap is reached fast.
            -- (Use task.wait that returns immediately; the iteration cap is the
            -- harness-safe bound the runtime applies.)

            local aClone = {Name = "A", _sceneRuntimeId = "aId", _children = {}}
            services.awaitUiHost = function(id)
                if id == "aId" then return aClone end
                -- Host B: never resolves.
                coroutine.yield()
                return nil
            end

            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()

            print("A_AWAKE_B=" .. tostring(events.aAwakeB))
            print("A_START=" .. tostring(events.aStart))
            print("B_AWAKE=" .. tostring(events.bAwake))
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        # No hang (the 15s subprocess timeout would have killed it) AND A
        # proceeded after the dependency timeout with a nil ref.
        assert "DONE" in lines, out
        assert "A_AWAKE_B=nil" in lines, out
        assert "A_START=true" in lines, out
        # B never built (its host never landed).
        assert "B_AWAKE=nil" in lines, out


class TestAwaitUiHostResolverDirect:
    """Fix-round-1 MAJOR #5 + test-coverage #6. Drive the REAL emitted
    ``awaitUiHost`` body inside a true coroutine harness, exercising the
    connect-first scan, the DescendantAdded resume, and the timeout path."""

    def _run_await(self, body: str):
        await_src = _await_ui_host_source()
        script = textwrap.dedent("""\
            -- Minimal mock Roblox surface for awaitUiHost.
            local _delays = {}
            local _clock = 0
            local task = {}
            function task.spawn(fn, ...)
                if type(fn) == "thread" then
                    coroutine.resume(fn, ...)
                else
                    coroutine.resume(coroutine.create(fn), ...)
                end
            end
            function task.delay(secs, fn, ...)
                table.insert(_delays, {fireAt = _clock + secs, fn = fn, args = {...}})
            end
            local function advanceTime(dt)
                _clock = _clock + dt
                local fired = {}
                for i = #_delays, 1, -1 do
                    if _delays[i].fireAt <= _clock then
                        table.insert(fired, _delays[i]); table.remove(_delays, i)
                    end
                end
                for _, e in ipairs(fired) do e.fn(table.unpack(e.args)) end
            end

            -- Mock PlayerGui: a descendant list + a DescendantAdded signal.
            local function mkSignal()
                local s = {_c = {}}
                function s:Connect(fn)
                    local id = tostring(fn); s._c[id] = fn
                    return {Disconnect = function() s._c[id] = nil end}
                end
                function s:fire(x) for _, fn in pairs(s._c) do fn(x) end end
                return s
            end
            local function mkGui(id)
                return {GetAttribute = function(self, n)
                    if n == "_SceneRuntimeId" then return id end
                end}
            end
            local PlayerGui = {_descs = {}, DescendantAdded = mkSignal()}
            function PlayerGui:GetDescendants() return self._descs end
            local function workspaceFind(id) return nil end

        """) + await_src + "\n" + body
        with tempfile.NamedTemporaryFile(
            suffix=".luau", mode="w", delete=False,
        ) as f:
            f.write(script)
            path = f.name
        try:
            r = subprocess.run(
                ["luau", path], capture_output=True, text=True, timeout=15,
            )
            return r.returncode, r.stdout, r.stderr
        finally:
            Path(path).unlink(missing_ok=True)

    def test_initial_scan_hit(self):
        # Clone already present -> resolves on the initial scan, no timeout.
        body = textwrap.dedent("""\
            PlayerGui._descs = {mkGui("other"), mkGui("hudId")}
            local result
            local co = coroutine.create(function()
                result = awaitUiHost("hudId")
            end)
            coroutine.resume(co)
            print("RESULT=" .. tostring(result and result:GetAttribute("_SceneRuntimeId")))
            print("DONE")
        """)
        rc, out, err = self._run_await(body)
        assert rc == 0, f"{err}\n{out}"
        assert "RESULT=hudId" in out, out

    def test_resolves_via_descendant_added_after_miss(self):
        # Initial scan misses; the clone arrives via DescendantAdded -> the
        # connect-first wiring catches it and resumes the waiter.
        body = textwrap.dedent("""\
            PlayerGui._descs = {mkGui("other")}
            local result
            local co = coroutine.create(function()
                result = awaitUiHost("hudId")
            end)
            coroutine.resume(co)  -- yields, waiting
            -- Now the clone lands.
            PlayerGui.DescendantAdded:fire(mkGui("hudId"))
            print("RESULT=" .. tostring(result and result:GetAttribute("_SceneRuntimeId")))
            print("DONE")
        """)
        rc, out, err = self._run_await(body)
        assert rc == 0, f"{err}\n{out}"
        assert "RESULT=hudId" in out, out

    def test_timeout_returns_nil(self):
        # Clone never lands -> the 10s timeout wakes the waiter with nil.
        body = textwrap.dedent("""\
            PlayerGui._descs = {mkGui("other")}
            local result = "UNSET"
            local co = coroutine.create(function()
                result = awaitUiHost("hudId")
            end)
            coroutine.resume(co)  -- yields, waiting
            advanceTime(11)       -- fire the timeout
            print("RESULT=" .. tostring(result))
            print("DONE")
        """)
        rc, out, err = self._run_await(body)
        assert rc == 0, f"{err}\n{out}"
        assert "RESULT=nil" in out, out
