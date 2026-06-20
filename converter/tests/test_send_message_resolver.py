"""Tests for the C# SendMessage/BroadcastMessage fact producer (slice 1.2).

Covers the shared classifier's normalization (arity + SendMessageOptions strip),
each real-corpus receiver shape, broadcast vs send kind, the code-position guard,
bare-self dispatch, and the ALIASED OverlapSphere-foreach exclusion (keyed on the
semantic ``var cols = OverlapSphere(); foreach col in cols`` shape, not inline).
Fixtures are inline C# strings except one mirroring Mine.cs's aliased shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from converter.send_message_resolver import (
    BROADCAST,
    SEND,
    SendMessageDispatchFact,
    build_send_message_map,
    classify_dispatch,
    _resolve_script,
)


def _facts(source: str) -> tuple[SendMessageDispatchFact, ...]:
    """Resolve facts for an inline C# source under a synthetic path key."""
    return _resolve_script(source, "/synthetic/Test.cs")


def _shapes(source: str) -> list[tuple[str, str, tuple[str, ...]]]:
    """(kind, method, gameplay_args) for each emitted fact, in order."""
    return [(f.kind, f.method, f.gameplay_args) for f in _facts(source)]


# --- shared classifier: normalization -------------------------------------


def test_classify_send_simple_one_arg():
    assert classify_dispatch("SendMessage", '"GetItem", itemName') == (
        SEND, "GetItem", ("itemName",)
    )


def test_classify_zero_gameplay_args():
    assert classify_dispatch("SendMessage", '"Reset"') == (SEND, "Reset", ())


def test_classify_two_gameplay_args():
    assert classify_dispatch("SendMessage", '"Hit", dmg, dir') == (
        SEND, "Hit", ("dmg", "dir")
    )


def test_classify_strips_trailing_options_only():
    # ("TakeDamage", DontRequireReceiver) -> gameplay arity 0
    assert classify_dispatch(
        "SendMessage", '"TakeDamage", SendMessageOptions.DontRequireReceiver'
    ) == (SEND, "TakeDamage", ())


def test_classify_strips_options_mixed_with_gameplay_arg():
    # ("ToggleDoor", true, DontRequireReceiver) -> gameplay arity 1
    assert classify_dispatch(
        "SendMessage", '"ToggleDoor", true, SendMessageOptions.DontRequireReceiver'
    ) == (SEND, "ToggleDoor", ("true",))


def test_classify_broadcast_kind():
    assert classify_dispatch("BroadcastMessage", '"Extinguish"') == (
        BROADCAST, "Extinguish", ()
    )


def test_classify_unknown_call_name_is_none():
    assert classify_dispatch("DoStuff", '"X"') is None


def test_classify_dynamic_method_name_is_none():
    # A non-literal first argument (a variable method name) is not statically keyable.
    assert classify_dispatch("SendMessage", "methodName, arg") is None


def test_classify_empty_args_is_none():
    assert classify_dispatch("SendMessage", "") is None


def test_classify_does_not_split_inside_generic_arg():
    # A generic call argument keeps its internal comma (one gameplay arg).
    assert classify_dispatch(
        "SendMessage", '"Use", GetThing<A, B>()'
    ) == (SEND, "Use", ("GetThing<A, B>()",))


def test_classify_does_not_split_inside_nested_call():
    assert classify_dispatch(
        "SendMessage", '"Use", Make(a, b), tail'
    ) == (SEND, "Use", ("Make(a, b)", "tail"))


def test_classify_does_not_split_on_comma_in_string_arg():
    assert classify_dispatch(
        "SendMessage", '"Say", "a, b"'
    ) == (SEND, "Say", ('"a, b"',))


# --- receiver shapes (the 6 corpus shapes) --------------------------------


def test_receiver_param():
    # other.SendMessage("GetItem", itemName) — Pickup/SpawnPoint shape.
    src = "void F(Collider other){ other.SendMessage(\"GetItem\", itemName); }"
    assert _shapes(src) == [(SEND, "GetItem", ("itemName",))]


def test_receiver_member_of_param_options_only():
    # hit.collider.SendMessage("TakeDamage", DontRequireReceiver) — Player.cs:189.
    src = (
        "void F(){ hit.collider.SendMessage(\"TakeDamage\", "
        "SendMessageOptions.DontRequireReceiver); }"
    )
    assert _shapes(src) == [(SEND, "TakeDamage", ())]


def test_receiver_member_of_param_with_gameplay_arg():
    # other.collider.SendMessage("TakeDamage", damage) — TurretBullet.cs:31.
    src = "void F(){ other.collider.SendMessage(\"TakeDamage\", damage); }"
    assert _shapes(src) == [(SEND, "TakeDamage", ("damage",))]


def test_receiver_indexed_array_with_options():
    # doors[number].SendMessage("ToggleDoor", true, Options) — Machine.cs:47.
    src = (
        "void F(){ doors[number].SendMessage(\"ToggleDoor\", true, "
        "SendMessageOptions.DontRequireReceiver); }"
    )
    assert _shapes(src) == [(SEND, "ToggleDoor", ("true",))]


def test_receiver_expression_find_call():
    # FindAnyObjectByType<GameManager>().SendMessage("RestartGame", 5) — Plane.cs:17.
    src = (
        "void F(){ FindAnyObjectByType<GameManager>()"
        ".SendMessage(\"RestartGame\", 5); }"
    )
    assert _shapes(src) == [(SEND, "RestartGame", ("5",))]


def test_bare_self_send():
    # SendMessage("Reset") — implicit-this, ObjectResetter/Explosive shape.
    src = "void F(){ SendMessage(\"Reset\"); }"
    assert _shapes(src) == [(SEND, "Reset", ())]


def test_bare_self_broadcast():
    # BroadcastMessage("Extinguish") — implicit-this, ParticleSystemDestroyer shape.
    src = "void F(){ BroadcastMessage(\"Extinguish\"); }"
    assert _shapes(src) == [(BROADCAST, "Extinguish", ())]


# --- code-position guard ---------------------------------------------------


def test_dispatch_in_line_comment_no_fact():
    src = "void F(){ // other.SendMessage(\"GetItem\", itemName);\n int x = 1; }"
    assert _facts(src) == ()


def test_dispatch_in_block_comment_no_fact():
    src = "void F(){ /* a.SendMessage(\"X\", y); */ int x = 1; }"
    assert _facts(src) == ()


def test_dispatch_in_string_literal_no_fact():
    src = "void F(){ string s = \"a.SendMessage(\\\"X\\\")\"; }"
    assert _facts(src) == ()


def test_real_dispatch_not_suppressed_by_unrelated_comment():
    src = (
        "void F(){\n"
        "  // dispatch the item\n"
        "  other.SendMessage(\"GetItem\", itemName);\n"
        "}"
    )
    assert _shapes(src) == [(SEND, "GetItem", ("itemName",))]


# --- OverlapSphere exclusion (ALIASED — the real corpus shape) -------------

# Mirrors Mine.cs:25-29 / PlaneBullet.cs:31-35: aliased local, typed foreach.
_MINE_ALIASED = """
public class Mine : MonoBehaviour {
    private void Explode() {
        Collider[] cols = Physics.OverlapSphere(transform.position, 2);
        foreach (Collider col in cols) {
            if (col.tag == "Player")
                col.SendMessage("TakeDamage", damage);
        }
    }
}
"""


def test_overlap_sphere_aliased_foreach_excluded():
    # The aliased radius dispatch is owned by playersInRadius (#201) -> NO fact.
    assert _facts(_MINE_ALIASED) == ()


def test_overlap_sphere_inline_foreach_excluded():
    # Same semantic shape, inline collection (no alias hop) -> also excluded.
    src = (
        "void F(){ foreach (Collider col in Physics.OverlapSphere(p, 2)) {"
        " col.SendMessage(\"TakeDamage\", damage); } }"
    )
    assert _facts(src) == ()


def test_overlap_sphere_var_foreach_excluded():
    # ``var col`` binding form of the aliased shape -> excluded.
    src = (
        "void F(){ var cols = Physics.OverlapSphere(p, 2);"
        " foreach (var col in cols) { col.SendMessage(\"TakeDamage\", damage); } }"
    )
    assert _facts(src) == ()


def test_non_overlap_foreach_receiver_still_emits():
    # A foreach over a NON-OverlapSphere collection is a normal dispatch -> fact.
    src = (
        "void F(){ foreach (var t in targets) {"
        " t.SendMessage(\"Ping\", 1); } }"
    )
    assert _shapes(src) == [(SEND, "Ping", ("1",))]


def test_dispatch_outside_overlap_foreach_body_still_emits():
    # A same-named receiver OUTSIDE the OverlapSphere loop body is not excluded.
    src = (
        "void F(){\n"
        "  var cols = Physics.OverlapSphere(p, 2);\n"
        "  foreach (var col in cols) { col.SendMessage(\"TakeDamage\", d); }\n"
        "  other.SendMessage(\"GetItem\", name);\n"
        "}"
    )
    assert _shapes(src) == [(SEND, "GetItem", ("name",))]


# --- scope/position-aware exclusion (finding 1.2 fixes) --------------------


def test_overlap_loop_and_unrelated_direct_dispatch_both_kept():
    # (a) An OverlapSphere damage loop AND a SEPARATE direct dispatch whose
    # receiver is NOT the foreach var -> the loop is excluded, the direct
    # dispatch IS emitted (the file-global exclusion must not suppress it).
    src = (
        "void F(){\n"
        "  Collider[] cols = Physics.OverlapSphere(p, 2);\n"
        "  foreach (Collider col in cols) { col.SendMessage(\"TakeDamage\", d); }\n"
        "  manager.SendMessage(\"Foo\", arg);\n"
        "}"
    )
    assert _shapes(src) == [(SEND, "Foo", ("arg",))]


def test_unrelated_overlap_in_other_method_does_not_suppress_real_dispatch():
    # (b) An unrelated ``cols = OverlapSphere()`` lives in one method; a DIFFERENT
    # method has its own ``cols`` (a non-OverlapSphere collection) iterated by a
    # foreach. The exclusion must be per-scope: the second method's dispatch is
    # NOT suppressed by the first method's same-named OverlapSphere local.
    src = (
        "class C {\n"
        "  void A() {\n"
        "    var cols = Physics.OverlapSphere(p, 2);\n"
        "    foreach (var col in cols) { col.SendMessage(\"TakeDamage\", d); }\n"
        "  }\n"
        "  void B() {\n"
        "    var cols = targets;\n"
        "    foreach (var col in cols) { col.SendMessage(\"Ping\", 1); }\n"
        "  }\n"
        "}"
    )
    # A's loop excluded (real OverlapSphere); B's loop kept (cols is targets).
    assert _shapes(src) == [(SEND, "Ping", ("1",))]


def test_overlap_in_sibling_block_does_not_suppress_later_foreach():
    # An OverlapSphere ``cols`` declared in an already-CLOSED sibling block must
    # not reach a later foreach over a different ``cols`` in an enclosing scope.
    src = (
        "void F(){\n"
        "  { var cols = Physics.OverlapSphere(p, 2); }\n"
        "  var cols = targets;\n"
        "  foreach (var col in cols) { col.SendMessage(\"Ping\", 1); }\n"
        "}"
    )
    assert _shapes(src) == [(SEND, "Ping", ("1",))]


def test_rebind_to_non_overlap_nearer_foreach_keeps_dispatch():
    # The NEAREST-PRECEDING in-scope binding wins: a re-assignment to a
    # non-OverlapSphere value after an earlier OverlapSphere decl -> KEEP.
    src = (
        "void F(){\n"
        "  var cols = Physics.OverlapSphere(p, 2);\n"
        "  cols = targets;\n"
        "  foreach (var col in cols) { col.SendMessage(\"Ping\", 1); }\n"
        "}"
    )
    assert _shapes(src) == [(SEND, "Ping", ("1",))]


def test_corpus_aliased_shape_still_excluded_with_neighbors():
    # (c) Confirm the real corpus shape is STILL excluded when it is the
    # nearest-preceding in-scope binding (no neighbor masks it).
    src = (
        "void F(){\n"
        "  var cols = Physics.OverlapSphere(transform.position, 2);\n"
        "  foreach (var col in cols) { col.SendMessage(\"TakeDamage\", d); }\n"
        "}"
    )
    assert _facts(src) == ()


def test_member_field_assignment_not_treated_as_bare_local_binding():
    # A FIELD assignment ``this.cols = Physics.OverlapSphere(...)`` is a different
    # lvalue from the bare local ``cols`` the foreach iterates: it must NOT be
    # taken as the collection's binding. The foreach here is over a bare local
    # ``cols`` with NO in-scope bare binding, so no OverlapSphere link is
    # confirmed -> the dispatch is KEPT (not falsely suppressed).
    src = (
        "void F(){\n"
        "  this.cols = Physics.OverlapSphere(p, 2);\n"
        "  foreach (var col in cols) { col.SendMessage(\"TakeDamage\", d); }\n"
        "}"
    )
    assert _shapes(src) == [(SEND, "TakeDamage", ("d",))]


def test_member_field_assignment_does_not_mask_real_bare_binding():
    # An UNRELATED field assignment ``obj.cols = ...`` must not shadow the REAL
    # nearest-preceding BARE-local OverlapSphere binding of ``cols``; the loop is
    # still correctly excluded.
    src = (
        "void F(){\n"
        "  Collider[] cols = Physics.OverlapSphere(p, 2);\n"
        "  obj.cols = somethingElse;\n"
        "  foreach (var col in cols) { col.SendMessage(\"TakeDamage\", d); }\n"
        "}"
    )
    assert _facts(src) == ()


# --- build_send_message_map (path keying + presence) ----------------------


def test_build_map_keys_by_canonical_path_and_omits_empty(tmp_path: Path):
    a = tmp_path / "Pickup.cs"
    a.write_text("void F(Collider other){ other.SendMessage(\"GetItem\", n); }")
    b = tmp_path / "Inert.cs"
    b.write_text("void F(){ int x = 1; }")
    c = tmp_path / "Mine.cs"
    c.write_text(_MINE_ALIASED)

    infos = [_FakeInfo(a), _FakeInfo(b), _FakeInfo(c)]
    result = build_send_message_map(infos)

    key_a = str(a.resolve())
    assert set(result) == {key_a}  # Inert (no dispatch) + Mine (excluded) omitted
    (fact,) = result[key_a]
    assert (fact.kind, fact.method, fact.gameplay_args, fact.source_path) == (
        SEND, "GetItem", ("n",), key_a
    )


def test_build_map_skips_info_with_no_path():
    assert build_send_message_map([_FakeInfo(None)]) == {}


def test_fact_is_frozen():
    f = SendMessageDispatchFact("/p.cs", "M", (), SEND)
    import pytest

    with pytest.raises(Exception):
        f.method = "X"  # type: ignore[misc]


@dataclass
class _FakeInfo:
    """Minimal ScriptInfo stand-in carrying just the ``path`` the producer reads."""

    path: Path | None
