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
