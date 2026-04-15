"""
mesh_splitter.py — Split multi-material Unity meshes into per-material submeshes.

Roblox allows only one material per MeshPart. Unity meshes commonly have
multiple submeshes with different materials. This module splits such meshes
into separate files so each can carry its own SurfaceAppearance.

No other module is imported here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class SplitMeshEntry:
    """One submesh from a multi-material mesh."""
    output_path: Path
    material_index: int  # index into the MeshRenderer.m_Materials array
    face_count: int = 0


@dataclass
class MeshSplitResult:
    """Result of splitting a mesh file."""
    source_path: Path
    submeshes: list[SplitMeshEntry] = field(default_factory=list)
    was_split: bool = False
    error: str = ""


def _load_scene(mesh_path: Path):
    """Load a mesh as a trimesh Scene to preserve material groups."""
    import trimesh  # type: ignore

    try:
        scene = trimesh.load(str(mesh_path), process=False)
    except NotImplementedError:
        # FBX not natively supported — try assimp CLI conversion
        scene = _load_fbx_as_scene(mesh_path)
    return scene


def _load_fbx_as_scene(mesh_path: Path):
    """Convert FBX to OBJ via assimp CLI, then load as trimesh Scene."""
    import shutil
    import subprocess
    import tempfile
    import trimesh  # type: ignore

    assimp_cli = shutil.which("assimp")
    if not assimp_cli:
        return None

    with tempfile.NamedTemporaryFile(suffix=".obj", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [assimp_cli, "export", str(mesh_path), tmp_path],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        return trimesh.load(tmp_path, process=False)
    except Exception:
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        Path(tmp_path.replace(".obj", ".mtl")).unlink(missing_ok=True)


def split_mesh(
    mesh_path: Path,
    material_count: int,
    output_dir: Path,
) -> MeshSplitResult:
    """Split a mesh file into per-material submeshes.

    If the mesh has only one geometry group or material_count <= 1,
    no splitting is performed.

    Args:
        mesh_path: Path to source mesh file (FBX, OBJ, etc.)
        material_count: Number of materials from the MeshRenderer
        output_dir: Directory to write split submesh files

    Returns:
        MeshSplitResult with submesh entries (empty if no split needed).
    """
    result = MeshSplitResult(source_path=mesh_path)

    if material_count <= 1:
        return result

    try:
        import trimesh  # type: ignore
    except ImportError:
        result.error = "trimesh not installed"
        return result

    try:
        scene = _load_scene(mesh_path)
    except Exception as exc:
        result.error = f"Failed to load mesh: {exc}"
        return result

    if scene is None:
        result.error = "Failed to load mesh (assimp unavailable)"
        return result

    # Extract geometry groups from the scene
    geometries = _extract_geometries(scene)

    if len(geometries) <= 1:
        # Single geometry — no splitting possible
        return result

    # Limit to the number of materials available
    geometries = geometries[:material_count]

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = mesh_path.stem

    for i, geom in enumerate(geometries):
        out_path = output_dir / f"{stem}_sub{i}.obj"
        try:
            geom.export(str(out_path))
        except Exception as exc:
            logger.warning("Failed to export submesh %d of %s: %s", i, mesh_path.name, exc)
            continue

        result.submeshes.append(SplitMeshEntry(
            output_path=out_path,
            material_index=i,
            face_count=len(geom.faces),
        ))

    if len(result.submeshes) > 1:
        result.was_split = True
        logger.info(
            "Split %s into %d submeshes (%s)",
            mesh_path.name, len(result.submeshes),
            ", ".join(f"{s.face_count} faces" for s in result.submeshes),
        )

    return result


def _extract_geometries(scene) -> list:
    """Extract individual mesh geometries from a trimesh Scene or Trimesh."""
    import trimesh  # type: ignore

    if isinstance(scene, trimesh.Trimesh):
        try:
            components = scene.split(only_watertight=False)
            if len(components) > 1:
                return components
        except Exception:
            pass
        return [scene]

    if isinstance(scene, trimesh.Scene):
        geoms = []
        for name in scene.geometry:
            g = scene.geometry[name]
            if isinstance(g, trimesh.Trimesh) and len(g.faces) > 0:
                geoms.append(g)
        return geoms if geoms else []

    return []
