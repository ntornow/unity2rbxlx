"""Capability dataclasses and the ``Behavior`` IR.

Each Unity behaviour translates into an ordered tuple of orthogonal
``Capability`` records grouped into families (Movement, Lifetime,
HitDetection, Effect, Trigger). Capabilities declare what ``ctx`` keys
they READ and WRITE via class-level ``READS`` / ``WRITES`` sets; the
emit-time validator in :mod:`converter.gameplay.composer` enforces
single-writer-per-key and reader-after-writer ordering.

Capabilities shipped:

  Door slice (PR #73a):
    - :class:`TriggerOnBoolAttribute`
    - :class:`MovementAttributeDrivenTween`
    - :class:`LifetimePersistent`

  Projectile slice (PR #73b):
    - :class:`MovementImpulse`
    - :class:`LifetimeDespawn`
    - :class:`HitDetectionRaycastSegment`
    - :class:`HitDetectionOverlapSphere`
    - :class:`EffectDamage`
    - :class:`EffectSplash`
    - :class:`EffectSpawnTemplate`
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Literal, Union


# ---------------------------------------------------------------------------
# Capability family namespaces in ``ctx``. Cross-family collisions are
# impossible by construction; intra-family collisions are caught by the
# single-writer-per-key check in the validator.
# ---------------------------------------------------------------------------

CTX_FAMILIES: tuple[str, ...] = (
    "movement",
    "lifetime",
    "hitDetection",
    "effect",
    "trigger",
)


# ---------------------------------------------------------------------------
# Container resolver
# ---------------------------------------------------------------------------
#
# A :class:`Behavior` binds a capability tuple to ONE Lua-side container.
# That container is what Trigger.OnBoolAttribute watches, what
# Movement.AttributeDrivenTween tweens, etc. ContainerResolver tells the
# emit function how to locate that container relative to the script
# itself.
#
#   - ``self`` (default): the container is ``script.Parent``. Matches
#     the design doc's canonical example.
#   - ``ascend_then_child``: walk up one level (``script.Parent.Parent``)
#     and select a named sibling of the script's parent. Used when the
#     Unity MonoBehaviour lives on a trigger volume but the actual mesh
#     being driven is a sibling — e.g. SciFi_Door, where ``Door.cs``
#     sits on a trigger child and the visible door mesh is a sibling
#     named ``door``.
#
# The resolver is per-instance metadata on the Behavior, NOT a class-
# level field on a Movement capability. Two doors with different sibling
# names get two different resolvers without coupling the Movement family
# to door-specific assumptions.

@dataclass(frozen=True)
class ContainerResolver:
    kind: Literal["self", "ascend_then_child"] = "self"
    child_name: str = ""


# ---------------------------------------------------------------------------
# Trigger family
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TriggerOnBoolAttribute:
    """Listen to a bool-valued attribute on the container and publish its
    normalized value to ``ctx.trigger.value``.

    Normalization rules (also documented in
    ``runtime/gameplay/triggers.luau``):
      - Missing/nil attribute on bind: published as ``false``.
      - Non-bool value on bind or change: coerced via Lua truthiness
        (anything that's not ``false`` or ``nil`` becomes ``true``).
      - Every change re-runs normalization and re-publishes.
    """

    name: str

    kind: ClassVar[str] = "trigger.on_bool_attribute"
    READS: ClassVar[frozenset[str]] = frozenset()
    # ``ctx.trigger.value`` is the current normalized bool;
    # ``ctx.trigger.changed`` is a BindableEvent fired AFTER each
    # value update. Downstream capabilities subscribe to the signal
    # rather than re-reading the underlying Roblox attribute.
    WRITES: ClassVar[frozenset[str]] = frozenset({
        "ctx.trigger.value",
        "ctx.trigger.changed",
    })


# ---------------------------------------------------------------------------
# Movement family
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MovementImpulse:
    """Apply an initial impulse-velocity along the container's local
    ``direction_local`` axis with magnitude ``force_unity`` (Unity m/s).

    Runtime maps ``force_unity`` to stud-space via STUDS_PER_METER and
    sets ``AssemblyLinearVelocity`` directly on the container (so the
    write is deterministic — applying ``ApplyImpulse`` requires reading
    mass and Roblox's ApplyImpulse interacts oddly with anchored
    chains). Anti-gravity is implicit: the runtime also installs a
    ``VectorForce`` that cancels ``workspace.Gravity * mass`` so the
    bullet flies like a near-massless Unity impulse rather than diving
    under Roblox's 196 studs/s² gravity.

    ``ctx.movement.velocity`` is published as a stud-space Vector3 the
    HitDetection family reads to segment-cast each frame.
    """

    direction_local: tuple[float, float, float]
    force_unity: float

    kind: ClassVar[str] = "movement.impulse"
    READS: ClassVar[frozenset[str]] = frozenset()
    WRITES: ClassVar[frozenset[str]] = frozenset({
        "ctx.movement.velocity",
    })


@dataclass(frozen=True)
class MovementAttributeDrivenTween:
    """Tween the container's primary part between a closed pose (current
    pose at first bind) and an open pose at ``target_offset_unity`` (in
    Unity meters, converted to studs at runtime) driven by
    ``ctx.trigger.value``.

    Runtime semantics:
      - On first bind, snap to open OR closed pose based on the current
        ``ctx.trigger.value`` (door spawned with ``open=true`` starts
        open).
      - Mid-tween reversal: cancel the in-flight tween and start a new
        one from the current pose toward the new target. No snapping.
      - Idempotent re-bind: gated by the composer's ``_GameplayBound``
        marker.
    """

    target_offset_unity: tuple[float, float, float]
    open_duration: float
    close_duration: float

    kind: ClassVar[str] = "movement.attribute_driven_tween"
    READS: ClassVar[frozenset[str]] = frozenset({
        "ctx.trigger.value",
        "ctx.trigger.changed",
    })
    WRITES: ClassVar[frozenset[str]] = frozenset()


# ---------------------------------------------------------------------------
# Lifetime family
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LifetimePersistent:
    """No timer, no destroy-on-hit. The container lives forever. Included
    explicitly so the IR records the lifetime decision rather than
    leaving it implicit.
    """

    kind: ClassVar[str] = "lifetime.persistent"
    READS: ClassVar[frozenset[str]] = frozenset()
    WRITES: ClassVar[frozenset[str]] = frozenset()


@dataclass(frozen=True)
class LifetimeDespawn:
    """Destroy the container after ``seconds`` regardless of hits.
    Maps to Unity's ``Destroy(gameObject, t)``.

    The runtime uses ``Debris:AddItem`` so re-binding is idempotent
    (Debris coalesces duplicate adds on the same instance) and so the
    despawn survives anchor changes by HitDetection.
    """

    seconds: float

    kind: ClassVar[str] = "lifetime.despawn"
    READS: ClassVar[frozenset[str]] = frozenset()
    WRITES: ClassVar[frozenset[str]] = frozenset()


# ---------------------------------------------------------------------------
# HitDetection family
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HitDetectionRaycastSegment:
    """Per-Heartbeat segment cast from the previous-frame container
    position to the current. Catches tunneling on fast projectiles that
    ``Touched`` misses (a 200 m/s bullet travels ~3 studs per 60Hz
    frame, well beyond the Touched contact zone).

    Publishes the hit Instance and impact CFrame to ctx for downstream
    Effect capabilities. ``ctx.hitDetection.consumed`` is also written
    as a Lua boolean so Effect capabilities can be no-ops between
    impacts (the validator doesn't track this — runtime invariant).
    """

    kind: ClassVar[str] = "hit_detection.raycast_segment"
    # Reading ctx.movement.velocity is technically optional — the
    # runtime falls back to the part's AssemblyLinearVelocity if the
    # ctx slot is absent — but declaring the read enforces that an
    # impulse capability runs FIRST so the bullet has velocity before
    # we start segment-casting. The validator catches a misordered
    # tuple at emit time.
    READS: ClassVar[frozenset[str]] = frozenset({
        "ctx.movement.velocity",
    })
    WRITES: ClassVar[frozenset[str]] = frozenset({
        "ctx.hitDetection.lastImpactCFrame",
        "ctx.hitDetection.lastInstance",
        "ctx.hitDetection.impactSignal",
    })


@dataclass(frozen=True)
class HitDetectionOverlapSphere:
    """Apply a one-shot ``GetPartBoundsInRadius`` sphere at the
    container's position (in stud-space, ``radius_unity`` × stud
    conversion). Used for triggers like proximity sensors.

    Distinct from :class:`EffectSplash`, which is an area-of-effect at
    the LAST raycast impact point. OverlapSphere fires from the
    container itself and doesn't require a prior HitDetection.
    """

    radius_unity: float

    kind: ClassVar[str] = "hit_detection.overlap_sphere"
    READS: ClassVar[frozenset[str]] = frozenset()
    WRITES: ClassVar[frozenset[str]] = frozenset({
        "ctx.hitDetection.lastInstance",
        "ctx.hitDetection.lastImpactCFrame",
        "ctx.hitDetection.impactSignal",
    })


# ---------------------------------------------------------------------------
# Effect family
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectDamage:
    """Apply ``value`` damage to the hit Instance's ancestor Humanoid.

    Reads ``ctx.hitDetection.lastInstance`` (set by either RaycastSegment
    or OverlapSphere — the validator enforces a writer ran first). The
    runtime walks up to the Humanoid-bearing model and calls
    ``Humanoid:TakeDamage`` plus sets a ``TakeDamage`` attribute on the
    model so server-side damage routing (PR #73c's damage_protocol)
    can also observe it.
    """

    value: float

    kind: ClassVar[str] = "effect.damage"
    READS: ClassVar[frozenset[str]] = frozenset({
        "ctx.hitDetection.impactSignal",
    })
    WRITES: ClassVar[frozenset[str]] = frozenset()


@dataclass(frozen=True)
class EffectSplash:
    """Area-of-effect damage at the last impact point. Replicates
    Unity ``OverlapSphere(transform.position, radius)`` + per-collider
    ``SendMessage("TakeDamage", value)`` from ``PlaneBullet.cs``.

    ``radius_unity`` is in Unity meters; the runtime converts to
    studs. Splash uses the IMPACT CFrame (codex round-5 P1 on PR #71
    flagged that centering on the bullet root misses wall-hits at
    tunneling speeds where the bullet over-travels the collision
    point).
    """

    radius_unity: float
    value: float

    kind: ClassVar[str] = "effect.splash"
    READS: ClassVar[frozenset[str]] = frozenset({
        "ctx.hitDetection.lastImpactCFrame",
        "ctx.hitDetection.impactSignal",
    })
    WRITES: ClassVar[frozenset[str]] = frozenset()


@dataclass(frozen=True)
class EffectSpawnTemplate:
    """Clone ``ReplicatedStorage.Templates.<name>`` at the impact
    CFrame. Maps to Unity ``Instantiate(prefab, transform.position,
    Quaternion.identity)`` for explosion VFX, hit-spark prefabs, etc.

    No-op when the template is absent — older outputs without prefab
    packages (or projects that didn't define an Explosion prefab) just
    skip the VFX rather than erroring. Cloned instances are debris-
    cleaned after 2 seconds so the workspace doesn't accumulate.
    """

    name: str

    kind: ClassVar[str] = "effect.spawn_template"
    READS: ClassVar[frozenset[str]] = frozenset({
        "ctx.hitDetection.lastImpactCFrame",
        "ctx.hitDetection.impactSignal",
    })
    WRITES: ClassVar[frozenset[str]] = frozenset()


# ---------------------------------------------------------------------------
# Capability union and Behavior IR
# ---------------------------------------------------------------------------

# Union over every capability variant the IR knows about.
Capability = Union[
    TriggerOnBoolAttribute,
    MovementImpulse,
    MovementAttributeDrivenTween,
    LifetimePersistent,
    LifetimeDespawn,
    HitDetectionRaycastSegment,
    HitDetectionOverlapSphere,
    EffectDamage,
    EffectSplash,
    EffectSpawnTemplate,
]


@dataclass(frozen=True)
class Behavior:
    """A per-instance behaviour bound to one Unity scene node.

    ``unity_file_id`` is the scene-node fileID this behaviour binds to;
    the deny-list keys off it. ``diagnostic_name`` is for logs and the
    conversion report only. ``container_resolver`` selects the Lua-side
    instance that capabilities operate on — see
    :class:`ContainerResolver` for the variants.
    """

    unity_file_id: str
    diagnostic_name: str
    capabilities: tuple[Capability, ...]
    container_resolver: ContainerResolver = field(
        default_factory=ContainerResolver,
    )
