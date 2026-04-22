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
