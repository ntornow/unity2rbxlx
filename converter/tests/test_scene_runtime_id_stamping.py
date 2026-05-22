"""Tests for PR2 ``_SceneRuntimeId`` stamping (Piece 3 of
``converter/docs/design/scene-runtime-contract.md``).

Covers the PR2 test matrix entries:
- ID stamping on logical GameObject hosts (scene + UI).
- Wrapped-geometry: outer Model stamped, synthetic ``*_Mesh`` inner NOT
  stamped (no duplicate IDs collide at host-runtime lookup time).
- Round-trip through ``rbxlx_writer`` (XML AttributesSerialize) and
  ``luau_place_builder`` (SetAttribute calls).
- Legacy ``unity_file_id`` constraint-referent path untouched.
- Stamping skipped (no-op) when no scene namespace is established.
"""
from __future__ import annotations

import base64
import struct
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter import scene_converter as sc
from converter.scene_converter import (
    _convert_node,
    _scene_namespace,
    _stamp_scene_runtime_id,
    convert_scene,
)
from converter.ui_translator import convert_canvas
from core.roblox_types import (
    RbxCFrame,
    RbxConstraint,
    RbxPart,
    RbxPlace,
)
from core.unity_types import ComponentData, ParsedScene, SceneNode


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------

@contextmanager
def _scene_ctx(namespace: str = "Assets/Scenes/Test.unity"):
    """Activate a SceneConversionContext with a fixed scene namespace.

    Mirrors the helper in ``test_scene_converter.py``; lets us call
    ``_convert_node`` and other helpers without standing up a full
    ``convert_scene`` invocation.
    """
    old = sc._current_ctx
    sc._current_ctx = sc.SceneConversionContext(scene_runtime_namespace=namespace)
    try:
        yield sc._current_ctx
    finally:
        sc._current_ctx = old


def _decode_attributes_blob(b64_text: str) -> dict[str, object]:
    """Decode the binary AttributesSerialize blob written by
    ``rbxlx_writer._encode_attributes`` so tests can assert keys + values
    independently of the encoding particulars.

    Mirrors only the type IDs PR2 needs (string-valued attributes).
    """
    raw = base64.b64decode(b64_text)
    out: dict[str, object] = {}
    if len(raw) < 4:
        return out
    (count,) = struct.unpack_from("<I", raw, 0)
    offset = 4
    for _ in range(count):
        (key_len,) = struct.unpack_from("<I", raw, offset)
        offset += 4
        key = raw[offset:offset + key_len].decode("utf-8")
        offset += key_len
        type_id = raw[offset]
        offset += 1
        if type_id == 0x02:  # String
            (val_len,) = struct.unpack_from("<I", raw, offset)
            offset += 4
            out[key] = raw[offset:offset + val_len].decode("utf-8")
            offset += val_len
        elif type_id == 0x03:  # Bool
            out[key] = bool(raw[offset])
            offset += 1
        elif type_id == 0x06:  # Float64
            (val,) = struct.unpack_from("<d", raw, offset)
            out[key] = val
            offset += 8
        else:
            # PR2 only stamps a string attr; other types untested here.
            break
    return out


def _find_part_attributes(rbxlx_path: Path, part_name: str) -> dict[str, object]:
    """Locate an Item by Name and return its decoded AttributesSerialize."""
    tree = ET.parse(rbxlx_path)
    for item in tree.iter("Item"):
        name_el = item.find(".//string[@name='Name']")
        if name_el is not None and name_el.text == part_name:
            blob = item.find(".//BinaryString[@name='AttributesSerialize']")
            if blob is None or not blob.text:
                return {}
            return _decode_attributes_blob(blob.text)
    return {}


def _make_node(
    name: str, file_id: str, *, children: list[SceneNode] | None = None,
    components: list[ComponentData] | None = None, active: bool = True,
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


# ---------------------------------------------------------------------------
# Namespace helper
# ---------------------------------------------------------------------------

class TestSceneNamespace:
    """The scene namespace must match PR1's planner format
    (``scene_runtime_planner._scene_namespace``) so the runtime can
    resolve ``<scene>:<fileID>`` against the plan's ``game_object_id``.
    """

    def test_project_relative_path(self, tmp_path):
        project_root = tmp_path
        scene = project_root / "Assets" / "Scenes" / "Main.unity"
        scene.parent.mkdir(parents=True)
        scene.write_text("")
        assert _scene_namespace(scene, project_root) == "Assets/Scenes/Main.unity"

    def test_outside_project_returns_empty(self, tmp_path):
        """Scene path outside the project root → no portable namespace
        is computable. Returning an absolute path would bake the host
        machine's directory layout into every stamp (and on Windows
        introduce a second colon that breaks the ``<scene>:<fileID>``
        parse). The right answer is to skip stamping."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        unrelated = tmp_path / "elsewhere" / "Loose.unity"
        unrelated.parent.mkdir()
        unrelated.write_text("")
        assert _scene_namespace(unrelated, project_root) == ""

    def test_none_path_returns_empty(self):
        assert _scene_namespace(None, None) == ""

    def test_no_project_root_returns_empty(self, tmp_path):
        """Callers that invoke ``convert_scene`` without threading a
        ``unity_project_root`` (synthetic tests, ad-hoc tooling) get
        no namespace and therefore no stamping. This is the only way
        to avoid machine-specific absolute paths leaking into stamps."""
        scene = tmp_path / "Scene.unity"
        scene.write_text("")
        assert _scene_namespace(scene, None) == ""


# ---------------------------------------------------------------------------
# Scene host stamping
# ---------------------------------------------------------------------------

class TestSceneHostStamping:
    """The logical GameObject host (the outer Part/Model returned by
    ``_convert_node``) carries ``_SceneRuntimeId`` = ``"<scene>:<fileID>"``."""

    def test_plain_part_is_stamped(self):
        node = _make_node("Box", file_id="42")
        with _scene_ctx(namespace="Assets/Scenes/Demo.unity"):
            part = _convert_node(node, None, {}, {})
        assert part is not None
        assert part.attributes["_SceneRuntimeId"] == "Assets/Scenes/Demo.unity:42"

    def test_well_formed_scene_colon_fileid_value(self):
        """PR2 test matrix: 'value is well-formed `<scene>:<fileID>`.'"""
        node = _make_node("Thing", file_id="999")
        with _scene_ctx(namespace="Assets/X.unity"):
            part = _convert_node(node, None, {}, {})
        sr_id = part.attributes["_SceneRuntimeId"]
        assert isinstance(sr_id, str)
        assert sr_id.count(":") == 1
        scene, fid = sr_id.split(":")
        assert scene == "Assets/X.unity"
        assert fid == "999"

    def test_empty_namespace_skips_stamping(self):
        """Synthetic / legacy invocations with no scene_path produce no
        ``_SceneRuntimeId`` attribute (and don't crash). This keeps PR2
        inert when downstream callers haven't opted in yet."""
        node = _make_node("Box", file_id="42")
        with _scene_ctx(namespace=""):
            part = _convert_node(node, None, {}, {})
        assert part is not None
        assert "_SceneRuntimeId" not in part.attributes

    def test_helper_no_op_on_empty_inputs(self):
        """Belt-and-suspenders: ``_stamp_scene_runtime_id`` itself must be
        a no-op when either the namespace or the file_id is empty."""
        with _scene_ctx(namespace="Assets/S.unity"):
            part = RbxPart(name="x")
            _stamp_scene_runtime_id(part, file_id="")
            assert "_SceneRuntimeId" not in part.attributes
        with _scene_ctx(namespace=""):
            part = RbxPart(name="x")
            _stamp_scene_runtime_id(part, file_id="42")
            assert "_SceneRuntimeId" not in part.attributes


# ---------------------------------------------------------------------------
# Wrapped-geometry no-duplicate
# ---------------------------------------------------------------------------

class TestWrappedGeometryNoDuplicate:
    """When ``_wrap_geometry_with_model`` splits a renderer + child-transform
    node into outer Model + inner ``*_Mesh``, the stamp stays on the outer
    only — stamping both would make host-runtime lookup ambiguous (Piece 3).
    """

    def test_stamp_blocks_single_child_flatten(self):
        """A stamped parent must NOT be flattened away — another
        MonoBehaviour can hold a SerializedField reference to that
        GameObject, and PR4's host runtime resolves the reference by
        looking up the parent's ``<scene>:<file_id>``. Flattening would
        leave that lookup with no instance to bind to.

        The original ``not part.attributes`` guard naturally protects
        against this in production (every stamped host carries the
        attribute). This test pins that invariant so a future
        relaxation of the guard can't silently regress prefab/reference
        resolution under PR4.
        """
        from converter.scene_converter import _flatten_single_child_models

        parent = RbxPart(name="Parent", class_name="Model", cframe=RbxCFrame())
        parent.attributes["_SceneRuntimeId"] = "Assets/S.unity:100"
        child = RbxPart(name="Child", class_name="Part", cframe=RbxCFrame(),
                        size=(1, 1, 1), unity_file_id="200")
        child.attributes["_SceneRuntimeId"] = "Assets/S.unity:200"
        parent.children.append(child)

        parts = [parent]
        flattened = _flatten_single_child_models(parts)
        assert flattened == 0, "stamped parent must NOT be flattened"
        # Tree unchanged: Parent still wraps Child.
        assert parts[0].name == "Parent"
        assert parts[0].class_name == "Model"
        assert len(parts[0].children) == 1
        assert parts[0].children[0].name == "Child"

    def test_no_stamp_still_flattens(self):
        """Without ``_SceneRuntimeId`` (synthetic / no-project-root
        callers), the original flatten behavior is preserved."""
        from converter.scene_converter import _flatten_single_child_models

        parent = RbxPart(name="Parent", class_name="Model", cframe=RbxCFrame())
        child = RbxPart(name="Child", class_name="Part", cframe=RbxCFrame(),
                        size=(1, 1, 1), unity_file_id="200")
        parent.children.append(child)

        parts = [parent]
        flattened = _flatten_single_child_models(parts)
        assert flattened == 1
        assert parts[0].class_name == "Part"
        assert parts[0].name == "Parent"

    def test_inner_mesh_does_not_get_stamp(self):
        from converter.scene_converter import (
            _wrap_geometry_with_children_into_model,
        )

        part = RbxPart(
            name="Turret", class_name="MeshPart", cframe=RbxCFrame(),
            size=(1.0, 1.0, 1.0),
            unity_file_id="100",
            mesh_id="rbxassetid://1",
        )
        # Simulate the post-_convert_node state: stamp + a transform child
        # already attached (this is the "renderer AND child transforms"
        # shape that trips the wrap branch).
        part.attributes["_SceneRuntimeId"] = "Assets/S.unity:100"
        part.children.append(RbxPart(name="Muzzle", cframe=RbxCFrame()))

        _wrap_geometry_with_children_into_model(part, node_name="Turret")

        # The outer became a Model and kept the stamp.
        assert part.class_name == "Model"
        assert part.attributes["_SceneRuntimeId"] == "Assets/S.unity:100"

        # The inner ``*_Mesh`` child carries the geometry but NOT the stamp.
        inner = next((c for c in part.children if c.name == "Turret_Mesh"), None)
        assert inner is not None, "wrap should produce a Turret_Mesh inner child"
        assert "_SceneRuntimeId" not in inner.attributes
        # But the unity_file_id constraint-referent path IS still
        # propagated to inner — that is intentional and documented.
        assert inner.unity_file_id == "100"


# ---------------------------------------------------------------------------
# UI host stamping
# ---------------------------------------------------------------------------

class TestUIHostStamping:
    """ScreenGui root AND descendant RbxUIElements both get stamped under
    the same ``<scene>:<fileID>`` scheme as workspace parts."""

    def _canvas_with_button(self) -> SceneNode:
        button = _make_node("StartButton", file_id="201", components=[
            ComponentData(component_type="Button", file_id="bc1",
                          properties={"m_OnClick": {"m_PersistentCalls": {"m_Calls": []}}}),
        ])
        return _make_node(
            "Canvas", file_id="200",
            components=[ComponentData(
                component_type="Canvas", file_id="cc1", properties={},
            )],
            children=[button],
        )

    def test_screen_gui_root_stamped(self):
        canvas = self._canvas_with_button()
        guis = convert_canvas([canvas], scene_namespace="Assets/Scenes/UI.unity")
        assert len(guis) == 1
        assert guis[0].attributes["_SceneRuntimeId"] == "Assets/Scenes/UI.unity:200"

    def test_nested_button_stamped(self):
        canvas = self._canvas_with_button()
        guis = convert_canvas([canvas], scene_namespace="Assets/Scenes/UI.unity")
        button = guis[0].elements[0]
        assert button.class_name == "TextButton"
        assert button.attributes["_SceneRuntimeId"] == "Assets/Scenes/UI.unity:201"

    def test_empty_namespace_skips_ui_stamping(self):
        canvas = self._canvas_with_button()
        guis = convert_canvas([canvas])  # default scene_namespace=""
        assert "_SceneRuntimeId" not in guis[0].attributes
        assert "_SceneRuntimeId" not in guis[0].elements[0].attributes


# ---------------------------------------------------------------------------
# Round-trip through rbxlx_writer
# ---------------------------------------------------------------------------

class TestRbxlxWriterRoundTrip:
    """Stamped attributes ride through ``write_rbxlx`` and emerge in the
    AttributesSerialize binary blob exactly as set."""

    def test_part_stamp_round_trips(self, tmp_path):
        from roblox.rbxlx_writer import write_rbxlx
        part = RbxPart(
            name="StampedPart",
            cframe=RbxCFrame(x=0, y=0, z=0), size=(2, 2, 2),
            unity_file_id="42",
        )
        part.attributes["_SceneRuntimeId"] = "Assets/Scenes/A.unity:42"
        place = RbxPlace(workspace_parts=[part])
        output = tmp_path / "out.rbxlx"
        write_rbxlx(place, output)

        attrs = _find_part_attributes(output, "StampedPart")
        assert attrs.get("_SceneRuntimeId") == "Assets/Scenes/A.unity:42"

    def test_screen_gui_stamp_round_trips(self, tmp_path):
        from core.roblox_types import RbxScreenGui, RbxUIElement
        from roblox.rbxlx_writer import write_rbxlx

        button = RbxUIElement(
            class_name="TextButton", name="StartBtn", text="Start",
        )
        button.attributes["_SceneRuntimeId"] = "Assets/Scenes/B.unity:201"
        gui = RbxScreenGui(name="MainUI", elements=[button])
        gui.attributes["_SceneRuntimeId"] = "Assets/Scenes/B.unity:200"
        place = RbxPlace(screen_guis=[gui])

        output = tmp_path / "ui.rbxlx"
        write_rbxlx(place, output)

        # ScreenGui root.
        gui_attrs = _find_part_attributes(output, "MainUI")
        assert gui_attrs.get("_SceneRuntimeId") == "Assets/Scenes/B.unity:200"
        # Nested button.
        btn_attrs = _find_part_attributes(output, "StartBtn")
        assert btn_attrs.get("_SceneRuntimeId") == "Assets/Scenes/B.unity:201"

    def test_unity_file_id_constraint_referent_untouched(self, tmp_path):
        """``unity_file_id`` is the constraint linker's referent key — PR2's
        stamping must not interfere with the existing referent
        pre-registration. A constraint connected by ``unity_file_id`` still
        resolves to a non-empty ``Part1`` referent regardless of whether
        the parts are stamped.
        """
        from roblox.rbxlx_writer import write_rbxlx
        anchor = RbxPart(name="Anchor", unity_file_id="1",
                         cframe=RbxCFrame(), size=(1, 1, 1))
        anchor.attributes["_SceneRuntimeId"] = "Assets/X.unity:1"  # also stamped
        target = RbxPart(name="Target", unity_file_id="2",
                         cframe=RbxCFrame(x=4), size=(1, 1, 1))
        anchor.constraints.append(RbxConstraint(
            constraint_type="WeldConstraint", connected_body_file_id="2",
        ))
        place = RbxPlace(workspace_parts=[anchor, target])

        output = tmp_path / "constraint.rbxlx"
        write_rbxlx(place, output)

        tree = ET.parse(output)
        weld = None
        for item in tree.iter("Item"):
            if item.get("class") == "WeldConstraint":
                weld = item
                break
        assert weld is not None, "WeldConstraint should serialize"
        part1 = weld.find(".//Ref[@name='Part1']")
        assert part1 is not None, "Constraint must still emit Part1 referent"
        assert part1.text and part1.text.startswith("RBX"), (
            "Part1 referent must point at the target part (pre-PR2 behavior)"
        )


# ---------------------------------------------------------------------------
# Round-trip through luau_place_builder
# ---------------------------------------------------------------------------

class TestLuauPlaceBuilderRoundTrip:
    """The headless builder must surface ``_SceneRuntimeId`` via SetAttribute
    calls for both workspace parts and UI hosts — without this, the host
    runtime (PR4) can't resolve IDs in luau-built places."""

    def test_part_stamp_emits_setattribute(self):
        from roblox.luau_place_builder import generate_place_luau

        part = RbxPart(
            name="StampedPart", cframe=RbxCFrame(), size=(2, 2, 2),
            anchored=True,
        )
        part.attributes["_SceneRuntimeId"] = "Assets/A.unity:42"
        place = RbxPlace(workspace_parts=[part])

        script = generate_place_luau(place)
        assert ':SetAttribute("_SceneRuntimeId","Assets/A.unity:42")' in script

    def test_screen_gui_and_button_stamp_emit_setattribute(self):
        from core.roblox_types import RbxScreenGui, RbxUIElement
        from roblox.luau_place_builder import generate_place_luau

        button = RbxUIElement(class_name="TextButton", name="StartBtn",
                              text="Start")
        button.attributes["_SceneRuntimeId"] = "Assets/B.unity:201"
        gui = RbxScreenGui(name="MainUI", elements=[button])
        gui.attributes["_SceneRuntimeId"] = "Assets/B.unity:200"
        place = RbxPlace(screen_guis=[gui])

        script = generate_place_luau(place)
        # Both ScreenGui root and descendant button get SetAttribute calls.
        assert ':SetAttribute("_SceneRuntimeId","Assets/B.unity:200")' in script
        assert ':SetAttribute("_SceneRuntimeId","Assets/B.unity:201")' in script


# ---------------------------------------------------------------------------
# Prefab stamping
# ---------------------------------------------------------------------------

class TestPrefabStableId:
    """``_prefab_stable_id`` mirrors PR1's ``scene_runtime_planner._prefab_stable_id``
    so converter-time stamps on prefab-instantiated GameObjects match the
    ``game_object_id`` PR1 emits for the same prefab template."""

    def test_guid_plus_relative_path(self, tmp_path):
        from converter.scene_converter import _prefab_stable_id

        project_root = tmp_path
        prefab_path = tmp_path / "Assets" / "Prefabs" / "Enemy.prefab"
        prefab_path.parent.mkdir(parents=True)
        prefab_path.write_text("")

        class FakeGuidIndex:
            def guid_for_path(self, path):
                if path == prefab_path:
                    return "abc123"
                return None

        class FakeTemplate:
            def __init__(self):
                self.prefab_path = prefab_path

        result = _prefab_stable_id(
            FakeTemplate(), FakeGuidIndex(), {}, project_root,
        )
        assert result == "abc123:Assets/Prefabs/Enemy.prefab"

    def test_by_guid_fallback(self, tmp_path):
        """When ``guid_for_path`` returns nothing (prefab-variant
        templates that land in ``by_guid`` before the meta index is
        built), fall back to the ``by_guid`` reverse lookup."""
        from converter.scene_converter import _prefab_stable_id

        project_root = tmp_path
        prefab_path = tmp_path / "Assets" / "Variants" / "EliteEnemy.prefab"
        prefab_path.parent.mkdir(parents=True)
        prefab_path.write_text("")

        class FakeGuidIndex:
            def guid_for_path(self, path):
                return None  # not in the meta index yet

        class FakeTemplate:
            def __init__(self):
                self.prefab_path = prefab_path

        template = FakeTemplate()
        result = _prefab_stable_id(
            template, FakeGuidIndex(), {"variantguid": template}, project_root,
        )
        assert result == "variantguid:Assets/Variants/EliteEnemy.prefab"

    def test_outside_project_returns_empty(self, tmp_path):
        from converter.scene_converter import _prefab_stable_id

        project_root = tmp_path / "project"
        project_root.mkdir()
        prefab_path = tmp_path / "elsewhere" / "Loose.prefab"
        prefab_path.parent.mkdir()
        prefab_path.write_text("")

        class FakeGuidIndex:
            def guid_for_path(self, path):
                return "abc"

        class FakeTemplate:
            def __init__(self):
                self.prefab_path = prefab_path

        # Outside the project root → no portable prefab_id.
        assert _prefab_stable_id(
            FakeTemplate(), FakeGuidIndex(), {}, project_root,
        ) == ""


class TestPrefabHostStamping:
    """End-to-end check that ``_convert_prefab_instance`` stamps the
    converted root **and** every descendant node it instantiates via
    ``_convert_prefab_node`` — under the prefab's stable namespace, not
    the scene's. Without this, prefab-heavy scenes leave most gameplay
    GameObjects un-stamped (codex P1 finding on PR2 round 1)."""

    def _build_pi_and_lib(self, tmp_path):
        """Construct a minimal PrefabInstance + PrefabLibrary so
        ``_convert_prefab_instance`` runs end-to-end against a synthetic
        2-node prefab (root + child)."""
        from core.unity_types import (
            GuidEntry, GuidIndex, PrefabComponent, PrefabInstanceData,
            PrefabLibrary, PrefabNode, PrefabTemplate,
        )

        project_root = tmp_path.resolve()
        prefab_path = project_root / "Assets" / "Prefabs" / "Demo.prefab"
        prefab_path.parent.mkdir(parents=True)
        prefab_path.write_text("")
        prefab_path = prefab_path.resolve()

        # Two-node prefab: root (with Transform component) + one child.
        # _convert_prefab_instance walks `root.children`, so the recursion
        # site is exercised.
        child = PrefabNode(
            name="ChildNode", file_id="50", active=True, parent_file_id="10",
        )
        root = PrefabNode(name="DemoRoot", file_id="10", active=True)
        root.components = [PrefabComponent(
            component_type="Transform", file_id="11", properties={},
        )]
        root.children = [child]

        template = PrefabTemplate(
            prefab_path=prefab_path, name="Demo", root=root,
            all_nodes={"10": root, "50": child},
        )
        lib = PrefabLibrary(
            prefabs=[template],
            by_name={"Demo": template},
            by_guid={"prefabguid": template},
        )

        guid_index = GuidIndex(
            project_root=project_root,
            guid_to_entry={"prefabguid": GuidEntry(
                guid="prefabguid", asset_path=prefab_path,
                relative_path=prefab_path.relative_to(project_root),
                kind=None,
            )},
            path_to_guid={prefab_path: "prefabguid"},
        )

        pi = PrefabInstanceData(
            file_id="999", source_prefab_guid="prefabguid",
            source_prefab_file_id="100100000",
            transform_parent_file_id="0",
            modifications=[],
        )
        return pi, lib, guid_index, project_root

    def test_root_and_descendant_both_stamped(self, tmp_path):
        from converter.scene_converter import (
            SceneConversionContext,
            _convert_prefab_instance,
        )
        import converter.scene_converter as sc

        pi, lib, guid_index, project_root = self._build_pi_and_lib(tmp_path)

        # Activate a fresh context (no scene namespace — prefab stamping
        # must work even when convert_scene is bypassed entirely).
        sc._current_ctx = SceneConversionContext(scene_runtime_namespace="")
        try:
            parts = _convert_prefab_instance(
                pi, lib, guid_index,
                material_mappings={}, uploaded_assets={},
            )
        finally:
            sc._current_ctx = None

        assert len(parts) == 1
        root_part = parts[0]
        expected_ns = "prefabguid:Assets/Prefabs/Demo.prefab"
        assert root_part.attributes["_SceneRuntimeId"] == f"{expected_ns}:10"
        # Descendant carries the prefab namespace, not the scene's.
        assert len(root_part.children) == 1
        child_part = root_part.children[0]
        assert child_part.attributes["_SceneRuntimeId"] == f"{expected_ns}:50"

    def _build_single_node_pi_and_lib(self, tmp_path):
        """Same shape as ``_build_pi_and_lib`` but with a prefab whose
        root has NO children. Hits the ``has_children == False`` branch
        of ``_convert_prefab_instance`` — the codex P1 site where the
        stamp was missing.
        """
        from core.unity_types import (
            GuidEntry, GuidIndex, PrefabComponent, PrefabInstanceData,
            PrefabLibrary, PrefabNode, PrefabTemplate,
        )

        project_root = tmp_path.resolve()
        prefab_path = project_root / "Assets" / "Prefabs" / "Solo.prefab"
        prefab_path.parent.mkdir(parents=True)
        prefab_path.write_text("")
        prefab_path = prefab_path.resolve()

        # Single-node prefab: just the root, no children.
        root = PrefabNode(name="SoloRoot", file_id="20", active=True)
        root.components = [PrefabComponent(
            component_type="Transform", file_id="21", properties={},
        )]
        # root.children is the default empty list.

        template = PrefabTemplate(
            prefab_path=prefab_path, name="Solo", root=root,
            all_nodes={"20": root},
        )
        lib = PrefabLibrary(
            prefabs=[template],
            by_name={"Solo": template},
            by_guid={"soloprefabguid": template},
        )
        guid_index = GuidIndex(
            project_root=project_root,
            guid_to_entry={"soloprefabguid": GuidEntry(
                guid="soloprefabguid", asset_path=prefab_path,
                relative_path=prefab_path.relative_to(project_root),
                kind=None,
            )},
            path_to_guid={prefab_path: "soloprefabguid"},
        )
        pi = PrefabInstanceData(
            file_id="888", source_prefab_guid="soloprefabguid",
            source_prefab_file_id="100100000",
            transform_parent_file_id="0",
            modifications=[],
        )
        return pi, lib, guid_index, project_root

    def test_single_node_prefab_root_stamped(self, tmp_path):
        """Codex P1: ``_convert_prefab_instance`` previously only stamped
        the root inside its ``has_children`` branch. Prefabs whose
        template root had no children fell through the ``else`` branch
        with no stamp at all, breaking PR4's prefab binding for the most
        common prefab shape."""
        from converter.scene_converter import (
            SceneConversionContext,
            _convert_prefab_instance,
        )
        import converter.scene_converter as sc

        pi, lib, guid_index, project_root = (
            self._build_single_node_pi_and_lib(tmp_path)
        )

        sc._current_ctx = SceneConversionContext(scene_runtime_namespace="")
        try:
            parts = _convert_prefab_instance(
                pi, lib, guid_index,
                material_mappings={}, uploaded_assets={},
            )
        finally:
            sc._current_ctx = None

        assert len(parts) == 1
        root_part = parts[0]
        assert root_part.children == []  # no children in the template
        expected_ns = "soloprefabguid:Assets/Prefabs/Solo.prefab"
        assert "_SceneRuntimeId" in root_part.attributes, (
            "single-node prefab root was not stamped — PR4 has no way "
            "to bind its plan row to this instance."
        )
        assert root_part.attributes["_SceneRuntimeId"] == f"{expected_ns}:20"


# ---------------------------------------------------------------------------
# End-to-end via convert_scene
# ---------------------------------------------------------------------------

class TestConvertSceneIntegration:
    """One full pass through ``convert_scene`` with a synthetic ParsedScene —
    confirms ``unity_project_root`` plumbing produces a namespace that
    matches PR1's planner format."""

    def test_namespace_matches_pr1_format(self, tmp_path):
        project_root = tmp_path
        scene_path = project_root / "Assets" / "Scenes" / "Synth.unity"
        scene_path.parent.mkdir(parents=True)
        scene_path.write_text("")

        node = _make_node("Root", file_id="7")
        scene = ParsedScene(scene_path=scene_path, roots=[node],
                            all_nodes={"7": node})

        place = convert_scene(parsed_scene=scene, unity_project_root=project_root)

        # Find the converted part for our node.
        converted = next(
            (p for p in place.workspace_parts if p.name == "Root"), None,
        )
        assert converted is not None
        assert converted.attributes["_SceneRuntimeId"] == \
            "Assets/Scenes/Synth.unity:7"
