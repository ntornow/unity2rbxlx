"""Slice 3.4 — ``teleport(cf)`` client-request -> server-apply, NON-load-bearing (D7).

Per D7 / D-P3-teleport, ``host.player:teleport(cf)`` is a fidelity nicety the AI
MAY call: the CLIENT fires the ``PlayerTeleport`` RemoteEvent; the SERVER owns the
character move (sets the requesting player's character HRP CFrame). It is EXPLICITLY
NON-load-bearing — if it is never called the goal still holds because respawn is
server-owned (autogen GameServer ``CharacterAdded`` spawn + engine ``SpawnLocation``).

These tests EXECUTE the real round-trip in one luau process: ``host.player:teleport``
(the REAL narrow-surface closure over ``_playerTeleport``) fires a recording remote
whose ``FireServer`` invokes the SERVER-APPLY handler — the handler body is EXTRACTED
from the REAL ``generate_game_server_script`` source (no hand-written drift; the
emission itself is additionally pinned in ``test_autogen_player_services.py``). The
server applies the CFrame to the REQUESTING player's own character HRP.

Acceptance: AC5a (round-trip), AC5b (non-load-bearing), AC5c (host.player narrow).
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from tests._camera_input_harness import (
    CAMERA_INPUT_PATH,
    camera_input_preamble,
    run_camera_scenario,
)
from tests.test_player_authority import _build_authority_runtime

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import generate_game_server_script  # noqa: E402

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


def _server_teleport_handler_body() -> str:
    """Extract the REAL ``teleportRemote.OnServerEvent`` handler body (the
    ``function(player, cf) ... end`` block) from the generated GameServer source,
    so the round-trip executes the ACTUAL server-apply logic (not a hand-written
    copy that could drift from what autogen emits)."""
    src = generate_game_server_script().source
    marker = "teleportRemote.OnServerEvent:Connect(function(player, cf)"
    start = src.index(marker)
    open_paren = start + marker.index("(function(player, cf)")
    # Walk to the matching ``end)`` that closes the Connect call. The handler is
    # a single flat function body (no nested function), so the FIRST ``end)``
    # after the marker closes it.
    rest = src[start:]
    end_at = rest.index("end)") + len("end)")
    handler = rest[:end_at]
    # Strip the outer ``teleportRemote.OnServerEvent:Connect(`` wrapper, leaving
    # ``function(player, cf) ... end`` so the test binds it to its own remote.
    inner = handler[handler.index("function(player, cf)") : handler.rindex(")")]
    return inner


# A recording remote whose FireServer routes the payload through the REAL
# server-apply handler, with a mock player whose character HRP records the
# applied CFrame. ``actorPlayer`` is the authenticated sender the server moves.
def _teleport_setup() -> str:
    handler = _server_teleport_handler_body()
    return f"""
        _tp = {{fires = {{}}}}
        -- The requesting player's character HRP (the server-apply target). Its
        -- CFrame is the thing the round-trip must set.
        local appliedHrp = {{
            CFrame = CFrame.new(Vector3.new(0, 0, 0)),
        }}
        local actorChar = {{
            FindFirstChild = function(_, name)
                if name == "HumanoidRootPart" then return appliedHrp end
                return nil
            end,
        }}
        local actorPlayer = {{Character = actorChar}}
        _tp.appliedHrp = appliedHrp
        _tp.actorPlayer = actorPlayer

        -- The REAL server-apply handler, extracted from generate_game_server_script.
        local serverApply = {handler}

        -- The recording PlayerTeleport remote: FireServer records the fire AND
        -- drives the server-apply handler with the authenticated sender (mirrors
        -- RemoteEvent semantics: the server receives ``player`` first).
        local teleportRemote = {{
            fires = {{}},
            FireServer = function(_self, cf)
                table.insert(_tp.fires, cf)
                serverApply(actorPlayer, cf)
            end,
        }}
        _tp.remote = teleportRemote
        engine._services.playerTeleportRemote = teleportRemote

        -- isCFrame: the host injects this predicate (autogen binds it to
        -- typeof(v) == "CFrame") instead of calling the typeof builtin directly,
        -- BECAUSE a standalone-luau loadstring'd chunk cannot see an outer-chunk
        -- typeof override (the builtin shadows it). Mirror Roblox semantics: our
        -- mock CFrames carry a _yaw marker; Vector3 carries X/Y/Z but no _yaw.
        engine._services.isCFrame = function(v)
            return type(v) == "table" and v._yaw ~= nil
        end

        -- typeof override (for the EXTRACTED server handler, which runs in the
        -- OUTER chunk where the override IS visible — unlike the runtime chunk).
        -- The server still type-guards via typeof(cf) == "CFrame".
        local _rawtypeof = typeof
        typeof = function(x)
            if type(x) == "table" and x._yaw ~= nil then return "CFrame" end
            return _rawtypeof(x)
        end
    """


# ---------------------------------------------------------------------------
# AC5a — client request -> server apply round-trip: host.player:teleport(cf)
# fires the remote, the server handler sets the requesting character's HRP CFrame.
# ---------------------------------------------------------------------------

def test_teleport_round_trip_client_request_server_apply() -> None:
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _teleport_setup()
        + """
        local host = engine:_makeHostSurface({})
        -- A distinct target CFrame the server must apply to the HRP.
        local target = CFrame.new(Vector3.new(42, 7, -3))
        target._yaw = 1.5

        host.player:teleport(target)

        print("FIRES=" .. tostring(#_tp.fires))
        -- The server applied the requested CFrame to the requesting character.
        local pos = _tp.appliedHrp.CFrame.Position
        print(string.format("APPLIED=%.1f,%.1f,%.1f", pos.X, pos.Y, pos.Z))
        print(string.format("APPLIED_YAW=%.6f", _tp.appliedHrp.CFrame._yaw))
        """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    # The client fired exactly once.
    assert "FIRES=1" in out, out
    # The server applied the REQUESTED CFrame to the requesting character HRP.
    assert "APPLIED=42.0,7.0,-3.0" in out, out
    assert "APPLIED_YAW=1.500000" in out, out


def test_teleport_round_trip_is_non_vacuous_mutation() -> None:
    """Mutation-prove the round-trip: a DIFFERENT requested CFrame produces a
    DIFFERENT applied HRP position (the server applies what the client sent, not
    a constant). If the server-apply were a no-op / hard-coded, this would fail
    on the second target."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _teleport_setup()
        + """
        local host = engine:_makeHostSurface({})

        local t1 = CFrame.new(Vector3.new(1, 2, 3)); t1._yaw = 0.1
        host.player:teleport(t1)
        local p1 = _tp.appliedHrp.CFrame.Position
        print(string.format("A1=%.1f,%.1f,%.1f", p1.X, p1.Y, p1.Z))

        local t2 = CFrame.new(Vector3.new(99, 88, 77)); t2._yaw = 0.2
        host.player:teleport(t2)
        local p2 = _tp.appliedHrp.CFrame.Position
        print(string.format("A2=%.1f,%.1f,%.1f", p2.X, p2.Y, p2.Z))
        """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "A1=1.0,2.0,3.0" in out, out
    assert "A2=99.0,88.0,77.0" in out, out


# ---------------------------------------------------------------------------
# AC5a guards — the client request validates its arg + nil-safe remote.
# ---------------------------------------------------------------------------

def test_teleport_rejects_non_cframe_arg() -> None:
    """``_playerTeleport`` no-ops (no fire, no crash) on a non-CFrame arg — the
    client never requests a malformed teleport, AND the server guard rejects it
    too (defence in depth)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _teleport_setup()
        + """
        local host = engine:_makeHostSurface({})
        local ok = pcall(function()
            host.player:teleport(Vector3.new(1, 2, 3))  -- not a CFrame
        end)
        print("OK=" .. tostring(ok))
        print("FIRES=" .. tostring(#_tp.fires))
        """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "OK=true" in out, out
    assert "FIRES=0" in out, out


def test_teleport_noop_when_remote_absent() -> None:
    """The remote was never injected (e.g. a place with no GameServer wiring):
    ``_playerTeleport`` no-ops cleanly — non-load-bearing, never crashes."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _teleport_setup()
        + """
        engine._services.playerTeleportRemote = nil
        local host = engine:_makeHostSurface({})
        local target = CFrame.new(Vector3.new(5, 5, 5)); target._yaw = 0.3
        local ok = pcall(function() host.player:teleport(target) end)
        print("OK=" .. tostring(ok))
        print("FIRES=" .. tostring(#_tp.fires))
        """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "OK=true" in out, out
    assert "FIRES=0" in out, out


def test_teleport_noops_on_server_no_player() -> None:
    """E6 — on the server ``self._player`` is nil, so ``_playerTeleport`` no-ops
    (the server never REQUESTS a teleport; it only applies). No fire, no crash."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _teleport_setup()
        + """
        engine._player = nil
        local host = engine:_makeHostSurface({})
        local target = CFrame.new(Vector3.new(5, 5, 5)); target._yaw = 0.3
        local ok = pcall(function() host.player:teleport(target) end)
        print("OK=" .. tostring(ok))
        print("FIRES=" .. tostring(#_tp.fires))
        """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "OK=true" in out, out
    assert "FIRES=0" in out, out


# ---------------------------------------------------------------------------
# AC5b — teleport is NON-load-bearing: never calling it leaves respawn working
# (server-owned), and no PlayerTeleport fire occurs in a normal flow.
# ---------------------------------------------------------------------------

def test_teleport_non_load_bearing_respawn_unaffected() -> None:
    """With ``host.player:teleport`` NEVER called, the PlayerTeleport remote never
    fires — and respawn is server-owned (autogen GameServer), so the goal holds.
    Build-time half: the GameServer's spawn-teleport is independent of the
    teleport remote (no ordering/data dependency between them)."""
    server_src = generate_game_server_script().source
    # Respawn (server-owned) sets the HRP to the spawn point on CharacterAdded.
    assert "player.CharacterAdded:Connect" in server_src, server_src
    assert "hrp.CFrame = spawnCFrame" in server_src, server_src
    # The teleport remote exists but the respawn path does NOT reference it — a
    # never-fired teleport cannot affect respawn (non-load-bearing).
    respawn = server_src[
        server_src.index("local function onPlayerAdded(player)") : server_src.index(
            "Players.PlayerAdded:Connect(onPlayerAdded)"
        )
    ]
    assert "teleportRemote" not in respawn, (
        "respawn must not depend on the teleport remote (D7 non-load-bearing)"
    )


def test_no_teleport_fire_in_normal_flow_luau() -> None:
    """Dynamic half of AC5b: a normal frame that never calls teleport leaves the
    remote unfired (zero ``FireServer`` calls)."""
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + _teleport_setup()
        + """
        -- A normal pre-tick (boot + input + camera) — never calls teleport.
        engine:_playerBoot()
        print("FIRES=" .. tostring(#_tp.fires))
        """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "FIRES=0" in out, out


# ---------------------------------------------------------------------------
# AC5c — host.player stays NARROW: teleport + getLookCFrame reachable;
# host.player._player / ._services / .start still nil (extends Phase-2 AC12).
# ---------------------------------------------------------------------------

def test_host_player_narrow_with_teleport() -> None:
    preamble = camera_input_preamble(mouse_deltas=[])
    body = (
        _build_authority_runtime()
        + """
        local host = engine:_makeHostSurface({})
        print("HASLOOK=" .. tostring(type(host.player.getLookCFrame)))
        print("HASTP=" .. tostring(type(host.player.teleport)))
        print("SERVICES=" .. tostring(host.player._services))
        print("PLAYER=" .. tostring(host.player._player))
        print("START=" .. tostring(host.player.start))
        """
    )
    rc, out, err = run_camera_scenario(preamble, body)
    assert rc == 0, f"scenario failed (rc={rc}): {err}\n{out}"
    assert "HASLOOK=function" in out, out
    assert "HASTP=function" in out, out
    assert "SERVICES=nil" in out, out
    assert "PLAYER=nil" in out, out
    assert "START=nil" in out, out


def test_extracted_handler_matches_emitted_source() -> None:
    """Guard the extraction: the body pulled from generate_game_server_script
    carries the server-apply logic (so the round-trip executes the REAL handler,
    not an empty/partial slice)."""
    handler = _server_teleport_handler_body()
    assert handler.startswith("function(player, cf)"), handler
    assert 'typeof(cf) ~= "CFrame"' in handler, handler
    assert "local char = player.Character" in handler, handler
    assert 'char:FindFirstChild("HumanoidRootPart")' in handler, handler
    assert "hrp.CFrame = cf" in handler, handler
    # No nested function — the flat-body assumption the extractor relies on holds.
    assert len(re.findall(r"\bfunction\b", handler)) == 1, handler
