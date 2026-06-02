# Phase 3 — Slice 3 handoff (check C: cross-domain attribute, Class-1 only)

**Branch:** `phase3-slice-3-check-c` (stacked on `phase3-slice-2-check-b` / PR
#169 → #168 → #167). **Status:** shipped to PR. Shadow warnings only.

## What shipped — Class-1 ONLY

`_check_cross_domain_attribute` flags a cross-domain literal `SetAttribute("X")`
writer + `GetAttribute[ChangedSignal]("X")` reader pair (writer & reader in
DIFFERENT topology domains, same field X) that has no `cross_domain_edges` entry
with `resolution.strategy == "remote_event_bridge"` matching BOTH script-identity
endpoints (`from_script`==writer sid AND `to_script`==reader sid AND field==X).
Field-only/domain-only matching is unsafe (field names collide). Site domain
comes from the topology `modules[sid]["domain"]` verdict, NOT
`RbxScript.script_type` (coherence flips Script↔LocalScript). Unjoinable /
colliding names abstain. `_name_to_script_id` reuses check A's `_join_name`.

## Class-2 DEFERRED (the headline door-bug — read before slice 4)

The dynamic shared-flag store-mismatch (server reads a client-written shared flag
from the wrong instance, e.g. HumanoidRootPart instead of Player/Character) is
**deferred as a known false-negative**, per the slice-3 arch review (Claude;
codex network-down all session). Three independently-verified reasons:

1. **PHANTOM on the corpus.** The verifier runs POST-coherence
   (`pipeline.py:4759`, after `_subphase_cohere_scripts` ~2656). The
   `door_player_flag_location` coherence pack (`script_coherence_packs.py:1368`)
   already rewrites the wrong-store read (`rootPart:GetAttribute("hasKey")`) to
   `player:GetAttribute(...)` BEFORE the verifier scans — so the bug no longer
   exists in the scanned scripts.
2. **Brittle regex-on-AI-output** (project rule forbids). The reader's own store
   is a Luau VARIABLE expression (`rootPart`, `model`, `target`) the topology
   does not record (GF7). Detecting the mismatch would mean guessing instance
   types from variable names.
3. **The `present==False` coverage alternative is VACUOUS:** `present =
   bool(read_names) or fail_open_present` (`shared_flag_channels.py:288`), so
   `read_names` non-empty ⟹ `present` True — the condition never fires.

**To do Class-2 right** (future slice): a PRE-coherence hook (scan
`transpilation_result.scripts` before packs rewrite), or detect the structural
gap the `door_player_flag_location` pack itself keys on, plus an adversarial
(codex) review. Do NOT ship a post-coherence store-token heuristic.

## Validation caveats (important for slice 4)

- **No AI-corpus validation.** An AI SimpleFPS conversion to populate real
  `cross_domain_edges` / `read_names` was attempted but is impractically slow
  (47 scripts × ~40s via claude_cli > the 25-min budget; killed mid-transpile).
  Class-1 is validated by SYNTHETIC fixtures only — SimpleFPS has 0 Class-1
  edges (its door is a Class-2 shared flag), so there is no real Class-1 target
  in the bundled corpus.
- **Codex was network-down the entire session** (sustained connection-reset /
  websocket failures) — slices 1/2/3 had Claude review only. Re-run codex review
  on #168/#169/#170 when it recovers, especially before any fail-closed flip.

## Reviews

Claude code review — CLEAN, no P1/P2; 3 P3s, 2 applied (reuse `_join_name`, drop
dead dedup set), 1 skipped (info-row for unknown-domain writer — minor shadow
asymmetry). Confirmed the Class-2 deferral claims against code. Full fast suite:
2421 passed; no-Any clean.

## Carry-forward to slice 4 (corpus shadow audit + per-check flip)

- **MeshContainer** (slice 1: helper emitted as auto-run Script) — audit.
- **Class-2 door-bug** — deferred FN; decide whether slice 4 (or a slice 3b)
  adds the pre-coherence Class-2 hook before flipping any check.
- The corpus harness needs AI transpilation to exercise B/C on real Luau, and AI
  conversion is slow — budget for it, or use a smaller AI-converted fixture.
- Per-check flip: check A + check B are the most corpus-clean candidates; check C
  Class-1 has no real corpus signal yet (synthetic only) — do NOT flip it to
  fail-closed until a project with real Class-1 edges validates it.
