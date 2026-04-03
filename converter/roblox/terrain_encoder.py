"""
terrain_encoder.py -- Encode terrain voxel data into Roblox SmoothGrid binary format.

Converts a heightmap into the binary voxel format used by Roblox's Terrain
object in rbxlx files.

SmoothGrid binary format (version 1, confirmed via Studio reference 2026-03-28):
  Byte 0: version (0x01)
  Byte 1: chunk size as power of 2 (0x05 = 32x32x32 chunks)
  Per chunk:
    12-byte DELTA chunk coordinates (MSB-interleaved signed int32 dx,dy,dz)
    First chunk is absolute, subsequent chunks are relative to previous
    RLE-encoded voxel data for 32^3 = 32768 voxels
  RLE entry: header_byte [occupancy_byte?] [runlength_byte?]
    header_byte: material(6 bits) | has_occupancy(1 bit) | has_runlength(1 bit)
    occupancy_byte: 0-255 (only if has_occupancy set; absent = fully solid)
    runlength_byte: run_length - 1 (only if has_runlength set; run range 2..256)
  Voxel axis mapping (SWAPPED from Roblox world):
    sx = world X, sy = world Z, sz = world Y (height)
    index = sx + sy*32 + sz*1024

Material IDs (6-bit, confirmed from Studio-saved reference file):
  0=Air, 1=Water, 2=Grass, 3=Slate, 4=Concrete, 5=Brick, 6=Sand,
  7=WoodPlanks, 8=Rock, 9=Glacier, 10=Snow, 11=Sandstone, 12=Mud,
  13=Basalt, 14=Ground, 15=CrackedLava, 16=Asphalt, 17=Cobblestone,
  18=Ice, 19=LeafyGrass, 20=Salt, 21=Limestone, 22=Pavement
"""

from __future__ import annotations

import base64
import logging
import math
from typing import Any

import config

log = logging.getLogger(__name__)

# Roblox terrain material IDs (6-bit, confirmed from Studio reference)
MATERIAL_AIR = 0
MATERIAL_WATER = 1
MATERIAL_GRASS = 2
MATERIAL_SLATE = 3
MATERIAL_CONCRETE = 4
MATERIAL_BRICK = 5
MATERIAL_SAND = 6
MATERIAL_WOODPLANKS = 7
MATERIAL_ROCK = 8
MATERIAL_GLACIER = 9
MATERIAL_SNOW = 10
MATERIAL_SANDSTONE = 11
MATERIAL_MUD = 12
MATERIAL_BASALT = 13
MATERIAL_GROUND = 14
MATERIAL_CRACKEDLAVA = 15
MATERIAL_ASPHALT = 16
MATERIAL_COBBLESTONE = 17
MATERIAL_ICE = 18
MATERIAL_LEAFYGRASS = 19
MATERIAL_SALT = 20
MATERIAL_LIMESTONE = 21
MATERIAL_PAVEMENT = 22

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
    "leafy": MATERIAL_LEAFYGRASS,
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
    "ice": MATERIAL_ICE,
    "frost": MATERIAL_SNOW,
    "glacier": MATERIAL_GLACIER,
    "sandstone": MATERIAL_SANDSTONE,
    "limestone": MATERIAL_LIMESTONE,
    "slate": MATERIAL_SLATE,
    "cobble": MATERIAL_COBBLESTONE,
    "cobblestone": MATERIAL_COBBLESTONE,
    "concrete": MATERIAL_CONCRETE,
    "asphalt": MATERIAL_ASPHALT,
    "pavement": MATERIAL_PAVEMENT,
    "brick": MATERIAL_BRICK,
    "wood": MATERIAL_WOODPLANKS,
    "plank": MATERIAL_WOODPLANKS,
    "basalt": MATERIAL_BASALT,
    "lava": MATERIAL_CRACKEDLAVA,
    "salt": MATERIAL_SALT,
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

    # Map world position to splat UV.
    # The splat texture image has row 0 at the top (standard image convention),
    # but Unity terrain Z=0 is at the bottom of the texture.  Flip V to match.
    u = max(0.0, min(1.0, world_x / terrain_width)) if terrain_width > 0 else 0.0
    v_raw = max(0.0, min(1.0, world_z / terrain_length)) if terrain_length > 0 else 0.0
    v = 1.0 - v_raw
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

    # Build a height lookup function (bilinear interpolation)
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

    chunk_size = 32
    chunks_x = int(math.ceil(grid_x / chunk_size))
    chunks_y = int(math.ceil(grid_y / chunk_size))
    chunks_z = int(math.ceil(grid_z / chunk_size))

    def _get_voxel(gx: int, gy: int, gz: int) -> tuple[int, int]:
        """Return (material, occupancy) for global voxel position.

        occupancy: 0 = empty, 255 = fully solid.
        Surface voxels get partial occupancy for smooth edges.
        """
        if gx < 0 or gy < 0 or gz < 0 or gx >= grid_x or gy >= grid_y or gz >= grid_z:
            return (MATERIAL_AIR, 0)

        h_studs = sample_height(gx * VOXEL_SIZE, gz * VOXEL_SIZE)
        voxel_bottom = gy * VOXEL_SIZE
        voxel_top = voxel_bottom + VOXEL_SIZE

        if voxel_top <= h_studs:
            # Fully below surface — solid
            mat = _get_surface_material(gx, gz, h_studs)
            return (mat, 255)
        elif voxel_bottom >= h_studs:
            # Fully above surface — air
            return (MATERIAL_AIR, 0)
        else:
            # Surface voxel — partial occupancy
            occ = (h_studs - voxel_bottom) / VOXEL_SIZE
            occ_byte = max(1, min(255, int(occ * 255)))
            mat = _get_surface_material(gx, gz, h_studs)
            return (mat, occ_byte)

    def _get_surface_material(gx: int, gz: int, h_studs: float) -> int:
        """Determine material for a voxel based on position."""
        if splat_alphas and splat_resolution > 0:
            return _splat_based_material(
                splat_alphas, splat_resolution,
                layer_names or [], gx * VOXEL_SIZE, gz * VOXEL_SIZE,
                width_studs, length_studs,
            )
        norm_h = h_studs / max_height_studs if max_height_studs > 0 else 0
        h_dx = abs(sample_height((gx + 1) * VOXEL_SIZE, gz * VOXEL_SIZE) - h_studs)
        h_dz = abs(sample_height(gx * VOXEL_SIZE, (gz + 1) * VOXEL_SIZE) - h_studs)
        slope = max(h_dx, h_dz) / VOXEL_SIZE
        return _height_based_material(norm_h, slope, max_height_studs)

    def _make_chunk_header(cx: int, cy: int, cz: int) -> bytes:
        """12-byte MSB-interleaved int32 chunk coordinates (absolute or delta)."""
        def _u32(v: int) -> int:
            return v & 0xFFFFFFFF
        ux, uy, uz = _u32(cx), _u32(cy), _u32(cz)
        return bytes([
            (ux >> 24) & 0xFF, (uy >> 24) & 0xFF, (uz >> 24) & 0xFF,
            (ux >> 16) & 0xFF, (uy >> 16) & 0xFF, (uz >> 16) & 0xFF,
            (ux >> 8) & 0xFF,  (uy >> 8) & 0xFF,  (uz >> 8) & 0xFF,
            ux & 0xFF,         uy & 0xFF,         uz & 0xFF,
        ])

    def _rle_encode_chunk(chunk_voxels: list[tuple[int, int]]) -> bytearray:
        """RLE-encode exactly 32^3 voxels for one chunk.

        Format per entry: [header_byte] [occupancy_byte?] [run_byte?]
          header_byte: material(6 bits) | has_occupancy(1 bit) | has_runlength(1 bit)
          occupancy_byte: 0-255 (only if has_occupancy; absent means fully solid)
          run_byte: run_length - 1 (only if has_runlength set)
        """
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

            # Determine if we need an explicit occupancy byte
            # Non-air at full occupancy (255) or air at 0 → no occ byte needed
            need_occ = (mat != MATERIAL_AIR and occ != 255)
            has_run = (run > 1)

            header = mat & 0x3F
            if need_occ:
                header |= 0x40
            if has_run:
                header |= 0x80
            out.append(header)
            if need_occ:
                out.append(occ)
            if has_run:
                out.append(run - 1)
            i += run
        return out

    # Build voxel data with AXIS SWAP: SmoothGrid (sx, sy, sz) = world (X, Z, Y)
    # sx = world X (horizontal), sy = world Z (depth), sz = world Y (height)
    # Voxel order: sx innermost, sy middle, sz outermost
    pending_chunks: list[tuple[tuple[int, int, int], bytearray]] = []

    # Terrain position offset in voxel units.
    # Terrain-local voxel (gx, gy, gz) maps to world voxel:
    #   wvx = gx + off_x
    #   wvy = gy + off_y
    #   wvz = off_z - gz  (Z is INVERTED: Unity Z+ → Roblox Z-)
    off_x = round(rx / VOXEL_SIZE)
    off_y = round(ry / VOXEL_SIZE)
    off_z = round(rz / VOXEL_SIZE)  # negative for negative rz

    # World chunk bounds containing the terrain
    wcx_min = off_x // chunk_size
    wcx_max = (off_x + grid_x - 1) // chunk_size
    wcy_min = off_y // chunk_size
    wcy_max = (off_y + grid_y - 1) // chunk_size
    # Z: terrain extends from off_z (most positive) to off_z - grid_z + 1 (most negative)
    wcz_min = (off_z - grid_z + 1) // chunk_size
    wcz_max = off_z // chunk_size

    for wcx in range(wcx_min, wcx_max + 1):
        for wcz in range(wcz_min, wcz_max + 1):
            for wcy in range(wcy_min, wcy_max + 1):
                chunk_voxels: list[tuple[int, int]] = []
                has_non_air = False

                # sz = Y axis (height), sy = Z axis (depth), sx = X axis
                for sz in range(chunk_size):
                    wvy = wcy * chunk_size + sz
                    gy = wvy - off_y  # terrain-local Y

                    for sy in range(chunk_size):
                        wvz = wcz * chunk_size + sy
                        gz = off_z - wvz  # terrain-local Z (INVERTED)

                        for sx in range(chunk_size):
                            wvx = wcx * chunk_size + sx
                            gx = wvx - off_x  # terrain-local X

                            v = _get_voxel(gx, gy, gz)
                            chunk_voxels.append(v)
                            if v[0] != MATERIAL_AIR:
                                has_non_air = True

                if not has_non_air:
                    continue

                # Chunk coords are world-space (already correct)
                coord = (wcx, wcy, wcz)
                pending_chunks.append((coord, _rle_encode_chunk(chunk_voxels)))

    # Sort chunks
    pending_chunks.sort(key=lambda c: c[0])

    # Build output
    buf = bytearray()
    buf.append(1)  # version
    buf.append(5)  # chunk_pow = 2^5 = 32

    total_voxels = 0
    chunk_count = 0

    prev_coord = (0, 0, 0)
    for coord, rle_data in pending_chunks:
        if chunk_count == 0:
            delta = coord  # first chunk is absolute
        else:
            delta = (coord[0] - prev_coord[0],
                     coord[1] - prev_coord[1],
                     coord[2] - prev_coord[2])
        buf.extend(_make_chunk_header(*delta))
        buf.extend(rle_data)
        prev_coord = coord
        total_voxels += 32768
        chunk_count += 1

    log.info("SmoothGrid: %d chunks, %d voxels -> %d bytes encoded",
             chunk_count, total_voxels, len(buf))
    return base64.b64encode(bytes(buf)).decode('ascii')


def encode_physics_grid() -> str:
    """Generate a default PhysicsGrid.

    The PhysicsGrid stores collision/physics data for terrain.
    This uses the default grid from a Studio-saved reference file.
    Roblox may regenerate it from SmoothGrid on load, but providing
    a valid one prevents "unexpected end at offset 4" parse errors.
    """
    # Default PhysicsGrid from Studio reference (230 bytes)
    # This is the PhysicsGrid Studio generates for a small terrain.
    return (
        "AgMAAAAS////////////////AAAAAAAAAAAAAAABAAAAAAAAAAAAAAAB"
        "AAD/AAD/AAD/AAH+AAAAAAAAAAAAAAABAAAAAAAAAAAAAAABAP//AP//"
        "AP//Af/+AAAAAAAAAAAAAAABAAAAAAAAAAAAAAAB"
        "AAD/AAD/AAD/AAH+AAAAAAAAAAAAAAABAAAAAAAAAAAAAAABAP//AP//"
        "AP//Af/+AAAAAAAAAAAAAAABAAAAAAAAAAAAAAAB"
        "AAD/AAD/AAD/AAH+AAAAAAAAAAAAAAABAAAAAAAAAAAAAAABAAAAAAAAAAA="
    )
