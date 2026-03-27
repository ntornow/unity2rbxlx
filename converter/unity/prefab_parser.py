"""
prefab_parser.py -- Parses Unity .prefab files into PrefabTemplate objects.

Same multi-pass approach as scene_parser but without PrefabInstance or
RenderSettings handling.  Supports prefab variants: when a .prefab file
contains a PrefabInstance document pointing to a source prefab, we
recursively load the source, then apply the variant's property
modifications on top.
"""

from __future__ import annotations

import copy
import logging
import re
from pathlib import Path
from typing import Any

from core.unity_types import (
    PrefabComponent,
    PrefabNode,
    PrefabTemplate,
    PrefabLibrary,
)
from unity.yaml_parser import (
    CID_GAME_OBJECT,
    CID_TRANSFORM,
    CID_RECT_TRANSFORM,
    CID_MESH_FILTER,
    CID_MESH_RENDERER,
    CID_SKINNED_MESH_RENDERER,
    CID_PREFAB_INSTANCE,
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


def _parse_single_prefab(prefab_path: Path) -> PrefabTemplate:
    """Parse a single .prefab file into a PrefabTemplate."""
    if not is_text_yaml(prefab_path):
        return PrefabTemplate(
            prefab_path=prefab_path,
            name=prefab_path.stem,
        )

    raw_text = prefab_path.read_text(encoding="utf-8", errors="replace")
    triples = parse_documents(raw_text)

    template = PrefabTemplate(
        prefab_path=prefab_path,
        name=prefab_path.stem,
    )
    template.raw_documents = [doc for _, _, doc in triples]

    # Classify documents
    go_docs: dict[str, dict] = {}
    transform_docs: dict[str, dict] = {}
    file_id_to_class: dict[str, int] = {}
    component_docs: list[tuple[str, int, dict]] = []

    for cid, fid, doc in triples:
        body = doc_body(doc)
        file_id_to_class[fid] = cid

        if cid == CID_GAME_OBJECT:
            go_docs[fid] = body
        elif cid in (CID_TRANSFORM, CID_RECT_TRANSFORM):
            transform_docs[fid] = body
        elif cid == CID_PREFAB_INSTANCE:
            # This prefab is a variant -- record the source prefab info
            source_ref = body.get("m_SourcePrefab", {})
            source_guid = ref_guid(source_ref) or ""
            if source_guid:
                template.is_variant = True
                template.source_prefab_guid = source_guid
                modification = body.get("m_Modification", {})
                template.variant_modifications = modification.get("m_Modifications", []) or []
                template.variant_removed_components = modification.get("m_RemovedComponents", []) or []
                template.variant_added_objects = modification.get("m_AddedGameObjects", []) or []
        elif cid in KNOWN_COMPONENT_CIDS:
            component_docs.append((fid, cid, body))

    # Build nodes
    for fid, go in go_docs.items():
        node = PrefabNode(
            name=go.get("m_Name", "GameObject"),
            file_id=fid,
            active=bool(go.get("m_IsActive", 1)),
        )
        template.all_nodes[fid] = node

    # Resolve transforms
    go_fid_to_transform: dict[str, tuple[str, dict]] = {}
    for xform_fid, xform in transform_docs.items():
        go_ref = ref_file_id(xform.get("m_GameObject"))
        if go_ref:
            go_fid_to_transform[go_ref] = (xform_fid, xform)

    xform_fid_to_go_fid: dict[str, str] = {}
    for go_fid, (xform_fid, _) in go_fid_to_transform.items():
        xform_fid_to_go_fid[xform_fid] = go_fid

    for go_fid, node in template.all_nodes.items():
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
            if parent_go_fid and parent_go_fid in template.all_nodes:
                node.parent_file_id = parent_go_fid

        comp_type = ("RectTransform"
                     if file_id_to_class.get(xform_fid) == CID_RECT_TRANSFORM
                     else "Transform")
        node.components.append(PrefabComponent(
            component_type=comp_type,
            file_id=xform_fid,
            properties=xform,
        ))

    # Attach components
    for comp_fid, cid, body in component_docs:
        go_ref = ref_file_id(body.get("m_GameObject"))
        if not go_ref:
            continue
        node = template.all_nodes.get(go_ref)
        if node is None:
            continue

        comp_type = COMPONENT_CID_TO_NAME.get(cid, f"Component_{cid}")
        node.components.append(PrefabComponent(
            component_type=comp_type,
            file_id=comp_fid,
            properties=body,
        ))

        if cid in (CID_MESH_FILTER, CID_SKINNED_MESH_RENDERER):
            mesh_ref = body.get("m_Mesh", {})
            guid = ref_guid(mesh_ref)
            if guid:
                node.mesh_guid = guid
                node.mesh_file_id = str(mesh_ref.get("fileID", ""))
                template.referenced_mesh_guids.add(guid)

        if cid in (CID_MESH_RENDERER, CID_SKINNED_MESH_RENDERER):
            for mat_ref in body.get("m_Materials") or []:
                guid = ref_guid(mat_ref)
                if guid:
                    template.referenced_material_guids.add(guid)

    # Wire hierarchy
    roots: list[PrefabNode] = []
    for node in template.all_nodes.values():
        if node.parent_file_id is None:
            roots.append(node)
        else:
            parent = template.all_nodes.get(node.parent_file_id)
            if parent:
                parent.children.append(node)
            else:
                roots.append(node)

    if len(roots) == 1:
        template.root = roots[0]
    elif len(roots) > 1:
        template.is_multi_root = True
        synthetic = PrefabNode(
            name=template.name,
            file_id="__synthetic_root__",
            active=True,
            children=roots,
        )
        template.root = synthetic
        template.all_nodes["__synthetic_root__"] = synthetic

    return template


def _read_meta_guid(prefab_path: Path) -> str | None:
    """Read the GUID from the .meta file adjacent to a prefab file."""
    meta_path = prefab_path.with_suffix(prefab_path.suffix + ".meta")
    if not meta_path.exists():
        return None
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"^guid:\s*([0-9a-fA-F]+)", text, re.MULTILINE)
        return m.group(1) if m else None
    except Exception:
        return None


def _apply_variant_modifications(
    template: PrefabTemplate,
    modifications: list[dict[str, Any]],
) -> None:
    """Apply property-value modifications from a variant onto a template.

    Each modification dict has:
      - target: {fileID: <id>}  -- which component/transform to modify
      - propertyPath: str       -- dotted path like "m_LocalPosition.x"
      - value: str              -- the new value (for simple types)
      - objectReference: dict   -- for object references (GUIDs etc.)
    """
    if not modifications:
        return

    # Build a lookup from fileID -> PrefabNode for direct node access
    node_by_fid: dict[str, PrefabNode] = dict(template.all_nodes)

    # Also build a lookup from component fileID -> (node, component_index)
    comp_by_fid: dict[str, tuple[PrefabNode, int]] = {}
    for node in template.all_nodes.values():
        for idx, comp in enumerate(node.components):
            comp_by_fid[comp.file_id] = (node, idx)

    for mod in modifications:
        if not isinstance(mod, dict):
            continue
        target = mod.get("target", {})
        if not isinstance(target, dict):
            continue
        target_fid = str(target.get("fileID", ""))
        if not target_fid or target_fid == "0":
            continue

        pp = mod.get("propertyPath", "")
        value = mod.get("value")
        obj_ref = mod.get("objectReference")

        if not pp:
            continue

        # Try to apply to a node directly (e.g., m_Name, m_IsActive on a GameObject)
        node = node_by_fid.get(target_fid)
        if node is not None:
            if pp == "m_Name":
                if value is not None:
                    node.name = str(value)
                continue
            if pp == "m_IsActive":
                try:
                    node.active = bool(int(value))
                except (ValueError, TypeError):
                    pass
                continue

        # Try to apply to a component's properties
        comp_entry = comp_by_fid.get(target_fid)
        if comp_entry is not None:
            comp_node, comp_idx = comp_entry
            comp = comp_node.components[comp_idx]

            # Handle transform property overrides on the owning node
            if comp.component_type in ("Transform", "RectTransform"):
                if pp == "m_LocalPosition.x":
                    _set_tuple_field(comp_node, "position", 0, value)
                    continue
                elif pp == "m_LocalPosition.y":
                    _set_tuple_field(comp_node, "position", 1, value)
                    continue
                elif pp == "m_LocalPosition.z":
                    _set_tuple_field(comp_node, "position", 2, value)
                    continue
                elif pp == "m_LocalRotation.x":
                    _set_tuple_field(comp_node, "rotation", 0, value)
                    continue
                elif pp == "m_LocalRotation.y":
                    _set_tuple_field(comp_node, "rotation", 1, value)
                    continue
                elif pp == "m_LocalRotation.z":
                    _set_tuple_field(comp_node, "rotation", 2, value)
                    continue
                elif pp == "m_LocalRotation.w":
                    _set_tuple_field(comp_node, "rotation", 3, value)
                    continue
                elif pp == "m_LocalScale.x":
                    _set_tuple_field(comp_node, "scale", 0, value)
                    continue
                elif pp == "m_LocalScale.y":
                    _set_tuple_field(comp_node, "scale", 1, value)
                    continue
                elif pp == "m_LocalScale.z":
                    _set_tuple_field(comp_node, "scale", 2, value)
                    continue

            # Apply to component properties dict using dotted path
            _set_nested_property(comp.properties, pp, value, obj_ref)
            continue

        # If we didn't find the target in nodes or components, log at debug level
        log.debug("Variant modification target fileID %s not found for path %s", target_fid, pp)


def _set_tuple_field(node: PrefabNode, attr: str, index: int, value: Any) -> None:
    """Set a single element of a tuple field (position/rotation/scale) on a node."""
    try:
        fval = float(value)
    except (ValueError, TypeError):
        return
    current = list(getattr(node, attr))
    current[index] = fval
    setattr(node, attr, tuple(current))


def _set_nested_property(
    props: dict[str, Any],
    property_path: str,
    value: Any,
    obj_ref: Any = None,
) -> None:
    """Set a value in a nested dict using a Unity propertyPath like 'm_Materials.Array.data[0]'.

    Handles dotted paths and array indexing.  If the intermediate dicts/lists
    don't exist they are created.
    """
    # Unity property paths use dots and Array.data[N] for arrays
    # Examples: "m_LocalPosition.x", "m_Materials.Array.data[0]", "speed"
    parts = property_path.split(".")
    target = props

    for i, part in enumerate(parts[:-1]):
        # Handle Array.data[N] pattern: skip "Array" and "data[N]" parts,
        # index into the list directly
        array_match = re.match(r"data\[(\d+)\]", part)
        if part == "Array":
            continue
        if array_match:
            idx = int(array_match.group(1))
            if isinstance(target, list) and idx < len(target):
                target = target[idx]
            else:
                return  # Can't navigate further
            continue

        if isinstance(target, dict):
            if part not in target:
                target[part] = {}
            target = target[part]
        else:
            return  # Can't navigate further

    # Set the final value
    last = parts[-1]
    array_match = re.match(r"data\[(\d+)\]", last)
    if array_match:
        idx = int(array_match.group(1))
        # The parent should be a list
        if isinstance(target, list) and idx < len(target):
            if obj_ref is not None:
                target[idx] = obj_ref
            elif value is not None:
                target[idx] = value
        return

    if last == "Array":
        return  # Nothing to set for bare "Array"

    if isinstance(target, dict):
        if obj_ref is not None and isinstance(obj_ref, dict) and obj_ref.get("guid"):
            target[last] = obj_ref
        elif value is not None:
            target[last] = value


def _resolve_variant_chain(
    template: PrefabTemplate,
    by_guid: dict[str, PrefabTemplate],
    resolving: set[str] | None = None,
) -> None:
    """Recursively resolve a prefab variant chain.

    If *template* is a variant (has source_prefab_guid), load the source,
    resolve it first (if it's also a variant), deep-copy its node tree,
    and apply this variant's modifications on top.
    """
    if template.variant_resolved:
        return
    if not template.is_variant or not template.source_prefab_guid:
        template.variant_resolved = True
        return

    if resolving is None:
        resolving = set()

    guid = template.source_prefab_guid

    # Cycle detection
    if guid in resolving:
        log.warning("Prefab variant cycle detected involving GUID %s for %s",
                     guid, template.name)
        template.variant_resolved = True
        return

    source = by_guid.get(guid)
    if source is None:
        log.debug("Variant source prefab GUID %s not found for %s",
                  guid, template.name)
        template.variant_resolved = True
        return

    # Resolve the source first (it might be a variant too)
    resolving.add(guid)
    _resolve_variant_chain(source, by_guid, resolving)
    resolving.discard(guid)

    if source.root is None:
        log.debug("Variant source prefab %s has no root -- skipping merge for %s",
                  source.name, template.name)
        template.variant_resolved = True
        return

    # Deep-copy the source's node tree so modifications don't affect the source.
    # We must copy root and all_nodes together so internal references stay linked.
    source_data = {"root": source.root, "all_nodes": source.all_nodes}
    copied = copy.deepcopy(source_data)
    merged_root = copied["root"]
    merged_nodes = copied["all_nodes"]

    # Build a temporary template to hold the merged tree for modification
    merged = PrefabTemplate(
        prefab_path=template.prefab_path,
        name=template.name,
        root=merged_root,
        all_nodes=merged_nodes,
        referenced_material_guids=set(source.referenced_material_guids),
        referenced_mesh_guids=set(source.referenced_mesh_guids),
        is_multi_root=source.is_multi_root,
    )

    # Apply this variant's modifications
    _apply_variant_modifications(merged, template.variant_modifications)

    # Transfer merged result back into the template
    template.root = merged.root
    template.all_nodes = merged.all_nodes
    template.is_multi_root = merged.is_multi_root
    template.referenced_material_guids |= merged.referenced_material_guids
    template.referenced_mesh_guids |= merged.referenced_mesh_guids
    template.variant_resolved = True

    log.debug("Resolved variant %s from source %s (%d modifications applied)",
              template.name, source.name, len(template.variant_modifications))


def parse_prefabs(unity_project_path: str | Path) -> PrefabLibrary:
    """Discover and parse all .prefab files under Assets/."""
    project = Path(unity_project_path)
    assets_dir = project / "Assets"
    if not assets_dir.exists():
        log.warning("Assets/ directory not found in %s", project)
        return PrefabLibrary()

    # Scan both Assets/ and Packages/ for prefab files
    scan_dirs = [assets_dir]
    packages_dir = project / "Packages"
    if packages_dir.exists():
        scan_dirs.append(packages_dir)
    prefab_files = sorted(pf for sd in scan_dirs for pf in sd.rglob("*.prefab"))
    log.info("Found %d prefab files", len(prefab_files))

    library = PrefabLibrary()
    for pf in prefab_files:
        try:
            template = _parse_single_prefab(pf)
            library.prefabs.append(template)
            # Store by name (last wins if collision, but also store by path stem)
            library.by_name[template.name] = template
            # Also store by full relative path stem to avoid collisions
            library.by_name[pf.stem] = template
            library.referenced_material_guids |= template.referenced_material_guids
            library.referenced_mesh_guids |= template.referenced_mesh_guids

            # Index by GUID from the .meta file
            guid = _read_meta_guid(pf)
            if guid:
                library.by_guid[guid] = template
        except Exception as e:
            log.warning("Failed to parse prefab %s: %s", pf.name, e)

    # Resolve prefab variant chains (must happen after all prefabs are indexed)
    variant_count = sum(1 for t in library.prefabs if t.is_variant)
    if variant_count:
        log.info("Resolving %d prefab variant(s)...", variant_count)
        for template in library.prefabs:
            if template.is_variant:
                try:
                    _resolve_variant_chain(template, library.by_guid)
                    # Update library-level material/mesh GUIDs after merge
                    library.referenced_material_guids |= template.referenced_material_guids
                    library.referenced_mesh_guids |= template.referenced_mesh_guids
                except Exception as e:
                    log.warning("Failed to resolve variant %s: %s", template.name, e)

    log.info("Parsed %d prefabs (%d variants resolved)", len(library.prefabs), variant_count)
    return library
