# PR1 follow-ups (deferred from Codex review)

Codex review on commit `e3f8b88` (the first PR1 commit) flagged eight
findings; PR1 absorbed the two P1s in the amended commit. These six P2s
are queued for follow-up PRs so PR1 ships clean.

Ordered roughly by load-bearing-ness for PR3a / PR3b consumers.

## 1. Lift `total=True` TypedDicts to `total=False`

**Where:** `converter/converter/scene_runtime_planner.py` —
`SceneRuntimeInstance`, `SceneRuntimeReference`, `SceneRuntimeScene`,
`SceneRuntimePrefab`, `SceneRuntimeArtifact`.

**Why:** the design's deferred `target_domain`-per-reference extension
(contract §6, "Cross-domain reference policy") is described as a
"non-breaking later schema extension". With every non-Module TypedDict
locked at `total=True`, adding `target_domain` is a breaking change at
the declared-type level. The runtime persistence is loose
(`ConversionContext.scene_runtime: dict[str, object]`), so this is a
declared-type concern only — still worth fixing before PR3b consumers
start narrowing types.

**Scope:** mechanical flip of `total=True` to `total=False` on five
TypedDicts. No behavior change. Update no callers (existing total=True
shape is the strictest superset of total=False).

## 2. `_merge_scene_runtime` malformed-JSON warning

**Where:** `converter/converter/pipeline.py:3535-3546` —
`_merge_scene_runtime` catches `json.loads` errors and logs at debug.

**Why:** comment claims it appends to `ctx.warnings`; the code only
logs. If an operator-edited `conversion_plan.json` is malformed, their
`domain_overrides` are silently dropped on the next `_classify_storage`
run with no surface signal. Fix: append a string to `self.ctx.warnings`
inside the except block, matching the comment.

**Scope:** one line of code, one assert added to the persistence test.

## 3. Soft parse failures in multi-scene driver

**Where:** `converter/converter/pipeline.py:499-510` —
`run_all_scenes()` pre-parse loop only catches hard exceptions.
`parse_scene()` returns an empty `ParsedScene` for binary /
unparseable scenes (`converter/unity/scene_parser.py:50-59`) rather
than raising, so those scenes enter `all_parsed_scenes` and the
per-scene loop runs `convert_scene` on an empty graph.

**Why:** wastes per-scene work and pollutes the scene_runtime artifact
with empty scene blocks. Same surface symptom as pre-PR1.

**Scope:** add an `if not parsed.all_nodes: log.warning(...); continue`
check in the pre-parse loop. Coverage: a unit test with a
binary-scene fixture (already in `tests/fixtures/`?).

## 4. `all_parsed_scenes` resume hazard

**Where:** `converter/converter/pipeline.py:100-107` — `PipelineState`
holds `all_parsed_scenes`, not `ConversionContext`. Currently moot:
`run_all_scenes()` is always invoked fresh; single-scene `resume()`
goes through `run_through` which re-runs essential phases that rebuild
`parsed_scene` but not `all_parsed_scenes`.

**Why:** any future single-scene resume that lands at
`plan_scene_runtime` would see `all_parsed_scenes=[]`. The fallback to
`[self.state.parsed_scene]` saves the single-scene case today, but a
multi-scene resume from this phase would silently emit only one
scene's artifact.

**Scope:** add `plan_scene_runtime` to `ESSENTIAL_PHASES` so it always
re-runs on resume — the parsed scenes are reachable through state by
that point. Add a resume integration test.

## 5. End-to-end "rewrite + resume" test for `_classify_storage`

**Where:** the PR1 test matrix item reads "Persistence: `scene_runtime`
+ nested `domain_overrides` survive a `_classify_storage` rewrite +
resume." PR1 covers rewrite and ctx round-trip separately
(`test_classify_storage_scene_runtime.py:63-156`), but no test does
rewrite → reload → re-run end-to-end.

**Scope:** new test that does `_classify_storage` → mutate
`domain_overrides` on disk → reload via `ConversionContext.load` →
`_classify_storage` again → assert override survives. Captures the
interaction Codex called out as untested.

## 6. `_prefab_stable_id` GUID-less fallback

**Where:** `converter/converter/scene_runtime_planner.py:186-187`.
If neither `guid_index` nor `prefab_library.by_guid` yields a GUID
(missing `.meta`, mid-import corruption), the function falls back to
plain project-relative path.

**Why:** still unique within one project, so collision risk is low,
but no longer satisfies the contract's "GUID + path" identity. PR3a's
require resolver could be confused by a `prefab_id` that has no GUID
prefix.

**Scope:** decide whether GUID-less prefabs should fail closed
(refuse to register the subplan), warn loudly, or accept the path-only
fallback under a different sentinel format. Most likely outcome: log
a warning and exclude the prefab from the artifact — same
fail-closed posture as stem collisions.

## 7. Share `_is_object_ref` / `_MONO_INTERNAL_PROPS` with `serialized_field_extractor`

**Where:** `converter/converter/scene_runtime_planner.py:112-129,272-279`
duplicates `converter/converter/serialized_field_extractor.py:24-46`.

**Why:** the design doc says PR1 "reuses serialized_field_extractor's
reference-walking primitives". The duplication is partly intentional
(per-instance + arrays + null slots vs. cross-asset first-seen
aggregate), but the helpers already diverge on the shared subset:
extractor treats any nonzero-GUID dict as a ref even without
`fileID`; planner requires `fileID`. They could disagree about
whether a ref exists.

**Scope:** extract a tiny shared module (`unity/serialized_ref.py`?)
holding `_MONO_INTERNAL_PROPS` and a single `is_object_ref` predicate
that both callers use. Verify both call sites still pass their tests.

## 7. Library/PackageCache prefab discovery (PR3a / PR4 scope)

**Where:** `converter/unity/prefab_parser.py:parse_prefabs` (and any
upstream caller that limits prefab discovery to `Assets/` + `Packages/`).

**Why:** `parse_prefabs()` only scans `Assets/` and `Packages/`, but
the GUID index resolves `.prefab` GUIDs from `Library/PackageCache`
too (registry packages). In projects that depend on registry packages
shipping prefabs, scene/prefab references into cached package prefabs
emit `target_kind: "prefab"` rows whose `prefab_id` never appears
under `scene_runtime.prefabs`. Prefab-attached MonoBehaviours inside
those templates never enter `runtime_bearing`. PR3a's resolver fails
closed; PR4's host has missing prefab subplans on a Unity layout that
is legitimately common. Codex round-2 P2.

**Scope:** include `Library/PackageCache` as a third prefab root (or
just walk every directory whose suffix is `.prefab` under the project
root) — registry packages live there with predictable paths. Add a
fixture-driven test that places a `.prefab` at
`Library/PackageCache/<pkg>/Prefabs/X.prefab` and asserts the planner
emits a `scene_runtime.prefabs[<X's prefab_id>]` row. Alternative: fail
closed whenever a prefab reference resolves to a `.prefab` whose
`prefab_id` is not present under `scene_runtime.prefabs`, so missing
subplans are at least visible at planning time.

## 8. scene_runtime schema version + resume invalidation (PR3a scope)

**Where:** `converter/converter/pipeline.py:_merge_scene_runtime` plus
the persistence path in `conversion_context.json`.

**Why:** `scene_runtime` is persisted verbatim in
`conversion_context.json`, but `plan_scene_runtime` is not an
essential prereq, so `resume("write_output")` and similar rebuild
paths will happily reuse whatever planner blob an earlier run saved.
`_merge_scene_runtime()` then treats that old blob as authoritative
structural state and only does a shallow top-level merge. A partial-
PR1 or mid-branch artifact missing keys like `prefabs` can therefore
survive indefinitely with no schema/version check, which is exactly
the stale-planner-state failure PR3a/PR3b will later consume. Codex
round-2 P2.

**Scope:** add a `_scene_runtime_schema_version: int` field (or
similar — `_schema_version` is shorter) at the top level of the
artifact. Bump it whenever the schema changes (the PR1 P1 absorb that
added `target_component_type` is the first such bump → version 2).
On resume, if the persisted artifact's version is missing or lower
than the current `_PLANNER_SCHEMA_VERSION` constant, invalidate the
blob and re-run `plan_scene_runtime` from parsed state. As a stopgap
until the version field lands: make `plan_scene_runtime` an essential
resume phase so the artifact is always fresh.

## 9. Project-wide vs. generic-reachable stem-collision scoping (PR3a scope)

**Where:** `converter/converter/scene_runtime_planner.py:_build_modules_table`
+ `build_require_graph`.

**Why:** The modules table is built from every `.cs` file in the GUID
index, so `build_require_graph()` reports stem collisions across the
whole repo. The spec narrows collision handling to scripts in the
**generic require graph**; dead editor/test/unreachable helpers should
not fail a generic conversion. Keeping the PR1 surface project-wide
forces PR3a either to fail closed on unrelated code or to ignore /
rebuild the PR1 graph. `tests/test_scene_runtime_planner.py:563`
currently locks in the broader behavior. Codex round-2 P2.

**Scope:** separate "all scripts inventory" from "generic-reachable
module graph." Two surfaces:
  - `scene_runtime.modules[script_id]` continues to hold every `.cs`
    in the project (legacy compat — PR2/PR3a both read this).
  - A new derived view (e.g. `runtime_modules_by_stem`) computed AT
    require-resolution time over only the runtime-bearing transitive
    closure; that's what fails closed on collision under generic.
Alternatively, defer collision reporting from PR1 to PR3a's resolver
entirely — the resolver already has the by-stem view; the planner
just hands it the raw modules dict.

## Findings codex absorbed in PR1's amended commit

These are absorbed in the new HEAD; not part of this follow-up list:

- **P1** — component-ref encoding. Local component refs to BUILT-IN
  components (Rigidbody / Button / RectTransform / Collider /
  AudioSource / ...) previously emitted `target_kind="component"` with
  `target_ref=<namespace>:<component_fileID>`, an id PR2/PR4 had no
  stable lookup surface for. Fix: distinguish peer MonoBehaviour
  refs from built-in component refs. Peer MB refs keep
  `target_kind="component"` (instance_id resolves via PR2 stamp);
  built-in component refs now resolve to `target_kind="gameobject"`
  with the OWNING GO's id and a new `target_component_type` field
  (Unity component type name). Schema extension: optional
  `target_component_type: str` field added via
  `SceneRuntimeReferenceExtra` TypedDict. Tests:
  `test_scene_runtime_planner.py::TestReferenceResolution::test_target_is_ui_for_component_ref_under_canvas`
  + `test_peer_monobehaviour_ref_keeps_component_kind`.
