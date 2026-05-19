# Converter TODO

Active work items only. Completed work + PR execution logs live in `TODO_archive.md`.

Priority: **P0** = blocks gameplay, **P1** = significant quality, **P2** = nice to have.

---

## Pipeline / runtime gaps

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

## Materials & meshes

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
