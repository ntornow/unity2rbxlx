# Converter TODO

Active work items only. Completed work + PR execution logs live in `TODO_archive.md`.

Priority: **P0** = blocks gameplay, **P1** = significant quality, **P2** = nice to have.

---

## Pipeline / runtime gaps

- [ ] **P1 — Door pack widening (PR #121 `371ab76`) has 1 fixture covering 1 of 3 emit shapes.** Claude review, 2026-05-21.
  `_detect_door_module_player_lookup` + `_fix_door_module_player_lookup` were
  widened to match three AI-emitted shapes: `playerHasKey(playerInstance)`,
  `playerHasKey()`, and `getPlayerHasKey()`; PLUS three Player-resolution
  paths: `getPlayerMod`, `PlayerScripts`, and
  `script.Parent:FindFirstChild("Player")`. `TestDoorModulePlayerToAttribute`
  has exactly one fixture (`_door_zero_param`) that exercises the first
  shape with `getPlayerModule`. Neither `getPlayerHas*` nor the
  sibling-require resolution path has a regression test. The
  `coherence_packs_single_pass` memory warns this class of regression is
  latent. Fix: add 2 fixtures for the missing shapes + 1 unit test on the
  attribute-name derivation regex
  (`^(?:get)?[pP]layer([Hh]as\w+)$`) in
  `script_coherence_packs.py:1750-1762`.

- [ ] **P2 — Weapon-mount pack "no `_MainCameraRig`" fallback freezes rifle in world space.** PR #121 review (codex, 2026-05-21).
  `_apply_weapon_mount` in `script_coherence_packs.py` has three seating
  branches: `WeaponSlot` (preferred), rig (fallback), workspace (last
  resort). The last branch does ONE
  `rifle:PivotTo(workspace.CurrentCamera.CFrame * fallback_offset_expr)`
  then sets `rifle.Parent = workspace` with `Anchored = true` — so on a
  non-FPS project (no rig), or one where the rig tag failed to land, the
  rifle freezes in world coords. Fix: in the workspace branch, emit a
  per-frame `RenderStepped` follower (the pre-fdb01c1 design) so the
  weapon still tracks the camera even without rig parenting. Add a
  fixture that exercises the no-rig path and asserts the follower is
  present in that branch.

- [ ] **P2 — Door-helper regex `(?:get)?[pP]layer[Hh]as\w+` over-matches.** PR #121 review (codex + Claude, 2026-05-21).
  `script_coherence_packs.py:1670` — `[Hh]as\w+` matches `playerHash`,
  `playerHasted`, `getPlayerHasher`, etc. The `s.name != "Door"`
  early-exit narrows file scope but doesn't guarantee semantic intent.
  Fix: tighten to `[Hh]as[A-Z]\w*` so the suffix must start with an
  uppercase letter, matching the `HasKey`/`HasItems`/`HasFooBar`
  camelCase convention. Eliminates `Hash`/`Hashed`/`Hasher` cleanly.

- [ ] **P2 — Weapon-mount pack equip-function naming is undocumented and exact-spelling.** PR #121 review (codex + Claude, 2026-05-21).
  `_equip_function_variants` handles Pascal/camel of the registry value
  (`GetRifle` → `getRifle`), but a third emitted variant like `Get_Rifle`
  or `GetRifleObject` would silently miss both detection AND rewrite.
  Nothing documents what `WeaponMount.equip_function` is allowed to
  contain. Fix: add a docstring constraint
  (`equip_function must be a single PascalCase verb-noun pair, no
  separators`), and consider a startup-time validator that asserts every
  registry entry matches the expected shape.

- [ ] **P1 — Genre-genericness follow-ups for FPS-leftward-migration PR (codex 2026-05-17).**
  PR #96 shipped pipeline-level fixes for SimpleFPS gameplay bugs (mouse-look,
  walk speed, mine trigger, rifle visibility, ParticleEmitter NumberSequence,
  TextLabel TextSize float, stale .rbxl regen). Codex review on the branch
  flagged six findings the PR explicitly defers — non-FPS Unity projects may
  regress on any of these until follow-up lands.

  - **P1.a — `localscript_api_shim` type-aware accessor classification.**
    `_classify_api()` (`script_coherence_packs.py:_classify_api`) currently
    treats any bare-identifier return (`return gotKey`) as boolean
    backing-state and emits a hardcoded `c:GetAttribute(...) == true or false`
    shim. Non-boolean APIs (ammo counts, cooldowns, enum/state IDs, inventory
    quantities) silently become "always false". Fix: classify by inferring the
    backing var's declared literal type (`= 0` → number, `= ""` → string, etc.)
    and emit type-appropriate `GetAttribute` reads. Add mixed-type test
    coverage in `tests/test_script_coherence_packs.py::TestLocalScriptApiShim`.
  - **P1.b — `localscript_api_shim` server-side consumer fails.** The shim's
    `_resolveCharacter(character)` (`script_coherence_packs.py:_build_shim_source`)
    falls back to `Players.LocalPlayer.Character`, which is nil on server
    Scripts. Door's `playerHasKey()` no-arg call therefore returns false
    forever. Fix: detect call-site context (Script vs LocalScript) at the
    consumer-rewrite stage and either (a) require an explicit `character`
    argument and rewrite the call site to pass it, or (b) emit per-context
    shim shapes. Add behavioural test (not just textual-rewrite assertion).
  - **P1.c — `template_clone_visibility` over-broad detector.** The pack
    matches ANY `cloneTemplate(...)` / `Templates:FindFirstChild(...):Clone()`
    anywhere and blindly forces `Transparency=0`, `CanCollide=false`,
    `Massless=true` + welds on every BasePart of the clone
    (`script_coherence_packs.py:_inject_template_clone_visibility`). Non-FPS
    projects will see invisible triggers, VFX helpers, physical props, vehicle
    parts, and projectile clones mutated. Fix: narrow the detector to consumers
    that re-parent the clone to a weapon-slot-style Part holder, OR gate on the
    template's actual BaseParts being `Transparency=1` at clone time. The
    existing `Spawner`/`Bullet` test fixture proves the over-fire path — flip
    it to a no-op assertion.
  - **P2.a — Gate FPS-specific transpiler rules.** The new rules in
    `code_transpiler.py` mouse-look (raw `GetMouseDelta()` + radians-per-pixel
    constant), HRP+1.5-stud camera, and `jumpSpeed → Humanoid.JumpHeight` are
    first-person humanoid recipes, not generic CharacterController policy. The
    walk-speed `WalkSpeed = speed × STUDS_PER_METER` rule and the physics-radii
    Unity-m → studs rule are genuinely generic. Fix: wrap the FPS-shaped rules
    in an explicit "ONLY for first-person / locked-mouse cameras" prelude so
    the AI doesn't emit them on third-person, top-down, platformer, or vehicle
    scripts.
  - **P2.b — Genre-negative `run_packs()` regression fixtures.** All new
    coherence pack tests cover "single unrelated script stays unchanged" but
    nothing pins "BoatAttack-style / RedRunner-style / ChopChop-style script
    set runs through `run_packs()` with zero pack fires." Add fixtures that
    load each non-FPS test project's transpiled output (or a minimal stub of
    it) and assert that none of the three new packs emit `<Name>Shared`,
    rewrite Touched handlers, or splice visibility fixups.
  - **P2.c — `convert_interactive.py upload` xml_to_binary error fallback.**
    On `xml_to_binary()` exception the upload path deletes `.rbxl` and
    "falls back" to uploading the `.rbxlx` (`convert_interactive.py:996`),
    but the surrounding comments and `u2r.py:528` both state Open Cloud
    rejects XML with HTTP 400. Fix: on regen failure, raise a hard error
    instead of producing a guaranteed-failed publish — OR verify the XML path
    actually works and delete the misleading "binary-only" comments.

- [ ] **P3 — Optional component-aware autogen injection.** The
  remaining piece of the original FPS-extraction P1: replace the
  heuristic-based ``detect_fps_game`` with component-aware injection.
  e.g. emit a Cinemachine bridge only when the scene actually has a
  CinemachineVirtualCamera, not when the script heuristic matches.
  Currently the detector still runs as a soft hint when
  ``--scaffolding=fps`` is not passed.

  Earlier phases of this work shipped:
  - PR #66: generalized ``script_coherence_packs.py``.
  - PR #1 (#68): made FPS scaffolding opt-in via ``--scaffolding=fps``.
  - PR #2: split ``fps_client_generator.py`` into
    ``converter/scaffolding/fps.py`` (FPS-specific) and
    ``converter/autogen.py`` (generic autogen scripts). The
    transitional re-export shim was deleted once all internal
    callers had migrated.

  Earlier cleanup landed:
  - PR #3: extracted ``connectClient`` into
    ``runtime/event_dispatch.luau`` (auto-injected when
    ``--scaffolding=fps`` opts in). HUDController now requires the
    shared module instead of inlining the BindableEvent vs
    RemoteEvent fork.


- [ ] **P2 — Persistent prefab/asset cache.** Prefab library is in-memory
  only; rebuilt from disk every conversion. Needs a cache-schema design
  pass before code — see
  [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md)
  § "Persistent prefab/asset cache".

- [ ] **P1 — Upstream classifier misroutes Roblox-dead Unity-rendering
  modules to server-only.** Phase 2a slice 7 audit, 2026-05-30.
  `infer_module_domains` classifies Unity-rendering helpers (`WaterBase`,
  `PlanarReflection`, `Displace`) as server-only because their only
  observed callers happen to be server-side. But the modules' Unity APIs
  (`OnWillRenderObject`, Camera-based reflection rendering,
  `Shader.EnableKeyword`) have no Roblox equivalent — the modules
  effectively run as dead code in the converted output. Routing them to
  `ServerStorage` based on caller-graph evidence is technically correct
  given the inputs, but ships dead modules to the wrong container.
  Fix shape: either (a) a dead-code pruning pass that drops modules whose
  bodies are dominated by Unity APIs without Roblox equivalents, or
  (b) an upstream classifier signal that detects "Roblox-dead" modules
  and short-circuits their domain inference. Option (a) is the more
  general fix.

- [x] **P1 — Transpiler false-positive `require()` injection poisons
  storage classification.** Phase 2a slice 7 audit, 2026-05-30. FIXED
  2026-06-01 (`fix/dead-require-from-runtime-lookup-generics`).
  **Corrected root cause (the audit's framing was incomplete):** the
  `Plane→GameManager` false edge is NOT a phantom — `Plane.cs` references
  `GameManager` via `FindObjectOfType<GameManager>()` (a RUNTIME scene
  lookup → `self.host.findObjectOfType("GameManager")`). The reference
  extractor's generic-type-arg regex (`script_analyzer.py`
  `<\s*([A-Z]\w+)`) captured that runtime-lookup type arg as a
  `referenced_type` → `dependency_map["Plane"]=[GameManager]`. That single
  fault poisoned BOTH consumers: the legacy require-injector AND the
  GENERIC-mode topology `caller_graph` (built directly from
  `dependency_map`; the prescribed injection-site guard would NOT have
  fixed generic mode, where `inject_require_calls` doesn't even run).
  **Fix (at the source, helps both modes):** exclude the type args of
  GLOBAL scene-lookup generics (`FindObjectOfType<T>` /
  `FindObjectsOfType<T>`) from `referenced_types` — they locate an
  already-existing instance, creating no dependency edge and no module
  require. Genuine deps are still captured via the new/field/param/base
  patterns. See `_GLOBAL_LOOKUP_GENERIC_METHODS` in `script_analyzer.py`.
  **Scoped narrowly (Codex review):** COMPONENT-lookup generics
  (`GetComponent<T>` / `AddComponent<T>` / `TryGetComponent<T>` / …) are
  NOT excluded — they're real peer edges the caller_graph / reachability
  consumers (`resolve_caller_graph`, `derive_reachability_requirements`,
  `_compute_network_behaviour_reachable`) need; dropping them would orphan
  a component referenced only that way. (Whether a component-lookup edge
  should ALSO drive a `require()` is a separate, pre-existing concern at
  the injection site.)

## Materials & meshes

- [ ] **P1 — Embedded-mesh resolver only warns on bad sub-mesh count, then ships arbitrary geometry.** PR #121 review (codex + Claude, 2026-05-21).
  `pipeline.py:2101+` asserts the "embedded synthesised FBX must resolve to
  exactly one sub-mesh" invariant via `log.warning`, but `_resolve_sub_mesh()`
  still returns `sub_meshes[0]` for embedded keys when the invariant is
  violated. The comment claims "loud-fail" but the implementation is
  loud-warn. Result: when `_strip_extra_geometries_and_dependents` misses a
  Geometry node, the conversion ships wrong geometry instead of falling back
  safely. Fix: quarantine the bad key by removing it from `mesh_hierarchies`
  + `mesh_native_sizes` and appending to `asset_upload_errors`, so the
  face-decal fallback path in `scene_converter` takes over instead of binding
  to a coincidence Geometry. Codex sketched the ~12-line diff in the review.

- [ ] **P2 — Multi-sub-mesh sizing emitters still inline the scale chain (parallel to the adapter).** PR #121 review (codex + Claude, 2026-05-21).
  Commit `141892d` centralised single-mesh sizing through
  `scene_converter._native_meters_from_roblox_size` ("one input shape, one
  formula"), but the multi-sub-mesh paths at `scene_converter.py:1465-67,
  1535-36, 2798-09, 3603-04, 3625-26, 3679-80, 4189-90` (8 sites) still
  compose `_get_fbx_import_scale × _get_fbx_unit_ratio × STUDS_PER_METER`
  inline. No current double-scale bug because all 8 sites land on the
  FBX path where the `.prefab`/`.asset` short-circuit is safe — but the
  parallel implementations are exactly where the previous drift re-entered.
  Fix: extract a `_mesh_scale_factor_studs_per_native(mesh_guid, guid_index)`
  helper and route every call site through it; or update the commit message
  + `_compute_mesh_size` module comment to say "FBX paths still inline,
  embedded path funnels through the adapter."

- [ ] **P2 — "No modern FBX template" silently degrades every embedded mesh.** PR #121 review (codex, 2026-05-21).
  `pipeline.py:1245` (`_upload_embedded_meshes`): when no 7.x FBX template is
  available, the function emits a `log.warning` and returns. Embedded meshes
  fall back to face-decal rendering, but the conversion summary reports a
  clean upload phase (nothing hits `asset_upload_errors`). Fix: append
  per-embedded-key errors to `asset_upload_errors` so the conversion report
  surfaces the degraded path.

- [ ] **P2 — FBX template selection is non-deterministic + UpAxis-coupled.** PR #121 review (Claude, 2026-05-21).
  `pipeline.py:1230-1244` picks the first FBX in `manifest.by_kind["mesh"]`
  that has a Geometry node. (a) Order depends on filesystem walk — different
  developers get different templates. (b) The template's `GlobalSettings.UpAxis`
  carries over to every synthesised embedded mesh; if the first match was
  Blender-exported Z-up, every embedded mesh rotates 90° around X. Fix:
  sort candidates by `relative_path` for determinism, and filter to
  `UpAxis == 1` (Y-up) OR normalise the synthesised FBX's UpAxis inside
  `synthesize_fbx`.

- [ ] **P2 — Full SurfaceAppearance round-trip through templates.** PR 5
  deferred. The smoke ran with `--no-upload` so real asset IDs never wired
  through `ReplicatedStorage.Templates`. Verify on a full upload run.

- [ ] **P1 — `read_fbx` rejects FBX version >= 7500 (64-bit offsets).**
  `fbx_binary.py:read_fbx` raises `NotImplementedError` for FBX 7500+
  (FBX 2016 and newer — extremely common for modern Unity assets). Effect:
  `mirror_fbx_handedness` catches the error and returns `False`, so the
  pipeline (`pipeline.py:1122-1123`) uploads the **raw original** — no
  handedness mirror, no bounding-box computation, no sub-mesh resolution.
  Modern FBX silently degrade. Found in the trash-dash conversion run
  (2026-05-18): `Cat.fbx` / `CatBase.fbx` / `Racoon.fbx` are all 7500;
  raw upload of these heavily-rigged multi-skin character FBX is rejected
  by Roblox Open Cloud with "Failed to parse the uploaded file".
  Fix: extend `_read_node` / `_write_node` to handle 7500's 64-bit
  EndOffset / NumProperties / PropertyListLen header fields. Note: even
  with 7500 read support, complex skinned-character FBX still cannot go
  through the Open Cloud mesh endpoint (see next item) — this fix recovers
  handedness + bbox for *static* 7500 meshes.

- [ ] **P2 — Skinned/animation-only FBX uploaded as meshes and rejected.**
  Two sub-cases found in the trash-dash run (2026-05-18):
  (a) Animation-only FBX (e.g. `Cat_Jump.fbx`, FBX 7400) contain a single
  `Geometry` node with **zero vertices**. The asset extractor classifies
  any `.fbx` as `kind="mesh"`; `mirror_fbx_handedness` finds the empty
  Geometry node and returns `True` without checking vertex count, so the
  empty file uploads and Roblox rejects it ("Cannot import file with no
  mesh content"). 24 such files failed this way.
  (b) Rigged character FBX (Skin/Cluster/Deformer nodes) cannot be ingested
  by the Open Cloud mesh endpoint at all — consistent with the existing
  `docs/UNSUPPORTED.md` skeletal-mesh limitation.
  Fix: detect zero-vertex `Geometry` and skinned FBX pre-upload; skip them
  and surface to `UNCONVERTED.md` instead of issuing a doomed upload.
## Infrastructure

- [ ] **P1 — Converter doesn't wire ScreenGui enable/disable into the state
  machine.** Trash-dash Mode-2 (2026-05-19): all 4 converted ScreenGuis
  (`Loadout`, `Game`, `GameOver`, `Leaderboard`) ship with `Enabled=true`,
  so they render stacked at once — an opaque white wall over the menu.
  In Unity the `GameManager` state machine shows/hides canvases per state
  (Loadout / Game / GameOver). The converter neither (a) sets non-initial
  canvases `Enabled=false` at build time, nor (b) emits state-machine code
  that toggles `ScreenGui.Enabled` on state transitions. Explore: where
  Unity `Canvas`/`GameObject.SetActive` and per-state canvas wiring should
  map to `ScreenGui.Enabled`, and why it is dropped. Note: `RbxScreenGui`
  (`core/roblox_types.py`) currently has no `enabled` field, and neither writer
  (`rbxlx_writer.py`, `luau_place_builder.py`) serializes `Enabled` — so the fix
  needs `Enabled` plumbed through the type + both writers, plus the
  state->visibility wiring. (This is its own gap — not the `classify_storage`
  P1, which only mutates scripts.)

- [ ] **P1 — Phase 4a.5 agent-override ingestion is unimplemented.**
  `storage_classifier.py` ("Phase 4a.5") is correctly meant to run during Step 4a
  — that is not the bug. The `/convert-unity` skill
  (`references/phase-4a-storage-classification.md`) designs 4a.5 as: the classifier
  emits a *proposed* `storage_plan` -> the agent reviews it -> the agent overrides
  by editing `storage_plan` in `conversion_plan.json` -> 4b/downstream use the
  overridden plan. `StoragePlan.overrides_applied` (`storage_classifier.py:113`) is
  the field reserved for this. But the override half is never built:
  - `classify_storage()` (`storage_classifier.py:119`) has no `existing_plan` /
    `overrides` parameter — it builds a fresh `StoragePlan()` from scratch every call.
  - `overrides_applied` is a declared field with a hopeful comment; nothing
    populates it.
  - `_classify_storage()` (`pipeline.py:3336`) calls the classifier with only
    `scripts` + `dependency_map`, then unconditionally rewrites `conversion_plan.json`
    (`pipeline.py:3356`) — and re-runs on every `write_output`. (Rehydration at
    `pipeline.py:3242` does briefly read the prior `conversion_plan.json` to seed
    `script_type`/`parent_path`, but `_classify_storage()` recomputes and overwrites
    it later in the same `write_output()` pass.)
  So an agent-edited `storage_plan` is silently discarded by the next `assemble`.
  Confirmed in the trash-dash Mode-2 run (2026-05-19): Step 4a authored
  1 server / 49 shared / 1 server-module / 8 overrides; after `assemble`,
  `overrides_applied` was 0. Fix: do NOT make the whole prior plan sticky — that
  would freeze stale auto-classifications and block future classifier improvements.
  Instead persist *explicit manual overrides* separately (a `name -> container`
  map), keep running fresh classification every time, then overlay only those
  explicit overrides and populate `overrides_applied`. This is the real
  plan->pipeline wiring gap — broader than the `--skip-architecture-step` gate
  from PR #109.

- [ ] **P1 — Storage classifier's ModuleScript path is fragile and under-tested.**
  Container assignment splits into two unequal paths. `Script` / `LocalScript`
  route by simple type rules (`Script` -> `ServerScriptService`, `LocalScript` ->
  `StarterPlayerScripts`; `storage_classifier.py:338`) — robust, hard to get wrong.
  `ModuleScript` routes by a caller-graph heuristic (`storage_classifier.py:309`)
  that (a) **ignores the module's own client/server API surface** and infers only
  from callers; (b) is fed a **regex-scanned** call graph (`storage_classifier.py:232`)
  that the synthesized `or game:GetService("ServerStorage"):FindFirstChild(...)`
  require-fallback (`script_coherence.py:69,183`) poisons into treating callers as
  server-side; and (c) is then **not corrected** — both
  `_fix_client_server_classification` (`script_coherence.py:423`) and
  `_propagate_client_classification` (`script_coherence.py:392`) skip modules.
  Every storage bug hit in the trash-dash run lives on this path. It is
  **under-tested**: the primary test project SimpleFPS is 76% `Script` (and the
  3+-client-API promotion guard at `script_coherence.py:136,237,281` keeps its big
  client scripts as `LocalScript`s), so it routes *around* the buggy path;
  `tests/test_storage_classifier.py` covers only toy module cases. trash-dash
  (88% `ModuleScript`) is the first real module-heavy game to exercise it — and it
  fell over. Fix direction: have the module router also inspect the module's own
  client/server API surface (not callers only); extend the correction passes to
  cover modules; harden the call graph against synthesized require-fallback
  strings; add a module-heavy test project / fixtures. (Claude + Codex cross-model
  analysis, 2026-05-19.)

- [ ] **P2 — Retire genre-specific scaffolding; make the converter fully
  genre-agnostic.** `--scaffolding=fps` (`u2r.py convert`) injects FPS-genre
  scripts (client controller LocalScript, HUD ScreenGui, HUDController) and
  carries backward-compat machinery in `pipeline.py` — `_fps_artifacts_on_disk`,
  `_fps_artifacts_at_init`, `apply_scaffolding`, plus the `converter/scaffolding`
  module. This cuts against the stated "the converter makes no game-genre
  assumptions" direction (the `--scaffolding` help text itself says so) and the
  recent retire-character-animation / remove-gameplay-adapters trend. Bigger
  refactor: remove the `scaffolding` module, the `--scaffolding` flag, and the
  FPS artifact-detection code paths. Blocked on confirming no live conversion
  flow relies on FPS scaffolding. Surfaced during the CLI parameter audit
  (2026-05-18).

- [ ] **P2 — Three-flow byte-equivalence: u2r.py vs convert_interactive.py
  divergence (Phase 5.1 follow-up).** The byte-equivalence test landed
  with `test_three_flows_produce_identical_rbxlx` xfailed because the
  in-memory u2r.py path inlines scripts via `_convert_prefab_node` while
  the cross-process interactive path goes through `rehydration_plan.py`,
  producing different sets of Script Items. Harmonize the two paths so
  the test flips from xfail to xpass.
- [ ] **P2 — Standalone `.rbxm` file output per prefab.** PR 5 deferred.
  Toolbox convenience; no runtime dependency. Design notes in
  [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md)
  § "Standalone `.rbxm` per-prefab output".
- [ ] **P2 — Visual-compare baseline screenshot (Phase 5.4 follow-up).**
  CI step is wired, gated on `eval_baseline_screenshots/SimpleFPS_main.png`
  existing. Commit a known-good baseline from the next clean smoke run
  to activate the SSIM 0.85 gate; until then the step warns and continues.
- [ ] **P2 — Real-upload smoke secrets (Phase 5.2b / 5.3 follow-up).**
  CI jobs `real-upload-smoke` and `ai-convert-matrix` skip cleanly until
  their repo secrets are configured: `real-upload-smoke` needs
  `ROBLOX_API_KEY`, `ROBLOX_UNIVERSE_ID`, `ROBLOX_PLACE_ID`, and
  `ROBLOX_CREATOR_ID`; `ai-convert-matrix` needs `ANTHROPIC_API_KEY`.
  Wire them when CI billing allows.
## Type-strictness debt (forward-only gate landed; cleanup separate)

The no-Any gate prevents new smuggling. Existing-offender cleanup has
landed in dedicated PRs (#10 gate, storage_plan, ported-module signatures
PR #34, PipelineState PR #36, trivial 3-fix + ConversionContext final 4).

No tracked remaining items. The `scene_converter.py` `mesh_hierarchies`
field that previously lived here is already typed
`dict[str, list[MeshHierarchyEntry]]` (see `scene_converter.py:177`).

---

For platform limitations, Unity features with no Roblox equivalent, and Open
Cloud API limits, see [`docs/UNSUPPORTED.md`](docs/UNSUPPORTED.md). For
architectural debt and bug-shaped gaps, see [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md).
For long-horizon strategic work, see [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md).
