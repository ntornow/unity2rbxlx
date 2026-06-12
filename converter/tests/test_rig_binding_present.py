"""S1b — binding-present FAIL-CLOSED verifier (``_check_rig_binding_present``).

The post-transpile rifle rig-retarget lowering rides on AI output the AI is NOT
trusted to preserve, so a DROPPED/reshaped/reverted binding must fail LOUD. The
verifier is the BINDING floor: it reads the ``rig_binding`` carrier ONLY for the
deterministic IR anchor (``field``/``child``), then INDEPENDENTLY scans the final
``script.source`` to confirm the binding landed — it does NOT trust the lowering's
``present`` self-stamp as the gate.

Acceptance (design-phase1 §4 (e.i-e.vi)):
  e.i  RED on a source MISSING the discharged binding -> promoted via fail_closed.
  e.ii GREEN only when the source CONTAINS the discharged binding AND present=True.
  e.iii ABSTAIN on rig_binding=None.
  e.iv INDEPENDENCE / mis-stamp: present=True BUT source absent -> STILL fires
       (the scan, not the stamp, decides).
  e.v  pre-fix-RED proof: fails against verifier code WITHOUT the check.
  e.vi fact-anchored, not arbitrary grep: keys on field/child; abstains/behaves
       for an unrelated script; does not fire on a stray FindFirstChild string.

The GREEN/discharged sources are produced by running S1's REAL lowering on the
on-corpus shape, so the verifier's independent scan is tested against what the
lowering ACTUALLY emits, not a synthetic guess.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from converter import contract_verifier  # noqa: E402
from converter.child_ref_resolver import (  # noqa: E402
    ChildRefScript,
    RigRootedRetargetFact,
)
from converter.contract_verifier import (  # noqa: E402
    FAIL_CLOSED_CHECKS,
    _check_rig_binding_present,
    _rig_binding_discharged,
    _rig_has_surviving_ordinal_write,
    fail_closed_errors,
    verify_contract,
)
from converter.rifle_rig_retarget_lowering import lower_rifle_rig_retarget  # noqa: E402
from core.roblox_types import RbxScript  # noqa: E402

# A non-empty topology so the smoke check stays quiet.
_TOPOLOGY = {"modules": {"Player": {"stem": "Player"}}}


# The on-corpus AI-output shape: Awake camera-child write + GetRifle reads +
# trailing ``return Player``. ``cam`` is the bare symbol seeded from
# ``Camera.main.transform`` (matching the real corpus admission).
_AI_OUTPUT_SHAPE = """local Player = {}
Player.__index = Player

function Player.new()
    local self = setmetatable({}, Player)
    return self
end

function Player:Awake()
    self.cam = workspace.CurrentCamera
    self.weaponSlot = self.cam and self.cam:GetChildren()[1]
end

function Player:GetRifle()
    local rifle = self.host.instantiatePrefab(self.riflePrefab, self.weaponSlot, pivotOf(self.weaponSlot))
    if self.weaponSlot then rifle:PivotTo(pivotOf(self.weaponSlot)) end
    return rifle
end

return Player
"""


class _Script:
    """Minimal ``_HasLuauSourceAndPath`` for driving the real lowering."""

    def __init__(self, source: str, source_path: str = "/proj/Player.cs") -> None:
        self.luau_source = source
        self.source_path = source_path
        self.rig_binding: dict[str, object] | None = None


def _lower(
    source: str = _AI_OUTPUT_SHAPE,
    *,
    field: str = "weaponSlot",
    child: str = "WeaponSlot",
    cam_receiver: str = "cam",
    source_path: str = "/proj/Player.cs",
) -> _Script:
    """Run S1's REAL lowering over ``source`` and return the edited script object
    (carrying the lowered ``luau_source`` + the stamped ``rig_binding``)."""
    s = _Script(source, source_path)
    fact = RigRootedRetargetFact(
        field_name=field, child_name=child, cam_receiver=cam_receiver
    )
    crs = ChildRefScript(
        facts=(), getchild_total=1, resolved_total=1, rig_facts=(fact,)
    )
    lower_rifle_rig_retarget([s], {source_path: crs})
    return s


def _rbx(name: str, source: str, rig_binding: dict[str, object] | None) -> RbxScript:
    return RbxScript(name=name, source=source, rig_binding=rig_binding)


def _rig_rows(scripts: list[RbxScript]) -> list:
    res = verify_contract(_TOPOLOGY, scripts)
    return [v for v in res.violations if v.check == "rig_binding_present"]


# Sanity: the real lowering discharges the on-corpus shape.
def test_real_lowering_discharges_corpus_shape() -> None:
    s = _lower()
    assert s.rig_binding == {
        "field": "weaponSlot",
        "child": "WeaponSlot",
        "present": True,
    }
    assert "function Player:_resolveWeaponSlot()" in s.luau_source
    assert "self:_resolveWeaponSlot()" in s.luau_source
    assert "self.weaponSlot = nil" in s.luau_source


def test_rig_binding_present_in_fail_closed_set() -> None:
    assert "rig_binding_present" in FAIL_CLOSED_CHECKS


# ---------------------------------------------------------------------------
# e.ii — GREEN only when source CONTAINS the discharged binding AND present=True
# ---------------------------------------------------------------------------
def test_e2_green_when_discharged_and_stamped() -> None:
    lowered = _lower()
    script = _rbx("Player", lowered.luau_source, lowered.rig_binding)
    rows = _rig_rows([script])
    assert rows == []
    # And nothing promotes.
    res = verify_contract(_TOPOLOGY, [script])
    assert not any("rig_binding_present" in e for e in fail_closed_errors(res))


# ---------------------------------------------------------------------------
# e.i — RED on a source MISSING the discharged binding -> promoted via fail_closed
# ---------------------------------------------------------------------------
def test_e1_red_when_binding_absent_and_promotes() -> None:
    # The AI shape with NO lowering applied: bare reads survive, ordinal write
    # present, no resolver method. The carrier is the honest present=False stamp
    # the lowering would emit on an abstained discharge.
    script = _rbx(
        "Player",
        _AI_OUTPUT_SHAPE,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": False},
    )
    rows = _rig_rows([script])
    assert len(rows) == 1
    assert rows[0].severity == "warning"
    res = verify_contract(_TOPOLOGY, [script])
    errs = fail_closed_errors(res)
    assert any("rig_binding_present" in e for e in errs)
    assert any("Player" in e for e in errs)


def test_e1_red_even_when_stamp_true_but_source_undischarged() -> None:
    # Belt-and-suspenders for e.i: a dropped binding with a (wrong) present=True
    # stamp still reds (covered more directly by e.iv below, but proves the RED
    # path does not depend on the stamp being False).
    script = _rbx(
        "Player",
        _AI_OUTPUT_SHAPE,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


# ---------------------------------------------------------------------------
# e.iii — ABSTAIN on rig_binding=None
# ---------------------------------------------------------------------------
def test_e3_abstain_on_none_carrier() -> None:
    # A non-rifle script whose source even happens to read self.weaponSlot: with
    # no carrier there is NO obligation, so the check abstains.
    script = _rbx("OtherScript", _AI_OUTPUT_SHAPE, None)
    assert _rig_rows([script]) == []


def test_e3_abstain_on_empty_carrier() -> None:
    # An empty dict is falsy -> abstain (same guard as None).
    script = _rbx("Player", _AI_OUTPUT_SHAPE, {})
    assert _rig_rows([script]) == []


# ---------------------------------------------------------------------------
# e.iv — INDEPENDENCE / mis-stamp: present=True BUT source absent -> STILL FIRES
# (the load-bearing test for FIX 1 — the scan, not the stamp, decides)
# ---------------------------------------------------------------------------
def test_e4_independence_forged_stamp_true_but_source_undischarged() -> None:
    # The forged/stale-resume case: the carrier claims present=True, but the
    # source NEVER got the lowering's edits (e.g. a syntax-revert reverted them,
    # or a stale carrier rode in on a preserve/resume assemble where the lowering
    # never re-ran). The INDEPENDENT scan returns False -> the check STILL fires.
    forged = {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    script = _rbx("Player", _AI_OUTPUT_SHAPE, forged)
    rows = _rig_rows([script])
    assert len(rows) == 1
    assert rows[0].severity == "warning"
    # The detail names the stamp/scan disagreement.
    assert "discharged=False" in rows[0].detail
    assert "lowering-stamp=True" in rows[0].detail


def test_e4_independence_stamp_false_but_source_discharged_also_fires() -> None:
    # The OTHER mis-stamp direction: the source IS discharged but the carrier says
    # present=False (a mis-stamp). PASS requires discharged AND stamp, so a
    # disagreement in EITHER direction fails — this surfaces the carrier bug.
    lowered = _lower()
    script = _rbx(
        "Player",
        lowered.luau_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": False},
    )
    rows = _rig_rows([script])
    assert len(rows) == 1
    assert "discharged=True" in rows[0].detail
    assert "lowering-stamp=False" in rows[0].detail


# ---------------------------------------------------------------------------
# e.vi — fact-anchored, not arbitrary grep
# ---------------------------------------------------------------------------
def test_e6_does_not_fire_on_unrelated_findfirstchild_string() -> None:
    # A source that contains a ``FindFirstChild("WeaponSlot")`` string in an
    # UNRELATED place, but NO resolver method / NO rewritten reads / and the
    # field's binding never landed. A naive grep would false-PASS; the
    # fact-anchored scan correctly says NOT discharged -> the check FIRES (because
    # the carrier obligated a binding that did not land).
    unrelated = (
        "function Player:Decorate()\n"
        '    local x = somethingElse:FindFirstChild("WeaponSlot")\n'
        "    return x\n"
        "end\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n"
        "return Player\n"
    )
    # Scan-level: the stray string does NOT discharge.
    assert _rig_binding_discharged(unrelated, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player",
        unrelated,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


def test_e6_keys_on_field_child_unrelated_script_abstains() -> None:
    # A genuinely unrelated script (no rig fact -> no carrier) NEVER reds, even if
    # its source mentions resolver-ish tokens — the anchor is the carrier, not the
    # presence of any token in any script.
    other = _rbx(
        "Inventory",
        "function Inventory:_resolveWeaponSlot() return nil end\nreturn Inventory\n",
        None,
    )
    assert _rig_rows([other]) == []


def test_e6_anchored_on_a_different_field_child() -> None:
    # The same discharged-shape mechanism for a DIFFERENT field/child resolves
    # green when the carrier's field/child match the source, and reds when they
    # don't — proving the scan keys on the carrier's field/child, not a hardcoded
    # weaponSlot/WeaponSlot.
    src = _lower(
        source=_AI_OUTPUT_SHAPE.replace("weaponSlot", "holster").replace(
            "WeaponSlot", "Holster"
        ),
        field="holster",
        child="Holster",
    )
    green = _rbx("Player", src.luau_source, src.rig_binding)
    assert _rig_rows([green]) == []
    # Carrier anchored on the WRONG field for this source -> not discharged -> red.
    wrong = _rbx(
        "Player",
        src.luau_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([wrong])) == 1


# ---------------------------------------------------------------------------
# e.v — pre-fix-RED proof: the test FAILS against verifier code WITHOUT the check
# (prove the check itself is load-bearing — a dropped binding sails through when
# absent). We simulate "pre-fix code" by stubbing out the check.
# ---------------------------------------------------------------------------
def test_e5_pre_fix_red_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dropped-binding scenario that MUST fail closed.
    script = _rbx(
        "Player",
        _AI_OUTPUT_SHAPE,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": False},
    )

    # WITH the check (current code): the dropped binding is caught.
    errs_with = fail_closed_errors(verify_contract(_TOPOLOGY, [script]))
    assert any("rig_binding_present" in e for e in errs_with), (
        "the check must catch the dropped binding"
    )

    # WITHOUT the check (pre-fix code): emulate the verifier as it was BEFORE this
    # slice — make _check_rig_binding_present a no-op. The dropped binding now
    # sails through (zero rig_binding_present rows). This is the pre-fix-RED proof:
    # the assertion above would FAIL against this no-op, so the check is a real
    # guard, not a tautology.
    monkeypatch.setattr(
        contract_verifier, "_check_rig_binding_present", lambda *a, **k: []
    )
    errs_without = fail_closed_errors(verify_contract(_TOPOLOGY, [script]))
    assert not any("rig_binding_present" in e for e in errs_without), (
        "pre-fix code (check absent) must NOT catch the dropped binding — "
        "proving the check is load-bearing"
    )


def test_e5_check_directly_returns_rows_on_dropped_binding() -> None:
    # Call the check directly (the unit the pre-fix proof brackets): a dropped
    # binding -> exactly one warning row.
    script = _rbx(
        "Player",
        _AI_OUTPUT_SHAPE,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": False},
    )
    rows = _check_rig_binding_present(_TOPOLOGY, [script])
    assert len(rows) == 1
    assert rows[0].check == "rig_binding_present"
    assert rows[0].severity == "warning"


# ---------------------------------------------------------------------------
# Multi-fact overflow carrier (present=False, multi_fact=True) -> fail-closed.
# ---------------------------------------------------------------------------
def test_multi_fact_overflow_carrier_fails_closed() -> None:
    # S1 stamps a {present:False, multi_fact:True} overflow carrier on a
    # >1-rig-fact script. The verifier reads field/child + present=False -> reds.
    script = _rbx(
        "Player",
        _AI_OUTPUT_SHAPE,
        {
            "field": "weaponSlot",
            "child": "WeaponSlot",
            "present": False,
            "multi_fact": True,
        },
    )
    assert len(_rig_rows([script])) == 1


# ---------------------------------------------------------------------------
# Idempotency / ordering: the binding floor reports BEFORE check D (FIX 5).
# ---------------------------------------------------------------------------
def test_fix5_binding_floor_ordered_before_child_ordinal() -> None:
    # A dropped binding leaves BOTH a missing binding AND (in the worst case) a
    # surviving ordinal. The rig_binding_present row must appear BEFORE the
    # child_ordinal rows in the violations list.
    script = RbxScript(
        name="Player",
        source=_AI_OUTPUT_SHAPE,
        rig_binding={"field": "weaponSlot", "child": "WeaponSlot", "present": False},
        child_ref_resolution={"getchild_total": 1, "resolved_total": 1},
    )
    res = verify_contract(_TOPOLOGY, [script])
    checks = [v.check for v in res.violations]
    rig_idx = checks.index("rig_binding_present")
    ord_rows = [
        i for i, c in enumerate(checks)
        if c in ("child_ordinal_survivor", "child_ordinal_coverage_gap")
    ]
    if ord_rows:
        assert rig_idx < min(ord_rows)


# ---------------------------------------------------------------------------
# Dual-voice REVIEW findings (round 1) — 3 false-NEGATIVES that let the verifier
# PASS when it must FAIL. Each is RED against 4a5aa84 (the pre-fix verifier).
# ---------------------------------------------------------------------------
import re  # noqa: E402


def test_review_f1_foreign_resolver_stub_does_not_discharge() -> None:
    """BLOCKING #1 — discharge must require the resolver method BODY to be the rig
    resolver, not merely a same-named method. A FOREIGN stub (``return nil``) with
    the discharged-looking call sites + a forged ``present=True`` must FAIL closed
    (the stale/forged-resume threat model)."""
    green = _lower()
    # Replace ONLY the resolver method body with a foreign stub; keep the name, the
    # rewritten call sites, and the neutralized write intact.
    foreign_source = re.sub(
        r"function Player:_resolveWeaponSlot\(\).*?\nend\n",
        "function Player:_resolveWeaponSlot()\n    return nil\nend\n",
        green.luau_source,
        flags=re.S,
    )
    assert "function Player:_resolveWeaponSlot()" in foreign_source
    assert "self:_resolveWeaponSlot()" in foreign_source  # call sites still present
    assert 'GetAttribute("_MainCameraRig")' not in foreign_source  # body is foreign
    # Scan-level: a foreign stub is NOT a discharge (pre-fix returned True = BUG).
    assert _rig_binding_discharged(foreign_source, "weaponSlot", "WeaponSlot") is False
    # Carrier forges present=True; the independent scan must STILL fire (fail-closed).
    script = _rbx(
        "Player",
        foreign_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


def test_review_f2_multiline_surviving_ordinal_write_is_detected() -> None:
    """BLOCKING #2 (both voices) — the surviving-ordinal-write scan must be
    continuation-aware. A multiline surviving camera-child write
    (``self.weaponSlot =\\n  self.cam:GetChildren()[1]``) re-clobbers the field at
    runtime, so it must be DETECTED -> NOT discharged -> the check fires."""
    green = _lower()
    # Inject a SURVIVING multiline camera-child ordinal write into the yielding
    # GetRifle (a re-clobber the lowering's one-write neutralize missed).
    multiline_source = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot =\n        self.cam:GetChildren()[1]\n    return rifle\nend",
    )
    assert "self.cam:GetChildren()[1]" in multiline_source
    # Scan-level: the multiline write IS detected (pre-fix missed it -> True = BUG).
    assert _rig_has_surviving_ordinal_write(multiline_source, "weaponSlot") is True
    assert _rig_binding_discharged(multiline_source, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player",
        multiline_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


def test_review_f3_resolver_only_in_long_bracket_does_not_discharge() -> None:
    """BLOCKING #3 — a ``_resolve<suffix>`` method/call appearing ONLY inside a Luau
    long-bracket literal (``[[ ]]``) must NOT count as live code -> NOT discharged.
    The real code has a surviving bare read + no real resolver, so a binding that
    only LOOKS discharged inside a string literal must fail closed."""
    long_bracket_source = (
        "local DOC = [[\n"
        "function Player:_resolveWeaponSlot()\n"
        '    return rig:FindFirstChild("WeaponSlot", true)\n'
        "end\n"
        "local x = self:_resolveWeaponSlot()\n"
        "]]\n"
        "function Player:Awake()\n"
        "    self.weaponSlot = self.cam and self.cam:GetChildren()[1]\n"
        "end\n"
        "function Player:GetRifle()\n"
        "    return pivotOf(self.weaponSlot)\n"
        "end\n"
        "return Player\n"
    )
    # The only resolver decl/call live inside the [[ ]] literal -> not live code ->
    # the binding is genuinely absent in real code -> not discharged.
    assert (
        _rig_binding_discharged(long_bracket_source, "weaponSlot", "WeaponSlot")
        is False
    )
    script = _rbx(
        "Player",
        long_bracket_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1
