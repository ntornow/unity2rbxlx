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
