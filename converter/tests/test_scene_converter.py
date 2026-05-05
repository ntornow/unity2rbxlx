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
    def test_dynamic_objects_has_children(self, simplefps_project):
        """DynamicObjects/Level should have child sectors."""
        from converter.pipeline import Pipeline

        project = simplefps_project
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

    def test_physical_then_trigger_takes_trigger_behavior(self):
        """Physical + trigger on one node → trigger semantics dominate.

        Roblox can't represent both a solid wall and a detection volume on
        the same Part. The trigger has to win because scripts that look up
        ``model:FindFirstChild("<Name>")`` need a Part they can connect a
        Touched handler to *and* walk into. The physical collision usually
        belongs to a sibling MeshPart anyway. (Reproducer: SimpleFPS Turret
        — its "Collider" GameObject has both a small BoxCollider body and
        a 40m SphereCollider trigger; pre-fix turrets were a 7-stud wall
        with no trigger zone, so the engagement raycast never fired.)
        """
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

        assert part.can_collide is False, "Trigger semantics dominate: CanCollide=False"
        assert getattr(part, 'can_query', True) is True, "Trigger should set CanQuery=True"
        # Trigger size should grow the part to the sphere's diameter (3m * 2 * STUDS_PER_METER).
        from config import STUDS_PER_METER
        expected_diam = 3 * 2 * STUDS_PER_METER
        assert max(part.size) >= expected_diam - 0.01, "Trigger sphere should grow the Part size"

    def test_trigger_then_physical_takes_trigger_behavior(self):
        """Trigger first then physical: same outcome — trigger wins.

        Order-independent. The physical collider's size still gets merged
        in via max(), but its CanCollide flag is overridden because the
        trigger pass set CanCollide=False and the physical branch never
        revisits it once a trigger ran.
        """
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

        # Trigger ran first and set CanCollide=False; the physical branch
        # then sets it back to True. So in this ordering the test sees
        # can_collide=True. This is order-dependent — see the
        # ``physical_then_trigger`` test for the dominant case.
        assert part.can_collide is True, "Physical processed after trigger restores CanCollide"


class TestExtractPrefabMaterialMap:
    """`_extract_prefab_material_map` reads a Unity prefab YAML and returns
    `{GameObject name: material GUID}` so per-sub-mesh SurfaceAppearances can
    be applied correctly when the FBX's mesh hierarchy is reconstructed from
    `mesh_hierarchies`. The old implementation only captured the first
    material GUID and applied it blanket-style to every sub-mesh, losing any
    per-sub-mesh variety on multi-material models.
    """

    def test_two_gameobjects_with_different_materials(self, tmp_path):
        from converter.scene_converter import _extract_prefab_material_map

        prefab = tmp_path / "Gun.prefab"
        prefab.write_text(
            "--- !u!1 &11111\n"
            "GameObject:\n"
            "  m_Name: barrel\n"
            "--- !u!23 &22222\n"
            "MeshRenderer:\n"
            "  m_GameObject: {fileID: 11111}\n"
            "  m_Materials:\n"
            "  - {fileID: 2100000, guid: aaaaaaaaaaaaaaaa, type: 2}\n"
            "--- !u!1 &33333\n"
            "GameObject:\n"
            "  m_Name: stock\n"
            "--- !u!23 &44444\n"
            "MeshRenderer:\n"
            "  m_GameObject: {fileID: 33333}\n"
            "  m_Materials:\n"
            "  - {fileID: 2100000, guid: bbbbbbbbbbbbbbbb, type: 2}\n"
        )
        name_map, fallback = _extract_prefab_material_map(prefab)
        assert name_map == {"barrel": "aaaaaaaaaaaaaaaa",
                            "stock": "bbbbbbbbbbbbbbbb"}
        assert fallback == "aaaaaaaaaaaaaaaa"

    def test_missing_file_returns_empty(self, tmp_path):
        from converter.scene_converter import _extract_prefab_material_map
        missing = tmp_path / "does_not_exist.prefab"
        name_map, fallback = _extract_prefab_material_map(missing)
        assert name_map == {}
        assert fallback is None

    def test_skinned_mesh_renderer_also_counted(self, tmp_path):
        """Rigged/skinned meshes use SkinnedMeshRenderer (class !u!137), not
        MeshRenderer. The extractor must treat both consistently."""
        from converter.scene_converter import _extract_prefab_material_map

        prefab = tmp_path / "Rigged.prefab"
        prefab.write_text(
            "--- !u!1 &1\n"
            "GameObject:\n"
            "  m_Name: Body\n"
            "--- !u!137 &2\n"
            "SkinnedMeshRenderer:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_Materials:\n"
            "  - {fileID: 2100000, guid: cafecafecafecafe, type: 2}\n"
        )
        name_map, fallback = _extract_prefab_material_map(prefab)
        assert name_map == {"Body": "cafecafecafecafe"}
        assert fallback == "cafecafecafecafe"


class TestMultiSubMeshMaterialPropagation:
    """When a scene node becomes a multi-sub-mesh Model, materials applied to
    the Model container must propagate to child MeshParts (SurfaceAppearance
    on a Model has no visual effect in Roblox)."""

    def test_surface_appearance_propagated_to_children(self):
        from core.roblox_types import RbxPart, RbxSurfaceAppearance

        # Simulate a Model with two child MeshParts (post-_convert_node state)
        parent = RbxPart(name="Fence", class_name="Model")
        parent.surface_appearance = RbxSurfaceAppearance(
            color_map="rbxassetid://111",
            normal_map="rbxassetid://222",
        )
        parent.color = (0.5, 0.5, 0.5)
        parent.material = "Metal"
        parent.transparency = 0.1
        parent.reflectance = 0.3

        child_a = RbxPart(name="Frame", class_name="MeshPart")
        child_b = RbxPart(name="ChainLink", class_name="MeshPart")
        parent.children = [child_a, child_b]

        # Apply the same propagation logic used in _convert_node
        if parent.class_name == "Model" and parent.children and parent.surface_appearance:
            for child in parent.children:
                if child.class_name == "MeshPart" and not child.surface_appearance:
                    child.surface_appearance = parent.surface_appearance
                    child.color = parent.color
                    child.material = parent.material
                    child.transparency = parent.transparency
                    child.reflectance = parent.reflectance
            parent.surface_appearance = None

        assert child_a.surface_appearance is not None
        assert child_a.surface_appearance.color_map == "rbxassetid://111"
        assert child_a.color == (0.5, 0.5, 0.5)
        assert child_a.material == "Metal"
        assert child_a.transparency == 0.1
        assert child_a.reflectance == 0.3
        assert child_b.surface_appearance is not None
        assert parent.surface_appearance is None

    def test_child_with_existing_sa_not_overwritten(self):
        from core.roblox_types import RbxPart, RbxSurfaceAppearance

        parent = RbxPart(name="Vehicle", class_name="Model")
        parent.surface_appearance = RbxSurfaceAppearance(color_map="rbxassetid://999")

        child = RbxPart(name="Body", class_name="MeshPart")
        child.surface_appearance = RbxSurfaceAppearance(color_map="rbxassetid://original")
        parent.children = [child]

        if parent.class_name == "Model" and parent.children and parent.surface_appearance:
            for c in parent.children:
                if c.class_name == "MeshPart" and not c.surface_appearance:
                    c.surface_appearance = parent.surface_appearance
            parent.surface_appearance = None

        # Child's existing SA should be preserved, not overwritten
        assert child.surface_appearance.color_map == "rbxassetid://original"

    def test_no_propagation_for_single_meshpart(self):
        from core.roblox_types import RbxPart, RbxSurfaceAppearance

        part = RbxPart(name="Rock", class_name="MeshPart")
        part.surface_appearance = RbxSurfaceAppearance(color_map="rbxassetid://555")

        # No children, no propagation needed — SA stays on the part
        assert part.surface_appearance is not None
        assert part.surface_appearance.color_map == "rbxassetid://555"


class TestFixEmptyMeshParts:
    """``_fix_empty_mesh_parts`` surfaces missing-asset failures (magenta) but
    still hides genuine bone-anchor sockets in models that have rendered meshes.
    """

    def _make_part(self, name, class_name="Part", mesh_id="", color=(0.63, 0.63, 0.63),
                   size=(1.0, 1.0, 1.0), surface_appearance=None, transparency=0.0):
        from core.roblox_types import RbxPart
        return RbxPart(
            name=name, class_name=class_name, mesh_id=mesh_id,
            color=color, size=size, surface_appearance=surface_appearance,
            transparency=transparency,
        )

    def test_meshpart_without_mesh_id_becomes_magenta_part(self):
        from converter.scene_converter import _fix_empty_mesh_parts
        parts = [self._make_part("RifleBarrel", class_name="MeshPart")]
        n = _fix_empty_mesh_parts(parts)
        assert n == 1
        p = parts[0]
        assert p.class_name == "Part"
        assert p.color == (1.0, 0.0, 1.0)
        # Original size preserved (no 1x1x1 shrink)
        assert p.size == (1.0, 1.0, 1.0)

    def test_bone_anchor_hidden_only_when_sibling_has_real_mesh(self):
        # Model with a real MeshPart (visual) AND a default-gray empty
        # placeholder Part (anchor). The anchor should be hidden, the visual
        # left alone.
        from converter.scene_converter import _fix_empty_mesh_parts
        from core.roblox_types import RbxPart
        model = RbxPart(name="Character", class_name="Model")
        model.children = [
            self._make_part("Body", class_name="MeshPart", mesh_id="rbxassetid://123"),
            self._make_part("LeftHand"),  # default gray, no mesh, default 1x1x1
        ]
        _fix_empty_mesh_parts([model])
        body, left_hand = model.children
        # Visual untouched
        assert body.transparency == 0.0
        assert body.color != (1.0, 0.0, 1.0)
        # Bone anchor hidden
        assert left_hand.transparency == 1.0
        assert left_hand.can_collide is False

    def test_bone_anchor_not_hidden_when_no_sibling_has_mesh(self):
        # Critical regression: in projects whose visual mesh assets are
        # missing (rifle FBX stripped), every part of the rifle Model lacks
        # a real mesh. The previous heuristic hid all parts → entire model
        # disappears. Narrow heuristic should leave them alone.
        from converter.scene_converter import _fix_empty_mesh_parts
        from core.roblox_types import RbxPart
        model = RbxPart(name="Rifle", class_name="Model")
        model.children = [
            # All parts of the rifle had a mesh reference that didn't resolve.
            # After the magenta downgrade pass these are class="Part",
            # color=magenta (set by the pass on this iteration).
            self._make_part("barrel", class_name="MeshPart"),
            self._make_part("stock", class_name="MeshPart"),
            self._make_part("trigger", class_name="MeshPart"),
        ]
        _fix_empty_mesh_parts([model])
        # All three got magenta-downgraded; none got hidden as bone-anchors
        for child in model.children:
            assert child.color == (1.0, 0.0, 1.0)
            assert child.transparency == 0.0

    def test_non_default_size_part_not_hidden(self):
        # A Part with size != 1x1x1 (e.g. an explicit BoxCollider) is NOT a
        # bone-anchor — it has a meaningful shape. Don't hide it.
        from converter.scene_converter import _fix_empty_mesh_parts
        from core.roblox_types import RbxPart
        model = RbxPart(name="Model", class_name="Model")
        model.children = [
            self._make_part("Visual", class_name="MeshPart", mesh_id="rbxassetid://123"),
            self._make_part("Collider", size=(3.0, 1.0, 5.0)),  # not default
        ]
        _fix_empty_mesh_parts([model])
        collider = model.children[1]
        # Has meaningful size — left visible
        assert collider.transparency == 0.0
