"""Tests: extract_assets wires sprite_extractor."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fake_sprite_result(count):
    from converter.sprite_extractor import SpriteExtractionResult

    return SpriteExtractionResult(
        extracted=[("hero", Path("/tmp/sprites/hero.png"))] * count,
        sprite_guid_to_file={"abc123": Path("/tmp/sprites/hero.png")} if count else {},
        warnings=[],
        total_spritesheets=1 if count else 0,
        total_sprites_extracted=count,
    )


def _make_pipeline(tmp_path):
    from converter.pipeline import Pipeline

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    return Pipeline(unity_project_path=project, output_dir=tmp_path / "out", skip_upload=True)


def _fake_manifest():
    m = MagicMock()
    m.assets = []
    m.total_size_bytes = 0
    return m


def test_extract_assets_invokes_sprite_extractor(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    pipeline.state.guid_index = MagicMock()
    fake_result = _fake_sprite_result(3)

    with patch("unity.asset_extractor.extract_assets", return_value=_fake_manifest()), \
         patch("converter.sprite_extractor.extract_sprites", return_value=fake_result), \
         patch.object(pipeline, "_compute_fbx_bounding_boxes"):
        pipeline.extract_assets()

    assert pipeline.state.sprite_result is fake_result
    assert pipeline.ctx.sprite_guid_to_file == {"abc123": "/tmp/sprites/hero.png"}


def test_extract_assets_skips_without_guid_index(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    pipeline.state.guid_index = None

    with patch("unity.asset_extractor.extract_assets", return_value=_fake_manifest()), \
         patch("converter.sprite_extractor.extract_sprites") as mock_extract, \
         patch.object(pipeline, "_compute_fbx_bounding_boxes"):
        pipeline.extract_assets()

    mock_extract.assert_not_called()
    assert pipeline.state.sprite_result is None


def test_extract_assets_surfaces_sprite_failure_as_warning(tmp_path):
    """Sprite extractor failures must not crash the pipeline (a broken
    spritesheet shouldn't take the whole conversion down), but the failure
    MUST be visible: a WARNING log and a ctx.warnings entry so the final
    report surfaces it. Silent swallow would hide a real bug.
    """
    pipeline = _make_pipeline(tmp_path)
    pipeline.state.guid_index = MagicMock()

    with patch("unity.asset_extractor.extract_assets", return_value=_fake_manifest()), \
         patch("converter.sprite_extractor.extract_sprites", side_effect=RuntimeError("boom")), \
         patch.object(pipeline, "_compute_fbx_bounding_boxes"):
        pipeline.extract_assets()

    # Pipeline didn't crash, sprite result still None (nothing to work with).
    assert pipeline.state.sprite_result is None
    # The failure must have been recorded to ctx.warnings, not silently swallowed.
    assert any(
        "Sprite extraction failed" in w and "boom" in w
        for w in pipeline.ctx.warnings
    ), f"sprite failure not surfaced in ctx.warnings: {pipeline.ctx.warnings}"


def test_extract_assets_surfaces_scriptable_object_failure_as_warning(tmp_path):
    """Same contract for the ScriptableObject converter: failure is
    survivable but must be visible in ctx.warnings for the report.
    """
    pipeline = _make_pipeline(tmp_path)
    pipeline.state.guid_index = MagicMock()

    with patch(
        "unity.asset_extractor.extract_assets", return_value=_fake_manifest(),
    ), patch(
        "converter.scriptable_object_converter.convert_asset_files",
        side_effect=RuntimeError("asset parse failed"),
    ), patch(
        "converter.sprite_extractor.extract_sprites",
        return_value=_fake_sprite_result(0),
    ), patch.object(pipeline, "_compute_fbx_bounding_boxes"):
        pipeline.extract_assets()

    assert pipeline.state.scriptable_objects is None
    assert any(
        "ScriptableObject conversion failed" in w and "asset parse failed" in w
        for w in pipeline.ctx.warnings
    ), (
        "scriptable object failure not surfaced in ctx.warnings: "
        f"{pipeline.ctx.warnings}"
    )
