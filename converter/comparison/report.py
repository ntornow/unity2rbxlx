"""
Generate Markdown comparison reports summarising the visual and state diff
results of a Unity-to-Roblox conversion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .state_diff import StateDiffResult

logger = logging.getLogger(__name__)


@dataclass
class ComparisonReport:
    """Container for all comparison metrics and artefacts."""

    visual_score: float
    """SSIM visual similarity score in ``[0.0, 1.0]``."""

    state_diff: StateDiffResult
    """Object-level transform comparison result."""

    screenshots: Dict[str, Path]
    """Mapping of label (e.g. ``"unity"``, ``"roblox"``) to screenshot path."""

    heatmap_path: Optional[Path] = None
    """Path to the visual diff heatmap image, if generated."""

    timestamp: str = ""
    """ISO-8601 timestamp of when the report was created."""

    warnings: List[str] = field(default_factory=list)
    """Any warnings or recommendations surfaced during comparison."""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()


def _quality_label(ssim: float) -> str:
    """Return a human-readable quality label for the given SSIM score."""
    if ssim >= 0.95:
        return "Excellent"
    if ssim >= 0.85:
        return "Good"
    if ssim >= 0.70:
        return "Fair"
    if ssim >= 0.50:
        return "Poor"
    return "Very Poor"


def generate_report(
    visual_score: float,
    state_diff: StateDiffResult,
    screenshots: Dict[str, Path],
    heatmap_path: Optional[Path],
    output_dir: str | Path,
    warnings: Optional[List[str]] = None,
) -> Path:
    """Write a Markdown comparison report to *output_dir*.

    Args:
        visual_score: SSIM score from :func:`visual_diff.compute_ssim`.
        state_diff: Result from :func:`state_diff.diff_states`.
        screenshots: Dict mapping labels to screenshot file paths.
        heatmap_path: Path to the diff heatmap, or ``None``.
        output_dir: Directory in which to write the report file.
        warnings: Optional list of warning strings to include.

    Returns:
        Path to the written Markdown report file.
    """
    report = ComparisonReport(
        visual_score=visual_score,
        state_diff=state_diff,
        screenshots=screenshots,
        heatmap_path=heatmap_path,
        warnings=warnings or [],
    )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "comparison_report.md"

    lines: List[str] = []

    # -- Header --
    lines.append("# Unity-to-Roblox Conversion Comparison Report")
    lines.append("")
    lines.append(f"**Generated:** {report.timestamp}")
    lines.append("")

    # -- Summary metrics --
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| SSIM Score | {report.visual_score:.4f} ({_quality_label(report.visual_score)}) |")
    lines.append(f"| Matched Objects | {state_diff.matched_objects} |")
    lines.append(f"| Unmatched (Unity only) | {len(state_diff.unmatched_unity)} |")
    lines.append(f"| Unmatched (Roblox only) | {len(state_diff.unmatched_roblox)} |")
    lines.append(f"| Mean Position Error | {state_diff.mean_position_error:.4f} |")
    lines.append(f"| Mean Rotation Error | {state_diff.mean_rotation_error:.4f} |")
    lines.append("")

    # -- Screenshots --
    if screenshots:
        lines.append("## Screenshots")
        lines.append("")
        for label, path in sorted(screenshots.items()):
            lines.append(f"### {label.title()}")
            lines.append(f"![{label}]({path})")
            lines.append("")

    # -- Heatmap --
    if heatmap_path:
        lines.append("## Visual Diff Heatmap")
        lines.append("")
        lines.append(f"![Diff Heatmap]({heatmap_path})")
        lines.append("")
        lines.append("Brighter regions indicate larger pixel-level differences.")
        lines.append("")

    # -- Position/Rotation Error Details --
    if state_diff.position_diffs:
        lines.append("## Per-Object Position Errors")
        lines.append("")
        lines.append("| Object | Position Error | Rotation Error | Size Error |")
        lines.append("|--------|----------------|----------------|------------|")
        for name in sorted(state_diff.position_diffs.keys()):
            pos_err = state_diff.position_diffs.get(name, 0.0)
            rot_err = state_diff.rotation_diffs.get(name, 0.0)
            size_err = state_diff.size_diffs.get(name, 0.0)
            lines.append(f"| {name} | {pos_err:.4f} | {rot_err:.4f} | {size_err:.4f} |")
        lines.append("")

    # -- Unmatched objects --
    if state_diff.unmatched_unity:
        lines.append("## Unmatched Unity Objects")
        lines.append("")
        lines.append("These objects exist in the Unity scene but were not found in Roblox:")
        lines.append("")
        for name in state_diff.unmatched_unity:
            lines.append(f"- `{name}`")
        lines.append("")

    if state_diff.unmatched_roblox:
        lines.append("## Unmatched Roblox Objects")
        lines.append("")
        lines.append("These objects exist in Roblox but were not found in the Unity scene:")
        lines.append("")
        for name in state_diff.unmatched_roblox:
            lines.append(f"- `{name}`")
        lines.append("")

    # -- Warnings and Recommendations --
    if report.warnings:
        lines.append("## Warnings and Recommendations")
        lines.append("")
        for warning in report.warnings:
            lines.append(f"- {warning}")
        lines.append("")

    # -- Footer --
    lines.append("---")
    lines.append("*Report generated by Unity-to-Roblox Converter*")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Comparison report written to %s", report_path)
    return report_path
