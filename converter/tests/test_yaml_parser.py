"""
test_yaml_parser.py -- Tests for Unity YAML parsing.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unity.yaml_parser import (
    parse_documents,
    doc_body,
    extract_vec3,
    extract_quat,
    ref_file_id,
    ref_guid,
    is_text_yaml,
    CID_GAME_OBJECT,
    CID_TRANSFORM,
    CID_CAMERA,
    CID_MESH_FILTER,
    CID_MESH_RENDERER,
    CID_LIGHT,
    CID_BOX_COLLIDER,
    CID_RENDER_SETTINGS,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestParseDocuments:
    def test_parse_simple_scene(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        assert len(docs) > 0
        # Check that we get classID, fileID, dict triples
        for cid, fid, doc in docs:
            assert isinstance(cid, int)
            assert isinstance(fid, str)
            assert isinstance(doc, dict)

    def test_classids_extracted(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        class_ids = {cid for cid, _, _ in docs}
        # Should find GameObjects, Transforms, Camera, etc.
        assert CID_GAME_OBJECT in class_ids  # GameObject
        assert CID_TRANSFORM in class_ids    # Transform
        assert CID_CAMERA in class_ids       # Camera

    def test_fileids_extracted(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        file_ids = {fid for _, fid, _ in docs}
        assert "100" in file_ids  # MainCamera GO
        assert "101" in file_ids  # MainCamera Transform
        assert "200" in file_ids  # Cube GO

    def test_gameobject_properties(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        go_docs = [(fid, doc) for cid, fid, doc in docs if cid == CID_GAME_OBJECT]
        # Find MainCamera
        camera_go = None
        for fid, doc in go_docs:
            body = doc_body(doc)
            if body.get("m_Name") == "MainCamera":
                camera_go = body
                break
        assert camera_go is not None
        assert camera_go["m_Name"] == "MainCamera"
        assert camera_go["m_TagString"] == "MainCamera"

    def test_transform_properties(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        # Find Cube's transform (fileID 201)
        for cid, fid, doc in docs:
            if cid == CID_TRANSFORM and fid == "201":
                body = doc_body(doc)
                pos = extract_vec3(body, "m_LocalPosition")
                assert pos == (2.0, 0.5, 3.0)
                rot = extract_quat(body, "m_LocalRotation")
                assert abs(rot[1] - 0.7071068) < 0.001
                break

    def test_mesh_filter_reference(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        for cid, fid, doc in docs:
            if cid == CID_MESH_FILTER:
                body = doc_body(doc)
                mesh_ref = body.get("m_Mesh", {})
                # Built-in mesh has all-zero GUID
                assert "guid" in mesh_ref or "fileID" in mesh_ref
                break

    def test_material_reference(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        for cid, fid, doc in docs:
            if cid == CID_MESH_RENDERER:
                body = doc_body(doc)
                mats = body.get("m_Materials", [])
                assert len(mats) > 0
                guid = ref_guid(mats[0])
                assert guid == "abcdef1234567890abcdef1234567890"
                break

    def test_render_settings(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        for cid, fid, doc in docs:
            if cid == CID_RENDER_SETTINGS:
                body = doc_body(doc)
                assert "m_Fog" in body
                break

    def test_parent_child_references(self, simple_scene_yaml):
        docs = parse_documents(simple_scene_yaml)
        # Child transform (501) should reference Parent transform (401) as father
        for cid, fid, doc in docs:
            if cid == CID_TRANSFORM and fid == "501":
                body = doc_body(doc)
                father_fid = ref_file_id(body.get("m_Father"))
                assert father_fid == "401"
                break


class TestDocBody:
    def test_unwrap(self):
        doc = {"GameObject": {"m_Name": "Test"}}
        assert doc_body(doc) == {"m_Name": "Test"}

    def test_no_inner_dict(self):
        doc = {"key": "value"}
        assert doc_body(doc) == {"key": "value"}


class TestExtractVec3:
    def test_normal(self):
        d = {"pos": {"x": 1.0, "y": 2.0, "z": 3.0}}
        assert extract_vec3(d, "pos") == (1.0, 2.0, 3.0)

    def test_missing_key(self):
        assert extract_vec3({}, "pos") == (0.0, 0.0, 0.0)

    def test_non_dict_value(self):
        assert extract_vec3({"pos": "invalid"}, "pos") == (0.0, 0.0, 0.0)


class TestExtractQuat:
    def test_normal(self):
        d = {"rot": {"x": 0, "y": 0.707, "z": 0, "w": 0.707}}
        result = extract_quat(d, "rot")
        assert abs(result[1] - 0.707) < 0.001
        assert abs(result[3] - 0.707) < 0.001

    def test_default(self):
        assert extract_quat({}, "rot") == (0.0, 0.0, 0.0, 1.0)


class TestRefFileId:
    def test_normal(self):
        assert ref_file_id({"fileID": 123}) == "123"

    def test_zero(self):
        assert ref_file_id({"fileID": 0}) is None

    def test_none(self):
        assert ref_file_id(None) is None


class TestRefGuid:
    def test_normal(self):
        assert ref_guid({"guid": "abcdef1234567890abcdef1234567890"}) == "abcdef1234567890abcdef1234567890"

    def test_all_zeros(self):
        assert ref_guid({"guid": "0" * 32}) is None

    def test_empty(self):
        assert ref_guid({"guid": ""}) is None


class TestIsTextYaml:
    def test_fixture_is_yaml(self, fixtures_dir):
        assert is_text_yaml(fixtures_dir / "simple_scene.yaml")

    def test_nonexistent(self, tmp_path):
        assert not is_text_yaml(tmp_path / "nope.unity")
