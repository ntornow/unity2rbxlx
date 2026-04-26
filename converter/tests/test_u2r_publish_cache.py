"""
test_u2r_publish_cache.py -- ID-cache write semantics for ``u2r convert``.

Regression for the Codex round-2 finding: writing ``.roblox_ids.json``
before the publish attempt poisons the shared cache when the user
supplies bad credentials or a typo'd place ID, because every later
``u2r publish`` / ``convert_interactive upload`` silently reuses the bad
IDs from the cache.

The interactive ``upload`` flow already cached only on success. The CLI
``convert`` flow now matches that behavior — validated here by stubbing
``publish_place`` to return failure and asserting the cache is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from u2r import main


@pytest.fixture
def fake_unity_project(tmp_path: Path) -> Path:
    """Minimal Unity-shaped directory tree."""
    project = tmp_path / "FakeProject"
    (project / "Assets").mkdir(parents=True)
    return project


def _stub_pipeline(monkeypatch, *, has_meshes: bool = True):
    """Replace converter.pipeline.Pipeline with a stub that pretends to
    convert without touching the filesystem or Open Cloud."""
    from converter import pipeline as _pl_mod
    from core.conversion_context import ConversionContext

    class _PState:
        rbx_place = object()  # any non-None sentinel

    class _StubPipeline:
        def __init__(self, *, unity_project_path, output_dir, **_):
            self.unity_project_path = Path(unity_project_path).resolve()
            self.output_dir = Path(output_dir).resolve()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.ctx = ConversionContext(unity_project_path=str(self.unity_project_path))
            if has_meshes:
                self.ctx.uploaded_assets["x.fbx"] = "rbxassetid://1"
            self.state = _PState()
            self.context = self.ctx
            self.skip_upload = False
            self._retranspile = False

        def run_all(self):
            return self.ctx

        def resume(self, phase):
            return self.ctx

    monkeypatch.setattr(_pl_mod, "Pipeline", _StubPipeline)


class TestRobloxIdsCacheTiming:
    def test_failed_publish_does_not_write_ids_cache(self, fake_unity_project, tmp_path, monkeypatch):
        """Bad credentials → publish_place returns failure → .roblox_ids.json
        must NOT be created. Otherwise the next run silently reuses the bad
        IDs.
        """
        _stub_pipeline(monkeypatch)

        # Stub publish_place to simulate a failed publish.
        from roblox import place_publisher
        from roblox.place_publisher import PublishResult

        monkeypatch.setattr(
            place_publisher, "publish_place",
            lambda *a, **kw: PublishResult(
                success=False,
                chunks=1,
                total_bytes=100,
                script_path=Path(a[4]) / "place_builder.luau",
                error="Chunk 1/1 failed.",
            ),
        )

        out = tmp_path / "out"
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "convert", str(fake_unity_project),
                "-o", str(out),
                "--api-key", "stub-key",
                "--universe-id", "999",
                "--place-id", "888",
            ],
            catch_exceptions=False,
        )

        # convert may exit non-zero; that's fine. The contract is that the
        # ID cache is not written on failure.
        ids_path = out / ".roblox_ids.json"
        assert not ids_path.exists(), (
            f"\n.roblox_ids.json was written despite publish failure.\n"
            f"output:\n{result.output[:1500]}"
        )

    def test_publish_rebuild_path_requires_creator_id_only_for_pending_uploads(
        self, tmp_path, monkeypatch,
    ):
        """The publish rebuild fallback only requires creator_id when the
        asset manifest contains uploads that aren't already in
        ctx.uploaded_assets. A previously-uploaded output should be able
        to republish with just an API key — no creator_id needed. Codex
        review round 6 caught the over-strict version of this guard.
        """
        import config as _config
        monkeypatch.setattr(_config, "ROBLOX_API_KEY", "")
        monkeypatch.setattr(_config, "ROBLOX_CREATOR_ID", None)
        monkeypatch.delenv("ROBLOX_API_KEY", raising=False)
        monkeypatch.delenv("ROBLOX_CREATOR_ID", raising=False)

        # Output dir: has conversion_context.json but no chunks JSON and
        # no place_builder.luau — forces the rebuild path. The manifest
        # carries one uploadable asset that's NOT in ctx.uploaded_assets,
        # so creator_id should be required.
        unity = tmp_path / "FakeProject"
        (unity / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()

        # Stub Pipeline.run_through to populate state.asset_manifest with
        # one pending upload, then the cred check fires.
        from converter import pipeline as _pl_mod
        from core.conversion_context import ConversionContext
        from core.unity_types import AssetEntry, AssetManifest

        ctx_obj = ConversionContext(unity_project_path=str(unity.resolve()))
        ctx_obj.save(out / "conversion_context.json")

        class _PState:
            asset_manifest = AssetManifest(
                project_root=unity,
                assets=[AssetEntry(
                    path=unity / "Assets" / "missing.png",
                    relative_path=Path("Assets/missing.png"),
                    kind="texture",
                    size_bytes=100,
                )],
                total_size_bytes=100,
            )
            rbx_place = None

        class _StubPipeline:
            ESSENTIAL_PHASES = _pl_mod.Pipeline.ESSENTIAL_PHASES
            def __init__(self, *, unity_project_path, output_dir, **_):
                self.unity_project_path = Path(unity_project_path).resolve()
                self.output_dir = Path(output_dir).resolve()
                self.ctx = ctx_obj
                self.state = _PState()
            def run_through(self, *a, **k):
                pass

        monkeypatch.setattr(_pl_mod, "Pipeline", _StubPipeline)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "publish", str(out),
                "--api-key", "stub",
                "--universe-id", "1",
                "--place-id", "2",
            ],
            catch_exceptions=False,
        )
        assert "creator-id" in result.output.lower() or "creator_id" in result.output.lower(), (
            f"expected creator-id error for pending uploads, got:\n{result.output[:1500]}"
        )

    def test_publish_rebuild_skips_blocklisted_assets_in_pending_check(
        self, tmp_path, monkeypatch,
    ):
        """A manifest asset that's in .upload_blocklist should not count as
        pending — upload_assets won't actually upload it. The publish
        precheck must mirror upload_assets's filter or it false-positives
        the creator_id requirement on output dirs whose only "pending"
        asset is blocklisted. Regression caught by Codex.
        """
        import config as _config
        monkeypatch.setattr(_config, "ROBLOX_API_KEY", "")
        monkeypatch.setattr(_config, "ROBLOX_CREATOR_ID", None)
        monkeypatch.delenv("ROBLOX_API_KEY", raising=False)
        monkeypatch.delenv("ROBLOX_CREATOR_ID", raising=False)

        unity = tmp_path / "FakeProject"
        (unity / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()

        from converter import pipeline as _pl_mod
        from core.conversion_context import ConversionContext
        from core.unity_types import AssetEntry, AssetManifest

        # Manifest has a blocklisted texture; nothing else to upload.
        ctx_obj = ConversionContext(unity_project_path=str(unity.resolve()))
        ctx_obj.save(out / "conversion_context.json")
        (out / ".upload_blocklist").write_text("Assets/blocked.png\n")

        ran_publish = []

        class _PState:
            asset_manifest = AssetManifest(
                project_root=unity,
                assets=[AssetEntry(
                    path=unity / "Assets" / "blocked.png",
                    relative_path=Path("Assets/blocked.png"),
                    kind="texture",
                    size_bytes=100,
                )],
                total_size_bytes=100,
            )
            rbx_place = object()

        class _StubPipeline:
            ESSENTIAL_PHASES = _pl_mod.Pipeline.ESSENTIAL_PHASES
            def __init__(self, *, unity_project_path, output_dir, **_):
                self.unity_project_path = Path(unity_project_path).resolve()
                self.output_dir = Path(output_dir).resolve()
                self.ctx = ctx_obj
                self.state = _PState()
            def run_through(self, *a, **k):
                pass

        monkeypatch.setattr(_pl_mod, "Pipeline", _StubPipeline)

        from roblox import place_publisher
        from roblox.place_publisher import PublishResult
        monkeypatch.setattr(
            place_publisher, "publish_place",
            lambda *a, **kw: ran_publish.append(True) or PublishResult(
                success=True, chunks=1, total_bytes=10,
                script_path=Path(a[4]) / "place_builder.luau",
            ),
        )
        monkeypatch.setattr(
            place_publisher, "publish_cached_chunks",
            lambda *a, **kw: None,
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "publish", str(out),
                "--api-key", "stub",
                "--universe-id", "1",
                "--place-id", "2",
            ],
            catch_exceptions=False,
        )

        # Must not demand creator-id when the only pending asset is blocklisted.
        assert "creator-id" not in result.output.lower() and "creator_id" not in result.output.lower(), (
            f"creator-id wrongly demanded for blocklisted-only pending list:\n"
            f"{result.output[:1500]}"
        )
        assert ran_publish, "should reach publish_place — no pending uploads after blocklist filter"

    def test_publish_rebuild_does_not_demand_creator_id_when_nothing_pending(
        self, tmp_path, monkeypatch,
    ):
        """If every uploadable asset in the manifest is already in
        ctx.uploaded_assets, the rebuild path must not block on a missing
        creator_id — no upload work would actually fire. Counterpart to
        the pending-uploads test above (Codex review round 6).
        """
        import config as _config
        monkeypatch.setattr(_config, "ROBLOX_API_KEY", "")
        monkeypatch.setattr(_config, "ROBLOX_CREATOR_ID", None)
        monkeypatch.delenv("ROBLOX_API_KEY", raising=False)
        monkeypatch.delenv("ROBLOX_CREATOR_ID", raising=False)

        unity = tmp_path / "FakeProject"
        (unity / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()

        from converter import pipeline as _pl_mod
        from core.conversion_context import ConversionContext
        from core.unity_types import AssetEntry, AssetManifest

        # ctx already has the asset uploaded → nothing pending.
        ctx_obj = ConversionContext(unity_project_path=str(unity.resolve()))
        ctx_obj.uploaded_assets["Assets/done.png"] = "rbxassetid://12345"
        ctx_obj.save(out / "conversion_context.json")

        ran_publish = []

        class _PState:
            asset_manifest = AssetManifest(
                project_root=unity,
                assets=[AssetEntry(
                    path=unity / "Assets" / "done.png",
                    relative_path=Path("Assets/done.png"),
                    kind="texture",
                    size_bytes=50,
                )],
                total_size_bytes=50,
            )
            rbx_place = object()

        class _StubPipeline:
            ESSENTIAL_PHASES = _pl_mod.Pipeline.ESSENTIAL_PHASES
            def __init__(self, *, unity_project_path, output_dir, **_):
                self.unity_project_path = Path(unity_project_path).resolve()
                self.output_dir = Path(output_dir).resolve()
                self.ctx = ctx_obj
                self.state = _PState()
            def run_through(self, *a, **k):
                pass

        monkeypatch.setattr(_pl_mod, "Pipeline", _StubPipeline)

        # Stub publish_place to avoid touching the network.
        from roblox import place_publisher
        from roblox.place_publisher import PublishResult
        monkeypatch.setattr(
            place_publisher, "publish_place",
            lambda *a, **kw: ran_publish.append(True) or PublishResult(
                success=True, chunks=1, total_bytes=10,
                script_path=Path(a[4]) / "place_builder.luau",
            ),
        )
        # publish_cached_chunks must return None to force the rebuild path.
        monkeypatch.setattr(
            place_publisher, "publish_cached_chunks",
            lambda *a, **kw: None,
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "publish", str(out),
                "--api-key", "stub",
                "--universe-id", "1",
                "--place-id", "2",
            ],
            catch_exceptions=False,
        )

        # Should not have errored on missing creator_id. The rebuild path
        # ran through to publish_place (counted in ran_publish).
        assert "creator-id" not in result.output.lower() and "creator_id" not in result.output.lower(), (
            f"creator-id wrongly demanded when nothing pending:\n{result.output[:1500]}"
        )
        assert ran_publish, "rebuild path should reach publish_place when nothing pending"

    def test_successful_publish_writes_ids_cache(self, fake_unity_project, tmp_path, monkeypatch):
        """The success path must still cache uid/pid so future runs are
        one-command. (Counterpart to the failure test — without it we'd
        only know the cache is never written, not that it's written
        correctly.)
        """
        _stub_pipeline(monkeypatch)

        from roblox import place_publisher
        from roblox.place_publisher import PublishResult

        monkeypatch.setattr(
            place_publisher, "publish_place",
            lambda *a, **kw: PublishResult(
                success=True,
                chunks=1,
                total_bytes=100,
                script_path=Path(a[4]) / "place_builder.luau",
            ),
        )

        out = tmp_path / "out"
        runner = CliRunner()
        runner.invoke(
            main,
            [
                "convert", str(fake_unity_project),
                "-o", str(out),
                "--api-key", "stub-key",
                "--universe-id", "1234",
                "--place-id", "5678",
            ],
            catch_exceptions=False,
        )

        ids_path = out / ".roblox_ids.json"
        assert ids_path.exists(), ".roblox_ids.json must be written on success"
        import json
        cached = json.loads(ids_path.read_text())
        assert cached == {"universe_id": 1234, "place_id": 5678}
