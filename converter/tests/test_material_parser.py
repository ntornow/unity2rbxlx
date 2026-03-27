"""Tests for material_mapper regex parsers that handle both old and new Unity YAML formats."""

from converter.material_mapper import (
    _regex_parse_tex_envs,
    _regex_parse_floats,
    _regex_parse_colors,
    _normalize_tex_envs,
    _parse_mat_yaml,
)


class TestRegexTexEnvs:
    """Tests for _regex_parse_tex_envs."""

    def test_old_format_single_texture(self):
        raw = """
      data:
        first:
          name: _MainTex
        second:
          m_Texture: {fileID: 2800000, guid: abcdef0123456789abcdef0123456789, type: 3}
"""
        result = _regex_parse_tex_envs(raw)
        assert len(result) == 1
        assert "_MainTex" in result[0]
        assert result[0]["_MainTex"]["m_Texture"]["guid"] == "abcdef0123456789abcdef0123456789"

    def test_old_format_multiple_textures(self):
        raw = """
      data:
        first:
          name: _MainTex
        second:
          m_Texture: {fileID: 2800000, guid: aaaa0000bbbb1111cccc2222dddd3333, type: 3}
      data:
        first:
          name: _BumpMap
        second:
          m_Texture: {fileID: 2800000, guid: 11112222333344445555666677778888, type: 3}
"""
        result = _regex_parse_tex_envs(raw)
        assert len(result) == 2
        names = {list(e.keys())[0] for e in result}
        assert names == {"_MainTex", "_BumpMap"}

    def test_new_format(self):
        raw = """
    - _MainTex:
        m_Texture: {fileID: 2800000, guid: abcdef0123456789abcdef0123456789, type: 3}
    - _BumpMap:
        m_Texture: {fileID: 0}
"""
        result = _regex_parse_tex_envs(raw)
        assert len(result) >= 1
        main_tex = next(e for e in result if "_MainTex" in e)
        assert main_tex["_MainTex"]["m_Texture"]["guid"] == "abcdef0123456789abcdef0123456789"

    def test_empty_texture(self):
        raw = """
      data:
        first:
          name: _MainTex
        second:
          m_Texture: {fileID: 0}
"""
        result = _regex_parse_tex_envs(raw)
        assert len(result) == 1


class TestRegexFloats:
    """Tests for _regex_parse_floats."""

    def test_old_format(self):
        raw = """
      data:
        first:
          name: _Metallic
        second: 0.5
      data:
        first:
          name: _Glossiness
        second: 0.8
"""
        result = _regex_parse_floats(raw)
        assert len(result) == 2

    def test_new_format(self):
        raw = """
    - _Metallic: 0.5
    - _Glossiness: 0.8
"""
        result = _regex_parse_floats(raw)
        assert len(result) == 2


class TestRegexColors:
    """Tests for _regex_parse_colors."""

    def test_old_format(self):
        raw = """
      data:
        first:
          name: _Color
        second:
          r: 0.588
          g: 0.588
          b: 0.588
          a: 1
"""
        result = _regex_parse_colors(raw)
        assert len(result) == 1
        color = result[0]["_Color"]
        assert abs(color["r"] - 0.588) < 0.001
        assert abs(color["g"] - 0.588) < 0.001

    def test_new_format(self):
        raw = """
    - _Color: {r: 0.5, g: 0.6, b: 0.7, a: 1}
"""
        result = _regex_parse_colors(raw)
        assert len(result) == 1


class TestParseMatYaml:
    """Tests for _parse_mat_yaml with both formats."""

    def test_old_format_material(self):
        raw = """%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!21 &2100000
Material:
  m_Name: TestMaterial
  m_Shader: {fileID: 46, guid: 0000000000000000f000000000000000, type: 0}
  m_SavedProperties:
    serializedVersion: 2
    m_TexEnvs:
      data:
        first:
          name: _MainTex
        second:
          m_Texture: {fileID: 2800000, guid: abcdef0123456789abcdef0123456789, type: 3}
          m_Scale: {x: 1, y: 1}
          m_Offset: {x: 0, y: 0}
    m_Floats:
      data:
        first:
          name: _Mode
        second: 0
    m_Colors:
      data:
        first:
          name: _Color
        second:
          r: 0.5
          g: 0.6
          b: 0.7
          a: 1
"""
        mat_data = _parse_mat_yaml(raw)
        assert mat_data is not None
        assert mat_data.get("m_Name") == "TestMaterial"

        saved = mat_data.get("m_SavedProperties", {})
        tex_envs = _normalize_tex_envs(saved.get("m_TexEnvs", []))
        assert "_MainTex" in tex_envs
