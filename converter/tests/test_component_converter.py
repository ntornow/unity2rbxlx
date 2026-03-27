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
        anchored, can_collide = convert_rigidbody(props)
        assert anchored is True

    def test_dynamic_rigidbody(self):
        props = {"m_IsKinematic": 0}
        anchored, can_collide = convert_rigidbody(props)
        assert anchored is False


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
