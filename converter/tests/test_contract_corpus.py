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

from converter.contract_verifier import _GETCOMPONENT_RE  # noqa: E402
from converter.pipeline import Pipeline  # noqa: E402
from core.roblox_types import RbxPlace, RbxScript  # noqa: E402

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "contract_corpus"


def _recompute_coverage(topology: dict, scripts: list[RbxScript]) -> dict[str, int]:
    """Re-derive the coverage facts from the fixture, independently of the
    pinned values — so a vacuous fixture (a check that scanned nothing) is
    caught. Mirrors ``tools/regen_contract_corpus._coverage``."""
    getcomponent_sites = sum(
        len(_GETCOMPONENT_RE.findall(s.source or "")) for s in scripts
    )
    runtime_edges = 0
    for e in topology.get("cross_domain_edges") or []:
        if isinstance(e, dict):
            fd, td = e.get("from_domain"), e.get("to_domain")
            if fd in ("client", "server") and td in ("client", "server") and fd != td:
                runtime_edges += 1
    domained = sum(
        1
        for m in (topology.get("modules") or {}).values()
        if isinstance(m, dict)
        and m.get("domain") in ("client", "server", "helper", "excluded")
    )
    return {
        "getcomponent_sites": getcomponent_sites,
        "runtime_cross_domain_edges": runtime_edges,
        "domained_modules": domained,
    }


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

    def test_check_d_witnesses_abstain_seam(
        self, fixture_path: Path, tmp_path: Path
    ) -> None:
        """The SimpleFPS Player entry carries ``child_ref_resolution {1,0}`` (a
        surviving ``cam:GetChildren()[1]`` ordinal whose receiver is the foreign
        ``Camera.main.transform``), so the LIVE hook must emit exactly one
        non-promoting ``info`` ``child_ordinal_coverage_gap`` row attributed to
        ``Player`` — proving the FT stamp -> verify -> abstain seam fires on real
        captured corpus output, not just the synthetic unit test. Other fixtures
        carry no such ref, so the seam is absent there."""
        fx = self._load(fixture_path)
        scripts = [RbxScript(**s) for s in fx["scripts"]]
        rows = _run_hook(fx["topology"], scripts, tmp_path)
        gaps = [r for r in rows if r["check"] == "child_ordinal_coverage_gap"]
        has_player_stamp = any(
            (s.child_ref_resolution or {}).get("getchild_total", 0) > 0
            and (s.child_ref_resolution or {}).get("resolved_total", 0)
            < (s.child_ref_resolution or {}).get("getchild_total", 0)
            for s in scripts
        )
        if has_player_stamp:
            assert [r["script"] for r in gaps] == ["Player"], (
                f"{fixture_path.parent.name}: expected exactly one "
                f"child_ordinal_coverage_gap row on Player, got "
                f"{[(r['script'], r['severity']) for r in gaps]}"
            )
            assert all(r["severity"] == "info" for r in gaps), (
                "coverage-gap row must be info-severity (never promoted)"
            )
        else:
            assert gaps == [], (
                f"{fixture_path.parent.name}: no partial child_ref_resolution "
                f"stamp, so no coverage-gap row expected, got {gaps}"
            )

    def test_coverage_matches_pinned(self, fixture_path: Path) -> None:
        """The pinned coverage facts match a fresh re-derivation — guards
        against a fixture whose scripts/topology shape silently changed so a
        flipped check no longer scans anything (codex review P1: "0 violations"
        must not be vacuous)."""
        fx = self._load(fixture_path)
        scripts = [RbxScript(**s) for s in fx["scripts"]]
        recomputed = _recompute_coverage(fx["topology"], scripts)
        assert recomputed == fx["coverage"], (
            f"{fixture_path.parent.name}: coverage drifted from pinned "
            f"{fx['coverage']} -> {recomputed}; re-run regen if intended."
        )


@pytest.mark.skipif(not _FIXTURES, reason="no contract-corpus fixtures committed yet")
def test_corpus_exercises_every_flipped_check() -> None:
    """The corpus AS A WHOLE must non-vacuously exercise every flipped check —
    else a "clean" gate proves nothing for that check. A (domained modules) +
    B (GetComponent sites) + C (runtime cross-domain edges) each need >0 across
    the committed fixtures. (Today: A+B from SimpleFPS, C from MiniNet.)"""
    totals = {"getcomponent_sites": 0, "runtime_cross_domain_edges": 0, "domained_modules": 0}
    for fp in _FIXTURES:
        cov = json.loads(fp.read_text(encoding="utf-8"))["coverage"]
        for k in totals:
            totals[k] += cov[k]
    assert totals["domained_modules"] > 0, "check A (consumer_compliance) unexercised"
    assert totals["getcomponent_sites"] > 0, "check B (component_availability) unexercised"
    assert totals["runtime_cross_domain_edges"] > 0, (
        "check C (cross_domain_attribute) unexercised — the corpus has no "
        "runtime client<->server edges; C's clean metric would be vacuous"
    )


@pytest.mark.skipif(not _FIXTURES, reason="no contract-corpus fixtures committed yet")
def test_corpus_exercises_rig_binding_present_check() -> None:
    """The rig_binding_present FAIL_CLOSED check must be corpus-EXERCISED, not
    vacuously green by abstain. ``rig_binding_present`` only fires when a script
    carries a non-None ``rig_binding`` carrier; a future regen that dropped the
    carrier (or that produced ``present=False``) would let the check silently
    abstain green for the wrong reason. Assert the corpus carries at least one
    DISCHARGED binding (``present=True``) AND that the carrier is backed by a
    REAL discharge in the captured Luau — the injected per-instance resolver
    method plus a rerouted consumer read — so a carrier-present-but-source-
    undischarged regen is caught too. Generic: scans every fixture/script, keys
    on the carrier's own field/child names (no game-specific hardcode)."""
    discharged: list[tuple[str, str, dict]] = []
    for fp in _FIXTURES:
        fx = json.loads(fp.read_text(encoding="utf-8"))
        for s in fx["scripts"]:
            rb = s.get("rig_binding")
            if isinstance(rb, dict) and rb.get("present") is True:
                discharged.append((fp.parent.name, s["name"], s))

    assert discharged, (
        "rig_binding_present unexercised — no corpus script carries a "
        "rig_binding with present=True, so the FAIL_CLOSED check abstains "
        "green-for-the-wrong-reason. A regen must capture the discharged "
        "Player binding (re-run tools/regen_contract_corpus.py)."
    )

    # Prove the discharge is REAL, not just a carrier flag: the captured source
    # must contain the injected resolver method for the carrier's child AND a
    # rerouted consumer read that calls it. Derive both names from the carrier
    # so this stays generic (no hardcoded weaponSlot/WeaponSlot).
    for project, script_name, s in discharged:
        rb = s["rig_binding"]
        child = rb.get("child")
        src = s.get("source") or ""
        resolver_def = f"function {script_name}:_resolve{child}("
        resolver_call = f"self:_resolve{child}()"
        assert resolver_def in src, (
            f"{project}/{script_name}: rig_binding present=True but the injected "
            f"resolver method ({resolver_def!r}) is absent from the captured "
            f"source — carrier-present-but-undischarged; the lowering did not "
            f"reroute the binding. Re-run tools/regen_contract_corpus.py."
        )
        assert resolver_call in src, (
            f"{project}/{script_name}: rig_binding present=True and the resolver "
            f"is defined, but no consumer read was rerouted to it "
            f"({resolver_call!r} absent) — the discharge is vacuous. "
            f"Re-run tools/regen_contract_corpus.py."
        )
