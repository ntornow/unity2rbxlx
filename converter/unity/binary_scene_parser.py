"""
binary_scene_parser.py -- Parse Unity binary serialized scene files using UnityPy.

Produces the same ParsedScene output as scene_parser.py but reads binary
.unity files that the YAML parser cannot handle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.unity_types import (
    ComponentData,
    ParsedScene,
    PrefabInstanceData,
    SceneNode,
)

log = logging.getLogger(__name__)

# Component class IDs we care about
_COMPONENT_TYPES = {
    "Light", "AudioSource", "Camera",
    "BoxCollider", "SphereCollider", "CapsuleCollider", "MeshCollider",
    "Rigidbody", "CharacterController",
    "MeshFilter", "MeshRenderer", "SkinnedMeshRenderer",
    "ParticleSystem", "Canvas", "RectTransform",
    "MonoBehaviour",
}


def parse_binary_scene(scene_path: Path) -> ParsedScene:
    """Parse a binary Unity scene file into a ParsedScene.

    Args:
        scene_path: Path to a binary .unity file.

    Returns:
        A ParsedScene with roots, all_nodes, prefab_instances, and render_settings.
    """
    try:
        import UnityPy
    except ImportError:
        log.error("UnityPy is required for binary scene parsing. Install with: pip install UnityPy")
        return ParsedScene(scene_path=scene_path, roots=[], all_nodes={}, prefab_instances=[], render_settings={})

    # Check for Git LFS pointer files (small text files with "version https://git-lfs")
    try:
        with open(scene_path, "rb") as f:
            header = f.read(50)
        if b"git-lfs.github.com" in header or b"version https://git-lfs" in header:
            log.warning("Scene '%s' is a Git LFS pointer — run 'git lfs pull' to fetch actual data",
                        scene_path.name)
            return ParsedScene(scene_path=scene_path, roots=[], all_nodes={}, prefab_instances=[], render_settings={})
    except OSError:
        pass

    log.info("Parsing binary scene: %s", scene_path.name)
    env = UnityPy.load(str(scene_path))

    # Index all objects by path_id for cross-referencing
    objects_by_id: dict[int, Any] = {}
    for obj in env.objects:
        objects_by_id[obj.path_id] = obj

    # Pass 1: Build GameObjects and their Transforms
    gameobjects: dict[int, dict] = {}  # path_id -> {name, active, components, transform_id}
    transforms: dict[int, dict] = {}   # path_id -> {go_id, parent_id, children_ids, pos, rot, scale}
    prefab_instances: list[dict] = []
    render_settings: dict[str, Any] = {}

    for obj in env.objects:
        try:
            if obj.type.name == "GameObject":
                go = obj.read_typetree() if obj.serialized_type and obj.serialized_type.nodes else _safe_read(obj)
                if go is None:
                    continue
                name = _get(go, "m_Name", f"GameObject_{obj.path_id}")
                active = bool(_get(go, "m_IsActive", 1))
                layer = int(_get(go, "m_Layer", 0))
                tag = int(_get(go, "m_Tag", 0))

                gameobjects[obj.path_id] = {
                    "name": name,
                    "active": active,
                    "layer": layer,
                    "tag": tag,
                    "components": [],
                    "transform_id": None,
                }

            elif obj.type.name in ("Transform", "RectTransform"):
                t = obj.read_typetree() if obj.serialized_type and obj.serialized_type.nodes else _safe_read(obj)
                if t is None:
                    continue
                go_ref = _get(t, "m_GameObject", {})
                go_id = _get_path_id(go_ref)
                parent_ref = _get(t, "m_Father", {})
                parent_id = _get_path_id(parent_ref)

                children_refs = _get(t, "m_Children", [])
                children_ids = [_get_path_id(c) for c in children_refs if _get_path_id(c)]

                pos = _get(t, "m_LocalPosition", {"x": 0, "y": 0, "z": 0})
                rot = _get(t, "m_LocalRotation", {"x": 0, "y": 0, "z": 0, "w": 1})
                scale = _get(t, "m_LocalScale", {"x": 1, "y": 1, "z": 1})

                transforms[obj.path_id] = {
                    "go_id": go_id,
                    "parent_id": parent_id,
                    "children_ids": children_ids,
                    "position": (_float(pos, "x"), _float(pos, "y"), _float(pos, "z")),
                    "rotation": (_float(rot, "x"), _float(rot, "y"), _float(rot, "z"), _float(rot, "w")),
                    "scale": (_float(scale, "x"), _float(scale, "y"), _float(scale, "z")),
                }

            elif obj.type.name == "PrefabInstance":
                pi = obj.read_typetree() if obj.serialized_type and obj.serialized_type.nodes else _safe_read(obj)
                if pi is None:
                    continue
                prefab_instances.append(pi)

            elif obj.type.name == "RenderSettings":
                rs = obj.read_typetree() if obj.serialized_type and obj.serialized_type.nodes else _safe_read(obj)
                if rs:
                    render_settings = dict(rs) if not isinstance(rs, dict) else rs

        except Exception as exc:
            log.debug("Failed to read object %s (type=%s): %s", obj.path_id, obj.type.name, exc)
            continue

    # Pass 2: Link transforms to GameObjects
    go_to_transform: dict[int, int] = {}  # go_path_id -> transform_path_id
    transform_to_go: dict[int, int] = {}  # transform_path_id -> go_path_id

    for t_id, t_data in transforms.items():
        go_id = t_data["go_id"]
        if go_id and go_id in gameobjects:
            gameobjects[go_id]["transform_id"] = t_id
            go_to_transform[go_id] = t_id
            transform_to_go[t_id] = go_id

    # Pass 3: Extract components for each GameObject
    for obj in env.objects:
        if obj.type.name in ("GameObject", "Transform", "RectTransform", "PrefabInstance", "RenderSettings"):
            continue
        try:
            data = obj.read_typetree() if obj.serialized_type and obj.serialized_type.nodes else _safe_read(obj)
            if data is None:
                continue
            go_ref = _get(data, "m_GameObject", {})
            go_id = _get_path_id(go_ref)
            if go_id and go_id in gameobjects:
                comp_type = obj.type.name
                props = dict(data) if not isinstance(data, dict) else data
                gameobjects[go_id]["components"].append({
                    "type": comp_type,
                    "properties": props,
                })
        except Exception:
            continue

    # Pass 4: Build SceneNode tree
    all_nodes: dict[str, SceneNode] = {}
    node_by_go_id: dict[int, SceneNode] = {}

    for go_id, go_data in gameobjects.items():
        t_id = go_data.get("transform_id")
        t_data = transforms.get(t_id, {}) if t_id else {}

        pos = t_data.get("position", (0, 0, 0))
        rot = t_data.get("rotation", (0, 0, 0, 1))
        scale = t_data.get("scale", (1, 1, 1))

        # Extract mesh GUID from MeshFilter/MeshRenderer/SkinnedMeshRenderer
        mesh_guid = None
        components = []
        for comp in go_data["components"]:
            ct = comp["type"]
            components.append(ComponentData(
                component_type=ct,
                file_id=str(go_id),
                properties=comp["properties"],
            ))
            # Check for mesh reference
            if ct in ("MeshFilter", "SkinnedMeshRenderer"):
                mesh_ref = comp["properties"].get("m_Mesh", {})
                if isinstance(mesh_ref, dict):
                    guid = mesh_ref.get("m_FileID", "") or mesh_ref.get("guid", "")
                    if guid:
                        mesh_guid = str(guid)

        node = SceneNode(
            name=go_data["name"],
            file_id=str(go_id),
            active=go_data["active"],
            layer=go_data.get("layer", 0),
            tag=str(go_data.get("tag", "Untagged")),
            position=pos,
            rotation=rot,
            scale=scale,
            components=components,
            mesh_guid=mesh_guid,
        )
        all_nodes[str(go_id)] = node
        node_by_go_id[go_id] = node

    # Pass 5: Build parent-child relationships
    roots: list[SceneNode] = []
    for t_id, t_data in transforms.items():
        go_id = transform_to_go.get(t_id)
        if not go_id or go_id not in node_by_go_id:
            continue

        node = node_by_go_id[go_id]
        parent_t_id = t_data.get("parent_id")

        if not parent_t_id or parent_t_id not in transform_to_go:
            roots.append(node)
        else:
            parent_go_id = transform_to_go.get(parent_t_id)
            if parent_go_id and parent_go_id in node_by_go_id:
                node_by_go_id[parent_go_id].children.append(node)
            else:
                roots.append(node)

    # Pass 6: Convert PrefabInstance data
    pi_list: list[PrefabInstanceData] = []
    for pi_data in prefab_instances:
        source_ref = _get(pi_data, "m_SourcePrefab", {})
        source_guid = ""
        if isinstance(source_ref, dict):
            source_guid = str(source_ref.get("guid", source_ref.get("m_FileID", "")))

        modification = _get(pi_data, "m_Modification", {})
        modifications = []
        if isinstance(modification, dict):
            mods = modification.get("m_Modifications", [])
            if isinstance(mods, list):
                for mod in mods:
                    if isinstance(mod, dict):
                        modifications.append({
                            "propertyPath": mod.get("propertyPath", ""),
                            "value": mod.get("value", ""),
                        })

        # Extract transform parent and file IDs from the PrefabInstance data
        transform_parent = ""
        if isinstance(modification, dict):
            transform_parent_ref = modification.get("m_TransformParent", {})
            if isinstance(transform_parent_ref, dict):
                tp_id = transform_parent_ref.get("m_FileID", transform_parent_ref.get("m_PathID", 0))
                transform_parent = str(tp_id) if tp_id else ""

        source_file_id = ""
        if isinstance(source_ref, dict):
            sf_id = source_ref.get("m_FileID", source_ref.get("m_PathID", 0))
            source_file_id = str(sf_id) if sf_id else ""

        if source_guid:
            pi_list.append(PrefabInstanceData(
                file_id=str(pi_data.get("m_ObjectHideFlags", id(pi_data))),
                source_prefab_guid=source_guid,
                source_prefab_file_id=source_file_id,
                transform_parent_file_id=transform_parent,
                modifications=modifications,
            ))

    log.info(
        "Binary scene parsed: %d GameObjects, %d roots, %d transforms, %d prefab instances",
        len(gameobjects), len(roots), len(transforms), len(pi_list),
    )

    return ParsedScene(
        scene_path=scene_path,
        roots=roots,
        all_nodes=all_nodes,
        prefab_instances=pi_list,
        render_settings=render_settings,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_read(obj) -> dict | None:
    """Safely read an object, trying multiple methods."""
    try:
        data = obj.parse_as_dict()
        return data
    except Exception:
        pass
    try:
        data = obj.read()
        if hasattr(data, '__dict__'):
            return vars(data)
        return data
    except Exception:
        return None


def _get(data: Any, key: str, default: Any = None) -> Any:
    """Get a value from a dict-like object."""
    if isinstance(data, dict):
        return data.get(key, default)
    return getattr(data, key, default)


def _get_path_id(ref: Any) -> int | None:
    """Extract path_id from a PPtr reference."""
    if isinstance(ref, dict):
        pid = ref.get("m_PathID", ref.get("path_id", ref.get("fileID", 0)))
        return int(pid) if pid else None
    if hasattr(ref, "path_id"):
        return ref.path_id
    if hasattr(ref, "m_PathID"):
        return ref.m_PathID
    return None


def _float(data: Any, key: str, default: float = 0.0) -> float:
    """Extract a float from a dict-like object."""
    if isinstance(data, dict):
        return float(data.get(key, default))
    return float(getattr(data, key, default))
