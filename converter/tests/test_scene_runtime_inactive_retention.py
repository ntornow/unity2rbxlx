"""PR3c carve-out 1 — generic-only inactive-retention in scene_converter.

Pinned by ``converter/docs/design/scene-runtime-contract.md`` Piece 3
("Inactive objects must survive conversion under generic") and the PR3b
row of the design doc's test matrix ("inactive but runtime-referenced
object emitted dormant with ``_SceneRuntimeId``; unreferenced inactive
still pruned; legacy pruning byte-unchanged").

Coverage:
- Under ``--scene-runtime=generic``, an inactive GameObject whose
  ``_SceneRuntimeId`` appears in ``scene_runtime.scenes[*].references[*]``
  or hosts an instance survives conversion as a dormant Model stamped
  with its ``_SceneRuntimeId`` and ``_Active = false``.
- Unreferenced inactive GameObjects are still pruned (same as legacy).
- Under ``--scene-runtime=legacy`` (the default), an inactive
  GameObject is pruned even if it is referenced by the (unused)
  planner artifact — i.e. the carve-out cannot leak into legacy.
- Snapshot test: converting a tiny synthetic scene under legacy mode
  produces an identical ``RbxPlace`` shape with vs. without a populated
  ``scene_runtime`` argument, proving the legacy path is byte-unchanged
  regardless of the planner artifact's contents.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.scene_converter import (
    _collect_runtime_referenced_ids,
    convert_scene,
)
from core.unity_types import ComponentData, ParsedScene, SceneNode
from roblox.rbxlx_writer import write_rbxlx


SCENE_PATH = Path("Assets/Scenes/PR3c.unity")


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------

def _make_node(
    name: str, file_id: str, *, active: bool = True,
    children: list[SceneNode] | None = None,
    components: list[ComponentData] | None = None,
    position: tuple[float, float, float] = (0.0, 0.0, 0.0),
    rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
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
        position=position,
        rotation=rotation,
        scale=(1.0, 1.0, 1.0),
    )


def _make_scene(roots: list[SceneNode], project_root: Path) -> ParsedScene:
    """Build a minimal ``ParsedScene`` with the project-relative scene
    path scaffolding ``convert_scene`` expects to compute a scene
    namespace. ``all_nodes`` is populated so ``_convert_node``'s parent-
    chain walk has something to traverse for child world positions.
    """
    scene_file = project_root / SCENE_PATH
    scene_file.parent.mkdir(parents=True, exist_ok=True)
    scene_file.touch(exist_ok=True)

    def _index(node: SceneNode, parent_fid: str | None) -> dict[str, SceneNode]:
        node.parent_file_id = parent_fid
        idx = {node.file_id: node}
        for c in node.children:
            idx.update(_index(c, node.file_id))
        return idx

    all_nodes: dict[str, SceneNode] = {}
    for r in roots:
        all_nodes.update(_index(r, None))

    return ParsedScene(
        scene_path=scene_file,
        roots=roots,
        all_nodes=all_nodes,
    )


def _scene_runtime_referencing(go_id: str) -> dict[str, object]:
    """Build a synthetic ``scene_runtime`` artifact whose only scene block
    references ``go_id`` via a single gameobject-kind reference row."""
    return {
        "scenes": {
            str(SCENE_PATH): {
                "instances": [],
                "references": [
                    {
                        "from": f"{SCENE_PATH.as_posix()}:99",
                        "field": "target",
                        "index": None,
                        "target_kind": "gameobject",
                        "target_ref": go_id,
                        "target_is_ui": False,
                    },
                ],
                "lifecycle_order": [],
            },
        },
    }


def _find_part_by_name(parts, name):
    for p in parts:
        if p.name == name:
            return p
        found = _find_part_by_name(p.children, name)
        if found is not None:
            return found
    return None


def _all_parts(parts):
    out = []
    for p in parts:
        out.append(p)
        out.extend(_all_parts(p.children))
    return out


# ---------------------------------------------------------------------------
# Helper unit tests — _collect_runtime_referenced_ids
# ---------------------------------------------------------------------------

class TestCollectRuntimeReferencedIds:
    """The helper that builds the runtime-referenced id set out of the
    planner artifact. Pinned independently of ``convert_scene`` so a
    schema drift surfaces here first."""

    def test_empty_input_returns_empty_set(self):
        assert _collect_runtime_referenced_ids(None) == set()
        assert _collect_runtime_referenced_ids({}) == set()

    def test_picks_up_instance_game_object_ids(self):
        artifact = {
            "scenes": {
                "Assets/Scenes/X.unity": {
                    "instances": [
                        {
                            "instance_id": "Assets/Scenes/X.unity:11",
                            "script_id": "MyMB",
                            "game_object_id": "Assets/Scenes/X.unity:10",
                            "active": False,
                            "enabled": True,
                            "config": {},
                        },
                    ],
                    "references": [],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_runtime_referenced_ids(artifact) == {
            "Assets/Scenes/X.unity:10",
        }

    def test_picks_up_gameobject_and_component_target_refs(self):
        artifact = {
            "scenes": {
                "Assets/Scenes/X.unity": {
                    "instances": [],
                    "references": [
                        {
                            "from": "Assets/Scenes/X.unity:11",
                            "field": "go",
                            "index": None,
                            "target_kind": "gameobject",
                            "target_ref": "Assets/Scenes/X.unity:20",
                            "target_is_ui": False,
                        },
                        {
                            "from": "Assets/Scenes/X.unity:11",
                            "field": "peer",
                            "index": None,
                            "target_kind": "component",
                            "target_ref": "Assets/Scenes/X.unity:21",
                            "target_is_ui": False,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_runtime_referenced_ids(artifact) == {
            "Assets/Scenes/X.unity:20",
            "Assets/Scenes/X.unity:21",
        }

    def test_skips_asset_and_prefab_target_refs(self):
        """asset / prefab / scriptable_object kinds are cross-asset and
        do not name a scene GameObject — they must not pollute the
        retention set (would surface as never-matched ghosts)."""
        artifact = {
            "scenes": {
                "Assets/Scenes/X.unity": {
                    "instances": [],
                    "references": [
                        {
                            "from": "Assets/Scenes/X.unity:11",
                            "field": "asset",
                            "index": None,
                            "target_kind": "asset",
                            "target_ref": "abc123guid",
                            "target_is_ui": False,
                        },
                        {
                            "from": "Assets/Scenes/X.unity:11",
                            "field": "prefab",
                            "index": None,
                            "target_kind": "prefab",
                            "target_ref": "deadbeef:Assets/Prefabs/X.prefab",
                            "target_is_ui": False,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_runtime_referenced_ids(artifact) == set()

    def test_prefab_block_ids_also_included(self):
        """Prefab-internal GameObjects with runtime-bearing instances
        must also land in the set so prefab walks (Piece 4) can reuse it."""
        artifact = {
            "prefabs": {
                "guid:Assets/Prefabs/P.prefab": {
                    "name": "P",
                    "instances": [
                        {
                            "instance_id": "guid:Assets/Prefabs/P.prefab:5",
                            "script_id": "InPrefabMB",
                            "game_object_id": "guid:Assets/Prefabs/P.prefab:4",
                            "active": True,
                            "enabled": True,
                            "config": {},
                        },
                    ],
                    "references": [],
                    "lifecycle_order": [],
                },
            },
        }
        assert _collect_runtime_referenced_ids(artifact) == {
            "guid:Assets/Prefabs/P.prefab:4",
        }


# ---------------------------------------------------------------------------
# Conversion behavior — generic vs legacy gating
# ---------------------------------------------------------------------------

class TestInactiveRetentionUnderGeneric:
    """Generic mode: inactive-but-referenced GameObjects survive
    conversion as dormant Models stamped with their ``_SceneRuntimeId``.
    """

    def test_referenced_inactive_emitted_as_dormant_holder(self, tmp_path):
        inactive_target = _make_node(
            "DormantTarget", file_id="42", active=False,
            position=(1.0, 2.0, 3.0),
            components=[
                ComponentData(component_type="MonoBehaviour", file_id="142",
                              properties={}),
            ],
        )
        live_controller = _make_node(
            "Controller", file_id="99", active=True,
            components=[
                ComponentData(component_type="MonoBehaviour", file_id="199",
                              properties={}),
            ],
        )
        scene = _make_scene([inactive_target, live_controller], tmp_path)

        artifact = _scene_runtime_referencing(f"{SCENE_PATH.as_posix()}:42")

        place = convert_scene(
            parsed_scene=scene,
            unity_project_root=tmp_path,
            scene_runtime=artifact,
            scene_runtime_mode="generic",
        )

        dormant = _find_part_by_name(place.workspace_parts, "DormantTarget")
        assert dormant is not None, (
            "inactive-but-referenced GameObject should have been emitted "
            "as a dormant Model under --scene-runtime=generic"
        )
        assert dormant.class_name == "Model"
        assert (
            dormant.attributes["_SceneRuntimeId"]
            == f"{SCENE_PATH.as_posix()}:42"
        )
        assert dormant.attributes["_Active"] is False
        # The dormant holder has no children — the host runtime is the
        # one that activates / instantiates content under it.
        assert dormant.children == []

    def test_dormant_holder_preserves_authored_rotation(self, tmp_path):
        """Codex P2: a rotated inactive GO must reactivate at the
        authored ORIENTATION, not identity. Pin this by emitting a
        rotated dormant GO and asserting the holder's CFrame rotation
        matrix matches what the active branch would produce for the
        same authored quaternion.

        Quaternion (0, sin(45°), 0, cos(45°)) ≈ (0, 0.707, 0, 0.707)
        is a 90° rotation around Y. After Unity→Roblox quat conversion
        + matrix expansion this must NOT be the 3x3 identity.
        """
        import math
        rot_y_90 = (0.0, math.sin(math.pi / 4), 0.0, math.cos(math.pi / 4))
        inactive = _make_node(
            "RotatedDormant", file_id="42", active=False,
            position=(0.0, 0.0, 0.0), rotation=rot_y_90,
        )
        scene = _make_scene([inactive], tmp_path)
        artifact = _scene_runtime_referencing(f"{SCENE_PATH.as_posix()}:42")

        place = convert_scene(
            parsed_scene=scene,
            unity_project_root=tmp_path,
            scene_runtime=artifact,
            scene_runtime_mode="generic",
        )

        dormant = _find_part_by_name(place.workspace_parts, "RotatedDormant")
        assert dormant is not None
        cf = dormant.cframe
        # Identity 3x3 would have r00=r11=r22=1 and off-diagonals=0.
        # A 90° Y rotation gives r00 ≈ 0, r02 ≈ ±1, r20 ≈ ∓1, r22 ≈ 0.
        # The point is: the matrix is NOT identity — that's the codex
        # P2 regression pin.
        is_identity = (
            abs((cf.r00 or 0.0) - 1.0) < 1e-6
            and abs((cf.r11 or 0.0) - 1.0) < 1e-6
            and abs((cf.r22 or 0.0) - 1.0) < 1e-6
            and abs(cf.r01 or 0.0) < 1e-6 and abs(cf.r02 or 0.0) < 1e-6
            and abs(cf.r10 or 0.0) < 1e-6 and abs(cf.r12 or 0.0) < 1e-6
            and abs(cf.r20 or 0.0) < 1e-6 and abs(cf.r21 or 0.0) < 1e-6
        )
        assert not is_identity, (
            "dormant holder must carry the authored rotation matrix, "
            "not the 3x3 identity — codex P2 regression"
        )

    def test_unreferenced_inactive_still_pruned_under_generic(self, tmp_path):
        """Carve-out is keyed on the planner reference set. An inactive
        GameObject the planner didn't tag remains pruned even under
        generic — otherwise generic conversion would silently retain
        every inactive editor-only object the user toggled off."""
        unreferenced_inactive = _make_node(
            "Ghost", file_id="50", active=False,
        )
        live = _make_node("Live", file_id="60", active=True)
        scene = _make_scene([unreferenced_inactive, live], tmp_path)

        # Artifact references a DIFFERENT id — Ghost must not be retained.
        artifact = _scene_runtime_referencing(f"{SCENE_PATH.as_posix()}:999")

        place = convert_scene(
            parsed_scene=scene,
            unity_project_root=tmp_path,
            scene_runtime=artifact,
            scene_runtime_mode="generic",
        )

        assert _find_part_by_name(place.workspace_parts, "Ghost") is None
        assert _find_part_by_name(place.workspace_parts, "Live") is not None


class TestLegacyEmitByteUnchanged:
    """Legacy mode (the default) must prune inactive GameObjects exactly
    as before PR3c — even when the caller threads a populated
    ``scene_runtime`` artifact that names the inactive GO. The carve-out
    cannot leak into legacy under any combination of inputs."""

    def test_legacy_prunes_referenced_inactive(self, tmp_path):
        inactive_target = _make_node(
            "DormantTarget", file_id="42", active=False,
        )
        live = _make_node("Live", file_id="99", active=True)
        scene = _make_scene([inactive_target, live], tmp_path)

        artifact = _scene_runtime_referencing(f"{SCENE_PATH.as_posix()}:42")

        # Default scene_runtime_mode="legacy" — artifact ignored.
        place = convert_scene(
            parsed_scene=scene,
            unity_project_root=tmp_path,
            scene_runtime=artifact,
        )

        assert _find_part_by_name(place.workspace_parts, "DormantTarget") is None
        assert _find_part_by_name(place.workspace_parts, "Live") is not None

    def test_legacy_rbxlx_bytes_unchanged_with_or_without_scene_runtime(self, tmp_path):
        """Strong byte-identity snapshot (codex P3): build the same
        scene twice under legacy mode — once with no ``scene_runtime``
        arg, once with a populated artifact that references every
        inactive GO — write each ``RbxPlace`` to disk via
        ``write_rbxlx`` and assert the XML payload (with random
        ``referent="..."`` attributes stripped) is identical.

        Stripping referents is necessary because ``rbxlx_writer``
        generates them via ``uuid4`` per-call; the rest of the XML
        (CFrames, sizes, colors, attributes — every per-instance
        serialized field) is fully deterministic and any carve-out
        leak would surface as a byte diff. This is a strictly
        stronger pin than the original ``(name, class_name,
        attributes)`` tuple snapshot.
        """
        import re
        # Drop random referent values (uuid4-generated, non-deterministic)
        # while keeping the attribute slot so byte indexes still line up
        # — what we care about is every OTHER serialized field staying
        # identical between the two emits.
        _referent_re = re.compile(rb' referent="RBX[0-9A-F]+"')
        _ref_re = re.compile(rb'<Ref name="[^"]+">RBX[0-9A-F]+</Ref>')

        def _normalize(b: bytes) -> bytes:
            b = _referent_re.sub(b' referent="<DROPPED>"', b)
            return _ref_re.sub(b'<Ref name="X"><DROPPED></Ref>', b)

        def _build_scene() -> ParsedScene:
            return _make_scene(
                [
                    _make_node(
                        "Live", file_id="10", active=True,
                        position=(1.0, 2.0, 3.0),
                    ),
                    _make_node("InactiveA", file_id="20", active=False),
                    _make_node("InactiveB", file_id="30", active=False),
                ],
                tmp_path,
            )

        place_no_runtime = convert_scene(
            parsed_scene=_build_scene(),
            unity_project_root=tmp_path,
        )
        artifact = {
            "scenes": {
                str(SCENE_PATH): {
                    "instances": [],
                    "references": [
                        {
                            "from": f"{SCENE_PATH.as_posix()}:99",
                            "field": "a",
                            "index": None,
                            "target_kind": "gameobject",
                            "target_ref": f"{SCENE_PATH.as_posix()}:20",
                            "target_is_ui": False,
                        },
                        {
                            "from": f"{SCENE_PATH.as_posix()}:99",
                            "field": "b",
                            "index": None,
                            "target_kind": "gameobject",
                            "target_ref": f"{SCENE_PATH.as_posix()}:30",
                            "target_is_ui": False,
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        }
        place_with_runtime_under_legacy = convert_scene(
            parsed_scene=_build_scene(),
            unity_project_root=tmp_path,
            scene_runtime=artifact,
            scene_runtime_mode="legacy",
        )

        out_baseline = tmp_path / "baseline.rbxlx"
        out_with_runtime = tmp_path / "with_runtime.rbxlx"
        write_rbxlx(place_no_runtime, out_baseline)
        write_rbxlx(place_with_runtime_under_legacy, out_with_runtime)

        baseline_bytes = _normalize(out_baseline.read_bytes())
        with_runtime_bytes = _normalize(out_with_runtime.read_bytes())
        assert baseline_bytes == with_runtime_bytes, (
            "legacy emit must be byte-identical on disk (modulo random "
            "referent UUIDs) regardless of whether a scene_runtime "
            "artifact is threaded through"
        )
