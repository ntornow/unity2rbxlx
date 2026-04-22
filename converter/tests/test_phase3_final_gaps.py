"""Pipeline-level wiring tests for the three closed Phase 3 gaps."""

from __future__ import annotations

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


class TestSkipBinaryRbxl:
    def test_default_is_false(self, tmp_path):
        pipeline = _make_pipeline(tmp_path)
        assert pipeline.skip_binary_rbxl is False

    def test_constructor_kwarg(self, tmp_path):
        from converter.pipeline import Pipeline

        project = tmp_path / "proj"
        (project / "Assets").mkdir(parents=True)
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path / "out",
            skip_upload=True,
            skip_binary_rbxl=True,
        )
        assert pipeline.skip_binary_rbxl is True

    def test_interactive_make_pipeline_threads_flag(self, tmp_path):
        """_make_pipeline must propagate skip_binary_rbxl so convert_interactive.upload
        can turn off the binary writer on the rebuild path."""
        from convert_interactive import _make_pipeline as cli_make_pipeline

        project = tmp_path / "proj"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "out"

        pipeline = cli_make_pipeline(project, out, skip_binary_rbxl=True)
        assert pipeline.skip_binary_rbxl is True

        default = cli_make_pipeline(project, out)
        assert default.skip_binary_rbxl is False


class TestReportIncludesSceneRelPath:
    def test_scene_is_project_relative(self, tmp_path):
        from core.conversion_context import ConversionContext

        pipeline = _make_pipeline(tmp_path)
        scene_abs = pipeline.unity_project_path / "Assets" / "Scenes" / "main.unity"
        pipeline.ctx = ConversionContext(
            unity_project_path=str(pipeline.unity_project_path),
            selected_scene=str(scene_abs),
        )
        report = pipeline._build_conversion_report(
            tmp_path / "place.rbxlx", {}, tmp_path / "report.json",
        )
        assert report.scene.selected_scene == "Assets/Scenes/main.unity"

    def test_scene_outside_project_falls_back_to_name(self, tmp_path):
        from core.conversion_context import ConversionContext

        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx = ConversionContext(
            unity_project_path=str(pipeline.unity_project_path),
            selected_scene="/totally/unrelated/path/alien.unity",
        )
        report = pipeline._build_conversion_report(
            tmp_path / "place.rbxlx", {}, tmp_path / "report.json",
        )
        assert report.scene.selected_scene == "alien.unity"

    def test_empty_scene_leaves_field_empty(self, tmp_path):
        from core.conversion_context import ConversionContext

        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx = ConversionContext(
            unity_project_path=str(pipeline.unity_project_path),
            selected_scene="",
        )
        report = pipeline._build_conversion_report(
            tmp_path / "place.rbxlx", {}, tmp_path / "report.json",
        )
        assert report.scene.selected_scene == ""


class TestStoragePlanScriptPaths:
    def test_records_relative_script_paths(self, tmp_path):
        from core.roblox_types import RbxScript

        pipeline = _make_pipeline(tmp_path)
        scripts_dir = pipeline.output_dir / "scripts"
        (scripts_dir / "animations").mkdir(parents=True)
        (scripts_dir / "scriptable_objects").mkdir(parents=True)
        (scripts_dir / "PlayerController.luau").write_text("-- top-level\n")
        (scripts_dir / "animations" / "Door.luau").write_text("-- animated\n")
        (scripts_dir / "scriptable_objects" / "Inventory.luau").write_text(
            "local data = {}\nreturn data\n",
        )
        pipeline.state.rbx_place.scripts = [
            RbxScript(name="PlayerController", source="", script_type="Script"),
            RbxScript(name="Door", source="", script_type="Script"),
            RbxScript(name="Inventory", source="", script_type="ModuleScript"),
        ]

        pipeline._classify_storage()

        plan = json.loads(
            (pipeline.output_dir / "conversion_plan.json").read_text(encoding="utf-8"),
        )
        paths = plan["script_paths"]
        assert paths["PlayerController"] == "PlayerController.luau"
        assert paths["Door"] == "animations/Door.luau"
        assert paths["Inventory"] == "scriptable_objects/Inventory.luau"

    def test_missing_scripts_dir_is_empty_dict(self, tmp_path):
        from core.roblox_types import RbxScript

        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place.scripts = [
            RbxScript(name="Foo", source="", script_type="Script"),
        ]
        pipeline._classify_storage()

        plan = json.loads(
            (pipeline.output_dir / "conversion_plan.json").read_text(encoding="utf-8"),
        )
        assert plan["script_paths"] == {}
