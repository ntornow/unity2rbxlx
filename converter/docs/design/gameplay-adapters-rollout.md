# Gameplay Adapters Rollout

**Status:** Authored PR #74 (default-on flip). Companion to
`gameplay-adapters.md`. This doc codifies the soak-and-delete exit
criteria for PR #76 (delete the three legacy coherence packs) and
the rollback levers we keep during the soak window.

## What "rollout" means here

PR #74 flips `--use-gameplay-adapters` to default ON. Every
conversion now routes door / projectile / damage patterns through the
gameplay-adapter pipeline. The legacy `script_coherence_packs` packs
(`bullet_physics_raycast`, `player_damage_remote_event`,
`door_tween_open`) still ship in the converter — disabled at pipeline
level when adapters are on (via
`LEGACY_PACKS_DISABLED_WHEN_ADAPTERS_ON` in `pipeline.py`), runnable
via the new `--legacy-gameplay-packs` opt-out flag.

The soak window is the interval between PR #74 merge and PR #76
merge. The four exit criteria below must ALL be documented as met
before PR #76 lands and deletes the legacy packs.

## Exit criteria for PR #76

The numbering matches the design doc's `gameplay-adapters.md` PR #76
bullet list. Order is significant — criterion 1 anchors the soak
window; the other three are observations across that window.

### 1. Adapters default-on for ≥ 2 consecutive converter releases

The soak window must span **at least two converter release boundaries**
with adapters default-on. A single release isn't enough — a bug that
escapes a clean smoke test can still surface in real conversions days
later when an operator runs a re-conversion against a project we
haven't tested. Two release cycles is the minimum interval to give
that signal time to land.

**How to verify:**
- `git log v74.x..v74.y -- converter/converter/pipeline.py
  converter/converter/gameplay/ converter/runtime/gameplay/` shows
  adapter-only commits between the two boundary releases.
- `git log v74.y..v74.z` shows the same. The second interval gates
  PR #76.

If the second interval contains a `--legacy-gameplay-packs`-triggered
rollback that wasn't a no-op, the soak window resets to the next
release cycle. Document the reset in this file's "Rollout history"
section.

### 2. Zero P1/P2 gameplay regressions on the three target patterns

Across the soak window:

  - **Door behaviour** (`Trigger.OnBoolAttribute` →
    `Movement.AttributeDrivenTween`). Doors must continue to open
    and close on attribute change. The runtime coexistence guard
    in the legacy `_AutoFpsDoorTweenInjected` block (defers to any
    sibling `Anim_*_door_*` driver) is NOT preserved by the adapter
    pipeline — projects with hand-authored door animation drivers
    must be on the deny-list. Document any such deny-list entries
    in the rollout history.

  - **Projectile behaviour** (`Movement.Impulse`,
    `HitDetection.RaycastSegment`, `Effect.Damage`,
    `Effect.Splash`). Bullets must travel and deal damage on hit.
    The `Effect.Damage` runtime contract (Player-tag gate +
    despawn-on-any-impact, pinned in
    `tests/test_gameplay_adapters_projectile.py`) is load-bearing —
    a P1 regression on either invariant fails this criterion.

  - **Damage routing** (`damage_protocol.luau` server-side
    validator). The origin-drift gate, raycast replay, distance
    gate, and value-preserving attribute mirror are all pinned by
    `tests/test_gameplay_adapters_damage_protocol.py`. Any P1
    regression on those failure modes fails this criterion. The
    PR #74 codex round-2 [P2] gate (DamageProtocol only emits when
    a Player-damage signal is present) protects projects using
    `DamageEvent` for unrelated traffic; a regression that re-binds
    OnServerEvent unconditionally also fails this criterion.

**How to verify:**
- `gh issue list --label gameplay-adapter --state all` filtered to
  the soak window — must show zero P1/P2 entries against the three
  patterns above. P3+ findings (cosmetic, edge-case-only,
  documentation) do not block PR #76 — escalate them to a separate
  follow-up.
- For each P1/P2 closed during the window, the PR fixing it must
  include a regression test pinning the invariant (the
  `test_gameplay_adapters_*` test layout is the canonical home).

### 3. Cross-project smoke matrix green at PR #74 merge AND at the end of soak

The smoke matrix lives in
`tests/test_gameplay_adapters_cross_project.py` and covers
SimpleFPS (real-source detection + synthetic fixture), Gamekit3D
(zero-match assertion + synthetic fixture rejection), and ChopChop
(zero-match assertion + synthetic fixture rejection). The synthetic-
fixture half runs on every test machine; the real-source half runs
only when the project tree is checked out under `test_projects/`.

Green at PR #74 merge is necessary to start the soak window. Green
at the end of soak is necessary to enter PR #76. Both invocations
must use the SAME detector inventory pin (`ALL_DETECTORS` set), so
a new detector shipped mid-soak forces an explicit decision about
cross-project coverage (the pin fails closed).

**How to verify:**
- PR #74 CI run shows the smoke matrix as green. CI logs are kept
  per the repo's standard CI retention policy.
- PR #76 CI run (against the soak-end main) shows the smoke matrix
  as green with the SAME `ALL_DETECTORS` set — diffs against PR #74
  must be explicit decisions, recorded in the rollout history below.

### 4. `--legacy-gameplay-packs` rollback usage near-zero during soak

The `--legacy-gameplay-packs` flag is the user-facing rollback
lever for the default-on flip. If users are reaching for it
non-trivially, the adapter pipeline isn't ready. "Near-zero" is
operationalized as **no recorded use beyond the PR #74 author's
own validation runs**. We don't ship a telemetry pipeline for the
converter; this criterion is tracked manually via:

  - The rollout-history table in this doc (every recorded
    rollback gets a row).
  - Any inbound user reports / GH issues mentioning
    `--legacy-gameplay-packs`.

A single recorded rollback during soak does NOT automatically fail
the criterion — the right response is to root-cause the project
that needed it and ship a fix (or a documented deny-list entry).
**Three or more independent rollbacks** during soak DOES fail this
criterion, and the soak window restarts after the root-cause fix.

## Rollback levers during the soak window

Each lever rolls back independently so a regression doesn't have to
revert the whole rollout:

| Lever | Scope | How to use |
|---|---|---|
| `--legacy-gameplay-packs` | Per-conversion. Force the legacy regex packs and skip every adapter runtime module. | `python u2r.py convert <project> --legacy-gameplay-packs ...`. The pipeline asserts mutual exclusion with `--use-gameplay-adapters` (passing both raises a `UsageError`). |
| `<output>/.gameplay_deny.txt` | Per-component within an adapter-enabled run. Deny one or more scene-node / component file_ids so the adapter pipeline skips them and falls through to AI transpile. | One id per line. Accepts bare `<file_id>` OR qualified `<source_path>#<file_id>` (use the qualified form when the same file_id appears in multiple prefab assets). |
| Revert PR #74 | Full converter rollback. | Default-off flip plus `--use-gameplay-adapters` opt-in (the pre-PR-#74 posture). Reserved for a P0 regression that the two-level rollbacks can't contain. |

## Rollout history

Append a row per rollout event. Used during PR #76 review as the
audit trail for criteria 1, 2, and 4.

| Date | Event | Notes |
|---|---|---|
| 2026-05-13 | PR #74 opened on `gameplay-adapters-74` | Default-on flip + `--legacy-gameplay-packs` opt-out + rehydration prune + [P2] DamageProtocol gate. Soak window begins at merge. |

## CI gate for PR #76

PR #76's CI must include a check that fails closed if any of the
four criteria above are not documented as met. The check reads:

  - This file's "Rollout history" table for criteria 1 + 4.
  - `gh issue list --label gameplay-adapter --state all` for
    criterion 2.
  - `tests/test_gameplay_adapters_cross_project.py` test status
    (must pass on the PR #76 base) for criterion 3.

The check is intentionally NOT auto-generated from CI alone — it
requires a human-readable "yes, all four met" entry in this doc
gated by reviewer sign-off. Auto-deletion of the legacy packs is
too consequential to gate on a green-check only.

## Cross-references

  - `gameplay-adapters.md` — the binding architecture spec. PR #74
    implements the "PR #74" section there.
  - `tests/test_gameplay_adapters_cross_project.py` — the smoke
    matrix for criterion 3.
  - `tests/test_gameplay_adapters_pr74.py` — pins the default flip
    + the prune-pass surfaces.
  - `tests/test_gameplay_adapters_damage_protocol.py` — pins the
    [P2] DamageProtocol gate + the orchestrator's conditional
    require chain.
