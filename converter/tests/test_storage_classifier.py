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
    StoragePlan,
    SERVER_SCRIPT_SERVICE,
    SERVER_STORAGE,
    REPLICATED_STORAGE,
    REPLICATED_FIRST,
    STARTER_PLAYER_SCRIPTS,
    STARTER_CHARACTER_SCRIPTS,
)


def _make_script(name: str, source: str, script_type: str = "Script") -> RbxScript:
    return RbxScript(name=name, source=source, script_type=script_type)


# ---------------------------------------------------------------------------
# Client vs server API detection
# ---------------------------------------------------------------------------


def test_client_only_api_forces_local_script():
    s = _make_script(
        "CameraController",
        "local UIS = game:GetService('UserInputService')\nUIS.InputBegan:Connect(function() end)",
    )
    plan = classify_storage([s])

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
    s = _make_script(
        "MouseThing",
        "local mouse = game:GetService('Players').LocalPlayer:GetMouse()\n"
        "print(mouse.Hit)",
    )
    plan = classify_storage([s])

    reasons = {d["script"]: d["reason"] for d in plan.decisions}
    assert "client-only" in reasons["MouseThing"]


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
