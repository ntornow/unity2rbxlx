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

Interactive, phase-based conversion of a Unity game project into a Roblox place (`.rbxlx`). The agent pauses at each decision point and decides autonomously based on documented factors. All commands output structured JSON to stdout; redirect stderr (`2>/dev/null`) to keep output clean.

For non-interactive end-to-end conversion, prefer `python3 u2r.py convert <project> -o <out>`. This skill is for cases where the conversion needs human review at one or more phases.

## How to use this skill

**This file is a thin orchestrator.** Detailed rules live in `references/`. At the start of each phase, you **MUST** `Read` the referenced file(s) before running the phase command. Do not skip Reads — the one-line teasers here are navigation aids, not substitutes.

If you find yourself guessing at a rule, you skipped a Read. Stop and load the relevant phase file.

## Universal rules (apply to every phase)

These are the rules most likely to cause silent failures if forgotten. Full detail in `references/phase-4.5-universal-rules.md`.

- **No renderer = invisible.** Every converted Part without a renderer must have `Transparency=1`. Roblox renders gray blocks by default; Unity does not.
- **Heartbeat wiring.** A Luau method named `Update` does nothing unless you connect it via `RunService.Heartbeat`.
- **Signal callbacks cannot yield.** `task.wait()` inside `Heartbeat:Connect` / `Touched:Connect` silently stops execution. Wrap yielding bodies in `task.spawn`.
- **Part size cap.** Roblox Parts silently fail to render above 2048 studs on any axis. Clamp or tile.
- **ScreenGui placement.** Always place converted ScreenGuis in `ReplicatedStorage` with `Enabled=false`. Never in `StarterGui`.
- **Faithful port over workarounds.** If Unity generates content at runtime, the Roblox port must too. Port the system, not the symptom.
- **Runtime modules (`runtime/`) are reusable.** Never modify them for one game. Game-specific scripts live in `<output_dir>/scripts/`.
- **The agent decides.** Each decision point names Question / Factors / Options / Escape hatch. Decide autonomously based on the factors; escalate only when genuinely ambiguous.

## Reference files

| File | When to read | Topic |
|---|---|---|
| `references/INDEX.md` | Before first use | File inventory and house rules |
| `references/phase-1-discovery.md` | Step 1 | Scene selection, parse errors |
| `references/phase-2-inventory.md` | Step 2 | Duplicate GUIDs, orphaned assets |
| `references/phase-3-materials.md` | Step 3 | Material mapping, SurfaceAppearance, split meshes |
| `references/phase-4-transpilation.md` | Step 4 | Transpile errors, per-script review |
| `references/phase-4.5-overview.md` | Start of 4.5 | Router + dependency DAG for 4.5 sub-files |
| `references/phase-4.5-architecture-map.md` | 4.5a | State machines, ownership, timing |
| `references/phase-4.5-divergence-and-scale.md` | 4.5b, 4.5c | Character/camera/input overrides, scale, mesh facing |
| `references/phase-4.5-universal-rules.md` | 4.5d, 4.5e | Game loop, yielding, visibility, assets, ScreenGui |
| `references/phase-4.5-animation.md` | 4.5g | Transform animation, root motion, particles |
| `references/phase-4.5-runtime-content.md` | 4.5f, 4.5h | Runtime spawning, templates, movement |
| `references/phase-4.5-transpiler-gaps.md` | 4.5h | Semantic gaps where transpiled Luau silently breaks |
| `references/phase-4.5-module-rewrite.md` | 4.5h, 4.5i | Module-per-component rules, bootstrap wiring |
| `references/phase-5-assembly.md` | Step 5 | Asset upload, mesh resolution, terrain, LFS |
| `references/phase-6-upload.md` | Step 6 | Headless place publish via Open Cloud |
| `references/upload-patching.md` | Step 6 detail | Asset ID patching internals + Strategy A/B |

## Workflow

### Step 0: Gather Inputs & Preflight

Parse `<unity_project_path>` and `<output_dir>` from arguments, or ask the user. Also ask whether they want AI-assisted transpilation (needs an Anthropic API key).

```bash
python3 convert_interactive.py preflight <unity_project_path> <output_dir> --install 2>/dev/null
```

If resuming an existing conversion:

```bash
python3 convert_interactive.py status <output_dir> 2>/dev/null
```

### Step 1: Discovery

**Read `references/phase-1-discovery.md`** — covers scene selection and parse-error decisions.

Teaser: pick the primary gameplay scene by size + dependency count + name signals; abort only on high parse-error rate in the primary scene.

```bash
python3 convert_interactive.py discover <unity_project_path> <output_dir> 2>/dev/null
```

### Step 2: Asset Inventory

**Read `references/phase-2-inventory.md`** — covers duplicate GUIDs and orphaned assets.

Teaser: keep the referenced copy of duplicate GUIDs; skip orphans unless the project uses Resources/Addressables.

```bash
python3 convert_interactive.py inventory <unity_project_path> <output_dir> 2>/dev/null
```

### Step 3: Material Mapping

**Read `references/phase-3-materials.md`** — covers partial materials, SurfaceAppearance rules, opaque-mode alpha bug, split meshes.

Teaser: SurfaceAppearance without a ColorMap makes parts white; only create it when `mapping.color_map_path` is present.

```bash
python3 convert_interactive.py materials <unity_project_path> <output_dir> 2>/dev/null
```

### Step 4: Code Transpilation

**Read `references/phase-4-transpilation.md`** — covers structured error handling and per-script review.

Teaser: `insufficient_credits` / `auth_failure` are terminal — do NOT retry. For flagged scripts, decide Accept / Retry / Edit / Skip based on script criticality and diff size.

```bash
python3 convert_interactive.py transpile <unity_project_path> <output_dir> --api-key <key> 2>/dev/null
python3 convert_interactive.py validate <output_dir> --write 2>/dev/null
```

### Step 4.5: Game Logic Porting

**Read `references/phase-4.5-overview.md` first** — it routes you to the seven sub-phase files and defines the dependency DAG.

Teaser: the transpiler converts files independently; 4.5 provides cross-file architectural awareness. Do NOT flatten the game into a monolithic script — mirror the Unity component separation, state machine, and timing model. **Faithful port over workarounds.**

Read order: overview → architecture-map → divergence-and-scale → universal-rules → animation → runtime-content → transpiler-gaps → module-rewrite.

### Step 5: Assembly

**Read `references/phase-5-assembly.md`** — covers asset upload, mesh resolution, terrain handling, and LFS.

Teaser: LFS pointer files block terrain extraction — tell the user to `git lfs pull`. The assembly phase uploads textures/meshes/audio first, then resolves real MeshIds via the headless mesh resolver, then writes the `.rbxlx` with `rbxassetid://` URLs already embedded.

```bash
python3 convert_interactive.py assemble <unity_project_path> <output_dir> \
  --api-key ../apikey --creator-id ../creator_id 2>/dev/null
```

### Step 6: Upload & Publish

**Read `references/phase-6-upload.md`** — covers the headless place builder and `execute_luau` flow. Read `references/upload-patching.md` only if something goes wrong at the asset-ID patching step.

Teaser: u2r publishes the place headlessly via the Open Cloud Luau Execution API — `roblox/luau_place_builder.py` generates a Luau script that calls `CreateMeshPartAsync` + `SavePlaceAsync` server-side. No Studio interaction required for the common case.

```bash
python3 convert_interactive.py upload <output_dir> \
  --api-key ../apikey --universe-id <uid> --place-id <pid> 2>/dev/null
```

### Step 7: Final Report

```bash
python3 convert_interactive.py report <output_dir> 2>/dev/null
```

## Error handling

If any phase fails, surface the error and decide how to proceed based on the factors in the relevant reference file. Never silently swallow errors. Never retry auth/credit failures.

## Bug fix protocol

When a problem is found in converted output, always fix **both**:

1. **Fix the pipeline first** — update `converter/`, `unity/`, `roblox/`, or `runtime/` so future conversions produce correct output.
2. **Then fix the current output** — update the affected scripts in `<output_dir>/scripts/` so the already-converted game works.

A fix only to the output regresses on the next conversion. A fix only to the pipeline leaves the current game broken.

## Guidelines

- Be concise in summaries; thorough at decision points.
- Use fenced code blocks with language tags for C# or Luau.
- For large lists, summarize counts first, then drill into specifics on request.
- Remember earlier decisions — don't re-ask within a single run.

## If you skipped a Read

If a phase is failing and you're guessing at a rule, you skipped a Read. Load the relevant phase file before continuing. The rules in `references/` are there because they caused past failures — they are load-bearing, not optional.
