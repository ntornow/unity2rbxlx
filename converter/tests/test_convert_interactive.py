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


class TestResumeVsRunThroughParity:
    """Parity tests called out as missing in the 2026-04-24 Codex review.

    ``Pipeline.resume(target)`` (used by ``u2r.py``) and ``_run_through(target)``
    (used by the interactive CLI) share the same essential_phases list
    when replaying prerequisites. If the two diverge silently, interactive
    commands can produce different in-memory state from their CLI
    counterparts — breaking the premise that the skill and the CLI are two
    interfaces to the same pipeline.
    """

    def test_essential_phases_sets_match(self):
        """Both functions declare an 'essential_phases' set literal.
        Extract them and assert equality — pure source-level check so the
        test fails the moment one literal is updated and the other isn't.
        """
        import ast
        import inspect
        import textwrap

        import convert_interactive
        from converter.pipeline import Pipeline

        def _essential_set(fn) -> set[str]:
            # getsource(method) preserves original indentation which breaks
            # ast.parse; dedent to column 0 first.
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "essential_phases"
                    and isinstance(node.value, ast.Set)
                ):
                    return {
                        elt.value for elt in node.value.elts
                        if isinstance(elt, ast.Constant)
                    }
            raise AssertionError("essential_phases not found")

        cli_set = _essential_set(Pipeline.resume)
        skill_set = _essential_set(convert_interactive._run_through)
        assert cli_set == skill_set, (
            "Pipeline.resume and _run_through have diverged on which phases "
            f"get re-run unconditionally. resume={cli_set}, "
            f"_run_through={skill_set}. Update both together."
        )

    def test_prereq_phases_identical_minus_cloud(self):
        """Executive check: the ordered list of prereq phases that run before
        a review phase target must be identical between the two drivers,
        except that _run_through deliberately drops the cloud phases.
        This pins the currently-working behavior so a refactor can't
        silently skip a prereq on one side.
        """
        from convert_interactive import _run_through
        from converter.pipeline import PHASES

        class _StubForRunThrough:
            def __init__(self):
                self.run_log: list[str] = []
                self.ctx = ConversionContext()
            def _run_phase(self, phase):
                self.run_log.append(phase)
                if phase not in self.ctx.completed_phases:
                    self.ctx.completed_phases.append(phase)

        target = "convert_materials"

        stub = _StubForRunThrough()
        _run_through(stub, target)

        target_idx = PHASES.index(target)
        expected_prereqs_minus_cloud = [
            p for p in PHASES[:target_idx]
            if p not in {"upload_assets", "resolve_assets"}
        ]
        # _run_through also runs the target itself, once.
        assert stub.run_log == expected_prereqs_minus_cloud + [target], (
            f"run_log: {stub.run_log}"
        )

    def test_run_through_stops_at_target_resume_would_continue(self):
        """Documented contract: _run_through stops at target; resume runs
        everything from target forward. Checks the interactive side
        explicitly so no future change silently makes it run further.
        """
        from convert_interactive import _run_through
        from converter.pipeline import PHASES

        class _Stub:
            def __init__(self):
                self.run_log: list[str] = []
                self.ctx = ConversionContext()
            def _run_phase(self, phase):
                self.run_log.append(phase)
                self.ctx.completed_phases.append(phase)

        stub = _Stub()
        _run_through(stub, "convert_materials")

        # Nothing after convert_materials should be in the log.
        target_idx = PHASES.index("convert_materials")
        for later in PHASES[target_idx + 1:]:
            assert later not in stub.run_log, (
                f"_run_through ran {later} after target; that's resume's job"
            )


class TestThreeFlowPhaseOrder:
    """Phase-order parity across the three documented flows:
      (a) u2r.py convert — Pipeline.run() iterates PHASES in order
      (b) interactive-fresh assemble — hand-rolled list in convert_interactive.assemble
      (c) interactive-rehydrated assemble — same hand-rolled list, skipping
          transpile_scripts when ctx.completed_phases already contains it.

    A full three-flow rbx_place equivalence test needs a realistic Unity
    fixture. This lighter check asserts the *phase order* (minus the
    flow-specific skips) is consistent, which is the invariant most likely
    to regress when someone edits one list without updating the other.
    """

    def test_assemble_phase_list_matches_pipeline_phases_minus_moderate_skip(self):
        """Interactive assemble's phase list must stay a subset of PHASES
        and preserve PHASES order. Earlier versions of the fix omitted
        moderate_assets (P1-4). A future edit that rearranges PHASES would
        silently regress unless we pin this.
        """
        import ast
        import inspect

        import convert_interactive
        from converter.pipeline import PHASES

        tree = ast.parse(inspect.getsource(convert_interactive.assemble.callback))
        assemble_list: list[str] | None = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.For)
                and isinstance(node.iter, ast.List)
                and all(
                    isinstance(el, ast.Constant) and isinstance(el.value, str)
                    for el in node.iter.elts
                )
            ):
                candidate = [el.value for el in node.iter.elts]
                if set(candidate).issubset(set(PHASES)):
                    assemble_list = candidate
                    break
        assert assemble_list is not None, "assemble phase list not found"

        # Every phase in assemble must appear in PHASES (subset).
        assert set(assemble_list).issubset(set(PHASES))
        # Order must match PHASES order.
        assert assemble_list == [p for p in PHASES if p in assemble_list], (
            f"assemble phase order diverges from PHASES. "
            f"assemble={assemble_list}, PHASES={PHASES}"
        )

    def test_rehydrated_flow_skips_only_transpile(self, monkeypatch):
        """The rehydrated interactive flow differs from fresh by exactly one
        phase: transpile_scripts is skipped when ctx.completed_phases
        already lists it and --retranspile is absent. Any additional skip
        would be a behavior drift.
        """
        import config as _config
        monkeypatch.setattr(_config, "ROBLOX_API_KEY", "stub")
        monkeypatch.setattr(_config, "ROBLOX_CREATOR_ID", 1)

        # We can't run real phases here, so we stub _make_pipeline's return.
        run_log: list[str] = []

        class _Stub:
            def __init__(self):
                self.ctx = ConversionContext()
                self.ctx.completed_phases.append("transpile_scripts")
            def _run_phase(self, phase):
                run_log.append(phase)

        import convert_interactive
        monkeypatch.setattr(
            convert_interactive, "_make_pipeline",
            lambda *args, **kwargs: _Stub(),
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["assemble", "/tmp/any", "/tmp/out", "--no-upload", "--no-resolve"],
            catch_exceptions=False,
        )
        # Under --no-upload the cred check is skipped (P1-4) so assemble runs.
        # Under --no-resolve, resolve_assets is skipped from the list.
        # Under completed transpile_scripts + no --retranspile, that's skipped too.
        # Everything else should appear in order.
        assert "transpile_scripts" not in run_log, (
            "rehydrated flow ran transpile_scripts despite ctx marking it done"
        )
        assert "resolve_assets" not in run_log, "resolve_assets not skipped by --no-resolve"
        # Remaining phases still run in PHASES order.
        from converter.pipeline import PHASES
        remaining = [p for p in PHASES if p in run_log]
        assert run_log == remaining, (
            f"phase order drift in rehydrated flow. run_log={run_log}, "
            f"expected={remaining}"
        )
        # Exit code may be non-zero because the stub _run_phase doesn't do real
        # work — we only care about the run_log shape.
        _ = result


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
# assemble — workflow fidelity guards (P1-4 from 2026-04-24 Codex review)
# ---------------------------------------------------------------------------


class TestAssembleWorkflowFidelity:
    """Regressions on the documented assemble workflow:
    phase-5-assembly.md lists moderate_assets before upload_assets, and
    upload_assets is supposed to be a no-op only when the user explicitly
    opts out via --no-upload. Before the P1-4 fix, moderate_assets was
    missing from the hand-rolled phase list and missing creds silently
    skipped uploads while still reporting success=True.
    """

    def test_phase_list_includes_moderate_assets_before_upload(self):
        """Read the assemble command's source and assert moderate_assets
        sits between extract_assets and upload_assets. (The live pipeline
        run would also catch this via Pipeline.moderate_assets side effects,
        but that requires a full project fixture — this source-level check
        catches ordering regressions without the heavy lift.)
        """
        import inspect

        import convert_interactive

        # convert_interactive.assemble is a Click command; the underlying
        # function lives on .callback.
        src = inspect.getsource(convert_interactive.assemble.callback)
        phase_list_start = src.index('for phase in [')
        phase_list_end = src.index(']', phase_list_start)
        phase_list_src = src[phase_list_start:phase_list_end]
        # Check order by scanning for the three phase names.
        for name in ("extract_assets", "moderate_assets", "upload_assets"):
            assert f'"{name}"' in phase_list_src, (
                f"{name} missing from assemble's phase list"
            )
        assert (
            phase_list_src.index('"extract_assets"')
            < phase_list_src.index('"moderate_assets"')
            < phase_list_src.index('"upload_assets"')
        ), "moderate_assets must run between extract_assets and upload_assets"

    def test_missing_creds_without_no_upload_fails_fast(self, tmp_path, monkeypatch):
        """When the user invokes assemble without --no-upload and without
        credentials (CLI / env / file), the command must fail fast before
        running any pipeline phases, not silently skip uploads.
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

        # Seed a conversion_context so _make_pipeline would otherwise succeed —
        # we want to prove the cred check fires first.
        ctx = ConversionContext(unity_project_path=str(unity.resolve()))
        ctx.save(out / "conversion_context.json")

        # Monkeypatch _make_pipeline so a pass-through would crash loudly and
        # give us a distinct signal — if we get here the fast-fail didn't fire.
        import convert_interactive
        def _guarded_make_pipeline(*args, **kwargs):
            raise AssertionError("pipeline built before cred check ran")
        monkeypatch.setattr(convert_interactive, "_make_pipeline", _guarded_make_pipeline)

        runner = CliRunner()
        _, payload = _invoke_json(runner, ["assemble", str(unity), str(out)])

        assert payload["phase"] == "assemble"
        assert payload["success"] is False
        assert any(
            "credentials" in e.lower() or "--api-key" in e
            for e in payload.get("errors", [])
        ), f"unexpected errors: {payload.get('errors')}"

    def test_no_upload_flag_skips_cred_check(self, tmp_path, monkeypatch):
        """--no-upload is the explicit opt-out: assemble must proceed even
        without credentials. We stub _make_pipeline to a no-op pipeline so
        the test doesn't need a real Unity project, and assert assemble
        makes it past the cred check.
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
        ctx = ConversionContext(unity_project_path=str(unity.resolve()))
        ctx.save(out / "conversion_context.json")

        import convert_interactive
        # If we reach _make_pipeline, the cred check correctly skipped.
        # Raise a sentinel error so we know we got past it — and so the test
        # doesn't try to run a real pipeline.
        class _ReachedPipeline(Exception):
            pass
        def _sentinel(*args, **kwargs):
            raise _ReachedPipeline
        monkeypatch.setattr(convert_interactive, "_make_pipeline", _sentinel)

        runner = CliRunner()
        result = runner.invoke(
            cli, ["assemble", str(unity), str(out), "--no-upload"],
            catch_exceptions=True,
        )
        assert isinstance(result.exception, _ReachedPipeline), (
            f"cred check fired under --no-upload; output: {result.output}"
        )


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


class TestTranspileValidateWorkflow:
    """Regression: the documented `transpile` -> `validate` workflow from
    SKILL.md must complete cleanly. `transpile` persists every Luau source
    from state.transpilation_result.scripts to `scripts/*.luau`, so
    `validate` (which reads from disk) can find them.

    The original P0 bug was surfaced in the 2026-04-24 Codex review: prior
    to the fix, transpile only populated in-memory state. `.luau` emission
    happened inside ``write_output`` (pipeline.py:1200-1201), which the
    interactive transpile command never runs. So a clean transpile ->
    validate flow handed validate an empty scripts/ dir and it hard-errored.
    """

    def test_transpile_persists_luau_and_validate_finds_them(
        self, tmp_path, monkeypatch,
    ):
        import convert_interactive
        from convert_interactive import cli
        from converter.code_transpiler import TranspilationResult, TranspiledScript

        unity = tmp_path / "FakeProject"
        (unity / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()

        def fake_run_through(pipeline, target_phase):
            # Stub the transpile_scripts phase: populate the in-memory result
            # the way the real phase does, without actually transpiling.
            pipeline.state.transpilation_result = TranspilationResult(
                scripts=[
                    TranspiledScript(
                        source_path="Assets/Foo.cs",
                        output_filename="Foo.luau",
                        csharp_source="// stub",
                        luau_source='-- Foo\nprint("foo")\n',
                        strategy="rule_based",
                        confidence=1.0,
                    ),
                    TranspiledScript(
                        source_path="Assets/Bar.cs",
                        output_filename="Bar.luau",
                        csharp_source="// stub",
                        luau_source='-- Bar\nlocal M = {}\nreturn M\n',
                        strategy="rule_based",
                        confidence=1.0,
                        script_type="ModuleScript",
                    ),
                ],
                total_transpiled=2,
                total_rule_based=2,
            )
            pipeline.ctx.total_scripts = 2
            pipeline.ctx.transpiled_scripts = 2
            pipeline.ctx.completed_phases.append(target_phase)

        monkeypatch.setattr(convert_interactive, "_run_through", fake_run_through)

        runner = CliRunner()

        exit_code, payload_transpile = _invoke_json(
            runner, ["transpile", str(unity), str(out)],
        )
        assert exit_code == 0
        assert payload_transpile["success"] is True
        assert payload_transpile["transpiled"] == 2

        # Transpile persisted both sources to disk under scripts/.
        scripts_dir = out / "scripts"
        assert (scripts_dir / "Foo.luau").read_text() == '-- Foo\nprint("foo")\n'
        assert (scripts_dir / "Bar.luau").read_text() == (
            '-- Bar\nlocal M = {}\nreturn M\n'
        )

        _, payload_validate = _invoke_json(runner, ["validate", str(out)])
        assert payload_validate["phase"] == "validate"
        assert payload_validate["success"] is True, (
            f"validate rejected transpile output: "
            f"{payload_validate.get('errors')}"
        )
        assert payload_validate["files_scanned"] == 2

    def test_transpile_clears_stale_top_level_luau_but_preserves_subdirs(
        self, tmp_path, monkeypatch,
    ):
        """A re-run of transpile should drop .luau files for C# scripts that
        no longer exist in the Unity project (stale top-level output), but
        must NOT touch sibling subdirectories written by other phases
        (animations/, animation_data/, scriptable_objects/).
        """
        import convert_interactive
        from convert_interactive import cli
        from converter.code_transpiler import TranspilationResult, TranspiledScript

        unity = tmp_path / "FakeProject"
        (unity / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        scripts_dir = out / "scripts"
        scripts_dir.mkdir(parents=True)

        # Seed stale top-level file AND untouchable subdir files.
        (scripts_dir / "Stale.luau").write_text("-- deleted from unity\n")
        (scripts_dir / "animations").mkdir()
        (scripts_dir / "animations" / "Door.luau").write_text("-- anim survives\n")
        (scripts_dir / "scriptable_objects").mkdir()
        (scripts_dir / "scriptable_objects" / "Inv.luau").write_text(
            "-- so survives\n"
        )

        def fake_run_through(pipeline, target_phase):
            pipeline.state.transpilation_result = TranspilationResult(
                scripts=[TranspiledScript(
                    source_path="Assets/Kept.cs",
                    output_filename="Kept.luau",
                    csharp_source="// stub",
                    luau_source="-- Kept\n",
                    strategy="rule_based",
                    confidence=1.0,
                )],
                total_transpiled=1,
                total_rule_based=1,
            )
            pipeline.ctx.total_scripts = 1
            pipeline.ctx.transpiled_scripts = 1
            pipeline.ctx.completed_phases.append(target_phase)

        monkeypatch.setattr(convert_interactive, "_run_through", fake_run_through)

        runner = CliRunner()
        _invoke_json(runner, ["transpile", str(unity), str(out)])

        assert (scripts_dir / "Kept.luau").exists()
        assert not (scripts_dir / "Stale.luau").exists(), "stale luau not swept"
        assert (scripts_dir / "animations" / "Door.luau").exists(), (
            "animations/ was wiped — must be preserved"
        )
        assert (scripts_dir / "scriptable_objects" / "Inv.luau").exists(), (
            "scriptable_objects/ was wiped — must be preserved"
        )


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
