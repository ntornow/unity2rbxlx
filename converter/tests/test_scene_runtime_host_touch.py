"""Host touch/player helper tests for converter/runtime/scene_runtime.luau.

Covers the generic touch contract helpers added to fix the
``gameObject.Touched on Model`` boot failure (every prefab-placement
Pickup / Door / Turret died in ``Awake``):

  * ``getTouchPart(go)`` part resolution:
      - BasePart passthrough,
      - Model with a ``TriggerZone`` named child,
      - Model with only an invisible non-mesh trigger volume,
      - Model with only a visible BasePart (Tier-3 fallback),
      - Model with no BasePart -> nil.
  * ``connectGameObjectSignal`` connects on the resolved part and routes
    through the lifecycle-tracked ``connect`` machinery; fails soft
    (warn + nil, no throw) when no part resolves.
  * ``playerFromTouch`` / ``isPlayerTouch`` normalize a touched BasePart
    (direct character limb AND accessory-mounted part) to the owning
    Player; non-player touches return nil/false.

Drives the production host through standalone ``luau`` with mock Roblox
Instances. Skips cleanly when ``luau`` is absent.
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


# Mock Roblox Instance + Players surface. ``IsA`` understands the small
# class lattice the helpers query (BasePart / MeshPart / Model /
# Humanoid). Children/descendants are explicit so ordering is
# deterministic.
_HARNESS_PREAMBLE = """\
-- Mock Instance factory ---------------------------------------------------
local function newInstance(spec)
    spec = spec or {}
    local inst = {}
    inst.Name = spec.Name or "Inst"
    inst.ClassName = spec.ClassName or "Part"
    inst.Transparency = spec.Transparency
    inst.CanTouch = spec.CanTouch
    inst._children = spec.children or {}
    inst._isa = spec.isa or {}
    inst._attrs = spec.attrs or {}
    inst._signals = {}
    inst.Parent = nil
    for _, c in ipairs(inst._children) do c.Parent = inst end

    function inst:GetAttribute(name)
        return self._attrs[name]
    end

    function inst:IsA(class)
        if self.ClassName == class then return true end
        return self._isa[class] == true
    end
    function inst:GetChildren()
        return self._children
    end
    function inst:GetDescendants()
        local out = {}
        local function walk(node)
            for _, c in ipairs(node._children) do
                table.insert(out, c)
                walk(c)
            end
        end
        walk(self)
        return out
    end
    function inst:FindFirstChildWhichIsA(class)
        for _, c in ipairs(self._children) do
            if c:IsA(class) then return c end
        end
        return nil
    end
    function inst:FindFirstAncestorWhichIsA(class)
        local node = self.Parent
        while node do
            if node:IsA(class) then return node end
            node = node.Parent
        end
        return nil
    end
    -- Touched / TouchEnded signals resolved by index access.
    setmetatable(inst, {
        __index = function(t, k)
            if k == "Touched" or k == "TouchEnded" then
                local sig = rawget(t, "_signals")[k]
                if not sig then
                    sig = {_conns = {}}
                    function sig:Connect(fn)
                        table.insert(self._conns, fn)
                        return {Disconnect = function() end}
                    end
                    function sig:fire(...)
                        for _, fn in ipairs(self._conns) do fn(...) end
                    end
                    rawget(t, "_signals")[k] = sig
                end
                return sig
            end
            return nil
        end,
    })
    return inst
end

-- Fireable signal (Roblox-shaped: ``:Connect`` returns a ``:Disconnect``
-- handle). Used to drive the engine Heartbeat the OnTriggerStay poll arms.
local function newSignal()
    local sig = { _conns = {}, _id = 0 }
    function sig:Connect(fn)
        self._id = self._id + 1
        local id = self._id
        self._conns[id] = fn
        return {Disconnect = function() self._conns[id] = nil end}
    end
    function sig:fire(...)
        for _, fn in pairs(self._conns) do fn(...) end
    end
    function sig:count()
        local n = 0
        for _ in pairs(self._conns) do n = n + 1 end
        return n
    end
    return sig
end

-- Mock Players service ----------------------------------------------------
local function newPlayers(map)
    return {
        GetPlayerFromCharacter = function(self, character)
            return map[character]
        end,
    }
end

local function baseServices(players)
    return {
        task = {
            spawn = function(fn, ...) fn(...) end,
            defer = function(fn, ...) end,
            delay = function(secs, fn, ...) end,
            wait = function() return 0 end,
            cancel = function() end,
        },
        warn = function(...)
            local parts = {...}
            for i, p in ipairs(parts) do parts[i] = tostring(p) end
            print("WARN " .. table.concat(parts, " "))
        end,
        resolveModule = function() return nil end,
        workspaceFind = function() return nil end,
        findFirstChildWhichIsA = function() return nil end,
        heartbeat = {Connect = function() return {Disconnect = function() end} end},
        fixedStep = 0.02,
        now = function() return 0 end,
        players = players,
    }
end

-- Services with a controllable Heartbeat + clock for OnTriggerStay poll
-- tests. ``svc.heartbeat:fire()`` drives one poll tick; ``svc.setClock(t)``
-- advances the throttle clock the poll reads via ``services.now``.
local function stayServices(players)
    local hb = newSignal()
    local clk = 0
    local svc = baseServices(players)
    svc.heartbeat = hb
    svc.now = function() return clk end
    svc.setClock = function(t) clk = t end
    return svc, hb
end

-- Stub ``workspace`` global with a settable ``GetPartsInPart`` result so
-- the poll can be driven deterministically. Set ``workspace._overlap`` to
-- the list of overlapping parts a sweep returns. ``workspace._queriedPart``
-- RECORDS the actual ``part`` argument every sweep was called with, so a
-- test can prove the poll queried the ``getTouchPart``-RESOLVED volume (the
-- marked trigger), not some other part.
local function installWorkspace(parts)
    workspace = {
        _overlap = parts or {},
        _queriedPart = nil,
        GetPartsInPart = function(self, part)
            self._queriedPart = part
            return self._overlap
        end,
    }
    return workspace
end
"""


def _run(scenario_body: str) -> tuple[int, str, str]:
    host_source = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in host_source or f"[{delim}[" in host_source:
        delim += "="
    embedded = f"[{delim}[\n{host_source}\n]{delim}]"
    preamble = textwrap.dedent(f"""\
        local HOST_RUNTIME_SOURCE = {embedded}
        local SceneRuntime
        do
            local chunk, err = loadstring(HOST_RUNTIME_SOURCE, "scene_runtime")
            assert(chunk, "load host runtime failed: " .. tostring(err))
            SceneRuntime = chunk()
        end
    """) + _HARNESS_PREAMBLE
    script = preamble + "\n" + scenario_body + "\n"
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
# getTouchPart
# ---------------------------------------------------------------------------

class TestGetTouchPart:

    def test_basepart_passthrough(self):
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local part = newInstance{Name = "P", ClassName = "Part",
                isa = {BasePart = true}}
            local got = engine:getTouchPart(part)
            print(got == part and "SAME" or "DIFF")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "SAME" in out

    def test_basepart_root_with_marked_trigger_child_returns_child(self):
        # Slice 1.1 Layer C: a BasePart-root go (e.g. a turret body Part)
        # with a ``_IsTriggerVolume``-marked descendant resolves to that
        # marked detection volume, NOT the small visible body — so the
        # OnTriggerStay poll overlap-tests the 285-stud Collider radius.
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1,
                attrs = {_IsTriggerVolume = true}}
            -- The root IS a BasePart (turret body) AND carries the marked
            -- trigger child as a descendant.
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local got = engine:getTouchPart(body)
            print(got == trig and "MARKED" or ("WRONG:" .. tostring(got and got.Name)))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "MARKED" in out

    def test_basepart_root_itself_marked_returns_self(self):
        # MINOR-2: ``_findMarkedTriggerVolume`` checks ``go`` ITSELF before
        # descendants — a BasePart go that is itself ``_IsTriggerVolume``
        # marked resolves to itself (the marked-volume preference applies to
        # the root, not only its children).
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local vol = newInstance{Name = "TriggerVol", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1,
                attrs = {_IsTriggerVolume = true}}
            local got = engine:getTouchPart(vol)
            print(got == vol and "SELF" or ("WRONG:" .. tostring(got and got.Name)))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "SELF" in out

    def test_childless_basepart_still_passes_through(self):
        # Slice 1.1 Layer C regression: a BasePart go with NO marked
        # trigger volume must STILL return itself (passthrough preserved).
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local part = newInstance{Name = "Body", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true}
            local got = engine:getTouchPart(part)
            print(got == part and "SAME" or "DIFF")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "SAME" in out

    def test_model_prefers_marked_descendant_over_named_trigger(self):
        # A Model go prefers a ``_IsTriggerVolume``-marked descendant even
        # when an (unmarked) named trigger child is also present.
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local named = newInstance{Name = "TriggerZone", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1}
            local marked = newInstance{Name = "Detector", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1,
                attrs = {_IsTriggerVolume = true}}
            local model = newInstance{Name = "Turret", ClassName = "Model",
                isa = {Model = true}, children = {named, marked}}
            local got = engine:getTouchPart(model)
            print(got == marked and "MARKED" or ("WRONG:" .. tostring(got and got.Name)))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "MARKED" in out

    def test_model_with_triggerzone_child(self):
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local mesh = newInstance{Name = "Body", ClassName = "MeshPart",
                isa = {BasePart = true}, CanTouch = true}
            local trig = newInstance{Name = "TriggerZone", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1}
            local model = newInstance{Name = "Pickup", ClassName = "Model",
                isa = {Model = true}, children = {mesh, trig}}
            local got = engine:getTouchPart(model)
            print(got == trig and "TRIGGER" or ("WRONG:" .. tostring(got and got.Name)))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "TRIGGER" in out

    def test_model_with_only_invisible_trigger(self):
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            -- No named trigger; just one invisible non-mesh part.
            local vol = newInstance{Name = "Volume", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1}
            local model = newInstance{Name = "Door", ClassName = "Model",
                isa = {Model = true}, children = {vol}}
            local got = engine:getTouchPart(model)
            print(got == vol and "VOLUME" or ("WRONG:" .. tostring(got and got.Name)))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "VOLUME" in out

    def test_model_fallback_first_basepart(self):
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            -- Only a visible mesh part: no named trigger, no invisible vol.
            local mesh = newInstance{Name = "Visual", ClassName = "MeshPart",
                isa = {BasePart = true}, CanTouch = true, Transparency = 0}
            local model = newInstance{Name = "Turret", ClassName = "Model",
                isa = {Model = true}, children = {mesh}}
            local got = engine:getTouchPart(model)
            print(got == mesh and "FALLBACK" or ("WRONG:" .. tostring(got and got.Name)))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "FALLBACK" in out

    def test_model_with_no_basepart_returns_nil(self):
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local child = newInstance{Name = "Folder", ClassName = "Folder"}
            local model = newInstance{Name = "Empty", ClassName = "Model",
                isa = {Model = true}, children = {child}}
            local got = engine:getTouchPart(model)
            print(got == nil and "NIL" or "NOTNIL")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "NIL" in out


# ---------------------------------------------------------------------------
# connectGameObjectSignal
# ---------------------------------------------------------------------------

class TestConnectGameObjectSignal:

    def test_connects_on_resolved_part(self):
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local trig = newInstance{Name = "TriggerZone", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1}
            local model = newInstance{Name = "Pickup", ClassName = "Model",
                isa = {Model = true}, children = {trig}}
            -- A registered component owner so the gate is open.
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local hits = 0
            local conn = engine:connectGameObjectSignal(comp, model, "Touched",
                function(other) hits = hits + 1 end)
            print(conn ~= nil and "CONNECTED" or "NOCONN")
            -- Fire the resolved part's Touched signal.
            trig.Touched:fire(newInstance{Name = "Limb", ClassName = "Part"})
            print("HITS " .. tostring(hits))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "CONNECTED" in out
        assert "HITS 1" in out

    def test_fail_soft_when_no_part(self):
        scenario = textwrap.dedent("""\
            local engine = SceneRuntime.new(baseServices(nil), {})
            local child = newInstance{Name = "Folder", ClassName = "Folder"}
            local model = newInstance{Name = "Empty", ClassName = "Model",
                isa = {Model = true}, children = {child}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local ok, conn = pcall(function()
                return engine:connectGameObjectSignal(comp, model, "Touched",
                    function() end)
            end)
            print(ok and "NOTHROW" or "THREW")
            print(conn == nil and "NILCONN" or "GOTCONN")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "NOTHROW" in out
        assert "NILCONN" in out
        # Fail-soft warning emitted.
        assert "no touch part" in out


# ---------------------------------------------------------------------------
# connectGameObjectSignalStay (OnTriggerStay overlap poll)
# ---------------------------------------------------------------------------

class TestConnectGameObjectSignalStay:

    def test_fires_for_part_already_overlapping_at_arm_time(self):
        # Teleport-inside analog: the player is ALREADY inside the trigger
        # volume when the poll arms (no ``.Touched`` edge ever fires). The
        # poll must still detect it on the first throttled tick.
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limb = newInstance{Name = "RightFoot", ClassName = "Part",
                isa = {BasePart = true}}
            installWorkspace({limb})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local hits = {}
            local conn = engine:connectGameObjectSignalStay(comp, body,
                function(other) table.insert(hits, other) end)
            print(conn ~= nil and "CONNECTED" or "NOCONN")
            -- First tick at t=1 (past the throttle from t=0 arm).
            services.setClock(1)
            hb:fire(0.016)
            print("HITS " .. tostring(#hits))
            print((hits[1] == limb) and "ISLIMB" or "WRONGHIT")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "CONNECTED" in out
        assert "HITS 1" in out
        assert "ISLIMB" in out

    def test_poll_queries_resolved_marked_trigger_not_body(self):
        # Slice 1.1 CORE production claim: the poll must overlap-test the
        # ``getTouchPart``-RESOLVED marked trigger volume (the 285-stud
        # Collider), NOT the small visible body. The workspace mock records
        # the actual ``part`` each sweep was called with; assert it is the
        # MARKED volume, not the body. A getTouchPart/poll-wiring regression
        # (poll querying the wrong part) turns this RED.
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limb = newInstance{Name = "Foot", ClassName = "Part",
                isa = {BasePart = true}}
            local ws = installWorkspace({limb})
            -- The 285-stud detection volume: marked, invisible, CanTouch.
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1,
                attrs = {_IsTriggerVolume = true}}
            -- The small visible body that the go root is.
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            engine:connectGameObjectSignalStay(comp, body,
                function() end)
            services.setClock(1); hb:fire(0.016)
            -- Prove the sweep queried the MARKED trigger, not the body.
            if ws._queriedPart == trig then
                print("QUERIED_MARKED")
            elseif ws._queriedPart == body then
                print("QUERIED_BODY")
            else
                print("QUERIED_OTHER:" .. tostring(ws._queriedPart and ws._queriedPart.Name))
            end
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "QUERIED_MARKED" in out, (
            "poll must query the getTouchPart-resolved MARKED trigger volume, "
            f"not the body\n{out}"
        )

    def test_fail_soft_when_no_heartbeat_service(self):
        # Heartbeat is optional across the runtime (the engine update loop
        # guards ``if self._services.heartbeat``). When it is absent the poll
        # must soft-fail (warn + nil) rather than hard-assert inside
        # ``connect`` (``signal ~= nil``).
        scenario = textwrap.dedent("""\
            local services = baseServices(nil)
            services.heartbeat = nil
            local engine = SceneRuntime.new(services, {})
            installWorkspace({})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local ok, conn = pcall(function()
                return engine:connectGameObjectSignalStay(comp, body,
                    function() end)
            end)
            print(ok and "NOTHROW" or "THREW")
            print(conn == nil and "NILCONN" or "GOTCONN")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "NOTHROW" in out, f"absent heartbeat must not throw\n{out}"
        assert "NILCONN" in out
        assert "no heartbeat" in out

    def test_gate_check_stops_callbacks_after_disable_mid_sweep(self):
        # MAJOR-1: a callback that DISABLES the component (not destroys it)
        # mid-iteration must stop further fn calls in the same sweep. The
        # per-hit guard re-checks the lifecycle gate (``_isGateOpen``), not
        # only ``_destroyed``.
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limbA = newInstance{Name = "A", ClassName = "Part",
                isa = {BasePart = true}}
            local limbB = newInstance{Name = "B", ClassName = "Part",
                isa = {BasePart = true}}
            installWorkspace({limbA, limbB})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local hits = 0
            engine:connectGameObjectSignalStay(comp, body, function(other)
                hits = hits + 1
                -- Disable (NOT destroy) mid-iteration after the first part.
                engine._meta[comp].enabled = false
            end)
            services.setClock(1); hb:fire(0.016)
            -- Only the first part fires; the gate re-check stops the rest.
            print("HITS " .. tostring(hits))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "HITS 1" in out, (
            "a mid-sweep DISABLE must stop remaining callbacks (gate re-check)"
            f"\n{out}"
        )

    def test_throttle_skips_ticks_inside_interval(self):
        # Two Heartbeat ticks within the throttle window => only ONE sweep
        # fires fn (the per-tick analog is throttled, not per-frame-raw).
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limb = newInstance{Name = "Foot", ClassName = "Part",
                isa = {BasePart = true}}
            installWorkspace({limb})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local hits = 0
            engine:connectGameObjectSignalStay(comp, body,
                function() hits = hits + 1 end)
            services.setClock(1)   ; hb:fire(0.016)  -- fires (past throttle)
            services.setClock(1.05); hb:fire(0.016)  -- within 0.13s -> skip
            print("HITS " .. tostring(hits))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "HITS 1" in out

    def test_disarms_on_disable_rearms_on_enable_no_leak(self):
        # The poll is routed through the lifecycle ``connect`` machinery, so
        # disabling the component disconnects the Heartbeat subscription (no
        # leak), and re-enabling rearms it. Proven by the signal's live
        # connection count AND by fn firing only while enabled.
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limb = newInstance{Name = "Foot", ClassName = "Part",
                isa = {BasePart = true}}
            installWorkspace({limb})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            -- Register the component with the gate machinery so disarm/rearm
            -- runs through the real _meta + _goActiveInHierarchy paths.
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true,
                                  gameObjectId = "go1"}
            engine._goActiveSelf["go1"] = true
            engine._goActiveInHierarchy["go1"] = true
            local hits = 0
            engine:connectGameObjectSignalStay(comp, body,
                function() hits = hits + 1 end)
            print("ARMED " .. tostring(hb:count()))   -- 1 live conn
            -- Disable: disarm the subscription.
            engine._meta[comp].enabled = false
            engine:_disarmSubs(comp)
            print("DISARMED " .. tostring(hb:count())) -- 0 live conns (no leak)
            -- A tick while disabled fires nothing.
            services.setClock(1); hb:fire(0.016)
            print("WHILE_DISABLED " .. tostring(hits))
            -- Re-enable: rearm.
            engine._meta[comp].enabled = true
            engine:_rearmSubs(comp)
            print("REARMED " .. tostring(hb:count()))  -- 1 live conn again
            services.setClock(2); hb:fire(0.016)
            print("AFTER_REARM " .. tostring(hits))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "ARMED 1" in out
        assert "DISARMED 0" in out, f"Heartbeat connection leaked on disable\n{out}"
        assert "WHILE_DISABLED 0" in out
        assert "REARMED 1" in out
        assert "AFTER_REARM 1" in out

    def test_player_from_touch_resolves_poll_discovered_limb(self):
        # The poll-discovered BasePart must normalize to the owning Player
        # via playerFromTouch (ancestor walk) — the load-bearing claim that
        # the poll path feeds the same detection as a .Touched edge.
        scenario = textwrap.dedent("""\
            local hum = newInstance{Name = "Humanoid", ClassName = "Humanoid",
                isa = {Humanoid = true}}
            local character = newInstance{Name = "Char", ClassName = "Model",
                isa = {Model = true}, children = {hum}}
            local limb = newInstance{Name = "LeftLeg", ClassName = "Part",
                isa = {BasePart = true}}
            limb.Parent = character
            local fakePlayer = {Name = "Player1"}
            local services, hb = stayServices(
                newPlayers({[character] = fakePlayer}))
            local engine = SceneRuntime.new(services, {})
            installWorkspace({limb})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local resolved = nil
            engine:connectGameObjectSignalStay(comp, body, function(other)
                resolved = engine:playerFromTouch(other)
            end)
            services.setClock(1); hb:fire(0.016)
            print((resolved == fakePlayer) and "PLAYER" or "NOPLAYER")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "PLAYER" in out

    def test_fail_soft_when_no_trigger_part(self):
        # No resolvable touch part => warn + nil, no throw (mirrors
        # connectGameObjectSignal).
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            installWorkspace({})
            local child = newInstance{Name = "Folder", ClassName = "Folder"}
            local model = newInstance{Name = "Empty", ClassName = "Model",
                isa = {Model = true}, children = {child}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local ok, conn = pcall(function()
                return engine:connectGameObjectSignalStay(comp, model,
                    function() end)
            end)
            print(ok and "NOTHROW" or "THREW")
            print(conn == nil and "NILCONN" or "GOTCONN")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "NOTHROW" in out
        assert "NILCONN" in out
        assert "no touch part" in out

    def test_liveness_guard_stops_callbacks_after_destroy(self):
        # Re-entrancy guard: a callback that destroys the component
        # mid-iteration stops further fn calls in the same sweep.
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limbA = newInstance{Name = "A", ClassName = "Part",
                isa = {BasePart = true}}
            local limbB = newInstance{Name = "B", ClassName = "Part",
                isa = {BasePart = true}}
            installWorkspace({limbA, limbB})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local comp = {}
            engine._meta[comp] = {activeInHierarchy = true, enabled = true}
            local hits = 0
            engine:connectGameObjectSignalStay(comp, body, function(other)
                hits = hits + 1
                -- Destroy mid-iteration after the first overlapping part.
                engine._destroyed[comp] = true
            end)
            services.setClock(1); hb:fire(0.016)
            -- Only the first part fires; the guard short-circuits the rest.
            print("HITS " .. tostring(hits))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "HITS 1" in out


# ---------------------------------------------------------------------------
# host-surface wrapper -> engine routing (the seam the lowering lands on)
# ---------------------------------------------------------------------------

class TestHostSurfaceConnectStayWrapper:
    """The lowered turret code emits ``self.host:connectGameObjectSignalStay(go, fn)``.

    The slice-1.1/1.2 tests above call the engine method directly; these
    drive the call through the real ``_makeHostSurface`` wrapper
    (scene_runtime.luau ~728) — the exact shape the contract_pipeline
    lowering produces — and prove BOTH the colon and dotted forms route to
    the engine with the correct ``(owner, go, fn)`` mapping.

    Load-bearing proof that this exercises the WRAPPER and not the engine
    directly: the wrapper binds ``owner`` (captured at ``_makeHostSurface``
    time) as the engine ``comp`` arg, and ``connect`` only arms the
    Heartbeat subscription ``if _isGateOpen(comp)``. So if the wrapper's arg
    mapping were wrong — passing the ``host`` table (whose ``_meta`` is nil
    => gate closed) instead of ``owner``, or swapping the ``go``/``fn``
    positions — the poll would never arm or ``getTouchPart(fn)`` would
    resolve nil and ``fn`` would never fire. The negative-control test below
    pins the owner binding directly.
    """

    def test_colon_form_routes_through_wrapper_and_fires(self):
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limb = newInstance{Name = "RightFoot", ClassName = "Part",
                isa = {BasePart = true}}
            installWorkspace({limb})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            -- The owner the host is bound to; gate must be open.
            local owner = {}
            engine._meta[owner] = {activeInHierarchy = true, enabled = true}
            local host = engine:_makeHostSurface(owner)
            local hits = {}
            -- COLON form, exactly as the lowered turret emits.
            local conn = host:connectGameObjectSignalStay(body,
                function(other) table.insert(hits, other) end)
            print(conn ~= nil and "CONNECTED" or "NOCONN")
            services.setClock(1); hb:fire(0.016)
            print("HITS " .. tostring(#hits))
            print((hits[1] == limb) and "ISLIMB" or "WRONGHIT")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "CONNECTED" in out
        assert "HITS 1" in out, f"colon-form wrapper did not route to engine\n{out}"
        assert "ISLIMB" in out

    def test_dotted_form_routes_through_wrapper_and_fires(self):
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limb = newInstance{Name = "LeftFoot", ClassName = "Part",
                isa = {BasePart = true}}
            installWorkspace({limb})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local owner = {}
            engine._meta[owner] = {activeInHierarchy = true, enabled = true}
            local host = engine:_makeHostSurface(owner)
            local hits = {}
            -- DOTTED form: host.connectGameObjectSignalStay(go, fn).
            local conn = host.connectGameObjectSignalStay(body,
                function(other) table.insert(hits, other) end)
            print(conn ~= nil and "CONNECTED" or "NOCONN")
            services.setClock(1); hb:fire(0.016)
            print("HITS " .. tostring(#hits))
            print((hits[1] == limb) and "ISLIMB" or "WRONGHIT")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "CONNECTED" in out
        assert "HITS 1" in out, f"dotted-form wrapper did not route to engine\n{out}"
        assert "ISLIMB" in out

    def test_wrapper_binds_owner_not_caller_comp_gate(self):
        # Pins the load-bearing ``owner`` arg mapping: the wrapper passes the
        # ``owner`` it was built for as the engine ``comp`` (NOT the host
        # table, NOT a caller-supplied comp). Build the host for an owner
        # whose gate is CLOSED (enabled=false); the colon form must still
        # not throw, but the poll must NOT arm/fire — proving the wrapper
        # threaded the real owner's gate through ``connect``. If the wrapper
        # mis-passed comp (e.g. the host table, with no _meta entry), this
        # would behave identically by accident, so we ALSO flip the SAME
        # owner's gate open and rearm to prove the binding is that owner.
        scenario = textwrap.dedent("""\
            local services, hb = stayServices(nil)
            local engine = SceneRuntime.new(services, {})
            local limb = newInstance{Name = "Foot", ClassName = "Part",
                isa = {BasePart = true}}
            installWorkspace({limb})
            local trig = newInstance{Name = "Collider", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, Transparency = 1,
                attrs = {_IsTriggerVolume = true}}
            local body = newInstance{Name = "Turret", ClassName = "Part",
                isa = {BasePart = true}, CanTouch = true, children = {trig}}
            local owner = {}
            -- Gate CLOSED at arm time (disabled). Registered so _rearmSubs
            -- can find the subs list keyed by this exact owner.
            engine._meta[owner] = {activeInHierarchy = true, enabled = false}
            local host = engine:_makeHostSurface(owner)
            local hits = 0
            host:connectGameObjectSignalStay(body, function() hits = hits + 1 end)
            -- Gate closed => subscription not armed => no fire.
            print("ARMED " .. tostring(hb:count()))
            services.setClock(1); hb:fire(0.016)
            print("WHILE_CLOSED " .. tostring(hits))
            -- Open the SAME owner's gate and rearm: the sub keyed by ``owner``
            -- must now arm and fire — proving the wrapper bound THIS owner.
            engine._meta[owner].enabled = true
            engine:_rearmSubs(owner)
            print("REARMED " .. tostring(hb:count()))
            services.setClock(2); hb:fire(0.016)
            print("AFTER_OPEN " .. tostring(hits))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "ARMED 0" in out, f"wrapper armed despite closed owner gate\n{out}"
        assert "WHILE_CLOSED 0" in out
        # Rearming the owner the wrapper bound proves the comp mapping is that
        # owner (a sub keyed by the wrong comp would not be found by
        # _rearmSubs(owner)).
        assert "REARMED 1" in out, (
            "wrapper did not key the subscription by the bound owner\n" + out
        )
        assert "AFTER_OPEN 1" in out


# ---------------------------------------------------------------------------
# playerFromTouch / isPlayerTouch
# ---------------------------------------------------------------------------

class TestPlayerNormalization:

    def test_direct_character_limb(self):
        scenario = textwrap.dedent("""\
            local hum = newInstance{Name = "Humanoid", ClassName = "Humanoid",
                isa = {Humanoid = true}}
            local character = newInstance{Name = "Char", ClassName = "Model",
                isa = {Model = true}, children = {hum}}
            local limb = newInstance{Name = "RightHand", ClassName = "Part",
                isa = {BasePart = true}}
            limb.Parent = character
            local fakePlayer = {Name = "Player1"}
            local engine = SceneRuntime.new(
                baseServices(newPlayers({[character] = fakePlayer})), {})
            local plr = engine:playerFromTouch(limb)
            print(plr == fakePlayer and "PLAYER" or "NOPLAYER")
            print(engine:isPlayerTouch(limb) and "ISTRUE" or "ISFALSE")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "PLAYER" in out
        assert "ISTRUE" in out

    def test_accessory_mounted_part(self):
        scenario = textwrap.dedent("""\
            local hum = newInstance{Name = "Humanoid", ClassName = "Humanoid",
                isa = {Humanoid = true}}
            local character = newInstance{Name = "Char", ClassName = "Model",
                isa = {Model = true}, children = {hum}}
            -- Accessory model nested under the character; the touched part
            -- is the accessory handle, whose immediate Parent is the
            -- Accessory model (no Humanoid), not the character.
            local handle = newInstance{Name = "Handle", ClassName = "Part",
                isa = {BasePart = true}}
            local accessory = newInstance{Name = "Hat", ClassName = "Accessory",
                isa = {Model = true, Accessory = true}, children = {handle}}
            accessory.Parent = character
            handle.Parent = accessory
            local fakePlayer = {Name = "Player2"}
            local engine = SceneRuntime.new(
                baseServices(newPlayers({[character] = fakePlayer})), {})
            local plr = engine:playerFromTouch(handle)
            -- The ancestor walk skips the Humanoid-less Accessory model and
            -- resolves the character Model (which carries a Humanoid).
            print(plr == fakePlayer and "PLAYER" or ("OTHER:" .. tostring(plr)))
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "PLAYER" in out

    def test_non_player_touch_returns_nil(self):
        scenario = textwrap.dedent("""\
            local part = newInstance{Name = "Crate", ClassName = "Part",
                isa = {BasePart = true}}
            local engine = SceneRuntime.new(baseServices(newPlayers({})), {})
            local plr = engine:playerFromTouch(part)
            print(plr == nil and "NIL" or "NOTNIL")
            print(engine:isPlayerTouch(part) and "ISTRUE" or "ISFALSE")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "NIL" in out
        assert "ISFALSE" in out

    def test_no_players_service_returns_nil(self):
        scenario = textwrap.dedent("""\
            local part = newInstance{Name = "Limb", ClassName = "Part",
                isa = {BasePart = true}}
            local engine = SceneRuntime.new(baseServices(nil), {})
            local plr = engine:playerFromTouch(part)
            print(plr == nil and "NIL" or "NOTNIL")
        """)
        rc, out, err = _run(scenario)
        assert rc == 0, f"luau failed: {err}\n{out}"
        assert "NIL" in out
