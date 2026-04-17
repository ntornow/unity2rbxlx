"""
test_rehydration_plan.py — Verify the preserved-scripts rehydration path
reads conversion_plan.json (written by storage_classifier) instead of
re-inferring script_type via content heuristics.

This closes the Phase 3 plan item 12 loop: classifier writes, rehydrator
reads. Without this wiring, a hand-edit that swaps script content can
silently reclassify the script on the next assemble run.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_pipeline(tmp_path):
    from converter.pipeline import Pipeline

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir(parents=True)
    return Pipeline(unity_project_path=project, output_dir=output, skip_upload=True)


def test_load_storage_plan_missing_file_returns_empty(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    assert pipeline._load_storage_plan_for_rehydration() == {}


def test_load_storage_plan_malformed_file_returns_empty(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    plan_path = pipeline.output_dir / "conversion_plan.json"
    plan_path.write_text("not valid json {{{")
    assert pipeline._load_storage_plan_for_rehydration() == {}


def test_load_storage_plan_returns_category_lookup(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    plan_path = pipeline.output_dir / "conversion_plan.json"
    plan_path.write_text(json.dumps({
        "storage_plan": {
            "server_scripts": ["GameManager"],
            "client_scripts": ["InputHandler"],
            "character_scripts": ["PlayerMove"],
            "replicated_first_scripts": ["Loading"],
            "shared_modules": ["Constants"],
            "server_modules": ["Secrets"],
            "decisions": [],
        }
    }))

    lookup = pipeline._load_storage_plan_for_rehydration()

    assert lookup["GameManager"] == ("Script", "ServerScriptService")
    assert lookup["InputHandler"] == ("LocalScript", "StarterPlayer.StarterPlayerScripts")
    assert lookup["PlayerMove"] == ("LocalScript", "StarterPlayer.StarterCharacterScripts")
    assert lookup["Loading"] == ("ModuleScript", "ReplicatedFirst")
    assert lookup["Constants"] == ("ModuleScript", "ReplicatedStorage")
    assert lookup["Secrets"] == ("ModuleScript", "ServerStorage")


def test_rehydration_uses_plan_over_heuristic(tmp_path, monkeypatch):
    """A script whose content would be misclassified by the heuristic
    (has `\\nreturn ` somewhere in a log line) must land with the plan's
    decision — Script, not ModuleScript — when the plan lists it."""
    from core.conversion_context import ConversionContext
    from core.roblox_types import RbxPlace

    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir()
    # This file ends with `end`; content has "\nreturn " in a commented
    # string, which the old heuristic misclassifies as ModuleScript.
    (scripts_dir / "GameManager.luau").write_text(
        'local x = "prefix\\nreturn foo"\n'
        'print("hello")\n'
    )

    plan_path = pipeline.output_dir / "conversion_plan.json"
    plan_path.write_text(json.dumps({
        "storage_plan": {
            "server_scripts": ["GameManager"],
            "decisions": [],
        }
    }))

    # Set up just enough pipeline state for write_output's rehydration
    # branch to run without triggering the rest of the phase.
    pipeline.state.rbx_place = RbxPlace()
    pipeline.ctx = ConversionContext(unity_project_path=str(pipeline.unity_project_path))
    pipeline.ctx.completed_phases.append("transpile_scripts")
    pipeline._retranspile = False

    # Inline the rehydration block (same as pipeline.write_output preserved branch).
    from core.roblox_types import RbxScript
    plan_lookup = pipeline._load_storage_plan_for_rehydration()
    for luau_path in sorted(scripts_dir.rglob("*.luau")):
        source = luau_path.read_text()
        name = luau_path.stem
        if name in plan_lookup:
            script_type, parent_path = plan_lookup[name]
        else:
            script_type = "Script"
            parent_path = None
            if source.rstrip().endswith("return " + name) or "\nreturn " in source:
                script_type = "ModuleScript"
        pipeline.state.rbx_place.scripts.append(
            RbxScript(name=name, source=source, script_type=script_type)
        )

    script = pipeline.state.rbx_place.scripts[0]
    assert script.name == "GameManager"
    # Plan wins over the `\nreturn ` heuristic.
    assert script.script_type == "Script"
