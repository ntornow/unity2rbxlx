"""Tests for ui_translator.py -- Unity Canvas to Roblox ScreenGui conversion."""

from converter.ui_translator import _extract_rect_transform, _apply_text_properties
from core.roblox_types import RbxUIElement
from core.unity_types import ComponentData, SceneNode


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


class TestBuildComponentOwnerIndex:
    """A5 — the component fileID -> owning GameObject fileID resolver.

    The Toggle's serialized ``graphic`` ref is a *component* fileID; the
    runtime binds the *owning* GameObject (the node carrying a
    ``_SceneRuntimeId``). This pure resolver makes that mapping.
    """

    @staticmethod
    def _node(
        name: str, file_id: str,
        components: list[ComponentData] | None = None,
        children: list[SceneNode] | None = None,
    ) -> SceneNode:
        return SceneNode(
            name=name,
            file_id=file_id,
            active=True,
            layer=0,
            tag="Untagged",
            components=components or [],
            children=children or [],
            parent_file_id=None,
        )

    def test_component_maps_to_owning_gameobject(self):
        """Component fileID C on GameObject G -> index maps C -> G."""
        from converter.ui_translator import build_component_owner_index

        go = self._node(
            "G", file_id="G",
            components=[
                ComponentData(component_type="Image", file_id="C", properties={}),
            ],
        )
        index = build_component_owner_index([go])
        assert index["C"] == "G"

    def test_out_of_canvas_subtree_component_still_resolves(self):
        """E4 — the resolver is scene-wide: a component on a GameObject in a
        sibling root (outside the canvas subtree) still resolves."""
        from converter.ui_translator import build_component_owner_index

        canvas_root = self._node(
            "Canvas", file_id="100",
            components=[
                ComponentData(component_type="Canvas", file_id="1001", properties={}),
            ],
        )
        # A separate top-level root (NOT under the canvas subtree).
        other_root = self._node(
            "OffCanvasGO", file_id="500",
            components=[
                ComponentData(component_type="Image", file_id="5001", properties={}),
            ],
        )
        index = build_component_owner_index([canvas_root, other_root])
        assert index["5001"] == "500"
        assert index["1001"] == "100"

    def test_recurses_into_nested_children(self):
        """Components on deeply nested children are indexed (recursive walk)."""
        from converter.ui_translator import build_component_owner_index

        grandchild = self._node(
            "Checkmark", file_id="250410364",
            components=[
                ComponentData(
                    component_type="Image", file_id="250410366", properties={},
                ),
            ],
        )
        child = self._node("Background", file_id="1614370918", children=[grandchild])
        toggle_go = self._node(
            "Battery", file_id="264237063",
            components=[
                ComponentData(
                    component_type="Toggle", file_id="264237065", properties={},
                ),
            ],
            children=[child],
        )
        index = build_component_owner_index([toggle_go])
        # The graphic ref (Image component 250410366) resolves to the
        # Checkmark GameObject (250410364) — the chain §1d pins.
        assert index["250410366"] == "250410364"
        assert index["264237065"] == "264237063"

    def test_unowned_fileid_absent(self):
        """E1 — a fileID with no owning node is absent (not present, no crash)."""
        from converter.ui_translator import build_component_owner_index

        go = self._node(
            "G", file_id="G",
            components=[
                ComponentData(component_type="Image", file_id="C", properties={}),
            ],
        )
        index = build_component_owner_index([go])
        assert "no-such-fileid" not in index
        assert index.get("0") is None

    def test_empty_roots_yields_empty_index(self):
        from converter.ui_translator import build_component_owner_index

        assert build_component_owner_index([]) == {}

    def test_pure_does_not_mutate_input(self):
        """Pure: input nodes/components are not mutated."""
        from converter.ui_translator import build_component_owner_index

        comps = [ComponentData(component_type="Image", file_id="C", properties={})]
        go = self._node("G", file_id="G", components=comps)
        before = [(c.component_type, c.file_id) for c in go.components]
        build_component_owner_index([go])
        after = [(c.component_type, c.file_id) for c in go.components]
        assert before == after


def _toggle_node(
    *, toggle_fid: str, graphic_comp_fid: object, m_is_on: int = 0,
    name: str = "TheToggle", component_type: str = "MonoBehaviour",
) -> SceneNode:
    """A Toggle GameObject whose Toggle component serializes a ``graphic`` ref.

    Defaults to ``MonoBehaviour`` — the way Unity ACTUALLY serializes a UI
    Toggle (an m_Script GUID, never a literal ``Toggle`` component_type). The
    earlier fixtures used the literal type, which is dead on real scenes and
    let the no-row regression slip past review.
    """
    return SceneNode(
        name=name,
        file_id=toggle_fid,
        active=True,
        layer=0,
        tag="Untagged",
        components=[
            ComponentData(
                component_type=component_type, file_id="toggleComp",
                properties={
                    "m_IsOn": m_is_on,
                    "graphic": {"fileID": graphic_comp_fid},
                },
            ),
        ],
        children=[],
        parent_file_id=None,
    )


class TestToggleGraphicBinding:
    """``_apply_toggle_properties`` emits a ``ToggleBinding`` row when the
    Toggle's serialized ``graphic`` resolves to an owning GameObject.

    Anchored on the Unity Toggle component + ``graphic`` fileID, resolved via
    ``build_component_owner_index`` to the checkmark's ``_SceneRuntimeId``.
    NO node-name matching, NO transport attribute on the produced element.
    """

    NS = "Assets/Scenes/main.unity"

    def _convert(self, node: SceneNode, *, owner_index, bindings):
        from converter.ui_translator import _convert_ui_element
        return _convert_ui_element(
            node, scene_namespace=self.NS,
            component_owner_index=owner_index,
            toggle_bindings=bindings,
        )

    def test_resolvable_graphic_emits_row(self):
        """A1 pipeline-path: a Toggle whose ``graphic`` resolves emits a row
        with the toggle + checkmark SRIs, ``initial_on`` from ``m_IsOn``, and
        ``attr_name`` = the single-source constant."""
        from converter.ui_translator import _TOGGLE_ISON_ATTR

        # graphic component fileID 250410366 is owned by GameObject 250410364.
        owner_index = {"250410366": "250410364"}
        bindings: list = []
        node = _toggle_node(
            toggle_fid="264237063", graphic_comp_fid="250410366", m_is_on=0,
        )
        element = self._convert(node, owner_index=owner_index, bindings=bindings)

        assert len(bindings) == 1
        row = bindings[0]
        assert row["toggle_sri"] == f"{self.NS}:264237063"
        assert row["graphic_sri"] == f"{self.NS}:250410364"
        assert row["initial_on"] is False
        assert row["attr_name"] == _TOGGLE_ISON_ATTR
        # The real attribute ToggleIsOn is still recorded.
        assert element.attributes["ToggleIsOn"] is False
        # NO transport attribute leaked onto the produced element.
        for k in element.attributes:
            assert "graphic" not in k.lower()
            assert "togglegraphicref" not in k.lower().replace("_", "")

    def test_monobehaviour_serialized_toggle_emits_row(self):
        """REGRESSION (e2e-found): a real Unity Toggle is a ``MonoBehaviour``
        with ``m_IsOn`` (NOT a literal ``Toggle`` component_type). The dispatch
        must detect it by ``m_IsOn`` (mirroring the Button ``m_OnClick``
        heuristic), else NO row is emitted on real scenes — which is exactly
        what shipped silently broken until a live SimpleFPS conversion (the
        Battery toggle: 4 HUD toggles, 0 rows)."""
        owner_index = {"250410366": "250410364"}
        bindings: list = []
        node = _toggle_node(
            toggle_fid="264237063", graphic_comp_fid="250410366", m_is_on=0,
            component_type="MonoBehaviour",   # the REAL serialization
        )
        # sanity: the component is NOT the literal "Toggle" type
        assert node.components[0].component_type == "MonoBehaviour"
        self._convert(node, owner_index=owner_index, bindings=bindings)
        assert len(bindings) == 1
        assert bindings[0]["toggle_sri"] == f"{self.NS}:264237063"
        assert bindings[0]["graphic_sri"] == f"{self.NS}:250410364"

    def test_literal_toggle_component_type_still_emits_row(self):
        """Backward-compat: a literal ``Toggle`` component_type (if a parser
        ever resolves the GUID to the type name) still dispatches."""
        owner_index = {"gc": "gg"}
        bindings: list = []
        node = _toggle_node(
            toggle_fid="t", graphic_comp_fid="gc", m_is_on=0,
            component_type="Toggle",
        )
        self._convert(node, owner_index=owner_index, bindings=bindings)
        assert len(bindings) == 1

    def test_initial_on_true_when_m_is_on_set(self):
        """E2 — ``m_IsOn=1`` -> ``initial_on=True``."""
        owner_index = {"gc": "gg"}
        bindings: list = []
        node = _toggle_node(toggle_fid="t", graphic_comp_fid="gc", m_is_on=1)
        self._convert(node, owner_index=owner_index, bindings=bindings)
        assert bindings[0]["initial_on"] is True

    def test_float_m_is_on_coerces_not_dropped(self):
        """``m_IsOn`` may cross the YAML boundary as a float (``1.0`` when
        written ``1.0``); it must coerce (NOT silently drop ``ToggleIsOn`` and
        the row). Regression pin: an over-strict ``isinstance`` narrowing once
        sent every float ``m_IsOn`` to an early ``return``, diverging from the
        pre-slice ``bool(int(is_on))`` behavior."""
        owner_index = {"gc": "gg"}
        for raw, expected in ((1.0, True), (0.0, False), ("1.0", True)):
            bindings: list = []
            node = SceneNode(
                name="T", file_id="t", active=True, layer=0, tag="Untagged",
                components=[ComponentData(
                    component_type="Toggle", file_id="toggleComp",
                    properties={"m_IsOn": raw, "graphic": {"fileID": "gc"}},
                )],
                children=[], parent_file_id=None,
            )
            element = self._convert(
                node, owner_index=owner_index, bindings=bindings,
            )
            assert element.attributes["ToggleIsOn"] is expected, raw
            assert len(bindings) == 1, raw
            assert bindings[0]["initial_on"] is expected, raw

    def test_unparseable_m_is_on_emits_no_row(self):
        """A non-numeric ``m_IsOn`` (no valid int) returns early: no
        ``ToggleIsOn`` attribute, no binding row, no crash."""
        owner_index = {"gc": "gg"}
        bindings: list = []
        node = SceneNode(
            name="T", file_id="t", active=True, layer=0, tag="Untagged",
            components=[ComponentData(
                component_type="Toggle", file_id="toggleComp",
                properties={"m_IsOn": "nope", "graphic": {"fileID": "gc"}},
            )],
            children=[], parent_file_id=None,
        )
        element = self._convert(node, owner_index=owner_index, bindings=bindings)
        assert "ToggleIsOn" not in element.attributes
        assert bindings == []

    def test_graphic_zero_emits_no_row(self):
        """E1 — ``graphic:{fileID:0}`` -> no binding row."""
        owner_index = {"gc": "gg"}
        bindings: list = []
        node = _toggle_node(toggle_fid="t", graphic_comp_fid=0, m_is_on=1)
        element = self._convert(node, owner_index=owner_index, bindings=bindings)
        assert bindings == []
        # ToggleIsOn still recorded (it is a real attribute).
        assert element.attributes["ToggleIsOn"] is True

    def test_unresolvable_graphic_emits_no_row(self):
        """E1 — a ``graphic`` fileID with no owner in the index -> no row."""
        owner_index = {"other": "gg"}  # does not contain "gc"
        bindings: list = []
        node = _toggle_node(toggle_fid="t", graphic_comp_fid="gc", m_is_on=0)
        self._convert(node, owner_index=owner_index, bindings=bindings)
        assert bindings == []

    def test_none_index_or_accumulator_emits_no_row(self):
        """Legacy / synthetic callers (``None`` index or accumulator) emit
        nothing -- byte-identical legacy output, no crash."""
        node = _toggle_node(toggle_fid="t", graphic_comp_fid="gc", m_is_on=1)
        # None index, with accumulator present.
        bindings: list = []
        el = self._convert(node, owner_index=None, bindings=bindings)
        assert bindings == []
        assert el.attributes["ToggleIsOn"] is True
        # Index present, None accumulator (no crash, no row recorded anywhere).
        el2 = self._convert(node, owner_index={"gc": "gg"}, bindings=None)
        assert el2.attributes["ToggleIsOn"] is True

    def test_non_hud_toggle_still_emits_row(self):
        """E3 — a Toggle anywhere (not HUD-specific) emits a row; keyed on the
        component + graphic, never node names."""
        owner_index = {"gc": "gg"}
        bindings: list = []
        node = _toggle_node(
            toggle_fid="optTog", graphic_comp_fid="gc", m_is_on=0,
            name="OptionsMenuSoundToggle",
        )
        self._convert(node, owner_index=owner_index, bindings=bindings)
        assert len(bindings) == 1
        assert bindings[0]["toggle_sri"] == f"{self.NS}:optTog"

    def test_graphic_equals_toggle_resolves_to_self(self):
        """E4 — ``graphic`` owned by the Toggle's own GameObject -> the toggle
        hides/shows itself (well-defined)."""
        owner_index = {"gc": "t"}  # graphic component owned by the toggle GO
        bindings: list = []
        node = _toggle_node(toggle_fid="t", graphic_comp_fid="gc", m_is_on=0)
        self._convert(node, owner_index=owner_index, bindings=bindings)
        assert bindings[0]["graphic_sri"] == f"{self.NS}:t"
        assert bindings[0]["toggle_sri"] == f"{self.NS}:t"

    def test_convert_canvas_threads_accumulator(self):
        """Pipeline path: ``convert_canvas`` threads the by-ref accumulator so
        a Toggle under a Canvas appends its row (no transport attribute)."""
        from converter.ui_translator import convert_canvas

        toggle = _toggle_node(
            toggle_fid="264237063", graphic_comp_fid="250410366", m_is_on=0,
        )
        canvas = SceneNode(
            name="Canvas", file_id="canvasFid", active=True, layer=0,
            tag="Untagged",
            components=[ComponentData("Canvas", "canvasComp", {})],
            children=[toggle], parent_file_id=None,
        )
        owner_index = {"250410366": "250410364"}
        bindings: list = []
        convert_canvas(
            [canvas], scene_namespace=self.NS,
            component_owner_index=owner_index, toggle_bindings=bindings,
        )
        assert len(bindings) == 1
        assert bindings[0]["toggle_sri"] == f"{self.NS}:264237063"
        assert bindings[0]["graphic_sri"] == f"{self.NS}:250410364"


class TestCanvasEnabled:
    """`_canvas_enabled` AND-semantics + `convert_canvas` Enabled wiring.

    Unity renders a Canvas only when BOTH the GameObject is active AND the
    Canvas component is enabled (`m_Enabled`). The ScreenGui.Enabled contract
    mirrors that AND. (AC#1, AC#2, AC#6)
    """

    NS = "TestScene"

    @staticmethod
    def _canvas_node(active: bool, m_enabled: int | None,
                     has_canvas_comp: bool = True) -> SceneNode:
        comps: list[ComponentData] = []
        if has_canvas_comp:
            props: dict[str, object] = (
                {} if m_enabled is None else {"m_Enabled": m_enabled})
            comps.append(ComponentData("Canvas", "canvasComp", props))
        return SceneNode(
            name="Canvas", file_id="canvasFid", active=active, layer=0,
            tag="Untagged", components=comps, children=[], parent_file_id=None,
        )

    def test_active_and_enabled_true(self):
        """active=True, m_Enabled=1 -> True. (AC#2)"""
        from converter.ui_translator import _canvas_enabled
        assert _canvas_enabled(self._canvas_node(True, 1)) is True

    def test_inactive_false(self):
        """active=False short-circuits to False regardless of m_Enabled. (AC#2)"""
        from converter.ui_translator import _canvas_enabled
        assert _canvas_enabled(self._canvas_node(False, 1)) is False

    def test_active_but_canvas_disabled_false(self):
        """active=True, m_Enabled=0 -> False (the AND). (AC#2)"""
        from converter.ui_translator import _canvas_enabled
        assert _canvas_enabled(self._canvas_node(True, 0)) is False

    def test_missing_m_enabled_defaults_true(self):
        """Canvas component present but no m_Enabled key -> defaults True. (AC#2)"""
        from converter.ui_translator import _canvas_enabled
        assert _canvas_enabled(self._canvas_node(True, None)) is True

    def test_no_canvas_component_active_only(self):
        """No Canvas component -> gates on active alone, never spurious False. (AC#2)"""
        from converter.ui_translator import _canvas_enabled
        assert _canvas_enabled(
            self._canvas_node(True, None, has_canvas_comp=False)) is True
        assert _canvas_enabled(
            self._canvas_node(False, None, has_canvas_comp=False)) is False

    @staticmethod
    def _canvas_node_raw_m_enabled(active: bool, raw: object) -> SceneNode:
        """Canvas node whose m_Enabled holds an arbitrary (possibly non-int)
        value, so we can exercise present-but-None / non-numeric inputs that
        the int-typed `_canvas_node` helper cannot express."""
        comp = ComponentData("Canvas", "canvasComp", {"m_Enabled": raw})
        return SceneNode(
            name="Canvas", file_id="canvasFid", active=active, layer=0,
            tag="Untagged", components=[comp], children=[], parent_file_id=None,
        )

    def test_present_none_m_enabled_defaults_true(self):
        """m_Enabled present-but-None -> defaults True, no crash.

        Pre-fix `int(None)` raised TypeError; the isinstance guard now
        defaults a non-int/non-bool value to True. (AC#2)
        """
        from converter.ui_translator import _canvas_enabled
        assert _canvas_enabled(
            self._canvas_node_raw_m_enabled(True, None)) is True

    def test_nonnumeric_string_m_enabled_defaults_true(self):
        """m_Enabled present as a non-numeric string -> defaults True, no crash.

        Pre-fix `int("true")` raised ValueError/TypeError; the isinstance
        guard now defaults a non-int/non-bool value to True. (AC#2)
        """
        from converter.ui_translator import _canvas_enabled
        assert _canvas_enabled(
            self._canvas_node_raw_m_enabled(True, "true")) is True

    def test_default_synthetic_node_true(self):
        """A synthetic node (active default True, no Canvas) -> True. (AC#1)"""
        from converter.ui_translator import _canvas_enabled
        node = SceneNode(name="Canvas", file_id="f", active=True, layer=0,
                         tag="Untagged")
        assert _canvas_enabled(node) is True

    def test_convert_canvas_sets_enabled(self):
        """`convert_canvas` threads `_canvas_enabled` onto the ScreenGui. (AC#2)"""
        from converter.ui_translator import convert_canvas
        enabled = convert_canvas([self._canvas_node(True, 1)],
                                 scene_namespace=self.NS)
        disabled = convert_canvas([self._canvas_node(False, 1)],
                                  scene_namespace=self.NS)
        assert enabled[0].enabled is True
        assert disabled[0].enabled is False

    def test_trash_dash_scene_active_states(self):
        """E2E: real trash-dash Main.unity -> Loadout enabled, the rest
        disabled, via the real scene_parser + find_canvas_nodes +
        convert_canvas. (AC#6)

        Real-corpus only: skips (does NOT silently substitute synthetic data)
        when the scene is absent, so the suite honestly reports whether the
        real-corpus check ran. The synthetic AND-semantics matrix is covered
        by the other tests in this class.
        """
        import pytest
        from pathlib import Path
        from converter.ui_translator import convert_canvas, find_canvas_nodes

        scene = Path("/Users/jiazou/workspace/trash-dash/Assets/Scenes/Main.unity")
        if not scene.exists():
            pytest.skip("trash-dash Main.unity not present in this env")
        from unity.scene_parser import parse_scene
        parsed = parse_scene(scene)
        canvases = find_canvas_nodes(parsed.roots)
        guis = convert_canvas(canvases, scene_namespace=self.NS)
        by_name = {g.name: g.enabled for g in guis}
        # The active boot canvas ships enabled; the rest ship disabled.
        assert by_name.get("Loadout") is True, by_name
        for n in ("Game", "GameOver", "Leaderboard"):
            assert by_name.get(n) is False, by_name

    def test_trash_dash_scene_enabled_serialized_full_chain(self, tmp_path):
        """Full chain: real trash-dash Main.unity -> parse -> find_canvas_nodes
        -> convert_canvas -> RbxPlace -> BOTH serializers, asserting each named
        ScreenGui lands the correct `Enabled` value in the ACTUAL serialized
        output. Completes AC#6's designed serialized-XML form (the parse-only
        sibling above stops at the in-memory RbxScreenGui list).

        Real-corpus only: skips (does NOT silently substitute synthetic data)
        when the scene is absent, mirroring the AC#6 sibling.
        """
        import re
        import xml.etree.ElementTree as ET
        from pathlib import Path

        import pytest

        from converter.ui_translator import convert_canvas, find_canvas_nodes
        from core.roblox_types import RbxPlace
        from roblox.luau_place_builder import generate_place_luau
        from roblox.rbxlx_writer import write_rbxlx

        scene = Path("/Users/jiazou/workspace/trash-dash/Assets/Scenes/Main.unity")
        if not scene.exists():
            pytest.skip("trash-dash Main.unity not present in this env")

        from unity.scene_parser import parse_scene
        parsed = parse_scene(scene)
        canvases = find_canvas_nodes(parsed.roots)
        guis = convert_canvas(canvases, scene_namespace=self.NS)
        place = RbxPlace(screen_guis=guis)

        # The four canvases of interest and their expected Enabled value.
        expected = {
            "Loadout": True,
            "Game": False,
            "GameOver": False,
            "Leaderboard": False,
        }

        # --- rbxlx serialization: locate each ScreenGui Item by its Name
        #     string property, then read its own Enabled bool (no global
        #     substring -- that can't tell the canvases apart). ---
        out = tmp_path / "place.rbxlx"
        write_rbxlx(place, out)
        root = ET.parse(out).getroot()

        def _prop_text(props: ET.Element, tag: str, name: str) -> str | None:
            for el in props.findall(tag):
                if el.get("name") == name:
                    return el.text
            return None

        enabled_by_name: dict[str, str | None] = {}
        for item in root.iter("Item"):
            if item.get("class") != "ScreenGui":
                continue
            props = item.find("Properties")
            assert props is not None
            sg_name = _prop_text(props, "string", "Name")
            if sg_name in expected:
                enabled_by_name[sg_name] = _prop_text(props, "bool", "Enabled")

        for name, want in expected.items():
            assert enabled_by_name.get(name) == ("true" if want else "false"), (
                f"rbxlx ScreenGui {name!r} Enabled={enabled_by_name.get(name)!r}"
            )

        # --- luau serialization: each ScreenGui emits a `g.Name="<name>"`
        #     immediately followed (within its own `do` block) by a
        #     `g.Enabled=true/false`. Match each block and check the value. ---
        luau = generate_place_luau(place)
        block_re = re.compile(
            r"g\.Name=\"(?P<name>[^\"]+)\".*?g\.Enabled=(?P<enabled>true|false)",
            re.DOTALL,
        )
        luau_enabled: dict[str, str] = {}
        for m in block_re.finditer(luau):
            nm = m.group("name")
            if nm in expected and nm not in luau_enabled:
                luau_enabled[nm] = m.group("enabled")

        for name, want in expected.items():
            assert luau_enabled.get(name) == ("true" if want else "false"), (
                f"luau ScreenGui {name!r} Enabled={luau_enabled.get(name)!r}"
            )

        # Robust cross-check: among the four canvases, exactly one is enabled.
        canvas_enabled = [luau_enabled[n] for n in expected]
        assert canvas_enabled.count("true") == 1, canvas_enabled
        assert canvas_enabled.count("false") == 3, canvas_enabled


class TestToggleIsOnAttrConvention:
    """Pin ``_TOGGLE_ISON_ATTR`` to the converter's Toggle-``isOn`` LOWERING
    CONVENTION, checked against EVERY converted-writer shape in the contract
    corpus (not just one fixture line).

    The transpiler lowers ``GetComponent<Toggle>().isOn = ...`` to
    ``<inst>:SetAttribute("<name>", ...)`` and stamps a
    ``GetComponent<Toggle>().isOn`` comment above it. This test asserts every
    such corpus write uses an attribute name equal to ``_TOGGLE_ISON_ATTR`` --
    so a convention/casing drift fails RED at converter-build time (the
    attr_name is an AI-output fingerprint, surfaced + guarded here).
    """

    def test_constant_value(self):
        from converter.ui_translator import _TOGGLE_ISON_ATTR
        assert _TOGGLE_ISON_ATTR == "isOn"

    def test_constant_matches_corpus_toggle_writer_convention(self):
        import json
        import re
        from pathlib import Path

        from converter.ui_translator import _TOGGLE_ISON_ATTR

        corpus_root = (
            Path(__file__).parent / "fixtures" / "contract_corpus"
        )
        # The lowering marker the transpiler emits above each Toggle-isOn write.
        marker_re = re.compile(r"GetComponent<Toggle>\(\)\.(\w+)")
        setattr_re = re.compile(
            r"SetAttribute\(\s*[\"']([^\"']+)[\"']"
        )

        checked = 0
        for fixture in corpus_root.glob("*/fixture.json"):
            data = json.loads(fixture.read_text(encoding="utf-8"))
            for script in data.get("scripts", []):
                src = script.get("source", "")
                if not isinstance(src, str):
                    continue
                for line in src.splitlines():
                    m = marker_re.search(line)
                    if not m:
                        continue
                    # The lowered attr name the convention produced for this
                    # Toggle-isOn write. The comment names the Unity member
                    # (``isOn``); the binding's constant must equal it AND the
                    # actual SetAttribute literal in this writer.
                    assert m.group(1) == _TOGGLE_ISON_ATTR, (
                        f"{script.get('name')}: Toggle member {m.group(1)!r} "
                        f"!= constant {_TOGGLE_ISON_ATTR!r}"
                    )
                    checked += 1
                # A Toggle-isOn writer must actually EMIT the lowered
                # ``SetAttribute(_TOGGLE_ISON_ATTR, ...)`` literal -- assert the
                # real literal is present (not merely "count the ones that
                # already match", which a drifted literal would silently skip).
                if "GetComponent<Toggle>().isOn" in src:
                    setattr_names = setattr_re.findall(src)
                    assert _TOGGLE_ISON_ATTR in setattr_names, (
                        f"{script.get('name')}: Toggle-isOn writer emits no "
                        f"SetAttribute({_TOGGLE_ISON_ATTR!r}, ...) literal "
                        f"(found {setattr_names!r}) -- convention drift"
                    )
                    checked += 1
        # The corpus must contain at least one Toggle-isOn writer to anchor on.
        assert checked >= 1, "no Toggle-isOn writer found in contract corpus"


class TestInactiveUiSubtreeEmission:
    """Gap #4 — an inactive UI subtree is EMITTED (hidden), not pruned.

    A Unity inactive GameObject (``m_IsActive: 0``) still EXISTS and is
    commonly woken later by a script ``SetActive(true)`` (the
    ``SettingPopup → AboutPopup`` popups). Before the fix
    (``ui_translator.py:394`` ``if not node.active: return None``) the whole
    inactive subtree was dropped from StarterGui, so none of its nodes got
    the ``_SceneRuntimeId``-stamped host clone the scene-runtime planner
    emits deferred-component rows against → "UI host clone … never landed".

    The fix emits the node hidden (``Visible=false`` via
    ``visible=node.active``) AND keeps recursing, so the host clone + its
    descendants land while honoring Unity's inactive intent.
    """

    NS = "Assets/Scenes/Main.unity"

    @staticmethod
    def _node(
        name: str, file_id: str, *, active: bool = True,
        components: list[ComponentData] | None = None,
        children: list[SceneNode] | None = None,
    ) -> SceneNode:
        return SceneNode(
            name=name,
            file_id=file_id,
            active=active,
            layer=0,
            tag="Untagged",
            components=components or [],
            children=children or [],
            parent_file_id=None,
        )

    def _convert(self, node: SceneNode) -> RbxUIElement | None:
        from converter.ui_translator import _convert_ui_element
        return _convert_ui_element(node, scene_namespace=self.NS)

    def _by_sri(self, element: RbxUIElement) -> dict[str, RbxUIElement]:
        """Flatten the produced tree into a ``_SceneRuntimeId -> element`` map."""
        out: dict[str, RbxUIElement] = {}

        def walk(el: RbxUIElement) -> None:
            sri = el.attributes.get("_SceneRuntimeId")
            if isinstance(sri, str):
                out[sri] = el
            for child in el.children:
                walk(child)

        walk(element)
        return out

    def _about_popup_tree(self) -> SceneNode:
        """The real inactive ``SettingPopup → AboutPopup`` hierarchy that
        hosts the 3 warned deferred components:

          - ConfirmPopup ``1918594629`` (DataDeleteConfirmation, m_IsActive 0)
          - VisitUnityButton ``1834564028`` (OpenURL)
          - VisitGameChangerButton ``375939466`` (OpenURL)
        """
        confirm = self._node(
            "ConfirmPopup", "1918594629", active=False,
            components=[ComponentData(
                component_type="MonoBehaviour", file_id="a37c4b6",
                properties={},
            )],
        )
        visit_unity = self._node(
            "VisitUnityButton", "1834564028",
            components=[ComponentData(
                component_type="MonoBehaviour", file_id="99717f89a",
                properties={},
            )],
        )
        visit_gc = self._node(
            "VisitGameChangerButton", "375939466",
            components=[ComponentData(
                component_type="MonoBehaviour", file_id="99717f89b",
                properties={},
            )],
        )
        about = self._node(
            "AboutPopup", "200", active=False,
            children=[visit_unity, visit_gc],
        )
        return self._node(
            "SettingPopup", "100", active=False,
            children=[about, confirm],
        )

    def test_inactive_subtree_emitted_with_host_clones(self):
        """AC4 — the 3 named deferred-component host ids land with their
        ``_SceneRuntimeId`` stamped (descendants of an inactive subtree are
        emitted, not pruned)."""
        element = self._convert(self._about_popup_tree())
        assert element is not None  # the inactive root itself is emitted
        sris = self._by_sri(element)
        for fid in ("100", "200", "1918594629", "1834564028", "375939466"):
            assert f"{self.NS}:{fid}" in sris, (
                f"host clone for {fid} missing — inactive subtree pruned"
            )

    def test_emitted_inactive_node_is_hidden_not_visible(self):
        """E6 — the emitted inactive node lands hidden (``Visible=false``), so
        the menu does not show the popup at boot; an active descendant under
        an inactive parent keeps its own active visibility."""
        element = self._convert(self._about_popup_tree())
        assert element is not None
        sris = self._by_sri(element)
        # Inactive roots/subtree -> hidden.
        assert sris[f"{self.NS}:100"].visible is False
        assert sris[f"{self.NS}:200"].visible is False
        assert sris[f"{self.NS}:1918594629"].visible is False
        # An active descendant (the OpenURL buttons) keeps Visible=true; the
        # runtime keeps the inactive PARENT hidden until SetActive(true).
        assert sris[f"{self.NS}:1834564028"].visible is True
        assert sris[f"{self.NS}:375939466"].visible is True

    def test_active_subtree_unchanged(self):
        """A normally-active subtree is emitted exactly as before (no
        behavior change for the common path)."""
        child = self._node(
            "ActiveChild", "11",
            components=[ComponentData(
                component_type="Image", file_id="111", properties={},
            )],
        )
        root = self._node("ActivePanel", "10", children=[child])
        element = self._convert(root)
        assert element is not None
        sris = self._by_sri(element)
        assert sris[f"{self.NS}:10"].visible is True
        assert sris[f"{self.NS}:11"].visible is True
        assert f"{self.NS}:11" in sris  # active child still recursed

    def test_nested_inactive_within_inactive(self):
        """Edge — an inactive node nested under another inactive node is still
        emitted (recursion does not stop at the first inactive boundary)."""
        deep = self._node(
            "DeepInactive", "30", active=False,
            components=[ComponentData(
                component_type="MonoBehaviour", file_id="300", properties={},
            )],
        )
        mid = self._node("MidInactive", "20", active=False, children=[deep])
        root = self._node("RootInactive", "10", active=False, children=[mid])
        element = self._convert(root)
        assert element is not None
        sris = self._by_sri(element)
        for fid in ("10", "20", "30"):
            assert f"{self.NS}:{fid}" in sris
            assert sris[f"{self.NS}:{fid}"].visible is False

    def test_bug_guard_pre_fix_would_prune(self):
        """BUG-GUARD (fails pre-fix) — under the old
        ``if not node.active: return None`` an inactive node returned ``None``
        and its descendants never landed. Post-fix the inactive root is a
        real element whose deferred-component descendants carry a
        ``_SceneRuntimeId``. This assertion is impossible against the pruned
        (None) tree."""
        element = self._convert(self._about_popup_tree())
        # Pre-fix: element is None (whole subtree pruned) -> AttributeError on
        # the next line, OR (if guarded) the descendant ids are simply absent.
        assert element is not None
        sris = self._by_sri(element)
        # The deepest deferred-component descendants — only reachable if the
        # inactive subtree was recursed into rather than dropped at the root.
        assert f"{self.NS}:1834564028" in sris
        assert f"{self.NS}:375939466" in sris


class TestClickBindingEmit:
    """``_emit_click_bindings`` (via ``_convert_ui_element``) emits one
    CANDIDATE ``ClickBinding`` per dispatchable onClick call, and records the
    static-arg / unresolvable rest in the unsupported accumulator.
    Component-precise (registry key), fail-loud. The DOMAIN gate is DEFERRED to
    a post-classification reclassification step (design AMENDMENT r3) -- stage 1
    is domain-AGNOSTIC, so a server/excluded target still emits a candidate here
    and is moved to unsupported later (see ``TestClickBindingDomainReclassify``).
    NO transport attribute on the produced element.
    """

    NS = "Assets/Scenes/main.unity"

    def _button_node(
        self, *, button_fid: str, target_fid: str, method: str,
        mode: int = 1, call_state: int = 2, name: str = "TheButton",
        extra_calls: list | None = None,
    ) -> SceneNode:
        calls = [{
            "m_MethodName": method,
            "m_CallState": call_state,
            "m_Mode": mode,
            "m_Target": {"fileID": target_fid},
        }]
        if extra_calls:
            calls.extend(extra_calls)
        return SceneNode(
            name=name, file_id=button_fid, active=True, layer=0, tag="Untagged",
            components=[ComponentData(
                component_type="MonoBehaviour", file_id=f"{button_fid}comp",
                properties={"m_OnClick": {"m_PersistentCalls": {"m_Calls": calls}}},
            )],
            children=[], parent_file_id=None,
        )

    def _convert(self, node, *, owner_index, clicks, unsupported):
        from converter.ui_translator import _convert_ui_element
        return _convert_ui_element(
            node, scene_namespace=self.NS,
            component_owner_index=owner_index,
            click_bindings=clicks,
            unsupported_click_bindings=unsupported,
        )

    def test_client_target_emits_component_precise_row(self):
        """A void-method (mode 1), owner-resolvable onClick emits ONE
        component-precise CANDIDATE ClickBinding with the owning-GO SRI +
        registry key. Domain-agnostic at this stage."""
        owner = {"869760749": "869760744"}        # target comp -> owning GO
        clicks: list = []
        unsupported: list = []
        node = self._button_node(
            button_fid="500", target_fid="869760749", method="StartGame", mode=1,
        )
        element = self._convert(
            node, owner_index=owner,
            clicks=clicks, unsupported=unsupported,
        )
        assert unsupported == []
        assert len(clicks) == 1
        row = clicks[0]
        assert row["button_sri"] == f"{self.NS}:500"
        assert row["target_sri"] == f"{self.NS}:869760744"
        assert row["target_component_id"] == f"{self.NS}:869760749"
        assert row["method"] == "StartGame"
        assert row["call_index"] == 0
        # No transport attribute on the produced element.
        assert "_OnClick" not in (element.attributes or {})

    def test_server_target_still_emits_candidate_at_stage1(self):
        """Domain is deferred: a target that will classify ``server`` STILL
        emits a candidate at stage 1 (no domain consulted here). It is moved to
        unsupported only by the post-classification reclassifier."""
        owner = {"77": "70"}
        clicks: list = []
        unsupported: list = []
        self._convert(
            self._button_node(button_fid="9", target_fid="77", method="ServerOnly"),
            owner_index=owner,
            clicks=clicks, unsupported=unsupported,
        )
        assert len(clicks) == 1
        assert clicks[0]["method"] == "ServerOnly"
        assert unsupported == []

    def test_static_argument_mode_recorded_unsupported(self):
        """A static-argument call (m_Mode >= 2; here Bool=6) is unsupported --
        the static-arg gate is at stage 1 (independent of domain)."""
        owner = {"77": "70"}
        clicks: list = []
        unsupported: list = []
        self._convert(
            self._button_node(
                button_fid="9", target_fid="77", method="SetActive", mode=6,
            ),
            owner_index=owner,
            clicks=clicks, unsupported=unsupported,
        )
        assert clicks == []
        assert unsupported[0]["reason"] == "static_argument"

    def test_unresolved_target_recorded_unsupported(self):
        """A target fileID absent from the owner index is unsupported -- the
        owner-resolution gate is at stage 1 (independent of domain)."""
        owner: dict = {}                       # nothing resolves
        clicks: list = []
        unsupported: list = []
        self._convert(
            self._button_node(button_fid="9", target_fid="77", method="X"),
            owner_index=owner,
            clicks=clicks, unsupported=unsupported,
        )
        assert clicks == []
        assert unsupported[0]["reason"] == "unresolved_target"

    def test_editor_and_runtime_call_state_emits(self):
        """FIX 1: Unity UnityEventCallState is Off=0, EditorAndRuntime=1,
        RuntimeOnly=2. BOTH EditorAndRuntime(1) and RuntimeOnly(2) invoke at
        runtime, so an EditorAndRuntime(1) client-domain button MUST emit a
        ClickBinding. RED against the pre-fix ``call_state >= 2`` gate, which
        silently dropped EditorAndRuntime listeners (they shipped dead and never
        reached the unsupported report).
        """
        owner = {"869760749": "869760744"}
        clicks: list = []
        unsupported: list = []
        node = self._button_node(
            button_fid="500", target_fid="869760749", method="StartGame",
            mode=1, call_state=1,   # EditorAndRuntime
        )
        self._convert(
            node, owner_index=owner,
            clicks=clicks, unsupported=unsupported,
        )
        assert len(clicks) == 1, (clicks, unsupported)
        assert clicks[0]["method"] == "StartGame"
        assert unsupported == []

    def test_off_call_state_neither_emits_nor_unsupported(self):
        """FIX 1: an Off(0) listener is deliberately disabled in Unity -- it
        does NOT invoke at runtime, so it correctly emits NO ClickBinding AND is
        NOT recorded as unsupported (a disabled listener is not a binding this
        version 'cannot honor', it's one the author turned off)."""
        owner = {"77": "70"}
        clicks: list = []
        unsupported: list = []
        node = self._button_node(
            button_fid="9", target_fid="77", method="Disabled",
            mode=1, call_state=0,   # Off
        )
        self._convert(
            node, owner_index=owner,
            clicks=clicks, unsupported=unsupported,
        )
        assert clicks == []
        assert unsupported == []

    def test_multi_call_preserves_order_and_call_index(self):
        """Two onClick calls -> two rows, call_index 0 and 1 in order."""
        owner = {"10": "100", "20": "200"}
        clicks: list = []
        unsupported: list = []
        node = self._button_node(
            button_fid="9", target_fid="10", method="First", mode=1,
            extra_calls=[{
                "m_MethodName": "Second", "m_CallState": 2, "m_Mode": 1,
                "m_Target": {"fileID": "20"},
            }],
        )
        self._convert(
            node, owner_index=owner,
            clicks=clicks, unsupported=unsupported,
        )
        assert [r["method"] for r in clicks] == ["First", "Second"]
        assert [r["call_index"] for r in clicks] == [0, 1]

    def test_real_main_unity_startbutton_emits_loadout_candidate(self):
        """Acceptance (i, stage 1): the REAL trash-dash Main.unity StartButton
        emits a CANDIDATE ClickBinding -> the LoadoutState-owning GameObject,
        method StartGame, with the real owner-resolved SRI + registry key. The
        domain is NOT consulted at this stage (deferred to reclassification);
        the candidate emits regardless of domain.

        Real-corpus only: skips (does NOT substitute synthetic data) when the
        scene is absent. Drives the converter's own scene_parser +
        build_component_owner_index, exactly the production resolution path.
        """
        import pytest
        from pathlib import Path
        from converter.ui_translator import (
            find_canvas_nodes, convert_canvas, build_component_owner_index,
        )

        scene = Path("/Users/jiazou/workspace/trash-dash/Assets/Scenes/Main.unity")
        if not scene.exists():
            pytest.skip("trash-dash Main.unity not present in this env")
        from unity.scene_parser import parse_scene
        parsed = parse_scene(scene)

        owner = build_component_owner_index(parsed.roots)
        canvases = find_canvas_nodes(parsed.roots)
        clicks: list = []
        unsupported: list = []
        convert_canvas(
            canvases, scene_namespace=self.NS,
            component_owner_index=owner,
            click_bindings=clicks,
            unsupported_click_bindings=unsupported,
        )

        # The StartButton -> StartGame call (target comp 869760749, owned by
        # GO 869760744 = the Loadout GameObject) must emit a candidate binding.
        start_rows = [
            r for r in clicks
            if r["method"] == "StartGame"
            and r["target_component_id"] == f"{self.NS}:869760749"
        ]
        assert len(start_rows) == 1, (clicks, unsupported)
        row = start_rows[0]
        assert row["target_sri"] == f"{self.NS}:869760744"   # the Loadout GO

    def test_real_main_unity_static_arg_buttons_unsupported(self):
        """Real-corpus: the mode-6 (Bool static-arg) SetActive onClicks land in
        the unsupported accumulator at stage 1, never as ClickBinding rows."""
        import pytest
        from pathlib import Path
        from converter.ui_translator import (
            find_canvas_nodes, convert_canvas, build_component_owner_index,
        )

        scene = Path("/Users/jiazou/workspace/trash-dash/Assets/Scenes/Main.unity")
        if not scene.exists():
            pytest.skip("trash-dash Main.unity not present in this env")
        from unity.scene_parser import parse_scene
        parsed = parse_scene(scene)
        owner = build_component_owner_index(parsed.roots)
        canvases = find_canvas_nodes(parsed.roots)
        clicks: list = []
        unsupported: list = []
        convert_canvas(
            canvases, scene_namespace=self.NS,
            component_owner_index=owner,
            click_bindings=clicks,
            unsupported_click_bindings=unsupported,
        )
        static_arg = [u for u in unsupported if u["reason"] == "static_argument"]
        # The About/BackButton SetActive(bool) onClicks are mode 6 -> static-arg.
        assert any(u["method"] == "SetActive" for u in static_arg), unsupported


class TestClickBindingProductionDomainWiring:
    """AMENDMENT r3 (deferred two-stage domain gate): the domain feeding the
    onClick gate is applied in a POST-classification reclassification step
    (``Pipeline._reclassify_click_bindings_by_domain``), NOT at ``convert_scene``
    time -- because the real pipeline stamps
    ``scene_runtime["modules"][*]["domain"]`` only AFTER ``convert_scene`` runs.

    These tests reproduce the REAL pipeline ORDERING, never a seeded-modules-at-
    convert_scene tautology: stage 1 (``convert_scene`` with NO domains in the
    modules map) emits a domain-agnostic CANDIDATE; stage 2 (the reclassifier,
    fed the classified modules map) re-gates it. A client target stays in
    ``ui_click_bindings``; a server target moves to
    ``rbx_place.unsupported_onclick_bindings``. (Fully end-to-end through the
    storage classifier is impractical in a unit test -- it needs the whole
    transpile/classify pipeline -- so we feed the modules map in its classified
    shape and assert the reclassifier, NOT a seeded-at-convert value, gates.)
    """

    def _stage1_emit(self, *, project_root, target_guid):
        """Stage 1: run ``convert_scene`` with an UNCLASSIFIED modules map (NO
        domains). Returns ``(place, parsed, scene_runtime)``. The candidate
        ClickBinding must emit regardless of domain (domain deferred)."""
        from converter.scene_converter import convert_scene
        parsed = self._scene(project_root=project_root, target_guid=target_guid)
        # Modules present (so the stage-2 gate is open) but domain UNSET at
        # convert_scene time -- exactly the real pipeline ordering (the domain is
        # stamped later by _classify_storage).
        scene_runtime: dict = {"modules": {target_guid: {}}}
        place = convert_scene(
            parsed_scene=parsed,
            unity_project_root=project_root,
            scene_runtime=scene_runtime,
            scene_runtime_mode="generic",
        )
        return place, parsed, scene_runtime

    def _reclassify(self, *, project_root, place, parsed, scene_runtime):
        """Stage 2: drive the REAL ``Pipeline._reclassify_click_bindings_by_domain``
        against the now-classified modules map (domains stamped)."""
        from converter.pipeline import Pipeline
        pipe = Pipeline(unity_project_path=project_root, output_dir=project_root / "out")
        pipe.state.rbx_place = place
        pipe.state.parsed_scene = parsed
        pipe.ctx.scene_runtime = scene_runtime
        pipe.ctx.scene_runtime_mode = "generic"
        pipe._reclassify_click_bindings_by_domain()

    def test_candidate_emits_at_stage1_without_domain(self, tmp_path):
        """Stage 1 (real ordering): ``convert_scene`` emits a candidate
        ClickBinding even though the modules map carries NO domain yet. RED
        against the pre-r3 code, which domain-gated at convert_scene and so
        emitted ZERO bindings (every client target fell to unresolved_target)."""
        project_root = tmp_path / "proj"
        (project_root / "Assets" / "Scenes").mkdir(parents=True)
        target_guid = "abc123def456abc123def456abc12345"
        place, parsed, scene_runtime = self._stage1_emit(
            project_root=project_root, target_guid=target_guid,
        )
        rows = scene_runtime.get("ui_click_bindings")
        assert isinstance(rows, list) and len(rows) == 1, scene_runtime
        ns = "Assets/Scenes/Main.unity"
        assert rows[0]["method"] == "StartGame"
        assert rows[0]["target_component_id"] == f"{ns}:710"
        # No domain consulted -> nothing in the unsupported report yet.
        assert not place.unsupported_onclick_bindings

    def test_client_target_survives_reclassification(self, tmp_path):
        """Acceptance (real ordering): convert_scene with NO seeded domains, THEN
        the reclassifier fed the REAL classified modules (client). The StartGame
        binding STAYS in ``ui_click_bindings`` and is NOT moved to unsupported.
        RED against the current pre-r3 code (dropped at convert_scene)."""
        project_root = tmp_path / "proj"
        (project_root / "Assets" / "Scenes").mkdir(parents=True)
        target_guid = "abc123def456abc123def456abc12345"
        place, parsed, scene_runtime = self._stage1_emit(
            project_root=project_root, target_guid=target_guid,
        )
        # The classifier stamps the module ``client`` (mutates the modules map
        # in place, exactly as _classify_storage does).
        scene_runtime["modules"][target_guid]["domain"] = "client"
        self._reclassify(
            project_root=project_root, place=place,
            parsed=parsed, scene_runtime=scene_runtime,
        )
        rows = scene_runtime.get("ui_click_bindings")
        assert isinstance(rows, list) and len(rows) == 1, scene_runtime
        ns = "Assets/Scenes/Main.unity"
        assert rows[0]["method"] == "StartGame"
        assert rows[0]["target_component_id"] == f"{ns}:710"
        assert not place.unsupported_onclick_bindings

    def test_server_target_moved_to_unsupported_at_stage2(self, tmp_path):
        """The mirror: convert_scene emits a candidate; the reclassifier fed a
        ``server``-classified module MOVES it out of ``ui_click_bindings`` to
        ``rbx_place.unsupported_onclick_bindings`` (reason ``domain_server``)."""
        project_root = tmp_path / "proj"
        (project_root / "Assets" / "Scenes").mkdir(parents=True)
        target_guid = "fff000fff000fff000fff000fff00012"
        place, parsed, scene_runtime = self._stage1_emit(
            project_root=project_root, target_guid=target_guid,
        )
        # Candidate present after stage 1.
        assert scene_runtime.get("ui_click_bindings")
        scene_runtime["modules"][target_guid]["domain"] = "server"
        self._reclassify(
            project_root=project_root, place=place,
            parsed=parsed, scene_runtime=scene_runtime,
        )
        assert not scene_runtime.get("ui_click_bindings"), scene_runtime
        unsupported = place.unsupported_onclick_bindings
        assert len(unsupported) == 1
        assert unsupported[0]["reason"] == "domain_server"
        assert unsupported[0]["method"] == "StartGame"
        assert unsupported[0]["target_file_id"] == "710"

    def _scene(self, *, project_root, target_guid):
        from pathlib import Path
        # GameObject owning the target component (m_Script = target_guid).
        target_go = SceneNode(
            name="LoadoutState", file_id="700", active=True, layer=0, tag="Untagged",
            components=[ComponentData(
                component_type="MonoBehaviour", file_id="710",
                properties={"m_Script": {"guid": target_guid, "fileID": "11500000"}},
            )],
            children=[], parent_file_id=None,
        )
        # Button GameObject whose onClick targets component 710 / method StartGame.
        button_go = SceneNode(
            name="StartButton", file_id="800", active=True, layer=0, tag="Untagged",
            components=[ComponentData(
                component_type="MonoBehaviour", file_id="810",
                properties={"m_OnClick": {"m_PersistentCalls": {"m_Calls": [{
                    "m_MethodName": "StartGame", "m_CallState": 2, "m_Mode": 1,
                    "m_Target": {"fileID": "710"},
                }]}}},
            )],
            children=[], parent_file_id="900",
        )
        canvas = SceneNode(
            name="Canvas", file_id="900", active=True, layer=0, tag="Untagged",
            components=[ComponentData(
                component_type="Canvas", file_id="910", properties={"m_Enabled": 1},
            )],
            children=[button_go], parent_file_id=None,
        )
        from core.unity_types import ParsedScene
        return ParsedScene(
            scene_path=Path(project_root) / "Assets" / "Scenes" / "Main.unity",
            roots=[canvas, target_go],
        )

    def test_stage1_emits_candidate_even_for_server_module(self, tmp_path):
        """Even with the modules map already carrying ``server``, stage 1
        (``convert_scene``) emits the candidate -- proving the domain is NOT
        consulted at convert_scene time. The move-to-unsupported happens only in
        stage 2 (``test_server_target_moved_to_unsupported_at_stage2``)."""
        from converter.scene_converter import convert_scene

        project_root = tmp_path / "proj"
        (project_root / "Assets" / "Scenes").mkdir(parents=True)
        target_guid = "fff000fff000fff000fff000fff00012"
        parsed = self._scene(project_root=project_root, target_guid=target_guid)
        scene_runtime: dict = {
            "modules": {target_guid: {"domain": "server"}},
        }
        place = convert_scene(
            parsed_scene=parsed,
            unity_project_root=project_root,
            scene_runtime=scene_runtime,
            scene_runtime_mode="generic",
        )
        # convert_scene emits the candidate regardless of domain (deferred gate).
        rows = scene_runtime.get("ui_click_bindings")
        assert isinstance(rows, list) and len(rows) == 1, scene_runtime
        assert rows[0]["method"] == "StartGame"
        assert not place.unsupported_onclick_bindings


class TestSliderFillElement:
    """Slider fill-path producer (slice 1.1) — criteria 1-4.

    Validates the MonoBehaviour-serialized Slider dispatch (D-1), the
    ``SliderFillElement`` relative-path emission, the abstain paths, and the
    pure ``_relative_fill_path`` helper.
    """

    @staticmethod
    def _node(
        name: str, file_id: str,
        parent_file_id: str | None = None,
        components: list[ComponentData] | None = None,
        children: list[SceneNode] | None = None,
    ) -> SceneNode:
        return SceneNode(
            name=name,
            file_id=file_id,
            active=True,
            layer=0,
            tag="Untagged",
            components=components or [],
            children=children or [],
            parent_file_id=parent_file_id,
        )

    def _simplefps_tree(self):
        """Build a SimpleFPS-shaped tree:

            Health (slider GO, MonoBehaviour w/ m_FillRect -> RectTransform 'rt')
              Back
                CurHealth   (the fill GO; owns RectTransform 'rt')

        Returns (health_node, node_index, component_owner_index).
        """
        from converter.ui_translator import build_component_owner_index

        # CurHealth GO owns the RectTransform component 'rt' (the m_FillRect target).
        cur_health = self._node(
            "CurHealth", file_id="cur", parent_file_id="back",
            components=[
                ComponentData(component_type="RectTransform", file_id="rt", properties={}),
            ],
        )
        back = self._node(
            "Back", file_id="back", parent_file_id="health",
            children=[cur_health],
        )
        health = self._node(
            "Health", file_id="health", parent_file_id=None,
            components=[
                ComponentData(
                    component_type="MonoBehaviour", file_id="slidercomp",
                    properties={
                        "m_FillRect": {"fileID": "rt"},
                        "m_Direction": 0,
                        "m_MinValue": 0,
                        "m_MaxValue": 1,
                        "m_Value": 1,
                    },
                ),
            ],
            children=[back],
        )
        node_index = {n.file_id: n for n in (health, back, cur_health)}
        component_owner_index = build_component_owner_index([health])
        return health, node_index, component_owner_index

    # --- Criterion 1: writer fires on the MonoBehaviour-serialized Slider. ---
    def test_writer_fires_on_monobehaviour_slider(self):
        from converter.ui_translator import _apply_slider_properties
        from core.roblox_types import RbxUIElement

        health, node_index, owner_index = self._simplefps_tree()
        element = RbxUIElement(class_name="Frame", name="Health")
        _apply_slider_properties(
            element, health.components[0].properties,
            node=health,
            component_owner_index=owner_index,
            node_index=node_index,
        )
        assert element.attributes["SliderFillElement"] == "Back/CurHealth"

    def test_dispatch_fires_via_convert_ui_element(self):
        """End-to-end through the dispatch: a MonoBehaviour Slider (no literal
        ``component_type=='Slider'``) gets the attribute via ``_convert_ui_element``."""
        from converter.ui_translator import _convert_ui_element, build_component_owner_index

        health, node_index, _ = self._simplefps_tree()
        owner_index = build_component_owner_index([health])
        element = _convert_ui_element(
            health, scene_namespace="",
            component_owner_index=owner_index,
            node_index=node_index,
        )
        assert element is not None
        assert element.attributes["SliderFillElement"] == "Back/CurHealth"

    # --- Criterion 2: Min/Max/Value/Direction still emitted (no regression). ---
    def test_writer_still_emits_minmax_value_direction(self):
        from converter.ui_translator import _apply_slider_properties
        from core.roblox_types import RbxUIElement

        health, node_index, owner_index = self._simplefps_tree()
        element = RbxUIElement(class_name="Frame", name="Health")
        _apply_slider_properties(
            element, health.components[0].properties,
            node=health,
            component_owner_index=owner_index,
            node_index=node_index,
        )
        assert element.attributes["MinValue"] == 0.0
        assert element.attributes["MaxValue"] == 1.0
        assert element.attributes["Value"] == 1.0
        assert element.attributes["SliderDirection"] == 0

    # --- Criterion 3: writer abstains correctly. ---
    def test_abstain_on_zero_fillrect(self):
        from converter.ui_translator import _apply_slider_properties
        from core.roblox_types import RbxUIElement

        health, node_index, owner_index = self._simplefps_tree()
        props = dict(health.components[0].properties)
        props["m_FillRect"] = {"fileID": "0"}
        element = RbxUIElement(class_name="Frame", name="Health")
        _apply_slider_properties(
            element, props, node=health,
            component_owner_index=owner_index, node_index=node_index,
        )
        assert "SliderFillElement" not in element.attributes
        # Min/Max still emitted.
        assert element.attributes["MinValue"] == 0.0

    def test_abstain_on_non_descendant_fill(self):
        from converter.ui_translator import _apply_slider_properties, build_component_owner_index
        from core.roblox_types import RbxUIElement

        # Fill GO is a sibling root, NOT a descendant of the slider.
        outside = self._node(
            "Outside", file_id="out", parent_file_id=None,
            components=[
                ComponentData(component_type="RectTransform", file_id="rt", properties={}),
            ],
        )
        health = self._node(
            "Health", file_id="health", parent_file_id=None,
            components=[
                ComponentData(
                    component_type="MonoBehaviour", file_id="slidercomp",
                    properties={"m_FillRect": {"fileID": "rt"}, "m_MinValue": 0, "m_MaxValue": 1},
                ),
            ],
        )
        node_index = {n.file_id: n for n in (health, outside)}
        owner_index = build_component_owner_index([health, outside])
        element = RbxUIElement(class_name="Frame", name="Health")
        _apply_slider_properties(
            element, health.components[0].properties,
            node=health, component_owner_index=owner_index, node_index=node_index,
        )
        assert "SliderFillElement" not in element.attributes
        assert element.attributes["MinValue"] == 0.0

    def test_abstain_on_legacy_caller(self):
        """No indices threaded (legacy/synthetic) -> no SliderFillElement, but
        Min/Max still emitted."""
        from converter.ui_translator import _apply_slider_properties
        from core.roblox_types import RbxUIElement

        element = RbxUIElement(class_name="Frame", name="Health")
        _apply_slider_properties(
            element,
            {"m_FillRect": {"fileID": "rt"}, "m_MinValue": 0, "m_MaxValue": 1},
        )
        assert "SliderFillElement" not in element.attributes
        assert element.attributes["MinValue"] == 0.0

    # --- Criterion 4: _relative_fill_path is grandchild-correct. ---
    def test_relative_fill_path_direct_child(self):
        from converter.ui_translator import _relative_fill_path

        slider = self._node("Health", file_id="health")
        fill = self._node("CurHealth", file_id="cur", parent_file_id="health")
        node_index = {"health": slider, "cur": fill}
        assert _relative_fill_path(slider, "cur", node_index) == "CurHealth"

    def test_relative_fill_path_grandchild(self):
        from converter.ui_translator import _relative_fill_path

        slider = self._node("Health", file_id="health")
        back = self._node("Back", file_id="back", parent_file_id="health")
        fill = self._node("CurHealth", file_id="cur", parent_file_id="back")
        node_index = {"health": slider, "back": back, "cur": fill}
        assert _relative_fill_path(slider, "cur", node_index) == "Back/CurHealth"

    def test_relative_fill_path_non_descendant_returns_none(self):
        from converter.ui_translator import _relative_fill_path

        slider = self._node("Health", file_id="health")
        # Fill chains up to a different root, never hitting the slider.
        other = self._node("Other", file_id="other", parent_file_id=None)
        fill = self._node("CurHealth", file_id="cur", parent_file_id="other")
        node_index = {"health": slider, "other": other, "cur": fill}
        assert _relative_fill_path(slider, "cur", node_index) is None

    def test_relative_fill_path_cycle_returns_none(self):
        from converter.ui_translator import _relative_fill_path

        slider = self._node("Health", file_id="health")
        # a -> b -> a cycle that never reaches the slider; bounded loop -> None.
        a = self._node("A", file_id="a", parent_file_id="b")
        b = self._node("B", file_id="b", parent_file_id="a")
        node_index = {"health": slider, "a": a, "b": b}
        assert _relative_fill_path(slider, "a", node_index) is None

    def test_relative_fill_path_fill_is_slider_returns_none(self):
        from converter.ui_translator import _relative_fill_path

        slider = self._node("Health", file_id="health")
        node_index = {"health": slider}
        # The fill GO IS the slider -> no descendant segments -> None.
        assert _relative_fill_path(slider, "health", node_index) is None
