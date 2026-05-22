"""
studio_behavior_driver.py -- CLI front-door for the behavior-fixture runner.

The ``/e2e-test`` skill calls this script to get per-fixture work it can
walk via Studio MCP. It is intentionally I/O-only: no MCP, no Studio.
The skill is the MCP transport; this script is the contract serializer.

Subcommands
-----------
``list <project>``
    Print a JSON array of ``{id, feature, has_setup, input_count,
    wait_seconds, assert_timeout_seconds}`` for every fixture in the
    project's behavior file. Used by the skill to enumerate work.

``validate <project>``
    Schema-validate the behavior file; exit non-zero on error. Same
    check the pytest harness runs, exposed as a CLI for sanity-checking
    a freshly authored fixture set without booting pytest.

``emit-plan <project> [--only id1,id2]``
    For each fixture, emit a JSON object with the fully-resolved Luau
    bodies (preamble already prepended), input sequences, wait
    durations, expected value + tolerance, and assert_timeout_seconds.
    The skill walks the emitted plan calling MCP tools and never has
    to know the runner's internal Step dataclass.

``report <run.json>``
    Re-print the markdown summary from a saved combined report. Useful
    after a run to ask "what failed last night?" without re-parsing the
    raw JSON.

Project resolution
------------------
``<project>`` resolves to ``tests/fixtures/upload_snapshots/<project>.behavior.json``
under the converter root. This matches the snapshot resolver convention
used by the offline-assembly test.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Allow running as ``python -m tests.studio_behavior_driver`` from the
# converter root without an install — same shim the offline-assembly
# test uses.
sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.studio_behavior_runner import (  # noqa: E402
    BehaviorSchemaError,
    FixtureResult,
    format_summary,
    iter_fixtures,
    load_fixtures,
    plan_for_fixture,
    validate_behavior_file,
)


_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "upload_snapshots"


def _project_path(project: str) -> Path:
    """Resolve ``<project>`` → behavior.json under fixtures/."""
    p = _FIXTURES_DIR / f"{project}.behavior.json"
    if not p.exists():
        raise FileNotFoundError(
            f"No behavior fixtures for {project!r} "
            f"(expected at {p}). Available: "
            f"{sorted(f.stem.split('.')[0] for f in _FIXTURES_DIR.glob('*.behavior.json'))}"
        )
    return p


def cmd_list(args: argparse.Namespace) -> int:
    """``list`` — enumerate fixtures as a JSON array."""
    path = _project_path(args.project)
    _, fixtures = load_fixtures(path)
    out: list[dict[str, Any]] = []
    for f in fixtures:
        out.append({
            "id": f["id"],
            "feature": f.get("feature", ""),
            "has_setup": bool(f.get("setup_luau")),
            "input_count": len(f.get("input_sequence") or []),
            "wait_seconds": f.get("wait_seconds", 0),
            "assert_timeout_seconds": f.get("assert_timeout_seconds", 0),
            "depends_on": list(f.get("depends_on") or []),
        })
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """``validate`` — schema-check the project's behavior file."""
    try:
        path = _project_path(args.project)
        validate_behavior_file(path)
    except (FileNotFoundError, BehaviorSchemaError) as exc:
        print(f"validate FAILED: {exc}", file=sys.stderr)
        return 2
    print(f"validate OK: {args.project}")
    return 0


def _mouse_action_to_mcp(payload: dict) -> dict[str, Any]:
    """Map a fixture mouse input to an mcp user_mouse_input action.

    Fixture vocabulary → mcp ``action`` enum:
      {kind: mouse_click, button: left|right}  → {action: mouseButtonClick, mouse_button: ...}
      {kind: mouse_move,  x, y}                → {action: moveTo, x, y}
    Any explicit ``action`` already in mcp form is passed through. Extra
    coordinate keys (x/y) are preserved so a click can carry a position.
    """
    kind = payload.get("kind")
    out: dict[str, Any] = {}
    if kind == "mouse_click":
        out["action"] = "mouseButtonClick"
        out["mouse_button"] = payload.get("button", "left")
    elif kind == "mouse_move":
        out["action"] = "moveTo"
    else:
        # Already-mcp-shaped or unknown: copy the action verbatim.
        if "action" in payload:
            out["action"] = payload["action"]
    for coord in ("x", "y", "instance_path", "wait_time_ms"):
        if coord in payload:
            out[coord] = payload[coord]
    return out


def _fixture_to_plan(fixture: dict, preamble: str) -> dict[str, Any]:
    """Serialise one fixture's plan as an MCP-ready JSON object.

    The skill consumes this directly without importing the runner's
    Step dataclass — it sees only string Luau bodies and JSON-native
    action descriptors.
    """
    steps = plan_for_fixture(fixture, preamble)
    safety_step = next(s for s in steps if s.kind == "safety_check_studio")
    setup_step = next((s for s in steps if s.kind == "execute_setup"), None)
    assert_step = next(s for s in steps if s.kind == "execute_assert")

    inputs: list[dict[str, Any]] = []
    for s in steps:
        if s.kind == "keyboard_input":
            # The fixture's keyboard shape ({action: keyDown|keyUp|keyPress,
            # key_code}) already matches mcp user_keyboard_input — pass it
            # through minus the routing 'kind' tag.
            inputs.append({"type": "keyboard",
                           "action": {k: v for k, v in s.payload.items() if k != "kind"}})
        elif s.kind == "mouse_input":
            # Translate the fixture's mouse shape to mcp user_mouse_input's
            # action vocabulary. The fixture authors think in
            # {kind: mouse_click, button: left} / {kind: mouse_move, x, y};
            # mcp wants {action: mouseButtonClick, mouse_button: left} /
            # {action: moveTo, x, y}. Doing the mapping HERE means the skill
            # never has to know the translation (it surfaced as a live bug
            # 2026-05-22: the skill sent the raw {button:left} and mcp
            # rejected "Unknown mouse action").
            inputs.append({"type": "mouse",
                           "action": _mouse_action_to_mcp(s.payload)})
        elif s.kind == "wait" and s.note != "settle before assert":
            inputs.append({"type": "wait", "seconds": s.payload["seconds"]})

    return {
        "id": fixture["id"],
        "feature": fixture.get("feature", ""),
        "safety_check_note": safety_step.note,
        "setup_luau": setup_step.payload if setup_step else None,
        "input_sequence": inputs,
        "wait_seconds": float(fixture.get("wait_seconds") or 0),
        "assert_luau": assert_step.payload,
        "assert_timeout_seconds": assert_step.timeout_seconds,
        "poll_interval_seconds": assert_step.poll_interval_seconds,
        "expect": fixture["expect"],
        "tolerance": fixture.get("tolerance"),
        "depends_on": list(fixture.get("depends_on") or []),
        "evidence_on_fail": list(fixture.get("evidence_on_fail") or []),
    }


def cmd_emit_plan(args: argparse.Namespace) -> int:
    """``emit-plan`` — JSON array of per-fixture plans, in order."""
    path = _project_path(args.project)
    preamble, _ = load_fixtures(path)
    only = set(args.only.split(",")) if args.only else None
    plans = [
        _fixture_to_plan(f, preamble)
        for f in iter_fixtures(path, only=only)
    ]
    json.dump(plans, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def _result_from_report_dict(d: dict) -> FixtureResult:
    """Reconstruct a FixtureResult from the serialized report shape so
    format_summary can render it. Loses step_results (the report doesn't
    persist them) but keeps everything format_summary cares about."""
    r = FixtureResult(fixture_id=d["id"], passed=bool(d.get("passed")))
    r.assertion_value = d.get("value")
    r.started_at = d.get("started_at")
    r.finished_at = d.get("finished_at")
    r.duration_seconds = float(d.get("duration_seconds") or 0.0)
    r.attempts = int(d.get("attempts") or 0)
    r.error = d.get("error")
    return r


def cmd_report(args: argparse.Namespace) -> int:
    """``report`` — re-print the stdout summary from a saved JSON report."""
    report_path = Path(args.run_json)
    data = json.loads(report_path.read_text(encoding="utf-8"))
    project = data.get("project", "?")
    conversion = data.get("conversion") or {}
    fixtures = data.get("gameplay", {}).get("fixtures") or []
    results = [_result_from_report_dict(f) for f in fixtures]
    print(format_summary(
        results, project=project,
        conversion_passed=conversion.get("passed"),
        conversion_duration_seconds=conversion.get("duration_seconds"),
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="studio_behavior_driver",
        description="Per-fixture plans for the /e2e-test skill.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="enumerate fixtures as JSON")
    p_list.add_argument("project", help="project name e.g. SimpleFPS")
    p_list.set_defaults(func=cmd_list)

    p_validate = sub.add_parser("validate", help="schema-check the behavior file")
    p_validate.add_argument("project")
    p_validate.set_defaults(func=cmd_validate)

    p_plan = sub.add_parser("emit-plan", help="per-fixture MCP-ready plans")
    p_plan.add_argument("project")
    p_plan.add_argument(
        "--only", default="",
        help="comma-separated fixture IDs to include (default: all)",
    )
    p_plan.set_defaults(func=cmd_emit_plan)

    p_report = sub.add_parser("report", help="re-print summary from a saved run.json")
    p_report.add_argument("run_json", help="path to combined report JSON")
    p_report.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
