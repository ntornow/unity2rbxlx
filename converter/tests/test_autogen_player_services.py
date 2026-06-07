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
    generate_scene_runtime_client_entrypoint,
    generate_scene_runtime_server_entrypoint,
)


def _client_source() -> str:
    return generate_scene_runtime_client_entrypoint().source


def _server_source() -> str:
    return generate_scene_runtime_server_entrypoint().source


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
        assert "isClient = true" in src, (
            "client services table must stamp isClient = true (the explicit "
            "client/server discriminator the authority gates on)"
        )

    def test_injects_three_client_service_keys(self) -> None:
        src = _client_source()
        assert "userInputService = UserInputService" in src, src
        assert "cameraAdvance = SceneCameraInput._advance" in src, src
        assert "cameraComposeLook = SceneCameraInput._composeLook" in src, src

    def test_resolves_user_input_service(self) -> None:
        src = _client_source()
        assert 'game:GetService("UserInputService")' in src, (
            "client entrypoint must resolve UserInputService for the authority"
        )


# ---------------------------------------------------------------------------
# SERVER entrypoint — isClient=false, NO client helpers (E4/AC9).
# ---------------------------------------------------------------------------

class TestServerEntrypoint:

    def test_isClient_false(self) -> None:
        src = _server_source()
        assert "isClient = false" in src, (
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

    def test_isClient_true_not_present(self) -> None:
        # Guard against a copy/paste that stamps both flags on the server.
        src = _server_source()
        assert "isClient = true" not in src, src
