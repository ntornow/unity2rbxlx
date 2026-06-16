"""Tests for unity.prefab_ref — the shared guid/object-ref -> canonical
prefab-id resolution primitive.

Builds a real ``GuidIndex``/``GuidEntry`` so the prefab-id math runs through the
production ``canonical_prefab_id`` core rather than a mock."""

from __future__ import annotations

from pathlib import Path

from core.unity_types import GuidEntry, GuidIndex
from unity.prefab_id import canonical_prefab_id
from unity.prefab_ref import prefab_id_for_guid, prefab_id_for_ref

ALL_ZERO = "0" * 32


def _classify(rel: str) -> str:
    return "prefab" if rel.endswith(".prefab") else "texture"


def _guid_index(mapping: dict[str, str], project_root: Path = Path("/proj")) -> GuidIndex:
    """Build a REAL ``GuidIndex`` so resolution runs the production way."""
    index = GuidIndex(project_root=project_root)
    for guid, rel in mapping.items():
        index.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=(project_root / rel),
            relative_path=Path(rel),
            kind=_classify(rel),
        )
    return index


# --- Acceptance #1: byte-identical core --------------------------------------

def test_known_prefab_guid_resolves_to_canonical_id(tmp_path):
    rel = "Assets/Bundles/Characters/Cat/character.prefab"
    gi = _guid_index({"catguid": rel}, project_root=tmp_path)
    got = prefab_id_for_guid("catguid", gi)
    # equal to a literal expected id (posix-normalised) ...
    assert got == "catguid:Assets/Bundles/Characters/Cat/character.prefab"
    # ... and equal to what canonical_prefab_id produces directly.
    assert got == canonical_prefab_id("catguid", tmp_path / rel, tmp_path)


# --- Acceptance #3: fail-soft None for each non-resolvable case ---------------

def test_guid_not_in_index_returns_none():
    gi = _guid_index({})
    assert prefab_id_for_guid("missing", gi) is None


def test_non_prefab_asset_returns_none():
    gi = _guid_index({"spriteguid": "Assets/Sprites/SomeIcon.png"})
    assert prefab_id_for_guid("spriteguid", gi) is None


def test_canonical_empty_outside_root_returns_none(tmp_path):
    # Entry path lives OUTSIDE project_root -> canonical_prefab_id == "" -> None.
    outside_root = tmp_path / "proj"
    elsewhere = tmp_path / "elsewhere"
    gi = GuidIndex(project_root=outside_root)
    gi.guid_to_entry["catguid"] = GuidEntry(
        guid="catguid",
        asset_path=(elsewhere / "character.prefab"),
        relative_path=Path("character.prefab"),
        kind="prefab",
    )
    assert prefab_id_for_guid("catguid", gi) is None


def test_rootless_index_known_guid_yields_bare_guid():
    """A rootless ``SimpleNamespace(project_root=None, ...)`` index is accepted by
    ``GuidIndexLike``. With project_root None + a known .prefab guid,
    ``canonical_prefab_id`` returns the bare guid (truthy), so the primitive
    yields it rather than None — the None-collapse fires only on the empty-id
    path."""
    from types import SimpleNamespace

    entry = GuidEntry(
        guid="catguid",
        asset_path=Path("/anywhere/character.prefab"),
        relative_path=Path("character.prefab"),
        kind="prefab",
    )
    gi = SimpleNamespace(project_root=None, guid_to_entry={"catguid": entry})
    assert prefab_id_for_guid("catguid", gi) == "catguid"


# --- Regression: getattr fail-soft on partial duck-typed shapes -------------
# A malformed/partial guid_index (or an entry missing asset_path) must drop to
# None rather than raise. Pin that.

def test_partial_index_missing_project_root_resolves_not_crash():
    """A duck-typed index missing ``project_root`` must not raise; getattr falls
    back to None (rootless), so a known .prefab guid resolves to the bare guid."""
    from types import SimpleNamespace

    entry = GuidEntry(
        guid="catguid",
        asset_path=Path("/anywhere/character.prefab"),
        relative_path=Path("character.prefab"),
        kind="prefab",
    )
    gi = SimpleNamespace(guid_to_entry={"catguid": entry})  # no project_root attr
    assert prefab_id_for_guid("catguid", gi) == "catguid"


def test_partial_index_missing_guid_to_entry_returns_none():
    """A duck-typed index missing ``guid_to_entry`` must fail soft to None, not
    raise AttributeError (getattr falls back to an empty mapping)."""
    from types import SimpleNamespace

    gi = SimpleNamespace(project_root=Path("/proj"))  # no guid_to_entry attr
    assert prefab_id_for_guid("catguid", gi) is None


def test_entry_missing_asset_path_returns_none():
    """An entry object missing ``asset_path`` must fail soft to None (getattr ->
    None hits the ``if path is None`` branch), not raise AttributeError."""
    from types import SimpleNamespace

    bad_entry = SimpleNamespace(guid="catguid")  # no asset_path attr
    gi = SimpleNamespace(project_root=Path("/proj"), guid_to_entry={"catguid": bad_entry})
    assert prefab_id_for_guid("catguid", gi) is None


# --- Acceptance #4: {fileID}-only ref (missionPopup shape) -> None, no crash --

def test_ref_fileid_only_returns_none():
    gi = _guid_index({})
    assert prefab_id_for_ref({"fileID": 80306028}, gi) is None


# --- Acceptance #3/edge 2: all-zero guid -> None -----------------------------

def test_ref_all_zero_guid_returns_none():
    gi = _guid_index({})
    assert prefab_id_for_ref({"guid": ALL_ZERO, "fileID": 0}, gi) is None


# --- Acceptance #3/edge 7: malformed ref -------------------------------------

def test_ref_not_a_dict_returns_none():
    gi = _guid_index({})
    assert prefab_id_for_ref(None, gi) is None  # type: ignore[arg-type]
    assert prefab_id_for_ref([1, 2, 3], gi) is None  # type: ignore[arg-type]


def test_ref_non_str_guid_returns_none():
    gi = _guid_index({})
    assert prefab_id_for_ref({"guid": 12345}, gi) is None  # type: ignore[dict-item]


# --- Acceptance #5: type field tolerated -------------------------------------

def test_ref_type_field_tolerated(tmp_path):
    rel = "Assets/Bundles/Characters/Cat/character.prefab"
    gi = _guid_index({"catguid": rel}, project_root=tmp_path)
    with_type = prefab_id_for_ref({"guid": "catguid", "fileID": 0, "type": 2}, gi)
    without_type = prefab_id_for_ref({"guid": "catguid"}, gi)
    assert with_type == without_type
    assert with_type == "catguid:Assets/Bundles/Characters/Cat/character.prefab"


# --- Acceptance #6: ref front door == core -----------------------------------

def test_ref_front_door_equals_core(tmp_path):
    rel = "Assets/Bundles/Characters/Cat/character.prefab"
    gi = _guid_index({"catguid": rel}, project_root=tmp_path)
    assert prefab_id_for_ref({"guid": "catguid"}, gi) == prefab_id_for_guid("catguid", gi)


# --- Edge 6 (absent type) + general: resolvable ref ignores fileID -----------

def test_ref_fileid_ignored_resolves_to_root(tmp_path):
    rel = "Assets/Bundles/Characters/Cat/character.prefab"
    gi = _guid_index({"catguid": rel}, project_root=tmp_path)
    # A sub-asset fileID is ignored; resolves to the prefab ROOT id.
    assert prefab_id_for_ref({"guid": "catguid", "fileID": 999}, gi) == (
        "catguid:Assets/Bundles/Characters/Cat/character.prefab"
    )
