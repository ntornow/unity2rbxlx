"""Tests for the damage routing slice (PR #73c).

Covers:
  - ``damage_protocol.luau`` structural contract: type guards run
    before any Instance call, origin drift gate, raycast replay from
    the client-supplied camera origin/dir, value-preserving attribute
    mirror with non-scalar coercion.
  - Pipeline injection: ``DamageProtocol`` is listed in the runtime
    modules tuple alongside the other Gameplay families.
  - Orchestrator force-require: ``gameplay.luau`` requires
    ``DamageProtocol`` so the server-side OnServerEvent is bound
    before any bullet hit fires the attribute mirror path.
  - Legacy ``player_damage_remote_event`` interaction: when adapter
    stubs are present, the pack still patches Player LocalScripts to
    ``FireServer`` (the inline client-fire path is required) but the
    ``_AutoDamageEventRouter`` Script is NOT emitted, and a stale
    legacy router from a prior conversion is removed so
    ``damage_protocol.luau`` doesn't double-bind ``OnServerEvent``.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Runtime file helpers
# ---------------------------------------------------------------------------

def _damage_protocol_path() -> Path:
    return (
        Path(__file__).parent.parent
        / "runtime" / "gameplay" / "damage_protocol.luau"
    )


def _gameplay_orchestrator_path() -> Path:
    return (
        Path(__file__).parent.parent
        / "runtime" / "gameplay" / "gameplay.luau"
    )


def _read_damage_protocol() -> str:
    return _damage_protocol_path().read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Structural contract on damage_protocol.luau
# ---------------------------------------------------------------------------

class TestDamageProtocolStructure:
    """The Lua source is the source of truth for the runtime contract.
    Structural assertions pin the invariants codex round-1 [P1] and
    rounds [P2] flagged on the legacy pack so a future refactor of
    damage_protocol.luau can't regress those without failing CI.
    """

    def test_file_present(self) -> None:
        assert _damage_protocol_path().exists(), (
            "runtime/gameplay/damage_protocol.luau missing — PR #73c "
            "injects it but the file isn't on disk."
        )

    def test_type_guards_before_instance_methods(self) -> None:
        """A malicious client can ``FireServer(true)`` /
        ``FireServer({})``; the server must reject the payload before
        calling ``hitInstance:IsA`` / ``:IsDescendantOf`` etc.

        Pin: inside ``_onServerEvent``, the first ``hitInstance:``
        method call must be preceded by a
        ``typeof(hitInstance) ~= "Instance"`` guard. Codex round-11
        [P2] regression on the legacy router. Scoped to the function
        body so the header docstring's prose mention of
        ``hitInstance:`` doesn't fail the check.
        """
        body = _read_damage_protocol()
        fn_start = body.find("local function _onServerEvent(")
        assert fn_start != -1, (
            "_onServerEvent function header missing — refactor must "
            "rename the test along with the symbol."
        )
        # Bound the search to the next top-level ``local function`` /
        # ``function DamageProtocol.`` declaration so we don't slip
        # into a different function's body.
        next_local = body.find("\nlocal function ", fn_start + 1)
        next_method = body.find("\nfunction DamageProtocol.", fn_start + 1)
        candidates = [c for c in (next_local, next_method) if c != -1]
        fn_end = min(candidates) if candidates else len(body)
        fn_body = body[fn_start:fn_end]

        type_guard_idx = fn_body.find('typeof(hitInstance) ~= "Instance"')
        assert type_guard_idx != -1, (
            "missing ``typeof(hitInstance) ~= \"Instance\"`` guard "
            "in _onServerEvent — a non-Instance payload crashes "
            ":IsA / :IsDescendantOf."
        )
        # Use a regex for METHOD calls so Luau type-annotation
        # parameter colons (``hitInstance: any``) don't fool the
        # check. Method calls have the form ``hitInstance:Method(``
        # where Method starts with an uppercase letter.
        import re as _re
        method_match = _re.search(r"hitInstance:[A-Z]\w+\(", fn_body)
        assert method_match is not None, (
            "_onServerEvent never calls hitInstance:Method(...) — "
            "the validator can't possibly mirror the attribute."
        )
        method_idx = method_match.start()
        assert type_guard_idx < method_idx, (
            f"hitInstance method call inside _onServerEvent at offset "
            f"{method_idx} precedes the typeof guard at offset "
            f"{type_guard_idx}. A bad payload would crash before "
            f"validation."
        )

    def test_origin_drift_gate_present(self) -> None:
        """Without an origin-drift check, a malicious client can fire
        ``DamageEvent`` with arbitrary camera origins anywhere in the
        map. Pin both the constant and the comparison.
        """
        body = _read_damage_protocol()
        assert "MAX_ORIGIN_DRIFT_STUDS" in body, (
            "origin drift constant gone — anti-teleport gate removed."
        )
        assert "(originPos - hrp.Position).Magnitude" in body, (
            "origin drift comparison removed — gate is a no-op."
        )

    def test_raycast_replay_from_client_origin(self) -> None:
        """Server raycast must use the CLIENT-supplied origin + dir.
        Codex round-9 [P1] on the legacy router: re-casting from
        HumanoidRootPart→hitInstance rejects legitimate over-cover
        shots because the FPS Player.cs raycasts from the camera, not
        the character root.
        """
        body = _read_damage_protocol()
        cast_idx = body.find("workspace:Raycast(")
        assert cast_idx != -1, "no workspace:Raycast call — replay path is missing."
        cast_args = body[cast_idx : cast_idx + 200]
        assert "originPos" in cast_args, (
            "workspace:Raycast no longer takes the client-supplied "
            "originPos — replay uses character-root origin and "
            "rejects over-cover shots."
        )
        assert "lookDir" in cast_args, (
            "workspace:Raycast no longer takes the client-supplied "
            "lookDir — replay uses an inferred direction."
        )

    def test_value_preserving_with_scalar_coercion(self) -> None:
        """Codex round-10 [P1]: synthesizing a counter on the server
        discards the client's payload, under-damaging listeners that
        read ``TakeDamage`` as the damage amount. Codex round-11 [P2]:
        non-scalar payloads must coerce to ``true`` so SetAttribute
        doesn't crash on tables / functions.
        """
        body = _read_damage_protocol()
        assert 'SetAttribute("TakeDamage"' in body, (
            "attribute mirror gone — listeners never fire."
        )
        # Coercion: ``true`` returned when type is none of
        # bool/number/string.
        assert (
            't == "boolean"' in body
            and 't == "number"' in body
            and 't == "string"' in body
        ), (
            "non-scalar coercion table missing — a malicious table "
            "payload would crash SetAttribute."
        )

    def test_remote_event_name_matches_legacy(self) -> None:
        """The legacy pack's body-patch FireServer call targets
        ``ReplicatedStorage.DamageEvent``. damage_protocol.luau must
        own that exact name or the patched LocalScripts no-op.
        """
        body = _read_damage_protocol()
        assert 'DAMAGE_EVENT_NAME: string = "DamageEvent"' in body, (
            "DamageEvent name diverged — legacy pack's body-patch "
            "calls won't resolve."
        )

    def test_orchestrator_force_requires_damage_protocol(self) -> None:
        """``gameplay.luau`` must force-require ``DamageProtocol`` so
        the server router's OnServerEvent is bound before the first
        adapter-stub ``Gameplay.run`` call. Without the require,
        Players who shoot before any adapter binds would FireServer
        into a RemoteEvent with no listener.
        """
        body = _gameplay_orchestrator_path().read_text(encoding="utf-8")
        assert 'WaitForChild("DamageProtocol")' in body, (
            "gameplay.luau orchestrator no longer force-requires "
            "DamageProtocol — server router init is racy."
        )

    def test_server_init_guarded_by_RunService_IsServer(self) -> None:
        """When ``damage_protocol.luau`` runs on the CLIENT (every
        AutoGen module replicates to all peers), it must not try to
        :Connect a server-only signal — ``OnServerEvent`` is not a
        signal on the client and the connection call errors. Pin
        ``RunService:IsServer()`` gating the OnServerEvent bind.
        """
        body = _read_damage_protocol()
        init_idx = body.find("OnServerEvent:Connect")
        assert init_idx != -1, "OnServerEvent:Connect call missing."
        # Walk backwards a reasonable window — the IsServer guard
        # should appear before the connect call within the init
        # function.
        window = body[max(0, init_idx - 600) : init_idx]
        assert "RunService:IsServer()" in window, (
            "OnServerEvent:Connect is not gated by "
            "RunService:IsServer() — clients would error on require."
        )

    def test_fire_helper_guarded_by_RunService_IsClient(self) -> None:
        """``DamageProtocol.fire`` is the client-side helper. If a
        server context accidentally calls it, ``FireServer`` errors.
        Pin the guard.
        """
        body = _read_damage_protocol()
        fire_idx = body.find("function DamageProtocol.fire(")
        assert fire_idx != -1, "DamageProtocol.fire helper missing."
        helper_window = body[fire_idx : fire_idx + 1200]
        assert "RunService:IsClient()" in helper_window, (
            "DamageProtocol.fire isn't gated by RunService:IsClient() "
            "— a server caller would crash on FireServer."
        )


# ---------------------------------------------------------------------------
# Pipeline injection wiring
# ---------------------------------------------------------------------------

class TestPipelineInjection:
    """The pipeline's ``_inject_runtime_modules`` lists every gameplay
    family that should be present under ReplicatedStorage.AutoGen when
    adapters bind. PR #73c adds DamageProtocol to that list.
    """

    def test_damage_protocol_in_gameplay_modules_tuple(self) -> None:
        """AST scan rather than substring scan so a comment mention
        doesn't count. Pin the actual tuple-literal entry.
        """
        import ast
        import inspect
        from converter import pipeline as pipeline_mod

        src = inspect.getsource(pipeline_mod)
        tree = ast.parse(src)

        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Tuple):
                continue
            # Each entry is a 2-tuple of string literals.
            for elt in node.elts:
                if not isinstance(elt, ast.Tuple) or len(elt.elts) != 2:
                    continue
                first, second = elt.elts
                if (
                    isinstance(first, ast.Constant)
                    and first.value == "DamageProtocol"
                    and isinstance(second, ast.Constant)
                    and second.value == "damage_protocol.luau"
                ):
                    found = True
                    break
            if found:
                break
        assert found, (
            "pipeline.py's gameplay_modules tuple no longer carries "
            "(\"DamageProtocol\", \"damage_protocol.luau\") — runtime "
            "modules won't include the damage router."
        )


# ---------------------------------------------------------------------------
# Legacy pack interaction
# ---------------------------------------------------------------------------

def _make_script(*, name: str, source: str, script_type: str = "Script",
                 parent_path: str = "") -> object:
    from core.roblox_types import RbxScript
    return RbxScript(
        name=name,
        source=source,
        script_type=script_type,
        parent_path=parent_path,
    )


def _patched_player_source() -> str:
    """Minimal Player LocalScript body that's already been patched by
    a prior legacy-pack run. The marker substring is what the pack's
    detect function keys off.
    """
    return (
        "-- player local script\n"
        'local hitInst = workspace.Origin\n'
        'hitInst:SetAttribute("TakeDamage", 10)\n'
        '-- _AutoDamageRemoteEventInjected: mirror client damage to server\n'
        "do\n"
        '    local _de = game:GetService("ReplicatedStorage")'
        ':FindFirstChild("DamageEvent")\n'
        "    if _de then _de:FireServer(hitInst, 10, Vector3.new(), Vector3.new()) end\n"
        "end\n"
    )


def _adapter_stub_source() -> str:
    """One adapter-emitted stub — the marker on the first line is what
    the legacy pack now keys off to decide that DamageProtocol owns
    the router role.
    """
    from converter.gameplay.composer import ADAPTER_STUB_MARKER
    return (
        f"-- {ADAPTER_STUB_MARKER} TurretBullet "
        "(generated by gameplay adapters)\n"
        "return nil\n"
    )


class TestLegacyPackInteraction:
    """Adapter-active → no legacy router script lingers."""

    def test_router_not_emitted_when_adapters_active_fresh(self) -> None:
        """Clean conversion with adapters on: Player.cs needs patching,
        adapter stubs are emitted, the legacy router script must NOT
        be appended.
        """
        from converter.script_coherence_packs import run_packs
        # Fresh Player LocalScript with NO _AutoDamageRemoteEventInjected
        # marker so step 1 (body-patch) runs.
        fresh_player_src = (
            "-- player local script\n"
            'local hitInst = workspace.Origin\n'
            'hitInst:SetAttribute("TakeDamage", true)\n'
            'local model = hitInst:FindFirstAncestorOfClass("Model")\n'
            'if model then model:SetAttribute("TakeDamage", true) end\n'
        )
        scripts = [
            _make_script(
                name="Player",
                source=fresh_player_src,
                script_type="LocalScript",
            ),
            _make_script(
                name="TurretBullet",
                source=_adapter_stub_source(),
                script_type="ModuleScript",
            ),
        ]
        run_packs(scripts, disabled=set())
        router_names = [s.name for s in scripts if s.name == "_AutoDamageEventRouter"]
        assert router_names == [], (
            "legacy _AutoDamageEventRouter emitted despite adapter "
            "stubs being present — damage_protocol.luau would "
            "double-bind OnServerEvent."
        )
        # Body-patch should still have happened.
        patched_player = next(s for s in scripts if s.name == "Player")
        assert "_AutoDamageRemoteEventInjected" in patched_player.source, (
            "Player.cs no longer FireServer's — adapter mutex must "
            "only skip the ROUTER half, not the body-patch half."
        )

    def test_stale_router_removed_when_adapters_active(self) -> None:
        """Rehydrate from an adapters-off output that left a legacy
        router on disk, now turning adapters on. The pack must remove
        the stale router so damage_protocol.luau doesn't double-bind.
        """
        from converter.script_coherence_packs import (
            run_packs, _DAMAGE_ROUTER_SOURCE,
        )
        scripts = [
            _make_script(
                name="Player",
                source=_patched_player_source(),
                script_type="LocalScript",
            ),
            _make_script(
                name="_AutoDamageEventRouter",
                source=_DAMAGE_ROUTER_SOURCE,
                script_type="Script",
                parent_path="ServerScriptService",
            ),
            _make_script(
                name="TurretBullet",
                source=_adapter_stub_source(),
                script_type="ModuleScript",
            ),
        ]
        run_packs(scripts, disabled=set())
        router_names = [
            s.name for s in scripts if s.name == "_AutoDamageEventRouter"
        ]
        assert router_names == [], (
            "stale legacy router survived a re-conversion with "
            "adapters active — double-binding risk."
        )

    def test_router_still_emitted_when_adapters_absent(self) -> None:
        """Adapters off → legacy router must still be emitted. The
        adapters-active short-circuit must not regress the legacy
        path.
        """
        from converter.script_coherence_packs import run_packs
        fresh_player_src = (
            "-- player local script\n"
            'local hitInst = workspace.Origin\n'
            'hitInst:SetAttribute("TakeDamage", true)\n'
            'local model = hitInst:FindFirstAncestorOfClass("Model")\n'
            'if model then model:SetAttribute("TakeDamage", true) end\n'
        )
        scripts = [
            _make_script(
                name="Player",
                source=fresh_player_src,
                script_type="LocalScript",
            ),
        ]
        run_packs(scripts, disabled=set())
        router_names = [
            s.name for s in scripts if s.name == "_AutoDamageEventRouter"
        ]
        assert router_names == ["_AutoDamageEventRouter"], (
            "legacy router not emitted in an adapters-off conversion "
            "— legacy behaviour regressed."
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
