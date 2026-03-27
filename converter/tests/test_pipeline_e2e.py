"""
test_pipeline_e2e.py -- End-to-end pipeline tests.

These tests require the test_projects to be available and
run the full conversion pipeline (minus upload).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _has_unitypy():
    try:
        import UnityPy  # noqa: F401
        return True
    except ImportError:
        return False


class TestPipelineE2E:
    """End-to-end tests against real test projects."""

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS").exists(),
        reason="SimpleFPS test project not available",
    )
    def test_simplefps_parse(self, tmp_path):
        """Test parsing the SimpleFPS project."""
        from unity.scene_parser import parse_scene
        from unity.prefab_parser import parse_prefabs
        from unity.guid_resolver import build_guid_index

        project = Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS"

        # Build GUID index
        guid_index = build_guid_index(project)
        assert guid_index.total_resolved > 0

        # Parse scene
        scene = parse_scene(project / "Assets" / "Scenes" / "main.unity")
        assert len(scene.all_nodes) > 0
        assert len(scene.roots) > 0

        # Parse prefabs
        library = parse_prefabs(project)
        assert len(library.prefabs) > 0

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS").exists(),
        reason="SimpleFPS test project not available",
    )
    def test_simplefps_full_convert(self, tmp_path):
        """Test full conversion of SimpleFPS (no upload)."""
        from converter.pipeline import Pipeline

        project = Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.run_all()

        # Should have generated output
        assert pipeline.context.total_game_objects > 0
        assert pipeline.context.converted_parts > 200  # SimpleFPS has 295+ parts
        assert pipeline.context.transpiled_scripts >= 30  # 36 scripts
        assert pipeline.context.converted_materials >= 30  # 35/36 materials

        # Check for .rbxlx file
        rbxlx_files = list(tmp_path.glob("*.rbxlx"))
        assert len(rbxlx_files) > 0

        # Validate the generated rbxlx
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(rbxlx_files[0]))
        assert tree.getroot().tag == "roblox"

        # Count element types
        classes = {}
        for item in tree.iter("Item"):
            cls = item.get("class", "")
            classes[cls] = classes.get(cls, 0) + 1

        assert classes.get("MeshPart", 0) > 200  # FBX-as-prefab adds ~38
        assert classes.get("Script", 0) + classes.get("LocalScript", 0) + classes.get("ModuleScript", 0) >= 40
        assert classes.get("Sound", 0) > 50
        assert classes.get("PointLight", 0) + classes.get("SpotLight", 0) > 20

        # Verify terrain ground collider exists if terrain was detected
        has_ground = any(
            item.get("class") == "Part" and
            item.find("Properties") is not None and
            any(s.text == "GroundCollider" for s in item.find("Properties").iter("string")
                if s.get("name") == "Name")
            for item in tree.iter("Item")
        )
        # GroundCollider provides collision while terrain generates
        assert has_ground, "GroundCollider should exist for terrain collision"

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "3D-Platformer").exists(),
        reason="3D-Platformer test project not available",
    )
    @pytest.mark.skipif(
        not _has_unitypy(),
        reason="UnityPy not installed (required for binary scenes)",
    )
    def test_3d_platformer_binary_scene(self, tmp_path):
        """Test binary scene conversion of 3D-Platformer."""
        from converter.pipeline import Pipeline

        project = Path(__file__).parent.parent.parent / "test_projects" / "3D-Platformer"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.run_all()

        assert pipeline.context.converted_parts > 0
        rbxlx_files = list(tmp_path.glob("*.rbxlx"))
        assert len(rbxlx_files) > 0

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "RedRunner").exists(),
        reason="RedRunner test project not available",
    )
    @pytest.mark.skipif(
        not _has_unitypy(),
        reason="UnityPy not installed (required for binary scenes)",
    )
    def test_redrunner_binary_scene(self, tmp_path):
        """Test binary scene conversion of RedRunner."""
        from converter.pipeline import Pipeline

        project = Path(__file__).parent.parent.parent / "test_projects" / "RedRunner"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.run_all()

        assert pipeline.context.converted_parts > 0
        assert pipeline.context.transpiled_scripts > 0

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "Gamekit3D").exists(),
        reason="Gamekit3D test project not available",
    )
    def test_gamekit3d_large_conversion(self, tmp_path):
        """Test large project conversion (Gamekit3D: 18k+ parts)."""
        from converter.pipeline import Pipeline

        project = Path(__file__).parent.parent.parent / "test_projects" / "Gamekit3D"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.run_all()

        assert pipeline.context.converted_parts > 10000
        assert pipeline.context.transpiled_scripts > 200
        assert pipeline.context.converted_materials > 100

        # Validate the output
        rbxlx = list(tmp_path.glob("*.rbxlx"))
        assert len(rbxlx) > 0
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(rbxlx[0]))
        assert tree.getroot().tag == "roblox"

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS").exists(),
        reason="SimpleFPS test project not available",
    )
    def test_simplefps_no_orphans(self, tmp_path):
        """Verify all prefab instances are parented (zero orphans)."""
        from converter.pipeline import Pipeline

        project = Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.context.selected_scene = "Assets/Scenes/main.unity"
        pipeline.run_all()

        # All 289 prefab parts should be parented (inactive containers catch skipped nodes)
        import xml.etree.ElementTree as ET
        tree = ET.parse(str(list(tmp_path.glob("*.rbxlx"))[0]))

        # Count Models at workspace root — inactive containers should be nested, not flat
        workspace = tree.find(".//Item[@class='Workspace']")
        root_items = workspace.findall("Item") if workspace is not None else []
        # Should have few top-level items (not 75+ orphaned flat parts)
        assert len(root_items) < 20, f"Too many workspace root items: {len(root_items)}"

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS").exists(),
        reason="SimpleFPS test project not available",
    )
    def test_simplefps_multi_scene(self, tmp_path):
        """Test multi-scene conversion produces separate .rbxlx files."""
        from converter.pipeline import Pipeline

        project = Path(__file__).parent.parent.parent / "test_projects" / "SimpleFPS"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.run_all_scenes()

        # Should produce two scene files
        rbxlx_files = sorted(tmp_path.glob("*.rbxlx"))
        assert len(rbxlx_files) == 2
        names = {f.stem for f in rbxlx_files}
        assert "main" in names
        assert "menu" in names

        # Both should be valid XML
        import xml.etree.ElementTree as ET
        for f in rbxlx_files:
            tree = ET.parse(str(f))
            assert tree.getroot().tag == "roblox"

        # scenes_metadata should be populated
        assert len(pipeline.context.scenes_metadata) == 2

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "ChopChop").exists(),
        reason="ChopChop test project not available",
    )
    def test_chopchop_nested_project(self, tmp_path):
        """Test auto-detection of nested Unity project root (UOP1_Project/)."""
        from converter.pipeline import Pipeline

        project = Path(__file__).parent.parent.parent / "test_projects" / "ChopChop"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.run_all()

        # Should detect UOP1_Project and produce output
        assert pipeline.context.converted_parts > 0
        assert pipeline.context.transpiled_scripts > 0

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "BossRoom").exists(),
        reason="BossRoom test project not available",
    )
    def test_bossroom_converts(self, tmp_path):
        """Test BossRoom networking project converts without errors."""
        from converter.pipeline import Pipeline
        import xml.etree.ElementTree as ET

        project = Path(__file__).parent.parent.parent / "test_projects" / "BossRoom"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.run_all()

        assert pipeline.context.transpiled_scripts > 100
        # Validate the XML is well-formed
        rbxlx = tmp_path / "converted_place.rbxlx"
        assert rbxlx.exists()
        tree = ET.parse(rbxlx)
        assert tree.getroot().tag == "roblox"

    @pytest.mark.skipif(
        not (Path(__file__).parent.parent.parent / "test_projects" / "BoatAttack").exists(),
        reason="BoatAttack test project not available",
    )
    def test_boatattack_converts(self, tmp_path):
        """Test BoatAttack URP project converts without errors."""
        from converter.pipeline import Pipeline
        import xml.etree.ElementTree as ET

        project = Path(__file__).parent.parent.parent / "test_projects" / "BoatAttack"
        pipeline = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path,
            skip_upload=True,
        )
        pipeline.run_all()

        assert pipeline.context.transpiled_scripts > 30
        rbxlx = tmp_path / "converted_place.rbxlx"
        assert rbxlx.exists()
        tree = ET.parse(rbxlx)
        assert tree.getroot().tag == "roblox"
