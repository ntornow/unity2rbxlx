"""Tests for unity.addressables_resolver — parsing Addressables groups +
resolving addresses/labels to scene-runtime prefab ids."""

from __future__ import annotations

from pathlib import Path

from core.unity_types import GuidEntry, GuidIndex
from unity.addressables_resolver import (
    parse_addressables,
    resolve_prefab_addressables,
    resolve_scriptable_object_addressables,
)

GROUP = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_Name: Characters
  m_GroupName: Characters
  m_SerializeEntries:
  - m_GUID: catguid
    m_Address: Trash Cat
    m_SerializedLabels:
    - characters
  - m_GUID: raccoonguid
    m_Address: Rubbish Raccoon
    m_SerializedLabels:
    - characters
  - m_GUID: uiguid
    m_Address: Assets/Prefabs/UI/Header.prefab
    m_SerializedLabels: []
  - m_GUID: spriteguid
    m_Address: SomeIcon
    m_SerializedLabels: []
"""


def _project(tmp_path: Path, *group_texts: str) -> Path:
    d = tmp_path / "Assets" / "AddressableAssetsData" / "AssetGroups"
    d.mkdir(parents=True)
    for i, txt in enumerate(group_texts):
        (d / f"Group{i}.asset").write_text(txt, encoding="utf-8")
    return tmp_path


def _classify(rel: str) -> str:
    return "prefab" if rel.endswith(".prefab") else "texture"


def _guid_index(mapping: dict[str, str], project_root: Path = Path("/proj")) -> GuidIndex:
    """Build a REAL ``GuidIndex`` (project-relative ``asset_path`` under
    ``project_root``) so the resolver computes prefab ids the production way —
    via ``unity.prefab_id.canonical_prefab_id`` against a real ``project_root``,
    not a hand-built ``guid:rel`` string."""
    index = GuidIndex(project_root=project_root)
    for guid, rel in mapping.items():
        asset_path = (project_root / rel)
        index.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=asset_path,
            relative_path=Path(rel),
            kind=_classify(rel),
        )
    return index


class TestParse:
    def test_parses_entries_addresses_labels(self, tmp_path):
        idx = parse_addressables(_project(tmp_path, GROUP))
        assert idx.by_address["Trash Cat"] == ["catguid"]
        assert set(idx.by_label["characters"]) == {"catguid", "raccoonguid"}
        assert idx.by_guid["raccoonguid"] == "Rubbish Raccoon"

    def test_missing_dir_is_empty(self, tmp_path):
        idx = parse_addressables(tmp_path)
        assert idx.entries == []

    def test_non_group_asset_contributes_nothing(self, tmp_path):
        settings = "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!114 &1\nMonoBehaviour:\n  m_Name: Settings\n"
        idx = parse_addressables(_project(tmp_path, settings))
        assert idx.entries == []

    def test_duplicate_address_keeps_both(self, tmp_path):
        g = GROUP.replace("Rubbish Raccoon", "Trash Cat")  # force dup address
        idx = parse_addressables(_project(tmp_path, g))
        assert set(idx.by_address["Trash Cat"]) == {"catguid", "raccoonguid"}


class TestResolve:
    def test_resolves_prefab_ids_and_filters_non_prefab(self, tmp_path):
        idx = parse_addressables(_project(tmp_path, GROUP))
        gi = _guid_index({
            "catguid": "Assets/Bundles/Characters/Cat/character.prefab",
            "raccoonguid": "Assets/Bundles/Characters/Raccoon/character.prefab",
            "uiguid": "Assets/Prefabs/UI/Header.prefab",
            "spriteguid": "Assets/Sprites/SomeIcon.png",  # NOT a prefab
        })
        res = resolve_prefab_addressables(idx, gi)
        assert res.by_address["Trash Cat"] == ["catguid:Assets/Bundles/Characters/Cat/character.prefab"]
        # sprite address dropped (non-prefab), counted as skipped
        assert "SomeIcon" not in res.by_address
        assert res.skipped_non_prefab == 1
        # label resolves to both character prefab ids
        assert len(res.by_label["characters"]) == 2

    def test_unknown_guid_dropped(self, tmp_path):
        idx = parse_addressables(_project(tmp_path, GROUP))
        res = resolve_prefab_addressables(idx, _guid_index({}))  # nothing resolves
        assert res.by_address == {}
        assert res.prefab_ids == set()

    def test_prefab_surface_unchanged_by_so_surface(self, tmp_path):
        """AC-8: the SO surface is additive and does NOT relax the shared
        ``.prefab`` filter — ``resolve_prefab_addressables`` output is identical
        whether or not the SO resolver is also called."""
        idx = parse_addressables(_project(tmp_path, GROUP))
        gi = _guid_index({
            "catguid": "Assets/Bundles/Characters/Cat/character.prefab",
            "raccoonguid": "Assets/Bundles/Characters/Raccoon/character.prefab",
            "uiguid": "Assets/Prefabs/UI/Header.prefab",
            "spriteguid": "Assets/Sprites/SomeIcon.png",
        })
        before = resolve_prefab_addressables(idx, gi)
        # Run the SO surface over the same index; it must not mutate anything.
        resolve_scriptable_object_addressables(idx, gi, {"spriteguid"})
        after = resolve_prefab_addressables(idx, gi)
        assert before.by_address == after.by_address
        assert before.by_label == after.by_label
        assert before.prefab_ids == after.prefab_ids

    def test_prefab_id_rel_is_posix_normalized(self, tmp_path):
        """The resolver routes through the shared ``canonical_prefab_id`` core,
        whose project-relative segment is always ``.as_posix()`` forward-
        slashed — so the resolver prefab_id is byte-identical with the planner /
        scene_converter ids regardless of the host OS path separator."""
        idx = parse_addressables(_project(tmp_path, GROUP))
        gi = _guid_index(
            {"catguid": "Assets/Bundles/Characters/Cat/character.prefab"},
            project_root=tmp_path,
        )
        res = resolve_prefab_addressables(idx, gi)
        assert res.by_address["Trash Cat"] == [
            "catguid:Assets/Bundles/Characters/Cat/character.prefab",
        ]
        assert "\\" not in next(iter(res.prefab_ids))


THEME_GROUP = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_Name: Themes
  m_GroupName: Themes
  m_SerializeEntries:
  - m_GUID: dayguid
    m_Address: themeData
    m_SerializedLabels:
    - themeData
  - m_GUID: nightguid
    m_Address: themeData
    m_SerializedLabels:
    - themeData
  - m_GUID: spriteguid
    m_Address: themeData
    m_SerializedLabels:
    - themeData
"""


class TestScriptableObjectSurface:
    """The PARALLEL SO-addressables surface (gated on positive evidence:
    an emitted SO module exists for the guid — not the absence of a .prefab)."""

    def test_retains_only_emitted_so_guids(self, tmp_path):
        idx = parse_addressables(_project(tmp_path, THEME_GROUP))
        # dayguid + nightguid emitted SO modules; spriteguid did NOT.
        so = resolve_scriptable_object_addressables(
            idx, _guid_index({}), {"dayguid", "nightguid"},
        )
        assert so.by_label["themeData"] == ["dayguid", "nightguid"]
        assert "spriteguid" not in so.so_guids
        assert so.so_guids == {"dayguid", "nightguid"}

    def test_address_axis_also_retained(self, tmp_path):
        idx = parse_addressables(_project(tmp_path, THEME_GROUP))
        so = resolve_scriptable_object_addressables(
            idx, _guid_index({}), {"dayguid", "nightguid"},
        )
        assert so.by_address["themeData"] == ["dayguid", "nightguid"]

    def test_empty_so_guids_yields_empty_surface(self, tmp_path):
        """Positive-evidence gate: with NO emitted SO modules, nothing is
        retained (never retained merely for failing the .prefab filter)."""
        idx = parse_addressables(_project(tmp_path, THEME_GROUP))
        so = resolve_scriptable_object_addressables(idx, _guid_index({}), set())
        assert so.by_label == {}
        assert so.by_address == {}
        assert so.so_guids == set()

    def test_dedupes_repeated_guid_per_label(self, tmp_path):
        group = THEME_GROUP.replace("nightguid", "dayguid")  # force dup guid
        idx = parse_addressables(_project(tmp_path, group))
        so = resolve_scriptable_object_addressables(idx, _guid_index({}), {"dayguid"})
        assert so.by_label["themeData"] == ["dayguid"]  # appears once

    def test_dedupes_repeated_guid_per_address(self, tmp_path):
        """Symmetric to the per-label dedupe: a guid repeated under the same
        ADDRESS is retained exactly once on the by_address axis."""
        group = THEME_GROUP.replace("nightguid", "dayguid")  # force dup guid
        idx = parse_addressables(_project(tmp_path, group))
        # Sanity: parse keeps both raw rows on the address axis before dedupe.
        assert idx.by_address["themeData"].count("dayguid") == 2
        so = resolve_scriptable_object_addressables(idx, _guid_index({}), {"dayguid"})
        assert so.by_address["themeData"] == ["dayguid"]  # appears once
