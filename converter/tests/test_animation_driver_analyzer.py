"""Unit tests for ``animation_driver_analyzer.extract_animator_param_writes``
and the string-preserving source accessor it consumes.

Phase 1 (door run), D13: the parser is a TWO-STEP, receiver-bound match
(bind Animator-typed identifiers, then collect ``Set*("name")`` writes on
those receivers only). Input is comment-stripped, string-literal-preserving
C# source (from ``module_domain._load_cs_source_preserving_strings``).
"""

from __future__ import annotations

from converter.scene_runtime_topology.animation_driver_analyzer import (
    _ANIMATOR_PARAM_SETTERS,
    extract_animator_param_writes,
)
from converter.scene_runtime_topology.module_domain import (
    _load_cs_source_preserving_strings,
    _strip_cs_noise,
)


# ---------------------------------------------------------------------------
# extract_animator_param_writes
# ---------------------------------------------------------------------------

class TestExtractAnimatorParamWrites:

    def test_empty_input_is_empty(self) -> None:
        assert extract_animator_param_writes("") == frozenset()

    def test_comment_only_write_yields_empty(self) -> None:
        """A param that lives ONLY inside a comment must not match.

        The accessor blanks comments first, so by the time the parser
        sees the source the comment is gone. Simulate that by passing
        comment-stripped source.
        """
        raw = (
            "private Animator doorAnim { get { return null; } }\n"
            "void F(){ // doorAnim.SetBool(\"open\", true)\n }\n"
        )
        stripped = _strip_cs_noise(raw, preserve_strings=True)
        assert extract_animator_param_writes(stripped) == frozenset()

    def test_string_preserved_real_write(self) -> None:
        """A real ``doorAnim.SetBool("open", …)`` write → {"open"}."""
        src = (
            "private Animator doorAnim { get { return null; } }\n"
            'void F(){ doorAnim.SetBool("open", value); }\n'
        )
        assert extract_animator_param_writes(src) == frozenset({"open"})

    def test_multiline_write(self) -> None:
        """Whitespace/newline between receiver/./method/(/literal."""
        src = (
            "Animator a = thing.GetComponent<Animator>();\n"
            'a\n  .SetBool(\n    "open", v);\n'
        )
        assert extract_animator_param_writes(src) == frozenset({"open"})

    def test_wrong_receiver_non_animator_ignored(self) -> None:
        """``.SetBool`` on a non-Animator identifier is ignored (D13)."""
        src = (
            "bool someBool;\n"
            'void F(){ someBool.SetBool("dead", true); }\n'
        )
        assert extract_animator_param_writes(src) == frozenset()

    def test_binding_via_property(self) -> None:
        src = (
            "private Animator doorAnim { get { return null; } }\n"
            'void F(){ doorAnim.SetTrigger("fire"); }\n'
        )
        assert extract_animator_param_writes(src) == frozenset({"fire"})

    def test_binding_via_serialized_field(self) -> None:
        src = (
            "[SerializeField] Animator anim;\n"
            'void F(){ anim.SetFloat("speed", 1.0f); }\n'
        )
        assert extract_animator_param_writes(src) == frozenset({"speed"})

    def test_binding_via_local_var_getcomponent(self) -> None:
        src = (
            "void F(){\n"
            "  var a = GetComponent<Animator>();\n"
            '  a.SetInteger("phase", 2);\n'
            "}\n"
        )
        assert extract_animator_param_writes(src) == frozenset({"phase"})

    def test_where_clause_does_not_bind_keyword(self) -> None:
        """``where T : Animator`` must not bind ``class`` (codex PD-F1)."""
        src = (
            "public class Foo<T> where T : Animator {\n"
            '  void F(){ class.SetBool("x", true); }\n'
            "}\n"
        )
        assert extract_animator_param_writes(src) == frozenset()

    def test_method_param_animator_binds(self) -> None:
        """``void F(Animator a){a.SetBool("x")}`` → {"x"} (``a`` IS an
        Animator, an acceptable bind per D13)."""
        src = 'void F(Animator a){ a.SetBool("x", true); }\n'
        assert extract_animator_param_writes(src) == frozenset({"x"})

    def test_list_of_animator_no_usable_bind(self) -> None:
        """``List<Animator> xs`` binds no usable receiver."""
        src = (
            "List<Animator> xs;\n"
            'void F(){ xs.SetBool("x", true); }\n'
        )
        assert extract_animator_param_writes(src) == frozenset()

    def test_animator_array_no_usable_bind(self) -> None:
        """``Animator[] arr`` binds no usable receiver."""
        src = (
            "Animator[] arr;\n"
            'void F(){ arr.SetBool("x", true); }\n'
        )
        assert extract_animator_param_writes(src) == frozenset()

    def test_real_door_property_plus_setbool(self) -> None:
        """The REAL SimpleFPS Door shape (Door.cs:12-15, 37)."""
        src = (
            "public class Door : MonoBehaviour {\n"
            "  private Animator doorAnim { get { return "
            'transform.parent.Find("door").GetComponent<Animator>(); } }\n'
            "  void ToggleDoor(bool value){\n"
            '    doorAnim.SetBool("open", value);\n'
            "  }\n"
            "}\n"
        )
        assert extract_animator_param_writes(src) == frozenset({"open"})

    def test_param_by_hash_not_matched(self) -> None:
        """``SetBool(Animator.StringToHash("open"), …)`` is out of scope
        (FU1) — the first arg is not a bare string literal."""
        src = (
            "[SerializeField] Animator anim;\n"
            'void F(){ anim.SetBool(Animator.StringToHash("open"), true); }\n'
        )
        assert extract_animator_param_writes(src) == frozenset()

    def test_setters_constant_is_the_four_methods(self) -> None:
        assert _ANIMATOR_PARAM_SETTERS == frozenset(
            {"SetBool", "SetTrigger", "SetFloat", "SetInteger"}
        )


# ---------------------------------------------------------------------------
# _strip_cs_noise(preserve_strings=...) / _load_cs_source_preserving_strings
# ---------------------------------------------------------------------------

class TestStringPreservingAccessor:

    def test_preserve_strings_keeps_literal_blanks_comments(self) -> None:
        raw = (
            'string s = "keep me"; // drop this comment\n'
            '/* and this block */ string t = "also keep";\n'
        )
        out = _strip_cs_noise(raw, preserve_strings=True)
        assert '"keep me"' in out
        assert '"also keep"' in out
        # Comments blanked.
        assert "drop this comment" not in out
        assert "and this block" not in out
        # Length preserved (same-length scrub invariant).
        assert len(out) == len(raw)

    def test_preserve_strings_false_blanks_strings(self) -> None:
        """Back-compat: default still blanks string literals."""
        raw = 'string s = "secret";\n'
        out = _strip_cs_noise(raw, preserve_strings=False)
        assert "secret" not in out
        assert len(out) == len(raw)

    def test_preserve_strings_blanks_char_literal_comment(self) -> None:
        raw = "char c = 'x'; // tail\n"
        out = _strip_cs_noise(raw, preserve_strings=True)
        # char literal preserved, comment blanked
        assert "'x'" in out
        assert "tail" not in out

    def test_loader_returns_empty_on_none_guid_index(self) -> None:
        assert _load_cs_source_preserving_strings("some-guid", None) == ""
