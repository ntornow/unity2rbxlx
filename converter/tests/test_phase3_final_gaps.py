"""Tests for Pipeline-level wiring of the Phase 3 items.

These cover the seams where the Pipeline integrates with the extracted
helpers (rehydration, report assembly, binary-writer skip, storage plan
emission). Unit-level tests for each helper live in their own files.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
    """MERGE_PLAN Phase 3 item 6: the interactive upload rebuild path must
    not produce a .rbxl file."""

    def test_default_runs_xml_to_binary(self, tmp_path):
        pipeline = _make_pipeline(tmp_path)
        assert pipeline.skip_binary_rbxl is False

    def test_flag_available_in_constructor(self, tmp_path):
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

    def test_interactive_upload_sets_skip_binary_rbxl(self, tmp_path):
        """convert_interactive.upload should toggle the flag on."""
        import convert_interactive as ci

        # Read the upload() source; look for the assignment that MERGE_PLAN
        # item 6 mandates. This is a static check — functionally exercising
        # the full upload() command requires Roblox API keys.
        src = Path(ci.__file__).read_text()
        assert "pipeline.skip_binary_rbxl = True" in src, (
            "convert_interactive.upload must set pipeline.skip_binary_rbxl = True"
        )


class TestReportIncludesSceneRelPath:
    """MERGE_PLAN Phase 3 item 7: structured report must include the
    project-relative scene path, not just an absolute one."""

    def test_scene_is_project_relative(self, tmp_path):
        from core.conversion_context import ConversionContext

        pipeline = _make_pipeline(tmp_path)
        # Point selected_scene at an absolute path inside the project.
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
    """MERGE_PLAN Phase 3 item 12: conversion_plan.json should record
    relative paths so rehydration can preserve directory identity."""

    def test_classify_storage_records_script_paths(self, tmp_path):
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
        assert "script_paths" in plan
        paths = plan["script_paths"]
        assert paths["PlayerController"] == "PlayerController.luau"
        assert paths["Door"] == "animations/Door.luau"
        assert paths["Inventory"] == "scriptable_objects/Inventory.luau"

    def test_classify_storage_handles_missing_scripts_dir(self, tmp_path):
        from core.roblox_types import RbxScript

        pipeline = _make_pipeline(tmp_path)
        # No scripts/ dir on disk.
        pipeline.state.rbx_place.scripts = [
            RbxScript(name="Foo", source="", script_type="Script"),
        ]
        pipeline._classify_storage()

        plan = json.loads(
            (pipeline.output_dir / "conversion_plan.json").read_text(encoding="utf-8"),
        )
        assert plan["script_paths"] == {}
