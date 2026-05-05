"""
scene_parser.py -- Parses Unity .unity scene files into a structured hierarchy.

7-pass algorithm:
  1. Index documents by fileID and classify by classID
  2. Build SceneNode stubs from GameObjects
  3. Resolve Transforms (position, rotation, scale, parent hierarchy)
  4. Attach other components to GameObjects
  5. Wire parent/child hierarchy
  6. Record PrefabInstance documents
  7. Extract RenderSettings
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from core.unity_types import (
    ComponentData,
    PrefabInstanceData,
    SceneNode,
    ParsedScene,
)
from unity.yaml_parser import (
    CID_GAME_OBJECT,
    CID_TRANSFORM,
    CID_RECT_TRANSFORM,
    CID_RENDER_SETTINGS,
    CID_PREFAB_INSTANCE,
    CID_MESH_FILTER,
    CID_MESH_RENDERER,
    CID_SKINNED_MESH_RENDERER,
    CID_ANIMATOR,
    KNOWN_COMPONENT_CIDS,
    COMPONENT_CID_TO_NAME,
    extract_vec3,
    extract_quat,
    ref_file_id,
    ref_guid,
    parse_documents,
    doc_body,
    is_text_yaml,
)

log = logging.getLogger(__name__)


def parse_scene(scene_path: str | Path) -> ParsedScene:
    """Parse a Unity .unity scene file into a tree of SceneNode objects."""
    scene_path = Path(scene_path).resolve()
    if not scene_path.exists():
        raise FileNotFoundError(f"Scene file not found: {scene_path}")

    if not is_text_yaml(scene_path):
        log.info("Scene %s is binary -- using UnityPy binary parser.", scene_path.name)
        try:
            from unity.binary_scene_parser import parse_binary_scene
            return parse_binary_scene(scene_path)
        except ImportError:
            log.warning("UnityPy not installed. Install with: pip install UnityPy")
            return ParsedScene(scene_path=scene_path)
        except Exception as exc:
            log.warning("Binary scene parsing failed for %s: %s", scene_path.name, exc)
            return ParsedScene(scene_path=scene_path)

    raw_text = scene_path.read_text(encoding="utf-8", errors="replace")
    result = ParsedScene(scene_path=scene_path)
    triples = parse_documents(raw_text, warnings_out=result.parse_warnings)

    result.raw_documents = [doc for _, _, doc in triples]

    # ------------------------------------------------------------------
    # Pass 1: Index all documents
    # ------------------------------------------------------------------
    file_id_to_doc: dict[str, dict] = {}
    file_id_to_class: dict[str, int] = {}
    go_docs: dict[str, dict] = {}
    transform_docs: dict[str, dict] = {}
    component_docs: list[tuple[str, int, dict]] = []
    prefab_instance_docs: list[tuple[str, dict]] = []
    render_settings_docs: list[tuple[str, dict]] = []

    for cid, fid, doc in triples:
        body = doc_body(doc)
        file_id_to_doc[fid] = body
        file_id_to_class[fid] = cid

        if cid == CID_GAME_OBJECT:
            go_docs[fid] = body
        elif cid in (CID_TRANSFORM, CID_RECT_TRANSFORM):
            transform_docs[fid] = body
        elif cid == CID_PREFAB_INSTANCE:
            prefab_instance_docs.append((fid, body))
        elif cid == CID_RENDER_SETTINGS:
            render_settings_docs.append((fid, body))
        elif cid in KNOWN_COMPONENT_CIDS:
            component_docs.append((fid, cid, body))

    log.info("Pass 1: %d documents, %d GameObjects, %d Transforms, %d components",
             len(triples), len(go_docs), len(transform_docs), len(component_docs))

    # ------------------------------------------------------------------
    # Pass 2: Build SceneNode stubs
    # ------------------------------------------------------------------
    for fid, go in go_docs.items():
        node = SceneNode(
            name=go.get("m_Name", "GameObject"),
            file_id=fid,
            active=bool(go.get("m_IsActive", 1)),
            layer=int(go.get("m_Layer", 0)),
            tag=go.get("m_TagString", "Untagged"),
        )
        result.all_nodes[fid] = node

    # ------------------------------------------------------------------
    # Pass 3: Resolve Transforms
    # ------------------------------------------------------------------
    go_fid_to_transform: dict[str, tuple[str, dict]] = {}
    for xform_fid, xform in transform_docs.items():
        go_ref = ref_file_id(xform.get("m_GameObject"))
        if go_ref:
            go_fid_to_transform[go_ref] = (xform_fid, xform)

    xform_fid_to_go_fid: dict[str, str] = {}
    for go_fid, (xform_fid, _) in go_fid_to_transform.items():
        xform_fid_to_go_fid[xform_fid] = go_fid
    result.transform_fid_to_go_fid = dict(xform_fid_to_go_fid)

    for go_fid, node in result.all_nodes.items():
        entry = go_fid_to_transform.get(go_fid)
        if entry is None:
            continue
        xform_fid, xform = entry

        node.position = extract_vec3(xform, "m_LocalPosition")
        node.rotation = extract_quat(xform, "m_LocalRotation")
        node.scale = extract_vec3(xform, "m_LocalScale")

        father_xform_fid = ref_file_id(xform.get("m_Father"))
        if father_xform_fid:
            parent_go_fid = xform_fid_to_go_fid.get(father_xform_fid)
            if parent_go_fid and parent_go_fid in result.all_nodes:
                node.parent_file_id = parent_go_fid

        comp_type = ("RectTransform"
                     if file_id_to_class.get(xform_fid) == CID_RECT_TRANSFORM
                     else "Transform")
        node.components.append(ComponentData(
            component_type=comp_type,
            file_id=xform_fid,
            properties=xform,
        ))

    # ------------------------------------------------------------------
    # Pass 4: Attach other components
    # ------------------------------------------------------------------
    for comp_fid, cid, body in component_docs:
        go_ref = ref_file_id(body.get("m_GameObject"))
        if not go_ref:
            continue
        node = result.all_nodes.get(go_ref)
        if node is None:
            continue

        comp_type = COMPONENT_CID_TO_NAME.get(cid, f"Component_{cid}")
        node.components.append(ComponentData(
            component_type=comp_type,
            file_id=comp_fid,
            properties=body,
        ))

        # Extract mesh GUID
        if cid in (CID_MESH_FILTER, CID_SKINNED_MESH_RENDERER):
            mesh_ref = body.get("m_Mesh", {})
            guid = ref_guid(mesh_ref)
            if guid:
                node.mesh_guid = guid
                node.mesh_file_id = str(mesh_ref.get("fileID", ""))
                result.referenced_mesh_guids.add(guid)

        # Extract material GUIDs
        if cid in (CID_MESH_RENDERER, CID_SKINNED_MESH_RENDERER):
            for mat_ref in body.get("m_Materials") or []:
                guid = ref_guid(mat_ref)
                if guid:
                    result.referenced_material_guids.add(guid)

        # Extract Animator controller GUID for 4.5 routing.
        if cid == CID_ANIMATOR:
            ctrl_guid = ref_guid(body.get("m_Controller", {}))
            if ctrl_guid:
                result.referenced_animator_controller_guids.add(ctrl_guid)

    # ------------------------------------------------------------------
    # Pass 5: Wire parent/child hierarchy
    #
    # Preserve Unity's display order by walking each Transform's
    # ``m_Children`` list. Without this, children land in YAML-document
    # iteration order — which differs from Unity's m_Children order — so
    # scripts that read ``transform.GetChild(i)`` translate to a Roblox
    # ``GetChildren()[i]`` lookup that returns the wrong sibling. (E.g.
    # SimpleFPS Turret model: m_Children=[Base, Collider] in Unity, but
    # the parser visited the YAML docs as [Collider, Base], breaking
    # rotation and bullet origin lookups.) Fall back to YAML order for
    # any child whose fileID isn't in the parent's m_Children list.
    for node in result.all_nodes.values():
        if node.parent_file_id is None:
            result.roots.append(node)
    for go_fid, node in result.all_nodes.items():
        entry = go_fid_to_transform.get(go_fid)
        if entry is None:
            continue
        _, xform = entry
        ordered_child_xform_fids = [
            ref_file_id(c) for c in (xform.get("m_Children") or [])
            if ref_file_id(c)
        ]
        ordered_child_go_fids = [
            xform_fid_to_go_fid.get(cf) for cf in ordered_child_xform_fids
            if xform_fid_to_go_fid.get(cf)
        ]
        # Append in Unity m_Children order first…
        seen: set[str] = set()
        for cgo in ordered_child_go_fids:
            child = result.all_nodes.get(cgo)
            if child and child.parent_file_id == go_fid and cgo not in seen:
                node.children.append(child)
                seen.add(cgo)
        # …then any stragglers (shouldn't happen for well-formed YAML).
        for other_go, other in result.all_nodes.items():
            if other.parent_file_id == go_fid and other_go not in seen:
                node.children.append(other)
                seen.add(other_go)

    # ------------------------------------------------------------------
    # Pass 6: Record PrefabInstance documents
    # ------------------------------------------------------------------
    for pi_fid, body in prefab_instance_docs:
        source_ref = body.get("m_SourcePrefab", {})
        source_guid = ref_guid(source_ref) or ""
        source_file_id = str(source_ref.get("fileID", ""))

        modification = body.get("m_Modification", {})
        transform_parent = ref_file_id(modification.get("m_TransformParent")) or ""
        modifications = modification.get("m_Modifications", []) or []
        removed = modification.get("m_RemovedComponents", []) or []

        result.prefab_instances.append(PrefabInstanceData(
            file_id=pi_fid,
            source_prefab_guid=source_guid,
            source_prefab_file_id=source_file_id,
            transform_parent_file_id=transform_parent,
            modifications=modifications,
            removed_components=removed,
        ))

    # ------------------------------------------------------------------
    # Pass 7: Extract RenderSettings
    # ------------------------------------------------------------------
    for _rs_fid, rs_body in render_settings_docs:
        result.render_settings.update(rs_body)
        skybox_ref = rs_body.get("m_SkyboxMaterial", {})
        if isinstance(skybox_ref, dict):
            guid = ref_guid(skybox_ref)
            if guid:
                result.skybox_material_guid = guid
                result.referenced_material_guids.add(guid)

    log.info("Parsed %s: %d roots, %d total nodes, %d prefab instances",
             scene_path.name, len(result.roots), len(result.all_nodes),
             len(result.prefab_instances))

    return result
