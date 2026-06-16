"""AC8f -- the _Rigidbody2D discriminator stamp + mesh-wrap co-location.

Relation #8 (scale-faithful gravity) corrects 3D Rigidbody bodies ONLY; Physics2D
is out of scope. ``_UnityMass`` is stamped class-agnostically BEFORE the 2D/3D
split (scene_converter.py:2797), so a 2D body carries it indistinguishably from a
3D one. The discriminator is ``_Rigidbody2D``, stamped UNCONDITIONALLY at the top
of the Rigidbody2D branch (a default-scale 2D body sets neither GravityScale nor
UseGravity, so _Rigidbody2D is its SOLE discriminator). The 3D Rigidbody branch
leaves it unflagged.

Because the per-carrier ``_Rigidbody2D == nil`` exclusion (every runtime scan
surface + the Python emit-gate) keys on the SAME instance the scan finds via
``_UnityMass``, ``_Rigidbody2D`` must travel WITH ``_UnityMass`` to the inner
``*_Mesh`` on mesh-wrap (the S2 shape).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.scene_converter import (
    _process_components,
    _wrap_geometry_with_children_into_model,
)
from core.roblox_types import RbxPart


class _FakeComponent:
    def __init__(self, component_type: str, properties: dict[str, object]):
        self.component_type = component_type
        self.properties = properties


class _FakeNode:
    def __init__(self, name: str, components: list[_FakeComponent]):
        self.name = name
        self.components = components
        self.mesh_guid = None


def _process(name: str, comp: _FakeComponent) -> RbxPart:
    part = RbxPart(name=name, size=(3.571, 3.571, 3.571))
    _process_components(_FakeNode(name, [comp]), part)
    return part


# ---------------------------------------------------------------------------
# AC8f -- the upstream stamp (UNCONDITIONAL, default-scale fixture)
# ---------------------------------------------------------------------------

def test_default_scale_2d_body_is_flagged_rigidbody2d() -> None:
    """A DEFAULT-scale Rigidbody2D (m_GravityScale == 1.0) -- which stamps
    neither GravityScale nor UseGravity -- still gets _Rigidbody2D == True and
    no scale/gravity attribute. (A != 1.0 fixture would pass even with the buggy
    nested placement -- green-test-for-the-wrong-reason; the default-scale
    fixture is the one that pins the UNCONDITIONAL stamp.)"""
    part = _process("Crate2D", _FakeComponent("Rigidbody2D", {"m_GravityScale": 1.0, "m_Mass": 2.0}))
    assert part.attributes.get("_Rigidbody2D") is True
    assert part.attributes.get("_UnityMass") == 2.0  # class-agnostic stamp
    assert "GravityScale" not in part.attributes
    assert "UseGravity" not in part.attributes


def test_zero_gravity_scale_2d_body_is_flagged_rigidbody2d() -> None:
    """A gravity_scale == 0.0 Rigidbody2D also gets _Rigidbody2D == True."""
    part = _process("Float2D", _FakeComponent("Rigidbody2D", {"m_GravityScale": 0.0}))
    assert part.attributes.get("_Rigidbody2D") is True
    # 0.0 scale still stamps UseGravity=False (a 2D fact) -- unchanged behavior.
    assert part.attributes.get("UseGravity") is False


def test_3d_rigidbody_is_not_flagged_rigidbody2d() -> None:
    """A 3D Rigidbody (m_UseGravity, no m_GravityScale) is NOT flagged."""
    part = _process("Crate3D", _FakeComponent("Rigidbody", {"m_UseGravity": 1, "m_Mass": 5.0}))
    assert "_Rigidbody2D" not in part.attributes
    assert part.attributes.get("_UnityMass") == 5.0


# ---------------------------------------------------------------------------
# AC8f -- mesh-wrap move-list co-location (S2 shape)
# ---------------------------------------------------------------------------

def test_meshwrap_co_locates_unitymass_and_rigidbody2d_on_inner() -> None:
    """Driving the wrap path on a wrappable body carrying BOTH _UnityMass and
    _Rigidbody2D: the inner *_Mesh carries BOTH; the outer Model retains
    neither."""
    outer = RbxPart(
        name="WrappedBody",
        class_name="Part",
        size=(3.571, 3.571, 3.571),
    )
    outer.shape = 1  # a primitive shape => _has_geometry True
    outer.attributes["_UnityMass"] = 3.0
    outer.attributes["_Rigidbody2D"] = True
    # A non-mesh, non-gameplay attribute that should STAY on the outer Model.
    outer.attributes["UseGravity"] = False
    # A child transform forces the wrap (geometry + children => Model wrap).
    outer.children.append(RbxPart(name="ChildXform", size=(1.0, 1.0, 1.0)))

    _wrap_geometry_with_children_into_model(outer, node_name="WrappedBody")

    assert outer.class_name == "Model"
    inner = next(c for c in outer.children if c.name == "WrappedBody_Mesh")

    # Both discriminator + mass travel to the inner carrier.
    assert inner.attributes.get("_UnityMass") == 3.0
    assert inner.attributes.get("_Rigidbody2D") is True
    # Outer Model retains neither.
    assert "_UnityMass" not in outer.attributes
    assert "_Rigidbody2D" not in outer.attributes
    # Non-move-list facts stay on the outer Model (per D1).
    assert outer.attributes.get("UseGravity") is False
