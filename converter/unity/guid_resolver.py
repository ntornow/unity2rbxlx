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


# ---------------------------------------------------------------------------
# Sprite atlas rect parsing
# ---------------------------------------------------------------------------

# Regex patterns to extract sprite rects from .meta TextureImporter sections.
# Each sprite entry has an internalID and rect {x, y, width, height}.
_RE_INTERNAL_ID = re.compile(r"^\s*internalID:\s*(\d+)\s*$", re.MULTILINE)
_RE_RECT_FIELD = re.compile(
    r"^\s*rect:\s*$\n"
    r"(?:\s*serializedVersion:\s*\d+\s*$\n)?"
    r"\s*x:\s*([\d.eE+-]+)\s*$\n"
    r"\s*y:\s*([\d.eE+-]+)\s*$\n"
    r"\s*width:\s*([\d.eE+-]+)\s*$\n"
    r"\s*height:\s*([\d.eE+-]+)\s*$",
    re.MULTILINE,
)


def parse_sprite_rects(meta_path: Path) -> dict[str, tuple[float, float, float, float]]:
    """Parse sprite rects from a texture's .meta file.

    Returns a dict mapping internalID (as string) to (x, y, width, height).
    The internalID corresponds to the fileID in m_Sprite references.
    Returns empty dict if the file has no sprite sheet data.
    """
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    # Find the spriteSheet section
    sheet_idx = text.find("spriteSheet:")
    if sheet_idx < 0:
        return {}

    # Find the sprites list within the spriteSheet section
    sprites_idx = text.find("sprites:", sheet_idx)
    if sprites_idx < 0:
        return {}

    # Extract sprite entries -- each starts with "- serializedVersion:" or "- name:"
    # and contains rect and internalID fields
    result: dict[str, tuple[float, float, float, float]] = {}

    # Split into individual sprite blocks (delimited by "    - " at sprite level)
    # Find the end of the sprites list (next top-level key at same or less indent)
    sprite_section = text[sprites_idx:]
    # The sprites list ends at the next key at spriteSheet indent level
    # (outline:, customData:, etc.)
    lines = sprite_section.split("\n")
    sprite_blocks: list[str] = []
    current_block: list[str] = []
    in_sprites = False

    for i, line in enumerate(lines):
        if i == 0:
            # "sprites:" header line -- check for empty list "sprites: []"
            if "[]" in line:
                return {}
            in_sprites = True
            continue
        if not in_sprites:
            continue
        # Detect end of sprites list: a line at same or lesser indent that isn't
        # part of a sprite entry (not starting with spaces + -)
        stripped = line.rstrip()
        if stripped and not stripped.startswith(" ") and not stripped.startswith("-"):
            break
        # New sprite entry starts with "    -" (list item marker)
        if re.match(r"^\s{4}-\s", line) or re.match(r"^\s{2}-\s", line):
            if current_block:
                sprite_blocks.append("\n".join(current_block))
            current_block = [line]
        elif current_block:
            current_block.append(line)
    if current_block:
        sprite_blocks.append("\n".join(current_block))

    for block in sprite_blocks:
        # Extract internalID
        id_match = _RE_INTERNAL_ID.search(block)
        if not id_match:
            continue
        internal_id = id_match.group(1)

        # Extract rect
        rect_match = _RE_RECT_FIELD.search(block)
        if not rect_match:
            # Try inline rect format: rect: {x: 0, y: 0, width: 128, height: 128}
            inline_match = re.search(
                r"rect:\s*\{[^}]*x:\s*([\d.eE+-]+)[^}]*y:\s*([\d.eE+-]+)"
                r"[^}]*width:\s*([\d.eE+-]+)[^}]*height:\s*([\d.eE+-]+)",
                block,
            )
            if inline_match:
                x, y, w, h = (float(inline_match.group(i)) for i in range(1, 5))
                result[internal_id] = (x, y, w, h)
            continue

        x = float(rect_match.group(1))
        y = float(rect_match.group(2))
        w = float(rect_match.group(3))
        h = float(rect_match.group(4))
        result[internal_id] = (x, y, w, h)

    return result
