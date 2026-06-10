"""test_player_verifier_reject.py -- Phase 4 paradigm-B player rejects.

The two lexically-decidable player rejects (rule ``p1`` -- a direct
``workspace.CurrentCamera.CFrame``/``CameraType`` write; rule ``p2`` -- a
direct ``Humanoid:Move(`` call) are NON-load-bearing: the host owns camera +
locomotion via ``self.host.player`` (paradigm C, which dominates by
last-writer ordering regardless). So a surviving player reject warns +
reprompts once, then fails OPEN -- it is tagged ``contract-verifier-player``
which NEVER matches ``_is_post_reprompt_warning`` and so NEVER promotes to a
project-level ``FailClosed``.

These tests drive the REAL functions (``verify_module``,
``_verify_and_reprompt``, ``_refresh_contract_warnings``,
``_is_post_reprompt_warning``, ``_is_contract_warning``) -- no surrogate --
and the fail-open invariants (AC5 / AC5-cache-hit) are mutation-proven:
they assert that the pre-fix bug shape (the generic ``contract-verifier ``
space tag) WOULD have promoted to fail-closed, so a regression that
re-emits a player reject under the space tag turns the test RED.

Coverage:
  AC2  -- p1 / p2 FIRE on a hand-broken player script (is_player=True),
          via ``verify_module`` directly and via ``_verify_and_reprompt``.
  AC3  -- PivotTo NOT rejected; a match inside a string/comment NOT rejected
          (the ``stripped`` scan); a non-player script gets neither check.
  AC5  -- a SURVIVING player reject is fail-OPEN: its ``-player`` tag does
          NOT match the REAL ``_is_post_reprompt_warning`` -> no FailClosed,
          while a rule-(a) survivor DOES (non-vacuous).
  AC5-cache-hit -- a surviving player reject is fail-OPEN on the REAL
          ``_refresh_contract_warnings`` cache-replay path too; mutation-
          proven against the space-tag bug.
  AC6-reject -- the rejects are gated on ``is_player_controller`` (the
          deterministic upstream identity), never an AI-output fingerprint.
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
    _format_contract_survivor_warning,
    _refresh_contract_warnings,
    _save_cache,
    _select_prompt,
    _verify_and_reprompt,
)
from converter.contract_pipeline import (  # noqa: E402
    _is_contract_warning,
    _is_post_reprompt_warning,
    transpile_with_contract,
)
from converter.runtime_contract import verify_module  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures -- hand-broken player scripts + the canonical clean shape.
# ---------------------------------------------------------------------------

# A player script that writes the host-owned camera directly (rule p1).
P1_CAMERA_WRITE = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Update()
    workspace.CurrentCamera.CFrame = self.aimCFrame
end
return Class
"""

# A player script that writes CameraType (the p1 sibling).
P1_CAMERA_TYPE = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Start()
    workspace.CurrentCamera.CameraType = Enum.CameraType.Scriptable
end
return Class
"""

# A player script that moves the Humanoid directly (rule p2).
P2_HUMANOID_MOVE = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Update()
    Humanoid:Move(self.dir)
end
return Class
"""

# A contract-clean player script -- no camera write, no Humanoid move.
CLEAN_PLAYER = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Update()
    self.host.player:applyRecoil(self.recoil)
end
return Class
"""

# A module that violates contract rule (a) -- top-level side-effecting call.
RULE_A_BROKEN = """\
print("loaded")
local Class = {}
function Class.new() return setmetatable({}, Class) end
return Class
"""


# ---------------------------------------------------------------------------
# AC2 -- the two rejects FIRE on a hand-broken player script.
# ---------------------------------------------------------------------------

class TestRejectsFireOnPlayerScript:

    def test_p1_camera_cframe_write_rejected(self):
        result = verify_module(P1_CAMERA_WRITE, is_player_controller=True)
        assert not result.ok
        rules = [v.rule for v in result.violations]
        assert "p1" in rules, f"expected p1, got {rules}"
        v = next(v for v in result.violations if v.rule == "p1")
        assert v.line == 4
        assert "CurrentCamera" in v.message

    def test_p1_camera_type_write_rejected(self):
        result = verify_module(P1_CAMERA_TYPE, is_player_controller=True)
        assert "p1" in [v.rule for v in result.violations]
        v = next(v for v in result.violations if v.rule == "p1")
        assert "CameraType" in v.message

    def test_p2_humanoid_move_rejected(self):
        result = verify_module(P2_HUMANOID_MOVE, is_player_controller=True)
        assert not result.ok
        rules = [v.rule for v in result.violations]
        assert "p2" in rules, f"expected p2, got {rules}"
        v = next(v for v in result.violations if v.rule == "p2")
        assert v.line == 4
        assert "Humanoid:Move" in v.message

    def test_p1_fires_through_verify_and_reprompt(self):
        # Drive the one-shot reprompt path: the backend returns the same
        # broken source, so the p1 reject SURVIVES and is surfaced.
        def reprompt(msg: str):
            return P1_CAMERA_WRITE

        out, warnings = _verify_and_reprompt(
            P1_CAMERA_WRITE, "csharp", "generic", reprompt,
            is_player_controller=True,
        )
        assert any("rule p1" in w for w in warnings), (
            f"p1 reject lost on the reprompt path: {warnings}"
        )

    def test_p2_fires_through_verify_and_reprompt(self):
        def reprompt(msg: str):
            return P2_HUMANOID_MOVE

        _out, warnings = _verify_and_reprompt(
            P2_HUMANOID_MOVE, "csharp", "generic", reprompt,
            is_player_controller=True,
        )
        assert any("rule p2" in w for w in warnings), (
            f"p2 reject lost on the reprompt path: {warnings}"
        )

    def test_reprompt_called_exactly_once_for_player_reject(self):
        # One-shot reprompt, matching the contract-rule path (no retries).
        calls: list[str] = []

        def reprompt(msg: str):
            calls.append(msg)
            return P1_CAMERA_WRITE

        _verify_and_reprompt(
            P1_CAMERA_WRITE, "csharp", "generic", reprompt,
            is_player_controller=True,
        )
        assert len(calls) == 1


# ---------------------------------------------------------------------------
# AC3 -- false-positive safety + non-player gating.
# ---------------------------------------------------------------------------

class TestRejectFalsePositiveSafety:

    def test_pivotto_is_not_rejected(self):
        # PivotTo serves yaw / translate / respawn and is not lexically
        # separable -- the design drops the PivotTo clause entirely.
        src = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Update()
    self.rig:PivotTo(self.targetCFrame)
end
return Class
"""
        result = verify_module(src, is_player_controller=True)
        assert "p1" not in [v.rule for v in result.violations]
        assert "p2" not in [v.rule for v in result.violations]

    def test_camera_write_inside_string_not_rejected(self):
        # The check scans ``stripped`` (string/comment-blanked), so a match
        # inside a string literal is not a false positive.
        src = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Log()
    print("workspace.CurrentCamera.CFrame = x")
end
return Class
"""
        assert verify_module(src, is_player_controller=True).ok

    def test_humanoid_move_inside_comment_not_rejected(self):
        src = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Update()
    -- legacy code used Humanoid:Move(dir) here; host owns it now
    self.host.player:applyRecoil(self.recoil)
end
return Class
"""
        assert verify_module(src, is_player_controller=True).ok

    def test_camera_equality_test_not_rejected(self):
        # ``==`` is a comparison, not a write -- the ``(?!=)`` lookahead.
        src = """\
local Class = {}
function Class.new() return setmetatable({}, Class) end
function Class:Update()
    if workspace.CurrentCamera.CFrame == self.last then return end
end
return Class
"""
        assert verify_module(src, is_player_controller=True).ok

    def test_non_player_script_gets_neither_check(self):
        # A turret/drone that legitimately writes its own camera is NOT a
        # player -> the p1/p2 rejects never run (is_player_controller=False,
        # the default).
        assert verify_module(P1_CAMERA_WRITE).ok
        assert verify_module(P2_HUMANOID_MOVE).ok
        # And the explicit-False form is identical to the default.
        assert verify_module(P1_CAMERA_WRITE, is_player_controller=False).ok
        assert verify_module(P2_HUMANOID_MOVE, is_player_controller=False).ok


# ---------------------------------------------------------------------------
# AC5 (cold fail-open) -- a SURVIVING player reject does NOT promote to
# fail-closed; a surviving rule-(a) violation DOES (non-vacuous contrast).
# ---------------------------------------------------------------------------

class TestColdFailOpen:

    def _promotion_failclosed_count(self, warnings: list[str]) -> int:
        # Replicate the REAL promotion filter at contract_pipeline.py:633-636
        # (``post_warnings = [w for w in warnings if _is_post_reprompt_warning(w)]``
        # -> one FailClosed(kind="verifier") when post_warnings is non-empty).
        post = [w for w in warnings if _is_post_reprompt_warning(w)]
        return 1 if post else 0

    def test_surviving_player_reject_is_fail_open(self):
        def reprompt(msg: str):
            return P1_CAMERA_WRITE  # reprompt did NOT fix it

        _out, warnings = _verify_and_reprompt(
            P1_CAMERA_WRITE, "csharp", "generic", reprompt,
            is_player_controller=True,
        )
        # The surviving reject carries the fail-OPEN ``-player`` tag.
        assert any(w.startswith("contract-verifier-player") for w in warnings)
        # Drive the REAL promotion predicate: a -player tag never matches
        # _is_post_reprompt_warning, so it never becomes FailClosed.
        assert not any(_is_post_reprompt_warning(w) for w in warnings), (
            f"player reject wrongly matched the fail-closed promotion: {warnings}"
        )
        assert self._promotion_failclosed_count(warnings) == 0, (
            "a surviving player reject must NOT promote to FailClosed"
        )

    def test_surviving_rule_a_violation_is_fail_closed(self):
        # NON-VACUOUS contrast: the SAME machinery promotes a real contract
        # (a)-(h) survivor to fail-closed.
        def reprompt(msg: str):
            return RULE_A_BROKEN  # still rule (a)

        _out, warnings = _verify_and_reprompt(
            RULE_A_BROKEN, "csharp", "generic", reprompt,
            is_player_controller=True,
        )
        assert any(_is_post_reprompt_warning(w) for w in warnings), (
            f"rule-(a) survivor must match the fail-closed promotion: {warnings}"
        )
        assert self._promotion_failclosed_count(warnings) == 1, (
            "a surviving rule-(a) violation MUST promote to FailClosed "
            "(otherwise the player fail-open test is vacuous)"
        )

    def test_fixed_player_reject_leaves_no_surviving_warning(self):
        # Player rules emit NO ``-pre`` tag; a reprompt that fixes p1 leaves
        # no surviving player warning at all (so the counter taxonomy never
        # false-counts it as rescued).
        def reprompt(msg: str):
            return CLEAN_PLAYER

        out, warnings = _verify_and_reprompt(
            P1_CAMERA_WRITE, "csharp", "generic", reprompt,
            is_player_controller=True,
        )
        assert out.strip() == CLEAN_PLAYER.strip()
        assert not any("contract-verifier-player" in w for w in warnings)
        assert not any("rule p1" in w for w in warnings), (
            f"a FIXED p1 left a stray player warning: {warnings}"
        )

    def test_player_survivor_excluded_from_contract_counters(self):
        # _is_contract_warning gates first_attempt_pass_count -- a -player
        # tag must NOT be counted as a contract warning (non-load-bearing).
        v_p1 = verify_module(
            P1_CAMERA_WRITE, is_player_controller=True,
        ).violations[0]
        player_tag = _format_contract_survivor_warning(v_p1)
        assert player_tag.startswith("contract-verifier-player")
        assert not _is_contract_warning(player_tag), (
            "player reject must be excluded from the contract counter taxonomy"
        )
        # Contrast: a real contract (a) survivor IS a contract warning.
        v_a = verify_module(RULE_A_BROKEN).violations[0]
        assert _is_contract_warning(_format_contract_survivor_warning(v_a))


# ---------------------------------------------------------------------------
# AC5-cache-hit (THE load-bearing one) -- fail-OPEN on the REAL cache-replay
# path, mutation-proven against the pre-fix space-tag bug.
# ---------------------------------------------------------------------------

class TestCacheReplayFailOpen:

    def test_surviving_player_reject_fails_open_on_cache_replay(self):
        # Drive the REAL _refresh_contract_warnings (the cache-hit warnings
        # refresh) with a cached player Luau that still trips p1.
        refreshed = _refresh_contract_warnings(
            P1_CAMERA_WRITE, [], is_player_controller=True,
        )
        # Re-emitted under the ``-player`` tag, NOT the generic space tag.
        assert any(
            w.startswith("contract-verifier-player") for w in refreshed
        ), f"cache replay lost the -player tag: {refreshed}"
        assert not any(
            w.startswith("contract-verifier ") for w in refreshed
        ), f"cache replay used the fail-CLOSED space tag: {refreshed}"
        # Drive the REAL promotion predicate -> no FailClosed on replay.
        assert not any(_is_post_reprompt_warning(w) for w in refreshed), (
            f"cache-replayed player reject matched fail-closed: {refreshed}"
        )

    def test_cache_replay_fail_open_is_non_vacuous(self):
        # MUTATION PROOF: had _refresh_contract_warnings re-emitted the p1
        # reject under the generic ``contract-verifier `` (space) tag -- the
        # pre-fix bug -- it WOULD have matched _is_post_reprompt_warning and
        # promoted to FailClosed on replay. This asserts the bug shape is
        # genuinely caught (so the fail-open assertion above is not vacuous).
        v_p1 = verify_module(
            P1_CAMERA_WRITE, is_player_controller=True,
        ).violations[0]
        pre_fix_bug_tag = (
            f"contract-verifier (rule {v_p1.rule}, line {v_p1.line}): "
            f"{v_p1.message}"
        )
        assert _is_post_reprompt_warning(pre_fix_bug_tag), (
            "the pre-fix space-tag shape MUST match the fail-closed promotion "
            "-- otherwise the fail-open test cannot distinguish fixed vs buggy"
        )
        # And the actual (fixed) tag must NOT.
        fixed_tag = _format_contract_survivor_warning(v_p1)
        assert not _is_post_reprompt_warning(fixed_tag)

    def test_cache_replay_p2_also_fails_open(self):
        refreshed = _refresh_contract_warnings(
            P2_HUMANOID_MOVE, [], is_player_controller=True,
        )
        assert any("rule p2" in w for w in refreshed)
        assert not any(_is_post_reprompt_warning(w) for w in refreshed)

    def test_cache_replay_non_player_unaffected(self):
        # is_player_controller=False on the cache path -> the p1/p2 rejects
        # never run, so a camera-writing non-player script replays clean.
        refreshed = _refresh_contract_warnings(
            P1_CAMERA_WRITE, [], is_player_controller=False,
        )
        assert refreshed == []

    def test_cache_replay_rule_a_still_fails_closed(self):
        # Non-vacuous: the cache path still promotes a real contract
        # survivor to fail-closed (only the player rejects fail open).
        refreshed = _refresh_contract_warnings(
            RULE_A_BROKEN, [], is_player_controller=True,
        )
        assert any(_is_post_reprompt_warning(w) for w in refreshed), (
            f"rule-(a) survivor must still fail closed on replay: {refreshed}"
        )


# ---------------------------------------------------------------------------
# AC5-cache-hit (DYNAMIC) -- drive the REAL ``_ai_transpile`` cache-HIT branch
# end-to-end, proving the backend actually THREADS ``is_player_controller``
# into ``_refresh_contract_warnings`` on replay. The tests above call the
# refresh helper directly; this closes the wiring gap (a green helper test does
# NOT prove the cache branch passes the flag) by seeding a real cache entry and
# taking the hit.
# ---------------------------------------------------------------------------

class TestCacheHitThreadsPlayerFlag:

    def _seed_and_hit(self, monkeypatch, tmp_path, *, source, is_player):
        """Seed a lint-clean cache entry for ``source`` under the EXACT key
        ``_ai_transpile`` computes, then call ``_ai_transpile`` so the cache
        HIT branch (the only path that re-tags via ``_refresh_contract_warnings``)
        runs. Returns the warnings the backend surfaced."""
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_DIR", str(tmp_path))
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_ENABLED", True)
        # A cache HIT short-circuits BEFORE the API client is constructed
        # (anthropic.Anthropic(...) at the post-cache path), so no network is
        # reachable: if the flag-threading regressed such that the hit branch
        # were skipped, _ai_transpile would fall through to the live API and
        # raise -- which the test surfaces as a hard error, not a silent pass.
        _system, prompt_hash = _select_prompt("generic")
        key = _ai_cache_key(
            csharp_source="csharp",
            class_name="Player",
            script_type="ModuleScript",
            project_context="",
            prompt_hash=prompt_hash,
            model="claude-sonnet-4",
        )
        _save_cache(key, {"luau": source, "confidence": 0.9, "warnings": []})
        _luau, _conf, warnings = _ai_transpile(
            "csharp", "dummy-key", "claude-sonnet-4",
            class_name="Player", script_type="ModuleScript",
            project_context="", runtime_mode="generic",
            is_player_controller=is_player,
        )
        return warnings

    def test_cache_hit_threads_player_flag_fail_open(
        self, monkeypatch, tmp_path,
    ):
        # The cached player Luau still trips p1. The cache-hit branch MUST run
        # the player reject (flag threaded) and re-tag it -player (fail OPEN).
        warnings = self._seed_and_hit(
            monkeypatch, tmp_path, source=P1_CAMERA_WRITE, is_player=True,
        )
        assert any(
            w.startswith("contract-verifier-player") for w in warnings
        ), f"cache-hit branch did not thread the player flag / re-tag: {warnings}"
        assert not any(_is_post_reprompt_warning(w) for w in warnings), (
            f"cache-hit player reject wrongly fail-CLOSED: {warnings}"
        )

    def test_cache_hit_without_flag_does_not_run_player_reject(
        self, monkeypatch, tmp_path,
    ):
        # NON-VACUOUS: the SAME cached camera-writing Luau, replayed as a
        # NON-player (flag False), runs only rules a-h -> no player reject.
        warnings = self._seed_and_hit(
            monkeypatch, tmp_path, source=P1_CAMERA_WRITE, is_player=False,
        )
        assert not any("rule p1" in w for w in warnings), (
            f"player reject ran on a non-player cache hit: {warnings}"
        )

    def test_cache_hit_rule_a_still_fails_closed(
        self, monkeypatch, tmp_path,
    ):
        # The cache-hit branch still promotes a real contract (a) survivor to
        # fail-closed -- only the player rejects fail open.
        warnings = self._seed_and_hit(
            monkeypatch, tmp_path, source=RULE_A_BROKEN, is_player=True,
        )
        assert any(_is_post_reprompt_warning(w) for w in warnings), (
            f"rule-(a) survivor must fail closed on the cache hit: {warnings}"
        )


# ---------------------------------------------------------------------------
# AC6-reject -- the rejects are gated on the deterministic identity, never an
# AI-output fingerprint.
# ---------------------------------------------------------------------------

class TestRejectGatedOnDeterministicIdentity:

    def test_identical_source_differs_only_by_identity_flag(self):
        # The ONLY thing that turns the p1/p2 rejects on is the
        # is_player_controller flag (threaded from the upstream
        # has_character_controller signal) -- NOT any property of the Luau
        # text itself. Same bytes, opposite verdicts.
        assert verify_module(P1_CAMERA_WRITE, is_player_controller=False).ok
        assert not verify_module(
            P1_CAMERA_WRITE, is_player_controller=True,
        ).ok
        assert verify_module(P2_HUMANOID_MOVE, is_player_controller=False).ok
        assert not verify_module(
            P2_HUMANOID_MOVE, is_player_controller=True,
        ).ok

    def test_contract_rules_unaffected_by_identity_flag(self):
        # The 8 contract rules (a)-(h) run regardless of the flag -- the
        # flag ADDS the player rejects, it does not gate the base contract.
        assert not verify_module(RULE_A_BROKEN, is_player_controller=False).ok
        assert not verify_module(RULE_A_BROKEN, is_player_controller=True).ok
        # A clean player script passes under both.
        assert verify_module(CLEAN_PLAYER, is_player_controller=False).ok
        assert verify_module(CLEAN_PLAYER, is_player_controller=True).ok


# ---------------------------------------------------------------------------
# AC2/AC3 (COLD-PATH END-TO-END) -- drive the REAL transpile_with_contract
# chain (``_player_controller_paths -> transpile_scripts -> _transpile_one ->
# _ai_transpile -> _verify_and_reprompt``) on a HAND-BROKEN player script and
# prove the p1/p2 reject ACTUALLY FIRES through every seam at once.
#
# The helper tests above call ``verify_module`` / ``_verify_and_reprompt`` /
# ``_refresh_contract_warnings`` DIRECTLY, and ``TestCacheHitThreadsPlayerFlag``
# only drives the cache-HIT branch. NONE of them prove the COLD (cache-miss)
# ``_ai_transpile -> _verify_and_reprompt`` thread: ``is_player_controller`` is
# captured at the ``_ai_transpile`` call site (code_transpiler.py:325) AND
# passed ONWARD into ``_verify_and_reprompt`` (:1852). A mutation dropping the
# flag at EITHER seam leaves this test RED (the reject stops firing), while the
# helper tests stay green -- which is exactly the coverage gap this closes.
#
# The mock is at the ``anthropic`` CLIENT boundary (not ``_ai_transpile``), so
# the REAL ``_ai_transpile`` body runs end-to-end: cache miss -> client call
# returns the hand-broken Luau -> lint clean -> ``_verify_and_reprompt`` runs
# the player reject -> one-shot reprompt (also broken) -> the reject SURVIVES
# and is surfaced under the fail-OPEN ``-player`` tag on the player's
# ``TranspiledScript.warnings``.
# ---------------------------------------------------------------------------

class _FakeAnthropicMessages:
    """Mimics ``client.messages`` -- ``create`` returns a canned response whose
    single text block is the broken player Luau on BOTH the initial transpile
    AND the one-shot reprompt, so the player reject SURVIVES (the reprompt
    'fails to fix' it, matching the surviving-reject fail-open path).

    Matching is by the requesting CLASS on the initial call (the user message
    opens with ``Class: `<name>``` -- code_transpiler.py:1789) and by the broken
    Luau's own SENTINEL on the reprompt call (the reprompt body embeds the
    PREVIOUS broken Luau -- code_transpiler.py:1535-1536 -- but NOT the class
    header). A non-matching call (e.g. HUD) returns a clean contract-compliant
    table so only the player carries a reject. Counts calls so the test can
    prove the cold path (not a cache hit) ran and the reprompt fired."""

    _CLEAN = "local M = {}\nreturn M\n"

    def __init__(self, player_class: str, player_luau: str, sentinel: str):
        self._player_class = player_class
        self._player_luau = player_luau
        self._sentinel = sentinel
        self.calls: list[str] = []

    def create(self, *, model, max_tokens, system, messages):
        user = messages[0]["content"]
        self.calls.append(user)
        # Initial call: keyed on the class header. Reprompt call: keyed on the
        # broken Luau sentinel echoed back in the reprompt body. Either way the
        # player keeps getting its broken Luau so the reject survives.
        if f"`{self._player_class}`" in user or self._sentinel in user:
            luau = self._player_luau
        else:
            luau = self._CLEAN
        block = types.SimpleNamespace(text=luau)
        return types.SimpleNamespace(content=[block])


class _FakeAnthropicClient:
    def __init__(self, *, api_key, messages):
        self.api_key = api_key
        self.messages = messages


def _install_fake_anthropic(
    monkeypatch, player_class: str, player_luau: str, sentinel: str,
):
    """Inject a fake ``anthropic`` module so the REAL ``_ai_transpile`` body
    runs but ``anthropic.Anthropic(...).messages.create`` is the canned mock.
    Returns the shared messages object (so the test can read ``.calls``)."""
    msgs = _FakeAnthropicMessages(player_class, player_luau, sentinel)
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = (  # type: ignore[attr-defined]
        lambda *, api_key: _FakeAnthropicClient(api_key=api_key, messages=msgs)
    )
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    # Keep the cold path hermetic: the hand-broken Luau is contract-broken but
    # lint-CLEAN, so stub luau-analyze to report no syntax errors (the real
    # tool may be absent in CI and would otherwise add noise warnings).
    monkeypatch.setattr(code_transpiler, "_luau_syntax_check", lambda src: [])
    return msgs


class _ScriptInfoStub:
    """Minimal ScriptInfo stand-in (mirrors test_player_directive.py)."""

    def __init__(self, path: Path, class_name: str) -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []
        self.suggested_type = "ModuleScript"
        self.base_class = "MonoBehaviour"


class TestColdPathRejectFiresEndToEnd:
    """The full ``transpile_with_contract`` cold path, mocked only at the
    anthropic client boundary, proves the player reject reaches through every
    seam (directive + flag + verifier)."""

    def _make_project(self, tmp_path: Path):
        """``Player`` is the lone CC-bearing controller; ``HUD`` is a plain
        component. ``_player_controller_paths`` resolves Player as the player
        (exactly-one CC join). The fake backend returns the broken player Luau
        for Player and a clean table for HUD."""
        proj = tmp_path / "unity"
        (proj / "Assets").mkdir(parents=True)
        player = proj / "Assets" / "Player.cs"
        player.write_text(
            "using UnityEngine;\n"
            "public class Player : MonoBehaviour { void Update() {} }\n"
        )
        hud = proj / "Assets" / "HUD.cs"
        hud.write_text(
            "using UnityEngine;\n"
            "public class HUD : MonoBehaviour { void Update() {} }\n"
        )
        infos = [
            _ScriptInfoStub(player, "Player"),
            _ScriptInfoStub(hud, "HUD"),
        ]
        scene_runtime = {
            "modules": {
                "guid-player": {
                    "stem": "Player", "class_name": "Player",
                    "runtime_bearing": True, "is_component_class": True,
                    "has_character_controller": True,
                },
                "guid-hud": {
                    "stem": "HUD", "class_name": "HUD",
                    "runtime_bearing": True, "is_component_class": True,
                    "has_character_controller": False,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        return proj, infos, scene_runtime

    def _run(self, monkeypatch, tmp_path, broken_player_luau: str,
             sentinel: str):
        """Disable the LLM cache (force the COLD path -- a cache hit would
        route through ``_refresh_contract_warnings``, not the
        ``_ai_transpile -> _verify_and_reprompt`` seam this test guards), then
        drive the real pipeline. ``sentinel`` is a distinctive substring of the
        broken Luau used to keep returning it on the reprompt call (whose body
        echoes the previous broken output but not the class header). Returns
        ``(result, player_script, hud_script, msgs)``."""
        monkeypatch.setattr(code_transpiler, "LLM_CACHE_ENABLED", False)
        msgs = _install_fake_anthropic(
            monkeypatch, "Player", broken_player_luau, sentinel,
        )
        proj, infos, scene_runtime = self._make_project(tmp_path)
        result = transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            api_key="dummy-key",
            use_ai=True,
        )
        player = next(
            s for s in result.transpilation.scripts
            if "Player" in s.output_filename
        )
        hud = next(
            s for s in result.transpilation.scripts
            if "HUD" in s.output_filename
        )
        return result, player, hud, msgs

    def test_p1_reject_fires_through_cold_transpile_with_contract(
        self, monkeypatch, tmp_path,
    ):
        result, player, hud, msgs = self._run(
            monkeypatch, tmp_path, P1_CAMERA_WRITE,
            sentinel="workspace.CurrentCamera.CFrame",
        )
        # The cold path was taken (initial + one-shot reprompt = 2 calls for
        # the surviving player reject; HUD adds its own initial call). A cache
        # hit would have made ZERO client calls.
        assert len(msgs.calls) >= 2, (
            f"cold path not exercised (calls={len(msgs.calls)})"
        )
        # The p1 reject ACTUALLY FIRED end-to-end: directive + flag + verifier
        # all threaded through to surface the surviving player warning.
        assert any(
            w.startswith("contract-verifier-player") and "rule p1" in w
            for w in player.warnings
        ), (
            "p1 reject did NOT fire through the cold transpile_with_contract "
            f"chain: {player.warnings}"
        )
        # Non-load-bearing: it fails OPEN (never promotes to fail-closed).
        assert not any(
            _is_post_reprompt_warning(w) for w in player.warnings
        ), f"cold-path player reject wrongly fail-CLOSED: {player.warnings}"
        # The non-player (HUD) gets NO player reject.
        assert not any(
            "rule p1" in w or "rule p2" in w
            or w.startswith("contract-verifier-player")
            for w in hud.warnings
        ), f"non-player HUD wrongly got a player reject: {hud.warnings}"

    def test_p2_reject_fires_through_cold_transpile_with_contract(
        self, monkeypatch, tmp_path,
    ):
        _result, player, hud, msgs = self._run(
            monkeypatch, tmp_path, P2_HUMANOID_MOVE,
            sentinel="Humanoid:Move(",
        )
        assert len(msgs.calls) >= 2
        assert any(
            w.startswith("contract-verifier-player") and "rule p2" in w
            for w in player.warnings
        ), (
            "p2 reject did NOT fire through the cold transpile_with_contract "
            f"chain: {player.warnings}"
        )
        assert not any(_is_post_reprompt_warning(w) for w in player.warnings)
        assert not any(
            "rule p2" in w or w.startswith("contract-verifier-player")
            for w in hud.warnings
        ), f"non-player HUD wrongly got a player reject: {hud.warnings}"
