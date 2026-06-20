"""gap#4 — SOUND clear-intent child suppression in ui_translator.

Under generic, a UI host's authored children are suppressed iff a runtime-
bearing controller on it PROVABLY clears-then-populates a serialized container
field (the upfront-C# ``cleared_container_fields`` signal) whose authored target
IS that subtree. Merely REFERENCING an asset or prefab NO LONGER triggers
suppression — that gate was unsound and destroyed real authored UI (gap#4). The
runtime re-populates the cleared container, so emitting the static authored
children as well would double-stamp.

Coverage:
- Under ``--scene-runtime=generic``, a Canvas whose UI GameObject hosts a
  runtime-bearing controller that clears-then-populates a serialized container
  field (resolved to that GO) gets no static descendants under it. The
  element's ``_SceneRuntimeId`` is the binding the runtime uses to repopulate.
- The same input under ``--scene-runtime=legacy`` keeps the static descendants
  intact (byte-unchanged emit).
- An asset-ref-WITHOUT-clear controller does NOT trigger suppression (the gap#4
  fix — authored UI is preserved).
- A non-runtime-bearing controller does NOT trigger suppression.
- The helper ``_collect_ui_child_suppression_ids`` returns the empty set on
  missing / partial planner artifacts so legacy never sees spurious suppression.
- Snapshot: the legacy emit is identical regardless of whether a populated
  planner artifact is threaded through.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.scene_converter import (
    _collect_ui_child_suppression_ids,
    convert_scene,
)
from converter.ui_translator import convert_canvas
from core.unity_types import ComponentData, ParsedScene, SceneNode
from roblox.rbxlx_writer import write_rbxlx


SCENE_PATH = Path("Assets/Scenes/UI.unity")


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------

def _make_node(
    name: str, file_id: str, *, active: bool = True,
    children: list[SceneNode] | None = None,
    components: list[ComponentData] | None = None,
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
        position=(0.0, 0.0, 0.0),
        rotation=(0.0, 0.0, 0.0, 1.0),
        scale=(1.0, 1.0, 1.0),
    )


def _canvas_with_controller_and_static_descendants() -> SceneNode:
    """Canvas → ControllerHost (Frame w/ MonoBehaviour) → StaticChild,
    StaticGrandchild. The runtime-bearing controller lives on
    ControllerHost (file_id=200); its serialized field points at a
    prefab GUID (recorded out-of-band on the synthetic scene_runtime
    artifact passed to ``convert_canvas``).
    """
    grandchild = _make_node("StaticGrandchild", file_id="202")
    static_child = _make_node(
        "StaticChild", file_id="201",
        children=[grandchild],
    )
    controller_host = _make_node(
        "ControllerHost", file_id="200",
        components=[
            ComponentData(
                component_type="RectTransform", file_id="2001",
                properties={},
            ),
            ComponentData(
                component_type="MonoBehaviour", file_id="2002",
                properties={
                    # Planner classified this MB as runtime-bearing; the
                    # serialized field with the prefab ref is the signal
                    # that triggers child suppression.
                    "m_inventoryItemPrefab": {"fileID": 0, "guid": "abc123guid"},
                },
            ),
        ],
        children=[static_child],
    )
    return _make_node(
        "Canvas", file_id="100",
        components=[
            ComponentData(component_type="Canvas", file_id="1001", properties={}),
        ],
        children=[controller_host],
    )


def _find_element(elements, name):
    for e in elements:
        if e.name == name:
            return e
        found = _find_element(e.children, name)
        if found is not None:
            return found
    return None


def _all_element_names(elements) -> list[str]:
    out: list[str] = []
    for e in elements:
        out.append(e.name)
        out.extend(_all_element_names(e.children))
    return out


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

class TestCollectUiChildSuppressionIds:
    """Direct coverage on the planner-artifact walker so a schema
    drift surfaces here instead of bubbling up through ``convert_scene``."""

    def test_empty_input_returns_empty_set(self):
        assert _collect_ui_child_suppression_ids(None) == frozenset()
        assert _collect_ui_child_suppression_ids({}) == frozenset()

    def test_runtime_bearing_cleared_container_marks_resolved_go(self):
        """A runtime-bearing controller that PROVABLY clears-then-populates
        ``containerTransform`` (a serialized field resolving to host GO 200 via
        a gameobject reference row) suppresses that GO's static children."""
        artifact = {
            "modules": {
                "InventoryController": {
                    "stem": "InventoryController",
                    "class_name": "InventoryController",
                    "runtime_bearing": True,
                    "cleared_container_fields": ["containerTransform"],
                },
            },
            "scenes": {
                str(SCENE_PATH): {
                    "instances": [
                        {
                            "instance_id": f"{SCENE_PATH.as_posix()}:2002",
                            "script_id": "InventoryController",
                            "game_object_id": f"{SCENE_PATH.as_posix()}:200",
                            "active": True,
                            "enabled": True,
                            "config": {},
                        },
                    ],
                    "references": [
                        {
                            "from": f"{SCENE_PATH.as_posix()}:2002",
                            "field": "containerTransform",
                            "index": None,
                            "target_kind": "gameobject",
                            "target_ref": f"{SCENE_PATH.as_posix()}:200",
                            "target_is_ui": True,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_ui_child_suppression_ids(artifact) == frozenset({
            f"{SCENE_PATH.as_posix()}:200",
        })

    def test_asset_ref_without_clear_keeps_children(self):
        """gap#4 fix: a runtime-bearing controller that REFERENCES an asset (or
        prefab) but does NOT provably clear-then-populate a container has NO
        cleared field — its authored UI children MUST be preserved. This is the
        class of UI the old asset-ref gate destroyed."""
        artifact = {
            "modules": {
                "SettingsPopup": {
                    "stem": "SettingsPopup",
                    "class_name": "SettingsPopup",
                    "runtime_bearing": True,
                    # References a mixer asset, never clears a container.
                    "cleared_container_fields": [],
                },
            },
            "scenes": {
                str(SCENE_PATH): {
                    "instances": [
                        {
                            "instance_id": f"{SCENE_PATH.as_posix()}:2002",
                            "script_id": "SettingsPopup",
                            "game_object_id": f"{SCENE_PATH.as_posix()}:200",
                            "active": True,
                            "enabled": True,
                            "config": {},
                        },
                    ],
                    "references": [
                        {
                            "from": f"{SCENE_PATH.as_posix()}:2002",
                            "field": "audioMixer",
                            "index": None,
                            "target_kind": "asset",
                            "target_ref": "mixerguid",
                            "target_is_ui": False,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_ui_child_suppression_ids(artifact) == frozenset()

    def test_non_runtime_bearing_mb_does_not_trigger(self):
        """The carve-out gates on ``runtime_bearing`` — a module the
        planner never marked runtime-bearing (e.g. a helper class
        bound at edit time but not running on a MonoBehaviour) must
        not trigger suppression even with a cleared container."""
        artifact = {
            "modules": {
                "OldController": {
                    "stem": "OldController",
                    "class_name": "OldController",
                    "runtime_bearing": False,
                    "cleared_container_fields": ["containerTransform"],
                },
            },
            "scenes": {
                str(SCENE_PATH): {
                    "instances": [
                        {
                            "instance_id": f"{SCENE_PATH.as_posix()}:2002",
                            "script_id": "OldController",
                            "game_object_id": f"{SCENE_PATH.as_posix()}:200",
                            "active": True,
                            "enabled": True,
                            "config": {},
                        },
                    ],
                    "references": [
                        {
                            "from": f"{SCENE_PATH.as_posix()}:2002",
                            "field": "containerTransform",
                            "index": None,
                            "target_kind": "gameobject",
                            "target_ref": f"{SCENE_PATH.as_posix()}:200",
                            "target_is_ui": True,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_ui_child_suppression_ids(artifact) == frozenset()

    def test_fail_closed_legacy_domain_does_not_trigger(self):
        """Codex P1: when PR3b's domain classifier forces a runtime-
        bearing module to ``domain="legacy"`` (both-side API,
        intra-class conflict, reachability conflict), the host runtime
        never wires that module — so the static UI subtree under its
        host element MUST persist. The carve-out must exclude
        domain=legacy entries even when ``runtime_bearing`` is True.

        At ``convert_scene`` time the classifier hasn't run yet
        (subphase order: convert_scene → _classify_storage), so the
        ``domain`` field is typically absent in the artifact. This
        test pins the guard for the case where a post-classify
        artifact is threaded through (e.g. by a future architecture
        change, or by a caller that runs convert_scene a second time
        after classify)."""
        artifact = {
            "modules": {
                "ConflictedController": {
                    "stem": "ConflictedController",
                    "class_name": "ConflictedController",
                    "runtime_bearing": True,
                    "domain": "legacy",  # PR3b fail-closed verdict.
                    # A cleared container that WOULD resolve+trigger — the domain
                    # guard must still suppress the carve-out.
                    "cleared_container_fields": ["containerTransform"],
                },
            },
            "scenes": {
                str(SCENE_PATH): {
                    "instances": [
                        {
                            "instance_id": f"{SCENE_PATH.as_posix()}:2002",
                            "script_id": "ConflictedController",
                            "game_object_id": f"{SCENE_PATH.as_posix()}:200",
                            "active": True,
                            "enabled": True,
                            "config": {},
                        },
                    ],
                    "references": [
                        {
                            "from": f"{SCENE_PATH.as_posix()}:2002",
                            "field": "containerTransform",
                            "index": None,
                            "target_kind": "gameobject",
                            "target_ref": f"{SCENE_PATH.as_posix()}:200",
                            "target_is_ui": True,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_ui_child_suppression_ids(artifact) == frozenset()

    def test_component_ref_cleared_container_resolves_owning_go(self):
        """A cleared field resolving through a ``component`` reference (the
        container Transform serialized as a peer-component) resolves to the
        peer's owning GameObject id."""
        artifact = {
            "modules": {
                "Ctrl": {
                    "stem": "Ctrl",
                    "class_name": "Ctrl",
                    "runtime_bearing": True,
                    "cleared_container_fields": ["content"],
                },
            },
            "scenes": {
                str(SCENE_PATH): {
                    "instances": [
                        {
                            "instance_id": f"{SCENE_PATH.as_posix()}:5",
                            "script_id": "Ctrl",
                            "game_object_id": f"{SCENE_PATH.as_posix()}:4",
                            "active": True,
                            "enabled": True,
                            "config": {},
                        },
                        # The container's peer component instance, owned by GO 7.
                        {
                            "instance_id": f"{SCENE_PATH.as_posix()}:8",
                            "script_id": "ContentPanel",
                            "game_object_id": f"{SCENE_PATH.as_posix()}:7",
                            "active": True,
                            "enabled": True,
                            "config": {},
                        },
                    ],
                    "references": [
                        {
                            "from": f"{SCENE_PATH.as_posix()}:5",
                            "field": "content",
                            "index": None,
                            "target_kind": "component",
                            "target_ref": f"{SCENE_PATH.as_posix()}:8",
                            "target_is_ui": True,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_ui_child_suppression_ids(artifact) == frozenset({
            f"{SCENE_PATH.as_posix()}:7",
        })

    def test_ref_field_not_a_cleared_container_does_not_trigger(self):
        """A gameobject/component ref whose ``field`` is NOT one of the
        controller's cleared-container fields must NOT trigger suppression —
        only provably-cleared containers are dropped."""
        artifact = {
            "modules": {
                "Ctrl": {
                    "stem": "Ctrl", "class_name": "Ctrl",
                    "runtime_bearing": True,
                    "cleared_container_fields": ["containerTransform"],
                },
            },
            "scenes": {
                str(SCENE_PATH): {
                    "instances": [
                        {
                            "instance_id": f"{SCENE_PATH.as_posix()}:5",
                            "script_id": "Ctrl",
                            "game_object_id": f"{SCENE_PATH.as_posix()}:4",
                            "active": True,
                            "enabled": True,
                            "config": {},
                        },
                    ],
                    "references": [
                        {
                            "from": f"{SCENE_PATH.as_posix()}:5",
                            "field": "targetGo",  # NOT the cleared field.
                            "index": None,
                            "target_kind": "gameobject",
                            "target_ref": f"{SCENE_PATH.as_posix()}:10",
                            "target_is_ui": False,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_ui_child_suppression_ids(artifact) == frozenset()


# ---------------------------------------------------------------------------
# convert_canvas-level integration — mode gating
# ---------------------------------------------------------------------------

class TestConvertCanvasChildSuppression:
    """Direct convert_canvas tests (no full ``convert_scene`` needed)
    so the carve-out's effect is observable without scene-runtime
    plumbing every test through ``parse_scene``."""

    def test_generic_drops_static_descendants_under_controller_host(self):
        canvas = _canvas_with_controller_and_static_descendants()
        suppress = frozenset({"Assets/Scenes/UI.unity:200"})

        guis = convert_canvas(
            [canvas],
            scene_namespace="Assets/Scenes/UI.unity",
            scene_runtime_mode="generic",
            suppress_static_children_ids=suppress,
        )

        assert len(guis) == 1
        controller_host = _find_element(guis[0].elements, "ControllerHost")
        assert controller_host is not None
        assert controller_host.children == [], (
            "controller host under generic must have no static descendants"
        )
        # The host itself is still stamped — the runtime resolves the
        # prefab population via the _SceneRuntimeId binding.
        assert (
            controller_host.attributes["_SceneRuntimeId"]
            == "Assets/Scenes/UI.unity:200"
        )
        # The descendants the static emit would have produced are gone
        # from the entire tree.
        assert "StaticChild" not in _all_element_names(guis[0].elements)
        assert "StaticGrandchild" not in _all_element_names(guis[0].elements)

    def test_legacy_keeps_static_descendants(self):
        canvas = _canvas_with_controller_and_static_descendants()

        # No mode / no suppression ids → legacy emit path.
        guis = convert_canvas(
            [canvas], scene_namespace="Assets/Scenes/UI.unity",
        )

        controller_host = _find_element(guis[0].elements, "ControllerHost")
        assert controller_host is not None
        assert _find_element(controller_host.children, "StaticChild") is not None
        assert _find_element(
            controller_host.children, "StaticGrandchild"
        ) is not None

    def test_legacy_mode_with_populated_suppression_still_emits(self):
        """Belt-and-suspenders: even if the caller (incorrectly) passes a
        populated ``suppress_static_children_ids`` under
        ``scene_runtime_mode="legacy"``, the legacy emit MUST stay
        unchanged — the gate is the mode AND the set, with mode being
        the authoritative gate."""
        canvas = _canvas_with_controller_and_static_descendants()
        suppress = frozenset({"Assets/Scenes/UI.unity:200"})

        guis = convert_canvas(
            [canvas],
            scene_namespace="Assets/Scenes/UI.unity",
            scene_runtime_mode="legacy",
            suppress_static_children_ids=suppress,
        )

        controller_host = _find_element(guis[0].elements, "ControllerHost")
        assert controller_host is not None
        assert _find_element(controller_host.children, "StaticChild") is not None

    def test_generic_no_suppression_set_keeps_static_descendants(self):
        """When the planner artifact yields no GO that needs
        suppression (e.g. no runtime-bearing UI controllers), generic
        emit is identical to legacy. Pins that the carve-out fires only
        on confirmed signals — no silent over-suppression."""
        canvas = _canvas_with_controller_and_static_descendants()

        guis = convert_canvas(
            [canvas],
            scene_namespace="Assets/Scenes/UI.unity",
            scene_runtime_mode="generic",
            suppress_static_children_ids=frozenset(),
        )

        controller_host = _find_element(guis[0].elements, "ControllerHost")
        assert controller_host is not None
        assert _find_element(controller_host.children, "StaticChild") is not None


# ---------------------------------------------------------------------------
# convert_scene-level end-to-end snapshot
# ---------------------------------------------------------------------------

def _build_scene(tmp_path: Path, canvas_root: SceneNode) -> ParsedScene:
    scene_file = tmp_path / SCENE_PATH
    scene_file.parent.mkdir(parents=True, exist_ok=True)
    scene_file.touch(exist_ok=True)

    def _index(node: SceneNode, parent_fid: str | None) -> dict[str, SceneNode]:
        node.parent_file_id = parent_fid
        idx = {node.file_id: node}
        for c in node.children:
            idx.update(_index(c, node.file_id))
        return idx

    return ParsedScene(
        scene_path=scene_file, roots=[canvas_root],
        all_nodes=_index(canvas_root, None),
    )


def _full_suppression_artifact() -> dict[str, object]:
    """A planner artifact that marks ControllerHost (file_id=200) as
    runtime-bearing and PROVABLY clears-then-populates ``containerTransform``,
    a serialized field resolving (via a gameobject reference row) to the host GO
    200. Matches the canvas built by
    ``_canvas_with_controller_and_static_descendants``."""
    return {
        "modules": {
            "InventoryController": {
                "stem": "InventoryController",
                "class_name": "InventoryController",
                "runtime_bearing": True,
                "cleared_container_fields": ["containerTransform"],
            },
        },
        "scenes": {
            str(SCENE_PATH): {
                "instances": [
                    {
                        "instance_id": f"{SCENE_PATH.as_posix()}:2002",
                        "script_id": "InventoryController",
                        "game_object_id": f"{SCENE_PATH.as_posix()}:200",
                        "active": True,
                        "enabled": True,
                        "config": {},
                    },
                ],
                "references": [
                    {
                        "from": f"{SCENE_PATH.as_posix()}:2002",
                        "field": "containerTransform",
                        "index": None,
                        "target_kind": "gameobject",
                        "target_ref": f"{SCENE_PATH.as_posix()}:200",
                        "target_is_ui": True,
                    },
                ],
                "lifecycle_order": [],
            },
        },
    }


class TestEndToEndConvertScene:
    """End-to-end: ``convert_scene`` plumbs ``scene_runtime`` +
    ``scene_runtime_mode`` into ``convert_canvas`` so the carve-out
    fires only under generic."""

    def test_generic_end_to_end_drops_descendants(self, tmp_path):
        canvas = _canvas_with_controller_and_static_descendants()
        scene = _build_scene(tmp_path, canvas)

        place = convert_scene(
            parsed_scene=scene,
            unity_project_root=tmp_path,
            scene_runtime=_full_suppression_artifact(),
            scene_runtime_mode="generic",
        )

        assert len(place.screen_guis) == 1
        controller_host = _find_element(
            place.screen_guis[0].elements, "ControllerHost",
        )
        assert controller_host is not None
        assert controller_host.children == []

    def test_legacy_end_to_end_keeps_descendants(self, tmp_path):
        canvas = _canvas_with_controller_and_static_descendants()
        scene = _build_scene(tmp_path, canvas)

        # Default scene_runtime_mode="legacy" + populated artifact.
        place = convert_scene(
            parsed_scene=scene,
            unity_project_root=tmp_path,
            scene_runtime=_full_suppression_artifact(),
        )

        assert len(place.screen_guis) == 1
        controller_host = _find_element(
            place.screen_guis[0].elements, "ControllerHost",
        )
        assert controller_host is not None
        assert _find_element(controller_host.children, "StaticChild") is not None
        assert _find_element(
            controller_host.children, "StaticGrandchild",
        ) is not None

    def test_legacy_rbxlx_bytes_unchanged_with_or_without_scene_runtime(self, tmp_path):
        """Strong byte-identity snapshot (codex P3): build the same UI
        scene twice under legacy mode — once with no artifact, once
        with the populated one — write each ``RbxPlace`` to disk via
        ``write_rbxlx`` and assert the XML payload (with random
        ``referent`` UUIDs stripped) is identical.

        Stripping referents is necessary because ``rbxlx_writer``
        generates them via ``uuid4`` per-call; the rest of the XML
        (every per-element position, size, text, color, layout,
        attribute) is fully deterministic and any carve-out leak
        would surface as a byte diff. Strictly stronger pin than the
        original class/name/attributes structural snapshot."""
        import re
        _referent_re = re.compile(rb' referent="RBX[0-9A-F]+"')
        _ref_re = re.compile(rb'<Ref name="[^"]+">RBX[0-9A-F]+</Ref>')

        def _normalize(b: bytes) -> bytes:
            b = _referent_re.sub(b' referent="<DROPPED>"', b)
            return _ref_re.sub(b'<Ref name="X"><DROPPED></Ref>', b)

        def _build() -> ParsedScene:
            return _build_scene(
                tmp_path, _canvas_with_controller_and_static_descendants(),
            )

        place_no_runtime = convert_scene(
            parsed_scene=_build(), unity_project_root=tmp_path,
        )
        place_with_runtime_legacy = convert_scene(
            parsed_scene=_build(), unity_project_root=tmp_path,
            scene_runtime=_full_suppression_artifact(),
            scene_runtime_mode="legacy",
        )

        out_baseline = tmp_path / "baseline.rbxlx"
        out_with_runtime = tmp_path / "with_runtime.rbxlx"
        write_rbxlx(place_no_runtime, out_baseline)
        write_rbxlx(place_with_runtime_legacy, out_with_runtime)

        baseline_bytes = _normalize(out_baseline.read_bytes())
        with_runtime_bytes = _normalize(out_with_runtime.read_bytes())
        assert baseline_bytes == with_runtime_bytes, (
            "legacy UI emit must be byte-identical on disk (modulo "
            "random referent UUIDs) regardless of whether a "
            "scene_runtime artifact is threaded through"
        )
