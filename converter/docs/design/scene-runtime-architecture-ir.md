# Scene-runtime topology authority

**Status:** in-flight (revised 2026-05-30 to reflect the Phase 2a slice 8
arch-review re-plan; the prior 2026-05-29 revision captured slice 5-7
decisions). Phase 1 shipped. Phase 2a is mid-flight: slices 1-7 shipped
(PRs #150, #151, #152, plus slice 7); slice 8 next; slices 9-11 follow.
**Owner:** unity2rbxlx converter team.
**Related:** `scene-runtime-contract.md` (the contract this layer implements),
`scene-runtime-domain-signals.md` (today's domain classifier, becomes the
`module_domain` slice of the new package), `scene-runtime-pr2-followups.md`
§6-7 (already-resolved planner follow-ups), GitHub issue #146, and the three
live-playtest bugs that motivated this plan (Player.Move on Part / HudControl
missing service-getter / door visual not opening).

## Goal

**Make `scene_runtime_topology/` the sole authority over deployment-affecting
decisions for every script the converter emits. Make every downstream emitter
a structurally bound consumer with no independent decision authority over
those fields.**

Today the converter independently decides:

- which domain a module runs in (`scene_runtime_domain.py`)
- where that module's script lands (`storage_classifier.py`, but also
  `scene_runtime_domain._stamp_container_and_path` /
  `_apply_reachability_rule`, and a re-derivation inside
  `pipeline._classify_storage`) — historical context as of pre-Phase-2a;
  slice 6 split `_apply_reachability_rule` into
  `derive_reachability_requirements` + `finalize_topology_containers`,
  and slice 11 deleted the now-dead original implementation
- what class the script gets (Script / LocalScript / ModuleScript — split
  across `storage_classifier`, `animation_converter`, and
  `code_transpiler._classify_script_type` plus a generic-mode override)
- where animation scripts live (always server `Script` via
  `animation_converter.py`, with no consultation of the driver's domain)
- cross-domain edges (`scene_runtime_domain.compute_cross_domain_edges`
  records them, but no consumer enforces communication shape)

Codex's review made the live split-brain concrete: `classify_scene_runtime_domains()`
decides `domain`, then `_apply_reachability_rule()` mutates
`parent_path/container/module_path` again; `_subphase_inject_scene_runtime()`
recomputes `cross_domain_edges`; `convert_animations()` independently decides
prefab-vs-scene animation placement. *Multiple authorities deciding
overlapping facts is non-reproducible builds, hidden divergence between
emitted Luau and the embedded plan, and host/runtime mismatches that contract
checks catch too late.*

The result: silent contract violations between layers. The user-visible
symptom is the door-visual bug, but the same architectural shape produces
the Anim_HostilePlane, Anim_PlaneHolder ×3, and the duplicated Anim_*
emissions — and will produce more bugs of the same family for any future
client-driven Animator pattern.

The fix is structural: ONE authority decides the topology; every other
phase is bound by it.

### Non-goals

- Owning C#→Luau translation semantics. That stays in `code_transpiler.py`
  + `_AI_SYSTEM_PROMPT`.
- Owning scene-graph hierarchy. That stays in `scene_converter.py`.
- Owning structural facts about which modules exist + their dependencies.
  That stays in `scene_runtime_planner.py` (which the topology layer
  consumes as read-only input).
- Becoming a general semantic IR. Codex's earlier review explicitly warned:
  *"if you let it become a giant omnibus IR, you risk freezing bad
  abstractions too early."* Scope is **deployment topology only**: domain,
  script class, container, lifecycle role, cross-domain edges, animation
  routing. Nothing else.

## Background: storage ≠ domain (concrete cases)

Storage classification is NOT a 1:1 derivation from domain. The mapping is
multi-input and deterministic, but not trivial. Concrete cases from this
codebase:

1. **ModuleScripts in `ReplicatedStorage` are requireable by either side.**
   Storage is neutral; execution context is determined by the caller. Today
   `Player.luau` is client-domain but lives in `ReplicatedStorage` because
   `SceneRuntimeClient` (the LocalScript in `StarterPlayer.StarterPlayerScripts`)
   requires it. Same container, different execution context.

2. **`HudControl.luau` and `GameManager.luau` both live in `ReplicatedStorage`.**
   Two client-domain ModuleScripts share a container. The choice of
   `ReplicatedStorage` vs `ServerStorage` depends on who needs cross-process
   reach, not on domain alone.

3. **Server-domain ModuleScript exposing shared types/constants.** Domain =
   server (lifecycle owned by server), but container = `ReplicatedStorage`
   so client code can also require for type references. Domain ≠ container
   by design.

4. **`ReplicatedFirst` is a placement HINT based on intent (loader/splash).**
   A client-domain LocalScript that loads bootstrap assets → `ReplicatedFirst`.
   A client-domain LocalScript that isn't a loader → `StarterPlayerScripts`.
   Same domain, different container, driven by intent.

5. **`StarterCharacterScripts` vs `StarterPlayerScripts`** are both
   client-domain. The split depends on scene wiring (was the script
   attached to the player character prefab in Unity?). Not derivable from
   domain.

6. **Cross-domain bridge = 4 coordinated scripts in 4 containers.** ONE
   semantic decision (server-authoritative state for X) → FOUR storage
   placements: client-side caller in `ReplicatedStorage`, server-side
   bridge listener in `ServerScriptService`, the RemoteEvent in
   `ReplicatedStorage`, and an animation listener somewhere. Per-script
   placement is the storage layer's job; the four-script grouping is a
   topology fact.

**So the storage mapping is many-to-many between domain and container, and
multi-input.** `script_storage.py` retains genuine multi-input decision
logic — not a dictionary lookup. What it does NOT do is re-classify domain
or re-derive what topology already decided.

## Architecture

### Producer/consumer boundary

```
scene_runtime_planner.py        ← STRUCTURAL TRUTH; runs first
                                  Produces: modules, instances, references,
                                  prefab/scene ownership, normalized
                                  module dependency graph.
                                  STAYS SEPARATE from topology.
        │
        ▼  (planner output is read-only input to topology)
        │
scene_runtime_topology/         ← TOPOLOGY AUTHORITY; runs during transpile
├── build_topology.py           ← coordinator: assembles + validates artifact;
│                                 single orchestration entry point
├── module_domain.py            ← C# signal detection + per-module domain
│                                 classification (today's scene_runtime_domain
│                                 minus the misplaced storage mutations)
├── animation_routing.py        ← per-animation driver-edge resolution +
│                                 domain inheritance from driver
├── cross_domain_edges.py       ← edge enumeration + bridge_group_id
│                                 assignment + bridge resolution metadata
└── lifecycle_roles.py          ← derive lifecycle_role (closed enum) per
                                  module from domain + class + intent hints

ARTIFACT EMITTED per module:    {domain, script_class, lifecycle_role,
                                 bridge_group_id?, animation_driver_ref?}
ARTIFACT EMITTED per anim:      {stable_id, driver_module_guid, domain,
                                 lifecycle_role, observed_attribute,
                                 bridge_group_id?}
ARTIFACT EMITTED globally:      cross_domain_edges with bridge resolution
                                 (event_name, direction, payload schema)
        │
        ▼  (topology artifact is read-only input to Phase 4B)
        │
Phase 4B consumers (structurally bound; NO independent topology decisions):
├── script_storage.py           ← BOUND DETERMINISTIC MAPPER; consumes
│                                 topology + caller_graph + character_attached
│                                 + name_hints; outputs specific Roblox
│                                 container per script. Retains real
│                                 multi-input decision logic. DOES NOT
│                                 re-classify domain.
├── animation_converter.py      ← rewrite to read animation_routing for
│                                 placement + emission shape
├── code_transpiler.py          ← rewrite to read domain + cross_domain_edges
│                                 for emission: FireServer for bridge writes,
│                                 SetAttribute for same-domain
└── contract_pipeline.py        ← Phase 3 verifier; enforces consumers
                                  obeyed the artifact + completeness checks

DELETED (logic absorbed by topology):
- storage_classifier._scripts_with_client_apis  (NB: slice 7 R3 RESTORED
  these as FALLBACK-PATH-ONLY infrastructure; the topology decision tree
  at _decide_script_container_from_topology consumes TopologyInputs.domains
  exclusively and never reads the regex sets. They are reached only when
  topology_inputs is None / sid lookup misses / the slice-6
  unconstrained-helper gate sends a ModuleScript to legacy fallback.)
- storage_classifier._scripts_with_server_apis  (same fallback-only contract
  as the client counterpart above)
- pipeline._classify_storage's classifier block (mis-layered re-derivation)
- scene_runtime_domain._stamp_container_and_path's AUDIT-SIGNAL WRITES
  (the parallel ``signals["reachability_forced_container"] = ...`` write
  was retired in slice 10; the function itself is retained as the canonical
  container + module_path writer for finalize_topology_containers --
  ``_stamp_container_and_path`` is still LIVE at module_domain.py:880/886/891)
- animation_converter's hardcoded ServerScriptService routing
- code_transpiler._classify_script_type's generic-mode override
```

### The topology artifact

Single artifact persisted into `conversion_plan.json`'s `scene_runtime`
block. Frozen and schema-checked at emit time.

```python
{
  "modules": {
    "<guid>": {
      "stem": "Door",
      "domain": "client" | "server" | "helper" | "excluded",
      "script_class": "ModuleScript" | "Script" | "LocalScript",
      "lifecycle_role": "auto_run" | "requireable" | "loader"
                        | "character_attached" | "bridge_listener"
                        | "scene_entrypoint",          # closed enum
      "bridge_group_id": "<edge_id>" | null,           # set when this module
                                                       # is part of a bridge
      "provenance": {
        "source_path": ".../Door.cs",
        "source_span": [12, 87]
      }
    }
  },

  "animation_drivers": {
    "<stable_id>": {
      "stable_id": "Door:door:open",  # <prefab>:<target_name>:<clip_name>
      "driver_module_guid": "<door.cs guid>",
      "domain": "client",              # INHERITED from driver
      "script_class": "Script",        # or LocalScript depending on domain
      "lifecycle_role": "auto_run",
      "observed_attribute": "open",
      "observed_target": {
        "kind": "sibling" | "child" | "descendant",
        "name": "door",
        "scope": "self.gameObject.Parent"
      },
      "bridge_group_id": "<edge_id>" | null
    }
  },

  "cross_domain_edges": [
    {
      "id": "<deterministic_edge_id>",     # used as bridge_group_id
      "kind": "attribute_write",
      "producer": { "guid": "<door.cs>", "domain": "client",
                    "module": "Door" },
      "consumer": { "stable_id": "Door:door:open", "domain": "server",
                    "script": "Anim_Door_door_open" },
      "payload": { "attribute_name": "open", "schema": "bool" },
      "resolution": {
        "strategy": "remote_event_bridge",
        "event_name": "Door_SetOpen",
        "direction": "client→server",
        "reliability": "fire_and_forget",  # vs "request_response"
        "validator": "default_trust"        # extensible per-game policy
      },
      "bridge_member_scripts": [             # the 4-script coordinated unit
        { "role": "client_caller",   "ref": "<door.cs guid>" },
        { "role": "server_listener", "ref": "<auto-generated bridge script id>" },
        { "role": "remote_event",    "ref": "ReplicatedStorage.Door_SetOpen" },
        { "role": "anim_listener",   "ref": "<stable_id>" }
      ]
    }
  ]
}
```

**Invariants enforced by `build_topology.py` at emit time:**

1. Every `animation_drivers[*].driver_module_guid` resolves to a `modules`
   entry; the animation's `domain` matches the driver's.
2. Every `cross_domain_edges[*]` has both producer and consumer with
   defined domains, and `resolution.strategy ∈ {remote_event_bridge,
   same_domain_no_bridge, excluded}`.
3. Every `Anim_*` script in the planned output corresponds to exactly ONE
   `animation_drivers` entry (no duplicates; structural via `stable_id`).
4. Every `lifecycle_role` is in the closed enum.
5. Every `bridge_group_id` in `modules` or `animation_drivers` refers to
   an existing `cross_domain_edges[*].id`.

Failures here ABORT the build with the offending input row + the
violated invariant. No warnings.

### `script_storage.py` — bound deterministic mapper

Consumes topology artifact + structural inputs. Decides specific Roblox
container per script. Hard constraints enforced AFTER decision (not mixed
into scoring).

**Inputs:**
- From topology: `domain`, `script_class`, `lifecycle_role`,
  `bridge_group_id`
- From planner: normalized caller graph (module dependency edges) — topology
  exposes this as a curated view; `script_storage` does NOT re-derive
  graph shape
- From scene_converter: `character_attached` flag (which scripts are
  attached to the player character prefab)
- From `code_transpiler`: ReplicatedFirst-name hints (loader/splash naming
  conventions)
- From topology: `entrypoint_kind` / `autorun_origin` flag (top-level scene
  entrypoint vs incidental auto-run script — added per Codex round-4)

**Decision tree (deterministic, first-match-wins, no re-classification):**

This ordering was reconciled across Claude + Codex review during the slice 6/7
arch-review cycle (2026-05-29). The previous draft of this tree referenced a
phantom `requires_server_storage_isolation` field on `TopologyModuleEntry`
that does not exist — under the literal-as-drafted tree, NO server-only
ModuleScript would have ever landed in `ServerStorage`, making the SS branch
dead code and the data-flow analysis cosmetic. The corrected rule is: trust
the analysis. If `topology_inputs` says a ModuleScript has only server-domain
callers, place it in `ServerStorage`. The classifier respects its inputs.

```python
def assign_container(script, topology_facts, structural_inputs):
    # 1. character_attached wins outright — Unity-character-prefab scripts
    #    only make sense in StarterCharacterScripts.
    if topology_facts.lifecycle_role == "character_attached":
        return STARTER_CHARACTER_SCRIPTS

    # 2. Loader intent → ReplicatedFirst. Never inferred from client-domain
    #    alone; only when topology stamped lifecycle_role == "loader".
    if topology_facts.lifecycle_role == "loader":
        return REPLICATED_FIRST

    # 3. Reachability-required container (e.g. a Player module reachable from
    #    StarterPlayerScripts must live in ReplicatedStorage). The sentinel
    #    "__excluded__" routes excluded-domain modules to ReplicatedStorage
    #    so they stay parseable but never auto-run.
    if topology_facts.reachability_required_container is not None:
        return topology_facts.reachability_required_container

    # 4. ModuleScript by caller-domain (consumes topology_inputs.caller_graph
    #    via topology_facts).
    if script.script_class == "ModuleScript":
        caller_domains = topology_facts.caller_domains  # already resolved
        if "client" in caller_domains:
            # Any client caller → ReplicatedStorage (cross-process reach).
            return REPLICATED_STORAGE
        if caller_domains == {"server"}:
            # Server-only ModuleScripts → ServerStorage. Faithful to the
            # domain analysis. NO phantom guard; if topology says
            # server-only, we trust it.
            return SERVER_STORAGE
        # Orphan / unknown → conservative default.
        return REPLICATED_STORAGE

    # 5. Auto-run script classes.
    if script.script_class == "LocalScript":
        return STARTER_PLAYER_SCRIPTS
    # script.script_class == "Script"
    return SERVER_SCRIPT_SERVICE

def enforce_constraints(script, container):
    # Hard constraints AFTER decision (not mixed into the tree).
    if script.script_class == "LocalScript" and container == SERVER_SCRIPT_SERVICE:
        raise ConstraintViolation(...)  # would never run
    if container == REPLICATED_FIRST and script.script_class == "ModuleScript":
        raise ConstraintViolation(...)  # ReplicatedFirst is for executable scripts
    # ... and other invariants ...
```

**Note on `character_attached`:** previously modeled as a `structural_inputs`
flag. Slice 5 moved it onto `lifecycle_role` (closed enum value
`"character_attached"`), so the decision tree consults topology only — no
out-of-band structural flag needed for this branch.

What MOVES OUT to topology:
- `_scripts_with_client_apis` regex (duplicates `module_domain` signal
  detection) — **restricted to fallback path only** (slice 7 R3). The
  topology decision tree never reads it; it is consumed only by
  `_decide_script_container_legacy` when the slice-6 unconstrained-
  helper gate (`storage_classifier.py:575-587`) sends a ModuleScript
  to the legacy six-rule path on no-transpile resume.
- `_scripts_with_server_apis` regex — same fallback-only contract
- Implicit re-derivation of domain via API analysis — deleted

What STAYS in `script_storage.py`:
- The decision tree above
- Caller-graph traversal (using the curated dependency graph topology exposes)
- Constraint enforcement (LocalScript-in-SSS validation, ReplicatedFirst
  class check, etc.)
- Name-hint loader detection (combined with topology's lifecycle_role hint)

## Persistence rule: save raw facts; always recompute conclusions

This rule was adopted during the slice 6 review cycle (2026-05-29) after four
rounds of whack-a-mole staleness bugs — operator input changes between runs
silently invalidated a persisted derived artifact, and the consumer had no way
to detect it.

**The rule:**

- **PERSIST raw facts.** Things that come from outside the converter and cannot
  be recomputed from current inputs, or things that come from an expensive
  one-shot process that runs at a specific pipeline stage:
  - `RbxScript.intrinsic_script_type` (slice 5) — stamped at transpile time.
  - Uploaded asset IDs.
  - Mesh hierarchies (from Roblox `LoadAsset`).
  - Transpile cache (C# → Luau).
  - `caller_graph` — **explicit exception, see below.**
- **DO NOT PERSIST derived conclusions.** Anything that is a deterministic
  function of current inputs + raw facts:
  - `topology_inputs` (all of: `domains`, `reachability_requirements`,
    `lifecycle_roles`, `script_id_by_name`).
- **ALWAYS RECOMPUTE on resume.** `topology_inputs` is recomputed from current
  operator inputs + intrinsic facts on every pipeline entry. The
  "assemble-no-retranspile" case yields an empty `reachability_requirements`
  — this is the same trade slice 3 already accepts, and the
  unconstrained-helper fallback (below) handles it gracefully.

**Why `caller_graph` is the explicit exception.** `caller_graph` depends on the
transpile-time dependency_map. On a no-transpile resume that map is absent, so
recomputing would yield an empty graph. Preservation yields strictly better
fidelity than always-recompute for this one structure. The rule allows it as a
named exception, not as a precedent: any future "persist this derived value
because it's expensive" proposal must clear the same bar (cannot be recomputed
without a transpile-stage input, and preservation strictly dominates
recomputation in fidelity).

### `TopologyInputs` shape — `transpile_ran: bool`

`TopologyInputs` carries a `transpile_ran: bool` raw fact set from
`state.transpilation_result is not None`. This is rule-safe (it's a raw fact
about pipeline execution, not a derived conclusion) and lets the consumer
distinguish two structurally-identical "empty `reachability_requirements`"
cases:

1. `transpile_ran is False` (no-transpile resume): empty is expected. Fall
   back to legacy six-rule path for any script not covered by topology.
2. `transpile_ran is True`: empty is a real classification bug. Raise/log.

### Unconstrained-helper fallback contract

When `topology_inputs.reachability_requirements[sid]` is absent **AND**
`topology_inputs.transpile_ran is False`, fall back to the legacy six-rule
path for **that script only** — not the whole pipeline. This is the Codex
amendment from the slice 6 arch review: per-script fallback keeps the rest of
the consumer wired through topology, so we don't lose the fix surface for
scripts that ARE covered.

When `transpile_ran is True` and a script is missing from
`reachability_requirements`, the consumer raises — the legacy fallback is
NOT engaged, because empty under those conditions indicates a real bug we
want loud.

## High-priority followup TODOs (tracked, not in slice plan)

These are tracked tasks surfaced during slice 5-8 review and are NOT part of
the in-flight slice's deliverable. Listed here so future readers know they
exist:

1. **`infer_module_domains` misclassifies Roblox-dead Unity rendering
   helpers.** Unity-only render helpers (`WaterBase`, `PlanarReflection`,
   `Displace`) currently classify as server-only. They have no Roblox runtime
   equivalent and should be filtered. Investigate whether a dedicated
   dead-code pruning pass is required, or whether the existing domain-signal
   detector can be tightened. (Task #8.)
2. **Transpiler dependency-analysis false positives.** Example: `Plane.cs` is
   getting an injected `require(GameManager)` despite no reference to
   GameManager in the C# source. The dependency analyzer is over-counting.
   Investigate root cause before this lands a wrong-classifier bug onto a
   downstream consumer. (Task #9.)
3. **Symmetric `class_name`-collision fix at
   `build_topology._build_modules_block:529`.** Surfaced during the slice 8
   arch review alongside the slice 9 recompute work. **Consider folding into
   slice 9** since slice 9 is already touching that code path; flag in
   slice 9's brief. (Task #10.)

## Phase plan

### Phase 1 — Introduce the topology package + migrate `animation_converter` + dedupe Anim_*

Smallest painful surface; tests the thesis on the most-broken consumer.

**Deliverables:**

1. **New `scene_runtime_topology/` package** with the 5 sub-modules
   sketched above. Most of the logic in today's `scene_runtime_domain.py`
   moves into `module_domain.py` (verbatim, then refactored slice by
   slice). `cross_domain_edges.py` is today's `compute_cross_domain_edges`
   verbatim, extended with `bridge_group_id` and resolution metadata.
   `animation_routing.py` is new (today's animation→script mapping is
   scattered across `animation_converter` and never explicit).
   `lifecycle_roles.py` is new but derives from existing facts.
   `build_topology.py` is the coordinator that calls each slice in order
   and assembles the artifact.

2. **`animation_converter.py` migrated** to consume `animation_routing`.
   When generating an animation script, it looks up
   `animation_drivers[stable_id]` and emits with the prescribed
   `script_class` + `domain`. No hardcoded `ServerScriptService` routing.

3. **Anim_* dedupe falls out structurally.** `stable_id` is the unique
   identity; emitting from `animation_drivers` means at most one script
   per id. The two known duplication sites in `pipeline.py`
   (`_attach_prefab_scoped_animation_scripts_to_templates` +
   `_attach_monobehaviour_scripts_to_templates`) are rewritten to ALSO
   consult the artifact — not as a separate cleanup pass.

4. **Lightweight invariant checks at emit time** — the five invariants
   listed under "topology artifact" above. Abort the build on violation.

5. **Phase 1 ships with the topology package as authority ONLY for
   animation script placement.** `module_domain` and `cross_domain_edges`
   functionally unchanged in behavior; just relocated into the package
   for structure. `script_storage` consumers in `pipeline._classify_storage`
   are NOT yet rewritten — they continue working as today. This keeps
   Phase 1 a structural refactor + animation fix, not a behavior change
   for other consumers.

**What this resolves:**

- Bug 3 (door visual): animation script gets placed in correct domain.
- All six known broken `Anim_*` scripts (Door open/close, HostilePlane
  Flying, PlaneHolder ×3) — same fix.
- Anim_* duplicate emission (currently 2 instances each in the converted
  output).

### Phase 2a — `script_storage.py` becomes bound consumer (placement-only)

Lower risk than transpile-emit changes; an earlier checkpoint per Codex's
round-1 review. Phase 2a is split into **11 slices** (revised from the
original 10-slice plan during the slice 5-7 review cycle: original slice 5
was re-scoped because its premise was wrong, and original slice 6 was split
into 6 + 7 once the pipeline-extraction surface area grew; slices 8-11 were
re-planned 2026-05-30 after the slice 8 arch review found the original
"move classify earlier" infeasible — see revision history).

- **Slices 1-4** (shipped, PR #150 at HEAD `fea6ec7`): foundation —
  planner helpers, canonical `class_name` helpers, `caller_graph`,
  schema-compat fixture.
- **Slice 5** (shipped, PR #151): structural prep — added immutable
  `RbxScript.intrinsic_script_type` field stamped at transpile time.
  Did NOT do the originally-planned pipeline reorder; the premise was
  wrong and the reorder was superseded by slice 6.
- **Slice 6** (shipped, PR #152): pipeline split — extracted
  `infer_module_domains` + `derive_reachability_requirements` +
  `finalize_topology_containers` from `classify_scene_runtime_domains` so
  they run **before** `classify_storage`. Threaded the new `topology_inputs`
  kwarg through `classify_storage` (legacy path still wins when `None`).
  Crucially, slice 6 did NOT persist `topology_inputs` — see the
  "Persistence rule" section below.
- **Slice 7** (shipped): `_decide_script_container` rewrite consuming
  `topology_inputs`. This is where the corrected decision tree above
  became live code.
- **Slice 8** (next): introduce a new `materialize_and_classify` pipeline
  phase. Lift `_subphase_emit_scripts_to_disk`, `_subphase_cohere_scripts`,
  and `_classify_storage` out of `write_output` into a new phase running
  between `convert_animations` and `convert_scene` (or wherever the
  dependency graph places it cleanly). Goal: make slice 6's "save raw
  facts, recompute conclusions" rule visible in the phase graph — emit +
  cohere produce the authoritative script set; classify produces the
  storage plan; both run upstream of `write_output`.

  **Acceptance gate (CRITICAL):** golden-output diff over bundled test
  projects (SimpleFPS, Gamekit3D, RedRunner, 3D-Platformer at minimum)
  shows **zero** `parent_path` drift on autogen / runtime-injection
  scripts. Autogen scripts (`GameServerManager`, `CollisionGroupSetup`,
  `NavAgent`, `EventSystem`, `CinemachineRuntime`, etc.) get appended
  AFTER classify today; the lift means they're either never classified or
  need a follow-on classify-pass before `write_output`.

  The autogen-classify gap is the structural risk. Slice 8 must either
  (a) move autogen-script construction earlier so they get classified by
  the lifted phase, or (b) add a small post-emit classify-pass in
  `write_output` for late-appended scripts. **Option (a) preferred;
  (b) is the safety net.**

- **Slice 9** (after slice 8): refactor
  `build_topology._build_modules_block` (lines 567-602 today) to recompute
  `module_path` / `reachability_forced_container` from canonical helpers
  + raw inputs instead of reading mutated `scene_runtime.modules[*]`
  fields. This is the structural prep for slice 10's deletion and must
  come **before** slice 10. Per Codex's slice 8 finding incorporated into
  this revised plan.

  **Split into 9a (unambiguous plumbing + #10 fold-in) and 9b (recompute,
  deferred for an A/B decision).** Pre-implementation synthesis (parallel
  Claude + Codex) found that naive recompute of
  `reachability_forced_container` is NOT provably row-equivalent: today's
  stamp is gated by `current_container not in
  _SERVER_CONTAINERS_FOR_REACHABILITY` (`module_domain.py:935-948`), so a
  helper already in `ReplicatedStorage` gets an empty signal today but
  would get a non-empty value under naive recompute from
  `reachability_requirements[sid]`. The semantic-shift question
  ("rule applies" vs "rule fired") is genuine; defer the A/B call until
  9a ships and we have code-level evidence.

  - **Slice 9a** (next): plumb `topology_inputs` through
    `_build_and_apply_topology` (`pipeline.py:4587`, called at
    `pipeline.py:4344`) — currently the prepass produces
    `topology_inputs` but the apply step doesn't receive it. Persist
    for `--phase=write_output` resumes (mirroring slice 5/6 patterns;
    `topology_inputs` itself stays out of `StoragePlan` per the slice 6
    persistence rule — plumbing means in-memory pass-through plus the
    existing `transpile_ran is False` resume path). **Fold in followup
    task #10** (symmetric `class_name`-collision fix at
    `build_topology._build_modules_block:529`): invert
    `build_script_id_by_name` into a `script_by_sid` map and consume it
    in `_build_modules_block` (same pattern as slice 7 R4 at
    `pipeline.py:4343-4348`). **No recompute** of
    `reachability_forced_container` or `module_path` yet — byte-equivalence
    of `_build_modules_block`'s output for those fields must be preserved.
    Estimated ~150-200 LOC, 3-4 review rounds.
  - **Slice 9b** (NEXT — Option C: drop the field).

    Drop `reachability_forced_container` from `TopologyModuleEntry`.
    The field is set ONLY when the late hoist rule fires
    (`module_domain.py:935-948`) and is read by exactly two sites: a
    trivial copy in `_build_modules_block:623-644` and an invariant-10
    lockstep check in `_enforce_invariants:1344-1367`. The invariant is
    tautological — same loop sets both fields from the same source. No
    runtime / storage / transpile / output decision depends on this
    field; `reachability_required_container` already carries the full
    semantic ("this module needs to be in container X").

    **Verification**: parallel Claude + Codex audits both independently
    arrived at Option C with the same key citations. The original
    Codex-discovered semantic gap (`reachability_forced_container` is
    gated by `current_container not in
    _SERVER_CONTAINERS_FOR_REACHABILITY`) is no longer a problem because
    the field itself is removed.

    **Deliverables**:
    - Remove `reachability_forced_container` from `TopologyModuleEntry`.
    - Remove the invariant-10 lockstep check at
      `build_topology.py:1350-1368`.
    - Update or delete the tests that exist purely to assert the field
      is set: `test_scene_runtime_topology.py:326-428, 1139-1227`,
      `test_module_domain_prepass.py:1184`,
      `test_scene_runtime_domain*.py:347/712`.
    - Fold in the slice 9a R1 degenerate-fixture corner: hoist
      `scripts == []` check above `assert topology_inputs is not None`
      at the new call site, OR conditional the assert.

    After slice 9b, slice 10 inherits a **two-ended** scope (NOT just
    write-deletion as originally framed): see the slice 10 entry below.

    Estimated 2-3 review rounds; net diff likely slightly negative
    (more deletions than additions).

- **Slice 10** (after slice 9): **two-ended**, not just write-deletion.
  Both slice 9b reviewers (Claude + Codex) and the implementer
  independently identified that `build_topology._build_modules_block:629`
  STILL reads `domain_signals["reachability_forced_container"]`
  (the planner-row audit signal) as the SOURCE for the surviving
  `reachability_required_container` field on `TopologyModuleEntry`.
  Slice 10 must therefore:

    1. **Switch the read site** at `_build_modules_block:629` from
       `domain_signals["reachability_forced_container"]` to a different
       source. Options to be picked by slice 10's arch review:
       - **Option a:** read from `module_row["container"]` if it
         already captures the needed semantic.
       - **Option b:** recompute from `reachability_requirements[sid]`
         (which is the underlying analysis output anyway).
    2. **Then retire the writes** of the planner-row audit signal at
       `scene_runtime_topology/module_domain.py:955` and `:1743`
       (formerly numbered `:880, :886, :891` pre-slice-9b). The order
       matters: switching the read MUST land before deleting the writes,
       otherwise the surviving `reachability_required_container` field
       loses its source.
    3. **Migrate external tests** that assert the planner-row audit
       signal directly: `test_module_domain_prepass.py:1184`,
       `test_scene_runtime_domain.py:347`,
       `test_scene_runtime_domain_v2.py:712`. They'll need migration or
       deletion depending on what slice 10's arch review picks for #1.

  Depends on slice 9b (the field is dropped from `TopologyModuleEntry`).

- **Slice 11** (last, **shipped 2026-05-30**): cleanup-only close-out.
  Three independent edits picked by parallel Claude + Codex arch review:

    1. **Fix two P3 pin-test gaps from the slice 10 handoff.**
       (a) Rewrite `test_storage_classifier_routing_unaffected_by_resume_empty_required_container`
       to drive `classify_storage` (the OUTER gate) instead of probing
       `_decide_script_container_from_topology` directly — the inner
       probe bypassed the slice-6 unconstrained-helper short-circuit at
       `storage_classifier.py:575-587` and asserted a property the
       production path does NOT exhibit on a no-transpile resume.
       (b) Delete the tautological
       `test_transpile_ran_flag_is_plumbed_but_not_consulted_at_artifact_read_site`
       (called `build_topology()` twice with identical args; the
       function has no `transpile_ran` kwarg) and replace it with a
       producer/consumer pin in `test_storage_classifier.py` asserting
       `TopologyInputs.transpile_ran` flips the gate.
    2. **Delete dead `_apply_reachability_rule`** (~130 LOC) at
       `module_domain.py:1623-1753` plus the shim import at
       `scene_runtime_domain.py:47`. No callers since slice 6; the
       deletion was promised in the function's own `.. deprecated::`
       docstring and deferred for hypothetical flag-day safety.
    3. **Patch this design doc** to match shipped reality (regex
       helpers RESTORED as fallback-only in slice 7 R3;
       `_stamp_container_and_path` retained as canonical container+path
       writer, only its audit-signal writes retired in slice 10).

  Stale-test cleanup retired
  `test_reachability_pair_populated_when_rule_fired` (seeded the
  removed planner-row audit signal directly) and dropped the audit-
  signal tail of `test_planner_rule_invisible_to_empty_name_scripts`.
  All `_scripts_with_client_apis` / `reachability_forced_container`
  mentions in surviving tests are **negative-assertion regression
  guards** (pin the slice 7 R3 fallback-only contract or the slice 9b
  field-removal) and were INTENTIONALLY kept.

**Dropped from the original plan:** the original slice 10 — "wire
remaining consumers (incl. `contract_pipeline`) through the persisted
topology artifact" — is **not** in the revised plan. Codex flagged that
`contract_pipeline` runs during `transpile_scripts` BEFORE the artifact
exists, so the "wire through artifact" idea doesn't fit. The contract
verifier work moves to Phase 3 where it's actually scheduled.

**Deliverables (intent across all 11 slices):**

1. **Rewrite `_decide_script_container` to read topology facts.** The
   regex-based API detection (`_scripts_with_client_apis`,
   `_scripts_with_server_apis`) is **restricted to the legacy fallback
   path**; the topology decision tree does NOT consume them. Slice 7
   round 1 deleted the helpers outright, but slice 7 R3 RESTORED them
   as fallback-only infrastructure after the deletion silently degraded
   the legacy path (the slice-6 unconstrained-helper gate at
   `storage_classifier.py:575-587` and the `script_id_by_name` miss
   path both fall through to `_decide_script_container_legacy`, which
   needs the regex sets). The contract is pinned by
   `test_storage_classifier_client_only_patterns_restored_as_fallback_only`
   in `test_scene_runtime_domain.py`. The decision tree above replaces
   the current six-rule sequence on the topology path. (Slice 7
   shipped + slice 7 R3 amendment.)

2. **Lift emit + cohere + classify out of `write_output`** into a new
   `materialize_and_classify` phase. Classify produces the storage plan
   upstream of `write_output`; `write_output` consumes persisted
   decisions, doesn't recompute. This addresses Codex's round-4 critique:
   *"deciding storage during write_output is late and likely wrong."*
   (Slice 8 — replaces the original "move `_classify_storage` out of
   `write_output`" plan with the broader phase-introduction that also
   carries emit + cohere with it.)

3. **Refactor `build_topology._build_modules_block` to recompute from
   canonical helpers** instead of reading stamped mutations. (Slice 9.)

4. **Retire the planner-row audit-signal writes** at
   `module_domain.py:955` and `:1743` (the parallel
   ``signals["reachability_forced_container"] = ...`` mutation).
   `_build_modules_block` now sources `reachability_required_container`
   from `TopologyInputs.reachability_requirements` normalized through
   the late-hoist predicate gate, NOT from the retired audit signal.
   (Slice 10, two-ended after slice 9b R1 review.) NOTE: this
   deliverable's earlier framing -- "delete the three
   `_stamp_container_and_path` sites" -- was retired. The function
   ITSELF (``module_domain.py:1603-1616``) remains LIVE as the
   canonical container + module_path writer for
   `finalize_topology_containers` (call sites at
   `module_domain.py:880, 886, 891`); only its audit-signal
   side-write was removed. "Placement decisions live in
   `script_storage.py` exclusively, topology emits facts only" is a
   future-phase north star, not a shipped Phase-2a deliverable.

5. **Schema-compat test** (Codex round-1, formalized round-3): a frozen
   fixture artifact in `tests/fixtures/topology/` consumed by
   `animation_converter`, `script_storage`, and (later) the contract
   verifier — all parsed and acted upon WITHOUT rerunning planning.
   Guards against planner/consumer drift. (Initial fixture shipped in
   slice 4; final test migration in slice 11.)

**What this resolves:**

- Single decision site for storage (eliminates Codex's "split-brain"
  critique structurally, not just by convention).
- The implicit Bug 2 mitigation: once topology owns `lifecycle_role` and
  the artifact carries it, the cross-script require-injector ordering
  becomes a single emit step driven by topology, not an independent
  pipeline pass.

### Phase 2b — bridge emission (post-transpile rewriter, edge-driven)

Higher-risk codegen behavior change; gated behind Phase 2a's checkpoint.
Architecture finalized 2026-05-30 after Claude+Codex parallel adversarial
review (`/tmp/topology/phase2b-adversarial-{claude.md,codex-raw.log,synthesis.md}`).
The earlier framing ("transpile-emit bridge code generation… AT EMIT TIME")
was structurally infeasible — see "Path C decision" below.

**Path C decision (Claude+Codex unanimous, 2026-05-30):** edges are
DERIVED across two stages (structural pre-transpile, enriched post-transpile)
and EMITTED post-transpile. NOT consumed inside the AI prompt at transpile
time. Rationale: the topology prepass that resolves `domains`,
`script_id_by_name`, and `bridge_member_scripts` runs inside
`_classify_storage`, which depends on `convert_scene` + `_subphase_emit_scripts_to_disk`
+ `_subphase_cohere_scripts` (`pipeline.py:2566-2618`,
`scene-runtime-pr4-followups.md:600-626`). Reordering would re-architect
`materialize_and_classify`'s entire phase contract. Path A (post-AI
rewriter ONLY) is too narrow to cover the pickup case; Path B (pre-transpile
move) is structurally blocked.

**Deliverables:**

1. **Two-stage edge derivation, both stages inside `_maybe_run_topology_prepass`.**
   - **Producers** (`compute_cross_domain_edges` +
     `compute_shared_attribute_candidates`) enumerate edge candidates
     from (a) `target_kind=="component"` peer-MonoBehaviour serialized
     references in `scenes/prefabs.references`, AND (b) a new
     `shared_attribute` candidate kind seeded from `SHARED_ATTRIBUTE_SEEDS`
     (the structural pre-transpile equivalent of the hardcoded
     `PlayerSetSharedFlag` allowlist at `code_transpiler.py:1267-1288`).
     Both kinds emit `kind: "attribute_write"`. Slice 1 placed producers
     inside `build_topology`; **slice 2 relocates them into
     `_maybe_run_topology_prepass`** so enrichment runs in the same scope
     with `TopologyInputs` available.
   - **Enrichment** resolves `bridge_member_scripts` and finalizes
     `resolution.event_name` for each candidate. The duplicate-`event_name`
     invariant scans BOTH buckets HERE (component-ref edges in
     `cross_domain_edges` + fan-out candidates in
     `cross_domain_edge_candidates`) — names aren't known until enrichment
     and a collision across buckets is just as bad as within. Lives in
     `_maybe_run_topology_prepass` per the slice 2 Q2 call.
   - **`TopologyInputs` grows two new fields** (slice 2): `cross_domain_edges:
     list[CrossDomainEdge]` and `cross_domain_edge_candidates: list[CrossDomainEdge]`.
     `build_topology` becomes pure-assembly, reading edges from
     `TopologyInputs` instead of calling producers itself.
   - **Two-bucket separation preserved** (slice 2 Q1 = B, unanimous):
     `cross_domain_edges` holds fully-resolved single-consumer edges;
     `cross_domain_edge_candidates` holds fan-out / multi-consumer edges
     with empty `to_*` but populated `bridge_member_scripts`. Invariant
     2 (`build_topology.py:1316-1330`) stays narrow on
     `cross_domain_edges`; slice 2 adds a candidate-specific invariant
     validating `bridge_member_scripts[*].ref` resolves to a real
     script_id.
   - **Consumer discovery for fan-out** (slice 2 Q3 = A): reuse the
     existing post-transpile Luau-scan pattern from
     `shared_state_linter.py:24,103`. Scan
     `state.transpilation_result.scripts[*].luau_source` for
     `:GetAttribute("has...")` patterns matching the seed table; the
     matched script ids populate `bridge_member_scripts`. This is the
     canonical existing mechanism for attribute-read discovery in this
     codebase; reusing it avoids inventing new regex on AI output.
   - **Synthesized server-listener id scheme committed in slice 2** so
     slice 3's emitter honors the same shape: a helper
     `bridge_emit.synthesize_listener_id(event_name) -> str` lives with
     slice 2's enrichment and is RE-IMPORTED by slice 3's emitter. One
     function, two callers; no silent mismatch.

2. **Post-transpile, pre-pack bridge emitter.** New module
   `scene_runtime_topology/bridge_emit.py`. Runs in
   `_subphase_cohere_scripts` BEFORE `run_packs` (or as its own subphase
   between cohere and pack). For every edge with `resolution.strategy ==
   "remote_event_bridge"`, rewrites the producer script's
   `SetAttribute(name, v)` to `<event_name>:FireServer(target, v)`.
   Idempotent: rows whose source already has the canonical
   `:FireServer(target, v)` shape are skipped. Gated on
   `self.state.transpilation_result is not None` (fresh transpile only).

3. **Auto-generated server-side bridge listener + RemoteEvent.** For
   each `remote_event_bridge` edge, synthesize a server `Script` in
   `ServerScriptService` whose body installs `OnServerEvent` →
   `target:SetAttribute(field, v)`. Stamp the corresponding
   `RemoteEvent` in `ReplicatedStorage`. `bridge_member_scripts[*]`
   names the synthesized script id; `lifecycle_role: bridge_listener`
   (forward-allocated at `lifecycle_roles.py:50`) tags the row.

4. **PickupItemEvent name locked for the SimpleFPS pickup edge.** Slice
   1 sets `resolution.event_name = "PickupItemEvent"` for that edge
   verbatim, preserving the literal string. The 3 downstream
   hardcoded sites (`pickup_remote_event_client` regex detector
   `packs.py:780-783`; `pickup_visual_target` template `packs.py:1167-1189`;
   client listener pack `packs.py:1032-1079`) continue to match without
   migration. Future cleanup migrates those sites to consume the edge
   artifact directly; Phase 2b does NOT.

5. **autogen.py scope: PlayerSetSharedFlag migrates, PlayerShoot and
   PlayerGetItem stay.** `autogen.py:147-177` keeps owning the two
   player-input RemoteEvents (`PlayerShoot`, `PlayerGetItem`) — those
   are input-domain events, not cross-domain attribute bridges, and
   don't fit the edge model. Phase 2b only retires the
   `PlayerSetSharedFlag` block by routing its writes through the new
   bridge emitter. The `_GENERIC_RUNTIME_PROMPT` hardcoded special case
   for `PlayerSetSharedFlag` (`code_transpiler.py:1267-1288`) is
   deleted in slice 3 — its semantics move to the topology-driven
   emitter.

6. **`pickup_remote_event_server` retires; walk-up `GetAttribute`
   compensation is DETANGLED first.** The pack mixes (a) cross-domain
   bridge rewriting with (b) AI-bug compensation (walk-up
   `GetAttribute("itemName")` lookups at `packs.py:849-908`). (b) is
   NOT bridge work — it's a Pickup-specific AI repair pass that
   survives the retirement. Slice 4 extracts (b) into a new pack
   `pickup_attribute_walkup` (or into the AI reprompt loop) BEFORE
   slice 5 deletes the bridge logic.

**5-slice plan (Codex producer-split philosophy):**

| Slice | Scope | Rounds |
|---|---|---|
| 1 | Edge schema extension (`kind: "attribute_write"`, `resolution`, `bridge_member_scripts`) + pre-transpile structural candidate pass (both component-ref and shared_attribute candidates, latter seeded from `PlayerSetSharedFlag` data). No emit, no consumer. Persisted-bytes change only in `conversion_plan.json`. | 2 |
| 2 | **Producers RELOCATE from `build_topology` into `_maybe_run_topology_prepass`.** Enrichment runs there too. `TopologyInputs` grows `cross_domain_edges` + `cross_domain_edge_candidates`. `build_topology` becomes pure-assembly. Consumer discovery via `shared_state_linter` Luau-scan reuse. Duplicate-`event_name` invariant scans BOTH buckets. Candidate-specific invariant: `bridge_member_scripts[*].ref` resolves to a real script_id. P3 carry-forward from slice 1: unify producer iteration order; tighten `_derive_event_name_from_owner_field` on empty field. No emit. | 2-3 |
| 3 | Consumer: `scene_runtime_topology/bridge_emit.py`. Idempotent. Gated on `self.state.transpilation_result is not None`. Delete `_GENERIC_RUNTIME_PROMPT` `PlayerSetSharedFlag` special case. | 3-4 |
| 4 | Detangle Pickup walk-up `GetAttribute` compensation into a separate pack. Update the 6 `after=("pickup_remote_event_server",)` consumers to depend on the new `bridge_emit` ordering anchor. Document the canonical-form acceptance criterion (replaces byte-equivalence). | 2-3 |
| 5 | Delete `_convert_pickup_to_remote_event` apply fn + helpers. Verify all 6 downstream packs work, specifically: `pickup_remote_event_client` regex detector still matches `"PickupItemEvent"`; `pickup_visual_target` template still produces a working pickup. Full e2e on SimpleFPS. | 3-4 |

**Total: 5 slices, 12-17 review rounds.**

**Per-Codex round-1:** *"transpile-time generation is easier to reason
about, easier to diff, and keeps failure visible in generated code. A
runtime host service centralizes logic, but it can become a hidden
second architecture layer."* The post-transpile rewriter preserves this
property (generated code is inspectable byte-shape) without requiring
the structurally infeasible AI-emit-time consumption.

**What this resolves:**

- Server-authoritative state for door (multiplayer correctness, per user's
  explicit requirement).
- Generic mechanism that subsumes the ad-hoc `pickup_remote_event_server`
  pack and the hardcoded `PlayerSetSharedFlag` prompt block, replacing
  both with one edge-driven path.

### Phase 3 — Contract verifier in `contract_pipeline.py`

Build-time enforcement layer. Shadow mode first; corpus-warning audit;
flip to fail-closed.

**Deliverables:**

1. **Consumer compliance check.** For every emitted artifact (animation
   script placement, transpiled Luau body, storage decision), verify it
   matches the topology artifact. Build fails on divergence.

2. **Cross-domain attribute access check.** For every `SetAttribute`
   writer + `GetAttributeChangedSignal` reader pair in the emitted Luau,
   verify both are in the same domain OR there's a matching
   `cross_domain_edges` entry with `remote_event_bridge` resolution.
   Otherwise fail with the offending file:line + a pointer to the missing
   edge.

3. **Component availability check.** For every `GetComponent("X"):Method()`
   call in the emitted Luau, verify `X` is in `_UNITY_TO_ROBLOX_CLASS` AND
   `Method` is a valid method of the mapped Roblox class. (Catches Bug 1
   recurrence — the `CharacterController = "BasePart"` mapping bug.)

4. **Shadow-mode rollout (Codex round-1 amendment).** Ships first as
   warnings + a structured metric (`contract_check_violations`) counted
   across a broader corpus (every bundled test project). Audit + fix
   real offenders. Flip to fail-closed only when the metric is zero
   across a representative cold conversion of every bundled test project,
   plus a one-release escape hatch.

5. **Per Codex:** *"Clean on bundled test projects is necessary, not
   sufficient. The failure mode at flip is newly converted external
   projects failing on latent patterns your fixtures do not cover."*

## Migration discipline

Per Codex round-2: **"Remove, not deprecate" is right, but per slice.**

- Temporary shims allowed ONLY as read-only lookup adapters over the new
  artifact (e.g. `script_storage.py` calling into topology lookup
  helpers).
- Duplicate decision logic must NOT survive. Deprecating old deciders
  invites drift; deleting each one as soon as its topology field is
  authoritative is the safer path.
- Each phase deletes the displaced logic in the same PR that wires the
  new consumer.

## Testing

### Phase 1
- **Unit (topology emission):** artifact carries correct `domain`,
  `script_class`, `lifecycle_role` for every module in SimpleFPS test
  project; every `Anim_*` has matching `animation_drivers` entry with
  correct `stable_id`; `cross_domain_edges` enumerate the canonical
  Door.cs→Anim_Door_door_open case.
- **Unit (invariants):** synthesize an artifact with each of the 5
  invariant violations; assert `build_topology` aborts with the right
  diagnostic.
- **Integration:** full SimpleFPS cold conversion. Assert:
  - No duplicate `Anim_Door_door_open` (1 instance, not 2).
  - `Anim_Door_door_open` lands in the correct container for its
    inherited domain (LocalScript in StarterPlayerScripts if driver is
    client; otherwise ServerScriptService).
  - All 6 known broken animation scripts have correct placement.
- **E2E:** `door_opens_with_key` fixture passes — door visual moves up
  14.28 studs after touch.

### Phase 2a
- **Schema-compat test (new cut):** frozen fixture artifact at
  `tests/fixtures/topology/simplefps_minimal.json` consumed by
  `animation_converter`, `script_storage`, `contract_pipeline` — all
  parse + emit without re-running planner. Guards against
  planner/consumer drift.
- **Unit (script_storage decision tree):** synthesize topology facts +
  structural inputs for every branch; assert deterministic container
  output. Includes the multi-input cases (ModuleScript caller-graph
  routing, ReplicatedFirst-only-via-loader-intent, character-attached
  forcing).
- **Regression:** every existing `test_storage_classifier.py` test
  passes after the rewrite (behavioral equivalence under the migrated
  logic; deltas are intentional and documented).

### Phase 2b
- **Slice 1 (schema + producer):** synthesize candidate edges from a
  fixture scene/prefab pair (component-ref) AND from the
  `PlayerSetSharedFlag` allowlist (shared_attribute); assert both
  emit `kind: "attribute_write"` with stable `id`s; assert the
  artifact persists with the new shape.
- **Slice 2 (enrichment + invariant):** synthesize two candidates that
  would resolve to the same `event_name`; assert the duplicate-name
  invariant aborts the build. Synthesize a same-domain candidate;
  assert it gets `resolution.strategy: "same_domain_no_bridge"`.
- **Slice 3 (emitter, golden-file regression):** synthesize a 2-module
  plan with a client writer + server reader sharing an attribute;
  assert post-emit Luau contains `<event>:FireServer(target, v)` (not
  raw `SetAttribute`). Adversarial fixtures for AI non-determinism: at
  least 3 receiver shapes (qualified, method-call chain, multi-line).
  Idempotency: run the emitter twice; assert byte-identical output.
- **Slice 4 (detangle + ordering):** the extracted
  `pickup_attribute_walkup` pack still applies the walk-up
  `GetAttribute` repair under the new ordering; each of the 6
  downstream `after=(...)` packs fires under its new ordering anchor
  with synthetic input. Canonical-form acceptance criterion (the
  documented diff allowlist) is committed alongside slice 4.
- **Slice 5 (retirement):** **canonical-form equivalence regression**
  (replaces the abandoned literal byte-equivalence promise): the
  emitted pickup-bridge bytes under the new path semantically match
  the pre-Phase-2b pack output, allowing a documented diff allowlist
  (whitespace, comment text, the walk-up `GetAttribute` block which is
  now in `pickup_attribute_walkup`). Specifically: post-emit place has
  `ReplicatedStorage.PickupItemEvent`; client listener at
  `Player.luau` still calls `getItem(itemName)`;
  `pickup_visual_target`-replaced Pickup still fires the bridge event;
  `door_player_flag_location` + `door_module_player_to_attribute` +
  `door_direct_character_attribute` still read the attribute the
  bridge writes.
- **Fast suite + e2e:** full converter test suite green; e2e on
  SimpleFPS — `door_opens_with_key` fixture passes. Existing
  `test_script_coherence_packs.py::TestPickupRemoteEventServer` is
  retired alongside the pack and replaced by the slice 3 golden-file
  regression + slice 5 canonical-form regression.

### Phase 3
- **Build-fail tests:** synthesize plans with each contract violation
  (undeclared cross-domain attribute access; unmapped GetComponent;
  consumer-compliance divergence) and assert the build aborts with a
  clear error pointing at the offending file:line.
- **Shadow-mode corpus audit:** run all bundled test projects
  (`SimpleFPS`, `Gamekit3D`, `RedRunner`, `3D-Platformer`, etc.) under
  Phase 3 in warnings mode; record + categorize all violations; fix
  real ones; only flip to fail-closed when the metric is clean.

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Topology artifact schema proves insufficient mid-migration | medium | high | Lightweight invariant checks (Codex round-1) catch contradictions early; Phase 1 ships with a SINGLE concrete consumer (animation_converter) before promising the schema to other phases |
| **Planner/consumer skew during phased rollout** (Codex round-3) | medium | high | Schema-compat test cut + frozen-fixture round-trip test prevents drift; topology version field in artifact so consumers can detect mismatches |
| Phase 2b's `pickup_remote_event_server` pack migration regresses Pickup | medium | medium | Canonical-form (not literal-byte) equivalence regression with documented diff allowlist; walk-up `GetAttribute` compensation extracted to `pickup_attribute_walkup` BEFORE pack deletion; PickupItemEvent name locked for the existing edge so the 3 downstream hardcoded sites continue to match unmodified |
| Phase 2b's edge derivation reordering (Path B) is structurally blocked | n/a | n/a | (resolved 2026-05-30) Path C decision in deliverable 1 splits derivation into structural-candidate (pre-transpile) + enrichment (post-transpile); rewriter is post-transpile, pre-pack. Cited evidence: `pipeline.py:2566-2618`, `scene-runtime-pr4-followups.md:600-626` |
| Phase 3 fail-closed mode breaks newly-converted external projects | medium | high | Shadow-mode metrics first; corpus audit across bundled projects; one-release escape hatch (env var to revert to warnings) |
| `lifecycle_role` enum proves insufficient for future cases | low | low | Closed enum + optional metadata bag (Codex round-4) — non-placement-affecting hints go in the bag, structural roles go in the enum; future enum extensions are backward-compatible |
| Cross-domain bridge RemoteEvent naming collisions across prefabs | low | medium | Deterministic `event_name = <prefab>_Set<Attribute>` scheme; topology invariant rejects collisions at emit time |
| `bridge_group_id` not unique under prefab nesting | low | low | Use deterministic edge id including provenance; topology emit-time check catches duplicates |
| Decision-tree branches in `script_storage` accidentally re-introduce regex-based domain re-derivation | low | medium | Phase 3 verifier checks all storage decisions are reproducible from topology + structural inputs (no source-scan inputs) |

## Open questions

1. **Storage location of the artifact:** new top-level key in
   `conversion_plan.json`, or a sibling `topology_plan.json`? Defer to
   Phase 1 implementation; either is fine if the consumers read through
   a single accessor.
2. **`bridge_member_scripts` schema for non-4-script bridges** (e.g.
   read-only RemoteFunction patterns or 2-script bridges): not in scope
   for Phase 1-2; add when a concrete case demands it.
3. **`entrypoint_kind` enum values:** Phase 1 starts with the minimum
   set needed for SimpleFPS; expand as test projects demand.
4. **Where does today's `_attach_monobehaviour_scripts_to_templates`
   logic go?** Probably becomes part of script_storage's bound consumer
   logic. Defer to Phase 2a.

## Sequencing + dependencies

```
PR B (HudControl injector ordering, TODO.md) ──┐
                                                │  not blocked by this refactor
PR A (CharacterController shim, TODO.md) ──────┤  ship via regular TODO queue
                                                │
Phase 1 PR (topology package + animation_converter ─┐
            migration + Anim_* dedupe + invariant   │  ~3 week scope; structural
            checks at emit time)                    │
        │                                           │
        ▼                                           │
Phase 2a PR (script_storage as bound consumer ──────┤  depends on Phase 1's
             + delete duplicated logic +            │  topology artifact shape
             move storage decision earlier +        │
             schema-compat test cut)                │
        │                                           │
        ▼                                           │
Phase 2b PR (code_transpiler bridge emission + ─────┤  depends on Phase 2a's
             retire pickup_remote_event_server)     │  artifact + multiplayer
        │                                           │  fix shape
        ▼                                           │
Phase 3 PR (contract verifier shadow → fail-closed) ┘  depends on PR A (correct
                                                       _UNITY_TO_ROBLOX_CLASS)
                                                       + Phase 2a/2b producing
                                                       reproducible decisions
```

Each phase delivers user-visible value standalone — Phase 1 fixes 6
broken animation scripts; Phase 2a + 2b together unblock multiplayer-
correct door state; Phase 3 prevents recurrence.

PR B and PR A ship through the regular TODO queue and are NOT blocked
by this refactor — they're surgical fixes for the live-playtest bugs
while the topology work is multi-PR.

## Revision history

- **2026-05-30** — Slice 2 design lock (post-slice-1, pre-slice-2-impl).
  Parallel arch review (Claude design subagent + `codex exec`) on the
  three slice 2 architectural questions returned: Q1 = B (unanimous,
  keep buckets separate); Q2 = divergent (Codex A inside the existing
  prepass, Claude C new helper) — user broke the tie on Codex A; Q3 = A
  (reuse `shared_state_linter` post-transpile Luau-scan pattern,
  unanimous). Doc updates: deliverable 1 expanded to spell out producer
  RELOCATION from `build_topology` into `_maybe_run_topology_prepass`,
  the `TopologyInputs` field growth, the BOTH-buckets duplicate-`event_name`
  invariant, the candidate-specific invariant, the
  `bridge_emit.synthesize_listener_id` shared helper. Slice 2 table row
  updated to match. Reviews on disk at
  `/tmp/topology/phase2b-slice2-{claude-arch,codex-raw.log,design-brief}.md`.
- **2026-05-30** — Phase 2b section rewritten BEFORE slice 1 implementer
  runs (per `update-design-doc-before-implementation` memory rule). Two
  parallel architectural reviews (Claude design subagent + `codex exec`
  on the consensus 5-slice plan) followed by an adversarial pass on the
  same brief converged unanimously on **Path C** (structural edge
  candidates pre-transpile, enrichment + emission post-transpile).
  Reviews on disk at `/tmp/topology/phase2b-{arch,adversarial}-*` and
  `/tmp/topology/phase2b-adversarial-synthesis.md`. Key corrections to
  the prior Phase 2b text:
  - "AT EMIT TIME" framing was structurally infeasible: `_classify_storage`
    depends on `convert_scene` + emit + cohere (`pipeline.py:2566-2618`,
    `scene-runtime-pr4-followups.md:600-626`), so the topology prepass
    cannot run before transpile. Replaced with the post-transpile
    rewriter triangle.
  - The edge model required widening: today's
    `cross_domain_edges` only enumerates `target_kind=="component"`
    peer-MonoBehaviour refs (`cross_domain_edges.py:84-88,111-143`), so
    Pickup's attribute-name shared-state writes are NOT in the set. Slice 1
    adds a second derivation source (`PlayerSetSharedFlag` allowlist
    lifted to topology data) so the artifact covers the pickup edge.
  - Byte-equivalence regression replaced by canonical-form / semantic
    equivalence with documented diff allowlist (the prior promise was
    unrealistic — the pack hardcodes `'\t\t\t'` indent, a 7-line comment
    block, AND walk-up `GetAttribute` compensation that isn't bridge
    work).
  - Walk-up `GetAttribute` compensation (`packs.py:849-908`) is now
    explicitly detangled into `pickup_attribute_walkup` in slice 4
    BEFORE slice 5 deletes the bridge logic. The current pack mixes
    bridge rewriting with AI-bug repair.
  - `PickupItemEvent` event name locked at slice 1 for the SimpleFPS
    pickup edge so the 3 downstream hardcoded sites (regex detector,
    visual_target template, listener pack) continue matching without
    migration. Future cleanup can migrate them to consume the edge
    artifact directly.
  - autogen.py scope narrowed: `PlayerShoot` + `PlayerGetItem` STAY
    (player-input events, don't fit the cross-domain edge model);
    only `PlayerSetSharedFlag` retires alongside the
    `_GENERIC_RUNTIME_PROMPT` special case at
    `code_transpiler.py:1267-1288`.
- **2026-05-30** — slice 11 (Phase 2a close-out) shipped: cleanup-only
  scope picked by parallel Claude + Codex arch review. Three commits:
  (1) test-only -- fixed two P3 pin-test gaps from the slice 10 handoff
  (rewrote `test_storage_classifier_routing_unaffected_by_resume_empty_required_container`
  to drive `classify_storage` instead of the inner-helper probe;
  deleted tautological
  `test_transpile_ran_flag_is_plumbed_but_not_consulted_at_artifact_read_site`
  and replaced with a producer/consumer pin in `test_storage_classifier.py`);
  retired stale `test_reachability_pair_populated_when_rule_fired` and
  the audit-signal tail of `test_planner_rule_invisible_to_empty_name_scripts`.
  (2) Deleted dead `_apply_reachability_rule` (~130 LOC) plus the shim
  import at `scene_runtime_domain.py:47` -- promised in the deprecated
  docstring since slice 6, deferred for hypothetical flag-day safety
  on a function with zero in-repo callers. (3) Patched this design doc
  to match shipped reality: Deliverable 1 (regex helpers RESTORED as
  fallback-only in slice 7 R3, not deleted); Deliverable 4 (slice 10
  retired audit-signal WRITES, NOT `_stamp_container_and_path` itself
  -- the function is retained as the canonical container + module_path
  writer at `module_domain.py:880/886/891`).
- **2026-05-30** — slice 10 scope expanded after slice 9b R1 review:
  both reviewers (Claude + Codex) and the implementer independently
  identified that `_build_modules_block:629` still reads the
  planner-row audit signal (`domain_signals["reachability_forced_container"]`)
  as a source for the surviving topology-entry field
  (`reachability_required_container`). Slice 10 is now **two-ended**:
  switch the read site first (to `module_row["container"]` or a
  recompute from `reachability_requirements[sid]`), then retire the
  writes at `module_domain.py:955` and `:1743`. Original framing
  ("slice 10 can delete the writes") was too optimistic.
- **2026-05-30** — slice 9b path chosen: Option C (drop
  `reachability_forced_container`) after parallel Claude + Codex
  audits independently verified no production code uses the field for
  runtime decisions. The semantic-shift concern that motivated the
  9a/9b split is moot once the field is removed.
- **2026-05-30** — slice 9 split into 9a (plumbing + #10 fold-in,
  unambiguous) and 9b (recompute, deferred until 9a ships + user picks
  A vs B). Codex caught the semantic-shift issue: today's
  `reachability_forced_container` value depends on whether the late
  hoist branch FIRED (gated by `current_container not in
  _SERVER_CONTAINERS_FOR_REACHABILITY` at `module_domain.py:935-948`),
  not just whether reachability was REQUIRED — these diverge under
  naive recompute (e.g., a helper already in `ReplicatedStorage`). 9a
  carries the unambiguous work (plumb `topology_inputs` into
  `_build_and_apply_topology`, persist for `--phase=write_output`
  resumes, fold in followup #10's symmetric `class_name`-collision fix
  via inverting `build_script_id_by_name` into a `script_by_sid` map);
  9b carries the recompute under an A (accept semantic shift) vs B
  (add `hoist_fired: bool` raw fact for byte-equivalence) decision
  deferred for code-level evidence.
- **2026-05-30** — Phase 2a slices 8-11 revised after the slice 8 arch
  review (parallel Claude + Codex) found the original "move classify
  earlier" infeasible. New plan: lift emit + cohere + classify together
  as a new `materialize_and_classify` phase (slice 8); `build_topology`
  recomputes via canonical helpers (slice 9); delete the three
  `_stamp_container_and_path` placement mutations at
  `module_domain.py:880, 886, 891` (slice 10); test sweep (slice 11).
  (Subsequent revision: slice 10 narrowed to retiring the audit-signal
  WRITES only; `_stamp_container_and_path` itself was kept as the
  canonical container+path writer. See the slice 10 + slice 11
  revision entries above for the shipped scope.)
  Original slice 10 "wire `contract_pipeline` through artifact" dropped —
  `contract_pipeline` runs pre-artifact (during `transpile_scripts`); its
  rework moves to Phase 3 where it's already scheduled. Added followup
  task #10 (symmetric `class_name`-collision fix at
  `build_topology._build_modules_block:529`) with a note to consider
  folding it into slice 9. Marked slice 7 as shipped.
- **2026-05-29** — reflected Phase 2a slice 5-7 decisions:
  - Renumbered slice plan from 10 slices to 11 slices (original slice 5
    re-scoped; original slice 6 split into 6 + 7).
  - Recorded the actual slice 5 deliverable (immutable
    `RbxScript.intrinsic_script_type`) and that slice 5's originally-planned
    pipeline reorder was dropped because the premise was wrong.
  - Recorded slice 6's actual deliverable (extracted
    `infer_module_domains` + `derive_reachability_requirements` +
    `finalize_topology_containers`; threaded `topology_inputs` kwarg
    through `classify_storage`; legacy still wins when `None`).
  - Added "Persistence rule: save raw facts; always recompute conclusions"
    with `caller_graph` as the named explicit exception. This was approved
    by parallel Claude+Codex arch review after 4 review rounds of
    staleness whack-a-mole in slice 6.
  - Added `TopologyInputs.transpile_ran: bool` as a raw fact and the
    unconstrained-helper per-script fallback contract for the no-transpile
    resume case.
  - Removed the phantom `requires_server_storage_isolation` field from the
    `script_storage.py` decision tree. The field never existed on
    `TopologyModuleEntry`; under the literal-as-drafted tree no server-only
    ModuleScript would have ever landed in `ServerStorage`. Replaced with
    the faithful-to-analysis rule: server-only ModuleScripts →
    `ServerStorage`.
  - Reconciled the decision-tree precedence ordering across Claude + Codex
    review (first-match-wins): `character_attached` →
    `loader` → `reachability_required_container` (incl. `__excluded__`
    sentinel) → ModuleScript-by-caller-domain → LocalScript /
    Script.
  - Promoted `character_attached` from a `structural_inputs` flag to a
    `lifecycle_role` enum value (consistent with slice 5's lifecycle_role
    surface).
  - Added "High-priority followup TODOs" section flagging the
    `infer_module_domains` Unity-render-helper misclassification and the
    transpiler dependency-analysis false positives (e.g., `Plane.cs`
    getting an injected `require(GameManager)` with no C# reference).
    Out of scope for slice 7 but tracked.
