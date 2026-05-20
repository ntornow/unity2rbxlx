# PR3a summary — runtime contract + generic-mode transpile

This document maps PR3a deliverables to the PR3a row in
`scene-runtime-contract.md`. The PR description should link here so
reviewers can navigate the change set against the design row 1:1.

## Scope from the design doc (PR3a row)

> Runtime **contract**: additive `_GENERIC_RUNTIME_PROMPT` (legacy
> prompt byte-unchanged) covering full host surface (Pieces 1 + 6);
> `runtime_mode` threaded through transpiler; pre-transpile
> `ModuleScript` target switch for runtime-bearing MonoBehaviours;
> lexical verifier (incl. constructor-purity rule e) + reprompt +
> fail-closed; generic allowlist (asset-rewrite + stem-keyed require
> resolution + verifier; all legacy repair passes off, incl.
> `write_output` emit-time subphases). `--scene-runtime` flag at every
> front door; `generic` rejected at CLI until PR4. Default output
> byte-identical. Ends with **compliance spike** — trash-dash +
> SimpleFPS verifier pass rate (pre/post-reprompt) recorded; PR3b/PR4
> don't start until rate clears agreed threshold.

## Code changes

### New modules

| File | Role |
|------|------|
| `converter/converter/runtime_contract.py` | Lexical verifier. Six rules (a–f) from Piece 1. Returns structured `Violation` rows. Pure (no I/O, no AI). |
| `converter/converter/contract_pipeline.py` | Generic-mode orchestrator. Wraps `transpile_scripts(runtime_mode="generic")` with the require resolver + fail-closed aggregator. Re-declares a local `SceneRuntimeArtifact` TypedDict so the orchestrator doesn't depend on PR1's `scene_runtime_planner` module (independent worktree topology). |
| `converter/tools/scene_runtime_spike.py` | Compliance-spike runner. Walks every `.cs` in a Unity project, builds a synthetic `scene_runtime` artifact (over-includes — every MonoBehaviour is `runtime_bearing`), drives the contract pipeline, prints + writes per-module pre/post-reprompt pass rate. |
| `converter/tools/__init__.py` | Package marker so `python -m tools.scene_runtime_spike` resolves. |

### Modified modules

| File | What changed |
|------|--------------|
| `converter/converter/code_transpiler.py` | Adds `_GENERIC_RUNTIME_PROMPT` (additive — `_AI_SYSTEM_PROMPT` byte-frozen, asserted by test). Threads `runtime_mode: Literal["legacy","generic"]` + `runtime_bearing_paths: frozenset[Path] \| None` through `transpile_scripts`. New `_select_prompt(runtime_mode)`, `_verify_and_reprompt(...)`, `_format_contract_violations(...)`, `_contract_reprompt_user_message(...)` helpers. Both backends (`_ai_transpile`, `_claude_cli_transpile`) accept `runtime_mode` and invoke the verifier + one-shot reprompt under generic. Cache key already takes `prompt_hash` → generic and legacy occupy disjoint cache namespaces by construction. |
| `converter/u2r.py` | Adds `--scene-runtime={legacy,auto,generic}` to `convert` and `eval`. Adds shared helper `_enforce_scene_runtime_legacy_until_pr4` that rejects non-legacy values with a pointer at the spike tool. Default `legacy` (existing invocations unchanged). |
| `converter/convert_interactive.py` | Adds `--scene-runtime` to the `transpile` command. Non-legacy values produce a structured-JSON error and `sys.exit(1)`. |

### Test files

| File | Coverage |
|------|----------|
| `converter/tests/test_runtime_contract.py` | Verifier fixtures: rules (a)–(f), all three rule-(f) syntactic shapes, constructor-purity (rule e), comment/string blanking, determinism. Plus the `LIFECYCLE_METHODS` / `UNITY_MESSAGE_CALLBACKS` constant invariants. |
| `converter/tests/test_scene_runtime_transpiler.py` | Prompt isolation (`sha256(_AI_SYSTEM_PROMPT)` byte-unchanged from `origin/main`, dynamically loaded via `exec`); cache-namespace disjointness; pre-transpile target switch (incl. prefab-only MB caught); default-arg ergonomics (`runtime_mode` defaults to `legacy`, no other call site changes). |
| `converter/tests/test_contract_reprompt.py` | `_verify_and_reprompt` control flow: clean-input skip-reprompt, reprompt-fixes-violation, reprompt-still-failing, backend-returns-None/empty, legacy-mode dormant. Plus violation-formatting structural checks. |
| `converter/tests/test_contract_pipeline.py` | `resolve_requires` (ok / missing_stem / stem_collision / multi-require / legacy-shape-ignored); orchestrator target-switch propagation; pre/post-reprompt pass-rate accounting; allowlist isolation (no legacy repair-pass imports — comment/docstring stripped before scanning). |
| `converter/tests/test_scene_runtime_cli.py` | All three front doors carry `--scene-runtime`. Generic + auto rejected with spike-tool pointer. Invalid value rejected by click. Default value `legacy` visibly documented in `--help`. |

## Key design decisions (locked in at session start)

1. **Spike harness, not pipeline integration.** PR3a's CLI rejects
   non-legacy modes; the compliance spike runs through the standalone
   `tools/scene_runtime_spike.py`. Full pipeline plumbing is PR3b/PR4
   work. Keeps `pipeline.py` clean and the spike repeatable.

2. **Verifier dormant under legacy.** `_verify_and_reprompt` returns
   the input unchanged when `runtime_mode != "generic"`. Legacy keeps
   its repair layer; generic enforces by construction.

3. **Same backend for the reprompt.** If initial transpile went via
   Anthropic API, the reprompt does too; same for Claude CLI. Symmetric
   with the existing `_lint_and_fix` syntax-error reprompt; the
   violation feedback flows through the user message, not a separate
   prompt.

4. **Independent local TypedDict for `scene_runtime`.** The orchestrator
   uses a local `_SceneRuntimeArtifact` / `_SceneRuntimeModule` instead
   of importing `scene_runtime_planner.SceneRuntimeArtifact`. PR1 / PR2
   / PR3a are independently landable per the design-doc topology; the
   artifact format is the contract surface, not PR1's planner code.

## Pass-rate accounting (compliance spike)

`_verify_and_reprompt` tags warnings two ways:

| Tag | Meaning |
|-----|---------|
| `contract-verifier-pre (rule X, line N): ...` | Violation the FIRST AI output had. Emitted only when reprompt was actually called. |
| `contract-verifier (rule X, line N): ...` | Violation that survived the reprompt. Routes to project-level fail-closed. |

The spike report decomposes runtime-bearing modules into:

- **First-attempt clean** — no warnings of either tag.
- **Reprompt-rescued** — has `-pre` warnings, no post warnings.
- **Still failing** — has post warnings (fail-closed candidates).

Pass rates: pre-reprompt = first-attempt / N; post-reprompt =
(first-attempt + rescued) / N. Stub-strategy modules (used when
`use_ai=False` or backend unavailable) are excluded from N — they
bypass the verifier entirely.

## What PR3a does NOT do

Per the design doc, deferred to later PRs:

- **Domain classification + new generic-only tables, intra-class
  conflict** → PR3b.
- **Reachability rule (`client require graph not in ServerStorage`)**
  → PR3b.
- **`.scene-runtime-mode` stamp + mismatch guard at front doors**
  → PR3b.
- **Inactive-object retention** → PR3b (`scene_converter.py` +
  `ui_translator.py` paths; the PR2 follow-ups doc has the detail).
- **Host runtime (`runtime/scene_runtime.luau`) + Piece 6 services**
  → PR4. Until this lands, `--scene-runtime=generic` is rejected at
  the CLI; the spike harness drives the verifier without it.
- **Auto-mode fallback wiring** → PR3b. PR3a's
  `ContractPipelineResult.fail_closed` is the surface PR3b's auto-mode
  reads to decide when to route to legacy.

## Compliance spike — gate decision

The PR3a → PR3b/PR4 gate is whether the verifier pass rate is high
enough to proceed. The threshold is not pre-committed in the design
doc; the gate review at session end decides. Spike command:

```
cd converter
python tools/scene_runtime_spike.py /path/to/UnityProject \
  --output spike-report.json
```

### Result

Ran on SimpleFPS (34 runtime-bearing MBs) + trash-dash (33). Backend:
Claude CLI. Total wall time: ~80 min for both.

| Project | AI-transpiled | Post-reprompt pass rate |
|---|---|---|
| SimpleFPS | 32 | **100% (32 / 32)** |
| trash-dash | 33 | **100% (33 / 33)** |
| Combined | 65 | **100% (65 / 65)** |

### Spike-discovered fixes (folded into PR3a)

The original spike v1 reported 84.4% / 81.8% post-reprompt — 9 modules
flagged rule (d) "no top-level return." Drilled into CharacterCollider's
cached output: the AI's module ended with `return CharacterCollider` on
line 305. The verifier's statement extractor wasn't reaching it.

**Root cause:** `_iter_top_level_statements` counted `then`+1 /
`end`-1, which inflated depth on multi-clause `if/elseif/elseif/end`
chains (one extra `then` per clause without matching `end`). Every
top-level statement past the first such chain in a method body was
silently dropped, including the bottom `return`.

**Fix:**
1. Block-opener token set switched from `{function, do, then, repeat}`
   to `{function, do, if, repeat}`. `then` / `elseif` / `else` are
   now no-op punctuation. Known limitation (documented inline): a
   top-level Luau if-expression `local x = if c then a else b` opens
   an `if` that never closes -- over-rejection follows. Rare in AI
   output; reprompt handles it if it happens.
2. Cache-hit paths in both backends (`_ai_transpile`,
   `_claude_cli_transpile`) now re-verify under `runtime_mode=generic`
   so a bugfix to the verifier invalidates stale contract-warning
   tags in cached entries. Helper: `_refresh_contract_warnings`.

Regression coverage:
`tests/test_runtime_contract.py::TestIfElseifChainDepthTracking` adds
4 fixtures -- 2-clause / else / nested / negative-guard.

### Spike-discovered, NOT a contract issue

17 `require_missing` failures on trash-dash all referenced `PlayerData`.
PlayerData.cs is a real `.cs` file in the project, but
`unity.script_analyzer._RE_CLASS` only matches `class X : Base` (with
inheritance). `class PlayerData` (base-less) is skipped; the editor-only
`class PlayerDataEditor : Editor` further down the file gets picked
instead, which flips `is_editor_script=True` and excludes the whole
file from `analyze_all_scripts`. PR1's real planner walks the GUID
index, not the analyzer's class output, so this issue does not surface
there. Tracked as a PR3a follow-up against `script_analyzer.py` --
independent of the contract pipeline.

### Reproducibility

Reports persisted at `/tmp/spike-simplefps-v2.json` and
`/tmp/spike-trashdash-v2.json` (post-bugfix runs). To re-run from
scratch (forces fresh AI transpilation): clear the
`.cache/llm/*.json` cache and invoke the spike again.
