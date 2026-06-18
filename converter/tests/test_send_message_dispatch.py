"""Slice 1.1: behavioral tests for the SendMessage / BroadcastMessage
host primitives in ``converter/runtime/scene_runtime.luau``.

These drive the production host runtime through the standalone ``luau``
interpreter with the same mock service surface used by
``test_scene_runtime_host_behavior.py`` (loadstring the embedded source,
mock the Roblox slice the runtime touches, assert on stdout markers).
Skips cleanly when ``luau`` is absent so CI without it doesn't fail.

Covered (per design-phase1.md Slice 1.1 test matrix):
  * multi-component dispatch on ONE GameObject (Unity multi-component);
  * BasePart receiver -> owning GameObject (ancestor-Model walk);
  * inactive GameObject is skipped (activeInHierarchy gate);
  * a missing method does not throw (best-effort, no missing-handler);
  * broadcastMessage descends to a child GameObject;
  * both the colon (``host:sendMessage``) and dotted
    (``host.sendMessage``) call forms.
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
# Shared harness: loads the host runtime + a mock Roblox surface. Mirrors
# test_scene_runtime_host_behavior.py so the scenarios see SceneRuntime,
# servicesFor, runDeferred, logs as top-level locals.
# ---------------------------------------------------------------------------

def _harness_preamble() -> str:
    host_source = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in host_source or f"[{delim}[" in host_source:
        delim += "="
    embedded = f"[{delim}[\n{host_source}\n]{delim}]"
    return textwrap.dedent(f"""\
        local HOST_RUNTIME_SOURCE = {embedded}
        local SceneRuntime
        do
            local chunk, err = loadstring(HOST_RUNTIME_SOURCE, "scene_runtime")
            assert(chunk, "load host runtime failed: " .. tostring(err))
            SceneRuntime = chunk()
        end
""") + _HARNESS_BODY


_HARNESS_BODY = """local _deferred = {}
local _nextHandle = 0
local function newHandle()
    _nextHandle = _nextHandle + 1
    return {handle = _nextHandle}
end
local task = {}
function task.spawn(fn, ...)
    local h = newHandle()
    pcall(fn, ...)
    return h
end
function task.defer(fn, ...)
    local h = newHandle()
    table.insert(_deferred, {handle = h, fn = fn, args = {...}})
    return h
end
function task.delay(secs, fn, ...) return newHandle() end
function task.wait(secs) return secs or 0 end
function task.cancel(h) end

local function runDeferred()
    local snap = _deferred
    _deferred = {}
    for _, entry in ipairs(snap) do
        pcall(entry.fn, table.unpack(entry.args))
    end
end

local function mockSignal()
    local sig = { _conns = {}, _connId = 0 }
    function sig:Connect(fn)
        sig._connId = sig._connId + 1
        local id = sig._connId
        sig._conns[id] = fn
        local conn = {}
        function conn:Disconnect() sig._conns[id] = nil end
        return conn
    end
    function sig:fire(...)
        for _, fn in pairs(sig._conns) do fn(...) end
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
    for _id, inst in pairs(instances) do
        if type(inst) == "table" then
            if inst.SetAttribute == nil then
                inst.SetAttribute = function(self, name, value)
                    if name == "_SceneRuntimeId" then
                        self._sceneRuntimeId = value
                    else
                        self["_attr_" .. tostring(name)] = value
                    end
                end
            end
            if inst.GetAttribute == nil then
                inst.GetAttribute = function(self, name)
                    if name == "_SceneRuntimeId" then
                        return self._sceneRuntimeId
                    end
                    return self["_attr_" .. tostring(name)]
                end
            end
        end
    end
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
        now = function() return 0 end,
        getInstanceId = function(inst)
            return inst and inst._sceneRuntimeId
        end,
        clonePrefabTemplate = function(prefabId, parent, cframe) return nil end,
        resolveCloneChild = function(clone, gameObjectId)
            if clone and clone._sceneRuntimeId == gameObjectId then
                return clone
            end
            if clone and clone._children then
                return clone._children[gameObjectId]
            end
            return nil
        end,
        collectDescendantIds = function(inst)
            local out = {}
            local function walk(node)
                if node._children then
                    for _, child in pairs(node._children) do walk(child) end
                end
                table.insert(out, node._sceneRuntimeId)
            end
            walk(inst)
            return out
        end,
        collectSubtreeIdsWithParents = function(inst)
            local out = {}
            local function walk(node, parentId)
                local id = node._sceneRuntimeId
                table.insert(out, {id = id, parentId = parentId})
                if node._children then
                    for _, child in pairs(node._children) do walk(child, id) end
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
# A reusable two-component / child-GameObject fixture builder. The
# receiver-targeting GameObject "g" carries two MonoBehaviours (Foo, Bar)
# that both implement GetItem; the child GameObject "gChild" carries one
# (Baz) used only by the broadcast test.
# ---------------------------------------------------------------------------

_SCENE_SETUP = """\
local calls = {}
local function record(tag, ...)
    local args = {...}
    for i, v in ipairs(args) do args[i] = tostring(v) end
    table.insert(calls, tag .. "(" .. table.concat(args, ",") .. ")")
end

local Foo = {} ; Foo.__index = Foo
function Foo.new(_) return setmetatable({}, Foo) end
function Foo:GetItem(name) record("Foo.GetItem", name) end

local Bar = {} ; Bar.__index = Bar
function Bar.new(_) return setmetatable({}, Bar) end
function Bar:GetItem(name) record("Bar.GetItem", name) end

local Baz = {} ; Baz.__index = Baz
function Baz.new(_) return setmetatable({}, Baz) end
function Baz:GetItem(name) record("Baz.GetItem", name) end

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
                 game_object_id = "g", active = true, enabled = true,
                 config = {}},
                {instance_id = "A:2", script_id = "bar",
                 game_object_id = "g", active = true, enabled = true,
                 config = {}},
                {instance_id = "A:3", script_id = "baz",
                 game_object_id = "gChild", active = true, enabled = true,
                 config = {}, parent_game_object_id = "g"},
            },
            references = {},
            lifecycle_order = {"A:1", "A:2", "A:3"},
        },
    },
    prefabs = {}, domain_overrides = {},
}
local childGo = {Name = "Child", _sceneRuntimeId = "gChild", _children = {}}
local parentGo = {Name = "Parent", _sceneRuntimeId = "g",
                  _children = {gChild = childGo}}
local services = servicesFor(plan, {foo = Foo, bar = Bar, baz = Baz}, {
    g = parentGo, gChild = childGo,
})
local engine = SceneRuntime.new(services, plan)
engine:start(nil)
runDeferred()
-- A live component on "g" (Foo) gives us a host surface + a component
-- receiver handle.
local fooComp, bazComp
for comp, m in pairs(engine._meta) do
    if m.gameObjectId == "g" and m.classTable == Foo then fooComp = comp end
    if m.gameObjectId == "gChild" then bazComp = comp end
end
local host = fooComp.host
"""


class TestSendMessageDispatch:

    def test_multi_component_dispatch_on_one_game_object(self):
        # SendMessage to a component on "g" must hit BOTH Foo and Bar
        # (Unity multi-component semantics), each exactly once.
        scenario = _SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(fooComp, "GetItem", "Rifle")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Foo.GetItem(Rifle)" in lines, out
        assert "Bar.GetItem(Rifle)" in lines, out
        assert lines.count("Foo.GetItem(Rifle)") == 1, out
        assert lines.count("Bar.GetItem(Rifle)") == 1, out

    def test_basepart_receiver_resolves_to_owning_game_object(self):
        # A touched BasePart whose own id carries no components must
        # resolve to its ancestor Model "g" and dispatch there.
        scenario = _SCENE_SETUP + textwrap.dedent("""\
            -- A BasePart parented under the "g" Model; not itself stamped
            -- with a component-bearing id, so resolution must climb.
            local part = {
                Name = "Hitbox",
                _sceneRuntimeId = "loose-part",
            }
            part.FindFirstAncestorWhichIsA = function(self, class)
                if class == "Model" then return parentGo end
                return nil
            end
            host:sendMessage(part, "GetItem", "Sword")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Foo.GetItem(Sword)" in lines, out
        assert "Bar.GetItem(Sword)" in lines, out

    def test_inactive_game_object_is_skipped(self):
        # Unity SendMessage targets only components on an ACTIVE
        # GameObject; toggle "g" inactive and assert nothing fires.
        scenario = _SCENE_SETUP + textwrap.dedent("""\
            engine:setActive(parentGo, false)
            host:sendMessage(fooComp, "GetItem", "Rifle")
            print("CALLS=" .. #calls)
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "CALLS=0" in out, out

    def test_missing_method_does_not_throw(self):
        # No component implements "Nonexistent"; the dispatch must be a
        # silent best-effort no-op (Roblox has no missing-handler error).
        scenario = _SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(fooComp, "Nonexistent", 1)
            print("CALLS=" .. #calls)
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "CALLS=0" in out, out
        assert "DONE" in out, out

    def test_broadcast_descends_to_child_game_object(self):
        # broadcastMessage on "g" hits Foo+Bar AND descends to the child
        # GameObject "gChild" (Baz). Plain sendMessage must NOT descend.
        scenario = _SCENE_SETUP + textwrap.dedent("""\
            host:broadcastMessage(fooComp, "GetItem", "Ammo")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Foo.GetItem(Ammo)" in lines, out
        assert "Bar.GetItem(Ammo)" in lines, out
        assert "Baz.GetItem(Ammo)" in lines, out

    def test_send_message_does_not_descend_to_child(self):
        # The negative control for the broadcast test: sendMessage stays
        # on the receiver's own GameObject.
        scenario = _SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(fooComp, "GetItem", "Ammo")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Foo.GetItem(Ammo)" in lines, out
        assert "Bar.GetItem(Ammo)" in lines, out
        assert "Baz.GetItem(Ammo)" not in lines, out

    def test_both_colon_and_dotted_call_forms(self):
        # The host-surface wrapper supports both ``host:sendMessage(...)``
        # (colon, arg1 == host) and ``host.sendMessage(recv, ...)``
        # (dotted) -- both must forward receiver + name + gameplay args.
        scenario = _SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(fooComp, "GetItem", "Colon")
            host.sendMessage(fooComp, "GetItem", "Dotted")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Foo.GetItem(Colon)" in lines, out
        assert "Foo.GetItem(Dotted)" in lines, out
        assert "Bar.GetItem(Colon)" in lines, out
        assert "Bar.GetItem(Dotted)" in lines, out

    def test_game_object_instance_receiver(self):
        # A Model/GameObject instance receiver resolves via getInstanceId
        # directly (no ancestor walk needed).
        scenario = _SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(parentGo, "GetItem", "Direct")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Foo.GetItem(Direct)" in lines, out
        assert "Bar.GetItem(Direct)" in lines, out
