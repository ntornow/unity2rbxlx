"""Tests for the Addressables Unit-4 roster producer (Phase 1).

Covers the pure ``assemble_rosters`` + ``resolve_roster_container_name`` and the
pipeline ``_collect_character_names`` field-presence selector. Acceptance
criteria: AC1 (channel typed), AC2 (assembly keyed on prefab_id), AC3
(field-presence characterName), AC8 (generic abstention), AC9 (dedup +
idempotency), AC10 (malformed config), plus edge cases E1–E3, E5, E9.
"""

import logging
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
import sys
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.roblox_types import (  # noqa: E402
    RbxAttrValue,
    RbxPlace,
    RbxRoster,
    RbxRosterMember,
)
from converter.roster_assembly import (  # noqa: E402
    DEFAULT_ROSTER_CONTAINER,
    ROSTER_TAG_MARKER,
    assemble_rosters,
    resolve_roster_container_name,
)
from converter.pipeline import Pipeline  # noqa: E402


# --- AC1 — channel exists & typed --------------------------------------------

def test_ac1_channel_exists_and_typed():
    """RbxPlace.rosters defaults to [] and holds RbxRoster/RbxRosterMember."""
    place = RbxPlace()
    assert place.rosters == []
    m = RbxRosterMember(template_name="Cat_abc123", tag="characters",
                        attributes={"characterName": "Cat"})
    r = RbxRoster(label="characters", members=[m])
    place.rosters.append(r)
    assert place.rosters[0].label == "characters"
    assert place.rosters[0].members[0].template_name == "Cat_abc123"
    assert place.rosters[0].members[0].tag == "characters"
    # attributes value type is the scalar union
    val: RbxAttrValue = place.rosters[0].members[0].attributes["characterName"]
    assert val == "Cat"


# --- AC2 — assembly from by_label, keyed on prefab_id ------------------------

def test_ac2_assembly_keyed_on_prefab_id_not_address_or_name():
    """template_name is resolved from prefab_id, NOT address/characterName."""
    by_label = {"characters": ["pidCat", "pidRaccoon"]}
    resolved = {"pidCat": "Cat_aaa111", "pidRaccoon": "Raccoon_bbb222"}
    emitted = {"Cat_aaa111", "Raccoon_bbb222"}
    # characterName intentionally DIFFERS from template_name and from address
    char_names = {"pidCat": "Trash Cat", "pidRaccoon": "Rubbish Raccoon"}
    rosters = assemble_rosters(by_label, resolved, emitted, char_names)
    assert len(rosters) == 1
    roster = rosters[0]
    assert roster.label == "characters"
    assert len(roster.members) == 2
    by_tname = {m.template_name: m for m in roster.members}
    assert set(by_tname) == {"Cat_aaa111", "Raccoon_bbb222"}
    assert by_tname["Cat_aaa111"].tag == "characters"
    assert by_tname["Cat_aaa111"].attributes["characterName"] == "Trash Cat"
    assert by_tname["Raccoon_bbb222"].attributes["characterName"] == "Rubbish Raccoon"


def test_ac2_multi_label():
    by_label = {"characters": ["pidA"], "consumables": ["pidB"]}
    resolved = {"pidA": "A_1", "pidB": "B_2"}
    emitted = {"A_1", "B_2"}
    rosters = assemble_rosters(by_label, resolved, emitted, {})
    labels = {r.label for r in rosters}
    assert labels == {"characters", "consumables"}
    for r in rosters:
        assert all(m.tag == r.label for m in r.members)


# --- AC8 / E1 — generic abstention -------------------------------------------

def test_ac8_e1_empty_by_label_returns_empty():
    assert assemble_rosters({}, {}, set(), {}) == []


def test_ac8_no_game_specific_literal_in_source():
    """The producer module carries no 'characters'/Trash-Dash literal."""
    src = (REPO_ROOT / "converter" / "roster_assembly.py").read_text()
    lowered = src.lower()
    assert "trash" not in lowered
    assert "trash-dash" not in lowered
    # The container default is a fixed generic literal, never a label/group.
    assert DEFAULT_ROSTER_CONTAINER == "RosterMembers"
    assert "characters" not in lowered  # never a hardcoded label string


# --- E2 — zero-member label ---------------------------------------------------

def test_e2_zero_surviving_members_yields_no_roster():
    by_label = {"characters": ["pidA"]}
    resolved = {"pidA": "A_1"}
    emitted: set[str] = set()  # template NOT emitted
    rosters = assemble_rosters(by_label, resolved, emitted, {})
    assert rosters == []


# --- E3 — prefab_id with no emitted template ---------------------------------

def test_e3_unemitted_template_skipped_survivors_remain():
    by_label = {"characters": ["pidA", "pidB"]}
    resolved = {"pidA": "A_1", "pidB": "B_2"}
    emitted = {"A_1"}  # B_2 dropped
    rosters = assemble_rosters(by_label, resolved, emitted, {})
    assert len(rosters) == 1
    names = {m.template_name for m in rosters[0].members}
    assert names == {"A_1"}


def test_e3_prefab_id_with_no_resolved_template_skipped():
    by_label = {"characters": ["pidA", "pidMissing"]}
    resolved = {"pidA": "A_1"}  # pidMissing absent
    emitted = {"A_1"}
    rosters = assemble_rosters(by_label, resolved, emitted, {})
    assert {m.template_name for m in rosters[0].members} == {"A_1"}


# --- AC9 / E5 — dedup on (label, prefab_id) + idempotency --------------------

def test_ac9_e5_duplicate_prefab_id_emits_member_once():
    by_label = {"characters": ["pidA", "pidA", "pidB"]}
    resolved = {"pidA": "A_1", "pidB": "B_2"}
    emitted = {"A_1", "B_2"}
    rosters = assemble_rosters(by_label, resolved, emitted, {})
    names = [m.template_name for m in rosters[0].members]
    assert names.count("A_1") == 1
    assert sorted(names) == ["A_1", "B_2"]


def test_ac9_twice_call_idempotent_pure():
    by_label = {"characters": ["pidB", "pidA", "pidA"]}
    resolved = {"pidA": "A_1", "pidB": "B_2"}
    emitted = {"A_1", "B_2"}
    out1 = assemble_rosters(by_label, resolved, emitted, {})
    out2 = assemble_rosters(by_label, resolved, emitted, {})
    # Identical, deterministic, sorted output; inputs unmutated.
    assert [(r.label, [m.template_name for m in r.members]) for r in out1] == \
           [(r.label, [m.template_name for m in r.members]) for r in out2]
    assert [m.template_name for m in out1[0].members] == ["A_1", "B_2"]
    assert by_label["characters"] == ["pidB", "pidA", "pidA"]  # input untouched


# --- AC10 / E9 — malformed config / non-str narrowing ------------------------

def test_ac10_non_str_characterName_omitted_member_still_tagged():
    by_label = {"characters": ["pidA"]}
    resolved = {"pidA": "A_1"}
    emitted = {"A_1"}
    char_names = {"pidA": 12345}  # type: ignore[dict-item]  # non-str
    rosters = assemble_rosters(by_label, resolved, emitted, char_names)
    m = rosters[0].members[0]
    assert m.template_name == "A_1"
    assert m.tag == "characters"
    assert "characterName" not in m.attributes  # omitted, not coerced


def test_e9_non_str_template_name_skips_member():
    by_label = {"characters": ["pidA", "pidB"]}
    resolved = {"pidA": "A_1", "pidB": 999}  # type: ignore[dict-item]
    emitted = {"A_1"}
    rosters = assemble_rosters(by_label, resolved, emitted, {})
    assert {m.template_name for m in rosters[0].members} == {"A_1"}


def test_e9_non_str_label_skipped():
    by_label = {123: ["pidA"], "characters": ["pidA"]}  # type: ignore[dict-item]
    resolved = {"pidA": "A_1"}
    emitted = {"A_1"}
    rosters = assemble_rosters(by_label, resolved, emitted, {})
    labels = {r.label for r in rosters}
    assert labels == {"characters"}


def test_member_without_characterName_still_tagged():
    by_label = {"characters": ["pidA"]}
    resolved = {"pidA": "A_1"}
    emitted = {"A_1"}
    rosters = assemble_rosters(by_label, resolved, emitted, {})  # no names
    m = rosters[0].members[0]
    assert m.tag == "characters"
    assert m.attributes == {}


# --- resolve_roster_container_name (E4/E11) ----------------------------------

def test_container_name_default_when_clear():
    assert resolve_roster_container_name(set()) == "RosterMembers"


def test_container_name_disambiguates_on_collision():
    reserved = {"RosterMembers"}
    name = resolve_roster_container_name(reserved)
    assert name == "RosterMembers_1"


def test_container_name_disambiguates_multiple():
    reserved = {"RosterMembers", "RosterMembers_1", "RosterMembers_2"}
    name = resolve_roster_container_name(reserved)
    assert name == "RosterMembers_3"


# --- AC3 — _collect_character_names field-presence selector -------------------

def _collect(scene_runtime):
    """Call the selector (uses no instance state) without a full Pipeline."""
    return Pipeline._collect_character_names(object(), scene_runtime)


def test_ac3_single_match_field_presence():
    """The instance whose config CONTAINS 'characterName' is selected — NOT by
    class literal; among 5 MonoBehaviour instances (mirroring the Cat prefab)."""
    sr = {
        "prefabs": {
            "pidCat": {
                "instances": [
                    {"instance_id": "pidCat:1", "script_id": "g_audio", "config": {"volume": 1}},
                    {"instance_id": "pidCat:2", "script_id": "g_char",
                     "config": {"characterName": "Cat", "speed": 5}},
                    {"instance_id": "pidCat:3", "script_id": "g_acc1", "config": {"slot": 0}},
                    {"instance_id": "pidCat:4", "script_id": "g_acc2", "config": {"slot": 1}},
                    {"instance_id": "pidCat:5", "script_id": "g_acc3", "config": {"slot": 2}},
                ]
            }
        },
        "modules": {},
    }
    assert _collect(sr) == {"pidCat": "Cat"}


def test_ac3_no_match_omitted():
    sr = {"prefabs": {"pidA": {"instances": [
        {"instance_id": "pidA:1", "script_id": "g1", "config": {"x": 1}},
    ]}}, "modules": {}}
    assert _collect(sr) == {}


def test_ac3_tiebreak_via_class_name(caplog):
    """Two instances both carry characterName → tiebreak via modules class_name."""
    sr = {
        "prefabs": {"pidA": {"instances": [
            {"instance_id": "pidA:1", "script_id": "g_other",
             "config": {"characterName": "WRONG"}},
            {"instance_id": "pidA:2", "script_id": "g_char",
             "config": {"characterName": "RIGHT"}},
        ]}},
        "modules": {
            "g_other": {"class_name": ""},      # no class name → not the winner
            "g_char": {"class_name": "Character"},
        },
    }
    assert _collect(sr) == {"pidA": "RIGHT"}


def test_ac3_remaining_tie_first_in_lifecycle_plus_warning(caplog):
    sr = {
        "prefabs": {"pidA": {"instances": [
            {"instance_id": "pidA:1", "script_id": "g1",
             "config": {"characterName": "FIRST"}},
            {"instance_id": "pidA:2", "script_id": "g2",
             "config": {"characterName": "SECOND"}},
        ]}},
        # Both carry class_name → tiebreak doesn't resolve to one.
        "modules": {
            "g1": {"class_name": "Character"},
            "g2": {"class_name": "Character"},
        },
    }
    with caplog.at_level(logging.WARNING):
        result = _collect(sr)
    assert result == {"pidA": "FIRST"}  # first-in-lifecycle
    assert any("characterName" in rec.message for rec in caplog.records)


def test_ac10_collect_non_str_characterName_omitted():
    sr = {"prefabs": {"pidA": {"instances": [
        {"instance_id": "pidA:1", "script_id": "g1",
         "config": {"characterName": 42}},
    ]}}, "modules": {}}
    assert _collect(sr) == {}


def test_collect_empty_scene_runtime():
    assert _collect({}) == {}
    assert _collect({"prefabs": "not a dict"}) == {}
