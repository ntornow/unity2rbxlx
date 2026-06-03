# Scene-runtime topology authority

**Status:** in-flight (revised 2026-06-02 to reflect the Phase 3 arch-review
re-plan; the prior 2026-05-30 revision captured the Phase 2a slice 8 re-plan).
Phases 1, 2a, and 2b shipped (merged through PR #161; slice-7 followups #162/#163;
#165/#166 open). **Phase 3 (this revision): plan locked after parallel
Claude+Codex arch review — see §"Phase 3" below for the corrected scope, the
5-slice breakdown, and the decisions that differ from this doc's original
deliverable text.**
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
- **Owning every runtime cross-domain WRITE.** (Scoping clarified 2026-06-01
  after the Phase 2b empirical finding — see the Phase 2b section.) Topology
  owns the cross-domain *channel contract*: for STATIC couplings (a
  serialized component reference), the per-edge bridge; for DYNAMIC shared
  state (attribute names computed at runtime, e.g. `"has" .. itemName`,
  routed through one funnel RemoteEvent), the *channel* (its presence, the
  set of literal names READ across the boundary, and the canonical store the
  funnel writes). Topology CANNOT own each individual runtime write — those
  names don't exist until runtime. The funnel is an irreducible runtime
  mechanism topology GATES + RECORDS and Phase 3 VERIFIES; it does not
  emit it per-write.

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
├── code_transpiler.py          ← Class-1 (component-ref) only: domain +
│                                 cross_domain_edges. Class-2 (shared-flag)
│                                 is NOT a transpiler rewrite — the funnel
│                                 stays (see 2026-06-01 reframe); topology
│                                 gates+records it.
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

## Roblox-dead module handling (TODO #8)

Some Unity modules are **Roblox-dead**: rendering / shader / camera-effect
helpers (the SimpleFPS water cluster — `WaterBase`, `Displace`,
`PlanarReflection`, `WaterTile`, `SpecularLighting`, `GerstnerDisplace`, …)
whose transpiled body does nothing executable in Roblox. Left alone they were
misrouted into `ServerStorage` by the caller-domain rule (their only callers are
server-default leaf Scripts → `caller_domains == {"server"}`), shipping dead
code in the wrong container. See the grounding facts + LOCKED DECISIONS in
[`roblox-dead-module-routing-brief.md`](roblox-dead-module-routing-brief.md).

**Detector (generic, no game-specific names) — `converter/roblox_dead_modules.py`.**
Definition **D3 (both-agree) + HARD VETO**:
- *Input prior (weak):* the fraction of the module's C# API references that
  resolve to a REAL (non-stub) mapping in `API_CALL_MAP` ∪ `TYPE_MAP`
  (`-- no equivalent` comment stubs AND absent entries both count as unmapped).
  "Dominated by unmapped" (real coverage ≤ ~0.49) ⇒ dead-leaning. The surface
  excludes `using`/`namespace` directives, the module's own class name, and
  structural/lifecycle tokens, so a trivial empty MonoBehaviour abstains.
- *Output confirmation (decisive):* the **post-coherence** Luau body is inert —
  only class-table boilerplate / comments / `print` / empty lifecycle handlers /
  `require`s, with no genuine Roblox effect. Uses the converter's own
  deterministic stub markers as strong signals (structural, not regex-on-AI).
- *Hard veto:* any single genuine Roblox effect (`Instance.new`, `.Parent =`, a
  real property write, a RemoteEvent/BindableEvent fire, a DataStore/service
  mutation, a genuinely-mapped call) ⇒ NOT dead, regardless of fraction. The
  converter-injected `PrimaryPart`/`script.Parent` guard idiom is excluded.

A module is dead iff input agrees AND output inert AND no veto.

**Pass placement.** `pipeline._subphase_analyze_dead_modules` runs **between
`_subphase_cohere_scripts` and `_classify_storage`** (the post-coherence Luau —
the decisive output signal + the injected require edges — only exists there). It
is eligible only for ModuleScripts whose transpile strategy is `ai`/`stub`
(deterministic): a `rule_based`/`hybrid` fallback can emit an inert TODO-skeleton
for a REAL gameplay module, so its inertness is not trusted. The verdict is a
TRANSIENT `PipelineState.dead_modules` (never persisted; abstains entirely on a
no-transpile resume, where the storage plan was already computed by the
transpiling run — honoring the recompute-only rule below).

**Routing consumer (B).** `classify_storage(dead_modules=…)` routes a dead
ModuleScript to `ReplicatedStorage` regardless of caller-domain, in **both** the
topology path (`_decide_script_container_from_topology`) and the legacy path
(`_decide_script_container_legacy`, whose `"…server-side callers"` reason text is
the cached symptom). The dead body is already an inert stub, so SceneRuntime
applies no effect; RS keeps it reachable for any surviving requirer.

**Prune consumer (A) — closure safety.**
`pipeline._subphase_prune_dead_module_closures` DROPS a dead module from
`rbx_place.scripts` (+ `_delete_pruned_script_from_disk`) **only when its entire
require-closure is also dead** — no live (non-dead) module requires it. The
closure is computed from the **FINAL EMITTED LUAU** injected-require edges
(`RS:FindFirstChild(name) or SS:FindFirstChild(name)`), NOT `dependency_map`
(which misses post-transpile injected requires). A dead-but-live-required module
stays emitted and falls back to the B reroute (so no surviving `require()`
becomes `require(nil)`, per the brief's GF8). Runtime-bearing generic-mode
components are never pruned (would dangle a `SceneRuntimePlan` row) — they stay
inert. Geometry is untouched (built in separate scene_converter branches), so
pruning the scripts causes no visual regression.

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
   `Displace`) classified as server-only and got caller-domain-routed into
   `ServerStorage`. **RESOLVED (Task #8):** the actual route was caller-domain
   storage routing (not `infer_module_domains`); fixed with a generic
   Roblox-dead detector + dual-path RS reroute + closure-safe prune pass. See
   the "Roblox-dead module handling" section above + the brief. (Task #8.)
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

### Phase 2b — cross-domain authority (two bridge classes)

Gated behind Phase 2a's checkpoint. **Current shape: see the EMPIRICAL
REFRAME below** — Phase 2b makes topology authoritative over the cross-domain
channel contract: it OWNS+records the static component-ref bridge (Class 1)
and GATES+records the dynamic shared-flag funnel (Class 2). It does NOT build
a per-attribute emitter. The two subsections below ("Path C decision" and the
original adversarial framing) are retained as HISTORICAL context; the
2026-06-01 reframe supersedes the "rewriter/emitter" framing for the
shared-flag class.

<details><summary>Historical: 2026-05-30 adversarial-review framing
(superseded for Class 2 by the 2026-06-01 reframe)</summary>

Architecture explored 2026-05-30 after Claude+Codex parallel adversarial
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

</details>

**EMPIRICAL REFRAME (2026-06-01, Claude+Codex parallel whole-plan review,
`/tmp/topology/phase2-3-strategy-{codex-raw.log,brief}.md`).** Before slice 3
(the emitter) we ran the empirical check the plan kept assuming away: what does
a real generic-mode conversion actually emit for cross-domain shared state?
Mining the live transpile cache (`converter/.cache/llm`) refuted the
"single edge-driven emitter" framing and split the problem into TWO bridge
classes. The slices-1-2 seed (`Pickup`→`PickupItemEvent`/`producer_domain`)
mis-modeled the dynamic class as the static class — the root of slice 2's
four direction/exclusion review rounds.

**Two bridge classes (this is the load-bearing distinction):**

| | Class 1 — static component-ref | Class 2 — dynamic shared-flag |
|---|---|---|
| Example | Door → Animator (`open` field) | pickup → `has<Item>` |
| Coupling | explicit serialized reference in scene data | implicit; attribute name computed at runtime (`"has" .. itemName`) |
| Writer name | a fixed field, statically known | **runtime-computed; no literal `SetAttribute("hasKey")` is ever emitted** |
| Reader | n/a (peer ref) | LITERAL — `GetAttribute("hasKey")`, server-domain |
| Runtime bridge | per-edge, derivable | ONE funnel RemoteEvent (`PlayerSetSharedFlag`) carrying `(name, value)` at runtime |
| Topology role | own + record the per-edge bridge | **gate + record the channel** (it can't own each runtime write) |

Verified facts (cited):
- Generic mode skips `run_packs` entirely (`pipeline.py:2930`), so the
  `pickup_remote_event_server` PACK is **legacy-mode only** — NOT the
  generic bridge.
- In generic mode the funnel ALREADY exists and is generic: the prompt
  teaches a variable-name funnel (`code_transpiler.py:1267-1288`,
  `mirrorFlag("has" .. itemName, …)` → `PlayerSetSharedFlag:FireServer`),
  and autogen provisions the `OnServerEvent` listener — **unconditionally**
  (`GameServerManager` always injected, `pipeline.py:3101-3105`;
  `PlayerSetSharedFlag` always created, `autogen.py:162`).
- Cache evidence: `GetAttribute("hasKey")` ×6, `GetAttribute("hasGasCan")`
  ×5; literal `SetAttribute("hasKey"|"hasGasCan")` ×0. The write is always
  `SetAttribute(flagName, value)` (variable) through the funnel.
- The motivating door bug was a CANONICAL-STORE mismatch (flag written on
  the Player Instance but read on the character Model), already fixed
  earlier — NOT a missing generic bridge. (Pinned by the legacy pack tests,
  `test_script_coherence_packs.py:416,435`.)

**Consequence:** the generic shared-flag bridge already works. Phase 2b's
real job is to make topology *authoritative* over it (gate + record), not to
build a new emitter. A symmetric `SetAttribute("N")`↔`GetAttribute("N")`
literal scan is impossible — the writer name is runtime-computed.

**Deliverables (reframed):**

1. **Class 1 — component-ref edges: keep as-is.** `compute_cross_domain_edges`
   (slices 1-2) records these GENERICALLY (`<owner>_Set<Field>` derived name,
   direction from domains). Correct; no change. Phase 3 verifies them with the
   exact-literal check.

2. **Class 2 — shared-flag channel: gate + record (fork ii, Claude+Codex
   unanimous).** Retire the `SHARED_ATTRIBUTE_SEEDS` table and the
   shared-flag `CrossDomainEdge` rows (`cross_domain_edge_candidates`). Replace
   with a distinct **`shared_flag_channels`** topology fact block. Slice 3
   then **gates** autogen's funnel injection on this fact (emit the
   `PlayerSetSharedFlag` funnel *only when* the fact says a cross-domain
   shared-flag reader exists, instead of unconditionally) and **records**
   the channel. The runtime funnel mechanism (prompt + autogen listener) is
   KEPT — it is the irreducible runtime bridge for dynamic names. **No
   per-attribute emitter; no producer Luau rewrite; the
   `_GENERIC_RUNTIME_PROMPT` funnel guidance STAYS** (deleting it forces a
   cold re-transpile and there is nothing to replace it with — the funnel is
   the generic mechanism).

   **`shared_flag_channels` schema (concrete, per doc-review):** a SINGLE
   artifact block — there is one funnel (`PlayerSetSharedFlag`), so one
   channel record, not per-flag rows:
   ```
   "shared_flag_channels": {
     "PlayerSetSharedFlag": {
       "read_names": ["hasKey", "hasGasCan", ...],  # literal flags read cross-domain
       "reader_domains": ["server", ...],           # domains that READ a flag
       "canonical_stores": ["Player", "Character"], # CONSTANT — what the funnel writes
       "present": true                               # the gate: funnel injected iff true
     }
   }
   ```
   - `read_names` comes from the existing `shared_state_linter`-style reader
     scan (slice 2's one reusable piece): literal `GetAttribute("...")` reads
     whose reader resolves to a domain different from the writer side.
   - `canonical_stores` is a CONSTANT recorded from the funnel's fixed
     behavior (`autogen.py:174-176` writes both `Player` and `Character`),
     NOT a per-read derivation — so slice 3 needs no new signal to record it.
     (Recording the reader's OWN store, for the Phase 3 coverage check, is a
     Phase 3 detail — see Phase 3 #2 — not a slice-3 blocker.)
   - "Coverage" in Phase 3 #2 means: a literal read name ∈ `read_names`
     (exact membership; `read_names` IS the set of covered literals — the
     "name/template" wording is reconciled to exact-name membership here).

   **RESUME behavior — the gate FAILS OPEN (settled per doc-review; this is
   the one genuinely-new design call).** The reader scan source
   (`state.transpilation_result.scripts`) is ABSENT on a no-transpile
   resume (`--phase=write_output`, `transpilation_result is None`,
   `pipeline.py:4659-4667`). A recompute-only fact would then recompute to
   an EMPTY `read_names` → the gate would WRONGLY disable a needed funnel.
   So: when `transpilation_result is None`, the gate does NOT tighten — it
   falls back to today's UNCONDITIONAL injection (`present: true`,
   `read_names` omitted/unknown). The gate only narrows the funnel on a
   FRESH transpile where the scan is authoritative. This mirrors the
   existing resume-degradation contract slice 2 already documents (no
   consumer rows on resume → broadcast fallback) and the `transpile_ran`
   pattern. Slice-3 tests MUST pin both: fresh-no-reader → funnel gated OFF;
   resume-`transpilation_result is None` → funnel KEPT.

3. **Fix the slice-2 persistence-contract drift.** `TopologyInputs` documents
   these facts as recompute-only / not persisted (`module_domain.py:623`,
   the "save raw facts, recompute conclusions" rule), but slice 2 persists
   `cross_domain_edge_candidates` (`build_topology.py:563`). The
   `shared_flag_channels` rework must honor the recompute-only contract.
   **The slices-1-2 revision must also retire the now-orphaned surfaces
   that the `cross_domain_edge_candidates` bucket carries** (or CI breaks):
   the `TopologyInputs` candidate field (`module_domain.py:597-621`), the
   artifact field + assembly (`build_topology.py:353-367,563`), the
   candidate-specific invariants (`build_topology.py:1442-1482`), and the
   ~30+ seed/candidate tests (`test_scene_runtime_topology.py` slice-1/2/R3/R4
   blocks). The gate read-site (**corrected, step-2 R2**): the topology prepass
   (`_maybe_run_topology_prepass`) stashes the computed
   `shared_flag_channels` fact on the TRANSIENT, run-scoped field
   `self.state.shared_flag_channels` (a `SharedFlagChannels | None` on
   `PipelineState`); the gate `_shared_flag_funnel_present` reads THAT stash
   at the autogen-inject site (`_subphase_inject_autogen_scripts`). The prepass
   runs in `materialize_and_classify` BEFORE `write_output`, so the stash is
   set by injection time. **It does NOT read `ctx.scene_runtime["topology"]`**
   — the original framing was wrong: `ctx.scene_runtime` is the planner's
   PRE-topology dict (set once at the planner phase) and the `topology` block
   is built only on the MERGED LOCAL dict inside `_classify_storage`
   (`_merge_scene_runtime` returns a fresh copy) and is NEVER written back to
   ctx. Reading off ctx therefore always missed → fail-open → the gate was a
   production NO-OP (tests passed only by manually seeding
   `ctx.scene_runtime["topology"]`). The transient stash is fed by the prepass
   on EVERY run (incl. the resume fail-open `present: True`), is never fed into
   `_merge_scene_runtime`, and is never persisted — so it does not re-stale the
   recompute-only fact. The persisted `topology["shared_flag_channels"]` in
   `conversion_plan.json` REMAINS the authority for **Phase 3** to read from
   the plan; it is just not the in-run gate's read site.

4. **Legacy-only pack retirement (unchanged, slices 4-5).** The
   `pickup_remote_event_server` pack and its 6 `after=(…)` dependents are
   legacy-mode-only. Slice 4 detangles the walk-up `GetAttribute("itemName")`
   AI-bug compensation (`packs.py:849-908`) into a separate pack; slice 5
   re-anchors the dependents and deletes the bridge logic. Decoupled from the
   generic-mode shared-flag work above.

**Reframed slice plan:**

| Slice | Status / Scope | Rounds |
|---|---|---|
| 1 (#159) | Edge schema. **Revise:** drop `SHARED_ATTRIBUTE_SEEDS` / `producer_domain` / the locked `PickupItemEvent` (all artifacts of mis-modeling Class 2 as Class 1). KEEP the `CrossDomainEdge` schema for Class 1. | open, revise |
| 2 (#160) | Enrichment + two buckets. **KEEP** the component-ref edge path + the reader scan (Class 2's one reusable piece). **Replace** the shared-flag candidate bucket with the `shared_flag_channels` fact; fix the persistence-drift. | open, revise |
| 3 | **Shared-flag: gate + record (fork ii).** Gate autogen's funnel injection on a `shared_flag_channels` fact; record the channel (read-name set + reader domains + canonical store). Keep the prompt + funnel. No emitter. | 3-4 |
| 4 | Legacy-only: detangle Pickup walk-up `GetAttribute` compensation into its own pack. | 2-3 |
| 5 | Legacy-only: re-anchor the 6 `after=("pickup_remote_event_server",)` packs; retire the pack. Full e2e on SimpleFPS. | 3-4 |

**What this resolves:**

- Topology becomes the **authority** over the shared-flag channel (it gates
  the funnel) rather than a bystander to an unconditional autogen side
  channel — the actual stated goal, correctly scoped to the channel contract.
- The generic mechanism is honest: dynamic shared flags ride one funnel
  (already generic); topology records the *contract* (which literal names
  cross, written to which canonical store) for Phase 3 to verify; the
  legacy game-specific `PickupItemEvent` pack is retired in legacy mode.

### Phase 3 — Contract verifier (new `contract_verifier.py`)

Build-time enforcement layer that proves every emitted artifact obeys the
topology authority. Shadow mode first; corpus audit; **per-check** flip to
fail-closed. **Scope corrected 2026-06-02 after a Claude+Codex parallel arch
review against the merged code (original deliverable text preserved in git):**

- **NOT `contract_pipeline.py`.** That module is a *transpile-time* orchestrator
  (`pipeline.transpile_scripts`, gated `scene_runtime_mode == "generic"`,
  `pipeline.py:2015`) and runs before script placement exists. The verifier
  needs the FULL topology artifact + final `RbxScript.parent_path` + emitted
  Luau, which only coexist after `_build_and_apply_topology` (inside
  `materialize_and_classify`, `pipeline.py:4730`). → **new module
  `contract_verifier.py`, invoked immediately after `_build_and_apply_topology`
  using the in-scope merged `scene_runtime` dict.** Plumbing hazard:
  `scene_runtime["topology"]` is written to the merged local dict +
  `conversion_plan.json` but NOT back to `ctx.scene_runtime`, so a later
  `write_output` hook would see no topology — the call site must use the merged
  dict.
- **Generic-mode only** (the whole topology path is gated `!= "legacy"`,
  `pipeline.py:4608-4612`). Runnability spike (2026-06-02): a generic-mode cold
  conversion of SimpleFPS reaches the topology stage and emits the full artifact
  + 34 Luau scripts — corpus viable.

**Deliverables (checks):**

1. **Consumer-compliance check (check A) = domain⟂placement consistency.**
   Slice-1 arch review (2026-06-02) corrected the mechanism: do NOT "stamp the
   final container and compare" — the artifact's `container`/`module_path` is
   MIRRORED from `RbxScript.parent_path` (`module_domain.py:1666-1670`, set after
   `classify_storage`), so that comparison is tautological (green-for-the-wrong-
   reason). The only INDEPENDENT signal is `domain` (source-derived; never reads
   `parent_path`/`script_type` — CI-guarded by
   `test_infer_module_domains_does_not_read_parent_path`). So check A reconciles
   the independent `domain` against the emitted (`script_type`, container-family
   of `parent_path`) via a consistency table. Container families: SERVER_ONLY =
   {ServerScriptService, ServerStorage}; CLIENT_ONLY = {StarterPlayerScripts,
   StarterCharacterScripts, ReplicatedFirst}; NEUTRAL = {ReplicatedStorage}.
   - **server:** violation if `LocalScript` (never runs server-side — the GF11
     type-before-domain latent class, `storage_classifier.py:719-720`) OR
     `parent_path ∈ CLIENT_ONLY`.
   - **client:** violation if `script_type == "Script"` AND `parent_path ∈
     SERVER_ONLY` (client logic auto-running on the server).
   - **helper:** violation if `script_type ∈ {Script, LocalScript}` (a helper is
     require-only). Container is NOT checked for helpers (a reachability hoist can
     legitimately place a client-reachable helper in a CLIENT_ONLY container).
   - **excluded:** if it still joined to an emitted RbxScript → "excluded but
     emitted" violation; else skip.
   Scope: **modules only this slice.** `animation_drivers` are deferred — their
   `domain↔script_class` is consistent by construction (build_topology invariant
   4) and they use a different (display-name) join, so there's no contradiction
   for check A to find there yet. Unverifiable joins (stem/name collision) are
   skipped with a recorded `severity:"info"` row (no silent gap). Must NOT
   duplicate the storage classifier's hard `ConstraintViolation`s
   (LocalScript-in-SSS, ModuleScript-in-ReplicatedFirst,
   `storage_classifier.py:898-908`). **Known gap (out of scope):** a server-secret
   module wrongly placed in NEUTRAL ReplicatedStorage (a replication leak) is
   structurally undetectable here — the artifact carries no "server-private"
   signal; record for a future slice, don't imply coverage.

2. **Cross-domain attribute access check.** Re-specified 2026-06-01 for the
   two bridge classes (the original "every `SetAttribute` writer +
   `GetAttribute` reader pair → same-domain OR matching edge" assumed literal
   writer/reader pairs, which is wrong for dynamic shared flags — the writer
   name is runtime-computed, so no literal writer exists to pair).
   - **Class 1 (static component-ref):** exact-literal check unchanged —
     a `SetAttribute("X")` writer + `GetAttribute("X")`/
     `GetAttributeChangedSignal("X")` reader must be same-domain OR covered by
     a matching `cross_domain_edges` entry with `remote_event_bridge`
     resolution.
   - **Class 2 (dynamic shared flag):** for every LITERAL
     `GetAttribute("hasX")`/`GetAttributeChangedSignal("hasX")` reader in a
     server domain, require coverage by EITHER a same-domain literal writer
     OR a `shared_flag_channels` entry where (1) the literal name is a member
     of `read_names` (exact membership — `read_names` IS the covered set) AND
     (2) the reader's OWN store (the object it reads — `Player` vs `Character`
     vs some other Instance) is a member of the channel's `canonical_stores`.
     "Funnel present" alone is insufficient — coverage MUST include the
     specific read name and the reader's store, so this catches the
     canonical-store mismatch class (the original door bug: funnel wrote
     Player, reader read the character Model). **Phase-3-specific signal:**
     the reader's own store is NOT recorded by slice 3's `read_names` scan
     (which captures the flag name, not the read target); Phase 3's verifier
     extends the scan to capture the read TARGET so it can check store
     membership. This is a Phase 3 deliverable, not a slice-3 prerequisite.
   Otherwise fail with the offending file:line + a pointer to the missing
   channel/edge.

3. **Component-availability check (check B) = GetComponent reachability.**
   Slice-2 arch review (2026-06-02) scoped this to REACHABILITY. Generic mode
   emits the peer form `self:GetComponent("X")` (`code_transpiler.py:1329`),
   resolved at runtime (`scene_runtime.luau:752-780`) as: peer
   converted-MonoBehaviour → else `_UNITY_TO_ROBLOX_CLASS[X]` →
   `findFirstChildWhichIsA(mapped or X)` → **nil** if unknown (subsequent use
   errors). Check B flags `:GetComponent[InChildren|InParent]("X")` literal-arg
   sites where `X` is UNREACHABLE: not a peer (module `stem` ∪ `script_id`, matching the runtime peer lookup),
   not a `_UNITY_TO_ROBLOX_CLASS` key, not one of its values, not in an explicit
   minimal Roblox-class allowlist. The allowlist exists because the emitted arg
   is always a Unity name or peer class name (never a Roblox class), so the
   values-as-proxy is "safe by accident" — the allowlist makes legitimate
   direct-Roblox-class passes (e.g. `GetComponent("Humanoid")`) safe-by-design
   before the fail-closed flip; it is biased to ABSTAIN (over-broad only fails
   open). The runtime map is the authority (NOT Python `TYPE_MAP`; they disagree
   — runtime `CharacterController → BasePart`); the verifier PARSES it from the
   Luau file (single source of truth) guarded by an EXHAUSTIVE key/value test.
   **Method-validity DEFERRED** (the `CharacterController → BasePart` →
   `:Move()` anecdote): the repo has no Roblox class→method database, and the
   transpiler already routes `CharacterController.Move`/`.SimpleMove`/`.isGrounded`
   through a bridge (`api_mappings` `API_CALL_MAP`), so the anecdote is largely
   already handled. Documented gap, not silently dropped. **Coverage:** only
   string-literal args (a variable arg can't be resolved statically and is
   skipped) — so a fail-closed flip of check B covers literal sites only.

4. **Shadow-mode rollout + metric.** Every check ships first as warnings + a
   structured `contract_check_violations` metric on `ConversionReport`
   (`report_generator.py`) and `ctx.scene_runtime` (mirroring the existing
   `contract_fail_closed` plumbing, `pipeline.py:2041`). Resume-idempotent: the
   verifier re-runs on any `materialize_and_classify` resume, so dedupe by
   violation identity (mirror `if msg not in ctx.errors`). A one-release env-var
   escape hatch (precedent `U2R_LEGACY_PREFAB_PIVOT`) is read at the
   error-promotion gate (compute-the-metric-but-don't-abort), not at verifier
   entry, so the shadow metric stays populated even when the hatch suppresses
   the fail.

5. **Per-check independent flip.** Each check flips shadow → fail-closed on its
   own cadence once its metric is clean across the runnable corpus — NOT all
   together. Corpus = the subset of bundled projects that complete a generic-mode
   cold conversion; `log()` every excluded project + reason (no silent cap).
   Exercising checks B/C requires AI transpilation (stub Luau can't be scanned;
   check A works on stubs). *Per Codex: clean on bundled projects is necessary,
   not sufficient — external projects surface latent patterns; the env-var hatch
   covers the flip.*

#### Slice breakdown (each an independently-reviewed stacked PR)

- **Slice 0** — runnability spike (done; SimpleFPS viable) + verifier skeleton:
  `contract_verifier.py`, the post-materialize hook (with the topology-restage
  fix), the `contract_check_violations` metric + report field, the inert env-var
  escape hatch, resume-idempotent dedup. Smoke check only.
- **Slice 1** — check A = domain⟂placement consistency (modules only; NO
  container stamp — see check #1 above). Animation_drivers deferred.
- **Slice 2** — check B (GetComponent reachability), keyed off the PARSED runtime
  `_UNITY_TO_ROBLOX_CLASS` + peer set (stem ∪ script_id) + Roblox-class
  allowlist. Reachability only; method-validity deferred.
- **Slice 3** — check C = a STRUCTURAL cross-domain-edge bridging invariant
  (read directly off `cross_domain_edges`: every runtime client↔server edge
  MUST be `remote_event_bridge`). Zero-FP regression guard. The original
  literal `SetAttribute`/`GetAttribute` Luau-scan reconciliation was REVERTED
  after the codex slice-3 review found it false-positives on (P1) Class-2
  shared-flag literal mirrors (modeled in `shared_flag_channels`, not edges)
  and (P2) the writer×reader Cartesian over reused field names (the emitted
  Luau carries no instance identity to match the edge granularity). **Class-2
  store-mismatch DEFERRED** as a backstop-only check with no corpus true-positive
  — see slice 4d for the corrected rationale (the earlier "phantom /
  pre-coherence hook" framing was wrong).
- **Slice 4** — corpus shadow audit + per-check fail-closed flip behind the
  env-var hatch. Decomposed (Claude+Codex review 2026-06-03):
  - **4a — fast corpus harness.** `tools/regen_contract_corpus.py` captures
    `(topology, scripts)` from a REAL generic-mode AI conversion per runnable
    bundled project (the topology dict only exists in the in-scope merged
    `scene_runtime`, so capture is via a hook monkeypatch, not a JSON
    reconstruct). `tests/test_contract_corpus.py` replays each committed fixture
    through the LIVE `_run_contract_verifier` hook (NOT the bare `verify_contract`
    pure fn — the flip lands in the hook, so a pure-fn gate is
    green-for-the-wrong-reason on wiring drift). Anti-tautology: regen REFUSES to
    write a fixture with any real (`warning`) violation, and the test pins
    per-check counts — so a dirty baseline or a new-violation regression both
    fail loudly. This is the gate each flip must pass.
  - **4b — per-check flip gate + flip A.** `contract_verifier.FAIL_CLOSED_CHECKS`
    (a frozenset; `consumer_compliance` joins it) + `fail_closed_errors(result)`.
    The promotion gate in `_run_contract_verifier` appends a flipped check's
    `warning` rows to `ctx.errors` (→ `conversion_report.success=False`), reading
    the slice-4 hatch `U2R_CONTRACT_VERIFIER_FAIL_OPEN` AT the gate
    (compute-the-metric-but-don't-abort; distinct from the entry-level
    `U2R_CONTRACT_VERIFIER_DISABLE`). Deduped against `ctx.errors`
    (resume-idempotent). `info` rows and shadow checks never promote. Flip check A
    first (stub-validatable, producer bug fixed via the require-fallback signal fix
    + #172, lowest FP risk). NOTE: flipping A required a producer fix first — the
    `_strip_require_calls` signal fix in `module_domain.py` (a converter-emitted
    `require(...ServerStorage...)` fallback was posing as a strong server signal,
    fail-closing the HUD to a dead-emit `excluded`); the corpus gate (4a) caught it.
  - **4c — flip check B** (component_availability). Validated on real AI Luau via
    4a: the SimpleFPS fixture has 20 literal-arg `GetComponent` sites, all
    reachable (B is exercised, not vacuously clean). Added to `FAIL_CLOSED_CHECKS`.
  - **4d — check C deferred to slice 7 (now done).** SimpleFPS has **0
    cross-domain edges**, so check C's "0 violations" on it is vacuous — it could
    not flip on the single-domain corpus. C flips in slice 7 once slice 6 adds a
    project with real runtime client↔server edges (no silent cap; the gap was
    logged, then closed). Document Class-2
    store-mismatch as a DEFERRED backstop. **Corrected rationale (empirical,
    2026-06-03):** a real generic-mode SimpleFPS conversion emits the door read
    as `plr:GetAttribute("hasKey")` where `plr = host.playerFromTouch(other)` =
    the Player instance = the CORRECT store. Generic mode steers every touch
    script through the `playerFromTouch` host helper — structural store-correctness
    at transpile time. So Class-2 has no corpus true-positive. It is NOT a phantom
    (the door packs are off in generic — `pipeline.py` `_subphase_cohere_scripts`
    early-returns) and does NOT need a pre-coherence hook (verification belongs on
    the FINAL artifact; a pre-coherence gate is the destructive-gate-before-
    decisive-check anti-pattern). The future Class-2 arm, if ever needed, is a
    store-aware orphan check on the final generic Luau, gated on a real uncovered
    true-positive. (Orphan-read check on the door: traced 2026-06-03 — NOT a bug.
    `_setSharedFlag` writes `plr:SetAttribute(flag,…)` via a variable flag name, and
    Door/Player/Pickup are all `domain=client`, so the write and read connect.)

### Phase 3 continuation — emergent slices 5-7 (folded in 2026-06-03)

Slice 4 surfaced three findings the original plan did not anticipate; all three are
now in-scope Phase 3 work. (The HudControl signal fix is already shipped in #174.)

- **Slice 5 — the `excluded` contract for unclassifiable runtime-bearing modules.**
  DESIGN-DEBATABLE → resolve via Claude+Codex review BEFORE implementing.
  Problem: classifier-v2 collapsed the contract's `legacy` verdict ("runs via the
  un-split fallback") into inert `excluded`. In generic mode an `excluded`
  runtime-bearing module is emitted but never constructed (the boot loop skips it)
  → silent dead emit. With check A flipped (slice 4b) that silent drop is now a
  LOUD build failure — good — but the operator has NO recourse: Rule-1 `excluded`
  accepts only an `excluded` override (`module_domain.py` §"Operator override"), so
  the only escape is editing the Unity source. Resolve the contract:
  (1) reconcile the `legacy→excluded` collapse — define what generic mode does with
  a genuinely-unclassifiable runtime-bearing module;
  (2) give a recourse — let `domain_overrides` pin an `excluded` runtime-bearing
  module to `client`/`server`. **Codex caveat to weigh in review:** pinning a
  GENUINELY both-side module to one side ships a half-broken module (its other-side
  API calls fail), which can be worse than a clean drop — so the override must be an
  explicit operator opt-in ("I accept the half-broken risk"), not an automatic
  re-route, and the default stays loud-fail. No auto-routing; no `excluded`
  redefinition (helper already covers non-runtime-bearing).
- **Slice 6 — networked corpus project (DONE).** The bundled corpus was only
  SimpleFPS — single-player, entirely `domain=client` (0 cross-domain edges) — so
  A/B were validated on one game and C was structurally unvalidatable. Added a
  minimal networked fixture, `tests/fixtures/corpus_projects/MiniNet`: a
  client-domain `ClientCtl` (UI MonoBehaviour) holding a serialized reference to a
  server-domain `ServerCtl` (`NetworkBehaviour` + `[Command]`/`[SyncVar]`). Under
  `--networking=mirror` the converter classifies them client/server and the
  serialized component reference yields one runtime client↔server
  `cross_domain_edge` (auto-bridged → `remote_event_bridge`). `CORPUS` in
  `regen_contract_corpus.py` now carries per-project `networking` + path (in-repo
  fixture vs submodule projects-root). MiniNet's captured fixture exercises check C
  (1 edge) with 0 violations.
- **Slice 7 — flip check C (DONE).** With MiniNet exercising C cleanly,
  `cross_domain_attribute` joined `FAIL_CLOSED_CHECKS`. All three contract checks
  (A/B/C) are now fail-closed; only the `smoke` wiring check stays shadow.

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
(Testing reframed 2026-06-01 with the two-bridge-class split. The Class-1
component-ref tests stay; the shared-flag tests move from "edge emitter" to
"channel gate + record.")
- **Slice 1 (schema):** synthesize Class-1 component-ref edges from a
  fixture scene/prefab pair; assert `kind: "attribute_write"` + stable `id`s
  + the `<owner>_Set<Field>` derived name. (The `SHARED_ATTRIBUTE_SEEDS` /
  `producer_domain` / locked-`PickupItemEvent` tests are RETIRED with the
  seed.)
- **Slice 2 (component-ref edges + reader scan + `shared_flag_channels`):**
  Class-1 duplicate-`event_name` invariant still aborts on a true semantic
  collision. NEW: the reader scan discovers a literal `GetAttribute("hasX")`
  server-domain reader and records a `shared_flag_channels` entry with the
  name, reader domain, and canonical store; assert the entry is NOT persisted
  (recompute-only contract); assert a scene with no cross-domain shared-flag
  reader records an empty channel set.
- **Slice 3 (gate + record):** with a cross-domain shared-flag reader
  present (FRESH transpile), assert the `PlayerSetSharedFlag` funnel IS
  injected and the `shared_flag_channels` fact is recorded; with NO such
  reader (FRESH transpile), assert the funnel is NOT injected (the gate —
  topology is now authoritative). **Resume fail-open:** with
  `transpilation_result is None` (no-transpile resume) and NO discoverable
  reader, assert the funnel is STILL injected (the gate does not tighten when
  the scan source is absent — see deliverable 2's RESUME behavior; prevents
  disabling a needed funnel on resume). Assert the `_GENERIC_RUNTIME_PROMPT`
  funnel guidance is UNCHANGED (no cold re-transpile) and no producer Luau is
  rewritten. Idempotency: re-run; the funnel is injected exactly once
  (`if not existing_server_mgr` guard holds).
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
| Phase 2b's `pickup_remote_event_server` pack migration regresses Pickup (legacy mode only) | medium | medium | Canonical-form (not literal-byte) equivalence regression with documented diff allowlist; walk-up `GetAttribute` compensation extracted to `pickup_attribute_walkup` BEFORE pack deletion. NOTE (2026-06-01 reframe): the pack + its `PickupItemEvent` name are LEGACY-mode-only; the generic-mode shared-flag bridge is the funnel, gated+recorded by topology (no `PickupItemEvent` lock in the generic path). |
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
Phase 2b PR (Class-1 component-ref edges +         ┤  depends on Phase 2a's
             topology gates+records the Class-2     │  artifact + multiplayer
             shared-flag funnel; retire legacy      │  fix shape
             pickup_remote_event_server pack)       │
        │                                           │
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

- **2026-06-01** — Phase 2b EMPIRICAL REFRAME (Claude+Codex parallel
  whole-plan review, before slice-3 implementation). The empirical check the
  plan kept deferring — what a real generic-mode conversion actually emits for
  cross-domain shared state — was run against the live transpile cache and
  **refuted the single-emitter framing**. Findings + reframe:
  - **Two bridge classes**, conflated by the slices-1-2 seed: Class 1 (static
    component-ref, statically known both sides — keep as-is) vs Class 2
    (dynamic shared-flag, runtime-computed name through one funnel). Evidence:
    cache shows `GetAttribute("hasKey")` readers but ZERO literal
    `SetAttribute("hasKey")` writers — the write is always
    `SetAttribute(flagName, value)` (variable) via `mirrorFlag(...)` →
    `PlayerSetSharedFlag:FireServer`.
  - **The generic funnel already exists** in generic mode (prompt
    `code_transpiler.py:1267-1288` + autogen listener `autogen.py:162`,
    injected unconditionally `pipeline.py:3101-3105`). The
    `pickup_remote_event_server` pack is **legacy-mode-only** (generic skips
    `run_packs`, `pipeline.py:2930`). The door bug was a canonical-store
    mismatch, already fixed — not a missing bridge.
  - **Decision: fork (ii) — topology GATES + RECORDS the funnel** (Claude+Codex
    unanimous). Slice 3 gates autogen's funnel on a `shared_flag_channels`
    fact (so topology is the authority, not a bystander to an unconditional
    side channel) and records the channel; it does NOT build a per-attribute
    emitter or rewrite producer Luau, and KEEPS the prompt funnel guidance (no
    cold re-transpile). The earlier "AT EMIT TIME rewrite" / "Path C
    post-transpile rewriter" framing is SUPERSEDED for the shared-flag class.
  - **Slices 1-2 revised:** drop `SHARED_ATTRIBUTE_SEEDS` / `producer_domain` /
    the locked `PickupItemEvent` (Class-2-as-Class-1 mis-modeling); keep the
    component-ref edges + reader scan; replace the shared-flag
    `cross_domain_edge_candidates` rows with a `shared_flag_channels` fact
    block. Fix the persistence-drift (slice 2 persists candidates against the
    recompute-only `TopologyInputs` contract, `build_topology.py:563` vs
    `module_domain.py:623`).
  - **Phase 3 deliverable #2 re-specified** for the two classes (exact-literal
    for Class 1; name+canonical-store coverage for Class 2 — catches the
    canonical-store bug class).
  - **Goal scoped** (non-goals section): topology owns the cross-domain
    *channel contract*, not every runtime write.
  - PRs #159/#160 are open and will be revised rather than stacked-on; the
    user okayed revisiting them.
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
