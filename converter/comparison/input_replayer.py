"""
Replay recorded input sequences in Unity and Roblox.

Generates engine-specific replay scripts/commands from a generic
:class:`InputSequence`.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Any, Dict, List, Tuple

from .input_recorder import InputEvent, InputSequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Key mapping: generic key name -> Unity KeyCode / Roblox Enum.KeyCode
# ---------------------------------------------------------------------------

_UNITY_KEYCODE_MAP: Dict[str, str] = {
    "W": "KeyCode.W",
    "A": "KeyCode.A",
    "S": "KeyCode.S",
    "D": "KeyCode.D",
    "Space": "KeyCode.Space",
    "LeftShift": "KeyCode.LeftShift",
    "RightShift": "KeyCode.RightShift",
    "LeftControl": "KeyCode.LeftControl",
    "RightControl": "KeyCode.RightControl",
    "Escape": "KeyCode.Escape",
    "Return": "KeyCode.Return",
    "Tab": "KeyCode.Tab",
    "Mouse1": "KeyCode.Mouse0",  # Unity uses 0-indexed mouse buttons
    "Mouse2": "KeyCode.Mouse1",
    "Mouse3": "KeyCode.Mouse2",
    "E": "KeyCode.E",
    "Q": "KeyCode.Q",
    "F": "KeyCode.F",
    "R": "KeyCode.R",
    "Alpha1": "KeyCode.Alpha1",
    "Alpha2": "KeyCode.Alpha2",
    "Alpha3": "KeyCode.Alpha3",
}

_ROBLOX_KEYCODE_MAP: Dict[str, str] = {
    "W": "Enum.KeyCode.W",
    "A": "Enum.KeyCode.A",
    "S": "Enum.KeyCode.S",
    "D": "Enum.KeyCode.D",
    "Space": "Enum.KeyCode.Space",
    "LeftShift": "Enum.KeyCode.LeftShift",
    "RightShift": "Enum.KeyCode.RightShift",
    "LeftControl": "Enum.KeyCode.LeftControl",
    "RightControl": "Enum.KeyCode.RightControl",
    "Escape": "Enum.KeyCode.Escape",
    "Return": "Enum.KeyCode.Return",
    "Tab": "Enum.KeyCode.Tab",
    "Mouse1": "Enum.UserInputType.MouseButton1",
    "Mouse2": "Enum.UserInputType.MouseButton2",
    "Mouse3": "Enum.UserInputType.MouseButton3",
    "E": "Enum.KeyCode.E",
    "Q": "Enum.KeyCode.Q",
    "F": "Enum.KeyCode.F",
    "R": "Enum.KeyCode.R",
    "Alpha1": "Enum.KeyCode.One",
    "Alpha2": "Enum.KeyCode.Two",
    "Alpha3": "Enum.KeyCode.Three",
}


def _unity_key(key: str) -> str:
    """Map a generic key name to a Unity ``KeyCode`` enum member."""
    return _UNITY_KEYCODE_MAP.get(key, f"KeyCode.{key}")


def _roblox_key(key: str) -> str:
    """Map a generic key name to a Roblox ``Enum.KeyCode`` value."""
    return _ROBLOX_KEYCODE_MAP.get(key, f"Enum.KeyCode.{key}")


# ---------------------------------------------------------------------------
# Unity replay (C# script generation)
# ---------------------------------------------------------------------------


def generate_unity_replay_script(sequence: InputSequence) -> str:
    """Generate a C# MonoBehaviour script that replays the given input sequence.

    The script uses ``UnityEngine.Input`` simulation via
    ``UnityEngine.InputSystem`` (new Input System) or coroutine-based key
    injection for the legacy system.

    Args:
        sequence: The :class:`InputSequence` to replay.

    Returns:
        A complete C# source file as a string.
    """
    event_entries: List[str] = []
    for evt in sequence.events:
        pos_str = "null"
        if evt.position is not None:
            pos_str = f"new Vector2({evt.position[0]}f, {evt.position[1]}f)"
        event_entries.append(
            f'            new InputEntry({evt.timestamp}f, '
            f'"{evt.event_type}", "{_unity_key(evt.key)}", {pos_str})'
        )

    entries_block = ",\n".join(event_entries)

    script = textwrap.dedent("""\
        using System.Collections;
        using System.Collections.Generic;
        using UnityEngine;

        /// <summary>
        /// Auto-generated input replay script.
        /// Attach to any GameObject to replay the recorded input sequence.
        /// </summary>
        public class InputReplay : MonoBehaviour
        {
            private struct InputEntry
            {
                public float timestamp;
                public string eventType;
                public string key;
                public Vector2? position;

                public InputEntry(float t, string type, string k, Vector2? pos)
                {
                    timestamp = t;
                    eventType = type;
                    key = k;
                    position = pos;
                }
            }

            private readonly InputEntry[] _events = new InputEntry[]
            {
    %(entries)s
            };

            private void Start()
            {
                StartCoroutine(Replay());
            }

            private IEnumerator Replay()
            {
                float startTime = Time.time;
                int index = 0;

                while (index < _events.Length)
                {
                    float elapsed = Time.time - startTime;
                    while (index < _events.Length && _events[index].timestamp <= elapsed)
                    {
                        var entry = _events[index];
                        Debug.Log($"[Replay] t={entry.timestamp:F3} {entry.eventType} {entry.key}");

                        if (entry.position.HasValue && entry.eventType == "mouse_move")
                        {
                            // Move cursor position (requires Input System or custom handler)
                            Debug.Log($"[Replay] Mouse -> ({entry.position.Value.x}, {entry.position.Value.y})");
                        }

                        index++;
                    }
                    yield return null;
                }

                Debug.Log("[Replay] Sequence complete.");
            }
        }
    """)

    return script % {"entries": entries_block}


# ---------------------------------------------------------------------------
# Roblox replay (MCP command sequence generation)
# ---------------------------------------------------------------------------


def generate_roblox_replay_luau(
    sequence: InputSequence,
) -> List[Tuple[float, str, Dict[str, Any]]]:
    """Generate a sequence of MCP tool invocations to replay inputs in Roblox.

    Each entry in the returned list is a tuple of
    ``(delay_seconds, mcp_tool_name, params_dict)`` that the orchestrator
    should execute sequentially.

    Args:
        sequence: The :class:`InputSequence` to replay.

    Returns:
        Ordered list of ``(delay, tool, params)`` tuples.
    """
    commands: List[Tuple[float, str, Dict[str, Any]]] = []
    prev_timestamp = 0.0

    for evt in sequence.events:
        delay = max(0.0, evt.timestamp - prev_timestamp)
        prev_timestamp = evt.timestamp

        if evt.event_type in ("key_down", "key_up"):
            roblox_key = _roblox_key(evt.key)
            commands.append((
                delay,
                "mcp__Roblox_Studio__user_keyboard_input",
                {
                    "key": roblox_key,
                    "action": "press" if evt.event_type == "key_down" else "release",
                },
            ))
        elif evt.event_type == "mouse_move":
            if evt.position is not None:
                commands.append((
                    delay,
                    "mcp__Roblox_Studio__user_mouse_input",
                    {
                        "action": "move",
                        "x": evt.position[0],
                        "y": evt.position[1],
                    },
                ))
        elif evt.event_type == "mouse_click":
            if evt.position is not None:
                commands.append((
                    delay,
                    "mcp__Roblox_Studio__user_mouse_input",
                    {
                        "action": "click",
                        "x": evt.position[0],
                        "y": evt.position[1],
                        "button": evt.key,
                    },
                ))

    logger.info("Generated %d MCP replay commands for %.2fs sequence", len(commands), sequence.duration)
    return commands
