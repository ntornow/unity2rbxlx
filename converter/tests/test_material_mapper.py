"""
test_material_mapper.py -- Tests for material mapping.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from unity.yaml_parser import parse_documents, doc_body

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class TestMaterialInferenceExtended:
    """Extended tests for Roblox material inference from names."""

    def test_neon_material(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("neon_strip") == "Neon"
        assert _infer_roblox_material("glow_ring") == "Neon"

    def test_ground_materials(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("dirt_path") == "Ground"
        assert _infer_roblox_material("gravel01") == "Pebble"
        assert _infer_roblox_material("sand_beach") == "Sand"

    def test_ice_and_snow(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("ice_surface") == "Ice"
        assert _infer_roblox_material("snow_terrain") == "Snow"

    def test_fabric_materials(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("fabric_curtain") == "Fabric"
        assert _infer_roblox_material("leather_seat") == "Fabric"
        assert _infer_roblox_material("carpet_floor") == "Fabric"

    def test_corroded_metal(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("rust_barrel") == "CorrodedMetal"

    def test_diamond_plate(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("steel_plate") == "DiamondPlate"

    def test_case_insensitive(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("CONCRETE_WALL") == "Concrete"
        assert _infer_roblox_material("Metal_Door") == "Metal"

    def test_default_plastic(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("unknown_thing") == "Plastic"

    def test_metallic_value_fallback(self):
        """High metallic value should map to Metal when name doesn't match."""
        from converter.material_mapper import _infer_roblox_material
        # This tests the name-based inference only
        result = _infer_roblox_material("ShinyThing")
        assert result == "Plastic"  # No keyword match → Plastic


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
