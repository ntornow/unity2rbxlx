"""Phase 3 contract verifier.

Build-time enforcement layer that proves every emitted artifact obeys the
topology authority. See the design doc §"Phase 3 — Contract verifier (new
``contract_verifier.py``)" in
``docs/design/scene-runtime-architecture-ir.md``.

**Shadow mode** — every check records warnings only; the per-check flip to
fail-closed lands in slice 4. Checks:

  * smoke (slice 0) — fires iff the topology artifact lacks a ``modules`` key,
    proving the data path reaches the verifier.
  * **check A — consumer compliance (slice 1, this file).** Reconciles each
    module's INDEPENDENT ``domain`` against its emitted (``script_type``,
    container-family of ``parent_path``). This is NOT a "placement == topology"
    comparison: the artifact's ``container``/``module_path`` is mirrored from
    ``RbxScript.parent_path`` (``module_domain.py:1666``), so that would be
    tautological. ``domain`` is the only independent signal (source-derived,
    never reads ``parent_path``/``script_type``), so a domain⟂placement
    mismatch is the real bug (e.g. a server-domain module emitted as a
    LocalScript that never runs server-side). Modules only — animation_drivers
    are deferred (their domain↔script_class is consistent by construction).
  * check B (component availability / GetComponent) — slice 2.
  * check C (cross-domain attribute access) — slice 3.

The module is deliberately import-light (no heavy pipeline imports) so it
stays unit-testable in isolation. ``RbxScript`` and ``TopologyArtifact`` are
imported from their real modules for concrete typing — no ``Any``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from converter.scene_runtime_topology.build_topology import TopologyArtifact
from core.roblox_types import RbxScript


@dataclass(frozen=True)
class ContractViolation:
    """A single contract violation surfaced by the verifier.

    ``identity`` is a STABLE dedup key (e.g. ``f"{check}:{script}:{key}"``).
    The pipeline stash dedupes on it so a ``materialize_and_classify`` resume
    replay does not double-count the same violation.
    """

    check: str
    """Which check produced this: "smoke" (slice 0), then "consumer_compliance"
    / "component_availability" / "cross_domain_attribute" in slices 1-3."""

    severity: str
    """"warning" in slice 0 — every check ships shadow first; the per-check
    flip to fail-closed lands in slice 4."""

    script: str
    """Source path or script name; "" if the violation is global (no single
    owning script)."""

    detail: str
    """Human-readable description of what is wrong."""

    identity: str
    """Stable dedup key. Two violations with the same identity are the same
    violation across runs/resumes."""


@dataclass
class ContractVerifierResult:
    """The result of a single ``verify_contract`` call."""

    violations: list[ContractViolation] = field(default_factory=list)

    def total(self) -> int:
        """Total number of violations."""
        return len(self.violations)

    def counts_by_check(self) -> dict[str, int]:
        """Violation counts grouped by ``check`` name."""
        counts: dict[str, int] = {}
        for v in self.violations:
            counts[v.check] = counts.get(v.check, 0) + 1
        return counts


def verify_contract(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
    *,
    mode: str = "shadow",
) -> ContractVerifierResult:
    """Run the contract verifier over the topology artifact + final scripts.

    Slice 0 runs NO real A/B/C checks. It runs ONE trivial **smoke check**:
    emit a ``ContractViolation(check="smoke", ...)`` iff ``topology`` lacks a
    ``"modules"`` key (i.e. the topology artifact never reached the verifier
    or is empty). When the topology carries modules → zero violations. This
    proves the data path is wired end-to-end.

    ``mode`` is "shadow" and is threaded through for the eventual per-check
    fail-closed flip (slice 4); it does not change behavior yet.
    """
    violations: list[ContractViolation] = []

    # Smoke check: the topology artifact must carry a populated ``modules``
    # block by the time the verifier runs (it is populated inside
    # ``_build_and_apply_topology`` before the hook fires). A missing/empty
    # ``modules`` key means the data never reached us — surface it so the
    # wiring is provable from a violation, not from silence.
    if not topology.get("modules"):
        violations.append(
            ContractViolation(
                check="smoke",
                severity="warning",
                script="",
                detail=(
                    "topology artifact reached the verifier without a "
                    "populated 'modules' block"
                ),
                identity="smoke:missing-modules",
            )
        )

    # Check A — consumer compliance (domain⟂placement consistency).
    violations.extend(_check_consumer_compliance(topology, scripts))

    # Check B — component availability (GetComponent reachability).
    violations.extend(_check_component_availability(topology, scripts))

    return ContractVerifierResult(violations=violations)


# ---------------------------------------------------------------------------
# Check A — consumer compliance (domain ⟂ placement consistency)
# ---------------------------------------------------------------------------

# Container families. The check fires only on a CONFIDENT family mismatch; an
# unrecognized ``parent_path`` (e.g. Workspace, a nested model path) maps to
# "other" and is never flagged.
_SERVER_ONLY_CONTAINERS = frozenset({"ServerScriptService", "ServerStorage"})
_NEUTRAL_CONTAINERS = frozenset({"ReplicatedStorage"})


def _container_family(parent_path: str) -> str:
    """Classify a final ``parent_path`` into "server" | "client" | "neutral"
    | "other".

    ReplicatedStorage is NEUTRAL — requireable by either side, so a module of
    any domain may legitimately live there (the doc's §"storage ≠ domain"
    cases 1-3, 6). Client-only containers are matched by substring so the
    dotted ``StarterPlayer.StarterPlayerScripts`` /
    ``StarterPlayer.StarterCharacterScripts`` forms and a bare
    ``ReplicatedFirst`` all classify as client.
    """
    # Module ``parent_path`` values are always TOP-LEVEL container strings
    # (the storage classifier + reachability path only ever emit
    # "ServerScriptService"/"ServerStorage"/"ReplicatedStorage"/"ReplicatedFirst"
    # /"StarterPlayer.Starter*Scripts"). A dotted "ServerStorage.Foo" would fall
    # through to "other" and escape the check — that is intentional and
    # currently unreachable for modules; do NOT "fix" it into a substring match
    # on the server set (that would re-introduce false positives on names that
    # merely contain a container token).
    if parent_path in _SERVER_ONLY_CONTAINERS:
        return "server"
    if parent_path in _NEUTRAL_CONTAINERS:
        return "neutral"
    if (
        "StarterPlayer" in parent_path
        or "StarterCharacter" in parent_path
        or parent_path == "ReplicatedFirst"
    ):
        return "client"
    return "other"


def _join_name(module: object) -> str:
    """The emitted-script name to join a topology module row to its
    ``RbxScript``.

    Prefer the last segment of ``module_path`` (built as
    ``f"{container}.{script.name}"`` in ``_stamp_container_and_path``, so its
    tail IS ``script.name`` by construction — robust even when the C# class
    name differs from the file stem). Fall back to ``stem`` when no
    ``module_path`` was stamped.
    """
    if not isinstance(module, dict):
        return ""
    module_path = str(module.get("module_path") or "")
    if module_path:
        return module_path.rsplit(".", 1)[-1]
    # Stem fallback: ``module_path`` is unstamped only when the module's
    # ``RbxScript.parent_path`` was empty (``_stamp_container_and_path``
    # requires a truthy container). Then ``stem`` may differ from the emitted
    # ``RbxScript.name`` (C# class name ≠ file stem), risking a mis-join — but an
    # empty ``parent_path`` also makes ``_container_family("") == "other"``, so
    # only the container-independent type rules could fire. Low-risk edge.
    return str(module.get("stem") or "")


def _domain_placement_violation(
    sid: str,
    name: str,
    domain: str,
    script_type: str,
    parent_path: str,
    family: str,
) -> ContractViolation | None:
    """Apply the domain⟂placement consistency table. Returns a warning-severity
    ``ContractViolation`` on a mismatch, else ``None``.

    Does NOT duplicate the storage classifier's hard ``ConstraintViolation``s
    (LocalScript-in-ServerScriptService, ModuleScript-in-ReplicatedFirst,
    ``storage_classifier.py:898``) — those abort before the verifier runs.
    """
    def _mk(reason_key: str, detail: str) -> ContractViolation:
        return ContractViolation(
            check="consumer_compliance",
            severity="warning",
            script=name,
            detail=detail,
            identity=f"consumer_compliance:{sid}:{reason_key}",
        )

    if domain == "server":
        if script_type == "LocalScript":
            return _mk(
                "server-localscript",
                f"server-domain module {name!r} emitted as a LocalScript "
                f"— a LocalScript never runs on the server",
            )
        if family == "client":
            return _mk(
                "server-in-client-container",
                f"server-domain module {name!r} placed in client-only "
                f"container {parent_path!r}",
            )
    elif domain == "client":
        if script_type == "Script" and family == "server":
            return _mk(
                "client-script-in-server-container",
                f"client-domain module {name!r} emitted as an auto-run Script "
                f"in server-only container {parent_path!r}",
            )
    elif domain == "helper":
        if script_type in ("Script", "LocalScript"):
            return _mk(
                "helper-autorun",
                f"helper module {name!r} emitted as auto-run {script_type} "
                f"— helpers are require-only ModuleScripts",
            )
        # Container is intentionally NOT checked for helpers: a reachability
        # hoist can legitimately place a client-reachable helper in a
        # client-only container.
    elif domain == "excluded":
        return _mk(
            "excluded-but-emitted",
            f"module {name!r} is domain=excluded but still emitted a script "
            f"({script_type} in {parent_path!r})",
        )
    return None


def _check_consumer_compliance(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> list[ContractViolation]:
    """Reconcile each module's independent ``domain`` against its emitted
    placement. See the module docstring + design doc §Phase 3 check #1."""
    violations: list[ContractViolation] = []
    modules = topology.get("modules") or {}
    if not modules:
        return violations

    scripts_by_name: dict[str, list[RbxScript]] = {}
    for s in scripts:
        scripts_by_name.setdefault(s.name, []).append(s)

    for sid, module in modules.items():
        if not isinstance(module, dict):
            continue
        domain = str(module.get("domain") or "")
        if domain not in ("client", "server", "helper", "excluded"):
            # No domain / unknown value — nothing independent to reconcile.
            continue
        name = _join_name(module)
        if not name:
            continue
        matches = scripts_by_name.get(name, [])
        if len(matches) != 1:
            # DQ4(a): an ambiguous / missing join is UNVERIFIABLE. Record it
            # (no silent gap) but do NOT raise a real violation — stem/name
            # collisions are already surfaced by the storage classifier, and a
            # verifier should not double-fail on a known-degraded join.
            violations.append(
                ContractViolation(
                    check="consumer_compliance",
                    severity="info",
                    script=name,
                    detail=(
                        f"unverifiable: module {sid!r} (name {name!r}) joined "
                        f"to {len(matches)} emitted script(s); placement not "
                        f"checked"
                    ),
                    identity=f"consumer_compliance:{sid}:unverifiable",
                )
            )
            continue
        script = matches[0]
        parent_path = script.parent_path or ""
        violation = _domain_placement_violation(
            sid,
            name,
            domain,
            script.script_type,
            parent_path,
            _container_family(parent_path),
        )
        if violation is not None:
            violations.append(violation)

    return violations


def violation_to_dict(violation: ContractViolation) -> dict[str, str]:
    """Serialize a ``ContractViolation`` to the plain JSON-able row the
    pipeline stashes on ``ctx.scene_runtime["contract_check_violations"]``."""
    return {
        "check": violation.check,
        "severity": violation.severity,
        "script": violation.script,
        "detail": violation.detail,
        "identity": violation.identity,
    }


def stash_violations(
    existing_rows: list[dict[str, str]],
    result: ContractVerifierResult,
) -> int:
    """Append ``result``'s violations to ``existing_rows`` IN PLACE, deduped
    by ``identity`` against rows already present.

    Mirrors the membership-gated ``contract_fail_closed`` plumbing
    (``pipeline.py:2041``) so a ``materialize_and_classify`` resume replay
    does not double-count. Reads existing identities into a set first, then
    appends only genuinely-new rows.

    Returns the number of rows actually appended (0 on a pure replay).
    """
    seen: set[str] = {
        str(row.get("identity", "")) for row in existing_rows
    }
    appended = 0
    for v in result.violations:
        if v.identity in seen:
            continue
        seen.add(v.identity)
        existing_rows.append(violation_to_dict(v))
        appended += 1
    return appended


# ---------------------------------------------------------------------------
# Check B — component availability (GetComponent reachability)
# ---------------------------------------------------------------------------
#
# Generic mode emits the peer form ``self:GetComponent("X")``
# (code_transpiler.py:1329). At runtime (scene_runtime.luau:752-780) X resolves
# to: a peer converted-MonoBehaviour (by stem/scriptId) -> else
# ``_UNITY_TO_ROBLOX_CLASS[X]`` -> else ``findFirstChildWhichIsA(X)``. An X that
# is none of those returns nil, so any subsequent use/method-call errors. Check
# B flags those unreachable sites.
#
# SCOPE (slice 2): reachability only. Method-validity (X maps to a Roblox class
# that lacks the called method — the CharacterController->BasePart->:Move()
# anecdote) is DEFERRED: the repo has no Roblox class->method database, and the
# transpiler already routes CharacterController.Move/.SimpleMove/.isGrounded
# through a bridge (api_mappings API_CALL_MAP), so that anecdote is largely
# already handled. Documented gap, not silently dropped.
#
# COVERAGE: only STRING-LITERAL args are checked. A non-literal arg
# (``self:GetComponent(typeVar)``) cannot be resolved statically and is skipped
# — so a future fail-closed flip of check B covers literal-arg sites only.

# Matches ``:GetComponent("X")`` / ``:GetComponentInChildren("X")`` /
# ``:GetComponentInParent("X")`` with a string-literal arg. Does NOT match the
# plural ``GetComponents`` (list semantics, different bug class) because a
# literal "(" must follow the optional In*/Parent suffix.
_GETCOMPONENT_RE = re.compile(
    r""":GetComponent(?:InChildren|InParent)?\s*\(\s*['"]([A-Za-z_]\w*)['"]""",
)

# Roblox classes converted (or hand-edited) code may legitimately pass to
# GetComponent directly — runtime ``findFirstChildWhichIsA`` resolves them, so
# they must NOT be flagged. The runtime map's VALUES already cover the
# transpiler's own outputs; this allowlist guards the fail-closed flip against
# legitimate direct-Roblox-class passes the values set happens to miss. Biased
# to ABSTAIN: an over-broad allowlist only suppresses warnings (fails open),
# which is the safe direction for a shadow→fail-closed check. Never flag a name
# the runtime can resolve.
_ROBLOX_CLASS_ALLOWLIST = frozenset({
    "Humanoid", "HumanoidRootPart", "Seat", "VehicleSeat",
    "ClickDetector", "ProximityPrompt", "Sound", "SoundGroup",
    "Camera", "BasePart", "MeshPart", "Part", "UnionOperation", "Model",
    "ParticleEmitter", "Beam", "Trail", "Light", "PointLight",
    "SpotLight", "SurfaceLight", "Attachment",
    "SurfaceGui", "BillboardGui", "ScreenGui", "Frame",
    "TextLabel", "TextButton", "TextBox", "ImageLabel", "ImageButton",
    "GuiButton", "Decal", "Texture", "Weld", "WeldConstraint", "Motor6D",
    "Highlight", "Folder", "Configuration",
})


@lru_cache(maxsize=1)
def _runtime_class_map() -> tuple[frozenset[str], frozenset[str]]:
    """Parse ``_UNITY_TO_ROBLOX_CLASS`` from ``runtime/scene_runtime.luau`` and
    return ``(keys, values)``.

    This is the single source of truth check B trusts (the locked decision:
    trust the RUNTIME map, which differs from Python ``TYPE_MAP`` — e.g.
    CharacterController -> "BasePart" here vs "Humanoid" there). Parsed, not
    duplicated, so the verifier and the runtime never drift. An EXHAUSTIVE
    guard test (``test_runtime_class_map_*``) pins the full parsed key/value
    set so a runtime-file refactor that drops/renames an entry fails loudly.
    Cached: the file never changes within a process.
    """
    path = Path(__file__).resolve().parent.parent / "runtime" / "scene_runtime.luau"
    text = path.read_text(encoding="utf-8")
    keys: set[str] = set()
    values: set[str] = set()
    # Block-bounded: the table body from ``= {`` to the first line that is a
    # bare ``}`` (the table's close). Avoids matching ``Ident = "Str"`` pairs
    # elsewhere in the file.
    block = re.search(
        r"local\s+_UNITY_TO_ROBLOX_CLASS[^=]*=\s*\{(.*?)\n\}",
        text,
        re.DOTALL,
    )
    if block is not None:
        for key, value in re.findall(r'(\w+)\s*=\s*"([^"]+)"', block.group(1)):
            keys.add(key)
            values.add(value)
    # Sentinel assigns outside the table literal:
    #   _UNITY_TO_ROBLOX_CLASS.Transform = _CLASS_TRANSFORM_SELF
    for key in re.findall(r"_UNITY_TO_ROBLOX_CLASS\.(\w+)\s*=", text):
        keys.add(key)
    sentinel = re.search(r'_CLASS_TRANSFORM_SELF\s*=\s*"([^"]+)"', text)
    if sentinel is not None:
        values.add(sentinel.group(1))
    return frozenset(keys), frozenset(values)


def _check_component_availability(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> list[ContractViolation]:
    """Flag ``GetComponent("X")`` sites whose ``X`` resolves to nil at runtime.
    See the section comment above + design doc §Phase 3 check #3."""
    keys, values = _runtime_class_map()

    # Peer converted-MonoBehaviours are reachable by stem OR class_name (the
    # runtime peer lookup matches m.stem or m.scriptId; the AI emits the C#
    # class name, which is the stem in the common case and the class_name when
    # they differ — include both to avoid a stem≠class-name false positive).
    peer: set[str] = set()
    modules = topology.get("modules") or {}
    for module in modules.values():
        if not isinstance(module, dict):
            continue
        for field_name in ("stem", "class_name"):
            value = module.get(field_name)
            if isinstance(value, str) and value:
                peer.add(value)

    reachable = peer | set(keys) | set(values) | _ROBLOX_CLASS_ALLOWLIST

    violations: list[ContractViolation] = []
    seen: set[tuple[str, str]] = set()
    for script in scripts:
        source = script.source or ""
        for match in _GETCOMPONENT_RE.finditer(source):
            x = match.group(1)
            if x in reachable:
                continue
            key = (script.name, x)
            if key in seen:
                continue
            seen.add(key)
            violations.append(
                ContractViolation(
                    check="component_availability",
                    severity="warning",
                    script=script.name,
                    detail=(
                        f"GetComponent({x!r}) resolves to nil — {x!r} is not a "
                        f"converted component, a mapped Unity type, or a known "
                        f"Roblox class; the result will error when used"
                    ),
                    identity=f"component_availability:{script.name}:{x}",
                )
            )
    return violations
