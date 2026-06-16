"""Structural source tests for the canonical ``correctDynamicAssembly``
helper (``autogen._GRAVITY_CORRECTION_HELPER_LUAU``) -- §1.5 of the Phase-1
design (relation #8, scale-faithful gravity).

The repo has NO Roblox-API execution harness for standalone ``luau`` (no
``workspace.Gravity`` / ``Instance.new`` / ``Vector3`` / ``Enum`` mock), so the
force-shape ACs are STRUCTURAL SOURCE assertions over the emitted Luau text
(FIX A). Each asserts a load-bearing token of the formula / scan / skip-rules /
tag is present in the canonical helper string.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import _GRAVITY_CORRECTION_HELPER_LUAU


HELPER = _GRAVITY_CORRECTION_HELPER_LUAU


def test_force_formula_is_present_verbatim() -> None:
    """AC1: the exact compensating-force expression and the
    desiredStuds = desiredBaseStuds * gravityScale line (no clamp)."""
    assert "mass * (workspace.Gravity - desiredStuds)" in HELPER
    assert "desiredStuds = desiredBaseStuds * gravityScale" in HELPER


def test_mass_is_the_force_scalar_and_apply_at_com() -> None:
    """AC2: structurally mass-proportional ⇒ net accel mass-independent."""
    assert "root.AssemblyMass" in HELPER
    assert "ApplyAtCenterOfMass = true" in HELPER


def test_one_force_per_root_tag_check_then_create_then_set() -> None:
    """AC3: tag early-return BEFORE force creation; SetAttribute AFTER."""
    check_idx = HELPER.index("root:GetAttribute(TAG)")
    create_idx = HELPER.index('Instance.new("VectorForce")')
    set_idx = HELPER.index("root:SetAttribute(TAG, true)")
    assert check_idx < create_idx < set_idx


def test_anchored_skip_token_before_force() -> None:
    """AC4: anchored skip (also skips Model-carrier shapes S3/S6)."""
    anchored_idx = HELPER.index("if root.Anchored then")
    create_idx = HELPER.index('Instance.new("VectorForce")')
    assert anchored_idx < create_idx


def test_humanoid_skip_uses_ancestor_model_contains_humanoid_form() -> None:
    """AC5: ancestor-Model-contains-Humanoid skip; NOT the dead
    FindFirstAncestorWhichIsA("Humanoid")."""
    assert 'FindFirstChildWhichIsA("Humanoid")' in HELPER
    assert 'FindFirstAncestorWhichIsA("Humanoid")' not in HELPER


def test_assembly_mass_le_zero_skip_without_tag() -> None:
    """AC5b: mass <= 0 early-return BEFORE force creation AND BEFORE tag set."""
    mass_idx = HELPER.index("if mass <= 0 then")
    create_idx = HELPER.index('Instance.new("VectorForce")')
    set_idx = HELPER.index("root:SetAttribute(TAG, true)")
    assert mass_idx < create_idx
    assert mass_idx < set_idx


def test_use_gravity_false_full_cancel_token() -> None:
    """AC6/AC7: gravityScale = 0 when UseGravity == false, else
    (gravityScaleAttr or 1.0) -- the in-scope 3D modulator is UseGravity on/off."""
    assert "(useGravityAttr == false) and 0 or (gravityScaleAttr or 1.0)" in HELPER


def test_carrier_first_then_ancestor_model_fact_resolution() -> None:
    """AC8/AC8b: factOf reads off the carrier FIRST, walks ancestor Models
    only when absent (per-attribute, not a single owning-Model lookup)."""
    assert "local function factOf(carrier, key)" in HELPER
    # carrier read precedes the ancestor walk in factOf.
    fact_start = HELPER.index("local function factOf(carrier, key)")
    carrier_read = HELPER.index("carrier:GetAttribute(key)", fact_start)
    ancestor_walk = HELPER.index(
        'carrier:FindFirstAncestorWhichIsA("Model")', fact_start
    )
    assert carrier_read < ancestor_walk
    # The helper reads its facts via factOf, not raw GetAttribute on the part.
    assert 'factOf(carrier, "UseGravity")' in HELPER
    assert 'factOf(carrier, "GravityScale")' in HELPER


def test_model_carrier_representative_part_resolution() -> None:
    """AC8c: a Model carrier resolves PrimaryPart / first descendant BasePart,
    then AssemblyRootPart -- and is skipped via skip-if-anchored (AC4)."""
    assert "carrier.PrimaryPart" in HELPER
    assert 'carrier:FindFirstChildWhichIsA("BasePart", true)' in HELPER
    assert "representativePart.AssemblyRootPart or representativePart" in HELPER


def test_vectorforce_world_relative_and_attachment() -> None:
    """AC1/AC4 structure: World-relative VectorForce with an Attachment0."""
    assert 'Instance.new("Attachment")' in HELPER
    assert "vf.RelativeTo = Enum.ActuatorRelativeTo.World" in HELPER
    assert "vf.Force = Vector3.new(0, force, 0)" in HELPER
    assert "vf.Attachment0 = att" in HELPER


def test_tag_constant_name() -> None:
    """The dedup tag is the canonical attribute name (set on the resolved root)."""
    # The literal "_ScaleGravityCorrected" lives in the server script preamble
    # (the TAG local); the helper references the TAG upvalue.
    assert "root:SetAttribute(TAG, true)" in HELPER
