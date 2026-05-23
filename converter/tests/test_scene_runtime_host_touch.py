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
    inst._signals = {}
    inst.Parent = nil
    for _, c in ipairs(inst._children) do c.Parent = inst end

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
