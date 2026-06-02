# Phase 3 — Slice 2 handoff (check B: component availability)

**Branch:** `phase3-slice-2-check-b` (stacked on `phase3-slice-1-check-a` / PR
#168, which is stacked on #167). **Status:** shipped to PR. Shadow warnings only.

## What shipped

- **`converter/contract_verifier.py`** — `_check_component_availability` +
  `_runtime_class_map` (parses `_UNITY_TO_ROBLOX_CLASS` from
  `runtime/scene_runtime.luau`) + `_GETCOMPONENT_RE` + `_ROBLOX_CLASS_ALLOWLIST`
  + `_strip_luau_comments`, wired into `verify_contract` after check A.
- **`converter/tests/test_contract_verifier.py`** — `TestRuntimeClassMap`
  (exhaustive key + value guards) + `TestCheckBComponentAvailability`.
- **`docs/design/scene-runtime-architecture-ir.md`** — §Phase 3 check #3 +
  slice-2 line rewritten to reachability-only.

## What check B does

Flags `self:GetComponent("X")` string-literal sites where X resolves to nil at
runtime. X is REACHABLE iff: peer module (`stem` ∪ `script_id` — exactly the
runtime lookup `m.stem == name or m.scriptId == name`, scene_runtime.luau:758) OR
a `_UNITY_TO_ROBLOX_CLASS` key OR one of its values OR in the explicit
`_ROBLOX_CLASS_ALLOWLIST`. Trusts the RUNTIME map (parsed, single source of
truth; CharacterController→BasePart, NOT Python TYPE_MAP's →Humanoid), guarded by
an exhaustive key+value test.

## Locked decisions

- **Reachability only; method-validity DEFERRED** (no Roblox class→method DB in
  repo; CharacterController.Move already bridge-routed → the anecdote is mostly
  handled). Documented gap.
- **Literal args only** — non-literal (`GetComponent(var)`) skipped → a
  fail-closed flip covers literal sites only (documented coverage hole).
- **Allowlist biased to abstain** (over-broad only fails open). It is
  load-bearing, NOT redundant with the map values: real transpiled Luau contains
  `GetComponent("Humanoid")` and Humanoid is NOT a map value → would
  false-positive without the allowlist.
- Regex matches ONLY `:GetComponent("X")` — not plural `GetComponents`, not
  `GetComponentInChildren/InParent` (transpiler lowers those to a hierarchy
  walk, code_transpiler.py:1330 — different resolution model).

## Review (R1 applied)

Claude code review found **P1**: peer set used `class_name` (dead — not in
`TopologyModuleEntry`; runtime uses `stem`/`scriptId`). Fixed to `stem ∪
script_id`. P2a: strip Luau comments (commented-out GetComponent was
false-positiving). P2b: exhaustive value guard. P3: dropped the In*/Parent
regex alternation. All applied + tested. `codex review` attempted but blocked by
a sustained codex network outage all session; Claude review (verified against
upstream code) stands in. Full fast suite: 2413 passed; no-Any clean.

## Real-output validation

Real transpiled Luau across worktrees: all real `:GetComponent(...)` forms match
the regex; Unity-type args (AudioSource, Rigidbody, colliders, Light, Text,
CharacterController) resolve via map keys; `Humanoid` via allowlist (proving it
load-bearing); game-specific classes via peer stems.

## Carry-forward

- **MeshContainer** (from slice 1) still pending slice-4 audit.
- Slice 3 (check C) reuses `verify_contract`/`ContractViolation`/`stash`
  unchanged. It will add the reader-store scan (shared_flag_channels currently
  records the WRITER funnel dests, not the reader's own store).
- Before the slice-4 fail-closed flip of check B: confirm the literal-only
  coverage + the allowlist breadth on the full corpus (AI transpile).
