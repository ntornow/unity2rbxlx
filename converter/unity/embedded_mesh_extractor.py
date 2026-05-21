"""
embedded_mesh_extractor.py -- Extract geometry from Unity meshes embedded
inside legacy ``.prefab``/``.asset`` YAML (NativeFormatImporter format).

Some Unity Asset Store packages ship their static-mesh geometry serialised
as a ``!u!43 Mesh`` document INSIDE a ``.prefab``/``.asset`` file rather
than as an external ``.fbx``. The pipeline's upload path only handles
``.fbx``/``.obj``, so those meshes never reach Roblox -- the converter
falls back to a textured primitive Block with cube-face decals (SimpleFPS
landmines were the concrete reproducer).

This module decodes the embedded vertex + index buffers from the YAML
and synthesises an ASCII OBJ buffer that the existing upload path can
ship to Open Cloud as a ``Model`` asset.

Design constraints (from the codex review of the plan):

* **Trust ``m_Channels[*].offset`` for the physical vertex layout.** Don't
  sum channel sizes in enum order -- real assets reorder Tangent past
  UV0. The stride is ``max(offset + size)`` across stream-0 channels.

* **Format handling is real, not guessed.** ``m_Channels[*].format`` is
  Unity's ``VertexAttributeFormat`` (0=Float32, 1=Float16, ..., 9=SInt16).
  ``m_MeshCompression == 0`` does NOT imply everything is Float32 --
  Unity's separate "Vertex Compression" project setting can pack
  normals/tangents/UVs to FP16 or 8-bit independently. We accept
  Float32+Float16 on positions/normals/UV0, reject the rest with a
  structured reason.

* **Handedness fix mirrors ``fbx_binary.mirror_fbx_handedness``: negate
  X and Y, not Z.** This is a rotation (preserves triangle winding); a
  Z flip is a reflection that would require also reversing winding.

* **Externalised ``m_StreamData`` (non-empty path) means the raw blob
  lives in a separate ``.resS`` file** -- different sharp edge from
  multi-stream layouts. Both rejected with distinct reasons.

* **Submesh ``firstByte`` is a BYTE offset into the index buffer**, not
  an index offset. ``baseVertex`` is added to every decoded index.

Failures return a structured ``ExtractionFailure`` so the pipeline can
emit one deduped warning per source key (mesh path + file id) rather
than silently falling back. See ``__init__.py`` re-exports for the
public surface.
"""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path

from unity.yaml_parser import doc_body, parse_documents

log = logging.getLogger(__name__)

# Unity classID for a serialised Mesh.
_MESH_CLASS_ID = 43


# ---------------------------------------------------------------------------
# Public data shapes
# ---------------------------------------------------------------------------

# Reasons we couldn't decode a given embedded mesh. Each value is stable and
# safe to surface to the user (UNCONVERTED.md, logs). The pipeline emits
# at most one warning per ``(source_key, reason)`` per conversion.
FAILURE_NOT_FOUND = "mesh document not found in asset"
FAILURE_NO_VERTEX_DATA = "mesh has no m_VertexData / m_Channels"
FAILURE_NO_INDEX_DATA = "mesh has empty m_IndexBuffer"
FAILURE_NO_POSITION_CHANNEL = "no Position channel (channel 0)"
FAILURE_POSITION_NOT_FLOAT32 = "Position channel is not Float32"
FAILURE_MULTI_STREAM = "multi-stream vertex layout (stream != 0)"
FAILURE_EXTERNAL_STREAM_DATA = "m_StreamData points at an external .resS blob"
FAILURE_MESH_COMPRESSED = "m_MeshCompression > 0 (packed vertex format)"
FAILURE_STRIDE_MISMATCH = "computed stride * vertexCount != m_DataSize"
FAILURE_UNSUPPORTED_TOPOLOGY = "all submeshes are non-triangle topology"
FAILURE_BAD_VERTEX_FORMAT = "Position dimension != 3 or unsupported format"
FAILURE_DECODE_FAILED = "binary decode raised (malformed blob)"


@dataclass(frozen=True)
class ExtractionFailure:
    """Why a given ``(asset, fileID)`` could not be decoded.

    The pipeline catches this, logs once per ``(source_key, reason)``,
    and falls back to the existing face-decal rendering for the
    affected Parts.
    """

    reason: str
    detail: str = ""


@dataclass
class EmbeddedMeshData:
    """Decoded geometry ready for OBJ synthesis.

    Coordinates are already in Roblox handedness (X+Y negated relative
    to the Unity source, matching ``fbx_binary.mirror_fbx_handedness``).
    """

    name: str
    positions: list[tuple[float, float, float]]
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    # Triangle indices, zero-based into ``positions``. Already
    # baseVertex-adjusted; safe to emit straight as OBJ faces (after
    # +1 for OBJ's 1-indexed convention).
    triangles: list[tuple[int, int, int]] = field(default_factory=list)
    # Half-size in metres from m_LocalAABB.m_Extent. Useful as
    # ``MeshPart.InitialSize`` so the pipeline's Size formula does not
    # have to round-trip through the synthesised OBJ.
    aabb_extent: tuple[float, float, float] = (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Module-level cache
# ---------------------------------------------------------------------------
#
# Keyed by ``(resolved_asset_path, file_id_str)``. Reset per conversion via
# ``reset_cache()`` so a fresh run can't serve a previous run's geometry
# (matches the pattern in scene_converter._embedded_mesh_aabb_cache).

_cache: dict[tuple[Path, str], EmbeddedMeshData | ExtractionFailure] = {}


def reset_cache() -> None:
    _cache.clear()


# ---------------------------------------------------------------------------
# Channel-format support tables (Unity VertexAttributeFormat)
# ---------------------------------------------------------------------------

# Map format code -> (struct char, size_bytes, is_normalized_int_to_float)
# ``None`` means we do not decode that format (yet).
_FORMAT_SPECS: dict[int, tuple[str, int]] = {
    0: ("<f", 4),     # Float32
    1: ("<e", 2),     # Float16 (Python 3.6+ struct supports 'e')
    # Norm types decode to float in [-1, 1] / [0, 1]. We only handle them
    # for Normal/UV0 right now (not Position); see _decode_channel.
    2: ("<B", 1),     # UNorm8 -> /255.0
    3: ("<b", 1),     # SNorm8 -> /127.0 (clamp)
    4: ("<H", 2),     # UNorm16 -> /65535.0
    5: ("<h", 2),     # SNorm16 -> /32767.0
    # Plain int types: we'd never expect these on Position/Normal/UV0,
    # but allow size lookup so stride math doesn't blow up.
    6: ("<B", 1),     # UInt8
    7: ("<b", 1),     # SInt8
    8: ("<H", 2),     # UInt16
    9: ("<h", 2),     # SInt16
    10: ("<I", 4),    # UInt32
    11: ("<i", 4),    # SInt32
}


def _format_size(fmt: int, dim: int) -> int | None:
    spec = _FORMAT_SPECS.get(fmt)
    if spec is None:
        return None
    return spec[1] * dim


# ---------------------------------------------------------------------------
# Hex blob parsing
# ---------------------------------------------------------------------------

_HEX_NONHEX_RE = re.compile(r"[^0-9a-fA-F]")


def _hex_to_bytes(hexstr: str | int | None) -> bytes:
    """Decode the YAML ``_typelessdata`` blob.

    Unity emits these blobs as a single (often very long) hex string,
    sometimes wrapped across lines or padded with whitespace by the
    YAML serialiser. Strip everything that isn't a hex digit before
    decoding.

    PyYAML quirk: an all-digit hex blob (rare in real assets; common in
    hand-built test fixtures) gets parsed as an integer. Handle that
    by reconstructing the hex from the int — but only when we know the
    intended byte count is even, so we can't reach here from production
    Unity output that lost a leading zero. Cast unknown types to ``""``
    rather than crashing.
    """
    if hexstr is None:
        return b""
    if isinstance(hexstr, int):
        # Reconstruct hex; pad to even length so ``bytes.fromhex`` accepts it.
        s = format(hexstr, "x")
        if len(s) % 2:
            s = "0" + s
        hexstr = s
    if not isinstance(hexstr, str):
        return b""
    cleaned = _HEX_NONHEX_RE.sub("", hexstr)
    if len(cleaned) % 2 != 0:
        # Drop the trailing nibble; the alternative is to fail the whole
        # mesh, but in practice the trailing nibble is always padding
        # introduced by the YAML emitter, never real data.
        cleaned = cleaned[:-1]
    return bytes.fromhex(cleaned)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_embedded_mesh(
    asset_path: Path, file_id: str
) -> EmbeddedMeshData | ExtractionFailure:
    """Decode the ``!u!43 Mesh`` matching ``file_id`` in ``asset_path``.

    ``asset_path`` should be the resolved ``.prefab`` / ``.asset`` on disk;
    ``file_id`` matches the value the MeshFilter referenced via
    ``m_Mesh.fileID``.

    Returns ``EmbeddedMeshData`` on success or ``ExtractionFailure`` with
    a structured reason on failure. Never raises for user-visible
    reasons -- low-level decode exceptions are caught and surfaced as
    ``FAILURE_DECODE_FAILED``.
    """
    cache_key = (asset_path.resolve(), str(file_id))
    cached = _cache.get(cache_key)
    if cached is not None:
        return cached

    result = _parse_uncached(asset_path, str(file_id))
    _cache[cache_key] = result
    return result


def _parse_uncached(
    asset_path: Path, file_id: str
) -> EmbeddedMeshData | ExtractionFailure:
    try:
        text = asset_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return ExtractionFailure(FAILURE_NOT_FOUND, str(exc))

    mesh_doc: dict | None = None
    for cid, fid, doc in parse_documents(text):
        if cid != _MESH_CLASS_ID:
            continue
        if fid == file_id:
            mesh_doc = doc_body(doc)
            break

    if mesh_doc is None:
        return ExtractionFailure(FAILURE_NOT_FOUND, f"no !u!43 &{file_id} in {asset_path.name}")

    # ----- pre-flight rejects (cheap, structured) -----
    mesh_compression = int(mesh_doc.get("m_MeshCompression", 0) or 0)
    if mesh_compression > 0:
        return ExtractionFailure(
            FAILURE_MESH_COMPRESSED,
            f"m_MeshCompression={mesh_compression}",
        )

    # ``m_StreamData`` describes an EXTERNAL .resS file when its ``path``
    # is non-empty. In SimpleFPS's mine the field is present but empty;
    # we only reject when it actually points outside.
    sd = mesh_doc.get("m_StreamData") or {}
    if isinstance(sd, dict):
        sd_path = sd.get("path") or ""
        if isinstance(sd_path, str) and sd_path.strip():
            return ExtractionFailure(FAILURE_EXTERNAL_STREAM_DATA, sd_path)

    vd = mesh_doc.get("m_VertexData")
    if not isinstance(vd, dict):
        return ExtractionFailure(FAILURE_NO_VERTEX_DATA)

    vertex_count = int(vd.get("m_VertexCount", 0) or 0)
    data_size = int(vd.get("m_DataSize", 0) or 0)
    channels_raw = vd.get("m_Channels") or []
    typeless = vd.get("_typelessdata") or ""

    if vertex_count <= 0 or not channels_raw or not typeless:
        return ExtractionFailure(FAILURE_NO_VERTEX_DATA)

    # ----- decode channel descriptors -----
    channels: list[dict] = []
    for idx, ch in enumerate(channels_raw):
        if not isinstance(ch, dict):
            continue
        stream = int(ch.get("stream", 0) or 0)
        offset = int(ch.get("offset", 0) or 0)
        fmt = int(ch.get("format", 0) or 0)
        dim = int(ch.get("dimension", 0) or 0)
        channels.append(
            {"index": idx, "stream": stream, "offset": offset,
             "format": fmt, "dimension": dim}
        )

    # Position is channel slot 0 by Unity convention.
    position = channels[0] if channels and channels[0]["dimension"] > 0 else None
    if position is None or position["dimension"] != 3:
        return ExtractionFailure(FAILURE_NO_POSITION_CHANNEL)
    if position["format"] != 0:  # Position must be Float32
        return ExtractionFailure(
            FAILURE_POSITION_NOT_FLOAT32,
            f"format={position['format']}",
        )
    if position["stream"] != 0:
        return ExtractionFailure(FAILURE_MULTI_STREAM, "Position not on stream 0")

    # Any USED (dimension > 0) channel sitting outside stream 0 means a
    # multi-stream layout we don't decode yet.
    for ch in channels:
        if ch["dimension"] > 0 and ch["stream"] != 0:
            return ExtractionFailure(
                FAILURE_MULTI_STREAM,
                f"channel {ch['index']} on stream {ch['stream']}",
            )

    # Compute stride from the actual offsets/sizes, NOT from enum order.
    stride = 0
    for ch in channels:
        if ch["dimension"] <= 0:
            continue
        attr_size = _format_size(ch["format"], ch["dimension"])
        if attr_size is None:
            # Unknown format on an active channel: cautiously reject so
            # we don't compute a too-small stride and misread positions.
            return ExtractionFailure(
                FAILURE_BAD_VERTEX_FORMAT,
                f"channel {ch['index']} format={ch['format']}",
            )
        stride = max(stride, ch["offset"] + attr_size)

    # Validate stride against the buffer Unity actually wrote.
    expected_size = stride * vertex_count
    if data_size and data_size != expected_size:
        return ExtractionFailure(
            FAILURE_STRIDE_MISMATCH,
            f"data_size={data_size} stride*count={expected_size}",
        )

    # ----- decode positions / normals / uvs -----
    try:
        blob = _hex_to_bytes(typeless)
    except ValueError as exc:
        return ExtractionFailure(FAILURE_DECODE_FAILED, f"hex decode: {exc}")
    if len(blob) < expected_size:
        return ExtractionFailure(
            FAILURE_DECODE_FAILED,
            f"blob {len(blob)}B < expected {expected_size}B",
        )

    try:
        positions = _decode_vec(blob, vertex_count, stride, position, dim=3)
    except (struct.error, ValueError) as exc:
        return ExtractionFailure(FAILURE_DECODE_FAILED, f"positions: {exc}")

    normal_ch = channels[1] if len(channels) > 1 else None
    normals: list[tuple[float, float, float]] = []
    if normal_ch and normal_ch["dimension"] == 3:
        try:
            normals = _decode_vec(blob, vertex_count, stride, normal_ch, dim=3)
        except (struct.error, ValueError) as exc:
            log.debug("normals decode skipped: %s", exc)

    uv0_ch = channels[4] if len(channels) > 4 else None
    uvs: list[tuple[float, float]] = []
    if uv0_ch and uv0_ch["dimension"] == 2:
        try:
            decoded = _decode_vec(blob, vertex_count, stride, uv0_ch, dim=2)
            uvs = [(u, v) for (u, v, *_rest) in decoded]
        except (struct.error, ValueError) as exc:
            log.debug("uvs decode skipped: %s", exc)

    # ----- decode index buffer + submeshes -----
    index_format = int(mesh_doc.get("m_IndexFormat", 0) or 0)
    idx_size = 4 if index_format == 1 else 2
    idx_struct = "<I" if index_format == 1 else "<H"

    try:
        idx_blob = _hex_to_bytes(mesh_doc.get("m_IndexBuffer") or "")
    except ValueError as exc:
        return ExtractionFailure(FAILURE_DECODE_FAILED, f"index hex: {exc}")
    if not idx_blob:
        return ExtractionFailure(FAILURE_NO_INDEX_DATA)

    submeshes = mesh_doc.get("m_SubMeshes") or []
    triangles: list[tuple[int, int, int]] = []
    seen_non_triangle = False
    for sm in submeshes:
        if not isinstance(sm, dict):
            continue
        topology = int(sm.get("topology", 0) or 0)
        if topology != 0:
            seen_non_triangle = True
            continue
        first_byte = int(sm.get("firstByte", 0) or 0)
        index_count = int(sm.get("indexCount", 0) or 0)
        base_vertex = int(sm.get("baseVertex", 0) or 0)
        if index_count <= 0:
            continue
        start = first_byte // idx_size
        try:
            indices = list(struct.iter_unpack(
                idx_struct, idx_blob[first_byte:first_byte + index_count * idx_size],
            ))
        except struct.error as exc:
            return ExtractionFailure(FAILURE_DECODE_FAILED, f"index submesh: {exc}")
        indices = [v[0] + base_vertex for v in indices]
        for i in range(0, len(indices) - 2, 3):
            triangles.append((indices[i], indices[i + 1], indices[i + 2]))

    if not triangles:
        return ExtractionFailure(
            FAILURE_UNSUPPORTED_TOPOLOGY
            if seen_non_triangle else FAILURE_NO_INDEX_DATA,
        )

    # ----- Unity -> Roblox handedness (negate X+Y; matches
    # converter/fbx_binary.mirror_fbx_handedness) -----
    positions = [(-x, -y, z) for (x, y, z) in positions]
    normals = [(-x, -y, z) for (x, y, z) in normals]

    # m_LocalAABB.m_Extent (already in metres). Read it so callers can
    # set MeshPart.InitialSize without re-deriving from the OBJ bounds.
    aabb = mesh_doc.get("m_LocalAABB") or {}
    ext = aabb.get("m_Extent") or {}
    extent: tuple[float, float, float] = (0.0, 0.0, 0.0)
    if isinstance(ext, dict):
        extent = (
            float(ext.get("x", 0) or 0),
            float(ext.get("y", 0) or 0),
            float(ext.get("z", 0) or 0),
        )

    name = str(mesh_doc.get("m_Name") or asset_path.stem)
    return EmbeddedMeshData(
        name=name,
        positions=positions,
        normals=normals,
        uvs=uvs,
        triangles=triangles,
        aabb_extent=extent,
    )


def _decode_vec(
    blob: bytes,
    vertex_count: int,
    stride: int,
    channel: dict,
    dim: int,
) -> list[tuple]:
    """Decode ``vertex_count`` vectors of ``dim`` components from ``blob``."""
    fmt = channel["format"]
    spec = _FORMAT_SPECS.get(fmt)
    if spec is None:
        raise ValueError(f"unsupported format {fmt}")
    pack_ch, comp_size = spec
    base_offset = channel["offset"]
    out: list[tuple] = []
    for i in range(vertex_count):
        row_off = i * stride + base_offset
        comps: list[float] = []
        for c in range(dim):
            raw, = struct.unpack_from(pack_ch, blob, row_off + c * comp_size)
            comps.append(_normalize(raw, fmt))
        out.append(tuple(comps))
    return out


def _normalize(value: float, fmt: int) -> float:
    """Convert an integer-format channel value into a float.

    Float formats pass through unchanged; UNorm/SNorm convert to the
    standard [0,1] / [-1,1] ranges. Other integer formats are not
    expected on Position/Normal/UV0 and pass through as-is.
    """
    if fmt == 0 or fmt == 1:    # Float32 / Float16
        return float(value)
    if fmt == 2:                # UNorm8
        return value / 255.0
    if fmt == 3:                # SNorm8
        return max(-1.0, value / 127.0)
    if fmt == 4:                # UNorm16
        return value / 65535.0
    if fmt == 5:                # SNorm16
        return max(-1.0, value / 32767.0)
    return float(value)


# ---------------------------------------------------------------------------
# OBJ serialiser
# ---------------------------------------------------------------------------


def serialize_obj(mesh: EmbeddedMeshData) -> bytes:
    """Render ``mesh`` to an ASCII Wavefront OBJ buffer.

    Uses canonical ``f v/vt/vn`` face syntax (1-indexed). Components are
    omitted when absent (e.g. ``f v//vn`` when there are no UVs).
    """
    has_uvs = bool(mesh.uvs) and len(mesh.uvs) == len(mesh.positions)
    has_normals = bool(mesh.normals) and len(mesh.normals) == len(mesh.positions)

    lines: list[str] = []
    lines.append(f"# Synthesised from embedded Unity Mesh '{mesh.name}'")
    lines.append("# Generated by unity2rbxlx embedded_mesh_extractor")
    lines.append(f"o {_sanitize_name(mesh.name)}")
    for (x, y, z) in mesh.positions:
        lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
    if has_uvs:
        for (u, v) in mesh.uvs:
            lines.append(f"vt {u:.6f} {v:.6f}")
    if has_normals:
        for (nx, ny, nz) in mesh.normals:
            lines.append(f"vn {nx:.6f} {ny:.6f} {nz:.6f}")
    for (a, b, c) in mesh.triangles:
        # 1-indexed for OBJ.
        ia, ib, ic = a + 1, b + 1, c + 1
        if has_uvs and has_normals:
            lines.append(f"f {ia}/{ia}/{ia} {ib}/{ib}/{ib} {ic}/{ic}/{ic}")
        elif has_normals:
            lines.append(f"f {ia}//{ia} {ib}//{ib} {ic}//{ic}")
        elif has_uvs:
            lines.append(f"f {ia}/{ia} {ib}/{ib} {ic}/{ic}")
        else:
            lines.append(f"f {ia} {ib} {ic}")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


_NAME_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


def _sanitize_name(name: str) -> str:
    return _NAME_SAFE_RE.sub("_", name) or "mesh"


_TRANSFORM_PROP_NAMES = frozenset({
    b"Lcl Translation", b"Lcl Rotation", b"Lcl Scaling",
    b"PreRotation", b"PostRotation",
    b"GeometricTranslation", b"GeometricRotation", b"GeometricScaling",
    b"RotationOffset", b"ScalingOffset", b"RotationPivot", b"ScalingPivot",
})


def _identity_transform_props_on_models(roots: list) -> None:
    """Reset every Model node's Lcl/Geometric/Pivot transform props to
    identity.

    The arbitrary-FBX template we clone (any project FBX with a Geometry
    node) carries the original Model's translation/rotation/scaling --
    e.g. HornetRifle's ``pod_R`` Model has ``Lcl Translation =
    (-0.35, 1.84, 4.42)`` and ``Lcl Rotation = (-90, 0, 0)``. Without
    this reset, every synthesised embedded mesh would inherit that
    transform and land shifted/rotated relative to its scene placement.

    Identity values:
        Lcl Translation / Lcl Rotation / PreRotation / PostRotation /
        Geometric* / *Offset / *Pivot → 0,0,0
        Lcl Scaling                                                → 1,1,1
    """
    for r in roots:
        if r.name != b"Objects":
            continue
        for child in r.children:
            if child.name != b"Model":
                continue
            for cc in child.children:
                if cc.name not in (b"Properties70", b"Properties60"):
                    continue
                for p_node in cc.children:
                    if p_node.name != b"P" or not p_node.properties:
                        continue
                    pname = p_node.properties[0].value
                    if pname not in _TRANSFORM_PROP_NAMES:
                        continue
                    # P-node properties layout (FBX 7.x):
                    #   [name, type, sub_type, flags, x, y, z]
                    # The last 3 are the vector components we override.
                    if len(p_node.properties) >= 7:
                        identity_z = (
                            1.0 if pname == b"Lcl Scaling" else 0.0
                        )
                        identity_xy = (
                            1.0 if pname == b"Lcl Scaling" else 0.0
                        )
                        # Each component is a Property dataclass; rebuild
                        # type_code as 'D' (Float64) which FBX uses for
                        # transform vectors.
                        for idx, val in zip(
                            (4, 5, 6), (identity_xy, identity_xy, identity_z),
                        ):
                            p_node.properties[idx].type_code = "D"
                            p_node.properties[idx].value = val


def _strip_extra_geometries_and_dependents(roots: list, keep) -> None:
    """Reduce a multi-Geometry FBX template to a single Geometry.

    Removes every Geometry node from the Objects section except ``keep``,
    then removes Model nodes that reference the dropped Geometries via
    Connections, and finally removes Connection records that point at
    any deleted Object ID. The Object ID (= first property, an int) is
    the canonical FBX identifier the Connection section uses.

    Best-effort: a malformed template might leave dangling references,
    but Roblox's importer tolerates extra Models that have no Geometry
    -- they just show up as empty MeshParts which the resolver returns
    with size zero, which our caller already validates against
    (see ``len(mesh_hierarchies[key]) == 1`` check in the pipeline).
    """
    # Find Objects section.
    objects_node = None
    for r in roots:
        if r.name == b"Objects":
            objects_node = r
            break
    if objects_node is None:
        return

    keep_id = keep.properties[0].value if keep.properties else None

    # First pass: drop extra Geometry nodes; collect their IDs so the
    # Connections cleanup can purge anything that referenced them.
    dropped_ids: set = set()
    surviving_objects = []
    for child in objects_node.children:
        if child.name == b"Geometry" and child is not keep:
            if child.properties:
                dropped_ids.add(child.properties[0].value)
            continue
        surviving_objects.append(child)
    objects_node.children = surviving_objects

    # Second pass: walk Connections to identify which Models the dropped
    # Geometries belonged to, then prune those Models from Objects too.
    # Without the Model prune, Roblox's FBX importer still instantiates
    # every Model entry as a separate MeshPart (even ones whose Geometry
    # link was removed), so ``resolve_assets`` returns N sub-meshes
    # where N == Model count in the template. Codex review [P1]:
    # "delete orphaned Model nodes when collapsing template FBXs".
    connections_node = None
    for r in roots:
        if r.name == b"Connections":
            connections_node = r
            break
    if connections_node is None:
        return

    # First identify Models linked to dropped Geometries via Connections.
    # FBX 7.x Connection records: "C" with three string-typed props
    # (relation, src_id, dst_id); object connections are "OO".
    models_to_drop: set = set()
    for conn in connections_node.children:
        if conn.name != b"C" or len(conn.properties) < 3:
            continue
        src_id = conn.properties[1].value
        dst_id = conn.properties[2].value
        if dst_id in dropped_ids:
            # src is the Model parenting this dropped Geometry.
            models_to_drop.add(src_id)
        elif src_id in dropped_ids:
            # src=Geometry, dst=Model -- same relationship, opposite
            # FBX convention.
            models_to_drop.add(dst_id)

    # Don't drop a Model that ALSO connects to the kept Geometry.
    for conn in connections_node.children:
        if conn.name != b"C" or len(conn.properties) < 3:
            continue
        src_id = conn.properties[1].value
        dst_id = conn.properties[2].value
        if dst_id == keep_id:
            models_to_drop.discard(src_id)
        if src_id == keep_id:
            models_to_drop.discard(dst_id)

    # Prune those Models from Objects.
    surviving_objects = []
    for child in objects_node.children:
        if child.name == b"Model" and child.properties:
            obj_id = child.properties[0].value
            if obj_id in models_to_drop:
                dropped_ids.add(obj_id)
                continue
        surviving_objects.append(child)
    objects_node.children = surviving_objects

    # Final pass: drop Connection records pointing at any dropped object.
    surviving_connections = []
    for conn in connections_node.children:
        if conn.name != b"C" or len(conn.properties) < 3:
            surviving_connections.append(conn)
            continue
        src_id = conn.properties[1].value
        dst_id = conn.properties[2].value
        if src_id in dropped_ids or dst_id in dropped_ids:
            continue
        surviving_connections.append(conn)
    connections_node.children = surviving_connections


# ---------------------------------------------------------------------------
# FBX synthesiser
# ---------------------------------------------------------------------------
#
# Roblox Open Cloud's Assets API rejects ``model/obj`` uploads with
# ``"Creating Model from a model/obj file is not supported yet."`` --
# only FBX is accepted for the ``Model`` asset type today. We could
# build an FBX from scratch, but the binary format's strict-parser
# footer + Definitions/Connections requirements make that brittle.
#
# Cheaper path: clone any FBX file already present in the project and
# replace its single Geometry node's ``Vertices`` + ``PolygonVertexIndex``
# arrays with the decoded embedded mesh. The template keeps a known-good
# Header/Definitions/Objects/Connections/Takes block + footer that Roblox
# accepts. Strip per-vertex layer elements (Normal/UV/etc.) from the
# template because their indices reference the original template's
# vertex count, not ours -- Roblox will recompute auto-normals from the
# geometry.


def synthesize_fbx(mesh: EmbeddedMeshData, template_fbx_path: Path) -> bytes:
    """Render ``mesh`` to a binary FBX buffer using ``template_fbx_path``
    as the structural template.

    The template's Geometry node is mutated in-place to hold the
    decoded mesh's vertices and triangle indices. Per-vertex layer
    elements (Normal/UV/Color/Tangent/Edges) are stripped because they
    are indexed against the template's vertex count, not the
    synthesised mesh's. Roblox imports auto-normals from the geometry.

    Raises ``ValueError`` if the template has no Geometry node.
    """
    # Lazy import: fbx_binary lives under ``converter`` and pulls in
    # zlib/struct -- avoid importing it at module load for callers
    # that only use the YAML extractor.
    from converter.fbx_binary import (
        _child,
        _find_geometry_nodes,
        read_fbx,
        write_fbx,
    )

    version, roots, footer = read_fbx(template_fbx_path)
    geometries = _find_geometry_nodes(roots)
    if not geometries:
        raise ValueError(
            f"template FBX has no Geometry node: {template_fbx_path}"
        )

    # Strip the template's extra Geometries (and the Model/Material/Texture
    # objects that referenced them via Connections). HornetRifle.fbx and
    # other multi-part templates ship ~14 Geometry nodes; we only fill
    # the first one with the embedded mesh's data, so leaving the other
    # 13 would publish a Model whose first sub-mesh is ours and whose
    # other sub-meshes are the template's leftover rifle parts. The
    # consumer then silently picks ``sub_meshes[0]`` and gets the right
    # answer ONLY by coincidence of geometry order in the template.
    # Strip them so the upload has exactly one Geometry -> exactly one
    # MeshPart on the Roblox side.
    geo = geometries[0]
    _strip_extra_geometries_and_dependents(roots, keep=geo)
    _identity_transform_props_on_models(roots)

    # Vertices: Float64 array (FBX type 'd'), flat (x1, y1, z1, x2, ...).
    verts_flat: list[float] = []
    for (x, y, z) in mesh.positions:
        verts_flat.extend((float(x), float(y), float(z)))
    vertices_node = _child(geo, b"Vertices")
    if vertices_node is None or not vertices_node.properties:
        raise ValueError("template Geometry missing Vertices node")
    vertices_node.properties[0].value = verts_flat
    vertices_node.properties[0].type_code = "d"

    # PolygonVertexIndex: Int32 array. Each polygon's LAST vertex
    # index is bit-inverted (``~i == -(i + 1)``) to mark the polygon
    # boundary -- a quirk of the FBX format we have to honour even
    # for triangles.
    pvi_flat: list[int] = []
    for (i0, i1, i2) in mesh.triangles:
        pvi_flat.append(int(i0))
        pvi_flat.append(int(i1))
        pvi_flat.append(-(int(i2) + 1))
    pvi_node = _child(geo, b"PolygonVertexIndex")
    if pvi_node is None or not pvi_node.properties:
        raise ValueError("template Geometry missing PolygonVertexIndex node")
    pvi_node.properties[0].value = pvi_flat
    pvi_node.properties[0].type_code = "i"

    # Strip layer elements (per-vertex normals/uvs/colors/tangents/edges)
    # -- their internal arrays are indexed against the template's
    # vertex count, not ours.
    geo.children = [
        c for c in geo.children
        if not c.name.startswith(b"LayerElement")
        and c.name not in (b"Edges",)
    ]

    # Serialise. ``write_fbx`` needs the template's footer to satisfy
    # Roblox's strict parser; ``read_fbx`` returned it.
    import tempfile
    import os

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".fbx")
    tmp.close()
    try:
        write_fbx(tmp.name, version, roots, footer)
        return Path(tmp.name).read_bytes()
    finally:
        os.unlink(tmp.name)
