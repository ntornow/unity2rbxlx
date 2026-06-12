"""Unit tests for the rifle rig-retarget slice S1 (resolver + lowering + carrier).

Covers acceptance (a), (a-neg), (b), (c), (h.1-h.6), (i-ii/i-iii):
- the resolver admits Camera.main -> MainCamera-tag and produces the rig fact,
  in the seeded AND direct forms, with the EXACT host-XOR-rig admission;
- the post-transpile lowering injects the per-instance real-Instance resolver
  BEFORE ``return <Class>`` (loadable), rewrites only YIELD-SAFE-method reads,
  fact-anchored-neutralizes the camera-child write, and stamps the carrier;
- the 6 robustness fixes incl. the idempotency twice-call, the desync-coupling
  test, and the yield-guard test;
- ``prerewrite_child_index`` never touches ``rig_facts``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.child_ref_resolver import (  # noqa: E402
    ChildRefScript,
    RigRootedRetargetFact,
    build_child_ref_map,
    prerewrite_child_index,
)
from converter.rifle_rig_retarget_lowering import (  # noqa: E402
    lower_rifle_rig_retarget,
)
from core.unity_types import (  # noqa: E402
    GuidEntry,
    GuidIndex,
    PrefabComponent,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
)
from unity.script_analyzer import ScriptInfo  # noqa: E402
from utils.luau_analyze import luau_analyze_path, syntax_errors_for_source  # noqa: E402

_GUID = "22222222222222222222222222222222"


# --- fixture builders ------------------------------------------------------


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
        name=name,
        file_id=name,
        active=True,
        tag=tag,
        children=children or [],
        components=[_mono(comp_guid)] if comp_guid else [],
    )


def _fps_library(*, child_name: str = "WeaponSlot",
                 cam_tag: str = "MainCamera",
                 second_child: str | None = None,
                 comp_guid: str = _GUID) -> PrefabLibrary:
    """Player(host) + a MainCamera-tagged node whose child[0] is uniquely
    ``child_name``. ``second_child`` adds a sibling (for non-unique tests)."""
    cam_children = [_pnode(child_name)]
    if second_child is not None:
        cam_children.append(_pnode(second_child))
    cam = _pnode("MainCamera", tag=cam_tag, children=cam_children)
    player = _pnode("Player", children=[cam], comp_guid=comp_guid)
    template = PrefabTemplate(prefab_path=Path("/p/Player.prefab"),
                              name="Player", root=player)
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


def _build(tmp_path: Path, source: str, library: PrefabLibrary,
           guid: str = _GUID) -> ChildRefScript | None:
    cs_path = _write(tmp_path, "Player.cs", source)
    idx = _guid_index(cs_path, guid)
    info = ScriptInfo(path=cs_path, class_name="Player", base_class="MonoBehaviour")
    crm = build_child_ref_map(
        script_infos=[info],
        parsed_scenes=None,
        prefab_library=library,
        guid_index=idx,
    )
    return crm.get(str(cs_path.resolve()))


# A synthetic AI-output Player carrying the on-corpus shapes.
_AI_PLAYER = """\
function Player.new(config)
    return setmetatable({}, Player)
end

function Player:Awake()
    self.cam = workspace.CurrentCamera
    -- weaponSlot = cam.GetChild(0)
    self.weaponSlot = self.cam and self.cam:GetChildren()[1]
end

function Player:GetRifle()
    local rifle = self.host.instantiatePrefab(self.riflePrefab, self.weaponSlot, pivotOf(self.weaponSlot))
    if self.weaponSlot then rifle:PivotTo(pivotOf(self.weaponSlot)) end
end

return Player
"""


class _Script:
    def __init__(self, src: str, path: str = "/proj/Player.cs") -> None:
        self.luau_source = src
        self.source_path = path
        self.rig_binding: dict[str, object] | None = None


def _rig_map(field: str = "weaponSlot", child: str = "WeaponSlot",
             path: str = "/proj/Player.cs") -> dict[str, ChildRefScript]:
    return {path: ChildRefScript(
        facts=(),
        getchild_total=1,
        resolved_total=1,
        rig_facts=(RigRootedRetargetFact(field_name=field, child_name=child),),
    )}


# === (a) resolver admits Camera.main -> MainCamera-tag ======================


def test_a_resolver_admits_seeded_camera_main(tmp_path: Path) -> None:
    src = (
        "using UnityEngine;\n"
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n"
        "}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.facts == ()
    assert entry.getchild_total == 1
    assert entry.resolved_total == 1
    assert entry.rig_facts == (
        RigRootedRetargetFact(field_name="weaponSlot", child_name="WeaponSlot"),
    )


def test_a_resolver_admits_direct_camera_main_no_seed(tmp_path: Path) -> None:
    # Direct form, no cam-symbol seed (round-1 BLOCKING #2 miss).
    src = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(0); }\n"
        "}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(field_name="weaponSlot", child_name="WeaponSlot"),
    )
    assert entry.resolved_total == 1


def test_a_neg_no_main_camera_tag_abstains(tmp_path: Path) -> None:
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "}\n"
    )
    entry = _build(tmp_path, src, _fps_library(cam_tag="Untagged"))
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


def test_a_neg_non_unique_main_camera_tag_abstains(tmp_path: Path) -> None:
    # Two MainCamera-tagged nodes -> non-unique -> abstain.
    cam1 = _pnode("MainCamera", tag="MainCamera", children=[_pnode("WeaponSlot")])
    cam2 = _pnode("MainCamera2", tag="MainCamera", children=[_pnode("WeaponSlot")])
    player = _pnode("Player", children=[cam1, cam2], comp_guid=_GUID)
    lib = PrefabLibrary(prefabs=[PrefabTemplate(
        prefab_path=Path("/p/Player.prefab"), name="Player", root=player)])
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
        "}\n"
    )
    entry = _build(tmp_path, src, lib)
    assert entry is not None
    assert entry.rig_facts == ()


def test_a_neg_child_sibling_collision_abstains(tmp_path: Path) -> None:
    # child[0] name collides with a sibling -> E1 -> abstain.
    entry = _build(
        tmp_path,
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(0); }\n}\n",
        _fps_library(child_name="Slot", second_child="Slot"),
    )
    assert entry is not None
    assert entry.rig_facts == ()


# === (a-neg) foreign-receiver rejection (load-bearing adversarial) ==========


def test_a_neg_foreign_enemy_cam_rejected(tmp_path: Path) -> None:
    src = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot; Enemy enemy;\n"
        "  void Awake() { weaponSlot = enemy.cam.GetChild(0); }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()


def test_a_neg_foreign_other_cam_transform_rejected(tmp_path: Path) -> None:
    src = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot; Other other;\n"
        "  void Awake() { weaponSlot = other.cam.transform.GetChild(0); }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()


def test_a_neg_member_access_lhs_rejected(tmp_path: Path) -> None:
    # ``x.weaponSlot = Camera.main.transform.GetChild(0)`` — member-access LHS.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  X x;\n"
        "  void Awake() { x.weaponSlot = Camera.main.transform.GetChild(0); }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()


def test_a_neg_longer_cam_chain_seed_rejected(tmp_path: Path) -> None:
    # ``cam = Camera.main.transform.parent`` — a longer chain, NOT the one-hop seed.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() { cam = Camera.main.transform.parent; weaponSlot = cam.GetChild(0); }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()


# === (b) the lowering rewrites consumer reads to the per-instance resolver ===


def test_b_lowering_full_discharge() -> None:
    s = _Script(_AI_PLAYER)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1
    out = s.luau_source
    # (i) the 4 reads in GetRifle become resolver calls; no bare read survives.
    assert out.count("self:_resolveWeaponSlot()") == 4
    assert "pivotOf(self.weaponSlot)" not in out
    # (ii) the resolver method is injected ONCE, BEFORE ``return Player``.
    assert out.count("function Player:_resolveWeaponSlot()") == 1
    assert out.index("function Player:_resolveWeaponSlot()") < out.index("return Player")
    assert 'm:GetAttribute("_MainCameraRig")' in out
    assert 'rig:FindFirstChild("WeaponSlot", true)' in out
    assert "for _ = 1, 30 do" in out
    assert "task.wait(0.1)" in out
    assert "self._weaponSlotCache" in out
    # (iii) NO proxy, NO module-level state.
    assert "setmetatable" not in out.split("function Player:_resolveWeaponSlot")[1]
    assert "_rigSlotPending" not in out
    assert "__index" not in out
    # (iv) the camera-child Awake write is neutralized to nil.
    assert "self.weaponSlot = nil" in out
    # (v) the original ordinal is gone.
    assert "self.cam:GetChildren()[1]" not in out
    # (vi) the carrier is stamped present=True.
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
    }


def test_b_abstain_no_matchable_read_stamps_present_false() -> None:
    # An AI shape with the write+return but NO consumer read of self.weaponSlot.
    src = (
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    # No yielding-method read -> discharge cannot be confirmed -> abstain + False.
    assert n == 0
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": False,
    }
    # Abstained -> source unedited (the un-discharged binding reaches the verifier).
    assert "self.weaponSlot = self.cam:GetChildren()[1]" in s.luau_source
    assert "_resolveWeaponSlot" not in s.luau_source


# === (c) the emitted binding is a real Instance, per-instance, loadable ======


def test_c_lowered_source_is_loadable_luau() -> None:
    if not luau_analyze_path():
        import pytest
        pytest.skip("luau-analyze not installed")
    s = _Script(_AI_PLAYER)
    lower_rifle_rig_retarget([s], _rig_map())
    assert syntax_errors_for_source(s.luau_source) == []


def test_c_resolver_returns_instance_not_table() -> None:
    s = _Script(_AI_PLAYER)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert 'rig:FindFirstChild("WeaponSlot", true)' in out  # a real Instance
    assert "setmetatable" not in out.split("_resolveWeaponSlot")[-1]


def test_c_cache_is_per_instance() -> None:
    s = _Script(_AI_PLAYER)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert "self._weaponSlotCache" in out  # per-instance memo on self
    # No module-level cache/pending local.
    assert "\nlocal _weaponSlotCache" not in out
    assert "_rigSlotCache" not in out
    assert "_rigSlotPending" not in out


# === (h) the 6 robustness fixes =============================================


def test_h1_after_return_splice_would_fail_syntax_check() -> None:
    # h.1: a module ending ``return Player`` -> resolver spliced BEFORE it (so it
    # parses). An after-``return`` splice would FAIL the syntax check -> abstain.
    if not luau_analyze_path():
        import pytest
        pytest.skip("luau-analyze not installed")
    s = _Script(_AI_PLAYER)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    # The injected method precedes the trailing return.
    assert out.rstrip().endswith("return Player")
    assert out.index("function Player:_resolveWeaponSlot()") < out.rindex("return Player")
    assert syntax_errors_for_source(out) == []


def test_h1_no_module_return_abstains() -> None:
    # No ``return <Class>`` epilogue -> nothing to splice before -> abstain.
    src = (
        "function Player:GetRifle()\n"
        "    local r = pivotOf(self.weaponSlot)\n"
        "end\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 0
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": False,
    }
    assert "_resolveWeaponSlot" not in s.luau_source


def test_h3_neutralize_fact_anchored_skips_unrelated_config() -> None:
    # h.3: an EARLIER unrelated ``self.weaponSlot = someConfig`` is UNTOUCHED;
    # only the camera-child write is neutralized.
    src = (
        "function Player.new(config)\n"
        "    self.weaponSlot = config.defaultSlot\n"
        "    return setmetatable({}, Player)\n"
        "end\n\n"
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert "self.weaponSlot = config.defaultSlot" in out  # untouched config
    assert "self.weaponSlot = self.cam:GetChildren()[1]" not in out  # neutralized
    assert "self.weaponSlot = nil" in out


def test_h3_multiline_camera_child_rhs_fully_replaced() -> None:
    src = (
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam\n"
        "        and self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert "self.cam:GetChildren()[1]" not in out
    assert "self.weaponSlot = nil" in out


def test_h3_desync_coupling_surviving_consumer_read_stamps_false() -> None:
    # h.3 DESYNC-COUPLING: a consumer read the rewrite CANNOT reach (an ALIASED
    # local, not bare ``self.weaponSlot``) leaves the read surviving via the alias
    # WHILE the camera-child write neutralizes -> the camera-child write is gone
    # (condition 2 holds) but the consumer still reads through the field name in a
    # yielding method -> discharge tracks the SCAN, never a fake green from the
    # pre-flipped resolved_total. The decisive guard: ``present`` ALWAYS equals the
    # independent source scan, so a half-applied lowering can never present green.
    src = (
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    local w = self.weaponSlot\n"  # a yielding-method read
        "    return pivotOf(w)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    from converter.rifle_rig_retarget_lowering import _binding_discharged
    assert s.rig_binding is not None
    # present MUST equal the independent scan (no stamp-only fake green).
    assert s.rig_binding["present"] == _binding_discharged(
        s.luau_source, "weaponSlot", "WeaponSlot"
    )


def test_h3_no_consumer_read_means_no_silent_green() -> None:
    # The half-applied desync the coupling closes: the camera-child write exists
    # (and would be neutralized) but there is NO consumer read in a yielding method
    # for the rewrite to land -> discharge is NOT confirmed -> present=False, and
    # the lowering ABSTAINS (leaves the source) rather than faking green off the
    # pre-flipped resolved_total. (resolved_total was flipped 0->1 pre-transpile;
    # the binding-present check, not check D, is the authority on "discharged".)
    src = (
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 0
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": False,
    }
    # Abstained: the camera-child ordinal still survives in the source so the
    # binding-present check fail-closes LOUD (and check D would still see it) —
    # never a silent green.
    assert "self.cam:GetChildren()[1]" in s.luau_source


def test_h4_member_tail_self_not_rewritten() -> None:
    # ``other.self.weaponSlot`` is a member tail -> NOT rewritten.
    src = (
        "function Player:GetRifle()\n"
        "    local x = other.self.weaponSlot\n"
        "    return self.weaponSlot\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert "other.self.weaponSlot" in out  # member tail untouched
    assert "return self:_resolveWeaponSlot()" in out  # bare read rewritten


def test_h5_yield_guard_abstains_in_awake_rewrites_in_getrifle() -> None:
    # h.5 YIELD-GUARD: a read in non-yielding Awake is LEFT; a read in yielding
    # GetRifle is rewritten.
    src = (
        "function Player:Awake()\n"
        "    local cached = self.weaponSlot\n"  # non-yielding -> abstain
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"  # yielding -> rewrite
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    # The Awake READ is left as self.weaponSlot (only the camera-child write is nil).
    awake_body = out.split("function Player:Awake()")[1].split("end")[0]
    assert "local cached = self.weaponSlot" in awake_body
    # The GetRifle read is rewritten.
    assert "return pivotOf(self:_resolveWeaponSlot())" in out


def test_h5_start_is_non_yielding() -> None:
    src = (
        "function Player:Start()\n"
        "    local cached = self.weaponSlot\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    start_body = out.split("function Player:Start()")[1].split("end")[0]
    assert "local cached = self.weaponSlot" in start_body
    assert "return pivotOf(self:_resolveWeaponSlot())" in out


def test_h6_idempotency_twice_call_byte_identical() -> None:
    s = _Script(_AI_PLAYER)
    lower_rifle_rig_retarget([s], _rig_map())
    after_first = s.luau_source
    binding_first = dict(s.rig_binding or {})
    n2 = lower_rifle_rig_retarget([s], _rig_map())
    assert n2 == 0  # nothing to do the second time
    assert s.luau_source == after_first  # byte-identical
    assert dict(s.rig_binding or {}) == binding_first  # carrier re-stamped identically


def test_h6_stray_marker_comment_does_not_suppress_injection() -> None:
    # A stray ``-- _RIG_RETARGET_WeaponSlot`` comment with NO injected method must
    # NOT suppress injection (the guard is the METHOD's presence, not the marker).
    src = (
        "-- _RIG_RETARGET_WeaponSlot (a stray comment, no method)\n"
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1
    assert s.luau_source.count("function Player:_resolveWeaponSlot()") == 1


# === (i) legacy untouched / prerewrite ignores rig_facts / strict typing =====


def test_i_prerewrite_child_index_ignores_rig_facts(tmp_path: Path) -> None:
    # A ChildRefScript with ONLY a rig fact (no host facts) -> prerewrite is a
    # no-op (it iterates entry.facts only; rig_facts is the construction-safety
    # boundary it must never touch).
    entry = ChildRefScript(
        facts=(),
        getchild_total=1,
        resolved_total=1,
        rig_facts=(RigRootedRetargetFact("weaponSlot", "WeaponSlot"),),
    )
    csharp = "weaponSlot = Camera.main.transform.GetChild(0);\n"
    out, count = prerewrite_child_index(csharp, entry)
    assert count == 0
    assert out == csharp  # untouched


def test_i_carrier_anchors_are_deterministic_projections() -> None:
    # The carrier's field/child come from the fact (deterministic upstream), not
    # an AI-output fingerprint or a hardcoded string. A DIFFERENT child name flows
    # through unchanged (generic).
    s = _Script(_AI_PLAYER.replace("WeaponSlot", "GunMount"))  # irrelevant text
    crm = _rig_map(field="weaponSlot", child="GunMount")
    lower_rifle_rig_retarget([s], crm)
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "GunMount", "present": True,
    }
    assert "function Player:_resolveGunMount()" in s.luau_source
    assert 'rig:FindFirstChild("GunMount", true)' in s.luau_source


def test_prerewrite_child_index_no_double_count_with_host_facts(tmp_path: Path) -> None:
    # resolved_total = len(facts) + len(rig_facts).
    entry = _build(
        tmp_path,
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(0); }\n}\n",
        _fps_library(),
    )
    assert entry is not None
    assert entry.resolved_total == len(entry.facts) + len(entry.rig_facts)


# === round-2 P1 fixes =======================================================


def test_p1_final_syntax_check_after_rewrites_stamps_false() -> None:
    # P1 (codex BLOCKING): the Luau syntax re-check must run AFTER ALL rewrites
    # (inject + read-rewrite + neutralize), not just after inject. Here the
    # neutralize swallows the inline ``if ... then ... end``'s closing ``end``
    # (the RHS span runs to the newline), producing UNLOADABLE Luau — yet the
    # shape-only ``_binding_discharged`` would stamp present=True. The final
    # syntax gate must catch it -> abstain/revert -> present=False.
    src = (
        "function Player:Awake()\n"
        "    if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 0  # abstained — never shipped broken Luau
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": False,
    }
    # Source reverted to the original (camera-child write intact for the verifier).
    assert "self.cam:GetChildren()[1]" in s.luau_source
    assert "_resolveWeaponSlot" not in s.luau_source
    # If luau-analyze is installed, prove the would-be FINAL source is unloadable
    # while the post-INJECT-only source parses (so only the FINAL gate catches it).
    if luau_analyze_path():
        from converter.rifle_rig_retarget_lowering import (
            _inject_resolver_method,
            _neutralize_assignment,
            _rewrite_field_reads,
        )
        ns, inj = _inject_resolver_method(src, "Player", "WeaponSlot", "weaponSlot")
        assert inj and syntax_errors_for_source(ns) == []  # post-inject parses
        ns, _ = _rewrite_field_reads(ns, "weaponSlot", "_resolveWeaponSlot")
        ns, _ = _neutralize_assignment(ns, "weaponSlot", "WeaponSlot")
        assert syntax_errors_for_source(ns) != []  # FINAL source is unloadable


def test_p4_unrelated_main_camera_in_other_prefab_does_not_suppress(
    tmp_path: Path,
) -> None:
    # P4 (codex MAJOR): an unrelated MainCamera-tagged node in a DIFFERENT
    # prefab/scene must NOT suppress the host's rig fact (the uniqueness check is
    # scoped to the host's owning scene/prefab, not global).
    cam = _pnode("MainCamera", tag="MainCamera", children=[_pnode("WeaponSlot")])
    player = _pnode("Player", children=[cam], comp_guid=_GUID)
    t1 = PrefabTemplate(prefab_path=Path("/p/Player.prefab"),
                        name="Player", root=player)
    # A second, unrelated prefab elsewhere ALSO carries a MainCamera tag.
    other_cam = _pnode("MainCamera", tag="MainCamera", children=[_pnode("Slot")])
    other = _pnode("Enemy", children=[other_cam])
    t2 = PrefabTemplate(prefab_path=Path("/p/Enemy.prefab"),
                        name="Enemy", root=other)
    lib = PrefabLibrary(prefabs=[t1, t2])
    src = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(0); }\n}\n"
    )
    entry = _build(tmp_path, src, lib)
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(field_name="weaponSlot", child_name="WeaponSlot"),
    )


def test_p4_non_unique_within_owning_prefab_still_abstains(tmp_path: Path) -> None:
    # The scoping does NOT weaken the in-scope uniqueness gate: two MainCamera
    # tags WITHIN the host's own prefab still abstain.
    cam1 = _pnode("MainCamera", tag="MainCamera", children=[_pnode("WeaponSlot")])
    cam2 = _pnode("MainCamera2", tag="MainCamera", children=[_pnode("WeaponSlot")])
    player = _pnode("Player", children=[cam1, cam2], comp_guid=_GUID)
    lib = PrefabLibrary(prefabs=[PrefabTemplate(
        prefab_path=Path("/p/Player.prefab"), name="Player", root=player)])
    src = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(0); }\n}\n"
    )
    entry = _build(tmp_path, src, lib)
    assert entry is not None
    assert entry.rig_facts == ()


def test_p2_seed_anchored_to_getchild_site_not_filewide(tmp_path: Path) -> None:
    # P2 (codex BLOCKING): a file-wide ``cam = Camera.main.transform`` seed that is
    # NOT the binding live AT the GetChild site must NOT admit a rig fact. Here the
    # binding live at ``weaponSlot = cam.GetChild(0)`` is ``cam = enemy.transform``
    # (foreign); a LATER ``cam = Camera.main.transform`` must not back-admit it.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Enemy enemy;\n"
        "  void Awake() {\n"
        "    cam = enemy.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "    cam = Camera.main.transform;\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()  # foreign live binding at the use site -> abstain


def test_p2_rebind_to_foreign_before_use_abstains(tmp_path: Path) -> None:
    # The NEAREST PRECEDING binding wins: a Camera.main seed REBOUND to a foreign
    # receiver before the GetChild abstains.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Enemy enemy;\n"
        "  void Awake() {\n"
        "    cam = Camera.main.transform;\n"
        "    cam = enemy.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()


def test_p3_shadowed_self_closure_not_rewritten() -> None:
    # P3 (both voices, h.4 half): a ``self`` shadowed by a closure parameter must
    # NOT be rewritten (wrong-object bind); only the real colon-receiver read is.
    src = (
        "function Player:GetRifle()\n"
        "    local fn = function(self) return self.weaponSlot end\n"
        "    return self.weaponSlot\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    # The shadowed read inside the closure is LEFT (its self is a foreign param).
    assert "function(self) return self.weaponSlot end" in out
    # The real colon-receiver read IS rewritten.
    assert "return self:_resolveWeaponSlot()" in out


def test_p3_shadowed_local_self_not_rewritten() -> None:
    # A ``local self`` in an inner block shadows the receiver for reads in that
    # block; reads OUTSIDE the shadow's scope are still rewritten.
    src = (
        "function Player:GetRifle()\n"
        "    do\n"
        "        local self = other\n"
        "        local x = self.weaponSlot\n"
        "    end\n"
        "    return self.weaponSlot\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert "local x = self.weaponSlot" in out  # shadowed -> left
    assert "return self:_resolveWeaponSlot()" in out  # real receiver -> rewritten


def test_p3_shadowed_self_mirror_in_surviving_field_read() -> None:
    # The mirror guard: a shadowed-self read must NOT count as a surviving consumer
    # read against discharge (so a script with ONLY a closure-self read + a real
    # rewritten read still discharges, and a stamp is never inflated by the shadow).
    from converter.rifle_rig_retarget_lowering import _has_surviving_field_read
    # After lowering the corpus-style closure case: the closure read survives by
    # design (it's foreign), but it must not block discharge.
    rewritten = (
        "function Player:GetRifle()\n"
        "    local fn = function(self) return self.weaponSlot end\n"
        "    return self:_resolveWeaponSlot()\n"
        "end\n"
    )
    # Only the foreign closure read remains; it is NOT a surviving consumer read.
    assert _has_surviving_field_read(rewritten, "weaponSlot") is False


def test_p5_multi_rig_fact_per_script_fails_closed() -> None:
    # P5 (codex MAJOR / claude MINOR): a script bearing >1 rig fact must NOT
    # silently keep only the last. The single-dict carrier fails closed
    # (present=False, multi_fact=True) and the lowering abstains on all edits.
    src = (
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "    self.shieldSlot = self.cam:GetChildren()[2]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot) + pivotOf(self.shieldSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    multi_map = {"/proj/Player.cs": ChildRefScript(
        facts=(),
        getchild_total=2,
        resolved_total=2,
        rig_facts=(
            RigRootedRetargetFact(field_name="weaponSlot", child_name="WeaponSlot"),
            RigRootedRetargetFact(field_name="shieldSlot", child_name="ShieldSlot"),
        ),
    )}
    n = lower_rifle_rig_retarget([s], multi_map)
    assert n == 0  # abstained on all edits — never an unverifiable partial discharge
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot",
        "present": False, "multi_fact": True,
    }
    # No edits applied — both camera-child writes survive for the loud fail-close.
    assert "self.cam:GetChildren()[1]" in s.luau_source
    assert "self.cam:GetChildren()[2]" in s.luau_source
    assert "_resolveWeaponSlot" not in s.luau_source


# === round-3 P1 fixes =======================================================


def test_r3_fallback_validates_if_then_end_block_balance(monkeypatch) -> None:
    # R3 P1 (codex BLOCKING, lowering:641): in an ANALYZER-ABSENT env the fallback
    # ``_structural_balance_ok`` must validate ``if``/``then``/``end`` block balance.
    # A single-line-``if`` neutralize whose RHS span swallows the closing ``end``
    # produces UNLOADABLE Luau; the fallback must FAIL it -> revert -> present=False
    # (NOT a stamp of present=True off the broken output). Force the fallback by
    # making ``luau_analyze_path`` report the binary absent.
    import utils.luau_analyze as ula
    monkeypatch.setattr(ula, "luau_analyze_path", lambda: None)
    src = (
        "function Player:Awake()\n"
        "    if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 0  # fallback caught the swallowed ``end`` -> abstained/reverted
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": False,
    }
    assert "self.cam:GetChildren()[1]" in s.luau_source  # reverted
    assert "_resolveWeaponSlot" not in s.luau_source
    # Direct proof the fallback rejects the broken FINAL shape but accepts the
    # well-formed original (so the gate is the discriminator, not a blanket reject).
    from converter.rifle_rig_retarget_lowering import (
        _inject_resolver_method,
        _neutralize_assignment,
        _rewrite_field_reads,
        _structural_balance_ok,
    )
    assert _structural_balance_ok(src) is True
    ns, inj = _inject_resolver_method(src, "Player", "WeaponSlot", "weaponSlot")
    assert inj
    ns, _ = _rewrite_field_reads(ns, "weaponSlot", "_resolveWeaponSlot")
    ns, _ = _neutralize_assignment(ns, "weaponSlot", "WeaponSlot")
    assert _structural_balance_ok(ns) is False  # swallowed ``end`` caught


def test_r3_fallback_accepts_well_formed_if_elseif_else() -> None:
    # The stricter block-balance must NOT false-reject a well-formed if/elseif/else
    # chain (TWO ``then`` openers but ONE ``end``) — ``elseif`` cancels its own
    # ``then``. Guards against an over-strict fallback that would abstain on valid
    # corpus Luau.
    from converter.rifle_rig_retarget_lowering import _structural_balance_ok
    well_formed = (
        "function Player:GetRifle()\n"
        "    if a then\n"
        "        return 1\n"
        "    elseif b then\n"
        "        return 2\n"
        "    else\n"
        "        return 3\n"
        "    end\n"
        "end\n\n"
        "return Player\n"
    )
    assert _structural_balance_ok(well_formed) is True


def test_r3_fallback_happy_path_still_loadable(monkeypatch) -> None:
    # The stricter fallback must NOT regress the corpus happy path: with the
    # analyzer forced absent, the well-formed corpus shape still discharges True.
    import utils.luau_analyze as ula
    monkeypatch.setattr(ula, "luau_analyze_path", lambda: None)
    s = _Script(_AI_PLAYER)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
    }


def test_r3_seed_in_dead_conditional_block_abstains(tmp_path: Path) -> None:
    # R3 P1 (codex BLOCKING, resolver:601): a ``cam = Camera.main.transform`` seed
    # buried in a dead/conditional block does NOT dominate ``cam.GetChild(0)``
    # below it, so its real receiver isn't Camera.main -> the resolver must ABSTAIN
    # (no rig fact). Order-nearest is not enough; dominance is required.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    if (false) { cam = Camera.main.transform; }\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()  # seed in a conditional block does not dominate


def test_r3_seed_in_braceless_if_abstains(tmp_path: Path) -> None:
    # The braceless single-statement conditional form is also caught.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    if (cond) cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()


def test_r3_straight_line_seed_still_admits(tmp_path: Path) -> None:
    # The straight-line happy path (seed unconditionally dominates the use) STILL
    # admits — the dominance gate doesn't over-abstain.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(field_name="weaponSlot", child_name="WeaponSlot"),
    )


def test_r3_shadow_local_function_self_not_rewritten() -> None:
    # R3 P1 (codex BLOCKING, lowering:360): a ``local function self()`` NAMES a
    # function ``self``, shadowing the colon-receiver in the enclosing scope. A
    # ``self.weaponSlot`` read after it must NOT be rewritten (wrong object).
    src = (
        "function Player:GetRifle()\n"
        "    local function self() return 1 end\n"
        "    local x = self.weaponSlot\n"
        "    return x\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert "local x = self.weaponSlot" in out  # shadowed by the local function name
    assert "self:_resolveWeaponSlot()" not in out


def test_r3_shadow_for_loop_self_var_not_rewritten() -> None:
    # The ``for _, self in ...`` loop-variable shadow form: a read inside the loop
    # body binds the loop ``self``, not the receiver -> must NOT be rewritten.
    src = (
        "function Player:GetRifle()\n"
        "    for _, self in ipairs(xs) do\n"
        "        local x = self.weaponSlot\n"
        "    end\n"
        "    return 1\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert "local x = self.weaponSlot" in out  # loop-var shadow -> left
    assert "self:_resolveWeaponSlot()" not in out


def test_r3_for_loop_self_does_not_block_real_receiver_read() -> None:
    # The loop-var shadow must be scoped to the loop BODY only: a real receiver
    # read OUTSIDE the loop is still rewritten (no over-abstain).
    src = (
        "function Player:GetRifle()\n"
        "    for _, self in ipairs(xs) do\n"
        "        local x = self.weaponSlot\n"
        "    end\n"
        "    return self.weaponSlot\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert "local x = self.weaponSlot" in out  # shadowed -> left
    assert "return self:_resolveWeaponSlot()" in out  # real receiver -> rewritten


def test_r3_neutralize_anchor_requires_ordinal_child_access() -> None:
    # R3 P1 (codex MAJOR, lowering:426): a boolean RHS that merely MENTIONS
    # ``self.cam`` but performs NO ordinal child lookup
    # (``self.weaponSlot = self.cam and self.defaultSlot``) must NOT be neutralized
    # to ``nil`` — that would be a false-green. The neutralizer must ABSTAIN.
    from converter.rifle_rig_retarget_lowering import _neutralize_assignment
    src = (
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam and self.defaultSlot\n"
        "end\n"
    )
    out, neutralized = _neutralize_assignment(src, "weaponSlot", "WeaponSlot")
    assert neutralized is False  # no ordinal child access -> abstain
    assert out == src  # untouched
    assert "self.weaponSlot = nil" not in out
    # And the real ordinal RHS IS still neutralized (discriminator, not blanket).
    src2 = (
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n"
    )
    out2, neutralized2 = _neutralize_assignment(src2, "weaponSlot", "WeaponSlot")
    assert neutralized2 is True
    assert "self.weaponSlot = nil" in out2
