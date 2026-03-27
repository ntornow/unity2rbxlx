"""
test_material_mapper.py -- Tests for material mapping.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unity.yaml_parser import parse_documents, doc_body

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestMaterialParsing:
    def test_parse_material_yaml(self, sample_material_yaml):
        docs = parse_documents(sample_material_yaml)
        assert len(docs) >= 1
        cid, fid, doc = docs[0]
        body = doc_body(doc)
        assert body.get("m_Name") == "TestMaterial"

    def test_extract_textures(self, sample_material_yaml):
        docs = parse_documents(sample_material_yaml)
        body = doc_body(docs[0][2])
        saved_props = body.get("m_SavedProperties", {})
        tex_envs = saved_props.get("m_TexEnvs", [])
        # Should find _MainTex, _BumpMap, _MetallicGlossMap
        tex_names = set()
        for entry in tex_envs:
            if isinstance(entry, dict):
                tex_names.update(entry.keys())
        assert "_MainTex" in tex_names
        assert "_BumpMap" in tex_names
        assert "_MetallicGlossMap" in tex_names

    def test_extract_color(self, sample_material_yaml):
        docs = parse_documents(sample_material_yaml)
        body = doc_body(docs[0][2])
        saved_props = body.get("m_SavedProperties", {})
        colors = saved_props.get("m_Colors", [])
        color_val = None
        for entry in colors:
            if isinstance(entry, dict) and "_Color" in entry:
                color_val = entry["_Color"]
                break
        assert color_val is not None
        assert abs(color_val["r"] - 0.8) < 0.01

    def test_extract_mode(self, sample_material_yaml):
        docs = parse_documents(sample_material_yaml)
        body = doc_body(docs[0][2])
        saved_props = body.get("m_SavedProperties", {})
        floats = saved_props.get("m_Floats", [])
        mode = None
        for entry in floats:
            if isinstance(entry, dict) and "_Mode" in entry:
                mode = entry["_Mode"]
                break
        assert mode == 0  # Opaque
