"""Phase 3 — SO-store DB consumer-lowering (gate b + c + d).

Covers the deterministic, ownership-keyed re-lowering of a keyed-dictionary
ScriptableObject-store database (the sibling of ``roster_consumer_lowering``):
fact derivation (AC3), the canonical lowered body + idempotence (AC4),
shared-label disjointness with the roster lowering (AC10), and the edge cases
(zero SO modules / ambiguous label / key-less stem fallback / unlocatable
anchors).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.so_db_consumer_lowering import (
    SoDbConsumerFact,
    SoDbUnresolved,
    find_so_db_consumers,
    lower_so_db_consumers,
)


# --- the REAL transpiled ThemeDatabase body (closure-private keyed dict) ----
# Captured from the #210-baseline cold diag output: a ``local themeDataList = nil``
# upvalue, a never-called ``addTheme(op)`` closure, no ipairs/table.insert.
THEME_DB_LUAU = """\
-- ThemeDatabase: static class that loads ThemeData from an asset bundle.
local ThemeDatabase = {}

-- static protected Dictionary<string, ThemeData> themeDataList
local themeDataList = nil

-- static protected bool m_Loaded
local m_Loaded = false

function ThemeDatabase.dictionnary()
	return themeDataList
end

function ThemeDatabase.loaded()
	return m_Loaded
end

function ThemeDatabase.GetThemeData(type)
	if themeDataList == nil then
		return nil
	end
	local list = themeDataList[type]
	if list == nil then
		return nil
	end
	return list
end

function ThemeDatabase.LoadDatabase()
	-- If not nil the dictionary was already loaded.
	if themeDataList == nil then
		themeDataList = {}
		local function addTheme(op)
			if op ~= nil then
				if themeDataList[op.themeName] == nil then
					themeDataList[op.themeName] = op
				end
			end
		end
		local _ = addTheme
		m_Loaded = true
	end
end

return ThemeDatabase
"""

# The real Trash-Dash ThemeDatabase C# (label themeData, key themeName, <ThemeData>).
THEME_DB_CS = """\
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

# A <GameObject> roster DB (CharacterDatabase shape) on a DIFFERENT label.
CHAR_DB_CS = """\
public class CharacterDatabase {
    static public IEnumerator LoadDatabase() {
        if (m_CharactersDict == null) {
            Addressables.LoadAssetsAsync<GameObject>("characters", op => {
                m_CharactersDict.Add(op.name, op.GetComponent<Character>());
            });
        }
    }
}
"""

_DAY = "ReplicatedStorage.scriptable_objects.themeData__86f154"
_NIGHT = "ReplicatedStorage.scriptable_objects.themeData__d9a369"


class _Script:
    """Minimal RbxScript stand-in carrying source_path + source."""

    def __init__(self, source_path: str | None, source: str) -> None:
        self.source_path = source_path
        self.source = source


def _theme_surface() -> tuple[dict[str, list[str]], dict[str, list[str]], dict[str, str]]:
    by_label = {"themeData": ["guidDay", "guidNight"]}
    by_address: dict[str, list[str]] = {}
    g2m = {"guidDay": _DAY, "guidNight": _NIGHT}
    return by_label, by_address, g2m


# ---------------------------------------------------------------------------
# AC3 — fact derivation
# ---------------------------------------------------------------------------

class TestFactDerivation:
    def test_one_fact_on_real_themedatabase(self):
        by_label, by_address, g2m = _theme_surface()
        facts = find_so_db_consumers(
            {"Themes/ThemeDatabase.cs": THEME_DB_CS}, by_label, by_address, g2m,
        )
        assert set(facts) == {"Themes/ThemeDatabase.cs"}
        f = facts["Themes/ThemeDatabase.cs"]
        assert f.label == "themeData"
        assert f.so_type == "ThemeData"
        assert f.key_field == "themeName"
        assert set(f.so_module_paths) == {_DAY, _NIGHT}

    def test_empty_so_surface_abstains(self):
        """Edge 1/9: no SO surface -> no fact."""
        facts = find_so_db_consumers(
            {"x": THEME_DB_CS}, {}, {}, {},
        )
        assert facts == {}

    def test_label_not_in_surface_abstains(self):
        by_label = {"otherLabel": ["guidX"]}
        g2m = {"guidX": _DAY}
        facts = find_so_db_consumers({"x": THEME_DB_CS}, by_label, {}, g2m)
        assert facts == {}

    def test_so_type_resolving_to_zero_modules_abstains(self):
        """Edge 5 (layer b): the label is in-surface but its guids resolve to NO
        emitted SO module (a <GameObject> roster load on a shared label) -> abstain.
        """
        by_label = {"themeData": ["prefabGuid"]}  # guid NOT in the SO module map
        facts = find_so_db_consumers({"x": THEME_DB_CS}, by_label, {}, {})
        assert facts == {}

    def test_ambiguous_two_labels_in_one_module_abstains(self):
        """Edge 2: a module loading >1 distinct in-surface label -> abstain."""
        cs = THEME_DB_CS.replace(
            "m_Loaded = true;",
            'Addressables.LoadAssetsAsync<ThemeData>("weatherData", op => {});\n'
            "            m_Loaded = true;",
        )
        by_label = {"themeData": ["guidDay"], "weatherData": ["guidNight"]}
        g2m = {"guidDay": _DAY, "guidNight": _NIGHT}
        facts = find_so_db_consumers({"x": cs}, by_label, {}, g2m)
        assert facts == {}

    def test_non_literal_label_abstains(self):
        cs = THEME_DB_CS.replace('"themeData"', "labelVar")
        by_label, _, g2m = _theme_surface()
        facts = find_so_db_consumers({"x": cs}, by_label, {}, g2m)
        assert facts == {}

    def test_resolves_by_address(self):
        """Unity LoadAssetsAsync<T>(key) accepts a LABEL *or* an ADDRESS."""
        by_address = {"themeData": ["guidDay", "guidNight"]}
        g2m = {"guidDay": _DAY, "guidNight": _NIGHT}
        facts = find_so_db_consumers({"x": THEME_DB_CS}, {}, by_address, g2m)
        assert set(facts) == {"x"}
        assert set(facts["x"].so_module_paths) == {_DAY, _NIGHT}

    def test_key_field_none_when_indexed_by_expression(self):
        """Edge 4: a key that is not ``op.<field>`` -> key_field None."""
        cs = THEME_DB_CS.replace(
            "themeDataList.Add(op.themeName, op)",
            "themeDataList[op.GetHashCode()] = op",
        )
        by_label, _, g2m = _theme_surface()
        facts = find_so_db_consumers({"x": cs}, by_label, {}, g2m)
        assert facts["x"].key_field is None


# ---------------------------------------------------------------------------
# AC4 — lowering output + idempotence
# ---------------------------------------------------------------------------

class TestLoweringOutput:
    def _lower_real(self):
        by_label, by_address, g2m = _theme_surface()
        facts = find_so_db_consumers({"x": THEME_DB_CS}, by_label, by_address, g2m)
        s = _Script("x", THEME_DB_LUAU)
        n = lower_so_db_consumers([s], facts, {"x": "LoadDatabase"})
        return s, n

    def test_requires_both_so_modules_and_keys_by_themename(self):
        s, n = self._lower_real()
        assert n == 1
        assert _DAY in s.source
        assert _NIGHT in s.source
        # keyed write under so[themeName]
        assert "so['themeName']" in s.source or 'so["themeName"]' in s.source
        assert "_so_db_dict[_key] = so" in s.source

    def test_getters_read_the_same_local(self):
        s, _ = self._lower_real()
        # dictionnary / loaded / GetThemeData re-emitted under located names,
        # all reading the pass-owned local.
        assert "function ThemeDatabase.dictionnary()" in s.source
        assert "function ThemeDatabase.loaded()" in s.source
        assert "function ThemeDatabase.GetThemeData(type)" in s.source
        assert "return _so_db_dict" in s.source
        assert "return _so_db_loaded" in s.source
        assert "return _so_db_dict[type]" in s.source

    def test_idempotent_byte_identical(self):
        s, _ = self._lower_real()
        first = s.source
        by_label, by_address, g2m = _theme_surface()
        facts = find_so_db_consumers({"x": THEME_DB_CS}, by_label, by_address, g2m)
        lower_so_db_consumers([s], facts, {"x": "LoadDatabase"})
        assert s.source == first

    def test_module_epilogue_return_preserved(self):
        s, _ = self._lower_real()
        assert s.source.rstrip().endswith("return ThemeDatabase")
        assert "local ThemeDatabase = {}" in s.source

    def test_key_field_none_uses_stem_fallback_nonempty(self):
        """Edge 4: with key_field None the body keys by the SO module stem so the
        dict is still NON-EMPTY (pairs-iterable)."""
        by_label, _, g2m = _theme_surface()
        fact = SoDbConsumerFact(
            source_path="x", label="themeData", so_type="ThemeData",
            key_field=None, so_module_paths=(_DAY, _NIGHT),
        )
        s = _Script("x", THEME_DB_LUAU)
        lower_so_db_consumers([s], {"x": fact}, {"x": "LoadDatabase"})
        assert "local _key = _stem" in s.source
        assert "_so_db_dict[_key] = so" in s.source

    def test_skips_nil_key(self):
        """Edge 6: a nil key is not written."""
        s, _ = self._lower_real()
        assert "if _key ~= nil then" in s.source

    def test_module_not_in_facts_untouched(self):
        other = _Script("other", "local X = {}\nreturn X\n")
        by_label, by_address, g2m = _theme_surface()
        facts = find_so_db_consumers({"x": THEME_DB_CS}, by_label, by_address, g2m)
        n = lower_so_db_consumers([other], facts, {"x": "LoadDatabase"})
        assert n == 0
        assert other.source == "local X = {}\nreturn X\n"

    def test_unlocatable_load_method_fails_closed(self):
        """Edge 7: a located fact whose load method name is absent from the
        transpiled body -> SoDbUnresolved (fail-closed, never a silent empty DB)."""
        fact = SoDbConsumerFact(
            source_path="x", label="themeData", so_type="ThemeData",
            key_field="themeName", so_module_paths=(_DAY,),
        )
        s = _Script("x", THEME_DB_LUAU)
        with pytest.raises(SoDbUnresolved):
            lower_so_db_consumers([s], {"x": fact}, {"x": "NoSuchMethod"})

    def test_missing_load_method_mapping_fails_closed(self):
        fact = SoDbConsumerFact(
            source_path="x", label="themeData", so_type="ThemeData",
            key_field="themeName", so_module_paths=(_DAY,),
        )
        s = _Script("x", THEME_DB_LUAU)
        with pytest.raises(SoDbUnresolved):
            lower_so_db_consumers([s], {"x": fact}, {})

    def test_script_with_none_source_path_skipped(self):
        by_label, by_address, g2m = _theme_surface()
        facts = find_so_db_consumers({"x": THEME_DB_CS}, by_label, by_address, g2m)
        s = _Script(None, THEME_DB_LUAU)
        n = lower_so_db_consumers([s], facts, {"x": "LoadDatabase"})
        assert n == 0


# ---------------------------------------------------------------------------
# AC10 — shared-label disjointness with the roster lowering
# ---------------------------------------------------------------------------

class TestSharedLabelDisjointness:
    def test_so_finder_claims_only_sotype_module_distinct_labels(self):
        """The COMMON case (layer b): the <GameObject> DB loads a label whose
        guids are prefab guids (absent from the SO map) and the <SOType> DB loads
        the SO label. find_so_db_consumers claims ONLY the <SOType> module — the
        <GameObject> module's label resolves to NO emitted SO module -> abstain."""
        char_cs = CHAR_DB_CS  # loads "characters" (a prefab label)
        theme_cs = THEME_DB_CS  # loads "themeData" (the SO label)
        # SO surface narrowed to SO guids: "characters" is present but its guid is
        # a PREFAB guid (not in the SO module map); "themeData" resolves to SO modules.
        so_by_label = {
            "themeData": ["guidDay", "guidNight"],
            "characters": ["prefabGuidOnly"],  # NOT in g2m -> resolves to 0 SO modules
        }
        g2m = {"guidDay": _DAY, "guidNight": _NIGHT}
        facts = find_so_db_consumers(
            {"Char.cs": char_cs, "Theme.cs": theme_cs}, so_by_label, {}, g2m,
        )
        assert set(facts) == {"Theme.cs"}  # NOT Char.cs (layer b)

    def test_truly_shared_label_needs_layer_a_guard(self):
        """The PATHOLOGICAL case the design names (§3f): a single label genuinely
        tags an SO so both DBs' loads resolve to the SAME SO guids; layer (b)
        alone cannot distinguish them, so the layer-(a) roster_claimed_paths
        exclusion is what enforces disjointness."""
        char_cs = CHAR_DB_CS.replace('"characters"', '"shared"')
        theme_cs = THEME_DB_CS.replace('"themeData"', '"shared"')
        so_by_label = {"shared": ["guidDay", "guidNight"]}
        g2m = {"guidDay": _DAY, "guidNight": _NIGHT}
        # Layer (b) alone: both resolve to the SO modules -> both claimed.
        unguarded = find_so_db_consumers(
            {"Char.cs": char_cs, "Theme.cs": theme_cs}, so_by_label, {}, g2m,
        )
        assert unguarded.keys() >= {"Char.cs", "Theme.cs"}
        # Layer (a): the roster lowering claimed Char.cs first -> SO finder abstains.
        guarded = find_so_db_consumers(
            {"Char.cs": char_cs, "Theme.cs": theme_cs}, so_by_label, {}, g2m,
            roster_claimed_paths=frozenset({"Char.cs"}),
        )
        assert set(guarded) == {"Theme.cs"}

    def test_roster_finder_claims_only_gameobject_module(self):
        """Mirror: find_roster_consumers (prefab by_label) claims ONLY the
        <GameObject> module. (The roster finder keys on the prefab surface.)"""
        from converter.roster_consumer_lowering import find_roster_consumers
        char_cs = CHAR_DB_CS.replace('"characters"', '"shared"')
        theme_cs = THEME_DB_CS.replace('"themeData"', '"shared"')
        prefab_by_label = {"shared": ["prefabId1", "prefabId2"]}
        roster = find_roster_consumers(
            {"Char.cs": char_cs, "Theme.cs": theme_cs}, prefab_by_label,
        )
        # Both modules call LoadAssetsAsync<T>("shared"); the roster finder does
        # not yet check <T> (the pre-existing gap), so it may claim both. The
        # layer-(a) guard below is what makes the SO finder disjoint regardless.
        assert "Char.cs" in roster

    def test_roster_claimed_path_excluded_from_so_finder(self):
        """Layer (a): a path in roster_claimed_paths makes the SO finder abstain
        on it, even when its <SOType> would otherwise resolve to an SO module
        (the pathological both-resolve case)."""
        theme_cs = THEME_DB_CS.replace('"themeData"', '"shared"')
        so_by_label = {"shared": ["guidDay"]}
        g2m = {"guidDay": _DAY}
        # Without the guard: the SO finder claims Theme.cs.
        facts_unguarded = find_so_db_consumers(
            {"Theme.cs": theme_cs}, so_by_label, {}, g2m,
        )
        assert set(facts_unguarded) == {"Theme.cs"}
        # With Theme.cs roster-claimed: the SO finder abstains.
        facts_guarded = find_so_db_consumers(
            {"Theme.cs": theme_cs}, so_by_label, {}, g2m,
            roster_claimed_paths=frozenset({"Theme.cs"}),
        )
        assert facts_guarded == {}


# ---------------------------------------------------------------------------
# Real on-disk integration (fact + lowering against the captured diag output)
# ---------------------------------------------------------------------------

class TestRealDiagOutput:
    _DIAG = Path(
        "/Users/jiazou/.claude/harness-runs/trash-dash-phase2-20260618T102928/"
        "wt/diag/converter/output/trash-dash-phase2-diag/scripts/ThemeDatabase.luau"
    )
    _CS = Path(
        "/Users/jiazou/workspace/trash-dash/Assets/Scripts/Themes/ThemeDatabase.cs"
    )

    def test_lowers_real_transpiled_themedatabase(self):
        if not (self._DIAG.exists() and self._CS.exists()):
            pytest.skip("diag output / trash-dash source not present")
        cs = self._CS.read_text(encoding="utf-8")
        luau = self._DIAG.read_text(encoding="utf-8")
        by_label = {"themeData": ["guidDay", "guidNight"]}
        g2m = {"guidDay": _DAY, "guidNight": _NIGHT}
        facts = find_so_db_consumers({"x": cs}, by_label, {}, g2m)
        assert set(facts) == {"x"}
        s = _Script("x", luau)
        n = lower_so_db_consumers([s], facts, {"x": "LoadDatabase"})
        assert n == 1
        assert "function ThemeDatabase.GetThemeData(type)" in s.source
        assert "return _so_db_dict[type]" in s.source
        # idempotent on the real shape
        first = s.source
        lower_so_db_consumers([s], facts, {"x": "LoadDatabase"})
        assert s.source == first
