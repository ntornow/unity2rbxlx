"""Slices 1.1 + 2.1: behavioral tests for the SendMessage / BroadcastMessage
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

Phase 2 (Slice 2.1) player-alias receiver routing (``TestPlayerAliasDispatch``):
  * AC-1 Player-object alias (``IsA("Player")``) -> embodiment GameObject;
  * AC-2 character-limb BasePart alias (via ``playerFromTouch``);
  * AC-3 non-player receiver takes the UNCHANGED Phase-1 path;
  * AC-4 fail-closed when ``_player`` is nil (warn + no dispatch, no throw);
  * AC-5 player module on >1 GameObjects -> dispatch to each (dedupe);
  * D-P2-9 dispatch count == ``#_playerGoIds()`` (== 1 single-embodiment);
  * AC-6 broadcastMessage to a player alias = flat per-goId dispatch.
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

    def test_broadcast_descends_multiple_levels(self):
        # broadcastMessage must reach a GRANDCHILD GameObject, not just a
        # direct child: the descendant walk follows the parent chain UP
        # from every GameObject, so a multi-level chain g -> gChild ->
        # gGrand must all receive. Builds a fresh 3-level scene.
        scenario = textwrap.dedent("""\
            local calls = {}
            local function record(tag, ...)
                local args = {...}
                for i, v in ipairs(args) do args[i] = tostring(v) end
                table.insert(calls, tag .. "(" .. table.concat(args, ",") .. ")")
            end
            local function mk(stem)
                local M = {} ; M.__index = M
                function M.new(_) return setmetatable({}, M) end
                function M:GetItem(name) record(stem .. ".GetItem", name) end
                return M
            end
            local Root, Mid, Leaf = mk("Root"), mk("Mid"), mk("Leaf")
            local plan = {
                modules = {
                    root = {stem = "Root", runtime_bearing = true, module_path = "x"},
                    mid  = {stem = "Mid",  runtime_bearing = true, module_path = "y"},
                    leaf = {stem = "Leaf", runtime_bearing = true, module_path = "z"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "root",
                             game_object_id = "g", active = true, enabled = true,
                             config = {}},
                            {instance_id = "A:2", script_id = "mid",
                             game_object_id = "gChild", active = true, enabled = true,
                             config = {}, parent_game_object_id = "g"},
                            {instance_id = "A:3", script_id = "leaf",
                             game_object_id = "gGrand", active = true, enabled = true,
                             config = {}, parent_game_object_id = "gChild"},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2", "A:3"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local grandGo = {Name = "Grand", _sceneRuntimeId = "gGrand", _children = {}}
            local childGo = {Name = "Child", _sceneRuntimeId = "gChild",
                             _children = {gGrand = grandGo}}
            local parentGo = {Name = "Parent", _sceneRuntimeId = "g",
                              _children = {gChild = childGo}}
            local services = servicesFor(plan, {root = Root, mid = Mid, leaf = Leaf}, {
                g = parentGo, gChild = childGo, gGrand = grandGo,
            })
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            local rootComp
            for comp, m in pairs(engine._meta) do
                if m.gameObjectId == "g" then rootComp = comp end
            end
            local host = rootComp.host
            host:broadcastMessage(rootComp, "GetItem", "Ammo")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Root.GetItem(Ammo)" in lines, out
        assert "Mid.GetItem(Ammo)" in lines, out
        assert "Leaf.GetItem(Ammo)" in lines, out

    def test_broadcast_handler_mutating_set_does_not_skip_targets(self):
        # The snapshot-before-dispatch guarantee: a handler that MUTATES
        # the GameObject/component set during dispatch (here, an Extinguish
        # handler destroying another target's GameObject) must NOT skip a
        # pre-existing surviving sibling, must not throw, and must not
        # deliver to the destroyed target. The fix collects the complete
        # target id list FIRST (no dispatch), THEN iterates that captured
        # array -- so no handler runs while ``_componentsByGameObject`` /
        # the parent map is being traversed by ``pairs``.
        #
        # (The reference-Lua hazard the fix removes -- ``pairs`` over a
        # table mutated mid-walk skipping/duplicating a DIFFERENT still-live
        # key -- is undefined behaviour; the standalone ``luau`` interpreter
        # happens to localise key removal to the removed key, so this guard
        # is a forward-looking invariant rather than a pre-fix-failing
        # repro. It still catches a regression that throws on mid-dispatch
        # mutation or delivers to the torn-down target.)
        scenario = textwrap.dedent("""\
            local calls = {}
            local function record(tag, ...)
                local args = {...}
                for i, v in ipairs(args) do args[i] = tostring(v) end
                table.insert(calls, tag .. "(" .. table.concat(args, ",") .. ")")
            end
            -- The first child's handler destroys the OTHER child's
            -- GameObject (mutating ``_componentsByGameObject`` mid-broadcast).
            local engineRef
            local victimGo
            local Killer = {} ; Killer.__index = Killer
            function Killer.new(_) return setmetatable({}, Killer) end
            function Killer:Extinguish(name)
                record("Killer.Extinguish", name)
                engineRef:destroy(victimGo)
            end
            -- Two more pre-existing children whose handlers must still fire.
            local function mk(stem)
                local M = {} ; M.__index = M
                function M.new(_) return setmetatable({}, M) end
                function M:Extinguish(name) record(stem .. ".Extinguish", name) end
                return M
            end
            local Root, Other, Victim = mk("Root"), mk("Other"), mk("Victim")
            local plan = {
                modules = {
                    root   = {stem = "Root",   runtime_bearing = true, module_path = "w"},
                    killer = {stem = "Killer", runtime_bearing = true, module_path = "x"},
                    other  = {stem = "Other",  runtime_bearing = true, module_path = "y"},
                    victim = {stem = "Victim", runtime_bearing = true, module_path = "z"},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "root",
                             game_object_id = "g", active = true, enabled = true,
                             config = {}},
                            {instance_id = "A:2", script_id = "killer",
                             game_object_id = "gK", active = true, enabled = true,
                             config = {}, parent_game_object_id = "g"},
                            {instance_id = "A:3", script_id = "other",
                             game_object_id = "gO", active = true, enabled = true,
                             config = {}, parent_game_object_id = "g"},
                            {instance_id = "A:4", script_id = "victim",
                             game_object_id = "gV", active = true, enabled = true,
                             config = {}, parent_game_object_id = "g"},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2", "A:3", "A:4"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local kGo = {Name = "Killer", _sceneRuntimeId = "gK", _children = {}}
            local oGo = {Name = "Other",  _sceneRuntimeId = "gO", _children = {}}
            local vGo = {Name = "Victim", _sceneRuntimeId = "gV", _children = {}}
            local parentGo = {Name = "Parent", _sceneRuntimeId = "g",
                              _children = {gK = kGo, gO = oGo, gV = vGo}}
            local services = servicesFor(plan,
                {root = Root, killer = Killer, other = Other, victim = Victim},
                {g = parentGo, gK = kGo, gO = oGo, gV = vGo})
            local engine = SceneRuntime.new(services, plan)
            engineRef = engine
            victimGo = vGo
            engine:start(nil)
            runDeferred()
            local rootComp
            for comp, m in pairs(engine._meta) do
                if m.gameObjectId == "g" then rootComp = comp end
            end
            local host = rootComp.host
            host:broadcastMessage(rootComp, "Extinguish", "Fire")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        # No throw; the broadcast completes (DONE printed).
        assert "DONE" in lines, out
        # The root and BOTH surviving pre-existing targets must fire even
        # though the Killer handler tore down a sibling mid-broadcast.
        assert "Root.Extinguish(Fire)" in lines, out
        assert "Killer.Extinguish(Fire)" in lines, out
        assert "Other.Extinguish(Fire)" in lines, out
        # The destroyed target must NOT receive (its bucket was torn down).
        assert "Victim.Extinguish(Fire)" not in lines, out

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


# ---------------------------------------------------------------------------
# Slice 2.1 -- player-alias receiver routing.
#
# The converter decouples the player into TWO entities: the Roblox character
# (what ``Players``/the touch handler points at -- carries NO Unity component)
# and the Unity Player-logic GameObject (carries ``GetItem``). A player-alias
# receiver (``Players``-service Player object, or a character limb) must route
# to the embodiment GameObject, NOT to whatever the alias literally points at.
#
# The flagship scene: one MODULE row ``player`` carries
# ``has_character_controller=true`` (the deterministic upstream signal that
# ``_initPlayerAuthority`` keys ``_player._playerScriptId`` on), placed on the
# embodiment GameObject ``gPlayer`` whose component ``Player`` implements
# ``GetItem``. Plus a NON-player door GameObject ``gDoor`` (component ``Door``,
# ``GetItem``) to prove the unchanged path. Services are extended with
# ``isClient=true`` and a mock ``players`` service whose
# ``GetPlayerFromCharacter`` maps a registered character Model to a Player
# object. The Player object's ``IsA("Player")`` returns true (the explicit
# guard ``_isPlayerAlias`` keys branch (a) on).
#
# RED-then-GREEN (AC-1): a ``Players``-service Player object hits NONE of
# ``_resolveReceiverGoId``'s cases (no ``_meta`` row, no stamped id with a
# component bucket, no ancestor Model in the scene graph), so PRE-fix it
# resolves to nil -> warn + no dispatch -> ``Player.GetItem`` never runs (the
# live ``hasRifle=nil`` symptom). POST-fix the player-FIRST branch routes it to
# the embodiment goId. The harness asserts ``Player.GetItem(Rifle)`` is
# recorded; against the Phase-1 tip ``a3451a4`` (no ``_isPlayerAlias`` branch)
# this assertion FAILS (CALLS=0 / warn emitted).
# ---------------------------------------------------------------------------

_PLAYER_SCENE_SETUP = """\
local calls = {}
local function record(tag, ...)
    local args = {...}
    for i, v in ipairs(args) do args[i] = tostring(v) end
    table.insert(calls, tag .. "(" .. table.concat(args, ",") .. ")")
end

-- Player-embodiment component (the Unity Player-logic GameObject's behaviour).
local Player = {} ; Player.__index = Player
function Player.new(_) return setmetatable({}, Player) end
function Player:GetItem(name) record("Player.GetItem", name) end
function Player:M(name) record("Player.M", name) end

-- A NON-player component (a door) -- proves the unchanged Phase-1 path.
local Door = {} ; Door.__index = Door
function Door.new(_) return setmetatable({}, Door) end
function Door:GetItem(name) record("Door.GetItem", name) end

local plan = {
    modules = {
        player = {stem = "Player", runtime_bearing = true, module_path = "p",
                  has_character_controller = true},
        door   = {stem = "Door",   runtime_bearing = true, module_path = "d"},
    },
    scenes = {
        A = {
            instances = {
                {instance_id = "A:1", script_id = "player",
                 game_object_id = "gPlayer", active = true, enabled = true,
                 config = {}},
                {instance_id = "A:2", script_id = "door",
                 game_object_id = "gDoor", active = true, enabled = true,
                 config = {}},
            },
            references = {},
            lifecycle_order = {"A:1", "A:2"},
        },
    },
    prefabs = {}, domain_overrides = {},
}
local playerGo = {Name = "PlayerLogic", _sceneRuntimeId = "gPlayer", _children = {}}
local doorGo   = {Name = "Door", _sceneRuntimeId = "gDoor", _children = {}}

-- The decoupled Roblox character + its owning Players-service Player object.
-- The character Model carries a Humanoid so ``playerFromTouch``'s ancestor
-- walk recognises it; it is NOT in the scene graph (no _SceneRuntimeId).
local plrObj = {Name = "Plr"}
function plrObj:IsA(class) return class == "Player" end
local charModel = {Name = "PlrChar"}
function charModel:IsA(class) return class == "Model" end
function charModel:FindFirstChildWhichIsA(class)
    if class == "Humanoid" then return {Name = "Humanoid"} end
    return nil
end
-- A character limb whose ancestor IS the character Model (for AC-2).
local limb = {Name = "RightHand", Parent = charModel}

local players = {}
function players:GetPlayerFromCharacter(model)
    if model == charModel then return plrObj end
    return nil
end

local services = servicesFor(plan, {player = Player, door = Door}, {
    gPlayer = playerGo, gDoor = doorGo,
})
services.players = players
services.isClient = true
local engine = SceneRuntime.new(services, plan)
engine:start(nil)
runDeferred()
-- A live door component gives us a host surface + a non-player component handle.
local doorComp
for comp, m in pairs(engine._meta) do
    if m.gameObjectId == "gDoor" then doorComp = comp end
end
local host = doorComp.host
"""


class TestPlayerAliasDispatch:

    def test_player_object_alias_routes_to_embodiment(self):
        # AC-1 (RED-then-GREEN). A Players-service Player object
        # (``IsA("Player")==true``) routes GetItem to the embodiment
        # GameObject's Player component -- NOT to the literal alias (which has
        # no component). Pre-fix (no _isPlayerAlias branch) this resolved to
        # nil -> the live ``hasRifle=nil`` symptom; the assertion below FAILS
        # against a3451a4. Also asserts dispatch count == #_playerGoIds() == 1
        # (D-P2-9: a double-registered rig -> >1 goId -> >1 dispatch -> a
        # duplicate rifle, caught here).
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(plrObj, "GetItem", "Rifle")
            print("GOIDS=" .. #engine:_playerGoIds())
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Player.GetItem(Rifle)" in lines, out
        # Single-embodiment: exactly one goId, exactly one dispatch.
        assert "GOIDS=1" in lines, out
        assert lines.count("Player.GetItem(Rifle)") == 1, out
        # Must NOT leak to the door (unrelated GameObject).
        assert "Door.GetItem(Rifle)" not in lines, out

    def test_character_limb_alias_routes_to_embodiment(self):
        # AC-2. A character limb BasePart whose ancestor character Model is
        # registered with players:GetPlayerFromCharacter routes via
        # playerFromTouch -> the embodiment, NOT the limb's own ancestor Model.
        # (Fails pre-fix: no player-alias branch.)
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(limb, "GetItem", "Rifle")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Player.GetItem(Rifle)" in lines, out
        assert lines.count("Player.GetItem(Rifle)") == 1, out

    def test_character_model_alias_routes_to_embodiment(self):
        # AC-1b (the live rifle-mount bug). Pickup passes ``plr.Character`` --
        # the character Model ITSELF -- as the sendMessage receiver. The Model
        # is Humanoid-bearing and resolves via players:GetPlayerFromCharacter,
        # so _playerFromAlias's INCLUSIVE ancestor walk (start at recv itself)
        # matches it as the FIRST node and routes GetItem to the embodiment.
        # FAILS against the pre-fix _isPlayerAlias (playerFromTouch(recv), which
        # starts at recv.Parent and structurally misses the Model itself).
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(charModel, "GetItem", "Rifle")
            print("GOIDS=" .. #engine:_playerGoIds())
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Player.GetItem(Rifle)" in lines, out
        assert "GOIDS=1" in lines, out
        assert lines.count("Player.GetItem(Rifle)") == 1, out
        assert "Door.GetItem(Rifle)" not in lines, out

    def test_npc_humanoid_model_not_player_alias(self):
        # Over-match boundary. A Model that HAS a Humanoid but for which
        # players:GetPlayerFromCharacter returns nil (an NPC) is NOT a player
        # alias -> _playerFromAlias returns nil -> the receiver takes the
        # UNCHANGED non-player _resolveReceiverGoId path. Here the NPC Model has
        # no scene-runtime id and no registered component, so it resolves to no
        # GameObject (warn + no dispatch) -- it must NOT route to the player
        # embodiment.
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            -- An NPC: Humanoid-bearing Model that GetPlayerFromCharacter does
            -- NOT resolve to a Player (players:GetPlayerFromCharacter returns
            -- nil for any model != charModel).
            local npc = {Name = "NpcChar"}
            function npc:IsA(class) return class == "Model" end
            function npc:FindFirstChildWhichIsA(class)
                if class == "Humanoid" then return {Name = "Humanoid"} end
                return nil
            end
            print("ALIAS=" .. tostring(engine:_isPlayerAlias(npc)))
            host:sendMessage(npc, "GetItem", "Rifle")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        # The NPC is NOT a player alias (the inclusive walk must not over-match).
        assert "ALIAS=false" in lines, out
        # And it must NOT route to the player embodiment.
        assert "Player.GetItem(Rifle)" not in lines, out

    def test_nested_humanoid_model_climbs_to_outer_player(self):
        # Robustness guard. The receiver is a NESTED non-player Humanoid Model
        # (an inner NPC/rig) whose ``players:GetPlayerFromCharacter`` returns
        # nil, but whose .Parent ancestor chain reaches the REAL player's
        # character Model (charModel, for which GetPlayerFromCharacter -> plrObj).
        # Branch (b)'s inclusive walk hits the inner Humanoid Model FIRST (resolves
        # nil) and must KEEP CLIMBING past it to find the outer player character
        # Model -> the embodiment. RED if branch (b) returns at the first Humanoid
        # Model (``return players:GetPlayerFromCharacter(node)`` without the
        # ``if p ~= nil then return p end`` climb-past): the inner nil short-circuits
        # the walk -> no route -> Player.GetItem never fires.
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            -- An inner NPC rig Model parented UNDER the real player character.
            -- It is Humanoid-bearing but GetPlayerFromCharacter(innerNpc) == nil.
            local innerNpc = {Name = "InnerRig", Parent = charModel}
            function innerNpc:IsA(class) return class == "Model" end
            function innerNpc:FindFirstChildWhichIsA(class)
                if class == "Humanoid" then return {Name = "Humanoid"} end
                return nil
            end
            host:sendMessage(innerNpc, "GetItem", "Rifle")
            print("GOIDS=" .. #engine:_playerGoIds())
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        # The walk climbed past the inner nil-resolving Humanoid Model to the
        # outer player character -> routed to the embodiment.
        assert "Player.GetItem(Rifle)" in lines, out
        assert "GOIDS=1" in lines, out
        assert lines.count("Player.GetItem(Rifle)") == 1, out
        assert "Door.GetItem(Rifle)" not in lines, out

    def test_humanoid_handle_routes_to_embodiment(self):
        # Handle variant: a Humanoid instance whose .Parent is the character
        # Model. The inclusive walk climbs Humanoid -> charModel (Humanoid-
        # bearing Model) -> GetPlayerFromCharacter -> the embodiment. Confirms
        # the resolver covers the Humanoid/HRP-handle shape (the limb test,
        # AC-2, covers a plain descendant BasePart).
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            local hum = {Name = "Humanoid", Parent = charModel}
            host:sendMessage(hum, "GetItem", "Rifle")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Player.GetItem(Rifle)" in lines, out
        assert lines.count("Player.GetItem(Rifle)") == 1, out

    def test_non_player_receiver_takes_unchanged_path(self):
        # AC-3. A non-player component-table receiver (the door) is NOT a
        # player alias (no IsA; playerFromTouch -> nil) -> falls through to the
        # UNCHANGED Phase-1 _resolveReceiverGoId path -> dispatches to its OWN
        # GameObject (gDoor), never the player embodiment.
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            host:sendMessage(doorComp, "GetItem", "Key")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Door.GetItem(Key)" in lines, out
        assert "Player.GetItem(Key)" not in lines, out

    def test_non_player_basepart_takes_unchanged_path(self):
        # AC-3 (BasePart variant). A loose BasePart whose ancestor Model is the
        # door GameObject (NOT a player character) is not a player alias ->
        # unchanged ancestor-Model walk dispatches to gDoor.
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            local part = {Name = "Knob", _sceneRuntimeId = "loose"}
            part.FindFirstAncestorWhichIsA = function(self, class)
                if class == "Model" then return doorGo end
                return nil
            end
            -- No player-character ancestor: playerFromTouch must return nil.
            part.Parent = nil
            host:sendMessage(part, "GetItem", "Knob")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Door.GetItem(Knob)" in lines, out
        assert "Player.GetItem(Knob)" not in lines, out

    def test_fail_closed_when_no_player_bound(self):
        # AC-4. With isClient=false (-> _player == nil), a Player-object
        # receiver makes _isPlayerAlias true (branch (a)), but _playerGoIds()
        # returns {} -> the branch warns "no player-embodiment GameObject
        # bound" and dispatches NOTHING, without erroring. AC-4 also requires
        # the warn to be EMITTED -- the harness ``logs`` table captures every
        # self._warn -> services.warn -> logWarn call, so we scan it for the
        # design's warn substring and assert it fired (no dispatch alone does
        # not prove the warn path ran).
        scenario = _PLAYER_SCENE_SETUP.replace(
            "services.isClient = true", "services.isClient = false"
        ) + textwrap.dedent("""\
            print("GOIDS=" .. #engine:_playerGoIds())
            host:sendMessage(plrObj, "GetItem", "Rifle")
            print("CALLS=" .. #calls)
            local warned = false
            for _, msg in ipairs(logs) do
                if string.find(msg, "no player-embodiment GameObject bound", 1, true) then
                    warned = true
                end
            end
            print("WARNED=" .. tostring(warned))
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "GOIDS=0" in lines, out
        assert "CALLS=0" in lines, out
        # AC-4 warn clause: the fail-closed warn is actually emitted.
        assert "WARNED=true" in lines, out
        assert "DONE" in lines, out  # no throw: rc == 0 + DONE printed.

    def test_fail_closed_when_player_script_id_nil(self):
        # AC-4 (second guard clause). Distinct from _player == nil: here
        # self._player EXISTS but _player._playerScriptId is nil, exercising the
        # SECOND clause of ``if not p or not p._playerScriptId`` in
        # _playerGoIds. A Player-object receiver is still _isPlayerAlias=true,
        # but _playerGoIds() returns {} -> warn + NO dispatch + NO throw.
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            -- Force the "_player set but _playerScriptId nil" state directly
            -- (a CC module that left the rig-resolution key unbound). _player
            -- is non-nil so we exercise the SECOND guard clause, not the first.
            engine._player = {_yaw = 0, _pitch = 0, _playerScriptId = nil}
            print("HASPLAYER=" .. tostring(engine._player ~= nil))
            print("GOIDS=" .. #engine:_playerGoIds())
            host:sendMessage(plrObj, "GetItem", "Rifle")
            print("CALLS=" .. #calls)
            local warned = false
            for _, msg in ipairs(logs) do
                if string.find(msg, "no player-embodiment GameObject bound", 1, true) then
                    warned = true
                end
            end
            print("WARNED=" .. tostring(warned))
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        # _player EXISTS (second clause is the one under test).
        assert "HASPLAYER=true" in lines, out
        assert "GOIDS=0" in lines, out
        assert "CALLS=0" in lines, out
        assert "WARNED=true" in lines, out
        assert "DONE" in lines, out  # no throw.

    def test_per_goid_dedupe_no_double_dispatch(self):
        # D-P2-9 (per-goId dedupe). TWO matching _meta rows (two components)
        # share ONE gameObjectId, both keyed to _playerScriptId. _playerGoIds()
        # must return EXACTLY ONE goId (the ``seen[goId]`` collapse), and
        # sendMessage to a player alias dispatches to that goId exactly once --
        # the active-gate + bucket already invokes BOTH components on the one
        # GO, so the point is the goId LIST is deduped (no double dispatch over
        # the same goId). A regression dropping ``seen`` would make goIds =
        # {gPlayer, gPlayer} -> each component invoked twice -> this test FAILS
        # (the duplicate-rifle multiplicity).
        scenario = textwrap.dedent("""\
            local calls = {}
            local function record(tag, ...)
                local args = {...}
                for i, v in ipairs(args) do args[i] = tostring(v) end
                table.insert(calls, tag .. "(" .. table.concat(args, ",") .. ")")
            end
            -- TWO components on the SAME GameObject (gPlayer), both under the
            -- SAME player script_id (one module row), so two _meta rows share
            -- one goId. Dispatch keys on meta.classTable[name], so both
            -- components share ONE class (PlayerMod.GetItem); each instance
            -- carries a distinct config ``tag`` to prove BOTH components fire.
            local PlayerMod = {} ; PlayerMod.__index = PlayerMod
            function PlayerMod.new(config)
                return setmetatable({_tag = (config or {}).tag or "?"}, PlayerMod)
            end
            function PlayerMod:GetItem(name) record(self._tag .. ".GetItem", name) end
            local plan = {
                modules = {
                    player = {stem = "Player", runtime_bearing = true,
                              module_path = "p", has_character_controller = true},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "player",
                             game_object_id = "gPlayer", active = true,
                             enabled = true, config = {tag = "Foo"}},
                            {instance_id = "A:2", script_id = "player",
                             game_object_id = "gPlayer", active = true,
                             enabled = true, config = {tag = "Bar"}},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local playerGo = {Name = "PlayerLogic", _sceneRuntimeId = "gPlayer",
                              _children = {}}
            local plrObj = {Name = "Plr"}
            function plrObj:IsA(class) return class == "Player" end
            local players = {}
            function players:GetPlayerFromCharacter(model) return nil end
            local services = servicesFor(plan, {player = PlayerMod},
                                         {gPlayer = playerGo})
            services.players = players
            services.isClient = true
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            local host
            for comp, m in pairs(engine._meta) do host = comp.host end
            print("GOIDS=" .. #engine:_playerGoIds())
            host:sendMessage(plrObj, "GetItem", "Rifle")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        # Two _meta rows share one goId -> deduped to exactly ONE goId.
        assert "GOIDS=1" in lines, out
        # Both components on that ONE goId fire (Unity multi-component) ...
        assert "Foo.GetItem(Rifle)" in lines, out
        assert "Bar.GetItem(Rifle)" in lines, out
        # ... each EXACTLY once: the goId is not dispatched twice (dropping
        # ``seen`` would double-dispatch the goId -> count == 2 each).
        assert lines.count("Foo.GetItem(Rifle)") == 1, out
        assert lines.count("Bar.GetItem(Rifle)") == 1, out

    def test_multiple_rig_instances_each_dispatch(self):
        # AC-5 / D-P2-9 (multi-goId fan-out). The player MODULE (ONE row
        # carrying has_character_controller -> ONE _playerScriptId) placed on
        # TWO distinct GameObjects (gP1/gP2) yields TWO distinct goIds after
        # dedupe, and BOTH embodiment components receive the dispatch. To prove
        # two DISTINCT embodiments each fire (not one component recorded twice),
        # each instance's config carries a distinct ``tag`` and the single
        # Player class records under that tag -> we assert BOTH "P1.GetItem" and
        # "P2.GetItem" appear AND the dispatch count == #_playerGoIds() == 2.
        # (Pre-fix: no _isPlayerAlias branch -> the Player object resolves to
        # nil -> NEITHER tag is recorded; this assertion FAILS against a3451a4.)
        scenario = textwrap.dedent("""\
            local calls = {}
            local function record(tag, ...)
                local args = {...}
                for i, v in ipairs(args) do args[i] = tostring(v) end
                table.insert(calls, tag .. "(" .. table.concat(args, ",") .. ")")
            end
            -- ONE class table for the ONE player MODULE; each instance gets a
            -- distinct ``tag`` via its config so the two embodiments record
            -- under DISTINCT tags (proving two distinct components each fire,
            -- not one component recorded twice).
            local Player = {} ; Player.__index = Player
            function Player.new(config)
                return setmetatable({_tag = (config or {}).tag or "?"}, Player)
            end
            function Player:GetItem(name) record(self._tag .. ".GetItem", name) end
            local plan = {
                modules = {
                    -- ONE module row carries has_character_controller; it is
                    -- placed on TWO GameObjects (P2-F multi-instance). Both
                    -- instances share the SAME script_id so _playerGoIds
                    -- collects both (distinct) goIds.
                    player = {stem = "Player", runtime_bearing = true,
                              module_path = "p", has_character_controller = true},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "player",
                             game_object_id = "gP1", active = true,
                             enabled = true, config = {tag = "P1"}},
                            {instance_id = "A:2", script_id = "player",
                             game_object_id = "gP2", active = true,
                             enabled = true, config = {tag = "P2"}},
                        },
                        references = {},
                        lifecycle_order = {"A:1", "A:2"},
                    },
                },
                prefabs = {}, domain_overrides = {},
            }
            local g1 = {Name = "P1", _sceneRuntimeId = "gP1", _children = {}}
            local g2 = {Name = "P2", _sceneRuntimeId = "gP2", _children = {}}
            local plrObj = {Name = "Plr"}
            function plrObj:IsA(class) return class == "Player" end
            local players = {}
            function players:GetPlayerFromCharacter(model) return nil end
            local services = servicesFor(plan, {player = Player}, {gP1 = g1, gP2 = g2})
            services.players = players
            services.isClient = true
            local engine = SceneRuntime.new(services, plan)
            engine:start(nil)
            runDeferred()
            local host
            for comp, m in pairs(engine._meta) do host = comp.host end
            print("GOIDS=" .. #engine:_playerGoIds())
            host:sendMessage(plrObj, "GetItem", "Rifle")
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "GOIDS=2" in lines, out
        # TWO DISTINCT embodiments each receive the dispatch (genuine fan-out).
        assert "P1.GetItem(Rifle)" in lines, out
        assert "P2.GetItem(Rifle)" in lines, out
        # Dispatch count == #_playerGoIds() == 2; neither component doubled.
        assert lines.count("P1.GetItem(Rifle)") == 1, out
        assert lines.count("P2.GetItem(Rifle)") == 1, out
        get_item_total = sum(1 for ln in lines if ln.endswith(".GetItem(Rifle)"))
        assert get_item_total == 2, out

    def test_broadcast_to_player_alias_flat_per_goid(self):
        # AC-6. broadcastMessage to a player alias dispatches FLAT to the same
        # player goId set as sendMessage (BC-1: no subtree descent). Single
        # embodiment -> exactly one dispatch.
        scenario = _PLAYER_SCENE_SETUP + textwrap.dedent("""\
            host:broadcastMessage(plrObj, "M", "Reload")
            print("GOIDS=" .. #engine:_playerGoIds())
            for _, c in ipairs(calls) do print(c) end
            print("DONE")
        """)
        rc, out, err = _run_scenario(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        lines = out.strip().splitlines()
        assert "Player.M(Reload)" in lines, out
        assert "GOIDS=1" in lines, out
        assert lines.count("Player.M(Reload)") == 1, out

    def test_broadcast_non_player_keeps_phase1_descent(self):
        # AC-6 (control). A non-player broadcastMessage still runs the Phase-1
        # descendant walk unchanged (reuses the Phase-1 child fixture).
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
        assert "Baz.GetItem(Ammo)" in lines, out  # descended to child
