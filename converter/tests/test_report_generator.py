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
    generate_report,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


class TestGenerateReport:
    """Verify JSON output correctness."""

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
        assert "generated_at" in data
        assert "unity_project_path" in data
        assert "assets" in data
        assert "scripts" in data
        assert "materials" in data
        assert "scene" in data
        assert "components" in data
        assert "output" in data

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
    """Verify print_summary produces output."""

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
