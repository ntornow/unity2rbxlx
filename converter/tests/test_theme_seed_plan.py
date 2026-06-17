"""Unit-3 theme registration — pipeline ownership-derivation, drain-bind
detector, and the recompute/allowlist seam (design-phase2 §2.2/§2.2a/§1.4).

Covers AC-6 (generic no-op), AC-7 (fail-loud abstain), AC-9 (mismatched
appender rejected), AC-10-P1 (seed reaches the EMITTED SceneRuntimePlan via
the _PLAN_KEYS_FOR_HOST allowlist) and AC-10-P2 (no-retranspile resume
recompute). The drive-real-wiring tests construct a Pipeline against a fake
Unity project and call the production ``_build_theme_seed_plan`` + the real
``generate_scene_runtime_plan_module`` emit path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.autogen import (
    _PLAN_KEYS_FOR_HOST,
    generate_scene_runtime_plan_module,
)
from converter.pipeline import (
    Pipeline,
    _derive_appender_name,
    _derive_cs_load_ownership,
    _derive_drain_field,
    _drain_field_is_writable_table,
)
from core.roblox_types import RbxPlace, RbxScript
from core.unity_types import GuidEntry, GuidIndex


# --- the REAL transpiled ThemeDatabase body (captured output) --------------
THEME_DB_LUAU = """\
local ThemeDatabase = {}
local themeDataList = nil
local m_Loaded = false
function ThemeDatabase.dictionnary()
	return themeDataList
end
function ThemeDatabase.loaded()
	return m_Loaded
end
function ThemeDatabase.GetThemeData(type)
	if themeDataList == nil then return nil end
	return themeDataList[type]
end
function ThemeDatabase.LoadDatabase()
	if themeDataList == nil then
		themeDataList = {}
		for _, op in ipairs(ThemeDatabase._pendingThemeData) do
			if op ~= nil then
				if themeDataList[op.themeName] == nil then
					themeDataList[op.themeName] = op
				end
			end
		end
		m_Loaded = true
	end
end
ThemeDatabase._pendingThemeData = {}
function ThemeDatabase.Register(themeData)
	table.insert(ThemeDatabase._pendingThemeData, themeData)
end
return ThemeDatabase
"""

# The originating C# (the deterministic upstream the converter re-reads).
THEME_DB_CS = """\
public class ThemeDatabase {
    static protected Dictionary<string, ThemeData> themeDataList;
    static public void LoadDatabase() {
        if (themeDataList == null) {
            themeDataList = new Dictionary<string, ThemeData>();
            Addressables.LoadAssetsAsync<ThemeData>("themeData", op => {
                if (!themeDataList.ContainsKey(op.themeName))
                    themeDataList.Add(op.themeName, op);
            });
        }
    }
}
"""


# ---------------------------------------------------------------------------
# Pure helpers: C# ownership derivation
# ---------------------------------------------------------------------------

class TestCsOwnershipDerivation:
    def test_derives_label_key_and_method(self):
        own = _derive_cs_load_ownership(THEME_DB_CS)
        assert own is not None
        assert own.label == "themeData"        # NOT hardcoded — read from <...>("themeData"
        assert own.key_field == "themeName"    # from Add(op.themeName, op)
        assert own.load_method_name == "LoadDatabase"

    def test_no_load_call_yields_none(self):
        """AC-6: a module with no LoadAssetsAsync<T>(label,…) is not an SO DB."""
        assert _derive_cs_load_ownership("public class Foo { void Bar() {} }") is None

    def test_label_not_hardcoded(self):
        cs = THEME_DB_CS.replace('"themeData"', '"weatherData"').replace(
            "op.themeName", "op.weatherName")
        own = _derive_cs_load_ownership(cs)
        assert own is not None
        assert own.label == "weatherData"
        assert own.key_field == "weatherName"

    def test_missing_key_field_is_optional(self):
        cs = THEME_DB_CS.replace("themeDataList.Add(op.themeName, op)",
                                 "themeDataList[op.GetHashCode()] = op")
        own = _derive_cs_load_ownership(cs)
        assert own is not None
        assert own.key_field is None  # seed still supplies instances; abstain off

    def test_key_field_scoped_to_callback_not_later_unrelated_add(self):
        """codex P1.3: when the ``LoadAssetsAsync<T>(...)`` callback indexes by an
        EXPRESSION (no ``.Add(op.F, op)`` inside the callback span) and a LATER
        unrelated ``.Add(op.otherKey, op)`` exists elsewhere in the method, the
        key field must NOT be mis-derived from that unrelated Add — it must stay
        ``None`` (the callback span has no ``op.F`` Add).

        Pre-fix (whole-file ``.search(cs_source, m.end())``) mis-derives
        ``key_field='otherKey'`` from the unrelated Add; post-fix the bounded
        callback-span search finds none → ``None``.
        """
        cs = """\
public class ThemeDatabase {
    static protected Dictionary<string, ThemeData> themeDataList;
    static Dictionary<string, ThemeData> _legacy = new Dictionary<string, ThemeData>();
    static public void LoadDatabase() {
        if (themeDataList == null) {
            themeDataList = new Dictionary<string, ThemeData>();
            Addressables.LoadAssetsAsync<ThemeData>("themeData", op => {
                themeDataList[op.themeName] = op;
            });
        }
        // an UNRELATED later Add in the SAME method, OUTSIDE the callback span:
        _legacy.Add(op.otherKey, op);
    }
}
"""
        own = _derive_cs_load_ownership(cs)
        assert own is not None
        assert own.label == "themeData"
        assert own.key_field is None   # NOT 'otherKey' from the unrelated Add

    def test_bare_load_assets_async_without_type_arg_not_matched(self):
        """The ``<T>`` type arg is load-bearing for the match: a bare
        ``LoadAssetsAsync("themeData", …)`` without ``<T>`` is not the typed SO
        load and is not derived as ownership."""
        cs = THEME_DB_CS.replace("LoadAssetsAsync<ThemeData>", "LoadAssetsAsync")
        assert _derive_cs_load_ownership(cs) is None


# ---------------------------------------------------------------------------
# Pure helpers: drain-bind detector (D16)
# ---------------------------------------------------------------------------

class TestDrainBindDetector:
    def test_derives_drain_field_from_load_method(self):
        assert _derive_drain_field(THEME_DB_LUAU, "LoadDatabase") == "_pendingThemeData"

    def test_appender_binds_to_drain_field(self):
        """Register binds because its table.insert target == the drain field."""
        assert _derive_appender_name(THEME_DB_LUAU, "_pendingThemeData") == "Register"

    def test_mismatched_appender_rejected(self):
        """AC-9: a public appender feeding a DIFFERENT list than LoadDatabase
        drains is REJECTED (no name match, only field-identity bind)."""
        src = THEME_DB_LUAU.replace(
            "function ThemeDatabase.Register(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end",
            "function ThemeDatabase.Register(themeData)\n"
            "\ttable.insert(ThemeDatabase._someOtherList, themeData)\n"
            "end",
        )
        # The appender now feeds _someOtherList, not the drained _pendingThemeData.
        assert _derive_appender_name(src, "_pendingThemeData") is None

    def test_no_drain_field_when_load_method_absent(self):
        """AC-7: no recognizable ipairs(<field>) drain → None (→ fail-loud)."""
        src = THEME_DB_LUAU.replace("ipairs(ThemeDatabase._pendingThemeData)",
                                    "pairs(somethingElse())")
        assert _derive_drain_field(src, "LoadDatabase") is None

    def test_drain_field_writable_table_detected(self):
        assert _drain_field_is_writable_table(THEME_DB_LUAU, "_pendingThemeData")
        assert not _drain_field_is_writable_table(THEME_DB_LUAU, "_nope")

    def test_drain_field_ambiguous_when_two_distinct_loops_abstains(self):
        """codex P1.2: LoadDatabase iterates an EARLIER unrelated list and the
        real pending list. Two distinct candidates → abstain (None), never
        silently bind the FIRST ipairs.

        Pre-fix (``re.search`` returns the first match) binds ``_warmup`` (the
        wrong surface); post-fix it abstains.
        """
        src = THEME_DB_LUAU.replace(
            "function ThemeDatabase.LoadDatabase()\n"
            "\tif themeDataList == nil then\n"
            "\t\tthemeDataList = {}\n",
            "function ThemeDatabase.LoadDatabase()\n"
            "\tif themeDataList == nil then\n"
            "\t\tthemeDataList = {}\n"
            "\t\tfor _, w in ipairs(ThemeDatabase._warmup) do\n"
            "\t\t\tlocal _ = w\n"
            "\t\tend\n",
        )
        assert _derive_drain_field(src, "LoadDatabase") is None

    def test_single_drain_loop_still_binds(self):
        """A lone ipairs(<field>) loop (the real shape) still binds — the
        ambiguity guard does not regress the unambiguous case."""
        assert _derive_drain_field(THEME_DB_LUAU, "LoadDatabase") == "_pendingThemeData"

    def test_multiple_appenders_to_drain_field_ambiguous(self):
        """codex P1.2: TWO public fns insert into the drained field (e.g. the
        real ``Register`` PLUS a ``SeedDefaults`` helper) → ambiguous, no single
        ingress can be chosen → returns the ambiguity sentinel (NOT the first).

        Pre-fix (return first match) silently picks ``Register``; post-fix it
        signals ambiguity so the pipeline abstains loud.
        """
        from converter.pipeline import _AMBIGUOUS_APPENDER
        src = THEME_DB_LUAU.replace(
            "function ThemeDatabase.Register(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end",
            "function ThemeDatabase.Register(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end\n"
            "function ThemeDatabase.SeedDefaults(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end",
        )
        assert _derive_appender_name(src, "_pendingThemeData") is _AMBIGUOUS_APPENDER

    def test_single_appender_still_binds(self):
        """The lone-appender case still returns the name — ambiguity guard does
        not regress the unambiguous bind."""
        assert _derive_appender_name(THEME_DB_LUAU, "_pendingThemeData") == "Register"

    def test_seed_defaults_helper_to_other_list_not_bound(self):
        """A ``SeedDefaults``-style helper that inserts into a DIFFERENT list than
        the drain field is ignored; the real ``Register`` (bound to the drain
        field) is selected unambiguously."""
        src = THEME_DB_LUAU.replace(
            "function ThemeDatabase.Register(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end",
            "function ThemeDatabase.SeedDefaults(themeData)\n"
            "\ttable.insert(ThemeDatabase._scratch, themeData)\n"
            "end\n"
            "function ThemeDatabase.Register(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end",
        )
        assert _derive_appender_name(src, "_pendingThemeData") == "Register"


# ---------------------------------------------------------------------------
# Integration: _build_theme_seed_plan + the EMITTED plan (drive real wiring)
# ---------------------------------------------------------------------------

def _theme_group_asset() -> str:
    return (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!114 &11400000\nMonoBehaviour:\n"
        "  m_Name: Themes\n  m_GroupName: Themes\n"
        "  m_SerializeEntries:\n"
        "  - m_GUID: dayguid\n    m_Address: themeData\n"
        "    m_SerializedLabels:\n    - themeData\n"
        "  - m_GUID: nightguid\n    m_Address: themeData\n"
        "    m_SerializedLabels:\n    - themeData\n"
    )


def _theme_group_asset_address_only() -> str:
    """A group whose entries carry an ``m_Address`` of ``themeData`` but NO
    ``m_SerializedLabels`` — so the SO guids land ONLY in ``by_address``
    (``by_label`` has no ``themeData`` entry). Models a database whose C# load
    key is an ADDRESS, not a label."""
    return (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!114 &11400000\nMonoBehaviour:\n"
        "  m_Name: Themes\n  m_GroupName: Themes\n"
        "  m_SerializeEntries:\n"
        "  - m_GUID: dayguid\n    m_Address: themeData\n"
        "  - m_GUID: nightguid\n    m_Address: themeData\n"
    )


def _make_project(tmp_path: Path, *, cs: str = THEME_DB_CS,
                  db_luau: str = THEME_DB_LUAU,
                  group_asset: str | None = None) -> Path:
    root = tmp_path / "proj"
    (root / "Assets").mkdir(parents=True)
    groups = root / "Assets" / "AddressableAssetsData" / "AssetGroups"
    groups.mkdir(parents=True)
    (groups / "Themes.asset").write_text(
        group_asset if group_asset is not None else _theme_group_asset(),
        encoding="utf-8")
    (root / "Assets" / "ThemeDatabase.cs").write_text(cs, encoding="utf-8")
    return root


def _pipeline_with_state(root: Path, tmp_path: Path, *,
                         db_luau: str = THEME_DB_LUAU) -> Pipeline:
    pipe = Pipeline(root, output_dir=tmp_path / "out", skip_upload=True)
    gi = GuidIndex(project_root=root)
    for guid in ("dayguid", "nightguid"):
        gi.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=root / "Assets" / f"{guid}.asset",
            relative_path=Path(f"Assets/{guid}.asset"),
            kind="scriptable_object",
        )
    pipe.state.guid_index = gi
    # The transpiled DB module body lives on rbx_place.scripts (rehydrated on
    # resume by materialize_and_classify in production).
    pipe.state.rbx_place = RbxPlace()
    pipe.state.rbx_place.scripts = [
        RbxScript(name="ThemeDatabase", source=db_luau,
                  script_type="ModuleScript", parent_path="ReplicatedStorage"),
    ]
    return pipe


def _scene_runtime_with_so_map() -> dict[str, object]:
    return {
        "scriptable_objects": {
            "dayguid": "ReplicatedStorage.ThemeData_Day",
            "nightguid": "ReplicatedStorage.ThemeData_Night",
        },
    }


class TestBuildThemeSeedPlanIntegration:
    def test_seed_record_built_and_drain_bound(self, tmp_path):
        root = _make_project(tmp_path)
        # rbx_place needs a real instance; construct via a minimal place.
        pipe = _pipeline_with_state(root, tmp_path)
        sr = _scene_runtime_with_so_map()
        pipe._build_theme_seed_plan(sr)
        seeds = sr["addressable_db_seeds"]
        assert isinstance(seeds, list) and len(seeds) == 1
        seed = seeds[0]
        assert seed["db_module_path"] == "ReplicatedStorage.ThemeDatabase"
        assert seed["load_method_name"] == "LoadDatabase"
        assert seed["drain_field"] == "_pendingThemeData"
        assert seed["appender_name"] == "Register"   # drain-bound (D16)
        assert seed["key_field"] == "themeName"
        assert seed["so_module_paths"] == [
            "ReplicatedStorage.ThemeData_Day",
            "ReplicatedStorage.ThemeData_Night",
        ]

    def test_seed_resolves_database_loaded_by_address(self, tmp_path):
        """codex P1 (phase2): Unity ``LoadAssetsAsync<T>(key)`` accepts a LABEL
        *or* an ADDRESS. A database whose load key is an ADDRESS (present in
        ``by_address``, absent from ``by_label``) must still seed — resolved via
        the by_address index.

        Pre-fix (``so_addr.by_label.get(ownership.label)`` only) finds nothing
        and silently emits no seed → empty registry; post-fix the union resolves
        the guids via by_address."""
        root = _make_project(
            tmp_path, group_asset=_theme_group_asset_address_only())
        pipe = _pipeline_with_state(root, tmp_path)
        sr = _scene_runtime_with_so_map()
        pipe._build_theme_seed_plan(sr)
        seeds = sr["addressable_db_seeds"]
        assert isinstance(seeds, list) and len(seeds) == 1
        seed = seeds[0]
        assert seed["db_module_path"] == "ReplicatedStorage.ThemeDatabase"
        assert seed["so_module_paths"] == [
            "ReplicatedStorage.ThemeData_Day",
            "ReplicatedStorage.ThemeData_Night",
        ]

    def test_db_shaped_module_with_no_resolvable_guids_warns_loud(
            self, tmp_path, caplog):
        """codex P1 (phase2): a derived DB-shaped module (LoadAssetsAsync<T>
        ownership + a recognizable drain surface) whose load key resolves to NO
        emitted SO guids in EITHER index is a likely-dead registry — warn loud
        rather than silently skip. The C# key (``otherKey``) matches no group
        entry, so neither by_label nor by_address has it."""
        cs = THEME_DB_CS.replace('"themeData"', '"otherKey"')
        root = _make_project(tmp_path, cs=cs)
        pipe = _pipeline_with_state(root, tmp_path)
        sr = _scene_runtime_with_so_map()
        with caplog.at_level("WARNING"):
            pipe._build_theme_seed_plan(sr)
        assert "addressable_db_seeds" not in sr
        assert any("registry would be empty" in r.message
                   for r in caplog.records)

    def test_non_db_module_with_no_drain_does_not_warn(self, tmp_path, caplog):
        """The fail-loud warning is scoped to DB-shaped modules: a module that
        derives LoadAssetsAsync<T> ownership but has NO recognizable drain
        surface (not a registry) and resolves to no guids must NOT emit the
        empty-registry warning — no log noise for ordinary modules."""
        cs = THEME_DB_CS.replace('"themeData"', '"otherKey"')
        no_drain = THEME_DB_LUAU.replace(
            "ipairs(ThemeDatabase._pendingThemeData)", "pairs(mystery())")
        root = _make_project(tmp_path, cs=cs)
        pipe = _pipeline_with_state(root, tmp_path, db_luau=no_drain)
        sr = _scene_runtime_with_so_map()
        with caplog.at_level("WARNING"):
            pipe._build_theme_seed_plan(sr)
        assert "addressable_db_seeds" not in sr
        assert not any("registry would be empty" in r.message
                       for r in caplog.records)

    def test_emitted_plan_carries_seed_via_allowlist(self, tmp_path):
        """AC-10-P1: the seed survives the _PLAN_KEYS_FOR_HOST filter into the
        EMITTED SceneRuntimePlan ModuleScript (the artifact the runtime
        requires), not just conversion_plan.json."""
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path)
        sr = _scene_runtime_with_so_map()
        pipe._build_theme_seed_plan(sr)
        module = generate_scene_runtime_plan_module(sr)
        assert "addressable_db_seeds" in module.source
        assert "ReplicatedStorage.ThemeData_Day" in module.source
        assert "Register" in module.source

    def test_dropping_allowlist_key_elides_seed(self, tmp_path, monkeypatch):
        """AC-10-P1 negative guard / edge 9: if the allowlist key were removed,
        the seed is ELIDED from the emitted plan (silent dead registry). This
        proves the allowlist membership is load-bearing, not incidental."""
        import converter.autogen as autogen
        without = tuple(k for k in _PLAN_KEYS_FOR_HOST if k != "addressable_db_seeds")
        monkeypatch.setattr(autogen, "_PLAN_KEYS_FOR_HOST", without)
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path)
        sr = _scene_runtime_with_so_map()
        pipe._build_theme_seed_plan(sr)
        module = generate_scene_runtime_plan_module(sr)
        assert "addressable_db_seeds" not in module.source

    def test_allowlist_contains_seed_key(self):
        assert "addressable_db_seeds" in _PLAN_KEYS_FOR_HOST

    def test_no_theme_db_is_noop(self, tmp_path):
        """AC-6: a project whose only C# has no LoadAssetsAsync<T> call emits
        zero seeds and never crashes."""
        root = _make_project(tmp_path, cs="public class Foo { void Bar(){} }")
        pipe = _pipeline_with_state(root, tmp_path)
        # Rename the module so its C# (Foo.cs) is what gets re-read.
        pipe.state.rbx_place.scripts = [
            RbxScript(name="Foo", source=THEME_DB_LUAU,
                      script_type="ModuleScript", parent_path="ReplicatedStorage"),
        ]
        (root / "Assets" / "Foo.cs").write_text(
            "public class Foo { void Bar(){} }", encoding="utf-8")
        sr = _scene_runtime_with_so_map()
        pipe._build_theme_seed_plan(sr)
        assert "addressable_db_seeds" not in sr

    def test_fail_loud_abstain_when_no_drain(self, tmp_path, caplog):
        """AC-7: drain can't be derived → WARNING + NO record (loud abstain,
        never a silent seed onto an unproven surface)."""
        broken = THEME_DB_LUAU.replace("ipairs(ThemeDatabase._pendingThemeData)",
                                       "pairs(mystery())")
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path, db_luau=broken)
        sr = _scene_runtime_with_so_map()
        with caplog.at_level("WARNING"):
            pipe._build_theme_seed_plan(sr)
        assert "addressable_db_seeds" not in sr
        assert any("abstaining" in r.message for r in caplog.records)

    def test_mismatched_appender_and_no_writable_drain_abstains(self, tmp_path, caplog):
        """AC-9 + edge 8: the only public appender feeds a DIFFERENT list and the
        drain field is not a declared writable table → abstain + warn."""
        src = THEME_DB_LUAU.replace(
            "table.insert(ThemeDatabase._pendingThemeData, themeData)",
            "table.insert(ThemeDatabase._someOtherList, themeData)",
        ).replace("ThemeDatabase._pendingThemeData = {}", "-- no pending decl")
        # LoadDatabase still iterates _pendingThemeData, but nothing binds to it
        # and it is not a declared writable table → no proven surface.
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path, db_luau=src)
        sr = _scene_runtime_with_so_map()
        with caplog.at_level("WARNING"):
            pipe._build_theme_seed_plan(sr)
        assert "addressable_db_seeds" not in sr
        assert any("abstaining" in r.message for r in caplog.records)

    def test_resume_no_retranspile_recomputes_seed(self, tmp_path):
        """AC-10-P2: on a no-retranspile resume ``transpilation_result is None``;
        the seed is RECOMPUTED from rehydrated state (the DB body on
        rbx_place.scripts + the .cs + the addressables groups off disk), so it is
        NOT transpile-gated and still reaches the emitted plan."""
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path)
        assert pipe.state.transpilation_result is None  # the resume condition
        sr = _scene_runtime_with_so_map()
        pipe._build_theme_seed_plan(sr)
        assert isinstance(sr["addressable_db_seeds"], list)
        assert len(sr["addressable_db_seeds"]) == 1
        module = generate_scene_runtime_plan_module(sr)
        assert "addressable_db_seeds" in module.source

    def test_entrypoints_emit_seed_call_between_new_and_start(self):
        """AC-5 (emit): both entrypoint sources call seedAddressableDatabases in
        the slot between SceneRuntime.new and engine:start."""
        from converter.autogen import (
            _SCENE_RUNTIME_CLIENT_SOURCE,
            _SCENE_RUNTIME_SERVER_SOURCE,
        )
        for src, domain in (
            (_SCENE_RUNTIME_CLIENT_SOURCE, "client"),
            (_SCENE_RUNTIME_SERVER_SOURCE, "server"),
        ):
            i_new = src.index("SceneRuntime.new(services, Plan)")
            i_seed = src.index("SceneRuntime.seedAddressableDatabases(Plan, services)")
            i_start = src.index(f'engine:start("{domain}")')
            assert i_new < i_seed < i_start

    def test_stale_seed_cleared_on_abstain(self, tmp_path, caplog):
        """codex P1.1: a scene_runtime that ALREADY carries a seed from a prior
        run must have it CLEARED when this run abstains — never leave a STALE
        record seeding a now-wrong/dead registry. Recompute is authoritative.

        Pre-fix (set only when seeds truthy) leaves the stale value in place;
        post-fix the entry-reset clears it before any abstain.
        """
        broken = THEME_DB_LUAU.replace("ipairs(ThemeDatabase._pendingThemeData)",
                                       "pairs(mystery())")
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path, db_luau=broken)
        sr = _scene_runtime_with_so_map()
        # A STALE seed from a prior run (a now-wrong record).
        sr["addressable_db_seeds"] = [{
            "db_module_path": "ReplicatedStorage.StaleDB",
            "load_method_name": "LoadDatabase",
            "drain_field": "_stale",
            "appender_name": "StaleAppend",
            "key_field": "staleKey",
            "so_module_paths": ["ReplicatedStorage.Stale_SO"],
        }]
        with caplog.at_level("WARNING"):
            pipe._build_theme_seed_plan(sr)
        # The abstain path cleared the stale key entirely — no silent dead seed.
        assert "addressable_db_seeds" not in sr
        assert any("abstaining" in r.message for r in caplog.records)

    def test_stale_seed_overwritten_on_successful_rebuild(self, tmp_path):
        """A successful recompute REPLACES a prior stale seed (does not append to
        or leave it). Authoritative recompute each run."""
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path)
        sr = _scene_runtime_with_so_map()
        sr["addressable_db_seeds"] = [{
            "db_module_path": "ReplicatedStorage.StaleDB",
            "load_method_name": "X", "drain_field": "_stale",
            "appender_name": None, "key_field": None,
            "so_module_paths": ["ReplicatedStorage.Stale_SO"],
        }]
        pipe._build_theme_seed_plan(sr)
        seeds = sr["addressable_db_seeds"]
        assert len(seeds) == 1
        assert seeds[0]["db_module_path"] == "ReplicatedStorage.ThemeDatabase"

    def test_multiple_appenders_abstains_loud(self, tmp_path, caplog):
        """codex P1.2 (integration): two public fns insert into the drained
        field → ambiguous → the pipeline abstains loud (warn + NO record), never
        seeds through an arbitrarily-chosen surface."""
        src = THEME_DB_LUAU.replace(
            "function ThemeDatabase.Register(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end",
            "function ThemeDatabase.Register(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end\n"
            "function ThemeDatabase.SeedDefaults(themeData)\n"
            "\ttable.insert(ThemeDatabase._pendingThemeData, themeData)\n"
            "end",
        )
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path, db_luau=src)
        sr = _scene_runtime_with_so_map()
        with caplog.at_level("WARNING"):
            pipe._build_theme_seed_plan(sr)
        assert "addressable_db_seeds" not in sr
        assert any("abstaining" in r.message for r in caplog.records)

    def test_earlier_unrelated_drain_loop_abstains_loud(self, tmp_path, caplog):
        """codex P1.2 (integration): LoadDatabase iterates an earlier unrelated
        list AND the real pending list → ambiguous drain → abstain loud rather
        than silently bind the FIRST loop."""
        src = THEME_DB_LUAU.replace(
            "function ThemeDatabase.LoadDatabase()\n"
            "\tif themeDataList == nil then\n"
            "\t\tthemeDataList = {}\n",
            "function ThemeDatabase.LoadDatabase()\n"
            "\tif themeDataList == nil then\n"
            "\t\tthemeDataList = {}\n"
            "\t\tfor _, w in ipairs(ThemeDatabase._warmup) do\n"
            "\t\t\tlocal _ = w\n"
            "\t\tend\n",
        )
        root = _make_project(tmp_path)
        pipe = _pipeline_with_state(root, tmp_path, db_luau=src)
        sr = _scene_runtime_with_so_map()
        with caplog.at_level("WARNING"):
            pipe._build_theme_seed_plan(sr)
        assert "addressable_db_seeds" not in sr
        assert any("abstaining" in r.message for r in caplog.records)

    def test_keyless_so_still_emits_record(self, tmp_path):
        """A missing key_field (DB indexes by non-op.field) still seeds; the
        shim handles key-less abstain at runtime (key_field=None → no filter)."""
        cs = THEME_DB_CS.replace("themeDataList.Add(op.themeName, op)",
                                 "themeDataList[Hash(op)] = op")
        root = _make_project(tmp_path, cs=cs)
        pipe = _pipeline_with_state(root, tmp_path)
        sr = _scene_runtime_with_so_map()
        pipe._build_theme_seed_plan(sr)
        seed = sr["addressable_db_seeds"][0]
        assert seed["key_field"] is None
