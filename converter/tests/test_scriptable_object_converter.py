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


class TestPrefabRefResolution:
    """SO object-ref fields that point at a ``.prefab`` resolve to the Unit-1
    canonical prefab id (``"<guid>:<relative_path>"``) at SO-emit time;
    non-prefab / unresolvable / no-index refs stay ``nil`` (fail-soft)."""

    def _real_index(self, project_root: Path, *entries):
        """Build a real ``GuidIndex`` (non-optional ``Path`` project_root) with
        the given ``(guid, relative_path, kind)`` entries. A real project_root
        under which ``asset_path`` lives is REQUIRED for the full
        ``<guid>:<path>`` id (else canonical_prefab_id returns a bare guid —
        edge case 9b — and the byte-match would pass for the wrong reason)."""
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

    def test_non_identifier_key_dict_ref_resolves(self, tmp_path):
        # A nested dict whose key is NOT a valid Python identifier (hyphen, no
        # m_ prefix to strip) → emit takes the ``["<key>"] = ...`` arm (:120).
        # Proves prefab-ref resolution threads through that recursion arm too.
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
        # Genuinely the non-identifier-key arm: bracketed-string key form.
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
        idx = self._real_index(proj, (PICKUP_GUID, PICKUP_REL, "prefab"))
        f = self._write_asset(
            tmp_path,
            f"  collectiblePrefab: {{fileID: 184264, guid: {PICKUP_GUID}, type: 3}}\n"
            "  themeIcon: {fileID: 21300036, guid: e1a536f74c7ef384a8d7132148ace0c8}\n",
        )
        with caplog.at_level(logging.INFO, logger="converter.scriptable_object_converter"):
            convert_asset_file(f, idx)
        assert "resolved to prefab ids" in caplog.text

    # --- belt-and-suspenders: real project parse, path-skipped -----------

    def test_real_project_byte_match(self):
        import pytest

        real_root = Path("/Users/jiazou/workspace/trash-dash")
        if not (real_root / "Assets" / "Prefabs" / "Pickup.prefab").exists():
            pytest.skip("trash-dash source project not on disk")
        from unity.guid_resolver import build_guid_index
        from unity.prefab_ref import prefab_id_for_ref

        idx = build_guid_index(real_root)
        pid = prefab_id_for_ref(
            {"guid": PICKUP_GUID, "fileID": 184264, "type": 3}, idx
        )
        assert pid == PICKUP_ID
