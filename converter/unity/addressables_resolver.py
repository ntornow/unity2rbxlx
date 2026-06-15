"""Parse a Unity project's Addressables configuration (the editor-time
``AddressableAssetsData/AssetGroups/*.asset`` files) into address/label → asset
maps, and resolve those to the scene-runtime prefab ids the host instantiates by.

Why this exists: a heavily-Addressables game (e.g. Trash Dash) loads almost all
runtime content by *string address* (``Addressables.InstantiateAsync("Trash Cat")``
→ ``host.instantiatePrefab("Trash Cat")``) or *label*
(``LoadAssetsAsync<GameObject>("characters")``). The prefab subplans already exist
in ``scene_runtime.prefabs`` keyed ``"<guid>:<path>"``, but nothing bridges an
address to that key. This module builds that bridge from the deterministic
source-time Addressables groups — no game-specific values.

Two stages, separated so parsing stays pure/testable:
  - ``parse_addressables(project)`` → raw ``AddressablesIndex`` (guid-keyed).
  - ``resolve_prefab_addressables(index, guid_index)`` → ``PrefabAddressables``
    (address/label → prefab_id), type-filtered to ``.prefab`` (GameObject) assets.

Addresses are NOT unique (the same address string can repeat across groups, and a
group's default address is the asset path), so every map value is a LIST.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from unity.prefab_id import canonical_prefab_id

logger = logging.getLogger(__name__)

# Unity YAML preamble + per-document tag lines (same shapes as the .asset/SO path).
_UNITY_YAML_HEADER = re.compile(r"^%YAML.*\n%TAG.*\n", re.MULTILINE)
_UNITY_DOC_SEPARATOR = re.compile(r"^--- !u!\d+ &\d+.*$", re.MULTILINE)


@dataclass
class AddressableEntry:
    """One Addressables group entry: an asset made addressable by ``address``,
    optionally tagged with ``labels``."""
    guid: str
    address: str
    labels: list[str] = field(default_factory=list)


@dataclass
class AddressablesIndex:
    """Raw, guid-keyed view of every Addressables group entry in the project."""
    entries: list[AddressableEntry] = field(default_factory=list)
    by_address: dict[str, list[str]] = field(default_factory=dict)  # address -> [guid]
    by_label: dict[str, list[str]] = field(default_factory=dict)    # label -> [guid]
    by_guid: dict[str, str] = field(default_factory=dict)           # guid -> first address
    parse_errors: list[str] = field(default_factory=list)


@dataclass
class PrefabAddressables:
    """Address/label maps narrowed to instantiable prefab ids
    (``"<guid>:<relative_path>"``), the key ``scene_runtime.prefabs`` uses."""
    by_address: dict[str, list[str]] = field(default_factory=dict)  # address -> [prefab_id]
    by_label: dict[str, list[str]] = field(default_factory=dict)    # label -> [prefab_id]
    prefab_ids: set[str] = field(default_factory=set)               # all addressable prefab ids
    skipped_non_prefab: int = 0                                     # addresses pointing at non-prefab assets


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _extract_entries(mono: dict[str, object]) -> list[AddressableEntry]:
    """Pull ``m_SerializeEntries`` out of one AssetGroup MonoBehaviour body."""
    raw_entries = mono.get("m_SerializeEntries")
    if not isinstance(raw_entries, list):
        return []
    out: list[AddressableEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        guid = _as_str(raw.get("m_GUID"))
        address = _as_str(raw.get("m_Address"))
        if guid is None or address is None:
            continue
        labels_raw = raw.get("m_SerializedLabels")
        labels = [s for s in labels_raw if isinstance(s, str)] if isinstance(labels_raw, list) else []
        out.append(AddressableEntry(guid=guid, address=address, labels=labels))
    return out


def parse_addressables(unity_project_path: str | Path) -> AddressablesIndex:
    """Parse every ``AssetGroups/*.asset`` group file into an ``AddressablesIndex``.

    Robust to non-group ``.asset`` files in the directory (settings, templates):
    a file with no ``m_SerializeEntries`` MonoBehaviour contributes nothing.
    """
    index = AddressablesIndex()
    groups_dir = Path(unity_project_path) / "Assets" / "AddressableAssetsData" / "AssetGroups"
    if not groups_dir.is_dir():
        return index

    for asset in sorted(groups_dir.glob("*.asset")):
        try:
            raw = asset.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            index.parse_errors.append(f"{asset.name}: {exc}")
            continue
        cleaned = _UNITY_YAML_HEADER.sub("", raw, count=1)
        cleaned = _UNITY_DOC_SEPARATOR.sub("---", cleaned)
        try:
            docs = list(yaml.safe_load_all(cleaned))
        except yaml.YAMLError as exc:
            index.parse_errors.append(f"{asset.name}: {exc}")
            continue
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            mono = doc.get("MonoBehaviour")
            if not isinstance(mono, dict):
                continue
            index.entries.extend(_extract_entries(mono))

    for e in index.entries:
        index.by_address.setdefault(e.address, []).append(e.guid)
        index.by_guid.setdefault(e.guid, e.address)
        for label in e.labels:
            index.by_label.setdefault(label, []).append(e.guid)
    return index


def resolve_prefab_addressables(
    index: AddressablesIndex,
    guid_index: object,
) -> PrefabAddressables:
    """Narrow the raw index to instantiable prefab ids.

    ``guid_index`` is a ``unity.guid_resolver.GuidIndex`` (duck-typed: needs
    ``guid_to_entry`` mapping guid → an entry with ``.asset_path`` /
    ``.relative_path``). Only guids whose asset is a ``.prefab`` become
    instantiable prefab ids ``"<guid>:<relative_path>"`` — addresses pointing at
    sprites/audio/scenes/SOs are counted in ``skipped_non_prefab`` and dropped
    from the instantiate maps.
    """
    out = PrefabAddressables()
    guid_to_entry = getattr(guid_index, "guid_to_entry", {})
    project_root = getattr(guid_index, "project_root", None)

    def prefab_id_for(guid: str) -> str | None:
        entry = guid_to_entry.get(guid)
        path = getattr(entry, "asset_path", None) if entry is not None else None
        if path is None or path.suffix != ".prefab":
            return None
        # Shared canonical-id core so the addressable prefab_id is byte-
        # identical with the planner / scene_converter ``_prefab_stable_id``
        # join key, including the outside-root / no-project-root fallbacks.
        pid = canonical_prefab_id(guid, path, project_root)
        return pid if pid else None

    for address, guids in index.by_address.items():
        ids = [pid for pid in (prefab_id_for(g) for g in guids) if pid is not None]
        out.skipped_non_prefab += len(guids) - len(ids)
        if ids:
            out.by_address[address] = ids
            out.prefab_ids.update(ids)
    for label, guids in index.by_label.items():
        ids = [pid for pid in (prefab_id_for(g) for g in guids) if pid is not None]
        if ids:
            out.by_label[label] = ids
            out.prefab_ids.update(ids)
    return out
