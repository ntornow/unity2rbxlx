# Refactor Plan — Resolve AI-Hostile Concentrations

Status: **eng-reviewed, held** until scene-runtime-contract 9-PR effort lands upstream.
Companion: `docs/architecture_critique.md`.

## In one paragraph

Nine PRs (PR-A through PR-H, plus PR-E0 ordering audit) reshape three mega-files (`pipeline.py` 3897 LOC, `script_coherence_packs.py` 4667 LOC, `scene_converter.py` 4856 LOC) into focused modules without behavior change. **All nine PRs are held** until the scene-runtime-contract 9-PR effort merges into ntornow upstream — Phase 1 PRs touch only non-`scene_converter.py` files but are still held to avoid two concurrent multi-PR efforts diluting review attention (user decision, 2026-05-21). Phase 2 is additionally blocked by the `scene_converter.py` lock. Total ~3.5 engineer-weeks of AI-driven work once execution begins.

## Constraints

- **`scene_converter.py` is locked** until scene-runtime-contract PRs #122/#123/#124 + stacked PR3b→PR8 merge upstream. Three worktrees touch it today.
- **No-Any CI gate** per `[[no_any_ci_gate]]`: every PR runs `bash converter/tools/check_no_any.sh`.
- **Branch off `origin/main`**, target ntornow upstream (`[[fork_pr_base_repo]]`).
- **Reviewable size:** ≤1500 lines of real diff per PR; pure renames don't count.

## Eng-review decisions (locked 2026-05-21)

1. **Dispatch:** PR-D replaces 49 `Pipeline` methods with `PHASE_FUNCS: dict[str, Callable]`. Per-phase methods deleted. Tests use new public `pipeline.run_phase(name)`.
2. **PR-E shim:** explicit submodule imports — `from .packs import fps, doors, pickups, proximity, misc`. No `import *`.
3. **Script-assembly split:** PR-D extracts the 15-helper grab bag into 5 themed modules now, not deferred.
4. **Golden snapshot:** canonicalize-then-hash, scheme fully specified:
   - JSON document with **sorted keys**, but **list / sibling order PRESERVED** — parent/child hierarchy is load-bearing in the rbxlx writer (cf. `roblox/rbxlx_writer.py:810`, `core/roblox_types.py:118`); flat-sorting siblings erases real regressions.
   - For each script: `(name, sha256(source))` — NOT line count (misses constant-length content edits).
   - Sets are sorted before hashing: `unhandled_components`, asset GUID sets.
   - UUID referents normalized via existing `_REFERENT_RE` in `tests/test_byte_equivalence.py`.
   - Excluded fields: `generated_at` timestamps, `mtime`, absolute temp paths.
5. **Golden test home:** extend existing `tests/test_byte_equivalence.py` with `TestFrozenBaseline`. No new file.
6. **Baselines:** SimpleFPS + Gamekit3D + 3D-Platformer (text YAML / scale stress / BINARY YAML).
7. **Phase signature:** `(state: PipelineState, ctx: ConversionContext, services: PipelineServices)`. New `PipelineServices` dataclass has two halves:
   - Config fields: `output_dir`, `skip_binary_rbxl`, `context_path`, `is_resume`, `fps_artifacts_at_init`.
   - Bound helper callables (the 8 cross-cutting helpers extracted from `class Pipeline`): `classify_storage`, `bind_scripts_to_parts`, `rehydrate_scripts_from_disk`, `inject_runtime_modules`, `generate_prefab_packages`, `collect_all_scripts`, `collect_method_warnings`, `apply_scaffolding`.
8. **Test rewrites in PR-D:** 16+ sites calling `pipeline.<phase>()` rewrite to `pipeline.run_phase('<phase>')`.
9. **PR-E0 prelude:** audit pack execution order on `origin/main`; add explicit `@patch_pack(after=...)` edges so the split can't reorder behavior.

## Per-PR done criteria (template)

Every PR satisfies:
- `pytest -m "not slow"` passes
- `bash converter/tools/check_no_any.sh` passes
- Frozen baselines unchanged (after PR-B lands)
- One commit per logical move; `git rebase --exec` verifies every intermediate commit is green
- Codex review on full diff for PRs C/D/E/H before requesting human review

Per-PR sections below list only additions to this template.

## PR sequence

| # | PR | Phase | Days | Depends on | Touches |
|---|----|------:|----:|---|---|
| 1 | PR-A — Trim `converter/CLAUDE.md` 322→150 | 1 | 0.5 | — | docs |
| 2 | PR-B — Frozen baselines in `test_byte_equivalence.py` | 1 | 1.5 | — | tests |
| 3 | PR-C — `write_output` → `phases/output/*` + `PipelineServices` | 1 | 3 | PR-B | pipeline |
| 4 | PR-D — Pipeline dispatch table + 14 phase modules + test rewrites | 1 | 4 | PR-C | pipeline, tests |
| 5 | PR-E0 — Pack ordering audit + `after=` edges | 1 | 1 | PR-B | coherence |
| 6 | PR-E — Split `script_coherence_packs.py` | 1 | 2 | PR-E0 | coherence |
| 7 | PR-F — Mirror split of `test_script_coherence_packs.py` | 1 | 1 | PR-E | tests |
| 8 | PR-G — Eliminate `_ctx()` (50 sites) | 2 | 1.5 | scene-runtime landed | scene_converter |
| 9 | PR-H — Split `scene_converter.py` → 11 modules | 2 | 4 | PR-G | scene_converter |

After execution unblocks (scene-runtime-contract lands upstream) and PR-B has merged, lane C (PR-C → PR-D) and lane D (PR-E0 → PR-E → PR-F) can run in parallel worktrees. Phase 2 is strictly sequential.

## PR detail

### PR-A — `CLAUDE.md` trim
Cut: Autonomous Work Plan, Recent Session blocks, Development History (2026-03-24 → -28), Full upload test (2026-03-25). Keep: bug fix protocol, upload semantics, coordinate system, test projects, CLI commands, mesh sizing, asset resolution, inline-over-runtime, Roblox safety rules. ~1.5K tokens saved per session.

### PR-B — Frozen baselines
Extend `tests/test_byte_equivalence.py` with `TestFrozenBaseline`. New files: `tests/golden/{simplefps,gamekit3d,platformer}.rbxlx.sha256`; `tests/golden/canonicalize.py` implementing the scheme from decision #4. Determinism guard runs each conversion twice on the test host and asserts canonical hash matches before comparing to baseline.

**+ done criteria** (PR-B specific — the template's "frozen baselines unchanged" criterion doesn't apply yet because PR-B is what creates them):
- `pytest -m slow tests/test_byte_equivalence.py::TestFrozenBaseline` passes on `origin/main` HEAD with all three baselines.
- Determinism guard exercised: each baseline runs twice on the test host with matching canonical hashes BEFORE comparing to the checked-in `.sha256`.
- The three `.sha256` files are committed and reproducible from `origin/main` HEAD by re-running the test.

### PR-C — `write_output` subphases + `PipelineServices`
New `phases/services.py` with `PipelineServices` dataclass (decision #7). New `phases/output/` package, one module per `_subphase_*` method: `emit_scripts.py`, `cohere_scripts.py`, `inject_autogen.py` (264 LOC, includes pre-scaffolding migration; locate via `grep -n 'Migrating pre-scaffolding' converter/converter/pipeline.py`), `encode_terrain.py`, `inject_mesh_loader.py`, `patch_setup_sounds.py`, `finalize_scripts.py`. Each: `def <name>(state, ctx, services) -> None`. `Pipeline.write_output` becomes a ~30-line orchestrator.

**+ done criteria:**
- `pipeline.py` line count drops from 3897 to ~3400 LOC.
- New `tests/test_pre_scaffolding_resume.py` regression test passes (covers the previously-uncovered pre-scaffolding migration branch).
- `python -c "from converter.phases.services import PipelineServices"` succeeds.

### PR-D — Pipeline dispatch + 14 phase modules

**Nine phase modules** (one per current `Pipeline.<phase>` method):

| Module | Contents |
|---|---|
| `phases/parse.py` | `parse` + private helpers |
| `phases/extract_assets.py` | `extract_assets`, `_extract_serialized_field_refs`, `_compute_fbx_bounding_boxes` |
| `phases/moderate_assets.py` | `moderate_assets` |
| `phases/upload_assets.py` | `upload_assets`, `_audit_new_uploads` |
| `phases/convert_materials.py` | `convert_materials`, `_bake_vertex_colors` |
| `phases/transpile.py` | `transpile_scripts` |
| `phases/convert_animations.py` | `convert_animations` |
| `phases/resolve_assets.py` | `resolve_assets` (272 LOC) |
| `phases/convert_scene.py` | `convert_scene`, `_delete_pruned_script_from_disk` |

**Five script-assembly themed modules** (decision #3):

| Module | Contents |
|---|---|
| `phases/script_binding.py` | `bind_scripts_to_parts`, `attach_prefab_scoped_animation_scripts_to_templates`, `attach_monobehaviour_scripts_to_templates` |
| `phases/storage_classification.py` | `classify_storage`, `load_storage_plan_for_rehydration` |
| `phases/rehydration.py` | `rehydrate_scripts_from_disk`, `remove_rehydrated_fps_autogen` |
| `phases/runtime_injection.py` | `inject_runtime_modules` |
| `phases/reporting.py` | `build_conversion_report`, `build_script_summary`, `collect_method_warnings`, `write_unconverted_md` |

**Dispatch:** `phases/__init__.py` defines `PHASE_FUNCS: dict[str, Callable]`. `PHASES = list(PHASE_FUNCS.keys())` derives from it (CQ-1 fold). `Pipeline._run_phase` and new public `Pipeline.run_phase(name)` both look up `PHASE_FUNCS[name]`.

**Frozen `Pipeline` public API after PR-D:** `__init__`, `apply_scaffolding`, `scaffolding`, `_find_unity_root`, `context`, `run_all`, `run_all_scenes`, `run_through`, `resume`, `run_phase`, `_run_phase`. All per-phase methods DELETED.

**Test call-site rewrites** (decision #8):
- `tests/test_resolve_assets_id_contract.py` — 6 sites
- `tests/test_sprite_extractor_wiring.py` — 4 sites
- `tests/test_scriptable_object_wiring.py` — 3 sites
- `tests/test_pipeline_write_output_subphases.py` — deeper rewrite (asserts on `services` shape, not `self` access)

**New test** `tests/test_pipeline_dispatch.py`: (a) `set(PHASE_FUNCS.keys()) == set(PHASES)`, (b) `run_phase('typo')` raises `KeyError`, (c) every dispatched callable signs `(state, ctx, services) -> None`.

**+ done criteria:** `pipeline.py` ≤ 800 LOC; `class Pipeline` ≤ 15 methods; `python -c "from converter.pipeline import Pipeline; from converter.phases import PHASE_FUNCS"` succeeds; from the `converter/` directory, `grep -rn 'pipeline\.\(parse\|extract_assets\|moderate_assets\|upload_assets\|convert_materials\|transpile_scripts\|convert_animations\|resolve_assets\|convert_scene\|write_output\)\s*(' tests/` returns zero matches.

### PR-E0 — Pack ordering audit

Dump current execution order on `origin/main`: `_topological_order(PatchPack._registry)` → checked-in `tests/fixtures/pack_execution_order.txt`. For every pack that detects against an earlier pack's post-rewrite shape (cf. `test_script_coherence_packs.py:894`, `TestProducerConsumerBindableEventGuard`), add `@patch_pack(after=('producer_name',))` to its decorator. New `TestPackOrderFrozenOnMain` asserts post-topo order matches the fixture. After PR-E0, registration order becomes irrelevant.

### PR-E — Split `script_coherence_packs.py`

New `converter/converter/coherence/`:

| Module | Contents | ~LOC |
|---|---|---:|
| `__init__.py` | Explicit submodule imports trigger registration; re-exports `run_packs`, `PatchPack`, `patch_pack` | 15 |
| `registry.py` | `PatchPack`, `patch_pack`, `_topological_order`, `run_packs` | 200 |
| `helpers.py` | Cross-pack helpers + shared regexes (`_LUA_BLOCK_OPEN_RE`, `_TOUCH_CALLBACK_RE`, etc.) | 250 |
| `packs/fps.py` | Weapon mount + `WEAPON_MOUNTS`, default controls, camera pitch, bullet physics + `_PICKUP_REPLACEMENT`, `_PICKUP_TOUCHED_*` | 1000 |
| `packs/doors.py` | Global player lookup, AI rotation strip, tween open, module player attr + `_DOOR_GLOBAL_PLAYER_*_RE` | 600 |
| `packs/pickups.py` | Remote event conversion, visual target, listener fanout + `_PICKUP_SETATTRIBUTE_RE`, `_PICKUP_HAS_ATTR_INJECTED_RE`, `_PICKUP_REMOTE_ALIAS_RE`, `_GETITEM_SYMBOL_RE` | 900 |
| `packs/proximity.py` | Trigger stay polling, proximity fanout | 400 |
| `packs/misc.py` | Template clone visibility (`_inject_template_clone_visibility`), LocalScript API shim (`_build_shim_source`, `_classify_api`), BindableEvent guard, self-destroying template guard + `_SELF_DESTROY_RE`, `_TEMPLATE_GUARD_*` | 700 |

Existing `script_coherence_packs.py` → ~15-line back-compat shim per decision #2.

**+ done criteria:** No file in `coherence/` exceeds 1100 LOC; names assertion on 24 specific pack names (not just count); `TestPackOrderFrozenOnMain` still passes; `TODO.md` P1.a/P1.b/P1.c entries rewritten to point at new `coherence/packs/misc.py` locations.

### PR-F — Mirror test split

`tests/coherence/test_registry.py`, `test_packs_fps.py`, `test_packs_doors.py`, `test_packs_pickups.py`, `test_packs_proximity.py`, `test_packs_misc.py`. Delete `tests/test_script_coherence_packs.py`. Pure relocation. **+ done criteria:** `pytest tests/coherence/ -v` collects the same test count as before.

### PR-G — Eliminate `_ctx()` in `scene_converter.py`

Lands the deferred refactor that the file's own comment block defers to "when individual helper signatures are refactored to accept ctx explicitly" (located via `grep -n "deferred" converter/converter/scene_converter.py`). Removes the module-global `_current_ctx` attribute and the `_ctx()` accessor function. Every call site (located via `grep -n '_ctx()' converter/converter/scene_converter.py` — was 50 sites at 2026-05-21, will drift with the scene-runtime-contract effort) gets `ctx: SceneConversionContext` as an explicit parameter. `convert_scene()` instantiates ctx and threads it through `_convert_node`, `_process_components`, etc.

**Drift note:** Because PR-G is gated on scene-runtime-contract landing first (which touches `scene_converter.py` heavily), the exact line numbers and call-site count WILL drift. Use grep, not hardcoded refs, during execution.

**+ done criteria:** `grep -cE '_ctx\(\)|_current_ctx' converter/converter/scene_converter.py` returns 0; new `test_no_module_global_ctx` deletes the attribute (via `delattr(scene_converter, '_current_ctx')` if it still exists) and runs `convert_scene()` — must not raise.

### PR-H — Split `scene_converter.py`

**Import-graph constraint:** edges go prefab → components, never the reverse. `scene/components.py` MUST NOT import `scene/prefab.py`. Today `_process_components` does not call back into prefab — preserve this.

New `converter/converter/scene/`:

| Module | Contents | ~LOC |
|---|---|---:|
| `__init__.py` | Re-exports `convert_scene` | 5 |
| `_context.py` | `SceneConversionContext` dataclass | 50 |
| `convert_scene.py` | `convert_scene` + `_convert_node` | 600 |
| `components.py` | `_process_components` | 600 |
| `prefab.py` | `_convert_prefab_instance`, `_convert_prefab_node`, `_convert_fbx_prefab_instance`, `_wrap_geometry_with_children_into_model` | 1100 |
| `mesh_sizing.py` | `_compute_mesh_*`, `_get_fbx_*`, `_read_*` | 700 |
| `mesh_resolution.py` | `_resolve_sub_mesh`, `_resolve_mesh_id`, `_resolve_mesh_texture_id`, `_get_multi_sub_meshes`, `_extract_prefab_material_map` | 350 |
| `materials.py` | `_apply_materials`, `_blend_extra_material_colors`, `_apply_prefab_materials` | 250 |
| `lighting.py` | `_extract_lighting`, `_apply_directional_light`, `_extract_skybox` | 200 |
| `water.py` | `_is_water_node`, `_extract_water_region`, `_extract_water_region_from_prefab` | 150 |
| `monobehaviour.py` | `_extract_monobehaviour_attributes` | 250 |
| `transforms.py` | `_compose_parts_with_parent_cframe` | 100 |

Old `scene_converter.py` → back-compat shim.

**+ done criteria:** No file in `scene/` exceeds 1500 LOC; all 3 frozen baselines unchanged; new `test_scene_split_imports.py::test_no_circular_imports` runs `python -c "from converter.converter.scene_converter import convert_scene"` in a fresh interpreter and asserts no `ImportError`; full SimpleFPS + Gamekit3D + 3D-Platformer e2e conversions match canonical hashes.

## What this plan does NOT cover

- `animation_converter.py` (2082 LOC) and `component_converter.py` (1986 LOC) splits — same shape, lower urgency, revisit after Phase 2.
- `test_animation_converter.py` split (3517 LOC) — couples to the above.
- Skills hygiene (global `~/.claude/skills/gstack` 2500-line `SKILL.md` files) — separate repo.
- Performance, features, or public API renames.

## Re-use, don't rebuild

- `tests/test_byte_equivalence.py` — UUID-referent normalization + flow-equivalence. PR-B extends.
- `tests/test_pipeline_e2e.py` — 7-project end-to-end harness. Baselines plug in.
- `tests/_project_paths.py` — `_has_project(...)` skipif pattern.
- `converter/converter/scaffolding/` — precedent for the `phases/` directory layout.
- `PHASES` list at `pipeline.py:40` — single source of truth, PR-D derives from `PHASE_FUNCS.keys()`.
- `@patch_pack` decorator + `_topological_order` machinery — unchanged, only file boundaries move.
- `bash converter/tools/check_no_any.sh` — existing CI gate.

## Failure modes after the new tests land

| PR | Realistic regression | Caught by |
|---|---|---|
| PR-C | Subphase order mutation | Frozen baseline |
| PR-D | Phase-module import error / missing `services` field | Test collection + e2e |
| PR-D | Test still calls deleted `pipeline.parse()` | Collection failure |
| PR-E0 | Missing `after=` edge | `TestPackOrderFrozenOnMain` |
| PR-E | Pack module not imported in `coherence/__init__.py` | Names assertion (24 specific names) |
| PR-G | Helper still references `_ctx()` after grep | No-global-state test |
| PR-H | Circular import between `scene/` modules | Smoke import test |

No critical silent gaps.

## Next step

When scene-runtime-contract PRs #122/#123/#124 + stacked PR3b→PR8 merge into ntornow upstream: re-baseline file line numbers (may have drifted), then execute PR-A → PR-B → PR-C → PR-D → PR-E0 → PR-E → PR-F. Phase 2 (PR-G → PR-H) follows.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Runs | Status |
|---|---|---|---|
| Eng Review | `/plan-eng-review` | 2 | CLEAR (PLAN) — round 2: 9 issues / 9 decisions locked. Round 4: 4 issues / all folded (PR-C done criteria, gating regression, PR-D path notation, PR-G drift wording). |
| Codex Review | outside voice | 4 | issues_found — round 1 on critique, round 2 on plan v1, round 3 on compression diff, round 4 on compressed plan |
| CEO / Design / DX | — | 0 | not applicable to refactor scope |

**Cross-model:** Round 2 — Claude eng-review + codex converged on dispatch-table + `_ctx()` priority; codex caught the `(state, ctx)` signature being too narrow. Round 3 — codex caught canonicalization erosion, PR-B gate, services enumeration. Round 4 — codex caught gating contradiction (compressed plan silently reverted user-locked "all held" decision), PR-D path-notation ambiguity, PR-G line-number drift fragility.

**Verdict:** ENG CLEARED — ready to execute once scene-runtime-contract lands.
