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

    def test_named_trigger_part_examples(self):
        # The AI gets concrete names to try first. SimpleFPS uses
        # 'Collider' (Turret) and 'PickupTouchDetector' (Pickup); other
        # projects may use 'Trigger'. Don't assert exhaustive coverage —
        # just that named-lookup is present.
        for name in ('"Collider"', '"Trigger"', '"PickupTouchDetector"'):
            assert name in _AI_SYSTEM_PROMPT, (
                f"Named-trigger example {name} missing — AI loses the "
                f"hint to prefer named lookup over first-BasePart."
            )

    def test_visible_filter_present(self):
        # The 'first non-trigger Part' fallback uses Transparency<1 as
        # the visible-vs-invisible heuristic. Imperfect but deterministic.
        assert "Transparency < 1" in _AI_SYSTEM_PROMPT or \
               "Transparency <" in _AI_SYSTEM_PROMPT, (
            "Visible-Part filter lost — AI falls back to "
            "FindFirstChildWhichIsA, which broke Pickup scripts."
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
