# Phase 3 Slice 2 — check B (component availability): design brief

For parallel arch review (Claude + codex, codex may be network-down). Stacked on
PR #168 (slice 1). Shadow-mode warnings only.

## The ONE goal

> **Check B: flag every emitted `GetComponent("X")` whose `X` resolves to NOTHING
> at runtime — i.e. X is not a converted peer component, not a mapped Unity type,
> and not a Roblox class — because the call returns nil and any subsequent
> use/method-call errors.** (The "unmapped component" bug class.)

## Verified facts (confirm/refute)

- **GF-B1:** Generic mode emits the PEER form `self:GetComponent("X")`
  (code_transpiler.py:1329), NOT `:FindFirstChildWhichIsA("T")` (that's the
  legacy-mode guidance at :843). So check B scans `:GetComponent("X")` literal
  sites, not FindFirstChildWhichIsA.
- **GF-B2:** Runtime resolution (scene_runtime.luau:752-780):
  `GetComponent(name)` → (1) peer converted-MonoBehaviour by stem/scriptId; else
  (2) `mapped = _UNITY_TO_ROBLOX_CLASS[name]`; if `mapped == _CLASS_TRANSFORM_SELF`
  → the GameObject root; else `findFirstChildWhichIsA(mapped or name)`. An
  unknown `name` (not peer, not in the map, not a real Roblox class) →
  `findFirstChildWhichIsA(name)` returns **nil** → subsequent use errors.
- **GF-B3:** `_UNITY_TO_ROBLOX_CLASS` is defined in **Luau**
  (runtime/scene_runtime.luau:71-105) — NOT in Python. CharacterController →
  "BasePart" there (vs Python TYPE_MAP → "Humanoid"). Per the locked decision,
  check B trusts the RUNTIME map → the verifier must READ it from the Luau file
  (single source of truth), not duplicate it in Python.
- **GF-B4:** There is **NO Roblox class→method database** anywhere in the repo
  (searched). So "is `Method` valid on the mapped class" (the
  CharacterController→BasePart→`.Move()` anecdote) cannot be validated generically
  without bundling a new dependency. AND the transpiler already routes
  `CharacterController.Move`/`.SimpleMove`/`.isGrounded` through a bridge
  (api_mappings API_CALL_MAP), so that specific anecdote is largely already
  handled — the method-validity gap is mostly theoretical.

## Decisions (recommendations — I'm deciding these myself per the autonomous run)

- **DQ1 scope → reachability only; DEFER method-validity.** Check B flags
  `GetComponent("X")` where X is unreachable. Method-validity (X maps to class C
  but the called method isn't on C) is DEFERRED with a documented gap (needs a
  Roblox class-method authority the repo lacks; the motivating anecdote is
  bridge-handled anyway). Recorded, not silently dropped.
- **DQ2 reachable set.** X is reachable iff ANY of: (a) X is a peer converted
  module — X ∈ {topology module `stem`s}; (b) X ∈ `_UNITY_TO_ROBLOX_CLASS` keys
  (a mapped Unity type, incl. the Transform/RectTransform sentinel); (c) X ∈
  `_UNITY_TO_ROBLOX_CLASS` VALUES (X is already a Roblox class name like
  "BasePart"/"Sound" — covers code that passes a Roblox class directly). Else →
  unreachable → warning. (Using the map's values as the "known Roblox classes"
  proxy avoids needing a full Roblox class list.)
- **DQ3 read the runtime map.** Parse `_UNITY_TO_ROBLOX_CLASS` out of
  runtime/scene_runtime.luau at verifier time (small line-scan: `Ident = "Str",`
  pairs within the table block + the two `_UNITY_TO_ROBLOX_CLASS.Transform =`
  sentinel assigns). Guard with a test asserting the parse finds known entries
  (CharacterController→BasePart, ParticleSystem→ParticleEmitter) so a runtime-file
  refactor that breaks the parse fails loudly, not silently.
- **DQ4 which call sites.** Scan `:GetComponent("X")` / `:GetComponentInChildren("X")`
  / `:GetComponentInParent("X")` with a STRING-LITERAL arg. Non-literal args
  (a variable) are skipped (can't resolve statically) — recorded as info?
  (Recommend: silently skip non-literal — too noisy otherwise; note in the
  metric docs.) Scan the emitted Luau on `self.state.rbx_place.scripts` (the
  same source surface check A uses).

## Open question for reviewers
- Is the DQ2 reachable set free of false positives? Specifically: are there
  legit `GetComponent("X")` calls where X is a Roblox class NOT present as a
  value in `_UNITY_TO_ROBLOX_CLASS` (e.g. "Humanoid", "ClickDetector")? If so the
  values-as-proxy misses them → false positive. Should the reachable set also
  include a small explicit allowlist of common Roblox classes, or read a broader
  source? (This is the main false-positive risk.)
- Peer-module join: peer lookup is by `stem` OR `scriptId` at runtime
  (scene_runtime.luau:758). The verifier only has stems easily — is stem
  sufficient, or can a GetComponent arg be a scriptId/GUID?

## What reviewers return
Confirm/refute GF-B1..B4. Vote DQ1-DQ4. The critical risk is DQ2 false positives
(flagging a legit Roblox-class GetComponent) — pressure-test it. Flag any 5th
decision (e.g. should `GetComponent<T>()` residual generic-syntax — which
code_transpiler.py:1771 already warns on — be in scope, or is that separate?).
