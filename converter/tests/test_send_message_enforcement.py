"""Tests for the SendMessage/BroadcastMessage reprompt enforcement (slice 1.3).

Covers ``runtime_contract.verify_module``'s fact-keyed ``sm`` rule:
  * a module MISSING a required dispatch produces an ``sm`` violation
    (which drives a reprompt in ``_verify_and_reprompt``);
  * a module that emits every required ``host:sendMessage`` / ``broadcastMessage``
    call produces NO ``sm`` violation;
  * PRESENCE per distinct ``(kind, method, gameplay_arity)`` key (each distinct
    key needs >=1 call; 2 same-key facts + 1 covering call do NOT fire; a key
    dropped entirely DOES fire);
  * DOTTED host-dispatch form (``self.host.sendMessage(...)``) covers, like colon;
  * RECEIVER best-effort (an aliased receiver still passes; a bare
    ``self:``/``self.`` self-dispatch is flagged, not credited);
  * comments / long-strings are not scanned (no false credit, no false fire);
  * arity + kind strict matching (wrong arity / send-vs-broadcast does not cover);
  * the threading param reaches the verify path (the facts a module's C# produced
    flow through ``_verify_and_reprompt`` into the ``sm`` reject + reprompt).

Fixtures are small inline Luau + ``SendMessageDispatchFact`` tuples; the resolver
(slice 1.2) is exercised once to confirm the producer->verifier handoff.
"""

from __future__ import annotations

from converter.runtime_contract import verify_module
from converter.send_message_resolver import (
    BROADCAST,
    SEND,
    SendMessageDispatchFact,
    build_send_message_map,
)
from converter import code_transpiler


def _fact(method: str, args: tuple[str, ...] = (), kind: str = SEND) -> SendMessageDispatchFact:
    return SendMessageDispatchFact(
        source_path="/synthetic/Test.cs",
        method=method,
        gameplay_args=args,
        kind=kind,
    )


def _sm_rules(source: str, facts: tuple[SendMessageDispatchFact, ...]) -> list[str]:
    """The methods named in ``sm`` violations for ``source`` against ``facts``."""
    result = verify_module(source, send_message_facts=facts)
    return [v.message for v in result.violations if v.rule == "sm"]


# A minimal contract-compliant module shell so the OTHER rules (a)-(h) stay
# silent and only ``sm`` is under test. The dispatch lines live inside a method.
_SHELL_HEAD = """\
local Pickup = {}
Pickup.__index = Pickup

function Pickup.new(config)
    return setmetatable({}, Pickup)
end

function Pickup:Awake()
"""
_SHELL_TAIL = """\
end

return Pickup
"""


def _module(body: str) -> str:
    return _SHELL_HEAD + body + "\n" + _SHELL_TAIL


# --- missing dispatch -> violation ----------------------------------------


def test_missing_dispatch_produces_sm_violation():
    # C# dispatched ``GetItem`` but the Luau collapsed it to a flag write.
    src = _module('    other:SetAttribute("hasRifle", true)')
    msgs = _sm_rules(src, (_fact("GetItem", ("itemName",)),))
    assert len(msgs) == 1
    assert "GetItem" in msgs[0]


def test_no_facts_means_no_sm_check():
    # No facts threaded (legacy / non-dispatching module) -> rule never runs.
    src = _module('    other:SetAttribute("hasRifle", true)')
    result = verify_module(src, send_message_facts=())
    assert all(v.rule != "sm" for v in result.violations)


# --- present dispatch -> no violation --------------------------------------


def test_present_dispatch_no_violation():
    src = _module('    self.host:sendMessage(other, "GetItem", self.itemName)')
    assert _sm_rules(src, (_fact("GetItem", ("itemName",)),)) == []


def test_zero_arg_dispatch_present():
    # Options-only C# arg -> gameplay arity 0; the Luau call has no gameplay arg.
    src = _module('    self.host:sendMessage(hit.collider, "TakeDamage")')
    assert _sm_rules(src, (_fact("TakeDamage", ()),)) == []


def test_broadcast_dispatch_present():
    src = _module('    self.host:broadcastMessage(self.gameObject, "Extinguish")')
    assert _sm_rules(src, (_fact("Extinguish", (), BROADCAST),)) == []


# --- dotted host-dispatch form ---------------------------------------------


def test_dotted_host_sendmessage_covers_fact():
    # The AI emits the DOTTED ``self.host.sendMessage(recv, name, ...)`` form
    # (every sibling generic-prompt directive is dotted) -- runtime-correct via
    # the host wrapper's ``arg1 == host`` discriminator, so it must cover the
    # fact. Arity for dotted matches colon: ``(recv, name, gameplay)`` -> 1.
    src = _module('    self.host.sendMessage(other, "GetItem", self.itemName)')
    assert _sm_rules(src, (_fact("GetItem", ("itemName",)),)) == []


def test_dotted_broadcast_covers_fact():
    src = _module('    self.host.broadcastMessage(self.gameObject, "Extinguish")')
    assert _sm_rules(src, (_fact("Extinguish", (), BROADCAST),)) == []


def test_dotted_bare_self_dispatch_is_flagged():
    # ``self.sendMessage(...)`` (dotted bare self) dropped the receiver, exactly
    # like the colon ``self:sendMessage`` -> NOT credited; fact uncovered.
    src = _module('    self.sendMessage("GetItem", name)')
    msgs = _sm_rules(src, (_fact("GetItem", ("itemName",)),))
    assert len(msgs) == 1
    assert "receiver" in msgs[0]


# --- presence per distinct key (NOT strict multiset count) -----------------


def test_two_facts_same_key_need_at_least_one_call():
    # Two ``ToggleDoor`` facts of the SAME key and ZERO emitted calls -> the key
    # is entirely dropped -> shortfall (the real bug a presence check still
    # catches: method goes 1->0).
    src = _module('    other:SetAttribute("doorOpen", true)')
    facts = (_fact("ToggleDoor", ("true",)), _fact("ToggleDoor", ("true",)))
    msgs = _sm_rules(src, facts)
    assert len(msgs) == 1
    assert "ToggleDoor" in msgs[0]


def test_two_facts_one_covering_call_does_not_fire():
    # PRESENCE, not count: the producer counted TWO same-key facts (e.g. a loop
    # the resolver saw as two dispatches) but a SINGLE emitted call legitimately
    # covers them -> must NOT fail-close.
    src = _module('    self.host:sendMessage(doors[i], "ToggleDoor", true)')
    facts = (_fact("ToggleDoor", ("true",)), _fact("ToggleDoor", ("true",)))
    assert _sm_rules(src, facts) == []


def test_distinct_keys_each_need_a_call():
    # Two facts of DIFFERENT keys (different arity) -> each distinct key still
    # needs >=1 covering call; only one is emitted -> the other fires.
    src = _module('    self.host:sendMessage(doors[1], "ToggleDoor", true)')
    facts = (_fact("ToggleDoor", ("true",)), _fact("ToggleDoor", ()))
    msgs = _sm_rules(src, facts)
    assert len(msgs) == 1
    assert "ToggleDoor" in msgs[0]


def test_two_distinct_keys_two_calls_ok():
    src = _module(
        '    self.host:sendMessage(doors[1], "ToggleDoor", true)\n'
        '    self.host:sendMessage(doors[2], "ToggleDoor")'
    )
    facts = (_fact("ToggleDoor", ("true",)), _fact("ToggleDoor", ()))
    assert _sm_rules(src, facts) == []


# --- comments / long-strings are not scanned -------------------------------


def test_sendmessage_in_comment_not_counted():
    # A ``host:sendMessage`` inside a ``--`` comment must NOT be credited as a
    # covering call (the scan runs over the comment-blanked source).
    src = _module(
        '    -- self.host:sendMessage(other, "GetItem", name)\n'
        '    other:SetAttribute("hasRifle", true)'
    )
    msgs = _sm_rules(src, (_fact("GetItem", ("itemName",)),))
    assert len(msgs) == 1
    assert "GetItem" in msgs[0]


def test_sendmessage_in_long_string_not_counted():
    src = _module(
        '    local doc = [[ self.host:sendMessage(other, "GetItem", name) ]]\n'
        '    other:SetAttribute("hasRifle", true)'
    )
    msgs = _sm_rules(src, (_fact("GetItem", ("itemName",)),))
    assert len(msgs) == 1
    assert "GetItem" in msgs[0]


# --- receiver best-effort --------------------------------------------------


def test_aliased_receiver_still_passes():
    # The C# receiver was ``other`` but the AI aliased it to ``tgt``; the verifier
    # does NOT strict-compare the operand, so this covers the fact.
    src = _module('    self.host:sendMessage(tgt, "GetItem", name)')
    assert _sm_rules(src, (_fact("GetItem", ("itemName",)),)) == []


def test_bare_self_dispatch_is_flagged():
    # ``self:sendMessage`` dropped the receiver argument -> NOT credited; the
    # fact is uncovered and the message hints at the dropped receiver.
    src = _module('    self:sendMessage("GetItem", name)')
    msgs = _sm_rules(src, (_fact("GetItem", ("itemName",)),))
    assert len(msgs) == 1
    assert "receiver" in msgs[0]


def test_host_receiver_alias_passes():
    # A locally-aliased host (``host:sendMessage`` rather than ``self.host:``)
    # still has a receiver first-arg -> covers the fact.
    src = _module('    host:sendMessage(other, "GetItem", name)')
    assert _sm_rules(src, (_fact("GetItem", ("itemName",)),)) == []


# --- kind + arity strictness ----------------------------------------------


def test_wrong_arity_does_not_cover():
    # Fact needs gameplay arity 1; the emitted call has arity 0 -> shortfall.
    src = _module('    self.host:sendMessage(other, "GetItem")')
    msgs = _sm_rules(src, (_fact("GetItem", ("itemName",)),))
    assert len(msgs) == 1


def test_send_does_not_satisfy_broadcast_fact():
    # A ``send`` call cannot cover a ``broadcast`` fact (kind is keyed).
    src = _module('    self.host:sendMessage(self.gameObject, "Extinguish")')
    msgs = _sm_rules(src, (_fact("Extinguish", (), BROADCAST),))
    assert len(msgs) == 1


def test_options_token_not_counted_as_gameplay_arg():
    # The Luau emits zero gameplay args (Options stripped); the fact has arity 0.
    src = _module('    self.host:sendMessage(hit.collider, "TakeDamage")')
    assert _sm_rules(src, (_fact("TakeDamage", ()),)) == []


# --- sm is fail-closed (load-bearing), not fail-open ----------------------


def test_sm_rule_is_not_fail_open():
    # The ``sm`` rule must keep the fail-closed ``contract-verifier `` (space)
    # tag, like rules (a)-(h) -- NOT a fail-open ``-player``/``-impulse`` tag.
    assert "sm" not in code_transpiler._FAIL_OPEN_RULES
    v = next(
        v for v in verify_module(
            _module('    other:SetAttribute("hasRifle", true)'),
            send_message_facts=(_fact("GetItem", ("itemName",)),),
        ).violations
        if v.rule == "sm"
    )
    warning = code_transpiler._format_contract_survivor_warning(v)
    # The space-tag form promotes to a hard fail-closed downstream.
    assert warning.startswith("contract-verifier (")


# --- threading smoke: producer output reaches the verify path -------------


def test_reprompt_loop_drives_sm_reject(tmp_path):
    """End-to-end of the threading: a C# script's facts (slice 1.2 producer)
    flow into ``_verify_and_reprompt`` and surface an ``sm`` reprompt + a
    surviving fail-closed warning when the Luau drops the dispatch."""
    cs = tmp_path / "Pickup.cs"
    cs.write_text(
        "public class Pickup : MonoBehaviour {\n"
        "  void OnTriggerEnter(Collider other) {\n"
        '    other.SendMessage("GetItem", itemName);\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    class _Info:
        path = cs

    sm_map = build_send_message_map([_Info()])
    facts = sm_map[str(cs.resolve())]
    assert [(f.kind, f.method, len(f.gameplay_args)) for f in facts] == [
        (SEND, "GetItem", 1)
    ]

    # The AI dropped the dispatch (flag-only collapse). The reprompt closure
    # returns the SAME broken output (simulating an AI that fails to fix it):
    # the surviving violation must carry the fail-closed contract-verifier tag.
    broken = (
        "local Pickup = {}\n"
        "function Pickup.new(c) return setmetatable({}, Pickup) end\n"
        "function Pickup:Awake()\n"
        '    local plr = game:GetService("Players").LocalPlayer\n'
        '    if plr then plr:SetAttribute("hasRifle", true) end\n'
        "end\n"
        "return Pickup\n"
    )

    seen: list[str] = []

    def _reprompt(user_msg: str) -> str:
        seen.append(user_msg)
        return broken  # AI fails to add the dispatch

    out_luau, warnings = code_transpiler._verify_and_reprompt(
        broken,
        cs.read_text(encoding="utf-8"),
        "generic",
        _reprompt,
        send_message_facts=facts,
    )
    # A reprompt was attempted (the sm violation drove it).
    assert seen, "expected the sm shortfall to drive a reprompt"
    assert "GetItem" in seen[0]
    # And the surviving shortfall is a fail-closed contract-verifier warning.
    assert any(
        w.startswith("contract-verifier (") and "GetItem" in w for w in warnings
    ), warnings


def test_reprompt_loop_passes_when_dispatch_present(tmp_path):
    """The mirror case: a Luau that DOES emit the dispatch produces no
    sm-driven reprompt (the initial verify is clean for sm)."""
    cs = tmp_path / "Pickup.cs"
    cs.write_text(
        "public class Pickup : MonoBehaviour {\n"
        "  void OnTriggerEnter(Collider other) {\n"
        '    other.SendMessage("GetItem", itemName);\n'
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    class _Info:
        path = cs

    facts = build_send_message_map([_Info()])[str(cs.resolve())]

    good = (
        "local Pickup = {}\n"
        "function Pickup.new(c) return setmetatable({}, Pickup) end\n"
        "function Pickup:Awake()\n"
        '    self.host:sendMessage(other, "GetItem", self.itemName)\n'
        "end\n"
        "return Pickup\n"
    )

    called: list[str] = []

    def _reprompt(user_msg: str) -> str:
        called.append(user_msg)
        return good

    out_luau, warnings = code_transpiler._verify_and_reprompt(
        good,
        cs.read_text(encoding="utf-8"),
        "generic",
        _reprompt,
        send_message_facts=facts,
    )
    # No sm-driven warning survives (the dispatch is present).
    assert not any("GetItem" in w for w in warnings), warnings
