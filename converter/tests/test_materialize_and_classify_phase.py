"""Phase 2a slice 8 tests: ``materialize_and_classify`` phase ordering.

Covers four contracts the slice-8 lift commits:

1. ``materialize_and_classify`` runs the three lifted subphases in
   ``MATERIALIZE_AND_CLASSIFY_ORDER`` exactly (emit → cohere → classify),
   no extras and no drops.
2. The phase sits between ``convert_scene`` and ``write_output`` in
   ``PHASES`` — placement is load-bearing because ``rbx_place`` is only
   created by ``convert_scene`` and ``write_output`` needs the populated
   script list.
3. After ``materialize_and_classify`` runs, ``rbx_place.scripts`` is
   populated AND every script carries the storage-plan ``parent_path``
   the classifier assigned. ``write_output`` consumes a fully-stamped
   list; it does not re-run emit or cohere.
4. The Option (b) safety-net ``_classify_late_appended_scripts`` stamps
   the rbxlx_writer default container on any script whose generator
   left ``parent_path = None`` — autogen / runtime / scene-runtime
   scripts get an explicit parent_path that matches the implicit-
   default routing the writer would have applied today. ZERO drift on
   parent_path for the well-known autogen scripts.

The slice 7 "tests passing with pre-stamped fixtures masked a
producer-after-consumer bug" lesson applies: tests here construct
``rbx_place.scripts`` by running the real producer
(``_subphase_emit_scripts_to_disk`` via ``materialize_and_classify``),
not by pre-stamping.
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import TranspilationResult, TranspiledScript  # noqa: E402
from converter.pipeline import PHASES, Pipeline  # noqa: E402
from core.roblox_types import RbxPlace, RbxScript  # noqa: E402


PIPELINE_PATH = Path(__file__).parent.parent / "converter" / "pipeline.py"


def _make_pipeline(tmp_path: Path) -> Pipeline:
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    return Pipeline(str(unity_project), str(output))


def _parse_materialize_and_classify_call_sequence() -> list[str]:
    """Extract ordered ``self.<method>()`` calls from
    ``materialize_and_classify``'s AST."""
    tree = ast.parse(PIPELINE_PATH.read_text())
    calls: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.FunctionDef)
            and node.name == "materialize_and_classify"
        ):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if not isinstance(func, ast.Attribute):
                continue
            if not isinstance(func.value, ast.Name) or func.value.id != "self":
                continue
            name = func.attr
            # Match the lifted subphases (all underscored) but drop
            # logging-only calls (no log calls in this method today).
            if name.startswith("_subphase_") or name == "_classify_storage":
                # ``_runtime_bearing_module_names`` is a helper invoked from
                # within ``_subphase_prune_dead_module_closures``, never from
                # the phase orchestrator -- it won't appear at this level.
                calls.append(name)
        break
    return calls


class TestPhaseOrdering:
    """``materialize_and_classify`` sits between ``convert_scene`` and
    ``write_output``. Placement is load-bearing — see the phase method
    docstring for the dependency rationale."""

    def test_phase_is_in_phases_list(self) -> None:
        assert "materialize_and_classify" in PHASES

    def test_phase_is_between_convert_scene_and_write_output(self) -> None:
        idx_convert = PHASES.index("convert_scene")
        idx_mat = PHASES.index("materialize_and_classify")
        idx_write = PHASES.index("write_output")
        assert idx_convert < idx_mat < idx_write, (
            "materialize_and_classify must sit strictly between "
            "convert_scene (producer of rbx_place) and write_output "
            "(consumer of the populated script list). Phases: %s"
            % PHASES
        )

    def test_phase_is_an_essential_phase(self) -> None:
        """Resume contract: ``materialize_and_classify`` populates
        ``rbx_place.scripts`` in-memory, so a ``--phase=write_output``
        resume must re-run it (otherwise write_output sees an empty
        script list)."""
        assert "materialize_and_classify" in Pipeline.ESSENTIAL_PHASES


class TestSubphaseOrderInvariant:
    """The constant MATERIALIZE_AND_CLASSIFY_ORDER documents the lift
    decision; the method body must call those subphases in exactly that
    order. Drift would silently break ordering-sensitive behaviors
    (cohere must run before classify; emit before cohere)."""

    def test_constant_is_defined(self) -> None:
        assert hasattr(Pipeline, "MATERIALIZE_AND_CLASSIFY_ORDER")
        assert isinstance(Pipeline.MATERIALIZE_AND_CLASSIFY_ORDER, tuple)
        assert len(Pipeline.MATERIALIZE_AND_CLASSIFY_ORDER) == 5, (
            "Slice 8 lifted emit, cohere, classify; TODO #8 inserted the "
            "dead-module analysis + prune passes between cohere and classify."
        )

    def test_constant_lists_the_lifted_subphases(self) -> None:
        assert Pipeline.MATERIALIZE_AND_CLASSIFY_ORDER == (
            "_subphase_emit_scripts_to_disk",
            "_subphase_cohere_scripts",
            "_subphase_analyze_dead_modules",
            "_subphase_prune_dead_module_closures",
            "_classify_storage",
        )

    def test_method_body_matches_constant(self) -> None:
        actual = _parse_materialize_and_classify_call_sequence()
        expected = list(Pipeline.MATERIALIZE_AND_CLASSIFY_ORDER)
        assert actual == expected, (
            "materialize_and_classify() call sequence drifted from "
            "MATERIALIZE_AND_CLASSIFY_ORDER.\n"
            f"  declared: {expected}\n"
            f"  actual:   {actual}"
        )

    def test_lifted_subphases_no_longer_in_subphase_order(self) -> None:
        """SUBPHASE_ORDER is the canonical list of write_output's
        subphases; after the lift it must NOT mention the three lifted
        methods (their owner is materialize_and_classify now)."""
        lifted = set(Pipeline.MATERIALIZE_AND_CLASSIFY_ORDER)
        carried = set(Pipeline.SUBPHASE_ORDER)
        assert lifted.isdisjoint(carried), (
            "Lifted subphases must not still be in SUBPHASE_ORDER. "
            f"Overlap: {lifted & carried}"
        )

    def test_every_listed_subphase_is_a_method(self) -> None:
        for name in Pipeline.MATERIALIZE_AND_CLASSIFY_ORDER:
            method = getattr(Pipeline, name, None)
            assert method is not None, (
                f"MATERIALIZE_AND_CLASSIFY_ORDER lists {name!r} but "
                f"Pipeline has no such method"
            )
            assert callable(method)
            sig = inspect.signature(method)
            assert "self" in sig.parameters


class TestEndToEndWiring:
    """Construct a real Pipeline + real transpilation result, run
    materialize_and_classify, then assert write_output's preconditions
    are met. This is the slice-7-lesson-aware end-to-end witness — no
    pre-stamped fixtures; the producer populates the consumer's input."""

    def test_emit_populates_rbx_place_scripts_from_transpilation_result(
        self, tmp_path: Path,
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.transpilation_result = TranspilationResult(
            scripts=[
                TranspiledScript(
                    source_path="Assets/UserScript.cs",
                    output_filename="UserScript.luau",
                    csharp_source="// stub",
                    luau_source="-- UserScript\nprint('hi')\n",
                    strategy="rule_based",
                    confidence=1.0,
                    script_type="Script",
                ),
                TranspiledScript(
                    source_path="Assets/UserModule.cs",
                    output_filename="UserModule.luau",
                    csharp_source="// stub",
                    luau_source="-- UserModule\nlocal M = {}\nreturn M\n",
                    strategy="rule_based",
                    confidence=1.0,
                    script_type="ModuleScript",
                ),
            ],
            total_transpiled=2,
            total_rule_based=2,
        )

        pipeline.materialize_and_classify()

        names = {s.name for s in pipeline.state.rbx_place.scripts}
        assert {"UserScript", "UserModule"}.issubset(names), (
            "Emit subphase must materialize transpilation result into "
            "rbx_place.scripts before write_output runs."
        )

    def test_classify_stamps_parent_path_on_every_user_script(
        self, tmp_path: Path,
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.transpilation_result = TranspilationResult(
            scripts=[
                TranspiledScript(
                    source_path="Assets/AServer.cs",
                    output_filename="AServer.luau",
                    csharp_source="// stub",
                    luau_source="-- server\nprint('hi')\n",
                    strategy="rule_based",
                    confidence=1.0,
                    script_type="Script",
                ),
            ],
            total_transpiled=1,
            total_rule_based=1,
        )

        pipeline.materialize_and_classify()

        for s in pipeline.state.rbx_place.scripts:
            assert s.parent_path, (
                f"Script {s.name!r} must carry an explicit parent_path "
                f"after materialize_and_classify, got: {s.parent_path!r}"
            )

    def test_storage_plan_persisted_to_disk(self, tmp_path: Path) -> None:
        """After materialize_and_classify the on-disk
        ``conversion_plan.json`` has the storage_plan block — this is
        what ``write_output`` consumes via persistence (the contract
        Codex flagged for ``--phase=write_output`` resumes)."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.transpilation_result = TranspilationResult(
            scripts=[
                TranspiledScript(
                    source_path="Assets/X.cs",
                    output_filename="X.luau",
                    csharp_source="// stub",
                    luau_source="-- X\n",
                    strategy="rule_based",
                    confidence=1.0,
                    script_type="Script",
                ),
            ],
            total_transpiled=1,
            total_rule_based=1,
        )

        pipeline.materialize_and_classify()

        plan_path = pipeline.output_dir / "conversion_plan.json"
        assert plan_path.is_file(), (
            "materialize_and_classify must persist conversion_plan.json "
            "for write_output (and --phase resumes) to consume."
        )

    def test_phase_is_a_noop_when_rbx_place_is_none(
        self, tmp_path: Path,
    ) -> None:
        """Defensive: convert_scene may no-op (e.g. missing parsed scene
        for a probe run). The new phase must not crash; it should log
        and return without touching state."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = None
        # Should not raise.
        pipeline.materialize_and_classify()
        assert pipeline.state.rbx_place is None


class TestLateAppendSafetyNet:
    """Option (b) ``_classify_late_appended_scripts`` stamps the
    rbxlx_writer default ``parent_path`` on any script whose generator
    left it as ``None``. The acceptance gate: ZERO drift on autogen /
    runtime / scene-runtime scripts after the lift."""

    def test_late_append_stamps_explicit_parent_path_on_unrouted_script(
        self, tmp_path: Path,
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace()
        # Simulate the post-injection state: a Script with no parent_path,
        # the way GameServerManager / CollisionGroupSetup land today.
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="GameServerManager",
                source="-- stub",
                script_type="Script",
            ),
        )
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="NavAgent",
                source="-- stub",
                script_type="ModuleScript",
            ),
        )
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="CinemachineRuntime",
                source="-- stub",
                script_type="LocalScript",
            ),
        )

        pipeline._classify_late_appended_scripts()

        by_name = {s.name: s for s in pipeline.state.rbx_place.scripts}
        # Mirrors rbxlx_writer fallback:
        #   Script → ServerScriptService
        #   ModuleScript → ReplicatedStorage
        #   LocalScript → StarterPlayer.StarterPlayerScripts
        assert by_name["GameServerManager"].parent_path == "ServerScriptService"
        assert by_name["NavAgent"].parent_path == "ReplicatedStorage"
        assert by_name["CinemachineRuntime"].parent_path == (
            "StarterPlayer.StarterPlayerScripts"
        )

    def test_late_append_pass_is_idempotent(self, tmp_path: Path) -> None:
        """Running twice must not change anything on the second call —
        already-stamped parent_path is preserved."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="GameServerManager",
                source="-- stub",
                script_type="Script",
            ),
        )

        pipeline._classify_late_appended_scripts()
        first = pipeline.state.rbx_place.scripts[0].parent_path
        pipeline._classify_late_appended_scripts()
        second = pipeline.state.rbx_place.scripts[0].parent_path

        assert first == second == "ServerScriptService"

    def test_late_append_respects_explicit_parent_path(
        self, tmp_path: Path,
    ) -> None:
        """Scripts with an already-set parent_path (e.g. SceneRuntime*
        entrypoints, SceneRuntimePlan, classified user scripts) pass
        through untouched."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="SceneRuntimeClient",
                source="-- stub",
                script_type="LocalScript",
                parent_path="StarterPlayer.StarterPlayerScripts",
            ),
        )
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="ClassifiedUserScript",
                source="-- stub",
                script_type="Script",
                parent_path="ServerStorage",  # not the Script default
            ),
        )

        pipeline._classify_late_appended_scripts()

        by_name = {s.name: s for s in pipeline.state.rbx_place.scripts}
        assert by_name["SceneRuntimeClient"].parent_path == (
            "StarterPlayer.StarterPlayerScripts"
        )
        # Verify the safety net does NOT overwrite an explicit non-
        # default placement (ServerStorage for a Script).
        assert by_name["ClassifiedUserScript"].parent_path == "ServerStorage"

    def test_late_append_pass_runs_in_write_output_subphase_order(
        self,
    ) -> None:
        """``_classify_late_appended_scripts`` must appear in
        SUBPHASE_ORDER between the three injection subphases (autogen /
        runtime / scene_runtime) and the rest of write_output. This is
        the acceptance-gate guard from the design doc."""
        order = list(Pipeline.SUBPHASE_ORDER)
        assert "_classify_late_appended_scripts" in order
        # Position must be AFTER every injection subphase.
        idx_safety = order.index("_classify_late_appended_scripts")
        for injector in (
            "_subphase_inject_autogen_scripts",
            "_inject_runtime_modules",
            "_subphase_inject_scene_runtime",
        ):
            assert order.index(injector) < idx_safety, (
                f"Safety-net pass must run AFTER {injector} so it sees "
                f"the late-appended scripts."
            )
