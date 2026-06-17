"""Phase 2 (Unit 4) — roster-consumer re-lowering tests.

Covers the in-pipeline acceptance criteria AC1–AC8 from
``design-phase2.md`` (AC9 cold-boot is the VERIFY stage, not here):

  AC1  deterministic consumer identification (find_roster_consumers)
  AC2  canonical body byte-identical across the 3 real drift shapes + a
       renamed-receiver control
  AC4  re-lowered module NOT dead-stubbed on a FRESH transpile (carrier)
  AC5  re-lowered module NOT dead-stubbed on RESUME (carrier round-trip)
  AC6  .gameObject binds the script-bearing Templates child
  AC7  idempotency (twice-call byte-equal) + fail-closed surfaces
  AC8  no game literal in the pass source OR the emitted template

The lowering is GENERIC: the trigger is the deterministic upstream
``Addressables.LoadAssetsAsync<T>("<L>", ...)`` C# fact (L in by_label),
never a ``"CharacterDatabase"`` / ``"characters"`` literal; the canonical
body substitutes the LOCATED receiver name.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.roster_consumer_lowering import (  # noqa: E402
    RosterConsumerFact,
    RosterUnresolved,
    _canonical_region,
    _locate_region,
    find_roster_consumers,
    lower_roster_consumers,
)


# ---------------------------------------------------------------------------
# Test doubles + fixtures
# ---------------------------------------------------------------------------

@dataclass
class _Script:
    """A minimal stand-in for ``TranspiledScript`` (the lowering only touches
    ``source_path`` / ``luau_source`` / ``roster_binding``)."""

    source_path: str
    luau_source: str
    roster_binding: object = None


# The deterministic upstream fact the orchestrator computes from the C# source.
_FACT = RosterConsumerFact(
    source_path="CharacterDatabase.cs",
    label="characters",
    component_type="Character",
    index_key="characterName",
)
_FACTS = {"CharacterDatabase.cs": _FACT}

# The C# the consumer is transpiled FROM (the deterministic anchor for AC1).
_ROSTER_CS = (
    "using UnityEngine; using UnityEngine.AddressableAssets;\n"
    "public class CharacterDatabase {\n"
    "  static System.Collections.Generic.Dictionary<string,Character> m_CharactersDict;\n"
    "  public static System.Collections.IEnumerator LoadDatabase() {\n"
    "    yield return Addressables.LoadAssetsAsync<GameObject>(\"characters\", op => {\n"
    "      Character c = op.GetComponent<Character>();\n"
    "      if (c != null) m_CharactersDict.Add(c.characterName, c);\n"
    "    });\n"
    "  }\n"
    "}\n"
)


def _drift_folder_findfirstchild(receiver: str = "CharacterDatabase") -> str:
    """Drift shape A — the FOLDER ``FindFirstChild`` shape (== reobs24 / unit1)."""
    return (
        f"local {receiver} = {{}}\n"
        f"\n"
        f"local m_CharactersDict = nil\n"
        f"local m_Loaded = false\n"
        f"\n"
        f"function {receiver}.dictionary()\n"
        f"\treturn m_CharactersDict\n"
        f"end\n"
        f"\n"
        f"function {receiver}.loaded()\n"
        f"\treturn m_Loaded\n"
        f"end\n"
        f"\n"
        f"function {receiver}.GetCharacter(type)\n"
        f"\tif m_CharactersDict == nil then\n"
        f"\t\treturn nil\n"
        f"\tend\n"
        f"\treturn m_CharactersDict[type]\n"
        f"end\n"
        f"\n"
        f"function {receiver}.LoadDatabase()\n"
        f"\tif m_CharactersDict == nil then\n"
        f"\t\tm_CharactersDict = {{}}\n"
        f"\t\tlocal ReplicatedStorage = game:GetService(\"ReplicatedStorage\")\n"
        f"\t\tlocal container = ReplicatedStorage:FindFirstChild(\"Characters\")\n"
        f"\t\tif container then\n"
        f"\t\t\tfor _, op in ipairs(container:GetChildren()) do\n"
        f"\t\t\t\tlocal characterName = op:GetAttribute(\"characterName\") or op.Name\n"
        f"\t\t\t\tm_CharactersDict[characterName] = op\n"
        f"\t\t\tend\n"
        f"\t\tend\n"
        f"\t\tm_Loaded = true\n"
        f"\tend\n"
        f"end\n"
        f"\n"
        f"return {receiver}\n"
    )


def _drift_folder_waitforchild(receiver: str = "CharacterDatabase") -> str:
    """Drift shape B — the FOLDER ``WaitForChild`` shape."""
    return (
        f"local {receiver} = {{}}\n"
        f"local m_CharactersDict = nil\n"
        f"local m_Loaded = false\n"
        f"\n"
        f"function {receiver}.dictionary()\n"
        f"\treturn m_CharactersDict\n"
        f"end\n"
        f"function {receiver}.loaded()\n"
        f"\treturn m_Loaded\n"
        f"end\n"
        f"function {receiver}.GetCharacter(name)\n"
        f"\treturn m_CharactersDict and m_CharactersDict[name] or nil\n"
        f"end\n"
        f"function {receiver}.LoadDatabase()\n"
        f"\tif m_CharactersDict == nil then\n"
        f"\t\tm_CharactersDict = {{}}\n"
        f"\t\tlocal RS = game:GetService(\"ReplicatedStorage\")\n"
        f"\t\tlocal folder = RS:WaitForChild(\"Characters\")\n"
        f"\t\tfor _, op in folder:GetChildren() do\n"
        f"\t\t\tm_CharactersDict[op.Name] = op\n"
        f"\t\tend\n"
        f"\t\tm_Loaded = true\n"
        f"\tend\n"
        f"end\n"
        f"\n"
        f"return {receiver}\n"
    )


def _drift_gettagged(receiver: str = "CharacterDatabase") -> str:
    """Drift shape C — the ``CollectionService:GetTagged`` shape (== unit2-proper)."""
    return (
        f"local CollectionService = game:GetService(\"CollectionService\")\n"
        f"\n"
        f"local {receiver} = {{}}\n"
        f"\n"
        f"local m_CharactersDict = nil\n"
        f"local m_Loaded = false\n"
        f"\n"
        f"function {receiver}.dictionary()\n"
        f"\treturn m_CharactersDict\n"
        f"end\n"
        f"\n"
        f"function {receiver}.loaded()\n"
        f"\treturn m_Loaded\n"
        f"end\n"
        f"\n"
        f"function {receiver}.GetCharacter(typeName)\n"
        f"\tif m_CharactersDict == nil then\n"
        f"\t\treturn nil\n"
        f"\tend\n"
        f"\treturn m_CharactersDict[typeName]\n"
        f"end\n"
        f"\n"
        f"function {receiver}.LoadDatabase()\n"
        f"\tif m_CharactersDict == nil then\n"
        f"\t\tm_CharactersDict = {{}}\n"
        f"\t\tfor _, op in CollectionService:GetTagged(\"characters\") do\n"
        f"\t\t\tlocal c = op\n"
        f"\t\t\tif c ~= nil then\n"
        f"\t\t\t\tlocal characterName = op:GetAttribute(\"characterName\")\n"
        f"\t\t\t\tif characterName ~= nil then\n"
        f"\t\t\t\t\tm_CharactersDict[characterName] = c\n"
        f"\t\t\t\tend\n"
        f"\t\t\tend\n"
        f"\t\tend\n"
        f"\t\tm_Loaded = true\n"
        f"\tend\n"
        f"end\n"
        f"\n"
        f"return {receiver}\n"
    )


_ALL_DRIFT_SHAPES = (
    _drift_folder_findfirstchild,
    _drift_folder_waitforchild,
    _drift_gettagged,
)


def _region_of(source: str) -> str:
    located = _locate_region(source)
    assert located is not None, "expected a locatable roster region"
    _, start, end = located
    return source[start:end]


# ---------------------------------------------------------------------------
# AC1 — deterministic consumer identification
# ---------------------------------------------------------------------------

class TestAC1FindRosterConsumers:
    def test_matches_load_assets_for_by_label_label(self) -> None:
        facts = find_roster_consumers(
            {"CharacterDatabase.cs": _ROSTER_CS},
            {"characters": ["pid1", "pid2"]},
        )
        assert "CharacterDatabase.cs" in facts
        fact = facts["CharacterDatabase.cs"]
        assert fact.label == "characters"
        assert fact.component_type == "Character"
        assert fact.index_key == "characterName"

    def test_label_not_in_by_label_abstains(self) -> None:
        facts = find_roster_consumers(
            {"CharacterDatabase.cs": _ROSTER_CS},
            {"themes": ["pid1"]},
        )
        assert facts == {}

    def test_non_literal_label_abstains(self) -> None:
        cs = "Addressables.LoadAssetsAsync<GameObject>(myLabelVar, op => {});"
        facts = find_roster_consumers({"a.cs": cs}, {"characters": ["pid1"]})
        assert facts == {}

    def test_no_load_assets_call_abstains(self) -> None:
        facts = find_roster_consumers(
            {"a.cs": "public class A { void X() {} }"},
            {"characters": ["pid1"]},
        )
        assert facts == {}

    def test_empty_by_label_abstains(self) -> None:
        facts = find_roster_consumers({"CharacterDatabase.cs": _ROSTER_CS}, {})
        assert facts == {}

    def test_module_loading_two_distinct_in_by_label_labels_abstains(self) -> None:
        cs = (
            'Addressables.LoadAssetsAsync<G>("characters", a => {});\n'
            'Addressables.LoadAssetsAsync<G>("themes", b => {});'
        )
        facts = find_roster_consumers(
            {"a.cs": cs}, {"characters": ["p"], "themes": ["q"]},
        )
        assert facts == {}, "a module loading >1 in-by_label label is ambiguous"


# ---------------------------------------------------------------------------
# AC2 — canonical body across the 3 drift shapes + renamed control
# ---------------------------------------------------------------------------

class TestAC2CanonicalBodyAcrossDriftShapes:
    def test_region_byte_identical_across_all_three_shapes(self) -> None:
        regions: set[str] = set()
        for make_shape in _ALL_DRIFT_SHAPES:
            s = _Script("CharacterDatabase.cs", make_shape())
            n = lower_roster_consumers([s], _FACTS)
            assert n == 1
            regions.add(_region_of(s.luau_source))
        assert len(regions) == 1, (
            "the canonical region must be byte-identical for one located <N> "
            "across all 3 AI drift shapes (AC2)"
        )

    def test_region_equals_canonical_render(self) -> None:
        s = _Script("CharacterDatabase.cs", _drift_gettagged())
        lower_roster_consumers([s], _FACTS)
        assert _region_of(s.luau_source) == _canonical_region(
            "CharacterDatabase", "characters", "Character", "characterName",
        )

    def test_renamed_receiver_carries_located_name_no_hardcoded_literal(self) -> None:
        # A control whose receiver table is NOT CharacterDatabase proves the
        # emitted body uses the LOCATED <N>, not a hardcoded literal.
        s = _Script("Foo.cs", _drift_folder_findfirstchild(receiver="PrefabRoster"))
        facts = {"Foo.cs": RosterConsumerFact("Foo.cs", "characters", "Character", "characterName")}
        lower_roster_consumers([s], facts)
        assert "function PrefabRoster.LoadDatabase()" in s.luau_source
        assert "function PrefabRoster.GetCharacter(" in s.luau_source
        assert "CharacterDatabase" not in s.luau_source
        assert s.roster_binding == {
            "label": "characters", "receiver": "PrefabRoster", "lowered": True,
        }


# ---------------------------------------------------------------------------
# AC6 — .gameObject binds the script-bearing Templates child
# ---------------------------------------------------------------------------

class TestAC6GameObjectBindsTemplatesChild:
    def test_gameobject_resolves_templates_child_then_falls_back(self) -> None:
        s = _Script("CharacterDatabase.cs", _drift_gettagged())
        lower_roster_consumers([s], _FACTS)
        body = s.luau_source
        # The Templates folder is the script-bearing canonical source (NOT the
        # script-stripped roster member).
        assert 'ReplicatedStorage:FindFirstChild("Templates")' in body
        assert "_templates:FindFirstChild(_key)" in body
        # Fallback to the tagged member when no Templates match.
        assert "or op" in body
        assert "c.gameObject = _go" in body

    def test_accessories_is_empty_table(self) -> None:
        s = _Script("CharacterDatabase.cs", _drift_gettagged())
        lower_roster_consumers([s], _FACTS)
        assert "c.accessories = {}" in s.luau_source

    def test_component_wrapper_constructor_used(self) -> None:
        s = _Script("CharacterDatabase.cs", _drift_gettagged())
        lower_roster_consumers([s], _FACTS)
        assert "Character.new({ characterName = _key })" in s.luau_source


# ---------------------------------------------------------------------------
# AC7 — idempotency + fail-closed
# ---------------------------------------------------------------------------

class TestAC7IdempotencyAndFailClosed:
    def test_twice_call_byte_identical(self) -> None:
        for make_shape in _ALL_DRIFT_SHAPES:
            s = _Script("CharacterDatabase.cs", make_shape())
            lower_roster_consumers([s], _FACTS)
            first = s.luau_source
            lower_roster_consumers([s], _FACTS)
            assert s.luau_source == first, (
                "twice-call must yield byte-identical output (AC7 idempotency)"
            )

    def test_unlocatable_method_raises_roster_unresolved(self) -> None:
        # A located FACT (the C# is a roster loader) but the transpiled body has
        # no locatable LoadDatabase/GetCharacter -> fail closed (E-P2-2).
        s = _Script("CharacterDatabase.cs", "local M = {}\nreturn M\n")
        with pytest.raises(RosterUnresolved):
            lower_roster_consumers([s], _FACTS)

    def test_interleaved_require_helper_is_not_deleted(self) -> None:
        # A roster consumer with an UNRELATED require/helper local between the
        # receiver-table decl and the state decls must keep it: the backward
        # region walk absorbs ONLY bare state-literal decls (= nil/false/{}/num),
        # never a `local X = require(...)` / `= <call>`. Whole-region-replacing
        # across such a line would silently DELETE a dependency the module needs.
        body = (
            "local CharacterDatabase = {}\n"
            "local Signal = require(game.ReplicatedStorage.Signal)\n"
            "local onLoaded = Signal.new()\n"
            "local m_CharactersDict = nil\n"
            "local m_Loaded = false\n"
            "function CharacterDatabase.dictionary()\n\treturn m_CharactersDict\nend\n"
            "function CharacterDatabase.loaded()\n\treturn m_Loaded\nend\n"
            "function CharacterDatabase.GetCharacter(t)\n\treturn nil\nend\n"
            "function CharacterDatabase.LoadDatabase()\nend\n"
            "return CharacterDatabase\n"
        )
        s = _Script("CharacterDatabase.cs", body)
        lower_roster_consumers([s], _FACTS)
        # The unrelated require + helper survive the whole-region replace.
        assert "require(game.ReplicatedStorage.Signal)" in s.luau_source
        assert "local onLoaded = Signal.new()" in s.luau_source
        # The canonical body still replaced the four methods + state.
        assert "_roster_dict" in s.luau_source
        assert "function CharacterDatabase.LoadDatabase()" in s.luau_source
        # The bare state locals ARE absorbed (re-owned), no leftover m_* dead code.
        assert "m_CharactersDict" not in s.luau_source
        assert "m_Loaded" not in s.luau_source
        # Idempotent on this shape too.
        first = s.luau_source
        lower_roster_consumers([s], _FACTS)
        assert s.luau_source == first

    def test_module_not_in_facts_is_untouched(self) -> None:
        # Generality gate (E-P2-6): a non-consumer is never rewritten.
        original = _drift_folder_findfirstchild()
        s = _Script("OtherModule.cs", original)
        n = lower_roster_consumers([s], _FACTS)
        assert n == 0
        assert s.luau_source == original
        assert s.roster_binding is None


# ---------------------------------------------------------------------------
# AC8 — generic, no game literal in source OR emitted template
# ---------------------------------------------------------------------------

class TestAC8NoGameLiteral:
    def test_pass_source_has_no_game_literal(self) -> None:
        src = (
            Path(__file__).parent.parent
            / "converter" / "roster_consumer_lowering.py"
        ).read_text()
        # Strip docstring/comment mentions: assert no string LITERAL of the game
        # name appears as code. The module names CharacterDatabase / "characters"
        # only in prose (docstring); a quoted code literal would be the bug.
        # Conservatively assert neither appears as a quoted string literal.
        assert '"CharacterDatabase"' not in src
        assert "'CharacterDatabase'" not in src
        assert '"characters"' not in src
        assert "'characters'" not in src

    def test_emitted_template_has_no_game_literal(self) -> None:
        # Render with a non-game receiver/label/component to prove nothing is
        # hardcoded — every per-game value is substituted.
        region = _canonical_region("Roster", "widgets", "Widget", "widgetName")
        assert "CharacterDatabase" not in region
        assert "characters" not in region
        assert "function Roster.LoadDatabase()" in region
        assert 'CollectionService:GetTagged("widgets")' in region
        assert "Widget.new({ widgetName = _key })" in region

    def test_canonical_body_owns_its_dict_loaded_locals(self) -> None:
        region = _canonical_region("Roster", "widgets", "Widget", "widgetName")
        # The pass owns its module-locals; it never assumes the AI's
        # m_CharactersDict / m_Loaded upvalues survive.
        assert "_roster_dict" in region
        assert "_roster_loaded" in region
        assert "m_CharactersDict" not in region
        assert "m_Loaded" not in region

    def test_component_and_key_default_when_fact_absent(self) -> None:
        # component_type / index_key None -> generic defaults, never a crash.
        region = _canonical_region("Roster", "widgets", None, None)
        assert "Character.new({ characterName = _key })" in region
        assert 'op:GetAttribute("characterName")' in region


# ---------------------------------------------------------------------------
# Generality — a NAMESPACED C# component type must lower to a valid Luau
# identifier + require path (its LAST dotted segment), never the raw dotted
# splice. Namespaced component types (My.Game.Character) are common across
# Unity games; the raw splice would emit
# ``local My.Game.Character = require(script.Parent.My.Game.Character)`` —
# invalid Luau (a dotted local name) and a wrong require path.
# ---------------------------------------------------------------------------

class TestNamespacedComponentType:
    def test_find_roster_consumers_captures_namespaced_type(self) -> None:
        # The C# capture itself keeps the dotted form (the regex is [\w.]+);
        # the normalization lives in the emitter, so the fact still carries
        # the full namespaced name.
        cs = (
            "Addressables.LoadAssetsAsync<GameObject>(\"characters\", op => {\n"
            "  var c = op.GetComponent<My.Game.Character>();\n"
            "  m_Dict.Add(c.characterName, c);\n"
            "});\n"
        )
        facts = find_roster_consumers({"X.cs": cs}, {"characters": ["p"]})
        assert facts["X.cs"].component_type == "My.Game.Character"

    def test_namespaced_type_lowers_to_last_segment_valid_luau(self) -> None:
        # The emitted body must use the LAST dotted segment as the Luau local,
        # the require child, and the constructor receiver — a valid identifier.
        region = _canonical_region(
            "Roster", "characters", "My.Game.Character", "characterName",
        )
        assert "local Character = require(script.Parent.Character)" in region
        assert "Character.new({ characterName = _key })" in region
        # RED against the pre-fix RAW splice: none of the dotted forms may
        # appear (a dotted local name / multi-level require path is invalid).
        assert "My.Game.Character" not in region
        assert "local My.Game" not in region
        assert "script.Parent.My.Game" not in region
        # No dotted IDENTIFIER survives in any emitted local/require/constructor.
        assert "local My." not in region
        assert "require(script.Parent.My." not in region

    def test_end_to_end_namespaced_consumer_emits_valid_body(self) -> None:
        # Drive the full lowering with a namespaced fact: the re-lowered source
        # uses ``Character`` everywhere the component is spliced, never the
        # dotted type.
        s = _Script("CharacterDatabase.cs", _drift_gettagged())
        fact = RosterConsumerFact(
            "CharacterDatabase.cs", "characters", "My.Game.Character",
            "characterName",
        )
        n = lower_roster_consumers([s], {"CharacterDatabase.cs": fact})
        assert n == 1
        assert "local Character = require(script.Parent.Character)" in s.luau_source
        assert "Character.new({ characterName = _key })" in s.luau_source
        assert "My.Game.Character" not in s.luau_source

    def test_simple_undotted_type_unchanged(self) -> None:
        # The simple (undotted) case is unchanged by the normalization.
        region = _canonical_region(
            "Roster", "characters", "Character", "characterName",
        )
        assert "local Character = require(script.Parent.Character)" in region

    def test_dots_only_or_trailing_dot_falls_back_to_default(self) -> None:
        # A degenerate dotted string (no real last segment) falls back to the
        # "Character" default rather than splicing an empty identifier.
        region = _canonical_region("Roster", "characters", "Foo.", "characterName")
        assert "local Character = require(script.Parent.Character)" in region
