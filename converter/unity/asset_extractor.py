"""
asset_extractor.py -- Discovers and catalogs all assets in a Unity project.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from config import ASSET_EXT_TO_KIND
from core.unity_types import AssetEntry, AssetManifest, GuidIndex

log = logging.getLogger(__name__)


def _hash_file(path: Path, chunk_size: int = 8192) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def extract_assets(
    unity_project_path: str | Path,
    guid_index: GuidIndex | None = None,
    compute_hashes: bool = False,
) -> AssetManifest:
    """Walk Assets/ and build a complete asset manifest.

    Args:
        unity_project_path: Root of the Unity project.
        guid_index: Optional GUID index for GUID lookups.
        compute_hashes: Whether to compute SHA-256 hashes (slower).

    Returns:
        AssetManifest with all discovered assets.
    """
    project = Path(unity_project_path).resolve()
    assets_dir = project / "Assets"
    if not assets_dir.exists():
        log.warning("Assets/ directory not found in %s", project)
        return AssetManifest(project_root=project)

    manifest = AssetManifest(project_root=project)

    # Scan both Assets/ and Packages/ directories
    scan_dirs = [assets_dir]
    packages_dir = project / "Packages"
    if packages_dir.exists():
        scan_dirs.append(packages_dir)

    for scan_dir in scan_dirs:
      for path in sorted(scan_dir.rglob("*")):
        if path.is_dir():
            continue
        if path.suffix == ".meta":
            continue

        ext = path.suffix.lower()
        kind = ASSET_EXT_TO_KIND.get(ext)
        if kind is None:
            continue

        relative = path.relative_to(project)
        size = path.stat().st_size

        # Skip Git LFS pointer files (they're not actual asset data)
        if size < 200 and ext in (".fbx", ".obj", ".mp3", ".ogg", ".wav"):
            try:
                header = path.read_bytes()[:30]
                if header.startswith(b"version https://git-lfs"):
                    log.debug("Skipping LFS pointer: %s", relative)
                    continue
            except OSError:
                pass

        guid = None
        if guid_index:
            guid = guid_index.path_to_guid.get(path.resolve())

        file_hash = None
        if compute_hashes:
            try:
                file_hash = _hash_file(path)
            except OSError:
                pass

        entry = AssetEntry(
            path=path.resolve(),
            relative_path=relative,
            kind=kind,
            guid=guid,
            size_bytes=size,
            hash=file_hash,
        )
        manifest.assets.append(entry)
        manifest.by_kind.setdefault(kind, []).append(entry)
        if guid:
            manifest.by_guid[guid] = entry
        manifest.total_size_bytes += size

    log.info("Asset manifest: %d assets, %.1f MB total",
             len(manifest.assets), manifest.total_size_bytes / (1024 * 1024))

    return manifest
