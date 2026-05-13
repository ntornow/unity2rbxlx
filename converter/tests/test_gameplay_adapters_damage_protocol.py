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

    def test_orchestrator_conditionally_requires_damage_protocol(self) -> None:
        """``gameplay.luau`` must require ``DamageProtocol`` when it's
        present so the server router's OnServerEvent is bound before
        the first adapter-stub ``Gameplay.run`` call. PR #74 codex
        round-2 [P2] gated the converter side: ``DamageProtocol`` is
        no longer emitted for adapter projects that lack a
        Player-damage signal (no ``effect.damage`` / ``effect.splash``
        match and no legacy ``FireServer(DamageEvent`` body-patch),
        and the orchestrator's require has to be a clean absence
        when the module is missing — not a 5-second ``WaitForChild``
        stall on every door-only / projectile-only project.

        Pin the conditional shape:

          * Uses ``FindFirstChild`` (immediate, returns nil if absent).
          * The require is guarded so a nil result is a no-op rather
            than an error.
          * The damage-bearing case still resolves: when the converter
            emits ``DamageProtocol`` alongside the orchestrator,
            ``FindFirstChild`` finds it and the require fires.
        """
        body = _gameplay_orchestrator_path().read_text(encoding="utf-8")
        assert 'FindFirstChild("DamageProtocol")' in body, (
            "gameplay.luau no longer probes DamageProtocol via "
            "FindFirstChild — PR #74 [P2] gating regressed."
        )
        assert 'WaitForChild("DamageProtocol")' not in body, (
            "gameplay.luau still uses WaitForChild for DamageProtocol "
            "— would 5-second stall every door/projectile-only "
            "adapter project. PR #74 [P2] fix regressed."
        )
        damage_idx = body.find('FindFirstChild("DamageProtocol")')
        # The require call must appear within a short window after
        # the FindFirstChild lookup AND be guarded by a nil check.
        window = body[damage_idx : damage_idx + 200]
        assert "if damageProtocol ~= nil" in window or "if damageProtocol" in window, (
            "gameplay.luau requires DamageProtocol without a nil "
            "guard — would error on door/projectile-only projects "
            "where the converter omitted the module."
        )
        assert "require(damageProtocol)" in window, (
            "gameplay.luau dropped the conditional require of "
            "DamageProtocol — damage-bearing projects regress."
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
    adapters bind. PR #73c adds DamageProtocol to that list AND emits a
    server-side bootstrap Script so the adapter runtime is force-
    loaded at server start (codex PR #73c-round-1 [P1] — without this,
    prefab-only adapter coverage delays DamageProtocol init past the
    first Player click and the body-patched FireServer call hits a
    nil RemoteEvent).
    """

    def test_damage_protocol_wired_into_gameplay_modules(self) -> None:
        """AST scan for the (``"DamageProtocol"``, ``"damage_protocol.luau"``)
        tuple literal anywhere in pipeline.py. PR #74 codex round-2
        [P2] made the entry conditional (only appended when
        ``_damage_protocol_needed`` returns True), so the scan is
        looser than the pre-PR-#74 "must appear inside the
        ``gameplay_modules`` tuple-of-tuples" check — but a tuple
        of the right shape still has to exist in the source.
        """
        import ast
        import inspect
        from converter import pipeline as pipeline_mod

        src = inspect.getsource(pipeline_mod)
        tree = ast.parse(src)

        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
                continue
            first, second = node.elts
            if (
                isinstance(first, ast.Constant)
                and first.value == "DamageProtocol"
                and isinstance(second, ast.Constant)
                and second.value == "damage_protocol.luau"
            ):
                found = True
                break
        assert found, (
            "pipeline.py no longer carries a "
            "(\"DamageProtocol\", \"damage_protocol.luau\") tuple — "
            "runtime modules can't include the damage router on the "
            "Player-damage path."
        )

    def test_damage_protocol_injection_is_gated(self) -> None:
        """PR #74 codex round-2 [P2]: the DamageProtocol tuple must be
        conditionally appended, not unconditionally included in the
        module list. Pin the gating method exists AND the append is
        inside an ``if`` block referring to it (so a future refactor
        that drops the gate fails this test rather than silently
        regressing the [P2]).
        """
        import inspect
        from converter import pipeline as pipeline_mod

        src = inspect.getsource(pipeline_mod)
        assert "_damage_protocol_needed" in src, (
            "pipeline.py no longer defines a _damage_protocol_needed "
            "helper — PR #74 [P2] gating regressed."
        )
        # The append must be guarded by the gate. Scan for the local
        # name binding pattern produced by the gating block:
        #   needs_damage_protocol = self._damage_protocol_needed()
        #   ...
        #   if needs_damage_protocol:
        #       base_modules.append(("DamageProtocol", "damage_protocol.luau"))
        assert "needs_damage_protocol" in src, (
            "pipeline.py no longer binds a needs_damage_protocol "
            "local from _damage_protocol_needed — [P2] gating "
            "regressed."
        )
        assert "if needs_damage_protocol" in src, (
            "pipeline.py no longer branches on needs_damage_protocol "
            "before appending DamageProtocol — [P2] gating regressed."
        )

    def test_damage_protocol_needed_signals(self) -> None:
        """Unit-test ``_damage_protocol_needed`` against the two
        documented signals (effect.damage / effect.splash capability
        kind; legacy ``FindFirstChild("DamageEvent")`` body-patch
        marker in any script source).

        Built without a real Pipeline instance — the method is pure
        over ``self.state.gameplay_matches`` and ``self.state.rbx_place``.
        """
        import types
        from converter.pipeline import Pipeline, _DAMAGE_CAPABILITY_KINDS

        # Minimal duck-typed state harness. _damage_protocol_needed
        # only reads .state.gameplay_matches and .state.rbx_place.
        class _M:
            def __init__(self, kinds: tuple[str, ...]) -> None:
                self.capability_kinds = kinds

        class _State:
            def __init__(self) -> None:
                self.gameplay_matches: list = []
                self.rbx_place = types.SimpleNamespace(
                    scripts=[], workspace_parts=[], replicated_templates=[],
                )

        def _call(state) -> bool:
            harness = types.SimpleNamespace(state=state)
            return Pipeline._damage_protocol_needed(harness)

        # No signal → False.
        s = _State()
        assert _call(s) is False, (
            "_damage_protocol_needed returned True on an empty state "
            "— would unnecessarily claim ReplicatedStorage.DamageEvent."
        )

        # Effect.damage capability → True.
        s = _State()
        s.gameplay_matches.append(_M(("movement.impulse", "effect.damage")))
        assert _call(s) is True, (
            "_damage_protocol_needed returned False for an "
            "effect.damage match — projectile slice would drop the "
            "server router."
        )

        # Effect.splash capability → True.
        s = _State()
        s.gameplay_matches.append(_M(("hit_detection.overlap_sphere", "effect.splash")))
        assert _call(s) is True, (
            "_damage_protocol_needed returned False for an "
            "effect.splash match — AoE damage would no-op server-side."
        )

        # Non-damage capability only → False.
        s = _State()
        s.gameplay_matches.append(_M(("trigger.on_bool_attribute", "movement.attribute_driven_tween")))
        assert _call(s) is False, (
            "_damage_protocol_needed returned True for a door-only "
            "match (trigger + tween, no damage capability) — the "
            "PR #74 [P2] collision-risk gate regressed."
        )

        # Legacy body-patch marker present → True.
        s = _State()
        s.rbx_place.scripts = [
            types.SimpleNamespace(
                source=(
                    'local _de = game:GetService("ReplicatedStorage")'
                    ':FindFirstChild("DamageEvent")\n'
                    "if _de then _de:FireServer(hitInst) end\n"
                ),
            ),
        ]
        assert _call(s) is True, (
            "_damage_protocol_needed missed the legacy body-patch "
            "marker — Player.cs body-patch path drops the server "
            "validator."
        )

        # Sanity-pin the capability set so a future capability rename
        # (e.g. ``effect.damage`` → ``effect.player_damage``) fails this
        # test instead of silently dropping damage routing.
        assert _DAMAGE_CAPABILITY_KINDS == frozenset({
            "effect.damage", "effect.splash",
        }), (
            "Damage capability set drifted — update both the "
            "Pipeline gate and this test in lockstep."
        )

    def test_damage_protocol_needed_rehydrates_via_adapter_stub_scan(
        self,
    ) -> None:
        """PR #74 codex round-4 [P1]: on resume / publish rebuild,
        ``state.gameplay_matches`` is empty by design. A damage-bearing
        adapter project (bullets with ``effect.damage`` capability,
        no legacy body-patch) must still be detected so DamageProtocol
        gets re-injected and the server-side ``DamageEvent`` listener
        stays live.

        Detection signal: the composer's capability-table literal
        ``{kind = "effect.damage"`` (or ``effect.splash``) on a stub
        emitted to ``state.rbx_place.scripts`` by a previous run.
        """
        import types
        from converter.pipeline import Pipeline

        class _State:
            def __init__(self) -> None:
                self.gameplay_matches: list = []
                self.rbx_place = types.SimpleNamespace(
                    scripts=[
                        types.SimpleNamespace(
                            source=(
                                "-- @@AUTOGEN_GAMEPLAY_ADAPTER@@ TurretBullet\n"
                                "Gameplay.run(_container, {\n"
                                '    {kind = "effect.damage", value = 10},\n'
                                "})\n"
                            ),
                        ),
                    ],
                    workspace_parts=[],
                    replicated_templates=[],
                )

        harness = types.SimpleNamespace(state=_State())
        assert Pipeline._damage_protocol_needed(harness) is True, (
            "rehydrate path missed an adapter stub carrying "
            "effect.damage — codex PR #74 round-4 [P1] regressed."
        )

        # And the splash variant — same path, different capability kind.
        class _StateSplash:
            def __init__(self) -> None:
                self.gameplay_matches: list = []
                self.rbx_place = types.SimpleNamespace(
                    scripts=[
                        types.SimpleNamespace(
                            source=(
                                "-- @@AUTOGEN_GAMEPLAY_ADAPTER@@ Explosion\n"
                                'Gameplay.run(_container, {\n'
                                '    {kind = "effect.splash", radius_studs = 20},\n'
                                "})\n"
                            ),
                        ),
                    ],
                    workspace_parts=[],
                    replicated_templates=[],
                )

        harness_splash = types.SimpleNamespace(state=_StateSplash())
        assert Pipeline._damage_protocol_needed(harness_splash) is True, (
            "rehydrate path missed an adapter stub carrying "
            "effect.splash — AoE damage routes lose their server "
            "validator on resume."
        )

    def test_legacy_probe_rejects_bare_FindFirstChild_lookup(self) -> None:
        """PR #74 codex round-4 [P2]: a bare
        ``FindFirstChild("DamageEvent")`` lookup in an unrelated user
        script must NOT trip the legacy-marker probe. The tighter
        marker pins on the pack's full ``local _de = game:GetService
        ("ReplicatedStorage"):FindFirstChild("DamageEvent")`` line.
        Without this narrowing, adapter-enabled projects that
        coincidentally look up a ``DamageEvent`` for unrelated
        networking would re-trigger DamageProtocol injection —
        the exact collision PR #74 is supposed to avoid.
        """
        import types
        from converter.pipeline import Pipeline

        class _State:
            def __init__(self) -> None:
                self.gameplay_matches: list = []
                self.rbx_place = types.SimpleNamespace(
                    scripts=[
                        types.SimpleNamespace(
                            source=(
                                "-- user-authored script unrelated "
                                "to the legacy damage pack\n"
                                "local rs = game:GetService"
                                "(\"ReplicatedStorage\")\n"
                                "local myEvent = rs:FindFirstChild"
                                "(\"DamageEvent\")\n"
                                "if myEvent then\n"
                                "    myEvent.OnClientEvent:Connect(...)\n"
                                "end\n"
                            ),
                        ),
                    ],
                    workspace_parts=[],
                    replicated_templates=[],
                )

        harness = types.SimpleNamespace(state=_State())
        assert Pipeline._damage_protocol_needed(harness) is False, (
            "_damage_protocol_needed false-positived on a bare "
            "FindFirstChild('DamageEvent') user-code call — codex "
            "PR #74 round-4 [P2] regressed. DamageProtocol would "
            "re-claim ReplicatedStorage.DamageEvent for non-damage "
            "projects."
        )

    def test_server_bootstrap_emits_under_server_script_service(
        self,
    ) -> None:
        """Codex PR #73c-round-1 [P1]: a bootstrap Script in
        ServerScriptService force-loads ``AutoGen.Gameplay`` at server
        start so DamageProtocol's OnServerEvent is bound before any
        client clicks. Pin: (a) the bootstrap source file exists, (b)
        ``_inject_runtime_modules`` references ``server_bootstrap.luau``
        AND ``"ServerScriptService"`` so the Script lands in the right
        parent, (c) the source body actually requires
        ``AutoGen.Gameplay`` (not some other module).
        """
        import inspect
        from converter import pipeline as pipeline_mod

        # (a) The bootstrap file is present.
        bootstrap_path = (
            Path(__file__).parent.parent
            / "runtime" / "gameplay" / "server_bootstrap.luau"
        )
        assert bootstrap_path.exists(), (
            "runtime/gameplay/server_bootstrap.luau missing — the "
            "[P1] always-on damage routing bootstrap isn't on disk."
        )

        # (b) Pipeline references the bootstrap by filename and
        # places it under ServerScriptService.
        src = inspect.getsource(pipeline_mod)
        assert "server_bootstrap.luau" in src, (
            "pipeline.py no longer references server_bootstrap.luau "
            "— the [P1] bootstrap script won't be injected."
        )
        # The injection block lives near the existing gameplay-modules
        # injection. Walk the source to confirm the bootstrap script
        # is parented to ServerScriptService AND typed as a Script (a
        # ModuleScript would have the same problem — it wouldn't
        # auto-run).
        bootstrap_idx = src.find("server_bootstrap.luau")
        # Look in a generous window around the reference for both
        # markers; the actual injection block fits within ~1500 chars
        # of the filename reference.
        window = src[max(0, bootstrap_idx - 200) : bootstrap_idx + 1500]
        assert '"ServerScriptService"' in window, (
            "pipeline.py references server_bootstrap.luau but the "
            "injection no longer parents it to ServerScriptService — "
            "the bootstrap won't auto-run on server start."
        )
        assert '"Script"' in window, (
            "pipeline.py references server_bootstrap.luau but the "
            "injection no longer types it as Script — a ModuleScript "
            "would not auto-run on server start."
        )

        # (c) The bootstrap body actually requires AutoGen.Gameplay.
        body = bootstrap_path.read_text(encoding="utf-8")
        assert 'WaitForChild("AutoGen"' in body, (
            "bootstrap no longer waits for AutoGen folder — startup "
            "race regression."
        )
        assert 'WaitForChild("Gameplay"' in body, (
            "bootstrap no longer waits for AutoGen.Gameplay — the "
            "orchestrator won't be required at server start."
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
