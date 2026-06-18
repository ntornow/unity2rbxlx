"""test_roblox_call_repair.py -- Slice 2.2 bounded agentic repair coverage.

The ``_repair_invalid_roblox_calls`` helper is the transpile-time repair hinge:
on a PROVEN hallucinated Roblox method call it reprompts the backend (a closure)
with structured, corpus-grounded feedback to REGENERATE corrected Luau, bounded
to <=2 tries, then returns whatever it produced (a survivor is handed downstream
to the universal net -- never silently shipped clean, never a crash).

These tests inject fake reprompt closures so the AI is never invoked. The
cache-hit test seeds a real cache entry with ``_save_cache`` and mocks the
``anthropic`` client (mirroring ``test_player_verifier_reject.py``) to prove a
proven-invalid cached Luau triggers a re-transpile.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter import code_transpiler  # noqa: E402
from converter.code_transpiler import (  # noqa: E402
    _ai_cache_key,
    _ai_transpile,
    _repair_invalid_roblox_calls,
    _save_cache,
    _select_prompt,
)
from converter.roblox_call_validator import find_invalid_roblox_calls  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------

# A hand-broken bullet: ``char = plr.Character`` (proven Roblox receiver) calls
# the HALLUCINATED ``FindFirstChildOfType``. The validator flags this proven.
BROKEN_BULLET = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:OnTouch(plr)
    local char = plr.Character
    local hum = char:FindFirstChildOfType("Humanoid")
    if hum then hum:TakeDamage(10) end
end
return Class
"""

# The corrected version a good reprompt would return: the real method.
FIXED_BULLET = BROKEN_BULLET.replace(
    "FindFirstChildOfType", "FindFirstChildWhichIsA",
)

# An UNPROVEN invalid: ``gm`` is a host component (findObjectOfType), so a
# hallucinated method on it is NOT proven -> must NOT trigger repair.
UNPROVEN_INVALID = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Update()
    local gm = self.host.findObjectOfType("GameManager")
    gm:FakeMethod()
end
return Class
"""

# Clean Luau: a real Roblox method on a proven receiver, no invalids.
CLEAN = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Update()
    local part = workspace:FindFirstChild("Target")
    if part then part:Destroy() end
end
return Class
"""


def _proven(source: str) -> list[str]:
    return [
        inv["method"]
        for inv in find_invalid_roblox_calls(source)
        if inv["receiver_provenance"] == "proven"
    ]


# ---------------------------------------------------------------------------
# Helper-level behavior.
# ---------------------------------------------------------------------------

class TestRepairHelper:

    def test_reprompt_fixes_proven_invalid(self):
        # A fake closure that returns the corrected Find* version -> the helper
        # returns corrected source with NO survivor warning.
        seen: list[str] = []

        def reprompt(msg: str) -> str:
            seen.append(msg)
            return FIXED_BULLET

        out, warnings = _repair_invalid_roblox_calls(BROKEN_BULLET, reprompt)
        assert _proven(out) == [], f"output still has proven invalids: {out}"
        assert out.strip() == FIXED_BULLET.strip()
        assert not any("roblox-call-survivor" in w for w in warnings), (
            f"unexpected survivor warning after a successful fix: {warnings}"
        )
        assert len(seen) == 1, "reprompt should fire exactly once for the fix"

    def test_repair_message_carries_candidates_and_signatures(self):
        # The structured message must ground the model: candidate method names
        # + a human-readable signature for the broken site.
        seen: list[str] = []

        def reprompt(msg: str) -> str:
            seen.append(msg)
            return FIXED_BULLET

        _repair_invalid_roblox_calls(BROKEN_BULLET, reprompt)
        assert seen, "reprompt closure should have been called"
        msg = seen[0]
        # The real Find* candidates the corpus suggests.
        assert "FindFirstChildOfClass" in msg or "FindFirstChildWhichIsA" in msg, (
            f"repair message lost the corpus candidates: {msg}"
        )
        # A signature (name + a typed param) for a candidate.
        assert "className: string" in msg, (
            f"repair message lost the candidate signature: {msg}"
        )
        # The offending method + line, and the "DO NOT EXIST" framing.
        assert "FindFirstChildOfType" in msg
        assert "DO NOT EXIST" in msg
        assert "line 5" in msg, f"repair message lost the line number: {msg}"

    def test_unfixed_after_two_tries_surfaces_survivor(self):
        # A fake closure that returns the SAME broken Luau -> after 2 tries the
        # helper returns still-broken source + a survivor warning (NOT a crash,
        # NOT a silently-clean verdict).
        calls: list[str] = []

        def reprompt(msg: str) -> str:
            calls.append(msg)
            return BROKEN_BULLET

        out, warnings = _repair_invalid_roblox_calls(BROKEN_BULLET, reprompt)
        assert _proven(out), "the proven invalid must still be present"
        assert any("roblox-call-survivor" in w for w in warnings), (
            f"a surviving proven invalid must be tagged: {warnings}"
        )
        assert any("FindFirstChildOfType" in w for w in warnings)
        assert len(calls) == 2, (
            f"repair must be bounded to 2 tries (got {len(calls)})"
        )

    def test_degraded_repair_keeps_best_and_surfaces_survivor(self):
        # Criterion 6 (slice 1.1): a repair response that is a structural
        # regression (a ``...`` fragment that lost the top-level return and
        # collapsed in length) is REJECTED -> the guard breaks, keeping the
        # prior ``best`` (the original broken bullet) and surfacing its survivor
        # warning. The closure fires ONCE (break on the degraded response, no
        # second try).
        calls: list[str] = []

        # A degraded fragment: no top-level return, ~1/8 the original length.
        degraded = (
            "function Class:OnTouch(plr)\n"
            "    -- ...\n"
            "end\n"
        )

        def reprompt(msg: str) -> str:
            calls.append(msg)
            return degraded

        out, warnings = _repair_invalid_roblox_calls(BROKEN_BULLET, reprompt)
        assert out == BROKEN_BULLET, (
            "a degraded repair response must leave the prior best untouched"
        )
        assert _proven(out), "the original proven invalid must still be present"
        assert any("roblox-call-survivor" in w for w in warnings), (
            f"the surviving proven invalid must be tagged: {warnings}"
        )
        assert any("FindFirstChildOfType" in w for w in warnings)
        assert len(calls) == 1, (
            f"the guard must break on the degraded response, not retry "
            f"(got {len(calls)} calls)"
        )

    def test_unproven_invalid_not_repaired(self):
        # A hallucinated method on a host-component receiver is UNPROVEN ->
        # no reprompt, returned unchanged.
        calls: list[str] = []

        def reprompt(msg: str) -> str:
            calls.append(msg)
            return CLEAN

        out, warnings = _repair_invalid_roblox_calls(UNPROVEN_INVALID, reprompt)
        assert out == UNPROVEN_INVALID
        assert warnings == []
        assert calls == [], (
            "an unproven invalid must NOT trigger a reprompt (only proven)"
        )

    def test_clean_luau_no_reprompt(self):
        calls: list[str] = []

        def reprompt(msg: str) -> str:
            calls.append(msg)
            return CLEAN

        out, warnings = _repair_invalid_roblox_calls(CLEAN, reprompt)
        assert out == CLEAN
        assert warnings == []
        assert calls == [], "clean Luau must NOT trigger a reprompt"

    def test_backend_failure_returns_best_with_survivor(self):
        # A closure returning None (backend failure) -> the original broken
        # source flows out + a survivor warning (not a crash, not clean).
        def reprompt(msg: str) -> None:
            return None

        out, warnings = _repair_invalid_roblox_calls(BROKEN_BULLET, reprompt)
        assert out == BROKEN_BULLET
        assert any("roblox-call-survivor" in w for w in warnings)


# ---------------------------------------------------------------------------
# Cache-hit re-transpile -- a proven-invalid cached Luau must NOT be served;
# it falls through to the cold path (which runs the repair). Mirrors the
# existing syntax-error cache invalidation. Driven through the REAL
# ``_ai_transpile`` body with the anthropic client mocked.
# ---------------------------------------------------------------------------

class _FakeMessages:
    """``client.messages`` stub. ``create`` returns the FIXED bullet so a cold
    re-transpile produces clean output. Counts calls so the test can prove the
    cold path (not a cache hit) ran."""

    def __init__(self, output: str) -> None:
        self._output = output
        self.calls: list[str] = []

    def create(self, *, model, max_tokens, system, messages):
        self.calls.append(messages[0]["content"])
        block = types.SimpleNamespace(text=self._output)
        return types.SimpleNamespace(content=[block])


class _FakeClient:
    def __init__(self, *, api_key, messages):
        self.api_key = api_key
        self.messages = messages


def _install_fake_anthropic(monkeypatch, output: str) -> _FakeMessages:
    msgs = _FakeMessages(output)
    fake = types.ModuleType("anthropic")
    fake.Anthropic = (  # type: ignore[attr-defined]
        lambda *, api_key: _FakeClient(api_key=api_key, messages=msgs)
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    # Keep the cold path hermetic: the fixtures are lint-clean by construction,
    # so stub luau-analyze to report no syntax errors (the tool may be absent).
    monkeypatch.setattr(code_transpiler, "_luau_syntax_check", lambda src: [])
    return msgs


class TestCacheHitProvenInvalidReTranspiles:

    def _seed_and_call(self, monkeypatch, tmp_path, *, cached_luau):
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_ENABLED", True)
        msgs = _install_fake_anthropic(monkeypatch, FIXED_BULLET)
        _system, prompt_hash = _select_prompt("legacy")
        key = _ai_cache_key(
            csharp_source="csharp",
            class_name="Bullet",
            script_type="ModuleScript",
            project_context="",
            prompt_hash=prompt_hash,
            model="claude-sonnet-4",
        )
        _save_cache(key, {"luau": cached_luau, "confidence": 0.9, "warnings": []})
        luau, _conf, _warnings = _ai_transpile(
            "csharp", "dummy-key", "claude-sonnet-4",
            class_name="Bullet", script_type="ModuleScript",
            project_context="", runtime_mode="legacy",
        )
        return luau, msgs

    def test_proven_invalid_cache_hit_triggers_re_transpile(
        self, monkeypatch, tmp_path,
    ):
        # The cached Luau has a PROVEN invalid -> the cache hit must be
        # rejected and the cold backend invoked (mirrors syntax-error path).
        luau, msgs = self._seed_and_call(
            monkeypatch, tmp_path, cached_luau=BROKEN_BULLET,
        )
        assert len(msgs.calls) >= 1, (
            "a proven-invalid cached Luau must trigger a re-transpile "
            "(the backend was never called)"
        )
        # The cold path returned the fixed output (clean).
        assert _proven(luau) == []

    def test_clean_cache_hit_is_served_without_backend(
        self, monkeypatch, tmp_path,
    ):
        # NON-VACUOUS contrast: a clean cached Luau is served straight from
        # cache -- the backend is NEVER called.
        luau, msgs = self._seed_and_call(
            monkeypatch, tmp_path, cached_luau=CLEAN,
        )
        assert msgs.calls == [], (
            "a clean cached Luau must be served from cache, not re-transpiled"
        )
        assert luau == CLEAN


# ---------------------------------------------------------------------------
# Post-repair contract-warning recompute (P1): the Roblox-call repair runs
# LAST, AFTER ``_verify_and_reprompt``. If the contract reprompt left a
# surviving ``contract-verifier`` tag for code the repair then FIXED, that
# stale warning must be recomputed away -- otherwise ``contract_pipeline``
# fails-close a module that was actually repaired.
# ---------------------------------------------------------------------------

class _SplitMessages:
    """``client.messages`` stub that answers the contract reprompt and the
    repair reprompt DIFFERENTLY, keyed on the message content.

    ``_verify_and_reprompt``'s message never says "DO NOT EXIST";
    ``_repair_invalid_roblox_calls``'s message always does. So:
      * the contract reprompt gets ``contract_reply`` (here: still broken,
        so an ``fc`` ``contract-verifier`` survivor tag is recorded), and
      * the repair reprompt gets ``repair_reply`` (here: the fixed bullet).
    This is the exact race the P1 finding describes: a stale contract
    survivor for a call the repair subsequently fixes.
    """

    def __init__(self, *, contract_reply: str, repair_reply: str) -> None:
        self._contract_reply = contract_reply
        self._repair_reply = repair_reply
        self.calls: list[str] = []

    def create(self, *, model, max_tokens, system, messages):
        msg = messages[0]["content"]
        self.calls.append(msg)
        reply = self._repair_reply if "DO NOT EXIST" in msg else self._contract_reply
        block = types.SimpleNamespace(text=reply)
        return types.SimpleNamespace(content=[block])


def _install_split_anthropic(
    monkeypatch, *, contract_reply: str, repair_reply: str,
) -> _SplitMessages:
    msgs = _SplitMessages(contract_reply=contract_reply, repair_reply=repair_reply)
    fake = types.ModuleType("anthropic")
    fake.Anthropic = (  # type: ignore[attr-defined]
        lambda *, api_key: _FakeClient(api_key=api_key, messages=msgs)
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setattr(code_transpiler, "_luau_syntax_check", lambda src: [])
    return msgs


class TestPostRepairContractRecompute:

    def test_stale_contract_survivor_cleared_after_repair_fix(
        self, monkeypatch, tmp_path,
    ):
        # First output = BROKEN_BULLET (an ``fc`` contract violation on the
        # hallucinated FindFirstChildOfType). The contract reprompt FAILS to
        # fix it (returns BROKEN again) -> a surviving ``contract-verifier
        # (rule fc...)`` tag is recorded. The repair reprompt THEN fixes the
        # call (returns FIXED_BULLET). Post-fix, the final Luau is clean, so
        # the recompute MUST drop the stale ``contract-verifier`` survivor.
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_ENABLED", False)
        _install_split_anthropic(
            monkeypatch, contract_reply=BROKEN_BULLET, repair_reply=FIXED_BULLET,
        )
        # The very first model output is BROKEN_BULLET too.
        monkeypatch.setattr(
            code_transpiler, "_strip_code_fences", lambda s: s,
        )

        # Seed the FIRST (pre-reprompt) output by mocking the initial create
        # to return BROKEN_BULLET: with cache disabled, _ai_transpile's first
        # client.messages.create call is the initial transpile, which our
        # split stub answers with contract_reply (no "DO NOT EXIST") = BROKEN.
        luau, _conf, warnings = _ai_transpile(
            "csharp", "dummy-key", "claude-sonnet-4",
            class_name="Bullet", script_type="ModuleScript",
            project_context="", runtime_mode="generic",
        )

        # Final Luau is repaired (clean).
        assert _proven(luau) == [], f"final Luau still has proven invalids: {luau}"
        # The stale fail-closed ``contract-verifier `` (space) survivor for the
        # repaired ``fc`` call must be GONE after the recompute.
        stale = [
            w for w in warnings
            if w.startswith("contract-verifier ")  # space = fail-closed survivor
        ]
        assert stale == [], (
            f"stale contract-verifier survivor not recomputed away: {stale}"
        )
        # The repair survivor warnings (roblox-call-survivor) are non-contract
        # and must be PRESERVED by the recompute -- here the fix succeeded so
        # there is none, but assert no survivor leaked either way.
        assert not any("roblox-call-survivor" in w for w in warnings), warnings

    def test_recompute_invoked_on_final_source_when_repair_changes_luau(
        self, monkeypatch, tmp_path,
    ):
        # Spy: when the repair changes the Luau in GENERIC mode, the recompute
        # must run on the FINAL (repaired) source, not the pre-repair source.
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_ENABLED", False)
        monkeypatch.setattr(code_transpiler, "_strip_code_fences", lambda s: s)
        _install_split_anthropic(
            monkeypatch, contract_reply=BROKEN_BULLET, repair_reply=FIXED_BULLET,
        )

        seen_sources: list[str] = []
        real_refresh = code_transpiler._refresh_contract_warnings

        def _spy(luau_source, cached_warnings, is_player_controller=False):
            seen_sources.append(luau_source)
            return real_refresh(
                luau_source, cached_warnings,
                is_player_controller=is_player_controller,
            )

        monkeypatch.setattr(code_transpiler, "_refresh_contract_warnings", _spy)

        _ai_transpile(
            "csharp", "dummy-key", "claude-sonnet-4",
            class_name="Bullet", script_type="ModuleScript",
            project_context="", runtime_mode="generic",
        )
        # The post-repair recompute ran on the FIXED (repaired) source.
        assert any(s.strip() == FIXED_BULLET.strip() for s in seen_sources), (
            "recompute was not invoked on the repaired source: "
            f"{seen_sources}"
        )

    def test_legacy_mode_skips_post_repair_recompute(
        self, monkeypatch, tmp_path,
    ):
        # CONTRAST: in legacy mode there are no contract warnings to refresh,
        # so the post-repair recompute must NOT run even when repair changes
        # the Luau.
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_ENABLED", False)
        monkeypatch.setattr(code_transpiler, "_strip_code_fences", lambda s: s)
        _install_split_anthropic(
            monkeypatch, contract_reply=BROKEN_BULLET, repair_reply=FIXED_BULLET,
        )

        seen: list[str] = []
        monkeypatch.setattr(
            code_transpiler, "_refresh_contract_warnings",
            lambda src, w, is_player_controller=False: (seen.append(src) or w),
        )

        _ai_transpile(
            "csharp", "dummy-key", "claude-sonnet-4",
            class_name="Bullet", script_type="ModuleScript",
            project_context="", runtime_mode="legacy",
        )
        assert seen == [], (
            "legacy mode must not invoke the contract-warning recompute"
        )
