"""Pipeline-level test for the consumable-prototype resolver (Phase 1 §1.B).

The unit tests in ``test_consumable_db_seed.py`` inject ``db_cs_source`` directly,
so they never exercise the asset-name -> DB-CLASS-name derivation the pipeline
must perform: the emitted SO MODULE is named by the asset's ``m_Name`` (e.g.
``Consumables``) but the draining CLASS is its backing ``m_Script`` class (e.g.
``ConsumableDatabase``). Looking the C# source up by the ASSET name finds no
``.cs`` and silently produces no seed (the feature is DEAD on real trash-dash).

This drives the production ``Pipeline._build_consumable_db_seeds`` end-to-end with
that ``m_Name != class-name`` SKEW and asserts a seed IS produced — so the green
suite can no longer coexist with a dead pipeline. (Against the pre-fix
asset-name-based lookup this FAILS: no ``Consumables.cs`` exists, so the source is
None and no seed is built.)
"""

from __future__ import annotations

from pathlib import Path

from unity.guid_resolver import build_guid_index
from converter.pipeline import Pipeline
from core.roblox_types import RbxPlace, RbxScript

from tests.test_consumable_db_seed import (
    G_ASSET,
    _build_trash_dash_like,
)


def _pipeline_with_state(root: Path, tmp_path: Path) -> Pipeline:
    pipe = Pipeline(root, output_dir=tmp_path / "out", skip_upload=True)
    pipe.state.guid_index = build_guid_index(root)
    pipe.state.rbx_place = RbxPlace()
    # The emitted SO module is named by the asset m_Name ("Consumables"), NOT the
    # draining class ("ConsumableDatabase"). _module_plan_path matches on this.
    pipe.state.rbx_place.scripts = [
        RbxScript(
            name="Consumables", source="return {}",
            script_type="ModuleScript", parent_path="ServerStorage",
        ),
        RbxScript(
            name="CoinMagnet", source="return {}",
            script_type="ModuleScript", parent_path="ServerStorage",
        ),
        RbxScript(
            name="ExtraLife", source="return {}",
            script_type="ModuleScript", parent_path="ServerStorage",
        ),
    ]
    return pipe


def _scene_runtime_with_so_map() -> dict[str, object]:
    # The SO guid->module map (built by an essential phase) keyed on the asset's
    # own guid; the value is the emitted module path (named by m_Name). The
    # ``modules`` registry is what the build-time subclass module-path resolver
    # (P2 fix) joins through — each subclass stem -> its emitted module_path.
    return {
        "scriptable_objects": {G_ASSET: "ServerStorage.Consumables"},
        "modules": {
            "guidCoinMagnet": {
                "stem": "CoinMagnet", "class_name": "CoinMagnet",
                "module_path": "ServerStorage.CoinMagnet",
            },
            "guidExtraLife": {
                "stem": "ExtraLife", "class_name": "ExtraLife",
                "module_path": "ServerStorage.ExtraLife",
            },
        },
    }


def test_pipeline_seed_built_with_asset_name_class_name_skew(tmp_path):
    """The asset's m_Name is "Consumables" but its class is "ConsumableDatabase".
    The pipeline must look the drain source up by the CLASS stem (via the asset's
    m_Script guid), not the asset name, or no seed is produced. FAILS against the
    pre-fix asset-name-based lookup."""
    root = _build_trash_dash_like(tmp_path)
    # Sanity: the skew is real — the asset is named Consumables, the drain class
    # is ConsumableDatabase, and there is NO Consumables.cs in the project.
    assert (root / "Assets" / "Scripts" / "ConsumableDatabase.cs").exists()
    assert not list(root.rglob("Consumables.cs"))

    pipe = _pipeline_with_state(root, tmp_path)
    sr = _scene_runtime_with_so_map()
    pipe._build_consumable_db_seeds(sr)

    seeds = sr.get("consumable_db_seeds")
    assert isinstance(seeds, list) and len(seeds) == 1, (
        "no seed produced — the DB C# source was not found by the class stem "
        "(asset-name lookup is dead on the m_Name != class-name skew)"
    )
    seed = seeds[0]
    assert seed["db_module_path"] == "ServerStorage.Consumables"
    assert seed["array_field"] == "consumbales"
    assert [e["class_stem"] for e in seed["elements"]] == ["CoinMagnet", "ExtraLife"]
    # P2: module_path resolved at BUILD time via the modules registry (no runtime
    # stem scan). Each element carries its full collision-free module path.
    assert [e["module_path"] for e in seed["elements"]] == [
        "ServerStorage.CoinMagnet", "ServerStorage.ExtraLife",
    ]


def test_pipeline_recompute_is_pop_first_idempotent(tmp_path):
    """Recompute is authoritative (pop-first): running twice yields an identical
    seed and never accumulates/duplicates."""
    root = _build_trash_dash_like(tmp_path)
    pipe = _pipeline_with_state(root, tmp_path)
    sr = _scene_runtime_with_so_map()
    pipe._build_consumable_db_seeds(sr)
    first = sr.get("consumable_db_seeds")
    pipe._build_consumable_db_seeds(sr)
    second = sr.get("consumable_db_seeds")
    assert first == second
    assert isinstance(second, list) and len(second) == 1
