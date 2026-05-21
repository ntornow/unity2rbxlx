# Review Log: Generic Scene-Runtime Contract

Round-by-round review history for
[`scene-runtime-contract.md`](scene-runtime-contract.md). This file is the
process archaeology — the design itself lives in the main doc. The review loop
appends a new round here; the main doc carries only the resulting spec.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `codex exec` | Independent 2nd opinion | 10 | issues_found | R1: 4P0/6P1 … R8: 1P0/2P1. R9: 1P0/2P1. R10: 1P0/4P1 — all absorbed |
| Eng Review | `/plan-eng-review` + Claude subagent | Architecture & tests (required) | 9 | issues_found | R2: 2P0+5P1/P2 … R9: 0P0/0P1 (split). R10: 1P0/3P1 (converged with Codex) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (no UI) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

**ROUND 2 (2026-05-19).** The round-1 "execution-ready" claim was wrong. A
second eng review + independent Codex review converged, with zero cross-model
disagreement, on two P0s:

- **PR3 was not flag-isolated** — it edited the shared `_AI_SYSTEM_PROMPT`,
  whose sha256 is a transpile-cache key, which regresses `legacy` output and
  makes the PR5-6 byte-comparable test impossible. *Fixed:* PR3 now adds a
  separate `_GENERIC_RUNTIME_PROMPT`; the legacy prompt and cache are
  byte-unchanged.
- **The execution-domain model conflated file placement with execution side**
  — the storage classifier only places ModuleScripts in `ReplicatedStorage` or
  `ServerStorage` and was wrongly cited as the client/server authority; the
  reused `storage_overrides` mechanism is `name→container` and cannot express
  domain. *Fixed:* `domain` is now a separate per-module decision derived from
  the classifier's client/server API-surface analysis, with its own
  `runtime_domain_overrides` map.

Also absorbed: multi-scene runtime semantics (one place per scene; `LoadScene`
explicitly unsupported), the incomplete host API surface (`GetComponent`/
`Instantiate`/`Destroy`/`SetActive` now in the contract), the unbacked verifier
("fail-closed" now has a real lexical detector), `conversion_plan.json`
cross-run rehydration contamination, PR8's missing FPS dependency web, and
PR-dependency honesty (added a "Depends on" column).

**ROUND 3 (2026-05-19).** A third Codex pass on the round-2 revision found the
round-2 fixes incomplete and the new text carrying concrete correctness bugs:

- **`domain` model still wrong** — round 2 treated `shared` as "server by
  default" and silently forced both-side-API modules to `server`, killing one
  side. *Fixed:* `shared` now means *both runtimes instantiate* (independent
  instance each); a both-side-API module is a contract conflict, never a silent
  `server`.
- **Prompt-hash isolation did not cover persisted script bodies** — generic
  `output/scripts/*.luau` can be rehydrated by a later `legacy` rebuild.
  *Fixed:* the script dir is mode-stamped; a mode mismatch forces retranspile.
- **A second prompt constant is not enough** — the transpiler had no
  `runtime_mode` parameter and built its target from `script_type`. *Fixed:*
  PR3 threads `runtime_mode` and makes the `ModuleScript`-target switch
  pre-transpile.
- **Lexical verifier missed `local c = …:Connect(…)`** — *fixed:* the rule now
  rejects top-level `local` bindings with side-effecting RHS (conservatively).
- **Emitted module filenames are not unique** (the `:166` suffixing is
  in-memory only) — *fixed:* `module_path` added; emit must de-duplicate.
- **PR3 could expose a selectable `generic` before PR4's host exists** —
  *fixed:* PR3 adds the flag but rejects `generic` at the CLI until PR4.

**ROUND 4 (2026-05-19).** Round 4 ran a Claude architecture review and a Codex
review **in parallel** and compared notes — they converged on the broken
mechanisms with little overlap elsewhere (complementary coverage). Findings,
all absorbed:

- **P0 — `domain: shared` = "run on both" double-executes** (Codex). A neutral
  gameplay component with no side-specific API would run its lifecycle on both
  client and server — two spawns, doubled score. *Fixed:* `domain` is now
  `{client, server}` only; neither-side-API defaults to `server`
  (authoritative, single-side); there is no `shared` execution domain.
- **P0 — the pre-transpile `ModuleScript` retype reaches `storage_plan`**
  (Codex). `_classify_storage` persists `script_type` into `storage_plan`, so
  the round-3 "don't mutate `storage_plan`" promise could not hold. *Fixed:*
  generic and legacy never share an output directory — one
  `output/.scene-runtime-mode` stamp + CLI-entry mismatch guard closes
  `storage_plan`, script-body, and plan contamination together (replaces the
  round-3 per-surface stamp, which Claude independently showed was pinned to
  the wrong pipeline phase and had no defined storage location).
- **P1 — `runtime_domain_overrides` was a top-level key** silently dropped by
  `_classify_storage`'s rewrite (Claude). *Fixed:* renamed `domain_overrides`,
  nested under `scene_runtime`, which PR1's merge already preserves.
- **P1 — the domain ordering was circular** — the API-surface helpers scan
  transpiled Luau (Claude). *Fixed:* the pre-transpile switch uses only the
  "runtime-bearing?" predicate; `domain` is computed post-transpile.
- **P1 — the lexical verifier still had a table-constructor hole**
  (`local t = {c = x:Connect(...)}`) plus top-level `require` injection (Codex).
  *Fixed:* the rule recurses into table constructors; `script_coherence`'s
  require injection is disabled under `generic`; the verifier is now described
  honestly as a best-effort backstop.
- **P2s** — corrected the `_load_storage_plan_for_rehydration` name, dropped the
  "reuses the lint+reprompt loop" overstatement, required `--scene-runtime` on
  every `write_output` front door.

**ROUND 5 (2026-05-19).** Parallel Claude + Codex pass on the round-4 revision.
Both converged on the domain-detector gap; the rest was complementary. All
absorbed:

- **P0 — the pre-transpile predicate was scene-only** (Codex). Prefab-only
  MonoBehaviours (never in a scene, only spawned via `instantiatePrefab`) would
  miss the `ModuleScript`/generic-prompt switch and transpile legacy-shaped.
  *Fixed:* the predicate is "runtime-bearing MonoBehaviour" = attached to a
  scene GameObject *or a prefab*, computed from the union of all scenes +
  prefabs before the shared transpile step.
- **P1 — the domain detector misroutes genuinely-client components** (Codex +
  Claude, converged). `_CLIENT_ONLY_PATTERNS` misses `RenderStepped` /
  `FireServer` (Codex) and never catches UI-controller modules that write to
  `ScreenGui`/`PlayerGui` via injected refs — those silently no-op server-side
  (Claude). *Fixed:* PR3 extends the pattern sets *and* adds a wired-reference
  UI signal; neither-signal modules default to `server` but are flagged
  low-confidence in the conversion report.
- **P1 — generic modules were not isolated from legacy coherence passes**
  (Codex). `_subphase_cohere_scripts` runs `fix_require_classifications`
  (`script.Parent` salvage, require injection) on all scripts. *Fixed:* under
  `generic` those legacy passes are skipped and the contract verifier runs
  *last*, after all generic-path mutators.
- **P1 — duplicate-stem fail-closed was scoped too narrowly** (Codex) and the
  `scene_runtime` persistence merge was asserted as *existing* when it is PR1
  work (Claude). *Fixed:* stem-collision fail-closed is global (vs any script);
  the doc now says PR1 *adds* the merge and distinguishes sticky
  `domain_overrides` from recomputed `domain`/`container`.
- **P2s** — corrected the `scripts_cache_intact()` front-door list (`u2r.py`
  publish/eval + `convert_interactive.py` assemble/upload), added the missing
  `--clean` flag to PR3 scope, fixed the `:663`→`:666` citation.

**ROUND 6 (2026-05-19).** Parallel Claude + Codex pass on the round-5 revision.
**Zero P0s** — and the two models converged on the *same three* P1s
independently, a strong signal the remaining gaps are now well-identified:

- **P1 — generic-path mutator isolation was still under-enumerated** (Codex +
  Claude, converged). "Skip two legacy passes" was wrong by ~15: `_subphase_
  cohere_scripts` → `fix_require_classifications` runs ~19 passes (incl.
  `_guard_client_code_in_modules`, which does the very semantic relocation the
  contract forbids), and `shared_state_linter` / `_subphase_patch_setup_sounds`
  mutate scripts outside it. *Fixed — the convergence fix:* generic **inverts
  the default** to an explicit allowlist (asset-reference rewrite + verifier
  only); every legacy repair pass is off. This kills the recurring "you missed
  pass N" class the same way the round-4 mode-stamp killed per-surface
  contamination.
- **P1 — the domain detector still missed client signals** (Codex + Claude,
  converged) — `OnClientEvent`, camera/`StarterGui` variants, `OnServerInvoke`.
  *Fixed:* PR3 consolidates onto the *richer* existing pattern set
  (`script_coherence.py:39`), not a hand-extended thin copy.
- **P1 — the UI signal was not derivable from the planner artifact** (Codex +
  Claude, converged). `target_kind` alone cannot say "became a `ScreenGui`".
  *Fixed:* the planner stamps an explicit `target_is_ui: bool` on each
  reference row (it knows `Canvas`→`ScreenGui` at conversion time).
- **P2s** — PR1's scope row now explicitly commits to prefab-attached
  MonoBehaviour enumeration; `convert_interactive.py` citation corrected.

**ROUND 7 (2026-05-20).** Parallel Claude + Codex pass on the round-6 revision.
**First split verdict:** Claude found zero P0/P1 ("execution-ready: yes");
Codex found zero P0 but two P1s. Cross-check: both Codex P1s are verified code
facts, and Claude's "yes" rested on two assumptions Codex checked and refuted —
so the P1s stand. All absorbed:

- **P1 — the allowlist dropped require-wiring with no replacement** (Codex).
  The allowlist turned off `inject_require_calls`, which wires *code-level*
  project-class imports — not covered by the host's component-reference
  injection. *Fixed:* the allowlist now includes module-require-path
  resolution; the generic prompt emits `require(...)` by name, an allowlist
  pass resolves the path to `module_path`.
- **P1 — "consolidate onto the richer existing pattern set" was fiction**
  (Codex). The `storage_classifier` and `script_coherence` client/server
  tables are near-duplicates and *neither* contains `RenderStepped` /
  `:FireServer(` / `.OnClientEvent` / `.OnServerInvoke`. *Fixed:* PR3 now
  **defines a new shared classifier table** with those signals.
- **P2s** — `target_is_ui` redefined as "anywhere inside a Canvas/UI subtree"
  (only the Canvas *root* becomes a `ScreenGui`; children are nested
  `RbxUIElement`s); made explicit that the pre-transpile retype mutates
  `RbxScript.script_type`; noted PR1 reuses `prefab_parser.py`.

**ROUND 8 (2026-05-20).** Parallel Claude + Codex pass on the round-7 revision.
Codex 1 P0 / 2 P1; Claude 1 P1. All verified against code and absorbed:

- **P0 — the round-7 "new shared classifier table" would mutate the legacy
  path** (Codex). Both consumers (`storage_classifier`, `script_coherence`) run
  on the legacy path; extending their tables shifts legacy script typing and
  breaks byte-identical legacy output. *Fixed:* the richer table is
  **generic-only**; the legacy tables stay byte-frozen.
- **P1 — require-resolution naming was under-specified** (Codex + Claude,
  converged). The codebase has three identifiers (`class_name` / internal
  `stem` / file stem); a "by-name placeholder" is ambiguous without pinning
  one. *Fixed:* the pass keys on `class_name`, validates against the planner
  dependency graph, and fails closed on ambiguity — and `scene_runtime.modules`
  now covers helper require-targets too (not just runtime-bearing
  MonoBehaviours), with the stem-collision check global across the require
  graph (Claude's helper↔helper gap).
- **P1 — UI references had no runtime resolution path** (Codex). `target_is_ui`
  classified the *module* but a field pointing at a `Button` had no ID to bind
  to. *Fixed:* Piece 3 / PR2 now stamp `_SceneRuntimeId` on converted UI
  instances and add UI-attribute emission to both writers.
- **P2 — the pre-transpile retype named the wrong object** (Codex — no
  `RbxScript` exists pre-transpile). *Fixed:* retype sets the transpile-time
  type (`ScriptInfo.suggested_type`); the `RbxScript` is born with it.

**ROUND 9 (2026-05-20).** Parallel pass on the round-8 revision. **Second split
verdict:** Claude found zero P0/P1 ("execution-ready: yes", 2 P3 nits); Codex
found 1 P0 + 2 P1. All Codex findings verified against code and absorbed:

- **P0 — inactive objects are pruned at conversion** (Codex). The lifecycle
  model's `active`/`setActive` assumes an inactive object exists as a dormant
  instance to bind and later flip — but `scene_converter.py` returns `None`
  for `node.active == false` (`:1111`), skips inactive prefab instances
  (`:3552/:3143`), and `ui_translator.py:261` drops inactive UI. This is an
  *original-design* gap, not round-8 fix text — 8 rounds missed it. *Fixed:*
  new "Inactive objects must survive conversion under `generic`" rule —
  runtime-bearing or runtime-referenced inactive objects are emitted dormant
  with `_SceneRuntimeId`, not pruned (`runtime_mode`-gated; legacy unchanged).
- **P1 — stem-collision keyed on the wrong identifier** (Codex). Physical
  identity is the emitted *file stem*, not `class_name`. *Fixed:* the
  collision check and the require key are now the emitted stem.
- **P1 — `class_name` extraction is unreliable** (Codex). `_RE_CLASS` misses
  base-less helper classes. *Fixed:* canonical require key is the file stem
  (always present); PR3 also strengthens `_RE_CLASS`.

**ROUND 10 (2026-05-20).** Fresh cold pass — a new Claude review reading the doc
with no carried assumptions, plus a parallel Codex pass, both verifying ~55
`file:line` citations against the tree. **Third split-then-converge:** both
models independently landed the same structural P0 and most of the same P1s.
All verified against code and absorbed:

- **P0 — non-scalar serialized fields had no injection contract** (Codex +
  Claude, converged). The host surface defined `gameObject`/`GetComponent`
  (component & GameObject refs) but the contract never said what a module
  receives for an `asset` (`AudioClip`/`Texture`/`Mesh`), `prefab`, or
  `scriptable_object` field. Today the scene converter materializes a `Sound`/
  `Model` *child* found via `script.Parent` (`scene_converter.py:2295,2316`) —
  the mechanism the contract removes. A genuine original-design gap, not
  fix-text. *Fixed:* new "Non-scalar serialized fields" host-surface rule —
  `asset` → injected `rbxassetid://` string, `prefab` → injected plan key for
  `instantiatePrefab`, `scriptable_object` → injected SO table; legacy child-
  materialization is suppressed under `generic` (PR3 scope).
- **P1 — PR1 was mis-rated `—`/Low/execution-ready** (Codex + Claude,
  converged). `scene_runtime.modules` must enumerate transitive helpers, but
  the only dependency graph (`pipeline.py:1555`) is `class_name`-keyed and
  drops empty-`class_name` (base-less) helpers. *Fixed:* PR1 now owns the
  `_RE_CLASS` strengthening + a stem-keyed planner require graph; PR1 risk
  Low→**Med**.
- **P1 — generic isolation covered only the transpile→emit path** (Codex). The
  `write_output` phase independently runs `_bind_scripts_to_parts`,
  `ClientBootstrap` injection, autogen, template script attachment, runtime-lib
  injection, and `setupSounds` patching. *Fixed:* the allowlist's inverted
  default now explicitly extends to every `write_output` emit-time subphase.
- **P1 — PR1's multi-scene planner phase was unplaced** (Codex). The planner
  needs all scenes + prefabs parsed before the shared transpile, but the
  single-scene path keeps only one scene. *Fixed:* PR1 adds an explicit
  `plan_scene_runtime` shared phase in both drivers.
- **P1 — the require key was self-contradictory** (Codex + Claude). Body +
  round-9 said emitted *stem*; the PR3 table row still said `class_name`-keyed.
  *Fixed:* the PR3 row now says stem-keyed. Also: AI contract-compliance was
  the biggest unquantified risk with no checkpoint — added a PR3→PR4 compliance
  gate; the `scene_runtime` schema gained the missing `prefabs` subplan slot.
- **P2s** — corrected the `_guard_client_code_in_modules` over-read (it
  nil-safes `LocalPlayer` + guards connections; it does *not* block-wrap
  module scope); fixed the `code_transpiler.py:80` mis-citation (public
  boundary is `transpile_scripts` at `:89`; `script_type` is internal, not a
  boundary param); `script_analyzer.py` is under `unity/`, `suggested_type` is
  `:59`; PR2 risk Low→Med; removed a duplicated sentence in the stem-collision
  paragraph.

**VERDICT — convergence assessment (as of round 10).** Ten rounds, two models,
~2.3M+ tokens of review. P0 count: 4 → 2 → 2 → 2 → 1 → 0 → 0 → 1 → 1 → 1. The
architecture has been sound and unchanged since round 1 — *every* round
confirms that. The loop is **not numerically converging on zero**: Codex's deep
per-round code verification keeps finding 1-3 real gaps, and round 10 showed
*both* models can still surface an original structural P0 (the non-scalar-ref
contract) nine rounds in. What *has* converged is the cross-model agreement —
rounds 6, 8, and 10 saw the two models independently name the same primary
defects. Honest status: the architecture is final; the spec is not yet at a
both-models-clean round. PR1-PR2 are landable but should not start until a
review round comes back clean or P2-only. Continue the review loop; stop when a
round produces no P0/P1 from either model.
