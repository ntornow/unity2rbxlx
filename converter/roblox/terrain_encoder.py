"""
terrain_encoder.py -- Encode terrain voxel data into Roblox SmoothGrid binary format.

Converts a heightmap into the binary voxel format used by Roblox's Terrain
object in rbxlx files. The format uses RLE compression with material+occupancy
per voxel.

SmoothGrid binary format (version 1):
  Byte 0: version (0x01)
  Byte 1: chunk size as power of 2 (0x05 = 32x32x32 chunks)
  Then RLE-encoded voxel data:
    Each run: header_byte [occupancy_byte] [runlength_byte]
    header_byte: material(6 bits) | has_occupancy(1 bit) | has_runlength(1 bit)
    occupancy_byte: 0-255 (decoded as (value+1)/256.0)
    runlength_byte: run_length - 1 (so max run = 256)

Material IDs (6-bit, matching Roblox internal enum):
  0=Air, 1=Grass, 2=Sand, 3=Rock, 4=Water, 5=Ground, 6=Concrete
"""

from __future__ import annotations

import base64
import logging
import math
from typing import Any

import config

log = logging.getLogger(__name__)

# Roblox terrain material IDs (internal 6-bit values for SmoothGrid)
# Confirmed from Studio-saved terrain reference file
MATERIAL_AIR = 0
MATERIAL_WATER = 1
MATERIAL_GRASS = 2
MATERIAL_SAND = 6
MATERIAL_ROCK = 7
MATERIAL_MUD = 14
MATERIAL_GROUND = 5
MATERIAL_SNOW = 9
MATERIAL_SANDSTONE = 16
MATERIAL_SLATE = 8

# Voxel size in studs
VOXEL_SIZE = 4

# Unity terrain layer name → Roblox material ID mapping
_LAYER_NAME_TO_MATERIAL: dict[str, int] = {
    "sand": MATERIAL_SAND,
    "beach": MATERIAL_SAND,
    "desert": MATERIAL_SAND,
    "grass": MATERIAL_GRASS,
    "grasshill": MATERIAL_GRASS,
    "grassrocky": MATERIAL_GRASS,
    "lawn": MATERIAL_GRASS,
    "meadow": MATERIAL_GRASS,
    "cliff": MATERIAL_ROCK,
    "rock": MATERIAL_ROCK,
    "stone": MATERIAL_ROCK,
    "boulder": MATERIAL_ROCK,
    "mountain": MATERIAL_ROCK,
    "gravel": MATERIAL_ROCK,
    "mud": MATERIAL_MUD,
    "mudrocky": MATERIAL_MUD,
    "swamp": MATERIAL_MUD,
    "wetland": MATERIAL_MUD,
    "dirt": MATERIAL_GROUND,
    "ground": MATERIAL_GROUND,
    "soil": MATERIAL_GROUND,
    "path": MATERIAL_GROUND,
    "trail": MATERIAL_GROUND,
    "snow": MATERIAL_SNOW,
    "ice": MATERIAL_SNOW,
    "frost": MATERIAL_SNOW,
    "sandstone": MATERIAL_SANDSTONE,
    "slate": MATERIAL_SLATE,
    "cobble": MATERIAL_SLATE,
    "concrete": MATERIAL_GROUND,
    "asphalt": MATERIAL_SLATE,
}


def _height_based_material(
    normalized_height: float,
    slope: float = 0.0,
    max_height_studs: float = 200.0,
) -> int:
    """Select terrain material based on height and slope.

    Uses a simple height-based biome model:
    - Low (0-15%): Sand (beach/shore)
    - Low-mid (15-35%): Grass (plains)
    - Mid (35-60%): Ground/Mud (hills)
    - High (60-85%): Rock (cliffs)
    - Very high (85%+): Rock/Slate (peaks)
    - Steep slopes (>45 deg): Rock regardless of height
    """
    if slope > 0.7:  # ~45 degrees
        return MATERIAL_ROCK
    if normalized_height < 0.15:
        return MATERIAL_SAND
    if normalized_height < 0.35:
        return MATERIAL_GRASS
    if normalized_height < 0.60:
        return MATERIAL_MUD if slope > 0.3 else MATERIAL_GRASS
    if normalized_height < 0.85:
        return MATERIAL_ROCK
    return MATERIAL_SLATE


def _splat_based_material(
    splat_alphas: list[list[float]],
    splat_resolution: int,
    layer_names: list[str],
    world_x: float,
    world_z: float,
    terrain_width: float,
    terrain_length: float,
) -> int:
    """Select terrain material from splat alpha map data.

    Looks up the dominant layer at the given world position using the
    painted alpha weights from Unity's terrain splat maps.
    """
    if not splat_alphas or splat_resolution <= 0:
        return MATERIAL_GRASS

    # Map world position to splat UV
    u = max(0.0, min(1.0, world_x / terrain_width)) if terrain_width > 0 else 0.0
    v = max(0.0, min(1.0, world_z / terrain_length)) if terrain_length > 0 else 0.0
    sx = int(u * (splat_resolution - 1))
    sz = int(v * (splat_resolution - 1))
    idx = sz * splat_resolution + sx

    # Find dominant layer
    best_alpha = -1.0
    best_layer = 0
    for layer_idx, alpha_map in enumerate(splat_alphas):
        if idx < len(alpha_map):
            a = alpha_map[idx]
            if a > best_alpha:
                best_alpha = a
                best_layer = layer_idx

    # Map layer name to Roblox material
    if best_layer < len(layer_names):
        name = layer_names[best_layer].lower()
        for key, mat_id in _LAYER_NAME_TO_MATERIAL.items():
            if key in name:
                return mat_id

    return MATERIAL_GRASS  # default


def encode_smooth_grid(
    heights: list[float],
    resolution: int,
    scale: tuple[float, float, float],
    terrain_position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    layer_names: list[str] | None = None,
    splat_alphas: list[list[float]] | None = None,
    splat_resolution: int = 0,
) -> str:
    """Encode a Unity heightmap into Roblox SmoothGrid binary format.

    Args:
        heights: Normalized heights (0-1) from Unity TerrainData.
        resolution: Heightmap resolution (e.g., 513).
        scale: (scale_x, max_height, scale_z) from Unity terrain.
        terrain_position: Terrain origin in Roblox coordinates (x, y, z).
        splat_alphas: Optional per-layer alpha maps for pixel-accurate materials.
        splat_resolution: Resolution of the splat alpha maps.
        layer_names: Optional terrain layer names for material inference.

    Returns:
        Base64-encoded SmoothGrid binary data.
    """
    scale_x, max_height, scale_z = scale
    STUDS = config.STUDS_PER_METER

    # Terrain dimensions in studs
    width_studs = (resolution - 1) * scale_x * STUDS
    max_height_studs = max_height * STUDS
    length_studs = (resolution - 1) * scale_z * STUDS

    # Use actual peak height, not the theoretical max
    actual_peak = max(heights) * max_height_studs
    grid_x = int(math.ceil(width_studs / VOXEL_SIZE))
    grid_z = int(math.ceil(length_studs / VOXEL_SIZE))
    grid_y = int(math.ceil(actual_peak / VOXEL_SIZE)) + 2  # +2 for surface voxels

    rx, ry, rz = terrain_position

    log.info("Terrain grid: %dx%dx%d voxels (%.0f x %.0f x %.0f studs)",
             grid_x, grid_y, grid_z, width_studs, max_height_studs, length_studs)

    # Build voxel data: for each (x, y, z) determine material and occupancy
    # Roblox terrain coordinate system: +X right, +Y up, +Z forward
    # We need to map from Unity heightmap to Roblox voxel grid

    # Build a height lookup function
    def sample_height(stud_x: float, stud_z: float) -> float:
        u = stud_x / width_studs if width_studs > 0 else 0
        v = stud_z / length_studs if length_studs > 0 else 0
        u = max(0.0, min(u, 1.0))
        v = max(0.0, min(v, 1.0))
        hx = u * (resolution - 1)
        hz = v * (resolution - 1)
        ix = min(int(hx), resolution - 2)
        iz = min(int(hz), resolution - 2)
        fx = hx - ix
        fz = hz - iz
        h00 = heights[iz * resolution + ix]
        h10 = heights[iz * resolution + ix + 1]
        h01 = heights[(iz + 1) * resolution + ix]
        h11 = heights[(iz + 1) * resolution + ix + 1]
        return (h00 * (1 - fx) * (1 - fz) + h10 * fx * (1 - fz) +
                h01 * (1 - fx) * fz + h11 * fx * fz) * max_height_studs

    # SmoothGrid v1 format (reverse-engineered from Studio-saved terrain):
    #   2-byte global header: version(1) + chunk_pow(5)
    #   Per chunk:
    #     12-byte delta-encoded chunk coordinates (MSB-interleaved int32 dx,dy,dz)
    #     RLE-encoded voxel data for 32^3 = 32768 voxels
    #   Chunk order: X outermost, Y middle, Z innermost
    #   Voxel order within chunk: X innermost (index = x + y*32 + z*1024)

    chunk_size = 32
    chunks_x = int(math.ceil(grid_x / chunk_size))
    chunks_y = int(math.ceil(grid_y / chunk_size))
    chunks_z = int(math.ceil(grid_z / chunk_size))

    def _get_material(gx: int, gy: int, gz: int) -> tuple[int, int]:
        """Return (material, occupancy) for global voxel position."""
        if gx >= grid_x or gy >= grid_y or gz >= grid_z:
            return (MATERIAL_AIR, 0)
        h_studs = sample_height(gx * VOXEL_SIZE, gz * VOXEL_SIZE)
        voxel_bottom = gy * VOXEL_SIZE
        voxel_top = voxel_bottom + VOXEL_SIZE
        if voxel_top <= h_studs:
            if splat_alphas and splat_resolution > 0:
                mat = _splat_based_material(
                    splat_alphas, splat_resolution,
                    layer_names or [], gx * VOXEL_SIZE, gz * VOXEL_SIZE,
                    width_studs, length_studs,
                )
            else:
                norm_h = h_studs / max_height_studs if max_height_studs > 0 else 0
                h_dx = abs(sample_height((gx + 1) * VOXEL_SIZE, gz * VOXEL_SIZE) - h_studs)
                h_dz = abs(sample_height(gx * VOXEL_SIZE, (gz + 1) * VOXEL_SIZE) - h_studs)
                slope = max(h_dx, h_dz) / VOXEL_SIZE
                mat = _height_based_material(norm_h, slope, max_height_studs)
            return (mat, 255)
        elif voxel_bottom >= h_studs:
            return (MATERIAL_AIR, 0)
        else:
            occ = (h_studs - voxel_bottom) / VOXEL_SIZE
            occ_byte = max(0, min(255, int(occ * 255)))
            if splat_alphas and splat_resolution > 0:
                mat = _splat_based_material(
                    splat_alphas, splat_resolution,
                    layer_names or [], gx * VOXEL_SIZE, gz * VOXEL_SIZE,
                    width_studs, length_studs,
                )
            else:
                norm_h = h_studs / max_height_studs if max_height_studs > 0 else 0
                h_dx = abs(sample_height((gx + 1) * VOXEL_SIZE, gz * VOXEL_SIZE) - h_studs)
                h_dz = abs(sample_height(gx * VOXEL_SIZE, (gz + 1) * VOXEL_SIZE) - h_studs)
                slope = max(h_dx, h_dz) / VOXEL_SIZE
                mat = _height_based_material(norm_h, slope, max_height_studs)
            return (mat, occ_byte)

    def _encode_chunk_coord_delta(dx: int, dy: int, dz: int) -> bytes:
        """Encode 3 int32 deltas as 12 MSB-interleaved bytes."""
        # Convert to unsigned 32-bit for byte extraction
        def _to_u32(v: int) -> int:
            return v & 0xFFFFFFFF
        udx, udy, udz = _to_u32(dx), _to_u32(dy), _to_u32(dz)
        return bytes([
            (udx >> 24) & 0xFF, (udy >> 24) & 0xFF, (udz >> 24) & 0xFF,
            (udx >> 16) & 0xFF, (udy >> 16) & 0xFF, (udz >> 16) & 0xFF,
            (udx >> 8) & 0xFF,  (udy >> 8) & 0xFF,  (udz >> 8) & 0xFF,
            udx & 0xFF,         udy & 0xFF,         udz & 0xFF,
        ])

    def _rle_encode_chunk(chunk_voxels: list[tuple[int, int]]) -> bytearray:
        """RLE-encode exactly 32^3 voxels for one chunk."""
        out = bytearray()
        i = 0
        while i < len(chunk_voxels):
            mat, occ = chunk_voxels[i]
            remaining = len(chunk_voxels) - i
            max_run = min(256, remaining)
            run = 1
            while (run < max_run and
                   chunk_voxels[i + run][0] == mat and
                   chunk_voxels[i + run][1] == occ):
                run += 1
            has_occ = (mat != MATERIAL_AIR)
            has_run = (run > 1)
            header = mat & 0x3F
            if has_occ:
                header |= 0x40
            if has_run:
                header |= 0x80
            out.append(header)
            if has_occ:
                out.append(occ)
            if has_run:
                out.append(run - 1)
            i += run
        return out

    # Build output
    buf = bytearray()
    buf.append(1)  # version
    buf.append(5)  # chunk_pow = 2^5 = 32

    prev_cx, prev_cy, prev_cz = 0, 0, 0
    total_voxels = 0
    chunk_count = 0

    # Chunk iteration: X outermost, Y middle, Z innermost
    for cx in range(chunks_x):
        for cy in range(chunks_y):
            for cz in range(chunks_z):
                # Delta-encoded chunk coordinates
                dx = cx - prev_cx
                dy = cy - prev_cy
                dz = cz - prev_cz
                buf.extend(_encode_chunk_coord_delta(dx, dy, dz))
                prev_cx, prev_cy, prev_cz = cx, cy, cz

                # Collect voxels in X-innermost order: index = x + y*32 + z*1024
                chunk_voxels = []
                for lz in range(chunk_size):
                    for ly in range(chunk_size):
                        for lx in range(chunk_size):
                            gx = cx * chunk_size + lx
                            gy = cy * chunk_size + ly
                            gz = cz * chunk_size + lz
                            chunk_voxels.append(_get_material(gx, gy, gz))

                buf.extend(_rle_encode_chunk(chunk_voxels))
                total_voxels += len(chunk_voxels)
                chunk_count += 1

    log.info("SmoothGrid: %d chunks, %d voxels -> %d bytes encoded",
             chunk_count, total_voxels, len(buf))
    return base64.b64encode(bytes(buf)).decode('ascii')


def encode_physics_grid() -> str:
    """Generate a minimal PhysicsGrid (empty/default).

    The PhysicsGrid stores collision data. For simple terrain,
    Roblox can regenerate it from the SmoothGrid.
    """
    # Minimal PhysicsGrid: version 2, empty
    buf = bytearray([2, 3, 0, 0])
    return base64.b64encode(bytes(buf)).decode('ascii')
