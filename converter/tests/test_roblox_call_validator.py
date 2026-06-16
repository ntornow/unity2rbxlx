"""Tests for the provenance-gated Roblox-call validator (slice 1.2).

The load-bearing test is :func:`test_zero_proven_false_positives`: across the
entire frozen fixture corpus of real converted output, the ONLY ``proven``
invalid calls must be exactly the two ``FindFirstChildOfType`` bug sites. This
encodes the design's correctness guarantee — do NOT loosen it to pass; fix the
provenance logic (or escalate) if it breaks.
"""

from __future__ import annotations

import glob
import os

from converter.roblox_call_validator import find_invalid_roblox_calls

_FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "roblox_calls")

# The two known bug sites (path suffix, method) — proven FindFirstChildOfType.
_BUG_SITES = {
    ("drive-door-generic/TurretBullet.luau", "FindFirstChildOfType"),
    ("drive-door-generic/PlaneBullet.luau", "FindFirstChildOfType"),
}


def _all_fixture_files() -> list[str]:
    return sorted(
        glob.glob(os.path.join(_FIXTURE_DIR, "**", "*.luau"), recursive=True)
    )


def _rel(path: str) -> str:
    return os.path.relpath(path, _FIXTURE_DIR)


def test_fixture_corpus_is_present() -> None:
    files = _all_fixture_files()
    # drive-door-generic (42) + trash-dash (120).
    assert len(files) >= 160, f"expected >=160 fixture scripts, got {len(files)}"


def test_two_bug_sites_caught_as_proven() -> None:
    """Both FindFirstChildOfType bug sites are flagged proven with the fix."""
    found: set[tuple[str, str]] = set()
    for path in _all_fixture_files():
        src = open(path, encoding="utf-8").read()
        for ic in find_invalid_roblox_calls(src):
            if ic["method"] != "FindFirstChildOfType":
                continue
            assert ic["receiver_provenance"] == "proven", (
                f"{_rel(path)}: FindFirstChildOfType should be proven"
            )
            assert ic["suggested_fix"] == "FindFirstChildWhichIsA", (
                f"{_rel(path)}: expected suggested_fix"
            )
            found.add((_rel(path), ic["method"]))
    assert found == _BUG_SITES, f"bug sites mismatch: {found}"


def test_zero_proven_false_positives() -> None:
    """LOAD-BEARING: the only proven invalids in the whole corpus are the 2 bugs.

    Iterate every fixture, collect every ``proven`` InvalidCall, and assert the
    set of (file, method) equals exactly the two bug sites. Any extra proven
    invalid is a false positive (or a real new bug) and must be reported, not
    suppressed.
    """
    proven: set[tuple[str, str]] = set()
    for path in _all_fixture_files():
        src = open(path, encoding="utf-8").read()
        for ic in find_invalid_roblox_calls(src):
            if ic["receiver_provenance"] == "proven":
                proven.add((_rel(path), ic["method"]))
    assert proven == _BUG_SITES, (
        "proven invalids must be exactly the 2 bug sites; got: "
        f"{sorted(proven)}"
    )


# --- Targeted inline-snippet unit tests ------------------------------------


def _methods(src: str) -> list[tuple[str, str, str | None]]:
    return [
        (ic["method"], ic["receiver_provenance"], ic["suggested_fix"])
        for ic in find_invalid_roblox_calls(src)
    ]


def test_plr_character_inline_proven() -> None:
    out = _methods('plr.Character:FindFirstChildOfType("Humanoid")')
    assert out == [("FindFirstChildOfType", "proven", "FindFirstChildWhichIsA")]


def test_plr_character_aliased_proven() -> None:
    src = 'local char = plr.Character\nchar:FindFirstChildOfType("Humanoid")'
    out = _methods(src)
    assert out == [("FindFirstChildOfType", "proven", "FindFirstChildWhichIsA")]


def test_host_signal_call_skipped() -> None:
    out = _methods("self.host:connectGameObjectSignal(a, b, c)")
    assert out == []


def test_host_dotted_call_skipped() -> None:
    out = _methods("self.host.foo:Bar()")
    assert out == []


def test_host_result_receiver_not_proven() -> None:
    src = 'local gm = self.host.findObjectOfType("GameManager")\ngm:RestartGame(5)'
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["method"] == "RestartGame"
    # Must NOT be proven — the receiver derives from a host result.
    assert out[0]["receiver_provenance"] != "proven"


def test_workspace_valid_method_no_invalid() -> None:
    out = _methods('workspace:FindFirstChild("X")')
    assert out == []


def test_humanoid_takedamage_valid() -> None:
    src = 'local humanoid = char:FindFirstChildWhichIsA("Humanoid")\nhumanoid:TakeDamage(10)'
    out = _methods(src)
    assert out == []


def test_proven_char_getpivot_valid() -> None:
    src = "local char = plr.Character\nchar:GetPivot()"
    out = _methods(src)
    assert out == []


# --- FINDING 1: component-table base must NOT be promoted to proven ----------


def test_component_base_parent_field_not_proven() -> None:
    """``gm.Parent:CustomMethod()`` where gm is a component is NOT proven."""
    src = (
        'local gm = self.host.findObjectOfType("GameManager")\n'
        "gm.Parent:CustomMethod()"
    )
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["method"] == "CustomMethod"
    assert out[0]["receiver_provenance"] != "proven"


def test_component_base_character_field_not_proven() -> None:
    """``enemy.Character:Damage(5)`` where enemy is a getComponent is NOT proven."""
    src = "local enemy = self.host.getComponent(self, Foo)\nenemy.Character:Damage(5)"
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["method"] == "Damage"
    assert out[0]["receiver_provenance"] != "proven"


def test_self_getcomponent_base_not_proven() -> None:
    """``c.Instance:Foo()`` where c is ``self:GetComponent(...)`` is NOT proven."""
    src = 'local c = self:GetComponent("Rigidbody")\nc.Instance:Foo()'
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["method"] == "Foo"
    assert out[0]["receiver_provenance"] != "proven"


def test_addcomponent_base_not_proven() -> None:
    src = "local c = self.host.addComponent(go, id, cfg)\nc.Parent:Foo()"
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["method"] == "Foo"
    assert out[0]["receiver_provenance"] != "proven"


def test_playerfromtouch_character_still_proven_regression() -> None:
    """REGRESSION GUARD: playerFromTouch returns a Player (Roblox), NOT a
    component, so ``plr.Character`` stays proven and the bug is still caught."""
    src = (
        "local plr = self.host.playerFromTouch(other)\n"
        "local char = plr.Character\n"
        'char:FindFirstChildOfType("Humanoid")'
    )
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["method"] == "FindFirstChildOfType"
    assert out[0]["receiver_provenance"] == "proven"
    assert out[0]["suggested_fix"] == "FindFirstChildWhichIsA"


def test_untracked_base_parent_may_stay_proven() -> None:
    """An UNtracked base (not a known component local) still promotes via the
    field rule — only TRACKED component locals are de-promoted."""
    out = _methods("node.Parent:CustomMethod()")
    assert out == [("CustomMethod", "proven", None)]


def test_findgameobject_base_still_proven() -> None:
    """findGameObject returns a Roblox Instance (NOT a component) -> proven."""
    src = 'local go = self.host.findGameObject("Door")\ngo.Parent:Custom()'
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["receiver_provenance"] == "proven"


# --- FINDING 2: multiline method chain must NOT downgrade proven->unproven ---


def test_multiline_chain_stays_proven() -> None:
    """A chain split across lines keeps the prior line's receiver provenance."""
    src = 'workspace:FindFirstChild("X")\n  :FakeMethod()'
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["method"] == "FakeMethod"
    assert out[0]["receiver_provenance"] == "proven"


def test_multiline_findfirstchildoftype_bug_proven() -> None:
    """The multiline form of the bug site is still caught as proven."""
    src = 'plr.Character\n  :FindFirstChildOfType("Humanoid")'
    out = find_invalid_roblox_calls(src)
    assert len(out) == 1
    assert out[0]["method"] == "FindFirstChildOfType"
    assert out[0]["receiver_provenance"] == "proven"
    assert out[0]["suggested_fix"] == "FindFirstChildWhichIsA"


# --- Isolated provenance-origin coverage (each design-listed PROVEN origin) ---
# The corpus zero-FP test exercises these against real usage; these isolated
# unit tests pin each rule individually so a regression in one origin can't hide
# behind another's coverage.


def test_instance_new_result_proven() -> None:
    """``Instance.new("Part")`` result is a Roblox instance -> proven."""
    out = _methods('Instance.new("Part"):FakeMethod()')
    assert out == [("FakeMethod", "proven", None)]


def test_instance_new_aliased_proven() -> None:
    """A local bound from ``Instance.new(...)`` is proven."""
    src = 'local p = Instance.new("Part")\np:FakeMethod()'
    out = _methods(src)
    assert out == [("FakeMethod", "proven", None)]


def test_instance_new_valid_method_no_invalid() -> None:
    src = 'local p = Instance.new("Part")\np:Destroy()'
    assert _methods(src) == []


def test_getservice_result_proven() -> None:
    """``game:GetService("Players")`` result is a Roblox service -> proven."""
    out = _methods('game:GetService("Players"):FakeMethod()')
    assert out == [("FakeMethod", "proven", None)]


def test_getservice_aliased_proven() -> None:
    src = 'local s = game:GetService("Players")\ns:FakeMethod()'
    out = _methods(src)
    assert out == [("FakeMethod", "proven", None)]


def test_getservice_valid_method_no_invalid() -> None:
    assert _methods('game:GetService("Players"):GetChildren()') == []


def test_raycast_instance_field_proven() -> None:
    """``workspace:Raycast(a,b).Instance`` is a Roblox instance -> proven."""
    out = _methods("workspace:Raycast(a, b).Instance:FakeMethod()")
    assert out == [("FakeMethod", "proven", None)]


def test_raycast_instance_aliased_proven() -> None:
    src = "local r = workspace:Raycast(a, b)\nr.Instance:FakeMethod()"
    out = _methods(src)
    assert out == [("FakeMethod", "proven", None)]


def test_getchildren_loop_var_proven() -> None:
    """A ``GetChildren()`` loop value var is proven."""
    src = "for _, c in workspace:GetChildren() do c:FakeMethod() end"
    out = _methods(src)
    assert out == [("FakeMethod", "proven", None)]


def test_getdescendants_loop_var_proven() -> None:
    src = "for _, c in workspace:GetDescendants() do c:FakeMethod() end"
    out = _methods(src)
    assert out == [("FakeMethod", "proven", None)]


def test_getpartboundsinradius_loop_var_proven() -> None:
    src = "for _, c in workspace:GetPartBoundsInRadius(a, b) do c:FakeMethod() end"
    out = _methods(src)
    assert out == [("FakeMethod", "proven", None)]


def test_loop_var_valid_method_no_invalid() -> None:
    src = "for _, c in workspace:GetChildren() do c:Destroy() end"
    assert _methods(src) == []


def test_self_gameobject_proven() -> None:
    """``self.gameObject`` is the script's GameObject (Roblox) -> proven."""
    out = _methods("self.gameObject:FakeMethod()")
    assert out == [("FakeMethod", "proven", None)]


def test_self_gameobject_valid_method_no_invalid() -> None:
    assert _methods("self.gameObject:GetChildren()") == []


def test_script_proven() -> None:
    """REGRESSION: bare ``script`` is a Roblox global -> proven.

    Previously the bare-identifier tracked-lookup intercepted ``script`` (and
    ``workspace``) before the global-origin branch, returning the default
    ``unproven``.
    """
    out = _methods("script:FakeMethod()")
    assert out == [("FakeMethod", "proven", None)]


def test_script_parent_proven() -> None:
    out = _methods("script.Parent:FakeMethod()")
    assert out == [("FakeMethod", "proven", None)]


def test_script_valid_method_no_invalid() -> None:
    assert _methods("script:GetChildren()") == []


def test_workspace_bare_hallucinated_proven() -> None:
    """REGRESSION: bare ``workspace`` global -> proven (same root bug as script)."""
    out = _methods("workspace:FakeMethod()")
    assert out == [("FakeMethod", "proven", None)]
