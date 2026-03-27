"""
Transform and property comparison between Unity and Roblox object states.

Matches objects by name path and computes per-object deltas for position,
rotation, and size.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Type alias for the state dict produced by state_dumper
StateDict = Dict[str, Dict[str, Any]]


@dataclass
class StateDiffResult:
    """Result of comparing Unity and Roblox object states."""

    matched_objects: int = 0
    """Number of objects matched by name path in both engines."""

    unmatched_unity: List[str] = field(default_factory=list)
    """Name paths present in Unity but not found in Roblox."""

    unmatched_roblox: List[str] = field(default_factory=list)
    """Name paths present in Roblox but not found in Unity."""

    position_diffs: Dict[str, float] = field(default_factory=dict)
    """Per-object Euclidean distance between matched positions."""

    rotation_diffs: Dict[str, float] = field(default_factory=dict)
    """Per-object Frobenius norm of the rotation matrix difference."""

    size_diffs: Dict[str, float] = field(default_factory=dict)
    """Per-object Euclidean distance between matched sizes."""

    mean_position_error: float = 0.0
    """Mean of all position differences across matched objects."""

    mean_rotation_error: float = 0.0
    """Mean of all rotation differences across matched objects."""


def _euclidean(a: List[float], b: List[float]) -> float:
    """Compute the Euclidean distance between two equal-length vectors."""
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


def _frobenius_3x3(a: List[float], b: List[float]) -> float:
    """Compute the Frobenius norm of the difference of two 3x3 matrices.

    Both *a* and *b* are 9-element lists in row-major order.
    """
    if len(a) != 9 or len(b) != 9:
        return 0.0
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


def _normalise_name(name: str) -> str:
    """Produce a canonical form for fuzzy name matching.

    Strips common prefixes like ``Workspace.`` and lowercases the result so
    that minor naming differences between engines do not prevent matching.
    """
    lower = name.lower()
    for prefix in ("workspace.", "game.workspace."):
        if lower.startswith(prefix):
            lower = lower[len(prefix):]
            break
    return lower


def diff_states(
    unity_state: StateDict,
    roblox_state: StateDict,
) -> StateDiffResult:
    """Compare object transforms between Unity and Roblox states.

    Objects are matched by their normalised name paths.  The coordinate-system
    conversion (Unity's left-hand Z-negate) is assumed to have already been
    applied when the *roblox_state* was produced.

    Args:
        unity_state: State dictionary from :func:`state_dumper.dump_unity_state`.
        roblox_state: State dictionary from :func:`state_dumper.parse_roblox_state`.

    Returns:
        A :class:`StateDiffResult` summarising the comparison.
    """
    result = StateDiffResult()

    # Build lookup tables keyed by normalised name
    unity_lookup: Dict[str, tuple[str, Dict[str, Any]]] = {
        _normalise_name(k): (k, v) for k, v in unity_state.items()
    }
    roblox_lookup: Dict[str, tuple[str, Dict[str, Any]]] = {
        _normalise_name(k): (k, v) for k, v in roblox_state.items()
    }

    unity_keys = set(unity_lookup.keys())
    roblox_keys = set(roblox_lookup.keys())

    matched_keys = unity_keys & roblox_keys
    result.matched_objects = len(matched_keys)
    result.unmatched_unity = [
        unity_lookup[k][0] for k in sorted(unity_keys - roblox_keys)
    ]
    result.unmatched_roblox = [
        roblox_lookup[k][0] for k in sorted(roblox_keys - unity_keys)
    ]

    total_pos_error = 0.0
    total_rot_error = 0.0

    for norm_name in sorted(matched_keys):
        u_name, u_data = unity_lookup[norm_name]
        r_name, r_data = roblox_lookup[norm_name]

        # Position diff
        u_pos = u_data.get("position", [0.0, 0.0, 0.0])
        r_pos = r_data.get("position", [0.0, 0.0, 0.0])
        pos_diff = _euclidean(u_pos, r_pos)
        result.position_diffs[u_name] = pos_diff
        total_pos_error += pos_diff

        # Rotation diff (Frobenius norm of matrix difference)
        u_rot = u_data.get("rotation", [1, 0, 0, 0, 1, 0, 0, 0, 1])
        r_rot = r_data.get("rotation", [1, 0, 0, 0, 1, 0, 0, 0, 1])
        rot_diff = _frobenius_3x3(u_rot, r_rot)
        result.rotation_diffs[u_name] = rot_diff
        total_rot_error += rot_diff

        # Size diff
        u_size = u_data.get("size", [1.0, 1.0, 1.0])
        r_size = r_data.get("size", [1.0, 1.0, 1.0])
        size_diff = _euclidean(u_size, r_size)
        result.size_diffs[u_name] = size_diff

    if result.matched_objects > 0:
        result.mean_position_error = total_pos_error / result.matched_objects
        result.mean_rotation_error = total_rot_error / result.matched_objects

    logger.info(
        "State diff: %d matched, %d unmatched Unity, %d unmatched Roblox, "
        "mean pos error=%.4f, mean rot error=%.4f",
        result.matched_objects,
        len(result.unmatched_unity),
        len(result.unmatched_roblox),
        result.mean_position_error,
        result.mean_rotation_error,
    )
    return result
