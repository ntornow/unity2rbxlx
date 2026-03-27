"""
studio_resolver.py -- Resolve uploaded asset IDs via Roblox Studio MCP.

After uploading assets to Roblox, certain IDs need to be resolved:
1. Mesh Model IDs → real MeshIds (MeshPart.MeshId) + native sizes
2. Texture Decal IDs → Image IDs (SurfaceAppearance needs Image, not Decal)

This module generates Luau scripts to run in Studio via MCP execute_luau.
The results are parsed and stored in the ConversionContext.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def generate_mesh_resolution_luau(
    uploaded_assets: dict[str, str],
    batch_size: int = 10,
) -> list[str]:
    """Generate Luau scripts to resolve mesh Model IDs to MeshPart hierarchies.

    Each script loads a batch of Model assets via InsertService:LoadAsset,
    extracts all MeshPart descendants with their MeshId, Size, Position,
    and TextureID, then returns the data as a pipe-delimited string.

    Returns a list of Luau script strings, one per batch.
    """
    mesh_entries = []
    for path, url in uploaded_assets.items():
        if any(path.lower().endswith(ext) for ext in ['.fbx', '.obj']):
            asset_id = url.replace("rbxassetid://", "")
            mesh_entries.append((path, int(asset_id)))

    if not mesh_entries:
        return []

    scripts = []
    for i in range(0, len(mesh_entries), batch_size):
        batch = mesh_entries[i:i + batch_size]
        entries_lua = ",\n".join(
            f'    {{id={aid}, path="{path}"}}'
            for path, aid in batch
        )
        script = f"""local InsertService = game:GetService("InsertService")
local models = {{
{entries_lua}
}}
local allData = {{}}
for _, entry in models do
    local ok, model = pcall(InsertService.LoadAsset, InsertService, entry.id)
    if not ok then continue end
    for _, d in model:GetDescendants() do
        if d:IsA("MeshPart") then
            local sz = d.Size; local pos = d.Position
            table.insert(allData, string.format("%s|%s|%s|%.4f,%.4f,%.4f|%.4f,%.4f,%.4f|%s",
                entry.path, d.Name, d.MeshId, sz.X, sz.Y, sz.Z, pos.X, pos.Y, pos.Z,
                d.TextureID ~= "" and d.TextureID or ""))
        end
    end
    model:Destroy(); task.wait(0.3)
end
return table.concat(allData, "\\n")"""
        scripts.append(script)

    return scripts


def parse_mesh_resolution_result(result: str) -> dict:
    """Parse the result of a mesh resolution Luau script.

    Returns dict mapping fbx_path -> list of sub-mesh dicts with:
    name, meshId, size, position, textureId
    """
    hierarchies: dict[str, list] = {}
    native_sizes: dict[str, list] = {}
    mesh_ids: dict[str, str] = {}

    for line in result.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue

        fbx_path = parts[0]
        name = parts[1]
        mesh_id = parts[2]
        size = [float(x) for x in parts[3].split(",")]
        pos = [float(x) for x in parts[4].split(",")]
        tex_id = parts[5] if len(parts) > 5 else ""

        if fbx_path not in hierarchies:
            hierarchies[fbx_path] = []
        hierarchies[fbx_path].append({
            "name": name,
            "meshId": mesh_id,
            "size": size,
            "position": pos,
            "textureId": tex_id,
        })

        # For backward compat: store first mesh's native size
        if fbx_path not in native_sizes:
            native_sizes[fbx_path] = size

        # Store first mesh's real MeshId (replaces Model ID)
        if fbx_path not in mesh_ids:
            mesh_ids[fbx_path] = mesh_id

    return {
        "hierarchies": hierarchies,
        "native_sizes": native_sizes,
        "mesh_ids": mesh_ids,
    }


def generate_texture_resolution_luau(
    uploaded_assets: dict[str, str],
    batch_size: int = 10,
) -> list[str]:
    """Generate Luau scripts to resolve Decal IDs to Image IDs.

    Returns a list of Luau script strings, one per batch.
    """
    tex_entries = []
    for path, url in uploaded_assets.items():
        if any(path.lower().endswith(ext) for ext in
               ['.png', '.jpg', '.jpeg', '.bmp', '.tga', '.tif', '.tiff', '.psd']):
            asset_id = url.replace("rbxassetid://", "")
            tex_entries.append(int(asset_id))

    if not tex_entries:
        return []

    scripts = []
    for i in range(0, len(tex_entries), batch_size):
        batch = tex_entries[i:i + batch_size]
        ids_str = ",".join(str(x) for x in batch)
        script = f"""local InsertService = game:GetService("InsertService")
local ids = {{{ids_str}}}
local r = {{}}
for _, did in ids do
    local ok, m = pcall(InsertService.LoadAsset, InsertService, did)
    if ok and m then
        for _, d in m:GetDescendants() do
            if d:IsA("Decal") then
                local iid = d.Texture:match("id=(%d+)")
                if iid then table.insert(r, did.."|"..iid) end
                break
            end
        end
        m:Destroy()
    end
    task.wait(0.2)
end
return table.concat(r, "\\n")"""
        scripts.append(script)

    return scripts


def parse_texture_resolution_result(result: str) -> dict[str, str]:
    """Parse the result of a texture resolution Luau script.

    Returns dict mapping Decal ID string -> Image ID string.
    """
    mapping = {}
    for line in result.strip().split("\n"):
        if "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) == 2:
            mapping[parts[0]] = parts[1]
    return mapping


def apply_texture_resolution(
    uploaded_assets: dict[str, str],
    decal_to_image: dict[str, str],
) -> int:
    """Replace Decal IDs with Image IDs in uploaded_assets.

    Returns the number of replacements made.
    """
    updated = 0
    for path, url in list(uploaded_assets.items()):
        if any(path.lower().endswith(ext) for ext in
               ['.png', '.jpg', '.jpeg', '.bmp', '.tga', '.tif', '.tiff', '.psd']):
            decal_id = url.replace("rbxassetid://", "")
            if decal_id in decal_to_image:
                image_id = decal_to_image[decal_id]
                uploaded_assets[path] = f"rbxassetid://{image_id}"
                updated += 1
    return updated
