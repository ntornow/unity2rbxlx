"""Fast corpus gate for the Phase 3 contract verifier (slice 4).

Replays committed fixtures — captured from REAL generic-mode AI conversions by
``tools/regen_contract_corpus.py`` — through the LIVE ``_run_contract_verifier``
pipeline hook (NOT the bare ``verify_contract`` pure function). Driving the real
hook is deliberate: the fail-closed flip (slice 4b+) lands in that hook, so a
pure-function check would be green-for-the-wrong-reason on any wiring drift
(topology read from the merged dict, the REPLACE-not-append stash, the metric
key). The fixtures are real AI output because checks B/C scan emitted Luau —
synthetic stubs can't exercise them.

This is the gate each per-check fail-closed flip must pass: the metric must be
clean (zero ``warning`` rows) across the runnable corpus before that check flips.

Anti-tautology: the fixture pins the per-check counts the capture produced AND
``regen_contract_corpus.py`` refuses to write a fixture with any real (warning)
violation. So this test fails if EITHER the verifier starts emitting a NEW
violation on known-good output (regression) OR a committed baseline was ever
dirty. ``info``-severity rows (unverifiable joins) are allowed and pinned.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.pipeline import Pipeline  # noqa: E402
from core.roblox_types import RbxPlace, RbxScript  # noqa: E402

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "contract_corpus"


def _corpus_fixtures() -> list[Path]:
    if not _FIXTURE_ROOT.exists():
        return []
    return sorted(_FIXTURE_ROOT.glob("*/fixture.json"))


def _ids(paths: list[Path]) -> list[str]:
    return [p.parent.name for p in paths]


def _run_hook(topology: dict, scripts: list[RbxScript], tmp_path: Path) -> list[dict]:
    """Drive the REAL pipeline hook and return the recorded metric rows."""
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()

    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = scripts
    pipeline.ctx.scene_runtime = {}
    pipeline._run_contract_verifier({"topology": topology})
    return pipeline.ctx.scene_runtime.get("contract_check_violations", [])


_FIXTURES = _corpus_fixtures()


@pytest.mark.skipif(not _FIXTURES, reason="no contract-corpus fixtures committed yet")
@pytest.mark.parametrize("fixture_path", _FIXTURES, ids=_ids(_FIXTURES))
class TestContractCorpus:
    def _load(self, fixture_path: Path) -> dict:
        return json.loads(fixture_path.read_text(encoding="utf-8"))

    def test_no_real_violations(self, fixture_path: Path, tmp_path: Path) -> None:
        """The live verifier produces ZERO warning-severity violations on this
        real generic-mode conversion. This is the per-check flip gate."""
        fx = self._load(fixture_path)
        scripts = [RbxScript(**s) for s in fx["scripts"]]
        rows = _run_hook(fx["topology"], scripts, tmp_path)
        warnings = [r for r in rows if r.get("severity") == "warning"]
        assert warnings == [], (
            f"{fixture_path.parent.name}: live verifier surfaced "
            f"{len(warnings)} real violation(s) on a known-good corpus "
            f"conversion: {[ (w['check'], w['script'], w['detail']) for w in warnings ]}"
        )

    def test_counts_match_pinned_baseline(
        self, fixture_path: Path, tmp_path: Path
    ) -> None:
        """Per-check counts match the pinned baseline — catches a verifier
        change that adds/drops rows (incl. info-severity) on fixed input."""
        fx = self._load(fixture_path)
        scripts = [RbxScript(**s) for s in fx["scripts"]]
        rows = _run_hook(fx["topology"], scripts, tmp_path)
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["check"]] = counts.get(r["check"], 0) + 1
        assert counts == fx["expected_counts"], (
            f"{fixture_path.parent.name}: per-check counts drifted from the "
            f"pinned baseline {fx['expected_counts']} -> {counts}. If this is an "
            f"intended verifier change, re-run tools/regen_contract_corpus.py."
        )
