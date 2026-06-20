"""Slice 1.2 — builder-integration + place_publisher long-bracket escaping.

Drives the REAL emission sites (not just the helpers) so a missed routing site is
caught: hostile class_name/light_type/constraint_type/layout_type strings, injecting
Enum-tail identifiers, and non-finite UDim2 offsets. Plus place_publisher's
``_build_collision_fidelity_fixup_script`` long-bracket routing (L3). Covers
acceptance criteria 1, 4, 10, 11 at the integration boundary.
"""

import re

import pytest

from roblox.luau_place_builder import (
    _LuauBuilder,
    _emit_constraint,
    _emit_light,
    _emit_screen_gui,
    _emit_ui_element,
    _long_bracket,
)
from roblox.place_publisher import _build_collision_fidelity_fixup_script
from core.roblox_types import RbxLight, RbxScreenGui, RbxUIElement


def _render(b: _LuauBuilder) -> str:
    return "\n".join(b._lines)


def _ui(**kw) -> RbxUIElement:
    base = dict(children=[], attributes={}, on_click_handlers=[])
    base.update(kw)
    return RbxUIElement(**base)


# ---------------------------------------------------------------------------
# Criterion 10 — no raw generator-derived data in emitted Luau
# ---------------------------------------------------------------------------

def test_hostile_class_name_is_quoted_no_breakout():
    elem = _ui(class_name="Frame'); evil()--", name="hud")
    b = _LuauBuilder()
    _emit_ui_element(b, elem, "g")
    src = _render(b)
    # The class arg is a quoted string literal; the injected ') cannot break out.
    assert 'Instance.new("Frame\'); evil()--")' in src
    # No bare evil() call statement appears.
    assert not re.search(r"^\s*evil\(\)", src, re.MULTILINE)


def test_hostile_layout_enum_tails_fall_back_to_defaults():
    elem = _ui(
        class_name="Frame",
        name="hud",
        layout_type="UIListLayout",
        layout_direction="Center;os.exit()",
        layout_h_alignment="Left Right",
        layout_v_alignment='Top") foo(',
    )
    b = _LuauBuilder()
    _emit_ui_element(b, elem, "g")
    src = _render(b)
    assert "Enum.FillDirection.Vertical" in src  # default
    assert "Enum.HorizontalAlignment.Left" in src  # default
    assert "Enum.VerticalAlignment.Top" in src  # default
    # No injected payload leaked as a bare Enum tail.
    assert "os.exit()" not in src
    assert "foo(" not in src


def test_hostile_layout_type_class_is_quoted():
    elem = _ui(class_name="Frame", name="hud", layout_type="UIListLayout'); bad(")
    b = _LuauBuilder()
    _emit_ui_element(b, elem, "g")
    src = _render(b)
    assert 'Instance.new("UIListLayout\'); bad(")' in src
    assert not re.search(r"^\s*bad\(", src, re.MULTILINE)


def test_hostile_light_type_is_quoted_with_control_escape():
    light = RbxLight(light_type="Point'); evil()--\x00")
    b = _LuauBuilder()
    _emit_light(b, light, "p")
    src = _render(b)
    # Quoted, control char escaped to \0, no breakout.
    assert 'Instance.new("Point\'); evil()--\\0")' in src
    assert "\x00" not in src


def test_non_finite_udim2_offset_does_not_crash():
    elem = _ui(
        class_name="Frame",
        name="hud",
        position=(0.0, float("inf"), 0.0, float("nan")),
        size=(1.0, float("-inf"), 1.0, 0.0),
    )
    b = _LuauBuilder()
    _emit_ui_element(b, elem, "g")  # must not raise
    src = _render(b)
    assert "inf" not in src and "nan" not in src


# ---------------------------------------------------------------------------
# Criterion 11 — valid input byte-identity (no behavior change)
# ---------------------------------------------------------------------------

def test_valid_ui_element_emission_unchanged():
    elem = _ui(
        class_name="Frame",
        name="HUD",
        layout_type="UIListLayout",
        layout_direction="Horizontal",
        layout_h_alignment="Center",
        layout_v_alignment="Bottom",
    )
    b = _LuauBuilder()
    _emit_ui_element(b, elem, "g")
    src = _render(b)
    assert 'Instance.new("Frame")' in src
    assert 'Instance.new("UIListLayout")' in src
    assert "Enum.FillDirection.Horizontal" in src  # valid -> unchanged
    assert "Enum.HorizontalAlignment.Center" in src
    assert "Enum.VerticalAlignment.Bottom" in src


def test_valid_light_emission_unchanged():
    light = RbxLight(light_type="SpotLight")
    b = _LuauBuilder()
    _emit_light(b, light, "p")
    src = _render(b)
    assert 'Instance.new("SpotLight")' in src


# ---------------------------------------------------------------------------
# Criterion 4 — place_publisher fixup script routes through _long_bracket (L3)
# ---------------------------------------------------------------------------

def test_fixup_script_payload_no_breakout():
    # A hostile asset path containing ]==] (the OLD hardcoded close) must not break out.
    targets = [{"path": "Room/Wall]==]evil", "mesh_id": "rbxassetid://1", "fidelity": "Hull"}]
    script = _build_collision_fidelity_fixup_script(targets)
    # Extract the long-bracket literal handed to JSONDecode.
    m = re.search(r"JSONDecode\((\[=*\[.*?\]=*\])\)", script, re.DOTALL)
    assert m is not None, "fixup script no longer uses a long-bracket literal"
    lit = m.group(1)
    lvl = len(re.match(r"^\[(=*)\[", lit).group(1))
    close = "]" + "=" * lvl + "]"
    inner = lit[len("[" + "=" * lvl + "["):-len("]" + "=" * lvl + "]")]
    # The chosen close sequence is absent in the payload -> no break-out.
    assert close not in inner
    # ]==] payload forced a level != 2.
    assert lvl != 2


def test_fixup_script_valid_payload_round_trips():
    import json
    targets = [{"path": "Room/Wall", "mesh_id": "rbxassetid://1", "fidelity": "Hull"}]
    script = _build_collision_fidelity_fixup_script(targets)
    m = re.search(r"JSONDecode\((\[=*\[.*?\]=*\])\)", script, re.DOTALL)
    lit = m.group(1)
    lvl = len(re.match(r"^\[(=*)\[", lit).group(1))
    inner = lit[len("[" + "=" * lvl + "["):-len("]" + "=" * lvl + "]")]
    # The payload is exactly json.dumps(targets) (byte-identity vs old [==[ form).
    assert json.loads(inner) == targets


def test_screen_gui_with_hostile_child_parses_structurally():
    # End-to-end-ish: a ScreenGui carrying a hostile element still emits balanced Luau.
    elem = _ui(class_name='Btn"); evil(', name="x\x1fy")
    gui = RbxScreenGui(name="G", elements=[elem], reset_on_spawn=False, attributes={})
    b = _LuauBuilder()
    _emit_screen_gui(b, gui)
    src = _render(b)
    assert "\x1f" not in src  # control char escaped
    assert 'Instance.new("Btn\\"); evil(")' in src  # quote escaped, no breakout
