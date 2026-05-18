"""Fast cross-phase integration tests for the conversion pipeline.

The existing integration suites (``test_integration.py``,
``test_pipeline_e2e.py``) are all ``@pytest.mark.slow`` *and* skip unless a
large real Unity project is checked out under ``../test_projects/``. As a
result the fast CI suite (``pytest -m "not slow"``) has **zero** true
cross-phase coverage — every fast test exercises a single module in
isolation.

This file fixes that. Every test here:

* runs in the FAST suite (no ``@pytest.mark.slow``, never skips),
* uses only the small bundled fixtures in ``tests/fixtures/`` (or tiny
  synthetic data built inline), so it has no dependency on
  ``../test_projects/``, the network, Roblox Studio, the ``claude`` CLI,
  or ``luau-analyze``,
* chains at least TWO real pipeline modules together — the output of one
  phase is fed as the input to the next — and asserts on the seam.

Phase seams covered:

* parse  -> convert_scene                       (scene parser -> converter)
* convert_scene -> rbxlx_writer                 (converter -> XML writer)
* convert_scene -> rbxlx_writer -> rbxl_binary  (full file-format chain)
* parse  -> convert_scene (component handling)  (light / collider / camera)
* prefab_parser -> scene_converter helpers      (prefab phase)
"""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unity.scene_parser import parse_scene
from unity.prefab_parser import _parse_single_prefab
from converter.scene_converter import convert_scene
from roblox.rbxlx_writer import write_rbxlx
from roblox.rbxl_binary_writer import xml_to_binary, MAGIC, FORMAT_VERSION
import struct

FIXTURES = Path(__file__).parent / "fixtures"
SIMPLE_SCENE = FIXTURES / "simple_scene.yaml"
TERRAIN_SCENE = FIXTURES / "terrain_scene.yaml"
SIMPLE_PREFAB = FIXTURES / "simple_prefab.yaml"
VARIANT_PREFAB = FIXTURES / "variant_prefab.yaml"


def _all_parts(parts):
    """Flatten an RbxPart tree (parts + nested children) into a list."""
    out = []
    for p in parts:
        out.append(p)
        out.extend(_all_parts(p.children))
    return out


def _find_part(parts, name):
    for p in _all_parts(parts):
        if p.name == name:
            return p
    return None


# ---------------------------------------------------------------------------
# Seam 1: scene_parser -> scene_converter
# ---------------------------------------------------------------------------

class TestParseToConvertSeam:
    """parse_scene() output feeds convert_scene(): the SceneNode tree must
    survive into an RbxPlace with the expected parts and structure."""

    def test_every_gameobject_becomes_a_part(self):
        """Each top-level Unity GameObject in simple_scene must yield a
        Roblox part.

        Exercises the seam: the parser builds 5 SceneNodes; the converter
        must turn each root into a part (the converter additionally
        synthesises a floor + SpawnLocation, so we assert the source nodes
        are present rather than an exact count). Note ``Parent``/``Child``
        is a single-child hierarchy: the converter's single-child Model
        flattening collapses it to one part named ``Parent``, so ``Child``
        is intentionally absorbed — see test_single_child_hierarchy_flattened.
        """
        scene = parse_scene(SIMPLE_SCENE)
        assert len(scene.all_nodes) == 5  # parser sanity check

        place = convert_scene(scene)
        parts = _all_parts(place.workspace_parts)

        for unity_name in ("MainCamera", "Cube", "PointLight", "Parent"):
            assert _find_part(parts, unity_name) is not None, (
                f"GameObject '{unity_name}' did not survive into the RbxPlace"
            )

    def test_single_child_hierarchy_flattened(self):
        """``Parent`` has exactly one child (``Child``); the converter's
        single-child Model flattening collapses the pair into one leaf part
        named ``Parent``. Pin that parse->convert behavior: the surviving
        ``Parent`` part must carry no children."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        parent = _find_part(place.workspace_parts, "Parent")
        assert parent is not None
        assert parent.children == [], (
            "single-child hierarchy should flatten to one leaf part"
        )

    def test_box_collider_node_is_collidable_and_sized(self):
        """The Cube has a BoxCollider; after conversion it must be a
        collidable part whose size reflects the 1m collider scaled by
        STUDS_PER_METER (the parser->converter->component_converter seam)."""
        from config import STUDS_PER_METER

        place = convert_scene(parse_scene(SIMPLE_SCENE))
        cube = _find_part(place.workspace_parts, "Cube")
        assert cube is not None
        assert cube.can_collide is True, "BoxCollider node must be collidable"
        # 1m BoxCollider -> 1 * STUDS_PER_METER studs on each axis.
        assert abs(cube.size[0] - STUDS_PER_METER) < 0.01

    def test_camera_extracted_from_parsed_scene(self):
        """A Unity Camera component must produce an RbxCameraConfig on the
        place — the converter reads it off the parsed SceneNode."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        assert place.camera is not None
        assert place.camera.field_of_view == 60.0
        assert place.camera.near_clip == 0.3
        assert place.camera.far_clip == 1000.0

    def test_point_light_node_carries_converted_light(self):
        """The PointLight GameObject has a Unity Light (m_Type=2); after
        conversion the corresponding part must carry an RbxLight of type
        PointLight. Covers parser -> converter -> component_converter."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        light_part = _find_part(place.workspace_parts, "PointLight")
        assert light_part is not None
        assert len(light_part.lights) == 1
        assert light_part.lights[0].light_type == "PointLight"
        # Unity m_Color {r:1, g:0.95, b:0.84} must propagate through.
        r, g, b = light_part.lights[0].color
        assert abs(r - 1.0) < 1e-3 and abs(g - 0.95) < 1e-3 and abs(b - 0.84) < 1e-3

    def test_terrain_component_becomes_rbx_terrain(self):
        """A Unity Terrain component in terrain_scene must convert into an
        RbxTerrain on the place — the converter routes Terrain components
        away from the workspace-parts list into place.terrains."""
        place = convert_scene(parse_scene(TERRAIN_SCENE))
        assert len(place.terrains) == 1
        # Terrain part itself should not also be a leftover workspace part.
        assert _find_part(place.workspace_parts, "Terrain") is None


# ---------------------------------------------------------------------------
# Seam 2: scene_converter -> rbxlx_writer
# ---------------------------------------------------------------------------

class TestConvertToRbxlxSeam:
    """An RbxPlace produced by convert_scene() must serialise to a
    well-formed .rbxlx that contains the converted parts."""

    def test_converted_scene_writes_well_formed_xml(self, tmp_path):
        """parse -> convert -> write_rbxlx; the result must be parseable XML
        with a <roblox> root."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        out = tmp_path / "scene.rbxlx"
        write_rbxlx(place, out)

        assert out.exists()
        tree = ET.parse(out)
        assert tree.getroot().tag == "roblox"

    def test_rbxlx_contains_converted_gameobject_names(self, tmp_path):
        """The names of the Unity GameObjects must appear as Item Name
        properties in the serialised XML — proving the converter's output
        actually reached the writer."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        out = tmp_path / "scene.rbxlx"
        write_rbxlx(place, out)

        tree = ET.parse(out)
        names = {
            el.text
            for el in tree.iter("string")
            if el.get("name") == "Name"
        }
        for unity_name in ("Cube", "PointLight", "MainCamera"):
            assert unity_name in names, f"'{unity_name}' missing from rbxlx"

    def test_rbxlx_has_workspace_with_parts(self, tmp_path):
        """The serialised place must contain a Workspace Item, and the
        Part/MeshPart count must match the converted place's part count."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        out = tmp_path / "scene.rbxlx"
        result = write_rbxlx(place, out)

        tree = ET.parse(out)
        root = tree.getroot()
        workspace = next(
            (i for i in root.iter("Item") if i.get("class") == "Workspace"),
            None,
        )
        assert workspace is not None, "no Workspace Item in rbxlx"

        # parts_written reported by the writer must be > 0 and consistent
        # with the converted parts (which includes synthesised floor/spawn).
        assert result["parts_written"] >= len(_all_parts(place.workspace_parts))

    def test_light_part_emits_pointlight_item(self, tmp_path):
        """The converted PointLight part must serialise a nested PointLight
        Item — covers convert_scene (light on part) -> rbxlx_writer."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        out = tmp_path / "scene.rbxlx"
        write_rbxlx(place, out)

        tree = ET.parse(out)
        point_lights = [
            i for i in tree.iter("Item") if i.get("class") == "PointLight"
        ]
        assert len(point_lights) >= 1, "PointLight Item not emitted"

    def test_terrain_scene_serialises_with_terrain_item(self, tmp_path):
        """parse(terrain_scene) -> convert -> write must emit a Terrain
        Item in the workspace."""
        place = convert_scene(parse_scene(TERRAIN_SCENE))
        out = tmp_path / "terrain.rbxlx"
        write_rbxlx(place, out)

        tree = ET.parse(out)
        terrains = [i for i in tree.iter("Item") if i.get("class") == "Terrain"]
        assert len(terrains) == 1, "Terrain Item not emitted from terrain scene"


# ---------------------------------------------------------------------------
# Seam 3: scene_converter -> rbxlx_writer -> rbxl_binary_writer
# ---------------------------------------------------------------------------

class TestRbxlxToBinarySeam:
    """The full file-format chain: a converted scene written as .rbxlx must
    then convert into a structurally valid binary .rbxl."""

    def test_converted_scene_produces_valid_binary(self, tmp_path):
        """parse -> convert -> write_rbxlx -> xml_to_binary; the binary file
        must exist, be non-empty, and start with the Roblox magic header."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        rbxlx = tmp_path / "scene.rbxlx"
        write_rbxlx(place, rbxlx)

        rbxl = xml_to_binary(rbxlx, tmp_path / "scene.rbxl")
        assert rbxl.exists()
        data = rbxl.read_bytes()
        assert data.startswith(MAGIC), "binary .rbxl missing Roblox magic header"
        assert len(data) > len(MAGIC) + 16

    def test_binary_header_instance_count_matches_xml(self, tmp_path):
        """The binary header's instance count must equal the number of
        <Item> elements in the .rbxlx — proving the rbxlx -> binary seam
        carried every converted instance through."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        rbxlx = tmp_path / "scene.rbxlx"
        write_rbxlx(place, rbxlx)

        xml_item_count = sum(1 for _ in ET.parse(rbxlx).iter("Item"))

        rbxl = xml_to_binary(rbxlx, tmp_path / "scene.rbxl")
        data = rbxl.read_bytes()
        offset = len(MAGIC)
        version = struct.unpack_from("<H", data, offset)[0]
        instance_count = struct.unpack_from("<I", data, offset + 6)[0]

        assert version == FORMAT_VERSION
        assert instance_count == xml_item_count, (
            f"binary records {instance_count} instances, "
            f"rbxlx has {xml_item_count} <Item>s"
        )

    def test_binary_has_all_required_chunks(self, tmp_path):
        """A converted scene's binary form must contain every mandatory
        chunk marker (META/INST/PROP/PRNT/END)."""
        place = convert_scene(parse_scene(TERRAIN_SCENE))
        rbxlx = tmp_path / "terrain.rbxlx"
        write_rbxlx(place, rbxlx)

        rbxl = xml_to_binary(rbxlx, tmp_path / "terrain.rbxl")
        data = rbxl.read_bytes()
        for marker in (b"META", b"INST", b"PROP", b"PRNT", b"END\x00"):
            assert marker in data, f"chunk {marker!r} missing from binary"


# ---------------------------------------------------------------------------
# Seam 4: scene with components -> component conversion (parser -> converter)
# ---------------------------------------------------------------------------

class TestComponentConversionSeam:
    """A scene whose nodes carry Unity components must, after the full
    parse -> convert seam, surface those components in the RbxPlace."""

    def test_trigger_collider_scene_disables_collision(self, tmp_path):
        """Build a tiny synthetic scene with a trigger SphereCollider; after
        parse -> convert the part must be non-colliding (trigger semantics).
        This exercises parser -> scene_converter -> component_converter on
        inline data, independent of any fixture."""
        scene_yaml = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &10
GameObject:
  m_Name: TriggerZone
  m_IsActive: 1
--- !u!4 &11
Transform:
  m_GameObject: {fileID: 10}
  m_LocalPosition: {x: 0, y: 0, z: 0}
  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 0}
--- !u!135 &12
SphereCollider:
  m_GameObject: {fileID: 10}
  m_IsTrigger: 1
  m_Radius: 2
  m_Center: {x: 0, y: 0, z: 0}
"""
        scene_file = tmp_path / "trigger.unity"
        scene_file.write_text(scene_yaml)

        place = convert_scene(parse_scene(scene_file))
        zone = _find_part(place.workspace_parts, "TriggerZone")
        assert zone is not None
        assert zone.can_collide is False, "trigger collider must not collide"

    def test_spotlight_component_converts_with_angle(self, tmp_path):
        """A Unity Light with m_Type=0 (Spot) must convert into a SpotLight
        on the part, carrying the spot angle. Inline-built scene through the
        parser -> converter -> component_converter chain."""
        scene_yaml = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &20
GameObject:
  m_Name: Spotlight
  m_IsActive: 1
--- !u!4 &21
Transform:
  m_GameObject: {fileID: 20}
  m_LocalPosition: {x: 0, y: 5, z: 0}
  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 0}
--- !u!108 &22
Light:
  m_GameObject: {fileID: 20}
  m_Type: 0
  m_Color: {r: 1, g: 1, b: 1, a: 1}
  m_Intensity: 3
  m_Range: 20
  m_SpotAngle: 45
"""
        scene_file = tmp_path / "spot.unity"
        scene_file.write_text(scene_yaml)

        place = convert_scene(parse_scene(scene_file))
        part = _find_part(place.workspace_parts, "Spotlight")
        assert part is not None
        assert len(part.lights) == 1
        light = part.lights[0]
        assert light.light_type == "SpotLight"
        assert light.angle == 45.0
        assert light.brightness == 3.0

    def test_box_collider_scene_serialises_collidable_part(self, tmp_path):
        """Full chain on a fixture: the Cube's BoxCollider must end up as a
        part whose serialised Anchored/CanCollide reaches valid XML.
        parse -> convert -> rbxlx_writer."""
        place = convert_scene(parse_scene(SIMPLE_SCENE))
        out = tmp_path / "scene.rbxlx"
        write_rbxlx(place, out)

        tree = ET.parse(out)
        cube_item = None
        for item in tree.iter("Item"):
            if item.get("class") not in ("Part", "MeshPart"):
                continue
            name_el = item.find("Properties/string[@name='Name']")
            if name_el is not None and name_el.text == "Cube":
                cube_item = item
                break
        assert cube_item is not None, "Cube part not in rbxlx"
        props = cube_item.find("Properties")
        cancollide = props.find("bool[@name='CanCollide']")
        assert cancollide is not None and cancollide.text == "true"


# ---------------------------------------------------------------------------
# Seam 5: prefab_parser -> scene_converter
# ---------------------------------------------------------------------------

class TestPrefabParseSeam:
    """A prefab fixture parsed by prefab_parser must produce a PrefabTemplate
    whose nodes feed cleanly into scene_converter helpers."""

    def test_prefab_template_node_hierarchy_parsed(self):
        """simple_prefab has a root TestPrefab with a SubPart child; the
        parser must build that two-node hierarchy with the child parented,
        and the parsed hierarchy must convert through scene_converter so the
        child's world transform is composed against its parent's. Exercises
        the prefab_parser -> scene_converter._convert_prefab_node seam."""
        from converter.scene_converter import _convert_prefab_node
        from config import STUDS_PER_METER

        template = _parse_single_prefab(SIMPLE_PREFAB)
        assert template.name == "simple_prefab"
        assert len(template.all_nodes) == 2

        names = {n.name for n in template.all_nodes.values()}
        assert names == {"TestPrefab", "SubPart"}

        root = next(n for n in template.all_nodes.values() if n.name == "TestPrefab")
        sub = next(n for n in template.all_nodes.values() if n.name == "SubPart")
        # SubPart's Transform m_Father points at TestPrefab's GameObject:
        # parent_file_id must be the *actual* parent node's file_id, not
        # merely non-None.
        assert sub.parent_file_id == root.file_id
        # SubPart sits 1m above its parent (m_LocalPosition.y == 1).
        assert sub.position[1] == 1.0

        # Chain into the converter: SubPart composed against TestPrefab's
        # world transform (root at origin) must land 1m up in Roblox studs.
        root_part = _convert_prefab_node(
            root, None, {}, {},
            parent_pos=[0.0, 0.0, 0.0],
            parent_rot=[0.0, 0.0, 0.0, 1.0],
            parent_scl=[1.0, 1.0, 1.0],
        )
        assert root_part is not None and root_part.name == "TestPrefab"

        sub_part = _convert_prefab_node(
            sub, None, {}, {},
            parent_pos=[root_part.cframe.x, root_part.cframe.y, root_part.cframe.z],
            parent_rot=[0.0, 0.0, 0.0, 1.0],
            parent_scl=[1.0, 1.0, 1.0],
        )
        assert sub_part is not None and sub_part.name == "SubPart"
        # Parent at origin + 1m local Y -> 1 * STUDS_PER_METER studs.
        assert abs(sub_part.cframe.y - STUDS_PER_METER) < 0.5, (
            f"child world Y not composed from parent: got {sub_part.cframe.y}"
        )

    def test_prefab_node_converts_through_convert_prefab_node(self):
        """A PrefabNode from the parsed template must convert into an RbxPart
        via scene_converter._convert_prefab_node — the prefab-instantiation
        seam. The converter must preserve the prefab's local Y position
        (scaled into Roblox studs)."""
        from converter.scene_converter import _convert_prefab_node
        from config import STUDS_PER_METER

        template = _parse_single_prefab(SIMPLE_PREFAB)
        sub = next(n for n in template.all_nodes.values() if n.name == "SubPart")

        part = _convert_prefab_node(
            sub, None, {}, {},
            parent_pos=[0.0, 0.0, 0.0],
            parent_rot=[0.0, 0.0, 0.0, 1.0],
            parent_scl=[1.0, 1.0, 1.0],
        )
        assert part is not None
        assert part.name == "SubPart"
        # Unity m_LocalPosition.y == 1m -> 1 * STUDS_PER_METER studs.
        assert abs(part.cframe.y - STUDS_PER_METER) < 0.5, (
            f"prefab local Y not preserved: got {part.cframe.y}"
        )

    def test_variant_prefab_modifications_parsed(self):
        """variant_prefab is a PrefabInstance (variant); the parser flags it
        as a variant and captures its property modifications. The variant's
        base prefab GUID is not resolvable from the bundled fixtures, so the
        modifications are applied here onto a synthetic PrefabNode which is
        then chained into scene_converter._convert_prefab_node — the
        converted RbxPart must reflect the variant's overridden m_Name and
        m_LocalPosition. Exercises prefab_parser -> scene_converter."""
        from core.unity_types import PrefabNode
        from converter.scene_converter import _convert_prefab_node
        from config import STUDS_PER_METER

        template = _parse_single_prefab(VARIANT_PREFAB)
        assert template.is_variant is True
        assert template.source_prefab_guid == "aabbccdd11223344aabbccdd11223344"
        # The variant overrides m_Name and several transform components.
        mods = {
            m.get("propertyPath"): m.get("value")
            for m in template.variant_modifications
        }
        assert "m_Name" in mods
        assert "m_LocalPosition.x" in mods

        # Apply the parsed variant modifications onto a base node, mirroring
        # what the scene_converter variant-merge phase does.
        node = PrefabNode(
            name=str(mods["m_Name"]),
            file_id="100",
            active=True,
            position=(
                float(mods["m_LocalPosition.x"]),
                float(mods["m_LocalPosition.y"]),
                0.0,
            ),
        )

        # Chain into the converter: the variant's modified name and position
        # must survive into the converted RbxPart.
        part = _convert_prefab_node(
            node, None, {}, {},
            parent_pos=[0.0, 0.0, 0.0],
            parent_rot=[0.0, 0.0, 0.0, 1.0],
            parent_scl=[1.0, 1.0, 1.0],
        )
        assert part is not None
        assert part.name == "VariantPrefab", (
            "variant m_Name override did not reach the converted part"
        )
        # Unity m_LocalPosition.y == 10m -> 10 * STUDS_PER_METER studs.
        assert abs(part.cframe.y - 10.0 * STUDS_PER_METER) < 0.5, (
            f"variant m_LocalPosition.y override not converted: got {part.cframe.y}"
        )
