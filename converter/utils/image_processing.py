"""
Texture processing utilities for material conversion.

Provides channel extraction, inversion, resizing, tiling, and solid-colour
texture generation using PIL/Pillow.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple, Union

from PIL import Image

logger = logging.getLogger(__name__)

# Type alias for a colour specified as an (R, G, B) or (R, G, B, A) tuple.
Color = Union[Tuple[int, int, int], Tuple[int, int, int, int]]


def _is_lfs_pointer(path: Path) -> bool:
    """Check if a file is a Git LFS pointer (small text file, not actual data)."""
    try:
        if path.stat().st_size > 200:
            return False
        with open(path, "rb") as f:
            header = f.read(50)
        return b"git-lfs" in header or b"version https://git-lfs" in header
    except OSError:
        return False


def extract_channel(
    image_path: str | Path,
    channel: str,
    output_path: str | Path,
) -> Path:
    """Extract a single colour channel from an image and save as grayscale.

    Args:
        image_path: Path to the source image.
        channel: One of ``"R"``, ``"G"``, ``"B"``, or ``"A"`` (case-insensitive).
        output_path: Destination path for the grayscale PNG.

    Returns:
        The resolved *output_path*.

    Raises:
        ValueError: If *channel* is not one of R, G, B, A.
    """
    channel = channel.upper()
    if channel not in ("R", "G", "B", "A"):
        raise ValueError(f"Invalid channel '{channel}'; expected R, G, B, or A")

    image_path = Path(image_path)
    if _is_lfs_pointer(image_path):
        raise OSError(f"Git LFS pointer (not actual image data): {image_path.name}")

    img = Image.open(image_path)

    # Ensure the image has the required channel
    if channel == "A":
        img = img.convert("RGBA")
    else:
        img = img.convert("RGBA" if img.mode == "RGBA" else "RGB")

    bands = img.split()
    channel_index = {"R": 0, "G": 1, "B": 2, "A": 3}[channel]
    grayscale = bands[channel_index]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    grayscale.save(output_path)

    logger.info("Extracted channel %s from %s -> %s", channel, image_path, output_path)
    return output_path


def invert_image(image_path: str | Path, output_path: str | Path) -> Path:
    """Invert the pixel values of an image.

    Operates on all colour channels.  An alpha channel, if present, is
    preserved without inversion.

    Args:
        image_path: Path to the source image.
        output_path: Destination path for the inverted image.

    Returns:
        The resolved *output_path*.
    """
    image_path = Path(image_path)
    if _is_lfs_pointer(image_path):
        raise OSError(f"Git LFS pointer (not actual image data): {image_path.name}")

    img = Image.open(image_path)
    has_alpha = img.mode in ("RGBA", "LA", "PA")

    if has_alpha:
        img = img.convert("RGBA")
        r, g, b, a = img.split()
        from PIL import ImageOps
        rgb = Image.merge("RGB", (r, g, b))
        rgb_inv = ImageOps.invert(rgb)
        ri, gi, bi = rgb_inv.split()
        result = Image.merge("RGBA", (ri, gi, bi, a))
    else:
        from PIL import ImageOps
        img = img.convert("RGB")
        result = ImageOps.invert(img)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path)

    logger.info("Inverted %s -> %s", image_path, output_path)
    return output_path


def convert_to_png(
    image_path: str | Path,
    output_path: str | Path | None = None,
) -> Path:
    """Convert any image (BMP, TGA, TIFF, etc.) to PNG format.

    Args:
        image_path: Path to the source image.
        output_path: Where to write the PNG. If None, writes next to source with .png extension.

    Returns:
        Path to the converted PNG file.
    """
    image_path = Path(image_path)
    if _is_lfs_pointer(image_path):
        raise OSError(f"Git LFS pointer (not actual image data): {image_path.name}")

    if output_path is None:
        output_path = image_path.with_suffix(".png")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Skip if already converted and newer than source
    if output_path.exists() and output_path.stat().st_mtime >= image_path.stat().st_mtime:
        logger.debug("Reusing cached PNG: %s", output_path.name)
        return output_path

    img = Image.open(image_path)
    if img.mode == "RGBA":
        img.save(output_path, "PNG")
    else:
        img.convert("RGB").save(output_path, "PNG")

    logger.info("Converted %s -> %s (%.0f KB -> %.0f KB)",
                image_path.name, output_path.name,
                image_path.stat().st_size / 1024,
                output_path.stat().st_size / 1024)
    return output_path


def resize_image(
    image_path: str | Path,
    max_size: int | Tuple[int, int],
    output_path: str | Path,
) -> Path:
    """Resize an image so that it fits within *max_size*, preserving aspect ratio.

    Args:
        image_path: Path to the source image.
        max_size: Maximum dimensions.  If an ``int``, used for both width and
            height.  If a tuple, ``(max_width, max_height)``.
        output_path: Destination path for the resized image.

    Returns:
        The resolved *output_path*.
    """
    if isinstance(max_size, int):
        max_size = (max_size, max_size)

    img = Image.open(image_path)
    img.thumbnail(max_size, Image.Resampling.LANCZOS)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)

    logger.info("Resized %s to %s -> %s", image_path, img.size, output_path)
    return output_path


def pre_tile_texture(
    image_path: str | Path,
    tile_x: int,
    tile_y: int,
    output_path: str | Path,
) -> Path:
    """Tile a texture by repeating it in a grid.

    The output image will be ``tile_x * width`` by ``tile_y * height`` pixels.

    Args:
        image_path: Path to the source texture.
        tile_x: Number of horizontal repetitions.
        tile_y: Number of vertical repetitions.
        output_path: Destination path for the tiled image.

    Returns:
        The resolved *output_path*.
    """
    if tile_x < 1 or tile_y < 1:
        raise ValueError(f"Tile counts must be >= 1, got tile_x={tile_x}, tile_y={tile_y}")

    tile = Image.open(image_path)
    tw, th = tile.size
    result = Image.new(tile.mode, (tw * tile_x, th * tile_y))

    for ix in range(tile_x):
        for iy in range(tile_y):
            result.paste(tile, (ix * tw, iy * th))

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path)

    logger.info(
        "Tiled %s (%dx%d) -> %s (%dx%d)",
        image_path, tw, th, output_path, tw * tile_x, th * tile_y,
    )
    return output_path


def generate_uniform_texture(
    color: Color,
    size: Tuple[int, int] = (4, 4),
    output_path: str | Path = "uniform.png",
) -> Path:
    """Create a solid-colour PNG texture.

    Args:
        color: ``(R, G, B)`` or ``(R, G, B, A)`` colour value with each
            component in ``[0, 255]``.
        size: Width and height of the texture in pixels.  Defaults to 4x4.
        output_path: Destination path for the image.

    Returns:
        The resolved *output_path*.
    """
    mode = "RGBA" if len(color) == 4 else "RGB"
    img = Image.new(mode, size, color)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path)

    logger.info("Generated %s %s texture -> %s", size, color, output_path)
    return output_path
