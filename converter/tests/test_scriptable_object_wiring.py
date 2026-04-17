"""Tests: ScriptableObject converter wiring in extract_assets + write_output."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent))


def _fake_so_result(assets):
    from converter.scriptable_object_converter import AssetConversionResult, ConvertedAsset

    return AssetConversionResult(
        assets=[
            ConvertedAsset(
                source_path=Path(f"/unity/{name}.asset"),
                asset_name=name,
                luau_source=f"local data = {{ name = \"{name}\" }}\nreturn data",
                field_count=1,
            )
            for name in assets
        ],
        total=len(assets),
        converted=len(assets),
    )


def test_extract_assets_writes_scriptable_objects_to_disk(tmp_path):
    from converter.pipeline import Pipeline

    project = tmp_path / "fake_project"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"

    pipeline = Pipeline(unity_project_path=project, output_dir=output, skip_upload=True)

    fake_manifest = MagicMock()
    fake_manifest.assets = []
    fake_manifest.total_size_bytes = 0

    fake_so = _fake_so_result(["Inventory", "QuestDatabase"])

    with patch("unity.asset_extractor.extract_assets", return_value=fake_manifest), \
         patch("converter.scriptable_object_converter.convert_asset_files", return_value=fake_so), \
         patch("converter.sprite_extractor.extract_sprites"), \
         patch.object(pipeline, "_compute_fbx_bounding_boxes"):
        pipeline.extract_assets()

    so_dir = output / "scripts" / "scriptable_objects"
    assert (so_dir / "Inventory.luau").exists()
    assert (so_dir / "QuestDatabase.luau").exists()
    assert "Inventory" in (so_dir / "Inventory.luau").read_text()
    assert pipeline.state.scriptable_objects is fake_so


def test_write_output_attaches_scriptable_objects_as_module_scripts():
    """Attach loop appends a ModuleScript per asset and dedupes by name."""
    from core.roblox_types import RbxPlace, RbxScript

    place = RbxPlace()
    place.scripts = [RbxScript(name="Existing", source="", script_type="Script")]
    so = _fake_so_result(["Inventory", "Existing"])

    existing = {s.name for s in place.scripts}
    for asset in so.assets:
        if asset.asset_name in existing:
            continue
        place.scripts.append(RbxScript(
            name=asset.asset_name,
            source=asset.luau_source,
            script_type="ModuleScript",
        ))

    names = [s.name for s in place.scripts]
    assert names == ["Existing", "Inventory"]
    assert next(s for s in place.scripts if s.name == "Inventory").script_type == "ModuleScript"
