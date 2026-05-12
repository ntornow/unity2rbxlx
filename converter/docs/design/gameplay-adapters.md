# Gameplay Adapters

**Status:** Proposed 2026-05-12. Revised after codex architecture review.
First stage: PR #72 (this doc). Targets the eventual removal of three
coherence packs from `converter/converter/script_coherence_packs.py`
(`bullet_physics_raycast`, `player_damage_remote_event`, `door_tween_open`).

## Decision

Translate Unity gameplay subsystems via **structured per-instance behaviour
descriptions composed from orthogonal capability pieces, executed by shared
runtime libraries** — instead of regex-patching AI-transpiled Luau text after
the fact.

## Why we're changing course

The current approach lives in `script_coherence_packs.py` as three packs.
PR #71 took 12 rounds of codex review with new findings every round. The
findings cluster into five recurring categories:

1. **Regex misses AI output variations** — different runs name locals
   `hitInst` / `hitPart` / `rb` / `rootPart`; AI emits
   `(GetAttribute or 0) + 1` one round and `true` the next.
2. **Client-server protocol drift** — the client patch and the server router
   live as two independent text blobs and drift across rounds.
3. **Mixed-project scoping** — packs run on the flat `place.scripts` list
   (one entry per `.cs` class), with no notion of which prefab instance the
   adapter should fire for.
4. **Network input validation** — every iteration of the `DamageEvent`
   RemoteEvent surfaces a new attack class.
5. **Unity-semantics regression** — wholesale-replacement keeps dropping
   Unity-specific behaviour (splash, explosion VFX, prefab override values).

These aren't regex-quality problems. The pack abstraction is the wrong tool
for the job: each pack is doing semantic lowering, protocol design, and
runtime generation inside a post-hoc text patcher.

## Architecture

### Capability pieces, not monolithic specs

Earlier drafts proposed three flat spec dataclasses (`ProjectileSpec`,
`ActuatorSpec`, `DamageInteractionSpec`). Codex review pushed back: those
overloaded transport, lifetime, hit-mode, area-of-effect, and VFX into a
single struct, and didn't cover sensor/trigger-driven interactions
(`OnTriggerEnter`, `OverlapSphere`, animation events). Replaced with an
orthogonal capability model.

Each Unity behaviour decomposes into composable pieces — `dataclass(frozen=True)`
records under `converter/converter/gameplay/capabilities.py`:

  - **Movement**
    - `Impulse(direction_local, force_unity)` — Rigidbody + AddRelativeForce
    - `ConstantVelocity(velocity_unity)`
    - `Anchored` (no motion; lifetime-only)
  - **Lifetime**
    - `Despawn(seconds)` — `Destroy(gameObject, t)`
    - `OnFirstHit` — destroyed by hit detection rather than timer
    - `Persistent`
  - **HitDetection**
    - `RaycastSegment` — server-side per-Heartbeat segment cast; catches
      tunneling on fast projectiles
    - `Touched` — Roblox Touched event
    - `OverlapSphere(radius_unity)` — `GetPartBoundsInRadius`, used for
      explosion/splash detection
  - **Effect**
    - `Damage(value)` — applies via the shared damage attribute protocol
    - `Splash(radius_unity, value)` — area damage in stud-converted radius
    - `SpawnTemplate(name)` — clone `ReplicatedStorage.Templates.<name>`
      at impact (e.g. `Explosion`)
    - `ApplyAttribute(name, value)` — generic attribute write on hit target
  - **Trigger**
    - `OnEnter(tag_or_attribute_filter)` — `OnTriggerEnter` with tag match
    - `OnBoolAttribute(name)` — Animator-bool, e.g. door's `open`; publishes
      current value to `ctx.trigger.value` (see normalization rules below)
    - `OnPickup(item_name)` — Pickup→Player attribute write

A `Behavior` dataclass is just an ordered list of capabilities plus a
binding context (which scene node carries it):

```python
@dataclass(frozen=True)
class Behavior:
    unity_file_id: str           # the scene-node fileID this behaviour binds to
    diagnostic_name: str         # e.g. "TurretBullet" — for logs/manifest only
    capabilities: tuple[Capability, ...]
```

PlaneBullet's converted behaviour is:
```python
Behavior(
    unity_file_id="…",
    diagnostic_name="PlaneBullet",
    capabilities=(
        Movement.Impulse(direction_local=(0,0,1), force_unity=200),
        Lifetime.Despawn(seconds=6),
        HitDetection.RaycastSegment(),
        Effect.SpawnTemplate(name="Explosion"),
        Effect.Splash(radius_unity=2, value=10),
    ),
)
```

TurretBullet's is:
```python
Behavior(
    unity_file_id="…",
    diagnostic_name="TurretBullet",
    capabilities=(
        Movement.Impulse(direction_local=(0,0,1), force_unity=60),
        Lifetime.Despawn(seconds=3),
        HitDetection.RaycastSegment(),
        Effect.Damage(value=10),
    ),
)
```

Door is:
```python
Behavior(
    unity_file_id="…",
    diagnostic_name="Door",
    capabilities=(
        Trigger.OnBoolAttribute(name="open"),  # writes ctx.trigger.value
        Movement.AttributeDrivenTween(          # reads ctx.trigger.value
            target_offset_unity=(0, 4, 0),     # from open.anim m_PositionCurves
            open_duration=1.0,                 # from clip length
            close_duration=1.0,                # from close.anim
        ),
        Lifetime.Persistent,
    ),
)
```

Door uses TWO capabilities, not one. Codex v3 review flagged that the
earlier `Movement.TweenOffset(direction_attribute="open")` form
embedded attribute-listening (a Trigger concern) inside a Movement
capability — undermining the otherwise-clean family split.

The capability split now mirrors the data flow:

  - `Trigger.OnBoolAttribute(name="open")` watches the named attribute
    and publishes its current value to `ctx.trigger.value`.
  - `Movement.AttributeDrivenTween` reads `ctx.trigger.value` and
    tweens toward/away from the target offset accordingly.

Cross-family handoff goes through the declared ctx contract, not
through a class-coupled field on the Movement capability. The same
`Trigger.OnBoolAttribute` is reusable for non-door cases (e.g. trap
plates, light switches) without dragging Movement semantics with it.

**`Trigger.OnBoolAttribute` normalization rules** (binding on
implementers, will be tested in PR #73):

  - **Missing or nil attribute on bind:** treated as `false`. The
    trigger publishes `ctx.trigger.value = false`, so a Door whose
    `open` attribute was never set starts closed.
  - **Non-bool value on bind or mutation:** coerced via Lua
    truthiness — anything that's not `false` or `nil` becomes `true`
    in ctx. (A numeric `1` or string `"yes"` both become `true`.)
    Avoids the situation where a misconfigured Unity prefab leaves
    the door in an undefined state.
  - **Attribute change event:** every change runs the value through
    the same normalization and re-publishes to ctx. Readers always
    see a strict bool.
  - **Idempotent listener registration:** if `Composer.run` is called
    on the same part twice, the second call observes the existing
    binding (see "Re-bind marker" below) and is a no-op for the
    listener registration too.

**Runtime semantics for `Movement.AttributeDrivenTween`** (binding on
implementers, will be tested in PR #73):

  - **Initial pose:** on first bind, read the current value of
    `ctx.trigger.value` and snap the part to the open OR closed pose
    without animating. A door spawned with `open=true` starts open.
  - **Rapid-toggle / mid-tween reversal:** when `ctx.trigger.value`
    changes during an in-flight tween, cancel that tween and start a
    new one from the current part position toward the new target. No
    snapping mid-motion.
  - **Idempotent re-bind:** calling `Composer.run` on the same part
    twice doesn't stack listeners or double-register attribute
    observers. The runtime detects an existing bind via the marker
    attribute `_GameplayBound = true` on the container (see "Re-bind
    marker" below) and is a no-op on the second call.

**Re-bind marker.** The composer sets a single attribute on each
container the first time it runs:

```
container:SetAttribute("_GameplayBound", true)
```

Every capability's runtime function reads this attribute BEFORE
registering anything (listeners, tweens, ctx writes) and exits early
when it's already set. One marker for the whole composer call —
individual capabilities don't each get their own marker. The marker
is intentionally underscored to indicate converter-owned, and named
generically (`_GameplayBound`, not `_DoorBound`) so the same
mechanism applies to every behaviour the composer drives.

Tests in PR #73 pin: (a) absent marker → composer registers
listeners as usual and sets the marker; (b) present marker →
composer is a no-op for THAT call (still safe to register new
behaviours that weren't part of the original bind via a separate
mechanism not covered here).

The capability vocabulary is **deliberately small**. New Unity patterns
extend the vocabulary by adding a single capability variant, not a new
top-level Spec class.

### Capability dataflow contract

The previous draft said the composer runs capabilities in tuple order
but didn't define how capabilities pass information to each other. Codex
review flagged this as the largest remaining gap: `HitDetection.RaycastSegment`
needs to know about the bullet's velocity (set by `Movement.Impulse`),
and `Effect.SpawnTemplate` needs the impact CFrame (set by the hit
detector). Without a declared contract, those handoffs become implicit
shared state through the part instance, which is exactly the kind of
fragility this redesign is trying to remove.

**Contract:** every capability declares what context keys it READS and
WRITES. The composer owns a per-behaviour `ctx` table keyed by capability
family, threads it through each capability call, and enforces:

  1. **Single-writer-per-key**: at most one capability per behaviour
     writes a given `ctx.<family>.<key>`. Two capabilities trying to
     write the same key is a converter-emit-time error, not a runtime
     surprise.
  2. **Reader-after-writer**: a capability that reads `ctx.<family>.<key>`
     must appear AFTER the capability that writes it in the tuple. Also
     enforced at emit time.
  3. **Namespaced by family**: ctx keys live under `ctx.movement.*`,
     `ctx.hitDetection.*`, etc. Cross-family collisions are impossible
     by construction; intra-family collisions are caught by the
     single-writer rule. The user's worry about local variable
     collision lives here — explicit namespacing per family is the
     answer.

Each capability declares its reads/writes via class-level constants
on the dataclass:

```python
@dataclass(frozen=True)
class MovementImpulse:
    direction_local: tuple[float, float, float]
    force_unity: float
    READS: ClassVar[frozenset[str]] = frozenset()
    WRITES: ClassVar[frozenset[str]] = frozenset({"ctx.movement.velocity"})

@dataclass(frozen=True)
class HitDetectionRaycastSegment:
    READS: ClassVar[frozenset[str]] = frozenset({"ctx.movement.velocity"})
    WRITES: ClassVar[frozenset[str]] = frozenset({
        "ctx.hitDetection.lastImpactCFrame",
        "ctx.hitDetection.lastInstance",
    })

@dataclass(frozen=True)
class EffectSpawnTemplate:
    name: str
    READS: ClassVar[frozenset[str]] = frozenset({"ctx.hitDetection.lastImpactCFrame"})
    WRITES: ClassVar[frozenset[str]] = frozenset()
```

Validator (`converter/converter/gameplay/composer.py`) walks the
capability tuple and checks: every key in a capability's `READS` set
has appeared in some prior capability's `WRITES` set; no key appears
in two `WRITES` sets. Mismatch → `BehaviorCompositionError` raised at
emit time, with a pointer to the source Unity component so the user
knows which prefab needs configuration adjustment (or which capability
combination isn't supported).

Runtime composer (`runtime/gameplay/composer.luau`) literally threads
a Lua table:

```lua
function Composer.run(container, capabilities)
    local ctx = {
        movement = {},
        lifetime = {},
        hitDetection = {},
        effect = {},
        trigger = {},
    }
    for _, cap in ipairs(capabilities) do
        local mod = _registry[cap.kind]
        mod.run(container, cap, ctx)
    end
end
```

Each runtime module is responsible only for its own family's namespace
in `ctx` — `movement.luau` writes `ctx.movement.*`, never anything else.
Convention enforced by tests (the registry's `run` function for each
family is given a `ctx.<family>` slice, not the full ctx, when feasible).

This gives us:
  - **No implicit shared state** between capabilities. Everything they
    share is named, declared, and validated.
  - **No variable collision** at the Lua level because each runtime
    module operates on a family-namespaced slice.
  - **Composition errors caught at emit time** with a clear diagnostic
    instead of mysterious runtime behaviour.
  - **Extension stays cheap**: a new capability variant declares its
    reads/writes once; the composer doesn't need to know about it
    until registration time.

### Detection is composition-first, source-substring is a confirmer

Codex review noted that C# substring matching is structurally the same
problem as Luau-regex matching, one level up. To prevent future detector
code from quietly violating "primary signal first," the detector API
itself separates the two layers:

```python
class Detector(Protocol):
    def primary(self, node: SceneNode) -> bool:
        """Returns True iff Unity component composition is compatible
        with this behaviour. MUST NOT inspect source bodies. Composition-
        only — Rigidbody/Collider/Animator/MonoBehaviour field shapes
        from the parsed scene data. Tested with composition-only
        fixtures (empty C# source)."""
    def confirm(self, node: SceneNode, source_csharp: str) -> bool:
        """Always called when primary() returns True. Substring/source
        inspection lives here, named and scoped per detector. Acts as a
        REJECTOR — returning False rules the detector out even if
        primary() said yes. Cannot upgrade an Infeasible primary() to
        Feasible (the API doesn't allow it)."""
    def behavior(self, node: SceneNode, source_csharp: str) -> Behavior:
        """Build the per-instance Behavior. Receives source for VALUE
        extraction (default literals from script body), NOT for
        classification."""
```

Codex v3 raised a sharper concern about the earlier "tiebreaker on
ambiguity only" form: if one detector's `primary()` is too broad, it
wins outright with no source disambiguation. Solution: rename to
`confirm()` and run it ALWAYS when `primary()` returns True. Two-layer
classification:

  1. **Feasibility gate (`primary()`):** composition-only. Catches the
     clear non-matches (no Rigidbody → not a projectile).
  2. **Confirmation (`confirm()`):** source-aware. Can reject a
     primary-match by detecting a counter-signal (e.g. the script's
     `OnCollisionEnter` calls `Rigidbody.AddExplosionForce` rather than
     `Destroy(gameObject)` → it's a damage receiver, not a projectile).

Both layers can vote No. Neither can override the other to Yes. A
detector that wanted to classify on substring alone would have to lie
in `primary()` — caught by contract tests that pass empty C# source.

Multi-detector resolution: if two detectors both pass `primary()` AND
`confirm()`, the classifier raises `AmbiguousDetectionError` with both
candidate names. Resolution is operator-driven via deny-list — no
silent "pick the first match" heuristic.

False positives are reported in `conversion_report.json` and rejectable via
an output-dir deny-list (`<output>/.gameplay_deny.txt` — one
`unity_file_id` per line).

### One runtime library per capability family

`converter/runtime/gameplay/`:

  - `movement.luau` — `Movement.applyImpulse(part, direction_local, force_unity)`
    handles stud-space conversion and anti-gravity VectorForce.
  - `lifetime.luau` — `Lifetime.scheduleDespawn(container, seconds)`,
    `Lifetime.consumeOnFirstHit(container, hitDetection)`.
  - `hit_detection.luau` — `HitDetection.raycastSegment(container, onHit)`,
    `HitDetection.overlapSphere(origin, radius)`.
  - `effects.luau` — `Effects.damage(target, value)`,
    `Effects.splash(origin, radius, value)`,
    `Effects.spawnTemplate(name, cframe)`.
  - `triggers.luau` — `Triggers.onBoolAttribute(target, name, ctx)`
    (publishes normalized bool to `ctx.trigger.value` on bind + every
    change), `Triggers.onPickup(zone, fn)`.
  - `damage_protocol.luau` — owns the RemoteEvent end-to-end (client fire,
    server receive, validation, attribute mirror). Client and server halves
    in one file; protocol drift is structurally impossible. The damage
    attribute is the boundary between Effect and listener; PR #72 doesn't
    define a client-side hitscan protocol (codex flagged the previous
    `DamageInteractionSpec` as FPS-flavoured — handled in a follow-up PR
    after we see real call sites).

A composer (`converter/runtime/gameplay/composer.luau`) wires capabilities
together at runtime. Given a behaviour list, it threads the per-family
`ctx` table through each capability in tuple order. The composer is small
(~80 lines including the family-namespace setup) because the capability
vocabulary is small. See "Capability dataflow contract" above for the
specifics — including how composition errors are caught at emit time, not
runtime, and why namespacing prevents the local-variable collision problem
that a flat shared-state approach would have.

### Emit: per-instance behaviour table

Per Unity scene node that matches at detection time, the converter emits a
behaviour table and a short require-and-run stub:

```lua
-- TurretBullet.luau (converter-emitted; AI is skipped for matched behaviours)
local Composer = require(game:GetService("ReplicatedStorage"):WaitForChild("AutoGen"):WaitForChild("Composer"))
Composer.run(script.Parent, {
    {kind = "movement.impulse", direction = Vector3.new(0, 0, 1), force = 60},
    {kind = "lifetime.despawn", seconds = 3},
    {kind = "hit_detection.raycast_segment"},
    {kind = "effect.damage", value = 10},
})
```

```lua
-- Door.luau (per-instance — two Door prefabs with different offsets get
-- two different tables)
local Composer = require(game:GetService("ReplicatedStorage"):WaitForChild("AutoGen"):WaitForChild("Composer"))
Composer.run(script.Parent, {
    {kind = "trigger.on_bool_attribute", name = "open"},
    {kind = "movement.attribute_driven_tween",
     target_offset = Vector3.new(0, 4, 0),
     open_duration = 1.0,
     close_duration = 1.0},
    {kind = "lifetime.persistent"},
})
```

The behaviour table is **per-instance data**, not class-level. Two doors
with different open offsets get two different tables emitted. No
`weapon_class` or `target_child_name` class-level leakage.

The composer reads `kind` and dispatches to the right runtime function;
tuple order is preserved AND enforced against the capability dataflow
contract (see above) so a `hit_detection.raycast_segment` that reads
`ctx.movement.velocity` cannot be placed before a `movement.impulse`
that writes it. The composer is the only runtime entrypoint script
consumers need to know about.

### Pipeline integration

`code_transpiler.transpile_scripts` gains a pre-AI classification step:

  1. For each Unity script class, run the gameplay detectors against the
     scene-node bindings that reference that class.
  2. For each scene node where detection returns a `Behavior`, emit the
     per-instance stub directly (skip AI for that node).
  3. For nodes that don't match, proceed with the existing AI path
     (`api_mappings.py` + `luau-analyze` reprompt loop).

The same `.cs` class can yield different stubs at different scene nodes if
the prefab overrides (force, damage) differ — the detector reads per-node
attributes.

### Genre neutrality

The patterns we're translating aren't FPS-specific despite the test project
being SimpleFPS:

| Capability | Genre coverage |
|---|---|
| `Movement.Impulse` | FPS bullets, RPG fireballs, RTS missiles, action grenades |
| `Trigger.OnBoolAttribute` | Doors, gates, elevators, traps |
| `HitDetection.OverlapSphere` | Splash damage, AoE spells, proximity sensors |
| `Trigger.OnPickup` | Items, ammo, keys, power-ups |

Capabilities and runtime libraries live under `converter/gameplay/` and
`runtime/gameplay/` — no "AutoFps" prefix on anything that isn't actually
FPS-specific. What stays in `converter/scaffolding/fps.py`: the genuinely
FPS-coded parts (HUD ScreenGui, `MouseBehavior.LockCenter`, WeaponSlot
attached to camera, first-person body-hide). Those are FPS-input/UX
conventions, not Unity patterns.

## Feature flag and migration

`--use-gameplay-adapters` CLI flag. Default off in PR #73c (and before),
default on in PR #74. Eight-PR migration (originally proposed as six;
codex pushback on PR #72 sharpened the slicing and added two cuts):

  - **PR #72 (this):** design doc only.

  - **PR #73a:** framework + door vertical slice.
    - CLI flag (default off).
    - `Behavior` IR, capability dataclasses, ClassVar `READS`/`WRITES`,
      emit-time validator (single-writer-per-key, reader-after-writer).
    - Detector Protocol (`primary()` / `confirm()` / `behavior()`) +
      `AmbiguousDetectionError`. Contract test that empty C# source
      doesn't change `primary()` results.
    - Composer (Lua) + family registry + `_GameplayBound` marker.
    - Door slice end-to-end: `Trigger.OnBoolAttribute`,
      `Movement.AttributeDrivenTween`, `Lifetime.Persistent`.
    - Deny-list plumbing (`<output>/.gameplay_deny.txt`).
    - Cross-family contract tests — the door alone exercises
      Trigger→Movement handoff via `ctx.trigger.value`.

  - **PR #73b:** projectile vertical slice.
    - `Movement.Impulse`, `Lifetime.Despawn`,
      `HitDetection.RaycastSegment`, `HitDetection.OverlapSphere`,
      `Effect.Damage`, `Effect.Splash`, `Effect.SpawnTemplate`.
    - Detectors for TurretBullet and PlaneBullet (both Unity-source
      shapes in SimpleFPS).
    - End-to-end smoke: SimpleFPS bullet behaviour matches the legacy
      pack output side-by-side (regression diff on rbxlx).

  - **PR #73c:** damage routing + cross-project smokes.
    - `damage_protocol.luau` — client and server halves in one file.
      Value-preserving (server mirrors client's payload verbatim,
      coerces non-scalar to `true`), camera-origin replay, distance
      gate, `AmbiguousDetectionError` on multi-match.
    - Cross-project smoke matrix: SimpleFPS + Gamekit3D + ChopChop.
      Each project's detectors run; the matches table is golden-tested
      so detector regressions on non-FPS projects fail CI.

  - **PR #74:** flip default ON.
    - `--use-gameplay-adapters` defaults true.
    - `--legacy-gameplay-packs` opt-out flag.
    - The two modes are **mutually exclusive** at pipeline level. The
      pipeline asserts at startup: if `--legacy-gameplay-packs` is set,
      adapter detection is disabled entirely and adapter runtime
      modules are NOT emitted. If `--use-gameplay-adapters` is on,
      legacy coherence packs are disabled entirely. Codex pushback on
      PR #72 flagged that running both produces double-binding (legacy
      mutates the transpiled body, adapter emits a new stub — they fight
      over the same scene parts).
    - Rehydration-aware prune pass: on re-conversion of an output that
      contains legacy artifacts (`_AutoFpsDoorTweenInjected` marker,
      `_AutoDamageEventRouter` script, `_AutoFpsHud*` GuiObjects), the
      pipeline removes them BEFORE adapters emit their replacements,
      regardless of which mode produced the artifacts. Prevents stale
      half-state from poisoning the new path.
    - Codifies the soak-and-delete exit criteria for PR #76 in a new
      `docs/design/gameplay-adapters-rollout.md` companion doc.

  - **PR #75:** introduce `EventDispatch` runtime module under
    `runtime/gameplay/event_dispatch.luau`, parented at runtime to a
    converter-owned `ReplicatedStorage.AutoGen` Folder (NOT directly
    under `ReplicatedStorage` where user-authored `EventDispatch.cs`
    transpilations would land). Add an Instance-level alias: at
    pipeline emit time, the converter ALSO drops a same-named
    `ModuleScript` at the original `ReplicatedStorage.AutoFpsEventDispatch`
    location.

    The alias body uses `WaitForChild` chains (NOT direct dot-chains)
    so early `require()` callers don't race against load order:
    ```lua
    -- ReplicatedStorage.AutoFpsEventDispatch (alias, emitted by PR #75)
    return require(
        game:GetService("ReplicatedStorage")
            :WaitForChild("AutoGen")
            :WaitForChild("EventDispatch")
    )
    ```

    Overwrite policy at emit time: if a non-ModuleScript Instance
    already exists at `ReplicatedStorage.AutoFpsEventDispatch` (a
    user's Folder, a stale Script, a leftover BindableEvent from
    a hand-edit), the converter logs a warning and skips the alias
    emission for that output — the user's content wins. If an
    existing ModuleScript is present, it's overwritten in place
    (overwriting our own alias from a prior run is the common
    case; overwriting a user-authored module is the same risk we
    already accept across the converter's other auto-injected
    modules and is documented in the conversion report).

    Already-converted outputs that call
    `WaitForChild("AutoFpsEventDispatch")` from their script bodies
    keep resolving. The user-script collision risk codex flagged is
    avoided because the new canonical name lives under `AutoGen/`,
    not directly under RS.

  - **PR #76:** delete the three coherence packs. Exit criteria
    codified in `docs/design/gameplay-adapters-rollout.md` (added
    in #74), enforced via CI check that fails if criteria aren't
    documented as met:
      1. Adapters default-on for ≥ 2 consecutive converter releases.
      2. Zero P1/P2 gameplay regressions reported on the three target
         patterns across the soak window.
      3. Cross-project smoke matrix (SimpleFPS / Gamekit3D / ChopChop)
         green at PR #74 merge AND at the end of the soak window.
      4. `--legacy-gameplay-packs` rollback usage near-zero during
         soak (tracked manually; no telemetry pipeline assumed).
    `--legacy-gameplay-packs` flag becomes a hard error pointing at
    the relevant CHANGELOG entry. Compat shim from PR #75 stays.

  - **PR #77:** remove `--use-gameplay-adapters` flag. Legacy packs
    are already gone (since #76) so the flag has no off-state to
    switch to. Pure cleanup. Compat shim from PR #75 stays.

  - **PR #78:** remove the `AutoFpsEventDispatch` Instance alias from
    PR #75, after at least one full reconversion cycle confirms no
    converted output still references the old name in `WaitForChild`.
    Separate from #77 because flag-removal and shim-removal are
    independent rollback levers — collapsing them prevents partial
    rollback if either turns out to be premature.

Codex review on the v6 plan pushed back specifically on:
  - PR #73 being too big as a single PR (now split into 73a/b/c).
  - Mode coexistence (now mutually exclusive at pipeline level).
  - `AutoFpsEventDispatch` rename being unsafe without an Instance-
    level alias (added in PR #75).
  - "Soaked in production" being too vague (now codified exit
    criteria in PR #74).
  - PR #77 collapsing flag-removal + shim-removal (now split into
    PR #77 and PR #78).

Each PR is reviewable independently and rolls back independently. The
8-PR plan looks longer than the original 6-PR plan but each cut is
smaller and the rollback levers stay distinct.

## Rejected alternatives

  - **Keep coherence packs, just make the regexes better.** Twelve rounds
    of codex review say no. Each fix uncovers a new edge because the
    matching surface is "AI-transpiled Luau text" and that's unbounded.
  - **One mega-runtime module that scripts require.** Collapses every
    capability into a single Lua file with N unrelated public APIs. The
    per-family split lets each runtime library be tested and versioned
    independently.
  - **Replace AI transpilation entirely with canonical bodies for ALL
    Unity scripts.** Out of scope. AI keeps clear value for Unity scripts
    that don't match a recognised pattern (Player.cs's full input handling,
    custom MonoBehaviours specific to the game). The adapter system is
    opt-in per detected behaviour.
  - **Three monolithic specs (`ProjectileSpec`, `ActuatorSpec`,
    `DamageInteractionSpec`).** Codex review pushed back: overloaded,
    didn't cover sensor/trigger interactions, smuggled class-level
    assumptions (`weapon_class`, `target_child_name`). Replaced with the
    capability composition above.

## Risks and mitigations

  - **Capability mis-detection.** A non-projectile Rigidbody that happens
    to have AddForce somewhere could match `Movement.Impulse`. Mitigation:
    detectors emit their decision into `conversion_report.json`; users can
    disable per-class via deny-list (`.gameplay_deny.txt`). Tests run all
    9 test projects through the detectors and pin expected matches —
    regression on Gamekit3D / ChopChop / etc. fails CI.
  - **Runtime-library regression.** Centralising e.g. all projectile logic
    in `movement.luau` means a bug there breaks every projectile. Each
    runtime library is small (~100 lines) and unit-testable end-to-end
    with stubbed `workspace` / `RunService`. Golden-file tests pin the
    expected emit for canonical Unity inputs.
  - **Capability vocabulary growth.** The list above is enough to cover
    the SimpleFPS + Gamekit3D + ChopChop patterns we know about. New
    Unity patterns extend by adding ONE variant to ONE family (e.g.
    `Movement.OrbitAround(point)` for spell projectiles). Adding a fifth
    family would be a flag that we got the partition wrong; PR #73's
    cross-project smoke tests will catch this early.
  - **Migration cost.** Six PRs sounds like a lot. The reason is each PR
    is small (~200 line changes) and individually reviewable. The total
    code surface deleted (~600 lines coherence packs + ~30 tests) is
    larger than the added composer + capability libraries (~500 lines)
    and the conceptual surface shrinks dramatically (one mental model:
    detector classifies → behaviour table → composer runs).
