"""Slice 2.3: the narrow ``fc`` rule (nonexistent ``:FindFirstChildOfType(``) is
RETIRED from the transpile-time ``verify_module`` path — it lived on a
bypassable per-route path that shipped the real bug as success=True. The
universal provenance-gated net (``roblox_call_validator.find_invalid_roblox_calls``,
surfaced as the ``nonexistent_roblox_method`` semantic rule) subsumes it.

This file's original intent ("FindFirstChildOfType must be caught") is migrated:
- the OLD assertion (``verify_module`` emits rule ``fc``) is INVERTED to prove
  retirement;
- the catch-it intent moves to the net (and the full net suite lives in
  ``test_roblox_call_net.py``).
"""

from __future__ import annotations

from converter.runtime_contract import verify_module
from converter.roblox_call_validator import find_invalid_roblox_calls


def _fc(src: str):
    return [v for v in verify_module(src).violations if v.rule == "fc"]


def test_fc_rule_retired_from_verify_module():
    # The real bullet shape that the old ``fc`` rule fired on.
    src = 'function C:OnHit(char) local h = char:FindFirstChildOfType("Humanoid"); h:TakeDamage(1) end\nreturn C\n'
    assert not _fc(src), "rule fc is retired; verify_module must no longer emit it"


def test_bug_still_caught_by_the_net():
    # The same hallucinated call is still caught — now by the universal net,
    # tagged ``proven`` (the receiver ``char = plr.Character`` is Roblox-typed).
    src = (
        "function C:OnHit(plr)\n"
        "    local char = plr.Character\n"
        '    local h = char:FindFirstChildOfType("Humanoid")\n'
        "    if h then h:TakeDamage(1) end\n"
        "end\n"
        "return C\n"
    )
    proven = [
        c for c in find_invalid_roblox_calls(src)
        if c["method"] == "FindFirstChildOfType"
        and c["receiver_provenance"] == "proven"
    ]
    assert proven, "the net must catch the FindFirstChildOfType bug as proven"


def test_valid_findfirstchildofclass_not_flagged_by_net():
    src = (
        "function C:OnHit(plr)\n"
        "    local char = plr.Character\n"
        '    local h = char:FindFirstChildOfClass("Humanoid")\n'
        "end\n"
        "return C\n"
    )
    bad = [
        c for c in find_invalid_roblox_calls(src)
        if c["method"] == "FindFirstChildOfClass"
    ]
    assert not bad, "the VALID FindFirstChildOfClass must NOT be flagged"
