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


# --- finding 1: foreign ``X.transform.GetChild(n)`` must ABSTAIN -------------


def _single_child_host(name: str = "Host") -> PrefabLibrary:
    root = _pnode(name, children=[_pnode("Base")], comp_guid=_GUID)
    return PrefabLibrary(prefabs=[
        PrefabTemplate(prefab_path=Path(f"/p/{name}.prefab"), name=name, root=root)
    ])


def test_foreign_camera_main_transform_abstains(tmp_path: Path) -> None:
    # ``Camera.main.transform.GetChild(0)`` — the trailing ``transform`` matches
    # the site regex but is a MEMBER access on a foreign camera, NOT the host.
    src = ("public class H : MonoBehaviour { "
           "void F(){ var x = Camera.main.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    # Counted toward total, but NO fact -> abstain (not rewritten to Find).
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()
    out, n = prerewrite_child_index(src, entry)
    assert n == 0
    assert "Camera.main.transform.GetChild(0)" in out


def test_foreign_member_transform_abstains(tmp_path: Path) -> None:
    # ``foo.transform.GetChild(0)`` — member access on a foreign ``foo``.
    src = ("public class H : MonoBehaviour { "
           "void F(){ var x = foo.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_bare_transform_still_resolves(tmp_path: Path) -> None:
    # Regression guard: bare ``transform.GetChild(0)`` still resolves to Base.
    src = ("public class H : MonoBehaviour { "
           "void F(){ var x = transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


def test_this_transform_resolves(tmp_path: Path) -> None:
    # ``this.transform`` IS the host transform — must resolve, not abstain.
    src = ("public class H : MonoBehaviour { "
           "void F(){ var x = this.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


# --- round-2 finding 1: host-self alias allowlist ---------------------------


def _resolve_one(tmp_path: Path, expr: str) -> "object":
    """Build the map for a single-statement script whose body is ``var x =
    <expr>;`` against a one-child (``Base``) host, returning its ChildRefScript."""
    from converter.child_ref_resolver import ChildRefScript  # noqa: F401
    src = (f"public class H : MonoBehaviour {{ void F(){{ var x = {expr}; }} }}")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    return m[str(cs.resolve())]


def test_gameObject_transform_resolves(tmp_path: Path) -> None:
    # ``gameObject.transform`` IS the host transform -> RESOLVE {1,1}.
    entry = _resolve_one(tmp_path, "gameObject.transform.GetChild(0)")
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


def test_base_transform_resolves(tmp_path: Path) -> None:
    # ``base.transform`` IS the host transform -> RESOLVE.
    entry = _resolve_one(tmp_path, "base.transform.GetChild(0)")
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


def test_this_gameObject_transform_resolves(tmp_path: Path) -> None:
    # ``this.gameObject.transform`` IS the host transform -> RESOLVE.
    entry = _resolve_one(tmp_path, "this.gameObject.transform.GetChild(0)")
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


def test_camera_main_transform_still_abstains(tmp_path: Path) -> None:
    # Round-1 guard kept: ``Camera.main.transform`` is foreign -> abstain {1,0}.
    entry = _resolve_one(tmp_path, "Camera.main.transform.GetChild(0)")
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_enemy_transform_still_abstains(tmp_path: Path) -> None:
    # ``enemy.transform`` is a member of a foreign field -> abstain.
    entry = _resolve_one(tmp_path, "enemy.transform.GetChild(0)")
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_foreign_gameObject_member_abstains(tmp_path: Path) -> None:
    # ``enemy.gameObject.transform`` — ``gameObject`` is a member of a foreign
    # field, NOT the host self-alias -> abstain.
    entry = _resolve_one(tmp_path, "enemy.gameObject.transform.GetChild(0)")
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_host_alias_seeds_symbol_table(tmp_path: Path) -> None:
    # The host-alias rule applies to symbol-table DEFINITION matchers too: a
    # symbol defined as ``gameObject.transform.GetChild(0)`` resolves and seeds
    # the chain, so a later site on it also resolves.
    root = _pnode("Host", children=[
        _pnode("Base", children=[_pnode("Tip")]),
    ], comp_guid=_GUID)
    lib = PrefabLibrary(prefabs=[
        PrefabTemplate(prefab_path=Path("/p/Host.prefab"), name="Host", root=root)
    ])
    src = ("public class H : MonoBehaviour { void F(){ "
           "Transform tBase = gameObject.transform.GetChild(0); "
           "var tip = tBase.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=lib, guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    # Both the definition site (->Base) and the chained site (->Tip) resolve.
    assert (entry.getchild_total, entry.resolved_total) == (2, 2)
    names = {f.child_name for f in entry.facts}
    assert names == {"Base", "Tip"}


# --- round-3 finding: gameObject/transform shadowed by a local/param --------


def test_param_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # ``void TakeDamage(GameObject gameObject)`` shadows the inherited member, so
    # ``gameObject.transform`` is the PARAMETER's transform, not the host's ->
    # ABSTAIN {1,0} (the backstop then guards it).
    src = ("public class H : MonoBehaviour { "
           "void TakeDamage(GameObject gameObject){ "
           "var x = gameObject.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()
    out, n = prerewrite_child_index(src, entry)
    assert n == 0
    assert "gameObject.transform.GetChild(0)" in out


def test_local_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # ``var gameObject = enemy;`` shadows the inherited member, so the later
    # ``gameObject.transform.GetChild(0)`` is the LOCAL's transform -> ABSTAIN.
    src = ("public class H : MonoBehaviour { "
           "void F(GameObject enemy){ var gameObject = enemy; "
           "var x = gameObject.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_unshadowed_gameObject_still_resolves(tmp_path: Path) -> None:
    # No local/param named ``gameObject`` -> the inherited member alias holds ->
    # the normal MonoBehaviour ``gameObject.transform.GetChild(0)`` RESOLVES.
    src = ("public class H : MonoBehaviour { "
           "void F(){ var x = gameObject.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


def test_this_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # A shadow disables the two-token ``this.gameObject`` alias too: the
    # ``gameObject`` member it dots into is shadowed -> ABSTAIN.
    src = ("public class H : MonoBehaviour { "
           "void F(GameObject gameObject){ "
           "var x = this.gameObject.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)


def test_bare_transform_no_shadow_resolves(tmp_path: Path) -> None:
    # Regression: bare ``transform.GetChild(0)`` with NO shadow declaration still
    # resolves; ``this``/``base``/un-shadowed ``gameObject`` qualifiers too.
    for expr in (
        "transform.GetChild(0)",
        "this.transform.GetChild(0)",
        "base.transform.GetChild(0)",
        "gameObject.transform.GetChild(0)",
    ):
        entry = _resolve_one(tmp_path, expr)
        assert (entry.getchild_total, entry.resolved_total) == (1, 1), expr
        assert entry.facts[0].child_name == "Base", expr


def test_transform_local_shadow_abstains(tmp_path: Path) -> None:
    # A local/param named ``transform`` shadows the inherited Component property,
    # so bare ``transform.GetChild(0)`` is the SHADOW's transform -> ABSTAIN.
    src = ("public class H : MonoBehaviour { "
           "void F(Transform transform){ var x = transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_this_base_still_resolve_under_gameObject_shadow(tmp_path: Path) -> None:
    # ``this``/``base`` are C# keywords (unshadowable): even with a ``gameObject``
    # shadow present, ``this.transform`` / ``base.transform`` stay host-self.
    src = ("public class H : MonoBehaviour { "
           "void F(GameObject gameObject){ "
           "var a = this.transform.GetChild(0); "
           "var b = base.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    # Both this.transform and base.transform resolve to Base; gameObject sites: 0.
    assert (entry.getchild_total, entry.resolved_total) == (2, 2)
    assert {f.child_name for f in entry.facts} == {"Base"}


# --- round-4 finding: comprehensive shadow-binding contexts -----------------
# foreach / lambda / tuple-deconstruction / pattern bindings of ``gameObject`` /
# ``transform`` ALSO shadow the inherited member -> the receiver is foreign ->
# the script's GetChild sites ABSTAIN ({n,0}).


def _map_for_body(tmp_path: Path, body: str) -> "object":
    """Build the child-ref map for a single-method script whose method body is
    ``body`` against the one-child (``Base``) host; return its ChildRefScript."""
    src = f"public class H : MonoBehaviour {{ void F(){{ {body} }} }}"
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    return m[str(cs.resolve())]


def test_foreach_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # ``foreach (var gameObject in xs)`` binds a loop variable named gameObject ->
    # the inherited member is shadowed -> the site abstains.
    entry = _map_for_body(
        tmp_path,
        "foreach (var gameObject in xs) { var x = gameObject.transform.GetChild(0); }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_foreach_typed_gameObject_shadow_abstains(tmp_path: Path) -> None:
    entry = _map_for_body(
        tmp_path,
        "foreach (GameObject gameObject in xs) "
        "{ var x = gameObject.transform.GetChild(0); }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)


def test_lambda_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # ``xs.ForEach(gameObject => gameObject.transform.GetChild(0))`` — the lambda
    # parameter shadows the inherited member -> abstain.
    entry = _map_for_body(
        tmp_path,
        "xs.ForEach(gameObject => gameObject.transform.GetChild(0));",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_lambda_paren_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # Parenthesized lambda param list, both bare and typed.
    for body in (
        "xs.ForEach((gameObject) => { var x = gameObject.transform.GetChild(0); });",
        "xs.ForEach((GameObject gameObject) => "
        "{ var x = gameObject.transform.GetChild(0); });",
        "xs.Each((a, gameObject) => { var x = gameObject.transform.GetChild(0); });",
    ):
        entry = _map_for_body(tmp_path, body)
        assert (entry.getchild_total, entry.resolved_total) == (1, 0), body


def test_tuple_deconstruction_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # ``var (gameObject, i) = pair;`` deconstructs into a local named gameObject.
    entry = _map_for_body(
        tmp_path,
        "var (gameObject, i) = pair; var x = gameObject.transform.GetChild(0);",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_tuple_mixed_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # ``(var gameObject, var i) = pair;`` — mixed-declaration deconstruction.
    entry = _map_for_body(
        tmp_path,
        "(var gameObject, var i) = pair; var x = gameObject.transform.GetChild(0);",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)


def test_case_pattern_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # ``case GameObject gameObject:`` declaration pattern binds gameObject.
    entry = _map_for_body(
        tmp_path,
        "switch (o) { case GameObject gameObject: "
        "var x = gameObject.transform.GetChild(0); break; }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_is_pattern_gameObject_shadow_abstains(tmp_path: Path) -> None:
    # ``o is GameObject gameObject`` is-pattern binds gameObject.
    entry = _map_for_body(
        tmp_path,
        "if (o is GameObject gameObject) { var x = gameObject.transform.GetChild(0); }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)


def test_foreach_transform_shadow_abstains(tmp_path: Path) -> None:
    # transform analogue: ``foreach (var transform in xs)`` shadows the inherited
    # Component property -> bare ``transform.GetChild(0)`` abstains.
    entry = _map_for_body(
        tmp_path,
        "foreach (var transform in xs) { var x = transform.GetChild(0); }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_lambda_transform_shadow_abstains(tmp_path: Path) -> None:
    entry = _map_for_body(
        tmp_path,
        "xs.ForEach(transform => transform.GetChild(0));",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)


def test_tuple_transform_shadow_abstains(tmp_path: Path) -> None:
    entry = _map_for_body(
        tmp_path,
        "var (transform, i) = pair; var x = transform.GetChild(0);",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)


def test_case_transform_shadow_abstains(tmp_path: Path) -> None:
    entry = _map_for_body(
        tmp_path,
        "switch (o) { case Transform transform: "
        "var x = transform.GetChild(0); break; }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)


def test_turret_getter_chain_still_resolves_under_broadened_detector(
    tmp_path: Path,
) -> None:
    # CRITICAL regression guard: the turret's ``return transform.GetChild(0)``
    # getter chain has NO local/param named transform/gameObject — the broadened
    # binding detector must NOT false-match the ``return`` keyword before
    # ``transform`` -> all 3 hops still resolve {3,3}.
    cs = _write(tmp_path, "Turret.cs", _TURRET_CS_BLOCK)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="Turret")],
        parsed_scenes=None, prefab_library=_turret_hierarchy(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (3, 3)
    assert [f.child_name for f in entry.facts] == ["Base", "Weapon", "Origin"]


def test_unshadowed_forms_still_resolve_under_broadened_detector(
    tmp_path: Path,
) -> None:
    # Un-shadowed ``gameObject.transform.GetChild(0)`` and bare
    # ``transform.GetChild(0)`` (no foreach/lambda/tuple/case binding) RESOLVE.
    for body in (
        "var x = gameObject.transform.GetChild(0);",
        "var x = transform.GetChild(0);",
    ):
        entry = _map_for_body(tmp_path, body)
        assert (entry.getchild_total, entry.resolved_total) == (1, 1), body
        assert entry.facts[0].child_name == "Base", body


# --- round-5 finding: C# keyword in the type slot is NOT a shadow binding ---
# The typed-declaration pattern (``<word> IDENT [;)]``) must reject a C# KEYWORD
# in the type slot, so common non-shadow idioms (``return transform;``,
# ``in transform)`` foreach-collection tail) don't over-abstain the whole script.


def test_return_transform_getter_idiom_resolves(tmp_path: Path) -> None:
    # ``return transform;`` — super-common getter idiom; ``return`` is a keyword,
    # NOT the type of a ``transform`` declaration -> a real ``transform.GetChild``
    # elsewhere still RESOLVES {1,1}.
    entry = _map_for_body(
        tmp_path,
        "Transform Self(){ return transform; } void G(){ transform.GetChild(0); }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


def test_return_gameObject_getter_idiom_resolves(tmp_path: Path) -> None:
    # ``return gameObject;`` — ``return`` is a keyword -> not a gameObject binding
    # -> ``gameObject.transform.GetChild(0)`` resolves via the host-self alias.
    entry = _map_for_body(
        tmp_path,
        "GameObject Self(){ return gameObject; } "
        "void G(){ gameObject.transform.GetChild(0); }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


def test_foreach_collection_transform_resolves(tmp_path: Path) -> None:
    # ``foreach (var c in transform)`` — ``transform`` is the COLLECTION being
    # iterated (``in transform`` — ``in`` is a keyword), NOT a binding -> a later
    # ``transform.GetChild(0)`` site still RESOLVES.
    entry = _map_for_body(
        tmp_path,
        "foreach(var c in transform){} transform.GetChild(0);",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    assert entry.facts[0].child_name == "Base"


def test_foreach_binding_gameObject_still_abstains(tmp_path: Path) -> None:
    # Discriminator: ``foreach(var gameObject in xs)`` — ``gameObject`` is the
    # BINDING (loop variable), not the collection -> still ABSTAIN {1,0}.
    entry = _map_for_body(
        tmp_path,
        "foreach(var gameObject in xs){ gameObject.transform.GetChild(0); }",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_param_gameObject_typed_decl_still_abstains(tmp_path: Path) -> None:
    # ``void F(GameObject gameObject)`` — ``GameObject`` is NOT a keyword, a real
    # typed param binding -> still ABSTAIN {1,0}.
    src = ("public class H : MonoBehaviour { "
           "void F(GameObject gameObject){ gameObject.transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


def test_var_gameObject_local_still_abstains(tmp_path: Path) -> None:
    # ``var gameObject = x;`` — ``var`` is the contextual type and DOES bind ->
    # still ABSTAIN {1,0} (var deliberately not treated as a keyword).
    entry = _map_for_body(
        tmp_path,
        "var gameObject = x; gameObject.transform.GetChild(0);",
    )
    assert (entry.getchild_total, entry.resolved_total) == (1, 0)
    assert entry.facts == ()


# --- finding 5: GetChild inside a comment/string is NOT rewritten -----------


def test_line_commented_getchild_not_rewritten(tmp_path: Path) -> None:
    src = ("public class H : MonoBehaviour { void F(){ "
           "// transform.GetChild(0)\n"
           "var y = 1; } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    # The only GetChild is inside a ``//`` comment -> 0 sites -> absent from map.
    assert str(cs.resolve()) not in m


def test_block_commented_getchild_not_rewritten(tmp_path: Path) -> None:
    # A ``/* ... */`` block comment spanning the GetChild must be skipped.
    src = ("public class H : MonoBehaviour { void F(){ "
           "/* transform.GetChild(0) */\n"
           "var y = transform.GetChild(0); } }")
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    entry = m[str(cs.resolve())]
    # Only the REAL (non-comment) site counts + resolves.
    assert (entry.getchild_total, entry.resolved_total) == (1, 1)
    out, n = prerewrite_child_index(src, entry)
    assert n == 1
    # The commented occurrence is left verbatim; only the real one is rewritten.
    assert "/* transform.GetChild(0) */" in out
    assert 'transform.Find("Base")' in out


def test_verbatim_string_getchild_not_counted(tmp_path: Path) -> None:
    # A ``@"..."`` verbatim string containing the pattern is not code.
    src = ('public class H : MonoBehaviour { void F(){ '
           'var s = @"transform.GetChild(0)"; } }')
    cs = _write(tmp_path, "H.cs", src)
    m = build_child_ref_map(
        script_infos=[ScriptInfo(path=cs, class_name="H")],
        parsed_scenes=None, prefab_library=_single_child_host(),
        guid_index=_guid_index(cs),
    )
    assert str(cs.resolve()) not in m


# ===========================================================================
# Comment-trivia robustness of the C# admission/dominance guards (codex HARDEN
# BLOCKING): the round-5 ``_skip_ws_and_comments_back`` discipline applied to the
# seed-LHS guard must also cover ``_seed_dominates_use`` (a comment between a
# braceless conditional header and its body was FALSE-ADMITTING the conditional
# seed as dominating) and ``_canonical_receiver``'s exact-seed match (a comment in
# the seed RHS was FALSE-REJECTING a legit camera seed -> a dropped rig fact).
# ===========================================================================

from converter.child_ref_resolver import (  # noqa: E402
    _canonical_receiver,
    _seed_dominates_use,
)


def test_dominates_braceless_conditional_with_block_comment_not_admitted() -> None:
    # A comment between ``if (c)`` and the braceless seed must NOT defeat the
    # conditional detection (pre-fix: lands ``prev`` on the comment delimiter ->
    # falls through -> returns True -> false-admits the conditional seed).
    src = "if (c) /*note*/ cam = X;\n weaponSlot = cam.GetChild(0);"
    assert _seed_dominates_use(
        src, src.index("cam = X"), src.index("weaponSlot")
    ) is False


def test_dominates_braceless_conditional_with_line_comment_not_admitted() -> None:
    src = "if (c) // note\n cam = X;\n weaponSlot = cam.GetChild(0);"
    assert _seed_dominates_use(
        src, src.index("cam = X"), src.index("weaponSlot")
    ) is False


def test_dominates_comment_between_keyword_and_paren_not_admitted() -> None:
    # ``if /*x*/ (c) seed`` — the comment is between the keyword and its ``(``; the
    # governor-keyword read must still recover ``if``.
    src = "if /*x*/ (c) cam = X;\n weaponSlot = cam.GetChild(0);"
    assert _seed_dominates_use(
        src, src.index("cam = X"), src.index("weaponSlot")
    ) is False


def test_dominates_legit_comment_before_straightline_seed_still_dominates() -> None:
    # A comment before a STRAIGHT-LINE (non-conditional) seed must NOT be mistaken
    # for a governor -> the seed still dominates.
    src = "/*setup*/ cam = X;\n weaponSlot = cam.GetChild(0);"
    assert _seed_dominates_use(
        src, src.index("cam = X"), src.index("weaponSlot")
    ) is True


def test_canonical_receiver_comment_split_seed_still_admits() -> None:
    # ``cam = /*c*/ Camera.main.transform`` — a comment between ``=`` and the literal
    # must NOT false-REJECT the legit camera seed (pre-fix: ``\s*`` did not span the
    # comment -> None -> the rig fact was silently dropped).
    src = "cam = /*c*/ Camera.main.transform;\n w = cam.GetChild(0);"
    assert _canonical_receiver(src, "cam", src.index("GetChild")) == (
        "Camera.main.transform"
    )


def test_canonical_receiver_line_comment_split_seed_still_admits() -> None:
    src = "cam = // x\n Camera.main.transform;\n w = cam.GetChild(0);"
    assert _canonical_receiver(src, "cam", src.index("GetChild")) == (
        "Camera.main.transform"
    )


def test_canonical_receiver_foreign_seed_with_trailing_comment_still_none() -> None:
    # A foreign RHS plus a trailing comment that mentions the literal must NOT
    # false-ADMIT (the comment is not code).
    src = "cam = enemy.transform; // Camera.main.transform\n w = cam.GetChild(0);"
    assert _canonical_receiver(src, "cam", src.index("GetChild")) is None


def test_canonical_receiver_longer_chain_with_comment_still_none() -> None:
    # ``Camera.main.transform /*c*/ .parent`` is a LONGER chain (trivia-aware tail
    # check) -> NOT the exact one-hop seed.
    src = "cam = Camera.main.transform /*c*/ .parent;\n w = cam.GetChild(0);"
    assert _canonical_receiver(src, "cam", src.index("GetChild")) is None
