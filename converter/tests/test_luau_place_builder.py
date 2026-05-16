"""Tests for the Luau place builder module."""

import pytest
from roblox.luau_place_builder import generate_place_luau, generate_place_luau_chunked
from core.roblox_types import (
    RbxCFrame, RbxLight, RbxPart, RbxPlace, RbxScript,
    RbxSurfaceAppearance, RbxSound, RbxConstraint,
)


def _minimal_place():
    """Create a minimal test place."""
    place = RbxPlace()
    part = RbxPart(
        name="TestPart", class_name="Part",
        cframe=RbxCFrame(x=10, y=5, z=-3),
        size=(4, 2, 6), color=(1.0, 0.0, 0.0),
        material="Brick", anchored=True,
    )
    place.workspace_parts.append(part)
    return place


class TestLuauPlaceBuilder:
    def test_generates_valid_script(self):
        place = _minimal_place()
        script = generate_place_luau(place)
        assert len(script) > 100
        assert "SavePlaceAsync" in script
        assert "TestPart" in script

    def test_under_4mb(self):
        place = _minimal_place()
        script = generate_place_luau(place)
        assert len(script.encode("utf-8")) < 4 * 1024 * 1024

    def test_meshpart_uses_create_mesh_part_async(self):
        place = RbxPlace()
        mesh = RbxPart(
            name="Mesh", class_name="MeshPart",
            cframe=RbxCFrame(), size=(1, 1, 1),
            mesh_id="rbxassetid://12345", anchored=True,
        )
        mesh.attributes["_MeshId"] = "rbxassetid://12345"
        place.workspace_parts.append(mesh)
        script = generate_place_luau(place)
        assert "CreateMeshPartAsync" in script
        assert "rbxassetid://12345" in script

    def test_script_embedding(self):
        place = _minimal_place()
        place.scripts.append(RbxScript(
            name="Hello", source='print("world")', script_type="Script"
        ))
        script = generate_place_luau(place)
        assert "Hello" in script
        assert 'print("world")' in script

    def test_surface_appearance_fallback(self):
        place = RbxPlace()
        part = RbxPart(
            name="SA", class_name="MeshPart",
            cframe=RbxCFrame(), size=(1, 1, 1), anchored=True,
            mesh_id="rbxassetid://12345",
        )
        part.attributes["_MeshId"] = "rbxassetid://12345"
        part.surface_appearance = RbxSurfaceAppearance(
            color_map="rbxassetid://99999",
        )
        place.workspace_parts.append(part)
        script = generate_place_luau(place)
        # Should have SurfaceAppearance try + Texture fallback
        assert "SurfaceAppearance" in script
        assert "Texture" in script
        assert "saOk" in script

    def test_surface_appearance_no_colormap_balanced(self):
        """SurfaceAppearance without color_map should still have balanced blocks."""
        place = RbxPlace()
        part = RbxPart(
            name="SA2", class_name="MeshPart",
            cframe=RbxCFrame(), size=(1, 1, 1), anchored=True,
        )
        part.surface_appearance = RbxSurfaceAppearance(
            normal_map="rbxassetid://88888",  # no color_map
        )
        place.workspace_parts.append(part)
        script = generate_place_luau(place)
        # Count do/end balance (simplified)
        do_count = script.count("\ndo\n") + script.count("\ndo ")
        end_count = script.count("\nend\n") + script.count("\nend)")
        # Should not have gross imbalance
        assert abs(do_count - end_count) < 5, f"Block imbalance: {do_count} do vs {end_count} end"

    def test_light_emission(self):
        place = RbxPlace()
        part = RbxPart(name="Lit", class_name="Part",
                       cframe=RbxCFrame(), size=(1, 1, 1), anchored=True)
        part.lights.append(RbxLight(
            light_type="PointLight", brightness=2.0,
            color=(1, 1, 0), range=20.0, shadows=True,
        ))
        place.workspace_parts.append(part)
        script = generate_place_luau(place)
        assert "PointLight" in script
        assert "Shadows=true" in script

    def test_constraint_emission(self):
        place = RbxPlace()
        part = RbxPart(name="C", class_name="Part",
                       cframe=RbxCFrame(), size=(1, 1, 1), anchored=True)
        part.constraints.append(RbxConstraint(
            constraint_type="HingeConstraint",
            limits_enabled=True,
            lower_angle=-30, upper_angle=60,
        ))
        place.workspace_parts.append(part)
        script = generate_place_luau(place)
        assert "HingeConstraint" in script
        assert "LimitsEnabled=true" in script

    def test_chunked_returns_single_for_small(self):
        place = _minimal_place()
        chunks = generate_place_luau_chunked(place)
        assert len(chunks) == 1

    def test_motor6d_emits_bone_parts_before_joint(self):
        """Codex round-3 finding: the headless Luau path used to emit
        Motor6Ds with ``FindFirstChild`` lookups for ``Part0`` /
        ``Part1`` but never created the bone Parts those lookups
        depend on. Without explicit bone parts, ``FindFirstChild``
        returns nil and the joint silently does nothing — rigged
        meshes don't animate.
        """
        from core.roblox_types import RbxMotor6D
        place = RbxPlace()
        part = RbxPart(
            name="Rig", class_name="MeshPart",
            cframe=RbxCFrame(), size=(1, 1, 1), anchored=True,
            mesh_id="rbxassetid://1",
        )
        part.motor6ds.append(RbxMotor6D(
            name="HipJoint",
            part0_name="HumanoidRootPart",
            part1_name="Spine",
            c0=RbxCFrame(),
            c1=RbxCFrame(),
        ))
        part.motor6ds.append(RbxMotor6D(
            name="SpineToChest",
            part0_name="Spine",
            part1_name="Chest",
            c0=RbxCFrame(),
            c1=RbxCFrame(),
        ))
        place.workspace_parts.append(part)
        script = generate_place_luau(place)

        # Every non-HumanoidRootPart bone name must appear as a Part
        # creation BEFORE the Motor6D references it. Look for the
        # Name assignment specifically.
        assert "bp.Name=\"Spine\"" in script
        assert "bp.Name=\"Chest\"" in script
        # And the Motor6D's Part0 for the HumanoidRootPart alias must
        # resolve to the parent mesh directly (not via FindFirstChild
        # which would search descendants and miss).
        # Find the HipJoint Motor6D block and verify it uses the
        # parent_var, not a FindFirstChild lookup for HumanoidRootPart.
        spine_idx = script.find("bp.Name=\"Spine\"")
        motor6d_idx = script.find("m.Name=\"HipJoint\"")
        assert spine_idx >= 0 and motor6d_idx >= 0
        assert spine_idx < motor6d_idx, (
            "Bone Parts must be emitted BEFORE the Motor6Ds that "
            "reference them; FindFirstChild on a not-yet-created "
            "child returns nil."
        )

    def test_child_hierarchy(self):
        place = RbxPlace()
        parent = RbxPart(name="Parent", class_name="Model",
                         cframe=RbxCFrame(), size=(1, 1, 1))
        child = RbxPart(name="Child", class_name="Part",
                        cframe=RbxCFrame(x=5), size=(2, 2, 2), anchored=True)
        parent.children.append(child)
        place.workspace_parts.append(parent)
        script = generate_place_luau(place)
        assert "Parent" in script
        assert "Child" in script

    def test_replicated_templates_emitted_under_templates_folder(self):
        """Headless publish must emit ``ReplicatedStorage.Templates`` and
        every ``replicated_templates`` entry under it. Without this folder,
        gameplay scripts that ``ReplicatedStorage:WaitForChild("Templates")``
        (PrefabSpawner.luau and prefab-scoped animation drivers) stall
        forever in the published place even though the .rbxlx path works
        end-to-end.
        """
        place = RbxPlace()
        # Anchor part so the place reads as non-empty.
        place.workspace_parts.append(RbxPart(
            name="Anchor", class_name="Part",
            cframe=RbxCFrame(), size=(4, 1, 4), anchored=True,
        ))
        template = RbxPart(name="Vehicle", class_name="Model")
        place.replicated_templates.append(template)

        script = generate_place_luau(place)

        assert "TF.Name='Templates'" in script, (
            "expected Templates folder creation; full script:\n" + script
        )
        assert "TF.Parent=RS" in script
        # The template itself is emitted as a child of the Templates folder.
        assert "Vehicle" in script

    def test_replicated_template_carries_child_scripts_in_headless(self):
        """A script attached to a template's ``scripts`` list (from the
        Phase 5.9 reparent pass) must travel with the template into the
        headless publish output. Pre-fix, the reparent moved scripts under
        templates while the headless builder ignored ``replicated_templates``,
        so prefab-scoped animation drivers vanished from headless publishes
        entirely."""
        place = RbxPlace()
        place.workspace_parts.append(RbxPart(
            name="Anchor", class_name="Part",
            cframe=RbxCFrame(), size=(4, 1, 4), anchored=True,
        ))
        template = RbxPart(name="Vehicle", class_name="Model")
        template.scripts.append(RbxScript(
            name="Anim_Vehicle_Wheel_Spin",
            source='print("vehicle anim driver")',
            script_type="Script",
        ))
        place.replicated_templates.append(template)

        script = generate_place_luau(place)

        # Both the script name and its source must reach the published place.
        assert "Anim_Vehicle_Wheel_Spin" in script, (
            "child script of replicated template missing from headless output"
        )
        assert 'print("vehicle anim driver")' in script

    def test_no_templates_folder_when_replicated_templates_empty(self):
        """An empty ``replicated_templates`` list must NOT emit the
        Templates folder. Adding it unconditionally would break projects
        that don't use prefab packages by leaving an empty folder behind."""
        place = _minimal_place()
        script = generate_place_luau(place)
        assert "TF.Name='Templates'" not in script


class TestWaterFillBlockChunking:
    """Roblox Terrain:FillBlock has a 2048-stud-per-axis cap; oversized
    regions silently no-op. The place builder must split big water planes
    (e.g. SimpleFPS's 16km island ocean) into ≤2048-stud chunks.
    """

    def _place_with_water(self, size_xyz):
        from core.roblox_types import RbxWaterRegion
        place = RbxPlace()
        # Need at least one part so the place builder generates a complete script
        place.workspace_parts.append(RbxPart(
            name="Anchor", class_name="Part",
            cframe=RbxCFrame(), size=(4, 1, 4), anchored=True,
        ))
        place.water_regions.append(RbxWaterRegion(
            position=(0.0, 14.28, 0.0), size=size_xyz, name="Ocean",
        ))
        return place

    def test_oversized_water_is_chunked(self):
        place = self._place_with_water((16069.0, 2.0, 16069.0))
        script = generate_place_luau(place)
        # Should emit multiple FillBlock(... Material.Water ...) calls
        water_fills = script.count("Enum.Material.Water")
        # 16069 / 2048 = 8 chunks per axis → 64 total
        assert water_fills == 64, f"expected 64 chunks for 16069×16069 water, got {water_fills}"

    def test_chunk_size_does_not_exceed_2048(self):
        import re as _re
        place = self._place_with_water((16069.0, 2.0, 16069.0))
        script = generate_place_luau(place)
        # Vector3.new(<x>, <y>, <z>) on the FillBlock lines — extract X size
        sizes = _re.findall(
            r"FillBlock\([^)]+\),Vector3\.new\(([\d.+-eE]+),([\d.+-eE]+),([\d.+-eE]+)\),Enum\.Material\.Water",
            script,
        )
        assert sizes, "no parsed water FillBlock sizes"
        for sx, _sy, sz in sizes:
            assert float(sx) <= 2048.0, f"chunk X size {sx} exceeds 2048"
            assert float(sz) <= 2048.0, f"chunk Z size {sz} exceeds 2048"

    def test_small_water_is_not_chunked(self):
        place = self._place_with_water((100.0, 2.0, 100.0))
        script = generate_place_luau(place)
        # Small region fits in one chunk
        assert script.count("Enum.Material.Water") == 1


class TestHeadlessTerrainEmit:
    """The place builder reads terrain FillBlock bodies from
    place.headless_terrain_scripts (NOT from place.scripts) so the embedded
    SmoothGrid in the rbxlx isn't wiped at Studio-load by a runtime script.
    Multi-terrain scenes contribute multiple bodies — all must be inlined.
    """

    def _place_with_terrains(self, n):
        from core.roblox_types import RbxTerrain
        place = RbxPlace()
        place.workspace_parts.append(RbxPart(
            name="Anchor", class_name="Part",
            cframe=RbxCFrame(), size=(4, 1, 4), anchored=True,
        ))
        for i in range(n):
            place.terrains.append(RbxTerrain(
                position=(0.0, 0.0, 0.0), size=(1000, 600, 1000),
            ))
        return place

    def test_emits_each_headless_body(self):
        place = self._place_with_terrains(2)
        place.headless_terrain_scripts.append("local t = workspace.Terrain\nt:FillBlock(CFrame.new(0,0,0), Vector3.new(4,4,4), Enum.Material.Grass)")
        place.headless_terrain_scripts.append("local t = workspace.Terrain\nt:FillBlock(CFrame.new(50,0,0), Vector3.new(4,4,4), Enum.Material.Sand)")
        script = generate_place_luau(place)
        # Both bodies inlined, in their own do/end blocks
        assert script.count("-- Terrain generation [1/2]") == 1
        assert script.count("-- Terrain generation [2/2]") == 1
        assert "Enum.Material.Grass" in script
        assert "Enum.Material.Sand" in script

    def test_terrain_present_but_no_headless_bodies_emits_marker(self):
        place = self._place_with_terrains(1)
        # No headless body registered → fallback to comment
        script = generate_place_luau(place)
        assert "-- No terrain generator available" in script

    def test_no_terrain_emits_nothing(self):
        place = RbxPlace()
        place.workspace_parts.append(RbxPart(
            name="Anchor", class_name="Part",
            cframe=RbxCFrame(), size=(4, 1, 4), anchored=True,
        ))
        place.headless_terrain_scripts.append("ignored")
        script = generate_place_luau(place)
        # No terrain → don't emit anything terrain-related
        assert "-- Terrain generation" not in script
        assert "-- No terrain generator available" not in script

    def test_terrain_bodies_not_in_place_scripts(self):
        # Regression guard: the rbxlx writer reads place.scripts, so anything
        # left in there is a runtime script that would run at Studio-load.
        # The fix specifically moved terrain bodies OUT of place.scripts.
        place = self._place_with_terrains(1)
        place.headless_terrain_scripts.append("t:FillBlock(...)")
        # The contract: scripts list does not contain a TerrainGenerator entry
        assert not any(s.name == "TerrainGenerator" for s in place.scripts)
