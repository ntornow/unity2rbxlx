"""PR1 unit tests for converter.scene_runtime_planner.

Covers the planner's per-PR1 test matrix:
  - synthetic ParsedScene → plan shape
  - array order preserved
  - local + cross-asset refs resolved
  - duplicate-stem scripts get distinct script_id
  - multi-scene namespacing
  - stable prefab_id distinguishes same-name prefabs in different folders;
    a prefab ref resolves to the correct one
  - stem-keyed require graph + collision detection
  - target_is_ui marks references into Canvas subtrees
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core.unity_types import (
    ComponentData,
    GuidEntry,
    GuidIndex,
    ParsedScene,
    PrefabInstanceData,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
    SceneNode,
    StrippedComponentRecord,
)
from converter.scene_runtime_planner import (
    _resolve_stripped_refs,
    build_require_graph,
    build_script_id_by_name,
    derive_intrinsic_script_class,
    plan_scene_runtime,
)
from core.roblox_types import RbxScript


# ---------------------------------------------------------------------------
# Synthetic-fixture builders
# ---------------------------------------------------------------------------

def _make_guid_index(
    project_root: Path, entries: dict[str, tuple[Path, str]],
) -> GuidIndex:
    """``entries: {guid: (absolute_asset_path, kind)}``. Builds a GuidIndex
    with the same shape ``build_guid_index`` produces, but in-memory.
    """
    idx = GuidIndex(project_root=project_root)
    for guid, (asset_path, kind) in entries.items():
        # Constructor in unity_types treats kind as a Literal; the runtime
        # check is structural so a str works.
        try:
            relative_path = asset_path.relative_to(project_root)
        except ValueError:
            relative_path = asset_path
        idx.guid_to_entry[guid] = GuidEntry(
            guid=guid,
            asset_path=asset_path,
            relative_path=relative_path,
            kind=kind,  # type: ignore[arg-type]
        )
        idx.path_to_guid[asset_path.resolve()] = guid
    return idx


def _mb_props(
    script_guid: str,
    *,
    enabled: int = 1,
    go_fid: str = "",
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    """Compose a MonoBehaviour ``properties`` dict — the shape scene_parser
    drops into ``ComponentData.properties``."""
    props: dict[str, object] = {
        "m_Script": {"fileID": "11500000", "guid": script_guid, "type": 3},
        "m_GameObject": {"fileID": go_fid} if go_fid else {"fileID": 0},
        "m_Enabled": enabled,
    }
    if extra:
        props.update(extra)
    return props


def _scene(
    scene_path: Path,
    *,
    roots: list[SceneNode],
    all_nodes: dict[str, SceneNode] | None = None,
) -> ParsedScene:
    scene = ParsedScene(scene_path=scene_path)
    scene.roots = roots

    def _flatten(node: SceneNode) -> dict[str, SceneNode]:
        out = {node.file_id: node}
        for c in node.children:
            out.update(_flatten(c))
        return out

    if all_nodes is None:
        all_nodes = {}
        for r in roots:
            all_nodes.update(_flatten(r))
    scene.all_nodes = all_nodes
    return scene


def _node(
    file_id: str,
    name: str = "GameObject",
    *,
    active: bool = True,
    components: list[ComponentData] | None = None,
    children: list[SceneNode] | None = None,
) -> SceneNode:
    n = SceneNode(name=name, file_id=file_id, active=active, layer=0, tag="")
    n.components = components or []
    n.children = children or []
    return n


# ---------------------------------------------------------------------------
# Plan shape & basic field extraction
# ---------------------------------------------------------------------------

class TestPlanShape:
    def test_empty_inputs_produce_empty_artifact(self, tmp_path: Path):
        artifact = plan_scene_runtime(
            parsed_scenes=[],
            prefab_library=None,
            guid_index=None,
            unity_project_root=tmp_path,
        )
        assert set(artifact.keys()) == {
            "modules", "scenes", "prefabs", "scene_prefab_placements",
            "domain_overrides",
        }
        assert artifact["modules"] == {}
        assert artifact["scenes"] == {}
        assert artifact["prefabs"] == {}
        assert artifact["scene_prefab_placements"] == []
        assert artifact["domain_overrides"] == {}

    def test_single_scene_emits_instance_with_scalar_config(self, tmp_path: Path):
        # Project layout: Assets/Scripts/Player.cs, Assets/Scenes/Main.unity
        scripts_dir = tmp_path / "Assets" / "Scripts"
        scripts_dir.mkdir(parents=True)
        cs = scripts_dir / "Player.cs"
        cs.write_text("public class Player : MonoBehaviour { }")

        guid = "a" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        mb_node = _node("100", "Player", components=[
            ComponentData(
                component_type="MonoBehaviour",
                file_id="200",
                properties=_mb_props(
                    guid, go_fid="100",
                    extra={"speed": 5.0, "playerName": "Alice"},
                ),
            )
        ])
        scene_path = tmp_path / "Assets" / "Scenes" / "Main.unity"
        scene = _scene(scene_path, roots=[mb_node])

        artifact = plan_scene_runtime(
            parsed_scenes=[scene],
            prefab_library=None,
            guid_index=idx,
            unity_project_root=tmp_path,
        )

        # Scene key is project-relative, forward-slashed.
        assert "Assets/Scenes/Main.unity" in artifact["scenes"]
        scene_block = artifact["scenes"]["Assets/Scenes/Main.unity"]
        assert len(scene_block["instances"]) == 1

        inst = scene_block["instances"][0]
        assert inst["instance_id"] == "Assets/Scenes/Main.unity:200"
        assert inst["script_id"] == guid
        assert inst["game_object_id"] == "Assets/Scenes/Main.unity:100"
        assert inst["active"] is True
        assert inst["enabled"] is True
        # Scalar serialized fields land in config; internals are filtered.
        assert inst["config"]["speed"] == 5.0
        assert inst["config"]["playerName"] == "Alice"
        assert "m_Script" not in inst["config"]
        assert "m_GameObject" not in inst["config"]

        # No refs in this fixture.
        assert scene_block["references"] == []
        # Lifecycle order follows DFS — one entry here.
        assert scene_block["lifecycle_order"] == [inst["instance_id"]]

        # Modules table includes the script with runtime_bearing=True.
        assert artifact["modules"][guid]["stem"] == "Player"
        assert artifact["modules"][guid]["class_name"] == "Player"
        assert artifact["modules"][guid]["runtime_bearing"] is True

    def test_disabled_monobehaviour_recorded_with_enabled_false(self, tmp_path: Path):
        scripts_dir = tmp_path / "Assets" / "Scripts"
        scripts_dir.mkdir(parents=True)
        cs = scripts_dir / "Foo.cs"
        cs.write_text("public class Foo : MonoBehaviour { }")
        guid = "b" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        scene = _scene(tmp_path / "Assets" / "Scenes" / "S.unity", roots=[
            _node("10", "Foo", active=False, components=[
                ComponentData(
                    component_type="MonoBehaviour",
                    file_id="20",
                    properties=_mb_props(guid, enabled=0, go_fid="10"),
                )
            ])
        ])
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        inst = artifact["scenes"]["Assets/Scenes/S.unity"]["instances"][0]
        assert inst["enabled"] is False
        assert inst["active"] is False


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------

class TestReferenceResolution:
    def test_local_reference_to_gameobject(self, tmp_path: Path):
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "Door.cs"
        cs.write_text("public class Door : MonoBehaviour { }")
        guid = "c" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        # GameObject 100 carries a Door MB referencing GameObject 500.
        target_node = _node("500", "OtherGO")
        mb_node = _node("100", "Door", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="200",
                properties=_mb_props(
                    guid, go_fid="100",
                    extra={"target": {"fileID": "500"}},
                ),
            )
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Main.unity",
            roots=[mb_node, target_node],
            all_nodes={"100": mb_node, "500": target_node},
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/Main.unity"]["references"]
        assert len(refs) == 1
        r = refs[0]
        assert r["from"] == "Assets/Scenes/Main.unity:200"
        assert r["field"] == "target"
        assert r["index"] is None
        assert r["target_kind"] == "gameobject"
        assert r["target_ref"] == "Assets/Scenes/Main.unity:500"
        assert r["target_is_ui"] is False

    def test_cross_asset_reference_to_scriptable_object(self, tmp_path: Path):
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "ItemHolder.cs"
        cs.write_text("public class ItemHolder : MonoBehaviour { }")
        so_path = tmp_path / "Assets" / "Data" / "Sword.asset"
        so_path.parent.mkdir(parents=True)
        so_path.touch()

        cs_guid = "d" * 32
        so_guid = "e" * 32
        idx = _make_guid_index(tmp_path, {
            cs_guid: (cs, "script"),
            so_guid: (so_path, "data"),
        })

        mb_node = _node("10", "Holder", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(
                    cs_guid, go_fid="10",
                    extra={"item": {"fileID": "11400000", "guid": so_guid, "type": 2}},
                ),
            )
        ])
        scene = _scene(tmp_path / "Assets" / "Scenes" / "Game.unity",
                       roots=[mb_node])
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/Game.unity"]["references"]
        assert len(refs) == 1
        assert refs[0]["target_kind"] == "scriptable_object"
        assert refs[0]["target_ref"] == so_guid

    def test_array_field_preserves_order_and_index(self, tmp_path: Path):
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "SpawnList.cs"
        cs.write_text("public class SpawnList : MonoBehaviour { }")
        guid = "f" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        # Three reference targets; the array order is what the planner
        # must round-trip.
        target_a = _node("501", "A")
        target_b = _node("502", "B")
        target_c = _node("503", "C")
        mb_node = _node("100", "Spawner", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="200",
                properties=_mb_props(
                    guid, go_fid="100",
                    extra={
                        "spawnPoints": [
                            {"fileID": "501"},
                            {"fileID": "502"},
                            {"fileID": "503"},
                        ]
                    },
                ),
            )
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "M.unity",
            roots=[mb_node, target_a, target_b, target_c],
            all_nodes={
                "100": mb_node, "501": target_a, "502": target_b, "503": target_c,
            },
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/M.unity"]["references"]
        assert [r["index"] for r in refs] == [0, 1, 2]
        assert [r["target_ref"] for r in refs] == [
            "Assets/Scenes/M.unity:501",
            "Assets/Scenes/M.unity:502",
            "Assets/Scenes/M.unity:503",
        ]

    def test_array_with_null_slots_drops_null_rows(self, tmp_path: Path):
        # Regression for the P1 Codex caught: previous PR1 emitted
        # ``target_kind: "null"`` for unassigned array slots, but the
        # contract enumerates exactly five kinds (component / gameobject
        # / prefab / scriptable_object / asset). Null array slots must be
        # dropped from ``references`` so every emitted row honors the
        # schema; the AI sees the gap as a hole in the index sequence.
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "Slots.cs"
        cs.write_text("public class Slots : MonoBehaviour { }")
        guid = "55" + "0" * 30
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        target_a = _node("501", "A")
        target_c = _node("503", "C")
        mb_node = _node("100", "Slots", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="200",
                properties=_mb_props(
                    guid, go_fid="100",
                    extra={"slots": [
                        {"fileID": "501"},       # real ref
                        {"fileID": 0},           # null sentinel
                        {"fileID": "503"},       # real ref
                    ]},
                ),
            )
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "M.unity",
            roots=[mb_node, target_a, target_c],
            all_nodes={"100": mb_node, "501": target_a, "503": target_c},
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/M.unity"]["references"]
        # Exactly two rows — the null slot is silently omitted.
        assert len(refs) == 2
        assert [r["index"] for r in refs] == [0, 2]
        # No "null" target_kind ever appears.
        allowed = {"component", "gameobject", "prefab",
                   "scriptable_object", "asset"}
        for r in refs:
            assert r["target_kind"] in allowed

    def test_null_reference_is_skipped(self, tmp_path: Path):
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "X.cs"
        cs.write_text("public class X : MonoBehaviour { }")
        guid = "1" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        mb_node = _node("10", "Holder", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(
                    guid, go_fid="10",
                    extra={"target": {"fileID": 0}},
                ),
            )
        ])
        scene = _scene(tmp_path / "Assets" / "Scenes" / "S.unity", roots=[mb_node])
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        # Null sentinel never emits a reference row.
        assert artifact["scenes"]["Assets/Scenes/S.unity"]["references"] == []

    def test_target_is_ui_for_component_ref_under_canvas(self, tmp_path: Path):
        # Regression for the P1 Codex caught: a MonoBehaviour field of
        # type ``Button`` (or any Component subclass) wires the *component*
        # fileID into the YAML, not the owning GameObject's. The planner
        # has to resolve the component fid back to its owning GO before
        # checking the Canvas-subtree set, or every `[SerializeField]
        # Button quitBtn` style field would be silently mislabeled
        # `target_is_ui=False`.
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "Hud.cs"
        cs.write_text("public class Hud : MonoBehaviour { }")
        guid = "ab" + "0" * 30
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        # Canvas → Button GO; Button GO holds a Button component (fid 301).
        button_comp = ComponentData(
            component_type="Button", file_id="301", properties={},
        )
        button_go = _node("300", "Button", components=[button_comp])
        canvas = _node(
            "200", "Canvas",
            components=[ComponentData(
                component_type="Canvas", file_id="201", properties={},
            )],
            children=[button_go],
        )
        button_go.parent_file_id = "200"

        # MonoBehaviour references the Button COMPONENT (fid 301), not
        # the Button GameObject (fid 300).
        mb_node = _node("10", "Binder", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(
                    guid, go_fid="10",
                    extra={"quitBtn": {"fileID": "301"}},
                ),
            )
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "UI.unity",
            roots=[canvas, mb_node],
            all_nodes={"10": mb_node, "200": canvas, "300": button_go},
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/UI.unity"]["references"]
        assert len(refs) == 1
        # POST-codex-P1: built-in component refs (Button is NOT a
        # MonoBehaviour) resolve to the OWNING GameObject -- PR2/PR4
        # only give stable lookup to GameObjects + UI instances, never
        # arbitrary component fileIDs. The host walks the resolved GO
        # for the recorded ``target_component_type``.
        assert refs[0]["target_kind"] == "gameobject"
        assert refs[0]["target_ref"] == "Assets/Scenes/UI.unity:300"
        assert refs[0]["target_is_ui"] is True
        # target_component_type is the Unity type name so PR4 knows
        # what class to find under the GameObject.
        assert refs[0].get("target_component_type") == "Button"

    def test_peer_monobehaviour_ref_keeps_component_kind(self, tmp_path: Path):
        # Codex P1 distinguishing case: a MonoBehaviour reference to ANOTHER
        # MonoBehaviour on a sibling GameObject stays ``target_kind="component"``
        # because PR2 stamps peer MBs with stable ``instance_id``s.
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs_a = scripts / "Controller.cs"
        cs_a.write_text("public class Controller : MonoBehaviour { }")
        cs_b = scripts / "Helper.cs"
        cs_b.write_text("public class Helper : MonoBehaviour { }")
        idx = _make_guid_index(
            tmp_path,
            {"a" + "0" * 31: (cs_a, "script"),
             "b" + "0" * 31: (cs_b, "script")},
        )

        # Helper MonoBehaviour on its own GameObject (fid 200).
        helper_go = _node("200", "HelperGo", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="210",
                properties=_mb_props("b" + "0" * 31, go_fid="200"),
            ),
        ])
        # Controller MonoBehaviour references the Helper COMPONENT
        # (fid 210), not the GameObject (fid 200).
        ctrl_node = _node("10", "Ctrl", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(
                    "a" + "0" * 31, go_fid="10",
                    extra={"helper": {"fileID": "210"}},
                ),
            ),
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Logic.unity",
            roots=[ctrl_node, helper_go],
            all_nodes={"10": ctrl_node, "200": helper_go},
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/Logic.unity"]["references"]
        # One ref (helper) from the Controller MB. Peer MB → stays
        # "component", target_ref is the peer's instance_id.
        helper_refs = [r for r in refs if r["field"] == "helper"]
        assert len(helper_refs) == 1
        assert helper_refs[0]["target_kind"] == "component"
        assert helper_refs[0]["target_ref"] == "Assets/Scenes/Logic.unity:210"
        # No target_component_type for peer MB refs -- the host registry
        # resolves the instance_id directly.
        assert "target_component_type" not in helper_refs[0]

    def test_target_is_ui_when_target_under_canvas(self, tmp_path: Path):
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "Bind.cs"
        cs.write_text("public class Bind : MonoBehaviour { }")
        guid = "2" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        # Canvas root with a button child; binder MB references the button.
        button = _node("300", "Button")
        canvas = _node(
            "200", "Canvas",
            components=[ComponentData(
                component_type="Canvas", file_id="201", properties={}
            )],
            children=[button],
        )
        button.parent_file_id = "200"
        mb_node = _node("10", "Binder", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(
                    guid, go_fid="10",
                    extra={"button": {"fileID": "300"}},
                ),
            )
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "UI.unity",
            roots=[canvas, mb_node],
            all_nodes={"10": mb_node, "200": canvas, "300": button},
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/UI.unity"]["references"]
        assert len(refs) == 1
        assert refs[0]["target_is_ui"] is True

    def test_instance_owner_is_ui_stamped_for_canvas_host(
        self, tmp_path: Path,
    ):
        # Classifier-v2: instance whose host GameObject lives under a
        # Canvas gets ``instance_owner_is_ui=True``. The domain classifier
        # reads this as a STRONG client signal.
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "HudPanel.cs"
        cs.write_text("public class HudPanel : MonoBehaviour { }")
        guid = "4" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        # GameObject id "10" lives directly under Canvas (id "200").
        # When the MonoBehaviour attached to "10" is walked, the
        # instance row should carry ``instance_owner_is_ui = True``.
        mb_on_panel = _node("10", "HudPanel", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(guid, go_fid="10"),
            )
        ])
        mb_on_panel.parent_file_id = "200"
        canvas = _node(
            "200", "Canvas",
            components=[ComponentData(
                component_type="Canvas", file_id="201", properties={}
            )],
            children=[mb_on_panel],
        )
        # Non-Canvas peer so we can confirm the flag isn't stamped
        # universally.
        plain = _node("30", "Plain", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="31",
                properties=_mb_props(guid, go_fid="30"),
            )
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "UI.unity",
            roots=[canvas, plain],
            all_nodes={"10": mb_on_panel, "200": canvas, "30": plain},
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        scene_block = artifact["scenes"]["Assets/Scenes/UI.unity"]
        ui_instance = next(
            i for i in scene_block["instances"]
            if i["game_object_id"].endswith(":10")
        )
        non_ui_instance = next(
            i for i in scene_block["instances"]
            if i["game_object_id"].endswith(":30")
        )
        # Per-instance flag stamped on the canvas-hosted MB but not the
        # other one.
        from typing import cast as _cast
        assert _cast(dict, ui_instance).get(
            "instance_owner_is_ui",
        ) is True
        assert "instance_owner_is_ui" not in _cast(
            dict, non_ui_instance,
        )


# ---------------------------------------------------------------------------
# Multi-scene namespacing & script_id uniqueness
# ---------------------------------------------------------------------------

class TestNamespacing:
    def test_two_scenes_use_distinct_namespaces(self, tmp_path: Path):
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True)
        cs = scripts / "Common.cs"
        cs.write_text("public class Common : MonoBehaviour { }")
        guid = "3" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        def _scene_with_mb(name: str) -> ParsedScene:
            mb_node = _node("10", "Host", components=[
                ComponentData(
                    component_type="MonoBehaviour", file_id="20",
                    properties=_mb_props(guid, go_fid="10"),
                )
            ])
            return _scene(
                tmp_path / "Assets" / "Scenes" / f"{name}.unity",
                roots=[mb_node],
            )

        artifact = plan_scene_runtime(
            parsed_scenes=[_scene_with_mb("SceneA"), _scene_with_mb("SceneB")],
            prefab_library=None,
            guid_index=idx,
            unity_project_root=tmp_path,
        )

        assert "Assets/Scenes/SceneA.unity" in artifact["scenes"]
        assert "Assets/Scenes/SceneB.unity" in artifact["scenes"]

        a_inst = artifact["scenes"]["Assets/Scenes/SceneA.unity"]["instances"][0]
        b_inst = artifact["scenes"]["Assets/Scenes/SceneB.unity"]["instances"][0]
        assert a_inst["instance_id"] != b_inst["instance_id"]
        assert a_inst["instance_id"].startswith("Assets/Scenes/SceneA.unity:")
        assert b_inst["instance_id"].startswith("Assets/Scenes/SceneB.unity:")

    def test_duplicate_stem_scripts_get_distinct_script_ids(self, tmp_path: Path):
        # Two .cs files with the same stem in different folders — the
        # planner keys modules by GUID so they remain distinguishable.
        a = tmp_path / "Assets" / "A" / "Player.cs"
        b = tmp_path / "Assets" / "B" / "Player.cs"
        for p in (a, b):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("public class Player : MonoBehaviour { }")
        guid_a = "a" * 32
        guid_b = "b" * 32
        idx = _make_guid_index(tmp_path, {
            guid_a: (a, "script"),
            guid_b: (b, "script"),
        })
        artifact = plan_scene_runtime(
            parsed_scenes=[],
            prefab_library=None,
            guid_index=idx,
            unity_project_root=tmp_path,
        )
        # Both appear in modules under distinct script_ids; the *stem* is
        # identical — which is exactly the case the require graph's
        # collision detector exists to flag.
        assert guid_a in artifact["modules"]
        assert guid_b in artifact["modules"]
        assert artifact["modules"][guid_a]["stem"] == "Player"
        assert artifact["modules"][guid_b]["stem"] == "Player"

        graph = build_require_graph(artifact["modules"])
        assert "Player" not in graph["by_stem"]
        assert "Player" in graph["collisions"]
        assert sorted(graph["collisions"]["Player"]) == sorted([guid_a, guid_b])


# ---------------------------------------------------------------------------
# Prefab subplan + stable prefab_id
# ---------------------------------------------------------------------------

class TestPrefabSubplan:
    def _prefab_with_mb(
        self,
        tmp_path: Path,
        prefab_rel_path: Path,
        prefab_name: str,
        script_guid: str,
    ) -> tuple[PrefabTemplate, Path]:
        cs = tmp_path / "Assets" / "Scripts" / f"{prefab_name}MB.cs"
        cs.parent.mkdir(parents=True, exist_ok=True)
        cs.write_text(
            f"public class {prefab_name}MB : MonoBehaviour {{ }}"
        )
        prefab_abs = tmp_path / prefab_rel_path
        prefab_abs.parent.mkdir(parents=True, exist_ok=True)
        prefab_abs.touch()

        root = PrefabNode(
            name=prefab_name, file_id="1000", active=True, tag="Untagged",
        )
        root.components = [
            ComponentData(
                component_type="MonoBehaviour", file_id="1100",
                properties=_mb_props(script_guid, go_fid="1000"),
            )
        ]
        template = PrefabTemplate(
            prefab_path=prefab_abs, name=prefab_name, root=root,
            all_nodes={"1000": root},
        )
        return template, cs

    def test_two_same_named_prefabs_in_different_folders_distinct_ids(
        self, tmp_path: Path,
    ):
        guid_a = "1" * 32
        guid_b = "2" * 32
        tmpl_a, cs_a = self._prefab_with_mb(
            tmp_path, Path("Assets/Prefabs/FolderA/Enemy.prefab"),
            "Enemy", guid_a,
        )
        tmpl_b, cs_b = self._prefab_with_mb(
            tmp_path, Path("Assets/Prefabs/FolderB/Enemy.prefab"),
            "Enemy", guid_b,
        )

        # Distinct prefab GUIDs in the guid_index — same name, different
        # folders, distinct files.
        prefab_guid_a = "11" + "0" * 30
        prefab_guid_b = "22" + "0" * 30
        idx = _make_guid_index(tmp_path, {
            guid_a: (cs_a, "script"),
            guid_b: (cs_b, "script"),
            prefab_guid_a: (tmpl_a.prefab_path, "prefab"),
            prefab_guid_b: (tmpl_b.prefab_path, "prefab"),
        })
        lib = PrefabLibrary()
        lib.prefabs.extend([tmpl_a, tmpl_b])
        lib.by_guid[prefab_guid_a] = tmpl_a
        lib.by_guid[prefab_guid_b] = tmpl_b

        artifact = plan_scene_runtime(
            parsed_scenes=[],
            prefab_library=lib,
            guid_index=idx,
            unity_project_root=tmp_path,
        )

        # Each prefab gets a unique key — bare names would have collided.
        keys = list(artifact["prefabs"].keys())
        assert len(keys) == 2
        assert (
            f"{prefab_guid_a}:Assets/Prefabs/FolderA/Enemy.prefab" in keys
        )
        assert (
            f"{prefab_guid_b}:Assets/Prefabs/FolderB/Enemy.prefab" in keys
        )
        # Each subplan carries the prefab name (informational).
        for k, sub in artifact["prefabs"].items():
            assert sub["name"] == "Enemy"
            assert len(sub["instances"]) == 1
            assert sub["instances"][0]["instance_id"].startswith(f"{k}:")
            # R2-P1.2: the bare template_name resolves a stable prefab_id
            # to the entry under ReplicatedStorage.Templates. Same-named
            # prefabs in different folders share template_name -- the
            # collision is intentional (prefab_packages emits one per
            # bare name; the planner can't pre-disambiguate without
            # changing the legacy emit path).
            assert sub["template_name"] == "Enemy", (
                f"prefab subplan must carry the bare template name "
                f"(R2-P1.2); got {sub.get('template_name')!r}"
            )

    def test_prefab_reference_from_scene_resolves_to_stable_id(
        self, tmp_path: Path,
    ):
        # Scene MB holds a serialized prefab field → planner emits a
        # reference row with target_kind=prefab and the stable prefab_id.
        script_guid = "3" * 32
        cs = tmp_path / "Assets" / "Scripts" / "Spawner.cs"
        cs.parent.mkdir(parents=True, exist_ok=True)
        cs.write_text("public class Spawner : MonoBehaviour { }")

        prefab_abs = tmp_path / "Assets" / "Prefabs" / "Foo" / "Enemy.prefab"
        prefab_abs.parent.mkdir(parents=True, exist_ok=True)
        prefab_abs.touch()
        prefab_root = PrefabNode(
            name="Enemy", file_id="1", active=True, tag="Untagged",
        )
        prefab_template = PrefabTemplate(
            prefab_path=prefab_abs, name="Enemy", root=prefab_root,
            all_nodes={"1": prefab_root},
        )
        prefab_guid = "ab" + "0" * 30
        lib = PrefabLibrary()
        lib.prefabs.append(prefab_template)
        lib.by_guid[prefab_guid] = prefab_template

        idx = _make_guid_index(tmp_path, {
            script_guid: (cs, "script"),
            prefab_guid: (prefab_abs, "prefab"),
        })

        mb_node = _node("10", "Spawner", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(
                    script_guid, go_fid="10",
                    extra={"enemyPrefab": {
                        "fileID": "1234567890", "guid": prefab_guid, "type": 3,
                    }},
                ),
            )
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Level.unity",
            roots=[mb_node],
        )

        artifact = plan_scene_runtime(
            parsed_scenes=[scene],
            prefab_library=lib,
            guid_index=idx,
            unity_project_root=tmp_path,
        )

        refs = artifact["scenes"]["Assets/Scenes/Level.unity"]["references"]
        assert len(refs) == 1
        assert refs[0]["target_kind"] == "prefab"
        assert refs[0]["target_ref"] == (
            f"{prefab_guid}:Assets/Prefabs/Foo/Enemy.prefab"
        )


# ---------------------------------------------------------------------------
# Runtime-bearing predicate
# ---------------------------------------------------------------------------

class TestRuntimeBearing:
    def test_prefab_only_monobehaviour_is_runtime_bearing(self, tmp_path: Path):
        # The MonoBehaviour is attached only to a prefab — never to a
        # scene — but the contract still classifies it as runtime-bearing
        # because instantiatePrefab will drive its lifecycle.
        cs = tmp_path / "Assets" / "Scripts" / "PrefabOnlyMB.cs"
        cs.parent.mkdir(parents=True, exist_ok=True)
        cs.write_text("public class PrefabOnlyMB : MonoBehaviour { }")
        script_guid = "9" * 32

        prefab_abs = tmp_path / "Assets" / "Prefabs" / "P.prefab"
        prefab_abs.parent.mkdir(parents=True, exist_ok=True)
        prefab_abs.touch()
        prefab_guid = "cd" + "0" * 30

        root = PrefabNode(name="P", file_id="1", active=True, tag="Untagged")
        root.components = [
            ComponentData(
                component_type="MonoBehaviour", file_id="2",
                properties=_mb_props(script_guid, go_fid="1"),
            )
        ]
        tmpl = PrefabTemplate(
            prefab_path=prefab_abs, name="P", root=root,
            all_nodes={"1": root},
        )
        lib = PrefabLibrary()
        lib.prefabs.append(tmpl)
        lib.by_guid[prefab_guid] = tmpl

        idx = _make_guid_index(tmp_path, {
            script_guid: (cs, "script"),
            prefab_guid: (prefab_abs, "prefab"),
        })

        artifact = plan_scene_runtime(
            parsed_scenes=[],
            prefab_library=lib,
            guid_index=idx,
            unity_project_root=tmp_path,
        )
        assert artifact["modules"][script_guid]["runtime_bearing"] is True

    def test_unattached_helper_is_not_runtime_bearing(self, tmp_path: Path):
        cs = tmp_path / "Assets" / "Scripts" / "Helpers.cs"
        cs.parent.mkdir(parents=True, exist_ok=True)
        cs.write_text("public static class Helpers { }")
        guid = "8" * 32
        idx = _make_guid_index(tmp_path, {guid: (cs, "script")})

        artifact = plan_scene_runtime(
            parsed_scenes=[],
            prefab_library=None,
            guid_index=idx,
            unity_project_root=tmp_path,
        )
        # Module is registered (so the require graph can resolve it by
        # stem) but not runtime-bearing.
        assert artifact["modules"][guid]["runtime_bearing"] is False
        assert artifact["modules"][guid]["class_name"] == "Helpers"


# ---------------------------------------------------------------------------
# Require graph
# ---------------------------------------------------------------------------

class TestRequireGraph:
    def test_unique_stems_resolve(self):
        modules = {
            "g1": {"stem": "Foo", "class_name": "Foo", "runtime_bearing": True},
            "g2": {"stem": "Bar", "class_name": "Bar", "runtime_bearing": False},
        }
        graph = build_require_graph(modules)
        assert graph["by_stem"] == {"Foo": "g1", "Bar": "g2"}
        assert graph["collisions"] == {}

    def test_empty_stem_modules_are_excluded(self):
        modules = {
            "g1": {"stem": "", "class_name": "", "runtime_bearing": True},
        }
        graph = build_require_graph(modules)
        assert graph["by_stem"] == {}
        assert graph["collisions"] == {}


# ---------------------------------------------------------------------------
# Bug B tier 1: pre-placed prefab instances → scene_prefab_placements
# ---------------------------------------------------------------------------

class TestScenePrefabPlacements:
    def _door_prefab(
        self, tmp_path: Path, script_guid: str,
    ) -> tuple[PrefabTemplate, Path]:
        cs = tmp_path / "Assets" / "Scripts" / "Door.cs"
        cs.parent.mkdir(parents=True, exist_ok=True)
        cs.write_text("public class Door : MonoBehaviour { }")
        prefab_abs = tmp_path / "Assets" / "Prefabs" / "Door.prefab"
        prefab_abs.parent.mkdir(parents=True, exist_ok=True)
        prefab_abs.touch()
        root = PrefabNode(
            name="Door", file_id="1000", active=True, tag="Untagged",
        )
        root.components = [
            ComponentData(
                component_type="MonoBehaviour", file_id="1100",
                properties=_mb_props(script_guid, go_fid="1000"),
            )
        ]
        template = PrefabTemplate(
            prefab_path=prefab_abs, name="Door", root=root,
            all_nodes={"1000": root},
        )
        return template, cs

    def test_single_placement_emits_row_bound_to_prefab_subplan(
        self, tmp_path: Path,
    ):
        script_guid = "d" * 32
        tmpl, cs = self._door_prefab(tmp_path, script_guid)
        prefab_guid = "dd" + "0" * 30
        idx = _make_guid_index(tmp_path, {
            script_guid: (cs, "script"),
            prefab_guid: (tmpl.prefab_path, "prefab"),
        })
        lib = PrefabLibrary()
        lib.prefabs.append(tmpl)
        lib.by_guid[prefab_guid] = tmpl

        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Level.unity", roots=[],
        )
        scene.prefab_instances = [PrefabInstanceData(
            file_id="555",
            source_prefab_guid=prefab_guid,
            source_prefab_file_id="0",
            transform_parent_file_id="",
            modifications=[],
        )]

        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )

        placements = artifact["scene_prefab_placements"]
        assert len(placements) == 1
        row = placements[0]
        # placement_id is the placement's own scene fileID, scene-namespaced.
        assert row["placement_id"] == "Assets/Scenes/Level.unity:555"
        # prefab_id matches the subplan key (so the runtime can look it up).
        assert row["prefab_id"] in artifact["prefabs"]
        assert row["prefab_id"] == f"{prefab_guid}:Assets/Prefabs/Door.prefab"
        # Tier 1 defaults active/enabled true; root placement → no parent key.
        assert row["active"] is True
        assert row["enabled"] is True
        assert "parent_game_object_id" not in row

    def test_parent_game_object_id_resolves_via_transform_fid_map(
        self, tmp_path: Path,
    ):
        script_guid = "d" * 32
        tmpl, cs = self._door_prefab(tmp_path, script_guid)
        prefab_guid = "dd" + "0" * 30
        idx = _make_guid_index(tmp_path, {
            script_guid: (cs, "script"),
            prefab_guid: (tmpl.prefab_path, "prefab"),
        })
        lib = PrefabLibrary()
        lib.prefabs.append(tmpl)
        lib.by_guid[prefab_guid] = tmpl

        # A scene parent GameObject "300", whose Transform has fileID "301".
        parent_go = _node("300", "Room")
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Level.unity", roots=[parent_go],
        )
        # The YAML parser populates this Transform-fileID → GO-fileID map.
        scene.transform_fid_to_go_fid = {"301": "300"}
        scene.prefab_instances = [PrefabInstanceData(
            file_id="555",
            source_prefab_guid=prefab_guid,
            source_prefab_file_id="0",
            transform_parent_file_id="301",  # the parent's Transform fileID
            modifications=[],
        )]

        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )

        row = artifact["scene_prefab_placements"][0]
        assert row["parent_game_object_id"] == "Assets/Scenes/Level.unity:300"

    def test_unresolvable_source_prefab_skipped(self, tmp_path: Path):
        # A placement whose source prefab GUID has no template in the
        # library produces no placement row (nothing to boot against).
        idx = _make_guid_index(tmp_path, {})
        lib = PrefabLibrary()
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Level.unity", roots=[],
        )
        scene.prefab_instances = [PrefabInstanceData(
            file_id="555",
            source_prefab_guid="f" * 32,
            source_prefab_file_id="0",
            transform_parent_file_id="",
            modifications=[],
        )]
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )
        assert artifact["scene_prefab_placements"] == []

    def test_binary_scene_emits_no_parent_when_transform_map_empty(
        self, tmp_path: Path,
    ):
        # Binary scenes don't populate transform_fid_to_go_fid
        # (binary_scene_parser.py). A placement still emits but omits the
        # parent key — documented gap, full binary placement support is
        # out of scope all tiers.
        script_guid = "d" * 32
        tmpl, cs = self._door_prefab(tmp_path, script_guid)
        prefab_guid = "dd" + "0" * 30
        idx = _make_guid_index(tmp_path, {
            script_guid: (cs, "script"),
            prefab_guid: (tmpl.prefab_path, "prefab"),
        })
        lib = PrefabLibrary()
        lib.prefabs.append(tmpl)
        lib.by_guid[prefab_guid] = tmpl
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Level.unity", roots=[],
        )
        scene.transform_fid_to_go_fid = {}  # binary scene leaves this empty
        scene.prefab_instances = [PrefabInstanceData(
            file_id="555",
            source_prefab_guid=prefab_guid,
            source_prefab_file_id="0",
            transform_parent_file_id="301",  # would-be parent, unmappable
            modifications=[],
        )]
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )
        placements = artifact["scene_prefab_placements"]
        assert len(placements) == 1
        assert "parent_game_object_id" not in placements[0]


# ---------------------------------------------------------------------------
# Phase 2a slice 5 round 2: intrinsic script_class helper
# ---------------------------------------------------------------------------

class TestDeriveIntrinsicScriptClass:
    """``derive_intrinsic_script_class`` returns the script's class as
    determined at construction time, stamped into the immutable
    ``RbxScript.intrinsic_script_type`` field by the transpiler /
    animation-gen / scriptable-object emit paths. The helper
    consults that immutable field so the answer is invariant to
    post-construction mutations of the mutable ``script_type`` (e.g.
    ``classify_storage``'s ``Script→LocalScript`` coercion). Falls back
    to ``script_type`` when ``intrinsic_script_type`` is unset (the
    rehydration / pre-field-introduction path).
    """

    def test_script_type_is_returned_as_is_via_fallback(self) -> None:
        # No intrinsic_script_type set — falls back to script_type
        # (the rehydration / pre-field-introduction path).
        s = RbxScript(name="Foo", source="return 1", script_type="LocalScript")
        assert derive_intrinsic_script_class(s) == "LocalScript"

    def test_module_script_returned_for_module(self) -> None:
        s = RbxScript(name="Foo", source="return 1", script_type="ModuleScript")
        assert derive_intrinsic_script_class(s) == "ModuleScript"

    def test_server_script_returned_for_script(self) -> None:
        s = RbxScript(name="Foo", source="return 1", script_type="Script")
        assert derive_intrinsic_script_class(s) == "Script"

    def test_none_script_defaults_to_module_script(self) -> None:
        # The orphan-row case at build_topology — no emitted script
        # matches the module's class_name. ModuleScript is the safe
        # require-target default.
        assert derive_intrinsic_script_class(None) == "ModuleScript"

    def test_empty_script_type_defaults_to_module_script(self) -> None:
        s = RbxScript(name="Foo", source="return 1", script_type="")
        assert derive_intrinsic_script_class(s) == "ModuleScript"

    # Round 2 deliverable — Test A: the WITNESS that the helper is
    # genuinely intrinsic. Construct an RbxScript whose mutable
    # script_type has been "mutated" by a simulated classify_storage
    # pass (Script → LocalScript) while the immutable
    # intrinsic_script_type retains the pre-classifier value. The
    # helper MUST return the intrinsic value, NOT the mutated one.
    # If this test fails, the helper is reading the wrong field — the
    # round-1 regression where derive_intrinsic_script_class consulted
    # script_type instead of intrinsic_script_type.
    def test_helper_returns_intrinsic_not_mutated_script_type(self) -> None:
        # Simulate the post-classify_storage shape:
        #   intrinsic_script_type stays at the transpiler's "Script"
        #   script_type was reassigned to "LocalScript" by classify_storage.
        s = RbxScript(
            name="Foo",
            source="return 1",
            script_type="LocalScript",          # post-classifier mutation
            intrinsic_script_type="Script",     # original transpile-time value
        )
        # The helper MUST report the intrinsic value, ignoring the
        # mutated script_type. This is the round-2 contract that
        # round 1 failed to provide.
        assert derive_intrinsic_script_class(s) == "Script"


# ---------------------------------------------------------------------------
# Phase 2a slice 5 step 3: RbxScript → script_id accessor
# ---------------------------------------------------------------------------

class TestBuildScriptIdByName:
    """``build_script_id_by_name`` mirrors ``build_scripts_by_class_name``
    in reverse: ``RbxScript.name → SceneRuntimeModule script_id``. Slice
    6's ``_decide_script_container_from_topology`` will use this for the
    ``TopologyModuleEntry`` lookup.

    Contract:
      - Primary join: ``script.name == module.class_name``.
      - Fallback join: ``script.name == module.stem`` (file-stem differs
        from declared class name).
      - Colliding class_names are excluded per the slice-3 degraded-
        service contract (same as ``build_scripts_by_class_name``).
    """

    def test_primary_join_by_class_name(self) -> None:
        modules: dict[str, object] = {
            "guid-a": {"stem": "Foo", "class_name": "Foo"},
            "guid-b": {"stem": "Bar", "class_name": "Bar"},
        }
        scripts = [
            RbxScript(name="Foo", source="", script_type="Script"),
            RbxScript(name="Bar", source="", script_type="LocalScript"),
        ]
        idx = build_script_id_by_name(scripts, modules)
        assert idx == {"Foo": "guid-a", "Bar": "guid-b"}

    def test_fallback_join_by_stem(self) -> None:
        # Bootstrap.cs declares class GameInit — script name matches
        # stem ("Bootstrap") but module's class_name is "GameInit".
        modules: dict[str, object] = {
            "guid-a": {"stem": "Bootstrap", "class_name": "GameInit"},
        }
        scripts = [RbxScript(name="Bootstrap", source="", script_type="Script")]
        idx = build_script_id_by_name(scripts, modules)
        assert idx == {"Bootstrap": "guid-a"}

    def test_colliding_class_names_excluded_degraded_service(self) -> None:
        # Two modules share class_name "Utils" (e.g. Utils.cs in two
        # different folders). Both rows excluded from the index per the
        # canonical ``compute_class_name_collisions`` contract — the
        # consumer falls through to orphan-routing.
        modules: dict[str, object] = {
            "guid-a": {"stem": "UtilsA", "class_name": "Utils"},
            "guid-b": {"stem": "UtilsB", "class_name": "Utils"},
            "guid-other": {"stem": "Other", "class_name": "Other"},
        }
        scripts = [
            RbxScript(name="Utils", source="", script_type="ModuleScript"),
            RbxScript(name="Other", source="", script_type="Script"),
        ]
        idx = build_script_id_by_name(scripts, modules)
        # "Utils" → excluded; "Other" → resolved.
        assert "Utils" not in idx
        assert idx == {"Other": "guid-other"}

    def test_colliding_stems_excluded_degraded_service(self) -> None:
        # Round-3 fix (Codex P3): two modules share a ``stem`` whose
        # class_name differs (e.g. ``Bootstrap.cs`` declares class
        # ``GameInit`` in one folder; another ``Bootstrap.cs`` declares
        # class ``BootSequence`` in a sibling folder). Pre-round-3 the
        # stem-fallback ``setdefault`` silently picked the first writer
        # — violating the docstring's degraded-service contract that
        # colliding join keys exclude BOTH rows. After the fix, both
        # rows fall through to the consumer's orphan-routing branch.
        modules: dict[str, object] = {
            "guid-a": {"stem": "Bootstrap", "class_name": "GameInit"},
            "guid-b": {"stem": "Bootstrap", "class_name": "BootSequence"},
            "guid-other": {"stem": "Other", "class_name": "Other"},
        }
        scripts = [
            RbxScript(name="Bootstrap", source="", script_type="Script"),
            RbxScript(name="Other", source="", script_type="Script"),
        ]
        idx = build_script_id_by_name(scripts, modules)
        # "Bootstrap" stem → excluded (collision on the fallback key);
        # neither row arbitrarily wins.
        assert "Bootstrap" not in idx
        # Primary-key lookups for the class_names themselves still work
        # (the class_name keyspace doesn't have a collision here).
        assert idx == {"Other": "guid-other"}

    def test_empty_script_names_skipped(self) -> None:
        modules: dict[str, object] = {
            "guid-a": {"stem": "Foo", "class_name": "Foo"},
        }
        scripts = [
            RbxScript(name="", source="", script_type=""),  # skipped
            RbxScript(name="Foo", source="", script_type="Script"),
        ]
        idx = build_script_id_by_name(scripts, modules)
        assert idx == {"Foo": "guid-a"}

    def test_scripts_without_module_row_omitted(self) -> None:
        # Script present but no corresponding module — the consumer
        # treats it as an orphan (slice 6's fallthrough branch).
        modules: dict[str, object] = {}
        scripts = [RbxScript(name="OrphanScript", source="", script_type="Script")]
        idx = build_script_id_by_name(scripts, modules)
        assert idx == {}

    def test_modules_without_matching_script_omitted(self) -> None:
        # Module row exists but no RbxScript synthesized for it (e.g.
        # AI-only helper or unwalkable extern base). Not in the index;
        # consumer falls through.
        modules: dict[str, object] = {
            "guid-a": {"stem": "Foo", "class_name": "Foo"},
        }
        idx = build_script_id_by_name([], modules)
        assert idx == {}


# ---------------------------------------------------------------------------
# has_character_controller -- the deterministic upstream player-avatar signal
# (a script co-located with a Unity CharacterController on a PLACED GameObject).
# ---------------------------------------------------------------------------

def _cc() -> ComponentData:
    """A Unity CharacterController component (the engine-level avatar signal)."""
    return ComponentData(
        component_type="CharacterController", file_id="900", properties={},
    )


class TestHasCharacterController:
    def test_scene_cc_colocated_script_flagged(self, tmp_path: Path) -> None:
        """A MonoBehaviour on a GameObject that also carries a CharacterController
        is flagged; one on a plain GameObject is not."""
        sdir = tmp_path / "Assets" / "Scripts"
        sdir.mkdir(parents=True)
        player_cs = sdir / "Player.cs"
        player_cs.write_text("public class Player : MonoBehaviour { }")
        enemy_cs = sdir / "Enemy.cs"
        enemy_cs.write_text("public class Enemy : MonoBehaviour { }")
        player_guid, enemy_guid = "a" * 32, "b" * 32
        idx = _make_guid_index(tmp_path, {
            player_guid: (player_cs, "script"),
            enemy_guid: (enemy_cs, "script"),
        })

        player_node = _node("100", "Player", components=[
            _cc(),
            ComponentData(
                component_type="MonoBehaviour", file_id="200",
                properties=_mb_props(player_guid, go_fid="100"),
            ),
        ])
        enemy_node = _node("300", "Enemy", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="400",
                properties=_mb_props(enemy_guid, go_fid="300"),
            ),
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Main.unity",
            roots=[player_node, enemy_node],
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        mods = artifact["modules"]
        assert mods[player_guid]["has_character_controller"] is True
        assert mods[enemy_guid]["has_character_controller"] is False

    def test_two_cc_scripts_both_flagged(self, tmp_path: Path) -> None:
        """Two distinct scripts each co-located with a CharacterController are
        BOTH flagged -- the pipeline then fail-closes on the ambiguity."""
        sdir = tmp_path / "Assets" / "Scripts"
        sdir.mkdir(parents=True)
        a_cs, b_cs = sdir / "P1.cs", sdir / "P2.cs"
        a_cs.write_text("public class P1 : MonoBehaviour { }")
        b_cs.write_text("public class P2 : MonoBehaviour { }")
        a_guid, b_guid = "1" * 32, "2" * 32
        idx = _make_guid_index(tmp_path, {
            a_guid: (a_cs, "script"), b_guid: (b_cs, "script"),
        })
        n1 = _node("10", "P1", components=[
            _cc(), ComponentData(
                component_type="MonoBehaviour", file_id="11",
                properties=_mb_props(a_guid, go_fid="10")),
        ])
        n2 = _node("20", "P2", components=[
            _cc(), ComponentData(
                component_type="MonoBehaviour", file_id="21",
                properties=_mb_props(b_guid, go_fid="20")),
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Main.unity", roots=[n1, n2],
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        mods = artifact["modules"]
        assert mods[a_guid]["has_character_controller"] is True
        assert mods[b_guid]["has_character_controller"] is True

    def test_placed_prefab_cc_flagged_unplaced_not(self, tmp_path: Path) -> None:
        """CharacterController evidence counts ONLY for PLACED prefabs: an
        unplaced library template never boots a player, so its co-located script
        must not be flagged (else it could spuriously trip the >1 gate)."""
        sdir = tmp_path / "Assets" / "Scripts"
        sdir.mkdir(parents=True)
        placed_cs = sdir / "PlacedPlayer.cs"
        placed_cs.write_text("public class PlacedPlayer : MonoBehaviour { }")
        unplaced_cs = sdir / "UnplacedPlayer.cs"
        unplaced_cs.write_text("public class UnplacedPlayer : MonoBehaviour { }")
        placed_guid, unplaced_guid = "c" * 32, "d" * 32

        def _cc_prefab(name: str, script_guid: str) -> PrefabTemplate:
            prefab_abs = tmp_path / "Assets" / "Prefabs" / f"{name}.prefab"
            prefab_abs.parent.mkdir(parents=True, exist_ok=True)
            prefab_abs.touch()
            root = PrefabNode(
                name=name, file_id="1000", active=True, tag="Untagged",
            )
            root.components = [
                _cc(),
                ComponentData(
                    component_type="MonoBehaviour", file_id="1100",
                    properties=_mb_props(script_guid, go_fid="1000"),
                ),
            ]
            return PrefabTemplate(
                prefab_path=prefab_abs, name=name, root=root,
                all_nodes={"1000": root},
            )

        placed_tmpl = _cc_prefab("PlacedPlayer", placed_guid)
        unplaced_tmpl = _cc_prefab("UnplacedPlayer", unplaced_guid)
        placed_prefab_guid = "ee" + "0" * 30
        unplaced_prefab_guid = "ff" + "0" * 30
        idx = _make_guid_index(tmp_path, {
            placed_guid: (placed_cs, "script"),
            unplaced_guid: (unplaced_cs, "script"),
            placed_prefab_guid: (placed_tmpl.prefab_path, "prefab"),
            unplaced_prefab_guid: (unplaced_tmpl.prefab_path, "prefab"),
        })
        lib = PrefabLibrary()
        lib.prefabs.extend([placed_tmpl, unplaced_tmpl])
        lib.by_guid[placed_prefab_guid] = placed_tmpl
        lib.by_guid[unplaced_prefab_guid] = unplaced_tmpl

        # Only the placed prefab is instantiated into the scene.
        scene = ParsedScene(
            scene_path=tmp_path / "Assets" / "Scenes" / "Main.unity",
            prefab_instances=[PrefabInstanceData(
                file_id="500",
                source_prefab_guid=placed_prefab_guid,
                source_prefab_file_id="0",
                transform_parent_file_id="0",
                modifications=[],
            )],
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=lib,
            guid_index=idx, unity_project_root=tmp_path,
        )
        mods = artifact["modules"]
        assert mods[placed_guid]["has_character_controller"] is True
        assert mods[unplaced_guid]["has_character_controller"] is False


# ---------------------------------------------------------------------------
# build_static_event_channels.
# Pure builder over (modules, C#-static-event map). Generic: no game-specific
# literals — the channel set is the C# enumeration, gated to same-domain
# runtime-bearing modules with a stamped module_path/container.
# ---------------------------------------------------------------------------

from converter.scene_runtime_planner import build_static_event_channels  # noqa: E402


class TestBuildStaticEventChannels:

    def _modules(self) -> dict[str, dict[str, object]]:
        return {
            "guidPlayer": {
                "runtime_bearing": True, "domain": "client",
                "container": "ReplicatedStorage",
                "module_path": "ReplicatedStorage.Player",
            },
            "guidServer": {
                "runtime_bearing": True, "domain": "server",
                "container": "ServerScriptService",
                "module_path": "ServerScriptService.GameManager",
            },
            "guidHelper": {  # excluded: helper domain
                "runtime_bearing": True, "domain": "helper",
                "container": "ReplicatedStorage",
                "module_path": "ReplicatedStorage.Helper",
            },
            "guidExcluded": {  # excluded: domain=excluded
                "runtime_bearing": True, "domain": "excluded",
                "container": "ServerStorage",
                "module_path": "ServerStorage.Dead",
            },
            "guidUnstamped": {  # excluded: no module_path yet
                "runtime_bearing": True, "domain": "client",
            },
            "guidNotBearing": {  # excluded: not runtime-bearing
                "runtime_bearing": False, "domain": "client",
                "container": "ReplicatedStorage",
                "module_path": "ReplicatedStorage.Lib",
            },
        }

    def test_emits_one_row_per_event_same_domain_only(self):
        se = {
            "guidPlayer": ["AmmoUpdate", "HealthUpdate"],
            "guidServer": ["Tick"],
            "guidHelper": ["HelperEv"],     # gated out (helper)
            "guidExcluded": ["DeadEv"],      # gated out (excluded)
            "guidUnstamped": ["NoPath"],     # gated out (no module_path)
            "guidNotBearing": ["LibEv"],     # gated out (not runtime-bearing)
        }
        channels = build_static_event_channels(self._modules(), se)
        got = {(c["module_id"], c["field_name"]) for c in channels}
        assert got == {
            ("guidPlayer", "AmmoUpdate"),
            ("guidPlayer", "HealthUpdate"),
            ("guidServer", "Tick"),
        }

    def test_row_shape_and_parent_path_is_container(self):
        from converter.scene_runtime_planner import _module_channel_folder
        se = {"guidPlayer": ["AmmoUpdate"]}
        [row] = build_static_event_channels(self._modules(), se)
        # channel_name is now the BARE field; the per-module identity lives in a
        # ``module_folder`` (opaque, derived from the UNIQUE module_id), and the
        # event is parented under it within the container.
        expected_folder = _module_channel_folder(
            "guidPlayer", "ReplicatedStorage.Player")
        assert row == {
            "module_id": "guidPlayer",
            "field_name": "AmmoUpdate",
            "channel_name": "AmmoUpdate",
            "module_folder": expected_folder,
            "parent_path": "ReplicatedStorage",
            "module_path": "ReplicatedStorage.Player",
            "domain": "client",
        }
        # The folder token is dot-free (the runtime splits parent_path on ".").
        assert "." not in expected_folder

    def test_same_field_two_modules_same_container_distinct_channels(self):
        # Cross-class channel aliasing: two DIFFERENT classes declaring
        # the SAME static-event member name in the SAME container must NOT alias
        # onto one BindableEvent. With the bare ``channel_name`` + a per-module
        # ``module_folder``, each class's event lives under a DISTINCT folder, so
        # ``findOrCreateChannel`` returns DISTINCT instances. Each row's
        # ``field_name`` + ``channel_name`` stay the bare member name; the
        # uniqueness is carried by ``module_folder``.
        modules = {
            "guidPlayer": {
                "runtime_bearing": True, "domain": "client",
                "container": "ReplicatedStorage",
                "module_path": "ReplicatedStorage.Player",
            },
            "guidEnemy": {
                "runtime_bearing": True, "domain": "client",
                "container": "ReplicatedStorage",
                "module_path": "ReplicatedStorage.Enemy",
            },
        }
        se = {"guidPlayer": ["AmmoUpdate"], "guidEnemy": ["AmmoUpdate"]}
        channels = build_static_event_channels(modules, se)
        # Both keep the bare field name AND the bare channel name…
        assert {c["field_name"] for c in channels} == {"AmmoUpdate"}
        assert {c["channel_name"] for c in channels} == {"AmmoUpdate"}
        # …but the per-module folders (hence the full channel locations) are
        # DISTINCT — no aliasing.
        folders = sorted(c["module_folder"] for c in channels)
        assert len(set(folders)) == 2
        locations = {(c["parent_path"], c["module_folder"], c["channel_name"])
                     for c in channels}
        assert len(locations) == 2

    def test_flat_concat_delimiter_ambiguity_distinct_channels(self):
        # Delimiter-ambiguity collision class: a flat ``<stem>_<field>`` concat
        # aliases ``stem="A_B",field="C"`` with ``stem="A",field="B_C"`` (both →
        # ``A_B_C``). The structured per-module folder identity eliminates this:
        # each module gets a DISTINCT folder keyed on its UNIQUE module_id, so the
        # two channels never collapse onto one location.
        modules = {
            "guidAB": {
                "runtime_bearing": True, "domain": "client",
                "container": "ReplicatedStorage",
                "module_path": "ReplicatedStorage.A_B",
            },
            "guidA": {
                "runtime_bearing": True, "domain": "client",
                "container": "ReplicatedStorage",
                "module_path": "ReplicatedStorage.A",
            },
        }
        se = {"guidAB": ["C"], "guidA": ["B_C"]}
        channels = build_static_event_channels(modules, se)
        assert len(channels) == 2
        # The full channel LOCATIONS (parent_path, module_folder, channel_name)
        # must be DISTINCT — the flat concat would have collapsed them.
        locations = {(c["parent_path"], c["module_folder"], c["channel_name"])
                     for c in channels}
        assert len(locations) == 2, (
            f"delimiter-ambiguous channels must not collide: {channels}")
        # And the dot-free folder tokens are distinct (keyed on module_id).
        folders = {c["module_folder"] for c in channels}
        assert len(folders) == 2
        assert all("." not in f for f in folders)

    def test_idempotent(self):
        se = {"guidPlayer": ["AmmoUpdate", "HealthUpdate"], "guidServer": ["Tick"]}
        modules = self._modules()
        first = build_static_event_channels(modules, se)
        second = build_static_event_channels(modules, se)
        assert first == second

    def test_empty_event_list_emits_nothing(self):
        channels = build_static_event_channels(self._modules(), {"guidPlayer": []})
        assert channels == []

    def test_unknown_module_id_skipped(self):
        channels = build_static_event_channels(
            self._modules(), {"guidGhost": ["X"]},
        )
        assert channels == []
# Slice 1.2 — prefab_id 3-way parity (AC14 / D6c / D11)
# ---------------------------------------------------------------------------

class TestPrefabStableIdThreeWayParity:
    """The planner ``_prefab_stable_id``, the emitter
    ``scene_converter._prefab_stable_id``, and the resolver's
    ``prefab_id_for_guid`` produce a byte-identical id for the same prefab,
    pinning the join key directly rather than only catching skew at
    integration."""

    def _build(self, tmp_path: Path, rel: str, name: str, guid: str):
        from core.unity_types import PrefabLibrary
        prefab_abs = tmp_path / rel
        prefab_abs.parent.mkdir(parents=True, exist_ok=True)
        prefab_abs.touch()
        template = PrefabTemplate(
            prefab_path=prefab_abs, name=name,
            root=PrefabNode(name=name, file_id="1", active=True, tag="Untagged"),
            all_nodes={},
        )
        lib = PrefabLibrary()
        lib.prefabs.append(template)
        lib.by_guid[guid] = template
        idx = _make_guid_index(tmp_path, {guid: (prefab_abs, "prefab")})
        return template, lib, idx

    def test_inside_root_three_way_identical(self, tmp_path: Path):
        from converter.scene_converter import _prefab_stable_id as conv_id
        from converter.scene_runtime_planner import _prefab_stable_id as plan_id
        from unity.addressables_resolver import resolve_prefab_addressables
        from unity.addressables_resolver import AddressablesIndex

        guid = "473ffa01" + "0" * 24
        rel = "Assets/Bundles/Characters/Cat/character.prefab"
        template, lib, idx = self._build(tmp_path, rel, "character", guid)

        plan = plan_id(template, idx, lib.by_guid, tmp_path)
        conv = conv_id(template, idx, lib.by_guid, tmp_path)
        # Resolver side: build an index with this guid as an addressable
        # prefab and confirm the prefab_id it derives matches.
        index = AddressablesIndex()
        index.by_address["Cat"] = [guid]
        resolved = resolve_prefab_addressables(index, idx)
        res = resolved.by_address["Cat"][0]

        assert plan == conv == res == f"{guid}:{rel}"

    def _resolver_id(self, guid: str, guid_index: object) -> str | None:
        """Drive the REAL resolver path (``resolve_prefab_addressables`` ->
        ``prefab_id_for_guid``) for ``guid`` and return the id it derives, or
        ``None`` when the resolver drops it. Computes the id the production way
        (via ``canonical_prefab_id`` against ``guid_index.project_root``), not a
        hand-built string."""
        from unity.addressables_resolver import (
            AddressablesIndex,
            resolve_prefab_addressables,
        )
        index = AddressablesIndex()
        index.by_address["addr"] = [guid]
        resolved = resolve_prefab_addressables(index, guid_index)
        ids = resolved.by_address.get("addr")
        return ids[0] if ids else None

    def test_outside_root_three_way_empty_string(self, tmp_path: Path):
        """A prefab outside the project root: planner, emitter, and resolver
        all skip it byte-identically — planner/emitter return ``""``
        (skip-stamping) and the resolver drops the address (``None``)."""
        from converter.scene_converter import _prefab_stable_id as conv_id
        from converter.scene_runtime_planner import _prefab_stable_id as plan_id

        project_root = tmp_path / "proj"
        project_root.mkdir()
        external = tmp_path / "external"
        external.mkdir()
        prefab_abs = external / "Loose.prefab"
        prefab_abs.touch()
        guid = "2ae64d0e" + "0" * 24
        template = PrefabTemplate(
            prefab_path=prefab_abs, name="Loose",
            root=PrefabNode(name="Loose", file_id="1", active=True, tag="Untagged"),
            all_nodes={},
        )
        from core.unity_types import PrefabLibrary
        lib = PrefabLibrary()
        lib.by_guid[guid] = template
        idx = _make_guid_index(project_root, {guid: (prefab_abs, "prefab")})

        plan = plan_id(template, idx, lib.by_guid, project_root)
        conv = conv_id(template, idx, lib.by_guid, project_root)
        res = self._resolver_id(guid, idx)
        # planner/emitter emit "" (skip), resolver drops to None — all three
        # agree the outside-root prefab produces no usable join key.
        assert plan == conv == ""
        assert res is None

    def test_project_root_none_three_way_guid_only(self, tmp_path: Path):
        """project_root=None: planner, emitter, and resolver all short-circuit
        to the bare guid (no path segment), byte-identical. The resolver
        reaches that branch via a guid_index whose ``project_root`` is None."""
        from types import SimpleNamespace

        from converter.scene_converter import _prefab_stable_id as conv_id
        from converter.scene_runtime_planner import _prefab_stable_id as plan_id

        guid = "1" * 32
        rel = "Assets/Prefabs/NoRoot.prefab"
        template, lib, idx = self._build(tmp_path, rel, "NoRoot", guid)

        plan = plan_id(template, idx, lib.by_guid, None)
        conv = conv_id(template, idx, lib.by_guid, None)
        # Resolver with a None project_root (root-less index) -> bare guid.
        rootless = SimpleNamespace(
            project_root=None,
            guid_to_entry=dict(idx.guid_to_entry),
        )
        res = self._resolver_id(guid, rootless)
        assert plan == conv == res == guid

    def test_no_guid_three_way_rel_only(self, tmp_path: Path):
        """No GUID resolvable but a project-relative path: planner and emitter
        both return the bare project-relative path (no ``guid:`` prefix),
        byte-identical. (The resolver always keys on a known guid, so its
        no-guid leg is the inside-root path covered above.)"""
        from converter.scene_converter import _prefab_stable_id as conv_id
        from converter.scene_runtime_planner import _prefab_stable_id as plan_id
        from unity.prefab_id import canonical_prefab_id

        rel = "Assets/Prefabs/Anon.prefab"
        prefab_abs = tmp_path / rel
        prefab_abs.parent.mkdir(parents=True, exist_ok=True)
        prefab_abs.touch()
        template = PrefabTemplate(
            prefab_path=prefab_abs, name="Anon",
            root=PrefabNode(name="Anon", file_id="1", active=True, tag="Untagged"),
            all_nodes={},
        )
        from core.unity_types import PrefabLibrary
        lib = PrefabLibrary()
        # No by_guid entry and an empty guid_index -> guid unresolvable.
        idx = _make_guid_index(tmp_path, {})

        plan = plan_id(template, idx, lib.by_guid, tmp_path)
        conv = conv_id(template, idx, lib.by_guid, tmp_path)
        # Shared core with no guid -> bare project-relative path.
        assert plan == conv == canonical_prefab_id("", prefab_abs, tmp_path) == rel


# ---------------------------------------------------------------------------
# Slice 1.2 — pipeline bridge + resolved-name pass (AC3 / AC4 / AC5 / AC8)
# ---------------------------------------------------------------------------

_ADDR_GROUP = """\
%YAML 1.1
%TAG !u! tag:unity3d.com,2011:
--- !u!114 &11400000
MonoBehaviour:
  m_Name: Characters
  m_GroupName: Characters
  m_SerializeEntries:
  - m_GUID: {cat}
    m_Address: Trash Cat
    m_SerializedLabels:
    - characters
  - m_GUID: {raccoon}
    m_Address: Rubbish Raccoon
    m_SerializedLabels:
    - characters
"""


class TestPlanSceneRuntimePipelineBridge:
    """Drive the REAL ``Pipeline.plan_scene_runtime`` (bridge + resolved-name
    pass) over a fixture project — AC3/AC4/AC5 + the AC8 resume guarantee."""

    CAT_GUID = "473ffa01" + "0" * 24
    RACCOON_GUID = "2ae64d0e" + "0" * 24
    ICON_GUID = "abcdef00" + "0" * 24

    def _make_pipeline(self, tmp_path: Path):
        from converter.pipeline import Pipeline, PipelineState
        from core.conversion_context import ConversionContext
        from core.unity_types import PrefabLibrary

        # --- Prefab files on disk ---
        specs = {
            self.CAT_GUID: ("Assets/Bundles/Characters/Cat/character.prefab", "character"),
            self.RACCOON_GUID: ("Assets/Bundles/Characters/Raccoon/character.prefab", "character"),
            self.ICON_GUID: ("Assets/Prefabs/IconConsumable.prefab", "IconConsumable"),
        }
        lib = PrefabLibrary()
        guid_entries: dict[str, tuple[Path, str]] = {}
        for guid, (rel, name) in specs.items():
            prefab_abs = tmp_path / rel
            prefab_abs.parent.mkdir(parents=True, exist_ok=True)
            prefab_abs.touch()
            template = PrefabTemplate(
                prefab_path=prefab_abs, name=name,
                root=PrefabNode(name=name, file_id="1", active=True, tag="Untagged"),
                all_nodes={},
            )
            lib.prefabs.append(template)
            lib.by_guid[guid] = template
            guid_entries[guid] = (prefab_abs, "prefab")
        idx = _make_guid_index(tmp_path, guid_entries)

        # --- Addressables group (Cat + Raccoon only) ---
        groups = tmp_path / "Assets" / "AddressableAssetsData" / "AssetGroups"
        groups.mkdir(parents=True)
        (groups / "Characters.asset").write_text(
            _ADDR_GROUP.format(cat=self.CAT_GUID, raccoon=self.RACCOON_GUID),
            encoding="utf-8",
        )

        p = Pipeline.__new__(Pipeline)
        p.unity_project_path = tmp_path
        p.ctx = ConversionContext(unity_project_path=str(tmp_path))
        # IconConsumable is REFERENCED (not addressable) — drives the
        # non-colliding bare-name leg of AC3.
        p.ctx.serialized_field_refs = {
            "some-go": {"icon": "IconConsumable"},
        }
        state = PipelineState()
        state.prefab_library = lib
        state.guid_index = idx
        state.parsed_scene = None
        state.all_parsed_scenes = []
        p.state = state
        return p

    def test_resolved_names_and_addressables_block(self, tmp_path: Path):
        p = self._make_pipeline(tmp_path)
        p.plan_scene_runtime()
        sr = p.ctx.scene_runtime

        cat_id = f"{self.CAT_GUID}:Assets/Bundles/Characters/Cat/character.prefab"
        raccoon_id = f"{self.RACCOON_GUID}:Assets/Bundles/Characters/Raccoon/character.prefab"
        icon_id = f"{self.ICON_GUID}:Assets/Prefabs/IconConsumable.prefab"

        prefabs = sr["prefabs"]
        assert isinstance(prefabs, dict)

        # AC3: colliding pair gets DISTINCT resolved template_names...
        cat_name = prefabs[cat_id]["template_name"]
        raccoon_name = prefabs[raccoon_id]["template_name"]
        assert cat_name == "character__473ffa"
        assert raccoon_name == "character__2ae64d"
        assert cat_name != raccoon_name
        # ...and the non-colliding referenced prefab stays BARE.
        assert prefabs[icon_id]["template_name"] == "IconConsumable"

        # AC4: addressables block present, list semantics, singleton for Cat.
        addr = sr["addressables"]
        assert addr["by_address"]["Trash Cat"] == [cat_id]
        assert addr["by_address"]["Rubbish Raccoon"] == [raccoon_id]
        assert set(addr["by_label"]["characters"]) == {cat_id, raccoon_id}

    def test_no_addressables_block_when_no_groups(self, tmp_path: Path):
        """No AddressableAssetsData dir → bridge returns None → no block,
        and colliding prefabs that are NOT referenced/addressable are not
        in the emitted set, so their template_name stays bare (edge 10/12)."""
        p = self._make_pipeline(tmp_path)
        import shutil
        shutil.rmtree(tmp_path / "Assets" / "AddressableAssetsData")
        p.plan_scene_runtime()
        sr = p.ctx.scene_runtime
        assert "addressables" not in sr
        icon_id = f"{self.ICON_GUID}:Assets/Prefabs/IconConsumable.prefab"
        # IconConsumable is referenced → emitted → unique → bare.
        assert sr["prefabs"][icon_id]["template_name"] == "IconConsumable"
        # Cat/Raccoon are neither referenced nor addressable now → NOT in
        # the emitted set → their bare template_name is left untouched.
        cat_id = f"{self.CAT_GUID}:Assets/Bundles/Characters/Cat/character.prefab"
        assert sr["prefabs"][cat_id]["template_name"] == "character"

    def test_planner_alone_leaves_bare_names(self, tmp_path: Path):
        """The planner module (NOT the pipeline) must keep BARE template
        names — the resolved-name pass lives in the pipeline, where the
        addressable set is known (Slice 1.2 interface)."""
        p = self._make_pipeline(tmp_path)
        artifact = plan_scene_runtime(
            parsed_scenes=[], prefab_library=p.state.prefab_library,
            guid_index=p.state.guid_index, unity_project_root=tmp_path,
        )
        for sub in artifact["prefabs"].values():
            assert sub["template_name"] in ("character", "IconConsumable")
        # Both colliding prefabs still carry the BARE "character".
        bare = [s["template_name"] for s in artifact["prefabs"].values()
                if s["name"] == "character"]
        assert bare == ["character", "character"]


class TestPlanSceneRuntimeIsEssential:
    """AC8 — the resume RECOMPUTE guarantee."""

    def test_plan_scene_runtime_in_essential_phases(self):
        from converter.pipeline import Pipeline
        assert "plan_scene_runtime" in Pipeline.ESSENTIAL_PHASES

    def test_resume_recomputes_addressables_over_stale_persisted_block(
        self, tmp_path: Path,
    ):
        """A ``--phase write_output`` resume re-runs ``plan_scene_runtime``
        (it is ESSENTIAL), so a stale persisted ``ctx.scene_runtime`` block is
        recomputed fresh rather than paired with a fresh ``prefab_library``;
        the real emitter then emits the addressable templates under the fresh
        resolved names. Drives the real recompute + the real
        ``generate_prefab_packages`` so a reused stale block would fail it."""
        from converter.prefab_packages import generate_prefab_packages

        p = TestPlanSceneRuntimePipelineBridge()._make_pipeline(tmp_path)

        cat_id = f"{TestPlanSceneRuntimePipelineBridge.CAT_GUID}:Assets/Bundles/Characters/Cat/character.prefab"
        raccoon_id = f"{TestPlanSceneRuntimePipelineBridge.RACCOON_GUID}:Assets/Bundles/Characters/Raccoon/character.prefab"

        # Persisted state from a PRIOR run, deliberately STALE/poisoned: a wrong
        # resolved name + a wrong addressables block + a phantom prefab id.
        p.ctx.scene_runtime = {
            "prefabs": {
                cat_id: {"name": "character", "template_name": "STALE_WRONG"},
                "PHANTOM:Assets/Gone.prefab": {
                    "name": "ghost", "template_name": "ghost",
                },
            },
            "addressables": {
                "by_address": {"Trash Cat": ["PHANTOM:Assets/Gone.prefab"]},
                "by_label": {},
            },
        }

        # The resume re-runs the essential planner → recompute.
        p.plan_scene_runtime()
        sr = p.ctx.scene_runtime

        # Stale poison is GONE — recomputed fresh, not reused.
        assert sr["prefabs"][cat_id]["template_name"] == "character__473ffa"
        assert "PHANTOM:Assets/Gone.prefab" not in sr["prefabs"]
        assert sr["addressables"]["by_address"]["Trash Cat"] == [cat_id]
        assert sr["addressables"]["by_address"]["Trash Cat"] != [
            "PHANTOM:Assets/Gone.prefab"
        ]

        # The emitter, reading the recomputed ctx (mirrors
        # Pipeline._generate_prefab_packages), emits the addressable templates
        # under the FRESH resolved names.
        resolved_template_names = {
            pid: sub["template_name"]
            for pid, sub in sr["prefabs"].items()
            if isinstance(sub, dict) and isinstance(sub.get("template_name"), str)
        }
        addressable_prefab_ids: set[str] = set()
        for axis in ("by_address", "by_label"):
            for ids in (sr["addressables"].get(axis) or {}).values():
                addressable_prefab_ids.update(
                    pid for pid in ids if isinstance(pid, str)
                )

        result = generate_prefab_packages(
            prefab_library=p.state.prefab_library,
            serialized_field_refs=p.ctx.serialized_field_refs or None,
            guid_index=p.state.guid_index,
            resolved_template_names=resolved_template_names,
            addressable_prefab_ids=addressable_prefab_ids,
        )
        emitted_names = {t.name for t in result.templates}
        # The addressable Cat/Raccoon templates are emitted under the FRESH
        # (recomputed) resolved names, and the stale name never appears.
        assert "character__473ffa" in emitted_names
        assert "character__2ae64d" in emitted_names
        assert "STALE_WRONG" not in emitted_names


class TestAddressablesReachesEmbeddedPlan:
    """AC5 — ``addressables`` is in the host allowlist and renders into the
    embedded ``SceneRuntimePlan`` ModuleScript."""

    def test_addressables_in_plan_keys_for_host(self):
        from converter.autogen import _PLAN_KEYS_FOR_HOST
        assert "addressables" in _PLAN_KEYS_FOR_HOST

    def test_block_renders_with_bracket_quoted_address(self):
        from converter.autogen import generate_scene_runtime_plan_module
        artifact = {
            "modules": {}, "scenes": {}, "prefabs": {},
            "domain_overrides": {}, "scriptable_objects": {},
            "scene_prefab_placements": {},
            "addressables": {
                "by_address": {"Trash Cat": ["catid:Assets/Cat.prefab"]},
                "by_label": {"characters": ["catid:Assets/Cat.prefab"]},
            },
        }
        script = generate_scene_runtime_plan_module(artifact)
        assert "addressables" in script.source
        # Non-identifier key must be bracket-quoted.
        assert '["Trash Cat"]' in script.source
        assert "catid:Assets/Cat.prefab" in script.source


# ---------------------------------------------------------------------------
# Phase 3 / Slice 3.2 — stripped-prefab-instance component-ref resolution
# (post-pass ``_resolve_stripped_refs`` rewriting unresolvable rows in place).
# AC2 (exact resolve + RED), AC5 (fail-closed), AC4 (no regression).
# ---------------------------------------------------------------------------

# Real Trash-Dash project (path-guarded — CI without the source skips).
_TRASH_DASH = Path("/Users/jiazou/workspace/trash-dash")
_TRASH_DASH_MAIN = _TRASH_DASH / "Assets" / "Scenes" / "Main.unity"


class TestStrippedRefResolution:
    """Slice 3.2 — the planner post-pass rewrites unresolvable stripped-MB
    component refs to the runtime engine-union key, fail-closed."""

    # The three deterministic upstream facts that make the bridge resolvable,
    # reproducing the real Trash-Dash shape (LoadoutState.missionPopup ->
    # MissionUI on a placed MissionPopup prefab instance).
    _SCRIPT_GUID = "ffff" + "0" * 28        # MissionUI.cs guid (the source MB class)
    _SRC_OBJ_FID = "114000011972273750"     # m_CorrespondingSourceObject.fileID
    _PI_FID = "1822972501"                  # m_PrefabInstance.fileID (the placement)
    _STRIPPED_FID = "137514649"             # scene-local stripped MB fileID
    _PREFAB_GUID = "a53f" + "0" * 28

    def _build(
        self,
        tmp_path: Path,
        *,
        # knobs for the fail-closed AC5 variants:
        emit_placement: bool = True,
        subplan_has_instance: bool = True,
        script_guid_matches: bool = True,
        source_prefab_guid_matches: bool = True,
        subplan_in_block: bool = True,
    ) -> tuple[list[ParsedScene], PrefabLibrary, GuidIndex]:
        """Assemble parsed inputs reproducing the real stripped-ref shape.

        - A scene with a ``LoadoutState`` MB whose ``missionPopup`` field is a
          fileID-only ref to a stripped MB (``_STRIPPED_FID``).
        - A scene ``PrefabInstance`` (``_PI_FID``) of MissionPopup.prefab ->
          emits a placement.
        - A MissionPopup prefab subplan containing a ``MissionUI`` MB at
          component fileID ``_SRC_OBJ_FID``.
        - ``scene.stripped_components`` carrying the bridge identity (as 3.1's
          parser would).
        """
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        loadout_cs = scripts / "LoadoutState.cs"
        loadout_cs.write_text("public class LoadoutState : MonoBehaviour { }")
        mission_cs = scripts / "MissionUI.cs"
        mission_cs.write_text("public class MissionUI : MonoBehaviour { }")
        # The prefab subplan's MB resolves to this guid; AC5 mismatch variant
        # points the subplan at a DIFFERENT class so the fail-closed check fires.
        subplan_script_guid = (
            self._SCRIPT_GUID if script_guid_matches else ("dead" + "0" * 28)
        )
        if not script_guid_matches:
            wrong_cs = scripts / "WrongClass.cs"
            wrong_cs.write_text("public class WrongClass : MonoBehaviour { }")

        loadout_guid = "100a" + "0" * 28
        loadout_cs.write_text("public class LoadoutState : MonoBehaviour { }")

        # Prefab template (MissionPopup) with a MissionUI MB at _SRC_OBJ_FID.
        prefab_abs = tmp_path / "Assets" / "Prefabs" / "UI" / "MissionPopup.prefab"
        prefab_abs.parent.mkdir(parents=True, exist_ok=True)
        prefab_abs.touch()
        prefab_root = PrefabNode(
            name="MissionPopup", file_id="9000", active=True, tag="Untagged",
        )
        prefab_props = _mb_props(subplan_script_guid, go_fid="9000")
        prefab_root.components = []
        if subplan_has_instance:
            prefab_root.components = [
                ComponentData(
                    component_type="MonoBehaviour", file_id=self._SRC_OBJ_FID,
                    properties=prefab_props,
                )
            ]
        template = PrefabTemplate(
            prefab_path=prefab_abs, name="MissionPopup", root=prefab_root,
            all_nodes={"9000": prefab_root},
        )

        guid_entries: dict[str, tuple[Path, str]] = {
            loadout_guid: (loadout_cs, "script"),
            self._SCRIPT_GUID: (mission_cs, "script"),
            self._PREFAB_GUID: (prefab_abs, "prefab"),
        }
        if not script_guid_matches:
            guid_entries[subplan_script_guid] = (
                scripts / "WrongClass.cs", "script",
            )
        idx = _make_guid_index(tmp_path, guid_entries)

        lib = PrefabLibrary()
        # ``by_guid`` is what ``_walk_scene_prefab_placements`` consults to EMIT
        # the placement; ``prefabs`` is the list ``plan_scene_runtime`` iterates
        # to BUILD ``prefabs_block``. With ``subplan_in_block=False`` the template
        # stays in ``by_guid`` (so the placement IS emitted) but is dropped from
        # ``prefabs`` (so ``prefabs_block`` lacks that prefab_id) -> the post-pass
        # hits the ``subplan is None`` fail-closed branch.
        if subplan_in_block:
            lib.prefabs.append(template)
        lib.by_guid[self._PREFAB_GUID] = template

        # Scene: a LoadoutState MB whose missionPopup -> the stripped fileID.
        loadout_go = _node("100", "LoadoutGO", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="110",
                properties=_mb_props(
                    loadout_guid, go_fid="100",
                    extra={"missionPopup": {"fileID": self._STRIPPED_FID}},
                ),
            ),
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Main.unity",
            roots=[loadout_go], all_nodes={"100": loadout_go},
        )
        # The fail-closed source-prefab-guid gate compares the placement's
        # prefab guid (== ``_PREFAB_GUID``) against this recorded value. The
        # mismatch variant points it at a DIFFERENT prefab guid so the gate
        # fires even though the pi_fid + src_obj_fid + script_guid all line up.
        recorded_source_guid = (
            self._PREFAB_GUID if source_prefab_guid_matches
            else ("beef" + "0" * 28)
        )
        scene.stripped_components = {
            self._STRIPPED_FID: StrippedComponentRecord(
                file_id=self._STRIPPED_FID, class_id=114,
                source_object_file_id=self._SRC_OBJ_FID,
                source_object_guid=recorded_source_guid,
                prefab_instance_file_id=self._PI_FID,
                script_guid=self._SCRIPT_GUID,
            )
        }
        if emit_placement:
            scene.prefab_instances = [PrefabInstanceData(
                file_id=self._PI_FID,
                source_prefab_guid=self._PREFAB_GUID,
                source_prefab_file_id="0",
                transform_parent_file_id="",
                modifications=[],
            )]
        return [scene], lib, idx

    def _missionpopup_row(self, artifact: dict) -> dict:
        refs = artifact["scenes"]["Assets/Scenes/Main.unity"]["references"]
        rows = [r for r in refs if r["field"] == "missionPopup"]
        assert len(rows) == 1
        return rows[0]

    # --- AC2: exact resolve + RED proof -------------------------------------

    def test_stripped_ref_resolves_to_engine_union_key(self, tmp_path: Path):
        scenes, lib, idx = self._build(tmp_path)
        artifact = plan_scene_runtime(
            parsed_scenes=scenes, prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )
        row = self._missionpopup_row(artifact)
        prefab_id = f"{self._PREFAB_GUID}:Assets/Prefabs/UI/MissionPopup.prefab"
        expected_ref = (
            f"Assets/Scenes/Main.unity:{self._PI_FID}:"
            f"{prefab_id}:{self._SRC_OBJ_FID}"
        )
        assert row["target_kind"] == "component"
        assert row["target_ref"] == expected_ref
        assert row["target_script_id"] == self._SCRIPT_GUID

    def test_stripped_ref_is_unresolvable_fallback_without_postpass(
        self, tmp_path: Path,
    ):
        """RED proof: with the post-pass skipped, the row stays the
        unresolvable scene-local fallback ``<ns>:<stripped_fid>``."""
        import converter.scene_runtime_planner as planner
        scenes, lib, idx = self._build(tmp_path)
        orig = planner._resolve_stripped_refs
        planner._resolve_stripped_refs = lambda *a, **k: None  # type: ignore[assignment]
        try:
            artifact = plan_scene_runtime(
                parsed_scenes=scenes, prefab_library=lib, guid_index=idx,
                unity_project_root=tmp_path,
            )
        finally:
            planner._resolve_stripped_refs = orig  # type: ignore[assignment]
        row = self._missionpopup_row(artifact)
        assert row["target_ref"] == (
            f"Assets/Scenes/Main.unity:{self._STRIPPED_FID}"
        )
        assert "target_script_id" not in row

    # --- AC5: fail-closed branches ------------------------------------------

    def test_fail_closed_when_no_placement(self, tmp_path: Path):
        scenes, lib, idx = self._build(tmp_path, emit_placement=False)
        artifact = plan_scene_runtime(
            parsed_scenes=scenes, prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )
        row = self._missionpopup_row(artifact)
        assert row["target_ref"] == (
            f"Assets/Scenes/Main.unity:{self._STRIPPED_FID}"
        )
        assert "target_script_id" not in row

    def test_fail_closed_when_subplan_lacks_instance(self, tmp_path: Path):
        scenes, lib, idx = self._build(tmp_path, subplan_has_instance=False)
        artifact = plan_scene_runtime(
            parsed_scenes=scenes, prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )
        row = self._missionpopup_row(artifact)
        assert row["target_ref"] == (
            f"Assets/Scenes/Main.unity:{self._STRIPPED_FID}"
        )
        assert "target_script_id" not in row

    def test_fail_closed_when_script_guid_mismatch(self, tmp_path: Path):
        scenes, lib, idx = self._build(tmp_path, script_guid_matches=False)
        artifact = plan_scene_runtime(
            parsed_scenes=scenes, prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )
        row = self._missionpopup_row(artifact)
        assert row["target_ref"] == (
            f"Assets/Scenes/Main.unity:{self._STRIPPED_FID}"
        )
        assert "target_script_id" not in row

    def test_fail_closed_when_source_prefab_guid_mismatch(self, tmp_path: Path):
        """A stripped ref whose recorded ``source_object_guid`` does NOT match
        the placement's prefab guid keeps the unresolvable fallback (the new
        prefab-identity fail-closed gate fires)."""
        scenes, lib, idx = self._build(
            tmp_path, source_prefab_guid_matches=False,
        )
        artifact = plan_scene_runtime(
            parsed_scenes=scenes, prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )
        row = self._missionpopup_row(artifact)
        assert row["target_ref"] == (
            f"Assets/Scenes/Main.unity:{self._STRIPPED_FID}"
        )
        assert "target_script_id" not in row

    def test_fail_closed_when_subplan_absent_from_prefabs_block(
        self, tmp_path: Path,
    ):
        """The placement EXISTS (its prefab guid is in ``by_guid`` so
        ``_walk_scene_prefab_placements`` emits it) but the prefab subplan is NOT
        in ``prefabs_block`` (the template was filtered out of
        ``prefab_library.prefabs``). The post-pass hits ``subplan is None`` and
        keeps the unresolvable scene-local fallback (distinct from the
        no-placement and subplan-lacks-instance branches)."""
        scenes, lib, idx = self._build(tmp_path, subplan_in_block=False)
        # Guard the fixture: a placement IS emitted (the branch under test is
        # only reachable once the placement + prefab-guid gates have passed).
        artifact = plan_scene_runtime(
            parsed_scenes=scenes, prefab_library=lib, guid_index=idx,
            unity_project_root=tmp_path,
        )
        prefab_id = f"{self._PREFAB_GUID}:Assets/Prefabs/UI/MissionPopup.prefab"
        placements = artifact["scene_prefab_placements"]
        assert any(p["prefab_id"] == prefab_id for p in placements), (
            "fixture invariant: the placement must be emitted so the post-pass "
            "reaches the subplan lookup"
        )
        assert prefab_id not in artifact["prefabs"], (
            "fixture invariant: the prefab subplan must be ABSENT from "
            "prefabs_block (the branch under test)"
        )
        row = self._missionpopup_row(artifact)
        assert row["target_ref"] == (
            f"Assets/Scenes/Main.unity:{self._STRIPPED_FID}"
        )
        assert "target_script_id" not in row

    # --- AC4: no regression on non-stripped refs ----------------------------

    def test_peer_component_ref_untouched_by_postpass(self, tmp_path: Path):
        """A normal peer-MonoBehaviour ref (NOT in stripped_components) is
        resolved by the existing branch and never rewritten by the post-pass."""
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        cs_a = scripts / "Controller.cs"
        cs_a.write_text("public class Controller : MonoBehaviour { }")
        cs_b = scripts / "Helper.cs"
        cs_b.write_text("public class Helper : MonoBehaviour { }")
        idx = _make_guid_index(tmp_path, {
            "aa" + "0" * 30: (cs_a, "script"),
            "bb" + "0" * 30: (cs_b, "script"),
        })
        helper_go = _node("200", "HelperGo", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="210",
                properties=_mb_props("bb" + "0" * 30, go_fid="200"),
            ),
        ])
        ctrl_go = _node("10", "Ctrl", components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(
                    "aa" + "0" * 30, go_fid="10",
                    extra={"helper": {"fileID": "210"}},
                ),
            ),
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Logic.unity",
            roots=[ctrl_go, helper_go],
            all_nodes={"10": ctrl_go, "200": helper_go},
        )
        # No stripped_components at all -> post-pass is a no-op on this scene.
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/Logic.unity"]["references"]
        helper = [r for r in refs if r["field"] == "helper"]
        assert len(helper) == 1
        assert helper[0]["target_kind"] == "component"
        assert helper[0]["target_ref"] == "Assets/Scenes/Logic.unity:210"
        assert "target_script_id" not in helper[0]

    def test_builtin_component_ref_untouched_by_postpass(self, tmp_path: Path):
        """A built-in-component ref (resolves to a gameobject via the existing
        branch) is never touched by the stripped post-pass."""
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        cs = scripts / "Mover.cs"
        cs.write_text("public class Mover : MonoBehaviour { }")
        idx = _make_guid_index(tmp_path, {"cc" + "0" * 30: (cs, "script")})
        # GameObject 300 with a Rigidbody (component fid 310) and the Mover MB
        # (fid 320) referencing that Rigidbody.
        go = _node("300", "Body", components=[
            ComponentData(
                component_type="Rigidbody", file_id="310", properties={},
            ),
            ComponentData(
                component_type="MonoBehaviour", file_id="320",
                properties=_mb_props(
                    "cc" + "0" * 30, go_fid="300",
                    extra={"body": {"fileID": "310"}},
                ),
            ),
        ])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Phys.unity",
            roots=[go], all_nodes={"300": go},
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        refs = artifact["scenes"]["Assets/Scenes/Phys.unity"]["references"]
        body = [r for r in refs if r["field"] == "body"]
        assert len(body) == 1
        # Built-in component -> gameobject kind with the owning GO id.
        assert body[0]["target_kind"] == "gameobject"
        assert body[0]["target_ref"] == "Assets/Scenes/Phys.unity:300"
        assert body[0].get("target_component_type") == "Rigidbody"
        assert "target_script_id" not in body[0]


@pytest.mark.skipif(
    not _TRASH_DASH_MAIN.exists(),
    reason="trash-dash source project not on disk",
)
class TestStrippedRefRealPlan:
    """AC2 real-plan assertion: drive ``plan_scene_runtime`` over the real
    Trash-Dash parsed inputs and assert all 3 stripped-MB refs resolve to a
    placement-scoped engine-union key whose suffix IS a prefab-local
    instance_id in the bound subplan."""

    def _real_artifact(self):
        from unity.scene_parser import parse_scene
        from unity.prefab_parser import parse_prefabs
        from unity.guid_resolver import build_guid_index
        idx = build_guid_index(_TRASH_DASH)
        lib = parse_prefabs(_TRASH_DASH)
        scene = parse_scene(_TRASH_DASH_MAIN)
        artifact = plan_scene_runtime([scene], lib, idx, _TRASH_DASH)
        return artifact

    def test_missionpopup_resolves_to_design_pinned_key(self):
        """The design's exact AC2 pin (LoadoutState ``869760749`` ->
        stripped ``137514649`` -> placement ``1822972501``)."""
        artifact = self._real_artifact()
        refs = artifact["scenes"]["Assets/Scenes/Main.unity"]["references"]
        row = next(
            r for r in refs
            if r["from"] == "Assets/Scenes/Main.unity:869760749"
            and r["field"] == "missionPopup"
        )
        assert row["target_ref"] == (
            "Assets/Scenes/Main.unity:1822972501:"
            "a53fe2875371488408daf0df7d69a981:"
            "Assets/Prefabs/UI/MissionPopup.prefab:114000011972273750"
        )
        assert row["target_script_id"] == "fff2f071f7335eb43a712a702b990041"

    def test_all_three_stripped_refs_resolve_fail_closed(self):
        """All 3 real stripped refs (137514649->MissionUI, 80306028->MissionUI,
        926798345->HighscoreUI) resolve to the EXACT byte-exact engine-union
        key ``<ns>:<pi_fid>:<prefab_id>:<src_fid>`` and target_script_id the
        planner produces over the real Trash-Dash inputs."""
        artifact = self._real_artifact()
        refs = artifact["scenes"]["Assets/Scenes/Main.unity"]["references"]
        prefabs = artifact["prefabs"]
        # The 3 (from_instance, field) rows that cite a stripped fileID, each
        # pinned to its EXACT resolved target_ref + target_script_id.
        cases = [
            (
                "Assets/Scenes/Main.unity:869760749", "missionPopup",
                "Assets/Scenes/Main.unity:1822972501:"
                "a53fe2875371488408daf0df7d69a981:"
                "Assets/Prefabs/UI/MissionPopup.prefab:114000011972273750",
                "fff2f071f7335eb43a712a702b990041",
            ),
            (
                "Assets/Scenes/Main.unity:455205752", "missionPopup",
                "Assets/Scenes/Main.unity:80306026:"
                "a53fe2875371488408daf0df7d69a981:"
                "Assets/Prefabs/UI/MissionPopup.prefab:114000011972273750",
                "fff2f071f7335eb43a712a702b990041",
            ),
            (
                "Assets/Scenes/Main.unity:1815696064", "playerEntry",
                "Assets/Scenes/Main.unity:972301424:"
                "ac361d43768a861498da8046b83b94f5:"
                "Assets/Prefabs/UI/Score.prefab:114000010752991706",
                "1a6452b9bb1a07a45b7eb7869a8a49ab",
            ),
        ]
        for src, field, expected_ref, expected_script_id in cases:
            row = next(
                r for r in refs if r["from"] == src and r["field"] == field
            )
            assert row["target_ref"] == expected_ref, (src, field)
            assert row["target_script_id"] == expected_script_id, (src, field)
            target = row["target_ref"]
            # Resolved key shape: <ns>:<pi_fid>:<prefab_id>:<src_fid>.
            # Split off the scene namespace + pi_fid prefix to recover the
            # prefab-local instance_id, and assert it lives in the subplan.
            parts = target.split(":")
            assert parts[0] == "Assets/Scenes/Main.unity"
            # prefab-local instance_id = everything after <ns>:<pi_fid>:
            local_instance_id = ":".join(parts[2:])
            # find the subplan that owns this instance_id + matching script_id
            found = False
            for subplan in prefabs.values():
                for inst in subplan["instances"]:
                    if inst["instance_id"] == local_instance_id:
                        assert inst["script_id"] == row["target_script_id"]
                        found = True
                        break
                if found:
                    break
            assert found, (src, field, target)


# ---------------------------------------------------------------------------
# Gap #3 — never-placed dormant-holder descendant suppression (slice 4.2)
# ---------------------------------------------------------------------------

class TestDormantHolderDescendantSuppression:
    """The scene-runtime planner must NOT emit a component instance row for a
    MonoBehaviour whose host GameObject has an INACTIVE ANCESTOR.

    ``scene_converter`` prunes an inactive GameObject's children in every mode
    (legacy prunes the whole subtree; generic emits the inactive node as a
    CHILDLESS dormant holder — ``_emit_dormant_holder`` never recurses), so
    such a host is never placed in the workspace. A row bound to it makes the
    host runtime ``workspaceFind`` a missing GameObject → ``self.gameObject =
    nil`` → ``connectGameObjectTriggerSignal: no touch part on nil`` at boot
    (gap #3). The host node's OWN inactive flag does NOT suppress — an
    inactive-but-referenced node still gets its dormant holder, so its own
    MonoBehaviour rows resolve to that holder.
    """

    def _idx(self, tmp_path: Path, guid: str, stem: str) -> GuidIndex:
        scripts = tmp_path / "Assets" / "Scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        cs = scripts / f"{stem}.cs"
        cs.write_text(f"public class {stem} : MonoBehaviour {{ }}")
        return _make_guid_index(tmp_path, {guid: (cs, "script")})

    def test_descendant_of_inactive_ancestor_row_suppressed(self, tmp_path: Path):
        # Mirrors the real bug: CharacterCollider (active GO "CharacterSlot")
        # under an INACTIVE parent ("PlayerPivot"). The collider row must NOT
        # be emitted because CharacterSlot is never placed.
        guid = "a" * 32
        idx = self._idx(tmp_path, guid, "CharacterCollider")

        # active child host with a MonoBehaviour, under an inactive parent.
        slot = _node("CharacterSlot", "CharacterSlot", active=True, components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="200",
                properties=_mb_props(guid, go_fid="CharacterSlot"),
            )
        ])
        pivot = _node("PlayerPivot", "PlayerPivot", active=False, children=[slot])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Main.unity", roots=[pivot],
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        block = artifact["scenes"]["Assets/Scenes/Main.unity"]
        # No dangling row bound to the never-placed CharacterSlot.
        slot_id = "Assets/Scenes/Main.unity:CharacterSlot"
        assert all(i["game_object_id"] != slot_id for i in block["instances"])
        assert block["instances"] == []
        assert block["references"] == []
        assert block["lifecycle_order"] == []
        # AC7 corollary: the module is still emitted (runtime_bearing) so the
        # class can run on a runtime-instantiated rig — only the dangling row
        # is dropped.
        assert artifact["modules"][guid]["runtime_bearing"] is True

    def test_inactive_node_itself_still_emits_row(self, tmp_path: Path):
        # An inactive node with NO inactive ancestor keeps its row: it becomes
        # a dormant holder stamped with its own _SceneRuntimeId, so its own
        # MonoBehaviour binds to that holder. (Self-inactive != ancestor-inactive.)
        guid = "b" * 32
        idx = self._idx(tmp_path, guid, "Popup")
        node = _node("10", "Popup", active=False, components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(guid, go_fid="10"),
            )
        ])
        scene = _scene(tmp_path / "Assets" / "Scenes" / "S.unity", roots=[node])
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        insts = artifact["scenes"]["Assets/Scenes/S.unity"]["instances"]
        assert len(insts) == 1
        assert insts[0]["game_object_id"] == "Assets/Scenes/S.unity:10"
        assert insts[0]["active"] is False

    def test_active_descendant_of_active_ancestor_emits_row(self, tmp_path: Path):
        # A normally-placed trigger: active host with an all-active ancestor
        # chain still emits its row (no over-suppression).
        guid = "c" * 32
        idx = self._idx(tmp_path, guid, "Trigger")
        child = _node("child", "TriggerGO", active=True, components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(guid, go_fid="child"),
            )
        ])
        parent = _node("parent", "ActiveParent", active=True, children=[child])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Main.unity", roots=[parent],
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        insts = artifact["scenes"]["Assets/Scenes/Main.unity"]["instances"]
        assert len(insts) == 1
        assert insts[0]["game_object_id"] == "Assets/Scenes/Main.unity:child"

    def test_inactive_subtree_does_not_suppress_active_sibling(self, tmp_path: Path):
        # Edge: a dormant holder with active descendants ELSEWHERE. An inactive
        # branch suppresses only ITS OWN descendants; an active sibling subtree
        # under the same active root still emits its rows.
        dead_guid = "d" * 32
        live_guid = "e" * 32
        idx = self._idx(tmp_path, dead_guid, "DeadMono")
        # add the second script to the same index
        scripts = tmp_path / "Assets" / "Scripts"
        cs2 = scripts / "LiveMono.cs"
        cs2.write_text("public class LiveMono : MonoBehaviour { }")
        idx = _make_guid_index(
            tmp_path,
            {dead_guid: (scripts / "DeadMono.cs", "script"),
             live_guid: (cs2, "script")},
        )

        dead_child = _node("deadchild", "DeadChild", active=True, components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="200",
                properties=_mb_props(dead_guid, go_fid="deadchild"),
            )
        ])
        inactive_branch = _node(
            "inactive", "InactiveBranch", active=False, children=[dead_child],
        )
        live = _node("live", "LiveGO", active=True, components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="300",
                properties=_mb_props(live_guid, go_fid="live"),
            )
        ])
        root = _node("root", "Root", active=True, children=[inactive_branch, live])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Main.unity", roots=[root],
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        insts = artifact["scenes"]["Assets/Scenes/Main.unity"]["instances"]
        gids = {i["game_object_id"] for i in insts}
        # active sibling under an active root emits; dead branch's descendant
        # is suppressed.
        assert "Assets/Scenes/Main.unity:live" in gids
        assert "Assets/Scenes/Main.unity:deadchild" not in gids

    def test_deep_descendant_of_inactive_ancestor_suppressed(self, tmp_path: Path):
        # The inactive ancestor need not be the immediate parent — any inactive
        # ancestor anywhere up the chain suppresses the row.
        guid = "f" * 32
        idx = self._idx(tmp_path, guid, "DeepMono")
        leaf = _node("leaf", "Leaf", active=True, components=[
            ComponentData(
                component_type="MonoBehaviour", file_id="20",
                properties=_mb_props(guid, go_fid="leaf"),
            )
        ])
        mid = _node("mid", "Mid", active=True, children=[leaf])
        top_inactive = _node("top", "TopInactive", active=False, children=[mid])
        scene = _scene(
            tmp_path / "Assets" / "Scenes" / "Main.unity", roots=[top_inactive],
        )
        artifact = plan_scene_runtime(
            parsed_scenes=[scene], prefab_library=None,
            guid_index=idx, unity_project_root=tmp_path,
        )
        block = artifact["scenes"]["Assets/Scenes/Main.unity"]
        assert block["instances"] == []
