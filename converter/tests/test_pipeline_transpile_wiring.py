"""Pipeline.transpile_scripts wiring test for the scene-runtime contract.

PR3a built ``contract_pipeline.transpile_with_contract`` but never wired
it into ``Pipeline.transpile_scripts``. As shipped, every
``--scene-runtime=generic`` conversion silently transpiled in legacy
mode and produced 28 non-compliant ReplicatedStorage modules that
crashed at line 14 once PR4's host runtime tried to require them
(``CFrame is not a valid member of ReplicatedStorage``).

This file pins the wiring: under generic mode the contract pipeline
runs; under legacy the legacy transpiler runs; fail-closed rows and
runtime-bearing paths land on ``ctx.scene_runtime`` so downstream
consumers (auto-mode fallback decision, post-run reports) can read
them without re-running the orchestrator.

Legacy byte-equivalence is asserted by
``test_legacy_mode_calls_legacy_transpile_scripts``: under default
``scene_runtime_mode="legacy"`` the legacy entry is called with the
exact kwargs it accepted pre-PR3a (no ``runtime_mode``, no
``runtime_bearing_paths``). The byte-identical-emit invariant that
PR3a / PR3b / PR3c committed to depends on this.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import TranspilationResult, TranspiledScript
from converter.contract_pipeline import (
    ContractPipelineResult,
    FailClosed,
)
from converter.pipeline import Pipeline
from core.roblox_types import RbxPlace


# ---------------------------------------------------------------------------
# Synthetic ScriptInfo + planner artifact -- the contract pipeline only
# reads ``info.path``, ``info.class_name``, ``info.referenced_types``, and
# the ``scene_runtime.modules`` map (stem + runtime_bearing + class_name).
# A real ``analyze_all_scripts`` invocation would walk disk and read .cs
# files we don't have here; we mock it instead.
# ---------------------------------------------------------------------------


class _ScriptInfo:
    """Stand-in for ``unity.script_analyzer.ScriptInfo``."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []


def _make_pipeline(tmp_path: Path) -> Pipeline:
    unity_project = tmp_path / "unity"
    unity_project.mkdir()
    (unity_project / "Assets").mkdir()
    output = tmp_path / "out"
    output.mkdir()
    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.state.rbx_place = RbxPlace()
    return pipeline


def _seed_runtime_bearing_module(pipeline: Pipeline, stem: str = "Foo") -> None:
    pipeline.ctx.scene_runtime = {
        "modules": {
            "guid-a": {
                "stem": stem,
                "class_name": stem,
                "runtime_bearing": True,
                # Phase 2a slice 2: build_topology invariant 7 requires
                # both booleans on every runtime_bearing planner row.
                "character_attached": False,
                "is_loader": False,
            },
        },
        "scenes": {},
        "prefabs": {},
        "domain_overrides": {},
    }


def _make_contract_result(
    *,
    fail_closed: list[FailClosed] | None = None,
    runtime_bearing_paths: frozenset[Path] | None = None,
) -> ContractPipelineResult:
    transpilation = TranspilationResult()
    transpilation.total_transpiled = 1
    transpilation.scripts.append(
        TranspiledScript(
            source_path="/proj/Assets/Foo.cs",
            output_filename="Foo.luau",
            csharp_source="",
            luau_source="return {}",
            strategy="ai",
            confidence=1.0,
            script_type="ModuleScript",
        )
    )
    return ContractPipelineResult(
        transpilation=transpilation,
        require_resolutions=[],
        fail_closed=list(fail_closed or []),
        runtime_bearing_paths=runtime_bearing_paths or frozenset(),
    )


# ---------------------------------------------------------------------------
# Fix 1 wiring -- generic vs legacy routing.
# ---------------------------------------------------------------------------


class TestGenericModeRoutesThroughContractPipeline:

    def test_generic_mode_calls_transpile_with_contract(
        self, tmp_path: Path,
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime_mode = "generic"
        _seed_runtime_bearing_module(pipeline)

        infos = [_ScriptInfo(tmp_path / "unity" / "Assets" / "Foo.cs", "Foo")]
        runtime_bearing_paths = frozenset({infos[0].path})
        contract_result = _make_contract_result(
            runtime_bearing_paths=runtime_bearing_paths,
        )

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.contract_pipeline.transpile_with_contract",
            return_value=contract_result,
        ) as mock_contract, patch(
            "converter.code_transpiler.transpile_scripts",
        ) as mock_legacy, patch(
            "converter.shared_state_linter.lint_and_rewrite",
            return_value=[],
        ):
            pipeline.transpile_scripts()

        assert mock_contract.called, (
            "Generic mode MUST route through transpile_with_contract; "
            "the legacy entry produces non-compliant modules that crash "
            "PR4's host runtime."
        )
        assert not mock_legacy.called, (
            "Legacy transpile_scripts must NOT be called under generic "
            "mode -- the contract pipeline is the only valid producer."
        )
        # Pin the FULL kwarg contract for transpile_with_contract --
        # missing any of these on the call site would either disable
        # the contract verifier or fail to thread the planner artifact.
        assert mock_contract.call_args.args == ()
        kwargs = mock_contract.call_args.kwargs
        assert set(kwargs.keys()) == {
            "unity_project_path",
            "script_infos",
            "scene_runtime",
            "use_ai",
            "api_key",
            "serialized_field_refs",
            "parsed_scenes",
            "prefab_library",
            "guid_index",
        }
        assert kwargs["script_infos"] == infos
        assert kwargs["scene_runtime"] is pipeline.ctx.scene_runtime
        # Verify the result landed on the state.
        assert pipeline.state.transpilation_result is contract_result.transpilation

    def test_legacy_mode_calls_legacy_transpile_scripts(
        self, tmp_path: Path,
    ) -> None:
        """Byte-equivalence guard: default ``scene_runtime_mode=legacy``
        must call the legacy ``transpile_scripts`` entry with the
        pre-PR3a kwarg set (no ``runtime_mode``, no
        ``runtime_bearing_paths``). Adding either kwarg here would
        drift the legacy output away from byte-identical to pre-PR3a."""
        pipeline = _make_pipeline(tmp_path)
        # ctx.scene_runtime_mode defaults to "legacy".
        assert pipeline.ctx.scene_runtime_mode == "legacy"

        infos = [_ScriptInfo(tmp_path / "unity" / "Assets" / "Foo.cs", "Foo")]
        legacy_result = TranspilationResult()

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.contract_pipeline.transpile_with_contract",
        ) as mock_contract, patch(
            "converter.code_transpiler.transpile_scripts",
            return_value=legacy_result,
        ) as mock_legacy, patch(
            "converter.shared_state_linter.lint_and_rewrite",
            return_value=[],
        ):
            pipeline.transpile_scripts()

        assert mock_legacy.called, (
            "Legacy mode MUST call code_transpiler.transpile_scripts."
        )
        assert not mock_contract.called, (
            "Legacy mode must NEVER touch the contract pipeline -- "
            "byte-identical-emit is a tested invariant."
        )
        # Pin the EXACT legacy kwargs. If a future PR threads
        # ``runtime_mode`` or ``runtime_bearing_paths`` into the legacy
        # branch, this assertion explodes -- which is the point. PR3a's
        # invariant ("legacy emit byte-unchanged") relies on the legacy
        # entry receiving no new kwargs.
        assert mock_legacy.call_args.args == (), (
            "Legacy transpile_scripts must be called keyword-only -- "
            "positional args would mask a kwarg drift in this assertion."
        )
        kwargs = mock_legacy.call_args.kwargs
        # The set of kwargs must be EXACTLY the pre-PR3a set. Asserting
        # the exact set (not just absence of the new ones + presence of
        # the old ones) catches any future kwarg drift -- including
        # additions we haven't yet thought to forbid.
        assert set(kwargs.keys()) == {
            "unity_project_path",
            "script_infos",
            "use_ai",
            "api_key",
            "serialized_field_refs",
        }, (
            f"Legacy transpile_scripts kwarg set drifted from the "
            f"pre-PR3a contract: {sorted(kwargs.keys())}. Any addition "
            f"would risk changing legacy-mode output."
        )

    def test_auto_mode_falls_back_to_legacy(self, tmp_path: Path) -> None:
        """``auto`` is PR5's territory -- under PR3-era plumbing the
        only generic-routing trigger is ``scene_runtime_mode ==
        'generic'`` exactly. ``auto`` must not unconditionally route
        through the contract pipeline (PR5 owns the routing decision).
        """
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime_mode = "auto"

        infos = [_ScriptInfo(tmp_path / "unity" / "Assets" / "Foo.cs", "Foo")]
        legacy_result = TranspilationResult()

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.contract_pipeline.transpile_with_contract",
        ) as mock_contract, patch(
            "converter.code_transpiler.transpile_scripts",
            return_value=legacy_result,
        ) as mock_legacy, patch(
            "converter.shared_state_linter.lint_and_rewrite",
            return_value=[],
        ):
            pipeline.transpile_scripts()

        assert mock_legacy.called
        assert not mock_contract.called


class TestContractTelemetryLandsOnCtx:

    def test_fail_closed_rows_plumbed_to_ctx(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime_mode = "generic"
        _seed_runtime_bearing_module(pipeline)
        infos = [_ScriptInfo(tmp_path / "unity" / "Assets" / "Foo.cs", "Foo")]
        fc = FailClosed(kind="verifier", detail="Foo.cs: 1 violation")
        contract_result = _make_contract_result(fail_closed=[fc])

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.contract_pipeline.transpile_with_contract",
            return_value=contract_result,
        ), patch(
            "converter.shared_state_linter.lint_and_rewrite",
            return_value=[],
        ):
            pipeline.transpile_scripts()

        rows = pipeline.ctx.scene_runtime.get("contract_fail_closed")
        assert isinstance(rows, list)
        assert len(rows) == 1
        assert rows[0] == {"kind": "verifier", "detail": "Foo.cs: 1 violation"}

    def test_runtime_bearing_paths_persisted_as_json_friendly_list(
        self, tmp_path: Path,
    ) -> None:
        """``runtime_bearing_paths`` is a frozenset[Path] in memory;
        ``ctx.save`` serializes through json, which can't carry either
        type. Persist as a sorted list of strings so a resume run
        reproduces the same set."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime_mode = "generic"
        _seed_runtime_bearing_module(pipeline)
        infos = [_ScriptInfo(tmp_path / "unity" / "Assets" / "Foo.cs", "Foo")]
        paths = frozenset({Path("/proj/Assets/B.cs"), Path("/proj/Assets/A.cs")})
        contract_result = _make_contract_result(runtime_bearing_paths=paths)

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.contract_pipeline.transpile_with_contract",
            return_value=contract_result,
        ), patch(
            "converter.shared_state_linter.lint_and_rewrite",
            return_value=[],
        ):
            pipeline.transpile_scripts()

        stored = pipeline.ctx.scene_runtime["runtime_bearing_paths"]
        assert stored == ["/proj/Assets/A.cs", "/proj/Assets/B.cs"]

    def test_fail_closed_rows_extend_on_resume(self, tmp_path: Path) -> None:
        """When ``ctx.scene_runtime['contract_fail_closed']`` already
        carries rows (e.g. from a prior phase replay during resume),
        the wiring must extend rather than clobber."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime_mode = "generic"
        _seed_runtime_bearing_module(pipeline)
        prior = {"kind": "stub_strategy", "detail": "carried over"}
        pipeline.ctx.scene_runtime["contract_fail_closed"] = [prior]
        infos = [_ScriptInfo(tmp_path / "unity" / "Assets" / "Foo.cs", "Foo")]
        new_fc = FailClosed(kind="verifier", detail="new failure")
        contract_result = _make_contract_result(fail_closed=[new_fc])

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=infos,
        ), patch(
            "converter.contract_pipeline.transpile_with_contract",
            return_value=contract_result,
        ), patch(
            "converter.shared_state_linter.lint_and_rewrite",
            return_value=[],
        ):
            pipeline.transpile_scripts()

        rows = pipeline.ctx.scene_runtime["contract_fail_closed"]
        assert rows == [
            prior,
            {"kind": "verifier", "detail": "new failure"},
        ]


class TestTranspileShortCircuitOnNoScripts:

    def test_no_scripts_does_not_invoke_either_transpiler(
        self, tmp_path: Path,
    ) -> None:
        """The existing short-circuit (no script_infos -> return) must
        keep working under generic mode. The contract pipeline only
        runs when there's something to transpile."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime_mode = "generic"
        _seed_runtime_bearing_module(pipeline)

        with patch(
            "unity.script_analyzer.analyze_all_scripts",
            return_value=[],
        ), patch(
            "converter.contract_pipeline.transpile_with_contract",
        ) as mock_contract, patch(
            "converter.code_transpiler.transpile_scripts",
        ) as mock_legacy:
            pipeline.transpile_scripts()

        assert not mock_contract.called
        assert not mock_legacy.called


# ---------------------------------------------------------------------------
# Legacy repair passes gated OFF in generic mode (contract allowlist).
#
# ``Pipeline._subphase_cohere_scripts`` runs three post-transpile passes:
#   1. ``rewrite_asset_references`` — allowlisted, runs in BOTH modes.
#   2. ``inject_require_calls`` — legacy require injection, OFF in generic.
#   3. ``fix_require_classifications`` -> ``run_packs`` — legacy coherence
#      packs, OFF in generic.
#
# The scene-runtime contract (docs/design/scene-runtime-contract.md:151-169)
# is an allowlist: ALL legacy repair passes are OFF in generic. Before this
# gate, the legacy door-tween pack ran in generic and appended a dead
# ``script.Parent``-based ``do...end`` block to the generic Door ModuleScript.
# ---------------------------------------------------------------------------


def _make_generic_door_script():
    """A generic-mode Door ModuleScript shaped like SimpleFPS's: flips the
    ``open`` attribute on a sibling ``door`` mesh, ends with ``return Door``.
    Trips the ``door_tween_open`` pack detector."""
    from core.roblox_types import RbxScript

    return RbxScript(
        name="Door",
        source=(
            "local Door = {}\n"
            "Door.__index = Door\n\n"
            "function Door:getDoorAnim()\n"
            '    return self.gameObject.Parent:FindFirstChild("door")\n'
            "end\n\n"
            "function Door:ToggleDoor(value)\n"
            "    local doorAnim = self:getDoorAnim()\n"
            "    if doorAnim then\n"
            '        doorAnim:SetAttribute("open", value)\n'
            "    end\n"
            "end\n\n"
            "return Door\n"
        ),
        script_type="ModuleScript",
    )


class TestLegacyRepairPassesGatedInGeneric:

    def test_generic_mode_skips_coherence_packs_on_door(
        self, tmp_path: Path,
    ) -> None:
        """In generic mode the legacy door-tween pack must NOT run, so the
        Door module gets no ``_AutoFpsDoorTweenInjected`` block, no appended
        ``do`` block, and still ends with its terminal ``return Door``."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.ctx.scene_runtime_mode = "generic"
        door = _make_generic_door_script()
        pipeline.state.rbx_place.scripts = [door]

        pipeline._subphase_cohere_scripts()

        assert "_AutoFpsDoorTweenInjected" not in door.source
        assert "script.Parent" not in door.source
        assert door.source.rstrip().endswith("return Door")

    def test_legacy_mode_still_runs_coherence_packs_on_door(
        self, tmp_path: Path,
    ) -> None:
        """Legacy mode is unchanged: the door-tween pack still fires and
        injects its block (proving the gate is mode-specific, not a global
        disable)."""
        pipeline = _make_pipeline(tmp_path)
        assert pipeline.ctx.scene_runtime_mode == "legacy"
        door = _make_generic_door_script()
        pipeline.state.rbx_place.scripts = [door]

        pipeline._subphase_cohere_scripts()

        assert "_AutoFpsDoorTweenInjected" in door.source
