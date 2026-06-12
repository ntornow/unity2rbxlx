"""Unit tests for ``child_ref_resolver`` — the chained transform-rooted
GetChild resolver + pre-rewrite.

Covers: the 3-hop turret chain (block-bodied getter / local-var /
expression-bodied), the {3,3} tally, the receiver-preserving rewrite, E1–E4 +
E8–E10 edge guards, key normalization (resolved/raw), single-scene fallback, and
the duplicate-named-prefab (``by_name`` collision) host walk.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.child_ref_resolver import (  # noqa: E402
    build_child_ref_map,
    prerewrite_child_index,
)
from core.unity_types import (  # noqa: E402
    GuidEntry,
    GuidIndex,
    ParsedScene,
    PrefabComponent,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
    SceneNode,
)
from unity.script_analyzer import ScriptInfo  # noqa: E402

_GUID = "11111111111111111111111111111111"


# --- fixture builders ------------------------------------------------------


def _mono(guid: str) -> PrefabComponent:
    return PrefabComponent(
        component_type="MonoBehaviour",
        file_id="100",
        properties={"m_Script": {"fileID": 11500000, "guid": guid, "type": 3}},
    )


def _pnode(name: str, *, children: list[PrefabNode] | None = None,
           comp_guid: str | None = None) -> PrefabNode:
    return PrefabNode(
        name=name,
        file_id=name,
        active=True,
        children=children or [],
        components=[_mono(comp_guid)] if comp_guid else [],
    )


def _turret_hierarchy(comp_guid: str = _GUID) -> PrefabLibrary:
    """Turret -> {Base -> {Weapon -> {Origin}}, Collider}. The MonoBehaviour is
    on the Turret root."""
    origin = _pnode("Origin")
    weapon = _pnode("Weapon", children=[origin])
    base = _pnode("Base", children=[weapon])
    collider = _pnode("Collider")
    root = _pnode("Turret", children=[base, collider], comp_guid=comp_guid)
    template = PrefabTemplate(prefab_path=Path("/p/Turret.prefab"),
                              name="Turret", root=root)
    return PrefabLibrary(prefabs=[template])


def _guid_index(cs_path: Path, guid: str = _GUID) -> GuidIndex:
    idx = GuidIndex(project_root=cs_path.parent)
    idx.guid_to_entry[guid] = GuidEntry(
        guid=guid, asset_path=cs_path,
        relative_path=Path(cs_path.name), kind="script",
    )
    return idx


def _write(tmp_path: Path, name: str, source: str) -> Path:
    p = tmp_path / name
    p.write_text(source, encoding="utf-8")
    return p


# The real turret shape: block-bodied chained property getters.
_TURRET_CS_BLOCK = """\
using UnityEngine;
public class Turret : MonoBehaviour {
    private Transform tBase { get { return transform.GetChild(0); } }
    private Transform tWeapon { get { return tBase.GetChild(0); } }
    private Transform tOrigin { get { return tWeapon.GetChild(0); } }
    void Fire() { var o = tOrigin.position; }
}
"""


# --- E8: the 3-hop chain (block-bodied getters) ----------------------------


def test_chain_resolves_three_hops_block_getter(tmp_path: Path) -> None:
    cs = _write(tmp_path, "Turret.cs", _TURRET_CS_BLOCK)
    infos = [ScriptInfo(path=cs, class_name="Turret")]
    m = build_child_ref_map(
        script_infos=infos, parsed_scenes=None,
        prefab_library=_turret_hierarchy(), guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert entry.getchild_total == 3
    assert entry.resolved_total == 3
    names = {(f.receiver, f.child_name) for f in entry.facts}
    assert names == {
        ("transform", "Base"),
        ("tBase", "Weapon"),
        ("tWeapon", "Origin"),
    }


def test_chain_rewrite_preserves_receiver(tmp_path: Path) -> None:
    cs = _write(tmp_path, "Turret.cs", _TURRET_CS_BLOCK)
    infos = [ScriptInfo(path=cs, class_name="Turret")]
    m = build_child_ref_map(
        script_infos=infos, parsed_scenes=None,
        prefab_library=_turret_hierarchy(), guid_index=_guid_index(cs),
    )
    out, n = prerewrite_child_index(_TURRET_CS_BLOCK, m[str(cs.resolve())])
    assert n == 3
    assert 'transform.Find("Base")' in out
    assert 'tBase.Find("Weapon")' in out
    assert 'tWeapon.Find("Origin")' in out
    assert ".GetChild(" not in out


def test_chain_resolves_local_var_form(tmp_path: Path) -> None:
    src = """\
public class Turret : MonoBehaviour {
    void Fire() {
        Transform tBase = transform.GetChild(0);
        Transform tWeapon = tBase.GetChild(0);
        var tOrigin = tWeapon.GetChild(0);
    }
}
"""
    cs = _write(tmp_path, "Turret.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="Turret")],
        parsed_scenes=None, prefab_library=_turret_hierarchy(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (3, 3)


def test_chain_resolves_expression_bodied_getter(tmp_path: Path) -> None:
    src = """\
public class Turret : MonoBehaviour {
    Transform tBase => transform.GetChild(0);
    Transform tWeapon => tBase.GetChild(0);
    Transform tOrigin => tWeapon.GetChild(0);
}
"""
    cs = _write(tmp_path, "Turret.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="Turret")],
        parsed_scenes=None, prefab_library=_turret_hierarchy(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (3, 3)


# --- E1: sibling name collision -> abstain ---------------------------------


def test_e1_name_collision_abstains(tmp_path: Path) -> None:
    # Two children of the host share the name "Dup"; GetChild(0) lands on one.
    root = _pnode("Host", children=[_pnode("Dup"), _pnode("Dup")],
                  comp_guid=_GUID)
    lib = PrefabLibrary(prefabs=[
        PrefabTemplate(prefab_path=Path("/p/H.prefab"), name="Host", root=root)
    ])
    src = "public class H : MonoBehaviour { void F(){ var x = transform.GetChild(0); } }"
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=lib, guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


# --- E2: unnamed child -> abstain ------------------------------------------


def test_e2_unnamed_child_abstains(tmp_path: Path) -> None:
    root = _pnode("Host", children=[_pnode("")], comp_guid=_GUID)
    lib = PrefabLibrary(prefabs=[
        PrefabTemplate(prefab_path=Path("/p/H.prefab"), name="Host", root=root)
    ])
    src = "public class H : MonoBehaviour { void F(){ transform.GetChild(0); } }"
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=lib, guid_index=_guid_index(cs),
    )
    assert (m[str(cs.resolve())].resolved_total) == 0


# --- E3: index past end -> abstain -----------------------------------------


def test_e3_index_past_end_abstains(tmp_path: Path) -> None:
    root = _pnode("Host", children=[_pnode("Only")], comp_guid=_GUID)
    lib = PrefabLibrary(prefabs=[
        PrefabTemplate(prefab_path=Path("/p/H.prefab"), name="Host", root=root)
    ])
    src = "public class H : MonoBehaviour { void F(){ transform.GetChild(5); } }"
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=lib, guid_index=_guid_index(cs),
    )
    assert (m[str(cs.resolve())].resolved_total) == 0


# --- E4: absent host / None inputs -----------------------------------------


def test_e4_absent_host_no_entry(tmp_path: Path) -> None:
    src = "public class H : MonoBehaviour { void F(){ transform.GetChild(0); } }"
    cs = _write(tmp_path, "H.cs", src)
    # No prefab/scene maps to this script -> not in the map at all.
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=PrefabLibrary(),
        guid_index=_guid_index(cs),
    )
    assert str(cs.resolve()) not in m


def test_e4_all_none_inputs_empty_map(tmp_path: Path) -> None:
    src = "public class H : MonoBehaviour { void F(){ transform.GetChild(0); } }"
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=None, guid_index=None,
    )
    assert m == {}


def test_e4_none_scene_entry_is_inert(tmp_path: Path) -> None:
    # The single-scene all-parse-failed fallback threads [None]; must not crash.
    src = "public class H : MonoBehaviour { void F(){ transform.GetChild(0); } }"
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=[None],  # type: ignore[list-item]
        prefab_library=None, guid_index=_guid_index(cs),
    )
    assert m == {}


# --- E9: foreign receiver (Player cam) -> abstain {1,0} ---------------------


def test_e9_foreign_receiver_abstains(tmp_path: Path) -> None:
    # cam = Camera.main.transform — a foreign object, never transform-rooted.
    src = """\
public class Player : MonoBehaviour {
    Transform cam;
    void Start() {
        cam = Camera.main.transform;
        var slot = cam.GetChild(0);
    }
}
"""
    root = _pnode("Player", children=[_pnode("Body")], comp_guid=_GUID)
    lib = PrefabLibrary(prefabs=[
        PrefabTemplate(prefab_path=Path("/p/P.prefab"), name="Player", root=root)
    ])
    cs = _write(tmp_path, "Player.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="Player")],
        parsed_scenes=None, prefab_library=lib, guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    # The pre-rewrite leaves the foreign site verbatim.
    out, n = prerewrite_child_index(src, entry)
    assert n == 0
    assert "cam.GetChild(0)" in out


# --- E10: mixed resolved + unresolved --------------------------------------


def test_e10_mixed_script(tmp_path: Path) -> None:
    src = """\
public class Mix : MonoBehaviour {
    Transform cam;
    void Start() {
        cam = Camera.main.transform;
        var a = transform.GetChild(0);
        var b = cam.GetChild(0);
    }
}
"""
    root = _pnode("Mix", children=[_pnode("Slot")], comp_guid=_GUID)
    lib = PrefabLibrary(prefabs=[
        PrefabTemplate(prefab_path=Path("/p/M.prefab"), name="Mix", root=root)
    ])
    cs = _write(tmp_path, "Mix.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="Mix")],
        parsed_scenes=None, prefab_library=lib, guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (2, 1)
    out, n = prerewrite_child_index(src, entry)
    assert n == 1
    assert 'transform.Find("Slot")' in out
    assert "cam.GetChild(0)" in out  # the unresolved site survives


# --- single-scene fallback (scene-hosted script) ---------------------------


def test_single_scene_fallback_resolves(tmp_path: Path) -> None:
    # A scene-hosted script resolves when threaded via [parsed_scene].
    child = SceneNode(name="Muzzle", file_id="2", active=True, layer=0, tag="")
    host = SceneNode(
        name="Gun", file_id="1", active=True, layer=0, tag="",
        children=[child],
        components=[_mono(_GUID)],
    )
    scene = ParsedScene(scene_path=Path("/s/Main.unity"),
                        all_nodes={"1": host, "2": child})
    src = "public class Gun : MonoBehaviour { void F(){ var m = transform.GetChild(0); } }"
    cs = _write(tmp_path, "Gun.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="Gun")],
        parsed_scenes=[scene], prefab_library=None, guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Muzzle"


# --- key normalization (raw fallback for a non-resolvable test path) --------


def test_key_normalization_raw_and_resolved(tmp_path: Path) -> None:
    cs = _write(tmp_path, "Turret.cs", _TURRET_CS_BLOCK)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="Turret")],
        parsed_scenes=None, prefab_library=_turret_hierarchy(),
        guid_index=_guid_index(cs),
    )
    # Both the resolved key and (since the file exists) the same canonical key
    # are present; a lookup under str(cs.resolve()) hits.
    assert str(cs.resolve()) in m


# --- ambiguous host (>1 node maps) -> whole-script abstain -----------------


def test_ambiguous_host_abstains(tmp_path: Path) -> None:
    # Two distinct prefab templates host the same script -> ambiguous -> absent.
    src = "public class H : MonoBehaviour { void F(){ transform.GetChild(0); } }"
    cs = _write(tmp_path, "H.cs", src)
    t1 = PrefabTemplate(prefab_path=Path("/p/A.prefab"), name="A",
                        root=_pnode("A", children=[_pnode("X")], comp_guid=_GUID))
    t2 = PrefabTemplate(prefab_path=Path("/p/B.prefab"), name="B",
                        root=_pnode("B", children=[_pnode("Y")], comp_guid=_GUID))
    lib = PrefabLibrary(prefabs=[t1, t2])
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=lib, guid_index=_guid_index(cs),
    )
    assert str(cs.resolve()) not in m


# --- duplicate-named prefabs: the `prefabs` list keeps both hosts ----------


def test_duplicate_named_prefab_walk_uses_prefabs_list(tmp_path: Path) -> None:
    # Two templates SHARE a name "Dup". by_name would drop one; walking the
    # `prefabs` list keeps both -> the script (hosted on one) sees 2 hosts ->
    # ambiguous -> abstain (proves the walk reads `prefabs`, not `by_name`).
    src = "public class H : MonoBehaviour { void F(){ transform.GetChild(0); } }"
    cs = _write(tmp_path, "H.cs", src)
    t1 = PrefabTemplate(prefab_path=Path("/p/D1.prefab"), name="Dup",
                        root=_pnode("Dup", children=[_pnode("X")], comp_guid=_GUID))
    t2 = PrefabTemplate(prefab_path=Path("/p/D2.prefab"), name="Dup",
                        root=_pnode("Dup", children=[_pnode("Y")], comp_guid=_GUID))
    lib = PrefabLibrary(prefabs=[t1, t2], by_name={"Dup": t2})
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=lib, guid_index=_guid_index(cs),
    )
    # Both hosts found -> ambiguous -> abstain. If the walk had used by_name,
    # only one host would map and it would (wrongly) resolve.
    assert str(cs.resolve()) not in m


# --- prerewrite idempotency / no-op on a script with 0 facts ---------------


def test_prerewrite_noop_when_no_facts() -> None:
    from converter.child_ref_resolver import ChildRefScript
    src = "transform.GetChild(0)"
    out, n = prerewrite_child_index(src, ChildRefScript(facts=(), getchild_total=1,
                                                        resolved_total=0))
    assert (out, n) == (src, 0)
