"""PR1 persistence test: ``_classify_storage`` preserves the
``scene_runtime`` block in ``conversion_plan.json`` across runs.

Specifically:
  - Planner-emitted ``modules`` / ``scenes`` / ``prefabs`` round-trip.
  - Operator-set ``domain_overrides`` is sticky — a second run with a
    different ``ctx.scene_runtime`` keeps the on-disk override.
  - Resume (loading the JSON back into ctx) reproduces both blocks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.pipeline import Pipeline
from converter.storage_classifier import StoragePlan
from core.conversion_context import ConversionContext
from core.roblox_types import RbxPlace, RbxScript


def _make_pipeline(tmp_path: Path) -> Pipeline:
    """Build a Pipeline wired against ``tmp_path`` with the minimum
    state ``_classify_storage`` reads — a single RbxScript and an
    output directory."""
    unity_project = tmp_path / "unity"
    unity_project.mkdir()
    (unity_project / "Assets").mkdir()
    output = tmp_path / "out"
    output.mkdir()

    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts.append(
        RbxScript(name="HelloScript", source="return 1", script_type="Script")
    )
    return pipeline


def _seed_scene_runtime(pipeline: Pipeline) -> dict[str, object]:
    artifact: dict[str, object] = {
        "modules": {
            "guid-a": {
                "stem": "Foo", "class_name": "Foo", "runtime_bearing": True,
            },
        },
        "scenes": {
            "Assets/Scenes/Main.unity": {
                "instances": [], "references": [], "lifecycle_order": [],
            },
        },
        "prefabs": {},
        "domain_overrides": {},
    }
    pipeline.ctx.scene_runtime = artifact
    return artifact


class TestSceneRuntimePersistence:
    def test_first_run_writes_scene_runtime_block(self, tmp_path: Path):
        pipeline = _make_pipeline(tmp_path)
        seeded = _seed_scene_runtime(pipeline)

        pipeline._classify_storage()

        plan_path = pipeline.output_dir / "conversion_plan.json"
        raw = json.loads(plan_path.read_text())
        assert "scene_runtime" in raw
        # All structural sub-blocks survive verbatim.
        assert raw["scene_runtime"]["modules"] == seeded["modules"]
        assert raw["scene_runtime"]["scenes"] == seeded["scenes"]
        assert raw["scene_runtime"]["prefabs"] == seeded["prefabs"]
        assert raw["scene_runtime"]["domain_overrides"] == {}

    def test_operator_domain_overrides_survive_second_classify(
        self, tmp_path: Path,
    ):
        pipeline = _make_pipeline(tmp_path)
        _seed_scene_runtime(pipeline)

        # First write — sets up the file.
        pipeline._classify_storage()

        # Operator edits the plan: stamps an override and saves it back.
        plan_path = pipeline.output_dir / "conversion_plan.json"
        raw = json.loads(plan_path.read_text())
        raw["scene_runtime"]["domain_overrides"]["guid-a"] = "client"
        plan_path.write_text(json.dumps(raw, indent=2))

        # Second run — ctx.scene_runtime still carries the planner's
        # ``{}`` domain_overrides, but the on-disk operator value must
        # win the merge.
        pipeline._classify_storage()

        reread = json.loads(plan_path.read_text())
        assert reread["scene_runtime"]["domain_overrides"] == {"guid-a": "client"}

    def test_second_run_with_different_modules_refreshes_structural_blocks(
        self, tmp_path: Path,
    ):
        # Structural sub-blocks (modules / scenes / prefabs) should come
        # from ctx.scene_runtime each run — not from the on-disk plan.
        # Operator overrides are sticky; everything else refreshes.
        pipeline = _make_pipeline(tmp_path)
        _seed_scene_runtime(pipeline)
        pipeline._classify_storage()

        # Second-run planner output drops one module and adds another.
        pipeline.ctx.scene_runtime = {
            "modules": {
                "guid-b": {
                    "stem": "Bar", "class_name": "Bar", "runtime_bearing": True,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline._classify_storage()

        raw = json.loads(
            (pipeline.output_dir / "conversion_plan.json").read_text()
        )
        # ``guid-a`` is gone — structural refresh, not merge. PR3b adds
        # ``domain`` / ``domain_signals`` etc. during _classify_storage,
        # so assert subset rather than exact equality.
        modules = raw["scene_runtime"]["modules"]
        assert set(modules.keys()) == {"guid-b"}
        assert modules["guid-b"]["stem"] == "Bar"
        assert modules["guid-b"]["class_name"] == "Bar"
        assert modules["guid-b"]["runtime_bearing"] is True

    def test_resume_loads_scene_runtime_block_back_into_ctx(
        self, tmp_path: Path,
    ):
        # Round-trip ConversionContext through save → load and verify the
        # scene_runtime field survives intact. PR1's resume contract.
        ctx = ConversionContext()
        ctx.scene_runtime = {
            "modules": {
                "g1": {"stem": "S", "class_name": "S", "runtime_bearing": True},
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {"g1": "server"},
        }
        ctx.storage_plan = StoragePlan()

        path = tmp_path / "conversion_context.json"
        ctx.save(path)
        reloaded = ConversionContext.load(path)

        assert reloaded.scene_runtime == ctx.scene_runtime
        # And the nested domain_overrides survived the round trip.
        assert reloaded.scene_runtime["domain_overrides"] == {"g1": "server"}
