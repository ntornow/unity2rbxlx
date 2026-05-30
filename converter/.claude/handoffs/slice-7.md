# Slice 7 handoff — for slice 8+

Slice 7 (`_decide_script_container` rewrite) is THE behavior change of Phase 2a:
`_decide_script_container` becomes a bound consumer of `topology_inputs`. Took 4
review rounds at the budget cap; final reviewers (Claude + Codex tight re-run)
both APPROVE.

HEAD at ship: `b100fb5` (R4 unify lifecycle prepass join).

## Architecture decisions made (with provenance)

- **Decision tree precedence (locked across 4 review rounds)**:
  1. `lifecycle_role==character_attached` → StarterCharacterScripts
  2. `lifecycle_role==loader` → ReplicatedFirst
  3. `reachability_required_container[sid]` → that container
     (`__excluded__` → ReplicatedStorage)
  4. ModuleScript caller-domain: any-client → ReplicatedStorage;
     server-only → ServerStorage; orphan/unknown → ReplicatedStorage
  5. Script + domain=client → StarterPlayerScripts (R2 addition)
  6. Class-driven default: LocalScript → SPS; Script → SSS

  Source: design doc + slice 7 R2/R3.

- **Server-only ModuleScript routes to ServerStorage** (faithful to upstream
  domain analysis; design-doc default was a degenerate ReplicatedStorage that
  collapsed because `requires_server_storage_isolation` was a phantom field on
  `TopologyModuleEntry`). User-approved override of doc default; design doc
  updated in commit `d3ce034`. Source: slice 7 arch synthesis + user decision
  2026-05-29.

- **Regex helpers (`_scripts_with_client_apis` + `_scripts_with_server_apis`)
  restored as FALLBACK-PATH ONLY** in slice 7 R3. The topology path uses
  upstream `infer_module_domains`; the fallback uses the regex helpers when
  `transpile_ran=False` AND `reachability_requirements[sid]` is absent.
  Source: slice 7 R2 Codex P1 #4+#5 + user-approved Option C.

- **`transpile_ran: bool` on `TopologyInputs`** is a raw fact populated from
  `state.transpilation_result is not None`. Slice 8 must NOT bypass this when
  deciding fallback gating. Source: slice 7 arch reviews.

- **Source-scan augmentation in `_build_call_graph`**: legacy fallback path
  consults `re.finditer(r'require\(')` to catch injected-require dependencies
  the topology `caller_graph` doesn't see. Topology path uses pure `dep_map`
  projection. Source: slice 7 R2 Codex P1 #5.

- **Dual-surface guard at `storage_classifier.py:783-788`**: a script touching
  both client AND server APIs fails CLOSED to server (routes to SSS, not SPS).
  Mirror of legacy fail-CLOSED semantics. Source: slice 7 R3 Codex P2-NEW-A
  reclassified to P1.

- **`lifecycle_role` computed in prepass** (not in
  `build_topology._build_modules_block`) via `derive_module_lifecycle_role` —
  placed in `topology_inputs.lifecycle_roles[sid]`. Source: slice 7 R1
  Codex P1 #2.

- **`character_attached` lifecycle_role gated on
  `script_class in ("Script", "LocalScript")`** (symmetric with `is_loader`).
  ModuleScript with `character_attached=True` → `requireable`, not
  `character_attached`. Source: slice 7 R2 Claude P2.

## What slice 8 inherits + what it should NOT undo

1. **`TopologyInputs.transpile_ran`** — raw fact, not a heuristic. Don't
   recompute or override; consume as-is.
2. **`TopologyInputs.lifecycle_roles[sid]`** — populated in the prepass via
   `derive_module_lifecycle_role`. Don't move computation back into
   `_build_modules_block`.
3. **Dual-surface fail-CLOSED-to-server** at `storage_classifier.py:783-788`.
   Don't soften to fail-open; this mirrors legacy semantics.
4. **Sentinel test** asserting `_decide_script_container_from_topology` body
   contains no reference to the regex helpers. Don't suppress the sentinel
   when adding new topology branches.
5. **Fallback regex helpers (`_scripts_with_client_apis`,
   `_scripts_with_server_apis`)** are FALLBACK-PATH ONLY. Their docstrings
   carry that contract. Don't reuse them from the topology path.
6. **`_build_call_graph` source-scan augmentation** is fallback-path only.
   Don't port to topology path; topology uses `dep_map` projection.
7. **End-to-end pipeline fixture test** runs REAL prepass → classify_storage
   chain (no pre-stamped topology_inputs). Don't replace with pre-stamped
   fixtures; that's what masked the R1 producer/consumer ordering bug.
8. **Decision tree precedence ordering** is load-bearing. Don't reorder steps
   1–6 without a design-doc update.
9. **Server-only ModuleScript → ServerStorage** (not ReplicatedStorage). The
   design doc was updated; don't revert based on the old doc text.
10. **`character_attached` script_class gate** must stay symmetric with
    `is_loader`. Don't relax to allow ModuleScript character_attached.

## What slice 7 deliberately deferred to slice 8+

- **Followup #8**: Investigate upstream input classification of Roblox-dead
  modules (water rendering, dead Unity APIs).
- **Followup #9**: Investigate transpiler false-positive require injection
  (Plane → GameManager).
- **Followup #10**: Symmetric class_name-collision fix at
  `build_topology._build_modules_block:529` (same bug as R4 P2-NEW-B but in
  the topology artifact persister).
- **Eventual deletion of fallback-path regex helpers** once topology coverage
  extends to the no-transpile resume case.

## Process notes for slice 8

- Slice 7 took 4 review rounds at the budget cap; pattern was "Codex finds
  another fallback-path regression each round." Slice 8 must verify
  legacy/fallback symmetry from the start, not just topology-path correctness.
- Memory rule: tests passing with pre-stamped fixtures masked a
  producer/consumer ordering bug in slice 7 R1. Slice 8 tests must construct
  fixtures by running real producers, not pre-stamping outputs.
- Two-pass codex plan review (high-effort/broad then medium-effort/narrow)
  caught operational ordering hazards the single-pass review missed.
- Anchor reviews on design intent over current behavior; label evidence as
  OLD/NEW/upstream; separate generic-rule from instance-classification.
