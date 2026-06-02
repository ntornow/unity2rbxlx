# Phase 3 Slice 3 — check C (cross-domain attribute access): design brief

For arch review (Claude; codex network-down all session). Stacked on PR #169.
Shadow warnings only. This is the headline **door-bug** check — and the most
false-positive-prone, so scope discipline matters.

## The ONE goal

> **Check C: flag cross-domain attribute access that no bridge/funnel covers —
> the two bridge classes — including the canonical-store mismatch (a server
> reader reading a client-written shared flag from the WRONG instance, e.g. the
> HumanoidRootPart instead of the Player/Character the funnel writes).**

## The hard constraint (and why scope is narrow)

The "reader's own store" is a Luau VARIABLE EXPRESSION (`player`, `character`,
`hrp`, `model`, `target`, `col.Parent`) whose real Roblox instance type is NOT
statically knowable without dataflow analysis. The project rule (and the user's
stated preference) is **structural/canonical-contract fixes over brittle regex
on AI output**. So:

- **Class 1 (static component-ref edges) — STRUCTURAL, clean.** Reconcile literal
  `SetAttribute("X")`/`GetAttribute[ChangedSignal]("X")` pairs against the
  topology's structured `cross_domain_edges` (from_script/to_script/from_domain/
  to_domain/field/resolution.strategy). No fuzzy parsing.
- **Class 2 (dynamic shared-flag store mismatch) — UNAVOIDABLY heuristic** (the
  topology does NOT record the reader's store — GF7), so do the MINIMUM
  confident thing: flag ONLY when a reader of a name ∈ `read_names` reads from a
  store that is **confidently a HumanoidRootPart/BasePart** (the exact door-bug
  shape), NOT "anything that isn't player/character." Bias to ABSTAIN. Shadow
  only; corpus-validate before any fail-closed flip.

## Grounding facts (verify/refute)

- **GF-C1:** `SharedFlagChannel` = `{read_names, reader_domains,
  canonical_stores=["Character","Player"], present}`; funnel writer domain =
  `"client"` (shared_flag_channels.py:96). A read is cross-domain iff the
  reader's domain ≠ "client" (today always "server"). On the `--no-ai` corpus
  read_names is EMPTY + present=False (stubs have no real reads) → check C is a
  no-op there; an AI conversion is needed to populate it (running now).
- **GF-C2:** `CrossDomainEdge` = from_script/to_script/from_domain/to_domain/
  field/`resolution.strategy ∈ {remote_event_bridge, same_domain_no_bridge,
  excluded}`/payload.attribute_name. SimpleFPS has 0 Class-1 edges (its door is a
  Class-2 shared flag) → Class-1 validated by synthetic fixture only.
- **GF-C3 (real attribute forms, surveyed):** the dominant real shared-flag read
  is `player:GetAttribute("hasKey")` (Player → canonical → covered). No real
  `hrp:GetAttribute("hasKey")` in the surveyed corpus, so the narrow door-bug
  flag won't fire spuriously on it.
- **GF-C4:** check C reuses `verify_contract(topology, scripts)` + the
  script→domain join (module stem/script_id → RbxScript) check A/B established.
  Reader domain = the domain of the module whose RbxScript contains the read.

## Decisions (recommendations — autonomous run)

- **DQ1 Class-1 reconciliation:** scan literal `SetAttribute("X")` (writer) +
  `GetAttribute[ChangedSignal]("X")` (reader) sites, resolve each site's script
  → domain. For a writer/reader pair with DIFFERENT domains on the same field X,
  require a `cross_domain_edges` entry covering (writer_script, X, reader_script)
  with `strategy == "remote_event_bridge"`. Else → warning. Same-domain pairs →
  no check.
- **DQ2 Class-2 store-mismatch (narrow):** for a `GetAttribute[ChangedSignal]("X")`
  reader where X ∈ `read_names` AND reader domain ≠ "client" (funnel writer) AND
  the reader's store token is confidently HumanoidRootPart/Part-shaped
  (`hrp`, `humanoidrootpart`, `rootpart`, `:FindFirstChild("HumanoidRootPart")`,
  `.PrimaryPart`) → warning (canonical-store mismatch: funnel writes
  Player+Character, this reads a Part). Player/Character-ish stores → covered.
  Ambiguous → ABSTAIN (no flag).
- **DQ3 store-token extraction:** capture the receiver expression immediately
  left of `:GetAttribute` (a `[\w.]+` token or a `:FindFirstChild("...")` tail).
  Classify into canonical / part-shaped / unknown by a small explicit pattern
  set. Bias to abstain.
- **DQ4 severity:** all `warning` (shadow). The Class-2 store heuristic is the
  most FP-prone check in the whole verifier — the design doc + handoff MUST mark
  it "corpus-validate + (ideally) codex-review before the slice-4 flip."

## Open questions for the reviewer
- Is the narrow part-shaped store set right, or will it MISS real door bugs
  (false negatives) by being too narrow? (Prefer false-negative over
  false-positive in shadow — but name the tradeoff.)
- Class-1: how to join an emitted attribute site to its `script` identity +
  domain robustly (reuse check A's module_path-tail join)? Is matching the edge
  by (field name + both domains) sufficient, or must script identity match?
- Should Class-2 also flag a shared-flag read when `present == False` (no funnel
  at all but a cross-domain shared-flag read exists)? That's a structural
  coverage gap independent of the store — arguably the cleanest Class-2 signal.

## What the reviewer returns
Confirm/refute GF-C1..C4. Vote DQ1-DQ4. MOST IMPORTANT: pressure-test the
Class-2 store heuristic (DQ2/DQ3) for false positives on the real attribute
forms (GF-C3) AND for false negatives (door bugs it would miss). Flag whether
Class-1 reconciliation can be done structurally without fuzzy joins.
