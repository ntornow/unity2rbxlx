"""Player-bind axis is serialized into smoke_test_report.json.

Guards the documented-red ↔ Phase-2 flip contract (see
docs/KNOWN_ISSUES.md "Player-bind Studio gate ships DOCUMENTED-RED"): the
`cold-e2e` CI gate and the `--verify` hook read `wasd_works` /
`mouse_moves_view` out of `smoke_test_report.json` to apply the
`REQUIRE_PLAYER_BIND` warn-vs-error rule. If either field is renamed or
dropped from `SmokeTestReport`, the bind axis goes dark silently and the
Phase-2 flip would have no signal to gate on. This pins the field names on the
serialized dict directly (no Studio launch).
"""
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from smoke_test import SmokeTestReport


def test_smoke_report_serializes_player_bind_fields():
    report = SmokeTestReport()
    report.wasd_works = True
    report.mouse_moves_view = True

    data = asdict(report)

    assert "wasd_works" in data, "wasd_works missing from serialized report"
    assert "mouse_moves_view" in data, "mouse_moves_view missing from serialized report"
    assert data["wasd_works"] is True
    assert data["mouse_moves_view"] is True

    # round-trips through json (the on-disk smoke_test_report.json shape)
    round_tripped = json.loads(json.dumps(data))
    assert round_tripped["wasd_works"] is True
    assert round_tripped["mouse_moves_view"] is True
