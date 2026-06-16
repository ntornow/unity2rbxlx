"""Slice 2.3: the universal fail-closed net for hallucinated Roblox calls.

Covers the new ``nonexistent_roblox_method`` semantic rule, the net-new
``write_output`` promotion (the pure ``_roblox_call_net_errors`` helper + its
resume-safe replace), proven-vs-unproven gating, and ``fc`` retirement.
"""

from __future__ import annotations

from core.roblox_types import RbxScript
from converter.semantic_validators import run_semantic_validators
from converter.pipeline import (
    ROBLOX_CALL_ERROR_PREFIX,
    _roblox_call_net_errors,
)
from converter.runtime_contract import verify_module


# The real bullet shape: a Roblox-typed receiver (``char = plr.Character``)
# calls the hallucinated ``FindFirstChildOfType`` → PROVEN invalid.
_PROVEN_BULLET = (
    "function C:OnHit(plr)\n"
    "    local char = plr.Character\n"
    '    local h = char:FindFirstChildOfType("Humanoid")\n'
    "    if h then h:TakeDamage(10) end\n"
    "end\n"
    "return C\n"
)

# An UNPROVEN invalid: ``gm`` is a host-component table (not Roblox-typed), so
# its bogus method is reported but never gates.
_UNPROVEN_SCRIPT = (
    "function C:Tick()\n"
    '    local gm = self.host.findObjectOfType("GameManager")\n'
    "    gm:FakeMethod()\n"
    "end\n"
    "return C\n"
)


def _net_issues(source: str):
    script = RbxScript(name="Bullet", source=source)
    report = run_semantic_validators(
        [script], enabled_rules={"nonexistent_roblox_method"}
    )
    return report


# ---------------------------------------------------------------------------
# Detection: the rule reports proven + unproven distinctly
# ---------------------------------------------------------------------------


def test_proven_invalid_reported_as_error():
    report = _net_issues(_PROVEN_BULLET)
    proven = [
        i for i in report.issues
        if i.rule == "nonexistent_roblox_method"
        and "FindFirstChildOfType" in i.explanation
    ]
    assert proven, "FindFirstChildOfType must be reported by the net"
    assert proven[0].severity == "error", "a proven invalid is tagged severity=error"


def test_unproven_invalid_reported_as_warning():
    report = _net_issues(_UNPROVEN_SCRIPT)
    fake = [
        i for i in report.issues
        if i.rule == "nonexistent_roblox_method"
        and "FakeMethod" in i.explanation
    ]
    assert fake, "FakeMethod must be reported by the net"
    assert fake[0].severity == "warning", "an unproven invalid is tagged severity=warning"


# ---------------------------------------------------------------------------
# Promotion: PROVEN issues flip ctx.errors; UNPROVEN do not
# ---------------------------------------------------------------------------


def test_proven_invalid_promotes_to_ctx_errors():
    report = _net_issues(_PROVEN_BULLET)
    errors = _roblox_call_net_errors(report)
    assert errors, "a proven invalid must promote to a conversion error"
    assert all(e.startswith(ROBLOX_CALL_ERROR_PREFIX) for e in errors)


def test_unproven_invalid_not_promoted():
    report = _net_issues(_UNPROVEN_SCRIPT)
    errors = _roblox_call_net_errors(report)
    assert errors == [], "an unproven invalid must stay report-only (no promotion)"


def test_promotion_replaces_not_duplicates_on_rerun():
    # Resume-safety: running the promotion twice (mirroring a
    # materialize_and_classify resume) must REPLACE, not duplicate.
    report = _net_issues(_PROVEN_BULLET)

    ctx_errors: list[str] = []

    def promote() -> None:
        ctx_errors[:] = [
            e for e in ctx_errors
            if not e.startswith(ROBLOX_CALL_ERROR_PREFIX)
        ]
        ctx_errors.extend(_roblox_call_net_errors(report))

    promote()
    first = list(ctx_errors)
    promote()
    second = list(ctx_errors)

    assert first, "first promotion must append the net rows"
    assert first == second, "a rerun must replace, not duplicate, the net rows"


def test_promotion_preserves_unrelated_errors():
    report = _net_issues(_PROVEN_BULLET)
    ctx_errors = ["[contract-verifier: unrelated]"]
    ctx_errors[:] = [
        e for e in ctx_errors if not e.startswith(ROBLOX_CALL_ERROR_PREFIX)
    ]
    ctx_errors.extend(_roblox_call_net_errors(report))
    assert "[contract-verifier: unrelated]" in ctx_errors
    assert any(e.startswith(ROBLOX_CALL_ERROR_PREFIX) for e in ctx_errors)


# ---------------------------------------------------------------------------
# fc retirement: verify_module no longer fires fc, but the net still catches it
# ---------------------------------------------------------------------------


def test_fc_retired_but_net_catches():
    fc = [v for v in verify_module(_PROVEN_BULLET).violations if v.rule == "fc"]
    assert not fc, "rule fc is retired from verify_module"

    report = _net_issues(_PROVEN_BULLET)
    caught = [
        i for i in report.issues
        if i.rule == "nonexistent_roblox_method" and i.severity == "error"
    ]
    assert caught, "the net must still catch the bug fc used to catch"
