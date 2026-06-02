# Design brief — TODO #8: Roblox-dead Unity-rendering modules misrouted to ServerStorage

**Status:** arch-review brief (pre-implementation). Decision gate at the end.
**Worktree:** `unity2rbxlx-task8`, branch `fix/roblox-dead-module-routing`, off `upstream/main`.

## ONE goal (restate)

Stop the converter from shipping **Roblox-dead** Unity modules (rendering helpers
with no Roblox equivalent) into the wrong place in the output. Do it with a
**generic** definition of "Roblox-dead" — driven by mapping coverage, never a
hardcoded class/API list. The converter must stay correct for ALL Unity games.

## Grounding facts (verify-or-refute each before reasoning — citations are load-bearing)

All file:line are in the `unity2rbxlx-task8` working tree (== `upstream/main`).

**GF1 — The symptom is real and empirically reproduced.** In the cached SimpleFPS
conversion (`converter/output/SimpleFPS/conversion_context.json`), the three water
rendering modules land in **ServerStorage** as ModuleScripts:
- `Displace`  → ServerStorage, reason `"required only by server-side callers (1)"`
- `WaterBase` → ServerStorage, reason `"required only by server-side callers (2)"`
- `PlanarReflection` → ServerStorage, reason `"required only by server-side callers (1)"`

**GF2 — The routing layer is caller-domain storage routing, NOT `infer_module_domains`.**
The task title says "`infer_module_domains` classifies them as server-only." That is
**imprecise** (sibling task #9 had the same kind of incomplete framing). The actual
route is `storage_classifier._decide_script_container_from_topology` **step 4**
(`converter/converter/storage_classifier.py:656-682`): for a ModuleScript, route by
the *callers'* domains — `caller_domains == {"server"}` → `ServerStorage`
(line 672-676). The module's OWN domain does not drive this.

**GF3 — Zero-signal modules do NOT get a server own-domain under single-player.**
`_apply_rule_table` (`module_domain.py:1455-1489`): a module with no client/server
API signals hits **Rule 7** → `client` under `networking="none"`, `server` only under
mirror/netcode. So under `none`, these modules' own domain is client/low-confidence —
further proof the ServerStorage routing is caller-driven, not own-domain-driven.

**GF4 — The water modules are a self-contained Roblox-dead cluster.** Their bodies
(`test_projects/SimpleFPS/Assets/Standard Assets/Environment/Water/Water/Scripts/`):
- `Displace.cs` (35 lines): 100% `Shader.EnableKeyword/DisableKeyword`.
- `WaterBase.cs` (77 lines): `Shader.EnableKeyword`, `sharedMaterial.shader.maximumLOD`,
  `SystemInfo.SupportsRenderTextureFormat`, `Camera.main.depthTextureMode`.
- `PlanarReflection.cs` (283 lines): `RenderTexture`, `Camera.Render()`, `GL.invertCulling`,
  `cullingMask`, `targetTexture`, reflection-matrix math.
`PlanarReflection` + `Displace` both `[RequireComponent(typeof(WaterBase))]`; the cluster
only requires itself + a leaf Script (`GerstnerDisplace`, → ServerScriptService default).
The leaf's server default cascades server-only-callers onto the modules → GF1.

**GF5 — Mapping coverage for these APIs is effectively zero, and is TRI-STATE.**
`converter/converter/api_mappings.py` (1067 lines):
- Absent entirely: `RenderTexture`, `GL.*`, `depthTextureMode`, `maximumLOD`,
  `SystemInfo`, `targetTexture`, `cullingMask`.
- Present but **explicit "no equivalent" comment stub**: `Shader.PropertyToID`,
  `Shader.Find` (`api_mappings.py:548-549`, value `"-- …: no equivalent"`).
  `Shader.EnableKeyword`/`DisableKeyword` aren't even listed.
- Genuinely mapped (incidental): `Camera.main` (1), `Mathf.FloorToInt` (1),
  `Vector3`/`Vector4`/`Matrix4x4` types.
So "has a map entry" ≠ "has a Roblox equivalent." A deadness metric must treat
`-- no equivalent` stubs as dead-contributing, same as absent.

**GF6 — `api_mappings.py` is NOT the only mapping authority.** Per project memory,
`api_mappings.py` is reference-ish; the **AI transpiler** is the live C#→Luau lever and
can map creatively. So "not in api_mappings" *overstates* deadness in general. For these
rendering APIs the AI also emits stubs/no-ops — but a generic definition must not assume
api_mappings is exhaustive. (Open question: measure deadness on the INPUT C# API surface
vs the OUTPUT transpiled Luau body.)

**GF7 — Emission is via `place.scripts` + per-script `parent_path`, not storage_plan
buckets.** `roblox/rbxlx_writer.py:1620-1634` iterates `place.scripts` and emits each by
its `parent_path`. `storage_plan.shared_modules`/`server_modules` are audit-only. To DROP
a module you must remove it from `place.scripts` before `write_output` (+ clean its cached
`.luau`). Precedent exists: `pipeline.py:_delete_pruned_script_from_disk` (~2556).

**GF8 — Drop-safety hinges on the require graph.** SceneRuntime tolerates a missing module
(`runtime/scene_runtime.luau:614-627`: warn + skip the instance, no crash). BUT a surviving
module-to-module `require()` to a dropped module **hard-crashes** (the injected
`RS:FindFirstChild(name) or SS:FindFirstChild(name)` → `require(nil)`,
`script_coherence.py:69-84`). There is NO emit-time validation that user-module requires
resolve (only `@scene_runtime/` requires are checked, `contract_pipeline.py:467-476`).
**Implication:** pruning is only safe for modules in a closure where no SURVIVING (live)
module requires them. The water cluster is self-contained → safe; a dead-but-live-required
module is NOT safe to drop.

**GF9 — There is already a "not instantiated at runtime" notion that does NOT drop.**
`runtime_bearing=False` → `domain="helper"` (`module_domain.py:766-777`); `excluded` /
`__excluded__` are domain verdicts. None of these remove the ModuleScript from output —
they still emit. So "mark it inert" and "drop it" are distinct levers today.

## The two fix shapes from the TODO (+ a hybrid)

**Option A — Dead-code pruning pass** (TODO's "more general"). Detect Roblox-dead modules
and DROP them from output (remove from `place.scripts`, delete cached `.luau`, strip/repair
any surviving `require()`). Pros: removes dead weight entirely; fixes wrong-container by
making the module not exist; also fixes dead modules that route to *client* containers
(broader than the reported symptom). Cons: biggest blast radius; must respect GF8
(never drop a module a live module requires); must define a safe closure; risk of pruning
real game logic if the deadness metric is wrong.

**Option B — Roblox-dead classifier signal short-circuiting routing.** Compute a generic
"Roblox-dead" signal in the topology/domain layer; consume it in storage routing to send
dead modules to a neutral container (ReplicatedStorage) and/or stamp them non-runtime so
SceneRuntime never instantiates them. Pros: narrow; no require-graph hazard (modules still
exist); directly kills the "wrong container" symptom. Cons: still ships dead code (just
in RS); leaves dead modules being required/loaded; doesn't address the deeper "this does
nothing" problem; "neutral container" is arguably cosmetic.

**Option C — Hybrid (signal in classifier, safe-subset pruning consumer).** One canonical
"Roblox-dead" signal computed once in the topology layer (the genericity-critical part);
a pruning consumer drops ONLY modules that are dead AND in a fully-dead require-closure
(no live requirer, per GF8); dead-but-live-required modules stay emitted but are stamped
inert (not instantiated). Pros: gets A's dead-weight removal where it's safe, B's safety
where it isn't, single source of truth for the definition. Cons: most surface area to build
+ test; two consumers of one signal.

## Sub-decision — the GENERIC definition of "Roblox-dead"

The whole task hinges here. Candidate definitions:

- **D1 (input-side / mapping-coverage):** A module is Roblox-dead when the fraction of its
  C# API references that resolve to a *real* (non-stub) entry in mapping coverage
  (`API_CALL_MAP` ∪ `TYPE_MAP` ∪ `UTILITY_FUNCTIONS`, treating `-- no equivalent` as
  unmapped per GF5) is below a threshold. Structural/canonical-contract (user-preferred
  over regex). Risk: GF6 — api_mappings isn't exhaustive, so it can over-flag modules the
  AI *could* have mapped.
- **D2 (output-side / transpiled-body deadness):** A module is Roblox-dead when the
  transpiled Luau body is dominated by no-op / comment / `-- no equivalent` stub lines
  (i.e. the AI itself produced nothing executable). Directly reflects "does nothing in
  Roblox" and captures the AI's actual decision. Risk: regex-on-AI-output (user distrusts);
  needs a robust, non-brittle metric.
- **D3 (hybrid):** input-side mapping coverage as the primary structural signal, with an
  output-side corroboration (the AI emitted only stubs/no-ops) as a confirmation gate, so a
  module is flagged dead only when BOTH agree. Lower false-positive rate.

Threshold + tie-breaks (open): what fraction = "dominated"? How to weight lifecycle-only
modules (Displace is 100% Shader)? Must any single real Roblox-effecting call (e.g. a real
`Instance` write) veto deadness?

## Genericity guardrails (non-negotiable)

- No hardcoded class names (`WaterBase`/water/etc.) or hardcoded API lists beyond what
  already exists as the mapping tables. The signal must be derived from coverage data.
- Must not flag a real gameplay module that merely uses a few unmapped APIs.
- Must not break a live module's `require()` (GF8).
- Must work in both legacy and `--generic` scene-runtime modes (the routing lives in
  `storage_classifier`, shared by both).

## Decision-options for reviewers to vote on

1. **Fix shape:** A (prune) / B (signal-only routing) / C (hybrid).
2. **Definition:** D1 (input/mapping-coverage) / D2 (output/transpiled-deadness) /
   D3 (hybrid both-agree).
3. **For any pruning:** confirm the GF8 safety rule (drop only fully-dead require-closures,
   never a module with a live requirer) — agree / propose stricter / propose looser.
4. **Layer placement:** where the signal is computed (topology `infer_module_domains` /
   storage `classify_storage` / a new pass) and where consumed.

## LOCKED DECISIONS (user sign-off 2026-06-01, after Claude+Codex arch review)

**Fix shape: C (hybrid), sequenced B-first within this PR.** Build one canonical
"Roblox-dead" signal; consume it (1) in storage routing — BOTH the topology path
(`storage_classifier.py:656-682`) AND the legacy path (`storage_classifier.py:759-793`,
whose reason text `"...server-side callers"` is what the cached symptom actually shows) —
to stop caller-domain routing from pulling dead modules into ServerStorage, and stamp them
inert so SceneRuntime skips instantiation; and (2) in a prune pass that DROPS a dead module
ONLY when its entire require-closure is also dead.

**Definition: D3 (both-agree) + HARD VETO.** Dead iff INPUT (C# API surface dominated by
unmapped APIs; `-- no equivalent` stubs count as unmapped) AND OUTPUT (post-coherence Luau
inert) agree. Hard veto: any single real Roblox effect (Instance write/new, RemoteEvent
fire, DataStore/real service mutation, a genuinely-mapped API call) ⇒ NOT dead, regardless
of fraction. Input is a weak prior; output confirmation is decisive.

**Replace, don't extend, the hardcoded heuristic.** `code_transpiler._is_visual_only_script`
(`code_transpiler.py:686-721`) hardcodes game class names (`waterbase`/`displace`/…) — the
ONLY reason `WaterBase`/`Displace` are caught today (generic `shader_count>=2` misses them
at 1 hit each). Replace the name list with the generic D3 detector (or demote names to a
legacy-only weak prior). No game-specific names in the new path.

**Pruning safety (stricter than GF8):** the require-closure MUST be computed from the FINAL
EMITTED LUAU (post-coherence injected `RS:FindFirstChild(name) or SS:FindFirstChild(name)`
edges), NOT `dependency_map` (which misses injected post-transpile requires —
`storage_classifier.py:360-404`). Never drop a module with ANY live (non-dead) requirer.
Do NOT prune runtime-bearing generic-mode components unless the SceneRuntimePlan rows are
updated too; for the dead-but-live-required remainder, keep them as inert host-loadable
stubs (the B fallback), don't prune.

**Layer placement:** a new analysis pass between `_subphase_cohere_scripts` and
`_classify_storage` (`pipeline.py:~2636`), where the final emitted Luau exists. Consume the
verdict in `classify_storage` (routing) and in a dedicated prune pass.
`infer_module_domains` is too early (no post-coherence Luau) — it only stores/reads the
verdict, doesn't compute it.

**Geometry safety (verified empirically 2026-06-01):** dropping the scripts does NOT remove
water geometry. `scene_converter` builds MeshPart geometry+material in dedicated component
branches independent of the `MonoBehaviour` branch (`scene_converter.py:2474-2479`), in a
separate phase from transpile; the prune pass only removes script Instances. For SimpleFPS
the water prefabs (`Water4Simple`/`Water4Advanced`) are placed in ZERO scenes — the 6
scripts are orphan dead code with no geometry at all. No visual regression either way.

## Questions for reviewers

- Is the empirical reframe in GF2 correct — is the symptom genuinely caller-domain
  routing, not own-domain? Refute with code if not.
- Is D1's reliance on `api_mappings.py` sound given GF6, or does D2/D3 better reflect
  ground truth? What's the lowest-false-positive generic metric?
- Does Option C's complexity earn its keep over B for the reported symptom, given B ships
  dead-but-harmless code while A/C carry require-graph risk?
- Any failure mode where a "Roblox-dead" verdict would wrongly prune/reroute a module that
  matters (e.g. a module that's dead on its own but whose require side-effects a live module
  depends on)?
