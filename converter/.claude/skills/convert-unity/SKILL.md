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

### Step 4a: Plan — start with `references/phase-4a-overview.md`

Upfront architectural planning pass. Read the Unity project and emit `conversion_plan.json` with architecture map, divergence overrides, templates manifest, module boundaries, and **storage plan** (the 3 networking questions: server / client / replicated). The overview file routes to the five sub-phase files and defines the dependency DAG. No pipeline command yet — planning is currently agent-driven.

### Step 4b: Transpile with plan — `references/phase-4-transpilation.md` + `references/phase-4b-*.md`

```bash
python3 convert_interactive.py transpile <unity_project_path> <output_dir> --api-key <key> 2>/dev/null
python3 convert_interactive.py validate <output_dir> --write 2>/dev/null
```

Transpile reads `conversion_plan.json` from 4a. Universal coding rules live in `references/phase-4b-universal-rules.md`; migrated transpile-time rules in `references/phase-4b-transpile-rules.md`.

### Step 4c: Reactive fixups — start with `references/phase-4c-overview.md`

Bootstrap emission, spawner wiring, animator-on-clone bindings, and residual transpiler gaps. Intentionally small — anything reliably automatable should migrate leftward to 4b.

### Step 5: Moderate & Assemble — `references/phase-5-assembly.md`

Screen every asset against Roblox's published safety standards first. Only run `assemble` after moderation comes back clean (see the reference file for standards, screening rules, and halting behaviour).

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
