"""Phase 2 (camera-mount -> player-mount equip) acceptance criteria 1-5.

Pure-Python source assertions over the autogen-emitted SceneRuntime entrypoints
(the ``EquipWeapon`` RemoteEvent declaration + server handler + client _services
injection), plus a Luau parse gate. No Studio.

  1. autogen emits the RemoteEvent declaration (Instance.new + .Name="EquipWeapon"
     + .Parent=RS) AND an ``equipWeaponRemote.OnServerEvent:Connect`` handler.
  2. the server handler delegates to the engine methods (resolveEquipPrefabId,
     equipWeaponOnCharacter, reequipLastWeapon via CharacterAdded).
  3. the client _services carries ``equipWeaponRemote = RS:WaitForChild(...)``
     and the SERVER source does NOT (client-only parity with playerTeleportRemote).
  4. name can't drift: both the declared .Name and the client WaitForChild literal
     equal the imported ``EQUIP_REMOTE_NAME`` constant.
  5. both regenerated sources pass the Luau parse gate (the new blocks balance),
     incl. the connect-BEFORE-parent ordering.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import (  # noqa: E402
    generate_scene_runtime_client_entrypoint,
    generate_scene_runtime_server_entrypoint,
)
from converter.camera_mount_equip_lowering import (  # noqa: E402
    EQUIP_REMOTE_NAME,
    EQUIP_REMOTE_SERVICE,
)


def _server_source() -> str:
    return generate_scene_runtime_server_entrypoint().source


def _client_source() -> str:
    return generate_scene_runtime_client_entrypoint().source


# ---------------------------------------------------------------------------
# Criterion 1 — RemoteEvent declaration + OnServerEvent handler
# ---------------------------------------------------------------------------

class TestRemoteEventDeclaration:

    def test_server_declares_remote_event(self):
        src = _server_source()
        assert 'Instance.new("RemoteEvent")' in src
        assert f'equipWeaponRemote.Name = "{EQUIP_REMOTE_NAME}"' in src
        assert "equipWeaponRemote.Parent = RS" in src

    def test_server_connects_on_server_event_handler(self):
        src = _server_source()
        assert "equipWeaponRemote.OnServerEvent:Connect" in src
        # Handler signature mirrors the teleport precedent (player, <arg>).
        assert "function(player, fieldName)" in src


# ---------------------------------------------------------------------------
# Criterion 2 — handler delegates to the engine methods
# ---------------------------------------------------------------------------

class TestHandlerDelegatesToEngine:

    def test_handler_references_engine_methods(self):
        src = _server_source()
        assert "engine:resolveEquipPrefabId(fieldName)" in src
        assert "engine:equipWeaponOnCharacter(character, prefabId)" in src

    def test_character_added_reequip_block(self):
        src = _server_source()
        assert "CharacterAdded" in src
        assert "engine:reequipLastWeapon" in src
        # Remembered on a successful equip so respawn re-equips.
        assert "engine:rememberEquip(player, prefabId)" in src

    def test_respawn_reequip_is_self_healing_on_limb_swap(self):
        # Bug-1 fix: the respawn re-equip must NOT one-shot weld at a guessed-stable
        # instant (the appearance signal can fire before the final hand limb lands).
        # It watches for a hand limb ARRIVING under the Character and re-equips, so a
        # transient limb that gets replaced is healed onto the final limb.
        src = _server_source()
        # DescendantAdded (matches the runtime's late-HRP watcher; depth-robust).
        assert "char.DescendantAdded:Connect" in src, "limb-swap watcher must be wired"
        assert "RightHand" in src and "Right Arm" in src, \
            "watcher must cover R15 (RightHand) and R6 (Right Arm) hand limbs"
        # The old appearance-gate one-shot was flagged (gates the wrong event) and
        # must be gone.
        assert "HasAppearanceLoaded" not in src, \
            "the appearance-load gate (wrong event) must be removed"


# ---------------------------------------------------------------------------
# Criterion 3 — client-only _services injection parity
# ---------------------------------------------------------------------------

class TestClientServicesInjection:

    def test_client_injects_equip_remote(self):
        src = _client_source()
        assert (
            f'{EQUIP_REMOTE_SERVICE} = RS:WaitForChild("{EQUIP_REMOTE_NAME}", 10)'
            in src
        )

    def test_server_does_not_inject_client_handle(self):
        # The server HOLDS the remote object it declared; it must NOT also do a
        # client-side WaitForChild injection (parity with playerTeleportRemote).
        src = _server_source()
        assert f'{EQUIP_REMOTE_SERVICE} = RS:WaitForChild' not in src


# ---------------------------------------------------------------------------
# Criterion 4 — name can't drift
# ---------------------------------------------------------------------------

class TestNameCannotDrift:

    def test_declared_name_equals_constant(self):
        # The .Name literal is bound from the imported constant, not a hardcoded
        # string that could drift from Phase 1's request target.
        assert EQUIP_REMOTE_NAME == "EquipWeapon"
        src = _server_source()
        assert f'equipWeaponRemote.Name = "{EQUIP_REMOTE_NAME}"' in src

    def test_client_waitforchild_literal_equals_constant(self):
        src = _client_source()
        assert f'RS:WaitForChild("{EQUIP_REMOTE_NAME}", 10)' in src

    def test_no_unreplaced_sentinels(self):
        # The sentinel-replace mechanism must leave no template markers behind.
        for src in (_server_source(), _client_source()):
            assert "__EQUIP_REMOTE_NAME__" not in src
            assert "__EQUIP_REMOTE_SERVICE__" not in src
            assert "__EQUIP_SERVER_HANDLER__" not in src
            assert "__EQUIP_CLIENT_INJECTION__" not in src


# ---------------------------------------------------------------------------
# Criterion 5 — Luau parse gate + connect-before-parent ordering
# ---------------------------------------------------------------------------

class TestConnectBeforeParentOrdering:

    def test_connect_precedes_parent(self):
        # The handler must be connected BEFORE the remote is parented to RS
        # (Roblox does not buffer OnServerEvent for late listeners).
        src = _server_source()
        connect_at = src.index("equipWeaponRemote.OnServerEvent:Connect")
        parent_at = src.index("equipWeaponRemote.Parent = RS")
        assert connect_at < parent_at, (
            "the OnServerEvent handler must be connected before the remote is "
            "parented to RS (connect-then-parent race fix)"
        )


_LUAU_ANALYZE = shutil.which("luau-analyze")
_LUAU = shutil.which("luau")


def _parse_errors(source: str) -> list[str]:
    """Return luau-analyze diagnostics that are PARSE/SYNTAX errors (not the
    'Unknown global' type errors that fire because Roblox globals — game, task,
    Instance, workspace — aren't stubbed for the standalone analyzer)."""
    with tempfile.NamedTemporaryFile(
        suffix=".luau", mode="w", delete=False,
    ) as f:
        f.write(source)
        path = f.name
    try:
        proc = subprocess.run(
            ["luau-analyze", path], capture_output=True, text=True, timeout=60,
        )
        out = proc.stdout + proc.stderr
    finally:
        Path(path).unlink(missing_ok=True)
    bad: list[str] = []
    for line in out.splitlines():
        if "SyntaxError" in line:
            bad.append(line)
        # luau-analyze reports parse failures as "Expected ... near ..."
        elif "Expected" in line and "near" in line:
            bad.append(line)
    return bad


class TestLuauSyntaxValid:

    @pytest.mark.skipif(_LUAU_ANALYZE is None, reason="luau-analyze not in PATH")
    def test_server_source_parses(self):
        errors = _parse_errors(_server_source())
        assert not errors, f"server entrypoint has parse errors: {errors}"

    @pytest.mark.skipif(_LUAU_ANALYZE is None, reason="luau-analyze not in PATH")
    def test_client_source_parses(self):
        errors = _parse_errors(_client_source())
        assert not errors, f"client entrypoint has parse errors: {errors}"

    @pytest.mark.skipif(_LUAU is None, reason="luau not in PATH")
    def test_server_source_loads_under_luau(self):
        # An independent parse gate: wrap the source in a function and loadstring
        # it (compiles -> proves it parses; we never run it, so Roblox globals
        # being absent is irrelevant).
        src = _server_source()
        harness = (
            "local SRC = [====[\n" + src + "\n]====]\n"
            'local chunk, err = loadstring(SRC, "server")\n'
            'assert(chunk, "parse failed: " .. tostring(err))\n'
            'print("ok")\n'
        )
        with tempfile.NamedTemporaryFile(
            suffix=".luau", mode="w", delete=False,
        ) as f:
            f.write(harness)
            path = f.name
        try:
            proc = subprocess.run(
                ["luau", path], capture_output=True, text=True, timeout=15,
            )
        finally:
            Path(path).unlink(missing_ok=True)
        assert proc.returncode == 0 and "ok" in proc.stdout, (
            f"server source failed to compile: {proc.stdout}{proc.stderr}"
        )
