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


# --------------------------------------------------------------------------- #
# Dead-module exemption (M1): a consumable subclass module the boot shim
# materializes is LIVE BY CONSTRUCTION even when its canonical body is inert.
# Without the exemption it is flagged Roblox-dead and rerouted to a ``.new``-less
# inert stub -> every consumable element is dropped -> the feature silently no-ops.
# --------------------------------------------------------------------------- #

from converter.code_transpiler import TranspilationResult, TranspiledScript

# A subclass C# whose API surface is MEASURED (>=3 distinct non-structural type
# refs) AND dead-leaning (0% resolve to a real Roblox mapping) -> the input prior
# agrees "dead". Pairs with an inert (no genuine Roblox effect) Luau body, so
# ``classify_module_dead`` returns is_dead=True absent the exemption.
_DEAD_LEANING_SUBCLASS_CS = """\
    public class CoinMagnet : Consumable
    {
        private FizzWidgetManager fizz;
        private BuzzGizmoSystem buzz;
        private WhirlGadgetController whirl;
        public override int GetConsumableType() { return 1; }
    }
"""

# An inert canonical subclass body: a ``.new`` constructor + a constant override,
# no Instance.new / .Parent / property write -> ``is_output_inert`` True, no veto.
_INERT_SUBCLASS_LUAU = """\
local CoinMagnet = {}
CoinMagnet.__index = CoinMagnet
function CoinMagnet.new()
    return setmetatable({}, CoinMagnet)
end
function CoinMagnet:GetConsumableType()
    return 1
end
return CoinMagnet
"""


def _seeded_pipeline_with_dead_leaning_subclass(
    tmp_path: Path,
) -> tuple[Pipeline, RbxScript]:
    """A pipeline whose CoinMagnet subclass module has an inert body + a
    dead-leaning C# prior, with an ACTIVE consumable seed referencing it. The
    CoinMagnet RbxScript is returned for the caller to assert on."""
    root = _build_trash_dash_like(tmp_path)
    pipe = _pipeline_with_state(root, tmp_path)
    coin = next(s for s in pipe.state.rbx_place.scripts if s.name == "CoinMagnet")
    coin.source = _INERT_SUBCLASS_LUAU
    # Drive the FRESH-transpile branch: a TranspilationResult supplies the C#
    # prior + strategy keyed by output filename stem.
    pipe.state.transpilation_result = TranspilationResult(
        scripts=[
            TranspiledScript(
                source_path="Scripts/Types/CoinMagnet.cs",
                output_filename="CoinMagnet.luau",
                csharp_source=_DEAD_LEANING_SUBCLASS_CS,
                luau_source=_INERT_SUBCLASS_LUAU,
                strategy="ai",
                confidence=1.0,
                script_type="ModuleScript",
            ),
        ],
    )
    # The seed plan inputs live on ctx.scene_runtime (recomputed by the helper).
    pipe.ctx.scene_runtime = _scene_runtime_with_so_map()
    return pipe, coin


def test_dead_leaning_subclass_is_flagged_dead_without_exemption(tmp_path):
    """Anchor: the constructed subclass IS Roblox-dead by the classifier (so the
    exemption is genuinely load-bearing, not exempting an already-live module)."""
    from converter.roblox_dead_modules import classify_module_dead
    verdict = classify_module_dead(
        "CoinMagnet",
        csharp_source=_DEAD_LEANING_SUBCLASS_CS,
        luau_source=_INERT_SUBCLASS_LUAU,
    )
    assert verdict.is_dead, (
        "fixture invalid: the subclass must classify dead for the exemption test "
        f"to be meaningful (reason: {verdict.reason})"
    )


def test_consumable_seed_module_exempt_from_dead_set(tmp_path):
    """WITH an active consumable seed referencing it, the inert subclass is NOT
    flagged dead -- the boot shim materializes it, so it is live by construction.

    Against the pre-exemption code this FAILS: classify_module_dead returns
    is_dead=True (proven by the anchor test above) and the module lands in
    ``state.dead_modules``, then gets rerouted to a ``.new``-less stub."""
    pipe, _coin = _seeded_pipeline_with_dead_leaning_subclass(tmp_path)
    # Sanity: the seed resolves and references CoinMagnet (else the exemption set
    # is empty for an unrelated reason).
    assert "CoinMagnet" in pipe._consumable_seed_live_module_names()

    pipe._subphase_analyze_dead_modules()
    assert "CoinMagnet" not in pipe.state.dead_modules, (
        "consumable subclass was flagged Roblox-dead despite an active seed; the "
        "boot shim instantiates CoinMagnet.new, so it must be exempt"
    )


def test_consumable_seed_exemption_survives_rehydrated_resume(tmp_path):
    """The exemption survives a no-retranspile (rehydrated) assemble: the seed
    plan is recomputed from disk-resident inputs, so the resume branch (which
    reuses the persisted dead set) also drops the consumable module.

    Pre-fix: the persisted-dead CoinMagnet, being inert, is re-added to the dead
    set on resume and rerouted to a stub."""
    pipe, _coin = _seeded_pipeline_with_dead_leaning_subclass(tmp_path)
    # Force the RESUME branch: no transpilation_result, but the module was
    # persisted dead by a prior (pre-seed) run.
    pipe.state.transpilation_result = None
    pipe.ctx.dead_modules = ["CoinMagnet"]

    pipe._subphase_analyze_dead_modules()
    assert "CoinMagnet" not in pipe.state.dead_modules, (
        "rehydrated resume re-added the consumable subclass to the dead set; the "
        "seed-derived exemption must survive a no-retranspile assemble"
    )
