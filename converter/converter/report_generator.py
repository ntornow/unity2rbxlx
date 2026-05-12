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
    # Phase 4.4: C# methods that disappeared silently from the Luau
    # output — neither present as a function nor marked UNCONVERTED.
    method_completeness_warnings: list[str] = field(default_factory=list)


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
class GameplayAdapterBinding:
    """One detector match recorded in ``conversion_report.json`` —
    everything the operator needs to write a ``.gameplay_deny.txt``
    line without re-reading the converter source.
    """
    detector_name: str = ""
    diagnostic_name: str = ""
    target_class_name: str = ""
    node_name: str = ""
    node_file_id: str = ""
    component_file_id: str = ""
    script_path: str = ""
    capability_kinds: list[str] = field(default_factory=list)


@dataclass
class GameplayAdapterDivergentBinding:
    """One of the two bindings that disagreed when a class fell into
    ``GameplayAdapterDivergence``. Carries enough structure that an
    operator can write a deny-list line targeting EITHER side without
    re-parsing the free-form ``detail`` message.
    """
    node_name: str = ""
    node_file_id: str = ""
    component_file_id: str = ""


@dataclass
class GameplayAdapterDivergence:
    """One class that fell back to AI because its per-node Behaviors
    diverged. Recorded so operators can see WHY a class they expected
    to be adapter-handled was left to the AI path.
    """
    class_name: str = ""
    script_path: str = ""
    detail: str = ""
    binding_a: GameplayAdapterDivergentBinding = field(
        default_factory=GameplayAdapterDivergentBinding,
    )
    binding_b: GameplayAdapterDivergentBinding = field(
        default_factory=GameplayAdapterDivergentBinding,
    )


@dataclass
class GameplayAdapterSummary:
    """Top-level gameplay-adapter section. Empty by default — only
    populated when ``--use-gameplay-adapters`` is on and the pass ran.

    Counters distinguish three states for clarity (codex
    PR #73a-round-3 flagged the prior ``total_classes_matched`` as
    ambiguous):
      - ``total_classes_emitted``: classes whose adapter Behavior was
        rendered into a TranspiledScript (unique by ``script_path``
        so same-named classes from different .cs files don't collapse).
      - ``total_classes_divergent``: classes that DID match detection
        but were dropped because per-node Behaviors diverged. AI
        handles those.
      - ``total_bindings``: every (node, component) hit, including
        multiple bindings of the same emitted class.
    """
    enabled: bool = False
    total_classes_emitted: int = 0
    total_classes_divergent: int = 0
    total_bindings: int = 0
    bindings: list[GameplayAdapterBinding] = field(default_factory=list)
    divergent_classes: list[GameplayAdapterDivergence] = field(default_factory=list)


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
    gameplay_adapters: GameplayAdapterSummary = field(
        default_factory=GameplayAdapterSummary,
    )


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


def augment_report(report_path: Path, extras: dict[str, Any]) -> dict[str, Any]:
    """Merge skill-specific fields into an existing conversion_report.json.

    pipeline.write_output serializes a ConversionReport dataclass via
    generate_report. The interactive /convert-unity skill then needs to
    decorate that report with skill-only state (completed_skill_phases,
    universe_id, rbxlx_size_mb, etc) without reopening the dataclass
    schema. This helper reads, merges, writes, and returns the merged
    dict — one path, one broad-catch, one stderr warning. Callers must
    not duplicate the json.loads/update/dumps dance.

    Missing or malformed reports start from an empty dict so the skill
    path still produces a report the user can inspect; the underlying
    parse error is surfaced on stderr.
    """
    import sys

    report_path = Path(report_path)
    report_data: dict[str, Any] = {}
    if report_path.exists():
        try:
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(
                f"warning: could not parse existing {report_path.name} ({exc}); "
                f"regenerating from scratch.",
                file=sys.stderr,
            )

    report_data.update(extras)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report_data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return report_data
