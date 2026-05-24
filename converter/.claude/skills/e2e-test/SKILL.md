---
name: e2e-test
description: End-to-end test for a converted Unity project. Delegates the full conversion (including agent-driven Steps 4a and 4c) to /convert-unity, then launches Studio with the produced rbxlx, drives the gameplay-feature fixtures via Studio MCP, and writes a combined pass/fail report. Upload step (6) is skipped — cached asset IDs from the project's upload snapshot are pre-seeded.
argument-hint: <project> [--close-and-relaunch] [--only fixture_ids] [--generic]
allowed-tools:
  - Bash(python3 -m tests.studio_behavior_driver *)
  - Bash(python3 -m tools.validate_e2e_conversion *)
  - Bash(python3 convert_interactive.py *)
  - Bash(python3 -c *)
  - Bash(mkdir *)
  - Bash(date *)
  - Bash(openssl rand *)
  - Bash(cat *)
  - Bash(realpath *)
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

The test has two halves: an **offline conversion** (delegated to
`/convert-unity`, which walks all phases 1–5 including the agent-driven
4a Plan and 4c Reactive fixups; phase 6 upload is skipped) and a
**gameplay verification** (drive the behavior fixtures in a Studio Play
session). They run as one motion — there is no separate trigger for the
gameplay half.

The conversion half is **not** a pytest harness. Earlier iterations
shortcut /convert-unity by calling `Pipeline.run_all()` directly through
`test_offline_assembly`; that silently skipped 4a (classifier overrides)
and 4c (residual transpiler gaps) because both are agent-driven and have
no pipeline command. The /e2e-test discipline now is: the same
conversion an operator would do interactively via /convert-unity is the
conversion this skill validates.

## House rules

- **One verdict.** The whole run produces a single combined report and a
  single exit code. A conversion failure short-circuits the gameplay
  verification with exit 2 (the rbxlx never built; there's nothing to
  play).
- **Delegate the conversion to /convert-unity.** Don't reach into
  pytest harnesses or call `Pipeline.run_all()` directly. The agent
  walking /e2e-test walks /convert-unity's phases inline (Step 3
  below). The only test-only carve-out is pre-seeding cached asset IDs
  so phase 6 upload is skipped.
- **Fresh Studio process for this run.** Re-opening a regenerated
  rbxlx in an already-running Studio does NOT reload the in-memory
  DataModel — so this run must own a *newly-launched* Studio process.
  Other unrelated Studios (different project, different rbxlx) can
  coexist: the skill snapshots pre-launch Studio IDs (Step 2), picks
  the new ID after launch and pins it via `set_active_studio` (Step 4),
  and teardown only kills its own PID (Step 8). Pass
  `--close-and-relaunch` if you want a clean slate (kill all editors
  before launching) — not required just because another Studio is
  open.
- **No gameplay verification without a clean conversion.** The fixtures
  only run as part of an end-to-end test — there is no standalone
  fixture-runner entry point.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Conversion passed and all gameplay fixtures passed |
| 2 | /convert-unity conversion failed (any phase 1–5); gameplay verification skipped |
| 3 | Studio liveness/handshake failed (couldn't get a verified Studio in 180s — includes "no new Studio appeared in `list_roblox_studios` after launch" and "E2ERunId handshake didn't echo back") |
| 4 | Conversion passed but ≥1 gameplay fixture failed |

## Workflow

### Step 0: Preflight

**Working directory.** Every later python invocation is relative to
the converter root (`convert_interactive.py` is there;
`tests/_project_paths.py` and `tools/validate_e2e_conversion.py`
are imported via paths anchored on `converter/`). Anchor the cwd
once at the top of Step 0 so the rest of the skill is invocation-
location-agnostic:

```bash
cd "$(git rev-parse --show-toplevel 2>/dev/null)/converter" || {
    echo "/e2e-test must run inside a unity2rbxlx git checkout with a 'converter/' subdirectory. cwd was: $(pwd)" >&2
    exit 64
}
```

(The `|| {…; exit 64;}` is load-bearing — without it a failed `cd`
silently leaves the agent in an arbitrary working directory, and
every later `python3 -c` / `python3 -m tools.…` invocation fails
with a cryptic `ModuleNotFoundError` instead of the clean
"must run inside a checkout" message.)

**Argument parsing.** Walk the argv as discrete tokens (a substring
glob like `*--generic*` would false-match `--generics`, `--no-generic-X`,
or a project name containing the literal `--generic`):

```bash
PROJECT=""
SCENE_RUNTIME_MODE="legacy"
CLOSE_AND_RELAUNCH=0
ONLY_FIXTURES=""
while (( "$#" )); do
    case "$1" in
        --generic)            SCENE_RUNTIME_MODE="generic" ; shift ;;
        --close-and-relaunch) CLOSE_AND_RELAUNCH=1         ; shift ;;
        --only)               [[ -z "${2:-}" ]] && { echo "--only requires a value (e.g. --only foo,bar)" >&2; exit 64; }
                              ONLY_FIXTURES="$2"           ; shift 2 ;;
        --*)                  echo "unknown flag: $1" >&2  ; exit 64 ;;
        *)                    if [[ -z "$PROJECT" ]]; then
                                  PROJECT="$1"; shift
                              else
                                  echo "extra positional: $1" >&2; exit 64
                              fi ;;
    esac
done
[[ -z "$PROJECT" ]] && { echo "missing <project> argument" >&2; exit 64; }
```

**Project path resolution.** Pass `PROJECT` via environment, not
shell-interpolation into the python heredoc (a project name with a
single quote would break parse; `';) ;import os; os.system("…")` would
execute):

```bash
UNITY_PROJECT_PATH="$(PROJECT="$PROJECT" python3 -c '
import os, sys
sys.path.insert(0, "tests")
from _project_paths import resolve_project, is_populated
p = resolve_project(os.environ["PROJECT"])
if not is_populated(p):
    sys.exit(f"resolve_project returned an unpopulated path: {p} -- check that "
             f"the snapshot or test_projects submodule exists for "
             f"{os.environ[\"PROJECT\"]!r}")
print(p)
')" || exit 1
```

`UNITY_PROJECT_PATH` is the resolved filesystem path /convert-unity's
phases take as their `<unity_project_path>` argument. The resolver
honours the snapshot's baked `source_unity_project`, the bundled
`test_projects/<project>` submodule, and `$UNITY2RBXLX_TEST_PROJECTS_ROOT`
in that order (see `tests/_project_paths.py:resolve_project`). The
`is_populated` check turns a silent submodule-uninitialised case into
a loud Step 0 failure rather than a downstream assemble crash.

**Behavior-fixture validation:**

```bash
python3 -m tests.studio_behavior_driver validate "${PROJECT}"
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

The run_id is the cross-context handshake: stamped into the conversion
manifest and the combined report, set as a `workspace` attribute via
the Studio handshake in Step 4, and read back to verify the plugin is
connected to the place this run produced (not a stale Studio left from
another run).

### Step 2: Pre-launch — snapshot existing Studio IDs (no refusal)

Call `mcp__Roblox_Studio__list_roblox_studios` and capture the set of
existing studio IDs into `PRE_LAUNCH_STUDIO_IDS`. This is how Step 4
identifies "the Studio this run launched" — any id present in the
post-launch list but NOT in `PRE_LAUNCH_STUDIO_IDS` is ours. The
skill does NOT refuse to run because other Studios exist; both the
launch (separate OS process) and the teardown (PID-scoped, Step 8)
coexist with them. Step 4 then pins routing via `set_active_studio`
on the new id, so unrelated Studios are not at risk of receiving
this run's MCP calls.

If `--close-and-relaunch` was passed: kill all running editors first
as a clean-slate option (not required for multi-Studio coexistence —
use this when you suspect a stale process is interfering):

```bash
python3 -c "from roblox.studio_launcher import close_running_studio_or_fail; close_running_studio_or_fail()"
```

If that raises, surface the error and exit 3. After the close, re-run
the snapshot above so `PRE_LAUNCH_STUDIO_IDS` reflects the
post-cleanup state (likely empty).

### Step 3: Full conversion via `/convert-unity`

This skill is the playback-verification harness. The conversion itself
is **owned by `/convert-unity`** — invoke that skill, let it walk every
phase (including the agent-driven 4a Plan and 4c Reactive fixups), and
capture the produced rbxlx. No test-only shortcut around the agent work
that `/convert-unity` is the canonical home for.

**Why:** offline pytest harnesses (`test_offline_assembly`) bypass 4a/4c
by going direct to `Pipeline.run_all()`. That makes them fast but
gives false confidence — classifier misclassifications and residual
transpiler gaps the agent would catch in /convert-unity slip through.
Calling /convert-unity here keeps /e2e-test as the high-fidelity
end-to-end test it claims to be.

#### What to invoke

Invoke the `/convert-unity` skill with the project's Unity path and
`${CONV_DIR}` as the output dir. As the agent driving /e2e-test, walk
/convert-unity's phases inline:

1. **Phases 1–3** (discover / inventory / materials): run the
   `convert_interactive.py` commands from /convert-unity's SKILL.md
   verbatim.
2. **Phase 4a (Plan)**: agent-driven. Read
   `references/phase-4a-*.md` and emit `conversion_plan.json` per the
   sub-phase walk. Decide autonomously based on each file's Factors
   block; the design doc rule applies — escalate only on genuine
   ambiguity.
3. **Phase 4b (Transpile)**: run `convert_interactive.py transpile`,
   then `convert_interactive.py validate` (the validator only reads
   scripts and reports diagnostics — it does not rewrite files).
4. **Phase 4c (Reactive fixups)**: agent-driven. Read
   `references/phase-4c-*.md` and apply project-specific post-transpile
   patches.
5. **Phase 5 (Assemble)**: pre-seed `${CONV_DIR}/conversion_context.json`
   and `${CONV_DIR}/.roblox_ids.json` from
   `tests/fixtures/upload_snapshots/<project>.snapshot.json` so any
   asset whose ID is already in the snapshot is treated as
   already-uploaded. Pass both inputs via env (a `<project>` arg
   containing a single quote would break a shell-interpolated
   heredoc):
   ```bash
   CONV_DIR="$CONV_DIR" PROJECT="$PROJECT" python3 -c '
   import os, sys
   sys.path.insert(0, "tests")
   from conversion_assertions import seed_output_dir, load_snapshot
   from pathlib import Path
   seed_output_dir(Path(os.environ["CONV_DIR"]), load_snapshot(os.environ["PROJECT"]))
   '
   ```
   Then run `assemble` with `--no-upload`. Template the
   `--scene-runtime` flag from the mode bound in Step 0 — a missing
   flag silently defaults to legacy and the Step 3.5 validator's
   `--mode generic` would trip a false failure:
   ```bash
   python3 convert_interactive.py assemble "${UNITY_PROJECT_PATH}" "${CONV_DIR}" \
     --no-upload \
     --scene-runtime="${SCENE_RUNTIME_MODE}" 2>/dev/null
   ```
   The `--no-upload` is **mandatory** for /e2e-test. Without it,
   `assemble`'s `force_rerun` set re-runs `upload_assets` every
   invocation (`convert_interactive.py:919`); the per-path dedup
   against `ctx.uploaded_assets` only suppresses uploads for paths
   already in the snapshot, so any NEW asset the project has gained
   since the snapshot would hit Open Cloud. With `--no-upload`,
   `Pipeline.skip_upload=True` short-circuits `upload_assets` (and
   `resolve_assets`) before per-path dedup runs — guarantees the
   "no upload during /e2e-test" contract regardless of snapshot age
   or new-asset shape. `assemble` is non-interactive; do not pass
   `--universe-id` / `--place-id` (they're only needed when uploads
   actually run, which they don't here).
6. **Phase 6 (Upload)**: **SKIP** — this is a test, no Open Cloud
   round-trip. Done after assemble.

The agent owns each /convert-unity decision (autonomous per
/convert-unity's "agent decides" rule). The only places /e2e-test
should pre-decide for /convert-unity are:
  - "scene-runtime mode" (pass through to /convert-unity's decisions:
    `--generic` here ↔ generic-mode emission in /convert-unity's
    Step 4 planning + transpile flags).
  - "skip Step 6 upload" (the test contract — never upload during
    /e2e-test).

**Scene-runtime mode.** When `--generic` was passed to /e2e-test,
ensure /convert-unity's `transpile` and `assemble` phases run with
`--scene-runtime=generic` (and the planning in 4a accounts for the
generic ModuleScript + host contract). Default is legacy.

#### Outcome capture

After /convert-unity finishes, `${CONV_DIR}/converted_place.rbxlx`
exists. Capture its absolute path into `RBXLX_PATH`:

```bash
RBXLX_PATH="$(realpath "${CONV_DIR}/converted_place.rbxlx")"
```

If the rbxlx is missing (any phase failed): write the combined report
with `conversion.passed = false` + `gameplay = null`, print the
summary line, exit 2.

Also write a `${CONV_DIR}/conversion_manifest.json` for the report
schema — same shape as before, the agent fills it in based on the
/convert-unity run's start/end timestamps and the rbxlx path.

### Step 3.5: Post-conversion artifact validation

The conversion is over; the rbxlx exists. Before handing off to Studio,
re-run the artifact-level assertions the old pytest harness enforced.
Without this, a broken conversion (asset resolution gap, mesh ID
mismatch, scene-runtime contract incomplete, transpile syntax error)
slips silently into the gameplay half — only surfacing if a fixture
happens to touch the defect.

```bash
python3 -m tools.validate_e2e_conversion "${CONV_DIR}" "${PROJECT}" \
  --mode "${SCENE_RUNTIME_MODE}"
```

`SCENE_RUNTIME_MODE` is `generic` when /e2e-test was invoked with
`--generic`, `legacy` otherwise. The validator covers:

  * No `rbxassetid://0` placeholders in the rbxlx.
  * Mesh IDs in the rbxlx match the snapshot
    (`tests/fixtures/upload_snapshots/<project>.snapshot.json`).
  * Generic-mode runtime contract embeds `scene_prefab_placements` +
    `_constructPrefabClone` (skipped under legacy).
  * `luau-analyze` clean across `${CONV_DIR}/scripts/` (soft-skipped
    when `luau-analyze` is not installed).

Not yet covered (need Pipeline in-memory state that isn't persisted
after `run_all`): snapshot drift gate (asset_manifest vs snapshot)
and place-builder chunk publishability. Both are deferred and
documented in `tools/validate_e2e_conversion.py`.

If the validator exits non-zero: write the combined report with
`conversion.passed = false` + `gameplay = null`, print the validator's
error output as the conversion failure reason, exit 2. The Studio
playback half **does not run** when validation fails — there is no
point exercising fixtures against a broken rbxlx.

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

Then the 4-step readiness probe (multi-Studio aware):

1. **Process up** — `wait_for_studio_ready` already returned true.
2. **New Studio visible to MCP** — poll
   `mcp__Roblox_Studio__list_roblox_studios` until a Studio with
   `id ∉ PRE_LAUNCH_STUDIO_IDS` appears (timeout: 60s). If multiple
   new IDs appear (rare — only if another process launched a Studio
   during our window), prefer the one whose `name` matches the
   basename of `${RBXLX_PATH}` (e.g. `converted_place.rbxlx`); if
   that tiebreak is ambiguous, exit 3. If none appears, exit 3.
3. **Pin the target** — call `mcp__Roblox_Studio__set_active_studio`
   with the new id. From this point on, every MCP call routes to
   the Studio this run launched, regardless of how many other
   Studios are open.
4. **Run-ID handshake** — call `mcp__Roblox_Studio__execute_luau`
   with:

```lua
workspace:SetAttribute("E2ERunId", "${RUN_ID}")
return workspace:GetAttribute("E2ERunId") == "${RUN_ID}"
```

If the return value is not `true`, the active Studio is not ours
(or `set_active_studio` didn't stick) — exit 3. The handshake is the
authoritative "we're talking to the right place" check; the new-id
diff is just how we find the candidate. Total handshake budget is
180s including launch.

### Step 5: Walk gameplay fixtures via MCP

```bash
python3 -m tests.studio_behavior_driver emit-plan <project> [--only <ids>] > "${OUTPUT_ROOT}/fixtures_plan.json"
```

For each fixture in the emitted JSON array, in order:

1. **Setup** — `execute_luau` with `setup_luau` (already preamble-prepended). Discard return value.
2. **Inputs** — for each entry in `input_sequence`. The driver has
   already translated each `action` into the MCP tool's vocabulary
   (keyboard: `keyDown`/`keyUp`/`keyPress` + `key_code`; mouse:
   `mouseButtonClick`/`moveTo` + `mouse_button`/`x`/`y`), so pass
   `action` straight through:
   - `type == "keyboard"`: `mcp__Roblox_Studio__user_keyboard_input` with `[action]`
   - `type == "mouse"`: `mcp__Roblox_Studio__user_mouse_input` with `[action]`
   - `type == "wait"`: sleep for `seconds`
3. **Settle** — sleep `wait_seconds` from the fixture.
4. **Assert** — `execute_luau` with `assert_luau` (wrapped, returns `{ok, value}`).
   - If `assert_timeout_seconds > 0`: poll, re-running the assert every `poll_interval_seconds` until `value` matches `expect` (with `tolerance` for numbers) or the timeout elapses.
   - Record `{passed, value, attempts, duration_seconds, error}` per fixture.
5. **Evidence on failure** — if `passed == false` and `evidence_on_fail` is non-empty:
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
