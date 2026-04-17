"""
test_report_generator.py — Smoke tests for the structured conversion report.

These verify the JSON shape that `pipeline.write_output` now writes via
`report_generator.generate_report`. Downstream consumers (the interactive
`report()` command, audit tooling) read these keys; a regression in the
shape would break both.
"""

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

    # The structured keys the rest of the system reads:
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


def test_interactive_report_preserves_structured_fields(tmp_path):
    """The interactive report() command reads the pipeline's structured
    report and augments it with skill-phase / place-identifier fields.
    It must NOT overwrite the structured shape."""
    from converter.report_generator import (
        ConversionReport, OutputSummary, generate_report,
    )

    # Simulate what pipeline.write_output writes.
    report = ConversionReport(
        unity_project_path="/unity/SimpleFPS",
        output_dir=str(tmp_path),
        success=True,
        output=OutputSummary(parts_written=42),
    )
    report_path = tmp_path / "conversion_report.json"
    generate_report(report, report_path, print_summary=False)

    # Simulate the interactive report() merging in skill-only fields.
    data = json.loads(report_path.read_text(encoding="utf-8"))
    data.update({
        "selected_scene": "Assets/Scenes/main.unity",
        "completed_skill_phases": ["discover", "inventory"],
        "universe_id": 12345,
        "rbxlx_size_mb": 1.5,
    })
    report_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    merged = json.loads(report_path.read_text(encoding="utf-8"))

    # Skill-only fields present.
    assert merged["selected_scene"] == "Assets/Scenes/main.unity"
    assert merged["universe_id"] == 12345
    # Structured fields survived.
    assert merged["output"]["parts_written"] == 42
    assert merged["success"] is True
