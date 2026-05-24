#!/usr/bin/env python3
"""Post-conversion validator for the /e2e-test skill.

After /convert-unity finishes assembling, this script runs the same
artifact-level assertions that ``test_offline_assembly`` enforced when
the /e2e-test skill previously delegated to that pytest. Exits 0 if
all assertions pass; non-zero (with a precise reason) otherwise.

Without this validator, /e2e-test's only conversion-half success
condition would be "``converted_place.rbxlx`` exists" — a broken
conversion (asset resolution gap, mesh ID mismatch, scene-runtime
contract incomplete, transpile syntax error) would slip silently
through the conversion half if no gameplay fixture happened to touch
the defect.

Coverage today:
  * no ``rbxassetid://0`` placeholders in the rbxlx
  * mesh IDs in the rbxlx match snapshot
  * generic-mode (when ``--mode generic``) runtime contract embeds
    ``scene_prefab_placements`` + ``_constructPrefabClone``
  * ``luau-analyze`` clean across ``<output_dir>/scripts/`` (when
    ``luau-analyze`` is installed; soft-skipped when absent)

Not yet covered — require Pipeline in-memory state that isn't
persisted to disk after run_all():
  * snapshot drift gate (``ctx.asset_manifest`` vs snapshot)
  * place-builder chunk publishability (largest chunk under the
    4 MB ``execute_luau`` cap)
Landing these requires either persisting the manifest + a
chunked-size summary to disk during assemble, or having this
validator partially re-run a Pipeline pass. Both are deferred.

Usage:
    python3 -m tools.validate_e2e_conversion <output_dir> <project_name> [--mode legacy|generic]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Import path: tools/ is sibling to tests/; both live under the
# converter root. Walk up one to import from tests.
_THIS = Path(__file__).resolve()
_CONVERTER_ROOT = _THIS.parent.parent
sys.path.insert(0, str(_CONVERTER_ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Validate a /convert-unity output dir against the "
                    "/e2e-test conversion-half contract.",
    )
    ap.add_argument(
        "output_dir", type=Path,
        help="The output directory /convert-unity wrote to.",
    )
    ap.add_argument(
        "project_name",
        help="Project name as used by the snapshot fixture "
             "(``tests/fixtures/upload_snapshots/<project_name>.snapshot.json``).",
    )
    ap.add_argument(
        "--mode", choices=["legacy", "generic"], default="legacy",
        help="Scene-runtime mode the conversion was driven in. ``generic`` "
             "enables the host-contract embed assertion.",
    )
    args = ap.parse_args()

    output_dir: Path = args.output_dir.resolve()
    project_name: str = args.project_name
    mode: str = args.mode

    if not output_dir.is_dir():
        print(f"ERROR: output_dir does not exist: {output_dir}", file=sys.stderr)
        return 2

    # Shared post-conversion assertion helpers live in their own
    # module so production-adjacent code doesn't reach into
    # underscored test internals. ``tests.conversion_assertions`` has
    # no import-time side effects (unlike ``test_offline_assembly``,
    # which resolves SIMPLEFPS_PROJECT / TRASHDASH_PROJECT at module
    # load via pytest.skip-on-missing), so importing it here is safe
    # outside a pytest run regardless of fixture availability.
    from tests.conversion_assertions import (
        assert_generic_scene_runtime,
        assert_mesh_ids_match_snapshot,
        assert_no_placeholder_ids,
        load_snapshot,
        run_luau_analyze,
    )

    # 1) rbxlx exists
    rbxlx_files = list(output_dir.glob("*.rbxlx"))
    if not rbxlx_files:
        print("FAIL: no rbxlx in output_dir", file=sys.stderr)
        return 1
    rbxlx = rbxlx_files[0]
    print(f"OK   rbxlx present: {rbxlx.name}")

    # 2) snapshot loads cleanly
    # ``load_snapshot`` calls ``pytest.skip(...)`` on a missing
    # fixture; outside pytest, ``pytest.skip`` raises
    # ``_pytest.outcomes.Skipped`` (subclass of BaseException, NOT
    # ``SystemExit``). The path check up front makes the helper's
    # pytest.skip path unreachable in practice; the Skipped catch
    # below is belt-and-braces in case the fixture-resolution shape
    # ever changes.
    snapshot_path = (
        Path(__file__).resolve().parent.parent
        / "tests" / "fixtures" / "upload_snapshots"
        / f"{project_name}.snapshot.json"
    )
    if not snapshot_path.exists():
        print(
            f"FAIL: snapshot fixture missing: {snapshot_path}",
            file=sys.stderr,
        )
        return 1
    try:
        snapshot = load_snapshot(project_name)
    except BaseException as exc:
        # Pytest's Skipped derives from BaseException, not Exception.
        # Filter by class name so we don't swallow real bugs (KeyboardInterrupt, etc).
        if type(exc).__name__ == "Skipped":
            print(
                f"FAIL: snapshot fixture missing for {project_name!r}: {exc}",
                file=sys.stderr,
            )
            return 1
        raise
    print(f"OK   snapshot loaded: {project_name}")

    # 3) no rbxassetid://0 placeholders
    try:
        assert_no_placeholder_ids(rbxlx)
    except AssertionError as e:
        print(f"FAIL: placeholder IDs in rbxlx: {e}", file=sys.stderr)
        return 1
    print("OK   no rbxassetid://0 placeholders")

    # 4) mesh IDs match snapshot
    try:
        assert_mesh_ids_match_snapshot(rbxlx, snapshot)
    except AssertionError as e:
        print(f"FAIL: mesh ID mismatch vs snapshot: {e}", file=sys.stderr)
        return 1
    print("OK   mesh IDs match snapshot")

    # 5) generic-mode runtime contract embedded
    if mode == "generic":
        try:
            assert_generic_scene_runtime(rbxlx)
        except AssertionError as e:
            print(
                f"FAIL: generic scene-runtime contract incomplete: {e}",
                file=sys.stderr,
            )
            return 1
        print("OK   generic scene-runtime contract embedded")

    # 6) luau-analyze clean (soft-skip when not installed)
    scripts_dir = output_dir / "scripts"
    if scripts_dir.is_dir():
        passed, failed, fails = run_luau_analyze(scripts_dir)
        if failed > 0:
            print(
                f"FAIL: luau-analyze found {failed} error(s):\n  "
                + "\n  ".join(fails[:10]),
                file=sys.stderr,
            )
            return 1
        print(f"OK   luau-analyze clean ({passed} scripts checked)")
    else:
        print("WARN scripts/ missing — luau-analyze skipped")

    print(f"\nVALIDATION PASSED ({mode} mode)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
