"""Phase 3 contract verifier.

Build-time enforcement layer that proves every emitted artifact obeys the
topology authority. See the design doc §"Phase 3 — Contract verifier (new
``contract_verifier.py``)" in
``docs/design/scene-runtime-architecture-ir.md``.

**Slice 0 (this file): shadow mode + skeleton only.** The three real checks
land in later slices:

  * Slice 1 — check A (consumer compliance) + domain-consistency invariant.
  * Slice 2 — check B (component availability / GetComponent).
  * Slice 3 — check C (cross-domain attribute access).

For now the module runs a single trivial **smoke check** that genuinely
depends on its input (it fires iff the topology artifact lacks a ``modules``
key), so tests can prove the data actually reaches the verifier rather than
passing green for the wrong reason.

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

    ``mode`` is "shadow" in slice 0 and is threaded through for the eventual
    per-check fail-closed flip (slice 4); it does not change behavior yet.
    ``scripts`` is unused in slice 0 (the real checks in slices 1-3 consume
    it) but is part of the locked signature.
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

    return ContractVerifierResult(violations=violations)


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
