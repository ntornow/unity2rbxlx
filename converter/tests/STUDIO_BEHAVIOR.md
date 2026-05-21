# Studio behavior fixtures — Phase B runbook

Phase A (`tests/test_offline_assembly.py`) is fully automated and runs in
CI alongside the rest of the suite. Phase B drives the *assembled* place
file in a Roblox Studio Play session via the Studio MCP plugin — that
means a human (or Claude Code in a conversation) needs to drive the MCP
calls; pytest cannot send them directly.

This doc shows how to run a fixture set end-to-end.

## When to run

After any change that touches Player.luau, the FPS scaffolding, the
camera rig autogen, or any gameplay-relevant transpile/coherence pack.
Phase A catches asset/script wiring regressions; Phase B catches the
"the game still feels right when you play it" regressions.

## Prerequisites

1. A real (cached) conversion of the target project. SimpleFPS lives
   under `converter/output/SimpleFPS_run_*/`.
2. Roblox Studio open with `converted_place.rbxlx` loaded.
3. Studio MCP plugin connected to the Claude Code session
   (`mcp__Roblox_Studio__list_roblox_studios` returns a non-empty list).
4. **Safety**: confirm the active Studio is NOT `Agas Map of London` —
   the runner refuses to send any work to that instance per
   `CLAUDE.md` and the inline guard, but verify before starting.

## Running fixtures from a Claude Code conversation

The runner is dependency-injected so the MCP calls are made by the
agent, not by Python. A typical session looks like:

```python
from pathlib import Path
from tests.studio_behavior_runner import load_fixtures, plan_for_fixture

preamble, fixtures = load_fixtures(
    Path("tests/fixtures/upload_snapshots/SimpleFPS.behavior.json")
)
for f in fixtures:
    plan = plan_for_fixture(f, preamble)
    for step in plan:
        print(step.kind, step.payload, step.note)
```

You don't actually need to import the runner inside the agent — easier
to just look at the JSON and drive the steps directly via MCP:

```
mcp__Roblox_Studio__list_roblox_studios       # confirm target
mcp__Roblox_Studio__execute_luau               # safety check
mcp__Roblox_Studio__execute_luau               # setup_luau
mcp__Roblox_Studio__user_keyboard_input        # input_sequence (keyboard)
mcp__Roblox_Studio__user_mouse_input           # input_sequence (mouse)
sleep wait_seconds
mcp__Roblox_Studio__execute_luau               # assert_luau (pcall-wrapped)
```

Each fixture's `setup_luau` and `assert_luau` MUST be prepended with
the `_schema.preamble` from the JSON so `plr`, `char`, `hrp`, `hum`,
`cam`, and `_state` resolve.

## Reporting

Per fixture, record:

- **pass/fail** based on `assert_luau`'s return value matching
  `expect` (with `tolerance` for numbers)
- **screen_capture** when the fixture lists it in `evidence_on_fail`
  and the assertion failed
- **console_tail** likewise — pull the last ~50 lines of Output and
  attach so transient errors are visible

## Known limitations

| Feature | Limitation |
|---|---|
| Mouse-look (`mouse_yaw_rotates_camera`, `mouse_pitch_rotates_camera`) | MCP-synthesized mouse motion carries `Delta=(0,0)`. The fixtures stuff a one-shot `Vector2` into `_G._mockMouseDelta` instead — only works if the converter's test seam injection (`converter/test_seam_injector.py`) has rewritten `UserInputService:GetMouseDelta()` calls to `_getMouseDelta()`. In test mode with AI off the stub Player.luau may not exercise this path. |
| Multiplayer | Studio Play is single-player by default. Fixtures that depend on a second player need Test → Local Server. |
| DataStore / TeleportService | Don't work against a local rbxlx with no published place context. Avoid in fixtures or mock them in `setup_luau`. |
| Visual correctness | Cannot be Luau-asserted. Use `screen_capture` evidence + manual review for "does it look right". |

## Refreshing a snapshot

```bash
python u2r.py snapshot-ids <output_dir> -o tests/fixtures/upload_snapshots/<Project>.snapshot.json
```

The snapshot bakes in `uploaded_assets`, `mesh_native_sizes`,
`mesh_hierarchies`, `asset_upload_errors`, `universe_id`, `place_id`,
and `_meta.source_unity_project` (used by Phase A to self-locate the
Unity project the snapshot came from).

## Nightly automation

Not built in — the MCP plugin is interactive-only. Options for a
future nightly runner:

1. Run Claude Code in non-interactive mode with a script that drives
   the fixtures from a prompt
2. Wait for an MCP CLI client and pipe `studio_behavior_runner` to it
3. Stand up a self-hosted Roblox Studio instance with the MCP plugin
   and a custom orchestrator that takes the place of Claude Code

The runner module is designed to support all three — its three
callable parameters (`execute_luau`, `keyboard_input`, `mouse_input`)
are the only MCP touchpoints.
