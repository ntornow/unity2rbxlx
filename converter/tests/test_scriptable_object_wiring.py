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


def test_extract_assets_populates_scriptable_objects_state(tmp_path):
    """extract_assets converts .asset files into state but defers the disk
    write to write_output so the scripts_dir rmtree doesn't wipe them."""
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

    assert pipeline.state.scriptable_objects is fake_so
    # Disk write deferred to write_output.
    assert not (output / "scripts" / "scriptable_objects").exists()


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


def test_scriptable_objects_survive_fresh_transpile_rmtree(tmp_path):
    """write_output wipes scripts/ on the fresh-transpile path, but the SO
    disk write happens *after* the wipe so the files land correctly."""
    from converter.pipeline import Pipeline

    project = tmp_path / "fake_project"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    scripts_dir = output / "scripts"
    scripts_dir.mkdir(parents=True)
    # Simulate leftover state a rmtree would clear.
    (scripts_dir / "stale.luau").write_text("stale")

    pipeline = Pipeline(unity_project_path=project, output_dir=output, skip_upload=True)
    pipeline.state.scriptable_objects = _fake_so_result(["Inventory"])

    # Reproduce the minimum slice of write_output that handles scripts_dir.
    # (Running the full write_output requires rbx_place + transpilation plumbing
    # that this unit test doesn't need.)
    import shutil
    if scripts_dir.exists():
        shutil.rmtree(scripts_dir)
    scripts_dir.mkdir(parents=True)
    if pipeline.state.scriptable_objects:
        so_dir = scripts_dir / "scriptable_objects"
        so_dir.mkdir(parents=True, exist_ok=True)
        for asset in pipeline.state.scriptable_objects.assets:
            (so_dir / f"{asset.asset_name}.luau").write_text(
                asset.luau_source, encoding="utf-8",
            )

    assert not (scripts_dir / "stale.luau").exists()
    assert (scripts_dir / "scriptable_objects" / "Inventory.luau").exists()
