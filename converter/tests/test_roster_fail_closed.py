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


# A minimal ORIGINAL-C# roster loader: the deterministic "a roster was
# expected" signal find_roster_consumers / csharp_label_loader_paths key on.
_CS_ROSTER_LOADER = (
    "void LoadDatabase() {\n"
    "  Addressables.LoadAssetsAsync<GameObject>(\"characters\", op => {\n"
    "    Character c = op.GetComponent<Character>();\n"
    "    if (c != null) m_CharactersDict.Add(c.characterName, c);\n"
    "  });\n"
    "}\n"
)
_CS_NO_LOADER = "void Update() { transform.position = Vector3.zero; }\n"


def test_no_rows_for_single_consumer() -> None:
    facts = {
        "A.cs": RosterConsumerFact("A.cs", "characters", "Character", "characterName"),
    }
    rows = _roster_fail_closed(
        facts,
        {"characters": ["p"]},
        {"A.cs": _CS_ROSTER_LOADER},
    )
    assert rows == []


def test_roster_ambiguous_two_modules_one_label() -> None:
    facts = {
        "A.cs": RosterConsumerFact("A.cs", "characters", "Character", "characterName"),
        "B.cs": RosterConsumerFact("B.cs", "characters", "Character", "characterName"),
    }
    rows = _roster_fail_closed(
        facts,
        {"characters": ["p"]},
        {"A.cs": _CS_ROSTER_LOADER, "B.cs": _CS_ROSTER_LOADER},
    )
    kinds = {r.kind for r in rows}
    assert "roster_ambiguous" in kinds
    detail = next(r.detail for r in rows if r.kind == "roster_ambiguous")
    assert "A.cs" in detail and "B.cs" in detail


def test_roster_signal_absent_stale_artifact() -> None:
    # REAL stale-artifact condition (drives the production path): a module's
    # ORIGINAL C# calls Addressables.LoadAssetsAsync<>("characters", ...) -- a
    # roster IS expected -- but the scene_runtime carries NO addressables block,
    # so by_label is empty and find_roster_consumers returns {}. The guard MUST
    # source "roster expected" from the C# fact, not by_label (which is exactly
    # what is missing). Pre-fix (guard keyed on `by_label and addressables is
    # None`) this is GREEN-but-wrong: by_label={} -> guard never fires. Post-fix
    # it fires off the C# loader fact.
    rows = _roster_fail_closed(
        {},                       # find_roster_consumers abstained (by_label empty)
        {},                       # the stale artifact has no by_label surface
        {"A.cs": _CS_ROSTER_LOADER},
    )
    kinds = {r.kind for r in rows}
    assert "roster_signal_absent" in kinds
    detail = next(r.detail for r in rows if r.kind == "roster_signal_absent")
    assert "A.cs" in detail


def test_no_signal_absent_when_no_csharp_loader() -> None:
    # No module loads an Addressables roster -> a non-roster game with a stale
    # artifact must NOT fire roster_signal_absent (no false positive).
    rows = _roster_fail_closed({}, {}, {"A.cs": _CS_NO_LOADER})
    assert rows == []


def test_no_signal_absent_on_healthy_path() -> None:
    # C# loader present AND by_label present (the normal Unit-4 path):
    # find_roster_consumers handles it; the stale-artifact guard must NOT fire.
    facts = {
        "A.cs": RosterConsumerFact("A.cs", "characters", "Character", "characterName"),
    }
    rows = _roster_fail_closed(
        facts,
        {"characters": ["p"]},
        {"A.cs": _CS_ROSTER_LOADER},
    )
    kinds = {r.kind for r in rows}
    assert "roster_signal_absent" not in kinds


def test_empty_by_label_no_rows() -> None:
    rows = _roster_fail_closed({}, {}, {})
    assert rows == []
