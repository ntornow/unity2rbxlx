"""Smoke tests for the XML -> binary RBXL writer."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_xml_to_binary_writes_magic_header(tmp_path):
    from roblox.rbxl_binary_writer import xml_to_binary, MAGIC

    xml_path = tmp_path / "tiny.rbxlx"
    xml_path.write_text(
        '<roblox version="4">'
        '<Item class="Workspace" referent="RBX0">'
        '<Properties>'
        '<string name="Name">Workspace</string>'
        '</Properties>'
        '</Item>'
        '</roblox>'
    )

    rbxl = xml_to_binary(xml_path)
    assert rbxl.exists()
    assert rbxl.suffix == ".rbxl"
    data = rbxl.read_bytes()
    assert data.startswith(MAGIC)


def test_xml_to_binary_custom_output_path(tmp_path):
    from roblox.rbxl_binary_writer import xml_to_binary

    xml_path = tmp_path / "in.rbxlx"
    xml_path.write_text(
        '<roblox version="4">'
        '<Item class="Workspace" referent="RBX0">'
        '<Properties><string name="Name">Workspace</string></Properties>'
        '</Item>'
        '</roblox>'
    )
    out = tmp_path / "custom.rbxl"
    result = xml_to_binary(xml_path, out)
    assert result == out
    assert out.exists()


def test_xml_to_binary_rejects_empty_file(tmp_path):
    from roblox.rbxl_binary_writer import xml_to_binary

    xml_path = tmp_path / "empty.rbxlx"
    xml_path.write_text('<roblox version="4"></roblox>')

    import pytest
    with pytest.raises(ValueError):
        xml_to_binary(xml_path)


def test_xml_to_binary_sibling_emission_round_trip(tmp_path):
    """The pipeline emits .rbxl alongside .rbxlx with the same stem."""
    from roblox.rbxl_binary_writer import xml_to_binary

    rbxlx = tmp_path / "converted_place.rbxlx"
    rbxlx.write_text(
        '<roblox version="4">'
        '<Item class="Workspace" referent="RBX0">'
        '<Properties><string name="Name">Workspace</string></Properties>'
        '</Item>'
        '</roblox>'
    )
    rbxl = xml_to_binary(rbxlx)
    assert rbxl.parent == rbxlx.parent
    assert rbxl.stem == rbxlx.stem
    assert rbxl.suffix == ".rbxl"
