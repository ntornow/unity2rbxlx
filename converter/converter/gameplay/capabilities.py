"""Capability dataclasses and the ``Behavior`` IR.

Each Unity behaviour translates into an ordered tuple of orthogonal
``Capability`` records grouped into families (Movement, Lifetime,
HitDetection, Effect, Trigger). Capabilities declare what ``ctx`` keys
they READ and WRITE via class-level ``READS`` / ``WRITES`` sets; the
emit-time validator in :mod:`converter.gameplay.composer` enforces
single-writer-per-key and reader-after-writer ordering.

PR #73a ships only the door slice's capabilities:

  - :class:`TriggerOnBoolAttribute`
  - :class:`MovementAttributeDrivenTween`
  - :class:`LifetimePersistent`

Projectile / hit-detection / effect capabilities arrive in PR #73b.
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


# ---------------------------------------------------------------------------
# Capability union and Behavior IR
# ---------------------------------------------------------------------------

# Union over every capability variant the IR knows about. PR #73b/c add
# Movement.Impulse / Lifetime.Despawn / HitDetection.* / Effect.* here.
Capability = Union[
    TriggerOnBoolAttribute,
    MovementAttributeDrivenTween,
    LifetimePersistent,
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
