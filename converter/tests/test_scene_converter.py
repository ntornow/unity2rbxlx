"""Tests for scene_converter.py key functions."""

from contextlib import contextmanager

import pytest
from pathlib import Path


@contextmanager
def _scene_ctx(**kwargs):
    """Activate a SceneConversionContext for the duration of the block.

    Used by tests that exercise scene_converter helpers in isolation.
    Production code goes through ``convert_scene()`` which sets up the
    context automatically.
    """
    import converter.scene_converter as sc
    old = sc._current_ctx
    sc._current_ctx = sc.SceneConversionContext(**kwargs)
    try:
        yield sc._current_ctx
    finally:
        sc._current_ctx = old


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

        abs_path = tmp_path / "Assets" / "Models" / "crate.fbx"
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.touch()
        meta = abs_path.with_suffix(".fbx.meta")
        meta.write_text("globalScale: 0.01\nuseFileScale: 0\n")

        guid = "test-guid-123"
        gi = self._make_guid_index(guid, "Assets/Models/crate.fbx", str(abs_path))

        with _scene_ctx(fbx_bounding_boxes={
            "Assets/Models/crate.fbx": (100.0, 50.0, 80.0),
        }):
            result = sc._compute_mesh_size_from_fbx_bbox(
                unity_scale=(1.0, 1.0, 1.0),
                mesh_guid=guid,
                guid_index=gi,
            )
        assert result is not None
        size, initial_size = result
        assert initial_size == (100.0, 50.0, 80.0)
        import config
        expected_factor = 0.01 * config.STUDS_PER_METER
        assert abs(size[0] - 100.0 * expected_factor) < 0.01
        assert abs(size[1] - 50.0 * expected_factor) < 0.01
        assert abs(size[2] - 80.0 * expected_factor) < 0.01

    def test_fbx_bbox_not_available_returns_none(self):
        """When no FBX bbox data exists, the function returns None."""
        import converter.scene_converter as sc

        with _scene_ctx(fbx_bounding_boxes={}):
            result = sc._compute_mesh_size_from_fbx_bbox(
                unity_scale=(1.0, 1.0, 1.0),
                mesh_guid="nonexistent",
                guid_index=self._make_guid_index("other", "x.fbx", "/tmp/x.fbx"),
            )
        assert result is None

    def test_fbx_bbox_respects_unity_scale(self, tmp_path):
        """Unity scale should multiply into the final Size."""
        import converter.scene_converter as sc

        abs_path = tmp_path / "Assets" / "Models" / "pillar.fbx"
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.touch()
        meta = abs_path.with_suffix(".fbx.meta")
        meta.write_text("globalScale: 0.01\nuseFileScale: 0\n")

        guid = "pillar-guid"
        gi = self._make_guid_index(guid, "Assets/Models/pillar.fbx", str(abs_path))

        with _scene_ctx(fbx_bounding_boxes={
            "Assets/Models/pillar.fbx": (20.0, 200.0, 20.0),
        }):
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


# A legacy NativeFormatImporter prefab embedding one Mesh as a !u!43 object,
# modelled on SimpleFPS's at_mine_LOD3.prefab (real m_LocalAABB values).
_EMBEDDED_MESH_PREFAB = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &107984
GameObject:
  m_Name: Mine
--- !u!43 &4322892
Mesh:
  m_Name: AT_Mine_LOD3
  serializedVersion: 11
  m_SubMeshes:
  - serializedVersion: 2
    localAABB:
      m_Center: {x: 0, y: 0, z: 0}
      m_Extent: {x: 99, y: 99, z: 99}
  m_LocalAABB:
    m_Center: {x: -0.000000074505806, y: -0.0014436208, z: -0.021289438}
    m_Extent: {x: 0.1615152, y: 0.048787132, z: 0.17490588}
  m_MeshCompression: 0
"""

# Two embedded meshes in one asset (LOD variants) -- fileID is required to
# pick one unambiguously.
_MULTI_MESH_PREFAB = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!43 &111
Mesh:
  m_Name: LOD0
  m_LocalAABB:
    m_Center: {x: 0, y: 0, z: 0}
    m_Extent: {x: 1, y: 2, z: 3}
--- !u!43 &222
Mesh:
  m_Name: LOD1
  m_LocalAABB:
    m_Center: {x: 0, y: 0, z: 0}
    m_Extent: {x: 4, y: 5, z: 6}
"""


class TestEmbeddedMeshAabb:
    """Embedded-mesh size recovery for legacy .prefab/.asset packs."""

    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        """The AABB cache is module-global; reset it around each test."""
        import converter.scene_converter as sc
        sc._embedded_mesh_aabb_cache.clear()
        yield
        sc._embedded_mesh_aabb_cache.clear()

    def _make_guid_index(self, guid, abs_path):
        class FakeGuidIndex:
            project_root = Path("/fake/project")
            def resolve(self, g):
                return Path(abs_path) if g == guid else None
            def resolve_relative(self, g):
                return None
        return FakeGuidIndex()

    def test_reads_top_level_local_aabb(self, tmp_path):
        """m_LocalAABB extent (half-size) becomes full size in metres."""
        import converter.scene_converter as sc

        prefab = tmp_path / "at_mine_LOD3.prefab"
        prefab.write_text(_EMBEDDED_MESH_PREFAB, encoding="utf-8")

        size = sc._read_embedded_mesh_aabb(prefab, "4322892")
        assert size is not None
        # Full size = 2 x extent; the per-submesh localAABB (99) is ignored.
        assert abs(size[0] - 0.3230304) < 1e-5
        assert abs(size[1] - 0.097574264) < 1e-5
        assert abs(size[2] - 0.34981176) < 1e-5

    def test_single_mesh_resolves_without_fileid(self, tmp_path):
        """A lone embedded mesh is unambiguous even with no fileID hint."""
        import converter.scene_converter as sc

        prefab = tmp_path / "single.prefab"
        prefab.write_text(_EMBEDDED_MESH_PREFAB, encoding="utf-8")
        assert sc._read_embedded_mesh_aabb(prefab, None) is not None

    def test_asset_extension_supported(self, tmp_path):
        """A .asset container is parsed the same as a .prefab."""
        import converter.scene_converter as sc

        asset = tmp_path / "mesh.asset"
        asset.write_text(_EMBEDDED_MESH_PREFAB, encoding="utf-8")
        assert sc._read_embedded_mesh_aabb(asset, "4322892") is not None

    def test_fbx_asset_returns_none(self, tmp_path):
        """Only .prefab/.asset containers are parsed; .fbx is uploaded."""
        import converter.scene_converter as sc

        fbx = tmp_path / "model.fbx"
        fbx.write_text(_EMBEDDED_MESH_PREFAB, encoding="utf-8")
        assert sc._read_embedded_mesh_aabb(fbx, "4322892") is None

    def test_multiple_meshes_need_fileid(self, tmp_path):
        """With several embedded meshes and no fileID hint, bail (no guess)."""
        import converter.scene_converter as sc

        prefab = tmp_path / "lods.prefab"
        prefab.write_text(_MULTI_MESH_PREFAB, encoding="utf-8")
        assert sc._read_embedded_mesh_aabb(prefab, None) is None
        # An explicit fileID resolves the right one.
        assert sc._read_embedded_mesh_aabb(prefab, "222") == (8.0, 10.0, 12.0)

    def test_unmatched_fileid_returns_none(self, tmp_path):
        """A fileID that matches no embedded mesh never falls back to a guess."""
        import converter.scene_converter as sc

        prefab = tmp_path / "lods.prefab"
        prefab.write_text(_MULTI_MESH_PREFAB, encoding="utf-8")
        assert sc._read_embedded_mesh_aabb(prefab, "999") is None

    def test_zero_extent_skipped(self, tmp_path):
        """A degenerate zero-size AABB is not used as a mesh size."""
        import converter.scene_converter as sc

        prefab = tmp_path / "degenerate.prefab"
        prefab.write_text(
            "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
            "--- !u!43 &1\nMesh:\n  m_Name: Empty\n  m_LocalAABB:\n"
            "    m_Center: {x: 0, y: 0, z: 0}\n"
            "    m_Extent: {x: 0, y: 0, z: 0}\n",
            encoding="utf-8",
        )
        assert sc._read_embedded_mesh_aabb(prefab, "1") is None

    def test_mesh_without_aabb_skipped(self, tmp_path):
        """A !u!43 block missing m_LocalAABB is skipped, not crashed on."""
        import converter.scene_converter as sc

        prefab = tmp_path / "no_aabb.prefab"
        prefab.write_text(
            "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
            "--- !u!43 &1\nMesh:\n  m_Name: NoAabb\n  m_MeshCompression: 0\n",
            encoding="utf-8",
        )
        assert sc._read_embedded_mesh_aabb(prefab, "1") is None

    def test_missing_file_returns_none(self, tmp_path):
        """A non-existent asset path yields None without raising."""
        import converter.scene_converter as sc

        assert sc._read_embedded_mesh_aabb(tmp_path / "gone.prefab", "1") is None

    def test_second_call_served_from_cache(self, tmp_path):
        """The asset is parsed once; later calls reuse the cached AABBs."""
        import converter.scene_converter as sc

        prefab = tmp_path / "at_mine_LOD3.prefab"
        prefab.write_text(_EMBEDDED_MESH_PREFAB, encoding="utf-8")
        first = sc._read_embedded_mesh_aabb(prefab, "4322892")
        assert str(prefab) in sc._embedded_mesh_aabb_cache
        # Corrupt the file -- a cached read must still return the first result.
        prefab.write_text("garbage", encoding="utf-8")
        assert sc._read_embedded_mesh_aabb(prefab, "4322892") == first

    def test_compute_mesh_size_uses_embedded_aabb(self, tmp_path):
        """_compute_mesh_size recovers size for an unuploaded embedded mesh."""
        import converter.scene_converter as sc
        import config

        prefab = tmp_path / "at_mine_LOD3.prefab"
        prefab.write_text(_EMBEDDED_MESH_PREFAB, encoding="utf-8")
        guid = "c95ed669496457245b94a76f843f0381"
        gi = self._make_guid_index(guid, str(prefab))

        with _scene_ctx():
            result = sc._compute_mesh_size(
                unity_scale=(2.5, 2.5, 2.5),
                mesh_guid=guid,
                guid_index=gi,
                mesh_native_sizes={},
                mesh_file_id="4322892",
            )
        assert result is not None
        size, initial = result
        f = config.STUDS_PER_METER
        assert abs(size[0] - 0.3230304 * 2.5 * f) < 0.01
        assert abs(size[1] - 0.097574264 * 2.5 * f) < 0.01
        assert abs(size[2] - 0.34981176 * 2.5 * f) < 0.01
        assert abs(initial[0] - 0.3230304) < 1e-5

    def test_fbx_bbox_fallback_recovers_embedded_mesh(self, tmp_path):
        """When no meshes resolved at all, the fbx-bbox stop still recovers
        an embedded mesh -- the path a converter run with no uploads takes."""
        import converter.scene_converter as sc
        import config

        prefab = tmp_path / "at_mine_LOD3.prefab"
        prefab.write_text(_EMBEDDED_MESH_PREFAB, encoding="utf-8")
        guid = "c95ed669496457245b94a76f843f0381"
        gi = self._make_guid_index(guid, str(prefab))

        with _scene_ctx(fbx_bounding_boxes={}):
            result = sc._compute_mesh_size_from_fbx_bbox(
                unity_scale=(1.0, 1.0, 1.0),
                mesh_guid=guid,
                guid_index=gi,
            )
        assert result is not None
        size, _ = result
        assert abs(size[0] - 0.3230304 * config.STUDS_PER_METER) < 0.01

    def test_fbx_bbox_fallback_threads_fileid(self, tmp_path):
        """The fbx-bbox fallback forwards mesh_file_id, so a multi-mesh
        legacy asset still resolves the right sub-mesh with no uploads."""
        import converter.scene_converter as sc
        import config

        prefab = tmp_path / "lods.prefab"
        prefab.write_text(_MULTI_MESH_PREFAB, encoding="utf-8")
        guid = "lodpackguid"
        gi = self._make_guid_index(guid, str(prefab))

        with _scene_ctx(fbx_bounding_boxes={}):
            result = sc._compute_mesh_size_from_fbx_bbox(
                unity_scale=(1.0, 1.0, 1.0),
                mesh_guid=guid,
                guid_index=gi,
                mesh_file_id="222",
            )
        assert result is not None
        size, initial = result
        assert initial == (8.0, 10.0, 12.0)
        assert abs(size[0] - 8.0 * config.STUDS_PER_METER) < 0.01


class TestMeshVerticalOffsetSubMesh:
    """Test per-sub-mesh vertical offset selection."""

    def test_submesh_name_fallback(self):
        """When fileID index is out of bounds, fall back to name matching."""
        import converter.scene_converter as sc
        from core.unity_types import GuidIndex, GuidEntry
        from pathlib import Path

        old_cache = sc._mesh_vertical_offset_cache
        try:
            sc._mesh_vertical_offset_cache = {}
            with _scene_ctx(mesh_hierarchies={
                "Assets/door.fbx": [
                    {"name": "frame_col", "position": [0, 3.22, 0], "size": [7, 6, 1]},
                    {"name": "base", "position": [0, 0.14, 0], "size": [5, 0.15, 2]},
                    {"name": "door", "position": [0, 2.66, 0], "size": [5, 5, 0.7]},
                ]
            }):
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

    def test_collider_center_offset_rotated_with_part(self):
        """Codex round-3: ``BoxCollider.m_Center`` is in the part's
        local space. The previous code added the raw component values
        to the part's CFrame position, which mis-placed the collider
        on any rotated part. A part rotated 90° around Y with a
        ``m_Center = (1, 0, 0)`` should shift the position along
        the part's local +X axis (which under that rotation lands on
        world -Z, not world +X).
        """
        from converter.scene_converter import _process_components
        from core.roblox_types import RbxPart, RbxCFrame

        class FakeBoxCollider:
            component_type = "BoxCollider"
            properties = {
                "m_IsTrigger": 0,
                "m_Size": {"x": 1, "y": 1, "z": 1},
                "m_Center": {"x": 1, "y": 0, "z": 0},
            }

        class FakeNode:
            name = "RotatedBox"
            components = [FakeBoxCollider()]
            mesh_guid = None

        # 90° CCW rotation around Y: local +X → world -Z.
        # Row-major rotation matrix:
        #   [[ 0, 0,  1],
        #    [ 0, 1,  0],
        #    [-1, 0,  0]]
        # so rotating local (1, 0, 0) yields world (0, 0, -1).
        part = RbxPart(
            name="RotatedBox",
            size=(3.571, 3.571, 3.571),
            cframe=RbxCFrame(
                x=10.0, y=20.0, z=30.0,
                r00=0.0, r01=0.0, r02=1.0,
                r10=0.0, r11=1.0, r12=0.0,
                r20=-1.0, r21=0.0, r22=0.0,
            ),
        )
        _process_components(FakeNode(), part)

        from config import STUDS_PER_METER
        # m_Center=(1,0,0) in meters → (STUDS_PER_METER, 0, 0) studs
        # rotated by the matrix above → (0, 0, -STUDS_PER_METER).
        # Final position = original + rotated offset.
        assert abs(part.cframe.x - 10.0) < 1e-4, (
            f"X should be unchanged by local +X offset on this rotation, "
            f"got {part.cframe.x}"
        )
        assert abs(part.cframe.y - 20.0) < 1e-4, (
            f"Y should be unchanged, got {part.cframe.y}"
        )
        assert abs(part.cframe.z - (30.0 - STUDS_PER_METER)) < 1e-4, (
            f"Local +X offset on Y=90° part must land on world -Z. "
            f"Got Z={part.cframe.z}, expected {30.0 - STUDS_PER_METER}"
        )

    def test_invisible_collider_stays_invisible(self):
        """Codex round-1 [P2]: an invisible collider proxy (no renderer
        + non-trigger collider) must keep ``transparency = 1.0``. Round-0
        fix gated ``can_collide`` correctly but skipped the transparency
        write, leaving converted dock floors / collision proxies
        rendering as gray boxes overlaying real geometry.
        """
        from converter.scene_converter import _convert_node
        from core.unity_types import SceneNode

        class FakeBoxCollider:
            component_type = "BoxCollider"
            properties = {
                "m_IsTrigger": 0,
                "m_Size": {"x": 15, "y": 0.25, "z": 2.25},
                "m_Center": {"x": 0, "y": 0, "z": 0},
            }

        node = SceneNode(
            name="Collider",
            file_id="1",
            active=True,
            layer=0,
            tag="Untagged",
            components=[FakeBoxCollider()],
            parent_file_id=None,
            position=(0.0, 0.0, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
        )

        with _scene_ctx():
            part = _convert_node(node, None, {}, {})

        assert part is not None
        assert part.transparency == 1.0, (
            "Collider proxy with no renderer must be invisible."
        )
        assert part.can_collide is True, (
            "Round-0 fix: collide must still be true."
        )

    def test_invisible_collider_not_dropped_as_marker(self):
        """A Part with no Renderer but a non-trigger Collider must keep
        ``CanCollide=True``. The "logic-only / marker" rule must not flip
        ``can_collide=False`` on it.

        Reproducer: SimpleFPS Pier's ``Collider`` GameObject is an
        invisible ``BoxCollider`` (m_IsTrigger=0) carrying the entire
        dock's collision; the visible ``plank``/``beam`` children are
        renderer-only. Pre-fix, this rule lumped any no-renderer Part
        as a marker and disabled collision — player fell through dock
        on respawn. The fix gates the rule on ``not has_collider``.
        """
        from converter.scene_converter import _convert_node
        from core.unity_types import SceneNode

        class FakeBoxCollider:
            component_type = "BoxCollider"
            properties = {
                "m_IsTrigger": 0,
                "m_Size": {"x": 15, "y": 0.25, "z": 2.25},
                "m_Center": {"x": 0, "y": 0, "z": 0},
            }

        node = SceneNode(
            name="Collider",
            file_id="1",
            active=True,
            layer=0,
            tag="Untagged",
            components=[FakeBoxCollider()],
            parent_file_id=None,
            position=(0.0, 0.0, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
        )

        with _scene_ctx():
            part = _convert_node(node, None, {}, {})

        assert part is not None
        assert part.can_collide is True, (
            "Invisible BoxCollider must remain colliding — "
            "without this, dock floors / collision proxies fall through."
        )

    def test_no_collider_no_renderer_part_treated_as_marker(self):
        """A Part with neither Renderer nor Collider is a true Unity
        container/marker (logic-only GameObject for child scripts) —
        ``can_collide=False`` and ``transparency=1.0`` are correct here.
        Pin the discriminator so future refactors don't drop the marker
        behavior entirely.
        """
        from converter.scene_converter import _convert_node
        from core.unity_types import SceneNode

        node = SceneNode(
            name="GameLogicContainer",
            file_id="1",
            active=True,
            layer=0,
            tag="Untagged",
            components=[],
            parent_file_id=None,
            position=(0.0, 0.0, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
        )

        with _scene_ctx():
            part = _convert_node(node, None, {}, {})

        assert part is not None
        assert part.can_collide is False
        assert part.transparency == 1.0

    def test_trigger_then_physical_takes_trigger_behavior(self):
        """Trigger first then physical: same outcome — trigger wins.

        Order-independent. ``_process_components`` pre-scans for any
        trigger collider on the node before iterating; if present, the
        physical branch skips its own ``can_collide=True`` assignment.
        Without the pre-scan the policy would be last-one-wins (which
        Unity component order is YAML-document order, not deterministic
        across prefab variants — that flaky behavior used to be the
        observed "feature" before this fix).
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

        assert part.can_collide is False, "Trigger semantics dominate regardless of YAML order"
        assert getattr(part, 'can_query', True) is True, "Trigger should set CanQuery=True"


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


class TestMultiSubMeshPositionGate:
    """``_convert_prefab_node`` historically replaced a child's local
    position with the FBX-internal sub-mesh pivot whenever the child
    referenced a multi-sub-mesh FBX. That destroyed authoritative
    ``m_LocalPosition`` values from real Unity prefabs that use the
    FBX only as a mesh source (assigning individual sub-meshes to
    manually-positioned GameObjects via MeshFilter).

    Reproducer: SimpleFPS Turret.prefab has Base.localPos.y=1.45m
    and Weapon.localPos.y=0.93m (Weapon is a child of Base), all
    referencing turret_01.fbx by ``m_Mesh`` fileID. Pre-fix the Y
    offsets collapsed to 0 and the converted turrets rendered as
    flat puddles of meshes piled on the ground.

    Fix: gate the substitution on ``local_pos ≈ (0, 0, 0)``. Only
    substitute when the prefab carries no positioning of its own
    (FBX-as-prefab wrapper pattern) — otherwise trust the prefab.
    """

    @staticmethod
    def _fake_guid_index_with_fbx(fbx_path):
        """Build a minimal GuidIndex-like object that resolves a single
        mesh GUID to a fake FBX path."""
        class _Idx:
            def resolve(self, guid):
                if guid == "turret-fbx-guid":
                    return fbx_path
                return None
            def resolve_relative(self, guid):
                if guid == "turret-fbx-guid":
                    return fbx_path
                return None
        return _Idx()

    def test_authoritative_local_pos_preserved(self, tmp_path):
        """A child PrefabNode with non-zero ``m_LocalPosition`` keeps
        its prefab-defined position even when its mesh is one of a
        multi-sub-mesh FBX. Pin the Y offset that the round-5 fix
        restored.
        """
        from converter.scene_converter import _convert_prefab_node
        from core.unity_types import PrefabNode

        fbx_path = tmp_path / "turret_01.fbx"
        fbx_path.write_bytes(b"")  # exists but empty — path is the marker

        idx = self._fake_guid_index_with_fbx(fbx_path)
        # PrefabNode for ``Base``: real Unity-prefab position 1.45m up.
        node = PrefabNode(
            name="Base",
            file_id="1",
            active=True,
            position=(0.0, 1.45, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
            mesh_guid="turret-fbx-guid",
            mesh_file_id="4300002",
        )

        # mesh_hierarchies says the FBX-internal sub-mesh sits at ~0.
        # The pre-fix code would have used (0, -0.0196, 0) instead of
        # (0, 1.45, 0). Set up the context with that hierarchy data;
        # ``_get_multi_sub_meshes`` looks up by ``str(asset_path)`` so
        # key must match the fake guid_index's resolve() return value.
        mesh_hierarchies = {
            str(fbx_path): [
                {"position": [0.0, -0.0196, 0.0], "fileID": "4300002", "name": "TurretBase"},
                {"position": [0.0, 0.0, 0.0], "fileID": "4300000", "name": "Tower"},
            ],
        }

        with _scene_ctx(mesh_hierarchies=mesh_hierarchies):
            # Convert with a parent at world origin.
            part = _convert_prefab_node(
                node, idx, {}, {},
                parent_pos=[0.0, 0.0, 0.0],
                parent_rot=[0.0, 0.0, 0.0, 1.0],
                parent_scl=[1.0, 1.0, 1.0],
            )

        assert part is not None
        # World Y must reflect the prefab's 1.45m × STUDS_PER_METER.
        # Allow ±0.1 stud slack (mesh vertical offset correction may add
        # small adjustments for Roblox center-of-bbox positioning).
        from config import STUDS_PER_METER
        expected_y = 1.45 * STUDS_PER_METER
        assert abs(part.cframe.y - expected_y) < 0.5, (
            f"Expected Y≈{expected_y:.2f} (Unity 1.45m), got {part.cframe.y:.2f}. "
            f"Pre-fix bug: prefab Y was overwritten with FBX sub-mesh pivot ~0."
        )

    def test_zero_local_pos_still_uses_submesh_position(self, tmp_path):
        """The FBX-as-prefab pattern (prefab is just an FBX wrapper
        with internal sub-mesh positioning) still needs the
        substitution. When the prefab's own local pos is ~0, use the
        FBX-internal sub-mesh pivot.
        """
        from converter.scene_converter import _convert_prefab_node
        from core.unity_types import PrefabNode

        fbx_path = tmp_path / "multi.fbx"
        fbx_path.write_bytes(b"")

        idx = self._fake_guid_index_with_fbx(fbx_path)

        # A real FBX wrapper-prefab: prefab position is 0, FBX-internal
        # sub-mesh position carries the offset.
        node = PrefabNode(
            name="SubA",
            file_id="1",
            active=True,
            position=(0.0, 0.0, 0.0),  # wrapper carries no offset
            rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
            mesh_guid="turret-fbx-guid",
            mesh_file_id="4300002",
        )

        # FBX-internal positions: SubA sits 2m up from the FBX origin.
        # Need 2+ entries to satisfy ``_get_multi_sub_meshes``'s
        # "single-mesh FBX" filter. mesh_file_id 4300002 maps to index 1
        # via Unity's ``(fid - 4300000) // 2`` rule, so SubA must be at
        # index 1 to be picked by ``_resolve_sub_mesh``.
        mesh_hierarchies = {
            str(fbx_path): [
                {"position": [0.0, 0.0, 0.0], "fileID": "4300000", "name": "SubB"},
                {"position": [0.0, 2.0, 0.0], "fileID": "4300002", "name": "SubA"},
            ],
        }

        with _scene_ctx(mesh_hierarchies=mesh_hierarchies):
            part = _convert_prefab_node(
                node, idx, {}, {},
                parent_pos=[0.0, 0.0, 0.0],
                parent_rot=[0.0, 0.0, 0.0, 1.0],
                parent_scl=[1.0, 1.0, 1.0],
            )

        assert part is not None
        # World Y should reflect the FBX-internal 2m sub-mesh pivot
        # scaled by STUDS_PER_METER (substitution feeds through
        # ``unity_to_roblox_pos``). Pin Y ≈ 7.14 studs, NOT 0.
        from config import STUDS_PER_METER
        expected_y = 2.0 * STUDS_PER_METER
        assert abs(part.cframe.y - expected_y) < 0.5, (
            f"Expected Y≈{expected_y:.2f} (FBX-internal sub-mesh pivot), "
            f"got {part.cframe.y:.2f}. Substitution must still fire when "
            f"prefab local pos is ~0."
        )


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


class TestFindCameraWorldTransform:
    """``_find_camera`` used to read ``node.position`` / ``node.rotation``
    directly — those are LOCAL transforms, so a camera parented under a
    rotated/translated rig (typical FPS rig setup) landed at the wrong
    world CFrame. Pin the contract that ancestor transforms compose.
    """

    def test_camera_under_translated_parent_uses_world_position(self):
        from converter.scene_converter import _find_camera
        from core.unity_types import SceneNode, ParsedScene
        import config

        class FakeCamera:
            component_type = "Camera"
            properties = {}

        parent = SceneNode(
            name="Rig",
            file_id="parent",
            active=True,
            layer=0,
            tag="Untagged",
            components=[],
            parent_file_id=None,
            position=(10.0, 0.0, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
        )
        camera = SceneNode(
            name="Camera",
            file_id="cam",
            active=True,
            layer=0,
            tag="Untagged",
            components=[FakeCamera()],
            parent_file_id="parent",
            position=(0.0, 0.0, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
        )
        scene = ParsedScene(
            scene_path=None,
            all_nodes={"parent": parent, "cam": camera},
            roots=[parent],
        )
        cfg = _find_camera(scene)
        assert cfg is not None
        # Parent at world x=10 (Unity), camera at local origin. Roblox X
        # preserves the value but the world composition matters: a
        # local-only read would put the camera at world x=0.
        expected_x = 10.0 * config.STUDS_PER_METER
        assert abs(cfg.cframe.x - expected_x) < 1e-3, (
            f"Camera world X should reflect parent translation. "
            f"Got {cfg.cframe.x}, expected {expected_x}"
        )


class TestMainCameraRig:
    """The Unity main-camera GameObject becomes a Model tagged with the
    ``_MainCameraRig`` attribute, so the auto-injected CameraRigFollower
    can pivot it onto ``workspace.CurrentCamera`` each frame and every
    camera-child object (weapon slots, viewmodels) rides the view.
    """

    def _camera_node(self, tag="MainCamera"):
        from core.unity_types import SceneNode

        class FakeCamera:
            component_type = "Camera"
            properties: dict = {}

        # A child makes the node convert to a Model (the rig container).
        child = SceneNode(
            name="WeaponSlot", file_id="2", active=True, layer=0,
            tag="Untagged", components=[], parent_file_id="1",
            position=(0.4, -0.64, 0.6), rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
        )
        return SceneNode(
            name="Main Camera", file_id="1", active=True, layer=0,
            tag=tag, components=[FakeCamera()], parent_file_id=None,
            children=[child],
            position=(0.0, 0.0, 0.0), rotation=(0.0, 0.0, 0.0, 1.0),
            scale=(1.0, 1.0, 1.0),
        )

    def test_main_camera_tagged_as_rig(self):
        from converter.scene_converter import _convert_node

        with _scene_ctx():
            part = _convert_node(self._camera_node(), None, {}, {})

        assert part is not None
        assert part.class_name == "Model", (
            "A camera GameObject with children must become a Model."
        )
        assert part.attributes.get("_MainCameraRig") is True

    def test_secondary_camera_not_tagged(self):
        """A camera not tagged ``MainCamera`` (e.g. a minimap camera)
        must NOT be marked as the rig — only one rig per scene."""
        from converter.scene_converter import _convert_node

        with _scene_ctx():
            part = _convert_node(
                self._camera_node(tag="Untagged"), None, {}, {}
            )

        assert part is not None
        assert "_MainCameraRig" not in part.attributes

    def _prefab_camera_node(self, tag="MainCamera"):
        """The main camera is usually authored inside a prefab (e.g.
        SimpleFPS Player.prefab), so it converts via _convert_prefab_node
        from a PrefabNode, not a SceneNode."""
        from core.unity_types import PrefabNode

        child = PrefabNode(
            name="WeaponSlot", file_id="2", active=True, tag="Untagged",
            parent_file_id="1", position=(0.4, -0.64, 0.6),
            rotation=(0.0, 0.0, 0.0, 1.0), scale=(1.0, 1.0, 1.0),
        )
        return PrefabNode(
            name="Main Camera", file_id="1", active=True, tag=tag,
            children=[child], position=(0.0, 0.0, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0), scale=(1.0, 1.0, 1.0),
        )

    def test_prefab_main_camera_tagged_as_rig(self):
        """Regression: the camera is prefab-instanced, so the rig marker
        must be applied on the _convert_prefab_node path too — tagging
        only _convert_node silently shipped no rig for real projects."""
        from converter.scene_converter import _convert_prefab_node

        with _scene_ctx():
            part = _convert_prefab_node(self._prefab_camera_node(), None, {}, {})

        assert part is not None
        assert part.class_name == "Model"
        assert part.attributes.get("_MainCameraRig") is True

    def test_prefab_secondary_camera_not_tagged(self):
        from converter.scene_converter import _convert_prefab_node

        with _scene_ctx():
            part = _convert_prefab_node(
                self._prefab_camera_node(tag="Untagged"), None, {}, {}
            )

        assert part is not None
        assert "_MainCameraRig" not in part.attributes


class TestTriggerVsMeshSizeAuthority:
    """When a GameObject carries a visible mesh AND a trigger collider, the
    Part Size and Transparency must come from the mesh, not the trigger.
    The trigger's intended volume is recorded as ``_Trigger*`` attributes so
    scripts can still do OverlapParams-style proximity detection.

    Concrete case this guards against: SimpleFPS landmines had MeshFilter +
    BoxCollider (~0.32 x 0.10 x 0.35 m) on the same GameObject as a
    SphereCollider trigger with radius 2 m. The earlier policy grew the
    Part Size to the 4 m sphere bounding box and forced Transparency=1,
    producing a 14.28-stud invisible cube instead of a small visible mine.
    """

    @staticmethod
    def _fake_comp(component_type, properties):
        c = type("FakeComp", (), {})()
        c.component_type = component_type
        c.properties = properties
        return c

    @staticmethod
    def _fake_node(components, name="Mine", mesh_guid=""):
        n = type("FakeNode", (), {})()
        n.name = name
        n.components = list(components)
        n.mesh_guid = mesh_guid
        return n

    def test_visible_mesh_plus_trigger_keeps_mesh_size(self):
        from converter.scene_converter import _process_components
        from core.roblox_types import RbxPart

        # ~SimpleFPS Mine geometry: 0.32 x 0.10 x 0.35 m mesh -> studs.
        # ``part.size`` is pre-populated by the mesh-sizing path before
        # ``_process_components`` runs in production; mirror that here.
        mesh_size = (1.153, 0.348, 1.250)
        part = RbxPart(name="Mine")
        part.size = mesh_size

        comps = [
            self._fake_comp("MeshFilter", {}),
            self._fake_comp("MeshRenderer", {}),
            self._fake_comp("BoxCollider",
                            {"m_Size": {"x": 0.323, "y": 0.098, "z": 0.350},
                             "m_IsTrigger": 0}),
            self._fake_comp("SphereCollider",
                            {"m_Radius": 2.0, "m_IsTrigger": 1}),
        ]
        node = self._fake_node(comps, mesh_guid="abcd"*8)

        _process_components(node, part)

        # The mesh wins for visible Size — the trigger sphere's ~14-stud
        # bounding box must NOT have grown the Part.
        assert part.size[0] < 5.0, f"Part Size X clobbered by trigger: {part.size}"
        assert part.size[1] < 5.0, f"Part Size Y clobbered by trigger: {part.size}"
        assert part.size[2] < 5.0, f"Part Size Z clobbered by trigger: {part.size}"
        # The trigger still imposes detection semantics on the visible Part:
        # CanQuery on, CanCollide off (trigger dominates per the existing
        # ``node_has_trigger`` policy).
        assert part.can_query is True
        assert part.can_collide is False
        # Trigger does NOT hide the visible mesh.
        assert part.transparency != 1.0, "Trigger forced Transparency=1 over a visible mesh"
        # Trigger volume preserved as attributes for OverlapParams-based
        # script detection.
        assert part.attributes is not None
        assert part.attributes.get("_TriggerShape") == "Sphere"
        # 2 m radius -> 4 m diameter -> ~14.28 studs on each axis.
        for axis in ("_TriggerSizeX", "_TriggerSizeY", "_TriggerSizeZ"):
            assert part.attributes.get(axis, 0) > 10.0, (
                f"{axis} not recorded ({part.attributes.get(axis)})"
            )
        # And the trigger is also emitted as a transparent CHILD Part so
        # ``Touched``-based detection still fires at the original radius
        # (without it, Touched on the small visible mesh only fires on
        # direct contact — breaks Unity's "walk near the door" pattern).
        trigger_children = [c for c in part.children if c.name == "TriggerZone"]
        assert len(trigger_children) == 1, (
            f"expected 1 TriggerZone child, got {len(trigger_children)}"
        )
        tz = trigger_children[0]
        assert tz.transparency == 1.0
        assert tz.can_collide is False
        assert tz.can_query is True
        assert tz.can_touch is True
        # Sized to the trigger sphere (~14.28 studs).
        assert tz.size[0] > 10.0 and tz.size[1] > 10.0 and tz.size[2] > 10.0, (
            f"TriggerZone size {tz.size} too small — expected ~14.28 stud sphere"
        )
        # Ball shape for SphereCollider.
        assert tz.shape == 0

    def test_trigger_alone_still_sizes_part(self):
        """Backward compatibility: when the trigger is the ONLY sized
        component on the node (no mesh, no non-trigger collider), it
        must still drive Part Size and Transparency=1 — the existing
        proximity-zone-on-a-marker-GameObject pattern keeps working."""
        from converter.scene_converter import _process_components
        from core.roblox_types import RbxPart

        part = RbxPart(name="ProximityZone")
        # Default Part size — pre-loop nothing else set it.
        part.size = (0.05, 0.05, 0.05)

        comps = [
            self._fake_comp("SphereCollider",
                            {"m_Radius": 2.0, "m_IsTrigger": 1}),
        ]
        node = self._fake_node(comps, name="ProximityZone")

        _process_components(node, part)

        # Trigger sized the part — 4 m diameter ~= 14.28 studs.
        assert max(part.size) > 10.0, f"Trigger-only didn't size: {part.size}"
        assert part.transparency == 1.0
        assert part.can_query is True
        assert part.can_collide is False
        # When the trigger owns size, the volume is the Part itself —
        # no need to also stash it on attributes.
        attrs = part.attributes or {}
        assert "_TriggerSizeX" not in attrs


class TestPrefabInstanceScaleNotDoubled:
    """A PrefabInstance must size its Part to the effective instance scale,
    not ``prefab_root.scale × instance_scale`` (which double-counts the
    prefab's localScale).

    Unity ``m_LocalScale.*`` modifications on a ``PrefabInstance`` REPLACE
    the prefab root's localScale on that instance — they don't compose
    multiplicatively. So when ``template.root.scale = (S, S, S)`` and the
    scene either omits any override (instance inherits S) or sets
    ``m_LocalScale.x = S`` (override matches), the effective instance
    scale is ``S``, not ``S × S``.

    Concrete case: SimpleFPS Mine — prefab root localScale 2.5 + an
    ``m_LocalScale.x = 2.4999995`` PrefabInstance override produced a Part
    sized to ``2.5 × 2.5 = 6.25 × mesh_AABB × STUDS_PER_METER`` = ~7.21
    studs, instead of the expected ~2.88 studs.
    """

    def _build_template(self, root_scale, mesh_size_meters):
        """Build a minimal prefab template with a root mesh node."""
        from converter.scene_converter import RbxPart  # noqa: F401 — module import
        # Lightweight fake matching the surface used by _convert_prefab_node:
        # template has .root with .scale, .position, .rotation, .name,
        # .mesh_guid, .mesh_file_id, .children, .components; template has .name.
        root = type("FakeNode", (), {})()
        root.name = "FakeMesh"
        root.scale = root_scale
        root.position = (0.0, 0.0, 0.0)
        root.rotation = (0.0, 0.0, 0.0, 1.0)
        root.mesh_guid = "deadbeef" * 4
        root.mesh_file_id = "0"
        root.children = []
        root.components = []
        template = type("FakeTemplate", (), {})()
        template.name = "Mine"
        template.root = root
        return template

    def test_instance_with_override_matching_template_keeps_single_scale(self, tmp_path, monkeypatch):
        """Prefab template root scale 2.5 + instance override ``m_LocalScale.x = 2.5``:
        the resulting Part size must be ``~2.5 × mesh_aabb × STUDS_PER_METER``,
        NOT ``~6.25 × mesh_aabb × STUDS_PER_METER``.
        """
        from converter import scene_converter as sc
        from converter.scene_converter import SceneConversionContext
        from core.unity_types import GuidIndex, GuidEntry

        mesh_asset = tmp_path / "embedded_mine.prefab"
        mesh_asset.write_text("dummy")
        mesh_guid = "deadbeef" * 4
        prefab_guid = "feedface" * 4
        guid_index = GuidIndex(project_root=tmp_path)
        guid_index.guid_to_entry[mesh_guid] = GuidEntry(
            guid=mesh_guid,
            asset_path=mesh_asset.resolve(),
            relative_path=mesh_asset.relative_to(tmp_path),
            kind="prefab",
        )
        # The PrefabInstance resolver also calls ``guid_index.resolve`` on
        # the prefab source. Point that at the same on-disk file so the
        # resolver can find a template by stem.
        guid_index.guid_to_entry[prefab_guid] = GuidEntry(
            guid=prefab_guid,
            asset_path=mesh_asset.resolve(),
            relative_path=mesh_asset.relative_to(tmp_path),
            kind="prefab",
        )

        # Stub the embedded-AABB reader so we don't need a real Unity YAML.
        monkeypatch.setattr(
            sc, "_read_embedded_mesh_aabb",
            lambda asset_path, mesh_file_id: (0.32, 0.10, 0.35),
        )

        template = self._build_template(
            root_scale=(2.5, 2.5, 2.5),
            mesh_size_meters=(0.32, 0.10, 0.35),
        )
        prefab_lib = type("FakeLib", (), {})()
        prefab_lib.by_name = {mesh_asset.stem: template}

        # Build a PrefabInstance with an m_LocalScale.x = 2.5 override.
        pi = type("FakePI", (), {})()
        pi.source_prefab_guid = prefab_guid
        pi.transform_parent_file_id = ""
        pi.removed_components = []
        pi.modifications = [
            {"propertyPath": "m_RootOrder",
             "target": {"fileID": "1"}, "value": "0"},
            {"propertyPath": "m_LocalPosition.x",
             "target": {"fileID": "1"}, "value": "0"},
            {"propertyPath": "m_LocalScale.x",
             "target": {"fileID": "1"}, "value": "2.5"},
        ]

        # An active SceneConversionContext is required by _ctx() helpers.
        ctx = SceneConversionContext()
        sc._current_ctx = ctx
        try:
            parts = sc._convert_prefab_instance(
                pi, prefab_lib, guid_index=guid_index,
                material_mappings={}, uploaded_assets={},
            )
        finally:
            sc._current_ctx = None
        assert parts, "Resolver returned no parts"
        part = parts[0]

        # Effective scale should be 2.5 (single application), not 6.25.
        # X-extent: 0.32 m × 2.5 × STUDS_PER_METER (3.571) ≈ 2.857 studs.
        # The bug it guards against produces ~7.14 studs (~6.25× mesh) so
        # any value under 5 studs proves the prefab scale isn't double-counted.
        assert part.size[0] < 5.0, (
            f"Part X size {part.size[0]:.3f} > 5: prefab root scale "
            f"double-counted (expected ~2.86 from 0.32 m × 2.5 × STUDS_PER_METER)"
        )
        # Sanity: not collapsed to zero either.
        assert part.size[0] > 1.5, (
            f"Part X size {part.size[0]:.3f} < 1.5: scale lost entirely "
            f"(expected ~2.86)"
        )


class TestSyntheticEmbeddedKeySizingParity:
    """When an embedded mesh is uploaded + resolved via Open Cloud,
    ``_compute_mesh_size`` finds it in ``mesh_hierarchies`` BEFORE the
    embedded-AABB fallback fires. Both paths must produce the same
    Size for the same source mesh -- otherwise wiring the synthetic
    key into ``mesh_hierarchies`` is a silent unit-system change.

    Concrete regression this guards against: PR #121 made
    ``_get_fbx_import_scale`` return ``0.01`` for ``.prefab`` paths,
    so the resolved-sub-mesh formula computed Size = unity_scale *
    native_meters * 0.01 * STUDS_PER_METER -- 100x too small. The
    rbxlx writer's 0.05 floor then absorbed the underflow and Mines
    went invisible. Asserting parity here would have caught it
    without any Studio playtest.
    """

    def test_resolved_and_fallback_paths_produce_same_size(
        self, tmp_path, monkeypatch,
    ) -> None:
        from converter import scene_converter as sc
        from converter.scene_converter import SceneConversionContext
        from core.unity_types import GuidIndex, GuidEntry

        # Make ``_get_fbx_import_scale`` early-return 1.0 for .prefab
        # paths in scope. Mocking the AABB reader removes the need for
        # a real Unity YAML.
        mesh_guid = "feedface" * 4
        asset = tmp_path / "MyMesh.prefab"
        asset.write_text("dummy")
        guid_index = GuidIndex(project_root=tmp_path)
        guid_index.guid_to_entry[mesh_guid] = GuidEntry(
            guid=mesh_guid,
            asset_path=asset.resolve(),
            relative_path=asset.relative_to(tmp_path),
            kind="prefab",
        )
        # Native size from the embedded AABB (in metres):
        native_metres = (0.32, 0.10, 0.35)
        monkeypatch.setattr(
            "converter.scene_converter._read_embedded_mesh_aabb",
            lambda asset_path, mesh_file_id: native_metres,
        )

        ctx = SceneConversionContext()
        sc._current_ctx = ctx
        try:
            # Path 1: fallback (no mesh_hierarchies entry for the
            # synthetic key) -> embedded-AABB branch.
            unity_scale = (2.5, 2.5, 2.5)
            fallback_result = sc._compute_mesh_size(
                unity_scale, mesh_guid, guid_index,
                mesh_native_sizes={},
                mesh_file_id="4322892",
            )
            assert fallback_result is not None
            fallback_size, _ = fallback_result

            # Path 2: resolved (synthetic key present in mesh_hierarchies).
            # The resolver gives back ``size`` in metres because our
            # adapter normalises before populating the table.
            ctx.mesh_hierarchies = {
                "MyMesh.prefab#4322892": [
                    {"name": "Embedded", "meshId": "rbxassetid://1",
                     "size": list(native_metres), "position": [0, 0, 0]},
                ],
            }
            resolved_result = sc._compute_mesh_size(
                unity_scale, mesh_guid, guid_index,
                mesh_native_sizes={},
                mesh_file_id="4322892",
            )
            assert resolved_result is not None
            resolved_size, _ = resolved_result
        finally:
            sc._current_ctx = None

        # Both paths must agree within 1% (we tolerate float jitter
        # in the import_scale/unit_ratio chain, not a 100x mismatch).
        for axis in range(3):
            a, b = fallback_size[axis], resolved_size[axis]
            assert abs(a - b) / max(abs(a), 1e-6) < 0.01, (
                f"Size axis {axis} disagrees between fallback and resolved "
                f"paths: fallback={a:.4f} resolved={b:.4f} -- the embedded "
                f"mesh would render at the wrong Size after upload."
            )


class TestPickupPayloadStaysRaw:
    """ItemType and the serialized ``itemName`` are GAMEPLAY PAYLOADS, not
    flag stems: pickups forward them raw to ``GetItem`` / ``pickup_runtime``
    dispatch. They must NOT be sanitized at the source — sanitizing only the
    derived ``"has" .. name`` flag (at the runtime concat) keeps the raw value
    intact for gameplay while the cross-script flag stays ``[%w_]``-valid.
    """

    def test_pickup_itemtype_with_spaces_stays_raw(self) -> None:
        from converter.scene_converter import _apply_gameplay_attributes
        from core.roblox_types import RbxPart
        part = RbxPart(name="Red Key Pickup")
        _apply_gameplay_attributes(part, "Red Key Pickup")
        assert part.attributes["IsPickup"] is True
        # "Red Key Pickup".replace("Pickup","").strip() == "Red Key" — RAW
        # (the GetItem dispatch payload must keep the original label).
        assert part.attributes["ItemType"] == "Red Key"

    def test_pickup_itemtype_with_hyphen_stays_raw(self) -> None:
        from converter.scene_converter import _apply_gameplay_attributes
        from core.roblox_types import RbxPart
        part = RbxPart(name="Health-Pack Pickup")
        _apply_gameplay_attributes(part, "Health-Pack Pickup")
        assert part.attributes["ItemType"] == "Health-Pack"

    def test_clean_pickup_itemtype_is_byte_identical(self) -> None:
        from converter.scene_converter import _apply_gameplay_attributes
        from core.roblox_types import RbxPart
        part = RbxPart(name="KeyPickup")
        _apply_gameplay_attributes(part, "KeyPickup")
        assert part.attributes["ItemType"] == "Key"

    def test_empty_itemtype_falls_back_to_generic(self) -> None:
        from converter.scene_converter import _apply_gameplay_attributes
        from core.roblox_types import RbxPart
        # Bare "Pickup" → "" → 'Generic' (pre-existing fallback, unchanged).
        part = RbxPart(name="Pickup")
        _apply_gameplay_attributes(part, "Pickup")
        assert part.attributes["ItemType"] == "Generic"

    def test_serialized_itemname_stays_raw(self) -> None:
        import converter.scene_converter as sc
        from core.roblox_types import RbxPart
        part = RbxPart(name="Pickup")
        sc._extract_monobehaviour_attributes(
            {"itemName": "Red Key"}, part, guid_index=None
        )
        # RAW: forwarded to GetItem(itemName); only the derived flag is sanitized.
        assert part.attributes["itemName"] == "Red Key"

    def test_serialized_clean_itemname_is_byte_identical(self) -> None:
        import converter.scene_converter as sc
        from core.roblox_types import RbxPart
        part = RbxPart(name="Pickup")
        sc._extract_monobehaviour_attributes(
            {"itemName": "Key"}, part, guid_index=None
        )
        assert part.attributes["itemName"] == "Key"
