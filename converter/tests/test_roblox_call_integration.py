"""Full-chain integration: bounded repair (slice 2.2) + universal net (slice 2.3).

Proves the two halves of the phase COMPOSE end to end:

  * SUCCESS path — the model first emits a proven-invalid bullet, the repair's
    reprompt RETURNS a corrected version. The transpiled output is clean (no
    proven invalid) AND the universal net reports NO ``error``-severity
    ``nonexistent_roblox_method`` issue. Repair handled it; the net agrees.

  * SURVIVOR path — a fake backend that NEVER fixes it. After repair the output
    still carries the proven invalid, so the net reports an ``error``-severity
    issue (which ``_roblox_call_net_errors`` would promote → fail-close). This
    proves the net is the backstop precisely when repair fails.

The repair is driven by injected fake reprompt closures (the same pattern as
``test_roblox_call_repair.py``) — no real API. The net is driven by the real
``run_semantic_validators`` over an ``RbxScript``, exactly as ``write_output``
invokes it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import RbxScript  # noqa: E402
from converter.code_transpiler import (  # noqa: E402
    _proven_invalid_roblox_calls,
    _repair_invalid_roblox_calls,
)
from converter.pipeline import _roblox_call_net_errors  # noqa: E402
from converter.semantic_validators import run_semantic_validators  # noqa: E402


# A hand-broken bullet whose proven Roblox receiver (``char = plr.Character``)
# calls the hallucinated ``FindFirstChildOfType`` — the validator flags proven.
BROKEN_BULLET = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:OnTouch(plr)
    local char = plr.Character
    local hum = char:FindFirstChildOfType("Humanoid")
    if hum then hum:TakeDamage(10) end
end
return Class
"""

# The correction a good reprompt returns: the real Roblox method.
FIXED_BULLET = BROKEN_BULLET.replace(
    "FindFirstChildOfType", "FindFirstChildWhichIsA",
)


def _net_error_issues(source: str) -> list:
    """Run the REAL universal net over ``source`` (as ``write_output`` does) and
    return the ``error``-severity ``nonexistent_roblox_method`` issues."""
    script = RbxScript(name="Bullet", source=source)
    report = run_semantic_validators([script])
    return [
        i for i in report.issues
        if i.rule == "nonexistent_roblox_method" and i.severity == "error"
    ]


def test_repair_fixes_and_net_agrees_clean() -> None:
    """SUCCESS: repair fixes the proven invalid → output clean → net error-free.

    Drives the real ``_repair_invalid_roblox_calls`` with a reprompt that
    returns the corrected bullet, then runs the real net over the repaired
    output. Both the repair and the net must report a clean module.
    """
    calls: list[str] = []

    def reprompt(msg: str) -> str:
        calls.append(msg)
        return FIXED_BULLET

    repaired, warnings = _repair_invalid_roblox_calls(BROKEN_BULLET, reprompt)

    # Repair half: the output is clean (no proven invalid), no survivor warning.
    assert _proven_invalid_roblox_calls(repaired) == [], (
        f"repair left a proven invalid: {repaired}"
    )
    assert not any("roblox-call-survivor" in w for w in warnings), warnings
    assert len(calls) == 1, "the reprompt should fire exactly once"

    # Net half: over the SAME repaired output, the net raises no error-severity
    # nonexistent_roblox_method issue → nothing to promote → conversion stays
    # success.
    errors = _net_error_issues(repaired)
    assert errors == [], (
        f"net reported an error on repaired-clean output: {errors}"
    )
    assert _roblox_call_net_errors(
        run_semantic_validators([RbxScript(name="Bullet", source=repaired)])
    ) == [], "a clean repaired module must promote no conversion errors"


def test_repair_fails_then_net_fails_closed() -> None:
    """SURVIVOR: repair never fixes it → net catches it (error) → would promote.

    A reprompt that always returns the SAME broken bullet exhausts the bounded
    tries; the output still has the proven invalid. The net is the backstop: it
    reports an ``error``-severity issue, and ``_roblox_call_net_errors`` renders
    a promotable conversion error (fail-closed).
    """
    calls: list[str] = []

    def reprompt(msg: str) -> str:
        calls.append(msg)
        return BROKEN_BULLET  # never fixed

    survived, warnings = _repair_invalid_roblox_calls(BROKEN_BULLET, reprompt)

    # Repair half: the proven invalid survived and is tagged (not crashed, not
    # silently clean).
    assert _proven_invalid_roblox_calls(survived), (
        "the proven invalid must survive a never-fixing backend"
    )
    assert any("roblox-call-survivor" in w for w in warnings), warnings
    assert len(calls) == 2, "repair must be bounded to 2 tries"

    # Net half: the backstop fires on exactly that survivor — an error-severity
    # nonexistent_roblox_method issue that promotes to a conversion error.
    errors = _net_error_issues(survived)
    assert errors, "the net must catch the surviving proven invalid"
    assert any("FindFirstChildOfType" in i.explanation for i in errors)

    promoted = _roblox_call_net_errors(
        run_semantic_validators([RbxScript(name="Bullet", source=survived)])
    )
    assert promoted, (
        "the net must render a promotable conversion error (fail-closed) "
        "when repair fails"
    )
