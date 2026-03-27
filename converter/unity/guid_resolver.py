"""
guid_resolver.py -- Builds a complete GUID <-> path index for a Unity project.

Scans .meta files to extract GUIDs and map them to asset paths.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from config import ASSET_EXT_TO_KIND
from core.unity_types import GuidEntry, GuidIndex

log = logging.getLogger(__name__)

_RE_GUID_LINE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)
_RE_FOLDER_TYPE = re.compile(r"^folderAsset:\s*yes", re.MULTILINE)


def _parse_meta_file(meta_path: Path) -> tuple[str, bool] | None:
    """Parse a .meta file to extract (guid, is_folder) or None."""
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    m = _RE_GUID_LINE.search(text)
    if not m:
        return None

    guid = m.group(1).lower()
    is_folder = bool(_RE_FOLDER_TYPE.search(text))
    return (guid, is_folder)


def _classify_asset(path: Path) -> str:
    """Classify an asset by its file extension."""
    ext = path.suffix.lower()
    return ASSET_EXT_TO_KIND.get(ext, "unknown")


def build_guid_index(unity_project_path: str | Path) -> GuidIndex:
    """Build a complete GUID index for a Unity project.

    Scans Assets/, Packages/ (if exists), and Library/PackageCache/ (if exists).
    """
    project = Path(unity_project_path).resolve()
    index = GuidIndex(project_root=project)

    scan_dirs = [project / "Assets"]
    for extra in ("Packages", "Library/PackageCache"):
        d = project / extra
        if d.exists():
            scan_dirs.append(d)

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue

        for meta_path in scan_dir.rglob("*.meta"):
            index.total_meta_files += 1

            result = _parse_meta_file(meta_path)
            if result is None:
                index.parse_errors.append(f"Failed to parse: {meta_path}")
                continue

            guid, is_folder = result
            asset_path = meta_path.with_suffix("")  # remove .meta

            if not asset_path.exists():
                index.orphan_metas.append(meta_path)
                # Still index orphan metas for reference resolution
                if is_folder:
                    continue

            # Check for duplicate GUIDs
            if guid in index.guid_to_entry:
                existing = index.guid_to_entry[guid]
                if guid not in index.duplicate_guids:
                    index.duplicate_guids[guid] = [existing.asset_path]
                index.duplicate_guids[guid].append(asset_path)
                continue

            try:
                relative_path = asset_path.relative_to(project)
            except ValueError:
                relative_path = asset_path

            kind = "directory" if is_folder else _classify_asset(asset_path)

            entry = GuidEntry(
                guid=guid,
                asset_path=asset_path.resolve(),
                relative_path=relative_path,
                kind=kind,
                is_directory=is_folder,
            )
            index.guid_to_entry[guid] = entry
            index.path_to_guid[asset_path.resolve()] = guid

    log.info("GUID index: %d entries, %d duplicates, %d orphans, %d errors",
             index.total_resolved, len(index.duplicate_guids),
             len(index.orphan_metas), len(index.parse_errors))

    return index
