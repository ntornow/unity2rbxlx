"""Tests for rbxl_binary_writer.xml_to_binary.

Roblox Studio's binary .rbxl format is the only path that preserves
mesh Models for headless place reconstruction. A regression that
produces an unparseable binary breaks the entire upload path silently —
the file looks fine on disk, fails on Studio import.

Byte-for-byte snapshots are too brittle for binary formats; tests verify
structural correctness: magic header present, instance/class counts in
the header match the input, and chunk markers (META, INST, PROP, PRNT,
END) all appear in the right positions.
"""
from __future__ import annotations

import struct
from pathlib import Path

from roblox.rbxl_binary_writer import FORMAT_VERSION, MAGIC, xml_to_binary


def _minimal_rbxlx(*, parts: int = 1) -> str:
    """Build the smallest valid rbxlx that exercises Item/Properties/parent."""
    items = []
    for i in range(parts):
        items.append(f"""    <Item class="Part" referent="part{i}">
      <Properties>
        <string name="Name">P{i}</string>
        <bool name="Anchored">true</bool>
      </Properties>
    </Item>""")
    body = "\n".join(items)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<roblox version="4">
  <Item class="Workspace" referent="workspace">
    <Properties>
      <string name="Name">Workspace</string>
    </Properties>
{body}
  </Item>
</roblox>
"""


class TestBinaryHeader:
    def test_writes_file(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "place.rbxlx"
        xml_path.write_text(_minimal_rbxlx())

        out = xml_to_binary(xml_path, tmp_path / "place.rbxl")

        assert out.exists()
        assert out.stat().st_size > 0

    def test_default_output_path_uses_rbxl_extension(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "scene.rbxlx"
        xml_path.write_text(_minimal_rbxlx())

        out = xml_to_binary(xml_path)

        assert out == xml_path.with_suffix(".rbxl")
        assert out.exists()

    def test_magic_header_present(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "p.rbxlx"
        xml_path.write_text(_minimal_rbxlx())
        out = xml_to_binary(xml_path)
        data = out.read_bytes()

        assert data.startswith(MAGIC), "binary file must start with Roblox magic"

    def test_header_records_class_and_instance_counts(self, tmp_path: Path) -> None:
        """The 32-byte header includes class_count and instance_count after
        magic + version. A regression that miscounts here makes Studio
        refuse to parse the file."""
        xml_path = tmp_path / "p.rbxlx"
        xml_path.write_text(_minimal_rbxlx(parts=3))
        out = xml_to_binary(xml_path)
        data = out.read_bytes()

        # Header layout: MAGIC (14) + version (2) + class_count (4) + instance_count (4) + reserved (8)
        version_offset = len(MAGIC)
        version = struct.unpack_from("<H", data, version_offset)[0]
        class_count = struct.unpack_from("<I", data, version_offset + 2)[0]
        instance_count = struct.unpack_from("<I", data, version_offset + 6)[0]

        assert version == FORMAT_VERSION
        # Workspace + Part = 2 distinct classes
        assert class_count == 2
        # 1 Workspace + 3 Parts = 4 instances
        assert instance_count == 4


class TestChunkPresence:
    """Every well-formed binary .rbxl contains: META, INST chunks (one per
    class), PROP chunks (per property per class), PRNT, END.

    Chunk names are 4-byte ASCII at known positions; we don't fully parse
    them, just verify each marker appears at least once."""

    def test_all_required_chunks_present(self, tmp_path: Path) -> None:
        xml_path = tmp_path / "p.rbxlx"
        xml_path.write_text(_minimal_rbxlx(parts=2))
        out = xml_to_binary(xml_path)
        data = out.read_bytes()

        for marker in (b"META", b"INST", b"PROP", b"PRNT", b"END\x00"):
            assert marker in data, f"chunk {marker!r} missing from binary"

    def test_inst_chunk_count_matches_class_count(self, tmp_path: Path) -> None:
        """Each distinct class gets one INST chunk. Three classes ⇒ three INST."""
        xml_with_three_classes = """<?xml version="1.0"?>
<roblox version="4">
  <Item class="Workspace" referent="ws">
    <Properties><string name="Name">Workspace</string></Properties>
    <Item class="Part" referent="p1">
      <Properties><string name="Name">P1</string></Properties>
    </Item>
    <Item class="Folder" referent="f1">
      <Properties><string name="Name">F1</string></Properties>
    </Item>
  </Item>
</roblox>
"""
        xml_path = tmp_path / "p.rbxlx"
        xml_path.write_text(xml_with_three_classes)
        out = xml_to_binary(xml_path)
        data = out.read_bytes()

        # Count INST chunk occurrences (4-byte marker)
        inst_count = data.count(b"INST")
        # Workspace + Part + Folder = 3 distinct classes
        assert inst_count == 3


class TestErrorHandling:
    def test_no_items_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.rbxlx"
        empty.write_text("""<?xml version="1.0"?><roblox version="4"></roblox>""")

        try:
            xml_to_binary(empty)
        except ValueError as exc:
            assert "Item" in str(exc) or "no" in str(exc).lower()
        else:
            raise AssertionError("expected ValueError for empty input")
