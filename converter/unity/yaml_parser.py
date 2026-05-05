"""
yaml_parser.py -- Multi-document YAML parser for Unity files.

Unity scene (.unity) and prefab (.prefab) files use multi-document YAML
with a custom header:

    %YAML 1.1
    %TAG !u! tag:unity3d.com,2011:
    --- !u!{classID} &{fileID}

This module handles header stripping, document separation, per-document
error recovery, and extraction helpers.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

import yaml
try:
    from yaml import CSafeLoader as _YamlLoader
except ImportError:
    from yaml import SafeLoader as _YamlLoader


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

UNITY_YAML_HEADER = re.compile(r"^(%[A-Z][^\n]*\n)+", re.MULTILINE)

UNITY_DOC_SEPARATOR = re.compile(
    r"^--- !u!(-?\d+) &(-?\d+)(?: stripped)?.*$", re.MULTILINE
)


# ---------------------------------------------------------------------------
# Well-known Unity classIDs
# ---------------------------------------------------------------------------

CID_GAME_OBJECT = 1
CID_TRANSFORM = 4
CID_CAMERA = 20
CID_MESH_RENDERER = 23
CID_MESH_FILTER = 33
CID_RIGIDBODY = 54
CID_RIGIDBODY_2D = 50
CID_HINGE_JOINT = 59
CID_POLYGON_COLLIDER_2D = 60
CID_BOX_COLLIDER_2D = 61
CID_CIRCLE_COLLIDER_2D = 58
CID_MESH_COLLIDER = 64
CID_BOX_COLLIDER = 65
CID_AUDIO_LISTENER = 81
CID_AUDIO_SOURCE = 82
CID_ANIMATOR = 95
CID_TRAIL_RENDERER = 96
CID_RENDER_SETTINGS = 104
CID_LIGHT = 108
CID_LINE_RENDERER = 120
CID_SPHERE_COLLIDER = 135
CID_CAPSULE_COLLIDER = 136
CID_SKINNED_MESH_RENDERER = 137
CID_FIXED_JOINT = 138
CID_CHARACTER_CONTROLLER = 143
CID_SPRING_JOINT = 145
CID_CONFIGURABLE_JOINT = 153
CID_TERRAIN_COLLIDER = 154
CID_MONO_BEHAVIOUR = 114
CID_CLOTH = 183
CID_WIND_ZONE = 182
CID_NAV_MESH_AGENT = 195
CID_PARTICLE_SYSTEM = 198
CID_LOD_GROUP = 205
CID_NAV_MESH_OBSTACLE = 208
CID_SPRITE_RENDERER = 212
CID_REFLECTION_PROBE = 215
CID_TERRAIN = 218
CID_LIGHT_PROBE_GROUP = 220
CID_CANVAS_RENDERER = 222
CID_CANVAS = 223
CID_RECT_TRANSFORM = 224
CID_CANVAS_GROUP = 225
CID_VIDEO_PLAYER = 328
CID_PLAYABLE_DIRECTOR = 320
CID_PREFAB_INSTANCE = 1001

KNOWN_COMPONENT_CIDS: frozenset[int] = frozenset({
    CID_MESH_FILTER, CID_MESH_RENDERER, CID_SKINNED_MESH_RENDERER,
    CID_MONO_BEHAVIOUR, CID_BOX_COLLIDER, CID_SPHERE_COLLIDER,
    CID_CAPSULE_COLLIDER, CID_MESH_COLLIDER, CID_RIGIDBODY,
    CID_AUDIO_SOURCE, CID_LIGHT, CID_CAMERA, CID_PARTICLE_SYSTEM,
    CID_ANIMATOR, CID_CHARACTER_CONTROLLER,
    CID_CANVAS, CID_CANVAS_RENDERER, CID_CANVAS_GROUP,
    CID_LOD_GROUP, CID_NAV_MESH_AGENT, CID_NAV_MESH_OBSTACLE,
    CID_TERRAIN, CID_TERRAIN_COLLIDER, CID_LINE_RENDERER,
    CID_TRAIL_RENDERER, CID_SPRITE_RENDERER,
    CID_RIGIDBODY_2D, CID_BOX_COLLIDER_2D, CID_CIRCLE_COLLIDER_2D,
    CID_POLYGON_COLLIDER_2D, CID_HINGE_JOINT, CID_FIXED_JOINT,
    CID_SPRING_JOINT, CID_CONFIGURABLE_JOINT,
    CID_CLOTH, CID_WIND_ZONE, CID_REFLECTION_PROBE,
    CID_LIGHT_PROBE_GROUP, CID_AUDIO_LISTENER,
    CID_VIDEO_PLAYER, CID_PLAYABLE_DIRECTOR,
})

COMPONENT_CID_TO_NAME: dict[int, str] = {
    CID_MESH_FILTER: "MeshFilter",
    CID_MESH_RENDERER: "MeshRenderer",
    CID_SKINNED_MESH_RENDERER: "SkinnedMeshRenderer",
    CID_MONO_BEHAVIOUR: "MonoBehaviour",
    CID_BOX_COLLIDER: "BoxCollider",
    CID_SPHERE_COLLIDER: "SphereCollider",
    CID_CAPSULE_COLLIDER: "CapsuleCollider",
    CID_MESH_COLLIDER: "MeshCollider",
    CID_RIGIDBODY: "Rigidbody",
    CID_AUDIO_SOURCE: "AudioSource",
    CID_LIGHT: "Light",
    CID_PARTICLE_SYSTEM: "ParticleSystem",
    CID_ANIMATOR: "Animator",
    CID_CHARACTER_CONTROLLER: "CharacterController",
    CID_CAMERA: "Camera",
    CID_CANVAS: "Canvas",
    CID_CANVAS_RENDERER: "CanvasRenderer",
    CID_CANVAS_GROUP: "CanvasGroup",
    CID_LOD_GROUP: "LODGroup",
    CID_NAV_MESH_AGENT: "NavMeshAgent",
    CID_NAV_MESH_OBSTACLE: "NavMeshObstacle",
    CID_TERRAIN: "Terrain",
    CID_TERRAIN_COLLIDER: "TerrainCollider",
    CID_LINE_RENDERER: "LineRenderer",
    CID_TRAIL_RENDERER: "TrailRenderer",
    CID_SPRITE_RENDERER: "SpriteRenderer",
    CID_RIGIDBODY_2D: "Rigidbody2D",
    CID_BOX_COLLIDER_2D: "BoxCollider2D",
    CID_CIRCLE_COLLIDER_2D: "CircleCollider2D",
    CID_POLYGON_COLLIDER_2D: "PolygonCollider2D",
    CID_HINGE_JOINT: "HingeJoint",
    CID_FIXED_JOINT: "FixedJoint",
    CID_SPRING_JOINT: "SpringJoint",
    CID_CONFIGURABLE_JOINT: "ConfigurableJoint",
    CID_CLOTH: "Cloth",
    CID_WIND_ZONE: "WindZone",
    CID_REFLECTION_PROBE: "ReflectionProbe",
    CID_LIGHT_PROBE_GROUP: "LightProbeGroup",
    CID_AUDIO_LISTENER: "AudioListener",
    CID_VIDEO_PLAYER: "VideoPlayer",
    CID_PLAYABLE_DIRECTOR: "PlayableDirector",
}


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def extract_vec3(d: dict, key: str) -> tuple[float, float, float]:
    """Extract a Vector3 (x, y, z) from a Unity YAML dict."""
    v = d.get(key, {})
    if not isinstance(v, dict):
        return (0.0, 0.0, 0.0)
    return (float(v.get("x", 0)), float(v.get("y", 0)), float(v.get("z", 0)))


def extract_quat(d: dict, key: str) -> tuple[float, float, float, float]:
    """Extract a Quaternion (x, y, z, w) from a Unity YAML dict."""
    v = d.get(key, {})
    if not isinstance(v, dict):
        return (0.0, 0.0, 0.0, 1.0)
    return (
        float(v.get("x", 0)), float(v.get("y", 0)),
        float(v.get("z", 0)), float(v.get("w", 1)),
    )


def ref_file_id(ref: Any) -> str | None:
    """Extract fileID from a Unity object reference dict, or None."""
    if isinstance(ref, dict):
        fid = ref.get("fileID", 0)
        if fid:
            return str(fid)
    return None


def ref_guid(ref: Any) -> str | None:
    """Extract guid from a Unity object reference dict, or None."""
    if isinstance(ref, dict):
        guid = ref.get("guid", "")
        if guid and guid != "0" * 32:
            return guid
    return None


def ordered_child_go_fids(
    transform_body: dict,
    xform_fid_to_go_fid: dict[str, str],
) -> list[str]:
    """Walk a Transform's ``m_Children`` list and resolve to GameObject fileIDs.

    Unity stores child Transform fileIDs in display order on the parent
    Transform. The parser otherwise visits nodes in YAML-document order,
    which doesn't match the prefab's authored order — so scripts that
    read ``transform.GetChild(i)`` translate to a Roblox ``GetChildren()[i]``
    that returns the wrong sibling. (SimpleFPS Turret had Collider listed
    in the YAML before Base; ``getTBase = getChildIndex(model, 1)`` then
    returned the trigger Part instead of the rotating MeshPart.)

    Returns child GameObject fileIDs in m_Children order, dropping any
    references that don't resolve to a known GameObject. The caller is
    responsible for falling back to YAML-doc order for stragglers and for
    preserving the parent → child hierarchy invariants.
    """
    out: list[str] = []
    seen: set[str] = set()
    for child_ref in (transform_body.get("m_Children") or []):
        cf = ref_file_id(child_ref)
        if not cf:
            continue
        cgo = xform_fid_to_go_fid.get(cf)
        if cgo and cgo not in seen:
            out.append(cgo)
            seen.add(cgo)
    return out


# ---------------------------------------------------------------------------
# Document parsing
# ---------------------------------------------------------------------------

def parse_documents(
    raw_text: str,
    warnings_out: list[str] | None = None,
) -> list[tuple[int, str, dict]]:
    """Parse a Unity YAML file into (classID, fileID, body_dict) triples.

    Pre-scans document separators to capture classID and fileID before
    handing the cleaned text to PyYAML.

    Features:
    - Negative fileIDs (Prefab Variants, Unity 2018.3+)
    - Stripped documents are filtered out
    - Per-document YAML error recovery

    If ``warnings_out`` is provided, per-document parse errors are appended
    to it so callers can surface them through the final conversion report
    instead of only the logger.
    """
    # Step 1: collect (classID, fileID, is_stripped)
    doc_headers: list[tuple[int, str, bool]] = []
    for m in UNITY_DOC_SEPARATOR.finditer(raw_text):
        is_stripped = "stripped" in (m.group(0)[m.end(2) - m.start():])
        doc_headers.append((int(m.group(1)), m.group(2), is_stripped))

    # Step 2: strip header and replace separators
    cleaned = UNITY_YAML_HEADER.sub("", raw_text, count=1)
    cleaned = UNITY_DOC_SEPARATOR.sub("---", cleaned)

    # Step 3: split into individual documents and parse each
    chunks = _split_yaml_documents(cleaned)
    docs: list[dict | None] = []
    for chunk in chunks:
        chunk_stripped = chunk.strip()
        if not chunk_stripped or chunk_stripped == "---":
            docs.append(None)
            continue
        try:
            parsed = yaml.load(chunk, Loader=_YamlLoader)
        except yaml.YAMLError as exc:
            msg = f"YAML parse error in document {len(docs)} (data may be lost): {str(exc)[:200]}"
            log.warning(msg)
            if warnings_out is not None:
                warnings_out.append(msg)
            docs.append(None)
            continue
        docs.append(parsed)

    # Step 4: pair documents with headers, skip stripped docs
    result: list[tuple[int, str, dict]] = []
    header_idx = 0
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if header_idx < len(doc_headers):
            cid, fid, stripped = doc_headers[header_idx]
            header_idx += 1
        else:
            cid, fid, stripped = 0, "0", False
        if stripped:
            continue
        result.append((cid, fid, doc))

    return result


def _split_yaml_documents(text: str) -> list[str]:
    """Split cleaned YAML text into individual document strings."""
    parts: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.strip() == "---":
            parts.append("\n".join(current))
            current = []
        else:
            current.append(line)
    if current:
        parts.append("\n".join(current))
    return parts


def doc_body(doc: dict) -> dict:
    """Unwrap Unity YAML: {ClassName: {actual_props}} -> inner dict."""
    for v in doc.values():
        if isinstance(v, dict):
            return v
    return doc


def is_text_yaml(file_path: str | Path) -> bool:
    """Check if a Unity file is text-based YAML (not binary)."""
    from pathlib import Path
    p = Path(file_path)
    try:
        with open(p, "r", encoding="utf-8", errors="strict") as f:
            header = f.read(64)
        return header.startswith("%YAML") or header.startswith("---")
    except (UnicodeDecodeError, OSError):
        return False
