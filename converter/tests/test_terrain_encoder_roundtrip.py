"""Tests for terrain_encoder.encode_smooth_grid and material inference.

The Roblox SmoothGrid binary format encodes terrain heights, materials,
and occupancy. A regression here renders the wrong terrain — wrong
material at the wrong height, sometimes invisibly so on a small map.

We can't easily decode the SmoothGrid output without re-implementing the
parser, so the tests focus on:
  - Output is a non-empty base64 string
  - Output changes when inputs change (catches a "function returns
    constant" regression)
  - Boundary inputs don't crash
  - Material inference helpers behave per their documented thresholds
  - Splat alphas affect material selection
"""
from __future__ import annotations

import base64

import pytest

from roblox.terrain_encoder import (
    MATERIAL_GRASS,
    MATERIAL_MUD,
    MATERIAL_ROCK,
    MATERIAL_SAND,
    MATERIAL_SLATE,
    _height_based_material,
    _splat_based_material,
    encode_smooth_grid,
)


def _flat_heights(resolution: int, height: float = 0.5) -> list[float]:
    return [height] * (resolution * resolution)


class TestHeightBasedMaterial:
    """Each band of normalized height maps to a fixed material —
    surprises here render the wrong biome."""

    @pytest.mark.parametrize(
        "norm_h,expected",
        [
            (0.0, MATERIAL_SAND),
            (0.10, MATERIAL_SAND),
            (0.20, MATERIAL_GRASS),
            (0.50, MATERIAL_GRASS),  # mid, no slope -> grass
            (0.70, MATERIAL_ROCK),
            (0.90, MATERIAL_SLATE),
        ],
    )
    def test_band_assignment(self, norm_h: float, expected: int) -> None:
        assert _height_based_material(norm_h, slope=0.0) == expected

    def test_steep_slope_overrides_to_rock(self) -> None:
        """A 45-degree slope overrides any height band to rock."""
        for h in (0.0, 0.3, 0.6, 0.95):
            assert _height_based_material(h, slope=1.0) == MATERIAL_ROCK

    def test_mid_band_with_moderate_slope_returns_mud(self) -> None:
        """Documented behavior: mid (35-60%) with slope > 0.3 -> mud."""
        assert _height_based_material(0.45, slope=0.5) == MATERIAL_MUD


class TestSplatBasedMaterial:
    def test_returns_default_when_no_splat_data(self) -> None:
        assert (
            _splat_based_material([], 0, [], 0.0, 0.0, 100.0, 100.0)
            == MATERIAL_GRASS
        )

    def test_dominant_layer_wins(self) -> None:
        """Two layers, layer 1 has the higher alpha at this position ->
        layer 1's name maps to material."""
        # 2x2 splat map; layer 0 weak, layer 1 strong
        layer0 = [0.1, 0.1, 0.1, 0.1]
        layer1 = [0.9, 0.9, 0.9, 0.9]
        layer_names = ["GrassTexture", "RockTexture"]
        result = _splat_based_material(
            [layer0, layer1], 2, layer_names,
            world_x=10.0, world_z=10.0,
            terrain_width=20.0, terrain_length=20.0,
        )
        assert result == MATERIAL_ROCK

    def test_unknown_layer_name_falls_back_to_grass(self) -> None:
        layer0 = [1.0, 1.0, 1.0, 1.0]
        result = _splat_based_material(
            [layer0], 2, ["MyCustomLayerName"],
            world_x=0.0, world_z=0.0,
            terrain_width=20.0, terrain_length=20.0,
        )
        assert result == MATERIAL_GRASS


class TestEncodeSmoothGridStructure:
    """The encoded output must be a non-empty base64 string and must vary
    with the input. Without these the encoder could silently regress to
    a constant or empty output."""

    def test_returns_non_empty_base64(self) -> None:
        encoded = encode_smooth_grid(
            heights=_flat_heights(8),
            resolution=8,
            scale=(1.0, 10.0, 1.0),
        )
        assert isinstance(encoded, str)
        assert encoded
        # Must round-trip through base64 decode
        decoded = base64.b64decode(encoded)
        assert len(decoded) > 0

    def test_different_heights_produce_different_output(self) -> None:
        flat = encode_smooth_grid(
            heights=_flat_heights(8, 0.2),
            resolution=8,
            scale=(1.0, 10.0, 1.0),
        )
        peaked = encode_smooth_grid(
            heights=[1.0 if i == 32 else 0.2 for i in range(64)],
            resolution=8,
            scale=(1.0, 10.0, 1.0),
        )
        assert flat != peaked

    def test_splat_alpha_changes_output(self) -> None:
        """The same heightmap must encode differently when splat alphas
        select a different layer."""
        heights = _flat_heights(8, 0.4)
        scale = (1.0, 10.0, 1.0)
        without_splat = encode_smooth_grid(
            heights=heights, resolution=8, scale=scale,
        )

        # Splat resolution mirrors heightmap; one strong layer everywhere
        layer = [1.0] * 64
        with_splat = encode_smooth_grid(
            heights=heights, resolution=8, scale=scale,
            layer_names=["Sand"],
            splat_alphas=[layer],
            splat_resolution=8,
        )
        assert without_splat != with_splat


class TestEncodeSmoothGridBoundaries:
    """Voxel-boundary inputs are silent-corruption hot spots: too-small
    resolution, all-zero heights, all-max heights, and one-pixel splat
    maps must all encode without crashing."""

    def test_minimum_resolution(self) -> None:
        encode_smooth_grid(
            heights=_flat_heights(2),
            resolution=2,
            scale=(1.0, 10.0, 1.0),
        )

    def test_all_zero_heights(self) -> None:
        encode_smooth_grid(
            heights=[0.0] * 64,
            resolution=8,
            scale=(1.0, 10.0, 1.0),
        )

    def test_all_max_heights(self) -> None:
        encode_smooth_grid(
            heights=[1.0] * 64,
            resolution=8,
            scale=(1.0, 10.0, 1.0),
        )

    def test_terrain_position_offset_changes_output(self) -> None:
        heights = _flat_heights(8, 0.3)
        at_origin = encode_smooth_grid(
            heights=heights, resolution=8, scale=(1.0, 10.0, 1.0),
            terrain_position=(0.0, 0.0, 0.0),
        )
        offset = encode_smooth_grid(
            heights=heights, resolution=8, scale=(1.0, 10.0, 1.0),
            terrain_position=(100.0, 0.0, 100.0),
        )
        # Chunk coordinates change with world offset; encoded bytes differ
        assert at_origin != offset
