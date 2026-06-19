"""Phase-1 unit tests — camera-mount equip fact producer + equip lowering.

Covers acceptance criteria 1, 2, 2b, 3, 4, 7, 8, 9 (design-phase1.md §3):
  1  FIRE — the rig fact carries the equip obligation (Instantiate(prefab) +
     SetParent(rig-slot) in one C# method).
  2  FIRE — the lowering rewrites the VERBATIM fixture ``Player:GetRifle`` body to
     the server request, keeping the outer ``if self.riflePrefab`` wrapper + the
     ``gotWeapon``/``TakeAmmo`` tail, parsing clean.
  2b FAIL CLOSED — a surviving captured-local read outside the excised region
     (``self.currentRifle = rifle``) -> present=False, dangling_capvar, edit nothing.
  3  ABSTAIN — a camera-child slot with no Instantiate+SetParent -> no obligation,
     no edit, output byte-identical.
  4  ABSTAIN/FIRE parametrized over both receiver shapes (seeded one-hop + direct).
  7  IDEMPOTENCY — two successive lowerings leave the source byte-identical.
  8  PREFAB UNRESOLVABLE -> ABSTAIN (Instantiate(new …) has no field arg).
  9  MULTI fail-closed — >1 instantiatePrefab(<prefab>) site in equip_method.

The lowering tests drive S1's REAL lowering on the on-corpus / verbatim-fixture
shapes so the verifier's independent scan is tested against what the lowering
ACTUALLY emits, not a synthetic guess.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from converter.camera_mount_equip_lowering import (  # noqa: E402
    EQUIP_REMOTE_NAME,
    EQUIP_REMOTE_SERVICE,
    lower_camera_mount_equip,
)
from converter.child_ref_resolver import (  # noqa: E402
    ChildRefScript,
    RigRootedRetargetFact,
    build_child_ref_map,
)
from converter.rifle_rig_retarget_lowering import _luau_syntax_ok  # noqa: E402
from core.unity_types import (  # noqa: E402
    GuidEntry,
    GuidIndex,
    PrefabComponent,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
)
from unity.script_analyzer import ScriptInfo  # noqa: E402

_GUID = "33333333333333333333333333333333"


# --- C# fixture builders (mirror test_rifle_rig_retarget) ------------------


def _mono(guid: str) -> PrefabComponent:
    return PrefabComponent(
        component_type="MonoBehaviour",
        file_id="100",
        properties={"m_Script": {"fileID": 11500000, "guid": guid, "type": 3}},
    )


def _pnode(name: str, *, tag: str = "Untagged",
           children: list[PrefabNode] | None = None,
           comp_guid: str | None = None) -> PrefabNode:
    return PrefabNode(
        name=name, file_id=name, active=True, tag=tag,
        children=children or [],
        components=[_mono(comp_guid)] if comp_guid else [],
    )


def _fps_library(child_name: str = "WeaponSlot") -> PrefabLibrary:
    cam = _pnode("MainCamera", tag="MainCamera", children=[_pnode(child_name)])
    player = _pnode("Player", children=[cam], comp_guid=_GUID)
    template = PrefabTemplate(prefab_path=Path("/p/Player.prefab"),
                              name="Player", root=player)
    return PrefabLibrary(prefabs=[template])


def _guid_index(cs_path: Path) -> GuidIndex:
    idx = GuidIndex(project_root=cs_path.parent)
    idx.guid_to_entry[_GUID] = GuidEntry(
        guid=_GUID, asset_path=cs_path,
        relative_path=Path(cs_path.name), kind="script",
    )
    return idx


def _build(tmp_path: Path, source: str) -> ChildRefScript | None:
    cs_path = tmp_path / "Player.cs"
    cs_path.write_text(source, encoding="utf-8")
    info = ScriptInfo(path=cs_path, class_name="Player", base_class="MonoBehaviour")
    crm = build_child_ref_map(
        script_infos=[info], parsed_scenes=None,
        prefab_library=_fps_library(), guid_index=_guid_index(cs_path),
    )
    return crm.get(str(cs_path.resolve()))


# --- Luau script stub + lowering driver ------------------------------------


class _Script:
    def __init__(self, src: str, path: str = "/proj/Player.cs") -> None:
        self.luau_source = src
        self.source_path = path
        self.equip_binding: dict[str, object] | None = None


def _equip_map(
    *, field: str = "weaponSlot", child: str = "WeaponSlot",
    equip_method: str = "GetRifle", prefab_field: str = "riflePrefab",
    path: str = "/proj/Player.cs",
) -> dict[str, ChildRefScript]:
    return {path: ChildRefScript(
        facts=(), getchild_total=1, resolved_total=1,
        rig_facts=(RigRootedRetargetFact(
            field_name=field, child_name=child, cam_receiver="cam",
            equip_method=equip_method, prefab_field=prefab_field),),
    )}


def _lower(src: str, **map_kwargs: object) -> _Script:
    s = _Script(src)
    lower_camera_mount_equip([s], _equip_map(**map_kwargs))  # type: ignore[arg-type]
    return s


# The VERBATIM fixture-shape GetRifle body (fixture.json scripts[13]).
_VERBATIM_GETRIFLE = """local Player = {}
Player.__index = Player

function Player:GetRifle()
    self:_playOneShot(self.pickAmmoSound)
    if self.riflePrefab then
        local slot = self:_resolveWeaponSlot() or self.gameObject
        local rifle = self.host.instantiatePrefab(self.riflePrefab, slot, slot:GetPivot())
        if rifle then
            if rifle:IsA("Model") then rifle:ScaleTo(0.2) end
            rifle:PivotTo(slot:GetPivot())
        end
    end
    self.gotWeapon = true
    self:TakeAmmo(20)
end

return Player
"""


# === Criterion 1 — FIRE: the rig fact carries the equip obligation =========


def test_c1_fact_carries_equip_obligation(tmp_path: Path) -> None:
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    var r = Instantiate(riflePrefab);\n"
        "    r.transform.SetParent(weaponSlot);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None
    assert len(entry.rig_facts) == 1
    fact = entry.rig_facts[0]
    assert fact.field_name == "weaponSlot"
    assert fact.equip_method == "GetRifle"
    assert fact.prefab_field == "riflePrefab"


# === Criterion 4 — FIRE on BOTH receiver shapes (seeded one-hop + direct) ===


@pytest.mark.parametrize(
    "awake_body",
    [
        # seeded one-hop: cam = Camera.main.transform; weaponSlot = cam.GetChild(0)
        "cam = Camera.main.transform; weaponSlot = cam.GetChild(0);",
        # direct form: weaponSlot = Camera.main.transform.GetChild(0)
        "weaponSlot = Camera.main.transform.GetChild(0);",
    ],
)
def test_c4_equip_obligation_both_receiver_shapes(
    tmp_path: Path, awake_body: str
) -> None:
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        f"  void Awake() {{ {awake_body} }}\n"
        "  void GetRifle() {\n"
        "    var r = Instantiate(riflePrefab);\n"
        "    r.transform.SetParent(weaponSlot);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    fact = entry.rig_facts[0]
    assert (fact.equip_method, fact.prefab_field) == ("GetRifle", "riflePrefab")


# === Criterion 3 — ABSTAIN: camera-child slot with no equip =================


def test_c3_camera_child_no_equip_abstains(tmp_path: Path) -> None:
    # weaponSlot is read for a cosmetic, NO Instantiate+SetParent.
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void Decorate() { var x = weaponSlot.position; }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    fact = entry.rig_facts[0]
    assert fact.equip_method == "" and fact.prefab_field == ""
    # And the lowering makes NO edit / stamps NO carrier when the obligation is empty.
    no_obl_map = {"/proj/Player.cs": ChildRefScript(
        facts=(), getchild_total=1, resolved_total=1, rig_facts=(fact,))}
    s = _Script(_VERBATIM_GETRIFLE)
    n = lower_camera_mount_equip([s], no_obl_map)
    assert n == 0
    assert s.equip_binding is None
    assert s.luau_source == _VERBATIM_GETRIFLE  # byte-identical


# === Criterion 8 — PREFAB UNRESOLVABLE -> ABSTAIN ==========================


def test_c8_unresolvable_prefab_abstains(tmp_path: Path) -> None:
    # Instantiate of a NEW object (no field) -> not the held-prefab shape (D11).
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    var r = Instantiate(new GameObject());\n"
        "    r.transform.SetParent(weaponSlot);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    fact = entry.rig_facts[0]
    assert fact.equip_method == "" and fact.prefab_field == ""


def test_c8_setparent_onto_different_slot_abstains(tmp_path: Path) -> None:
    # The SetParent targets a NON-rig field -> not this obligation.
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public Transform other;\n"
        "  public GameObject riflePrefab;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    var r = Instantiate(riflePrefab);\n"
        "    r.transform.SetParent(other);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    assert entry.rig_facts[0].equip_method == ""


# === Round-2 P1-2: the PARENTED object must be the INSTANTIATED one =========


def test_p1_2_setparent_of_different_object_abstains(tmp_path: Path) -> None:
    # ``fx = Instantiate(riflePrefab)`` but ``existingWeapon.SetParent(weaponSlot)``
    # parents a DIFFERENT object. Pre-fix this credited (GetRifle, riflePrefab); the
    # fix requires the SetParent receiver to be the Instantiate result -> ABSTAIN.
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        "  GameObject existingWeapon;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    var fx = Instantiate(riflePrefab);\n"
        "    existingWeapon.transform.SetParent(weaponSlot);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    assert entry.rig_facts[0].equip_method == ""
    assert entry.rig_facts[0].prefab_field == ""


def test_p1_2_chained_instantiate_setparent_fires(tmp_path: Path) -> None:
    # The directly-chained form: Instantiate(prefab).transform.SetParent(slot) —
    # the parented object IS the result by construction. Must FIRE.
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    Instantiate(riflePrefab).transform.SetParent(weaponSlot);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    assert (entry.rig_facts[0].equip_method,
            entry.rig_facts[0].prefab_field) == ("GetRifle", "riflePrefab")


def test_p1_2_bound_symbol_setparent_via_transform_fires(tmp_path: Path) -> None:
    # The real GetRifle shape: var r = Instantiate(riflePrefab);
    # r.transform.SetParent(weaponSlot) — the receiver base symbol (r) IS the bound
    # Instantiate result. Must FIRE. (This is also c1's shape; pinned explicitly.)
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    var r = Instantiate(riflePrefab);\n"
        "    r.transform.SetParent(weaponSlot);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    assert (entry.rig_facts[0].equip_method,
            entry.rig_facts[0].prefab_field) == ("GetRifle", "riflePrefab")


def test_p1_2_bound_symbol_direct_setparent_fires(tmp_path: Path) -> None:
    # var r = Instantiate(riflePrefab); r.SetParent(weaponSlot) (no .transform).
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    var r = Instantiate(riflePrefab);\n"
        "    r.SetParent(weaponSlot);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    assert (entry.rig_facts[0].equip_method,
            entry.rig_facts[0].prefab_field) == ("GetRifle", "riflePrefab")


def test_p1_2_unbound_instantiate_then_setparent_other_var_abstains(
    tmp_path: Path,
) -> None:
    # No binding at all: Instantiate(riflePrefab) (result discarded) on one line,
    # r.transform.SetParent(weaponSlot) where r was never the Instantiate result.
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        "  Transform r;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    Instantiate(riflePrefab);\n"
        "    r.SetParent(weaponSlot);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    # The Instantiate result is never bound to ``r`` -> parented object unproven.
    assert entry.rig_facts[0].equip_method == ""


def test_c8_instantiate_setparent_in_different_methods_abstains(
    tmp_path: Path,
) -> None:
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        "  GameObject r;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void Spawn() { r = Instantiate(riflePrefab); }\n"
        "  void Attach() { r.transform.SetParent(weaponSlot); }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    # Instantiate is in Spawn, SetParent in Attach -> cannot bind one obligation.
    assert entry.rig_facts[0].equip_method == ""


# === Criterion 2 — FIRE: lowering on the VERBATIM fixture body ==============


def test_c2_lowering_on_verbatim_fixture_body() -> None:
    s = _lower(_VERBATIM_GETRIFLE)
    src = s.luau_source
    # (a) parses
    assert _luau_syntax_ok(src) is True
    # (b) request + own-emit marker WITHIN GetRifle
    assert 'FireServer("riflePrefab")' in src
    assert "_EQUIP_REQUEST_riflePrefab" in src
    assert f"self._services.{EQUIP_REMOTE_SERVICE}" in src
    # (c) no surviving instantiatePrefab(self.riflePrefab) / no rifle read / guard
    assert "instantiatePrefab(self.riflePrefab" not in src
    assert "if rifle then" not in src
    assert "rifle:PivotTo" not in src
    # (d) outer wrapper + tail survive intact and balanced
    assert "if self.riflePrefab then" in src
    assert "self.gotWeapon = true" in src
    assert "self:TakeAmmo(20)" in src
    # (e) the carrier
    assert s.equip_binding == {
        "prefab": "riflePrefab", "method": "GetRifle",
        "remote": "equipWeaponRemote", "present": True,
    }


def test_c2_remote_name_constant() -> None:
    # The Phase-2 declaration target name is the shared constant.
    assert EQUIP_REMOTE_NAME == "EquipWeapon"
    assert EQUIP_REMOTE_SERVICE == "equipWeaponRemote"


def test_c2_trivially_safe_no_following_guard() -> None:
    # A bare ``local rifle = instantiate…`` with NO further use -> replace just the
    # assignment (no guard to span); discharges.
    src = (
        "function Player:GetRifle()\n"
        "    local rifle = self.host.instantiatePrefab(self.riflePrefab, slot)\n"
        "    self.gotWeapon = true\n"
        "end\n"
        "return Player\n"
    )
    s = _lower(src)
    assert s.equip_binding is not None and s.equip_binding["present"] is True
    assert _luau_syntax_ok(s.luau_source)
    assert 'FireServer("riflePrefab")' in s.luau_source
    assert "self.gotWeapon = true" in s.luau_source


# === Criterion 2b — FAIL CLOSED: surviving captured-local read ==============


def test_c2b_dangling_capvar_fails_closed() -> None:
    # ``rifle`` is used AFTER the guard (self.currentRifle = rifle) -> removing the
    # binding would leave a nil-global read -> fail closed, edit nothing.
    src = (
        "function Player:GetRifle()\n"
        "    if self.riflePrefab then\n"
        "        local rifle = self.host.instantiatePrefab(self.riflePrefab, slot)\n"
        "        if rifle then rifle:PivotTo(slot:GetPivot()) end\n"
        "    end\n"
        "    self.currentRifle = rifle\n"
        "    self.gotWeapon = true\n"
        "end\n"
        "return Player\n"
    )
    s = _lower(src)
    assert s.luau_source == src  # edit NOTHING
    assert s.equip_binding is not None
    assert s.equip_binding["present"] is False
    assert s.equip_binding.get("dangling_capvar") is True


# === P1-A — guard-span only the TRIVIAL ``if <capvar> then`` weld guard ======


def test_p1a_compound_guard_not_spanned_fails_closed() -> None:
    # A following guard with REAL logic ``if rifle and self.shouldTrack then
    # self.currentRifle = rifle end`` must NOT be spanned wholesale (it would drop
    # the ``self.currentRifle = rifle`` logic). The compound condition is left intact
    # -> a ``rifle`` read survives OUTSIDE the excised region -> dangling_capvar
    # fail-closed, edit NOTHING. Pre-fix: the span consumed any ``if rifle …`` guard,
    # silently deleting the compound guard's body.
    src = (
        "function Player:GetRifle()\n"
        "    local rifle = self.host.instantiatePrefab(self.riflePrefab, slot)\n"
        "    if rifle and self.shouldTrack then self.currentRifle = rifle end\n"
        "    self.gotWeapon = true\n"
        "end\n"
        "return Player\n"
    )
    s = _lower(src)
    assert s.luau_source == src  # edit NOTHING
    assert s.equip_binding is not None
    assert s.equip_binding["present"] is False
    assert s.equip_binding.get("dangling_capvar") is True
    # The compound guard's real logic survives untouched.
    assert "self.currentRifle = rifle" in s.luau_source


def test_p1a_trivial_guard_still_spans_and_discharges() -> None:
    # The TRIVIAL obsolete weld guard ``if rifle then rifle:ScaleTo(..);
    # rifle:PivotTo(..) end`` (condition EXACTLY ``rifle``, only obsolete client-side
    # placement inside) is still spanned + discharged.
    src = (
        "function Player:GetRifle()\n"
        "    local rifle = self.host.instantiatePrefab(self.riflePrefab, slot)\n"
        "    if rifle then rifle:ScaleTo(0.2); rifle:PivotTo(slot:GetPivot()) end\n"
        "    self.gotWeapon = true\n"
        "end\n"
        "return Player\n"
    )
    s = _lower(src)
    assert _luau_syntax_ok(s.luau_source)
    assert s.equip_binding is not None and s.equip_binding["present"] is True
    assert 'FireServer("riflePrefab")' in s.luau_source
    assert "if rifle then" not in s.luau_source  # the trivial guard was excised
    assert "rifle:ScaleTo" not in s.luau_source
    assert "self.gotWeapon = true" in s.luau_source


# === P1-B — overloaded C# equip method name -> ABSTAIN (D8) ==================


def test_p1b_overloaded_equip_method_abstains(tmp_path: Path) -> None:
    # Two ``GetRifle(...)`` overloads share the equip name; one carries the equip
    # shape. The obligation is keyed by bare method NAME, so the two collapse -> the
    # recognizer must ABSTAIN (no obligation) rather than bind one arbitrary site.
    # Pre-fix: the resolver records (GetRifle, riflePrefab).
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "  void GetRifle() {\n"
        "    var r = Instantiate(riflePrefab);\n"
        "    r.transform.SetParent(weaponSlot);\n"
        "  }\n"
        "  void GetRifle(int ammo) {\n"
        "    Debug.Log(ammo);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src)
    assert entry is not None and len(entry.rig_facts) == 1
    fact = entry.rig_facts[0]
    assert fact.equip_method == "" and fact.prefab_field == ""


# === Criterion 9 — MULTI fail-closed ========================================


def test_c9_multi_site_fails_closed() -> None:
    # Two instantiatePrefab(self.riflePrefab) assignments in GetRifle.
    src = (
        "function Player:GetRifle()\n"
        "    local a = self.host.instantiatePrefab(self.riflePrefab, slot)\n"
        "    local b = self.host.instantiatePrefab(self.riflePrefab, slot)\n"
        "    self.gotWeapon = true\n"
        "end\n"
        "return Player\n"
    )
    s = _lower(src)
    assert s.luau_source == src  # edit NOTHING
    assert s.equip_binding is not None
    assert s.equip_binding["present"] is False
    assert s.equip_binding.get("multi_site") is True


def test_c9_multi_obligation_fails_closed() -> None:
    # D8 abstain-on-ambiguity: a single script with TWO distinct equip
    # obligations (two camera-mounted weapon slots, two prefab fields) cannot
    # be disambiguated to one request. The lowering must edit NOTHING and stamp
    # present=False + multi_obligation=True. FAILS against the pre-fix
    # ``fact = obligations[0]`` behavior (which lowered the first slot silently).
    src = (
        "function Player:GetRifle()\n"
        "    local a = self.host.instantiatePrefab(self.riflePrefab, slot)\n"
        "    self.gotRifle = true\n"
        "end\n"
        "function Player:GetPistol()\n"
        "    local b = self.host.instantiatePrefab(self.pistolPrefab, slot2)\n"
        "    self.gotPistol = true\n"
        "end\n"
        "return Player\n"
    )
    two_obligation_map = {"/proj/Player.cs": ChildRefScript(
        facts=(), getchild_total=2, resolved_total=2,
        rig_facts=(
            RigRootedRetargetFact(
                field_name="weaponSlot", child_name="WeaponSlot",
                cam_receiver="cam", equip_method="GetRifle",
                prefab_field="riflePrefab"),
            RigRootedRetargetFact(
                field_name="weaponSlot2", child_name="WeaponSlot2",
                cam_receiver="cam", equip_method="GetPistol",
                prefab_field="pistolPrefab"),
        ),
    )}
    s = _Script(src)
    n = lower_camera_mount_equip([s], two_obligation_map)  # type: ignore[arg-type]
    assert n == 0
    assert s.luau_source == src  # edit NOTHING (no request spliced)
    assert "FireServer" not in s.luau_source
    assert s.equip_binding is not None
    assert s.equip_binding["present"] is False
    assert s.equip_binding.get("multi_obligation") is True


# === Criterion 7 — IDEMPOTENCY ==============================================


def test_c7_idempotent_twice_call() -> None:
    s = _lower(_VERBATIM_GETRIFLE)
    after_first = s.luau_source
    assert s.equip_binding is not None and s.equip_binding["present"] is True
    # Second call: no-op (own-emit marker recognized), source byte-identical,
    # present stays True.
    n2 = lower_camera_mount_equip([s], _equip_map())
    assert n2 == 0
    assert s.luau_source == after_first
    assert s.equip_binding is not None and s.equip_binding["present"] is True


# === The OTHER instantiatePrefab calls in the real fixture are untouched =====


def test_feedback_prefab_calls_untouched() -> None:
    # A method with a DIFFERENT prefab field instantiate must not be rewritten —
    # the lowering scopes to equip_method + keys on prefab_field.
    src = (
        "function Player:Shoot()\n"
        "    local gunFlare = self.host.instantiatePrefab(self.feedbackPrefab, origin, cf)\n"
        "end\n"
        "function Player:GetRifle()\n"
        "    local rifle = self.host.instantiatePrefab(self.riflePrefab, slot)\n"
        "    self.gotWeapon = true\n"
        "end\n"
        "return Player\n"
    )
    s = _lower(src)
    assert "instantiatePrefab(self.feedbackPrefab" in s.luau_source  # untouched
    assert 'FireServer("riflePrefab")' in s.luau_source
    assert "instantiatePrefab(self.riflePrefab" not in s.luau_source


# === The committed fixture's Player source has the shape the lowering targets ==


def test_committed_fixture_player_carries_lowered_equip() -> None:
    """After this slice's regen, fixture.json scripts[13] (Player) carries the
    lowered equip request + equip_binding present=True (criterion 10's producer
    half — the corpus replay test asserts the verifier goes green)."""
    fixture = (
        Path(__file__).parent
        / "fixtures" / "contract_corpus" / "SimpleFPS" / "fixture.json"
    )
    if not fixture.exists():
        pytest.skip("SimpleFPS corpus fixture not committed")
    data = json.loads(fixture.read_text(encoding="utf-8"))
    player = next((s for s in data["scripts"] if s.get("name") == "Player"), None)
    assert player is not None, "Player script missing from SimpleFPS fixture"
    eb = player.get("equip_binding")
    assert eb is not None, "Player must carry the equip_binding after regen"
    assert eb["present"] is True
    assert eb["prefab"] == "riflePrefab"
    assert eb["method"] == "GetRifle"
    assert 'FireServer("riflePrefab")' in player["source"]
    assert "instantiatePrefab(self.riflePrefab" not in player["source"]


def test_committed_fixture_player_is_real_lowering_fixed_point() -> None:
    """PROVABILITY (round-2 P1-3b): the committed fixture's Player equip block is a
    genuine FIXED POINT of the REAL production lowering, not a hand-fabrication that
    merely looks lowered. Drive the actual ``lower_camera_mount_equip`` (with the
    obligation the REAL Player.cs yields) over the committed source: it must be a
    no-op (already discharged, own-emit marker recognized) AND re-stamp
    ``present=True`` with the exact carrier — i.e. running production code over the
    fixture reproduces the fixture. A hand-edit that diverged from the lowering's
    output (different marker, alias name, or guard shape) would FAIL this idempotency
    check."""
    fixture = (
        Path(__file__).parent
        / "fixtures" / "contract_corpus" / "SimpleFPS" / "fixture.json"
    )
    if not fixture.exists():
        pytest.skip("SimpleFPS corpus fixture not committed")
    data = json.loads(fixture.read_text(encoding="utf-8"))
    player = next((s for s in data["scripts"] if s.get("name") == "Player"), None)
    assert player is not None
    committed_eb = player["equip_binding"]
    s = _Script(player["source"])
    n = lower_camera_mount_equip(
        [s],
        _equip_map(
            field="weaponSlot", child="WeaponSlot",
            equip_method=committed_eb["method"],
            prefab_field=committed_eb["prefab"],
            path="/proj/Player.cs",
        ),
    )
    # No-op: the committed source already carries the lowering's own output.
    assert n == 0
    assert s.luau_source == player["source"], (
        "the real lowering changed the committed fixture's Player source — the "
        "committed source is NOT the lowering's output (hand-fabricated?). "
        "Re-run the equip-fixture regen."
    )
    assert s.equip_binding is not None
    assert s.equip_binding["present"] is True
    assert s.equip_binding["prefab"] == committed_eb["prefab"]
    assert s.equip_binding["method"] == committed_eb["method"]
    assert s.equip_binding["remote"] == committed_eb["remote"]
