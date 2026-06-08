"""Slice 2.3 — the autogen entrypoint services tables carry the player-authority
client/server packaging (P1-2/P2-A/P2-H).

The host-owned player-embodiment authority (paradigm C) lives IN
``scene_runtime.luau`` as methods over ``self._player`` and reaches its
dependencies through the injected ``services`` table. The CLIENT entrypoint
must require ``SceneCameraInput`` and inject ``isClient = true`` plus the three
client-only service keys (``userInputService`` + the two pure camera helpers);
the SERVER entrypoint stamps ONLY ``isClient = false`` and injects NONE of the
client helpers (the server never builds/drives the authority — E4/AC9).

Pure-Python source assertions over the generated entrypoints — no luau needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import (  # noqa: E402
    generate_game_server_script,
    generate_scene_runtime_client_entrypoint,
    generate_scene_runtime_server_entrypoint,
)


def _client_source() -> str:
    return generate_scene_runtime_client_entrypoint().source


def _server_source() -> str:
    return generate_scene_runtime_server_entrypoint().source


def _game_server_source() -> str:
    return generate_game_server_script().source


# ---------------------------------------------------------------------------
# CLIENT entrypoint — requires SceneCameraInput + isClient=true + helpers.
# ---------------------------------------------------------------------------

class TestClientEntrypoint:

    def test_requires_scene_camera_input(self) -> None:
        src = _client_source()
        assert 'require(RS:WaitForChild("SceneCameraInput"))' in src, (
            "client entrypoint must require SceneCameraInput so the player "
            "authority can reuse its pure pose helpers"
        )

    def test_isClient_true(self) -> None:
        src = _client_source()
        # Match the TABLE ASSIGNMENT (trailing comma), not a bare substring a
        # doc comment also satisfies — deleting the real key must fail this.
        assert "isClient = true," in src, (
            "client services table must stamp isClient = true (the explicit "
            "client/server discriminator the authority gates on)"
        )

    def test_injects_three_client_service_keys(self) -> None:
        src = _client_source()
        assert "userInputService = UserInputService" in src, src
        assert "cameraAdvance = SceneCameraInput._advance" in src, src
        assert "cameraComposeLook = SceneCameraInput._composeLook" in src, src

    def test_injects_camera_yaw_of(self) -> None:
        # AC3-services-pin (D-P3-services-pin): the lifecycle resync reads
        # self._services.cameraYawOf, so the CLIENT table must emit it bound to
        # the exported SceneCameraInput._yawOf — anchoring the contract on the
        # emitted source, not a manually-seeded harness fixture.
        src = _client_source()
        assert "cameraYawOf = SceneCameraInput._yawOf" in src, src

    def test_resolves_user_input_service(self) -> None:
        src = _client_source()
        assert 'game:GetService("UserInputService")' in src, (
            "client entrypoint must resolve UserInputService for the authority"
        )

    def test_injects_player_teleport_remote(self) -> None:
        # AC5a (D-P3-teleport): the host's _playerTeleport reads
        # self._services.playerTeleportRemote and FireServers it, so the CLIENT
        # table must emit it bound to the GameServer-created PlayerTeleport remote
        # — CLIENT ONLY (the server never requests a teleport). Pin the emission
        # on the source so the round-trip can't pass on a manually-seeded service.
        src = _client_source()
        assert 'playerTeleportRemote = RS:WaitForChild("PlayerTeleport"' in src, src

    def test_injects_is_cframe_predicate(self) -> None:
        # _playerTeleport guards via self._services.isCFrame (injected rather
        # than a bare typeof builtin so the predicate is testable). Pin the
        # client emission bound to typeof.
        src = _client_source()
        assert 'isCFrame = function(v) return typeof(v) == "CFrame" end' in src, src


# ---------------------------------------------------------------------------
# SERVER entrypoint — isClient=false, NO client helpers (E4/AC9).
# ---------------------------------------------------------------------------

class TestServerEntrypoint:

    def test_isClient_false(self) -> None:
        src = _server_source()
        # Match the TABLE ASSIGNMENT (trailing comma), not a bare substring the
        # surrounding doc comment ALSO contains — deleting the real key (which
        # would build authority on the server) must fail this.
        assert "isClient = false," in src, (
            "server services table must stamp isClient = false so "
            "_initPlayerAuthority leaves self._player nil (no authority)"
        )

    def test_does_not_require_scene_camera_input(self) -> None:
        src = _server_source()
        assert "SceneCameraInput" not in src, (
            "server must NOT require SceneCameraInput — it never runs the "
            f"player authority; got a reference in:\n{src}"
        )

    def test_no_client_authority_helpers(self) -> None:
        src = _server_source()
        assert "userInputService" not in src, src
        assert "cameraAdvance" not in src, src
        assert "cameraComposeLook" not in src, src
        # AC3-services-pin: the SERVER table must OMIT cameraYawOf (the server
        # never runs the lifecycle resync).
        assert "cameraYawOf" not in src, src

    def test_isClient_true_not_present(self) -> None:
        # Guard against a copy/paste that stamps the client flag on the server.
        # Forbid the ASSIGNMENT form (trailing comma) so a future doc comment
        # mentioning ``isClient = true`` doesn't false-trip this guard.
        src = _server_source()
        assert "isClient = true," not in src, src

    def test_no_player_teleport_remote_service(self) -> None:
        # The teleport REQUEST is client-only (the server applies, never
        # requests), so the server services table must OMIT the remote AND its
        # client-only CFrame predicate.
        src = _server_source()
        assert "playerTeleportRemote" not in src, src
        assert "isCFrame" not in src, src


# ---------------------------------------------------------------------------
# GameServer — the PlayerTeleport RemoteEvent + its server-apply handler
# (AC5a, D-P3-teleport). The server OWNS the character move.
# ---------------------------------------------------------------------------

class TestGameServerTeleport:

    def test_creates_player_teleport_remote(self) -> None:
        src = _game_server_source()
        assert 'teleportRemote.Name = "PlayerTeleport"' in src, src
        assert "teleportRemote.Parent = ReplicatedStorage" in src, src

    def test_server_applies_teleport_to_requesting_character(self) -> None:
        # The server-apply handler validates a CFrame and sets the REQUESTING
        # player's own character HRP CFrame (server-owned move, mirrors the
        # spawn teleport). It must read player.Character (the authenticated
        # sender), not an arbitrary target.
        src = _game_server_source()
        assert "teleportRemote.OnServerEvent:Connect(function(player, cf)" in src, src
        assert 'typeof(cf) ~= "CFrame"' in src, src
        assert "local char = player.Character" in src, src
        assert 'char:FindFirstChild("HumanoidRootPart")' in src, src
        assert "hrp.CFrame = cf" in src, src

    def test_teleport_remote_present_unconditionally(self) -> None:
        # Like PlayerShoot/PlayerGetItem, the teleport remote ships whether or
        # not the shared-flag funnel is included (it's not gated on a topology
        # fact) — so a place that never wires the funnel still has it.
        src_with = generate_game_server_script(
            include_shared_flag_funnel=True
        ).source
        src_without = generate_game_server_script(
            include_shared_flag_funnel=False
        ).source
        assert 'teleportRemote.Name = "PlayerTeleport"' in src_with
        assert 'teleportRemote.Name = "PlayerTeleport"' in src_without
