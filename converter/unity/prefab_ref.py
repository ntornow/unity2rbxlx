"""Shared, pure guid/object-ref -> canonical prefab-id resolution.

Turns a Unity object reference (or bare guid) into the Unit-1 canonical prefab
id (`"<guid>:<relative_path>"`), or fails soft to None. Imported by
unity.addressables_resolver and converter.scriptable_object_converter; lives in
unity/ so converter-side callers import it with no converter->unity cycle.

Returns None for: missing/all-zero guid, guid not in the index, guid resolves to
a non-.prefab asset, or canonical_prefab_id yields "" (no project_root / path
outside project_root). Reads through getattr, so a partial/malformed duck-typed
index also fails soft to None rather than raising AttributeError.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, TypedDict

from core.unity_types import GuidEntry
from unity.prefab_id import canonical_prefab_id


class GuidIndexLike(Protocol):
    """Structural shape the resolver/helpers need from a guid index.

    A real ``core.unity_types.GuidIndex`` satisfies this, as does a rootless
    ``SimpleNamespace(project_root=None, guid_to_entry=...)`` (which a nominal
    ``GuidIndex`` with a non-optional ``Path`` project_root cannot represent).
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

    Returns "<guid>:<relative_path>" iff `guid` is in the index AND its asset is
    a `.prefab` AND canonical_prefab_id yields a non-empty id; else None. No
    all-zero/empty guard here (that lives in prefab_id_for_ref) — an empty guid
    simply misses the lookup. Reads through ``getattr`` so a partial guid_index
    or an entry missing ``asset_path`` fails soft to None, not AttributeError.
    """
    guid_to_entry = getattr(guid_index, "guid_to_entry", {})
    entry = guid_to_entry.get(guid)
    path = getattr(entry, "asset_path", None) if entry is not None else None
    if path is None or path.suffix != ".prefab":
        return None
    project_root = getattr(guid_index, "project_root", None)
    pid = canonical_prefab_id(guid, path, project_root)
    return pid or None


def prefab_id_for_ref(ref: ObjectRef, guid_index: GuidIndexLike) -> str | None:
    """Resolve a Unity {guid, fileID, type?} object ref to its prefab id, or None.

    Front door for SO/scene object-ref fields. Applies the all-zero/missing-guid
    guard here ({guid:"0"*32} / {fileID}-only shapes fail soft to None), ignores
    fileID/type for top-level prefab refs (one id per .prefab file; sub-asset
    fileID disambiguation is out of scope — D3 / followups), and delegates to
    prefab_id_for_guid.
    """
    if not isinstance(ref, dict):
        return None
    guid = ref.get("guid", "")
    if not isinstance(guid, str) or not guid or guid == "0" * 32:
        return None
    return prefab_id_for_guid(guid, guid_index)
