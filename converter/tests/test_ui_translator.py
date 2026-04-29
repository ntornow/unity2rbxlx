"""Tests for ui_translator.py -- Unity Canvas to Roblox ScreenGui conversion."""

import pytest
from converter.ui_translator import _extract_rect_transform, _apply_text_properties
from core.roblox_types import RbxUIElement


class TestRectTransform:
    """Tests for Unity RectTransform to Roblox UDim2 conversion."""

    def test_centered_element(self):
        """Centered element (anchors 0.5,0.5) with pixel size."""
        rt = {
            "m_AnchorMin": {"x": 0.5, "y": 0.5},
            "m_AnchorMax": {"x": 0.5, "y": 0.5},
            "m_AnchoredPosition": {"x": 0, "y": 0},
            "m_SizeDelta": {"x": 30, "y": 30},
            "m_Pivot": {"x": 0.5, "y": 0.5},
        }
        pos, size = _extract_rect_transform(rt)
        # Should be centered at (0.5, ?, 0.5, ?)
        assert abs(pos[0] - 0.5) < 0.01
        assert abs(pos[2] - 0.5) < 0.01
        # Size should be pixel-based
        assert size[0] == 0  # No scale
        assert size[1] == 30  # 30px X
        assert size[2] == 0  # No scale
        assert size[3] == 30  # 30px Y

    def test_stretched_element(self):
        """Full-width stretched element."""
        rt = {
            "m_AnchorMin": {"x": 0, "y": 0},
            "m_AnchorMax": {"x": 1, "y": 0.1},
            "m_AnchoredPosition": {"x": 0, "y": 0},
            "m_SizeDelta": {"x": 0, "y": 0},
            "m_Pivot": {"x": 0.5, "y": 0.5},
        }
        pos, size = _extract_rect_transform(rt)
        assert abs(size[0] - 1.0) < 0.01  # Full width scale
        assert abs(size[2] - 0.1) < 0.01  # 10% height scale

    def test_bottom_left_element(self):
        """Element anchored to bottom-left (Unity) = top-left position difference (Roblox)."""
        rt = {
            "m_AnchorMin": {"x": 0, "y": 0},
            "m_AnchorMax": {"x": 0, "y": 0},
            "m_AnchoredPosition": {"x": 50, "y": 50},
            "m_SizeDelta": {"x": 100, "y": 50},
            "m_Pivot": {"x": 0, "y": 0},
        }
        pos, size = _extract_rect_transform(rt)
        # Unity Y=0 is bottom; Roblox Y=0 is top
        # Position Y scale should be flipped (1.0 - anchor)
        assert pos[2] == 1.0  # Y scale = 1.0 - 0.0

    def test_empty_rect(self):
        pos, size = _extract_rect_transform({})
        assert pos == (0, 0, 0, 0)
        assert size == (1, 0, 1, 0)


class TestTextProperties:
    def test_explicit_font_size(self):
        element = RbxUIElement()
        props = {"m_Text": "Hello", "m_FontSize": 24}
        _apply_text_properties(element, props)
        assert element.text == "Hello"
        assert element.text_size == 24

    def test_missing_font_size_uses_element_height(self):
        """When font size is 0/missing, use element pixel height."""
        element = RbxUIElement()
        props = {"m_Text": "+"}
        size = (0, 30, 0, 30)  # 30px x 30px
        _apply_text_properties(element, props, size)
        assert element.text_size == 30

    def test_missing_font_size_fallback(self):
        """When no font size and no pixel height, fall back to 14."""
        element = RbxUIElement()
        props = {"m_Text": "text"}
        _apply_text_properties(element, props)
        assert element.text_size == 14

    def test_text_color(self):
        element = RbxUIElement()
        props = {"m_Text": "x", "m_FontSize": 12, "m_Color": {"r": 1, "g": 0, "b": 0.5}}
        _apply_text_properties(element, props)
        assert element.text_color == (1.0, 0.0, 0.5)

    def test_text_anchor_split(self):
        """Phase 4.6: m_Alignment 0..8 splits into TextXAlignment + TextYAlignment."""
        cases = [
            (0, "Left",   "Top"),
            (1, "Center", "Top"),
            (2, "Right",  "Top"),
            (3, "Left",   "Center"),
            (4, "Center", "Center"),
            (5, "Right",  "Center"),
            (6, "Left",   "Bottom"),
            (7, "Center", "Bottom"),
            (8, "Right",  "Bottom"),
        ]
        for anchor, want_x, want_y in cases:
            element = RbxUIElement()
            _apply_text_properties(element, {"m_Text": "t", "m_FontSize": 12, "m_Alignment": anchor})
            assert element.text_x_alignment == want_x, f"anchor={anchor}"
            assert element.text_y_alignment == want_y, f"anchor={anchor}"

    def test_text_anchor_absent_leaves_defaults(self):
        """When Unity doesn't set m_Alignment, writer-side defaults must win."""
        element = RbxUIElement()
        _apply_text_properties(element, {"m_Text": "t", "m_FontSize": 12})
        assert element.text_x_alignment == ""
        assert element.text_y_alignment == ""

    def test_font_map(self):
        """Phase 4.6: Unity m_Font.m_Name maps to Roblox Font enum labels."""
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "t",
            "m_FontSize": 12,
            "m_Font": {"m_Name": "Arial"},
        })
        assert element.font == "Arial"

    def test_font_unknown_leaves_default(self):
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "t",
            "m_FontSize": 12,
            "m_Font": {"m_Name": "SomeCustomFont"},
        })
        assert element.font == ""

    def test_alignment_and_font_nested_in_font_data(self):
        """Phase 4.6: SimpleFPS-style serialization stores m_Alignment and
        m_Font inside m_FontData; extractor must fall back to the nested
        layout when top-level keys are absent.
        """
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "Battery 1",
            "m_FontSize": 10,
            "m_FontData": {
                "m_Font": {"m_Name": "Arial"},
                "m_FontSize": 10,
                "m_Alignment": 7,  # LowerCenter
            },
        })
        assert element.font == "Arial"
        assert element.text_x_alignment == "Center"
        assert element.text_y_alignment == "Bottom"

    def test_top_level_alignment_wins_over_nested(self):
        """When both layouts are present, top-level takes precedence."""
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "x",
            "m_FontSize": 12,
            "m_Alignment": 0,  # UpperLeft
            "m_Font": {"m_Name": "Roboto"},
            "m_FontData": {
                "m_Alignment": 8,  # LowerRight
                "m_Font": {"m_Name": "Arial"},
            },
        })
        assert element.text_x_alignment == "Left"
        assert element.text_y_alignment == "Top"
        assert element.font == "Roboto"

    def test_tmp_horizontal_bitfield(self):
        """Phase 5.10: TMP m_HorizontalAlignment bitfields map to TextXAlignment.

        HorizontalAlignmentOptions: Left=1, Center=2, Right=4, Justified=8,
        Flush=16, Geometry=32. Justified collapses to Left, Flush/Geometry to
        Center (no Roblox equivalent).
        """
        cases = [
            (1, "Left"),
            (2, "Center"),
            (4, "Right"),
            (8, "Left"),    # Justified -> Left
            (16, "Center"), # Flush -> Center
            (32, "Center"), # Geometry -> Center
        ]
        for h_value, want_x in cases:
            element = RbxUIElement()
            _apply_text_properties(element, {
                "m_Text": "t",
                "m_FontSize": 12,
                "m_HorizontalAlignment": h_value,
            })
            assert element.text_x_alignment == want_x, f"h={h_value}"
            # Vertical untouched when only horizontal is set.
            assert element.text_y_alignment == ""

    def test_tmp_vertical_bitfield(self):
        """Phase 5.10: TMP m_VerticalAlignment bitfields map to TextYAlignment.

        VerticalAlignmentOptions: Top=256, Middle=512, Bottom=1024,
        Baseline=2048, Geometry=4096, Capline=8192. Baseline -> Bottom,
        Geometry -> Center, Capline -> Top.
        """
        cases = [
            (256, "Top"),
            (512, "Center"),
            (1024, "Bottom"),
            (2048, "Bottom"),  # Baseline
            (4096, "Center"),  # Geometry
            (8192, "Top"),     # Capline
        ]
        for v_value, want_y in cases:
            element = RbxUIElement()
            _apply_text_properties(element, {
                "m_Text": "t",
                "m_FontSize": 12,
                "m_VerticalAlignment": v_value,
            })
            assert element.text_y_alignment == want_y, f"v={v_value}"
            assert element.text_x_alignment == ""

    def test_tmp_bitfield_paired(self):
        """A TMP fixture with explicit bitfield values converts to matching
        text_x_alignment / text_y_alignment (the plan's acceptance criterion).
        """
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "Score",
            "m_FontSize": 18,
            "m_HorizontalAlignment": 4,    # Right
            "m_VerticalAlignment": 1024,    # Bottom
        })
        assert element.text_x_alignment == "Right"
        assert element.text_y_alignment == "Bottom"

    def test_tmp_bitfield_overrides_legacy_alignment(self):
        """When both legacy m_Alignment and TMP split fields are serialized,
        the TMP bitfield values win per axis.
        """
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "x",
            "m_FontSize": 12,
            "m_Alignment": 0,                  # UpperLeft (legacy)
            "m_HorizontalAlignment": 4,         # Right
            "m_VerticalAlignment": 1024,        # Bottom
        })
        assert element.text_x_alignment == "Right"
        assert element.text_y_alignment == "Bottom"

    def test_tmp_bitfield_partial_override_keeps_legacy_other_axis(self):
        """If only m_HorizontalAlignment is provided alongside legacy
        m_Alignment, the vertical axis comes from the legacy enum.
        """
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "x",
            "m_FontSize": 12,
            "m_Alignment": 7,                  # LowerCenter (legacy)
            "m_HorizontalAlignment": 1,         # Left
        })
        assert element.text_x_alignment == "Left"     # TMP wins on x
        assert element.text_y_alignment == "Bottom"   # Legacy wins on y

    def test_tmp_bitfield_zero_or_invalid_ignored(self):
        """0 and unparseable bitfields are ignored; defaults persist."""
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "x",
            "m_FontSize": 12,
            "m_HorizontalAlignment": 0,
            "m_VerticalAlignment": "garbage",
        })
        assert element.text_x_alignment == ""
        assert element.text_y_alignment == ""

    def test_tmp_bitfield_in_font_data_fallback(self):
        """Some TMP fixtures nest split fields inside m_FontData; fall back
        when top-level keys are absent.
        """
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "x",
            "m_FontSize": 12,
            "m_FontData": {
                "m_HorizontalAlignment": 2,   # Center
                "m_VerticalAlignment": 256,    # Top
            },
        })
        assert element.text_x_alignment == "Center"
        assert element.text_y_alignment == "Top"

    def test_tmp_bitfield_multibit_falls_back_to_lowest(self):
        """Multi-flag bitfield values resolve via lowest set bit."""
        element = RbxUIElement()
        _apply_text_properties(element, {
            "m_Text": "x",
            "m_FontSize": 12,
            "m_HorizontalAlignment": 1 | 32,   # Left | Geometry -> Left
            "m_VerticalAlignment": 256 | 4096,  # Top | Geometry -> Top
        })
        assert element.text_x_alignment == "Left"
        assert element.text_y_alignment == "Top"


class TestImageScriptGuidFallback:
    def test_mb_with_image_script_guid_detected_as_image(self):
        """Phase 4.6: Custom Image subclasses (MonoBehaviour + script GUID
        fe87c0e1...) are detected as Images even without m_Sprite in props."""
        from converter.ui_translator import _is_ui_image_mb

        assert _is_ui_image_mb({
            "m_Script": {"guid": "fe87c0e1cc204ed48ad3b37840f39efc"},
        })
        assert not _is_ui_image_mb({"m_Script": {"guid": "abc123"}})
        assert not _is_ui_image_mb({})
        assert not _is_ui_image_mb({"m_Script": "not-a-dict"})
