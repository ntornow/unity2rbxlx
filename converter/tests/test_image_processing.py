"""Phase 4.2: Texture operations added for material mapping."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

PIL = pytest.importorskip("PIL")
from PIL import Image

from utils.image_processing import (
    bake_ao, threshold_alpha, to_grayscale, offset_image, scale_normal_map,
)


@pytest.fixture
def tmp_albedo(tmp_path):
    p = tmp_path / "albedo.png"
    Image.new("RGBA", (4, 4), (200, 150, 100, 255)).save(p)
    return p


@pytest.fixture
def tmp_ao(tmp_path):
    # Half bright, half dark AO map.
    p = tmp_path / "ao.png"
    img = Image.new("L", (4, 4), 255)
    px = img.load()
    for y in range(4):
        for x in range(4):
            px[x, y] = 255 if x < 2 else 0
    img.save(p)
    return p


class TestBakeAo:
    def test_strength_zero_leaves_albedo_unchanged(self, tmp_albedo, tmp_ao, tmp_path):
        out = tmp_path / "out.png"
        bake_ao(tmp_albedo, tmp_ao, out, strength=0.0)
        result = Image.open(out).convert("RGBA")
        px = result.load()
        # strength=0: factor is always 1 → pixels stay put.
        assert px[0, 0] == (200, 150, 100, 255)
        assert px[3, 3] == (200, 150, 100, 255)

    def test_strength_one_darkens_where_ao_is_dark(self, tmp_albedo, tmp_ao, tmp_path):
        out = tmp_path / "out.png"
        bake_ao(tmp_albedo, tmp_ao, out, strength=1.0)
        result = Image.open(out).convert("RGBA")
        px = result.load()
        # AO is 255 (bright) on x<2 → no darkening.
        assert px[0, 0] == (200, 150, 100, 255)
        # AO is 0 (dark) on x>=2 → fully dark.
        assert px[3, 3] == (0, 0, 0, 255)


class TestThresholdAlpha:
    def test_binary_mask(self, tmp_path):
        # Gradient alpha: 0, 85, 170, 255 across x.
        src = tmp_path / "src.png"
        img = Image.new("RGBA", (4, 1), (255, 255, 255, 0))
        for x in range(4):
            img.putpixel((x, 0), (255, 255, 255, x * 85))
        img.save(src)
        out = tmp_path / "out.png"
        threshold_alpha(src, out, cutoff=0.5)
        result = Image.open(out).convert("RGBA")
        # cutoff 0.5 == 127: 0, 85 → 0 ; 170, 255 → 255.
        assert result.getpixel((0, 0))[3] == 0
        assert result.getpixel((1, 0))[3] == 0
        assert result.getpixel((2, 0))[3] == 255
        assert result.getpixel((3, 0))[3] == 255


class TestToGrayscale:
    def test_rgb_to_l(self, tmp_path):
        src = tmp_path / "src.png"
        Image.new("RGB", (2, 2), (255, 0, 0)).save(src)
        out = tmp_path / "out.png"
        to_grayscale(src, out)
        result = Image.open(out)
        assert result.mode == "L"


class TestOffsetImage:
    def test_wrap_shift(self, tmp_path):
        src = tmp_path / "src.png"
        img = Image.new("RGBA", (4, 1))
        for x in range(4):
            img.putpixel((x, 0), (x * 60, 0, 0, 255))
        img.save(src)
        out = tmp_path / "out.png"
        offset_image(src, out, (1, 0))
        result = Image.open(out).convert("RGBA")
        # Offset +1 X wraps: [180, 0, 60, 120] for R.
        assert result.getpixel((0, 0))[0] == 180
        assert result.getpixel((1, 0))[0] == 0
        assert result.getpixel((2, 0))[0] == 60


class TestScaleNormalMap:
    def test_identity_preserves_normal(self, tmp_path):
        src = tmp_path / "src.png"
        # Encoded "straight up" normal (0, 0, 1) = (128, 128, 255).
        Image.new("RGBA", (2, 2), (128, 128, 255, 255)).save(src)
        out = tmp_path / "out.png"
        scale_normal_map(src, out, scale=1.0)
        result = Image.open(out).convert("RGBA")
        px = result.getpixel((0, 0))
        # Round-trip through float math — allow ±1.
        assert abs(px[0] - 128) <= 1
        assert abs(px[1] - 128) <= 1
        assert abs(px[2] - 255) <= 1
