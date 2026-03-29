"""
component_converter.py -- Convert Unity components to Roblox equivalents.

Handles lights, audio sources, colliders, rigidbodies, and cameras,
producing typed Roblox data objects.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from core.roblox_types import (
    RbxBeam, RbxCameraConfig, RbxCFrame, RbxConstraint, RbxLight,
    RbxMotor6D, RbxPart, RbxParticleEmitter, RbxPostProcessing,
    RbxReverbSoundEffect, RbxSound, RbxTerrain, RbxTrail,
    RbxVideoFrame,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Light conversion
# ---------------------------------------------------------------------------

# Unity Light.type values.
_LIGHT_TYPE_SPOT = 0
_LIGHT_TYPE_DIRECTIONAL = 1
_LIGHT_TYPE_POINT = 2
_LIGHT_TYPE_AREA = 3  # Not directly supported in Roblox.


def convert_light(properties: dict[str, Any]) -> RbxLight | None:
    """Convert a Unity Light component to an RbxLight.

    Unity Light type mapping:
        0 (Spot)        -> SpotLight
        1 (Directional) -> DirectionalLight (placed in Lighting service)
        2 (Point)       -> PointLight
        3 (Area)        -> SurfaceLight (approximation)

    Args:
        properties: Raw component properties from the Unity Light.

    Returns:
        An RbxLight, or None if the light cannot be converted.
    """
    light_type_int = int(properties.get("m_Type", _LIGHT_TYPE_POINT))

    # Map Unity light type to Roblox class name.
    if light_type_int == _LIGHT_TYPE_SPOT:
        rbx_type = "SpotLight"
    elif light_type_int == _LIGHT_TYPE_DIRECTIONAL:
        # Roblox doesn't have DirectionalLight as a Part child.
        # Directional lighting is handled via the Lighting service.
        return None
    elif light_type_int == _LIGHT_TYPE_POINT:
        rbx_type = "PointLight"
    elif light_type_int == _LIGHT_TYPE_AREA:
        rbx_type = "SurfaceLight"
    else:
        log.warning("Unknown Unity light type: %d", light_type_int)
        rbx_type = "PointLight"

    # Color.
    color_data = properties.get("m_Color", {})
    if isinstance(color_data, dict):
        r = float(color_data.get("r", 1.0))
        g = float(color_data.get("g", 1.0))
        b = float(color_data.get("b", 1.0))
    else:
        r, g, b = 1.0, 1.0, 1.0

    # Intensity -> Brightness.
    # Unity intensity is typically 0-8; Roblox brightness is 0-inf.
    intensity = float(properties.get("m_Intensity", 1.0))
    brightness = intensity

    # Range.
    range_val = float(properties.get("m_Range", 10.0))
    # Unity range is in meters; convert to Roblox studs (1 stud ≈ 0.28m).
    import config
    rbx_range = range_val * config.STUDS_PER_METER

    # Spot angle (for SpotLight only).
    spot_angle = float(properties.get("m_SpotAngle", 30.0))

    # Shadows.
    shadow_type = int(properties.get("m_Shadows", {}).get("m_Type", 0)
                      if isinstance(properties.get("m_Shadows"), dict)
                      else properties.get("m_Shadows", 0))
    has_shadows = shadow_type > 0

    return RbxLight(
        light_type=rbx_type,
        brightness=brightness,
        color=(r, g, b),
        range=rbx_range,
        angle=spot_angle if rbx_type == "SpotLight" else 0.0,
        shadows=has_shadows,
    )


# ---------------------------------------------------------------------------
# Audio conversion
# ---------------------------------------------------------------------------

def convert_audio(
    properties: dict[str, Any],
    guid_index: Any | None = None,
    uploaded_assets: dict[str, str] | None = None,
) -> RbxSound | None:
    """Convert a Unity AudioSource component to an RbxSound.

    Maps:
        m_Volume      -> Volume
        m_Pitch       -> PlaybackSpeed
        m_Loop        -> Looped
        m_PlayOnAwake -> Playing (initial state)
        m_audioClip   -> SoundId (resolved via guid_index + uploaded_assets)
        m_MinDistance  -> RollOffMinDistance
        m_MaxDistance  -> RollOffMaxDistance

    Args:
        properties: Raw component properties from the Unity AudioSource.
        guid_index: GUID -> path resolver for looking up audio clip paths.
        uploaded_assets: Dict mapping local asset paths to rbxassetid:// URLs.

    Returns:
        An RbxSound, or None if conversion fails.
    """
    volume = float(properties.get("m_Volume", 1.0))
    # Clamp volume to Roblox range [0, 10].
    volume = max(0.0, min(volume, 10.0))

    pitch = float(properties.get("m_Pitch", 1.0))
    # Roblox PlaybackSpeed: 0 to 6.
    playback_speed = max(0.0, min(pitch, 6.0))

    looped = bool(int(properties.get("m_Loop", 0)))
    play_on_awake = bool(int(properties.get("m_PlayOnAwake", 1)))

    min_distance = float(properties.get("m_MinDistance", 1.0))
    max_distance = float(properties.get("m_MaxDistance", 500.0))

    # Resolve audio clip GUID to rbxassetid:// URL.
    sound_id = ""
    audio_clip = properties.get("m_audioClip", {})
    if isinstance(audio_clip, dict):
        guid = audio_clip.get("guid", "")
        if guid and guid_index and uploaded_assets:
            audio_path = guid_index.resolve(guid)
            if audio_path:
                # Try multiple key formats
                relative = guid_index.resolve_relative(guid) if hasattr(guid_index, "resolve_relative") else None
                candidates = [str(audio_path)]
                if relative:
                    candidates.append(str(relative))
                for key in list(candidates):
                    candidates.append(key.replace("\\", "/"))
                    candidates.append(key.replace("/", "\\"))
                for key in candidates:
                    if key in uploaded_assets:
                        sound_id = uploaded_assets[key]
                        break

    return RbxSound(
        sound_id=sound_id,
        volume=volume,
        playback_speed=playback_speed,
        looped=looped,
        playing=play_on_awake,
        roll_off_min_distance=min_distance,
        roll_off_max_distance=max_distance,
    )


# ---------------------------------------------------------------------------
# Collider conversion
# ---------------------------------------------------------------------------

def _extract_collider_center(properties: dict[str, Any]) -> tuple[float, float, float]:
    """Extract collider center offset in studs (Unity m_Center → Roblox offset).

    Unity colliders can have a non-zero center offset from the GameObject's
    transform.  This offset needs to be applied to the Part's CFrame position.
    Returns (dx, dy, dz) in studs with Roblox coordinate conventions (Z negated).
    """
    import config

    center = properties.get("m_Center", {})
    if not isinstance(center, dict):
        return (0.0, 0.0, 0.0)

    cx = float(center.get("x", 0.0))
    cy = float(center.get("y", 0.0))
    cz = float(center.get("z", 0.0))

    if cx == 0.0 and cy == 0.0 and cz == 0.0:
        return (0.0, 0.0, 0.0)

    # Convert to studs and negate Z for Roblox coordinate system
    return (
        cx * config.STUDS_PER_METER,
        cy * config.STUDS_PER_METER,
        -cz * config.STUDS_PER_METER,
    )


def convert_collider(
    component_type: str,
    properties: dict[str, Any],
    current_size: tuple[float, float, float],
) -> tuple[tuple[float, float, float], bool, tuple[float, float, float]]:
    """Convert a Unity Collider component to adjusted size, collision flag, and center offset.

    Unity colliders adjust the effective bounding volume:
        BoxCollider     -> adjust size by center + size
        SphereCollider  -> uniform size from radius
        CapsuleCollider -> capsule approximation

    Args:
        component_type: The Unity component type name.
        properties: Raw component properties.
        current_size: The current part size (x, y, z).

    Returns:
        Tuple of (adjusted_size, can_collide, center_offset_studs).
        center_offset_studs is (dx, dy, dz) in Roblox coordinates.
    """
    is_trigger = bool(int(properties.get("m_IsTrigger", 0)))
    can_collide = not is_trigger  # Triggers don't collide.
    no_offset = (0.0, 0.0, 0.0)

    if component_type == "BoxCollider":
        center = _extract_collider_center(properties)
        return _convert_box_collider(properties, current_size), can_collide, center

    elif component_type == "SphereCollider":
        center = _extract_collider_center(properties)
        return _convert_sphere_collider(properties, current_size), can_collide, center

    elif component_type == "CapsuleCollider":
        center = _extract_collider_center(properties)
        return _convert_capsule_collider(properties, current_size), can_collide, center

    elif component_type == "MeshCollider":
        # MeshCollider uses the mesh shape; size stays the same.
        return current_size, can_collide, no_offset

    # 2D colliders: convert to thin 3D equivalents
    elif component_type == "BoxCollider2D":
        return _convert_box_collider_2d(properties, current_size), can_collide, no_offset

    elif component_type in ("CircleCollider2D", "CapsuleCollider2D"):
        return _convert_circle_collider_2d(properties, current_size), can_collide, no_offset

    elif component_type in ("PolygonCollider2D", "EdgeCollider2D"):
        # Complex 2D shapes: keep current size (can't easily approximate)
        return current_size, can_collide, no_offset

    elif component_type == "WheelCollider":
        # WheelCollider for vehicles — approximate as cylinder
        import config
        radius = float(properties.get("m_Radius", 0.5)) * config.STUDS_PER_METER
        diameter = radius * 2
        # WheelCollider is round — use diameter for all dimensions
        return (diameter, diameter, diameter), can_collide, no_offset

    else:
        log.warning("Unknown collider type: %s", component_type)
        return current_size, can_collide, no_offset


def _convert_box_collider(
    properties: dict[str, Any],
    current_size: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Adjust part size based on BoxCollider center and size.

    Unity BoxCollider.m_Size is in local object-space units (meters).
    For primitive Parts (Cube etc.), m_Size=(1,1,1) means "same as mesh",
    so multiplying by part scale works.  For MeshParts with custom mesh
    sizing, m_Size represents actual collider dimensions in meters.
    We convert directly to studs.
    """
    import config

    size_data = properties.get("m_Size", {})
    if isinstance(size_data, dict):
        sx = float(size_data.get("x", 1.0))
        sy = float(size_data.get("y", 1.0))
        sz = float(size_data.get("z", 1.0))
    else:
        sx, sy, sz = 1.0, 1.0, 1.0

    # Convert collider dimensions from meters to studs.
    collider_studs = (
        abs(sx * config.STUDS_PER_METER),
        abs(sy * config.STUDS_PER_METER),
        abs(sz * config.STUDS_PER_METER),
    )

    # Use whichever is larger: the mesh size or the collider size.
    return (
        max(current_size[0], collider_studs[0]),
        max(current_size[1], collider_studs[1]),
        max(current_size[2], collider_studs[2]),
    )


def _convert_sphere_collider(
    properties: dict[str, Any],
    current_size: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Adjust part size based on SphereCollider radius."""
    import config
    radius = float(properties.get("m_Radius", 0.5))
    diameter = radius * 2.0 * config.STUDS_PER_METER
    return (
        max(current_size[0], diameter),
        max(current_size[1], diameter),
        max(current_size[2], diameter),
    )


def _convert_capsule_collider(
    properties: dict[str, Any],
    current_size: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Adjust part size based on CapsuleCollider radius and height."""
    import config
    radius = float(properties.get("m_Radius", 0.5))
    height = float(properties.get("m_Height", 2.0))
    direction = int(properties.get("m_Direction", 1))  # 0=X, 1=Y, 2=Z

    diameter = radius * 2.0 * config.STUDS_PER_METER
    capsule_height = height * config.STUDS_PER_METER

    if direction == 0:
        return (max(current_size[0], capsule_height), max(current_size[1], diameter), max(current_size[2], diameter))
    elif direction == 1:
        return (max(current_size[0], diameter), max(current_size[1], capsule_height), max(current_size[2], diameter))
    else:
        return (max(current_size[0], diameter), max(current_size[1], diameter), max(current_size[2], capsule_height))


# ---------------------------------------------------------------------------
# Rigidbody conversion
# ---------------------------------------------------------------------------

def _convert_box_collider_2d(
    properties: dict[str, Any],
    current_size: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Convert a 2D BoxCollider to a thin 3D box."""
    import config
    size_data = properties.get("m_Size", {})
    if isinstance(size_data, dict):
        sx = float(size_data.get("x", 1.0))
        sy = float(size_data.get("y", 1.0))
    else:
        sx, sy = 1.0, 1.0
    return (
        max(current_size[0], abs(sx * config.STUDS_PER_METER)),
        max(current_size[1], abs(sy * config.STUDS_PER_METER)),
        max(current_size[2], 0.5),  # Thin Z for 2D
    )


def _convert_circle_collider_2d(
    properties: dict[str, Any],
    current_size: tuple[float, float, float],
) -> tuple[float, float, float]:
    """Convert a 2D CircleCollider/CapsuleCollider to a thin sphere approximation."""
    import config
    radius = float(properties.get("m_Radius", 0.5))
    diameter = radius * 2 * config.STUDS_PER_METER
    return (
        max(current_size[0], diameter),
        max(current_size[1], diameter),
        max(current_size[2], 0.5),  # Thin Z for 2D
    )


def convert_rigidbody(
    properties: dict[str, Any],
) -> tuple[bool, bool, tuple[float, float, float, float, float] | None]:
    """Convert a Unity Rigidbody component to anchored/canCollide/physics properties.

    Anchoring logic:
        isKinematic = True  -> anchored = True  (script-driven movement)
        isKinematic = False -> anchored = False (physics-driven movement)
        All position axes frozen (constraints & 0b111 == 0b111) -> anchored = True

    Unity m_Constraints bitmask:
        bit 1: FreezePositionX, bit 2: FreezePositionY, bit 3: FreezePositionZ
        bit 4: FreezeRotationX, bit 5: FreezeRotationY, bit 6: FreezeRotationZ

    Physics property mapping:
        Unity mass (kg) -> Roblox density via mass / volume
        Unity drag -> Roblox friction approximation (0=frictionless, higher=more friction)
        Unity useGravity=false -> store as attribute for script use

    Args:
        properties: Raw component properties.

    Returns:
        Tuple of (anchored, can_collide, custom_physical_properties).
        custom_physical_properties is (density, friction, elasticity, frictionWeight, elasticityWeight)
        or None if default physics are fine.
    """
    is_kinematic = bool(int(properties.get("m_IsKinematic", 0)))
    # Roblox: Anchored parts are not affected by physics.
    anchored = is_kinematic

    # Check freeze constraints — if all position axes frozen, treat as anchored.
    constraints = int(properties.get("m_Constraints", 0))
    freeze_pos_all = (constraints & 0b0000_0111) == 0b0000_0111  # bits 0-2
    if freeze_pos_all:
        anchored = True

    # canCollide is generally True for rigidbodies unless explicitly disabled.
    can_collide = True

    # Check for detectCollisions flag (Unity).
    detect_collisions = properties.get("m_DetectCollisions", 1)
    if not bool(int(detect_collisions)):
        can_collide = False

    # Extract physics properties for CustomPhysicalProperties
    # Rigidbody uses m_Drag, Rigidbody2D uses m_LinearDrag
    mass = float(properties.get("m_Mass", 1.0))
    drag = float(properties.get("m_Drag", properties.get("m_LinearDrag", 0.0)))
    angular_drag = float(properties.get("m_AngularDrag", 0.05))
    use_gravity = bool(int(properties.get("m_UseGravity", 1)))

    # Only set CustomPhysicalProperties if mass differs from default
    # Roblox density default is ~0.7 (based on default Part mass/volume ratio)
    # Unity mass is in kg; Roblox density = mass / volume (in studs³)
    # We can't compute exact density without knowing part volume here,
    # so we scale relative to Unity's default mass of 1.0:
    #   mass > 1 → higher density, mass < 1 → lower density
    # Roblox default density is 0.7, so: density = 0.7 * mass
    custom_phys = None
    has_non_default = (mass != 1.0 or drag > 0.01 or angular_drag > 0.06)
    if has_non_default and not anchored:
        density = max(0.01, 0.7 * mass)
        # Unity drag maps loosely to Roblox friction (both resist motion)
        # drag=0 → friction=0.3 (default), drag=1 → friction=0.7, drag=10 → friction=1.0
        friction = min(1.0, 0.3 + drag * 0.4)
        # Default elasticity (bounciness) — Unity PhysicMaterial handles this
        # but we have no PhysicMaterial data here, use a reasonable default
        elasticity = 0.5
        custom_phys = (density, friction, elasticity, 1.0, 1.0)

    return anchored, can_collide, custom_phys


# ---------------------------------------------------------------------------
# Camera conversion
# ---------------------------------------------------------------------------

def convert_camera(
    properties: dict[str, Any],
) -> RbxCameraConfig | None:
    """Convert a Unity Camera component to an RbxCameraConfig.

    Maps:
        field of view         -> FieldOfView
        near clip plane       -> NearClip
        far clip plane        -> FarClip

    Args:
        properties: Raw component properties.

    Returns:
        An RbxCameraConfig, or None if conversion fails.
    """
    fov = float(properties.get("field of view", 60.0))
    near_clip = float(properties.get("near clip plane", 0.3))
    far_clip = float(properties.get("far clip plane", 1000.0))

    # Unity default FOV is vertical; Roblox uses vertical FOV too.
    # Clamp to Roblox valid range (1 - 120).
    fov = max(1.0, min(fov, 120.0))

    return RbxCameraConfig(
        field_of_view=fov,
        near_clip=near_clip,
        far_clip=far_clip,
    )


# ---------------------------------------------------------------------------
# ParticleSystem conversion
# ---------------------------------------------------------------------------

def convert_particle_system(properties: dict[str, Any]) -> RbxParticleEmitter | None:
    """Convert a Unity ParticleSystem component to an RbxParticleEmitter.

    Maps the main module properties:
        startLifetime         -> Lifetime (NumberRange)
        startSpeed            -> Speed (NumberRange)
        startSize             -> Size (NumberSequence)
        startColor            -> Color (ColorSequence)
        maxParticles          -> Rate approximation
        emission.rateOverTime -> Rate
        shape module          -> ShapeStyle / ShapeInOut / SpreadAngle
        gravityModifier       -> Acceleration (Y component)
        colorOverLifetime     -> Color (ColorSequence)
        sizeOverLifetime      -> Size (NumberSequence)
        velocityOverLifetime  -> Speed adjustment
        rotationOverLifetime  -> RotSpeed (NumberRange)
        noiseModule           -> Speed range randomization
        forceOverLifetime     -> Acceleration vector
        collision module      -> stored as attribute
        subEmitters           -> stored as attribute

    Args:
        properties: Raw component properties from the Unity ParticleSystem.

    Returns:
        An RbxParticleEmitter, or None if conversion fails.
    """
    emitter = RbxParticleEmitter()

    # Main module (nested under various keys depending on Unity version)
    main = properties

    # playOnAwake: if false, the emitter should start disabled
    play_on_awake = main.get("playOnAwake", main.get("m_PlayOnAwake", 1))
    if not bool(int(play_on_awake)):
        emitter.enabled = False

    # Emission rate
    emission = main.get("EmissionModule", main.get("emission", {}))
    if isinstance(emission, dict):
        rate_data = emission.get("rateOverTime", emission.get("rate", {}))
        emitter.rate = _extract_scalar(rate_data, default=20.0)

    # Lifetime
    lifetime_data = main.get("startLifetime", main.get("StartLifetime", {}))
    if isinstance(lifetime_data, dict):
        mode = lifetime_data.get("mode", 0)
        if mode == 0:  # Constant
            val = float(lifetime_data.get("scalar", lifetime_data.get("value", 2.0)))
            emitter.lifetime_min = val
            emitter.lifetime_max = val
        elif mode == 3:  # Random between two constants
            emitter.lifetime_min = float(lifetime_data.get("minScalar", 1.0))
            emitter.lifetime_max = float(lifetime_data.get("scalar", 2.0))

    # Speed
    speed_data = main.get("startSpeed", main.get("StartSpeed", {}))
    if isinstance(speed_data, dict):
        mode = speed_data.get("mode", 0)
        if mode == 0:
            val = float(speed_data.get("scalar", speed_data.get("value", 5.0)))
            emitter.speed_min = val
            emitter.speed_max = val
        elif mode == 3:
            emitter.speed_min = float(speed_data.get("minScalar", 2.0))
            emitter.speed_max = float(speed_data.get("scalar", 5.0))

    # Size
    size_data = main.get("startSize", main.get("StartSize", {}))
    if isinstance(size_data, dict):
        mode = size_data.get("mode", 0)
        if mode == 0:
            val = float(size_data.get("scalar", size_data.get("value", 1.0)))
            emitter.size_min = val
            emitter.size_max = val
        elif mode == 3:
            emitter.size_min = float(size_data.get("minScalar", 0.5))
            emitter.size_max = float(size_data.get("scalar", 1.0))

    # Color
    color_data = main.get("startColor", main.get("StartColor", {}))
    if isinstance(color_data, dict):
        rgba = color_data.get("rgba", color_data.get("maxColor", {}))
        if isinstance(rgba, dict):
            emitter.color = (
                float(rgba.get("r", 1.0)),
                float(rgba.get("g", 1.0)),
                float(rgba.get("b", 1.0)),
            )
            alpha = float(rgba.get("a", 1.0))
            emitter.transparency = 1.0 - alpha

    # -------------------------------------------------------------------
    # Shape module -> ShapeStyle / ShapeInOut / SpreadAngle
    # -------------------------------------------------------------------
    shape = main.get("ShapeModule", main.get("shape", {}))
    if isinstance(shape, dict):
        shape_enabled = bool(int(shape.get("enabled", 1)))
        if shape_enabled:
            angle = float(shape.get("angle", 25.0))
            emitter.spread_angle = min(angle, 180.0)

            # Unity shape types: 0=Sphere, 1=Hemisphere, 2=Cone, 3=Box,
            # 4=Mesh, 5=MeshRenderer, 6=SkinnedMeshRenderer, 7=Circle,
            # 8=Edge, 12=Rectangle, 17=Donut
            shape_type = int(shape.get("type", 2))  # default Cone
            if shape_type in (0, 1):  # Sphere / Hemisphere
                emitter.shape_style = "Sphere"
            elif shape_type == 2:  # Cone
                emitter.shape_style = "Cylinder"
            elif shape_type == 3:  # Box
                emitter.shape_style = "Block"
            elif shape_type in (7, 17):  # Circle / Donut
                emitter.shape_style = "Disc"
            else:
                # Mesh-based or Edge -> default to Cylinder
                emitter.shape_style = "Cylinder"

            # radiusThickness: 1.0 = surface only, 0.0 = volume fill
            radius_thickness = float(shape.get("radiusThickness", 1.0))
            if radius_thickness >= 0.9:
                emitter.shape_in_out = "Outward"
            elif radius_thickness <= 0.1:
                emitter.shape_in_out = "InAndOut"
            else:
                emitter.shape_in_out = "Outward"

    # -------------------------------------------------------------------
    # Color over lifetime -> ColorSequence
    # -------------------------------------------------------------------
    col_lifetime = main.get("ColorBySpeedModule",
                            main.get("ColorModule",
                            main.get("colorOverLifetime",
                            main.get("ColorOverLifetimeModule", {}))))
    if isinstance(col_lifetime, dict) and bool(int(col_lifetime.get("enabled", 0))):
        gradient = col_lifetime.get("gradient", col_lifetime.get("m_Gradient", {}))
        color_seq = _extract_color_gradient(gradient)
        if color_seq:
            emitter.color_sequence = color_seq

    # -------------------------------------------------------------------
    # Size over lifetime -> NumberSequence for size
    # -------------------------------------------------------------------
    size_lifetime = main.get("SizeBySpeedModule",
                             main.get("SizeModule",
                             main.get("sizeOverLifetime",
                             main.get("SizeOverLifetimeModule", {}))))
    if isinstance(size_lifetime, dict) and bool(int(size_lifetime.get("enabled", 0))):
        curve_data = size_lifetime.get("curve", size_lifetime.get("size", {}))
        size_seq = _extract_curve_to_number_sequence(curve_data)
        if size_seq:
            # Scale by the start size
            base_size = (emitter.size_min + emitter.size_max) / 2.0
            emitter.size_sequence = [
                (t, v * base_size, e * base_size) for t, v, e in size_seq
            ]

    # -------------------------------------------------------------------
    # Velocity over lifetime -> add to speed range
    # -------------------------------------------------------------------
    vel_lifetime = main.get("VelocityModule",
                            main.get("velocityOverLifetime",
                            main.get("VelocityOverLifetimeModule", {})))
    if isinstance(vel_lifetime, dict) and bool(int(vel_lifetime.get("enabled", 0))):
        vx = _extract_scalar(vel_lifetime.get("x", {}), default=0.0)
        vy = _extract_scalar(vel_lifetime.get("y", {}), default=0.0)
        vz = _extract_scalar(vel_lifetime.get("z", {}), default=0.0)
        vel_mag = math.sqrt(vx * vx + vy * vy + vz * vz)
        if vel_mag > 0:
            emitter.speed_min = max(0.0, emitter.speed_min - vel_mag * 0.5)
            emitter.speed_max += vel_mag

    # -------------------------------------------------------------------
    # Rotation over lifetime -> RotSpeed
    # -------------------------------------------------------------------
    rot_lifetime = main.get("RotationBySpeedModule",
                            main.get("RotationModule",
                            main.get("rotationOverLifetime",
                            main.get("RotationOverLifetimeModule", {}))))
    if isinstance(rot_lifetime, dict) and bool(int(rot_lifetime.get("enabled", 0))):
        rot_curve = rot_lifetime.get("curve",
                                     rot_lifetime.get("angularVelocity", {}))
        if isinstance(rot_curve, dict):
            mode = rot_curve.get("mode", 0)
            if mode == 0:  # Constant
                # Unity stores radians/s; Roblox uses degrees/s
                val = float(rot_curve.get("scalar",
                            rot_curve.get("value", 0.0)))
                deg = math.degrees(val)
                emitter.rot_speed_min = deg
                emitter.rot_speed_max = deg
            elif mode == 3:  # Random between two constants
                min_val = math.degrees(
                    float(rot_curve.get("minScalar", 0.0)))
                max_val = math.degrees(
                    float(rot_curve.get("scalar", 1.0)))
                emitter.rot_speed_min = min_val
                emitter.rot_speed_max = max_val

    # -------------------------------------------------------------------
    # Noise module -> slight randomization to speed range
    # -------------------------------------------------------------------
    noise = main.get("NoiseModule", main.get("noise", {}))
    if isinstance(noise, dict) and bool(int(noise.get("enabled", 0))):
        strength = _extract_scalar(
            noise.get("strength", noise.get("strengthX", {})), default=0.0)
        frequency = _extract_scalar(noise.get("frequency", {}), default=1.0)
        if strength > 0:
            noise_offset = strength * frequency * 0.5
            emitter.speed_min = max(0.0, emitter.speed_min - noise_offset)
            emitter.speed_max += noise_offset

    # -------------------------------------------------------------------
    # Force over lifetime -> Acceleration vector
    # -------------------------------------------------------------------
    force_lifetime = main.get("ForceModule",
                              main.get("forceOverLifetime",
                              main.get("ForceOverLifetimeModule", {})))
    if isinstance(force_lifetime, dict) and bool(int(force_lifetime.get("enabled", 0))):
        import config as _cfg
        fx = _extract_scalar(force_lifetime.get("x", {}), default=0.0)
        fy = _extract_scalar(force_lifetime.get("y", {}), default=0.0)
        fz = _extract_scalar(force_lifetime.get("z", {}), default=0.0)
        # Convert from Unity m/s^2 to Roblox studs/s^2, with Z-flip
        ax = fx * _cfg.STUDS_PER_METER
        ay = fy * _cfg.STUDS_PER_METER
        az = -fz * _cfg.STUDS_PER_METER
        prev = emitter.acceleration
        emitter.acceleration = (prev[0] + ax, prev[1] + ay, prev[2] + az)

    # -------------------------------------------------------------------
    # Collision module -> store as attribute (no direct Roblox equivalent)
    # -------------------------------------------------------------------
    collision = main.get("CollisionModule", main.get("collision", {}))
    if isinstance(collision, dict) and bool(int(collision.get("enabled", 0))):
        col_type = int(collision.get("type", 0))  # 0=Planes, 1=World
        emitter.attributes["UnityCollisionType"] = (
            "World" if col_type == 1 else "Planes")
        bounce_raw = collision.get("m_Bounce", collision.get("bounce", {}))
        emitter.attributes["UnityCollisionBounce"] = (
            _extract_scalar(bounce_raw, default=0.0))
        emitter.attributes["UnityCollisionDampen"] = _extract_scalar(
            collision.get("dampen", {}), default=0.0)
        emitter.attributes["UnityCollisionLifetimeLoss"] = _extract_scalar(
            collision.get("lifetimeLoss",
                          collision.get("lifetimeLossOnCollision", {})),
            default=0.0)
        log.info("ParticleSystem collision module stored as attributes "
                 "(no direct Roblox equivalent)")

    # -------------------------------------------------------------------
    # Sub-emitters -> store as attribute (would need runtime script)
    # -------------------------------------------------------------------
    sub_emitters = main.get("SubModule",
                            main.get("subEmitters",
                            main.get("SubEmittersModule", {})))
    if isinstance(sub_emitters, dict) and bool(int(sub_emitters.get("enabled", 0))):
        sub_list = sub_emitters.get("subEmitters", [])
        if isinstance(sub_list, list) and sub_list:
            emitter.attributes["UnitySubEmitterCount"] = len(sub_list)
            triggers = []
            for se in sub_list:
                if isinstance(se, dict):
                    # type: 0=Birth, 1=Collision, 2=Death, 3=Trigger,
                    #        4=Manual
                    trigger_type = int(se.get("type", 0))
                    trigger_names = {
                        0: "Birth", 1: "Collision", 2: "Death",
                        3: "Trigger", 4: "Manual",
                    }
                    triggers.append(
                        trigger_names.get(trigger_type, "Unknown"))
            if triggers:
                emitter.attributes["UnitySubEmitterTriggers"] = (
                    ",".join(triggers))
            emitter.attributes["_HasSubEmitters"] = True
            log.info("ParticleSystem sub-emitters: %d triggers (%s)",
                     len(sub_list), ",".join(triggers))

    # Gravity modifier -> acceleration Y component
    import config
    gravity_mod = _extract_scalar(main.get("gravityModifier", {}), default=0.0)
    if gravity_mod != 0.0:
        # Unity gravity is -9.81, Roblox is -196.2. gravity_mod multiplies Unity gravity.
        prev = emitter.acceleration
        emitter.acceleration = (prev[0], prev[1] + (-gravity_mod * 196.2), prev[2])

    # Drag (simulation speed damping)
    damping = _extract_scalar(main.get("dampen", main.get("Dampen", {})), default=0.0)
    if damping > 0:
        emitter.drag = damping * 10.0  # Roblox drag scale differs from Unity

    # Velocity inheritance
    inherit = _extract_scalar(main.get("inheritVelocity", {}), default=0.0)
    emitter.velocity_inheritance = min(1.0, abs(inherit))

    # LockedToPart: if simulation space is local, lock to part
    sim_space = int(main.get("simulationSpace", main.get("moveWithTransform", 0)))
    if sim_space == 1:  # Local space
        emitter.locked_to_part = True

    # Light emission from renderer module
    renderer = main.get("RendererModule", main.get("renderer", {}))
    if isinstance(renderer, dict):
        pass

    # Max particles -> clamp rate
    max_particles = int(main.get("maxNumParticles", main.get("maxParticles", 1000)))
    if max_particles < 50:
        emitter.rate = min(emitter.rate, float(max_particles))

    return emitter


# ---------------------------------------------------------------------------
# Tilemap conversion
# ---------------------------------------------------------------------------


def convert_tilemap(
    properties: dict[str, Any],
    cell_size: tuple[float, float, float] | None = None,
) -> list[RbxPart]:
    """Convert a Unity Tilemap component to a list of RbxParts (one per tile).

    Unity Tilemap stores tile data as a grid of sprite/tile references at
    integer positions.  Each tile becomes a thin Part positioned at the
    corresponding grid coordinate, scaled by the cell size.

    Args:
        properties: Raw component properties from the Unity Tilemap.
        cell_size: Override for cell size (x, y, z) in Unity units.
            Defaults to (1, 1, 1) if not provided and not in properties.

    Returns:
        A list of RbxPart instances, one per tile.
    """
    import config

    parts: list[RbxPart] = []

    # Cell size from the Grid parent or from properties
    if cell_size is None:
        cs = properties.get("m_CellSize", properties.get("cellSize", {}))
        if isinstance(cs, dict):
            cx = float(cs.get("x", 1.0))
            cy = float(cs.get("y", 1.0))
            cz = float(cs.get("z", 1.0))
            cell_size = (cx, cy, cz)
        else:
            cell_size = (1.0, 1.0, 1.0)

    # Tile thickness in studs (tiles are flat, like sprites)
    tile_thickness = 0.2

    # Extract tile data -- Unity stores tiles in m_Tiles as a list of entries
    tiles = properties.get("m_Tiles", [])
    if not isinstance(tiles, list):
        tiles = []

    # Also check for m_CompressedTiles (newer Unity format)
    if not tiles:
        tiles = properties.get("m_CompressedTiles", [])
        if not isinstance(tiles, list):
            tiles = []

    # Extract tile color array if available
    tile_colors = properties.get("m_TileColorArray", properties.get("m_Colors", []))
    if not isinstance(tile_colors, list):
        tile_colors = []

    for i, tile_entry in enumerate(tiles):
        if not isinstance(tile_entry, dict):
            continue

        # Position: stored in first/second keys or as m_Position or position
        pos = tile_entry.get("first", tile_entry.get("m_Position", tile_entry.get("position", {})))
        if isinstance(pos, dict):
            tx = int(float(pos.get("x", 0)))
            ty = int(float(pos.get("y", 0)))
            tz = int(float(pos.get("z", 0)))
        else:
            continue  # No valid position, skip

        # Tile reference (sprite or tile asset)
        tile_data = tile_entry.get("second", tile_entry.get("m_TileData", tile_entry.get("tile", {})))
        if isinstance(tile_data, dict):
            # Check if tile reference is null/empty
            tile_ref = tile_data.get("m_Tile", tile_data.get("tile", tile_data))
            if isinstance(tile_ref, dict):
                file_id = tile_ref.get("fileID", 0)
                if file_id == 0 and not tile_ref.get("guid"):
                    continue  # Empty tile slot
        elif tile_data == 0 or tile_data is None:
            continue  # Empty tile

        # Convert grid position to Roblox world coordinates
        # Unity 2D: X is right, Y is up; Roblox: X right, Y up, -Z forward
        stud_x = tx * cell_size[0] * config.STUDS_PER_METER
        stud_y = ty * cell_size[1] * config.STUDS_PER_METER
        stud_z = -(tz * cell_size[2] * config.STUDS_PER_METER)

        # Size: one cell in X/Z, thin in Y (like a sprite)
        part_sx = cell_size[0] * config.STUDS_PER_METER
        part_sy = tile_thickness
        part_sz = cell_size[1] * config.STUDS_PER_METER  # Y cell maps to Z depth

        # Extract color if available
        color = (0.63, 0.63, 0.63)
        transparency = 0.0
        if i < len(tile_colors):
            tc = tile_colors[i]
            if isinstance(tc, dict):
                r = float(tc.get("r", 0.63))
                g = float(tc.get("g", 0.63))
                b = float(tc.get("b", 0.63))
                a = float(tc.get("a", 1.0))
                color = (r, g, b)
                if a < 1.0:
                    transparency = 1.0 - a

        tile_part = RbxPart(
            name=f"Tile_{tx}_{ty}",
            class_name="Part",
            cframe=RbxCFrame(x=stud_x, y=stud_y, z=stud_z),
            size=(part_sx, part_sy, part_sz),
            color=color,
            transparency=transparency,
            anchored=True,
            can_collide=True,
            cast_shadow=False,  # Tiles generally don't need shadow casting
        )

        # Store tile metadata as attributes
        tile_part.attributes["_TileGridX"] = tx
        tile_part.attributes["_TileGridY"] = ty
        if isinstance(tile_data, dict):
            sprite_ref = tile_data.get("m_Sprite", tile_data.get("sprite", {}))
            if isinstance(sprite_ref, dict):
                guid = sprite_ref.get("guid", "")
                if guid:
                    tile_part.attributes["_SpriteGuid"] = guid

        parts.append(tile_part)

    # If no explicit tile entries, check for m_Size to create a placeholder grid
    if not parts:
        grid_size = properties.get("m_Size", {})
        if isinstance(grid_size, dict):
            gx = int(float(grid_size.get("x", 0)))
            gy = int(float(grid_size.get("y", 0)))
            if gx > 0 and gy > 0:
                log.info("Tilemap has m_Size %dx%d but no tile entries found; "
                         "storing grid dimensions as attributes", gx, gy)

    return parts


def convert_tilemap_renderer(
    properties: dict[str, Any],
) -> dict[str, Any]:
    """Extract TilemapRenderer properties as attributes.

    TilemapRenderer controls rendering order and mode for the tilemap.
    Since Roblox doesn't have a direct tilemap renderer, we store
    the sort order and rendering mode as attributes for reference.

    Args:
        properties: Raw component properties from the Unity TilemapRenderer.

    Returns:
        A dict of attributes to attach to the parent part/folder.
    """
    attrs: dict[str, Any] = {}

    # Sort order (m_SortOrder or m_SortingOrder)
    sort_order = properties.get("m_SortingOrder",
                                properties.get("m_SortOrder", 0))
    attrs["_TilemapSortOrder"] = int(sort_order)

    # Sorting layer
    sorting_layer = properties.get("m_SortingLayerID",
                                   properties.get("m_SortingLayer", 0))
    if sorting_layer:
        attrs["_TilemapSortingLayer"] = int(sorting_layer)

    # Render mode: 0=Chunk, 1=Individual
    mode = properties.get("m_Mode", properties.get("mode", 0))
    attrs["_TilemapRenderMode"] = "Individual" if int(mode) == 1 else "Chunk"

    # Detect tile anchor (defaults to 0.5, 0.5 for center)
    anchor = properties.get("m_TileAnchor", {})
    if isinstance(anchor, dict):
        ax = float(anchor.get("x", 0.5))
        ay = float(anchor.get("y", 0.5))
        if abs(ax - 0.5) > 0.01 or abs(ay - 0.5) > 0.01:
            attrs["_TilemapAnchorX"] = ax
            attrs["_TilemapAnchorY"] = ay

    return attrs


# ---------------------------------------------------------------------------
# Terrain conversion
# ---------------------------------------------------------------------------

# Default Unity Terrain dimensions (width x height x length) in Unity units.
_DEFAULT_TERRAIN_SIZE = (1000.0, 600.0, 1000.0)


def convert_terrain(
    properties: dict[str, Any],
    node_position: tuple[float, float, float] = (0.0, 0.0, 0.0),
) -> RbxTerrain | None:
    """Convert a Unity Terrain component to an RbxTerrain.

    Unity Terrain stores its landscape data in a referenced TerrainData asset
    (binary).  The scene-level Terrain component itself does not carry the
    terrain size directly, but it references the TerrainData via
    ``m_TerrainData``.  Because the TerrainData asset is binary and not
    trivially parseable, we use Unity's default terrain size
    (1000 x 600 x 1000) as a reasonable fallback.

    The node's Transform position is used to place the terrain in the world.

    Args:
        properties: Raw component properties from the Unity Terrain document.
        node_position: World-space position of the GameObject that owns
            the Terrain component (from its Transform).

    Returns:
        An RbxTerrain, or None if the component looks disabled.
    """
    enabled = properties.get("m_Enabled", 1)
    if not bool(int(enabled)):
        return None

    # Extract the TerrainData GUID for reference / future use.
    terrain_data_ref = properties.get("m_TerrainData", {})
    terrain_data_guid = ""
    if isinstance(terrain_data_ref, dict):
        terrain_data_guid = terrain_data_ref.get("guid", "")

    return RbxTerrain(
        position=node_position,
        size=_DEFAULT_TERRAIN_SIZE,
        terrain_data_guid=terrain_data_guid,
    )


# ---------------------------------------------------------------------------
# Physics joint conversion
# ---------------------------------------------------------------------------

_JOINT_TYPE_MAP = {
    "FixedJoint": "WeldConstraint",
    "HingeJoint": "HingeConstraint",
    "SpringJoint": "SpringConstraint",
    "CharacterJoint": "BallSocketConstraint",
    "ConfigurableJoint": "BallSocketConstraint",  # Best approximation
}


def convert_joint(
    component_type: str,
    properties: dict[str, Any],
) -> RbxConstraint | None:
    """Convert a Unity Joint component to an RbxConstraint.

    Maps:
        FixedJoint       -> WeldConstraint
        HingeJoint       -> HingeConstraint (with limits)
        SpringJoint      -> SpringConstraint (with stiffness/damping)
        CharacterJoint   -> BallSocketConstraint (with twist limits)
        ConfigurableJoint -> BallSocketConstraint (approximation)

    Args:
        component_type: The Unity component type name.
        properties: Raw component properties.

    Returns:
        An RbxConstraint, or None if conversion fails.
    """
    rbx_type = _JOINT_TYPE_MAP.get(component_type)
    if not rbx_type:
        log.warning("Unknown joint type: %s", component_type)
        return None

    constraint = RbxConstraint(constraint_type=rbx_type)

    # Extract connected body reference (for Part1 in Roblox)
    connected_body = properties.get("m_ConnectedBody", {})
    if isinstance(connected_body, dict):
        fid = connected_body.get("fileID", "0")
        if fid and str(fid) != "0":
            constraint.connected_body_file_id = str(fid)

    if component_type == "HingeJoint":
        use_limits = bool(int(properties.get("m_UseLimits", 0)))
        constraint.limits_enabled = use_limits
        limits = properties.get("m_Limits", {})
        if isinstance(limits, dict):
            constraint.lower_angle = float(limits.get("min", -45.0))
            constraint.upper_angle = float(limits.get("max", 45.0))

    elif component_type == "SpringJoint":
        import config
        constraint.stiffness = float(properties.get("m_Spring", 0.0))
        constraint.damping = float(properties.get("m_Damper", 0.0))
        min_dist = float(properties.get("m_MinDistance", 0.0))
        max_dist = float(properties.get("m_MaxDistance", 0.0))
        constraint.free_length = max_dist * config.STUDS_PER_METER

    elif component_type in ("CharacterJoint", "ConfigurableJoint"):
        constraint.twist_limits_enabled = True
        twist_limit = properties.get("m_TwistLimitSpring", properties.get("m_LowTwistLimit", {}))
        if isinstance(twist_limit, dict):
            constraint.upper_twist_angle = float(twist_limit.get("limit", 45.0))

    return constraint


# ---------------------------------------------------------------------------
# Trail / LineRenderer conversion
# ---------------------------------------------------------------------------

def convert_trail_renderer(
    properties: dict[str, Any],
) -> RbxTrail | None:
    """Convert a Unity TrailRenderer to an RbxTrail.

    Maps:
        m_Time      -> Lifetime
        m_Colors    -> Color (first color)
        m_MinVertexDistance -> MinLength
        m_Width     -> WidthScale (from width curve)

    Args:
        properties: Raw component properties.

    Returns:
        An RbxTrail, or None if conversion fails.
    """
    trail = RbxTrail()
    trail.lifetime = float(properties.get("m_Time", 2.0))
    trail.min_length = float(properties.get("m_MinVertexDistance", 0.1))

    # Extract color from gradient
    color_grad = properties.get("m_Colors", properties.get("m_Parameters", {}))
    if isinstance(color_grad, dict):
        color0 = color_grad.get("color0", color_grad.get("m_Color0", {}))
        if isinstance(color0, dict):
            trail.color = (
                float(color0.get("r", 1.0)),
                float(color0.get("g", 1.0)),
                float(color0.get("b", 1.0)),
            )
            trail.transparency = 1.0 - float(color0.get("a", 1.0))

    # Width from curve (use first key value)
    width_curve = properties.get("m_Parameters", {})
    if isinstance(width_curve, dict):
        widths = width_curve.get("m_Widths", width_curve.get("widthCurve", {}))
        if isinstance(widths, dict):
            keys = widths.get("m_Curve", [])
            if isinstance(keys, list) and keys:
                first_key = keys[0] if isinstance(keys[0], dict) else {}
                trail.width_scale = float(first_key.get("value", 1.0))

    return trail


def convert_line_renderer(
    properties: dict[str, Any],
) -> RbxBeam | None:
    """Convert a Unity LineRenderer to an RbxBeam.

    Maps:
        m_Parameters.m_StartWidth  -> Width0
        m_Parameters.m_EndWidth    -> Width1
        m_Parameters.m_StartColor  -> Color
        m_Parameters.m_NumPositions -> Segments

    Args:
        properties: Raw component properties.

    Returns:
        An RbxBeam, or None if conversion fails.
    """
    beam = RbxBeam()

    params = properties.get("m_Parameters", {})
    if isinstance(params, dict):
        beam.width0 = float(params.get("m_StartWidth", params.get("startWidth", 1.0)))
        beam.width1 = float(params.get("m_EndWidth", params.get("endWidth", 1.0)))

        start_color = params.get("m_StartColor", {})
        if isinstance(start_color, dict):
            beam.color = (
                float(start_color.get("r", 1.0)),
                float(start_color.get("g", 1.0)),
                float(start_color.get("b", 1.0)),
            )
            beam.transparency = 1.0 - float(start_color.get("a", 1.0))

    num_positions = int(properties.get("m_Positions", {}).get("size", 10)
                        if isinstance(properties.get("m_Positions"), dict) else 10)
    beam.segments = max(1, num_positions)

    return beam


# ---------------------------------------------------------------------------
# Post-processing conversion
# ---------------------------------------------------------------------------

def convert_post_processing(
    components: list[Any],
) -> RbxPostProcessing | None:
    """Convert Unity post-processing components to RbxPostProcessing.

    Scans a list of components for known post-processing types and
    builds a combined RbxPostProcessing config.

    Supported Unity post-processing:
        Bloom/BloomOptimized -> BloomEffect
        ColorGrading/ColorAdjustments -> ColorCorrectionEffect
        DepthOfField -> DepthOfFieldEffect
        Volume (URP) -> extracts profile settings

    Args:
        components: List of component objects with component_type and properties.

    Returns:
        An RbxPostProcessing, or None if no post-processing found.
    """
    pp = RbxPostProcessing()
    found_any = False

    for comp in components:
        ct = comp.component_type if hasattr(comp, "component_type") else ""
        props = comp.properties if hasattr(comp, "properties") else {}

        if ct in ("Bloom", "BloomOptimized"):
            pp.bloom_enabled = True
            pp.bloom_intensity = float(props.get("m_Intensity", props.get("intensity", {}).get("value", 0.4))
                                       if not isinstance(props.get("m_Intensity"), dict)
                                       else float(props.get("m_Intensity", {}).get("value", 0.4)))
            pp.bloom_threshold = float(props.get("m_Threshold", props.get("threshold", {}).get("value", 0.95))
                                       if not isinstance(props.get("m_Threshold"), dict)
                                       else float(props.get("m_Threshold", {}).get("value", 0.95)))
            found_any = True

        elif ct in ("ColorGrading", "ColorAdjustments", "ColorLookup"):
            pp.color_correction_enabled = True
            pp.cc_brightness = float(props.get("m_Brightness", props.get("postExposure", {}).get("value", 0.0))
                                     if not isinstance(props.get("m_Brightness"), dict)
                                     else float(props.get("m_Brightness", {}).get("value", 0.0)))
            pp.cc_contrast = float(props.get("m_Contrast", props.get("contrast", {}).get("value", 0.0))
                                   if not isinstance(props.get("m_Contrast"), dict)
                                   else float(props.get("m_Contrast", {}).get("value", 0.0)))
            pp.cc_saturation = float(props.get("m_Saturation", props.get("saturation", {}).get("value", 0.0))
                                     if not isinstance(props.get("m_Saturation"), dict)
                                     else float(props.get("m_Saturation", {}).get("value", 0.0)))
            found_any = True

        elif ct == "DepthOfField":
            pp.dof_enabled = True
            pp.dof_focus_distance = float(props.get("m_FocusDistance", props.get("focusDistance", {}).get("value", 10.0))
                                          if not isinstance(props.get("m_FocusDistance"), dict)
                                          else float(props.get("m_FocusDistance", {}).get("value", 10.0)))
            found_any = True

        elif ct == "SunShafts":
            pp.sun_rays_enabled = True
            pp.sun_rays_intensity = float(props.get("sunShaftIntensity", 0.25))
            found_any = True

        elif ct == "Volume":
            # URP Volume — extract profile settings
            profile = props.get("m_Profile", {})
            if isinstance(profile, dict):
                settings = profile.get("m_Settings", [])
                if isinstance(settings, list):
                    for setting in settings:
                        if isinstance(setting, dict):
                            stype = setting.get("$type", "")
                            if "Bloom" in stype:
                                pp.bloom_enabled = True
                                pp.bloom_intensity = _extract_scalar(setting.get("intensity", {}), 0.4)
                                pp.bloom_threshold = _extract_scalar(setting.get("threshold", {}), 0.95)
                                found_any = True
                            elif "ColorAdjustments" in stype or "ColorGrading" in stype:
                                pp.color_correction_enabled = True
                                pp.cc_contrast = _extract_scalar(setting.get("contrast", {}), 0.0)
                                pp.cc_saturation = _extract_scalar(setting.get("saturation", {}), 0.0)
                                found_any = True
                            elif "DepthOfField" in stype:
                                pp.dof_enabled = True
                                pp.dof_focus_distance = _extract_scalar(setting.get("focusDistance", {}), 10.0)
                                found_any = True
                            elif "Vignette" in stype:
                                intensity = _extract_scalar(setting.get("intensity", {}), 0.0)
                                if intensity > 0:
                                    pp.attributes["VignetteIntensity"] = round(intensity, 3)
                                    found_any = True
                            elif "AmbientOcclusion" in stype or "ScreenSpaceAmbientOcclusion" in stype:
                                pp.attributes["AmbientOcclusionEnabled"] = True
                                ao_intensity = _extract_scalar(setting.get("intensity", {}), 1.0)
                                pp.attributes["AmbientOcclusionIntensity"] = round(ao_intensity, 3)
                                found_any = True
                            elif "MotionBlur" in stype:
                                pp.attributes["MotionBlurEnabled"] = True
                                pp.attributes["MotionBlurIntensity"] = _extract_scalar(setting.get("intensity", {}), 0.5)
                                found_any = True
                            elif "ChromaticAberration" in stype:
                                pp.attributes["ChromaticAberration"] = _extract_scalar(setting.get("intensity", {}), 0.0)
                                found_any = True

    # Enable atmosphere if any post-processing is active (better visual quality)
    if found_any:
        pp.atmosphere_enabled = True

    return pp if found_any else None


def _extract_scalar(data: Any, default: float = 0.0) -> float:
    """Extract a scalar value from a Unity MinMaxCurve-like structure."""
    if isinstance(data, (int, float)):
        return float(data)
    if isinstance(data, dict):
        return float(data.get("scalar", data.get("value", data.get("m_Scalar", default))))
    return default


def _extract_color_gradient(
    gradient: Any,
) -> list[tuple[float, float, float, float]] | None:
    """Extract a Unity Gradient to a list of (time, r, g, b) keypoints.

    Unity gradients store color keys and alpha keys separately.
    We merge them into a ColorSequence-compatible format.

    Returns:
        List of (time, r, g, b) tuples, or None if no gradient found.
    """
    if not isinstance(gradient, dict):
        return None

    # Try key-based gradient format (key0, key1, ... key7)
    keypoints: list[tuple[float, float, float, float]] = []
    for i in range(8):
        color_key = gradient.get(f"key{i}", {})
        if isinstance(color_key, dict):
            r = float(color_key.get("r", 1.0))
            g = float(color_key.get("g", 1.0))
            b = float(color_key.get("b", 1.0))
            t = float(color_key.get("t", i / 7.0 if i < 8 else 1.0))
            # Skip keys that look like unused padding
            if i > 1 and t == 0.0 and r == 0.0 and g == 0.0 and b == 0.0:
                continue
            keypoints.append((t, r, g, b))

    # Deduplicate by time
    if len(keypoints) > 2:
        seen_times: set[float] = set()
        unique: list[tuple[float, float, float, float]] = []
        for kp in keypoints:
            if kp[0] not in seen_times:
                seen_times.add(kp[0])
                unique.append(kp)
        keypoints = unique

    # Try m_ColorKeys / m_AlphaKeys list format
    if not keypoints:
        color_keys = gradient.get("m_ColorKeys", gradient.get("colorKeys", []))
        if isinstance(color_keys, list) and color_keys:
            for ck in color_keys:
                if isinstance(ck, dict):
                    r = float(ck.get("r", 1.0))
                    g = float(ck.get("g", 1.0))
                    b = float(ck.get("b", 1.0))
                    t = float(ck.get("time", 0.0))
                    keypoints.append((t, r, g, b))

    if len(keypoints) < 2:
        return None

    # Sort by time and ensure endpoints at 0 and 1
    keypoints.sort(key=lambda kp: kp[0])
    if keypoints[0][0] > 0.0:
        keypoints.insert(0, (0.0, keypoints[0][1], keypoints[0][2], keypoints[0][3]))
    if keypoints[-1][0] < 1.0:
        keypoints.append((1.0, keypoints[-1][1], keypoints[-1][2], keypoints[-1][3]))

    return keypoints


def _extract_curve_to_number_sequence(
    curve_data: Any,
) -> list[tuple[float, float, float]] | None:
    """Extract a Unity AnimationCurve / MinMaxCurve to (time, value, envelope) keypoints.

    Returns:
        List of (time, value, envelope) tuples, or None if no curve found.
    """
    if not isinstance(curve_data, dict):
        return None

    mode = curve_data.get("mode", 0)

    if mode == 0:  # Constant
        val = float(curve_data.get("scalar", curve_data.get("value", 1.0)))
        return [(0.0, val, 0.0), (1.0, val, 0.0)]

    if mode == 1:  # Curve
        curve = curve_data.get("maxCurve", curve_data.get("m_Curve", {}))
        if isinstance(curve, dict):
            keys = curve.get("m_Curve", [])
            if isinstance(keys, list) and keys:
                points: list[tuple[float, float, float]] = []
                scalar = float(curve_data.get("scalar", 1.0))
                for key in keys:
                    if isinstance(key, dict):
                        t = float(key.get("time", 0.0))
                        v = float(key.get("value", 1.0)) * scalar
                        points.append((t, v, 0.0))
                if len(points) >= 2:
                    return points

    if mode == 3:  # Random between two constants
        min_val = float(curve_data.get("minScalar", 0.0))
        max_val = float(curve_data.get("scalar", 1.0))
        avg = (min_val + max_val) / 2.0
        env = (max_val - min_val) / 2.0
        return [(0.0, avg, env), (1.0, avg, env)]

    return None


# ---------------------------------------------------------------------------
# Audio reverb conversion
# ---------------------------------------------------------------------------

# Unity AudioReverbPreset enum → approximate (DecayTime, Density, Diffusion)
# values.  Unity defines 28 presets; we map the most common ones and provide
# sensible defaults for the rest.  Values are rough perceptual matches to
# Roblox's ReverbSoundEffect parameters.
_REVERB_PRESETS: dict[int, tuple[float, float, float]] = {
    0:  (1.0,  1.0,  1.0),   # Generic
    1:  (0.4,  0.3,  0.3),   # PaddedCell
    2:  (1.5,  0.9,  0.9),   # Room
    3:  (1.2,  0.6,  0.6),   # Bathroom
    4:  (1.5,  0.7,  0.7),   # LivingRoom
    5:  (2.3,  0.8,  0.8),   # StoneRoom
    6:  (4.0,  0.5,  0.5),   # Auditorium
    7:  (3.9,  0.5,  0.5),   # ConcertHall
    8:  (2.9,  1.0,  0.6),   # Cave
    9:  (3.2,  0.6,  0.6),   # Arena
    10: (5.0,  0.5,  0.5),   # Hangar
    11: (0.3,  0.5,  1.0),   # CarpetedHallway
    12: (1.5,  0.6,  0.6),   # Hallway
    13: (2.5,  0.7,  0.7),   # StoneCorridor
    14: (1.5,  0.3,  0.3),   # Alley
    15: (1.5,  0.5,  0.5),   # Forest
    16: (1.0,  0.2,  0.2),   # City
    17: (1.5,  0.3,  0.3),   # Mountains
    18: (0.3,  0.2,  0.2),   # Quarry
    19: (1.5,  0.4,  0.4),   # Plain
    20: (1.8,  0.6,  0.6),   # ParkingLot
    21: (1.2,  0.5,  0.5),   # SewerPipe
    22: (8.0,  0.7,  0.7),   # Underwater
    23: (1.0,  0.5,  0.5),   # Drugged
    24: (1.0,  0.5,  0.5),   # Dizzy
    25: (1.0,  0.5,  0.5),   # Psychotic
    26: (1.0,  1.0,  1.0),   # User (custom, fallback to generic)
    27: (0.0,  0.0,  0.0),   # Off
}


def convert_reverb_zone(
    properties: dict[str, Any],
) -> RbxReverbSoundEffect | None:
    """Convert a Unity AudioReverbZone to an RbxReverbSoundEffect.

    Unity AudioReverbZone defines a spherical region that applies reverb to
    any AudioSource playing within it.  Roblox has no spatial reverb zone,
    so we attach a ReverbSoundEffect to the Part.  The zone distances are
    stored so downstream code could use them for scripted activation.

    Maps:
        m_MinDistance   -> min_distance (stored for reference)
        m_MaxDistance   -> max_distance (stored for reference)
        m_ReverbPreset -> DecayTime / Density / Diffusion approximation

    Args:
        properties: Raw component properties from the Unity AudioReverbZone.

    Returns:
        An RbxReverbSoundEffect, or None if the preset is Off.
    """
    import config

    preset = int(properties.get("m_ReverbPreset", 0))
    decay, density, diffusion = _REVERB_PRESETS.get(preset, (1.0, 1.0, 1.0))

    # Preset 27 is "Off" — skip.
    if preset == 27:
        return None

    min_dist = float(properties.get("m_MinDistance", 10.0))
    max_dist = float(properties.get("m_MaxDistance", 15.0))

    return RbxReverbSoundEffect(
        decay_time=decay,
        density=density,
        diffusion=diffusion,
        min_distance=min_dist * config.STUDS_PER_METER,
        max_distance=max_dist * config.STUDS_PER_METER,
    )


def convert_reverb_filter(
    properties: dict[str, Any],
) -> RbxReverbSoundEffect | None:
    """Convert a Unity AudioReverbFilter to an RbxReverbSoundEffect.

    AudioReverbFilter is attached directly to an AudioSource's GameObject
    and modifies that source's output.  This maps cleanly to a
    ReverbSoundEffect child of the Roblox Sound.

    Maps:
        m_ReverbPreset     -> DecayTime / Density / Diffusion approximation
        m_DecayTime        -> DecayTime (override if preset is User/custom)
        m_Density          -> Density   (override if preset is User/custom)
        m_Diffusion        -> Diffusion (override if preset is User/custom)
        m_DryLevel         -> DryLevel
        m_Room             -> WetLevel approximation

    Args:
        properties: Raw component properties from the Unity AudioReverbFilter.

    Returns:
        An RbxReverbSoundEffect, or None if the preset is Off.
    """
    preset = int(properties.get("m_ReverbPreset", 0))

    # Preset 27 is "Off" — skip.
    if preset == 27:
        return None

    decay, density, diffusion = _REVERB_PRESETS.get(preset, (1.0, 1.0, 1.0))

    # For User preset (26), use explicit property values if provided.
    if preset == 26:
        decay = float(properties.get("m_DecayTime", decay))
        density = float(properties.get("m_Density", density))
        diffusion = float(properties.get("m_Diffusion", diffusion))

    dry_level = float(properties.get("m_DryLevel", 0.0))
    # Unity m_Room is in millibels; convert to a rough dB approximation.
    room_mb = float(properties.get("m_Room", -1000.0))
    wet_level = room_mb / 1000.0  # Rough scale: -1000 mb → -1.0

    return RbxReverbSoundEffect(
        decay_time=decay,
        density=density,
        diffusion=diffusion,
        dry_level=dry_level,
        wet_level=wet_level,
    )


# ---------------------------------------------------------------------------
# Cinemachine conversion
# ---------------------------------------------------------------------------

# Cinemachine component type sets for easy membership checks.
CINEMACHINE_VIRTUAL_CAMERA_TYPES = {
    "CinemachineVirtualCamera",
    "Cinemachine.CinemachineVirtualCamera",
}

CINEMACHINE_FREELOOK_TYPES = {
    "CinemachineFreeLook",
    "Cinemachine.CinemachineFreeLook",
}

CINEMACHINE_BRAIN_TYPES = {
    "CinemachineBrain",
    "Cinemachine.CinemachineBrain",
}

CINEMACHINE_ALL_TYPES = (
    CINEMACHINE_VIRTUAL_CAMERA_TYPES
    | CINEMACHINE_FREELOOK_TYPES
    | CINEMACHINE_BRAIN_TYPES
)


def _resolve_target_name(
    target_ref: Any,
    file_id_to_name: dict[str, str] | None = None,
) -> str:
    """Resolve a Unity object reference (Follow/LookAt) to a target name.

    Args:
        target_ref: The raw property value -- typically a dict with fileID.
        file_id_to_name: Optional mapping from Unity fileID strings to
            GameObject names (provided by the caller).

    Returns:
        The resolved target name, or empty string if unresolvable.
    """
    if not isinstance(target_ref, dict):
        return ""
    fid = str(target_ref.get("fileID", "0"))
    if fid == "0":
        return ""
    if file_id_to_name and fid in file_id_to_name:
        return file_id_to_name[fid]
    # Return the raw fileID so downstream code can attempt resolution.
    return fid


def convert_cinemachine_virtual_camera(
    properties: dict[str, Any],
    file_id_to_name: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a CinemachineVirtualCamera to a dict of Part attributes.

    The returned dict is meant to be merged into ``part.attributes``.

    Args:
        properties: Raw component properties from the Unity component.
        file_id_to_name: Optional fileID->name map for Follow/LookAt resolution.

    Returns:
        Dict of attribute name -> value to store on the parent Part.
    """
    attrs: dict[str, Any] = {"CinemachineVCam": True}

    # Lens settings (nested under m_Lens).
    lens = properties.get("m_Lens", {})
    if isinstance(lens, dict):
        fov = float(lens.get("FieldOfView", lens.get("field of view", 60.0)))
        attrs["CinemachineFOV"] = max(1.0, min(fov, 120.0))
        attrs["CinemachineNear"] = float(lens.get("NearClipPlane", lens.get("near clip plane", 0.3)))
        attrs["CinemachineFar"] = float(lens.get("FarClipPlane", lens.get("far clip plane", 1000.0)))

    # Priority.
    priority = properties.get("m_Priority", properties.get("Priority", None))
    if priority is not None:
        attrs["CinemachinePriority"] = int(priority)

    # Follow target.
    follow_ref = properties.get("Follow", properties.get("m_Follow", {}))
    follow_name = _resolve_target_name(follow_ref, file_id_to_name)
    if follow_name:
        attrs["CinemachineFollow"] = follow_name

    # LookAt target.
    lookat_ref = properties.get("LookAt", properties.get("m_LookAt", {}))
    lookat_name = _resolve_target_name(lookat_ref, file_id_to_name)
    if lookat_name:
        attrs["CinemachineLookAt"] = lookat_name

    return attrs


def convert_cinemachine_freelook(
    properties: dict[str, Any],
    file_id_to_name: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Convert a CinemachineFreeLook to Part attributes.

    Includes the same fields as VirtualCamera plus axis settings.

    Args:
        properties: Raw component properties.
        file_id_to_name: Optional fileID->name map.

    Returns:
        Dict of attribute name -> value.
    """
    # Start with the same lens/priority/target extraction.
    attrs = convert_cinemachine_virtual_camera(properties, file_id_to_name)
    attrs["CinemachineFreeLook"] = True
    attrs.pop("CinemachineVCam", None)

    # X-axis (horizontal orbit).
    x_axis = properties.get("m_XAxis", {})
    if isinstance(x_axis, dict):
        attrs["CinemachineXAxisSpeed"] = float(x_axis.get("m_MaxSpeed", 300.0))

    # Y-axis (vertical orbit).
    y_axis = properties.get("m_YAxis", {})
    if isinstance(y_axis, dict):
        attrs["CinemachineYAxisSpeed"] = float(y_axis.get("m_MaxSpeed", 2.0))

    return attrs


def convert_cinemachine_brain(
    properties: dict[str, Any],
) -> dict[str, Any]:
    """Convert a CinemachineBrain to Part attributes.

    Args:
        properties: Raw component properties.

    Returns:
        Dict of attribute name -> value.
    """
    attrs: dict[str, Any] = {"CinemachineBrain": True}

    # Default blend time.
    blend = properties.get("m_DefaultBlend", {})
    if isinstance(blend, dict):
        attrs["CinemachineBlendTime"] = float(blend.get("m_Time", 2.0))

    return attrs


# ---------------------------------------------------------------------------
# VideoPlayer conversion
# ---------------------------------------------------------------------------

def convert_video_player(
    properties: dict[str, Any],
    guid_index: Any | None = None,
    uploaded_assets: dict[str, str] | None = None,
) -> RbxVideoFrame | None:
    """Convert a Unity VideoPlayer component to an RbxVideoFrame.

    Maps:
        m_Url / m_VideoClip  -> Video (rbxassetid:// URL)
        m_Looping            -> Looped
        m_PlayOnAwake        -> Playing (initial state)
        m_DirectAudioVolume  -> Volume

    Args:
        properties: Raw component properties from the Unity VideoPlayer.
        guid_index: GUID -> path resolver for looking up video clip paths.
        uploaded_assets: Dict mapping local asset paths to rbxassetid:// URLs.

    Returns:
        An RbxVideoFrame, or None if conversion fails.
    """
    looped = bool(int(properties.get("m_Looping", 0)))
    play_on_awake = bool(int(properties.get("m_PlayOnAwake", 1)))

    # Volume: Unity stores per-track volumes in m_DirectAudioVolume (often
    # a single-element list or a float).
    volume_data = properties.get("m_DirectAudioVolume", 0.5)
    if isinstance(volume_data, list) and volume_data:
        volume = float(volume_data[0])
    elif isinstance(volume_data, (int, float)):
        volume = float(volume_data)
    else:
        volume = 0.5
    volume = max(0.0, min(volume, 10.0))

    # Resolve video asset URL.
    video_url = ""

    # First try direct URL (VideoPlayer.url mode).
    url = properties.get("m_Url", "")
    if url and isinstance(url, str):
        video_url = url

    # Then try video clip asset reference (VideoPlayer.clip mode).
    if not video_url:
        video_clip = properties.get("m_VideoClip", {})
        if isinstance(video_clip, dict):
            guid = video_clip.get("guid", "")
            if guid and guid_index and uploaded_assets:
                video_path = guid_index.resolve(guid)
                if video_path:
                    relative = (guid_index.resolve_relative(guid)
                                if hasattr(guid_index, "resolve_relative") else None)
                    candidates = [str(video_path)]
                    if relative:
                        candidates.append(str(relative))
                    for key in list(candidates):
                        candidates.append(key.replace("\\", "/"))
                        candidates.append(key.replace("/", "\\"))
                    for key in candidates:
                        if key in uploaded_assets:
                            video_url = uploaded_assets[key]
                            break

    return RbxVideoFrame(
        video=video_url,
        looped=looped,
        playing=play_on_awake,
        volume=volume,
    )


# ---------------------------------------------------------------------------
# Skeletal animation (SkinnedMeshRenderer -> Motor6D chain)
# ---------------------------------------------------------------------------

def _extract_bone_file_ids(properties: dict[str, Any]) -> list[str]:
    """Extract bone Transform fileIDs from a SkinnedMeshRenderer's m_Bones array.

    Unity stores bones as a list of Transform references:
        m_Bones:
        - {fileID: 12345}
        - {fileID: 67890}

    Returns a list of fileID strings.
    """
    bones = properties.get("m_Bones", [])
    if not isinstance(bones, list):
        return []
    file_ids: list[str] = []
    for bone_ref in bones:
        if isinstance(bone_ref, dict):
            fid = str(bone_ref.get("fileID", "0"))
            if fid and fid != "0":
                file_ids.append(fid)
    return file_ids


def _extract_root_bone_file_id(properties: dict[str, Any]) -> str | None:
    """Extract the root bone Transform fileID from m_RootBone."""
    root_bone = properties.get("m_RootBone", {})
    if isinstance(root_bone, dict):
        fid = str(root_bone.get("fileID", "0"))
        if fid and fid != "0":
            return fid
    return None


def convert_skinned_mesh_renderer(
    properties: dict[str, Any],
    scene_nodes: dict[str, Any] | None = None,
) -> tuple[list[RbxMotor6D], dict[str, Any]]:
    """Convert a Unity SkinnedMeshRenderer into Motor6D joints and bone attributes.

    Extracts the bone hierarchy from m_Bones, resolves parent-child relationships
    using the scene node map, and creates Motor6D joints for each bone pair.

    Args:
        properties: Raw component properties from the Unity SkinnedMeshRenderer.
        scene_nodes: Dict mapping fileID -> SceneNode for resolving bone transforms.
            Each node should have: name, file_id, parent (with file_id),
            position (dict with x,y,z), rotation (dict with x,y,z,w).

    Returns:
        A tuple of (motor6d_list, bone_attributes):
        - motor6d_list: List of RbxMotor6D objects for the bone chain.
        - bone_attributes: Dict of attributes to store on the part for runtime use,
          including bone names, hierarchy, and the root bone name.
    """
    bone_file_ids = _extract_bone_file_ids(properties)
    root_bone_fid = _extract_root_bone_file_id(properties)

    if not bone_file_ids:
        return [], {}

    if scene_nodes is None:
        scene_nodes = {}

    # Build a set of bone fileIDs for quick lookup
    bone_fid_set = set(bone_file_ids)

    # Collect bone info: name, parent fileID, local transform
    bone_info: list[dict[str, Any]] = []
    bone_name_by_fid: dict[str, str] = {}

    for fid in bone_file_ids:
        node = scene_nodes.get(fid)
        if node is None:
            # Bone references a transform we can't resolve -- skip it
            continue

        name = getattr(node, "name", f"Bone_{fid}")
        bone_name_by_fid[fid] = name

        # Get parent fileID
        parent_fid = None
        parent = getattr(node, "parent", None)
        if parent is not None:
            pfid = getattr(parent, "file_id", None)
            if pfid:
                parent_fid = str(pfid)

        # Extract local position from the node's transform
        pos = {"x": 0.0, "y": 0.0, "z": 0.0}
        rot = {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}

        # SceneNode stores position/rotation as dicts or has them from transform
        node_pos = getattr(node, "position", None)
        if isinstance(node_pos, dict):
            pos = node_pos
        elif node_pos is not None and hasattr(node_pos, "__iter__"):
            # Could be a tuple/list (x, y, z)
            try:
                vals = list(node_pos)
                if len(vals) >= 3:
                    pos = {"x": float(vals[0]), "y": float(vals[1]), "z": float(vals[2])}
            except (TypeError, ValueError):
                pass

        node_rot = getattr(node, "rotation", None)
        if isinstance(node_rot, dict):
            rot = node_rot
        elif node_rot is not None and hasattr(node_rot, "__iter__"):
            try:
                vals = list(node_rot)
                if len(vals) >= 4:
                    rot = {"x": float(vals[0]), "y": float(vals[1]),
                           "z": float(vals[2]), "w": float(vals[3])}
            except (TypeError, ValueError):
                pass

        bone_info.append({
            "file_id": fid,
            "name": name,
            "parent_fid": parent_fid,
            "pos": pos,
            "rot": rot,
        })

    if not bone_info:
        return [], {}

    # Create Motor6D joints: one per bone that has a parent also in the bone set
    motor6ds: list[RbxMotor6D] = []

    for bone in bone_info:
        parent_fid = bone["parent_fid"]
        if parent_fid is None:
            continue

        # Parent must also be a bone (or the root bone's parent is the mesh root)
        parent_name = bone_name_by_fid.get(parent_fid)
        if parent_name is None:
            # Parent is not in the bone set -- this bone connects to the mesh root
            # Use a generic name; the runtime script can resolve it
            if parent_fid == root_bone_fid or bone["file_id"] == root_bone_fid:
                parent_name = "HumanoidRootPart"
            else:
                continue

        # Build C0: offset from parent bone center to joint (in parent's local space)
        # The bone's local position relative to its parent IS the joint offset
        import config
        px = float(bone["pos"].get("x", 0.0)) * config.STUDS_PER_METER
        py = float(bone["pos"].get("y", 0.0)) * config.STUDS_PER_METER
        # Negate Z for Unity->Roblox coordinate conversion
        pz = -float(bone["pos"].get("z", 0.0)) * config.STUDS_PER_METER

        c0 = RbxCFrame(x=px, y=py, z=pz)

        # C1 is identity (joint is at child bone's origin)
        c1 = RbxCFrame()

        motor6d = RbxMotor6D(
            name=bone["name"],
            part0_name=parent_name,
            part1_name=bone["name"],
            c0=c0,
            c1=c1,
        )
        motor6ds.append(motor6d)

    # Build bone attributes for runtime animation scripts
    bone_names = [b["name"] for b in bone_info]
    bone_hierarchy: dict[str, str] = {}  # child_name -> parent_name
    for bone in bone_info:
        parent_fid = bone["parent_fid"]
        if parent_fid and parent_fid in bone_name_by_fid:
            bone_hierarchy[bone["name"]] = bone_name_by_fid[parent_fid]

    root_bone_name = ""
    if root_bone_fid and root_bone_fid in bone_name_by_fid:
        root_bone_name = bone_name_by_fid[root_bone_fid]

    bone_attributes: dict[str, Any] = {
        "_HasSkeleton": True,
        "_BoneCount": len(bone_info),
        "_RootBone": root_bone_name,
        "_BoneNames": ",".join(bone_names),
    }

    # Store parent mapping as "child:parent,child:parent,..." for runtime
    if bone_hierarchy:
        pairs = [f"{child}:{parent}" for child, parent in bone_hierarchy.items()]
        bone_attributes["_BoneHierarchy"] = ",".join(pairs)

    log.info(
        "Converted SkinnedMeshRenderer: %d bones, %d Motor6D joints, root=%s",
        len(bone_info), len(motor6ds), root_bone_name,
    )

    return motor6ds, bone_attributes
