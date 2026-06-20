"""Acceptance test (slice 1.4) — the load-bearing, proven-fail-pre-fix guarantee
that a Unity ``SendMessage``/``BroadcastMessage`` dispatch can never silently
collapse to a flag-only ``SetAttribute`` write.

This drives the REAL pipeline pieces end to end — NOT reimplemented logic:

  1. ``send_message_resolver.build_send_message_map`` over real C# fixtures (one
     per corpus receiver-kind + arg-shape, written to disk because the producer
     reads sources from disk), asserting it produces the right
     ``(kind, method, gameplay_arity)`` facts AND excludes the OverlapSphere
     radius case (``playersInRadius`` owns that, #201).

  2. ``runtime_contract.verify_module`` fed those exact facts. The PROVE-FAIL-
     PRE-FIX assertion: a FLAG-ONLY Pickup-style Luau (the broken shape that
     shipped the rifle bug — ``plr:SetAttribute("hasRifle", true)`` with NO
     ``host:sendMessage``) makes the ``sm`` rule FIRE. A CORRECT dispatched Luau
     (both the colon ``self.host:sendMessage(...)`` and the dotted
     ``self.host.sendMessage(...)`` host-surface forms) raises NO ``sm``
     violation. This is the deterministic net: the dropped dispatch is now caught
     and drives a reprompt / fail-closed instead of shipping the collapse.

The C# fixtures mirror the real SimpleFPS scripts' EXACT call shapes (Pickup.cs,
Player.cs, Machine.cs, Plane.cs, Explosive.cs, ObjectResetter.cs,
WaterHoseParticles.cs, Mine.cs/PlaneBullet.cs). The real ``test_projects`` files
are absent from this worktree, so faithful inline strings stand in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from converter.runtime_contract import verify_module
from converter.send_message_resolver import (
    BROADCAST,
    SEND,
    SendMessageDispatchFact,
    build_send_message_map,
)


@dataclass
class _FakeInfo:
    """Minimal ScriptInfo stand-in carrying just the ``path`` the producer reads."""

    path: Path | None


# --- Real-corpus C# fixtures (one per receiver-kind + arg-shape) -----------
# Each mirrors the EXACT call shape of the named SimpleFPS script.

# Pickup.cs — touch arg + 1 gameplay arg.
_PICKUP_CS = (
    "public class Pickup : MonoBehaviour {\n"
    "  public string itemName;\n"
    "  void OnTriggerEnter(Collider other) {\n"
    "    other.SendMessage(\"GetItem\", itemName);\n"
    "  }\n"
    "}\n"
)

# Explosive.cs — implicit-self (bare) 0-arg send.
_EXPLOSIVE_CS = (
    "public class Explosive : MonoBehaviour {\n"
    "  void Detonate() { SendMessage(\"Immobilize\"); }\n"
    "}\n"
)

# ObjectResetter.cs — implicit-self (bare) 0-arg send.
_OBJECT_RESETTER_CS = (
    "public class ObjectResetter : MonoBehaviour {\n"
    "  void Trigger() { SendMessage(\"Reset\"); }\n"
    "}\n"
)

# Plane.cs — find-call receiver, 1 gameplay arg.
_PLANE_CS = (
    "public class Plane : MonoBehaviour {\n"
    "  void Crash() {\n"
    "    FindAnyObjectByType<GameManager>().SendMessage(\"RestartGame\", 5);\n"
    "  }\n"
    "}\n"
)

# Machine.cs — indexed array/field receiver, gameplay arity 1 + trailing Options.
_MACHINE_CS = (
    "public class Machine : MonoBehaviour {\n"
    "  GameObject[] doors;\n"
    "  void Open(int number) {\n"
    "    doors[number].SendMessage(\"ToggleDoor\", true, "
    "SendMessageOptions.DontRequireReceiver);\n"
    "  }\n"
    "}\n"
)

# Player.cs:189 — member-of-param receiver, options-only -> gameplay arity 0.
_PLAYER_CS = (
    "public class Player : MonoBehaviour {\n"
    "  void Shoot(RaycastHit hit) {\n"
    "    hit.collider.SendMessage(\"TakeDamage\", "
    "SendMessageOptions.DontRequireReceiver);\n"
    "  }\n"
    "}\n"
)

# WaterHoseParticles.cs — non-player broadcast, 0 gameplay args.
_WATER_HOSE_CS = (
    "public class WaterHoseParticles : MonoBehaviour {\n"
    "  void OnParticleCollision(GameObject fire) {\n"
    "    fire.BroadcastMessage(\"Extinguish\");\n"
    "  }\n"
    "}\n"
)

# Mine.cs:25-29 / PlaneBullet.cs:31-35 — the OverlapSphere radius case that MUST
# be EXCLUDED (playersInRadius owns it, #201). Aliased local + typed foreach.
_MINE_CS = (
    "public class Mine : MonoBehaviour {\n"
    "  float damage;\n"
    "  void Explode() {\n"
    "    Collider[] cols = Physics.OverlapSphere(transform.position, 2);\n"
    "    foreach (Collider col in cols) {\n"
    "      if (col.tag == \"Player\")\n"
    "        col.SendMessage(\"TakeDamage\", damage);\n"
    "    }\n"
    "  }\n"
    "}\n"
)


def _write_corpus(root: Path) -> dict[str, Path]:
    """Write each fixture to a real .cs file (the producer reads from disk) and
    return a name -> path map."""
    files = {
        "Pickup": _PICKUP_CS,
        "Explosive": _EXPLOSIVE_CS,
        "ObjectResetter": _OBJECT_RESETTER_CS,
        "Plane": _PLANE_CS,
        "Machine": _MACHINE_CS,
        "Player": _PLAYER_CS,
        "WaterHoseParticles": _WATER_HOSE_CS,
        "Mine": _MINE_CS,
    }
    out: dict[str, Path] = {}
    for name, src in files.items():
        p = root / f"{name}.cs"
        p.write_text(src, encoding="utf-8")
        out[name] = p
    return out


# --- Part 1: build_send_message_map produces the right per-kind facts ------


def test_corpus_produces_expected_facts_per_kind_and_arg_shape(tmp_path: Path):
    """The REAL fact producer, over the real-shaped corpus, emits exactly the
    expected ``(kind, method, gameplay_arity)`` for each receiver-kind +
    arg-shape, AND excludes the OverlapSphere radius dispatch."""
    paths = _write_corpus(tmp_path)
    result = build_send_message_map([_FakeInfo(p) for p in paths.values()])

    def shape(name: str) -> tuple[tuple[str, str, int], ...]:
        facts = result.get(str(paths[name].resolve()), ())
        return tuple((f.kind, f.method, len(f.gameplay_args)) for f in facts)

    # touch-arg + 1 gameplay arg.
    assert shape("Pickup") == ((SEND, "GetItem", 1),)
    # implicit-self (bare) 0-arg.
    assert shape("Explosive") == ((SEND, "Immobilize", 0),)
    assert shape("ObjectResetter") == ((SEND, "Reset", 0),)
    # find-call receiver, 1 gameplay arg.
    assert shape("Plane") == ((SEND, "RestartGame", 1),)
    # indexed-array receiver, gameplay arity 1 (trailing Options stripped).
    assert shape("Machine") == ((SEND, "ToggleDoor", 1),)
    # options-only -> gameplay arity 0.
    assert shape("Player") == ((SEND, "TakeDamage", 0),)
    # non-player broadcast, 0 gameplay args.
    assert shape("WaterHoseParticles") == ((BROADCAST, "Extinguish", 0),)

    # OverlapSphere radius case EXCLUDED -> Mine has no fact -> absent from map.
    assert str(paths["Mine"].resolve()) not in result


def test_corpus_full_fact_detail_pickup_and_machine(tmp_path: Path):
    """Spot-check the full fact (method + gameplay_args text + kind), not just
    the keyed shape, on the flagship Pickup + the Options-stripping Machine."""
    paths = _write_corpus(tmp_path)
    result = build_send_message_map([_FakeInfo(p) for p in paths.values()])

    (pickup,) = result[str(paths["Pickup"].resolve())]
    assert (pickup.kind, pickup.method, pickup.gameplay_args) == (
        SEND, "GetItem", ("itemName",)
    )
    (machine,) = result[str(paths["Machine"].resolve())]
    # The trailing SendMessageOptions token is dropped; ``true`` remains.
    assert (machine.kind, machine.method, machine.gameplay_args) == (
        SEND, "ToggleDoor", ("true",)
    )


# --- Part 2: prove-fail-pre-fix via the REAL verify_module -----------------

# The BROKEN Pickup.luau shape that shipped the rifle bug: the dispatch dropped,
# only the canonical flag mirror written. This is the exact collapse the design's
# DIAGNOSIS.md captured.
_PICKUP_LUAU_FLAG_ONLY = """\
local Pickup = {}
Pickup.__index = Pickup

function Pickup.new()
	local self = setmetatable({}, Pickup)
	self.itemName = "Rifle"
	return self
end

function Pickup:onTouch(other)
	local plr = self.host:playerFromTouch(other)
	if plr then
		plr:SetAttribute("hasRifle", true)
	end
end

return Pickup
"""

# The CORRECT shape — colon host-surface dispatch, flag mirror kept as an
# ADDITION (not a replacement).
_PICKUP_LUAU_DISPATCH_COLON = """\
local Pickup = {}
Pickup.__index = Pickup

function Pickup.new()
	local self = setmetatable({}, Pickup)
	self.itemName = "Rifle"
	return self
end

function Pickup:onTouch(other)
	self.host:sendMessage(other, "GetItem", self.itemName)
	local plr = self.host:playerFromTouch(other)
	if plr then
		plr:SetAttribute("hasRifle", true)
	end
end

return Pickup
"""

# The CORRECT shape — DOTTED host-surface form (every sibling generic directive
# is dotted, so the AI plausibly emits this; the verifier must accept it too).
_PICKUP_LUAU_DISPATCH_DOTTED = """\
local Pickup = {}
Pickup.__index = Pickup

function Pickup.new()
	local self = setmetatable({}, Pickup)
	self.itemName = "Rifle"
	return self
end

function Pickup:onTouch(other)
	self.host.sendMessage(other, "GetItem", self.itemName)
	local plr = self.host:playerFromTouch(other)
	if plr then
		plr:SetAttribute("hasRifle", true)
	end
end

return Pickup
"""

# The Pickup C# fact, as the threaded per-module subset would carry it.
_PICKUP_FACT = SendMessageDispatchFact(
    source_path="/synthetic/Pickup.cs",
    method="GetItem",
    gameplay_args=("itemName",),
    kind=SEND,
)


def _sm_violations(luau: str, facts: tuple[SendMessageDispatchFact, ...]):
    """The ``sm`` violations the REAL verifier raises for ``luau`` given ``facts``."""
    result = verify_module(luau, send_message_facts=facts)
    return [v for v in result.violations if v.rule == "sm"]


def test_flag_only_pickup_fires_sm_rule_pre_fix():
    """PROVE-FAIL-PRE-FIX: the broken flag-only Pickup.luau (dispatch dropped)
    makes the REAL ``sm`` rule fire — the deterministic catch for the rifle bug."""
    sm = _sm_violations(_PICKUP_LUAU_FLAG_ONLY, (_PICKUP_FACT,))
    assert len(sm) == 1
    # The finding names the dropped method so the reprompt is actionable.
    assert "GetItem" in sm[0].message


def test_correct_dispatch_colon_form_no_sm_violation():
    """The corrected colon ``self.host:sendMessage(other, "GetItem", ...)`` shape
    (flag mirror kept additively) raises NO ``sm`` violation."""
    assert _sm_violations(_PICKUP_LUAU_DISPATCH_COLON, (_PICKUP_FACT,)) == []


def test_correct_dispatch_dotted_form_no_sm_violation():
    """The corrected DOTTED ``self.host.sendMessage(other, "GetItem", ...)``
    host-surface shape (``arg1 == recv``) also raises NO ``sm`` violation."""
    assert _sm_violations(_PICKUP_LUAU_DISPATCH_DOTTED, (_PICKUP_FACT,)) == []


def test_no_facts_means_no_sm_check():
    """With no threaded facts the ``sm`` rule does not run — the flag-only shape
    is NOT flagged (the check is gated on the upstream C# obligation)."""
    assert _sm_violations(_PICKUP_LUAU_FLAG_ONLY, ()) == []


# --- Part 3: end-to-end producer -> verifier, per kind ---------------------


def test_producer_facts_drive_verifier_per_kind(tmp_path: Path):
    """End to end on every corpus kind: the producer's REAL facts, fed to the
    REAL verifier, FIRE pre-fix and PASS once the matching
    ``host:sendMessage``/``broadcastMessage`` is present. This proves the
    guarantee holds per-receiver-kind + per-arg-shape, not just for Pickup.

    Per kind the pre-fix is checked TWO ways: (a) the dispatch WHOLLY absent (an
    empty module body -- the original guarantee, kept); and (b) a REALISTIC
    collapsed/wrong dispatch -- the kind's plausible failure mode: a flag-only
    ``SetAttribute`` mirror (the shipped rifle bug), a dropped-receiver bare
    ``self:sendMessage`` (the receiver-argument drop), a wrong-arity dispatch
    (gameplay arg lost), or a wrong-kind ``send`` standing in for a
    ``broadcast``. Both must fire the load-bearing ``sm`` rule -- (b) proves the
    catch on the actual collapse shapes, not merely on an empty body."""
    paths = _write_corpus(tmp_path)
    result = build_send_message_map([_FakeInfo(p) for p in paths.values()])

    # (script name, the matching emitted host call once correct).
    correct_call = {
        "Pickup": 'self.host:sendMessage(other, "GetItem", self.itemName)',
        "Explosive": 'self.host:sendMessage(self, "Immobilize")',
        "ObjectResetter": 'self.host:sendMessage(self, "Reset")',
        "Plane": 'self.host:sendMessage(gm, "RestartGame", 5)',
        "Machine": 'self.host:sendMessage(door, "ToggleDoor", true)',
        "Player": 'self.host:sendMessage(hitPart, "TakeDamage")',
        "WaterHoseParticles": 'self.host:broadcastMessage(fire, "Extinguish")',
    }

    # (script name, the REALISTIC collapsed/wrong shape that must STILL fire sm).
    # Each line is a plausible failure mode -- not an empty body:
    #   Pickup           -- flag-only SetAttribute mirror (the shipped rifle bug)
    #   Explosive        -- flag-only mirror, 0-arg send dropped
    #   ObjectResetter   -- flag-only mirror, 0-arg send dropped
    #   Plane            -- wrong arity: the gameplay arg ``5`` dropped (arity 1->0)
    #   Machine          -- dropped receiver: bare ``self:sendMessage`` self-dispatch
    #   Player           -- flag-only mirror, 0-arg send dropped
    #   WaterHoseParticles -- wrong kind: ``send`` emitted for a ``broadcast`` fact
    collapsed_pre_fix = {
        "Pickup": 'plr:SetAttribute("hasRifle", true)',
        "Explosive": 'self.gameObject:SetAttribute("immobilized", true)',
        "ObjectResetter": 'self.gameObject:SetAttribute("needsReset", true)',
        "Plane": 'self.host:sendMessage(gm, "RestartGame")',
        "Machine": 'self:sendMessage("ToggleDoor", true)',
        "Player": 'hitPart:SetAttribute("damaged", true)',
        "WaterHoseParticles": 'self.host:sendMessage(fire, "Extinguish")',
    }
    assert set(collapsed_pre_fix) == set(correct_call)  # every kind covered

    for name, call in correct_call.items():
        facts = result[str(paths[name].resolve())]
        method = call.split('"')[1]
        # Pre-fix (a): the dispatch WHOLLY absent (empty body) -> sm fires. Kept
        # from the original so the "dropped entirely" guarantee stays per-kind.
        empty = "function M:run() end\nreturn M\n"
        pre_empty = [v for v in verify_module(empty, send_message_facts=facts).violations
                     if v.rule == "sm"]
        assert len(pre_empty) == 1, f"{name}: expected sm violation on empty body"
        # Pre-fix (b): a REALISTIC collapsed/wrong dispatch -> the verifier STILL
        # fires the sm rule (proves the catch on the actual failure modes, not
        # merely on a wholly-empty body).
        broken = f"function M:run()\n\t{collapsed_pre_fix[name]}\nend\nreturn M\n"
        pre = [v for v in verify_module(broken, send_message_facts=facts).violations
               if v.rule == "sm"]
        assert len(pre) == 1, (
            f"{name}: expected sm violation on the collapsed/wrong shape"
        )
        # The finding names the dropped method so the reprompt is actionable.
        assert method in pre[0].message, f"{name}: sm message must name {method}"
        # Post-fix: the matching host call present -> no sm violation.
        fixed = f"function M:run()\n\t{call}\nend\nreturn M\n"
        post = [v for v in verify_module(fixed, send_message_facts=facts).violations
                if v.rule == "sm"]
        assert post == [], f"{name}: unexpected sm violation post-fix: {post}"
