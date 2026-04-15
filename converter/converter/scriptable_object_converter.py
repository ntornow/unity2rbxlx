"""
scriptable_object_converter.py — Converts Unity ScriptableObject .asset files to Luau data tables.

Unity ScriptableObject data assets (.asset files) are YAML files containing serialized
field data. This module parses them and generates Roblox ModuleScript source that
returns a Luau table with the same data.

Example Unity .asset:
    %YAML 1.1
    --- !u!114 &11400000
    MonoBehaviour:
      m_Name: MyDatabase
      myInt: 42
      myString: hello
      items:
        - name: Sword
          damage: 10

Generated Luau ModuleScript:
    -- Auto-generated from MyDatabase.asset
    local data = {
        myInt = 42,
        myString = "hello",
        items = {
            { name = "Sword", damage = 10 },
        },
    }
    return data

No other module is imported here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# Unity YAML document headers
_UNITY_YAML_HEADER = re.compile(r"^%YAML.*\n%TAG.*\n", re.MULTILINE)
_UNITY_DOC_SEPARATOR = re.compile(r"^--- !u!\d+ &\d+.*$", re.MULTILINE)

# Unity internal fields to skip (not user data)
_SKIP_FIELDS = {
    "m_ObjectHideFlags", "m_CorrespondingSourceObject", "m_PrefabInstance",
    "m_PrefabAsset", "m_GameObject", "m_Enabled", "m_EditorHideFlags",
    "m_EditorClassIdentifier", "m_Script",
}


@dataclass
class ConvertedAsset:
    """Result of converting a single .asset file."""
    source_path: Path
    asset_name: str
    luau_source: str
    field_count: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class AssetConversionResult:
    """Aggregate result of converting all .asset files."""
    assets: list[ConvertedAsset] = field(default_factory=list)
    total: int = 0
    converted: int = 0
    skipped: int = 0


def _lua_escape_string(s: str) -> str:
    """Escape a string for Luau literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _value_to_lua(value: Any, indent: int = 1) -> str:
    """Convert a Python value to a Luau literal string."""
    prefix = "\t" * indent
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value}"
    if isinstance(value, str):
        return f'"{_lua_escape_string(value)}"'
    if isinstance(value, list):
        if not value:
            return "{}"
        items = []
        for item in value:
            items.append(f"{prefix}\t{_value_to_lua(item, indent + 1)},")
        return "{\n" + "\n".join(items) + f"\n{prefix}}}"
    if isinstance(value, dict):
        if not value:
            return "{}"
        # Check if it looks like a Unity object reference (fileID/guid)
        if set(value.keys()) <= {"fileID", "guid", "type"}:
            return "nil --[[(Unity object reference)]]"
        items = []
        for k, v in value.items():
            if isinstance(k, str) and k.startswith("m_") and k in _SKIP_FIELDS:
                continue
            # Clean up Unity m_ prefix for readability
            clean_key = k
            if isinstance(k, str) and k.startswith("m_") and k not in ("m_Name",):
                clean_key = k[2:]  # strip m_ prefix
            # Lua key formatting
            if isinstance(clean_key, str) and clean_key.isidentifier():
                items.append(f"{prefix}\t{clean_key} = {_value_to_lua(v, indent + 1)},")
            else:
                items.append(f'{prefix}\t["{_lua_escape_string(str(clean_key))}"] = {_value_to_lua(v, indent + 1)},')
        if not items:
            return "{}"
        return "{\n" + "\n".join(items) + f"\n{prefix}}}"
    return str(value)


def convert_asset_file(asset_path: Path) -> ConvertedAsset | None:
    """
    Convert a single Unity .asset file to a Luau ModuleScript.

    Returns None if the file is not a valid ScriptableObject asset.
    """
    raw_text = asset_path.read_text(encoding="utf-8", errors="replace")

    # Strip Unity YAML header and doc separators
    cleaned = _UNITY_YAML_HEADER.sub("", raw_text, count=1)
    cleaned = _UNITY_DOC_SEPARATOR.sub("---", cleaned)

    try:
        docs = list(yaml.safe_load_all(cleaned))
    except yaml.YAMLError as exc:
        logger.warning("Failed to parse YAML in %s: %s", asset_path.name, exc)
        return None

    # Find the MonoBehaviour document (ScriptableObjects serialize as MonoBehaviour)
    data_body = None
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if "MonoBehaviour" in doc:
            data_body = doc["MonoBehaviour"]
            break

    if data_body is None:
        return None
    if not isinstance(data_body, dict):
        return None

    asset_name = data_body.get("m_Name", asset_path.stem)
    warnings: list[str] = []

    # Filter to user-defined fields only
    user_fields = {}
    for key, value in data_body.items():
        if key in _SKIP_FIELDS:
            continue
        if key == "m_Name":
            continue  # handled separately
        user_fields[key] = value

    if not user_fields:
        return ConvertedAsset(
            source_path=asset_path,
            asset_name=asset_name,
            luau_source=f"-- Auto-generated from {asset_path.name} (no user data fields)\nreturn {{}}\n",
            field_count=0,
            warnings=warnings,
        )

    # Generate Luau source
    lines = [
        f"-- Auto-generated from {asset_path.name}",
        f"-- ScriptableObject: {asset_name}",
        "",
        f"local data = {_value_to_lua(user_fields)}",
        "",
        "return data",
        "",
    ]

    return ConvertedAsset(
        source_path=asset_path,
        asset_name=asset_name,
        luau_source="\n".join(lines),
        field_count=len(user_fields),
        warnings=warnings,
    )


def convert_asset_files(unity_path: Path) -> AssetConversionResult:
    """
    Discover and convert all .asset files in a Unity project.

    Args:
        unity_path: Root of the Unity project.

    Returns:
        AssetConversionResult with converted ModuleScript sources.
    """
    result = AssetConversionResult()

    assets_dir = unity_path / "Assets"
    if not assets_dir.is_dir():
        return result

    for asset_file in assets_dir.rglob("*.asset"):
        result.total += 1
        converted = convert_asset_file(asset_file)
        if converted:
            result.assets.append(converted)
            result.converted += 1
        else:
            result.skipped += 1

    return result
