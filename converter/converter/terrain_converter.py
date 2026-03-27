"""
terrain_converter.py -- Convert Unity terrain heightmap to Roblox terrain.

Reads Unity TerrainData .asset files (via UnityPy) and generates Roblox
terrain voxel data or Luau scripts to create terrain in Studio.

Unity terrain uses a continuous heightmap (e.g., 513×513 16-bit samples).
Roblox terrain uses 4×4×4 stud voxels with material per cell.

Conversion approach:
1. Read heightmap from Unity TerrainData binary asset
2. Sample heights at Roblox voxel resolution (every 4 studs)
3. Generate FillBlock calls to create terrain columns at each sample point
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import config

log = logging.getLogger(__name__)

# Roblox terrain voxel size
VOXEL_SIZE = 4  # studs


def read_unity_terrain(terrain_data_path: Path) -> dict[str, Any] | None:
    """Read terrain data from a Unity TerrainData .asset file.

    Returns dict with:
        heights: list of float heights (0-1 normalized)
        resolution: int (e.g., 513)
        scale: (x, y, z) meters per heightmap sample
        terrain_size: (width, max_height, length) in meters
        layers: list of terrain layer names
    """
    try:
        import UnityPy
    except ImportError:
        log.warning("UnityPy required for terrain heightmap extraction")
        return None

    try:
        env = UnityPy.load(str(terrain_data_path))
    except Exception as exc:
        log.warning("Failed to load terrain data: %s", exc)
        return None

    for obj in env.objects:
        if obj.type.name != "TerrainData":
            continue

        try:
            data = obj.read()
        except Exception as exc:
            log.warning("Failed to read TerrainData: %s", exc)
            return None

        hm = data.m_Heightmap
        raw_heights = list(hm.m_Heights)
        resolution = int(math.sqrt(len(raw_heights)))

        # Scale: meters per heightmap sample gap
        scale_x = float(hm.m_Scale.x) if hasattr(hm.m_Scale, "x") else 1.0
        scale_y = float(hm.m_Scale.y) if hasattr(hm.m_Scale, "y") else 600.0
        scale_z = float(hm.m_Scale.z) if hasattr(hm.m_Scale, "z") else 1.0

        # Terrain dimensions in meters
        width = (resolution - 1) * scale_x
        max_height = scale_y
        length = (resolution - 1) * scale_z

        # Normalize heights to 0-1 range (Unity stores as 16-bit unsigned)
        max_raw = 65535.0
        normalized = [h / max_raw for h in raw_heights]

        # Extract terrain layer names and splat alpha maps
        layer_names = []
        splat_alphas: list[list[float]] = []  # list of alpha maps (one per 4 layers)
        splat_resolution = 0
        if hasattr(data, "m_SplatDatabase"):
            splat = data.m_SplatDatabase
            if hasattr(splat, "m_TerrainLayers"):
                for layer_ref in splat.m_TerrainLayers:
                    # Try to resolve the layer name from the referenced object
                    layer_name = ""
                    try:
                        if hasattr(layer_ref, "read"):
                            layer_obj = layer_ref.read()
                            if hasattr(layer_obj, "m_Name"):
                                layer_name = str(layer_obj.m_Name)
                            elif hasattr(layer_obj, "name"):
                                layer_name = str(layer_obj.name)
                        elif hasattr(layer_ref, "get_obj"):
                            layer_obj = layer_ref.get_obj().read()
                            if hasattr(layer_obj, "m_Name"):
                                layer_name = str(layer_obj.m_Name)
                    except Exception:
                        pass
                    if not layer_name:
                        layer_name = str(layer_ref)
                    layer_names.append(layer_name)

                # If layer names are still PPtr references, try to find
                # .terrainlayer files in the project directory
                if layer_names and all("PPtr" in n for n in layer_names):
                    terrain_layer_files = sorted(
                        terrain_data_path.parent.parent.glob("**/*.terrainlayer")
                    )
                    if terrain_layer_files:
                        resolved = [f.stem for f in terrain_layer_files[:len(layer_names)]]
                        log.info("Resolved terrain layer names from files: %s", resolved)
                        layer_names = resolved

            # Extract splat alpha maps (painted texture weights)
            if hasattr(splat, "m_AlphaTextures"):
                for alpha_tex_ref in splat.m_AlphaTextures:
                    try:
                        if hasattr(alpha_tex_ref, "read"):
                            tex = alpha_tex_ref.read()
                        elif hasattr(alpha_tex_ref, "get_obj"):
                            tex = alpha_tex_ref.get_obj().read()
                        else:
                            continue
                        # Get the alpha texture image data
                        if hasattr(tex, "image"):
                            img = tex.image
                            import numpy as np
                            arr = np.array(img)
                            splat_resolution = arr.shape[0]
                            # Each RGBA channel is one layer's alpha
                            for ch in range(min(4, arr.shape[2])):
                                channel_data = (arr[:, :, ch] / 255.0).flatten().tolist()
                                splat_alphas.append(channel_data)
                            log.info("Extracted splat alpha map: %dx%d, %d channels",
                                     arr.shape[0], arr.shape[1], min(4, arr.shape[2]))
                    except Exception as exc:
                        log.debug("Failed to read alpha texture: %s", exc)

        if layer_names:
            log.info("Terrain layers: %s", ", ".join(layer_names))

        log.info(
            "Terrain data: %dx%d heightmap, size=(%.0f, %.0f, %.0f)m, "
            "height range: %.1f-%.1fm",
            resolution, resolution, width, max_height, length,
            min(normalized) * max_height, max(normalized) * max_height,
        )

        result = {
            "heights": normalized,
            "resolution": resolution,
            "scale": (scale_x, scale_y, scale_z),
            "terrain_size": (width, max_height, length),
            "layers": layer_names,
        }
        if splat_alphas:
            result["splat_alphas"] = splat_alphas
            result["splat_resolution"] = splat_resolution
        return result

    log.warning("No TerrainData found in %s", terrain_data_path)
    return None


def generate_terrain_luau(
    terrain_data: dict[str, Any],
    terrain_position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    material: str = "Grass",
    voxel_size: int = VOXEL_SIZE,
) -> str:
    """Generate a Luau script to create Roblox terrain from Unity heightmap data.

    The script uses workspace.Terrain:FillBlock to create terrain columns.
    Designed to be run in Roblox Studio command bar or via MCP.

    Args:
        terrain_data: Output from read_unity_terrain().
        terrain_position: Unity terrain origin in Roblox coordinates.
        material: Default Roblox terrain material.
        voxel_size: Terrain voxel size in studs (4=max detail, 8=balanced, 16=fast).

    Returns:
        Luau source code string.
    """
    heights = terrain_data["heights"]
    resolution = terrain_data["resolution"]
    scale_x, scale_y, scale_z = terrain_data["scale"]
    width, max_height, length = terrain_data["terrain_size"]

    STUDS = config.STUDS_PER_METER
    VOXEL = voxel_size

    # Convert terrain dimensions to studs
    width_studs = width * STUDS
    length_studs = length * STUDS
    max_height_studs = max_height * STUDS

    # Sample interval: every VOXEL_SIZE studs
    samples_x = int(width_studs / VOXEL) + 1
    samples_z = int(length_studs / VOXEL) + 1

    # Roblox terrain position (Unity terrain starts at its position and
    # extends in +X, +Z; Roblox Z is negated)
    rx, ry, rz = terrain_position

    # Build height samples at voxel resolution
    # For each Roblox voxel column, find the corresponding Unity heightmap value
    height_rows = []
    for sz in range(samples_z):
        row = []
        for sx in range(samples_x):
            # Map stud position to heightmap index
            stud_x = sx * VOXEL
            stud_z = sz * VOXEL
            # Convert to heightmap UV
            u = stud_x / width_studs
            v = stud_z / length_studs
            # Clamp to valid range
            u = max(0.0, min(u, 1.0))
            v = max(0.0, min(v, 1.0))
            # Bilinear sample from heightmap
            hx = u * (resolution - 1)
            hz = v * (resolution - 1)
            ix = int(hx)
            iz = int(hz)
            fx = hx - ix
            fz = hz - iz
            ix = min(ix, resolution - 2)
            iz = min(iz, resolution - 2)
            # Unity heightmap is row-major: index = z * resolution + x
            h00 = heights[iz * resolution + ix]
            h10 = heights[iz * resolution + ix + 1]
            h01 = heights[(iz + 1) * resolution + ix]
            h11 = heights[(iz + 1) * resolution + ix + 1]
            h = h00 * (1 - fx) * (1 - fz) + h10 * fx * (1 - fz) + h01 * (1 - fx) * fz + h11 * fx * fz
            height_studs = h * max_height_studs
            row.append(round(height_studs, 1))
        height_rows.append(row)

    # Generate Luau script
    lines = [
        "-- Auto-generated terrain from Unity heightmap",
        f"-- Original size: {width:.0f}x{max_height:.0f}x{length:.0f} meters",
        f"-- Roblox size: {width_studs:.0f}x{max_height_studs:.0f}x{length_studs:.0f} studs",
        f"-- Voxel grid: {samples_x}x{samples_z} columns",
        "",
        "local terrain = workspace.Terrain",
        f"local material = Enum.Material.{material}",
        f"local VOXEL = {VOXEL}",
        f"local originX = {rx}",
        f"local originY = {ry}",
        f"local originZ = {rz}",
        "",
        "-- Height data (row-major, Z then X)",
        "local heightData = {",
    ]

    for row in height_rows:
        lines.append("    {" + ",".join(str(h) for h in row) + "},")
    lines.append("}")

    lines.extend([
        "",
        "-- Clear existing terrain",
        "terrain:Clear()",
        "",
        "-- Height-based material selection (approximates Unity terrain layers)",
        "local function getMaterial(h, maxH)",
        "    local norm = h / maxH",
        "    if norm < 0.15 then return Enum.Material.Sand end",
        "    if norm < 0.35 then return Enum.Material.Grass end",
        "    if norm < 0.60 then return Enum.Material.Ground end",
        "    if norm < 0.85 then return Enum.Material.Rock end",
        "    return Enum.Material.Slate",
        "end",
        "",
        f"local maxHeight = {max_height_studs:.1f}",
        "",
        "-- Fill terrain columns",
        "local total = 0",
        "for z = 1, #heightData do",
        "    for x = 1, #heightData[z] do",
        "        local h = heightData[z][x]",
        "        if h > 0.5 then",
        "            local worldX = originX + (x - 1) * VOXEL",
        "            local worldZ = originZ - (z - 1) * VOXEL  -- Z negated for Roblox",
        "            local halfH = h / 2",
        "            local cf = CFrame.new(worldX, originY + halfH, worldZ)",
        "            local size = Vector3.new(VOXEL, h, VOXEL)",
        "            local mat = getMaterial(h, maxHeight)",
        "            terrain:FillBlock(cf, size, mat)",
        "            total = total + 1",
        "        end",
        "    end",
        "    if z % 10 == 0 then",
        "        task.wait()",
        "    end",
        "end",
        "",
        "-- Fill water at sea level where there is no land",
        "local waterLevel = 2  -- studs (shallow water surface)",
        "local waterCount = 0",
        "for z = 1, #heightData do",
        "    for x = 1, #heightData[z] do",
        "        local h = heightData[z][x]",
        "        if h < 0.5 then",
        "            local worldX = originX + (x - 1) * VOXEL",
        "            local worldZ = originZ - (z - 1) * VOXEL",
        "            terrain:FillBlock(CFrame.new(worldX, originY + waterLevel/2, worldZ), Vector3.new(VOXEL, waterLevel, VOXEL), Enum.Material.Water)",
        "            waterCount = waterCount + 1",
        "        end",
        "    end",
        "    if z % 10 == 0 then task.wait() end",
        "end",
        "",
        'print("Terrain generated: " .. total .. " land columns, " .. waterCount .. " water columns")',
    ])

    return "\n".join(lines)
