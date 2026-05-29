# Slice 6 handoff → Slice 7

## The slice-6 rule (READ THIS FIRST)

**"Save raw facts, recompute conclusions."**

Slice 6 establishes the structural rule for the topology layer:

- **Raw facts** = inputs derived from authoritative artifacts that the
  run-site cannot reliably reconstruct on a later resume. PERSIST these.
  Examples: `caller_graph` (built from post-transpile bodies; absent
  when `transpilation_result is None`), `intrinsic_script_type` (stamped
  once at construction time), `script_id_by_name` / `lifecycle_roles`
  (function of source + scene/prefab YAML, but historically persisted
  alongside the planner artifact).
- **Conclusions** = anything derived from current operator inputs
  (`scene_runtime.domain_overrides`, `networking_mode`) + current
  source. DO NOT persist these. Recompute on every run.

`topology_inputs.domains` and `topology_inputs.reachability_requirements`
are **conclusions** — they depend on operator-editable inputs that can
change between runs. Persisting them creates a class of staleness bugs
that the slice-6 original implementation tried to patch field-by-field
across rounds 2 (preserve-on-resume), 3 (overlay re-apply), and 4
(snapshot + divergence gate). Each round added a new staleness carrier
and Codex found a new field that wasn't covered.

The rule eliminates the bug class by construction: nothing is
persisted, so nothing can go stale.

Provenance: parallel arch verification 2026-05-29 — Claude's `APPROVE`
(`/tmp/topology/rethink-verify-claude.md`) + Codex's
`APPROVE_WITH_AMENDMENT` (`/tmp/topology/rethink-verify-codex.md`).

## Codex amendment — two semantics points slice 7 inherits

### 1. The "unconstrained helper" fallback contract

When `topology_inputs.reachability_requirements` is empty (the
assemble-no-retranspile resume case: `dependency_map` is empty, so
`derive_reachability_requirements` returns `{}`), helpers are
**unconstrained**.

Slice 7's `_decide_script_container_from_topology` MUST treat
"helper not in `reachability_requirements`" as "fall back to the
legacy six-rule path / legacy/orphan default of `ReplicatedStorage`".

Do NOT default empty-reqs to `ServerScriptService` — that would be a
silent regression on resume. The accepted trade is: degraded
(unconstrained) routing on no-transpile resumes = byte-identical to
today's slice-5 behavior. This is the same trade slice 3 already
accepted for `caller_graph`.

Codex citation: "helpers not listed in `reachability_requirements` are
'unconstrained,' pipeline comments say slice 7 should fall back for
unconstrained helpers, legacy/orphan module routing defaults to
`ReplicatedStorage`, and `callers_of()` treats 'no callers' and
'missing from graph' identically. I do not see evidence for an
empty-req default to `ServerScriptService`."

### 2. `caller_graph` remains the explicit non-recomputable exception

`caller_graph` depends on the transpile-time `dependency_map` surface,
which is NOT guaranteed to exist on later resume points (when
`transpilation_result is None` and `dependency_map` is empty for
legitimate reasons). "Always-recompute" yields `{}` and erases a real
prior graph.

The slice-6 prepass already honors this exception via the
`resolve_caller_graph` helper (`build_topology.py`):

- When `state.transpilation_result is not None`, recompute from
  `dependency_map`.
- When `state.transpilation_result is None`, preserve the prior block
  from `scene_runtime["topology"]["caller_graph"]` (slice 3's
  `preserved_caller_graph` pattern).

Slice 7 inherits this behavior; do NOT change it. The exception is
explicit: "do not 'always recompute' a derived artifact when the
run site cannot reliably reconstruct the upstream analysis that
produced it."

`intrinsic_script_type` (slice 5) is a separate, parallel exception
for the same structural reason: "an immutable fact stamped at
construction time, and rehydration cannot recover it from
post-classifier `script_type`."

## What slice 7 inherits

### TopologyInputs (typed shape)

Defined in `converter/converter/scene_runtime_topology/module_domain.py`:

```python
class TopologyInputs(TypedDict):
    domains: dict[str, str]                       # script_id -> "client"/"server"/"helper"
    reachability_requirements: dict[str, str]     # script_id -> "ReplicatedStorage"/"ServerStorage"/"ServerScriptService" (or "EXCLUDED" for conflicts)
    lifecycle_roles: dict[str, str]               # script_id -> "auto_run"/"prefab_template"/etc.
    script_id_by_name: dict[str, str]             # RbxScript.name -> script_id
    caller_graph: dict[str, list[str]]            # callee_script_id -> [caller_script_id, ...]
```

### Call surface

`classify_storage(..., topology_inputs: TopologyInputs | None = None)`
is the consumer hook. Slice 6 plumbs the kwarg but `_decide_script_container`
ignores it (`_ = topology_inputs`) and the legacy six-rule sequence
still wins. Slice 7 flips the consumer:

```python
def _decide_script_container(s, *, ..., topology_inputs=None):
    if topology_inputs is not None:
        return _decide_script_container_from_topology(s, ..., topology_inputs)
    # legacy path
    ...
```

When the fork is flipped, slice 7 atomically removes:
- `_scripts_with_client_apis` / `_scripts_with_server_apis`
- `_CLIENT_ONLY_PATTERNS` / `_SERVER_ONLY_PATTERNS`
- `_apply_reachability_rule` (already marked `.. deprecated::` in slice 6)

### Pipeline wiring

`Pipeline._maybe_run_topology_prepass(scene_runtime)` (in
`pipeline.py`) returns `TopologyInputs | None`. Returns `None` when:
- `scene_runtime_mode == "legacy"`, OR
- `scene_runtime["modules"]` is empty/missing, OR
- `scene_runtime["__skip_domain_classifier__"]` is truthy, OR
- `rbx_place is None` / no scripts.

When non-None, the result is threaded directly into `classify_storage`.
The pipeline does this on EVERY run — including assemble-no-retranspile
resumes. There is no persistence and no resume short-circuit.

### Empty `reachability_requirements` semantics

On assemble-no-retranspile resumes, `dependency_map` is empty (set
only inside `transpile_scripts`). `derive_reachability_requirements`
returns `{}` in that case. This is the **intentional trade slice 3
already accepts** (same shape as preserved-caller_graph reasoning):
empty requirements → unconstrained helpers → slice 7 falls back to
legacy routing → byte-identical to today's slice-5 behavior on
resume. Not a regression — it's the existing baseline.

Document this trade prominently in the slice-7 `_decide_script_container_from_topology`
docstring. Do NOT add machinery to "preserve" reachability_requirements
across resumes — that's the exact bug class slice 6 rounds 2/3/4
generated and the rule eliminates.

## What slice 7's `_decide_script_container` is allowed to consume

From `topology_inputs`:
- `domains[script_id]` → drives client/server/helper container choice.
- `reachability_requirements[script_id]` → when present, MUST be honored
  (slice-4 triple semantics: ReplicatedStorage / ServerStorage /
  ServerScriptService / EXCLUDED-on-conflict).
- `caller_graph[script_id]` → "no callers" and "missing from graph"
  are equivalent (orphan; route to default container).
- `lifecycle_roles[script_id]` → gated decisions (`auto_run` for
  bootstrap, etc.).
- `script_id_by_name[s.name]` → THE lookup. Do not roll your own.

From the scripts themselves:
- `s.script_type` (or `s.intrinsic_script_type` for the immutable
  pre-coercion value — see slice 5 handoff).
- `s.parent_path` IS now writeable by `_decide_script_container` —
  the prepass already ran and is `parent_path`-free.

Do NOT consume:
- Regex-API matches (`_CLIENT_ONLY_PATTERNS` etc.) — these are
  superseded by `domains` and will be removed by slice 7.
- Persisted `topology_inputs` from `StoragePlan` — there is none.

## Open architectural questions for slice 7

1. **Server-only ModuleScript routing default** — deferred from
   slice 5. Claude said `ReplicatedStorage` (design doc); Codex said
   preserve legacy `ServerStorage`. Slice 7 implementer must decide
   at start.

2. **Reachability conflict policy** — when
   `reachability_requirements[id] == "EXCLUDED"` (helper required by
   both domains), slice 7 must pick: skip the helper, fall back to
   `ReplicatedStorage` (safer / lower-fidelity), or raise. Design
   doc says hoist to `ReplicatedStorage`. Confirm.

3. **Decision-tree test migration** — `test_storage_classifier.py`
   has 19 tests against the legacy six-rule sequence. When the
   legacy branch is deleted, decide per-test: preserve (assertion
   still holds under topology routing), adapt (replace regex-API
   fixture with topology-input fixture), or delete (regex-only
   coverage).

## Slice 6 final state (after rework)

- Branch: `feat/scene-runtime-topology-phase-2a-slice-6`.
- HEAD: rework commit on top of slice 5 (5 commits total since
  `feat/scene-runtime-topology-phase-2a-slice-5`).
- Tests: 2217 passed, 45 skipped, 5 xfailed.
- no-Any gate: clean.

### Architecture decisions made (with provenance)

- **Save raw facts, recompute conclusions** (the slice-6 rule). Provenance:
  parallel arch verification 2026-05-29.
  `/tmp/topology/rethink-verify-claude.md` (APPROVE) +
  `/tmp/topology/rethink-verify-codex.md` (APPROVE_WITH_AMENDMENT).
- **No persistence of `topology_inputs` to `StoragePlan`.** Eliminates
  the staleness carrier that drove R2/R3/R4 patches.
- **`_maybe_run_topology_prepass` always recomputes on every run** —
  no resume short-circuit, no snapshot, no divergence gate, no
  overlay re-application.
- **`caller_graph` exception preserved** — `resolve_caller_graph`
  honors `state.transpilation_result is not None` as the
  "did transpile run this invocation" signal. Provenance: slice 3
  round 5 + Codex amendment 2.
- **Slice-7 empty-reqs fallback contract documented** in this handoff
  (NOT in source comments). Provenance: Codex amendment 1.

### Process notes for slice 7

- The original slice 6 ran 4 review rounds (R1 → R2 → R3 → R4) and
  hit the budget cap. The rework collapses R2/R3/R4 into one
  structural rule. If a slice 7 review surfaces a new
  "X derived field can go stale" finding, the fix is to
  recompute X — not to add another persistence layer.
- Parallel Claude + Codex review is essential. Both verified
  independently against PR-branch files (not main).
- The rework is **pure subtractive shipping** — R1's pipeline
  split + kwarg-threading is the entire slice-6 net contribution.
- Slice 7 implementer: read this handoff before reading the
  R2/R3/R4 review history. The reviews are accurate about
  staleness bugs in the PERSISTED design; they're irrelevant under
  the rework rule.
