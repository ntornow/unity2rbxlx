"""Tests: preserved-scripts rehydration reads conversion_plan.json."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_pipeline(tmp_path):
    from converter.pipeline import Pipeline
    from core.roblox_types import RbxPlace

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir(parents=True)
    pipeline = Pipeline(unity_project_path=project, output_dir=output, skip_upload=True)
    pipeline.state.rbx_place = RbxPlace()
    return pipeline


def _write_plan(pipeline, storage_plan):
    plan_path = pipeline.output_dir / "conversion_plan.json"
    plan_path.write_text(json.dumps({"storage_plan": storage_plan}))
    return plan_path


def test_load_storage_plan_missing_file_returns_empty(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    assert pipeline._load_storage_plan_for_rehydration() == {}


def test_load_storage_plan_malformed_file_returns_empty(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    (pipeline.output_dir / "conversion_plan.json").write_text("not valid json {{{")
    assert pipeline._load_storage_plan_for_rehydration() == {}


def test_load_storage_plan_returns_category_lookup(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    _write_plan(pipeline, {
        "server_scripts": ["GameManager"],
        "client_scripts": ["InputHandler"],
        "character_scripts": ["PlayerMove"],
        "replicated_first_scripts": ["Loading"],
        "shared_modules": ["Constants"],
        "server_modules": ["Secrets"],
    })

    lookup = pipeline._load_storage_plan_for_rehydration()
    assert lookup["GameManager"] == ("Script", "ServerScriptService")
    assert lookup["InputHandler"] == ("LocalScript", "StarterPlayer.StarterPlayerScripts")
    assert lookup["PlayerMove"] == ("LocalScript", "StarterPlayer.StarterCharacterScripts")
    assert lookup["Loading"] == ("ModuleScript", "ReplicatedFirst")
    assert lookup["Constants"] == ("ModuleScript", "ReplicatedStorage")
    assert lookup["Secrets"] == ("ModuleScript", "ServerStorage")


def test_rehydration_uses_plan_over_heuristic(tmp_path):
    """Heuristic would flag the `\\nreturn` substring as ModuleScript; plan wins."""
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "GameManager.luau").write_text(
        'local x = "prefix\\nreturn foo"\nprint("hello")\n'
    )
    _write_plan(pipeline, {"server_scripts": ["GameManager"]})

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    script = pipeline.state.rbx_place.scripts[0]
    assert script.name == "GameManager"
    assert script.script_type == "Script"


def test_rehydration_sets_parent_path_from_plan(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "InputHandler.luau").write_text("-- local script\n")
    _write_plan(pipeline, {"client_scripts": ["InputHandler"]})

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    script = pipeline.state.rbx_place.scripts[0]
    assert script.script_type == "LocalScript"
    assert getattr(script, "parent_path", None) == "StarterPlayer.StarterPlayerScripts"


def test_rehydration_falls_back_to_heuristic_for_unplanned_script(tmp_path):
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "NewModule.luau").write_text("local M = {}\nreturn M\n")
    # Plan covers a different script only.
    _write_plan(pipeline, {"server_scripts": ["SomethingElse"]})

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    script = pipeline.state.rbx_place.scripts[0]
    assert script.name == "NewModule"
    assert script.script_type == "ModuleScript"  # heuristic on `\nreturn `
    assert getattr(script, "parent_path", None) is None


def test_rehydration_picks_up_scriptable_objects_subdir(tmp_path):
    """Item 5 writes to scripts/scriptable_objects/; item 12 must find them."""
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    so_dir = scripts_dir / "scriptable_objects"
    so_dir.mkdir(parents=True)
    (so_dir / "Inventory.luau").write_text('local data = {}\nreturn data\n')

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    names = [s.name for s in pipeline.state.rbx_place.scripts]
    assert "Inventory" in names
    inv = next(s for s in pipeline.state.rbx_place.scripts if s.name == "Inventory")
    assert inv.script_type == "ModuleScript"


def test_rehydration_records_source_path_for_nested_files(tmp_path):
    """Lossless rehydration (P0-3): each rehydrated RbxScript must record its
    relative disk path so the final rewrite loop can update the original
    file in-place instead of dumping everything into scripts/ root.
    """
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    (scripts_dir / "animations").mkdir(parents=True)
    (scripts_dir / "animation_data").mkdir(parents=True)
    (scripts_dir / "scriptable_objects").mkdir(parents=True)

    (scripts_dir / "Top.luau").write_text("-- top-level\n")
    (scripts_dir / "animations" / "Door.luau").write_text("-- anim\n")
    (scripts_dir / "animation_data" / "DoorData.luau").write_text(
        "local M = {}\nreturn M\n"
    )
    (scripts_dir / "scriptable_objects" / "Inventory.luau").write_text(
        "local I = {}\nreturn I\n"
    )

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    by_name = {s.name: s for s in pipeline.state.rbx_place.scripts}
    assert by_name["Top"].source_path == "Top.luau"
    assert by_name["Door"].source_path == "animations/Door.luau"
    assert by_name["DoorData"].source_path == "animation_data/DoorData.luau"
    assert by_name["Inventory"].source_path == "scriptable_objects/Inventory.luau"


def test_final_rewrite_honors_source_path_for_nested_scripts(tmp_path):
    """write_output's trailing rewrite loop must use RbxScript.source_path so
    an edit applied in-memory (after rehydration, during require injection
    / reclassification) lands on the nested-dir file it came from — not in a
    duplicate copy at scripts/<name>.luau.
    """
    from core.roblox_types import RbxScript, RbxPart

    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    so_dir = scripts_dir / "scriptable_objects"
    anim_dir = scripts_dir / "animations"
    so_dir.mkdir(parents=True)
    anim_dir.mkdir(parents=True)

    # Original on-disk contents (what rehydration would read).
    (so_dir / "Inventory.luau").write_text("-- original\n")
    (anim_dir / "Door.luau").write_text("-- original\n")

    pipeline.state.rbx_place.scripts = [
        RbxScript(
            name="Inventory",
            source="-- REWRITTEN\n",
            script_type="ModuleScript",
            source_path="scriptable_objects/Inventory.luau",
        ),
        RbxScript(
            name="Door",
            source="-- REWRITTEN\n",
            script_type="Script",
            source_path="animations/Door.luau",
        ),
    ]

    # Exercise just the final rewrite loop in isolation — mirroring the code
    # at the tail of write_output.
    for s in pipeline.state.rbx_place.scripts:
        out_path = scripts_dir / s.source_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(s.source, encoding="utf-8")

    assert (so_dir / "Inventory.luau").read_text() == "-- REWRITTEN\n"
    assert (anim_dir / "Door.luau").read_text() == "-- REWRITTEN\n"
    # No duplicate at the top level — the bug before this fix.
    assert not (scripts_dir / "Inventory.luau").exists()
    assert not (scripts_dir / "Door.luau").exists()


def test_unconverted_md_aggregates_material_warnings(tmp_path):
    """Phase 4.2: material warnings surface as UNCONVERTED.md entries."""
    from converter.animation_converter import AnimationConversionResult
    from converter.material_mapper import MaterialMapping

    pipeline = _make_pipeline(tmp_path)
    pipeline.state.animation_result = AnimationConversionResult(unconverted=[])
    pipeline.state.material_mappings = {
        "guid-a": MaterialMapping(
            material_name="SandShader",
            shader_name="Some/CustomShader",
            warnings=["Unsupported shader: Some/CustomShader"],
        ),
        "guid-b": MaterialMapping(
            material_name="VCOLMat",
            shader_name="Legacy Shaders/VertexLit",
            uses_vertex_colors=True,
            warnings=["Vertex-color baking skipped: no mesh referrers found for this material"],
        ),
    }

    pipeline._write_unconverted_md()
    md = (pipeline.output_dir / "UNCONVERTED.md").read_text()
    assert "## material" in md
    assert "SandShader" in md
    assert "VCOLMat" in md
    assert "Unsupported shader" in md
    assert "no mesh referrers" in md


def test_unconverted_md_written_when_entries_exist(tmp_path):
    """Phase 4.5b: ``_write_unconverted_md`` aggregates animation entries
    into ``UNCONVERTED.md`` grouped by category.
    """
    from converter.animation_converter import AnimationConversionResult
    pipeline = _make_pipeline(tmp_path)
    pipeline.state.animation_result = AnimationConversionResult(
        unconverted=[
            {"category": "animator_controller",
             "item": "Enemy.controller",
             "reason": "binary-encoded .controller"},
            {"category": "blend_tree",
             "item": "Player/Move",
             "reason": "2D BlendType=1 not supported"},
        ],
    )
    pipeline._write_unconverted_md()
    md = (pipeline.output_dir / "UNCONVERTED.md").read_text()
    assert "## animator_controller" in md
    assert "Enemy.controller" in md
    assert "## blend_tree" in md
    assert "Player/Move" in md


def test_unconverted_md_removed_when_no_entries(tmp_path):
    """An empty unconverted list means UNCONVERTED.md must not linger."""
    from converter.animation_converter import AnimationConversionResult
    pipeline = _make_pipeline(tmp_path)
    # Seed a stale file as if a prior run had emitted it.
    stale = pipeline.output_dir / "UNCONVERTED.md"
    stale.write_text("# Stale\n")
    pipeline.state.animation_result = AnimationConversionResult(unconverted=[])

    pipeline._write_unconverted_md()
    assert not stale.exists()


def test_rehydration_round_trip_animation_data_preserves_layout(tmp_path):
    """Phase 4.11: animator controller data modules live in `animation_data/`.
    Seed one, rehydrate, mutate in-memory, run the same final rewrite
    policy as write_output, and assert the file is overwritten in place
    with no duplicate at the scripts/ root.
    """
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    (scripts_dir / "animation_data").mkdir(parents=True)

    original = "-- animator controller v1 data\nlocal Data = {}\nreturn Data\n"
    (scripts_dir / "animation_data" / "Level1_PlayerAnimController.luau").write_text(original)

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    # Sanity: rehydrated, categorized as ModuleScript, source_path recorded.
    script = next(
        s for s in pipeline.state.rbx_place.scripts
        if s.name == "Level1_PlayerAnimController"
    )
    assert script.script_type == "ModuleScript"
    assert script.source_path == "animation_data/Level1_PlayerAnimController.luau"
    assert script.source == original

    # Mutate in memory (simulating require-injection / reclassification).
    new_source = "-- animator controller v2 data\nlocal Data = {updated=true}\nreturn Data\n"
    script.source = new_source

    # Mirror the final write_output rewrite loop — source_path routes back.
    for s in pipeline.state.rbx_place.scripts:
        if getattr(s, "source_path", None):
            out_path = scripts_dir / s.source_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(s.source, encoding="utf-8")

    assert (scripts_dir / "animation_data" / "Level1_PlayerAnimController.luau").read_text() == new_source
    # No duplicate at scripts/ root.
    assert not (scripts_dir / "Level1_PlayerAnimController.luau").exists()


def test_rehydration_round_trip_nested_dirs_preserves_layout(tmp_path):
    """End-to-end: seed nested scripts, rehydrate, mutate in memory, run the
    final rewrite loop as-defined in pipeline.write_output, and confirm the
    on-disk layout matches the seeded layout with updated content — no
    duplicates at scripts/ root, no lost nested files.
    """
    pipeline = _make_pipeline(tmp_path)
    scripts_dir = pipeline.output_dir / "scripts"
    (scripts_dir / "animations").mkdir(parents=True)
    (scripts_dir / "scriptable_objects").mkdir(parents=True)

    (scripts_dir / "GameManager.luau").write_text("-- v1 GameManager\n")
    (scripts_dir / "animations" / "DoorOpen.luau").write_text("-- v1 DoorOpen\n")
    (scripts_dir / "scriptable_objects" / "Config.luau").write_text(
        "local C = {}\nreturn C\n"
    )

    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    for s in pipeline.state.rbx_place.scripts:
        s.source = f"-- v2 {s.name}\n"

    # Copy-paste of the final rewrite block in write_output (post-fix).
    for s in pipeline.state.rbx_place.scripts:
        if getattr(s, "source_path", None):
            out_path = scripts_dir / s.source_path
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(s.source, encoding="utf-8")
            continue
        luau_path = scripts_dir / f"{s.name}.luau"
        anim_path = scripts_dir / "animations" / f"{s.name}.luau"
        if anim_path.exists():
            anim_path.write_text(s.source, encoding="utf-8")
        elif luau_path.exists() or not (scripts_dir / "animations").exists():
            luau_path.write_text(s.source, encoding="utf-8")

    assert (scripts_dir / "GameManager.luau").read_text() == "-- v2 GameManager\n"
    assert (scripts_dir / "animations" / "DoorOpen.luau").read_text() == "-- v2 DoorOpen\n"
    assert (scripts_dir / "scriptable_objects" / "Config.luau").read_text() == "-- v2 Config\n"
    # Each script exists in exactly one place.
    assert sorted(p.relative_to(scripts_dir).as_posix() for p in scripts_dir.rglob("*.luau")) == [
        "GameManager.luau",
        "animations/DoorOpen.luau",
        "scriptable_objects/Config.luau",
    ]
