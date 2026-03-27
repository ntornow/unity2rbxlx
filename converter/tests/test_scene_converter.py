"""Tests for scene_converter.py key functions."""

import pytest
from pathlib import Path


class TestWaterDetection:
    """Test water node detection logic."""

    def test_is_water_by_shader_name(self):
        """Water shader materials should be detected."""
        from converter.scene_converter import _is_water_node

        class FakeComp:
            component_type = "MeshRenderer"
            properties = {"m_Materials": [{"guid": "fake-water-guid"}]}

        class FakeNode:
            name = "WaterPlane"
            components = [FakeComp()]
            children = []
            position = (0, 0, 0)
            scale = (1, 1, 1)

        # Without material mappings, fall back to name check
        result = _is_water_node(FakeNode(), {}, None)
        assert result is True  # "Water" in name

    def test_non_water_node(self):
        """Regular nodes should not be detected as water."""
        from converter.scene_converter import _is_water_node

        class FakeNode:
            name = "Turret"
            components = []
            children = []
            position = (0, 0, 0)
            scale = (1, 1, 1)

        result = _is_water_node(FakeNode(), {}, None)
        assert result is False


class TestHierarchyParenting:
    """Test that prefab hierarchy parenting works."""

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS").exists(),
        reason="SimpleFPS test project not available",
    )
    def test_dynamic_objects_has_children(self):
        """DynamicObjects/Level should have child sectors."""
        from converter.pipeline import Pipeline

        project = Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=Path("/tmp/test_hierarchy"),
            skip_upload=True,
        )
        pipeline.run_all()

        # Find DynamicObjects
        dyn = None
        for part in pipeline.state.rbx_place.workspace_parts:
            if part.name == "DynamicObjects":
                dyn = part
                break

        assert dyn is not None, "DynamicObjects should exist"
        assert len(dyn.children) > 0, "DynamicObjects should have children"

        # Find Level under DynamicObjects
        level = None
        for child in dyn.children:
            if child.name == "Level":
                level = child
                break

        assert level is not None, "Level should exist under DynamicObjects"
        assert len(level.children) >= 4, "Level should have at least 4 sector children"

        import shutil
        shutil.rmtree("/tmp/test_hierarchy", ignore_errors=True)
