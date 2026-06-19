"""Phase-1 verifier tests — ``_check_equip_present`` (request-scoped, fail-closed).

Covers acceptance criteria 5, 6 (design-phase1.md §3):
  5  VERIFIER fails closed when a recognized obligation is UNDISCHARGED — a script
     whose equip_binding obligates a request the source does not carry (or a
     present=True mis-stamp) -> exactly one ``equip_present`` violation, promoted by
     ``fail_closed_errors``. RED proof: passes WITHOUT the check, fails WITH.
  6  VERIFIER ABSTAINS on ``equip_binding=None`` (no equip obligation) — no rows.

The GREEN/discharged source is produced by S1's REAL lowering on the verbatim
fixture body, so the verifier's independent scan is tested against the actual
lowered output, not a synthetic guess.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from converter import contract_verifier  # noqa: E402
from converter.camera_mount_equip_lowering import (  # noqa: E402
    lower_camera_mount_equip,
)
from converter.child_ref_resolver import (  # noqa: E402
    ChildRefScript,
    RigRootedRetargetFact,
)
from converter.contract_verifier import (  # noqa: E402
    FAIL_CLOSED_CHECKS,
    _check_equip_present,
    _equip_request_discharged,
    fail_closed_errors,
    verify_contract,
)
from core.roblox_types import RbxScript  # noqa: E402

_TOPOLOGY = {"modules": {"Player": {"stem": "Player"}}}


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


class _Script:
    def __init__(self, src: str, path: str = "/proj/Player.cs") -> None:
        self.luau_source = src
        self.source_path = path
        self.equip_binding: dict[str, object] | None = None


def _lowered_player() -> _Script:
    s = _Script(_VERBATIM_GETRIFLE)
    fact = RigRootedRetargetFact(
        field_name="weaponSlot", child_name="WeaponSlot", cam_receiver="cam",
        equip_method="GetRifle", prefab_field="riflePrefab",
    )
    crs = ChildRefScript(
        facts=(), getchild_total=1, resolved_total=1, rig_facts=(fact,))
    lower_camera_mount_equip([s], {"/proj/Player.cs": crs})
    return s


def _carrier(present: bool = True, **extra: object) -> dict[str, object]:
    c: dict[str, object] = {
        "prefab": "riflePrefab", "method": "GetRifle",
        "remote": "equipWeaponRemote", "present": present,
    }
    c.update(extra)
    return c


def _rbx(name: str, source: str, eb: dict[str, object] | None) -> RbxScript:
    return RbxScript(name=name, source=source, equip_binding=eb)


def _equip_rows(scripts: list[RbxScript]) -> list:
    res = verify_contract(_TOPOLOGY, scripts)
    return [v for v in res.violations if v.check == "equip_present"]


# === Sanity / membership ====================================================


def test_equip_present_in_fail_closed_set() -> None:
    assert "equip_present" in FAIL_CLOSED_CHECKS


def test_real_lowering_discharges_and_verifier_green() -> None:
    s = _lowered_player()
    assert s.equip_binding is not None and s.equip_binding["present"] is True
    script = _rbx("Player", s.luau_source, s.equip_binding)
    assert _equip_rows([script]) == []
    res = verify_contract(_TOPOLOGY, [script])
    assert not any("equip_present" in e for e in fail_closed_errors(res))


# === Criterion 5 — VERIFIER fails closed on an UNDISCHARGED obligation =======


def test_c5_red_when_request_absent_present_false() -> None:
    # The un-lowered AI shape (still has instantiatePrefab, no request) + honest
    # present=False stamp the lowering would emit on an abstained discharge.
    script = _rbx("Player", _VERBATIM_GETRIFLE, _carrier(present=False))
    rows = _equip_rows([script])
    assert len(rows) == 1
    assert rows[0].severity == "warning"
    res = verify_contract(_TOPOLOGY, [script])
    errs = fail_closed_errors(res)
    assert any("equip_present" in e for e in errs)
    assert any("Player" in e for e in errs)


def test_c5_independence_forged_stamp_true_but_source_undischarged() -> None:
    # The forged/stale-resume case: carrier claims present=True but the source never
    # got the request -> the INDEPENDENT scan returns False -> the check STILL fires.
    script = _rbx("Player", _VERBATIM_GETRIFLE, _carrier(present=True))
    rows = _equip_rows([script])
    assert len(rows) == 1
    assert "discharged=False" in rows[0].detail
    assert "lowering-stamp=True" in rows[0].detail


def test_c5_independence_stamp_false_but_source_discharged_also_fires() -> None:
    # The OTHER mis-stamp direction: source IS discharged but carrier says
    # present=False -> PASS requires discharged AND stamp -> disagreement fires.
    s = _lowered_player()
    script = _rbx("Player", s.luau_source, _carrier(present=False))
    rows = _equip_rows([script])
    assert len(rows) == 1
    assert "discharged=True" in rows[0].detail
    assert "lowering-stamp=False" in rows[0].detail


def test_c5_multi_site_carrier_fails_closed() -> None:
    # The lowering's multi_site overflow carrier (present=False) reds.
    script = _rbx(
        "Player", _VERBATIM_GETRIFLE, _carrier(present=False, multi_site=True))
    assert len(_equip_rows([script])) == 1


def test_c5_dangling_capvar_carrier_fails_closed() -> None:
    script = _rbx(
        "Player", _VERBATIM_GETRIFLE, _carrier(present=False, dangling_capvar=True))
    assert len(_equip_rows([script])) == 1


def test_c5_pre_fix_red_proof(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dropped-request scenario that MUST fail closed.
    script = _rbx("Player", _VERBATIM_GETRIFLE, _carrier(present=False))

    # WITH the check (current code): the dropped request is caught.
    errs_with = fail_closed_errors(verify_contract(_TOPOLOGY, [script]))
    assert any("equip_present" in e for e in errs_with), (
        "the check must catch the dropped equip request"
    )

    # WITHOUT the check: stub _check_equip_present to a no-op. The
    # dropped request now sails through -> the assertion above would FAIL against
    # this no-op, proving the check is load-bearing, not a tautology.
    monkeypatch.setattr(
        contract_verifier, "_check_equip_present", lambda *a, **k: []
    )
    errs_without = fail_closed_errors(verify_contract(_TOPOLOGY, [script]))
    assert not any("equip_present" in e for e in errs_without), (
        "code with the check absent must NOT catch the dropped request"
    )


def test_c5_check_directly_returns_rows_on_dropped_request() -> None:
    script = _rbx("Player", _VERBATIM_GETRIFLE, _carrier(present=False))
    rows = _check_equip_present(_TOPOLOGY, [script])
    assert len(rows) == 1
    assert rows[0].check == "equip_present"
    assert rows[0].severity == "warning"


# === Criterion 6 — VERIFIER ABSTAINS on equip_binding=None ==================


def test_c6_abstain_on_none_carrier() -> None:
    # A script with NO equip obligation -> no rows, even if its source mentions
    # instantiatePrefab.
    script = _rbx("Player", _VERBATIM_GETRIFLE, None)
    assert _equip_rows([script]) == []


def test_c6_abstain_on_empty_carrier() -> None:
    script = _rbx("Player", _VERBATIM_GETRIFLE, {})
    assert _equip_rows([script]) == []


# === Method-scoping: a same-prefab spawn in ANOTHER method neither helps nor breaks


def test_method_scoped_discharge_ignores_other_method_instantiate() -> None:
    # The recognized equip_method (GetRifle) IS discharged, but an unrelated method
    # also spawns the same prefab. The discharge scan is scoped to GetRifle, so the
    # other method's instantiate does NOT break discharge.
    s = _lowered_player()
    src = s.luau_source.replace(
        "return Player\n",
        "function Player:Other()\n"
        "    local x = self.host.instantiatePrefab(self.riflePrefab, here)\n"
        "end\n"
        "return Player\n",
    )
    assert _equip_request_discharged(src, "riflePrefab", "equipWeaponRemote", "GetRifle") is True
    script = _rbx("Player", src, _carrier(present=True))
    assert _equip_rows([script]) == []


def test_discharge_scan_directly_false_on_undischarged() -> None:
    assert _equip_request_discharged(
        _VERBATIM_GETRIFLE, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is False
    s = _lowered_player()
    assert _equip_request_discharged(
        s.luau_source, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is True


# === Discharge is REMOTE-bound (FireServer on a foreign remote fails) ===


# A method that carries the own-emit marker + a same-prefab FireServer, BUT the
# alias is bound to a DIFFERENT service and the prefab is fired on THAT alias.
# Discharge ties the FireServer to the carrier's own-remote binding, so a bare
# marker + a foreign-remote FireServer must FAIL discharge.
_FOREIGN_REMOTE_GETRIFLE = """local Player = {}
Player.__index = Player

function Player:GetRifle()
    -- _EQUIP_REQUEST_riflePrefab (auto: camera-mount equip lowered to server request)
    local otherRemote = self._services and self._services.notEquipRemote
    if otherRemote and otherRemote.FireServer then
        otherRemote:FireServer("riflePrefab")
    end
    self.gotWeapon = true
end

return Player
"""


def test_p1_1_fireserver_on_foreign_remote_fails_discharge() -> None:
    # Marker + a same-prefab FireServer present, but fired on an alias bound to a
    # NON-equip remote -> NOT discharged.
    assert _equip_request_discharged(
        _FOREIGN_REMOTE_GETRIFLE, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is False
    # And it fails the full verifier even with a forged present=True stamp.
    script = _rbx("Player", _FOREIGN_REMOTE_GETRIFLE, _carrier(present=True))
    rows = _equip_rows([script])
    assert len(rows) == 1
    assert "discharged=False" in rows[0].detail


def test_p1_1_carrier_remote_threaded_to_predicate() -> None:
    # The real lowered output discharges only when checked against ITS own remote
    # (equipWeaponRemote). Checking against a DIFFERENT remote name (as if the
    # carrier recorded another remote) must NOT discharge — proving the carrier's
    # `remote` is load-bearing, not ignored.
    s = _lowered_player()
    assert _equip_request_discharged(
        s.luau_source, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is True
    assert _equip_request_discharged(
        s.luau_source, "riflePrefab", "someOtherRemote", "GetRifle"
    ) is False


def test_p1_1_marker_present_but_no_alias_binding_fails() -> None:
    # Marker + a bare FireServer with NO `local <alias> = self._services...` binding
    # at all -> not discharged (the request must fire on a proven own-remote alias).
    src = """function Player:GetRifle()
    -- _EQUIP_REQUEST_riflePrefab (auto: camera-mount equip lowered to server request)
    someGlobal:FireServer("riflePrefab")
    self.gotWeapon = true
end
return Player
"""
    assert _equip_request_discharged(
        src, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is False


# === alias-SHADOW bypass — discharge anchors on the contiguous block ===

# The marker + an own-remote binding land, but the FIRE that actually carries the
# prefab is on a SHADOWING `local _equipRemote = self._services.notEquipRemote`
# rebind to a FOREIGN remote, OUTSIDE the marked block. Discharge scopes the
# binding+fire to the marker's CONTIGUOUS block, so the out-of-block shadow fire
# (the only fire of the prefab) cannot satisfy discharge.
_ALIAS_SHADOW_GETRIFLE = """local Player = {}
Player.__index = Player

function Player:GetRifle()
    -- _EQUIP_REQUEST_riflePrefab (auto: camera-mount equip lowered to server request)
    local _equipRemote = self._services and self._services.equipWeaponRemote
    if _equipRemote and _equipRemote.FireServer then
        local _ = _equipRemote
    end
    -- shadowing rebind to a FOREIGN remote + the real fire, OUTSIDE the marked block
    local _equipRemote = self._services and self._services.notEquipRemote
    _equipRemote:FireServer("riflePrefab")
    self.gotWeapon = true
end

return Player
"""


def test_p1_alias_shadow_outside_block_fails_discharge() -> None:
    src = _ALIAS_SHADOW_GETRIFLE
    # Sanity: the marked block carries the own-remote binding; the FOREIGN shadow +
    # the only prefab fire live OUTSIDE the block, reusing the `_equipRemote` name.
    assert "self._services.equipWeaponRemote" in src  # own-remote binding (in block)
    assert "self._services.notEquipRemote" in src      # foreign shadow (out of block)
    assert src.count("local _equipRemote") == 2
    # The own-remote fire is NOT inside the marked block; the only fire is the foreign
    # shadow -> NOT discharged.
    assert _equip_request_discharged(
        src, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is False
    # And the full verifier reds even with a forged present=True stamp.
    script = _rbx("Player", src, _carrier(present=True))
    rows = _equip_rows([script])
    assert len(rows) == 1
    assert "discharged=False" in rows[0].detail


def test_p1_real_contiguous_block_still_discharges() -> None:
    # The genuine lowered output (the marker's own block carries binding+fire) MUST
    # still pass — the block-scoping must not regress the real corpus shape.
    s = _lowered_player()
    assert _equip_request_discharged(
        s.luau_source, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is True


# === verifier span is SYMMETRIC with the producer on duplicates ===
# The producer (`camera_mount_equip_lowering._method_body_span`) fails closed
# (returns None) on >1 same-named `function …:<method>(` declaration, so the lowering
# refuses to pick an ambiguous rewrite site. The verifier's `_equip_method_body_span`
# must do the SAME — otherwise it could discharge against the FIRST body of a
# duplicated `GetRifle` (a fail-closed gate weaker than its producer). These tests
# drive the VERIFIER path (`_equip_method_body_span` / `_equip_request_discharged` /
# `_check_equip_present`), not the producer helper directly.


def _duplicate_getrifle(src: str) -> str:
    # Append a SECOND `function Player:GetRifle(...)` so two same-named methods exist.
    # The first body is the genuine discharged one; the second is an inert stub. A
    # verifier that returns the FIRST span would still discharge -> the test only
    # goes RED once the span fails closed on the duplicate.
    return src.replace(
        "return Player\n",
        "function Player:GetRifle()\n"
        "    self.gotWeapon = true\n"
        "end\n"
        "return Player\n",
    )


def test_p1b_verifier_span_none_on_duplicate_method() -> None:
    # The verifier's own span helper must return None on a duplicated method.
    s = _lowered_player()
    dup = _duplicate_getrifle(s.luau_source)
    assert dup.count("function Player:GetRifle") == 2
    assert contract_verifier._equip_method_body_span(dup, "GetRifle") is None
    # Single-method source still spans (the producer admits exactly one).
    assert contract_verifier._equip_method_body_span(s.luau_source, "GetRifle") is not None


def test_p1b_discharge_false_on_duplicate_even_if_first_body_discharged() -> None:
    # The genuine discharged body is FIRST. With the symmetric fail-closed span, the
    # duplicate -> None span -> NOT discharged.
    s = _lowered_player()
    dup = _duplicate_getrifle(s.luau_source)
    # Sanity: the single-method form genuinely discharges (so the duplicate is the
    # ONLY thing turning discharge off).
    assert _equip_request_discharged(
        s.luau_source, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is True
    assert _equip_request_discharged(
        dup, "riflePrefab", "equipWeaponRemote", "GetRifle"
    ) is False


def test_p1b_verifier_fails_closed_on_duplicate_method_carrier_present() -> None:
    # Full verifier path: a present=True carrier whose method is duplicated must FIRE
    # the fail-closed violation (ambiguous method = NOT discharged), not silently pass
    # against the first body.
    s = _lowered_player()
    dup = _duplicate_getrifle(s.luau_source)
    script = _rbx("Player", dup, _carrier(present=True))
    rows = _equip_rows([script])
    assert len(rows) == 1
    assert "discharged=False" in rows[0].detail
    res = verify_contract(_TOPOLOGY, [script])
    assert any("equip_present" in e for e in fail_closed_errors(res))


# === equip_binding carrier load/serialize round-trip (resume path) ======
# ``_load_equip_binding_for_rehydration`` (pipeline.py) + the serialize-save have
# zero coverage while rig_binding/roster_binding both have loader tests. Mirrors
# ``test_rifle_rig_retarget.test_r3_rehydrate_*``: a well-formed row restored
# verbatim; partial/malformed dropped to {} (the safe abstain default); missing
# plan -> {}; sub-flags preserved.


def _pipeline_for_rehydrate(out_dir: Path):
    from converter.pipeline import Pipeline
    p = Pipeline.__new__(Pipeline)
    p.output_dir = out_dir  # the only attr _load_equip_binding_for_rehydration reads
    return p


def _write_equip_plan(out_dir: Path, equip_binding: dict) -> None:
    import json as _json
    (out_dir / "conversion_plan.json").write_text(
        _json.dumps({"equip_binding": equip_binding}), encoding="utf-8")


def test_p2_rehydrate_load_preserves_wellformed_row(tmp_path: Path) -> None:
    # A well-formed 4-key carrier round-trips intact (the discharge anchor + stamp
    # survive a preserve/resume assemble).
    carrier = {
        "prefab": "riflePrefab", "method": "GetRifle",
        "remote": "equipWeaponRemote", "present": True,
    }
    _write_equip_plan(tmp_path, {"Player": carrier})
    p = _pipeline_for_rehydrate(tmp_path)
    assert p._load_equip_binding_for_rehydration() == {"Player": carrier}


def test_p2_rehydrate_present_false_subflags_preserved(tmp_path: Path) -> None:
    # A present=False fail-closed carrier MUST survive intact (incl. the
    # multi_site/dangling_capvar sub-flags) so the verifier fires loud on resume —
    # NOT get dropped to None (which would silently abstain).
    for flag in ("multi_site", "dangling_capvar"):
        carrier = {
            "prefab": "riflePrefab", "method": "GetRifle",
            "remote": "equipWeaponRemote", "present": False, flag: True,
        }
        _write_equip_plan(tmp_path, {"Player": carrier})
        p = _pipeline_for_rehydrate(tmp_path)
        assert p._load_equip_binding_for_rehydration() == {"Player": carrier}, flag


def test_p2_rehydrate_drops_partial_row_missing_core_key(tmp_path: Path) -> None:
    # A partial carrier (missing ``remote``) is dropped -> {} -> the verifier abstains
    # (the safe default), NEVER a partial carrier that would anchor on a missing key.
    _write_equip_plan(tmp_path, {"Player": {
        "prefab": "riflePrefab", "method": "GetRifle", "present": True}})
    p = _pipeline_for_rehydrate(tmp_path)
    assert p._load_equip_binding_for_rehydration() == {}


def test_p2_rehydrate_drops_malformed_present(tmp_path: Path) -> None:
    # ``present`` must be a real bool (not a truthy string / int) — a malformed value
    # drops the whole row.
    for bad in ["true", 1, None, 0]:
        _write_equip_plan(tmp_path, {"Player": {
            "prefab": "riflePrefab", "method": "GetRifle",
            "remote": "equipWeaponRemote", "present": bad}})
        p = _pipeline_for_rehydrate(tmp_path)
        assert p._load_equip_binding_for_rehydration() == {}, bad


def test_p2_rehydrate_drops_nonstr_prefab(tmp_path: Path) -> None:
    _write_equip_plan(tmp_path, {"Player": {
        "prefab": 123, "method": "GetRifle",
        "remote": "equipWeaponRemote", "present": True}})
    p = _pipeline_for_rehydrate(tmp_path)
    assert p._load_equip_binding_for_rehydration() == {}


def test_p2_rehydrate_missing_plan_returns_empty(tmp_path: Path) -> None:
    # No conversion_plan.json on disk -> {} (a fresh-transpile / no-resume run).
    p = _pipeline_for_rehydrate(tmp_path)
    assert p._load_equip_binding_for_rehydration() == {}


def test_p2_rehydrate_plan_without_equip_block_returns_empty(tmp_path: Path) -> None:
    # A plan that pre-dates the equip_binding field (no ``equip_binding`` key) -> {}.
    import json as _json
    (tmp_path / "conversion_plan.json").write_text(
        _json.dumps({"rig_binding": {}}), encoding="utf-8")
    p = _pipeline_for_rehydrate(tmp_path)
    assert p._load_equip_binding_for_rehydration() == {}
