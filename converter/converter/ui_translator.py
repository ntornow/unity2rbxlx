"""
ui_translator.py -- Convert Unity Canvas UI hierarchy to Roblox ScreenGui.

Handles Canvas -> ScreenGui, RectTransform -> Frame with UDim2 positioning,
Text/TextMeshPro -> TextLabel, Image -> ImageLabel, Button -> TextButton.
"""

from __future__ import annotations

import logging
from typing import Any, TypedDict

from core.unity_types import SceneNode
from core.roblox_types import RbxScreenGui, RbxUIElement
from unity.yaml_parser import ref_guid

log = logging.getLogger(__name__)

# Attribute NAME the converter's Toggle-``isOn`` lowering writes (the
# transpiled HUD writer does ``toggle:SetAttribute("isOn", true)``). Single
# source of truth: ``_apply_toggle_properties`` stamps it onto each
# ``ToggleBinding`` row as ``attr_name`` and the runtime reads ``b.attr_name``
# (never a hard-coded literal). A convention-drift guard pins this against the
# contract corpus at converter-build time (test_ui_translator).
_TOGGLE_ISON_ATTR = "isOn"


class ToggleBinding(TypedDict):
    """One Unity ``Toggle`` -> checkmark-graphic visual binding record.

    Emitted by ``_apply_toggle_properties`` for a Toggle whose serialized
    ``graphic`` component reference resolves to an owning GameObject, and
    consumed by the runtime (``scene_runtime.luau``) to drive the checkmark's
    ``.Visible`` off the Toggle's ``isOn`` attribute. Strictly typed --
    str/bool only, NO ``Any``.
    """

    toggle_sri: str    # "<scene>:<toggle_go_fileID>" -- the Toggle GameObject's SRI
    graphic_sri: str   # "<scene>:<graphic_go_fileID>" -- the checkmark's SRI
    initial_on: bool   # from m_IsOn
    attr_name: str     # the isOn attribute NAME the writer uses (= _TOGGLE_ISON_ATTR)


class ClickBinding(TypedDict):
    """One Unity Button ``onClick`` -> target-component method invocation row.

    Mirrors ``ToggleBinding``: a deterministic build-time fact (keyed on the
    Unity ``m_OnClick`` persistent-call target component fileID + method name,
    NOT AI output) consumed by the runtime (``scene_runtime.luau``) to wire the
    Button's ``Activated`` signal to the converted component-instance method.

    Component-precise: ``target_component_id`` is the engine registry key for
    the EXACT target component instance (``"<scene>:<component_fileID>"`` --
    identical to the planner's per-instance ``instance_id``), so dispatch hits
    that component (preserving Unity semantics) rather than fanning out across
    every component on the GameObject. ``target_sri`` is the owning
    GameObject's SRI, used only for the provably-unambiguous GO-level fallback.
    Strictly typed -- str/int only, NO ``Any``.
    """

    button_sri: str           # "<scene>:<button_go_fileID>" -- the Button GameObject's SRI
    target_sri: str           # "<scene>:<target_go_fileID>" -- owning GameObject's SRI
    target_component_id: str  # "<scene>:<target_component_fileID>" -- engine registry key
    method: str               # the onClick method name (m_MethodName)
    call_index: int           # ordinal of this call within the Button's m_Calls list


class UnsupportedClickBinding(TypedDict):
    """One Button ``onClick`` call this version cannot honor, recorded LOUD.

    Surfaced in the conversion report (operator-inspectable) so a menu-critical
    dead button is visible at convert time. NEVER shipped to the host plan (the
    converter emits no no-op binding for it). ``reason`` is one of:
    ``"domain_server"`` / ``"domain_excluded"`` / ``"unresolved_target"`` /
    ``"static_argument"``.
    """

    button_sri: str           # the Button GameObject's SRI (or "" if unstamped)
    target_file_id: str       # the raw Unity target component fileID
    method: str               # the onClick method name
    reason: str               # why it could not be honored (see above)
    call_index: int           # ordinal of this call within the Button's m_Calls list


# Unity ``PersistentListenerMode`` (the ``m_Mode`` on each ``m_OnClick`` call):
#   0 EventDefined -- use the UnityEvent's own argument (a Button onClick is a
#                     no-argument UnityEvent, so this behaves like Void).
#   1 Void         -- call a no-argument method (the dispatchable case).
#   2 Object / 3 Int / 4 Float / 5 String / 6 Bool -- the call passes a STATIC
#                     argument captured in the scene. v1 dispatches no-arg
#                     methods only, so any ``m_Mode >= 2`` is recorded as
#                     ``static_argument`` (a named follow-on), not emitted.
# (The design doc's "static-argument == m_Mode==1" was a slip: real Trash-Dash
# StartButton->StartGame is ``m_Mode==1`` and MUST emit; mode-6 SetActive(bool)
# is the genuine static-arg case. Keyed on the real serialized values.)
_PERSISTENT_MODE_STATIC_ARG_MIN = 2

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

# Unity font name -> Roblox Font enum label. Unknown fonts fall back to
# SourceSans (the Roblox default) so legacy content still renders.
_FONT_MAP: dict[str, str] = {
    "Arial": "Arial",
    "Arial-Bold": "ArialBold",
    "ArialMT": "Arial",
    "Roboto": "Roboto",
    "Roboto-Bold": "Roboto",
    "LiberationSans": "SourceSans",
}

# Unity TextAnchor (0..8, row-major UpperLeft..LowerRight) split into
# horizontal + vertical Roblox TextAlignment tokens.
_TEXT_ANCHOR_X: dict[int, str] = {
    0: "Left",   1: "Center", 2: "Right",
    3: "Left",   4: "Center", 5: "Right",
    6: "Left",   7: "Center", 8: "Right",
}
_TEXT_ANCHOR_Y: dict[int, str] = {
    0: "Top",    1: "Top",    2: "Top",
    3: "Center", 4: "Center", 5: "Center",
    6: "Bottom", 7: "Bottom", 8: "Bottom",
}

# TextMeshPro HorizontalAlignmentOptions bitfield values mapped to Roblox
# TextXAlignment tokens. Justified/Flush/Geometry have no Roblox equivalent;
# Justified collapses to Left (extends from a left-aligned baseline) and
# Flush/Geometry collapse to Center (closer to their visual centroid).
_TMP_HORIZONTAL_BITS: dict[int, str] = {
    1: "Left",       # Left
    2: "Center",     # Center
    4: "Right",      # Right
    8: "Left",       # Justified -> Left (no Roblox equivalent)
    16: "Center",    # Flush -> Center
    32: "Center",    # Geometry -> Center
}

# TextMeshPro VerticalAlignmentOptions bitfield values mapped to Roblox
# TextYAlignment tokens. Baseline collapses to Bottom (baseline ≈ glyph
# bottom for non-descender text), Geometry to Center, Capline to Top.
_TMP_VERTICAL_BITS: dict[int, str] = {
    256: "Top",      # Top
    512: "Center",   # Middle
    1024: "Bottom",  # Bottom
    2048: "Bottom",  # Baseline -> Bottom
    4096: "Center",  # Geometry -> Center
    8192: "Top",     # Capline -> Top
}


def _coerce_int(raw: object) -> int | None:
    """Best-effort int coercion for serialized YAML scalars; None on failure.

    Accepts ``object`` because YAML scalars cross the parser boundary as
    untyped values (int, float, str, bool, None) — narrow inside the try.
    """
    try:
        return int(float(raw))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _map_tmp_bitfield(raw: object, table: dict[int, str]) -> str | None:
    """Resolve a TMP alignment bitfield int to a Roblox token.

    Direct lookup first; if absent, fall back to the lowest set bit so
    multi-flag values still classify (TMP only sets one flag per axis in
    practice, but YAML files written by some tools may OR extra bits).
    """
    value = _coerce_int(raw)
    if value is None or value <= 0:
        return None
    if value in table:
        return table[value]
    lowest_bit = value & -value
    return table.get(lowest_bit)

# UnityEngine.UI.Image script GUID — MonoBehaviours using this script are
# Image components that the scene parser can't distinguish by classID alone.
_UI_IMAGE_SCRIPT_GUID_PREFIX = "fe87c0e1cc204ed48ad3"


def _is_ui_image_mb(props: dict[str, Any]) -> bool:
    """Detect a MonoBehaviour-wrapped UnityEngine.UI.Image by its script GUID.

    Custom Image subclasses may omit m_Sprite from serialized fields while
    still rendering an image at runtime; matching the known Image script
    GUID is the fallback.
    """
    script = props.get("m_Script", {})
    if not isinstance(script, dict):
        return False
    guid = (ref_guid(script) or "")
    return isinstance(guid, str) and guid.startswith(_UI_IMAGE_SCRIPT_GUID_PREFIX)


def build_component_owner_index(roots: list[SceneNode]) -> dict[str, str]:
    """Map each component's fileID to its owning GameObject's fileID, scene-wide.

    Walks every node in ``roots`` and all of their ``children`` recursively;
    for each component on a node, records ``comp.file_id -> node.file_id``.
    Built scene-wide (not just the canvas subtree) because a serialized
    component reference (e.g. a Unity ``Toggle.graphic``) may legally point
    at a component owned by a GameObject outside the canvas subtree.

    Pure: builds and returns a fresh ``dict[str, str]``; no mutation of input.
    No name matching, no regex -- keyed solely on scene-local fileIDs.
    """
    index: dict[str, str] = {}
    stack: list[SceneNode] = list(roots)
    while stack:
        node = stack.pop()
        for comp in node.components:
            index[comp.file_id] = node.file_id
        stack.extend(node.children)
    return index


def _relative_fill_path(
    slider_node: SceneNode,
    fill_go_fid: str,
    node_index: dict[str, SceneNode],
) -> str | None:
    """Build the ``/``-separated relative descendant path from the slider
    GameObject to the fill GameObject, using converted Roblox instance NAMES.

    Walks the fill node's ``parent_file_id`` chain upward, prepending each
    ``node.name`` segment, until it reaches ``slider_node.file_id`` (success)
    or runs off the top of the tree / a missing node (failure -> ``None``).
    The returned path NEVER includes the slider node itself and has no
    leading/trailing slash; a direct-child fill yields a bare name
    (e.g. ``"CurHealth"``), a grandchild yields ``"Back/CurHealth"``.

    Returns ``None`` when the fill is not a descendant of the slider (chain
    hits a root, a missing fileID, or a cycle) so the writer abstains. The
    loop is bounded by ``len(node_index)`` to defeat a pathological cycle.

    Pure: reads ``node_index`` / ``slider_node`` only, mutates nothing.
    """
    fill_node = node_index.get(fill_go_fid)
    if fill_node is None:
        return None
    segments: list[str] = []
    current: SceneNode | None = fill_node
    # Bound the walk so a corrupt parent chain (cycle) can't loop forever.
    for _ in range(len(node_index) + 1):
        if current is None:
            return None
        if current.file_id == slider_node.file_id:
            # Reached the slider without ever recording it -> path is the
            # collected descendant segments. Empty means fill IS the slider.
            if not segments:
                return None
            return "/".join(segments)
        segments.insert(0, current.name)
        parent_fid = current.parent_file_id
        current = node_index.get(parent_fid) if parent_fid is not None else None
    return None


def build_component_domain_index(
    roots: list[SceneNode], module_domains: dict[str, str],
) -> dict[str, str]:
    """Map each MonoBehaviour component's fileID to its module domain, scene-wide.

    Walks every node recursively; for each component carrying an ``m_Script``
    GUID present in ``module_domains`` (the classified
    ``scene_runtime["modules"][<guid>]["domain"]`` map), records
    ``comp.file_id -> domain`` (``client`` / ``server`` / ``helper`` /
    ``excluded``). Components whose script GUID is absent (built-in components,
    unresolved scripts) are omitted -- the onClick domain gate then treats them
    as unresolvable.

    Pure: builds and returns a fresh ``dict[str, str]``; no input mutation. No
    name matching -- keyed solely on the deterministic ``m_Script`` GUID.
    """
    index: dict[str, str] = {}
    stack: list[SceneNode] = list(roots)
    while stack:
        node = stack.pop()
        for comp in node.components:
            script_ref = comp.properties.get("m_Script")
            if not isinstance(script_ref, dict):
                continue
            guid = ref_guid(script_ref) or ""
            domain = module_domains.get(guid)
            if domain:
                index[comp.file_id] = domain
        stack.extend(node.children)
    return index


def _canvas_enabled(canvas_node: SceneNode) -> bool:
    """ScreenGui.Enabled approximates the Unity render gate (activeInHierarchy AND Canvas.enabled).

    Unity renders a Canvas only when BOTH the GameObject is active in the hierarchy
    AND the Canvas component is enabled. We approximate this with the Canvas
    GameObject's OWN active-self (``canvas_node.active`` / the parsed ``m_IsActive``)
    AND the Canvas component's ``m_Enabled``. Each missing input defaults to True so
    absence never spuriously disables a canvas.

    LIMITATION (active-self, not true activeInHierarchy): we use the Canvas node's own
    ``m_IsActive``, NOT the full ancestor chain, so a Canvas authored active-self=True
    under an INACTIVE ANCESTOR ships ``Enabled=true`` even though Unity would hide it.
    This is intentional for now: ancestor active-state is not available at this call
    site (``convert_canvas`` receives only the flat canvas-node list), it matches the
    converter's existing element-level ``visible=node.active`` (active-self) model, and
    it is a strict improvement over the prior all-``Enabled=true`` behavior. Threading
    true activeInHierarchy through the caller is tracked as a follow-on.
    """
    active = bool(canvas_node.active)
    canvas_m_enabled = True
    for comp in canvas_node.components:
        if comp.component_type == "Canvas":
            raw = comp.properties.get("m_Enabled", 1)
            canvas_m_enabled = bool(raw) if isinstance(raw, (int, bool)) else True
            break
    return active and canvas_m_enabled


def convert_canvas(
    canvas_nodes: list[SceneNode],
    scene_namespace: str = "",
    scene_runtime_mode: str = "legacy",
    suppress_static_children_ids: frozenset[str] | None = None,
    component_owner_index: dict[str, str] | None = None,
    node_index: dict[str, SceneNode] | None = None,
    toggle_bindings: list[ToggleBinding] | None = None,
    click_bindings: list[ClickBinding] | None = None,
    unsupported_click_bindings: list[UnsupportedClickBinding] | None = None,
) -> list[RbxScreenGui]:
    """Convert a list of Unity Canvas root nodes to Roblox ScreenGui objects.

    Each Canvas node becomes an RbxScreenGui with its children converted
    recursively into RbxUIElements.  If a CanvasScaler component is present,
    its reference resolution and scaling mode are stored as attributes on the
    ScreenGui so that a runtime script can apply responsive scaling.

    Args:
        canvas_nodes: List of SceneNode objects that have a Canvas component.
        scene_namespace: Scene-runtime namespace string used as the
            ``_SceneRuntimeId`` prefix for stamped UI hosts. Empty string
            means stamping is skipped (synthetic / legacy callers).
        scene_runtime_mode: PR3c — the requested scene-runtime contract
            mode for this conversion. ``"generic"`` enables the
            serialized-field child-suppression carve-out (Piece 4); any
            other value (``"legacy"`` / ``"auto"`` / default) preserves
            the pre-PR3c static-emit behavior byte-identically. Legacy
            callers don't pass this and get the historical path.
        suppress_static_children_ids: PR3c — set of
            ``"<scene_namespace>:<file_id>"`` ids identifying UI
            GameObjects whose runtime-bearing controller has a serialized
            field referencing an asset / prefab. Under generic the static
            child tree under those elements is dropped; the host runtime
            instantiates the content via ``host.instantiatePrefab``.
            Empty / None means no suppression (the generic test fires on
            membership, so legacy passes ``None`` and gets the old path).
        component_owner_index: Scene-wide
            ``component fileID -> owning GameObject fileID`` map (built once
            per scene via ``build_component_owner_index``). Threaded down to
            element conversion so a serialized component reference can be
            resolved to its owning GameObject. ``None`` for legacy / synthetic
            callers (no resolution).
        toggle_bindings: By-ref accumulator. For each Toggle whose serialized
            ``graphic`` resolves (via ``component_owner_index``) to an owning
            GameObject, ``_apply_toggle_properties`` appends a ``ToggleBinding``
            row here. ``None`` for legacy / synthetic callers (no rows). The
            caller (``convert_scene``) creates the list, passes it by-ref, and
            stashes the populated result onto ``ctx.scene_runtime`` -- so the
            rows surface without riding the (unchanged) return value and without
            any transport attribute leaking onto a produced instance.
        click_bindings: By-ref ``ClickBinding`` accumulator (mirrors
            ``toggle_bindings``). For each dispatchable onClick call (no static
            arg, owner-resolvable target) ``_emit_click_bindings`` appends a
            CANDIDATE row -- the domain gate is deferred to a post-classification
            reclassification step (the domain isn't stamped yet at this phase).
            ``None`` for legacy callers.
        unsupported_click_bindings: By-ref accumulator for onClick calls this
            version cannot honor (server/excluded/unresolved target or static
            arg). Surfaced LOUD in the conversion report; never shipped to the
            host plan. ``None`` for legacy callers.

    Returns:
        List of RbxScreenGui objects.
    """
    results: list[RbxScreenGui] = []
    suppress_ids = suppress_static_children_ids or frozenset()
    suppression_active = (
        scene_runtime_mode == "generic" and bool(suppress_ids)
    )

    for canvas_node in canvas_nodes:
        screen_gui = RbxScreenGui(
            name=canvas_node.name,
            enabled=_canvas_enabled(canvas_node),
        )

        # Extract CanvasScaler settings and store as attributes.
        _apply_canvas_scaler(screen_gui, canvas_node)

        # Stamp the ScreenGui host with ``_SceneRuntimeId`` so the PR4
        # runtime can resolve a UI ``<scene>:<fileID>`` reference back to
        # this instance. UI element descendants get stamped inside
        # ``_convert_ui_element``.
        _stamp_scene_runtime_id(screen_gui, canvas_node.file_id, scene_namespace)

        for child in canvas_node.children:
            element = _convert_ui_element(
                child, scene_namespace,
                suppress_static_children_ids=(
                    suppress_ids if suppression_active else frozenset()
                ),
                component_owner_index=component_owner_index,
                node_index=node_index,
                toggle_bindings=toggle_bindings,
                click_bindings=click_bindings,
                unsupported_click_bindings=unsupported_click_bindings,
            )
            if element is not None:
                screen_gui.elements.append(element)

        results.append(screen_gui)
        log.info(
            "Converted Canvas '%s' with %d top-level elements",
            canvas_node.name,
            len(screen_gui.elements),
        )

    return results


def _stamp_scene_runtime_id(
    target: RbxScreenGui | RbxUIElement, file_id: str, scene_namespace: str,
) -> None:
    """Stamp ``_SceneRuntimeId`` onto a UI host (ScreenGui or descendant
    RbxUIElement).

    A no-op when either side of the ``<scene>:<file_id>`` value is empty —
    callers don't have to gate every stamp site. Mirrors the scene-side
    helper in ``scene_converter._stamp_scene_runtime_id``.
    """
    if not scene_namespace or not file_id:
        return
    target.attributes["_SceneRuntimeId"] = f"{scene_namespace}:{file_id}"


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


def _convert_ui_element(
    node: SceneNode, scene_namespace: str = "",
    suppress_static_children_ids: frozenset[str] = frozenset(),
    component_owner_index: dict[str, str] | None = None,
    node_index: dict[str, SceneNode] | None = None,
    toggle_bindings: list[ToggleBinding] | None = None,
    click_bindings: list[ClickBinding] | None = None,
    unsupported_click_bindings: list[UnsupportedClickBinding] | None = None,
) -> RbxUIElement | None:
    """Recursively convert a SceneNode (under a Canvas) to an RbxUIElement.

    Determines the Roblox class based on attached UI components
    (Text, Image, Button, etc.) and extracts relevant properties.

    Args:
        node: A SceneNode that is a child of a Canvas.
        scene_namespace: Scene-runtime namespace for ``_SceneRuntimeId``
            stamping; threaded through recursion so every descendant UI
            host gets stamped under the same ``<scene>:<fileID>`` scheme.
        suppress_static_children_ids: PR3c — UI GameObject ids
            (``"<scene_namespace>:<file_id>"``) whose runtime-bearing
            controller owns an asset / prefab serialized field. Under
            generic mode, those elements' static child trees are dropped
            (the host runtime instantiates them via
            ``host.instantiatePrefab``). Empty frozenset disables the
            carve-out — legacy mode passes empty, so child emit is
            byte-identical to pre-PR3c.
        component_owner_index: Scene-wide
            ``component fileID -> owning GameObject fileID`` map, threaded
            through recursion so a serialized component reference can be
            resolved to its owning GameObject. ``None`` for legacy / synthetic
            callers.
        toggle_bindings: By-ref ``ToggleBinding`` accumulator threaded through
            recursion; ``_apply_toggle_properties`` appends a row for each
            Toggle with a resolvable ``graphic``. ``None`` for legacy callers.

    Returns:
        An RbxUIElement, or None if the node should be skipped.

    Note on inactive nodes (Gap #4): an inactive Unity GameObject is NOT
    pruned. Unity inactive objects still EXIST and are commonly woken later
    by a script ``SetActive(true)`` (e.g. ``SettingPopup → AboutPopup``
    popups). Pruning the subtree dropped the ``_SceneRuntimeId``-stamped
    host clones the scene-runtime planner emits deferred-component rows
    against, so those rows dangled ("UI host clone … never landed"). We
    therefore EMIT the inactive subtree and KEEP RECURSING into children;
    the element is created with ``visible=node.active`` (below), so an
    inactive node lands hidden and the runtime ``_applyPlannerFlagsAndTag``
    keeps it inactive until a script wakes it.
    """
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
            if "m_Sprite" in props or _is_ui_image_mb(props):
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
                for call_index, call in enumerate(calls):
                    if isinstance(call, dict):
                        method = call.get("m_MethodName", "")
                        call_state = int(call.get("m_CallState", 0))
                        # Unity UnityEventCallState: Off=0, EditorAndRuntime=1,
                        # RuntimeOnly=2. BOTH EditorAndRuntime(1) and RuntimeOnly(2)
                        # invoke at runtime; only Off(0) is disabled. Accept >= 1 so
                        # EditorAndRuntime buttons are not silently dropped; an Off
                        # listener is deliberately disabled (correctly NOT unsupported).
                        if method and call_state >= 1:
                            target_ref = call.get("m_Target", {})
                            target_id = target_ref.get("fileID", "") if isinstance(target_ref, dict) else ""
                            mode = _coerce_int(call.get("m_Mode", 1))
                            on_click_handlers.append({
                                "method": str(method),
                                "target_file_id": str(target_id),
                                # Ordinal within m_Calls (preserve dispatch order)
                                # and the PersistentListenerMode for the static-arg gate.
                                "call_index": str(call_index),
                                "mode": str(mode if mode is not None else 1),
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
    # Stamp the descendant UI host with ``_SceneRuntimeId`` (PR2). Done
    # before any per-class property overlay so the attribute survives
    # downstream rewrites that read but don't replace the attributes dict.
    _stamp_scene_runtime_id(element, node.file_id, scene_namespace)
    # onClick wiring is carried by the ``ClickBinding`` round-trip (see
    # ``_emit_click_bindings`` below), NOT a stub LocalScript reading an
    # ``_OnClick`` attribute. No transport attribute is written onto the
    # element -- the rows go straight to the by-ref accumulator.
    _emit_click_bindings(
        element, node, on_click_handlers,
        component_owner_index=component_owner_index,
        scene_namespace=scene_namespace,
        click_bindings=click_bindings,
        unsupported_click_bindings=unsupported_click_bindings,
    )

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
        if ct == "Slider" or (
            comp.component_type == "MonoBehaviour" and "m_FillRect" in comp.properties
        ):
            # Unity serializes a UI Slider as a MonoBehaviour (m_Script GUID),
            # never a literal "Slider" component_type — identify it by its
            # defining ``m_FillRect`` field (mirrors the Toggle ``m_IsOn``
            # heuristic below). The bare ``ct == "Slider"`` form is dead on
            # real scenes (kept as a harmless OR for synthetic fixtures).
            _apply_slider_properties(
                element, comp.properties,
                node=node,
                component_owner_index=component_owner_index,
                node_index=node_index,
            )
        elif ct in ("InputField", "TMP_InputField"):
            _apply_inputfield_properties(element, comp.properties)
        elif ct in ("Dropdown", "TMP_Dropdown"):
            _apply_dropdown_properties(element, comp.properties)
        elif ct == "Toggle" or (
            comp.component_type == "MonoBehaviour" and "m_IsOn" in comp.properties
        ):
            # Unity serializes a UI Toggle as a MonoBehaviour (m_Script GUID),
            # never a literal "Toggle" component_type — identify it by its
            # defining ``m_IsOn`` field (mirrors the Button m_OnClick heuristic
            # above). The bare ``ct == "Toggle"`` form is dead on real scenes.
            _apply_toggle_properties(
                element, comp.properties,
                component_owner_index=component_owner_index,
                scene_namespace=scene_namespace,
                toggle_bindings=toggle_bindings,
            )

    # Extract background color.
    _apply_color_properties(element, ui_properties)

    # Detect layout group components.
    _apply_layout_properties(element, node.components)

    # PR3c carve-out (generic-only): if this UI GameObject's runtime-
    # bearing controller has a serialized field referencing an asset or
    # prefab, the host runtime is responsible for instantiating the
    # static child tree at runtime (via ``host.instantiatePrefab``).
    # Emitting them statically here would double-stamp the tree — runtime
    # adds its copy and the static copy never goes away. We drop static
    # descendants under this element; the stamped ``_SceneRuntimeId``
    # plus the runtime's binding is the source of truth for what lives
    # under it. Legacy mode never reaches this branch because the caller
    # passes an empty ``suppress_static_children_ids``.
    if scene_namespace and node.file_id:
        sr_id = f"{scene_namespace}:{node.file_id}"
        if sr_id in suppress_static_children_ids:
            return element

    # Recurse into children.
    for child_node in node.children:
        child_element = _convert_ui_element(
            child_node, scene_namespace,
            suppress_static_children_ids=suppress_static_children_ids,
            component_owner_index=component_owner_index,
            node_index=node_index,
            toggle_bindings=toggle_bindings,
            click_bindings=click_bindings,
            unsupported_click_bindings=unsupported_click_bindings,
        )
        if child_element is not None:
            element.children.append(child_element)

    return element


def _emit_click_bindings(
    element: RbxUIElement,
    node: SceneNode,
    on_click_handlers: list[dict[str, str]],
    *,
    component_owner_index: dict[str, str] | None,
    scene_namespace: str,
    click_bindings: list[ClickBinding] | None,
    unsupported_click_bindings: list[UnsupportedClickBinding] | None,
) -> None:
    """Emit a CANDIDATE ``ClickBinding`` per dispatchable onClick call; record the rest.

    Stage 1 of the deferred two-stage domain gate (design AMENDMENT r3). The
    component domain is NOT yet known at ``convert_scene`` time -- it is stamped
    later in ``_classify_storage`` (``scene_runtime["modules"][*]["domain"]``).
    So this stage emits a *candidate* binding for every owner-resolvable,
    non-static-arg call WITHOUT consulting domain; the post-classification step
    (``Pipeline._reclassify_click_bindings_by_domain``) re-gates the candidates
    once domains exist, moving any server/excluded/unknown target to the
    unsupported report.

    For each parsed onClick call (already gated on ``m_CallState >= 1`` --
    EditorAndRuntime(1) and RuntimeOnly(2) both fire at runtime; Off(0) is
    skipped upstream as a deliberately-disabled listener, NOT unsupported):
      * A static-argument call (``m_Mode >= 2``) is recorded as unsupported.
      * An unresolvable target (``target_file_id`` of ``0`` / not in the owner
        index, or a Button element that wasn't SRI-stamped) is recorded as
        unsupported.
      * Every other (owner-resolvable, non-static-arg) call emits a candidate
        ``ClickBinding`` row, component-precise (the engine registry key). The
        domain gate is deferred to stage 2.

    Fail LOUD, never silent: every non-emitted call lands in
    ``unsupported_click_bindings`` (surfaced in the conversion report). NO
    transport attribute is written onto ``element``. Legacy / synthetic callers
    that pass ``None`` accumulators get no rows (back-compat).
    """
    if not on_click_handlers:
        return
    if click_bindings is None or unsupported_click_bindings is None:
        return
    if not scene_namespace:
        return

    button_sri = element.attributes.get("_SceneRuntimeId")
    button_sri = button_sri if isinstance(button_sri, str) else ""
    owner_index = component_owner_index or {}

    for handler in on_click_handlers:
        method = handler.get("method", "")
        target_file_id = handler.get("target_file_id", "")
        call_index = _coerce_int(handler.get("call_index", "0")) or 0
        mode = _coerce_int(handler.get("mode", "1"))
        mode = mode if mode is not None else 1
        if not method:
            continue

        def _record_unsupported(reason: str) -> None:
            unsupported_click_bindings.append(UnsupportedClickBinding(
                button_sri=button_sri,
                target_file_id=target_file_id,
                method=method,
                reason=reason,
                call_index=call_index,
            ))

        # Static-argument calls (Object/Int/Float/String/Bool) are not
        # dispatched in v1 -> named follow-on.
        if mode >= _PERSISTENT_MODE_STATIC_ARG_MIN:
            _record_unsupported("static_argument")
            continue

        # Resolve target component fileID -> owning GameObject fileID.
        target_go_fid = owner_index.get(target_file_id) if target_file_id else None
        if not target_go_fid or not button_sri:
            _record_unsupported("unresolved_target")
            continue

        # Domain gate is DEFERRED to stage 2 (the domain isn't stamped yet at
        # convert_scene time). Emit a candidate binding; the post-classification
        # reclassifier re-gates it once ``modules[*].domain`` exists.
        click_bindings.append(ClickBinding(
            button_sri=button_sri,
            target_sri=f"{scene_namespace}:{target_go_fid}",
            target_component_id=f"{scene_namespace}:{target_file_id}",
            method=method,
            call_index=call_index,
        ))


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

    # Partial-anchor warning: one axis stretched, the other absolute.
    # Roblox UDim2 handles the mixed case, but the visual result often
    # diverges from Unity when m_Pivot/anchor offsets interact oddly.
    stretched_x = abs(amin_x - amax_x) >= 0.001
    stretched_y = abs(amin_y - amax_y) >= 0.001
    if stretched_x != stretched_y:
        log.warning(
            "RectTransform uses mixed stretch+absolute anchoring "
            "(anchor_min=(%g,%g), anchor_max=(%g,%g)); layout may differ from Unity",
            amin_x, amin_y, amax_x, amax_y,
        )

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

    # Font + alignment live at the top level on some Unity Text
    # serializations but inside m_FontData on others (the legacy layout
    # that ships with SimpleFPS). Check both, top-level first.
    font_data = props.get("m_FontData")
    font_data = font_data if isinstance(font_data, dict) else {}

    font_ref = props.get("m_Font")
    if not isinstance(font_ref, dict):
        font_ref = font_data.get("m_Font", {})
    font_name = ""
    if isinstance(font_ref, dict):
        font_name = str(font_ref.get("m_Name", "") or "")
    mapped_font = _FONT_MAP.get(font_name)
    if mapped_font:
        element.font = mapped_font

    # Alignment. Two serializations coexist:
    #   - Legacy UnityEngine.UI.Text: single m_Alignment 0..8 row-major enum.
    #   - TextMeshPro: split m_HorizontalAlignment / m_VerticalAlignment
    #     bitfields (HorizontalAlignmentOptions / VerticalAlignmentOptions).
    # TMP fields take precedence per axis when present so a TMP component
    # writing both legacy and split fields routes to the bitfield reading.
    anchor_raw = props.get("m_Alignment")
    if anchor_raw is None:
        anchor_raw = font_data.get("m_Alignment")
    if anchor_raw is not None:
        anchor = _coerce_int(anchor_raw)
        if anchor is not None and anchor in _TEXT_ANCHOR_X:
            element.text_x_alignment = _TEXT_ANCHOR_X[anchor]
            element.text_y_alignment = _TEXT_ANCHOR_Y[anchor]

    h_raw = props.get("m_HorizontalAlignment", font_data.get("m_HorizontalAlignment"))
    h_token = _map_tmp_bitfield(h_raw, _TMP_HORIZONTAL_BITS)
    if h_token is not None:
        element.text_x_alignment = h_token

    v_raw = props.get("m_VerticalAlignment", font_data.get("m_VerticalAlignment"))
    v_token = _map_tmp_bitfield(v_raw, _TMP_VERTICAL_BITS)
    if v_token is not None:
        element.text_y_alignment = v_token

    # Background transparency (text elements are usually transparent).
    element.background_transparency = 1.0


def _apply_image_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract image properties from Unity Image/RawImage components."""
    # Sprite/texture reference (GUID needs external resolution).
    sprite_ref = props.get("m_Sprite", props.get("m_Texture", {}))
    if isinstance(sprite_ref, dict):
        guid = (ref_guid(sprite_ref) or "")
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
    element.attributes["_ScrollHorizontal"] = bool(int(horizontal))
    element.attributes["_ScrollVertical"] = bool(int(vertical))


def _apply_slider_properties(
    element: RbxUIElement,
    props: dict[str, Any],
    *,
    node: SceneNode | None = None,
    component_owner_index: dict[str, str] | None = None,
    node_index: dict[str, SceneNode] | None = None,
) -> None:
    """Extract Slider properties as attributes for runtime scripts.

    In addition to the value/direction attributes, resolves the Unity
    ``m_FillRect`` (a RectTransform component ref) to its owning GameObject
    and records the ``/``-separated relative descendant path from the slider
    Frame to that fill element as the ``SliderFillElement`` attribute. The
    runtime ``setSliderValue`` resolves the fill by this path instead of
    guessing a child name. Abstains (no ``SliderFillElement``) when the fill
    ref is missing / unresolvable / not a descendant, or when the resolution
    indices are unavailable (legacy / synthetic callers).
    """
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

    # Resolve the fill element by the serialized ``m_FillRect`` ref so the
    # runtime never guesses the child name. Abstain (no attribute) on any
    # break in the chain.
    fill_rect = props.get("m_FillRect")
    if not isinstance(fill_rect, dict):
        return
    fill_comp_fid = fill_rect.get("fileID")
    if fill_comp_fid is None:
        return
    fill_comp_fid = str(fill_comp_fid)
    if not fill_comp_fid or fill_comp_fid == "0":
        return
    if node is None or component_owner_index is None or node_index is None:
        return
    fill_go_fid = component_owner_index.get(fill_comp_fid)
    if not fill_go_fid:
        return
    path = _relative_fill_path(node, fill_go_fid, node_index)
    if path is None:
        return
    element.attributes["SliderFillElement"] = path


def _apply_inputfield_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract InputField/TMP_InputField properties."""
    text = props.get("m_Text", props.get("m_text", ""))
    if isinstance(text, str) and text:
        element.text = text
    placeholder = props.get("m_Placeholder", {})
    if isinstance(placeholder, dict):
        # The placeholder is a reference to a child Text component
        # Store the reference for post-processing
        element.attributes["_PlaceholderRef"] = str(placeholder.get("fileID", ""))
    char_limit = props.get("m_CharacterLimit", 0)
    try:
        cl = int(char_limit)
        if cl > 0:
            element.attributes["_CharacterLimit"] = cl
    except (TypeError, ValueError):
        pass


def _apply_dropdown_properties(element: RbxUIElement, props: dict[str, Any]) -> None:
    """Extract Dropdown/TMP_Dropdown properties as attributes."""
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


def _apply_toggle_properties(
    element: RbxUIElement,
    props: dict[str, object],
    *,
    component_owner_index: dict[str, str] | None = None,
    scene_namespace: str = "",
    toggle_bindings: list[ToggleBinding] | None = None,
) -> None:
    """Extract Toggle properties and (generic) emit a checkmark-binding row.

    Always records ``ToggleIsOn`` (a real attribute other consumers may read).

    Additionally, when ``component_owner_index`` resolves the Toggle's
    serialized ``graphic`` component reference to an owning GameObject AND a
    ``toggle_bindings`` accumulator is supplied, appends a ``ToggleBinding`` row
    so the runtime can bind ``isOn`` -> the checkmark's ``.Visible``. NO
    transport attribute is written onto the element (the row goes straight to
    the accumulator, so nothing planner-internal serializes into the produced
    instance). If ``graphic`` is ``0`` / unresolvable, or either side is
    missing, no row is appended.
    """
    if not hasattr(element, 'attributes'):
        element.attributes = {}
    # ``m_IsOn`` crosses the YAML boundary untyped (int/float/bool/str);
    # ``_coerce_int`` is the file's canonical scalar coercion (accepts a float
    # like ``1.0`` and a float-string, ``None`` on genuine failure).
    is_on_int = _coerce_int(props.get("m_IsOn", 1))
    if is_on_int is None:
        return
    initial_on = bool(is_on_int)
    element.attributes["ToggleIsOn"] = initial_on

    # Emit a binding row iff the serialized ``graphic`` (a *component* fileID)
    # resolves to its owning GameObject. The Toggle's own element already
    # carries ``_SceneRuntimeId`` (stamped before this dispatch).
    if component_owner_index is None or toggle_bindings is None:
        return
    graphic_ref = props.get("graphic", {})
    graphic_comp_fid = (
        graphic_ref.get("fileID") if isinstance(graphic_ref, dict) else None
    )
    if not graphic_comp_fid:
        return  # graphic:0 / absent -> no binding
    graphic_go_fid = component_owner_index.get(str(graphic_comp_fid))
    if not graphic_go_fid:
        return  # unresolvable component fileID -> no binding
    toggle_sri = element.attributes.get("_SceneRuntimeId")
    if not isinstance(toggle_sri, str) or not toggle_sri or not scene_namespace:
        return  # the Toggle element wasn't SRI-stamped -> can't bind
    toggle_bindings.append(ToggleBinding(
        toggle_sri=toggle_sri,
        graphic_sri=f"{scene_namespace}:{graphic_go_fid}",
        initial_on=initial_on,
        attr_name=_TOGGLE_ISON_ATTR,
    ))


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
