"""
fbx_binary.py -- Minimal FBX binary reader/writer for in-place vertex editing.

Parses FBX binary files into a node tree and serialises them back, preserving
all sub-mesh structure. Used by ``mirror_fbx_z_inplace()`` to negate Z
coordinates in Vertices/Normals arrays and flip polygon winding in
PolygonVertexIndex arrays without the sub-mesh loss that assimp's
FBX→OBJ→FBX round-trip causes.

Supports FBX versions < 7500 (32-bit offsets) and >= 7500 (64-bit offsets;
FBX 2016+). The 64-bit branches live in ``_read_node``/``_write_node`` (the
``<QQQ>`` header + 25-byte null sentinel).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FBX_HEADER = b"Kaydara FBX Binary\x20\x20\x00\x1a\x00"


@dataclass
class FbxProperty:
    type_code: str  # single char: Y/C/I/F/D/L/f/d/l/i/b/S/R
    value: Any  # int, float, bytes, list[int|float]
    encoding: int = 0  # 0=raw, 1=zlib (only for array types)


@dataclass
class FbxNode:
    name: bytes
    properties: list[FbxProperty] = field(default_factory=list)
    children: list["FbxNode"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def _read_property(data: bytes, pos: int) -> tuple[FbxProperty, int]:
    tc = chr(data[pos])
    pos += 1
    prim = {"Y": ("<h", 2), "C": ("<B", 1), "I": ("<i", 4),
            "F": ("<f", 4), "D": ("<d", 8), "L": ("<q", 8)}
    arr_type = {"f": ("f", 4), "d": ("d", 8),
                "l": ("q", 8), "i": ("i", 4), "b": ("B", 1)}
    if tc in prim:
        fmt, sz = prim[tc]
        v = struct.unpack_from(fmt, data, pos)[0]
        return FbxProperty(tc, v), pos + sz
    if tc in arr_type:
        item_code, item_sz = arr_type[tc]
        length, encoding, comp_len = struct.unpack_from("<III", data, pos)
        pos += 12
        raw = data[pos:pos + comp_len]
        pos += comp_len
        if encoding == 1:
            raw = zlib.decompress(raw)
        values = list(struct.unpack(f"<{length}{item_code}", raw))
        return FbxProperty(tc, values, encoding=encoding), pos
    if tc == "S" or tc == "R":
        (length,) = struct.unpack_from("<I", data, pos)
        pos += 4
        v = data[pos:pos + length]
        pos += length
        return FbxProperty(tc, v), pos
    raise ValueError(f"Unknown FBX property type code: {tc!r} at pos {pos - 1}")


def _read_node(data: bytes, pos: int, ver: int) -> tuple[FbxNode | None, int]:
    # For v < 7500: 32-bit offsets; for >= 7500: 64-bit.
    if ver >= 7500:
        end_offset, num_props, prop_list_len = struct.unpack_from("<QQQ", data, pos)
        pos += 24
    else:
        end_offset, num_props, prop_list_len = struct.unpack_from("<III", data, pos)
        pos += 12
    name_len = data[pos]
    pos += 1

    # Null terminator (all zeros) signals end of children
    if end_offset == 0:
        return None, pos + name_len  # consume name bytes too (zero for null)

    name = data[pos:pos + name_len]
    pos += name_len

    # Properties
    props: list[FbxProperty] = []
    prop_end = pos + prop_list_len
    while pos < prop_end:
        p, pos = _read_property(data, pos)
        props.append(p)

    # Children (if any)
    children: list[FbxNode] = []
    while pos < end_offset:
        child, pos = _read_node(data, pos, ver)
        if child is None:
            break
        children.append(child)

    return FbxNode(name=name, properties=props, children=children), end_offset


def read_fbx(path: str | Path) -> tuple[int, list[FbxNode], bytes]:
    """Read an FBX binary file and return (version, root_nodes, footer).

    The footer includes the root-level null terminator (13 or 25 bytes of
    zeros) and the trailing magic/version bytes that Roblox and other
    strict FBX parsers validate. Preserving it verbatim is the simplest
    way to produce a byte-level-valid FBX when modifying vertex data.
    """
    data = Path(path).read_bytes()
    if not data.startswith(FBX_HEADER):
        raise ValueError("Not an FBX binary file")
    ver = struct.unpack_from("<I", data, 23)[0]
    # FBX 7500+ (FBX 2016+) uses 64-bit offsets; _read_node handles the <QQQ>
    # header for ver >= 7500. The root-terminator peek matches the offset width
    # (<Q> for 7500+, <I> otherwise) so a 64-bit end_offset is never mis-read
    # as the zero terminator.
    pos = 27
    offset_fmt = "<Q" if ver >= 7500 else "<I"
    roots: list[FbxNode] = []
    while pos < len(data):
        # Peek at end_offset; if 0, that's the root-level terminator
        end_offset = struct.unpack_from(offset_fmt, data, pos)[0]
        if end_offset == 0:
            break
        node, pos = _read_node(data, pos, ver)
        if node is None:
            break
        roots.append(node)
    # Footer starts at the root-level null terminator and runs to EOF
    footer = data[pos:]
    return ver, roots, footer


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def _write_property(p: FbxProperty) -> bytes:
    tc = p.type_code
    out = tc.encode()
    prim = {"Y": "<h", "C": "<B", "I": "<i", "F": "<f", "D": "<d", "L": "<q"}
    arr_type = {"f": "f", "d": "d", "l": "q", "i": "i", "b": "B"}
    if tc in prim:
        return out + struct.pack(prim[tc], p.value)
    if tc in arr_type:
        item_code = arr_type[tc]
        raw = struct.pack(f"<{len(p.value)}{item_code}", *p.value)
        if p.encoding == 1:
            comp = zlib.compress(raw)
            return out + struct.pack("<III", len(p.value), 1, len(comp)) + comp
        else:
            return out + struct.pack("<III", len(p.value), 0, len(raw)) + raw
    if tc == "S" or tc == "R":
        v = p.value if isinstance(p.value, (bytes, bytearray)) else p.value.encode()
        return out + struct.pack("<I", len(v)) + v
    raise ValueError(f"Unknown property type: {tc}")


def _write_node(node: FbxNode, pos: int, ver: int) -> tuple[bytes, int]:
    # Serialise properties first
    prop_bytes = b"".join(_write_property(p) for p in node.properties)

    # Compute header size
    header_size = 13 + len(node.name) if ver < 7500 else 25 + len(node.name)

    # Start with a placeholder for the header; fill in end_offset after
    # children are serialised.
    current_pos = pos + header_size + len(prop_bytes)

    # Serialise children
    child_bytes = b""
    for c in node.children:
        cb, current_pos = _write_node(c, current_pos, ver)
        child_bytes += cb

    # If there are children, add the null terminator record
    if node.children:
        null_size = 13 if ver < 7500 else 25
        child_bytes += b"\x00" * null_size
        current_pos += null_size

    end_offset = current_pos

    # Build header with correct end_offset
    if ver < 7500:
        header = struct.pack("<III", end_offset, len(node.properties), len(prop_bytes))
    else:
        header = struct.pack("<QQQ", end_offset, len(node.properties), len(prop_bytes))
    header += bytes([len(node.name)]) + node.name

    return header + prop_bytes + child_bytes, end_offset


def write_fbx(
    path: str | Path,
    version: int,
    roots: list[FbxNode],
    footer: bytes = b"",
) -> None:
    """Serialise (version, roots) to an FBX binary file.

    ``footer`` should be the byte sequence returned by ``read_fbx()`` —
    it contains the root-level null terminator plus the trailing magic
    bytes that strict parsers (Roblox, Autodesk FBX SDK) require.
    """
    out = bytearray()
    out += FBX_HEADER
    out += struct.pack("<I", version)

    pos = len(out)
    for r in roots:
        rb, pos = _write_node(r, pos, version)
        out += rb

    if footer:
        out += footer
    else:
        # Minimal fallback: just the null terminator (won't pass strict
        # parsers like Roblox's FBX importer).
        null_size = 13 if version < 7500 else 25
        out += b"\x00" * null_size

    Path(path).write_bytes(bytes(out))


# ---------------------------------------------------------------------------
# High-level: handedness mirror
# ---------------------------------------------------------------------------

def _find_geometry_nodes(nodes: list[FbxNode]) -> list[FbxNode]:
    """Recursively find all Geometry nodes in the tree."""
    result = []
    for n in nodes:
        if n.name == b"Geometry":
            result.append(n)
        result.extend(_find_geometry_nodes(n.children))
    return result


def _child(node: FbxNode, name: bytes) -> FbxNode | None:
    for c in node.children:
        if c.name == name:
            return c
    return None


def _negate_axis(values: list[float], axis: int) -> list[float]:
    """Negate one component of each (x,y,z) triple in a flat vertex list.

    Args:
        values: Flat list [x0,y0,z0, x1,y1,z1, ...].
        axis: 0=X, 1=Y, 2=Z.
    """
    out = list(values)
    for i in range(axis, len(out), 3):
        out[i] = -out[i]
    return out


def mirror_fbx_handedness(src_path: str | Path, dst_path: str | Path) -> bool:
    """Negate the appropriate axis to fix left-handed → right-handed handedness.

    The FBX format is right-handed. Unity converts to left-handed on import
    by negating one axis. Roblox keeps right-handed. To make Roblox render
    the mesh the same way Unity does, we negate **both X and Y** in the
    vertices and normals — equivalent to a 180° rotation around the vertical
    (Z) axis. Because negating two axes has determinant +1, it is a proper
    rotation rather than a mirror, so triangle winding is preserved and no
    winding flip is needed. This fixes which side of the mesh shows which
    texture (e.g. asymmetric door/logo features) without flipping vertical
    position or making text upside-down.

    Writes the modified FBX to ``dst_path``. Preserves sub-mesh structure.
    Returns True on success, False if the file format isn't supported.
    """
    try:
        ver, roots, footer = read_fbx(src_path)
    except (ValueError, NotImplementedError, struct.error):
        # struct.error: a truncated/corrupt FBX (e.g. a malformed 7500 file
        # whose 64-bit header runs past EOF). Before 7500 reads were enabled
        # such files hit the early NotImplementedError; now they reach the
        # struct unpack. Degrade to a raw upload rather than crash the phase.
        return False

    geoms = _find_geometry_nodes(roots)
    if not geoms:
        return False

    # Negate both X and Y axes. This is equivalent to a 180° rotation
    # around Z (the vertical axis in these FBX files). Unlike a single-
    # axis negate (which is a mirror and reverses triangle winding),
    # negating two axes is a proper rotation (determinant = +1) that
    # preserves triangle winding — so no winding flip is needed and
    # faces remain visible. The mesh simply faces the opposite
    # direction, fixing which side shows which texture (e.g. SEA vs
    # A32 on the SimpleFPS doors) without making text upside-down.

    for geom in geoms:
        v_node = _child(geom, b"Vertices")
        if v_node and v_node.properties and v_node.properties[0].type_code == "d":
            v_node.properties[0].value = _negate_axis(v_node.properties[0].value, 0)  # X
            v_node.properties[0].value = _negate_axis(v_node.properties[0].value, 1)  # Y

        # No winding flip needed — negating two axes is a rotation
        # (det = +1), not a reflection, so triangle handedness is
        # preserved and faces render correctly without backface culling.

        # Negate X and Y in normals
        for ln in geom.children:
            if ln.name == b"LayerElementNormal":
                for nc in ln.children:
                    if nc.name in (b"Normals", b"NormalsW"):
                        if nc.properties and nc.properties[0].type_code == "d":
                            nc.properties[0].value = _negate_axis(nc.properties[0].value, 0)
                            nc.properties[0].value = _negate_axis(nc.properties[0].value, 1)

    write_fbx(dst_path, ver, roots, footer=footer)
    return True
