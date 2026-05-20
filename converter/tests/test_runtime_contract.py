"""test_runtime_contract.py -- Verifier fixtures for the generic-runtime
contract (rules a-f from converter/docs/design/scene-runtime-contract.md
Piece 1).

Each rule has at least one rejection fixture (the verifier flags it) and
one acceptance fixture (the corresponding canonical good shape passes).
The reprompt path is exercised indirectly: every Violation row carries a
``message`` the reprompt feeds back, so we assert each rule's message is
specific enough that the AI can act on it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.runtime_contract import (  # noqa: E402
    LIFECYCLE_METHODS,
    UNITY_MESSAGE_CALLBACKS,
    verify_module,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rules(source: str) -> list[str]:
    """Return the rule letters of every violation, in source-line order."""
    return [v.rule for v in verify_module(source).violations]


def _assert_clean(source: str):
    result = verify_module(source)
    assert result.ok, (
        f"expected clean verify; got violations: "
        f"{[(v.rule, v.line, v.message) for v in result.violations]}"
    )


def _assert_rule(source: str, rule: str):
    result = verify_module(source)
    assert not result.ok, "expected at least one violation, got none"
    assert rule in [v.rule for v in result.violations], (
        f"expected rule {rule!r}; got "
        f"{[(v.rule, v.line) for v in result.violations]}"
    )


# A minimal compliant module the rule-specific tests can graft onto.
COMPLIANT = """\
local Class = {}
Class.__index = Class

function Class.new(config)
    local self = setmetatable({}, Class)
    self.config = config
    return self
end

function Class:Awake()
end

function Class:Start()
end

return Class
"""


# ---------------------------------------------------------------------------
# Sanity -- the compliant baseline passes.
# ---------------------------------------------------------------------------

class TestCompliantBaseline:

    def test_minimal_module_passes(self):
        _assert_clean(COMPLIANT)

    def test_compliant_with_require_passes(self):
        # ``require("@scene_runtime/...")`` is the contract-compliant
        # shape; verifier accepts it.
        src = (
            'local Other = require("@scene_runtime/Other")\n'
            + COMPLIANT
        )
        _assert_clean(src)


# ---------------------------------------------------------------------------
# Rule (a) -- forbidden top-level statements.
# ---------------------------------------------------------------------------

class TestRuleA:

    def test_top_level_call_rejected(self):
        # ``workspace:GetService(...)`` is a side-effecting top-level call.
        src = (
            'workspace:GetService("Players")\n'
            + COMPLIANT
        )
        _assert_rule(src, "a")

    def test_top_level_print_rejected(self):
        src = 'print("hi")\n' + COMPLIANT
        _assert_rule(src, "a")

    def test_top_level_assignment_to_existing_name_rejected(self):
        # ``X = ...`` without ``local`` is module-scope side-effect (writes
        # to globals).
        src = "X = 42\n" + COMPLIANT
        _assert_rule(src, "a")

    def test_module_scope_loop_rejected(self):
        # A top-level ``while`` loop is side-effecting.
        src = "while true do break end\n" + COMPLIANT
        _assert_rule(src, "a")


# ---------------------------------------------------------------------------
# Rule (b) -- top-level local with side-effecting RHS.
# ---------------------------------------------------------------------------

class TestRuleB:

    def test_local_with_connect_rejected(self):
        # The doc explicitly lists ``local c = ...:Connect(...)`` as
        # forbidden.
        src = (
            "local c = signal:Connect(function() end)\n"
            + COMPLIANT
        )
        _assert_rule(src, "b")

    def test_local_with_clone_rejected(self):
        src = "local t = template:Clone()\n" + COMPLIANT
        _assert_rule(src, "b")

    def test_local_with_function_call_rejected(self):
        src = "local svc = game:GetService(\"Players\")\n" + COMPLIANT
        _assert_rule(src, "b")

    def test_local_with_table_holding_side_effect_rejected(self):
        # The doc: ``local t = {c = ...:Connect(...)}`` is a violation
        # because the table constructor's value calls Connect.
        src = (
            "local t = { c = signal:Connect(function() end) }\n"
            + COMPLIANT
        )
        _assert_rule(src, "b")

    def test_local_literal_passes(self):
        src = "local SPEED = 12\n" + COMPLIANT
        _assert_clean(src)

    def test_local_string_passes(self):
        src = 'local NAME = "Foo"\n' + COMPLIANT
        _assert_clean(src)

    def test_local_function_literal_passes(self):
        src = "local f = function(x) return x end\n" + COMPLIANT
        _assert_clean(src)

    def test_local_table_literal_passes(self):
        src = "local DIRS = { 1, 2, 3 }\n" + COMPLIANT
        _assert_clean(src)

    def test_local_require_passes(self):
        src = 'local M = require("@scene_runtime/Other")\n' + COMPLIANT
        _assert_clean(src)


# ---------------------------------------------------------------------------
# Rule (c) -- lifecycle key assigned a non-function value.
# ---------------------------------------------------------------------------

class TestRuleC:

    def test_lifecycle_assigned_string_rejected(self):
        src = (
            'local Class = {}\n'
            'Class.Awake = "broken"\n'
            'return Class\n'
        )
        _assert_rule(src, "c")

    def test_lifecycle_assigned_number_rejected(self):
        src = (
            'local Class = {}\n'
            'Class.Update = 42\n'
            'return Class\n'
        )
        _assert_rule(src, "c")

    def test_lifecycle_assigned_function_literal_passes(self):
        src = (
            'local Class = {}\n'
            'Class.Awake = function(self) end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_lifecycle_defined_as_method_passes(self):
        # Already covered by COMPLIANT; explicit check for the canonical
        # ``function Class:Awake(self) ... end`` shape.
        src = (
            'local Class = {}\n'
            'function Class:Awake() end\n'
            'return Class\n'
        )
        _assert_clean(src)


# ---------------------------------------------------------------------------
# Rule (d) -- missing top-level return.
# ---------------------------------------------------------------------------

class TestRuleD:

    def test_no_return_rejected(self):
        src = (
            'local Class = {}\n'
            'function Class.new() end\n'
        )
        _assert_rule(src, "d")

    def test_return_present_passes(self):
        _assert_clean(COMPLIANT)


# ---------------------------------------------------------------------------
# Rule (e) -- constructor purity.
# ---------------------------------------------------------------------------

class TestRuleE:

    def test_new_reading_self_host_rejected(self):
        # The doc's exact rule-e case.
        src = (
            'local Class = {}\n'
            'function Class.new(config)\n'
            '    local self = setmetatable({}, Class)\n'
            '    self.host.invoke(self, "Tick", 1)\n'
            '    return self\n'
            'end\n'
            'return Class\n'
        )
        _assert_rule(src, "e")

    def test_new_calling_getcomponent_rejected(self):
        src = (
            'local Class = {}\n'
            'function Class.new(config)\n'
            '    local self = setmetatable({}, Class)\n'
            '    self:GetComponent("Rigidbody")\n'
            '    return self\n'
            'end\n'
            'return Class\n'
        )
        _assert_rule(src, "e")

    def test_new_pure_passes(self):
        # No host access; reads config only.
        src = (
            'local Class = {}\n'
            'function Class.new(config)\n'
            '    local self = setmetatable({}, Class)\n'
            '    self.speed = config.speed or 12\n'
            '    return self\n'
            'end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_self_host_inside_awake_passes(self):
        # Rule (e) is constructor-specific; the same access inside Awake
        # is canonical.
        src = (
            'local Class = {}\n'
            'function Class.new(config)\n'
            '    return setmetatable({}, Class)\n'
            'end\n'
            'function Class:Awake()\n'
            '    self.host:connect(self.gameObject.Touched, function() end)\n'
            'end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_new_as_table_literal_entry_rejected(self):
        # Rule (e) must also flag ``new = function(self, ...)`` shape --
        # which is what the table-literal class shape uses.
        src = (
            'local Class = {\n'
            '    new = function(config)\n'
            '        local self = {}\n'
            '        self.host.invoke(self, "Tick", 1)\n'
            '        return self\n'
            '    end,\n'
            '}\n'
            'return Class\n'
        )
        _assert_rule(src, "e")


# ---------------------------------------------------------------------------
# Rule (f) -- Unity message callbacks bound on the class table.
# All three syntactic shapes from the design doc.
# ---------------------------------------------------------------------------

class TestRuleF:

    def test_method_shape_rejected(self):
        # ``function Class:OnTriggerEnter(other)``
        src = (
            'local Class = {}\n'
            'function Class:OnTriggerEnter(other) end\n'
            'return Class\n'
        )
        _assert_rule(src, "f")

    def test_dot_assign_shape_rejected(self):
        # ``Class.OnTriggerEnter = function(other) ... end``
        src = (
            'local Class = {}\n'
            'Class.OnTriggerEnter = function(other) end\n'
            'return Class\n'
        )
        _assert_rule(src, "f")

    def test_table_literal_shape_rejected(self):
        # ``return { OnTriggerEnter = function(...) ... end }``
        src = (
            'local Class = {}\n'
            'return {\n'
            '    OnTriggerEnter = function(other) end,\n'
            '}\n'
        )
        _assert_rule(src, "f")

    def test_host_connect_in_awake_passes(self):
        # The canonical good shape from Piece 6.
        src = (
            'local Class = {}\n'
            'function Class.new(config)\n'
            '    return setmetatable({}, Class)\n'
            'end\n'
            'function Class:Awake()\n'
            '    self.host:connect(self.gameObject.Touched, function(other) end)\n'
            'end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_all_unity_callback_names_rejected(self):
        # Walk every name in the contract set. The verifier must reject
        # all twelve.
        for name in sorted(UNITY_MESSAGE_CALLBACKS):
            src = (
                'local Class = {}\n'
                f'function Class:{name}(other) end\n'
                'return Class\n'
            )
            _assert_rule(src, "f")


# ---------------------------------------------------------------------------
# Reprompt-path readability -- every violation message must be specific
# enough for the AI to act on. We don't run the AI here; we just assert
# the message references either the rule's keyword or a concrete shape.
# ---------------------------------------------------------------------------

class TestViolationMessages:

    def test_rule_a_message_mentions_module_scope(self):
        v = verify_module('print("hi")\n' + COMPLIANT).violations[0]
        assert "module-scope" in v.message or "module scope" in v.message

    def test_rule_b_message_mentions_local(self):
        v = verify_module(
            "local c = sig:Connect(function() end)\n" + COMPLIANT,
        ).violations[0]
        assert "local" in v.message.lower()

    def test_rule_c_message_names_the_method(self):
        src = 'local Class = {}\nClass.Awake = "x"\nreturn Class\n'
        v = verify_module(src).violations[0]
        assert "Awake" in v.message

    def test_rule_e_message_mentions_host_or_getcomponent(self):
        src = (
            'local Class = {}\n'
            'function Class.new()\n'
            '    return self.host\n'
            'end\n'
            'return Class\n'
        )
        v = verify_module(src).violations[0]
        assert "host" in v.message.lower() or "getcomponent" in v.message.lower()

    def test_rule_f_message_mentions_host_connect(self):
        src = (
            'local Class = {}\n'
            'function Class:OnTriggerEnter() end\n'
            'return Class\n'
        )
        v = verify_module(src).violations[0]
        assert "host:connect" in v.message or "host.connect" in v.message


# ---------------------------------------------------------------------------
# Comment / string blanking -- patterns inside strings or comments must
# NOT trigger violations.
# ---------------------------------------------------------------------------

class TestCommentStringIsolation:

    def test_unity_callback_name_inside_string_passes(self):
        # The literal ``"OnTriggerEnter"`` inside a comment / log line is
        # not a binding.
        src = (
            'local Class = {}\n'
            'function Class.new()\n'
            '    local msg = "OnTriggerEnter fired"\n'
            '    return setmetatable({}, Class)\n'
            'end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_unity_callback_name_inside_comment_passes(self):
        src = (
            'local Class = {}\n'
            '-- function Class:OnTriggerEnter(other) end  -- legacy comment\n'
            'function Class.new() return setmetatable({}, Class) end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_block_comment_isolated(self):
        # ``--[[ ... ]]`` containing forbidden patterns is not flagged.
        src = (
            'local Class = {}\n'
            '--[[\n'
            'function Class:OnTriggerEnter() end\n'
            ']]\n'
            'function Class.new() return setmetatable({}, Class) end\n'
            'return Class\n'
        )
        _assert_clean(src)


# ---------------------------------------------------------------------------
# Determinism -- repeated verification of the same source produces the
# same violation list. Tests for the contract verifier in the auto-mode
# fail-closed path will rely on this.
# ---------------------------------------------------------------------------

class TestDeterminism:

    def test_violation_list_is_deterministic(self):
        # A source with multiple violations -- different rules, different
        # lines. The sort order (line, rule) means repeated runs match.
        src = (
            'print("a")\n'                                  # rule a, line 1
            'local c = sig:Connect(function() end)\n'       # rule b, line 2
            'local Class = {}\n'
            'function Class:OnTriggerEnter() end\n'         # rule f, line 4
            'function Class.new()\n'
            '    self.host = nil\n'                         # not in new() args -- pure-ish, but let's add the host READ check
            '    return setmetatable({}, Class)\n'
            'end\n'
            'return Class\n'
        )
        r1 = verify_module(src).violations
        r2 = verify_module(src).violations
        assert r1 == r2
        # Source-line ordering preserved.
        lines = [v.line for v in r1]
        assert lines == sorted(lines)


# ---------------------------------------------------------------------------
# Lifecycle method set is complete -- the contract pins the eight Unity
# lifecycle hooks plus ``new``. Regression guard.
# ---------------------------------------------------------------------------

class TestIfElseifChainDepthTracking:
    """Regression: the depth tracker must handle multi-clause ``if /
    elseif / elseif / end`` correctly. The naïve ``then``+1, ``end``-1
    scheme that pre-PR3a-compliance-spike code used inflated depth past
    every chain (one ``then`` per clause, only one ``end``), which
    silently dropped every subsequent top-level statement in modules
    that had even a single such chain. The compliance spike found
    rule-(d) false positives on 9 of 65 modules (CharacterCollider,
    Mine, Pickup, Player, etc.) tracing back to this bug.

    The fix counts ``if`` (not ``then``) as the block opener; ``then`` /
    ``elseif`` / ``else`` are no-ops.
    """

    def test_two_clause_if_does_not_drop_return(self):
        # A module with a multi-clause if/elseif/end inside a method
        # body, followed by another method, followed by ``return``. The
        # old extractor stopped yielding statements after the if-chain
        # and missed the bottom ``return``.
        src = (
            'local Class = {}\n'
            'Class.__index = Class\n'
            'function Class.new(config)\n'
            '    return setmetatable({}, Class)\n'
            'end\n'
            'function Class:_layerOf(part)\n'
            '    if cond1 then\n'
            '        return 1\n'
            '    elseif cond2 then\n'
            '        return 2\n'
            '    elseif cond3 then\n'
            '        return 3\n'
            '    end\n'
            '    return -1\n'
            'end\n'
            'function Class:Other()\n'
            'end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_if_else_does_not_drop_return(self):
        # ``if ... else ... end`` is also a single block; same bug class.
        src = (
            'local Class = {}\n'
            'function Class.new(config)\n'
            '    return setmetatable({}, Class)\n'
            'end\n'
            'function Class:Update(dt)\n'
            '    if self.enabled then\n'
            '        self.x = self.x + dt\n'
            '    else\n'
            '        self.x = 0\n'
            '    end\n'
            'end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_nested_if_chains(self):
        # Two if-chains in different methods. Both need to balance.
        src = (
            'local Class = {}\n'
            'function Class.new(config) return setmetatable({}, Class) end\n'
            'function Class:A()\n'
            '    if x then\n'
            '        if y then\n'
            '            return 1\n'
            '        elseif z then\n'
            '            return 2\n'
            '        else\n'
            '            return 3\n'
            '        end\n'
            '    end\n'
            'end\n'
            'function Class:B()\n'
            '    if a then\n'
            '        return 1\n'
            '    elseif b then\n'
            '        return 2\n'
            '    end\n'
            'end\n'
            'return Class\n'
        )
        _assert_clean(src)

    def test_legacy_top_level_calls_still_rejected(self):
        # The fix can't accidentally let a side-effecting top-level call
        # through. ``print(...)`` at module scope still trips rule (a).
        src = (
            'print("loaded")\n'
            'local Class = {}\n'
            'function Class.new() return setmetatable({}, Class) end\n'
            'return Class\n'
        )
        _assert_rule(src, "a")


class TestLifecycleConstants:

    def test_all_lifecycle_methods_present(self):
        for name in (
            "Awake", "OnEnable", "Start",
            "Update", "FixedUpdate", "LateUpdate",
            "OnDisable", "OnDestroy",
            "new",
        ):
            assert name in LIFECYCLE_METHODS, (
                f"LIFECYCLE_METHODS lost {name!r} -- rule (c) would silently "
                f"skip non-function assignments to it."
            )

    def test_all_unity_message_callbacks_present(self):
        # The doc enumerates exactly twelve. Each one's class-table
        # binding must be rejected by rule (f).
        expected = {
            "OnTriggerEnter", "OnTriggerExit", "OnTriggerStay",
            "OnCollisionEnter", "OnCollisionExit", "OnCollisionStay",
            "OnMouseDown", "OnMouseUp", "OnMouseEnter",
            "OnMouseExit", "OnMouseOver", "OnMouseDrag",
        }
        assert UNITY_MESSAGE_CALLBACKS == expected
