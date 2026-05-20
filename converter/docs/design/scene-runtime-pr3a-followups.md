# PR3a follow-ups (deferred from Codex review)

Codex reviewed PR3a in one `codex exec` pass (architecture-flavored,
single-shot, brief at `/tmp/codex-pr3a-review.txt`) against the
original commit `569bfc5`. The amended commit absorbs both P1 findings
in-line (see PR3a description); the two P2 carry-overs below are
queued for follow-up so PR3a ships clean.

Ordered by load-bearing-ness for downstream consumers (PR3b / PR4).

## 1. Stubbed runtime-bearing modules don't surface fail-closed (PR3b scope)

**Where:** `converter/converter/contract_pipeline.py` —
`transpile_with_contract`'s ``fail_closed`` aggregation loop.

**Why:** When AI transpilation fails entirely (backend error,
unavailable, low confidence), `transpile_scripts` falls back to the
stub generator at `code_transpiler.py:294`, which emits a minimal
`print("X loaded")` placeholder. The current orchestrator only counts
post-reprompt verifier failures and require-resolution errors as
fail-closed reasons; a stubbed runtime-bearing MonoBehaviour ends
the run as placeholder logic with `ContractPipelineResult.fail_closed`
silent. PR3b's auto-mode reads this surface to decide when to fall
back to legacy — a stubbed runtime-bearing module is operationally
equivalent to "the AI couldn't follow the contract" and should
trigger the same fallback.

**Scope (in PR3b):** in the orchestrator's aggregation loop, add a
fourth fail-closed kind `stub_strategy` that fires when a
runtime-bearing script's final `strategy != "ai"`. The
``verify_module`` re-check on cache hit already covers the "we have
cached AI output but the verifier rejects it" case; this catches the
distinct "we never got AI output" case. Add a fixture: AI backend
disabled (`use_ai=False`), runtime-bearing MB falls through to stub,
assert `fail_closed` contains a `stub_strategy` row.

## 2. Rule (e) only catches `self.host` and `self:GetComponent`

**Where:** `converter/converter/runtime_contract.py:_check_constructor_purity`
— `_FORBIDDEN_IN_NEW` list (line ~340 in PR3a's HEAD).

**Why:** Piece 1 of the design doc says the **full** injected host
surface (`self.gameObject`, `self.transform`, `self.instance`,
`self.enabled`, `self:GetComponent(...)`, `self.host`) is bound by the
host **after** `new()` returns. Rule (e) only flags `self.host` reads
and `self:GetComponent(...)` calls. A constructor that reads
`self.gameObject.Name` or `self.transform:GetPivot()` passes the
verifier today and then crashes at runtime in PR4 — the host hasn't
populated those fields yet.

**Scope (in PR3a or a follow-up):** add `self.gameObject`,
`self.transform`, `self.instance`, `self.enabled` to the
`_FORBIDDEN_IN_NEW` regex list. Add 4 regression fixtures (one per
field) to `tests/test_runtime_contract.py::TestRuleE`. The check is
lexical-substring already, so the change is mechanical. Deferred only
because the compliance spike's two test projects (SimpleFPS,
trash-dash) didn't hit this code path empirically and a separate
fixture-driven verification is cleaner than a re-run of the spike.

## Findings codex absorbed in PR3a (NOT in this list)

These two P1s landed in the amended PR3a commit:

- **P1#1** — generic prompt + verifier were applied to non-runtime-
  bearing scripts (LocalScripts, helpers) under generic mode. Fix:
  per-script `effective_runtime_mode` gated on
  `info.path in runtime_bearing_paths`. Tests:
  `test_scene_runtime_transpiler.py::TestPerScriptGating`.

- **P1#2** — runtime-bearing modules with stem collisions were
  silently dropped from `runtime_bearing_paths` with only a log
  warning. For prefab-only or component-registry-only behaviours
  that never appear at a `require("@scene_runtime/<stem>")` site,
  the collision was invisible. Fix: `_runtime_bearing_paths` now
  returns `(paths, collisions)` and `transpile_with_contract` emits
  a `FailClosed(kind="runtime_bearing_collision")` row per colliding
  stem. Tests: `test_contract_pipeline.py::TestRuntimeBearingCollisions`.
