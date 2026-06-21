# decisions.md audit — inconsistencies, conflicts & directionally-wrong architecture

Source: `.harness/decisions.md` (4,144 lines, ~13 `/drive` runs).
Method: two independent passes reconciled to consensus — four Claude readers over the full file
(disjoint line ranges) + a Codex (gpt-5.4, xhigh) pass over the whole doc. Items marked **[consensus]**
were independently surfaced by BOTH voices with matching exemplars; **[claude]** / **[codex]** = single-voice.

Line numbers are `decisions.md` line numbers as of this audit (the doc is append-only; ranges may drift).
This is a checklist for a LATER investigation — nothing here is yet acted on. Each box is one lead.

---

## Headline conclusion

The log is honest and high-discipline (reversals are flagged, dual-voice reviewed, live-verified). The
churn is not sloppiness — it traces to ONE structural failure mode that recurs across nearly every run:
**load-bearing behavior keeps being re-anchored onto non-deterministic AI (LLM) output, violating the
project's own rule "anchor on the deterministic upstream source, never a fingerprint of generated output."**
Gate accumulation, oscillation, and late shape-discovery are downstream symptoms of that one cause.

Two strongest leads for a deeper pass:
1. Audit every active post-transpile lowering/verifier — is its TRIGGER *and* its TARGET deterministic?
   Several have a deterministic trigger but a non-deterministic (AI-output) rewrite target.
2. The dead-module classifier now carries so many bespoke exemption carriers it may no longer be a
   trustworthy authority. Re-evaluate whether the seam itself is wrong.

---

## A. Inconsistencies & conflicts (mostly logged supersessions; direction reversals worth chasing)

- [ ] **A1 [consensus] Child-index pack: blessed then condemned.** Legacy regex/output-shape pack blessed
  as the generic fix (`45-51`), then declared "exactly the rung-2b fragility the enforcement contract
  forbids" and replaced by deterministic pre-rewrite + verifier (`395-398`). Direct architectural-contract
  reversal, not an impl tweak.

- [ ] **A2 [consensus] Rifle `weaponSlot` mechanism oscillated ~12×.** pre-rewrite/no-verifier (`585-600`)
  → post-transpile/no-verifier (`615-639`) → pre-AI token (`770-780`) → post-transpile + new verifier
  (`782-812`) → full re-anchor on consumer READ-reroute, write-shape anchor proven wrong (`1176-1236`).
  The §4 "reference facts get no verifier" boundary was cited to FORBID the verifier (v2-v4) and then
  re-read to REQUIRE it (PATH CORRECTION). ~7 rounds of S1/S1b hardening thrown away at re-anchor.

- [ ] **A3 [consensus] Gravity execution surface flipped 6×.** constructor loop (`1975-77`) → server-only
  sweep (`2003-07`) → standalone server script (`2016-23`) → dual-domain emit (`2028-37`) → revert to
  server-only (`2038-48`) → +client clone hook (`2050-60`). Oscillation around the true network-authority
  boundary.

- [ ] **A4 [consensus] Gravity fact-resolution reversed + Model-carrier build-then-delete.** owning-Model-first
  (`2124-32`) → root-first (`2136-51`); the first revision was itself a silent fail-open to default gravity.
  Model carriers "IS corrected" (D-P1.11, `2168-92`) → re-scoped to "skip, effectively static" (`2217-34`).
  A full design round built then removed.

- [ ] **A5 [consensus] Check D "fail on ANY surviving ordinal" rolled back.** `415-417` → "assert only
  against a resolved fact, abstain otherwise" once the real chained turret + Player cam ref were met
  (`520-558`). Gate breadth turned a coverage gap into noise.

- [ ] **A6 [consensus] Roster surface flipped 4×.** tag+folder (`3146-49`) → tag-primary/folder-optional
  (`3174-77`) → no-folder-by-default (`3221-23`) → mandatory dedicated collision-checked folder (`3269-72`).
  Real discovery/naming contract change.

- [ ] **A7 [consensus] Checkmark-Toggle premise wrong.** Dispatched on `ct=="Toggle"` (`2872-79`) — dead on
  real scenes; Toggles serialize as `MonoBehaviour + m_IsOn` (`2917-23`). Faithful conversion also showed
  the "no reader exists" bug premise was off — AI already emitted a name-based reveal path; the real value
  was initial-hide + genericity (`2927-37`).

- [ ] **A8 [consensus] Stated-fact corrections.** `_tick` `:2656`→`:2792` (`292-95`); "serialized_field_extractor
  strips `m_` at :73-74" asserted then refuted FALSE (`3250-51`); mesh "no scene_converter change"
  asserted→refuted→re-confirmed (`3615-39`).

- [ ] **A9 [consensus] Audit-trail noise.** Duplicated run headings (`1879-83`, `2912-16`); reused D-numbers
  within one run (door-binding D2–D5 twice, `3443-65` vs `3467-98`); site-count drift (long-bracket
  "two→three" `4016`; float "~4→~15-20" `4034`; L1 spawn "five→six→four-of-five" `3833`/`3864`).

- [ ] **A10 [claude] gap4 D0 bans the mechanism shipped 3 runs earlier.** D0 forbids
  `spawn_call_site_lowering` / post-transpile AI-output rewrites (`4131`) — the exact mechanism built and
  shipped at D-P4-3 (`3806`). Architecture both adopts and forbids the approach.

- [ ] **A11 [claude] Slice F∥T parallelism took 3 positions.** F→T sequential → F∥T (`455-64`) → atomic FT
  (`506-18`). Self-corrected but signals the produced-then-consumed contract should never have been split.

---

## B. Directionally-wrong architecture

- [ ] **B1 [consensus] Legacy regex pack on the generic child-index path** (`45-51`). Anchors correctness on
  downstream Luau shape; project later admits it's forbidden fragility (`395-398`).

- [ ] **B2 [consensus] Rifle post-transpile AI-shape rewriting + verifier backstops** (`615-639`, `782-812`).
  Proven wrong by the log: 5 valid AI Player shapes incl. `self.weaponSlot = self.gameObject` → shape-anchored
  lowering silently abstained (`1186-1213`). Textbook rule-#3 violation.

- [ ] **B3 [consensus] "Fail on any surviving ordinal" gate** (`415-417`). Incomplete resolver coverage
  turned into a broad fail-closed gate, masking the gap as noise until rolled back (`520-558`).

- [ ] **B4 [consensus] `rig_binding` + check-D dead-write exemption stack** (`802-838`, `1485-1659`). A large
  compensating system — 5-key carrier threaded through ~6 sites, two parallel Luau text-scanners
  intentionally allowed to desync, a forge-able trust boundary closed only by a comment — built to stop one
  gate misfiring on another gate's correct output. Effort spent proving the gate isn't lying ≈ proof the
  seam is wrong.

- [ ] **B5 [consensus] Dual-domain gravity correction** (`2032-37`). Split authority across server+client
  with replicated tags + timing → immediate double-apply/race hazards → forced revert (`2038-48`). Fails by
  interaction, not by a single clear contract.

- [ ] **B6 [claude] Hand-rolled Luau parsing via regex + "code projection"** (`1157`, `1344`, `1424`, `1643`).
  Each round found another lexical form missed (`\r\n`, `(self)[k]`, `[[…]]` keys, encoded string keys). A
  real Luau parser closes the whole class once.

- [ ] **B7 [claude] `_PLAN_KEYS_FOR_HOST` allowlist as a silent choke point** (`3119-37`). Every plan-carried
  feature (gravity, addressables, db-seeds) must remember to add its key or ship a silently-dead feature on a
  fresh convert. Central contract with a silent-failure mode.

- [ ] **B8 [claude] Token-pin-only verification for force behavior** (`2239-52`). CI never checks actual fall
  rate, only emitted tokens. Green-for-wrong-reason on the load-bearing behavior; behavioral check is
  Studio-only (CI blind spot, accepted by design).

- [ ] **B9 [claude] Mesh-wrap attribute move-list is a hardcoded literal tuple** (`2311-29`,
  scene_converter.py:2177-2182). Every new attribute that must survive mesh-wrapping needs a manual edit;
  a forgotten entry silently misclassifies a body (the S2 wrapped-2D bug that triggered this).

- [ ] **B10 [claude] Generic mode skips coherence packs — features rediscover this the hard way**
  (`2972-84`, `3038-41`, `4131`). Two divergent post-processing paths where a whole class of fixes (packs)
  silently no-ops in the generic path. Footgun for any future fix reaching for a pack.

- [ ] **B11 [claude] `DEP_MAX_ITERS=200000` runtime cap chosen to satisfy a mock-clock test** (`160-63`).
  Test-double limitation leaking into production runtime semantics.

- [ ] **B12 [claude] Userdata-vs-table dispatch bug class recurs** (`4124`, scene_runtime.luau ~2643). Real
  Instances are `userdata`, not `table`; table-mock unit tests pass while production silently no-ops. A whole
  category of runtime no-ops the Luau test suite structurally can't catch.

---

## C. Systemic themes (highest-order — both voices, verbatim-aligned)

- [ ] **C1 Re-anchoring on AI output after explicitly forbidding it.** THE root cause. Child-index pack,
  rifle lowering, spawn-call-site rewrites (`3806-13`, `3828-49`). Decisions keyed on deterministic upstream
  facts stabilize; decisions keyed on AI-output shape oscillate.

- [ ] **C2 Gates/verifiers/exemption-carriers accumulate faster than canonical contracts.** check D (`433-39`),
  `rig_binding_present` (`802-838`), 5-key carrier + dead-write exemption (`1485-1566`), dead-module carriers
  (`3316-18`), SO-DB fail-closed target (`3701-05`). Each new lowering needs its own "don't-stub-this" carrier
  → steadily weakens the classifier as an authority.

- [ ] **C3 Real-shape enumeration happens too late.** Designs built against an assumed canonical shape, meet
  the real emitted/runtime shape mid-implement: chained turret GetChild (`520-558`), live Player cache
  (`1186-1201`), Model/2D gravity carriers (`2168-234`, `2272-338`), Toggle serialization (`2917-23`), mesh
  binding gate (`3621-37`). Fix: enumerate the real input space BEFORE design review (project's own rule).

- [ ] **C4 Oscillation marks unsettled contract boundaries.** Rifle (`585-790`, `1176-236`), gravity
  (`1975-2060`), roster (`3146-291`). Not random churn — the true authority boundary was never nailed early.

- [ ] **C5 Persistence/resume/allowlist seams drift when they store CONCLUSIONS instead of recomputing FACTS.**
  Addressables essential-recompute (`2669-78`), `rig_binding` carrier widening (`1485-509`, `1838-45`), gravity
  early-stash (`2092-104`), db-seed recompute + allowlist (`3099-137`). Rule of thumb the log keeps
  rediscovering: persist facts, recompute conclusions.

---

## Suggested next steps (not yet done)
1. **AI-output-anchoring audit (C1/B1/B2).** Enumerate every active post-transpile lowering + verifier; for
   each, classify TRIGGER and TARGET as deterministic vs AI-output. Flag any with a non-deterministic target.
2. **Classifier-authority review (C2/B4).** Inventory every dead-module/dead-write exemption carrier; assess
   whether the classifier seam should be redesigned rather than further exempted.
3. **Shape-enumeration-first checklist (C3).** Add a design-gate item: capture the real emitted/runtime shape
   corpus before locking any lowering abstraction.
