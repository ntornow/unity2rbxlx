"""Tests for component_converter.py -- collider, light, audio, rigidbody, joint, trail, video conversion."""

import pytest
from converter.component_converter import (
    convert_collider,
    convert_light,
    convert_audio,
    convert_rigidbody,
    convert_joint,
    convert_trail_renderer,
    convert_line_renderer,
    convert_video_player,
    convert_reverb_zone,
    convert_reverb_filter,
    convert_particle_system,
)


class TestBoxCollider:
    """Tests for BoxCollider conversion."""

    def test_default_box_collider(self):
        """Default BoxCollider m_Size=(1,1,1) should not shrink the part."""
        props = {"m_Size": {"x": 1, "y": 1, "z": 1}, "m_IsTrigger": 0}
        size, can_collide, _offset = convert_collider("BoxCollider", props, (3.571, 3.571, 3.571))
        assert can_collide is True
        assert all(s >= 3.5 for s in size)

    def test_large_box_collider(self):
        """Large BoxCollider should expand part size."""
        props = {"m_Size": {"x": 10, "y": 5, "z": 10}, "m_IsTrigger": 0}
        size, can_collide, _offset = convert_collider("BoxCollider", props, (2.0, 2.0, 2.0))
        # 10 * 3.571 = 35.71
        assert size[0] > 30
        assert size[1] > 15

    def test_trigger_collider(self):
        """Trigger colliders should set can_collide=False."""
        props = {"m_Size": {"x": 1, "y": 1, "z": 1}, "m_IsTrigger": 1}
        size, can_collide, _offset = convert_collider("BoxCollider", props, (5.0, 5.0, 5.0))
        assert can_collide is False

    def test_collider_uses_studs(self):
        """Collider m_Size should be converted from meters to studs."""
        import config
        props = {"m_Size": {"x": 2, "y": 3, "z": 2}, "m_IsTrigger": 0}
        size, _, _offset = convert_collider("BoxCollider", props, (1.0, 1.0, 1.0))
        assert abs(size[0] - 2 * config.STUDS_PER_METER) < 0.1 or size[0] >= 2 * config.STUDS_PER_METER


    def test_collider_center_offset(self):
        """BoxCollider with non-zero m_Center should return center offset in studs."""
        import config
        props = {
            "m_Size": {"x": 1, "y": 1, "z": 1},
            "m_Center": {"x": 1.0, "y": 2.0, "z": 3.0},
            "m_IsTrigger": 0,
        }
        _size, _can_collide, offset = convert_collider("BoxCollider", props, (3.571, 3.571, 3.571))
        assert abs(offset[0] - 1.0 * config.STUDS_PER_METER) < 0.01
        assert abs(offset[1] - 2.0 * config.STUDS_PER_METER) < 0.01
        # Z is negated for Roblox coordinate system
        assert abs(offset[2] - (-3.0 * config.STUDS_PER_METER)) < 0.01

    def test_collider_zero_center(self):
        """BoxCollider with zero m_Center should return zero offset."""
        props = {
            "m_Size": {"x": 1, "y": 1, "z": 1},
            "m_Center": {"x": 0, "y": 0, "z": 0},
            "m_IsTrigger": 0,
        }
        _size, _can_collide, offset = convert_collider("BoxCollider", props, (3.571, 3.571, 3.571))
        assert offset == (0.0, 0.0, 0.0)


class TestSphereCollider:
    def test_sphere_collider(self):
        """SphereCollider should create uniform size from radius."""
        props = {"m_Radius": 2.0, "m_IsTrigger": 0}
        size, can_collide, _offset = convert_collider("SphereCollider", props, (1.0, 1.0, 1.0))
        # 2.0 * 2 * 3.571 = 14.28
        assert all(s > 14 for s in size)
        assert can_collide is True


class TestCapsuleCollider:
    def test_capsule_y_axis(self):
        """CapsuleCollider direction=1 should be tall on Y."""
        props = {"m_Radius": 0.5, "m_Height": 3.0, "m_Direction": 1, "m_IsTrigger": 0}
        size, _, _offset = convert_collider("CapsuleCollider", props, (1.0, 1.0, 1.0))
        assert size[1] > size[0]  # Y should be tallest


class TestMeshCollider:
    def test_mesh_collider_preserves_size(self):
        """MeshCollider should not change size."""
        props = {"m_IsTrigger": 0}
        size, can_collide, _offset = convert_collider("MeshCollider", props, (5.0, 10.0, 5.0))
        assert size == (5.0, 10.0, 5.0)
        assert can_collide is True


class TestLightConversion:
    def test_point_light(self):
        props = {"m_Type": 2, "m_Color": {"r": 1, "g": 0.5, "b": 0}, "m_Intensity": 2.0, "m_Range": 10}
        light = convert_light(props)
        assert light is not None
        assert light.light_type == "PointLight"
        assert light.brightness == 2.0
        assert light.color[0] == 1.0

    def test_spot_light(self):
        props = {"m_Type": 0, "m_SpotAngle": 45}
        light = convert_light(props)
        assert light is not None
        assert light.light_type == "SpotLight"
        assert light.angle == 45.0

    def test_directional_light_returns_none(self):
        props = {"m_Type": 1}
        light = convert_light(props)
        assert light is None


class TestMonoBehaviourAttributes:
    def test_extract_numeric_fields(self):
        from converter.scene_converter import _extract_monobehaviour_attributes
        from core.roblox_types import RbxPart
        part = RbxPart(name="test")
        props = {
            "m_ObjectHideFlags": 0,
            "m_Script": {"guid": "abc"},
            "damage": 10,
            "speed": 5.5,
            "label": "turret",
            "enabled": 1,
        }
        _extract_monobehaviour_attributes(props, part, None)
        assert part.attributes["damage"] == 10
        assert part.attributes["speed"] == 5.5
        assert part.attributes["label"] == "turret"

    def test_skip_object_references(self):
        from converter.scene_converter import _extract_monobehaviour_attributes
        from core.roblox_types import RbxPart
        part = RbxPart(name="test")
        props = {
            "damage": 10,
            "targetRef": {"fileID": 12345, "guid": "abc"},
        }
        _extract_monobehaviour_attributes(props, part, None)
        assert "damage" in part.attributes
        assert "targetRef" not in part.attributes


class TestSubMeshResolution:
    def test_resolve_sub_mesh_by_file_id(self):
        """fileID 4300000 → index 0, 4300002 → index 1, etc."""
        from converter.scene_converter import _resolve_sub_mesh, _mesh_hierarchies
        # Temporarily set mesh hierarchies
        import converter.scene_converter as sc
        old = sc._mesh_hierarchies
        sc._mesh_hierarchies = {
            "Assets/Models/turret.fbx": [
                {"name": "base", "meshId": "rbxassetid://100", "size": [1, 2, 1]},
                {"name": "weapon", "meshId": "rbxassetid://200", "size": [0.5, 0.5, 1]},
                {"name": "barrel", "meshId": "rbxassetid://300", "size": [0.2, 0.2, 0.8]},
            ]
        }
        try:
            from unittest.mock import MagicMock
            mock_idx = MagicMock()
            mock_idx.resolve.return_value = None
            mock_idx.resolve_relative.return_value = "Assets/Models/turret.fbx"

            # Override resolve to return a path
            from pathlib import Path
            mock_idx.resolve.return_value = Path("Assets/Models/turret.fbx")
            mock_idx.resolve_relative.return_value = Path("Assets/Models/turret.fbx")

            result0 = _resolve_sub_mesh("guid1", "4300000", mock_idx)
            assert result0 is not None
            assert result0["name"] == "base"

            result1 = _resolve_sub_mesh("guid1", "4300002", mock_idx)
            assert result1 is not None
            assert result1["name"] == "weapon"

            result2 = _resolve_sub_mesh("guid1", "4300004", mock_idx)
            assert result2 is not None
            assert result2["name"] == "barrel"
        finally:
            sc._mesh_hierarchies = old


class TestMaterialInference:
    def test_concrete_material(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("wallconcreterough") == "Concrete"

    def test_wood_material(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("wood01") == "Wood"
        assert _infer_roblox_material("beam") == "Wood"
        assert _infer_roblox_material("crate01") == "Wood"

    def test_metal_material(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("ship_metal_mat") == "Metal"
        assert _infer_roblox_material("chainlink") == "Metal"

    def test_default_plastic(self):
        from converter.material_mapper import _infer_roblox_material
        assert _infer_roblox_material("SomeMaterial") == "Plastic"
        assert _infer_roblox_material("01 - Default") == "Plastic"


class TestRigidbody:
    def test_kinematic_rigidbody(self):
        props = {"m_IsKinematic": 1}
        anchored, can_collide, custom_phys = convert_rigidbody(props)
        assert anchored is True

    def test_dynamic_rigidbody(self):
        props = {"m_IsKinematic": 0}
        anchored, can_collide, custom_phys = convert_rigidbody(props)
        assert anchored is False

    def test_heavy_rigidbody_custom_physics(self):
        """Heavy objects should get higher density via CustomPhysicalProperties."""
        props = {"m_IsKinematic": 0, "m_Mass": 10.0, "m_Drag": 0.0}
        anchored, can_collide, custom_phys = convert_rigidbody(props)
        assert custom_phys is not None
        density, friction, elasticity, fw, ew = custom_phys
        assert density > 0.7  # Heavier than default

    def test_light_rigidbody_custom_physics(self):
        """Light objects should get lower density."""
        props = {"m_IsKinematic": 0, "m_Mass": 0.1, "m_Drag": 0.0}
        anchored, can_collide, custom_phys = convert_rigidbody(props)
        assert custom_phys is not None
        density = custom_phys[0]
        assert density < 0.7  # Lighter than default

    def test_default_mass_no_custom_physics(self):
        """Default mass=1 should not create CustomPhysicalProperties."""
        props = {"m_IsKinematic": 0, "m_Mass": 1.0, "m_Drag": 0.0, "m_AngularDrag": 0.05}
        _, _, custom_phys = convert_rigidbody(props)
        assert custom_phys is None

    def test_drag_increases_friction(self):
        """High drag should map to higher friction."""
        props = {"m_IsKinematic": 0, "m_Mass": 1.0, "m_Drag": 5.0}
        _, _, custom_phys = convert_rigidbody(props)
        assert custom_phys is not None
        friction = custom_phys[1]
        assert friction > 0.3  # Higher than default

    def test_frozen_constraints_anchored(self):
        """All position axes frozen should anchor the part."""
        props = {"m_IsKinematic": 0, "m_Constraints": 0b0000_0111}
        anchored, _, _ = convert_rigidbody(props)
        assert anchored is True


class TestAudioConversion:
    """Tests for AudioSource → RbxSound conversion."""

    def test_default_audio(self):
        """Default properties should produce a valid RbxSound."""
        props = {}
        sound = convert_audio(props)
        assert sound is not None
        assert sound.volume == 1.0
        assert sound.playback_speed == 1.0
        assert sound.looped is False
        assert sound.playing is True  # PlayOnAwake defaults to 1

    def test_volume_clamp(self):
        """Volume should clamp to [0, 10]."""
        props = {"m_Volume": 15.0}
        sound = convert_audio(props)
        assert sound.volume == 10.0

    def test_pitch_to_playback_speed(self):
        props = {"m_Pitch": 2.5}
        sound = convert_audio(props)
        assert sound.playback_speed == 2.5

    def test_loop_and_play_on_awake(self):
        props = {"m_Loop": 1, "m_PlayOnAwake": 0}
        sound = convert_audio(props)
        assert sound.looped is True
        assert sound.playing is False

    def test_rolloff_distances(self):
        props = {"m_MinDistance": 5.0, "m_MaxDistance": 100.0}
        sound = convert_audio(props)
        assert sound.roll_off_min_distance == 5.0
        assert sound.roll_off_max_distance == 100.0


class TestJointConversion:
    """Tests for Unity joint → RbxConstraint conversion."""

    def test_fixed_joint(self):
        props = {}
        constraint = convert_joint("FixedJoint", props)
        assert constraint is not None
        assert constraint.constraint_type == "WeldConstraint"

    def test_hinge_joint_with_limits(self):
        props = {
            "m_UseLimits": 1,
            "m_Limits": {"min": -30.0, "max": 60.0},
        }
        constraint = convert_joint("HingeJoint", props)
        assert constraint is not None
        assert constraint.constraint_type == "HingeConstraint"
        assert constraint.limits_enabled is True
        assert constraint.lower_angle == -30.0
        assert constraint.upper_angle == 60.0

    def test_spring_joint(self):
        props = {"m_Spring": 100.0, "m_Damper": 10.0, "m_MaxDistance": 5.0}
        constraint = convert_joint("SpringJoint", props)
        assert constraint is not None
        assert constraint.constraint_type == "SpringConstraint"
        assert constraint.stiffness == 100.0
        assert constraint.damping == 10.0

    def test_character_joint(self):
        props = {"m_TwistLimitSpring": {"limit": 90.0}}
        constraint = convert_joint("CharacterJoint", props)
        assert constraint is not None
        assert constraint.constraint_type == "BallSocketConstraint"
        assert constraint.twist_limits_enabled is True

    def test_connected_body_reference(self):
        props = {"m_ConnectedBody": {"fileID": 12345}}
        constraint = convert_joint("FixedJoint", props)
        assert constraint.connected_body_file_id == "12345"

    def test_unknown_joint_returns_none(self):
        constraint = convert_joint("UnknownJoint", {})
        assert constraint is None


class TestTrailRendererConversion:
    """Tests for TrailRenderer → RbxTrail conversion."""

    def test_default_trail(self):
        props = {}
        trail = convert_trail_renderer(props)
        assert trail is not None
        assert trail.lifetime == 2.0

    def test_trail_with_properties(self):
        props = {"m_Time": 5.0, "m_MinVertexDistance": 0.5}
        trail = convert_trail_renderer(props)
        assert trail.lifetime == 5.0
        assert trail.min_length == 0.5

    def test_trail_color_extraction(self):
        props = {
            "m_Colors": {
                "color0": {"r": 1.0, "g": 0.0, "b": 0.0, "a": 0.8}
            }
        }
        trail = convert_trail_renderer(props)
        assert trail.color == (1.0, 0.0, 0.0)
        assert abs(trail.transparency - 0.2) < 0.01


class TestLineRendererConversion:
    """Tests for LineRenderer → RbxBeam conversion."""

    def test_default_beam(self):
        props = {}
        beam = convert_line_renderer(props)
        assert beam is not None
        assert beam.segments >= 1

    def test_beam_widths(self):
        props = {"m_Parameters": {"m_StartWidth": 2.0, "m_EndWidth": 0.5}}
        beam = convert_line_renderer(props)
        assert beam.width0 == 2.0
        assert beam.width1 == 0.5

    def test_beam_color(self):
        props = {
            "m_Parameters": {
                "m_StartColor": {"r": 0.0, "g": 1.0, "b": 0.0, "a": 1.0}
            }
        }
        beam = convert_line_renderer(props)
        assert beam.color == (0.0, 1.0, 0.0)


class TestVideoPlayerConversion:
    """Tests for VideoPlayer → RbxVideoFrame conversion."""

    def test_default_video(self):
        props = {}
        video = convert_video_player(props)
        assert video is not None
        assert video.playing is True  # PlayOnAwake=1 default

    def test_video_with_url(self):
        props = {"m_Url": "http://example.com/video.mp4", "m_Looping": 1}
        video = convert_video_player(props)
        assert video.video == "http://example.com/video.mp4"
        assert video.looped is True

    def test_video_volume_list(self):
        props = {"m_DirectAudioVolume": [0.8]}
        video = convert_video_player(props)
        assert video.volume == 0.8

    def test_video_volume_clamp(self):
        props = {"m_DirectAudioVolume": 15.0}
        video = convert_video_player(props)
        assert video.volume == 10.0


class TestReverbConversion:
    """Tests for AudioReverbZone/Filter → RbxReverbSoundEffect conversion."""

    def test_reverb_zone_basic(self):
        props = {"m_ReverbPreset": 1, "m_MinDistance": 5.0, "m_MaxDistance": 10.0}
        reverb = convert_reverb_zone(props)
        assert reverb is not None
        assert reverb.min_distance > 0

    def test_reverb_zone_off_preset(self):
        props = {"m_ReverbPreset": 27}
        reverb = convert_reverb_zone(props)
        assert reverb is None

    def test_reverb_filter_custom_preset(self):
        props = {
            "m_ReverbPreset": 26,
            "m_DecayTime": 3.0,
            "m_Density": 0.8,
            "m_Diffusion": 0.5,
        }
        reverb = convert_reverb_filter(props)
        assert reverb is not None
        assert reverb.decay_time == 3.0
        assert reverb.density == 0.8
        assert reverb.diffusion == 0.5

    def test_reverb_filter_off_preset(self):
        props = {"m_ReverbPreset": 27}
        reverb = convert_reverb_filter(props)
        assert reverb is None


class TestParticleSystemConversion:
    """Tests for ParticleSystem → RbxParticleEmitter conversion."""

    def test_default_particle(self):
        props = {}
        particle = convert_particle_system(props)
        assert particle is not None

    def test_particle_lifetime(self):
        props = {
            "startLifetime": {"minMaxState": 0, "scalar": 3.0},
        }
        particle = convert_particle_system(props)
        assert particle is not None
        assert particle.lifetime_max == 3.0

    def test_particle_emission_rate(self):
        props = {
            "EmissionModule": {
                "enabled": 1,
                "rateOverTime": {"scalar": 50.0},
            }
        }
        particle = convert_particle_system(props)
        assert particle is not None
        assert particle.rate == 50.0


class TestTilemapConversion:
    """Tests for Tilemap → grid of RbxParts conversion."""

    def test_empty_tilemap(self):
        """Tilemap with no tiles should return empty list."""
        from converter.component_converter import convert_tilemap
        props = {}
        parts = convert_tilemap(props)
        assert parts == []

    def test_tilemap_with_tiles(self):
        """Tilemap with tile entries should produce one Part per tile."""
        from converter.component_converter import convert_tilemap
        import config
        props = {
            "m_Tiles": [
                {
                    "first": {"x": 0, "y": 0, "z": 0},
                    "second": {"m_Tile": {"fileID": 11400000, "guid": "abc123"}},
                },
                {
                    "first": {"x": 1, "y": 0, "z": 0},
                    "second": {"m_Tile": {"fileID": 11400000, "guid": "abc123"}},
                },
                {
                    "first": {"x": 0, "y": 1, "z": 0},
                    "second": {"m_Tile": {"fileID": 11400000, "guid": "def456"}},
                },
            ],
        }
        parts = convert_tilemap(props)
        assert len(parts) == 3
        # Check positions are at grid coords * cell_size * STUDS_PER_METER
        assert parts[0].name == "Tile_0_0"
        assert parts[1].name == "Tile_1_0"
        assert parts[2].name == "Tile_0_1"
        # First tile at origin
        assert abs(parts[0].cframe.x) < 0.01
        assert abs(parts[0].cframe.y) < 0.01
        # Second tile offset by 1 cell in X
        assert abs(parts[1].cframe.x - config.STUDS_PER_METER) < 0.01

    def test_tilemap_custom_cell_size(self):
        """Tilemap with custom cell size should scale positions accordingly."""
        from converter.component_converter import convert_tilemap
        import config
        props = {
            "m_Tiles": [
                {
                    "first": {"x": 2, "y": 3, "z": 0},
                    "second": {"m_Tile": {"fileID": 100, "guid": "aaa"}},
                },
            ],
        }
        cell = (0.5, 0.5, 1.0)
        parts = convert_tilemap(cell_size=cell, properties=props)
        assert len(parts) == 1
        expected_x = 2 * 0.5 * config.STUDS_PER_METER
        expected_y = 3 * 0.5 * config.STUDS_PER_METER
        assert abs(parts[0].cframe.x - expected_x) < 0.01
        assert abs(parts[0].cframe.y - expected_y) < 0.01

    def test_tilemap_cell_size_from_properties(self):
        """Cell size should be read from m_CellSize in properties."""
        from converter.component_converter import convert_tilemap
        import config
        props = {
            "m_CellSize": {"x": 2.0, "y": 2.0, "z": 1.0},
            "m_Tiles": [
                {
                    "first": {"x": 1, "y": 0, "z": 0},
                    "second": {"m_Tile": {"fileID": 100, "guid": "bbb"}},
                },
            ],
        }
        parts = convert_tilemap(props)
        assert len(parts) == 1
        expected_x = 1 * 2.0 * config.STUDS_PER_METER
        assert abs(parts[0].cframe.x - expected_x) < 0.01

    def test_tilemap_skips_empty_tiles(self):
        """Tiles with fileID=0 and no guid should be skipped."""
        from converter.component_converter import convert_tilemap
        props = {
            "m_Tiles": [
                {
                    "first": {"x": 0, "y": 0, "z": 0},
                    "second": {"m_Tile": {"fileID": 0}},
                },
                {
                    "first": {"x": 1, "y": 0, "z": 0},
                    "second": {"m_Tile": {"fileID": 11400000, "guid": "abc"}},
                },
            ],
        }
        parts = convert_tilemap(props)
        assert len(parts) == 1
        assert parts[0].name == "Tile_1_0"

    def test_tilemap_tile_attributes(self):
        """Each tile should store grid coordinates as attributes."""
        from converter.component_converter import convert_tilemap
        props = {
            "m_Tiles": [
                {
                    "first": {"x": 5, "y": -3, "z": 0},
                    "second": {"m_Sprite": {"guid": "sprite123"}, "m_Tile": {"fileID": 100, "guid": "t1"}},
                },
            ],
        }
        parts = convert_tilemap(props)
        assert len(parts) == 1
        assert parts[0].attributes["_TileGridX"] == 5
        assert parts[0].attributes["_TileGridY"] == -3

    def test_tilemap_tile_colors(self):
        """Tile colors from m_TileColorArray should be applied."""
        from converter.component_converter import convert_tilemap
        props = {
            "m_Tiles": [
                {
                    "first": {"x": 0, "y": 0, "z": 0},
                    "second": {"m_Tile": {"fileID": 100, "guid": "t1"}},
                },
            ],
            "m_TileColorArray": [
                {"r": 1.0, "g": 0.0, "b": 0.0, "a": 0.5},
            ],
        }
        parts = convert_tilemap(props)
        assert len(parts) == 1
        assert parts[0].color == (1.0, 0.0, 0.0)
        assert abs(parts[0].transparency - 0.5) < 0.01

    def test_tilemap_parts_are_thin(self):
        """Tile parts should be thin (like sprites)."""
        from converter.component_converter import convert_tilemap
        props = {
            "m_Tiles": [
                {
                    "first": {"x": 0, "y": 0, "z": 0},
                    "second": {"m_Tile": {"fileID": 100, "guid": "t1"}},
                },
            ],
        }
        parts = convert_tilemap(props)
        assert len(parts) == 1
        # Y dimension (thickness) should be small
        assert parts[0].size[1] <= 0.5

    def test_tilemap_parts_anchored_no_shadow(self):
        """Tile parts should be anchored and not cast shadows."""
        from converter.component_converter import convert_tilemap
        props = {
            "m_Tiles": [
                {
                    "first": {"x": 0, "y": 0, "z": 0},
                    "second": {"m_Tile": {"fileID": 100, "guid": "t1"}},
                },
            ],
        }
        parts = convert_tilemap(props)
        assert len(parts) == 1
        assert parts[0].anchored is True
        assert parts[0].cast_shadow is False


class TestTilemapRendererConversion:
    """Tests for TilemapRenderer attribute extraction."""

    def test_default_renderer(self):
        """Default TilemapRenderer should have sort order 0 and Chunk mode."""
        from converter.component_converter import convert_tilemap_renderer
        props = {}
        attrs = convert_tilemap_renderer(props)
        assert attrs["_TilemapSortOrder"] == 0
        assert attrs["_TilemapRenderMode"] == "Chunk"

    def test_sort_order(self):
        """Sort order should be extracted from m_SortingOrder."""
        from converter.component_converter import convert_tilemap_renderer
        props = {"m_SortingOrder": 5}
        attrs = convert_tilemap_renderer(props)
        assert attrs["_TilemapSortOrder"] == 5

    def test_individual_mode(self):
        """Mode=1 should produce Individual render mode."""
        from converter.component_converter import convert_tilemap_renderer
        props = {"m_Mode": 1}
        attrs = convert_tilemap_renderer(props)
        assert attrs["_TilemapRenderMode"] == "Individual"

    def test_tile_anchor(self):
        """Non-default tile anchor should be stored."""
        from converter.component_converter import convert_tilemap_renderer
        props = {"m_TileAnchor": {"x": 0.0, "y": 0.0}}
        attrs = convert_tilemap_renderer(props)
        assert attrs["_TilemapAnchorX"] == 0.0
        assert attrs["_TilemapAnchorY"] == 0.0

    def test_default_anchor_not_stored(self):
        """Default anchor (0.5, 0.5) should not produce extra attributes."""
        from converter.component_converter import convert_tilemap_renderer
        props = {"m_TileAnchor": {"x": 0.5, "y": 0.5}}
        attrs = convert_tilemap_renderer(props)
        assert "_TilemapAnchorX" not in attrs


class TestSkinnedMeshRendererBoneResolution:
    """Regression tests for convert_skinned_mesh_renderer.

    Bug history: an earlier version read `node.parent` (and expected position
    as a dict), but `SceneNode` exposes `parent_file_id` and tuple position/
    rotation. The result was zero Motor6Ds and an empty bone hierarchy on
    every real scene.
    """

    def _make_node(self, fid: str, name: str, parent_fid: str | None = None):
        from core.unity_types import SceneNode
        return SceneNode(
            name=name,
            file_id=fid,
            active=True,
            layer=0,
            tag="",
            parent_file_id=parent_fid,
            position=(0.0, 1.0, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
        )

    def test_bone_chain_produces_motor6ds(self):
        """A two-bone chain (root → child, both in m_Bones) must yield a Motor6D
        whose part0 is the parent bone name."""
        from converter.component_converter import convert_skinned_mesh_renderer

        scene_nodes = {
            "10": self._make_node("10", "Hips"),
            "11": self._make_node("11", "Spine", parent_fid="10"),
        }
        props = {
            "m_Bones": [{"fileID": "10"}, {"fileID": "11"}],
            "m_RootBone": {"fileID": "10"},
        }

        motor6ds, attrs = convert_skinned_mesh_renderer(props, scene_nodes=scene_nodes)

        assert len(motor6ds) == 1, "child bone must produce one Motor6D joint"
        assert motor6ds[0].part0_name == "Hips"
        assert motor6ds[0].part1_name == "Spine"
        assert attrs["_BoneCount"] == 2
        assert attrs["_RootBone"] == "Hips"
        assert attrs["_BoneHierarchy"] == "Spine:Hips"

    def test_bone_position_uses_unity_to_roblox_z_negation(self):
        """C0 offset must scale by STUDS_PER_METER and negate Z."""
        from converter.component_converter import convert_skinned_mesh_renderer
        import config

        from core.unity_types import SceneNode
        child = SceneNode(
            name="Child", file_id="2", active=True, layer=0, tag="",
            parent_file_id="1",
            position=(0.0, 0.0, 1.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
        )
        parent = SceneNode(
            name="Parent", file_id="1", active=True, layer=0, tag="",
            position=(0.0, 0.0, 0.0),
            rotation=(0.0, 0.0, 0.0, 1.0),
        )
        props = {
            "m_Bones": [{"fileID": "1"}, {"fileID": "2"}],
            "m_RootBone": {"fileID": "1"},
        }

        motor6ds, _ = convert_skinned_mesh_renderer(
            props, scene_nodes={"1": parent, "2": child},
        )
        assert motor6ds[0].c0.z == pytest.approx(-config.STUDS_PER_METER)

    def test_unresolved_bones_skipped_silently(self):
        """A bone whose fileID is missing from scene_nodes must be skipped."""
        from converter.component_converter import convert_skinned_mesh_renderer

        scene_nodes = {"10": self._make_node("10", "Hips")}
        props = {"m_Bones": [{"fileID": "10"}, {"fileID": "999"}]}

        motor6ds, attrs = convert_skinned_mesh_renderer(props, scene_nodes=scene_nodes)
        assert motor6ds == []  # only one bone resolved, no parent chain
        assert attrs["_BoneCount"] == 1
