---
name: e2e-test
description: Unified end-to-end test for a converted Unity project. Runs the AI conversion offline (no upload/publish), launches Studio with the produced rbxlx, drives the gameplay-feature fixtures via Studio MCP, and writes a combined pass/fail report.
argument-hint: <project> [--close-and-relaunch] [--only fixture_ids]
allowed-tools:
  - Bash(python3 -m tests.studio_behavior_driver *)
  - Bash(python3 -m pytest *)
  - Bash(python3 -c *)
  - Bash(mkdir *)
  - Bash(date *)
  - Bash(openssl rand *)
  - Bash(cat *)
  - Read
  - Write
  - mcp__Roblox_Studio__list_roblox_studios
  - mcp__Roblox_Studio__set_active_studio
  - mcp__Roblox_Studio__execute_luau
  - mcp__Roblox_Studio__user_keyboard_input
  - mcp__Roblox_Studio__user_mouse_input
  - mcp__Roblox_Studio__screen_capture
  - mcp__Roblox_Studio__get_console_output
---

# Unified end-to-end test

This skill answers the question **"does the entire conversion pipeline
still produce a place that plays correctly?"** in one invocation. It is
the test the user originally asked for ("automated real test that does
the full conversion modulo upload/publish"). Trigger model is manual +
nightly, not per-PR CI.

The test has two halves: an **offline conversion** (AI transpile +
assemble, no upload/publish) and a **gameplay verification** (drive the
behavior fixtures in a Studio Play session). They run as one motion —
there is no separate trigger for the gameplay half.

## House rules

- **One verdict.** The whole run produces a single combined report and a
  single exit code. A conversion failure short-circuits the gameplay
  verification with exit 2 (the rbxlx never built; there's nothing to
  play).
- **Fresh Studio only.** Re-opening a regenerated rbxlx in an
  already-running Studio does NOT reload the in-memory DataModel (see
  `/Users/jiazou/.context` notes). The skill refuses to attach to a
  running Studio unless `--close-and-relaunch` is passed.
- **Never run on protected instances.** Refuse to send any work to a
  Studio whose `game.Name == "Agas Map of London"`.
- **No gameplay verification without a clean conversion.** The fixtures
  only run as part of an end-to-end test — there is no standalone
  fixture-runner entry point.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Conversion passed and all gameplay fixtures passed |
| 2 | Conversion (offline-assembly pytest) failed; gameplay verification skipped |
| 3 | Studio liveness/handshake failed (couldn't get a verified Studio in 180s) |
| 4 | Conversion passed but ≥1 gameplay fixture failed |

## Workflow

### Step 0: Preflight

Parse `<project>` from arguments (required). Parse `--close-and-relaunch`
and `--only <ids>` (optional). Validate the behavior fixtures file
exists and is well-formed:

```bash
python3 -m tests.studio_behavior_driver validate <project>
```

If exit non-zero, surface the error and stop. (No conversion run when
the test contract itself is broken.)

### Step 1: Generate run_id + output dir

```bash
RUN_ID="$(date -u +%Y-%m-%dT%H-%M-%S)-$(openssl rand -hex 3)"
OUTPUT_ROOT="output/e2e/${RUN_ID}"
CONV_DIR="${OUTPUT_ROOT}/conversion"
mkdir -p "${CONV_DIR}"
```

The run_id is the cross-context handshake: passed to the conversion
pytest via env, written into both the conversion manifest and the
combined report, read back via the Studio handshake to verify the
plugin is connected to the right place.

### Step 2: Studio safety — refuse to attach to a running instance

Call `mcp__Roblox_Studio__list_roblox_studios`. If it returns ≥1
candidate AND `--close-and-relaunch` was NOT passed: print
"Studio is already running; refusing to attach (re-opening a
regenerated rbxlx does not reload the DataModel). Pass
`--close-and-relaunch` to force a fresh process." and exit 3.

If `--close-and-relaunch` was passed: run

```bash
python3 -c "from roblox.studio_launcher import close_running_studio_or_fail; close_running_studio_or_fail()"
```

If that raises, surface the error and exit 3.

### Step 3: Offline AI conversion

```bash
E2E_OUTPUT_DIR="${CONV_DIR}" E2E_RUN_ID="${RUN_ID}" \
python3 -m pytest -m slow \
  tests/test_offline_assembly.py::TestOfflineAssembly::test_simplefps_assembly_with_cached_ids \
  -v --tb=short --no-header
```

(For projects other than SimpleFPS, swap the test method. V1 only ships
SimpleFPS; the test parameterization is task #6.)

If pytest exits non-zero: write the combined report with
`conversion.passed = false` + `gameplay = null`, print the summary line,
exit 2.

Otherwise read `${CONV_DIR}/conversion_manifest.json`:

```bash
cat "${CONV_DIR}/conversion_manifest.json"
```

Capture `rbxlx_path`, `duration_seconds`.

### Step 4: Launch Studio + 3-step readiness probe

```bash
python3 -c "
from roblox.studio_launcher import launch_studio, wait_for_studio_ready
import sys
p = launch_studio(rbxlx_path='${RBXLX_PATH}')
if not p:
    sys.exit('launch_studio returned None')
if not wait_for_studio_ready(timeout=120):
    sys.exit('Studio process never appeared within 120s')
print('STUDIO_PID', p.pid)
"
```

Capture the printed `STUDIO_PID <n>` into `STUDIO_PID` — Step 8 uses it to close
**only the editor this run launched**, leaving any concurrent Studios (and the
StudioMCP proxy) alone.

Then the 3-step readiness probe (Codex finding #2):

1. **Process up** — `wait_for_studio_ready` already returned true.
2. **MCP sees the place** — call `mcp__Roblox_Studio__list_roblox_studios`;
   if empty or the active instance is `Agas Map of London`, retry up to
   60s, then exit 3.
3. **Run-ID handshake** — call `mcp__Roblox_Studio__execute_luau` with:

```lua
workspace:SetAttribute("E2ERunId", "${RUN_ID}")
return workspace:GetAttribute("E2ERunId") == "${RUN_ID}"
```

If the return value is not `true`, exit 3. Total handshake budget is
180s including launch.

### Step 5: Walk gameplay fixtures via MCP

```bash
python3 -m tests.studio_behavior_driver emit-plan <project> [--only <ids>] > "${OUTPUT_ROOT}/fixtures_plan.json"
```

For each fixture in the emitted JSON array, in order:

1. **Safety check** — `execute_luau`: `assert(game.Name ~= "Agas Map of London"); return true`. If it raises, exit 3.
2. **Setup** — `execute_luau` with `setup_luau` (already preamble-prepended). Discard return value.
3. **Inputs** — for each entry in `input_sequence`. The driver has
   already translated each `action` into the MCP tool's vocabulary
   (keyboard: `keyDown`/`keyUp`/`keyPress` + `key_code`; mouse:
   `mouseButtonClick`/`moveTo` + `mouse_button`/`x`/`y`), so pass
   `action` straight through:
   - `type == "keyboard"`: `mcp__Roblox_Studio__user_keyboard_input` with `[action]`
   - `type == "mouse"`: `mcp__Roblox_Studio__user_mouse_input` with `[action]`
   - `type == "wait"`: sleep for `seconds`
4. **Settle** — sleep `wait_seconds` from the fixture.
5. **Assert** — `execute_luau` with `assert_luau` (wrapped, returns `{ok, value}`).
   - If `assert_timeout_seconds > 0`: poll, re-running the assert every `poll_interval_seconds` until `value` matches `expect` (with `tolerance` for numbers) or the timeout elapses.
   - Record `{passed, value, attempts, duration_seconds, error}` per fixture.
6. **Evidence on failure** — if `passed == false` and `evidence_on_fail` is non-empty:
   - `"screen_capture"` → `mcp__Roblox_Studio__screen_capture` → save under `${OUTPUT_ROOT}/evidence/<fixture_id>.png`
   - `"console_tail"` → `mcp__Roblox_Studio__get_console_output` → save under `${OUTPUT_ROOT}/evidence/<fixture_id>.console.txt`

The plan emitter has already done the preamble prepend + pcall wrap; the
skill never has to think about the Luau shape.

### Step 6: Write combined report

```
${OUTPUT_ROOT}/report.json
```

Shape:

```json
{
  "schema_version": 1,
  "project": "<project>",
  "run_id": "<run_id>",
  "rbxlx_path": "<absolute path>",
  "conversion": {
    "passed": true,
    "duration_seconds": <number>,
    "started_at": "...",
    "finished_at": "..."
  },
  "gameplay": {
    "summary": { "total": N, "passed": P, "failed": F },
    "fixtures": [
      { "id", "feature", "passed", "expected", "value",
        "started_at", "finished_at", "duration_seconds",
        "attempts", "error", "evidence": [...] }
    ]
  }
}
```

### Step 7: Print summary + exit

Stdout one-liner via `format_summary` (see `tests/studio_behavior_runner.py`):

```
[SimpleFPS]; Conversion passed (821.4s); 16/16 fixtures passed
```

or

```
[SimpleFPS]; Conversion passed (821.4s); 14/16 fixtures (failed: mouse_yaw_rotates_camera, rifle_visible_in_viewport)
```

Exit 0 if all passed; exit 4 if any gameplay fixture failed.

### Step 8: Teardown — close the Studio this run launched

Run this on every path that launched Studio (exit 0 and exit 4) so a run
never leaves an orphaned Studio process behind. (Skip it for exit 2 —
Studio never launched — and exit 3 — launch/handshake already failed.)

```bash
python3 -c "from roblox.studio_launcher import close_running_studio_or_fail; close_running_studio_or_fail(pid=${STUDIO_PID})"
```

Pass `pid=${STUDIO_PID}` (captured in Step 4) so teardown closes **only the
editor this run launched** — a concurrent Studio from another project is left
running. `close_running_studio_or_fail` escalates SIGTERM → SIGKILL, so a
lingering Play session or a "save changes?" dialog can't keep the process alive.
Teardown is **best-effort**: do it *after* writing the report and printing
the summary, and if it raises, log the error but keep the exit code from
Step 7 — the test verdict stands; a failed cleanup is an environment issue,
not a test result. This also satisfies the "Fresh Studio only" rule for the
next run by leaving no process to refuse-attach to.

## If you skipped a Read

The fixtures' `setup_luau` and `assert_luau` bodies are the ONLY thing
you should be sending to `execute_luau` — never inline your own Luau.
If you find yourself constructing Luau strings outside what
`emit-plan` returns, you skipped reading the contract. Re-read
`docs/E2E_INPUT_CHANNEL.md` and `tests/STUDIO_BEHAVIOR.md`.

## Configuration

- `output/e2e/` is gitignored — runs accumulate locally.
- Studio path: `roblox/studio_launcher.py` reads `STUDIO_PATH` from
  `config`. Set it in `converter/config.py` per platform.
- Behavior fixtures: `tests/fixtures/upload_snapshots/<project>.behavior.json`
- Run with `--only foo,bar` to test a subset of fixtures (useful when
  iterating on a single feature; full suite is the contract for nightly).
