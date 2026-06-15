"""Structural source tests for the client clone-site gravity hook in
``converter/runtime/scene_runtime.luau`` -- §1.6 of the Phase-1 design
(relation #8, scale-faithful gravity), slice 1.3.

The harness has NO Roblox-API execution seam for standalone ``luau`` (no
``workspace.Gravity`` / ``Instance.new`` / ``Vector3`` / ``Enum`` mock -- FIX A),
so the force-APPLICATION ACs (AC15/AC8d/AC8e) cannot be EXECUTED here; they are
STRUCTURAL SOURCE assertions over the emitted ``scene_runtime.luau`` text,
matching the repo's ``assert "<token>" in source`` style. Behavioral
force-on-a-client-clone confirmation is the Studio acceptance (S2/S4).

AC14 (client-mirror half): the mirrored ``correctDynamicAssembly`` logic in
``scene_runtime.luau`` carries the SAME force/skip/tag tokens as the canonical
``autogen._GRAVITY_CORRECTION_HELPER_LUAU`` (drift guard; the cross-file parity
assertion proper is slice 1.4).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import _GRAVITY_CORRECTION_HELPER_LUAU  # noqa: E402


RUNTIME_PATH = Path(__file__).parent.parent / "runtime" / "scene_runtime.luau"
SOURCE = RUNTIME_PATH.read_text(encoding="utf-8")


def _method_body(name: str) -> str:
    """The source span of one ``SceneRuntime:<name>`` method -- from its
    ``function`` line to the next ``\\nfunction SceneRuntime:`` (or EOF).

    Bounds on the actual method end rather than a fixed char window so the
    assertions stay precise as the method's comments grow/shrink and never
    bleed into the following method (addresses the +2000-window NIT)."""
    start = SOURCE.index(f"function SceneRuntime:{name}(")
    nxt = SOURCE.find("\nfunction SceneRuntime:", start + 1)
    return SOURCE[start:nxt if nxt != -1 else len(SOURCE)]


# ---------------------------------------------------------------------------
# AC15 -- client clone-site hook present + correct (structural)
# ---------------------------------------------------------------------------

def test_instantiate_prefab_calls_correct_cloned_dynamics_after_lifecycle() -> None:
    """AC15(i): instantiatePrefab calls self:_correctClonedDynamics(clone)
    AFTER _runAwakeEnableStart and BEFORE return clone."""
    inst_idx = SOURCE.index("function SceneRuntime:instantiatePrefab(")
    lifecycle_idx = SOURCE.index("self:_runAwakeEnableStart(componentList)", inst_idx)
    hook_idx = SOURCE.index("self:_correctClonedDynamics(clone)", inst_idx)
    return_idx = SOURCE.index("return clone", inst_idx)
    assert lifecycle_idx < hook_idx < return_idx


def test_correct_cloned_dynamics_method_defined() -> None:
    """AC15(ii): the _correctClonedDynamics method exists on SceneRuntime."""
    assert "function SceneRuntime:_correctClonedDynamics(clone)" in SOURCE


def test_correct_cloned_dynamics_scans_root_and_descendants() -> None:
    """AC15(ii): scans clone:GetDescendants() AND tests the clone ROOT itself."""
    body = _method_body("_correctClonedDynamics")
    assert "clone:GetDescendants()" in body
    # The clone root itself is tested (a Model-carrier clone is found by the
    # root test, not the descendant walk).
    assert 'clone:GetAttribute("_UnityMass")' in body


def test_correct_cloned_dynamics_is_class_agnostic() -> None:
    """AC15(ii): selection is on _UnityMass presence REGARDLESS of class --
    NOT gated on IsA("BasePart")."""
    body = _method_body("_correctClonedDynamics")
    assert 'GetAttribute("_UnityMass") ~= nil' in body
    assert 'IsA("BasePart")' not in body


def test_correct_cloned_dynamics_reads_plan_scalar_with_default() -> None:
    """AC15(iii): reads self._plan.gravityDesiredBaseStuds, with the documented
    fallback (STUDS_PER_METER * DEFAULT_UNITY_GRAVITY_Y) when absent."""
    body = _method_body("_correctClonedDynamics")
    assert "self._plan.gravityDesiredBaseStuds" in body
    assert "STUDS_PER_METER * DEFAULT_UNITY_GRAVITY_Y" in body


def test_correct_cloned_dynamics_calls_mirrored_helper() -> None:
    """AC15(iii): the hook calls the mirrored correctDynamicAssembly."""
    body = _method_body("_correctClonedDynamics")
    assert "_gravityCorrectDynamicAssembly(" in body


def test_correct_cloned_dynamics_defers_correction_for_weld_settle() -> None:
    """[MAJOR fix] The per-clone correction is routed through
    self._services.task.defer (matching the server spawn-hook DescendantAdded
    settle semantics) so a welded multi-part (S4) clone's AssemblyRootPart /
    AssemblyMass are read AFTER the welds settle, NOT synchronously off a
    temporary part. The _ScaleGravityCorrected tag then guarantees exactly-once
    across the deferred clone-site path AND the server DescendantAdded path for a
    server-side instantiatePrefab clone."""
    body = _method_body("_correctClonedDynamics")
    # The helper call is wrapped in a deferred closure (not invoked synchronously).
    assert "self._services.task.defer(" in body
    defer_idx = body.index("self._services.task.defer(")
    helper_idx = body.index("_gravityCorrectDynamicAssembly(")
    # The helper is invoked INSIDE the deferred closure, not before it.
    assert defer_idx < helper_idx


def test_runtime_has_no_blanket_workspace_descendantadded_gravity_sweep() -> None:
    """[P2 fix] NEGATIVE invariant: the runtime must NOT wire a blanket
    workspace.DescendantAdded-based gravity sweep (the rejected round-3 race).
    The ONLY gravity correction surface in scene_runtime.luau is the clone-site
    _correctClonedDynamics hook; a workspace-wide DescendantAdded gravity hook
    would re-introduce the replication race the design explicitly rejected.

    Scoped to gravity context (and to actual ``DescendantAdded:Connect`` WIRING,
    not mere comment mentions) so it does not false-trigger on the unrelated
    PlayerGui/character DescendantAdded hooks, nor on this method's own comments:
    assert no DescendantAdded HANDLER in the runtime sits in proximity to the
    gravity helper / tag."""
    import re

    gravity_markers = (
        "_gravityCorrectDynamicAssembly",
        "_ScaleGravityCorrected",
        "gravityDesiredBaseStuds",
        "correctDynamicAssembly",
    )
    # Only actual event WIRING (``...DescendantAdded:Connect(``), not comment
    # mentions of the word -- a comment cannot install a sweep.
    for m in re.finditer(r"DescendantAdded:Connect\b", SOURCE):
        window = SOURCE[max(0, m.start() - 400):m.start() + 400]
        for marker in gravity_markers:
            assert marker not in window, (
                "scene_runtime.luau wires a DescendantAdded-based gravity sweep "
                f"(found {marker!r} near a DescendantAdded:Connect) -- the rejected "
                "round-3 race; gravity correction must be the clone-site hook only"
            )


# ---------------------------------------------------------------------------
# AC8e -- Rigidbody2D carrier SKIPPED (2D exclusion, Physics2D OOS) -- client half
# ---------------------------------------------------------------------------

def test_client_hook_excludes_rigidbody2d_carriers() -> None:
    """AC8e (client half): the client scan gates on _Rigidbody2D == nil
    alongside the _UnityMass test, before calling correctDynamicAssembly --
    the same per-carrier 2D exclusion as the server surfaces. Covers both the
    non-wrapped (marker on the carrier) and mesh-wrapped (marker co-located on
    the inner carrier via the move-list) 2D bodies."""
    body = _method_body("_correctClonedDynamics")
    # Both the root test and the descendant walk gate on _Rigidbody2D == nil.
    assert body.count('GetAttribute("_Rigidbody2D") == nil') == 2


# ---------------------------------------------------------------------------
# AC8d -- Model-CARRIER (S3/S6) SKIPPED via skip-if-anchored -- client clone-site
# ---------------------------------------------------------------------------

def test_model_carrier_skipped_via_anchored_in_mirrored_helper() -> None:
    """AC8d: the client uses the SAME helper, so a clone whose ROOT is a Model
    carrier (anchored descendants) resolves an Anchored representative part and
    is SKIPPED via skip-if-anchored (no force, not tagged) -- identical to the
    server path. Assert the mirrored helper resolves a representative part and
    the anchored skip gates BEFORE any VectorForce creation."""
    # The mirror resolves a representative BasePart from a Model carrier.
    assert "carrier.PrimaryPart" in SOURCE
    assert 'carrier:FindFirstChildWhichIsA("BasePart", true)' in SOURCE
    assert "representativePart.AssemblyRootPart or representativePart" in SOURCE
    # skip-if-anchored gates before force creation in the mirror.
    anchored_idx = SOURCE.index("if root.Anchored then")
    create_idx = SOURCE.index('Instance.new("VectorForce")', anchored_idx - 5000)
    assert anchored_idx < create_idx


# ---------------------------------------------------------------------------
# AC14 (client-mirror half) -- shared tokens match the canonical helper
# ---------------------------------------------------------------------------

# Load-bearing tokens the canonical helper (autogen) and the scene_runtime.luau
# mirror MUST share verbatim (formula + skip-rules + fact-resolution + tag).
_SHARED_TOKENS = (
    # force formula
    "mass * (workspace.Gravity - desiredStuds)",
    "desiredStuds = desiredBaseStuds * gravityScale",
    "(useGravityAttr == false) and 0 or (gravityScaleAttr or 1.0)",
    # mass + apply-at-com
    "root.AssemblyMass",
    "ApplyAtCenterOfMass = true",
    # skip-rules
    "root:GetAttribute(TAG)",
    "if root.Anchored then",
    'FindFirstChildWhichIsA("Humanoid")',
    "if mass <= 0 then",
    # fact resolution (carrier-first, ancestor-Model walk)
    "local function factOf(carrier, key)",
    'factOf(carrier, "UseGravity")',
    'factOf(carrier, "GravityScale")',
    # representative-part resolution
    "carrier.PrimaryPart",
    'carrier:FindFirstChildWhichIsA("BasePart", true)',
    "representativePart.AssemblyRootPart or representativePart",
    # VectorForce shape
    'Instance.new("Attachment")',
    'Instance.new("VectorForce")',
    "vf.RelativeTo = Enum.ActuatorRelativeTo.World",
    "vf.Force = Vector3.new(0, force, 0)",
    "vf.Attachment0 = att",
    # tag
    "root:SetAttribute(TAG, true)",
)


def test_client_mirror_carries_canonical_helper_tokens() -> None:
    """AC14 (client-mirror half): every load-bearing helper token present in the
    canonical autogen text is also present in the scene_runtime.luau mirror."""
    for token in _SHARED_TOKENS:
        assert token in _GRAVITY_CORRECTION_HELPER_LUAU, (
            f"token missing from CANONICAL helper: {token!r}"
        )
        assert token in SOURCE, (
            f"token missing from scene_runtime.luau MIRROR: {token!r}"
        )


def test_humanoid_skip_uses_ancestor_model_form_not_dead_clause() -> None:
    """AC14: the mirror uses the ancestor-Model-contains-Humanoid form, NOT the
    dead FindFirstAncestorWhichIsA("Humanoid")."""
    assert 'FindFirstChildWhichIsA("Humanoid")' in SOURCE
    assert 'FindFirstAncestorWhichIsA("Humanoid")' not in SOURCE


def test_force_check_create_set_ordering_in_mirror() -> None:
    """AC14: in the mirror, tag-check + anchored-skip + mass-skip precede force
    creation, and SetAttribute(TAG) follows it (one force per root, idempotent)."""
    check_idx = SOURCE.index("root:GetAttribute(TAG)")
    create_idx = SOURCE.index('Instance.new("VectorForce")')
    set_idx = SOURCE.index("root:SetAttribute(TAG, true)")
    assert check_idx < create_idx < set_idx


# ---------------------------------------------------------------------------
# Syntax smoke test -- the emitted scene_runtime.luau loads under luau.
# ---------------------------------------------------------------------------

def _luau_available() -> bool:
    return shutil.which("luau") is not None


@pytest.mark.skipif(not _luau_available(), reason="luau interpreter not installed")
def test_scene_runtime_luau_loads() -> None:
    """The mirrored hook must keep scene_runtime.luau syntactically valid."""
    level = 0
    while ("]" + "=" * level + "]") in SOURCE:
        level += 1
    eq = "=" * level
    harness = (
        f"local SRC = [{eq}[\n{SOURCE}\n]{eq}]\n"
        'local chunk, err = loadstring(SRC, "scene_runtime")\n'
        'if not chunk then print("SYNTAX ERROR: " .. tostring(err))\n'
        'else print("OK") end\n'
    )
    with tempfile.NamedTemporaryFile(
        "w", suffix=".luau", encoding="utf-8", delete=False
    ) as fh:
        fh.write(harness)
        harness_path = fh.name
    try:
        result = subprocess.run(
            ["luau", harness_path],
            capture_output=True,
            text=True,
        )
    finally:
        Path(harness_path).unlink(missing_ok=True)
    out = (result.stdout or "") + (result.stderr or "")
    assert "OK" in out and "SYNTAX ERROR" not in out, out
