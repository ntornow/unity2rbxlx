"""
test_rbxlx_writer_sanitize.py -- XML/float output-boundary escaping for the
rbxlx writer (Phase 1, Slice 1.1).

Covers acceptance criteria 1 (byte-identity on valid input), 2 (XML control-char
totality), 3 (XML float finiteness), 7 (idempotence), 8 (round-trip XML
validity), 9 (no new Any). Feeds HOSTILE inputs (C0 controls, U+FFFE/FFFF, a
lone surrogate, nan/inf/-inf) into the F5-F16 float emitters specifically, not
just _add_float.
"""

import math
import sys
import xml.dom.minidom
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import (
    RbxCFrame,
    RbxLightingConfig,
    RbxParticleEmitter,
    RbxPart,
    RbxPlace,
    RbxScreenGui,
    RbxUIElement,
)
from roblox.rbxlx_writer import (
    _XML_INF_CLAMP,
    _add_cframe,
    _add_color3,
    _add_content,
    _add_float,
    _add_protected_string,
    _add_string,
    _add_vector3,
    _finite_val,
    _make_item,
    _make_part,
    _make_particle_emitter,
    _make_service,
    _make_ui_element,
    _xml_text,
    write_rbxlx,
)


# Every code point _xml_text must strip: C0 controls (U+0000-U+001F except
# 09/0A/0D), the always-illegal scalars U+FFFE/U+FFFF, and lone surrogates.
_STRIPPED_CODEPOINTS = (
    [c for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)]
    + [0xFFFE, 0xFFFF, 0xD800, 0xDC00, 0xDFFF]
)


# ---------------------------------------------------------------------------
# _xml_text unit behavior
# ---------------------------------------------------------------------------

class TestXmlText:
    def test_valid_input_is_byte_identical(self):
        # Criterion 1: valid text returns the SAME object, unchanged.
        for s in ("Door", "rbxassetid://12345", "", "héllo \t\n\r world",
                  "a<b>&c\"d'e", "emoji 😀 and 中文", "tab\tand\nnewline\rok"):
            out = _xml_text(s)
            assert out == s
            assert out is s, f"valid input should be returned unchanged (no copy): {s!r}"

    def test_does_not_touch_xml_metachars(self):
        # _xml_text must NOT entity-escape; ElementTree does that.
        s = "<tag> & \"quote\" 'apos'"
        assert _xml_text(s) == s

    def test_strips_every_illegal_codepoint(self):
        for cp in _STRIPPED_CODEPOINTS:
            ch = chr(cp)
            out = _xml_text(f"a{ch}b")
            assert out == "ab", f"U+{cp:04X} should be stripped, got {out!r}"
            assert ch not in out

    def test_keeps_tab_lf_cr(self):
        for cp in (0x09, 0x0A, 0x0D):
            ch = chr(cp)
            assert _xml_text(f"x{ch}y") == f"x{ch}y"

    def test_keeps_high_valid_ranges(self):
        # U+E000 (private use), U+FFFD (replacement char), astral plane.
        for ch in ("", "�", "\U0001F600", "\U0010FFFF", "퟿"):
            assert _xml_text(f"a{ch}b") == f"a{ch}b"

    def test_idempotent(self):
        # Criterion 7.
        for s in ("a\x00b\x01c", "￾￿", "Door", "\ud800lone",
                  "mix\x07ed\ttext￿"):
            once = _xml_text(s)
            assert _xml_text(once) == once

    def test_empty_string(self):
        assert _xml_text("") == ""


# ---------------------------------------------------------------------------
# _finite_val unit behavior
# ---------------------------------------------------------------------------

class TestFiniteVal:
    def test_finite_returns_unchanged(self):
        # Criterion 1: byte-identical formatting downstream relies on identity.
        for v in (0.0, 1.5, -3.25, 1e30, -1e30, 100, 0):
            assert _finite_val(v) == v

    def test_nan_to_zero(self):
        assert _finite_val(float("nan")) == 0.0

    def test_pos_inf_clamped(self):
        assert _finite_val(float("inf")) == _XML_INF_CLAMP

    def test_neg_inf_clamped(self):
        assert _finite_val(float("-inf")) == -_XML_INF_CLAMP

    def test_clamp_below_float32_max(self):
        # DP3: 1e38 < float32 max (3.4028235e38) so binary re-encode stays finite.
        assert _XML_INF_CLAMP < 3.4028235e38

    def test_result_always_finite(self):
        for v in (float("nan"), float("inf"), float("-inf")):
            assert math.isfinite(_finite_val(v))

    def test_idempotent(self):
        for v in (float("nan"), float("inf"), float("-inf"), 1.5):
            once = _finite_val(v)
            assert _finite_val(once) == once


# ---------------------------------------------------------------------------
# Leaf-writer byte-identity on valid input (criterion 1)
# ---------------------------------------------------------------------------

class TestLeafWriterByteIdentity:
    def test_add_float_valid(self):
        root = ET.Element("r")
        _add_float(root, "Volume", 1.5)
        assert root[0].text == "1.5"

    def test_add_vector3_valid(self):
        root = ET.Element("r")
        _add_vector3(root, "Size", 4.0, 1.0, 2.5)
        assert [c.text for c in root[0]] == ["4.0", "1.0", "2.5"]

    def test_add_cframe_valid(self):
        root = ET.Element("r")
        _add_cframe(root, "CFrame", RbxCFrame(x=1.0, y=2.0, z=3.0))
        # X/Y/Z then rotation. Just confirm no inf/nan and finite floats.
        for child in root[0]:
            assert child.text not in ("inf", "-inf", "nan")

    def test_add_string_valid(self):
        root = ET.Element("r")
        _add_string(root, "Name", "Door")
        assert root[0].text == "Door"

    def test_add_content_valid(self):
        root = ET.Element("r")
        _add_content(root, "Texture", "rbxassetid://9")
        assert root[0][0].text == "rbxassetid://9"


# ---------------------------------------------------------------------------
# XML control-char totality (criterion 2) -- every site re-parses cleanly
# ---------------------------------------------------------------------------

def _reparses(elem: ET.Element) -> None:
    """Assert ET.tostring(elem) re-parses under minidom without raising."""
    xml_bytes = ET.tostring(elem, encoding="unicode")
    xml.dom.minidom.parseString(f"<root>{xml_bytes}</root>")


class TestXmlControlCharTotality:
    @pytest.mark.parametrize("cp", _STRIPPED_CODEPOINTS)
    def test_name_site(self, cp):
        root = ET.Element("r")
        _add_string(root, "Name", f"Door{chr(cp)}End")
        _reparses(root)
        assert chr(cp) not in (root[0].text or "")

    @pytest.mark.parametrize("cp", _STRIPPED_CODEPOINTS)
    def test_url_site(self, cp):
        root = ET.Element("r")
        _add_content(root, "Texture", f"rbxassetid://9{chr(cp)}9")
        _reparses(root)
        assert chr(cp) not in (root[0][0].text or "")

    @pytest.mark.parametrize("cp", _STRIPPED_CODEPOINTS)
    def test_item_class_site(self, cp):
        root = ET.Element("r")
        item, _props = _make_item(root, f"Part{chr(cp)}", "Name")
        _reparses(root)
        assert chr(cp) not in item.get("class", "")

    @pytest.mark.parametrize("cp", _STRIPPED_CODEPOINTS)
    def test_service_class_site(self, cp):
        root = ET.Element("r")
        item = _make_service(root, f"Workspace{chr(cp)}")
        _reparses(root)
        assert chr(cp) not in item.get("class", "")

    @pytest.mark.parametrize("cp", _STRIPPED_CODEPOINTS)
    def test_protected_string_source_site(self, cp):
        # Control char inside script source -> CDATA still crashes minidom
        # without the strip.
        root = ET.Element("r")
        _add_protected_string(root, "Source", f"print('hi'){chr(cp)}end")
        _reparses(root)
        assert chr(cp) not in (root[0].text or "")


# ---------------------------------------------------------------------------
# XML float finiteness (criterion 3) -- F5-F16 hostile emitters
# ---------------------------------------------------------------------------

_HOSTILE_FLOATS = (float("inf"), float("-inf"), float("nan"))


def _all_text_finite(elem: ET.Element) -> None:
    """Every numeric-looking text node parses finite; none is inf/nan."""
    for node in elem.iter():
        txt = (node.text or "").strip()
        if not txt:
            continue
        assert "inf" not in txt.lower()
        assert "nan" not in txt.lower()
        # NumberRange/Sequence/etc. pack several space-separated numbers.
        for tok in txt.split():
            try:
                f = float(tok)
            except ValueError:
                continue  # non-numeric text node (e.g. a class name)
            assert math.isfinite(f), f"non-finite token {tok!r} in {txt!r}"


class TestXmlFloatFiniteness:
    @pytest.mark.parametrize("bad", _HOSTILE_FLOATS)
    def test_add_float(self, bad):
        root = ET.Element("r")
        _add_float(root, "Brightness", bad)
        _all_text_finite(root)

    @pytest.mark.parametrize("bad", _HOSTILE_FLOATS)
    def test_cframe(self, bad):
        root = ET.Element("r")
        _add_cframe(root, "CFrame", RbxCFrame(
            x=bad, y=bad, z=bad,
            r00=bad, r11=bad, r22=bad,
        ))
        _all_text_finite(root)

    @pytest.mark.parametrize("bad", _HOSTILE_FLOATS)
    def test_vector3(self, bad):
        root = ET.Element("r")
        _add_vector3(root, "Acceleration", bad, bad, bad)
        _all_text_finite(root)

    @pytest.mark.parametrize("bad", _HOSTILE_FLOATS)
    def test_color3_clamp_finitizes(self, bad):
        # F4: the 0-1 clamp already finitizes; assert no inf/nan leaks.
        root = ET.Element("r")
        _add_color3(root, "Color", bad, bad, bad)
        _all_text_finite(root)

    @pytest.mark.parametrize("bad", _HOSTILE_FLOATS)
    def test_particle_emitter_numberrange_sequence_vector2(self, bad):
        # F5-F9: NumberRange (Lifetime/Speed/Rotation/RotSpeed),
        # NumberSequence (Size/Transparency, both scalar + keypoint forms),
        # ColorSequence, SpreadAngle Vector2.
        pe = RbxParticleEmitter(
            rate=10.0,
            lifetime_min=bad, lifetime_max=bad,
            speed_min=bad, speed_max=bad,
            size_min=bad, size_max=bad,
            rotation_min=bad, rotation_max=bad,
            rot_speed_min=bad, rot_speed_max=bad,
            spread_angle=bad,
            transparency=bad,
            color_sequence=[(0.0, bad, bad, bad)],
            size_sequence=[(0.0, bad, 0.0)],
            transparency_sequence=[(0.0, bad, 0.0)],
        )
        root = ET.Element("r")
        _make_particle_emitter(root, pe)
        _all_text_finite(root)

    @pytest.mark.parametrize("bad", _HOSTILE_FLOATS)
    def test_ui_element_udim2(self, bad):
        # F11/F12: UDim2 Size + Position; the int(offset) path must not crash.
        elem = RbxUIElement(
            class_name="Frame",
            size=(bad, bad, bad, bad),
            position=(bad, bad, bad, bad),
        )
        root = ET.Element("r")
        _make_ui_element(root, elem)
        _all_text_finite(root)

    @pytest.mark.parametrize("bad", _HOSTILE_FLOATS)
    def test_part_color3uint8_and_cpp(self, bad):
        # F14: int(c*255) color path; F15: CustomPhysicalProperties .4f.
        part = RbxPart(
            name="P",
            cframe=RbxCFrame(x=0, y=0, z=0),
            size=(1, 1, 1),
            color=(bad, bad, bad),
            custom_physical_properties=(bad, bad, bad, bad, bad),
        )
        root = ET.Element("r")
        # _make_part needs a workspace-ish parent; a bare Element works.
        _make_part(root, part)
        _all_text_finite(root)

    def test_cpp_keeps_4f_format_on_valid(self):
        # DP8: f"{_finite_val(0.5):.4f}" must stay "0.5000".
        part = RbxPart(
            name="P",
            cframe=RbxCFrame(x=0, y=0, z=0),
            size=(1, 1, 1),
            custom_physical_properties=(0.5, 0.25, 0.0, 1.0, 1.0),
        )
        root = ET.Element("r")
        _make_part(root, part)
        densities = [n.text for n in root.iter("Density")]
        assert densities == ["0.5000"]


# ---------------------------------------------------------------------------
# Full write_rbxlx round-trip (criteria 2/3/8) with hostile fixture
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def _hostile_place(self) -> RbxPlace:
        ctrl = "\x00\x01\x07\x1f￾￿\ud800"
        pe = RbxParticleEmitter(
            rate=float("inf"),
            lifetime_min=float("nan"), lifetime_max=float("inf"),
            speed_min=float("-inf"), speed_max=float("nan"),
            spread_angle=float("inf"),
            transparency=float("nan"),
        )
        part = RbxPart(
            name=f"Door{ctrl}End",
            cframe=RbxCFrame(x=float("inf"), y=float("nan"), z=float("-inf")),
            size=(float("inf"), 1.0, 1.0),
            color=(float("nan"), float("inf"), 0.5),
            custom_physical_properties=(
                float("inf"), float("nan"), float("-inf"), 1.0, 1.0,
            ),
            particle_emitters=[pe],
        )
        ui_elem = RbxUIElement(
            class_name=f"Frame{ctrl}",
            size=(float("inf"), float("nan"), 0.0, 0.0),
            position=(float("nan"), float("inf"), 0.0, 0.0),
            text=f"Label{ctrl}",
        )
        gui = RbxScreenGui(name=f"HUD{ctrl}", elements=[ui_elem])
        return RbxPlace(
            workspace_parts=[part],
            screen_guis=[gui],
            lighting=RbxLightingConfig(brightness=float("inf")),
        )

    def test_hostile_place_reparses_and_is_finite(self, tmp_path):
        out = tmp_path / "hostile.rbxlx"
        write_rbxlx(self._hostile_place(), out)
        raw = out.read_text(encoding="utf-8")
        # Criterion 2: valid XML (minidom re-parse without raising).
        doc = xml.dom.minidom.parseString(raw)
        # Criterion 3: no literal inf/nan in any numeric text node.
        root = ET.parse(out).getroot()
        for node in root.iter():
            txt = (node.text or "").strip()
            for tok in txt.split():
                try:
                    f = float(tok)
                except ValueError:
                    continue
                assert math.isfinite(f)
        assert doc is not None

    def test_valid_place_unaffected(self, tmp_path):
        # Criterion 1 at the write_rbxlx level: a normal place still writes &
        # parses, no behavior change.
        part = RbxPart(
            name="Door",
            cframe=RbxCFrame(x=1, y=2, z=3),
            size=(4, 1, 2),
            color=(1.0, 0.0, 0.0),
        )
        place = RbxPlace(workspace_parts=[part])
        out = tmp_path / "ok.rbxlx"
        write_rbxlx(place, out)
        root = ET.parse(out).getroot()
        names = [n.text for n in root.iter("string") if n.get("name") == "Name"]
        assert "Door" in names
