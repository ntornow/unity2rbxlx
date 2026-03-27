"""
test_terrain.py -- Tests for Unity Terrain detection and conversion.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unity.scene_parser import parse_scene
from converter.scene_converter import convert_scene
from converter.component_converter import convert_terrain
from core.roblox_types import RbxTerrain

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestConvertTerrain:
    """Unit tests for the convert_terrain function."""

    def test_basic_terrain(self):
        props = {
            "m_Enabled": 1,
            "m_TerrainData": {
                "fileID": 15600000,
                "guid": "abc123",
                "type": 2,
            },
        }
        result = convert_terrain(props, (10.0, 0.0, 20.0))
        assert result is not None
        assert isinstance(result, RbxTerrain)
        assert result.position == (10.0, 0.0, 20.0)
        assert result.terrain_data_guid == "abc123"
        # Default size should be 1000x600x1000
        assert result.size == (1000.0, 600.0, 1000.0)

    def test_disabled_terrain_returns_none(self):
        props = {
            "m_Enabled": 0,
            "m_TerrainData": {"guid": "abc123"},
        }
        result = convert_terrain(props)
        assert result is None

    def test_terrain_missing_guid(self):
        props = {
            "m_Enabled": 1,
            "m_TerrainData": {},
        }
        result = convert_terrain(props)
        assert result is not None
        assert result.terrain_data_guid == ""

    def test_terrain_default_position(self):
        props = {"m_Enabled": 1, "m_TerrainData": {"guid": "xyz"}}
        result = convert_terrain(props)
        assert result is not None
        assert result.position == (0.0, 0.0, 0.0)


class TestTerrainParsing:
    """Tests that terrain components are parsed from scene YAML."""

    def test_terrain_scene_has_terrain_component(self):
        scene = parse_scene(FIXTURES_DIR / "terrain_scene.yaml")
        # Find the Terrain node
        terrain_node = None
        for node in scene.all_nodes.values():
            if node.name == "Terrain":
                terrain_node = node
                break
        assert terrain_node is not None
        comp_types = {c.component_type for c in terrain_node.components}
        assert "Terrain" in comp_types
        assert "TerrainCollider" in comp_types

    def test_terrain_component_properties(self):
        scene = parse_scene(FIXTURES_DIR / "terrain_scene.yaml")
        terrain_node = None
        for node in scene.all_nodes.values():
            if node.name == "Terrain":
                terrain_node = node
                break
        assert terrain_node is not None
        # Find the Terrain component
        terrain_comp = None
        for comp in terrain_node.components:
            if comp.component_type == "Terrain":
                terrain_comp = comp
                break
        assert terrain_comp is not None
        assert terrain_comp.properties["m_Enabled"] == 1
        td = terrain_comp.properties["m_TerrainData"]
        assert td["guid"] == "efeec9bb5989ceb4d9a65574d8454d3a"


class TestTerrainConversion:
    """Integration tests for terrain in scene conversion."""

    def test_scene_with_terrain_has_terrain_part(self):
        scene = parse_scene(FIXTURES_DIR / "terrain_scene.yaml")
        place = convert_scene(scene)
        # Should have at least one terrain recorded
        assert len(place.terrains) == 1
        assert place.terrains[0].terrain_data_guid == "efeec9bb5989ceb4d9a65574d8454d3a"

    def test_no_flat_terrain_part(self):
        """Terrain should NOT create a flat Part — real terrain is generated at runtime."""
        scene = parse_scene(FIXTURES_DIR / "terrain_scene.yaml")
        place = convert_scene(scene)
        terrain_parts = [p for p in place.workspace_parts if p.name == "Terrain" and p.class_name == "Part"]
        assert len(terrain_parts) == 0, "Flat terrain Part should not be created"

    def test_terrain_suppresses_auto_floor(self):
        scene = parse_scene(FIXTURES_DIR / "terrain_scene.yaml")
        place = convert_scene(scene)
        floor_parts = [p for p in place.workspace_parts if p.name == "ConvertedFloor"]
        assert len(floor_parts) == 0, "Auto-generated floor should be suppressed when terrain is present"

    def test_spawn_still_generated_with_terrain(self):
        scene = parse_scene(FIXTURES_DIR / "terrain_scene.yaml")
        place = convert_scene(scene)
        spawn_parts = [p for p in place.workspace_parts if p.name == "SpawnLocation"]
        assert len(spawn_parts) == 1

    def test_scene_without_terrain_still_has_floor(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        place = convert_scene(scene)
        floor_parts = [p for p in place.workspace_parts if p.name == "ConvertedFloor"]
        assert len(floor_parts) == 1, "Auto-generated floor should still appear when no terrain"

    def test_terrain_metadata_stored(self):
        """Terrain metadata (position, size, GUID) should be stored for runtime generation."""
        scene = parse_scene(FIXTURES_DIR / "terrain_scene.yaml")
        place = convert_scene(scene)
        assert len(place.terrains) == 1
        t = place.terrains[0]
        assert t.terrain_data_guid == "efeec9bb5989ceb4d9a65574d8454d3a"
        assert t.size[0] > 0
        assert t.size[2] > 0


class TestTerrainSmoothGridEncoding:
    """Tests for terrain SmoothGrid binary encoding in rbxlx output."""

    def test_smooth_grid_encoding(self):
        """Test that SmoothGrid encoder produces valid base64 data."""
        from roblox.terrain_encoder import encode_smooth_grid, encode_physics_grid
        import base64

        # Simple 3x3 heightmap: flat terrain at half height
        heights = [0.5] * 9
        sg = encode_smooth_grid(heights, 3, (1.0, 100.0, 1.0), (0.0, 0.0, 0.0))
        assert sg  # Non-empty
        # Should be valid base64
        data = base64.b64decode(sg)
        assert data[0] == 1  # version
        assert data[1] == 5  # chunk size 2^5 = 32

    def test_physics_grid_default(self):
        from roblox.terrain_encoder import encode_physics_grid
        import base64

        pg = encode_physics_grid()
        data = base64.b64decode(pg)
        assert data[0] == 2  # version 2

    def test_terrain_in_rbxlx(self):
        """Test that terrain produces an empty Terrain item in rbxlx (data is runtime-generated)."""
        from core.roblox_types import RbxPlace, RbxTerrain
        from roblox.rbxlx_writer import write_rbxlx
        import tempfile
        import xml.etree.ElementTree as ET

        terrain = RbxTerrain(
            position=(0.0, 0.0, 0.0),
            size=(100.0, 50.0, 100.0),
            terrain_data_guid="test",
        )
        place = RbxPlace(terrains=[terrain])

        with tempfile.NamedTemporaryFile(suffix=".rbxlx", delete=False) as f:
            write_rbxlx(place, f.name)
            tree = ET.parse(f.name)

        # Should have an empty Terrain item in Workspace (filled at runtime)
        terrain_items = [
            item for item in tree.iter("Item") if item.get("class") == "Terrain"
        ]
        assert len(terrain_items) == 1

    def test_no_terrain_without_terrains(self):
        """Place without terrains should NOT produce a Terrain item."""
        from core.roblox_types import RbxPlace
        from roblox.rbxlx_writer import write_rbxlx
        import tempfile
        import xml.etree.ElementTree as ET

        place = RbxPlace()

        with tempfile.NamedTemporaryFile(suffix=".rbxlx", delete=False) as f:
            write_rbxlx(place, f.name)
            tree = ET.parse(f.name)

        terrain_items = [
            item for item in tree.iter("Item") if item.get("class") == "Terrain"
        ]
        assert len(terrain_items) == 0


class TestWaterRegion:
    """Test water region detection and sizing."""

    def test_water_region_scale_composition(self):
        """Water regions should compose prefab root scale with instance scale."""
        from converter.scene_converter import _extract_water_region_from_prefab
        import config

        # Simulate composed scale of (450, 1, 450) - typical water prefab
        composed_scl = (450.0, 1.0, 450.0)
        region = _extract_water_region_from_prefab(
            pos=(300.0, 1.0, 300.0),
            scl=composed_scl,
            name="WaterTest",
        )

        # Size should be 450 * 10 (plane base) * STUDS_PER_METER
        expected_width = 450.0 * 10.0 * config.STUDS_PER_METER
        assert abs(region.size[0] - expected_width) < 1.0
        assert region.size[1] == 2.0  # thin water surface


class TestTerrainRealScene:
    """Tests against real test projects (skipped if not available)."""

    def test_simplefps_terrain_detected(self, simplefps_project):
        scene_path = simplefps_project / "Assets" / "Scenes" / "main.unity"
        if not scene_path.exists():
            import pytest
            pytest.skip("SimpleFPS not available")

        scene = parse_scene(scene_path)
        place = convert_scene(scene)
        assert len(place.terrains) >= 1, "SimpleFPS main scene should have at least one terrain"
        # Should find the terrain with the known GUID
        guids = {t.terrain_data_guid for t in place.terrains}
        assert "efeec9bb5989ceb4d9a65574d8454d3a" in guids
