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
    _RigDeadWriteExempt,
    _check_rig_binding_present,
    _check_surviving_child_ordinal,
    _count_surviving_child_ordinals,
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


def _carrier(
    *,
    field: str = "weaponSlot",
    child: str = "WeaponSlot",
    present: bool = True,
    cam_receiver: str = "cam",
    cam_ordinal: int = 0,
) -> dict[str, object]:
    """The 5-key ``rig_binding`` carrier shape slice 1.1 now stamps (field/child/
    present + the REDESIGN r3 ``cam_receiver``/``cam_ordinal`` anchors)."""
    return {
        "field": field,
        "child": child,
        "present": present,
        "cam_receiver": cam_receiver,
        "cam_ordinal": cam_ordinal,
    }


# The credited dead-write exemption spec for the on-corpus shape: field
# ``weaponSlot``, receiver ``self.cam`` (``cam_receiver="cam"``), ordinal
# ``GetChildren()[1]`` (``cam_ordinal=0``, +1 -> 1).
_CORPUS_EXEMPT = _RigDeadWriteExempt(
    field_name="weaponSlot", cam_receiver="cam", cam_ordinal=0
)


def _rig_rows(scripts: list[RbxScript]) -> list:
    res = verify_contract(_TOPOLOGY, scripts)
    return [v for v in res.violations if v.check == "rig_binding_present"]


# Sanity: the real lowering discharges the on-corpus shape.
def test_real_lowering_discharges_corpus_shape() -> None:
    s = _lower()
    assert s.rig_binding == _carrier()
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


def test_review_f2_multiline_surviving_ordinal_write_secondary_diagnostic() -> None:
    """SECONDARY DIAGNOSTIC (Path A re-anchor) — the surviving-ordinal-write scan
    stays continuation-aware (kept machinery), but under Path A a surviving
    init-WRITE no longer fails DISCHARGE: with the reads rerouted, the leftover
    write is dead data (nothing reads ``self.weaponSlot``). The scan still DETECTS
    the multiline write (it is the demoted secondary diagnostic), but
    ``_rig_binding_discharged`` is now True (the read reroute is the load-bearing
    gate, not the write shape)."""
    green = _lower()
    # Inject a SURVIVING multiline camera-child ordinal write into the yielding
    # GetRifle (a leftover the lowering's one-write neutralize skipped). It is an
    # LHS write of the field, so it does NOT survive as a raw READ.
    multiline_source = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot =\n        self.cam:GetChildren()[1]\n    return rifle\nend",
    )
    assert "self.cam:GetChildren()[1]" in multiline_source
    # Secondary diagnostic still detects the surviving write (kept machinery).
    assert _rig_has_surviving_ordinal_write(multiline_source, "weaponSlot") is True
    # Path A: discharge keys on the READ reroute — a dead write does NOT block it.
    assert _rig_binding_discharged(multiline_source, "weaponSlot", "WeaponSlot") is True
    script = _rbx(
        "Player",
        multiline_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert _rig_rows([script]) == []


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


# ---------------------------------------------------------------------------
# Dual-voice REVIEW finding (round 2) — POSTFIX-continuation class. The round-1
# continuation scan admitted operator-led + ``:``-led heads but NOT the ``[``
# index head, so a surviving write split BEFORE the index leaked past the RHS
# span. The fix closes the whole postfix class (``[``/``(``/``.``/``:``). Each
# split case is RED against 1ca1cbb (the round-1 verifier).
# ---------------------------------------------------------------------------
def test_review_r2_split_before_index_surviving_write_secondary_diagnostic() -> None:
    """ROUND-2 (now SECONDARY DIAGNOSTIC under Path A) — the continuation-aware RHS
    span still reaches a write split BEFORE the ``[`` index
    (``self.weaponSlot = self.cam:GetChildren()\\n    [1]``), so the secondary
    diagnostic DETECTS it (kept machinery). But Path A drops the write from the
    discharge gate: with the reads rerouted, the leftover write is dead -> the
    binding STILL discharges True."""
    green = _lower()
    split_source = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot = self.cam:GetChildren()\n        [1]\n    return rifle\nend",
    )
    assert "self.cam:GetChildren()\n        [1]" in split_source
    # Secondary diagnostic still detects the split-before-index write.
    assert _rig_has_surviving_ordinal_write(split_source, "weaponSlot") is True
    # Path A: a dead leftover write does not block discharge.
    assert _rig_binding_discharged(split_source, "weaponSlot", "WeaponSlot") is True
    script = _rbx(
        "Player",
        split_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert _rig_rows([script]) == []


def test_review_r2_split_before_getchild_call_secondary_diagnostic() -> None:
    """ROUND-2 (secondary diagnostic) — the call-continuation head (``(``): the scan
    still spans a write split BEFORE the ``GetChild(`` call args; Path A keeps it as
    the demoted diagnostic, discharge stays True (dead write)."""
    green = _lower()
    split_source = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot = self.cam:GetChild\n        (1)\n    return rifle\nend",
    )
    assert _rig_has_surviving_ordinal_write(split_source, "weaponSlot") is True
    assert _rig_binding_discharged(split_source, "weaponSlot", "WeaponSlot") is True
    script = _rbx(
        "Player",
        split_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert _rig_rows([script]) == []


def test_review_r2_split_before_member_getchildren_secondary_diagnostic() -> None:
    """ROUND-2 (secondary diagnostic) — the member-continuation head (``.``): the
    scan still spans a write split BEFORE a ``.GetChild`` member form. Path A keeps
    the scan as the demoted diagnostic; discharge stays True (dead write)."""
    green = _lower()
    split_source = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot = self.cam\n        .GetChild(1)\n    return rifle\nend",
    )
    assert _rig_has_surviving_ordinal_write(split_source, "weaponSlot") is True
    assert _rig_binding_discharged(split_source, "weaponSlot", "WeaponSlot") is True


def test_review_r2_over_reach_guard_does_not_swallow_following_statement() -> None:
    """ROUND-2 over-reach guard — admitting ``(``/``[`` as continuation heads must
    NOT reach across a FOLLOWING statement. A clean (already-neutralized) write
    followed by an unrelated statement that begins a new ``self.<field> =`` /
    ``local`` must STILL discharge: the boundary check stops the span at the new
    statement so its (hypothetical) ordinal text does not leak into the RHS span."""
    # The real discharged shape: write is ``self.weaponSlot = nil``. Inject a
    # following statement that begins with ``local`` AND happens to carry an
    # ordinal access — the span must stop at ``local``, not swallow it.
    green = _lower()
    over_reach_source = green.luau_source.replace(
        "self.weaponSlot = nil",
        "self.weaponSlot = nil\n    local other = somethingElse:GetChildren()[1]",
    )
    # The neutralized write does NOT pick up the following ``local`` ordinal -> the
    # binding STILL discharges (the over-reach guard held).
    assert _rig_has_surviving_ordinal_write(over_reach_source, "weaponSlot") is False
    assert _rig_binding_discharged(over_reach_source, "weaponSlot", "WeaponSlot") is True


# ---------------------------------------------------------------------------
# Dual-voice REVIEW finding (round 3) — the surviving-ordinal-write scan's manual
# inline whitespace/comment/continuation handling had a long formatting tail.
# Round 3's STRUCTURAL fix runs the detection over a CODE PROJECTION of the source
# (comment/string interiors blanked, position-preserving) with the spanned RHS
# whitespace-collapsed before the canonical tail match — so the whole formatting
# class collapses to ``self.<field> = <recv>:GetChildren()[n]`` at once. Each case
# below is RED against f4f1e63 (the round-2 verifier) and the corpus happy-path
# still discharges (the legit resolver body must NOT look like a surviving write).
# ---------------------------------------------------------------------------
def test_review_r3_whitespace_at_colon_method_junction_is_detected() -> None:
    """ROUND-3 — whitespace at the receiver -> ``:`` -> method junction
    (``self.cam : GetChildren ( ) [ 1 ]``) must still be detected as a surviving
    ordinal write. The round-2 tail regex had no ``\\s*`` between ``:`` and the
    method name; the projection's whitespace-collapse closes the whole class."""
    green = _lower()
    spaced = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot = self.cam : GetChildren ( ) [ 1 ]\n    return rifle\nend",
    )
    assert _rig_has_surviving_ordinal_write(spaced, "weaponSlot") is True
    # Path A: the surviving write is a dead leftover -> discharge stays True.
    assert _rig_binding_discharged(spaced, "weaponSlot", "WeaponSlot") is True
    script = _rbx(
        "Player", spaced, {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    )
    assert _rig_rows([script]) == []


def test_review_r3_whitespace_before_method_getchild_is_detected() -> None:
    """ROUND-3 — the ``:`` -> ``GetChild`` junction whitespace variant
    (``self.cam: GetChild(1)``) is also detected (secondary diagnostic)."""
    green = _lower()
    spaced = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot = self.cam: GetChild(1)\n    return rifle\nend",
    )
    assert _rig_has_surviving_ordinal_write(spaced, "weaponSlot") is True
    assert _rig_binding_discharged(spaced, "weaponSlot", "WeaponSlot") is True


def test_review_r3_comment_after_call_before_index_is_detected() -> None:
    """ROUND-3 — an intra-statement ``--`` comment between the call and its postfix
    index (``self.cam:GetChildren() -- gap\\n  [1]``) must NOT truncate the RHS span:
    the projection blanks the comment, so the span reaches ``[1]`` and the write is
    detected. Round-2 broke the span at ``--`` and missed it (fail-open)."""
    green = _lower()
    commented = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot = self.cam:GetChildren() -- gap\n        [1]\n    return rifle\nend",
    )
    assert "-- gap" in commented
    assert _rig_has_surviving_ordinal_write(commented, "weaponSlot") is True
    # Path A: the surviving write is a dead leftover -> discharge stays True.
    assert _rig_binding_discharged(commented, "weaponSlot", "WeaponSlot") is True
    script = _rbx(
        "Player", commented,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert _rig_rows([script]) == []


def test_review_r3_comment_before_method_is_detected() -> None:
    """ROUND-3 — a ``--`` comment between the receiver and its method
    (``self.cam -- gap\\n  :GetChildren()[1]``) must not truncate the span either
    (secondary diagnostic)."""
    green = _lower()
    commented = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot = self.cam -- gap\n        :GetChildren()[1]\n    return rifle\nend",
    )
    assert _rig_has_surviving_ordinal_write(commented, "weaponSlot") is True
    assert _rig_binding_discharged(commented, "weaponSlot", "WeaponSlot") is True


def test_review_r3_crlf_line_ending_split_is_detected() -> None:
    """ROUND-3 — a ``\\r\\n`` (Windows) line ending at a postfix split
    (``self.cam:GetChildren()\\r\\n  [1]``) must be spanned and detected; the
    projection/collapse treats ``\\r`` as inter-token whitespace."""
    green = _lower()
    crlf = green.luau_source.replace(
        "return rifle\nend",
        "self.weaponSlot = self.cam:GetChildren()\r\n        [1]\r\n    return rifle\nend",
    )
    assert "\r\n" in crlf
    assert _rig_has_surviving_ordinal_write(crlf, "weaponSlot") is True
    # Path A: the surviving write is a dead leftover -> discharge stays True.
    assert _rig_binding_discharged(crlf, "weaponSlot", "WeaponSlot") is True


def test_review_r3_ordinal_inside_string_does_not_false_detect() -> None:
    """ROUND-3 — the projection blanks STRING interiors, so an ordinal-shaped token
    inside a string literal assigned to the field
    (``self.weaponSlot = "self.cam:GetChildren()[1]"``) is NOT a surviving ordinal
    write (it is a harmless string) -> must NOT be detected."""
    green = _lower()
    stringy = green.luau_source.replace(
        "self.weaponSlot = nil",
        'self.weaponSlot = "self.cam:GetChildren()[1]"',
    )
    assert _rig_has_surviving_ordinal_write(stringy, "weaponSlot") is False


def test_review_r3_corpus_happy_path_still_discharges() -> None:
    """ROUND-3 REGRESSION GUARD — the structural projection must NOT make the legit
    resolver body (its internal ``:GetChildren()`` / ``FindFirstChild`` /
    ``:GetAttribute("_MainCameraRig")``) or the neutralized ``self.weaponSlot = nil``
    write look like a surviving ordinal write. The corpus happy-path discharges."""
    green = _lower()
    assert "self.weaponSlot = nil" in green.luau_source
    assert "function Player:_resolveWeaponSlot()" in green.luau_source
    assert _rig_has_surviving_ordinal_write(green.luau_source, "weaponSlot") is False
    assert _rig_binding_discharged(green.luau_source, "weaponSlot", "WeaponSlot") is True


# ---------------------------------------------------------------------------
# Dual-voice REVIEW finding (round 4) — METHOD-SPAN block-balance. The
# ``_rig_method_body_end`` scanner counted every ``then`` as a fresh opener but
# ``end``/``until`` as the only closers, so an ``if ... elseif ... elseif ... end``
# chain (multiple ``then``, ONE ``end``) over-counted openers and the span walked
# PAST the foreign stub's closing ``end`` into later unrelated code — sweeping a
# later decoy helper carrying the ``_MainCameraRig``/``FindFirstChild`` markers into
# the "method body", false-discharging a FOREIGN stub (REOPENS the round-1 foreign-
# stub closure). The fix mirrors S1's ``_structural_balance_ok``: ``elseif`` is a
# CLOSER that cancels its own upcoming ``then`` (net 0 for the chain); ``else`` is a
# pure +0. Each case below is RED against bb288b6 (the round-3 verifier).
# ---------------------------------------------------------------------------
def _foreign_stub_with_body(stub_body: str) -> str:
    """The real lowered Player output with ONLY the resolver method BODY replaced by
    ``stub_body`` (a foreign body, NO rig markers) AND a later DECOY helper carrying
    the ``_MainCameraRig`` + ``FindFirstChild("WeaponSlot", true)`` markers spliced
    before ``return Player``. The call sites + neutralized write stay intact, so the
    ONLY thing preventing discharge is the span correctly ending at the foreign
    stub's own ``end`` (not overrunning into the decoy)."""
    green = _lower()
    foreign = re.sub(
        r"function Player:_resolveWeaponSlot\(\).*?\nend\n",
        "function Player:_resolveWeaponSlot()\n" + stub_body + "end\n",
        green.luau_source,
        flags=re.S,
    )
    assert "function Player:_resolveWeaponSlot()" in foreign
    assert "self:_resolveWeaponSlot()" in foreign  # call sites intact
    assert "self.weaponSlot = nil" in foreign  # neutralized write intact
    decoy = (
        "function Player:DecoyHelper()\n"
        "    local rig\n"
        "    for _, m in workspace:GetDescendants() do\n"
        '        if m:GetAttribute("_MainCameraRig") then rig = m end\n'
        "    end\n"
        '    return rig and rig:FindFirstChild("WeaponSlot", true)\n'
        "end\n"
    )
    foreign = foreign.replace("return Player\n", decoy + "return Player\n")
    # The decoy carries the markers the stub body lacks — proving the markers found
    # by a span overrun would belong to the decoy, not the stub.
    assert 'GetAttribute("_MainCameraRig")' in foreign
    assert "DecoyHelper" in foreign
    return foreign


def test_review_r4_elseif_chain_stub_does_not_overrun_into_decoy() -> None:
    """ROUND-4 BLOCKING — a foreign ``_resolveWeaponSlot`` stub whose body is an
    ``if/elseif/elseif/end`` chain (3 ``then``, 1 ``end``) must NOT make the method-
    span scanner over-count openers and walk past its closing ``end`` into the later
    decoy helper. The span ends at the stub's own ``end`` -> the decoy's rig markers
    are NOT in the method body -> the foreign stub FAILS CLOSED (discharged=False).
    RED against bb288b6: the span overran, the decoy's markers were swept in, and
    the foreign stub false-discharged True."""
    foreign = _foreign_stub_with_body(
        "    local x = 1\n"
        "    if x == 1 then\n"
        "        return nil\n"
        "    elseif x == 2 then\n"
        "        return nil\n"
        "    elseif x == 3 then\n"
        "        return nil\n"
        "    end\n"
        "    return nil\n"
    )
    assert _rig_binding_discharged(foreign, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player",
        foreign,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


def test_review_r4_else_chain_stub_does_not_overrun_into_decoy() -> None:
    """ROUND-4 — the ``else`` variant (``if ... then ... else ... end``): ``else``
    follows no ``then``, so it is a pure +0 continuation. The single ``then`` + single
    ``end`` already balance, so the span ends at the stub's ``end``; the decoy is not
    swept in -> foreign stub fails closed."""
    foreign = _foreign_stub_with_body(
        "    local x = 1\n"
        "    if x == 1 then\n"
        "        return nil\n"
        "    else\n"
        "        return nil\n"
        "    end\n"
    )
    assert _rig_binding_discharged(foreign, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player",
        foreign,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


def test_review_r4_nested_if_for_while_function_stub_spans_correctly() -> None:
    """ROUND-4 — nested ``if``/``for``/``while``/``function`` blocks inside the
    foreign resolver body must each pair with their own ``end``, so the span ends at
    the resolver's OWN closing ``end`` (not early on a nested ``end``, not late past
    the file). A nested-block foreign stub still fails closed; the decoy is excluded."""
    foreign = _foreign_stub_with_body(
        "    local total = 0\n"
        "    for i = 1, 3 do\n"
        "        if i == 2 then\n"
        "            total = total + i\n"
        "        end\n"
        "    end\n"
        "    while total > 0 do\n"
        "        total = total - 1\n"
        "    end\n"
        "    local function helper()\n"
        "        return 0\n"
        "    end\n"
        "    return helper()\n"
    )
    assert _rig_binding_discharged(foreign, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player",
        foreign,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


def test_review_r4_repeat_until_stub_spans_correctly() -> None:
    """ROUND-4 — a ``repeat ... until`` block inside the foreign body
    (``repeat`` opens, ``until`` closes — NOT ``end``) must balance so the span ends
    at the resolver's own ``end``. Confirms the ``repeat``->``until`` pairing the
    method-span scanner relies on still holds alongside the ``elseif`` fix."""
    foreign = _foreign_stub_with_body(
        "    local n = 0\n"
        "    repeat\n"
        "        n = n + 1\n"
        "    until n >= 3\n"
        "    return nil\n"
    )
    assert _rig_binding_discharged(foreign, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player",
        foreign,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


def test_review_r4_corpus_with_real_resolver_body_still_discharges() -> None:
    """ROUND-4 REGRESSION GUARD — S1's REAL resolver body contains an ``if``/``for``
    + a ``for ... do ... end`` loop (multiple block keywords). The ``elseif`` fix
    must NOT shorten the span before the real resolver's internal markers: the span
    ends at the REAL resolver's closing ``end`` (recognizing its ``_MainCameraRig`` +
    ``FindFirstChild`` markers as live code inside the body) -> the corpus happy-path
    STILL discharges present=True."""
    green = _lower()
    assert "function Player:_resolveWeaponSlot()" in green.luau_source
    assert _rig_binding_discharged(green.luau_source, "weaponSlot", "WeaponSlot") is True
    script = _rbx("Player", green.luau_source, green.rig_binding)
    assert _rig_rows([script]) == []


# ===========================================================================
# PATH A re-anchor (D-S1b-PATHA) — discharge keys on the READ reroute (RHS-agnostic),
# NOT the write shape; + the FAIL-CLOSED BOUNDARY for forms the reroute cannot rewrite.
# ===========================================================================

# The 5 real AI write shapes for ``self.weaponSlot`` (design §3.4 / acceptance e.i):
# the lowering reroutes the GetRifle READS identically on ALL of them because the
# reroute keys on the AI-STABLE ``self.<field>`` member access, NOT the write RHS.
_WRITE_SHAPES = {
    "getchildren_ordinal": "self.weaponSlot = self.cam and self.cam:GetChildren()[1]",
    "gameobject": "self.weaponSlot = self.gameObject",
    "unitychild": "self.weaponSlot = self.cam and __unityChild(self.cam, 1)",
    "multistep_local": (
        "local children = self.cam:GetChildren()\n"
        "    self.weaponSlot = children[1]"
    ),
    "findfirstchild_fallback": (
        'self.weaponSlot = self.cam:FindFirstChild("WeaponSlot")'
    ),
}


@pytest.mark.parametrize("shape_name", sorted(_WRITE_SHAPES))
def test_pathA_discharges_on_all_five_real_write_shapes(shape_name: str) -> None:
    """e.i (Path A) — discharge is RHS-AGNOSTIC: across all 5 real write shapes the
    read reroute lands identically, ``_rig_binding_discharged`` is True, and the
    verifier is GREEN. The OLD write-anchored discharge abstained on shapes 2-5
    (the D-S2-REDESIGN failure); Path A discharges on all 5."""
    write = _WRITE_SHAPES[shape_name]
    src = _AI_OUTPUT_SHAPE.replace(
        "self.weaponSlot = self.cam and self.cam:GetChildren()[1]", write
    )
    lowered = _lower(source=src)
    # The reads were rerouted regardless of which write shape the AI emitted.
    assert "self:_resolveWeaponSlot()" in lowered.luau_source
    assert _rig_binding_discharged(
        lowered.luau_source, "weaponSlot", "WeaponSlot"
    ) is True
    # The 5-key carrier is stamped RHS-agnostically — cam_receiver/cam_ordinal ride
    # along on every write shape (the credited GetChild(0) on receiver ``cam``).
    assert lowered.rig_binding == _carrier()
    script = _rbx("Player", lowered.luau_source, lowered.rig_binding)
    assert _rig_rows([script]) == []


def test_pathA_tier2_skipped_write_but_reads_rerouted_discharges_true() -> None:
    """e.i / h.3 (Path A) — a script whose Tier-2 init-write neutralize was SKIPPED
    (the collapsed ``self.gameObject`` shape the recognizer does not match) but whose
    consumer READS are all rerouted MUST discharge True. The leftover write is dead
    data; discharge keys on the read reroute, NOT the write. (This DROPS the old
    'surviving ordinal write fails discharge' gate — the superseded clause.)"""
    src = _AI_OUTPUT_SHAPE.replace(
        "self.weaponSlot = self.cam and self.cam:GetChildren()[1]",
        "self.weaponSlot = self.gameObject",
    )
    lowered = _lower(source=src)
    # The init-write was NOT neutralized (no camera-child ordinal to recognize) ...
    assert "self.weaponSlot = self.gameObject" in lowered.luau_source
    # ... but discharge still holds because every consumer read was rerouted.
    assert _rig_binding_discharged(
        lowered.luau_source, "weaponSlot", "WeaponSlot"
    ) is True
    assert lowered.rig_binding["present"] is True
    assert _rig_rows([_rbx("Player", lowered.luau_source, lowered.rig_binding)]) == []


# ---------------------------------------------------------------------------
# e-boundary (i)-(iv) — the FAIL-CLOSED BOUNDARY. Each is a source where the
# resolver method + a call ARE present (clauses 1a/1b pass) but an UNSUPPORTED
# consumption form survives -> discharge MUST be False -> a loud row -> success=False.
# Each is RED against 5da0eab (the pre-boundary verifier, which exempted lifecycle/
# shadowed reads and only gated on the dot-form read + the ordinal WRITE).
# ---------------------------------------------------------------------------
def _green_plus_boundary_read(injected_read_stmt: str) -> str:
    """The real discharged Player output with an extra UNSUPPORTED boundary READ
    of the field injected into the yielding GetRifle (after the rerouted reads).
    The resolver + calls stay intact, so the ONLY thing blocking discharge is the
    surviving boundary consumption."""
    green = _lower()
    return green.luau_source.replace(
        "    return rifle\nend",
        "    " + injected_read_stmt + "\n    return rifle\nend",
        1,
    )


def test_e_boundary_i_nonself_receiver_owner_fails_closed() -> None:
    """e-boundary (i) — a NON-``self`` receiver read ``owner.weaponSlot`` (the
    reroute target ``self.weaponSlot`` is absent) FAILS CLOSED. A bare
    'no self.<field> survives' check alone would FALSE-PASS this (the codex
    fail-open hole)."""
    src = _green_plus_boundary_read("local x = owner.weaponSlot")
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    )
    assert len(_rig_rows([script])) == 1


def test_e_boundary_i_receiver_alias_fails_closed() -> None:
    """e-boundary (i) — a receiver-alias ``local p = self; p.weaponSlot`` read FAILS
    CLOSED (the alias ``p`` is a non-``self`` receiver the reroute does not cover)."""
    src = _green_plus_boundary_read("local p = self\n    local x = p.weaponSlot")
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    )
    assert len(_rig_rows([script])) == 1


def test_e_boundary_i_module_table_fails_closed() -> None:
    """e-boundary (i) — a legacy module-table read ``Player.weaponSlot`` FAILS CLOSED
    (dead-legacy form; a future module-table mode is a logged boundary, not a silent
    miss)."""
    src = _green_plus_boundary_read("local x = Player.weaponSlot")
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    assert len(_rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ])) == 1


def test_e_boundary_ii_bracket_index_fails_closed() -> None:
    """e-boundary (ii) — a bracket-index read ``self["weaponSlot"]`` FAILS CLOSED
    (the reroute covers the dot-form only)."""
    src = _green_plus_boundary_read('local x = self["weaponSlot"]')
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    )
    assert len(_rig_rows([script])) == 1


def test_e_boundary_iii_lifecycle_read_fails_closed() -> None:
    """e-boundary (iii) — a raw ``self.weaponSlot`` READ surviving in a NON-yielding
    lifecycle method (``Awake``/``Start``) gets its OWN loud row (NOT a blanket
    exempt). Under Path A the init-write is only best-effort-neutralized, so a
    lifecycle read can cache the stale wrong value -> it must fail closed. The
    pre-boundary verifier EXEMPTED lifecycle reads (false-pass) -> RED vs 5da0eab."""
    green = _lower()
    # Inject a surviving raw read into Awake (a non-yielding lifecycle method the
    # lowering does not reroute). The resolver + GetRifle reroute stay intact.
    src = green.luau_source.replace(
        "    self.weaponSlot = nil",
        "    self.weaponSlot = nil\n    local cached = self.weaponSlot",
        1,
    )
    assert "local cached = self.weaponSlot" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    )
    assert len(_rig_rows([script])) == 1


def test_e_boundary_iv_shadowed_self_read_fails_closed() -> None:
    """e-boundary (iv) — a ``self.weaponSlot`` read under an enclosing shadowing
    ``self`` (a ``function(self)`` closure) FAILS CLOSED: it is still a surviving raw
    ``self.<field>`` read (on a FOREIGN object), so the verifier never counts it as
    discharged. The pre-boundary verifier exempted shadowed reads -> RED vs 5da0eab."""
    src = _green_plus_boundary_read(
        "local cb = function(self)\n"
        "        return self.weaponSlot\n"
        "    end\n"
        "    cb(somethingElse)"
    )
    assert "function(self)" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    )
    assert len(_rig_rows([script])) == 1


# ---------------------------------------------------------------------------
# Generic — a DIFFERENT game (field/child) discharges IDENTICALLY (g-generic).
# ---------------------------------------------------------------------------
def test_g_generic_different_game_torchmount_discharges_green() -> None:
    """g-generic (Path A) — a class ``Hero`` with field ``torchMount`` and a
    MainCamera child ``TorchAnchor`` discharges green via the same mechanism: NO
    ``weaponSlot``/``WeaponSlot``/``rifle`` hardcode anywhere in the discharge path.
    ``field``/``child`` are purely the carrier's projections."""
    hero_src = (
        _AI_OUTPUT_SHAPE.replace("Player", "Hero")
        .replace("weaponSlot", "torchMount")
        .replace("riflePrefab", "torchPrefab")
        .replace("GetRifle", "GetTorch")
        .replace("rifle", "torch")
    )
    lowered = _lower(
        source=hero_src,
        field="torchMount",
        child="TorchAnchor",
        source_path="/proj/Hero.cs",
    )
    assert "function Hero:_resolveTorchAnchor()" in lowered.luau_source
    assert 'rig:FindFirstChild("TorchAnchor", true)' in lowered.luau_source
    assert "self:_resolveTorchAnchor()" in lowered.luau_source
    assert _rig_binding_discharged(
        lowered.luau_source, "torchMount", "TorchAnchor"
    ) is True
    assert lowered.rig_binding == _carrier(field="torchMount", child="TorchAnchor")
    # The verifier is GREEN, and keys on the carrier's field/child, not a hardcode.
    assert _rig_rows([_rbx("Hero", lowered.luau_source, lowered.rig_binding)]) == []
    # No rifle/weaponSlot leaked into the lowered output.
    assert "weaponSlot" not in lowered.luau_source
    assert "WeaponSlot" not in lowered.luau_source


# ---------------------------------------------------------------------------
# Pre-fix RED proof for the boundary (e-boundary) — narrator-INDEPENDENT: load the
# ACTUAL 5da0eab verifier blob from git and run the boundary sources through ITS
# ``_rig_binding_discharged``. The forms the pre-boundary verifier MISSED (non-self
# receiver, bracket-index, lifecycle read) false-PASS there (discharged=True) and
# fail closed under Path A — proving the boundary is a real, load-bearing addition.
# (The shadowed-self form already failed closed at 5da0eab — it is a textual
# ``self.<field>`` dot read the old read-scan caught — so it is a REGRESSION GUARD,
# green both ways, asserted separately by test_e_boundary_iv above.)
# ---------------------------------------------------------------------------
def _load_old_verifier_module():
    """Import the 5da0eab (pre-boundary) ``contract_verifier`` blob as a standalone
    module, so the RED proof runs against the REAL prior code, not a reconstruction."""
    import importlib.util
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent.parent  # the worktree root
    blob = subprocess.run(
        ["git", "show", "5da0eab:converter/converter/contract_verifier.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_contract_verifier_5da0eab.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location(
            "_old_cv_5da0eab", str(old_path)
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_5da0eab"] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        old_path.unlink(missing_ok=True)


def test_e_boundary_pre_fix_red_proof_against_real_5da0eab_blob() -> None:
    """e-boundary pre-fix-RED proof — the non-self / bracket / lifecycle boundary
    forms each FALSE-PASS the ACTUAL 5da0eab discharge gate (loaded from git), and
    fail closed under Path A. Run against the real prior code (narrator-independent),
    not a reconstruction."""
    old = _load_old_verifier_module()
    red_forms = {
        "owner": _green_plus_boundary_read("local x = owner.weaponSlot"),
        "alias": _green_plus_boundary_read("local p = self\n    local x = p.weaponSlot"),
        "module": _green_plus_boundary_read("local x = Player.weaponSlot"),
        "bracket": _green_plus_boundary_read('local x = self["weaponSlot"]'),
        "lifecycle": _lower().luau_source.replace(
            "    self.weaponSlot = nil",
            "    self.weaponSlot = nil\n    local cached = self.weaponSlot",
            1,
        ),
    }
    for name, src in red_forms.items():
        # PRE-FIX (real 5da0eab): false-passes (discharged True).
        assert old._rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True, (
            f"{name}: the real 5da0eab gate must false-PASS (proving the Path A "
            f"boundary is load-bearing)"
        )
        # POST-FIX (Path A): fails closed.
        assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False, (
            f"{name}: Path A boundary must fail closed"
        )


def test_e_boundary_iv_shadowed_is_regression_guard_red_both_ways() -> None:
    """The shadowed-``self`` form is a REGRESSION GUARD: it failed closed already at
    5da0eab (the read is textually ``self.weaponSlot`` and the old read-scan caught
    it) and still fails closed under Path A. Asserting it green-both-ways documents
    that it is NOT a newly-caught false-pass (unlike the non-self/bracket/lifecycle
    forms)."""
    old = _load_old_verifier_module()
    src = _green_plus_boundary_read(
        "local cb = function(self)\n        return self.weaponSlot\n    end\n    cb(o)"
    )
    assert old._rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False


# ===========================================================================
# Dual-voice REVIEW round 1 (D-S1b-PATHA-r1) — the boundary gate was a BLACKLIST
# (``_rig_nonself_read_re`` = single-token ``<ident>.field``; ``_rig_bracket_read_re``
# = bare ``self["field"]``), so EXOTIC receivers EVADED it and false-passed an
# undischarged source. The fix is a RECEIVER-AGNOSTIC whitelist: ANY surviving
# code-position field-access READ of ``<field>`` (dot member OR string-key bracket,
# computed key included) fails closed REGARDLESS of receiver, except an assignment
# LHS (a Tier-2-skipped write) and the injected resolver's own internals.
#
# Each exotic form below false-PASSES the REAL 7b59488 blacklist (proven by
# ``test_pathA_r1_exotic_receivers_red_against_7b59488``) and fails closed now.
# ===========================================================================

# The exotic-receiver field READS codex listed + the whitespace/newline-dot + a
# computed/concatenated bracket key. Each is a SURVIVING field-access READ of the
# bound field that the ``self.<field>`` read-reroute does NOT cover.
_EXOTIC_RECEIVER_READS = {
    "paren_owner": "local x = (owner).weaponSlot",
    "getter_call": "local x = getOwner().weaponSlot",
    "indexed_owner": "local x = owners[1].weaponSlot",
    "member_tail_self": "local x = other.self.weaponSlot",
    "paren_self_bracket": 'local x = (self)["weaponSlot"]',
    "whitespace_dot": "local x = self .weaponSlot",
    "newline_dot": "local x = self.\n        weaponSlot",
    "computed_bracket_key": 'local x = self["weapon".."Slot"]',
}


@pytest.mark.parametrize("form", sorted(_EXOTIC_RECEIVER_READS))
def test_pathA_r1_exotic_receiver_read_fails_closed(form: str) -> None:
    """D-S1b-PATHA-r1 — each exotic-receiver / whitespace-dot / computed-key field
    READ is a surviving raw consumption the reroute cannot rewrite, so discharge
    FAILS CLOSED and the verifier fires a loud row. The resolver + the rerouted
    GetRifle reads stay intact, so the ONLY thing blocking discharge is the exotic
    surviving READ — the receiver-agnostic gate must catch it regardless of shape."""
    src = _green_plus_boundary_read(_EXOTIC_RECEIVER_READS[form])
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    )
    assert len(_rig_rows([script])) == 1


def test_pathA_r1_exotic_receivers_red_against_7b59488() -> None:
    """D-S1b-PATHA-r1 pre-fix-RED proof (narrator-INDEPENDENT) — load the ACTUAL
    7b59488 (round-1 blacklist) verifier blob from git and run each exotic form
    through ITS ``_rig_binding_discharged``. Every exotic form false-PASSES there
    (discharged=True — the blacklist evaded it) and fails closed under the
    receiver-agnostic whitelist, proving the fix is load-bearing."""
    import importlib.util
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent.parent  # the worktree root
    blob = subprocess.run(
        ["git", "show", "7b59488:converter/converter/contract_verifier.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_contract_verifier_7b59488.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_old_cv_7b59488", str(old_path))
        assert spec is not None and spec.loader is not None
        old = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_7b59488"] = old
        spec.loader.exec_module(old)
    finally:
        old_path.unlink(missing_ok=True)

    for form, stmt in _EXOTIC_RECEIVER_READS.items():
        src = _green_plus_boundary_read(stmt)
        # PRE-FIX (real 7b59488 blacklist): the exotic receiver EVADED it -> false-pass.
        assert old._rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True, (
            f"{form}: the real 7b59488 blacklist must false-PASS (proving the "
            f"receiver-agnostic gate is load-bearing)"
        )
        # POST-FIX (receiver-agnostic whitelist): fails closed.
        assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False, (
            f"{form}: the receiver-agnostic gate must fail closed"
        )


def test_pathA_r1_nonself_write_lhs_exception_still_discharges() -> None:
    """D-S1b-PATHA-r1 write-LHS exception — a surviving NON-``self`` receiver field
    WRITE (``owner.weaponSlot = 5``) is a Tier-2-skipped write, NOT a read, so it
    does NOT block discharge (discharge is decoupled from neutralize). With the
    reads rerouted, the binding STILL discharges True. (The corpus's own
    ``self.weaponSlot = nil`` write-LHS exception is covered by the happy-path
    tests; this proves the exception is RECEIVER-AGNOSTIC too — a write LHS on any
    receiver survives.)"""
    src = _green_plus_boundary_read("owner.weaponSlot = 5")
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ]) == []


def test_pathA_r1_bracket_write_lhs_exception_still_discharges() -> None:
    """D-S1b-PATHA-r1 write-LHS exception (bracket form) — a surviving bracket WRITE
    ``self["weaponSlot"] = x`` is a write LHS, not a read, so it does not block
    discharge."""
    src = _green_plus_boundary_read('self["weaponSlot"] = somethingElse')
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ]) == []


def test_pathA_r2_decoy_resolver_body_does_not_exempt_foreign_read() -> None:
    """D-S1b-PATHA-r2 — the resolver-body-span ACCESS exemption was dropped (kept
    ONLY the ``_<field>Cache`` exemption). A forged source with the REAL resolver +
    a ``self:_resolveWeaponSlot()`` call PLUS a DECOY balanced
    ``function tbl:_resolveWeaponSlot() ... local y = owner.weaponSlot ... end`` body
    must NOT hide the foreign ``owner.weaponSlot`` READ inside the decoy span — the
    binding FAILS CLOSED (discharged=False). Pre-fix the method-body-span branch
    exempted ANY ``.<field>`` access inside ANY ``_resolveWeaponSlot`` body keyed on
    the method NAME alone -> fail-OPEN (RED against 56691bb)."""
    green = _lower()
    # The real resolver + call sites stay intact; inject a SECOND, decoy method with
    # the same resolver NAME on a different receiver whose body reads the field off a
    # FOREIGN receiver (``owner.weaponSlot``). It is LIVE code (not in a string),
    # balanced, and would have been span-exempted by the dropped branch.
    decoy_source = green.luau_source.replace(
        "    return rifle\nend",
        (
            "    return rifle\nend\n"
            "function Decoy:_resolveWeaponSlot()\n"
            "    local y = owner.weaponSlot\n"
            "    return y\nend"
        ),
        1,
    )
    assert "function Player:_resolveWeaponSlot()" in decoy_source  # real resolver
    assert "self:_resolveWeaponSlot()" in decoy_source  # call sites
    assert "function Decoy:_resolveWeaponSlot()" in decoy_source  # decoy body
    assert "owner.weaponSlot" in decoy_source  # foreign surviving READ
    # The foreign read inside the decoy body must NOT be exempted -> fail closed.
    assert _rig_binding_discharged(decoy_source, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player",
        decoy_source,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": True},
    )
    assert len(_rig_rows([script])) == 1


# ===========================================================================
# Dual-voice REVIEW round 3 (D-S1b-PATHA-r3) — ``_rig_has_computed_field_key`` was
# OVER-BROAD: it (a) flagged any concatenated bracket key whose string fragments
# merely CONTAINED ``<field>`` as a SUBSTRING (not an exact constant-fold), and (b)
# never applied the assignment-LHS exception. So a computed-key WRITE
# (``self["weapon".."Slot"] = x``) and an unrelated substring-containing key
# (``self["preweaponSlotpost".."x"]``) both false-FAILED discharge. The fix: flag a
# computed key ONLY when it CONSTANT-FOLDS (string-literals only) to EXACTLY
# ``<field>``, honor the LHS exception, and never flag a non-foldable dynamic key.
# ===========================================================================

def test_pathA_r3_computed_key_write_lhs_exempt_discharges() -> None:
    """D-S1b-PATHA-r3 — a computed-key WRITE ``self["weapon".."Slot"] = x`` folds to
    exactly ``weaponSlot`` but is an assignment LHS (a Tier-2-skipped write may
    survive), so it does NOT block discharge — discharges True (was false-FAILED)."""
    src = _green_plus_boundary_read('self["weapon".."Slot"] = somethingElse')
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ]) == []


def test_pathA_r3_computed_key_substring_not_exact_does_not_fail() -> None:
    """D-S1b-PATHA-r3 — a computed key ``self["preweaponSlotpost".."x"]`` folds to
    ``preweaponSlotpostx`` which CONTAINS ``weaponSlot`` as a substring but is NOT the
    field, so it must NOT block discharge — discharges True (was false-FAILED on the
    substring match)."""
    src = _green_plus_boundary_read('local x = self["preweaponSlotpost".."x"]')
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ]) == []


def test_pathA_r3_computed_key_read_exact_fold_fails_closed() -> None:
    """D-S1b-PATHA-r3 — a computed-key READ ``self["weapon".."Slot"]`` that folds to
    EXACTLY ``weaponSlot`` is a surviving raw field consumption the reroute cannot
    rewrite, so discharge still FAILS CLOSED (no regression of the exact-fold read)."""
    src = _green_plus_boundary_read('local x = self["weapon".."Slot"]')
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    assert len(_rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ])) == 1


@pytest.mark.parametrize(
    "dynamic_key",
    [
        "local x = self[someVar]",
        "local x = self[getKey()]",
        'local x = self["weapon"..suffix]',
    ],
)
def test_pathA_r2_dynamic_self_index_read_fails_closed(dynamic_key: str) -> None:
    """D-S1b-PATHA-r2 (UPDATED from r3) — a DYNAMIC ``self[<expr>]`` index whose key
    the analyzer cannot decode to a static string (``self[someVar]``, ``self[fn()]``,
    ``self["a"..var]``) COULD read ``<field>`` at runtime and was NOT rerouted (the
    dot-form reroute only rewrites ``self.<field>``). The verifier cannot prove the
    field is unread -> discharge FAILS CLOSED (a loud row). This SUPERSEDES the prior
    r3 stance that such keys discharge True — codex r2 showed that stance let a real
    surviving dynamic read slip through silently and, via the discharge-gated check-D
    exemption, masked a live GetChildren write survivor."""
    src = _green_plus_boundary_read(dynamic_key)
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    assert len(_rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ])) == 1


@pytest.mark.parametrize(
    "non_self_dynamic_key",
    [
        "local x = other[someVar]",
        "local x = tbl[getKey()]",
        'local x = arr["a"..var]',
    ],
)
def test_pathA_r2_non_self_dynamic_index_does_not_false_fail(
    non_self_dynamic_key: str,
) -> None:
    """D-S1b-PATHA-r2 SCOPE GUARD — the dynamic-index fail-closed is scoped to a
    ``self`` receiver. A dynamic index of an UNRELATED table (``other[k]`` /
    ``tbl[fn()]`` / ``arr["a"..var]``) is not a read of THIS instance's field and must
    NOT block discharge — discharges True (no over-broadening to all bracket indexing)."""
    src = _green_plus_boundary_read(non_self_dynamic_key)
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ]) == []


def test_pathA_r3_false_fail_shapes_red_against_041e0ec() -> None:
    """D-S1b-PATHA-r3 pre-fix-RED proof (narrator-INDEPENDENT) — load the ACTUAL
    041e0ec (over-broad computed-key) verifier blob from git. The WRITE / substring /
    dynamic shapes false-FAIL there (discharged=False — over-strict) and discharge
    True now, proving the tightening is load-bearing. The exact-fold READ stays
    fail-closed in BOTH (no regression of the realistic case)."""
    import importlib.util
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent.parent  # the worktree root
    blob = subprocess.run(
        ["git", "show", "041e0ec:converter/converter/contract_verifier.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_contract_verifier_041e0ec.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_old_cv_041e0ec", str(old_path))
        assert spec is not None and spec.loader is not None
        old = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_041e0ec"] = old
        spec.loader.exec_module(old)
    finally:
        old_path.unlink(missing_ok=True)

    # The shapes the over-broad gate false-FAILED (over-strict): each discharges True
    # now. (The fully-dynamic keys — ``self[someVar]`` / ``self[fn()]`` /
    # ``self["weapon"..suffix]`` — were NOT false-failed by 041e0ec either: no ``..``
    # reconstructs the field token, so the old gate already passed them; their
    # non-regression is covered by ``test_pathA_r3_dynamic_non_foldable_key_*``.)
    false_fail_shapes = {
        "computed_write_lhs": 'self["weapon".."Slot"] = somethingElse',
        "substring_not_exact": 'local x = self["preweaponSlotpost".."x"]',
    }
    for name, stmt in false_fail_shapes.items():
        src = _green_plus_boundary_read(stmt)
        # PRE-FIX (041e0ec over-broad): false-FAILS (discharged=False — over-strict).
        assert old._rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False, (
            f"{name}: the real 041e0ec over-broad gate must false-FAIL (proving the "
            f"tightening is load-bearing)"
        )
        # POST-FIX: discharges True (no longer false-fails).
        assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True, (
            f"{name}: the tightened gate must no longer false-fail"
        )

    # The exact-fold READ stays fail-closed in BOTH versions (no regression).
    exact_read = _green_plus_boundary_read('local x = self["weapon".."Slot"]')
    assert old._rig_binding_discharged(exact_read, "weaponSlot", "WeaponSlot") is False
    assert _rig_binding_discharged(exact_read, "weaponSlot", "WeaponSlot") is False


# ===========================================================================
# Dual-voice REVIEW round 4 (D-S1b-PATHA-r4) — two findings:
#
#   FIX 1 [BLOCKING] — bracket-key detection was RAW-TEXT-ONLY, so ENCODED
#   string-literal keys that resolve to the field bypassed the fail-closed gate
#   (fail-OPEN): ``self[[weaponSlot]]`` / ``self[=[weaponSlot]=]`` (long-bracket
#   string keys), ``self["wea\x70onSlot"]`` (hex escape), ``self["wea\x70on".."Slot"]``
#   (escape + concat) all DISCHARGED True at d51ae90 despite being surviving READs.
#   The fix DECODES the full finite Luau string-literal grammar
#   (``_rig_decode_luau_string_key``) and compares to ``<field>`` exactly, closing the
#   whole bracket-key class.
#
#   FIX 2 [MINOR] — the write-LHS exception missed MULTI-ASSIGNMENT: a target list
#   ``self.weaponSlot, other = ...`` / ``self["weapon".."Slot"], other = ...`` has a
#   ``,`` before the ``=``, so it false-FAILED as a READ. The fix recognizes a
#   multi-target assignment list as an LHS WRITE.
#
# Each encoded-read form is RED against d51ae90 (fail-open) and each multi-assignment
# write is RED against d51ae90 (false-fail), proven both directions below.
# ===========================================================================

# The encoded-key READ forms — each DECODES to exactly ``weaponSlot`` and is a
# surviving raw field consumption the dot-form reroute cannot rewrite.
_ENCODED_KEY_READS = {
    "long_bracket": "local x = self[[weaponSlot]]",
    "long_bracket_eq": "local x = self[=[weaponSlot]=]",
    "hex_escape": r'local x = self["wea\x70onSlot"]',
    "hex_escape_concat": r'local x = self["wea\x70on".."Slot"]',
}


@pytest.mark.parametrize("form", sorted(_ENCODED_KEY_READS))
def test_pathA_r4_encoded_key_read_fails_closed(form: str) -> None:
    """D-S1b-PATHA-r4 FIX 1 — each ENCODED string-literal bracket key that decodes to
    exactly ``weaponSlot`` is a surviving READ -> discharge FAILS CLOSED and the
    verifier fires a loud row. The resolver + rerouted reads stay intact, so the ONLY
    thing blocking discharge is the encoded surviving READ — the decode-then-compare
    gate must catch it regardless of encoding (raw-text-only bypass closed)."""
    src = _green_plus_boundary_read(_ENCODED_KEY_READS[form])
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    script = _rbx(
        "Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    )
    assert len(_rig_rows([script])) == 1


def test_pathA_r4_encoded_key_write_lhs_still_discharges() -> None:
    """D-S1b-PATHA-r4 FIX 1 — a long-bracket string-key WRITE ``self[[weaponSlot]] = x``
    decodes to exactly ``weaponSlot`` but is an assignment LHS (a Tier-2-skipped write
    may survive), so it does NOT block discharge — discharges True."""
    src = _green_plus_boundary_read("self[[weaponSlot]] = somethingElse")
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ]) == []


@pytest.mark.parametrize(
    "unrelated_key",
    [
        'local x = self["someOther"]',
        "local x = self[[preweaponSlotpost]]",
    ],
)
def test_pathA_r4_unrelated_decoded_key_does_not_fail(unrelated_key: str) -> None:
    """D-S1b-PATHA-r4 FIX 1 — a key that decodes to a value that is NOT exactly
    ``weaponSlot`` (an unrelated short-string key, or a long-bracket key that merely
    CONTAINS the field as a substring) must NOT block discharge — the compare is
    EXACT, not substring. Discharges True."""
    src = _green_plus_boundary_read(unrelated_key)
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ]) == []


def test_pathA_r2_dynamic_self_bracket_key_fails_closed() -> None:
    """D-S1b-PATHA-r2 (UPDATED from r4 FIX 1) — a fully-dynamic ``self[k]`` decodes to
    None (not a static string), so the analyzer cannot prove it is NOT a read of
    ``<field>``; the dot-form reroute did not rewrite it -> discharge FAILS CLOSED.
    (Supersedes the r4 stance that ``self[k]`` discharges True — see
    ``test_pathA_r2_dynamic_self_index_read_fails_closed`` for the rationale.)"""
    src = _green_plus_boundary_read("local x = self[k]")
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    assert len(_rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ])) == 1


@pytest.mark.parametrize(
    "multi_write",
    [
        "self.weaponSlot, y = a, b",
        'self["weapon".."Slot"], y = a, b',
    ],
)
def test_pathA_r4_multi_assignment_write_lhs_discharges(multi_write: str) -> None:
    """D-S1b-PATHA-r4 FIX 2 — a MULTI-TARGET assignment list
    (``self.weaponSlot, y = a, b`` / ``self["weapon".."Slot"], y = a, b``) is a WRITE
    LHS, not a READ, so it does NOT block discharge — discharges True. The pre-fix
    LHS check only recognized a bare immediate ``=`` and false-FAILED these as reads."""
    src = _green_plus_boundary_read(multi_write)
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _rig_rows([
        _rbx("Player", src, {"field": "weaponSlot", "child": "WeaponSlot", "present": True})
    ]) == []


def test_pathA_r4_encoded_reads_and_multi_writes_red_against_d51ae90() -> None:
    """D-S1b-PATHA-r4 pre-fix-RED proof (narrator-INDEPENDENT) — load the ACTUAL
    d51ae90 verifier blob from git and run each round-4 shape through ITS
    ``_rig_binding_discharged``:

      * each ENCODED-KEY READ false-PASSES there (discharged=True — raw-text-only
        bracket detection missed the encoding) and fails closed now (FIX 1);
      * each MULTI-ASSIGNMENT WRITE false-FAILS there (discharged=False — the LHS
        check missed the ``,``…``=`` list) and discharges True now (FIX 2).

    Proves both fixes are load-bearing in BOTH directions against the real prior code."""
    import importlib.util
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent.parent  # the worktree root
    blob = subprocess.run(
        ["git", "show", "d51ae90:converter/converter/contract_verifier.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_contract_verifier_d51ae90.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_old_cv_d51ae90", str(old_path))
        assert spec is not None and spec.loader is not None
        old = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_d51ae90"] = old
        spec.loader.exec_module(old)
    finally:
        old_path.unlink(missing_ok=True)

    # FIX 1 — encoded-key READs false-PASS at d51ae90 (fail-OPEN), fail closed now.
    for form, stmt in _ENCODED_KEY_READS.items():
        src = _green_plus_boundary_read(stmt)
        assert old._rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True, (
            f"{form}: d51ae90 must false-PASS the encoded-key READ (fail-open) — "
            f"proving FIX 1 is load-bearing"
        )
        assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False, (
            f"{form}: the decode-then-compare gate must fail closed"
        )

    # FIX 2 — multi-assignment WRITEs false-FAIL at d51ae90, discharge True now.
    multi_writes = {
        "dot_multi": "self.weaponSlot, y = a, b",
        "concat_multi": 'self["weapon".."Slot"], y = a, b',
    }
    for name, stmt in multi_writes.items():
        src = _green_plus_boundary_read(stmt)
        assert old._rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False, (
            f"{name}: d51ae90 must false-FAIL the multi-assignment WRITE — proving "
            f"FIX 2 is load-bearing"
        )
        assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True, (
            f"{name}: the multi-target LHS must be recognized as a WRITE"
        )


# ---------------------------------------------------------------------------
# PHASE-INTEGRATION P1 — check D (``_check_surviving_child_ordinal``) FALSE
# POSITIVE on a DISCHARGED rig binding's dead init-write ordinal.
#
# Path A discharges the binding via the READ reroute; the Tier-2 init-write
# neutralize is best-effort and SKIPS the single-line ``if self.cam then
# self.weaponSlot = self.cam:GetChildren()[1] end`` shape. So a DEAD ordinal
# write survives. The binding is fully discharged (present-check GREEN), but
# pre-fix check D counted that dead write against a 0 unresolved-site budget
# ({getchild_total:1, resolved_total:1}) and fired ``child_ordinal_survivor``,
# blocking a correctly-converted game. The fix makes check D rig-aware: the
# write-LHS ordinal of a discharged rig field is "resolved-but-left-behind",
# exempt from the survivor count — but ONLY that dead write (a READ survivor or
# an undischarged binding STILL fires). These tests run the FULL verifier
# interaction the rig-binding-row-only test at module scope did not.
# ---------------------------------------------------------------------------

# The skipped-neutralize single-line-if ordinal-write shape (one of the 5 real
# AI write shapes; blessed as a valid Path A outcome by
# test_rifle_rig_retarget.py:766). The Tier-2 neutralize skips this, so the dead
# ``self.cam:GetChildren()[1]`` write survives in the lowered output.
_SKIPPED_NEUTRALIZE_SINGLELINE_IF = (
    "function Player:Awake()\n"
    "    self.cam = workspace.CurrentCamera\n"
    "    if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n"
    "end\n\n"
    "function Player:GetRifle()\n"
    "    return pivotOf(self.weaponSlot)\n"
    "end\n\n"
    "return Player\n"
)


def _rbx_with_accounting(
    name: str,
    source: str,
    rig_binding: dict[str, object] | None,
    *,
    getchild_total: int,
    resolved_total: int,
) -> RbxScript:
    """An ``RbxScript`` carrying BOTH the rig_binding carrier AND the real
    ``child_ref_resolution`` accounting check D reads (the row the rig-binding-
    row-only test omitted)."""
    return RbxScript(
        name=name,
        source=source,
        rig_binding=rig_binding,
        child_ref_resolution={
            "getchild_total": getchild_total,
            "resolved_total": resolved_total,
        },
    )


def test_checkD_no_false_positive_on_discharged_rig_dead_write() -> None:
    """P1 REGRESSION — the dead init-write ordinal of a DISCHARGED rig binding
    must NOT fire ``child_ordinal_survivor``. Drives the REAL lowering on the
    skipped-neutralize single-line-if shape, with the REAL {1,1} accounting, then
    runs the FULL ``verify_contract`` and asserts: present=True (no
    rig_binding_present violation) AND no child_ordinal_survivor violation."""
    lowered = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF)
    # The dead ordinal write survived (Tier-2 neutralize skipped this shape).
    assert "self.cam:GetChildren()[1]" in lowered.luau_source
    # The binding is fully discharged via the read reroute (Path A).
    assert (
        _rig_binding_discharged(lowered.luau_source, "weaponSlot", "WeaponSlot")
        is True
    )
    assert lowered.rig_binding == _carrier()
    # The REAL pipeline accounting: getchild_total=1 (the one Camera.main
    # GetChild), resolved_total=1 (the rig fact bumped it) -> 0 unresolved budget.
    script = _rbx_with_accounting(
        "Player",
        lowered.luau_source,
        lowered.rig_binding,
        getchild_total=1,
        resolved_total=1,
    )
    # FULL verifier interaction (the gap the row-only test missed).
    res = verify_contract(_TOPOLOGY, [script])
    checks = {v.check for v in res.violations}
    assert "rig_binding_present" not in checks  # present-check GREEN
    assert "child_ordinal_survivor" not in checks  # the P1 false positive is gone
    assert not any("child_ordinal_survivor" in e for e in fail_closed_errors(res))
    # And directly at the unit boundary the codex finding flagged.
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert [v.check for v in cd if v.check == "child_ordinal_survivor"] == []


def test_checkD_pre_fix_red_proof() -> None:
    """PRE-FIX RED proof: the d51ae90 (pre-fix) check D fires
    ``child_ordinal_survivor`` on this exact discharged-rig scenario — proving the
    fix is load-bearing, not a tautology. Rebuilds the pre-fix verifier from git
    and runs ITS check D on the same lowered script + accounting."""
    import importlib.util
    import subprocess

    blob = subprocess.run(
        ["git", "show", "d51ae90:converter/converter/contract_verifier.py"],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_cv_checkd_d51ae90.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_old_cv_checkd", str(old_path))
        assert spec is not None and spec.loader is not None
        old = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_checkd"] = old
        spec.loader.exec_module(old)
    finally:
        old_path.unlink(missing_ok=True)

    lowered = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF)
    script = _rbx_with_accounting(
        "Player",
        lowered.luau_source,
        lowered.rig_binding,
        getchild_total=1,
        resolved_total=1,
    )
    old_cd = old._check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in old_cd), (
        "the pre-fix check D must FIRE the false positive — proving the rig-aware "
        "exemption is load-bearing"
    )
    # The fixed check D does NOT fire on the same input.
    new_cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert not any(v.check == "child_ordinal_survivor" for v in new_cd)


def test_checkD_still_fires_on_read_survivor_no_rig() -> None:
    """PRECISION GUARD — the fix did NOT blanket-disable check D. A genuine
    UNRESOLVED surviving READ ordinal on a script with NO rig binding STILL fires
    ``child_ordinal_survivor`` (budget 0, survivor present, no exemption applies)."""
    # A surviving READ ordinal (consumption, NOT a self.<field>= write), no rig.
    read_survivor_source = (
        "function Player:GetRifle()\n"
        "    local slot = self.cam:GetChildren()[1]\n"
        "    return pivotOf(slot)\n"
        "end\n\n"
        "return Player\n"
    )
    script = _rbx_with_accounting(
        "Player",
        read_survivor_source,
        None,  # NO rig binding -> no exemption is even considered
        getchild_total=1,
        resolved_total=1,  # budget 0 -> any survivor fires
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "an unresolved READ survivor on a non-rig script must STILL fail closed"
    )


def test_checkD_still_fires_on_read_survivor_with_discharged_rig() -> None:
    """PRECISION GUARD — even WITH a discharged rig binding, a survivor that is NOT
    the dead rig write-LHS (a READ consumption of an unrelated ordinal, beyond what
    the rig write accounts for) STILL fires. The exemption subtracts EXACTLY the
    one dead rig write, not every survivor."""
    lowered = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF)
    # Inject an ADDITIONAL surviving READ ordinal (a non-field receiver READ) on
    # top of the discharged rig's dead write. This survivor is NOT a
    # ``self.weaponSlot =`` write, so it is NOT exempt.
    extra_read_source = lowered.luau_source.replace(
        "function Player:GetRifle()\n",
        "function Player:GetRifle()\n"
        "    local extra = self.muzzle:GetChildren()[2]\n",
    )
    assert "self.muzzle:GetChildren()[2]" in extra_read_source
    script = _rbx_with_accounting(
        "Player",
        extra_read_source,
        lowered.rig_binding,  # discharged rig binding present
        getchild_total=1,
        resolved_total=1,  # budget 0; 2 survivors - 1 exempt dead write = 1 > 0
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "a READ survivor beyond the exempt dead rig write must STILL fail closed — "
        "the exemption subtracts only the one dead write, not every survivor"
    )


def test_checkD_no_exemption_on_undischarged_rig() -> None:
    """PRECISION GUARD — the exemption is gated on the binding being DISCHARGED.
    A rig field whose surviving ordinal write is present but whose binding is NOT
    discharged (no resolver landed -> raw reads survive) does NOT get the
    exemption, so check D STILL fires on the surviving ordinal."""
    # The raw AI shape with NO lowering applied: the ``self.weaponSlot =
    # ...GetChildren()[1]`` write survives, no resolver, raw reads survive ->
    # _rig_binding_discharged is False.
    raw = _AI_OUTPUT_SHAPE
    assert _rig_binding_discharged(raw, "weaponSlot", "WeaponSlot") is False
    assert "self.cam:GetChildren()[1]" in raw
    script = _rbx_with_accounting(
        "Player",
        raw,
        {"field": "weaponSlot", "child": "WeaponSlot", "present": False},
        getchild_total=1,
        resolved_total=1,  # budget 0
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "an UNDISCHARGED binding gets no exemption — the surviving ordinal fires"
    )


# ---------------------------------------------------------------------------
# ROUND-6 P1 — the rig exemption (round-5) was computed by a SEPARATE regex
# (``_rig_discharged_ordinal_write_exempt_count``) that counted ``self.<field>=``
# writes WITHOUT check D's engine-global filter and WITHOUT its receiver-shape
# constraint, and matched EVERY same-field GetChildren()[n] write — so it could
# subtract a number larger than what check D actually counted, silently swallowing
# a SEPARATE real survivor on the same script. The fix makes the exemption
# SITE-ALIGNED inside ``_count_surviving_child_ordinals`` (skip AT MOST the one
# counted dead-init-write site, after the same code-position + engine-global
# filters), guaranteeing ``exempt ⊆ counted-survivors``. These tests drive the
# REAL helpers / lowering and would each have FAILED against 3a4231a.
# ---------------------------------------------------------------------------


def test_checkD_engine_global_dead_write_does_not_mask_separate_survivor() -> None:
    """ROUND-6 BYPASS-1 GUARD — a discharged rig whose dead init-write uses an
    ENGINE-GLOBAL receiver (``self.weaponSlot = workspace:GetChildren()[1]``) is
    NOT a site check D counts (engine-global filtered), so it must NOT be exempted.
    A SEPARATE genuine survivor on the same script (``foo:GetChildren()[2]``) STILL
    fires. Against 3a4231a the separate-regex exempt count over-counted the
    engine-global write (1) and the subtraction masked the real survivor."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    # Re-root the dead write at an engine global (check D does NOT count it), and
    # add a SEPARATE genuine READ survivor that check D DOES count.
    src = base.replace(
        "self.weaponSlot = self.cam:GetChildren()[1]",
        "self.weaponSlot = workspace:GetChildren()[1]",
    ).replace(
        "function Player:GetRifle()\n",
        "function Player:GetRifle()\n    local extra = foo:GetChildren()[2]\n",
    )
    assert "self.weaponSlot = workspace:GetChildren()[1]" in src
    assert "foo:GetChildren()[2]" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),
        getchild_total=1,
        resolved_total=1,  # budget 0 -> the separate survivor must fire
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "an engine-global-rooted dead write is not a counted site, so it must not "
        "be exempted — the SEPARATE real survivor must STILL fail closed"
    )


def test_checkD_bracket_receiver_dead_write_does_not_mask_separate_survivor() -> None:
    """ROUND-6 BYPASS-1 GUARD (variant) — a discharged rig whose dead init-write has
    a BRACKET-INDEXED receiver (``self.weaponSlot = arr[1]:GetChildren()[2]``) is NOT
    spanned by check D's receiver pattern, so it is NOT a counted site and must NOT
    be exempted. A SEPARATE genuine survivor STILL fires."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    src = base.replace(
        "self.weaponSlot = self.cam:GetChildren()[1]",
        "self.weaponSlot = arr[1]:GetChildren()[2]",
    ).replace(
        "function Player:GetRifle()\n",
        "function Player:GetRifle()\n    local extra = foo:GetChildren()[3]\n",
    )
    assert "self.weaponSlot = arr[1]:GetChildren()[2]" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),
        getchild_total=1,
        resolved_total=1,
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "a bracket-indexed-receiver dead write is not a counted site, so it must "
        "not be exempted — the SEPARATE real survivor must STILL fail closed"
    )


def test_checkD_exempts_only_one_same_field_write() -> None:
    """ROUND-6 BYPASS-2 GUARD — the exemption is bounded to AT MOST the SINGLE dead
    rig init-write. A discharged rig with the legitimate dead write PLUS a SECOND
    same-field ``GetChildren()[n]`` write on a different receiver
    (``self.weaponSlot = self.muzzle:GetChildren()[2]``) leaves the extra write
    counted (2 survivors, 1 exempt -> 1) so it STILL fires. Against 3a4231a the
    unanchored same-field exempt regex matched BOTH writes (exempt 2) and masked it."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    src = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n"
        "    self.weaponSlot = self.muzzle:GetChildren()[2]\n",
    )
    assert "self.weaponSlot = self.muzzle:GetChildren()[2]" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),
        getchild_total=1,
        resolved_total=1,  # budget 0; credited cam write exempt, muzzle write fires
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "only the SINGLE credited dead rig init-write is exempt — a second same-field "
        "write through a DIFFERENT receiver (codex r8) must STILL fail closed"
    )


def test_checkD_round6_pre_fix_red_proof() -> None:
    """PRE-FIX RED proof for round 6: the 3a4231a (separate-regex) exempt logic
    MASKED both bypasses — its check D did NOT fire on the engine-global-write and
    second-same-field-write scenarios above. Proves the site-aligned refactor is
    load-bearing, not a tautology."""
    import importlib.util
    import subprocess

    blob = subprocess.run(
        ["git", "show", "3a4231a:converter/converter/contract_verifier.py"],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_cv_checkd_3a4231a.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_old_cv_3a4231a", str(old_path))
        assert spec is not None and spec.loader is not None
        old = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_3a4231a"] = old
        spec.loader.exec_module(old)
    finally:
        old_path.unlink(missing_ok=True)

    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    rb = _carrier()

    # BYPASS-1 scenario: engine-global dead write + separate genuine survivor.
    b1 = base.replace(
        "self.weaponSlot = self.cam:GetChildren()[1]",
        "self.weaponSlot = workspace:GetChildren()[1]",
    ).replace(
        "function Player:GetRifle()\n",
        "function Player:GetRifle()\n    local extra = foo:GetChildren()[2]\n",
    )
    s1 = _rbx_with_accounting("Player", b1, rb, getchild_total=1, resolved_total=1)
    assert not any(
        v.check == "child_ordinal_survivor"
        for v in old._check_surviving_child_ordinal(_TOPOLOGY, [s1])
    ), "the pre-fix check D must MASK bypass-1 — proving the round-6 fix is load-bearing"
    assert any(
        v.check == "child_ordinal_survivor"
        for v in _check_surviving_child_ordinal(_TOPOLOGY, [s1])
    )

    # BYPASS-2 scenario: legit dead write + second same-field write.
    b2 = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n"
        "    self.weaponSlot = self.muzzle:GetChildren()[2]\n",
    )
    s2 = _rbx_with_accounting("Player", b2, rb, getchild_total=1, resolved_total=1)
    assert not any(
        v.check == "child_ordinal_survivor"
        for v in old._check_surviving_child_ordinal(_TOPOLOGY, [s2])
    ), "the pre-fix check D must MASK bypass-2 — proving the round-6 fix is load-bearing"
    assert any(
        v.check == "child_ordinal_survivor"
        for v in _check_surviving_child_ordinal(_TOPOLOGY, [s2])
    )


# ---------------------------------------------------------------------------
# ROUND-7 P1 — the SITE identification was still TEXT-FORWARD: the exempt skip
# was driven by a forward RHS span from a loose ``self.<field> =`` SUBSTRING
# (``_rig_field_write_rhs_spans``). Two NEW holes, same root:
#   (a) ``;`` boundary (Claude): the RHS span ended at a depth-0 NEWLINE but not
#       a Luau ``;`` separator, so ``self.weaponSlot = nil ; foo:GetChildren()[1]``
#       swallowed the genuine survivor into the exempt span -> masked.
#   (b) substring LHS (codex): ``self.<field> =`` matched as a SUBSTRING, so
#       ``myself.weaponSlot = ...`` / ``other.self.weaponSlot = ...`` were taken
#       as the exempt write -> a real survivor masked.
# The fix INVERTS the logic: the exemption is now STATEMENT-ANCHORED on the
# COUNTED site — ``_site_is_discharged_rig_dead_write`` parses the single Luau
# statement physically containing the counted GetChildren site (bounded by its
# own depth-0 ``;``/newline) and exempts ONLY when that statement is EXACTLY an
# assignment whose LHS is the STANDALONE lvalue ``self.<field>`` with the site on
# its RHS. An ambiguous statement is NOT exempted (counted, fail closed). These
# tests drive the REAL helpers and would each have FAILED against de8e7f9.
# ---------------------------------------------------------------------------


def test_checkD_semicolon_separated_survivor_still_fires() -> None:
    """ROUND-7 BYPASS-A GUARD (``;`` boundary) — a discharged rig with the dead
    write and a GENUINE UNRELATED survivor on the SAME physical line separated by a
    Luau ``;`` (``self.weaponSlot = nil ; local stray = foo:GetChildren()[1]``) must
    STILL fire. The survivor is in a SEPARATE ``;``-bounded statement, so the
    statement-anchored exemption never reaches it. Against de8e7f9 the forward RHS
    span over-reached past the ``;`` and the single exempt skip masked the survivor."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    # Replace the surviving dead-write line with: a NIL write (still discharges —
    # discharge keys on the read reroute, not the write shape) then a ``;`` and a
    # genuine UNRELATED survivor on the same physical line.
    src = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "self.weaponSlot = nil ; local stray = foo:GetChildren()[1]\n",
    )
    assert "self.weaponSlot = nil ; local stray = foo:GetChildren()[1]" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    # The genuine survivor must be counted (NOT swallowed by the exempt skip).
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),
        getchild_total=1,
        resolved_total=1,  # budget 0 -> the unrelated survivor must fire
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "a genuine survivor sharing a ``;``-line with the dead rig write must STILL "
        "fail closed — the exempt skip is statement-bounded, not line-bounded"
    )


def test_checkD_myself_substring_lhs_survivor_still_fires() -> None:
    """ROUND-7 BYPASS-B GUARD (substring LHS, ``myself``) — a survivor written via a
    NON-``self`` lvalue whose name merely ENDS in ``self`` (``myself.weaponSlot =
    foo:GetChildren()[2]``) is NOT the exempt rig write: ``self`` must be a STANDALONE
    identifier. The statement-anchored LHS match rejects it, so the survivor STILL
    fires. Against de8e7f9 the loose ``self.<field> =`` substring matched it."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    src = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "myself.weaponSlot = foo:GetChildren()[2]\n",
    )
    assert "myself.weaponSlot = foo:GetChildren()[2]" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),
        getchild_total=1,
        resolved_total=1,
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "a ``myself.<field> =`` write is NOT the exempt rig write (``self`` is not "
        "standalone) — the survivor must STILL fail closed"
    )


def test_checkD_dotted_self_substring_lhs_survivor_still_fires() -> None:
    """ROUND-7 BYPASS-B GUARD (substring LHS, ``other.self``) — a survivor written via
    ``other.self.weaponSlot = foo:GetChildren()[2]`` is NOT the exempt rig write: the
    char before ``self`` is a ``.`` (it is a FIELD access, not the standalone lvalue).
    The statement-anchored LHS match rejects it, so the survivor STILL fires."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    src = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "other.self.weaponSlot = foo:GetChildren()[2]\n",
    )
    assert "other.self.weaponSlot = foo:GetChildren()[2]" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),
        getchild_total=1,
        resolved_total=1,
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "an ``other.self.<field> =`` write is NOT the exempt rig write (``self`` is a "
        "field access, not standalone) — the survivor must STILL fail closed"
    )


def test_checkD_field_prefix_collision_does_not_exempt() -> None:
    """ROUND-7 NEIGHBOR GUARD (field word-boundary) — a rig field ``weaponSlot`` must
    NOT exempt a write to ``self.weaponSlotBackup = foo:GetChildren()[1]`` (a longer
    field whose name has ``weaponSlot`` as a prefix). The field match requires a word
    boundary, so the survivor STILL fires."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    src = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "self.weaponSlotBackup = foo:GetChildren()[1]\n",
    )
    assert "self.weaponSlotBackup = foo:GetChildren()[1]" in src
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    # exempt field ``weaponSlot`` must NOT exempt the ``weaponSlotBackup`` write.
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),
        getchild_total=1,
        resolved_total=1,
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "a ``weaponSlot`` rig must NOT exempt a ``weaponSlotBackup`` write — the "
        "field match is word-bounded, so the survivor must STILL fail closed"
    )


def test_checkD_field_prefix_collision_reverse() -> None:
    """ROUND-7 NEIGHBOR GUARD (field word-boundary, reverse) — the dead write is
    ``self.weaponSlot = ...`` but the exempt field is the LONGER ``weaponSlotBackup``;
    the shorter write must NOT be exempted (word boundary both directions)."""
    # A standalone unit assertion is cleanest here: a single dead write on field
    # ``weaponSlot`` is NOT exempted when the exempt field is ``weaponSlotBackup``.
    # Receiver + ordinal match (``self.cam``/[1]); ONLY the field differs -> counted.
    one_write = "self.weaponSlot = self.cam:GetChildren()[1]\n"
    exempt = _RigDeadWriteExempt(
        field_name="weaponSlotBackup", cam_receiver="cam", cam_ordinal=0
    )
    assert _count_surviving_child_ordinals(one_write, exempt) == 1


def test_checkD_comparison_on_lhs_line_not_exempted() -> None:
    """ROUND-7 NEIGHBOR GUARD (real-assignment) — a COMPARISON (``==``), not an
    assignment (``if self.weaponSlot == foo:GetChildren()[1] then end``), is NOT the
    exempt rig write: the statement-anchored LHS match requires a REAL ``=``, not a
    comparison operator. So even with the exempt field set, the survivor is COUNTED.
    Asserted at the ``_count_surviving_child_ordinals`` boundary (a surviving
    ``self.weaponSlot ==`` READ would itself fail discharge upstream, so this isolates
    the assignment-vs-comparison discrimination in the site anchor)."""
    # Receiver + ordinal MATCH (``self.cam``/[1]) so the ONLY discriminator under test
    # is assignment-vs-comparison; the comparison must NOT be exempted -> counted.
    src = "if self.weaponSlot == self.cam:GetChildren()[1] then end\n"
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    # And every comparison operator on the LHS is rejected as a non-assignment.
    for op in ("==", "<=", ">=", "~="):
        cmp_src = f"if self.weaponSlot {op} self.cam:GetChildren()[1] then end\n"
        assert _count_surviving_child_ordinals(cmp_src, _CORPUS_EXEMPT) == 1, op


def test_checkD_rhs_with_leading_operand_not_exempted() -> None:
    """ROUND-7 CODEX GUARD (RHS identity) — the exempt write must be the BARE dead-write
    shape ``self.<field> = <recv>:GetChildren()[n]`` (the site is the WHOLE RHS). An
    assignment whose RHS merely CONTAINS a GetChildren after a leading operand
    (``self.weaponSlot = nil or foo:GetChildren()[1]``) is NOT the dead init-write — the
    ``foo:GetChildren()[1]`` is a genuine survivor and must STILL be counted. Against the
    statement-anchored draft (before the whole-RHS guard) this masked the survivor."""
    # Receiver + ordinal MATCH (``self.cam``/[1]) so the ONLY discriminator is the
    # whole-RHS constraint; the leading ``nil or`` operand means the site is NOT the
    # whole RHS -> NOT the dead init-write -> counted.
    src = "self.weaponSlot = nil or self.cam:GetChildren()[1]\n"
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1


def test_checkD_rhs_with_multiple_getchildren_operands_all_counted() -> None:
    """ROUND-7 CODEX GUARD (RHS identity, multi-operand) — an assignment whose RHS is a
    BINARY expression of two GetChildren sites (``self.weaponSlot = a:GetChildren()[1] +
    b:GetChildren()[2]``) is not a dead init-write; NEITHER site is the whole RHS, so
    BOTH are counted (no exemption spent)."""
    # The first operand has the matching receiver+ordinal (``self.cam``/[1]) but is NOT
    # the whole RHS (a ``+ bar:...`` follows), so it is NOT exempt; both are counted.
    src = "self.weaponSlot = self.cam:GetChildren()[1] + bar:GetChildren()[2]\n"
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 2


def test_checkD_bare_dead_write_shapes_still_exempt() -> None:
    """ROUND-7 CODEX GUARD (companion) + REDESIGN r3 — the whole-RHS constraint does
    NOT break the legitimate credited dead-write shapes the exemption exists for: a
    plain ``self.cam`` receiver, an ``if c then ... end`` single-line block, and a
    multiline continuation all still exempt their single credited dead init-write site
    (receiver ``self.cam`` + ordinal [1] both anchor on ``_CORPUS_EXEMPT``)."""
    for src in (
        "self.weaponSlot = self.cam:GetChildren()[1]\n",
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "self.weaponSlot =\n    self.cam:GetChildren()[1]\n",
    ):
        assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 0, src


def test_checkD_two_dead_writes_only_one_exempt() -> None:
    """ROUND-7 NEIGHBOR GUARD (cap) — two IDENTICAL credited ``self.weaponSlot =
    self.cam:GetChildren()[1]`` writes (both matching the exemption's receiver +
    ordinal): only ONE is exempt, the second STILL fires. Statement-anchored identity
    + the r3 receiver/ordinal anchors + the ``< 1`` cap together exempt at most the
    single credited dead init-write."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    src = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "self.weaponSlot = self.cam:GetChildren()[1]\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]\n",
    )
    assert src.count("self.weaponSlot = self.cam:GetChildren()[1]") == 2
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    # 2 counted writes - 1 exempt = 1 survivor (the cap allows AT MOST one).
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),
        getchild_total=1,
        resolved_total=1,
    )
    cd = _check_surviving_child_ordinal(_TOPOLOGY, [script])
    assert any(v.check == "child_ordinal_survivor" for v in cd), (
        "only ONE dead rig write is exempt — a SECOND same-field write must STILL "
        "fail closed"
    )


def test_checkD_round7_pre_fix_red_proof() -> None:
    """PRE-FIX RED proof for round 7: the de8e7f9 (forward-RHS-span) exempt logic
    MASKED both new bypasses — its check D did NOT fire on the ``;``-separated
    survivor and the ``myself.``/``other.self.`` substring-LHS scenarios. Proves the
    statement-anchored inversion is load-bearing, not a tautology. Rebuilds de8e7f9's
    verifier from git and runs ITS check D on the same inputs."""
    import importlib.util
    import subprocess

    blob = subprocess.run(
        ["git", "show", "de8e7f9:converter/converter/contract_verifier.py"],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_cv_checkd_de8e7f9.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_old_cv_de8e7f9", str(old_path))
        assert spec is not None and spec.loader is not None
        old = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_de8e7f9"] = old
        spec.loader.exec_module(old)
    finally:
        old_path.unlink(missing_ok=True)

    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    rb = _carrier()

    # BYPASS-A scenario: ``;``-separated unrelated survivor.
    a = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "self.weaponSlot = nil ; local stray = foo:GetChildren()[1]\n",
    )
    sa = _rbx_with_accounting("Player", a, rb, getchild_total=1, resolved_total=1)
    assert not any(
        v.check == "child_ordinal_survivor"
        for v in old._check_surviving_child_ordinal(_TOPOLOGY, [sa])
    ), "the pre-fix check D must MASK the ``;`` bypass — proving the round-7 fix is load-bearing"
    assert any(
        v.check == "child_ordinal_survivor"
        for v in _check_surviving_child_ordinal(_TOPOLOGY, [sa])
    )

    # BYPASS-B scenario: ``myself.`` substring-LHS survivor.
    b = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        "myself.weaponSlot = foo:GetChildren()[2]\n",
    )
    sb = _rbx_with_accounting("Player", b, rb, getchild_total=1, resolved_total=1)
    assert not any(
        v.check == "child_ordinal_survivor"
        for v in old._check_surviving_child_ordinal(_TOPOLOGY, [sb])
    ), "the pre-fix check D must MASK the substring-LHS bypass — proving the round-7 fix is load-bearing"
    assert any(
        v.check == "child_ordinal_survivor"
        for v in _check_surviving_child_ordinal(_TOPOLOGY, [sb])
    )


# ===========================================================================
# §4 (f-r3) — check D's RIG-AWARE exemption is POSITIVELY anchored on
# cam_receiver + cam_ordinal: it exempts ONLY the rig's EXACT credited dead write,
# never masks another survivor (REDESIGN r3). Drives the REAL helpers /
# verify_contract; each case maps to the design's (i)-(x).
# ===========================================================================


def _discharged_with_write(write_line: str) -> str:
    """A FULLY-DISCHARGED rig source (resolver method + rerouted reads + no raw
    ``self.weaponSlot`` read) whose surviving dead init-write line is ``write_line``.
    Built by lowering the corpus shape (which discharges) then swapping the single
    surviving ``if self.cam then ... end`` write for the supplied statement."""
    base = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source
    src = base.replace(
        "if self.cam then self.weaponSlot = self.cam:GetChildren()[1] end\n",
        write_line + "\n",
    )
    assert write_line in src
    # The swap must not disturb discharge (still the read reroute + resolver).
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True
    return src


def _fr3_script(src: str, carrier: dict[str, object] | None) -> RbxScript:
    """A budget-0 ({1,1}) ``RbxScript`` carrying ``carrier`` + ``src``."""
    return _rbx_with_accounting(
        "Player", src, carrier, getchild_total=1, resolved_total=1
    )


def _checkD_fires(script: RbxScript) -> bool:
    return any(
        v.check == "child_ordinal_survivor"
        for v in _check_surviving_child_ordinal(_TOPOLOGY, [script])
    )


def test_fr3_i_exempt_exact_credited_dead_write() -> None:
    """(i) EXEMPT — the credited dead init-write ``self.weaponSlot =
    self.cam:GetChildren()[1]`` (receiver ``self.cam`` == self.<cam_receiver>, ordinal
    1 == cam_ordinal+1) is exempted -> NO child_ordinal_survivor (the r1 false-positive
    is gone)."""
    src = _discharged_with_write("self.weaponSlot = self.cam:GetChildren()[1]")
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 0
    assert not _checkD_fires(_fr3_script(src, _carrier()))


def test_fr3_ii_fires_on_different_receiver_codex_r8() -> None:
    """(ii) STILL FIRES — DIFFERENT receiver (codex r8): ``self.weaponSlot =
    self.muzzle:GetChildren()[2]`` -> receiver ``self.muzzle`` != ``self.cam`` ->
    COUNTED. The receiver-blind r8 exemption masked this."""
    src = _discharged_with_write("self.weaponSlot = self.muzzle:GetChildren()[2]")
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    assert _checkD_fires(_fr3_script(src, _carrier()))


def test_fr3_iii_fires_on_same_receiver_different_ordinal_codex_r3() -> None:
    """(iii) STILL FIRES — SAME receiver, DIFFERENT ordinal (codex r3): the credited
    init ``[1]`` was neutralized away and a stray ``self.weaponSlot =
    self.cam:GetChildren()[2]`` survives -> ordinal 2 != cam_ordinal+1 (==1) ->
    COUNTED (a genuine survivor, not masked)."""
    src = _discharged_with_write("self.weaponSlot = self.cam:GetChildren()[2]")
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    assert _checkD_fires(_fr3_script(src, _carrier()))


def test_fr3_iv_fires_on_bare_receiver() -> None:
    """(iv) STILL FIRES — bare-receiver guard (review MAJOR #3): a write whose receiver
    is a bare local ``cam`` (``self.weaponSlot = cam:GetChildren()[1]``, NOT
    ``self.cam``) -> not the ``self.<member>`` form -> COUNTED. ``cam_receiver`` matches
    only ``self.cam``, never a bare ``cam`` local that could be an unrelated symbol."""
    src = _discharged_with_write("self.weaponSlot = cam:GetChildren()[1]")
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    assert _checkD_fires(_fr3_script(src, _carrier()))


def test_fr3_v_fires_on_second_credited_write_at_most_one() -> None:
    """(v) STILL FIRES — second credited-shape write past the one exemption: two
    identical ``self.weaponSlot = self.cam:GetChildren()[1]`` writes -> at-most-one
    exempt, the second fires."""
    src = _discharged_with_write(
        "self.weaponSlot = self.cam:GetChildren()[1]\n"
        "    self.weaponSlot = self.cam:GetChildren()[1]"
    )
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    assert _checkD_fires(_fr3_script(src, _carrier()))


def test_fr3_vi_fires_on_read_survivor() -> None:
    """(vi-a) STILL FIRES — a surviving READ ordinal (a consumption, not a
    ``self.<field> =`` write) on a discharged rig is NOT the credited dead write ->
    COUNTED. The credited cam write is itself exempt, the extra READ still fires."""
    src = _discharged_with_write("self.weaponSlot = self.cam:GetChildren()[1]")
    src = src.replace(
        "function Player:GetRifle()\n",
        "function Player:GetRifle()\n    local extra = self.muzzle:GetChildren()[2]\n",
    )
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    assert _checkD_fires(_fr3_script(src, _carrier()))


def test_fr3_vi_fires_on_none_carrier_non_rig() -> None:
    """(vi-b) STILL FIRES — a non-rig script (``rig_binding=None``) gets NO exemption;
    a surviving ordinal fails closed."""
    src = _discharged_with_write("self.weaponSlot = self.cam:GetChildren()[1]")
    assert _checkD_fires(_fr3_script(src, None))


def test_fr3_vi_fires_on_undischarged_stamp_true() -> None:
    """(vi-c) STILL FIRES — gated on REAL discharge, not the stamp: ``present=True`` but
    the source is NOT discharged (raw reads survive, no resolver) -> the independent
    ``_rig_binding_discharged`` returns False -> no exemption -> the surviving ordinal
    fires."""
    raw = _AI_OUTPUT_SHAPE  # un-lowered: raw reads survive, no resolver
    assert _rig_binding_discharged(raw, "weaponSlot", "WeaponSlot") is False
    assert "self.cam:GetChildren()[1]" in raw
    assert _checkD_fires(_fr3_script(raw, _carrier(present=True)))


def test_fr3_vii_safe_false_positive_on_direct_no_seed_form() -> None:
    """(vii) SAFE false-positive — the direct no-seed form has
    ``cam_receiver=="Camera.main.transform"`` (NEVER ""); it forms no valid
    ``self.<member>`` match, so the rig's own dead write is COUNTED (fail-closed, never
    silently exempted)."""
    # The surviving write IS the direct-form camera-child write the resolver credited.
    src = _discharged_with_write(
        "self.weaponSlot = Camera.main.transform:GetChildren()[1]"
    )
    direct = _RigDeadWriteExempt(
        field_name="weaponSlot",
        cam_receiver="Camera.main.transform",
        cam_ordinal=0,
    )
    # No ``self.<member>`` match -> the credited write is COUNTED (safe false-positive).
    assert _count_surviving_child_ordinals(src, direct) == 1
    carrier = _carrier(cam_receiver="Camera.main.transform")
    assert _checkD_fires(_fr3_script(src, carrier))


def test_fr3_viii_statement_anchored_retained_semicolon_and_substring() -> None:
    """(viii) Statement-anchored RETAINED (r7) — the r3 anchors are ANDed onto
    ``_site_is_discharged_rig_dead_write``, not a replacement: a ``;``-separated
    survivor and a substring-LHS ``myself.weaponSlot`` survivor BOTH still fire even
    when the receiver+ordinal happen to match the credited triple."""
    # ``;``-span: the credited write became ``nil``; a SEPARATE survivor shares the
    # physical line via ``;`` with the matching receiver+ordinal — must still fire
    # (the exempt skip is statement-bounded, not line-bounded).
    semi = _discharged_with_write(
        "self.weaponSlot = nil ; local stray = self.cam:GetChildren()[1]"
    )
    assert _count_surviving_child_ordinals(semi, _CORPUS_EXEMPT) == 1
    assert _checkD_fires(_fr3_script(semi, _carrier()))
    # substring-LHS: ``myself.weaponSlot`` with the matching receiver+ordinal is NOT the
    # standalone ``self.<field>`` lvalue -> still fires.
    sub = _discharged_with_write(
        "myself.weaponSlot = self.cam:GetChildren()[1]"
    )
    assert _count_surviving_child_ordinals(sub, _CORPUS_EXEMPT) == 1
    assert _checkD_fires(_fr3_script(sub, _carrier()))


def test_fr3_ix_threading_proof_rehydrated_carrier_still_anchors() -> None:
    """(ix) Threading proof (review BLOCKING #1) — a carrier that carries
    ``cam_receiver``+``cam_ordinal`` (as the widened LOAD validator rehydrates) still
    anchors the exemption: the credited write is exempt and the DIFFERENT-receiver
    survivor (ii) still fires. A carrier that DROPPED those keys (the pre-fix field-only
    LOAD filter) would re-mask (ii) — asserted as the delta."""
    # The credited write — exempt only because the rehydrated carrier carries the keys.
    credited = _discharged_with_write("self.weaponSlot = self.cam:GetChildren()[1]")
    full_carrier = _carrier()  # 5-key, as the widened validator rehydrates
    assert not _checkD_fires(_fr3_script(credited, full_carrier))
    # The DIFFERENT-receiver survivor — fires under the full carrier (anchored).
    diff = _discharged_with_write("self.weaponSlot = self.muzzle:GetChildren()[2]")
    assert _checkD_fires(_fr3_script(diff, full_carrier))
    # DELTA: a carrier that DROPPED cam_receiver/cam_ordinal (the pre-fix 3-key LOAD
    # filter) yields NO exemption at all in our entry-point guard -> the credited write
    # is no longer exempt (so the exemption is provably keyed on the threaded keys).
    dropped = {"field": "weaponSlot", "child": "WeaponSlot", "present": True}
    assert _checkD_fires(_fr3_script(credited, dropped))


def test_fr3_x_pre_fix_red_proof_receiver_blind_r8_masks() -> None:
    """(x) Pre-fix proof — the receiver-blind r8 exemption (HEAD 91a19d4, field-only
    ``_count_surviving_child_ordinals(source, exempt_field)``) MASKS the
    different-receiver (ii) and different-ordinal (iii) survivors; the r3 anchored
    exemption does not. Loads the ACTUAL 91a19d4 verifier blob from git and runs ITS
    check D on the same inputs."""
    import importlib.util
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent.parent  # worktree root
    blob = subprocess.run(
        ["git", "show", "91a19d4:converter/converter/contract_verifier.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_cv_r8_91a19d4.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_old_cv_r8", str(old_path))
        assert spec is not None and spec.loader is not None
        old = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_r8"] = old
        spec.loader.exec_module(old)
    finally:
        old_path.unlink(missing_ok=True)

    carrier = _carrier()  # the old verifier reads only field/child/present
    # (ii) DIFFERENT-receiver survivor: the r8 field-only exemption MASKS it.
    diff_recv = _discharged_with_write(
        "self.weaponSlot = self.muzzle:GetChildren()[2]"
    )
    s_ii = _fr3_script(diff_recv, carrier)
    assert not any(
        v.check == "child_ordinal_survivor"
        for v in old._check_surviving_child_ordinal(_TOPOLOGY, [s_ii])
    ), "the receiver-blind r8 exemption MUST mask the different-receiver survivor"
    assert _checkD_fires(s_ii)  # r3 does not mask it

    # (iii) SAME-receiver DIFFERENT-ordinal survivor: the r8 field-only exemption MASKS.
    diff_ord = _discharged_with_write(
        "self.weaponSlot = self.cam:GetChildren()[2]"
    )
    s_iii = _fr3_script(diff_ord, carrier)
    assert not any(
        v.check == "child_ordinal_survivor"
        for v in old._check_surviving_child_ordinal(_TOPOLOGY, [s_iii])
    ), "the receiver-blind r8 exemption MUST mask the different-ordinal survivor"
    assert _checkD_fires(s_iii)  # r3 does not mask it


# ===========================================================================
# §4 (f-r3-INERT-BOUND) — codex r3 trust-boundary adjudication (User-Challenge/Taste).
#
# The check-D rig exemption ANCHORS on the carrier's cam_receiver/cam_ordinal, which
# it TRUSTS as the deterministic resolver-fact's proxy (it cannot be re-derived from
# the source — the source can't self-identify which GetChild site the resolver
# credited; exactly as field/child are trusted anchors in FIX 1). Codex showed a
# WELL-FORMED FORGED carrier (valid types, receiver+ordinal chosen to match a genuine
# survivor) could exempt that survivor.
#
# The ADJUDICATED bound (NOT a behavioral silent-miss — these tests PROVE and DOCUMENT
# the structural guarantee, they do not add impossible authentication): the exemption
# only ever skips a site that BOTH
#   (1) passes _site_is_discharged_rig_dead_write — i.e. the site is the WHOLE RHS of a
#       ``self.<field> = ...`` assignment (a WRITE to the rig field), AND
#   (2) is on a script whose binding is INDEPENDENTLY discharged
#       (_rig_binding_discharged -> no raw ``self.<field>`` READ survives).
# Therefore the masked site is ALWAYS a write to a field that is never read -> dead code
# whose ``:GetChildren()`` result is DISCARDED -> functionally INERT. A forged carrier
# can mask only an inert dead write, NEVER a live child-ref regression. (Forging the
# carrier requires tampering the internal conversion_plan.json — out of threat model.)
# ===========================================================================


def test_inert_bound_forged_carrier_can_only_skip_a_self_field_write_site() -> None:
    """INERT BOUND (i) — an attacker-CHOSEN ("forged") carrier whose cam_receiver +
    cam_ordinal are picked to MATCH a genuine survivor can cause the exemption to skip
    that survivor ONLY when the survivor is the WHOLE RHS of a ``self.<field> = ...``
    WRITE on a DISCHARGED script. We prove the structural guarantee directly: the one
    exempted survivor is a write-LHS to ``self.weaponSlot`` AND, because the script is
    discharged, NO raw read of ``self.weaponSlot`` survives -> the skipped site is dead
    code, functionally inert."""
    # A discharged source whose surviving dead init-write IS the (attacker-known)
    # credited shape. ``_discharged_with_write`` asserts discharge holds.
    src = _discharged_with_write("self.weaponSlot = self.cam:GetChildren()[1]")
    # The carrier is "forged" in the threat-model sense: cam_receiver/cam_ordinal are
    # chosen to match the survivor. (Here they happen to equal the corpus values; the
    # point is the verifier TRUSTS them, never re-derives them.)
    forged = _carrier(cam_receiver="cam", cam_ordinal=0)
    # (a) The exemption skips EXACTLY this one site (count drops to 0).
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 0
    assert not _checkD_fires(_fr3_script(src, forged))
    # (b) STRUCTURAL GUARANTEE — the exempted site's enclosing statement is a WRITE to
    #     ``self.<field>`` (the LHS), the site being its whole RHS. Assert the credited
    #     statement is present in exactly the ``self.weaponSlot = <site>`` write form.
    assert "self.weaponSlot = self.cam:GetChildren()[1]" in src
    # (c) INERT — the entry-gate required _rig_binding_discharged, so NO raw read of
    #     ``self.<field>`` survives: the write target is never read -> dead code whose
    #     :GetChildren() result is discarded. (boundary tests above prove discharge
    #     fails closed the instant any raw read survives.)
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True


def test_inert_bound_forged_carrier_cannot_mask_a_read_survivor() -> None:
    """INERT BOUND (ii) — a forged carrier naming a receiver/ordinal that match a
    survivor which is a READ (a consumption, NOT a ``self.<field> =`` write) CANNOT
    cause the exemption to skip it: _site_is_discharged_rig_dead_write requires the
    statement to OPEN with the ``self.<field>`` write-LHS, so a read survivor fails
    clause (1) -> COUNTED. This is the load-bearing half of the bound — a forged
    carrier can never mask a LIVE child-ref read regression."""
    # A discharged script PLUS a surviving READ ordinal on the SAME receiver+ordinal the
    # forged carrier names (``self.cam:GetChildren()[1]`` as an RHS the field never owns).
    src = _discharged_with_write("self.weaponSlot = self.cam:GetChildren()[1]")
    src = src.replace(
        "function Player:GetRifle()\n",
        "function Player:GetRifle()\n    local extra = self.cam:GetChildren()[1]\n",
    )
    # The forged carrier names EXACTLY this receiver+ordinal (cam / [1]).
    forged = _carrier(cam_receiver="cam", cam_ordinal=0)
    # The credited dead WRITE is exempted (one site), but the READ survivor — same
    # receiver+ordinal — is NOT: a forged carrier cannot mask it. Net: 1 survivor.
    assert _count_surviving_child_ordinals(src, _RigDeadWriteExempt(
        field_name="weaponSlot", cam_receiver="cam", cam_ordinal=0
    )) == 1
    assert _checkD_fires(_fr3_script(src, forged))


def test_inert_bound_forged_carrier_cannot_mask_a_different_lvalue_write() -> None:
    """INERT BOUND (iii) — a forged carrier whose receiver/ordinal match a survivor that
    is a WRITE to a DIFFERENT lvalue (``self.muzzle = self.cam:GetChildren()[1]``, NOT
    ``self.<field>``) CANNOT mask it: the LHS gate (_rig_exempt_lhs_re on ``self.<field>``)
    rejects a write whose lvalue is not the bound field -> COUNTED. The exemption is
    bound to a write of the rig FIELD, never an arbitrary same-RHS write."""
    src = _discharged_with_write("self.muzzle = self.cam:GetChildren()[1]")
    forged = _carrier(cam_receiver="cam", cam_ordinal=0)
    # The survivor writes ``self.muzzle`` (not ``self.weaponSlot``) -> LHS gate rejects
    # the exemption -> COUNTED, even though receiver+ordinal match the forged carrier.
    assert _count_surviving_child_ordinals(src, _CORPUS_EXEMPT) == 1
    assert _checkD_fires(_fr3_script(src, forged))


# ===========================================================================
# Dual-voice REVIEW round 2 (D-S1b-r2 BLOCKING) — DISCHARGE SOUNDNESS GAP on a
# DYNAMIC self-index read. ``_rig_binding_discharged`` only caught STATIC reads
# (dot-form ``self.<field>`` + decoded static-string bracket ``self["<field>"]``);
# it MISSED a DYNAMIC ``self[k]`` where ``k`` is a computed expression that may
# evaluate to the field name (``self["weapon".."Slot"]``; ``local k = ...; self[k]``).
# Such a script false-PASSED discharge (the field IS read dynamically, un-rerouted)
# AND, because check D's rig exemption is GATED on discharge, the exemption then
# masked the surviving GetChildren WRITE to the field — a LIVE survivor slipped
# through (``verify_contract`` -> [] at a10c76a). The fix makes discharge FAIL CLOSED
# on any un-analyzable dynamic ``self[...]`` index. Each case below is RED against
# a10c76a (the pre-fix verifier).
# ===========================================================================

# The lowered corpus base (resolver + rerouted reads + a SURVIVING dead-write to the
# field) onto which the dynamic read is injected. The skipped-neutralize single-line-
# if shape leaves ``self.weaponSlot = self.cam:GetChildren()[1]`` as a surviving WRITE
# — the exact site check D would EXEMPT if the binding (wrongly) discharged.
_R2_BASE_WITH_SURVIVING_WRITE = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF).luau_source


def _r2_inject_dynamic_read(read_stmt: str) -> str:
    """The lowered corpus base (resolver + reroute + surviving dead GetChildren write)
    with a DYNAMIC ``self[...]`` field read injected into the yielding GetRifle."""
    src = _R2_BASE_WITH_SURVIVING_WRITE.replace(
        "function Player:GetRifle()\n",
        "function Player:GetRifle()\n    " + read_stmt + "\n",
        1,
    )
    assert read_stmt in src
    # Sanity: the surviving dead GetChildren WRITE is present (the masking target).
    assert "self.weaponSlot = self.cam:GetChildren()[1]" in src
    return src


@pytest.mark.parametrize(
    "read_stmt",
    [
        'local x = self["weapon".."Slot"]',          # concat key (codex's exact repro)
        'local k = "weapon".."Slot"\n    local x = self[k]',  # var-bound concat key
    ],
)
def test_r2_dynamic_self_index_read_reds_full_verify_contract(read_stmt: str) -> None:
    """D-S1b-r2 (codex's exact repro) — a discharged-LOOKING script (resolver + reroute,
    NO static ``self.weaponSlot`` / ``self["weaponSlot"]`` read) that reads the field via
    a DYNAMIC key must now: (1) NOT discharge (``_rig_binding_discharged`` -> False);
    (2) FIRE ``rig_binding_present``; AND (3) because the exemption is discharge-gated,
    NO LONGER exempt the surviving GetChildren WRITE -> ``verify_contract`` is NOT [].
    The live survivor (the un-rerouted dynamic read + its now-counted write) is no
    longer masked."""
    src = _r2_inject_dynamic_read(read_stmt)
    # (1) discharge is now fail-closed on the un-analyzable dynamic self-index.
    assert _rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is False
    script = _rbx_with_accounting(
        "Player",
        src,
        _carrier(),  # well-formed carrier, present=True
        getchild_total=1,
        resolved_total=1,  # budget 0
    )
    res = verify_contract(_TOPOLOGY, [script])
    checks = {v.check for v in res.violations}
    # (2) the binding-present floor fires (the real un-rerouted read is surfaced).
    assert "rig_binding_present" in checks
    # (3) the discharge-gated check-D exemption does NOT apply -> the surviving
    #     GetChildren WRITE is COUNTED, not masked: the live survivor reds too.
    assert "child_ordinal_survivor" in checks
    # The full contract is NOT empty — the live survivor is no longer masked (the
    # codex repro returned [] at a10c76a).
    assert res.violations != []


def test_r2_dynamic_self_index_red_against_a10c76a() -> None:
    """D-S1b-r2 pre-fix-RED proof (narrator-INDEPENDENT) — load the ACTUAL a10c76a
    verifier blob from git and run codex's exact dynamic-read repro through ITS
    ``verify_contract``. At a10c76a the binding false-DISCHARGES (the dynamic read was
    missed) so the rig exemption masks the surviving write and ``verify_contract``
    returns NO ``rig_binding_present`` AND NO ``child_ordinal_survivor`` (the [] codex
    observed). The fixed verifier fires both. Proves the fix is load-bearing."""
    import importlib.util
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent.parent  # worktree root
    blob = subprocess.run(
        ["git", "show", "a10c76a:converter/converter/contract_verifier.py"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old_path = Path(__file__).parent / "_old_cv_r2_a10c76a.py"
    old_path.write_text(blob, encoding="utf-8")
    try:
        spec = importlib.util.spec_from_file_location("_old_cv_r2_a10c76a", str(old_path))
        assert spec is not None and spec.loader is not None
        old = importlib.util.module_from_spec(spec)
        sys.modules["_old_cv_r2_a10c76a"] = old
        spec.loader.exec_module(old)
    finally:
        old_path.unlink(missing_ok=True)

    # The VARIABLE-BOUND key is the genuine gap: a10c76a's decode-then-compare path
    # statically folds the inline ``"weapon".."Slot"`` concat (so it already caught the
    # concat-literal form), but it CANNOT see through a ``local k = ...; self[k]`` — the
    # key is a bare variable that decodes to None -> false-discharge there.
    src = _r2_inject_dynamic_read('local k = "weapon".."Slot"\n    local x = self[k]')
    script = _rbx_with_accounting(
        "Player", src, _carrier(), getchild_total=1, resolved_total=1
    )
    # PRE-FIX (real a10c76a): the dynamic read is missed -> false-discharge -> the rig
    # exemption masks the surviving write -> neither floor nor survivor fires.
    old_res = old.verify_contract(_TOPOLOGY, [script])
    old_checks = {v.check for v in old_res.violations}
    assert old._rig_binding_discharged(src, "weaponSlot", "WeaponSlot") is True, (
        "a10c76a must false-DISCHARGE the dynamic read (proving the gap)"
    )
    assert "rig_binding_present" not in old_checks
    assert "child_ordinal_survivor" not in old_checks, (
        "a10c76a's discharge-gated exemption must MASK the live survivor (the [] codex "
        "reproduced) — proving the fix is load-bearing"
    )
    # POST-FIX: discharge fails closed; both the floor and the survivor fire.
    new_res = verify_contract(_TOPOLOGY, [script])
    new_checks = {v.check for v in new_res.violations}
    assert "rig_binding_present" in new_checks
    assert "child_ordinal_survivor" in new_checks


def test_r2_corpus_dot_form_reads_still_discharge_and_exempt() -> None:
    """D-S1b-r2 REGRESSION GUARD — the strengthened discharge must NOT regress the REAL
    corpus shape: the Player reads the field via DOT-form ``self.weaponSlot`` (rerouted
    by the lowering) with NO dynamic self-index. Discharge stays True, the binding-
    present check is GREEN, and check D's dead-write exemption still applies (no
    false positive on the discharged dead write)."""
    lowered = _lower(_SKIPPED_NEUTRALIZE_SINGLELINE_IF)
    # No dynamic self-index in the real corpus output.
    assert _rig_binding_discharged(lowered.luau_source, "weaponSlot", "WeaponSlot") is True
    script = _rbx_with_accounting(
        "Player",
        lowered.luau_source,
        lowered.rig_binding,
        getchild_total=1,
        resolved_total=1,
    )
    res = verify_contract(_TOPOLOGY, [script])
    checks = {v.check for v in res.violations}
    assert "rig_binding_present" not in checks  # discharge stayed True
    assert "child_ordinal_survivor" not in checks  # the dead-write exemption still applies
