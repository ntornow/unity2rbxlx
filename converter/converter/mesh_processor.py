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


def read_fbx_vertex_bounds(
    mesh_path: str | Path,
) -> dict[str, Any] | None:
    """Extract vertex bounding box from an FBX binary file.

    Parses the raw FBX binary to find the largest "Vertices" double array
    and computes the axis-aligned bounding box and center offset from the
    mesh origin.  This is needed because trimesh cannot load FBX files.

    Returns dict with bounding_box, center_offset, bounds_min, bounds_max,
    or None if the file cannot be parsed.
    """
    import struct
    import zlib as _zlib

    path = Path(mesh_path)
    if not path.exists():
        return None

    try:
        data = path.read_bytes()
    except OSError:
        return None

    if not data.startswith(b"Kaydara FBX Binary"):
        return None

    # Find ALL "Vertices" double arrays, pick the one with most vertices.
    search_token = b"\x08Vertices"
    best = None  # (vertex_count, min_pt, max_pt)
    idx = 0
    while True:
        idx = data.find(search_token, idx)
        if idx < 0:
            break
        prop_offset = idx + len(search_token)
        if prop_offset >= len(data):
            break
        type_byte = data[prop_offset]
        if type_byte == ord("d"):
            try:
                array_len, encoding, comp_len = struct.unpack_from(
                    "<III", data, prop_offset + 1
                )
                if array_len >= 9 and array_len % 3 == 0:
                    data_start = prop_offset + 1 + 12
                    if encoding == 1:
                        raw = _zlib.decompress(
                            data[data_start : data_start + comp_len]
                        )
                    else:
                        raw = data[data_start : data_start + array_len * 8]
                    vals = struct.unpack(f"<{array_len}d", raw)
                    n_verts = len(vals) // 3
                    if best is None or n_verts > best[0]:
                        xs, ys, zs = vals[0::3], vals[1::3], vals[2::3]
                        best = (n_verts, (min(xs), min(ys), min(zs)),
                                (max(xs), max(ys), max(zs)))
            except (struct.error, _zlib.error, OverflowError):
                pass
        idx += 1

    if best is None:
        return None

    _, min_pt, max_pt = best
    center = tuple((a + b) / 2.0 for a, b in zip(min_pt, max_pt))
    size = tuple(b - a for a, b in zip(min_pt, max_pt))
    return {
        "bounding_box": size,
        "center_offset": center,
        "bounds_min": min_pt,
        "bounds_max": max_pt,
    }


def mirror_mesh_z(
    mesh_path: str | Path,
    output_path: str | Path,
) -> Path | None:
    """Mirror a mesh along the Z axis for left-handed → right-handed conversion.

    Unity uses a left-handed coordinate system (Z-forward) while Roblox uses
    right-handed (Z-back).  Negating the Z coordinate of every vertex fixes
    mirrored text and geometry (e.g., "SEA" reading backwards on door meshes).

    The face winding is flipped after the mirror to preserve correct normals.

    Uses assimp CLI to convert FBX→OBJ (trimesh can't load FBX natively),
    then trimesh for the vertex manipulation.

    Returns the output path on success, or None on failure.
    """
    import shutil
    import subprocess
    import tempfile

    mesh_path = Path(mesh_path)
    output_path = Path(output_path)

    if not mesh_path.exists():
        return None

    try:
        import trimesh
        import numpy as np
    except ImportError:
        log.warning("trimesh/numpy not available; skipping Z-mirror for %s", mesh_path.name)
        return None

    # For OBJ files, load directly. For FBX, convert via assimp first.
    obj_to_load = None
    tmp_obj = None

    if mesh_path.suffix.lower() == ".obj":
        obj_to_load = str(mesh_path)
    else:
        assimp_cli = shutil.which("assimp")
        if not assimp_cli:
            log.warning("assimp CLI not found; skipping Z-mirror for %s", mesh_path.name)
            return None

        tmp_obj = tempfile.NamedTemporaryFile(suffix=".obj", delete=False)
        tmp_obj.close()
        try:
            result = subprocess.run(
                [assimp_cli, "export", str(mesh_path), tmp_obj.name],
                capture_output=True, timeout=60,
            )
            if result.returncode != 0:
                log.warning("assimp export failed for %s: %s", mesh_path.name,
                            result.stderr.decode(errors="replace")[:200])
                return None
            obj_to_load = tmp_obj.name
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.warning("assimp export error for %s: %s", mesh_path.name, exc)
            return None

    tmp_mirrored_obj = None
    try:
        scene = trimesh.load(obj_to_load, process=False)

        # Apply Z-axis mirror transform and fix face winding
        mirror = np.eye(4)
        mirror[2, 2] = -1.0

        if hasattr(scene, "geometry"):
            # Scene with multiple meshes
            for geom in scene.geometry.values():
                geom.apply_transform(mirror)
                geom.invert()  # fix face winding after mirror
        elif hasattr(scene, "vertices"):
            # Single mesh
            scene.apply_transform(mirror)
            scene.invert()

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # If output is FBX, we need to go through assimp (trimesh can't write FBX).
        # Write an intermediate OBJ, then convert to FBX with assimp.
        if output_path.suffix.lower() == ".fbx":
            assimp_cli = shutil.which("assimp")
            if not assimp_cli:
                log.warning("assimp CLI not found; cannot export %s as FBX", mesh_path.name)
                return None
            tmp_mirrored_obj = tempfile.NamedTemporaryFile(suffix=".obj", delete=False)
            tmp_mirrored_obj.close()
            scene.export(tmp_mirrored_obj.name)
            result = subprocess.run(
                [assimp_cli, "export", tmp_mirrored_obj.name, str(output_path)],
                capture_output=True, timeout=60,
            )
            if result.returncode != 0:
                log.warning("assimp FBX export failed for %s: %s", mesh_path.name,
                            result.stderr.decode(errors="replace")[:200])
                return None
        else:
            scene.export(str(output_path))

        log.info("Z-mirrored %s -> %s", mesh_path.name, output_path.name)
        return output_path

    except Exception as exc:
        log.warning("Failed to Z-mirror %s: %s", mesh_path.name, exc)
        return None
    finally:
        if tmp_obj:
            Path(tmp_obj.name).unlink(missing_ok=True)
            Path(tmp_obj.name.replace(".obj", ".mtl")).unlink(missing_ok=True)
        if tmp_mirrored_obj:
            Path(tmp_mirrored_obj.name).unlink(missing_ok=True)
            Path(tmp_mirrored_obj.name.replace(".obj", ".mtl")).unlink(missing_ok=True)


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
