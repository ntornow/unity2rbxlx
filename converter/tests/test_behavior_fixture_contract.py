"""
test_behavior_fixture_contract.py -- structural guard that the SimpleFPS
behavior fixtures the Studio gate / Phase-2 require-bind flip depend on
stay present and schema-valid.

This is a cheap, no-Studio unit (Phase-1 design §2.2c): a fixture
rename/drop or a schema break fails THIS test, not a Studio run. It keys
off STRUCTURAL facts only -- the driver's ``validate`` schema check and
the literal fixture IDs in the behavior JSON -- never on AI transpile
output.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.studio_behavior_driver import _project_path, main  # noqa: E402
from tests.studio_behavior_runner import load_fixtures  # noqa: E402

# The player-bind fixtures the Phase-2 REQUIRE_PLAYER_BIND flip will
# require (design §2.2c "Real fixture IDs").
_PLAYER_BIND_IDS = frozenset({
    "wasd_w_moves_forward",
    "mouse_yaw_rotates_camera",
    "shoot_fires_remote_or_bullet",
})

# The turret/door bind fixtures that ride the same gate. Verified to
# exist verbatim in SimpleFPS.behavior.json.
_TURRET_DOOR_IDS = frozenset({
    "turrets_target_and_damage_player",
    "door_opens_with_key",
    "walk_to_cardkey_picks_it_up",
})


def _simplefps_ids() -> set[str]:
    _, fixtures = load_fixtures(_project_path("SimpleFPS"))
    return {f["id"] for f in fixtures}


def test_validate_simplefps_passes():
    """``studio_behavior_driver validate SimpleFPS`` exits 0 (schema OK)."""
    assert main(["validate", "SimpleFPS"]) == 0


def test_player_bind_fixture_ids_present():
    """The player-bind fixtures the Phase-2 flip requires are present."""
    ids = _simplefps_ids()
    missing = _PLAYER_BIND_IDS - ids
    assert not missing, f"player-bind fixtures missing from SimpleFPS: {sorted(missing)}"


def test_turret_and_door_fixture_ids_present():
    """The turret/door bind fixtures that ride the same gate are present."""
    ids = _simplefps_ids()
    missing = _TURRET_DOOR_IDS - ids
    assert not missing, f"turret/door fixtures missing from SimpleFPS: {sorted(missing)}"
