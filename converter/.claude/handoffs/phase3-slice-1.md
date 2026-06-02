# Phase 3 — Slice 1 handoff (check A: consumer compliance)

**Branch:** `phase3-slice-1-check-a` (stacked on `phase3-slice-0-skeleton` / PR
#167). **Status:** shipped to PR. Shadow-mode warnings only.

## What shipped

- **`converter/contract_verifier.py`** — `_check_consumer_compliance` +
  `_domain_placement_violation` + `_container_family` + `_join_name`, wired into
  `verify_contract` after the smoke check.
- **`converter/tests/test_contract_verifier.py`** — `TestCheckAConsumerCompliance`
  (13 tests: violation cases, the full storage≠domain false-positive table,
  helper/excluded, unverifiable join, unknown domain).
- **`docs/design/scene-runtime-architecture-ir.md`** — §Phase 3 check #1 +
  slice-1 line rewritten to the domain⟂placement framing.

## Locked decision: it is NOT a placement==topology comparison

The artifact's `container`/`module_path` is MIRRORED from `RbxScript.parent_path`
(`module_domain.py:1666-1670`, set after `classify_storage`), so comparing them
is tautological. Check A reconciles the INDEPENDENT `domain` (source-derived;
CI-guarded never to read `parent_path`/`script_type`) against the emitted
(`script_type`, container-family of `parent_path`).

**Consistency table** (arch-reviewed vs the doc's §"storage ≠ domain" cases — no
false positives): server ⇒ not LocalScript & not in CLIENT_ONLY container;
client ⇒ not (auto-run Script in SERVER_ONLY container); helper ⇒ not auto-run
(container NOT checked — reachability hoist into a client container is legal);
excluded ⇒ flag if still emitted. Families: SERVER_ONLY={SSS, ServerStorage},
CLIENT_ONLY={StarterPlayerScripts, StarterCharacterScripts, ReplicatedFirst},
NEUTRAL={ReplicatedStorage}. Unverifiable joins (name collision / no match) →
`severity:"info"` row (no silent gap, no double-fail).

**Scope:** modules only. `animation_drivers` deferred — their domain↔script_class
is consistent by construction (build_topology invariant 4) and they use a
different (display-name) join, so there's nothing for check A to find there yet.

## Real-output validation (SimpleFPS, generic, --no-ai)

Check A ran on the real path (3 rows): 28 client ModuleScripts-in-ReplicatedStorage
correctly passed (0 false positives on the legal neutral-container cases); 2
helpers not emitted → `unverifiable` info rows; **1 real warning: `MeshContainer`
— a helper-domain module emitted as an auto-run `Script`** instead of a
require-only ModuleScript.

## Carry-forward

- **`MeshContainer` finding** is a candidate real bug for the slice-4 audit (or a
  standalone fix): a helper emitted as auto-run Script. Investigate whether it's
  a producer misclassification (storage classifier / intrinsic type) before the
  slice-4 fail-closed flip, or it will block the flip.
- **Known gap (out of scope):** a server-private module mis-placed in NEUTRAL
  ReplicatedStorage (a replication leak) is structurally undetectable — the
  artifact carries no "server-private" signal. Add such a signal in a future
  slice rather than implying coverage.
- Slice 2 (check B) + slice 3 (check C) reuse `verify_contract` /
  `ContractViolation` / `stash_violations` unchanged — just `violations.extend`
  with their own `check=`/`identity=` prefixes.

## Reviews

Claude code review — clean, no P1/P2; two P3 clarifying comments applied.
`codex review --base phase3-slice-0-skeleton` was attempted twice but **blocked by
a sustained codex network outage** (connection-reset / websocket failures); the
Claude review (verified against upstream code, not just the diff) + the
end-to-end real-output validation stand in as the gate. Full fast suite: 2398
passed; no-Any clean.
