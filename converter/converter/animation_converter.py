"""
animation_converter.py -- Convert Unity .anim and .controller files to Roblox Luau scripts.

Parses Unity AnimationClip YAML files to extract position, rotation, and scale
keyframe curves, then generates TweenService-based Luau scripts that replicate
the animations in Roblox.

Also parses AnimatorController files to understand state machines, transitions,
and parameters so the generated scripts can respond to the same triggers.

Supported animation types:
  - Position curves (m_PositionCurves) -> TweenService position tweens
  - Rotation curves (m_RotationCurves) -> TweenService rotation tweens
  - Euler curves (m_EulerCurves) -> TweenService rotation tweens
  - Scale curves (m_ScaleCurves) -> TweenService size tweens

For complex per-frame animations (e.g. the plane flying animation with hundreds
of keyframes), we sample at reduced keyframe density and use sequential tweens.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from unity.yaml_parser import parse_documents, doc_body, is_text_yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class AnimKeyframe:
    """A single keyframe in an animation curve."""
    time: float
    value: tuple[float, ...]  # 3-tuple for pos/scale, 4-tuple for quat
    in_slope: tuple[float, ...] | None = None
    out_slope: tuple[float, ...] | None = None


@dataclass
class AnimCurve:
    """A single animation curve (e.g. position of a child path)."""
    property_type: str  # "position", "rotation", "euler", "scale"
    path: str  # child object path (empty string = self)
    keyframes: list[AnimKeyframe] = field(default_factory=list)


@dataclass
@dataclass
class AnimEvent:
    """An event in a Unity AnimationClip."""
    time: float
    function_name: str
    int_parameter: int = 0
    float_parameter: float = 0.0
    string_parameter: str = ""


@dataclass
class AnimClip:
    """A parsed Unity AnimationClip."""
    name: str
    duration: float  # m_StopTime - m_StartTime
    loop: bool  # m_LoopTime
    sample_rate: float  # m_SampleRate
    curves: list[AnimCurve] = field(default_factory=list)
    events: list[AnimEvent] = field(default_factory=list)
    source_path: Path | None = None


@dataclass
class AnimState:
    """A state in an AnimatorController state machine."""
    name: str
    file_id: str
    clip_guid: str  # GUID of the referenced AnimationClip
    speed: float = 1.0
    transitions: list[AnimTransition] = field(default_factory=list)


@dataclass
class AnimTransition:
    """A transition between states in the Animator."""
    name: str
    dst_state_file_id: str
    conditions: list[AnimCondition] = field(default_factory=list)
    has_exit_time: bool = False
    exit_time: float = 0.0
    transition_duration: float = 0.0


@dataclass
class AnimCondition:
    """A condition on an Animator state transition."""
    parameter: str
    mode: int  # 1=If(true), 2=IfNot(false), 3=Greater, 4=Less, 6=Equals, 7=NotEqual
    threshold: float = 0.0


@dataclass
class AnimParameter:
    """A parameter defined in an AnimatorController."""
    name: str
    param_type: int  # 1=Float, 3=Int, 4=Bool, 9=Trigger
    default_float: float = 0.0
    default_int: int = 0
    default_bool: bool = False


@dataclass
class AnimatorController:
    """A parsed Unity AnimatorController."""
    name: str
    parameters: list[AnimParameter] = field(default_factory=list)
    states: list[AnimState] = field(default_factory=list)
    default_state_file_id: str = ""
    source_path: Path | None = None


@dataclass
class AnimationConversionResult:
    """Result of converting animations for a project."""
    clips: list[AnimClip] = field(default_factory=list)
    controllers: list[AnimatorController] = field(default_factory=list)
    generated_scripts: list[tuple[str, str]] = field(default_factory=list)  # (name, luau_source)
    total_clips: int = 0
    total_controllers: int = 0
    total_scripts_generated: int = 0


# ---------------------------------------------------------------------------
# .anim file parsing
# ---------------------------------------------------------------------------

def parse_anim_file(anim_path: Path) -> AnimClip | None:
    """Parse a Unity .anim file and return an AnimClip.

    Args:
        anim_path: Path to the .anim file.

    Returns:
        An AnimClip, or None if parsing fails.
    """
    if not anim_path.exists():
        log.warning("Animation file not found: %s", anim_path)
        return None

    if not is_text_yaml(anim_path):
        log.debug("Animation file is binary, skipping: %s", anim_path.name)
        return None

    try:
        raw_text = anim_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Failed to read animation file %s: %s", anim_path, exc)
        return None

    triples = parse_documents(raw_text)
    if not triples:
        return None

    # Find the AnimationClip document (classID 74)
    for cid, fid, doc in triples:
        if cid != 74:
            continue
        body = doc_body(doc)
        return _parse_clip_body(body, anim_path)

    return None


def _parse_clip_body(body: dict[str, Any], source_path: Path) -> AnimClip:
    """Parse an AnimationClip document body into an AnimClip."""
    name = body.get("m_Name", source_path.stem)

    # Duration and loop settings
    settings = body.get("m_AnimationClipSettings", {})
    start_time = float(settings.get("m_StartTime", 0))
    stop_time = float(settings.get("m_StopTime", 1))
    duration = stop_time - start_time
    loop = bool(int(settings.get("m_LoopTime", 0)))
    sample_rate = float(body.get("m_SampleRate", 60))

    clip = AnimClip(
        name=name,
        duration=duration,
        loop=loop,
        sample_rate=sample_rate,
        source_path=source_path,
    )

    # Parse position curves
    for curve_data in body.get("m_PositionCurves", []) or []:
        curve = _parse_vector_curve(curve_data, "position")
        if curve:
            clip.curves.append(curve)

    # Parse rotation curves (quaternion)
    for curve_data in body.get("m_RotationCurves", []) or []:
        curve = _parse_vector_curve(curve_data, "rotation")
        if curve:
            clip.curves.append(curve)

    # Parse euler curves
    for curve_data in body.get("m_EulerCurves", []) or []:
        curve = _parse_vector_curve(curve_data, "euler")
        if curve:
            clip.curves.append(curve)

    # Parse scale curves
    for curve_data in body.get("m_ScaleCurves", []) or []:
        curve = _parse_vector_curve(curve_data, "scale")
        if curve:
            clip.curves.append(curve)

    # Parse animation events
    for event_data in body.get("m_Events", []) or []:
        if not isinstance(event_data, dict):
            continue
        func_name = event_data.get("functionName", "")
        if func_name:
            clip.events.append(AnimEvent(
                time=float(event_data.get("time", 0)),
                function_name=func_name,
                int_parameter=int(event_data.get("intParameter", 0)),
                float_parameter=float(event_data.get("floatParameter", 0)),
                string_parameter=event_data.get("stringParameter", ""),
            ))

    return clip


def _parse_vector_curve(curve_data: dict[str, Any], property_type: str) -> AnimCurve | None:
    """Parse a Unity vector/quaternion curve from YAML data."""
    if not isinstance(curve_data, dict):
        return None

    path = curve_data.get("path", "")
    inner_curve = curve_data.get("curve", {})
    if not isinstance(inner_curve, dict):
        return None

    keyframe_list = inner_curve.get("m_Curve", [])
    if not keyframe_list:
        return None

    curve = AnimCurve(property_type=property_type, path=path)

    for kf_data in keyframe_list:
        if not isinstance(kf_data, dict):
            continue

        time = float(kf_data.get("time", 0))
        value_data = kf_data.get("value", {})

        if isinstance(value_data, dict):
            if property_type == "rotation":
                # Quaternion: x, y, z, w
                value = (
                    float(value_data.get("x", 0)),
                    float(value_data.get("y", 0)),
                    float(value_data.get("z", 0)),
                    float(value_data.get("w", 1)),
                )
            else:
                # Vector3: x, y, z
                value = (
                    float(value_data.get("x", 0)),
                    float(value_data.get("y", 0)),
                    float(value_data.get("z", 0)),
                )
        elif isinstance(value_data, (int, float)):
            value = (float(value_data),)
        else:
            continue

        kf = AnimKeyframe(time=time, value=value)

        # Parse slopes if available
        in_slope = kf_data.get("inSlope")
        out_slope = kf_data.get("outSlope")
        if isinstance(in_slope, dict):
            if property_type == "rotation":
                kf.in_slope = (
                    float(in_slope.get("x", 0)), float(in_slope.get("y", 0)),
                    float(in_slope.get("z", 0)), float(in_slope.get("w", 0)),
                )
            else:
                kf.in_slope = (
                    float(in_slope.get("x", 0)), float(in_slope.get("y", 0)),
                    float(in_slope.get("z", 0)),
                )
        if isinstance(out_slope, dict):
            if property_type == "rotation":
                kf.out_slope = (
                    float(out_slope.get("x", 0)), float(out_slope.get("y", 0)),
                    float(out_slope.get("z", 0)), float(out_slope.get("w", 0)),
                )
            else:
                kf.out_slope = (
                    float(out_slope.get("x", 0)), float(out_slope.get("y", 0)),
                    float(out_slope.get("z", 0)),
                )

        curve.keyframes.append(kf)

    return curve if curve.keyframes else None


# ---------------------------------------------------------------------------
# .controller file parsing
# ---------------------------------------------------------------------------

def parse_controller_file(controller_path: Path) -> AnimatorController | None:
    """Parse a Unity .controller file and return an AnimatorController.

    Args:
        controller_path: Path to the .controller file.

    Returns:
        An AnimatorController, or None if parsing fails.
    """
    if not controller_path.exists():
        log.warning("Controller file not found: %s", controller_path)
        return None

    if not is_text_yaml(controller_path):
        log.debug("Controller file is binary, skipping: %s", controller_path.name)
        return None

    try:
        raw_text = controller_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("Failed to read controller file %s: %s", controller_path, exc)
        return None

    triples = parse_documents(raw_text)
    if not triples:
        return None

    # Index all documents by fileID
    docs_by_fid: dict[str, tuple[int, dict]] = {}
    for cid, fid, doc in triples:
        docs_by_fid[fid] = (cid, doc_body(doc))

    # Find the AnimatorController document (classID 91)
    controller_body = None
    for cid, fid, doc in triples:
        if cid == 91:
            controller_body = doc_body(doc)
            break

    if controller_body is None:
        return None

    name = controller_body.get("m_Name", controller_path.stem)
    controller = AnimatorController(name=name, source_path=controller_path)

    # Parse parameters
    for param_data in controller_body.get("m_AnimatorParameters", []) or []:
        if not isinstance(param_data, dict):
            continue
        controller.parameters.append(AnimParameter(
            name=param_data.get("m_Name", ""),
            param_type=int(param_data.get("m_Type", 0)),
            default_float=float(param_data.get("m_DefaultFloat", 0)),
            default_int=int(param_data.get("m_DefaultInt", 0)),
            default_bool=bool(int(param_data.get("m_DefaultBool", 0))),
        ))

    # Find the default state from the state machine
    layers = controller_body.get("m_AnimatorLayers", []) or []
    state_machine_fid = ""
    if layers:
        first_layer = layers[0]
        sm_ref = first_layer.get("m_StateMachine", {})
        if isinstance(sm_ref, dict):
            state_machine_fid = str(sm_ref.get("fileID", ""))

    # Parse the state machine to find states and default
    if state_machine_fid and state_machine_fid in docs_by_fid:
        sm_cid, sm_body = docs_by_fid[state_machine_fid]
        default_ref = sm_body.get("m_DefaultState", {})
        if isinstance(default_ref, dict):
            controller.default_state_file_id = str(default_ref.get("fileID", ""))

    # Parse states (classID 1102 = AnimatorState)
    for fid, (cid, body) in docs_by_fid.items():
        if cid != 1102:
            continue

        motion_ref = body.get("m_Motion", {})
        clip_guid = ""
        if isinstance(motion_ref, dict):
            clip_guid = motion_ref.get("guid", "")
            # If no GUID, the motion might be a BlendTree (local fileID reference)
            if not clip_guid:
                motion_fid = str(motion_ref.get("fileID", ""))
                if motion_fid and motion_fid in docs_by_fid:
                    bt_cid, bt_body = docs_by_fid[motion_fid]
                    if bt_cid == 206:  # BlendTree classID
                        # Extract the first child motion's clip GUID as fallback
                        children = bt_body.get("m_Childs", []) or []
                        for child in children:
                            if not isinstance(child, dict):
                                continue
                            child_motion = child.get("m_Motion", {})
                            if isinstance(child_motion, dict):
                                child_guid = child_motion.get("guid", "")
                                if child_guid:
                                    clip_guid = child_guid
                                    break
                                # Nested BlendTree: try its children too
                                nested_fid = str(child_motion.get("fileID", ""))
                                if nested_fid and nested_fid in docs_by_fid:
                                    n_cid, n_body = docs_by_fid[nested_fid]
                                    if n_cid == 206:
                                        for nc in (n_body.get("m_Childs", []) or []):
                                            if isinstance(nc, dict):
                                                nm = nc.get("m_Motion", {})
                                                if isinstance(nm, dict) and nm.get("guid"):
                                                    clip_guid = nm["guid"]
                                                    break
                                    if clip_guid:
                                        break

        state = AnimState(
            name=body.get("m_Name", ""),
            file_id=fid,
            clip_guid=clip_guid,
            speed=float(body.get("m_Speed", 1.0)),
        )

        # Parse transitions for this state
        for trans_ref in body.get("m_Transitions", []) or []:
            if not isinstance(trans_ref, dict):
                continue
            trans_fid = str(trans_ref.get("fileID", ""))
            if trans_fid and trans_fid in docs_by_fid:
                trans_cid, trans_body = docs_by_fid[trans_fid]
                transition = _parse_transition(trans_body)
                if transition:
                    state.transitions.append(transition)

        controller.states.append(state)

    return controller


def _parse_transition(body: dict[str, Any]) -> AnimTransition | None:
    """Parse an AnimatorStateTransition document body."""
    dst_ref = body.get("m_DstState", {})
    dst_fid = ""
    if isinstance(dst_ref, dict):
        dst_fid = str(dst_ref.get("fileID", ""))

    if not dst_fid:
        return None

    transition = AnimTransition(
        name=body.get("m_Name", ""),
        dst_state_file_id=dst_fid,
        has_exit_time=bool(int(body.get("m_HasExitTime", 0))),
        exit_time=float(body.get("m_ExitTime", 0)),
        transition_duration=float(body.get("m_TransitionDuration", 0)),
    )

    for cond_data in body.get("m_Conditions", []) or []:
        if not isinstance(cond_data, dict):
            continue
        transition.conditions.append(AnimCondition(
            parameter=cond_data.get("m_ConditionEvent", ""),
            mode=int(cond_data.get("m_ConditionMode", 0)),
            threshold=float(cond_data.get("m_EventTreshold", 0)),
        ))

    return transition


# ---------------------------------------------------------------------------
# Keyframe simplification
# ---------------------------------------------------------------------------

# Maximum keyframes to keep per curve when generating TweenService code.
# More than this becomes unwieldy and offers diminishing visual returns.
MAX_KEYFRAMES_PER_CURVE = 20


def simplify_keyframes(keyframes: list[AnimKeyframe], max_count: int = MAX_KEYFRAMES_PER_CURVE) -> list[AnimKeyframe]:
    """Reduce keyframe count by sampling at even intervals.

    Always keeps the first and last keyframes.  For curves with more
    keyframes than max_count, samples evenly across the time range.

    Args:
        keyframes: Original keyframe list, sorted by time.
        max_count: Maximum number of keyframes to keep.

    Returns:
        Simplified keyframe list.
    """
    if len(keyframes) <= max_count:
        return list(keyframes)

    if max_count < 2:
        return [keyframes[0], keyframes[-1]]

    # Always include first and last
    result = [keyframes[0]]
    step = (len(keyframes) - 1) / (max_count - 1)
    for i in range(1, max_count - 1):
        idx = int(round(i * step))
        idx = min(idx, len(keyframes) - 1)
        result.append(keyframes[idx])
    result.append(keyframes[-1])

    return result


# ---------------------------------------------------------------------------
# Luau code generation
# ---------------------------------------------------------------------------

def _quat_to_euler_degrees(x: float, y: float, z: float, w: float) -> tuple[float, float, float]:
    """Convert a quaternion (x, y, z, w) to Euler angles in degrees (X, Y, Z).

    Uses the ZYX convention that Unity uses internally.
    """
    # Roll (X)
    sinr_cosp = 2 * (w * x + y * z)
    cosr_cosp = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)

    # Pitch (Y)
    sinp = 2 * (w * y - z * x)
    if abs(sinp) >= 1:
        pitch = math.copysign(math.pi / 2, sinp)
    else:
        pitch = math.asin(sinp)

    # Yaw (Z)
    siny_cosp = 2 * (w * z + x * y)
    cosy_cosp = 1 - 2 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)

    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def generate_tween_script(
    clip: AnimClip,
    game_object_name: str = "",
    controller: AnimatorController | None = None,
) -> str:
    """Generate a Luau script that uses TweenService to play an AnimClip.

    For simple animations (few keyframes, position/rotation only), generates
    clean sequential TweenService calls.  For complex animations, generates
    a keyframe table and iterates through it.

    Args:
        clip: The parsed AnimClip to convert.
        game_object_name: Name of the target Roblox part/model.
        controller: Optional controller for state machine context.

    Returns:
        Luau source code as a string.
    """
    if not clip.curves:
        return ""

    target_name = game_object_name or ""

    lines: list[str] = []
    lines.append("-- Auto-generated animation script")
    lines.append(f"-- Converted from Unity AnimationClip: {clip.name}")
    lines.append(f"-- Duration: {clip.duration:.2f}s, Loop: {clip.loop}")
    lines.append("")
    lines.append("local TweenService = game:GetService(\"TweenService\")")
    lines.append("local RunService = game:GetService(\"RunService\")")
    lines.append("")

    # Animation scripts live in ServerScriptService, so find targets in workspace.
    # Try the explicit name first, then fall back to searching by clip name.
    # Extract potential target names from curve paths (used for both target search and child path trimming)
    curve_roots: set[str] = set()
    for c in clip.curves:
        if c.path:
            root = c.path.split("/")[0]
            curve_roots.add(root)

    if target_name:
        lines.append(f'local target = workspace:FindFirstChild("{target_name}", true)')
    else:
        # Try to find target by clip name or common parent patterns
        lines.append(f'-- No explicit target; search workspace for an object this animation might apply to')
        lines.append(f'local target = nil')
        if curve_roots:
            for root in sorted(curve_roots):
                lines.append(f'if not target then target = workspace:FindFirstChild("{root}", true) end')
        else:
            lines.append(f'target = workspace:FindFirstChild("{clip.name}", true)')
    lines.append(f'if not target then')
    lines.append(f'    -- Animation target not found; script will not run')
    lines.append(f'    return')
    lines.append(f'end')
    lines.append("")
    lines.append("-- If target is a Model, use its PrimaryPart or first BasePart")
    lines.append("if target:IsA('Model') then")
    lines.append("    target = target.PrimaryPart or target:FindFirstChildWhichIsA('BasePart') or target")
    lines.append("end")
    lines.append("")

    # Separate curves by path (empty path = self, non-empty = child)
    self_curves = [c for c in clip.curves if not c.path]
    child_curves = [c for c in clip.curves if c.path]

    # Group child curves by path
    child_paths: dict[str, list[AnimCurve]] = {}
    for c in child_curves:
        child_paths.setdefault(c.path, []).append(c)

    # Generate animation function
    lines.append("local function playAnimation()")

    if self_curves:
        _generate_curves_code(lines, self_curves, "target", clip, indent=1)

    for child_path, curves in child_paths.items():
        # Convert Unity hierarchy path (e.g. "Base10/Box1") to Roblox FindFirstChild chain.
        # Skip leading path segments that match curve_roots used as the target,
        # since target already IS that object.
        parts = child_path.split("/")
        if parts and parts[0] in curve_roots:
            parts = parts[1:]  # Skip the root segment — target already is this object
        safe_var = child_path.replace("/", "_").replace(" ", "_").replace("-", "_")
        if not parts:
            # Path was just the target itself — treat as self
            _generate_curves_code(lines, curves, "target", clip, indent=1)
            continue
        find_chain = "target"
        for part in parts:
            find_chain += f":FindFirstChild(\"{part}\")"
        lines.append(f"\tlocal {safe_var} = {find_chain}")
        lines.append(f"\tif {safe_var} then")
        _generate_curves_code(lines, curves, safe_var, clip, indent=2)
        lines.append(f"\tend")

    # Fire animation events at their scheduled times
    if clip.events:
        lines.append("")
        lines.append("\t-- Animation events")
        for event in sorted(clip.events, key=lambda e: e.time):
            delay = max(0, event.time)
            if event.string_parameter:
                lines.append(f'\ttask.delay({delay:.3f}, function() '
                             f'if target:FindFirstChild("{event.function_name}") then '
                             f'target:FindFirstChild("{event.function_name}"):Fire("{event.string_parameter}") end end)')
            elif event.int_parameter:
                lines.append(f'\ttask.delay({delay:.3f}, function() '
                             f'if target:FindFirstChild("{event.function_name}") then '
                             f'target:FindFirstChild("{event.function_name}"):Fire({event.int_parameter}) end end)')
            else:
                lines.append(f'\ttask.delay({delay:.3f}, function() '
                             f'if target:FindFirstChild("{event.function_name}") then '
                             f'target:FindFirstChild("{event.function_name}"):Fire() end end)')

    lines.append("end")
    lines.append("")

    # Determine how to trigger the animation
    if controller and controller.parameters:
        _generate_parameter_driven_playback(lines, clip, controller)
    elif clip.loop:
        lines.append("-- Loop the animation")
        lines.append("while true do")
        lines.append("\tplayAnimation()")
        lines.append("end")
    else:
        lines.append("-- Play once on start")
        lines.append("playAnimation()")

    return "\n".join(lines) + "\n"


def _generate_curves_code(
    lines: list[str],
    curves: list[AnimCurve],
    target_var: str,
    clip: AnimClip,
    indent: int = 1,
) -> None:
    """Generate TweenService code for a set of curves targeting one object."""
    tab = "\t" * indent

    # Collect all unique times across curves for this target
    has_position = any(c.property_type == "position" for c in curves)
    has_rotation = any(c.property_type in ("rotation", "euler") for c in curves)
    has_scale = any(c.property_type == "scale" for c in curves)

    # For each curve type, generate sequential tweens
    for curve in curves:
        simplified = simplify_keyframes(curve.keyframes)
        if len(simplified) < 2:
            continue

        if curve.property_type == "position":
            _generate_position_tweens(lines, simplified, target_var, tab)
        elif curve.property_type == "rotation":
            _generate_rotation_tweens(lines, simplified, target_var, tab)
        elif curve.property_type == "euler":
            _generate_euler_tweens(lines, simplified, target_var, tab)
        elif curve.property_type == "scale":
            _generate_scale_tweens(lines, simplified, target_var, tab)


def _generate_position_tweens(
    lines: list[str],
    keyframes: list[AnimKeyframe],
    target_var: str,
    tab: str,
) -> None:
    """Generate TweenService code for position keyframes."""
    lines.append(f"{tab}-- Position animation ({len(keyframes)} keyframes)")
    lines.append(f"{tab}local basePos = {target_var}.Position")

    for i in range(1, len(keyframes)):
        prev = keyframes[i - 1]
        curr = keyframes[i]
        dt = max(curr.time - prev.time, 0.001)

        # Unity position offset -> Roblox position offset
        # Apply coordinate system conversion: Unity (x,y,z) -> Roblox (x,y,-z)
        x, y, z = curr.value[0], curr.value[1], -curr.value[2]

        lines.append(f"{tab}local tweenInfo{i} = TweenInfo.new({dt:.4f}, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)")
        lines.append(f"{tab}local tween{i} = TweenService:Create({target_var}, tweenInfo{i}, {{")
        lines.append(f"{tab}\tPosition = basePos + Vector3.new({x:.4f}, {y:.4f}, {z:.4f})")
        lines.append(f"{tab}}})")
        lines.append(f"{tab}tween{i}:Play()")
        lines.append(f"{tab}tween{i}.Completed:Wait()")


def _generate_rotation_tweens(
    lines: list[str],
    keyframes: list[AnimKeyframe],
    target_var: str,
    tab: str,
) -> None:
    """Generate TweenService code for quaternion rotation keyframes."""
    lines.append(f"{tab}-- Rotation animation ({len(keyframes)} keyframes)")
    lines.append(f"{tab}local basePos = {target_var}.Position")

    for i in range(1, len(keyframes)):
        prev = keyframes[i - 1]
        curr = keyframes[i]
        dt = max(curr.time - prev.time, 0.001)

        # Convert Unity quaternion to Roblox-compatible Euler angles
        # Unity quat (x,y,z,w) -> Roblox: negate x,y for coordinate conversion
        qx, qy, qz, qw = curr.value
        ex, ey, ez = _quat_to_euler_degrees(-qx, -qy, qz, qw)

        lines.append(f"{tab}local tweenInfo{i} = TweenInfo.new({dt:.4f}, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)")
        lines.append(f"{tab}local tween{i} = TweenService:Create({target_var}, tweenInfo{i}, {{")
        lines.append(f"{tab}\tCFrame = CFrame.new(basePos) * CFrame.Angles(math.rad({ex:.2f}), math.rad({ey:.2f}), math.rad({ez:.2f}))")
        lines.append(f"{tab}}})")
        lines.append(f"{tab}tween{i}:Play()")
        lines.append(f"{tab}tween{i}.Completed:Wait()")


def _generate_euler_tweens(
    lines: list[str],
    keyframes: list[AnimKeyframe],
    target_var: str,
    tab: str,
) -> None:
    """Generate TweenService code for Euler angle rotation keyframes."""
    lines.append(f"{tab}-- Euler rotation animation ({len(keyframes)} keyframes)")
    lines.append(f"{tab}local basePos = {target_var}.Position")

    for i in range(1, len(keyframes)):
        prev = keyframes[i - 1]
        curr = keyframes[i]
        dt = max(curr.time - prev.time, 0.001)

        # Unity Euler (x,y,z) in degrees -> Roblox with coordinate conversion
        ex, ey, ez = curr.value[0], curr.value[1], -curr.value[2]

        lines.append(f"{tab}local tweenInfo{i} = TweenInfo.new({dt:.4f}, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)")
        lines.append(f"{tab}local tween{i} = TweenService:Create({target_var}, tweenInfo{i}, {{")
        lines.append(f"{tab}\tCFrame = CFrame.new(basePos) * CFrame.Angles(math.rad({ex:.2f}), math.rad({ey:.2f}), math.rad({ez:.2f}))")
        lines.append(f"{tab}}})")
        lines.append(f"{tab}tween{i}:Play()")
        lines.append(f"{tab}tween{i}.Completed:Wait()")


def _generate_scale_tweens(
    lines: list[str],
    keyframes: list[AnimKeyframe],
    target_var: str,
    tab: str,
) -> None:
    """Generate TweenService code for scale keyframes."""
    lines.append(f"{tab}-- Scale animation ({len(keyframes)} keyframes)")
    lines.append(f"{tab}local baseSize = {target_var}.Size")

    for i in range(1, len(keyframes)):
        prev = keyframes[i - 1]
        curr = keyframes[i]
        dt = max(curr.time - prev.time, 0.001)

        sx, sy, sz = curr.value[0], curr.value[1], curr.value[2]

        lines.append(f"{tab}local tweenInfo{i} = TweenInfo.new({dt:.4f}, Enum.EasingStyle.Quad, Enum.EasingDirection.InOut)")
        lines.append(f"{tab}local tween{i} = TweenService:Create({target_var}, tweenInfo{i}, {{")
        lines.append(f"{tab}\tSize = Vector3.new(baseSize.X * {sx:.4f}, baseSize.Y * {sy:.4f}, baseSize.Z * {sz:.4f})")
        lines.append(f"{tab}}})")
        lines.append(f"{tab}tween{i}:Play()")
        lines.append(f"{tab}tween{i}.Completed:Wait()")


def _generate_parameter_driven_playback(
    lines: list[str],
    clip: AnimClip,
    controller: AnimatorController,
) -> None:
    """Generate Luau code for parameter-driven animation (e.g. door open/close)."""
    # Find bool parameters (common for doors: "open" bool triggers open/close)
    bool_params = [p for p in controller.parameters if p.param_type == 4]
    int_params = [p for p in controller.parameters if p.param_type == 3]

    if bool_params:
        param = bool_params[0]
        lines.append(f"-- Parameter-driven animation: {param.name} (bool)")
        lines.append(f"local isActive = false")
        lines.append("")
        lines.append(f"-- Listen for attribute changes to trigger animation")
        lines.append(f"target:SetAttribute(\"{param.name}\", false)")
        lines.append("")
        lines.append(f"target:GetAttributeChangedSignal(\"{param.name}\"):Connect(function()")
        lines.append(f"\tlocal val = target:GetAttribute(\"{param.name}\")")
        lines.append(f"\tif val and not isActive then")
        lines.append(f"\t\tisActive = true")
        lines.append(f"\t\tplayAnimation()")
        if clip.loop:
            lines.append(f"\telseif not val then")
            lines.append(f"\t\tisActive = false")
        else:
            lines.append(f"\t\tisActive = false")
        lines.append(f"\tend")
        lines.append(f"end)")
    elif int_params:
        param = int_params[0]
        lines.append(f"-- Parameter-driven animation: {param.name} (int)")
        lines.append(f"target:SetAttribute(\"{param.name}\", 0)")
        lines.append("")
        lines.append(f"target:GetAttributeChangedSignal(\"{param.name}\"):Connect(function()")
        lines.append(f"\tplayAnimation()")
        lines.append(f"end)")
    else:
        # No parameters, auto-play
        if clip.loop:
            lines.append("-- Auto-play looping animation")
            lines.append("while true do")
            lines.append("\tplayAnimation()")
            lines.append("end")
        else:
            lines.append("-- Play once on start")
            lines.append("playAnimation()")


# ---------------------------------------------------------------------------
# Project-level animation discovery and conversion
# ---------------------------------------------------------------------------

def discover_animations(
    unity_project_path: Path,
    guid_index: Any = None,
) -> tuple[list[AnimClip], list[AnimatorController]]:
    """Discover and parse all .anim and .controller files in a Unity project.

    Args:
        unity_project_path: Root path of the Unity project.
        guid_index: Optional GUID index for resolving clip references.

    Returns:
        Tuple of (clips, controllers).
    """
    assets_dir = unity_project_path / "Assets"
    if not assets_dir.exists():
        return [], []

    clips: list[AnimClip] = []
    controllers: list[AnimatorController] = []

    # Find all .anim files
    for anim_path in sorted(assets_dir.rglob("*.anim")):
        clip = parse_anim_file(anim_path)
        if clip:
            clips.append(clip)
            log.debug("Parsed animation clip: %s (%d curves, %.2fs)",
                      clip.name, len(clip.curves), clip.duration)

    # Find all .controller files
    for ctrl_path in sorted(assets_dir.rglob("*.controller")):
        ctrl = parse_controller_file(ctrl_path)
        if ctrl:
            controllers.append(ctrl)
            log.debug("Parsed animator controller: %s (%d states, %d params)",
                      ctrl.name, len(ctrl.states), len(ctrl.parameters))

    return clips, controllers


def convert_animations(
    unity_project_path: Path,
    guid_index: Any = None,
) -> AnimationConversionResult:
    """Convert all animations in a Unity project to Roblox Luau scripts.

    This is the main entry point for the animation conversion phase.

    Args:
        unity_project_path: Root path of the Unity project.
        guid_index: GUID index for resolving clip references in controllers.

    Returns:
        AnimationConversionResult with clips, controllers, and generated scripts.
    """
    clips, controllers = discover_animations(unity_project_path, guid_index)

    result = AnimationConversionResult(
        clips=clips,
        controllers=controllers,
        total_clips=len(clips),
        total_controllers=len(controllers),
    )

    # Build a guid -> clip mapping if we have a guid_index
    clip_by_path: dict[str, AnimClip] = {}
    for clip in clips:
        if clip.source_path:
            clip_by_path[str(clip.source_path)] = clip

    # For each controller, find associated clips and generate scripts
    for ctrl in controllers:
        ctrl_clips: list[AnimClip] = []

        for state in ctrl.states:
            if not state.clip_guid:
                continue

            # Try to resolve the clip GUID to a path
            if guid_index:
                clip_path = guid_index.resolve(state.clip_guid)
                if clip_path and str(clip_path) in clip_by_path:
                    ctrl_clips.append(clip_by_path[str(clip_path)])

        if not ctrl_clips:
            continue

        # Generate a combined script for all clips in this controller
        for clip in ctrl_clips:
            if not clip.curves:
                continue

            script_name = f"Anim_{ctrl.name}_{clip.name}"
            luau_source = generate_tween_script(
                clip=clip,
                game_object_name=ctrl.name,
                controller=ctrl,
            )

            if luau_source:
                result.generated_scripts.append((script_name, luau_source))

    # Also generate scripts for standalone clips not referenced by any controller
    referenced_clips: set[str] = set()
    for ctrl in controllers:
        for state in ctrl.states:
            if state.clip_guid and guid_index:
                clip_path = guid_index.resolve(state.clip_guid)
                if clip_path:
                    referenced_clips.add(str(clip_path))

    for clip in clips:
        if clip.source_path and str(clip.source_path) not in referenced_clips:
            if not clip.curves:
                continue
            script_name = f"Anim_{clip.name}"
            luau_source = generate_tween_script(clip=clip)
            if luau_source:
                result.generated_scripts.append((script_name, luau_source))

    result.total_scripts_generated = len(result.generated_scripts)

    log.info("Animation conversion: %d clips, %d controllers, %d scripts generated",
             result.total_clips, result.total_controllers, result.total_scripts_generated)

    return result
