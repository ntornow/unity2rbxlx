# Architecture Critique — AI-Effectiveness Lens

Date: 2026-05-21
Branch: `arch-critique`
Reviewer: Claude Opus 4.7, with independent second opinion from Codex (GPT-5, high reasoning).

> **⚠️ Numbers below are 2026-05-21 and STALE — the conclusions still hold (more so).** Current `main` (2026-06-04): `pipeline.py` **6495** LOC / ~**65** methods (not 3897 / 49), `scene_converter.py` **5553** (not 4856), `script_coherence_packs.py` **5373** / **30** packs (not 4667), `_ctx()` **58** sites (not 50), suite ~**1340+** tests. Two clean modules landed since (`scene_runtime_topology/`, `contract_verifier.py`) — not split targets. The mega-files grew, so the refactor is *more* urgent. Current figures + the live plan: `refactor_plan.md` (2026-06-04 banner) and `docs/design/scene-runtime-and-refactor-execution.md`.

This is not a human-ergonomics review. The lens is: **does the shape of this codebase let an AI agent edit it reliably?** That question has a specific, measurable answer because the cost function is well-defined — context tokens consumed per useful edit, attention budget used per file, cache hits per session.

---

## TL;DR

The converter has the classic "AI-hostile" shape: a handful of multi-thousand-line files concentrate most of the logic, share mutable module-global state, and combine orchestration with implementation. The project's own `convert-unity` skill is a model of how to do progressive disclosure right — the production code does not follow the same discipline. Fix three files and trim one auto-loaded markdown and the codebase becomes meaningfully easier for AI agents to work in.

The user's *global* skill collection (`~/.claude/skills/gstack/*`) is a separate, larger problem: 2500-line skill bodies, duplicated 7× across plugin-vendor subdirectories.

---

## The data

Top Python files by line count, with structural notes:

| Lines | File | Anti-pattern |
|-------|------|---|
| 4856  | `converter/converter/scene_converter.py` | 44 top-level defs; module-global `_current_ctx` accessed via `_ctx()` 50× |
| 4667  | `converter/converter/script_coherence_packs.py` | 84 top-level functions, 24 `@patch_pack` registrations |
| 3897  | `converter/converter/pipeline.py` | One class with **49 methods**, ~3700 lines |
| 3726  | `converter/tests/test_script_coherence_packs.py` | 24 test classes — mirrors the production sprawl |
| 3517  | `converter/tests/test_animation_converter.py` | 15 test classes |
| 2082  | `converter/converter/animation_converter.py` | |
| 1986  | `converter/converter/component_converter.py` | |
| 1923  | `converter/u2r.py` | CLI in a single module |
| 1762  | `converter/roblox/rbxlx_writer.py` | |
| 1644  | `converter/converter/code_transpiler.py` | |

Concentration: **51% of all 77,621 Python lines live in the 18 files ≥1000 lines.** Five files cross 3000 lines. The repo is not "lots of small modules" — it's "a few mega-modules and a long tail."

Worst single functions:
- `scene_converter._process_components` — **591 lines** in one function
- `scene_converter._convert_prefab_instance` — 547 lines
- `scene_converter._convert_node` — 336 lines
- `Pipeline.resolve_assets` — 272 lines
- `Pipeline._subphase_inject_autogen_scripts` — 264 lines

---

## How file size actually hurts AI agents

It is tempting to say "AI handles 5000 lines fine, this is a human-readability issue." That's wrong. The mechanisms are:

**(1) Attention degradation, not retrieval failure.** `rg` finds `_process_components` in any size file. The problem starts after the file is loaded: needle-in-haystack accuracy and multi-hop reasoning across distant code regions drop measurably past ~50–70K context tokens on every frontier model. `scene_converter.py` alone is ~25K tokens; loading it plus `pipeline.py` (~22K) plus their tests puts a session over 70K before any conversation, tool results, or thinking.

**(2) Hidden mutable state compounds size.** `scene_converter.py` keeps a module-global `_current_ctx: SceneConversionContext` and 50 helper sites call `_ctx()` to fish state out of it (`scene_converter.py:185-200`). The file itself acknowledges this is debt — the refactor to pass `ctx` explicitly was deferred to "minimize diff" (`scene_converter.py:161-165`). Hidden state means an AI cannot edit a 100-line helper in isolation: it has to keep the whole file in working memory to know what `_ctx()` is at any callsite. **5000 lines of pure functions is much easier to edit than 5000 lines of pure functions plus one shared global.**

**(3) Hot files thrash the prompt cache.** The Anthropic prompt cache has a 5-minute TTL. A coherence-pack edit session that touches `script_coherence_packs.py`, runs tests, re-reads after a fix, re-runs tests, and re-reads again pays the 4667-line read cost three or four times. Splitting the file by theme (`packs/fps.py`, `packs/doors.py`, `packs/pickups.py`) means each iteration loads only the relevant ~500 lines.

**(4) Orchestration + implementation in one class blocks parallel work.** `class Pipeline` (`pipeline.py:110`) has 49 methods spanning end-to-end flow control, every phase implementation, every disk-I/O subphase, report generation, and back-compat migration logic. An agent cannot work on `write_output` without paging in `resolve_assets`, cannot edit `transpile_scripts` without `_subphase_inject_autogen_scripts`. Two agents cannot work on different phases without conflicting on the same file. The five biggest methods alone account for ~1000 lines inside one class definition.

The combination "**large file × hidden state × many responsibilities × hot edit path**" is what kills AI productivity. This repo has all four together in three files.

---

## Auto-loaded context tax

Every session starts with these loaded before the model sees the user's question:

- `converter/CLAUDE.md` — 322 lines / 2939 words / ~4K tokens
- `converter/ARCHITECTURE.md` — 178 lines / ~2K tokens
- `~/CLAUDE.md` — 60 lines / ~700 tokens
- `MEMORY.md` index + skill descriptions + tool schemas

`CLAUDE.md` is bloated with historical narrative — "Recent session (2026-04-11/12)", "Development History (2026-03-24 through 2026-03-28)", "Full upload test (2026-03-25)" — that is preserved in git anyway. About a third of the file is "what we shipped last session" content. That's a permanent ~1.5K-token tax on every conversation that adds no operational signal.

The durable rules (Bug fix protocol, Upload semantics, Coordinate System, Test Projects) are excellent and should stay. Archive the rest.

---

## Skills

### Project skill (`convert-unity`) — this is the right model

- `SKILL.md` is 100 lines.
- 22 reference files in `references/`, each 40–150 lines, with `INDEX.md`.
- Each phase's instructions say *"Read this reference before running the phase"* — progressive disclosure done correctly.
- Total content is 1619 lines, but no single load exceeds ~250 lines.

This is exactly the pattern the rest of the codebase should be measured against.

### Global skills (`~/.claude/skills/gstack/*`) — the problem

- `gstack/ship/SKILL.md`: **2543 lines** (~12K tokens loaded on `/ship`).
- `plan-ceo-review/SKILL.md`: 1837 lines.
- `plan-devex-review/SKILL.md`: 1833 lines.
- Same SKILL.md duplicated 7× across `.factory/`, `.slate/`, `.opencode/`, `.kiro/`, `.cursor/`, `.openclaw/`, `.agents/` — that one decision blows the skills tree up to **333,490 markdown lines** total.

Skill bodies are loaded on invocation, not eagerly, so the cost is paid per-use rather than per-session. But when you actually invoke `/ship`, you front-load 12K tokens of policy before any repo-specific reasoning starts. For comparison, `convert-unity`'s SKILL.md is ~25× smaller and works fine.

The duplicated trees are also discovery noise — anything that greps `~/.claude/skills` for patterns hits the same content seven times.

---

## Concrete recommendations, ranked by AI-productivity impact

**1. Split `scene_converter.py`** *(highest impact)*

- Extract `_process_components` (591 lines) into `scene_converter/components.py`.
- Extract `_convert_prefab_instance` / `_convert_fbx_prefab_instance` / `_convert_prefab_node` into `scene_converter/prefab.py`.
- Extract `_compute_mesh_*` / `_get_fbx_*` helpers into `scene_converter/mesh_sizing.py`.
- **Eliminate `_ctx()`** by passing `SceneConversionContext` explicitly. The deferred refactor at `scene_converter.py:161-165` should land *now* — it's the single biggest hidden-state liability in the codebase. 50 callsites is a one-day mechanical change.

**2. Split `Pipeline` into orchestration + phase modules**

- Keep `Pipeline` as a thin coordinator: 50–100 lines.
- Move each phase to its own module that takes `ConversionContext` in and returns mutations or a result struct: `phases/parse.py`, `phases/transpile.py`, `phases/convert_scene.py`, `phases/write_output.py`.
- The `write_output` region (`pipeline.py:1986–2385`) is the most cross-cutting and should split first — `_subphase_emit_scripts_to_disk`, `_subphase_cohere_scripts`, `_subphase_inject_autogen_scripts`, `_subphase_inject_mesh_loader`, `_subphase_finalize_scripts_to_disk` are already named like modules.

**3. Split `script_coherence_packs.py` by theme**

- `coherence/registry.py` — `PatchPack`, `patch_pack`, `run_packs`, `_topological_order`.
- `coherence/packs/fps.py` — weapon mount, default controls, camera pitch, bullet physics (~6 packs).
- `coherence/packs/doors.py` — global player lookup, AI rotation strip, tween open, module player attr.
- `coherence/packs/pickups.py` — remote event conversion, visual target, listener fanout.
- `coherence/packs/proximity.py` — trigger stay polling, proximity fanout.
- `coherence/packs/misc.py` — template clone, LocalScript API shim, BindableEvent guard.
- Split `test_script_coherence_packs.py` to match (each test class moves with its pack).

**4. Trim `converter/CLAUDE.md`**

Keep: Bug fix protocol, Upload semantics, Coordinate System, Test Projects, CLI Commands, Mesh Sizing, Asset Resolution, Inline-over-runtime principle.
Cut: Autonomous Work Plan (already done), Recent Session blocks, Development History, Full Upload Test snapshot. These belong in `TODO_archive.md`. Target: drop from 322 lines to ~150.

**5. Skills hygiene (global, lower priority)**

Convert `gstack/ship`, `plan-ceo-review`, `plan-devex-review` from monolithic SKILL.md to the `convert-unity` pattern (lean SKILL.md + numbered references/). Delete the duplicate `.factory`/`.slate`/`.opencode`/etc. trees or symlink them — 6 copies of 2500-line files don't help anyone.

---

## Codex's independent take (verbatim summary)

I asked Codex (GPT-5, high reasoning) the same questions in parallel. The points where we converged:

- **Same three files flagged first**, in the same order: scene_converter → pipeline → script_coherence_packs.
- **Same primary failure mode**: attention degradation when the file is loaded, not grep failure. Cache-miss cost ranked second.
- **Same recommendation to lift `_ctx()`**: Codex independently spotted the deferred refactor at `scene_converter.py:161-165` and named it the biggest hidden-state liability without being prompted.
- **Same direction on coherence packs**: registry + themed pack modules, not 40 microfiles.

Where Codex pushed back: my brief said "43 detector/injector pairs" in `script_coherence_packs.py`. The actual count is 24 `@patch_pack` registrations (helpers and pair partners pad to 84 functions). Corrected above.

Where Codex added something I missed: framed skill bodies as "wasteful when unused, actively harmful when invoked" — front-loading 8–12K tokens of policy before repo-specific reasoning starts. That's a sharper framing than "skills are big."

The agreement on three of three top-priority refactors, derived independently from raw file metrics, is the strongest signal that this is the right place to start.

---

## What this critique deliberately does *not* recommend

- **Microservices, plugin systems, or DI frameworks.** None of the issues here require architectural overhaul. They require splitting four files and deleting some markdown.
- **Reducing total line count.** 77K lines is fine for what this code does. The problem is shape, not size.
- **More tests.** The test suite (1340 tests) is dense already. The test *file* sizes are a side-effect of production sprawl — split with production code.
- **Documentation rewrites.** `ARCHITECTURE.md` is well-pitched. `CLAUDE.md` just needs history pruned.

The codebase is in fundamentally good shape. It has accumulated three or four AI-hostile concentrations that are mechanical to fix. The `convert-unity` skill proves the team already knows how to structure for AI agents — apply the same discipline to the hot files.
