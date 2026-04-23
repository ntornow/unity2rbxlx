"""Single conversion-report writer. Replaces the per-caller inline JSON
emissions that used to drift between pipeline.write_output and the
interactive CLI's report command (Phase 3 item 7)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class AssetSummary:
    total: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    total_size_bytes: int = 0
    duplicates_removed: int = 0


@dataclass
class ScriptSummary:
    total: int = 0
    succeeded: int = 0
    flagged_for_review: int = 0
    skipped: int = 0
    ai_transpiled: int = 0
    flagged_scripts: list[str] = field(default_factory=list)   # filenames


@dataclass
class MaterialSummary:
    total: int = 0
    fully_converted: int = 0
    partially_converted: int = 0
    unconvertible: int = 0
    texture_ops: int = 0
    unconverted_md_path: str = ""


@dataclass
class ComponentSummary:
    total_encountered: int = 0
    converted: int = 0
    dropped: int = 0
    dropped_by_type: dict[str, int] = field(default_factory=dict)
    dropped_details: list[dict[str, str]] = field(default_factory=list)


@dataclass
class SceneSummary:
    selected_scene: str = ""  # project-relative Unity scene path
    scenes_parsed: int = 0
    total_game_objects: int = 0
    prefabs_parsed: int = 0
    prefab_instances_resolved: int = 0
    meshes_decimated: int = 0
    meshes_compliant: int = 0


@dataclass
class PackageSummary:
    total_packages: int = 0
    package_names: list[str] = field(default_factory=list)
    packages_dir: str = ""


@dataclass
class OutputSummary:
    rbxl_path: str = ""
    parts_written: int = 0
    scripts_in_place: int = 0
    report_path: str = ""
    packages: PackageSummary = field(default_factory=PackageSummary)


@dataclass
class ConversionReport:
    """Top-level conversion report written to disk after a completed run."""
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    unity_project_path: str = ""
    output_dir: str = ""
    duration_seconds: float = 0.0
    success: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assets: AssetSummary = field(default_factory=AssetSummary)
    materials: MaterialSummary = field(default_factory=MaterialSummary)
    scripts: ScriptSummary = field(default_factory=ScriptSummary)
    scene: SceneSummary = field(default_factory=SceneSummary)
    components: ComponentSummary = field(default_factory=ComponentSummary)
    output: OutputSummary = field(default_factory=OutputSummary)


def generate_report(
    report: ConversionReport,
    output_path: str | Path,
    verbose: bool = True,
    print_summary: bool = True,
) -> Path:
    """
    Serialise a ConversionReport to a JSON file and optionally print a summary.

    Args:
        report: Fully populated ConversionReport instance.
        output_path: File path for the JSON report (created/overwritten).
        verbose: If True, include per-script details in the JSON output.
        print_summary: If True, print a human-readable summary to stdout.

    Returns:
        Resolved Path of the written report file.
    """
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report_dict = asdict(report)

    if not verbose:
        # Strip large lists for compact mode
        report_dict["scripts"].pop("flagged_scripts", None)

    output_path.write_text(
        json.dumps(report_dict, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if print_summary:
        status = "SUCCESS" if report.success else "FAILED"
        lines = [
            "",
            "== Conversion Report ==",
            f"  Status       : {status}",
            f"  Duration     : {report.duration_seconds:.1f}s",
            f"  Assets found : {report.assets.total}",
            f"  Materials    : {report.materials.total} total, "
            f"{report.materials.fully_converted} full, "
            f"{report.materials.partially_converted} partial, "
            f"{report.materials.unconvertible} unconvertible",
            f"  Scripts      : {report.scripts.total} total, "
            f"{report.scripts.succeeded} OK, "
            f"{report.scripts.flagged_for_review} flagged",
            f"  GameObjects  : {report.scene.total_game_objects}",
            f"  Prefabs      : {report.scene.prefabs_parsed} parsed, "
            f"{report.scene.prefab_instances_resolved} instances resolved",
            f"  Meshes       : {report.scene.meshes_compliant} compliant, "
            f"{report.scene.meshes_decimated} decimated",
            f"  Parts in .rbxl: {report.output.parts_written}",
        ]
        if report.components.dropped > 0:
            lines.append(
                f"  Components   : {report.components.converted} converted, "
                f"{report.components.dropped} dropped"
            )
            for ctype, count in sorted(
                report.components.dropped_by_type.items(),
                key=lambda x: -x[1],
            ):
                lines.append(f"    ! {ctype}: {count} instance(s) not converted")
        if report.output.packages.total_packages:
            lines.append(f"  Packages     : {report.output.packages.total_packages} .rbxm file(s)")
        if report.warnings:
            lines.append(f"  Warnings     : {len(report.warnings)}")
        if report.errors:
            lines.append(f"  Errors       : {len(report.errors)}")
            for err in report.errors:
                lines.append(f"    x {err}")
        lines.append(f"  Report saved : {output_path}")
        lines.append("")
        print("\n".join(lines))

    return output_path
