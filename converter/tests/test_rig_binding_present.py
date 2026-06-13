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
    assert lowered.rig_binding == {
        "field": "weaponSlot",
        "child": "WeaponSlot",
        "present": True,
    }
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
    assert lowered.rig_binding == {
        "field": "torchMount",
        "child": "TorchAnchor",
        "present": True,
    }
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
