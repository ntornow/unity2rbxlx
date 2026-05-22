"""test_contract_reprompt.py -- PR3a verify-and-reprompt loop coverage.

The ``_verify_and_reprompt`` helper is the orchestration hinge: on a
violation it calls the backend's reprompt closure once and re-verifies.
Tests use a fake reprompt closure so the AI is never invoked -- we only
care about the control flow:

  - clean input -> no reprompt, no warnings
  - bad input + reprompt that fixes -> clean output, no warnings
  - bad input + reprompt that doesn't fix -> remaining violations as warnings
  - bad input + reprompt that returns None (backend failure) -> original output,
    every original violation surfaced as a warning
  - legacy mode -> verifier dormant (no-op regardless of input)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import (  # noqa: E402
    _format_contract_violations,
    _verify_and_reprompt,
)
from converter.runtime_contract import verify_module  # noqa: E402


CLEAN = """\
local Class = {}
Class.__index = Class
function Class.new(config)
    return setmetatable({}, Class)
end
function Class:Awake() end
return Class
"""

# A module that violates rule (a) -- top-level side-effecting call.
RULE_A_BROKEN = """\
print("loaded")
local Class = {}
function Class.new(config) return setmetatable({}, Class) end
return Class
"""


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

class TestVerifyAndReprompt:

    def test_clean_input_skips_reprompt(self):
        # When the initial output is already contract-compliant, the
        # reprompt closure must NOT be invoked.
        calls: list[str] = []

        def reprompt(msg: str):
            calls.append(msg)
            return CLEAN

        out, warnings = _verify_and_reprompt(
            CLEAN, "csharp", "generic", reprompt,
        )
        assert out == CLEAN
        assert warnings == []
        assert calls == [], (
            "Reprompt closure was called on already-compliant input."
        )

    def test_reprompt_fixes_violation(self):
        # Backend reprompt returns a compliant version; the FINAL output
        # is the compliant one and no ``contract-verifier`` (post-reprompt)
        # warnings remain. ``contract-verifier-pre`` warnings DO survive
        # so the compliance spike can count "reprompt-rescued" modules
        # separately from "first-attempt clean" modules.
        calls: list[str] = []

        def reprompt(msg: str):
            calls.append(msg)
            return CLEAN

        out, warnings = _verify_and_reprompt(
            RULE_A_BROKEN, "csharp", "generic", reprompt,
        )
        assert out.strip() == CLEAN.strip()
        # No post-reprompt failures.
        assert not any(
            w.startswith("contract-verifier ") or w.startswith("contract-verifier:")
            for w in warnings
        ), f"unexpected post-reprompt warnings: {warnings}"
        # But the pre-reprompt warning IS preserved as a trace -- this
        # is what the spike uses to compute pre/post pass-rate delta.
        assert any(w.startswith("contract-verifier-pre") for w in warnings), (
            f"reprompt rescued the module but the pre-reprompt trace "
            f"warning was lost: {warnings}"
        )
        assert len(calls) == 1, (
            "Reprompt should be called exactly once per module under PR3a "
            "(design doc: 'reprompts the AI once with the specific "
            "violation')."
        )

    def test_reprompt_passes_violation_feedback(self):
        # The reprompt user message must mention rule (a) and the
        # violating line so the AI has something to act on.
        seen: list[str] = []

        def reprompt(msg: str):
            seen.append(msg)
            return CLEAN

        _verify_and_reprompt(
            RULE_A_BROKEN, "csharp", "generic", reprompt,
        )
        assert seen, "reprompt closure should have been called"
        msg = seen[0]
        assert "rule a" in msg.lower(), (
            f"Reprompt message lost the rule letter (expected 'rule a'): {msg[:200]}"
        )
        # The line number where ``print("loaded")`` sits is line 1.
        assert "line 1" in msg, (
            f"Reprompt message lost the line number: {msg[:200]}"
        )


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

class TestReprompts:

    def test_reprompt_still_failing_surfaces_warnings(self):
        # Backend returns a still-non-compliant fix. The orchestrator
        # downstream (transpile_with_contract) fails closed at the
        # project level; here we only assert the per-module warnings
        # carry the rule labels.
        broken_again = "do_something()\n" + CLEAN  # still rule (a)

        def reprompt(msg: str):
            return broken_again

        out, warnings = _verify_and_reprompt(
            RULE_A_BROKEN, "csharp", "generic", reprompt,
        )
        # Output IS the reprompt's result (it's our best effort -- the
        # ``_strip_code_fences`` pass rstrips the AI output, so we
        # compare on content rather than exact whitespace).
        assert out.strip() == broken_again.strip()
        assert warnings, "remaining violations must be surfaced as warnings"
        assert any("rule a" in w for w in warnings), (
            f"warnings lost rule-letter labels: {warnings}"
        )

    def test_reprompt_returning_none_surfaces_original_violations(self):
        # Backend reprompt fails entirely (e.g. CLI unavailable). The
        # original (broken) source flows out unchanged; every initial
        # violation is recorded as a warning for the orchestrator.
        def reprompt(msg: str):
            return None

        out, warnings = _verify_and_reprompt(
            RULE_A_BROKEN, "csharp", "generic", reprompt,
        )
        assert out == RULE_A_BROKEN, (
            "Backend reprompt failure should leave the original source "
            "untouched (orchestrator decides fallback)."
        )
        assert warnings, "warnings must carry the verifier's violations"

    def test_reprompt_with_empty_string_keeps_original(self):
        # Some CLIs return empty stdout on certain errors. Treat as
        # backend failure -- don't overwrite with empty Luau.
        def reprompt(msg: str):
            return ""

        out, warnings = _verify_and_reprompt(
            RULE_A_BROKEN, "csharp", "generic", reprompt,
        )
        assert out == RULE_A_BROKEN
        assert warnings


# ---------------------------------------------------------------------------
# Mode gating
# ---------------------------------------------------------------------------

class TestModeGating:

    def test_legacy_mode_skips_verifier_entirely(self):
        # Under legacy, the contract verifier must NEVER run -- the
        # legacy repair layer handles non-compliant shapes its own way.
        # Even if the input is wildly non-compliant, the helper returns
        # it untouched.
        calls: list[str] = []

        def reprompt(msg: str):
            calls.append(msg)
            return CLEAN

        out, warnings = _verify_and_reprompt(
            RULE_A_BROKEN, "csharp", "legacy", reprompt,
        )
        assert out == RULE_A_BROKEN, (
            "Legacy-mode pipeline must not rewrite Luau; the contract "
            "verifier is generic-only."
        )
        assert warnings == []
        assert calls == [], (
            "Reprompt called under legacy mode -- the verifier is supposed "
            "to be dormant outside generic."
        )


# ---------------------------------------------------------------------------
# Violation formatting -- the reprompt's input format must be parseable
# enough for the AI to act on. Smoke check the structure.
# ---------------------------------------------------------------------------

class TestViolationFormatting:

    def test_formatter_numbers_each_violation(self):
        result = verify_module(RULE_A_BROKEN)
        text = _format_contract_violations(result.violations)
        # One entry per violation; numbered 1., 2., ...
        for i, _ in enumerate(result.violations):
            assert f"{i + 1}." in text

    def test_formatter_includes_rule_and_line(self):
        result = verify_module(RULE_A_BROKEN)
        text = _format_contract_violations(result.violations)
        # ``[rule X]`` + ``line N`` per the contract reprompt format.
        assert "[rule" in text
        assert "line" in text
