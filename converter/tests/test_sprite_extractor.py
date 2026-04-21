"""
test_sprite_extractor.py -- Unit tests for sprite extraction from Unity spritesheets.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from converter.sprite_extractor import (
    SpriteExtractionResult,
    SpriteRect,
    SpriteSheetInfo,
    _parse_sprite_entries,
    _slice_sprite,
    parse_spritesheet_meta,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MULTI_SPRITE_META = """\
fileFormatVersion: 2
guid: abcdef1234567890abcdef1234567890
TextureImporter:
  spriteMode: 2
  spriteSheet:
    sprites:
      - serializedVersion: 2
        name: idle_0
        rect:
          serializedVersion: 2
          x: 0
          y: 64
          width: 32
          height: 32
        pivot: {x: 0.5, y: 0.5}
      - serializedVersion: 2
        name: idle_1
        rect:
          serializedVersion: 2
          x: 32
          y: 64
          width: 32
          height: 32
        pivot: {x: 0.5, y: 0.5}
"""

SINGLE_SPRITE_META = """\
fileFormatVersion: 2
guid: 11111111222222223333333344444444
TextureImporter:
  spriteMode: 1
  spriteSheet:
    sprites: []
"""

NO_SPRITE_META = """\
fileFormatVersion: 2
guid: aaaabbbbccccddddeeeeffffaaaabbbb
TextureImporter:
  spriteMode: 0
"""

NO_GUID_META = """\
fileFormatVersion: 2
TextureImporter:
  spriteMode: 2
"""


# ---------------------------------------------------------------------------
# parse_spritesheet_meta
# ---------------------------------------------------------------------------


class TestParseSpriteSheetMeta:
    """Test .meta file parsing for spritesheet data."""

    def test_multiple_sprites(self, tmp_path):
        meta = tmp_path / "sheet.png.meta"
        meta.write_text(MULTI_SPRITE_META, encoding="utf-8")
        # Create the texture file so texture_path is valid
        (tmp_path / "sheet.png").write_bytes(b"")
        result = parse_spritesheet_meta(meta)
        assert result is not None
        assert result.sprite_mode == 2
        assert len(result.sprites) == 2
        assert result.sprites[0].name == "idle_0"
        assert result.sprites[1].name == "idle_1"
        assert result.texture_guid == "abcdef1234567890abcdef1234567890"

    def test_single_sprite(self, tmp_path):
        meta = tmp_path / "icon.png.meta"
        meta.write_text(SINGLE_SPRITE_META, encoding="utf-8")
        (tmp_path / "icon.png").write_bytes(b"")
        result = parse_spritesheet_meta(meta)
        assert result is not None
        assert result.sprite_mode == 1
        assert len(result.sprites) == 1
        assert result.sprites[0].name == "icon"

    def test_no_sprite_mode(self, tmp_path):
        meta = tmp_path / "texture.png.meta"
        meta.write_text(NO_SPRITE_META, encoding="utf-8")
        result = parse_spritesheet_meta(meta)
        assert result is None

    def test_no_guid(self, tmp_path):
        meta = tmp_path / "noguid.png.meta"
        meta.write_text(NO_GUID_META, encoding="utf-8")
        result = parse_spritesheet_meta(meta)
        assert result is None

    def test_nonexistent_file(self, tmp_path):
        result = parse_spritesheet_meta(tmp_path / "nope.meta")
        assert result is None


# ---------------------------------------------------------------------------
# _parse_sprite_entries
# ---------------------------------------------------------------------------


class TestParseSpriteEntries:
    """Test sprite entry parsing from spriteSheet section."""

    def test_parses_entries(self):
        entries = _parse_sprite_entries(MULTI_SPRITE_META)
        assert len(entries) == 2
        assert entries[0].name == "idle_0"
        assert entries[0].x == 0
        assert entries[0].y == 64
        assert entries[0].width == 32
        assert entries[0].height == 32
        assert entries[1].name == "idle_1"
        assert entries[1].x == 32

    def test_pivot_parsed(self):
        entries = _parse_sprite_entries(MULTI_SPRITE_META)
        assert entries[0].pivot_x == 0.5
        assert entries[0].pivot_y == 0.5

    def test_no_spritesheet_section(self):
        entries = _parse_sprite_entries("some random text")
        assert entries == []


# ---------------------------------------------------------------------------
# SpriteRect
# ---------------------------------------------------------------------------


class TestSpriteRect:
    """Test SpriteRect dataclass."""

    def test_frozen(self):
        sr = SpriteRect(name="test", x=0, y=0, width=10, height=10)
        with pytest.raises(AttributeError):
            sr.x = 5  # type: ignore[misc]

    def test_default_pivot(self):
        sr = SpriteRect(name="test", x=0, y=0, width=10, height=10)
        assert sr.pivot_x == 0.5
        assert sr.pivot_y == 0.5


# ---------------------------------------------------------------------------
# _slice_sprite (with Pillow mock)
# ---------------------------------------------------------------------------


class TestSliceSprite:
    """Test coordinate conversion in sprite slicing."""

    def _mock_image(self, width=128, height=128):
        img = MagicMock()
        img.size = (width, height)
        img.crop.return_value = MagicMock()
        img.copy.return_value = MagicMock()
        return img

    def test_full_image_for_zero_size(self):
        img = self._mock_image()
        sprite = SpriteRect(name="full", x=0, y=0, width=0, height=0)
        _slice_sprite(img, sprite)
        img.copy.assert_called_once()

    def test_coordinate_conversion(self):
        img = self._mock_image(width=128, height=128)
        # Unity: bottom-left origin, y=64 means 64px from bottom
        sprite = SpriteRect(name="test", x=0, y=64, width=32, height=32)
        _slice_sprite(img, sprite)
        # Pillow: top-left origin
        # upper = img_h - y - h = 128 - 64 - 32 = 32
        img.crop.assert_called_once_with((0, 32, 32, 64))

    def test_clamped_to_image_bounds(self):
        img = self._mock_image(width=64, height=64)
        sprite = SpriteRect(name="oob", x=50, y=0, width=32, height=32)
        _slice_sprite(img, sprite)
        # left=50, upper=64-0-32=32, right=min(64,82)=64, bottom=min(64,64)=64
        img.crop.assert_called_once_with((50, 32, 64, 64))


# ---------------------------------------------------------------------------
# SpriteExtractionResult
# ---------------------------------------------------------------------------


class TestSpriteExtractionResult:
    def test_defaults(self):
        r = SpriteExtractionResult(extracted=[], sprite_guid_to_file={})
        assert r.total_spritesheets == 0
        assert r.total_sprites_extracted == 0
        assert r.warnings == []
