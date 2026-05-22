"""scene_runtime_spike.py -- Compliance spike runner for PR3a.

Drives the generic-runtime contract verifier over every MonoBehaviour
in a Unity project and reports per-module + aggregate pre/post-reprompt
pass rate. This is the gate measurement before PR3b/PR4 of the scene-
runtime effort (see ``converter/docs/design/scene-runtime-contract.md``
PR3a row -- the verifier pass rate is the single biggest unquantified
risk in the effort).

What it does:
  1. Discovers every ``.cs`` script in the project via
     ``unity.script_analyzer.analyze_all_scripts``.
  2. Builds a SYNTHETIC ``scene_runtime`` artifact -- marks every
     MonoBehaviour/NetworkBehaviour-derived class as runtime_bearing.
     This OVER-counts (PR1's planner additionally checks scene/prefab
     attachment) which is the right direction for a gate measurement:
     it stresses the verifier across the broadest possible MB surface
     the project ships.
  3. Calls ``contract_pipeline.transpile_with_contract`` with that
     artifact -- transpiler runs in generic mode, contract verifier
     fires per module, one-shot reprompt on violation.
  4. Computes aggregate pre/post-reprompt pass rate, breaks down
     fail-closed reasons, prints a report.

Usage (run from the ``converter/`` directory):
  python tools/scene_runtime_spike.py <unity-project> [--output report.json]

The default AI backend is whatever ``code_transpiler._find_transpiler``
detects (Claude CLI preferred, Anthropic API as fallback). Set
``--no-ai`` for a smoke test that uses the stub generator (verifier
will reject every stub since they're plain C#-flavored shells).
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import click

# Add the converter root to sys.path so we can import from converter.*.
sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.contract_pipeline import (  # noqa: E402
    ContractPipelineResult,
    _SceneRuntimeArtifact,
    transpile_with_contract,
)
from unity.script_analyzer import ScriptInfo  # noqa: E402
from utils.logging_config import setup_logging  # noqa: E402

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Report shape
# ---------------------------------------------------------------------------

@dataclass
class ModuleReport:
    """Per-module compliance result for the spike report."""
    source_path: str
    class_name: str
    strategy: str           # "ai" | "stub" | "rule_based"
    first_attempt_clean: bool
    reprompt_rescued: bool
    failed_closed: bool
    pre_reprompt_violations: list[str] = field(default_factory=list)
    post_reprompt_violations: list[str] = field(default_factory=list)


@dataclass
class SpikeReport:
    """Aggregate compliance-spike report for one project.

    Pass rates are computed over the AI-transpiled subset only. Stubs
    bypass the verifier (they're emitted when ``use_ai=False`` or the
    backend isn't reachable) and are excluded from the rates -- a
    ``--no-ai`` run reports a real ``ai_module_count=0`` instead of a
    misleading 100% pass rate.
    """
    project_path: str
    total_runtime_bearing: int
    ai_module_count: int           # the denominator of the pass rates
    stub_module_count: int
    first_attempt_pass_count: int
    reprompt_rescued_count: int
    fail_closed_count: int
    pre_reprompt_pass_rate: float  # over AI-transpiled only
    post_reprompt_pass_rate: float # over AI-transpiled only
    modules: list[ModuleReport] = field(default_factory=list)
    fail_closed_reasons: list[dict[str, str]] = field(default_factory=list)
    require_failures: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Synthetic planner artifact -- mirrors PR1's ``scene_runtime`` shape
# without depending on PR1's planner code. The contract surface is the
# artifact, not the planner.
# ---------------------------------------------------------------------------

def _build_synthetic_scene_runtime(
    script_infos: list[ScriptInfo],
) -> _SceneRuntimeArtifact:
    """Build a ``scene_runtime``-shaped dict directly from script_infos.

    Marks every MonoBehaviour-derived class as runtime_bearing. PR1's
    real planner filters this further (must be attached to a scene
    GameObject or prefab) -- the spike OVER-includes so we get the
    broadest possible compliance measurement.
    """
    from converter.contract_pipeline import _SceneRuntimeModule
    modules: dict[str, _SceneRuntimeModule] = {}
    for info in script_infos:
        # Skip editor / test scripts -- they're never converted.
        if info.is_editor_script or info.is_test_script:
            continue
        # Use the file path as a synthetic script id (PR1 uses .cs GUID).
        # The contract pipeline doesn't depend on the id format; it only
        # needs ``stem`` for the require resolver + ``runtime_bearing``
        # for the target switch.
        script_id = str(info.path)
        # Predicate: MonoBehaviour-derived (matches the transpiler's own
        # ``_classify_script_type`` MB check). Caught classes: anything
        # whose base mentions MonoBehaviour or NetworkBehaviour. Spike-
        # specific over-inclusion: also include scripts whose source
        # mentions MonoBehaviour anywhere (regex match in
        # ``analyze_script`` already records ``base_class``).
        is_mb = (
            "MonoBehaviour" in (info.base_class or "")
            or "NetworkBehaviour" in (info.base_class or "")
        )
        modules[script_id] = {
            "stem": info.path.stem,
            "class_name": info.class_name,
            "runtime_bearing": is_mb,
        }
    return {
        "modules": modules,
        "scenes": {},
        "prefabs": {},
        "domain_overrides": {},
    }


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _build_module_reports(result: ContractPipelineResult) -> list[ModuleReport]:
    out: list[ModuleReport] = []
    for script in result.runtime_bearing_scripts:
        warnings = script.warnings
        pre = [w for w in warnings if w.startswith("contract-verifier-pre")]
        post = [
            w for w in warnings
            if w.startswith("contract-verifier ") or w.startswith("contract-verifier:")
        ]
        first_clean = not pre and not post
        rescued = bool(pre) and not post
        failed = bool(post)
        out.append(ModuleReport(
            source_path=script.source_path,
            class_name=Path(script.source_path).stem,
            strategy=script.strategy,
            first_attempt_clean=first_clean,
            reprompt_rescued=rescued,
            failed_closed=failed,
            pre_reprompt_violations=pre,
            post_reprompt_violations=post,
        ))
    return out


def _build_spike_report(
    project_path: Path, result: ContractPipelineResult,
) -> SpikeReport:
    modules = _build_module_reports(result)
    # Pass rates are over AI-transpiled modules only. Stubs bypass the
    # verifier; counting them would inflate (--no-ai) or deflate the
    # rate depending on how the stub generator output happens to lex.
    ai_modules = [m for m in modules if m.strategy == "ai"]
    stub_modules = [m for m in modules if m.strategy != "ai"]
    ai_n = len(ai_modules)
    first_attempt = sum(1 for m in ai_modules if m.first_attempt_clean)
    rescued = sum(1 for m in ai_modules if m.reprompt_rescued)
    failed = sum(1 for m in ai_modules if m.failed_closed)
    pre_rate = (first_attempt / ai_n) if ai_n else 0.0
    post_rate = ((first_attempt + rescued) / ai_n) if ai_n else 0.0
    return SpikeReport(
        project_path=str(project_path),
        total_runtime_bearing=result.total_runtime_bearing,
        ai_module_count=ai_n,
        stub_module_count=len(stub_modules),
        first_attempt_pass_count=first_attempt,
        reprompt_rescued_count=rescued,
        fail_closed_count=failed,
        pre_reprompt_pass_rate=pre_rate,
        post_reprompt_pass_rate=post_rate,
        modules=modules,
        fail_closed_reasons=[
            {"kind": fc.kind, "detail": fc.detail}
            for fc in result.fail_closed
        ],
        require_failures=[
            {
                "from_script": r.from_script,
                "stem": r.stem,
                "reason": r.reason,
            }
            for r in result.require_resolutions
            if not r.ok
        ],
    )


# ---------------------------------------------------------------------------
# Pretty printer
# ---------------------------------------------------------------------------

def _print_report(report: SpikeReport) -> None:
    click.echo("")
    click.echo("=" * 70)
    click.echo(f"Compliance spike: {report.project_path}")
    click.echo("=" * 70)
    n = report.total_runtime_bearing
    if n == 0:
        click.echo("No runtime-bearing MonoBehaviours found.")
        return
    click.echo(f"Runtime-bearing MonoBehaviours: {n}")
    click.echo(f"  AI-transpiled:    {report.ai_module_count}")
    click.echo(f"  Stub/rule-based:  {report.stub_module_count}  (excluded from pass rates)")
    if report.ai_module_count == 0:
        click.echo("")
        click.echo("WARNING: 0 modules were AI-transpiled (likely --no-ai or no")
        click.echo("backend available). Pass rates are meaningless. The compliance")
        click.echo("spike requires the Claude CLI on PATH or --api-key set.")
        return
    ai_n = report.ai_module_count
    click.echo("")
    click.echo("Pass-rate breakdown (over AI-transpiled subset):")
    click.echo(
        f"  First-attempt clean:    "
        f"{report.first_attempt_pass_count:3d} / {ai_n:3d}  "
        f"({report.first_attempt_pass_count / ai_n:.1%})"
    )
    click.echo(
        f"  Reprompt-rescued:       "
        f"{report.reprompt_rescued_count:3d} / {ai_n:3d}  "
        f"({report.reprompt_rescued_count / ai_n:.1%})"
    )
    click.echo(
        f"  Still failing:          "
        f"{report.fail_closed_count:3d} / {ai_n:3d}  "
        f"({report.fail_closed_count / ai_n:.1%})"
    )
    click.echo("")
    click.echo("Aggregate pass rate:")
    click.echo(f"  Pre-reprompt:   {report.pre_reprompt_pass_rate:.1%}")
    click.echo(f"  Post-reprompt:  {report.post_reprompt_pass_rate:.1%}")
    click.echo("")

    if report.fail_closed_reasons:
        click.echo("Fail-closed reasons (would route to legacy under --scene-runtime=auto):")
        by_kind: dict[str, int] = {}
        for fc in report.fail_closed_reasons:
            by_kind[fc["kind"]] = by_kind.get(fc["kind"], 0) + 1
        for kind, count in sorted(by_kind.items()):
            click.echo(f"  {kind}: {count}")
        click.echo("")

    # List still-failing modules so the reviewer can read their violations.
    still_failing = [m for m in report.modules if m.failed_closed]
    if still_failing:
        click.echo("Still-failing modules (post-reprompt violations):")
        for m in still_failing[:20]:
            click.echo(f"  {m.class_name}:")
            for v in m.post_reprompt_violations[:3]:
                # Strip the ``contract-verifier`` prefix for terseness.
                msg = v.split(":", 1)[-1].strip() if ":" in v else v
                click.echo(f"    - {msg[:120]}")
        if len(still_failing) > 20:
            click.echo(f"  ... and {len(still_failing) - 20} more")


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

@click.command()
@click.argument("unity_project", type=click.Path(exists=True, file_okay=False))
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Write the report as JSON to this path "
                   "(default: stdout summary only).")
@click.option("--api-key", type=str, default=None,
              help="Anthropic API key (string or path to a key file). "
                   "Skipped when the Claude CLI is on PATH.")
@click.option("--no-ai", is_flag=True,
              help="Disable AI; produces stub output. Useful for "
                   "validating the spike plumbing without burning tokens "
                   "-- the verifier will reject every stub, so pass "
                   "rates will be 0%%.")
@click.option("--verbose", "-v", is_flag=True,
              help="Debug logging.")
def main(
    unity_project: str,
    output: str | None,
    api_key: str | None,
    no_ai: bool,
    verbose: bool,
) -> None:
    """Run the scene-runtime contract verifier over a Unity project.

    Drives every MonoBehaviour through the generic-runtime transpile
    pipeline and records per-module pre/post-reprompt verifier pass
    rate. Use the report to decide whether the PR3a → PR3b/PR4 gate
    has cleared.

    Example (run from the ``converter/`` directory):

      python tools/scene_runtime_spike.py /path/to/UnityProject \\
        --output spike-report.json
    """
    setup_logging("DEBUG" if verbose else "INFO")
    project = Path(unity_project).resolve()

    # Resolve API key (may be a path).
    if api_key:
        key_path = Path(api_key)
        key_value = key_path.read_text().strip() if key_path.is_file() else api_key.strip()
    else:
        key_value = ""

    # Discover scripts.
    from unity.script_analyzer import analyze_all_scripts
    script_infos = analyze_all_scripts(project)
    if not script_infos:
        click.echo(f"No .cs scripts found under {project}", err=True)
        sys.exit(1)
    click.echo(f"Discovered {len(script_infos)} scripts.")

    scene_runtime = _build_synthetic_scene_runtime(script_infos)
    runtime_bearing_count = sum(
        1 for m in scene_runtime["modules"].values() if m["runtime_bearing"]
    )
    click.echo(
        f"Synthetic planner: {runtime_bearing_count} MonoBehaviour(s) "
        f"flagged runtime_bearing."
    )

    click.echo("Running generic-runtime transpile + verifier...")
    result = transpile_with_contract(
        unity_project_path=project,
        script_infos=script_infos,
        scene_runtime=scene_runtime,
        use_ai=not no_ai,
        api_key=key_value,
    )

    report = _build_spike_report(project, result)
    _print_report(report)

    if output:
        out_path = Path(output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(asdict(report), indent=2))
        click.echo(f"\nReport written to {out_path}")


if __name__ == "__main__":
    main()
