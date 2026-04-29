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

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.unity_types import GuidIndex, ParsedScene, PrefabLibrary
from unity.yaml_parser import parse_documents, doc_body, is_text_yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Unity humanoid-bone → Roblox R15 part name.
#
# Used by is_transform_only() to decide a clip's routing target:
#   - any bone matches  → humanoid clip → animator_runtime.luau
#   - no bone matches   → transform-only → inline TweenService
#
# See docs/design/inline-over-runtime-wrappers.md for the policy that deletes
# AnimatorBridge / TransformAnimator and mandates these two inline targets.
# ---------------------------------------------------------------------------

UNITY_TO_R15_BONE_MAP: dict[str, str] = {
    "Hips": "HumanoidRootPart",
    "Spine": "LowerTorso",
    "Chest": "UpperTorso",
    "UpperChest": "UpperTorso",
    "Neck": "Head",  # Roblox has no separate Neck part
    "Head": "Head",
    "LeftShoulder": "LeftUpperArm",
    "LeftUpperArm": "LeftUpperArm",
    "LeftLowerArm": "LeftLowerArm",
    "LeftHand": "LeftHand",
    "RightShoulder": "RightUpperArm",
    "RightUpperArm": "RightUpperArm",
    "RightLowerArm": "RightLowerArm",
    "RightHand": "RightHand",
    "LeftUpperLeg": "LeftUpperLeg",
    "LeftLowerLeg": "LeftLowerLeg",
    "LeftFoot": "LeftFoot",
    "RightUpperLeg": "RightUpperLeg",
    "RightLowerLeg": "RightLowerLeg",
    "RightFoot": "RightFoot",
}

_HUMANOID_BONE_NAMES = frozenset(UNITY_TO_R15_BONE_MAP.keys())


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
    # Union of unique transform-curve paths in this clip, used for routing.
    bone_paths: list[str] = field(default_factory=list)

    @property
    def is_transform_only(self) -> bool:
        """True when no curve path references a humanoid bone.

        Clips driving humanoid bones (Hips, Spine, LeftUpperArm, …) must
        go through animator_runtime.luau so Roblox's Humanoid can play
        them. Clips driving only arbitrary transform children (a spinning
        platform, a bobbing door) can use inline TweenService instead.

        Returns False for empty clips — we can't route what we can't see.
        """
        if not self.curves:
            return False
        for curve in self.curves:
            if not curve.path:
                continue
            for part in curve.path.split("/"):
                # Mixamo-style "Armature|LeftFoot" — last segment wins.
                clean = part.split("|")[-1] if "|" in part else part
                if clean in _HUMANOID_BONE_NAMES:
                    return False
        return True


@dataclass
class AnimState:
    """A state in an AnimatorController state machine."""
    name: str
    file_id: str
    clip_guid: str  # GUID of the referenced AnimationClip
    speed: float = 1.0
    transitions: list[AnimTransition] = field(default_factory=list)
    # When this state's motion is a BlendTree, its name is recorded here
    # and the runtime looks it up in AnimatorController.blend_trees.
    blend_tree_name: str = ""


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
class BlendTreeEntry:
    """One motion entry in a 1D blend tree.

    ``clip_guid`` is captured at parse time; ``clip_name`` is populated
    later once the GUID → AnimClip lookup is available. Nested blend
    trees are flattened: the entry's nested tree (if any) is inlined
    by picking its first resolvable child clip.
    """
    threshold: float
    clip_guid: str = ""
    clip_name: str = ""


@dataclass
class BlendTree:
    """A 1D Unity blend tree.

    The Luau runtime at ``runtime/animator_runtime.luau`` expects the
    emitted JSON shape ``{name -> {param, clips: [{clip, threshold}]}}``.
    2D blend trees are not emitted; those states log a warning and fall
    back to the first child clip in their state's ``clip_guid``.
    """
    name: str
    param: str
    entries: list[BlendTreeEntry] = field(default_factory=list)


@dataclass
class AnimatorController:
    """A parsed Unity AnimatorController."""
    name: str
    parameters: list[AnimParameter] = field(default_factory=list)
    states: list[AnimState] = field(default_factory=list)
    default_state_file_id: str = ""
    source_path: Path | None = None
    # Blend trees referenced by any state in this controller, keyed by name.
    blend_trees: dict[str, BlendTree] = field(default_factory=dict)


@dataclass
class AnimationConversionResult:
    """Result of converting animations for a project."""
    clips: list[AnimClip] = field(default_factory=list)
    controllers: list[AnimatorController] = field(default_factory=list)
    generated_scripts: list[tuple[str, str]] = field(default_factory=list)  # (name, luau_source)
    animation_data_modules: list[tuple[str, str]] = field(default_factory=list)  # (name, luau_source)
    total_clips: int = 0
    total_controllers: int = 0
    total_scripts_generated: int = 0
    # Per-clip routing decisions.  Shape:
    #   { controller_name: { clip_name: { "target": str, "reason": str } } }
    # "target" is one of: "animator_runtime", "inline_tween", "skipped".
    # Serialized into conversion_plan.json under the "animation_routing" key
    # so rehydration and downstream consumers can see what got routed where.
    routing: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    # Entries describing unconverted animation features — binary controllers
    # we can't parse, 2D blend trees we don't emit, etc. The pipeline
    # aggregates these into UNCONVERTED.md so users know what was dropped.
    # Each entry: {"category": str, "item": str, "reason": str}.
    unconverted: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# .anim file parsing
# ---------------------------------------------------------------------------

def parse_anim_file(anim_path: Path) -> AnimClip | None:
    """Parse a Unity .anim file and return an AnimClip.

    Handles both text YAML (ForceText / Mixed serialization) and binary
    (ForceBinary). Binary files are read via UnityPy and lowered to the
    same dict shape the YAML parser produces, so ``_parse_clip_body``
    consumes either path identically.

    Args:
        anim_path: Path to the .anim file.

    Returns:
        An AnimClip, or None if parsing fails.
    """
    if not anim_path.exists():
        log.warning("Animation file not found: %s", anim_path)
        return None

    if not is_text_yaml(anim_path):
        return _parse_anim_binary(anim_path)

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


def _parse_anim_binary(anim_path: Path) -> AnimClip | None:
    """Parse a binary-encoded .anim via UnityPy.

    UnityPy's typetree dump uses the same Unity field names the YAML parser
    expects (m_Name, m_PositionCurves, m_RotationCurves, m_EulerCurves,
    m_ScaleCurves, m_FloatCurves, m_SampleRate, m_Events,
    m_AnimationClipSettings) so we delegate to the existing
    ``_parse_clip_body`` after reading the typetree.
    """
    try:
        import UnityPy  # type: ignore[import-untyped]
    except ImportError:
        log.warning(
            "Binary .anim parsing requires UnityPy. Install with: pip install UnityPy "
            "(skipping %s)", anim_path.name,
        )
        return None

    try:
        env = UnityPy.load(str(anim_path))
    except Exception as exc:  # UnityPy raises a variety of errors on malformed input
        log.warning("UnityPy failed to load %s: %s", anim_path.name, exc)
        return None

    for obj in env.objects:
        if obj.type.name != "AnimationClip":
            continue
        try:
            body = obj.read_typetree()
        except Exception as exc:
            log.warning(
                "UnityPy failed to read typetree for %s: %s", anim_path.name, exc,
            )
            return None
        if not isinstance(body, dict):
            continue
        return _parse_clip_body(body, anim_path)

    log.debug("No AnimationClip object found in binary file %s", anim_path.name)
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

    # Record unique paths so is_transform_only() and downstream routing can
    # inspect the clip without re-walking its curves.
    seen_paths: set[str] = set()
    for curve in clip.curves:
        if curve.path and curve.path not in seen_paths:
            seen_paths.add(curve.path)
            clip.bone_paths.append(curve.path)

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
    dropped_count = 0

    for kf_data in keyframe_list:
        if not isinstance(kf_data, dict):
            dropped_count += 1
            continue

        try:
            time = float(kf_data.get("time", 0))
        except (TypeError, ValueError):
            dropped_count += 1
            continue
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
            dropped_count += 1
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

    if dropped_count:
        log.warning(
            "Dropped %d malformed keyframe(s) from %s curve on path %r",
            dropped_count, property_type, path,
        )

    return curve if curve.keyframes else None


# ---------------------------------------------------------------------------
# .controller file parsing
# ---------------------------------------------------------------------------

def parse_controller_file(
    controller_path: Path,
    unconverted_out: list[dict[str, str]] | None = None,
) -> AnimatorController | None:
    """Parse a Unity .controller file and return an AnimatorController.

    Handles both text YAML and binary serialization. Binary files route
    through UnityPy and lower into the same ``docs_by_fid`` shape the YAML
    path produces (PPtrs translated to fileID/guid keys), so the same
    state-machine / transition / blend-tree walker drives both.

    Args:
        controller_path: Path to the .controller file.

    Returns:
        An AnimatorController, or None if parsing fails.
    """
    if not controller_path.exists():
        log.warning("Controller file not found: %s", controller_path)
        return None

    if not is_text_yaml(controller_path):
        return _parse_controller_binary(controller_path, unconverted_out)

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
    return _build_controller_from_docs(
        name, controller_body, docs_by_fid, controller_path, unconverted_out,
    )


# Class IDs for AnimatorController-graph object types. UnityPy exposes
# `obj.type.name` reliably across versions; the integer class_id surface
# has churned. Both match the YAML !u! tags.
_ANIMATOR_GRAPH_CIDS: dict[str, int] = {
    "AnimatorController": 91,
    "BlendTree": 206,
    "AnimatorStateTransition": 1101,
    "AnimatorState": 1102,
    "AnimatorStateMachine": 1107,
    "AnimatorTransition": 1109,  # AnyState/Entry transitions
}


def _parse_controller_binary(
    controller_path: Path,
    unconverted_out: list[dict[str, str]] | None,
) -> AnimatorController | None:
    """Parse a binary-encoded .controller via UnityPy.

    Walks every object in the SerializedFile, translates PPtr structs
    (``{m_FileID, m_PathID}``) into the YAML-style ``{fileID, guid}`` form
    the rest of the parser expects, then delegates to the same builder
    the YAML path uses.

    On failure (UnityPy missing, malformed file, no AnimatorController
    object) records an UNCONVERTED entry on ``unconverted_out`` and
    returns None.
    """
    try:
        import UnityPy  # type: ignore[import-untyped]
    except ImportError:
        log.warning(
            "Binary .controller parsing requires UnityPy. Install with: "
            "pip install UnityPy (skipping %s)", controller_path.name,
        )
        _record_binary_controller_unconverted(
            controller_path, unconverted_out,
            reason="binary-encoded .controller; UnityPy not installed",
        )
        return None

    try:
        env = UnityPy.load(str(controller_path))
    except Exception as exc:
        log.warning(
            "UnityPy failed to load binary .controller %s: %s",
            controller_path.name, exc,
        )
        _record_binary_controller_unconverted(
            controller_path, unconverted_out,
            reason="binary-encoded .controller; UnityPy load failed",
        )
        return None

    externals = _collect_externals(env)

    docs_by_fid: dict[str, tuple[int, dict]] = {}
    controller_body: dict[str, Any] | None = None

    for obj in env.objects:
        type_name = getattr(obj.type, "name", "") if hasattr(obj, "type") else ""
        try:
            body = obj.read_typetree()
        except Exception as exc:
            log.debug(
                "UnityPy failed typetree on %s/%s: %s",
                controller_path.name, type_name, exc,
            )
            continue
        if not isinstance(body, dict):
            continue
        _translate_pptrs(body, externals)
        cid = _ANIMATOR_GRAPH_CIDS.get(type_name, 0)
        docs_by_fid[str(obj.path_id)] = (cid, body)
        if type_name == "AnimatorController":
            controller_body = body

    if controller_body is None:
        _record_binary_controller_unconverted(
            controller_path, unconverted_out,
            reason="binary-encoded .controller; no AnimatorController object found",
        )
        return None

    name = controller_body.get("m_Name", controller_path.stem)
    return _build_controller_from_docs(
        name, controller_body, docs_by_fid, controller_path, unconverted_out,
    )


def _collect_externals(env: Any) -> list[Any]:
    """Pull the ordered external-dependency list off any object's
    SerializedFile. PPtr ``m_FileID`` is 1-indexed into this list.
    """
    for obj in getattr(env, "objects", []) or []:
        af = getattr(obj, "assets_file", None)
        if af is None:
            continue
        ext = getattr(af, "externals", None)
        if ext is None:
            continue
        try:
            return list(ext)
        except TypeError:
            return []
    return []


def _externals_guid_str(externals: list[Any], file_id: int) -> str:
    """Resolve a PPtr ``m_FileID`` into a ``.meta``-format hex GUID.

    Returns ``""`` when out of range or the entry has no GUID. UnityPy
    exposes the GUID as a ``UnityGUID`` (``__str__`` produces the .meta
    hex), as a hex string, or as raw bytes depending on version — handle
    all three.
    """
    idx = int(file_id) - 1
    if idx < 0 or idx >= len(externals):
        return ""
    ext = externals[idx]
    guid_val = getattr(ext, "guid", None)
    if guid_val is None:
        return ""
    if isinstance(guid_val, str):
        return guid_val
    if isinstance(guid_val, (bytes, bytearray)):
        return bytes(guid_val).hex()
    return str(guid_val)


def _translate_pptrs(body: Any, externals: list[Any]) -> None:
    """Recursively rewrite UnityPy PPtr dicts ``{m_FileID, m_PathID}`` to
    also carry ``fileID`` (the destination object's path_id) and ``guid``
    (resolved from the externals list when m_FileID > 0).

    The YAML-side parser (``_parse_blend_tree``, ``_parse_transition``,
    ``_first_leaf_clip_guid``, the state walker) reads ``fileID`` for
    local refs and ``guid`` for cross-file refs. This translation lets
    the same code drive both formats.
    """
    if isinstance(body, dict):
        if "m_FileID" in body and "m_PathID" in body and "fileID" not in body:
            file_id = body["m_FileID"]
            path_id = body["m_PathID"]
            try:
                body["fileID"] = int(path_id) if path_id is not None else 0
            except (TypeError, ValueError):
                body["fileID"] = 0
            try:
                body["guid"] = (
                    _externals_guid_str(externals, int(file_id))
                    if file_id else ""
                )
            except (TypeError, ValueError):
                body["guid"] = ""
        for v in body.values():
            _translate_pptrs(v, externals)
    elif isinstance(body, list):
        for item in body:
            _translate_pptrs(item, externals)


def _record_binary_controller_unconverted(
    controller_path: Path,
    unconverted_out: list[dict[str, str]] | None,
    reason: str,
) -> None:
    """Emit an UNCONVERTED entry for a binary .controller we couldn't
    parse, carrying the .meta GUID so scene-scoped filtering still works.
    """
    if unconverted_out is None:
        return
    ctrl_guid = ""
    meta = controller_path.with_suffix(controller_path.suffix + ".meta")
    if meta.exists():
        try:
            for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s.startswith("guid:"):
                    ctrl_guid = s.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
    entry: dict[str, str] = {
        "category": "animator_controller",
        "item": controller_path.name,
        "reason": reason,
    }
    if ctrl_guid:
        entry["guid"] = ctrl_guid
    unconverted_out.append(entry)


def _build_controller_from_docs(
    name: str,
    controller_body: dict[str, Any],
    docs_by_fid: dict[str, tuple[int, dict]],
    source_path: Path,
    unconverted_out: list[dict[str, str]] | None,
) -> AnimatorController:
    """Build an AnimatorController from a pre-indexed document graph.

    Used by both the YAML and binary parsing paths; the binary path
    feeds an equivalent ``docs_by_fid`` keyed by UnityPy path_id with
    PPtrs translated into the YAML-style ``fileID`` / ``guid`` shape.
    """
    controller = AnimatorController(name=name, source_path=source_path)

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

        state_name = body.get("m_Name", "")
        motion_ref = body.get("m_Motion", {})
        clip_guid = ""
        blend_tree_name = ""

        if isinstance(motion_ref, dict):
            clip_guid = motion_ref.get("guid", "") or ""
            if not clip_guid:
                # Local fileID refers to a BlendTree doc in this same file.
                motion_fid = str(motion_ref.get("fileID", ""))
                if motion_fid and motion_fid in docs_by_fid:
                    bt_cid, bt_body = docs_by_fid[motion_fid]
                    if bt_cid == 206:
                        bt = _parse_blend_tree(
                            bt_body, docs_by_fid, state_name,
                            unconverted_out=unconverted_out,
                            controller_name=name,
                        )
                        if bt is not None:
                            controller.blend_trees[bt.name] = bt
                            blend_tree_name = bt.name
                        # Always set a clip_guid fallback (first resolvable
                        # leaf) so rehydration / degraded playback still
                        # picks up something for unsupported 2D trees.
                        clip_guid = _first_leaf_clip_guid(
                            bt_body, docs_by_fid,
                            unconverted_out=unconverted_out,
                            context=f"{name}/{state_name}",
                        )

        state = AnimState(
            name=state_name,
            file_id=fid,
            clip_guid=clip_guid,
            speed=float(body.get("m_Speed", 1.0)),
            blend_tree_name=blend_tree_name,
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


def _parse_blend_tree(
    bt_body: dict[str, Any],
    docs_by_fid: dict[str, tuple[int, dict]],
    owning_state_name: str,
    unconverted_out: list[dict[str, str]] | None = None,
    controller_name: str = "",
) -> BlendTree | None:
    """Parse a BlendTree YAML body into a 1D BlendTree structure.

    2D blend trees (``m_BlendType`` != 0) are skipped with a warning —
    the runtime only consumes 1D. Nested blend trees flatten: a child
    that itself points at another blend tree contributes its first
    resolvable leaf clip.
    """
    blend_type = int(bt_body.get("m_BlendType", 0))
    if blend_type != 0:
        log.warning(
            "BlendTree on state %r uses unsupported BlendType=%d (2D); "
            "keeping fallback clip only",
            owning_state_name, blend_type,
        )
        if unconverted_out is not None:
            qual = f"{controller_name}/{owning_state_name}" if controller_name else owning_state_name
            unconverted_out.append({
                "category": "blend_tree",
                "item": qual,
                "reason": f"2D BlendType={blend_type} not supported; first-leaf clip used as fallback",
            })
        return None

    # Unity serialized name lives on the BlendTree doc; fall back to the
    # owning state so lookups remain deterministic.
    name = bt_body.get("m_Name") or owning_state_name or "BlendTree"
    param = bt_body.get("m_BlendParameter", "") or ""

    entries: list[BlendTreeEntry] = []
    for child in bt_body.get("m_Childs", []) or []:
        if not isinstance(child, dict):
            continue
        threshold = float(child.get("m_Threshold", 0) or 0)
        motion_ref = child.get("m_Motion", {})
        if not isinstance(motion_ref, dict):
            continue
        guid = motion_ref.get("guid", "") or ""
        if not guid:
            # Nested blend tree → inline its first leaf clip. Also record
            # the nested tree as unconverted when it's 2D, so a 1D tree
            # with a 2D grandchild isn't silently collapsed.
            nested_body = _deref_motion(motion_ref, docs_by_fid)
            if nested_body:
                nested_type = int(nested_body.get("m_BlendType", 0) or 0)
                if nested_type != 0 and unconverted_out is not None:
                    qual = (
                        f"{controller_name}/{owning_state_name}/nested"
                        if controller_name else f"{owning_state_name}/nested"
                    )
                    unconverted_out.append({
                        "category": "blend_tree",
                        "item": qual,
                        "reason": (
                            f"nested BlendType={nested_type} not supported; "
                            "first-leaf clip used as fallback"
                        ),
                    })
            guid = _first_leaf_clip_guid(
                nested_body, docs_by_fid,
                unconverted_out=unconverted_out,
                context=(
                    f"{controller_name}/{owning_state_name}"
                    if controller_name else owning_state_name
                ),
            )
        if guid:
            entries.append(BlendTreeEntry(threshold=threshold, clip_guid=guid))

    if not entries:
        return None
    return BlendTree(name=name, param=param, entries=entries)


def _deref_motion(
    motion_ref: dict[str, Any],
    docs_by_fid: dict[str, tuple[int, dict]],
) -> dict[str, Any]:
    """Resolve a Motion reference to its BlendTree doc, or {} if not one."""
    motion_fid = str(motion_ref.get("fileID", ""))
    if motion_fid and motion_fid in docs_by_fid:
        cid, body = docs_by_fid[motion_fid]
        if cid == 206:
            return body
    return {}


def _first_leaf_clip_guid(
    bt_body: dict[str, Any],
    docs_by_fid: dict[str, tuple[int, dict]],
    unconverted_out: list[dict[str, str]] | None = None,
    context: str = "",
) -> str:
    """Return the first clip GUID reachable from a (possibly nested) tree.

    When ``unconverted_out`` is provided, any nested BlendTree whose
    ``m_BlendType`` is non-zero also records an UNCONVERTED entry —
    this catches the case where a supported 1D tree contains a
    deeply nested 2D child that would otherwise silently collapse to
    its first-leaf clip.
    """
    if not bt_body:
        return ""
    for child in bt_body.get("m_Childs", []) or []:
        if not isinstance(child, dict):
            continue
        child_motion = child.get("m_Motion", {})
        if not isinstance(child_motion, dict):
            continue
        guid = child_motion.get("guid", "") or ""
        if guid:
            return guid
        nested = _deref_motion(child_motion, docs_by_fid)
        if nested:
            nested_type = int(nested.get("m_BlendType", 0) or 0)
            if nested_type != 0 and unconverted_out is not None:
                qual = f"{context}/nested" if context else "nested"
                unconverted_out.append({
                    "category": "blend_tree",
                    "item": qual,
                    "reason": (
                        f"nested BlendType={nested_type} not supported; "
                        "first-leaf clip used as fallback"
                    ),
                })
            nested_guid = _first_leaf_clip_guid(
                nested, docs_by_fid, unconverted_out, context,
            )
            if nested_guid:
                return nested_guid
    return ""


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
    lines.append(
        "-- Inline TweenService per docs/design/inline-over-runtime-wrappers.md"
        " (no TransformAnimator / AnimatorBridge require).")
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
# Unified state machine script generation
# ---------------------------------------------------------------------------

def generate_state_machine_script(
    controller: AnimatorController,
    clips_by_guid: dict[str, AnimClip],
    game_object_name: str = "",
) -> str:
    """Generate a unified Luau state machine script for an AnimatorController.

    Instead of separate scripts per clip, this generates a single script that:
    - Defines all animation states with their clip playback
    - Evaluates transition conditions each frame
    - Switches states based on parameter values
    - Supports exit-time transitions (play clip, then auto-transition)

    Args:
        controller: The parsed AnimatorController.
        clips_by_guid: Dict mapping clip GUID to AnimClip.
        game_object_name: Name of the target Roblox part/model.

    Returns:
        Luau source code as a string, or "" if no usable states.
    """
    # Build state info with resolved clips
    states_with_clips: list[tuple[AnimState, AnimClip | None]] = []
    state_by_fid: dict[str, AnimState] = {}
    for state in controller.states:
        clip = clips_by_guid.get(state.clip_guid)
        states_with_clips.append((state, clip))
        state_by_fid[state.file_id] = state

    if not states_with_clips:
        return ""

    # Find default state
    default_state = None
    for state, clip in states_with_clips:
        if state.file_id == controller.default_state_file_id:
            default_state = state
            break
    if default_state is None and states_with_clips:
        default_state = states_with_clips[0][0]

    lines: list[str] = []
    lines.append("-- Auto-generated Animator State Machine")
    lines.append(f"-- Controller: {controller.name}")
    lines.append(f"-- States: {len(states_with_clips)}, Parameters: {len(controller.parameters)}")
    lines.append("")
    lines.append('local TweenService = game:GetService("TweenService")')
    lines.append('local RunService = game:GetService("RunService")')
    lines.append("")

    # Find target
    target_name = game_object_name or controller.name
    lines.append(f'local target = workspace:FindFirstChild("{target_name}", true)')
    lines.append("if not target then return end")
    lines.append("if target:IsA('Model') then")
    lines.append("    target = target.PrimaryPart or target:FindFirstChildWhichIsA('BasePart') or target")
    lines.append("end")
    lines.append("")

    # Initialize parameters as attributes on the target
    lines.append("-- Initialize parameters")
    for param in controller.parameters:
        if param.param_type == 4:  # Bool
            lines.append(f'target:SetAttribute("{param.name}", {str(param.default_bool).lower()})')
        elif param.param_type == 1:  # Float
            lines.append(f'target:SetAttribute("{param.name}", {param.default_float})')
        elif param.param_type == 3:  # Int
            lines.append(f'target:SetAttribute("{param.name}", {param.default_int})')
        elif param.param_type == 9:  # Trigger
            lines.append(f'target:SetAttribute("{param.name}", false)')
    lines.append("")

    # Define state play functions
    for state, clip in states_with_clips:
        safe_name = state.name.replace(" ", "_").replace("-", "_")
        if clip and clip.curves:
            lines.append(f"local function play_{safe_name}()")
            lines.append(f"\t-- Play animation: {clip.name} ({clip.duration:.2f}s)")
            # Generate simplified tween for the first position/rotation curve
            for curve in clip.curves[:3]:  # Limit to 3 curves per state
                simplified = simplify_keyframes(curve.keyframes, max_count=5)
                if len(simplified) >= 2:
                    if curve.property_type == "position":
                        kf_start = simplified[0]
                        kf_end = simplified[-1]
                        # Use delta between first and last keyframe for relative movement.
                        # This correctly handles close animations that return to origin
                        # (endpoint 0,0,0 with startpoint 0,4,0 → delta 0,-4,0).
                        dx = kf_end.value[0] - kf_start.value[0]
                        dy = kf_end.value[1] - kf_start.value[1]
                        dz = -(kf_end.value[2] - kf_start.value[2])  # Z inversion
                        lines.append(f"\tlocal info = TweenInfo.new({clip.duration:.2f})")
                        lines.append(f"\tlocal tween = TweenService:Create(target, info, {{")
                        lines.append(f"\t\tPosition = target.Position + Vector3.new({dx:.3f}, {dy:.3f}, {dz:.3f})")
                        lines.append(f"\t}})")
                        lines.append(f"\ttween:Play()")
                        lines.append(f"\ttween.Completed:Wait()")
            if not clip.curves:
                lines.append(f"\ttask.wait({clip.duration:.2f})")
            lines.append("end")
        else:
            lines.append(f"local function play_{safe_name}()")
            lines.append(f"\ttask.wait(0.1) -- No animation data")
            lines.append("end")
        lines.append("")

    # State machine loop
    lines.append(f'local currentState = "{default_state.name if default_state else ""}"')
    lines.append("")
    lines.append("local function checkTransitions()")

    for state, clip in states_with_clips:
        safe_name = state.name.replace(" ", "_").replace("-", "_")
        prefix = "if" if state == states_with_clips[0][0] else "elseif"
        lines.append(f'\t{prefix} currentState == "{state.name}" then')

        for trans in state.transitions:
            dst_state = state_by_fid.get(trans.dst_state_file_id)
            if not dst_state:
                continue

            if trans.conditions:
                cond_parts = []
                for cond in trans.conditions:
                    attr_get = f'target:GetAttribute("{cond.parameter}")'
                    if cond.mode == 1:  # If (true)
                        cond_parts.append(f"{attr_get} == true")
                    elif cond.mode == 2:  # IfNot (false)
                        cond_parts.append(f"{attr_get} == false")
                    elif cond.mode == 3:  # Greater
                        cond_parts.append(f"({attr_get} or 0) > {cond.threshold}")
                    elif cond.mode == 4:  # Less
                        cond_parts.append(f"({attr_get} or 0) < {cond.threshold}")
                    elif cond.mode == 6:  # Equals
                        cond_parts.append(f"({attr_get} or 0) == {cond.threshold}")
                    elif cond.mode == 7:  # NotEqual
                        cond_parts.append(f"({attr_get} or 0) ~= {cond.threshold}")

                if cond_parts:
                    cond_str = " and ".join(cond_parts)
                    lines.append(f'\t\tif {cond_str} then')
                    lines.append(f'\t\t\tcurrentState = "{dst_state.name}"')
                    # Reset triggers
                    for cond in trans.conditions:
                        param = next((p for p in controller.parameters if p.name == cond.parameter), None)
                        if param and param.param_type == 9:  # Trigger
                            lines.append(f'\t\t\ttarget:SetAttribute("{cond.parameter}", false)')
                    lines.append(f"\t\t\treturn true")
                    lines.append(f"\t\tend")
            elif trans.has_exit_time:
                lines.append(f'\t\t-- Auto-transition after exit time')
                lines.append(f'\t\tcurrentState = "{dst_state.name}"')
                lines.append(f"\t\treturn true")

    if states_with_clips:
        lines.append("\tend")
    lines.append("\treturn false")
    lines.append("end")
    lines.append("")

    # Main loop
    lines.append("-- State machine main loop")
    lines.append("while true do")
    for state, clip in states_with_clips:
        safe_name = state.name.replace(" ", "_").replace("-", "_")
        prefix = "if" if state == states_with_clips[0][0] else "elseif"
        lines.append(f'\t{prefix} currentState == "{state.name}" then')
        lines.append(f"\t\tplay_{safe_name}()")
    if states_with_clips:
        lines.append("\tend")
    lines.append("\tcheckTransitions()")
    lines.append("\ttask.wait()")
    lines.append("end")

    return "\n".join(lines) + "\n"


def export_controller_json(
    controller: AnimatorController,
    clip_name_by_guid: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Export an AnimatorController as a JSON-serializable dict for the runtime.

    The animator_runtime.luau expects this format for state machine evaluation.

    When ``clip_name_by_guid`` is provided, BlendTree entries resolve their
    ``clip_guid`` → ``clip_name`` so the runtime can look clips up by name.
    Without it, entries missing a name are dropped (they'd be unreachable).
    """
    clip_name_by_guid = clip_name_by_guid or {}
    states = []
    for state in controller.states:
        transitions = []
        for trans in state.transitions:
            conditions = []
            for cond in trans.conditions:
                mode_names = {1: "If", 2: "IfNot", 3: "Greater", 4: "Less", 6: "Equals", 7: "NotEqual"}
                conditions.append({
                    "parameter": cond.parameter,
                    "mode": mode_names.get(cond.mode, "If"),
                    "threshold": cond.threshold,
                })
            transitions.append({
                "destination": "",  # Filled below from file_id resolution
                "dst_file_id": trans.dst_state_file_id,
                "conditions": conditions,
                "duration": trans.transition_duration,
                "hasExitTime": trans.has_exit_time,
                "exitTime": trans.exit_time,
            })
        state_entry: dict[str, Any] = {
            "name": state.name,
            "motion": state.name,  # Use state name as motion key
            "speed": state.speed,
            "transitions": transitions,
        }
        if state.blend_tree_name:
            state_entry["blendTree"] = state.blend_tree_name
        states.append(state_entry)

    # Resolve transition destination names from file_id
    state_name_by_fid = {s.file_id: s.name for s in controller.states}
    for state_data in states:
        for trans in state_data["transitions"]:
            trans["destination"] = state_name_by_fid.get(trans.pop("dst_file_id", ""), "")

    parameters = []
    for param in controller.parameters:
        type_names = {1: "Float", 3: "Int", 4: "Bool", 9: "Trigger"}
        default_val: Any = 0
        if param.param_type == 4:
            default_val = param.default_bool
        elif param.param_type == 1:
            default_val = param.default_float
        elif param.param_type == 3:
            default_val = param.default_int
        parameters.append({
            "name": param.name,
            "type": type_names.get(param.param_type, "Float"),
            "defaultValue": default_val,
        })

    # Resolve blend-tree entries' clip_guid → clip_name for the Luau runtime.
    blend_trees: dict[str, Any] = {}
    for bt_name, bt in controller.blend_trees.items():
        out_clips: list[dict[str, Any]] = []
        for entry in bt.entries:
            clip_name = entry.clip_name or clip_name_by_guid.get(entry.clip_guid, "")
            if not clip_name:
                continue
            out_clips.append({"clip": clip_name, "threshold": entry.threshold})
        if out_clips:
            blend_trees[bt_name] = {"param": bt.param, "clips": out_clips}

    result: dict[str, Any] = {
        "name": controller.name,
        "states": states,
        "parameters": parameters,
        "defaultState": state_name_by_fid.get(controller.default_state_file_id, ""),
    }
    if blend_trees:
        result["blendTrees"] = blend_trees
    return result


def export_clip_keyframes(clip: AnimClip) -> dict[str, Any]:
    """Export a clip's keyframes as a JSON-serializable dict for runtime playback.

    Returns a dict with:
        duration: float
        bones: { boneName: [ {time, cf: {x,y,z,rx,ry,rz}} ] }
    """
    bones: dict[str, list[dict]] = {}

    for curve in clip.curves:
        # Extract bone name from animation path (e.g., "Armature/Spine/Chest" → "Chest")
        # Also store the full path for hierarchical lookups
        bone_name = curve.path.split("/")[-1] if curve.path else "Root"
        # Normalize: strip common prefixes like "mixamorig:" (Mixamo rigs)
        if ":" in bone_name:
            bone_name = bone_name.split(":")[-1]
        if bone_name not in bones:
            bones[bone_name] = []

        for kf in simplify_keyframes(curve.keyframes, max_count=10):
            entry: dict[str, Any] = {"time": round(kf.time, 3)}
            cf: dict[str, float] = {}

            if curve.property_type == "position":
                cf["x"] = round(kf.value[0], 4)
                cf["y"] = round(kf.value[1], 4)
                cf["z"] = round(-kf.value[2], 4)  # Unity→Roblox Z negation
            elif curve.property_type == "euler":
                cf["rx"] = round(kf.value[0], 2)
                cf["ry"] = round(kf.value[1], 2)
                cf["rz"] = round(-kf.value[2], 2)
            elif curve.property_type == "rotation":
                # Convert quaternion to euler
                ex, ey, ez = _quat_to_euler_degrees(*kf.value[:4])
                cf["rx"] = round(ex, 2)
                cf["ry"] = round(ey, 2)
                cf["rz"] = round(-ez, 2)
            elif curve.property_type == "scale":
                cf["sx"] = round(kf.value[0], 4)
                cf["sy"] = round(kf.value[1], 4)
                cf["sz"] = round(kf.value[2], 4)

            entry["cf"] = cf
            bones[bone_name].append(entry)

    return {
        "duration": round(clip.duration, 3),
        "bones": bones,
    }


# ---------------------------------------------------------------------------
# Project-level animation discovery and conversion
# ---------------------------------------------------------------------------

def discover_animations(
    unity_project_path: Path,
    guid_index: Any = None,
    unconverted_out: list[dict[str, str]] | None = None,
) -> tuple[list[AnimClip], list[AnimatorController]]:
    """Discover and parse all .anim and .controller files in a Unity project.

    Args:
        unity_project_path: Root path of the Unity project.
        guid_index: Optional GUID index for resolving clip references.
        unconverted_out: When supplied, ``parse_controller_file`` appends
            entries here for binary controllers and 2D blend trees that
            couldn't be emitted. Pipeline writes these to UNCONVERTED.md.

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
        ctrl = parse_controller_file(ctrl_path, unconverted_out=unconverted_out)
        if ctrl:
            controllers.append(ctrl)
            log.debug("Parsed animator controller: %s (%d states, %d params)",
                      ctrl.name, len(ctrl.states), len(ctrl.parameters))

    return clips, controllers


def convert_animations(
    unity_project_path: Path,
    guid_index: GuidIndex | None = None,
    parsed_scenes: list[ParsedScene] | None = None,
    prefab_library: PrefabLibrary | None = None,
) -> AnimationConversionResult:
    """Convert all animations in a Unity project to Roblox Luau scripts.

    Phase 4.5 routing: each clip is sent to exactly one target —
      - humanoid clips (touch R15 bone names)  → animator_runtime.luau JSON
      - transform-only clips (non-humanoid)    → inline TweenService Scripts
    No ``require()`` of deleted bridge modules is emitted from either path.
    Per-clip routing + reason is recorded on ``result.routing`` so the
    pipeline can persist it to ``conversion_plan.json``.

    Args:
        unity_project_path: Root path of the Unity project.
        guid_index: GUID index for resolving clip references in controllers.
        parsed_scenes: When provided, only controllers referenced by at
            least one scene's ``referenced_animator_controller_guids`` are
            emitted, and module names are prefixed with the scene name.
            One set of animation_data modules is emitted per (scene,
            controller) pair. Unset → project-wide emission, no prefix
            (current/test behaviour).
        prefab_library: Phase 5.9 — when supplied alongside ``parsed_scenes``,
            controllers referenced via PrefabTemplate get a prefab-name scope
            in addition to (or instead of) the scene scope. Transform-only
            tween scripts then live alongside the prefab template under
            ``ReplicatedStorage.Templates.<Prefab>`` and dedupe across
            multiple scene instances of the same prefab.

    Returns:
        AnimationConversionResult with clips, controllers, generated
        scripts, and per-clip routing metadata.
    """
    unconverted: list[dict[str, str]] = []
    clips, controllers = discover_animations(
        unity_project_path, guid_index, unconverted_out=unconverted,
    )

    result = AnimationConversionResult(
        clips=clips,
        controllers=controllers,
        total_clips=len(clips),
        total_controllers=len(controllers),
        unconverted=unconverted,
    )

    # Path lookup + GUID → clip name for BlendTree entry resolution.
    clip_by_path: dict[str, AnimClip] = {}
    for clip in clips:
        if clip.source_path:
            clip_by_path[str(clip.source_path.resolve())] = clip

    clip_name_by_guid: dict[str, str] = {}
    if guid_index:
        for clip in clips:
            if clip.source_path:
                gid = guid_index.guid_for_path(clip.source_path.resolve())
                if gid:
                    clip_name_by_guid[gid] = clip.name

    # Determine which scenes reference each controller (by GUID). Scenes
    # only directly carry Animator components when a GameObject in the
    # scene YAML has one; in most projects Animators live inside prefabs,
    # so the scene's set is empty. In that case fall back to unscoped
    # emission for every controller — filtering would drop them all.
    scenes_per_controller: dict[str, list[str]] = {}
    any_scene_has_refs = False
    if parsed_scenes and guid_index:
        for scene in parsed_scenes:
            if getattr(scene, "referenced_animator_controller_guids", None):
                any_scene_has_refs = True
                break
    if any_scene_has_refs and guid_index:
        for ctrl in controllers:
            ctrl_guid = (
                guid_index.guid_for_path(ctrl.source_path.resolve())
                if ctrl.source_path else None
            )
            if not ctrl_guid:
                continue
            for scene in parsed_scenes or ():
                refs = getattr(scene, "referenced_animator_controller_guids", set())
                if ctrl_guid in refs:
                    scene_stem = getattr(scene, "scene_path", None)
                    scene_stem = scene_stem.stem if scene_stem else ""
                    scenes_per_controller.setdefault(ctrl.name, []).append(scene_stem)

    # Phase 5.9: prefab-scoped emission. When a controller lives inside a
    # PrefabTemplate (the common case), emit one tween script per prefab
    # template rather than per-scene-instance. This dedupes scripts across
    # scene instantiations and lets the runtime require the script from
    # ``ReplicatedStorage.Templates.<Prefab>`` directly.
    #
    # Whenever ``parsed_scenes`` is supplied, restrict consideration to
    # prefabs actually instantiated by one of those scenes — independent
    # of whether the scene's controller-ref set has been pre-aggregated.
    # The aggregate-first pattern is fragile (a caller invoking
    # ``convert_animations()`` without first running
    # ``aggregate_prefab_controller_refs()`` would otherwise see every
    # prefab in the library treated as in-scope).
    instantiated_prefab_guids: set[str] | None = None
    if parsed_scenes is not None:
        instantiated_prefab_guids = set()
        for scene in parsed_scenes:
            for instance in scene.prefab_instances:
                if instance.source_prefab_guid:
                    instantiated_prefab_guids.add(instance.source_prefab_guid)

    prefab_to_guid: dict[int, str] = {}
    if prefab_library is not None:
        for guid, prefab in prefab_library.by_guid.items():
            prefab_to_guid[id(prefab)] = guid

    # Pre-walk per-instance m_Controller overrides: map override_guid →
    # set of prefab names whose instance applied that override. Lets a
    # scene-level override route to the correct prefab scope (Codex P2).
    instance_overrides_by_ctrl_guid: dict[str, set[str]] = {}
    if prefab_library is not None and parsed_scenes:
        for scene in parsed_scenes:
            for inst in scene.prefab_instances:
                if not inst.source_prefab_guid:
                    continue
                inst_prefab = (prefab_library.by_guid or {}).get(
                    inst.source_prefab_guid
                )
                if inst_prefab is None:
                    continue
                for mod in inst.modifications or ():
                    if not isinstance(mod, dict):
                        continue
                    prop = mod.get("propertyPath", "")
                    if not isinstance(prop, str) or not prop.endswith("m_Controller"):
                        continue
                    obj_ref = mod.get("objectReference") or {}
                    if not isinstance(obj_ref, dict):
                        continue
                    override_guid = obj_ref.get("guid", "")
                    if isinstance(override_guid, str) and override_guid:
                        instance_overrides_by_ctrl_guid.setdefault(
                            override_guid, set(),
                        ).add(inst_prefab.name)

    prefabs_per_controller: dict[str, list[str]] = {}
    if prefab_library is not None and guid_index:
        for ctrl in controllers:
            ctrl_guid = (
                guid_index.guid_for_path(ctrl.source_path.resolve())
                if ctrl.source_path else None
            )
            if not ctrl_guid:
                continue
            for prefab in prefab_library.prefabs:
                if ctrl_guid not in prefab.referenced_animator_controller_guids:
                    continue
                if instantiated_prefab_guids is not None:
                    prefab_guid = prefab_to_guid.get(id(prefab))
                    if prefab_guid is None or prefab_guid not in instantiated_prefab_guids:
                        continue
                if prefab.name not in prefabs_per_controller.setdefault(
                    ctrl.name, []
                ):
                    prefabs_per_controller[ctrl.name].append(prefab.name)
            # Per-instance override: even when the prefab template itself
            # doesn't reference this controller, an instance that swapped
            # to it should still pull the prefab into scope so the override
            # animates under the prefab template, not the scene.
            for prefab_name in sorted(
                instance_overrides_by_ctrl_guid.get(ctrl_guid, set()),
            ):
                if prefab_name not in prefabs_per_controller.setdefault(
                    ctrl.name, [],
                ):
                    prefabs_per_controller[ctrl.name].append(prefab_name)
    default_scopes = [""]

    for ctrl in controllers:
        scene_scopes = scenes_per_controller.get(ctrl.name, [])
        prefab_scopes = prefabs_per_controller.get(ctrl.name, [])
        # Prefab scopes win when present (5.9): the controller belongs to a
        # prefab template, and the runtime requires the script from
        # ``ReplicatedStorage.Templates.<Prefab>``. Scene scoping remains for
        # controllers attached directly to a scene's GameObjects.
        if prefab_scopes:
            scopes = list(prefab_scopes)
        elif scene_scopes:
            scopes = list(scene_scopes)
        else:
            scopes = default_scopes

        scene_match = ctrl.name in scenes_per_controller
        prefab_match = ctrl.name in prefabs_per_controller
        if any_scene_has_refs and not scene_match and not prefab_match:
            # Scene filtering active and this controller is unreferenced
            # by any parsed scene OR any prefab — skip, log as routing.
            result.routing.setdefault(ctrl.name, {})["__controller__"] = {
                "target": "skipped",
                "reason": "not referenced by any parsed scene",
            }
            continue

        # Resolve clips the controller actually references (via states).
        ctrl_clips: list[AnimClip] = []
        clips_by_guid: dict[str, AnimClip] = {}
        for state in ctrl.states:
            if not state.clip_guid or not guid_index:
                continue
            clip_path = guid_index.resolve(state.clip_guid)
            if not clip_path:
                continue
            resolved_key = str(clip_path.resolve())
            clip = clip_by_path.get(resolved_key)
            if clip and clip not in ctrl_clips:
                ctrl_clips.append(clip)
                clips_by_guid[state.clip_guid] = clip

        if not ctrl_clips:
            continue

        # Partition the controller's clips by routing target.
        humanoid_clips: list[AnimClip] = []
        transform_only_clips: list[AnimClip] = []
        per_clip_routing: dict[str, dict[str, str]] = {}
        for clip in ctrl_clips:
            if clip.is_transform_only:
                transform_only_clips.append(clip)
                per_clip_routing[clip.name] = {
                    "target": "inline_tween",
                    "reason": "non-humanoid transform-only curves",
                }
            else:
                humanoid_clips.append(clip)
                per_clip_routing[clip.name] = {
                    "target": "animator_runtime",
                    "reason": "humanoid bone targets or empty curve set",
                }
        result.routing[ctrl.name] = per_clip_routing

        # Emit per (scene, controller) pair.
        for scope in scopes:
            prefix = f"{scope}_" if scope else ""

            # State-machine script for humanoid clips when the controller
            # has transitions — one script per scope.
            has_transitions = any(s.transitions for s in ctrl.states)
            if humanoid_clips and has_transitions and len(ctrl.states) >= 2:
                script_name = f"Anim_{prefix}{ctrl.name}_StateMachine"
                luau_source = generate_state_machine_script(
                    controller=ctrl,
                    clips_by_guid={g: c for g, c in clips_by_guid.items() if not c.is_transform_only},
                    game_object_name=ctrl.name,
                )
                if luau_source:
                    result.generated_scripts.append((script_name, luau_source))

            # Inline TweenService script for every transform-only clip,
            # regardless of state-machine presence — these never go
            # through animator_runtime.
            for clip in transform_only_clips:
                if not clip.curves:
                    continue
                script_name = f"Anim_{prefix}{ctrl.name}_{clip.name}"
                luau_source = generate_tween_script(
                    clip=clip,
                    game_object_name=ctrl.name,
                    controller=ctrl,
                )
                if luau_source:
                    result.generated_scripts.append((script_name, luau_source))

            # animator_runtime JSON module — emit only when at least one
            # humanoid clip lands here. Transform-only clips are NOT
            # bundled (they're inline Scripts above).
            if humanoid_clips:
                controller_data = export_controller_json(ctrl, clip_name_by_guid)
                keyframes: dict[str, Any] = {
                    clip.name: export_clip_keyframes(clip) for clip in humanoid_clips
                }
                combined = {"controller": controller_data, "keyframes": keyframes}
                module_name = f"AnimationData_{prefix}{ctrl.name}"
                json_str = json.dumps(combined, indent=2)
                module_source = (
                    f"-- Auto-generated animation data for {module_name}\n"
                    f"-- Consumed by animator_runtime.luau LoadKeyframes()\n"
                    f"-- Policy: see docs/design/inline-over-runtime-wrappers.md\n"
                    f"local data = game:GetService(\"HttpService\")"
                    f":JSONDecode([==[{json_str}]==])\n"
                    f"return data\n"
                )
                result.animation_data_modules.append((module_name, module_source))

    # Scene-scoped runs: UNCONVERTED entries were collected during
    # project-wide discovery, before the scene filter got applied.
    # Drop entries for controllers the current run isn't emitting
    # output for, so the md doesn't report features from unrelated
    # scenes.
    if any_scene_has_refs:
        accepted_names = {
            ctrl.name for ctrl in controllers
            if ctrl.name in scenes_per_controller
            or ctrl.name in prefabs_per_controller
        }
        any_scene_refs: set[str] = set()
        for scene in parsed_scenes or ():
            any_scene_refs.update(
                getattr(scene, "referenced_animator_controller_guids", set())
            )
        filtered: list[dict[str, str]] = []
        for entry in result.unconverted:
            category = entry.get("category", "")
            if category == "blend_tree":
                # Item shape is "Controller/..." — keep iff controller accepted.
                owner = entry.get("item", "").split("/", 1)[0]
                if owner in accepted_names:
                    filtered.append(entry)
            elif category == "animator_controller":
                # Binary controllers only know their .meta guid.
                guid = entry.get("guid", "")
                if guid and guid in any_scene_refs:
                    filtered.append(entry)
                # If no guid recorded (no .meta), drop — we can't
                # prove the controller is in scope.
            else:
                filtered.append(entry)
        result.unconverted = filtered

    # Standalone clips (no controller references them) — treated as
    # transform-only if they match the predicate, else still dropped
    # through generate_tween_script (which is the only non-bridge
    # option available for orphaned clips).
    referenced_clips: set[str] = set()
    for ctrl in controllers:
        for state in ctrl.states:
            if state.clip_guid and guid_index:
                clip_path = guid_index.resolve(state.clip_guid)
                if clip_path:
                    referenced_clips.add(str(clip_path.resolve()))

    for clip in clips:
        if not clip.source_path:
            continue
        if str(clip.source_path.resolve()) in referenced_clips:
            continue
        if not clip.curves:
            continue
        script_name = f"Anim_{clip.name}"
        luau_source = generate_tween_script(clip=clip)
        if luau_source:
            result.generated_scripts.append((script_name, luau_source))
            result.routing.setdefault("__orphans__", {})[clip.name] = {
                "target": "inline_tween",
                "reason": "no controller references this clip",
            }

    result.total_scripts_generated = len(result.generated_scripts)

    log.info("Animation conversion: %d clips, %d controllers, %d scripts generated",
             result.total_clips, result.total_controllers, result.total_scripts_generated)

    return result
