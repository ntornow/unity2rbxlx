"""
test_scriptable_object_converter.py -- Unit tests for Unity ScriptableObject
to Luau ModuleScript conversion.
"""

from __future__ import annotations

from pathlib import Path

from converter.scriptable_object_converter import (
    AssetConversionResult,
    RefResolveCounts,
    _value_to_lua,
    convert_asset_file,
    convert_asset_files,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASIC_ASSET = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_ObjectHideFlags: 0
  m_CorrespondingSourceObject: {fileID: 0}
  m_PrefabInstance: {fileID: 0}
  m_PrefabAsset: {fileID: 0}
  m_GameObject: {fileID: 0}
  m_Enabled: 1
  m_EditorHideFlags: 0
  m_Script: {fileID: 11500000, guid: abc123, type: 3}
  m_Name: MyDatabase
  m_EditorClassIdentifier:
  myInt: 42
  myString: hello world
  myFloat: 3.14
  myBool: 1
"""

NESTED_ASSET = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_ObjectHideFlags: 0
  m_Script: {fileID: 11500000, guid: def456, type: 3}
  m_Name: ItemDB
  items:
    - name: Sword
      damage: 10
      weight: 2.5
    - name: Shield
      damage: 0
      weight: 5.0
  metadata:
    version: 2
    author: test
"""

EMPTY_ASSET = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_ObjectHideFlags: 0
  m_Script: {fileID: 11500000, guid: ghi789, type: 3}
  m_Name: EmptyData
"""

OBJECT_REF_ASSET = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_ObjectHideFlags: 0
  m_Script: {fileID: 11500000, guid: jkl012, type: 3}
  m_Name: WithRef
  targetObject: {fileID: 12345, guid: abc123def456}
  speed: 5
"""

NOT_MONOBEHAVIOUR = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!1 &100000
GameObject:
  m_Name: SomeObject
"""


# ---------------------------------------------------------------------------
# convert_asset_file
# ---------------------------------------------------------------------------


class TestConvertAssetFile:
    """Test individual .asset file conversion."""

    def test_basic_fields(self, tmp_path):
        f = tmp_path / "MyDatabase.asset"
        f.write_text(BASIC_ASSET, encoding="utf-8")
        result = convert_asset_file(f)
        assert result is not None
        assert result.asset_name == "MyDatabase"
        assert result.field_count == 4
        assert "myInt = 42" in result.luau_source
        assert '"hello world"' in result.luau_source
        assert "3.14" in result.luau_source
        assert "return data" in result.luau_source

    def test_nested_lists_and_dicts(self, tmp_path):
        f = tmp_path / "ItemDB.asset"
        f.write_text(NESTED_ASSET, encoding="utf-8")
        result = convert_asset_file(f)
        assert result is not None
        assert result.asset_name == "ItemDB"
        assert "Sword" in result.luau_source
        assert "Shield" in result.luau_source
        assert "damage = 10" in result.luau_source
        assert "version = 2" in result.luau_source

    def test_empty_asset(self, tmp_path):
        f = tmp_path / "EmptyData.asset"
        f.write_text(EMPTY_ASSET, encoding="utf-8")
        result = convert_asset_file(f)
        assert result is not None
        assert result.field_count == 0
        # No user fields → an empty data table. Without a guid_index there is
        # no class link, so it returns the bare data (legacy behavior).
        assert "local data = {}" in result.luau_source
        assert result.luau_source.rstrip().endswith("return data")

    def test_unity_object_reference_becomes_nil(self, tmp_path):
        f = tmp_path / "WithRef.asset"
        f.write_text(OBJECT_REF_ASSET, encoding="utf-8")
        result = convert_asset_file(f)
        assert result is not None
        assert "nil" in result.luau_source
        assert "Unity object reference" in result.luau_source
        assert "speed = 5" in result.luau_source

    def test_skip_fields_filtered(self, tmp_path):
        f = tmp_path / "Test.asset"
        f.write_text(BASIC_ASSET, encoding="utf-8")
        result = convert_asset_file(f)
        assert result is not None
        assert "ObjectHideFlags" not in result.luau_source
        assert "m_Script" not in result.luau_source
        assert "m_PrefabInstance" not in result.luau_source

    def test_not_monobehaviour_returns_none(self, tmp_path):
        f = tmp_path / "NotMB.asset"
        f.write_text(NOT_MONOBEHAVIOUR, encoding="utf-8")
        result = convert_asset_file(f)
        assert result is None

    def test_invalid_yaml_returns_none(self, tmp_path):
        f = tmp_path / "bad.asset"
        f.write_text("{{{{not yaml at all", encoding="utf-8")
        result = convert_asset_file(f)
        assert result is None

    def test_m_prefix_stripped(self, tmp_path):
        asset_text = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_Script: {fileID: 11500000, guid: abc, type: 3}
  m_Name: PrefixTest
  m_Health: 100
  m_Speed: 5.5
"""
        f = tmp_path / "PrefixTest.asset"
        f.write_text(asset_text, encoding="utf-8")
        result = convert_asset_file(f)
        assert result is not None
        assert "Health = 100" in result.luau_source
        assert "Speed = 5.5" in result.luau_source

    def test_bool_conversion(self, tmp_path):
        f = tmp_path / "BoolTest.asset"
        f.write_text(BASIC_ASSET, encoding="utf-8")
        result = convert_asset_file(f)
        assert result is not None
        # YAML 1 → Python int 1, not bool — but the source should have it


# ---------------------------------------------------------------------------
# convert_asset_files (batch)
# ---------------------------------------------------------------------------


class TestConvertAssetFiles:
    """Test batch conversion across a Unity project directory."""

    def test_finds_assets_in_project(self, tmp_path):
        assets_dir = tmp_path / "Assets"
        assets_dir.mkdir()
        (assets_dir / "Data.asset").write_text(BASIC_ASSET, encoding="utf-8")
        (assets_dir / "Items.asset").write_text(NESTED_ASSET, encoding="utf-8")
        result = convert_asset_files(tmp_path)
        assert result.total == 2
        assert result.converted == 2
        assert len(result.assets) == 2

    def test_no_assets_dir(self, tmp_path):
        result = convert_asset_files(tmp_path)
        assert result.total == 0
        assert result.converted == 0

    def test_mixed_valid_and_invalid(self, tmp_path):
        assets_dir = tmp_path / "Assets"
        assets_dir.mkdir()
        (assets_dir / "Good.asset").write_text(BASIC_ASSET, encoding="utf-8")
        (assets_dir / "Bad.asset").write_text(NOT_MONOBEHAVIOUR, encoding="utf-8")
        result = convert_asset_files(tmp_path)
        assert result.total == 2
        assert result.converted == 1
        assert result.skipped == 1

    def test_nested_subdirectories(self, tmp_path):
        sub = tmp_path / "Assets" / "Data" / "Config"
        sub.mkdir(parents=True)
        (sub / "Nested.asset").write_text(BASIC_ASSET, encoding="utf-8")
        result = convert_asset_files(tmp_path)
        assert result.total == 1
        assert result.converted == 1

    def test_result_type(self, tmp_path):
        result = convert_asset_files(tmp_path)
        assert isinstance(result, AssetConversionResult)


class TestScriptableObjectClassLink:
    """A ScriptableObject asset IS an instance of its class — its data module
    links to the class so method calls (e.g. Load) resolve. Regression for the
    Trash Dash ``m_ConsumableDatabase:Load()`` 'missing method' crash."""

    from types import SimpleNamespace as _NS

    def _idx(self, guid, cs_path):
        from types import SimpleNamespace
        entry = SimpleNamespace(asset_path=Path(cs_path))
        return SimpleNamespace(guid_to_entry={guid: entry})

    ASSET = (
        "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
        "--- !u!114 &11400000\n"
        "MonoBehaviour:\n"
        "  m_Script: {fileID: 11500000, guid: abc123, type: 3}\n"
        "  m_Name: Consumables\n"
        "  consumbales: []\n"
    )

    def test_links_to_class_when_guid_resolves(self, tmp_path):
        f = tmp_path / "Consumables.asset"; f.write_text(self.ASSET, encoding="utf-8")
        idx = self._idx("abc123", "/proj/Assets/ConsumableDatabase.cs")
        src = convert_asset_file(f, idx).luau_source
        assert 'FindFirstChild("ConsumableDatabase", true)' in src
        # Lazy, fail-open binding (resolved on first miss; pcall + table check).
        assert "setmetatable(data, {" in src
        assert "__index = function" in src
        assert "pcall(require, _m)" in src

    def test_falls_back_to_bare_data_without_index(self, tmp_path):
        f = tmp_path / "Consumables.asset"; f.write_text(self.ASSET, encoding="utf-8")
        src = convert_asset_file(f).luau_source
        assert "setmetatable" not in src
        assert src.rstrip().endswith("return data")

    def test_no_link_when_class_name_equals_asset_name(self, tmp_path):
        # data module + class module would share a name → ambiguous FindFirstChild.
        f = tmp_path / "Consumables.asset"; f.write_text(self.ASSET, encoding="utf-8")
        idx = self._idx("abc123", "/proj/Assets/Consumables.cs")  # stem == asset m_Name
        src = convert_asset_file(f, idx).luau_source
        assert "setmetatable" not in src

    def test_no_link_when_guid_points_to_non_script(self, tmp_path):
        f = tmp_path / "Consumables.asset"; f.write_text(self.ASSET, encoding="utf-8")
        idx = self._idx("abc123", "/proj/Assets/Some.prefab")  # not .cs
        src = convert_asset_file(f, idx).luau_source
        assert "setmetatable" not in src


# ---------------------------------------------------------------------------
# SO object-ref -> prefab-id resolution at emit (Phase 2 / slice 2.1)
# ---------------------------------------------------------------------------

# The exact canonical prefab id (the scene_runtime.prefabs key Unit-1 holds).
PICKUP_GUID = "16cac8b68c4ca6448baecd0680e025f6"
PICKUP_REL = "Assets/Prefabs/Pickup.prefab"
PICKUP_ID = f"{PICKUP_GUID}:{PICKUP_REL}"

# Real Trash-Dash project (path-guarded — CI without the source skips).
_TRASH_DASH = Path("/Users/jiazou/workspace/trash-dash")


class TestPrefabRefResolution:
    """SO object-ref fields that point at a ``.prefab`` resolve to the Unit-1
    canonical prefab id (``"<guid>:<relative_path>"``) at SO-emit time;
    non-prefab / unresolvable / no-index refs stay ``nil`` (fail-soft)."""

    def _real_index(self, project_root: Path, *entries):
        """Build a real ``GuidIndex`` with the given ``(guid, relative_path,
        kind)`` entries. A real project_root under which ``asset_path`` lives is
        required for the full ``<guid>:<path>`` id (else canonical_prefab_id
        returns only a bare guid)."""
        from core.unity_types import GuidEntry, GuidIndex

        idx = GuidIndex(project_root=project_root)
        for guid, rel, kind in entries:
            rel_path = Path(rel)
            abs_path = project_root / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text("# stub asset\n", encoding="utf-8")
            idx.guid_to_entry[guid] = GuidEntry(
                guid=guid,
                asset_path=abs_path,
                relative_path=rel_path,
                kind=kind,
            )
        return idx

    def _write_asset(self, tmp_path, body: str) -> Path:
        text = (
            "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n"
            "--- !u!114 &11400000\n"
            "MonoBehaviour:\n"
            "  m_Script: {fileID: 11500000, guid: 99999, type: 3}\n"
            "  m_Name: themeData\n"
            f"{body}"
        )
        f = tmp_path / "themeData.asset"
        f.write_text(text, encoding="utf-8")
        return f

    # --- Acceptance #1: EXACT-id byte-match (real project_root) -----------

    def test_prefab_ref_resolves_to_exact_id(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            f"  collectiblePrefab: {{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        assert f'"{PICKUP_ID}"' in src

    def test_two_key_prefab_ref_resolves_to_exact_id(self, tmp_path):
        # The EXACT two-key {guid,fileID} ref shape (NO ``type`` key) must still
        # hit ``_value_to_lua``'s detection ``set(keys) <= {fileID,guid,type}``
        # and resolve. The prefab_ref unit tests bypass this glue-layer
        # detection, so this is the only guard that a regression requiring
        # ``type`` (e.g. ``== {fileID,guid,type}``) would surface — two-key refs
        # would silently fall through to the dict arm and never resolve.
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            f"  collectiblePrefab: {{guid: {PICKUP_GUID}, fileID: 184264}}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        assert f'"{PICKUP_ID}"' in src

    # --- Acceptance #2: non-prefab (sprite/mesh) stays nil ---------------

    def test_non_prefab_ref_stays_nil(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(
            proj,
            ("e1a536f74c7ef384a8d7132148ace0c8", "Assets/UI/themeIcon.png", "texture"),
            ("0fd40b0b70fef064c9b7e20779b1f8ec", "Assets/Meshes/sky.fbx", "mesh"),
        )
        f = self._write_asset(
            tmp_path,
            "  themeIcon: {fileID: 21300036, guid: e1a536f74c7ef384a8d7132148ace0c8}\n"
            "  skyMesh: {fileID: 4300002, guid: 0fd40b0b70fef064c9b7e20779b1f8ec}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        assert "Unity object reference" in src
        assert "themeIcon = nil" in src
        assert "skyMesh = nil" in src

    # --- Acceptance #3: no-index stays nil -------------------------------

    def test_no_index_keeps_prefab_ref_nil(self, tmp_path):
        f = self._write_asset(
            tmp_path,
            f"  collectiblePrefab: {{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n",
        )
        src = convert_asset_file(f).luau_source  # no guid_index
        assert PICKUP_ID not in src
        assert "collectiblePrefab = nil --[[(Unity object reference)]]" in src

    # --- Acceptance #4: missing / all-zero / dangling guid stays nil -----

    def test_fileid_only_ref_stays_nil(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(tmp_path, "  someRef: {fileID: 0}\n")
        src = convert_asset_file(f, idx).luau_source
        assert "someRef = nil --[[(Unity object reference)]]" in src

    def test_all_zero_guid_stays_nil(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            "  someRef: {fileID: 0, guid: 00000000000000000000000000000000}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        assert "someRef = nil --[[(Unity object reference)]]" in src

    def test_dangling_guid_stays_nil(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            "  someRef: {fileID: 1, guid: deadbeefdeadbeefdeadbeefdeadbeef}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        assert "someRef = nil --[[(Unity object reference)]]" in src

    # --- Acceptance #5: nested list + nested dict resolution --------------

    def test_nested_list_ref_resolves(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            "  cloudPrefabs:\n"
            f"    - {{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        assert f'"{PICKUP_ID}"' in src

    def test_nested_list_ref_counts(self, tmp_path):
        # A ``cloudPrefabs``-style list of refs (one prefab + one non-prefab):
        # the counter must reflect the nested refs, proving ``counts`` survives
        # the list-recursion arm.
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(
            proj,
            (PICKUP_GUID, PICKUP_REL, "prefab"),
            ("e1a536f74c7ef384a8d7132148ace0c8", "Assets/UI/themeIcon.png", "texture"),
        )
        fields = {
            "cloudPrefabs": [
                {"fileID": 184264, "guid": PICKUP_GUID, "type": 3},
                {"fileID": 21300036, "guid": "e1a536f74c7ef384a8d7132148ace0c8"},
            ],
        }
        counts = RefResolveCounts()
        out = _value_to_lua(fields, guid_index=idx, counts=counts)
        assert f'"{PICKUP_ID}"' in out
        assert counts.resolved == 1
        assert counts.skipped == 1

    def test_nested_dict_ref_resolves(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            "  prefabList:\n"
            "    - m_CachedAsset: "
            f"{{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        assert f'"{PICKUP_ID}"' in src

    def test_nested_dict_ref_counts(self, tmp_path):
        # A ``prefabList[].m_CachedAsset``-style nested dict ref: the counter
        # must reflect the nested ref, proving ``counts`` survives the dict-value
        # recursion arms (identifier-key and bracketed-key).
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        fields = {
            "prefabList": [
                {"m_CachedAsset": {"fileID": 184264, "guid": PICKUP_GUID, "type": 3}},
            ],
        }
        counts = RefResolveCounts()
        out = _value_to_lua(fields, guid_index=idx, counts=counts)
        assert f'"{PICKUP_ID}"' in out
        assert counts.resolved == 1
        assert counts.skipped == 0

    def test_non_identifier_key_dict_ref_resolves(self, tmp_path):
        # A nested dict whose key is not a valid Python identifier (hyphen) →
        # emit takes the ``["<key>"] = ...`` arm. Proves prefab-ref resolution
        # threads through that recursion arm too.
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            "  prefabMap:\n"
            "    bad-key: "
            f"{{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        # The non-identifier-key arm emits the bracketed-string key form.
        assert f'["bad-key"] = "{PICKUP_ID}"' in src

    # --- Acceptance #6: counters -----------------------------------------

    def test_counts_tally_resolved_and_skipped(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(
            proj,
            (PICKUP_GUID, PICKUP_REL, "prefab"),
            ("e1a536f74c7ef384a8d7132148ace0c8", "Assets/UI/themeIcon.png", "texture"),
        )
        fields = {
            "collectiblePrefab": {"fileID": 184264, "guid": PICKUP_GUID, "type": 3},
            "themeIcon": {"fileID": 21300036, "guid": "e1a536f74c7ef384a8d7132148ace0c8"},
            "missionPopup": {"fileID": 0},
        }
        counts = RefResolveCounts()
        out = _value_to_lua(fields, guid_index=idx, counts=counts)
        assert f'"{PICKUP_ID}"' in out
        assert counts.resolved == 1
        assert counts.skipped == 2

    def test_convert_asset_file_logs_counts(self, tmp_path, caplog):
        import logging

        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(
            proj,
            (PICKUP_GUID, PICKUP_REL, "prefab"),
            ("e1a536f74c7ef384a8d7132148ace0c8", "Assets/UI/themeIcon.png", "texture"),
        )
        f = self._write_asset(
            tmp_path,
            f"  collectiblePrefab: {{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n"
            "  themeIcon: {fileID: 21300036, guid: e1a536f74c7ef384a8d7132148ace0c8}\n",
        )
        with caplog.at_level(logging.INFO, logger="converter.scriptable_object_converter"):
            convert_asset_file(f, idx)
        # Assert the CONCRETE fixture counts and the asset name, not a generic
        # substring — one prefab ref resolved, one non-prefab (sprite) kept nil.
        # Swapped/hard-coded numbers or a wrong asset name would fail here.
        assert (
            "themeData.asset: 1 object-ref(s) resolved to prefab ids, 1 kept nil"
            in caplog.text
        )

    # --- real project parse (path-guarded, skips without the source) -----

    def test_real_project_byte_match(self):
        import pytest

        if not (_TRASH_DASH / "Assets" / "Prefabs" / "Pickup.prefab").exists():
            pytest.skip("trash-dash source project not on disk")
        from unity.guid_resolver import build_guid_index
        from unity.prefab_ref import prefab_id_for_ref

        idx = build_guid_index(_TRASH_DASH)
        pid = prefab_id_for_ref(
            {"guid": PICKUP_GUID, "fileID": 184264, "type": 3}, idx
        )
        assert pid == PICKUP_ID


class TestAssetReferenceResolution:
    """Unity AssetReference (Addressables) structs —
    ``{m_AssetGUID, m_CachedAsset[, m_SubObjectName]}`` — collapse to ONE
    prefab-id string in place of the whole dict at SO-emit time. Non-prefab /
    empty / dangling AssetReferences fall soft to ``nil``; a struct that merely
    carries an ``m_AssetGUID`` (no ``m_CachedAsset``, or with extra fields) is
    NOT swallowed and falls through to the generic-dict branch with its data
    intact. Built on ``TestPrefabRefResolution``'s harness."""

    # Reuse the existing harness helpers (same fixture shape).
    _real_index = TestPrefabRefResolution._real_index
    _write_asset = TestPrefabRefResolution._write_asset

    def _assetref(self, guid: str, file_id: int = 1000011175313116) -> dict:
        """A real-shaped Unity AssetReference: bare-guid m_AssetGUID + embedded
        {fileID,guid,type} m_CachedAsset whose guid matches (as Unity emits)."""
        return {
            "m_AssetGUID": guid,
            "m_CachedAsset": {"fileID": file_id, "guid": guid, "type": 3},
        }

    # --- AC-3: exact-id byte match on m_AssetGUID ------------------------

    def test_assetref_resolves_to_exact_id(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        fields = {"themePrefab": self._assetref(PICKUP_GUID)}
        out = _value_to_lua(fields, guid_index=idx)
        assert f'themePrefab = "{PICKUP_ID}"' in out

    # --- AC-1: prefabList emits a Luau list of STRINGS, not tables -------

    def test_prefab_list_emits_strings_not_tables(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            "  prefabList:\n"
            f"    - m_AssetGUID: {PICKUP_GUID}\n"
            f"      m_CachedAsset: {{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n"
            f"    - m_AssetGUID: {PICKUP_GUID}\n"
            f"      m_CachedAsset: {{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n",
        )
        src = convert_asset_file(f, idx).luau_source
        # EVERY element collapsed to a bare string literal, not a struct and not
        # a nil marker: both AssetReferences resolve to the same id, so the
        # resolved-string literal must appear exactly twice and NO AssetReference
        # nil marker may leak in. (A bare `"<id>" in src` would pass even if one
        # element silently collapsed to `nil --[[(Unity AssetReference)]]`.)
        assert src.count(f'"{PICKUP_ID}"') == 2
        assert "nil --[[(Unity AssetReference)]]" not in src
        assert "AssetGUID" not in src
        assert "CachedAsset" not in src

    # --- AC-2: consumer-shape match (byte-identical to object-ref arm) ----

    def test_assetref_string_matches_object_ref_string(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        # AssetReference and a plain {fileID,guid,type} object-ref on the SAME
        # guid must emit byte-identical prefab-id strings.
        ref_out = _value_to_lua(
            {"collectiblePrefab": {"fileID": 184264, "guid": PICKUP_GUID, "type": 3}},
            guid_index=idx,
        )
        ar_out = _value_to_lua(
            {"collectiblePrefab": self._assetref(PICKUP_GUID)}, guid_index=idx
        )
        assert ref_out == ar_out
        assert f'"{PICKUP_ID}"' in ar_out

    # --- AC-4: non-prefab / empty / all-zero / dangling -> nil -----------

    def test_non_prefab_assetref_stays_nil(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        guid = "e1a536f74c7ef384a8d7132148ace0c8"
        idx = self._real_index(proj, (guid, "Assets/UI/themeIcon.png", "texture"))
        out = _value_to_lua({"themeRef": self._assetref(guid)}, guid_index=idx)
        assert "themeRef = nil --[[(Unity AssetReference)]]" in out

    def test_empty_assetref_stays_nil(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        fields = {
            "themeRef": {
                "m_AssetGUID": "",
                "m_CachedAsset": {"fileID": 0, "guid": "", "type": 0},
            }
        }
        out = _value_to_lua(fields, guid_index=idx)
        assert "themeRef = nil --[[(Unity AssetReference)]]" in out

    def test_all_zero_assetref_stays_nil(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        zero = "0" * 32
        out = _value_to_lua({"themeRef": self._assetref(zero, file_id=0)}, guid_index=idx)
        assert "themeRef = nil --[[(Unity AssetReference)]]" in out

    def test_dangling_assetref_stays_nil(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        dangling = "deadbeefdeadbeefdeadbeefdeadbeef"
        out = _value_to_lua({"themeRef": self._assetref(dangling)}, guid_index=idx)
        assert "themeRef = nil --[[(Unity AssetReference)]]" in out

    # --- AC-8: m_AssetGUID-only struct (no m_CachedAsset) NOT swallowed ---

    def test_assetguid_only_struct_falls_through(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        # No m_CachedAsset -> not an AssetReference -> generic-dict branch.
        out = _value_to_lua({"themeRef": {"m_AssetGUID": PICKUP_GUID}}, guid_index=idx)
        assert "AssetReference" not in out
        # m_ prefix stripped -> AssetGUID key survives with its raw value.
        assert f'AssetGUID = "{PICKUP_GUID}"' in out

    def test_assetguid_with_subobjectname_only_falls_through(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        # {m_AssetGUID, m_SubObjectName} lacks m_CachedAsset -> not swallowed.
        fields = {"themeRef": {"m_AssetGUID": PICKUP_GUID, "m_SubObjectName": "wheel"}}
        out = _value_to_lua(fields, guid_index=idx)
        assert "AssetReference" not in out
        assert f'AssetGUID = "{PICKUP_GUID}"' in out
        assert 'SubObjectName = "wheel"' in out

    # --- AC-5: struct carrying m_AssetGUID + extra field is preserved ----

    def test_assetref_with_extra_field_preserved(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        # Has both required keys PLUS an unrelated field -> fails subset bound,
        # falls through to generic-dict; the extra field survives.
        fields = {
            "themeRef": {
                "m_AssetGUID": PICKUP_GUID,
                "m_CachedAsset": {"fileID": 184264, "guid": PICKUP_GUID, "type": 3},
                "weight": 3,
            }
        }
        out = _value_to_lua(fields, guid_index=idx)
        assert "AssetReference" not in out
        assert "weight = 3" in out
        # The inner m_CachedAsset object-ref still resolves via its own arm.
        assert f'"{PICKUP_ID}"' in out

    # --- m_SubObjectName present (sub-asset) -> resolves on parent --------

    def test_subobjectname_assetref_resolves(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        fields = {
            "themeRef": {
                "m_AssetGUID": PICKUP_GUID,
                "m_CachedAsset": {"fileID": 184264, "guid": PICKUP_GUID, "type": 3},
                "m_SubObjectName": "wheel",
            }
        }
        out = _value_to_lua(fields, guid_index=idx)
        assert f'themeRef = "{PICKUP_ID}"' in out

    # --- m_CachedAsset fallback when m_AssetGUID empty -------------------

    def test_cached_asset_fallback_resolves(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        # m_AssetGUID empty (misses index) -> fallback to m_CachedAsset guid.
        fields = {
            "themeRef": {
                "m_AssetGUID": "",
                "m_CachedAsset": {"fileID": 184264, "guid": PICKUP_GUID, "type": 3},
            }
        }
        out = _value_to_lua(fields, guid_index=idx)
        assert f'themeRef = "{PICKUP_ID}"' in out

    # --- AC-8 (no index) -> nil -----------------------------------------

    def test_no_index_assetref_stays_nil(self, tmp_path):
        out = _value_to_lua({"themeRef": self._assetref(PICKUP_GUID)})  # no index
        assert "themeRef = nil --[[(Unity AssetReference)]]" in out
        assert PICKUP_ID not in out

    # --- AC-9: counts tally resolved / skipped ---------------------------

    def test_assetref_counts(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        nonprefab = "e1a536f74c7ef384a8d7132148ace0c8"
        idx = self._real_index(
            proj,
            (PICKUP_GUID, PICKUP_REL, "prefab"),
            (nonprefab, "Assets/UI/themeIcon.png", "texture"),
        )
        fields = {
            "prefabList": [
                self._assetref(PICKUP_GUID),
                self._assetref(nonprefab),
            ]
        }
        counts = RefResolveCounts()
        out = _value_to_lua(fields, guid_index=idx, counts=counts)
        assert f'"{PICKUP_ID}"' in out
        assert "nil --[[(Unity AssetReference)]]" in out
        assert counts.resolved == 1
        assert counts.skipped == 1

    # --- empty / mixed list ---------------------------------------------

    def test_empty_prefab_list(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        out = _value_to_lua({"prefabList": []}, guid_index=idx)
        assert "prefabList = {}" in out

    # --- AC-6/AC-7: object-ref arm still resolves (no regression) --------

    def test_object_ref_arm_unaffected(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        # A plain {fileID,guid,type} object-ref still hits its own arm and emits
        # the object-ref nil marker for a non-prefab, distinct from AssetReference.
        out = _value_to_lua(
            {"collectiblePrefab": {"fileID": 184264, "guid": PICKUP_GUID, "type": 3}},
            guid_index=idx,
        )
        assert f'collectiblePrefab = "{PICKUP_ID}"' in out
        assert "AssetReference" not in out

    def test_nil_markers_stay_distinct_per_arm(self, tmp_path):
        # Regression pin: the two unresolved nil markers must stay DISTINCT post
        # Phase 1. An UNRESOLVABLE legacy object-ref ({fileID,guid,type} whose
        # guid is a non-prefab) emits the object-ref marker; an UNRESOLVABLE
        # AssetReference ({m_AssetGUID, m_CachedAsset} on the same non-prefab
        # guid) emits the AssetReference marker. Both arms exercised here against
        # the REAL _value_to_lua / GuidIndex (no stubbed state); if a refactor
        # collapsed the two markers to one string, exactly one of these arms
        # would flip to the wrong literal and fail.
        proj = tmp_path / "proj"
        proj.mkdir()
        nonprefab = "e1a536f74c7ef384a8d7132148ace0c8"
        idx = self._real_index(proj, (nonprefab, "Assets/UI/themeIcon.png", "texture"))
        # Legacy object-ref arm -> object-ref nil marker, NOT the AssetReference one.
        obj_out = _value_to_lua(
            {"objRef": {"fileID": 21300036, "guid": nonprefab, "type": 3}},
            guid_index=idx,
        )
        assert "objRef = nil --[[(Unity object reference)]]" in obj_out
        assert "Unity AssetReference" not in obj_out
        # AssetReference arm (same non-prefab guid) -> AssetReference nil marker,
        # NOT the object-ref one.
        ar_out = _value_to_lua({"arRef": self._assetref(nonprefab)}, guid_index=idx)
        assert "arRef = nil --[[(Unity AssetReference)]]" in ar_out
        assert "Unity object reference" not in ar_out
