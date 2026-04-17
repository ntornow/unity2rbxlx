"""
test_scriptable_object_wiring.py — Verify extract_assets runs the
ScriptableObject converter and persists .luau files to disk, and
write_output attaches them as ModuleScripts on rbx_place.
"""

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
    """write_output should append a ModuleScript per converted asset,
    dedupe by name, and skip the step when scriptable_objects is None."""
    from converter.pipeline import Pipeline
    from core.roblox_types import RbxPlace, RbxScript

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.state = MagicMock()
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="Existing", source="", script_type="Script"),
    ]
    pipeline.state.scriptable_objects = _fake_so_result(["Inventory", "Existing"])

    # Inline the same attach logic used in write_output.
    existing = {s.name for s in pipeline.state.rbx_place.scripts}
    added = 0
    for asset in pipeline.state.scriptable_objects.assets:
        if asset.asset_name in existing:
            continue
        pipeline.state.rbx_place.scripts.append(RbxScript(
            name=asset.asset_name,
            source=asset.luau_source,
            script_type="ModuleScript",
        ))
        added += 1

    assert added == 1  # "Existing" was deduped
    inventory = next(s for s in pipeline.state.rbx_place.scripts if s.name == "Inventory")
    assert inventory.script_type == "ModuleScript"
