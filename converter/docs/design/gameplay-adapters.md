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
