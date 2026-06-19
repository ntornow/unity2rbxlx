"""Tests for unity.addressables_resolver — parsing Addressables groups +
resolving addresses/labels to scene-runtime prefab ids."""

from __future__ import annotations

from pathlib import Path

from core.unity_types import GuidEntry, GuidIndex
from unity.addressables_resolver import (
    parse_addressables,
    resolve_prefab_addressables,
    resolve_scriptable_object_addressables,
    resolve_so_assetref_prefab_ids,
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


# --- L0: SO-AssetReference prefab inclusion (gap #5 spawn closure) -----------

# A ThemeData-style SO .asset carrying prefab refs in BOTH spellings the walk
# must catch: ``m_AssetGUID:`` (AssetReference, prefabList) and ``guid:``
# (object-ref, collectiblePrefab/cloudPrefabs), plus a NON-prefab ref (a Sprite
# themeIcon + the m_Script .cs) that must be DROPPED by the .prefab filter.
# Unity guids are exactly 32 hex chars; the resolver's token regexes require that
# (a deterministic guard). Use realistic 32-hex guids so the fixture matches the
# real serialized shape, not a synthetic short token the regex would reject.
_SEG1 = "11111111111111111111111111111111"
_SEG2 = "22222222222222222222222222222222"
_COIN = "33333333333333333333333333333333"
_CLOUD = "44444444444444444444444444444444"
_SPRITE = "55555555555555555555555555555555"
_SCRIPT = "66666666666666666666666666666666"
_THEME = "77777777777777777777777777777777"

SO_ASSET = f"""\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_Script: {{fileID: 11500000, guid: {_SCRIPT}, type: 3}}
  m_Name: themeData
  themeIcon: {{fileID: 21300036, guid: {_SPRITE}, type: 3}}
  zones:
  - prefabList:
    - m_AssetGUID: {_SEG1}
      m_CachedAsset: {{fileID: 1000, guid: {_SEG1}, type: 3}}
    - m_AssetGUID: {_SEG2}
      m_CachedAsset: {{fileID: 1001, guid: {_SEG2}, type: 3}}
  collectiblePrefab: {{fileID: 184264, guid: {_COIN}, type: 3}}
  cloudPrefabs:
  - {{fileID: 1002, guid: {_CLOUD}, type: 3}}
"""


def _so_guid_index(tmp_path: Path) -> tuple[GuidIndex, str]:
    """A GuidIndex with: one EMITTED SO (.asset on real disk) + prefab guids +
    a non-prefab sprite + a .cs script guid. Returns (index, so_guid)."""
    asset_file = tmp_path / "themeData.asset"
    asset_file.write_text(SO_ASSET, encoding="utf-8")
    project_root = tmp_path
    index = GuidIndex(project_root=project_root)

    def _add(guid: str, abspath: Path, rel: str, kind: str) -> None:
        index.guid_to_entry[guid] = GuidEntry(
            guid=guid, asset_path=abspath, relative_path=Path(rel), kind=kind,
        )

    so_guid = _THEME
    _add(so_guid, asset_file, "themeData.asset", "scriptableobject")
    for g, rel in (
        (_SEG1, "Themes/Seg1.prefab"),
        (_SEG2, "Themes/Seg2.prefab"),
        (_COIN, "Prefabs/Coin.prefab"),
        (_CLOUD, "Sky/Cloud.prefab"),
    ):
        _add(g, project_root / rel, rel, "prefab")
    _add(_SPRITE, project_root / "Icon.png", "Icon.png", "texture")
    _add(_SCRIPT, project_root / "Theme.cs", "Theme.cs", "script")
    return index, so_guid


class TestSoAssetRefPrefabResolver:
    def test_resolves_prefab_ids_from_both_ref_spellings(self, tmp_path):
        index, so_guid = _so_guid_index(tmp_path)
        ids = resolve_so_assetref_prefab_ids({so_guid}, index)
        # All four prefab refs are recovered (AssetReference + object-ref forms).
        assert any(i.startswith(f"{_SEG1}:") for i in ids)
        assert any(i.startswith(f"{_SEG2}:") for i in ids)
        assert any(i.startswith(f"{_COIN}:") for i in ids)
        assert any(i.startswith(f"{_CLOUD}:") for i in ids)
        assert len(ids) == 4

    def test_non_prefab_refs_excluded(self, tmp_path):
        index, so_guid = _so_guid_index(tmp_path)
        ids = resolve_so_assetref_prefab_ids({so_guid}, index)
        # The Sprite themeIcon and the m_Script .cs are NOT prefabs -> dropped.
        assert not any(i.startswith(f"{_SPRITE}:") for i in ids)
        assert not any(i.startswith(f"{_SCRIPT}:") for i in ids)

    def test_so_guid_gate_excludes_non_emitted_so(self, tmp_path):
        # The SO file exists + carries prefab refs, but it is NOT in the emitted
        # so_guids set -> nothing reachable (the positive-evidence gate).
        index, _ = _so_guid_index(tmp_path)
        ids = resolve_so_assetref_prefab_ids(set(), index)
        assert ids == set()

    def test_unresolvable_so_guid_fails_soft(self, tmp_path):
        # An so_guid not in the index resolves to no .asset path -> no crash, {}.
        index, _ = _so_guid_index(tmp_path)
        ids = resolve_so_assetref_prefab_ids({"99999999999999999999999999999999"}, index)
        assert ids == set()

    def test_disjoint_from_prefab_address_surface(self, tmp_path):
        # The L0 SO walk must not perturb the AddressableAssetsData prefab surface
        # (it feeds the emit set, not by_address/by_label). Resolve both from the
        # same project and assert they are independent.
        index, so_guid = _so_guid_index(tmp_path)
        # No AssetGroups dir here -> the prefab address surface is empty.
        pa = parse_addressables(tmp_path)
        assert pa.by_address == {} and pa.by_label == {}
        ids = resolve_so_assetref_prefab_ids({so_guid}, index)
        assert len(ids) == 4  # the SO surface is populated independently


class TestPersistedSoPrefabIdsAxis:
    """AC5: the L0 so-prefab ids land in the PERSISTED ``addressables`` block under
    the ``so_prefab_ids`` axis (a FLAT list, not a label/address dict), and the
    emit-gate consumers read that axis so the prefabs pass the emission gate."""

    def test_consumer_loop_reads_so_prefab_ids_axis(self):
        # Drives the REAL shared consumer (``prefab_packages.collect_addressable_prefab_ids``)
        # that BOTH pipeline emit-gate arms (``_generate_prefab_packages`` and the
        # collision-rekey pass) call — NOT a re-implemented inline loop. The
        # by_address/by_label axes are label->[ids] dicts; so_prefab_ids (L0 gap #5)
        # is a FLAT list read directly. If the real arm dropped the so_prefab_ids
        # read, this would go RED (no longer green-for-wrong-reason).
        from converter.prefab_packages import collect_addressable_prefab_ids

        addr_block = {
            "by_address": {"Coin": ["coin:Prefabs/Coin.prefab"]},
            "by_label": {},
            "so_prefab_ids": [
                "seg1:Themes/Seg1.prefab",
                "seg2:Themes/Seg2.prefab",
            ],
        }
        addressable_prefab_ids = collect_addressable_prefab_ids(addr_block)
        assert "seg1:Themes/Seg1.prefab" in addressable_prefab_ids
        assert "seg2:Themes/Seg2.prefab" in addressable_prefab_ids
        assert "coin:Prefabs/Coin.prefab" in addressable_prefab_ids
        # A FLAT-list mis-read via ``.values()`` (the bug the axis guards against)
        # would iterate the id STRINGS char-by-char — assert that did not happen.
        assert all(len(pid) > 1 for pid in addressable_prefab_ids)

    def test_so_prefab_ids_pass_emit_gate(self):
        # AC5(b): the so-prefab ids pass ``select_emitted_prefab_ids`` (i.e.
        # ``_is_emitted``) — a prefab id in the addressable set is emitted even
        # with no prefab library entry (the union arm at prefab_packages.py:222).
        from converter.prefab_packages import select_emitted_prefab_ids

        so_ids = {"seg1:Themes/Seg1.prefab", "seg2:Themes/Seg2.prefab"}
        emitted = select_emitted_prefab_ids(None, None, so_ids)
        assert so_ids <= emitted
