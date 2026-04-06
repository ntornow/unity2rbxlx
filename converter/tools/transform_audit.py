#!/usr/bin/env python3
"""Transform audit: compare Unity scene transforms to Roblox rbxlx output.

Parses the Unity YAML scene and the generated rbxlx, computes expected vs actual
world positions/rotations, and reports discrepancies.

Usage:
    python tools/transform_audit.py <unity_scene.unity> <converted.rbxlx> [--threshold 1.0]
"""
import math
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Add converter to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import config


STUDS_PER_METER = config.STUDS_PER_METER


# ---------------------------------------------------------------------------
# Quaternion math
# ---------------------------------------------------------------------------

def quat_multiply(a, b):
    """Multiply quaternions (x,y,z,w) format."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    )


def quat_rotate(q, v):
    """Rotate vector v by quaternion q."""
    qx, qy, qz, qw = q
    vx, vy, vz = v
    t = [
        2.0 * (qy * vz - qz * vy),
        2.0 * (qz * vx - qx * vz),
        2.0 * (qx * vy - qy * vx),
    ]
    return (
        vx + qw * t[0] + (qy * t[2] - qz * t[1]),
        vy + qw * t[1] + (qz * t[0] - qx * t[2]),
        vz + qw * t[2] + (qx * t[1] - qy * t[0]),
    )


def quat_angle_diff(q1, q2):
    """Angle difference in degrees between two quaternions."""
    dot = abs(q1[0]*q2[0] + q1[1]*q2[1] + q1[2]*q2[2] + q1[3]*q2[3])
    dot = min(1.0, dot)
    return math.degrees(2.0 * math.acos(dot))


def unity_to_roblox_pos(x, y, z):
    return (x * STUDS_PER_METER, y * STUDS_PER_METER, -z * STUDS_PER_METER)


def roblox_to_unity_pos(rx, ry, rz):
    return (rx / STUDS_PER_METER, ry / STUDS_PER_METER, -rz / STUDS_PER_METER)


def rotation_matrix_to_quat(r):
    """Convert 3x3 rotation matrix (row-major list of 9) to quaternion (x,y,z,w)."""
    r00, r01, r02, r10, r11, r12, r20, r21, r22 = r
    trace = r00 + r11 + r22
    if trace > 0:
        s = 0.5 / math.sqrt(trace + 1.0)
        w = 0.25 / s
        x = (r21 - r12) * s
        y = (r02 - r20) * s
        z = (r10 - r01) * s
    elif r00 > r11 and r00 > r22:
        s = 2.0 * math.sqrt(1.0 + r00 - r11 - r22)
        w = (r21 - r12) / s
        x = 0.25 * s
        y = (r01 + r10) / s
        z = (r02 + r20) / s
    elif r11 > r22:
        s = 2.0 * math.sqrt(1.0 + r11 - r00 - r22)
        w = (r02 - r20) / s
        x = (r01 + r10) / s
        y = 0.25 * s
        z = (r10 + r21) / s
    else:
        s = 2.0 * math.sqrt(1.0 + r22 - r00 - r11)
        w = (r10 - r01) / s
        x = (r02 + r20) / s
        y = (r12 + r21) / s
        z = 0.25 * s
    return (x, y, z, w)


def roblox_quat_to_unity_quat(rqx, rqy, rqz, rqw):
    """Reverse of unity_quat_to_roblox_quat: (-qx, -qy, qz, qw) -> (qx, qy, qz, qw)."""
    return (-rqx, -rqy, rqz, rqw)


# ---------------------------------------------------------------------------
# Parse Roblox rbxlx
# ---------------------------------------------------------------------------

def parse_rbxlx(path: str) -> dict[str, list[dict]]:
    """Parse rbxlx and extract all Parts/MeshParts with their CFrames.

    Returns dict: name -> list of {name, class, pos_roblox, quat_roblox, pos_unity, quat_unity, path}.
    """
    tree = ET.parse(path)
    root = tree.getroot()
    results = {}

    def _get_path(item, ancestors):
        parts = []
        for a in ancestors:
            p = a.find('Properties')
            if p is not None:
                n = p.find('string[@name="Name"]')
                if n is not None and n.text:
                    parts.append(n.text)
        p = item.find('Properties')
        if p is not None:
            n = p.find('string[@name="Name"]')
            if n is not None and n.text:
                parts.append(n.text)
        return '.'.join(parts)

    def _walk(item, ancestors):
        cls = item.get('class', '')
        if cls in ('Part', 'MeshPart', 'WedgePart', 'SpawnLocation', 'UnionOperation'):
            props = item.find('Properties')
            if props is not None:
                name_el = props.find('string[@name="Name"]')
                name = name_el.text if name_el is not None else '?'
                cf = props.find('CoordinateFrame[@name="CFrame"]')
                if cf is not None:
                    px = float(cf.find('X').text)
                    py = float(cf.find('Y').text)
                    pz = float(cf.find('Z').text)
                    # Extract rotation matrix
                    r = []
                    for rname in ['R00','R01','R02','R10','R11','R12','R20','R21','R22']:
                        el = cf.find(rname)
                        r.append(float(el.text) if el is not None else (1.0 if rname in ('R00','R11','R22') else 0.0))

                    rquat = rotation_matrix_to_quat(r)
                    upos = roblox_to_unity_pos(px, py, pz)
                    uquat = roblox_quat_to_unity_quat(*rquat)

                    entry = {
                        'name': name,
                        'class': cls,
                        'pos_roblox': (px, py, pz),
                        'quat_roblox': rquat,
                        'pos_unity': upos,
                        'quat_unity': uquat,
                        'path': _get_path(item, ancestors),
                    }
                    results.setdefault(name, []).append(entry)

        for child in item:
            if child.tag == 'Item':
                _walk(child, ancestors + [item])

    for child in root:
        if child.tag == 'Item':
            _walk(child, [])

    return results


# ---------------------------------------------------------------------------
# Parse Unity YAML scene (lightweight — just transforms)
# ---------------------------------------------------------------------------

def parse_unity_scene_transforms(scene_path: str) -> dict[str, list[dict]]:
    """Parse Unity YAML scene and compute world transforms for all GameObjects.

    Returns dict: name -> list of {name, unity_world_pos, unity_world_rot, type}.
    """
    from unity.scene_parser import parse_scene
    from unity.guid_resolver import GuidIndex

    project_root = Path(scene_path).parent.parent.parent
    guid_index = GuidIndex(project_root)
    parsed = parse_scene(Path(scene_path))
    all_nodes = list(parsed.all_nodes.values())

    # Build lookup
    node_by_fid = {}
    for node in all_nodes:
        if hasattr(node, 'file_id'):
            node_by_fid[node.file_id] = node

    # Build transform_fid → scene node mapping for parent lookup
    xform_to_node = {}
    if hasattr(parsed, 'transform_fid_to_go_fid'):
        for xform_fid, go_fid in parsed.transform_fid_to_go_fid.items():
            if go_fid in node_by_fid:
                xform_to_node[str(xform_fid)] = node_by_fid[go_fid]

    def _compute_node_world_transform(node):
        """Compute world pos/rot for a scene node by walking parent chain."""
        chain = []
        fid = getattr(node, 'parent_file_id', None)
        while fid and fid in node_by_fid:
            chain.append(node_by_fid[fid])
            fid = getattr(node_by_fid[fid], 'parent_file_id', None)
        world_pos = [0.0, 0.0, 0.0]
        world_rot = [0.0, 0.0, 0.0, 1.0]
        for ancestor in reversed(chain):
            apos = list(ancestor.position)
            arot = list(ancestor.rotation)
            rotated = quat_rotate(world_rot, apos)
            world_pos = [world_pos[0]+rotated[0], world_pos[1]+rotated[1], world_pos[2]+rotated[2]]
            world_rot = list(quat_multiply(world_rot, arot))
        node_rotated = quat_rotate(world_rot, list(node.position))
        wx = world_pos[0] + node_rotated[0]
        wy = world_pos[1] + node_rotated[1]
        wz = world_pos[2] + node_rotated[2]
        world_rot = list(quat_multiply(world_rot, list(node.rotation)))
        return (wx, wy, wz), tuple(world_rot)

    results = {}

    for node in all_nodes:
        wpos, wrot = _compute_node_world_transform(node)
        entry = {
            'name': node.name,
            'unity_world_pos': wpos,
            'unity_world_rot': wrot,
            'type': 'scene_node',
        }
        results.setdefault(node.name, []).append(entry)

    # Extract prefab instance root transforms, composing with parent scene node
    for pi in parsed.prefab_instances:
        pi_name = None
        pi_pos = [0.0, 0.0, 0.0]
        pi_rot = [0.0, 0.0, 0.0, 1.0]

        for mod in pi.modifications:
            if not isinstance(mod, dict):
                continue
            pp = mod.get('propertyPath', '')
            val = mod.get('value', '0')
            if pp == 'm_Name':
                pi_name = val
            try:
                fval = float(val)
            except (ValueError, TypeError):
                continue
            if pp == 'm_LocalPosition.x': pi_pos[0] = fval
            elif pp == 'm_LocalPosition.y': pi_pos[1] = fval
            elif pp == 'm_LocalPosition.z': pi_pos[2] = fval
            elif pp == 'm_LocalRotation.x': pi_rot[0] = fval
            elif pp == 'm_LocalRotation.y': pi_rot[1] = fval
            elif pp == 'm_LocalRotation.z': pi_rot[2] = fval
            elif pp == 'm_LocalRotation.w': pi_rot[3] = fval

        if not pi_name:
            continue

        # Find parent scene node via transform_fid → node mapping
        parent_xform_fid = str(getattr(pi, 'transform_parent_file_id', '') or '')
        parent_node = xform_to_node.get(parent_xform_fid)

        world_pos = list(pi_pos)
        world_rot = list(pi_rot)

        if parent_node:
            p_wpos, p_wrot = _compute_node_world_transform(parent_node)
            # Also include the parent node's own transform
            rotated_local = quat_rotate(list(p_wrot), pi_pos)
            world_pos = [p_wpos[0]+rotated_local[0], p_wpos[1]+rotated_local[1], p_wpos[2]+rotated_local[2]]
            world_rot = list(quat_multiply(list(p_wrot), pi_rot))

        entry = {
            'name': pi_name,
            'unity_world_pos': tuple(world_pos),
            'unity_world_rot': tuple(world_rot),
            'type': 'prefab_instance',
        }
        results.setdefault(pi_name, []).append(entry)

    return results


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def compare_transforms(
    unity_data: dict[str, list[dict]],
    roblox_data: dict[str, list[dict]],
    pos_threshold: float = 1.0,
    rot_threshold: float = 10.0,
) -> list[dict]:
    """Compare Unity expected transforms to Roblox actual transforms.

    Position comparison uses XZ distance only (ignoring Y) because Roblox
    MeshPart.Position is at the bounding box center while Unity's is at the
    FBX origin (typically bottom). The converter correctly applies a Y-offset
    for this pivot difference, so Y differences are expected.

    Returns list of discrepancies sorted by position error (worst first).
    """
    discrepancies = []

    for name, unity_entries in unity_data.items():
        if name not in roblox_data:
            continue

        roblox_entries = roblox_data[name]

        for ue in unity_entries:
            upos = ue['unity_world_pos']
            best_match = None
            best_dist = float('inf')

            for re_entry in roblox_entries:
                rpos = re_entry['pos_unity']
                # Use XZ distance for matching (Y differs due to pivot offset)
                dist_xz = math.sqrt((upos[0]-rpos[0])**2 + (upos[2]-rpos[2])**2)
                if dist_xz < best_dist:
                    best_dist = dist_xz
                    best_match = re_entry

            if best_match is None:
                continue

            # XZ position error (Y is expected to differ by mesh pivot offset)
            rpos = best_match['pos_unity']
            pos_error_xz = math.sqrt((upos[0]-rpos[0])**2 + (upos[2]-rpos[2])**2)
            rot_error = quat_angle_diff(ue['unity_world_rot'], best_match['quat_unity'])

            if pos_error_xz > pos_threshold or rot_error > rot_threshold:
                discrepancies.append({
                    'name': name,
                    'pos_error_m': pos_error_xz,
                    'rot_error_deg': rot_error,
                    'unity_pos': upos,
                    'roblox_pos_as_unity': rpos,
                    'unity_rot': ue['unity_world_rot'],
                    'roblox_rot_as_unity': best_match['quat_unity'],
                    'path': best_match.get('path', ''),
                    'type': ue.get('type', '?'),
                })

    discrepancies.sort(key=lambda d: d['pos_error_m'], reverse=True)
    return discrepancies


def print_report(discrepancies: list[dict], limit: int = 50):
    """Print a human-readable report of transform discrepancies."""
    if not discrepancies:
        print("No discrepancies found above threshold!")
        return

    print(f"\n{'='*80}")
    print(f"TRANSFORM AUDIT REPORT — {len(discrepancies)} discrepancies")
    print(f"{'='*80}\n")

    # Summary stats
    pos_errors = [d['pos_error_m'] for d in discrepancies]
    rot_errors = [d['rot_error_deg'] for d in discrepancies]
    print(f"Position errors (meters): max={max(pos_errors):.2f}, avg={sum(pos_errors)/len(pos_errors):.2f}, median={sorted(pos_errors)[len(pos_errors)//2]:.2f}")
    print(f"Rotation errors (degrees): max={max(rot_errors):.1f}, avg={sum(rot_errors)/len(rot_errors):.1f}, median={sorted(rot_errors)[len(rot_errors)//2]:.1f}")

    # Count by error type
    pos_only = sum(1 for d in discrepancies if d['pos_error_m'] > 1.0 and d['rot_error_deg'] <= 10.0)
    rot_only = sum(1 for d in discrepancies if d['pos_error_m'] <= 1.0 and d['rot_error_deg'] > 10.0)
    both = sum(1 for d in discrepancies if d['pos_error_m'] > 1.0 and d['rot_error_deg'] > 10.0)
    print(f"\nPosition-only errors: {pos_only}")
    print(f"Rotation-only errors: {rot_only}")
    print(f"Both position+rotation: {both}")

    print(f"\n{'─'*80}")
    print(f"Top {min(limit, len(discrepancies))} worst offenders:")
    print(f"{'─'*80}")
    for i, d in enumerate(discrepancies[:limit]):
        print(f"\n{i+1}. {d['name']} ({d['type']})")
        print(f"   Path: {d['path']}")
        print(f"   Position error: {d['pos_error_m']:.2f}m")
        print(f"     Unity:  ({d['unity_pos'][0]:.2f}, {d['unity_pos'][1]:.2f}, {d['unity_pos'][2]:.2f})")
        print(f"     Roblox: ({d['roblox_pos_as_unity'][0]:.2f}, {d['roblox_pos_as_unity'][1]:.2f}, {d['roblox_pos_as_unity'][2]:.2f})")
        print(f"   Rotation error: {d['rot_error_deg']:.1f}°")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Audit object placement between Unity and Roblox')
    parser.add_argument('unity_scene', help='Path to Unity .unity scene file')
    parser.add_argument('rbxlx', help='Path to converted .rbxlx file')
    parser.add_argument('--pos-threshold', type=float, default=0.5, help='Position error threshold in meters')
    parser.add_argument('--rot-threshold', type=float, default=5.0, help='Rotation error threshold in degrees')
    parser.add_argument('--limit', type=int, default=50, help='Max entries to show')
    args = parser.parse_args()

    print("Parsing Roblox rbxlx...")
    roblox_data = parse_rbxlx(args.rbxlx)
    print(f"  Found {sum(len(v) for v in roblox_data.values())} parts")

    print("Parsing Unity scene...")
    unity_data = parse_unity_scene_transforms(args.unity_scene)
    print(f"  Found {sum(len(v) for v in unity_data.values())} nodes")

    print("Comparing transforms...")
    discrepancies = compare_transforms(unity_data, roblox_data, args.pos_threshold, args.rot_threshold)

    print_report(discrepancies, args.limit)
