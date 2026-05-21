# Design: domain-classifier signal taxonomy

**Status**: DRAFT v2, post-audit. Replaces PR3b's implicit
classifier policy. See audit at
`/tmp/codex-domain-audit-v2-output.txt` and the SimpleFPS
misclassification evidence at
`/tmp/simplefps-wiring-ai-v2/conversion_plan.json` (24/28 modules
on server, root cause: classifier reads only post-transpile Luau
+ never consults C# source + never uses the strongest available
UI signal which the planner already computes).

**Key changes vs PR3b's original implicit policy**:
- Adds C# source as a primary signal channel (not just Luau).
- Adds `instance_owner_is_ui` per-instance signal.
- Adds explicit `--networking=none|mirror|netcode` mode selection.
- Adds `--strict-classification` toggle for production runs.
- **Eliminates `legacy` as a value.** Cleanly separates two
  orthogonal axes:
  - **Container (storage)**: `server` | `client` | `replicated` —
    where the script's file lives in the Roblox DataModel.
  - **Execution domain**: `client` | `server` | `helper` |
    `excluded` — what the host runtime does with the module.
  Unresolvable runtime-bearing modules become `excluded`
  (recorded in report, not instantiated) in default mode, or
  hard-fail in `--strict-classification` mode. No silent
  fallback to a pre-contract path.

## Problem

PR3b's domain classifier (`scene_runtime_domain.py`) consults
exactly two inputs: (1) regex patterns against the post-transpile
Luau body, and (2) `target_is_ui` stamps on outgoing references.
Both signals are sparse under the generic prompt — the contract-
compliant Luau output uses host-surface idioms (`self.gameObject`,
`self.host:connect`) instead of Roblox-flavored APIs the
classifier looks for. The strongest available UI signal — whether
the script's own host GameObject lives under a Canvas — is
computed by the planner (`scene_runtime_planner._scene_ui_go_fids`)
but never consumed by classification. When no signals fire, the
classifier defaults to `server` low_confidence — which crashes
client-only modules at runtime when their host GameObject (HUD
under PlayerGui) doesn't exist on the server.

Separately: the `legacy` fail-closed verdict silently routes
modules through a different (pre-contract) emit path. This
created the FireLight crash family — modules silently rendered as
legacy top-level scripts despite the user requesting `--scene-
runtime=generic`. The new model rejects silent fallback.

## Scope

This doc describes the **signals** the domain classifier uses,
the **resolution rules** that combine signals into a verdict, the
**fallback policy** when no signals fire, and the **handling of
unresolvable cases**. It does NOT redesign the overall PR3b
contract (placement under `ReplicatedStorage`, host-runtime
instantiation, `_GENERIC_RUNTIME_PROMPT` shape) — those stay as
PR3b committed. This is signal-acquisition + verdict policy only.

## Target model

The converter targets two distinct Unity game shapes, selected
via a new CLI flag:

- `--networking=none` (default): **single-player Unity ports**
  (asset-store games, demos, prototypes). No netcode framework in
  the source; Roblox client/server split is invented by the
  converter. Default fallback: **client**.
- `--networking=mirror` / `--networking=netcode`: **networked
  Unity games**. Source uses Mirror or Unity.Netcode for
  GameObjects. Converter HONORS netcode annotations directly.
  Default fallback: **server** (authoritative).

No auto-detection. Operator picks. Wrong picks surface at
conversion time via the report.

## Two-axis model

PR3b's `domain` field conflated two independent concerns. This
design separates them:

### Axis 1 — Container (storage)

Where the script's file lives in the Roblox DataModel. Mirrors
storage_classifier's existing output. Three values:

| Value | Roblox location |
|-------|-----------------|
| `server` | `ServerStorage` / `ServerScriptService` |
| `client` | `StarterPlayer.StarterPlayerScripts` / `StarterPlayer.StarterCharacterScripts` |
| `replicated` | `ReplicatedStorage` / `ReplicatedFirst` |

Storage_classifier (unchanged from PR3b) computes this from
call-graph reachability + the operator's `storage_overrides`.

### Axis 2 — Execution domain

What the host runtime does with the module. The CLASSIFIER's
job. Four values:

| Value | Meaning | Lifecycle |
|-------|---------|-----------|
| `client` | Runtime-bearing; instantiated by `SceneRuntimeClient` per-player | Awake/Start/Update/etc. fire on client |
| `server` | Runtime-bearing; instantiated by `SceneRuntimeServer` | Awake/Start/Update/etc. fire on server |
| `helper` | Not runtime-bearing; pure utility module | Never instantiated; only `require()`-ed |
| `excluded` | Runtime-bearing but unresolvable; not instantiated | Lifecycle never fires; recorded in conversion report |

The two axes are independent except for one constraint:

**Reachability constraint**: a module with `execution_domain =
"client"` MUST have `container ∈ {"client", "replicated"}` —
client runtime cannot `require()` from `ServerStorage` /
`ServerScriptService`. Storage_classifier honors this today via
the reachability rule in `scene_runtime_domain.py:588-665`
(client require graph → never reaches ServerStorage). The
classifier should fail-closed (set `execution_domain =
"excluded"`) if signals point at `client` but the require graph
forces `container = "server"` (the existing reachability
fail-closed case).

Examples:

| Module | Container | Execution | Why |
|--------|-----------|-----------|-----|
| `HudControl` | `replicated` | `client` | In ReplicatedStorage so client can require; runs as per-player UI |
| `GameManager` | `replicated` | `server` | In ReplicatedStorage (client may need to require for type info); runs as server singleton |
| `MathUtil` | `replicated` | `helper` | Pure utility; required by other modules; never instantiated |
| `AntiCheatChecker` | `server` | `server` | In ServerStorage (hidden from client); runs on server |
| `ConflictingComponent` (both_side_api conflict) | `replicated` | `excluded` | Can't resolve; emit but don't instantiate |

### Schema naming (keep `domain`)

To avoid breaking on-disk PR3b artifacts and the multiple
readers (`scene_runtime_planner.py:48-73`,
`scene_runtime_domain.py:304-305`,
`runtime/scene_runtime.luau:456`, autogen plan encoder,
`pipeline.py:3916-3957`):

- `SceneRuntimeModule.container` stays as Axis 1 (already exists).
- `SceneRuntimeModule.domain` stays as Axis 2 (no rename) — its
  legal values change from `{client, server, legacy}` to
  `{client, server, helper, excluded}`.
- `scene_runtime.domain_overrides` keeps its name (sticky-merge
  logic depends on this key).

The doc uses "execution_domain" CONCEPTUALLY for clarity vs the
container axis; in code + artifact, the field is `domain`.

Migration: existing PR3b `domain == "legacy"` rows map to
`domain = "excluded"` on first re-read and are recorded in the
report.

## Signal taxonomy

Two tiers: **strong** (unambiguous direction) and **moderate**
(generally correct, can be overridden by stronger signals).
**Weak signals are not used** — class-naming heuristics and
AudioSource are dropped. Animator was demoted from "weak"
(dropped) to "moderate client" after asset-store-game evidence
suggested it correlates strongly with client-side visual playback.

### Strong client signals

| Pattern | Source | Mode |
|---------|--------|------|
| `[ClientRpc]`, `[Client]` | C# annotation | Mirror only |
| `using UnityEngine.UI` | C# import | Both |
| `using TMPro` | C# import | Both |
| `using UnityEngine.EventSystems` | C# import | Both |
| `Input.Get*`, `Input.mousePosition` | C# API | Both |
| `OnGUI()` method body | C# | Both |
| **Script attached to GameObject owning a Canvas** | Planner `ui_go_fids` (per-instance) | Both |
| `PlayerPrefs.*` | C# API | Both |
| `Cursor.*`, `Screen.*`, `Application.platform` | C# API | Both |
| `[SerializeField] Text/Image/Slider/Button/RectTransform/RawImage/TMP_Text/CanvasGroup` | C# field type | Both |
| **Roblox-flavored patterns from post-transpile Luau** (`Players.LocalPlayer`, `.PlayerGui`, `RunService.RenderStepped`, `.OnClientEvent`, `UserInputService`, `workspace.CurrentCamera`) | Luau (PR3b's existing `_GENERIC_CLIENT_API_PATTERNS`) | Both |

### Moderate client signals

| Pattern | Source | Mode | Notes |
|---------|--------|------|-------|
| `Camera.main` | C# API | Both | Per-player camera; usually client, but server scripts can read it via Unity's main-camera tag |
| `Animator.SetBool/SetFloat/SetInteger/SetTrigger/CrossFade/Play` | C# API | Both | Visual playback. Server can replicate animation state via attributes, but the playback API itself correlates strongly with client. Promoted back from "weak" per user direction. |

### Strong server signals

| Pattern | Source | Mode |
|---------|--------|------|
| `[ServerRpc]`, `[Server]`, `[ServerCallback]` | C# annotation | Mirror only |
| `: NetworkBehaviour` | C# class declaration | Mirror only |
| `[SyncVar]` | C# field annotation | Mirror only |
| **Roblox-flavored server patterns** (`.OnServerEvent`, `:FireClient(`, `DataStoreService`, `MessagingService`) | Luau (PR3b's existing `_GENERIC_SERVER_API_PATTERNS`) | Both |

### Moderate server signals

| Pattern | Source | Mode |
|---------|--------|------|
| Script's transitive require graph reaches a `NetworkBehaviour` subclass | Reference graph | Mirror only |

## Mirror-mode adoption heuristic

When `--networking=mirror` (or `netcode`), the converter expects
the source to use netcode annotations (`[ServerRpc]`,
`NetworkBehaviour`, etc.). The `mirror_adoption_low` warning
fires when **either**:
- Annotated classes count < `max(2, ceil(0.05 × runtime_bearing_classes))`, OR
- Project has zero `using Mirror` / `using Unity.Netcode` imports anywhere.

Warning message: "Mirror mode declared but only N of M
runtime-bearing classes carry netcode annotations. Most modules
will fall through to the server default. Consider
--networking=none or expand annotations."

This catches the misuse case: operator declared `--networking=
mirror` but the Unity source isn't actually a Mirror project.
The hybrid threshold scales with project size; the `using`
check catches projects that forgot annotations entirely.

## Resolution rules (execution_domain only)

These rules determine **execution_domain** (Axis 2). Container
(Axis 1) is computed separately by storage_classifier.

**`instance_owner_is_ui` is a STRONG CLIENT SIGNAL** (it's
listed in the strong-signal table above, and pre-aggregated into
`strong_client` before rule evaluation). Same applies to
`target_is_ui` references PR3b already stamps.

For non-runtime-bearing modules (helper classes, pure data,
type-only declarations): **`domain = "helper"`**. Signal
pipeline doesn't apply.

For runtime-bearing modules, given match counts
`SC = strong_client`, `SS = strong_server`,
`MC = moderate_client`, `MS = moderate_server`:

| # | Condition | Result | Confidence |
|---|-----------|--------|------------|
| 1 | `SC > 0 AND SS > 0` | `excluded` (unresolvable) | — |
| 2 | `SC > 0 AND SS == 0` (any moderate counts allowed) | `client` | High |
| 3 | `SS > 0 AND SC == 0` (any moderate counts allowed) | `server` | High |
| 4 | `SC == 0 AND SS == 0 AND MC > 0 AND MS > 0` | `excluded` (unresolvable, moderate-only ambiguity) | — |
| 5 | `SC == 0 AND SS == 0 AND MC > 0 AND MS == 0` | `client` | Moderate |
| 6 | `SC == 0 AND SS == 0 AND MS > 0 AND MC == 0` | `server` | Moderate |
| 7 | All zero (no signals) | Fallback (mode-dependent, see below) | Low (`low_confidence` flag set) |

Rule 2 covers `SC + MC` ("strong client only"), `SC + MS` ("strong
client + moderate server" — strong wins), and `SC + MC + MS`
("strong client + mixed moderates"). Symmetric for Rule 3.

**Operator override** (`scene_runtime.domain_overrides[script_id]`):
Applies after the rule table. Override values must be in
`{client, server, excluded}`.

| Verdict before override | Override allowed? | Effect |
|-------------------------|-------------------|--------|
| Any (single-side or fallback) | `client` / `server` / `excluded` | Replaces verdict; signals recorded for audit |
| `excluded` from Rule 1 (both strong sides) | **Only `excluded`** | Operator can ACKNOWLEDGE the exclusion; cannot pin to a side. Splitting the source class is the only way to reach client/server. |
| `excluded` from Rule 4 (moderate-only ambiguity) | `client` / `server` / `excluded` | Operator can pin; moderate-only conflicts are softer than strong-side conflicts. |

The asymmetry (Rule 1 cannot be override-pinned to a side, Rule
4 can) is intentional: strong-side conflicts mean the code
actually touches both APIs and would crash whichever side it's
forced onto. Moderate-side conflicts have weaker evidence and
the operator's judgment is honored. This addresses the Codex
critique that "operator must refactor third-party Unity C#" was
impractical — they can at least ACK-AND-SKIP (override to
`excluded`) without source surgery.

## Fallback policy (zero-signal runtime-bearing scripts)

Mode-dependent default:

- `--networking=none` → `client` (low_confidence stamped)
- `--networking=mirror`/`netcode` → `server` (low_confidence
  stamped)

`low_confidence` is recorded in
`scene_runtime.modules[*].domain_signals.low_confidence`. The
conversion report enumerates all low_confidence modules so the
operator can review.

## Unresolvable cases → `execution_domain = excluded`

A runtime-bearing module's execution_domain becomes `excluded`
when:
- Rule 1: both strong sides fire (code disagrees with itself), OR
- Rule 6: both moderate sides fire and no strong signals exist,
  OR
- Reachability conflict: helper required by both sides AND the
  storage classifier wants ServerStorage (PR3b's existing case).

`excluded` modules produce a `FailClosed` row in the conversion
report with kind `unresolvable_execution_domain` and a `detail`
listing the conflicting signals.

**Behavior depends on `--strict-classification`**:

| Strict mode | Behavior |
|-------------|----------|
| ON (default for production) | Conversion BLOCKS before transpile. Operator must add `scene_runtime.domain_overrides` for each `excluded` module OR split the source class to remove the conflict. |
| OFF (default for iteration) | Conversion proceeds. `excluded` modules are NOT instantiated by either host runtime — the `SceneRuntimePlan` lists them but with `execution_domain = "excluded"` so the runtime skips lifecycle wiring. The conversion report lists them prominently. The converted place runs without those modules — silently broken for the affected behavior, but no crash. |

**No silent fallback to a pre-contract path.** Either the
operator resolves the conflict, or the module's execution_domain
stays `excluded`.

## Strict classification mode

CLI flag: `--strict-classification` (default off).

OFF (default): low_confidence and zero-signal modules proceed via
fallback; unresolvable modules are excluded from
`SceneRuntimePlan`; warnings surface in the conversion report.

ON: any low_confidence, zero-signal, or unresolvable runtime-
bearing module BLOCKS the transpile phase. Operator must add
`domain_overrides` for each before conversion can proceed.

## Operator workflow

1. First conversion: run `convert --scene-runtime=generic
   --networking=none` (or `mirror`). Classifier reports per-module
   verdicts + signals fired. low_confidence and unresolvable
   modules surface in the conversion report.
2. Operator reviews report. For low_confidence modules where
   they have intent, they add `scene_runtime.domain_overrides`
   entries. For unresolvable modules, they either split the
   source class or pin via override.
3. Re-run conversion. Sticky overrides preserved per existing
   `_classify_storage` merge logic.
4. Optional: use `--strict-classification` to force explicit
   classification for every ambiguous module before transpile.
   Recommended for production runs.

## Test matrix

Per-canary expected breakdowns under `--networking=none`:

| Canary | Strong client | Strong server | Zero-signal (→client) | Unresolvable |
|--------|--------------|---------------|----------------------|--------------|
| SimpleFPS (single-player FPS) | HudControl, Menu, Player, Plane, HostilePlane | (none under `none`) | Door, Pickup, Turret, FireLight, ParticleSystem*, Water* | (none expected) |
| trash-dash | HUDController, ScoreUI, InputController | StateManager, GameStateAuthority | helpers | CharacterInputController (both_side_api confirmed by audit) |

Under `--networking=mirror`:
- Hypothetical Mirror-using FPS canary: scripts annotated with
  `[ClientRpc]` → client; `[ServerRpc]` → server; helpers without
  annotations → server (mode fallback) + `mirror_adoption_low`
  warning if <2 annotated classes total.

## Migration from current PR3b

Current PR3b is `--networking=mirror`-style fallback (server-
authoritative default) without any of the strong client signals
this design adds, AND uses `legacy` as a domain value. The
migration:

1. Add the new strong-signal patterns + C# source plumbing.
2. Add the `instance_owner_is_ui` per-instance signal stamping.
3. Add the `--networking` CLI flag with `none` as default for new
   conversions.
4. Add `--strict-classification` flag.
5. **Remove `legacy` as a valid domain value.** Audit all uses:
   - `scene_runtime_domain.py:_classify_module` — replace legacy
     verdicts with unresolvable handling.
   - `scene_runtime_planner.py` — strip "legacy" from domain
     value enums / TypedDicts.
   - Host runtime (`runtime/scene_runtime.luau`) — stop
     branching on `domain == "legacy"`.
   - `_subphase_inject_autogen_scripts` legacy emit (the
     `ClientBootstrap` coexistence per PR4 followups #3) — out
     of scope here but flagged for a follow-up.
6. Existing projects with cached conversions: re-run
   classify_storage surfaces new signals; sticky `domain_overrides`
   preserved.

## Mirror without explicit annotations

Per user direction: add the `mirror_adoption_low` warning
heuristic described above. Threshold is initially 2 annotated
classes; tunable. The warning fires AFTER classification (not
before) so it doesn't block iteration. Combined with strict mode,
it surfaces operator misconfiguration loudly.

## Animator handling

Per user direction: Animator is back in as a **moderate client
signal**. Promoted from the dropped "weak" tier. Rationale: real
asset-store games use Animator predominantly for client-side
visual playback. Server-side Animator usage in Unity is rare
(usually replicated as state, not driven directly). If real
canary evidence shows Animator firing wrong, demote later.

## Not in scope

- Weak signals beyond Animator (class naming, AudioSource) —
  separate design pass if needed.
- Per-instance auto-detection of conflicts (e.g., same class
  instantiated under Canvas AND under Workspace) — already handled
  by PR3b's intra_class_conflict logic.
- Cross-domain reference detection — already handled by PR4.
- Project-level signal override file — explicitly declined per
  user direction. Operators use `domain_overrides` instead.

## Open questions (none remaining)

All three prior open questions resolved by user direction:
- Mirror-without-annotations → adoption-low warning heuristic (above).
- Animator → moderate client signal.
- Project-level override file → out of scope.
