"""test_contract_verifier.py -- Phase 3 slice 0 (shadow-mode skeleton).

Drives the REAL ``contract_verifier`` code + the pipeline hook. Each test
is built so a green result PROVES the wiring rather than passing for the
wrong reason:

  - The smoke check fires IFF the topology lacks ``modules`` -- so the
    "zero violations" case genuinely depends on the input being inspected.
  - The idempotency test calls the stash helper twice and asserts the row
    count is stable (resume-replay safety).
  - The hook test seeds ``ctx.scene_runtime`` with a topology that WOULD
    fire the smoke check, but passes a DIFFERENT ``scene_runtime`` (with a
    populated topology) into ``_run_contract_verifier`` -- asserting the
    hook reads the passed dict, not ``ctx.scene_runtime``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.contract_verifier import (  # noqa: E402
    ContractViolation,
    ContractVerifierResult,
    stash_violations,
    verify_contract,
    violation_to_dict,
)
from converter.pipeline import Pipeline  # noqa: E402
from core.roblox_types import RbxPlace, RbxScript  # noqa: E402


# A minimal topology artifact whose ``modules`` block is populated. The exact
# module shape is irrelevant to the slice-0 smoke check (it only inspects the
# presence of a truthy ``modules`` key).
def _topology_with_modules() -> dict[str, object]:
    return {"modules": {"guid-a": {"stem": "Foo", "runtime_bearing": True}}}


# ---------------------------------------------------------------------------
# verify_contract -- smoke check
# ---------------------------------------------------------------------------

class TestVerifyContractSmoke:
    def test_topology_with_modules_has_no_violations(self) -> None:
        """A populated topology -> zero violations (proves the smoke check is
        gated on real input, not unconditionally firing)."""
        result = verify_contract(_topology_with_modules(), [])
        assert result.total() == 0
        assert result.violations == []

    def test_topology_missing_modules_yields_one_smoke_violation(self) -> None:
        """Empty topology -> exactly one ``smoke`` violation. Proves the
        input is actually inspected (the data path is wired)."""
        result = verify_contract({}, [])
        assert result.total() == 1
        v = result.violations[0]
        assert v.check == "smoke"
        assert v.severity == "warning"
        assert v.identity == "smoke:missing-modules"

    def test_empty_modules_dict_is_treated_as_missing(self) -> None:
        """A present-but-empty ``modules`` block still fires (falsy)."""
        result = verify_contract({"modules": {}}, [])
        assert result.total() == 1
        assert result.violations[0].check == "smoke"

    def test_scripts_arg_does_not_change_slice0_result(self) -> None:
        """``scripts`` is part of the signature but unused in slice 0; a
        populated topology stays clean regardless of scripts passed."""
        scripts = [RbxScript(name="A", source="return 1")]
        assert verify_contract(_topology_with_modules(), scripts).total() == 0


# ---------------------------------------------------------------------------
# ContractVerifierResult -- counting
# ---------------------------------------------------------------------------

class TestResultCounting:
    def test_counts_by_check_and_total_on_mixed_list(self) -> None:
        violations = [
            ContractViolation("smoke", "warning", "", "d1", "smoke:1"),
            ContractViolation("consumer_compliance", "warning", "S.lua", "d2", "cc:1"),
            ContractViolation("consumer_compliance", "warning", "T.lua", "d3", "cc:2"),
        ]
        result = ContractVerifierResult(violations=violations)
        assert result.total() == 3
        assert result.counts_by_check() == {
            "smoke": 1,
            "consumer_compliance": 2,
        }

    def test_empty_result_counts(self) -> None:
        result = ContractVerifierResult()
        assert result.total() == 0
        assert result.counts_by_check() == {}


# ---------------------------------------------------------------------------
# stash_violations -- idempotency / dedup
# ---------------------------------------------------------------------------

class TestStashIdempotency:
    def test_first_stash_appends_then_replay_is_noop(self) -> None:
        """Calling the stash twice with the same result does NOT
        double-count (mirrors the resume-replay dedup)."""
        result = verify_contract({}, [])  # one smoke violation
        rows: list[dict[str, str]] = []

        first = stash_violations(rows, result)
        assert first == 1
        assert len(rows) == 1

        second = stash_violations(rows, result)
        assert second == 0
        assert len(rows) == 1  # stable -- no double count

    def test_stash_appends_only_new_identities(self) -> None:
        rows: list[dict[str, str]] = [
            {"check": "smoke", "severity": "warning", "script": "",
             "detail": "d", "identity": "smoke:missing-modules"},
        ]
        # A result whose only violation matches an existing identity.
        result = verify_contract({}, [])
        appended = stash_violations(rows, result)
        assert appended == 0
        assert len(rows) == 1

    def test_stash_rows_are_json_serializable_dicts(self) -> None:
        result = verify_contract({}, [])
        rows: list[dict[str, str]] = []
        stash_violations(rows, result)
        assert rows[0] == {
            "check": "smoke",
            "severity": "warning",
            "script": "",
            "detail": result.violations[0].detail,
            "identity": "smoke:missing-modules",
        }

    def test_violation_to_dict_round_trips_fields(self) -> None:
        v = ContractViolation("smoke", "warning", "s.lua", "detail", "id:1")
        assert violation_to_dict(v) == {
            "check": "smoke",
            "severity": "warning",
            "script": "s.lua",
            "detail": "detail",
            "identity": "id:1",
        }


# ---------------------------------------------------------------------------
# Pipeline hook -- _run_contract_verifier
# ---------------------------------------------------------------------------

def _make_pipeline(tmp_path: Path) -> Pipeline:
    unity_project = tmp_path / "unity"
    unity_project.mkdir()
    (unity_project / "Assets").mkdir()
    output = tmp_path / "out"
    output.mkdir()

    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts.append(
        RbxScript(name="HelloScript", source="return 1", script_type="Script")
    )
    return pipeline


class TestRunContractVerifierHook:
    def test_hook_reads_passed_scene_runtime_not_ctx(self, tmp_path: Path) -> None:
        """The hook must read topology from its ``scene_runtime`` ARG, not
        from ``ctx.scene_runtime`` (which never receives the topology block).

        Seed ctx with a topology that WOULD fire the smoke check (no
        ``modules``); pass a scene_runtime whose topology HAS modules. If the
        hook (wrongly) read ctx, it would record a smoke violation -- we
        assert it records ZERO, proving it read the passed dict."""
        pipeline = _make_pipeline(tmp_path)
        # ctx topology lacks modules -> would fire smoke if read.
        pipeline.ctx.scene_runtime = {"topology": {}}

        passed = {"topology": _topology_with_modules()}
        pipeline._run_contract_verifier(passed)

        rows = pipeline.ctx.scene_runtime.get("contract_check_violations", [])
        assert rows == []

    def test_hook_records_violation_from_passed_topology(self, tmp_path: Path) -> None:
        """Conversely, a passed topology MISSING modules records the smoke
        violation on ctx -- even though ctx's own topology has modules."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime = {"topology": _topology_with_modules()}

        passed = {"topology": {}}  # missing modules -> smoke fires
        pipeline._run_contract_verifier(passed)

        rows = pipeline.ctx.scene_runtime.get("contract_check_violations", [])
        assert len(rows) == 1
        assert rows[0]["check"] == "smoke"
        assert rows[0]["identity"] == "smoke:missing-modules"

    def test_hook_is_resume_idempotent(self, tmp_path: Path) -> None:
        """Running the hook twice (resume replay) does not double-count."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime = {}

        passed = {"topology": {}}  # missing modules -> one smoke violation
        pipeline._run_contract_verifier(passed)
        pipeline._run_contract_verifier(passed)

        rows = pipeline.ctx.scene_runtime.get("contract_check_violations", [])
        assert len(rows) == 1

    def test_hook_drops_stale_rows_on_clean_resume(self, tmp_path: Path) -> None:
        """Resume regression (codex slice-0 P2): ctx.scene_runtime persists +
        reloads across a resume, so a violation row recorded by a PRIOR run is
        present when the hook re-runs. If the current run is now clean, that
        stale row MUST be dropped -- the verifier replaces its rows, not
        appends. (Fails against the pre-fix setdefault+append stash, which
        would keep the stale row forever.)"""
        pipeline = _make_pipeline(tmp_path)
        # Simulate a reloaded context carrying a stale violation from a run
        # whose underlying issue is now fixed.
        pipeline.ctx.scene_runtime = {
            "contract_check_violations": [
                {
                    "check": "smoke",
                    "severity": "warning",
                    "script": "",
                    "detail": "stale from prior run",
                    "identity": "smoke:missing-modules",
                },
            ],
        }

        passed = {"topology": _topology_with_modules()}  # clean -> no violations
        pipeline._run_contract_verifier(passed)

        rows = pipeline.ctx.scene_runtime.get("contract_check_violations", [])
        assert rows == []

    def test_hook_replaces_stale_rows_with_current_run(
        self, tmp_path: Path
    ) -> None:
        """A stale row with a DIFFERENT identity than the current run's
        violation is dropped too -- the metric reflects only the current run,
        never a union with reloaded history."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime = {
            "contract_check_violations": [
                {
                    "check": "consumer_compliance",
                    "severity": "warning",
                    "script": "Door.luau",
                    "detail": "stale, different identity",
                    "identity": "consumer_compliance:Door.luau:stale",
                },
            ],
        }

        passed = {"topology": {}}  # missing modules -> current smoke fires
        pipeline._run_contract_verifier(passed)

        rows = pipeline.ctx.scene_runtime.get("contract_check_violations", [])
        assert len(rows) == 1
        assert rows[0]["identity"] == "smoke:missing-modules"

    def test_env_hatch_disables_verifier(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """U2R_CONTRACT_VERIFIER_DISABLE truthy -> verifier short-circuits,
        no rows recorded even though the topology would fire smoke."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime = {}
        monkeypatch.setenv("U2R_CONTRACT_VERIFIER_DISABLE", "1")

        pipeline._run_contract_verifier({"topology": {}})

        assert "contract_check_violations" not in pipeline.ctx.scene_runtime
