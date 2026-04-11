---
name: convert-unity
description: Convert a Unity game project into a Roblox place file interactively, with decision points at each phase
argument-hint: <unity_project_path> <output_dir>
allowed-tools:
  - Bash(python3 convert_interactive.py *)
  - Bash(python -m pytest *)
  - Read
---

# Convert Unity Project to Roblox

Interactive, phase-based conversion of a Unity project into a Roblox place (`.rbxlx`). Each phase command emits structured JSON; redirect stderr (`2>/dev/null`) to keep output clean. For non-interactive end-to-end conversion, use `python3 u2r.py convert <project> -o <out>` instead.

## House rules

- **Read before each phase.** Detailed rules live in `references/`. Load the referenced file(s) before running the phase command. If you're guessing at a rule, you skipped a Read. See `references/INDEX.md` for the full file list.
- **The agent decides.** Each reference file names Question / Factors / Options / Escape hatch at every decision point. Decide autonomously based on the factors; escalate only when genuinely ambiguous.
- **Bug fix protocol.** Fix both the pipeline (`converter/`, `unity/`, `roblox/`, `runtime/`) and the affected scripts in `<output_dir>/scripts/` — one without the other regresses or stays broken.
- **Never retry auth/credit failures.** `auth_failure`, `insufficient_credits`, `place_not_published` are terminal — surface to the user immediately. Retrying burns money or rate-limit budget without progress.

## Workflow

### Step 0: Preflight

Parse `<unity_project_path>` and `<output_dir>` from arguments, or ask. Ask whether AI-assisted transpilation is wanted (needs an Anthropic API key).

```bash
python3 convert_interactive.py preflight <unity_project_path> <output_dir> --install 2>/dev/null
```

Resuming an existing conversion:

```bash
python3 convert_interactive.py status <output_dir> 2>/dev/null
```

### Step 1: Discovery — `references/phase-1-discovery.md`

```bash
python3 convert_interactive.py discover <unity_project_path> <output_dir> 2>/dev/null
```

### Step 2: Asset Inventory — `references/phase-2-inventory.md`

```bash
python3 convert_interactive.py inventory <unity_project_path> <output_dir> 2>/dev/null
```

### Step 3: Material Mapping — `references/phase-3-materials.md`

```bash
python3 convert_interactive.py materials <unity_project_path> <output_dir> 2>/dev/null
```

### Step 4: Code Transpilation — `references/phase-4-transpilation.md`

```bash
python3 convert_interactive.py transpile <unity_project_path> <output_dir> --api-key <key> 2>/dev/null
python3 convert_interactive.py validate <output_dir> --write 2>/dev/null
```

### Step 4.5: Game Logic Porting — start with `references/phase-4.5-overview.md`

The transpiler converts files independently; 4.5 is the cross-file architectural pass. Mirror Unity's component separation, state machine, and timing model — **faithful port over workarounds**. The overview file routes you to the seven sub-phase files and defines the dependency DAG.

### Step 5: Assembly — `references/phase-5-assembly.md`

```bash
python3 convert_interactive.py assemble <unity_project_path> <output_dir> \
  --api-key ../apikey --creator-id ../creator_id 2>/dev/null
```

### Step 6: Upload & Publish — `references/phase-6-upload.md`

```bash
python3 convert_interactive.py upload <output_dir> \
  --api-key ../apikey --universe-id <uid> --place-id <pid> 2>/dev/null
```

### Step 7: Final Report

```bash
python3 convert_interactive.py report <output_dir> 2>/dev/null
```

## If you skipped a Read

If a phase is failing and you're guessing at a rule, you skipped a Read. Load the relevant phase file before continuing. The rules in `references/` are there because they caused past failures — they are load-bearing, not optional.
