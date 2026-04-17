"""
ui_translator.py -- Convert Unity Canvas UI hierarchy to Roblox ScreenGui.

Handles Canvas -> ScreenGui, RectTransform -> Frame with UDim2 positioning,
Text/TextMeshPro -> TextLabel, Image -> ImageLabel, Button -> TextButton.
"""

from __future__ import annotations

import logging
from typing import Any

from core.unity_types import SceneNode
from core.roblox_types import RbxScreenGui, RbxUIElement

log = logging.getLogger(__name__)

# Unity UI component type -> Roblox class name mapping.
_UI_CLASS_MAP: dict[str, str] = {
    "Canvas": "ScreenGui",
    "Text": "TextLabel",
    "TextMeshProUGUI": "TextLabel",
    "TextMeshPro": "TextLabel",
    "TMP_Text": "TextLabel",
    "Image": "ImageLabel",
    "RawImage": "ImageLabel",
    "Button": "TextButton",
    "Toggle": "TextButton",
    "Slider": "Frame",
    "Scrollbar": "Frame",
    "InputField": "TextBox",
    "TMP_InputField": "TextBox",
    "Dropdown": "Frame",
    "TMP_Dropdown": "Frame",
    "ScrollRect": "ScrollingFrame",
}


def convert_canvas(canvas_nodes: list[SceneNode]) -> list[RbxScreenGui]:
    """Convert a list of Unity Canvas root nodes to Roblox ScreenGui objects.

    Each Canvas node becomes an RbxScreenGui with its children converted
    recursively into RbxUIElements.  If a CanvasScaler component is present,
    its reference resolution and scaling mode are stored as attributes on the
    ScreenGui so that a runtime script can apply responsive scaling.

    Args:
        canvas_nodes: List of SceneNode objects that have a Canvas component.

    Returns:
        List of RbxScreenGui objects.
    """
    results: list[RbxScreenGui] = []

    for canvas_node in canvas_nodes:
        screen_gui = RbxScreenGui(name=canvas_node.name)

        # Extract CanvasScaler settings and store as attributes.
        _apply_canvas_scaler(screen_gui, canvas_node)

        for child in canvas_node.children:
            element = _convert_ui_element(child)
            if element is not None:
                screen_gui.elements.append(element)

        results.append(screen_gui)
        log.info(
            "Converted Canvas '%s' with %d top-level elements",
            canvas_node.name,
            len(screen_gui.elements),
        )

    return results


# ---------------------------------------------------------------------------
# CanvasScaler conversion
# ---------------------------------------------------------------------------

# Unity CanvasScaler.ScaleMode enum values.
_SCALE_MODE_CONSTANT_PIXEL = 0
_SCALE_MODE_SCALE_WITH_SCREEN = 1
_SCALE_MODE_CONSTANT_PHYSICAL = 2


def _apply_canvas_scaler(screen_gui: RbxScreenGui, canvas_node: SceneNode) -> None:
    """Extract CanvasScaler settings and store as attributes on the ScreenGui.

    Unity CanvasScaler modes:
        0 (ConstantPixelSize)    -> No scaling; pixel offsets used as-is.
        1 (ScaleWithScreenSize)  -> Scale UI relative to a reference resolution.
                                    Stores ReferenceResolutionX, ReferenceResolutionY,
                                    and MatchWidthOrHeight as ScreenGui attributes
                                    so a runtime Luau script can apply responsive
                                    scaling via UIScale.
        2 (ConstantPhysicalSize) -> Treat like ConstantPixelSize (no DPI info
                                    available in Roblox).

    Args:
        screen_gui: The RbxScreenGui to annotate with attributes.
        canvas_node: The SceneNode that owns the Canvas (and possibly CanvasScaler).
    """
    for comp in canvas_node.components:
        if comp.component_type != "CanvasScaler":
            continue

        props = comp.properties
        scale_mode = int(props.get("m_UiScaleMode", _SCALE_MODE_CONSTANT_PIXEL))

        if scale_mode == _SCALE_MODE_SCALE_WITH_SCREEN:
            ref_res = props.get("m_ReferenceResolution", {})
            if isinstance(ref_res, dict):
                ref_x = float(ref_res.get("x", 1920))
                ref_y = float(ref_res.get("y", 1080))
            else:
                ref_x, ref_y = 1920.0, 1080.0

            match_val = float(props.get("m_MatchWidthOrHeight", 0.0))

            screen_gui.attributes["_ScaleMode"] = "ScaleWithScreenSize"
            screen_gui.attributes["_ReferenceResolutionX"] = ref_x
            screen_gui.attributes["_ReferenceResolutionY"] = ref_y
            screen_gui.attributes["_MatchWidthOrHeight"] = match_val

            log.info(
                "CanvasScaler on '%s': ScaleWithScreenSize ref=%dx%d match=%.2f",
                canvas_node.name, int(ref_x), int(ref_y), match_val,
            )

        elif scale_mode == _SCALE_MODE_CONSTANT_PHYSICAL:
            # No DPI information in Roblox; treat like constant pixel size.
            screen_gui.attributes["_ScaleMode"] = "ConstantPhysicalSize"
            log.info(
                "CanvasScaler on '%s': ConstantPhysicalSize (treated as no scaling)",
                canvas_node.name,
            )

        else:
            # ConstantPixelSize or unknown -- no extra attributes needed.
            screen_gui.attributes["_ScaleMode"] = "ConstantPixelSize"

        # Only process the first CanvasScaler found.
        break


def _convert_ui_element(node: SceneNode) -> RbxUIElement | None:
    """Recursively convert a SceneNode (under a Canvas) to an RbxUIElement.

    Determines the Roblox class based on attached UI components
    (Text, Image, Button, etc.) and extracts relevant properties.

    Args:
        node: A SceneNode that is a child of a Canvas.

    Returns:
        An RbxUIElement, or None if the node should be skipped.
    """
    if not node.active:
        return None

    # Determine element class from components.
    element_class = "Frame"  # Default to Frame if no specific UI component.
    ui_properties: dict[str, Any] = {}

    for comp in node.components:
        ct = comp.component_type

        if ct in _UI_CLASS_MAP:
            element_class = _UI_CLASS_MAP[ct]
            ui_properties.update(comp.properties)

        # RectTransform provides position/size.
        if ct == "RectTransform":
            ui_properties["_rect_transform"] = comp.properties

    # Check MonoBehaviour for TMP/Text properties.
    for comp in node.components:
        if comp.component_type == "MonoBehaviour":
            props = comp.properties
            if "m_Text" in props or "m_text" in props:
                if element_class == "Frame":
                    element_class = "TextLabel"
                ui_properties.update(props)
            if "m_Sprite" in props:
                if element_class == "Frame":
                    element_class = "ImageLabel"
                ui_properties.update(props)

    # Check for Button component override and extract onClick handlers.
    # Unity Button is a MonoBehaviour — identified by m_OnClick or m_Interactable fields.
    on_click_handlers = []
    for comp in node.components:
        is_button = (
            "Button" in comp.component_type or
            ("MonoBehaviour" in comp.component_type and "m_OnClick" in comp.properties)
        )
        if is_button:
            element_class = "TextButton"
            # Extract m_OnClick persistent calls
            on_click = comp.properties.get("m_OnClick", {})
            persistent_calls = on_click.get("m_PersistentCalls", {})
            calls = persistent_calls.get("m_Calls", [])
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict):
                        method = call.get("m_MethodName", "")
                        call_state = int(call.get("m_CallState", 0))
                        if method and call_state >= 2:  # RuntimeOnly or EditorAndRuntime
                            target_ref = call.get("m_Target", {})
                            target_id = target_ref.get("fileID", "") if isinstance(target_ref, dict) else ""
                            on_click_handlers.append({
                                "method": method,
                                "target_file_id": str(target_id),
                            })
                            log.info("  [%s] Button onClick: %s (target %s)", node.name, method, target_id)

    # Extract position and size from RectTransform.
    position, size = _extract_rect_transform(
        ui_properties.get("_rect_transform", {}),
    )

    # Create the element.
    element = RbxUIElement(
        class_name=element_class,
        name=node.name,
        position=position,
        size=size,
        visible=node.active,
        on_click_handlers=on_click_handlers,
    )
    # Store onClick method names as attributes for script wiring
    if on_click_handlers:
        methods = ",".join(h["method"] for h in on_click_handlers)
        element.attributes["_OnClick"] = methods

    # Extract type-specific properties.
    if element_class in ("TextLabel", "TextButton"):
        _apply_text_properties(element, ui_properties, size)

    elif element_class == "ImageLabel":
        _apply_image_properties(element, ui_properties)

    elif element_class == "TextBox":
        _apply_text_properties(element, ui_properties, size)

    elif element_class == "ScrollingFrame":
        _apply_scroll_properties(element, ui_properties)

    # Extract UI widget properties — these store values/config as
    # attributes so runtime scripts can read them.
    for comp in node.components:
        ct = comp.component_type
        if ct == "Slider":
            _apply_slider_properties(element, comp.properties)
        elif ct in ("InputField", "TMP_InputField"):
            _apply_inputfield_properties(element, comp.properties)
        elif ct in ("Dropdown", "TMP_Dropdown"):
            _apply_dropdown_properties(element, comp.properties)
        elif ct == "Toggle":
            _apply_toggle_properties(element, comp.properties)

    # Extract background color.
    _apply_color_properties(element, ui_properties)

    # Detect layout group components.
    _apply_layout_properties(element, node.components)

    # Recurse into children.
    for child_node in node.children:
        child_element = _convert_ui_element(child_node)
        if child_element is not None:
            element.children.append(child_element)

    return element


# ---------------------------------------------------------------------------
# RectTransform -> UDim2 conversion
# ---------------------------------------------------------------------------

def _extract_rect_transform(
    rt_props: dict[str, Any],
) -> tuple[tuple[float, float, float, float], tuple[float, float, float, float]]:
    """Convert Unity RectTransform anchors/offsets to Roblox UDim2 position and size.

    Unity RectTransform uses:
        - anchorMin/anchorMax: normalized (0-1) anchor positions
        - offsetMin/offsetMax: pixel offsets from anchors
        - anchoredPosition: position relative to anchors
        - sizeDelta: size adjustment

    Roblox UDim2 uses:
        - Scale (0-1) + Offset (pixels) per axis

    Returns:
        Tuple of (position_udim2, size_udim2) where each is
        (scale_x, offset_x, scale_y, offset_y).
    """
    if not rt_props:
        return (0, 0, 0, 0), (1, 0, 1, 0)

    # Anchor min/max.
    anchor_min = rt_props.get("m_AnchorMin", {})
    anchor_max = rt_props.get("m_AnchorMax", {})
    anchored_pos = rt_props.get("m_AnchoredPosition", {})
    size_delta = rt_props.get("m_SizeDelta", {})
    pivot = rt_props.get("m_Pivot", {})

    # Parse values.
    amin_x = float(anchor_min.get("x", 0)) if isinstance(anchor_min, dict) else 0
    amin_y = float(anchor_min.get("y", 0)) if isinstance(anchor_min, dict) else 0
    amax_x = float(anchor_max.get("x", 1)) if isinstance(anchor_max, dict) else 1
    amax_y = float(anchor_max.get("y", 1)) if isinstance(anchor_max, dict) else 1

    apos_x = float(anchored_pos.get("x", 0)) if isinstance(anchored_pos, dict) else 0
    apos_y = float(anchored_pos.get("y", 0)) if isinstance(anchored_pos, dict) else 0

    sd_x = float(size_delta.get("x", 0)) if isinstance(size_delta, dict) else 0
    sd_y = float(size_delta.get("y", 0)) if isinstance(size_delta, dict) else 0

    pivot_x = float(pivot.get("x", 0.5)) if isinstance(pivot, dict) else 0.5
    pivot_y = float(pivot.get("y", 0.5)) if isinstance(pivot, dict) else 0.5

    # If anchors are the same point -> absolute positioning.
    if abs(amin_x - amax_x) < 0.001 and abs(amin_y - amax_y) < 0.001:
        # Size is from sizeDelta.
        size_scale_x = 0.0
        size_offset_x = sd_x
        size_scale_y = 0.0
        size_offset_y = sd_y

        # Position is anchor + offset - (pivot * size).
        pos_scale_x = amin_x
        pos_offset_x = apos_x - pivot_x * sd_x
        pos_scale_y = 1.0 - amin_y  # Unity Y is bottom-up; Roblox is top-down.
        pos_offset_y = -(apos_y + (1.0 - pivot_y) * sd_y)
    else:
        # Stretched mode -> size comes from anchor difference.
        size_scale_x = amax_x - amin_x
        size_offset_x = sd_x
        size_scale_y = amax_y - amin_y
        size_offset_y = sd_y

        # Position from anchor min.
        pos_scale_x = amin_x
        pos_offset_x = apos_x
        pos_scale_y = 1.0 - amax_y  # Flip Y.
        pos_offset_y = -apos_y

    position = (pos_scale_x, pos_offset_x, pos_scale_y, pos_offset_y)
    size = (size_scale_x, size_offset_x, size_scale_y, size_offset_y)

    return position, size


# ---------------------------------------------------------------------------
# Property extractors
# ---------------------------------------------------------------------------

def _apply_text_properties(
    element: RbxUIElement,
    props: dict[str, Any],
    size: tuple[float, float, float, float] = (0, 0, 0, 0),
) -> None:
    """Extract text properties from Unity Text/TMP components."""
    # Text content.
    text = props.get("m_Text", props.get("m_text", ""))
    if isinstance(text, str):
        element.text = text

    # Font size.
    font_size = props.get("m_FontSize", props.get("m_fontSize", 0))
    try:
        fs = int(float(font_size))
    except (TypeError, ValueError):
        fs = 0

    if fs > 0:
        element.text_size = fs
    else:
        # No font size specified: use the element's pixel height if available,
        # or fall back to 14.  This handles "Best Fit" mode and crosshair-type
        # elements where the text fills the element.
        pixel_height = int(size[3]) if size[3] else 0  # YO (offset Y)
        if pixel_height > 0:
            element.text_size = pixel_height
        else:
            element.text_size = 14

    # Text color.
    color = props.get("m_Color", props.get("m_fontColor", {}))
    if isinstance(color, dict):
        element.text_color = (
            float(color.get("r", 0)),
            float(color.get("g", 0)),
            float(color.get("b", 0)),
        )

    # Background transparency (text elements are usually transparent).
    element.background_transparency = 1.0


def _apply_image_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract image properties from Unity Image/RawImage components."""
    # Sprite/texture reference (GUID needs external resolution).
    sprite_ref = props.get("m_Sprite", props.get("m_Texture", {}))
    if isinstance(sprite_ref, dict):
        guid = sprite_ref.get("guid", "")
        # Skip null/empty GUIDs (Unity built-in default material)
        if guid and guid != "0000000000000000f000000000000000":
            element.image = f"guid://{guid}"

    # Color tint.
    color = props.get("m_Color", {})
    if isinstance(color, dict):
        element.background_color = (
            float(color.get("r", 1)),
            float(color.get("g", 1)),
            float(color.get("b", 1)),
        )
        alpha = float(color.get("a", 1.0))
        element.background_transparency = 1.0 - alpha


def _apply_scroll_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract scroll properties from Unity ScrollRect → Roblox ScrollingFrame."""
    horizontal = props.get("m_Horizontal", 1)
    vertical = props.get("m_Vertical", 1)
    if not hasattr(element, 'attributes'):
        element.attributes = {}
    element.attributes["_ScrollHorizontal"] = bool(int(horizontal))
    element.attributes["_ScrollVertical"] = bool(int(vertical))


def _apply_slider_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract Slider properties as attributes for runtime scripts."""
    if not hasattr(element, 'attributes'):
        element.attributes = {}
    for key in ("m_MinValue", "m_MaxValue", "m_Value", "m_WholeNumbers"):
        val = props.get(key)
        if val is not None:
            attr_name = key[2:]  # Strip m_ prefix
            try:
                element.attributes[attr_name] = float(val)
            except (TypeError, ValueError):
                pass
    # Direction: 0=LeftToRight, 1=RightToLeft, 2=BottomToTop, 3=TopToBottom
    direction = props.get("m_Direction", 0)
    try:
        element.attributes["SliderDirection"] = int(direction)
    except (TypeError, ValueError):
        pass


def _apply_inputfield_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract InputField/TMP_InputField properties."""
    text = props.get("m_Text", props.get("m_text", ""))
    if isinstance(text, str) and text:
        element.text = text
    placeholder = props.get("m_Placeholder", {})
    if isinstance(placeholder, dict):
        # The placeholder is a reference to a child Text component
        # Store the reference for post-processing
        if not hasattr(element, 'attributes'):
            element.attributes = {}
        element.attributes["_PlaceholderRef"] = str(placeholder.get("fileID", ""))
    char_limit = props.get("m_CharacterLimit", 0)
    try:
        cl = int(char_limit)
        if cl > 0:
            if not hasattr(element, 'attributes'):
                element.attributes = {}
            element.attributes["_CharacterLimit"] = cl
    except (TypeError, ValueError):
        pass


def _apply_dropdown_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract Dropdown/TMP_Dropdown properties as attributes."""
    if not hasattr(element, 'attributes'):
        element.attributes = {}
    value = props.get("m_Value", 0)
    try:
        element.attributes["DropdownValue"] = int(value)
    except (TypeError, ValueError):
        pass
    # Options are stored as m_Options.m_Options[].m_Text
    options = props.get("m_Options", {})
    if isinstance(options, dict):
        option_list = options.get("m_Options", [])
        if isinstance(option_list, list):
            texts = []
            for opt in option_list:
                if isinstance(opt, dict):
                    texts.append(opt.get("m_Text", ""))
            if texts:
                element.attributes["_DropdownOptions"] = ",".join(texts)


def _apply_toggle_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract Toggle properties."""
    if not hasattr(element, 'attributes'):
        element.attributes = {}
    is_on = props.get("m_IsOn", 1)
    try:
        element.attributes["ToggleIsOn"] = bool(int(is_on))
    except (TypeError, ValueError):
        pass


def _apply_color_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Apply background color from generic UI component color property."""
    # Only apply if not already set by a specific handler.
    if element.background_color == (1, 1, 1):
        color = props.get("m_Color", {})
        if isinstance(color, dict) and "r" in color:
            element.background_color = (
                float(color.get("r", 1)),
                float(color.get("g", 1)),
                float(color.get("b", 1)),
            )


def find_canvas_nodes(roots: list[SceneNode]) -> list[SceneNode]:
    """Walk a scene hierarchy and return all nodes that have a Canvas component.

    Useful for extracting UI hierarchies from a full scene parse.

    Args:
        roots: The root SceneNode list from a ParsedScene.

    Returns:
        List of SceneNodes that contain a Canvas component.
    """
    canvas_nodes: list[SceneNode] = []

    def _walk(node: SceneNode) -> None:
        for comp in node.components:
            if comp.component_type == "Canvas":
                canvas_nodes.append(node)
                return  # Don't recurse into Canvas children here.
        for child in node.children:
            _walk(child)

    for root in roots:
        _walk(root)

    return canvas_nodes


# ---------------------------------------------------------------------------
# Layout group conversion
# ---------------------------------------------------------------------------

def _apply_layout_properties(element: RbxUIElement, components: list) -> None:
    """Detect and apply Unity layout group components to the element.

    Maps:
        VerticalLayoutGroup   -> UIListLayout (FillDirection=Vertical)
        HorizontalLayoutGroup -> UIListLayout (FillDirection=Horizontal)
        GridLayoutGroup       -> UIGridLayout

    Args:
        element: The RbxUIElement to update.
        components: List of component objects on this node.
    """
    for comp in components:
        ct = comp.component_type
        props = comp.properties

        if ct == "VerticalLayoutGroup":
            element.layout_type = "UIListLayout"
            element.layout_direction = "Vertical"
            _extract_layout_spacing(element, props)

        elif ct == "HorizontalLayoutGroup":
            element.layout_type = "UIListLayout"
            element.layout_direction = "Horizontal"
            _extract_layout_spacing(element, props)

        elif ct == "GridLayoutGroup":
            element.layout_type = "UIGridLayout"
            cell_size = props.get("m_CellSize", {})
            if isinstance(cell_size, dict):
                element.layout_cell_size = (
                    int(float(cell_size.get("x", 100))),
                    int(float(cell_size.get("y", 100))),
                )
            _extract_layout_spacing(element, props)

            # Grid start axis → direction
            start_axis = int(props.get("m_StartAxis", 0))
            element.layout_direction = "Horizontal" if start_axis == 0 else "Vertical"

        # Also detect ContentSizeFitter and LayoutElement (just mark for reference)
        elif ct == "ContentSizeFitter":
            pass  # Roblox handles auto-sizing differently

        elif ct == "LayoutElement":
            pass  # Individual element layout hints — not directly mappable


def _extract_layout_spacing(element: RbxUIElement, props: dict) -> None:
    """Extract spacing and alignment from layout group properties."""
    # Spacing
    spacing = props.get("m_Spacing", 0)
    element.layout_padding = int(float(spacing))

    # Child alignment (Unity enum → Roblox alignment)
    alignment = int(props.get("m_ChildAlignment", 0))
    # Unity: 0=UpperLeft, 1=UpperCenter, 2=UpperRight, 3=MiddleLeft, etc.
    h_map = {0: "Left", 1: "Center", 2: "Right", 3: "Left", 4: "Center", 5: "Right",
             6: "Left", 7: "Center", 8: "Right"}
    v_map = {0: "Top", 1: "Top", 2: "Top", 3: "Center", 4: "Center", 5: "Center",
             6: "Bottom", 7: "Bottom", 8: "Bottom"}
    element.layout_h_alignment = h_map.get(alignment, "Left")
    element.layout_v_alignment = v_map.get(alignment, "Top")
