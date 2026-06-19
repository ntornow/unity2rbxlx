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
