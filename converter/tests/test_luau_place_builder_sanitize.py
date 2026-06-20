"""Slice 1.2 — Luau output-boundary escaping contract.

Covers acceptance criteria (design-phase1.md §5): 1 (byte-identity), 4 (long-bracket
no-breakout), 5 (_luau_str control totality), 6 (_f/_c3u8/UDim2-offset crash-freedom),
7 (idempotence), 10 (no raw generator-derived data in emitted Luau), 11 (valid-input
byte-identity for identifiers/strings). HOSTILE inputs are fed throughout.
"""

import math
import re

import pytest

from roblox.luau_place_builder import (
    _LUAU_INF_CLAMP,
    _c3u8,
    _f,
    _finite_num,
    _long_bracket,
    _luau_ident,
    _luau_str,
)


# ---------------------------------------------------------------------------
# Criterion 4 — _long_bracket no-breakout (structural)
# ---------------------------------------------------------------------------

def _bracket_level(out: str) -> int:
    m = re.match(r"^\[(=*)\[", out)
    assert m is not None, f"not a long-bracket literal: {out!r}"
    return len(m.group(1))


@pytest.mark.parametrize(
    "payload",
    ["]]", "]=]", "]==]", "]===]", "]]]====]]", "]" * 20 + "=" * 20 + "]" * 20],
)
def test_long_bracket_no_breakout(payload):
    out = _long_bracket(payload)
    lvl = _bracket_level(out)
    close = "]" + "=" * lvl + "]"
    # The chosen close sequence MUST be absent in the payload (no break-out).
    assert close not in payload
    # Output is exactly [eq[ payload ]eq] — structural, payload verbatim inside.
    assert out == f"[{'=' * lvl}[{payload}]{'=' * lvl}]"


def test_long_bracket_all_brackets_payload_terminates():
    # A finite string cannot contain ']' + '='*n + ']' for n > len(s); search ends.
    payload = "]" + "=" * 5 + "]"  # exactly a level-5 close
    out = _long_bracket(payload)
    lvl = _bracket_level(out)
    assert lvl != 5
    assert ("]" + "=" * lvl + "]") not in payload


def test_long_bracket_empty():
    assert _long_bracket("") == "[[]]"


def test_long_bracket_idempotence_stable_level():
    # Wrapping a payload twice is meaningful only as a stable structural op;
    # re-wrapping the already-wrapped literal still produces a no-breakout literal.
    once = _long_bracket("plain source")
    twice = _long_bracket(once)
    lvl = _bracket_level(twice)
    assert ("]" + "=" * lvl + "]") not in once


# ---------------------------------------------------------------------------
# Criterion 5 — _luau_str control-char totality + round-trip
# ---------------------------------------------------------------------------

def _luau_decode(lit: str) -> str:
    """Decode a double-quoted Luau string literal back to its value.

    Supports the escapes _luau_str emits: \\\\, \\", \\n, \\r, \\ddd (decimal).
    """
    assert lit.startswith('"') and lit.endswith('"'), lit
    body = lit[1:-1]
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1]
            if nxt == "\\":
                out.append("\\")
                i += 2
            elif nxt == '"':
                out.append('"')
                i += 2
            elif nxt.isdigit():
                j = i + 1
                digits = ""
                # Luau \ddd: up to 3 decimal digits.
                while j < len(body) and body[j].isdigit() and len(digits) < 3:
                    digits += body[j]
                    j += 1
                out.append(chr(int(digits)))
                i = j
            else:
                raise AssertionError(f"unexpected escape \\{nxt}")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@pytest.mark.parametrize(
    "s",
    [
        "a\x00b",
        "tab\tno",  # \t is < 0x20 -> escaped
        "x\ry\nz",
        "ctrl\x1fend",
        'has "quote"',
        "back\\slash",
        "del\x7fchar",
        "".join(chr(c) for c in range(0x20)) + "\x7f",
        'mix\x00"\\next',
    ],
)
def test_luau_str_control_round_trip(s):
    lit = _luau_str(s)
    # No raw control char survives in the literal output.
    for ch in lit:
        assert not (ord(ch) < 0x20 or ord(ch) == 0x7F), f"raw control in {lit!r}"
    # Round-trips byte-exact under Luau decoding.
    assert _luau_decode(lit) == s


@pytest.mark.parametrize("s", ["Door", "PointLight", "rbxassetid://123", "a b c", "name42"])
def test_luau_str_fast_path_byte_identity(s):
    # No special char -> f'"{s}"' UNCHANGED (criterion 1/11).
    assert _luau_str(s) == f'"{s}"'


def test_luau_str_empty():
    assert _luau_str("") == '""'


def test_luau_str_idempotence_of_decode():
    s = 'evil"); os.exit() --\x00'
    lit = _luau_str(s)
    assert _luau_decode(lit) == s
    # The only BARE quotes are the two delimiters; every inner " is escaped (\")
    # so none can terminate the literal early.
    body = lit[1:-1]
    i = 0
    bare_quotes = 0
    while i < len(body):
        if body[i] == "\\":
            i += 2
            continue
        if body[i] == '"':
            bare_quotes += 1
        i += 1
    assert bare_quotes == 0
    # The escaped inner quote is present.
    assert '\\"' in lit


# ---------------------------------------------------------------------------
# Criterion 6 — _f / _c3u8 / UDim2-offset crash-freedom on nan/inf
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("v", [float("inf"), float("-inf"), float("nan")])
def test_f_no_crash_on_non_finite(v):
    out = _f(v)  # must not raise OverflowError/ValueError
    # No literal inf/nan token.
    assert "inf" not in out and "nan" not in out
    # Parses as a finite Python float.
    assert math.isfinite(float(out))


@pytest.mark.parametrize(
    "v,expected",
    [(1.5, "1.5"), (3.0, "3"), (0.0, "0"), (-2.0, "-2"), (0.25, "0.25")],
)
def test_f_finite_byte_identity(v, expected):
    assert _f(v) == expected


def test_finite_num_passthrough_and_clamp():
    assert _finite_num(1.5) == 1.5
    assert _finite_num(0.0) == 0.0
    assert _finite_num(float("nan")) == 0.0
    assert _finite_num(float("inf")) == _LUAU_INF_CLAMP
    assert _finite_num(float("-inf")) == -_LUAU_INF_CLAMP
    # Clamp stays below float32 max so a float32 re-encode does not re-inf.
    assert _LUAU_INF_CLAMP < 3.4028235e38


def test_finite_num_idempotence():
    for v in (1.5, float("nan"), float("inf"), float("-inf")):
        assert _finite_num(_finite_num(v)) == _finite_num(v)


@pytest.mark.parametrize(
    "r,g,b",
    [
        (float("nan"), float("inf"), 0.5),
        (float("-inf"), float("nan"), 1.0),
        (float("inf"), float("inf"), float("inf")),
    ],
)
def test_c3u8_no_crash_on_non_finite(r, g, b):
    out = _c3u8(r, g, b)  # must not raise
    assert out.startswith("Color3.fromRGB(")
    nums = out[len("Color3.fromRGB("):-1].split(",")
    for n in nums:
        assert int(n) == float(n)  # all finite integers


def test_c3u8_finite_byte_identity():
    # 0-1 floats unchanged on the valid path.
    assert _c3u8(1.0, 0.0, 0.5) == "Color3.fromRGB(255,0,127)"


@pytest.mark.parametrize("v", [float("inf"), float("-inf"), float("nan")])
def test_udim2_offset_int_finitize_no_crash(v):
    # The N3 sites do int(_finite_num(offset)); that must not raise.
    from roblox.luau_place_builder import _finite_num as fn
    assert isinstance(int(fn(v)), int)


# ---------------------------------------------------------------------------
# Criterion 10/11 — _luau_ident injection -> fallback, valid -> unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "evil",
    [
        "Center;os.exit()",
        "Vertical') Instance.new('Script",
        "Left Right",
        'q"uote',
        "has.dot",
        "has-dash",
        "",
        "1startsdigit",
        "]==]",
    ],
)
def test_luau_ident_injection_falls_back(evil):
    assert _luau_ident(evil, "SAFE") == "SAFE"


@pytest.mark.parametrize("ok", ["Horizontal", "Vertical", "Left", "Top", "Overlay", "_x", "a1_b"])
def test_luau_ident_valid_unchanged(ok):
    assert _luau_ident(ok, "SAFE") == ok
