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

from core.unity_types import (
    ComponentData,
    GuidEntry,
    GuidIndex,
    ParsedScene,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
    SceneNode,
)
from converter.scene_runtime_planner import (
    build_require_graph,
    plan_scene_runtime,
)


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
            "modules", "scenes", "prefabs", "domain_overrides",
        }
        assert artifact["modules"] == {}
        assert artifact["scenes"] == {}
        assert artifact["prefabs"] == {}
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
