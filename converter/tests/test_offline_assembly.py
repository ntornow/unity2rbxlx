"""
test_offline_assembly.py -- Offline full-conversion regression with cached IDs.

These tests answer the question "does the converter still produce a
publish-ready place file?" without touching Open Cloud or publishing a
real place. The trick is to pre-seed ``conversion_context.json`` with a
committed snapshot of ``uploaded_assets`` + ``mesh_native_sizes`` +
``mesh_hierarchies`` from a real prior conversion, then run the pipeline
with ``skip_upload=True``. ``pipeline.upload_assets`` and
``pipeline.resolve_assets`` no-op, but downstream phases see real Roblox
asset IDs from the snapshot and the assembled rbxlx ends up identical
in shape to one that came from a real upload.

What this catches:
  * Scene assembly regressions that emit ``rbxassetid://0`` placeholders
  * Asset key shape drift (e.g. embedded-mesh keying convention change)
  * Mesh resolution lookup regressions in convert_scene
  * Luau syntax regressions in transpiled scripts (via luau-analyze)
  * place_builder chunking regressions that would exceed the 4MB
    execute_luau cap

What it cannot catch (by design):
  * Asset upload itself (cloud_api code paths)
  * New assets that have never been uploaded — the drift gate fails
    loudly so the snapshot can be refreshed
  * SavePlaceAsync / PublishPlace correctness

Snapshot refresh: ``python u2r.py snapshot-ids <output_dir> -o <fixture>``
after a real conversion.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests._project_paths import SIMPLEFPS_PATH, is_populated, resolve_project  # noqa: E402
from tests.conversion_assertions import (  # noqa: E402
    _FIXTURES,
    assert_generic_scene_runtime,
    assert_mesh_ids_match_snapshot,
    assert_no_placeholder_ids,
    assert_place_builder_chunks_publishable,
    assert_snapshot_covers_manifest,
    load_snapshot,
    run_luau_analyze,
    seed_output_dir,
)

# Back-compat aliases: PR #143 wired tools/validate_e2e_conversion.py and the
# /e2e-test SKILL.md seed snippet to import these as underscored names from
# this module. The helpers moved to ``tests.conversion_assertions`` in a later
# cleanup; the aliases keep any straggling import working until the next
# consumer migration. New code should ``from tests.conversion_assertions
# import ...`` directly.
_load_snapshot = load_snapshot
_seed_output_dir = seed_output_dir
_assert_no_placeholder_ids = assert_no_placeholder_ids
_assert_snapshot_covers_manifest = assert_snapshot_covers_manifest
_assert_mesh_ids_match_snapshot = assert_mesh_ids_match_snapshot
_assert_generic_scene_runtime = assert_generic_scene_runtime
_assert_place_builder_chunks_publishable = assert_place_builder_chunks_publishable
_run_luau_analyze = run_luau_analyze


def _e2e_output_dir(tmp_path: Path) -> Path:
    """Resolve where this test's conversion artifacts should land.

    Honours the ``E2E_OUTPUT_DIR`` env var set by the ``/e2e-test`` skill
    so the produced rbxlx is at a known path the skill can hand to
    Studio. Falls back to pytest's ``tmp_path`` for normal unit runs.
    Codex finding #3: skill ↔ pytest contract is a filesystem path, not
    pytest stdout parsing.
    """
    explicit = os.environ.get("E2E_OUTPUT_DIR")
    if explicit:
        out = Path(explicit)
        out.mkdir(parents=True, exist_ok=True)
        return out
    return tmp_path


def _scene_runtime_mode() -> str:
    """Return the scene-runtime mode the offline-assembly test should drive.

    Honours ``E2E_SCENE_RUNTIME_MODE`` (set by the ``/e2e-test`` skill).
    Defaults to ``"legacy"`` so a run with the env var unset is
    byte-identical to the historical behaviour: ctx loads from disk in
    legacy mode and scripts emit as auto-running top-level Scripts. When
    set to ``"generic"`` the test drives the scene-runtime contract
    (ModuleScripts hosted by an embedded SceneRuntime).
    """
    return os.environ.get("E2E_SCENE_RUNTIME_MODE", "legacy")


def _write_conversion_manifest(
    output_dir: Path,
    *,
    project: str,
    rbxlx_path: Path,
    started_at: str,
    started_monotonic: float,
    scene_runtime_mode: str = "legacy",
) -> None:
    """Emit the conversion_manifest.json artifact contract.

    Lives at ``<E2E_OUTPUT_DIR>/conversion_manifest.json`` when the env
    var is set (skill-driven run); skipped silently in normal pytest
    runs where no consumer would read it. The skill reads this file
    iff pytest exited 0 and uses ``rbxlx_path`` to open Studio.
    """
    if "E2E_OUTPUT_DIR" not in os.environ:
        return
    finished_at = datetime.now(timezone.utc).isoformat()
    duration_seconds = round(time.monotonic() - started_monotonic, 3)
    manifest = {
        "schema_version": 1,
        "project": project,
        "run_id": os.environ.get("E2E_RUN_ID", "unset"),
        "rbxlx_path": str(rbxlx_path.resolve()),
        "scene_runtime_mode": scene_runtime_mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
    }
    (output_dir / "conversion_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def _resolve_unity_project(snapshot_name: str, fallback_name: str) -> Path:
    """Find the Unity source for a snapshot.

    Resolution order, first hit wins:
      1. ``_meta.source_unity_project`` baked into the snapshot — when
         present and the path still exists on disk. This is the
         canonical source the snapshot was captured from; using it
         guarantees the cache key (csharp_source, project_context, ...)
         matches and the test runs against the exact files that
         produced the committed asset IDs. Submodules can be missing
         LFS-tracked files (heightmaps, large textures) that the
         production project has on disk; preferring the baked path
         dodges that trap.
      2. ``test_projects/<fallback_name>`` submodule (if populated)
      3. ``$UNITY2RBXLX_TEST_PROJECTS_ROOT/<fallback_name>``
    """
    snap = load_snapshot(snapshot_name)
    baked = snap.get("_meta", {}).get("source_unity_project", "")
    if baked:
        p = Path(baked)
        if is_populated(p):
            return p

    return resolve_project(fallback_name)


# Resolved at import so the @skipif decorator below can read them.
SIMPLEFPS_PROJECT = _resolve_unity_project("SimpleFPS", "SimpleFPS")
TRASHDASH_PROJECT = _resolve_unity_project("TrashDash", "trash-dash")


def _claude_cli_available() -> bool:
    """Return True if the Claude CLI is installed and on PATH.

    The full-flow offline-assembly tests run the real AI transpiler so
    every phase of the converter (transpile, coherence, autogen, test
    seam injection, scene conversion, place builder) is exercised
    end-to-end. Without Claude CLI the test cannot run, so we skip
    cleanly rather than fail with a confusing subprocess error.
    """
    return shutil.which("claude") is not None


@pytest.mark.slow
class TestOfflineAssembly:
    """Offline assembly — full converter end-to-end against cached asset IDs.

    Exercises the entire pipeline (parse → extract → transpile via AI →
    coherence → seam injection → convert_scene → write_output → place
    builder) with ``skip_upload=True`` so no Open Cloud calls happen.
    The snapshot seeds ``uploaded_assets`` + ``mesh_native_sizes`` so
    downstream phases see real Roblox IDs.

    Designed for manual + nightly runs, not per-PR. First run on a
    given project is ~10 min (full AI transpile); subsequent runs hit
    the system's ``.cache/llm/`` and finish in ~30s. The test is
    ``@pytest.mark.slow`` and skipped automatically when Claude CLI is
    not on PATH so CI environments without the CLI don't fail.
    """

    @pytest.fixture(autouse=True)
    def _enable_ai(self, monkeypatch):
        """Force AI transpilation on for the full-flow regression run.

        Other tests in the suite disable AI via ``_disable_ai`` to avoid
        Claude CLI hangs — this test deliberately re-enables it because
        the whole point is to validate the production transpile path.
        ``USE_AI_TRANSPILATION`` is read live from ``config`` by
        ``pipeline.transpile_scripts`` (pipeline.py:1780 — ``_config.``
        prefix means no import-time binding to worry about), so a single
        monkeypatch.setattr on the config module is sufficient.
        """
        import config
        monkeypatch.setattr(config, "USE_AI_TRANSPILATION", True)

    @pytest.mark.skipif(
        not is_populated(SIMPLEFPS_PROJECT),
        reason="SimpleFPS not available (init submodule, set "
               "UNITY2RBXLX_TEST_PROJECTS_ROOT, or run a real conversion "
               "first so the snapshot bakes in source_unity_project)",
    )
    @pytest.mark.skipif(
        not _claude_cli_available(),
        reason="claude CLI required (full-flow test runs real AI "
               "transpile; install Claude CLI or run nightly with it "
               "available)",
    )
    def test_simplefps_assembly_with_cached_ids(self, tmp_path: Path) -> None:
        from converter.pipeline import Pipeline

        # Honour E2E_OUTPUT_DIR when the /e2e-test skill drives the run;
        # otherwise tmp_path keeps unit-test isolation.
        output_dir = _e2e_output_dir(tmp_path)
        started_at = datetime.now(timezone.utc).isoformat()
        started_monotonic = time.monotonic()

        snapshot = _load_snapshot("SimpleFPS")
        _seed_output_dir(output_dir, snapshot)

        from core.conversion_context import ConversionContext
        pipeline = Pipeline(
            unity_project_path=SIMPLEFPS_PROJECT,
            output_dir=output_dir,
            skip_upload=True,
        )
        # Pipeline.__init__ always creates a fresh ctx; resume() is the
        # only public path that loads from disk and re-runs from a
        # specific phase. We want a full run_all() but with the seeded
        # uploaded_assets/mesh maps already populated, so load the ctx
        # explicitly before running.
        pipeline.ctx = ConversionContext.load(pipeline._context_path)

        # Scene-runtime mode is env-driven (default "legacy" — byte-identical
        # to the historical behaviour). When "generic", drive the
        # scene-runtime contract (ModuleScripts hosted by an embedded
        # SceneRuntime) in single-player config. ConversionContext.load
        # defaults to legacy, so we override after load and before run_all().
        mode = _scene_runtime_mode()
        if mode == "generic":
            pipeline.ctx.scene_runtime_mode = "generic"
            pipeline.ctx.networking_mode = "none"

        ctx = pipeline.run_all()

        # Drift gate first — most actionable failure when snapshot is stale.
        manifest = pipeline.state.asset_manifest
        assert manifest is not None, "asset manifest never built"
        _assert_snapshot_covers_manifest(manifest.assets, snapshot, "SimpleFPS")

        # rbxlx-level assertions
        rbxlx_files = list(output_dir.glob("*.rbxlx"))
        assert rbxlx_files, "no rbxlx produced"
        rbxlx = rbxlx_files[0]
        _assert_no_placeholder_ids(rbxlx)
        _assert_mesh_ids_match_snapshot(rbxlx, snapshot)

        # Generic-mode only: assert the scene-runtime contract embeds the
        # tier-1 prefab placements + boot path. Legacy assertions above
        # still run in both modes.
        if mode == "generic":
            # Fail-closed gate (Fix #15 Root A): a runtime-bearing module
            # that survives reprompt still broken promotes a "contract
            # failed closed" error onto ctx.errors. run_all() does not raise
            # on it and the rbxlx still gets written, so without this
            # assertion a structurally-broken place (e.g. a stubbed
            # Player.luau that throws at boot) would pass the suite green.
            contract_failures = [
                e for e in ctx.errors
                if "scene-runtime contract failed closed" in e
            ]
            assert not contract_failures, (
                "generic conversion shipped with contract fail-closed "
                "errors — the place will throw at boot:\n  "
                + "\n  ".join(contract_failures)
            )
            _assert_generic_scene_runtime(rbxlx)

        # Publish-stage artifact (still no cloud)
        rbx_place = getattr(ctx, "rbx_place", None) or pipeline.state.rbx_place
        _assert_place_builder_chunks_publishable(rbx_place)

        # Luau syntax — soft-skip when luau-analyze isn't installed.
        passed, failed, fails = _run_luau_analyze(output_dir / "scripts")
        assert failed == 0, (
            f"luau-analyze found {failed} syntax error(s):\n"
            + "\n".join(fails[:10])
        )

        # Skill-facing artifact contract — silently no-op for normal
        # pytest runs (no E2E_OUTPUT_DIR set).
        _write_conversion_manifest(
            output_dir,
            project="SimpleFPS",
            rbxlx_path=rbxlx,
            started_at=started_at,
            started_monotonic=started_monotonic,
            scene_runtime_mode=mode,
        )

    @pytest.mark.skipif(
        not is_populated(TRASHDASH_PROJECT),
        reason="Trash Dash not available — set "
               "UNITY2RBXLX_TEST_PROJECTS_ROOT, or run a real conversion "
               "first so the snapshot bakes in source_unity_project",
    )
    @pytest.mark.skipif(
        not _claude_cli_available(),
        reason="claude CLI required (full-flow test runs real AI "
               "transpile)",
    )
    def test_trashdash_assembly_with_cached_ids(self, tmp_path: Path) -> None:
        from converter.pipeline import Pipeline

        snapshot = _load_snapshot("TrashDash")
        _seed_output_dir(tmp_path, snapshot)

        from core.conversion_context import ConversionContext
        pipeline = Pipeline(
            unity_project_path=TRASHDASH_PROJECT,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.ctx = ConversionContext.load(pipeline._context_path)
        ctx = pipeline.run_all()

        manifest = pipeline.state.asset_manifest
        assert manifest is not None
        _assert_snapshot_covers_manifest(manifest.assets, snapshot, "TrashDash")

        rbxlx_files = list(tmp_path.glob("*.rbxlx"))
        assert rbxlx_files
        rbxlx = rbxlx_files[0]
        _assert_no_placeholder_ids(rbxlx)
        _assert_mesh_ids_match_snapshot(rbxlx, snapshot)

        rbx_place = getattr(ctx, "rbx_place", None) or pipeline.state.rbx_place
        _assert_place_builder_chunks_publishable(rbx_place)

        passed, failed, fails = _run_luau_analyze(tmp_path / "scripts")
        assert failed == 0, (
            f"luau-analyze found {failed} syntax error(s):\n"
            + "\n".join(fails[:10])
        )


class TestConversionManifest:
    """Skill-facing artifact contract (Codex finding #3).

    The /e2e-test skill passes ``E2E_OUTPUT_DIR`` and ``E2E_RUN_ID`` env
    vars; the offline-assembly test honours them so the produced rbxlx
    ends up at a path the skill knows. A ``conversion_manifest.json``
    is written iff ``E2E_OUTPUT_DIR`` is set, so normal pytest runs
    never touch disk outside ``tmp_path``.
    """

    def test_e2e_output_dir_honours_env(self, tmp_path: Path, monkeypatch) -> None:
        target = tmp_path / "skill_chose_this"
        monkeypatch.setenv("E2E_OUTPUT_DIR", str(target))
        resolved = _e2e_output_dir(tmp_path / "ignored")
        assert resolved == target
        assert resolved.exists(), "must mkdir if absent"

    def test_e2e_output_dir_falls_back_to_tmp_path(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("E2E_OUTPUT_DIR", raising=False)
        assert _e2e_output_dir(tmp_path) == tmp_path

    def test_manifest_written_when_env_set(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("E2E_OUTPUT_DIR", str(tmp_path))
        monkeypatch.setenv("E2E_RUN_ID", "2026-05-21T00-00-00-abcdef")
        fake_rbxlx = tmp_path / "converted_place.rbxlx"
        fake_rbxlx.write_text("<roblox/>", encoding="utf-8")

        _write_conversion_manifest(
            tmp_path,
            project="SimpleFPS",
            rbxlx_path=fake_rbxlx,
            started_at="2026-05-21T00:00:00+00:00",
            started_monotonic=time.monotonic() - 12.345,
        )

        manifest_path = tmp_path / "conversion_manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["schema_version"] == 1
        assert manifest["project"] == "SimpleFPS"
        assert manifest["run_id"] == "2026-05-21T00-00-00-abcdef"
        assert manifest["rbxlx_path"] == str(fake_rbxlx.resolve())
        # Duration is recorded — exact value not asserted because clock
        # round-tripping is noisy in fast tests; sanity-check it's a
        # non-negative float that matches the elapsed window.
        assert 10.0 <= manifest["duration_seconds"] <= 20.0

    def test_manifest_silent_no_op_without_env(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("E2E_OUTPUT_DIR", raising=False)
        fake_rbxlx = tmp_path / "x.rbxlx"
        fake_rbxlx.write_text("<roblox/>", encoding="utf-8")
        _write_conversion_manifest(
            tmp_path,
            project="SimpleFPS",
            rbxlx_path=fake_rbxlx,
            started_at="2026-05-21T00:00:00+00:00",
            started_monotonic=time.monotonic(),
        )
        # Normal pytest runs: helper must not litter the test's tmp_path
        # with skill artifacts.
        assert not (tmp_path / "conversion_manifest.json").exists()
