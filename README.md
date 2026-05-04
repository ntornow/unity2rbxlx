# unity2rbxlx

Converts Unity game projects into Roblox `.rbxlx` place files. Uses Python to orchestrate the conversion pipeline and Claude Code for C# to Luau transpilation.

## What it does

- Parses Unity scenes (YAML and binary), prefabs, materials, and scripts
- Converts scene hierarchy, transforms, colliders, lights, audio, particles, and UI to Roblox equivalents
- Transpiles C# MonoBehaviours to Luau scripts (rule-based + AI via Claude CLI)
- Uploads textures, meshes, and audio to Roblox via Open Cloud API
- Outputs a `.rbxlx` file you can open directly in Roblox Studio

## Two ways to run it

unity2rbxlx exposes the same pipeline through two interfaces:

1. **`u2r.py` — non-interactive CLI.** Runs the entire pipeline end-to-end with no human in the loop. Use this for one-shot conversions, CI/CD, and batch jobs.
2. **`convert_interactive.py` — phase-by-phase CLI for the `/convert-unity` Claude Code skill.** Each pipeline phase becomes a Click subcommand that emits structured JSON, so a Claude Code session can pause for human review at any decision point (scene selection, material review, transpilation review, upload configuration, etc.).

Both entry points share the same `Pipeline` class, the same `ConversionContext` state container, and the same `conversion_context.json` on disk — so you can start a conversion interactively, then finish it non-interactively (or vice versa).

## Prerequisites

- **Python 3.11+**
- **Claude CLI** (`claude`) -- used for AI-assisted C# to Luau transpilation
- **Roblox Open Cloud API key** -- for asset uploads ([create one here](https://create.roblox.com/dashboard/credentials))
- **Roblox Studio** -- for opening converted files and resolving uploaded asset IDs

## Setup

```bash
# Clone with submodules (test projects)
git clone --recursive https://github.com/ntornow/unity2rbxlx.git
cd unity2rbxlx

# Install Python dependencies
cd converter
pip install -e ".[dev]"
```

### Credentials

The converter looks for credentials in three places (in order):

1. **CLI arguments**: `--api-key <value_or_path>` and `--creator-id <value_or_path>`
2. **Environment variables**: `ROBLOX_API_KEY` and `ROBLOX_CREATOR_ID`
3. **Auto-discovery files**: `apikey` and `creator_id` files in the project parent directory or current working directory

To set up credential files:

```bash
# In the repo root (these are gitignored)
echo "your-api-key-here" > apikey
echo "your-creator-id-here" > creator_id
```

## Usage

All commands run from the `converter/` directory:

```bash
cd converter

# Convert a Unity project (with asset upload)
python u2r.py convert ../test_projects/SimpleFPS -o ./output/SimpleFPS --api-key ../apikey

# Convert without uploading assets
python u2r.py convert ../test_projects/SimpleFPS -o ./output/SimpleFPS --no-upload

# Convert without AI transpilation (rule-based only, faster)
python u2r.py convert ../test_projects/SimpleFPS -o ./output/SimpleFPS --no-ai

# Convert all scenes in a project
python u2r.py convert ../test_projects/SimpleFPS -o ./output/SimpleFPS --scene all

# Resume from a specific pipeline phase
python u2r.py convert ../test_projects/SimpleFPS -o ./output/SimpleFPS --phase convert_scene

# Analyze a Unity project without converting
python u2r.py analyze ../test_projects/SimpleFPS

# Validate a generated .rbxlx file
python u2r.py validate ./output/SimpleFPS/converted_place.rbxlx

# Re-publish a previously converted place
python u2r.py publish ./output/SimpleFPS --universe-id <uid> --place-id <pid>
```

### Interactive (skill-driven) workflow

The `/convert-unity` Claude Code skill walks through the same pipeline one phase at a time, pausing for human input. Each subcommand of `convert_interactive.py` emits structured JSON to stdout that the skill consumes:

```bash
cd converter

# 0. Preflight — Python version, packages, Unity project validity
python convert_interactive.py preflight ../test_projects/SimpleFPS ./output/SimpleFPS --install

# 1. Discover — parse Unity scenes
python convert_interactive.py discover ../test_projects/SimpleFPS ./output/SimpleFPS

# 2. Inventory — build asset manifest + GUID index
python convert_interactive.py inventory ../test_projects/SimpleFPS ./output/SimpleFPS

# 3. Materials — Unity .mat → Roblox SurfaceAppearance
python convert_interactive.py materials ../test_projects/SimpleFPS ./output/SimpleFPS

# 4. Transpile — C# → Luau
python convert_interactive.py transpile ../test_projects/SimpleFPS ./output/SimpleFPS --api-key <anthropic_key>

# 4c. Validate — run luau-analyze over transpiled output
python convert_interactive.py validate ./output/SimpleFPS --write

# 5. Assemble — upload assets, resolve, convert scene, write .rbxlx
python convert_interactive.py assemble ../test_projects/SimpleFPS ./output/SimpleFPS \
    --api-key ../apikey --creator-id ../creator_id

# 6. Upload — headless place publish via Open Cloud execute_luau
python convert_interactive.py upload ./output/SimpleFPS \
    --api-key ../apikey --universe-id <uid> --place-id <pid>

# 7. Report — write conversion_report.json
python convert_interactive.py report ./output/SimpleFPS

# Status of an in-progress conversion
python convert_interactive.py status ./output/SimpleFPS
```

The skill itself lives at `converter/.claude/skills/convert-unity/SKILL.md` and is invoked from Claude Code via `/convert-unity`. The skill is responsible for:

- Asking the user about scene selection, material review choices, script review, scale strategy, etc.
- Presenting flagged scripts for review and accepting edits
- Step 4.5: collaborative game-logic porting (architecture map, Unity↔Roblox divergence analysis, module rewrite, bootstrap wiring)
- Pausing on every error for human decision (retry / skip / abort)

### Post-upload asset resolution

After uploading, mesh and texture IDs need to be resolved in Roblox Studio:

1. Open the generated `.rbxlx` in Studio
2. Run the resolution scripts generated by `u2r.py resolve` via the Studio command bar or MCP
3. This converts Model IDs to real MeshIds and Decal IDs to Image IDs
4. Regenerate the `.rbxlx` with the updated `conversion_context.json`

## Running tests

```bash
cd converter
python -m pytest tests/ -v
```

## Project structure

```
converter/
  u2r.py                          # Non-interactive CLI entry point
  convert_interactive.py          # Phase-by-phase CLI for the /convert-unity skill
  config.py                       # Global configuration
  core/                           # Typed pipeline state, coordinate system, types
  unity/                          # Unity project parsing (YAML, binary, prefabs, GUIDs)
  converter/                      # Pipeline + scene/material/script/animation conversion
  roblox/                         # rbxlx writer, cloud API, headless place builder
  runtime/                        # Luau runtime libraries injected into converted games
  comparison/                     # Visual/state comparison between Unity and Roblox versions
  tools/                          # Standalone utilities
  tests/                          # Test suite
  .claude/skills/convert-unity/   # /convert-unity skill (SKILL.md + references/)
test_projects/                    # Git submodules of sample Unity projects
```

## Coordinate system

- Unity: left-handed Y-up, Z-forward
- Roblox: right-handed Y-up
- Position: `(x, y, z)` Unity -> `(x, y, -z)` Roblox
- Quaternion: `(qx, qy, qz, qw)` Unity -> `(-qx, -qy, qz, qw)` Roblox

## Documentation

- [`converter/CLAUDE.md`](converter/CLAUDE.md) — engineering overview + upload semantics
- [`converter/ARCHITECTURE.md`](converter/ARCHITECTURE.md) — pipeline architecture + design decisions
- [`converter/docs/UNSUPPORTED.md`](converter/docs/UNSUPPORTED.md) — what the converter cannot do
- [`converter/docs/KNOWN_ISSUES.md`](converter/docs/KNOWN_ISSUES.md) — architectural debt and bug-shaped concerns
- [`converter/docs/FUTURE_IMPROVEMENTS.md`](converter/docs/FUTURE_IMPROVEMENTS.md) — long-horizon, multi-PR work
- [`converter/docs/design/inline-over-runtime-wrappers.md`](converter/docs/design/inline-over-runtime-wrappers.md) — key design decision (transpile-time API translation, no runtime wrappers)
- [`converter/TODO.md`](converter/TODO.md) — active PR-scoped work

## Limitations

- Skeletal animation supported for R15-mappable rigs (Motor6D + `animator_runtime.luau`); both text-YAML and binary `.anim` / `.controller` are parsed (binary requires the optional `UnityPy` dependency). See `converter/docs/UNSUPPORTED.md` for the full list.
- Custom shaders are approximated with Roblox materials
- Git LFS pointer files are detected and skipped (run `git lfs pull` first)
- VFX Graph is not converted (no node-graph primitive on Roblox); particle sub-emitters are auto-converted via `sub_emitter_runtime.luau` when `_HasSubEmitters` is detected
