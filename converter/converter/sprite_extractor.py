"""
sprite_extractor.py — Extract individual sprites from Unity spritesheets.

Parses TextureImporter metadata from .meta files, slices individual sprites
from source textures using Pillow, and writes them to an output directory.

No other module is imported here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpriteRect:
    """A single sprite rect within a spritesheet (Unity bottom-left origin)."""
    name: str
    x: float
    y: float
    width: float
    height: float
    pivot_x: float = 0.5
    pivot_y: float = 0.5


@dataclass
class SpriteSheetInfo:
    texture_guid: str
    texture_path: Path
    sprite_mode: int  # 0=None, 1=Single, 2=Multiple
    sprites: list[SpriteRect] = field(default_factory=list)


@dataclass
class SpriteExtractionResult:
    extracted: list[tuple[str, Path]]
    sprite_guid_to_file: dict[str, Path]
    warnings: list[str] = field(default_factory=list)
    total_spritesheets: int = 0
    total_sprites_extracted: int = 0


_RE_SPRITE_MODE = re.compile(r"^\s*spriteMode:\s*(\d+)", re.MULTILINE)


def _parse_float(val: str) -> float:
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def parse_spritesheet_meta(meta_path: Path) -> SpriteSheetInfo | None:
    """Parse a .meta file for spritesheet data. Returns None if not a sprite."""
    try:
        text = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    guid_m = re.search(r"^guid:\s*([0-9a-fA-F]{32})\s*$", text, re.MULTILINE)
    if not guid_m:
        return None
    guid = guid_m.group(1)

    mode_m = _RE_SPRITE_MODE.search(text)
    sprite_mode = int(mode_m.group(1)) if mode_m else 0
    if sprite_mode == 0:
        return None

    texture_path = meta_path.with_suffix("")
    info = SpriteSheetInfo(
        texture_guid=guid, texture_path=texture_path, sprite_mode=sprite_mode,
    )

    if sprite_mode == 1:
        # Single sprite — entire image
        info.sprites.append(SpriteRect(
            name=texture_path.stem, x=0, y=0, width=0, height=0,
        ))
        return info

    info.sprites = _parse_sprite_entries(text)
    return info if info.sprites else None


def _parse_sprite_entries(meta_text: str) -> list[SpriteRect]:
    """Parse sprite rect entries from the spriteSheet section of a .meta file."""
    sprites: list[SpriteRect] = []

    sheet_match = re.search(r"^\s*spriteSheet:", meta_text, re.MULTILINE)
    if not sheet_match:
        return sprites

    sheet_section = meta_text[sheet_match.start():]
    sprites_match = re.search(r"^\s*sprites:", sheet_section, re.MULTILINE)
    if not sprites_match:
        return sprites

    sprites_text = sheet_section[sprites_match.end():]

    # Split into individual entries; stop at next top-level key
    entries: list[str] = []
    current: list[str] = []
    for line in sprites_text.splitlines():
        stripped = line.lstrip()
        if stripped and not line[0].isspace() and not stripped.startswith("-"):
            break
        if re.match(r"^\s+-\s+", line) or re.match(r"^\s+-$", line):
            if current:
                entries.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        entries.append("\n".join(current))

    for entry in entries:
        sprite = _parse_single_sprite(entry)
        if sprite:
            sprites.append(sprite)

    return sprites


def _parse_single_sprite(entry_text: str) -> SpriteRect | None:
    name_m = re.search(r"name:\s*(.+?)$", entry_text, re.MULTILINE)
    if not name_m:
        return None
    name = name_m.group(1).strip()

    rect_section = re.search(
        r"rect:.*?x:\s*([\d.e+-]+).*?y:\s*([\d.e+-]+).*?"
        r"width:\s*([\d.e+-]+).*?height:\s*([\d.e+-]+)",
        entry_text, re.DOTALL,
    )
    if not rect_section:
        return None

    x = _parse_float(rect_section.group(1))
    y = _parse_float(rect_section.group(2))
    w = _parse_float(rect_section.group(3))
    h = _parse_float(rect_section.group(4))

    pivot_x, pivot_y = 0.5, 0.5
    pivot_m = re.search(
        r"pivot:\s*\{?\s*x:\s*([\d.e+-]+)\s*,?\s*y:\s*([\d.e+-]+)",
        entry_text,
    )
    if pivot_m:
        pivot_x = _parse_float(pivot_m.group(1))
        pivot_y = _parse_float(pivot_m.group(2))

    return SpriteRect(name=name, x=x, y=y, width=w, height=h,
                      pivot_x=pivot_x, pivot_y=pivot_y)


def _slice_sprite(img: Any, sprite: SpriteRect) -> Any:
    """Crop a sprite from a spritesheet, converting Unity bottom-left -> Pillow top-left Y."""
    img_w, img_h = img.size

    if sprite.width == 0 and sprite.height == 0:
        return img.copy()

    left = int(sprite.x)
    w = int(sprite.width)
    h = int(sprite.height)
    upper = img_h - int(sprite.y) - h

    box = (
        max(0, left),
        max(0, upper),
        min(img_w, left + w),
        min(img_h, upper + h),
    )
    return img.crop(box)


def extract_sprites(guid_index: Any, output_dir: Path) -> SpriteExtractionResult:
    """Scan texture assets for spritesheets, slice sprites, write to output_dir/sprites/."""
    if Image is None:
        return SpriteExtractionResult(
            extracted=[],
            sprite_guid_to_file={},
            warnings=["Pillow not installed -- sprite extraction skipped"],
        )

    result = SpriteExtractionResult(extracted=[], sprite_guid_to_file={})
    sprites_dir = output_dir / "sprites"
    texture_entries = guid_index.filter_by_kind("texture")

    for guid, entry in texture_entries.items():
        meta_path = Path(str(entry.asset_path) + ".meta")
        if not meta_path.exists():
            continue

        sheet_info = parse_spritesheet_meta(meta_path)
        if not sheet_info or not sheet_info.sprites:
            continue

        if not sheet_info.texture_path.exists():
            result.warnings.append(
                f"Texture file missing for spritesheet: {sheet_info.texture_path}"
            )
            continue

        result.total_spritesheets += 1

        try:
            img = Image.open(sheet_info.texture_path)
        except Exception as exc:
            result.warnings.append(
                f"Failed to open texture {sheet_info.texture_path.name}: {exc}"
            )
            continue

        for sprite_rect in sheet_info.sprites:
            try:
                cropped = _slice_sprite(img, sprite_rect)
            except Exception as exc:
                result.warnings.append(
                    f"Failed to slice sprite '{sprite_rect.name}' "
                    f"from {sheet_info.texture_path.name}: {exc}"
                )
                continue

            safe_name = re.sub(r'[^\w\-.]', '_', sprite_rect.name)
            out_path = sprites_dir / f"{safe_name}.png"
            sprites_dir.mkdir(parents=True, exist_ok=True)
            cropped.save(out_path, "PNG")

            result.extracted.append((sprite_rect.name, out_path))
            result.total_sprites_extracted += 1

            # Single-sprite: GUID maps directly; multi-sprite: "guid:name" compound key
            if sheet_info.sprite_mode == 1:
                result.sprite_guid_to_file[guid] = out_path
            else:
                result.sprite_guid_to_file[f"{guid}:{sprite_rect.name}"] = out_path

        img.close()

    if result.total_sprites_extracted:
        logger.info(
            "Extracted %d sprites from %d spritesheets",
            result.total_sprites_extracted,
            result.total_spritesheets,
        )

    return result
