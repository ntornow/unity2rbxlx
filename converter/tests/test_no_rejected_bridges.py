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


def test_character_animator_has_consolidated_features():
    source = (RUNTIME_DIR / "character_animator.luau").read_text()
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
    # Check every ``require(...)`` call with a paren-balanced scanner —
    # a character-class like ``[^)]*`` stops at the first close-paren
    # and misses idiomatic Luau forms like:
    #     require(game:GetService("ReplicatedStorage"):FindFirstChild("AnimatorBridge"))
    # Plain mentions outside a require() are fine (the inline-policy
    # header comment names the deleted bridges on purpose).
    for bad in ("AnimatorBridge", "TransformAnimator"):
        for arg in _require_arguments(source):
            assert bad not in arg, (
                f"generated transform-only script require()s {bad}: {arg!r}"
            )


def test_require_arguments_catches_nested_service_lookup():
    """Sanity: the new paren-balanced scanner must flag the exact
    nested form the old regex missed.
    """
    luau = 'local Bridge = require(game:GetService("ReplicatedStorage"):FindFirstChild("AnimatorBridge"))\n'
    args = _require_arguments(luau)
    assert len(args) == 1
    assert "AnimatorBridge" in args[0]

    # Control: a plain mention in a comment is NOT captured.
    luau_comment = '-- AnimatorBridge is removed; see inline-over-runtime-wrappers.md\nlocal t = {}\n'
    assert _require_arguments(luau_comment) == []


def _require_arguments(source: str) -> list[str]:
    """Return the argument text of every ``require(...)`` call.

    Reads balanced parens starting at each ``require(``. Does not try
    to ignore requires inside string literals — Luau scripts rarely
    contain the literal text ``require(`` in a string, and a false
    positive here only means the test is stricter.
    """
    results: list[str] = []
    i = 0
    while True:
        idx = source.find("require", i)
        if idx == -1:
            break
        j = idx + len("require")
        while j < len(source) and source[j] in " \t\n\r":
            j += 1
        if j >= len(source) or source[j] != "(":
            i = idx + 1
            continue
        start = j + 1
        depth = 1
        k = start
        while k < len(source) and depth > 0:
            ch = source[k]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            k += 1
        if depth == 0:
            results.append(source[start:k - 1])
            i = k
        else:
            # Unbalanced — bail to avoid infinite loop.
            break
    return results


def test_character_animator_luau_syntax():
    source = (RUNTIME_DIR / "character_animator.luau").read_text()
    lines = source.splitlines()

    # No leftover --- docstring blocks (slop indicator)
    triple_dash = [i for i, l in enumerate(lines, 1) if l.strip().startswith("---")]
    assert not triple_dash, f"--- docstrings at lines {triple_dash}"

    # Must return the module table
    assert lines[-1].strip() == "return CharacterAnimator"

    # Sanity: expected method count (22 functions after consolidation)
    func_count = sum(1 for l in lines if l.strip().startswith("function ") or
                     l.strip().startswith("local function "))
    assert func_count >= 20, f"unexpectedly few functions: {func_count}"
