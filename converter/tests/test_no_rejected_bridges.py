"""Regression guard for inline-over-runtime-wrappers policy.

See docs/design/inline-over-runtime-wrappers.md.
"""

from pathlib import Path

CONVERTER_ROOT = Path(__file__).resolve().parent.parent
RUNTIME_DIR = CONVERTER_ROOT / "runtime"


_REJECTED_BRIDGES = [
    "Input.luau",
    "Time.luau",
    "MonoBehaviour.luau",
    "Coroutine.luau",
    "GameObjectUtil.luau",
    "StateMachine.luau",
    "physics_queries.luau",
    "animator_bridge.luau",
    "TransformAnimator.luau",
]


_REJECTED_PYTHON_MODULES = [
    "converter/bridge_injector.py",
    # mesh_splitter was ported for Phase 3 item 4 (split multi-material
    # FBX meshes into per-material OBJs). Superseded by the sub-mesh
    # hierarchy path in scene_converter, which routes each material slot
    # to a MeshPart child using mesh_hierarchies from Studio resolution.
    # See docs/design/merge-plan-phase-3-augmented.md.
    "converter/mesh_splitter.py",
]


def test_rejected_runtime_bridges_do_not_exist():
    surviving = [
        name for name in _REJECTED_BRIDGES if (RUNTIME_DIR / name).exists()
    ]
    assert not surviving, f"Rejected bridges reappeared: {surviving}"


def test_rejected_python_modules_do_not_exist():
    """bridge_injector and mesh_splitter were ported then rejected; guard against reappearance."""
    for rel in _REJECTED_PYTHON_MODULES:
        path = CONVERTER_ROOT / rel
        assert not path.exists(), (
            f"{rel} reappeared. See docs/design/inline-over-runtime-wrappers.md "
            f"and docs/design/merge-plan-phase-3-augmented.md for the rationale."
        )


def test_api_mappings_still_inlines_covered_apis():
    from converter.api_mappings import API_CALL_MAP, UTILITY_FUNCTIONS

    # Time.luau replacements
    assert API_CALL_MAP.get("Time.deltaTime") == "dt"
    assert API_CALL_MAP.get("Time.fixedDeltaTime") == "dt"
    assert "Time.time" in API_CALL_MAP
    assert "Time.timeScale" in API_CALL_MAP

    # Coroutine.luau replacements
    assert API_CALL_MAP.get("StartCoroutine") == "task.spawn"

    # physics_queries.luau replacements
    assert API_CALL_MAP.get("Physics.Raycast") == "workspace:Raycast"
    assert "Physics.OverlapSphere" in API_CALL_MAP

    # Input.luau replacements (key/mouse)
    assert "Input.GetKey" in API_CALL_MAP
    assert "Input.GetKeyDown" in API_CALL_MAP
    assert "Input.GetMouseButton" in API_CALL_MAP

    assert API_CALL_MAP.get("Input.GetSwipe") == "getSwipe"
    assert "inputHorizontal" in UTILITY_FUNCTIONS
    assert "inputVertical" in UTILITY_FUNCTIONS
    assert "getSwipe" in UTILITY_FUNCTIONS

    # GameObjectUtil.luau replacements
    assert "Instantiate" in API_CALL_MAP
    assert "Destroy" in API_CALL_MAP
    assert "GameObject.Find" in API_CALL_MAP
    assert "GameObject.FindWithTag" in API_CALL_MAP

    from converter.api_mappings import LIFECYCLE_MAP
    for hook in [
        "Awake", "Start", "Update", "FixedUpdate", "LateUpdate",
        "OnEnable", "OnDisable", "OnDestroy",
        "OnCollisionEnter", "OnTriggerEnter",
    ]:
        assert hook in LIFECYCLE_MAP, f"Lifecycle hook {hook} missing from LIFECYCLE_MAP"


def test_animator_runtime_has_consolidated_features():
    source = (RUNTIME_DIR / "animator_runtime.luau").read_text()
    for method in ["GetFloat", "GetBool", "GetInt", "Play",
                    "Destroy", "_startBlendTree", "_updateBlendTree",
                    "_lazyLoadTrack", "anyStateTransitions"]:
        assert method in source, f"missing: {method}"


def test_generated_transform_only_script_has_no_deleted_bridge_require():
    """Phase 4.5: transform-only animation scripts are inline TweenService —
    they must not require() any deleted runtime bridge.
    """
    from converter.animation_converter import (
        AnimClip, AnimCurve, AnimKeyframe, generate_tween_script,
    )

    clip = AnimClip(
        name="Spin",
        duration=1.0,
        loop=True,
        sample_rate=30.0,
        curves=[
            AnimCurve(
                property_type="euler",
                path="Spinner",
                keyframes=[
                    AnimKeyframe(time=0.0, value=(0, 0, 0)),
                    AnimKeyframe(time=1.0, value=(0, 360, 0)),
                ],
            ),
        ],
    )
    source = generate_tween_script(clip=clip)
    assert source is not None
    for bad in ("AnimatorBridge", "TransformAnimator",
                "bridge/TransformAnimator", "bridge/AnimatorBridge"):
        assert bad not in source, f"generated transform-only script references {bad}"


def test_animator_runtime_luau_syntax():
    source = (RUNTIME_DIR / "animator_runtime.luau").read_text()
    lines = source.splitlines()

    # No leftover --- docstring blocks (slop indicator)
    triple_dash = [i for i, l in enumerate(lines, 1) if l.strip().startswith("---")]
    assert not triple_dash, f"--- docstrings at lines {triple_dash}"

    # Must return the module table
    assert lines[-1].strip() == "return AnimatorRuntime"

    # Sanity: expected method count (22 functions after consolidation)
    func_count = sum(1 for l in lines if l.strip().startswith("function ") or
                     l.strip().startswith("local function "))
    assert func_count >= 20, f"unexpectedly few functions: {func_count}"
