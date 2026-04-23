"""
test_convert_interactive.py -- Unit + e2e tests for the phase-by-phase
interactive CLI used by the ``/convert-unity`` skill.

Fast tests cover helpers and the no-state-yet paths of every subcommand
(CliRunner, no filesystem dependencies beyond ``tmp_path``).  One
``@pytest.mark.slow`` test exercises ``discover`` end-to-end against the
SimpleFPS fixture, matching the pattern used in ``test_pipeline_e2e.py``.
"""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from convert_interactive import (
    SKILL_PHASES,
    _ctx_summary,
    _load_skill_state,
    _next_skill_phase,
    _run_through,
    cli,
)
from core.conversion_context import ConversionContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _invoke_json(runner: CliRunner, args: list[str]) -> tuple[int, dict]:
    """Invoke ``cli`` and parse the JSON document it prints to stdout."""
    result = runner.invoke(cli, args, catch_exceptions=False)
    # The last JSON document is the emitted payload; earlier lines may be log
    # output.  _emit writes a single indented block so we can find its start
    # by scanning for the first '{' at column 0.
    lines = result.output.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
    assert start is not None, f"no JSON in output:\n{result.output}"
    payload = json.loads("\n".join(lines[start:]))
    return result.exit_code, payload


# ---------------------------------------------------------------------------
# Pure helpers — _next_skill_phase, _ctx_summary
# ---------------------------------------------------------------------------


class TestNextSkillPhase:
    def test_empty_completed_returns_first_phase(self):
        assert _next_skill_phase([]) == SKILL_PHASES[0]

    def test_partial_completed_returns_next_unseen(self):
        assert _next_skill_phase(["discover", "inventory"]) == "materials"

    def test_all_completed_returns_none(self):
        assert _next_skill_phase(list(SKILL_PHASES)) is None

    def test_out_of_order_completed_still_returns_first_gap(self):
        # If someone marked "assemble" done but skipped "materials",
        # the next phase should still be "materials".
        assert _next_skill_phase(["discover", "inventory", "assemble"]) == "materials"


class TestCtxSummary:
    def test_shape_and_keys(self):
        ctx = ConversionContext(
            unity_project_path="/tmp/fake/unity",
            selected_scene="Assets/Scenes/Main/level.unity",
            scene_paths=["Assets/Scenes/Main/level.unity", "Assets/Scenes/menu.unity"],
            total_game_objects=42,
            converted_parts=30,
            total_scripts=10,
            transpiled_scripts=9,
            total_materials=5,
            converted_materials=5,
            uploaded_assets={"a": "rbxassetid://1", "b": "rbxassetid://2"},
            warnings=["w1", "w2"],
            errors=["e1"],
            completed_phases=["parse", "extract_assets"],
        )
        summary = _ctx_summary(ctx)

        # Project-relative scene paths for disambiguation.
        assert summary["selected_scene"] == "Assets/Scenes/Main/level.unity"
        assert summary["scene_count"] == 2
        assert summary["total_game_objects"] == 42
        assert summary["converted_parts"] == 30
        assert summary["total_scripts"] == 10
        assert summary["transpiled_scripts"] == 9
        assert summary["uploaded_assets"] == 2
        assert summary["asset_upload_errors"] == 0
        assert summary["warnings"] == 2
        assert summary["errors"] == 1
        assert summary["completed_pipeline_phases"] == ["parse", "extract_assets"]

    def test_empty_context_has_empty_scene_string(self):
        ctx = ConversionContext()
        summary = _ctx_summary(ctx)
        assert summary["selected_scene"] == ""
        assert summary["scene_count"] == 0
        assert summary["total_game_objects"] == 0


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


class TestRunThrough:
    """Regression tests for `_run_through`: review phases (materials,
    transpile) must never run the cloud side-effect phases
    `upload_assets` / `resolve_assets` as prerequisites, even on a fresh
    output directory.  Running them as silent prerequisites would leak
    Open Cloud quota on every review-phase invocation.
    """

    class _StubPipeline:
        """Minimal stand-in for Pipeline that records what phases ran."""

        def __init__(self):
            self.run_log: list[str] = []
            self.ctx = ConversionContext()

        def _run_phase(self, phase: str) -> None:
            self.run_log.append(phase)
            # Track completion the way the real pipeline does, so the
            # "skip if already completed" branch in _run_through behaves
            # realistically.
            if phase not in self.ctx.completed_phases:
                self.ctx.completed_phases.append(phase)

    def test_materials_does_not_run_cloud_phases(self):
        """Running `_run_through(convert_materials)` on a fresh output must
        NOT run `upload_assets` or `resolve_assets` as prerequisites."""
        pipeline = self._StubPipeline()
        _run_through(pipeline, "convert_materials")

        assert "upload_assets" not in pipeline.run_log
        assert "resolve_assets" not in pipeline.run_log
        # But it must still run the phases it depends on.
        assert "parse" in pipeline.run_log
        assert "extract_assets" in pipeline.run_log
        assert "convert_materials" in pipeline.run_log

    def test_transpile_does_not_run_cloud_phases(self):
        pipeline = self._StubPipeline()
        _run_through(pipeline, "transpile_scripts")

        assert "upload_assets" not in pipeline.run_log
        assert "resolve_assets" not in pipeline.run_log
        assert "transpile_scripts" in pipeline.run_log

    def test_phase_order_preserved_without_cloud(self):
        """Prerequisites should still run in declared PHASES order when the
        cloud phases are filtered out."""
        pipeline = self._StubPipeline()
        _run_through(pipeline, "transpile_scripts")

        # parse and extract_assets must come before transpile_scripts, and
        # no cloud phase should appear anywhere in the log.
        parse_idx = pipeline.run_log.index("parse")
        extract_idx = pipeline.run_log.index("extract_assets")
        transpile_idx = pipeline.run_log.index("transpile_scripts")
        assert parse_idx < extract_idx < transpile_idx


class TestMakePipelineCrossProjectGuard:
    """Cross-project contamination guard: ``_make_pipeline`` must refuse to
    load a persisted ``conversion_context.json`` whose stored
    ``unity_project_path`` differs from the one the caller supplied. Mixing
    state across projects silently feeds the new project's GUID index with
    the old project's stale ``selected_scene`` / uploaded asset IDs and
    produces broken rbxlx output instead of a clean failure.

    The fix was originally called out in the Phase 1 deferred-fixes memo
    (C3, closed in commit ``86392e6``) but regressed. Re-landed 2026-04-24
    after the Codex review surfaced it again.
    """

    def test_mismatched_project_path_rejects_with_usage_error(self, tmp_path):
        from convert_interactive import _make_pipeline

        project_a = tmp_path / "ProjectA"
        project_b = tmp_path / "ProjectB"
        project_a.mkdir()
        project_b.mkdir()
        out = tmp_path / "out"
        out.mkdir()

        # Persist a context that was built for Project A.
        ctx = ConversionContext(unity_project_path=str(project_a.resolve()))
        ctx.save(out / "conversion_context.json")

        # Requesting Project B against the same output dir must fail loudly.
        with pytest.raises(click.exceptions.UsageError) as exc_info:
            _make_pipeline(str(project_b), str(out))

        msg = str(exc_info.value)
        assert str(project_a.resolve()) in msg
        assert str(project_b) in msg or str(project_b.resolve()) in msg

    def test_matching_project_path_loads_cleanly(self, tmp_path):
        from convert_interactive import _make_pipeline

        project = tmp_path / "Project"
        project.mkdir()
        out = tmp_path / "out"
        out.mkdir()

        ctx = ConversionContext(unity_project_path=str(project.resolve()))
        ctx.total_game_objects = 7
        ctx.save(out / "conversion_context.json")

        pipeline = _make_pipeline(str(project), str(out))
        # Context round-tripped from disk.
        assert pipeline.ctx.total_game_objects == 7

    def test_recovered_path_skips_comparison(self, tmp_path):
        """When caller passes unity_project_path=None, the path is recovered
        from the persisted context. The comparison must not fire in that
        branch since it would always compare a value to itself.
        """
        from convert_interactive import _make_pipeline

        project = tmp_path / "Project"
        project.mkdir()
        out = tmp_path / "out"
        out.mkdir()

        ctx = ConversionContext(unity_project_path=str(project.resolve()))
        ctx.save(out / "conversion_context.json")

        pipeline = _make_pipeline(None, str(out))
        assert str(pipeline.ctx.unity_project_path) == str(project.resolve())


class TestConversionContextSanitizedSave:
    """`ConversionContext.save_sanitized()` strips the fields that tie a
    conversion to a specific creator/place/experience before writing, so
    users can share a context.json in bug reports / forums without
    leaking uploaded asset URLs or Roblox IDs.
    """

    def test_sensitive_fields_are_stripped(self, tmp_path):
        ctx = ConversionContext(
            unity_project_path="/tmp/fake/unity",
            universe_id=9803233464,
            place_id=83382983775955,
            experience_name="My Private Test Game",
            uploaded_assets={
                "Assets/Sounds/music.mp3": "rbxassetid://73218644566508",
                "Assets/Textures/hero.png": "rbxassetid://111411402941783",
            },
            mesh_hierarchies={"some/mesh.fbx": [{"meshId": "rbxassetid://1"}]},
            total_game_objects=42,
            warnings=["sample warning"],
        )
        out = tmp_path / "context_sanitized.json"
        ctx.save_sanitized(out)

        assert out.exists()
        data = json.loads(out.read_text())
        # Stripped: credentials / identifiable fields
        assert data["universe_id"] is None
        assert data["place_id"] is None
        assert data["experience_name"] is None
        assert data["uploaded_assets"] == {}
        assert data["mesh_hierarchies"] == {}
        assert data["mesh_native_sizes"] == {}
        # Preserved: stats / workflow metadata
        assert data["unity_project_path"] == "/tmp/fake/unity"
        assert data["total_game_objects"] == 42
        assert data["warnings"] == ["sample warning"]
        # Marker so consumers know it's been sanitized
        assert data["_sanitized"] is True

    def test_full_save_preserves_everything(self, tmp_path):
        """`.save()` still writes the full state — sanitization is opt-in."""
        ctx = ConversionContext(
            universe_id=9803233464,
            uploaded_assets={"x": "rbxassetid://1"},
        )
        out = tmp_path / "context_full.json"
        ctx.save(out)

        data = json.loads(out.read_text())
        assert data["universe_id"] == 9803233464
        assert data["uploaded_assets"] == {"x": "rbxassetid://1"}
        assert "_sanitized" not in data

    def test_sprite_guid_to_file_round_trips(self, tmp_path):
        """sprite_extractor results survive save/load."""
        ctx = ConversionContext(
            sprite_guid_to_file={
                "abc123": "/out/sprites/hero.png",
                "def456:frame_01": "/out/sprites/frame_01.png",
            },
        )
        out = tmp_path / "ctx.json"
        ctx.save(out)
        loaded = ConversionContext.load(out)
        assert loaded.sprite_guid_to_file == ctx.sprite_guid_to_file


class TestAnthropicKeyBinding:
    """Regression: the pipeline must read ANTHROPIC_API_KEY lazily from the
    config module (``_config.ANTHROPIC_API_KEY``), not capture it at import
    time. Without lazy binding, the CLI has to patch both ``config`` and
    ``converter.pipeline`` module globals to make a new key visible — a
    historical footgun that left the transpiler seeing a stale ``None``.
    """

    def test_pipeline_sees_mutated_config_key(self, monkeypatch):
        import config
        from converter import pipeline as pipeline_module

        monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-ant-test-sentinel")
        # pipeline.py should resolve the key through its ``_config`` alias,
        # so a config-level mutation is immediately observable without also
        # touching pipeline_module.ANTHROPIC_API_KEY.
        assert pipeline_module._config.ANTHROPIC_API_KEY == "sk-ant-test-sentinel"
        # And the module must NOT export a captured top-level ANTHROPIC_API_KEY
        # (which would shadow the lazy lookup and defeat the fix).
        assert not hasattr(pipeline_module, "ANTHROPIC_API_KEY"), (
            "pipeline.py should not re-export ANTHROPIC_API_KEY at module level"
        )


class TestPreflight:
    def test_invalid_unity_project(self, tmp_path):
        runner = CliRunner()
        not_a_unity_project = tmp_path / "empty"
        not_a_unity_project.mkdir()
        out_dir = tmp_path / "out"

        code, payload = _invoke_json(
            runner, ["preflight", str(not_a_unity_project), str(out_dir)]
        )

        assert payload["phase"] == "preflight"
        assert payload["unity_project_valid"] is False
        assert payload["success"] is False
        # Output dir must be created even on failure so later phases have a home.
        assert out_dir.is_dir()

    def test_valid_unity_project_layout(self, tmp_path):
        """A directory containing an ``Assets/`` subdirectory must be recognised
        as a valid Unity project, independent of installed packages."""
        runner = CliRunner()
        fake_project = tmp_path / "FakeUnity"
        (fake_project / "Assets").mkdir(parents=True)
        out_dir = tmp_path / "out"

        _, payload = _invoke_json(
            runner, ["preflight", str(fake_project), str(out_dir)]
        )

        assert payload["unity_project_valid"] is True
        assert "missing_packages" in payload
        assert "python_version" in payload

    def test_preflight_reports_missing_optional_packages_separately(self, tmp_path):
        """`anthropic` is now in the optional package bucket — its absence
        must NOT make preflight report failure.  The payload should expose
        a `missing_optional_packages` field so the skill can decide whether
        to offer AI-assisted transpilation."""
        runner = CliRunner()
        fake_project = tmp_path / "FakeUnity"
        (fake_project / "Assets").mkdir(parents=True)
        out_dir = tmp_path / "out"

        _, payload = _invoke_json(
            runner, ["preflight", str(fake_project), str(out_dir)]
        )

        # The optional-packages field is always present, even when it's empty.
        assert "missing_optional_packages" in payload
        assert isinstance(payload["missing_optional_packages"], list)

    def test_nested_unity_project_detection(self, tmp_path):
        """preflight should walk one level deep for nested Unity projects
        (matches ChopChop/PrefabWorkflows layout)."""
        runner = CliRunner()
        outer = tmp_path / "Outer"
        inner = outer / "UOP1_Project"
        (inner / "Assets").mkdir(parents=True)
        out_dir = tmp_path / "out"

        _, payload = _invoke_json(runner, ["preflight", str(outer), str(out_dir)])

        assert payload["unity_project_valid"] is True
        assert payload["nested_root"] == str(inner)


# ---------------------------------------------------------------------------
# status — no-state and resumption paths
# ---------------------------------------------------------------------------


class TestStatus:
    def test_no_conversion_in_progress(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "never_converted"
        out_dir.mkdir()

        _, payload = _invoke_json(runner, ["status", str(out_dir)])

        assert payload["phase"] == "status"
        assert payload["status"] == "no_conversion"

    def test_status_reflects_marked_skill_phases(self, tmp_path):
        """If the skill state file exists, status should report completed
        skill phases and compute the next one."""
        runner = CliRunner()
        out_dir = tmp_path / "in_progress"
        out_dir.mkdir()

        # Minimal ConversionContext on disk so status takes the in_progress path.
        ctx = ConversionContext(unity_project_path="/tmp/fake/unity")
        ctx.save(out_dir / "conversion_context.json")

        # Skill state with discover + inventory completed.
        (out_dir / ".convert_state.json").write_text(
            json.dumps({"completed_skill_phases": ["discover", "inventory"]})
        )

        _, payload = _invoke_json(runner, ["status", str(out_dir)])

        assert payload["status"] == "in_progress"
        assert payload["completed_skill_phases"] == ["discover", "inventory"]
        assert payload["next_skill_phase"] == "materials"
        assert payload["context"]["unity_project_path"] == "/tmp/fake/unity"


# ---------------------------------------------------------------------------
# validate — runs against already-transpiled .lua/.luau files on disk
# ---------------------------------------------------------------------------


class TestValidate:
    def test_no_lua_files_returns_error(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        # No scripts/ subdirectory → validate should report no files.

        _, payload = _invoke_json(runner, ["validate", str(out_dir)])

        assert payload["phase"] == "validate"
        assert payload["success"] is False
        assert any("No .lua/.luau files" in e for e in payload["errors"])

    def test_dry_run_does_not_write_files(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "out"
        scripts_dir = out_dir / "scripts"
        scripts_dir.mkdir(parents=True)

        # A minimally malformed Luau file so the validator has something to fix.
        bad_file = scripts_dir / "bad.lua"
        original_source = 'local x = "hello"\nprint (x)\n'
        bad_file.write_text(original_source)

        _, payload = _invoke_json(runner, ["validate", str(out_dir)])

        assert payload["phase"] == "validate"
        assert payload["success"] is True
        assert payload["wrote_changes"] is False
        # File contents unchanged regardless of whether the validator fixed anything.
        assert bad_file.read_text() == original_source

        # Skill state should now mark validate as complete.
        state = _load_skill_state(out_dir)
        assert "validate" in state.get("completed_skill_phases", [])


# ---------------------------------------------------------------------------
# upload — error paths that don't require a real Roblox API call
# ---------------------------------------------------------------------------


class TestUploadErrorPaths:
    def test_missing_context_is_reported(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        _, payload = _invoke_json(runner, ["upload", str(out_dir)])

        assert payload["phase"] == "upload"
        assert payload["success"] is False
        assert any("conversion_context.json" in e for e in payload["errors"])

    def test_missing_rbxlx_is_reported(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "out"
        out_dir.mkdir()
        ctx = ConversionContext(unity_project_path="/tmp/fake/unity")
        ctx.save(out_dir / "conversion_context.json")

        _, payload = _invoke_json(runner, ["upload", str(out_dir)])

        assert payload["success"] is False
        assert any("RBXLX not found" in e for e in payload["errors"])


# ---------------------------------------------------------------------------
# report — no context on disk
# ---------------------------------------------------------------------------


class TestReport:
    def test_missing_context_is_reported(self, tmp_path):
        runner = CliRunner()
        out_dir = tmp_path / "out"
        out_dir.mkdir()

        _, payload = _invoke_json(runner, ["report", str(out_dir)])

        assert payload["phase"] == "report"
        assert payload["success"] is False


# ---------------------------------------------------------------------------
# discover — full e2e against SimpleFPS (slow)
# ---------------------------------------------------------------------------


@pytest.mark.slow
class TestDiscoverE2E:
    """End-to-end: run `discover` against the real SimpleFPS fixture.

    Mirrors the setup pattern from test_pipeline_e2e.py — AI transpilation
    is disabled and the test is skipped if the fixture isn't available.
    """

    @pytest.fixture(autouse=True)
    def _disable_ai(self):
        import config
        old = config.USE_AI_TRANSPILATION
        config.USE_AI_TRANSPILATION = False
        yield
        config.USE_AI_TRANSPILATION = old

    def test_discover_against_simplefps(self, tmp_path, simplefps_project):
        runner = CliRunner()
        out_dir = tmp_path / "simplefps_out"

        code, payload = _invoke_json(
            runner, ["discover", str(simplefps_project), str(out_dir)]
        )

        assert code == 0, f"discover failed: {payload}"
        assert payload["phase"] == "discover"
        assert payload["success"] is True
        assert payload["scene_count"] >= 1
        assert payload["total_game_objects"] > 0

        # Context file should have been written by _run_phase's save hook.
        ctx_path = out_dir / "conversion_context.json"
        assert ctx_path.exists()

        # Skill state should mark `discover` as completed, and `status` should
        # report `inventory` as the next skill phase.
        state = _load_skill_state(out_dir)
        assert "discover" in state["completed_skill_phases"]

        _, status_payload = _invoke_json(runner, ["status", str(out_dir)])
        assert status_payload["status"] == "in_progress"
        assert status_payload["next_skill_phase"] == "inventory"
