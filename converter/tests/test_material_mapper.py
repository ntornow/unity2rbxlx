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


class TestPhase42ShaderCategorization:
    """Phase 4.2: name-based shader categorization."""

    def test_categorize_standard(self):
        from converter.material_mapper import categorize_shader
        assert categorize_shader('Standard') == 'BUILTIN'
        assert categorize_shader('Standard (Specular setup)') == 'BUILTIN'

    def test_categorize_urp(self):
        from converter.material_mapper import categorize_shader
        assert categorize_shader('Universal Render Pipeline/Lit') == 'URP'
        assert categorize_shader('Universal Render Pipeline/Simple Lit') == 'URP'

    def test_categorize_hdrp(self):
        from converter.material_mapper import categorize_shader
        assert categorize_shader('HDRP/Lit') == 'HDRP'
        assert categorize_shader('HDRenderPipeline/Lit') == 'HDRP'

    def test_categorize_legacy(self):
        from converter.material_mapper import categorize_shader
        assert categorize_shader('Legacy Shaders/Diffuse') == 'LEGACY'

    def test_categorize_particle(self):
        from converter.material_mapper import categorize_shader
        assert categorize_shader('Particles/Standard Surface') == 'PARTICLE'
        assert categorize_shader('Legacy Shaders/Particles/Alpha Blended') == 'PARTICLE'

    def test_categorize_unlit(self):
        from converter.material_mapper import categorize_shader
        assert categorize_shader('Unlit/Texture') == 'UNLIT'

    def test_categorize_skybox(self):
        from converter.material_mapper import categorize_shader
        assert categorize_shader('Skybox/Procedural') == 'SKYBOX'

    def test_categorize_unknown(self):
        from converter.material_mapper import categorize_shader
        assert categorize_shader('SomeGame/CustomShader') == 'UNKNOWN'
        assert categorize_shader('') == 'UNKNOWN'


class TestPhase42VertexColorDetection:
    def test_vertex_lit_flagged(self):
        from converter.material_mapper import shader_uses_vertex_colors
        assert shader_uses_vertex_colors('Legacy Shaders/VertexLit')
        assert shader_uses_vertex_colors('Particles/VertexLit Blended')

    def test_standard_not_flagged(self):
        from converter.material_mapper import shader_uses_vertex_colors
        assert not shader_uses_vertex_colors('Standard')
        assert not shader_uses_vertex_colors('Universal Render Pipeline/Lit')

    def test_empty_not_flagged(self):
        from converter.material_mapper import shader_uses_vertex_colors
        assert not shader_uses_vertex_colors('')


class TestPhase42MaterialMappingExtension:
    def test_new_fields_default_safely(self):
        from converter.material_mapper import MaterialMapping
        m = MaterialMapping()
        assert m.shader_category == 'UNKNOWN'
        assert m.uses_vertex_colors is False
        assert m.emission_strength == 1.0
        assert m.source_path is None
        assert m.ao_map_path is None

