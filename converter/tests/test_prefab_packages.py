"""Phase 4.10 — generate_prefab_packages."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.prefab_packages import (
    PrefabPackagesResult,
    _collect_referenced_prefab_names,
    _SPAWNER_LUAU,
    generate_prefab_packages,
    write_packages_manifest,
)
from core.roblox_types import RbxPart


def _prefab_template(name: str, has_root: bool = True):
    """Build a minimal PrefabTemplate-like object for generator tests."""
    root = SimpleNamespace(
        name=name,
        position=(0.0, 0.0, 0.0),
        rotation=(0.0, 0.0, 0.0, 1.0),
        scale=(1.0, 1.0, 1.0),
        mesh_guid=None,
        mesh_file_id=None,
        components=[],
        children=[],
        active=True,
        file_id="1",
        from_prefab_instance=False,
        source_prefab_name=None,
    )
    return SimpleNamespace(
        name=name,
        root=(root if has_root else None),
    )


def _library(*prefabs):
    return SimpleNamespace(prefabs=list(prefabs))


class TestCollectReferencedNames:
    def test_gathers_prefab_refs_from_serialized_map(self):
        refs = {
            "Assets/Player.cs": {"riflePrefab": "Rifle", "feedbackPrefab": "Flare"},
            "Assets/Enemy.cs":  {"spawnTarget": "Enemy"},
        }
        names = _collect_referenced_prefab_names(refs)
        assert names == {"Rifle", "Flare", "Enemy"}

    def test_audio_refs_filtered_out(self):
        refs = {
            "Assets/X.cs": {
                "prefab": "Item",
                "sound": "audio:/abs/path/shot.ogg",
            },
        }
        assert _collect_referenced_prefab_names(refs) == {"Item"}

    def test_none_returns_empty(self):
        assert _collect_referenced_prefab_names(None) == set()

    def test_empty_returns_empty(self):
        assert _collect_referenced_prefab_names({}) == set()


class TestGenerateBasic:
    def test_no_library_no_output(self):
        result = generate_prefab_packages(None, None, None)
        assert result.templates == []
        assert result.spawner_script is None

    def test_empty_library_no_output(self):
        result = generate_prefab_packages(_library(), None, None)
        assert result.templates == []

    def test_unreferenced_prefab_not_emitted(self, monkeypatch):
        """Default: only prefabs named in serialized_field_refs emit."""
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        lib = _library(_prefab_template("Rifle"), _prefab_template("Unused"))
        refs = {"Assets/P.cs": {"riflePrefab": "Rifle"}}
        result = generate_prefab_packages(lib, refs, guid_index=None)
        emitted = {t.name for t in result.templates}
        assert emitted == {"Rifle"}
        assert result.manifest["emitted_names"] == ["Rifle"]

    def test_include_all_bypasses_filter(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        lib = _library(_prefab_template("A"), _prefab_template("B"))
        result = generate_prefab_packages(
            lib, None, guid_index=None, include_all=True,
        )
        assert {t.name for t in result.templates} == {"A", "B"}


class TestUnconvertedEntries:
    def test_null_root_records_entry(self):
        lib = _library(_prefab_template("Broken", has_root=False))
        result = generate_prefab_packages(
            lib, None, guid_index=None, include_all=True,
        )
        assert result.templates == []
        assert len(result.unconverted) == 1
        assert result.unconverted[0]["category"] == "prefab_package"
        assert result.unconverted[0]["item"] == "Broken"

    def test_converter_returning_none_records_entry(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: None,
        )
        lib = _library(_prefab_template("Empty"))
        result = generate_prefab_packages(
            lib, None, guid_index=None, include_all=True,
        )
        assert result.templates == []
        assert len(result.unconverted) == 1
        assert "returned None" in result.unconverted[0]["reason"]

    def test_converter_raising_recorded_not_fatal(self, monkeypatch):
        def _raise(node, **_):
            raise RuntimeError("boom")
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node", _raise,
        )
        lib = _library(_prefab_template("Exploding"), _prefab_template("Fine"))
        # Second prefab uses the raising stub too since we monkeypatched
        # the whole symbol — but the important assertion is that one
        # failing prefab doesn't stop the loop.
        result = generate_prefab_packages(
            lib, None, guid_index=None, include_all=True,
        )
        assert len(result.unconverted) == 2


class TestSpawnerScript:
    def test_emitted_when_templates_present(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"riflePrefab": "Rifle"}},
            guid_index=None,
        )
        assert result.spawner_script is not None
        assert result.spawner_script.name == "PrefabSpawner"
        assert result.spawner_script.script_type == "ModuleScript"
        assert result.spawner_script.source_path == "packages/PrefabSpawner.luau"
        # Source must contain the WaitForChild pattern scripts rely on.
        assert "Templates:WaitForChild" in _SPAWNER_LUAU or "WaitForChild" in _SPAWNER_LUAU
        assert "ReplicatedStorage" in result.spawner_script.source
        assert "inline-over-runtime-wrappers.md" in result.spawner_script.source

    def test_absent_when_no_templates(self):
        """No prefabs → no spawner."""
        lib = _library()
        result = generate_prefab_packages(lib, None, guid_index=None)
        assert result.spawner_script is None


class TestManifestPersistence:
    def test_write_creates_packages_dir(self, tmp_path):
        manifest = {"total_templates": 2, "emitted_names": ["A", "B"]}
        path = write_packages_manifest(tmp_path, manifest)
        assert path.exists()
        assert (tmp_path / "packages").is_dir()
        data = json.loads(path.read_text())
        assert data == manifest

    def test_missing_referenced_reported(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        lib = _library(_prefab_template("Rifle"))
        # Script references Rifle AND Grenade — only Rifle is in the
        # library. Manifest must surface Grenade as missing.
        refs = {"Assets/P.cs": {"a": "Rifle", "b": "Grenade"}}
        result = generate_prefab_packages(lib, refs, guid_index=None)
        assert result.manifest["emitted_names"] == ["Rifle"]
        assert result.manifest["referenced_but_missing"] == ["Grenade"]


class TestReplicatedTemplatesField:
    def test_rbxplace_has_field(self):
        from core.roblox_types import RbxPlace
        p = RbxPlace()
        assert p.replicated_templates == []
        assert isinstance(p.replicated_templates, list)


class TestWorldPivotPreservation:
    """Prefab templates must carry a meaningful ``WorldPivot`` so
    ``Model:GetPivot()`` returns a per-prefab anchor instead of Studio's
    geometric-centroid fallback. Without this, scripts that
    ``Model:PivotTo(target)`` see the template placed by centroid (not
    by the Unity prefab root), and per-prefab offset compensation has
    to be baked into every consumer script."""

    def _model_with_child_at(self, name: str, child_pos: tuple[float, float, float]) -> RbxPart:
        from core.roblox_types import RbxCFrame
        child = RbxPart(
            name=f"{name}.Child",
            class_name="MeshPart",
            cframe=RbxCFrame(x=child_pos[0], y=child_pos[1], z=child_pos[2]),
        )
        return RbxPart(
            name=name, class_name="Model",
            children=[child],
        )

    def test_wrapped_root_mesh_sets_pivot_to_anchor(self, monkeypatch):
        # SimpleFPS-style wrapped prefab: ``_wrap_geometry_with_children``
        # emits a ``<root>_Mesh`` child. The anchor lands there so
        # ``Model:PivotTo(target)`` places the rendered root at target.
        from core.roblox_types import RbxCFrame
        wrapped = RbxPart(
            name="Rifle_Mesh", class_name="MeshPart",
            cframe=RbxCFrame(x=0.0, y=4.37, z=0.0),
        )
        rifle = RbxPart(
            name="Rifle", class_name="Model", children=[wrapped],
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: rifle,
        )
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Rifle"}}, guid_index=None,
        )
        assert len(result.templates) == 1
        cf = result.templates[0].cframe
        assert cf.x == 0.0
        assert abs(cf.y - 4.37) < 1e-9
        assert cf.z == 0.0

    def test_non_wrapped_prefab_falls_back_to_identity(self, monkeypatch):
        """Anything that doesn't match the wrapped-root pattern (just
        a child mesh with the original prefab name, or primitives, or
        markers) falls back to the legacy identity wipe."""
        rifle = self._model_with_child_at("Rifle", (0.0, 4.37, 0.0))
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: rifle,
        )
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Rifle"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        assert cf.x == cf.y == cf.z == 0.0

    def test_legacy_flag_wipes_pivot_to_identity(self, monkeypatch):
        rifle = self._model_with_child_at("Rifle", (0.0, 4.37, 0.0))
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: rifle,
        )
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Rifle"}},
            guid_index=None, legacy_prefab_pivot=True,
        )
        cf = result.templates[0].cframe
        assert cf.x == cf.y == cf.z == 0.0

    def test_empty_model_wipes_to_identity(self, monkeypatch):
        """No descendants → legacy wipe. Single-part templates also
        take this branch and rely on the wipe so callers can
        ``:Clone()`` and parent at origin."""
        empty = RbxPart(name="Empty", class_name="Model", children=[])
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: empty,
        )
        lib = _library(_prefab_template("Empty"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Empty"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        assert cf.x == cf.y == cf.z == 0.0
        # Rotation also identity.
        assert cf.r00 == 1.0 and cf.r11 == 1.0 and cf.r22 == 1.0

    def test_single_part_template_wipes_to_identity(self, monkeypatch):
        """Codex round-2 [P1]: a Part/MeshPart template without
        children must wipe its CFrame so callers that just ``:Clone()``
        and parent get a clone at origin, not at the prefab source's
        baked position."""
        from core.roblox_types import RbxCFrame
        sole = RbxPart(
            name="Bullet", class_name="MeshPart", children=[],
            cframe=RbxCFrame(x=120.0, y=4.0, z=-37.0),  # authored pos
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: sole,
        )
        lib = _library(_prefab_template("Bullet"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Bullet"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        assert cf.x == cf.y == cf.z == 0.0

    def test_prefers_wrapped_root_mesh_over_marker(self, monkeypatch):
        """Codex [P1] regression: when ``_wrap_geometry_with_children``
        wraps a root mesh + markers (Origin/Muzzle) into a Model, the
        first DFS BasePart is a marker. The anchor picker must prefer
        the ``<root>_Mesh`` child."""
        from core.roblox_types import RbxCFrame
        marker = RbxPart(
            name="Origin", class_name="Part",
            cframe=RbxCFrame(x=10.0, y=0.0, z=0.0),
        )
        muzzle = RbxPart(
            name="Muzzle", class_name="Part",
            cframe=RbxCFrame(x=20.0, y=0.0, z=0.0),
        )
        root_mesh = RbxPart(
            name="Rifle_Mesh", class_name="MeshPart",
            cframe=RbxCFrame(x=0.0, y=4.37, z=0.0),
        )
        rifle = RbxPart(
            name="Rifle", class_name="Model",
            # _wrap_geometry_with_children_into_model puts geometry LAST.
            children=[marker, muzzle, root_mesh],
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: rifle,
        )
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Rifle"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        # Pivot must land on Rifle_Mesh (0, 4.37, 0), NOT on the marker.
        assert cf.x == 0.0
        assert abs(cf.y - 4.37) < 1e-9
        assert cf.z == 0.0

    def test_multi_submesh_prefab_falls_back_to_identity(self, monkeypatch):
        """Codex round-16 [P1]: a multi-submesh FBX prefab has no
        ``<root>_Mesh`` wrap — direct children are the submeshes
        themselves. The anchor heuristic must not pick the first
        submesh as the pivot; fall back to identity instead."""
        from core.roblox_types import RbxCFrame
        sub1 = RbxPart(
            name="Barrel", class_name="MeshPart",
            cframe=RbxCFrame(x=0.0, y=0.0, z=0.0),
            transparency=0.0,
        )
        sub2 = RbxPart(
            name="Stock", class_name="MeshPart",
            cframe=RbxCFrame(x=0.0, y=-1.0, z=0.0),
            transparency=0.0,
        )
        sub3 = RbxPart(
            name="Trigger", class_name="MeshPart",
            cframe=RbxCFrame(x=0.5, y=-0.3, z=0.0),
            transparency=0.0,
        )
        # No "Rifle_Mesh" child — pure multi-submesh layout.
        m = RbxPart(
            name="Rifle", class_name="Model",
            children=[sub1, sub2, sub3],
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: m,
        )
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Rifle"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        # Identity wipe — no clear anchor signal.
        assert cf.x == cf.y == cf.z == 0.0

    # ``test_prefers_meshpart_direct_child_over_marker_part`` removed
    # when the anchor heuristic narrowed to wrapped-root only
    # (round-16 [P1]) — non-wrapped layouts legacy-wipe. and cf.z == 0.0

    # ``test_default_block_part_is_valid_anchor`` removed when the
    # anchor heuristic narrowed to wrapped-root only (round-16 [P1]).
    # Non-wrapped block-part layouts now legacy-wipe.

    def test_trigger_only_prefab_falls_back_to_identity(self, monkeypatch):
        """Codex round-12 [P2] regression: a prefab whose descendants
        are ALL invisible markers/triggers must wipe to identity rather
        than anchoring on one of the markers."""
        from core.roblox_types import RbxCFrame
        t1 = RbxPart(
            name="TriggerA", class_name="Part",
            cframe=RbxCFrame(x=5.0, y=0.0, z=0.0),
            transparency=1.0,
        )
        t2 = RbxPart(
            name="TriggerB", class_name="Part",
            cframe=RbxCFrame(x=-5.0, y=0.0, z=0.0),
            transparency=1.0,
        )
        m = RbxPart(
            name="TriggerVolume", class_name="Model",
            children=[t1, t2],
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: m,
        )
        lib = _library(_prefab_template("TriggerVolume"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "TriggerVolume"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        # All children invisible → identity wipe.
        assert cf.x == cf.y == cf.z == 0.0

    def test_transparent_wrapped_root_mesh_falls_back(self, monkeypatch):
        """Codex round-9 [P2]: when ``_wrap_geometry_with_children``
        produces a TRANSPARENT ``<root>_Mesh``, the narrowed-scope
        anchor picker rejects it (marker check) and the prefab falls
        back to identity rather than anchoring on a visible sibling
        with an arbitrary offset."""
        from core.roblox_types import RbxCFrame
        invisible_wrap = RbxPart(
            name="Trigger_Mesh", class_name="MeshPart",
            cframe=RbxCFrame(x=10.0, y=0.0, z=0.0),
            transparency=1.0,
        )
        visible_sibling = RbxPart(
            name="Body", class_name="MeshPart",
            cframe=RbxCFrame(x=0.0, y=3.0, z=0.0),
            transparency=0.0,
        )
        m = RbxPart(
            name="Trigger", class_name="Model",
            children=[invisible_wrap, visible_sibling],
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: m,
        )
        lib = _library(_prefab_template("Trigger"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Trigger"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        # Wrapped-root match is invisible → no anchor → identity wipe.
        assert cf.x == cf.y == cf.z == 0.0

    def test_non_wrapped_with_spawn_location_falls_back(self, monkeypatch):
        """Non-wrapped layouts (no ``<root>_Mesh``) fall back to
        identity per the narrowed-scope contract — including spawn-rig
        templates whose only descendant is a SpawnLocation."""
        from core.roblox_types import RbxCFrame
        spawn = RbxPart(
            name="Spawn", class_name="SpawnLocation",
            cframe=RbxCFrame(x=0.0, y=1.0, z=0.0),
        )
        m = RbxPart(
            name="SpawnRig", class_name="Model",
            children=[spawn],
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: m,
        )
        lib = _library(_prefab_template("SpawnRig"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "SpawnRig"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        assert cf.x == cf.y == cf.z == 0.0

    # Note: round-5/7/8 tests for non-wrapped layouts (primitive
    # geometry, invisible MeshPart proxies, marker Parts) were removed
    # when the anchor heuristic was narrowed to wrapped-root only
    # (round-16 [P1]). Non-wrapped layouts now legacy-wipe; the
    # ``test_non_wrapped_prefab_falls_back_to_identity`` test above
    # covers the new default. and cf.z == 0.0

    def test_anchor_picker_uses_original_root_name_for_mesh_match(self, monkeypatch):
        """Codex round-6 [P2]: when the prefab asset name differs from
        the GameObject's root name, the wrapped ``<root>_Mesh`` child
        was emitted using the GameObject's name. The anchor picker
        must still find it after the prefab-name rename."""
        from core.roblox_types import RbxCFrame
        # GameObject named "RifleGO" produces a wrapped child named
        # "RifleGO_Mesh". The prefab asset is named "Rifle".
        marker = RbxPart(
            name="Origin", class_name="Part",
            cframe=RbxCFrame(x=10.0, y=0.0, z=0.0),
            transparency=1.0,
        )
        wrapped_mesh = RbxPart(
            name="RifleGO_Mesh", class_name="MeshPart",
            cframe=RbxCFrame(x=0.0, y=4.37, z=0.0),
        )
        root_go = RbxPart(
            name="RifleGO", class_name="Model",
            children=[marker, wrapped_mesh],
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: root_go,
        )
        # Prefab asset name is "Rifle" (≠ GameObject "RifleGO").
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Rifle"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        # Anchor landed on RifleGO_Mesh (the wrapped root), NOT the
        # marker, even though the prefab was renamed to "Rifle".
        assert cf.x == 0.0 and abs(cf.y - 4.37) < 1e-9

    def test_preserves_root_rotation_when_child_is_rotated(self, monkeypatch):
        """Codex [P1] regression: the chosen child's rotation must NOT
        be copied into ``WorldPivot``. The prefab root's rotation is
        the authoritative orientation."""
        from core.roblox_types import RbxCFrame
        # Child has a 90° Y rotation; if we copy its basis the whole
        # model spawns rotated.
        rotated_child = RbxPart(
            name="Rifle_Mesh", class_name="MeshPart",
            cframe=RbxCFrame(
                x=0.0, y=0.5, z=0.0,
                r00=0.0, r01=0.0, r02=1.0,
                r10=0.0, r11=1.0, r12=0.0,
                r20=-1.0, r21=0.0, r22=0.0,
            ),
        )
        root_rot = RbxCFrame(
            x=0.0, y=0.0, z=0.0,
            r00=1.0, r01=0.0, r02=0.0,
            r10=0.0, r11=1.0, r12=0.0,
            r20=0.0, r21=0.0, r22=1.0,
        )
        rifle = RbxPart(
            name="Rifle", class_name="Model",
            children=[rotated_child],
            cframe=root_rot,
        )
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: rifle,
        )
        lib = _library(_prefab_template("Rifle"))
        result = generate_prefab_packages(
            lib, {"Assets/P.cs": {"r": "Rifle"}}, guid_index=None,
        )
        cf = result.templates[0].cframe
        # Translation came from the child.
        assert abs(cf.y - 0.5) < 1e-9
        # Rotation is the prefab root's identity, NOT the child's 90° Y.
        assert cf.r00 == 1.0 and cf.r11 == 1.0 and cf.r22 == 1.0
        assert cf.r02 == 0.0 and cf.r20 == 0.0


class TestAttachPrefabScopedAnimationScripts:
    """Phase 5.9 deep-fix: prefab-scoped animation scripts must reach the
    template's ``scripts`` list (so cloning the template carries the
    driver) without losing the flat-list copy that drives scene-baked
    instances. The copy uses smart binding (script.Parent first, fall
    back to workspace search) so a single source body works in both
    contexts."""

    def _make_pipeline(self, tmp_path):
        """Build a Pipeline against a minimal Unity project layout, with
        an empty RbxPlace allocated up front (write_output normally
        creates this; we skip straight to the subphase under test)."""
        from converter.pipeline import Pipeline
        from core.roblox_types import RbxPlace
        (tmp_path / "Assets").mkdir(parents=True, exist_ok=True)
        pipeline = Pipeline(unity_project_path=tmp_path, output_dir=tmp_path / "out")
        pipeline.state.rbx_place = RbxPlace()
        return pipeline

    def _make_animation_result(self, script_scopes):
        """Animation result with the given prefab-scope mapping."""
        from converter.animation_converter import AnimationConversionResult
        return AnimationConversionResult(script_scopes=dict(script_scopes))

    def test_attaches_copy_under_template_keeps_original(self, tmp_path):
        """Script with a matching template gets a copy attached under
        ``template.scripts``. The original stays in the flat list so
        scene-baked prefab instances still get a driver."""
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.animation_result = self._make_animation_result(
            {"Anim_Vehicle_Wheel_Spin": "Vehicle"}
        )
        anim_script = RbxScript(
            name="Anim_Vehicle_Wheel_Spin",
            source="-- anim",
            script_type="Script",
            parent_path="ServerScriptService",
        )
        pipeline.state.rbx_place.scripts.append(anim_script)
        template = RbxPart(name="Vehicle", class_name="Model")
        pipeline.state.rbx_place.replicated_templates.append(template)

        pipeline._attach_prefab_scoped_animation_scripts_to_templates()

        # Original stays put — scene-baked path keeps its global driver.
        assert anim_script in pipeline.state.rbx_place.scripts
        assert anim_script.parent_path == "ServerScriptService"
        # Template carries an independent copy (clone path).
        assert len(template.scripts) == 1
        attached = template.scripts[0]
        assert attached is not anim_script, "template copy must be independent"
        assert attached.name == anim_script.name
        assert attached.source == anim_script.source
        # The template copy's parent_path is cleared so storage_classifier
        # / writer don't reroute it back to a top-level container.
        assert attached.parent_path is None

    def test_skips_when_template_missing(self, tmp_path):
        """Script whose template was filtered out (e.g. by serialized_field_refs)
        is left alone — no exception, no silent loss."""
        from core.roblox_types import RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.animation_result = self._make_animation_result(
            {"Anim_Ghost_Ctrl_Clip": "Ghost"}
        )
        anim_script = RbxScript(
            name="Anim_Ghost_Ctrl_Clip",
            source="-- anim",
            script_type="Script",
            parent_path="ServerScriptService",
        )
        pipeline.state.rbx_place.scripts.append(anim_script)
        # Note: no Ghost template registered.

        pipeline._attach_prefab_scoped_animation_scripts_to_templates()

        assert anim_script in pipeline.state.rbx_place.scripts
        assert anim_script.parent_path == "ServerScriptService"

    def test_unscoped_scripts_untouched(self, tmp_path):
        """Empty script_scopes is a no-op — scene-scoped and project-wide
        animation scripts don't leak into ``template.scripts``."""
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.animation_result = self._make_animation_result({})
        anim_script = RbxScript(
            name="Anim_Level1_Ctrl_Clip",
            source="-- anim",
            script_type="Script",
            parent_path="ServerScriptService",
        )
        pipeline.state.rbx_place.scripts.append(anim_script)
        template = RbxPart(name="Vehicle", class_name="Model")
        pipeline.state.rbx_place.replicated_templates.append(template)

        pipeline._attach_prefab_scoped_animation_scripts_to_templates()

        assert anim_script in pipeline.state.rbx_place.scripts
        assert template.scripts == []

    def test_no_animation_result_is_noop(self, tmp_path):
        """Pipelines that never ran transpile_scripts have no
        animation_result. The attach pass must tolerate that without
        crashing."""
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.animation_result = None
        pipeline.state.rbx_place.replicated_templates.append(
            RbxPart(name="Vehicle"),
        )
        pipeline.state.rbx_place.scripts.append(
            RbxScript(name="x", source="", script_type="Script"),
        )

        pipeline._attach_prefab_scoped_animation_scripts_to_templates()  # must not raise

    def test_self_guarded_script_skips_baseparts_guard(self, tmp_path):
        """A smart-binding script that already self-guards via
        ``script.Parent:IsA("Model")`` must not get the unconditional
        BasePart guard prepended — that would short-circuit the
        script's own conditional before it runs, breaking both the
        flat-list and template-attached copies."""
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        smart_source = (
            "if script.Parent and (script.Parent:IsA('Model') "
            "or script.Parent:IsA('BasePart')) then\n"
            "  local target = script.Parent:FindFirstChild('Vehicle', true)\n"
            "else\n"
            "  local target = workspace:FindFirstChild('Vehicle', true)\n"
            "end\n"
        )
        anim_script = RbxScript(
            name="Anim_Vehicle_Wheel_Spin",
            source=smart_source,
            script_type="Script",
            parent_path="ServerScriptService",
        )
        pipeline.state.rbx_place.scripts.append(anim_script)
        pipeline.state.rbx_place.workspace_parts.append(
            RbxPart(name="Anchor", class_name="Part"),
        )

        pipeline._bind_scripts_to_parts()

        assert "if not script.Parent:IsA(\"BasePart\") then return end" not in anim_script.source, (
            "self-guarded script must not receive the BasePart-only guard; "
            "full source:\n" + anim_script.source
        )


def _variant_template(name: str, source_prefab_guid: str | None = None):
    """Build a minimal variant-aware PrefabTemplate-like object for tests."""
    root = SimpleNamespace(
        name=name,
        position=(0.0, 0.0, 0.0),
        rotation=(0.0, 0.0, 0.0, 1.0),
        scale=(1.0, 1.0, 1.0),
        mesh_guid=None,
        mesh_file_id=None,
        components=[],
        children=[],
        active=True,
        file_id="1",
        from_prefab_instance=False,
        source_prefab_name=None,
    )
    return SimpleNamespace(
        name=name,
        root=root,
        is_variant=source_prefab_guid is not None,
        source_prefab_guid=source_prefab_guid,
    )


def _variant_library(*prefabs, by_guid=None):
    return SimpleNamespace(
        prefabs=list(prefabs),
        by_guid=dict(by_guid or {}),
    )


class TestPhase513VariantChainTemplates:
    """Phase 5.13: per-prefab variant-chain preservation in templates.

    Acceptance: a prefab with two variants emits three Templates that
    compose at clone time. Variant templates carry a
    ``VariantParentTemplate`` attribute pointing at their source prefab
    and the manifest reports the variant chain.
    """

    def test_two_variants_emit_three_templates(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        base = _variant_template("Hero")
        blue = _variant_template("HeroBlue", source_prefab_guid="hero" + "0" * 28)
        red = _variant_template("HeroRed", source_prefab_guid="hero" + "0" * 28)
        lib = _variant_library(
            base, blue, red,
            by_guid={
                "hero" + "0" * 28: base,
                "blue" + "0" * 28: blue,
                "red0" + "0" * 28: red,
            },
        )
        result = generate_prefab_packages(lib, None, guid_index=None, include_all=True)

        emitted = sorted(t.name for t in result.templates)
        assert emitted == ["Hero", "HeroBlue", "HeroRed"]
        assert result.manifest["total_templates"] == 3

    def test_variant_template_carries_parent_attribute(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        base = _variant_template("Hero")
        variant = _variant_template(
            "HeroBlue", source_prefab_guid="hero" + "0" * 28,
        )
        lib = _variant_library(
            base, variant,
            by_guid={
                "hero" + "0" * 28: base,
                "blue" + "0" * 28: variant,
            },
        )
        result = generate_prefab_packages(lib, None, guid_index=None, include_all=True)

        emitted_by_name = {t.name: t for t in result.templates}
        # Base has NO parent attribute.
        assert "VariantParentTemplate" not in emitted_by_name["Hero"].attributes
        # Variant carries parent name.
        assert emitted_by_name["HeroBlue"].attributes["VariantParentTemplate"] == "Hero"

    def test_manifest_reports_variant_chain(self, monkeypatch):
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        base = _variant_template("Hero")
        blue = _variant_template("HeroBlue", source_prefab_guid="hero" + "0" * 28)
        red = _variant_template("HeroRed", source_prefab_guid="hero" + "0" * 28)
        lib = _variant_library(
            base, blue, red,
            by_guid={
                "hero" + "0" * 28: base,
                "blue" + "0" * 28: blue,
                "red0" + "0" * 28: red,
            },
        )
        result = generate_prefab_packages(lib, None, guid_index=None, include_all=True)

        assert result.manifest["variant_chains"] == {
            "HeroBlue": "Hero",
            "HeroRed": "Hero",
        }

    def test_unknown_parent_guid_skips_variant_metadata(self, monkeypatch):
        """Variant pointing at a missing parent GUID just emits the variant
        without metadata — no crash, no broken chain.
        """
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        orphan = _variant_template(
            "Orphan", source_prefab_guid="missing" + "0" * 25,
        )
        lib = _variant_library(orphan, by_guid={"orph" + "0" * 28: orphan})
        result = generate_prefab_packages(lib, None, guid_index=None, include_all=True)

        emitted = result.templates[0]
        assert "VariantParentTemplate" not in emitted.attributes
        assert result.manifest["variant_chains"] == {}

    def test_spawner_script_exposes_variant_chain_helper(self, monkeypatch):
        """The auto-generated PrefabSpawner module includes a variantChain
        helper that walks VariantParentTemplate attributes.
        """
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        base = _variant_template("Hero")
        variant = _variant_template(
            "HeroBlue", source_prefab_guid="hero" + "0" * 28,
        )
        lib = _variant_library(
            base, variant,
            by_guid={
                "hero" + "0" * 28: base,
                "blue" + "0" * 28: variant,
            },
        )
        result = generate_prefab_packages(lib, None, guid_index=None, include_all=True)

        spawner = result.spawner_script
        assert spawner is not None
        assert "PrefabSpawner.variantChain" in spawner.source
        assert "VariantParentTemplate" in spawner.source

    def test_unreferenced_variant_filtered_with_target_set(self, monkeypatch):
        """When serialized_field_refs is supplied, variants not referenced
        by any script are filtered out (parent emission unaffected).
        """
        monkeypatch.setattr(
            "converter.scene_converter._convert_prefab_node",
            lambda node, **_: RbxPart(name=node.name),
        )
        base = _variant_template("Hero")
        variant = _variant_template(
            "HeroBlue", source_prefab_guid="hero" + "0" * 28,
        )
        lib = _variant_library(
            base, variant,
            by_guid={
                "hero" + "0" * 28: base,
                "blue" + "0" * 28: variant,
            },
        )
        # Script only references HeroBlue, not Hero.
        refs = {"Assets/P.cs": {"prefab": "HeroBlue"}}
        result = generate_prefab_packages(lib, refs, guid_index=None)

        emitted = sorted(t.name for t in result.templates)
        assert emitted == ["HeroBlue"]
        # Manifest still records the variant chain entry; HeroBlue is
        # emitted even though its parent Hero was filtered out.
        assert result.manifest["variant_chains"] == {"HeroBlue": "Hero"}


class TestAttachMonoBehaviourScripts:
    """``_attach_monobehaviour_scripts_to_templates`` attaches a
    Script copy under each prefab-template part that carries a
    ``_ScriptClass`` attribute, even when ``_bind_scripts_to_parts``
    has already moved the script source out of the flat list onto a
    scene part. Without this, runtime-cloned prefab templates have no
    behaviour (concrete case: SimpleFPS TurretBullet template was a
    bare red cube with no flight/damage code — turret bullets fell
    inert to the ground).
    """

    def _make_pipeline(self, tmp_path):
        from converter.pipeline import Pipeline
        from core.roblox_types import RbxPlace
        (tmp_path / "Assets").mkdir(parents=True, exist_ok=True)
        pipeline = Pipeline(unity_project_path=tmp_path, output_dir=tmp_path / "out")
        pipeline.state.rbx_place = RbxPlace()
        return pipeline

    def test_attaches_script_from_flat_list(self, tmp_path):
        """Common case: script lives in the flat ``place.scripts`` list
        (no scene instance carried it). Template attribute matches by
        ``_ScriptClass``; an independent copy lands under the template.
        """
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        source = RbxScript(
            name="TurretBullet",
            source="-- bullet flight logic\n",
            script_type="Script",
        )
        pipeline.state.rbx_place.scripts.append(source)

        template = RbxPart(name="TurretBullet", class_name="Part")
        template.attributes["_ScriptClass"] = "TurretBullet"
        pipeline.state.rbx_place.replicated_templates.append(template)

        pipeline._attach_monobehaviour_scripts_to_templates()

        assert len(template.scripts) == 1
        attached = template.scripts[0]
        assert attached is not source, "must be independent copy"
        assert attached.name == "TurretBullet"
        assert attached.source == source.source
        assert attached.parent_path is None
        # Source stays in the flat list — scene-baked instances still find it.
        assert source in pipeline.state.rbx_place.scripts

    def test_attaches_script_already_moved_to_scene_part(self, tmp_path):
        """``_bind_scripts_to_parts`` runs before
        ``_generate_prefab_packages`` and may have already moved the
        script out of the flat list onto a scene-level part. The
        attach pass must search those scene-part script lists too;
        otherwise the template ends up empty. This is the actual
        SimpleFPS TurretBullet bug path.
        """
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        # Script is NOT in the flat list — only on a scene part:
        scene_bullet = RbxPart(name="TurretBullet", class_name="Part")
        scene_bullet.scripts.append(
            RbxScript(
                name="TurretBullet",
                source="-- bullet flight logic\n",
                script_type="Script",
            )
        )
        pipeline.state.rbx_place.workspace_parts.append(scene_bullet)

        template = RbxPart(name="TurretBullet", class_name="Part")
        template.attributes["_ScriptClass"] = "TurretBullet"
        pipeline.state.rbx_place.replicated_templates.append(template)

        pipeline._attach_monobehaviour_scripts_to_templates()

        assert len(template.scripts) == 1, (
            "Template must get a copy even when the script was already "
            "moved to a scene part by _bind_scripts_to_parts."
        )
        assert template.scripts[0].source == "-- bullet flight logic\n"

    def test_walks_nested_template_descendants(self, tmp_path):
        """A prefab template can have nested children (e.g. Turret model
        with a child weapon Part carrying ``_ScriptClass``). The walk
        must recurse so every level gets its script attached.
        """
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="WeaponLogic",
                source="-- weapon\n",
                script_type="Script",
            )
        )
        weapon = RbxPart(name="Weapon", class_name="MeshPart")
        weapon.attributes["_ScriptClass"] = "WeaponLogic"
        template = RbxPart(name="Turret", class_name="Model")
        template.children.append(weapon)
        pipeline.state.rbx_place.replicated_templates.append(template)

        pipeline._attach_monobehaviour_scripts_to_templates()

        # Template root has no _ScriptClass → no script attached at root.
        assert template.scripts == []
        # Nested Weapon child got the script.
        assert len(weapon.scripts) == 1
        assert weapon.scripts[0].name == "WeaponLogic"

    def test_idempotent_under_re_run(self, tmp_path):
        """Re-running the pass must not duplicate scripts already
        attached. Detects the existing script by name and skips."""
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="TurretBullet",
                source="-- bullet\n",
                script_type="Script",
            )
        )
        template = RbxPart(name="TurretBullet", class_name="Part")
        template.attributes["_ScriptClass"] = "TurretBullet"
        pipeline.state.rbx_place.replicated_templates.append(template)

        pipeline._attach_monobehaviour_scripts_to_templates()
        pipeline._attach_monobehaviour_scripts_to_templates()

        assert len(template.scripts) == 1, "second run must not duplicate"

    def test_skips_localscripts_and_modulescripts(self, tmp_path):
        """Only ``Script`` (server) types belong under a workspace part.
        LocalScripts live under StarterPlayerScripts, ModuleScripts in
        ReplicatedStorage. Attaching them as part children would either
        not execute (LocalScript) or pollute the part with require()
        modules.
        """
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.rbx_place.scripts.extend([
            RbxScript(name="HUD", source="-- hud\n", script_type="LocalScript"),
            RbxScript(name="Util", source="return {}\n", script_type="ModuleScript"),
        ])
        template = RbxPart(name="X", class_name="Part")
        template.attributes["_ScriptClass"] = "HUD"
        template.attributes["_ScriptClass_2"] = "Util"
        pipeline.state.rbx_place.replicated_templates.append(template)

        pipeline._attach_monobehaviour_scripts_to_templates()

        assert template.scripts == [], (
            "Non-Script types must stay in their canonical containers."
        )

    def test_skips_ai_stub_scripts(self, tmp_path):
        """Scripts whose body is an AI-transpilation-recommended stub
        (no API key, no Claude CLI) must not be attached to the
        template — the stub would shadow any later real implementation
        and ship a placeholder to runtime.
        """
        from core.roblox_types import RbxPart, RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.rbx_place.scripts.append(
            RbxScript(
                name="Stub",
                source="-- AI transpilation recommended\nreturn nil\n",
                script_type="Script",
            )
        )
        template = RbxPart(name="X", class_name="Part")
        template.attributes["_ScriptClass"] = "Stub"
        pipeline.state.rbx_place.replicated_templates.append(template)

        pipeline._attach_monobehaviour_scripts_to_templates()

        assert template.scripts == []

    def test_no_templates_is_noop(self, tmp_path):
        """Pipelines that didn't emit any templates leave the call as a
        no-op rather than crashing on missing state.
        """
        from core.roblox_types import RbxScript

        pipeline = self._make_pipeline(tmp_path)
        pipeline.state.rbx_place.scripts.append(
            RbxScript(name="x", source="", script_type="Script"),
        )
        # No replicated_templates added.
        pipeline._attach_monobehaviour_scripts_to_templates()  # must not raise
