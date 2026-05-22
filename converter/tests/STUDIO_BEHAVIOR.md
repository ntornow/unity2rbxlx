# Studio behavior fixtures — contract reference

This file documents the JSON schema, runner contract, and known
constraints for the gameplay-behavior fixtures in
`tests/fixtures/upload_snapshots/<project>.behavior.json`. The fixtures
are consumed by the `/e2e-test` skill
(`converter/.claude/skills/e2e-test/SKILL.md`) — that's the runbook;
this is the reference.

## Why fixtures exist

The offline-assembly test (`tests/test_offline_assembly.py`) verifies
the produced rbxlx is well-formed without ever running the game. It
cannot answer "does the player actually spawn on the dock?" or "does
pressing W move forward?" — those are runtime properties only visible
when Roblox Studio executes the place.

The behavior fixtures are short Luau scripts the `/e2e-test` skill runs
via Studio MCP `execute_luau` against a fresh Play session. Each fixture
sets up state, optionally sends input, waits, then asserts a Luau
expression matches an expected value.

## Schema

```jsonc
{
  "_schema": {
    "version": 2,                 // bumped when the preamble's contract changes
    "doc": "...",                 // human-readable overview
    "preamble": "local Players = …",  // prepended to every setup + assert body
    "input_sequence_kinds": { ... },  // documentation for input types
    "isolation": "..."             // fixture-isolation contract
  },
  "fixtures": [
    {
      "id": "spawn_lands_on_walkable_floor",   // unique within file
      "feature": "1. After spawn, …",          // user-readable feature label
      "play_mode": true,                       // fixture requires Play mode (always true today)
      "setup_luau": "_reset(); …",             // MUST start with _reset() — see below
      "input_sequence": [ … ],                  // optional ordered input actions
      "wait_seconds": 0.3,                     // optional settle delay before assert
      "assert_luau": "return …",               // expression returning a value
      "expect": true,                          // value the assert must return
      "tolerance": 0.05,                       // optional numeric tolerance
      "assert_timeout_seconds": 2.0,           // optional polling-assert window
      "depends_on": [ "previous_fixture_id" ], // ordering hint (forward refs rejected)
      "evidence_on_fail": [ "screen_capture", "console_tail" ]
    }
  ]
}
```

### Required fields per fixture

- `id`, `assert_luau`, `expect`.

### Field semantics

| Field | Meaning |
|---|---|
| `setup_luau` | Executed via `execute_luau` once before inputs. **Must start with `_reset()`** (see isolation below). |
| `input_sequence` | Ordered list. Each entry has `kind` ∈ `{keyboard, mouse_move, mouse_click}` or is a `{action: "wait", wait_time_ms: N}` standalone wait. Keyboard/mouse entries map directly to `mcp__Roblox_Studio__user_keyboard_input` / `user_mouse_input` payloads. |
| `wait_seconds` | After inputs, before assert. For the visible-effect time, not for polling. |
| `assert_luau` | Wrapped by the runner in `pcall` returning `{ok, value}` so the runner can distinguish "Luau raised" from "value mismatch". |
| `expect` | The value the assert must return. Numbers respect `tolerance` (absolute). Booleans / strings / tables compare by equality. |
| `assert_timeout_seconds` | When > 0, the runner re-executes the assert every `poll_interval_seconds` (default 0.5s) until the value matches `expect` or the timeout elapses. Defends against flaky `wait → assert once` (Codex finding #6). Zero/absent = single-shot. |
| `depends_on` | Earlier fixture IDs whose effects this fixture relies on (e.g. `rifle_visible_in_viewport` depends on `rifle_picked_up_sets_hasRifle`). Forward references are rejected at validation time. |
| `evidence_on_fail` | When the fixture fails, the skill captures these — `screen_capture` → PNG, `console_tail` → text. Saved next to the report. |

## Fixture isolation (Codex finding #5)

Every fixture's `setup_luau` MUST start with `_reset()`. `_reset()` is
defined in the preamble; it:

- Disconnects any `RBXScriptConnection`s left in `_G._state` (health log,
  ammo log, descendant-added listener, etc.)
- Resets `_G._state` to an empty table
- Clears the workspace E2E mouse-channel attributes (so a previous
  fixture's mouse seq doesn't carry into this one)

Without `_reset()` the suite becomes order-dependent — a `setup_luau`
that adds a `Health` connection without disconnecting the previous one
silently double-counts; mouse-look fixtures inherit the previous seq;
etc.

`_reset()` deliberately does NOT touch:

- Character attributes (`hasKey`, `hasRifle`) — fixtures with
  `depends_on` need these to persist
- HumanoidRootPart position — fixtures that need a specific position
  teleport themselves
- Any Workspace state outside the E2E channel

A pytest assertion
(`tests/test_studio_behavior_runner.py::TestValidateBehaviorFile`
`::test_simplefps_behavior_is_well_formed`) enforces the
`_reset()`-first rule so future fixture additions can't forget it.

## Preamble helpers

The preamble exposes these helpers to every `setup_luau` and
`assert_luau` body:

| Name | Purpose |
|---|---|
| `plr`, `char`, `hrp`, `hum`, `cam` | LocalPlayer + character aliases |
| `_G._state` | Persists across `execute_luau` calls — store probe state here |
| `_findVisible(node)` | First non-transparent BasePart of a Model. Skips the invisible `PickupTouchDetector` triggers SimpleFPS attaches to pickup roots. |
| `_trackHealth()` / `_minHealth()` | Health-log helper that survives Humanoid auto-regen (~10s erases damage; use min over the log) |
| `_reset()` | Fixture isolation (see above) |
| `_pumpMouse(dx, dy)` | E2E mouse channel — see `docs/E2E_INPUT_CHANNEL.md` |

## Input channels

| Action | Channel | Notes |
|---|---|---|
| Keyboard press / release | `mcp__Roblox_Studio__user_keyboard_input` | Reliable. `key_code` is the Roblox `KeyCode` name. |
| Mouse click | `mcp__Roblox_Studio__user_mouse_input` with `button: "left"`/`"right"` | Reliable. |
| Mouse-look (camera yaw/pitch) | `_pumpMouse(dx, dy)` (workspace E2E channel) | MCP `user_mouse_input` synthesises `Delta=(0,0)`; cannot drive `GetMouseDelta()`. The E2E channel is documented in `docs/E2E_INPUT_CHANNEL.md`. |

## Known limitations

| Feature | Limitation |
|---|---|
| Multiplayer | Studio Play is single-player by default. Fixtures needing a second player would require Test → Local Server (not implemented). |
| DataStore / TeleportService | Don't work against a local rbxlx with no published place context. Avoid in fixtures or mock in `setup_luau`. |
| Visual correctness | Cannot be Luau-asserted. Use `evidence_on_fail: ["screen_capture"]` for manual review of "does it look right". |
| Slow gameplay effects | Use `assert_timeout_seconds` to poll instead of one-shot `wait → assert`. The default poll interval is 0.5s. |

## Refreshing a snapshot

Snapshots are the asset-ID cache the offline-assembly test uses:

```bash
python u2r.py snapshot-ids <output_dir> -o tests/fixtures/upload_snapshots/<Project>.snapshot.json
```

Bakes in `uploaded_assets`, `mesh_native_sizes`, `mesh_hierarchies`,
`asset_upload_errors`, `universe_id`, `place_id`, and
`_meta.source_unity_project`. The offline-assembly test self-locates
the Unity project from the snapshot's baked path so missing submodules
don't break the test.

## Driver CLI

The `studio_behavior_driver.py` module is the skill's CLI front door —
it's pure I/O, no MCP. Useful directly when debugging:

```bash
python3 -m tests.studio_behavior_driver list SimpleFPS
python3 -m tests.studio_behavior_driver validate SimpleFPS
python3 -m tests.studio_behavior_driver emit-plan SimpleFPS --only spawn_lands_on_walkable_floor
python3 -m tests.studio_behavior_driver report output/e2e/<run_id>/report.json
```

`emit-plan` is what the `/e2e-test` skill calls — for each fixture it
returns the preamble-prepended Luau, the resolved input sequence as
JSON-native action objects, and the assert metadata. The skill walks
the result through MCP and writes the combined report.
