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

from dataclasses import dataclass, field

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
        if family == "server":
            # ANY client-domain module in a server-only container is wrong:
            # the client cannot reach ServerStorage/ServerScriptService, so a
            # client ModuleScript there can't be required and a client Script
            # there never runs client-side. (Codex slice-1 review P2: the
            # earlier `script_type == "Script"` gate missed the ModuleScript
            # case, which `_decide_script_container_from_topology` can produce
            # from caller domains alone.) ReplicatedStorage is NEUTRAL so it is
            # not in this family — no false positive on the legit shared-module
            # case.
            return _mk(
                "client-in-server-container",
                f"client-domain module {name!r} ({script_type}) placed in "
                f"server-only container {parent_path!r} — unreachable by the "
                f"client",
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

    # Check A is "modules only" this slice — exclude generated animation
    # scripts from the join so an Anim_* name that collides with a user module
    # stem doesn't downgrade that module's real check to an "unverifiable" info
    # row (Codex slice-1 review P3 false-negative). Animation drivers get their
    # own check in a later slice.
    scripts_by_name: dict[str, list[RbxScript]] = {}
    for s in scripts:
        if s.name.startswith("Anim_"):
            continue
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
