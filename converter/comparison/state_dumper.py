"""
Dump object state from both Unity and Roblox engines.

Produces a normalised state dictionary keyed by object name-path, with
position, rotation (as a 3x3 matrix), and size for every object.
"""

from __future__ import annotations

import json
import logging
import textwrap
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# State format:
#   {
#     "Workspace.Folder.PartName": {
#       "position": [x, y, z],
#       "rotation": [r00, r01, r02, r10, r11, r12, r20, r21, r22],
#       "size": [x, y, z]
#     },
#     ...
#   }

StateDict = Dict[str, Dict[str, Any]]


# ---------------------------------------------------------------------------
# Unity state extraction
# ---------------------------------------------------------------------------


def dump_unity_state(parsed_scene: Any) -> StateDict:
    """Extract all object transforms from a parsed Unity scene.

    Args:
        parsed_scene: A ``ParsedScene`` (or similar) object that exposes a
            ``game_objects`` iterable.  Each game object is expected to carry
            at minimum:

            * ``name_path`` (``str``) -- dot-separated hierarchy path
            * ``position``  (``list[float]``) -- ``[x, y, z]``
            * ``rotation``  (``list[float]``) -- 9-element row-major 3x3
            * ``scale``     (``list[float]``) -- ``[x, y, z]``

    Returns:
        A :data:`StateDict` mapping name paths to transform data.
    """
    state: StateDict = {}

    game_objects: List[Any] = []
    if hasattr(parsed_scene, "all_nodes"):
        # ParsedScene: use all_nodes dict values
        game_objects = list(parsed_scene.all_nodes.values())
    elif hasattr(parsed_scene, "game_objects"):
        game_objects = parsed_scene.game_objects
    elif isinstance(parsed_scene, dict) and "game_objects" in parsed_scene:
        game_objects = parsed_scene["game_objects"]
    elif isinstance(parsed_scene, dict) and "all_nodes" in parsed_scene:
        game_objects = list(parsed_scene["all_nodes"].values())
    else:
        logger.warning("parsed_scene has no 'all_nodes' or 'game_objects' attribute; returning empty state")
        return state

    for go in game_objects:
        # Support both attribute and dict access
        if isinstance(go, dict):
            name_path = go.get("name_path", go.get("name", "unknown"))
            position = go.get("position", [0.0, 0.0, 0.0])
            rotation = go.get("rotation", [1, 0, 0, 0, 1, 0, 0, 0, 1])
            scale = go.get("scale", [1.0, 1.0, 1.0])
        else:
            name_path = getattr(go, "name_path", getattr(go, "name", "unknown"))
            position = list(getattr(go, "position", [0.0, 0.0, 0.0]))
            rotation = list(getattr(go, "rotation", [1, 0, 0, 0, 1, 0, 0, 0, 1]))
            scale = list(getattr(go, "scale", [1.0, 1.0, 1.0]))

        state[str(name_path)] = {
            "position": [float(v) for v in position],
            "rotation": [float(v) for v in rotation],
            "size": [float(v) for v in scale],
        }

    logger.info("Dumped Unity state for %d objects", len(state))
    return state


# ---------------------------------------------------------------------------
# Roblox state dump (Luau script generation)
# ---------------------------------------------------------------------------


def dump_roblox_state_luau() -> str:
    """Generate a Luau script that dumps all workspace BasePart states to JSON.

    The script should be executed inside Roblox Studio (e.g. via
    ``mcp__Roblox_Studio__execute_luau``).  It prints a JSON string to the
    output that can be captured and fed to :func:`parse_roblox_state`.

    Returns:
        The Luau source code as a string.
    """
    return textwrap.dedent("""\
        local HttpService = game:GetService("HttpService")

        local function getNamePath(inst)
            local parts = {}
            local current = inst
            while current and current ~= game do
                table.insert(parts, 1, current.Name)
                current = current.Parent
            end
            return table.concat(parts, ".")
        end

        local result = {}
        for _, part in workspace:GetDescendants() do
            if part:IsA("BasePart") then
                local cf = part.CFrame
                local r00, r01, r02,
                      r10, r11, r12,
                      r20, r21, r22 = cf:GetComponents()
                -- GetComponents returns: x, y, z, r00..r22
                -- We already have position from cf.Position
                local pos = cf.Position
                result[getNamePath(part)] = {
                    position = {pos.X, pos.Y, pos.Z},
                    rotation = {r00, r01, r02, r10, r11, r12, r20, r21, r22},
                    size = {part.Size.X, part.Size.Y, part.Size.Z},
                }
            end
        end

        print(HttpService:JSONEncode(result))
    """)


# ---------------------------------------------------------------------------
# Roblox state parsing
# ---------------------------------------------------------------------------


def parse_roblox_state(json_str: str) -> StateDict:
    """Parse the JSON output produced by the Roblox Luau state-dump script.

    Args:
        json_str: Raw JSON string printed by the Luau script.

    Returns:
        A :data:`StateDict` with the same schema as :func:`dump_unity_state`.

    Raises:
        json.JSONDecodeError: If *json_str* is not valid JSON.
    """
    raw: Dict[str, Any] = json.loads(json_str)
    state: StateDict = {}

    for name_path, data in raw.items():
        position = [float(v) for v in data.get("position", [0, 0, 0])]
        rotation = [float(v) for v in data.get("rotation", [1, 0, 0, 0, 1, 0, 0, 0, 1])]
        size = [float(v) for v in data.get("size", [1, 1, 1])]

        state[name_path] = {
            "position": position,
            "rotation": rotation,
            "size": size,
        }

    logger.info("Parsed Roblox state for %d objects", len(state))
    return state
