# Execution design — reaching PR#127's goal (AI-editable mega-files) alongside the scene-runtime recut

**Status:** execution design · 2026-06-04 · no code
**Inputs:** [`scene-runtime-pr5-8-recut-plan.md`](scene-runtime-pr5-8-recut-plan.md) (the scene-runtime work that remains) + PR#127's [`refactor_plan.md`](refactor_plan.md) / [`architecture_critique.md`](architecture_critique.md) (the 8-PR mega-file split, eng-reviewed + codex-vetted, 9 locked decisions).
**Scope of this doc:** the *interleaving / gating* of the two efforts. It does **not** redesign any PR-B…PR-H internals — those 9 locked decisions stand. It changes only the **gate**, which PR#127 itself flags as a user decision (codex round-4 P1: "a compressed plan silently reverted the user-locked 'all held' decision" — so the gate change here is surfaced explicitly, not baked in).

## The reframe — why the blanket hold is now partly stale

PR#127 holds **all** refactor PRs until "scene-runtime-contract PR5–PR8 land upstream," for two reasons: (1) avoid two multi-PR efforts diluting review attention; (2) Phase 2 (`scene_converter.py`) is "additionally blocked" because the remaining scene-runtime PRs churn that file. Two facts have changed:

1. **scene-runtime did not land as #122–#133.** It landed via phases; we retired the #131/#132/#133 drafts and the remainder is the narrower **recut**: Slice T (Turret lowering) + Slice H (HudControl domain) → PR6 guard → PR7 flip → PR8 retire. Its file footprint is `contract_pipeline.py`, `scene_runtime_topology/module_domain.py` (the post-PR#148 domain authority — `scene_runtime_domain.py` is a back-compat shim), `pipeline.py` (one guard method), `script_coherence_packs.py` (delete 5 packs), `scaffolding/`. Slice H must also satisfy the fail-closed **`contract_verifier`** (PR#148 Phase 3) — coherent client domain + container + LocalScript, see the recut plan.
2. **`scene_converter.py` is decoupled from the recut.** Verified: closed PR5/6/8 touch `scene_converter.py` **0 times**; recent main churn on it is flag-names/meshes, not scene-runtime. The "Phase 2 blocked by the scene_converter lock" condition has **expired**. UI/Pause completeness lives in `ui_translator.py` (854 LOC, not a split target); Slice T's lowering lives in `contract_pipeline.py`. So `scene_converter.py` can be held frozen through its own split.

Net: the file PR#127 flagged as the **worst** (`scene_converter.py`, 5553 LOC, `_ctx()` hidden state at ~58 sites) is the one that is **now executable**, while the genuine collisions are localized to two files.

## Collision matrix (the real gating signal — replaces the blanket hold)

| Refactor PR | Target file | Recut touches it? | Gate |
|---|---|---|---|
| **PR-B** frozen-hash baseline | `tests/test_offline_assembly.py` | no | **none — do first, always**; freeze **mode=`legacy` explicitly** (immune to PR7's default flip) |
| **PR-G** `_ctx()` elimination | `scene_converter.py` | **no** | **unblocked now** |
| **PR-H** split `scene_converter.py` → 11 modules | `scene_converter.py` | **no** | **unblocked now** (after PR-G); final byte-equiv gate re-proves after **PR8** (shared SimpleFPS hash) |
| **PR-C** `write_output` → `phases/output/*` | `pipeline.py` | **yes — PR8 removes `apply_scaffolding`/`scaffolding` plumbing** | after **PR8** |
| **PR-D** Pipeline dispatch table | `pipeline.py` | **yes — PR8 rewrites pipeline.py 365+/516−; deletes methods PR-D's frozen API binds** | after **PR8** (then PR-C) |
| **PR-E0** pack ordering audit | `coherence` fixture | indirect (PR8 deletes packs) | after **PR8** |
| **PR-E** split `script_coherence_packs.py` | `script_coherence_packs.py` | **yes** (PR8 deletes 5 packs) | after **PR8** |
| **PR-F** mirror test split | `test_script_coherence_packs.py` | indirect | after **PR-E** |

The blanket "hold everything" collapses to **one** surgical gate, not two: **both the pipeline split and the pack split wait for PR8.**

> **Correction (parallel-review, 2026-06-04):** the first draft gated the pipeline split behind **PR6**. That is wrong. PR8 — not PR6 — is the dominant `pipeline.py` churn from the recut: it rewrites the file **365+/516−** and **deletes `apply_scaffolding` + the `scaffolding` property + `_init_scaffolding`**, which PR-D's *frozen `Pipeline` public API* (refactor_plan.md:138) and `PipelineServices` *bound helpers* (refactor_plan.md:54) both retain. PR-C/PR-D authored before PR8 would bake deleted methods into the dispatch contract. **Gate the pipeline split behind PR8.** PR6's guard (+1 method) is absorbed into the same post-PR8 baseline.
>
> **Third collision surface:** the `plan_scene_runtime` **phase** (pipeline.py:57) calls `scene_runtime_planner`, and the domain classifier (`scene_runtime_topology/module_domain.py` — **Slice H's file**) runs in the classify/`materialize_and_classify` phase. PR-D's phase-module enumeration mentions `plan_scene_runtime` **zero** times (also omits `materialize_and_classify`). So PR-D's table is stale *and* its phase split brushes the same phases Slice H edits — sequence PR-D **after Slice H**, and refresh the enumeration at re-baseline.

## Three-track plan

```
TRACK 0 — prerequisites (serial, gate the rest)
  Re-baseline (pipeline.py 4917→6495 since last; refresh PR-C/PR-D LOC targets;
     revalidate PipelineServices field set — PR8 REMOVES apply_scaffolding +
     fps_artifacts_at_init, guard ADDS reach; pack count 27→~22 post-PR8;
     add plan_scene_runtime + materialize_and_classify to PR-D's phase table)
  → PR-B frozen-hash baseline, mode=legacy   (no collision; protects every split)

TRACK 1 — scene-runtime recut  (critical path; ★ = moves the legacy SimpleFPS baseline)
  Slice T (Turret child-index lowering, generic-only — no legacy-baseline move)
  → Slice H (HudControl UI→client domain, generic-only — no legacy-baseline move)
  → PR6 guard
  → PR7 flip default legacy→auto   (PR-B froze mode=legacy → not invalidated)
  → PR8 retire scaffolding+packs ★ ── unblocks BOTH splits ──┐
                                                             │
TRACK 2 — scene_converter refactor (PARALLEL now, file-decoupled from Track 1)
  PR-G _ctx() elimination → PR-H split → 11 modules
     └─ PR-H's final SimpleFPS byte-equiv gate re-proves after PR8 ★ ─┐
                                                                      │
TRACK 3 — collision-file refactors (ALL gated behind PR8)             │
  PR-C write_output extract → PR-D dispatch table        ←────────────┤(pipeline; PR-D after Slice H)
  PR-E0 ordering audit → PR-E pack split → PR-F test split ←───────────┘(packs)
```

Track 2's *editing* never waits on Track 1 (disjoint files), but its *final byte-equivalence proof* shares the SimpleFPS frozen hash with the recut, which PR8 moves — so PR-H's closing gate re-proves against the post-PR8 freeze. Tracks 1 and 2 are the two concurrent efforts; Track 3 is gated behind PR8, not concurrent.

## The regression-harness contract (the subtle coordination)

The two efforts have **opposite** definitions of "correct," so the shared baseline needs an explicit ownership rule. The subtlety (parallel-review correction): **most Track 1 steps do NOT move the refactor's baseline**, because PR-B freezes `mode=legacy` and the recut's fidelity work is generic-only.

- **Refactor (Tracks 2,3) = byte-equivalence** against the **legacy-mode** frozen hash. A split must never change output.
- **scene-runtime (Track 1):** which steps move the *legacy* baseline:
  - **Slice T** — `contract_pipeline.py` is **generic-only** (`runtime_mode="generic"`) → legacy byte-identical, no baseline move. **Slice H** edits the domain classifier (`module_domain.py`), which feeds the *generic* host-emit; legacy script emission doesn't consult the domain verdict → expected baseline-neutral, **but confirm at re-baseline** (the classifier also runs in the classify phase). Both validated by the canary/Studio, not the hash.
  - **PR6 guard** — write_output gate; emits no new legacy content → baseline-neutral.
  - **PR7 default flip** — changes the *default* mode, not legacy output; **PR-B's explicit `mode=legacy` freeze is immune.**
  - **PR8** — deletes the FPS coherence packs, which run in **legacy** and rewrite SimpleFPS scripts → **moves the legacy SimpleFPS frozen hash.** This is the one ★ re-freeze.
- **Rule:** the frozen baseline is **owned by Track 1**; **PR8 is the sole re-freeze event.** Track 3 (both splits) is gated behind PR8, so it always rebases onto the post-PR8 freeze. Track 2 edits in parallel; whichever of {PR8, PR-H} lands **last** owns reconciling the SimpleFPS hash (PR8 updates the baseline as an intended change; an in-flight PR-H rebases onto it). No deadlock — `scene_converter.py` imports neither `pipeline` nor `scene_runtime_*`, so Track 2 is import-decoupled.

## Parallel review — 2026-06-04

- **Claude (adversarial):** found 3 real holes — pipeline split was wrongly gated behind PR6 (PR8 deletes `apply_scaffolding`/`scaffolding` that PR-D binds + rewrites `pipeline.py` 881 L); `plan_scene_runtime` phase is a third collision surface calling Slice H's files and is absent from PR-D's enumeration; harness ★ markers were on generic-only steps. All three verified against the repo and folded above.
- **codex:** unavailable (WSS transport down all attempts). Its five collision questions were instead answered by direct repo inspection: (1) PR8 is the last recut step → one gate suffices; (2) `test_offline_assembly` modes off `E2E_SCENE_RUNTIME_MODE` default `legacy` → PR7-immune; (3) `contract_pipeline` is `runtime_mode="generic"` → no legacy leak; (4) PR8 owns the re-freeze, no deadlock; (5) `scene_converter.py` imports neither `pipeline` nor `scene_runtime_*`; the only shared test (`test_pipeline_write_output_subphases`) is pipeline-side, already behind the PR8 gate. Re-run codex when connectivity returns for the formal second voice.

### Second pass (PR#177 extensive review) — corrections folded
A deeper repo-verified pass (Claude subagent + codex both blocked by infra; done by direct inspection) caught that the docs predated **PR#148's topology refactor**:
- **Slice H file was wrong** — `scene_runtime_domain.py` is a back-compat **shim**; the domain authority is `scene_runtime_topology/module_domain.py` (`:324` cites `HudControl` as the canonical mis-`excluded` example). Corrected throughout.
- **Slice H must satisfy the fail-closed `contract_verifier`** (PR#148 Phase 3): flipping the domain to client is insufficient — it must produce a coherent client domain + client-reachable container + LocalScript, or trip `client-in-server-container`/`server-localscript`. Added to the recut plan.
- **PR#127's refactor plan predates `scene_runtime_topology/` + `contract_verifier.py`** — both are clean, well-factored new modules (not mega-files), so **no new split target**, but the plan's file census should note them. Not a collision; a completeness note.

## Preserved from PR#127 (not redesigned)

All 9 locked eng-review decisions stand: `PHASE_FUNCS` dispatch table + `pipeline.run_phase()`; `(state, ctx, services)` + `PipelineServices` dataclass; themed `coherence/packs/*` split after a PR-E0 `@patch_pack(after=...)` audit; PR-B frozen-hash on `test_offline_assembly.py`; the per-PR done-criteria + failure-modes table. Two figures need refresh at re-baseline: `pipeline.py` LOC (PR-C/PR-D targets) and pack count (PR8 lands first → audit ~22, not 27).

## The one decision this changes — DECIDED 2026-06-04

PR#127's blanket hold was a **user decision, reaffirmed**. This plan keeps the hold for the two collision files (`pipeline.py` → after PR6; `script_coherence_packs.py` → after PR8) but **lifts it for `scene_converter.py`**.

**Decision (jiazou, 2026-06-04): Parallelize `scene_converter` now.** Track 2 (PR-B → PR-G → PR-H) runs in its own worktree concurrently with Track 1 (the scene-runtime recut). The two efforts touch disjoint files, so the reintroduced concurrency costs only review attention, not merge risk. This supersedes the blanket-hold reaffirmation of 2026-05-23 **for `scene_converter.py` only**; the collision-file gates stand.

## Immediate first action

`scene_converter.py` Track 2 and Track 1 are both unblocked, but **PR-B (frozen-hash baseline) is the prerequisite for any refactor split** — it is the safety net that proves PR-G/PR-H change no output. So the very first refactor step is: **re-baseline LOC + pack count, then land PR-B**, before PR-G starts. Track 1 (Slice T) can start immediately in parallel.
