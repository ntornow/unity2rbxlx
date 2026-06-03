"""Phase 3 contract verifier — shadow-mode checks over the topology artifact
and the emitted scripts. See design doc §"Phase 3 — Contract verifier".

  * smoke — fires iff the topology lacks a ``modules`` block (proves the data
    path reached the verifier).
  * check A (consumer compliance) — reconciles each module's INDEPENDENT
    ``domain`` against its emitted (``script_type``, container-family). NOT a
    ``placement == topology`` mirror: the artifact's container/module_path is
    mirrored from ``RbxScript.parent_path`` (module_domain.py:1666), so that
    comparison would be tautological. ``domain`` is the only independent signal.
  * check B (component availability) — models runtime GetComponent resolution
    from the runtime ``_UNITY_TO_ROBLOX_CLASS`` map (scene_runtime.luau).
  * check C (cross-domain attribute) — validates ``cross_domain_edges``
    structurally; the literal Luau scan was reverted after false positives.

Every check records ``severity="warning"`` (or ``"info"`` for unverifiable
joins) — never fails the build; the per-check fail-closed flip lands in slice 4.
Import-light (typed against ``RbxScript`` / ``TopologyArtifact``, no ``Any``).
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
    """One contract violation. ``identity`` is a stable cross-run dedup key so a
    ``materialize_and_classify`` resume replay does not double-count."""

    check: str
    severity: str
    script: str
    detail: str
    identity: str


@dataclass
class ContractVerifierResult:
    violations: list[ContractViolation] = field(default_factory=list)

    def total(self) -> int:
        return len(self.violations)

    def counts_by_check(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for v in self.violations:
            counts[v.check] = counts.get(v.check, 0) + 1
        return counts


def verify_contract(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> ContractVerifierResult:
    """Run the shadow-mode contract checks. Emits a smoke violation when
    ``topology`` carries no ``modules`` block (the artifact never reached us)."""
    violations: list[ContractViolation] = []

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

    violations.extend(_check_consumer_compliance(topology, scripts))
    violations.extend(_check_component_availability(topology, scripts))
    violations.extend(_check_cross_domain_attribute(topology, scripts))
    return ContractVerifierResult(violations=violations)


# ---------------------------------------------------------------------------
# Check A — consumer compliance (domain ⟂ placement consistency)
# ---------------------------------------------------------------------------

_SERVER_ONLY_CONTAINERS = frozenset({"ServerScriptService", "ServerStorage"})
_NEUTRAL_CONTAINERS = frozenset({"ReplicatedStorage"})


def _container_family(parent_path: str) -> str:
    """Classify ``parent_path`` into server | client | neutral | other.

    ReplicatedStorage is NEUTRAL (requireable by either side — the doc's
    §"storage ≠ domain" cases). Module ``parent_path``s are always top-level
    container strings, so a dotted "ServerStorage.Foo" falls to "other" and
    escapes the check by design — do NOT widen this to a substring match on the
    server set (that re-introduces false positives on names merely containing a
    container token).
    """
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
    """Emitted-script name to join a module row to its ``RbxScript``: the tail
    of ``module_path`` (== ``script.name`` by construction in
    ``_stamp_container_and_path``), else ``stem``. The stem fallback only
    triggers when ``parent_path`` was empty — which also yields family "other",
    so only the container-independent type rules can fire (low-risk mis-join).
    """
    if not isinstance(module, dict):
        return ""
    module_path = str(module.get("module_path") or "")
    if module_path:
        return module_path.rsplit(".", 1)[-1]
    return str(module.get("stem") or "")


def _domain_placement_violation(
    sid: str,
    name: str,
    domain: str,
    script_type: str,
    parent_path: str,
    family: str,
) -> ContractViolation | None:
    """The domain⟂placement consistency table. Returns a violation on a
    mismatch, else None. Does NOT duplicate the storage classifier's hard
    ``ConstraintViolation``s (those abort before the verifier runs)."""
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
        # ANY client-domain module in a server-only container is wrong — the
        # client can't reach ServerStorage/ServerScriptService, so a
        # ModuleScript there can't be required and a Script never runs
        # client-side. (NEUTRAL ReplicatedStorage is not in this family.)
        if family == "server":
            return _mk(
                "client-in-server-container",
                f"client-domain module {name!r} ({script_type}) placed in "
                f"server-only container {parent_path!r} — unreachable by the "
                f"client",
            )
    elif domain == "helper":
        # Container is NOT checked for helpers: a reachability hoist can
        # legitimately place a client-reachable helper in a client container.
        if script_type in ("Script", "LocalScript"):
            return _mk(
                "helper-autorun",
                f"helper module {name!r} emitted as auto-run {script_type} "
                f"— helpers are require-only ModuleScripts",
            )
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
    placement. Modules only — ``Anim_*`` scripts are excluded from the join so
    an animation name colliding with a module stem can't downgrade the module's
    real check to an unverifiable info row."""
    violations: list[ContractViolation] = []
    modules = topology.get("modules") or {}
    if not modules:
        return violations

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
            continue
        name = _join_name(module)
        if not name:
            continue
        matches = scripts_by_name.get(name, [])
        if len(matches) != 1:
            # Ambiguous / missing join is UNVERIFIABLE: record it (no silent
            # gap) but don't raise a real violation — collisions are already
            # surfaced by the storage classifier; don't double-fail.
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
    """Serialize a violation to the JSON row stashed on
    ``ctx.scene_runtime["contract_check_violations"]``."""
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
    """Append ``result``'s violations to ``existing_rows`` in place, deduped by
    ``identity``. Returns the count appended (0 on a pure replay)."""
    seen: set[str] = {str(row.get("identity", "")) for row in existing_rows}
    appended = 0
    for v in result.violations:
        if v.identity in seen:
            continue
        seen.add(v.identity)
        existing_rows.append(violation_to_dict(v))
        appended += 1
    return appended


# ---------------------------------------------------------------------------
# Per-check fail-closed flip (slice 4)
# ---------------------------------------------------------------------------
#
# A check flips from shadow (warning-only metric) to fail-closed PER-CHECK, on
# its own cadence, only after its metric is clean across the runnable corpus
# (``tests/test_contract_corpus.py``). A flipped check's ``warning`` violations
# promote to ``ctx.errors`` at the pipeline gate (``conversion_report.success``
# becomes False). ``info`` rows (unverifiable joins) and shadow checks never
# promote. Add a check name here ONLY once its corpus gate is green AND the
# corpus actually EXERCISES it (a vacuous "0 violations because 0 relevant
# constructs" is not validation).
#   * consumer_compliance (A): flipped — SimpleFPS exercises it (module domains);
#     clean after the require-fallback signal fix.
#   * component_availability (B): flipped — SimpleFPS exercises it (20 literal-arg
#     GetComponent sites); all reachable.
#   * cross_domain_attribute (C): STILL SHADOW — SimpleFPS has 0 cross-domain
#     edges, so its "clean" metric is vacuous. C flips only once a corpus project
#     with runtime client<->server edges validates it. (Class-2 store-mismatch is
#     a separate deferred backstop — see the design doc §"Phase 3" slice 4d.)
FAIL_CLOSED_CHECKS: frozenset[str] = frozenset(
    {"consumer_compliance", "component_availability"}
)


def fail_closed_errors(result: ContractVerifierResult) -> list[str]:
    """Error strings the pipeline promotes to ``ctx.errors`` — one per real
    (``warning``) violation of a check in ``FAIL_CLOSED_CHECKS``. Shadow checks
    and ``info`` rows produce nothing (they stay metric-only)."""
    return [
        f"[contract:{v.check}] {v.script}: {v.detail}"
        for v in result.violations
        if v.severity == "warning" and v.check in FAIL_CLOSED_CHECKS
    ]


# ---------------------------------------------------------------------------
# Check B — component availability (GetComponent reachability)
# ---------------------------------------------------------------------------
#
# Generic mode emits ``self:GetComponent("X")``; at runtime X resolves to a peer
# MonoBehaviour (by stem/scriptId) -> ``_UNITY_TO_ROBLOX_CLASS[X]`` ->
# ``findFirstChildWhichIsA(X)``, else nil (and any later use errors). Check B
# flags string-literal sites whose X can't resolve. Method-validity (X maps to a
# class lacking the called method) is DEFERRED — no Roblox class→method DB
# exists, and the transpiler already bridges CharacterController.Move. Non-literal
# args are out of scope (can't resolve statically), so a fail-closed flip covers
# literal sites only.

# Matches ``:GetComponent("X")`` only — NOT plural ``GetComponents`` (different
# bug class) nor ``GetComponentIn{Children,Parent}`` (the transpiler lowers those
# to a hierarchy walk, not a map resolution). The arg class is ``[\w-]+`` so a
# peer lookup by scriptId (Unity GUIDs / ``<stem>-<idx>``, which contain ``-``)
# is scannable too; real transpiler args are identifier-shaped class names, so
# this only widens coverage.
_GETCOMPONENT_RE = re.compile(r""":GetComponent\s*\(\s*['"]([\w-]+)['"]""")


def _strip_luau_comments(source: str) -> str:
    """Strip Luau comments (``--[[ ]]`` then ``-- ...``) so the scan doesn't
    fire on a commented-out call. A ``--`` inside a string truncates the line,
    which only ever DROPS a match — the safe direction."""
    no_block = re.sub(r"--\[\[.*?\]\]", "", source, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", no_block)


# Roblox classes converted/hand-edited code may pass to GetComponent directly;
# the runtime resolves them, so they must NOT be flagged. The map's VALUES cover
# the transpiler's own outputs; this allowlist additionally guards legitimate
# direct passes the values miss (e.g. "Humanoid"). Biased to ABSTAIN — an
# over-broad allowlist only suppresses warnings (fails open), the safe direction.
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
    """Parse ``(keys, values)`` of ``_UNITY_TO_ROBLOX_CLASS`` from
    ``runtime/scene_runtime.luau`` — the single source of truth check B trusts
    (the RUNTIME map, which differs from Python ``TYPE_MAP``: CharacterController
    → "BasePart" here vs "Humanoid" there). Parsed rather than duplicated so the
    two never drift; an exhaustive guard test pins the full set so a runtime-file
    refactor that drops/renames an entry fails loudly."""
    path = Path(__file__).resolve().parent.parent / "runtime" / "scene_runtime.luau"
    text = path.read_text(encoding="utf-8")
    keys: set[str] = set()
    values: set[str] = set()
    # Block-bounded to the table body (``= {`` .. bare ``}``) so we don't match
    # ``Ident = "Str"`` pairs elsewhere in the file.
    block = re.search(
        r"local\s+_UNITY_TO_ROBLOX_CLASS[^=]*=\s*\{(.*?)\n\}",
        text,
        re.DOTALL,
    )
    if block is not None:
        for key, value in re.findall(r'(\w+)\s*=\s*"([^"]+)"', block.group(1)):
            keys.add(key)
            values.add(value)
    # Out-of-table sentinel assigns: ``_UNITY_TO_ROBLOX_CLASS.Transform = ...``.
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
    """Flag ``GetComponent("X")`` sites whose X resolves to nil at runtime."""
    keys, values = _runtime_class_map()

    # Peer set = stem ∪ scriptId (the modules dict key), matching the runtime
    # lookup ``m.stem == name or m.scriptId == name``. This is GLOBAL, not scoped
    # to the call's GameObject (the runtime peer branch only searches the current
    # GameObject's components) — a deliberate LENIENT bias / known false negative:
    # the verifier has no per-GameObject component placement, and a scoped check
    # would flag every peer GetComponent.
    peer: set[str] = set()
    modules = topology.get("modules") or {}
    for script_id, module in modules.items():
        peer.add(str(script_id))
        if isinstance(module, dict):
            stem = module.get("stem")
            if isinstance(stem, str) and stem:
                peer.add(stem)

    reachable = peer | set(keys) | set(values) | _ROBLOX_CLASS_ALLOWLIST

    violations: list[ContractViolation] = []
    # Key dedup + identity on (name, parent_path, X) so two different scripts
    # sharing a name each surface their own violation.
    seen: set[tuple[str, str, str]] = set()
    for script in scripts:
        source = _strip_luau_comments(script.source or "")
        ppath = script.parent_path or ""
        for match in _GETCOMPONENT_RE.finditer(source):
            x = match.group(1)
            if x in reachable:
                continue
            key = (script.name, ppath, x)
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
                    identity=f"component_availability:{script.name}@{ppath}:{x}",
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Check C — cross-domain attribute access (structural edge invariant)
# ---------------------------------------------------------------------------
#
# Read directly off ``cross_domain_edges`` (NOT a Luau scan): every edge whose
# endpoints are both RUNTIME (client/server) and DIFFERENT must resolve via
# ``remote_event_bridge``, else the cross-process write never reaches the reader.
# Zero false positives; currently satisfied (the producer always bridges) but a
# real regression guard.
#
# The original literal ``SetAttribute``/``GetAttribute`` scan was REVERTED: it
# false-positives on (P1) Class-2 shared-flag literal mirrors (modeled in
# ``shared_flag_channels``, not edges) and (P2) the writer×reader Cartesian over
# reused field names (the emitted Luau carries no instance identity to match the
# edge granularity). The Class-2 store-mismatch (door bug) stays DEFERRED — it's
# phantom post-coherence and needs a pre-coherence signal + adversarial review.

_RUNTIME_DOMAINS = frozenset({"client", "server"})


def _check_cross_domain_attribute(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> list[ContractViolation]:
    """Structural cross-domain-edge bridging invariant (``scripts`` unused — the
    check reads the structured edges, not the Luau)."""
    violations: list[ContractViolation] = []
    for edge in topology.get("cross_domain_edges") or []:
        if not isinstance(edge, dict):
            continue
        from_d = str(edge.get("from_domain") or "")
        to_d = str(edge.get("to_domain") or "")
        # Only runtime-to-runtime cross-domain edges need a bridge; a non-runtime
        # endpoint is legitimately excluded and a same-domain edge needs none.
        if from_d not in _RUNTIME_DOMAINS or to_d not in _RUNTIME_DOMAINS:
            continue
        if from_d == to_d:
            continue
        resolution = edge.get("resolution")
        strategy = (
            resolution.get("strategy") if isinstance(resolution, dict) else ""
        )
        if strategy == "remote_event_bridge":
            continue
        field_name = str(edge.get("field") or "")
        edge_id = str(
            edge.get("id")
            or f"{edge.get('from_script')}::{field_name}::{edge.get('to_script')}"
        )
        violations.append(
            ContractViolation(
                check="cross_domain_attribute",
                severity="warning",
                script=field_name,
                detail=(
                    f"cross-domain edge {edge_id!r} (field {field_name!r}, "
                    f"{from_d!r}→{to_d!r}) has resolution strategy "
                    f"{str(strategy)!r}, not 'remote_event_bridge' — the "
                    f"cross-process write never reaches the reader"
                ),
                identity=f"cross_domain_attribute:{edge_id}",
            )
        )
    return violations
