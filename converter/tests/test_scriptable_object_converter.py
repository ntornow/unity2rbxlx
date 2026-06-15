"""
test_scriptable_object_converter.py -- Unit tests for Unity ScriptableObject
to Luau ModuleScript conversion.
"""

from __future__ import annotations

from pathlib import Path

from converter.scriptable_object_converter import (
    AssetConversionResult,
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
