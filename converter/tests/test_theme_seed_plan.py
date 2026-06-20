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

import re
from pathlib import Path

import pytest

from converter.autogen import (
    _PLAN_KEYS_FOR_HOST,
    generate_scene_runtime_plan_module,
)
from converter.pipeline import (
    _CS_LOAD_ASSETS_ASYNC,
    _CS_METHOD_HEADER,
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


# The REAL Trash-Dash ThemeDatabase.cs shape: a COMMENT precedes the control-flow
# ``if (themeDataList == null) {`` inside the load method. The comment's trailing
# word satisfies the OLD ``[\w.]+`` "return-type" slot so the OLD regex mis-captures
# ``if`` as a method header (gate (b) — design-phase3 §1). The unit fixture above
# (THEME_DB_CS) lacks the comment, so its ``if`` never matched and the bug went
# undetected (a green-test-for-wrong-reason). This fixture has the comment.
THEME_DB_CS_REAL_SHAPE = """\
public class ThemeDatabase {
    static protected Dictionary<string, ThemeData> themeDataList;
    static public IEnumerator LoadDatabase() {
        // If not null the dictionary was already loaded.
        if (themeDataList == null) {
            themeDataList = new Dictionary<string, ThemeData>();
            yield return Addressables.LoadAssetsAsync<ThemeData>("themeData", op => {
                if (!themeDataList.ContainsKey(op.themeName))
                    themeDataList.Add(op.themeName, op);
            });
            m_Loaded = true;
        }
    }
}
"""


class TestGateBMethodHeaderRegex:
    """Gate (b) — a control-flow ``if (...) {`` preceded by a comment must NOT be
    mis-captured as the enclosing method name; ``LoadDatabase`` must win."""

    # The PRE-FIX regex (no control-keyword negative lookahead) — reconstructed
    # here to prove the bug it had and that THIS fixture exercises it.
    _OLD_HEADER = re.compile(
        r"(?:public|private|protected|internal|static|virtual|override|async|\s)+"
        r"[\w<>,.\[\]]+\s+(?P<name>[A-Za-z_]\w*)\s*\([^;{]*\)\s*\{"
    )

    def _old_enclosing(self, src, pos):
        name = None
        for hm in self._OLD_HEADER.finditer(src):
            if hm.start() > pos:
                break
            name = hm.group("name")
        return name

    def test_old_regex_miscaptures_if(self):
        """Documents the bug: the OLD regex derives ``if`` on the real shape."""
        m = _CS_LOAD_ASSETS_ASYNC.search(THEME_DB_CS_REAL_SHAPE)
        assert m is not None
        assert self._old_enclosing(THEME_DB_CS_REAL_SHAPE, m.start()) == "if"

    def test_new_regex_derives_loaddatabase(self):
        """AC1: the FIXED derivation returns ``LoadDatabase`` on the real shape."""
        own = _derive_cs_load_ownership(THEME_DB_CS_REAL_SHAPE)
        assert own is not None
        assert own.load_method_name == "LoadDatabase"
        assert own.label == "themeData"
        assert own.key_field == "themeName"

    def test_new_regex_keeps_identifier_starting_with_keyword(self):
        """``\\b`` anchoring: an identifier that merely STARTS with a control
        keyword (``ifMatched`` / ``forEachItem``) is a real method, NOT excluded."""
        src = (
            "public class C {\n"
            "  public void ifMatched(int x) { }\n"
            "  public void forEachItem() { }\n"
            "}\n"
        )
        names = [m.group("name") for m in _CS_METHOD_HEADER.finditer(src)]
        assert "ifMatched" in names
        assert "forEachItem" in names

    def test_real_themedatabase_cs_on_disk(self):
        """AC1 against the REAL on-disk Trash-Dash ThemeDatabase.cs (skipped when
        the source tree is not present)."""
        cs = Path(
            "/Users/jiazou/workspace/trash-dash/Assets/Scripts/Themes/"
            "ThemeDatabase.cs"
        )
        if not cs.exists():
            pytest.skip("trash-dash source tree not present")
        own = _derive_cs_load_ownership(cs.read_text(encoding="utf-8"))
        assert own is not None
        assert own.load_method_name == "LoadDatabase"
        assert own.label == "themeData"
        assert own.key_field == "themeName"

    def test_real_characterdatabase_cs_load_method(self):
        """AC2 (generality): CharacterDatabase.cs (a ``<GameObject>`` roster DB)
        also derives ``LoadDatabase``, not ``if``."""
        cs = Path(
            "/Users/jiazou/workspace/trash-dash/Assets/Scripts/Characters/"
            "CharacterDatabase.cs"
        )
        if not cs.exists():
            pytest.skip("trash-dash source tree not present")
        own = _derive_cs_load_ownership(cs.read_text(encoding="utf-8"))
        assert own is not None
        assert own.load_method_name == "LoadDatabase"


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
        """Unity ``LoadAssetsAsync<T>(key)`` accepts a LABEL *or* an ADDRESS. A
        database whose load key is an ADDRESS (in ``by_address``, absent from
        ``by_label``) must still seed — resolved via the by_address index."""
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

    def test_two_distinct_dbs_sharing_one_key_both_seeded(self, tmp_path):
        """codex P1 (phase2): two DISTINCT database modules that legitimately
        load the SAME Addressables key each need their OWN seed. Dedupe must key
        off DB IDENTITY (module path), NOT the load key.

        Pre-fix (``seeded_keys`` keyed by ``ownership.label``) silently suppresses
        the SECOND DB → it boots with an EMPTY registry; post-fix (dedupe by
        ``db_module_path``) BOTH DBs get a seed row.
        """
        # A second DB module with a DIFFERENT name + class but the SAME
        # ``LoadAssetsAsync<T>("themeData")`` key (shared label) and its own
        # distinct drain/appender field names (still a valid registry).
        second_cs = (
            THEME_DB_CS.replace("class ThemeDatabase", "class ThemeMirror")
        )
        second_luau = (
            THEME_DB_LUAU.replace("ThemeDatabase", "ThemeMirror")
        )
        root = _make_project(tmp_path)
        # Second DB's .cs is re-read by name (ThemeMirror.cs).
        (root / "Assets" / "ThemeMirror.cs").write_text(second_cs, encoding="utf-8")
        pipe = _pipeline_with_state(root, tmp_path)
        # BOTH DB module bodies live on rbx_place.scripts (rehydrated state).
        pipe.state.rbx_place.scripts = [
            RbxScript(name="ThemeDatabase", source=THEME_DB_LUAU,
                      script_type="ModuleScript", parent_path="ReplicatedStorage"),
            RbxScript(name="ThemeMirror", source=second_luau,
                      script_type="ModuleScript", parent_path="ReplicatedStorage"),
        ]
        sr = _scene_runtime_with_so_map()
        pipe._build_theme_seed_plan(sr)
        seeds = sr["addressable_db_seeds"]
        assert isinstance(seeds, list) and len(seeds) == 2, (
            "both distinct DBs sharing one key must each get a seed"
        )
        paths = {s["db_module_path"] for s in seeds}
        assert paths == {
            "ReplicatedStorage.ThemeDatabase",
            "ReplicatedStorage.ThemeMirror",
        }
        # Each seed carries the full SO write surface (neither is dropped).
        for seed in seeds:
            assert seed["so_module_paths"] == [
                "ReplicatedStorage.ThemeData_Day",
                "ReplicatedStorage.ThemeData_Night",
            ]

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


# ---------------------------------------------------------------------------
# Pipeline orchestration: _lower_so_db_consumers (drive REAL wiring end-to-end)
# ---------------------------------------------------------------------------
# These drive the production write_output method on a real Pipeline against the
# tmp Unity project — exercising the input derivation it owns (parse_addressables
# -> resolve_scriptable_object_addressables; the per-module .cs re-read +
# _derive_cs_load_ownership -> load_method_by_path; the read-only roster
# re-derivation -> roster_claimed_paths) AND the fail-closed re-raise — NOT just
# the pure module functions in test_so_db_consumer_lowering.py.

# The REAL closure-private keyed-dict transpiled shape (a ``local <dict> = nil``
# upvalue + a never-called ``addTheme`` closure; no ipairs/table.insert). This is
# the shape the SO-DB lowering supersedes — distinct from the list-store
# THEME_DB_LUAU above that the seed path drains.
_KEYED_DICT_LUAU = """\
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
	-- If not nil the dictionary was already loaded.
	if themeDataList == nil then
		themeDataList = {}
		local function addTheme(op)
			if op ~= nil then
				themeDataList[op.themeName] = op
			end
		end
		local _ = addTheme
		m_Loaded = true
	end
end
return ThemeDatabase
"""


def _lower_pipeline(tmp_path: Path, *, cs: str = THEME_DB_CS_REAL_SHAPE,
                    db_luau: str = _KEYED_DICT_LUAU,
                    source_path: str | None = "ThemeDatabase.luau"):
    """Build a real Pipeline whose ThemeDatabase RbxScript carries the keyed-dict
    body + a ``source_path`` (production stamps this at pipeline.py:3334), then run
    the production orchestration. Returns (pipe, scene_runtime, db_script)."""
    root = _make_project(tmp_path, cs=cs)
    pipe = _pipeline_with_state(root, tmp_path)
    pipe.state.rbx_place.scripts = [
        RbxScript(name="ThemeDatabase", source=db_luau,
                  script_type="ModuleScript", parent_path="ReplicatedStorage",
                  source_path=source_path),
    ]
    sr = _scene_runtime_with_so_map()
    pipe._lower_so_db_consumers(sr)
    return pipe, sr, pipe.state.rbx_place.scripts[0]


class TestLowerSoDbConsumersOrchestration:
    def test_orchestration_lowers_keyed_dict_db(self, tmp_path):
        """The production method derives the SO surface + C# ownership + facts and
        rewrites the placed RbxScript.source to the canonical keyed-dict body that
        requires both SO modules and writes ``_so_db_dict[so['themeName']]``."""
        _, _, db = _lower_pipeline(tmp_path)
        assert "ReplicatedStorage.ThemeData_Day" in db.source
        assert "ReplicatedStorage.ThemeData_Night" in db.source
        assert "_so_db_dict[_key] = so" in db.source
        assert "so['themeName']" in db.source or 'so["themeName"]' in db.source
        # getters re-emitted under their LOCATED game-specific names
        assert "function ThemeDatabase.GetThemeData(type)" in db.source
        assert "return _so_db_dict[type]" in db.source

    def test_orchestration_noop_without_source_path(self, tmp_path):
        """The orchestration keys csharp_by_path by RbxScript.source_path (the same
        key the locator uses). A placed script with no source_path is skipped — so
        the test_orchestration_lowers_keyed_dict_db pass above is attributable to the
        production source_path keying, NOT a green-for-wrong-reason. (Production sets
        source_path at pipeline.py:3334; this guards that contract.)"""
        _, _, db = _lower_pipeline(tmp_path, source_path=None)
        assert "_so_db_dict" not in db.source
        assert db.source == _KEYED_DICT_LUAU

    def test_orchestration_idempotent(self, tmp_path):
        """Re-running the orchestration on its own lowered output is byte-identical
        (edge 8) — the region walk-back absorbs the pass-owned state decls."""
        pipe, sr, db = _lower_pipeline(tmp_path)
        first = db.source
        pipe._lower_so_db_consumers(sr)
        assert db.source == first

    def test_orchestration_fail_closed_on_unlocatable_body(self, tmp_path):
        """A module whose C# is a located SO-DB consumer but whose transpiled body
        lacks the C#-derived load method (AI emitted an unrecognizable shape) must
        re-raise SoDbUnresolved through the orchestration (never a silent empty DB)."""
        from converter.so_db_consumer_lowering import SoDbUnresolved
        broken = _KEYED_DICT_LUAU.replace(
            "function ThemeDatabase.LoadDatabase()",
            "function ThemeDatabase.SomethingElse()",
        )
        with pytest.raises(SoDbUnresolved):
            _lower_pipeline(tmp_path, db_luau=broken)

    def test_orchestration_noop_on_non_db_module(self, tmp_path):
        """A module whose C# issues no LoadAssetsAsync<T>(literal) yields no fact;
        the orchestration leaves its source untouched."""
        cs = "public class Foo { void Bar() {} }"
        plain = "local Foo = {}\nreturn Foo\n"
        root = _make_project(tmp_path, cs=cs)
        pipe = _pipeline_with_state(root, tmp_path)
        pipe.state.rbx_place.scripts = [
            RbxScript(name="Foo", source=plain, script_type="ModuleScript",
                      parent_path="ReplicatedStorage", source_path="Foo.luau"),
        ]
        sr = _scene_runtime_with_so_map()
        pipe._lower_so_db_consumers(sr)
        assert pipe.state.rbx_place.scripts[0].source == plain
