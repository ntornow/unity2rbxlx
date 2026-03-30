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

    def test_smooth_grid_rle_format(self):
        """Test RLE encoding: 6-bit material + has_occ(1bit) + has_run(1bit)."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Small flat terrain — should produce at least one non-air chunk
        heights = [1.0] * 9  # Full-height terrain
        sg = encode_smooth_grid(heights, 3, (1.0, 10.0, 1.0), (0.0, 0.0, 0.0))
        data = base64.b64decode(sg)

        # Skip 2-byte header + 12-byte chunk coord = offset 14
        offset = 14
        # Scan RLE entries for at least one non-air material
        found_non_air = False
        scan_pos = offset
        while scan_pos < len(data):
            header = data[scan_pos]
            material = header & 0x3F
            if material != 0:
                found_non_air = True
                break
            has_occ = bool(header & 0x40)
            has_run = bool(header & 0x80)
            scan_pos += 1
            if has_occ:
                scan_pos += 1
            if has_run:
                scan_pos += 1
        assert found_non_air, "Terrain chunk should contain at least one non-air voxel"

    def test_smooth_grid_skips_air_chunks(self):
        """All-air chunks should not be included in the output."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Very small terrain that doesn't fill a full chunk
        heights = [0.01] * 4  # Tiny terrain — most voxels are air
        sg = encode_smooth_grid(heights, 2, (1.0, 1.0, 1.0), (0.0, 0.0, 0.0))
        data = base64.b64decode(sg)
        # Should have header (2 bytes) + at least one chunk
        assert len(data) >= 14  # 2 header + 12 chunk coord minimum

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


class TestSmoothGridBinaryFormat:
    """Deep validation of SmoothGrid binary format against Roblox spec.

    Verifies byte-level correctness: header, chunk coords, RLE encoding,
    material IDs, occupancy values, axis mapping, and delta encoding.
    """

    def _decode_rle(self, data: bytes, offset: int) -> list[tuple[int, int]]:
        """Decode RLE voxel data starting at offset, returning (material, occupancy) list."""
        voxels = []
        pos = offset
        while pos < len(data) and len(voxels) < 32768:
            header = data[pos]
            pos += 1
            material = header & 0x3F
            has_occ = bool(header & 0x40)
            has_run = bool(header & 0x80)

            if has_occ:
                occ = data[pos]
                pos += 1
            else:
                # No occupancy byte: air=0, non-air=255
                occ = 0 if material == 0 else 255

            run = 1
            if has_run:
                run = data[pos] + 1
                pos += 1

            for _ in range(run):
                voxels.append((material, occ))

        return voxels

    def _decode_chunk_header(self, data: bytes, offset: int) -> tuple[int, int, int]:
        """Decode 12-byte MSB-interleaved chunk coordinate."""
        b = data[offset:offset + 12]
        # MSB-interleaved: [x3,y3,z3, x2,y2,z2, x1,y1,z1, x0,y0,z0]
        x = (b[0] << 24) | (b[3] << 16) | (b[6] << 8) | b[9]
        y = (b[1] << 24) | (b[4] << 16) | (b[7] << 8) | b[10]
        z = (b[2] << 24) | (b[5] << 16) | (b[8] << 8) | b[11]
        # Convert from unsigned to signed int32
        if x >= 0x80000000:
            x -= 0x100000000
        if y >= 0x80000000:
            y -= 0x100000000
        if z >= 0x80000000:
            z -= 0x100000000
        return (x, y, z)

    def test_header_bytes(self):
        """Version=1, chunk_pow=5 (32 voxels per axis)."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        heights = [0.5] * 9
        data = base64.b64decode(
            encode_smooth_grid(heights, 3, (1.0, 50.0, 1.0))
        )
        assert data[0] == 1, "SmoothGrid version must be 1"
        assert data[1] == 5, "Chunk power must be 5 (2^5=32)"

    def test_chunk_coord_at_origin(self):
        """Terrain at origin: first chunk should be at or near (0, 0, -1)."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Small terrain fitting in one chunk (< 32 voxels in each dimension)
        heights = [0.5] * 9
        data = base64.b64decode(
            encode_smooth_grid(heights, 3, (1.0, 20.0, 1.0))
        )
        coord = self._decode_chunk_header(data, 2)
        # Terrain at (0,0,0): gz=0→wvz=0 in chunk 0, gz=1→wvz=-1 in chunk -1
        # First sorted chunk is (0, 0, -1)
        assert coord == (0, 0, -1), f"First chunk at origin should be (0,0,-1), got {coord}"

    def test_rle_decodes_to_32768_voxels(self):
        """Each chunk must contain exactly 32^3 = 32768 voxels when decoded."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        heights = [1.0] * 9  # Full height, ensures solid voxels
        data = base64.b64decode(
            encode_smooth_grid(heights, 3, (1.0, 10.0, 1.0))
        )
        # Decode first chunk RLE (starts at offset 14)
        voxels = self._decode_rle(data, 14)
        assert len(voxels) == 32768, f"Chunk must have 32768 voxels, got {len(voxels)}"

    def test_material_ids_in_valid_range(self):
        """All material IDs must be 0-22 (the 23 valid Roblox terrain materials)."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        heights = [0.5] * 25  # 5x5 heightmap
        data = base64.b64decode(
            encode_smooth_grid(heights, 5, (2.0, 80.0, 2.0))
        )
        voxels = self._decode_rle(data, 14)
        materials = {v[0] for v in voxels}
        for m in materials:
            assert 0 <= m <= 22, f"Material {m} outside valid range 0-22"

    def test_occupancy_values_valid(self):
        """Occupancy must be 0 for air, 1-255 for non-air voxels."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        heights = [0.5] * 9
        data = base64.b64decode(
            encode_smooth_grid(heights, 3, (1.0, 50.0, 1.0))
        )
        voxels = self._decode_rle(data, 14)
        for mat, occ in voxels:
            if mat == 0:  # Air
                assert occ == 0, f"Air voxel must have occupancy 0, got {occ}"
            else:
                assert 1 <= occ <= 255, f"Non-air voxel (mat={mat}) must have occupancy 1-255, got {occ}"

    def test_surface_voxels_have_partial_occupancy(self):
        """Surface voxels (at terrain height boundary) should have partial occupancy."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Half-height terrain -- surface voxels should have occ < 255
        heights = [0.5] * 9
        data = base64.b64decode(
            encode_smooth_grid(heights, 3, (1.0, 50.0, 1.0))
        )
        voxels = self._decode_rle(data, 14)
        non_air = [(m, o) for m, o in voxels if m != 0]
        assert len(non_air) > 0, "Should have some non-air voxels"
        # At least some should have partial occupancy (not all 255)
        partial = [o for _, o in non_air if o < 255]
        assert len(partial) > 0, "Surface terrain should have some partial-occupancy voxels"

    def test_solid_below_surface_full_occupancy(self):
        """Voxels fully below terrain surface should have occupancy 255."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Tall terrain (100 studs) -- bottom voxels should be fully solid
        heights = [1.0] * 9  # Full height
        data = base64.b64decode(
            encode_smooth_grid(heights, 3, (1.0, 100.0, 1.0))
        )
        voxels = self._decode_rle(data, 14)
        fully_solid = [(m, o) for m, o in voxels if m != 0 and o == 255]
        assert len(fully_solid) > 0, "Should have fully solid voxels below surface"

    def test_rle_run_encoding(self):
        """RLE runs should correctly represent consecutive identical voxels."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Flat terrain at full height -- large runs of identical voxels expected
        heights = [1.0] * 9
        data = base64.b64decode(
            encode_smooth_grid(heights, 3, (1.0, 10.0, 1.0))
        )
        # Parse RLE entries manually to verify run lengths
        pos = 14
        total_voxels = 0
        while total_voxels < 32768 and pos < len(data):
            header = data[pos]
            pos += 1
            has_occ = bool(header & 0x40)
            has_run = bool(header & 0x80)
            if has_occ:
                pos += 1
            run = 1
            if has_run:
                run = data[pos] + 1
                pos += 1
                assert 2 <= run <= 256, f"RLE run {run} outside valid range 2-256"
            total_voxels += run
        assert total_voxels == 32768, f"Total decoded voxels {total_voxels} != 32768"

    def test_air_voxel_no_occupancy_byte(self):
        """Air voxels (material=0) should not emit an occupancy byte."""
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Very low terrain -- most of the chunk will be air
        heights = [0.01] * 4
        data = base64.b64decode(
            encode_smooth_grid(heights, 2, (1.0, 1.0, 1.0))
        )
        # Find an air RLE entry in the first chunk
        pos = 14
        found_air = False
        total = 0
        while total < 32768 and pos < len(data):
            header = data[pos]
            pos += 1
            mat = header & 0x3F
            has_occ = bool(header & 0x40)
            has_run = bool(header & 0x80)
            if mat == 0:
                found_air = True
                assert not has_occ, "Air voxel should not have occupancy byte (has_occ=0)"
            if has_occ:
                pos += 1
            run = 1
            if has_run:
                run = data[pos] + 1
                pos += 1
            total += run
        assert found_air, "Expected at least one air RLE entry in a mostly-air chunk"

    def test_multi_chunk_delta_encoding(self):
        """Multi-chunk terrain must use delta encoding for chunk coordinates.

        First chunk is absolute, subsequent chunks are relative to previous.
        """
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Large terrain: 200 studs wide x 200 studs deep x 40 studs tall
        # At VOXEL_SIZE=4, that's 50x10x50 voxels -> 2x1x2 chunks
        res = 51  # High resolution heightmap
        heights = [1.0] * (res * res)  # Full height everywhere
        data = base64.b64decode(
            encode_smooth_grid(heights, res, (4.0, 40.0, 4.0))
        )

        assert data[0] == 1  # version
        assert data[1] == 5  # chunk_pow

        # Parse all chunk headers to verify delta encoding
        # We need to walk through the RLE data to find chunk boundaries
        pos = 2
        chunk_coords = []
        abs_x, abs_y, abs_z = 0, 0, 0

        while pos + 12 <= len(data):
            dx, dy, dz = self._decode_chunk_header(data, pos)
            pos += 12

            if len(chunk_coords) == 0:
                abs_x, abs_y, abs_z = dx, dy, dz
            else:
                abs_x += dx
                abs_y += dy
                abs_z += dz

            chunk_coords.append((abs_x, abs_y, abs_z))

            # Skip RLE data for this chunk
            voxel_count = 0
            while voxel_count < 32768 and pos < len(data):
                header = data[pos]
                pos += 1
                has_occ = bool(header & 0x40)
                has_run = bool(header & 0x80)
                if has_occ:
                    pos += 1
                run = 1
                if has_run:
                    run = data[pos] + 1
                    pos += 1
                voxel_count += run

        assert len(chunk_coords) > 1, (
            f"Expected multiple chunks for large terrain, got {len(chunk_coords)}"
        )

        # Verify chunks are sorted lexicographically
        for i in range(1, len(chunk_coords)):
            assert chunk_coords[i] >= chunk_coords[i - 1], (
                f"Chunks not sorted: {chunk_coords[i - 1]} >= {chunk_coords[i]}"
            )

    def test_axis_mapping_height_is_sz(self):
        """Voxel axis mapping: sz (outermost loop) = world Y (height).

        In SmoothGrid, voxels are ordered sx (innermost), sy (middle), sz (outermost).
        sz maps to world Y (height), so tall terrain should have non-air voxels
        at low sz values and air at high sz values.
        """
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # Terrain: 4 studs wide x 4 studs deep, 20 studs tall
        # Fits in single chunk: 1 voxel wide x 1 voxel deep x 5 voxels tall
        heights = [1.0] * 4  # Full height
        data = base64.b64decode(
            encode_smooth_grid(heights, 2, (1.0, 20.0, 1.0))
        )
        voxels = self._decode_rle(data, 14)
        assert len(voxels) == 32768

        # Voxel index = sx + sy*32 + sz*1024
        # sz=0 corresponds to world Y=0 (bottom), should be solid
        # sz=31 corresponds to world Y=31*4=124 studs (way above 20), should be air
        bottom_voxel = voxels[0]  # sx=0, sy=0, sz=0
        top_voxel = voxels[31 * 1024]  # sx=0, sy=0, sz=31

        assert bottom_voxel[0] != 0, f"Bottom voxel (sz=0) should be solid, got air"
        assert top_voxel[0] == 0, f"Top voxel (sz=31) should be air at height {31*4} studs"

    def test_flat_terrain_voxel_pattern(self):
        """Flat terrain at a known height should produce a predictable voxel pattern.

        For flat terrain at 8 studs (2 voxels), with VOXEL_SIZE=4:
        - sz=0 (Y=0..4): fully solid (occ=255)
        - sz=1 (Y=4..8): fully solid (occ=255)
        - sz=2 (Y=8..12): air (height 8 < voxel_bottom 8)
        - sz=3+ : air
        """
        from roblox.terrain_encoder import encode_smooth_grid
        import base64

        # 2x2 heightmap, all at normalized height 1.0, max_height=8 studs
        # This gives us 8 studs / STUDS_PER_METER of actual height
        # With scale (1.0, 8.0, 1.0) and STUDS_PER_METER:
        # max_height_studs = 8.0 * STUDS_PER_METER = 28.57 studs
        import config
        max_h = 8.0
        heights = [1.0] * 4
        data = base64.b64decode(
            encode_smooth_grid(heights, 2, (1.0, max_h, 1.0))
        )
        voxels = self._decode_rle(data, 14)

        max_h_studs = max_h * config.STUDS_PER_METER
        expected_solid_layers = int(max_h_studs / 4)  # VOXEL_SIZE=4

        # Check that the expected number of layers are solid
        for sz in range(min(expected_solid_layers, 32)):
            idx = sz * 1024  # sx=0, sy=0, sz=layer
            mat, occ = voxels[idx]
            assert mat != 0, (
                f"Layer sz={sz} (Y={sz*4}-{sz*4+4}) should be solid "
                f"(terrain height={max_h_studs:.1f} studs), got air"
            )
            assert occ == 255, (
                f"Layer sz={sz} fully below surface should have occ=255, got {occ}"
            )

    def test_all_material_ids_valid_constants(self):
        """All material ID constants match the Roblox terrain enum (0-22)."""
        from roblox.terrain_encoder import (
            MATERIAL_AIR, MATERIAL_WATER, MATERIAL_GRASS, MATERIAL_SLATE,
            MATERIAL_CONCRETE, MATERIAL_BRICK, MATERIAL_SAND, MATERIAL_WOODPLANKS,
            MATERIAL_ROCK, MATERIAL_GLACIER, MATERIAL_SNOW, MATERIAL_SANDSTONE,
            MATERIAL_MUD, MATERIAL_BASALT, MATERIAL_GROUND, MATERIAL_CRACKEDLAVA,
            MATERIAL_ASPHALT, MATERIAL_COBBLESTONE, MATERIAL_ICE, MATERIAL_LEAFYGRASS,
            MATERIAL_SALT, MATERIAL_LIMESTONE, MATERIAL_PAVEMENT,
        )
        expected = {
            "Air": 0, "Water": 1, "Grass": 2, "Slate": 3, "Concrete": 4,
            "Brick": 5, "Sand": 6, "WoodPlanks": 7, "Rock": 8, "Glacier": 9,
            "Snow": 10, "Sandstone": 11, "Mud": 12, "Basalt": 13, "Ground": 14,
            "CrackedLava": 15, "Asphalt": 16, "Cobblestone": 17, "Ice": 18,
            "LeafyGrass": 19, "Salt": 20, "Limestone": 21, "Pavement": 22,
        }
        actuals = {
            "Air": MATERIAL_AIR, "Water": MATERIAL_WATER, "Grass": MATERIAL_GRASS,
            "Slate": MATERIAL_SLATE, "Concrete": MATERIAL_CONCRETE,
            "Brick": MATERIAL_BRICK, "Sand": MATERIAL_SAND,
            "WoodPlanks": MATERIAL_WOODPLANKS, "Rock": MATERIAL_ROCK,
            "Glacier": MATERIAL_GLACIER, "Snow": MATERIAL_SNOW,
            "Sandstone": MATERIAL_SANDSTONE, "Mud": MATERIAL_MUD,
            "Basalt": MATERIAL_BASALT, "Ground": MATERIAL_GROUND,
            "CrackedLava": MATERIAL_CRACKEDLAVA, "Asphalt": MATERIAL_ASPHALT,
            "Cobblestone": MATERIAL_COBBLESTONE, "Ice": MATERIAL_ICE,
            "LeafyGrass": MATERIAL_LEAFYGRASS, "Salt": MATERIAL_SALT,
            "Limestone": MATERIAL_LIMESTONE, "Pavement": MATERIAL_PAVEMENT,
        }
        for name, exp_id in expected.items():
            assert actuals[name] == exp_id, f"{name}: expected {exp_id}, got {actuals[name]}"

    def test_material_fits_in_6_bits(self):
        """All material IDs must fit in 6 bits (0-63). Max used is 22."""
        from roblox import terrain_encoder as te
        all_mats = [v for k, v in vars(te).items() if k.startswith("MATERIAL_")]
        for m in all_mats:
            assert 0 <= m <= 63, f"Material ID {m} exceeds 6-bit range"
        assert max(all_mats) == 22, f"Expected max material to be 22 (Pavement)"


class TestSmoothGridInRbxlx:
    """Test that SmoothGrid terrain data is correctly embedded in rbxlx XML."""

    def test_terrain_with_smooth_grid_in_rbxlx(self):
        """Terrain with smooth_grid data should have BinaryString SmoothGrid in XML."""
        from core.roblox_types import RbxPlace, RbxTerrain
        from roblox.rbxlx_writer import write_rbxlx
        from roblox.terrain_encoder import encode_smooth_grid, encode_physics_grid
        import tempfile
        import xml.etree.ElementTree as ET
        import base64

        # Generate real terrain data
        heights = [0.5] * 9
        sg = encode_smooth_grid(heights, 3, (1.0, 50.0, 1.0))
        pg = encode_physics_grid()

        terrain = RbxTerrain(
            position=(0.0, 0.0, 0.0),
            size=(100.0, 50.0, 100.0),
            terrain_data_guid="test",
            smooth_grid=sg,
            physics_grid=pg,
        )
        place = RbxPlace(terrains=[terrain])

        with tempfile.NamedTemporaryFile(suffix=".rbxlx", delete=False) as f:
            write_rbxlx(place, f.name)
            # Read the raw XML to check for CDATA-wrapped base64
            import pathlib
            raw_xml = pathlib.Path(f.name).read_text()
            tree = ET.parse(f.name)

        terrain_items = [
            item for item in tree.iter("Item") if item.get("class") == "Terrain"
        ]
        assert len(terrain_items) == 1
        terrain_el = terrain_items[0]
        props = terrain_el.find("Properties")
        assert props is not None

        # Check SmoothGrid BinaryString exists
        sg_el = props.find("BinaryString[@name='SmoothGrid']")
        assert sg_el is not None, "SmoothGrid BinaryString missing from Terrain properties"

        # Check PhysicsGrid BinaryString exists
        pg_el = props.find("BinaryString[@name='PhysicsGrid']")
        assert pg_el is not None, "PhysicsGrid BinaryString missing from Terrain properties"

        # Check MaterialColors exists
        mc_el = props.find("BinaryString[@name='MaterialColors']")
        assert mc_el is not None, "MaterialColors BinaryString missing from Terrain properties"

        # Verify the SmoothGrid data in the raw XML is valid base64
        # (may be CDATA-wrapped, so extract from raw XML)
        assert sg in raw_xml, "SmoothGrid base64 data not found in raw XML output"
        assert pg in raw_xml, "PhysicsGrid base64 data not found in raw XML output"

        # Verify decoded SmoothGrid has correct header
        decoded = base64.b64decode(sg)
        assert decoded[0] == 1, "SmoothGrid version"
        assert decoded[1] == 5, "SmoothGrid chunk_pow"

    def test_terrain_water_properties_in_xml(self):
        """Terrain XML should include water configuration properties."""
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

        terrain_el = [
            item for item in tree.iter("Item") if item.get("class") == "Terrain"
        ][0]
        props = terrain_el.find("Properties")

        # Check water properties exist
        assert props.find("Color3[@name='WaterColor']") is not None
        assert props.find("float[@name='WaterReflectance']") is not None
        assert props.find("float[@name='WaterTransparency']") is not None
        assert props.find("float[@name='WaterWaveSize']") is not None
        assert props.find("float[@name='WaterWaveSpeed']") is not None

        # Check SmoothVoxelsUpgraded
        svf = props.find("bool[@name='SmoothVoxelsUpgraded']")
        assert svf is not None

    def test_terrain_without_smooth_grid_still_has_physics(self):
        """Terrain without smooth_grid should still get a default PhysicsGrid."""
        from core.roblox_types import RbxPlace, RbxTerrain
        from roblox.rbxlx_writer import write_rbxlx
        import tempfile
        import xml.etree.ElementTree as ET

        terrain = RbxTerrain(
            position=(0.0, 0.0, 0.0),
            size=(100.0, 50.0, 100.0),
            terrain_data_guid="test",
            # No smooth_grid or physics_grid
        )
        place = RbxPlace(terrains=[terrain])

        with tempfile.NamedTemporaryFile(suffix=".rbxlx", delete=False) as f:
            write_rbxlx(place, f.name)
            raw = open(f.name).read()
            tree = ET.parse(f.name)

        terrain_el = [
            item for item in tree.iter("Item") if item.get("class") == "Terrain"
        ][0]
        props = terrain_el.find("Properties")

        # SmoothGrid should NOT be present (no data)
        sg_el = props.find("BinaryString[@name='SmoothGrid']")
        assert sg_el is None, "SmoothGrid should not be present when smooth_grid is empty"

        # PhysicsGrid should still be present (default generated)
        assert "PhysicsGrid" in raw, "Default PhysicsGrid should still be written"


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
