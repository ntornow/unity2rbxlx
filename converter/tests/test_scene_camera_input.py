"""Tests for runtime/scene_camera_input.luau — the generic first-person
camera/input service (PR5).

Two layers:
  * Pure look-state logic (`_advance`) under the standalone luau interpreter —
    yaw is UNBOUNDED (the bug was yaw pinned to 0), pitch clamps, signs match
    Roblox's positive-down GetMouseDelta().Y. (`_composeLook` uses CFrame, a
    Roblox datatype absent from standalone luau, so it's verified in e2e.)
  * A scope-cap guard (pure Python) — the service's public API must NOT grow
    into locomotion/weapon territory ("owning locomotion = rebuilding Unity").
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

SERVICE_PATH = Path(__file__).parent.parent / "runtime" / "scene_camera_input.luau"


def _luau_available() -> bool:
    return shutil.which("luau") is not None


# ---------------------------------------------------------------------------
# Pure look-state logic under luau
# ---------------------------------------------------------------------------

def _load_preamble() -> str:
    """Mock just the load-time Roblox surface (the module only calls
    game:GetService at module scope) and expose the loaded service."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in src or f"[{delim}[" in src:
        delim += "="
    embedded = f"[{delim}[\n{src}\n]{delim}]"
    return textwrap.dedent(f"""\
        local function noop() end
        local _svc = setmetatable({{}}, {{__index = function() return noop end}})
        game = {{ GetService = function(_, _n) return _svc end }}
        workspace = {{ CurrentCamera = nil,
            GetAttribute = function() return nil end, SetAttribute = noop }}
        local SRC = {embedded}
        local chunk, err = loadstring(SRC, "scene_camera_input")
        assert(chunk, "load failed: " .. tostring(err))
        local SceneCameraInput = chunk()
    """)


def _run(scenario: str) -> tuple[int, str, str]:
    script = _load_preamble() + "\n" + scenario + "\n"
    with tempfile.NamedTemporaryFile(suffix=".luau", mode="w", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(["luau", path], capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout, r.stderr
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.skipif(
    not _luau_available() or not SERVICE_PATH.exists(),
    reason="needs standalone luau + service file",
)
class TestAdvanceLookState:
    def test_yaw_pitch_and_clamps(self) -> None:
        rc, out, err = _run(textwrap.dedent("""\
            local function approx(a, b) return math.abs(a - b) < 1e-6 end
            -- yaw subtracts dx*sensitivity; pitch unchanged when dy==0
            local y, p = SceneCameraInput._advance(0, 0, 100, 0, 0.01, -1, 1)
            assert(approx(y, -1), "yaw should be -1, got " .. tostring(y))
            assert(approx(p, 0), "pitch should be 0, got " .. tostring(p))
            -- THE FIX: yaw is UNBOUNDED — it accumulates past 0 (was pinned to 0)
            local y2 = SceneCameraInput._advance(-5, 0, 100, 0, 0.01, -10, 10)
            assert(approx(y2, -6), "yaw must accumulate, got " .. tostring(y2))
            -- pitch clamps to both bounds
            local _, pmin = SceneCameraInput._advance(0, 0, 0, 100000, 0.01, -1, 1)
            assert(approx(pmin, -1), "pitch clamps to min")
            local _, pmax = SceneCameraInput._advance(0, 0, 0, -100000, 0.01, -1, 1)
            assert(approx(pmax, 1), "pitch clamps to max")
            -- sign: positive dy (mouse down) lowers pitch (Roblox Y is +down)
            local _, pdown = SceneCameraInput._advance(0, 0, 0, 10, 0.01, -1, 1)
            assert(pdown < 0, "dy>0 must lower pitch")
            print("DONE")
        """))
        assert rc == 0, f"luau failed: {err or out}"
        assert "DONE" in out


# ---------------------------------------------------------------------------
# step() eye-source under luau (acceptance #9: followCharacter)
#
# step() uses CFrame/Vector3/Enum (Roblox datatypes absent from standalone
# luau), so we provide minimal math/engine shims and a configurable engine
# surface (workspace.CurrentCamera, Players.LocalPlayer.Character.HRP, the rig).
# The shims record PivotTo calls and the final camera eye so we can assert the
# eye source and that the character is NOT PivotTo'd.
# ---------------------------------------------------------------------------

_STEP_SHIMS = textwrap.dedent("""\
    -- loadstring chunks see GLOBALS, not the enclosing locals, so these
    -- math/engine shims must be global for the loaded service to use them.
    -- Minimal Vector3 (records component math the eye composition needs).
    Vector3 = {}
    Vector3.__index = Vector3
    function Vector3.new(x, y, z)
        return setmetatable({x = x or 0, y = y or 0, z = z or 0}, Vector3)
    end
    Vector3.__add = function(a, b)
        return Vector3.new(a.x + b.x, a.y + b.y, a.z + b.z)
    end
    Vector3.zero = Vector3.new(0, 0, 0)

    -- Minimal CFrame carrying only a Position (Vector3); rotation is opaque.
    CFrame = {}
    CFrame.__index = CFrame
    function CFrame.new(a, b, c)
        local pos
        if type(a) == "table" then pos = a
        elseif a ~= nil then pos = Vector3.new(a, b, c)
        else pos = Vector3.new(0, 0, 0) end
        return setmetatable({Position = pos}, CFrame)
    end
    function CFrame.Angles(_x, _y, _z)
        return setmetatable({Position = Vector3.new(0, 0, 0), _angles = true}, CFrame)
    end
    CFrame.__mul = function(a, _b)
        -- Position-preserving for our assertions (rotation is opaque here).
        return setmetatable({Position = a.Position}, CFrame)
    end
    function CFrame:ToEulerAnglesYXZ() return 0, 0, 0 end

    Enum = {
        CameraType = {Scriptable = "Scriptable", Custom = "Custom"},
        MouseBehavior = {LockCenter = "LockCenter"},
    }
""")


def _run_step(scenario: str) -> tuple[int, str, str]:
    """Load the service with full math/engine shims so step() can run, then
    execute `scenario` (which builds the configured engine surface)."""
    src = SERVICE_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in src or f"[{delim}[" in src:
        delim += "="
    embedded = f"[{delim}[\n{src}\n]{delim}]"
    preamble = textwrap.dedent(f"""\
        {_STEP_SHIMS}

        -- Configurable engine surface, mutated by the scenario before loading.
        local ENV = {{
            mouseDelta = {{X = 0, Y = 0}},
            isClient = false,           -- keep _ensureInit a no-op
            camera = {{CFrame = CFrame.new(0, 0, 0), CameraType = "Custom"}},
            localPlayer = nil,          -- Players.LocalPlayer
        }}
        _ENV = ENV

        local function noop() end
        local UserInputService = {{
            GetMouseDelta = function() return ENV.mouseDelta end,
        }}
        local RunService = {{ IsClient = function() return ENV.isClient end }}
        local Players = setmetatable({{}}, {{__index = function(_, k)
            if k == "LocalPlayer" then return ENV.localPlayer end
            return noop
        end}})
        local services = {{
            RunService = RunService,
            UserInputService = UserInputService,
            Players = Players,
        }}
        game = {{ GetService = function(_, n) return services[n] end }}
        workspace = setmetatable({{
            GetAttribute = function() return nil end,
            SetAttribute = noop,
        }}, {{__index = function(_, k)
            if k == "CurrentCamera" then return ENV.camera end
            return nil
        end}})

        local SRC = {embedded}
        local chunk, err = loadstring(SRC, "scene_camera_input")
        assert(chunk, "load failed: " .. tostring(err))
        SceneCameraInput = chunk()
    """)
    script = preamble + "\n" + scenario + "\n"
    with tempfile.NamedTemporaryFile(suffix=".luau", mode="w", delete=False) as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(["luau", path], capture_output=True, text=True, timeout=15)
        return r.returncode, r.stdout, r.stderr
    finally:
        Path(path).unlink(missing_ok=True)


@pytest.mark.skipif(
    not _luau_available() or not SERVICE_PATH.exists(),
    reason="needs standalone luau + service file",
)
class TestStepEyeSource:
    def test_follow_character_sets_eye_from_hrp_no_pivot(self) -> None:
        # followCharacter=true + a live HRP => eye = HRP.Position + (0, eyeH, 0),
        # and the character HRP is NEVER PivotTo'd (Humanoid owns its CFrame).
        rc, out, err = _run_step(textwrap.dedent("""\
            local function approx(a, b) return math.abs(a - b) < 1e-6 end
            local hrpPivoted = false
            local hrp = setmetatable({
                Position = _ENV.camera.CFrame.Position, -- placeholder, overwritten
                PivotTo = function() hrpPivoted = true end,
            }, {})
            -- give the HRP a known position
            local Vec = getmetatable(_ENV.camera.CFrame.Position)
            hrp.Position = setmetatable({x = 10, y = 5, z = -3}, Vec)
            local char = { HumanoidRootPart = hrp }
            function char:FindFirstChild(n)
                if n == "HumanoidRootPart" then return hrp end
                return nil
            end
            _ENV.localPlayer = { Character = char }

            local svc = SceneCameraInput.acquire()
            svc:configure({ eyeHeight = 1.5, followCharacter = true })
            svc:step(0)

            local eye = _ENV.camera.CFrame.Position
            assert(approx(eye.x, 10), "eye.x should be HRP.x, got " .. tostring(eye.x))
            assert(approx(eye.y, 6.5), "eye.y should be HRP.y + 1.5, got " .. tostring(eye.y))
            assert(approx(eye.z, -3), "eye.z should be HRP.z, got " .. tostring(eye.z))
            assert(not hrpPivoted, "character HRP must NOT be PivotTo'd")
            print("DONE")
        """))
        assert rc == 0, f"luau failed: {err or out}"
        assert "DONE" in out

    def test_followchar_unset_uses_rig_path_and_pivots_rig(self) -> None:
        # followCharacter unset => the existing rig-follow path: the rig IS
        # PivotTo'd (yaw) and the eye comes from the rig pivot + eyeHeight.
        rc, out, err = _run_step(textwrap.dedent("""\
            local function approx(a, b) return math.abs(a - b) < 1e-6 end
            local rigPivoted = false
            local Vec = getmetatable(_ENV.camera.CFrame.Position)
            local rigPos = setmetatable({x = 1, y = 2, z = 3}, Vec)
            local CF = getmetatable(_ENV.camera.CFrame)
            local rig = {
                CFrame = setmetatable({Position = rigPos}, CF),
                GetPivot = function(self) return setmetatable({Position = rigPos}, CF) end,
                PivotTo = function() rigPivoted = true end,
            }
            -- a character exists, but followCharacter is unset => ignored
            local hrp = { Position = setmetatable({x = 99, y = 99, z = 99}, Vec) }
            local char = {}
            function char:FindFirstChild(n)
                if n == "HumanoidRootPart" then return hrp end
            end
            _ENV.localPlayer = { Character = char }

            local svc = SceneCameraInput.acquire()
            svc:configure({ rig = rig, eyeHeight = 1.5 })  -- no followCharacter
            svc:step(0)

            assert(rigPivoted, "rig must be PivotTo'd in the base path")
            local eye = _ENV.camera.CFrame.Position
            assert(approx(eye.x, 1), "eye.x from rig pivot, got " .. tostring(eye.x))
            assert(approx(eye.y, 3.5), "eye.y = rig.y + 1.5, got " .. tostring(eye.y))
            assert(approx(eye.z, 3), "eye.z from rig pivot, got " .. tostring(eye.z))
            print("DONE")
        """))
        assert rc == 0, f"luau failed: {err or out}"
        assert "DONE" in out

    def test_followchar_set_but_no_character_falls_through_to_rig(self) -> None:
        # followCharacter=true but no character yet => _playerRoot() nil =>
        # fall through to the rig path with no error.
        rc, out, err = _run_step(textwrap.dedent("""\
            local function approx(a, b) return math.abs(a - b) < 1e-6 end
            local rigPivoted = false
            local Vec = getmetatable(_ENV.camera.CFrame.Position)
            local rigPos = setmetatable({x = 7, y = 8, z = 9}, Vec)
            local CF = getmetatable(_ENV.camera.CFrame)
            local rig = {
                CFrame = setmetatable({Position = rigPos}, CF),
                GetPivot = function(self) return setmetatable({Position = rigPos}, CF) end,
                PivotTo = function() rigPivoted = true end,
            }
            _ENV.localPlayer = { Character = nil }  -- no character spawned yet

            local svc = SceneCameraInput.acquire()
            svc:configure({ rig = rig, eyeHeight = 1.5, followCharacter = true })
            svc:step(0)

            assert(rigPivoted, "with no character, the rig path runs (no error)")
            local eye = _ENV.camera.CFrame.Position
            assert(approx(eye.y, 9.5), "eye.y = rig.y + 1.5, got " .. tostring(eye.y))
            print("DONE")
        """))
        assert rc == 0, f"luau failed: {err or out}"
        assert "DONE" in out

    def test_followcharacter_is_sticky(self) -> None:
        # Once set true, a later plain configure (no followCharacter key) must
        # NOT unset it — the character eye still wins.
        rc, out, err = _run_step(textwrap.dedent("""\
            local function approx(a, b) return math.abs(a - b) < 1e-6 end
            local Vec = getmetatable(_ENV.camera.CFrame.Position)
            local hrp = { Position = setmetatable({x = 0, y = 4, z = 0}, Vec) }
            local char = {}
            function char:FindFirstChild(n)
                if n == "HumanoidRootPart" then return hrp end
            end
            _ENV.localPlayer = { Character = char }

            local svc = SceneCameraInput.acquire()
            svc:configure({ followCharacter = true, eyeHeight = 1.5 })
            svc:configure({ sensitivity = 0.01 })  -- plain reconfigure, no flag
            svc:step(0)

            local eye = _ENV.camera.CFrame.Position
            assert(approx(eye.y, 5.5), "sticky followChar: eye.y = HRP.y+1.5, got " .. tostring(eye.y))
            print("DONE")
        """))
        assert rc == 0, f"luau failed: {err or out}"
        assert "DONE" in out


# ---------------------------------------------------------------------------
# Scope-cap guard (pure Python — no luau needed)
# ---------------------------------------------------------------------------

class TestScopeCap:
    # The capped public surface (everything else must be _-prefixed private).
    _ALLOWED_PUBLIC = {
        "acquire", "configure", "step", "applyRecoil",
        "getYawBasis", "getLookCFrame", "onRespawn",
    }
    # Words that would signal the service is absorbing locomotion/weapons.
    _FORBIDDEN_SUBSTRINGS = (
        "move", "walk", "jump", "strafe", "shoot", "fire",
        "weapon", "ammo", "reload", "damage", "velocity",
    )

    def test_public_api_within_cap(self) -> None:
        src = SERVICE_PATH.read_text(encoding="utf-8")
        # Public methods: ``function SceneCameraInput:Name(`` / ``.Name(``
        # whose name does not start with an underscore.
        methods = set(re.findall(
            r"function\s+SceneCameraInput[.:]([A-Za-z]\w*)\s*\(", src,
        ))
        public = {m for m in methods if not m.startswith("_")}
        extra = public - self._ALLOWED_PUBLIC
        assert not extra, (
            f"scene_camera_input grew public API beyond the scope cap: {sorted(extra)}. "
            "If intentional, update the cap AND the design-doc scope list."
        )

    def test_no_locomotion_or_weapon_methods(self) -> None:
        src = SERVICE_PATH.read_text(encoding="utf-8")
        names = re.findall(r"function\s+SceneCameraInput[.:](\w+)\s*\(", src)
        for n in names:
            low = n.lower()
            for bad in self._FORBIDDEN_SUBSTRINGS:
                assert bad not in low, (
                    f"method {n!r} suggests the service is crossing the scope cap "
                    f"(matched {bad!r}) — locomotion/weapons stay in the controller."
                )
