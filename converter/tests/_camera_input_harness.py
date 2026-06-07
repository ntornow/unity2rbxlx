"""Reusable standalone-``luau`` harness for ``runtime/scene_camera_input.luau``.

NON-collected sibling to ``test_scene_runtime_host_behavior.py`` (the leading
underscore keeps pytest from collecting it as a test module — see design
Phase-1 §Interfaces item 2 / edge case E6). The camera follow-MATH units
(``test_camera_follow_math.py``) import this rig so they share ONE
camera-input loader and ONE mock surface.

``scene_camera_input.luau`` resolves ``game:GetService("RunService" /
"UserInputService" / "Players")`` at module-load time (lines 30-32), so the
mock ``game`` / ``workspace`` / ``Vector2`` / ``Vector3`` / ``CFrame`` / ``Enum``
globals MUST be installed in the chunk BEFORE the embedded source is run via
``loadstring`` — mirroring the existing ``_harness_preamble`` embed+load trick.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

# Reuse the proven subprocess+tempfile runner from the host-behavior module.
# Importing the symbol is cheap (the module's top level only defines the
# preamble builders + test classes; pytest collection is what's expensive, and
# a plain ``import`` does not collect). Per design DP2 / item 2, prefer this
# over copying the ~12-line runner.
from tests.test_scene_runtime_host_behavior import _run_scenario

CAMERA_INPUT_PATH = (
    Path(__file__).parent.parent / "runtime" / "scene_camera_input.luau"
)


def _camera_service_loader() -> str:
    """Return a luau snippet that embeds ``scene_camera_input.luau`` and loads
    it via ``loadstring``, exposing the result as a top-level ``SceneCameraInput``
    local — WITHOUT installing any Roblox-API globals of its own.

    Binds against whatever ``game`` / ``workspace`` / ``CFrame`` / ``Vector3`` /
    ``Enum`` globals are live at the point the snippet runs (the module captures
    ``RunService`` / ``UserInputService`` / ``Players`` from ``game:GetService``
    at load time — lines 30-32). ``camera_input_preamble`` installs its own
    surface first, then loads the REAL service through this one snippet — there
    is a single camera-service loader.
    """
    source = CAMERA_INPUT_PATH.read_text(encoding="utf-8")
    delim = "==="
    while f"]{delim}]" in source or f"[{delim}[" in source:
        delim += "="
    embedded = f"[{delim}[\n{source}\n]{delim}]"
    return textwrap.dedent(f"""\
        -- Load the REAL scene_camera_input.luau under the ambient mocks.
        local CAMERA_INPUT_SOURCE = {embedded}
        local SceneCameraInput
        do
            local chunk, err = loadstring(CAMERA_INPUT_SOURCE, "scene_camera_input")
            assert(chunk, "load scene_camera_input failed: " .. tostring(err))
            SceneCameraInput = chunk()
        end
    """)


def camera_input_preamble(*, mouse_deltas: list[tuple[float, float]]) -> str:
    """Return a luau chunk that loads ``scene_camera_input.luau`` under mocks.

    The returned chunk:
      (a) embeds ``scene_camera_input.luau`` as a long-string and loads it via
          ``loadstring`` (same delimiter trick as ``_harness_preamble``);
      (b) installs a mock ``game`` / ``game:GetService`` returning mock
          ``RunService`` (``IsClient() -> true``), ``UserInputService``
          (``GetMouseDelta()`` pops successive entries from ``mouse_deltas`` as
          a mock ``Vector2`` with ``.X`` / ``.Y``, then ``Vector2(0, 0)`` once
          exhausted), and ``Players`` (``LocalPlayer``);
      (c) installs a mock ``workspace`` whose ``CurrentCamera`` is a table with a
          writable ``.CFrame``, plus mock ``Vector2`` / ``Vector3`` / ``CFrame``
          / ``Enum`` sufficient for the module to load and for ``_readDelta`` /
          ``_composeLook`` / ``_advance`` / ``step`` to run;
      (d) supports the E2E attribute channel — ``GetAttribute`` / ``SetAttribute``
          on ``workspace`` for ``E2EMouseSeq`` / ``E2EMouseDeltaX`` /
          ``E2EMouseDeltaY`` / ``E2EMouseAckSeq``.

    After the preamble, a scenario body sees ``SceneCameraInput`` as a top-level
    local and can call ``SceneCameraInput._readDelta`` /
    ``SceneCameraInput._composeLook`` / ``SceneCameraInput._advance`` /
    ``SceneCameraInput:step``. ``workspace`` and ``UserInputService`` are also
    in scope so a scenario can drive the E2E channel / inspect camera writes.
    """
    # Lua array literal of {x, y} pairs for the queued mouse deltas.
    deltas_literal = "{" + ", ".join(
        f"{{{float(x)}, {float(y)}}}" for (x, y) in mouse_deltas
    ) + "}"

    mocks = textwrap.dedent(f"""\
        -- ===================================================================
        -- Camera-input harness preamble: install the slice of the Roblox API
        -- that scene_camera_input.luau touches as chunk-level globals BEFORE
        -- the module source runs (it calls game:GetService at load time).
        -- ===================================================================

        -- Vector2 / Vector3: plain field-bag tables with arithmetic enough for
        -- the eye-position math (Vector3 add in :step).
        local Vector2mt = {{}}
        Vector2mt.__index = Vector2mt
        local function Vector2new(x, y)
            return setmetatable({{X = x or 0, Y = y or 0}}, Vector2mt)
        end

        local Vector3mt = {{}}
        Vector3mt.__index = Vector3mt
        function Vector3mt.__add(a, b)
            return setmetatable(
                {{X = a.X + b.X, Y = a.Y + b.Y, Z = a.Z + b.Z}}, Vector3mt)
        end
        local function Vector3new(x, y, z)
            return setmetatable({{X = x or 0, Y = y or 0, Z = z or 0}}, Vector3mt)
        end
        -- ``.new`` + ``.zero`` are the only members scene_camera_input.luau
        -- reads (``Vector3.zero`` at line 230).
        Vector3 = {{new = Vector3new, zero = Vector3new(0, 0, 0)}}
        Vector2 = {{new = Vector2new}}

        -- CFrame: carry a Position + the yaw/pitch we composed so a scenario can
        -- assert on the look. ``*`` accumulates rotation; ``.new(pos)`` seeds
        -- position; ``.Angles(x, y, z)`` carries pitch (x) / yaw (y).
        local CFramemt = {{}}
        CFramemt.__index = CFramemt
        function CFramemt:ToEulerAnglesYXZ()
            return self._pitch or 0, self._yaw or 0, 0
        end
        function CFramemt.__mul(a, b)
            return setmetatable({{
                Position = a.Position or Vector3new(0, 0, 0),
                _yaw = (a._yaw or 0) + (b._yaw or 0),
                _pitch = (a._pitch or 0) + (b._pitch or 0),
            }}, CFramemt)
        end
        CFrame = {{
            new = function(pos)
                return setmetatable(
                    {{Position = pos or Vector3new(0, 0, 0),
                      _yaw = 0, _pitch = 0}}, CFramemt)
            end,
            Angles = function(x, y, z)
                return setmetatable(
                    {{Position = Vector3new(0, 0, 0),
                      _pitch = x or 0, _yaw = y or 0}}, CFramemt)
            end,
        }}

        -- Enum: only the members scene_camera_input.luau reads.
        Enum = {{
            CameraType = {{Scriptable = "Scriptable", Custom = "Custom"}},
            MouseBehavior = {{LockCenter = "LockCenter", Default = "Default"}},
        }}

        -- UserInputService: drainable GetMouseDelta + writable mouse state.
        local _mouseDeltas = {deltas_literal}
        local _mouseDeltaIdx = 0
        local UserInputService = {{
            MouseBehavior = nil,
            MouseIconEnabled = true,
        }}
        function UserInputService:GetMouseDelta()
            _mouseDeltaIdx = _mouseDeltaIdx + 1
            local entry = _mouseDeltas[_mouseDeltaIdx]
            if entry then return Vector2new(entry[1], entry[2]) end
            return Vector2new(0, 0)
        end

        -- RunService: a client.
        local RunService = {{}}
        function RunService:IsClient() return true end

        -- Players + LocalPlayer (no Character by default; a scenario can set one).
        local LocalPlayer = {{
            Character = nil,
            FindFirstChild = function(_, _name) return nil end,
        }}
        local _charAddedSig = {{Connect = function(_, _fn) return {{Disconnect = function() end}} end}}
        LocalPlayer.CharacterAdded = _charAddedSig
        local Players = {{LocalPlayer = LocalPlayer}}

        -- game:GetService dispatch.
        local _services = {{
            RunService = RunService,
            UserInputService = UserInputService,
            Players = Players,
        }}
        game = {{}}
        function game:GetService(name) return _services[name] end

        -- workspace: CurrentCamera with a writable .CFrame + the E2E attribute
        -- channel (GetAttribute / SetAttribute).
        local _attrs = {{}}
        local CurrentCamera = {{
            CameraType = nil,
            CFrame = CFrame.new(Vector3new(0, 0, 0)),
        }}
        workspace = {{CurrentCamera = CurrentCamera}}
        function workspace:GetAttribute(name) return _attrs[name] end
        function workspace:SetAttribute(name, value) _attrs[name] = value end

        -- task: scene_camera_input.luau only touches task.wait via CharacterAdded
        -- (never reached in headless scenarios), but define it so a future
        -- scenario that drives :_ensureInit's CharacterAdded path doesn't nil-index.
        if task == nil then
            task = {{wait = function(_secs) return 0 end}}
        end
    """)
    # Load the production module under the mocks above, via the shared loader.
    return mocks + "\n" + _camera_service_loader()


def run_camera_scenario(preamble: str, body: str) -> tuple[int, str, str]:
    """Stitch ``preamble`` + ``body`` and run under standalone ``luau``.

    Delegates to ``_run_scenario`` — BUT ``_run_scenario`` prepends the
    host-runtime ``_harness_preamble`` (which loads ``scene_runtime.luau`` and
    defines ``task``/etc.). The camera preamble must follow it so the camera
    mocks (``game``/``workspace``/``CFrame``/...) shadow nothing it needs and so
    ``task`` exists. We therefore pass the camera preamble + body as the
    ``_run_scenario`` body: the host preamble runs first (defining ``task``),
    then our camera preamble installs the camera mocks and loads the module,
    then the scenario body runs.
    """
    return _run_scenario(preamble + "\n" + body + "\n")

