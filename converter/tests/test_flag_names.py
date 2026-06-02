"""Tests for the canonical shared-flag name sanitizer (core.flag_names).

Sanitization happens at the RUNTIME ``"has" .. name`` concat (the emitted Luau
gsub). ``sanitize_flag_stem`` is the Python REFERENCE MIRROR of that gsub; it
is NOT applied to itemName/ItemType gameplay payloads. These tests pin the
spec:

  - replace each run of [^A-Za-z0-9_] with a single "_",
  - no case change,
  - no-op on clean identifiers (SimpleFPS byte-identity),
  - ASCII-explicit (a Unicode "café" → "caf_", NOT "café"),
  - pure mirror: no skip/None — the runtime gsub can't skip, so the funnel
    gate (not this function) is the backstop for overlong names.
"""
from __future__ import annotations

import re

import pytest

from core.flag_names import (
    _LUAU_FLAG_SANITIZE,
    luau_flag_sanitize_expr,
    sanitize_flag_stem,
)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Red Key", "Red_Key"),       # space → _
        ("a-b c", "a_b_c"),           # hyphen + space, each run → one _
        ("Key", "Key"),               # no-op on clean identifier
        ("Rifle", "Rifle"),           # no-op on clean identifier
        ("Red  Key", "Red_Key"),      # contiguous run collapses to ONE _
        ("café", "caf_"),             # ASCII-explicit: non-ASCII é → _
        ("Item123", "Item123"),       # digits preserved
        ("a_b", "a_b"),               # underscore preserved
        ("+++", "_"),                 # all-symbol run → single _ (degenerate)
        ("", ""),                     # empty → empty (pure, no skip)
    ],
)
def test_sanitize_flag_stem(name: str, expected: str) -> None:
    assert sanitize_flag_stem(name) == expected


def test_no_op_on_clean_identifiers_is_byte_identical() -> None:
    # SimpleFPS safety: existing literal GetAttribute("hasKey") readers
    # must keep matching, so the stem must be byte-identical.
    assert sanitize_flag_stem("Key") == "Key"
    assert sanitize_flag_stem("Rifle") == "Rifle"


class TestPythonLuauParity:
    """The emitted Luau gsub mirrors the Python ASCII regex byte-for-byte.

    Luau can't run in pytest, so parity is asserted at the string level plus
    by exercising the Python reference (``sanitize_flag_stem``) which uses the
    SAME ASCII charset (``[^A-Za-z0-9_]+``) the Lua pattern (``[^%w_]+``)
    mirrors.
    """

    def test_one_constant_emits_paren_wrapped_gsub(self) -> None:
        assert luau_flag_sanitize_expr("itemName") == (
            '(itemName:gsub("[^%w_]+", "_"))'
        )
        assert luau_flag_sanitize_expr("name") == (
            '(name:gsub("[^%w_]+", "_"))'
        )

    def test_emitted_lua_pattern_mirrors_python_charset(self) -> None:
        # The Lua complement class is [^%w_]; %w is alphanumeric, so the
        # class excludes exactly [A-Za-z0-9_] — identical to the Python
        # ASCII regex backing sanitize_flag_stem.
        emitted = luau_flag_sanitize_expr("x")
        assert '[^%w_]+' in emitted
        assert '"_"' in emitted
        # All sites derive from the ONE constant template.
        assert _LUAU_FLAG_SANITIZE.format(expr="x") == emitted

    @pytest.mark.parametrize(
        "name", ["Red Key", "a-b c", "café", "Key", "Big!!!Gun", "+++"]
    )
    def test_python_reference_matches_ascii_gsub_semantics(self, name: str) -> None:
        # The Lua gsub("[^%w_]+","_") on ASCII == this exact Python sub.
        assert sanitize_flag_stem(name) == re.sub(r"[^A-Za-z0-9_]+", "_", name)
