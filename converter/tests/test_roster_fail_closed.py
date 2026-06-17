"""Phase 2 (Unit 4) — roster orchestrator fail-closed rows (AC7 c/d).

``_roster_fail_closed`` surfaces the PROJECT-level guards
``find_roster_consumers`` cannot see from one module — symmetric with the
player-binding guards (D-P2-7):

  AC7(c)  roster_ambiguous     — >1 distinct module loads the SAME label.
  AC7(d)  roster_signal_absent — a non-empty by_label is expected but the
          scene_runtime carries no addressables block (stale artifact).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.contract_pipeline import _roster_fail_closed  # noqa: E402
from converter.roster_consumer_lowering import RosterConsumerFact  # noqa: E402


def test_no_rows_for_single_consumer() -> None:
    facts = {
        "A.cs": RosterConsumerFact("A.cs", "characters", "Character", "characterName"),
    }
    rows = _roster_fail_closed(
        facts, {"characters": ["p"]}, {"addressables": {"by_label": {"characters": ["p"]}}},
    )
    assert rows == []


def test_roster_ambiguous_two_modules_one_label() -> None:
    facts = {
        "A.cs": RosterConsumerFact("A.cs", "characters", "Character", "characterName"),
        "B.cs": RosterConsumerFact("B.cs", "characters", "Character", "characterName"),
    }
    rows = _roster_fail_closed(
        facts, {"characters": ["p"]}, {"addressables": {"by_label": {"characters": ["p"]}}},
    )
    kinds = {r.kind for r in rows}
    assert "roster_ambiguous" in kinds
    detail = next(r.detail for r in rows if r.kind == "roster_ambiguous")
    assert "A.cs" in detail and "B.cs" in detail


def test_roster_signal_absent_stale_artifact() -> None:
    # by_label expected (non-empty) but the scene_runtime has no addressables
    # block at all -> stale artifact, fail closed.
    rows = _roster_fail_closed({}, {"characters": ["p"]}, {})
    kinds = {r.kind for r in rows}
    assert "roster_signal_absent" in kinds


def test_empty_by_label_no_rows() -> None:
    rows = _roster_fail_closed({}, {}, {})
    assert rows == []
