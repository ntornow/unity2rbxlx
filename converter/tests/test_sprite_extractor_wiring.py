"""
test_sprite_extractor_wiring.py — Verify the extract_assets phase invokes
sprite_extractor and persists results onto the ConversionContext.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fake_sprite_result(extracted_count: int, warnings=None):
    """Build a SpriteExtractionResult shape extract_sprites returns."""
    from converter.sprite_extractor import SpriteExtractionResult

    return SpriteExtractionResult(
        extracted=[("hero", Path("/tmp/sprites/hero.png"))] * extracted_count,
        sprite_guid_to_file={"abc123": Path("/tmp/sprites/hero.png")} if extracted_count else {},
        warnings=warnings or [],
        total_spritesheets=1 if extracted_count else 0,
        total_sprites_extracted=extracted_count,
    )


def test_extract_assets_invokes_sprite_extractor(tmp_path):
    """extract_assets should call sprite_extractor.extract_sprites and stash
    its result on PipelineState.sprite_result + ctx.sprite_guid_to_file."""
    from converter.pipeline import Pipeline

    project = tmp_path / "fake_project"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"

    pipeline = Pipeline(
        unity_project_path=project,
        output_dir=output,
        skip_upload=True,
    )

    # Stand in for extract_assets' upstream dependencies.
    fake_manifest = MagicMock()
    fake_manifest.assets = []
    fake_manifest.total_size_bytes = 0

    pipeline.state.guid_index = MagicMock()  # sprite extractor only needs the object

    fake_result = _fake_sprite_result(extracted_count=3)

    with patch("unity.asset_extractor.extract_assets", return_value=fake_manifest), \
         patch("converter.sprite_extractor.extract_sprites", return_value=fake_result), \
         patch.object(pipeline, "_compute_fbx_bounding_boxes"):
        pipeline.extract_assets()

    assert pipeline.state.sprite_result is fake_result
    assert pipeline.ctx.sprite_guid_to_file == {"abc123": "/tmp/sprites/hero.png"}


def test_extract_assets_skips_sprite_extractor_without_guid_index(tmp_path):
    from converter.pipeline import Pipeline

    project = tmp_path / "fake_project"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"

    pipeline = Pipeline(unity_project_path=project, output_dir=output, skip_upload=True)
    pipeline.state.guid_index = None

    fake_manifest = MagicMock()
    fake_manifest.assets = []
    fake_manifest.total_size_bytes = 0

    with patch("unity.asset_extractor.extract_assets", return_value=fake_manifest), \
         patch("converter.sprite_extractor.extract_sprites") as mock_extract, \
         patch.object(pipeline, "_compute_fbx_bounding_boxes"):
        pipeline.extract_assets()

    mock_extract.assert_not_called()
    assert pipeline.state.sprite_result is None
    assert pipeline.ctx.sprite_guid_to_file == {}


def test_extract_assets_swallows_sprite_extractor_failure(tmp_path):
    """Sprite extraction is best-effort; an exception must not abort
    extract_assets."""
    from converter.pipeline import Pipeline

    project = tmp_path / "fake_project"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"

    pipeline = Pipeline(unity_project_path=project, output_dir=output, skip_upload=True)
    pipeline.state.guid_index = MagicMock()

    fake_manifest = MagicMock()
    fake_manifest.assets = []
    fake_manifest.total_size_bytes = 0

    with patch("unity.asset_extractor.extract_assets", return_value=fake_manifest), \
         patch("converter.sprite_extractor.extract_sprites", side_effect=RuntimeError("boom")), \
         patch.object(pipeline, "_compute_fbx_bounding_boxes"):
        pipeline.extract_assets()  # must not raise

    assert pipeline.state.sprite_result is None
    assert pipeline.ctx.sprite_guid_to_file == {}
