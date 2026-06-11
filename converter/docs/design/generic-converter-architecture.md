# Generic converter architecture — north star (fault-line model + gameplay-IR relations)

**Status:** north-star / durability design-of-record for generic-mode conversion. The **umbrella landing
doc**: it frames *why* each generic-mode component exists and *where* the durable fix belongs, and links
out to the detail docs — it does NOT restate their mechanism, nor own their scope.
Authored 2026-06-06, synthesizing a four-voice component audit (3× Claude + Codex high-effort) + two
Codex reviews of this consolidation.
**Owner:** unity2rbxlx converter team.

**Read this first** to pick *what* to build and *which rung* it belongs at; then implement from the detail
doc it points you to (§7). This doc is the decision frame, not a build spec.

**Status discipline (this doc is a navigational index, not a status dashboard).** This doc owns durable
*intent / architecture / plan*. It does NOT own *current implementation status* — that is **derived from
git + [`scene-runtime-execution-log.md`](scene-runtime-execution-log.md) + open PRs**, never
hand-maintained here (a hand-written status snapshot rots — the IR doc's stale header proved it). Where a
status appears below (the §3 maturity tags, the §5 audit counts, §7's checkboxes) it is a *dated pointer
to where the live truth lives*, not the truth itself. To answer "what's the state of X," follow the
pointer to the owning doc / the code, don't trust a number typed here.

**Layering (Codex review — unify the RULE, not the LAYERS):** all the docs below share ONE governance
rule — *one deterministic upstream authority owns every load-bearing decision the AI must not make; the
transpiler and the runtime are bound consumers; a verifier enforces it.* They are NOT one layer:
`scene-runtime-contract.md` owns the runtime MECHANISM; `scene-runtime-architecture-ir.md` owns DEPLOYMENT
topology (and explicitly disclaims being a general semantic IR); this doc owns the durability THEORY +
the gameplay-semantic relation catalog. Same rule, different fact families — kept in separate docs on
purpose.

**"Authority" has modes.** Owning a fact is not the only form: the shared-flag channel shows authority can
mean *gate-and-record an irreducible runtime mechanism* rather than *own each write*; the verifier's role
is *check*. A relation's mechanism column below says which mode applies.

**Producer boundary (preserved):** the planner (`scene_runtime_planner.py`) remains separate structural
truth, consumed read-only. Relations are facts an authority derives FROM planner output — they do not
blur into the planner.

---

## 1. The one goal + the fault-line model

**Goal.** A converted Unity game plays correctly in GENERIC scene-runtime mode **for ANY valid AI
transpilation** of its scripts — verifiable at build time, not only on a lucky (cached) e2e run.

**Why this keeps breaking.** Core gameplay bindings (player↔character, turret detect/aim/fire, door
key-flag, pickup/machine progression) have regressed repeatedly because the fix was made by *reading the
non-deterministic AI output* and patching it. A fresh conversion with an equally-valid different AI shape
silently breaks the binding. The durable framing is a ladder of "rungs," with a **fault line** between
rung 2 and rung 3:

```
 rung 1   coherence @patch_pack, name-keyed (s.name=="Turret")            ── most fragile
 rung 2a  deterministic lowering keyed on a PARSED / PLAN-DERIVED fact     ─┐ above the fault line:
 rung 2b  deterministic lowering keyed on the AI OUTPUT SHAPE             ─┘ still reads AI output
 ─────────────────────────────  THE FAULT LINE  ─────────────────────────────
 rung 3   host runtime authority + a deterministic UPSTREAM Unity signal   ── durable: no AI shape
          stamped at parse/spawn                                              can break it
```

A *second* fault line runs **inside rung 2**, between 2a (keyed on a parsed Unity fact — robust) and 2b
(keyed on the emitted Luau text or a prompt-mandated comment — silently abstains on a fresh AI shape).
The durability rule: **anchor load-bearing identity/classification/binding on the deterministic upstream
Unity signal, never on a fingerprint of the AI output.** Pieces that reached rung 3 (`_IsTriggerVolume`,
`_SpawnLook`, the host poll, `has_character_controller`) never regressed; pieces that stopped at rung 2
are exactly the open regressions.

---

## 2. Two workstreams (orthogonal; this doc does NOT lock their order — see §7 for a suggestion)

The "minimize the fragile layer" goal decomposes into two workstreams that barely overlap:

```
 WORKSTREAM 1 — generic scene-runtime + host authority   (Class-B: execution model)
   the host OWNS the Unity execution model + the engine-level bindings, keyed on upstream signals.
   detail docs:  scene-runtime-contract.md (mechanism)
                 → scene-runtime-architecture-ir.md (topology authority, phased build)
                 → scene-runtime-domain-signals.md (domain classifier)
                 → camera-input-fidelity-plan.md (camera+embodiment — shipped worked example)
                 → player-embodiment-authority-design.md (host.player — the next runtime primitive)
                 → inline-over-runtime-wrappers.md (the lowering-vs-wrapper principle)

 WORKSTREAM 2 — upstream contract synthesis   (Class-A: cross-script contracts)
   derive shared-flag / event-channel relations ONCE from the whole-corpus parse, generate BOTH ends
   from the contract → the cross-script pack zoo retires by construction. The MACHINERY is largely BUILT
   (scene-runtime-architecture-ir.md: shared_flag_channels.py, cross_domain_edges.py, edge_enrichment.py,
   contract_verifier.py all shipped on upstream/main as of 2026-06-03). The remaining work is (a) finish
   the Phase-2b tail (retire the legacy pickup_remote_event_server pack + dependents) and (b) generalize
   the cross-domain bridges to the first-class #5/#6 relations — an INCREMENT on shipped infra, not a
   from-scratch build.
```

The component audit (§5) found the runtime side is the real architecture and the pack side is "mostly an
archaeological record of missing IR relations." Both workstreams are real; they target different rungs of
different components.

---

## 3. Gameplay-IR relation catalog — CLOSED-BY-DEFAULT

The recurring pattern: today the AI transpiles each script *blind to the others*, guessing a relation from
local C#; a downstream pack/lowering reconciles it by fingerprinting the output; it breaks on a new shape.
The fix is to resolve each relation ONCE from the whole-corpus parse — a **gameplay-IR relation** — that
the emitter and runtime both read.

> **This catalog is CLOSED BY DEFAULT** (Codex review — the omnibus risk is real; 5 of 8 below are not yet
> built, "exactly where bad abstractions freeze"). It is NOT a general semantic IR and must not grow into
> one. A new relation is admitted ONLY if it meets all three **admission criteria**:
> 1. exactly one **deterministic upstream Unity signal** it anchors on (parse/convert-time);
> 2. exactly one **named pack/lowering it retires** (a concrete fragility it removes);
> 3. exactly one **concrete consumer/mechanism** (host primitive, lowering, or verifier check).
> Until a relation is BUILT + corpus-verified it is **candidate backlog**, not architecture-of-record —
> tagged in the Maturity column. Scope note: `scene-runtime-architecture-ir.md` is *deployment topology*
> and explicitly fences off "becoming a general semantic IR"; these relations are the semantic layer it
> declines to own, governed by the same rule but admitted under this discipline.
>
> **HOW each relation is enforced** (the per-relation fact → output shape → enforcement-mode contract) lives in
> [`gameplay-ir-obligation-schema.md`](gameplay-ir-obligation-schema.md). The durability rule it adds: a fact is
> made *impossible to get wrong* (PRE-REWRITE / LOAD-BEARING TOKEN) or *loud when wrong* (FAIL-CLOSED VERIFIER) —
> never silently repaired. Boundary: reference/identity/placement → no verifier; behavioral (#1) + bilateral
> cross-script (#5/#6) → verifier mandatory.

| # | Relation | Upstream signal | Mechanism (mode) | Retires | Maturity |
|---|----------|-----------------|------------------|---------|----------|
| 7 | **domain/authority** `domain(Player)=CLIENT`, `authority(respawn)=SERVER` | domain classifier + authority rules | topology *owns* (drives client/server split; wires `onRespawn`) | `player_damage_remote_event`, `localscript_api_shim` | **BUILT** (domain-signals + IR cross-domain edges) |
| 3 | **component/controller-role** `role(Player)=PLAYER_CONTROLLER` | `_HasCharacterController` (scene_converter.py:2589); trigger-Collider; launched-Rigidbody prefab | role *selects* the host authority; no method-location | camera/movement facet *locators*, `fps_default_controls_off` | **PARTIAL** (`has_character_controller` identity is rung-3; locators still 2b) |
| 4 | **embodiment** `embodiment(PLAYER_CONTROLLER)→LocalPlayer.Character` | the controller-role (#3) | `self.host.player` *owns* camera/move/aim/respawn; rig shadow-synced | camera/movement facet *body rewrites* | **IN-FLIGHT** (camera shipped; player-embodiment-authority-design converged, not built) |
| 5 | **shared-flag-contract** `flag("hasKey",writer=Pickup,readers=[Door],store=Player)` | whole-corpus read/write sites; one canonical store | topology *gates+records* the channel; writer+reader generated from one contract | the 4 door-flag packs, `machine_item_check`, `door_player_flag_location` | **PARTIAL** (IR Class-2 `shared_flag_channels`, cross-domain only) |
| 6 | **event-channel** `event("ToggleDoor",producer=Machine,consumers=[Door])` | the Unity `event +=` / `SendMessage` topology | both ends generated from the channel | `door_machine_signal_listener`, `producer_consumer_bindable_events`, the 3 pickup-event packs | **PARTIAL** (IR Class-1 component-ref bridges) |
| 1 | **callback-kind** `callback(script,method)=TRIGGER_STAY` | the C# method signature `void OnTriggerStay(Collider)` | host schedules the stay-poll for that method | `trigger_stay_lowering` (keys on a `-- OnTriggerStay` comment), `trigger_stay_polling`+`v2` packs | **CANDIDATE** |
| 2 | **child/path-ref** `child_ref(Turret,0)=GameObject#Base` | `transform.GetChild(0)`/`Find("Weapon")` vs the parsed prefab hierarchy | IR carries the resolved instance; no ordinal lookup emitted | `child_index_lowering`, `turret_canonical_spatial_child`, `unity_transform_child_index` | **CANDIDATE** (partial: Phase-3 verifier check B) |
| 8 | **projectile/physics-semantics** `projectile(TurretBullet,launch=AddRelativeForce)` | the prefab is Instantiated + Rigidbody-launched (projectile-vs-recoil is indistinguishable at OUTPUT level) | route through a host projectile primitive (stud velocity + anti-gravity + raycast) | `bullet_physics_raycast` (rung-1, name-gated, absent from generic = coverage gap) | **CANDIDATE** (needs its own design) |
| 9 | **dynamic-component-ref → driver-domain** `driver(Door.open)=client Animator MB` | a dynamic `GetComponent<Animator>()` (no serialized ref) + C# param-writes (`SetBool`/`SetTrigger`) matched to the clip's `observed_attribute` | resolve the driver → place the generated Anim script in the writer's domain; cross-domain verifier backstops | per-project animation-driver `domain_overrides`; the `animation_routing` serialized-only resolution gap | **CANDIDATE** (door-scoped, 2026-06-11 PR #188; other animation drivers fail for different reasons) |

Workstream mapping: **#3, #4, #1, #2, #8, #9 feed Workstream 1** (host runtime primitives / placement — the player
fix is #3+#4); **#5, #6 feed Workstream 2** (upstream contract synthesis); **#7 is built**. Enforcement contract
for all relations: [`gameplay-ir-obligation-schema.md`](gameplay-ir-obligation-schema.md).

---

## 4. Class-A / Class-B decomposition

Coherence packs solve two fundamentally different problems; conflating them is why "just minimize packs"
was ambiguous:

- **Class-A — cross-script CONTRACT mismatch** (two scripts disagree on a shared flag/event/store/require).
  Whole-program contract synthesis CAN eliminate these (relations #5, #6). ~12 of 30 packs.
- **Class-B — Unity EXECUTION-MODEL / API semantic** (tick/order, GetComponent, OnTriggerStay,
  Destroy(obj,delay), coord flips, camera/input, physics, replication). These CANNOT be transpiled away —
  the need relocates into a runtime (centralized) or duplicated per-script boilerplate (distributed,
  losing the cross-component ordering barrier). The scene runtime is the centralized home. ~18 of 30 packs.

**The real choice is centralized vs distributed runtime, not runtime vs none.** Centralized wins (the full
keep-me-honest H1/H2 analysis lives in the originating run's design ledger under `~/.claude/harness-runs/`,
not in the repo).

---

## 5. Component-audit verdict (four voices: 3× Claude + Codex)

**Scene runtime → KEEP; existence justified.** ~60% is irreducible Class-B primitives that cannot be
inlined per-script: lifecycle staging, component registry/identity, host surface, lifecycle-scoped
scheduler + event wiring, `_tick` phases, OnTriggerStay synthesis, recursive destroy, out-of-tree camera
recomposition, dynamic queries. Critique is **scope, not existence** — ~1/3 of its *complexity* is
movable upstream: the per-placement prefab-identity restamp dance (exists only because the converter
doesn't mint per-placement ids), the `_UNITY_TO_ROBLOX_CLASS` map duplicating `api_mappings.TYPE_MAP`,
and the cross-domain edge re-derivation. One standing fidelity ceiling: `_tick` iterates `pairs(self._meta)`
= hash order, not `lifecycle_order`.

**Coherence packs → mostly should not exist as-is.** Scorecard *(dated snapshot, 2026-06-06 — these counts
drift as packs/lowerings change; the live truth is `script_coherence_packs.py` + the `*_lowering.py` files,
re-derivable by re-running the §5 audit. Don't cite these numbers as current)*:
```
 30 packs, ALL legacy-only (generic mode runs run_packs() NONE of them)
   rung:    26/30 fingerprint AI output (2b/1) · 1 parsed-fact (2a) · 0 reach rung 3
   verdict: 11 RETIRE (Class-A→contract synthesis) · 8 MOVE-DOWN (2b→3) · ~6 KEEP · ~6 HACK-CEILING · 4 QUESTION
 7 generic lowerings: 3 durable 2a (find_player_controllers, resolve_requires, asset rewriter,
   contract player-facet) · 4 still 2b (camera locator, movement WASD-locator, trigger-stay comment,
   child-index) · bullet = HACK-CEILING + absent from generic mode (coverage GAP)
```
Biggest structural finding (all four voices): **one missing IR relation, exploded into ~9 packs** — the
Pickup→Door→Machine→Player flag/event web is one `shared-flag-contract` + `event-channel` reconstructed
nine ways. The four door-flag packs are the same fix matching four AI shapes; the turret trigger-stay
v1+v2 split is the same fix that broke when the AI output drifted. **Highest-leverage consolidation:**
promote the `shared-flag-contract` / `event-channel` / `controller-role` / `callback-kind` stamps into
the planner/contract — "most of the Door/Pickup/Machine/Player pack zoo, the legacy FPS look packs, and
the producer/consumer shims disappear."

Two latent bugs the audit surfaced (both consumed by the player fix): `SceneCameraInput:onRespawn` and
`:getLookCFrame` are **never called today** (yaw never resyncs after respawn).

---

## 6. Document map

| Concern | Owning doc | Status |
|---------|-----------|--------|
| Generic-runtime MECHANISM (6-piece contract, modes, prompt, verifier rules) | `scene-runtime-contract.md` | authoritative (mechanism layer) |
| Deployment-topology authority (domain/class/container/cross-domain edges) + the contract-verifier hook/boundary (the verifier CHECK SPECS live in the execution-log Phase 3 section) | `scene-runtime-architecture-ir.md` | **mostly shipped** (Phases 1/2a, 2b-core, Phase 3 verifier on upstream/main as of 2026-06-03); Phase-2b legacy-pack-retirement tail pending. Topology only. |
| Phase ledger / per-slice status / testing / sequencing / revision history (Workstream 2 execution) | `scene-runtime-execution-log.md` | living status log (companion to the IR doc) |
| Gameplay-IR relation **enforcement** (per-relation fact → output shape → mode: pre-rewrite / token / verifier) | `gameplay-ir-obligation-schema.md` | design-of-record (companion to §3 catalog) |
| Domain classifier signal taxonomy | `scene-runtime-domain-signals.md` | in-flight |
| Camera + embodiment (player↔character) | `camera-input-fidelity-plan.md` | mostly shipped (worked example for #3/#4) |
| `self.host.player` embodiment authority (next runtime primitive) | `player-embodiment-authority-design.md` | converged, not built |
| Inline-vs-runtime-wrapper principle | `inline-over-runtime-wrappers.md` | durable principle |
| AI-hostile-file split (code org) | `../architecture_critique.md` + `../refactor_plan.md` (both in `docs/`, one level up) + `scene-runtime-and-refactor-execution.md` | orthogonal track |
| Superseded | `phase3-contract-verifier-brief.md` (→ execution-log §Phase 3), `merge-plan-phase-3-augmented.md` (different old "Phase 3", closed) | historical |

> §1 rung model, §3 catalog, §4 Class-A/B, §5 audit are net-new synthesis authored here. This doc is the
> umbrella; it links out and does not restate the detail docs (Codex: don't let it become an omnibus).

---

## 7. How these docs drive implementation + suggested order

**Per-work-item workflow:** (1) start HERE — identify the relation (#1–#9; #9 is candidate), workstream, and rung, and
confirm the guardrail (*the fix must not add a rung-2b matcher for a rung-3 subsystem*); (2) go to the
detail doc this points you to (Workstream-1 host primitive → `scene-runtime-contract.md` + the piece's
design e.g. `player-embodiment-authority-design.md`; Workstream-2 contract synthesis →
`scene-runtime-architecture-ir.md`; domain → `scene-runtime-domain-signals.md`); (3) build per that doc's
slices, verify on the cold-e2e oracle + shape-variance corpus; (4) record status in the PR / `scene-runtime-execution-log.md`,
and if a NEW relation is admitted, add it to §3 per the admission criteria. You implement *from* the
detail doc, never from this umbrella.

**Next-steps backlog (suggested order — NOT locked; the §2 build-order fork is the owner's).** Each item
names the relation(s), **build from** (the detail doc you implement against), and **track in** (where its
live status lives — do NOT mark status here; the checkbox is only a coarse cue, the authority is the
linked surface). Re-derive "done" from git / the execution-log, not from this list.

- [ ] **Step 0 — NET FIRST.** Gate 0 frame-ordering spike + shape-variance corpus + cold-(uncached)-e2e in CI.
      The historically-missing net; nothing below is durably verifiable without it.
      *Build from:* `player-embodiment-authority-design.md` §Gate 0 + followups #3. *Track in:* CI config + the run's followups.
- [ ] **Step 1 — Player-embodiment authority** (Workstream 1, #3+#4). Scaffolded; highest-regression surface;
      wires the 2 dead functions; retires the worst 2b locators. (Codex: build first.)
      *Build from:* `player-embodiment-authority-design.md` (+ `scene-runtime-contract.md` for the host surface). *Track in:* its own PR(s).
- [ ] **Step 2 — Cheap 2b→upstream moves** (#1 callback-kind, #2 child/path-ref). Re-key `trigger_stay` on a
      parsed `OnTriggerStay` fact; `child_index` on a parsed `GetChild(n)` fact. Proves the promote-a-stamp pattern.
      *Build from:* `scene-runtime-architecture-ir.md`. *Track in:* `scene-runtime-execution-log.md`.
- [ ] **Step 3 — Finish + generalize Workstream 2** (#5 shared-flag, #6 event-channel). Machinery shipped;
      remaining: (a) retire legacy `pickup_remote_event_server` + dependents (Phase-2b tail); (b) generalize
      the bridges to first-class #5/#6 relations. Retires the ~9-pack Pickup/Door/Machine zoo.
      *Build from:* `scene-runtime-architecture-ir.md`. *Track in:* `scene-runtime-execution-log.md`.
- [ ] **Step 4 — Projectile primitive (#8)** (needs its own design — the bullet follow-up) **+ locking gate (#4)**:
      a conformance check that fails CI when a new 2b matcher is added for a rung-3 subsystem — locks the gains.
      *Build from:* a new projectile design doc (TBD) + a `check_no_any.sh`-style gate. *Track in:* its own PR(s).

Rationale: net-first (so fixes are verifiable — the historical gap), then the scaffolded high-value player
fix, then cheap pattern-proving moves, then the big Class-A synthesis, then close the door. Alternative
fork: lead with Step 3 if killing the most packs first matters more than the player fix. **Status of record
is the PR / `scene-runtime-execution-log.md`, not these checkboxes — re-derive "done" from there.**
