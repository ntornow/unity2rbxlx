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


class TestOrderedChildGoFids:
    """Walking ``m_Children`` in display order is the only way to keep
    Unity's ``transform.GetChild(i)`` semantics intact when the YAML
    document iteration order differs from authored order. SimpleFPS
    Turret was the canonical reproducer: ``m_Children=[Base, Collider]``
    in the prefab, but the YAML had Collider's GameObject defined first,
    so the parser visited [Collider, Base] and ``getTBase = GetChild(0)``
    returned the wrong sibling.
    """

    from unity.yaml_parser import ordered_child_go_fids

    def test_returns_children_in_m_children_order(self):
        from unity.yaml_parser import ordered_child_go_fids
        # Two child Transforms, fileIDs 100 and 200.
        # m_Children lists them as [200, 100] — i.e. the AUTHORED order
        # is opposite the YAML-document order.
        xform = {
            "m_Children": [
                {"fileID": 200},
                {"fileID": 100},
            ]
        }
        # xform_fid → go_fid mapping (each Transform belongs to one GO)
        xform_to_go = {"100": "go_first", "200": "go_second"}
        result = ordered_child_go_fids(xform, xform_to_go)
        assert result == ["go_second", "go_first"]

    def test_drops_dangling_references(self):
        # An m_Children entry whose fileID doesn't resolve to a known
        # GameObject must be skipped — not produce an empty string in
        # the output (which would later look up nothing under that key
        # and waste cycles or crash on .children.append(None)).
        from unity.yaml_parser import ordered_child_go_fids
        xform = {
            "m_Children": [
                {"fileID": 100},
                {"fileID": 999},  # unknown
                {"fileID": 200},
            ]
        }
        xform_to_go = {"100": "go_a", "200": "go_b"}
        result = ordered_child_go_fids(xform, xform_to_go)
        assert result == ["go_a", "go_b"]

    def test_empty_m_children_returns_empty(self):
        from unity.yaml_parser import ordered_child_go_fids
        assert ordered_child_go_fids({}, {}) == []
        assert ordered_child_go_fids({"m_Children": []}, {}) == []
        # m_Children present but all references are zero/null
        assert ordered_child_go_fids(
            {"m_Children": [{"fileID": 0}, {"fileID": 0}]},
            {},
        ) == []

    def test_dedupes_repeated_fileids(self):
        # Defensive: an authored prefab shouldn't list the same child
        # twice, but if it did, the helper must not yield duplicates.
        from unity.yaml_parser import ordered_child_go_fids
        xform = {
            "m_Children": [
                {"fileID": 100},
                {"fileID": 100},
            ]
        }
        result = ordered_child_go_fids(xform, {"100": "go_a"})
        assert result == ["go_a"]


# Synthetic Unity scene with one normal MonoBehaviour and one STRIPPED
# prefab-instance MonoBehaviour, plus a PrefabInstance doc. Self-contained
# (does not depend on the source project) so this gate runs in CI.
STRIPPED_SCENE_YAML = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &900001
GameObject:
  m_Name: Holder
  m_IsActive: 1
--- !u!114 &900002
MonoBehaviour:
  m_GameObject: {fileID: 900001}
  m_Name: NormalScript
  m_Script: {fileID: 11500000, guid: aaaa1111aaaa1111aaaa1111aaaa1111, type: 3}
--- !u!114 &137514649 stripped
MonoBehaviour:
  m_CorrespondingSourceObject: {fileID: 114000011972273750, guid: a53fe2875371488408daf0df7d69a981,
    type: 3}
  m_PrefabInstance: {fileID: 1822972501}
  m_PrefabAsset: {fileID: 0}
  m_GameObject: {fileID: 0}
  m_Enabled: 1
  m_Script: {fileID: 11500000, guid: fff2f071f7335eb43a712a702b990041, type: 3}
  m_Name:
--- !u!1001 &1822972501
PrefabInstance:
  m_SourcePrefab: {fileID: 100100000, guid: a53fe2875371488408daf0df7d69a981, type: 3}
"""


class TestStrippedOut:
    def test_returned_triples_byte_identical_with_stripped_out(self):
        # The returned result list must be unaffected by passing stripped_out.
        without = parse_documents(STRIPPED_SCENE_YAML)
        stripped: list = []
        with_out = parse_documents(STRIPPED_SCENE_YAML, stripped_out=stripped)
        assert with_out == without
        # And the stripped fileID never enters the returned triples.
        fids = {fid for _, fid, _ in with_out}
        assert "137514649" not in fids

    def test_stripped_out_captures_stripped_mb_triple(self):
        stripped: list = []
        parse_documents(STRIPPED_SCENE_YAML, stripped_out=stripped)
        assert len(stripped) == 1
        cid, fid, body = stripped[0]
        assert cid == 114
        assert fid == "137514649"
        assert isinstance(body, dict)
        mb = body["MonoBehaviour"]
        assert mb["m_PrefabInstance"]["fileID"] == 1822972501

    def test_stripped_out_default_none_no_capture(self):
        # No exception, no capture, when stripped_out is omitted.
        docs = parse_documents(STRIPPED_SCENE_YAML)
        fids = {fid for _, fid, _ in docs}
        assert "137514649" not in fids


# Synthetic scene with an interior malformed (non-dict) document BEFORE a
# stripped MonoBehaviour. Under a per-dict header counter the malformed doc
# would shift every later doc onto the wrong header (the stripped MB leaks into
# ``result`` with a wrong (cid, fid) and never reaches ``stripped_out``).
# Position-stable pairing must keep each doc on its own header.
MALFORMED_BEFORE_STRIPPED_YAML = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &1
: : : not valid yaml mapping : :
--- !u!114 &2
MonoBehaviour:
  m_Name: Real
--- !u!114 &3 stripped
MonoBehaviour:
  m_PrefabInstance: {fileID: 9999}
"""


class TestPositionStablePairing:
    def test_stripped_after_malformed_doc_captured_with_right_fileid(self):
        # (a) the stripped MB is captured in stripped_out with the RIGHT fileID.
        stripped: list = []
        result = parse_documents(
            MALFORMED_BEFORE_STRIPPED_YAML, stripped_out=stripped
        )
        assert len(stripped) == 1
        cid, fid, body = stripped[0]
        assert cid == 114
        assert fid == "3"
        assert body["MonoBehaviour"]["m_PrefabInstance"]["fileID"] == 9999

        # (b) the stripped MB is NOT in result (under any fileID).
        result_fids = {fid for _, fid, _ in result}
        assert "3" not in result_fids

        # (c) the non-stripped doc carries its CORRECT (cid, fid) despite the
        # earlier malformed doc consuming its own header slot.
        assert result == [(114, "2", {"MonoBehaviour": {"m_Name": "Real"}})]


REAL_SCENE = Path("/Users/jiazou/workspace/trash-dash/Assets/Scenes/Main.unity")


class TestStrippedOutRealScene:
    def test_real_scene_stripped_mb_captured(self):
        if not REAL_SCENE.exists():
            import pytest
            pytest.skip("real Trash-Dash scene not present")
        raw = REAL_SCENE.read_text(encoding="utf-8", errors="replace")
        stripped: list = []
        without = parse_documents(raw)
        with_out = parse_documents(raw, stripped_out=stripped)
        # contract: returned triples byte-identical
        assert with_out == without
        captured = {fid for _, fid, _ in stripped}
        assert "137514649" in captured
        assert "80306028" in captured
        assert "926798345" in captured
        # the stripped fileIDs are NOT in the returned triples
        result_fids = {fid for _, fid, _ in with_out}
        assert "137514649" not in result_fids
