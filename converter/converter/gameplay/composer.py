"""Emit-time composition validator + per-instance Luau stub emitter.

The validator walks a :class:`Behavior`'s capability tuple and enforces:

  1. **Single-writer-per-key**: at most one capability writes a given
     ``ctx.<family>.<key>``.
  2. **Reader-after-writer**: every key in a capability's READS must
     already appear in some prior capability's WRITES.
  3. **Namespaced by family**: enforced by capability dataclasses
     declaring READS/WRITES that start with ``ctx.<family>.``; the
     validator surfaces an error if a key escapes the namespace.

The Luau emitter turns a validated Behavior into the per-instance stub
form documented in ``docs/design/gameplay-adapters.md``.
"""
from __future__ import annotations

from dataclasses import dataclass

from converter.gameplay.capabilities import (
    Behavior,
    Capability,
    ContainerResolver,
    CTX_FAMILIES,
    LifetimePersistent,
    MovementAttributeDrivenTween,
    TriggerOnBoolAttribute,
)


class BehaviorCompositionError(ValueError):
    """Raised at emit time when a Behavior's capability tuple violates
    the ctx dataflow contract.

    Carries enough information to point the user at the offending
    capability (by index + class name + family) without forcing the
    caller to re-parse a free-form message.
    """

    def __init__(
        self,
        message: str,
        *,
        behavior: Behavior,
        capability_index: int | None = None,
        key: str | None = None,
    ) -> None:
        super().__init__(message)
        self.behavior = behavior
        self.capability_index = capability_index
        self.key = key


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

def _ctx_family_of(key: str) -> str | None:
    """Return the family segment of a ``ctx.<family>.<rest>`` key.

    Returns ``None`` if the key is not in the expected shape.
    """
    parts = key.split(".")
    if len(parts) < 3 or parts[0] != "ctx":
        return None
    return parts[1]


def validate_behavior(behavior: Behavior) -> None:
    """Walk *behavior*'s capability tuple and raise
    :class:`BehaviorCompositionError` on any contract violation.

    Pure function — no side effects beyond the raise.
    """
    if not behavior.capabilities:
        # Empty capability tuples are a converter bug, not a Unity
        # data issue. Emitting an empty Composer.run is harmless at
        # runtime but masks a detector that returned a no-op Behavior;
        # surface it loudly at emit time.
        raise BehaviorCompositionError(
            f"Behavior {behavior.diagnostic_name!r} has no capabilities",
            behavior=behavior,
        )

    seen_writes: dict[str, int] = {}  # key -> capability index that wrote it
    for idx, cap in enumerate(behavior.capabilities):
        reads = getattr(cap, "READS", frozenset())
        writes = getattr(cap, "WRITES", frozenset())

        for key in writes:
            family = _ctx_family_of(key)
            if family is None or family not in CTX_FAMILIES:
                raise BehaviorCompositionError(
                    f"Capability {type(cap).__name__} writes {key!r} "
                    f"which is not a ctx.<family>.<name> key (family must "
                    f"be one of {sorted(CTX_FAMILIES)})",
                    behavior=behavior,
                    capability_index=idx,
                    key=key,
                )
            if key in seen_writes:
                prior_idx = seen_writes[key]
                prior_cap = behavior.capabilities[prior_idx]
                raise BehaviorCompositionError(
                    f"Capability {type(cap).__name__} (index {idx}) "
                    f"writes {key!r} but it was already written by "
                    f"{type(prior_cap).__name__} (index {prior_idx}). "
                    f"Two capabilities cannot share a ctx key.",
                    behavior=behavior,
                    capability_index=idx,
                    key=key,
                )
            seen_writes[key] = idx

        for key in reads:
            family = _ctx_family_of(key)
            if family is None or family not in CTX_FAMILIES:
                raise BehaviorCompositionError(
                    f"Capability {type(cap).__name__} reads {key!r} "
                    f"which is not a ctx.<family>.<name> key (family must "
                    f"be one of {sorted(CTX_FAMILIES)})",
                    behavior=behavior,
                    capability_index=idx,
                    key=key,
                )
            if key not in seen_writes:
                raise BehaviorCompositionError(
                    f"Capability {type(cap).__name__} (index {idx}) "
                    f"reads {key!r} but no prior capability writes it. "
                    f"Reorder the tuple so the writer appears first, or "
                    f"add the missing capability.",
                    behavior=behavior,
                    capability_index=idx,
                    key=key,
                )


# ---------------------------------------------------------------------------
# Luau stub emitter
# ---------------------------------------------------------------------------

# The orchestrator ModuleScript is published under
# ``ReplicatedStorage.AutoGen.Gameplay`` (see ``_inject_runtime_modules``
# in pipeline.py for the matching emit). Per-instance stubs require
# the orchestrator (not Composer directly) so the family modules are
# already registered when ``Gameplay.run`` is called.
_COMPOSER_REQUIRE = (
    'local Gameplay = require('
    'game:GetService("ReplicatedStorage")'
    ':WaitForChild("AutoGen")'
    ':WaitForChild("Gameplay"))'
)

# Structural marker baked into the first line of every emitted stub.
# The rehydrate path (publish rebuild, interactive upload re-run)
# scans every script — global AND part-bound — for this exact substring
# to decide whether to emit the AutoGen runtime modules. Phrasing is
# deliberately weird ("@@AUTOGEN_GAMEPLAY_ADAPTER@@") so no plausible
# user comment or string literal collides. Codex PR #73a-round-3
# flagged the prior marker (the ``WaitForChild("AutoGen"):...``
# substring) as user-collidable.
ADAPTER_STUB_MARKER: str = "@@AUTOGEN_GAMEPLAY_ADAPTER@@"


def _lua_string(s: str) -> str:
    """Render a Python string as a Lua double-quoted literal. Escapes
    the minimum set of characters that can appear in capability values
    (backslash, double-quote, newline). Capability names are tightly
    constrained by the Unity attribute they bind to, but we still want
    a known-safe escape path because user-named animations / attributes
    flow through here.
    """
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _emit_capability(cap: Capability) -> str:
    """Render a single capability as the Lua table form Composer.run
    consumes.
    """
    if isinstance(cap, TriggerOnBoolAttribute):
        return (
            "    {kind = " + _lua_string(cap.kind)
            + ", name = " + _lua_string(cap.name) + "},"
        )
    if isinstance(cap, MovementAttributeDrivenTween):
        x, y, z = cap.target_offset_unity
        return (
            "    {kind = " + _lua_string(cap.kind) + ",\n"
            f"     target_offset_unity = Vector3.new({x}, {y}, {z}),\n"
            f"     open_duration = {cap.open_duration},\n"
            f"     close_duration = {cap.close_duration}}},"
        )
    if isinstance(cap, LifetimePersistent):
        return "    {kind = " + _lua_string(cap.kind) + "},"
    raise BehaviorCompositionError(
        f"Unknown capability type: {type(cap).__name__}",
        behavior=Behavior(unity_file_id="", diagnostic_name="<unknown>", capabilities=()),
    )


def _emit_container_expr(resolver: ContainerResolver) -> str:
    """Render a :class:`ContainerResolver` as a Lua expression that
    locates the bind target at runtime.

    The result is a SINGLE expression usable on the right-hand side of
    ``local _container = ...``. For ``ascend_then_child`` we use an IIFE
    wrapper so we can apply a BOUNDED ``WaitForChild`` (5s) plus a
    ``warn``-and-return-nil path. The emitted Door.luau is guarded at
    the call site so a nil container doesn't reach ``Gameplay.run``.

    ``self``: ``script.Parent``.
    ``ascend_then_child``: walk up one level and wait (bounded) for a
        named child. Returns ``nil`` and warns on timeout rather than
        deadlocking script init — codex round-1 P2 on PR #73a flagged
        that the legacy door pack degraded to no-op while an unbounded
        wait would hang the script forever on prefab drift.
    """
    if resolver.kind == "self":
        return "script.Parent"
    if resolver.kind == "ascend_then_child":
        if not resolver.child_name:
            raise BehaviorCompositionError(
                "ContainerResolver(kind='ascend_then_child') requires a "
                "non-empty child_name",
                behavior=Behavior(
                    unity_file_id="",
                    diagnostic_name="<unknown>",
                    capabilities=(),
                ),
            )
        # Bounded WaitForChild with a warn-on-timeout path. IIFE keeps
        # this as a single rvalue expression so the emit stays a clean
        # ``local _container = ...`` assignment.
        return (
            "(function()\n"
            "    local _ascended = script.Parent.Parent or script.Parent\n"
            f"    local _child = _ascended:WaitForChild({_lua_string(resolver.child_name)}, 5)\n"
            "    if _child == nil then\n"
            "        warn(string.format(\n"
            "            \"[gameplay-adapter] container child %q missing under %s — adapter not bound\",\n"
            f"            {_lua_string(resolver.child_name)}, _ascended:GetFullName()\n"
            "        ))\n"
            "    end\n"
            "    return _child\n"
            "end)()"
        )
    raise BehaviorCompositionError(
        f"Unknown ContainerResolver kind: {resolver.kind!r}",
        behavior=Behavior(
            unity_file_id="",
            diagnostic_name="<unknown>",
            capabilities=(),
        ),
    )


def emit_behavior_stub(behavior: Behavior) -> str:
    """Render *behavior* as a per-instance Luau script body.

    Calls :func:`validate_behavior` first; any violation raises
    :class:`BehaviorCompositionError` BEFORE the stub is written, so
    composition errors surface at the converter rather than in
    Studio.
    """
    validate_behavior(behavior)
    container_expr = _emit_container_expr(behavior.container_resolver)
    lines = [
        # First line of every emitted stub is a structural marker the
        # rehydrate-path runtime-module injector keys off. Substring
        # MUST be unique enough that no user-authored script could
        # contain it as a comment or string literal — see
        # ``ADAPTER_STUB_MARKER`` below.
        f"-- {ADAPTER_STUB_MARKER} {behavior.diagnostic_name} "
        f"unity_file_id={behavior.unity_file_id}",
        _COMPOSER_REQUIRE,
        f"local _container = {container_expr}",
        # Bounded ``WaitForChild`` in ``ascend_then_child`` can return
        # nil on prefab drift. Guard before dispatch so we ``warn``
        # instead of indexing nil inside Gameplay.run.
        "if _container == nil then",
        f"    warn(\"[gameplay-adapter] {behavior.diagnostic_name}: "
        "container resolution returned nil, adapter not bound\")",
        "    return",
        "end",
        "Gameplay.run(_container, {",
    ]
    for cap in behavior.capabilities:
        lines.append(_emit_capability(cap))
    lines.append("})")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompositionReport:
    """One entry in ``conversion_report.json`` per matched Behavior.

    Mirrors what the design doc calls out as a debuggability requirement
    — false positives must be visible enough that an operator can write
    a deny-list entry without re-reading the converter.
    """

    unity_file_id: str
    diagnostic_name: str
    detector_name: str
    capabilities: tuple[str, ...]  # capability kind strings, in tuple order
