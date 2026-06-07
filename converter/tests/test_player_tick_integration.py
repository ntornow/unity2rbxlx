"""Slice 2.3 — integration tests driving the REAL ``_tick`` / ``_initPlayerAuthority``
host-owned player-embodiment authority wiring in ``scene_runtime.luau``.

Where ``test_player_authority.py`` (Slices 2.1/2.2) attaches ``self._player``
DIRECTLY and calls the methods in isolation, THESE tests drive the real
integration surface Slice 2.3 builds:

  * AC1  — ``_initPlayerAuthority`` selects the player from the upstream
           ``has_character_controller`` MODULE-ROW signal over
           ``self._plan.modules`` (fail-closed: 0 -> nil, 1 -> bind, >1 ->
           warn+nil), counting distinct MODULE rows NOT ``_meta`` instances
           (one CC module placed as TWO instances is ONE row -> binds), keyed
           ONLY on the deterministic upstream signal (no AI-output matcher).
  * AC2  — the two ``_tick`` brackets bracket the component ``pairs()`` loop:
           ``_playerWriteCamera`` runs BEFORE the Update pass (a mid-Update
           component reads C's pre-write pose, non-vacuous) AND AFTER LateUpdate
           (the final camera is C's post-write); twice-call idempotency (E9).
  * AC9  — ``isClient = false`` -> ``_initPlayerAuthority`` leaves
           ``self._player == nil`` and ``_tick`` drives components but builds /
           drives NO camera authority (the brackets no-op). Gate is the explicit
           boolean, NOT a RunService presence sniff.
  * AC12 — ``host.player:getLookCFrame()`` is reachable AND narrow:
           ``host.player._services`` / ``._player`` / ``.start`` are all nil.

The mock Roblox surface + the REAL ``SceneCameraInput`` pure helpers come from
the shared camera-input harness (reuse-not-rebuild). Skips cleanly without luau.
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


# ---------------------------------------------------------------------------
# Shared snippet: build a SceneRuntime over a given plan with the injected pure
# camera helpers + UIS + an explicit ``isClient`` flag, then call the REAL
# ``_initPlayerAuthority``. ``plan_literal`` is a Lua table literal for the
# plan; ``is_client`` controls the gate.
# ---------------------------------------------------------------------------

def _init_authority_body(*, plan_literal: str, is_client: str = "true") -> str:
    return f"""
        local plan = {plan_literal}
        local services = servicesFor(plan, {{}}, {{}})
        services.isClient = {is_client}
        services.userInputService = game:GetService("UserInputService")
        services.cameraAdvance = SceneCameraInput._advance
        services.cameraComposeLook = SceneCameraInput._composeLook

        local engine = SceneRuntime.new(services, plan)
        engine:_initPlayerAuthority()
    """


# ---------------------------------------------------------------------------
# AC1 — module-row identity selection, fail-closed, upstream signal only.
# ---------------------------------------------------------------------------

class TestInitPlayerAuthorityIdentity:

    def test_one_cc_module_binds(self) -> None:
        preamble = camera_input_preamble(mouse_deltas=[])
        body = _init_authority_body(plan_literal="""{
            modules = {
                player = {stem = "Player", runtime_bearing = true,
                          has_character_controller = true},
                world = {stem = "World", runtime_bearing = true},
            },
        }""") + """
            print("PLAYER=" .. tostring(engine._player ~= nil))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "PLAYER=true" in out, out

    def test_zero_cc_modules_no_player(self) -> None:
        preamble = camera_input_preamble(mouse_deltas=[])
        body = _init_authority_body(plan_literal="""{
            modules = {
                world = {stem = "World", runtime_bearing = true},
            },
        }""") + """
            print("PLAYER=" .. tostring(engine._player ~= nil))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "PLAYER=false" in out, out

    def test_multiple_cc_modules_warn_and_no_player(self) -> None:
        preamble = camera_input_preamble(mouse_deltas=[])
        body = _init_authority_body(plan_literal="""{
            modules = {
                p1 = {stem = "P1", has_character_controller = true},
                p2 = {stem = "P2", has_character_controller = true},
            },
        }""") + """
            print("PLAYER=" .. tostring(engine._player ~= nil))
            print("WARNS=" .. tostring(#logs))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "PLAYER=false" in out, out
        # >1 CC module rows must warn (ambiguous) — fail-closed, not silent.
        assert "WARNS=1" in out, out

    def test_one_module_two_instances_is_not_ambiguous(self) -> None:
        # P1-5/P2-F: identity counts distinct MODULE rows, NOT placed
        # instances. ONE CC module placed twice is ONE row -> binds (a
        # per-instance ``_meta`` scan would false-ambiguate to >1 here).
        preamble = camera_input_preamble(mouse_deltas=[])
        body = _init_authority_body(plan_literal="""{
            modules = {
                player = {stem = "Player", runtime_bearing = true,
                          has_character_controller = true},
            },
            scenes = {
                A = {
                    instances = {
                        {instance_id = "A:1", script_id = "player",
                         game_object_id = "go1"},
                        {instance_id = "A:2", script_id = "player",
                         game_object_id = "go2"},
                    },
                },
            },
        }""") + """
            print("PLAYER=" .. tostring(engine._player ~= nil))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "PLAYER=true" in out, out

    def test_identity_reads_no_transpiled_source_string(self) -> None:
        # §3 guardrail: the selection keys ONLY on ``row.has_character_controller``
        # — never an AI-output substring fingerprint. Pin it statically: the
        # ``_initPlayerAuthority`` body references the upstream field and does
        # NOT match transpiled source by name/substring.
        src = HOST_RUNTIME_PATH.read_text(encoding="utf-8")
        init = src[src.index("function SceneRuntime:_initPlayerAuthority()"):]
        init = init[: init.index("\nfunction SceneRuntime:")]
        assert "has_character_controller" in init, init
        # No source-fingerprint matchers in the identity scan.
        for forbidden in ("string.find", "string.match", ":find(", ":match(",
                          ".source", "GetSource"):
            assert forbidden not in init, (
                f"identity scan must not fingerprint source ({forbidden}):\n{init}"
            )


# ---------------------------------------------------------------------------
# AC1+AC2 (SHIPPED boot path) — drive the REAL engine:start("client") wiring:
# start() -> _initPlayerAuthority binds self._player, then the heartbeat ->
# _tick -> _playerPreTick boot path flips _booted + applies camera/mouse
# control. Every OTHER Phase-2 test calls _initPlayerAuthority directly or
# seeds engine._player; this one is the ONLY test that exercises the wiring
# start() and the heartbeat connection actually ship (so dropping the
# start()->init call OR the heartbeat->_tick->boot hook is caught here).
# ---------------------------------------------------------------------------

class TestStartClientBootPath:

    def test_start_client_binds_player_and_first_heartbeat_boots(self) -> None:
        # SHIPPED PATH: build a SceneRuntime over a one-CC-module plan with the
        # injected client services (isClient=true + the camera helpers + UIS),
        # call engine:start("client") (which connects _tick to the heartbeat
        # signal AND calls _initPlayerAuthority at its end), then FIRE the
        # heartbeat once to drive the first real _tick. We assert:
        #   (1) start() bound self._player from the upstream
        #       has_character_controller MODULE-ROW signal (non-nil); AC1.
        #   (2) before the first heartbeat the player is NOT booted; after the
        #       first heartbeat -> _tick -> _playerPreTick -> _playerBoot it IS
        #       booted AND camera/mouse control is applied (CameraType=Scriptable,
        #       MouseBehavior=LockCenter, MouseIconEnabled=false). AC2/boot.
        # Drives the REAL start() + heartbeat wiring — NOT _initPlayerAuthority
        # directly and NOT a hand-seeded engine._player.
        preamble = camera_input_preamble(mouse_deltas=[])
        body = """
            local plan = {
                modules = {
                    player = {stem = "Player", runtime_bearing = true,
                              domain = "client",
                              has_character_controller = true},
                },
                scenes = {},
                prefabs = {},
            }
            local services = servicesFor(plan, {}, {})
            services.isClient = true
            services.userInputService = game:GetService("UserInputService")
            services.players = game:GetService("Players")
            services.cameraAdvance = SceneCameraInput._advance
            services.cameraComposeLook = SceneCameraInput._composeLook

            local engine = SceneRuntime.new(services, plan)

            -- SHIPPED: start("client") connects _tick to the heartbeat AND
            -- calls _initPlayerAuthority at its end.
            engine:start("client")

            -- AC1: start() bound the authority off the upstream signal.
            print("BOUND=" .. tostring(engine._player ~= nil))
            print("BOOTED_PRE=" .. tostring(engine._player and engine._player._booted))

            -- Drive the FIRST real heartbeat -> _tick -> _playerPreTick -> boot.
            services.heartbeat:fire(0.016)

            print("BOOTED_POST=" .. tostring(engine._player and engine._player._booted))
            print("CAMTYPE=" .. tostring(workspace.CurrentCamera.CameraType))
            print("MOUSEBEHAVIOR=" .. tostring(game:GetService("UserInputService").MouseBehavior))
            print("MOUSEICON=" .. tostring(game:GetService("UserInputService").MouseIconEnabled))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # AC1: the SHIPPED start() bound the player from the upstream
        # has_character_controller signal (NOT seeded by the test).
        assert "BOUND=true" in out, out
        # Boot is deferred to the first heartbeat/_tick (start() only binds).
        assert "BOOTED_PRE=false" in out, out
        # First heartbeat -> _tick -> _playerPreTick -> _playerBoot flipped it.
        assert "BOOTED_POST=true" in out, out
        # ... and applied authoritative camera/mouse control.
        assert "CAMTYPE=Scriptable" in out, out
        assert "MOUSEBEHAVIOR=LockCenter" in out, out
        assert "MOUSEICON=false" in out, out


# ---------------------------------------------------------------------------
# AC2 — pre/post camera-write ordering around the component pass + idempotency.
# ---------------------------------------------------------------------------

class TestTickCameraBracketOrdering:

    def test_pre_write_precedes_update_and_post_write_is_last(self) -> None:
        # Drive the REAL _tick once with a bound authority + a synthetic
        # component that STOMPS a competing camera value in BOTH Update and
        # LateUpdate. Two halves of AC2, each made non-vacuous:
        #   * pre-write-precedes-Update: C's pre-write seeds yaw=1.25 BEFORE
        #     Update, so the component's mid-Update READ (recorded before it
        #     stomps) sees C's pose, not the stale 0.
        #   * post-write-is-LAST: the component stomps a SENTINEL yaw (7.0) in
        #     Update AND LateUpdate; the final camera must be C's post-write
        #     (1.25), proving _playerPostTick re-wrote AFTER the LateUpdate
        #     pass. If _playerPostTick were removed or moved before LateUpdate,
        #     the LateUpdate stomp (7.0) would survive and FINAL would be 7.0.
        preamble = camera_input_preamble(mouse_deltas=[(0.0, 0.0)])
        body = """
            local plan = {modules = {}}
            local services = servicesFor(plan, {}, {})
            services.isClient = true
            services.userInputService = game:GetService("UserInputService")
            services.cameraAdvance = SceneCameraInput._advance
            services.cameraComposeLook = SceneCameraInput._composeLook
            local engine = SceneRuntime.new(services, plan)

            -- Bind the authority with a KNOWN yaw (no mouse delta this frame,
            -- so _playerReadInput leaves it unchanged).
            engine._player = {
                _yaw = 1.25, _pitch = 0,
                _booted = true, _jumpHeld = false,
                _sensitivity = 0.0045,
                _minPitch = math.rad(-80), _maxPitch = math.rad(80),
                _eyeHeight = 1.5,
            }

            -- Synthetic component: Update records the camera yaw it observes
            -- (mid-pass, BEFORE stomping), then BOTH Update and LateUpdate
            -- STOMP a competing sentinel CFrame (yaw 7.0). The post-LateUpdate
            -- bracket must win, overwriting the LateUpdate stomp.
            local function stomp()
                local c = CFrame.new(Vector3.new(0, 0, 0))
                c._yaw = 7.0
                workspace.CurrentCamera.CFrame = c
            end
            local observed = {}
            local Comp = {}
            Comp.__index = Comp
            function Comp:Update(_dt)
                observed.preYaw = workspace.CurrentCamera.CFrame._yaw
                stomp()
            end
            function Comp:LateUpdate(_dt)
                stomp()
            end
            local comp = setmetatable({}, Comp)
            engine._meta[comp] = {
                classTable = Comp, scriptId = "comp",
                activeInHierarchy = true, enabled = true,
            }

            engine:_tick(0.016)

            print("OBSERVED_PRE=" .. tostring(observed.preYaw))
            print("FINAL=" .. tostring(workspace.CurrentCamera.CFrame._yaw))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # Pre-write-precedes-Update (non-vacuous): the mid-Update component saw
        # C's pre-write yaw (1.25), not the stale initial 0.
        assert "OBSERVED_PRE=1.25" in out, out
        # Post-write-is-LAST (non-vacuous): C's post-write (1.25) overwrote the
        # component's LateUpdate stomp (7.0). FAILS (FINAL=7.0) if
        # _playerPostTick is removed or scheduled before the LateUpdate pass.
        assert "FINAL=1.25" in out, out

    # E9 twice-call idempotency is pinned at the unit level in
    # test_player_authority.py::test_write_camera_twice_idempotent; the
    # integration value here is the pre/post ORDERING above, not a second copy.


# ---------------------------------------------------------------------------
# AC9 — server builds NO authority; _tick drives components but not the player.
# ---------------------------------------------------------------------------

class TestServerNoAuthority:

    def test_isClient_false_builds_no_player(self) -> None:
        preamble = camera_input_preamble(mouse_deltas=[])
        body = _init_authority_body(
            plan_literal="""{
                modules = {
                    player = {stem = "Player", runtime_bearing = true,
                              has_character_controller = true},
                },
            }""",
            is_client="false",
        ) + """
            print("PLAYER=" .. tostring(engine._player))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # Even with a CC module present, the server (isClient=false) builds NO
        # authority — the gate is the explicit boolean (E4/P1-2).
        assert "PLAYER=nil" in out, out

    def test_server_tick_runs_components_but_no_camera_authority(self) -> None:
        # The server _tick drives components normally but the bracket no-ops
        # (self._player == nil) — the camera is NEVER written by the authority.
        preamble = camera_input_preamble(mouse_deltas=[])
        body = """
            local plan = {modules = {
                player = {stem = "Player", has_character_controller = true},
            }}
            local services = servicesFor(plan, {}, {})
            services.isClient = false
            local engine = SceneRuntime.new(services, plan)
            engine:_initPlayerAuthority()

            -- Sentinel: the authority NEVER overwrites this if it no-ops.
            workspace.CurrentCamera.CFrame = CFrame.new(Vector3.new(0,0,0))
            workspace.CurrentCamera.CFrame._yaw = 99.0

            local ran = {n = 0}
            local Comp = {}
            Comp.__index = Comp
            function Comp:Update(_dt) ran.n = ran.n + 1 end
            local comp = setmetatable({}, Comp)
            engine._meta[comp] = {
                classTable = Comp, scriptId = "comp",
                activeInHierarchy = true, enabled = true,
            }

            engine:_tick(0.016)

            print("COMP_RAN=" .. tostring(ran.n))
            print("CAM_YAW=" .. tostring(workspace.CurrentCamera.CFrame._yaw))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        # Components still ran (server _tick is unchanged) ...
        assert "COMP_RAN=1" in out, out
        # ... but the authority bracket no-op'd: the sentinel camera survives.
        assert "CAM_YAW=99" in out, out


# ---------------------------------------------------------------------------
# AC12 — host.player:getLookCFrame() is reachable AND narrow (P1-4).
# ---------------------------------------------------------------------------

class TestHostPlayerSurface:

    def test_get_look_cframe_reachable(self) -> None:
        preamble = camera_input_preamble(mouse_deltas=[])
        body = """
            local plan = {modules = {}}
            local services = servicesFor(plan, {}, {})
            services.isClient = true
            services.cameraComposeLook = SceneCameraInput._composeLook
            local engine = SceneRuntime.new(services, plan)

            -- Seed a known camera pose; getLookCFrame mirrors CurrentCamera.
            workspace.CurrentCamera.CFrame = CFrame.new(Vector3.new(1,2,3))
            workspace.CurrentCamera.CFrame._yaw = 0.75

            local host = engine:_makeHostSurface({})
            local look = host.player:getLookCFrame()
            print("LOOK_YAW=" .. tostring(look._yaw))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "LOOK_YAW=0.75" in out, out

    def test_host_player_is_narrow(self) -> None:
        # P1-4: host.player exposes ONLY getLookCFrame — it is NOT the raw
        # engine, so internal scheduling state is not reachable through it.
        preamble = camera_input_preamble(mouse_deltas=[])
        body = """
            local plan = {modules = {}}
            local services = servicesFor(plan, {}, {})
            services.isClient = true
            local engine = SceneRuntime.new(services, plan)
            local host = engine:_makeHostSurface({})
            print("SERVICES=" .. tostring(host.player._services))
            print("PLAYER=" .. tostring(host.player._player))
            print("START=" .. tostring(host.player.start))
            print("HASLOOK=" .. tostring(type(host.player.getLookCFrame)))
        """
        rc, out, err = run_camera_scenario(preamble, body)
        assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
        assert "SERVICES=nil" in out, out
        assert "PLAYER=nil" in out, out
        assert "START=nil" in out, out
        assert "HASLOOK=function" in out, out
