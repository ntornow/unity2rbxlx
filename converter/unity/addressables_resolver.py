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
from typing import cast

import yaml

from unity.prefab_ref import GuidIndexLike, prefab_id_for_guid

logger = logging.getLogger(__name__)

# A 32-hex Unity guid as it appears in a serialized ``.asset`` (both the
# ``m_AssetGUID: <hex>`` AssetReference form and the ``{fileID, guid: <hex>,
# type: N}`` object-ref form). Extracting every guid token and resolving each
# through the shared ``.prefab`` filter (``prefab_id_for_guid``) is the
# DETERMINISTIC reachability signal — no field-name allowlist, no AI fingerprint.
_GUID_TOKEN = re.compile(r"\bguid:\s*([0-9a-fA-F]{32})\b")
_ASSET_GUID_TOKEN = re.compile(r"\bm_AssetGUID:\s*([0-9a-fA-F]{32})\b")

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


@dataclass
class ScriptableObjectAddressables:
    """Label/address → SO asset guids, the PARALLEL non-prefab surface.

    Keyed by the SAME parsed ``AddressablesIndex``; values are raw guids (NOT
    prefab ids — these are ``.asset`` SO guids resolved later via the
    ``scene_runtime.scriptable_objects`` guid→module map). Built alongside
    ``PrefabAddressables`` but gated on ``so_guids`` (positive evidence an SO
    module was emitted for the guid) rather than on the absence of a ``.prefab``
    — the latter would also retain sprites/audio/scenes that produced no module.
    """
    by_label: dict[str, list[str]] = field(default_factory=dict)    # label -> [so_guid]
    by_address: dict[str, list[str]] = field(default_factory=dict)  # address -> [so_guid]
    so_guids: set[str] = field(default_factory=set)                 # all retained SO guids


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
    gi = cast(GuidIndexLike, guid_index)

    for address, guids in index.by_address.items():
        ids = [pid for pid in (prefab_id_for_guid(g, gi) for g in guids) if pid is not None]
        out.skipped_non_prefab += len(guids) - len(ids)
        if ids:
            out.by_address[address] = ids
            out.prefab_ids.update(ids)
    for label, guids in index.by_label.items():
        ids = [pid for pid in (prefab_id_for_guid(g, gi) for g in guids) if pid is not None]
        if ids:
            out.by_label[label] = ids
            out.prefab_ids.update(ids)
    return out


def resolve_scriptable_object_addressables(
    index: AddressablesIndex,
    guid_index: object,
    so_guids: set[str],
) -> ScriptableObjectAddressables:
    """Retain addressable guids whose asset is a non-``.prefab`` SO the
    converter actually emitted a module for.

    PARALLEL to ``resolve_prefab_addressables`` — it does NOT touch the shared
    ``.prefab`` filter (``prefab_ref.prefab_id_for_guid``), so the prefab maps
    are unaffected (AC-8). Membership is gated on ``so_guids`` (the set of guids
    that produced an emitted SO ModuleScript) — positive evidence an SO module
    exists, NOT merely that the guid failed the ``.prefab`` filter (which would
    also catch sprites/audio/scenes). ``guid_index`` is accepted for symmetry
    with the prefab resolver / future asset-path use but is not required for the
    so_guids gate.
    """
    del guid_index  # symmetry with resolve_prefab_addressables; gate is so_guids
    out = ScriptableObjectAddressables()

    def _retained(guids: list[str]) -> list[str]:
        seen: set[str] = set()
        kept: list[str] = []
        for g in guids:
            if g in so_guids and g not in seen:
                seen.add(g)
                kept.append(g)
        return kept

    for address, guids in index.by_address.items():
        kept = _retained(guids)
        if kept:
            out.by_address[address] = kept
            out.so_guids.update(kept)
    for label, guids in index.by_label.items():
        kept = _retained(guids)
        if kept:
            out.by_label[label] = kept
            out.so_guids.update(kept)
    return out


def resolve_so_assetref_prefab_ids(
    so_guids: set[str],
    guid_index: GuidIndexLike,
) -> set[str]:
    """Return the prefab ids (``"<guid>:<path>"``) reachable from the
    ``AssetReference`` / object-ref fields of every EMITTED ScriptableObject
    ``.asset`` (the L0 spawn-closure surface).

    WHY this exists: a heavily-Addressables game (e.g. Trash Dash) references its
    TrackSegment/obstacle prefabs ONLY from inside a ThemeData SO's
    ``zones[].prefabList`` / ``collectiblePrefab`` / ``cloudPrefabs`` arrays —
    they appear in NO ``AddressableAssetsData/AssetGroups/*.asset`` group, so the
    address/label-sourced ``PrefabAddressables`` never reaches them and the emit
    gate (``prefab_packages._is_emitted``) never emits them as ``Templates``
    children. This walks the emitted SOs' serialized refs to recover those
    prefab ids so the caller can union them into the emit set.

    Gating + generality (D-P4-9 / edge case 1):
      * ``so_guids`` is the set of guids that produced an EMITTED SO module
        (positive evidence — the SAME gate ``resolve_scriptable_object_addressables``
        uses), NOT every SO in the project. A prefab reachable only from a
        non-emitted SO is excluded.
      * Only ``.prefab``-typed targets are returned (via the shared
        ``prefab_id_for_guid`` filter) — Sprites/Meshes/Materials/Scenes are
        dropped. Keys on the deterministic serialized guid + the guid_index
        ``.prefab`` classification, NEVER a per-game field name or AI fingerprint.

    PURE. ``guid_index`` is duck-typed (``GuidIndexLike``: ``guid_to_entry`` +
    ``project_root``). An SO guid that the index cannot resolve to a readable
    ``.asset`` file contributes nothing (fails soft, not raising).
    """
    out: set[str] = set()
    if not so_guids:
        return out
    guid_to_entry = getattr(guid_index, "guid_to_entry", {})

    for so_guid in so_guids:
        entry = guid_to_entry.get(so_guid)
        asset_path = getattr(entry, "asset_path", None) if entry is not None else None
        if asset_path is None:
            continue
        try:
            text = Path(asset_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Every guid token in the .asset — both the ``m_AssetGUID:`` (AssetReference)
        # and the ``guid:`` (object-ref) spellings. The shared ``.prefab`` filter
        # narrows to instantiable prefabs; non-prefab targets resolve to ``None``.
        for m in _ASSET_GUID_TOKEN.finditer(text):
            pid = prefab_id_for_guid(m.group(1), guid_index)
            if pid is not None:
                out.add(pid)
        for m in _GUID_TOKEN.finditer(text):
            pid = prefab_id_for_guid(m.group(1), guid_index)
            if pid is not None:
                out.add(pid)
    return out
