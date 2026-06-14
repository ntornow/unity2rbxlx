"""Regenerate the contract-verifier corpus fixtures from REAL generic-mode
conversions.

The fast corpus gate (``tests/test_contract_corpus.py``) replays committed
fixtures through the real ``_run_contract_verifier`` hook. Those fixtures MUST
come from real AI conversions — synthetic stubs can't exercise checks B/C (they
scan emitted Luau). This tool produces them.

Cost: one real generic-mode conversion per project. SLOW on a cold AI-transpile
cache (~25 min/project); fast on a warm cache (cache replay, no AI calls). The
``_AI_SYSTEM_PROMPT`` is byte-frozen, so a committed cache stays valid.

Capture point: the tool monkeypatches ``Pipeline._run_contract_verifier`` to
record the EXACT ``(topology, scripts)`` the hook receives — the topology dict
is only assembled in the in-scope merged ``scene_runtime`` (never persisted to
``ctx`` in that shape), so it can't be reconstructed from ``conversion_plan.json``.
It then aborts the run right after the hook (the artifact at the hook IS the
verifier's real input; terrain/rbxlx emission downstream is irrelevant and slow).

Anti-tautology: the fixture stores the per-check count of REAL (``warning``)
violations the captured conversion produced, and this tool REFUSES to write a
fixture whose real-violation count is non-zero (a corpus fixture is a known-good
baseline by construction). So a regen that surfaces a new real violation fails
here loudly instead of silently re-baselining a dirty snapshot.

Usage:
    python3 tools/regen_contract_corpus.py [PROJECT ...]   # default: all
Env:
    LLM_CACHE_DIR                 reuse a warm AI-transpile cache
    CONTRACT_CORPUS_PROJECTS_ROOT path to populated test_projects (default:
                                  ../test_projects relative to the repo root;
                                  test_projects are git submodules — empty in a
                                  worktree, so point this at the main checkout)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent  # converter/
sys.path.insert(0, str(_REPO))

# Projects known to complete a generic-mode cold conversion (see design doc
# §"Phase 3" slice 4/6). Add a project here only after confirming it converts.
# Each spec: ``networking`` mode, and EXACTLY ONE of:
#   ``under_projects_root`` — path under CONTRACT_CORPUS_PROJECTS_ROOT (default
#     ../test_projects; the bundled git-submodule projects live there), or
#   ``under_repo`` — path under the converter/ repo (committed in-tree fixtures
#     like the MiniNet networked project that exercises check C — SimpleFPS is
#     single-domain so it has 0 cross-domain edges).
CORPUS: dict[str, dict[str, str]] = {
    "SimpleFPS": {"networking": "none", "under_projects_root": "SimpleFPS"},
    "MiniNet": {
        "networking": "mirror",
        "under_repo": "tests/fixtures/corpus_projects/MiniNet",
    },
}

# RbxScript fields the verifier reads (check A: script_type/parent_path/name;
# check B: source/name/parent_path). Captured verbatim so the replay is faithful.
_FIELDS = (
    "name",
    "source",
    "script_type",
    "intrinsic_script_type",
    "parent_path",
    "source_path",
    "requires_part_parent",
    # The child-ref resolution tally the check-D backstop reads. Without it the
    # field is never copied into fixture.json and check D goes DEAD on the corpus.
    "child_ref_resolution",
    # The rig-retarget binding carrier the binding-present fail-closed check reads.
    # The captured dict carries ALL FIVE keys verbatim ({field, child, present,
    # cam_receiver, cam_ordinal}); cam_receiver/cam_ordinal are check D's dead-write
    # exemption anchor. Without "rig_binding" in _FIELDS the carrier is never copied
    # into fixture.json and rig_binding_present goes DEAD on the corpus (abstains
    # green-for-the-wrong-reason instead of asserting the discharged Player binding).
    # The fixture assertion in test_contract_corpus.py confirms both keys land on the
    # committed Player carrier.
    "rig_binding",
)

_FIXTURE_ROOT = _REPO / "tests" / "fixtures" / "contract_corpus"


class _CaptureDone(Exception):
    """Sentinel raised after the hook fires to abort the slow downstream steps."""


def _capture(project: str, project_path: Path, networking: str) -> dict[str, object]:
    from converter.contract_verifier import verify_contract
    from converter.pipeline import Pipeline

    captured: dict[str, object] = {}
    orig = Pipeline._run_contract_verifier

    def _wrap(self: Pipeline, scene_runtime: dict[str, object]) -> None:  # type: ignore[no-untyped-def]
        captured["topology"] = scene_runtime.get("topology", {})
        # Omit a field when its captured value is None so an optional fact
        # (child_ref_resolution, rig_binding) is ABSENT on the scripts that lack
        # it — never serialized as a null spray. The verifier treats absent and
        # null identically (both -> abstain), so this is a pure-serialization
        # consistency choice that keeps the committed diff scoped to the scripts
        # that actually carry the fact.
        captured["scripts"] = [
            {f: v for f in _FIELDS if (v := getattr(s, f)) is not None}
            for s in (self.state.rbx_place.scripts or [])
        ]
        raise _CaptureDone

    Pipeline._run_contract_verifier = _wrap  # type: ignore[method-assign]
    try:
        with tempfile.TemporaryDirectory() as out:
            pipeline = Pipeline(
                unity_project_path=str(project_path),
                output_dir=out,
                skip_upload=True,
            )
            pipeline.ctx.scene_runtime_mode = "generic"
            pipeline.ctx.networking_mode = networking
            try:
                pipeline.run_all()
            except _CaptureDone:
                pass
    finally:
        Pipeline._run_contract_verifier = orig  # type: ignore[method-assign]

    if "topology" not in captured:
        raise SystemExit(
            f"[{project}] verifier hook never fired — conversion did not reach "
            f"_run_contract_verifier (not a generic-mode topology build?)"
        )

    # Recompute the verdict from the captured inputs (the source of truth the
    # replay test will also use) and gate on zero real violations.
    from core.roblox_types import RbxScript

    scripts = [RbxScript(**s) for s in captured["scripts"]]  # type: ignore[arg-type]
    result = verify_contract(captured["topology"], scripts)  # type: ignore[arg-type]
    counts: dict[str, int] = {}
    real = 0
    for v in result.violations:
        counts[v.check] = counts.get(v.check, 0) + 1
        if v.severity == "warning":
            real += 1
    if real:
        offending = [
            f"{v.check}/{v.script}: {v.detail}"
            for v in result.violations
            if v.severity == "warning"
        ]
        raise SystemExit(
            f"[{project}] REFUSING to write fixture: {real} real (warning) "
            f"violation(s) in a corpus baseline — fix the producer first:\n  "
            + "\n  ".join(offending)
        )

    return {
        "project": project,
        "captured_via": "u2r.py convert --scene-runtime=generic (real AI)",
        "expected_counts": counts,  # info-severity rows allowed; warnings == 0
        # Coverage facts the replay test asserts so a "0 violations" result can't
        # be vacuous (codex review P1): a flipped check that silently scanned
        # NOTHING would otherwise pass green-for-the-wrong-reason. Pins the
        # number of constructs each check actually ran over.
        "coverage": _coverage(captured["topology"], scripts),  # type: ignore[arg-type]
        "topology": captured["topology"],
        "scripts": captured["scripts"],
    }


def _coverage(topology: dict[str, object], scripts: list[object]) -> dict[str, int]:
    """Count the constructs each flipped check actually scans, so the replay
    test can prove the check ran (not just that it found nothing)."""
    from converter.contract_verifier import _GETCOMPONENT_RE  # type: ignore[attr-defined]

    # Check B: literal-arg GetComponent sites across emitted Luau.
    getcomponent_sites = sum(
        len(_GETCOMPONENT_RE.findall(getattr(s, "source", "") or ""))
        for s in scripts
    )
    # Check C: runtime client<->server cross-domain edges.
    runtime_edges = 0
    for e in topology.get("cross_domain_edges") or []:  # type: ignore[union-attr]
        if not isinstance(e, dict):
            continue
        fd, td = e.get("from_domain"), e.get("to_domain")
        if fd in ("client", "server") and td in ("client", "server") and fd != td:
            runtime_edges += 1
    # Check A: modules carrying a real domain verdict.
    modules = topology.get("modules") or {}  # type: ignore[union-attr]
    domained_modules = sum(
        1
        for m in modules.values()
        if isinstance(m, dict)
        and m.get("domain") in ("client", "server", "helper", "excluded")
    )
    return {
        "getcomponent_sites": getcomponent_sites,
        "runtime_cross_domain_edges": runtime_edges,
        "domained_modules": domained_modules,
    }


def _resolve_project_path(spec: dict[str, str], projects_root: Path) -> Path:
    if "under_projects_root" in spec:
        return projects_root / spec["under_projects_root"]
    if "under_repo" in spec:
        return _REPO / spec["under_repo"]
    raise SystemExit(
        f"corpus spec {spec!r} has neither under_projects_root nor under_repo"
    )


def main(argv: list[str]) -> int:
    projects = argv or list(CORPUS)
    projects_root = Path(
        os.environ.get(
            "CONTRACT_CORPUS_PROJECTS_ROOT", str(_REPO.parent / "test_projects")
        )
    )
    for project in projects:
        if project not in CORPUS:
            raise SystemExit(f"unknown corpus project {project!r}; known: {list(CORPUS)}")
        spec = CORPUS[project]
        project_path = _resolve_project_path(spec, projects_root)
        if not project_path.exists():
            raise SystemExit(
                f"[{project}] unity project not found at {project_path} — for a "
                f"projects-root project set CONTRACT_CORPUS_PROJECTS_ROOT to a "
                f"populated test_projects dir"
            )
        print(
            f"[{project}] converting (generic, networking={spec['networking']}) "
            f"from {project_path} ..."
        )
        fixture = _capture(project, project_path, spec["networking"])
        out_dir = _FIXTURE_ROOT / project
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "fixture.json"
        out_path.write_text(json.dumps(fixture, indent=2, sort_keys=True), encoding="utf-8")
        print(
            f"[{project}] wrote {out_path} "
            f"({len(fixture['scripts'])} scripts, counts={fixture['expected_counts']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
