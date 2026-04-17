"""Tests: preserved-scripts rehydration reads conversion_plan.json."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_pipeline(tmp_path):
    from converter.pipeline import Pipeline
    from core.roblox_types import RbxPlace

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir(parents=True)
    pipeline = Pipeline(unity_project_path=project, output_dir=output, skip_upload=True)
    pipeline.state.rbx_place = RbxPlace()
    return pipeline


def _write_plan(pipeline, storage_plan):
    plan_path = pipeline.output_dir / "conversion_plan.json"
    plan_path.write_text(json.dumps({"storage_plan": storage_plan}))
    return plan_path


def test_load_storage_plan_missing_file_returns_empty(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    assert pipeline._load_storage_plan_for_rehydration() == {}


def test_load_storage_plan_malformed_file_returns_empty(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    (pipeline.output_dir / "conversion_plan.json").write_text("not valid json {{{")
    assert pipeline._load_storage_plan_for_rehydration() == {}


def test_load_storage_plan_returns_category_lookup(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    _write_plan(pipeline, {
        "server_scripts": ["GameManager"],
        "client_scripts": ["InputHandler"],
        "character_scripts": ["PlayerMove"],
        "replicated_first_scripts": ["Loading"],
        "shared_modules": ["Constants"],
        "server_modules": ["Secrets"],
    })

    lookup = pipeline._load_storage_plan_for_rehydration()
    assert lookup["GameManager"] == ("Script", "ServerScriptService")
    assert lookup["InputHandler"] == ("LocalScript", "StarterPlayer.StarterPlayerScripts")
    assert lookup["PlayerMove"] == ("LocalScript", "StarterPlayer.StarterCharacterScripts")
    assert lookup["Loading"] == ("ModuleScript", "ReplicatedFirst")
    assert lookup["Constants"] == ("ModuleScript", "ReplicatedStorage")
    assert lookup["Secrets"] == ("ModuleScript", "ServerStorage")


def test_rehydration_uses_plan_over_heuristic(tmp_path):
    """Heuristic would flag the `\\nreturn` substring as ModuleScript; plan wins."""
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "GameManager.luau").write_text(
        'local x = "prefix\\nreturn foo"\nprint("hello")\n'
    )
    _write_plan(pipeline, {"server_scripts": ["GameManager"]})

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    script = pipeline.state.rbx_place.scripts[0]
    assert script.name == "GameManager"
    assert script.script_type == "Script"


def test_rehydration_sets_parent_path_from_plan(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "InputHandler.luau").write_text("-- local script\n")
    _write_plan(pipeline, {"client_scripts": ["InputHandler"]})

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    script = pipeline.state.rbx_place.scripts[0]
    assert script.script_type == "LocalScript"
    assert getattr(script, "parent_path", None) == "StarterPlayer.StarterPlayerScripts"


def test_rehydration_falls_back_to_heuristic_for_unplanned_script(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "NewModule.luau").write_text("local M = {}\nreturn M\n")
    # Plan covers a different script only.
    _write_plan(pipeline, {"server_scripts": ["SomethingElse"]})

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    script = pipeline.state.rbx_place.scripts[0]
    assert script.name == "NewModule"
    assert script.script_type == "ModuleScript"  # heuristic on `\nreturn `
    assert getattr(script, "parent_path", None) is None


def test_rehydration_picks_up_scriptable_objects_subdir(tmp_path):
    """Item 5 writes to scripts/scriptable_objects/; item 12 must find them."""
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    so_dir = scripts_dir / "scriptable_objects"
    so_dir.mkdir(parents=True)
    (so_dir / "Inventory.luau").write_text('local data = {}\nreturn data\n')

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    names = [s.name for s in pipeline.state.rbx_place.scripts]
    assert "Inventory" in names
    inv = next(s for s in pipeline.state.rbx_place.scripts if s.name == "Inventory")
    assert inv.script_type == "ModuleScript"
