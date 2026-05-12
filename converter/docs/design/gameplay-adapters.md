# Gameplay Adapters

**Status:** Proposed 2026-05-12. First stage: PR #72 (this doc + skeleton).
Targets: `converter/converter/gameplay/`, `converter/runtime/gameplay/`, and
removal of three coherence packs from `converter/converter/script_coherence_packs.py`
(`bullet_physics_raycast`, `player_damage_remote_event`, `door_tween_open`).

## Decision

Translate Unity gameplay subsystems (Rigidbody projectiles, Animator-driven
actuators, camera-raycast damage interactions) via **structured adapters that
consume Unity facts and emit canonical Luau stubs over a shared runtime
library** — instead of regex-patching AI-transpiled Luau text after the fact.

## Why we're changing course

The current approach lives in `script_coherence_packs.py` as three packs:

  - `bullet_physics_raycast` regex-detects `TurretBullet`/`PlaneBullet` Luau
    output and wholesale-replaces the body.
  - `player_damage_remote_event` regex-patches `Player.luau` to `FireServer`,
    plus emits a server router script that validates and replays the raycast.
  - `door_tween_open` regex-detects `Door.luau` and appends a TweenService
    block, with a runtime guard that tries to defer to animation-phase
    drivers.

PR #71 took 12 rounds of codex review with new findings every round. The
findings cluster into five recurring categories:

1. **Regex misses AI output variations** — different runs name locals
   `hitInst` / `hitPart` / `rb` / `rootPart`; AI emits `(GetAttribute or 0) + 1`
   one round and `true` the next; door driver names include spaces or
   capitalised clip names. Every variation is a separate regex repair.
2. **Client-server protocol drift** — the client patch and the server router
   are emitted from the same pack but live as two independent text blobs.
   Adding a payload field on one side without the other leaves rehydrated
   outputs half-patched.
3. **Mixed-project scoping** — packs run on the flat `place.scripts` list
   (one entry per `.cs` class), with no notion of which prefab instance the
   adapter should fire for. A project with two Door prefabs where one has an
   animation-phase driver and the other doesn't trips repeatedly.
4. **Network input validation** — every iteration of the `DamageEvent`
   RemoteEvent surfaces a new attack class (non-Instance payloads, non-scalar
   values, repeated-`true` writes that don't fire `GetAttributeChangedSignal`).
5. **Unity-semantics regression** — wholesale-replacement keeps dropping
   Unity-specific behaviour (PlaneBullet's `OverlapSphere(2)` splash, the
   `Explosion` template clone, prefab `force=200` override values).

These aren't regex-quality problems. The pack abstraction is the wrong tool
for the job: each pack is doing semantic lowering, protocol design, and
runtime generation inside a post-hoc text patcher.

## Architecture

### Three spec dataclasses (`converter/converter/gameplay/specs.py`)

```python
@dataclass(frozen=True)
class ProjectileSpec:
    """Lowered form of a Unity Rigidbody projectile MonoBehaviour."""
    unity_class: str         # diagnostic; matches the source .cs class name
    force: float             # Unity-meters/sec impulse magnitude
    fade_time: float         # seconds — destroy after this
    damage: float            # listener-facing payload value
    splash_radius: float = 0 # 0 = direct-hit only; >0 = Unity OverlapSphere
    spawn_explosion: bool = False  # clone ReplicatedStorage.Templates.Explosion on impact

@dataclass(frozen=True)
class ActuatorSpec:
    """Lowered form of an Animator-bool-driven motion (door/elevator/gate)."""
    attribute_name: str          # e.g. "open" — the bool parameter
    open_offset_unity: tuple[float, float, float]  # m_PositionCurves delta
    open_duration: float         # seconds (clip length)
    close_duration: float        # seconds (close.anim clip length)
    target_child_name: str = "door"  # which child mesh moves; default matches SciFi_Door

@dataclass(frozen=True)
class DamageInteractionSpec:
    """Lowered form of a Camera-raycast + SendMessage("TakeDamage") hitscan."""
    weapon_class: str            # diagnostic; matches the source .cs class name
    shoot_range_unity: float     # Player.cs ``shootRange`` in meters
    trigger_input: str           # "MouseButton1" by default
    damage_value: float          # what the server writes to TakeDamage
```

Each spec carries **Unity-side values verbatim**. Stud-space conversion,
gravity policy, raycast budget, and Roblox-specific concerns live in the
runtime libraries, not the spec.

### Three runtime libraries (`converter/runtime/gameplay/`)

`projectile.luau` exposes:
```lua
Projectile.spawn(container: Instance, spec: ProjectileSpec) -> ()
```
The runtime owns: anti-gravity VectorForce, stud-space velocity (Unity
`force * STUDS_PER_METER`), per-Heartbeat segment raycast (so high-velocity
bullets don't tunnel past targets), splash damage at the raycast hit
position, and the Explosion-template clone on impact.

`actuator.luau` exposes:
```lua
Actuator.bind(container: Instance, spec: ActuatorSpec) -> ()
```
The runtime owns: per-instance coexistence detection (does this prefab
already have an animation-phase driver listening on the same attribute? if
so, no-op), TweenService creation, easing, idempotency.

`damage_router.luau` exposes:
```lua
DamageRouter.installServer() -> ()        -- called once from a Script
DamageRouter.fireFromClient(hitInstance: BasePart, takeDamageValue: any) -> ()
```
The runtime owns: RemoteEvent creation, full payload validation (typeof,
IsA, IsDescendantOf, distance gate, server-side raycast replay from camera
origin), and the `TakeDamage` SetAttribute mirror. Both halves live in one
Lua file — protocol drift is structurally impossible.

### Detectors operate on Unity facts (`converter/converter/gameplay/*.py`)

Detection input: parsed Unity data the converter already has — MonoBehaviour
fields (`_extract_monobehaviour_attributes`), C# source bodies (used for
pattern detection, not parsing), Animator controller clips, prefab
composition.

Detection output: a `Spec` instance (or `None`). One spec per Unity
component instance, not one per `.cs` class.

```python
# converter/gameplay/projectile.py
def detect(node: SceneNode, csharp_sources: dict[str, str]) -> ProjectileSpec | None:
    if not _has_rigidbody(node):
        return None
    cs_class = node.attributes.get("_ScriptClass")
    if not cs_class:
        return None
    src = csharp_sources.get(cs_class)
    if src is None or not _looks_like_projectile(src):
        return None
    return ProjectileSpec(
        unity_class=cs_class,
        force=float(node.attributes.get("force", _default_force(src))),
        damage=float(node.attributes.get("damage", _default_damage(src))),
        fade_time=float(node.attributes.get("fadeTime", _default_fade(src))),
        splash_radius=_splash_radius_from_overlap_sphere(src),  # 0 if not present
        spawn_explosion="Instantiate(explosion" in src,
    )
```

`_looks_like_projectile` is a C# (not Luau) signature check —
`AddRelativeForce`/`AddForce` + Rigidbody + `OnCollisionEnter` with a
`Destroy(gameObject)`. C# is the source of truth and far less variable than
AI-transpiled Luau output.

### Emit: three-line stubs

When a detector returns a spec, the converter emits a 3-line transpiled-script
body that requires the runtime lib and passes the spec:

```lua
-- TurretBullet.luau (converter-emitted, AI never sees this script)
local Projectile = require(game:GetService("ReplicatedStorage"):WaitForChild("AutoGen"):WaitForChild("Projectile"))
Projectile.spawn(script.Parent, {
    force = 60, fadeTime = 3, damage = 10, splashRadius = 0,
    spawnExplosion = false,
})
```

`AutoGen/` is a converter-owned folder under ReplicatedStorage. Naming
deliberately project-neutral — the libraries inside aren't FPS-flavoured
(see "Genre neutrality" below).

The transpile phase (`code_transpiler.transpile_scripts`) is where this fits:
before sending a script to Claude/AI, check the gameplay detectors. If any
matches, skip the AI entirely and emit the canonical stub. If none match,
proceed with the existing AI path (`api_mappings.py` + `luau-analyze`
reprompt loop).

### Genre neutrality

The patterns we're translating aren't FPS-specific despite the test project
being SimpleFPS:

| Pattern | Genre coverage |
|---|---|
| Rigidbody projectile | FPS bullets, RPG fireballs, RTS missiles, action-game grenades |
| Animator-bool actuator | Doors, drawbridges, elevators, gates, traps |
| Camera-raycast hitscan | FPS shooters, RTS unit attacks, top-down click damage |

Adapters live under `converter/gameplay/`, not `converter/scaffolding/fps/`.
Runtime libraries live under `runtime/gameplay/`. No "Auto Fps" prefix on
anything that isn't actually FPS-specific.

What STAYS in `converter/scaffolding/fps.py`: the genuinely FPS-coded parts —
HUD ScreenGui (health/ammo/crosshair), `MouseBehavior.LockCenter`, WeaponSlot
attached to camera, first-person body-hide via `LocalTransparencyModifier`.
Those are an FPS-input/UX convention, not a Unity pattern.

## Feature flag and migration

`--use-gameplay-adapters` CLI flag, default off in PR #72. When off, the
existing coherence packs run unchanged (no regression for projects that have
already been converted).

PR sequence:

  - PR #72 (this PR): introduce specs + runtime libs + detector skeletons,
    behind the flag. Off by default. Existing coherence packs unchanged.
  - PR #73: turn the flag on, delete the three packs, run end-to-end on
    SimpleFPS (already validated in Studio) and smoke-test on Gamekit3D and
    ChopChop to confirm cross-project pattern matching.
  - PR #74: rename `_AutoFpsEventDispatch` → `EventDispatch` in
    `runtime/event_dispatch.luau` (the `AutoFps` prefix was defensive
    naming; the gameplay runtime libs adopt a clean prefix scheme from the
    start so they don't repeat that mistake).
  - PR #75: remove the feature flag, finalise rename to default behaviour.

Each PR is reviewable independently and rolls back independently.

## Rejected alternatives

**Keep coherence packs, just make the regexes better.** Twelve rounds of
codex review say no. Each fix uncovers a new edge because the matching
surface is "AI-transpiled Luau text" and that's an unbounded surface.

**One mega-runtime module that scripts require.** Looks attractive, but
collapses all three adapters into a single Lua file with three unrelated
public APIs. Easier to test and version per-adapter.

**Replace AI transpilation entirely with canonical bodies for ALL Unity
scripts.** Out of scope. The AI still has clear value for Unity scripts that
don't match a recognised gameplay pattern. The adapter system is opt-in per
script via spec detection — unrecognised scripts go through the existing
AI path unchanged.

## Risks and mitigations

  - **Spec mis-detection.** A non-projectile script that happens to have
    `AddForce` + `OnCollisionEnter` could be falsely classified. Mitigation:
    detectors emit their decision into `conversion_report.json`; users can
    disable adapter classification per-class via a deny-list config file in
    the output dir. Tests run all 9 test projects through the detectors and
    pin expected matches.
  - **Runtime-library regression.** Centralising the projectile logic into
    one file means a bug in `projectile.luau` breaks every projectile. The
    file is small and unit-testable end-to-end with stubbed `workspace` /
    `RunService` — much easier to test than scattered regex matches.
  - **Migration cost.** Three coherence packs + ~30 tests get deleted in
    PR #73. Total deletion is ~600 lines; total addition (this PR series) is
    ~800 lines. Net code growth is modest; conceptual surface shrinks
    significantly because the three packs collapse to one mental model
    (adapter classifies, runtime executes).
