"""
test_no_rejected_bridges.py -- Regression guard for the
inline-over-runtime-wrappers policy.

See docs/design/inline-over-runtime-wrappers.md.

Nine runtime wrappers were removed in favor of inline translations in
api_mappings.py, regex fixes in luau_validator.py, and consolidation
into animator_runtime.luau. This test asserts they stay deleted, so
that if anyone regenerates the bridge layer from scratch later, CI
will remind them of the decision before it lands.
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
]


def test_rejected_runtime_bridges_do_not_exist():
    """The nine rejected wrappers must not reappear in converter/runtime/.

    If this test fails, either (a) you're legitimately restoring one of
    these modules and should update the design doc + this test, or (b)
    a rebase or merge resurrected a deleted file and you should delete
    it again.
    """
    surviving = [
        name for name in _REJECTED_BRIDGES if (RUNTIME_DIR / name).exists()
    ]
    assert not surviving, (
        f"Rejected runtime bridges reappeared in {RUNTIME_DIR}: {surviving}. "
        "See docs/design/inline-over-runtime-wrappers.md for why these were removed. "
        "If you are intentionally restoring them, update the design doc and this "
        "test together."
    )


def test_bridge_injector_does_not_exist():
    """The bridge_injector scanner was orphaned after the wrappers were
    removed; it should not reappear either."""
    for rel in _REJECTED_PYTHON_MODULES:
        path = CONVERTER_ROOT / rel
        assert not path.exists(), (
            f"{rel} reappeared. See docs/design/inline-over-runtime-wrappers.md."
        )


def test_api_mappings_still_inlines_covered_apis():
    """The inline mappings that replaced the wrappers must still be
    present. If one of these goes missing, a transpiled script will
    either emit the raw Unity call or rely on a runtime wrapper we no
    longer ship.
    """
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

    # Input.luau new utilities (axis + swipe) — these were added as part
    # of the deletion, not pre-existing.
    assert API_CALL_MAP.get("Input.GetSwipe") == "getSwipe"
    assert "inputHorizontal" in UTILITY_FUNCTIONS
    assert "inputVertical" in UTILITY_FUNCTIONS
    assert "getSwipe" in UTILITY_FUNCTIONS

    # GameObjectUtil.luau replacements
    assert "Instantiate" in API_CALL_MAP
    assert "Destroy" in API_CALL_MAP
    assert "GameObject.Find" in API_CALL_MAP
    assert "GameObject.FindWithTag" in API_CALL_MAP

    # MonoBehaviour.luau lifecycle coverage (sampled — full check is
    # in the LIFECYCLE_MAP tests)
    from converter.api_mappings import LIFECYCLE_MAP
    for hook in [
        "Awake", "Start", "Update", "FixedUpdate", "LateUpdate",
        "OnEnable", "OnDisable", "OnDestroy",
        "OnCollisionEnter", "OnTriggerEnter",
    ]:
        assert hook in LIFECYCLE_MAP, f"Lifecycle hook {hook} missing from LIFECYCLE_MAP"


def test_animator_runtime_has_consolidated_features():
    """animator_runtime.luau must contain features merged from
    animator_bridge.luau (getters, Play, blend trees, Any-state,
    Destroy). If this test fails, the consolidation was reverted."""
    runtime_path = RUNTIME_DIR / "animator_runtime.luau"
    assert runtime_path.exists(), "animator_runtime.luau is missing"
    source = runtime_path.read_text(encoding="utf-8")

    for method in ["GetFloat", "GetBool", "GetInt", "Play",
                    "Destroy", "_startBlendTree", "_updateBlendTree",
                    "_lazyLoadTrack", "anyStateTransitions"]:
        assert method in source, (
            f"animator_runtime.luau missing consolidated method/feature: {method}"
        )
