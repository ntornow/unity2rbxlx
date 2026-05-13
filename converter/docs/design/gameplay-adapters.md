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

**`Effect.Damage` runtime contract** (added to the doc in PR #73c —
codex PR #73b round-1 [P1] flagged that both invariants below are
load-bearing and were previously implicit). Binding on implementers,
tested in `tests/test_gameplay_adapters_projectile.py::TestDamagePlayerFilter`:

  - **Player-tag gate.** ``Effects.damage`` applies damage ONLY when
    the hit Instance's ancestor model is recognised as a Player.
    Three-way check: (a) `Players:GetPlayerFromCharacter(model)`
    resolves to a real Player; OR (b) the hit Instance carries the
    `"Player"` CollectionService tag; OR (c) the model is named
    `"Player"` (test scaffolding + the auto-injected FPS character).
    Any one match means damage applies. Mirrors Unity
    `TurretBullet.cs`'s `other.collider.tag == "Player"` filter —
    NPCs / allied Humanoids MUST NOT take damage from a TurretBullet,
    or any project with friendly characters regresses. The check is
    inside `Effects.damage`'s impact subscription, indent-scoped so
    a future refactor that wraps the damage call in a different
    conditional fails the test gate.
  - **Despawn-on-any-impact.** The bullet container is destroyed on
    every impact — Player damage, non-Player Humanoid, OR wall hit.
    Without this, raycast bullets persist past their first hit and
    can damage multiple targets in a single frame, or worse, fly on
    forever as ghost particles. The destroy call sits OUTSIDE the
    `if isPlayer` branch so wall hits also clean up the container.
    `Effect.Splash` and `Effect.SpawnTemplate` follow the same
    pattern; whichever Effect runs LAST in the tuple owns the
    `container:Destroy` (validator-enforced ordering ensures exactly
    one destroyer).
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
itself separates the two layers AND binds detection to a SPECIFIC
component (not the whole node — see "Detector input is a (node,
component) pair" below):

```python
class Detector(Protocol):
    name: str

    def primary(self, node: SceneNode, component: ComponentData) -> bool:
        """Returns True iff *component*'s Unity composition is compatible
        with this behaviour. MUST NOT inspect source bodies. Composition-
        only — looks at component.component_type and the resolved
        ``_script_class_name`` field populated by guid_index. Tested with
        composition-only fixtures (empty C# source)."""
    def confirm(
        self, node: SceneNode, component: ComponentData, source_csharp: str,
    ) -> bool:
        """Always called when primary() returns True. Source is the C#
        body of THIS component's class only. Acts as a REJECTOR —
        returning False rules the detector out even if primary() said
        yes. Cannot upgrade an Infeasible primary() to Feasible (the
        API doesn't allow it)."""
    def behavior(
        self, node: SceneNode, component: ComponentData, source_csharp: str,
    ) -> Behavior:
        """Build the per-instance Behavior. Receives source for VALUE
        extraction (default literals from script body), NOT for
        classification."""
```

**Detector input is a (node, component) pair, not just a node.** Codex
PR #73a-round-1 caught a bug in an earlier draft of this section: if a
node carries TWO MonoBehaviours (Door + an unrelated user script), and
the dispatcher iterated component sources while running `primary(node)`
once per source, the unrelated script's body could `confirm` for the
Door detector because `primary` voted yes (Door IS on the node) and
`confirm`'s regex happens to match. Binding the protocol to a specific
component closes that hole: `primary` checks the component's own class
name; `confirm` reads only that component's class source. The dispatch
in `converter.gameplay.detectors.detect` is the single enforcement
point — every call site goes through it.

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
an output-dir deny-list (`<output>/.gameplay_deny.txt` — one ID per
line; either a scene-node `unity_file_id` OR a component file_id is
accepted, so an operator can suppress one MonoBehaviour on a multi-
component node without losing detection on the others).

**Deny-list entry forms (PR #73b — qualified form added in PR #73c
doc-update):**

  - **Bare `<file_id>`** — matches across every source that carries
    that ID. The original PR #73a form. Still supported for backward
    compatibility, but ambiguous after PR #73b's prefab walk.
  - **Qualified `<source_path>#<file_id>`** — matches the file_id only
    when it appears under the given absolute source path. Required
    when two prefab assets share a local file_id (Unity's local IDs
    are not globally unique across `.prefab` files; PR #73b prefab
    walking made `&100000` a routine collision in SimpleFPS).

A multi-match `AmbiguousDetectionError` log line surfaces all three
deny-list forms an operator can paste — bare node_file_id, bare
component_file_id, and the qualified `<source_path>#<id>` shape — so
suppressing one specific prefab doesn't require an extra round trip
to find the source path. Both forms can coexist in a single
`.gameplay_deny.txt`; whichever matches first wins. Lines beginning
with `#` are comments. The conversion
report's `gameplay_adapters.bindings[]` entries carry `node_name`,
`node_file_id`, `component_file_id`, `target_class_name`,
`script_path` (absolute on disk; "relative" is a misnomer in #73a —
the field name is `script_path` for that reason), `detector_name`,
`diagnostic_name`, and `capability_kinds[]` (preserved in capability
tuple order — see the dataflow contract) so an operator can write
a deny-list line straight from the report without re-reading the
converter. Divergent classes (per-instance shape mismatch) appear in
`gameplay_adapters.divergent_classes[]` with `class_name`,
`script_path`, `detail`, and structured `binding_a` / `binding_b`
records (each carrying `node_name`, `node_file_id`,
`component_file_id`) so the operator can deny-list either side
precisely. Top-level counters: `total_classes_emitted` (unique by
`script_path`), `total_classes_divergent`, and `total_bindings`.

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
  - `damage_protocol.luau` — owns `ReplicatedStorage.DamageEvent`
    end-to-end (client fire, server receive, validation, attribute
    mirror). Client and server halves in one file so protocol drift is
    structurally impossible. Server validation pipeline: type guards →
    origin drift gate (`MAX_ORIGIN_DRIFT_STUDS = 20`) → raycast replay
    from the client-supplied camera origin + dir → distance gate via
    `MAX_SHOOT_RANGE_STUDS = 100m·3.571·1.5` → value-preserving
    attribute mirror (non-scalar payloads coerce to `true` so a
    malicious table can't crash `SetAttribute`). Client half is
    `DamageProtocol.fire(hitInstance, takeDamageValue)` — reads
    `workspace.CurrentCamera.CFrame` and FireServers the four-arg
    payload. The damage attribute remains the boundary between Effect
    and listener.

    **Idempotent server init** is part of the public contract: an
    internal `_serverInitialized` flag short-circuits a second
    `_initServer()` call so a double-load (the always-on bootstrap
    Script + a workspace-placed adapter stub triggering the same
    `require()`) doesn't double-bind `OnServerEvent`. Test-only
    `_resetForTest()` hook re-arms the flag.

    **Canonical name collision posture**: if a non-RemoteEvent
    Instance already sits at `ReplicatedStorage.DamageEvent` at init
    time (user-authored Folder, leftover BindableEvent, etc.), the
    bootstrap renames it to `DamageEvent_displaced` and emits a fresh
    RemoteEvent at the canonical name. Same posture the legacy
    `_AutoDamageEventRouter` took before the migration; protects
    `FireServer` callers from crashing on a type mismatch.

A composer (`converter/runtime/gameplay/composer.luau`) wires capabilities
together at runtime. Given a behaviour list, it threads the per-family
`ctx` table through each capability in tuple order. The composer is small
(~80 lines including the family-namespace setup) because the capability
vocabulary is small. See "Capability dataflow contract" above for the
specifics — including how composition errors are caught at emit time, not
runtime, and why namespacing prevents the local-variable collision problem
that a flat shared-state approach would have.

### Always-on server bootstrap

(Added PR #73c after codex round-1 [P1] on damage routing.)

Every family runtime module — including `damage_protocol.luau` — is a
ModuleScript parented to `ReplicatedStorage.AutoGen`. ModuleScripts in
ReplicatedStorage **do not auto-run**; they execute only when
something else `require()`s them. Per-instance adapter stubs DO
require `Gameplay` (the orchestrator that pulls in every family,
including DamageProtocol), but those stubs are typically attached to
prefab template parts under `ReplicatedStorage.Templates` — and
template-scoped scripts don't run until the first runtime clone
spawns into Workspace. SimpleFPS is the canonical case: TurretBullet,
PlaneBullet, and SciFi_Door MonoBehaviours all live on prefab roots,
so the entire Gameplay orchestrator wouldn't load until the first
bullet was instantiated.

That timing gap is fatal for any family that needs to be **listening
at server start**, not just available when a stub fires. Damage
routing is the prototypical case: a Player LocalScript firing
`DamageEvent` on its first click (before any bullet has spawned)
would hit a nil RemoteEvent and silently drop the shot. The legacy
`_AutoDamageEventRouter` was an always-on `Script` in
`ServerScriptService` precisely for this reason — moving the
validator logic into a ModuleScript without preserving the always-on
posture is a regression.

Fix: `runtime/gameplay/server_bootstrap.luau` — a `Script` (not
ModuleScript) parented to `ServerScriptService` at emit time.
`WaitForChild("AutoGen", 30) → WaitForChild("Gameplay", 30) →
require(...)`. Pipeline injection lives next to the existing
ReplicatedStorage gameplay-modules block, gated on the same
`adapter_stubs_present` predicate.

**When to add a new family to the bootstrap path.** Only when the
family needs server-side state attached at game start, independent of
any specific adapter binding. Common case: owning a RemoteEvent /
RemoteFunction the AI-transpiled scripts call into. Counter-case: a
family that only does work *during* a `Composer.run` call (Movement,
Lifetime, HitDetection, Effects) needs no bootstrap — its handler
fires when the per-instance stub fires, which is the right time. The
orchestrator already force-requires every family, so adding a new
family to the bootstrap path is automatic IF the family's `require()`
side-effect performs the server-side init. DamageProtocol's
`_initServer()` is the reference pattern: idempotent, `RunService:
IsServer()`-gated, runs once at first require.

### Emit: behaviour table

The IR is per-instance — `Behavior` carries a `unity_file_id`. PR #73a
ships a per-class emitter with an equivalence check (see
"Per-instance vs per-class emission" below); PR #73b extends to
per-instance emission once a real divergent case shows up. The Lua
form below is what the emitter writes for the matched class:

```lua
-- TurretBullet.luau (converter-emitted; AI is skipped for matched behaviours)
local Gameplay = require(game:GetService("ReplicatedStorage"):WaitForChild("AutoGen"):WaitForChild("Gameplay"))
local _container = script.Parent
Gameplay.run(_container, {
    {kind = "movement.impulse", direction = Vector3.new(0, 0, 1), force = 60},
    {kind = "lifetime.despawn", seconds = 3},
    {kind = "hit_detection.raycast_segment"},
    {kind = "effect.damage", value = 10},
})
```

```lua
-- Door.luau (per-instance — two Door prefabs with different offsets get
-- two different tables; the IIFE locates the moving door mesh from a
-- sibling lookup with a bounded 5s wait)
local Gameplay = require(game:GetService("ReplicatedStorage"):WaitForChild("AutoGen"):WaitForChild("Gameplay"))
local _container = (function()
    local _ascended = script.Parent.Parent or script.Parent
    local _child = _ascended:WaitForChild("door", 5)
    if _child == nil then
        warn(string.format(
            "[gameplay-adapter] container child %q missing under %s — adapter not bound",
            "door", _ascended:GetFullName()
        ))
    end
    return _child
end)()
if _container == nil then
    warn("[gameplay-adapter] Door: container resolution returned nil, adapter not bound")
    return
end
Gameplay.run(_container, {
    {kind = "trigger.on_bool_attribute", name = "open"},
    {kind = "movement.attribute_driven_tween",
     target_offset_unity = Vector3.new(0, 4, 0),
     open_duration = 1.0,
     close_duration = 1.0},
    {kind = "lifetime.persistent"},
})
```

Per-instance stubs require `AutoGen.Gameplay`, NOT `AutoGen.Composer`
directly. `Gameplay` is an orchestrator ModuleScript that force-
requires every family module (`Triggers`, `Movement`, `Lifetime`, ...)
before re-exporting `Composer`. Stubs that hit `Composer` directly
would race against family-registration order; the orchestrator dodge
also avoids the cyclic-require pitfall (families require `Composer`,
so `Composer` itself cannot require families — only the orchestrator
can.)

**Container resolution.** The `ContainerResolver` typed enum on
`Behavior` (`self` / `ascend_then_child`) carries the per-instance
metadata for locating the bind target. The `ascend_then_child` variant
ALWAYS emits a bounded `:WaitForChild(name, 5)` plus a warn-and-return-
nil path — codex PR #73a-round-1 flagged that an unbounded wait would
deadlock the script forever on prefab drift, where the legacy door
pack at least degraded to a no-op. The call site guards against nil
before dispatching so a missing container doesn't poison `Gameplay.run`.

**Per-instance vs per-class emission (PR #73a binding model).** The IR
is per-instance by construction (`Behavior.unity_file_id` is the scene
node it binds to). PR #73a emits ONE TranspiledScript per matched C#
class plus an equivalence check that rejects divergent per-instance
shapes:

  - Within a single class, every per-node `Behavior` must share the
    same shape (`(diagnostic_name, capabilities, container_resolver)`
    — `unity_file_id` is intentionally excluded).
  - If the shapes diverge, that class drops out of
    `ClassificationResult.matches` and lands in
    `ClassificationResult.divergent` (a tuple of
    `DivergentClassRecord(class_name, script_path, error)`). The
    pipeline reads `divergent` for the report; the matched class falls
    through to the AI path because its `.cs` path was never removed
    from `script_infos`. Per-instance distinct emission is a PR #73b
    refinement — `classify_scripts` does NOT raise, and one divergent
    class never disturbs another class's emission in the same pass.

This keeps PR #73a small while preventing silent coalescing of distinct
prefab variants. The conversion report still records every per-node
binding so an operator can see all instances even when the emit is
per-class.

The behaviour table is **per-instance data**, not class-level — the
`Behavior` IR carries `unity_file_id` and per-resolver metadata for
each node. PR #73a emits ONE table per matched class plus an
equivalence gate; two doors with different open offsets would land in
`ClassificationResult.divergent` and fall back to AI today. PR #73b
relaxes that gate by emitting one stub per scene node so genuine
prefab-variant divergence is supported in-pipeline. Either way: no
`weapon_class` or `target_child_name` class-level leakage in the IR.

The composer reads `kind` and dispatches to the right runtime function;
tuple order is preserved AND enforced against the capability dataflow
contract (see above) so a `hit_detection.raycast_segment` that reads
`ctx.movement.velocity` cannot be placed before a `movement.impulse`
that writes it. The composer is the only runtime entrypoint script
consumers need to know about.

### Cross-family signal handoff is on the public contract

`Trigger.OnBoolAttribute` publishes two ctx slots, both declared in
its `WRITES` set:

  - `ctx.trigger.value` — the current normalized bool. Movement /
    Effect read this synchronously to make decisions.
  - `ctx.trigger.changed` — a BindableEvent fired AFTER each value
    update. Downstream capabilities subscribe to it instead of
    reaching back to the underlying Roblox attribute.

The signal slot is part of the public dataflow contract, not an
implementation backchannel. Codex PR #73a-round-1 pushed back that
declaring a BindableEvent as just another string key understates the
runtime coupling (BindableEvents carry type and lifetime semantics the
validator can't see). The compromise for #73a is to surface signals
explicitly in `WRITES` so the validator at least catches reader-after-
writer ordering on signal subscribers. PR #73b will revisit whether
signals should become a first-class capability output (typed
`subscribe()` interface) rather than smuggling them through ctx.

### Legacy pack mutex — three tiers

(Added PR #73c. PR #73a introduced full-disable mutex for one pack;
PR #73c added the partial-disable tier and made the model explicit.)

The pre-existing `script_coherence_packs.py` packs and the new
gameplay adapters can BOTH mutate the converter output in overlapping
ways. Coexistence has to be policed pack-by-pack, not globally —
codex pushback on PR #72 flagged that "let both run" produces double-
binding (e.g. legacy door pack mutates the AI-transpiled body to
animate a sliding mesh, AND the adapter emits a Composer.run stub on
the same prefab → two tween paths fight over the same `CFrame`).

Three tiers depending on how much of the legacy pack the adapter
fully replaces:

  1. **Full disable** — entire pack is skipped when adapters are on.
     Use when the adapter's emission completely supersedes the pack's
     output. Implementation: the pack name lives in the
     `LEGACY_PACKS_DISABLED_WHEN_ADAPTERS_ON` frozenset in
     `pipeline.py`, which `transpile_scripts` forwards to
     `script_coherence_packs.run_packs` as `disabled`. Members:
     `door_tween_open` (PR #73a — adapter owns the door slice end-
     to-end), `bullet_physics_raycast` (PR #73b — adapter owns the
     bullet behaviour).

  2. **Partial disable via runtime marker scan** — pack still runs
     but skips ONE of its responsibilities when adapter stubs are
     present. Use when the pack does two distinct things and the
     adapter only replaces one of them. Implementation: the pack's
     apply function scans `scripts` for `ADAPTER_STUB_MARKER` and
     short-circuits the now-redundant half. Member:
     `player_damage_remote_event` (PR #73c — adapter's
     `damage_protocol.luau` replaces the `_AutoDamageEventRouter`
     Script, but the pack's body-patch half still has to run because
     AI-transpiled Player LocalScripts still need their inline
     `FireServer` call wired up).

  3. **No mutex needed** — packs whose work doesn't overlap with any
     adapter family. Default posture; no listing required. The
     adapter system is opt-in per detected behaviour, so most legacy
     packs (FPS rifle pickup, animator coercion, etc.) keep running
     unchanged.

**Stale-artifact pruning** (tier 2 supplemental). The partial-
disable pack ALSO removes any stale artifact from a prior adapters-
off conversion at apply time. Example: a pre-existing
`_AutoDamageEventRouter` Script left on disk by a previous adapters-
off run would double-bind `OnServerEvent` against the new
DamageProtocol if both ran. The pack's `detect()` widens to fire
when adapters are active AND a legacy router exists (so the apply
path runs and prunes); the apply function removes the script in
place. PR #74's broader rehydration-aware prune pass covers the rest
of the legacy-artifact surface (`_AutoFpsDoorTweenInjected` marker,
HUD GuiObjects); PR #73c handles only the damage-router slice
because that's the one a partial-disable pack already touches.

**Why partial disable beats splitting the pack into two packs**:
splitting requires duplicating the `detect()` signal (Player.cs body
plus router source) across two packs, then orchestrating their
execution order. The marker scan is a single side-effecting check
inside the existing apply function. Future tier-2 packs should
follow the same pattern: keep the pack atomic, gate the half-that-
adapters-replace on `ADAPTER_STUB_MARKER in s.source for s in scripts`.

### Pipeline integration

`Pipeline.transpile_scripts` runs the classification step BEFORE the
AI pass (the post-AI variant in an earlier #73a draft was reverted —
codex PR #73a-round-1 flagged that overwriting AI output silently
dropped matches whenever AI failed for the class):

  1. Walk `parsed_scene.all_nodes` **AND every prefab template root**
     in `prefab_library.prefabs[].root`. For each `(node, component)`
     pair where `component` is a MonoBehaviour with a resolvable script
     class, run the detectors via
     `gameplay.integration.classify_scripts`. Every binding carries the
     absolute `source_path` of the scene or prefab it came from so
     deny-list checks (and operator reports) can disambiguate across
     prefab assets that share local file_ids.

     **Prefab traversal rationale** (added to the doc in PR #73c —
     this load-bearing detail was missing from the #73a/#73b drafts).
     Many Unity behaviours (doors, bullets, pickups) live exclusively
     on `.prefab` roots and are instantiated at runtime by other
     scripts (`Turret.cs` does `Instantiate(bulletPrefab)`); the
     `main.unity` scene file itself carries no MonoBehaviour for them.
     Without the prefab walk, neither the door slice (PR #73a) nor the
     projectile slice (PR #73b) would fire end-to-end on a real
     SimpleFPS conversion — the matched `.luau` is attached to the
     prefab template part in `ReplicatedStorage.Templates.<name>`, so
     every runtime clone (`Instantiate` → `Clone()` in Roblox)
     inherits the adapter stub. PR #73b's prefab-walk extension
     retroactively made PR #73a's door slice actually fire too —
     SciFi_Door's MonoBehaviour also lives on the prefab template,
     not the scene root.
  2. Aggregate per-class. Equivalence-check every per-node `Behavior`
     within a class (see "Per-instance vs per-class emission" above);
     divergent classes drop out of `matches` and land in `divergent`.
     Pipeline reads both buckets; it never catches an exception from
     `classify_scripts`.
  3. Build first-class `TranspiledScript` records via
     `gameplay.integration.adapter_transpiled_scripts` and REMOVE
     matched classes from the `script_infos` list fed to the AI
     transpiler — no AI tokens are spent on matched classes.
  4. Run the existing AI path on the remaining `script_infos`
     (`api_mappings.py` + `luau-analyze` reprompt loop).
  5. Merge adapter TranspiledScripts onto the AI result. Adapter
     scripts count under `TranspilationResult.total_gameplay_adapter`,
     NOT `total_rule_based` — the strategies are distinct and the
     conversion report's `scripts.counts` exposes both buckets so
     downstream phases see honest accounting.

The pre-AI shape means a STABLE detector match (one that survives the
per-class equivalence gate) is guaranteed to produce an adapter
artifact — AI failure can no longer silently drop it. Divergent
classes still fall back to AI, where AI failure is the only failure
mode and is surfaced via ``conversion_report.json``'s
``gameplay_adapters.divergent_classes[]``.

The same `.cs` class can yield different stubs at different scene nodes
once PR #73b enables per-instance emission; PR #73a's equivalence check
makes the silent-coalescing failure mode loud.

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
    - CLI flag (default off). Persisted on
      `ConversionContext.use_gameplay_adapters`; constructor snapshot
      survives the resume() ctx swap.
    - `Behavior` IR with `ContainerResolver` (`self` /
      `ascend_then_child`), capability dataclasses, ClassVar
      `READS`/`WRITES`, emit-time validator (single-writer-per-key,
      reader-after-writer, namespace check).
    - Detector Protocol with component-level binding
      (`primary(node, component)` / `confirm(node, component, source)` /
      `behavior(node, component, source)`) + `AmbiguousDetectionError`
      carrying both node and component file_ids. Contract test that
      empty C# source doesn't change `primary()` results.
    - Pre-AI classification pipeline: `classify_scripts` runs against
      the parsed scene BEFORE the AI transpiler; matched classes are
      removed from the AI input list and replaced with first-class
      `TranspiledScript`s. AI-failure no longer silently drops matches.
    - Per-class emission with per-instance equivalence check:
      `DivergentBehaviorsError` raises (and falls back to AI) when one
      class produces non-equivalent Behaviors across nodes.
    - Composer (Lua) + family registry + `_GameplayBound` marker, plus
      a `Gameplay` orchestrator ModuleScript that force-requires every
      family module to dodge the cyclic-require pitfall. Per-instance
      stubs require `AutoGen.Gameplay`, never `AutoGen.Composer`
      directly.
    - Bounded `WaitForChild` (5s) + warn-and-return-nil path in
      `ascend_then_child` resolver; call-site nil-guard before
      `Gameplay.run`. Prefab drift no longer deadlocks script init.
    - Door slice end-to-end: `Trigger.OnBoolAttribute`,
      `Movement.AttributeDrivenTween`, `Lifetime.Persistent`. Trigger
      publishes both `ctx.trigger.value` and `ctx.trigger.changed`
      (BindableEvent) on `WRITES`; Movement subscribes via the signal.
    - Mutual exclusion: `door_tween_open` legacy coherence pack is
      force-disabled whenever the flag is on. Intentional rollout
      posture — deny-listed nodes don't fall back to legacy.
    - Runtime modules emitted under `ReplicatedStorage.AutoGen.<Name>`
      via lazy Folder creation in `rbxlx_writer` (parent_path now
      supports dotted paths).
    - Deny-list plumbing (`<output>/.gameplay_deny.txt`): one ID per
      line, accepts either a scene-node file_id OR a component file_id
      so an operator can suppress one component on a multi-component
      node without losing the others.
    - Cross-family contract tests — the door alone exercises
      Trigger→Movement handoff via `ctx.trigger.value` and
      `ctx.trigger.changed`.

  - **PR #73b:** projectile vertical slice.
    - `Movement.Impulse`, `Lifetime.Despawn`,
      `HitDetection.RaycastSegment`, `HitDetection.OverlapSphere`,
      `Effect.Damage`, `Effect.Splash`, `Effect.SpawnTemplate`.
    - Detectors for TurretBullet and PlaneBullet (both Unity-source
      shapes in SimpleFPS).
    - End-to-end smoke: SimpleFPS bullet behaviour matches the legacy
      pack output side-by-side (regression diff on rbxlx).

  - **PR #73c:** damage routing + cross-project smokes + design-doc
    catch-up.
    - `damage_protocol.luau` — client and server halves in one file
      under `runtime/gameplay/`. Owns `ReplicatedStorage.DamageEvent`
      end-to-end: server-side `OnServerEvent` handler with type
      guards → origin drift gate (`MAX_ORIGIN_DRIFT_STUDS = 20`) →
      raycast replay from the client-supplied camera origin + dir →
      distance gate via `MAX_SHOOT_RANGE_STUDS = 100m·3.571·1.5` →
      value-preserving attribute mirror (non-scalar payloads coerce
      to `true` so a malicious table can't crash `SetAttribute`).
      Force-required by the `Gameplay` orchestrator; the legacy
      `player_damage_remote_event` coherence pack's body-patch half
      still runs (Player.cs LocalScripts still need their inline
      `FireServer` call), but the legacy `_AutoDamageEventRouter`
      Script is no longer emitted when adapter stubs are present, and
      any stale legacy router from a prior adapters-off conversion is
      removed so the two paths don't double-bind `OnServerEvent`.
    - `_GameplayServerBootstrap` Script parented to
      `ServerScriptService` (codex PR #73c-round-1 [P1]). All other
      adapter runtime modules are ModuleScripts under
      `ReplicatedStorage.AutoGen`; ReplicatedStorage scripts don't
      auto-run, and per-instance stubs that `require()` the Gameplay
      orchestrator typically live on prefab templates (the common
      SimpleFPS shape — TurretBullet / PlaneBullet / SciFi_Door all
      sit on prefab roots), so the orchestrator wouldn't load until
      the first runtime clone spawned. Without the bootstrap, a
      Player LocalScript firing `DamageEvent` on its first click
      (before any bullet has spawned) would hit a nil RemoteEvent.
      The legacy `_AutoDamageEventRouter` was an always-on
      ServerScriptService Script for the same reason; the bootstrap
      preserves that posture while keeping the validator logic in a
      ModuleScript. `DamageProtocol._initServer` is idempotent so a
      double-load (bootstrap + a workspace-placed adapter stub
      triggering the same `require`) doesn't double-bind
      `OnServerEvent`.
    - Cross-project smoke matrix: SimpleFPS + Gamekit3D + ChopChop.
      Two layers — real-source classification when the project tree
      is checked out (pins TurretBullet / PlaneBullet / Door for
      SimpleFPS; pins ZERO matches across Gamekit3D and ChopChop),
      plus always-on synthetic-fixture rejection tests so developer
      machines without the full `test_projects/` tree still catch
      detector regressions. A detector inventory pin
      (`ALL_DETECTORS` exact set) forces an explicit decision about
      cross-project coverage whenever a new detector ships.
    - Design-doc updates deferred from #73a/#73b: (a) prefab-template
      walk in `classify_scripts` (the load-bearing detail that made
      doors and bullets actually fire end-to-end on SimpleFPS); (b)
      qualified `<source_path>#<file_id>` deny-list form alongside
      the bare-file_id form (PR #73b made bare IDs ambiguous across
      prefab assets); (c) `Effect.Damage` runtime contract (Player-
      tag gate + despawn-on-any-impact). All three were already
      shipped in code in #73b — PR #73c brings the doc into sync.

  - **PR #74:** flip default ON. Eleven codex review rounds
    (rounds 1–11) hardened the rollback / resume / cross-feature-
    collision surfaces; the architectural decisions below carry
    forward into every subsequent PR in the migration.
    - **Default flip.** `ConversionContext.use_gameplay_adapters`
      defaults to `True`. `Pipeline.__init__`'s
      `use_gameplay_adapters` parameter is a **tri-state**
      (`bool | None`, codex round-1 [P1]):
        - `None` (default) — caller has no preference this run.
          Constructor leaves `ctx.use_gameplay_adapters` at its
          dataclass default for fresh ctx (`True`), and at the
          persisted value for a resumed ctx. **Resume preserves
          persisted state.**
        - `True` / `False` — explicit caller choice. Wins over
          persisted state both at construction and after
          `Pipeline.resume()`'s ctx swap.
      A hard `True` default would unconditionally overwrite a
      persisted `False` on `--phase` resumes, breaking sticky
      rollback for projects originally converted with
      `--legacy-gameplay-packs`. The CLI seam computes "was the
      user explicit?" via `click.get_parameter_source(...)` and
      forwards the corresponding bool or `None`.
    - **Mode-flip transpile-cache invalidation** (codex round-2
      [P1] + round-8 [P2]). When an explicit override flips the
      mode (e.g. `--legacy-gameplay-packs` against an originally
      adapters-on output), the cached `scripts/*.luau` carries the
      PREVIOUS mode's output and `_subphase_emit_scripts_to_disk`'s
      preserve-scripts path would silently keep it. The flip is
      detected at `Pipeline.resume()` AND
      `convert_interactive._make_pipeline()` post-ctx-swap;
      `Pipeline._invalidate_transpile_cache_for_mode_flip()` then
      (a) sets `self._retranspile = True` so the disk-cache wipe
      fires and (b) removes `"transpile_scripts"` from
      `ctx.completed_phases` so the phase actually re-runs.
      `convert_interactive` callers OR the caller's `--retranspile`
      flag with the invalidator-set `_retranspile` so the True
      survives — `pipeline._retranspile = retranspile or
      getattr(pipeline, "_retranspile", False)`.
    - **Pre-PR-#73a ctx → legacy** (codex round-3 [P1]).
      `ConversionContext.load()` checks the raw JSON dict before
      passing to `cls(**cleaned)`; when `use_gameplay_adapters` is
      missing from the file (i.e. the ctx predates PR #73a), the
      loader forces `False` instead of letting the new dataclass
      default fill in `True`. Preserves sticky rollback for
      pre-PR-#73a outputs that get resumed against PR-#74-or-later
      converters.
    - **`--legacy-gameplay-packs` opt-out flag** (default false).
      Click shape: `--use-gameplay-adapters/--no-use-gameplay-adapters`
      (default True) PLUS the new `--legacy-gameplay-packs` boolean
      flag, so an operator who wants the legacy pipeline writes
      `--legacy-gameplay-packs` alone and the CLI's mutex check
      flips adapters off without requiring the explicit
      `--no-use-gameplay-adapters`. **Exposed on all three CLI
      entry points** (codex round-1 [P2] + round-5 [P2]): `u2r.py
      convert`, `convert_interactive assemble`, and
      `convert_interactive transpile`. The last one matters because
      the interactive workflow has operators review `scripts/`
      between `transpile` and `assemble`; an operator who plans to
      finish with `--legacy-gameplay-packs` at assemble needs the
      right Luau out of `transpile` too. `convert_interactive
      transpile` also wipes `scripts/*.luau` when
      `pipeline._retranspile` is True even on an empty-transpile
      pass — covers the round-9 [P2] mode-flip case where the
      Unity project has zero runtime C# files but the stale cache
      would otherwise survive.
    - **Mutually exclusive at pipeline level.** Mutex resolution
      happens at the CLI seam via
      `click.get_current_context().get_parameter_source(
      "use_gameplay_adapters")`: passing `--legacy-gameplay-packs`
      AND an EXPLICIT `--use-gameplay-adapters` raises
      `click.UsageError`. Passing `--legacy-gameplay-packs` alone
      forces adapters off (legacy mode wins; the rest of the
      pipeline takes the pre-PR-#74 path: no adapter detection, no
      adapter runtime modules, no rehydration prune). Codex
      pushback on PR #72 flagged that running both produces
      double-binding (legacy mutates the transpiled body, adapter
      emits a new stub — they fight over the same scene parts).
    - **DamageProtocol injection gating — three signals** (PR #74
      codex round-2 [P2] + round-4 [P1] + round-4 [P2] + round-7
      [P2]). Before PR #74, the runtime-module injection force-
      emitted `damage_protocol.luau` whenever ANY adapter match was
      present (door-only / projectile-only included).
      `damage_protocol.luau` claims `ReplicatedStorage.DamageEvent`
      unconditionally and binds `OnServerEvent` to any pre-existing
      RemoteEvent at that name — a real cross-feature collision for
      adapter-enabled projects that already use a `DamageEvent` for
      unrelated traffic. `Pipeline._damage_protocol_needed()` now
      gates injection on **any one** of:
        1. **Fresh-conversion adapter path.** A `GameplayMatch`
           carries `effect.damage` or `effect.splash` in
           `capability_kinds`. Captures bullets / explosions that
           the gameplay-adapter pipeline routes through
           `DamageEvent`. Pinned set:
           `Pipeline._DAMAGE_CAPABILITY_KINDS`.
        2. **Rehydrate-path adapter scan.** On resume / publish-
           rebuild, `state.gameplay_matches` is empty by design.
           `_place_has_damage_adapter_stub` walks every script-
           bearing surface (`place.scripts` +
           `workspace_parts` + `replicated_templates`) and looks
           for the composer-emitted capability literal
           (`{kind = "effect.damage"` / `{kind = "effect.splash"`)
           **AND** the `ADAPTER_STUB_MARKER` (`@@AUTOGEN_GAMEPLAY_ADAPTER@@`)
           in the same script. Both required so a user-authored
           Luau that incidentally mentions the kind string (a
           tutorial, a script that uses the same kind-table
           shape) doesn't false-positive.
        3. **Legacy body-patch path.** Any script's source
           contains the FULL pack-emitted line `local _de =
           game:GetService("ReplicatedStorage"):FindFirstChild(
           "DamageEvent")`. The `local _de = ...` prefix is
           pack-specific (composer never emits that variable
           name) and uniquely identifies the body-patch. The
           previous tier-1 substring `:FindFirstChild(
           "DamageEvent")` false-positived on any user code that
           coincidentally looked up a `DamageEvent` RemoteEvent.
      The orchestrator (`runtime/gameplay/gameplay.luau`) loads
      DamageProtocol via `FindFirstChild` (NOT `WaitForChild`) with
      a nil guard so the absent case is an immediate no-op rather
      than a 5-second stall on every door/projectile-only adapter
      project. The server bootstrap stays always-on when adapters
      are on (`require(Gameplay)` is still useful for registering
      Movement/Lifetime/Trigger/HitDetection/Effects families at
      server start) — only DamageProtocol itself is gated. Pinned
      by `tests/test_gameplay_adapters_damage_protocol.py::
      TestPipelineInjection::{test_damage_protocol_needed_signals,
      test_damage_protocol_needed_rehydrates_via_adapter_stub_scan,
      test_legacy_probe_rejects_bare_FindFirstChild_lookup,
      test_damage_stub_scan_requires_adapter_marker}`.
    - **Runtime-module identity — structural marker + refresh-not-
      skip + stale-prune** (codex round-6 [P1] + round-7 [P1] +
      round-8 [P1] + round-9 [P1] + round-10 [P2]).
      `_inject_runtime_modules` runs against `rbx_place.scripts`
      which has typically already been hydrated from disk on
      resume paths, so the previous run's adapter runtime modules
      are sitting in place. Three coordinated decisions:
        - **Structural marker.** Every gameplay runtime module
          under `converter/runtime/gameplay/` (composer, triggers,
          movement, lifetime, hit_detection, effects,
          damage_protocol, gameplay, server_bootstrap) begins with
          `-- @@GAMEPLAY_RUNTIME_MODULE@@`. The exact marker is
          intentionally unique enough that no user-authored script
          could plausibly carry it as a comment or string literal.
        - **Identity predicate.**
          `_is_converter_gameplay_runtime_module(script,
          module_name, filename)` accepts three signals:
          (a) `parent_path == "ReplicatedStorage.AutoGen"` (in-
          memory canonical or plan-restored); (b) source contains
          `GAMEPLAY_RUNTIME_MODULE_MARKER` (definitive
          converter-owned); (c) source starts with one of the
          canonical pre-marker first lines in
          `_LEGACY_GAMEPLAY_RUNTIME_PRE_MARKER_HEADERS` (back-compat
          for rehydrates of outputs produced before the round-9
          marker landed — full canonical first lines, unique enough
          to not false-positive on user code). A user
          `Composer.cs` / `Effects.cs` / `Gameplay.cs` that
          classify_storage routed to a non-AutoGen path AND whose
          source carries none of these signals is treated as user-
          owned and left alone.
        - **Refresh-not-skip + canonical pre-pass prune.** The
          injection loop overwrites the source on existing matches
          (the runtime modules are deterministic outputs; the
          canonical version always lives on disk in
          `converter/runtime/gameplay/`). Backfills
          `parent_path = "ReplicatedStorage.AutoGen"` and
          `script_type = "ModuleScript"` on rehydrated entries
          whose parent_path came back `None` (rehydrate happens
          BEFORE `conversion_plan.json` has the gameplay-module
          paths). A pre-pass walk drops any script whose name is
          in `_ALL_GAMEPLAY_RUNTIME_MODULE_NAMES` but NOT in this
          run's emit set (e.g. a stale `DamageProtocol.luau` from
          a previous damage-bearing run that no longer qualifies)
          AND whose identity predicate matches.
        - **On-disk delete.**
          `Pipeline._delete_pruned_script_from_disk(script)`
          unlinks the cached `.luau` whenever a script is pruned
          from `rbx_place.scripts`. Without this, the next
          resume's `_rehydrate_scripts_from_disk` would load the
          orphan back. Uses the script's `source_path` when set
          (preserves nested-dir routing) and falls back to
          `<name>.luau` at the top of `scripts/`. Idempotent;
          no-op when the file is already gone or `output_dir` is
          `None` (test-harness duck-typed Pipelines).
    - **Rehydration-aware prune pass — three artifact surfaces.**
      `Pipeline._prune_legacy_gameplay_artifacts()` runs at the
      start of the gameplay-adapter runtime-injection branch in
      `_subphase_inject_runtime_libraries` (only when
      `ctx.use_gameplay_adapters` AND adapter stubs are present).
      On re-conversion of an output that contains legacy artifacts
      — regardless of which mode produced them — the prune
      removes them BEFORE adapters emit their replacements. Three
      surfaces with per-surface gating:
        - `_AutoDamageEventRouter` Script removal: global
          `place.scripts` list + part-bound recursive walk over
          `workspace_parts` and `replicated_templates`. Floors
          the legacy pack's tier-2 inline-prune at PR #73c — the
          pack only prunes when its detector fires, but the
          central prune runs whenever adapters are on. Always-on
          when adapters are on (no per-surface gate).
        - `_AutoFpsDoorTweenInjected` block strip via
          `_strip_legacy_door_tween_block`. The legacy
          `door_tween_open` pack appends the block at end-of-
          script (`script_coherence_packs.py:1775`), so slicing
          from the first marker line to end-of-string is both
          safe and complete. **Gated on
          `_door_adapter_will_emit()`** (codex round-11 [P2]):
          fires only when a door adapter (capability kind
          `movement.attribute_driven_tween`) will replace it this
          run, either via a fresh `GameplayMatch` or a rehydrate-
          path adapter stub. Without this gate, a project where
          the Door class was divergent / deny-listed / not
          detected (but other adapters fired) would lose its only
          door-open implementation. When the gate skips, a log
          line surfaces the decision so operators understand why
          a marker'd Door.luau survives.
        - `_AutoFpsHud` ScreenGui removal: drops every ScreenGui
          carrying the `attributes["_AutoFpsHud"] = True` marker
          from `place.screen_guis`. Bundled with the adapter
          prune even though the HUD is FPS-scaffolding (not an
          adapter artifact) per the design-doc surface
          enumeration. **Gated on `"fps" not in self.scaffolding`**
          (codex round-3 [P1]): the SUBPHASE_ORDER runs
          `_subphase_inject_autogen_scripts` BEFORE
          `_inject_runtime_modules`, so when `"fps"` scaffolding is
          active this run the freshly-emitted HUD already sits in
          `place.screen_guis` by the time the prune fires. An
          unconditional strip would wipe the just-emitted HUD on
          every adapter-enabled FPS conversion. The gate fires
          only when the operator is rolling back from a previous-
          run FPS opt-in to no FPS this run.
      Logs each prune so a rehydrate run is observable in
      conversion output. Pinned end-to-end by
      `tests/test_gameplay_adapters_pr74.py::
      TestPruneLegacyGameplayArtifacts`.
    - **Finalize walks part-bound scripts** (codex round-5 [P3]).
      `_subphase_finalize_scripts_to_disk` walks
      `rbx_place.scripts` + `workspace_parts` +
      `replicated_templates` with `id()`-based dedup, so a script
      that's both in the global list and bound to a part (the
      first-bind shared-ref case) writes exactly once and any
      post-binding mutation (the prune mutating bound clones)
      reaches disk. Identity-dedup keeps the work
      `O(scripts)`, not `O(scripts × parts)`.
    - Codifies the soak-and-delete exit criteria for PR #76 in a new
      `docs/design/gameplay-adapters-rollout.md` companion doc.

  - **PR #75 (MERGED, 6 codex review rounds):** introduce
    `EventDispatch` runtime module under
    `runtime/gameplay/event_dispatch.luau` carrying the
    `@@GAMEPLAY_RUNTIME_MODULE@@` first-line marker, parented at
    runtime to a converter-owned `ReplicatedStorage.AutoGen` Folder
    (NOT directly under `ReplicatedStorage` where user-authored
    `EventDispatch.cs` transpilations would land). At pipeline emit
    time the converter ALSO drops a same-named `ModuleScript` at the
    original `ReplicatedStorage.AutoFpsEventDispatch` location, body:
    ```lua
    -- @@AUTO_FPS_EVENT_DISPATCH_ALIAS@@ converter-owned (PR #75 compat alias).
    -- ... (proxies historic name to ReplicatedStorage.AutoGen.EventDispatch)
    return require(
        game:GetService("ReplicatedStorage")
            :WaitForChild("AutoGen")
            :WaitForChild("EventDispatch")
    )
    ```

    Marker constants (`_EVENT_DISPATCH_CANONICAL_NAME`,
    `_EVENT_DISPATCH_ALIAS_NAME`, `_EVENT_DISPATCH_ALIAS_MARKER`)
    centralize the strings so the prune predicates, opt-out cleanup,
    classifier-skip, and refresh paths can't drift.

    Key shipped decisions across the six review rounds:

      1. **Canonical filename stem matches in-memory name** (round-1
         [P1]). On-disk path is `scripts/AutoGen/EventDispatch.luau`
         — `Path.stem == "EventDispatch"` so the rehydrate path
         deserializes back under the same name every refresh / prune
         predicate keys on. Lowercase `event_dispatch.luau` would
         break idempotency on every resume.

      2. **Alias overwrite policy is path-scoped + marker-rescued**
         (round-2 [P2] + round-3 [P3]).
         - Path scope: only entries whose `parent_path` is `None`
           or `"ReplicatedStorage"` participate in the policy. A
           user `Script` named `AutoFpsEventDispatch` in
           `ServerScriptService` is out of scope.
         - Marker rescue: an entry carrying
           `@@AUTO_FPS_EVENT_DISPATCH_ALIAS@@` (or the legacy
           canonical body header) is rescued even from off-path so
           a previously-classified-into-`ServerStorage` alias gets
           refreshed + pinned back rather than left as a dangling
           duplicate.
         - Mixed-state dedup: when both an off-path rescue match
           AND an at-path entry exist, keep the at-path entry,
           prune the rest from in-memory + disk (matches PR #74
           round-10 [P2] on-disk delete pattern).
         - Non-ModuleScript at path: skip emission + log; user
           content wins. Already-converted HUD requires will fail
           until the user renames the colliding script, but the
           user-authored content stays intact.
         - Existing ModuleScript at path: refresh body in place
           AND pin `parent_path` back to `ReplicatedStorage` (so a
           same-named user module previously routed to
           `ServerStorage` doesn't strand the alias off-path).

      3. **Fresh-emit alias carries an explicit `source_path`**
         (round-2 [P3]). The finalize-to-disk fallback skips
         no-`source_path` writes when an `animations/` cache
         exists, which would silently drop the alias on
         animation-bearing projects.

      4. **Canonical uses an `AutoGen/` subdir on disk** to avoid
         collision with a user-authored `EventDispatch.cs`
         transpilation that lands at top-level
         `scripts/EventDispatch.luau`. Other adapter-runtime modules
         (Composer, Gameplay, etc.) keep top-level disk paths per
         PR #73a convention; EventDispatch is the only module
         susceptible to a generic Unity-class-name collision.

      5. **Lowercase migration sweep** (round-2 [P3]). Outputs
         produced by the round-1-buggy commit on case-sensitive
         filesystems carried a stale
         `scripts/AutoGen/event_dispatch.luau` (lowercase stem).
         Both `_inject_event_dispatch_with_alias` (on FPS-on runs)
         and `_remove_rehydrated_fps_autogen` (on FPS-off runs)
         marker-gate the prune so the upgrade path clears it
         without touching user-authored snake_case scripts.

      6. **Classifier-skip for converter-owned modules** (round-3
         [P2] + round-4 [P2]). `_classify_storage` keeps modules
         carrying the marker / parent_path `ReplicatedStorage.AutoGen` /
         any `_LEGACY_GAMEPLAY_RUNTIME_PRE_MARKER_HEADERS` prefix
         OUT of the plan. Otherwise the name-keyed plan bucket
         collapsed the user's `EventDispatch.cs` and the canonical
         into one entry, and the next rehydrate's `plan_lookup`
         silently demoted the user's LocalScript into the
         canonical's container (ModuleScript at ReplicatedStorage).
         Detection signal set matches the runtime predicate's
         (`_is_converter_gameplay_runtime_module`) so the upgrade
         path closes for pre-marker outputs too.

      7. **Template attach index filters to `script_type == "Script"`**
         (round-5 [P2]). `_attach_monobehaviour_scripts_to_templates`
         builds its name index flat-list-first; without the filter,
         the canonical `EventDispatch` ModuleScript (added by
         `_inject_runtime_modules`) shadows a user MonoBehaviour
         literally named `EventDispatch` because the workspace
         walk's `setdefault` no-ops on the existing key. Filtering
         to Script-only up-front excludes the canonical and lets
         the workspace walk surface the user's behaviour for
         template clone.

      8. **Opt-out path-scoped + marker-gated prune.** The opt-out
         branch in `_subphase_inject_autogen_scripts` drops a
         rehydrated `AutoFpsEventDispatch` (marker-gated, no path
         scope on the marker detection so a misclassified previous
         emit is still pruned) AND the canonical
         `AutoGen.EventDispatch` (via
         `_is_converter_gameplay_runtime_module` predicate). A
         user-authored `EventDispatch.cs` (no marker, no AutoGen
         parent_path, no legacy header) survives both the alias
         prune and the canonical prune.

      9. **`EventDispatch` is in `_ALL_GAMEPLAY_RUNTIME_MODULE_NAMES`
         but gated on FPS scaffolding.** The adapter-mode pre-pass
         normally prunes anything in that set that's not in the
         current emit; the pre-pass extends `current_module_names`
         with `EventDispatch` when `"fps" in self.scaffolding`,
         keeping the freshly-emitted canonical alive while still
         pruning it on adapter-mode-on + fps-opted-out runs (the
         expected behaviour for a non-FPS resume of a former FPS
         output).

    The alias retires in PR #78 after one full reconversion cycle
    confirms no converted output still references the old name.

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
