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

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from converter.child_index_lowering import _luau_pos_is_code
from converter.scene_runtime_topology.build_topology import TopologyArtifact

# Deterministic Luau code-position primitives (NOT lowering discharge-logic): the
# long-bracket guards let the verifier exclude ``[[...]]`` / ``[=[...]=]`` /
# ``--[[...]]`` strings/comments before counting tokens, the same way every other
# code-position scan in the pipeline does. Importing the position primitives keeps
# the verifier INDEPENDENT of the lowering's discharge state (it re-derives discharge
# from the source itself) while sharing the canonical position arithmetic.
from converter.trigger_stay_lowering import (
    _long_bracket_open_level,
    _luau_pos_in_long_bracket,
)
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
    # FIX 5 ordering: the BINDING floor reports BEFORE the secondary ordinal
    # floor (check D) — a dropped/reshaped rig binding is the real cause; the
    # surviving ordinal it might leave behind is the downstream symptom.
    violations.extend(_check_rig_binding_present(topology, scripts))
    violations.extend(_check_surviving_child_ordinal(topology, scripts))
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
#   * cross_domain_attribute (C): flipped — the MiniNet networked corpus project
#     (slice 6) exercises it (1 runtime client<->server edge, correctly bridged);
#     SimpleFPS alone has 0 edges, which is why a second project was needed.
#     (Class-2 store-mismatch is a separate deferred backstop — see the design
#     doc §"Phase 3" slice 4d.)
#   * child_ordinal_survivor (D): flipped — fact-based. Fires ONLY on a surviving
#     positional child ordinal in a FULLY-resolved script (the pre-rewrite should
#     have eliminated it -> a real regression). The non-promoting
#     ``child_ordinal_coverage_gap`` info row (an unresolved/Phase-2 ref) is NOT
#     in this set.
#   * rig_binding_present: flipped — fact-based. Fires when an IR-declared rig
#     retarget binding (the ``rig_binding`` carrier's ``field``/``child``) is NOT
#     confirmed DISCHARGED by an INDEPENDENT scan of the final source (the lazy
#     resolver method + rewritten reads + neutralized camera-child write), or when
#     that scan DISAGREES with the lowering's ``present`` self-stamp (a mis-stamp /
#     reverted edit / stale-resume carrier). SimpleFPS EXERCISES it (the Player rig
#     binding); the corpus goes green at S2's fixture regen (which captures the
#     discharged Player source + ``rig_binding present=True``), so per the
#     admission rule the flip is admissible — until then the un-regenerated
#     corpus fixture has no ``rig_binding`` carrier and ABSTAINS (None -> no row).
FAIL_CLOSED_CHECKS: frozenset[str] = frozenset(
    {
        "consumer_compliance",
        "component_availability",
        "cross_domain_attribute",
        "child_ordinal_survivor",
        "rig_binding_present",
    }
)

# Every promoted verifier error carries this prefix so the pipeline can REPLACE
# its own rows on a rerun (drop prior, re-add current) — ``ctx.errors`` persists
# across a ``materialize_and_classify`` resume, so an append-only promotion would
# leave a stale ``success=False`` after the issue is fixed or the fail-open hatch
# is set. Distinct from the transpile-time ``scene-runtime contract failed
# closed`` strings, which this prefix must never match.
CONTRACT_ERROR_PREFIX = "[contract-verifier:"


def fail_closed_errors(result: ContractVerifierResult) -> list[str]:
    """Error strings the pipeline promotes to ``ctx.errors`` — one per real
    (``warning``) violation of a check in ``FAIL_CLOSED_CHECKS``. Shadow checks
    and ``info`` rows produce nothing (they stay metric-only). Each is prefixed
    with ``CONTRACT_ERROR_PREFIX`` so the pipeline owns + replaces them."""
    return [
        f"{CONTRACT_ERROR_PREFIX}{v.check}] {v.script}: {v.detail}"
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


# ---------------------------------------------------------------------------
# Check D — surviving child ordinal (FACT-BASED backstop for relation #2)
# ---------------------------------------------------------------------------
#
# The generic-mode pre-rewrite (``child_ref_resolver``) resolves transform-rooted
# ``transform.GetChild(n)`` to named ``Find("<name>")`` lookups before transpile,
# stamping each script's ``{getchild_total, resolved_total}`` onto
# ``RbxScript.child_ref_resolution``. Check D asserts AGAINST THAT FACT: for a
# FULLY-resolved script the pre-rewrite should have eliminated every ordinal, so a
# surviving positional ``GetChildren()[n]`` is a regression (fail-closed). A script
# with no fact (pre-field fixture) or with ``resolved_total < getchild_total`` (a
# genuinely-unresolvable ref, e.g. the Player ``cam = Camera.main.transform``)
# ABSTAINS — never reds the corpus. Keyed on the deterministic resolver tally, not
# a fragile C#-symbol->Luau-name match (the AI does not preserve the symbol names).

# Adjacent survivor shape: ``<recv>:GetChildren()[N]`` where ``<recv>`` is a
# simple dotted name OR a method call (``self:_tBase()``) — the latter is what an
# AI factored survivor looks like (``self:_tBase():GetChildren()[1]``). A
# variable index (``[i]``) is a genuine dynamic lookup, not a flattened ordinal,
# so N must be an integer literal. Group 1 captures the receiver expression so
# the engine-global filter can read its ROOT token.
_GETCHILDREN_INDEX_ANY_RE = re.compile(
    r"([A-Za-z_][\w.]*(?::[A-Za-z_]\w*\(\))?):GetChildren\(\)\s*\[\s*(\d+)\s*\]"
)

# Two-line factored shape (E5): ``local v = X:GetChildren()`` then a later
# ``v[<int>]`` positional index. ``X`` may be a simple dotted name OR a method
# call (``self:_tBase():GetChildren()``), so a method-receiver factored survivor
# is caught too. The trailing ``(?!\s*\[)`` excludes the ADJACENT form
# (``X:GetChildren()[N]``, already counted by ``_GETCHILDREN_INDEX_ANY_RE``) so
# a single site is not double-counted. Group 1 captures the ``local`` ident;
# group 2 captures the receiver expression for the engine-global filter.
_GETCHILDREN_ASSIGN_RE = re.compile(
    r"\blocal\s+([A-Za-z_]\w*)\s*=\s*"
    r"([A-Za-z_][\w.]*(?::[A-Za-z_]\w*\(\))?):GetChildren\(\)(?!\s*\[)"
)

# Known-safe Roblox ENGINE GLOBALS: a ``<root>...:GetChildren()[N]`` rooted at one
# of these is an engine-tree iteration (``workspace.Folder:GetChildren()[1]``),
# NOT an unresolved child-ref ordinal, so it must NOT count against the per-site
# unresolved-site budget. Conservative — only CLEARLY-global roots; ``self`` and
# locals are NOT here (they are child-ref-plausible and DO count).
_ENGINE_GLOBAL_ROOTS = frozenset({"workspace", "game", "script", "Players"})

# The ROOT token of a receiver expression is its first identifier (before the
# first ``.``, ``:`` or ``(``). ``game:GetService("Players").Foo`` and
# ``game.Players.Foo`` both root at ``game`` -> engine-global.
_RECEIVER_ROOT_RE = re.compile(r"^([A-Za-z_]\w*)")


def _receiver_roots_at_engine_global(receiver: str) -> bool:
    """True if the survivor's receiver expression roots at a known-safe Roblox
    engine global (``workspace``/``game``/``script``/``Players``), so it is an
    engine-tree iteration, not a child-ref ordinal. Conservative: only the
    clearly-global roots; ``self`` and local variables return False (they ARE
    child-ref-plausible and count against the budget)."""
    m = _RECEIVER_ROOT_RE.match(receiver.strip())
    return m is not None and m.group(1) in _ENGINE_GLOBAL_ROOTS


@dataclass(frozen=True)
class _RigDeadWriteExempt:
    """The deterministic upstream identity of the ONE rig dead-init-write that check
    D may exempt (REDESIGN r3). All three are projections of the resolver fact carried
    on ``RbxScript.rig_binding`` — the AI-STABLE identity, NOT an output-shape
    fingerprint. A surviving ``self.<field> = self.<cam_receiver>:GetChildren()[k]``
    site is exempted ONLY when it matches this EXACT triple (field + receiver +
    ordinal) AND the statement-anchored shape; every mismatch biases to COUNT."""

    field_name: str       # the rig field (``weaponSlot``) — the assignment LHS
    cam_receiver: str     # the C# receiver text (``cam`` seeded / ``Camera.main.transform``
                          #   direct — never ``""``); the site receiver must be
                          #   exactly ``self.<cam_receiver>``
    cam_ordinal: int      # the 0-based ``GetChild(n)``; the site's 1-based ``[k]`` must
                          #   equal ``cam_ordinal + 1``


def _count_surviving_child_ordinals(
    source: str, exempt: _RigDeadWriteExempt | None = None
) -> int:
    """Count POSITIONAL child-ordinal survivor SITES in ``source`` — the
    adjacent shape ``<recv>:GetChildren()[N]`` (simple OR method-call receiver)
    plus the across-lines factored shape (``local v = X:GetChildren()`` then a
    later ``v[<int>]``). Per-site (not boolean) so the backstop can fail-close
    when survivors exceed the script's unresolved-site budget. Code-position
    aware; counts each factored ``local v`` chain ONCE. Survivors whose receiver
    roots at a known-safe ENGINE GLOBAL (``workspace``/``game``/...) are EXCLUDED
    — they are engine-tree iterations, not unresolved child-ref ordinals.

    RIG-AWARE exemption (SITE-ANCHORED, POSITIVELY anchored — REDESIGN r3): when
    ``exempt`` is set (the caller has independently confirmed a DISCHARGED rig
    binding) AT MOST ONE adjacent ``self.<field> = self.<cam_receiver>:GetChildren()[k]``
    dead init-write site is skipped from the count — the EXACT credited site the
    Path-A read-reroute superseded. The decision is made INSIDE this same walk,
    AFTER the identical ``_luau_pos_is_code`` + engine-global filters that COUNT a
    site, and ONLY when ``_site_is_discharged_rig_dead_write`` confirms a TIGHT
    POSITIVE match of the credited site: the r7 statement identity (the enclosing Luau
    statement is EXACTLY ``self.<field> = <site>``, the site being its whole RHS) ANDed
    with the r3 receiver anchor (receiver is exactly ``self.<cam_receiver>``, never a
    bare local) AND the r3 ordinal anchor (the surviving ``[k]`` equals
    ``cam_ordinal + 1``). So the exemption can only ever remove a site this function
    WOULD have counted (``exempt ⊆ counted-survivors``, structurally), it never spans
    the across-lines factored form, and the ``< 1`` cap exempts only the SINGLE credited
    write — a second same-shape write, a READ survivor, a DIFFERENT-receiver write
    (``self.muzzle:...`` — codex r8), a SAME-receiver DIFFERENT-ordinal write
    (``...[2]`` when the credited init was ``[1]`` — codex r3), a bare-``cam`` receiver,
    the direct no-seed form, a substring-LHS look-alike, or an engine-global/bracket
    survivor this function did NOT count all REMAIN counted and still fail closed.
    Any mismatch / ambiguous site is NOT exempted — it is counted (fail closed)."""
    exempted = 0
    count = 0
    for m in _GETCHILDREN_INDEX_ANY_RE.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _receiver_roots_at_engine_global(m.group(1)):
            continue  # engine-global iteration — not a child-ref survivor
        if (
            exempted < 1
            and exempt is not None
            and _site_is_discharged_rig_dead_write(
                source,
                m.start(),
                m.end(),
                exempt.field_name,
                m.group(1),
                int(m.group(2)),
                exempt.cam_receiver,
                exempt.cam_ordinal,
            )
        ):
            # The EXACT credited dead init-write of the discharged rig field —
            # superseded by the read reroute (Path A). Skip exactly this one site.
            exempted += 1
            continue
        count += 1
    for m in _GETCHILDREN_ASSIGN_RE.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _receiver_roots_at_engine_global(m.group(2)):
            continue  # engine-global iteration — not a child-ref survivor
        ident = m.group(1)
        index_re = re.compile(r"\b" + re.escape(ident) + r"\s*\[\s*\d+\s*\]")
        for im in index_re.finditer(source, m.end()):
            if _luau_pos_is_code(source, im.start()):
                count += 1
                break  # one factored chain == one survivor site
    return count


# LHS of the exempt dead init-write: the STANDALONE lvalue ``self.<field>`` at the
# START of a statement, then a REAL assignment ``=`` (not ``==``/``<=``/``>=``/``~=``).
# Anchored at the statement start so ``other.self.weaponSlot =`` / ``myself.weaponSlot
# =`` cannot match (their statement starts at ``other``/``myself``, not ``self``); the
# field word-boundary ``(?!\w)`` rejects the ``weaponSlot`` vs ``weaponSlotBackup``
# prefix collision; ``(?<![<>~])=`` + ``(?!=)`` rejects every comparison operator.
_RIG_EXEMPT_LHS_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _rig_exempt_lhs_re(field: str) -> re.Pattern[str]:
    """Compiled (cached) regex matching a statement that OPENS with the standalone
    lvalue ``self.<field>`` followed by a real assignment ``=``. Match group spans the
    LHS+``=``; ``match`` (anchored) is applied to a single statement's projected text."""
    cached = _RIG_EXEMPT_LHS_RE_CACHE.get(field)
    if cached is None:
        cached = re.compile(
            r"self\." + re.escape(field) + r"(?!\w)\s*(?<![<>~=])=(?!=)"
        )
        _RIG_EXEMPT_LHS_RE_CACHE[field] = cached
    return cached


# Block-opener KEYWORDS: each begins a nested statement region, so the statement that
# physically contains a site AFTER one of them starts past the keyword, not at the
# enclosing ``if``/``for``/``while`` head. (``)`` is handled positionally — a ``)``
# closing a control header, e.g. ``for (...)``, is an un-matched closer at depth 0 in
# the backward scan and already advances ``start``.)
_RIG_BLOCK_OPENER_KEYWORDS = frozenset({"then", "do", "else", "repeat"})
# Block CLOSER keywords: each ENDS an inner statement region, so the forward statement
# scan stops there (``if c then <stmt> end`` -> ``<stmt>`` ends at ``end``).
_RIG_BLOCK_CLOSER_KEYWORDS = frozenset({"end", "else", "elseif", "until"})
_RIG_WORD_RE = re.compile(r"[A-Za-z_]\w*")


def _advance_past_block_openers(projected: str, start: int, pos: int) -> int:
    """Return the largest position ``<= pos`` that is the END of a depth-0 block-opener
    keyword (``then``/``do``/``else``/``repeat``) lying in ``[start, pos)``, or ``start``
    if none. Forward token scan over ``[start, pos)`` tracking bracket depth so a keyword
    inside ``()``/``[]``/``{}`` (never a statement opener at this depth) is ignored."""
    result = start
    depth = 0
    k = start
    while k < pos:
        ch = projected[k]
        if ch in "([{":
            depth += 1
            k += 1
            continue
        if ch in ")]}":
            if depth > 0:
                depth -= 1
            k += 1
            continue
        if depth == 0 and (ch.isalpha() or ch == "_"):
            m = _RIG_WORD_RE.match(projected, k)
            assert m is not None
            if m.group(0) in _RIG_BLOCK_OPENER_KEYWORDS and m.end() <= pos:
                result = m.end()
            k = m.end()
            continue
        k += 1
    return result


def _rig_statement_bounds(projected: str, pos: int) -> tuple[int, int]:
    """The ``[start, end)`` char bounds of the single Luau STATEMENT that physically
    contains ``pos``, over the position-PRESERVING code projection ``projected``.

    Backward from ``pos``: stop AFTER the nearest preceding depth-0 statement boundary
    — a depth-0 ``;``, a code newline that is NOT an RHS continuation, an UN-matched
    block opener (``)``/``{``/``then``/``do``/``else``/``repeat``), or start-of-source.
    Forward from ``pos``: stop AT the next depth-0 ``;`` or non-continuation newline, or
    EOF. Bracket depth is tracked so a ``;``/newline inside ``()``/``[]``/``{}`` does not
    split. Both scans run over the projection so a delimiter inside a string/comment
    (already blanked) can never bound the statement."""
    n = len(projected)
    # --- backward to statement start ---
    start = 0
    depth = 0
    i = pos - 1
    while i >= 0:
        ch = projected[i]
        if ch in ")]}":
            depth += 1
            i -= 1
            continue
        if ch in "([{":
            if depth == 0:
                start = i + 1  # an un-matched opener — statement begins after it
                break
            depth -= 1
            i -= 1
            continue
        if depth == 0:
            if ch == ";":
                start = i + 1
                break
            if ch == "\n":
                # A code newline ends the prior statement UNLESS the RHS continues
                # across it (``_rig_line_continues`` mirrors the lowering's rule). The
                # continuation test reads the line ENDING at this newline, so pass that
                # line's start (the char after the preceding newline / source start).
                line_start = projected.rfind("\n", 0, i) + 1
                if not _rig_line_continues(projected, line_start, i):
                    start = i + 1
                    break
        i -= 1
    # A block opener KEYWORD (``then``/``do``/``else``/``repeat``) at depth 0 between
    # ``start`` and ``pos`` begins a nested statement — advance ``start`` past the LAST
    # such opener so ``if c then self.f = X[1] end`` anchors the inner statement at
    # ``self.f`` (else the ``if`` head defeats the standalone-``self`` LHS match). Scan
    # FORWARD from ``start`` over code positions, tracking bracket depth so an opener
    # inside ``()``/``[]``/``{}`` is ignored.
    start = _advance_past_block_openers(projected, start, pos)
    # Skip leading blanks so the LHS anchor sits at the first token.
    while start < pos and projected[start] in " \t\r\n":
        start += 1
    # --- forward to statement end ---
    end = n
    depth = 0
    j = pos
    while j < n:
        ch = projected[j]
        if ch in "([{":
            depth += 1
            j += 1
            continue
        if ch in ")]}":
            if depth == 0:
                end = j
                break
            depth -= 1
            j += 1
            continue
        if depth == 0:
            if ch == ";":
                end = j
                break
            if ch == "\n":
                if not _rig_line_continues(projected, start, j):
                    end = j
                    break
            if ch.isalpha() or ch == "_":
                wm = _RIG_WORD_RE.match(projected, j)
                assert wm is not None
                if wm.group(0) in _RIG_BLOCK_CLOSER_KEYWORDS:
                    # A block CLOSER (``end``/``else``/``elseif``/``until``) ends the
                    # inner statement: ``if c then self.f = X[1] end`` -> the write
                    # statement ends at ``end``, so a trailing ``end`` does not look
                    # like a trailing RHS operand.
                    end = j
                    break
                j = wm.end()  # skip the whole identifier so its chars aren't re-scanned
                continue
        j += 1
    return (start, end)


def _site_is_discharged_rig_dead_write(
    source: str,
    site_start: int,
    site_end: int,
    field: str,
    site_receiver: str,
    site_ordinal: int,
    cam_receiver: str,
    cam_ordinal: int,
) -> bool:
    """True iff the GetChildren survivor SITE spanning ``[site_start, site_end)`` is the
    EXACT credited dead init-write of a discharged rig binding — a TIGHT POSITIVE match
    of the one site the resolver fact credited (REDESIGN r3), NOT a negative text filter.
    ALL of the following must hold; every mismatch biases to COUNT (return False):

      (statement identity — the r7 protections, RETAINED and ANDed, NOT replaced)
        the single Luau statement physically containing the site is EXACTLY the
        assignment ``self.<field> = <recv>:GetChildren()[k]`` whose ENTIRE RHS is that
        one site:
          (1) the statement OPENS with the standalone lvalue ``self.<field>`` then a
              real assignment ``=`` (``_rig_exempt_lhs_re`` — rejects ``myself``/
              ``a.self`` look-alikes, the ``weaponSlot``/``weaponSlotBackup`` prefix
              collision, and every comparison operator); AND
          (2) the GetChildren site is the WHOLE RHS — its receiver starts exactly at the
              first RHS token (no leading ``nil or ...`` / other operand) and nothing but
              whitespace follows the site to the statement end (no trailing
              ``+ bar:Get...`` operand). So an arbitrary RHS that merely CONTAINS a
              GetChildren (``self.<field> = nil or foo:GetChildren()[1]``) is NOT exempt.

      (receiver anchor — REDESIGN r3, NEW) the GetChildren RECEIVER is exactly
        ``self.<cam_receiver>`` — the dot-form member access of the carrier's deterministic
        C# receiver text, matched ONLY in the ``self.<member>`` form. A bare-``cam`` local,
        a DIFFERENT receiver (``self.muzzle`` — codex r8), or the direct no-seed form
        (``cam_receiver=="Camera.main.transform"`` forms no ``self.<member>``) does NOT
        match -> COUNTED (the direct form is a SAFE false-positive).

      (ordinal anchor — REDESIGN r3, NEW) the surviving ``:GetChildren()[k]`` ordinal
        ``k`` equals ``cam_ordinal + 1`` (the carrier's 0-based ``GetChild(n)``; Luau
        ``GetChildren()`` is 1-based). A same-receiver write at a DIFFERENT ordinal
        (``self.cam:GetChildren()[2]`` when the credited init was ``[1]`` — codex r3)
        does NOT match -> COUNTED.

    Any gate failure (incl. an un-parseable/ambiguous statement, an empty ``field``, or
    an empty ``cam_receiver``) returns False — the site is counted, fail closed (a
    false-positive is safe; a silent mask is not)."""
    if not field or not cam_receiver:
        return False
    # (receiver anchor) the site's GetChildren receiver must be EXACTLY
    # ``self.<cam_receiver>`` — the dot-form member, never a bare local. The direct
    # no-seed form (``cam_receiver == "Camera.main.transform"``) forms no valid
    # ``self.<member>`` (a dotted receiver makes ``self.Camera.main.transform``, which
    # the resolver never emits), so it correctly never matches -> the rig's own write
    # COUNTS (a safe false-positive).
    if site_receiver.strip() != f"self.{cam_receiver}":
        return False
    # (ordinal anchor) the surviving 1-based ``[k]`` must be the credited 0-based
    # ``GetChild(n)`` + 1.
    if site_ordinal != cam_ordinal + 1:
        return False
    # (statement identity — r7) the credited dead init-write statement shape.
    projected = _rig_code_projection(source)
    start, end = _rig_statement_bounds(projected, site_start)
    if not (start <= site_start and site_end <= end):
        return False
    stmt = projected[start:end]
    m = _rig_exempt_lhs_re(field).match(stmt)
    if m is None:
        return False  # statement does not OPEN with ``self.<field> =``
    rhs_start = start + m.end()
    # (2) the site must BE the whole RHS: receiver flush against the RHS start (only
    # whitespace before it) and only whitespace after the site to the statement end.
    if projected[rhs_start:site_start].strip() != "":
        return False  # a leading operand precedes the site -> not the dead init-write
    if projected[site_end:end].strip() != "":
        return False  # a trailing operand follows the site -> not the dead init-write
    return True


def _check_surviving_child_ordinal(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> list[ContractViolation]:
    """Backstop for relation #2 (child/path-ref), FACT-BASED.

    Reads each script's ``child_ref_resolution`` dict with a None/absent guard.
    PER-SITE fail-close: a survivor lands on a RESOLVED site (a regression) when
    the number of surviving positional ordinals (``S``) EXCEEDS the script's
    unresolved-site budget (``getchild_total - resolved_total``) — the unresolved
    sites can legitimately account for at most that many survivors, so any excess
    must be a resolved site whose ``Find("<name>")`` rewrite was lost. That is
    ``warning`` ``child_ordinal_survivor`` (fail-closed) and covers the
    fully-resolved case (budget 0 -> any survivor fires). Survivors WITHIN budget
    on a partially-resolved script (``resolved_total < getchild_total``) ->
    non-promoting ``info`` ``child_ordinal_coverage_gap`` (a tracked gap). A
    script with NO fact (absent/``None``) -> pure abstain (no row), so pre-field
    fixtures load with zero count drift.
    """
    violations: list[ContractViolation] = []
    for script in scripts:
        r = script.child_ref_resolution
        if not r:
            continue  # no fact (pre-field fixture) — pure abstain, emit nothing
        gt = r.get("getchild_total")
        rt = r.get("resolved_total")
        if gt is None or rt is None or gt <= 0:
            continue
        # RIG-AWARE exemption (POSITIVELY anchored — REDESIGN r3): a surviving
        # positional ordinal that is the EXACT credited dead init-WRITE of a
        # DISCHARGED rig binding (``self.<field> = self.<cam_receiver>:GetChildren()[k]``,
        # ``k == cam_ordinal + 1``) is superseded by the read reroute (Path A) — it is
        # "resolved-but-left-behind", NOT an unresolved child-ref survivor, and the rig
        # fact already bumped ``resolved_total``. The exemption is applied SITE-ALIGNED
        # INSIDE ``_count_surviving_child_ordinals`` (skip AT MOST the one counted
        # credited site), so it can never subtract a site check D did not count. It is
        # gated on (a) the binding PRESENT stamp AND the INDEPENDENT
        # ``_rig_binding_discharged`` re-derivation, AND (b) the carrier's deterministic
        # ``cam_receiver``/``cam_ordinal`` anchors (the AI-stable upstream identity, NOT
        # an output fingerprint). A DIFFERENT-receiver write (codex r8), a SAME-receiver
        # DIFFERENT-ordinal write (codex r3), a bare-``cam`` receiver, the direct
        # no-seed form, a READ survivor, a non-/un-discharged script, or any survivor
        # beyond the single credited write is NOT exempted and still fails closed.
        #
        # TRUST BOUNDARY (codex r3 adjudication — User-Challenge/Taste). The exemption
        # TRUSTS the carrier's ``cam_receiver``/``cam_ordinal`` as the deterministic
        # resolver-fact's proxy; it CANNOT re-derive them from the source (the source
        # can't self-identify which GetChild site the resolver credited), exactly as
        # ``field``/``child`` are trusted anchors above. A well-formed FORGED carrier
        # (receiver+ordinal chosen to match a genuine survivor) could therefore exempt
        # that survivor. The SECURITY BOUND is that the skip is gated on
        # ``_site_is_discharged_rig_dead_write`` (the site is the WHOLE RHS of a
        # ``self.<field> = ...`` WRITE) ANDed with independent discharge below
        # (``_rig_binding_discharged`` -> no raw ``self.<field>`` READ survives). So the
        # WORST a stale/forged carrier can do is mask an INERT dead write to an
        # already-discharged field (dead code whose ``:GetChildren()`` result is
        # discarded) — NEVER a live child-ref regression (a READ, or a write to a
        # different lvalue, fails the gate and stays counted). Forging the carrier
        # requires tampering the internal ``conversion_plan.json``, which the converter
        # writes itself — out of threat model (an attacker who can edit it can edit the
        # output Luau directly). See test_rig_binding_present.py §f-r3-INERT-BOUND.
        exempt: _RigDeadWriteExempt | None = None
        rb = script.rig_binding
        if rb:
            field = str(rb.get("field") or "")
            child = str(rb.get("child") or "")
            cam_receiver = str(rb.get("cam_receiver") or "")
            cam_ordinal_raw = rb.get("cam_ordinal")
            stamp = rb.get("present") is True
            if (
                field
                and child
                and cam_receiver
                and isinstance(cam_ordinal_raw, int)
                and not isinstance(cam_ordinal_raw, bool)
                and stamp
                and _rig_binding_discharged(script.source, field, child)
            ):
                exempt = _RigDeadWriteExempt(
                    field_name=field,
                    cam_receiver=cam_receiver,
                    cam_ordinal=cam_ordinal_raw,
                )
        survivors = _count_surviving_child_ordinals(script.source, exempt)
        if survivors <= 0:
            continue
        unresolved_budget = gt - rt
        # A survivor on a RESOLVED site iff survivors exceed the unresolved
        # budget. Fully-resolved scripts have budget 0, so ANY survivor fires.
        if survivors > unresolved_budget:
            violations.append(
                ContractViolation(
                    check="child_ordinal_survivor",
                    severity="warning",
                    script=script.name,
                    detail=(
                        f"{script.name}: {survivors} positional child "
                        f"ordinal(s) survived the pre-rewrite, exceeding the "
                        f"unresolved-site budget {unresolved_budget} "
                        f"(getchild_total={gt}, resolved_total={rt}); at least "
                        f"one survivor lands on a RESOLVED site whose "
                        f"Find(\"<name>\") lookup should have replaced its "
                        f"GetChild(n) — this is a child-ref regression"
                    ),
                    identity=f"child_ordinal_survivor:{script.name}",
                )
            )
        else:
            violations.append(
                ContractViolation(
                    check="child_ordinal_coverage_gap",
                    severity="info",
                    script=script.name,
                    detail=(
                        f"{script.name}: a positional child ordinal survives on "
                        f"an UNRESOLVED child ref (getchild_total={gt}, "
                        f"resolved_total={rt}); the receiver does not root at the "
                        f"host node (e.g. a foreign Camera.main.transform) so the "
                        f"pre-rewrite could not name it — tracked coverage gap, "
                        f"not a failure"
                    ),
                    identity=f"child_ordinal_coverage_gap:{script.name}",
                )
            )
    return violations


# ---------------------------------------------------------------------------
# Check — rig binding present (BINDING floor for the Camera.main rig retarget)
# ---------------------------------------------------------------------------
#
# The post-transpile ``rifle_rig_retarget_lowering`` discharges an IR-declared
# Camera.main->rig binding by editing the AI's emitted Luau (a per-instance lazy
# resolver method + rewritten consumer reads + a neutralized camera-child write).
# The AI is NOT trusted to preserve that seam, so a DROPPED/reshaped/reverted
# binding must fail LOUD. This check is the FLOOR: it reads the deterministic
# ``rig_binding`` carrier (``{field, child, present}``) ONLY for the IR ANCHOR
# (``field``/``child``), then INDEPENDENTLY scans the final ``script.source`` to
# confirm the binding actually landed — it does NOT trust the lowering's
# ``present`` self-stamp as the gate (OPERATING.md: an actor's self-report is not
# evidence the work happened). ``present`` is a cross-check; a stamp/scan
# DISAGREEMENT also fails (catches a mis-stamp on a reverted edit or a
# stale/forged carrier on a preserve/resume assemble where the lowering never
# re-runs). ``rig_binding=None`` ABSTAINS (no rig fact -> no obligation), so
# non-rifle scripts and pre-field fixtures emit nothing.


# A valid Luau identifier (the shape a resolver-method-name suffix must satisfy).
_RIG_LUAU_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _rig_method_suffix(child: str) -> str:
    """A deterministic VALID-LUAU-IDENTIFIER suffix for the resolver method name
    ``_resolve<suffix>``, derived ONLY from the IR ``child`` name — INDEPENDENT of
    the lowering (mirrors ``rifle_rig_retarget_lowering._method_suffix`` so the
    verifier reconstructs the same method name the lowering emitted, from the same
    deterministic upstream signal, without importing/trusting the lowering's state).

    A child that is already a valid identifier is used verbatim (the happy path);
    otherwise illegal chars map to ``_`` and a short hash of the REAL name is
    appended for collision resistance."""
    if _RIG_LUAU_IDENT_RE.match(child):
        return child
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", child)
    if not sanitized or not re.match(r"[A-Za-z_]", sanitized):
        sanitized = "_" + sanitized
    digest = hashlib.sha1(child.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}_{digest}"


# The non-yielding lifecycle methods the LOWERING abstains on (it cannot land a
# ``task.wait``-bearing resolver call under the synchronous build loop). The
# lowering leaves a ``self.<field>`` read there un-rerouted. Path A re-anchor: the
# VERIFIER does NOT inherit the lowering's lifecycle exemption — a raw read that
# survives in ``Awake``/``Start`` can cache the stale wrong value (the init-write is
# only Tier-2 best-effort-neutralized), so it is a FAIL-CLOSED boundary case (its
# OWN loud row), NOT a silently-safe one. This set is used only to LABEL the read's
# lifecycle context for the boundary scan, not to exempt it.
_RIG_NON_YIELDING_LIFECYCLE: frozenset[str] = frozenset({"Awake", "Start"})

# A code-level ``function <Class>:<method>(`` / ``function <Class>.<method>(``
# declaration — used to read the class name and locate the nearest enclosing
# method for the consumer-read check.
_RIG_FUNCTION_METHOD_RE = re.compile(
    r"\bfunction\s+([A-Za-z_]\w*)[:.]([A-Za-z_]\w*)\s*\("
)

# A surviving camera-child / positional-ordinal WRITE of the field: the RHS
# textually carries a ``GetChild(n)`` / ``GetChildren()[n]`` positional access
# (the camera-child ordinal shape the lowering should have neutralized to ``nil``).
# Anchored on the deterministic ``field`` (the IR projection), code-position
# guarded — NOT an arbitrary AI-output grep. Span-limited to the start of the RHS
# (the assignment text up to the access) so it does not run across statements.
#
# Run over the CODE PROJECTION of the source (``_rig_code_projection``): comment
# and string interiors are already blanked there, and the RHS is whitespace-
# collapsed before the match (``_rig_collapse_code_ws``), so the whole formatting
# tail collapses to one canonical form — ``self.<field> = <recv>:GetChildren()[n]``
# — regardless of inter-token whitespace, ``--`` comments between tokens, or line
# splits. This is the STRUCTURAL close of the surviving-write formatting class
# (round 3): normalization over an ever-growing set of per-junction ``\s*``
# tolerances. The pattern stays minimal (no per-junction ``\s*``) because the
# projection has already removed the whitespace the regex used to chase.
_RIG_ORDINAL_WRITE_TAIL_RE = re.compile(
    r":GetChildren\(\)\[\d+\]|[:.]GetChild\(\d+\)"
)


def _rig_pos_is_real_code(source: str, pos: int) -> bool:
    """A position that is BOTH code (not in a short string / line-comment) AND NOT
    inside a Luau long-bracket string/comment (``[[...]]`` / ``[=[...]=]`` /
    ``--[[...]]``). The single code-position predicate every rig scan uses so a
    token inside a long-bracket literal never counts as live code (codex BLOCKING:
    a fake ``_resolve...`` token inside ``[[ ]]`` must not false-discharge)."""
    return _luau_pos_is_code(source, pos) and not _luau_pos_in_long_bracket(
        source, pos
    )


def _rig_enclosing_method(source: str, pos: int) -> str | None:
    """The method name of the nearest enclosing code-level
    ``function <Class>:<method>(`` declaration before ``pos`` (None at module
    scope)."""
    method: str | None = None
    for m in _RIG_FUNCTION_METHOD_RE.finditer(source):
        if m.start() >= pos:
            break
        if not _rig_pos_is_real_code(source, m.start()):
            continue
        method = m.group(2)
    return method


# A field-access READ of the ``<field>`` token, RECEIVER-AGNOSTIC. The reroute
# only rewrites the AI-STABLE ``self.<field>`` dot read; ANY OTHER field-access of
# the same token that survives is a raw consumption the lowering could not rewrite,
# so the binding is NOT discharged — fail closed. We do NOT enumerate receiver
# shapes (the round-1 blacklist a long tail of exotic receivers evaded). Instead we
# close the whole class: a SURVIVING field-access READ of ``<field>`` fails closed
# REGARDLESS of receiver, with exactly two known-good exceptions (assignment LHS;
# the injected resolver's own internals).
#
# (1) DOT member access: the ``<field>`` token immediately preceded (modulo
#     whitespace/comments) by a ``.`` — ``self.<field>``, ``owner.<field>``,
#     ``(owner).<field>``, ``getOwner().<field>``, ``owners[1].<field>``,
#     ``other.self.<field>``, ``self .<field>``, ``self.\n<field>`` ALL match,
#     because the discriminator is "a ``.`` precedes the token", not the receiver.
def _rig_field_token_re(field: str) -> "re.Pattern[str]":
    return re.compile(r"\b" + re.escape(field) + r"\b")


# (2) STRING-KEY bracket access: ``self[<key>]`` where ``<key>`` is a STATIC Luau
#     string expression that DECODES to exactly ``<field>`` — receiver-agnostic
#     (``(self)[<key>]``, ``owner[<key>]``, ...). The whole finite Luau string-literal
#     grammar is decoded by ``_rig_decode_luau_string_key`` (short strings with escape
#     processing, long-bracket strings, and ``..`` concatenations of these), so an
#     ENCODED key that resolves to the field (``[[weaponSlot]]``, ``["wea\\x70onSlot"]``,
#     ``["wea\\x70on".."Slot"]``) is caught — closing the raw-text-only bypass (codex
#     BLOCKING). A NON-static / dynamic key (``self[var]``, ``self[fn()]``,
#     ``self["a"..var]``) decodes to None and is NEVER flagged (no false-fail).


def _rig_pos_is_assignment_lhs(proj: str, end: int) -> bool:
    """True if the field-access ending at ``end`` is the LHS of an assignment (a
    WRITE — a Tier-2-skipped init-write may legitimately survive; discharge is
    decoupled from neutralize). Read off the position-preserving code PROJECTION
    ``proj``.

    Two LHS shapes are recognized:

      * BARE single-target — the next non-ws CODE char after the access is a single
        ``=`` (not ``==``): ``self.<field> = x``;
      * MULTI-TARGET assignment list — the access is followed (modulo ws) by one or
        more ``, <lvalue>`` targets and then a single ``=``:
        ``self.<field>, other = a, b``. The access sits to the LEFT of the ``=`` in
        an assignment-target list, so it is still a WRITE, not a READ (codex MINOR:
        a multi-assignment write false-failed as a read before this).

    The target-list scan walks ``, <balanced-lvalue>`` segments at bracket depth 0
    up to a bare ``=``; a ``;`` / ``)`` / EOF / a binary ``==`` (not preceded by a
    target separator) ends the search as NON-LHS."""
    a = end
    n = len(proj)
    while a < n and proj[a] in " \t\r\n":
        a += 1
    if a >= n:
        return False
    # Bare single-target ``= `` (not ``==``).
    if proj[a] == "=" and not (a + 1 < n and proj[a + 1] == "="):
        return True
    # Multi-target list: the access must be immediately followed by a ``,`` that
    # opens an assignment-target list ending in a bare ``=``.
    if proj[a] != ",":
        return False
    depth = 0
    while a < n:
        ch = proj[a]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return False  # closed past our expression — not an LHS list
            depth -= 1
        elif depth == 0:
            if ch == "=":
                # A bare ``=`` terminates the target list as an assignment (LHS);
                # a ``==`` is a comparison, so this was NOT an assignment list.
                return not (a + 1 < n and proj[a + 1] == "=")
            if ch in ";\n":
                return False  # statement ended before any ``=`` — not an LHS
        a += 1
    return False


# Luau short-string single-char escapes (``\n`` etc.) -> the literal char they
# denote. ``\xHH`` (hex), ``\ddd`` (1-3 decimal), ``\u{...}`` (unicode), ``\z`` (skip
# following whitespace), and ``\<newline>`` (line continuation) are handled inline by
# the decoder below.
_RIG_LUAU_SIMPLE_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b",
    "f": "\f", "v": "\v", "\\": "\\", '"': '"', "'": "'", "0": "\0",
}


def _rig_decode_short_string(expr: str) -> str | None:
    """Decode a single Luau SHORT string literal (``"..."`` / ``'...'``) to its
    constant value, processing the full finite escape grammar. ``None`` if ``expr``
    (whitespace-trimmed) is not exactly one well-formed short string literal."""
    s = expr.strip()
    if len(s) < 2 or s[0] not in "\"'" or s[-1] != s[0]:
        return None
    quote = s[0]
    body = s[1:-1]
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == quote:
            return None  # an unescaped closing quote mid-body -> not one literal
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        i += 1
        if i >= n:
            return None  # trailing backslash
        e = body[i]
        if e in _RIG_LUAU_SIMPLE_ESCAPES:
            out.append(_RIG_LUAU_SIMPLE_ESCAPES[e])
            i += 1
            continue
        if e == "x":  # ``\xHH`` — exactly two hex digits
            hexd = body[i + 1:i + 3]
            if len(hexd) != 2 or any(c not in "0123456789abcdefABCDEF" for c in hexd):
                return None
            out.append(chr(int(hexd, 16)))
            i += 3
            continue
        if e.isdigit():  # ``\ddd`` — 1-3 decimal digits
            j = i
            while j < n and j < i + 3 and body[j].isdigit():
                j += 1
            val = int(body[i:j])
            if val > 255:
                return None
            out.append(chr(val))
            i = j
            continue
        if e == "u":  # ``\u{...}`` — hex codepoint
            if i + 1 >= n or body[i + 1] != "{":
                return None
            close = body.find("}", i + 2)
            if close == -1:
                return None
            hexd = body[i + 2:close]
            if not hexd or any(c not in "0123456789abcdefABCDEF" for c in hexd):
                return None
            cp = int(hexd, 16)
            if cp > 0x10FFFF:
                return None
            out.append(chr(cp))
            i = close + 1
            continue
        if e == "z":  # ``\z`` — skip following whitespace
            i += 1
            while i < n and body[i] in " \t\r\n\f\v":
                i += 1
            continue
        if e == "\n":  # ``\<newline>`` line continuation -> a literal newline
            out.append("\n")
            i += 1
            continue
        if e == "\r":  # ``\<CR>`` / ``\<CRLF>`` line continuation
            out.append("\n")
            i += 1
            if i < n and body[i] == "\n":
                i += 1
            continue
        return None  # an unknown escape -> not a well-formed literal we decode
    return "".join(out)


def _rig_decode_long_bracket_string(expr: str) -> str | None:
    """Decode a single Luau LONG-BRACKET string literal (``[[...]]`` / ``[=[...]=]``)
    to its RAW content (NO escape processing inside long brackets). Per Luau, a first
    newline immediately after the opener is stripped. ``None`` if ``expr`` (trimmed)
    is not exactly one well-formed long-bracket string."""
    s = expr.strip()
    level = _long_bracket_open_level(s, 0)
    if level is None:
        return None
    open_len = level + 2  # ``[`` + ``level`` ``=`` + ``[``
    closer = "]" + "=" * level + "]"
    if not s.endswith(closer):
        return None
    inner = s[open_len:len(s) - len(closer)]
    # A long-bracket span must contain no earlier closer of the same level (else the
    # literal ended before the trailing ``]...]`` and ``expr`` is not one literal).
    if closer in inner:
        return None
    if inner.startswith("\r\n"):
        inner = inner[2:]
    elif inner.startswith("\n") or inner.startswith("\r"):
        inner = inner[1:]
    return inner


def _rig_split_concat_operands(interior: str) -> list[str] | None:
    """Split a bracket-key ``interior`` on the top-level ``..`` concatenation operator,
    respecting string literals so a ``..`` INSIDE a string is not a split point.
    ``None`` if a ``..`` appears at a structurally ambiguous spot (inside brackets /
    parens — a dynamic expression we will not fold). Returns the list of operand
    substrings (one element when there is no top-level ``..``)."""
    operands: list[str] = []
    start = 0
    i = 0
    n = len(interior)
    while i < n:
        ch = interior[i]
        # Skip a short string literal wholesale (so its interior ``..`` is ignored).
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if interior[j] == "\\":
                    j += 2
                    continue
                if interior[j] == ch:
                    break
                j += 1
            i = j + 1
            continue
        # Skip a long-bracket string wholesale.
        level = _long_bracket_open_level(interior, i)
        if level is not None:
            close = interior.find("]" + "=" * level + "]", i + level + 2)
            i = (n if close == -1 else close + level + 2)
            continue
        if ch == "." and i + 1 < n and interior[i + 1] == ".":
            operands.append(interior[start:i])
            i += 2
            start = i
            continue
        i += 1
    operands.append(interior[start:])
    return operands


def _rig_decode_luau_string_key(expr: str) -> str | None:
    """Fully DECODE a static Luau bracket-key string expression ``expr`` to its
    constant string value, or ``None`` if ``expr`` is NOT a provably-static string
    (a variable, a call, an arithmetic term, or any operand that is not a string
    literal -> dynamic, not provably the field).

    Decodes the FINITE Luau string-literal grammar (codex BLOCKING FIX 1 — closes the
    encoded-key class for good):

      * SHORT strings (``"..."`` / ``'...'``) with full escape processing
        (``\\xHH``, ``\\ddd``, ``\\u{...}``, ``\\n``/``\\t``/...; ``\\z``; line
        continuations) — via ``_rig_decode_short_string``;
      * LONG-BRACKET strings (``[[...]]`` / ``[=[...]=]``) — RAW content, first
        newline stripped — via ``_rig_decode_long_bracket_string``;
      * ``..`` CONCATENATIONS of the above — folded to the joined constant.

    A single non-string-literal operand makes the whole key dynamic -> ``None``."""
    operands = _rig_split_concat_operands(expr)
    if operands is None:
        return None
    parts: list[str] = []
    for operand in operands:
        decoded = _rig_decode_short_string(operand)
        if decoded is None:
            decoded = _rig_decode_long_bracket_string(operand)
        if decoded is None:
            return None  # a non-string-literal operand -> dynamic key, abstain
        parts.append(decoded)
    return "".join(parts)


def _rig_bracket_key_spans(source: str) -> list[tuple[int, int, str]]:
    """Find every code-position ``[ ... ]`` bracket access in ``source`` and return
    ``(open_index, close_index_exclusive, interior)`` for each. The matching ``]`` is
    located by balancing ``[``/``]`` while skipping Luau string literals and
    long-bracket spans, so a ``]`` inside a string never closes the access and a
    ``[[...]]`` key is spanned as a whole. A ``[`` that opens a LONG-BRACKET string
    (a string key like ``self[[weaponSlot]]``) yields the whole long string as the
    interior. Only brackets whose ``[`` is at a real-code position are returned."""
    spans: list[tuple[int, int, str]] = []
    n = len(source)
    i = 0
    while i < n:
        ch = source[i]
        # Skip short strings wholesale.
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == ch or source[j] == "\n":
                    break
                j += 1
            i = j + 1
            continue
        # Skip ``--`` comments wholesale.
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            level = _long_bracket_open_level(source, i + 2)
            if level is not None:
                close = source.find("]" + "=" * level + "]", i + 2)
                i = n if close == -1 else close + level + 2
                continue
            nl = source.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if ch == "[":
            if not _rig_pos_is_real_code(source, i):
                i += 1
                continue
            # ``self[[key]]`` / ``self[=[key]=]`` — the ``[`` at ``i`` ITSELF opens a
            # LONG-BRACKET string (the ``t[[...]]`` call/index-with-long-string form).
            # Span the whole long string as the bracket interior so the key decodes —
            # the directive treats these long-bracket string keys as field reads.
            level = _long_bracket_open_level(source, i)
            if level is not None:
                closer = "]" + "=" * level + "]"
                lclose = source.find(closer, i + level + 2)
                if lclose == -1:
                    i += 1
                    continue
                interior_end = lclose + level + 2
                spans.append((i, interior_end, source[i:interior_end]))
                i = interior_end
                continue
            # Ordinary ``[ <key> ]`` — balance to the matching ``]``, skipping
            # strings/long-brackets/comments inside.
            close = _rig_find_bracket_close(source, i)
            if close == -1:
                i += 1
                continue
            spans.append((i, close + 1, source[i + 1:close]))
            i = close + 1
            continue
        i += 1
    return spans


def _rig_find_bracket_close(source: str, open_idx: int) -> int:
    """The index of the ``]`` matching the ``[`` at ``open_idx``, balancing nested
    ``[``/``]`` and skipping string literals / long-bracket spans / comments. -1 if
    unbalanced."""
    n = len(source)
    depth = 0
    i = open_idx
    while i < n:
        ch = source[i]
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == ch or source[j] == "\n":
                    break
                j += 1
            i = j + 1
            continue
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            level = _long_bracket_open_level(source, i + 2)
            if level is not None:
                cl = source.find("]" + "=" * level + "]", i + 2)
                i = n if cl == -1 else cl + level + 2
                continue
            nl = source.find("\n", i)
            i = n if nl == -1 else nl
            continue
        if ch == "[":
            # A nested long-bracket string key is skipped wholesale (its inner ``]``
            # must not be mistaken for the access closer).
            level = _long_bracket_open_level(source, i)
            if level is not None and i != open_idx:
                cl = source.find("]" + "=" * level + "]", i + level + 2)
                i = n if cl == -1 else cl + level + 2
                continue
            depth += 1
            i += 1
            continue
        if ch == "]":
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue
        i += 1
    return -1


def _rig_has_decoded_field_bracket_read(source: str, field: str) -> bool:
    """True if any code-position ``self[<key>]`` bracket access whose ``<key>`` is a
    STATIC string expression that DECODES to EXACTLY ``<field>`` survives as a READ.

    The unified bracket-key gate (codex BLOCKING FIX 1): every ``[ ... ]`` access is
    found structurally, its key fully DECODED (``_rig_decode_luau_string_key`` — short
    strings with escape processing, long-bracket strings, ``..`` concatenations), and
    flagged iff the decoded value EXACTLY equals ``<field>``. This subsumes BOTH the
    old clean-literal matcher and the old computed-key folder, closing the encoded-key
    class (hex/decimal/unicode escapes, long-bracket keys, escape+concat).

    Two known-good non-firings (mirroring the dot path):

      * ASSIGNMENT LHS (``self[<key>] = x``, incl. multi-target) -> a WRITE may
        legitimately survive (discharge decoupled from neutralize);
      * a NON-static / dynamic key (``self[var]``, ``self[fn()]``, ``self["a"..var]``)
        -> decodes to None, never flagged (flagging it false-fails unrelated accesses).

    Compares to ``<field>`` exactly (not a substring) and is fully GENERIC."""
    if not field:
        return False
    proj = _rig_code_projection(source)
    for open_idx, close_excl, interior in _rig_bracket_key_spans(source):
        decoded = _rig_decode_luau_string_key(interior)
        if decoded is None or decoded != field:
            continue
        if _rig_pos_is_assignment_lhs(proj, close_excl):
            continue  # WRITE LHS -> a Tier-2-skipped write may survive
        return True
    return False


# The resolver's own internal field of the form ``_<field>Cache`` (the memo). Its
# token is ``_<field>Cache``, NOT a bare ``<field>`` field-access (the char before
# ``<field>`` is ``_`` and after is ``C`` — no word boundary), so the
# ``_rig_field_token_re`` word-boundary scan never matches it. Kept as an explicit
# exception only for robustness against a future cache-field rename.
#
# NOTE: there is deliberately NO method-body-span exemption. The injected resolver
# body contains NO bare ``.<field>`` READ — it reads ``self._<field>Cache`` (the
# memo, covered above) and ``FindFirstChild("<child>", true)`` (the CHILD name, not
# the field). A body-span exemption keyed on the resolver-method NAME would
# fail-OPEN: a forged source could plant a decoy ``function tbl:_resolve<suffix>()
# ... owner.<field> ... end`` to hide a real surviving read. The discharge's
# separate ``_rig_resolver_body_is_rig_lookup`` check still requires a real rig
# resolver to be present.
def _rig_is_resolver_internal_access(source: str, start: int, end: int) -> bool:
    """True if the field-access at ``[start, end)`` is the INJECTED resolver's own
    ``_<field>Cache`` memo field — so it is NOT a foreign surviving consumption. (A
    REROUTED read became ``self:_resolve<suffix>()`` and carries NO ``.<field>``
    field-access, so it correctly does not reach here.)"""
    # The ``_<field>Cache`` memo: a ``_`` immediately precedes the token and
    # ``Cache`` immediately follows it.
    return (
        start >= 1
        and source[start - 1] == "_"
        and source[end:end + 5] == "Cache"
    )


def _rig_has_surviving_field_consumption(source: str, field: str) -> bool:
    """True if ANY surviving code-position field-access READ of the ``<field>``
    token survives that the lowering's dot-form READ reroute did NOT (and could not)
    safely rewrite — so the binding is NOT discharged. Path A re-anchor: this is the
    load-bearing discharge gate (replaces the old surviving-WRITE gate).

    RECEIVER-AGNOSTIC (round-1 BLOCKING fix): the discriminator is "a raw
    ``<field>`` field-access READ survived", NOT the receiver shape. We do NOT
    enumerate receiver forms (the blacklist a long tail of exotic receivers —
    ``(owner).<field>``, ``getOwner().<field>``, ``owners[1].<field>``,
    ``other.self.<field>``, ``(self)["<field>"]`` — silently evaded). A field-access
    READ is any occurrence of the exact ``<field>`` token used as a FIELD ACCESS:

      * preceded (modulo whitespace/comments) by a ``.`` (dot member access), OR
      * appearing as a bracket key ``[ <key> ]`` whose ``<key>`` is a STATIC Luau
        string expression that DECODES to exactly ``<field>`` — short strings (escape
        processing), long-bracket strings, and ``..`` concatenations of these.

    Each such access is a SURVIVING CONSUMPTION (→ fail closed) UNLESS it is one of
    the two known-good exceptions:

      1. an ASSIGNMENT LHS (``<recv>.<field> =`` / ``<recv>["<field>"] =``, not
         ``==``) — a WRITE; a Tier-2-skipped init-write may legitimately survive
         (discharge is decoupled from neutralize);
      2. the injected resolver's OWN ``_<field>Cache`` memo. (A rerouted read became
         ``self:_resolve<suffix>()`` and contains NO ``.<field>`` field-access, so it
         correctly never matches.) There is deliberately NO method-body-span
         exemption: it would fail-OPEN on a decoy ``_resolve<suffix>`` body planted
         to hide a foreign ``.<field>`` read.

    The RECEIVER is ignored entirely (``self``, ``owner``, ``(owner)``,
    ``getOwner()``, ``owners[1]``, ``other.self``, parenthesized, aliased — ALL fail
    closed identically), closing the whole receiver-form class with no enumeration."""
    # Position-preserving code projection (comment/string interiors blanked) so the
    # preceding-``.`` and assignment-LHS checks see CODE, not formatting or strings.
    proj = _rig_code_projection(source)

    # (1) DOT member access: a ``.`` immediately precedes the ``<field>`` token.
    for m in _rig_field_token_re(field).finditer(source):
        start = m.start()
        end = m.end()
        if not _rig_pos_is_real_code(source, start):
            continue
        # Walk back over whitespace AND blanked comment chars (projection) to the
        # preceding CODE char; a ``.`` there makes this a dot field-access.
        j = start - 1
        while j >= 0 and proj[j] in " \t\r\n":
            j -= 1
        if j < 0 or proj[j] != ".":
            continue  # not a dot member access (a bare identifier / write target name)
        if _rig_pos_is_assignment_lhs(proj, end):
            continue  # WRITE LHS -> a Tier-2-skipped write may survive
        if _rig_is_resolver_internal_access(source, start, end):
            continue  # the injected resolver's own ``_<field>Cache`` memo
        return True

    # (2) STRING-KEY bracket access ``self[<key>]`` — UNIFIED decode-then-compare
    # over the full finite Luau string-literal grammar (short strings with escape
    # processing, long-bracket string keys, ``..`` concatenations). A key that
    # DECODES to exactly ``<field>`` is a surviving READ (write-LHS exempt); a
    # dynamic / non-static key decodes to None and is never flagged. This subsumes
    # the old clean-literal matcher AND the old computed-key folder, closing the
    # encoded-key bypass class (codex BLOCKING FIX 1).
    if _rig_has_decoded_field_bracket_read(source, field):
        return True

    return False


# Continuation HEADS — a next-line first token that continues the RHS. Beyond the
# operator-led heads (``and``/``or``/``..``/arithmetic/comparison) this admits the
# FULL Luau POSTFIX-continuation class so a write split before a trailing access is
# spanned: ``[`` (index — ``expr\n[1]``), ``(`` (call — ``expr\n(args)``), ``.``
# (member — ``expr\n.field``), ``:`` (method — ``expr\n:m()``). Closing the postfix
# class as a whole (not just ``[``) avoids whack-a-moling ``(``/``.`` next round.
_RIG_CONTINUATION_HEAD_RE = re.compile(
    r"^(and|or|not|\.\.|[.:+\-*/%<>=~^#({\[]|\bthen\b)"
)
_RIG_CONTINUATION_TAIL_RE = re.compile(
    r"(\b(and|or|not)|\.\.|[.:+\-*/%<>=~^,({\[]|=)\s*$"
)

# A next-line first token that begins a NEW statement (so a ``(``/``[`` postfix head
# must NOT swallow it). Admitting ``(``/``[`` as continuation heads risks reaching
# across a following parenthesized-expression statement or the ``a = b\n(f)()``
# ambiguity; this boundary check stops the span there. Bias: when ambiguous we keep
# scanning (fail-closed — over-detecting a survivor yields ``discharged=False`` ->
# a violation row, the SAFE direction for this verifier; a MISSED survivor is unsafe).
_RIG_STATEMENT_BOUNDARY_RE = re.compile(
    r"^(local\b|function\b|return\b|end\b|if\b|for\b|while\b|repeat\b|until\b"
    r"|do\b|else\b|elseif\b|break\b|self\.\w+\s*=(?!=)|\w+\s*=(?!=))"
)


def _rig_line_continues(source: str, start: int, nl_pos: int) -> bool:
    """True if the RHS logical expression continues past the newline at ``nl_pos``
    (bracket depth 0): the text from ``start`` to ``nl_pos`` ends with a
    binary/continuation operator, OR the next non-blank line begins with one.
    Mirrors the lowering's ``_line_continues`` so the verifier's RHS span is
    continuation-aware (codex BLOCKING: a multiline surviving ordinal write must be
    spanned, not truncated at the first newline), AND closes the postfix-head class
    (``[``/``(``/``.``/``:``) so a write split before a trailing index/call/member is
    spanned — guarded by a statement-boundary check so a ``(``/``[`` head does not
    swallow a following statement."""
    before = source[start:nl_pos]
    if _RIG_CONTINUATION_TAIL_RE.search(before):
        return True
    j = nl_pos + 1
    n = len(source)
    while j < n and source[j] in " \t\r\n":
        j += 1
    if j >= n:
        return False
    nxt = source[j:j + 16]
    # A next line that clearly opens a new statement is NOT a continuation — this
    # bounds the span so an admitted ``(``/``[`` head cannot reach across a following
    # statement (over-reach guard).
    if _RIG_STATEMENT_BOUNDARY_RE.match(nxt):
        return False
    return _RIG_CONTINUATION_HEAD_RE.match(nxt) is not None


def _rig_statement_rhs_end(source: str, start: int) -> int:
    """The end char index of the RHS expression beginning at ``start``, balanced
    across (), [], {} and short strings, terminating at the end of the logical
    statement (a code-level newline at bracket depth 0, or EOF). Multiline-aware so
    a multi-line camera-child RHS is fully spanned. Mirrors the lowering's
    ``_statement_rhs_end``."""
    i = start
    n = len(source)
    depth = 0
    while i < n:
        ch = source[i]
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            if depth == 0:
                break
            depth -= 1
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                c = source[i]
                if c == "\\":
                    i += 2
                    continue
                if c == quote or c == "\n":
                    break
                i += 1
            i += 1
            continue
        if ch == "\n" and depth == 0:
            if _rig_line_continues(source, start, i):
                i += 1
                continue
            break
        if ch == "-" and i + 1 < n and source[i + 1] == "-" and depth == 0:
            break  # a trailing comment -> RHS ends before it
        i += 1
    return i


def _rig_code_projection(source: str) -> str:
    """A position-PRESERVING projection of ``source`` that keeps ONLY code and blanks
    every comment / string / long-bracket span — INCLUDING the ``--`` / quote / long-
    bracket delimiters themselves — to spaces (newlines kept as newlines so statement
    structure, continuation, and the over-reach boundary check are unchanged). Same
    length as ``source`` so the char-index machinery (RHS span, continuation scan)
    maps 1:1.

    This is the STRUCTURAL normalization the round-3 fix is built on: once a comment
    span (delimiter and all) is whitespace, a ``--`` between tokens can never
    truncate an RHS span and a token inside a string can never match. Walks the
    source with the SAME state machine as ``_luau_pos_is_code`` (line/block comments,
    long strings, short strings) so the projection agrees exactly with every other
    rig scan's notion of code; a non-code char is blanked, a non-code newline kept."""
    chars = list(source)
    n = len(source)

    def _blank(lo: int, hi: int) -> None:
        for k in range(lo, min(hi, n)):
            if chars[k] != "\n":
                chars[k] = " "

    i = 0
    while i < n:
        ch = source[i]
        # Comment — line or block (``--`` then optional long bracket).
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            level = _long_bracket_open_level(source, i + 2)
            if level is not None:
                close = source.find("]" + "=" * level + "]", i + 2)
                end = n if close == -1 else close + level + 2
                _blank(i, end)
                i = end
                continue
            nl = source.find("\n", i)
            end = n if nl == -1 else nl  # keep the terminating newline
            _blank(i, end)
            i = end
            continue
        # Long string ``[[ ]]`` / ``[=[ ]=]`` (not a comment).
        level = _long_bracket_open_level(source, i)
        if level is not None:
            close = source.find("]" + "=" * level + "]", i + level + 2)
            end = n if close == -1 else close + level + 2
            _blank(i, end)
            i = end
            continue
        # Short string ``"..."`` / ``'...'`` (``\\`` escapes).
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == ch or source[j] == "\n":
                    break
                j += 1
            end = j + 1 if j < n and source[j] == ch else j
            _blank(i, end)
            i = end
            continue
        i += 1
    return "".join(chars)


def _rig_collapse_code_ws(rhs: str) -> str:
    """Collapse inter-token whitespace in an already-projected RHS so the canonical
    tail pattern matches regardless of the original formatting: runs of whitespace
    (spaces/newlines — the projection left no comments) become empty where they sit
    between two punctuation/identifier-boundary characters, and a single space only
    where two identifier characters would otherwise fuse. The tail regex matches
    ``:GetChildren()[n]`` / ``:GetChild(n)`` which is pure punctuation+name with no
    internal identifier-identifier boundary, so collapsing to empty between
    non-identifier neighbours suffices to canonicalize ``self.cam :  GetChildren ( )
    [ 1 ]`` -> ``self.cam:GetChildren()[1]``."""
    out: list[str] = []
    i = 0
    n = len(rhs)
    while i < n:
        ch = rhs[i]
        if ch in " \t\r\n":
            j = i
            while j < n and rhs[j] in " \t\r\n":
                j += 1
            prev = out[-1] if out else ""
            nxt = rhs[j] if j < n else ""
            # Keep a single space only when removing it would fuse two identifier
            # characters into one token (``a b`` -> ``ab``); drop it otherwise.
            if prev and nxt and _rig_is_ident_char(prev) and _rig_is_ident_char(nxt):
                out.append(" ")
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _rig_is_ident_char(c: str) -> bool:
    """True if ``c`` can be part of a Luau identifier (alphanumeric or ``_``)."""
    return c.isalnum() or c == "_"


def _rig_has_surviving_ordinal_write(source: str, field: str) -> bool:
    """True if a code-position ``self.<field> = <... GetChild(n) | GetChildren()[n] ...>``
    positional-ordinal WRITE survives — the camera-child shape the lowering should
    have neutralized to ``nil``. Anchored on the deterministic ``field``; the RHS
    is balanced to the END of the logical statement (multiline-aware), so a
    multi-line camera-child RHS (``self.<field> =\\n  self.cam:GetChildren()[1]``)
    is fully spanned and a later unrelated statement's ordinal does not leak in.

    The scan runs over the CODE PROJECTION of ``source`` (comment/string interiors
    blanked, position-preserving) and the spanned RHS is whitespace-collapsed before
    the tail match, so the whole whitespace/comment/line-split formatting class
    collapses to the one canonical access form — closing it structurally rather than
    per-junction (round-3 directive)."""
    projected = _rig_code_projection(source)
    assign_re = re.compile(r"self\." + re.escape(field) + r"\s*=(?!=)")
    for m in assign_re.finditer(projected):
        # The ``=`` established we are mid-statement, so the value may begin on the
        # NEXT line (``self.<field> =\n  self.cam:GetChildren()[1]``). Advance past
        # leading whitespace/newlines to the first real RHS token before spanning,
        # so a value-on-next-line write is not truncated at the leading newline.
        rhs_start = m.end()
        while rhs_start < len(projected) and projected[rhs_start] in " \t\r\n":
            rhs_start += 1
        rhs_end = _rig_statement_rhs_end(projected, rhs_start)
        rhs = _rig_collapse_code_ws(projected[rhs_start:rhs_end])
        if _RIG_ORDINAL_WRITE_TAIL_RE.search(rhs):
            return True
    return False


def _rig_method_body_end(source: str, decl_start: int) -> int:
    """The char index just past the matching closing ``end`` of the
    ``function ... _resolve<suffix>(`` method declared at ``decl_start`` (block-
    keyword balanced over code positions, long-bracket strings/comments skipped
    wholesale). EOF if the method is unterminated. Used to bound the rig-lookup
    body scan to THIS method, so a marker elsewhere in the file does not satisfy a
    foreign same-named stub."""
    i = decl_start
    n = len(source)
    block = 0  # block-keyword nesting; the ``function`` declaration opens level 1
    seen_open = False
    # Mirrors S1's proven ``_structural_balance_ok`` block-keyword set: ``function``/
    # ``do``/``then``/``repeat`` OPEN a scope; ``end``/``until`` CLOSE it. ``elseif``
    # is a CLOSER too — an ``if a then ... elseif b then ... end`` chain has multiple
    # ``then`` openers but ONE ``end``, so ``elseif`` decrements to cancel its OWN
    # upcoming ``then``'s increment (net 0 for the whole chain). ``else`` follows no
    # ``then`` (pure +0 continuation), so it is not a token here. Without this,
    # an ``elseif`` chain over-counts openers and the span overruns the method's
    # closing ``end`` into later unrelated code (codex round-4 BLOCKING).
    opener_re = re.compile(r"\b(function|do|then|repeat)\b")
    closer_re = re.compile(r"\b(end|until|elseif)\b")
    while i < n:
        ch = source[i]
        # Skip Luau long-bracket comments/strings wholesale.
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            j = i + 2
            level = _long_bracket_open_level(source, j)
            if level is not None:
                close = source.find("]" + "=" * level + "]", j)
                i = n if close == -1 else close + level + 2
                continue
            nl = source.find("\n", j)
            i = n if nl == -1 else nl + 1
            continue
        if ch == "[":
            level = _long_bracket_open_level(source, i)
            if level is not None:
                close = source.find("]" + "=" * level + "]", i + level + 2)
                i = n if close == -1 else close + level + 2
                continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == "\n":
                    break
                i += 1
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            om = opener_re.match(source, i)
            if om and (i == 0 or not (source[i - 1].isalnum() or source[i - 1] == "_")):
                block += 1
                seen_open = True
                i = om.end()
                continue
            cm = closer_re.match(source, i)
            if cm and (i == 0 or not (source[i - 1].isalnum() or source[i - 1] == "_")):
                block -= 1
                i = cm.end()
                if seen_open and block == 0:
                    return i
                continue
            # advance past the whole identifier so an embedded ``end`` substring
            # (``send``/``endpoint``) is never matched as a keyword.
            k = i
            while k < n and (source[k].isalnum() or source[k] == "_"):
                k += 1
            i = k
            continue
        i += 1
    return n


def _rig_resolver_body_is_rig_lookup(source: str, suffix: str, child: str) -> bool:
    """True if a code-position ``function <Class>:_resolve<suffix>(`` method exists
    WHOSE BODY is the lowering's rig resolver — i.e. the distinctive
    ``_MainCameraRig`` rig lookup appears as LIVE code inside that method's span:
    BOTH ``:GetAttribute("_MainCameraRig")`` AND ``FindFirstChild("<child>", true)``
    (the real S1 emit, anchored on the deterministic ``child``).

    Codex BLOCKING: a FOREIGN same-named stub (``return nil`` / a wrong lookup) +
    a forged/stale ``present=True`` must NOT count as discharged. Requiring the rig-
    lookup body as live code inside the method span is the fail-closed floor — a
    bare same-named method without that body does not discharge."""
    decl_re = re.compile(
        r"\bfunction\s+[A-Za-z_]\w*[:.]_resolve" + re.escape(suffix) + r"\s*\("
    )
    rig_attr = ':GetAttribute("_MainCameraRig")'
    find_child = f'FindFirstChild("{child}", true)'
    for m in decl_re.finditer(source):
        if not _rig_pos_is_real_code(source, m.start()):
            continue
        body_end = _rig_method_body_end(source, m.start())
        # The markers must each appear at a code position WITHIN this method body.
        if _rig_span_code_contains(source, m.start(), body_end, rig_attr) and (
            _rig_span_code_contains(source, m.start(), body_end, find_child)
        ):
            return True
    return False


def _rig_span_code_contains(
    source: str, span_start: int, span_end: int, token: str
) -> bool:
    """True if ``token`` appears at a code position (not comment/string/long-
    bracket) within ``[span_start, span_end)``."""
    idx = source.find(token, span_start)
    while idx != -1 and idx < span_end:
        if _rig_pos_is_real_code(source, idx):
            return True
        idx = source.find(token, idx + 1)
    return False


def _rig_binding_discharged(source: str, field: str, child: str) -> bool:
    """INDEPENDENT, code-position-aware derivation (the LOAD-BEARING authority):
    is ``field``'s binding discharged via the rig retarget in THIS final source?
    Derived from the SOURCE alone — anchored ONLY on the deterministic IR
    ``field``/``child`` (NOT the lowering's ``present`` self-stamp, NOT an
    arbitrary AI-output token, and it never REPAIRS).

    PATH A re-anchor — discharge keys on the consumer-READ reroute (the AI-STABLE
    member access), NOT on the AI-VOLATILE write/ordinal shape. True IFF, over code
    positions:

      (1) the injected per-instance resolver landed — the method
          ``function <Class>:_resolve<suffix>(`` exists WHOSE BODY is the rig
          resolver (the distinctive ``_MainCameraRig`` lookup as LIVE code) AND
          >=1 ``self:_resolve<suffix>(`` CALL exists; AND
      (2) **NO raw consumption of the binding survives** — no bare ``self.<field>``
          dot-form READ (in ANY method, incl. ``Awake``/``Start`` and a shadowed
          ``self``), no bracket-index ``self["<field>"]``, and no NON-``self``
          receiver read (``<Class>.<field>`` / ``owner.<field>`` / a receiver-alias
          ``p.<field>``). This is the discharge gate (``_rig_has_surviving_field_
          consumption``); every surviving form FAILS CLOSED (the Path A generality
          boundary — never silently passes).

    The 'no surviving camera-child ordinal WRITE' clause is DROPPED from the
    discharge gate (Path A re-anchor). On the 5 real RHS write shapes there may be
    no positional ordinal to anchor, and the write is dead data once the reads are
    rerouted, so a surviving init-write must NOT fail discharge — a script whose
    init-write was Tier-2-SKIPPED but whose reads are all rerouted MUST discharge
    True. The surviving-ordinal-WRITE scan is retained as a best-effort SECONDARY
    DIAGNOSTIC only (``_rig_has_surviving_ordinal_write``), not a PASS/FAIL gate.

    ``suffix`` is reconstructed from ``child`` by the same deterministic
    sanitization the lowering uses, so the method name matches whatever the
    lowering emitted (verbatim for a plain child name; a sanitized+hashed suffix
    for a child with spaces/special chars). This is the §2 'loud-check-against-the-
    fact' — it confirms the LOWERING's deterministic READ reroute actually LANDED,
    independent of the lowering's belief, so a mis-stamp / reverted edit / stale
    resume carrier (or a FOREIGN same-named stub) is caught, AND an unsupported
    boundary form fails loud."""
    if not field or not child:
        return False
    suffix = _rig_method_suffix(child)
    call = f"self:_resolve{suffix}("
    # (1a) the resolver METHOD declaration is present at a code position AND its
    # BODY is the rig resolver (the distinctive ``_MainCameraRig`` lookup as live
    # code) — a foreign same-named stub does NOT discharge (codex BLOCKING).
    if not _rig_resolver_body_is_rig_lookup(source, suffix, child):
        return False
    # (1b) >=1 ``self:_resolve<suffix>(`` CALL (distinct from the declaration —
    # the declaration is ``function <Class>:_resolve<suffix>(``, not ``self:...``).
    if not _rig_code_contains(source, call):
        return False
    # (2) no surviving raw consumption of the binding (the LOAD-BEARING Path A gate):
    # a dot-form ``self.<field>`` read (incl. lifecycle/shadowed), a bracket-index
    # ``self["<field>"]``, or a NON-``self`` receiver read — each a fail-closed
    # boundary the read-reroute cannot safely rewrite.
    if _rig_has_surviving_field_consumption(source, field):
        return False
    return True


def _rig_code_contains(source: str, token: str) -> bool:
    """True if ``token`` appears at a code position (not in a comment/string and
    NOT inside a Luau long-bracket literal — codex BLOCKING)."""
    idx = source.find(token)
    while idx != -1:
        if _rig_pos_is_real_code(source, idx):
            return True
        idx = source.find(token, idx + 1)
    return False


def _check_rig_binding_present(
    topology: TopologyArtifact,
    scripts: list[RbxScript],
) -> list[ContractViolation]:
    """Fail-closed assertion that every IR-declared rig-retarget binding was
    DISCHARGED — derived INDEPENDENTLY from the final ``script.source``, NOT from
    the lowering's ``present`` self-stamp. The carrier's ``field``/``child`` are
    the deterministic IR ANCHOR (the resolver's projection of the upstream C#
    field + parsed MainCamera-child); the check then SCANS the real final source
    to confirm that anchored binding actually landed. ``present`` is a cross-check,
    not the gate — PASS requires ``discharged AND stamp``; a stamp/scan
    DISAGREEMENT also fails (catches a mis-stamp: a syntax-revert that reverted the
    edit, or a stale/forged carrier on a preserve/resume assemble where the
    lowering never re-ran). ``rig_binding=None`` ABSTAINS (no rig fact)."""
    violations: list[ContractViolation] = []
    for script in scripts:
        rb = script.rig_binding
        if not rb:
            continue  # no rig fact -> abstain (pre-field fixtures, non-rifle scripts)
        field = str(rb.get("field") or "")
        child = str(rb.get("child") or "")
        discharged = _rig_binding_discharged(script.source or "", field, child)  # INDEPENDENT
        stamp = rb.get("present") is True
        if discharged and stamp:
            continue  # PASS — the independent scan AND the cross-check agree
        violations.append(
            ContractViolation(
                check="rig_binding_present",
                severity="warning",
                script=script.name,
                detail=(
                    f"{script.name}: the IR-declared rig retarget binding "
                    f"(field {field!r} -> _MainCameraRig child {child!r}) was NOT "
                    f"confirmed in the lowered output "
                    f"(source-scan discharged={discharged}, lowering-stamp={stamp}). "
                    f"Either the deterministic rebind was dropped/reshaped/reverted, "
                    f"or the carrier disagrees with the source (mis-stamp); the rifle "
                    f"would bind to the wrong body or nil."
                ),
                identity=f"rig_binding_present:{script.name}:{field}",
            )
        )
    return violations
