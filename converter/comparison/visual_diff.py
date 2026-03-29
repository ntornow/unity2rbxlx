"""
Visual comparison utilities for Unity and Roblox screenshots.

Uses PIL/Pillow and NumPy for image I/O and the Structural Similarity
Index (SSIM) metric.  No external dependencies beyond those two.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Image loading helpers
# ---------------------------------------------------------------------------


def _load_grayscale(image_path: str | Path) -> np.ndarray:
    """Load an image as a grayscale float64 array in ``[0, 1]``."""
    img = Image.open(image_path).convert("L")
    return np.asarray(img, dtype=np.float64) / 255.0


def _load_rgb(image_path: str | Path) -> np.ndarray:
    """Load an image as an RGB uint8 array."""
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
    return arr_a, np.asarray(img_b, dtype=arr_a.dtype)


# ---------------------------------------------------------------------------
# Pure-NumPy SSIM implementation
# ---------------------------------------------------------------------------


def _gaussian_kernel_1d(size: int, sigma: float) -> np.ndarray:
    """Create a 1-D Gaussian kernel."""
    coords = np.arange(size, dtype=np.float64) - (size - 1) / 2.0
    g = np.exp(-0.5 * (coords / sigma) ** 2)
    return g / g.sum()


def _gaussian_filter(img: np.ndarray, size: int = 11, sigma: float = 1.5) -> np.ndarray:
    """Apply separable Gaussian filter to a 2-D array.

    Uses reflect-padding to avoid edge artefacts.
    """
    kernel = _gaussian_kernel_1d(size, sigma)
    pad = size // 2

    # Pad and convolve along rows then columns (separable)
    padded = np.pad(img, pad, mode="reflect")
    # Row convolution
    row_conv = np.zeros_like(padded)
    for i in range(size):
        row_conv += kernel[i] * np.roll(padded, i - pad, axis=1)
    # Trim columns that wrapped
    row_conv = row_conv[:, pad:-pad]
    # Re-pad for column convolution
    padded2 = np.pad(row_conv, ((pad, pad), (0, 0)), mode="reflect")
    col_conv = np.zeros_like(padded2)
    for i in range(size):
        col_conv += kernel[i] * np.roll(padded2, i - pad, axis=0)
    return col_conv[pad:-pad, :]


def _ssim_single_channel(
    img_a: np.ndarray,
    img_b: np.ndarray,
    data_range: float = 1.0,
    win_size: int = 7,
    sigma: float = 1.5,
) -> Tuple[float, np.ndarray]:
    """Compute SSIM for two same-sized 2-D float64 arrays.

    Returns (mean_ssim, ssim_map).
    """
    C1 = (0.01 * data_range) ** 2
    C2 = (0.03 * data_range) ** 2

    mu_a = _gaussian_filter(img_a, win_size, sigma)
    mu_b = _gaussian_filter(img_b, win_size, sigma)

    mu_a_sq = mu_a * mu_a
    mu_b_sq = mu_b * mu_b
    mu_ab = mu_a * mu_b

    sigma_a_sq = _gaussian_filter(img_a * img_a, win_size, sigma) - mu_a_sq
    sigma_b_sq = _gaussian_filter(img_b * img_b, win_size, sigma) - mu_b_sq
    sigma_ab = _gaussian_filter(img_a * img_b, win_size, sigma) - mu_ab

    numerator = (2.0 * mu_ab + C1) * (2.0 * sigma_ab + C2)
    denominator = (mu_a_sq + mu_b_sq + C1) * (sigma_a_sq + sigma_b_sq + C2)

    ssim_map = numerator / denominator
    return float(np.mean(ssim_map)), ssim_map


# ---------------------------------------------------------------------------
# Viewport cropping
# ---------------------------------------------------------------------------


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
        output_path: Where to save the cropped image.  Defaults to
            ``<stem>_viewport.png`` next to the original.
        margin_pct: Fraction of image to remove from each edge (0.0 -- 0.5).

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


def compute_ssim(
    image_a_path: str | Path,
    image_b_path: str | Path,
    crop: bool = False,
    crop_margin: float = 0.1,
) -> float:
    """Compute the Structural Similarity Index (SSIM) between two images.

    Both images are converted to grayscale before comparison.  If the images
    differ in size, the second image is resized to match the first.

    Args:
        image_a_path: Path to the first image (reference).
        image_b_path: Path to the second image (test).
        crop: If True, crop both images to viewport center before comparing.
        crop_margin: Margin fraction when cropping (default 10%).

    Returns:
        SSIM score in ``[0.0, 1.0]`` where ``1.0`` means identical.
    """
    path_a = Path(image_a_path)
    path_b = Path(image_b_path)

    if crop:
        path_a = crop_viewport(path_a, path_a.parent / f"{path_a.stem}_cropped.png", crop_margin)
        path_b = crop_viewport(path_b, path_b.parent / f"{path_b.stem}_cropped.png", crop_margin)

    gray_a = _load_grayscale(path_a)
    gray_b = _load_grayscale(path_b)
    gray_a, gray_b = _ensure_same_size(gray_a, gray_b)

    score, _ = _ssim_single_channel(gray_a, gray_b, data_range=1.0)
    logger.info("SSIM between %s and %s: %.4f", image_a_path, image_b_path, score)
    return score


def compute_ssim_rgb(
    image_a_path: str | Path,
    image_b_path: str | Path,
) -> float:
    """Compute mean SSIM across R, G, B channels independently.

    This gives a more nuanced score that accounts for colour differences,
    not just luminance.

    Returns:
        Mean SSIM across the three channels in ``[0.0, 1.0]``.
    """
    rgb_a = _load_rgb(image_a_path).astype(np.float64) / 255.0
    rgb_b = _load_rgb(image_b_path).astype(np.float64) / 255.0
    rgb_a, rgb_b = _ensure_same_size(rgb_a, rgb_b)

    scores = []
    for ch in range(3):
        s, _ = _ssim_single_channel(rgb_a[..., ch], rgb_b[..., ch], data_range=1.0)
        scores.append(s)

    mean_score = float(np.mean(scores))
    logger.info("RGB SSIM: R=%.4f G=%.4f B=%.4f mean=%.4f", *scores, mean_score)
    return mean_score


def generate_diff_heatmap(
    image_a_path: str | Path,
    image_b_path: str | Path,
    output_path: str | Path,
) -> Path:
    """Create and save a visual diff heatmap highlighting pixel differences.

    The heatmap uses a black-to-red-to-yellow-to-white colour ramp where
    brighter areas indicate larger per-pixel differences.

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

    # Per-pixel absolute difference, max across channels
    diff = np.abs(rgb_a.astype(np.float64) - rgb_b.astype(np.float64))
    diff_gray = np.max(diff, axis=2)

    # Normalise to [0, 255]
    max_val = diff_gray.max()
    if max_val > 0:
        diff_norm = (diff_gray / max_val * 255).astype(np.uint8)
    else:
        diff_norm = diff_gray.astype(np.uint8)

    # Simple heatmap: black -> red -> yellow -> white
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
        heatmap_output: Optional path for the heatmap image.

    Returns:
        A tuple of ``(percent_different, heatmap_path)``.
    """
    rgb_a = _load_rgb(image_a_path)
    rgb_b = _load_rgb(image_b_path)
    rgb_a, rgb_b = _ensure_same_size(rgb_a, rgb_b)

    tolerance = 5
    diff = np.abs(rgb_a.astype(np.int16) - rgb_b.astype(np.int16))
    differs = np.any(diff > tolerance, axis=2)
    percent_different = float(np.mean(differs) * 100.0)

    if heatmap_output is None:
        stem_a = Path(image_a_path).stem
        stem_b = Path(image_b_path).stem
        heatmap_output = Path(image_a_path).parent / f"{stem_a}_vs_{stem_b}_diff.png"

    heatmap_path = generate_diff_heatmap(image_a_path, image_b_path, heatmap_output)

    logger.info(
        "Pixel diff: %.2f%% different (%s vs %s)",
        percent_different,
        image_a_path,
        image_b_path,
    )
    return percent_different, heatmap_path


def compare_images(
    image_a_path: str | Path,
    image_b_path: str | Path,
    output_dir: str | Path,
    crop: bool = False,
    crop_margin: float = 0.1,
) -> dict:
    """Run a full visual comparison between two images.

    Computes SSIM (grayscale and RGB), pixel diff percentage, and generates
    a diff heatmap.  All artefacts are saved under *output_dir*.

    Args:
        image_a_path: Reference image (e.g. Unity screenshot).
        image_b_path: Test image (e.g. Roblox screenshot).
        output_dir: Directory to write the heatmap and any cropped images.
        crop: Whether to crop viewport before comparing.
        crop_margin: Margin fraction for cropping.

    Returns:
        Dict with keys: ``ssim``, ``ssim_rgb``, ``pixel_diff_pct``,
        ``heatmap_path``, ``quality_label``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    ssim_score = compute_ssim(image_a_path, image_b_path, crop=crop, crop_margin=crop_margin)
    ssim_rgb = compute_ssim_rgb(image_a_path, image_b_path)

    heatmap_out = output_dir / "diff_heatmap.png"
    pixel_pct, heatmap_path = compute_pixel_diff(image_a_path, image_b_path, heatmap_out)

    # Quality label
    if ssim_score >= 0.95:
        label = "Excellent"
    elif ssim_score >= 0.85:
        label = "Good"
    elif ssim_score >= 0.70:
        label = "Fair"
    elif ssim_score >= 0.50:
        label = "Poor"
    else:
        label = "Very Poor"

    return {
        "ssim": ssim_score,
        "ssim_rgb": ssim_rgb,
        "pixel_diff_pct": pixel_pct,
        "heatmap_path": str(heatmap_path),
        "quality_label": label,
    }
