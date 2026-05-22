"""PR4: behavioral tests for converter/runtime/scene_runtime.luau.

These tests drive the host runtime through the standalone ``luau``
interpreter with a mock service surface. Each test embeds a small Lua
harness, points it at the production ``scene_runtime.luau``, and
asserts on stdout markers. Skips cleanly when ``luau`` is absent so
CI environments without it don't fail.

Covered (per the design doc PR4 test matrix):
  * 2-MonoBehaviour synthetic scene wired end-to-end through the host.
  * Reference-cycle fixture (mutual peer refs do not loop forever).
  * Lifecycle order: ``new`` -> inject -> ``Awake`` -> ``OnEnable`` ->
    ``Start`` (next tick) -> ``Update``.
  * ``FixedUpdate`` fires on a fixed-step accumulator, not per-tick.
  * ``addComponent`` registers + runs the lifecycle.
  * ``findObjectOfType`` returns inactive objects.
  * ``host.invoke`` cancels on owning component's ``OnDestroy``.
  * ``host.destroy(parent)`` walks DFS deepest-first; idempotent.
  * ``GetComponent`` fallback: peer module hit + Roblox built-in hit.
  * ``host.connect`` lifecycle scoping: dispatch only while
    ``active && enabled``; flipping ``enabled`` re-arms; ``OnDestroy``
    disconnects.
  * Cross-domain refs inject ``nil`` + log + the edge is countable.
  * ``instantiatePrefab`` lifecycle.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


HOST_RUNTIME_PATH = (
    Path(__file__).parent.parent / "runtime" / "scene_runtime.luau"
)


def _luau_available() -> bool:
    return shutil.which("luau") is not None


pytestmark = pytest.mark.skipif(
    not _luau_available() or not HOST_RUNTIME_PATH.exists(),
    reason="needs standalone luau interpreter + host runtime file",
)


# ---------------------------------------------------------------------------
# Shared harness preamble: loads the host runtime and exposes the mock
# Roblox surface. Each test appends scenario code.
# ---------------------------------------------------------------------------

def _harness_preamble() -> str:
    # luau standalone has no loadfile -- read the host runtime
    # source in Python and embed it into the harness as a long string.
    host_source = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in host_source or f"[{delim}[" in host_source:
        delim += "="
    embedded = f"[{delim}[\n{host_source}\n]{delim}]"
    return textwrap.dedent(f"""\
        -- Harness preamble: mocks the slice of the Roblox API the host
        -- runtime touches so tests can run under standalone luau.
        -- Subsequent scenario code sees SceneRuntime, servicesFor,
        -- advanceTime, runDeferred, mockSignal, logs as
        -- top-level locals in the same chunk.

        local HOST_RUNTIME_SOURCE = {embedded}
        local SceneRuntime
        do
            local chunk, err = loadstring(HOST_RUNTIME_SOURCE, "scene_runtime")
            assert(chunk, "load host runtime failed: " .. tostring(err))
            SceneRuntime = chunk()
        end
""") + _HARNESS_BODY


_HARNESS_BODY = """local _deferred = {}
local _delays = {}
local _cancelled = {}
local _nextHandle = 0
local function newHandle()
    _nextHandle = _nextHandle + 1
    return {handle = _nextHandle}
end
local task = {}
function task.spawn(fn, ...)
    local h = newHandle()
    local ok, err = pcall(fn, ...)
    if not ok then warn("[mocktask spawn] " .. tostring(err)) end
    return h
end
function task.defer(fn, ...)
    local h = newHandle()
    table.insert(_deferred, {handle = h, fn = fn, args = {...}})
    return h
end
local _clock = 0
function task.delay(secs, fn, ...)
    local h = newHandle()
    table.insert(_delays, {
        handle = h, fn = fn, args = {...},
        fireAt = _clock + secs,
    })
    return h
end
function task.wait(secs) return secs or 0 end
function task.cancel(h)
    if type(h) ~= "table" or h.handle == nil then return end
    _cancelled[h.handle] = true
    for i = #_delays, 1, -1 do
        if _delays[i].handle == h.handle then table.remove(_delays, i) end
    end
    for i = #_deferred, 1, -1 do
        if _deferred[i].handle == h.handle then table.remove(_deferred, i) end
    end
end

local function advanceTime(dt)
    _clock = _clock + dt
    local fired = {}
    for i = #_delays, 1, -1 do
        if _delays[i].fireAt <= _clock then
            table.insert(fired, _delays[i])
            table.remove(_delays, i)
        end
    end
    for _, entry in ipairs(fired) do
        if not _cancelled[entry.handle] then
            pcall(entry.fn, table.unpack(entry.args))
        end
    end
end
local function runDeferred()
    local snap = _deferred
    _deferred = {}
    for _, entry in ipairs(snap) do
        if not _cancelled[entry.handle] then
            pcall(entry.fn, table.unpack(entry.args))
        end
    end
end

local function mockSignal()
    local sig = { _conns = {}, _connId = 0 }
    function sig:Connect(fn)
        sig._connId = sig._connId + 1
        local id = sig._connId
        sig._conns[id] = fn
        local conn = {}
        function conn:Disconnect()
            sig._conns[id] = nil
        end
        return conn
    end
    function sig:fire(...)
        for _, fn in pairs(sig._conns) do
            fn(...)
        end
    end
    return sig
end

local logs = {}
local function logWarn(...)
    local parts = {...}
    for i, p in ipairs(parts) do parts[i] = tostring(p) end
    table.insert(logs, table.concat(parts, " "))
end

local function servicesFor(plan, modules, instances)
    return {
        task = task,
        warn = logWarn,
        resolveModule = function(scriptId, modulePath)
            return modules[scriptId]
        end,
        workspaceFind = function(sceneRuntimeId)
            return instances[sceneRuntimeId]
        end,
        findFirstChildWhichIsA = function(inst, class)
            if not inst or not inst._builtins then return nil end
            return inst._builtins[class]
        end,
        heartbeat = mockSignal(),
        fixedStep = 0.02,
        now = function() return _clock end,
        getInstanceId = function(inst)
            return inst and inst._sceneRuntimeId
        end,
        clonePrefabTemplate = function(prefabId, parent, cframe)
            return nil
        end,
        resolveCloneChild = function(clone, gameObjectId)
            return (clone and clone._children
                    and clone._children[gameObjectId]) or clone
        end,
        collectDescendantIds = function(inst)
            local out = {}
            local function walk(node)
                if node._children then
                    for _, child in pairs(node._children) do
                        walk(child)
                    end
                end
                table.insert(out, node._sceneRuntimeId)
            end
            walk(inst)
            return out
        end,
        collectSubtreeIdsWithParents = function(inst)
            -- R4-P1.2: DFS preorder + parent id so the setActive
            -- cascade can recompute activeInHierarchy correctly across
            -- a multi-level tree with mixed-authored activeSelf flags.
            local out = {}
            local function walk(node, parentId)
                local id = node._sceneRuntimeId
                table.insert(out, {id = id, parentId = parentId})
                if node._children then
                    for _, child in pairs(node._children) do
                        walk(child, id)
                    end
                end
            end
            walk(inst, nil)
            return out
        end,
        destroyInstance = function(inst) end,
    }
end

"""

def _run_scenario(scenario_body: str) -> tuple[int, str, str]:
    """Stitch the preamble + scenario, execute, return (rc, stdout, stderr)."""
    script = _harness_preamble() + "\n" + scenario_body + "\n"
    with tempfile.NamedTemporaryFile(
        suffix=".luau", mode="w", delete=False,
    ) as f:
        f.write(script)
        path = f.name
    try:
        result = subprocess.run(
            ["luau", path], capture_output=True, text=True, timeout=15,
        )
        return result.returncode, result.stdout, result.stderr
    finally:
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 2-MonoBehaviour synthetic scene wired end-to-end
# ---------------------------------------------------------------------------

class TestTwoMonoBehaviourScene:

    def test_lifecycle_order_new_inject_awake_enable_start_update(self):
        scenario = textwrap.dedent("""\
            local order = {}
            local Foo = {}
            Foo.__index = Foo
            function Foo.new(config)
                table.insert(order, "Foo.new")
                local self = setmetatable({}, Foo)
                self._config = config
                return self
            end
            function Foo:Awake()
                table.insert(order, "Foo.Awake")
                assert(self.host ~= nil, "host must be bound before Awake")
                assert(self.gameObject ~= nil, "go must be bound")
            end
            function Foo:OnEnable() table.insert(order, "Foo.OnEnable") end
            function Foo:Start() table.insert(order, "Foo.Start") end
            function Foo:Update(dt) table.insert(order, "Foo.Update") end

            local Bar = {}
            Bar.__index = Bar
            function Bar.new(config)
                table.insert(order, "Bar.new")
                return setmetatable({}, Bar)
            end
            function Bar:Awake() table.insert(order, "Bar.Awake") end
            function Bar:OnEnable() table.insert(order, "Bar.OnEnable") end
            function Bar:Start() table.insert(order, "Bar.Start") end

            local plan = {
                modules = {
                    foo = {stem = "Foo", runtime_bearing = true, module_path = "x"},
                    bar = {stem = "Bar", runtime_bearing = true, module_path = "y"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "foo",
                             game_object_id = "go1", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "bar",
                             game_object_id = "go2", active = true,
                             enabled = true, config = {}},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }
            local modules = {foo = Foo, bar = Bar}
            local instances = {
                go1 = {Name = "Go1", _sceneRuntimeId = "go1", _children = {}},
                go2 = {Name = "Go2", _sceneRuntimeId = "go2", _children = {}},
            }
            local services = servicesFor(plan, modules, instances)
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)  -- run both domains
            runDeferred()  -- flush Start
            services.heartbeat:fire(0.016)  -- one heartbeat tick

            for _, x in ipairs(order) do print(x) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}"
        # Expected order: every new() first, then per-instance Awake,
        # then OnEnable, then Start (after defer flush), then Update.
        lines = out.strip().splitlines()
        assert "DONE" in lines
        # ``new`` events come before any ``Awake``.
        first_awake = next((i for i, l in enumerate(lines)
                            if l.endswith(".Awake")), -1)
        last_new = max((i for i, l in enumerate(lines)
                        if l.endswith(".new")), default=-1)
        assert last_new < first_awake, (
            f"all new() must precede any Awake; got {lines}"
        )
        # ``OnEnable`` after ``Awake``, ``Start`` after ``OnEnable``.
        assert lines.index("Foo.Awake") < lines.index("Foo.OnEnable")
        assert lines.index("Foo.OnEnable") < lines.index("Foo.Start")
        assert lines.index("Foo.Start") < lines.index("Foo.Update")


# ---------------------------------------------------------------------------
# Reference-cycle fixture
# ---------------------------------------------------------------------------

class TestReferenceCycle:

    def test_mutual_peer_refs_resolve_without_looping(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                assert(self.peer ~= nil, "peer ref must be wired before Awake")
                -- Cycle: peer's peer is self.
                assert(self.peer.peer == self, "cycle must close")
            end
            local plan = {
                modules = {
                    foo = {stem = "Foo", runtime_bearing = true, module_path = "x"},
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
                            {["from"] = "A:2", field = "peer", index = nil,
                             target_kind = "component", target_ref = "A:1",
                             target_is_ui = false},
                        },
                        lifecycle_order = {"A:1", "A:2"},
                    },
                },
                prefabs = {},
                domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g1 = {Name = "G1", _sceneRuntimeId = "g1", _children = {}},
                g2 = {Name = "G2", _sceneRuntimeId = "g2", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


# ---------------------------------------------------------------------------
# FixedUpdate fixed-step
# ---------------------------------------------------------------------------

class TestFixedUpdate:

    def test_fixed_update_fires_on_step_not_per_tick(self):
        scenario = textwrap.dedent("""\
            local fixedCount = 0
            local updateCount = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Update(dt) updateCount = updateCount + 1 end
            function Foo:FixedUpdate(dt) fixedCount = fixedCount + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g = {Name = "G", _sceneRuntimeId = "g", _children = {}},
            })
            -- fixedStep is 0.02 (default in services).
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            -- Fire 5 heartbeats of 0.016s each = 0.08s elapsed.
            for i = 1, 5 do services.heartbeat:fire(0.016) end
            -- 0.08 / 0.02 = 4 fixed steps; Update fires every tick = 5.
            print("U=" .. updateCount, "F=" .. fixedCount)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "U=5" in out
        assert "F=4" in out


# ---------------------------------------------------------------------------
# addComponent
# ---------------------------------------------------------------------------

class TestAddComponent:

    def test_add_component_registers_and_runs_lifecycle(self):
        scenario = textwrap.dedent("""\
            local awakeCount = 0
            local startCount = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(c) return setmetatable({_c = c}, Foo) end
            function Foo:Awake() awakeCount = awakeCount + 1 end
            function Foo:Start() startCount = startCount + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {}, prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {})
            local engine = SceneRuntime.new(services, plan)
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {}}
            local comp = engine:addComponent(go, "foo", {speed = 5})
            assert(comp ~= nil, "addComponent must return the instance")
            assert(comp._c.speed == 5, "config must reach new()")
            runDeferred()  -- flush Start
            -- findObjectOfType should now see the new component.
            assert(engine:findObjectOfType("Foo") == comp,
                "addComponent must register in global lookup")
            print("A=" .. awakeCount, "S=" .. startCount)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "A=1" in out
        assert "S=1" in out


# ---------------------------------------------------------------------------
# findObjectOfType: sees inactive
# ---------------------------------------------------------------------------

class TestFindObjectOfType:

    def test_finds_inactive_objects(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = false,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g = {Name = "G", _sceneRuntimeId = "g", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            local found = engine:findObjectOfType("Foo")
            assert(found ~= nil, "findObjectOfType must see inactive objects")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "OK" in out


# ---------------------------------------------------------------------------
# host.invoke cancels on OnDestroy
# ---------------------------------------------------------------------------

class TestHostInvokeCancellation:

    def test_invoke_cancels_on_destroy(self):
        scenario = textwrap.dedent("""\
            local fired = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:DoLater() fired = fired + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g = {Name = "G", _sceneRuntimeId = "g", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            local comp = engine:findObjectOfType("Foo")
            engine:invoke(comp, "DoLater", 1.0)
            -- Destroy before the delay fires.
            engine:destroy(comp)
            advanceTime(2.0)
            print("fired=" .. fired)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "fired=0" in out, (
            "invoke must cancel on OnDestroy; got " + out
        )


# ---------------------------------------------------------------------------
# host.destroy DFS deepest-first; idempotent
# ---------------------------------------------------------------------------

class TestRecursiveDestroy:

    def test_destroy_runs_disable_then_destroy_deepest_first_and_is_idempotent(self):
        scenario = textwrap.dedent("""\
            local order = {}
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:OnEnable() end
            function Foo:OnDisable() table.insert(order, self._tag .. ":disable") end
            function Foo:OnDestroy() table.insert(order, self._tag .. ":destroy") end

            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "foo",
                             game_object_id = "parent", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "foo",
                             game_object_id = "child", active = true,
                             enabled = true, config = {}},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local childGo = {Name = "Child", _sceneRuntimeId = "child", _children = {}}
            local parentGo = {Name = "Parent", _sceneRuntimeId = "parent",
                              _children = {child = childGo}}
            local services = servicesFor(plan, {foo = Foo}, {
                parent = parentGo, child = childGo,
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            -- Tag the components so OnDisable/OnDestroy print which one ran.
            for comp, m in pairs(engine._meta) do
                comp._tag = m.gameObjectName or m.gameObjectId
            end
            engine:destroy(parentGo)
            for _, x in ipairs(order) do print(x) end
            -- Second destroy: idempotent.
            local lenBefore = #order
            engine:destroy(parentGo)
            print("len_after=" .. #order)
            print("len_before=" .. lenBefore)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        lines = out.strip().splitlines()
        # ``Child`` events come before ``Parent`` events (deepest-first).
        try:
            child_destroy = lines.index("Child:destroy")
            parent_destroy = lines.index("Parent:destroy")
            assert child_destroy < parent_destroy
            child_disable = lines.index("Child:disable")
            assert child_disable < child_destroy   # disable before destroy
        except ValueError as exc:
            pytest.fail(f"missing expected destroy event in {lines}: {exc}")
        # Idempotent: second destroy did not add more events.
        len_before = int([l for l in lines if l.startswith("len_before=")][0]
                         .split("=")[1])
        len_after = int([l for l in lines if l.startswith("len_after=")][0]
                        .split("=")[1])
        assert len_after == len_before


# ---------------------------------------------------------------------------
# GetComponent peer + Roblox fallback
# ---------------------------------------------------------------------------

class TestGetComponent:

    def test_peer_lookup_returns_module_instance(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            local Bar = {} ; Bar.__index = Bar
            function Bar.new(_) return setmetatable({}, Bar) end
            function Bar:Awake()
                local peer = self:GetComponent("Foo")
                assert(peer ~= nil, "peer GetComponent must hit")
                self._peerTag = peer
            end
            local plan = {
                modules = {
                    foo = {stem = "Foo", runtime_bearing = true, module_path = "x"},
                    bar = {stem = "Bar", runtime_bearing = true, module_path = "y"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "foo",
                             game_object_id = "g", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "bar",
                             game_object_id = "g", active = true,
                             enabled = true, config = {}},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo, bar = Bar}, {
                g = {Name = "G", _sceneRuntimeId = "g", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "OK" in out

    def test_builtin_fallback_for_rigidbody(self):
        # R4-P1.3: GetComponent("Rigidbody") now translates Rigidbody
        # to the Roblox-side class (BasePart) before calling
        # findFirstChildWhichIsA. The fixture's _builtins map must be
        # keyed by the translated Roblox name -- the harness mock does
        # a direct key lookup mimicking how real findFirstChildWhichIsA
        # only knows Roblox class names.
        scenario = textwrap.dedent("""\
            local mockRigidbody = {Name = "FakeRigidbody"}
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                local rb = self:GetComponent("Rigidbody")
                assert(rb == mockRigidbody,
                    "GetComponent fallback must translate Rigidbody->BasePart")
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {
                Name = "G",
                _sceneRuntimeId = "g",
                _children = {},
                _builtins = {BasePart = mockRigidbody},
            }
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "OK" in out


# ---------------------------------------------------------------------------
# host.connect lifecycle scoping
# ---------------------------------------------------------------------------

class TestHostConnect:

    def test_dispatch_gated_on_active_and_enabled(self):
        scenario = textwrap.dedent("""\
            local sig = mockSignal()
            local hits = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                self.host.connect(self, sig, function() hits = hits + 1 end)
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            local comp = engine:findObjectOfType("Foo")

            sig:fire()
            assert(hits == 1, "subscribed callback should fire when enabled")
            engine:setEnabled(comp, false)
            sig:fire()
            assert(hits == 1, "flipping enabled=false must suspend dispatch")
            engine:setEnabled(comp, true)
            sig:fire()
            assert(hits == 2, "re-enabling must reconnect dispatch")

            engine:setActive(go, false)
            sig:fire()
            assert(hits == 2, "setActive(false) suspends dispatch")
            engine:setActive(go, true)
            sig:fire()
            assert(hits == 3, "setActive(true) re-arms")

            -- OnDestroy disconnects all subs.
            engine:destroy(comp)
            sig:fire()
            assert(hits == 3, "OnDestroy must disconnect subs")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


# ---------------------------------------------------------------------------
# Cross-domain reference policy
# ---------------------------------------------------------------------------

class TestCrossDomainPolicy:

    def test_cross_domain_ref_injects_nil_and_logs(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                assert(self.peer == nil,
                    "cross-domain ref must inject nil; got " .. tostring(self.peer))
            end
            local Bar = {} ; Bar.__index = Bar
            function Bar.new(_) return setmetatable({}, Bar) end
            local plan = {
                modules = {
                    foo = {stem = "Foo", runtime_bearing = true,
                           module_path = "x", domain = "client"},
                    bar = {stem = "Bar", runtime_bearing = true,
                           module_path = "y", domain = "server"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "foo",
                             game_object_id = "g1", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "bar",
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
            local services = servicesFor(plan, {foo = Foo, bar = Bar}, {
                g1 = {Name = "G1", _sceneRuntimeId = "g1", _children = {}},
                g2 = {Name = "G2", _sceneRuntimeId = "g2", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            local edges = engine:start(nil)
            assert(#edges == 1, "cross-domain edge must surface in start() return")
            assert(edges[1].from_script == "foo")
            assert(edges[1].to_script == "bar")
            local logged = false
            for _, line in ipairs(logs) do
                if string.find(line, "cross%-domain") then logged = true end
            end
            assert(logged, "cross-domain ref must log a structured warning")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out

    def test_same_domain_ref_resolves_live_instance(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({_marker = true}, Foo) end
            function Foo:Awake()
                assert(self.peer ~= nil, "same-domain ref must resolve")
                assert(self.peer._marker, "ref must point at peer module instance")
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
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


# ---------------------------------------------------------------------------
# instantiatePrefab lifecycle
# ---------------------------------------------------------------------------

class TestInstantiatePrefab:

    def test_instantiate_prefab_runs_lifecycle(self):
        scenario = textwrap.dedent("""\
            local awakeCount = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake() awakeCount = awakeCount + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {},
                prefabs = {
                    ["pfb1"] = {
                        name = "MyPrefab",
                        instances = {{instance_id = "pfb1:1", script_id = "foo",
                                      game_object_id = "pfb1:1", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"pfb1:1"},
                    },
                },
                domain_overrides = {},
            }
            local cloneInstance = {
                Name = "Clone", _sceneRuntimeId = "clone",
                _children = {["pfb1:1"] = {Name = "ClonedChild",
                              _sceneRuntimeId = "pfb1:1", _children = {}}},
            }
            local services = servicesFor(plan, {foo = Foo}, {})
            services.clonePrefabTemplate = function(prefabId, parent, cframe)
                return cloneInstance
            end
            local engine = SceneRuntime.new(services, plan)
            local clone = engine:instantiatePrefab("pfb1", nil, nil, nil)
            runDeferred()
            assert(clone == cloneInstance, "instantiatePrefab returns the clone")
            assert(awakeCount == 1, "prefab component Awake must fire once")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


# ---------------------------------------------------------------------------
# Codex P1 regressions (PR4 review absorption)
# ---------------------------------------------------------------------------

class TestPlannerDormantFlagsPreserved:
    """P1.1: ``_injectHostSurface`` must not overwrite planner ``active``
    / ``enabled`` flags. A component the planner marked dormant must not
    fire ``OnEnable`` / ``Start``. (Pre-fix, _injectHostSurface forced
    meta.enabled = true after _buildComponent's caller copied the
    planner flag, so dormant components booted live.)"""

    def test_dormant_instance_skips_on_enable_and_start(self):
        scenario = textwrap.dedent("""\
            local awakeCount = 0
            local enableCount = 0
            local startCount = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake() awakeCount = awakeCount + 1 end
            function Foo:OnEnable() enableCount = enableCount + 1 end
            function Foo:Start() startCount = startCount + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = false, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g = {Name = "G", _sceneRuntimeId = "g", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            -- Awake fires regardless of enabled (Unity semantics), but
            -- OnEnable + Start must be suppressed for dormant components.
            print("A=" .. awakeCount, "E=" .. enableCount, "S=" .. startCount)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "A=1" in out, out
        assert "E=0" in out, out
        assert "S=0" in out, out


class TestSelfEnabledProxy:
    """P1.1: writing ``self.enabled = false`` from inside a component
    must suspend host.connect subscriptions; ``self.enabled = true``
    re-arms them. Pre-fix there was no proxy at all -- the assignment
    only updated the instance table and never gated dispatch."""

    def test_self_enabled_writes_route_through_set_enabled(self):
        scenario = textwrap.dedent("""\
            local sig = mockSignal()
            local hits = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                self.host.connect(self, sig, function() hits = hits + 1 end)
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g = {Name = "G", _sceneRuntimeId = "g", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            local comp = engine:findObjectOfType("Foo")

            sig:fire()
            assert(hits == 1, "initial dispatch should hit")
            -- User writes self.enabled = false -- must suspend.
            comp.enabled = false
            sig:fire()
            assert(hits == 1, "self.enabled = false must suspend dispatch")
            -- Read-back must match.
            assert(comp.enabled == false, "self.enabled read must reflect setter")
            comp.enabled = true
            sig:fire()
            assert(hits == 2, "self.enabled = true must re-arm dispatch")
            assert(comp.enabled == true, "self.enabled read must reflect re-enable")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


class TestArrayIndexZeroIsArrayNotScalar:
    """P1.2: planner emits 0-based array indexes (Unity convention).
    ``index = 0`` must mean ``self.field[1] = target``, not
    ``self.field = target``. Only ``index = nil`` means scalar."""

    def test_array_index_zero_targets_first_element_not_scalar_field(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            local Bar = {} ; Bar.__index = Bar
            function Bar.new(_) return setmetatable({_markerA = true}, Bar) end
            local Baz = {} ; Baz.__index = Baz
            function Baz.new(_) return setmetatable({_markerB = true}, Baz) end
            local plan = {
                modules = {
                    foo = {stem = "Foo", runtime_bearing = true, module_path = "x"},
                    bar = {stem = "Bar", runtime_bearing = true, module_path = "y"},
                    baz = {stem = "Baz", runtime_bearing = true, module_path = "z"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "foo",
                             game_object_id = "g1", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "bar",
                             game_object_id = "g2", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:3", script_id = "baz",
                             game_object_id = "g3", active = true,
                             enabled = true, config = {}},
                        },
                        references = {
                            {["from"] = "A:1", field = "peers", index = 0,
                             target_kind = "component", target_ref = "A:2",
                             target_is_ui = false},
                            {["from"] = "A:1", field = "peers", index = 1,
                             target_kind = "component", target_ref = "A:3",
                             target_is_ui = false},
                        },
                        lifecycle_order = {"A:1", "A:2", "A:3"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo, bar = Bar, baz = Baz}, {
                g1 = {Name = "G1", _sceneRuntimeId = "g1", _children = {}},
                g2 = {Name = "G2", _sceneRuntimeId = "g2", _children = {}},
                g3 = {Name = "G3", _sceneRuntimeId = "g3", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            local foo = engine:findObjectOfType("Foo")
            assert(type(foo.peers) == "table", "field must be a table, not scalar; got " .. type(foo.peers))
            assert(foo.peers[1] ~= nil, "0-based index 0 must populate Lua slot 1")
            assert(foo.peers[1]._markerA == true, "slot 1 must be Bar")
            assert(foo.peers[2]._markerB == true, "slot 2 must be Baz")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


class TestHostConnectColonForm:
    """P1.3: ``self.host:connect(signal, fn)`` (colon-form) is the
    contract+verifier+prompt-taught calling convention. Pre-fix, the
    runtime only handled the dotted form
    ``self.host.connect(self, signal, fn)``; the colon form silently
    no-op'd because ``comp`` resolved to the host table itself,
    ``_isGateOpen`` returned false, and the callback never armed."""

    def test_host_connect_colon_form_dispatches(self):
        scenario = textwrap.dedent("""\
            local sig = mockSignal()
            local hits = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                -- Colon form (rule (f) reprompt teaches this for all
                -- Unity trigger/collision/mouse callbacks):
                self.host:connect(sig, function() hits = hits + 1 end)
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {
                g = {Name = "G", _sceneRuntimeId = "g", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            sig:fire()
            assert(hits == 1, "host:connect colon form must arm dispatch; hits=" .. hits)
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


class TestCrossDomainLogPlanResolved:
    """P1.4: cross-domain ref policy must fire on real boots, where the
    process only constructs ONE partition's components. The host needs
    to resolve target script/domain from the plan, not the live
    components map; otherwise the warning + edge record never fire."""

    def test_cross_domain_log_fires_when_target_not_locally_constructed(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                assert(self.peer == nil,
                    "cross-domain ref must inject nil; got " .. tostring(self.peer))
            end
            local Bar = {} ; Bar.__index = Bar
            function Bar.new(_) return setmetatable({}, Bar) end
            local plan = {
                modules = {
                    foo = {stem = "Foo", runtime_bearing = true,
                           module_path = "x", domain = "client"},
                    bar = {stem = "Bar", runtime_bearing = true,
                           module_path = "y", domain = "server"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "foo",
                             game_object_id = "g1", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "bar",
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
            local services = servicesFor(plan, {foo = Foo, bar = Bar}, {
                g1 = {Name = "G1", _sceneRuntimeId = "g1", _children = {}},
                g2 = {Name = "G2", _sceneRuntimeId = "g2", _children = {}},
            })
            local engine = SceneRuntime.new(services, plan)
            -- Real production call: only ONE partition runs in this
            -- process. The opposite-side component is never constructed.
            local edges = engine:start("client")
            assert(#edges == 1,
                "cross-domain edge must surface even when target not " ..
                "locally constructed; got " .. #edges)
            assert(edges[1].from_script == "foo")
            assert(edges[1].to_script == "bar")
            assert(edges[1].from_domain == "client")
            assert(edges[1].to_domain == "server")
            local logged = false
            for _, line in ipairs(logs) do
                if string.find(line, "cross%-domain") then logged = true end
            end
            assert(logged, "cross-domain ref must log a structured warning")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


class TestFindGameObjectReturnsGameObjectInstance:
    """P1.5: ``GameObject.Find(name)`` and
    ``GameObject.FindGameObjectsWithTag(tag)`` must return Roblox
    GameObject instances, NOT component module tables. Pre-fix,
    ``_register`` seeded ``_byName`` / ``_byTag`` with component
    instances; ``findGameObject()`` returned a component table and
    ``_byTag`` was never populated at all (no tag plumbing)."""

    def test_find_game_object_returns_roblox_instance_not_component(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({_isComponent = true}, Foo) end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g_player", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local playerGo = {Name = "Player", _sceneRuntimeId = "g_player",
                              _children = {}, _isRobloxInstance = true}
            local services = servicesFor(plan, {foo = Foo}, {g_player = playerGo})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            local found = engine:findGameObject("Player")
            assert(found == playerGo,
                "findGameObject must return the GameObject instance, not the component")
            assert(found._isRobloxInstance == true,
                "must be Roblox instance, got: " .. tostring(found))
            assert(found._isComponent == nil,
                "must NOT be the component table")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out

    def test_find_game_objects_with_tag_returns_roblox_instances(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({_isComponent = true}, Foo) end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "foo",
                             game_object_id = "g1", active = true,
                             enabled = true, tag = "Enemy", config = {}},
                            {instance_id = "A:2", script_id = "foo",
                             game_object_id = "g2", active = true,
                             enabled = true, tag = "Enemy", config = {}},
                            {instance_id = "A:3", script_id = "foo",
                             game_object_id = "g3", active = true,
                             enabled = true, tag = "Friend", config = {}},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2", "A:3"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local g1 = {Name = "E1", _sceneRuntimeId = "g1", _children = {}, _isRobloxInstance = true}
            local g2 = {Name = "E2", _sceneRuntimeId = "g2", _children = {}, _isRobloxInstance = true}
            local g3 = {Name = "F1", _sceneRuntimeId = "g3", _children = {}, _isRobloxInstance = true}
            local services = servicesFor(plan, {foo = Foo}, {g1 = g1, g2 = g2, g3 = g3})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            local enemies = engine:findGameObjectsWithTag("Enemy")
            assert(#enemies == 2, "expected 2 enemy GameObjects, got " .. #enemies)
            for _, found in ipairs(enemies) do
                assert(found._isRobloxInstance == true,
                    "findGameObjectsWithTag must return GameObject instances; got " ..
                    tostring(found))
                assert(found._isComponent == nil,
                    "must NOT be a component table")
            end
            local friends = engine:findGameObjectsWithTag("Friend")
            assert(#friends == 1, "expected 1 friend, got " .. #friends)
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


class TestPrefabComponentReceivesGameObject:
    """P1.6: prefab-spawned component must have non-nil
    ``self.gameObject`` after Awake. Pre-fix, ``instantiatePrefab``
    reinjected with ``m.gameObjectInstance``, but ``_register`` never
    stored ``gameObjectInstance`` -- prefab components booted with
    ``self.gameObject = nil`` and built-in GetComponent fallback lost
    its search root."""

    def test_prefab_component_self_gameobject_non_nil_after_awake(self):
        scenario = textwrap.dedent("""\
            local capturedGo = nil
            local capturedTransform = nil
            local capturedInstance = nil
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                capturedGo = self.gameObject
                capturedTransform = self.transform
                capturedInstance = self.instance
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {},
                prefabs = {
                    ["pfb1"] = {
                        name = "MyPrefab",
                        instances = {{instance_id = "pfb1:1", script_id = "foo",
                                      game_object_id = "pfb1:1", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"pfb1:1"},
                    },
                },
                domain_overrides = {},
            }
            local childGo = {Name = "ClonedChild", _sceneRuntimeId = "pfb1:1", _children = {}}
            local cloneInstance = {
                Name = "Clone", _sceneRuntimeId = "clone",
                _children = {["pfb1:1"] = childGo},
            }
            local services = servicesFor(plan, {foo = Foo}, {})
            services.clonePrefabTemplate = function(prefabId, parent, cframe)
                return cloneInstance
            end
            local engine = SceneRuntime.new(services, plan)
            engine:instantiatePrefab("pfb1", nil, nil, nil)
            runDeferred()
            assert(capturedGo == childGo,
                "prefab component self.gameObject must point at the cloned child")
            assert(capturedTransform == childGo,
                "self.transform must alias the gameObject")
            assert(capturedInstance == childGo,
                "self.instance must alias the gameObject")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "OK" in out


# ---------------------------------------------------------------------------
# P1.7 -- autogen entrypoint must use PlayerGui + DFS post-order
# (asserted by inspecting the generated source string, not via the Luau
# harness, which previously shadowed both with correct helpers).
# ---------------------------------------------------------------------------

class TestAutogenClientEntrypointUsesPlayerGui:
    """P1.7: ``SceneRuntimeClient`` must resolve UI ``_SceneRuntimeId``s
    out of the local player's live ``PlayerGui`` -- ``StarterGui`` is
    the unconverted template that gets cloned per-player and is not
    interactive at runtime."""

    def test_client_entrypoint_uses_player_gui(self):
        from converter.autogen import generate_scene_runtime_client_entrypoint
        src = generate_scene_runtime_client_entrypoint().source
        # Must reference PlayerGui via the local player.
        assert "PlayerGui" in src, "client entrypoint must look up PlayerGui"
        assert "LocalPlayer" in src, (
            "client entrypoint must resolve PlayerGui via LocalPlayer "
            "(StarterGui is the template, not the interactive tree)"
        )
        # PlayerGui lookup must happen during workspaceFind/UI resolution.
        assert "WaitForChild(\"PlayerGui\"" in src, (
            "must WaitForChild for PlayerGui to handle early-lifecycle race"
        )


class TestAutogenCollectDescendantIdsIsDfsPostOrder:
    """P1.7: ``collectDescendantIds`` (used by
    ``host.destroy(parent)``) must walk DFS post-order (children
    deepest-first, then self) per the design doc's recursive-teardown
    contract. Reversing ``GetDescendants()`` (BFS) is NOT the same."""

    @staticmethod
    def _code_only(block: str) -> str:
        # Strip Luau ``--`` line comments so assertions test on
        # executed code, not commentary.
        lines = []
        for line in block.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("--"):
                continue
            # Trim trailing inline comment.
            if " --" in line:
                line = line.split(" --", 1)[0]
            lines.append(line)
        return "\n".join(lines)

    def _collect_block(self, src: str) -> str:
        assert "collectDescendantIds" in src
        # Take from the first "collectDescendantIds = function(" to the
        # closing ",\n" of the function table entry.
        after = src.split("collectDescendantIds")[1]
        block = after.split("end,", 1)[0]
        return self._code_only(block)

    def test_client_entrypoint_collect_descendant_ids_dfs(self):
        from converter.autogen import generate_scene_runtime_client_entrypoint
        src = generate_scene_runtime_client_entrypoint().source
        block = self._collect_block(src)
        # The fixed implementation defines a recursive walk that
        # visits children first, then appends self. The pre-fix
        # implementation called GetDescendants() and used table.insert
        # at index 1 to reverse it.
        assert "GetChildren" in block, (
            "DFS post-order requires walking GetChildren recursively"
        )
        assert "GetDescendants" not in block, (
            "collectDescendantIds must not use GetDescendants (BFS)"
        )
        # table.insert at index 1 was the reverse-BFS workaround; the
        # post-order walker appends to the tail.
        assert "table.insert(out, 1," not in block, (
            "reversing GetDescendants is not equivalent to DFS post-order"
        )

    def test_server_entrypoint_collect_descendant_ids_dfs(self):
        from converter.autogen import generate_scene_runtime_server_entrypoint
        src = generate_scene_runtime_server_entrypoint().source
        block = self._collect_block(src)
        assert "GetChildren" in block, (
            "server entrypoint DFS post-order requires GetChildren walk"
        )
        assert "GetDescendants" not in block, (
            "collectDescendantIds must not use GetDescendants (BFS)"
        )
        assert "table.insert(out, 1," not in block, (
            "reversing GetDescendants is not equivalent to DFS post-order"
        )


# ---------------------------------------------------------------------------
# R2-P1.4: setActive cascades to subtree.
# ---------------------------------------------------------------------------

class TestSetActiveCascadesToSubtree:
    """Codex round-2 P1: ``engine:setActive(parent, false)`` must suspend
    every component in the parent's GameObject subtree, not just the
    components directly attached to the parent GO. Round-2 verified the
    pre-fix bug with ``childHits=2`` (child callback still fired after
    parent disabled). Regression test: a child component's
    ``host.connect`` subscription must NOT fire after the parent toggles
    inactive, and must fire again after the parent toggles active."""

    def test_set_active_false_disables_child_components(self):
        scenario = textwrap.dedent("""\
            local parentSig = mockSignal()
            local childHits = 0
            local Child = {} ; Child.__index = Child
            function Child.new(_) return setmetatable({}, Child) end
            function Child:Awake()
                self.host:connect(parentSig, function()
                    childHits = childHits + 1
                end)
            end
            local plan = {
                modules = {child = {stem = "Child", runtime_bearing = true,
                                    module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "child",
                                      game_object_id = "childGo", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local parent = {Name = "Parent", _sceneRuntimeId = "parentGo",
                             _children = {}}
            local child = {Name = "ChildGo", _sceneRuntimeId = "childGo",
                            _children = {}}
            parent._children.child = child
            local services = servicesFor(plan, {child = Child},
                                          {childGo = child, parentGo = parent})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()

            parentSig:fire()  -- should hit child (parent still active)
            engine:setActive(parent, false)
            parentSig:fire()  -- must NOT hit child (parent disabled, cascade)
            print("afterDisable=" .. childHits)
            engine:setActive(parent, true)
            parentSig:fire()  -- should hit again (cascade re-arms)
            print("afterReEnable=" .. childHits)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "afterDisable=1" in out, (
            f"child component must NOT fire after parent setActive(false); "
            f"got: {out}"
        )
        assert "afterReEnable=2" in out, (
            f"child component must fire again after parent setActive(true); "
            f"got: {out}"
        )


# ---------------------------------------------------------------------------
# R2-P1.5: late re-enable delivers the first Start.
# ---------------------------------------------------------------------------

class TestLateReEnableFiresStartOnFirstTransition:
    """Codex round-2 P1: a component booted with ``enabled=false`` gets
    ``Awake`` but never ``Start``, even after a later ``setEnabled(true)``.
    Unity semantics: Start fires once on the FIRST transition to
    active+enabled -- the transition can happen at boot OR later.
    Subsequent toggles must NOT re-fire Start."""

    def test_dormant_then_setenabled_fires_start_exactly_once(self):
        scenario = textwrap.dedent("""\
            local starts = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Start() starts = starts + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = false, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            print("atBoot=" .. starts)
            local comp = engine:findObjectOfType('Foo')
            engine:setEnabled(comp, true)
            runDeferred()
            print("afterEnable=" .. starts)
            -- Second toggle: false -> true again. Start must NOT re-fire.
            engine:setEnabled(comp, false)
            engine:setEnabled(comp, true)
            runDeferred()
            print("afterToggle=" .. starts)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "atBoot=0" in out, (
            f"dormant component must NOT fire Start at boot; got: {out}"
        )
        assert "afterEnable=1" in out, (
            f"setEnabled(comp, true) must fire Start once; got: {out}"
        )
        assert "afterToggle=1" in out, (
            f"second toggle must NOT re-fire Start; got: {out}"
        )

    def test_dormant_then_setactive_fires_start_exactly_once(self):
        scenario = textwrap.dedent("""\
            local starts = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Start() starts = starts + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = false,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            print("atBoot=" .. starts)
            engine:setActive(go, true)
            runDeferred()
            print("afterSetActive=" .. starts)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "atBoot=0" in out, (
            f"inactive component must NOT fire Start at boot; got: {out}"
        )
        assert "afterSetActive=1" in out, (
            f"setActive(go, true) must fire Start once; got: {out}"
        )


# ---------------------------------------------------------------------------
# R2-P1.3: ScriptableObject ref resolves via plan.scriptable_objects map.
# ---------------------------------------------------------------------------

class TestScriptableObjectRefResolvesViaPlanMap:
    """Codex round-2 P1.3 (contract resolution): the planner persists raw
    Unity GUIDs for ``target_kind == "scriptable_object"`` refs. The host
    runtime now reads ``plan.scriptable_objects[guid]`` to get the dotted
    DataModel module path, then feeds THAT path to ``resolveModule``.
    Pre-fix the runtime fed the raw GUID straight through, which always
    resolved nil in production."""

    def test_so_ref_resolves_module_via_plan_map(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                assert(self.cfg ~= nil, "SO field must wire before Awake")
                assert(self.cfg.value == 42, "SO module table must arrive")
            end
            local SettingsModule = {value = 42}
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {{
                            ["from"] = "A:1", field = "cfg", index = nil,
                            target_kind = "scriptable_object",
                            target_ref = "guid-aaaa",
                            target_is_ui = false,
                        }},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
                scriptable_objects = {
                    ["guid-aaaa"] = "ReplicatedStorage.Settings",
                },
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            -- resolveModule sees scriptId="guid-aaaa" + modulePath=mapped path.
            services.resolveModule = function(scriptId, modulePath)
                if scriptId == "foo" then return Foo end
                if modulePath == "ReplicatedStorage.Settings" then
                    return SettingsModule
                end
                return nil
            end
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}"
        assert "DONE" in out, (
            f"SO ref must resolve via scriptable_objects map; got: {out}, "
            f"err: {err}"
        )


# ---------------------------------------------------------------------------
# R3-P1.1: instantiatePrefab honors planner dormant flags
# ---------------------------------------------------------------------------

class TestInstantiatePrefabDormantFlags:
    """Codex round-3 P1: ``instantiatePrefab`` previously bypassed the
    planner ``active`` / ``enabled`` flag copy that the scene-boot path
    does, so a dormant prefab instance (``enabled=false``) immediately
    ran ``OnEnable`` + ``Start``."""

    def test_prefab_instance_enabled_false_stays_dormant(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            local onEnable = 0
            local starts = 0
            function Foo:OnEnable() onEnable = onEnable + 1 end
            function Foo:Start() starts = starts + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {},
                prefabs = {
                    P = {
                        template_name = "P",
                        instances = {{instance_id = "P:1", script_id = "foo",
                                      game_object_id = "go", active = true,
                                      enabled = false, config = {}}},
                        references = {},
                        lifecycle_order = {"P:1"},
                    },
                },
                domain_overrides = {},
            }
            local services = servicesFor(plan, {foo = Foo}, {})
            services.clonePrefabTemplate = function(prefabId, parent, cframe)
                return {Name = "Clone", _sceneRuntimeId = "clone-root",
                        _children = {go = {Name = "Go",
                                           _sceneRuntimeId = "go",
                                           _children = {}}}}
            end
            services.resolveCloneChild = function(clone, goId)
                return clone._children.go
            end
            local engine = SceneRuntime.new(services, plan)
            engine:instantiatePrefab("P", nil, nil, nil)
            runDeferred()
            print("onEnable=" .. onEnable)
            print("starts=" .. starts)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "onEnable=0" in out, (
            f"dormant prefab instance must NOT fire OnEnable; got: {out}"
        )
        assert "starts=0" in out, (
            f"dormant prefab instance must NOT fire Start; got: {out}"
        )


# ---------------------------------------------------------------------------
# R3-P1.2: dormant destroy skips OnDestroy
# ---------------------------------------------------------------------------

class TestDormantDestroySkipsOnDestroy:
    """Codex round-3 P1: contract says BOTH OnDisable AND OnDestroy are
    skipped when OnEnable never ran. A boot-dormant component destroyed
    before activation must NOT fire OnDestroy."""

    def test_dormant_then_destroy_skips_on_destroy(self):
        scenario = textwrap.dedent("""\
            local awakes = 0
            local destroys = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake() awakes = awakes + 1 end
            function Foo:OnDestroy() destroys = destroys + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = false, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            local comp = engine:findObjectOfType('Foo')
            engine:destroy(comp)
            print("awakes=" .. awakes)
            print("destroys=" .. destroys)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "awakes=1" in out, (
            f"Awake must have fired at boot; got: {out}"
        )
        assert "destroys=0" in out, (
            f"dormant component teardown must NOT fire OnDestroy; got: {out}"
        )

    def test_active_then_destroy_fires_on_destroy(self):
        # Positive control: a live (active+enabled) component destroyed
        # after boot DOES fire OnDestroy.
        scenario = textwrap.dedent("""\
            local destroys = 0
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:OnDestroy() destroys = destroys + 1 end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            local comp = engine:findObjectOfType('Foo')
            engine:destroy(comp)
            print("destroys=" .. destroys)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "destroys=1" in out, (
            f"active component teardown must fire OnDestroy; got: {out}"
        )


# ---------------------------------------------------------------------------
# R3-P1.3: GetComponent built-in fallback includes the GameObject itself.
# ---------------------------------------------------------------------------

class TestGetComponentBuiltinFallbackIncludesSelf:
    """Codex round-3 P1: ``findFirstChildWhichIsA`` (Roblox) searches
    children only, but the contract says ``self:GetComponent("Rigidbody")``
    must return the GameObject itself when it IsA the named class. The
    autogen entrypoints' service helper now checks ``inst:IsA(class)``
    before the recursive child search.
    """

    def test_entrypoint_findfirstchild_helper_checks_self_first(self):
        from converter.autogen import (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_server_entrypoint,
        )
        for gen in (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_server_entrypoint,
        ):
            src = gen().source
            # The autogen-emitted helper must check IsA(class) before
            # falling back to FindFirstChildWhichIsA. Search for the
            # IsA-self pattern.
            assert "inst:IsA(class)" in src, (
                f"{gen.__name__} findFirstChildWhichIsA must check "
                f"inst:IsA(class) so GetComponent on a Part-rooted GO "
                f"finds the BasePart itself (R3-P1.3); got source: {src}"
            )


# ---------------------------------------------------------------------------
# R3-P2: host:instantiatePrefab colon-form preserves externalRefs.
# ---------------------------------------------------------------------------

class TestInstantiatePrefabColonFormPreservesExternalRefs:
    """Codex round-3 P2: the colon-form wrapper for
    ``host:instantiatePrefab(prefab_id, parent, cframe, externalRefs)``
    previously dropped ``externalRefs`` because the wrapper collapsed
    two arg shapes into four formals. Fix: five-arg shape distinguishes
    colon vs dotted.
    """

    def test_colon_form_externalRefs_arrives(self):
        scenario = textwrap.dedent("""\
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                assert(self.injected == "override-value",
                    "externalRefs must arrive on colon-form path")
            end
            -- A bootstrap component that calls host:instantiatePrefab(...).
            local Boot = {} ; Boot.__index = Boot
            function Boot.new(_) return setmetatable({}, Boot) end
            function Boot:Awake()
                self.host:instantiatePrefab("P", nil, nil, {
                    ["P:1"] = {injected = "override-value"},
                })
            end
            local plan = {
                modules = {
                    boot = {stem = "Boot", runtime_bearing = true,
                            module_path = "x"},
                    foo = {stem = "Foo", runtime_bearing = true,
                            module_path = "y"},
                },
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "boot",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {
                    P = {
                        template_name = "P",
                        instances = {{instance_id = "P:1", script_id = "foo",
                                      game_object_id = "pgo", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"P:1"},
                    },
                },
                domain_overrides = {},
            }
            local services = servicesFor(plan, {boot = Boot, foo = Foo},
                                          {g = {Name = "G",
                                                _sceneRuntimeId = "g",
                                                _children = {}}})
            services.clonePrefabTemplate = function(prefabId, parent, cframe)
                return {Name = "Clone", _sceneRuntimeId = "clone-root",
                        _children = {pgo = {Name = "Pgo",
                                            _sceneRuntimeId = "pgo",
                                            _children = {}}}}
            end
            services.resolveCloneChild = function(clone, goId)
                return clone._children.pgo
            end
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "DONE" in out, (
            f"externalRefs must arrive on colon-form instantiatePrefab; "
            f"got: {out}, err: {err}"
        )


# ---------------------------------------------------------------------------
# R4-P1.3: GetComponent translates Unity class names to Roblox class names.
# ---------------------------------------------------------------------------

class TestGetComponentTranslatesUnityToRobloxClassName:
    """Codex round-4 P1.3: contract-emitted MonoBehaviours call
    ``self:GetComponent("Rigidbody")`` with the Unity type name, but
    Roblox's ``IsA``/``findFirstChildWhichIsA`` only knows Roblox class
    names. The host runtime now translates known Unity names
    (``Rigidbody`` -> ``BasePart``, ``MeshRenderer`` -> ``MeshPart``,
    etc.) before the lookup. Unknown names fall through unchanged so
    Roblox class names passed directly still work, and operators can
    extend the table without breaking the contract."""

    def test_rigidbody_translates_to_basepart(self):
        scenario = textwrap.dedent("""\
            local fakePart = {Name = "ThePart"}
            local seen = nil
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                seen = self:GetComponent("Rigidbody")
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            -- Mock stores the lookup keyed by Roblox class name; if the
            -- host failed to translate, the lookup would miss.
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {},
                        _builtins = {BasePart = fakePart}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            assert(seen == fakePart,
                "Rigidbody must translate to BasePart for the fallback")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "OK" in out, out

    def test_meshrenderer_translates_to_meshpart(self):
        scenario = textwrap.dedent("""\
            local fakeMesh = {Name = "Mesh"}
            local seen = nil
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                seen = self:GetComponent("MeshRenderer")
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {},
                        _builtins = {MeshPart = fakeMesh}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            assert(seen == fakeMesh,
                "MeshRenderer must translate to MeshPart")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "OK" in out, out

    def test_transform_returns_self_gameobject(self):
        scenario = textwrap.dedent("""\
            local seen = nil
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                seen = self:GetComponent("Transform")
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            -- Transform is intrinsic to a Unity GameObject; the Roblox
            -- analog is the GameObject's own root instance.
            assert(seen == go,
                "GetComponent('Transform') must return the GameObject itself")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "OK" in out, out

    def test_unknown_unity_name_falls_through(self):
        """Unmapped names pass through to the raw findFirstChildWhichIsA
        lookup so:
          (a) operators who pass Roblox class names directly still hit;
          (b) unrecognized Unity names preserve the pre-fix nil result
              rather than silently swallowing the lookup."""
        scenario = textwrap.dedent("""\
            local fakeAttachment = {Name = "Att"}
            local seenKnown = nil
            local seenUnknown = "sentinel"
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                -- Roblox class name passed directly -- raw passthrough.
                seenKnown = self:GetComponent("Attachment")
                -- Unmapped Unity-style name; raw lookup misses.
                seenUnknown = self:GetComponent("SomeMadeUpUnityType")
            end
            local plan = {
                modules = {foo = {stem = "Foo", runtime_bearing = true,
                                  module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "foo",
                                      game_object_id = "g", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {},
                        _builtins = {Attachment = fakeAttachment}}
            local services = servicesFor(plan, {foo = Foo}, {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            assert(seenKnown == fakeAttachment,
                "Roblox class name should pass through untranslated")
            assert(seenUnknown == nil,
                "Unknown Unity name should fall through to nil lookup")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "OK" in out, out

    def test_peer_module_lookup_still_wins_over_translation(self):
        """A peer MonoBehaviour with stem matching the Unity-style name
        should still beat the built-in fallback. The translation table
        only fires AFTER the peer-component search misses."""
        scenario = textwrap.dedent("""\
            local seen = nil
            local Rigidbody = {} ; Rigidbody.__index = Rigidbody
            function Rigidbody.new(_) return setmetatable({mark = "peer"}, Rigidbody) end
            local Foo = {} ; Foo.__index = Foo
            function Foo.new(_) return setmetatable({}, Foo) end
            function Foo:Awake()
                seen = self:GetComponent("Rigidbody")
            end
            local plan = {
                modules = {
                    rb = {stem = "Rigidbody", runtime_bearing = true,
                          module_path = "x"},
                    foo = {stem = "Foo", runtime_bearing = true,
                           module_path = "y"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "rb",
                             game_object_id = "g", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "foo",
                             game_object_id = "g", active = true,
                             enabled = true, config = {}},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local go = {Name = "G", _sceneRuntimeId = "g", _children = {},
                        _builtins = {BasePart = {Name = "wrong"}}}
            local services = servicesFor(plan, {rb = Rigidbody, foo = Foo},
                                          {g = go})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            assert(seen ~= nil and seen.mark == "peer",
                "peer MonoBehaviour named 'Rigidbody' must win over the "
                .. "built-in translation table")
            print("OK")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "OK" in out, out


# ---------------------------------------------------------------------------
# R4-P1.2: setActive preserves descendant activeSelf (activeInHierarchy split).
# ---------------------------------------------------------------------------

class TestSetActivePreservesDescendantActiveSelf:
    """Codex round-4 P1.2: Unity's GameObject has TWO active flags --
    ``activeSelf`` (the GO's OWN authored flag) and
    ``activeInHierarchy`` (``activeSelf`` AND every ancestor's
    ``activeSelf``). Pre-fix the host conflated them: the cascade
    overwrote each descendant's stored ``activeSelf`` to the toggled
    value, so a child authored ``activeSelf == false`` wrongly
    re-activated when its parent toggled back on. Post-fix:
    ``setActive(parent, true)`` only changes the parent's
    ``activeSelf``; descendant gates are recomputed but their authored
    ``activeSelf`` is preserved, so a dormant child stays dormant
    until ``setActive(child, true)`` is called directly.
    """

    def test_authored_inactive_child_stays_inactive_after_parent_retoggle(self):
        scenario = textwrap.dedent("""\
            local childEnableHits = 0
            local Child = {} ; Child.__index = Child
            function Child.new(_) return setmetatable({}, Child) end
            function Child:OnEnable() childEnableHits = childEnableHits + 1 end
            local plan = {
                modules = {child = {stem = "Child", runtime_bearing = true,
                                    module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{
                            instance_id = "A:1", script_id = "child",
                            game_object_id = "childGo",
                            -- AUTHORED INACTIVE: must stay inactive across
                            -- parent toggles.
                            active = false, enabled = true, config = {},
                        }},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local parent = {Name = "Parent", _sceneRuntimeId = "parentGo",
                             _children = {}}
            local child = {Name = "ChildGo", _sceneRuntimeId = "childGo",
                            _children = {}}
            parent._children.child = child
            local services = servicesFor(plan, {child = Child},
                                          {childGo = child, parentGo = parent})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            print("atBoot=" .. childEnableHits)
            engine:setActive(parent, false)
            print("afterParentFalse=" .. childEnableHits)
            engine:setActive(parent, true)
            print("afterParentTrue=" .. childEnableHits)
            -- Now directly enable the child; both gates should open.
            engine:setActive(child, true)
            print("afterChildTrue=" .. childEnableHits)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "atBoot=0" in out, (
            f"authored-inactive child must NOT fire OnEnable at boot; "
            f"got: {out}"
        )
        assert "afterParentFalse=0" in out, (
            f"already-dormant child must not fire on parent disable; "
            f"got: {out}"
        )
        # THE CORE R4-P1.2 ASSERTION: parent re-enable must NOT
        # re-activate a child whose authored activeSelf is false.
        assert "afterParentTrue=0" in out, (
            f"R4-P1.2 regression: parent setActive(true) wrongly "
            f"re-activated a child whose authored activeSelf == false; "
            f"got: {out}"
        )
        assert "afterChildTrue=1" in out, (
            f"direct setActive(child, true) must finally fire OnEnable; "
            f"got: {out}"
        )

    def test_active_child_still_cascades_with_parent(self):
        """Sanity: the cascade still works for an ACTIVE child --
        toggling the parent off suspends the child, toggling parent
        back on resumes it. The R4 fix must not regress the R2 cascade.
        """
        scenario = textwrap.dedent("""\
            local sig = mockSignal()
            local childHits = 0
            local Child = {} ; Child.__index = Child
            function Child.new(_) return setmetatable({}, Child) end
            function Child:Awake()
                self.host:connect(sig, function()
                    childHits = childHits + 1
                end)
            end
            local plan = {
                modules = {child = {stem = "Child", runtime_bearing = true,
                                    module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "child",
                                      game_object_id = "childGo", active = true,
                                      enabled = true, config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local parent = {Name = "Parent", _sceneRuntimeId = "parentGo",
                             _children = {}}
            local child = {Name = "ChildGo", _sceneRuntimeId = "childGo",
                            _children = {}}
            parent._children.child = child
            local services = servicesFor(plan, {child = Child},
                                          {childGo = child, parentGo = parent})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            sig:fire()
            engine:setActive(parent, false)
            sig:fire()  -- must NOT hit (cascade suspends)
            print("afterDisable=" .. childHits)
            engine:setActive(parent, true)
            sig:fire()  -- should hit (both gates open again)
            print("afterReEnable=" .. childHits)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "afterDisable=1" in out, out
        assert "afterReEnable=2" in out, out

    def test_direct_setactive_on_authored_inactive_child_works(self):
        """``setActive(child, true)`` on a child authored inactive must
        open the gate when the parent is active. This is the documented
        Unity behaviour the brief describes.
        """
        scenario = textwrap.dedent("""\
            local enables = 0
            local Child = {} ; Child.__index = Child
            function Child.new(_) return setmetatable({}, Child) end
            function Child:OnEnable() enables = enables + 1 end
            local plan = {
                modules = {child = {stem = "Child", runtime_bearing = true,
                                    module_path = "x"}},
                scenes = {
                    A = {
                        instances = {{instance_id = "A:1", script_id = "child",
                                      game_object_id = "childGo",
                                      active = false, enabled = true,
                                      config = {}}},
                        references = {},
                        lifecycle_order = {"A:1"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local parent = {Name = "Parent", _sceneRuntimeId = "parentGo",
                             _children = {}}
            local child = {Name = "ChildGo", _sceneRuntimeId = "childGo",
                            _children = {}}
            parent._children.child = child
            local services = servicesFor(plan, {child = Child},
                                          {childGo = child, parentGo = parent})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            engine:setActive(child, true)
            print("afterDirect=" .. enables)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "afterDirect=1" in out, out


class TestSetActiveSubtreeIdsWithParentsHelperEmitted:
    """The host runtime's R4-P1.2 cascade prefers the optional
    ``collectSubtreeIdsWithParents`` service when present (full
    preorder + parent-id list). Both autogen entrypoints must emit
    the helper so the generated entrypoints' cascade is correct in
    production, not just in the test harness.
    """

    def test_both_entrypoints_emit_collect_subtree_ids_with_parents(self):
        from converter.autogen import (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_server_entrypoint,
        )
        for gen in (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_server_entrypoint,
        ):
            src = gen().source
            assert "collectSubtreeIdsWithParents" in src, (
                f"{gen.__name__} must emit collectSubtreeIdsWithParents "
                f"so the R4-P1.2 setActive cascade has parent info "
                f"available in production; got: {src[:400]}"
            )
            # Sanity: the emitted helper must record parentId on each
            # entry (the cascade reads ``entry.parentId``).
            assert "parentId" in src, (
                f"{gen.__name__} subtree helper must emit parentId "
                f"per entry; got: {src[:400]}"
            )


# ---------------------------------------------------------------------------
# R5-P1.2: deep ancestor semantics. The planner emits
# ``parent_game_object_id`` on every instance; the host walks the parent
# map UP when computing ``activeInHierarchy``. Without this fix:
#   * boot-time: inactive parent + active child still fires child's
#     OnEnable at boot;
#   * ``setActive(grandchild, true)`` while a grandparent is inactive
#     wrongly opens the grandchild's gate.
# ---------------------------------------------------------------------------


class TestDeepAncestorActiveInHierarchy:
    """R5-P1.2: planner ``parent_game_object_id`` lets the host walk the
    ancestor chain UP. Previously the runtime could only walk subtree
    DOWN (via ``collectSubtreeIdsWithParents``), so a deeply-nested GO's
    own ancestor gate was approximated as "active" -- making
    ``setActive(grandchild, true)`` wrongly open the gate even when a
    grandparent was inactive, and a boot-time inactive parent failed to
    suppress an active child's ``OnEnable``.
    """

    def test_boot_inactive_parent_suppresses_active_child_on_enable(self):
        # Parent GO is inactive (active=false), child GO is active.
        # Child's MB must NOT fire OnEnable at boot because the
        # ancestor chain is inactive. Pre-fix the runtime ignored the
        # parent edge and fired OnEnable.
        scenario = textwrap.dedent("""\
            local childEnableHits = 0
            local Parent = {} ; Parent.__index = Parent
            function Parent.new(_) return setmetatable({}, Parent) end
            function Parent:OnEnable() error("parent must not enable") end
            local Child = {} ; Child.__index = Child
            function Child.new(_) return setmetatable({}, Child) end
            function Child:OnEnable() childEnableHits = childEnableHits + 1 end
            local plan = {
                modules = {
                    parent = {stem = "Parent", runtime_bearing = true,
                              module_path = "x"},
                    child = {stem = "Child", runtime_bearing = true,
                             module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:p", script_id = "parent",
                             game_object_id = "parentGo", active = false,
                             enabled = true, config = {}},
                            {instance_id = "A:c", script_id = "child",
                             game_object_id = "childGo",
                             parent_game_object_id = "parentGo",
                             active = true, enabled = true, config = {}},
                        },
                        references = {},
                        lifecycle_order = {"A:p", "A:c"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local parent = {Name = "Parent", _sceneRuntimeId = "parentGo",
                             _children = {}}
            local child = {Name = "ChildGo", _sceneRuntimeId = "childGo",
                            _children = {}}
            parent._children.child = child
            local services = servicesFor(plan,
                {parent = Parent, child = Child},
                {parentGo = parent, childGo = child})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            print("atBoot=" .. childEnableHits)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "atBoot=0" in out, (
            f"R5-P1.2 regression: inactive parent must suppress active "
            f"child's OnEnable at boot via the planner parent map; "
            f"got: {out}"
        )

    def test_setactive_grandchild_blocked_by_inactive_grandparent(self):
        # Grandparent inactive at boot; child + grandchild active.
        # ``setActive(grandchild, true)`` is a no-op because the
        # ancestor chain is still inactive. Only when the grandparent
        # re-enables does the gate open. Pre-fix the runtime
        # approximated the toggled GO's ancestor gate as "true" and
        # wrongly fired OnEnable.
        scenario = textwrap.dedent("""\
            local enables = 0
            local Grand = {} ; Grand.__index = Grand
            function Grand.new(_) return setmetatable({}, Grand) end
            local Mid = {} ; Mid.__index = Mid
            function Mid.new(_) return setmetatable({}, Mid) end
            local Child = {} ; Child.__index = Child
            function Child.new(_) return setmetatable({}, Child) end
            function Child:OnEnable() enables = enables + 1 end
            local plan = {
                modules = {
                    grand = {stem = "Grand", runtime_bearing = true,
                             module_path = "x"},
                    mid = {stem = "Mid", runtime_bearing = true,
                           module_path = "x"},
                    child = {stem = "Child", runtime_bearing = true,
                             module_path = "x"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:g", script_id = "grand",
                             game_object_id = "grandGo", active = false,
                             enabled = true, config = {}},
                            {instance_id = "A:m", script_id = "mid",
                             game_object_id = "midGo",
                             parent_game_object_id = "grandGo",
                             active = true, enabled = true, config = {}},
                            {instance_id = "A:c", script_id = "child",
                             game_object_id = "childGo",
                             parent_game_object_id = "midGo",
                             active = true, enabled = true, config = {}},
                        },
                        references = {},
                        lifecycle_order = {"A:g", "A:m", "A:c"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local grand = {Name = "Grand", _sceneRuntimeId = "grandGo",
                            _children = {}}
            local mid = {Name = "Mid", _sceneRuntimeId = "midGo",
                          _children = {}}
            local child = {Name = "ChildGo", _sceneRuntimeId = "childGo",
                            _children = {}}
            grand._children.mid = mid
            mid._children.child = child
            local services = servicesFor(plan,
                {grand = Grand, mid = Mid, child = Child},
                {grandGo = grand, midGo = mid, childGo = child})
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            print("atBoot=" .. enables)
            -- ``setActive(child, true)`` is a no-op: child is already
            -- authored active; grandparent is still inactive so the
            -- ancestor walk keeps the gate shut.
            engine:setActive(child, true)
            print("afterChildToggle=" .. enables)
            -- Re-enabling the grandparent must cascade through the
            -- intermediate active mid down to the child.
            engine:setActive(grand, true)
            print("afterGrandToggle=" .. enables)
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, err
        assert "atBoot=0" in out, (
            f"R5-P1.2: boot must keep grandchild dormant when grandparent "
            f"is inactive; got: {out}"
        )
        assert "afterChildToggle=0" in out, (
            f"R5-P1.2 regression: setActive(child) wrongly opened the "
            f"gate despite an inactive grandparent; got: {out}"
        )
        assert "afterGrandToggle=1" in out, (
            f"R5-P1.2: grandparent re-enable must cascade through the "
            f"intermediate active GO and fire the grandchild's OnEnable; "
            f"got: {out}"
        )

    def test_planner_emits_parent_game_object_id_on_child_instances(self):
        """The runtime fix is only useful if the planner actually emits
        ``parent_game_object_id`` on instances whose Unity GO had a
        parent. Sanity: child rows carry the parent edge; root rows do
        not.
        """
        from pathlib import Path

        from converter.scene_runtime_planner import _walk_scene
        from core.unity_types import ComponentData, ParsedScene, SceneNode

        # Two-level scene: root + one child, both with a MonoBehaviour
        # carrying a resolvable script GUID.
        mono_props = {
            "m_Script": {"fileID": 11500000, "guid": "a" * 32, "type": 3},
            "m_Enabled": 1,
        }
        root_node = SceneNode(
            name="Root",
            file_id="1",
            active=True,
            layer=0,
            tag="",
            components=[ComponentData(
                file_id="11", component_type="MonoBehaviour",
                properties=mono_props,
            )],
            children=[],
            parent_file_id=None,
        )
        child_node = SceneNode(
            name="Child",
            file_id="2",
            active=True,
            layer=0,
            tag="",
            components=[ComponentData(
                file_id="22", component_type="MonoBehaviour",
                properties=mono_props,
            )],
            children=[],
            parent_file_id="1",
        )
        root_node.children.append(child_node)
        scene = ParsedScene(
            scene_path=Path("/tmp/X.unity"),
            roots=[root_node],
            all_nodes={"1": root_node, "2": child_node},
        )

        # Minimal stub for guid_index that returns a .cs path.
        class _Stub:
            def resolve(self, guid):
                from pathlib import Path as _P
                return _P("/tmp/x.cs") if guid == "a" * 32 else None
            def guid_for_path(self, path):
                return None

        result = _walk_scene(
            scene, "ns", _Stub(), {}, None, set(),
        )
        rows = {row["instance_id"]: row for row in result["instances"]}
        assert "ns:11" in rows and "ns:22" in rows
        # Root: no parent edge (it's a scene root).
        assert "parent_game_object_id" not in rows["ns:11"], (
            f"scene root must not carry parent_game_object_id; got: "
            f"{rows['ns:11']}"
        )
        # Child: parent edge resolves to the namespaced root id.
        assert rows["ns:22"].get("parent_game_object_id") == "ns:1", (
            f"child must carry parent_game_object_id = ns:1; got: "
            f"{rows['ns:22']}"
        )
