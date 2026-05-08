"""
rbxl_binary_writer.py — Converts XML .rbxl/.rbxlx to Roblox binary format.

The Roblox Open Cloud Place API only accepts binary .rbxl files.
This module reads the XML output from rbxlx_writer.py and serialises it
into the binary chunk format documented at:
  https://dom.rojo.space/binary
  https://github.com/RobloxAPI/spec/blob/master/formats/rbxl.md

No other module is imported here (except stdlib + lz4).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

import lz4.block

# -- File constants ----------------------------------------------------------

MAGIC = b"<roblox!\x89\xff\x0d\x0a\x1a\x0a"
FORMAT_VERSION = 0

# -- Property type IDs -------------------------------------------------------

TYPE_STRING = 0x01
TYPE_BOOL = 0x02
TYPE_INT32 = 0x03
TYPE_FLOAT = 0x04
TYPE_DOUBLE = 0x05
TYPE_UDIM = 0x06
TYPE_UDIM2 = 0x07
TYPE_BRICKCOLOR = 0x0B
TYPE_COLOR3 = 0x0C
TYPE_VECTOR2 = 0x0D
TYPE_VECTOR3 = 0x0E
TYPE_CFRAME = 0x10
TYPE_ENUM = 0x12
TYPE_REFERENT = 0x13
TYPE_NUMBERSEQUENCE = 0x15
TYPE_COLORSEQUENCE = 0x16
TYPE_NUMBERRANGE = 0x17
TYPE_COLOR3UINT8 = 0x1A
# Map XML element tags -> binary type IDs.
_XML_TYPE_MAP: dict[str, int] = {
    "string": TYPE_STRING,
    "bool": TYPE_BOOL,
    "int": TYPE_INT32,
    "float": TYPE_FLOAT,
    "double": TYPE_DOUBLE,
    "Vector2": TYPE_VECTOR2,
    "Vector3": TYPE_VECTOR3,
    "CoordinateFrame": TYPE_CFRAME,
    "Color3": TYPE_COLOR3,
    "Color3uint8": TYPE_COLOR3UINT8,
    "BrickColor": TYPE_BRICKCOLOR,
    "token": TYPE_ENUM,
    # Roblox Studio expects ContentId properties (MeshId, TextureId, ColorMap,
    # SoundId) as type 0x01 (String), not 0x20 (Content).  Writing 0x20 causes
    # "Unexpected format 32 (expected 1) << ContentId" on load.
    "Content": TYPE_STRING,
    "ProtectedString": TYPE_STRING,
    # BinaryString carries base64-encoded raw bytes in XML
    # (AttributesSerialize blobs etc.) The Roblox binary format groups
    # BinaryString under property type 0x01 (String), with the raw decoded
    # bytes as the payload — see https://dom.rojo.space/binary-strings.html.
    # _parse_property base64-decodes; _encode_string handles arbitrary bytes.
    "BinaryString": TYPE_STRING,
    "UDim2": TYPE_UDIM2,
    "NumberRange": TYPE_NUMBERRANGE,
    "NumberSequence": TYPE_NUMBERSEQUENCE,
    "ColorSequence": TYPE_COLORSEQUENCE,
}

# -- BrickColor name -> integer ----------------------------------------------

_BRICK_COLOR_MAP: dict[str, int] = {
    "White": 1, "Grey": 194, "Light yellow": 24, "Brick yellow": 5,
    "Light green (Mint)": 6, "Light reddish violet": 9, "Pastel Blue": 11,
    "Light orange brown": 12, "Nougat": 18, "Bright red": 21,
    "Med. reddish violet": 22, "Bright blue": 23, "Bright yellow": 24,
    "Earth orange": 25, "Black": 26, "Dark grey": 27,
    "Dark green": 28, "Medium green": 29, "Lig. Yellowich orange": 36,
    "Bright green": 37, "Dark orange": 38, "Light bluish violet": 39,
    "Transparent": 40, "Tr. Red": 41, "Tr. Lg blue": 42, "Tr. Blue": 43,
    "Tr. Yellow": 44, "Light blue": 45, "Tr. Flu. Reddish orange": 47,
    "Tr. Green": 48, "Tr. Flu. Green": 49, "Phosph. White": 50,
    "Light red": 100, "Medium red": 101, "Medium blue": 102,
    "Light grey": 103, "Bright violet": 104, "Br. yellowish orange": 105,
    "Bright orange": 106, "Bright bluish green": 107, "Earth yellow": 108,
    "Bright bluish violet": 110, "Tr. Brown": 111, "Medium bluish violet": 112,
    "Tr. Medi. reddish violet": 113, "Med. yellowish green": 115,
    "Med. bluish green": 116, "Light bluish green": 118,
    "Br. yellowish green": 119, "Lig. yellowish green": 120,
    "Med. yellowish orange": 121, "Br. reddish orange": 123,
    "Bright reddish violet": 124, "Light stone grey": 125,
    "Dark stone grey": 126, "Lemon metallic": 127,
    "Light metallic": 131, "Sand yellow metallic": 133,
    "Copper metallic": 134, "Silver flip/flop": 136,
    "Sand yellow": 138, "Sand red": 139, "Sand blue": 140,
    "Sand green": 141, "Sand violet": 142, "Cool yellow": 148,
    "Faded green": 151, "Really black": 26, "Really red": 1004,
    "Deep orange": 1005, "Alder": 1006, "Dusty Rose": 1007,
    "Olive": 1008, "New Yeller": 1009, "Really blue": 1010,
    "Navy blue": 1011, "Deep blue": 1012, "Magenta": 1013,
    "Pink": 1014, "Teal": 1018, "Toothpaste": 1019,
    "Lime green": 1020, "Camo": 1021, "Grime": 1022,
    "Lavender": 1023, "Pastel light blue": 1024,
    "Pastel orange": 1025, "Pastel violet": 1026,
    "Pastel blue-green": 1027, "Pastel green": 1028,
    "Pastel yellow": 1029, "Pastel brown": 1030,
    "Royal purple": 1031, "Hot pink": 1032,
    "Medium stone grey": 194, "Fossil": 1033,
    "Maroon": 1034, "Gold": 1035, "Daisy orange": 1036,
    "Flint": 1037, "Smoky grey": 1038, "Crimson": 1039,
    "Mint": 1040, "Baby blue": 1041, "Carnation pink": 1042,
    "Persimmon": 1043, "Institutional white": 1044,
    "Mid gray": 1002, "Medium stone gray": 194,
}

# Roblox services that get is_service=True in INST chunks.
_SERVICES = {
    "Workspace", "Lighting", "ReplicatedStorage", "ServerScriptService",
    "ServerStorage", "StarterGui", "StarterPack", "StarterPlayer",
    "StarterPlayerScripts", "StarterCharacterScripts", "SoundService",
    "Chat", "LocalizationService", "TestService",
}


# -- Data structures ---------------------------------------------------------

@dataclass
class _Instance:
    class_name: str
    referent: int
    parent_referent: int  # -1 for root-level
    properties: dict[str, tuple[int, object]]  # name -> (type_id, parsed_value)
    is_service: bool = False


# -- Encoding helpers --------------------------------------------------------

def _zigzag_i32(n: int) -> int:
    n = n & 0xFFFFFFFF  # truncate to 32-bit unsigned
    signed = n if n < 0x80000000 else n - 0x100000000
    return ((signed << 1) ^ (signed >> 31)) & 0xFFFFFFFF


def _rotate_float_bits(f: float) -> int:
    """Rotate IEEE-754 float bits for better LZ4 compression."""
    bits = struct.unpack(">I", struct.pack(">f", f))[0]
    return ((bits << 1) | (bits >> 31)) & 0xFFFFFFFF


def _interleave_u32(values: list[int]) -> bytes:
    """Byte-interleave an array of uint32 values (big-endian per value)."""
    n = len(values)
    packed = [struct.pack(">I", v & 0xFFFFFFFF) for v in values]
    result = bytearray(n * 4)
    for byte_idx in range(4):
        for val_idx in range(n):
            result[byte_idx * n + val_idx] = packed[val_idx][byte_idx]
    return bytes(result)


def _encode_string(s: str | bytes) -> bytes:
    """Encode a Roblox binary string property value (length-prefixed payload).

    Accepts either text (UTF-8 encoded) or raw bytes (BinaryString blobs
    such as AttributesSerialize, where the XML carries base64-encoded
    arbitrary bytes — see https://dom.rojo.space/binary-strings.html).
    """
    if isinstance(s, bytes):
        return struct.pack("<I", len(s)) + s
    encoded = s.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _write_chunk(name: bytes, data: bytes, compress: bool = True) -> bytes:
    """Build a chunk frame: 4-byte name + 4 compressed_len + 4 uncompressed_len + 4 reserved + payload."""
    assert len(name) == 4
    uncompressed_len = len(data)

    if compress and uncompressed_len > 0:
        compressed = lz4.block.compress(data, store_size=False)
        # Only use compression if it actually saves space.
        if len(compressed) < uncompressed_len:
            header = struct.pack("<4sIII", name, len(compressed), uncompressed_len, 0)
            return header + compressed

    # Uncompressed: compressed_len = 0 signals raw data.
    header = struct.pack("<4sIII", name, 0, uncompressed_len, 0)
    return header + data


# -- XML parsing -------------------------------------------------------------

def _parse_vector3(el: ET.Element) -> tuple[float, float, float]:
    x = float(el.findtext("X", "0"))
    y = float(el.findtext("Y", "0"))
    z = float(el.findtext("Z", "0"))
    return (x, y, z)


def _parse_color3(el: ET.Element) -> tuple[float, float, float]:
    r = float(el.findtext("R", "0"))
    g = float(el.findtext("G", "0"))
    b = float(el.findtext("B", "0"))
    return (r, g, b)


def _parse_cframe(el: ET.Element) -> tuple[float, ...]:
    """Parse CoordinateFrame -> (x, y, z, r00..r22)."""
    x = float(el.findtext("X", "0"))
    y = float(el.findtext("Y", "0"))
    z = float(el.findtext("Z", "0"))
    r00 = float(el.findtext("R00", "1"))
    r01 = float(el.findtext("R01", "0"))
    r02 = float(el.findtext("R02", "0"))
    r10 = float(el.findtext("R10", "0"))
    r11 = float(el.findtext("R11", "1"))
    r12 = float(el.findtext("R12", "0"))
    r20 = float(el.findtext("R20", "0"))
    r21 = float(el.findtext("R21", "0"))
    r22 = float(el.findtext("R22", "1"))
    return (x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22)


def _parse_udim2(el: ET.Element) -> tuple[float, int, float, int]:
    xs = float(el.findtext("XS", "0"))
    xo = int(el.findtext("XO", "0"))
    ys = float(el.findtext("YS", "0"))
    yo = int(el.findtext("YO", "0"))
    return (xs, xo, ys, yo)


def _parse_vector2(el: ET.Element) -> tuple[float, float]:
    x = float(el.findtext("X", "0"))
    y = float(el.findtext("Y", "0"))
    return (x, y)


def _parse_number_range(el: ET.Element) -> tuple[float, float]:
    parts = (el.text or "0 0").split()
    return (float(parts[0]), float(parts[1]) if len(parts) > 1 else float(parts[0]))


def _parse_number_sequence(el: ET.Element) -> list[tuple[float, float, float]]:
    keypoints = []
    for kp in el.findall("Keypoint"):
        t = float(kp.get("time", "0"))
        v = float(kp.get("value", "0"))
        e = float(kp.get("envelope", "0"))
        keypoints.append((t, v, e))
    if not keypoints:
        keypoints = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
    return keypoints


def _parse_color_sequence(el: ET.Element) -> list[tuple[float, float, float, float, int]]:
    """Parse a ColorSequence XML element into keypoints: (time, r, g, b, envelope)."""
    keypoints: list[tuple[float, float, float, float, int]] = []
    for kp in el.findall("Keypoint"):
        t = float(kp.get("time", "0"))
        color_str = kp.get("color", "1 1 1 0")
        parts = color_str.split()
        r = float(parts[0]) if len(parts) > 0 else 1.0
        g = float(parts[1]) if len(parts) > 1 else 1.0
        b = float(parts[2]) if len(parts) > 2 else 1.0
        e = int(float(parts[3])) if len(parts) > 3 else 0
        keypoints.append((t, r, g, b, e))
    if not keypoints:
        keypoints = [(0.0, 1.0, 1.0, 1.0, 0), (1.0, 1.0, 1.0, 1.0, 0)]
    return keypoints


def _parse_property(el: ET.Element) -> tuple[int, object] | None:
    """Parse an XML property element into (type_id, value). Returns None for unknown types."""
    tag = el.tag
    type_id = _XML_TYPE_MAP.get(tag)
    if type_id is None:
        return None

    if tag in ("string", "ProtectedString"):
        return (TYPE_STRING, el.text or "")
    elif tag == "BinaryString":
        # Base64-decoded raw bytes (AttributesSerialize blobs etc.). XML
        # may include a leading newline / CDATA wrapper; strip whitespace
        # before decoding. Empty body decodes to empty bytes.
        import base64 as _b64
        text = (el.text or "").strip()
        try:
            raw = _b64.b64decode(text) if text else b""
        except (ValueError, _b64.binascii.Error):
            raw = b""
        return (TYPE_STRING, raw)
    elif tag == "Content":
        # Roblox XML uses <Content><url>value</url></Content> for asset refs.
        # Fall back to el.text for legacy flat-text Content elements.
        url_child = el.find("url")
        value = url_child.text if url_child is not None and url_child.text else (el.text or "")
        return (TYPE_STRING, value)
    elif tag == "Color3uint8":
        # MeshPart uses ``Color3uint8`` instead of ``Color3``. The XML
        # value is a uint32 packing 0xRRGGBB (alpha unused). Roblox
        # binary type id 0x1A stores 3 component arrays: R, G, B as
        # bytes (no alpha) — see https://dom.rojo.space/binary.html.
        try:
            packed = int(el.text or "0")
        except ValueError:
            packed = 0
        r = (packed >> 16) & 0xFF
        g = (packed >> 8) & 0xFF
        b = packed & 0xFF
        return (TYPE_COLOR3UINT8, (r, g, b))
    elif tag == "bool":
        return (TYPE_BOOL, (el.text or "").lower() == "true")
    elif tag == "int":
        return (TYPE_INT32, int(el.text or "0"))
    elif tag == "float":
        return (TYPE_FLOAT, float(el.text or "0"))
    elif tag == "double":
        return (TYPE_DOUBLE, float(el.text or "0"))
    elif tag == "Vector3":
        return (TYPE_VECTOR3, _parse_vector3(el))
    elif tag == "Vector2":
        return (TYPE_VECTOR2, _parse_vector2(el))
    elif tag == "Color3":
        return (TYPE_COLOR3, _parse_color3(el))
    elif tag == "CoordinateFrame":
        return (TYPE_CFRAME, _parse_cframe(el))
    elif tag == "BrickColor":
        name = el.text or "Medium stone grey"
        return (TYPE_BRICKCOLOR, _BRICK_COLOR_MAP.get(name, 194))
    elif tag == "token":
        # Tokens are stored as int32 enums.  The XML may already contain a
        # numeric string ("1", "2") or a name ("SmoothPlastic").  Try int
        # first; fall back to 0.
        txt = el.text or "0"
        try:
            return (TYPE_ENUM, int(txt))
        except ValueError:
            return (TYPE_ENUM, 0)
    elif tag == "UDim2":
        return (TYPE_UDIM2, _parse_udim2(el))
    elif tag == "NumberRange":
        return (TYPE_NUMBERRANGE, _parse_number_range(el))
    elif tag == "NumberSequence":
        return (TYPE_NUMBERSEQUENCE, _parse_number_sequence(el))
    elif tag == "ColorSequence":
        return (TYPE_COLORSEQUENCE, _parse_color_sequence(el))
    return None


def _walk_items(parent: ET.Element, parent_ref: int, instances: list[_Instance], counter: list[int]) -> None:
    """Recursively walk <Item> elements and build _Instance objects."""
    for item_el in parent.findall("Item"):
        class_name = item_el.get("class", "")
        referent = counter[0]
        counter[0] += 1

        props: dict[str, tuple[int, object]] = {}
        props_el = item_el.find("Properties")
        if props_el is not None:
            for prop_el in props_el:
                prop_name = prop_el.get("name")
                if prop_name is None:
                    continue
                parsed = _parse_property(prop_el)
                if parsed is not None:
                    props[prop_name] = parsed

        inst = _Instance(
            class_name=class_name,
            referent=referent,
            parent_referent=parent_ref,
            properties=props,
            is_service=class_name in _SERVICES,
        )
        instances.append(inst)

        # Recurse into child items.
        _walk_items(item_el, referent, instances, counter)


# -- Property serialisation --------------------------------------------------

def _default_for_type(type_id: int) -> object:
    """Return a sensible default value for a given property type."""
    defaults: dict[int, object] = {
        TYPE_STRING: "",
        TYPE_BOOL: False,
        TYPE_INT32: 0,
        TYPE_FLOAT: 0.0,
        TYPE_DOUBLE: 0.0,
        TYPE_VECTOR2: (0.0, 0.0),
        TYPE_VECTOR3: (0.0, 0.0, 0.0),
        TYPE_COLOR3: (0.0, 0.0, 0.0),
        TYPE_COLOR3UINT8: (162, 162, 162),
        TYPE_CFRAME: (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
        TYPE_BRICKCOLOR: 194,
        TYPE_ENUM: 0,
        TYPE_UDIM2: (0.0, 0, 0.0, 0),
        TYPE_NUMBERRANGE: (0.0, 0.0),
        TYPE_NUMBERSEQUENCE: [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)],
        TYPE_COLORSEQUENCE: [(0.0, 1.0, 1.0, 1.0, 0), (1.0, 1.0, 1.0, 1.0, 0)],
        TYPE_REFERENT: -1,
    }
    return defaults.get(type_id, "")


def _serialise_prop_values(type_id: int, values: list[object]) -> bytes:
    """Serialise an array of property values for one PROP chunk."""
    n = len(values)
    buf = bytearray()

    if type_id == TYPE_STRING:
        for v in values:
            # BinaryString blobs (e.g. AttributesSerialize) decode to raw
            # bytes via _parse_property; keep them as-is. Everything else
            # round-trips through str() — _encode_string accepts both.
            buf += _encode_string(v if isinstance(v, (str, bytes)) else str(v))

    elif type_id == TYPE_BOOL:
        for v in values:
            buf += struct.pack("B", 1 if v else 0)

    elif type_id == TYPE_INT32:
        buf += _interleave_u32([_zigzag_i32(int(v)) for v in values])

    elif type_id == TYPE_FLOAT:
        buf += _interleave_u32([_rotate_float_bits(float(v)) for v in values])

    elif type_id == TYPE_DOUBLE:
        for v in values:
            buf += struct.pack("<d", float(v))

    elif type_id == TYPE_VECTOR2:
        xs = [_rotate_float_bits(v[0]) for v in values]
        ys = [_rotate_float_bits(v[1]) for v in values]
        buf += _interleave_u32(xs)
        buf += _interleave_u32(ys)

    elif type_id == TYPE_VECTOR3:
        xs = [_rotate_float_bits(v[0]) for v in values]
        ys = [_rotate_float_bits(v[1]) for v in values]
        zs = [_rotate_float_bits(v[2]) for v in values]
        buf += _interleave_u32(xs)
        buf += _interleave_u32(ys)
        buf += _interleave_u32(zs)

    elif type_id == TYPE_COLOR3:
        rs = [_rotate_float_bits(v[0]) for v in values]
        gs = [_rotate_float_bits(v[1]) for v in values]
        bs = [_rotate_float_bits(v[2]) for v in values]
        buf += _interleave_u32(rs)
        buf += _interleave_u32(gs)
        buf += _interleave_u32(bs)

    elif type_id == TYPE_COLOR3UINT8:
        # https://dom.rojo.space/binary.html — Color3uint8 stores three
        # component arrays (R bytes, G bytes, B bytes), no alpha,
        # no interleave. Each component is a single byte per instance.
        rs = bytearray(int(v[0]) & 0xFF for v in values)
        gs = bytearray(int(v[1]) & 0xFF for v in values)
        bs = bytearray(int(v[2]) & 0xFF for v in values)
        buf += rs
        buf += gs
        buf += bs

    elif type_id == TYPE_CFRAME:
        # For each CFrame: 1 byte rotation_id.
        # rotation_id 0 -> full 3x3 matrix follows (9 floats).
        # Then position Vector3 at the end (interleaved).
        rot_data = bytearray()
        positions_x: list[int] = []
        positions_y: list[int] = []
        positions_z: list[int] = []

        for v in values:
            # v = (x, y, z, r00, r01, r02, r10, r11, r12, r20, r21, r22)
            x, y, z = v[0], v[1], v[2]
            positions_x.append(_rotate_float_bits(x))
            positions_y.append(_rotate_float_bits(y))
            positions_z.append(_rotate_float_bits(z))

            # Check for identity rotation.
            r = v[3:]  # 9 floats
            is_identity = (
                abs(r[0] - 1) < 1e-6 and abs(r[1]) < 1e-6 and abs(r[2]) < 1e-6
                and abs(r[3]) < 1e-6 and abs(r[4] - 1) < 1e-6 and abs(r[5]) < 1e-6
                and abs(r[6]) < 1e-6 and abs(r[7]) < 1e-6 and abs(r[8] - 1) < 1e-6
            )
            if is_identity:
                rot_data += struct.pack("B", 0x02)  # identity rotation ID
            else:
                rot_data += struct.pack("B", 0x00)  # full matrix follows
                for component in r:
                    rot_data += struct.pack("<f", component)

        buf += rot_data
        buf += _interleave_u32(positions_x)
        buf += _interleave_u32(positions_y)
        buf += _interleave_u32(positions_z)

    elif type_id == TYPE_BRICKCOLOR:
        buf += _interleave_u32([int(v) for v in values])

    elif type_id == TYPE_ENUM:
        buf += _interleave_u32([int(v) for v in values])

    elif type_id == TYPE_UDIM2:
        # UDim2 = (xs, xo, ys, yo) -> 4 interleaved arrays
        xs_vals = [_rotate_float_bits(v[0]) for v in values]
        xo_vals = [_zigzag_i32(int(v[1])) for v in values]
        ys_vals = [_rotate_float_bits(v[2]) for v in values]
        yo_vals = [_zigzag_i32(int(v[3])) for v in values]
        buf += _interleave_u32(xs_vals)
        buf += _interleave_u32(xo_vals)
        buf += _interleave_u32(ys_vals)
        buf += _interleave_u32(yo_vals)

    elif type_id == TYPE_NUMBERRANGE:
        for v in values:
            buf += struct.pack("<ff", float(v[0]), float(v[1]))

    elif type_id == TYPE_NUMBERSEQUENCE:
        for v in values:
            keypoints = v
            buf += struct.pack("<I", len(keypoints))
            for t, val, env in keypoints:
                buf += struct.pack("<fff", t, val, env)

    elif type_id == TYPE_COLORSEQUENCE:
        # Same layout as NumberSequence but keypoints have (time, r, g, b, envelope)
        for v in values:
            keypoints = v
            buf += struct.pack("<I", len(keypoints))
            for t, r, g, b, e in keypoints:
                buf += struct.pack("<ffffi", t, r, g, b, e)

    elif type_id == TYPE_REFERENT:
        refs = [int(v) for v in values]
        # Delta-encode then zigzag.
        deltas: list[int] = []
        prev = 0
        for r in refs:
            deltas.append(r - prev)
            prev = r
        buf += _interleave_u32([_zigzag_i32(d) for d in deltas])

    return bytes(buf)


# -- Chunk builders ----------------------------------------------------------

def _build_meta() -> bytes:
    """META chunk with ExplicitAutoJoints = true."""
    buf = bytearray()
    num_entries = 1
    buf += struct.pack("<I", num_entries)
    buf += _encode_string("ExplicitAutoJoints")
    buf += _encode_string("true")
    return _write_chunk(b"META", bytes(buf))


def _build_inst(class_idx: int, class_name: str, referents: list[int], is_service: bool) -> bytes:
    """INST chunk for one class."""
    buf = bytearray()
    buf += struct.pack("<I", class_idx)
    buf += _encode_string(class_name)
    buf += struct.pack("B", 1 if is_service else 0)
    buf += struct.pack("<I", len(referents))

    # Referents are delta-encoded then zigzag-encoded then interleaved.
    deltas: list[int] = []
    prev = 0
    for r in referents:
        deltas.append(r - prev)
        prev = r
    buf += _interleave_u32([_zigzag_i32(d) for d in deltas])

    if is_service:
        # Service format flag: one byte per instance (always 1 = is service).
        for _ in referents:
            buf += struct.pack("B", 1)

    return _write_chunk(b"INST", bytes(buf))


def _build_prop(class_idx: int, prop_name: str, type_id: int, values: list[object]) -> bytes:
    """PROP chunk for one property of one class."""
    buf = bytearray()
    buf += struct.pack("<I", class_idx)
    buf += _encode_string(prop_name)
    buf += struct.pack("B", type_id)
    buf += _serialise_prop_values(type_id, values)
    return _write_chunk(b"PROP", bytes(buf))


def _build_prnt(children: list[int], parents: list[int]) -> bytes:
    """PRNT chunk encoding parent-child relationships."""
    buf = bytearray()
    buf += struct.pack("B", 0)  # version
    buf += struct.pack("<I", len(children))

    # Children referents -- delta + zigzag + interleave.
    child_deltas: list[int] = []
    prev = 0
    for c in children:
        child_deltas.append(c - prev)
        prev = c
    buf += _interleave_u32([_zigzag_i32(d) for d in child_deltas])

    # Parent referents -- delta + zigzag + interleave.
    parent_deltas: list[int] = []
    prev = 0
    for p in parents:
        parent_deltas.append(p - prev)
        prev = p
    buf += _interleave_u32([_zigzag_i32(d) for d in parent_deltas])

    return _write_chunk(b"PRNT", bytes(buf))


def _build_end() -> bytes:
    """END chunk -- must be uncompressed and contains </roblox>."""
    return _write_chunk(b"END\x00", b"</roblox>", compress=False)


# -- Public API --------------------------------------------------------------

def xml_to_binary(xml_path: str | Path, binary_path: str | Path | None = None) -> Path:
    """
    Convert an XML .rbxl/.rbxlx file to Roblox binary .rbxl format.

    Args:
        xml_path: Path to the input XML place file.
        binary_path: Output path for the binary file.
                     Defaults to same path with .rbxl extension.

    Returns:
        Path to the written binary file.
    """
    xml_path = Path(xml_path)
    if binary_path is None:
        binary_path = xml_path.with_suffix(".rbxl")
    binary_path = Path(binary_path)

    # -- Parse XML -----------------------------------------------------------
    tree = ET.parse(xml_path)
    root = tree.getroot()

    instances: list[_Instance] = []
    counter = [0]  # mutable counter for referent assignment
    _walk_items(root, -1, instances, counter)

    if not instances:
        raise ValueError(f"No <Item> elements found in {xml_path}")

    total_instances = len(instances)

    # -- Group by class ------------------------------------------------------
    class_order: list[str] = []  # preserves first-seen order
    class_instances: dict[str, list[_Instance]] = {}
    for inst in instances:
        if inst.class_name not in class_instances:
            class_order.append(inst.class_name)
            class_instances[inst.class_name] = []
        class_instances[inst.class_name].append(inst)

    class_count = len(class_order)
    class_idx_map = {name: idx for idx, name in enumerate(class_order)}

    # -- Build binary --------------------------------------------------------
    output = bytearray()

    # File header (32 bytes).
    output += MAGIC  # 14 bytes
    output += struct.pack("<H", FORMAT_VERSION)  # 2 bytes
    output += struct.pack("<I", class_count)  # 4 bytes
    output += struct.pack("<I", total_instances)  # 4 bytes
    output += b"\x00" * 8  # 8 bytes reserved

    # META chunk.
    output += _build_meta()

    # INST chunks -- one per class.
    for class_name in class_order:
        insts = class_instances[class_name]
        referents = [i.referent for i in insts]
        is_service = insts[0].is_service
        idx = class_idx_map[class_name]
        output += _build_inst(idx, class_name, referents, is_service)

    # PROP chunks -- one per property per class.
    for class_name in class_order:
        insts = class_instances[class_name]
        idx = class_idx_map[class_name]

        # Discover all (prop_name, type_id) pairs used by any instance of this class.
        prop_schema: dict[str, int] = {}  # prop_name -> type_id
        for inst in insts:
            for pname, (tid, _) in inst.properties.items():
                if pname not in prop_schema:
                    prop_schema[pname] = tid

        # Emit one PROP chunk per property.
        for prop_name, type_id in sorted(prop_schema.items()):
            values: list[object] = []
            for inst in insts:
                if prop_name in inst.properties:
                    values.append(inst.properties[prop_name][1])
                else:
                    values.append(_default_for_type(type_id))
            output += _build_prop(idx, prop_name, type_id, values)

    # PRNT chunk.
    children = [inst.referent for inst in instances]
    parents = [inst.parent_referent for inst in instances]
    output += _build_prnt(children, parents)

    # END chunk.
    output += _build_end()

    # -- Write ---------------------------------------------------------------
    binary_path.parent.mkdir(parents=True, exist_ok=True)
    binary_path.write_bytes(bytes(output))
    return binary_path
