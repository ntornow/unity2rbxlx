"""
roblox_types.py -- Data models for Roblox output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ScriptType = Literal["Script", "LocalScript", "ModuleScript"]


@dataclass
class RbxCFrame:
    """A Roblox CFrame (position + rotation matrix)."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    # 3x3 rotation matrix (row-major)
    r00: float = 1.0; r01: float = 0.0; r02: float = 0.0
    r10: float = 0.0; r11: float = 1.0; r12: float = 0.0
    r20: float = 0.0; r21: float = 0.0; r22: float = 1.0


@dataclass
class RbxSurfaceAppearance:
    """PBR material for a Roblox MeshPart."""
    color_map: str | None = None       # rbxassetid:// URL or local path
    normal_map: str | None = None
    metalness_map: str | None = None
    roughness_map: str | None = None
    alpha_mode: str = "Overlay"        # Overlay, Transparency
    color: tuple[float, float, float] = (0.63, 0.63, 0.63)
    transparency: float = 0.0
    tiling: tuple[float, float] | None = None  # UV scale (sx, sy) from Unity material


@dataclass
class RbxLight:
    """A Roblox light instance."""
    light_type: str = "PointLight"     # PointLight, SpotLight, SurfaceLight
    brightness: float = 1.0
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    range: float = 60.0
    angle: float = 0.0                 # SpotLight only
    shadows: bool = False


@dataclass
class RbxSound:
    """A Roblox Sound instance."""
    name: str = "Sound"
    sound_id: str = ""
    volume: float = 0.5
    playback_speed: float = 1.0
    looped: bool = False
    playing: bool = False
    roll_off_max_distance: float = 10000.0
    roll_off_min_distance: float = 10.0


@dataclass
class RbxScript:
    """A Roblox script."""
    name: str
    source: str
    script_type: ScriptType = "Script"
    parent_path: str | None = None     # where to place in hierarchy


@dataclass
class RbxPart:
    """A Roblox Part/MeshPart with hierarchy."""
    name: str
    class_name: str = "Part"           # Part, MeshPart, Model
    cframe: RbxCFrame = field(default_factory=RbxCFrame)
    size: tuple[float, float, float] = (4.0, 1.0, 2.0)
    color: tuple[float, float, float] = (0.63, 0.63, 0.63)
    transparency: float = 0.0
    reflectance: float = 0.0
    anchored: bool = True
    can_collide: bool = True
    can_query: bool = True         # CanQuery for spatial queries (false for invisible logic parts)
    can_touch: bool = True         # CanTouch for Touched events
    cast_shadow: bool = True       # CastShadow for shadow rendering
    massless: bool = False         # Massless for physics (doesn't contribute to assembly mass)
    mesh_id: str | None = None         # rbxassetid:// URL
    texture_id: str | None = None      # rbxassetid:// URL for MeshPart TextureID
    initial_size: tuple[float, float, float] | None = None  # MeshPart native mesh size
    shape: int | None = None           # Part.Shape enum: 1=Ball, 2=Block, 3=Cylinder
    material: str = "Plastic"
    surface_appearance: RbxSurfaceAppearance | None = None
    lights: list[RbxLight] = field(default_factory=list)
    sounds: list[RbxSound] = field(default_factory=list)
    particle_emitters: list[RbxParticleEmitter] = field(default_factory=list)
    constraints: list[RbxConstraint] = field(default_factory=list)
    trails: list[RbxTrail] = field(default_factory=list)
    beams: list[RbxBeam] = field(default_factory=list)
    motor6ds: list[RbxMotor6D] = field(default_factory=list)
    reverb_effects: list[RbxReverbSoundEffect] = field(default_factory=list)
    video_frames: list[RbxVideoFrame] = field(default_factory=list)
    decals: list[RbxDecal] = field(default_factory=list)
    children: list[RbxPart] = field(default_factory=list)
    scripts: list[RbxScript] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    # CustomPhysicalProperties: (density, friction, elasticity, frictionWeight, elasticityWeight)
    custom_physical_properties: tuple[float, float, float, float, float] | None = None
    # CollisionFidelity: 0=Default, 1=Hull, 2=Box, 3=PreciseConvexDecomposition
    collision_fidelity: int | None = None

    # Source Unity node reference (for comparison)
    unity_file_id: str | None = None


@dataclass
class RbxUIElement:
    """A Roblox GUI element."""
    class_name: str = "Frame"          # Frame, TextLabel, TextButton, ImageLabel
    name: str = "Frame"
    position: tuple[float, float, float, float] = (0, 0, 0, 0)  # UDim2: Sx, Ox, Sy, Oy
    size: tuple[float, float, float, float] = (1, 0, 1, 0)
    background_color: tuple[float, float, float] = (1, 1, 1)
    background_transparency: float = 0.0
    text: str = ""
    text_color: tuple[float, float, float] = (0, 0, 0)
    text_size: int = 14
    image: str = ""                    # rbxassetid:// URL
    children: list[RbxUIElement] = field(default_factory=list)
    visible: bool = True
    # Layout child (UIListLayout or UIGridLayout)
    layout_type: str | None = None  # "UIListLayout" or "UIGridLayout"
    layout_direction: str = "Vertical"  # Vertical, Horizontal
    layout_padding: int = 0
    layout_cell_size: tuple[int, int] = (100, 100)  # For UIGridLayout
    layout_h_alignment: str = "Left"  # Left, Center, Right
    layout_v_alignment: str = "Top"  # Top, Center, Bottom
    attributes: dict[str, Any] = field(default_factory=dict)
    on_click_handlers: list[dict[str, str]] = field(default_factory=list)  # [{method: str, target_name: str}]


@dataclass
class RbxScreenGui:
    """A Roblox ScreenGui container."""
    name: str = "ScreenGui"
    elements: list[RbxUIElement] = field(default_factory=list)
    reset_on_spawn: bool = False
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class RbxParticleEmitter:
    """A Roblox ParticleEmitter instance."""
    rate: float = 20.0
    lifetime_min: float = 1.0
    lifetime_max: float = 2.0
    speed_min: float = 5.0
    speed_max: float = 10.0
    size_min: float = 0.5
    size_max: float = 2.0
    rotation_min: float = 0.0
    rotation_max: float = 360.0
    spread_angle: float = 45.0
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    transparency: float = 0.0
    texture: str = ""  # rbxassetid://
    light_emission: float = 0.0
    enabled: bool = True
    drag: float = 0.0
    locked_to_part: bool = False
    acceleration: tuple[float, float, float] = (0.0, 0.0, 0.0)
    velocity_inheritance: float = 0.0
    # ColorSequence keypoints: list of (time, r, g, b) tuples
    color_sequence: list[tuple[float, float, float, float]] | None = None
    # NumberSequence keypoints for size: list of (time, value, envelope) tuples
    size_sequence: list[tuple[float, float, float]] | None = None
    # NumberSequence keypoints for transparency: list of (time, value, envelope)
    transparency_sequence: list[tuple[float, float, float]] | None = None
    # Rotation speed (RotSpeed NumberRange)
    rot_speed_min: float = 0.0
    rot_speed_max: float = 0.0
    # Shape properties
    shape_style: str = "Cylinder"  # Cylinder, Sphere, Block, Disc
    shape_in_out: str = "Outward"  # Outward, Inward, InAndOut
    # Attributes for features without direct Roblox equivalents
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class RbxConstraint:
    """A Roblox physics constraint."""
    constraint_type: str = "WeldConstraint"  # WeldConstraint, HingeConstraint, BallSocketConstraint, SpringConstraint, RodConstraint
    # Part0/Part1 are resolved during rbxlx writing (Part0=owner, Part1=connected)
    connected_body_file_id: str = ""  # Unity m_ConnectedBody fileID for resolution
    # HingeConstraint properties
    limits_enabled: bool = False
    lower_angle: float = -45.0
    upper_angle: float = 45.0
    # SpringConstraint properties
    stiffness: float = 0.0
    damping: float = 0.0
    free_length: float = 0.0
    # BallSocketConstraint properties
    twist_limits_enabled: bool = False
    upper_twist_angle: float = 45.0


@dataclass
class RbxMotor6D:
    """A Roblox Motor6D joint connecting two parts (for skeletal animation).

    Motor6D is the standard joint type for character rigs in Roblox.
    Part0 is the parent bone part, Part1 is the child bone part.
    C0 and C1 define the CFrame offsets from each part's center to the joint.
    """
    name: str = "Motor6D"
    part0_name: str = ""  # Name of the parent bone part
    part1_name: str = ""  # Name of the child bone part
    c0: RbxCFrame = field(default_factory=RbxCFrame)  # Offset from Part0 to joint
    c1: RbxCFrame = field(default_factory=RbxCFrame)  # Offset from Part1 to joint


@dataclass
class RbxTrail:
    """A Roblox Trail instance."""
    lifetime: float = 2.0
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    transparency: float = 0.0
    min_length: float = 0.1
    width_scale: float = 1.0
    texture: str = ""
    light_emission: float = 0.0


@dataclass
class RbxBeam:
    """A Roblox Beam instance (for LineRenderer)."""
    color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    transparency: float = 0.0
    width0: float = 1.0
    width1: float = 1.0
    texture: str = ""
    light_emission: float = 0.0
    segments: int = 10


@dataclass
class RbxDecal:
    """A Roblox Decal instance (face texture on a Part)."""
    face: str = "Front"  # Top, Bottom, Front, Back, Left, Right
    texture: str = ""    # rbxassetid:// URL
    transparency: float = 0.0


@dataclass
class RbxVideoFrame:
    """A Roblox VideoFrame instance."""
    video: str = ""                    # rbxassetid:// URL
    looped: bool = False
    playing: bool = False
    volume: float = 0.5


@dataclass
class RbxReverbSoundEffect:
    """A Roblox ReverbSoundEffect instance (child of Sound or Part).

    Maps from Unity AudioReverbZone / AudioReverbFilter presets to
    Roblox ReverbSoundEffect properties.
    """
    decay_time: float = 1.0
    density: float = 1.0
    diffusion: float = 1.0
    dry_level: float = -6.0
    wet_level: float = 0.0
    # When sourced from AudioReverbZone, store zone distances
    min_distance: float = 10.0
    max_distance: float = 15.0


@dataclass
class RbxPostProcessing:
    """Post-processing effects for Roblox Lighting children."""
    # BloomEffect
    bloom_enabled: bool = False
    bloom_intensity: float = 0.4
    bloom_size: float = 24.0
    bloom_threshold: float = 0.95
    # ColorCorrectionEffect
    color_correction_enabled: bool = False
    cc_brightness: float = 0.0
    cc_contrast: float = 0.0
    cc_saturation: float = 0.0
    cc_tint_color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    # DepthOfFieldEffect
    dof_enabled: bool = False
    dof_far_intensity: float = 0.75
    dof_focus_distance: float = 0.05
    dof_in_focus_radius: float = 10.0
    dof_near_intensity: float = 0.75
    # SunRaysEffect
    sun_rays_enabled: bool = False
    sun_rays_intensity: float = 0.25
    sun_rays_spread: float = 1.0
    # Atmosphere
    atmosphere_enabled: bool = False
    atmosphere_density: float = 0.395
    atmosphere_offset: float = 0.0
    atmosphere_color: tuple[float, float, float] = (0.68, 0.75, 0.85)
    atmosphere_decay_color: tuple[float, float, float] = (0.93, 0.73, 0.47)
    atmosphere_glare: float = 0.0
    atmosphere_haze: float = 0.0
    # Extra attributes for effects without direct Roblox equivalent
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class RbxLightingConfig:
    """Global lighting configuration."""
    brightness: float = 2.0
    ambient: tuple[float, float, float] = (0.5, 0.5, 0.5)
    outdoor_ambient: tuple[float, float, float] = (0.5, 0.5, 0.5)
    clock_time: float = 14.0
    geographic_latitude: float = 41.7
    fog_color: tuple[float, float, float] = (0.75, 0.75, 0.75)
    fog_start: float = 0.0
    fog_end: float = 100000.0


@dataclass
class RbxCameraConfig:
    """Camera configuration."""
    cframe: RbxCFrame = field(default_factory=RbxCFrame)
    field_of_view: float = 70.0
    near_clip: float = 0.1
    far_clip: float = 10000.0


@dataclass
class RbxSkyboxConfig:
    """Skybox face textures."""
    front: str = ""
    back: str = ""
    left: str = ""
    right: str = ""
    up: str = ""
    down: str = ""


@dataclass
class RbxTerrain:
    """Terrain data extracted from a Unity Terrain component.

    Stores the position and dimensions of the terrain footprint so that
    a ground-plane Part (or set of Parts) can be generated at the correct
    location and scale.  Default size follows Unity's default terrain
    dimensions (1000 x 600 x 1000 in Unity units, which map to studs).

    When heightmap data is available, smooth_grid and physics_grid contain
    base64-encoded binary data for the Roblox Terrain item in rbxlx.
    """
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    size: tuple[float, float, float] = (1000.0, 600.0, 1000.0)
    terrain_data_guid: str = ""
    smooth_grid: str = ""  # base64-encoded SmoothGrid binary
    physics_grid: str = ""  # base64-encoded PhysicsGrid binary


@dataclass
class RbxWaterRegion:
    """A water fill region converted from a Unity water plane.

    Stores position and size in Roblox studs so that a Luau script can call
    Terrain:FillBlock(CFrame.new(x,y,z), size, Enum.Material.Water) at runtime.
    """
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    size: tuple[float, float, float] = (10.0, 2.0, 10.0)
    name: str = ""  # source Unity node name for debugging


@dataclass
class RbxPlace:
    """Complete Roblox place data."""
    workspace_parts: list[RbxPart] = field(default_factory=list)
    scripts: list[RbxScript] = field(default_factory=list)
    screen_guis: list[RbxScreenGui] = field(default_factory=list)
    lighting: RbxLightingConfig = field(default_factory=RbxLightingConfig)
    camera: RbxCameraConfig | None = None
    skybox: RbxSkyboxConfig | None = None
    server_storage_parts: list[RbxPart] = field(default_factory=list)
    terrains: list[RbxTerrain] = field(default_factory=list)
    water_regions: list[RbxWaterRegion] = field(default_factory=list)
    post_processing: RbxPostProcessing | None = None
    is_fps_game: bool = False
