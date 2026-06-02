# Phase 3 Slice 1 — check A (consumer compliance): design brief

**For parallel arch review** (Claude design subagent + `codex exec`, same brief).
Stacked on PR #167 (slice 0). NOT yet approved.

## The ONE goal

> **Check A: prove each emitted script's placement is CONSISTENT with its
> independently-classified domain — catching the door-bug family where a
> script's deployment (script_type + container) contradicts the domain its
> source evidence implies.** Shadow-mode warnings only this slice (per-check
> flip is slice 4).

## The trap this brief exists to avoid (verify it)

The naive reading of "consumer compliance" is "emitted placement == topology's
placement decision." **That is tautological and would be green-for-the-wrong-
reason.** The topology artifact does NOT make an independent placement decision —
it MIRRORS the storage classifier's `RbxScript.parent_path`:

- **GF-A:** `_stamp_container_and_path` (module_domain.py:1666-1670) does
  `container = script.parent_path; module["container"] = container;
  module["module_path"] = f"{container}.{script.name}"`. And `classify_storage`
  (storage_classifier.py:283) sets `parent_path` BEFORE `_build_and_apply_topology`
  runs (pipeline.py: classify_storage at ~4580, topology build at ~4693). So the
  artifact's `container`/`module_path` is the live `parent_path` by construction —
  comparing them checks nothing.

The substantive, non-tautological signal is **`domain`**, which is derived
INDEPENDENTLY of placement:

- **GF-B (verify):** `classify_scene_runtime_domains` / `infer_module_domains`
  derives `domain` from SOURCE evidence (client/server API signals, dependency
  graph, networking annotations) — NOT from `script_type` or `parent_path`.
  Confirm domain derivation never reads `parent_path`/`script_type`.

So **check A = reconcile the independent `domain` against the emitted
(`script_type`, container-family of `parent_path`).** If they disagree, the
deployment is wrong (the bug class).

## Grounding facts (verify or refute each)

- **GF-C:** At the post-`_build_and_apply_topology` hook (slice 0's
  `_run_contract_verifier`), both are in hand: artifact `topology["modules"][sid]`
  carries `domain` + `script_class` (intrinsic) + `module_path`; live
  `self.state.rbx_place.scripts` carry final `script_type` + `parent_path`. The
  script_id↔RbxScript join is `script_id_by_name` (pipeline.py:5343-5347;
  storage_classifier.py:576) with a `class_name` fallback on collisions.
- **GF-D:** A LocalScript in ServerScriptService is ALREADY a hard
  `ConstraintViolation` raised in storage_classifier.py (~898-908) — so that
  exact combo aborts before reaching the verifier. Check A must NOT duplicate
  what the classifier already hard-rejects; it targets combos the classifier
  PERMITS but that contradict domain (e.g. a server-domain module emitted as a
  LocalScript in StarterPlayerScripts — permitted by the type-before-domain
  path, storage_classifier.py:719-720, the GF11 latent class).
- **GF-E:** `animation_drivers` entries also carry `domain` + `script_class` and
  their Anim_* scripts get placed. They are the original door-visual bug surface.
  Decide whether check A covers them (DQ3).
- **GF-F:** The doc's §"Background: storage ≠ domain" (lines ~67-110) enumerates
  LEGAL domain≠container cases that check A must NOT false-positive on:
  client-domain ModuleScript in ReplicatedStorage (case 1/2); server-domain
  ModuleScript in ReplicatedStorage for shared types (case 3); ReplicatedFirst
  only via loader intent (case 4); StarterCharacter vs StarterPlayer (case 5).
  ReplicatedStorage is a NEUTRAL container (requireable by either side).

## Decision options (vote on the same forks)

### DQ1 — check framing
- **A1 (recommended): domain⟂placement consistency.** Reconcile independent
  `domain` vs emitted (`script_type`, container-family). NOT a mirror comparison.
- **A2: mirror comparison** (parent_path == artifact container). Rejected as
  tautological per GF-A — listed only so reviewers can confirm the refutation.

### DQ2 — the consistency table (the substantive spec; pressure-test vs GF-F)
Proposed rules (violation = warning in slice 1). Container families:
SERVER_ONLY = {ServerScriptService, ServerStorage}; CLIENT_ONLY =
{StarterPlayer.StarterPlayerScripts, StarterPlayer.StarterCharacterScripts,
ReplicatedFirst}; NEUTRAL = {ReplicatedStorage}.
- **domain == server:** VIOLATION if `script_type == LocalScript` (never runs
  server-side) OR `parent_path` ∈ CLIENT_ONLY. (Script/ModuleScript in
  SERVER_ONLY or NEUTRAL = OK.)
- **domain == client:** VIOLATION if `script_type == "Script"` (auto-run) AND
  `parent_path` ∈ SERVER_ONLY (client logic auto-running on the server). LocalScript
  or ModuleScript in CLIENT_ONLY/NEUTRAL = OK. (A client-domain plain Script is
  itself suspect, but flagging script_type alone risks false positives — gate on
  the server-container combo.)
- **domain == helper:** VIOLATION if `script_type` ∈ {Script, LocalScript} (a
  helper is require-only; auto-run contradicts helper role) OR `parent_path` ∈
  CLIENT_ONLY. ModuleScript in NEUTRAL/SERVER_ONLY = OK.
- **domain == excluded:** no check (should be inert/dropped; out of scope).
Reviewers: is this table correct + free of GF-F false positives? Any combo
missing or over-broad? Should the client-domain rule also flag LocalScript in
SERVER_ONLY (or does GF-D already cover it)?

### DQ3 — scope: modules only, or also animation_drivers?
- **C1 (recommended): cover BOTH `modules` and `animation_drivers`.** The
  animation drivers ARE the door-visual surface; same domain⟂placement rule
  applies to their Anim_* scripts. Adds the anim-script join.
- **C2: modules only this slice;** animation_drivers as a slice-1 follow-on.
  Smaller, but leaves the original motivating bug surface unchecked longer.

### DQ4 — join robustness
On a stem/class_name collision the `script_id_by_name` join can miss (returns no
RbxScript). Options: (a) skip the module + emit an "unverifiable" info row (no
silent gap); (b) treat a missing join as its own violation. Recommended: (a) —
collisions are already surfaced elsewhere; a verifier shouldn't double-fail, but
must not silently skip (log/record the skip).

## What reviewers return
Per DQ1-DQ4: option letter + file:line-grounded rationale. Confirm/refute GF-A
(the tautology), GF-B (domain independence), GF-D (the hard ConstraintViolation
overlap). Pressure-test the DQ2 table against EACH §"storage ≠ domain" case for
false positives. Flag any 5th decision (e.g. what about modules with
`domain` absent/low-confidence? ReplicatedStorage-as-neutral assumption?).
