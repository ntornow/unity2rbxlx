"""
test_rbxl_binary_writer.py -- Unit tests for the XML-to-binary .rbxl converter.

Tests binary format correctness, property encoding, and error handling.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from roblox.rbxl_binary_writer import MAGIC, xml_to_binary


# ---------------------------------------------------------------------------
# Minimal RBXLX XML fixtures
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# xml_to_binary — basic conversion
# ---------------------------------------------------------------------------


class TestXmlToBinary:
    """Core conversion tests."""

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
        data = result.read_bytes()
        assert data[:len(MAGIC)] == MAGIC

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


class TestBinaryWithScripts:
    """Verify script CDATA is preserved in binary output."""

    def test_script_rbxlx_converts(self, tmp_path):
        xml_file = tmp_path / "test.rbxlx"
        xml_file.write_text(SCRIPT_RBXLX, encoding="utf-8")
        result = xml_to_binary(xml_file)
        assert result.exists()
        data = result.read_bytes()
        assert data[:len(MAGIC)] == MAGIC
        # The script source should appear somewhere in the binary
        assert b"hello world" in data


class TestBinaryErrorHandling:
    """Verify graceful handling of bad input."""

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
