"""Generate Luau injection chunks for the converted SimpleFPS scene."""

import json
from pathlib import Path
from unity.scene_parser import parse_scene
from unity.prefab_parser import parse_prefabs
from unity.guid_resolver import build_guid_index
from core.coordinate_system import (
    unity_to_roblox_pos,
    unity_quat_to_roblox_quat,
    quaternion_to_rotation_matrix,
)

project = Path("../test_projects/SimpleFPS")
scene = parse_scene(project / "Assets/Scenes/main.unity")
prefabs = parse_prefabs(project)
guid_index = build_guid_index(project)


def safe_name(name):
    return name.replace('"', "").replace("\\", "").replace("\n", "")[:50]


def fmt(f):
    return f"{f:.4f}"


def node_to_luau(node, parent_var, lines, counter):
    counter[0] += 1
    var = f"p{counter[0]}"

    has_mesh = node.mesh_guid is not None
    has_children = len(node.children) > 0

    if has_children and not has_mesh:
        cls = "Model"
    elif has_mesh:
        cls = "MeshPart"
    else:
        cls = "Part"

    rx, ry, rz = unity_to_roblox_pos(*node.position)
    rqx, rqy, rqz, rqw = unity_quat_to_roblox_quat(*node.rotation)
    mat = quaternion_to_rotation_matrix(rqx, rqy, rqz, rqw)

    sx = abs(node.scale[0]) if hasattr(node, "scale") else 1
    sy = abs(node.scale[1]) if hasattr(node, "scale") else 1
    sz = abs(node.scale[2]) if hasattr(node, "scale") else 1
    # Avoid zero-size parts
    sx = max(sx, 0.1)
    sy = max(sy, 0.1)
    sz = max(sz, 0.1)

    name = safe_name(node.name)

    if cls == "Model":
        lines.append(f'local {var} = Instance.new("Model")')
        lines.append(f'{var}.Name = "{name}"')
        lines.append(f"{var}.Parent = {parent_var}")
    else:
        lines.append(f'local {var} = Instance.new("Part")')
        lines.append(f'{var}.Name = "{name}"')
        lines.append(f"{var}.Size = Vector3.new({fmt(sx)}, {fmt(sy)}, {fmt(sz)})")
        cf = f"CFrame.new({fmt(rx)}, {fmt(ry)}, {fmt(rz)})"
        lines.append(f"{var}.CFrame = {cf}")
        lines.append(f"{var}.Anchored = true")

        # Check for light
        for comp in node.components:
            if comp.component_type == "Light":
                lt = comp.properties.get("m_Type", 2)
                color = comp.properties.get("m_Color", {})
                cr = float(color.get("r", 1))
                cg = float(color.get("g", 1))
                cb = float(color.get("b", 1))
                intensity = float(comp.properties.get("m_Intensity", 1))
                rng = float(comp.properties.get("m_Range", 10))
                light_cls = {0: "SpotLight", 2: "PointLight"}.get(lt, "PointLight")
                if lt != 1:
                    ln = f"l{counter[0]}"
                    lines.append(f'local {ln} = Instance.new("{light_cls}")')
                    lines.append(f"{ln}.Brightness = {intensity}")
                    lines.append(f"{ln}.Color = Color3.new({fmt(cr)}, {fmt(cg)}, {fmt(cb)})")
                    lines.append(f"{ln}.Range = {fmt(rng * 3)}")
                    lines.append(f"{ln}.Parent = {var}")

        lines.append(f"{var}.Parent = {parent_var}")

    for child in node.children:
        node_to_luau(child, var, lines, counter)


all_lines = []
counter = [0]

# Scene hierarchy (direct nodes)
for root in scene.roots:
    node_to_luau(root, "workspace", all_lines, counter)

# Prefab instances
for pi in scene.prefab_instances:
    resolved = guid_index.resolve(pi.source_prefab_guid)
    if not resolved:
        continue
    template = prefabs.by_name.get(resolved.stem)
    if not template:
        continue

    pos = [0.0, 0.0, 0.0]
    rot = [0.0, 0.0, 0.0, 1.0]
    name_override = None
    for mod in pi.modifications:
        if not isinstance(mod, dict):
            continue
        pp = mod.get("propertyPath", "")
        val = mod.get("value", "0")
        try:
            fval = float(val)
        except (ValueError, TypeError):
            fval = 0
            if pp == "m_Name":
                name_override = val
            continue
        if pp == "m_LocalPosition.x":
            pos[0] = fval
        elif pp == "m_LocalPosition.y":
            pos[1] = fval
        elif pp == "m_LocalPosition.z":
            pos[2] = fval
        elif pp == "m_LocalRotation.x":
            rot[0] = fval
        elif pp == "m_LocalRotation.y":
            rot[1] = fval
        elif pp == "m_LocalRotation.z":
            rot[2] = fval
        elif pp == "m_LocalRotation.w":
            rot[3] = fval

    # Convert transform
    rx, ry, rz = unity_to_roblox_pos(*pos)
    rqx, rqy, rqz, rqw = unity_quat_to_roblox_quat(*rot)
    mat = quaternion_to_rotation_matrix(rqx, rqy, rqz, rqw)

    counter[0] += 1
    var = f"p{counter[0]}"
    name = safe_name(name_override or template.name)

    has_children = template.root and len(template.root.children) > 0

    if has_children:
        all_lines.append(f'local {var} = Instance.new("Model")')
        all_lines.append(f'{var}.Name = "{name}"')
        all_lines.append(f"{var}.Parent = workspace")
    else:
        all_lines.append(f'local {var} = Instance.new("Part")')
        all_lines.append(f'{var}.Name = "{name}"')
        all_lines.append(f"{var}.Anchored = true")
        all_lines.append(
            f"{var}.CFrame = CFrame.new({fmt(rx)}, {fmt(ry)}, {fmt(rz)})"
        )
        all_lines.append(f"{var}.Parent = workspace")

    # If prefab has children, also create them
    if template.root and template.root.children:
        for child in template.root.children:
            node_to_luau(child, var, all_lines, counter)

# Write chunks
chunk_size = 600
chunks = []
for i in range(0, len(all_lines), chunk_size):
    chunk = all_lines[i : i + chunk_size]
    chunks.append("\n".join(chunk))

output_dir = Path("output/SimpleFPS/injection")
output_dir.mkdir(parents=True, exist_ok=True)
for i, chunk in enumerate(chunks):
    (output_dir / f"chunk_{i:03d}.luau").write_text(chunk, encoding="utf-8")

print(f"Generated {len(all_lines)} lines of Luau in {len(chunks)} chunks")
print(f"Total instances created: {counter[0]}")
