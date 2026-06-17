"""
storage_classifier.py -- Phase 4a.5: server/client/replicated container assignment.

Unity has no networking model; Roblox replicates between server and client.
Every module needs an explicit container. This module answers, per script:

1. Does the server need it?
2. Does the client need it?
3. Do both need it?

Output: a StoragePlan with a concrete parent_path on each RbxScript.
rbxlx_writer.py and luau_place_builder.py route by parent_path.

Phase 2a slice 7 (2026-05-30): when ``topology_inputs`` is provided
(every non-legacy run), ``_decide_script_container`` consults the
topology-driven decision tree below FIRST. The legacy six-rule path
is preserved as a per-script fallback for the unconstrained-helper
contract (see ``_decide_script_container_from_topology`` docstring).

Decision tree (first-match-wins) when ``topology_inputs`` is provided:

  1. ``lifecycle_role == "character_attached"`` -> StarterCharacterScripts
  2. ``lifecycle_role == "loader"`` -> ReplicatedFirst
  3. ``reachability_required_container`` present -> that container
     (``"__excluded__"`` sentinel hoists to ReplicatedStorage)
  4. ModuleScript -> route by ``topology_inputs.domains`` + caller_graph
     * any client-domain caller -> ReplicatedStorage
     * all callers server-domain -> ServerStorage
     * orphan / unknown -> ReplicatedStorage (conservative)
  5. LocalScript -> StarterPlayerScripts
  6. Script -> ServerScriptService

Legacy six-rule (slice-5 byte-parity) path, used when:
  * ``topology_inputs is None`` (legacy mode / probe flag), OR
  * topology says this script has no signal AND
    ``transpile_ran is False`` (no-transpile resume; degraded
    reachability per the unconstrained-helper contract), OR
  * ``script_id_by_name`` cannot resolve ``s.name`` (degraded service
    contract on stem/class_name collisions).

Hard constraints (enforced AFTER the decision tree, defense in depth):
  * LocalScript in ServerScriptService -> ConstraintViolation
  * ReplicatedFirst + ModuleScript -> ConstraintViolation

Ambiguity (legacy path only): default to ReplicatedStorage. Misplacing
into ReplicatedStorage degrades security; misplacing into ServerStorage
breaks the game.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, TYPE_CHECKING

from core.roblox_types import RbxScript

if TYPE_CHECKING:
    from converter.scene_runtime_topology.module_domain import TopologyInputs

log = logging.getLogger(__name__)


# Parent-path strings that rbxlx_writer and luau_place_builder recognize.
SERVER_SCRIPT_SERVICE = "ServerScriptService"
SERVER_STORAGE = "ServerStorage"
REPLICATED_STORAGE = "ReplicatedStorage"
REPLICATED_FIRST = "ReplicatedFirst"
STARTER_PLAYER_SCRIPTS = "StarterPlayer.StarterPlayerScripts"
STARTER_CHARACTER_SCRIPTS = "StarterPlayer.StarterCharacterScripts"
STARTER_GUI = "StarterGui"


# Client-only API patterns. FALLBACK-PATH ONLY: the topology path classifies
# via ``infer_module_domains`` and does NOT consume these. They guard the
# legacy fallback so a Script using ``LocalPlayer`` (where ``LocalPlayer`` is
# nil on the server) is not routed to ServerScriptService. See
# ``_scripts_with_client_apis`` / ``_scripts_with_server_apis``.
_CLIENT_ONLY_PATTERNS = [
    r"Players\.LocalPlayer",
    r'GetService\(["\']Players["\']\)\.LocalPlayer',
    r'GetService\(["\']UserInputService["\']\)',
    r"UserInputService",
    r"workspace\.CurrentCamera",
    r'GetService\(["\']StarterGui["\']\)',
    r"LocalPlayer\.Character",
    r"\.PlayerGui",
    r"mouse\.Hit",
    r"mouse\.Target",
    r'GetService\(["\']ContextActionService["\']\)',
    r'GetService\(["\']GuiService["\']\)',
]

# APIs that ONLY work on the server. FALLBACK-PATH ONLY (see
# ``_scripts_with_server_apis``).
_SERVER_ONLY_PATTERNS = [
    r"\.OnServerEvent",
    r":FireClient\(",
    r":FireAllClients\(",
    r'GetService\(["\']DataStoreService["\']\)',
    r'GetService\(["\']MessagingService["\']\)',
    r'GetService\(["\']ServerStorage["\']\)',
    r'GetService\(["\']ServerScriptService["\']\)',
]

# Name hints for ReplicatedFirst scripts (loaders / splash screens that must
# run before full replication). Public: `scene_runtime_planner` imports it as
# the single source of truth for the loader-name heuristic.
REPLICATED_FIRST_HINTS = re.compile(
    r"(?i)(loading|loader|boot|bootstrap|splash|preload|intro)"
)

# Name hints for server-secret templates (prefabs that genuinely must be hidden
# from clients — admin tools, cheat-detection prefabs, etc.).
_SERVER_SECRET_HINTS = re.compile(
    r"(?i)(admin|secret|server(only|_only|-only)?|cheat)"
)


@dataclass
class StoragePlan:
    """Explicit per-script and per-template container assignments."""

    # Scripts
    server_scripts: list[str] = field(default_factory=list)
    client_scripts: list[str] = field(default_factory=list)
    character_scripts: list[str] = field(default_factory=list)
    replicated_first_scripts: list[str] = field(default_factory=list)
    shared_modules: list[str] = field(default_factory=list)
    server_modules: list[str] = field(default_factory=list)

    # Templates (forward-looking; populated when templates_manifest is wired)
    replicated_templates: list[str] = field(default_factory=list)
    server_templates: list[str] = field(default_factory=list)
    ui_templates: list[str] = field(default_factory=list)

    # RemoteEvents / RemoteFunctions (always ReplicatedStorage by Roblox rules)
    remote_events: list[str] = field(default_factory=list)

    # Audit trail. Each entry is a dict carrying:
    #   ``script``                 — script name
    #   ``script_type``            — final ``Script`` / ``LocalScript`` /
    #                                ``ModuleScript`` (post-classifier).
    #   ``intrinsic_script_type``  — the immutable transpile-time class
    #                                captured BEFORE the ``Script→LocalScript``
    #                                coercion; ``None`` for scripts built
    #                                outside the transpile path. Restored on
    #                                resume by ``_rehydrate_scripts_from_disk``.
    #   ``container``              — final dotted DataModel path
    #   ``reason``                 — human-readable decision rationale
    #   ``source``                 — ``"classifier"`` or ``"topology"``
    #                                (scene_runtime_topology override).
    # Values are ``str`` for every key EXCEPT ``intrinsic_script_type`` which
    # may be ``None``.
    decisions: list[dict[str, str | None]] = field(default_factory=list)

    # Agent-applied overrides from manual editing of conversion_plan.json.
    overrides_applied: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_storage(
    scripts: list[RbxScript],
    *,
    dependency_map: dict[str, list[str]] | None = None,
    character_script_names: Iterable[str] | None = None,
    template_names: Iterable[str] | None = None,
    template_spawn_callers: dict[str, list[str]] | None = None,
    topology_inputs: "TopologyInputs | None" = None,
    dead_modules: frozenset[str] | None = None,
) -> StoragePlan:
    """Assign each script a concrete parent_path and build a StoragePlan.

    Mutates ``scripts`` in-place: sets ``script.parent_path`` on every entry.

    Args:
        scripts: Transpiled scripts to classify.
        dependency_map: ``class_name -> [referenced_class_names]``. Used to build
            the require() call graph. If omitted, falls back to regex-scanning
            ``require(...)`` calls in script source.
        character_script_names: Scripts attached to a player character prefab
            (per scene wiring). Forced to StarterCharacterScripts.
        template_names: Prefab template names (forward-looking — used when
            ``templates_manifest`` is wired through).
        template_spawn_callers: ``template_name -> [caller_script_names]``.
            Decides replicated vs server container.
        topology_inputs: Phase 2a slice 6 -- output of
            ``Pipeline._maybe_run_topology_prepass`` (per-module domain
            verdict + reachability requirements + caller_graph +
            script_id_by_name + lifecycle_roles), or ``None`` when the
            prepass gate rejected this run (legacy mode / no modules /
            probe flag). Slice 6 plumbs the kwarg but does NOT yet
            change behavior -- ``_decide_script_container`` still runs
            the legacy six-rule sequence. Slice 7 flips the consumer:
            when ``topology_inputs is not None`` the decision tree
            consults ``domains`` / ``reachability_requirements`` /
            ``caller_graph`` etc. and the regex-API legacy branch is
            removed atomically with this fork. Per the slice-6
            "save raw facts, recompute conclusions" rule, this kwarg
            is NEVER persisted onto ``StoragePlan`` -- it is always
            recomputed by the pipeline on every run, including
            assemble-no-retranspile resumes (when
            ``reachability_requirements`` collapses to ``{}`` and
            slice 7 falls back to the "unconstrained helper" path).

        dead_modules: Names of Roblox-dead ModuleScripts (rendering/visual
            helpers with no Roblox runtime effect), computed once by the
            post-coherence dead-module pass
            (``pipeline._subphase_analyze_dead_modules`` via
            ``converter.roblox_dead_modules``). A dead ModuleScript is routed
            OUT of ServerStorage to ReplicatedStorage regardless of
            caller-domain, in BOTH the topology and legacy paths -- caller-
            domain routing would otherwise pull a self-contained dead cluster
            into ServerStorage (the reported symptom). The module body is
            already an inert stub (no-op lifecycle), so SceneRuntime never
            applies any effect; rerouting to RS keeps it reachable for any
            surviving requirer without parking dead code in a server-private
            container. ``None`` (the default) preserves pre-fix behavior for
            direct test callers that do not run the dead-module pass.

    Returns:
        StoragePlan describing every container assignment with an audit trail.
    """
    plan = StoragePlan()
    dead_set: frozenset[str] = dead_modules or frozenset()
    character_set = set(character_script_names or [])
    script_by_name: dict[str, RbxScript] = {s.name: s for s in scripts}

    call_graph = _build_call_graph(scripts, dependency_map)
    # Phase 2a slice 7 round 3 (2026-05-30): client_touchers /
    # server_touchers regex sets are restored as **FALLBACK-PATH ONLY**
    # inputs. The topology path consumes ``topology_inputs.domains``
    # instead and never reads these sets. ``_decide_script_container``
    # threads them only into ``_decide_script_container_legacy``;
    # ``_decide_script_container_from_topology`` never sees them.
    # See ``_scripts_with_client_apis`` for the deletion contract.
    fallback_client_touchers = _scripts_with_client_apis(scripts)
    fallback_server_touchers = _scripts_with_server_apis(scripts)

    for s in scripts:
        container, reason = _decide_script_container(
            s,
            call_graph=call_graph,
            character_set=character_set,
            script_by_name=script_by_name,
            topology_inputs=topology_inputs,
            fallback_client_touchers=fallback_client_touchers,
            fallback_server_touchers=fallback_server_touchers,
            dead_modules=dead_set,
        )
        s.parent_path = container

        # Roblox requires LocalScript parent = StarterPlayer(.StarterPlayerScripts)
        # or StarterCharacterScripts; Script parent = ServerScriptService or
        # ServerStorage won't run; ModuleScript can live anywhere but is only
        # useful where callers can reach it. Keep script_type aligned with the
        # assigned container:
        if container == STARTER_PLAYER_SCRIPTS or container == STARTER_CHARACTER_SCRIPTS:
            if s.script_type != "LocalScript" and s.script_type != "ModuleScript":
                s.script_type = "LocalScript"
        elif container == SERVER_SCRIPT_SERVICE:
            if s.script_type == "LocalScript":
                # Client script in SSS would never run — keep LocalScript, place
                # in StarterPlayerScripts instead. Also caught by
                # ``_enforce_hard_constraints`` below; the in-flow correction
                # preserves the auto-heal behavior callers expect.
                s.parent_path = STARTER_PLAYER_SCRIPTS
                container = STARTER_PLAYER_SCRIPTS
                reason += " (forced to StarterPlayerScripts: LocalScript cannot live in SSS)"
        # ModuleScripts keep whatever container was chosen.

        # Phase 2a slice 7: hard-constraint post-validator. These
        # checks are belt-and-suspenders -- the in-flow corrections
        # above SHOULD prevent the violations, but the validator
        # raises if a future edit slips through.
        _enforce_hard_constraints(s, container)

        # Phase 2a slice 5 round 3: persist the immutable
        # ``intrinsic_script_type`` alongside the (potentially-coerced)
        # ``script_type``. The intrinsic field is set ONCE at RbxScript
        # construction and never mutated, so reading it after the
        # coercion above still returns the transpile-time value. Stored
        # so ``pipeline._rehydrate_scripts_from_disk`` can restore it on
        # resume and the cycle ``classify→rehydrate→classify`` preserves
        # the intrinsic reading. ``None`` for non-transpile construction
        # paths that have not yet been migrated to stamp the field; the
        # rehydration path treats that as "fall back to script_type"
        # which preserves the pre-round-3 behaviour for those paths.
        plan.decisions.append({
            "script": s.name,
            "script_type": s.script_type,
            "intrinsic_script_type": s.intrinsic_script_type,
            "container": container,
            "reason": reason,
            "source": "classifier",
        })
        _append_to_bucket(plan, s.name, s.script_type, container)

    _collect_remote_event_names(plan, scripts)
    # Slice 7: derive client_touchers from the just-computed plan
    # buckets instead of the deleted regex set. Any script that
    # landed in a client-side container is a client-side caller from
    # the template-routing perspective.
    client_touchers = set(plan.client_scripts) | set(plan.character_scripts)
    _assign_template_containers(
        plan,
        template_names=template_names,
        template_spawn_callers=template_spawn_callers,
        client_touchers=client_touchers,
    )

    log.info(
        "[storage_classifier] %d scripts placed: %d server, %d client, %d character, "
        "%d replicated_first, %d shared_modules, %d server_modules",
        len(plan.decisions),
        len(plan.server_scripts),
        len(plan.client_scripts),
        len(plan.character_scripts),
        len(plan.replicated_first_scripts),
        len(plan.shared_modules),
        len(plan.server_modules),
    )

    return plan


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_call_graph(
    scripts: list[RbxScript],
    dependency_map: dict[str, list[str]] | None,
) -> dict[str, set[str]]:
    """Return caller -> set(callees) built from dependency_map + source scan.

    **FALLBACK-PATH ONLY** -- the legacy decision tree
    (``_decide_script_container_legacy``) consults this graph. The
    topology path uses
    ``topology_inputs["caller_graph"]`` instead, which is built by
    ``scene_runtime_topology.build_topology.resolve_caller_graph`` as
    a pure ``dependency_map`` projection (no source-scan).

    Round 3 (Codex R2 P1 #5): the source-scan augmentation below is
    INTENTIONALLY kept on the legacy path. ``dependency_map`` is
    produced by the C# analyzer and misses ``require()`` calls
    injected post-transpile (coherence packs / runtime helpers /
    generic-mode callees discovered post-hoc). The legacy fallback
    fires precisely in degraded-topology cases (no transpile this
    run, ``script_id_by_name`` miss, etc.) where post-analyzer
    requires are exactly the requires the topology pipeline could
    not see. Removing the source-scan from the legacy path would
    silently regress server-private modules to ReplicatedStorage.

    The topology ``caller_graph`` stays as a pure projection because
    the topology path's domain inference + reachability solver
    already cover the cases this scanner targets when they have
    valid inputs. Will be deleted from the legacy path when the
    legacy path itself is deleted (post-topology-coverage extension).
    """
    graph: dict[str, set[str]] = {s.name: set() for s in scripts}
    script_names = set(graph.keys())

    if dependency_map:
        for caller, callees in dependency_map.items():
            if caller not in graph:
                continue
            graph[caller].update(c for c in callees if c in script_names)

    # FALLBACK-PATH ONLY source-scan augmentation (see docstring).
    # Always augment: dependency_map is from C# analyzer and may
    # miss require() calls injected post-transpile. Walk each
    # `require(...)` and extract candidate names from its full body
    # — including nested parens like
    # `require(game:GetService("ServerStorage"):FindFirstChild("Utility"))`.
    quoted_pat = re.compile(r'["\'](\w+)["\']')
    dotted_pat = re.compile(r'\.(\w+)')
    for s in scripts:
        for m in re.finditer(r'require\(', s.source):
            body = _balanced_paren_body(s.source, m.end())
            for q in quoted_pat.finditer(body):
                name = q.group(1)
                if name in script_names and name != s.name:
                    graph[s.name].add(name)
            for d in dotted_pat.finditer(body):
                name = d.group(1)
                if name in script_names and name != s.name:
                    graph[s.name].add(name)

    return graph


def _balanced_paren_body(source: str, start: int) -> str:
    """Return the substring from ``start`` to the matching close-paren.

    ``start`` should point to the first character after an opening ``(``.
    Handles nested parens by bracket counting. Bounded scan (max 1024 chars)
    to stay O(n) overall.
    """
    depth = 1
    end = start
    limit = min(len(source), start + 1024)
    while end < limit and depth > 0:
        c = source[end]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        end += 1
    return source[start:end]


class ConstraintViolation(ValueError):
    """Raised when ``_enforce_hard_constraints`` detects an
    impossible-to-honor (script_type, container) pair.

    Phase 2a slice 7: defense-in-depth post-validator. The decision
    tree should not normally produce these, but the validator catches
    drift if a future edit introduces it.
    """


def _scripts_with_client_apis(scripts: list[RbxScript]) -> set[str]:
    """Names of scripts whose source matches any client-only API pattern.

    **FALLBACK-PATH ONLY** -- used by ``_decide_script_container_legacy``
    when topology data is degraded (the legacy mode, the
    ``script_id_by_name`` miss case, or the
    ``transpile_ran=False`` + missing-``reachability_requirements``
    no-transpile-resume case). The topology path uses
    ``infer_module_domains`` instead and MUST NOT call this helper.

    Will be deleted when topology coverage extends to cover the
    no-transpile resume case (see ``slice-7-r2-decision.md`` Option C).
    """
    return {
        s.name for s in scripts
        if any(re.search(p, s.source) for p in _CLIENT_ONLY_PATTERNS)
    }


def _scripts_with_server_apis(scripts: list[RbxScript]) -> set[str]:
    """Names of scripts whose source matches any server-only API pattern.

    **FALLBACK-PATH ONLY** -- used by ``_decide_script_container_legacy``
    when topology data is degraded (the legacy mode, the
    ``script_id_by_name`` miss case, or the
    ``transpile_ran=False`` + missing-``reachability_requirements``
    no-transpile-resume case). The topology path uses
    ``infer_module_domains`` instead and MUST NOT call this helper.

    Will be deleted when topology coverage extends to cover the
    no-transpile resume case (see ``slice-7-r2-decision.md`` Option C).
    """
    return {
        s.name for s in scripts
        if any(re.search(p, s.source) for p in _SERVER_ONLY_PATTERNS)
    }


def _decide_script_container(
    s: RbxScript,
    *,
    call_graph: dict[str, set[str]],
    character_set: set[str],
    script_by_name: dict[str, RbxScript],
    topology_inputs: "TopologyInputs | None" = None,
    fallback_client_touchers: set[str] | None = None,
    fallback_server_touchers: set[str] | None = None,
    dead_modules: frozenset[str] | None = None,
) -> tuple[str, str]:
    """Return (container, reason) for a single script.

    Phase 2a slice 7 (2026-05-30): forks on ``topology_inputs``. When
    ``topology_inputs is not None`` (every non-legacy pipeline run),
    the topology-driven decision tree owns the call. The legacy path
    is preserved as a per-script fallback for two narrow cases the
    topology path can't service:

    1. ``topology_inputs is None`` (legacy mode / probe flag / no
       modules / no scripts -- see ``_maybe_run_topology_prepass``
       gate). Caller uses ``classify_storage`` without the prepass.

    2. ``script_id_by_name.get(s.name) is None`` (degraded-service
       contract on stem/class_name collisions; see
       ``scene_runtime_planner.build_script_id_by_name``). The
       topology layer cannot identify ``s`` so the tree can't apply.

    3. ``topology_inputs.transpile_ran is False`` AND the script is a
       ModuleScript that's NOT in
       ``topology_inputs.reachability_requirements`` (assemble-no-
       retranspile resume; reachability is empty by design because
       ``dependency_map`` is empty -- see the slice-6 handoff). The
       unconstrained-helper contract says fall back PER-SCRIPT, not
       globally; helpers that ARE covered by topology still route
       through the tree.

    Per-script fallback (rather than whole-pipeline) keeps the
    topology fix surface intact for the scripts that ARE covered.

    ``fallback_client_touchers`` / ``fallback_server_touchers`` are
    **FALLBACK-PATH-ONLY** inputs (round 3, Codex R2 P1 #4 fix). They
    are forwarded into ``_decide_script_container_legacy`` so the
    legacy fallback recovers the pre-slice-7
    ``_CLIENT_ONLY_PATTERNS`` / ``_SERVER_ONLY_PATTERNS`` semantic for
    Scripts that escape ``code_transpiler._classify_script_type``'s
    promotion. The TOPOLOGY path
    (``_decide_script_container_from_topology``) does NOT receive
    them. Default ``None`` so callers in tests that construct the
    helper directly stay backward-compatible (an empty set is treated
    as "no client/server-touching scripts").
    """
    client_touchers = (
        fallback_client_touchers
        if fallback_client_touchers is not None
        else set()
    )
    server_touchers = (
        fallback_server_touchers
        if fallback_server_touchers is not None
        else set()
    )
    dead_set: frozenset[str] = dead_modules or frozenset()

    # ------------------------------------------------------------------
    # Fallback gates -- evaluated BEFORE the topology tree dereferences
    # any sid.
    # ------------------------------------------------------------------
    if topology_inputs is None:
        return _decide_script_container_legacy(
            s,
            call_graph=call_graph,
            character_set=character_set,
            script_by_name=script_by_name,
            client_touchers=client_touchers,
            server_touchers=server_touchers,
            dead_modules=dead_set,
        )

    sid = topology_inputs["script_id_by_name"].get(s.name)
    if sid is None:
        # Degraded-service contract: cannot identify this script in
        # the topology layer. Fall back to the legacy decision tree
        # for THIS script only.
        return _decide_script_container_legacy(
            s,
            call_graph=call_graph,
            character_set=character_set,
            script_by_name=script_by_name,
            client_touchers=client_touchers,
            server_touchers=server_touchers,
            dead_modules=dead_set,
        )

    # Unconstrained-helper contract (Codex amendment 1, slice-6
    # handoff). When the pipeline is in no-transpile-resume mode the
    # reachability_requirements map is empty BY DESIGN. ModuleScripts
    # not covered by reachability fall back to legacy per-script,
    # NOT to the topology tree's "orphan -> RS" branch (which would
    # silently regress server-only modules to RS on resume).
    if (
        s.script_type == "ModuleScript"
        and not topology_inputs["transpile_ran"]
        and sid not in topology_inputs["reachability_requirements"]
    ):
        return _decide_script_container_legacy(
            s,
            call_graph=call_graph,
            character_set=character_set,
            script_by_name=script_by_name,
            client_touchers=client_touchers,
            server_touchers=server_touchers,
            dead_modules=dead_set,
        )

    return _decide_script_container_from_topology(
        s, sid=sid, topology_inputs=topology_inputs, dead_modules=dead_set,
    )


def _decide_script_container_from_topology(
    s: RbxScript,
    *,
    sid: str,
    topology_inputs: "TopologyInputs",
    dead_modules: frozenset[str] | None = None,
) -> tuple[str, str]:
    """Slice-7 topology-driven decision tree.

    First-match-wins, generic over Unity input. The tree
    consumes ONLY ``topology_inputs`` (per-module domain verdict +
    reachability requirements + caller graph + lifecycle roles) and
    the script's own ``script_type``. No source-text inputs.

    Order:
      1. ``lifecycle_role == "character_attached"`` -> StarterCharacterScripts
      2. ``lifecycle_role == "loader"`` -> ReplicatedFirst
      3. ``reachability_requirements[sid]`` present -> that container
         * sentinel ``"__excluded__"`` (helper reached by BOTH client
           and server require-graphs) -> ReplicatedStorage with the
           ``reachability_conflict`` reason. The
           ``finalize_topology_containers`` late pass then stamps the
           ``fail_closed_reason`` on the module row.
      4. ModuleScript -> route by caller-domain (consulting
         ``caller_graph[sid]`` -> ``domains[caller_sid]``)
         * any client-domain caller -> ReplicatedStorage
         * all server-domain callers -> ServerStorage (faithful to
           the analysis -- if topology says server-only, trust it)
         * orphan / unknown -> ReplicatedStorage (conservative)
      5. LocalScript -> StarterPlayerScripts
      6. ``script_type == "Script"`` with ``domain == "client"`` ->
         StarterPlayerScripts (round-2 patch -- restores the
         pre-slice-7 ``_CLIENT_ONLY_PATTERNS`` regex semantic for
         the narrow case where ``code_transpiler._classify_script_type``
         did NOT promote ``Script`` to ``LocalScript`` but
         ``infer_module_domains`` still classified the module as
         client-domain. The downstream auto-coercion at
         ``classify_storage`` flips ``script_type`` to ``LocalScript``
         so ``_enforce_hard_constraints`` does not raise.)
      7. Script -> ServerScriptService

    See ``scene-runtime-architecture-ir.md`` §"script_storage.py --
    bound deterministic mapper" for the design rationale.
    """
    lifecycle_role = topology_inputs["lifecycle_roles"].get(sid, "")
    if lifecycle_role == "character_attached":
        return STARTER_CHARACTER_SCRIPTS, (
            "topology: lifecycle_role=character_attached"
        )
    if lifecycle_role == "loader":
        return REPLICATED_FIRST, "topology: lifecycle_role=loader"

    requirement = topology_inputs["reachability_requirements"].get(sid)
    if requirement is not None:
        if requirement == "__excluded__":
            return REPLICATED_STORAGE, (
                "topology: reachability_conflict "
                "(reached by both client+server require-graphs)"
            )
        return requirement, (
            f"topology: reachability_required_container={requirement}"
        )

    if s.script_type == "ModuleScript":
        # Roblox-dead module: route OUT of ServerStorage to ReplicatedStorage
        # regardless of caller-domain. The caller-domain rule below would pull
        # a self-contained dead rendering cluster (whose only callers are
        # server-default leaf Scripts) into ServerStorage -- the reported
        # symptom. The body is already an inert stub, so RS is the neutral,
        # reachable home. (See pipeline._subphase_analyze_dead_modules.)
        if dead_modules and s.name in dead_modules:
            return REPLICATED_STORAGE, (
                "topology: Roblox-dead module (inert visual/rendering helper): "
                "routed to ReplicatedStorage (no caller-domain ServerStorage)"
            )
        callers = topology_inputs["caller_graph"].get(sid, [])
        if not callers:
            return REPLICATED_STORAGE, (
                "topology: orphan ModuleScript (no callers): default replicated"
            )
        caller_domains: set[str] = set()
        for caller_sid in callers:
            d = topology_inputs["domains"].get(caller_sid, "")
            if d:
                caller_domains.add(d)
        if "client" in caller_domains:
            return REPLICATED_STORAGE, (
                f"topology: ModuleScript required by {len(callers)} caller(s), "
                "at least one client-domain"
            )
        if caller_domains == {"server"}:
            return SERVER_STORAGE, (
                f"topology: ModuleScript required only by server-domain "
                f"callers ({len(callers)})"
            )
        # Mixed unknown / helper callers, no client signal -> default
        # to RS so any late-added caller can reach it.
        return REPLICATED_STORAGE, (
            f"topology: ModuleScript required by {len(callers)} caller(s), "
            "no client/server domain signal: default replicated"
        )

    if s.script_type == "LocalScript":
        # Defensive surface for the latent type/domain conflict (Codex,
        # 2026-06-02): routing is BY script_type here — a LocalScript MUST
        # land in a client container (Roblox engine constraint). The
        # source-derived ``domains`` verdict is independent of script_type;
        # if it says "server" while the type is LocalScript, the type is
        # likely mis-classified. In LEGACY mode
        # ``_fix_client_server_classification`` corrects the type BEFORE this
        # runs; in GENERIC mode that pass is off by contract, so a
        # stale/uncorrected LocalScript can survive. We do NOT silently
        # reroute (a LocalScript in ServerScriptService would not run) — we
        # WARN so the conflict is diagnosable instead of an invisible misroute.
        if topology_inputs["domains"].get(sid, "") == "server":
            log.warning(
                "[storage] LocalScript %r has server-domain source but is "
                "routed to StarterPlayerScripts by its script_type; the type "
                "is likely mis-classified (generic mode skips client/server "
                "type reconciliation). script_id=%s",
                s.name, sid,
            )
        return STARTER_PLAYER_SCRIPTS, "topology: LocalScript (default container)"

    # script_type == "Script". Slice 7 round 2 (Codex R1 P1 #1+#3):
    # check the client-domain branch BEFORE falling through to SSS.
    # Restores the pre-slice-7 ``_CLIENT_ONLY_PATTERNS`` regex
    # semantic for the narrow case where
    # ``code_transpiler._classify_script_type`` left ``script_type``
    # as ``Script`` but the upstream domain classifier flagged the
    # module as client-domain (e.g. uses ``Players.LocalPlayer``
    # without any of the API substrings that drive
    # ``_classify_script_type``'s ``Script -> LocalScript`` promotion).
    # The auto-coercion at ``classify_storage`` lines 237-239 then
    # flips ``script_type`` to ``LocalScript`` so the resulting
    # placement satisfies the Roblox engine constraint
    # ``_enforce_hard_constraints`` checks for.
    domain = topology_inputs["domains"].get(sid, "")
    if domain == "client":
        return STARTER_PLAYER_SCRIPTS, (
            "topology: script_type=Script with client domain "
            "(client-only API surface)"
        )
    return SERVER_SCRIPT_SERVICE, "topology: server Script (default)"


def _decide_script_container_legacy(
    s: RbxScript,
    *,
    call_graph: dict[str, set[str]],
    character_set: set[str],
    script_by_name: dict[str, RbxScript],
    client_touchers: set[str] | None = None,
    server_touchers: set[str] | None = None,
    dead_modules: frozenset[str] | None = None,
) -> tuple[str, str]:
    """Legacy fallback decision tree.

    Phase 2a slice 7: this is the per-script fallback for the three
    cases documented on ``_decide_script_container``. Compared to
    slice 5's legacy path the ModuleScript caller-domain detection
    still relies on ``script_type`` alone (the topology path's domain
    classification is upstream of this fallback).

    Round 3 (Codex R2 P1 #4): restored the
    ``_CLIENT_ONLY_PATTERNS`` / ``_SERVER_ONLY_PATTERNS`` regex-set
    branches via ``client_touchers`` / ``server_touchers``. A Script
    using ``Players.LocalPlayer`` (or any other client-only API)
    that escapes ``code_transpiler._classify_script_type``'s
    Script -> LocalScript promotion would otherwise default to
    ServerScriptService where ``LocalPlayer`` is nil at runtime.
    These kwargs are populated by ``classify_storage`` from
    ``_scripts_with_client_apis`` / ``_scripts_with_server_apis``
    -- both of which are documented as **FALLBACK-PATH ONLY** infra.
    The topology decision tree
    (``_decide_script_container_from_topology``) does NOT consume
    these sets.

    ``client_touchers`` / ``server_touchers`` default to ``None`` so
    direct callers in tests stay backward-compatible (treated as
    empty sets -- no Scripts with client/server API surface to
    promote).

    Tests in ``test_storage_classifier.py`` exercise this path
    directly (none of them pass ``topology_inputs``).
    """
    client_touchers = client_touchers if client_touchers is not None else set()
    server_touchers = server_touchers if server_touchers is not None else set()

    # Character-attached scripts go to StarterCharacterScripts.
    if s.name in character_set:
        return STARTER_CHARACTER_SCRIPTS, "character-attached per scene wiring"

    # Loader/splash scripts that need to run before full replication.
    if REPLICATED_FIRST_HINTS.search(s.name) and s.script_type != "ModuleScript":
        return REPLICATED_FIRST, f"name hint matches ReplicatedFirst pattern ({s.name})"

    # ModuleScripts: route by who requires them.
    if s.script_type == "ModuleScript":
        # Roblox-dead module: route OUT of ServerStorage to ReplicatedStorage
        # regardless of caller domain. The cached SimpleFPS symptom uses THIS
        # path's ``...server-side callers`` reason text, so the dead reroute
        # must fire here too (LOCKED DECISION: fix both paths). The body is an
        # inert stub; RS is the neutral, reachable home.
        if dead_modules and s.name in dead_modules:
            return REPLICATED_STORAGE, (
                "Roblox-dead module (inert visual/rendering helper): "
                "routed to ReplicatedStorage (no caller-domain ServerStorage)"
            )
        callers = _find_callers(s.name, call_graph)
        if not callers:
            # Orphan module — nobody requires it. Default to ReplicatedStorage
            # so that if a late-added caller wants it, it can reach it.
            return REPLICATED_STORAGE, "orphan module (no callers): default replicated"

        caller_is_client = any(
            _caller_is_local_script(c, script_by_name) for c in callers
        )
        caller_is_server = any(
            _caller_is_server_script(c, script_by_name) for c in callers
        )
        # Round 3: a caller in client_touchers is a client-domain
        # caller even if its ``script_type`` is still ``Script``
        # (escaped the promotion). Symmetric for server_touchers.
        caller_is_client = caller_is_client or any(
            c in client_touchers for c in callers
        )
        caller_is_server = caller_is_server or any(
            c in server_touchers for c in callers
        )

        if caller_is_client:
            return REPLICATED_STORAGE, (
                f"required by {len(callers)} caller(s), at least one client-side"
            )
        if caller_is_server and not caller_is_client:
            return SERVER_STORAGE, (
                f"required only by server-side callers ({len(callers)})"
            )
        return REPLICATED_STORAGE, (
            f"required by {len(callers)} caller(s), client/server unknown: default replicated"
        )

    # LocalScripts default to StarterPlayerScripts.
    if s.script_type == "LocalScript":
        return STARTER_PLAYER_SCRIPTS, "LocalScript (default container)"

    # A Script with client-only API surface that escaped the
    # ``Script -> LocalScript`` promotion routes to StarterPlayerScripts;
    # downstream auto-coercion flips ``script_type`` so
    # ``_enforce_hard_constraints`` does not raise. The
    # ``not in server_touchers`` guard fails CLOSED to server for a
    # dual-surface Script (touches both client- and server-only APIs):
    # it lands in SSS where the server APIs work, since a server can
    # often call client-replicated APIs but not the inverse.
    if s.name in client_touchers and s.name not in server_touchers:
        return STARTER_PLAYER_SCRIPTS, (
            "fallback: Script with client-only API surface "
            "(legacy _CLIENT_ONLY_PATTERNS branch)"
        )
    if s.name in server_touchers:
        return SERVER_SCRIPT_SERVICE, (
            "fallback: Script with server-only API surface "
            "(legacy _SERVER_ONLY_PATTERNS branch)"
        )

    # Everything else is a server Script.
    return SERVER_SCRIPT_SERVICE, "server Script (default)"


def _enforce_hard_constraints(s: RbxScript, container: str) -> None:
    """Post-decision hard-constraint validator.

    Defense-in-depth check. The decision tree + in-flow corrections
    should never produce these pairs, but the validator raises if a
    future edit slips through. The constraints encode Roblox-engine
    impossibilities:

      * LocalScript in ServerScriptService -- would silently never
        run (engine ignores LocalScripts under server services).
      * ReplicatedFirst + ModuleScript -- ReplicatedFirst is for
        executable scripts that run before full replication;
        ModuleScripts there are inert.

    Raises ``ConstraintViolation`` with the offending pair when
    triggered.
    """
    if s.script_type == "LocalScript" and container == SERVER_SCRIPT_SERVICE:
        raise ConstraintViolation(
            f"LocalScript {s.name!r} cannot live in {SERVER_SCRIPT_SERVICE} "
            "-- Roblox engine ignores LocalScripts under server services."
        )
    if container == REPLICATED_FIRST and s.script_type == "ModuleScript":
        raise ConstraintViolation(
            f"ModuleScript {s.name!r} cannot live in {REPLICATED_FIRST} "
            "-- the container is for executable scripts; ModuleScripts "
            "there are inert."
        )


def _find_callers(target: str, call_graph: dict[str, set[str]]) -> set[str]:
    """Return the set of script names that require() the target."""
    return {caller for caller, callees in call_graph.items() if target in callees}


def _caller_is_local_script(name: str, script_by_name: dict[str, RbxScript]) -> bool:
    s = script_by_name.get(name)
    return bool(s and s.script_type == "LocalScript")


def _caller_is_server_script(name: str, script_by_name: dict[str, RbxScript]) -> bool:
    s = script_by_name.get(name)
    return bool(s and s.script_type == "Script")


def _append_to_bucket(
    plan: StoragePlan,
    name: str,
    script_type: str,
    container: str,
) -> None:
    """Record the script name in the right StoragePlan bucket."""
    if container == SERVER_SCRIPT_SERVICE:
        plan.server_scripts.append(name)
    elif container == STARTER_PLAYER_SCRIPTS:
        plan.client_scripts.append(name)
    elif container == STARTER_CHARACTER_SCRIPTS:
        plan.character_scripts.append(name)
    elif container == REPLICATED_FIRST:
        plan.replicated_first_scripts.append(name)
    elif container == REPLICATED_STORAGE:
        plan.shared_modules.append(name)
    elif container == SERVER_STORAGE:
        plan.server_modules.append(name)


def _collect_remote_event_names(plan: StoragePlan, scripts: list[RbxScript]) -> None:
    """Scan scripts for RemoteEvent/RemoteFunction references."""
    pat = re.compile(
        r'(?:FindFirstChild|WaitForChild)\s*\(\s*["\']([^"\']+)["\']'
    )
    candidates: set[str] = set()
    for s in scripts:
        for m in pat.finditer(s.source):
            candidates.add(m.group(1))

    # Filter to likely RemoteEvent names — require a FireServer / OnServerEvent /
    # FireClient / OnClientEvent / FireAllClients reference somewhere.
    remote_signatures = ("FireServer", "OnServerEvent", "FireClient",
                         "OnClientEvent", "FireAllClients")
    for name in candidates:
        for s in scripts:
            if f'"{name}"' in s.source and any(sig in s.source for sig in remote_signatures):
                plan.remote_events.append(name)
                break
    plan.remote_events = sorted(set(plan.remote_events))


def _assign_template_containers(
    plan: StoragePlan,
    *,
    template_names: Iterable[str] | None,
    template_spawn_callers: dict[str, list[str]] | None,
    client_touchers: set[str],
) -> None:
    """Assign prefab templates to ReplicatedStorage vs ServerStorage.

    Forward-looking: templates_manifest is not yet wired through the pipeline.
    When it is, each template gets a container based on who clones it.
    """
    if not template_names:
        return

    spawn_callers = template_spawn_callers or {}
    for name in template_names:
        if _SERVER_SECRET_HINTS.search(name):
            plan.server_templates.append(name)
            continue
        callers = spawn_callers.get(name, [])
        if not callers:
            # Unknown caller set — default replicated so both sides can reach it.
            plan.replicated_templates.append(name)
            continue
        if any(c in client_touchers for c in callers):
            plan.replicated_templates.append(name)
        else:
            # Server-only spawn: default replicated (Roblox replicates server-
            # parented clones automatically). ServerStorage is reserved for
            # templates genuinely hidden from clients.
            plan.replicated_templates.append(name)
