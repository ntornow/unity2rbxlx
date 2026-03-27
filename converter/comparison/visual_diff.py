"""
Visual comparison utilities for Unity and Roblox screenshots.

Uses PIL/Pillow for image I/O and scikit-image for the Structural Similarity
Index (SSIM) metric.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def _load_grayscale(image_path: str | Path) -> np.ndarray:
    """Load an image as a grayscale NumPy array in ``[0, 1]``."""
    img = Image.open(image_path).convert("L")
    return np.asarray(img, dtype=np.float64) / 255.0


def _load_rgb(image_path: str | Path) -> np.ndarray:
    """Load an image as an RGB NumPy array in ``[0, 255]`` uint8."""
    return np.asarray(Image.open(image_path).convert("RGB"))


def _ensure_same_size(
    arr_a: np.ndarray, arr_b: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """Resize *arr_b* to match *arr_a* if their shapes differ."""
    if arr_a.shape == arr_b.shape:
        return arr_a, arr_b

    h, w = arr_a.shape[:2]
    img_b = Image.fromarray(arr_b)
    img_b = img_b.resize((w, h), Image.Resampling.LANCZOS)
    return arr_a, np.asarray(img_b)


def crop_viewport(
    image_path: str | Path,
    output_path: str | Path | None = None,
    margin_pct: float = 0.1,
) -> Path:
    """Crop an editor screenshot to just the viewport area.

    Removes toolbars, panels, and status bars by cropping to the center
    of the image with configurable margins.

    Args:
        image_path: Path to the full editor screenshot.
        output_path: Where to save the cropped image. Defaults to
            ``<stem>_viewport.png`` next to the original.
        margin_pct: Percentage of image to remove from each edge (0.0-0.5).

    Returns:
        Path to the cropped viewport image.
    """
    img = Image.open(image_path)
    w, h = img.size
    left = int(w * margin_pct)
    top = int(h * margin_pct)
    right = int(w * (1 - margin_pct))
    bottom = int(h * (1 - margin_pct))
    cropped = img.crop((left, top, right, bottom))

    if output_path is None:
        p = Path(image_path)
        output_path = p.parent / f"{p.stem}_viewport{p.suffix}"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path)
    return output_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_ssim(image_a_path: str | Path, image_b_path: str | Path) -> float:
    """Compute the Structural Similarity Index (SSIM) between two images.

    Both images are converted to grayscale before comparison.  If the images
    differ in size, the second image is resized to match the first.

    Args:
        image_a_path: Path to the first image (reference).
        image_b_path: Path to the second image (test).

    Returns:
        SSIM score in the range ``[0.0, 1.0]`` where ``1.0`` means identical.
    """
    gray_a = _load_grayscale(image_a_path)
    gray_b = _load_grayscale(image_b_path)
    gray_a, gray_b = _ensure_same_size(gray_a, gray_b)

    score: float = structural_similarity(gray_a, gray_b, data_range=1.0)
    logger.info("SSIM between %s and %s: %.4f", image_a_path, image_b_path, score)
    return score


def generate_diff_heatmap(
    image_a_path: str | Path,
    image_b_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Create and save a visual diff heatmap highlighting pixel differences.

    The heatmap is a colour-mapped image where brighter areas indicate larger
    per-pixel differences.

    Args:
        image_a_path: Path to the first image (reference).
        image_b_path: Path to the second image (test).
        output_path: Destination path for the heatmap PNG.

    Returns:
        The resolved *output_path*.
    """
    rgb_a = _load_rgb(image_a_path)
    rgb_b = _load_rgb(image_b_path)
    rgb_a, rgb_b = _ensure_same_size(rgb_a, rgb_b)

    # Compute per-pixel absolute difference and take the max across channels
    diff = np.abs(rgb_a.astype(np.float64) - rgb_b.astype(np.float64))
    diff_gray = np.max(diff, axis=2)  # shape (H, W)

    # Normalise to [0, 255]
    max_val = diff_gray.max()
    if max_val > 0:
        diff_norm = (diff_gray / max_val * 255).astype(np.uint8)
    else:
        diff_norm = diff_gray.astype(np.uint8)

    # Apply a simple red-channel heatmap: black -> red -> yellow -> white
    h, w = diff_norm.shape
    heatmap = np.zeros((h, w, 3), dtype=np.uint8)
    heatmap[..., 0] = np.clip(diff_norm * 2, 0, 255).astype(np.uint8)
    heatmap[..., 1] = np.clip((diff_norm.astype(np.int16) - 128) * 2, 0, 255).astype(np.uint8)
    heatmap[..., 2] = np.clip((diff_norm.astype(np.int16) - 192) * 4, 0, 255).astype(np.uint8)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(heatmap).save(output_path)

    logger.info("Diff heatmap saved to %s", output_path)
    return output_path


def compute_pixel_diff(
    image_a_path: str | Path,
    image_b_path: str | Path,
    heatmap_output: str | Path | None = None,
) -> Tuple[float, Path]:
    """Compute the percentage of differing pixels and produce a heatmap.

    A pixel is considered *different* if any channel differs by more than a
    small tolerance (5 / 255).

    Args:
        image_a_path: Path to the first image (reference).
        image_b_path: Path to the second image (test).
        heatmap_output: Optional path for the heatmap image.  Defaults to
            ``<image_a_stem>_vs_<image_b_stem>_diff.png`` in the same
            directory as *image_a_path*.

    Returns:
        A tuple of ``(percent_different, heatmap_path)`` where
        *percent_different* is in ``[0.0, 100.0]``.
    """
    rgb_a = _load_rgb(image_a_path)
    rgb_b = _load_rgb(image_b_path)
    rgb_a, rgb_b = _ensure_same_size(rgb_a, rgb_b)

    tolerance = 5  # out of 255
    diff = np.abs(rgb_a.astype(np.int16) - rgb_b.astype(np.int16))
    differs = np.any(diff > tolerance, axis=2)
    percent_different = float(np.mean(differs) * 100.0)

    if heatmap_output is None:
        stem_a = Path(image_a_path).stem
        stem_b = Path(image_b_path).stem
        heatmap_output = Path(image_a_path).parent / f"{stem_a}_vs_{stem_b}_diff.png"

    heatmap_path = generate_diff_heatmap(image_a_path, image_b_path, heatmap_output)

    logger.info(
        "Pixel diff between %s and %s: %.2f%% different",
        image_a_path,
        image_b_path,
        percent_different,
    )
    return percent_different, heatmap_path
