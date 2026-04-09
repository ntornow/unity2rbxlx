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

    @pytest.mark.slow
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


class TestSpriteRendererAtlasRect:
    """Test that SpriteRenderer extracts atlas rect from .meta files."""

    def test_sprite_rect_attributes_set_from_meta(self, tmp_path):
        """When a sprite has a fileID matching a .meta sprite entry, rect attrs are stored."""
        from converter.scene_converter import _process_components
        from core.roblox_types import RbxPart
        from core.unity_types import GuidIndex, GuidEntry

        # Create a fake texture file and .meta with sprite atlas data
        tex_file = tmp_path / "atlas.png"
        tex_file.write_text("")
        meta_file = tmp_path / "atlas.png.meta"
        meta_file.write_text("""fileFormatVersion: 2
guid: aabbccdd11223344aabbccdd11223344
TextureImporter:
  spriteSheet:
    serializedVersion: 2
    sprites:
    - serializedVersion: 2
      name: sprite_0
      rect:
        serializedVersion: 2
        x: 10
        y: 20
        width: 64
        height: 32
      internalID: 21300000
    - serializedVersion: 2
      name: sprite_1
      rect:
        serializedVersion: 2
        x: 100
        y: 200
        width: 128
        height: 64
      internalID: 21300002
""")

        # Build a minimal GuidIndex
        guid = "aabbccdd11223344aabbccdd11223344"
        guid_index = GuidIndex(project_root=tmp_path)
        guid_index.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=tex_file.resolve(),
            relative_path=tex_file.relative_to(tmp_path),
            kind="texture",
        )

        # Fake node with SpriteRenderer component referencing sprite_1 (fileID=21300002)
        class FakeComp:
            component_type = "SpriteRenderer"
            properties = {
                "m_Color": {"r": 1, "g": 1, "b": 1, "a": 1},
                "m_Sprite": {"fileID": 21300002, "guid": guid, "type": 3},
            }

        class FakeNode:
            name = "SpriteObj"
            components = [FakeComp()]

        part = RbxPart(name="SpriteObj")
        _process_components(FakeNode(), part, guid_index=guid_index)

        assert part.attributes.get("_SpriteRectX") == 100.0
        assert part.attributes.get("_SpriteRectY") == 200.0
        assert part.attributes.get("_SpriteRectW") == 128.0
        assert part.attributes.get("_SpriteRectH") == 64.0

    def test_sprite_no_rect_when_no_meta(self):
        """Without a .meta file, no rect attributes should be set."""
        from converter.scene_converter import _process_components
        from core.roblox_types import RbxPart
        from core.unity_types import GuidIndex, GuidEntry
        from pathlib import Path

        guid = "deadbeef12345678deadbeef12345678"
        guid_index = GuidIndex(project_root=Path("/nonexistent"))
        guid_index.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=Path("/nonexistent/sprite.png"),
            relative_path=Path("sprite.png"),
            kind="texture",
        )

        class FakeComp:
            component_type = "SpriteRenderer"
            properties = {
                "m_Color": {"r": 1, "g": 0.5, "b": 0, "a": 0.8},
                "m_Sprite": {"fileID": 21300000, "guid": guid, "type": 3},
            }

        class FakeNode:
            name = "NoMetaSprite"
            components = [FakeComp()]

        part = RbxPart(name="NoMetaSprite")
        _process_components(FakeNode(), part, guid_index=guid_index)

        # No rect attrs because .meta file doesn't exist
        assert "_SpriteRectX" not in part.attributes
        # But guid should still be set
        assert part.attributes.get("_SpriteGuid") == guid

    def test_sprite_no_rect_when_fileid_not_in_atlas(self, tmp_path):
        """If fileID doesn't match any sprite in the atlas, no rect attributes."""
        from converter.scene_converter import _process_components
        from core.roblox_types import RbxPart
        from core.unity_types import GuidIndex, GuidEntry

        tex_file = tmp_path / "tex.png"
        tex_file.write_text("")
        meta_file = tmp_path / "tex.png.meta"
        meta_file.write_text("""fileFormatVersion: 2
guid: 11111111222222223333333344444444
TextureImporter:
  spriteSheet:
    serializedVersion: 2
    sprites:
    - serializedVersion: 2
      name: only_sprite
      rect:
        serializedVersion: 2
        x: 0
        y: 0
        width: 64
        height: 64
      internalID: 21300000
""")

        guid = "11111111222222223333333344444444"
        guid_index = GuidIndex(project_root=tmp_path)
        guid_index.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=tex_file.resolve(),
            relative_path=tex_file.relative_to(tmp_path),
            kind="texture",
        )

        class FakeComp:
            component_type = "SpriteRenderer"
            properties = {
                "m_Color": {"r": 1, "g": 1, "b": 1, "a": 1},
                "m_Sprite": {"fileID": 99999, "guid": guid, "type": 3},
            }

        class FakeNode:
            name = "WrongFileID"
            components = [FakeComp()]

        part = RbxPart(name="WrongFileID")
        _process_components(FakeNode(), part, guid_index=guid_index)

        assert "_SpriteRectX" not in part.attributes


class TestParseSpriteRects:
    """Test the parse_sprite_rects utility for reading atlas data from .meta files."""

    def test_multi_sprite_atlas(self, tmp_path):
        """Parse a meta file with multiple sprites in the atlas."""
        from unity.guid_resolver import parse_sprite_rects

        meta = tmp_path / "atlas.png.meta"
        meta.write_text("""fileFormatVersion: 2
guid: aabb0011aabb0011aabb0011aabb0011
TextureImporter:
  spriteSheet:
    serializedVersion: 2
    sprites:
    - serializedVersion: 2
      name: sprite_0
      rect:
        serializedVersion: 2
        x: 0
        y: 0
        width: 64
        height: 64
      internalID: 21300000
    - serializedVersion: 2
      name: sprite_1
      rect:
        serializedVersion: 2
        x: 64
        y: 0
        width: 128
        height: 96
      internalID: 21300002
    - serializedVersion: 2
      name: sprite_2
      rect:
        serializedVersion: 2
        x: 200
        y: 100
        width: 50
        height: 50
      internalID: 21300004
""")
        rects = parse_sprite_rects(meta)
        assert len(rects) == 3
        assert rects["21300000"] == (0.0, 0.0, 64.0, 64.0)
        assert rects["21300002"] == (64.0, 0.0, 128.0, 96.0)
        assert rects["21300004"] == (200.0, 100.0, 50.0, 50.0)

    def test_empty_sprites_list(self, tmp_path):
        """Meta file with empty sprites: [] should return empty dict."""
        from unity.guid_resolver import parse_sprite_rects

        meta = tmp_path / "single.png.meta"
        meta.write_text("""fileFormatVersion: 2
guid: deadbeefdeadbeefdeadbeefdeadbeef
TextureImporter:
  spriteSheet:
    serializedVersion: 2
    sprites: []
""")
        rects = parse_sprite_rects(meta)
        assert rects == {}

    def test_no_sprite_sheet_section(self, tmp_path):
        """Meta file without spriteSheet section should return empty dict."""
        from unity.guid_resolver import parse_sprite_rects

        meta = tmp_path / "mesh.fbx.meta"
        meta.write_text("""fileFormatVersion: 2
guid: 1234567812345678
ModelImporter:
  meshes:
    - name: mesh0
""")
        rects = parse_sprite_rects(meta)
        assert rects == {}

    def test_nonexistent_file(self):
        """Nonexistent file should return empty dict without error."""
        from unity.guid_resolver import parse_sprite_rects
        from pathlib import Path

        rects = parse_sprite_rects(Path("/nonexistent/file.meta"))
        assert rects == {}

    def test_single_sprite_full_texture(self, tmp_path):
        """Single sprite covering the whole texture."""
        from unity.guid_resolver import parse_sprite_rects

        meta = tmp_path / "full.png.meta"
        meta.write_text("""fileFormatVersion: 2
guid: 00112233445566778899aabbccddeeff
TextureImporter:
  spriteSheet:
    serializedVersion: 2
    sprites:
    - serializedVersion: 2
      name: full_sprite
      rect:
        serializedVersion: 2
        x: 0
        y: 0
        width: 512
        height: 512
      alignment: 0
      pivot: {x: 0.5, y: 0.5}
      border: {x: 0, y: 0, z: 0, w: 0}
      customData:
      outline: []
      physicsShape: []
      tessellationDetail: 0
      bones: []
      spriteID: 02305410000000000800000000000000
      internalID: 21300000
      vertices: []
      indices:
      edges: []
      weights: []
    outline: []
""")
        rects = parse_sprite_rects(meta)
        assert len(rects) == 1
        assert rects["21300000"] == (0.0, 0.0, 512.0, 512.0)


class TestMeshSizeFbxBboxFallback:
    """Test FBX bounding box fallback for mesh InitialSize."""

    def _make_guid_index(self, guid, rel_path, abs_path, meta_text=None):
        """Create a minimal GuidIndex stub."""
        class FakeGuidIndex:
            project_root = Path("/fake/project")
            def resolve(self, g):
                return Path(abs_path) if g == guid else None
            def resolve_relative(self, g):
                return Path(rel_path) if g == guid else None
        gi = FakeGuidIndex()
        if meta_text is not None:
            meta_path = Path(str(abs_path) + ".meta")
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(meta_text, encoding="utf-8")
        return gi

    def test_fbx_bbox_produces_initial_size(self, tmp_path):
        """When FBX bbox data is available, it should be used as InitialSize."""
        import converter.scene_converter as sc

        # Set up FBX bounding boxes (simulating trimesh output)
        old_bboxes = sc._fbx_bounding_boxes
        try:
            sc._fbx_bounding_boxes = {
                "Assets/Models/crate.fbx": (100.0, 50.0, 80.0),
            }

            abs_path = tmp_path / "Assets" / "Models" / "crate.fbx"
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.touch()
            # Write a .meta file with default import scale (0.01)
            meta = abs_path.with_suffix(".fbx.meta")
            meta.write_text("globalScale: 0.01\nuseFileScale: 0\n")

            guid = "test-guid-123"
            gi = self._make_guid_index(guid, "Assets/Models/crate.fbx", str(abs_path))

            result = sc._compute_mesh_size_from_fbx_bbox(
                unity_scale=(1.0, 1.0, 1.0),
                mesh_guid=guid,
                guid_index=gi,
            )
            assert result is not None
            size, initial_size = result
            # InitialSize should match the FBX bbox
            assert initial_size == (100.0, 50.0, 80.0)
            # Size = InitialSize * import_scale(0.01) * STUDS_PER_METER(3.571) * unity_scale(1)
            import config
            expected_factor = 0.01 * config.STUDS_PER_METER
            assert abs(size[0] - 100.0 * expected_factor) < 0.01
            assert abs(size[1] - 50.0 * expected_factor) < 0.01
            assert abs(size[2] - 80.0 * expected_factor) < 0.01
        finally:
            sc._fbx_bounding_boxes = old_bboxes

    def test_fbx_bbox_not_available_returns_none(self):
        """When no FBX bbox data exists, the function returns None."""
        import converter.scene_converter as sc

        old_bboxes = sc._fbx_bounding_boxes
        try:
            sc._fbx_bounding_boxes = {}
            result = sc._compute_mesh_size_from_fbx_bbox(
                unity_scale=(1.0, 1.0, 1.0),
                mesh_guid="nonexistent",
                guid_index=self._make_guid_index("other", "x.fbx", "/tmp/x.fbx"),
            )
            assert result is None
        finally:
            sc._fbx_bounding_boxes = old_bboxes

    def test_fbx_bbox_respects_unity_scale(self, tmp_path):
        """Unity scale should multiply into the final Size."""
        import converter.scene_converter as sc

        old_bboxes = sc._fbx_bounding_boxes
        try:
            sc._fbx_bounding_boxes = {
                "Assets/Models/pillar.fbx": (20.0, 200.0, 20.0),
            }

            abs_path = tmp_path / "Assets" / "Models" / "pillar.fbx"
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.touch()
            meta = abs_path.with_suffix(".fbx.meta")
            meta.write_text("globalScale: 0.01\nuseFileScale: 0\n")

            guid = "pillar-guid"
            gi = self._make_guid_index(guid, "Assets/Models/pillar.fbx", str(abs_path))

            result = sc._compute_mesh_size_from_fbx_bbox(
                unity_scale=(2.0, 3.0, 0.5),
                mesh_guid=guid,
                guid_index=gi,
            )
            assert result is not None
            size, initial_size = result
            assert initial_size == (20.0, 200.0, 20.0)
            import config
            f = 0.01 * config.STUDS_PER_METER
            assert abs(size[0] - 2.0 * 20.0 * f) < 0.01
            assert abs(size[1] - 3.0 * 200.0 * f) < 0.01
            assert abs(size[2] - 0.5 * 20.0 * f) < 0.01
        finally:
            sc._fbx_bounding_boxes = old_bboxes


class TestMeshVerticalOffsetSubMesh:
    """Test per-sub-mesh vertical offset selection."""

    def test_submesh_name_fallback(self):
        """When fileID index is out of bounds, fall back to name matching."""
        import converter.scene_converter as sc
        from core.unity_types import GuidIndex, GuidEntry
        from pathlib import Path

        old_mh = sc._mesh_hierarchies
        old_cache = sc._mesh_vertical_offset_cache
        try:
            sc._mesh_hierarchies = {
                "Assets/door.fbx": [
                    {"name": "frame_col", "position": [0, 3.22, 0], "size": [7, 6, 1]},
                    {"name": "base", "position": [0, 0.14, 0], "size": [5, 0.15, 2]},
                    {"name": "door", "position": [0, 2.66, 0], "size": [5, 5, 0.7]},
                ]
            }
            sc._mesh_vertical_offset_cache = {}

            gi = GuidIndex(project_root=Path("/fake"))
            gi.guid_to_entry["door-guid"] = GuidEntry(
                guid="door-guid",
                asset_path=Path("/fake/Assets/door.fbx"),
                relative_path=Path("Assets/door.fbx"),
                kind="model",
            )

            # Using mesh_name="door" should find the door sub-mesh (pos Y=2.66)
            # not the first sub-mesh (frame_col at Y=3.22)
            import config
            offset = sc._compute_mesh_vertical_offset(
                "door-guid", gi, 1.0,
                mesh_file_id="9999999",  # invalid fileID to force name fallback
                mesh_name="door",
            )
            # Default import_scale is 0.01 (cm→m) when no .meta file exists
            expected = 2.66 * 0.01 * config.STUDS_PER_METER
            assert abs(offset - expected) < 0.01, f"Expected ~{expected:.4f}, got {offset:.4f}"

            # Verify it used "door" not "frame_col" (Y=3.22) or "base" (Y=0.14)
            frame_col_offset = 3.22 * 0.01 * config.STUDS_PER_METER
            base_offset = 0.14 * 0.01 * config.STUDS_PER_METER
            assert abs(offset - frame_col_offset) > 0.01, "Should NOT use frame_col"
            assert abs(offset - base_offset) > 0.01, "Should NOT use base"
        finally:
            sc._mesh_hierarchies = old_mh
            sc._mesh_vertical_offset_cache = old_cache


class TestMixedColliderHandling:
    """Test that physical + trigger colliders on the same node work correctly."""

    def test_physical_then_trigger_keeps_collidable(self):
        """Physical collider followed by trigger → CanCollide stays True."""
        from converter.scene_converter import _process_components
        from core.roblox_types import RbxPart

        class FakeBoxCollider:
            component_type = "BoxCollider"
            properties = {"m_IsTrigger": 0, "m_Size": {"x": 1, "y": 1, "z": 1}, "m_Center": {"x": 0, "y": 0, "z": 0}}

        class FakeTriggerCollider:
            component_type = "SphereCollider"
            properties = {"m_IsTrigger": 1, "m_Radius": 3, "m_Center": {"x": 0, "y": 0, "z": 0}}

        class FakeNode:
            name = "DoorBase"
            components = [FakeBoxCollider(), FakeTriggerCollider()]
            mesh_guid = None

        part = RbxPart(name="DoorBase", size=(3.571, 3.571, 3.571))
        _process_components(FakeNode(), part)

        assert part.can_collide is True, "Physical collider should keep CanCollide=True"
        assert getattr(part, 'can_query', True) is True, "Trigger should set CanQuery=True"

    def test_trigger_then_physical_keeps_collidable(self):
        """Trigger first, then physical → CanCollide should be True."""
        from converter.scene_converter import _process_components
        from core.roblox_types import RbxPart

        class FakeTrigger:
            component_type = "SphereCollider"
            properties = {"m_IsTrigger": 1, "m_Radius": 3, "m_Center": {"x": 0, "y": 0, "z": 0}}

        class FakeBox:
            component_type = "BoxCollider"
            properties = {"m_IsTrigger": 0, "m_Size": {"x": 1, "y": 1, "z": 1}, "m_Center": {"x": 0, "y": 0, "z": 0}}

        class FakeNode:
            name = "DoorBase"
            components = [FakeTrigger(), FakeBox()]
            mesh_guid = None

        part = RbxPart(name="DoorBase", size=(3.571, 3.571, 3.571))
        _process_components(FakeNode(), part)

        assert part.can_collide is True, "Physical collider should override trigger's CanCollide=False"
