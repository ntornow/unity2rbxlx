"""
vertex_color_baker.py — Bakes mesh vertex colors into albedo textures.

Roblox's SurfaceAppearance ignores vertex colors stored in mesh data.
Many Unity games (especially mobile) rely heavily on vertex-color
multiplication to add variation without extra texture lookups.

This module:
  1. Loads a mesh file (OBJ, PLY, GLTF/GLB — FBX when trimesh supports it)
  2. Extracts per-vertex RGBA colors and UV coordinates
  3. Rasterises vertex colors onto a UV-space texture
  4. Multiplies the rasterised colour map into the albedo texture

No other module is imported here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BakeResult:
    """Outcome of baking vertex colours for a single mesh."""
    mesh_path: Path
    output_path: Path | None = None
    baked: bool = False
    has_vertex_colors: bool = False
    error: str = ""


@dataclass
class VertexColorBakeResult:
    """Aggregate result for all meshes processed."""
    entries: list[BakeResult] = field(default_factory=list)
    total: int = 0
    baked: int = 0
    skipped: int = 0
    no_colors: int = 0
    warnings: list[str] = field(default_factory=list)


_UNITY_FBX_FILE_ID_BASE = 4300000


def _unity_file_id_to_submesh_index(mesh_file_id: str | None) -> int | None:
    """Convert a Unity FBX sub-mesh fileID to a 0-based assimp mesh index.

    Unity's FBX importer assigns fileIDs starting at 4300000 and incrementing
    by 2 (4300000 → 0, 4300002 → 1, 4300004 → 2, ...). Returns None when the
    fileID is missing, malformed, below the base, or otherwise unconvertible.
    """
    if not mesh_file_id:
        return None
    try:
        fid = int(mesh_file_id)
    except (TypeError, ValueError):
        return None
    if fid < _UNITY_FBX_FILE_ID_BASE:
        return None
    return (fid - _UNITY_FBX_FILE_ID_BASE) // 2


def _load_fbx_via_assimp(
    mesh_path: Path,
    submesh_index: int | None = None,
) -> tuple[Any, Any, Any, Any] | None:
    """Load FBX using pyassimp's ctypes bindings (requires libassimp).

    Returns (vertices, faces, uv_coords, vertex_colors) or None.

    When ``submesh_index`` is set, only that single sub-mesh is loaded —
    used by the per-mesh-id baking path (Phase 5.7) so an FBX with N
    sub-meshes and N distinct vertex-color sets bakes to N distinct
    textures rather than one combined texture.
    """
    import numpy as np
    import ctypes
    import shutil

    # Locate libassimp shared library via the assimp CLI tool first,
    # then fall back to common system paths.
    import platform

    lib_path = None

    # Try to find via the assimp CLI binary (works on any platform)
    assimp_bin = shutil.which("assimp")
    if assimp_bin:
        bin_dir = Path(assimp_bin).resolve().parent.parent / "lib"
        for suffix in ("libassimp.dylib", "libassimp.so", "libassimp.dll"):
            p = bin_dir / suffix
            if p.exists():
                lib_path = str(p)
                break

    # Fall back to common system paths
    if lib_path is None:
        candidates = ["/usr/lib/libassimp.so", "/usr/local/lib/libassimp.so"]
        if platform.system() == "Darwin":
            candidates = [
                "/opt/homebrew/lib/libassimp.dylib",
                "/usr/local/lib/libassimp.dylib",
            ] + candidates
        for candidate in candidates:
            if Path(candidate).exists():
                lib_path = candidate
                break

    if lib_path is None:
        return None

    try:
        dll = ctypes.cdll.LoadLibrary(lib_path)
    except OSError:
        return None

    # Define minimal ctypes structs for assimp (avoids pyassimp import issues)
    class _aiVector3D(ctypes.Structure):
        _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float), ("z", ctypes.c_float)]

    class _aiColor4D(ctypes.Structure):
        _fields_ = [
            ("r", ctypes.c_float), ("g", ctypes.c_float),
            ("b", ctypes.c_float), ("a", ctypes.c_float),
        ]

    class _aiFace(ctypes.Structure):
        _fields_ = [
            ("mNumIndices", ctypes.c_uint),
            ("mIndices", ctypes.POINTER(ctypes.c_uint)),
        ]

    MAX_TEX_COORDS = 8
    MAX_COLOR_SETS = 8

    class _aiMesh(ctypes.Structure):
        _fields_ = [
            ("mPrimitiveTypes", ctypes.c_uint),
            ("mNumVertices", ctypes.c_uint),
            ("mNumFaces", ctypes.c_uint),
            ("mVertices", ctypes.POINTER(_aiVector3D)),
            ("mNormals", ctypes.POINTER(_aiVector3D)),
            ("mTangents", ctypes.POINTER(_aiVector3D)),
            ("mBitangents", ctypes.POINTER(_aiVector3D)),
            ("mColors", ctypes.POINTER(_aiColor4D) * MAX_COLOR_SETS),
            ("mTextureCoords", ctypes.POINTER(_aiVector3D) * MAX_TEX_COORDS),
            ("mNumUVComponents", ctypes.c_uint * MAX_TEX_COORDS),
            ("mFaces", ctypes.POINTER(_aiFace)),
        ]

    class _aiScene(ctypes.Structure):
        _fields_ = [
            ("mFlags", ctypes.c_uint),
            ("mRootNode", ctypes.c_void_p),
            ("mNumMeshes", ctypes.c_uint),
            ("mMeshes", ctypes.POINTER(ctypes.POINTER(_aiMesh))),
        ]

    load_fn = dll.aiImportFile
    load_fn.restype = ctypes.POINTER(_aiScene)
    release_fn = dll.aiReleaseImport

    scene_ptr = load_fn(str(mesh_path).encode(), 0)
    if not scene_ptr:
        return None

    try:
        scene = scene_ptr.contents
        if scene.mNumMeshes == 0:
            return None

        all_verts = []
        all_faces = []
        all_uvs = []
        all_colors = []
        vert_offset = 0

        if submesh_index is not None:
            if submesh_index < 0 or submesh_index >= scene.mNumMeshes:
                return None
            mesh_indices: range | list[int] = [submesh_index]
        else:
            mesh_indices = range(scene.mNumMeshes)

        for i in mesh_indices:
            m = scene.mMeshes[i].contents
            nv = m.mNumVertices

            has_colors = bool(m.mColors[0])
            has_uvs = bool(m.mTextureCoords[0])
            if not has_colors or not has_uvs:
                continue

            verts = np.zeros((nv, 3), dtype=np.float32)
            uvs = np.zeros((nv, 2), dtype=np.float32)
            colors = np.zeros((nv, 4), dtype=np.uint8)

            for j in range(nv):
                v = m.mVertices[j]
                verts[j] = (v.x, v.y, v.z)
                uv = m.mTextureCoords[0][j]
                uvs[j] = (uv.x, uv.y)
                c = m.mColors[0][j]
                colors[j] = (
                    int(max(0, min(255, c.r * 255))),
                    int(max(0, min(255, c.g * 255))),
                    int(max(0, min(255, c.b * 255))),
                    int(max(0, min(255, c.a * 255))),
                )

            faces = np.zeros((m.mNumFaces, 3), dtype=np.int32)
            for j in range(m.mNumFaces):
                f = m.mFaces[0]  # dummy — need pointer arithmetic
                pass

            # Use proper face access via the mFaces pointer
            face_ptr = m.mFaces
            faces_list = []
            for j in range(m.mNumFaces):
                f = face_ptr[j]
                if f.mNumIndices == 3:
                    faces_list.append((
                        f.mIndices[0] + vert_offset,
                        f.mIndices[1] + vert_offset,
                        f.mIndices[2] + vert_offset,
                    ))
            faces = np.array(faces_list, dtype=np.int32) if faces_list else np.zeros((0, 3), dtype=np.int32)

            all_verts.append(verts)
            all_faces.append(faces)
            all_uvs.append(uvs)
            all_colors.append(colors)
            vert_offset += nv

        if not all_verts:
            return None

        vertices = np.concatenate(all_verts)
        faces = np.concatenate(all_faces)
        uv_coords = np.concatenate(all_uvs)
        vertex_colors = np.concatenate(all_colors)

        if np.all(vertex_colors[:, :3] >= 250):
            return None

        return vertices, faces, uv_coords, vertex_colors
    finally:
        release_fn(scene_ptr)


def _load_mesh_vertex_data(
    mesh_path: Path,
    mesh_file_id: str | None = None,
) -> tuple[Any, Any, Any, Any] | None:
    """
    Load mesh and extract vertices, faces, UVs, and vertex colors.

    Returns (vertices, faces, uv_coords, vertex_colors) or None on failure.
    Each array is a numpy ndarray:
      - vertices: (N, 3) float
      - faces: (F, 3) int
      - uv_coords: (N, 2) float — per-vertex UV0
      - vertex_colors: (N, 4) uint8 — RGBA per vertex

    When ``mesh_file_id`` is a valid Unity FBX sub-mesh fileID
    (4300000-base + 2*index), only that sub-mesh's data is returned
    so per-sub-mesh vertex-color baking emits one texture per sub-mesh.
    """
    try:
        import numpy as np
    except ImportError:
        return None

    # FBX files: use pyassimp (trimesh can't load FBX)
    if mesh_path.suffix.lower() == ".fbx":
        submesh_index = _unity_file_id_to_submesh_index(mesh_file_id)
        return _load_fbx_via_assimp(mesh_path, submesh_index=submesh_index)

    try:
        import trimesh  # type: ignore
    except ImportError:
        return None

    try:
        mesh = trimesh.load(str(mesh_path), force="mesh", process=False)
    except Exception:
        return None

    vertices = mesh.vertices
    faces = mesh.faces

    # Extract vertex colors
    visual = mesh.visual
    colors = None
    if hasattr(visual, "kind") and visual.kind == "vertex":
        colors = visual.vertex_colors
    elif hasattr(visual, "vertex_colors") and visual.vertex_colors is not None:
        vc = visual.vertex_colors
        if hasattr(vc, "__len__") and len(vc) > 0:
            colors = vc

    # Fallback: extract from raw PLY metadata when trimesh stored vertex
    # colors in metadata but created TextureVisuals (because UVs exist).
    if colors is None and hasattr(mesh, "metadata"):
        raw = mesh.metadata.get("_ply_raw", {})
        vdata = raw.get("vertex", {}).get("data", {})
        if "red" in vdata and "green" in vdata and "blue" in vdata:
            r = np.array(vdata["red"], dtype=np.uint8).ravel()
            g = np.array(vdata["green"], dtype=np.uint8).ravel()
            b = np.array(vdata["blue"], dtype=np.uint8).ravel()
            if "alpha" in vdata:
                a = np.array(vdata["alpha"], dtype=np.uint8).ravel()
            else:
                a = np.full(len(r), 255, dtype=np.uint8)
            if len(r) == len(vertices):
                colors = np.column_stack([r, g, b, a])

    if colors is None or len(colors) == 0:
        return None

    # Ensure RGBA uint8
    colors = np.array(colors, dtype=np.uint8)
    if colors.ndim != 2 or colors.shape[1] < 3:
        return None
    if colors.shape[1] == 3:
        # Add alpha channel
        alpha = np.full((colors.shape[0], 1), 255, dtype=np.uint8)
        colors = np.hstack([colors, alpha])

    # Check if all vertex colors are white (255,255,255) — skip baking
    if np.all(colors[:, :3] >= 250):
        return None

    # Extract UVs
    uv = None
    if hasattr(visual, "uv") and visual.uv is not None:
        uv = np.array(visual.uv, dtype=np.float32)
    elif hasattr(mesh, "visual") and hasattr(mesh.visual, "uv"):
        uv = np.array(mesh.visual.uv, dtype=np.float32)

    if uv is None or len(uv) != len(vertices):
        # Try texture visual
        if hasattr(visual, "to_texture") and callable(visual.to_texture):
            try:
                tex_visual = visual.to_texture()
                if hasattr(tex_visual, "uv") and tex_visual.uv is not None:
                    uv = np.array(tex_visual.uv, dtype=np.float32)
            except Exception:
                pass

    if uv is None or len(uv) != len(vertices):
        return None

    return vertices, faces, uv, colors


def _rasterise_vertex_colors(
    faces: Any,
    uv_coords: Any,
    vertex_colors: Any,
    resolution: int = 512,
) -> Any:
    """
    Rasterise per-vertex colors onto a UV-space texture.

    For each triangle, fill the UV-space region with interpolated
    vertex colors using barycentric coordinates. Vectorized with NumPy
    for performance (~100x faster than per-pixel Python loops).

    Returns an (H, W, 4) uint8 RGBA numpy array.
    """
    import numpy as np

    tex = np.zeros((resolution, resolution, 4), dtype=np.float32)
    weight = np.zeros((resolution, resolution), dtype=np.float32)

    # Precompute all triangle data in bulk
    i0 = faces[:, 0]
    i1 = faces[:, 1]
    i2 = faces[:, 2]

    # UV → pixel coords (clamped to [0, 1])
    uv = np.clip(uv_coords, 0.0, 1.0)
    res_m1 = resolution - 1

    px_x = uv[:, 0] * res_m1  # all vertices x in pixel space
    px_y = (1.0 - uv[:, 1]) * res_m1  # all vertices y (flipped V)

    colors_f = vertex_colors.astype(np.float32)

    # Per-triangle pixel coordinates
    px0x, px0y = px_x[i0], px_y[i0]
    px1x, px1y = px_x[i1], px_y[i1]
    px2x, px2y = px_x[i2], px_y[i2]

    # Barycentric denominator for each triangle
    denom = (px1y - px2y) * (px0x - px2x) + (px2x - px1x) * (px0y - px2y)
    valid = np.abs(denom) > 1e-10

    # Process each valid triangle
    for fi in np.where(valid)[0]:
        x0, y0 = px0x[fi], px0y[fi]
        x1, y1 = px1x[fi], px1y[fi]
        x2, y2 = px2x[fi], px2y[fi]
        c0 = colors_f[i0[fi]]
        c1 = colors_f[i1[fi]]
        c2 = colors_f[i2[fi]]

        min_x = max(0, int(min(x0, x1, x2)))
        max_x = min(res_m1, int(max(x0, x1, x2)) + 1)
        min_y = max(0, int(min(y0, y1, y2)))
        max_y = min(res_m1, int(max(y0, y1, y2)) + 1)

        if max_x <= min_x or max_y <= min_y:
            continue

        inv_d = 1.0 / denom[fi]

        # Vectorize the inner pixel loop per triangle
        xs = np.arange(min_x, max_x + 1, dtype=np.float32)
        ys = np.arange(min_y, max_y + 1, dtype=np.float32)
        gx, gy = np.meshgrid(xs, ys)

        w0 = ((y1 - y2) * (gx - x2) + (x2 - x1) * (gy - y2)) * inv_d
        w1 = ((y2 - y0) * (gx - x2) + (x0 - x2) * (gy - y2)) * inv_d
        w2 = 1.0 - w0 - w1

        inside = (w0 >= -0.01) & (w1 >= -0.01) & (w2 >= -0.01)
        if not inside.any():
            continue

        # Clamp and normalize weights
        w0c = np.maximum(w0[inside], 0.0)
        w1c = np.maximum(w1[inside], 0.0)
        w2c = np.maximum(w2[inside], 0.0)
        wsum = w0c + w1c + w2c
        wsum[wsum == 0] = 1.0
        w0c /= wsum
        w1c /= wsum
        w2c /= wsum

        # Interpolate colors
        interp = (
            np.outer(w0c, c0) + np.outer(w1c, c1) + np.outer(w2c, c2)
        )

        # Write to texture
        py = gy[inside].astype(np.int32)
        px = gx[inside].astype(np.int32)
        # Use np.add.at for correct accumulation at duplicate indices
        np.add.at(tex, (py, px), interp)
        np.add.at(weight, (py, px), 1.0)

    # Average where multiple triangles overlap
    mask = weight > 0
    for c in range(4):
        tex[..., c][mask] /= weight[mask]

    # Fill uncovered pixels with average of covered pixels
    if not np.all(mask):
        if mask.any():
            avg_color = np.array([tex[..., c][mask].mean() for c in range(4)])
            for c in range(4):
                tex[..., c][~mask] = avg_color[c]
        else:
            tex[...] = 255.0

    return np.clip(tex, 0, 255).astype(np.uint8)


def bake_vertex_colors_into_albedo(
    mesh_path: Path,
    albedo_path: Path,
    output_path: Path,
    resolution: int | None = None,
    mesh_file_id: str | None = None,
) -> BakeResult:
    """
    Bake vertex colours from a mesh into an albedo texture.

    The vertex colours are rasterised onto UV space and multiplied
    into the albedo texture.  If the mesh has no vertex colours or
    they are all white, the albedo is left unchanged.

    Args:
        mesh_path: Path to the mesh file (OBJ, PLY, GLB, etc.)
        albedo_path: Path to the albedo texture to multiply into.
        output_path: Where to write the resulting texture.
        resolution: Resolution for the vertex colour raster.
            Defaults to the albedo texture's width.

    Returns:
        BakeResult with outcome details.
    """
    result = BakeResult(mesh_path=mesh_path)

    data = _load_mesh_vertex_data(mesh_path, mesh_file_id=mesh_file_id)
    if data is None:
        result.has_vertex_colors = False
        return result

    vertices, faces, uv_coords, vertex_colors = data
    result.has_vertex_colors = True

    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        result.error = "Pillow or numpy not installed"
        return result

    try:
        albedo = Image.open(albedo_path).convert("RGB")
    except Exception as exc:
        result.error = f"Failed to open albedo: {exc}"
        return result

    res = resolution or albedo.width
    vc_texture = _rasterise_vertex_colors(faces, uv_coords, vertex_colors, res)

    # Resize VC texture to match albedo
    vc_img = Image.fromarray(vc_texture[..., :3])  # drop alpha, use RGB
    if vc_img.size != albedo.size:
        vc_img = vc_img.resize(albedo.size, Image.LANCZOS)

    # Multiply: result = albedo * (vc / 255)
    alb_arr = np.array(albedo, dtype=np.float32)
    vc_arr = np.array(vc_img, dtype=np.float32) / 255.0
    baked = np.clip(alb_arr * vc_arr, 0, 255).astype(np.uint8)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(baked).save(output_path, "PNG")
    result.output_path = output_path
    result.baked = True
    return result


def bake_vertex_colors_standalone(
    mesh_path: Path,
    output_path: Path,
    resolution: int = 512,
) -> BakeResult:
    """Bake vertex colours from a mesh into a standalone texture (no albedo needed).

    For meshes that use only vertex colors (Unity VCOL material), this outputs
    the vertex colors directly as a PNG texture.
    """
    result = BakeResult(mesh_path=mesh_path)

    data = _load_mesh_vertex_data(mesh_path)
    if data is None:
        result.has_vertex_colors = False
        return result

    _vertices, faces, uv_coords, vertex_colors = data
    result.has_vertex_colors = True

    try:
        from PIL import Image
    except ImportError:
        result.error = "Pillow not installed"
        return result

    vc_texture = _rasterise_vertex_colors(faces, uv_coords, vertex_colors, resolution)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(vc_texture[..., :3]).save(output_path, "PNG")
    result.output_path = output_path
    result.baked = True
    return result


def bake_vertex_colors_batch(
    mesh_albedo_pairs: list[tuple[Path, Path]] | list[tuple[Path, Path, str | None]],
    output_dir: Path,
    resolution: int | None = None,
) -> VertexColorBakeResult:
    """
    Batch-bake vertex colours for multiple mesh/albedo pairs.

    Args:
        mesh_albedo_pairs: List of (mesh_path, albedo_path) or
            (mesh_path, albedo_path, mesh_file_id) tuples. The 3-tuple
            form (Phase 5.7) selects a specific FBX sub-mesh; the output
            filename is keyed by ``mesh_file_id`` so different sub-meshes
            of the same FBX bake to distinct textures rather than
            overwriting each other.
        output_dir: Directory for output textures.
        resolution: Optional resolution override.

    Returns:
        VertexColorBakeResult with per-mesh outcomes.
    """
    result = VertexColorBakeResult()

    for entry_tuple in mesh_albedo_pairs:
        if len(entry_tuple) == 3:
            mesh_path, albedo_path, mesh_file_id = entry_tuple
        else:
            mesh_path, albedo_path = entry_tuple
            mesh_file_id = None

        result.total += 1
        if mesh_file_id:
            out_name = f"{mesh_path.stem}_{mesh_file_id}_vc_baked.png"
        else:
            out_name = f"{mesh_path.stem}_vc_baked.png"
        out_path = output_dir / out_name

        entry = bake_vertex_colors_into_albedo(
            mesh_path, albedo_path, out_path, resolution,
            mesh_file_id=mesh_file_id,
        )
        result.entries.append(entry)

        if entry.baked:
            result.baked += 1
        elif not entry.has_vertex_colors:
            result.no_colors += 1
        else:
            result.skipped += 1
            if entry.error:
                result.warnings.append(f"{mesh_path.name}: {entry.error}")

    return result
