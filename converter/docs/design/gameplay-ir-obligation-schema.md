# Gameplay-IR obligation schema — how each relation is enforced

**Status:** design-of-record for *how* the gameplay-IR relations are enforced in generic mode · authored
2026-06-11. The companion to the relation **catalog** in
[`generic-converter-architecture.md`](generic-converter-architecture.md) §3: that doc owns *why each relation
exists and which rung it belongs at*; this doc owns *the contract that makes each relation durable* — the exact
fact, the output shape that satisfies it, and the enforcement mode.
**Authority:** subordinate to `generic-converter-architecture.md` (the umbrella) and `scene-runtime-contract.md`
(the runtime mechanism). Does not restate the rung/fault-line model — read the umbrella §1 first.
**Empirical companion:** [`scene-runtime-pr5-8-recut-plan.md`](scene-runtime-pr5-8-recut-plan.md) is the
*symptom catalog*. NB on state: on `main` it carries the 2026-06-04 scope (Slices T + H only); the SimpleFPS
canary slices **T-bullet / R / D and the 2026-06-11 sharpening of T/H** live in **PR #188 (open, unmerged)** and
land in the recut-plan when it merges. That doc records *what breaks*; this doc records *the durable contract that
fixes the class*. They are complements — neither supersedes the other (§8 maps each slice to its relation).
**Owner:** unity2rbxlx converter team.

**Status discipline:** this doc owns durable *intent*, not implementation status. It cites code by **symbol name**
(never line numbers — they rot). To answer "is relation X enforced yet," read the named symbol / git, not this doc.

---

## 1. The problem this schema solves

Generic mode transpiles C# → Luau with a **non-deterministic AI**, then (historically) repaired the output with
coherence packs that fingerprint the AI's text and **silently abstain** on a valid-but-different shape (the umbrella's
rung-2b fragility). The durable alternative is to feed the AI the deterministic facts it needs and make compliance
either *impossible to get wrong* or *loud when wrong* — never silently repaired.

Codex review (2026-06-11) named the gap that lets this drift back into hints: **the design must specify, per
relation, (a) the fact, (b) the exact output token/shape that satisfies it, (c) the enforcement mode, (d) how a
violation fails.** This doc is that specification.

## 2. The three enforcement modes (none is a coherence pack)

A coherence pack is bad because it **matches an output shape and repairs it, abstaining silently** when the shape
differs. None of the modes below do that — they key on the deterministic IR and fail visibly.

- **PRE-REWRITE** — the converter resolves the fact and substitutes it into the **C# input** (or emits the resolved
  Luau directly) *before* the AI transpiles. The AI never makes the decision, so there is nothing to verify. The
  ambiguous form never reaches the AI. *Use for local, resolved-reference facts.*
- **LOAD-BEARING TOKEN** — the AI emits a token the deterministic layer expands, where the token is the **only**
  route to the runtime primitive (executable + exclusive). Non-compliance **fails loud** (unresolved symbol /
  `luau-analyze` / runtime error). Precedent: the `@scene_runtime/<stem>` require contract (the resolver fails closed
  on a missing/colliding stem). *Use for facts that map to an exclusive host/runtime primitive.*
- **FAIL-CLOSED VERIFIER** — the AI emits, and a check tests the output **against the IR fact** (never against an
  output *shape*) and **errors** if unsatisfied. Required where a wrong output is still *runnable* (behavioral) or the
  relation is *bilateral* (one side can comply while the other doesn't). A missed case is a gap, never a silent
  regression. This is the opposite of a pack: *loud-check-against-the-fact*, not *silent-match-and-repair*. Lives in
  `contract_verifier.py` (already fail-closed in generic for checks A/B/C).

> **Boundary rule:** reference / identity / placement facts → PRE-REWRITE or TOKEN (no verifier). behavioral /
> bilateral facts → VERIFIER mandatory. A verifier implemented by matching an output *shape* (rather than checking
> against the IR fact) is a coherence pack in disguise — forbidden.

## 3. The obligation table (relations #1–#9)

Relation ids #1–#8 match `generic-converter-architecture.md` §3. The table below is the **admitted/enforced**
set; **candidate** relations that do not yet meet the umbrella's admission criteria (exactly one upstream signal,
one retired mechanism, one real check that asserts the IR fact) are listed separately in §3a — they are NOT yet
enforcement-of-record.

| # | Relation | Deterministic fact (IR) | Output shape that satisfies it | Enforcement | How a violation fails | Retires |
|---|----------|-------------------------|--------------------------------|-------------|-----------------------|---------|
| 2 | child/path-ref (mis-shaped) | `transform.GetChild(0)` on Turret = child named `Base`, resolved against the parsed prefab hierarchy | a **named** lookup (`:FindFirstChild("Base")`); **no surviving ordinal** `GetChildren()[n]` | **PRE-REWRITE** the C# `GetChild(n)`→`Find("<name>")` before transpile; backstop verifier flags any surviving `GetChildren()[<int>]` | construction-impossible (ordinal never reaches the AI) + loud backstop | `child_index_lowering`, `unity_transform_child_index`, `turret_canonical_spatial_child` |
| 1 | callback-kind | C# method `OnTriggerStay(Collider)` is a STAY callback (vs Enter/Exit) | `self.host:connectGameObjectSignalStay(<go>, fn)` — the exclusive stay primitive, distinct from the edge `connectGameObjectSignal(.., "Touched", ..)` | **INJECT + FAIL-CLOSED VERIFIER**: IR marks method M as OnTriggerStay; verifier checks M's binding uses the Stay primitive | the edge form is valid + runnable but **silently wrong** (fires on enter, not every frame) → verifier errors | `trigger_stay_lowering` (comment-keyed), `trigger_stay_polling` v1/v2 |
| 3 | controller-role | `role(PlayerScript)=PLAYER_CONTROLLER` from `_HasCharacterController` (parsed Unity fact) | controller wired onto the exclusive host primitive (`self.host.player` look/move/aim) | **LOAD-BEARING TOKEN** (exclusive host wiring — the only route to the primitive) | loud (no other path to the primitive) | camera/movement facet *locators*, `fps_default_controls_off` |
| 4 | embodiment | `PLAYER_CONTROLLER → LocalPlayer.Character` (derived from #3) | `self.host.player` owns camera/move/aim/respawn; rig shadow-synced | **LOAD-BEARING TOKEN** (host owns; rung-3) | loud | camera/movement facet *body rewrites* |
| 7 | domain/authority | `domain(Player)=CLIENT`, `authority(respawn)=SERVER` (topology / planner) | script placed in the domain-correct container + correct script class | **DETERMINISTIC OWNERSHIP** (topology decides placement) + existing fail-closed verifier check A (`consumer_compliance`) | already loud (check A fail-closed in generic) | `player_damage_remote_event`, `localscript_api_shim` |
| 5 | shared-flag contract | `flag("hasKey", writer=Pickup, readers=[Door], store=Player)` from whole-corpus read/write sites + one canonical store | writer emits `SetAttribute("hasKey", ..)` on the canonical store; each reader emits `GetAttribute("hasKey")` on the **same** store | **INJECT (both ends) + FAIL-CLOSED VERIFIER** | **bilateral**: writer can comply while a reader uses the wrong store/name → silently dead → verifier checks both ends present + same store/name | the 4 door-flag packs, `machine_item_check`, `door_player_flag_location` |
| 6 | event-channel | `event("ToggleDoor", producer=Machine, consumers=[Door])` from the Unity `event +=` / `SendMessage` topology | producer fires a named channel; each consumer subscribes to the **same** channel | **INJECT (both ends) + FAIL-CLOSED VERIFIER** | **bilateral** (same failure mode as #5) → verifier checks producer + every consumer wired to the channel | `door_machine_signal_listener`, `producer_consumer_bindable_events`, the 3 pickup-event packs |
| 8 | projectile/physics | `projectile(TurretBullet, launch=AddRelativeForce)` — prefab Instantiated + Rigidbody-launched | route through a host projectile primitive (stud velocity + anti-gravity + raycast) | **LOAD-BEARING TOKEN** (host primitive) — **needs its own design first** | loud once routed through the primitive; until designed, a coverage GAP | `bullet_physics_raycast` (absent from generic) |

## 3a. Candidate relations (NOT yet admitted/enforced)

These were surfaced by the 2026-06-11 playtest (PR #188) but do **not** yet meet the admission criteria — each
needs one unambiguous upstream signal, one retired mechanism, and a real check that asserts the IR fact (not a
narrative backstop). Tracked here so they aren't mistaken for enforcement-of-record.

- **#2-dropped — dropped child/path-ref** (rifle `weaponSlot = cam.GetChild(0)` omitted entirely). DIFFERENT
  mechanism from #2-mis-shaped: a source pre-rewrite can't fix a line the AI drops. Proposed: a deterministic
  rebind-by-name + a verifier that the IR-declared binding is present — but the rule (which refs get a rebind,
  what declarative binding record the verifier asserts against) is **unresolved** (§7.2). Not fail-loud until that
  exists. Maps to #188 Slice R.
- **#9 — dynamic-component-ref → driver-domain** (door dynamic `GetComponent<Animator>()` → unresolved →
  server-placed → client `open` write doesn't replicate). Does **not** meet "one signal" (composite: dynamic
  GetComponent + C# param-writes + clip `observed_attribute`) and the existing cross-domain verifier only checks
  placement coherence, **not** that the driver was matched to the right writer (that check is unbuilt). Door-scoped
  (§7.4). Maps to #188 Slice D. Admit only after the match-check exists and the signal is reduced to one.

## 4. The boundary it draws

- **No verifier (durable by construction):** #2-mis-shaped (pre-rewritten), #3, #4, #7, #8-once-designed. The fact is
  either resolved before the AI or expanded through an exclusive primitive.
- **Verifier mandatory (irreducible):** #1 (behavioral — a wrong output still runs), #5 + #6 (bilateral — one end can
  silently diverge). This is the floor: contract synthesis cannot make these construction-safe, so the fail-closed
  verifier is the durable home.
- **Candidate (not yet placed on the boundary):** #2-dropped and #9 (§3a) — their enforcement is unresolved, so they
  are not yet claimed as either construction-safe or verifier-covered.

## 5. The IR is one per-script "obligations blob", not a new compiler IR

"Build an IR" here means: **normalize the already-derived parse / topology / analyzer facts into one per-script
obligations record, threaded into the prompt** — not invent a semantic compiler IR (the umbrella explicitly fences
that off). The mechanism precedent already exists: `code_transpiler._build_serialized_field_context` injects
per-script facts into the prompt today. The missing wiring:

1. `state.parsed_scene` / `state.prefab_library` are **not threaded into** `transpile_scripts()` (available at the
   call site, just not passed) — required for #2 pre-rewrite and any hierarchy-derived fact.
2. No per-script builder emits the resolved child-refs (#2) / callback-kind (#1) / contracts (#5/#6).
3. Confirm the **single producer** of the obligations blob (planner vs a new normalizer over planner + parsed_scene
   + analyzer) so facts don't re-scatter.

## 6. Build sequencing (conditional — see the run's design memo / umbrella §7)

- **Net first** — shape-variance corpus + cold/uncached generic e2e in CI + the fail-closed verifier surface. Without
  it, IR-aided output is not *trustable* and the verifier modes above have no teeth.
- **Turret (#2 + #1)** proves the two non-trivial modes on one subsystem: #2 PRE-REWRITE (kill the ordinal at the
  source); #1 replace the comment hook with INJECT + a fail-closed verifier check.
- **Contracts (#5 + #6)** extend the verifier-mandatory mode to retire the Pickup/Door/Machine pack zoo; the
  synthesis machinery (`shared_flag_channels`, `cross_domain_edges`, `edge_enrichment`) already ships.
- **Locking gate** — a conformance check that fails CI when a load-bearing decision is keyed on AI-output shape
  without a fail-closed verifier (prevents backsliding to rung-2b).

## 7. Open items to resolve before implementing

1. **#8 projectile needs its own design** — projectile-vs-recoil is indistinguishable at the output level; out of
   scope here.
2. **#2 pre-rewrite edge cases** — name collisions, unnamed children, `GetChild(n)` indexing past the resolved set:
   define the deterministic fallback, and ensure the backstop verifier catches any surviving ordinal. Also define
   the *dropped-ref* rebind: which references get a deterministic by-name rebind (and the binding-present check).
3. **Single producer of the obligations blob** (§5.3) — pick it before threading facts, or the scatter returns.
4. **#9 is door-scoped, NOT a universal animation cure** (Codex, 2026-06-11) — the other unresolved animation
   drivers fail for *different* reasons (`HostilePlane:Flying` is an autoplay loop with `observed_attribute=""` → no
   parameter to narrow on; the `PlaneHolder` entries have a cross-prefab REF gap). Each is its own slice; do not
   over-generalize the parameter-write resolution.

## 8. Mapping to the recut-plan / PR #188 canary slices

The PR #188 (open) playtest symptoms map to a relation + disposition here. This is the cross-link (neither doc
supersedes the other); slices R/D/T-bullet are not yet in the on-`main` recut-plan (see the header state note):

| #188 slice | Symptom | Relation / disposition |
|---|---|---|
| **T** turret | `GetChild(0)`→Sound; lowering abstains on the factored shape | **#2 mis-shaped** (admitted/enforced) → PRE-REWRITE (NOT "add another output-shape matcher" — that re-creates the rung-2b fragility) |
| **R** rifle | `weaponSlot = cam.GetChild(0)` dropped → rifle on far body | **#2-dropped — CANDIDATE (§3a)**, different mechanism from T; rebind rule unresolved (§7.2) |
| **D** door | dynamic `GetComponent<Animator>()` unresolved → server-placed → client write dead | **#9 — CANDIDATE (§3a)**, door-scoped (§7.4); admit only after the driver-match check exists |
| **T-bullet** | runtime clone `parent=nil` never enters DataModel | **NOT an IR relation** — a codegen default bug in the `instantiatePrefab` clone service (`autogen.py`: default nil→`workspace`). Track in the recut-plan, not here. Required (with T) for turrets to *shoot* |
| **H** HudControl | "HUD dead" in playtest | **Domain classification is FIXED** (#7 durable: `domain=client`) per #188's own run ledger — which marks Slice H done and the recut-plan's `domain="excluded"` Slice H text **stale**. Any runtime-dead HUD is then a **host-runtime binding** issue (deferred UI-host / `self.gameObject`→ScreenGui) or the pre-existing **Pause-panel** gap — NOT a classifier/IR gap. Out of this schema |
