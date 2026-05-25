# Scene-runtime topology authority

**Status:** plan (revised 2026-05-26 after 4 rounds of Codex review). Awaits
sign-off before implementation.
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
  `pipeline._classify_storage`)
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
- storage_classifier._scripts_with_client_apis  (duplicates module_domain)
- storage_classifier._scripts_with_server_apis  (duplicates module_domain)
- pipeline._classify_storage's classifier block (mis-layered re-derivation)
- scene_runtime_domain._stamp_container_and_path mutations  (placement
  decisions move into script_storage; topology emits facts, not mutations
  of live RbxScript)
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

**Decision tree (deterministic, no re-classification):**

```python
def assign_container(script, topology_facts, structural_inputs):
    # Hard-priority pinpoints first.
    if structural_inputs.character_attached:
        return STARTER_CHARACTER_SCRIPTS

    if topology_facts.lifecycle_role == "loader":
        # ReplicatedFirst wins ONLY for explicit loader intent.
        # Never inferred from client-domain alone.
        return REPLICATED_FIRST

    # Class-driven placement: ModuleScript routing depends on callers;
    # auto-run scripts use domain directly.
    if script.script_class == "ModuleScript":
        caller_domains = {
            module.domain
            for module in callers_of(script, structural_inputs.caller_graph)
        }
        if "client" in caller_domains:
            return REPLICATED_STORAGE
        if caller_domains == {"server"}:
            # ServerStorage only when not needed by replication/tooling.
            # Conservative default: ReplicatedStorage.
            if topology_facts.requires_server_storage_isolation:
                return SERVER_STORAGE
            return REPLICATED_STORAGE
        # No callers / mixed → conservative default
        return REPLICATED_STORAGE

    if script.script_class == "LocalScript":
        return STARTER_PLAYER_SCRIPTS

    # Auto-run Script.
    assert topology_facts.domain == "server", (
        "auto-run Script with non-server domain — topology should have caught"
    )
    return SERVER_SCRIPT_SERVICE

def enforce_constraints(script, container):
    # Hard constraints AFTER decision (not mixed into the tree).
    if script.script_class == "LocalScript" and container == SERVER_SCRIPT_SERVICE:
        raise ConstraintViolation(...)  # would never run
    if container == REPLICATED_FIRST and script.script_class == "ModuleScript":
        raise ConstraintViolation(...)  # ReplicatedFirst is for executable scripts
    # ... and other invariants ...
```

What MOVES OUT to topology:
- `_scripts_with_client_apis` regex (duplicates `module_domain` signal
  detection) — deleted
- `_scripts_with_server_apis` regex — deleted
- Implicit re-derivation of domain via API analysis — deleted

What STAYS in `script_storage.py`:
- The decision tree above
- Caller-graph traversal (using the curated dependency graph topology exposes)
- Constraint enforcement (LocalScript-in-SSS validation, ReplicatedFirst
  class check, etc.)
- Name-hint loader detection (combined with topology's lifecycle_role hint)

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
round-1 review.

**Deliverables:**

1. **Rewrite `_decide_script_container` to read topology facts.** The
   regex-based API detection (`_scripts_with_client_apis`,
   `_scripts_with_server_apis`) is DELETED — topology already provides
   domain. The decision tree above replaces the current
   six-rule sequence.

2. **Move `_classify_storage` out of `write_output`** into the transpile
   phase (run right after `scene_runtime_topology` produces the
   artifact). `write_output` consumes the persisted decisions, doesn't
   recompute. This addresses Codex's round-4 critique: *"deciding storage
   during write_output is late and likely wrong."*

3. **Delete the placement mutations in `scene_runtime_domain`** (the
   `_apply_reachability_rule` `parent_path` mutation, etc.). Storage
   decisions live in `script_storage.py` exclusively after Phase 2a;
   topology emits facts only.

4. **Schema-compat test** (Codex round-1, formalized round-3): a frozen
   fixture artifact in `tests/fixtures/topology/` consumed by
   `animation_converter`, `script_storage`, and (later) the contract
   verifier — all parsed and acted upon WITHOUT rerunning planning.
   Guards against planner/consumer drift.

**What this resolves:**

- Single decision site for storage (eliminates Codex's "split-brain"
  critique structurally, not just by convention).
- The implicit Bug 2 mitigation: once topology owns `lifecycle_role` and
  the artifact carries it, the cross-script require-injector ordering
  becomes a single emit step driven by topology, not an independent
  pipeline pass.

### Phase 2b — `code_transpiler.py` becomes bound consumer (bridge emission)

Higher-risk codegen behavior change; gated behind Phase 2a's checkpoint.

**Deliverables:**

1. **Transpile-emit bridge code generation.** For every
   `cross_domain_edges[*]` with `resolution.strategy ==
   "remote_event_bridge"`, `code_transpiler` rewrites the producer's
   `SetAttribute(name, v)` call to `<event_name>:FireServer(target, v)`
   AT EMIT TIME. Server-side bridge listener script is auto-generated
   (lives in `ServerScriptService` per the bridge member descriptors).
   RemoteEvent is auto-added to `ReplicatedStorage`. No coherence pack
   rewrites.

2. **Per-Codex round-1:** *"transpile-time generation is easier to reason
   about, easier to diff, and keeps failure visible in generated code. A
   runtime host service centralizes logic, but it can become a hidden
   second architecture layer."*

3. **Existing `pickup_remote_event_server` pack retires.** Pickup's
   `hasKey`/`hasRifle` writes become just another cross-domain edge
   handled at transpile-emit. Byte-equivalence regression test guards the
   migration.

**What this resolves:**

- Server-authoritative state for door (multiplayer correctness, per user's
  explicit requirement).
- Generic mechanism that subsumes the ad-hoc `pickup_remote_event_server`
  pack and removes a class of one-off coherence packs.

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
- **Golden-file regression:** synthesize a 2-module plan with a client
  writer + server reader sharing an attribute; assert transpiled output
  contains `<event>:FireServer(target, v)` (not raw `SetAttribute`).
- **Byte-equivalence regression:** existing `pickup_remote_event_server`
  pack output matches the new transpile-emit output. Proves migration
  didn't semantically drift before retiring the pack.
- **Fast suite:** full converter test suite green. The existing
  `test_script_coherence_packs.py::TestPickupRemoteEventServer` must
  still pass after the pack becomes a no-op.

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
| Phase 2b's `pickup_remote_event_server` pack migration regresses Pickup | medium | medium | Byte-equivalence regression test before retiring the pack; keep both paths for one release behind a feature flag |
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
