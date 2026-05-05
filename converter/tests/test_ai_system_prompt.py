"""
test_ai_system_prompt.py — Structural assertions on the AI transpiler prompt.

The fast suite has no other coverage of the AI prompt itself
(``test_script_transpilation`` is marked slow because it invokes ``claude -p``
live whenever ``LLM_CACHE_DIR`` is cold — see ``test_integration.py:152``).
These tests are cheap text-level invariants: they don't run the AI, just
verify the prompt string still teaches the patterns we depend on. If a
future edit accidentally drops one, CI catches it instead of waiting for
a real game to break.

The asserts are deliberately on stable identifiers (Roblox API names,
section headers) rather than prose so prompt rewording doesn't churn
the test.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import _AI_SYSTEM_PROMPT


class TestModelDispatchSection:
    """The 'Model vs Part dispatch' subsection teaches the AI to emit a
    helper that handles ``script.Parent`` being either a BasePart or a
    Model. Reproducer for why this exists: SimpleFPS Pickup.cs translated
    to a script that called ``part.CFrame * Angles(...)`` on the first
    BasePart child of a Model — which was the invisible trigger zone, so
    rotation animated nothing visible.
    """

    def test_dispatch_section_present(self):
        assert "Model vs Part dispatch" in _AI_SYSTEM_PROMPT, (
            "Section header missing — the AI loses the helper template."
        )

    def test_helper_function_names(self):
        # The three helpers the dispatch teaches.
        for name in ("getCFrame", "setCFrame", "getPosition"):
            assert f"local function {name}" in _AI_SYSTEM_PROMPT, (
                f"Helper '{name}' missing from prompt template; AI won't "
                f"emit dispatch for transform.X reads/writes on Models."
            )

    def test_pivot_apis_taught(self):
        # Models use :GetPivot / :PivotTo, not .CFrame / .Position.
        for api in (":PivotTo", ":GetPivot"):
            assert api in _AI_SYSTEM_PROMPT, (
                f"Roblox Model API '{api}' missing — AI may translate "
                f"transform.X to BasePart-only ``part.CFrame =`` and "
                f"silently fail on Model-rooted prefabs."
            )

    def test_primarypart_pinning_present(self):
        # Without PrimaryPart, GetPivot/PivotTo use the bounding-box
        # centre. Compose-rotation patterns then rotate around the wrong
        # pivot. The dispatch must tell the AI to pin a PrimaryPart at
        # script init.
        assert "PrimaryPart" in _AI_SYSTEM_PROMPT, (
            "PrimaryPart pinning lost from prompt — Models without an "
            "authored PrimaryPart will rotate around bounding-box centre."
        )
        # Specific shape: assignment, not just mention.
        assert "container.PrimaryPart =" in _AI_SYSTEM_PROMPT or \
               "PrimaryPart =" in _AI_SYSTEM_PROMPT, (
            "PrimaryPart referenced but never assigned — pinning code "
            "got dropped on a refactor."
        )

    def test_isa_model_branching(self):
        # Dispatch must literally branch on ``:IsA("Model")`` — that's
        # the gate that decides BasePart vs Model code path. Without it
        # the helpers degenerate to the BasePart-only path.
        assert ':IsA("Model")' in _AI_SYSTEM_PROMPT


class TestTriggerPartGuidance:
    """OnTriggerEnter → Touched translation. The naive
    ``FindFirstChildWhichIsA("BasePart")`` picks the FIRST BasePart child,
    which is usually an invisible trigger zone in Unity prefabs. The
    prompt must steer the AI away from that pattern.
    """

    def test_warns_about_findfirstchildwhichisa_basepart(self):
        # Verbatim warning so the prompt stays explicit about WHY the
        # naive pattern is wrong.
        assert "FindFirstChildWhichIsA" in _AI_SYSTEM_PROMPT
        assert "WRONG one" in _AI_SYSTEM_PROMPT, (
            "Lost the explicit 'returns the WRONG one' warning. "
            "Without it the AI may revert to the naive lookup."
        )

    def test_trigger_name_list_covers_common_unity_conventions(self):
        # Unity prefab authors use a variety of names for the trigger
        # GameObject. SimpleFPS specifically used "Collider" (Turret) and
        # "PickupTouchDetector" (Pickup), but other projects ship with
        # different names. Codex P2 review noted the original list was
        # SimpleFPS-flavored; expanded to cover broader convention.
        for name in (
            '"Collider"', '"Trigger"', '"TriggerZone"', '"Detector"',
            '"Sensor"', '"Hitbox"', '"Range"', '"ProximityVolume"',
            '"PickupTouchDetector"',
        ):
            assert name in _AI_SYSTEM_PROMPT, (
                f"Trigger name {name} missing from TRIGGER_NAMES list — "
                f"AI may fail to find the trigger Part on prefabs that "
                f"use this naming convention."
            )

    def test_finder_helpers_taught_as_tiered_functions(self):
        # The improved guidance emits ``findTriggerPart`` and
        # ``findVisualTarget`` helpers with a tiered fallback rather
        # than relying on ``Transparency < 1`` alone. Tier 1 (named
        # lookup) catches the common case; tiers 2-4 degrade gracefully
        # so non-converter-emitted prefabs still get a reasonable target.
        assert "findTriggerPart" in _AI_SYSTEM_PROMPT, (
            "Lost ``findTriggerPart`` helper definition — AI falls back "
            "to inline lookups that the Pickup script bug originated from."
        )
        assert "findVisualTarget" in _AI_SYSTEM_PROMPT, (
            "Lost ``findVisualTarget`` helper — animations would target "
            "the trigger zone again."
        )

    def test_visual_target_prefers_models_then_meshparts_then_visible(self):
        # The tiered finder must teach: Model child first (Unity
        # pickups), then MeshPart (mesh implies visual), then
        # Transparency<1 fallback. The exact order matters because
        # tiers 1-2 catch the common cases without depending on the
        # imperfect Transparency proxy.
        assert ':IsA("Model")' in _AI_SYSTEM_PROMPT
        assert ':IsA("MeshPart")' in _AI_SYSTEM_PROMPT
        assert "Transparency < 1" in _AI_SYSTEM_PROMPT, (
            "Visible-Part fallback lost. Tiers 1-2 cover most cases "
            "but tier 3 is the only thing that handles plain Parts."
        )
        # Order check: Model tier should appear before Transparency
        # tier in the prompt text. Brittle but cheap.
        model_pos = _AI_SYSTEM_PROMPT.index(':IsA("Model") and c.Name ~= "MinimapIcon"')
        transp_pos = _AI_SYSTEM_PROMPT.index("Transparency < 1")
        assert model_pos < transp_pos, (
            "Tiered finder reordered — Model preference must come "
            "before the Transparency<1 fallback; otherwise we hit the "
            "imperfect proxy first and skip the cleaner case."
        )


class TestReparentingNote:
    """The dispatch helper captures ``script.Parent`` once at init.
    That's correct for typical Unity scripts, but breaks if the script
    reparents itself at runtime. Edge case but worth flagging — codex
    P2 review noted the gap.
    """

    def test_reparenting_caveat_present(self):
        # The note tells the AI when to inline ``script.Parent`` instead
        # of capturing.
        assert "reparent" in _AI_SYSTEM_PROMPT.lower(), (
            "Lost the reparenting caveat — AI may emit captured "
            "``container`` for self-reparenting scripts and silently "
            "operate on the original parent forever."
        )


class TestSlowMarkExcludesScriptTranspilation:
    """``@pytest.mark.slow`` on ``test_script_transpilation`` (added in
    this PR) must actually exclude the test from ``-m 'not slow'``. The
    test invokes ``claude -p`` live when ``LLM_CACHE_DIR`` is cold — a
    real risk because the AI prompt is the cache key, so any prompt
    edit (this PR is exactly such an edit) invalidates every cached
    entry. If the mark doesn't take, the fast suite hangs for 5+ min
    per script in CI.
    """

    def test_collection_excludes_slow_test(self, pytestconfig):
        # Programmatic collection: ask pytest which test items match
        # ``-m "not slow"`` against the integration file, then assert
        # ``test_script_transpilation`` isn't among them.
        import subprocess
        repo_root = Path(__file__).parent.parent
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                "tests/test_integration.py",
                "-m", "not slow",
                "--collect-only", "-q",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # ``-q --collect-only`` prints one line per collected nodeid.
        collected = result.stdout
        assert "test_script_transpilation" not in collected, (
            "@pytest.mark.slow not honored — test_script_transpilation "
            "got collected by -m 'not slow'. Check the marker is on the "
            "method, not stripped by a decorator-eating linter, and "
            "that ``slow`` is registered in pyproject.toml."
        )

    def test_collection_includes_slow_test_when_unfiltered(self, pytestconfig):
        # Sanity check: without the filter, the test IS collected.
        # Otherwise the previous test passes trivially (e.g. if the
        # node id changed and we're matching the wrong string).
        import subprocess
        repo_root = Path(__file__).parent.parent
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest",
                "tests/test_integration.py",
                "--collect-only", "-q",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "test_script_transpilation" in result.stdout, (
            "Sanity check failed: test_script_transpilation not in the "
            "unfiltered collection. The previous assertion is meaningless "
            "without this. Maybe the test was renamed?"
        )
