"""
studio_behavior_runner.py -- Drive Studio MCP fixtures defined in
``*.behavior.json`` files. Dependency-injected so the caller controls
the MCP plumbing (the runner itself does no I/O).

Three roles:

* **Schema validation** (``validate_behavior_file``) — a pytest fixture
  loads every ``*.behavior.json`` and asserts the schema is well-formed,
  with no MCP/Studio dependency at all.

* **Plan generation** (``plan_for_fixture``) — converts one fixture
  entry into an ordered list of ``Step`` records describing the MCP
  calls that need to happen, in order. This is the canonical
  translation table between fixture JSON and Studio actions; it has no
  side effects.

* **Plan execution** (``run_fixture``) — takes the plan plus three
  callables (``execute_luau``, ``keyboard_input``, ``mouse_input``)
  and runs the plan, returning a ``FixtureResult``. The callables are
  the only things that touch MCP — wire them to
  ``mcp__Roblox_Studio__execute_luau`` etc. in a Claude Code
  conversation, or to a future MCP CLI client for nightly runs.

The preamble defined in ``behavior.json._schema.preamble`` is prepended
to every ``setup_luau`` and ``assert_luau`` body so fixture authors
don't repeat the ``plr/char/hrp/hum/cam/_state`` boilerplate.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Literal


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_REQUIRED_FIXTURE_FIELDS = ("id", "assert_luau", "expect")
_KNOWN_FIXTURE_FIELDS = frozenset({
    "id", "feature", "play_mode", "setup_luau", "input_sequence",
    "wait_seconds", "assert_luau", "expect", "tolerance", "depends_on",
    "evidence_on_fail",
})
_KNOWN_INPUT_KINDS = frozenset({"keyboard", "mouse_move", "mouse_click"})


class BehaviorSchemaError(ValueError):
    """Raised when a behavior.json file has a structural problem."""


def validate_behavior_file(path: Path) -> dict:
    """Load and structurally-validate a behavior.json file.

    Returns the parsed dict on success; raises BehaviorSchemaError with
    a message that names the offending fixture id (or "<no id>") and
    the specific structural problem.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BehaviorSchemaError(f"{path.name}: invalid JSON ({exc})") from exc

    if not isinstance(data, dict):
        raise BehaviorSchemaError(f"{path.name}: top-level must be an object")
    if "fixtures" not in data:
        raise BehaviorSchemaError(f"{path.name}: missing 'fixtures' array")
    fixtures = data["fixtures"]
    if not isinstance(fixtures, list):
        raise BehaviorSchemaError(f"{path.name}: 'fixtures' must be a list")

    seen_ids: set[str] = set()
    for i, f in enumerate(fixtures):
        label = f.get("id", f"<index {i}>") if isinstance(f, dict) else f"<index {i}>"

        if not isinstance(f, dict):
            raise BehaviorSchemaError(f"{path.name}[{label}]: fixture must be an object")

        for req in _REQUIRED_FIXTURE_FIELDS:
            if req not in f:
                raise BehaviorSchemaError(
                    f"{path.name}[{label}]: missing required field '{req}'"
                )

        unknown = set(f.keys()) - _KNOWN_FIXTURE_FIELDS
        if unknown:
            raise BehaviorSchemaError(
                f"{path.name}[{label}]: unknown field(s) {sorted(unknown)!r} "
                f"— update _KNOWN_FIXTURE_FIELDS if intentional"
            )

        fid = f["id"]
        if not isinstance(fid, str) or not fid:
            raise BehaviorSchemaError(f"{path.name}[{label}]: id must be a non-empty string")
        if fid in seen_ids:
            raise BehaviorSchemaError(f"{path.name}: duplicate fixture id {fid!r}")
        seen_ids.add(fid)

        for input_step in f.get("input_sequence", []) or []:
            if not isinstance(input_step, dict):
                raise BehaviorSchemaError(
                    f"{path.name}[{fid}]: input_sequence entries must be objects"
                )
            kind = input_step.get("kind")
            action = input_step.get("action")
            if kind is not None and kind not in _KNOWN_INPUT_KINDS:
                raise BehaviorSchemaError(
                    f"{path.name}[{fid}]: unknown input kind {kind!r} "
                    f"(known: {sorted(_KNOWN_INPUT_KINDS)})"
                )
            if action == "wait" and "wait_time_ms" not in input_step:
                raise BehaviorSchemaError(
                    f"{path.name}[{fid}]: wait action missing wait_time_ms"
                )

        # depends_on must reference earlier ids — forward references would
        # invert the harness's ordering guarantees and produce silently
        # wrong sequencing.
        for dep in f.get("depends_on", []) or []:
            if dep not in seen_ids:
                raise BehaviorSchemaError(
                    f"{path.name}[{fid}]: depends_on {dep!r} either is "
                    f"undefined or appears after this fixture"
                )

    return data


# ---------------------------------------------------------------------------
# Plan generation
# ---------------------------------------------------------------------------

StepKind = Literal[
    "safety_check_studio",
    "execute_luau_preamble",
    "execute_setup",
    "keyboard_input",
    "mouse_input",
    "wait",
    "execute_assert",
]


@dataclass
class Step:
    kind: StepKind
    payload: Any = None
    note: str = ""


def plan_for_fixture(fixture: dict, preamble: str) -> list[Step]:
    """Translate one fixture entry into an ordered list of executable Steps.

    The preamble is prepended to every setup_luau and assert_luau body
    so authored fixtures stay terse.
    """
    steps: list[Step] = []

    # Safety: every plan runs the "is this Agas Map of London?" guard at
    # the top so the harness refuses to send any work if the active
    # Studio is the wrong one. Re-checked inline in execute_luau too;
    # this version makes the failure mode explicit in the plan output.
    steps.append(Step(
        kind="safety_check_studio",
        note="verify game.Name != 'Agas Map of London'",
    ))

    setup = fixture.get("setup_luau")
    if setup:
        steps.append(Step(
            kind="execute_setup",
            payload=f"{preamble}\n{setup}",
            note="setup_luau",
        ))

    for input_step in fixture.get("input_sequence", []) or []:
        kind = input_step.get("kind")
        if kind == "keyboard":
            steps.append(Step(kind="keyboard_input", payload=input_step))
        elif kind in ("mouse_move", "mouse_click"):
            steps.append(Step(kind="mouse_input", payload=input_step))
        elif input_step.get("action") == "wait":
            # Standalone wait entries with no kind are also valid inside
            # an input sequence — translate to a wait step.
            steps.append(Step(
                kind="wait",
                payload={"seconds": input_step["wait_time_ms"] / 1000.0},
            ))
        # Other shapes are rejected by validate_behavior_file before
        # we get here, so no else branch needed.

    wait_s = fixture.get("wait_seconds", 0)
    if wait_s and wait_s > 0:
        steps.append(Step(
            kind="wait",
            payload={"seconds": float(wait_s)},
            note="settle before assert",
        ))

    assertion = fixture["assert_luau"]
    # Wrap the assertion so the runner gets back a structured result and
    # can compare against `expect` with tolerance. The user's assertion
    # returns a value; the wrapper boxes it into a table for the runner.
    wrapped = (
        f"{preamble}\n"
        f"local _ok, _val = pcall(function()\n"
        f"    {assertion}\n"
        f"end)\n"
        f"return {{ ok = _ok, value = _val }}"
    )
    steps.append(Step(
        kind="execute_assert",
        payload=wrapped,
        note=f"expect={fixture['expect']!r}",
    ))

    return steps


# ---------------------------------------------------------------------------
# Plan execution
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    step: Step
    ok: bool
    output: Any = None
    error: str | None = None


@dataclass
class FixtureResult:
    fixture_id: str
    passed: bool
    step_results: list[StepResult] = field(default_factory=list)
    assertion_value: Any = None
    error: str | None = None


# Injection types: the runner calls these to interact with Studio. The
# real impls live in the Claude Code MCP layer; tests pass fakes.
ExecuteLuauFn = Callable[[str], Any]
KeyboardInputFn = Callable[[list[dict]], Any]
MouseInputFn = Callable[[list[dict]], Any]
SleepFn = Callable[[float], None]


def _values_match(got: Any, expected: Any, tolerance: float | None) -> bool:
    """Compare an assertion's return value against the fixture's expect.

    Numbers compare with absolute tolerance when one is supplied; falls
    back to exact equality. Booleans, strings, and nil compare exactly.
    """
    if isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        tol = float(tolerance) if tolerance is not None else 0.0
        return abs(float(got) - float(expected)) <= tol
    return got == expected


def run_fixture(
    fixture: dict,
    preamble: str,
    *,
    execute_luau: ExecuteLuauFn,
    keyboard_input: KeyboardInputFn,
    mouse_input: MouseInputFn,
    sleep: SleepFn = time.sleep,
) -> FixtureResult:
    """Execute a single fixture and return a structured result.

    The three MCP callables are the only side-effect surface. ``sleep``
    is injected so unit tests can override it with a no-op.
    """
    fid = fixture["id"]
    plan = plan_for_fixture(fixture, preamble)
    result = FixtureResult(fixture_id=fid, passed=False)

    for step in plan:
        try:
            if step.kind == "safety_check_studio":
                # Cheap inline check before any other work touches Studio.
                guard = (
                    'assert(game.Name ~= "Agas Map of London", '
                    '"refusing to run on Agas Map of London Studio")\n'
                    "return true"
                )
                out = execute_luau(guard)
                result.step_results.append(StepResult(step=step, ok=True, output=out))

            elif step.kind == "execute_setup":
                out = execute_luau(step.payload)
                result.step_results.append(StepResult(step=step, ok=True, output=out))

            elif step.kind == "keyboard_input":
                action = {k: v for k, v in step.payload.items() if k != "kind"}
                out = keyboard_input([action])
                result.step_results.append(StepResult(step=step, ok=True, output=out))

            elif step.kind == "mouse_input":
                action = {k: v for k, v in step.payload.items() if k != "kind"}
                out = mouse_input([action])
                result.step_results.append(StepResult(step=step, ok=True, output=out))

            elif step.kind == "wait":
                sleep(step.payload["seconds"])
                result.step_results.append(StepResult(step=step, ok=True))

            elif step.kind == "execute_assert":
                out = execute_luau(step.payload)
                # Out shape: { "ok": bool, "value": <any> }. ok=false means
                # the user's assertion raised — surface that distinctly so
                # the failure message points at the Luau error, not at the
                # numeric/value mismatch.
                if not isinstance(out, dict):
                    raise RuntimeError(
                        f"assertion returned non-dict {out!r} — "
                        f"execute_luau adapter must return the dict literal "
                        f"from the wrapped script"
                    )
                if not out.get("ok"):
                    raise RuntimeError(f"assertion raised: {out.get('value')!r}")
                result.assertion_value = out.get("value")
                tolerance = fixture.get("tolerance")
                result.passed = _values_match(
                    result.assertion_value, fixture["expect"], tolerance,
                )
                result.step_results.append(StepResult(
                    step=step, ok=result.passed, output=out,
                    error=None if result.passed else (
                        f"expected {fixture['expect']!r}, "
                        f"got {result.assertion_value!r}"
                    ),
                ))

        except Exception as exc:
            result.step_results.append(StepResult(
                step=step, ok=False, error=str(exc),
            ))
            result.error = str(exc)
            return result

    return result


def load_fixtures(behavior_path: Path) -> tuple[str, list[dict]]:
    """Convenience: validate + return ``(preamble, fixtures)``."""
    data = validate_behavior_file(behavior_path)
    preamble = data.get("_schema", {}).get("preamble", "")
    return preamble, data["fixtures"]


def iter_fixtures(
    behavior_path: Path,
    *,
    only: Iterable[str] | None = None,
) -> Iterable[dict]:
    """Yield fixtures in declaration order, honouring an optional id filter."""
    _, fixtures = load_fixtures(behavior_path)
    if only is None:
        yield from fixtures
        return
    keep = set(only)
    for f in fixtures:
        if f["id"] in keep:
            yield f
