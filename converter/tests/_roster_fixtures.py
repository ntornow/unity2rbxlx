"""Shared fixtures for roster-emit tests (rbxlx + luau parity).

Builds a SINGLE Model-rooted, >=2-part template with an intra-template
constraint (Weld), so the same RbxPlace exercises BOTH the Model-root Tags-lift
(AC4/AC11) AND the referent re-key / intra-member Part1 resolution (AC12) in one
build, and the cross-emitter parity test (AC7) drives both writers off it.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.roblox_types import (
    RbxConstraint,
    RbxPart,
    RbxPlace,
    RbxRoster,
    RbxRosterMember,
)


TEMPLATE_NAME = "Cat_abc123"
LABEL = "characters"
CHAR_NAME = "Trash Cat"
UNITY_TAG = "Enemy"  # a Unity m_TagString carried on the Model root


def make_model_rooted_template(
    *, with_unity_tag: bool = False, template_name: str = TEMPLATE_NAME
) -> RbxPart:
    """A Model root with 2 child Parts joined by a WeldConstraint.

    childB carries unity_file_id "200"; childA carries a WeldConstraint whose
    connected_body_file_id == "200" (an INTRA-template link). The Model root has
    unity_file_id "100" (mirrors a wrapped multi-part prefab).
    """
    child_b = RbxPart(name="LimbB", class_name="Part", unity_file_id="200")
    child_a = RbxPart(
        name="LimbA",
        class_name="Part",
        unity_file_id="201",
        constraints=[RbxConstraint(
            constraint_type="WeldConstraint",
            connected_body_file_id="200",
        )],
    )
    root_attrs = {"Tag": UNITY_TAG} if with_unity_tag else {}
    root = RbxPart(
        name=template_name,
        class_name="Model",
        unity_file_id="100",
        attributes=dict(root_attrs),
        children=[child_a, child_b],
    )
    return root


def make_place_with_roster(*, with_unity_tag: bool = False) -> RbxPlace:
    """An RbxPlace whose Templates has the Model template AND a roster member
    cloning it (tag=LABEL, characterName=CHAR_NAME)."""
    template = make_model_rooted_template(with_unity_tag=with_unity_tag)
    place = RbxPlace(replicated_templates=[template])
    place.rosters = [RbxRoster(label=LABEL, members=[
        RbxRosterMember(
            template_name=TEMPLATE_NAME,
            tag=LABEL,
            attributes={"characterName": CHAR_NAME},
        ),
    ])]
    return place
