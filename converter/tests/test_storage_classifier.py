"""
test_storage_classifier.py -- Tests for Phase 4a.5 storage classification.

Covers the explicit server / client / replicated storage decisions that
phase-4a-storage-classification.md describes. Exercises:

- Scripts with client-only APIs -> StarterPlayerScripts
- Scripts with server-only APIs -> ServerScriptService
- ModuleScripts required only by Scripts -> ServerStorage
- ModuleScripts required by any LocalScript -> ReplicatedStorage
- ModuleScripts with mixed callers -> ReplicatedStorage (safer default)
- Orphan modules -> ReplicatedStorage (survivable default)
- Loader scripts with name hints -> ReplicatedFirst
- Character-attached scripts -> StarterCharacterScripts
- RemoteEvent name harvesting
- Template container assignment (forward-looking)
"""

from __future__ import annotations


import pytest

from core.roblox_types import RbxScript
from converter.storage_classifier import (
    classify_storage,
    ConstraintViolation,
    SERVER_SCRIPT_SERVICE,
    SERVER_STORAGE,
    REPLICATED_STORAGE,
    REPLICATED_FIRST,
    STARTER_PLAYER_SCRIPTS,
    STARTER_CHARACTER_SCRIPTS,
    _decide_script_container_from_topology,
    _enforce_hard_constraints,
)


def _make_script(name: str, source: str, script_type: str = "Script") -> RbxScript:
    return RbxScript(name=name, source=source, script_type=script_type)


# ---------------------------------------------------------------------------
# Client vs server API detection
# ---------------------------------------------------------------------------


def test_client_local_script_lands_in_starter_player_scripts():
    """Phase 2a slice 7 (2026-05-30): migrated from
    ``test_client_only_api_forces_local_script``. The legacy regex
    paths (_CLIENT_ONLY_PATTERNS / _SERVER_ONLY_PATTERNS) were
    deleted in slice 7 -- domain classification now comes from the
    topology prepass (``infer_module_domains``) and the script_type
    promotion (Script -> LocalScript for client-domain scripts) is
    owned upstream by the transpile / classify_script_type pass.

    What this test now asserts: given a LocalScript (as the
    upstream would emit for a client-domain script), the topology
    decision tree routes it to StarterPlayerScripts.
    """
    from converter.scene_runtime_topology.module_domain import TopologyInputs

    s = _make_script(
        "CameraController",
        "local UIS = game:GetService('UserInputService')\nUIS.InputBegan:Connect(function() end)",
        script_type="LocalScript",
    )
    inputs: TopologyInputs = {
        "domains": {"g-cam": "client"},
        "reachability_requirements": {},
        "lifecycle_roles": {},
        "script_id_by_name": {"CameraController": "g-cam"},
        "caller_graph": {},
        "transpile_ran": True,
    }
    plan = classify_storage([s], topology_inputs=inputs)

    assert s.parent_path == STARTER_PLAYER_SCRIPTS
    assert s.script_type == "LocalScript"
    assert s.name in plan.client_scripts


def test_server_only_api_stays_server():
    s = _make_script(
        "ServerManager",
        "local DSS = game:GetService('DataStoreService')\nlocal store = DSS:GetDataStore('x')",
    )
    plan = classify_storage([s])

    assert s.parent_path == SERVER_SCRIPT_SERVICE
    assert s.script_type == "Script"
    assert s.name in plan.server_scripts


def test_default_server_script_without_api_hints():
    s = _make_script("GameManager", "local x = 1\nprint(x)")
    plan = classify_storage([s])

    assert s.parent_path == SERVER_SCRIPT_SERVICE
    assert s.name in plan.server_scripts


# ---------------------------------------------------------------------------
# Module call graph routing
# ---------------------------------------------------------------------------


def test_module_required_by_local_script_goes_replicated():
    client = _make_script(
        "ClientUI",
        'local Players = game:GetService("Players")\n'
        'local lp = Players.LocalPlayer\n'
        'local Helper = require(game:GetService("ReplicatedStorage"):FindFirstChild("Helper"))',
        script_type="LocalScript",
    )
    helper = _make_script(
        "Helper",
        "local Helper = {}\nfunction Helper.run() end\nreturn Helper",
        script_type="ModuleScript",
    )
    plan = classify_storage([client, helper])

    assert helper.parent_path == REPLICATED_STORAGE
    assert helper.name in plan.shared_modules


def test_module_required_only_by_server_goes_server_storage():
    server = _make_script(
        "ServerLogic",
        'local DSS = game:GetService("DataStoreService")\n'
        'local Utility = require(game:GetService("ServerStorage"):FindFirstChild("Utility"))',
    )
    utility = _make_script(
        "Utility",
        "local Utility = {}\nreturn Utility",
        script_type="ModuleScript",
    )
    plan = classify_storage([server, utility])

    assert utility.parent_path == SERVER_STORAGE
    assert utility.name in plan.server_modules


def test_module_required_from_both_defaults_replicated():
    client = _make_script(
        "ClientA",
        'local Players = game:GetService("Players")\nlocal lp = Players.LocalPlayer\n'
        'local Shared = require(game.ReplicatedStorage.Shared)',
        script_type="LocalScript",
    )
    server = _make_script(
        "ServerA",
        'local DSS = game:GetService("DataStoreService")\n'
        'local Shared = require(game.ServerStorage.Shared)',
    )
    shared = _make_script(
        "Shared",
        "local Shared = {}\nreturn Shared",
        script_type="ModuleScript",
    )
    plan = classify_storage([client, server, shared])

    assert shared.parent_path == REPLICATED_STORAGE
    assert shared.name in plan.shared_modules


def test_orphan_module_defaults_replicated_storage():
    lonely = _make_script(
        "UnusedHelper",
        "local M = {}\nreturn M",
        script_type="ModuleScript",
    )
    plan = classify_storage([lonely])

    assert lonely.parent_path == REPLICATED_STORAGE
    assert lonely.name in plan.shared_modules


# ---------------------------------------------------------------------------
# ReplicatedFirst / character scripts
# ---------------------------------------------------------------------------


def test_loading_script_name_hint_goes_replicated_first():
    loader = _make_script(
        "LoadingScreen",
        "print('loading...')",
        script_type="Script",
    )
    plan = classify_storage([loader])

    assert loader.parent_path == REPLICATED_FIRST
    assert loader.name in plan.replicated_first_scripts


def test_character_attached_script_goes_character_scripts():
    char = _make_script("CharacterController", "print('char')", script_type="Script")
    plan = classify_storage([char], character_script_names=["CharacterController"])

    assert char.parent_path == STARTER_CHARACTER_SCRIPTS
    assert char.name in plan.character_scripts


# ---------------------------------------------------------------------------
# RemoteEvents
# ---------------------------------------------------------------------------


def test_remote_event_names_collected():
    server = _make_script(
        "ServerListener",
        'local re = game.ReplicatedStorage:WaitForChild("FireWeapon")\n'
        're.OnServerEvent:Connect(function() end)',
    )
    client = _make_script(
        "ClientSender",
        'local re = game.ReplicatedStorage:FindFirstChild("FireWeapon")\n'
        're:FireServer()',
        script_type="LocalScript",
    )
    plan = classify_storage([server, client])

    assert "FireWeapon" in plan.remote_events


def test_non_remote_name_not_harvested_as_remote():
    # A :FindFirstChild that isn't accompanied by any RemoteEvent signatures
    # should NOT end up in plan.remote_events.
    s = _make_script(
        "Setup",
        'local hud = game.StarterGui:FindFirstChild("HUD")',
        script_type="LocalScript",
    )
    plan = classify_storage([s])

    assert "HUD" not in plan.remote_events


# ---------------------------------------------------------------------------
# Call-graph source scan (no dependency_map provided)
# ---------------------------------------------------------------------------


def test_source_scan_builds_call_graph_without_dependency_map():
    caller = _make_script(
        "Caller",
        'local Players = game:GetService("Players")\nlocal lp = Players.LocalPlayer\n'
        'local Target = require(script.Parent.Target)',
        script_type="LocalScript",
    )
    target = _make_script(
        "Target",
        "local Target = {}\nreturn Target",
        script_type="ModuleScript",
    )
    plan = classify_storage([caller, target])

    # Target is reachable from a LocalScript via `require(...Target)` → Replicated.
    assert target.parent_path == REPLICATED_STORAGE


# ---------------------------------------------------------------------------
# Templates (forward-looking)
# ---------------------------------------------------------------------------


def test_template_referenced_by_client_goes_replicated():
    client = _make_script("ClientSpawner", "-- spawner", script_type="LocalScript")
    plan = classify_storage(
        [client],
        template_names=["EnemyPrefab"],
        template_spawn_callers={"EnemyPrefab": ["ClientSpawner"]},
    )
    # The client script is in client_touchers only if it uses client APIs.
    # Without client API usage, we default to replicated anyway.
    assert "EnemyPrefab" in plan.replicated_templates


def test_template_with_server_secret_name_hint_goes_server_storage():
    plan = classify_storage(
        [],
        template_names=["AdminToolPrefab", "SecretLoot"],
    )
    assert "AdminToolPrefab" in plan.server_templates
    assert "SecretLoot" in plan.server_templates


def test_template_unknown_callers_defaults_replicated():
    plan = classify_storage(
        [],
        template_names=["MysteryPrefab"],
    )
    assert "MysteryPrefab" in plan.replicated_templates


# ---------------------------------------------------------------------------
# StoragePlan serialization
# ---------------------------------------------------------------------------


def test_plan_is_json_serializable():
    import json
    s = _make_script("X", "print('x')")
    plan = classify_storage([s])

    dumped = json.dumps(plan.to_dict())
    revived = json.loads(dumped)
    assert "server_scripts" in revived
    assert "decisions" in revived


def test_plan_decisions_record_reason():
    """Phase 2a slice 7 (2026-05-30): rewritten to assert the
    decision REASON is recorded in plan.decisions. The original
    "client-only" substring was an artifact of the regex-API
    detector's reason string, which slice 7 deleted; assert the
    structural presence of the reason field instead.
    """
    s = _make_script(
        "MouseThing",
        "local mouse = game:GetService('Players').LocalPlayer:GetMouse()\n"
        "print(mouse.Hit)",
    )
    plan = classify_storage([s])

    reasons = {d["script"]: d["reason"] for d in plan.decisions}
    # The reason is recorded; legacy fallback (no topology_inputs)
    # routes a Script with no character / loader / module signal
    # to ServerScriptService with a "server Script (default)"
    # reason.
    assert reasons["MouseThing"] is not None
    assert "default" in reasons["MouseThing"] or "Script" in reasons["MouseThing"]


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_classifier_is_idempotent():
    s = _make_script("Mod", "local M = {}\nreturn M", script_type="ModuleScript")
    plan1 = classify_storage([s])
    first_path = s.parent_path

    plan2 = classify_storage([s])
    assert s.parent_path == first_path
    assert plan2.shared_modules == plan1.shared_modules


# ---------------------------------------------------------------------------
# LocalScript-in-SSS correction
# ---------------------------------------------------------------------------


def test_local_script_without_client_api_still_routed_to_starter():
    # A script typed as LocalScript but without detectable client API usage
    # must still go to StarterPlayerScripts — not ServerScriptService, which
    # would silently not run.
    s = _make_script("MysteryLocal", "print('hi')", script_type="LocalScript")
    plan = classify_storage([s])

    assert s.parent_path == STARTER_PLAYER_SCRIPTS
    assert s.name in plan.client_scripts


# ---------------------------------------------------------------------------
# Slice 7: topology-driven decision tree
# ---------------------------------------------------------------------------


def _mk_topology_inputs(**overrides) -> dict:
    """Phase 2a slice 7 test helper. Returns a ``TopologyInputs``-shaped
    dict with empty defaults; override specific fields per test.

    Returned as a plain ``dict`` (not a TypedDict cast) to keep the
    helper simple — the consumer reads it through ``__getitem__`` /
    ``.get``, both of which work on plain dicts.
    """
    base = {
        "domains": {},
        "reachability_requirements": {},
        "lifecycle_roles": {},
        "script_id_by_name": {},
        "caller_graph": {},
        "transpile_ran": True,
    }
    base.update(overrides)
    return base


class TestSlice7TopologyDecisionTree:
    """Phase 2a slice 7: ``_decide_script_container_from_topology``
    consumes ``topology_inputs`` and routes each script per the
    locked precedence tree.
    """

    def test_lifecycle_role_character_attached_wins_over_all(self) -> None:
        """Rule 1: ``character_attached`` precedence is highest. Even
        a Script with a reachability requirement of RS gets routed to
        StarterCharacterScripts because the Roblox character-mount
        semantic is a structural fact reachability cannot override.
        """
        s = _make_script("Hud", "print('hud')", script_type="Script")
        inputs = _mk_topology_inputs(
            lifecycle_roles={"g-hud": "character_attached"},
            # Reachability would otherwise route to RS -- gets overridden.
            reachability_requirements={"g-hud": REPLICATED_STORAGE},
            script_id_by_name={"Hud": "g-hud"},
        )
        plan = classify_storage([s], topology_inputs=inputs)
        assert s.parent_path == STARTER_CHARACTER_SCRIPTS
        assert s.name in plan.character_scripts

    def test_lifecycle_role_loader_routes_to_replicated_first(self) -> None:
        """Rule 2: ``loader`` lifecycle_role -> ReplicatedFirst. Beats
        reachability because loaders are pre-replication-bootstrap
        scripts; the container is a structural Roblox requirement.
        """
        s = _make_script("Boot", "print('boot')", script_type="Script")
        inputs = _mk_topology_inputs(
            lifecycle_roles={"g-boot": "loader"},
            script_id_by_name={"Boot": "g-boot"},
        )
        plan = classify_storage([s], topology_inputs=inputs)
        assert s.parent_path == REPLICATED_FIRST
        assert s.name in plan.replicated_first_scripts

    def test_reachability_required_container_honored(self) -> None:
        """Rule 3: a ModuleScript with
        ``reachability_requirements[sid] == REPLICATED_STORAGE`` lands
        there even when caller_graph would route it elsewhere.
        """
        helper = _make_script(
            "Helper", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            reachability_requirements={"g-helper": REPLICATED_STORAGE},
            script_id_by_name={"Helper": "g-helper"},
            # Caller-graph signal would route to SS (server-only caller)
            # but reachability takes precedence.
            domains={"g-server-caller": "server"},
            caller_graph={"g-helper": ["g-server-caller"]},
        )
        plan = classify_storage([helper], topology_inputs=inputs)
        assert helper.parent_path == REPLICATED_STORAGE
        assert helper.name in plan.shared_modules

    def test_reachability_excluded_sentinel_routes_to_replicated_storage(
        self,
    ) -> None:
        """Rule 3 sentinel: ``"__excluded__"`` (helper reached by BOTH
        client and server require-graphs) hoists to ReplicatedStorage
        with the ``reachability_conflict`` reason.
        """
        helper = _make_script(
            "Helper", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            reachability_requirements={"g-helper": "__excluded__"},
            script_id_by_name={"Helper": "g-helper"},
        )
        plan = classify_storage([helper], topology_inputs=inputs)
        assert helper.parent_path == REPLICATED_STORAGE
        reasons = {d["script"]: d["reason"] for d in plan.decisions}
        assert "reachability_conflict" in reasons["Helper"]

    def test_module_with_client_caller_goes_replicated(self) -> None:
        """Rule 4 (client branch): any client-domain caller routes the
        ModuleScript to ReplicatedStorage (cross-process reach)."""
        helper = _make_script(
            "Helper", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            script_id_by_name={"Helper": "g-helper"},
            domains={"g-client": "client", "g-server": "server"},
            caller_graph={"g-helper": ["g-client", "g-server"]},
        )
        plan = classify_storage([helper], topology_inputs=inputs)
        assert helper.parent_path == REPLICATED_STORAGE

    def test_module_with_server_only_callers_goes_server_storage(
        self,
    ) -> None:
        """Rule 4 (server-only branch): all callers server-domain ->
        ServerStorage. Slice 7 trusts the analysis -- if topology says
        server-only, the module lands in ServerStorage. Tighter
        security surface than RS; design-doc decision after Codex
        empirical verification 2026-05-29.
        """
        helper = _make_script(
            "Helper", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            script_id_by_name={"Helper": "g-helper"},
            domains={"g-s1": "server", "g-s2": "server"},
            caller_graph={"g-helper": ["g-s1", "g-s2"]},
        )
        plan = classify_storage([helper], topology_inputs=inputs)
        assert helper.parent_path == SERVER_STORAGE
        assert helper.name in plan.server_modules

    def test_module_orphan_defaults_replicated_storage(self) -> None:
        """Rule 4 (orphan): no callers -> RS (conservative)."""
        helper = _make_script(
            "Helper", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            script_id_by_name={"Helper": "g-helper"},
            domains={},
            caller_graph={},
        )
        plan = classify_storage([helper], topology_inputs=inputs)
        assert helper.parent_path == REPLICATED_STORAGE
        assert helper.name in plan.shared_modules

    def test_module_with_helper_only_callers_defaults_replicated(
        self,
    ) -> None:
        """Rule 4 (no client/server signal): callers exist but none
        is client OR server domain -> RS default. Matches the
        "client/server unknown: default replicated" branch."""
        helper = _make_script(
            "Helper", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            script_id_by_name={"Helper": "g-helper"},
            domains={"g-helper-caller": "helper"},
            caller_graph={"g-helper": ["g-helper-caller"]},
        )
        plan = classify_storage([helper], topology_inputs=inputs)
        assert helper.parent_path == REPLICATED_STORAGE

    def test_local_script_goes_starter_player_scripts(self) -> None:
        """Rule 5: a LocalScript with no other signals lands in
        StarterPlayerScripts."""
        s = _make_script("Camera", "print('cam')", script_type="LocalScript")
        inputs = _mk_topology_inputs(
            script_id_by_name={"Camera": "g-cam"},
            domains={"g-cam": "client"},
        )
        plan = classify_storage([s], topology_inputs=inputs)
        assert s.parent_path == STARTER_PLAYER_SCRIPTS
        assert s.name in plan.client_scripts

    def test_script_goes_server_script_service(self) -> None:
        """Rule 6: a Script with no other signals lands in
        ServerScriptService."""
        s = _make_script("World", "print('world')", script_type="Script")
        inputs = _mk_topology_inputs(
            script_id_by_name={"World": "g-world"},
            domains={"g-world": "server"},
        )
        plan = classify_storage([s], topology_inputs=inputs)
        assert s.parent_path == SERVER_SCRIPT_SERVICE
        assert s.name in plan.server_scripts


class TestSlice7FallbackGates:
    """Phase 2a slice 7: the topology-path gates the legacy fallback
    in three narrow cases.
    """

    def test_script_id_by_name_miss_falls_back_to_legacy(self) -> None:
        """Codex P1.2: when ``script_id_by_name.get(s.name) is None``
        (degraded-service contract on stem/class_name collisions),
        slice 7 MUST fall back to legacy six-rule per-script.
        """
        s = _make_script(
            "Mystery", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            # Intentionally don't include Mystery in script_id_by_name.
            script_id_by_name={},
        )
        plan = classify_storage([s], topology_inputs=inputs)
        # Legacy path: orphan module -> RS.
        assert s.parent_path == REPLICATED_STORAGE

    def test_unconstrained_helper_fallback_on_no_transpile_resume(
        self,
    ) -> None:
        """Codex amendment 1: on assemble-no-retranspile resume
        (``transpile_ran is False``), a ModuleScript not present in
        ``reachability_requirements`` falls back to legacy per-script.
        This preserves slice-5 byte-identical resume behavior for
        unconstrained helpers.
        """
        helper = _make_script(
            "Helper", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            script_id_by_name={"Helper": "g-helper"},
            reachability_requirements={},  # empty - unconstrained
            transpile_ran=False,           # no-transpile resume signal
        )
        plan = classify_storage([helper], topology_inputs=inputs)
        # Legacy path: orphan module -> RS.
        assert helper.parent_path == REPLICATED_STORAGE

    def test_genuine_unconstrained_helper_uses_topology_when_transpile_ran(
        self,
    ) -> None:
        """Counterpart to the previous test: when ``transpile_ran is
        True`` and the helper is genuinely unconstrained (analysis
        produced no reachability requirement), the topology path
        APPLIES -- it routes via the caller_graph / domains.
        """
        helper = _make_script(
            "Helper", "return {}", script_type="ModuleScript",
        )
        inputs = _mk_topology_inputs(
            script_id_by_name={"Helper": "g-helper"},
            reachability_requirements={},  # empty - genuinely unconstrained
            transpile_ran=True,
            # No callers -> topology routes via orphan branch.
            caller_graph={},
        )
        plan = classify_storage([helper], topology_inputs=inputs)
        assert helper.parent_path == REPLICATED_STORAGE
        # Reason carries the "topology:" prefix -- proves topology
        # path ran, not legacy.
        reasons = {d["script"]: d["reason"] for d in plan.decisions}
        assert reasons["Helper"].startswith("topology:")

    def test_non_module_script_uses_topology_even_when_transpile_ran_false(
        self,
    ) -> None:
        """The unconstrained-helper fallback gate is ModuleScript-only.
        A Script / LocalScript with ``transpile_ran=False`` still
        routes via the topology tree (Script -> SSS, LocalScript ->
        SPS). The fallback exists for helpers whose reachability
        cannot be recomputed on resume; Script class-driven routing
        is reachability-independent.
        """
        s = _make_script("Boot", "print()", script_type="Script")
        inputs = _mk_topology_inputs(
            script_id_by_name={"Boot": "g-boot"},
            transpile_ran=False,
        )
        plan = classify_storage([s], topology_inputs=inputs)
        assert s.parent_path == SERVER_SCRIPT_SERVICE
        reasons = {d["script"]: d["reason"] for d in plan.decisions}
        assert reasons["Boot"].startswith("topology:")


class TestSlice7HardConstraints:
    """Phase 2a slice 7: ``_enforce_hard_constraints`` is a
    defense-in-depth post-validator. The decision tree + in-flow
    corrections normally prevent the constraint violations, but the
    validator raises if a future edit slips them through.
    """

    def test_local_script_in_sss_violates(self) -> None:
        """LocalScript in ServerScriptService would silently not run.
        The validator raises ``ConstraintViolation``.
        """
        s = _make_script("X", "print()", script_type="LocalScript")
        with pytest.raises(ConstraintViolation, match="LocalScript"):
            _enforce_hard_constraints(s, SERVER_SCRIPT_SERVICE)

    def test_module_in_replicated_first_violates(self) -> None:
        """ReplicatedFirst is for executable scripts; ModuleScripts
        there are inert. The validator raises ``ConstraintViolation``.
        """
        s = _make_script("X", "return {}", script_type="ModuleScript")
        with pytest.raises(ConstraintViolation, match="ModuleScript"):
            _enforce_hard_constraints(s, REPLICATED_FIRST)

    def test_legal_pairs_do_not_raise(self) -> None:
        """Spot-check the non-violating pairs."""
        legal_pairs = [
            ("Script", SERVER_SCRIPT_SERVICE),
            ("Script", SERVER_STORAGE),
            ("LocalScript", STARTER_PLAYER_SCRIPTS),
            ("LocalScript", STARTER_CHARACTER_SCRIPTS),
            ("ModuleScript", REPLICATED_STORAGE),
            ("ModuleScript", SERVER_STORAGE),
            ("ModuleScript", SERVER_SCRIPT_SERVICE),
            ("Script", REPLICATED_FIRST),
            ("LocalScript", REPLICATED_FIRST),
        ]
        for st, container in legal_pairs:
            s = _make_script("X", "print()", script_type=st)
            _enforce_hard_constraints(s, container)  # must not raise

    def test_in_flow_correction_prevents_local_script_in_sss(self) -> None:
        """classify_storage's in-flow correction moves a LocalScript
        out of SSS BEFORE the validator runs. The flow is:
        decision -> Script container == SSS -> auto-flip to SPS ->
        validator OK.
        """
        # Provide a LocalScript with no topology signal; legacy path
        # routes it to SPS directly. This test is here to demonstrate
        # the validator isn't triggered on the well-trodden path.
        s = _make_script("X", "print()", script_type="LocalScript")
        plan = classify_storage([s])
        assert s.parent_path == STARTER_PLAYER_SCRIPTS
