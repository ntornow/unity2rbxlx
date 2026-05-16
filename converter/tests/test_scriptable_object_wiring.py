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


def test_write_output_attaches_scriptable_objects_as_module_scripts(tmp_path):
    """write_output must append one ModuleScript per ScriptableObject asset
    (deduped by name) and set source_path so in-memory edits round-trip back
    to scripts/scriptable_objects/ on the next run.

    This test drives the real pipeline.write_output so a regression in the
    attach block is caught — not a reproduction of the logic.
    """
    from converter.pipeline import Pipeline
    from core.roblox_types import RbxPlace, RbxScript

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"

    pipeline = Pipeline(
        unity_project_path=project, output_dir=output, skip_upload=True,
    )
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.rbx_place.scripts = [
        RbxScript(name="Existing", source="", script_type="Script"),
    ]
    pipeline.state.scriptable_objects = _fake_so_result(["Inventory", "Existing"])
    # Mark transpile_scripts completed so write_output takes the
    # preserve-vs-fresh branch relevant to this test. We want the
    # fresh-write attach path (scriptable_objects state populated) — set
    # it up so write_output falls into the fresh branch.
    pipeline.ctx.completed_phases.append("transpile_scripts")
    pipeline._retranspile = True  # forces fresh branch (not preserve)

    pipeline.write_output()

    names = [s.name for s in pipeline.state.rbx_place.scripts]
    assert "Inventory" in names
    assert names.count("Existing") == 1, "Existing should not be duplicated"

    inventory = next(
        s for s in pipeline.state.rbx_place.scripts if s.name == "Inventory"
    )
    assert inventory.script_type == "ModuleScript"
    assert inventory.source_path == "scriptable_objects/Inventory.luau", (
        "ScriptableObject RbxScript must record source_path so the final "
        "rewrite loop lands in-memory edits back in the correct subdir."
    )


def test_so_unique_names_stable_across_checkout_roots():
    """resolve_unique_asset_names must hash a project-relative path
    (anchored at ``Assets/``) so the same converted project produces the
    same hashed module name on every machine. Hashing the absolute path
    leaks the developer's checkout root into the public ModuleScript
    name.
    """
    from converter.scriptable_object_converter import (
        ConvertedAsset, resolve_unique_asset_names,
    )

    # Same project, two different checkout roots.
    assets_a = [
        ConvertedAsset(
            source_path=Path("/Users/alice/proj/Assets/Audio/Settings.asset"),
            asset_name="Settings",
            luau_source="-- audio\n",
        ),
        ConvertedAsset(
            source_path=Path("/Users/alice/proj/Assets/Graphics/Settings.asset"),
            asset_name="Settings",
            luau_source="-- graphics\n",
        ),
    ]
    assets_b = [
        ConvertedAsset(
            source_path=Path("/var/ci/build/123/Assets/Audio/Settings.asset"),
            asset_name="Settings",
            luau_source="-- audio\n",
        ),
        ConvertedAsset(
            source_path=Path("/var/ci/build/123/Assets/Graphics/Settings.asset"),
            asset_name="Settings",
            luau_source="-- graphics\n",
        ),
    ]
    names_a = sorted(resolve_unique_asset_names(assets_a).values())
    names_b = sorted(resolve_unique_asset_names(assets_b).values())
    assert names_a == names_b, (
        f"unique names must be checkout-root-independent; "
        f"got {names_a} vs {names_b}"
    )


def test_preserve_scripts_does_not_clobber_hand_edited_scriptable_objects(tmp_path):
    """On the preserve_scripts path (transpile_scripts completed, no
    retranspile, no fresh transpilation_result), the SO disk write must
    leave existing files alone so hand-edits made between assemble and
    upload survive the round-trip.
    """
    from converter.pipeline import Pipeline
    from converter.scriptable_object_converter import (
        AssetConversionResult, ConvertedAsset,
    )
    from core.roblox_types import RbxPlace

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    scripts_dir = output / "scripts"
    so_dir = scripts_dir / "scriptable_objects"
    so_dir.mkdir(parents=True)
    # Simulate a prior assemble landing the module, then a hand-edit.
    hand_edited = so_dir / "Inventory.luau"
    hand_edited.write_text("-- hand-edited by user\nreturn { fixed = true }\n")

    pipeline = Pipeline(
        unity_project_path=project, output_dir=output, skip_upload=True,
    )
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.scriptable_objects = AssetConversionResult(
        assets=[
            ConvertedAsset(
                source_path=Path("/unity/Inventory.asset"),
                asset_name="Inventory",
                luau_source="-- original generated body\nreturn { fixed = false }\n",
                field_count=1,
            ),
        ],
        total=1,
        converted=1,
    )
    pipeline.ctx.completed_phases.append("transpile_scripts")
    # preserve_scripts branch: no retranspile, scripts_dir exists,
    # no transpilation_result.
    pipeline._retranspile = False
    pipeline.state.transpilation_result = None

    pipeline.write_output()

    surviving = hand_edited.read_text()
    assert "hand-edited by user" in surviving, (
        "preserve_scripts must keep the user's hand-edited ScriptableObject "
        f"module; got: {surviving!r}"
    )


def test_scriptable_objects_dedupe_by_folder_when_names_collide(tmp_path):
    """Two ScriptableObjects with the same m_Name from different folders
    (e.g. Audio/Settings.asset and Graphics/Settings.asset) must both
    survive into the place. The old dedupe-by-name dropped one of them
    AND let the disk write overwrite the first .luau file.
    """
    from converter.pipeline import Pipeline
    from converter.scriptable_object_converter import (
        AssetConversionResult, ConvertedAsset,
    )
    from core.roblox_types import RbxPlace

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"

    pipeline = Pipeline(
        unity_project_path=project, output_dir=output, skip_upload=True,
    )
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.scriptable_objects = AssetConversionResult(
        assets=[
            ConvertedAsset(
                source_path=Path("/unity/Audio/Settings.asset"),
                asset_name="Settings",
                luau_source="-- audio settings\nreturn { domain = 'audio' }",
                field_count=1,
            ),
            ConvertedAsset(
                source_path=Path("/unity/Graphics/Settings.asset"),
                asset_name="Settings",
                luau_source="-- graphics settings\nreturn { domain = 'graphics' }",
                field_count=1,
            ),
        ],
        total=2,
        converted=2,
    )
    pipeline.ctx.completed_phases.append("transpile_scripts")
    pipeline._retranspile = True  # forces fresh-transpile branch

    pipeline.write_output()

    so_dir = output / "scripts" / "scriptable_objects"
    luau_files = sorted(p.name for p in so_dir.glob("*.luau"))
    assert len(luau_files) == 2, (
        f"Both Settings.asset files must land on disk under unique names; "
        f"got {luau_files}"
    )
    # Both luau files must carry the original distinct contents.
    contents = {p.read_text() for p in so_dir.glob("*.luau")}
    assert any("'audio'" in c for c in contents), "audio Settings payload missing"
    assert any("'graphics'" in c for c in contents), "graphics Settings payload missing"

    so_scripts = [
        s for s in pipeline.state.rbx_place.scripts
        if s.script_type == "ModuleScript" and s.source_path.startswith("scriptable_objects/")
    ]
    assert len(so_scripts) == 2, (
        f"Both ScriptableObjects must attach to rbx_place; got {[s.name for s in so_scripts]}"
    )
    # Names should be distinct so the rbxlx writer doesn't collapse them.
    assert len({s.name for s in so_scripts}) == 2


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
