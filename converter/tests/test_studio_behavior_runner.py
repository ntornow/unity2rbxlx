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
    iter_fixtures,
    load_fixtures,
    plan_for_fixture,
    run_fixture,
    validate_behavior_file,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "upload_snapshots"


class TestValidateBehaviorFile:
    """Catch malformed behavior fixtures before the runner ever boots Studio."""

    def test_simplefps_behavior_is_well_formed(self) -> None:
        # The committed SimpleFPS fixture set is the canonical example —
        # this test fails if a future edit breaks the schema.
        path = _FIXTURES_DIR / "SimpleFPS.behavior.json"
        if not path.exists():
            pytest.skip("SimpleFPS.behavior.json not present")
        data = validate_behavior_file(path)
        assert data["_schema"]["version"] == 1
        assert data["fixtures"], "expected at least one fixture"

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
