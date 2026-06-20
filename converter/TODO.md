# Converter TODO

Active work items only. Completed work + PR execution logs live in `TODO_archive.md`.

Priority: **P0** = blocks gameplay, **P1** = significant quality, **P2** = nice to have.

Citation rule: reference code by grep-target (`file.py` + symbol/string), never by line
number — line refs rot (the 2026-06-11 review found every CLAUDE.md/TODO.md line ref stale).

---

## Architecture & platform currency (2026-06-11 review)

From a full repo audit + external fact-check of platform claims against Roblox Creator Docs
(2024-2026 changes). Verified CORRECT and needing no action: coordinate/quaternion math, stud
scale (0.28 m), 2048-stud part cap, FBX-7500 diagnosis, task-library usage, two-tier publish
cache. The items below are where code or docs are stale or wrong.

- [ ] **P1 — Architecture docs describe a retired pipeline.** ARCHITECTURE.md/CLAUDE.md list 10
  phases; `pipeline.py:PHASES` has 12 (`plan_scene_runtime`, `materialize_and_classify` and the
  whole scene-runtime-topology / contract-verifier / `contract_pipeline.py` subsystem are
  undocumented). The "Asset Resolution (Critical)" section is obsolete twice: textures upload as
  `Image` assetType (`cloud_api.upload_image` — no Decal→Image resolution needed) and mesh
  resolution is headless via the official Open Cloud Luau Execution API (`execute_luau` =
  `luau-execution-session-tasks`) — not "Studio-required". Also fix: runtime-module list
  (animator retired; `scene_runtime.luau`/`scene_camera_input.luau` undocumented), AI backend
  (Claude CLI preferred + Anthropic API fallback), `--api-key` not `--api-key-file` (incl. a
  stale error msg in `u2r.py`), 6 undocumented u2r subcommands, stale test counts, and convert
  all hardcoded `file:line` citations to grep-targets.

- [ ] **P1 — Workspace gravity comment is false (the comment only — physics divergence handled).**
  PARTIAL: the 5.6× physics divergence is now corrected at runtime via `project_gravity.py` +
  `scene_runtime.luau` clone-site hook (PR #194, in tree) — dynamic assemblies use scale-faithful
  gravity from the Unity project's real `DynamicsManager.asset`. REMAINING: `rbxlx_writer.py` still
  writes the static `Gravity = 196.2` with the wrong comment "(9.81 m/s²)" (at 0.28 m/stud that's
  54.9 m/s²; 196.2 = 20×9.81 is the pre-2019 5 cm/stud convention). Fix the comment; decide whether
  the static workspace Gravity value should also change (a) `9.81 × STUDS_PER_METER ≈ 35.0`,
  (b) keep 196.2 for Roblox-native avatar feel, or (c) config knob — playtest (a) before committing.

- [ ] **P1 — Transpile gate: silent pass when luau-analyze is missing; cheap semantic upgrade
  available.** `utils/luau_analyze.py` returns `[]` when the binary is absent — "syntax-gated"
  is silently false (likely incl. CI ubuntu jobs). Fail loud / stamp the report. Then: the
  KNOWN_ISSUES "validator catches syntax, not Roblox API semantics" gap is not a research
  project — `luau-lsp analyze --definitions:@roblox=globalTypes.d.luau`
  (JohnnyMorganz/luau-lsp) ships maintained Roblox API types; wire into the existing
  lint+reprompt loop.

- [ ] **P2 — AI truncation guard.** `code_transpiler` never checks
  `response.stop_reason == "max_tokens"`; truncated-but-syntactically-valid Luau ships
  incomplete logic. (Promoted from KNOWN_ISSUES.)

- [ ] **P2 — `conversion_context.json` save is not atomic.** `ConversionContext.save` is a
  single `write_text`; a crash/Ctrl+C mid-write corrupts the only cross-CLI state file (no
  KeyboardInterrupt handling either — KNOWN_ISSUES). Write-tmp-then-rename.

- [ ] **P2 — CI gate gaps.** (a) `nightly-summary` echoes `cold-e2e` result but never gates on
  it — a nightly cold-e2e failure reports green (`test.yml`). (b) smoke/cold-e2e convert a
  hardcoded local clone (`UNITY_SIMPLEFPS_DIR`), not the pinned submodule — the gate can drift
  from repo state; one self-hosted Mac = bus factor 1. (c) Consider: required gate on the
  cached AI shape + advisory fresh-AI run, so pre-merge red isn't a function of model variance.

- [ ] **P2 — Attribute sanitizer: raise cap, guard RBX prefix.** Real SetAttribute rules:
  alphanumeric + `. - / _`, max **100** chars, reserved `RBX` name prefix. Our `[A-Za-z0-9_]`
  charset is safe-but-lossy, the 64 cap can go to 100, and nothing guards `RBX*` (throws at
  runtime). `core/flag_names.py` + serialized-field attribute emission.

- [ ] **P2 — ".rbxlx → HTTP 400" is folklore; fix the comments.** Official place-publishing
  docs accept both `application/xml` (.rbxlx) and `application/octet-stream` (.rbxl), and
  `cloud_api.publish_place_file` already sets Content-Type by extension. Verify one real
  `.rbxlx` publish, then rewrite the three "Open Cloud rejects XML with 400" comments
  (`convert_interactive.py` upload, `u2r.py`) — the on-error fallback that deletes the `.rbxl`
  and publishes the `.rbxlx` is probably fine; the comments steer changes wrong.

- [ ] **P2 — Document Open Cloud quotas.** 100 audio uploads/month (10 if not ID-verified),
  video 20/day, 20 MB/call, 120 req/min. A sound-heavy game can exhaust the monthly audio
  quota in one conversion. Add to UNSUPPORTED.md + surface in the upload report.

- [ ] **P2 — Unit/easing sweep for component conversion.** ParticleSystem `startSpeed` is
  emitted raw m/s (force-over-lifetime and light range ARE scaled) → particles ~3.6× slow;
  audit all unit-bearing props in `component_converter.py` once. Animation easing is
  hardcoded Quad/InOut at all four TweenInfo emit sites in `animation_converter.py` — Unity
  AnimationCurve easing is parsed but dropped.

## Legacy retirement (strategic direction, 2026-06-11)

- [ ] **P1 — Retire legacy mode + the coherence-pack layer; generic is the path forward.**
  Direction set 2026-06-11: stop *investing new architecture* in legacy. The eventual end state
  relocates semantic-fidelity jobs to the deterministic lowering layer / host-runtime services
  (the Turret/HudControl pattern — KNOWN_ISSUES § "Scene-runtime generic mode") and deletes
  `script_coherence_packs.py`, `--scaffolding=fps` + `converter/scaffolding/` +
  `_fps_artifacts_*` in `pipeline.py`, the `detect_fps_game` heuristic, and the legacy
  `scene_runtime_mode` branch. Gates before any deletion:
  - (a) generic parity on the e2e fixture set — player-bind Phase 2 (`self.host.player`
    authority, then `REQUIRE_PLAYER_BIND` 0→1 in `test.yml`);
  - (b) contract verifier flipped fail-closed (Architecture section above);
  - (c) deterministic-lowering homes for the known pack-covered gaps verified on a generic
    conversion.
  **NOTE (hybrid, 2026-06-19):** this is a north-star, NOT a license to drop the live pack
  bugs. The pack layer is still in active use and being fixed tactically (door binding-race
  #208 landed in `animation_converter`; camera-mount / turret-bullet-damage / checkmark-toggle
  sessions are touching packs/lowering now). The specific pack-hardening items below
  (door-pack widening, genre-genericness P1.a/b/c, weapon-mount packs) **remain tracked** until
  their semantics have a verified deterministic-lowering home — closing them IS gate (c), not a
  precondition waived by this decision. `docs/refactor_plan.md` (2026-06-11): pack-split lane
  (PR-E0/E/F) dropped — packs are deleted under retirement, not split.

## Pipeline / runtime gaps

> **Top remaining gameplay gap (updated 2026-06-19):** the **turret projectile physics +
> damage** (#8, P0 below) — blocks the F16 fixture; a generic-flow coverage gap (the legacy
> projectile primitive was never ported). The door (#9, F10) is DONE — animation driver-domain
> narrowing (#195) + the Anim_Door runtime-placement binding race (#208); both archived.

- [ ] **P0 (generic) — F16 turret never damages: bullet drops to ground (#8 projectile physics) + damage hits wrong health surface. Analyzed 2026-06-15 (Claude + Codex; static + rbxlx, live Studio owed).**
  **IN PROGRESS (2026-06-20): actively owned by a concurrent session — do NOT double-claim.**
  NOTE: PRs #193 / #197 / #198 (merged 2026-06-15/16) already touched stages 3–4 (host-impulse
  launch + Humanoid-damage API + bind-Touched-to-colliding-body); the concurrent session owns
  re-verifying whether F16 is now green and closing whatever remains. The analysis below predates
  those merges — treat it as background, not the current state.
  The turret kill-chain has FOUR stages; two fail, for unrelated reasons. **None is a client/server
  problem** — Turret + TurretBullet + Player are all Rule-7 `domain:client` in single-player, so every
  interaction is local (contrast the door #9, which IS a domain split).

  1. **Targeting (fragile).** `Turret:Awake` connects an EDGE `Touched` (lowered from `OnTriggerStay`),
     not the host stay-poll. Unity `OnTriggerStay` fires every frame in the sight volume; the edge only
     re-checks the `angle<55° + LOS raycast` gate on a contact edge → misses already-inside / wrong-cone
     edges. Host HAS `connectGameObjectSignalStay` (`scene_runtime.luau:538`) but the generated script
     doesn't use it. This is relation **#1 (callback-kind TRIGGER_STAY)** — the comment-keyed lowering
     ABSTAINED on the AI's combined `-- OnTriggerStay -> Touched ; OnTriggerExit -> TouchEnded` comment
     shape. (Re-confirm on current main vs #181.)
  2. **Firing (works).** `instantiatePrefab(self.turretBullet, workspace, tOrigin:GetPivot())` clones the
     RS template, stamps a runtime placement id, builds components, runs `_runAwakeEnableStart`
     (`scene_runtime.luau:2025-2053`). **The old "TurretBullet SRI gap" framing (PR #145 / the F16
     handoff) is STALE — binding works; the failure is downstream.**
  3. **Bullet physics — FIRST HARD FAILURE; bullet DROPS to the ground.** The runtime-cloned template
     body is `TurretBullet_Mesh` **`Anchored=false`** (confirmed in the ReplicatedStorage template, not
     the anchored scene leftovers). `GetComponent("Rigidbody")`→BasePart returns it, so
     `rb:ApplyImpulse(LookVector * 60)` DOES apply — but `60` is the RAW Unity force (no ×STUDS_PER_METER),
     there is NO anti-gravity, no muzzle push-out, no swept raycast. Net: a weak nudge dominated by
     Roblox gravity (196) → the bullet arcs into the ground immediately and never reaches the player.
     This is relation **#8 (projectile/physics-semantics)**: legacy had `bullet_physics_raycast` (stud
     velocity + anti-gravity + raycast hit); generic SKIPS the coherence-pack layer → **coverage gap.
     STRONGEST generic-refactor tie.** Needs its own design (§7.1 of the schema).
  4. **Damage (independent failure; survives a stage-3 fix).** Even on a forced bullet↔player touch,
     `TurretBullet.Awake` calls `findObjectOfType("Player"):TakeDamage(damage)`. `Player:TakeDamage`
     (`Player.luau:332`) only decrements the component's internal `self.curHealth` + fires the HUD
     `HealthUpdate` — it **never touches `Humanoid.Health`**. The F16 fixture asserts on `Humanoid.Health`,
     so the hit isn't measured. CONTRAST: `Mine.luau:35` does `hum:TakeDamage` on the real Humanoid (which
     is why F15 mine damage works). Same Unity `SendMessage("TakeDamage")`, AI-translated to DIFFERENT
     targets — an AI-translation inconsistency + a fixture-vs-game-model mismatch, NOT generic-caused.
     Fix EITHER: bullet damages the Humanoid like the mine, OR the game's health model is unified on
     `Player.curHealth` and the fixture checks that.
  **Order:** stage 3 is the first blocker but stage 4 is independent — fixing the projectile alone won't
  turn F16 green. Schema cross-refs: §3 row 8 (projectile), §8 T-bullet row (corrected 2026-06-15).

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

  - **P1.a — `localscript_api_shim` type-aware accessor classification. — DONE (`95b4fc5`,
    moved to archive).** `_classify_api` now gates the boolean shim on `_is_boolean_state_var`;
    non-boolean accessors fall through to `return nil`. Kept here only as a pointer — P1.b/P1.c below remain open.
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
  NEARLY DONE: `_read_node` / `_write_node` ALREADY handle the 7500 64-bit
  EndOffset / NumProperties / PropertyListLen header fields + 25-byte NULL sentinel (the
  `ver >= 7500` `<QQQ>`/24-byte branches are implemented in tree). The ONLY thing still
  rejecting 7500 is the early `raise NotImplementedError` gate at the top of `read_fbx` —
  remove it so the existing 64-bit path runs (then test against a real 7500 file). Keep the
  32-bit path — Blender still exports 7400. Note: even with 7500 read support, complex skinned-character FBX still
  cannot go through the Open Cloud mesh endpoint (see next item) — this fix recovers
  handedness + bbox for *static* 7500 meshes. Alternative worth a spike: Open Cloud now
  accepts `.gltf/.glb` for Model uploads (Oct 2025), which may bypass FBX patching entirely.

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
  and surface to `UNCONVERTED.md` instead of issuing a doomed upload. Note:
  EditableMesh gained programmatic skinning APIs (Studio beta, 2025-06) — a
  possible future skinned-mesh path; the UNSUPPORTED.md stance stands for now.

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
## Type-strictness debt

Forward-only no-Any gate landed; no tracked remaining cleanup items.

---

For platform limitations, Unity features with no Roblox equivalent, and Open
Cloud API limits, see [`docs/UNSUPPORTED.md`](docs/UNSUPPORTED.md). For
architectural debt and bug-shaped gaps, see [`docs/KNOWN_ISSUES.md`](docs/KNOWN_ISSUES.md).
For long-horizon strategic work, see [`docs/FUTURE_IMPROVEMENTS.md`](docs/FUTURE_IMPROVEMENTS.md).
