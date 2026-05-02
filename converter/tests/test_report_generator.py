"""
test_report_generator.py -- Unit tests for the structured conversion report generator.
"""

from __future__ import annotations

import json

import pytest

from converter.report_generator import (
    AssetSummary,
    ComponentSummary,
    ConversionReport,
    MaterialSummary,
    OutputSummary,
    SceneSummary,
    ScriptSummary,
    augment_report,
    generate_report,
)


def _make_report(**overrides) -> ConversionReport:
    defaults = dict(
        unity_project_path="/fake/project",
        output_dir="/fake/output",
        duration_seconds=12.5,
        success=True,
        errors=[],
        warnings=["texture missing"],
        assets=AssetSummary(total=50, by_kind={"texture": 30, "mesh": 20}),
        scripts=ScriptSummary(total=10, succeeded=8, flagged_for_review=2,
                              flagged_scripts=["PlayerController", "EnemyAI"]),
        materials=MaterialSummary(total=25, fully_converted=20,
                                  partially_converted=3, unconvertible=2),
        scene=SceneSummary(total_game_objects=100, prefabs_parsed=15,
                           prefab_instances_resolved=45),
        components=ComponentSummary(total_encountered=200, converted=180,
                                    dropped=20, dropped_by_type={"Cloth": 5, "WindZone": 15}),
        output=OutputSummary(rbxl_path="/fake/output/place.rbxlx",
                             parts_written=80, scripts_in_place=10,
                             report_path="/fake/output/report.json"),
    )
    defaults.update(overrides)
    return ConversionReport(**defaults)


class TestGenerateReport:
    def test_writes_json_file(self, tmp_path):
        report = _make_report()
        out = tmp_path / "report.json"
        result = generate_report(report, out, print_summary=False)
        assert result.exists()
        data = json.loads(result.read_text(encoding="utf-8"))
        assert data["success"] is True

    def test_all_fields_present(self, tmp_path):
        report = _make_report()
        out = tmp_path / "report.json"
        generate_report(report, out, print_summary=False)
        data = json.loads(out.read_text(encoding="utf-8"))
        for key in ("generated_at", "unity_project_path", "assets", "scripts",
                    "materials", "scene", "components", "output"):
            assert key in data

    def test_asset_summary_serialized(self, tmp_path):
        report = _make_report()
        out = tmp_path / "report.json"
        generate_report(report, out, print_summary=False)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["assets"]["total"] == 50
        assert data["assets"]["by_kind"]["texture"] == 30

    def test_verbose_false_strips_flagged_scripts(self, tmp_path):
        report = _make_report()
        out = tmp_path / "report.json"
        generate_report(report, out, verbose=False, print_summary=False)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "flagged_scripts" not in data["scripts"]

    def test_verbose_true_includes_flagged_scripts(self, tmp_path):
        report = _make_report()
        out = tmp_path / "report.json"
        generate_report(report, out, verbose=True, print_summary=False)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "flagged_scripts" in data["scripts"]
        assert "PlayerController" in data["scripts"]["flagged_scripts"]

    def test_empty_report(self, tmp_path):
        report = ConversionReport()
        out = tmp_path / "report.json"
        generate_report(report, out, print_summary=False)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["success"] is True
        assert data["assets"]["total"] == 0

    def test_failed_report(self, tmp_path):
        report = _make_report(success=False, errors=["Parse failed"])
        out = tmp_path / "report.json"
        generate_report(report, out, print_summary=False)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["success"] is False
        assert "Parse failed" in data["errors"]


class TestReportSummaryPrinting:
    def test_print_summary_true(self, tmp_path, capsys):
        report = _make_report()
        out = tmp_path / "report.json"
        generate_report(report, out, print_summary=True)
        captured = capsys.readouterr()
        assert "Conversion Report" in captured.out
        assert "SUCCESS" in captured.out

    def test_print_summary_false(self, tmp_path, capsys):
        report = _make_report()
        out = tmp_path / "report.json"
        generate_report(report, out, print_summary=False)
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_print_dropped_components(self, tmp_path, capsys):
        report = _make_report()
        out = tmp_path / "report.json"
        generate_report(report, out, print_summary=True)
        captured = capsys.readouterr()
        assert "Cloth" in captured.out
        assert "WindZone" in captured.out

    def test_print_failed_status(self, tmp_path, capsys):
        report = _make_report(success=False, errors=["Boom"])
        out = tmp_path / "report.json"
        generate_report(report, out, print_summary=True)
        captured = capsys.readouterr()
        assert "FAILED" in captured.out

    def test_creates_parent_dirs(self, tmp_path):
        report = _make_report()
        out = tmp_path / "sub" / "dir" / "report.json"
        generate_report(report, out, print_summary=False)
        assert out.exists()


class TestAugmentReport:
    """augment_report centralises the 'read existing report.json and merge
    skill-only fields' flow that convert_interactive.report used to do
    inline. Routing through one helper keeps the two reporting paths from
    drifting and localises the malformed-JSON handling.
    """

    def test_merges_extras_into_existing_report(self, tmp_path):
        report = _make_report()
        path = tmp_path / "report.json"
        generate_report(report, path, print_summary=False)

        merged = augment_report(path, {
            "completed_skill_phases": ["discover", "inventory"],
            "universe_id": 12345,
        })

        assert merged["completed_skill_phases"] == ["discover", "inventory"]
        assert merged["universe_id"] == 12345
        # Existing fields from generate_report survive.
        assert merged["unity_project_path"] == "/fake/project"
        assert merged["assets"]["total"] == 50

        # Disk write reflects the merge.
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["universe_id"] == 12345
        assert on_disk["unity_project_path"] == "/fake/project"

    def test_missing_file_starts_from_empty(self, tmp_path):
        path = tmp_path / "nope.json"
        merged = augment_report(path, {"foo": "bar"})

        assert merged == {"foo": "bar"}
        assert json.loads(path.read_text(encoding="utf-8")) == {"foo": "bar"}

    def test_malformed_file_warns_and_starts_from_empty(self, tmp_path, capsys):
        path = tmp_path / "report.json"
        path.write_text("not valid json {{{")

        merged = augment_report(path, {"after": "ok"})

        captured = capsys.readouterr()
        assert "could not parse" in captured.err
        assert merged == {"after": "ok"}
        assert json.loads(path.read_text(encoding="utf-8")) == {"after": "ok"}

    def test_extras_override_existing_fields(self, tmp_path):
        path = tmp_path / "report.json"
        path.write_text(json.dumps({"status": "old", "keep": "me"}))

        merged = augment_report(path, {"status": "new"})

        assert merged["status"] == "new"  # override
        assert merged["keep"] == "me"  # preserved


class TestPipelineIntegration:
    """Tests that exercise Pipeline._build_conversion_report end-to-end."""

    def test_build_conversion_report_reflects_ctx(self, tmp_path):
        from converter.pipeline import Pipeline
        from core.conversion_context import ConversionContext
        from core.roblox_types import RbxPlace, RbxScript

        project = tmp_path / "proj"
        (project / "Assets").mkdir(parents=True)
        pipeline = Pipeline(
            unity_project_path=project, output_dir=tmp_path / "out", skip_upload=True,
        )
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

        report = pipeline._build_conversion_report(
            tmp_path / "place.rbxlx",
            {"parts_written": 295, "scripts_written": 36},
            tmp_path / "report.json",
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

    def test_script_summary_reflects_transpilation_result(self, tmp_path):
        """When ``state.transpilation_result`` is populated, the report's
        ``scripts`` block must surface ai/rule-based/flagged counts and
        the list of flagged script names — not just the bare total from
        ``ctx.transpiled_scripts``.
        """
        from converter.pipeline import Pipeline
        from converter.code_transpiler import TranspilationResult, TranspiledScript
        from core.conversion_context import ConversionContext
        from core.roblox_types import RbxPlace

        project = tmp_path / "proj"
        (project / "Assets").mkdir(parents=True)
        pipeline = Pipeline(
            unity_project_path=project, output_dir=tmp_path / "out", skip_upload=True,
        )
        pipeline.ctx = ConversionContext(transpiled_scripts=3)
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.transpilation_result = TranspilationResult(
            scripts=[
                TranspiledScript(
                    source_path=str(project / "Assets" / "Foo.cs"),
                    output_filename="Foo.luau",
                    csharp_source="", luau_source="",
                    strategy="ai", confidence=0.95,
                    flagged_for_review=False,
                ),
                TranspiledScript(
                    source_path=str(project / "Assets" / "Bar.cs"),
                    output_filename="Bar.luau",
                    csharp_source="", luau_source="",
                    strategy="ai", confidence=0.4,
                    flagged_for_review=True,
                ),
                TranspiledScript(
                    source_path=str(project / "Assets" / "Baz.cs"),
                    output_filename="Baz.luau",
                    csharp_source="", luau_source="",
                    strategy="stub", confidence=0.3,
                    flagged_for_review=False,
                ),
            ],
            total_transpiled=3, total_ai=2, total_rule_based=1,
            total_flagged=1, total_failed=0,
        )

        report = pipeline._build_conversion_report(
            tmp_path / "place.rbxlx", {"parts_written": 0, "scripts_written": 3},
            tmp_path / "report.json",
        )
        assert report.scripts.total == 3
        assert report.scripts.succeeded == 3
        assert report.scripts.ai_transpiled == 2
        assert report.scripts.flagged_for_review == 1
        assert report.scripts.flagged_scripts == ["Bar.cs"]

    def test_interactive_report_preserves_structured_fields(self, tmp_path):
        """Interactive report() augments the file without clobbering shape."""
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
