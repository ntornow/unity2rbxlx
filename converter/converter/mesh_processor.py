"""
mesh_processor.py -- Mesh processing: info extraction, decimation, bounding box.

Provides mesh analysis and processing utilities needed for the conversion
pipeline, including face-count-based decimation checks and trimesh-powered
mesh simplification.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from config import MESH_ROBLOX_MAX_FACES, MESH_TARGET_FACES, MESH_QUALITY_FLOOR

log = logging.getLogger(__name__)


def get_mesh_info(mesh_path: str | Path) -> dict[str, Any]:
    """Extract mesh metadata: face count, vertex count, bounding box.

    Uses trimesh for loading. Falls back gracefully if trimesh is unavailable
    or the mesh cannot be loaded.

    Args:
        mesh_path: Path to the mesh file (.fbx, .obj, .glb, etc.).

    Returns:
        Dict with keys:
            - face_count (int): Number of triangular faces.
            - vertex_count (int): Number of vertices.
            - bounding_box (tuple[float, float, float]): Width, height, depth.
            - file_size (int): File size in bytes.
        Returns empty dict if the file does not exist.
    """
    path = Path(mesh_path)
    if not path.exists():
        log.warning("Mesh file not found: %s", path)
        return {}

    result: dict[str, Any] = {"file_size": path.stat().st_size}

    try:
        import trimesh  # type: ignore[import-untyped]
    except ImportError:
        log.warning("trimesh not installed; mesh info unavailable for %s", path.name)
        result.update(face_count=0, vertex_count=0, bounding_box=(1.0, 1.0, 1.0))
        return result

    try:
        scene_or_mesh = trimesh.load(str(path), force="mesh")

        # trimesh.load may return a Scene or a Trimesh depending on the file.
        if hasattr(scene_or_mesh, "faces"):
            mesh = scene_or_mesh
        elif hasattr(scene_or_mesh, "geometry"):
            # Scene with multiple meshes -- merge into one.
            meshes = list(scene_or_mesh.geometry.values())
            if meshes:
                mesh = trimesh.util.concatenate(meshes)
            else:
                mesh = None
        else:
            mesh = None

        if mesh is not None and hasattr(mesh, "faces"):
            result["face_count"] = int(len(mesh.faces))
            result["vertex_count"] = int(len(mesh.vertices))

            if hasattr(mesh, "bounds") and mesh.bounds is not None:
                bounds = mesh.bounds
                result["bounding_box"] = (
                    float(bounds[1][0] - bounds[0][0]),
                    float(bounds[1][1] - bounds[0][1]),
                    float(bounds[1][2] - bounds[0][2]),
                )
            else:
                result["bounding_box"] = (1.0, 1.0, 1.0)
        else:
            result.update(face_count=0, vertex_count=0, bounding_box=(1.0, 1.0, 1.0))

    except Exception as exc:
        log.warning("Failed to load mesh %s: %s", path.name, exc)
        result.update(face_count=0, vertex_count=0, bounding_box=(1.0, 1.0, 1.0))

    return result


def needs_decimation(face_count: int, max_faces: int = MESH_ROBLOX_MAX_FACES) -> bool:
    """Check whether a mesh exceeds the Roblox face-count limit and needs decimation.

    Args:
        face_count: Number of faces in the mesh.
        max_faces: Maximum allowed faces (default from config).

    Returns:
        True if the mesh should be decimated.
    """
    return face_count > max_faces


def decimate_mesh(
    mesh_path: str | Path,
    target_faces: int = MESH_TARGET_FACES,
    output_path: str | Path | None = None,
) -> Path:
    """Decimate a mesh to the target face count using trimesh.

    Uses quadric decimation when available, falling back to a simple
    vertex-subsampling heuristic.

    Args:
        mesh_path: Path to the source mesh file.
        target_faces: Desired number of faces after decimation.
        output_path: Where to write the decimated mesh. If None, writes
            next to the source with a ``_decimated`` suffix.

    Returns:
        Path to the output mesh file.

    Raises:
        ImportError: If trimesh is not installed.
        FileNotFoundError: If the source mesh does not exist.
    """
    import trimesh  # type: ignore[import-untyped]

    path = Path(mesh_path)
    if not path.exists():
        raise FileNotFoundError(f"Mesh file not found: {path}")

    if output_path is None:
        out = path.with_stem(path.stem + "_decimated")
    else:
        out = Path(output_path)

    out.parent.mkdir(parents=True, exist_ok=True)

    mesh = trimesh.load(str(path), force="mesh")

    # Handle scene objects.
    if hasattr(mesh, "geometry") and not hasattr(mesh, "faces"):
        meshes = list(mesh.geometry.values())
        if meshes:
            mesh = trimesh.util.concatenate(meshes)
        else:
            shutil.copy2(path, out)
            return out

    original_faces = int(len(mesh.faces))

    if original_faces <= target_faces:
        # No decimation needed -- just copy.
        shutil.copy2(path, out)
        log.info("Mesh %s has %d faces (under target %d); copied as-is",
                 path.name, original_faces, target_faces)
        return out

    # Compute decimation ratio for quality floor check.
    ratio = target_faces / original_faces
    if ratio < MESH_QUALITY_FLOOR:
        log.warning(
            "Decimation ratio %.2f is below quality floor %.2f for %s; "
            "clamping to floor",
            ratio, MESH_QUALITY_FLOOR, path.name,
        )
        target_faces = max(target_faces, int(original_faces * MESH_QUALITY_FLOOR))

    try:
        # Primary: quadric decimation (requires scipy/quadric extension).
        decimated = mesh.simplify_quadric_decimation(target_faces)
        decimated.export(str(out))
        final_faces = int(len(decimated.faces))
        log.info(
            "Decimated %s: %d -> %d faces (%.1f%% retained)",
            path.name,
            original_faces,
            final_faces,
            final_faces / original_faces * 100,
        )
    except (AttributeError, Exception) as exc:
        log.warning(
            "Quadric decimation unavailable for %s (%s); "
            "exporting original mesh",
            path.name, exc,
        )
        # Fallback: export the original mesh (better than corrupting it).
        mesh.export(str(out))

    return out


def compute_bounding_box(mesh_path: str | Path) -> tuple[float, float, float]:
    """Compute the axis-aligned bounding box size of a mesh.

    Args:
        mesh_path: Path to the mesh file.

    Returns:
        Tuple of (width, height, depth) in mesh units.
        Returns (1.0, 1.0, 1.0) on failure.
    """
    info = get_mesh_info(mesh_path)
    bbox = info.get("bounding_box")
    if bbox is not None and isinstance(bbox, tuple) and len(bbox) == 3:
        return bbox
    return (1.0, 1.0, 1.0)
