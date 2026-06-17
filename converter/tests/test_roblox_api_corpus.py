"""Tests for the vendored Roblox callable-member corpus + loader.

A green result proves the snapshot loaded, the filter kept real instance methods,
and dropped properties/events — so the corpus can be trusted to flag a
hallucinated method name (the bug this slice exists to catch).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.roblox_api_corpus import (  # noqa: E402
    corpus_version,
    is_callable_member,
    signature,
    suggest_candidates,
)

_SNAPSHOT_PATH = (
    Path(__file__).parent.parent / "converter" / "data" / "roblox_callable_members.json"
)

# Real instance methods that MUST survive the filter. A mis-filter (e.g. keying
# on "Method" instead of "Function") would drop all of these.
_WITNESS_METHODS = [
    "FindFirstChild",
    "FindFirstChildWhichIsA",
    "FindFirstChildOfClass",
    "FindFirstAncestorWhichIsA",
    "GetPivot",
    "PivotTo",
    "GetAttribute",
    "SetAttribute",
    "GetChildren",
    "GetDescendants",
    "GetPartBoundsInRadius",
    "Raycast",
    "IsA",
    "ApplyImpulse",
    "GetService",
    "TakeDamage",
    "Destroy",
    "WaitForChild",
]

# Names that are NOT callable members (properties / events). The corpus is
# callable-ONLY; these must be absent so a property reference isn't mistaken
# for a method.
_NON_CALLABLE_NAMES = ["Health", "Position", "Parent", "Character"]

# The hallucinated method — the bug. It must be absent so the validator flags it.
_HALLUCINATED_METHOD = "FindFirstChildOfType"


def _load_raw() -> dict[str, object]:
    return json.loads(_SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_snapshot_loads_and_is_non_empty() -> None:
    raw = _load_raw()
    members = raw["callableMembers"]
    assert isinstance(members, list)
    assert len(members) > 1000


def test_callable_members_sorted_and_unique() -> None:
    raw = _load_raw()
    members = raw["callableMembers"]
    assert members == sorted(members), "callableMembers must be sorted"
    assert len(members) == len(set(members)), "callableMembers must be unique"


def test_snapshot_metadata_shape() -> None:
    raw = _load_raw()
    assert isinstance(raw["apiDumpVersion"], str)
    assert raw["apiDumpVersion"]
    assert raw["generatedFrom"] == "setup.rbxcdn.com API-Dump.json"


def test_witness_methods_present() -> None:
    raw = _load_raw()
    members = set(raw["callableMembers"])
    missing = [m for m in _WITNESS_METHODS if m not in members]
    assert not missing, f"witness methods dropped by filter: {missing}"


def test_non_callable_names_absent() -> None:
    raw = _load_raw()
    members = set(raw["callableMembers"])
    present = [n for n in _NON_CALLABLE_NAMES if n in members]
    assert not present, f"non-callable names leaked into corpus: {present}"


def test_hallucinated_method_absent() -> None:
    raw = _load_raw()
    members = set(raw["callableMembers"])
    assert _HALLUCINATED_METHOD not in members


def test_is_callable_member_api() -> None:
    assert is_callable_member("TakeDamage") is True
    assert is_callable_member("FindFirstChildOfType") is False


def test_corpus_version_non_empty() -> None:
    assert corpus_version()
    assert isinstance(corpus_version(), str)


def test_signatures_map_present_and_covers_witnesses() -> None:
    raw = _load_raw()
    sigs = raw["signatures"]
    assert isinstance(sigs, dict)
    assert sigs
    missing = [m for m in _WITNESS_METHODS if m not in sigs]
    assert not missing, f"signatures missing witness methods: {missing}"


def test_signature_for_real_method() -> None:
    sig = signature("FindFirstChildOfClass")
    assert isinstance(sig, str)
    assert sig
    assert "FindFirstChildOfClass" in sig
    # A real param must appear (the dump names it ``className``).
    assert "(" in sig and ":" in sig
    assert "className" in sig


def test_signature_for_hallucinated_method_is_none() -> None:
    assert signature("FindFirstChildOfType") is None


def test_signature_for_unknown_name_is_none() -> None:
    assert signature("Nonexistent") is None


def test_suggest_candidates_ranks_real_family_top() -> None:
    result = suggest_candidates("FindFirstChildOfType")
    assert len(result) <= 3
    assert "FindFirstChildOfClass" in result
    assert "FindFirstChildWhichIsA" in result


def test_suggest_candidates_respects_k() -> None:
    result = suggest_candidates("FindFirstChildOfType", k=2)
    assert len(result) <= 2
    # The closest real method must still survive a tighter k.
    assert "FindFirstChildOfClass" in result


def test_suggest_candidates_includes_close_real_method() -> None:
    result = suggest_candidates("ApplyImpulse")
    assert result
    # An exact/near match must surface itself or a real impulse method.
    assert "ApplyImpulse" in result


def test_rbxscriptsignal_members_callable() -> None:
    """ACCEPTANCE 9: RBXScriptSignal members are callable with signatures.

    These belong to the RBXScriptSignal DATA TYPE (outside the API dump's
    ``Classes`` array), unioned into the corpus by the refresh tool so a valid
    ``signal:Connect(fn)`` is not a false positive.
    """
    for name in ("Connect", "Once", "Wait", "ConnectParallel"):
        assert is_callable_member(name) is True, f"{name} must be callable"
        sig = signature(name)
        assert isinstance(sig, str) and sig, f"{name} must have a non-None signature"
        assert name in sig


def test_suggest_candidates_garbage_input_is_bounded() -> None:
    result = suggest_candidates("Xqzptv")
    assert isinstance(result, list)
    assert len(result) <= 3
    assert all(is_callable_member(name) for name in result)
