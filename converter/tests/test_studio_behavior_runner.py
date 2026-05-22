"""
test_studio_behavior_runner.py -- Schema validation + planner/runner unit tests
for ``tests/studio_behavior_runner.py``. No Studio dependency.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.studio_behavior_runner import (  # noqa: E402
    BehaviorSchemaError,
    FixtureResult,
    Step,
    format_summary,
    iter_fixtures,
    load_fixtures,
    plan_for_fixture,
    run_fixture,
    serialize_results,
    validate_behavior_file,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "upload_snapshots"


class TestValidateBehaviorFile:
    """Catch malformed behavior fixtures before the runner ever boots Studio."""

    def test_simplefps_behavior_is_well_formed(self) -> None:
        # The committed SimpleFPS fixture set is the canonical example —
        # this test fails if a future edit breaks the schema. Schema
        # version bumped to 2 in PR-B1 when the workspace E2E mouse
        # channel + _reset() helper were introduced; bumps are expected
        # only when the preamble's contract changes shape.
        path = _FIXTURES_DIR / "SimpleFPS.behavior.json"
        if not path.exists():
            pytest.skip("SimpleFPS.behavior.json not present")
        data = validate_behavior_file(path)
        assert data["_schema"]["version"] >= 2
        assert data["fixtures"], "expected at least one fixture"
        # Codex finding #5: every fixture's setup_luau must call
        # _reset() first. Without it the suite leaks state and becomes
        # order-dependent. Pin the contract here so future fixture
        # additions can't quietly forget it.
        for f in data["fixtures"]:
            setup = f.get("setup_luau", "")
            assert setup.startswith("_reset()"), (
                f"fixture {f['id']!r} setup_luau must start with "
                f"'_reset()' (current: {setup[:60]!r}...). See _schema.isolation."
            )

    def test_rejects_missing_fixtures_array(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.behavior.json"
        p.write_text(json.dumps({"_schema": {"version": 1}}))
        with pytest.raises(BehaviorSchemaError, match="missing 'fixtures'"):
            validate_behavior_file(p)

    def test_rejects_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.behavior.json"
        p.write_text("{not json")
        with pytest.raises(BehaviorSchemaError, match="invalid JSON"):
            validate_behavior_file(p)

    def test_rejects_fixture_missing_required_field(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.behavior.json"
        p.write_text(json.dumps({
            "fixtures": [{"id": "x", "assert_luau": "return true"}],  # no 'expect'
        }))
        with pytest.raises(BehaviorSchemaError, match="missing required field 'expect'"):
            validate_behavior_file(p)

    def test_rejects_duplicate_ids(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.behavior.json"
        p.write_text(json.dumps({
            "fixtures": [
                {"id": "a", "assert_luau": "return true", "expect": True},
                {"id": "a", "assert_luau": "return false", "expect": False},
            ],
        }))
        with pytest.raises(BehaviorSchemaError, match="duplicate fixture id 'a'"):
            validate_behavior_file(p)

    def test_rejects_unknown_field(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.behavior.json"
        p.write_text(json.dumps({
            "fixtures": [{
                "id": "x", "assert_luau": "return true", "expect": True,
                "definitely_not_a_real_field": 42,
            }],
        }))
        with pytest.raises(BehaviorSchemaError, match="unknown field"):
            validate_behavior_file(p)

    def test_rejects_unknown_input_kind(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.behavior.json"
        p.write_text(json.dumps({
            "fixtures": [{
                "id": "x", "assert_luau": "return true", "expect": True,
                "input_sequence": [{"kind": "telepathy"}],
            }],
        }))
        with pytest.raises(BehaviorSchemaError, match="unknown input kind"):
            validate_behavior_file(p)

    def test_rejects_forward_dependency(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.behavior.json"
        p.write_text(json.dumps({
            "fixtures": [
                {"id": "a", "assert_luau": "return true", "expect": True,
                 "depends_on": ["b"]},
                {"id": "b", "assert_luau": "return true", "expect": True},
            ],
        }))
        with pytest.raises(BehaviorSchemaError, match="depends_on 'b'"):
            validate_behavior_file(p)


class TestPlanForFixture:
    def _bare(self, **overrides) -> dict:
        base = {"id": "f", "assert_luau": "return true", "expect": True}
        base.update(overrides)
        return base

    def test_minimal_fixture_produces_safety_and_assert(self) -> None:
        plan = plan_for_fixture(self._bare(), preamble="local x = 1")
        kinds = [s.kind for s in plan]
        assert kinds == ["safety_check_studio", "execute_assert"]

    def test_setup_setup_step_emitted(self) -> None:
        plan = plan_for_fixture(
            self._bare(setup_luau="_state.x = 42"),
            preamble="--preamble",
        )
        assert any(s.kind == "execute_setup" for s in plan)
        setup_step = next(s for s in plan if s.kind == "execute_setup")
        assert "--preamble" in setup_step.payload
        assert "_state.x = 42" in setup_step.payload

    def test_input_sequence_translates_per_kind(self) -> None:
        plan = plan_for_fixture(
            self._bare(input_sequence=[
                {"kind": "keyboard", "action": "keyDown", "key_code": "W"},
                {"kind": "mouse_click", "button": "left"},
            ]),
            preamble="",
        )
        kinds = [s.kind for s in plan]
        assert "keyboard_input" in kinds
        assert "mouse_input" in kinds

    def test_wait_seconds_emits_wait_step(self) -> None:
        plan = plan_for_fixture(
            self._bare(wait_seconds=2.5),
            preamble="",
        )
        wait = next(s for s in plan if s.kind == "wait")
        assert wait.payload["seconds"] == 2.5

    def test_assert_step_wraps_in_pcall(self) -> None:
        plan = plan_for_fixture(self._bare(), preamble="")
        assert_step = next(s for s in plan if s.kind == "execute_assert")
        assert "pcall" in assert_step.payload
        assert "return { ok = _ok, value = _val }" in assert_step.payload


class _Recorder:
    """Pluggable callable that records its calls and returns a scripted value."""

    def __init__(self, returns: object = None) -> None:
        self.calls: list[object] = []
        self.returns = returns

    def __call__(self, *args, **kwargs) -> object:
        self.calls.append((args, kwargs))
        if callable(self.returns):
            return self.returns(*args, **kwargs)
        return self.returns


class TestRunFixture:
    def _ok_fixture(self) -> dict:
        return {
            "id": "f", "assert_luau": "return 42", "expect": 42,
        }

    def _make_execute_returning(self, asserted_value: object):
        """Build an execute_luau fake that returns the right shape per call."""
        def fn(script: str) -> object:
            if "pcall" in script:
                return {"ok": True, "value": asserted_value}
            # safety check or setup — return whatever, the runner ignores it
            return True
        return fn

    def test_pass_path(self) -> None:
        result = run_fixture(
            self._ok_fixture(),
            preamble="",
            execute_luau=self._make_execute_returning(42),
            keyboard_input=_Recorder(),
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
        )
        assert result.passed
        assert result.assertion_value == 42

    def test_fail_path_mismatch(self) -> None:
        result = run_fixture(
            self._ok_fixture(),
            preamble="",
            execute_luau=self._make_execute_returning(99),
            keyboard_input=_Recorder(),
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
        )
        assert not result.passed
        assert result.assertion_value == 99
        # The assert step records an explanatory error
        assert_step = [r for r in result.step_results if r.step.kind == "execute_assert"][0]
        assert "expected 42" in (assert_step.error or "")

    def test_assertion_raises_surfaces_error(self) -> None:
        def fn(script: str) -> object:
            if "pcall" in script:
                return {"ok": False, "value": "attempt to index nil"}
            return True
        result = run_fixture(
            self._ok_fixture(),
            preamble="",
            execute_luau=fn,
            keyboard_input=_Recorder(),
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
        )
        assert not result.passed
        assert "attempt to index nil" in (result.error or "")

    def test_keyboard_input_invoked_with_action_dict(self) -> None:
        rec = _Recorder()
        fixture = {
            "id": "kb",
            "assert_luau": "return true",
            "expect": True,
            "input_sequence": [
                {"kind": "keyboard", "action": "keyDown", "key_code": "W"},
            ],
        }
        run_fixture(
            fixture, preamble="",
            execute_luau=self._make_execute_returning(True),
            keyboard_input=rec,
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
        )
        # Recorder saw one call; payload is a list with the action minus the 'kind' tag.
        assert len(rec.calls) == 1
        (actions,), _kwargs = rec.calls[0]
        assert actions == [{"action": "keyDown", "key_code": "W"}]

    def test_numeric_tolerance(self) -> None:
        fixture = {
            "id": "f", "assert_luau": "return 3.14", "expect": 3.1, "tolerance": 0.05,
        }
        result = run_fixture(
            fixture, preamble="",
            execute_luau=self._make_execute_returning(3.14),
            keyboard_input=_Recorder(),
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
        )
        assert result.passed


class TestLoadFixtures:
    def test_iter_fixtures_filters_by_id(self, tmp_path: Path) -> None:
        p = tmp_path / "b.behavior.json"
        p.write_text(json.dumps({
            "_schema": {"preamble": "--p"},
            "fixtures": [
                {"id": "a", "assert_luau": "return true", "expect": True},
                {"id": "b", "assert_luau": "return true", "expect": True},
                {"id": "c", "assert_luau": "return true", "expect": True},
            ],
        }))
        got = [f["id"] for f in iter_fixtures(p, only={"a", "c"})]
        assert got == ["a", "c"]

    def test_load_fixtures_returns_preamble_and_list(self, tmp_path: Path) -> None:
        p = tmp_path / "b.behavior.json"
        p.write_text(json.dumps({
            "_schema": {"preamble": "local x = 1"},
            "fixtures": [
                {"id": "a", "assert_luau": "return true", "expect": True},
            ],
        }))
        preamble, fixtures = load_fixtures(p)
        assert preamble == "local x = 1"
        assert len(fixtures) == 1


class TestPollingAssert:
    """Codex finding #6: `wait → assert once` is flaky. With
    ``assert_timeout_seconds > 0`` the runner re-runs the assertion until
    it matches expect or the deadline elapses."""

    def _polling_fixture(self, timeout: float = 1.0) -> dict:
        return {
            "id": "p",
            "assert_luau": "return _G._counter",
            "expect": 3,
            "assert_timeout_seconds": timeout,
        }

    def _ticking_clock(self, step: float = 0.1):
        """Monotonic-clock fake that advances by ``step`` per call."""
        state = {"t": 0.0}

        def now() -> float:
            cur = state["t"]
            state["t"] += step
            return cur

        return now

    def test_eventually_succeeds_after_polls(self) -> None:
        # The assertion returns 1, 2, 3 across calls; expect=3 should
        # pass on the third attempt rather than failing once and stopping.
        values = iter([1, 2, 3])

        def fn(script: str) -> object:
            if "pcall" in script:
                return {"ok": True, "value": next(values)}
            return True

        result = run_fixture(
            self._polling_fixture(timeout=10.0),
            preamble="",
            execute_luau=fn,
            keyboard_input=_Recorder(),
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
            monotonic=self._ticking_clock(step=0.05),
        )
        assert result.passed
        assert result.assertion_value == 3
        assert result.attempts == 3

    def test_times_out_when_value_never_matches(self) -> None:
        # The ticking clock + small timeout means we get a few attempts
        # then bail. The result records the last value seen.
        def fn(script: str) -> object:
            if "pcall" in script:
                return {"ok": True, "value": 99}
            return True

        result = run_fixture(
            self._polling_fixture(timeout=0.3),
            preamble="",
            execute_luau=fn,
            keyboard_input=_Recorder(),
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
            monotonic=self._ticking_clock(step=0.1),
        )
        assert not result.passed
        assert result.assertion_value == 99
        assert result.attempts >= 2  # at least one retry before deadline
        # The recorded error names the mismatch + attempt count.
        assert_step = [r for r in result.step_results if r.step.kind == "execute_assert"][0]
        assert "expected 3" in (assert_step.error or "")
        assert "attempt(s)" in (assert_step.error or "")

    def test_legacy_single_shot_when_timeout_absent(self) -> None:
        # No assert_timeout_seconds field => default 0.0 => one call only,
        # mismatch fails immediately. Pins the legacy contract so existing
        # fixtures don't change behavior.
        calls: list[int] = []

        def fn(script: str) -> object:
            calls.append(1)
            if "pcall" in script:
                return {"ok": True, "value": 1}
            return True

        result = run_fixture(
            {"id": "p", "assert_luau": "return 1", "expect": 99},
            preamble="",
            execute_luau=fn,
            keyboard_input=_Recorder(),
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
        )
        assert not result.passed
        assert result.attempts == 1


class TestTimingAndSchema:
    """Timing fields + the new assert_timeout_seconds schema field."""

    def test_fixture_result_records_timing(self) -> None:
        clock = iter([100.0, 100.5])

        result = run_fixture(
            {"id": "t", "assert_luau": "return 1", "expect": 1},
            preamble="",
            execute_luau=lambda _s: {"ok": True, "value": 1} if "pcall" in _s else True,
            keyboard_input=_Recorder(),
            mouse_input=_Recorder(),
            sleep=lambda _s: None,
            monotonic=lambda: next(clock),
        )
        assert result.started_at is not None
        assert result.finished_at is not None
        assert result.duration_seconds == 0.5

    def test_schema_rejects_negative_assert_timeout(self, tmp_path: Path) -> None:
        p = tmp_path / "b.behavior.json"
        p.write_text(json.dumps({
            "_schema": {"preamble": ""},
            "fixtures": [
                {
                    "id": "neg",
                    "assert_luau": "return true",
                    "expect": True,
                    "assert_timeout_seconds": -1,
                }
            ],
        }))
        with pytest.raises(BehaviorSchemaError, match="non-negative"):
            validate_behavior_file(p)

    def test_schema_accepts_zero_and_positive_assert_timeout(self, tmp_path: Path) -> None:
        p = tmp_path / "b.behavior.json"
        p.write_text(json.dumps({
            "_schema": {"preamble": ""},
            "fixtures": [
                {"id": "z", "assert_luau": "return true", "expect": True,
                 "assert_timeout_seconds": 0},
                {"id": "p", "assert_luau": "return true", "expect": True,
                 "assert_timeout_seconds": 2.5},
            ],
        }))
        data = validate_behavior_file(p)
        assert data["fixtures"][0]["assert_timeout_seconds"] == 0
        assert data["fixtures"][1]["assert_timeout_seconds"] == 2.5


class TestReporting:
    """``serialize_results`` + ``format_summary`` produce the JSON shape
    and stdout line the /e2e-test skill writes."""

    def _passing_result(self, fid: str, value: object = True) -> FixtureResult:
        r = FixtureResult(fixture_id=fid, passed=True)
        r.assertion_value = value
        r.started_at = "2026-05-21T00:00:00+00:00"
        r.finished_at = "2026-05-21T00:00:01+00:00"
        r.duration_seconds = 1.0
        r.attempts = 1
        return r

    def _failing_result(self, fid: str, value: object = False) -> FixtureResult:
        r = FixtureResult(fixture_id=fid, passed=False)
        r.assertion_value = value
        r.duration_seconds = 0.5
        r.attempts = 3
        r.error = "expected True, got False after 3 attempt(s)"
        return r

    def test_serialize_results_all_pass(self) -> None:
        fixtures = [
            {"id": "a", "feature": "1. spawn", "assert_luau": "x", "expect": True},
            {"id": "b", "feature": "2. wasd",  "assert_luau": "x", "expect": True},
        ]
        results = [self._passing_result("a"), self._passing_result("b")]
        report = serialize_results(
            fixtures, results,
            project="SimpleFPS",
            run_id="2026-05-21T00-00-00-abcdef",
            rbxlx_path="/tmp/converted_place.rbxlx",
            conversion={"passed": True, "duration_seconds": 821.4},
        )
        assert report["schema_version"] == 1
        assert report["project"] == "SimpleFPS"
        assert report["run_id"] == "2026-05-21T00-00-00-abcdef"
        assert report["conversion"]["passed"] is True
        assert report["gameplay"]["summary"] == {
            "total": 2, "passed": 2, "failed": 0,
        }
        assert [f["id"] for f in report["gameplay"]["fixtures"]] == ["a", "b"]
        # Feature text is carried through so the report is human-readable
        # without cross-referencing the behavior.json.
        assert report["gameplay"]["fixtures"][0]["feature"] == "1. spawn"

    def test_serialize_results_mixed_pass_fail(self) -> None:
        fixtures = [
            {"id": "a", "assert_luau": "x", "expect": True},
            {"id": "b", "assert_luau": "x", "expect": True},
        ]
        results = [self._passing_result("a"), self._failing_result("b")]
        report = serialize_results(
            fixtures, results,
            project="SimpleFPS",
            run_id="r",
        )
        assert report["gameplay"]["summary"] == {
            "total": 2, "passed": 1, "failed": 1,
        }
        fail = report["gameplay"]["fixtures"][1]
        assert fail["passed"] is False
        assert fail["attempts"] == 3
        assert "after 3 attempt(s)" in (fail["error"] or "")

    def test_serialize_rejects_length_mismatch(self) -> None:
        with pytest.raises(ValueError, match="length mismatch"):
            serialize_results(
                [{"id": "a", "assert_luau": "x", "expect": True}],
                [self._passing_result("a"), self._passing_result("b")],
                project="p", run_id="r",
            )

    def test_format_summary_all_pass(self) -> None:
        results = [self._passing_result("a"), self._passing_result("b")]
        s = format_summary(
            results, project="SimpleFPS",
            conversion_passed=True, conversion_duration_seconds=821.4,
        )
        assert s == "[SimpleFPS]; Conversion passed (821.4s); 2/2 fixtures passed"

    def test_format_summary_conversion_failed_skips_fixtures(self) -> None:
        s = format_summary([], project="SimpleFPS", conversion_passed=False)
        assert "Conversion FAILED" in s
        # The string mentions "fixtures skipped" but never reports any
        # pass/fail count for them — that's the real contract.
        assert "/0" not in s
        assert "passed" not in s

    def test_format_summary_names_failing_fixtures(self) -> None:
        results = [
            self._passing_result("spawn"),
            self._failing_result("mouse_yaw"),
            self._failing_result("rifle_view"),
        ]
        s = format_summary(results, project="SimpleFPS", conversion_passed=True)
        assert "1/3" in s
        assert "mouse_yaw" in s
        assert "rifle_view" in s
