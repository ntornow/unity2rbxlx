"""test_resolve_assets_id_contract.py — phase contract for resolve_assets.

Codex round 7 finding: ``assemble`` produced a final-looking
``converted_place.rbxlx`` while silently skipping headless mesh
resolution when no universe/place IDs were available. Studio then
failed to fetch the meshes (raw Model IDs are not Mesh IDs), the
floor never loaded, and the spawned character could not move.

The fix:
  1. ``resolve_assets`` must raise when uploads are enabled, mesh
     assets exist, and no IDs are available — degraded output is
     worse than a loud failure.
  2. Mesh-free projects (or fully-resolved reruns) without IDs are
     legitimately a no-op; the warning path must stay quiet there.
  3. ``assemble`` must accept ``--universe-id`` / ``--place-id`` so
     first-run users can supply them at the point of need rather
     than waiting for the post-upload cache.
  4. ``upload`` must self-heal the local rbxlx after caching IDs:
     a re-run of resolve_assets + write_output rewrites
     converted_place.rbxlx to match the published place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.pipeline import Pipeline


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_unity_project(tmp_path: Path) -> Path:
    project = tmp_path / "FakeProject"
    (project / "Assets").mkdir(parents=True)
    return project


def _set_creds(monkeypatch, *, api_key: str = "stub-key", creator_id: int = 1) -> None:
    """resolve_assets early-returns without an API key + creator_id; the
    contract under test runs only after those checks pass."""
    import config as _config
    monkeypatch.setattr(_config, "ROBLOX_API_KEY", api_key)
    monkeypatch.setattr(_config, "ROBLOX_CREATOR_ID", creator_id)


# ---------------------------------------------------------------------------
# Pipeline.resolve_assets contract
# ---------------------------------------------------------------------------

class TestResolveAssetsContract:
    def test_raises_when_unresolved_meshes_and_no_ids(
        self, fake_unity_project, tmp_path, monkeypatch,
    ):
        """The whole point of the fix: producing a broken local rbxlx is
        worse than a loud halt. With uploads enabled, an unresolved mesh,
        and no IDs from any source, the phase must raise."""
        _set_creds(monkeypatch)

        pipeline = Pipeline(
            unity_project_path=fake_unity_project,
            output_dir=tmp_path / "out",
            skip_upload=False,
        )
        # One uploaded mesh that hasn't been resolved yet.
        pipeline.ctx.uploaded_assets["Assets/x.fbx"] = "rbxassetid://111"

        with pytest.raises(RuntimeError, match="Cannot finalize converted_place.rbxlx"):
            pipeline.resolve_assets()

    def test_quiet_skip_when_no_ids_and_no_unresolved_meshes(
        self, fake_unity_project, tmp_path, monkeypatch, caplog,
    ):
        """Mesh-free project (or fully-resolved rerun) without IDs is a
        legitimate no-op. The previous code emitted a multi-line warning
        block here; the new code must NOT raise and must NOT spam."""
        _set_creds(monkeypatch)

        pipeline = Pipeline(
            unity_project_path=fake_unity_project,
            output_dir=tmp_path / "out",
            skip_upload=False,
        )
        # No FBX/OBJ uploads at all.
        pipeline.ctx.uploaded_assets["Assets/sound.mp3"] = "rbxassetid://222"

        with caplog.at_level("WARNING"):
            pipeline.resolve_assets()  # should not raise

        # The new info-level skip is fine, but no WARNING-level noise
        # about missing IDs should fire when there's nothing to resolve.
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert not any("No --universe-id" in r.getMessage() for r in warnings), (
            "Mesh-free runs should not warn about missing IDs"
        )

    def test_quiet_skip_when_skip_upload(
        self, fake_unity_project, tmp_path, monkeypatch,
    ):
        """``--no-upload`` must continue to short-circuit before any ID
        check — users who pass it explicitly opted out of cloud work."""
        _set_creds(monkeypatch)

        pipeline = Pipeline(
            unity_project_path=fake_unity_project,
            output_dir=tmp_path / "out",
            skip_upload=True,
        )
        pipeline.ctx.uploaded_assets["Assets/x.fbx"] = "rbxassetid://111"

        # Even with unresolved meshes, --no-upload must not raise.
        pipeline.resolve_assets()

    def test_uses_ids_from_ctx(
        self, fake_unity_project, tmp_path, monkeypatch,
    ):
        """When IDs are present in ctx, resolve_assets proceeds past the
        ID gate and reaches the real resolve loop. We stub out the
        execute_luau call so the test stays offline."""
        _set_creds(monkeypatch)

        pipeline = Pipeline(
            unity_project_path=fake_unity_project,
            output_dir=tmp_path / "out",
            skip_upload=False,
        )
        pipeline.ctx.uploaded_assets["Assets/x.fbx"] = "rbxassetid://111"
        pipeline.ctx.universe_id = 12345
        pipeline.ctx.place_id = 67890

        # Stub execute_luau to confirm we got past the ID check.
        called = {}
        from roblox import cloud_api

        def _stub(api_key, uid, pid, script, **_kw):
            called["uid"] = uid
            called["pid"] = pid
            return {"output": {"results": ["[]"]}}  # empty resolve batch

        monkeypatch.setattr(cloud_api, "execute_luau", _stub)
        # Stub id_cache.write_ids so the test doesn't litter the tmp dir.
        from roblox import id_cache
        monkeypatch.setattr(id_cache, "write_ids", lambda *a, **k: None)

        pipeline.resolve_assets()
        assert called.get("uid") == 12345
        assert called.get("pid") == 67890

    def test_recovers_ids_from_roblox_ids_json(
        self, fake_unity_project, tmp_path, monkeypatch,
    ):
        """If ctx has no IDs but ``.roblox_ids.json`` does, resolve_assets
        must recover them. This is the ``upload``-then-rerun-``assemble``
        pre-fix workaround that we keep working as a backup."""
        _set_creds(monkeypatch)

        out = tmp_path / "out"
        out.mkdir()
        from roblox.id_cache import write_ids
        write_ids(out, 99, 88)

        pipeline = Pipeline(
            unity_project_path=fake_unity_project,
            output_dir=out,
            skip_upload=False,
        )
        pipeline.ctx.uploaded_assets["Assets/x.fbx"] = "rbxassetid://111"
        # ctx.universe_id / place_id intentionally unset.

        from roblox import cloud_api, id_cache
        captured = {}

        def _stub(api_key, uid, pid, script, **_kw):
            captured["uid"] = uid
            captured["pid"] = pid
            return {"output": {"results": ["[]"]}}

        monkeypatch.setattr(cloud_api, "execute_luau", _stub)
        monkeypatch.setattr(id_cache, "write_ids", lambda *a, **k: None)

        pipeline.resolve_assets()
        assert captured.get("uid") == 99
        assert captured.get("pid") == 88
        # And ctx should now reflect the recovered IDs.
        assert pipeline.ctx.universe_id == 99
        assert pipeline.ctx.place_id == 88

    def test_stale_resolved_keys_do_not_suppress_new_resolution(
        self, fake_unity_project, tmp_path, monkeypatch,
    ):
        """Codex review found that ``all_meshes_resolved`` used to
        compare resolved-entry COUNT against current upload count.
        A previous run that left mesh_native_sizes={A,B,C} (3 entries)
        plus a current uploaded_assets={D,E} (2 entries) would compute
        3 >= 2, declare "all resolved", and never resolve D or E.

        The fix requires that every currently-uploaded mesh key be
        present in mesh_native_sizes. With D/E unresolved, the phase
        must proceed to the resolve loop.
        """
        _set_creds(monkeypatch)

        pipeline = Pipeline(
            unity_project_path=fake_unity_project,
            output_dir=tmp_path / "out",
            skip_upload=False,
        )
        # Stale resolution from a previous run — keys that no longer
        # appear in the current uploaded_assets dict.
        pipeline.ctx.mesh_native_sizes = {
            "Assets/stale1.fbx": [1.0, 1.0, 1.0],
            "Assets/stale2.fbx": [1.0, 1.0, 1.0],
            "Assets/stale3.fbx": [1.0, 1.0, 1.0],
        }
        # Current uploads are different.
        pipeline.ctx.uploaded_assets = {
            "Assets/new1.fbx": "rbxassetid://1",
            "Assets/new2.fbx": "rbxassetid://2",
        }
        pipeline.ctx.universe_id = 1234
        pipeline.ctx.place_id = 5678

        from roblox import cloud_api, id_cache
        called: list[str] = []

        def _stub(api_key, uid, pid, script, **_kw):
            called.append(script)
            return {"output": {"results": ["[]"]}}

        monkeypatch.setattr(cloud_api, "execute_luau", _stub)
        monkeypatch.setattr(id_cache, "write_ids", lambda *a, **k: None)

        pipeline.resolve_assets()

        # The phase must have called execute_luau to resolve new1/new2
        # rather than short-circuiting on a stale count comparison.
        assert called, (
            "resolve_assets must actually resolve unresolved meshes "
            "even when stale entries inflate the count."
        )
        joined = "\n".join(called)
        assert "new1.fbx" in joined and "new2.fbx" in joined


# ---------------------------------------------------------------------------
# convert_interactive assemble CLI surface
# ---------------------------------------------------------------------------

class TestAssembleAcceptsIds:
    def test_assemble_help_lists_universe_and_place_id(self):
        """The flags must be on ``assemble`` so the skill workflow can
        gather them at Phase 5 instead of Phase 6."""
        from click.testing import CliRunner
        from convert_interactive import cli

        result = CliRunner().invoke(cli, ["assemble", "--help"])
        assert result.exit_code == 0, result.output
        assert "--universe-id" in result.output
        assert "--place-id" in result.output
