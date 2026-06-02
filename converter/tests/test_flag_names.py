"""Tests for the canonical shared-flag name sanitizer (core.flag_names).

The sanitizer is the SINGLE source of truth so the Python source path
(scene_converter ItemType / itemName) and the emitted Luau runtime path
(transpiler prompt + coherence packs) produce byte-identical tokens for
ASCII. These tests pin the spec:

  - replace each run of [^A-Za-z0-9_] with a single "_",
  - no case change,
  - no-op on clean identifiers (SimpleFPS byte-identity),
  - skip (None) when empty / no ASCII alnum / "has"+stem > 64,
  - ASCII-explicit (a Unicode "café" → "caf_", NOT "café").
"""
from __future__ import annotations

import pytest

from core.flag_names import (
    _LUAU_FLAG_SANITIZE,
    canonical_flag_token,
    luau_flag_sanitize_expr,
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
    ],
)
def test_canonical_flag_token_sanitizes(name: str, expected: str) -> None:
    assert canonical_flag_token(name) == expected


def test_no_op_on_clean_identifiers_is_byte_identical() -> None:
    # SimpleFPS safety: existing literal GetAttribute("hasKey") readers
    # must keep matching, so the stem must be byte-identical.
    assert canonical_flag_token("Key") == "Key"
    assert canonical_flag_token("Rifle") == "Rifle"


@pytest.mark.parametrize("name", ["", "---", "!!!", " ", "@#$%"])
def test_skips_names_with_no_ascii_alnum(name: str) -> None:
    # Empty or all-symbol names sanitize to no original alphanumeric → skip.
    assert canonical_flag_token(name) is None


def test_skips_when_has_prefix_plus_stem_exceeds_64() -> None:
    # "has" (3) + stem must fit in 64 → stem budget is 61.
    stem_61 = "x" * 61
    assert canonical_flag_token(stem_61) == stem_61  # exactly fits
    stem_62 = "x" * 62
    assert canonical_flag_token(stem_62) is None      # 3 + 62 = 65 > 64


def test_non_ascii_only_name_skips() -> None:
    # A name with ONLY non-ASCII letters has no ASCII alnum → skip.
    assert canonical_flag_token("café") == "caf_"  # has ASCII "caf"
    assert canonical_flag_token("日本") is None       # no ASCII alnum


class TestPythonLuauParity:
    """The emitted Luau gsub mirrors the Python ASCII regex.

    Luau can't run in pytest, so parity is asserted at the string level:
    the one constant emits ``(<expr>:gsub("[^%w_]+", "_"))``, whose Lua
    pattern ``[^%w_]+`` is the byte-for-byte ASCII mirror of the Python
    ``re.sub(r"[^A-Za-z0-9_]+", "_", ...)`` that backs canonical_flag_token.
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
        # ASCII regex used by canonical_flag_token.
        emitted = luau_flag_sanitize_expr("x")
        assert '[^%w_]+' in emitted
        assert '"_"' in emitted
        # All sites derive from the ONE constant template.
        assert _LUAU_FLAG_SANITIZE.format(expr="x") == emitted
