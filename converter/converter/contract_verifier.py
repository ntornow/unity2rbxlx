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


def _count_surviving_child_ordinals(source: str) -> int:
    """Count POSITIONAL child-ordinal survivor SITES in ``source`` — the
    adjacent shape ``<recv>:GetChildren()[N]`` (simple OR method-call receiver)
    plus the across-lines factored shape (``local v = X:GetChildren()`` then a
    later ``v[<int>]``). Per-site (not boolean) so the backstop can fail-close
    when survivors exceed the script's unresolved-site budget. Code-position
    aware; counts each factored ``local v`` chain ONCE. Survivors whose receiver
    roots at a known-safe ENGINE GLOBAL (``workspace``/``game``/...) are EXCLUDED
    — they are engine-tree iterations, not unresolved child-ref ordinals."""
    count = 0
    for m in _GETCHILDREN_INDEX_ANY_RE.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        if _receiver_roots_at_engine_global(m.group(1)):
            continue  # engine-global iteration — not a child-ref survivor
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
        survivors = _count_surviving_child_ordinals(script.source)
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


# The non-yielding lifecycle methods the rewrite ABSTAINS on (it cannot land a
# ``task.wait``-bearing resolver call under the synchronous build loop). A bare
# ``self.<field>`` READ inside one of these is NOT a surviving consumer — the
# yield-guard intentionally leaves it (it reads the neutralized ``nil`` safely),
# so it does not count against discharge. Mirrors the lowering's closed list.
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
_RIG_ORDINAL_WRITE_TAIL_RE = re.compile(
    r":GetChildren\(\)\s*\[\s*\d+\s*\]|[:.]GetChild\(\s*\d+\s*\)"
)


def _rig_enclosing_method(source: str, pos: int) -> str | None:
    """The method name of the nearest enclosing code-level
    ``function <Class>:<method>(`` declaration before ``pos`` (None at module
    scope)."""
    method: str | None = None
    for m in _RIG_FUNCTION_METHOD_RE.finditer(source):
        if m.start() >= pos:
            break
        if not _luau_pos_is_code(source, m.start()):
            continue
        method = m.group(2)
    return method


def _rig_has_surviving_field_read(source: str, field: str) -> bool:
    """True if a bare ``self.<field>`` consumer READ survives at a code position
    in a YIELD-SAFE method — meaning the lowering did NOT rewrite it through the
    resolver. Excludes (a) a member-tail ``self`` (``x.self.<field>``), (b) the
    assignment LHS (``self.<field> =``, not ``==``), and (c) a read inside a
    non-yielding lifecycle method (``Awake``/``Start``) — the yield-guard leaves
    those reading the neutralized ``nil``, which is safe and not a consumer."""
    pattern = re.compile(r"self\." + re.escape(field) + r"\b")
    for m in pattern.finditer(source):
        start = m.start()
        if not _luau_pos_is_code(source, start):
            continue
        j = start - 1
        while j >= 0 and source[j] in " \t":
            j -= 1
        if j >= 0 and source[j] == ".":
            continue  # x.self.<field> -> not a bare read
        a = m.end()
        while a < len(source) and source[a] in " \t":
            a += 1
        if a < len(source) and source[a] == "=" and not (
            a + 1 < len(source) and source[a + 1] == "="
        ):
            continue  # assignment LHS -> not a read
        if _rig_enclosing_method(source, start) in _RIG_NON_YIELDING_LIFECYCLE:
            continue  # non-yielding lifecycle read -> abstained, not a consumer
        return True
    return False


def _rig_has_surviving_ordinal_write(source: str, field: str) -> bool:
    """True if a code-position ``self.<field> = <... GetChild(n) | GetChildren()[n] ...>``
    positional-ordinal WRITE survives — the camera-child shape the lowering should
    have neutralized to ``nil``. Anchored on the deterministic ``field``; the RHS
    is read up to the end of the logical line (newline at the top level), so a
    later unrelated statement's ordinal does not leak in."""
    assign_re = re.compile(r"self\." + re.escape(field) + r"\s*=(?!=)")
    for m in assign_re.finditer(source):
        if not _luau_pos_is_code(source, m.start()):
            continue
        rhs_start = m.end()
        nl = source.find("\n", rhs_start)
        rhs = source[rhs_start: nl if nl != -1 else len(source)]
        if _RIG_ORDINAL_WRITE_TAIL_RE.search(rhs):
            return True
    return False


def _rig_binding_discharged(source: str, field: str, child: str) -> bool:
    """INDEPENDENT, code-position-aware derivation (the LOAD-BEARING authority):
    is ``field``'s binding discharged via the rig retarget in THIS final source?
    Derived from the SOURCE alone — anchored ONLY on the deterministic IR
    ``field``/``child`` (NOT the lowering's ``present`` self-stamp, NOT an
    arbitrary AI-output token, and it never REPAIRS). True IFF, over code positions:

      (1) the injected per-instance resolver landed — the method
          ``function <Class>:_resolve<suffix>(`` exists AND >=1
          ``self:_resolve<suffix>(`` CALL exists AND NO bare ``self.<field>``
          consumer READ survives (the reads were rewritten through the resolver);
          AND
      (2) the original ordinal/camera-child WRITE is gone — no surviving
          ``self.<field> = <... GetChild(n) | GetChildren()[n] ...>`` assignment.

    ``suffix`` is reconstructed from ``child`` by the same deterministic
    sanitization the lowering uses, so the method name matches whatever the
    lowering emitted (verbatim for a plain child name; a sanitized+hashed suffix
    for a child with spaces/special chars). This is the §2 'loud-check-against-the-
    fact' — it confirms the LOWERING's deterministic binding actually LANDED,
    independent of the lowering's belief, so a mis-stamp / reverted edit / stale
    resume carrier is caught."""
    if not field or not child:
        return False
    suffix = _rig_method_suffix(child)
    decl_re = re.compile(
        r"\bfunction\s+[A-Za-z_]\w*[:.]_resolve" + re.escape(suffix) + r"\s*\("
    )
    call = f"self:_resolve{suffix}("
    # (1a) the resolver METHOD declaration is present at a code position.
    if not any(
        _luau_pos_is_code(source, m.start()) for m in decl_re.finditer(source)
    ):
        return False
    # (1b) >=1 ``self:_resolve<suffix>(`` CALL (distinct from the declaration —
    # the declaration is ``function <Class>:_resolve<suffix>(``, not ``self:...``).
    if not _rig_code_contains(source, call):
        return False
    # (1c) no surviving bare consumer READ of ``self.<field>``.
    if _rig_has_surviving_field_read(source, field):
        return False
    # (2) the ordinal/camera-child WRITE was neutralized (no surviving positional
    # ordinal write of the field).
    if _rig_has_surviving_ordinal_write(source, field):
        return False
    return True


def _rig_code_contains(source: str, token: str) -> bool:
    """True if ``token`` appears at a code position (not in a comment/string)."""
    idx = source.find(token)
    while idx != -1:
        if _luau_pos_is_code(source, idx):
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
