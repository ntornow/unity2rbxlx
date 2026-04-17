"""Tests for the structured conversion_report.json shape."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_generate_report_produces_structured_shape(tmp_path):
    from converter.report_generator import (
        ConversionReport, AssetSummary, ScriptSummary, MaterialSummary,
        ComponentSummary, SceneSummary, OutputSummary, generate_report,
    )

    report = ConversionReport(
        unity_project_path="/unity/SimpleFPS",
        output_dir=str(tmp_path),
        success=True,
        errors=[],
        warnings=["one warning"],
        assets=AssetSummary(total=3, by_kind={"Script": 2, "ModuleScript": 1}),
        scripts=ScriptSummary(total=36, succeeded=36),
        materials=MaterialSummary(total=12, fully_converted=10),
        scene=SceneSummary(total_game_objects=500),
        components=ComponentSummary(converted=295),
        output=OutputSummary(
            rbxl_path=str(tmp_path / "converted_place.rbxlx"),
            parts_written=295,
            scripts_in_place=36,
            report_path=str(tmp_path / "conversion_report.json"),
        ),
    )

    out = tmp_path / "conversion_report.json"
    generate_report(report, out, print_summary=False)

    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))

    assert data["unity_project_path"] == "/unity/SimpleFPS"
    assert data["success"] is True
    assert data["assets"]["total"] == 3
    assert data["assets"]["by_kind"] == {"Script": 2, "ModuleScript": 1}
    assert data["scripts"]["total"] == 36
    assert data["scripts"]["succeeded"] == 36
    assert data["materials"]["total"] == 12
    assert data["materials"]["fully_converted"] == 10
    assert data["scene"]["total_game_objects"] == 500
    assert data["components"]["converted"] == 295
    assert data["output"]["parts_written"] == 295
    assert data["output"]["scripts_in_place"] == 36


def test_pipeline_build_conversion_report_reflects_ctx(tmp_path):
    """Pipeline._build_conversion_report mirrors ctx + rbx_place state."""
    from converter.pipeline import Pipeline
    from core.conversion_context import ConversionContext
    from core.roblox_types import RbxPlace, RbxScript

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    pipeline = Pipeline(unity_project_path=project, output_dir=tmp_path / "out", skip_upload=True)
    pipeline.ctx = ConversionContext(
        total_game_objects=500,
        converted_parts=295,
        transpiled_scripts=36,
        total_materials=12,
        converted_materials=10,
        uploaded_assets={"a": "rbxassetid://1", "b": "rbxassetid://2"},
        asset_upload_errors=["bad.png"],
        errors=[],
        warnings=["hi"],
    )
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="A", source="", script_type="Script"),
        RbxScript(name="B", source="", script_type="LocalScript"),
        RbxScript(name="C", source="", script_type="ModuleScript"),
    ]

    rbxlx = tmp_path / "place.rbxlx"
    report_path = tmp_path / "report.json"
    report = pipeline._build_conversion_report(
        rbxlx, {"parts_written": 295, "scripts_written": 36}, report_path,
    )

    assert report.success is True
    assert report.warnings == ["hi"]
    assert report.assets.total == 2
    assert report.assets.by_kind == {
        "Script": 1, "LocalScript": 1, "ModuleScript": 1, "upload_errors": 1,
    }
    assert report.scripts.total == 36
    assert report.materials.fully_converted == 10
    assert report.scene.total_game_objects == 500
    assert report.components.converted == 295
    assert report.output.parts_written == 295


def test_interactive_report_preserves_structured_fields(tmp_path):
    """Interactive report() augments the file without clobbering shape."""
    from converter.report_generator import (
        ConversionReport, OutputSummary, generate_report,
    )

    report = ConversionReport(
        unity_project_path="/unity/SimpleFPS",
        output_dir=str(tmp_path),
        success=True,
        output=OutputSummary(parts_written=42),
    )
    report_path = tmp_path / "conversion_report.json"
    generate_report(report, report_path, print_summary=False)

    data = json.loads(report_path.read_text(encoding="utf-8"))
    data.update({
        "selected_scene": "Assets/Scenes/main.unity",
        "completed_skill_phases": ["discover", "inventory"],
        "universe_id": 12345,
        "rbxlx_size_mb": 1.5,
    })
    report_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    merged = json.loads(report_path.read_text(encoding="utf-8"))
    assert merged["selected_scene"] == "Assets/Scenes/main.unity"
    assert merged["universe_id"] == 12345
    assert merged["output"]["parts_written"] == 42
    assert merged["success"] is True
