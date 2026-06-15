"""Shared, pure guid/object-ref -> canonical prefab-id resolution.

One implementation of "turn a Unity object reference (or bare guid) into the
Unit-1 canonical prefab id (`"<guid>:<relative_path>"`), or fail soft to None".
Imported by unity.addressables_resolver (address/label -> prefab id) and, in
Phase 2, by converter.scriptable_object_converter (SO object-ref fields). Lives
in unity/ so converter-side callers import it with no converter->unity cycle.

Fail-soft (returns None, never raises): missing/all-zero guid, guid not in the
index, guid resolves to a non-.prefab asset, or canonical_prefab_id yields ""
(no project_root / path outside project_root).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, TypedDict

from core.unity_types import GuidEntry
from unity.prefab_id import canonical_prefab_id


class GuidIndexLike(Protocol):
    """Structural shape the resolver/helpers need from a guid index.

    A real ``core.unity_types.GuidIndex`` satisfies this, AND so does the rootless
    ``SimpleNamespace(project_root=None, guid_to_entry=...)`` that the existing
    three-way-identity test (test_scene_runtime_planner.py) relies on — which a
    NOMINAL ``GuidIndex`` (non-optional ``Path`` project_root) cannot represent.
    """
    project_root: Path | None
    guid_to_entry: Mapping[str, GuidEntry]


class ObjectRef(TypedDict, total=False):
    """A serialized Unity object reference: {guid, fileID, type?}."""
    guid: str
    fileID: int | str
    type: int


def prefab_id_for_guid(guid: str, guid_index: GuidIndexLike) -> str | None:
    """Resolve a bare asset guid to its canonical prefab id, or None.

    Pure mechanical extraction of the former nested closure — NO all-zero/empty
    guard (that lives in prefab_id_for_ref). Returns "<guid>:<relative_path>" iff
    `guid` is in the index AND its asset is a `.prefab` AND canonical_prefab_id
    yields a non-empty id; else None. An empty/all-zero guid simply misses the
    lookup → None, byte-identical to the closure on the resolver path.
    """
    entry = guid_index.guid_to_entry.get(guid)
    if entry is None:
        return None
    path = entry.asset_path
    if path is None or path.suffix != ".prefab":
        return None
    pid = canonical_prefab_id(guid, path, guid_index.project_root)
    return pid or None


def prefab_id_for_ref(ref: ObjectRef, guid_index: GuidIndexLike) -> str | None:
    """Resolve a Unity {guid, fileID, type?} object ref to its prefab id, or None.

    Front door for SO/scene object-ref fields. Applies the all-zero/missing-guid
    guard HERE (the {guid:"0"*32} / {fileID}-only shapes that appear on the ref
    path, incl. the missionPopup shape — fail soft to None, never crash), ignores
    fileID/type for top-level prefab refs (one id per .prefab FILE; sub-asset
    fileID disambiguation is out of scope — D3 / followups), and delegates to
    prefab_id_for_guid.
    """
    if not isinstance(ref, dict):
        return None
    guid = ref.get("guid", "")
    if not isinstance(guid, str) or not guid or guid == "0" * 32:
        return None
    return prefab_id_for_guid(guid, guid_index)
