"""Converts Unity ScriptableObject .asset YAML to Luau ModuleScripts. Roblox
has no ScriptableObject equivalent, so we serialize each asset's field data
into a returned Luau table consumers can require()."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from unity.prefab_ref import prefab_id_for_ref, prefab_id_for_guid, GuidIndexLike

if TYPE_CHECKING:
    from unity.guid_resolver import GuidIndex

logger = logging.getLogger(__name__)

# A ScriptableObject class stem must be a bare Luau/C# identifier to be safe
# inside a generated string literal and a ``FindFirstChild`` lookup.
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")


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
class RefResolveCounts:
    """Tally of object-ref fields encountered while emitting one .asset."""
    resolved: int = 0   # {guid,fileID} ref -> a .prefab id
    skipped: int = 0    # non-prefab / fileID-only / missing-guid / no-index -> kept nil


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


# Unity AssetReference: m_AssetGUID + m_CachedAsset both required;
# m_SubObjectName (sub-asset) optional.
_ASSETREF_REQUIRED = {"m_AssetGUID", "m_CachedAsset"}
_ASSETREF_KEYS = {"m_AssetGUID", "m_CachedAsset", "m_SubObjectName"}


def _is_asset_reference(d: dict[object, object]) -> bool:
    """True iff *d* is a Unity AssetReference struct.

    Requiring m_CachedAsset (not just m_AssetGUID) and subset-bounding the keys
    keeps an unrelated m_AssetGUID-carrying struct from being swallowed — it
    falls through to the generic-dict branch with its data intact.
    """
    keys = set(d.keys())
    return _ASSETREF_REQUIRED <= keys <= _ASSETREF_KEYS


def _value_to_lua(
    value: object,
    indent: int = 1,
    guid_index: GuidIndexLike | None = None,
    counts: RefResolveCounts | None = None,
) -> str:
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
            items.append(f"{prefix}\t{_value_to_lua(item, indent + 1, guid_index, counts)},")
        return "{\n" + "\n".join(items) + f"\n{prefix}}}"
    if isinstance(value, dict):
        if not value:
            return "{}"
        # Unity AssetReference collapses to ONE prefab-id string in place of the
        # whole struct (checked before the disjoint {fileID,guid,type} arm).
        # Resolve on the bare-guid m_AssetGUID, falling back to the embedded
        # m_CachedAsset; both via the shared .prefab filter.
        if _is_asset_reference(value):
            pid = None
            if guid_index is not None:
                guid = value.get("m_AssetGUID")
                if isinstance(guid, str):
                    pid = prefab_id_for_guid(guid, guid_index)
                if pid is None:
                    cached = value.get("m_CachedAsset")
                    if isinstance(cached, dict):
                        pid = prefab_id_for_ref(cached, guid_index)
            if pid is not None:
                if counts is not None:
                    counts.resolved += 1
                return f'"{_lua_escape_string(pid)}"'
            if counts is not None:
                counts.skipped += 1
            return "nil --[[(Unity AssetReference)]]"
        # Check if it looks like a Unity object reference (fileID/guid)
        if set(value.keys()) <= {"fileID", "guid", "type"}:
            pid = prefab_id_for_ref(value, guid_index) if guid_index is not None else None
            if pid is not None:
                if counts is not None:
                    counts.resolved += 1
                return f'"{_lua_escape_string(pid)}"'
            if counts is not None:
                counts.skipped += 1
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
                items.append(f"{prefix}\t{clean_key} = {_value_to_lua(v, indent + 1, guid_index, counts)},")
            else:
                items.append(f'{prefix}\t["{_lua_escape_string(str(clean_key))}"] = {_value_to_lua(v, indent + 1, guid_index, counts)},')
        if not items:
            return "{}"
        return "{\n" + "\n".join(items) + f"\n{prefix}}}"
    return str(value)


def _resolve_script_class_stem(
    data_body: dict[str, object],
    guid_index: GuidIndex | None,
    asset_name: str,
) -> str | None:
    """Resolve the asset's backing ScriptableObject CLASS file stem from its
    ``m_Script`` GUID. A ScriptableObject asset IS an instance of its class, so
    the emitted module links its data to that class's methods. Returns ``None``
    (→ bare ``return data``, the legacy behavior) when the GUID is absent,
    doesn't resolve to a project ``.cs`` script, isn't a valid identifier, or
    equals the asset's own module name (which would make the runtime
    ``FindFirstChild`` self-resolve to the data module)."""
    if guid_index is None:
        return None
    m_script = data_body.get("m_Script")
    if not isinstance(m_script, dict):
        return None
    guid = m_script.get("guid")
    if not isinstance(guid, str) or not guid:
        return None
    entry = getattr(guid_index, "guid_to_entry", {}).get(guid)
    path = getattr(entry, "asset_path", None) if entry is not None else None
    if path is None or path.suffix != ".cs":
        return None
    stem = path.stem
    if not _IDENT_RE.match(stem) or stem == asset_name:
        return None
    return stem


def _so_return_lines(class_stem: str | None) -> list[str]:
    """Render the SO module's return statement. With a known class, wrap
    ``data`` so unresolved field/method lookups fall through to the class table
    (a ScriptableObject asset IS an instance of its class). The binding is:

    - LAZY: resolved on the first missing-key access, not at module load, so a
      class module that loads after this asset still binds (Roblox caches a
      module's return value for the session — eager resolution would freeze an
      early "not found" permanently).
    - FAIL-OPEN: ``pcall(require)`` + table check, and only the SUCCESS is
      memoized. A missing/broken class degrades to plain data (returns ``nil``
      for the key) instead of erroring the whole asset module.

    Falls back to bare ``return data`` when no class is resolved at build time
    (pure-data / built-in classes), so a field-only SO never regresses."""
    if not class_stem:
        return ["return data"]
    return [
        "-- ScriptableObject asset = instance of its class; lazy, fail-open",
        "-- method binding (see converter/scriptable_object_converter.py).",
        "local _cls = nil",
        "return setmetatable(data, {",
        "\t__index = function(_, _key)",
        "\t\tif _cls == nil then",
        f'\t\t\tlocal _m = game:GetService("ReplicatedStorage"):FindFirstChild("{class_stem}", true)',
        f'\t\t\t\tor game:GetService("ServerStorage"):FindFirstChild("{class_stem}", true)',
        "\t\t\tif _m ~= nil then",
        "\t\t\t\tlocal _ok, _mod = pcall(require, _m)",
        '\t\t\t\tif _ok and type(_mod) == "table" then _cls = _mod end',
        "\t\t\tend",
        "\t\t\tif _cls == nil then return nil end  -- unresolved; don't memoize the miss",
        "\t\tend",
        "\t\treturn _cls[_key]",
        "\tend,",
        "})",
    ]


def convert_asset_file(
    asset_path: Path,
    guid_index: GuidIndex | None = None,
) -> ConvertedAsset | None:
    """
    Convert a single Unity .asset file to a Luau ModuleScript.

    When *guid_index* is supplied, the asset's backing ScriptableObject class
    (its ``m_Script`` GUID) is linked so the asset's serialized data carries the
    class's methods (``setmetatable(data, {__index = require(<class>)})``).

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

    # Link the data to its ScriptableObject class so method calls resolve. Done
    # for the field-less case too: a class can carry methods with no serialized
    # fields. Falls back to bare ``data`` when the class can't be resolved.
    class_stem = _resolve_script_class_stem(data_body, guid_index, asset_name)
    class_note = f" (class: {class_stem})" if class_stem else ""
    counts = RefResolveCounts()
    data_literal = (
        _value_to_lua(user_fields, guid_index=guid_index, counts=counts)
        if user_fields else "{}"
    )
    if counts.resolved or counts.skipped:
        logger.info(
            "[scriptable_object] %s: %d object-ref(s) resolved to prefab ids, %d kept nil",
            asset_path.name, counts.resolved, counts.skipped,
        )

    lines = [
        f"-- Auto-generated from {asset_path.name}",
        f"-- ScriptableObject: {asset_name}{class_note}",
        "",
        f"local data = {data_literal}",
        "",
        *_so_return_lines(class_stem),
        "",
    ]

    return ConvertedAsset(
        source_path=asset_path,
        asset_name=asset_name,
        luau_source="\n".join(lines),
        field_count=len(user_fields),
        warnings=warnings,
    )


def convert_asset_files(
    unity_path: Path,
    guid_index: GuidIndex | None = None,
) -> AssetConversionResult:
    """
    Discover and convert all .asset files in a Unity project.

    Args:
        unity_path: Root of the Unity project.
        guid_index: Optional GUID index; when supplied, each asset's data is
            linked to its backing ScriptableObject class so method calls
            resolve (see ``convert_asset_file``).

    Returns:
        AssetConversionResult with converted ModuleScript sources.
    """
    result = AssetConversionResult()

    assets_dir = unity_path / "Assets"
    if not assets_dir.is_dir():
        return result

    for asset_file in assets_dir.rglob("*.asset"):
        result.total += 1
        converted = convert_asset_file(asset_file, guid_index)
        if converted:
            result.assets.append(converted)
            result.converted += 1
        else:
            result.skipped += 1

    return result


def resolve_unique_asset_names(assets: list[ConvertedAsset]) -> dict[int, str]:
    """Assign a unique on-disk stem to each ConvertedAsset.

    Two ScriptableObjects in different folders may share ``m_Name`` (e.g.
    ``Settings.asset`` in ``Audio/`` and ``Settings.asset`` in ``Graphics/``).
    Writing both as ``Settings.luau`` would let the second overwrite the
    first on disk and the dedupe-by-name in ``write_output`` would silently
    drop one from ``rbx_place.scripts``. Disambiguate collisions by
    appending ``__<hash6>`` of the project-relative source path — same
    scheme PR #87 used for duplicate C# class names.

    The hash is anchored at ``Assets/`` (the Unity project root marker) so
    converting the same project from different checkout roots emits the
    same ModuleScript names, and stale hashed modules don't accumulate
    in a preserved output tree.

    Returns a mapping of ``id(asset)`` -> unique stem so callers can look
    up each asset's resolved name without mutating the assets themselves.
    """
    import hashlib

    def _project_relative(p: Path) -> str:
        # Anchor at the Unity ``Assets`` segment so paths are stable
        # across checkout roots. Fall back to the last three segments
        # when no ``Assets`` is present (synthetic inputs in tests) —
        # still enough to disambiguate same-named assets in distinct
        # parent folders without leaking the developer's home directory.
        parts = p.parts
        for i, segment in enumerate(parts):
            if segment == "Assets":
                return "/".join(parts[i:])
        return "/".join(parts[-3:])

    by_name: dict[str, list[ConvertedAsset]] = {}
    for asset in assets:
        by_name.setdefault(asset.asset_name, []).append(asset)

    resolved: dict[int, str] = {}
    for name, group in by_name.items():
        if len(group) == 1:
            resolved[id(group[0])] = name
            continue
        for asset in group:
            digest = hashlib.sha256(
                _project_relative(asset.source_path).encode("utf-8")
            ).hexdigest()[:6]
            resolved[id(asset)] = f"{name}__{digest}"
    return resolved
