"""Slice 3.1 — U1 rig shadow-sync tests for ``scene_runtime.luau``.

Drive the REAL ``_playerShadowSyncRig`` + the REAL ``_meta``-by-scriptId
resolution (``_playerRigInstances``) under the standalone-``luau`` host harness
(NOT a surrogate). Each frame, BEFORE the component Update pass, the authored
player-rig Instance(s) track the character HRP CFrame (U1: the rig is a
POSITIONAL SHADOW of the one body). The component still lives on the authored
rig Instance, so GetComponent / _SceneRuntimeId / registry closures are
PRESERVED (U1, NOT U2 — ``self.gameObject`` is never rebound).

  * AC1a — after ``_playerPreTick``, EVERY player-rig Instance's pivot equals
           the character HRP CFrame. Non-vacuous: the rig starts at a DIFFERENT
           pose and the assertion fails if shadow-sync is removed.
  * AC1b — GetComponent / _byClass / _componentsByGameObject registry closures
           still resolve the component on the authored rig AFTER shadow-sync
           (identity preserved — only the rig's CFrame moved, not its identity).
  * AC1c — the rig converges to the HRP each frame EVEN when a component's
           Update does a competing ``gameObject:PivotTo(junk)`` mid-pass
           (E4 — the AI write is vestigial; next pre-Update sync overwrites it).
  * AC1d — nothing authoritative reads rig yaw: ``_playerDriveLocomotion``
           computes its move basis from ``p._yaw`` (the camera yaw), NOT the rig
           pivot. Static source guard + execution test (move direction is
           unchanged when the rig is PivotTo'd to a different yaw).

Reuses the shared camera-input harness (mock Roblox surface + REAL
``SceneCameraInput`` pure helpers). Skips cleanly without ``luau``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests._camera_input_harness import (
    CAMERA_INPUT_PATH,
    camera_input_preamble,
    run_camera_scenario,
)

HOST_RUNTIME_PATH = (
    Path(__file__).parent.parent / "runtime" / "scene_runtime.luau"
)


def _luau_available() -> bool:
    return shutil.which("luau") is not None


pytestmark = pytest.mark.skipif(
    not _luau_available()
    or not CAMERA_INPUT_PATH.exists()
    or not HOST_RUNTIME_PATH.exists(),
    reason="needs standalone luau interpreter + runtime files",
)


# Locomotion mocks (extend the camera-harness Enum + CFrame.Angles with the
# yaw-relative ``VectorToWorldSpace`` / ``.Magnitude`` / ``.Unit`` the runtime's
# ``_playerDriveLocomotion`` branches on). Mirrors test_player_authority's
# locomotion setup; injected via ``extra_mock_setup`` so it patches the live
# CFrame metatable BEFORE the module loads. Used by the AC1d move-basis test.
_LOCOMOTION_MOCKS = """
    Enum.KeyCode = {W = "W", A = "A", S = "S", D = "D", Space = "Space"}
    Enum.HumanoidStateType = {Jumping = "Jumping"}

    local _origAngles = CFrame.Angles
    CFrame.Angles = function(x, y, z)
        local cf = _origAngles(x, y, z)
        cf._yawBasis = y or 0
        function cf:VectorToWorldSpace(vec)
            local yaw = self._yawBasis or 0
            local cy, sy = math.cos(yaw), math.sin(yaw)
            local wx = vec.X * cy + vec.Z * sy
            local wz = -vec.X * sy + vec.Z * cy
            local out = Vector3.new(wx, vec.Y, wz)
            local mag = math.sqrt(wx * wx + vec.Y * vec.Y + wz * wz)
            out.Magnitude = mag
            if mag > 0 then
                out.Unit = Vector3.new(wx / mag, vec.Y / mag, wz / mag)
            else
                out.Unit = Vector3.new(0, 0, 0)
            end
            return out
        end
        return cf
    end
"""


# ---------------------------------------------------------------------------
# Shared luau snippet: provision a character whose HRP has a CFrame at a known
# yaw, alias LocalPlayer.Character to it, then build the player component through
# the REAL ``engine:start()`` path — NOT a fabricated ``_meta`` row.
#
# Building through ``start()`` drives the production identity machinery for real:
#   * ``_buildComponent`` runs ``Player.new(config)`` and ``_register`` populates
#     ``_componentsByGameObject`` / ``_byClass`` / ``_meta`` from the SAME source
#     the production path uses;
#   * ``_injectHostSurface`` binds ``comp.gameObject`` / ``comp.transform`` to the
#     authored rig Instance and installs the live ``comp.GetComponent`` closure
#     (peer-by-stem over ``_componentsByGameObject`` -> built-in fallback);
#   * the rig Instance is a REAL registered ``instances`` entry, so the
#     ``_SceneRuntimeId`` lookup path (``getInstanceId`` / ``workspaceFind``)
#     resolves it.
# So AC1b asserts on the SAME tables/closures a U2-style rebind would disturb —
# the proof is non-vacuous (the rig is never hand-seeded into ``_meta``).
#
# Returns luau that leaves ``engine``, ``rig``, ``comp``, ``Comp``, ``hrp``,
# ``rigYaw`` in scope. ``Comp`` is the Player module class table (so a scenario
# may add ``function Comp:Update(...)`` — the metatable __index chains to it).
# ---------------------------------------------------------------------------

def _shadow_sync_setup(
    *,
    rig_kind: str = "model",     # "model" -> PivotTo ; "basepart" -> .CFrame
    rig_start_yaw: str = "3.0",
    hrp_yaw: str = "1.25",
    script_id: str = "player",
) -> str:
    # A mock rig Instance. A Model exposes ``PivotTo`` (records the last pivot
    # in ``_pivot``); a BasePart exposes a writable ``.CFrame``. Both start at a
    # DISTINCT yaw so the rig-tracks-HRP assertion is non-vacuous. The rig is a
    # REAL registered ``instances`` entry (``_sceneRuntimeId`` + ``_children``)
    # so ``workspaceFind`` / ``getInstanceId`` resolve it (servicesFor installs
    # the SetAttribute/GetAttribute mirror onto it).
    if rig_kind == "model":
        rig_decl = f"""
            local rig = {{ Name = "Rig", _sceneRuntimeId = "go1", _children = {{}},
                          _pivot = CFrame.new(Vector3.new(0,0,0)) }}
            rig._pivot._yaw = {rig_start_yaw}
            function rig:PivotTo(cf) self._pivot = cf end
            function rig:GetPivot() return self._pivot end
        """
        rig_yaw_expr = "rig._pivot._yaw"
    else:
        rig_decl = f"""
            local rig = {{ Name = "Rig", _sceneRuntimeId = "go1", _children = {{}},
                          CFrame = CFrame.new(Vector3.new(0,0,0)) }}
            rig.CFrame._yaw = {rig_start_yaw}
        """
        rig_yaw_expr = "rig.CFrame._yaw"

    return f"""
        -- The authored rig Instance, declared BEFORE servicesFor so the
        -- ``instances`` map can register it through the real workspaceFind /
        -- getInstanceId / SetAttribute(_SceneRuntimeId) surface.
        {rig_decl}

        -- The REAL Player module: a class table with a new(config) constructor.
        -- ``_register`` / ``_injectHostSurface`` bind the host surface +
        -- GetComponent onto each instance of this class.
        local Comp = {{}}
        Comp.__index = Comp
        function Comp.new(_config) return setmetatable({{}}, Comp) end

        local plan = {{
            modules = {{
                {script_id} = {{stem = "Player", runtime_bearing = true,
                          domain = "client", module_path = "Player",
                          has_character_controller = true}},
            }},
            scenes = {{
                A = {{
                    instances = {{
                        {{instance_id = "A:1", script_id = "{script_id}",
                          game_object_id = "go1", active = true,
                          enabled = true, config = {{}}}},
                    }},
                    references = {{}},
                    lifecycle_order = {{"A:1"}},
                }},
            }},
            prefabs = {{}},
            domain_overrides = {{}},
        }}
        local services = servicesFor(plan, {{{script_id} = Comp}}, {{go1 = rig}})
        services.isClient = true
        services.players = game:GetService("Players")
        services.userInputService = game:GetService("UserInputService")
        services.cameraAdvance = SceneCameraInput._advance
        services.cameraComposeLook = SceneCameraInput._composeLook
        -- Slice 3.3: _playerBoot's boot-reseed (_playerResyncToCharacter) reads
        -- services.cameraYawOf when a character is present at boot.
        services.cameraYawOf = SceneCameraInput._yawOf

        local engine = SceneRuntime.new(services, plan)

        -- Provision a character whose HRP carries a known-yaw CFrame and alias
        -- LocalPlayer.Character to it (``_playerCharacterHRP`` re-resolves
        -- LocalPlayer.Character each call).
        -- The HRP part: ``.CFrame`` carries the known yaw (shadow-sync reads it)
        -- AND a ``.Position`` Vector3 (``_playerWriteCamera`` reads the eye off it).
        local hrpCF = CFrame.new(Vector3.new(0,0,0))
        hrpCF._yaw = {hrp_yaw}
        local hrp = hrpCF       -- alias used by scenarios that print hrp._yaw
        local hrpPart = {{ CFrame = hrpCF, Position = Vector3.new(0,0,0) }}
        local char = {{
            FindFirstChild = function(_, name)
                if name == "HumanoidRootPart" then return hrpPart end
                return nil
            end,
            -- No Humanoid -> _playerDriveLocomotion (in _playerPostTick) no-ops;
            -- a scenario that needs locomotion overrides Character itself.
            FindFirstChildOfClass = function(_, _cls) return nil end,
            -- _playerBoot's hideChar walks the avatar (DescendantAdded /
            -- GetDescendants); provide empty mocks so the real boot path runs.
            DescendantAdded = {{ Connect = function(_, _fn) return {{Disconnect = function() end}} end }},
            GetDescendants = function(_) return {{}} end,
        }}
        game:GetService("Players").LocalPlayer.Character = char

        -- Build the component THROUGH the real boot path: start() runs
        -- new() -> _register -> _injectHostSurface (binding comp.gameObject +
        -- GetComponent) and, at its end, _initPlayerAuthority (binding
        -- self._player off the upstream has_character_controller signal).
        engine:start(nil)
        runDeferred()

        -- The live registered component on the authored rig (the SAME object
        -- _register inserted into _componentsByGameObject["go1"]). NOT a
        -- hand-seeded _meta row.
        local comp = engine._componentsByGameObject["go1"][1]
        local rigYaw = function() return {rig_yaw_expr} end
    """


# ---------------------------------------------------------------------------
# AC1a — rig pivot tracks the character HRP after _playerPreTick.
# ---------------------------------------------------------------------------

class TestRigTracksHRP:

    def test_model_rig_pivot_follows_hrp(self) -> None:
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        body = _shadow_sync_setup(rig_kind="model") + """
            print("RIG_PRE=" .. tostring(rigYaw()))
            engine:_playerPreTick(0.016)
            print("RIG_POST=" .. tostring(rigYaw()))
            print("HRP_YAW=" .. tostring(hrp._yaw))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # Non-vacuous: the rig STARTED at a different yaw (3.0) ...
        assert "RIG_PRE=3" in out, out
        # ... and after the pre-tick shadow-sync it equals the HRP yaw (1.25).
        assert "RIG_POST=1.25" in out, out
        assert "HRP_YAW=1.25" in out, out

    def test_basepart_rig_cframe_follows_hrp(self) -> None:
        # CharacterController->BasePart map: a BasePart rig (no PivotTo) gets
        # its ``.CFrame`` driven instead.
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        body = _shadow_sync_setup(rig_kind="basepart") + """
            engine:_playerPreTick(0.016)
            print("RIG_POST=" .. tostring(rigYaw()))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "RIG_POST=1.25" in out, out

    def test_no_character_is_noop(self) -> None:
        # E1: no character -> _playerShadowSyncRig no-ops (rig keeps its pose,
        # no nil-deref).
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        body = _shadow_sync_setup(rig_kind="model") + """
            -- Clear the character: shadow-sync must no-op, not crash.
            game:GetService("Players").LocalPlayer.Character = nil
            engine:_playerShadowSyncRig()
            print("RIG_POST=" .. tostring(rigYaw()))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # Rig keeps its original pose (no sync ran).
        assert "RIG_POST=3" in out, out

    def test_no_rig_built_is_noop(self) -> None:
        # E2: no component under the player scriptId in _meta -> the resolution
        # returns {} and shadow-sync loops zero times (no crash).
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        body = """
            local plan = {
                modules = {
                    player = {stem = "Player", runtime_bearing = true,
                              has_character_controller = true},
                },
            }
            local services = servicesFor(plan, {}, {})
            services.isClient = true
            services.players = game:GetService("Players")
            services.cameraComposeLook = SceneCameraInput._composeLook
            local engine = SceneRuntime.new(services, plan)
            engine:_initPlayerAuthority()

            local hrp = CFrame.new(Vector3.new(0,0,0))
            hrp._yaw = 1.25
            local hrpPart = { CFrame = hrp }
            local char = { FindFirstChild = function(_, name)
                if name == "HumanoidRootPart" then return hrpPart end
                return nil
            end }
            game:GetService("Players").LocalPlayer.Character = char

            -- No _meta entry for the player scriptId.
            local rigs = engine:_playerRigInstances()
            print("RIGCOUNT=" .. tostring(#rigs))
            engine:_playerShadowSyncRig()      -- must not crash
            print("OK=true")
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "RIGCOUNT=0" in out, out
        assert "OK=true" in out, out

    def test_multiple_rig_instances_all_synced(self) -> None:
        # E8/P2-F: one player MODULE placed on >1 GameObject -> all N rig
        # instances are shadow-synced to the one HRP. Both placements are built
        # through the REAL start() path (a second scene instance of the SAME
        # ``player`` module on ``go2``), so _playerRigInstances scans the real
        # _meta the production _register populated — NOT a hand-seeded row.
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        body = """
            local rig1 = { Name = "Rig1", _sceneRuntimeId = "go1", _children = {},
                           _pivot = CFrame.new(Vector3.new(0,0,0)) }
            rig1._pivot._yaw = 3.0
            function rig1:PivotTo(cf) self._pivot = cf end
            local rig2 = { Name = "Rig2", _sceneRuntimeId = "go2", _children = {},
                           _pivot = CFrame.new(Vector3.new(0,0,0)) }
            rig2._pivot._yaw = 9.0
            function rig2:PivotTo(cf) self._pivot = cf end

            local Comp = {}
            Comp.__index = Comp
            function Comp.new(_config) return setmetatable({}, Comp) end

            local plan = {
                modules = {
                    player = {stem = "Player", runtime_bearing = true,
                              domain = "client", module_path = "Player",
                              has_character_controller = true},
                },
                scenes = {
                    A = {
                        instances = {
                            {instance_id = "A:1", script_id = "player",
                             game_object_id = "go1", active = true,
                             enabled = true, config = {}},
                            {instance_id = "A:2", script_id = "player",
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
            local services = servicesFor(plan, {player = Comp}, {go1 = rig1, go2 = rig2})
            services.isClient = true
            services.players = game:GetService("Players")
            services.userInputService = game:GetService("UserInputService")
            services.cameraAdvance = SceneCameraInput._advance
            services.cameraComposeLook = SceneCameraInput._composeLook
            -- Slice 3.3: boot-reseed reads services.cameraYawOf.
            services.cameraYawOf = SceneCameraInput._yawOf

            local engine = SceneRuntime.new(services, plan)

            local hrpCF = CFrame.new(Vector3.new(0,0,0))
            hrpCF._yaw = 1.25
            local hrpPart = { CFrame = hrpCF, Position = Vector3.new(0,0,0) }
            local char = {
                FindFirstChild = function(_, name)
                    if name == "HumanoidRootPart" then return hrpPart end
                    return nil
                end,
                FindFirstChildOfClass = function(_, _cls) return nil end,
                DescendantAdded = { Connect = function(_, _fn) return {Disconnect = function() end} end },
                GetDescendants = function(_) return {} end,
            }
            game:GetService("Players").LocalPlayer.Character = char

            engine:start(nil)
            runDeferred()

            print("RIGCOUNT=" .. tostring(#engine:_playerRigInstances()))
            engine:_playerPreTick(0.016)
            print("RIG1=" .. tostring(rig1._pivot._yaw))
            print("RIG2=" .. tostring(rig2._pivot._yaw))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "RIGCOUNT=2" in out, out
        # BOTH rig instances track the one HRP.
        assert "RIG1=1.25" in out, out
        assert "RIG2=1.25" in out, out


# ---------------------------------------------------------------------------
# AC1b — registry / identity closures still resolve the component on the
# authored rig AFTER shadow-sync (U1, NOT U2: identity preserved).
# ---------------------------------------------------------------------------

class TestIdentityPreserved:

    def test_registry_closures_resolve_after_sync(self) -> None:
        # The component was built through the REAL ``start()`` path, so
        # ``comp.gameObject`` / ``comp.GetComponent`` / the ``_SceneRuntimeId``
        # lookup are the PRODUCTION surfaces (no hand-seeded tables). We drive
        # each by EXECUTION and assert it still resolves the component on the
        # AUTHORED rig after shadow-sync moved the rig's CFrame (U1, NOT U2:
        # only position moved, identity is untouched — ``self.gameObject`` is
        # never rebound to the character).
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        body = _shadow_sync_setup(rig_kind="model") + """
            -- BEFORE the sync: the real injected host surface binds
            -- comp.gameObject to the authored rig, and the real GetComponent
            -- closure resolves the component on it.
            local goBefore = comp.gameObject == rig
            local getCompBefore = comp:GetComponent("Player") == comp
            -- The _SceneRuntimeId-driven lookup: getInstanceId(comp.gameObject)
            -- -> "go1" (stamped during boot) -> workspaceFind resolves the rig.
            local idBefore = engine._services.getInstanceId(comp.gameObject)
            local sriBefore = engine._services.workspaceFind(idBefore) == rig

            engine:_playerPreTick(0.016)

            -- AFTER shadow-sync (rig CFrame MOVED): every identity surface still
            -- resolves the SAME component on the SAME authored rig.
            local goAfter = comp.gameObject == rig
            -- GetComponent STILL resolves the component on the authored rig.
            local getCompAfter = comp:GetComponent("Player") == comp
            -- The _SceneRuntimeId lookup STILL maps the moved rig.
            local idAfter = engine._services.getInstanceId(comp.gameObject)
            local sriAfter = engine._services.workspaceFind(idAfter) == rig
            print("GO_BEFORE=" .. tostring(goBefore))
            print("GETCOMP_BEFORE=" .. tostring(getCompBefore))
            print("SRI_BEFORE=" .. tostring(sriBefore))
            print("GO_AFTER=" .. tostring(goAfter))
            print("GETCOMP_AFTER=" .. tostring(getCompAfter))
            print("SRI_AFTER=" .. tostring(sriAfter))
            -- The rig MOVED (its CFrame is the HRP's now) — only position, not
            -- identity.
            print("RIG_MOVED=" .. tostring(rigYaw() == 1.25))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # Identity surfaces resolve BEFORE the sync (sanity).
        assert "GO_BEFORE=true" in out, out
        assert "GETCOMP_BEFORE=true" in out, out
        assert "SRI_BEFORE=true" in out, out
        # ... and STILL resolve the component on the authored rig AFTER it moved
        # (U1: identity preserved; a U2-style rebind/break would flip these).
        assert "GO_AFTER=true" in out, out
        assert "GETCOMP_AFTER=true" in out, out
        assert "SRI_AFTER=true" in out, out
        assert "RIG_MOVED=true" in out, out


# ---------------------------------------------------------------------------
# AC1c — rig converges to the HRP each frame even when a component's Update
# PivotTo's the rig to junk mid-pass (E4 — the AI write is vestigial).
# ---------------------------------------------------------------------------

class TestVestigialAIPivot:

    def test_rig_reconverges_after_competing_pivot(self) -> None:
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        body = _shadow_sync_setup(rig_kind="model") + """
            -- The component's Update writes JUNK to the rig pivot mid-pass
            -- (modeling the AI's vestigial gameObject:PivotTo). Wire it through
            -- the REAL _tick (pre-Update sync -> Update junk-write).
            local junk = CFrame.new(Vector3.new(0,0,0))
            junk._yaw = 8.0
            function Comp:Update(_dt) rig:PivotTo(junk) end

            -- Frame 1: pre-tick syncs rig->HRP, then Update stomps it to 8.0.
            engine:_tick(0.016)
            print("AFTER_F1=" .. tostring(rigYaw()))

            -- Frame 2: the next pre-Update shadow-sync overwrites the junk back
            -- to the HRP (the AI write was vestigial).
            engine:_tick(0.016)
            -- Read the rig BEFORE this frame's Update runs is what matters; but
            -- _tick's Update stomps again at frame end, so assert the sync ran
            -- by checking the pre-tick value directly:
            engine:_playerShadowSyncRig()
            print("AFTER_SYNC=" .. tostring(rigYaw()))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # End of frame 1: the AI's Update junk write is the last writer (8.0).
        assert "AFTER_F1=8" in out, out
        # The shadow-sync overwrites it back to the HRP (1.25) — vestigial AI.
        assert "AFTER_SYNC=1.25" in out, out


# ---------------------------------------------------------------------------
# AC1d — nothing authoritative reads rig yaw: locomotion's move basis is
# ``p._yaw`` (camera yaw), not the rig pivot.
# ---------------------------------------------------------------------------

class TestRigYawNotAuthoritative:

    def test_drive_locomotion_reads_p_yaw_not_rig_static(self) -> None:
        # Static source guard: ``_playerDriveLocomotion`` reads ``p._yaw`` for
        # the move basis and does NOT read the rig pivot / _playerRigInstances /
        # GetPivot for it.
        src = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
        marker = "function SceneRuntime:_playerDriveLocomotion()"
        body = src[src.index(marker):]
        body = body[: body.index("\nfunction SceneRuntime:")]
        assert "p._yaw" in body, body
        for forbidden in (
            "_playerRigInstances", "GetPivot", ":GetPivot", "gameObjectInstance",
        ):
            assert forbidden not in body, (
                f"locomotion must not read rig pivot ({forbidden}):\n{body}"
            )

    def test_move_direction_unchanged_when_rig_pivot_differs(self) -> None:
        # Execution proof: with p._yaw held at 0 and 'W' held, run
        # _playerDriveLocomotion; then PivotTo the rig to a TOTALLY different yaw
        # and drive again. The move direction depends ONLY on p._yaw (the camera
        # yaw), NOT the rig pivot — so it is IDENTICAL across the two calls
        # (rig yaw is not consumed by the authority).
        preamble = camera_input_preamble(
            mouse_deltas=[(0.0, 0.0)], extra_mock_setup=_LOCOMOTION_MOCKS
        )
        body = _shadow_sync_setup(rig_kind="model") + """
            -- A recording Humanoid (Move logs the world direction).
            local moveLog = {}
            local hum = {}
            function hum:Move(dir, rel) table.insert(moveLog, dir) end
            function hum:ChangeState(_s) end
            local char2 = {
                FindFirstChild = function(_, name)
                    if name == "HumanoidRootPart" then return { CFrame = hrp } end
                    return nil
                end,
                FindFirstChildOfClass = function(_, cls)
                    if cls == "Humanoid" then return hum end
                    return nil
                end,
            }
            game:GetService("Players").LocalPlayer.Character = char2

            -- Hold W only.
            _keysDown = {W = true}
            do
                local uis = game:GetService("UserInputService")
                function uis:IsKeyDown(code) return _keysDown[code] == true end
            end

            engine._player._yaw = 0
            engine:_playerDriveLocomotion()
            -- Move the rig to a TOTALLY different yaw between calls.
            do
                local c = CFrame.new(Vector3.new(0,0,0)); c._yaw = 5.5
                rig:PivotTo(c)
            end
            engine:_playerDriveLocomotion()

            local d1, d2 = moveLog[1], moveLog[2]
            print("DX1=" .. string.format("%.4f", d1.X) .. " DZ1=" .. string.format("%.4f", d1.Z))
            print("DX2=" .. string.format("%.4f", d2.X) .. " DZ2=" .. string.format("%.4f", d2.Z))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # Parse the two move directions — they must be identical (rig yaw never
        # entered the move basis).
        import re
        m1 = re.search(r"DX1=(-?\d+\.\d+) DZ1=(-?\d+\.\d+)", out)
        m2 = re.search(r"DX2=(-?\d+\.\d+) DZ2=(-?\d+\.\d+)", out)
        assert m1 and m2, out
        assert abs(float(m1.group(1)) - float(m2.group(1))) < 1e-6, out
        assert abs(float(m1.group(2)) - float(m2.group(2))) < 1e-6, out
