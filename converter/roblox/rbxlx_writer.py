"""
RBXLX XML file writer for Unity-to-Roblox game conversion.

Generates .rbxlx (Roblox XML place) files from an intermediate RbxPlace
representation, producing a valid Roblox Studio-loadable document.
"""

from __future__ import annotations

import base64
import logging
import struct
import xml.etree.ElementTree as ET
import xml.dom.minidom
from pathlib import Path
from uuid import uuid4
from typing import Any

from core.roblox_types import (
    RbxBeam,
    RbxCFrame,
    RbxCameraConfig,
    RbxConstraint,
    RbxLight,
    RbxLightingConfig,
    RbxMotor6D,
    RbxPart,
    RbxParticleEmitter,
    RbxPlace,
    RbxPostProcessing,
    RbxReverbSoundEffect,
    RbxScript,
    RbxScreenGui,
    RbxSkyboxConfig,
    RbxSound,
    RbxSurfaceAppearance,
    RbxTrail,
    RbxUIElement,
    RbxVideoFrame,
)
from core.coordinate_system import quaternion_to_rotation_matrix

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ref_id() -> str:
    """Generate a unique referent ID with the standard RBX prefix."""
    return f"RBX{uuid4().hex[:32].upper()}"


# Maps Unity fileID → Roblox referent for constraint Part1 resolution.
# Populated during _make_part, consumed during _make_constraint.
_unity_fid_to_referent: dict[str, str] = {}


def _quat_to_rotation_matrix(
    qx: float, qy: float, qz: float, qw: float
) -> tuple[float, float, float, float, float, float, float, float, float]:
    """Convert a quaternion (x, y, z, w) to a 3x3 rotation matrix.

    Returns the nine elements (R00, R01, R02, R10, R11, R12, R20, R21, R22)
    suitable for embedding in an RBXLX ``<CoordinateFrame>`` element.
    """
    mat = quaternion_to_rotation_matrix(qx, qy, qz, qw)
    # quaternion_to_rotation_matrix returns a 3x3 list-of-lists or flat tuple.
    # Normalise to a flat 9-float tuple.
    if isinstance(mat, (list, tuple)) and isinstance(mat[0], (list, tuple)):
        return (
            float(mat[0][0]), float(mat[0][1]), float(mat[0][2]),
            float(mat[1][0]), float(mat[1][1]), float(mat[1][2]),
            float(mat[2][0]), float(mat[2][1]), float(mat[2][2]),
        )
    # Already flat
    return tuple(float(v) for v in mat[:9])  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# XML property helpers
# ---------------------------------------------------------------------------

def _add_string(parent: ET.Element, name: str, value: str) -> ET.Element:
    elem = ET.SubElement(parent, "string", name=name)
    elem.text = value
    return elem


def _add_bool(parent: ET.Element, name: str, value: bool) -> ET.Element:
    elem = ET.SubElement(parent, "bool", name=name)
    elem.text = "true" if value else "false"
    return elem


def _add_float(parent: ET.Element, name: str, value: float) -> ET.Element:
    elem = ET.SubElement(parent, "float", name=name)
    elem.text = str(value)
    return elem


def _add_int(parent: ET.Element, name: str, value: int) -> ET.Element:
    elem = ET.SubElement(parent, "int", name=name)
    elem.text = str(value)
    return elem


def _add_double(parent: ET.Element, name: str, value: float) -> ET.Element:
    elem = ET.SubElement(parent, "double", name=name)
    elem.text = str(value)
    return elem


def _add_token(parent: ET.Element, name: str, value: int) -> ET.Element:
    elem = ET.SubElement(parent, "token", name=name)
    elem.text = str(value)
    return elem


def _add_vector3(
    parent: ET.Element, name: str, x: float, y: float, z: float
) -> ET.Element:
    vec = ET.SubElement(parent, "Vector3", name=name)
    ET.SubElement(vec, "X").text = str(x)
    ET.SubElement(vec, "Y").text = str(y)
    ET.SubElement(vec, "Z").text = str(z)
    return vec


def _add_cframe(parent: ET.Element, name: str, cf: RbxCFrame) -> ET.Element:
    """Serialize a CFrame as a ``<CoordinateFrame>`` element."""
    coord = ET.SubElement(parent, "CoordinateFrame", name=name)
    ET.SubElement(coord, "X").text = str(cf.x)
    ET.SubElement(coord, "Y").text = str(cf.y)
    ET.SubElement(coord, "Z").text = str(cf.z)

    # RbxCFrame already stores the rotation matrix directly
    for label, val in [
        ("R00", cf.r00), ("R01", cf.r01), ("R02", cf.r02),
        ("R10", cf.r10), ("R11", cf.r11), ("R12", cf.r12),
        ("R20", cf.r20), ("R21", cf.r21), ("R22", cf.r22),
    ]:
        ET.SubElement(coord, label).text = str(val)
    return coord


def _add_color3uint8(parent: ET.Element, name: str, r: int, g: int, b: int) -> ET.Element:
    """Pack RGB into a single uint32 and emit ``<Color3uint8>``."""
    packed = (r & 0xFF) << 16 | (g & 0xFF) << 8 | (b & 0xFF)
    elem = ET.SubElement(parent, "Color3uint8", name=name)
    elem.text = str(packed)
    return elem


def _add_color3(parent: ET.Element, name: str, r: float, g: float, b: float) -> ET.Element:
    c = ET.SubElement(parent, "Color3", name=name)
    # Clamp to valid 0-1 range (Unity HDR colors can exceed 1.0)
    ET.SubElement(c, "R").text = str(max(0.0, min(1.0, r)))
    ET.SubElement(c, "G").text = str(max(0.0, min(1.0, g)))
    ET.SubElement(c, "B").text = str(max(0.0, min(1.0, b)))
    return c


def _add_protected_string(parent: ET.Element, name: str, source: str) -> ET.Element:
    elem = ET.SubElement(parent, "ProtectedString", name=name)
    elem.text = source  # Will be wrapped in CDATA during pretty-print
    return elem


def _add_content(parent: ET.Element, name: str, url: str) -> ET.Element:
    elem = ET.SubElement(parent, "Content", name=name)
    sub = ET.SubElement(elem, "url")
    sub.text = url
    return elem


def _add_binary_string(parent: ET.Element, name: str, b64_data: str) -> ET.Element:
    """Add a BinaryString property (base64-encoded, wrapped in CDATA by _pretty_xml)."""
    elem = ET.SubElement(parent, "BinaryString", name=name)
    elem.text = b64_data
    return elem


# ---------------------------------------------------------------------------
# Service / item construction
# ---------------------------------------------------------------------------

def _make_service(root: ET.Element, class_name: str, name: str | None = None) -> ET.Element:
    """Create a top-level service Item."""
    item = ET.SubElement(root, "Item", attrib={
        "class": class_name,
        "referent": _ref_id(),
    })
    props = ET.SubElement(item, "Properties")
    _add_string(props, "Name", name or class_name)
    return item


def _make_item(parent: ET.Element, class_name: str, name: str) -> tuple[ET.Element, ET.Element]:
    """Create an ``<Item>`` with a ``<Properties>`` child. Returns (item, props)."""
    item = ET.SubElement(parent, "Item", attrib={
        "class": class_name,
        "referent": _ref_id(),
    })
    props = ET.SubElement(item, "Properties")
    _add_string(props, "Name", name)
    return item, props


# ---------------------------------------------------------------------------
# Part / hierarchy serialization
# ---------------------------------------------------------------------------

def _make_surface_appearance(parent_xml: ET.Element, sa: RbxSurfaceAppearance) -> None:
    """Serialize a SurfaceAppearance as a child of *parent_xml*.

    Only creates the element if at least one texture map has an rbxassetid URL.
    Empty SurfaceAppearances are skipped to avoid bloat.
    """
    # Check if any texture has a valid rbxassetid URL
    has_any_texture = False
    for attr in ("color_map", "metalness_map", "normal_map", "roughness_map"):
        val = getattr(sa, attr, None)
        if val and "rbxassetid" in val:
            has_any_texture = True
            break

    if not has_any_texture:
        return  # Skip empty SurfaceAppearance

    item, props = _make_item(parent_xml, "SurfaceAppearance", "SurfaceAppearance")
    if hasattr(sa, "color_map") and sa.color_map and "rbxassetid" in sa.color_map:
        _add_content(props, "ColorMap", sa.color_map)
    if hasattr(sa, "metalness_map") and sa.metalness_map and "rbxassetid" in sa.metalness_map:
        _add_content(props, "MetalnessMap", sa.metalness_map)
    if hasattr(sa, "normal_map") and sa.normal_map and "rbxassetid" in sa.normal_map:
        _add_content(props, "NormalMap", sa.normal_map)
    if hasattr(sa, "roughness_map") and sa.roughness_map and "rbxassetid" in sa.roughness_map:
        _add_content(props, "RoughnessMap", sa.roughness_map)

    # AlphaMode: 0=Overlay (default), 1=Transparency
    alpha_mode = getattr(sa, "alpha_mode", "Overlay")
    if alpha_mode == "Transparency":
        _add_token(props, "AlphaMode", 1)


def _make_light(parent_xml: ET.Element, light: RbxLight) -> None:
    """Serialize a light (PointLight / SpotLight / SurfaceLight) as an Item."""
    light_class = getattr(light, "light_type", "PointLight")
    item, props = _make_item(parent_xml, light_class, light_class)
    if hasattr(light, "brightness"):
        _add_float(props, "Brightness", light.brightness)
    if hasattr(light, "color") and light.color:
        _add_color3(props, "Color", *light.color[:3])
    if hasattr(light, "range"):
        _add_float(props, "Range", light.range)
    if hasattr(light, "enabled"):
        _add_bool(props, "Enabled", light.enabled)
    if hasattr(light, "angle") and light_class == "SpotLight":
        _add_float(props, "Angle", light.angle)
    if hasattr(light, "shadows"):
        _add_bool(props, "Shadows", light.shadows)


def _make_particle_emitter(parent_xml: ET.Element, pe: RbxParticleEmitter) -> None:
    """Serialize a ParticleEmitter instance."""
    item, props = _make_item(parent_xml, "ParticleEmitter", "ParticleEmitter")
    _add_float(props, "Rate", pe.rate)
    # NumberRange for Lifetime
    nr = ET.SubElement(props, "NumberRange", name="Lifetime")
    nr.text = f"{pe.lifetime_min} {pe.lifetime_max}"
    # NumberRange for Speed
    nr_speed = ET.SubElement(props, "NumberRange", name="Speed")
    nr_speed.text = f"{pe.speed_min} {pe.speed_max}"
    # NumberSequence for Size
    ns = ET.SubElement(props, "NumberSequence", name="Size")
    if pe.size_sequence:
        # Use full NumberSequence keypoints from sizeOverLifetime
        ns.text = " ".join(
            f"{t} {v} {e}" for t, v, e in pe.size_sequence
        ) + " "
    else:
        ns.text = f"0 {pe.size_max} 0 1 {pe.size_min} 0 "
    # SpreadAngle
    vec2 = ET.SubElement(props, "Vector2", name="SpreadAngle")
    ET.SubElement(vec2, "X").text = str(pe.spread_angle)
    ET.SubElement(vec2, "Y").text = str(pe.spread_angle)
    # Color -- prefer ColorSequence if available, else single color
    if pe.color_sequence:
        cs = ET.SubElement(props, "ColorSequence", name="Color")
        cs.text = " ".join(
            f"{t} {r} {g} {b} 0" for t, r, g, b in pe.color_sequence
        ) + " "
    elif pe.color != (1.0, 1.0, 1.0):
        _add_color3(props, "Color", *pe.color[:3])
    # Transparency -- prefer sequence if available
    if pe.transparency_sequence:
        ts = ET.SubElement(props, "NumberSequence", name="Transparency")
        ts.text = " ".join(
            f"{t} {v} {e}" for t, v, e in pe.transparency_sequence
        ) + " "
    else:
        _add_float(props, "Transparency", pe.transparency)
    _add_float(props, "LightEmission", pe.light_emission)
    if pe.texture:
        _add_content(props, "Texture", pe.texture)
    _add_bool(props, "Enabled", pe.enabled)
    # Shape properties (Roblox enum: Block=0, Sphere=1, Disc=2, Cylinder=3)
    _SHAPE_ENUM = {"Block": 0, "Sphere": 1, "Disc": 2, "Cylinder": 3}
    shape_val = _SHAPE_ENUM.get(pe.shape_style, 3)
    if shape_val != 3:  # Only write if not default Cylinder
        _add_token(props, "Shape", shape_val)
    # ShapeInOut (Roblox enum: Outward=0, Inward=1, InAndOut=2)
    _SHAPE_IO_ENUM = {"Outward": 0, "Inward": 1, "InAndOut": 2}
    shape_io_val = _SHAPE_IO_ENUM.get(pe.shape_in_out, 0)
    if shape_io_val != 0:  # Only write if not default Outward
        _add_token(props, "ShapeInOut", shape_io_val)
    # Additional properties
    if pe.drag > 0:
        _add_float(props, "Drag", pe.drag)
    if pe.locked_to_part:
        _add_bool(props, "LockedToPart", True)
    if pe.velocity_inheritance > 0:
        _add_float(props, "VelocityInheritance", pe.velocity_inheritance)
    if pe.acceleration != (0.0, 0.0, 0.0):
        _add_vector3(props, "Acceleration", *pe.acceleration)
    # Rotation
    if pe.rotation_min != 0 or pe.rotation_max != 360:
        nr_rot = ET.SubElement(props, "NumberRange", name="Rotation")
        nr_rot.text = f"{pe.rotation_min} {pe.rotation_max}"
    # RotSpeed
    if pe.rot_speed_min != 0 or pe.rot_speed_max != 0:
        nr_rspd = ET.SubElement(props, "NumberRange", name="RotSpeed")
        nr_rspd.text = f"{pe.rot_speed_min} {pe.rot_speed_max}"
    # Attributes from Unity modules without direct Roblox equivalents
    pe_attrs = getattr(pe, "attributes", None) or {}
    if pe_attrs:
        encoded = _encode_attributes(pe_attrs)
        if encoded:
            elem = ET.SubElement(props, "BinaryString", name="AttributesSerialize")
            elem.text = encoded


def _make_sound(parent_xml: ET.Element, sound: RbxSound) -> None:
    """Serialize a Sound instance."""
    item, props = _make_item(parent_xml, "Sound", getattr(sound, "name", "Sound"))
    if hasattr(sound, "sound_id") and sound.sound_id:
        _add_content(props, "SoundId", sound.sound_id)
    if hasattr(sound, "volume"):
        _add_float(props, "Volume", sound.volume)
    if hasattr(sound, "looped"):
        _add_bool(props, "Looped", sound.looped)
    if hasattr(sound, "playing"):
        _add_bool(props, "Playing", sound.playing)
    if hasattr(sound, "playback_speed"):
        _add_float(props, "PlaybackSpeed", sound.playback_speed)
    if hasattr(sound, "roll_off_max_distance"):
        _add_float(props, "RollOffMaxDistance", sound.roll_off_max_distance)
    if hasattr(sound, "roll_off_min_distance"):
        _add_float(props, "RollOffMinDistance", sound.roll_off_min_distance)


def _make_video_frame(parent_xml: ET.Element, video: RbxVideoFrame) -> None:
    """Serialize a VideoFrame instance.

    VideoFrame is parented under a SurfaceGui (for in-world video on a Part)
    or a ScreenGui (for fullscreen video).  When attached to a Part, we wrap
    it in a SurfaceGui so it renders on the part's surface.
    """
    # Wrap in a SurfaceGui so the VideoFrame renders on the part surface.
    sg_item, sg_props = _make_item(parent_xml, "SurfaceGui", "VideoSurfaceGui")
    _add_string(sg_props, "Face", "Front")
    # Size the SurfaceGui to cover the surface.
    udim2 = ET.SubElement(sg_props, "UDim2", name="CanvasSize")
    ET.SubElement(udim2, "XS").text = "0"
    ET.SubElement(udim2, "XO").text = "800"
    ET.SubElement(udim2, "YS").text = "0"
    ET.SubElement(udim2, "YO").text = "600"

    vf_item, vf_props = _make_item(sg_item, "VideoFrame", "VideoFrame")
    if video.video:
        _add_content(vf_props, "Video", video.video)
    _add_bool(vf_props, "Looped", video.looped)
    _add_bool(vf_props, "Playing", video.playing)
    _add_float(vf_props, "Volume", video.volume)
    # Fill the entire SurfaceGui.
    size = ET.SubElement(vf_props, "UDim2", name="Size")
    ET.SubElement(size, "XS").text = "1"
    ET.SubElement(size, "XO").text = "0"
    ET.SubElement(size, "YS").text = "1"
    ET.SubElement(size, "YO").text = "0"


def _make_reverb_sound_effect(parent_xml: ET.Element, reverb: RbxReverbSoundEffect) -> None:
    """Serialize a ReverbSoundEffect instance."""
    item, props = _make_item(parent_xml, "ReverbSoundEffect", "ReverbSoundEffect")
    _add_float(props, "DecayTime", reverb.decay_time)
    _add_float(props, "Density", reverb.density)
    _add_float(props, "Diffusion", reverb.diffusion)
    _add_float(props, "DryLevel", reverb.dry_level)
    _add_float(props, "WetLevel", reverb.wet_level)


def _make_constraint(parent_xml: ET.Element, constraint: RbxConstraint,
                     parent_referent: str = "") -> None:
    """Serialize a physics constraint (WeldConstraint, HingeConstraint, etc.).

    Part0 is set to the parent part (via referent). Part1 is resolved from
    the connected_body_file_id using the global _unity_fid_to_referent mapping.
    """
    ctype = constraint.constraint_type
    item, props = _make_item(parent_xml, ctype, ctype)

    # Part0 reference (the owning part)
    if parent_referent:
        ref0 = ET.SubElement(props, "Ref", name="Part0")
        ref0.text = parent_referent

    # Part1 reference (the connected body, resolved from Unity fileID)
    connected_fid = getattr(constraint, "connected_body_file_id", "")
    if connected_fid:
        part1_ref = _unity_fid_to_referent.get(connected_fid, "")
        if part1_ref:
            ref1 = ET.SubElement(props, "Ref", name="Part1")
            ref1.text = part1_ref

    if ctype == "HingeConstraint":
        _add_bool(props, "LimitsEnabled", constraint.limits_enabled)
        if constraint.limits_enabled:
            _add_float(props, "LowerAngle", constraint.lower_angle)
            _add_float(props, "UpperAngle", constraint.upper_angle)

    elif ctype == "SpringConstraint":
        _add_float(props, "Stiffness", constraint.stiffness)
        _add_float(props, "Damping", constraint.damping)
        _add_float(props, "FreeLength", constraint.free_length)

    elif ctype == "BallSocketConstraint":
        _add_bool(props, "LimitsEnabled", constraint.twist_limits_enabled)
        if constraint.twist_limits_enabled:
            _add_float(props, "UpperAngle", constraint.upper_twist_angle)


def _make_bone_parts_and_motor6ds(
    parent_xml: ET.Element,
    motor6ds: list["RbxMotor6D"],
    parent_referent: str = "",
) -> None:
    """Create bone Parts and Motor6D joints for skeletal animation.

    Creates small transparent Parts for each bone as children of the mesh part,
    then creates Motor6D joints with proper Part0/Part1 Ref links. This is how
    Roblox character rigs work — each bone is a real Part with Motor6D connecting
    the hierarchy.

    Args:
        parent_xml: The parent MeshPart's XML element.
        motor6ds: List of Motor6D joints defining the bone chain.
        parent_referent: Referent ID of the parent MeshPart.
    """
    if not motor6ds:
        return

    # Create a bone Part for each unique bone name, track referent IDs
    bone_referents: dict[str, str] = {}
    # The mesh root part itself is available as parent_referent
    # Collect all unique bone names (both Part0 and Part1)
    bone_names: set[str] = set()
    for m in motor6ds:
        if m.part1_name:
            bone_names.add(m.part1_name)
        # Part0 names that aren't the root mesh need bone parts too
        if m.part0_name and m.part0_name != "HumanoidRootPart":
            bone_names.add(m.part0_name)

    # Create a tiny transparent Part for each bone
    for bone_name in sorted(bone_names):
        ref_id = _ref_id()
        bone_referents[bone_name] = ref_id

        bone_item = ET.SubElement(parent_xml, "Item")
        bone_item.set("class", "Part")
        bone_item.set("referent", ref_id)
        bone_props = ET.SubElement(bone_item, "Properties")
        _add_string(bone_props, "Name", bone_name)
        _add_float(bone_props, "Transparency", 1.0)
        _add_bool(bone_props, "CanCollide", False)
        _add_bool(bone_props, "CanQuery", False)
        _add_bool(bone_props, "Anchored", False)
        _add_bool(bone_props, "Massless", True)
        _add_vector3(bone_props, "Size", 0.2, 0.2, 0.2)
        _add_token(bone_props, "TopSurface", 0)
        _add_token(bone_props, "BottomSurface", 0)

    # "HumanoidRootPart" maps to the parent mesh part
    bone_referents["HumanoidRootPart"] = parent_referent

    # Create Motor6D joints
    for m in motor6ds:
        item, props = _make_item(parent_xml, "Motor6D", m.name)

        # Part0 reference
        p0_ref = bone_referents.get(m.part0_name, parent_referent)
        ref0 = ET.SubElement(props, "Ref", name="Part0")
        ref0.text = p0_ref

        # Part1 reference
        p1_ref = bone_referents.get(m.part1_name, "")
        if p1_ref:
            ref1 = ET.SubElement(props, "Ref", name="Part1")
            ref1.text = p1_ref

        # C0 and C1 transforms
        _add_cframe(props, "C0", m.c0)
        _add_cframe(props, "C1", m.c1)


def _make_trail(parent_xml: ET.Element, trail: RbxTrail) -> None:
    """Serialize a Trail instance with auto-generated Attachments."""
    # Trails need Attachment0 and Attachment1 on the parent part
    att0, att0_props = _make_item(parent_xml, "Attachment", "TrailAttachment0")
    _add_cframe(att0_props, "CFrame", RbxCFrame(x=0, y=0.5, z=0))
    att1, att1_props = _make_item(parent_xml, "Attachment", "TrailAttachment1")
    _add_cframe(att1_props, "CFrame", RbxCFrame(x=0, y=-0.5, z=0))

    item, props = _make_item(parent_xml, "Trail", "Trail")
    # Reference the attachments
    ref0 = ET.SubElement(props, "Ref", name="Attachment0")
    ref0.text = att0.get("referent", "")
    ref1 = ET.SubElement(props, "Ref", name="Attachment1")
    ref1.text = att1.get("referent", "")

    _add_float(props, "Lifetime", trail.lifetime)
    _add_float(props, "MinLength", trail.min_length)
    _add_float(props, "LightEmission", trail.light_emission)
    if trail.color != (1.0, 1.0, 1.0):
        _add_color3(props, "Color", *trail.color[:3])
    if trail.texture:
        _add_content(props, "Texture", trail.texture)


def _make_beam(parent_xml: ET.Element, beam: RbxBeam) -> None:
    """Serialize a Beam instance with auto-generated Attachments."""
    # Beams need Attachment0 and Attachment1
    att0, att0_props = _make_item(parent_xml, "Attachment", "BeamAttachment0")
    _add_cframe(att0_props, "CFrame", RbxCFrame(x=0, y=0, z=-1))
    att1, att1_props = _make_item(parent_xml, "Attachment", "BeamAttachment1")
    _add_cframe(att1_props, "CFrame", RbxCFrame(x=0, y=0, z=1))

    item, props = _make_item(parent_xml, "Beam", "Beam")
    ref0 = ET.SubElement(props, "Ref", name="Attachment0")
    ref0.text = att0.get("referent", "")
    ref1 = ET.SubElement(props, "Ref", name="Attachment1")
    ref1.text = att1.get("referent", "")

    _add_float(props, "Width0", beam.width0)
    _add_float(props, "Width1", beam.width1)
    _add_float(props, "LightEmission", beam.light_emission)
    _add_int(props, "Segments", beam.segments)
    if beam.color != (1.0, 1.0, 1.0):
        _add_color3(props, "Color", *beam.color[:3])
    if beam.texture:
        _add_content(props, "Texture", beam.texture)


def _make_ui_element(parent_xml: ET.Element, elem: RbxUIElement) -> None:
    """Recursively serialize a UI element and its children."""
    ui_class = getattr(elem, "class_name", "Frame")
    item, props = _make_item(parent_xml, ui_class, getattr(elem, "name", ui_class))

    # Common UI properties -- size/position are tuples: (scale_x, offset_x, scale_y, offset_y)
    if hasattr(elem, "size") and elem.size:
        s = elem.size
        if isinstance(s, dict):
            sx, ox, sy, oy = s.get("xs", 0), s.get("xo", 0), s.get("ys", 0), s.get("yo", 0)
        else:
            sx, ox, sy, oy = s[0], s[1], s[2], s[3]
        udim2 = ET.SubElement(props, "UDim2", name="Size")
        ET.SubElement(udim2, "XS").text = str(sx)
        ET.SubElement(udim2, "XO").text = str(int(ox))
        ET.SubElement(udim2, "YS").text = str(sy)
        ET.SubElement(udim2, "YO").text = str(int(oy))

    if hasattr(elem, "position") and elem.position:
        p = elem.position
        if isinstance(p, dict):
            sx, ox, sy, oy = p.get("xs", 0), p.get("xo", 0), p.get("ys", 0), p.get("yo", 0)
        else:
            sx, ox, sy, oy = p[0], p[1], p[2], p[3]
        udim2 = ET.SubElement(props, "UDim2", name="Position")
        ET.SubElement(udim2, "XS").text = str(sx)
        ET.SubElement(udim2, "XO").text = str(int(ox))
        ET.SubElement(udim2, "YS").text = str(sy)
        ET.SubElement(udim2, "YO").text = str(int(oy))

    if hasattr(elem, "background_color") and elem.background_color:
        _add_color3(props, "BackgroundColor3", *elem.background_color[:3])

    if hasattr(elem, "background_transparency"):
        _add_float(props, "BackgroundTransparency", elem.background_transparency)

    if hasattr(elem, "text") and elem.text is not None:
        _add_string(props, "Text", elem.text)

    if hasattr(elem, "text_color") and elem.text_color:
        _add_color3(props, "TextColor3", *elem.text_color[:3])

    if hasattr(elem, "text_size") and elem.text_size:
        _add_int(props, "TextSize", elem.text_size)

    if hasattr(elem, "image") and elem.image and "guid://" not in elem.image:
        _add_content(props, "Image", elem.image)

    if hasattr(elem, "visible"):
        _add_bool(props, "Visible", elem.visible)

    # Add layout child if present
    layout_type = getattr(elem, "layout_type", None)
    if layout_type:
        layout_item, layout_props = _make_item(item, layout_type, layout_type)
        if layout_type == "UIListLayout":
            # FillDirection: 0=Horizontal, 1=Vertical
            direction = getattr(elem, "layout_direction", "Vertical")
            _add_token(layout_props, "FillDirection", 1 if direction == "Vertical" else 0)
            _add_token(layout_props, "SortOrder", 2)  # LayoutOrder
        elif layout_type == "UIGridLayout":
            cell = getattr(elem, "layout_cell_size", (100, 100))
            udim2 = ET.SubElement(layout_props, "UDim2", name="CellSize")
            ET.SubElement(udim2, "XS").text = "0"
            ET.SubElement(udim2, "XO").text = str(cell[0])
            ET.SubElement(udim2, "YS").text = "0"
            ET.SubElement(udim2, "YO").text = str(cell[1])
            direction = getattr(elem, "layout_direction", "Horizontal")
            _add_token(layout_props, "StartCorner", 0)  # TopLeft
            _add_token(layout_props, "FillDirection", 0 if direction == "Horizontal" else 1)
            _add_token(layout_props, "SortOrder", 2)  # LayoutOrder
        padding = getattr(elem, "layout_padding", 0)
        if padding > 0:
            udim = ET.SubElement(layout_props, "UDim", name="Padding")
            ET.SubElement(udim, "S").text = "0"
            ET.SubElement(udim, "O").text = str(padding)
        h_align = getattr(elem, "layout_h_alignment", "Left")
        v_align = getattr(elem, "layout_v_alignment", "Top")
        h_map = {"Left": 0, "Center": 1, "Right": 2}
        v_map = {"Top": 0, "Center": 1, "Bottom": 2}
        _add_token(layout_props, "HorizontalAlignment", h_map.get(h_align, 0))
        _add_token(layout_props, "VerticalAlignment", v_map.get(v_align, 0))

    # Serialize attributes (e.g., _OnClick for button event handlers)
    elem_attrs = getattr(elem, "attributes", None) or {}
    if elem_attrs:
        encoded = _encode_attributes(elem_attrs)
        attr_elem = ET.SubElement(props, "BinaryString", name="AttributesSerialize")
        attr_elem.text = encoded

    # Recurse into children
    children = getattr(elem, "children", None) or []
    for child in children:
        _make_ui_element(item, child)


def _generate_ui_event_script(screen_guis: list) -> str | None:
    """Generate a LocalScript that wires up Button onClick events.

    Scans all ScreenGui elements for TextButtons with _OnClick attributes
    and generates Activated:Connect() handlers.
    """
    handlers = []

    def _scan_element(element, path: str):
        name = getattr(element, "name", "")
        cls = getattr(element, "class_name", "")
        current_path = f'{path}:FindFirstChild("{name}")'
        on_click = (getattr(element, "attributes", {}) or {}).get("_OnClick", "")
        if cls == "TextButton" and on_click:
            for method in on_click.split(","):
                method = method.strip()
                if method:
                    handlers.append((current_path, method, name))
        for child in getattr(element, "children", []) or []:
            _scan_element(child, current_path)

    for sg in screen_guis:
        sg_name = getattr(sg, "name", "ScreenGui")
        sg_path = f'script.Parent:FindFirstChild("{sg_name}")'
        for elem in getattr(sg, "elements", []) or []:
            _scan_element(elem, sg_path)

    if not handlers:
        return None

    lines = [
        "-- Auto-generated UI event wiring script",
        "-- Connects Unity Button onClick events to Roblox TextButton.Activated",
        "",
        "local gui = script.Parent",
        "",
        "task.wait(0.1) -- Wait for GUI to load",
        "",
    ]

    for path, method, button_name in handlers:
        lines.extend([
            f'-- Button: {button_name} → {method}()',
            f'local btn_{method} = {path}',
            f'if btn_{method} and btn_{method}:IsA("TextButton") then',
            f'    btn_{method}.Activated:Connect(function()',
            f'        -- TODO: Wire to {method}() function from converted scripts',
            f'        print("[UI] {button_name} clicked → {method}()")',
            f'        local handler = script.Parent:FindFirstChild("{method}", true)',
            f'        if handler and handler:IsA("ModuleScript") then',
            f'            local ok, mod = pcall(require, handler)',
            f'            if ok and type(mod) == "table" and mod.{method} then',
            f'                mod.{method}()',
            f'            end',
            f'        end',
            f'    end)',
            f'end',
            '',
        ])

    return "\n".join(lines)


def _generate_water_fill_script(water_regions: list) -> str:
    """Generate a Luau script that fills terrain water blocks for each water region.

    Each water region becomes a Terrain:FillBlock call with Enum.Material.Water
    at the specified position and size.
    """
    lines = [
        "-- Auto-generated water fill from Unity water planes",
        "-- Fills terrain water blocks at positions matching Unity water surfaces",
        "",
        "local terrain = workspace.Terrain",
        "",
    ]

    MAX_FILL = 2048.0  # Roblox FillBlock max per axis

    for i, region in enumerate(water_regions):
        pos = region.position
        size = region.size
        name = getattr(region, "name", "") or f"water_{i}"
        lines.append(f"-- Water region: {name}")

        # Cap size and split into chunks if needed
        sx = min(abs(size[0]), MAX_FILL * 20)  # reasonable cap
        sy = min(abs(size[1]), MAX_FILL)
        sz = min(abs(size[2]), MAX_FILL * 20)

        # Split into MAX_FILL-sized chunks
        import math
        nx = max(1, math.ceil(sx / MAX_FILL))
        nz = max(1, math.ceil(sz / MAX_FILL))
        chunk_sx = sx / nx
        chunk_sz = sz / nz

        start_x = pos[0] - sx / 2 + chunk_sx / 2
        start_z = pos[2] - sz / 2 + chunk_sz / 2

        for ix in range(nx):
            for iz in range(nz):
                cx = start_x + ix * chunk_sx
                cz = start_z + iz * chunk_sz
                lines.append(
                    f"terrain:FillBlock(CFrame.new({cx:.2f}, {pos[1]:.2f}, {cz:.2f}), "
                    f"Vector3.new({chunk_sx:.2f}, {sy:.2f}, {chunk_sz:.2f}), "
                    f"Enum.Material.Water)"
                )
        lines.append("")

    lines.append(f'print("Water fill complete: {len(water_regions)} region(s)")')
    return "\n".join(lines)


def _make_script(parent_xml: ET.Element, script: RbxScript) -> None:
    """Serialize a Script / LocalScript / ModuleScript."""
    script_class = getattr(script, "script_type", "Script")
    name = getattr(script, "name", script_class)
    item, props = _make_item(parent_xml, script_class, name)
    disabled = getattr(script, "disabled", False)
    _add_bool(props, "Disabled", disabled)
    # LinkedSource (empty = no external source)
    linked = ET.SubElement(props, "Content", name="LinkedSource")
    ET.SubElement(linked, "null")
    # RunContext for Script class (Legacy=0, Server=1, Client=2)
    # Use Server (1) explicitly — Legacy (0) scripts in ServerScriptService
    # are silently dropped when Studio loads rbxlx files from disk.
    if script_class == "Script":
        _add_token(props, "RunContext", 1)  # Server
    source = getattr(script, "source", "")
    _add_protected_string(props, "Source", source)


def _encode_attributes(attrs: dict[str, Any]) -> str:
    """Encode a dict of attributes into Roblox binary attribute format (base64).

    Format: u32 entry_count, then for each entry:
      u32 key_length + key_bytes + u8 type_id + value_bytes

    Type IDs: 0x02=String, 0x03=Bool, 0x04=Int32, 0x05=Float32, 0x06=Float64
    """
    buf = bytearray()
    # Entry count prefix (required by Roblox)
    buf.extend(struct.pack('<I', len(attrs)))
    for key, value in attrs.items():
        # Key: u32le length + UTF-8 bytes
        key_bytes = key.encode('utf-8')
        buf.extend(struct.pack('<I', len(key_bytes)))
        buf.extend(key_bytes)
        # Value
        if isinstance(value, bool):
            buf.append(0x03)  # Bool type
            buf.append(0x01 if value else 0x00)
        elif isinstance(value, str):
            buf.append(0x02)  # String type
            val_bytes = value.encode('utf-8')
            buf.extend(struct.pack('<I', len(val_bytes)))
            buf.extend(val_bytes)
        elif isinstance(value, int):
            buf.append(0x06)  # Float64 type (ints as double for Roblox compat)
            buf.extend(struct.pack('<d', float(value)))
        elif isinstance(value, float):
            buf.append(0x06)  # Float64 type
            buf.extend(struct.pack('<d', value))
    return base64.b64encode(bytes(buf)).decode('ascii')


def _make_part(parent_xml: ET.Element, part: RbxPart) -> None:
    """Recursively serialize a part and all of its children into *parent_xml*."""
    # Determine Item class
    part_class = getattr(part, "class_name", "Part")
    name = getattr(part, "name", "Part")

    # SpawnLocation is a special class
    if name == "SpawnLocation":
        part_class = "SpawnLocation"

    # SpawnLocation needs extra properties
    is_spawn = part_class == "SpawnLocation"

    # Models wrap children but have fewer direct properties
    if part_class == "Model":
        item, props = _make_item(parent_xml, "Model", name)
        if hasattr(part, "cframe") and part.cframe:
            _add_cframe(props, "WorldPivot", part.cframe)
    else:
        item, props = _make_item(parent_xml, part_class, name)

    # Use pre-registered referent for this part if available (for constraint linking)
    ufid = getattr(part, "unity_file_id", None)
    if ufid and str(ufid) in _unity_fid_to_referent:
        item.set("referent", _unity_fid_to_referent[str(ufid)])

    if part_class != "Model":
        # CFrame
        if hasattr(part, "cframe") and part.cframe:
            _add_cframe(props, "CFrame", part.cframe)

        # Size (capped at 2048 studs per axis for visual parts,
        # but allow larger for invisible colliders like GroundCollider)
        if hasattr(part, "size") and part.size:
            max_size = 2048.0 if (part.transparency or 0) < 1.0 else 16384.0
            sx = min(max_size, max(0.05, part.size[0]))
            sy = min(max_size, max(0.05, part.size[1]))
            sz = min(max_size, max(0.05, part.size[2]))
            _add_vector3(props, "Size", sx, sy, sz)

        # Color
        if hasattr(part, "color") and part.color:
            r, g, b = [int(c * 255) if isinstance(c, float) and c <= 1.0 else int(c) for c in part.color[:3]]
            _add_color3uint8(props, "Color3uint8", r, g, b)

        # Material (must be token ID, not string name)
        _MATERIAL_TOKENS = {
            "Plastic": 256, "SmoothPlastic": 272, "Neon": 288,
            "Wood": 512, "WoodPlanks": 528, "Marble": 784, "Basalt": 788,
            "Slate": 800, "CrackedLava": 804, "Concrete": 816,
            "Limestone": 820, "Pavement": 836, "Granite": 832,
            "Brick": 848, "Pebble": 864, "Cobblestone": 880,
            "Rock": 896, "Sandstone": 912, "CorrodedMetal": 1040,
            "DiamondPlate": 1056, "Foil": 1072, "Metal": 1088,
            "Grass": 1280, "LeafyGrass": 1284, "Sand": 1296,
            "Fabric": 1312, "Snow": 1328, "Mud": 1344,
            "Ground": 1360, "Asphalt": 1376, "Salt": 1392,
            "Ice": 1536, "Glacier": 1552, "Glass": 1568,
            "ForceField": 1584, "Air": 1792, "Water": 2048,
        }
        if hasattr(part, "material") and part.material is not None:
            if isinstance(part.material, int):
                _add_token(props, "Material", part.material)
            elif isinstance(part.material, str) and part.material in _MATERIAL_TOKENS:
                _add_token(props, "Material", _MATERIAL_TOKENS[part.material])
            else:
                _add_token(props, "Material", 256)  # Default to Plastic

        # Transparency
        if hasattr(part, "transparency") and part.transparency:
            _add_float(props, "Transparency", part.transparency)

        # Anchored
        anchored = getattr(part, "anchored", True)
        _add_bool(props, "Anchored", anchored)

        # CanCollide
        if hasattr(part, "can_collide"):
            _add_bool(props, "CanCollide", part.can_collide)

        # CanQuery (default True — only write if False to save space)
        can_query = getattr(part, "can_query", True)
        if not can_query:
            _add_bool(props, "CanQuery", False)

        # CanTouch (default True — only write if False)
        can_touch = getattr(part, "can_touch", True)
        if not can_touch:
            _add_bool(props, "CanTouch", False)

        # CastShadow (default True — only write if False)
        cast_shadow = getattr(part, "cast_shadow", True)
        if not cast_shadow:
            _add_bool(props, "CastShadow", False)

        # Massless (default False — only write if True)
        massless = getattr(part, "massless", False)
        if massless:
            _add_bool(props, "Massless", True)

        # CollectionService Tags from Unity m_TagString
        part_attrs = getattr(part, "attributes", {}) or {}
        unity_tag = part_attrs.get("Tag", "")
        if unity_tag and unity_tag != "Untagged":
            # Roblox Tags property is a BinaryString containing the tag name
            tags_elem = ET.SubElement(props, "BinaryString", name="Tags")
            tags_elem.text = unity_tag

        # CollisionGroup from Unity layer
        unity_layer = part_attrs.get("UnityLayer", 0)
        if unity_layer and int(unity_layer) != 0:
            _add_string(props, "CollisionGroup", f"UnityLayer{int(unity_layer)}")

        # CustomPhysicalProperties
        cpp = getattr(part, "custom_physical_properties", None)
        if cpp is not None:
            density, friction, elasticity, friction_w, elasticity_w = cpp
            cpp_elem = ET.SubElement(props, "CustomPhysicalProperties",
                                     name="CustomPhysicalProperties")
            ET.SubElement(cpp_elem, "CustomPhysics").text = "true"
            ET.SubElement(cpp_elem, "Density").text = f"{density:.4f}"
            ET.SubElement(cpp_elem, "Friction").text = f"{friction:.4f}"
            ET.SubElement(cpp_elem, "Elasticity").text = f"{elasticity:.4f}"
            ET.SubElement(cpp_elem, "FrictionWeight").text = f"{friction_w:.4f}"
            ET.SubElement(cpp_elem, "ElasticityWeight").text = f"{elasticity_w:.4f}"

        # Smooth surfaces (avoid default studs)
        _add_token(props, "TopSurface", 0)     # Smooth
        _add_token(props, "BottomSurface", 0)   # Smooth

        # Reflectance
        reflectance = getattr(part, "reflectance", 0.0)
        if reflectance and reflectance > 0:
            _add_float(props, "Reflectance", reflectance)

        # Shape (for basic Parts: 1=Ball, 2=Block, 3=Cylinder)
        if part_class == "Part" and hasattr(part, "shape") and part.shape is not None:
            _add_token(props, "Shape", part.shape)

        # CollisionFidelity for MeshParts with MeshCollider
        coll_fid = getattr(part, "collision_fidelity", None)
        if coll_fid is not None and part_class == "MeshPart":
            _add_token(props, "CollisionFidelity", coll_fid)

        # MeshId and InitialSize for MeshPart
        if part_class == "MeshPart" and hasattr(part, "mesh_id") and part.mesh_id:
            _add_content(props, "MeshId", part.mesh_id)
            init_size = getattr(part, "initial_size", None)
            if init_size:
                _add_vector3(props, "InitialSize", *init_size)

        # TextureID for MeshPart
        if part_class == "MeshPart" and hasattr(part, "texture_id") and part.texture_id:
            _add_content(props, "TextureID", part.texture_id)

    # SpawnLocation-specific properties
    if is_spawn:
        _add_int(props, "Duration", 0)  # No spawn protection
        _add_bool(props, "Neutral", True)  # All teams can spawn

    # --- Collect ALL attributes (part attrs + mesh/texture data) ---
    all_attrs: dict[str, Any] = dict(getattr(part, "attributes", None) or {})

    # Also store MeshId as attribute for MeshLoader fallback
    if part_class == "MeshPart" and hasattr(part, "mesh_id") and part.mesh_id:
        all_attrs["_MeshId"] = part.mesh_id

    # TextureID as attribute fallback
    if part_class == "MeshPart" and hasattr(part, "texture_id") and part.texture_id:
        all_attrs["_TextureId"] = part.texture_id

    # SurfaceAppearance: store texture URLs as attributes for runtime loading
    sa = getattr(part, "surface_appearance", None)
    if sa is not None and part_class == "MeshPart":
        color_map = getattr(sa, "color_map", None)
        normal_map = getattr(sa, "normal_map", None)
        metalness_map = getattr(sa, "metalness_map", None)
        roughness_map = getattr(sa, "roughness_map", None)
        if color_map and "rbxassetid" in color_map:
            all_attrs["_ColorMap"] = color_map
        if normal_map and "rbxassetid" in normal_map:
            all_attrs["_NormalMap"] = normal_map
        if metalness_map and "rbxassetid" in metalness_map:
            all_attrs["_MetalnessMap"] = metalness_map
        if roughness_map and "rbxassetid" in roughness_map:
            all_attrs["_RoughnessMap"] = roughness_map

    # Encode all attributes
    if all_attrs:
        encoded = _encode_attributes(all_attrs)
        elem = ET.SubElement(props, "BinaryString", name="AttributesSerialize")
        elem.text = encoded

    # --- Child attachments ---
    # SurfaceAppearance as XML child element (native rbxlx format)
    sa = getattr(part, "surface_appearance", None)
    if sa is not None and part_class == "MeshPart":
        _make_surface_appearance(item, sa)

    # For non-MeshPart Parts with SurfaceAppearance, add Texture children.
    # Roblox Parts don't support SurfaceAppearance but they do support
    # Texture objects on each face, providing tiled texture rendering.
    if sa is not None and part_class == "Part":
        color_map = getattr(sa, "color_map", None)
        if color_map and "rbxassetid" in color_map:
            # Compute StudsPerTile from Unity UV tiling scale.
            # UV scale N means the texture repeats N times across the mesh.
            # StudsPerTile = PartSizeAlongAxis / N
            tiling = getattr(sa, "tiling", None)
            part_sx = part.size[0] if hasattr(part, "size") and part.size else 4.0
            part_sz = part.size[2] if hasattr(part, "size") and part.size else 4.0
            if tiling and tiling[0] > 0 and tiling[1] > 0:
                studs_u = part_sx / tiling[0]
                studs_v = part_sz / tiling[1]
            else:
                studs_u = part_sx  # default: 1 tile across the whole face
                studs_v = part_sz
            # Apply textures to all 6 faces for full coverage.
            # NormalId enum: Right=0, Top=1, Back=2, Left=3, Bottom=4, Front=5
            for face_enum, face_label in ((1, "Top"), (4, "Bottom"), (5, "Front"),
                                          (2, "Back"), (0, "Right"), (3, "Left")):
                tex_item, tex_props = _make_item(item, "Texture", f"Texture_{face_label}")
                _add_content(tex_props, "Texture", color_map)
                _add_token(tex_props, "Face", face_enum)
                _add_float(tex_props, "StudsPerTileU", studs_u)
                _add_float(tex_props, "StudsPerTileV", studs_v)

    # Sprite texture → Decal (full image) or SurfaceGui>ImageLabel (atlas crop)
    sprite_tex = all_attrs.get("_SpriteTextureId", "")
    if sprite_tex and "rbxassetid" in sprite_tex:
        has_rect = all(
            k in all_attrs for k in ("_SpriteRectX", "_SpriteRectY", "_SpriteRectW", "_SpriteRectH")
        )
        if has_rect:
            # Atlas sprite: use SurfaceGui > ImageLabel with ImageRectOffset/Size
            rx = float(all_attrs["_SpriteRectX"])
            ry = float(all_attrs["_SpriteRectY"])
            rw = float(all_attrs["_SpriteRectW"])
            rh = float(all_attrs["_SpriteRectH"])
            for face_name in ("Front", "Back"):
                sg_item, sg_props = _make_item(item, "SurfaceGui", f"SpriteSurfaceGui{face_name}")
                _add_token(sg_props, "Face", 5 if face_name == "Front" else 2)
                # SizingMode = 2 (FixedSize) -- not needed, default works fine
                # Canvas size matches the sprite rect so the image fills the surface
                canvas = ET.SubElement(sg_props, "UDim2", name="CanvasSize")
                ET.SubElement(canvas, "XS").text = "0"
                ET.SubElement(canvas, "XO").text = str(int(rw))
                ET.SubElement(canvas, "YS").text = "0"
                ET.SubElement(canvas, "YO").text = str(int(rh))

                il_item, il_props = _make_item(sg_item, "ImageLabel", f"SpriteImage{face_name}")
                _add_content(il_props, "Image", sprite_tex)
                _add_float(il_props, "BackgroundTransparency", 1.0)
                # Fill the SurfaceGui
                size = ET.SubElement(il_props, "UDim2", name="Size")
                ET.SubElement(size, "XS").text = "1"
                ET.SubElement(size, "XO").text = "0"
                ET.SubElement(size, "YS").text = "1"
                ET.SubElement(size, "YO").text = "0"
                # ImageRectOffset and ImageRectSize as Vector2
                offset_v = ET.SubElement(il_props, "Vector2", name="ImageRectOffset")
                ET.SubElement(offset_v, "X").text = str(rx)
                ET.SubElement(offset_v, "Y").text = str(ry)
                rect_size_v = ET.SubElement(il_props, "Vector2", name="ImageRectSize")
                ET.SubElement(rect_size_v, "X").text = str(rw)
                ET.SubElement(rect_size_v, "Y").text = str(rh)
        else:
            # Full sprite (no atlas): use simple Decal
            for face_name, face_val in [("Front", 5), ("Back", 2)]:
                decal_item, decal_props = _make_item(item, "Decal", f"SpriteDecal{face_name}")
                _add_content(decal_props, "Texture", sprite_tex)
                _add_token(decal_props, "Face", face_val)

    for light in getattr(part, "lights", None) or []:
        _make_light(item, light)

    for sound in getattr(part, "sounds", None) or []:
        _make_sound(item, sound)

    for pe in getattr(part, "particle_emitters", None) or []:
        _make_particle_emitter(item, pe)

    for constraint in getattr(part, "constraints", None) or []:
        _make_constraint(item, constraint, parent_referent=item.get("referent", ""))

    motor6ds_list = getattr(part, "motor6ds", None) or []
    if motor6ds_list:
        _make_bone_parts_and_motor6ds(item, motor6ds_list, parent_referent=item.get("referent", ""))

    for trail in getattr(part, "trails", None) or []:
        _make_trail(item, trail)

    for beam in getattr(part, "beams", None) or []:
        _make_beam(item, beam)

    for reverb in getattr(part, "reverb_effects", None) or []:
        _make_reverb_sound_effect(item, reverb)

    for vf in getattr(part, "video_frames", None) or []:
        _make_video_frame(item, vf)

    for script in getattr(part, "scripts", None) or []:
        _make_script(item, script)

    # Recursive children
    for child in getattr(part, "children", None) or []:
        _make_part(item, child)


# ---------------------------------------------------------------------------
# Lighting, Camera, Skybox
# ---------------------------------------------------------------------------

def _make_lighting(lighting_item: ET.Element, config: RbxLightingConfig) -> None:
    """Populate the Lighting service with properties from *config*."""
    props_list = lighting_item.findall("Properties")
    if props_list:
        props = props_list[0]
    else:
        props = ET.SubElement(lighting_item, "Properties")

    if hasattr(config, "brightness") and config.brightness is not None:
        _add_float(props, "Brightness", config.brightness)
    if hasattr(config, "ambient") and config.ambient:
        _add_color3(props, "Ambient", *config.ambient[:3])
    if hasattr(config, "outdoor_ambient") and config.outdoor_ambient:
        _add_color3(props, "OutdoorAmbient", *config.outdoor_ambient[:3])
    if hasattr(config, "clock_time") and config.clock_time is not None:
        _add_float(props, "ClockTime", config.clock_time)
    if hasattr(config, "geographic_latitude") and config.geographic_latitude is not None:
        _add_float(props, "GeographicLatitude", config.geographic_latitude)
    if hasattr(config, "fog_color") and config.fog_color:
        _add_color3(props, "FogColor", *config.fog_color[:3])
    if hasattr(config, "fog_start") and config.fog_start is not None:
        _add_float(props, "FogStart", config.fog_start)
    if hasattr(config, "fog_end") and config.fog_end is not None:
        _add_float(props, "FogEnd", config.fog_end)

    # Use Future lighting technology for best visual fidelity
    # Technology enum: 0=Compatibility, 1=Voxel, 2=ShadowMap, 3=Future
    _add_token(props, "Technology", 3)
    # EnvironmentDiffuseScale/EnvironmentSpecularScale for PBR
    _add_float(props, "EnvironmentDiffuseScale", 1.0)
    _add_float(props, "EnvironmentSpecularScale", 1.0)

    # Add Atmosphere for outdoor scenes (reduces default Roblox haze)
    atmo_item, atmo_props = _make_item(lighting_item, "Atmosphere", "Atmosphere")
    atmo_cfg = getattr(config, "atmosphere_density", None)
    density = atmo_cfg if atmo_cfg is not None else 0.3
    _add_float(atmo_props, "Density", density)
    _add_float(atmo_props, "Offset", getattr(config, "atmosphere_offset", 0.25))
    _add_color3(atmo_props, "Color",
                *getattr(config, "atmosphere_color", (0.68, 0.75, 0.85))[:3])
    _add_color3(atmo_props, "Decay",
                *getattr(config, "atmosphere_decay_color", (0.93, 0.73, 0.47))[:3])
    _add_float(atmo_props, "Glare", getattr(config, "atmosphere_glare", 0.0))
    _add_float(atmo_props, "Haze", getattr(config, "atmosphere_haze", 0.0))


def _make_skybox(parent_xml: ET.Element, skybox: RbxSkyboxConfig) -> None:
    """Add a Sky object under Lighting."""
    item, props = _make_item(parent_xml, "Sky", "Sky")
    # Map Roblox Sky property names to RbxSkyboxConfig field names.
    _face_to_field = {
        "SkyboxBk": "back",
        "SkyboxDn": "down",
        "SkyboxFt": "front",
        "SkyboxLf": "left",
        "SkyboxRt": "right",
        "SkyboxUp": "up",
    }
    for face, field_name in _face_to_field.items():
        url = getattr(skybox, field_name, "") or ""
        if url:
            _add_content(props, face, url)
    if hasattr(skybox, "celestial_bodies_shown"):
        _add_bool(props, "CelestialBodiesShown", skybox.celestial_bodies_shown)
    if hasattr(skybox, "star_count"):
        _add_int(props, "StarCount", skybox.star_count)


def _make_post_processing(lighting_item: ET.Element, pp: RbxPostProcessing) -> None:
    """Add post-processing effect children under Lighting."""
    if pp.bloom_enabled:
        item, props = _make_item(lighting_item, "BloomEffect", "BloomEffect")
        _add_float(props, "Intensity", pp.bloom_intensity)
        _add_float(props, "Size", pp.bloom_size)
        _add_float(props, "Threshold", pp.bloom_threshold)

    if pp.color_correction_enabled:
        item, props = _make_item(lighting_item, "ColorCorrectionEffect", "ColorCorrectionEffect")
        _add_float(props, "Brightness", pp.cc_brightness)
        _add_float(props, "Contrast", pp.cc_contrast)
        _add_float(props, "Saturation", pp.cc_saturation)
        if pp.cc_tint_color != (1.0, 1.0, 1.0):
            _add_color3(props, "TintColor", *pp.cc_tint_color[:3])

    if pp.dof_enabled:
        item, props = _make_item(lighting_item, "DepthOfFieldEffect", "DepthOfFieldEffect")
        _add_float(props, "FarIntensity", pp.dof_far_intensity)
        _add_float(props, "FocusDistance", pp.dof_focus_distance)
        _add_float(props, "InFocusRadius", pp.dof_in_focus_radius)
        _add_float(props, "NearIntensity", pp.dof_near_intensity)

    if pp.sun_rays_enabled:
        item, props = _make_item(lighting_item, "SunRaysEffect", "SunRaysEffect")
        _add_float(props, "Intensity", pp.sun_rays_intensity)
        _add_float(props, "Spread", pp.sun_rays_spread)

    if pp.atmosphere_enabled:
        item, props = _make_item(lighting_item, "Atmosphere", "Atmosphere")
        _add_float(props, "Density", pp.atmosphere_density)
        _add_float(props, "Offset", pp.atmosphere_offset)
        _add_color3(props, "Color", *pp.atmosphere_color[:3])
        _add_color3(props, "Decay", *pp.atmosphere_decay_color[:3])
        _add_float(props, "Glare", pp.atmosphere_glare)
        _add_float(props, "Haze", pp.atmosphere_haze)

    # Extra PP attributes (no direct Roblox equivalent — store for scripts/plugins)
    pp_attrs = getattr(pp, "attributes", {})
    if pp_attrs:
        lighting_props = lighting_item.find("Properties")
        if lighting_props is not None:
            _write_attributes(lighting_props, pp_attrs)


def _make_camera(workspace_item: ET.Element, camera: RbxCameraConfig) -> None:
    """Add a Camera item under Workspace."""
    item, props = _make_item(workspace_item, "Camera", "Camera")
    if hasattr(camera, "cframe") and camera.cframe:
        _add_cframe(props, "CFrame", camera.cframe)
    if hasattr(camera, "field_of_view") and camera.field_of_view is not None:
        _add_float(props, "FieldOfView", camera.field_of_view)
    if hasattr(camera, "camera_type") and camera.camera_type is not None:
        _add_token(props, "CameraType", camera.camera_type)


# ---------------------------------------------------------------------------
# Pretty-printing with CDATA support
# ---------------------------------------------------------------------------

def _pretty_xml(root: ET.Element) -> str:
    """Convert an ElementTree root to a pretty-printed XML string.

    ProtectedString elements have their text wrapped in ``<![CDATA[...]]>``
    sections so that Luau source code is preserved verbatim.
    """
    rough = ET.tostring(root, encoding="unicode", xml_declaration=False)
    dom = xml.dom.minidom.parseString(f'<?xml version="1.0" encoding="utf-8"?>\n{rough}')
    pretty = dom.toprettyxml(indent="  ", encoding=None)

    # minidom adds its own xml declaration; strip the duplicate if present
    lines = pretty.split("\n")
    if lines and lines[0].startswith("<?xml"):
        lines[0] = '<?xml version="1.0" encoding="utf-8"?>'

    result = "\n".join(lines)

    # Wrap ProtectedString and BinaryString content in CDATA, undoing XML escaping
    # that minidom applied to the script source text.
    # Uses a scan-based approach instead of regex to avoid issues with non-greedy
    # matching across many elements with multiline content.
    import html

    result = _wrap_elements_in_cdata(result, "ProtectedString")
    result = _wrap_elements_in_cdata(result, "BinaryString")

    return result


def _wrap_elements_in_cdata(xml_str: str, tag_name: str) -> str:
    """Wrap all <tagName>content</tagName> elements in CDATA sections.

    Uses a forward scan to find matched open/close tags robustly,
    avoiding regex backtracking issues with large multiline content.
    """
    import html
    open_tag = f"<{tag_name} "
    close_tag = f"</{tag_name}>"
    parts: list[str] = []
    pos = 0

    while pos < len(xml_str):
        # Find next opening tag
        open_idx = xml_str.find(open_tag, pos)
        if open_idx < 0:
            parts.append(xml_str[pos:])
            break

        # Find the end of the opening tag (the >)
        tag_end = xml_str.find(">", open_idx)
        if tag_end < 0:
            parts.append(xml_str[pos:])
            break

        # Check for self-closing tag
        if xml_str[tag_end - 1] == "/":
            # Self-closing: <TagName .../> — keep as-is
            parts.append(xml_str[pos:tag_end + 1])
            pos = tag_end + 1
            continue

        # Find matching close tag
        close_idx = xml_str.find(close_tag, tag_end + 1)
        if close_idx < 0:
            # No close tag found — keep as-is (shouldn't happen in valid XML)
            parts.append(xml_str[pos:tag_end + 1])
            pos = tag_end + 1
            continue

        # Extract pieces
        before = xml_str[pos:tag_end + 1]  # Everything up to and including >
        content = xml_str[tag_end + 1:close_idx]
        # Undo XML entity escaping inside CDATA (minidom escaped our source)
        content = html.unescape(content)
        # Escape any ]]> in content to prevent breaking CDATA
        content = content.replace("]]>", "]]]]><![CDATA[>")
        parts.append(f"{before}<![CDATA[{content}]]>{close_tag}")
        pos = close_idx + len(close_tag)

    return "".join(parts)


def _count_parts(part: RbxPart) -> int:
    """Recursively count a part and all its children."""
    count = 1
    for child in getattr(part, "children", []) or []:
        count += _count_parts(child)
    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_rbxlx(place: RbxPlace, output_path: Path) -> dict[str, Any]:
    """Write an RbxPlace to a ``.rbxlx`` file at *output_path*.

    Returns a statistics dictionary with counts of serialized elements:
    ``{"parts", "scripts", "lights", "sounds", "ui_elements"}``.
    """
    # Reset and pre-populate unity fileID → referent mapping
    # Pre-pass assigns referents so constraints can resolve Part1 before
    # the target part is serialized (avoids ordering dependency).
    _unity_fid_to_referent.clear()
    def _pre_register(parts: list) -> None:
        for p in parts:
            ufid = getattr(p, "unity_file_id", None)
            if ufid:
                _unity_fid_to_referent[str(ufid)] = _ref_id()
            _pre_register(getattr(p, "children", None) or [])
    _pre_register(getattr(place, "workspace_parts", None) or [])

    stats: dict[str, int] = {
        "parts_written": 0,
        "scripts_written": 0,
        "lights": 0,
        "sounds": 0,
        "ui_elements": 0,
    }

    # Root element
    root = ET.Element("roblox", attrib={
        "xmlns:xmime": "http://www.w3.org/2005/05/xmlmime",
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "xsi:noNamespaceSchemaLocation": "http://www.roblox.com/roblox.xsd",
        "version": "4",
    })

    # ---- Services --------------------------------------------------------
    workspace = _make_service(root, "Workspace")
    ws_props = workspace.find("Properties")
    if ws_props is not None:
        # Enable streaming for large scenes (>5000 parts) for better performance
        def _count_workspace_parts(parts: list) -> int:
            total = 0
            for p in parts:
                total += 1
                total += _count_workspace_parts(getattr(p, "children", None) or [])
            return total
        part_count = _count_workspace_parts(getattr(place, "workspace_parts", None) or [])
        streaming = part_count > 5000
        _add_bool(ws_props, "StreamingEnabled", streaming)
        if streaming:
            # StreamingIntegrityMode: 1=PauseOutsideLoadedArea (safest)
            _add_token(ws_props, "StreamingIntegrityMode", 1)
        _add_float(ws_props, "Gravity", 196.2)  # Standard Roblox gravity (9.81 m/s²)
        _add_float(ws_props, "FallenPartsDestroyHeight", -500.0)  # Clean up fallen parts

    # ---- Terrain -----------------------------------------------------------
    terrains: list = getattr(place, "terrains", None) or []
    water_regions: list = getattr(place, "water_regions", None) or []
    if terrains or water_regions:
        terrain_item, terrain_props = _make_item(workspace, "Terrain", "Terrain")
        # Terrain properties required for Studio to load voxel data
        ET.SubElement(terrain_props, "bool", name="Decoration").text = "false"
        # MaterialColors: default color palette (from Studio reference)
        mc_el = ET.SubElement(terrain_props, "BinaryString", name="MaterialColors")
        mc_el.text = "AAAAAAAAan8/P39rf2Y/ilY+j35fi21PZmxvZbDqw8faiVpHOi4kHh4lZlw76JxKc3trhHtagcLgc4RKxr21zq2UlJSM"
        ET.SubElement(terrain_props, "bool", name="SmoothVoxelsUpgraded").text = "false"
        # Water properties
        wc = ET.SubElement(terrain_props, "Color3", name="WaterColor")
        ET.SubElement(wc, "R").text = "0.05"
        ET.SubElement(wc, "G").text = "0.33"
        ET.SubElement(wc, "B").text = "0.36"
        ET.SubElement(terrain_props, "float", name="WaterReflectance").text = "1"
        ET.SubElement(terrain_props, "float", name="WaterTransparency").text = "0.3"
        ET.SubElement(terrain_props, "float", name="WaterWaveSize").text = "0.15"
        ET.SubElement(terrain_props, "float", name="WaterWaveSpeed").text = "10"
        # Embed SmoothGrid and PhysicsGrid binary data
        for t in terrains:
            sg = getattr(t, "smooth_grid", None)
            pg = getattr(t, "physics_grid", None)
            if sg:
                sg_el = ET.SubElement(terrain_props, "BinaryString", name="SmoothGrid")
                sg_el.text = sg
                log.info("Embedded SmoothGrid terrain data (%d chars base64)", len(sg))
            # Always write PhysicsGrid — use provided or default
            pg_el = ET.SubElement(terrain_props, "BinaryString", name="PhysicsGrid")
            from roblox.terrain_encoder import encode_physics_grid
            pg_el.text = pg if pg else encode_physics_grid()

    lighting = _make_service(root, "Lighting")
    server_script_service = _make_service(root, "ServerScriptService")
    starter_player = _make_service(root, "StarterPlayer")
    sp_props = starter_player.find("Properties")
    if sp_props is not None:
        # Apply camera settings from Unity scene
        cam = getattr(place, "camera", None)
        if cam:
            fov = getattr(cam, "field_of_view", 70)
            if fov and fov != 70:
                _add_float(sp_props, "CameraMaxZoomDistance", max(128, fov * 2))
            # Near/far clip from Unity camera
            near_clip = getattr(cam, "near_clip", 0.3)
            if near_clip and near_clip > 0.3:
                _add_float(sp_props, "CameraMinZoomDistance", near_clip)
        # Lock first-person camera for FPS games
        if getattr(place, "is_fps_game", False):
            _add_token(sp_props, "CameraMode", 1)  # 1 = LockFirstPerson
            _add_float(sp_props, "CameraMinZoomDistance", 0.5)
            _add_float(sp_props, "CameraMaxZoomDistance", 0.5)
    starter_player_scripts_item, _ = _make_item(starter_player, "StarterPlayerScripts", "StarterPlayerScripts")
    starter_char_scripts_item, _ = _make_item(starter_player, "StarterCharacterScripts", "StarterCharacterScripts")
    starter_gui = _make_service(root, "StarterGui")
    replicated_storage = _make_service(root, "ReplicatedStorage")
    replicated_first = _make_service(root, "ReplicatedFirst")
    server_storage = _make_service(root, "ServerStorage")

    # ---- Workspace parts -------------------------------------------------
    parts = getattr(place, "workspace_parts", None) or getattr(place, "parts", None) or []
    for part in parts:
        _make_part(workspace, part)
        stats["parts_written"] += _count_parts(part)

    # ---- Default SpawnLocation if none exists ---------------------------
    def _has_spawn(parts_list: list) -> bool:
        for p in parts_list:
            if getattr(p, "name", "") == "SpawnLocation":
                return True
            if getattr(p, "class_name", "") == "SpawnLocation":
                return True
            if _has_spawn(getattr(p, "children", None) or []):
                return True
        return False

    if not _has_spawn(parts):
        # Create a spawn location above the scene center
        spawn_item, spawn_props = _make_item(workspace, "SpawnLocation", "SpawnLocation")
        _add_string(spawn_props, "Name", "SpawnLocation")
        _add_vector3(spawn_props, "Size", 6, 1, 6)
        _add_cframe(spawn_props, "CFrame", RbxCFrame(x=0, y=5, z=0))
        _add_bool(spawn_props, "Anchored", True)
        _add_float(spawn_props, "Transparency", 1.0)  # Invisible
        _add_bool(spawn_props, "CanCollide", False)
        _add_int(spawn_props, "Duration", 0)
        _add_bool(spawn_props, "Neutral", True)
        _add_token(spawn_props, "TopSurface", 0)
        _add_token(spawn_props, "BottomSurface", 0)
        stats["parts_written"] += 1

    # ---- Camera ----------------------------------------------------------
    camera_config: RbxCameraConfig | None = getattr(place, "camera", None)
    if not camera_config:
        # Default camera: above and behind spawn looking forward
        camera_config = RbxCameraConfig(
            cframe=RbxCFrame(x=0, y=10, z=20, r00=1, r01=0, r02=0,
                             r10=0, r11=1, r12=0, r20=0, r21=0, r22=1),
            field_of_view=70.0,
        )
    _make_camera(workspace, camera_config)

    # ---- Lighting --------------------------------------------------------
    lighting_config: RbxLightingConfig | None = getattr(place, "lighting", None)
    if lighting_config:
        _make_lighting(lighting, lighting_config)

    skybox_config: RbxSkyboxConfig | None = getattr(place, "skybox", None)
    if skybox_config:
        _make_skybox(lighting, skybox_config)

    pp_config: RbxPostProcessing | None = getattr(place, "post_processing", None)
    if pp_config:
        _make_post_processing(lighting, pp_config)

    # ---- Water fill script ------------------------------------------------
    if water_regions:
        water_script = _generate_water_fill_script(water_regions)
        _make_script(server_script_service, RbxScript(
            name="WaterFill",
            source=water_script,
            script_type="Script",
        ))
        stats["scripts_written"] += 1

    # ---- Scripts ---------------------------------------------------------
    # Routing priority:
    #   1. script.parent_path (from Phase 4a.5 storage classification) — explicit
    #      per-script container assignment produced by converter.storage_classifier.
    #   2. script.script_type — fallback heuristic for scripts without a plan.
    #
    # parent_path values the classifier emits:
    #   ServerScriptService, ServerStorage, ReplicatedStorage, ReplicatedFirst,
    #   StarterPlayer.StarterPlayerScripts, StarterPlayer.StarterCharacterScripts,
    #   StarterGui
    scripts: list[RbxScript] = getattr(place, "scripts", None) or []
    _container_by_path: dict[str, Any] = {
        "ServerScriptService": server_script_service,
        "ServerStorage": server_storage,
        "ReplicatedStorage": replicated_storage,
        "ReplicatedFirst": replicated_first,
        "StarterPlayer.StarterPlayerScripts": starter_player_scripts_item,
        "StarterPlayer.StarterCharacterScripts": starter_char_scripts_item,
        "StarterGui": starter_gui,
    }
    for script in scripts:
        stype = getattr(script, "script_type", "Script")
        parent_path = getattr(script, "parent_path", None)
        target = _container_by_path.get(parent_path) if parent_path else None
        if target is None:
            # Fallback: script_type-based heuristic (legacy path for scripts
            # without a storage plan, e.g. hand-edited rehydration).
            if stype == "LocalScript":
                target = starter_player_scripts_item
            elif stype == "ModuleScript":
                target = replicated_storage
            else:
                target = server_script_service
        _make_script(target, script)
        stats["scripts_written"] += 1

    # ---- Auto-create RemoteEvents referenced by scripts --------------------
    import re as _re
    _remote_names: set[str] = set()
    for script in scripts:
        src = getattr(script, "source", "")
        # Match patterns like: ReplicatedStorage:FindFirstChild("Foo") or :WaitForChild("Foo")
        for m in _re.finditer(r'(?:FindFirstChild|WaitForChild)\s*\(\s*"([^"]+)"\s*\)', src):
            _remote_names.add(m.group(1))
    # Filter to likely RemoteEvent names (exclude common service/gui names)
    _skip = {"PlayerGui", "HUD", "Module", "ItemModule", "Pause", "Crosshair",
             "Health", "Fill", "Ammo", "Cur", "Back", "CurHealth", "Label",
             "ResumeButton", "Frame", "Background", "Checkmark",
             "HumanoidRootPart", "Humanoid", "Head", "Torso", "Character",
             "Backpack", "PlayerScripts", "leaderstats"}
    # Also skip names that scripts create as BindableEvents (not RemoteEvents)
    _bindable_names: set[str] = set()
    for script in scripts:
        src = getattr(script, "source", "")
        if "BindableEvent" in src:
            for m in _re.finditer(r'\.Name\s*=\s*"([^"]+)"', src):
                _bindable_names.add(m.group(1))
    _skip = _skip | _bindable_names
    for rname in sorted(_remote_names - _skip):
        # Only create if the script context suggests it's a RemoteEvent
        # (referenced via ReplicatedStorage or used with FireServer/OnServerEvent)
        is_remote = any(
            f'"{rname}"' in getattr(s, "source", "") and
            ("FireServer" in getattr(s, "source", "") or
             "OnServerEvent" in getattr(s, "source", "") or
             "FireClient" in getattr(s, "source", "") or
             "OnClientEvent" in getattr(s, "source", "") or
             "FireAllClients" in getattr(s, "source", ""))
            for s in scripts
        )
        if is_remote:
            _make_item(replicated_storage, "RemoteEvent", rname)

    # ---- StarterGui (ScreenGuis) -----------------------------------------
    screen_guis: list[RbxScreenGui] = getattr(place, "screen_guis", None) or []
    for sg in screen_guis:
        sg_item, sg_props = _make_item(starter_gui, "ScreenGui", getattr(sg, "name", "ScreenGui"))
        if hasattr(sg, "reset_on_spawn"):
            _add_bool(sg_props, "ResetOnSpawn", sg.reset_on_spawn)
        # Serialize ScreenGui attributes (e.g. CanvasScaler reference resolution)
        sg_attrs = getattr(sg, "attributes", None) or {}
        if sg_attrs:
            encoded = _encode_attributes(sg_attrs)
            elem = ET.SubElement(sg_props, "BinaryString", name="AttributesSerialize")
            elem.text = encoded
        children = getattr(sg, "elements", None) or getattr(sg, "children", None) or []
        for child in children:
            _make_ui_element(sg_item, child)
            stats["ui_elements"] += 1

    # Generate UI event wiring script if any buttons have onClick handlers
    ui_event_source = _generate_ui_event_script(screen_guis)
    if ui_event_source:
        ui_script_item, ui_script_props = _make_item(starter_gui, "LocalScript", "UIEventWiring")
        _add_protected_string(ui_script_props, "Source", ui_event_source)
        stats["scripts"] = stats.get("scripts", 0) + 1
        log.info("Injected UIEventWiring LocalScript for %d button handlers",
                 ui_event_source.count("Activated:Connect"))

    # ---- ServerStorage (prefab templates) --------------------------------
    prefabs = getattr(place, "prefabs", None) or []
    for prefab in prefabs:
        _make_part(server_storage, prefab)

    # ---- Lights / sounds (standalone, attached to place level) -----------
    standalone_lights: list[RbxLight] = getattr(place, "lights", None) or []
    for light in standalone_lights:
        _make_light(workspace, light)
        stats["lights"] += 1

    standalone_sounds: list[RbxSound] = getattr(place, "sounds", None) or []
    for sound in standalone_sounds:
        _make_sound(workspace, sound)
        stats["sounds"] += 1

    # ---- Write out -------------------------------------------------------
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    xml_str = _pretty_xml(root)
    output_path.write_text(xml_str, encoding="utf-8")

    return stats
