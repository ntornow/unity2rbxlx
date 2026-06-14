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

import pytest

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
             path: str = "/proj/Player.cs",
             cam_receiver: str = "cam") -> dict[str, ChildRefScript]:
    return {path: ChildRefScript(
        facts=(),
        getchild_total=1,
        resolved_total=1,
        rig_facts=(RigRootedRetargetFact(
            field_name=field, child_name=child, cam_receiver=cam_receiver),),
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
    # The seeded form records the C# camera symbol (``cam``) as the receiver anchor.
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot", cam_receiver="cam"),
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
    # The direct form records the literal ``Camera.main.transform`` as the anchor.
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot",
            cam_receiver="Camera.main.transform"),
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
    # (vi) the carrier is stamped present=True, with the r3 fact-projection keys.
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": "cam", "cam_ordinal": 0,
    }


def test_b_pathA_self_gameobject_shape_discharges_present_true() -> None:
    # PATH A — THE case epoch-1 abstained on (D-S2-REDESIGN): the live AI output
    # collapsed the Camera.main ordinal to ``self.weaponSlot = self.gameObject`` (NO
    # camera-child ordinal anywhere). The write-anchored epoch-1 lowering returned
    # modified=0 / present=False; under Path A the consumer READ reroute discharges
    # present=True regardless of the write shape. RED-vs-epoch-1.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    -- weaponSlot was cam.GetChild(0)\n"
        "    self.weaponSlot = self.gameObject\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    local rifle = self.host.instantiatePrefab(self.riflePrefab, self.weaponSlot, pivotOf(self.weaponSlot))\n"
        "    if self.weaponSlot then rifle:PivotTo(pivotOf(self.weaponSlot)) end\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1  # epoch-1 returned 0 here (the abstain the re-anchor fixes)
    out = s.luau_source
    # All 4 reads rerouted; the resolver injected before return Player.
    assert out.count("self:_resolveWeaponSlot()") == 4
    assert out.count("function Player:_resolveWeaponSlot()") == 1
    assert out.index("function Player:_resolveWeaponSlot()") < out.index("return Player")
    # The Tier-2 neutralize SKIPPED (self.gameObject is not a camera ordinal) — the
    # leftover write is dead data, harmless because no raw read survives.
    assert "self.weaponSlot = self.gameObject" in out
    assert "self.weaponSlot = nil" not in out
    # Discharge is the read reroute, RHS-agnostic -> present=True.
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": "cam", "cam_ordinal": 0,
    }


def test_b_neutralize_skipped_ambiguous_but_reads_rerouted_discharges() -> None:
    # PATH A: the Tier-2 init-write neutralize is SKIP-on-ambiguity (here TWO
    # same-field writes -> the recognizer cannot prove a unique dominating init), but
    # the read reroute still discharges present=True. Proves discharge is decoupled
    # from neutralize.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.defaultSlots\n"      # ambiguous first write
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"  # camera ordinal
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1
    # The GetRifle read rerouted -> present=True regardless of how the writes resolved.
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": "cam", "cam_ordinal": 0,
    }
    assert "return pivotOf(self:_resolveWeaponSlot())" in s.luau_source


def test_b_abstain_no_matchable_read_stamps_present_false() -> None:
    # An AI shape with the write+return but NO consumer read of self.weaponSlot.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
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
        "cam_receiver": "cam", "cam_ordinal": 0,
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
        "cam_receiver": "cam", "cam_ordinal": 0,
    }
    assert "_resolveWeaponSlot" not in s.luau_source


def test_h3_neutralize_skips_on_ambiguity_two_same_field_writes() -> None:
    # D-P1-PATHA.tier2 SKIP-ON-AMBIGUITY: with MORE THAN ONE ``self.weaponSlot = ...``
    # write the "init" write cannot be UNIQUELY identified, so the Tier-2 neutralize
    # SKIPS — it neutralizes NOTHING. Here a camera-child write in Awake AND a later
    # legitimate ``self.weaponSlot = config.defaultSlot`` write coexist; neither is
    # rewritten to ``nil`` (the later legit write must be left untouched — neutralizing
    # the first ordinal match would clobber a legitimate non-init write). Discharge is
    # UNAFFECTED (decoupled from neutralize): the GetRifle read reroutes -> present=True.
    src = (
        "function Player.new(config)\n"
        "    return setmetatable({}, Player)\n"
        "end\n\n"
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:Reset()\n"
        "    self.weaponSlot = config.defaultSlot\n"  # later legitimate non-init write
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1
    out = s.luau_source
    # SKIP-on-ambiguity: NOTHING neutralized. The later legit write AND the camera
    # write both survive verbatim; no write was rewritten to nil.
    assert "self.weaponSlot = config.defaultSlot" in out  # later legit write untouched
    assert "self.weaponSlot = self.cam:GetChildren()[1]" in out  # camera write untouched
    assert "self.weaponSlot = nil" not in out
    # Discharge is decoupled -> the GetRifle read rerouted -> present=True.
    assert "return pivotOf(self:_resolveWeaponSlot())" in out
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": "cam", "cam_ordinal": 0,
    }


def test_h3_single_unique_camera_write_still_neutralizes() -> None:
    # SKIP-on-ambiguity HAPPY PATH: when there is EXACTLY ONE ``self.weaponSlot = ...``
    # write and it is the unambiguous camera-child shape, the unique dominating
    # init-write IS provable -> neutralize it (the corpus shape; the single-write
    # behavior is unchanged by the SKIP-on-ambiguity guard).
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
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
    assert "self.weaponSlot = self.cam:GetChildren()[1]" not in out  # neutralized
    assert "self.weaponSlot = nil" in out


def test_h3_multiline_camera_child_rhs_fully_replaced() -> None:
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
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


def test_h3_decoupling_present_always_equals_independent_scan() -> None:
    # PATH A DECOUPLING (supersedes the round-1 BLOCKING #3 desync): discharge keys
    # on the READ reroute, NOT on the neutralize/resolved_total, so a half-applied
    # lowering can never present green off a stamp. The decisive guard: ``present``
    # ALWAYS equals the independent source scan. Here the bare ``self.weaponSlot``
    # read in the yielding GetRifle IS rerouted, so discharge is True.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
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
        s.luau_source, "weaponSlot", "WeaponSlot", "WeaponSlot",
    )
    # The bare read was rerouted -> discharge True (RHS-agnostic, decoupled).
    assert s.rig_binding["present"] is True
    assert "local w = self:_resolveWeaponSlot()" in s.luau_source


def test_h3_no_consumer_read_means_no_silent_green() -> None:
    # The half-applied desync the coupling closes: the camera-child write exists
    # (and would be neutralized) but there is NO consumer read in a yielding method
    # for the rewrite to land -> discharge is NOT confirmed -> present=False, and
    # the lowering ABSTAINS (leaves the source) rather than faking green off the
    # pre-flipped resolved_total. (resolved_total was flipped 0->1 pre-transpile;
    # the binding-present check, not check D, is the authority on "discharged".)
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 0
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": False,
        "cam_receiver": "cam", "cam_ordinal": 0,
    }
    # Abstained: the camera-child ordinal still survives in the source so the
    # binding-present check fail-closes LOUD (and check D would still see it) —
    # never a silent green.
    assert "self.cam:GetChildren()[1]" in s.luau_source


def test_h4_member_tail_self_not_rewritten() -> None:
    # ``other.self.weaponSlot`` is a member tail -> NOT rewritten.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
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
    # h.5 YIELD-GUARD + phase-integration MAJOR #2 (lowering↔verifier discharge
    # parity): a RAW ``self.<field>`` READ surviving in non-yielding ``Awake`` is a
    # BOUNDARY form the slice-1.2 verifier fails closed on. The lowering MUST mirror
    # it and ABSTAIN (present=False, source unedited for the reroute) — NOT stamp
    # present=True while leaving the Awake read (the desync). Supersedes the old
    # "Awake read sees a safe nil, still discharge" premise (design §1.6 / lines 194).
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    local cached = self.weaponSlot\n"  # non-yielding raw read -> boundary form
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert n == 0  # abstained — never an unverifiable partial discharge
    assert (s.rig_binding or {}).get("present") is False
    # Source unedited for the reroute: no resolver injected, the reads stay raw.
    assert "_resolveWeaponSlot" not in out
    assert "local cached = self.weaponSlot" in out
    assert "return pivotOf(self.weaponSlot)" in out


def test_h5_start_is_non_yielding() -> None:
    # Same boundary-form abstain as Awake, for the other non-yielding lifecycle
    # method ``Start`` — the lowering abstains to stay in lock-step with the verifier.
    src = (
        "function Player:Start()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    local cached = self.weaponSlot\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    out = s.luau_source
    assert n == 0
    assert (s.rig_binding or {}).get("present") is False
    assert "_resolveWeaponSlot" not in out
    assert "local cached = self.weaponSlot" in out
    assert "return pivotOf(self.weaponSlot)" in out


# === FINDING 2: lowering↔verifier discharge parity on BOUNDARY forms ==========
# A read of the field the consumer-read reroute CANNOT safely rewrite is a form the
# slice-1.2 verifier fails closed on. The lowering MUST mirror it and ABSTAIN
# (present=False, source unedited for the reroute) so the two AGREE — never stamp
# present=True on a verifier-fire case. A CLEAN dot-form script still discharges True.


def _assert_boundary_abstain(src: str) -> _Script:
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 0  # abstained on the reroute
    assert (s.rig_binding or {}).get("present") is False
    assert "_resolveWeaponSlot" not in s.luau_source  # no resolver injected
    return s


def test_f2_bracket_string_key_read_abstains() -> None:
    # A bracket-index read ``self["weaponSlot"]`` is the boundary form the reroute
    # cannot rewrite -> abstain (verifier rejects it).
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        'function Player:GetRifle()\n'
        '    return pivotOf(self["weaponSlot"])\n'
        "end\n\n"
        "return Player\n"
    )
    s = _assert_boundary_abstain(src)
    assert 'self["weaponSlot"]' in s.luau_source  # left unedited


def test_f2_dynamic_bracket_read_abstains() -> None:
    # A DYNAMIC bracket read ``self[k]`` (incl. parenthesized ``self[(k)]``) cannot be
    # tied to (or ruled out as) the field -> abstain.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        '    local k = "weaponSlot"\n'
        "    return pivotOf(self[(k)])\n"
        "end\n\n"
        "return Player\n"
    )
    _assert_boundary_abstain(src)


def test_f2_non_self_receiver_read_abstains() -> None:
    # A NON-``self`` receiver read of the field (module-table / alias) -> abstain.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    local p = self\n"
        "    return pivotOf(p.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _assert_boundary_abstain(src)
    assert "p.weaponSlot" in s.luau_source  # left unedited


def test_f2_module_table_receiver_read_abstains() -> None:
    # ``Player.weaponSlot`` (module-table receiver) is a non-self form -> abstain.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(Player.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    _assert_boundary_abstain(src)


def test_f2_clean_dot_form_still_discharges() -> None:
    # CONTROL: a clean ``self.<field>`` dot-form consumer in a yield-safe method has
    # NO boundary form -> discharges present=True (the change must not over-abstain).
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
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
    assert (s.rig_binding or {}).get("present") is True
    assert "return pivotOf(self:_resolveWeaponSlot())" in s.luau_source


def test_f2_unrelated_bracket_index_does_not_block_discharge() -> None:
    # A pure-integer ``self[1]`` (array index, NOT a field read) and a DIFFERENT
    # string key ``self["other"]`` are NOT boundary forms -> a clean dot-form read
    # still discharges True (no over-abstain on unrelated bracket access).
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    local a = self[1]\n"
        '    local b = self["other"]\n'
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1
    assert (s.rig_binding or {}).get("present") is True
    assert "return pivotOf(self:_resolveWeaponSlot())" in s.luau_source


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
        "    self.cam = workspace.CurrentCamera\n"
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
        "cam_receiver": "cam", "cam_ordinal": 0,
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


def test_p1_single_line_if_neutralize_skips_whole_rhs_guard_still_discharges() -> None:
    # P1 (codex BLOCKING) was: the Tier-2 neutralize swallowed an inline
    # ``if ... then self.weaponSlot = ... end``'s closing ``end`` (RHS span to the
    # newline) -> UNLOADABLE Luau. The WHOLE-RHS guard PREVENTS that at the source:
    # the camera-child access must BE the whole RHS value, so an RHS that trails
    # ``... end`` does NOT match -> the neutralize SKIPS (no corruption).
    # PATH A: the neutralize SKIP no longer blocks discharge — the GetRifle read is
    # rerouted, so present=True even though the if-write is left intact (dead data).
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1  # the read reroute discharged; the if-write neutralize SKIPPED
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": "cam", "cam_ordinal": 0,
    }
    # The if-write is left intact + LOADABLE (the whole-RHS guard never swallowed ``end``).
    assert "self.cam:GetChildren()[1] end" in s.luau_source
    # The GetRifle read was rerouted (the load-bearing discharge).
    assert "return pivotOf(self:_resolveWeaponSlot())" in s.luau_source
    # Direct: the neutralize SKIPS on the single-line-if shape (WHOLE-RHS guard),
    # so the FINAL source stays loadable (no swallowed ``end``).
    from converter.rifle_rig_retarget_lowering import (
        _camera_symbol_forms,
        _neutralize_assignment,
    )
    out, neutralized = _neutralize_assignment(
        src, "weaponSlot", "WeaponSlot", _camera_symbol_forms("cam")
    )
    assert neutralized is False  # WHOLE-RHS guard -> SKIP, never corrupt
    assert out == src
    if luau_analyze_path():
        assert syntax_errors_for_source(s.luau_source) == []  # final source loadable


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
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot",
            cam_receiver="Camera.main.transform"),
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
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
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
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
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
        "    self.cam = workspace.CurrentCamera\n"
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
    # REDESIGN r3: the multi-fact carrier is a FULL 5-key carrier (from the FIRST
    # rig fact) + present=False + multi_fact=True, so it round-trips the 5-key
    # rehydrate LOAD validator and fires LOUD on the resume path too.
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot",
        "present": False, "multi_fact": True,
        "cam_receiver": "", "cam_ordinal": 0,
    }
    # No edits applied — both camera-child writes survive for the loud fail-close.
    assert "self.cam:GetChildren()[1]" in s.luau_source
    assert "self.cam:GetChildren()[2]" in s.luau_source
    assert "_resolveWeaponSlot" not in s.luau_source


# === round-3 P1 fixes =======================================================


def test_r3_fallback_validates_if_then_end_block_balance(monkeypatch) -> None:
    # R3 P1 (codex BLOCKING, lowering:641): in an ANALYZER-ABSENT env the fallback
    # ``_structural_balance_ok`` must validate ``if``/``then``/``end`` block balance.
    # A source that swallowed a single-line-``if``'s closing ``end`` is UNLOADABLE
    # Luau and the fallback must FAIL it (NOT a stamp of present=True off broken
    # output). Force the fallback by making ``luau_analyze_path`` report absent.
    import utils.luau_analyze as ula
    monkeypatch.setattr(ula, "luau_analyze_path", lambda: None)
    from converter.rifle_rig_retarget_lowering import _structural_balance_ok
    # Well-formed single-line-if -> accepted.
    good = (
        "function Player:Awake()\n"
        "    if self.cam then self.weaponSlot = nil end\n"
        "end\n\n"
        "return Player\n"
    )
    assert _structural_balance_ok(good) is True
    # The same shape with the inline ``end`` SWALLOWED (the bug a corrupting
    # neutralize would produce) -> the unclosed ``then`` block FAILS.
    swallowed = (
        "function Player:Awake()\n"
        "    if self.cam then self.weaponSlot = nil\n"
        "end\n\n"
        "return Player\n"
    )
    assert _structural_balance_ok(swallowed) is False  # unbalanced then -> reject
    # PATH A: the WHOLE-RHS-aware Tier-2 neutralize SKIPS the single-line-if camera
    # write (it never corrupts the ``end``), but the GetRifle read STILL reroutes ->
    # present=True, and the final source stays loadable under the fallback.
    src = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert n == 1
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": "cam", "cam_ordinal": 0,
    }
    assert "self.cam:GetChildren()[1] end" in s.luau_source  # if-write untouched
    assert "return pivotOf(self:_resolveWeaponSlot())" in s.luau_source
    assert _structural_balance_ok(s.luau_source) is True  # final source loadable


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
        "cam_receiver": "cam", "cam_ordinal": 0,
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
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot", cam_receiver="cam"),
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
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
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
    from converter.rifle_rig_retarget_lowering import (
        _camera_symbol_forms,
        _neutralize_assignment,
    )
    cam = _camera_symbol_forms("cam")
    src = (
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam and self.defaultSlot\n"
        "end\n"
    )
    out, neutralized = _neutralize_assignment(src, "weaponSlot", "WeaponSlot", cam)
    assert neutralized is False  # no ordinal child access -> SKIP (Tier-2 best-effort)
    assert out == src  # untouched
    assert "self.weaponSlot = nil" not in out
    # And the real camera ordinal RHS IS still neutralized (discriminator, not blanket).
    src2 = (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n"
    )
    out2, neutralized2 = _neutralize_assignment(src2, "weaponSlot", "WeaponSlot", cam)
    assert neutralized2 is True
    assert "self.weaponSlot = nil" in out2


# === round-4 P1 fixes =======================================================


def test_r4_special_char_child_name_produces_valid_luau() -> None:
    # R4 BLOCKING (lowering:107,196,678): a Roblox child name with a SPACE
    # (``"Weapon Slot"``) must NOT be raw-spliced into a Luau method identifier
    # (``self:_resolveWeapon Slot()`` is invalid). The method-name suffix is
    # sanitized to a valid identifier while the rig LOOKUP still uses the REAL
    # ``"Weapon Slot"`` string in ``FindFirstChild``.
    s = _Script(_AI_PLAYER)
    crm = _rig_map(field="weaponSlot", child="Weapon Slot")
    lower_rifle_rig_retarget([s], crm)
    out = s.luau_source
    # No invalid identifier is ever emitted (no bare ``_resolveWeapon Slot``).
    assert "_resolveWeapon Slot" not in out
    # The rig lookup uses the LITERAL real child name string.
    assert 'rig:FindFirstChild("Weapon Slot", true)' in out
    # The carrier discharges True on the corpus shape with a spaced child name.
    assert s.rig_binding is not None
    assert s.rig_binding["present"] is True
    assert s.rig_binding["child"] == "Weapon Slot"
    # The emitted method-name identifier parses as valid Luau (if analyzer present).
    if luau_analyze_path():
        assert syntax_errors_for_source(out) == []


def test_r4_special_char_method_suffix_is_valid_luau_identifier() -> None:
    # The sanitized suffix is always a valid Luau identifier (generic — any
    # special char). Distinct child names get distinct suffixes (collision-resistant).
    from converter.rifle_rig_retarget_lowering import _LUAU_IDENT_RE, _method_suffix
    for name in ("Weapon Slot", "Weapon-Slot", "1stSlot", "слот", "a.b", "Slot!"):
        suffix = _method_suffix(name)
        assert _LUAU_IDENT_RE.match(suffix), f"{name!r} -> {suffix!r} invalid"
    # A valid identifier passes through verbatim (happy-path + idempotency).
    assert _method_suffix("WeaponSlot") == "WeaponSlot"
    # Two distinct names that sanitize alike get distinct suffixes.
    assert _method_suffix("Weapon Slot") != _method_suffix("Weapon-Slot")


def test_r4_preexisting_foreign_resolver_method_not_false_discharged() -> None:
    # R4 BLOCKING (lowering:240,578): a PREEXISTING foreign ``_resolveWeaponSlot``
    # method (body just ``return nil``) + a preexisting call, NO camera-child write,
    # NO surviving consumer read. The OLD shape-only discharge (bare same-named
    # method + call + no read + no camera-write) stamps present=True with the
    # lowering having done NO work this run (modified=0, no own emit). Discharge must
    # bind to the lowering's OWN emit: never a present=True with the FOREIGN method
    # as the only resolver.
    src = (
        "function Player:_resolveWeaponSlot()\n"
        "    return nil\n"  # FOREIGN body — not the lowering's emit
        "end\n\n"
        "function Player:GetRifle()\n"
        "    return self:_resolveWeaponSlot()\n"  # a call already exists; no bare read
        "end\n\n"
        "return Player\n"
    )
    s = _Script(src)
    n = lower_rifle_rig_retarget([s], _rig_map())
    assert s.rig_binding is not None
    own_emitted = 'm:GetAttribute("_MainCameraRig")' in s.luau_source
    # INVARIANT: present=True ONLY if the lowering's OWN emit landed this run.
    # A bare foreign method must never count as discharged.
    if s.rig_binding["present"] is True:
        assert own_emitted and n == 1  # re-injected the OWN method (real work)
    else:
        # Abstained: present=False, foreign method left, no own emit.
        assert s.rig_binding["present"] is False
        assert not own_emitted


def test_r4_foreign_resolver_alone_is_not_own_emit() -> None:
    # Unit: a foreign ``_resolveWeaponSlot`` (different body) is NOT recognized as
    # the lowering's own method (structural equality to the canonical emit); the
    # lowering's OWN emit IS.
    from converter.rifle_rig_retarget_lowering import (
        _has_own_resolver_method,
        _resolver_method_text,
    )
    own = _resolver_method_text("Player", "WeaponSlot", "weaponSlot", "WeaponSlot")
    foreign = (
        "function Player:_resolveWeaponSlot()\n"
        "    return nil\n"
        "end\n"
    )
    assert _has_own_resolver_method(foreign, "WeaponSlot", own) is False
    assert _has_own_resolver_method(own, "WeaponSlot", own) is True


def test_r4_fallback_tail_scan_allows_trailing_comment(monkeypatch) -> None:
    # R4 MINOR (lowering:762): the analyzer-absent fallback's post-return tail scan
    # must be code-position aware — a COMMENT after ``return <Class>`` containing
    # ``end``/``function`` as prose must NOT false-reject a valid source. Both the
    # inline-comment form (``return Player -- ...``) and a following comment LINE.
    import utils.luau_analyze as ula
    monkeypatch.setattr(ula, "luau_analyze_path", lambda: None)
    from converter.rifle_rig_retarget_lowering import _structural_balance_ok
    inline = (
        "function Player:GetRifle()\n"
        "    return 1\n"
        "end\n\n"
        "return Player -- ends the function\n"
    )
    assert _structural_balance_ok(inline) is True
    following_comment_line = (
        "function Player:GetRifle()\n"
        "    return 1\n"
        "end\n\n"
        "return Player\n"
        "-- this is the end of the function module\n"
    )
    assert _structural_balance_ok(following_comment_line) is True
    # A REAL code-level ``function``/``end`` after the return is still rejected.
    broken = (
        "return Player\n"
        "function Player:Stray()\n"
        "    return 2\n"
        "end\n"
    )
    assert _structural_balance_ok(broken) is False


def test_r4_typed_local_decl_abstains_field_write_admits(tmp_path: Path) -> None:
    # R4 MAJOR (resolver:723,760): a C# TYPED LOCAL declaration
    # ``Transform weaponSlot = Camera.main.transform.GetChild(0);`` must NOT be
    # admitted as a rig fact (it would flip resolved_total + a bogus fail-closed
    # path); only a bare FIELD write admits.
    typed_local = (
        "public class Player : MonoBehaviour {\n"
        "  void Awake() {\n"
        "    Transform weaponSlot = Camera.main.transform.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, typed_local, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()  # typed local decl -> abstain
    # The real FIELD write still admits.
    field_write = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(0); }\n}\n"
    )
    entry2 = _build(tmp_path, field_write, _fps_library())
    assert entry2 is not None
    assert entry2.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot",
            cam_receiver="Camera.main.transform"),
    )


def test_r4_var_local_decl_abstains(tmp_path: Path) -> None:
    # ``var weaponSlot = Camera.main.transform.GetChild(0);`` — a ``var`` typed local
    # is also a declaration -> abstain.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  void Awake() {\n"
        "    var weaponSlot = Camera.main.transform.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()


# === round-5 P1 fixes =======================================================


def test_r5_discharge_is_rhs_agnostic_across_write_shapes() -> None:
    # PATH A re-anchor: discharge keys on the consumer-READ reroute (the AI-STABLE
    # member access), NOT on matching the camera-child WRITE shape. So the field's
    # binding discharges present=True REGARDLESS of the RHS the AI emitted — the 5
    # real shapes (:GetChildren()[1], self.gameObject, __unityChild(...), a multi-step
    # local, a FindFirstChild fallback). The Tier-2 neutralize SKIPS the shapes it
    # cannot recognize as a camera ordinal (harmless: no raw read survives). This
    # REPLACES the epoch-1 r5 tests that (under the superseded coupling) asserted
    # present=False whenever the neutralize could not camera-anchor the write.
    shapes = (
        "self.cam and self.cam:GetChildren()[1]",   # corpus ordinal (neutralized)
        "self.gameObject",                          # AI collapsed to GameObject
        "self.cam and __unityChild(self.cam, 1)",   # __unityChild helper
        "self.defaultSlots:GetChildren()[1]",       # ordinal on a NON-camera receiver
        "self.defaultSlots or self.cam:GetChildren()[1]",  # mixed disjunction
    )
    for rhs in shapes:
        src = (
            "function Player:Awake()\n"
            "    self.cam = workspace.CurrentCamera\n"
            f"    self.weaponSlot = {rhs}\n"
            "end\n\n"
            "function Player:GetRifle()\n"
            "    return pivotOf(self.weaponSlot)\n"
            "end\n\n"
            "return Player\n"
        )
        s = _Script(src)
        n = lower_rifle_rig_retarget([s], _rig_map(cam_receiver="cam"))
        # The yielding GetRifle read is rerouted on EVERY shape -> discharge True.
        assert n == 1, rhs
        assert s.rig_binding == {
            "field": "weaponSlot", "child": "WeaponSlot", "present": True,
            "cam_receiver": "cam", "cam_ordinal": 0,
        }, rhs
        assert "return pivotOf(self:_resolveWeaponSlot())" in s.luau_source, rhs

def test_r5_structural_balance_fails_closed_on_unterminated_long_bracket(
    monkeypatch,
) -> None:
    # R5 (codex follow-up): the analyzer-absent fallback must FAIL CLOSED on an
    # UNTERMINATED long-bracket comment/string or a short string spanning a newline
    # (all invalid Luau) — never treat "rest of file is a comment/string" as valid.
    import utils.luau_analyze as ula
    monkeypatch.setattr(ula, "luau_analyze_path", lambda: None)
    from converter.rifle_rig_retarget_lowering import _structural_balance_ok
    assert _structural_balance_ok("--[[ unterminated block comment\n") is False
    assert _structural_balance_ok("local s = [[ unterminated long string\n") is False
    assert _structural_balance_ok('local s = "oops\nreturn Player\n') is False
    # A properly TERMINATED block comment + valid module still passes.
    assert _structural_balance_ok(
        "--[[ terminated ]]\nfunction P:F()\n    return 1\nend\n\nreturn P\n"
    ) is True


def test_r5_lhs_abstains_on_every_typed_local_form(tmp_path: Path) -> None:
    # R5 (codex follow-up): the typed-local guard ALLOW-LISTS by statement terminator
    # (fail-closed). It must ABSTAIN on EVERY typed-local form a single-char reject-
    # list could not enumerate — simple, generic, array, qualified, NULLABLE
    # (``Transform?``), TUPLE (``(Transform, int)``), and COMMENT-separated
    # (``Transform /*c*/``) — admitting a typed local would be a bogus rig fact
    # (a false fail-close on valid code). A plain-statement bare write still admits.
    for body in (
        "Transform weaponSlot = Camera.main.transform.GetChild(0);",
        "List<Transform> weaponSlot = Camera.main.transform.GetChild(0);",
        "Transform[] weaponSlot = Camera.main.transform.GetChild(0);",
        "System.Collections.Generic.List<Transform> weaponSlot = "
        "Camera.main.transform.GetChild(0);",
        "Transform? weaponSlot = Camera.main.transform.GetChild(0);",
        "(Transform, int) weaponSlot = Camera.main.transform.GetChild(0);",
        "Transform /*c*/ weaponSlot = Camera.main.transform.GetChild(0);",
    ):
        src = (
            "public class Player : MonoBehaviour {\n"
            "  public Transform weaponSlot;\n"
            f"  void Awake() {{ {body} }}\n}}\n"
        )
        entry = _build(tmp_path, src, _fps_library())
        assert entry is not None, body
        assert entry.rig_facts == (), f"typed local must abstain: {body!r}"
    # A plain-statement bare field write still admits.
    plain = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(0); }\n}\n"
    )
    entry = _build(tmp_path, plain, _fps_library())
    assert entry is not None
    assert len(entry.rig_facts) == 1


def test_r5_foreign_resolver_with_marker_is_not_own_emit() -> None:
    # R5 BLOCKING (lowering:687): a FOREIGN ``_resolveWeaponSlot`` whose body USES
    # the ``_MainCameraRig`` marker as live code (but differs from the canonical
    # emit) must NOT count as the lowering's own emit. With such a method + an
    # existing call already present and NO write/read for the lowering to edit, the
    # old marker-substring check false-discharged present=True on modified=0.
    foreign_src = (
        "function Player:GetRifle()\n"
        "    return self:_resolveWeaponSlot()\n"
        "end\n\n"
        "function Player:_resolveWeaponSlot()\n"
        "    -- a DIFFERENT, foreign body that merely uses the marker as live code\n"
        '    local m = workspace:FindFirstChild("Rig")\n'
        '    if m and m:GetAttribute("_MainCameraRig") then return m end\n'
        "    return nil\n"
        "end\n\n"
        "return Player\n"
    )
    s = _Script(foreign_src)
    n = lower_rifle_rig_retarget([s], _rig_map(cam_receiver="cam"))
    # The foreign method is NOT the canonical emit -> the lowering RE-INJECTS its
    # own (real work) when it can. present=True is earned ONLY when the OWN method
    # landed this run (n == 1), never a marker-substring false-green on modified=0.
    assert s.rig_binding is not None
    if s.rig_binding["present"] is True:
        assert n == 1
        from converter.rifle_rig_retarget_lowering import (
            _has_own_resolver_method,
            _resolver_method_text,
        )
        own = _resolver_method_text("Player", "WeaponSlot", "weaponSlot", "WeaponSlot")
        assert _has_own_resolver_method(s.luau_source, "WeaponSlot", own) is True
    else:
        assert n == 0
        assert s.rig_binding["present"] is False


def test_r5_has_own_resolver_rejects_foreign_marker_body() -> None:
    # Unit for the structural-equality own-emit check (R5 BLOCKING): a foreign
    # same-named method that merely USES the marker is NOT the own emit; the
    # canonical emit IS.
    from converter.rifle_rig_retarget_lowering import (
        _has_own_resolver_method,
        _resolver_method_text,
    )
    own = _resolver_method_text("Player", "WeaponSlot", "weaponSlot", "WeaponSlot")
    # The EXACT own-emit marker (``m:GetAttribute("_MainCameraRig")``) used as live
    # code in a DIFFERENT body — the adversarial case the round-4 marker-substring
    # check false-passed. Structural equality rejects it.
    foreign_with_marker = (
        "function Player:_resolveWeaponSlot()\n"
        "    for _, m in workspace:GetDescendants() do\n"
        '        if m:GetAttribute("_MainCameraRig") then return m end\n'
        "    end\n"
        "    return nil\n"
        "end\n"
    )
    assert _has_own_resolver_method(foreign_with_marker, "WeaponSlot", own) is False
    assert _has_own_resolver_method(own, "WeaponSlot", own) is True


def test_r5_resolver_abstains_generic_array_qualified_typed_locals(
    tmp_path: Path,
) -> None:
    # R5 MAJOR (resolver:723): a GENERIC / ARRAY / QUALIFIED-GENERIC typed local
    # declaration must ABSTAIN (the round-4 check only rejected an alnum preceding
    # char, so a type ending in ``>``/``]``/``.`` false-admitted). Only a bare field
    # write admits.
    for decl in (
        "List<Transform> weaponSlot = Camera.main.transform.GetChild(0);",
        "Transform[] weaponSlot = Camera.main.transform.GetChild(0);",
        "System.Collections.Generic.List<Transform> weaponSlot = "
        "Camera.main.transform.GetChild(0);",
        "IDictionary<int,Transform> weaponSlot = Camera.main.transform.GetChild(0);",
    ):
        src = (
            "public class Player : MonoBehaviour {\n"
            "  void Awake() {\n"
            f"    {decl}\n"
            "  }\n}\n"
        )
        entry = _build(tmp_path, src, _fps_library())
        assert entry is not None, decl
        assert entry.rig_facts == (), f"typed local must abstain: {decl}"
    # The bare field write still admits.
    field_write = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(0); }\n}\n"
    )
    entry = _build(tmp_path, field_write, _fps_library())
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot",
            cam_receiver="Camera.main.transform"),
    )


def test_r5_structural_balance_skips_block_comment_brackets(monkeypatch) -> None:
    # R5 MAJOR (lowering:_structural_balance_ok): the analyzer-absent fallback must
    # SKIP Luau long-bracket block comments/strings before counting brackets, so a
    # valid source carrying a ``--[[ ... ]]`` block comment with brackets/keywords
    # is NOT false-rejected (the ``]]`` closer leaked as a code-level ``]`` closer).
    import utils.luau_analyze as ula
    monkeypatch.setattr(ula, "luau_analyze_path", lambda: None)
    from converter.rifle_rig_retarget_lowering import _structural_balance_ok
    # A valid module with a multi-line block comment containing brackets + keywords.
    valid = (
        "--[[ a block comment\n"
        "  containing ] brackets [ and the words function end if then do\n"
        "  and a long string [=[ nested ]=] inside\n"
        "]]\n"
        "function Player:Foo()\n"
        "    local t = { a = 1, b = 2 }\n"
        "    if t.a then return t.b end\n"
        "end\n\n"
        "return Player\n"
    )
    assert _structural_balance_ok(valid) is True
    # A long STRING with brackets is likewise skipped.
    valid_str = (
        "function Player:Bar()\n"
        "    local s = [[ ] [ ]]\n"
        "    return s\n"
        "end\n\n"
        "return Player\n"
    )
    assert _structural_balance_ok(valid_str) is True
    # A GENUINELY unbalanced source still fails (an extra code-level ``)``).
    unbalanced = (
        "function Player:Baz()\n"
        "    return (1 + 2))\n"
        "end\n\n"
        "return Player\n"
    )
    assert _structural_balance_ok(unbalanced) is False


def test_r6_member_write_does_not_seed_bare_cam(tmp_path: Path) -> None:
    # R6 BLOCKING (resolver:_canonical_receiver) — phase-integration FINDING 1
    # (was an accepted xfail residual; now FIXED): ``other.cam = Camera.main.transform``
    # is a member write on ANOTHER object — it must NOT seed the bare local ``cam``
    # read at ``weaponSlot = cam.GetChild(0)`` (the GetChild's ``cam`` is a distinct
    # local with no Camera.main binding live at the use site -> abstain).
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Other other;\n"
        "  void Awake() {\n"
        "    other.cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


def test_r6_bare_local_seed_still_admits(tmp_path: Path) -> None:
    # The legit bare-local seed (no member-access tail) still admits the fact.
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
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot", cam_receiver="cam"),
    )
    assert entry.resolved_total == 1


def test_r7_this_dot_cam_field_seed_admits(tmp_path: Path) -> None:
    # R7 (resolver:_canonical_receiver revert): ``this.cam = Camera.main.transform``
    # is a SAME-OBJECT field seed for the bare local ``cam`` read at the GetChild —
    # it must ADMIT. The round-6 member-dot seed filter wrongly false-rejected this
    # (the ``.`` before ``cam`` looked like a foreign member write); reverting it
    # restores the legitimate ``this.``-qualified field seed.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    this.cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot", cam_receiver="cam"),
    )
    assert entry.resolved_total == 1


def test_f1_deep_member_seed_does_not_seed_bare_cam(tmp_path: Path) -> None:
    # FINDING 1 extension: a DEEPER member-chain seed ``a.b.cam = Camera.main.transform``
    # is still a FOREIGN member write (``cam`` preceded by ``.``, prefix != ``this``) —
    # it must NOT seed the bare local ``cam`` at the GetChild. Guards the multi-dot
    # case beyond the single-dot ``other.cam`` above.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Holder a;\n"
        "  void Awake() {\n"
        "    a.b.cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


def test_f1_foreign_this_tail_member_seed_rejected(tmp_path: Path) -> None:
    # FINDING 1 edge: ``foo.this.cam = ...`` is NOT a legitimate ``this.``-qualified
    # seed (``this`` is itself a member tail of ``foo``) — it must NOT seed bare ``cam``.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Wrap foo;\n"
        "  void Awake() {\n"
        "    foo.this.cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


def test_r6_line_comment_before_field_write_still_admits(tmp_path: Path) -> None:
    # R6 MAJOR (resolver:_lhs_is_bare_field): a bare field write preceded by a
    # ``// line comment`` must still admit — the comment is not a leading token, so
    # the rig fact must NOT silently drop (else the rifle binding is not retargeted).
    src = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    // pick the camera's first child as the weapon slot\n"
        "    weaponSlot = Camera.main.transform.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot",
            cam_receiver="Camera.main.transform"),
    )
    assert entry.resolved_total == 1


def test_r6_block_comment_before_field_write_still_admits(tmp_path: Path) -> None:
    # An inline ``/* block */`` comment between the prior statement and the field
    # write is likewise skipped; the bare field write still admits.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    DoSetup(); /* slot from camera rig */ weaponSlot = Camera.main.transform.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot",
            cam_receiver="Camera.main.transform"),
    )
    assert entry.resolved_total == 1


def test_r6_typed_local_after_comment_still_abstains(tmp_path: Path) -> None:
    # Don't regress the round-5 typed-local allow-list: a typed-local declaration
    # remains a leading TOKEN even when a comment precedes the statement, so it must
    # still abstain (the field never persists on the instance -> not a rig fact).
    src = (
        "public class Player : MonoBehaviour {\n"
        "  void Awake() {\n"
        "    // local temp, not a field\n"
        "    Transform weaponSlot = Camera.main.transform.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


# === REDESIGN r3 — carrier gains cam_receiver + cam_ordinal =================
# S1 promotes the resolver fact's deterministic identity (cam_receiver +
# a new RigRootedRetargetFact.ordinal) into the rig_binding carrier, threaded
# through transpile/pipeline-copy/rehydrate so slice 1.2's check-D exemption
# can anchor on it. These tests assert the new keys and the widened LOAD
# validator (all FIVE keys, partial-row drop).


def test_r3_fact_carries_ordinal_from_nonzero_getchild(tmp_path: Path) -> None:
    # The credited GetChild(n) ordinal is captured on the fact (was implicit
    # int(m.group(3)); r3 surfaces it as RigRootedRetargetFact.ordinal).
    src = (
        "public class Player : MonoBehaviour {\n"
        "  public Transform weaponSlot;\n"
        "  void Awake() { weaponSlot = Camera.main.transform.GetChild(1); }\n"
        "}\n"
    )
    # child[1] must resolve uniquely -> give the MainCamera node a second child.
    lib = _fps_library(child_name="Barrel", second_child="WeaponSlot")
    entry = _build(tmp_path, src, lib)
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot",
            cam_receiver="Camera.main.transform", ordinal=1),
    )


def test_r3_carrier_stamps_cam_receiver_and_ordinal_from_fact() -> None:
    # The discharged carrier carries cam_receiver + cam_ordinal as fact
    # projections (NON-default values prove they are threaded from the fact, not
    # hardcoded): a direct-form receiver with ordinal=2.
    s = _Script(_AI_PLAYER)
    crm = {"/proj/Player.cs": ChildRefScript(
        facts=(), getchild_total=1, resolved_total=1,
        rig_facts=(RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot",
            cam_receiver="Camera.main.transform", ordinal=2),),
    )}
    n = lower_rifle_rig_retarget([s], crm)
    assert n == 1
    assert s.rig_binding == {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": "Camera.main.transform", "cam_ordinal": 2,
    }


def test_r3_carrier_keys_are_rhs_agnostic_across_shapes() -> None:
    # The cam_receiver/cam_ordinal keys are stamped from the fact regardless of
    # the AI write shape (they are fact projections, not output fingerprints).
    shapes = [
        "self.cam:GetChildren()[1]",
        "self.gameObject",
        "self.cam and __unityChild(self.cam, 1)",
    ]
    for rhs in shapes:
        src = (
            "function Player:Awake()\n"
            "    self.cam = workspace.CurrentCamera\n"
            f"    self.weaponSlot = {rhs}\n"
            "end\n\n"
            "function Player:GetRifle()\n"
            "    return pivotOf(self.weaponSlot)\n"
            "end\n\n"
            "return Player\n"
        )
        s = _Script(src)
        lower_rifle_rig_retarget([s], _rig_map(cam_receiver="cam"))
        assert s.rig_binding is not None
        assert s.rig_binding["cam_receiver"] == "cam", rhs
        assert s.rig_binding["cam_ordinal"] == 0, rhs


def _pipeline_for_rehydrate(out_dir: Path):
    from converter.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    p.output_dir = out_dir  # the only attr _load_rig_binding_for_rehydration reads
    return p


def _write_plan(out_dir: Path, rig_binding: dict) -> None:
    import json as _json
    (out_dir / "conversion_plan.json").write_text(
        _json.dumps({"rig_binding": rig_binding}), encoding="utf-8")


def test_r3_rehydrate_load_preserves_all_five_keys(tmp_path: Path) -> None:
    # SAVE persists the full carrier; LOAD (widened) reads + validates ALL FIVE
    # keys and round-trips them (the check-D exemption anchor survives resume).
    carrier = {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": "cam", "cam_ordinal": 0,
    }
    _write_plan(tmp_path, {"Player": carrier})
    p = _pipeline_for_rehydrate(tmp_path)
    loaded = p._load_rig_binding_for_rehydration()
    assert loaded == {"Player": carrier}


def test_r3_rehydrate_present_false_5key_survives_to_fire_loud(tmp_path: Path) -> None:
    # A present=False 5-key carrier (the abstained / multi-fact case) MUST survive
    # rehydrate intact so the binding-present verifier fires loud on the resume
    # path — NOT get dropped to None (which would silently abstain).
    carrier = {
        "field": "weaponSlot", "child": "WeaponSlot", "present": False,
        "cam_receiver": "", "cam_ordinal": 0, "multi_fact": True,
    }
    _write_plan(tmp_path, {"Player": carrier})
    p = _pipeline_for_rehydrate(tmp_path)
    loaded = p._load_rig_binding_for_rehydration()
    assert loaded == {"Player": carrier}  # incl. the optional multi_fact flag


def test_r3_rehydrate_drops_partial_carrier_missing_new_keys(tmp_path: Path) -> None:
    # The PRE-FIX 3-key carrier (no cam_receiver/cam_ordinal) is now a PARTIAL row
    # -> dropped to None -> the verifier abstains (the safe default), NEVER a
    # partial carrier that would exempt blind in check D.
    _write_plan(tmp_path, {"Player": {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True}})
    p = _pipeline_for_rehydrate(tmp_path)
    assert p._load_rig_binding_for_rehydration() == {}


def test_r3_rehydrate_drops_row_with_malformed_cam_ordinal(tmp_path: Path) -> None:
    # cam_ordinal must be an int (and NOT a bool masquerading as int) — a malformed
    # value drops the whole row.
    for bad in ["0", True, None, 1.5]:
        _write_plan(tmp_path, {"Player": {
            "field": "weaponSlot", "child": "WeaponSlot", "present": True,
            "cam_receiver": "cam", "cam_ordinal": bad}})
        p = _pipeline_for_rehydrate(tmp_path)
        assert p._load_rig_binding_for_rehydration() == {}, bad


def test_r3_rehydrate_drops_row_with_nonstr_cam_receiver(tmp_path: Path) -> None:
    _write_plan(tmp_path, {"Player": {
        "field": "weaponSlot", "child": "WeaponSlot", "present": True,
        "cam_receiver": 123, "cam_ordinal": 0}})
    p = _pipeline_for_rehydrate(tmp_path)
    assert p._load_rig_binding_for_rehydration() == {}


# === r4 fix-round FINDING 1 — seed-LHS trivia false-admit (UNSAFE) ===========
# ``_seed_lhs_is_bare_or_this`` must skip ALL C# trivia (spaces, tabs, NEWLINES,
# ``//`` line + ``/* */`` block comments) between the seed symbol and a preceding
# ``.``: a FOREIGN member-LHS seed split by a comment/newline (``other.\ncam = ...``
# / ``other./*c*/cam = ...``) would otherwise FALSE-ADMIT a non-camera binding as a
# camera seed -> a bogus RigRootedRetargetFact the verifier does NOT catch (ships a
# wrong retarget). The UNSAFE direction. Legit bare / ``this.`` seeds still admit.


def test_f1_member_seed_newline_before_sym_rejected(tmp_path: Path) -> None:
    # ``other.\ncam = Camera.main.transform`` — a foreign member write split by a
    # NEWLINE. The ``.`` still precedes ``cam`` -> NOT a bare-cam seed -> abstain.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Other other;\n"
        "  void Awake() {\n"
        "    other.\n"
        "cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


def test_f1_member_seed_block_comment_before_sym_rejected(tmp_path: Path) -> None:
    # ``other./*c*/cam = Camera.main.transform`` — a foreign member write split by an
    # inline BLOCK comment. The ``.`` still precedes ``cam`` -> abstain.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Other other;\n"
        "  void Awake() {\n"
        "    other./*c*/cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


def test_f1_member_seed_line_comment_and_newline_rejected(tmp_path: Path) -> None:
    # ``other. // c\n cam = ...`` — a foreign member write split by a LINE comment +
    # newline + indentation. Still abstains.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Other other;\n"
        "  void Awake() {\n"
        "    other. // pick from the other object\n"
        "    cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


def test_f1_member_seed_tab_newline_before_sym_rejected(tmp_path: Path) -> None:
    # ``other.\n\tcam = ...`` — tabs + newline trivia between the ``.`` and ``cam``.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot; Other other;\n"
        "  void Awake() {\n"
        "    other.\n"
        "\tcam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == ()
    assert entry.resolved_total == 0


def test_f1_this_dot_block_comment_seed_still_admits(tmp_path: Path) -> None:
    # ``this./*c*/cam = Camera.main.transform`` is a SAME-OBJECT field seed split by a
    # comment — it must still ADMIT (trivia-skipping must not over-reject the legit
    # ``this.``-qualified seed).
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    this./*c*/cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot", cam_receiver="cam"),
    )
    assert entry.resolved_total == 1


def test_f1_this_spaced_dot_seed_still_admits(tmp_path: Path) -> None:
    # ``this . cam = Camera.main.transform`` — spaces around the ``.`` of a legit
    # ``this.``-qualified seed must still admit.
    src = (
        "public class Player : MonoBehaviour {\n"
        "  Transform cam; public Transform weaponSlot;\n"
        "  void Awake() {\n"
        "    this . cam = Camera.main.transform;\n"
        "    weaponSlot = cam.GetChild(0);\n"
        "  }\n}\n"
    )
    entry = _build(tmp_path, src, _fps_library())
    assert entry is not None
    assert entry.rig_facts == (
        RigRootedRetargetFact(
            field_name="weaponSlot", child_name="WeaponSlot", cam_receiver="cam"),
    )
    assert entry.resolved_total == 1


def test_f1_seed_lhs_helper_unit_trivia_forms() -> None:
    # Direct unit on ``_seed_lhs_is_bare_or_this`` over the trivia matrix: the symbol
    # ``cam`` (start located by find) must REJECT every foreign-member trivia form and
    # ADMIT every bare / ``this.`` form.
    from converter.child_ref_resolver import _seed_lhs_is_bare_or_this

    def admits(src: str) -> bool:
        return _seed_lhs_is_bare_or_this(src, src.index("cam ="))

    # Foreign member LHS, trivia-split -> REJECT.
    assert admits("other.\ncam = x") is False
    assert admits("other./*c*/cam = x") is False
    assert admits("other. // c\n cam = x") is False
    assert admits("other.\n\tcam = x") is False
    assert admits("a.b.cam = x") is False
    assert admits("foo.this.cam = x") is False  # this is a member tail of foo
    # Bare / this-qualified -> ADMIT.
    assert admits("cam = x") is True
    assert admits("this.cam = x") is True
    assert admits("this . cam = x") is True
    assert admits("this./*c*/cam = x") is True
    assert admits("this.\ncam = x") is True


# === r4 fix-round FINDING 2 — no-mutual-mask safety property ==================
# The lowering's ``_binding_discharged`` is a BEST-EFFORT HINT; the slice-1.2
# verifier's ``_rig_binding_discharged`` (run on the final output) is the SOLE
# authority. Every lowering<->verifier disagreement must be the FAIL-CLOSED-SAFE
# direction (lowering=discharged / verifier=fires, i.e. verifier STRICTER), NEVER
# the UNSAFE mutual-mask (BOTH discharged while the field is actually read). When the
# verifier predicate is present (phase integration) we cross-check both scanners over
# a boundary-form matrix; on this slice branch the verifier r3 predicate is not yet
# present, so we SKIP that cross-check and instead assert the lowering's OWN
# conservatism property: on every boundary form the lowering is at-least-as-lenient
# (never stricter than) a surviving-read, so it can never be the masking party.

_BOUNDARY_FORMS: tuple[tuple[str, str], ...] = (
    # (label, GetRifle body) — boundary forms the dot-form reroute cannot rewrite.
    ("bracket_string_key", '    return pivotOf(self["weaponSlot"])\n'),
    ("dynamic_bracket", '    local k = "weaponSlot"\n    return pivotOf(self[(k)])\n'),
    ("non_self_alias", "    local p = self\n    return pivotOf(p.weaponSlot)\n"),
    ("module_table", "    return pivotOf(Player.weaponSlot)\n"),
    ("self_int_index", "    return pivotOf(self[1])\n"),  # array index, NOT field
    ("concat_bracket_key", '    return pivotOf(self["weapon" .. "Slot"])\n'),
)


def _boundary_src(get_rifle_body: str) -> str:
    return (
        "function Player:Awake()\n"
        "    self.cam = workspace.CurrentCamera\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n"
        "end\n\n"
        "function Player:GetRifle()\n"
        f"{get_rifle_body}"
        "end\n\n"
        "return Player\n"
    )


def test_f2_no_mutual_mask_against_verifier_or_conservatism() -> None:
    # SAFETY CHECK: there is NO case where the lowering stamps present=True AND the
    # verifier ALSO returns discharged while the field is ACTUALLY read (mutual-mask).
    # Cross-check the real verifier when it is importable; otherwise assert the
    # lowering's own conservatism (it is at-least-as-lenient, so never the masker).
    from converter.rifle_rig_retarget_lowering import (
        _binding_discharged,
        _has_surviving_field_read,
    )
    try:
        from converter.contract_verifier import (  # type: ignore[attr-defined]
            _rig_binding_discharged as _verifier_discharged,
        )
        have_verifier = True
    except ImportError:
        _verifier_discharged = None  # noqa: N806
        have_verifier = False

    for label, body in _BOUNDARY_FORMS:
        src = _boundary_src(body)
        s = _Script(src)
        lower_rifle_rig_retarget([s], _rig_map())
        final = s.luau_source
        lowering_present = bool((s.rig_binding or {}).get("present"))
        # Is there a real surviving CONSUMER read of the field in the final source
        # (a bare ``self.<field>`` read in a yield-safe method)?  ``self[...]`` /
        # ``p.weaponSlot`` / ``Player.weaponSlot`` are not bare-self consumer reads,
        # so this is the residual the dot-form reroute leaves.
        residual_self_read = _has_surviving_field_read(final, "weaponSlot")
        if have_verifier and _verifier_discharged is not None:
            verifier_present = bool(
                _verifier_discharged(final, "weaponSlot", "WeaponSlot")
            )
            # Each _BOUNDARY_FORMS case carries a real surviving read of the field.
            # The only UNSAFE state is a MUTUAL-MASK: BOTH the lowering AND the
            # verifier report discharged while the field is actually read. A
            # lowering-lenient / verifier-fires desync is fail-closed-safe by design
            # (the verifier is the SOLE discharge authority, design §1.6/FIX 1) and
            # is explicitly allowed — so we assert ONLY the absence of mutual-mask.
            assert not (lowering_present and verifier_present), (
                f"MUTUAL-MASK on {label}: both the lowering and the verifier report "
                "discharged while the field is read"
            )
        else:
            # No verifier on this branch (lands in slice 1.2; parity asserted at
            # phase integration). Conservatism property: the lowering ABSTAINS
            # (present=False) on every boundary form -> it can never be the masking
            # party. If it ever stamps present=True, there must be NO surviving bare
            # self read (else it masked one).
            if lowering_present:
                assert not residual_self_read, (
                    f"{label}: lowering present=True while a bare self read survives "
                    "-> mutual-mask risk"
                )
            else:
                # The fail-closed-safe direction: lowering abstains, source unedited.
                assert "_resolveWeaponSlot" not in final, label
    # Note: when the verifier r3 predicate is absent (this slice branch), the
    # cross-scanner parity lands at phase integration (slice 1.2); the lowering's
    # no-mutual-mask conservatism asserted above is the safety property for now.
