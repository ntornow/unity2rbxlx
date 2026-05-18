"""
test_rbxl_binary_writer.py -- Unit tests for the XML-to-binary .rbxl converter.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from roblox.rbxl_binary_writer import MAGIC, xml_to_binary


MINIMAL_RBXLX = """\
<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" version="4">
  <Item class="Workspace" referent="RBX0">
    <Properties>
      <string name="Name">Workspace</string>
    </Properties>
    <Item class="Part" referent="RBX1">
      <Properties>
        <string name="Name">TestPart</string>
        <bool name="Anchored">true</bool>
        <float name="Transparency">0</float>
        <int name="BrickColor">194</int>
        <Vector3 name="Position">
          <X>1.5</X><Y>2.5</Y><Z>3.5</Z>
        </Vector3>
        <CoordinateFrame name="CFrame">
          <X>1.5</X><Y>2.5</Y><Z>3.5</Z>
          <R00>1</R00><R01>0</R01><R02>0</R02>
          <R10>0</R10><R11>1</R11><R12>0</R12>
          <R20>0</R20><R21>0</R21><R22>1</R22>
        </CoordinateFrame>
        <Color3uint8 name="Color3uint8">4294967295</Color3uint8>
      </Properties>
    </Item>
  </Item>
</roblox>
"""

SCRIPT_RBXLX = """\
<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" version="4">
  <Item class="Workspace" referent="RBX0">
    <Properties>
      <string name="Name">Workspace</string>
    </Properties>
    <Item class="Script" referent="RBX1">
      <Properties>
        <string name="Name">TestScript</string>
        <ProtectedString name="Source"><![CDATA[print("hello world")]]></ProtectedString>
      </Properties>
    </Item>
  </Item>
</roblox>
"""


class TestXmlToBinary:
    def test_produces_binary_file(self, tmp_path):
        xml_file = tmp_path / "test.rbxlx"
        xml_file.write_text(MINIMAL_RBXLX, encoding="utf-8")
        result = xml_to_binary(xml_file)
        assert result.exists()
        assert result.suffix == ".rbxl"

    def test_binary_has_magic_header(self, tmp_path):
        xml_file = tmp_path / "test.rbxlx"
        xml_file.write_text(MINIMAL_RBXLX, encoding="utf-8")
        result = xml_to_binary(xml_file)
        assert result.read_bytes()[:len(MAGIC)] == MAGIC

    def test_binary_is_not_empty(self, tmp_path):
        xml_file = tmp_path / "test.rbxlx"
        xml_file.write_text(MINIMAL_RBXLX, encoding="utf-8")
        result = xml_to_binary(xml_file)
        assert result.stat().st_size > len(MAGIC) + 10

    def test_custom_output_path(self, tmp_path):
        xml_file = tmp_path / "test.rbxlx"
        xml_file.write_text(MINIMAL_RBXLX, encoding="utf-8")
        out = tmp_path / "custom.rbxl"
        result = xml_to_binary(xml_file, out)
        assert result == out
        assert result.exists()

    def test_default_output_path(self, tmp_path):
        xml_file = tmp_path / "myplace.rbxlx"
        xml_file.write_text(MINIMAL_RBXLX, encoding="utf-8")
        result = xml_to_binary(xml_file)
        assert result.name == "myplace.rbxl"

    def test_sibling_emission_round_trip(self, tmp_path):
        """Pipeline emits .rbxl alongside .rbxlx with the same stem."""
        rbxlx = tmp_path / "converted_place.rbxlx"
        rbxlx.write_text(MINIMAL_RBXLX, encoding="utf-8")
        rbxl = xml_to_binary(rbxlx)
        assert rbxl.parent == rbxlx.parent
        assert rbxl.stem == rbxlx.stem
        assert rbxl.suffix == ".rbxl"


class TestBinaryWithScripts:
    def test_script_rbxlx_converts(self, tmp_path):
        xml_file = tmp_path / "test.rbxlx"
        xml_file.write_text(SCRIPT_RBXLX, encoding="utf-8")
        result = xml_to_binary(xml_file)
        data = result.read_bytes()
        assert data[:len(MAGIC)] == MAGIC
        assert b"hello world" in data


class TestTokenPropertyDefaults:
    """The binary format groups properties per class — if any instance of
    a class has a Token property set, every instance in that class needs
    a value emitted. The wrong default silently corrupts data only visible
    when the binary is loaded — the XML form loads fine because Studio
    uses Roblox's engine default for absent properties. The defaults must
    be keyed by ``(class_name, property_name)`` because the same property
    name (e.g. ``Shape``) has different defaults on different classes
    (``Part.Shape=Block`` vs ``ParticleEmitter.Shape=Box``).
    """

    def test_part_shape_default_is_block(self):
        from roblox.rbxl_binary_writer import _default_for_property, TYPE_ENUM
        # BasePart.Shape → Block (1), not Ball (0).
        assert _default_for_property("Part", "Shape", TYPE_ENUM) == 1

    def test_particle_emitter_shape_does_not_collide_with_part_shape(self):
        """Regression: do NOT force ``ParticleEmitter.Shape`` to Block (1).
        Its enum is ``ParticleEmitterShape`` whose default is Box (0).
        Name-only keying (the original PR #99 design) would have collided
        with ``Part.Shape`` and forced every ParticleEmitter to a wrong
        shape."""
        from roblox.rbxl_binary_writer import _default_for_property, TYPE_ENUM
        # Falls through to type-default 0 (= Box for this enum) because
        # there is no explicit ParticleEmitter override entry.
        assert _default_for_property("ParticleEmitter", "Shape", TYPE_ENUM) == 0

    def test_material_default_is_plastic(self):
        """BasePart.Material defaults to ``Enum.Material.Plastic = 256``,
        not 0 (which is invalid)."""
        from roblox.rbxl_binary_writer import _default_for_property, TYPE_ENUM
        assert _default_for_property("Part", "Material", TYPE_ENUM) == 256
        assert _default_for_property("MeshPart", "Material", TYPE_ENUM) == 256
        assert _default_for_property("SpawnLocation", "Material", TYPE_ENUM) == 256

    def test_text_alignment_defaults_are_center(self):
        """Text alignment defaults to Center on every Text* GUI element:
        TextXAlignment=2, TextYAlignment=1."""
        from roblox.rbxl_binary_writer import _default_for_property, TYPE_ENUM
        for cls in ("TextLabel", "TextButton", "TextBox"):
            assert _default_for_property(cls, "TextXAlignment", TYPE_ENUM) == 2
            assert _default_for_property(cls, "TextYAlignment", TYPE_ENUM) == 1

    def test_part_surfaces_default_to_smooth(self):
        """BasePart surface tokens default to ``Enum.SurfaceType.Smooth =
        0``. ``rbxlx_writer.py`` always emits these explicitly so the fill
        path is mostly defensive, but a third-party XML lacking the
        property must still land on Smooth."""
        from roblox.rbxl_binary_writer import _default_for_property, TYPE_ENUM
        for face in ("TopSurface", "BottomSurface", "FrontSurface",
                     "BackSurface", "LeftSurface", "RightSurface"):
            assert _default_for_property("Part", face, TYPE_ENUM) == 0
            assert _default_for_property("MeshPart", face, TYPE_ENUM) == 0

    def test_unknown_token_falls_through_to_type_default(self):
        from roblox.rbxl_binary_writer import (
            _default_for_property, _default_for_type, TYPE_ENUM,
        )
        assert _default_for_property("Workspace", "SomeUnknownToken", TYPE_ENUM) == 0
        assert _default_for_type(TYPE_ENUM) == 0

    def test_non_token_types_ignore_class_keying(self):
        """The class-aware lookup only kicks in for Token properties.
        Float/Vector/String defaults still come from ``_default_for_type``
        regardless of class — keeping the fast path for the common case."""
        from roblox.rbxl_binary_writer import (
            _default_for_property, _default_for_type, TYPE_FLOAT, TYPE_STRING,
        )
        assert _default_for_property("Part", "Transparency", TYPE_FLOAT) == _default_for_type(TYPE_FLOAT)
        assert _default_for_property("AnyClass", "AnyProp", TYPE_STRING) == _default_for_type(TYPE_STRING)

    def test_two_parts_one_missing_shape_writes_valid_binary(self, tmp_path):
        """End-to-end regression — given a Part with no Shape property in
        the XML and a sibling Part that *does* set Shape, the binary must
        be well-formed and produce expected names. Full PROP-chunk
        decoding lives in the round-trip test suite."""
        xml = (
            '<?xml version="1.0"?>'
            '<roblox>'
            '<Item class="Workspace" referent="W">'
            '  <Item class="Part" referent="P1">'
            '    <Properties>'
            '      <string name="Name">FlatCollider</string>'
            '      <Vector3 name="size"><X>50</X><Y>1</Y><Z>10</Z></Vector3>'
            '    </Properties>'
            '  </Item>'
            '  <Item class="Part" referent="P2">'
            '    <Properties>'
            '      <string name="Name">SphereTrigger</string>'
            '      <Vector3 name="size"><X>2</X><Y>2</Y><Z>2</Z></Vector3>'
            '      <token name="Shape">0</token>'
            '    </Properties>'
            '  </Item>'
            '</Item>'
            '</roblox>'
        )
        xml_file = tmp_path / "shape_default.rbxlx"
        xml_file.write_text(xml, encoding="utf-8")
        result = xml_to_binary(xml_file)
        data = result.read_bytes()
        assert data[:len(MAGIC)] == MAGIC
        assert b"FlatCollider" in data
        assert b"SphereTrigger" in data


class TestBinaryErrorHandling:
    def test_nonexistent_file(self, tmp_path):
        with pytest.raises((FileNotFoundError, ET.ParseError, OSError)):
            xml_to_binary(tmp_path / "nope.rbxlx")

    def test_empty_xml(self, tmp_path):
        xml_file = tmp_path / "empty.rbxlx"
        xml_file.write_text("", encoding="utf-8")
        with pytest.raises((ET.ParseError, Exception)):
            xml_to_binary(xml_file)

    def test_malformed_xml(self, tmp_path):
        xml_file = tmp_path / "bad.rbxlx"
        xml_file.write_text("<roblox><Item>unclosed", encoding="utf-8")
        with pytest.raises((ET.ParseError, Exception)):
            xml_to_binary(xml_file)
