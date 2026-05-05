"""
test_scene_parser.py -- Tests for scene parsing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unity.scene_parser import parse_scene

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestParseScene:
    def test_parse_fixture_scene(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        assert scene is not None
        assert len(scene.all_nodes) > 0

    def test_root_nodes(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        root_names = {n.name for n in scene.roots}
        assert "MainCamera" in root_names
        assert "Cube" in root_names
        assert "PointLight" in root_names
        assert "Parent" in root_names
        # Child should NOT be a root
        assert "Child" not in root_names

    def test_hierarchy(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        # Find Parent node
        parent = None
        for node in scene.all_nodes.values():
            if node.name == "Parent":
                parent = node
                break
        assert parent is not None
        assert len(parent.children) == 1
        assert parent.children[0].name == "Child"

    def test_transform_values(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        # Find Cube node
        cube = None
        for node in scene.all_nodes.values():
            if node.name == "Cube":
                cube = node
                break
        assert cube is not None
        assert cube.position == (2.0, 0.5, 3.0)
        assert abs(cube.rotation[1] - 0.7071068) < 0.001

    def test_child_transform(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        child = None
        for node in scene.all_nodes.values():
            if node.name == "Child":
                child = node
                break
        assert child is not None
        assert child.position == (1.0, 2.0, 3.0)
        assert child.scale == (0.5, 0.5, 0.5)

    def test_components_attached(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        cube = None
        for node in scene.all_nodes.values():
            if node.name == "Cube":
                cube = node
                break
        assert cube is not None
        comp_types = {c.component_type for c in cube.components}
        assert "Transform" in comp_types
        assert "MeshFilter" in comp_types
        assert "MeshRenderer" in comp_types
        assert "BoxCollider" in comp_types

    def test_material_guids_extracted(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        assert "abcdef1234567890abcdef1234567890" in scene.referenced_material_guids

    def test_render_settings(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        assert "m_Fog" in scene.render_settings

    def test_camera_component(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        camera = None
        for node in scene.all_nodes.values():
            if node.name == "MainCamera":
                camera = node
                break
        assert camera is not None
        assert camera.tag == "MainCamera"
        comp_types = {c.component_type for c in camera.components}
        assert "Camera" in comp_types

    def test_light_component(self):
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        light = None
        for node in scene.all_nodes.values():
            if node.name == "PointLight":
                light = node
                break
        assert light is not None
        comp_types = {c.component_type for c in light.components}
        assert "Light" in comp_types

    def test_nonexistent_file(self):
        import pytest
        with pytest.raises(FileNotFoundError):
            parse_scene("/nonexistent/path.unity")

    def test_parse_warnings_list_exists(self):
        """Phase 4.7: ParsedScene.parse_warnings accumulates per-doc YAML errors."""
        scene = parse_scene(FIXTURES_DIR / "simple_scene.yaml")
        # Clean fixture — field must exist and be empty, not missing.
        assert isinstance(scene.parse_warnings, list)

    def test_animator_controller_guid_aggregated(self, tmp_path):
        """Phase 4.7: Animator components surface their m_Controller GUID on
        ParsedScene.referenced_animator_controller_guids for 4.5 routing.
        """
        scene_yaml = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &100
GameObject:
  m_Name: Player
  m_IsActive: 1
  serializedVersion: 6
  m_Component:
  - component: {fileID: 101}
  - component: {fileID: 102}
--- !u!4 &101
Transform:
  m_GameObject: {fileID: 100}
  m_LocalPosition: {x: 0, y: 0, z: 0}
  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}
  m_LocalScale: {x: 1, y: 1, z: 1}
  m_Father: {fileID: 0}
--- !u!95 &102
Animator:
  m_GameObject: {fileID: 100}
  m_Controller: {fileID: 9100000, guid: deadbeefcafebabe1234567890abcdef, type: 2}
"""
        scene_file = tmp_path / "anim_scene.unity"
        scene_file.write_text(scene_yaml)
        scene = parse_scene(scene_file)
        assert "deadbeefcafebabe1234567890abcdef" in scene.referenced_animator_controller_guids


class TestParseRealScene:
    """Tests against real test projects (skipped if not available)."""

    def test_parse_simplefps_main(self, simplefps_project):
        scene_path = simplefps_project / "Assets" / "Scenes" / "main.unity"
        if not scene_path.exists():
            import pytest
            pytest.skip("SimpleFPS not available")

        scene = parse_scene(scene_path)
        assert len(scene.all_nodes) > 0
        assert len(scene.roots) > 0
        # SimpleFPS should have many game objects
        assert len(scene.all_nodes) > 10


class TestMChildrenOrderPreserved:
    """When a parent Transform lists children in m_Children, the parser
    must preserve that authored order — even when the YAML's document
    iteration order is different. Reproducer: SimpleFPS Turret prefab
    declared Collider's GameObject before Base in the YAML stream, but
    the authored m_Children was [Base, Collider]. ``transform.GetChild(0)``
    in the C# (translated to ``getTBase = getChildIndex(model, 1)``) needs
    Base, not Collider, or the turret rotates the trigger zone instead
    of its visible mesh.
    """

    def test_children_appear_in_m_children_order_not_yaml_order(self, tmp_path):
        # A scene with three sibling GameObjects whose YAML-doc order
        # is [C, B, A] but whose parent's m_Children says [A, B, C].
        # The parsed root must yield children in [A, B, C] order.
        scene = tmp_path / "ordered.unity"
        scene.write_text(
            "%YAML 1.1\n"
            "%TAG !u! tag:unity3d.com,2011:\n"
            # Parent GO + Transform first, but its m_Children list is the
            # authored order: A then B then C.
            "--- !u!1 &1\n"
            "GameObject:\n"
            "  m_Component:\n"
            "  - component: {fileID: 11}\n"
            "  m_Name: Root\n"
            "--- !u!4 &11\n"
            "Transform:\n"
            "  m_GameObject: {fileID: 1}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "  m_Children:\n"
            "  - {fileID: 30}\n"
            "  - {fileID: 20}\n"
            "  - {fileID: 31}\n"
            "  m_Father: {fileID: 0}\n"
            # Children declared in REVERSED YAML order: C, B, A.
            "--- !u!1 &3\n"
            "GameObject:\n"
            "  m_Component:\n"
            "  - component: {fileID: 31}\n"
            "  m_Name: ChildC\n"
            "--- !u!4 &31\n"
            "Transform:\n"
            "  m_GameObject: {fileID: 3}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "  m_Children: []\n"
            "  m_Father: {fileID: 11}\n"
            "--- !u!1 &2\n"
            "GameObject:\n"
            "  m_Component:\n"
            "  - component: {fileID: 20}\n"
            "  m_Name: ChildB\n"
            "--- !u!4 &20\n"
            "Transform:\n"
            "  m_GameObject: {fileID: 2}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "  m_Children: []\n"
            "  m_Father: {fileID: 11}\n"
            "--- !u!1 &30\n"
            "GameObject:\n"
            "  m_Component:\n"
            "  - component: {fileID: 30}\n"
            "  m_Name: ChildA\n"
            "--- !u!4 &30\n"
            "Transform:\n"
            "  m_GameObject: {fileID: 30}\n"
            "  m_LocalPosition: {x: 0, y: 0, z: 0}\n"
            "  m_LocalRotation: {x: 0, y: 0, z: 0, w: 1}\n"
            "  m_LocalScale: {x: 1, y: 1, z: 1}\n"
            "  m_Children: []\n"
            "  m_Father: {fileID: 11}\n"
        )
        result = parse_scene(scene)
        assert len(result.roots) == 1
        root = result.roots[0]
        assert root.name == "Root"
        names = [c.name for c in root.children]
        # Authored m_Children was [30, 20, 31] = [ChildA, ChildB, ChildC]
        # YAML-doc order was [C, B, A] — must NOT match.
        assert names == ["ChildA", "ChildB", "ChildC"], (
            f"children appeared in {names}; expected [ChildA, ChildB, ChildC] "
            f"from m_Children order. Falling back to YAML order would "
            f"break ``transform.GetChild(i)`` for any prefab."
        )
