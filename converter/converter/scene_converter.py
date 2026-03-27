"""
scene_converter.py -- Convert a parsed Unity scene hierarchy to a Roblox place.

Walks the SceneNode tree recursively, applying coordinate transforms,
material mappings, component conversion, and mesh asset linking to produce
an RbxPlace ready for serialization.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import config

from core.unity_types import (
    AssetManifest,
    ComponentData,
    GuidIndex,
    ParsedScene,
    SceneNode,
)
from core.roblox_types import (
    RbxCFrame,
    RbxCameraConfig,
    RbxLight,
    RbxLightingConfig,
    RbxPart,
    RbxPlace,
    RbxSkyboxConfig,
    RbxSound,
    RbxTerrain,
    RbxWaterRegion,
)
from core.coordinate_system import (
    unity_to_roblox_pos,
    unity_quat_to_roblox_quat,
    quaternion_to_rotation_matrix,
    unity_scale_to_roblox_size,
)
from converter.component_converter import (
    CINEMACHINE_ALL_TYPES,
    CINEMACHINE_BRAIN_TYPES,
    CINEMACHINE_FREELOOK_TYPES,
    CINEMACHINE_VIRTUAL_CAMERA_TYPES,
    convert_audio,
    convert_camera,
    convert_cinemachine_brain,
    convert_cinemachine_freelook,
    convert_cinemachine_virtual_camera,
    convert_collider,
    convert_joint,
    convert_light,
    convert_line_renderer,
    convert_particle_system,
    convert_post_processing,
    convert_reverb_filter,
    convert_reverb_zone,
    convert_rigidbody,
    convert_skinned_mesh_renderer,
    convert_terrain,
    convert_trail_renderer,
    convert_video_player,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quaternion helpers for composing parent + child transforms
# ---------------------------------------------------------------------------

def _quat_multiply(q1: list | tuple, q2: list | tuple) -> list[float]:
    """Multiply two quaternions (x, y, z, w) -> combined rotation."""
    x1, y1, z1, w1 = q1
    x2, y2, z2, w2 = q2
    return [
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
    ]


def _quat_rotate(q: list | tuple, v: list) -> list[float]:
    """Rotate a 3D vector by a quaternion (x, y, z, w)."""
    qx, qy, qz, qw = q
    vx, vy, vz = v
    # q * v * q_conjugate (optimized)
    t = [
        2.0 * (qy * vz - qz * vy),
        2.0 * (qz * vx - qx * vz),
        2.0 * (qx * vy - qy * vx),
    ]
    return [
        vx + qw * t[0] + (qy * t[2] - qz * t[1]),
        vy + qw * t[1] + (qz * t[0] - qx * t[2]),
        vz + qw * t[2] + (qx * t[1] - qy * t[0]),
    ]


# Component types that indicate a light.
_LIGHT_TYPES = {"Light"}

# Component types that indicate audio.
_AUDIO_TYPES = {"AudioSource"}

# Component types that indicate a collider.
_COLLIDER_TYPES = {"BoxCollider", "SphereCollider", "CapsuleCollider", "MeshCollider",
                   "BoxCollider2D", "CircleCollider2D", "CapsuleCollider2D", "PolygonCollider2D",
                   "EdgeCollider2D"}

# Component types that indicate a camera.
_CAMERA_TYPES = {"Camera"}

# Component types that indicate terrain.
_TERRAIN_TYPES = {"Terrain"}

# Component types that indicate physics joints.
_JOINT_TYPES = {"FixedJoint", "HingeJoint", "SpringJoint", "CharacterJoint", "ConfigurableJoint"}

# Component types that indicate trail/line renderers.
_TRAIL_TYPES = {"TrailRenderer"}
_LINE_RENDERER_TYPES = {"LineRenderer"}

# Component types that indicate post-processing.
_POST_PROCESSING_TYPES = {"Bloom", "BloomOptimized", "ColorGrading", "ColorAdjustments",
                          "DepthOfField", "SunShafts", "Volume"}

# Audio reverb types.
_REVERB_ZONE_TYPES = {"AudioReverbZone"}
_REVERB_FILTER_TYPES = {"AudioReverbFilter"}

# Component types to skip gracefully (detected but no Roblox equivalent).
_SKIP_TYPES = {"ReflectionProbe", "LightProbeGroup", "OcclusionArea", "OcclusionPortal",
               "Cloth", "WindZone", "LensFlare"}

# Types that are already handled elsewhere or have no meaningful Roblox output.
_SILENT_SKIP_TYPES = {
    "Transform", "RectTransform", "CanvasRenderer", "MeshRenderer",
    "MeshFilter", "Renderer",
    "Terrain", "TerrainCollider", "Canvas", "CanvasScaler",
    "Animator", "Animation",
    "AudioListener", "GUILayer", "FlareLayer",
    "NavMeshObstacle",
    "EventSystem", "StandaloneInputModule", "GraphicRaycaster",
    "PlayableDirector",  # Timeline → handled via API mappings in transpiled scripts
}

_NAVMESH_TYPES = {"NavMeshAgent"}

# Character controller
_CHARACTER_CONTROLLER_TYPES = {"CharacterController"}

# LOD Group — we pick the highest detail mesh.
_LOD_TYPES = {"LODGroup"}

# Unity built-in primitive mesh fileIDs -> (Roblox Part.Shape enum, flatten_y).
# Shape enum: 0=Ball, 1=Block, 2=Cylinder, 3=Wedge
# Default assumed native mesh extent (in FBX units).  Most 3D assets are
# authored at real-world scale: a 1m crate is ~100 FBX units (cm).  Unity's
# default FBX import globalScale is 0.01 (1 FBX cm → 0.01 Unity meters),
# meaning a 100-FBX-unit crate becomes 1 Unity unit = 1 Roblox stud.
# However, many asset packs use globalScale=0.1 or 1.0.
# We multiply (Unity_scale × import_scale × _NATIVE_MESH_EXTENT) to get studs.
# Since we can't parse FBX vertex bounds, we assume typical meshes span
# ~100 FBX units (1m at default import scale).
_NATIVE_MESH_EXTENT = 100.0  # approximate FBX-unit span of a typical mesh

# Module-level storage for mesh native sizes (set during convert_scene)
_mesh_native_sizes: dict[str, tuple[float, float, float]] = {}

# Module-level storage for mesh texture IDs (set during convert_scene)
_mesh_texture_ids: dict[str, str] = {}

# Module-level storage for mesh hierarchies (set during convert_scene)
# Maps FBX path -> list of sub-mesh dicts with {name, meshId, size, position, textureId}
_mesh_hierarchies: dict[str, list[dict]] = {}

# Module-level collector for water regions discovered during node conversion.
# Populated by _convert_node / _convert_prefab_instance, consumed by convert_scene.
_water_regions: list[RbxWaterRegion] = []


def _read_fbx_unit_scale_factors(fbx_path: Path) -> tuple[float | None, float | None]:
    """Read UnitScaleFactor and OriginalUnitScaleFactor from an FBX binary.

    Returns (usf, original_usf). Either may be None if not found.
    """
    import struct
    try:
        data = fbx_path.read_bytes()
    except OSError:
        return None, None

    def _find_double_after(name: bytes) -> float | None:
        idx = data.find(name)
        if idx < 0:
            return None
        search_start = idx + len(name)
        d_idx = data.find(b'D', search_start, search_start + 60)
        if d_idx < 0:
            return None
        try:
            return struct.unpack_from('<d', data, d_idx + 1)[0]
        except struct.error:
            return None

    usf = _find_double_after(b'UnitScaleFactor')
    original_usf = _find_double_after(b'OriginalUnitScaleFactor')
    return usf, original_usf


def _get_fbx_import_scale(
    mesh_guid: str,
    guid_index: GuidIndex | None,
) -> float:
    """Read the effective import scale from a mesh asset's .meta file.

    Unity FBX import has two modes:
    1. useFileScale=0: globalScale is the direct scale factor
    2. useFileScale=1: globalScale includes fileScale (UnitScaleFactor/100)

    The returned value converts FBX vertex units to Unity meters:
      visual_meters = fbx_vertex × returned_scale
    """
    if guid_index is None:
        return 0.01

    asset_path = guid_index.resolve(mesh_guid)
    if asset_path is None:
        return 0.01

    meta_path = Path(str(asset_path) + ".meta")
    if not meta_path.exists():
        return 0.01

    try:
        import re
        text = meta_path.read_text(encoding="utf-8", errors="replace")

        global_scale = 0.01
        use_file_scale = False

        m = re.search(r"globalScale:\s*([0-9.eE+-]+)", text)
        if m:
            global_scale = float(m.group(1))

        m = re.search(r"useFileScale:\s*([01])", text)
        if m:
            use_file_scale = m.group(1) == "1"

        if use_file_scale and abs(global_scale - 1.0) < 0.001:
            usf, _ = _read_fbx_unit_scale_factors(Path(str(asset_path)))
            if usf and usf > 0:
                return usf / 100.0

        return global_scale
    except (OSError, ValueError):
        return 0.01


def _get_fbx_unit_ratio(
    mesh_guid: str,
    guid_index: GuidIndex | None,
) -> float:
    """Get the FBX internal unit conversion ratio.

    When an FBX file was converted from one unit system to another
    (e.g., cm to meters), the vertex data stays in the original units
    but UnitScaleFactor is updated. The ratio USF/OriginalUSF tells us
    how much Roblox's raw vertex sizes need to be scaled to match
    the declared unit system.

    This only applies when Unity's .meta has useFileScale=1 and
    globalScale=1 (meaning Unity auto-computed the scale from USF).
    When globalScale is explicitly set (e.g., 100), it already
    accounts for the unit conversion.

    Returns 1.0 if no conversion needed or data is unavailable.
    """
    if not guid_index:
        return 1.0
    asset_path = guid_index.resolve(mesh_guid)
    if not asset_path:
        return 1.0

    # Only apply unit ratio when globalScale=1 and useFileScale=1
    # (the import_scale function handles the meta reading)
    import re
    meta_path = Path(str(asset_path) + ".meta")
    if not meta_path.exists():
        return 1.0
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"globalScale:\s*([0-9.eE+-]+)", text)
        global_scale = float(m.group(1)) if m else 0.01
        m = re.search(r"useFileScale:\s*([01])", text)
        use_file_scale = m.group(1) == "1" if m else False

        # Only apply when Unity used auto file scale (globalScale=1, useFileScale=1)
        if not (use_file_scale and abs(global_scale - 1.0) < 0.001):
            return 1.0
    except (OSError, ValueError):
        return 1.0

    usf, original_usf = _read_fbx_unit_scale_factors(Path(str(asset_path)))
    if usf and original_usf and original_usf > 0:
        ratio = usf / original_usf
        if abs(ratio - 1.0) > 0.001:
            return ratio
    return 1.0


def _compute_mesh_size(
    unity_scale: tuple[float, float, float],
    mesh_guid: str,
    guid_index: GuidIndex,
    mesh_native_sizes: dict[str, tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """Compute Roblox MeshPart Size and InitialSize from Unity + Roblox data.

    Requires native sizes from Roblox LoadAsset (populated by the resolve step).

    InitialSize = native mesh bounding box from Roblox
    Size = InitialSize × import_scale × unity_scale × STUDS_PER_METER
    Roblox renders at Size/InitialSize = import_scale × unity_scale × STUDS_PER_METER

    Returns (size, initial_size) or None if the mesh is not in native_sizes.
    """
    asset_path = guid_index.resolve(mesh_guid)
    if not asset_path:
        return None

    relative = guid_index.resolve_relative(mesh_guid)
    for key in [str(relative), str(asset_path)] if relative else [str(asset_path)]:
        if key in mesh_native_sizes:
            native = mesh_native_sizes[key]
            import_scale = _get_fbx_import_scale(mesh_guid, guid_index)
            unit_ratio = _get_fbx_unit_ratio(mesh_guid, guid_index)
            initial_size = (native[0], native[1], native[2])
            # unit_ratio compensates for FBX internal node transforms
            # that Roblox doesn't apply (e.g., cm→m conversion baked
            # into Lcl Scaling nodes)
            scale_factor = import_scale * unit_ratio * config.STUDS_PER_METER
            size = (
                abs(unity_scale[0]) * initial_size[0] * scale_factor,
                abs(unity_scale[1]) * initial_size[1] * scale_factor,
                abs(unity_scale[2]) * initial_size[2] * scale_factor,
            )
            return size, initial_size

    return None

_UNITY_BUILTIN_MESH_SHAPES: dict[str, tuple[int, bool]] = {
    "10202": (1, False),    # Cube -> Block
    "10206": (2, False),    # Cylinder -> Cylinder
    "10207": (0, False),    # Sphere -> Ball
    "10208": (2, False),    # Capsule -> Cylinder (approximation)
    "10209": (1, True),     # Plane -> Block (flattened)
    "10210": (1, True),     # Quad -> Block (flattened)
}


def convert_scene(
    parsed_scene: ParsedScene,
    guid_index: GuidIndex | None = None,
    asset_manifest: AssetManifest | None = None,
    material_mappings: dict[str, Any] | None = None,
    uploaded_assets: dict[str, str] | None = None,
    mesh_native_sizes: dict[str, tuple[float, float, float]] | None = None,
    mesh_texture_ids: dict[str, str] | None = None,
    mesh_hierarchies: dict[str, list[dict]] | None = None,
) -> RbxPlace:
    """Convert a parsed Unity scene to a Roblox place hierarchy.

    Args:
        parsed_scene: The parsed Unity scene with SceneNode tree.
        guid_index: GUID -> path resolver for mesh/material assets.
        asset_manifest: Full asset manifest for the Unity project.
        material_mappings: Material GUID -> MaterialMapping dict.
        uploaded_assets: Local path -> rbxassetid URL dict for uploaded meshes.
        mesh_native_sizes: FBX path -> (x, y, z) native mesh size in Roblox studs.
        mesh_texture_ids: FBX path -> rbxassetid:// URL for embedded mesh textures.

    Returns:
        An RbxPlace with workspace parts, lighting, camera, and scripts.
    """
    material_mappings = material_mappings or {}
    uploaded_assets = uploaded_assets or {}

    place = RbxPlace()

    # Reset module-level water region collector for this conversion run
    global _water_regions
    _water_regions = []

    # Set module-level mesh native sizes for use by _compute_mesh_size
    global _mesh_native_sizes
    _mesh_native_sizes = mesh_native_sizes or {}
    if not _mesh_native_sizes:
        log.warning("No mesh native sizes available — mesh sizing will use fallback estimates. "
                    "Run Studio asset resolution for accurate sizing.")

    # Set module-level mesh texture IDs for use by _resolve_mesh_texture_id
    global _mesh_texture_ids
    _mesh_texture_ids = mesh_texture_ids or {}

    # Set module-level mesh hierarchies for sub-mesh resolution
    global _mesh_hierarchies
    _mesh_hierarchies = mesh_hierarchies or {}

    # Identify Canvas nodes so we can exclude them from workspace conversion
    # (they're handled separately by the UI translator).
    from converter.ui_translator import find_canvas_nodes as _find_canvas
    canvas_node_ids = {id(n) for n in _find_canvas(parsed_scene.roots)}

    # Identify Terrain nodes so we can exclude them
    # (they're handled by _collect_terrains below).
    terrain_node_ids: set[int] = set()
    for node in parsed_scene.all_nodes.values():
        for comp in node.components:
            if comp.component_type in _TERRAIN_TYPES:
                terrain_node_ids.add(id(node))

    # Build a mapping from Transform file_id → scene node, so we can parent
    # prefab instances under their correct hierarchy container.
    transform_to_node: dict[str, Any] = {}  # Transform file_id → SceneNode
    def _index_transforms(node):
        for comp in node.components:
            if comp.component_type in ("Transform", "RectTransform"):
                transform_to_node[comp.file_id] = node
        for child in node.children:
            _index_transforms(child)
    for root in parsed_scene.roots:
        _index_transforms(root)

    # Convert the scene hierarchy (direct nodes), skipping Canvas UI and Terrain nodes.
    # Also build a mapping from SceneNode → RbxPart for prefab parenting.
    node_to_rbx: dict[int, Any] = {}  # id(SceneNode) → RbxPart

    def _convert_and_index(node, guid_index, material_mappings, uploaded_assets, scene_nodes=None):
        """Convert a node and recursively index all converted parts."""
        rbx_part = _convert_node(
            node=node,
            guid_index=guid_index,
            material_mappings=material_mappings,
            uploaded_assets=uploaded_assets,
            scene_nodes=scene_nodes,
        )
        if rbx_part is not None:
            node_to_rbx[id(node)] = rbx_part
            # Also index children recursively (they're already in rbx_part.children)
            _index_children_recursive(node, rbx_part)
        return rbx_part

    def _index_children_recursive(node, rbx_part):
        """Match scene node children to their RbxPart equivalents.

        Uses name matching with fallback to positional matching for
        nodes with duplicate names.
        """
        # Build name→rbx mapping, handling duplicates by position
        rbx_children = list(rbx_part.children)
        used = set()
        for child_node in node.children:
            # Try exact name match first (pick first unused match)
            matched = None
            for i, rc in enumerate(rbx_children):
                if i not in used and rc.name == child_node.name:
                    matched = (i, rc)
                    break
            if matched:
                used.add(matched[0])
                node_to_rbx[id(child_node)] = matched[1]
                _index_children_recursive(child_node, matched[1])

    for root_node in parsed_scene.roots:
        if id(root_node) in canvas_node_ids:
            continue  # Handled by UI translator below
        if id(root_node) in terrain_node_ids:
            continue  # Handled by _collect_terrains below
        try:
            rbx_part = _convert_and_index(
                root_node, guid_index, material_mappings, uploaded_assets,
                scene_nodes=parsed_scene.all_nodes,
            )
            if rbx_part is not None:
                place.workspace_parts.append(rbx_part)
        except Exception as exc:
            log.warning("Failed to convert scene node '%s': %s", root_node.name, exc)

    # Build transform_file_id → RbxPart mapping for prefab parenting.
    # Includes lazily-created container Models for inactive/skipped scene nodes
    # so that prefab instances parented under them still get proper hierarchy.
    transform_to_rbx: dict[str, Any] = {}
    for tfm_id, scene_node in transform_to_node.items():
        rbx = node_to_rbx.get(id(scene_node))
        if rbx is not None:
            transform_to_rbx[tfm_id] = rbx

    # Create container Models for inactive/skipped scene nodes that might have
    # prefab instance children.  Walk the scene hierarchy so containers are
    # nested under their own (possibly also-inactive) parents.
    # Build GO file_id → Transform file_id mapping for parent chain resolution.
    go_fid_to_tfm_fid: dict[str, str] = {}
    for node in parsed_scene.all_nodes.values():
        for comp in node.components:
            if comp.component_type in ("Transform", "RectTransform"):
                go_fid_to_tfm_fid[node.file_id] = comp.file_id
                break

    _inactive_containers: dict[str, RbxPart] = {}  # tfm_id → lazy container

    def _ensure_inactive_container(tfm_id: str) -> RbxPart | None:
        """Return (or create) a container Model for an unconverted scene node."""
        if tfm_id in transform_to_rbx:
            return transform_to_rbx[tfm_id]
        if tfm_id in _inactive_containers:
            return _inactive_containers[tfm_id]
        scene_node = transform_to_node.get(tfm_id)
        if scene_node is None:
            return None
        container = RbxPart(name=scene_node.name, class_name="Model")
        _inactive_containers[tfm_id] = container
        transform_to_rbx[tfm_id] = container
        # Try to parent this container under its own parent scene node
        parent_go_fid = scene_node.parent_file_id
        parent_tfm_fid = go_fid_to_tfm_fid.get(parent_go_fid, "") if parent_go_fid else ""
        if parent_tfm_fid:
            parent_container = _ensure_inactive_container(parent_tfm_fid)
            if parent_container is not None:
                parent_container.children.append(container)
            else:
                place.workspace_parts.append(container)
        else:
            place.workspace_parts.append(container)
        return container

    # Resolve prefab instances into parts, parenting under correct hierarchy.
    # Two-pass approach: first pass parents prefabs under scene nodes (creating
    # lazy containers for inactive parents), second pass parents orphans under
    # previously-converted prefab instances via stripped Transform offset.
    prefab_lib = None
    if parsed_scene.prefab_instances and guid_index:
        prefab_lib = _load_prefab_library(guid_index)
    if prefab_lib and parsed_scene.prefab_instances:
        failed_prefabs = 0
        parented = 0

        # Track converted prefab instances by their file_id for nested parenting.
        # Also register the stripped Transform ID (file_id+1) so child PIs can
        # find their parent directly without relying on offset arithmetic.
        pi_file_id_to_rbx: dict[str, Any] = {}
        orphaned_pis: list[tuple[Any, list]] = []  # (pi, pi_parts) pairs

        for pi in parsed_scene.prefab_instances:
            try:
                pi_parts = _convert_prefab_instance(
                    pi, prefab_lib, guid_index, material_mappings, uploaded_assets,
                )
                if pi_parts:
                    # Register this prefab instance's file_id for nested parenting
                    pi_file_id_to_rbx[pi.file_id] = pi_parts[0]
                    # Also register the stripped Transform ID (PI file_id + 1)
                    if pi.file_id.isdigit():
                        stripped_tfm_id = str(int(pi.file_id) + 1)
                        pi_file_id_to_rbx[stripped_tfm_id] = pi_parts[0]

                    # Try to parent under a scene node (or lazy inactive container)
                    tp = pi.transform_parent_file_id
                    if not tp or tp == "0":
                        # Root-level prefab instance — add directly to workspace
                        place.workspace_parts.extend(pi_parts)
                        parented += len(pi_parts)
                    else:
                        parent_rbx = transform_to_rbx.get(tp)
                        if parent_rbx is None:
                            # Check if parent is an unconverted scene node
                            parent_rbx = _ensure_inactive_container(tp)
                        if parent_rbx is not None:
                            parent_rbx.children.extend(pi_parts)
                            parented += len(pi_parts)
                        else:
                            orphaned_pis.append((pi, pi_parts))
            except Exception as exc:
                failed_prefabs += 1
                if failed_prefabs <= 5:
                    log.warning("Failed to convert prefab instance: %s", exc)

        # Multi-pass resolution: try to parent orphaned prefab instances under
        # other prefab instances via their stripped Transform IDs.
        # We need multiple passes because parent chains can be multiple levels deep.
        still_orphaned_pis = orphaned_pis
        max_passes = 5
        for pass_num in range(max_passes):
            if not still_orphaned_pis:
                break
            next_orphans = []
            resolved_this_pass = 0
            for pi, pi_parts in still_orphaned_pis:
                parent_id = pi.transform_parent_file_id
                parent_rbx = pi_file_id_to_rbx.get(parent_id)

                if parent_rbx is not None:
                    parent_rbx.children.extend(pi_parts)
                    parented += len(pi_parts)
                    resolved_this_pass += len(pi_parts)
                else:
                    next_orphans.append((pi, pi_parts))

            still_orphaned_pis = next_orphans
            if resolved_this_pass == 0:
                break  # No progress, stop

        still_orphaned = 0
        for pi, pi_parts in still_orphaned_pis:
            place.workspace_parts.extend(pi_parts)
            still_orphaned += len(pi_parts)

        if failed_prefabs:
            log.warning("Total failed prefab instances: %d", failed_prefabs)
        if parented:
            log.info("Parented %d prefab parts under scene hierarchy", parented)
        if still_orphaned:
            log.info("Orphaned %d prefab parts (parent not found, added to workspace root)", still_orphaned)

    # Extract lighting from render_settings.
    place.lighting = _extract_lighting(parsed_scene.render_settings)

    # Apply directional light rotation → ClockTime and brightness.
    _apply_directional_light(parsed_scene, place.lighting)

    # Extract skybox from the scene's skybox material.
    if parsed_scene.skybox_material_guid and guid_index:
        skybox = _extract_skybox(
            parsed_scene.skybox_material_guid,
            guid_index,
            uploaded_assets,
        )
        if skybox is not None:
            place.skybox = skybox

    # Extract camera from the scene (find first Camera component).
    camera_config = _find_camera(parsed_scene)
    if camera_config is not None:
        place.camera = camera_config

    # Convert Unity Canvas UI elements to Roblox ScreenGuis.
    from converter.ui_translator import find_canvas_nodes, convert_canvas
    canvas_nodes = find_canvas_nodes(parsed_scene.roots)
    if canvas_nodes:
        place.screen_guis = convert_canvas(canvas_nodes)
        log.info("Converted %d Canvas nodes to ScreenGuis", len(place.screen_guis))

    # Detect terrain components and convert them to terrain ground parts.
    _collect_terrains(parsed_scene, place)

    # Collect water regions discovered during node/prefab conversion.
    if _water_regions:
        place.water_regions = list(_water_regions)
        log.info("Collected %d water regions for terrain water fill", len(place.water_regions))

    # Collect post-processing components from the scene.
    _collect_post_processing(parsed_scene, place)

    # Auto-generate floor and SpawnLocation based on scene bounds.
    # If terrain was found, skip the auto-generated floor (terrain provides it).
    _add_floor_and_spawn(place)

    # Flatten single-child Models: if a Model has exactly one child
    # Part/MeshPart (no scripts, sounds, lights etc.), promote the child
    # to replace the Model, keeping the Model's name.
    flattened = _flatten_single_child_models(place.workspace_parts)
    if flattened:
        log.info("Flattened %d single-child Models", flattened)

    log.info(
        "Scene converted: %d top-level parts, %d screen_guis, %d terrains, %d water regions, lighting configured, camera %s",
        len(place.workspace_parts),
        len(place.screen_guis),
        len(place.terrains),
        len(place.water_regions),
        "found" if place.camera else "default",
    )
    return place


# ---------------------------------------------------------------------------
# Node conversion
# ---------------------------------------------------------------------------

def _convert_node(
    node: SceneNode,
    guid_index: GuidIndex | None,
    material_mappings: dict[str, Any],
    uploaded_assets: dict[str, str],
    depth: int = 0,
    scene_nodes: dict[str, SceneNode] | None = None,
) -> RbxPart | None:
    """Recursively convert a SceneNode to an RbxPart.

    Returns None for nodes that should be skipped (inactive, editor-only).
    """
    if not node.active:
        return None

    # Skip terrain nodes (handled separately by _collect_terrains).
    for comp in node.components:
        if comp.component_type in _TERRAIN_TYPES:
            return None

    # Skip editor-only nodes.
    if node.name.startswith("__"):
        return None

    # Collect water nodes as terrain water regions instead of creating Parts.
    if _is_water_node(node, material_mappings, guid_index):
        region = _extract_water_region(node)
        _water_regions.append(region)
        log.info("Detected water plane '%s' at (%.1f, %.1f, %.1f), size (%.1f, %.1f, %.1f)",
                 node.name, *region.position, *region.size)
        return None

    # Limit recursion depth for safety.
    if depth > 64:
        log.warning("Max recursion depth reached at node '%s'", node.name)
        return None

    # -- Position --
    rx, ry, rz = unity_to_roblox_pos(*node.position)

    # -- Rotation --
    rqx, rqy, rqz, rqw = unity_quat_to_roblox_quat(*node.rotation)
    rot = quaternion_to_rotation_matrix(rqx, rqy, rqz, rqw)

    # -- Size --
    size = unity_scale_to_roblox_size(node.scale)

    # -- CFrame --
    cframe = RbxCFrame(
        x=rx, y=ry, z=rz,
        r00=rot[0], r01=rot[1], r02=rot[2],
        r10=rot[3], r11=rot[4], r12=rot[5],
        r20=rot[6], r21=rot[7], r22=rot[8],
    )

    # -- Unity built-in primitive mesh mapping --
    # Unity built-in meshes have specific fileIDs; map them to Roblox Part shapes.
    _builtin_shape = _UNITY_BUILTIN_MESH_SHAPES.get(node.mesh_file_id or "")

    # Determine class name based on mesh presence.
    has_mesh = node.mesh_guid is not None
    if _builtin_shape:
        class_name = "Part"
    elif has_mesh:
        class_name = "MeshPart"
    else:
        class_name = "Part"

    # If the node has children but no mesh, use Model as the container.
    if node.children and not has_mesh and not _builtin_shape:
        class_name = "Model"

    part = RbxPart(
        name=node.name,
        class_name=class_name,
        cframe=cframe,
        size=size,
        unity_file_id=node.file_id,
    )

    # -- Primitive shape --
    if _builtin_shape:
        shape_enum, flatten = _builtin_shape
        part.shape = shape_enum
        if flatten:
            part.size = (size[0], 0.2, size[2])
    # -- Mesh asset --
    elif has_mesh and node.mesh_guid:
        mesh_id = _resolve_mesh_id(node.mesh_guid, guid_index, uploaded_assets,
                                    mesh_file_id=node.mesh_file_id)
        if mesh_id:
            part.mesh_id = mesh_id
        # Compute mesh size from native Roblox data (requires upload + resolve)
        sized = False
        if _mesh_native_sizes and guid_index:
            result = _compute_mesh_size(node.scale, node.mesh_guid, guid_index, _mesh_native_sizes)
            if result:
                part.size, part.initial_size = result
                sized = True
        # Fallback: use FBX import scale × unity scale when native sizes unavailable.
        # This gives a reasonable estimate even without the resolve step.
        if not sized and guid_index and node.mesh_guid:
            import_scale = _get_fbx_import_scale(node.mesh_guid, guid_index)
            unit_ratio = _get_fbx_unit_ratio(node.mesh_guid, guid_index)
            scale_factor = import_scale * unit_ratio * config.STUDS_PER_METER
            sx, sy, sz = node.scale
            # Use unity localScale as the base dimension (in meters), then convert
            part.size = (
                abs(sx) * scale_factor,
                abs(sy) * scale_factor,
                abs(sz) * scale_factor,
            )
        # Set TextureID from embedded FBX texture if available
        tex_id = _resolve_mesh_texture_id(node.mesh_guid, guid_index)
        if tex_id:
            part.texture_id = tex_id

    # -- Material --
    _apply_materials(node, part, material_mappings)

    # -- Components --
    _process_components(node, part, guid_index=guid_index, uploaded_assets=uploaded_assets, scene_nodes=scene_nodes)

    # -- Logic-only nodes: make invisible if no mesh and no visual components --
    has_visual = (
        part.mesh_id is not None or
        part.class_name == "MeshPart" or
        part.surface_appearance is not None or
        any(comp.component_type in ("MeshRenderer", "SkinnedMeshRenderer", "SpriteRenderer")
            for comp in node.components)
    )
    if not has_visual and part.class_name == "Part" and not part.children:
        part.transparency = 1.0
        part.can_collide = False

    # -- Tag as attribute --
    if node.tag and node.tag != "Untagged":
        part.attributes["Tag"] = node.tag

    # -- Layer as attribute (for CollisionGroup mapping) --
    if hasattr(node, "layer") and node.layer and node.layer != 0:
        part.attributes["UnityLayer"] = node.layer

    # -- Animator component -> attributes for animation script targeting --
    for comp in node.components:
        if comp.component_type == "Animator":
            part.attributes["HasAnimator"] = True
            # Extract controller GUID for animation script matching
            controller_ref = comp.properties.get("m_Controller", {})
            if isinstance(controller_ref, dict):
                ctrl_guid = controller_ref.get("guid", "")
                if ctrl_guid and ctrl_guid != "0" * 32:
                    part.attributes["AnimatorController"] = ctrl_guid
            # Extract culling mode
            cull_mode = comp.properties.get("m_CullingMode", 0)
            if int(cull_mode) == 2:  # CullCompletely
                part.attributes["AnimCullWhenOffscreen"] = True
            break

    # -- Children --
    # If this node has a LODGroup, skip lower LOD children (keep LOD0 only).
    has_lod_group = part.attributes.get("_HasLODGroup", False)
    for child_node in node.children:
        if has_lod_group:
            child_name = child_node.name.lower()
            # Skip LOD1, LOD2, LOD_1, LOD_2, etc. — keep LOD0 or non-LOD children.
            if re.match(r"lod[_\s]?[1-9]", child_name):
                continue
        child_part = _convert_node(
            node=child_node,
            guid_index=guid_index,
            material_mappings=material_mappings,
            uploaded_assets=uploaded_assets,
            depth=depth + 1,
            scene_nodes=scene_nodes,
        )
        if child_part is not None:
            part.children.append(child_part)

    # Clean up internal attributes
    part.attributes.pop("_HasLODGroup", None)

    return part


# ---------------------------------------------------------------------------
# Component processing
# ---------------------------------------------------------------------------

def _process_components(
    node: SceneNode,
    part: RbxPart,
    guid_index: GuidIndex | None = None,
    uploaded_assets: dict[str, str] | None = None,
    scene_nodes: dict[str, SceneNode] | None = None,
) -> None:
    """Process all components on a SceneNode and attach results to the RbxPart."""
    has_rigidbody = False
    rigidbody_props: dict[str, Any] = {}
    original_size = part.size  # Save for collider calculations

    for comp in node.components:
        ct = comp.component_type

        # -- Lights --
        if ct in _LIGHT_TYPES:
            light = convert_light(comp.properties)
            if light is not None:
                part.lights.append(light)

        # -- Audio --
        elif ct in _AUDIO_TYPES:
            sound = convert_audio(
                comp.properties,
                guid_index=guid_index,
                uploaded_assets=uploaded_assets,
            )
            if sound is not None:
                part.sounds.append(sound)

        # -- Colliders --
        elif ct in _COLLIDER_TYPES:
            is_trigger = bool(int(comp.properties.get("m_IsTrigger", 0)))
            if is_trigger:
                # Trigger colliders are detection zones — don't expand part size
                part.can_collide = False
            else:
                # Apply each collider against the original size to avoid
                # compounding when multiple colliders exist on one node.
                adjusted_size, can_collide = convert_collider(
                    ct, comp.properties, original_size,
                )
                # Keep the largest result across all physical colliders
                part.size = (
                    max(part.size[0], adjusted_size[0]),
                    max(part.size[1], adjusted_size[1]),
                    max(part.size[2], adjusted_size[2]),
                )
                part.can_collide = can_collide

        # -- Rigidbody / Rigidbody2D --
        elif ct in ("Rigidbody", "Rigidbody2D"):
            has_rigidbody = True
            rigidbody_props = comp.properties

        # -- Camera --
        elif ct in _CAMERA_TYPES:
            # Camera components are handled at scene level.
            pass

        # -- ParticleSystem --
        elif ct == "ParticleSystem":
            particle = convert_particle_system(comp.properties)
            if particle is not None:
                part.particle_emitters.append(particle)

        # -- MonoBehaviour: extract serialized numeric/string fields as attributes --
        elif ct == "MonoBehaviour":
            _extract_monobehaviour_attributes(comp.properties, part, guid_index)

        # -- Physics Joints --
        elif ct in _JOINT_TYPES:
            constraint = convert_joint(ct, comp.properties)
            if constraint is not None:
                part.constraints.append(constraint)

        # -- TrailRenderer --
        elif ct in _TRAIL_TYPES:
            trail = convert_trail_renderer(comp.properties)
            if trail is not None:
                part.trails.append(trail)

        # -- LineRenderer --
        elif ct in _LINE_RENDERER_TYPES:
            beam = convert_line_renderer(comp.properties)
            if beam is not None:
                part.beams.append(beam)

        # -- AudioReverbZone --
        elif ct in _REVERB_ZONE_TYPES:
            reverb = convert_reverb_zone(comp.properties)
            if reverb is not None:
                part.reverb_effects.append(reverb)

        # -- AudioReverbFilter --
        elif ct in _REVERB_FILTER_TYPES:
            reverb = convert_reverb_filter(comp.properties)
            if reverb is not None:
                part.reverb_effects.append(reverb)

        # -- VideoPlayer --
        elif ct == "VideoPlayer":
            video_frame = convert_video_player(
                comp.properties,
                guid_index=guid_index,
                uploaded_assets=uploaded_assets,
            )
            if video_frame is not None:
                part.video_frames.append(video_frame)

        # -- Components with no Roblox equivalent (skip gracefully) --
        elif ct in _SKIP_TYPES:
            pass  # Intentionally skipped

        # -- LODGroup: mark for LOD child filtering --
        elif ct in _LOD_TYPES:
            # Flag this node so child conversion skips lower LOD levels
            part.attributes["_HasLODGroup"] = True

        # -- CharacterController: mark as having character controller --
        elif ct in _CHARACTER_CONTROLLER_TYPES:
            # Store character controller properties as attributes
            import config
            height = float(comp.properties.get("m_Height", 2.0))
            radius = float(comp.properties.get("m_Radius", 0.5))
            part.attributes["_HasCharacterController"] = True
            part.attributes["_WalkSpeed"] = 16.0  # Default Roblox walk speed
            part.attributes["_JumpHeight"] = float(comp.properties.get("m_StepOffset", 0.3)) * config.STUDS_PER_METER
            # Adjust part size to capsule dimensions
            diameter = radius * 2 * config.STUDS_PER_METER
            part.size = (diameter, height * config.STUDS_PER_METER, diameter)
            part.anchored = False  # Character controllers are physics-driven

        # -- SpriteRenderer: convert to thin Part with color --
        elif ct == "SpriteRenderer":
            # Extract sprite color for the part
            sprite_color = comp.properties.get("m_Color", {})
            if isinstance(sprite_color, dict):
                r = float(sprite_color.get("r", 1.0))
                g = float(sprite_color.get("g", 1.0))
                b = float(sprite_color.get("b", 1.0))
                a = float(sprite_color.get("a", 1.0))
                part.color = (r, g, b)
                if a < 1.0:
                    part.transparency = 1.0 - a
            # Sprites are flat — make the part thin
            part.size = (part.size[0], 0.1, part.size[2])
            # Extract sprite GUID for potential texture upload
            sprite_ref = comp.properties.get("m_Sprite", {})
            if isinstance(sprite_ref, dict):
                sprite_guid = sprite_ref.get("guid", "")
                if sprite_guid and sprite_guid != "0" * 32:
                    part.attributes["_SpriteGuid"] = sprite_guid

        # -- Cinemachine: store camera config as attributes --
        elif ct in CINEMACHINE_VIRTUAL_CAMERA_TYPES:
            attrs = convert_cinemachine_virtual_camera(comp.properties)
            part.attributes.update(attrs)

        elif ct in CINEMACHINE_FREELOOK_TYPES:
            attrs = convert_cinemachine_freelook(comp.properties)
            part.attributes.update(attrs)

        elif ct in CINEMACHINE_BRAIN_TYPES:
            attrs = convert_cinemachine_brain(comp.properties)
            part.attributes.update(attrs)

        # -- NavMeshAgent: store pathfinding attributes --
        elif ct in _NAVMESH_TYPES:
            import config
            part.attributes["_HasNavMeshAgent"] = True
            speed = float(comp.properties.get("m_Speed", 3.5))
            part.attributes["_WalkSpeed"] = speed * config.STUDS_PER_METER
            stopping_dist = float(comp.properties.get("m_StoppingDistance", 0.0))
            part.attributes["_StoppingDistance"] = stopping_dist * config.STUDS_PER_METER
            part.anchored = False  # NavMesh agents need physics

        # -- SkinnedMeshRenderer: extract bone hierarchy as Motor6D joints --
        elif ct == "SkinnedMeshRenderer":
            motor6ds, bone_attrs = convert_skinned_mesh_renderer(
                comp.properties,
                scene_nodes=scene_nodes,
            )
            if motor6ds:
                part.motor6ds.extend(motor6ds)
            if bone_attrs:
                part.attributes.update(bone_attrs)

        # -- Types handled elsewhere or with no Roblox equivalent --
        elif ct in _SILENT_SKIP_TYPES:
            pass

        # -- Unknown component: log for debugging --
        else:
            log.debug("Unhandled component type '%s' on node '%s'", ct, node.name)

    # -- Anchoring logic --
    # No rigidbody -> anchored (static geometry).
    # Rigidbody with isKinematic -> anchored (script-driven movement).
    # Rigidbody without isKinematic -> not anchored (physics-driven).
    if has_rigidbody:
        anchored, can_collide = convert_rigidbody(rigidbody_props)
        part.anchored = anchored
        # Only override can_collide if rigidbody says so.
        if not can_collide:
            part.can_collide = can_collide
    else:
        part.anchored = True


# ---------------------------------------------------------------------------
# Mesh resolution
# ---------------------------------------------------------------------------

def _resolve_sub_mesh(
    mesh_guid: str,
    mesh_file_id: str | None,
    guid_index: GuidIndex | None,
) -> dict | None:
    """Resolve a specific sub-mesh within a multi-mesh FBX using mesh_hierarchies.

    Unity FBX fileIDs map to sub-mesh indices: 4300000 → index 0, 4300002 → index 1, etc.
    Returns the sub-mesh dict or None if not available.
    """
    if not _mesh_hierarchies or not guid_index or not mesh_file_id:
        return None

    asset_path = guid_index.resolve(mesh_guid)
    if not asset_path:
        return None

    relative = guid_index.resolve_relative(mesh_guid)
    for key in ([str(relative), str(asset_path)] if relative else [str(asset_path)]):
        if key in _mesh_hierarchies:
            sub_meshes = _mesh_hierarchies[key]
            if not sub_meshes:
                return None
            # Convert fileID to sub-mesh index
            # Unity FBX mesh fileIDs: 4300000, 4300002, 4300004, ...
            try:
                fid = int(mesh_file_id)
                if fid >= 4300000:
                    idx = (fid - 4300000) // 2
                    if 0 <= idx < len(sub_meshes):
                        return sub_meshes[idx]
            except (ValueError, TypeError):
                pass
            # Fallback: return first sub-mesh
            return sub_meshes[0]

    return None


def _resolve_mesh_id(
    mesh_guid: str,
    guid_index: GuidIndex | None,
    uploaded_assets: dict[str, str],
    mesh_file_id: str | None = None,
) -> str | None:
    """Resolve a mesh GUID to an rbxassetid URL.

    When mesh_hierarchies data is available and mesh_file_id is provided,
    resolves to the specific sub-mesh within a multi-mesh FBX.
    Otherwise falls back to uploaded_assets lookup.
    """
    # Try sub-mesh resolution first
    sub_mesh = _resolve_sub_mesh(mesh_guid, mesh_file_id, guid_index)
    if sub_mesh and sub_mesh.get("meshId"):
        return sub_mesh["meshId"]
    if guid_index is None:
        return None

    asset_path = guid_index.resolve(mesh_guid)
    if asset_path is None:
        return None

    # Check uploaded_assets with multiple key formats (absolute, relative, forward/back slashes)
    relative = guid_index.resolve_relative(mesh_guid)
    candidates = [str(asset_path)]
    if relative:
        candidates.append(str(relative))
    # Try all with both slash directions
    for key in list(candidates):
        candidates.append(key.replace("\\", "/"))
        candidates.append(key.replace("/", "\\"))

    for key in candidates:
        if key in uploaded_assets:
            return uploaded_assets[key]

    return None


def _resolve_mesh_texture_id(
    mesh_guid: str,
    guid_index: GuidIndex | None,
) -> str | None:
    """Resolve a mesh GUID to an embedded TextureID from _mesh_texture_ids.

    When FBX models are uploaded to Roblox and resolved via InsertService,
    they may contain an embedded TextureID. This function looks up the
    texture ID using the mesh asset path.
    """
    if not _mesh_texture_ids or guid_index is None:
        return None

    asset_path = guid_index.resolve(mesh_guid)
    if asset_path is None:
        return None

    relative = guid_index.resolve_relative(mesh_guid)
    candidates = [str(asset_path)]
    if relative:
        candidates.append(str(relative))
    for key in list(candidates):
        candidates.append(key.replace("\\", "/"))
        candidates.append(key.replace("/", "\\"))

    for key in candidates:
        if key in _mesh_texture_ids:
            return _mesh_texture_ids[key]

    return None


# ---------------------------------------------------------------------------
# MonoBehaviour serialized field extraction
# ---------------------------------------------------------------------------

# System properties to skip when extracting MonoBehaviour fields
_MONO_SYSTEM_PROPS = frozenset({
    "m_ObjectHideFlags", "m_CorrespondingSourceObject", "m_PrefabInstance",
    "m_PrefabAsset", "m_GameObject", "m_Enabled", "m_EditorHideFlags",
    "m_Script", "m_Name", "m_EditorClassIdentifier", "serializedVersion",
    "m_IncludeLayers", "m_ExcludeLayers", "m_LayerOverridePriority",
    "m_Material", "m_ProvidesContacts",
})


def _extract_monobehaviour_attributes(
    properties: dict[str, Any],
    part: RbxPart,
    guid_index: GuidIndex | None,
) -> None:
    """Extract serialized fields from a MonoBehaviour as Roblox attributes.

    Extracts simple numeric (int/float), string, and boolean values.
    Also resolves the script class name for script-to-part binding.
    Object references and complex types are skipped.
    """
    # Resolve script class name from m_Script GUID
    script_ref = properties.get("m_Script", {})
    if isinstance(script_ref, dict) and guid_index:
        script_guid = script_ref.get("guid", "")
        if script_guid:
            script_path = guid_index.resolve(script_guid)
            if script_path and script_path.suffix == ".cs":
                part.attributes["_ScriptClass"] = script_path.stem

    for key, value in properties.items():
        if key in _MONO_SYSTEM_PROPS or key.startswith("m_"):
            continue

        # Only extract simple types that map to Roblox attributes
        if isinstance(value, (int, float)):
            part.attributes[key] = value
        elif isinstance(value, str) and len(value) < 100:
            part.attributes[key] = value
        elif isinstance(value, bool):
            part.attributes[key] = value


# ---------------------------------------------------------------------------
# Water shader detection
# ---------------------------------------------------------------------------

def _is_water_node(
    node: Any,
    material_mappings: dict[str, Any],
    guid_index: Any = None,
) -> bool:
    """Check if a node uses a water shader (should be converted to terrain water).

    Checks the node's MeshRenderer materials for water-related shader names
    and material names.  Falls back to node name if no shader info available.
    """
    if not hasattr(node, "components"):
        return False
    for comp in node.components:
        if comp.component_type in ("MeshRenderer", "SkinnedMeshRenderer"):
            for mat_ref in comp.properties.get("m_Materials", []):
                if isinstance(mat_ref, dict):
                    guid = mat_ref.get("guid", "")
                    if guid and material_mappings:
                        mapping = material_mappings.get(guid)
                        if mapping and hasattr(mapping, "shader_name"):
                            shader = mapping.shader_name.lower()
                            if "water" in shader or "ocean" in shader:
                                return True
                        # Also check material name for water keywords
                        if mapping and hasattr(mapping, "material_name"):
                            mat_name = mapping.material_name.lower()
                            if "water" in mat_name or "ocean" in mat_name:
                                return True
    # Fallback: check node name for water keywords
    if hasattr(node, "name") and node.name:
        name_lower = node.name.lower()
        if "water" in name_lower or "ocean" in name_lower:
            return True
    return False


def _extract_water_region(node: Any) -> RbxWaterRegion:
    """Extract a water fill region from a Unity water plane node.

    Unity water is typically a Plane (10x10m base) or Quad (1x1m base) with
    scale applied.  The default Unity Plane is 10x10 meters; its localScale
    is multiplied to get the actual footprint.

    The water region is a flat block in Roblox: wide in X/Z, thin in Y.
    A small Y thickness (2 studs) gives a visible water surface.
    """
    STUDS = config.STUDS_PER_METER

    # Position: convert Unity position to Roblox coordinates.
    ux, uy, uz = node.position
    rx, ry, rz = unity_to_roblox_pos(ux, uy, uz)

    # Scale: Unity Plane mesh is 10x10 meters by default.
    # A Quad is 1x1 meter.  Detect by mesh fileID or assume Plane.
    sx, sy, sz = node.scale

    # Unity built-in Plane mesh fileID = 10209 (10x10m base)
    # Unity built-in Quad mesh fileID = 10210 (1x1m base)
    base_size = 10.0  # default Plane
    if hasattr(node, "mesh_file_id") and node.mesh_file_id == "10210":
        base_size = 1.0  # Quad

    width_m = abs(sx) * base_size
    depth_m = abs(sz) * base_size
    water_thickness = 2.0  # studs — thin water surface

    width_studs = width_m * STUDS
    depth_studs = depth_m * STUDS

    return RbxWaterRegion(
        position=(rx, ry, rz),
        size=(width_studs, water_thickness, depth_studs),
        name=node.name,
    )


def _extract_water_region_from_prefab(
    pos: tuple[float, float, float],
    scl: tuple[float, float, float],
    name: str,
) -> RbxWaterRegion:
    """Extract a water region from a prefab instance's world-space transform.

    Similar to _extract_water_region but takes pre-composed transform data
    as used by the prefab conversion path.
    """
    STUDS = config.STUDS_PER_METER

    rx, ry, rz = unity_to_roblox_pos(*pos)

    # Assume Plane base (10x10m).  Prefabs may vary but Plane is typical.
    base_size = 10.0
    width_m = abs(scl[0]) * base_size
    depth_m = abs(scl[2]) * base_size
    water_thickness = 2.0

    return RbxWaterRegion(
        position=(rx, ry, rz),
        size=(width_m * STUDS, water_thickness, depth_m * STUDS),
        name=name,
    )


# ---------------------------------------------------------------------------
# Material application
# ---------------------------------------------------------------------------

def _apply_materials(
    node: SceneNode,
    part: RbxPart,
    material_mappings: dict[str, Any],
) -> None:
    """Apply material mappings to a part based on the node's MeshRenderer."""
    from core.roblox_types import RbxSurfaceAppearance

    for comp in node.components:
        if comp.component_type not in ("MeshRenderer", "SkinnedMeshRenderer"):
            continue

        mat_refs = comp.properties.get("m_Materials", [])
        if not mat_refs:
            continue

        # Use the first material for the part's primary appearance.
        first_ref = mat_refs[0] if mat_refs else None
        if first_ref is None:
            continue

        guid = first_ref.get("guid", "") if isinstance(first_ref, dict) else ""
        if not guid or guid not in material_mappings:
            continue

        mapping = material_mappings[guid]

        # Build SurfaceAppearance.
        sa = RbxSurfaceAppearance(
            color_map=getattr(mapping, "color_map_path", None),
            normal_map=getattr(mapping, "normal_map_path", None),
            metalness_map=getattr(mapping, "metalness_map_path", None),
            roughness_map=getattr(mapping, "roughness_map_path", None),
            alpha_mode=getattr(mapping, "alpha_mode", "Overlay"),
            transparency=getattr(mapping, "transparency", 0.0),
        )

        base_color = getattr(mapping, "base_color", None)
        if base_color:
            sa.color = (base_color[0], base_color[1], base_color[2])
            part.color = sa.color

        part.surface_appearance = sa
        part.transparency = sa.transparency

        # Apply inferred Roblox material (Concrete, Metal, Wood, etc.)
        roblox_mat = getattr(mapping, "roblox_material", "Plastic")
        if roblox_mat != "Plastic":
            part.material = roblox_mat

        # Set reflectance from metallic value (shinier materials reflect more)
        metallic = getattr(mapping, "metallic", 0.0)
        if metallic and float(metallic) > 0.1:
            part.reflectance = min(1.0, float(metallic) * 0.5)

        # Store texture tiling/offset as attributes for custom shaders
        tiling = getattr(mapping, "tiling", None)
        if tiling:
            part.attributes["_TilingX"] = tiling[0]
            part.attributes["_TilingY"] = tiling[1]
        offset = getattr(mapping, "offset", None)
        if offset:
            part.attributes["_OffsetX"] = offset[0]
            part.attributes["_OffsetY"] = offset[1]

        # Roblox doesn't support multiple SurfaceAppearances on a single Part,
        # so we keep the first material's SurfaceAppearance but also blend in
        # the base color from additional materials for a more accurate part color.
        if len(mat_refs) > 1:
            _blend_extra_material_colors(mat_refs[1:], material_mappings, part)

        break  # SurfaceAppearance applied from first material; extra colors blended above.


def _blend_extra_material_colors(
    extra_refs: list[Any],
    material_mappings: dict[str, Any],
    part: RbxPart,
) -> None:
    """Blend base colors from additional materials into the part color.

    Since Roblox only supports one SurfaceAppearance per Part, we average
    the base colors from all materials (including the already-applied first one)
    to produce a more representative part color.
    """
    colors: list[tuple[float, float, float]] = []
    if part.color and part.color != (0.63, 0.63, 0.63):
        colors.append(part.color)

    for ref in extra_refs:
        guid = ref.get("guid", "") if isinstance(ref, dict) else ""
        if not guid or guid not in material_mappings:
            continue
        mapping = material_mappings[guid]
        base_color = getattr(mapping, "base_color", None)
        if base_color:
            colors.append((base_color[0], base_color[1], base_color[2]))

    if len(colors) >= 2:
        avg_r = sum(c[0] for c in colors) / len(colors)
        avg_g = sum(c[1] for c in colors) / len(colors)
        avg_b = sum(c[2] for c in colors) / len(colors)
        part.color = (avg_r, avg_g, avg_b)


def _apply_prefab_materials(
    node: Any,
    part: RbxPart,
    material_mappings: dict[str, Any],
) -> None:
    """Apply material mappings to a part based on a prefab node's components.

    Works like _apply_materials but for PrefabNode objects, which store
    PrefabComponent instances with the same component_type/properties interface.
    """
    from core.roblox_types import RbxSurfaceAppearance

    components = getattr(node, "components", None) or []
    for comp in components:
        if comp.component_type not in ("MeshRenderer", "SkinnedMeshRenderer"):
            continue

        mat_refs = comp.properties.get("m_Materials", [])
        if not mat_refs:
            continue

        # Use the first material for the part's primary appearance.
        first_ref = mat_refs[0] if mat_refs else None
        if first_ref is None:
            continue

        guid = first_ref.get("guid", "") if isinstance(first_ref, dict) else ""
        if not guid or guid not in material_mappings:
            continue

        mapping = material_mappings[guid]

        # Build SurfaceAppearance.
        sa = RbxSurfaceAppearance(
            color_map=getattr(mapping, "color_map_path", None),
            normal_map=getattr(mapping, "normal_map_path", None),
            metalness_map=getattr(mapping, "metalness_map_path", None),
            roughness_map=getattr(mapping, "roughness_map_path", None),
            alpha_mode=getattr(mapping, "alpha_mode", "Overlay"),
            transparency=getattr(mapping, "transparency", 0.0),
        )

        base_color = getattr(mapping, "base_color", None)
        if base_color:
            sa.color = (base_color[0], base_color[1], base_color[2])
            part.color = sa.color

        part.surface_appearance = sa
        part.transparency = sa.transparency

        # Apply inferred Roblox material
        roblox_mat = getattr(mapping, "roblox_material", "Plastic")
        if roblox_mat != "Plastic":
            part.material = roblox_mat

        if len(mat_refs) > 1:
            _blend_extra_material_colors(mat_refs[1:], material_mappings, part)

        break  # Use first material


# ---------------------------------------------------------------------------
# Lighting extraction
# ---------------------------------------------------------------------------

def _extract_lighting(render_settings: dict[str, Any]) -> RbxLightingConfig:
    """Extract lighting configuration from Unity RenderSettings."""
    lighting = RbxLightingConfig()

    if not render_settings:
        return lighting

    # Ambient color.
    ambient = render_settings.get("m_AmbientSkyColor", {})
    if isinstance(ambient, dict):
        r = float(ambient.get("r", 0.5))
        g = float(ambient.get("g", 0.5))
        b = float(ambient.get("b", 0.5))
        lighting.ambient = (r, g, b)
        lighting.outdoor_ambient = (r, g, b)

    # Fog.
    fog_enabled = render_settings.get("m_Fog", 0)
    if fog_enabled:
        fog_color = render_settings.get("m_FogColor", {})
        if isinstance(fog_color, dict):
            lighting.fog_color = (
                float(fog_color.get("r", 0.75)),
                float(fog_color.get("g", 0.75)),
                float(fog_color.get("b", 0.75)),
            )
        fog_start = render_settings.get("m_LinearFogStart", 0.0)
        fog_end = render_settings.get("m_LinearFogEnd", 100000.0)
        lighting.fog_start = float(fog_start)
        lighting.fog_end = float(fog_end)

    # Ambient intensity -> brightness.
    intensity = render_settings.get("m_AmbientIntensity", None)
    if intensity is not None:
        lighting.brightness = max(0.0, float(intensity) * 2.0)

    return lighting


def _apply_directional_light(
    parsed_scene: ParsedScene,
    lighting: RbxLightingConfig,
) -> None:
    """Extract directional light rotation and color to set ClockTime and brightness."""
    for node in parsed_scene.all_nodes.values():
        for comp in node.components:
            if comp.component_type != "Light":
                continue
            if int(comp.properties.get("m_Type", -1)) != 1:  # 1 = Directional
                continue

            # Extract sun color for Lighting.ColorShift_Top
            color = comp.properties.get("m_Color", {})
            if isinstance(color, dict):
                # Warm sun color affects the overall scene brightness
                sun_intensity = float(comp.properties.get("m_Intensity", 1.0))
                lighting.brightness = max(lighting.brightness, sun_intensity * 2.0)

            # Convert directional light rotation to ClockTime
            qx, qy, qz, qw = node.rotation
            import math
            # Pitch from quaternion (X rotation in ZXY order)
            sinr = 2.0 * (qw * qx + qy * qz)
            cosr = 1.0 - 2.0 * (qx * qx + qy * qy)
            pitch_deg = math.degrees(math.atan2(sinr, cosr))

            # Map pitch to Roblox ClockTime:
            # pitch 0° = horizon (6:00 or 18:00)
            # pitch -90° = directly overhead (12:00 noon)
            # pitch 90° = below horizon (0:00 midnight)
            clock_time = 12.0 + (pitch_deg / 90.0) * 6.0
            clock_time = max(0.0, min(24.0, clock_time))
            lighting.clock_time = clock_time

            log.debug("Directional light: pitch=%.1f° -> ClockTime=%.1f", pitch_deg, clock_time)
            return  # Use first directional light


# ---------------------------------------------------------------------------
# Skybox extraction
# ---------------------------------------------------------------------------

# Unity skybox material texture property names -> RbxSkyboxConfig field names.
_SKYBOX_FACE_MAP: dict[str, str] = {
    "_FrontTex": "front",
    "_MainTex": "front",   # Some skybox shaders use _MainTex for front
    "_BackTex": "back",
    "_LeftTex": "left",
    "_RightTex": "right",
    "_UpTex": "up",
    "_DownTex": "down",
}


def _extract_skybox(
    skybox_material_guid: str,
    guid_index: GuidIndex,
    uploaded_assets: dict[str, str],
) -> RbxSkyboxConfig | None:
    """Resolve a Unity skybox material GUID to an RbxSkyboxConfig.

    Parses the .mat file to find the 6 face texture GUIDs
    (_FrontTex, _BackTex, _LeftTex, _RightTex, _UpTex, _DownTex),
    resolves them to paths, and looks up rbxassetid:// URLs in uploaded_assets.
    """
    mat_path = guid_index.resolve(skybox_material_guid)
    if mat_path is None or not mat_path.exists():
        log.debug("Skybox material GUID %s could not be resolved", skybox_material_guid)
        return None

    try:
        raw = mat_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    # Parse skybox face textures using regex — PyYAML can't handle
    # Unity's older material format with repeated 'data:' keys.
    import re

    face_urls: dict[str, str] = {}
    # Pattern: "name: _FrontTex" followed eventually by "guid: HEXGUID"
    for unity_prop, rbx_field in _SKYBOX_FACE_MAP.items():
        # Find the property name in the material YAML
        pattern = rf"name:\s*{re.escape(unity_prop)}\s*\n\s*second:\s*\n\s*m_Texture:\s*\{{[^}}]*guid:\s*([a-f0-9]+)"
        m = re.search(pattern, raw)
        if not m:
            continue
        tex_guid = m.group(1)
        if not tex_guid or tex_guid == "0" * 32:
            continue

        tex_path = guid_index.resolve(tex_guid)
        if tex_path is None:
            continue

        # Look up the texture path in uploaded_assets to get an rbxassetid:// URL.
        url = _resolve_asset_url(str(tex_path), guid_index, tex_guid, uploaded_assets)
        if url:
            face_urls[rbx_field] = url

    if not face_urls:
        log.debug("No skybox face textures resolved for material %s", mat_path.name)
        return None

    skybox = RbxSkyboxConfig(
        front=face_urls.get("front", ""),
        back=face_urls.get("back", ""),
        left=face_urls.get("left", ""),
        right=face_urls.get("right", ""),
        up=face_urls.get("up", ""),
        down=face_urls.get("down", ""),
    )
    log.info("Skybox extracted: %d face textures resolved", len(face_urls))
    return skybox


def _resolve_asset_url(
    asset_path_str: str,
    guid_index: GuidIndex,
    guid: str,
    uploaded_assets: dict[str, str],
) -> str | None:
    """Resolve an asset path or GUID to an rbxassetid:// URL via uploaded_assets."""
    relative = guid_index.resolve_relative(guid)
    candidates = [asset_path_str]
    if relative:
        candidates.append(str(relative))

    # Also try "Assets/..." form from the absolute path
    p = Path(asset_path_str)
    parts = p.parts
    for i, part in enumerate(parts):
        if part == "Assets":
            candidates.append("/".join(parts[i:]))
            break

    # Try with both slash directions
    for key in list(candidates):
        candidates.append(key.replace("\\", "/"))
        candidates.append(key.replace("/", "\\"))

    for key in candidates:
        if key in uploaded_assets:
            return uploaded_assets[key]

    return None


# ---------------------------------------------------------------------------
# Camera extraction
# ---------------------------------------------------------------------------

def _find_camera(parsed_scene: ParsedScene) -> RbxCameraConfig | None:
    """Find the first Camera component in the scene and build an RbxCameraConfig."""
    for node in parsed_scene.all_nodes.values():
        for comp in node.components:
            if comp.component_type != "Camera":
                continue

            config = convert_camera(comp.properties)
            if config is None:
                continue

            # Apply the node's transform to the camera CFrame.
            rx, ry, rz = unity_to_roblox_pos(*node.position)
            rqx, rqy, rqz, rqw = unity_quat_to_roblox_quat(*node.rotation)
            rot = quaternion_to_rotation_matrix(rqx, rqy, rqz, rqw)

            config.cframe = RbxCFrame(
                x=rx, y=ry, z=rz,
                r00=rot[0], r01=rot[1], r02=rot[2],
                r10=rot[3], r11=rot[4], r12=rot[5],
                r20=rot[6], r21=rot[7], r22=rot[8],
            )
            return config

    return None


# ---------------------------------------------------------------------------
# Prefab instance resolution
# ---------------------------------------------------------------------------

_prefab_lib_cache: dict[str, Any] = {}


def _load_prefab_library(guid_index: GuidIndex) -> Any:
    """Lazy-load the prefab library for the project."""
    project_root = str(guid_index.project_root)
    if project_root not in _prefab_lib_cache:
        from unity.prefab_parser import parse_prefabs
        _prefab_lib_cache[project_root] = parse_prefabs(guid_index.project_root)
    return _prefab_lib_cache[project_root]



def _apply_gameplay_attributes(part: RbxPart, name: str) -> None:
    """Detect gameplay-critical prefabs by name and set attributes for runtime scripts."""
    name_lower = name.lower()

    if 'pickup' in name_lower:
        item_type = name.replace('Pickup', '').replace('pickup', '').strip()
        if not item_type:
            item_type = 'Generic'
        part.attributes['IsPickup'] = True
        part.attributes['ItemType'] = item_type
        if part.class_name == 'Model' and part.children:
            _set_pickup_on_first_basepart(part)
        log.debug('Pickup detected: %s (ItemType=%s)', name, item_type)

    if 'spawnpoint' in name_lower or 'spawn_point' in name_lower:
        part.attributes['IsSpawnPoint'] = True
        log.debug('SpawnPoint detected: %s', name)

    if 'mine' in name_lower and 'pickup' not in name_lower:
        part.attributes['IsMine'] = True
        log.debug('Mine detected: %s', name)


def _set_pickup_on_first_basepart(model: RbxPart) -> None:
    """Add a transparent touch-detection Part to pickup Models.

    Instead of setting IsPickup on visible MeshParts (which causes them to
    spin via the Pickup script's bobbing animation), we always add a
    dedicated invisible touch detector.
    """
    touch_part = RbxPart(
        name='PickupTouchDetector',
        class_name='Part',
        cframe=model.cframe,
        size=(4.0, 4.0, 4.0),
        transparency=1.0,
        anchored=True,
        can_collide=False,
    )
    touch_part.attributes['IsPickup'] = True
    if 'ItemType' in model.attributes:
        touch_part.attributes['ItemType'] = model.attributes['ItemType']
    model.children.append(touch_part)


def _convert_fbx_prefab_instance(
    pi: Any,
    fbx_path: Path,
    guid_index: GuidIndex | None,
    material_mappings: dict[str, Any],
    uploaded_assets: dict[str, str],
) -> list[RbxPart]:
    """Convert an FBX file used as a prefab instance into a MeshPart.

    Unity allows instantiating FBX models directly as prefabs. Since we can't
    parse the FBX hierarchy, we create a single MeshPart with the FBX mesh.
    """
    # Extract transform from modifications
    pos = [0.0, 0.0, 0.0]
    rot = [0.0, 0.0, 0.0, 1.0]
    scl = [1.0, 1.0, 1.0]
    name_override = None

    for mod in pi.modifications:
        if not isinstance(mod, dict):
            continue
        pp = mod.get("propertyPath", "")
        val = mod.get("value", "0")
        if pp == "m_Name":
            name_override = val
            continue
        if pp == "m_IsActive":
            try:
                if int(val) == 0:
                    return []
            except (ValueError, TypeError):
                pass
            continue
        try:
            fval = float(val)
        except (ValueError, TypeError):
            continue
        if pp == "m_LocalPosition.x": pos[0] = fval
        elif pp == "m_LocalPosition.y": pos[1] = fval
        elif pp == "m_LocalPosition.z": pos[2] = fval
        elif pp == "m_LocalRotation.x": rot[0] = fval
        elif pp == "m_LocalRotation.y": rot[1] = fval
        elif pp == "m_LocalRotation.z": rot[2] = fval
        elif pp == "m_LocalRotation.w": rot[3] = fval
        elif pp == "m_LocalScale.x": scl[0] = fval
        elif pp == "m_LocalScale.y": scl[1] = fval
        elif pp == "m_LocalScale.z": scl[2] = fval

    rx, ry, rz = unity_to_roblox_pos(*pos)
    rqx, rqy, rqz, rqw = unity_quat_to_roblox_quat(*rot)
    rot_mat = quaternion_to_rotation_matrix(rqx, rqy, rqz, rqw)

    cframe = RbxCFrame(
        x=rx, y=ry, z=rz,
        r00=rot_mat[0], r01=rot_mat[1], r02=rot_mat[2],
        r10=rot_mat[3], r11=rot_mat[4], r12=rot_mat[5],
        r20=rot_mat[6], r21=rot_mat[7], r22=rot_mat[8],
    )

    name = name_override or fbx_path.stem

    # Resolve mesh ID from uploaded assets
    mesh_guid = guid_index.guid_for_path(fbx_path) if guid_index and hasattr(guid_index, 'guid_for_path') else None
    mesh_id = None
    if mesh_guid:
        mesh_id = _resolve_mesh_id(mesh_guid, guid_index, uploaded_assets)

    # Compute size from native Roblox data (requires upload + resolve)
    combined_scale = (abs(scl[0]), abs(scl[1]), abs(scl[2]))
    mesh_size = unity_scale_to_roblox_size(combined_scale)
    mesh_init = None
    if mesh_guid and _mesh_native_sizes and guid_index:
        result = _compute_mesh_size(combined_scale, mesh_guid, guid_index, _mesh_native_sizes)
        if result:
            mesh_size, mesh_init = result
    elif mesh_guid and guid_index:
        # Fallback: use FBX import scale when native sizes unavailable
        import_scale = _get_fbx_import_scale(mesh_guid, guid_index)
        unit_ratio = _get_fbx_unit_ratio(mesh_guid, guid_index)
        sf = import_scale * unit_ratio * config.STUDS_PER_METER
        mesh_size = (combined_scale[0] * sf, combined_scale[1] * sf, combined_scale[2] * sf)

    # Infer Roblox material from the FBX filename
    from converter.material_mapper import _infer_roblox_material
    inferred_material = _infer_roblox_material(name)

    part = RbxPart(
        name=name,
        class_name="MeshPart",
        cframe=cframe,
        size=mesh_size,
        initial_size=mesh_init,
        anchored=True,
        material=inferred_material,
    )
    if mesh_id:
        part.mesh_id = mesh_id

    return [part]


def _convert_prefab_instance(
    pi: Any,
    prefab_lib: Any,
    guid_index: GuidIndex | None,
    material_mappings: dict[str, Any],
    uploaded_assets: dict[str, str],
) -> list[RbxPart]:
    """Convert a PrefabInstance into RbxParts by resolving its source prefab."""
    from core.unity_types import PrefabInstanceData

    # Resolve prefab template
    resolved = guid_index.resolve(pi.source_prefab_guid) if guid_index else None
    if not resolved:
        return []

    template = prefab_lib.by_name.get(resolved.stem)
    if not template or not template.root:
        # Handle FBX/OBJ files used as prefabs (Model Prefabs in Unity)
        if resolved.suffix.lower() in ('.fbx', '.obj'):
            return _convert_fbx_prefab_instance(pi, resolved, guid_index, material_mappings, uploaded_assets)
        return []

    # Check for removed components -- these should not be instantiated
    removed_component_ids: set[str] = set()
    if hasattr(pi, "removed_components") and pi.removed_components:
        for rc in pi.removed_components:
            if isinstance(rc, dict):
                fid = str(rc.get("fileID", ""))
                if fid:
                    removed_component_ids.add(fid)

    # Build per-target-fileID modification map for child overrides.
    # Modifications with no target or matching the root transform go to root.
    # Others are grouped by target fileID for child node application.
    child_modifications: dict[str, list[dict]] = {}  # fileID -> [mod, ...]
    root_transform_fid = str(template.root.file_id) if hasattr(template.root, 'file_id') else ""
    for mod in pi.modifications:
        if not isinstance(mod, dict):
            continue
        target = mod.get("target", {})
        target_fid = str(target.get("fileID", "")) if isinstance(target, dict) else ""
        if target_fid and target_fid != "0" and target_fid != root_transform_fid:
            child_modifications.setdefault(target_fid, []).append(mod)

    # Extract position/rotation/scale and additional properties from modifications
    pos = [0.0, 0.0, 0.0]
    rot = [0.0, 0.0, 0.0, 1.0]
    scl = [1.0, 1.0, 1.0]
    name_override = None
    material_override_guid: str | None = None
    is_static = False
    tag_override: str | None = None
    layer_override: int | None = None
    disabled_components: set[str] = set()  # target fileIDs with m_Enabled=0
    custom_field_overrides: dict[str, Any] = {}  # MonoBehaviour field overrides

    for mod in pi.modifications:
        if not isinstance(mod, dict):
            continue
        pp = mod.get("propertyPath", "")
        val = mod.get("value", "0")

        # m_IsActive: if the GameObject is inactive, skip the entire instance
        if pp == "m_IsActive":
            try:
                if int(val) == 0:
                    return []
            except (ValueError, TypeError):
                pass
            continue

        if pp == "m_Name":
            name_override = val
            continue

        # m_Enabled: track which components are disabled
        if pp == "m_Enabled":
            try:
                if int(val) == 0:
                    target = mod.get("target", {})
                    if isinstance(target, dict):
                        target_fid = str(target.get("fileID", ""))
                        if target_fid:
                            disabled_components.add(target_fid)
            except (ValueError, TypeError):
                pass
            continue

        # m_Materials.Array.data[*]: material override on the prefab instance
        if pp.startswith("m_Materials.Array.data["):
            obj_ref = mod.get("objectReference", {})
            if isinstance(obj_ref, dict):
                guid = obj_ref.get("guid", "")
                if guid:
                    material_override_guid = guid
            continue

        # m_StaticEditorFlags: indicates if object is static (useful for anchoring)
        if pp == "m_StaticEditorFlags":
            try:
                if int(val) != 0:
                    is_static = True
            except (ValueError, TypeError):
                pass
            continue

        # m_TagString: tag override
        if pp == "m_TagString":
            tag_override = val
            continue

        # m_Layer: layer override
        if pp == "m_Layer":
            try:
                layer_override = int(val)
            except (ValueError, TypeError):
                pass
            continue

        try:
            fval = float(val)
        except (ValueError, TypeError):
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
        elif pp == "m_LocalScale.x":
            scl[0] = fval
        elif pp == "m_LocalScale.y":
            scl[1] = fval
        elif pp == "m_LocalScale.z":
            scl[2] = fval
        elif not pp.startswith("m_") and "." not in pp:
            # Custom MonoBehaviour field override (e.g., "rotate", "damage")
            custom_field_overrides[pp] = fval

    # Also collect string-valued custom field overrides we skipped above
    for mod in pi.modifications:
        if not isinstance(mod, dict):
            continue
        pp = mod.get("propertyPath", "")
        val = mod.get("value", "")
        if not pp.startswith("m_") and "." not in pp and pp not in custom_field_overrides:
            if isinstance(val, str) and val and len(val) < 100:
                try:
                    float(val)  # Already captured as numeric
                except (ValueError, TypeError):
                    custom_field_overrides[pp] = val

    # Convert transform
    rx, ry, rz = unity_to_roblox_pos(*pos)
    rqx, rqy, rqz, rqw = unity_quat_to_roblox_quat(*rot)
    rot_mat = quaternion_to_rotation_matrix(rqx, rqy, rqz, rqw)

    cframe = RbxCFrame(
        x=rx, y=ry, z=rz,
        r00=rot_mat[0], r01=rot_mat[1], r02=rot_mat[2],
        r10=rot_mat[3], r11=rot_mat[4], r12=rot_mat[5],
        r20=rot_mat[6], r21=rot_mat[7], r22=rot_mat[8],
    )

    name = name_override or template.name

    # Collect prefab instances that use water shaders as water regions
    root = template.root
    if _is_water_node(root, material_mappings, guid_index):
        # Compose instance scale with prefab root's internal scale
        root_scl = root.scale
        composed_scl = (scl[0] * root_scl[0], scl[1] * root_scl[1], scl[2] * root_scl[2])
        region = _extract_water_region_from_prefab(pos, composed_scl, name)
        _water_regions.append(region)
        log.info("Detected water prefab '%s' at (%.1f, %.1f, %.1f), size (%.1f, %.1f, %.1f)",
                 name, *region.position, *region.size)
        return []

    # Convert the prefab's root node into an RbxPart
    has_children = len(root.children) > 0

    if has_children:
        part = RbxPart(
            name=name,
            class_name="Model",
            cframe=cframe,
        )
        for child in root.children:
            # Convert child prefab nodes with parent world transform
            # so positions are in world space (Roblox Models don't
            # apply parent transforms to children like Unity does).
            child_part = _convert_prefab_node(
                child, guid_index, material_mappings, uploaded_assets,
                parent_pos=pos, parent_rot=rot, parent_scl=scl,
                child_modifications=child_modifications,
                disabled_components=disabled_components,
            )
            if child_part:
                part.children.append(child_part)

        # If the root has a BoxCollider and child meshes are very small,
        # add an invisible collision Part at the Model's position.
        # This gives turret-type prefabs proper collision bounds.
        if hasattr(root, 'components'):
            # Check if children are visually tiny
            max_child_size = 0
            for child in part.children:
                if hasattr(child, 'size') and child.size:
                    max_child_size = max(max_child_size, max(child.size))

            for comp in root.components:
                if comp.component_type == "BoxCollider":
                    col_size = comp.properties.get("m_Size", {})
                    if isinstance(col_size, dict) and "x" in col_size:
                        cx = float(col_size.get("x", 1))
                        cy = float(col_size.get("y", 1))
                        cz = float(col_size.get("z", 1))
                        cx *= abs(scl[0])
                        cy *= abs(scl[1])
                        cz *= abs(scl[2])
                        col_studs = (cx * config.STUDS_PER_METER, cy * config.STUDS_PER_METER, cz * config.STUDS_PER_METER)

                        # Only add if children are small relative to collider
                        if max_child_size < max(col_studs) * 0.3:
                            collider_part = RbxPart(
                                name="Collider",
                                class_name="Part",
                                cframe=cframe,
                                size=col_studs,
                                transparency=1.0,
                                anchored=True,
                                can_collide=True,
                            )
                            part.children.append(collider_part)
                    break

        # Process root node components (lights, audio, rigidbody) and attach
        # them to the first child BasePart since Roblox Models can't hold them.
        if hasattr(root, 'components') and root.components and part.children:
            # Find the first visible child Part/MeshPart
            target_child = None
            for child in part.children:
                if child.class_name in ('Part', 'MeshPart') and child.transparency < 1.0:
                    target_child = child
                    break
            if target_child is None and part.children:
                target_child = part.children[0]
            if target_child:
                _process_components(root, target_child, guid_index=guid_index, uploaded_assets=uploaded_assets)
    else:
        # Determine size: combine prefab root scale with instance scale override
        root_sx = abs(root.scale[0]) if hasattr(root, "scale") else 1.0
        root_sy = abs(root.scale[1]) if hasattr(root, "scale") else 1.0
        root_sz = abs(root.scale[2]) if hasattr(root, "scale") else 1.0
        sx = max(root_sx * abs(scl[0]), 0.1)
        sy = max(root_sy * abs(scl[1]), 0.1)
        sz = max(root_sz * abs(scl[2]), 0.1)

        # Convert size using coordinate transform (Unity Z -> Roblox -Z, same magnitude)
        rbx_size = unity_scale_to_roblox_size((sx, sy, sz))

        has_mesh = root.mesh_guid is not None
        _builtin = _UNITY_BUILTIN_MESH_SHAPES.get(root.mesh_file_id or "") if hasattr(root, "mesh_file_id") else None

        if _builtin:
            class_name = "Part"
        elif has_mesh:
            class_name = "MeshPart"
        else:
            class_name = "Part"

        part = RbxPart(
            name=name,
            class_name=class_name,
            cframe=cframe,
            size=rbx_size,
            anchored=True,
        )

        if _builtin:
            shape_enum, flatten = _builtin
            part.shape = shape_enum
            if flatten:
                part.size = (rbx_size[0], 0.2, rbx_size[2])
        elif has_mesh and root.mesh_guid:
            mesh_id = _resolve_mesh_id(root.mesh_guid, guid_index, uploaded_assets,
                                        mesh_file_id=root.mesh_file_id if hasattr(root, 'mesh_file_id') else None)
            if mesh_id:
                part.mesh_id = mesh_id
            # Compute mesh size from native Roblox data (requires upload + resolve)
            combined_scale = (sx, sy, sz)
            sized = False
            if _mesh_native_sizes and guid_index:
                result = _compute_mesh_size(combined_scale, root.mesh_guid, guid_index, _mesh_native_sizes)
                if result:
                    part.size, part.initial_size = result
                    sized = True
            if not sized and guid_index:
                import_scale = _get_fbx_import_scale(root.mesh_guid, guid_index)
                unit_ratio = _get_fbx_unit_ratio(root.mesh_guid, guid_index)
                sf = import_scale * unit_ratio * config.STUDS_PER_METER
                part.size = (abs(combined_scale[0]) * sf, abs(combined_scale[1]) * sf, abs(combined_scale[2]) * sf)
            # Set TextureID from embedded FBX texture if available
            tex_id = _resolve_mesh_texture_id(root.mesh_guid, guid_index)
            if tex_id:
                part.texture_id = tex_id

        # Apply materials from the prefab root's components
        _apply_prefab_materials(root, part, material_mappings)

        # Process components (colliders, lights, audio, rigidbody, particles)
        if hasattr(root, 'components') and root.components:
            _process_components(root, part, guid_index=guid_index, uploaded_assets=uploaded_assets)

    # Apply additional modification properties to the part
    if is_static:
        part.anchored = True

    if tag_override:
        part.attributes["UnityTag"] = tag_override

    if layer_override is not None:
        part.attributes["UnityLayer"] = layer_override

    # Apply material override if one was specified
    if material_override_guid and guid_index:
        mat_path = guid_index.resolve(material_override_guid)
        if mat_path and str(mat_path) in material_mappings:
            mat_info = material_mappings[str(mat_path)]
            if isinstance(mat_info, dict):
                if "color" in mat_info:
                    part.color = mat_info["color"]
                if "surface_appearance" in mat_info:
                    part.surface_appearance = mat_info["surface_appearance"]

    # Note: removed_component_ids and disabled_components are collected for
    # future use when component-level conversion is added to prefab instances.

    # --- Apply custom MonoBehaviour field overrides as attributes ---
    # These are per-instance overrides like "rotate=0" on specific turrets
    if custom_field_overrides:
        for field_name, field_value in custom_field_overrides.items():
            part.attributes[field_name] = field_value

    # --- Gameplay attribute detection ---
    # Detect pickup, spawn point, and mine prefabs by name and set attributes
    # so that runtime scripts can find them via attribute queries.
    _apply_gameplay_attributes(part, name)

    return [part]


def _convert_prefab_node(
    node: Any,
    guid_index: GuidIndex | None,
    material_mappings: dict[str, Any],
    uploaded_assets: dict[str, str],
    depth: int = 0,
    parent_pos: list[float] | tuple[float, ...] | None = None,
    parent_rot: list[float] | tuple[float, ...] | None = None,
    parent_scl: list[float] | tuple[float, ...] | None = None,
    child_modifications: dict[str, list[dict]] | None = None,
    disabled_components: set[str] | None = None,
) -> RbxPart | None:
    """Convert a PrefabNode to an RbxPart.

    When parent_pos/parent_rot/parent_scl are provided, the node's local
    transform is composed with the parent's world transform so that the
    resulting CFrame is in world space (required because Roblox Model
    children use world-space CFrames, not local offsets).

    child_modifications: per-target-fileID overrides from the prefab instance.
    disabled_components: set of component fileIDs that are disabled on this instance.
    """
    if depth > 32:
        return None

    # Compose local position with parent world transform if provided.
    local_pos = list(node.position)
    local_rot = list(node.rotation)
    local_scl = list(node.scale)

    if parent_pos is not None:
        # Apply parent rotation to local position, then add parent position.
        # This is how Unity composes parent + child transforms.
        pr = parent_rot or [0.0, 0.0, 0.0, 1.0]
        pp = parent_pos or [0.0, 0.0, 0.0]
        ps = parent_scl or [1.0, 1.0, 1.0]

        # Scale local position by parent scale
        scaled_local = [
            local_pos[0] * ps[0],
            local_pos[1] * ps[1],
            local_pos[2] * ps[2],
        ]

        # Rotate scaled local position by parent quaternion
        rotated = _quat_rotate(pr, scaled_local)

        # Add parent position
        world_pos = [
            pp[0] + rotated[0],
            pp[1] + rotated[1],
            pp[2] + rotated[2],
        ]

        # Compose rotations (parent * child)
        world_rot = _quat_multiply(pr, local_rot)

        # Compose scales
        world_scl = [
            local_scl[0] * ps[0],
            local_scl[1] * ps[1],
            local_scl[2] * ps[2],
        ]

        local_pos = world_pos
        local_rot = world_rot
        local_scl = world_scl

    rx, ry, rz = unity_to_roblox_pos(*local_pos)
    rqx, rqy, rqz, rqw = unity_quat_to_roblox_quat(*local_rot)
    rot = quaternion_to_rotation_matrix(rqx, rqy, rqz, rqw)

    rbx_size = unity_scale_to_roblox_size(local_scl)

    has_mesh = node.mesh_guid is not None
    has_children = len(node.children) > 0
    _builtin = _UNITY_BUILTIN_MESH_SHAPES.get(node.mesh_file_id or "") if hasattr(node, "mesh_file_id") else None

    if _builtin:
        class_name = "Part"
    elif has_children and not has_mesh:
        class_name = "Model"
    elif has_mesh:
        class_name = "MeshPart"
    else:
        class_name = "Part"

    cframe = RbxCFrame(
        x=rx, y=ry, z=rz,
        r00=rot[0], r01=rot[1], r02=rot[2],
        r10=rot[3], r11=rot[4], r12=rot[5],
        r20=rot[6], r21=rot[7], r22=rot[8],
    )

    part = RbxPart(
        name=node.name,
        class_name=class_name,
        cframe=cframe,
        size=rbx_size,
        anchored=True,
    )

    if _builtin:
        shape_enum, flatten = _builtin
        part.shape = shape_enum
        if flatten:
            part.size = (rbx_size[0], 0.2, rbx_size[2])
    elif has_mesh and node.mesh_guid:
        mesh_id = _resolve_mesh_id(node.mesh_guid, guid_index, uploaded_assets,
                                    mesh_file_id=node.mesh_file_id if hasattr(node, 'mesh_file_id') else None)
        if mesh_id:
            part.mesh_id = mesh_id
        # Compute mesh size from native Roblox data (requires upload + resolve)
        sized = False
        if _mesh_native_sizes and guid_index:
            result = _compute_mesh_size(local_scl, node.mesh_guid, guid_index, _mesh_native_sizes)
            if result:
                part.size, part.initial_size = result
                sized = True
        if not sized and guid_index:
            import_scale = _get_fbx_import_scale(node.mesh_guid, guid_index)
            unit_ratio = _get_fbx_unit_ratio(node.mesh_guid, guid_index)
            sf = import_scale * unit_ratio * config.STUDS_PER_METER
            part.size = (abs(local_scl[0]) * sf, abs(local_scl[1]) * sf, abs(local_scl[2]) * sf)
        # Set TextureID from embedded FBX texture if available
        tex_id = _resolve_mesh_texture_id(node.mesh_guid, guid_index)
        if tex_id:
            part.texture_id = tex_id

    # Apply per-instance modifications for this specific child node.
    node_fid = str(getattr(node, 'file_id', ''))
    if child_modifications and node_fid and node_fid in child_modifications:
        for mod in child_modifications[node_fid]:
            pp = mod.get("propertyPath", "")
            val = mod.get("value", "")
            # Name override
            if pp == "m_Name":
                part.name = val
            # Custom field overrides
            elif not pp.startswith("m_") and "." not in pp:
                try:
                    part.attributes[pp] = float(val)
                except (ValueError, TypeError):
                    if isinstance(val, str) and val and len(val) < 100:
                        part.attributes[pp] = val
            # Material override for this child
            elif pp.startswith("m_Materials.Array.data["):
                obj_ref = mod.get("objectReference", {})
                if isinstance(obj_ref, dict):
                    mat_guid = obj_ref.get("guid", "")
                    if mat_guid and guid_index:
                        mat_path = guid_index.resolve(mat_guid)
                        if mat_path and str(mat_path) in material_mappings:
                            mapping = material_mappings[str(mat_path)]
                            base_color = getattr(mapping, "base_color", None)
                            if base_color:
                                part.color = (base_color[0], base_color[1], base_color[2])

    # Check if this node's components are disabled
    if disabled_components and hasattr(node, 'components'):
        for comp in node.components:
            comp_fid = str(getattr(comp, 'file_id', ''))
            if comp_fid and comp_fid in disabled_components:
                part.transparency = 1.0
                part.can_collide = False

    # Apply materials from prefab node components (MeshRenderer/SkinnedMeshRenderer)
    _apply_prefab_materials(node, part, material_mappings)

    # Process components (colliders, lights, audio, rigidbody, particles)
    if hasattr(node, 'components') and node.components:
        _process_components(node, part, guid_index=guid_index, uploaded_assets=uploaded_assets)

    for child in node.children:
        child_part = _convert_prefab_node(
            child, guid_index, material_mappings, uploaded_assets, depth + 1,
            parent_pos=local_pos, parent_rot=local_rot, parent_scl=local_scl,
            child_modifications=child_modifications,
            disabled_components=disabled_components,
        )
        if child_part:
            part.children.append(child_part)

    return part


# ---------------------------------------------------------------------------
# Terrain collection
# ---------------------------------------------------------------------------

def _collect_terrains(parsed_scene: ParsedScene, place: RbxPlace) -> None:
    """Scan all scene nodes for Terrain components and create terrain parts.

    For each Terrain component found, an RbxTerrain is recorded on the place
    and a corresponding ground-plane Part is added to workspace_parts.  The
    Part uses the terrain's position and size (converted to Roblox coordinates)
    with a Grass material.

    Unity Terrain is positioned at its Transform origin, and its size extends
    in +X and +Z from that origin.  So the Roblox Part center needs to be
    offset by half the terrain width/depth.
    """
    for node in parsed_scene.all_nodes.values():
        for comp in node.components:
            if comp.component_type not in _TERRAIN_TYPES:
                continue

            terrain = convert_terrain(comp.properties, node.position)
            if terrain is None:
                continue

            place.terrains.append(terrain)

            # Terrain metadata is stored for the terrain generator script.
            # No flat Part is created — real voxel terrain is generated
            # by the embedded TerrainGenerator script at runtime.
            log.info(
                "Terrain detected: size=(%.0f, %.0f, %.0f), position=(%.1f, %.1f, %.1f), guid=%s",
                terrain.size[0], terrain.size[1], terrain.size[2],
                terrain.position[0], terrain.position[1], terrain.position[2],
                terrain.terrain_data_guid,
            )


def _collect_post_processing(parsed_scene: ParsedScene, place: RbxPlace) -> None:
    """Scan all scene nodes for post-processing components.

    Collects Bloom, ColorGrading, DepthOfField, SunShafts, and Volume
    components and converts them to a single RbxPostProcessing config
    stored on the place.
    """
    pp_components = []
    for node in parsed_scene.all_nodes.values():
        for comp in node.components:
            if comp.component_type in _POST_PROCESSING_TYPES:
                pp_components.append(comp)

    if pp_components:
        pp = convert_post_processing(pp_components)
        if pp is not None:
            place.post_processing = pp
            log.info("Post-processing detected: bloom=%s, color_correction=%s, dof=%s, sun_rays=%s",
                     pp.bloom_enabled, pp.color_correction_enabled, pp.dof_enabled, pp.sun_rays_enabled)


# ---------------------------------------------------------------------------
# Auto-generated floor and spawn
# ---------------------------------------------------------------------------


def _flatten_single_child_models(parts: list[RbxPart]) -> int:
    """Flatten Models with a single Part/MeshPart child.

    When a Model has exactly one child that is a Part or MeshPart (not another
    Model), and the Model itself has no scripts/sounds/lights, replace the
    Model with the child, keeping the Model's name.

    Recurses into the tree.  Returns the total number of flattened Models.
    """
    count = 0
    for i, part in enumerate(parts):
        # Recurse first so inner flattening happens before outer checks
        if part.children:
            count += _flatten_single_child_models(part.children)

        if (part.class_name == "Model"
            and len(part.children) == 1
            and part.children[0].class_name in ("Part", "MeshPart")
            and not part.scripts
            and not part.sounds
            and not part.lights
            and not part.attributes):
            child = part.children[0]
            child.name = part.name  # Keep the Model's name
            parts[i] = child
            count += 1

    return count


def _add_floor_and_spawn(place: RbxPlace) -> None:
    """Add a terrain floor and SpawnLocation based on the scene's bounding box.

    If terrain was already detected (place.terrains is non-empty), the
    auto-generated floor is skipped since the terrain Part already provides
    a ground surface.  A SpawnLocation is still generated.

    Uses percentile-based filtering to avoid outlier parts (e.g. sky objects,
    far-off triggers) pulling the floor/spawn to extreme positions.
    """
    if not place.workspace_parts:
        return

    has_terrain = bool(place.terrains)

    # Collect all part positions
    positions: list[tuple[float, float, float]] = []

    def _scan(part: RbxPart) -> None:
        x, y, z = part.cframe.x, part.cframe.y, part.cframe.z
        if abs(x) < 10000 and abs(y) < 10000 and abs(z) < 10000:
            positions.append((x, y, z))
        for child in part.children:
            _scan(child)

    for part in place.workspace_parts:
        _scan(part)

    if not positions:
        return

    # Use 5th/95th percentile to exclude outliers
    xs = sorted(p[0] for p in positions)
    ys = sorted(p[1] for p in positions)
    zs = sorted(p[2] for p in positions)

    n = len(positions)
    lo = max(0, int(n * 0.05))
    hi = min(n - 1, int(n * 0.95))

    min_x, max_x = xs[lo], xs[hi]
    min_y = ys[lo]
    min_z, max_z = zs[lo], zs[hi]

    # Use median Y for spawn height (most parts cluster around ground level)
    median_y = ys[n // 2]

    center_x = (min_x + max_x) / 2
    center_z = (min_z + max_z) / 2
    width = max(max_x - min_x + 100, 200)
    depth = max(max_z - min_z + 100, 200)
    floor_y = min_y - 2

    # Floor -- only if no terrain was detected.
    if not has_terrain:
        floor = RbxPart(
            name="ConvertedFloor",
            class_name="Part",
            cframe=RbxCFrame(x=center_x, y=floor_y, z=center_z),
            size=(width, 1, depth),
            color=(0.36, 0.6, 0.3),  # green grass color
            anchored=True,
        )
        place.workspace_parts.append(floor)
    else:
        # Use terrain Y for floor reference.
        floor_y = place.terrains[0].position[1] - 1

    # SpawnLocation at center, at median Y + 3 studs (where most geometry is)
    spawn_y = max(median_y + 3, floor_y + 3)
    spawn = RbxPart(
        name="SpawnLocation",
        class_name="Part",
        cframe=RbxCFrame(x=center_x, y=spawn_y, z=center_z),
        size=(6, 1, 6),
        transparency=1.0,
        anchored=True,
    )
    place.workspace_parts.append(spawn)

    # Add a large invisible floor for collision while terrain generates.
    # Terrain is created at runtime by TerrainGenerator script, but the
    # player spawns immediately and needs solid ground.
    if has_terrain:
        tw = place.terrains[0].size[0]
        td = place.terrains[0].size[2]
        from core.coordinate_system import unity_to_roblox_pos
        tp = place.terrains[0].position
        tcx, tcy, tcz = unity_to_roblox_pos(
            tp[0] + tw / 2, tp[1] - 0.5, tp[2] + td / 2,
        )
        ground = RbxPart(
            name="GroundCollider",
            class_name="Part",
            cframe=RbxCFrame(x=tcx, y=tcy, z=tcz),
            size=(tw * config.STUDS_PER_METER, 1, td * config.STUDS_PER_METER),
            transparency=1.0,
            anchored=True,
            can_collide=True,
        )
        place.workspace_parts.append(ground)

    if has_terrain:
        log.info("Terrain-based spawn at (%.0f, %.1f, %.0f) [%d terrains, %d parts]",
                 center_x, spawn_y, center_z, len(place.terrains), n)
    else:
        log.info("Auto-generated floor at y=%.1f (%.0fx%.0f) and SpawnLocation at (%.0f, %.1f, %.0f) [median_y=%.1f, %d parts]",
                 floor_y, width, depth, center_x, spawn_y, center_z, median_y, n)
