"""Check D (``child_ordinal_survivor``) — FACT-BASED backstop tests.

Per AC (b): a FULLY-resolved ({n,n}) RbxScript with a surviving positional
ordinal (adjacent OR two-line factored) FIRES ``child_ordinal_survivor`` and
``fail_closed_errors`` promotes it; an UNRESOLVED ({1,0}) Player-cam-shaped
script with a surviving ordinal yields ONLY a non-promoting ``info``
``child_ordinal_coverage_gap`` row; a clean fully-resolved script and an
absent-field script yield ZERO rows from check D.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest  # noqa: E402

from converter import contract_verifier  # noqa: E402
from converter.contract_verifier import (  # noqa: E402
    FAIL_CLOSED_CHECKS,
    _receiver_roots_at_engine_global,
    fail_closed_errors,
    verify_contract,
)
from core.roblox_types import RbxScript  # noqa: E402

# A non-empty topology so the smoke check stays quiet.
_TOPOLOGY = {"modules": {"Turret": {"stem": "Turret"}}}


def _check_d(scripts: list[RbxScript]) -> list:
    """Run only check D's rows out of verify_contract (filter by its checks)."""
    res = verify_contract(_TOPOLOGY, scripts)
    return [
        v for v in res.violations
        if v.check in ("child_ordinal_survivor", "child_ordinal_coverage_gap")
    ]


def test_child_ordinal_survivor_in_fail_closed_set() -> None:
    assert "child_ordinal_survivor" in FAIL_CLOSED_CHECKS
    assert "child_ordinal_coverage_gap" not in FAIL_CLOSED_CHECKS


def test_fully_resolved_with_survivor_fires_and_promotes() -> None:
    s = RbxScript(
        name="Turret",
        source="local b = base:GetChildren()[1]\nreturn b",
        child_ref_resolution={"getchild_total": 3, "resolved_total": 3},
    )
    res = verify_contract(_TOPOLOGY, [s])
    survivors = [v for v in res.violations if v.check == "child_ordinal_survivor"]
    assert len(survivors) == 1
    assert survivors[0].severity == "warning"
    # fail_closed_errors promotes it -> conversion would report success False.
    errs = fail_closed_errors(res)
    assert any("child_ordinal_survivor" in e for e in errs)


def test_fully_resolved_two_line_factored_fires() -> None:
    s = RbxScript(
        name="Turret",
        source=(
            "local kids = base:GetChildren()\n"
            "local first = kids[1]\n"
            "return first"
        ),
        child_ref_resolution={"getchild_total": 3, "resolved_total": 3},
    )
    survivors = [v for v in _check_d([s]) if v.check == "child_ordinal_survivor"]
    assert len(survivors) == 1


def test_fully_resolved_clean_yields_zero() -> None:
    s = RbxScript(
        name="Turret",
        source='local b = base:FindFirstChild("Base")\nreturn b',
        child_ref_resolution={"getchild_total": 3, "resolved_total": 3},
    )
    assert _check_d([s]) == []


def test_unresolved_player_cam_only_info_not_promoted() -> None:
    s = RbxScript(
        name="Player",
        source="local slot = self.cam:GetChildren()[1]\nreturn slot",
        child_ref_resolution={"getchild_total": 1, "resolved_total": 0},
    )
    res = verify_contract(_TOPOLOGY, [s])
    survivors = [v for v in res.violations if v.check == "child_ordinal_survivor"]
    gaps = [v for v in res.violations if v.check == "child_ordinal_coverage_gap"]
    assert survivors == []
    assert len(gaps) == 1
    assert gaps[0].severity == "info"
    # The info row is NOT promoted to a fail-closed error.
    assert fail_closed_errors(res) == []


def test_absent_field_abstains_no_rows() -> None:
    # Pre-field fixture: no child_ref_resolution -> pure abstain, zero rows.
    s = RbxScript(
        name="Old",
        source="local b = base:GetChildren()[1]\nreturn b",
    )
    assert _check_d([s]) == []


def test_unresolved_without_survivor_yields_nothing() -> None:
    s = RbxScript(
        name="Player",
        source='local slot = self.cam:FindFirstChild("X")',
        child_ref_resolution={"getchild_total": 1, "resolved_total": 0},
    )
    assert _check_d([s]) == []


def test_green_for_the_wrong_reason_guard() -> None:
    # Same fact, but the pre-rewritten (named-lookup) source is CLEAN while the
    # un-rewritten (ordinal) source FIRES — proving the check keys on the
    # surviving ordinal, not the fact alone.
    fact = {"getchild_total": 3, "resolved_total": 3}
    dirty = RbxScript(name="T", source="x = base:GetChildren()[1]",
                      child_ref_resolution=fact)
    clean = RbxScript(name="T", source='x = base:FindFirstChild("Base")',
                      child_ref_resolution=fact)
    assert len([v for v in _check_d([dirty])
                if v.check == "child_ordinal_survivor"]) == 1
    assert _check_d([clean]) == []


def test_legacy_never_fed_to_verify_contract() -> None:
    # §1.3: verify_contract is only reached on the generic topology branch.
    # A legacy-shaped RbxScript with a surviving ordinal but NO fact abstains
    # (absent field) — even if someone fed it directly, it never fail-closes.
    s = RbxScript(name="Legacy", source="x = base:GetChildren()[1]")
    res = verify_contract(_TOPOLOGY, [s])
    assert [v for v in res.violations
            if v.check == "child_ordinal_survivor"] == []


def test_check_d_wired_into_verify_contract() -> None:
    # Sanity: the function is actually invoked by verify_contract (not dead).
    assert hasattr(contract_verifier, "_check_surviving_child_ordinal")
    s = RbxScript(name="T", source="x = base:GetChildren()[1]",
                  child_ref_resolution={"getchild_total": 1, "resolved_total": 1})
    res = verify_contract(_TOPOLOGY, [s])
    assert any(v.check == "child_ordinal_survivor" for v in res.violations)


# --- finding 3: PER-SITE fail-close (no whole-script abstain bypass) ---------


def test_partial_resolved_survivor_exceeds_budget_fires() -> None:
    # {2,1}: budget = 1 unresolved site. TWO surviving ordinals -> at least one
    # lands on the RESOLVED site -> fail-closed (no blanket abstain).
    s = RbxScript(
        name="Turret",
        source=(
            "local a = base:GetChildren()[1]\n"
            "local b = self.cam:GetChildren()[1]\n"
            "return a, b"
        ),
        child_ref_resolution={"getchild_total": 2, "resolved_total": 1},
    )
    survivors = [v for v in _check_d([s]) if v.check == "child_ordinal_survivor"]
    assert len(survivors) == 1
    res = verify_contract(_TOPOLOGY, [s])
    assert any("child_ordinal_survivor" in e for e in fail_closed_errors(res))


def test_partial_resolved_survivor_within_budget_only_info() -> None:
    # {2,1}: budget 1, ONE survivor -> attributable to the unresolved site ->
    # non-promoting info (the resolved site is clean).
    s = RbxScript(
        name="Turret",
        source="local b = self.cam:GetChildren()[1]\nreturn b",
        child_ref_resolution={"getchild_total": 2, "resolved_total": 1},
    )
    rows = _check_d([s])
    assert [v.check for v in rows] == ["child_ordinal_coverage_gap"]
    assert rows[0].severity == "info"
    res = verify_contract(_TOPOLOGY, [s])
    assert fail_closed_errors(res) == []


def test_method_receiver_factored_survivor_fires() -> None:
    # {3,3} with a method-call-receiver factored survivor:
    # ``local kids = self:_tBase():GetChildren(); local first = kids[1]``.
    s = RbxScript(
        name="Turret",
        source=(
            "local kids = self:_tBase():GetChildren()\n"
            "local first = kids[1]\n"
            "return first"
        ),
        child_ref_resolution={"getchild_total": 3, "resolved_total": 3},
    )
    survivors = [v for v in _check_d([s]) if v.check == "child_ordinal_survivor"]
    assert len(survivors) == 1


def test_method_receiver_adjacent_survivor_fires() -> None:
    # Adjacent method-receiver form ``self:_tBase():GetChildren()[1]``.
    s = RbxScript(
        name="Turret",
        source="local b = self:_tBase():GetChildren()[1]\nreturn b",
        child_ref_resolution={"getchild_total": 3, "resolved_total": 3},
    )
    survivors = [v for v in _check_d([s]) if v.check == "child_ordinal_survivor"]
    assert len(survivors) == 1


# --- round-2 finding 2: engine-global GetChildren excluded from the budget ---


def test_global_getchildren_not_counted_within_budget() -> None:
    # {2,1}: one REAL child-ref survivor + one unrelated engine-global
    # ``workspace.Folder:GetChildren()[1]``. The global is excluded, so only the
    # 1 real survivor counts == budget 1 -> does NOT fire (info only).
    s = RbxScript(
        name="Turret",
        source=(
            "local a = self.cam:GetChildren()[1]\n"
            "local g = workspace.Folder:GetChildren()[1]\n"
            "return a, g"
        ),
        child_ref_resolution={"getchild_total": 2, "resolved_total": 1},
    )
    survivors = [v for v in _check_d([s]) if v.check == "child_ordinal_survivor"]
    assert survivors == []
    res = verify_contract(_TOPOLOGY, [s])
    assert fail_closed_errors(res) == []


def test_two_real_survivors_still_fire_with_global_present() -> None:
    # {2,1}: budget 1, but TWO real child-ref survivors (resolved-site regression)
    # plus an engine-global. The global is excluded; 2 > budget 1 -> FIRES.
    s = RbxScript(
        name="Turret",
        source=(
            "local a = base:GetChildren()[1]\n"
            "local b = self.cam:GetChildren()[1]\n"
            "local g = workspace.Folder:GetChildren()[1]\n"
            "return a, b, g"
        ),
        child_ref_resolution={"getchild_total": 2, "resolved_total": 1},
    )
    survivors = [v for v in _check_d([s]) if v.check == "child_ordinal_survivor"]
    assert len(survivors) == 1
    res = verify_contract(_TOPOLOGY, [s])
    assert any("child_ordinal_survivor" in e for e in fail_closed_errors(res))


def test_fully_resolved_only_global_survivor_does_not_fire() -> None:
    # {1,1}: the ONLY GetChildren survivor is ``workspace:GetChildren()[1]`` (an
    # engine-tree iteration). Excluded -> 0 counted survivors -> does NOT fire.
    s = RbxScript(
        name="Turret",
        source="local g = workspace:GetChildren()[1]\nreturn g",
        child_ref_resolution={"getchild_total": 1, "resolved_total": 1},
    )
    assert _check_d([s]) == []


@pytest.mark.parametrize(
    "receiver, is_global",
    [
        # Every root in _ENGINE_GLOBAL_ROOTS is excluded...
        ("workspace.Folder", True),
        ("game.Players.Foo", True),
        ('game:GetService("Players").Foo', True),  # service-call root still roots at game
        ("script.Parent", True),
        ("Players.LocalPlayer", True),
        # ...but a child-ref-plausible root (self / a local) is NOT excluded.
        ("self.cam", False),
        ("base", False),
        ("origin.Parent", False),
    ],
)
def test_engine_global_root_classification(receiver: str, is_global: bool) -> None:
    assert _receiver_roots_at_engine_global(receiver) is is_global


@pytest.mark.parametrize(
    "global_recv",
    ["game.Players", 'game:GetService("Players")', "script.Parent", "Players.Local"],
)
def test_each_engine_global_survivor_excluded_from_budget(global_recv: str) -> None:
    # {1,1} fully-resolved: the only ordinal roots at a NON-workspace engine global
    # -> excluded -> 0 counted survivors -> check D does NOT fire. Guards the
    # game/Players/script roots beyond the workspace case above.
    s = RbxScript(
        name="Turret",
        source=f"local g = {global_recv}:GetChildren()[1]\nreturn g",
        child_ref_resolution={"getchild_total": 1, "resolved_total": 1},
    )
    assert _check_d([s]) == []


# --- finding 5: a survivor inside a Luau block comment/string does NOT fire --


def test_luau_block_comment_survivor_does_not_fire() -> None:
    s = RbxScript(
        name="Turret",
        source=(
            "--[[ x:GetChildren()[1] ]]\n"
            'local b = base:FindFirstChild("Base")\n'
            "return b"
        ),
        child_ref_resolution={"getchild_total": 3, "resolved_total": 3},
    )
    assert _check_d([s]) == []


def test_luau_long_string_survivor_does_not_fire() -> None:
    s = RbxScript(
        name="Turret",
        source=(
            "local doc = [[ base:GetChildren()[1] ]]\n"
            "return doc"
        ),
        child_ref_resolution={"getchild_total": 3, "resolved_total": 3},
    )
    assert _check_d([s]) == []
