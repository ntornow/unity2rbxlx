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

_FIXTURES = Path(__file__).parent / "fixtures" / "upload_snapshots"


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


def _write_conversion_manifest(
    output_dir: Path,
    *,
    project: str,
    rbxlx_path: Path,
    started_at: str,
    started_monotonic: float,
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
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
    }
    (output_dir / "conversion_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_snapshot(name: str) -> dict:
    path = _FIXTURES / f"{name}.snapshot.json"
    if not path.exists():
        pytest.skip(f"snapshot fixture missing: {path}")
    return json.loads(path.read_text())


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
    snap = _load_snapshot(snapshot_name)
    baked = snap.get("_meta", {}).get("source_unity_project", "")
    if baked:
        p = Path(baked)
        if is_populated(p):
            return p

    return resolve_project(fallback_name)


# Resolved at import so the @skipif decorator below can read them.
SIMPLEFPS_PROJECT = _resolve_unity_project("SimpleFPS", "SimpleFPS")
TRASHDASH_PROJECT = _resolve_unity_project("TrashDash", "trash-dash")


def _seed_output_dir(output_dir: Path, snapshot: dict) -> None:
    """Pre-populate ``output_dir`` so Pipeline sees prior upload+resolve state.

    Writes a minimal ``conversion_context.json`` carrying the snapshot's
    ``uploaded_assets`` / mesh resolution maps, and a ``.roblox_ids.json``
    carrying universe/place IDs. The pipeline loads these on init.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    ctx = {
        "uploaded_assets": snapshot["uploaded_assets"],
        "mesh_native_sizes": snapshot["mesh_native_sizes"],
        "mesh_hierarchies": snapshot["mesh_hierarchies"],
        "universe_id": snapshot.get("universe_id"),
        "place_id": snapshot.get("place_id"),
        "completed_phases": [
            "moderate_assets", "upload_assets", "resolve_assets",
        ],
    }
    (output_dir / "conversion_context.json").write_text(
        json.dumps(ctx, indent=2), encoding="utf-8"
    )

    # Mirror the shared id_cache shape so resolve_assets retarget paths
    # also find the IDs even without ctx.universe_id/place_id.
    if snapshot.get("universe_id") and snapshot.get("place_id"):
        (output_dir / ".roblox_ids.json").write_text(
            json.dumps({
                "universe_id": str(snapshot["universe_id"]),
                "place_id": str(snapshot["place_id"]),
            }),
            encoding="utf-8",
        )


def _assert_no_placeholder_ids(rbxlx_path: Path) -> None:
    """Fail if the assembled rbxlx contains unresolved asset references.

    ``rbxassetid://0`` is the placeholder convert_scene emits when an
    asset key is in the scene but missing from ``uploaded_assets``. A
    fully-seeded run should never produce one — if it does, the
    snapshot is incomplete or a converter regression dropped the lookup.
    """
    text = rbxlx_path.read_text(encoding="utf-8", errors="replace")
    bad = text.count("rbxassetid://0")
    assert bad == 0, (
        f"{rbxlx_path.name} contains {bad} placeholder rbxassetid://0 "
        "references — either the snapshot is stale (regenerate with "
        "`python u2r.py snapshot-ids`) or a converter regression "
        "dropped an asset lookup."
    )


def _assert_snapshot_covers_manifest(
    manifest_assets: list, snapshot: dict, project_name: str,
) -> None:
    """Drift gate: every uploadable asset in the manifest must be snapshotted.

    Two categories count as "covered":
      * ``uploaded_assets`` — the asset has a real Roblox ID
      * ``asset_upload_errors`` — the asset was legitimately rejected
        (moderation, network failure that the original run logged)

    Anything else is real drift — usually a new Unity asset that the
    snapshot was captured before, OR a converter regression that
    started discovering assets that previous runs missed.
    """
    uploaded = snapshot["uploaded_assets"]
    eligible_exts = {
        ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".psd",
        ".fbx", ".obj",
        ".mp3", ".ogg", ".wav", ".flac",
    }
    # asset_upload_errors entries are formatted "<rel_path> (<reason>)".
    # Strip the trailing " (...)" suffix so we can match against the
    # bare relative path the manifest uses.
    rejected_paths: set[str] = set()
    for entry in snapshot.get("asset_upload_errors", []) or []:
        s = str(entry)
        paren = s.rfind(" (")
        rejected_paths.add(s[:paren] if paren > 0 else s)

    missing: list[str] = []
    for asset in manifest_assets:
        rel = str(asset.relative_path)
        if Path(rel).suffix.lower() not in eligible_exts:
            continue
        if rel in uploaded or rel in rejected_paths:
            continue
        missing.append(rel)

    assert not missing, (
        f"[{project_name}] snapshot is missing {len(missing)} asset(s) "
        f"the Unity project now references — first 5: {missing[:5]!r}. "
        f"Regenerate with: python u2r.py snapshot-ids <output_dir> -o "
        f"tests/fixtures/upload_snapshots/{project_name}.snapshot.json"
    )


def _assert_mesh_ids_match_snapshot(rbxlx_path: Path, snapshot: dict) -> None:
    """Every MeshId in the assembled rbxlx must be a snapshot-known ID.

    Catches the case where the assembly fabricates IDs (e.g. accidental
    string concatenation, default integer) instead of looking them up
    from the seeded ``uploaded_assets`` + ``mesh_hierarchies`` tables.
    Per-row failure surface, not just a count.
    """
    import re

    snapshot_ids = set(snapshot["uploaded_assets"].values())
    # mesh_hierarchies entries carry the real per-sub-mesh ``meshId``
    # (already in rbxassetid:// form) plus ``textureId`` for materials
    # baked into the sub-mesh. Flatten both into the legal set so the
    # MeshId references the rbxlx emits (one per sub-mesh, not one per
    # uploaded FBX) all match something the snapshot covered.
    for entries in snapshot.get("mesh_hierarchies", {}).values():
        for e in entries:
            if not isinstance(e, dict):
                continue
            for field in ("meshId", "textureId"):
                ref = e.get(field)
                if ref:
                    snapshot_ids.add(str(ref))

    text = rbxlx_path.read_text(encoding="utf-8", errors="replace")
    # MeshId is wrapped in <Content name="MeshId"><url>rbxassetid://N</url></Content>
    pattern = re.compile(
        r'<Content[^>]*name="MeshId"[^>]*>\s*<url>(rbxassetid://\d+)</url>',
        re.MULTILINE,
    )
    unknown: list[str] = []
    for m in pattern.finditer(text):
        ref = m.group(1)
        if ref not in snapshot_ids:
            unknown.append(ref)

    # Allow up to ~3 unknowns to absorb autogen meshes the converter
    # may add (FPS_Weapon_Mount viewmodel, GroundCollider, etc.) that
    # don't come from the upload pipeline. A larger number signals
    # an assembly regression.
    assert len(unknown) <= 3, (
        f"{rbxlx_path.name} references {len(unknown)} mesh IDs not in "
        f"the snapshot (first 5: {unknown[:5]!r}). Snapshot covered "
        f"{len(snapshot_ids)} known IDs. Either fabricated by the "
        f"converter or the snapshot is stale."
    )


def _assert_place_builder_chunks_publishable(rbx_place) -> None:
    """Generate the place-builder Luau chunks and assert publish viability.

    No cloud call — just runs the same chunker the publish step would,
    then asserts the largest chunk is under the 4MB execute_luau cap
    so we'd catch a regression that ballooned the script output.
    """
    from roblox.luau_place_builder import generate_place_luau_chunked
    from roblox.place_publisher import MAX_EXECUTE_LUAU_BYTES

    chunks = generate_place_luau_chunked(rbx_place)
    assert chunks, "place builder produced zero chunks — the builder bailed"

    max_chunk = max(len(c) for c in chunks)
    assert max_chunk < MAX_EXECUTE_LUAU_BYTES, (
        f"largest place-builder chunk is {max_chunk:,} bytes "
        f"(cap: {MAX_EXECUTE_LUAU_BYTES:,}). A real publish would fail."
    )


def _run_luau_analyze(scripts_dir: Path) -> tuple[int, int, list[str]]:
    """Run luau-analyze over every .luau under ``scripts_dir``.

    Uses the shared ``utils.luau_analyze`` helper, which filters output to
    SyntaxError lines only — TypeError noise for Roblox-specific globals
    and lint warnings (FunctionUnused, etc.) are intentionally ignored so
    the gate only fires on actual parse failures. The shared helper also
    no-ops cleanly when luau-analyze is not installed, so this test still
    runs in CI environments that don't ship it.
    """
    from utils.luau_analyze import luau_analyze_path, syntax_errors_for_file

    if not luau_analyze_path():
        return (0, 0, ["luau-analyze not installed — skipping syntax check"])

    luau_files = list(scripts_dir.rglob("*.luau"))
    if not luau_files:
        return (0, 0, [])

    failures: list[str] = []
    passed = 0
    for lf in luau_files:
        errs = syntax_errors_for_file(lf)
        if errs:
            failures.append(
                f"{lf.relative_to(scripts_dir)}: " + "; ".join(errs[:3])
            )
        else:
            passed += 1
    return (passed, len(failures), failures)


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
