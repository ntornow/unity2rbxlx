"""Tests for unity.addressables_resolver — parsing Addressables groups +
resolving addresses/labels to scene-runtime prefab ids."""

from __future__ import annotations

from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

from unity.addressables_resolver import (
    parse_addressables,
    resolve_prefab_addressables,
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


def _guid_index(mapping: dict[str, str]):
    # guid -> relative path; build entries with asset_path/relative_path.
    entries = {}
    for guid, rel in mapping.items():
        entries[guid] = SimpleNamespace(asset_path=Path("/proj") / rel, relative_path=Path(rel))
    return SimpleNamespace(guid_to_entry=entries)


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

    def test_prefab_id_rel_is_posix_normalized(self, tmp_path):
        """Slice 1.2 / D11: ``prefab_id_for`` normalizes the rel via
        ``.as_posix()`` so a Windows-native ``relative_path`` (backslashes)
        does NOT skew the prefab_id away from the byte-identical id the
        planner / scene_converter ``_prefab_stable_id`` produce (which are
        always forward-slashed)."""
        from types import SimpleNamespace

        idx = parse_addressables(_project(tmp_path, GROUP))
        # ``relative_path`` carries OS-native backslashes (PureWindowsPath
        # round-trips to a backslashed str); the resolver must forward-slash it.
        gi = SimpleNamespace(guid_to_entry={
            "catguid": SimpleNamespace(
                asset_path=PureWindowsPath(
                    r"C:\proj\Assets\Bundles\Characters\Cat\character.prefab"),
                relative_path=PureWindowsPath(
                    r"Assets\Bundles\Characters\Cat\character.prefab"),
            ),
        })
        res = resolve_prefab_addressables(idx, gi)
        assert res.by_address["Trash Cat"] == [
            "catguid:Assets/Bundles/Characters/Cat/character.prefab",
        ]
        assert "\\" not in next(iter(res.prefab_ids))
