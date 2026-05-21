# PR3b follow-ups (deferred)

PR3b landed the core contract-pipeline surface area: domain
classifier, reachability rule, intra-class instance-domain conflict
handling, `.scene-runtime-mode` stamp + mismatch guard, `--clean`,
and the PR3a P2 #1 carry-over (`stub_strategy` fail-closed). The
two items below were in the PR3b doc row but are deferred to a
PR3c-style cleanup PR (or absorbed into PR4) because they are
generic-only carve-outs that only matter when PR4's host runtime
actually consumes the artifact.

Ordered by load-bearing-ness for downstream consumers.

## ~~1. `scene_converter` inactive retention (generic-only)~~ — ABSORBED in PR3c

**Status:** Shipped in PR3c (see the PR opened off
`feat/scene-runtime-pr3c`). `_convert_node` gates the
inactive-retention branch on
`ctx.scene_runtime_mode == "generic"` AND membership in the
planner-derived runtime-referenced id set; legacy emit is
byte-unchanged (pinned by a snapshot test).

**Where:** `converter/converter/scene_converter.py` — wherever
`m_IsActive == 0` GameObjects are pruned today.

**Why:** Per the design doc Piece 4 + the PR3b row test matrix
("**Inactive objects:** inactive but runtime-referenced object
emitted dormant with `_SceneRuntimeId`; unreferenced inactive
still pruned; legacy pruning byte-unchanged"). Under generic mode
the host runtime needs inactive-but-referenced objects to land in
the converted place with their `_SceneRuntimeId` attribute so it
can `setActive(true)` them later. Today's converter prunes them
unconditionally.

**Scope:** Generic-mode-only carve-out keyed on the `scene_runtime`
artifact (it lists every runtime-referenced instance). Legacy mode
keeps the current prune-on-inactive behavior byte-identical. Test
fixture: a scene with an inactive GameObject referenced by a
runtime-bearing MonoBehaviour — under `--scene-runtime=generic`
the dormant object appears in the converted place; under legacy
the place is unchanged.

**Why deferred:** The carve-out is generic-only and `generic` is
still CLI-rejected until PR4. Without a runtime consumer it
produces dead branches that can drift before they're exercised.
PR4's compliance work will exercise it for the first time, so the
implementation is best landed adjacent to PR4 (or as a tightly-
scoped PR3c).

## ~~2. `ui_translator` asset/prefab serialized-field child suppression~~ — ABSORBED in PR3c

**Status:** Shipped in PR3c (same PR as item #1). `convert_canvas`
gains `scene_runtime_mode` + `suppress_static_children_ids` kwargs
(both legacy defaults short-circuit the new branch). The static
child tree under a UI controller is dropped iff the planner marked
the controller `runtime_bearing` AND it owns an asset/prefab
serialized-field ref. Legacy emit byte-unchanged (snapshot test).

**Where:** `converter/converter/ui_translator.py` — UI conversion
emit path; specifically where serialized-field references to
assets/prefabs propagate child elements into the converted UI tree.

**Why:** Per Piece 4 + the PR3b row ("asset/prefab serialized-field
child suppression"). Under generic the host runtime is responsible
for instantiating prefab content and wiring asset references at
runtime; double-emitting them through the converter's static
serialization produces a duplicated tree (the runtime instantiates
its own copy and the static copy survives). Under legacy the static
emit is the only source of truth and must stay.

**Scope:** Same shape as item 1: generic-mode-only carve-out, keyed
on `scene_runtime.modules[*].domain == "client"` for UI controllers
and on `scene_runtime.prefabs` for prefab references. Test fixture:
a Canvas with a runtime-bearing controller whose serialized-field
points at a prefab — under generic the converted Canvas has no
static prefab descendants; under legacy it does.

**Why deferred:** Same reason as item 1 — generic-only carve-out
without a consumer. Best landed adjacent to PR4.

## How this followups doc relates to the design

These two items are explicitly listed in the PR3b row of the design
doc's PR table. Deferring them does NOT redesign PR3b's contract —
it just sequences the implementation behind PR4's first real
consumer. The PR3c (or PR4-adjacent) implementation must still
honor the original design wording without re-relitigating Piece 4.

## Carry-over markers

- [x] PR3c: implement `scene_converter` inactive retention; add the
  test from PR3b's test matrix. — Shipped via the PR3c branch.
- [x] PR3c: implement `ui_translator` serialized-field child
  suppression; add the matching test fixture. — Shipped via the
  PR3c branch.
- [ ] Confirm PR4's host runtime exercises these carve-outs (i.e.,
  the runtime DOES instantiate prefabs + wire UI refs) before
  declaring the carve-outs "live."
- [ ] **PR4 architecture gap** (codex P1 on PR3c review): the UI
  child-suppression carve-out gates on
  `scene_runtime.modules[script_id].runtime_bearing == True` AND
  `domain != "legacy"`. Today the `domain` field is populated by
  `_classify_storage` (a `write_output` subphase) which runs AFTER
  `convert_scene`, so at suppression-decision time the field is
  unset and the guard is dead code. Net effect: a runtime-bearing
  module that later fails closed to `domain="legacy"` (both-side API,
  intra-class conflict, reachability conflict) WILL have its UI
  children dropped despite the host runtime never wiring it. PR4 has
  two viable fixes:
  (a) move domain classification into a new phase between
      `transpile_scripts` and `convert_scene` so `convert_scene` sees
      the final domain, OR
  (b) defer UI child-suppression to a post-`_classify_storage` step
      that walks `state.rbx_place.screen_guis` and prunes children
      using the final `domain` field.
  PR3c ships the runtime-bearing gate per the brief's literal
  specification + a `domain="legacy"` exclusion that's structurally
  correct for when the architecture is fixed; the suppression set
  computation already returns an empty set when no runtime-bearing
  modules exist, so the impact is bounded to fail-closed
  controllers specifically.

## Additional codex P3 (from PR3b review)

- `SceneRuntimeDisplacedInstance.scene` overloads two identities:
  scene path or prefab id. Consumable as-is for PR4's cross-domain
  report; splitting into `owner_kind` + `owner_ref` (with kind
  ∈ `"scene"`/`"prefab"`) would make the PR4 report-generation
  code simpler.

## Codex P2 (deferred, NOT merge-blocking with generic CLI-rejected)

- `--clean` on `u2r publish` and `convert_interactive upload` wipes
  the whole output dir, which destroys `conversion_context.json` /
  `converted_place.rbxlx` and leaves the rebuild path with no
  state. Today these commands won't see a real mismatch until PR4
  lifts the generic CLI rejection; once they can, either:
    a. Make `--clean` scoped (only wipe the stamp + non-rebuild
       artifacts), OR
    b. Make `publish`/`upload` refuse `--clean` and tell the
       operator to run `convert --clean` instead.
