"""contract_pipeline.py -- Generic-runtime orchestration over the
transpiler.

Wraps ``code_transpiler.transpile_scripts(runtime_mode="generic", ...)``
with the three post-transpile passes the design doc allowlists:

  1. Asset-reference rewriting -- delegated to the existing legacy
     ``script_asset_rewriter`` when uploaded assets are provided. This
     module just sequences the call; it doesn't reimplement.
  2. Module-require-path resolution -- new generic-only pass. Scans
     each runtime-bearing module for ``require("@scene_runtime/<stem>")``
     calls and resolves each ``<stem>`` against the planner's
     ``by_stem`` map. Fails closed on missing stem or stem collision.
  3. Final contract verifier -- already runs inside the transpile
     backends (per-script verify + one-shot reprompt). The orchestrator
     just aggregates the per-script results.

NOT in scope here:
  * Legacy repair passes (shared_state_linter, fix_require_classifications,
    _guard_client_code_in_modules, script_coherence_packs, etc.) are
    deliberately NOT called. The contract enforces compliance by
    construction; the repair layer fixes problems the contract prevents.
  * Pipeline integration. PR3a's CLI rejects ``--scene-runtime=generic``;
    the spike harness is the only caller. PR3b/PR4 land the full
    integration.

The spike harness (``converter/tools/scene_runtime_spike.py``) drives
this module on real Unity projects to measure verifier pass rate. See
``converter/docs/design/scene-runtime-contract.md`` PR3a row.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from converter.child_ref_resolver import build_child_ref_map
from converter.code_transpiler import (
    TranspilationResult,
    TranspiledScript,
    _is_visual_only_script,
    transpile_scripts,
)
from converter.roster_consumer_lowering import RosterConsumerFact
from core.unity_types import GuidIndex, ParsedScene, PrefabLibrary
from unity.script_analyzer import ScriptInfo


# ---------------------------------------------------------------------------
# Planner artifact shape -- a local TypedDict subset of what PR1 emits.
#
# Per the PR3a kickoff brief, the contract surface is PR1's *artifact
# format*, not PR1's planner code. Importing
# ``scene_runtime_planner.SceneRuntimeArtifact`` would create a hard code
# dependency that breaks the "PR1 / PR2 / PR3a independently landable"
# topology (the worktree may not have the planner module). Re-declaring
# the subset PR3a actually consumes keeps the dependency direction one-
# way: PR1 produces the JSON; PR3a reads it.
# ---------------------------------------------------------------------------

class _SceneRuntimeModule(TypedDict, total=False):
    """One ``scene_runtime.modules`` row. Same shape as PR1's
    ``SceneRuntimeModule`` (``total=False`` for forward compatibility:
    PR3b adds ``domain`` / ``container`` / ``module_path``)."""

    stem: str
    class_name: str
    runtime_bearing: bool
    module_path: str  # PR3b -- present iff the storage classifier has run


class _Addressables(TypedDict, total=False):
    """The ``addressables`` block the planner stamps (Unit-4 surface). Phase 2
    reads ``by_label`` (label -> [prefab_id]) to identify the roster consumer."""

    by_label: dict[str, list[str]]
    by_address: dict[str, str]


class _SceneRuntimeArtifact(TypedDict, total=False):
    """Top-level ``scene_runtime`` shape PR3a reads."""

    modules: dict[str, _SceneRuntimeModule]
    # PR3a doesn't read scenes / prefabs / domain_overrides directly --
    # they survive the dict round-trip but the orchestrator doesn't
    # inspect them. ``total=False`` keeps them optional in fixtures.
    scenes: dict[str, object]
    prefabs: dict[str, object]
    domain_overrides: dict[str, str]
    # Unit-4 roster: the addressables label/address block the planner stamps.
    # ``find_roster_consumers`` keys the roster re-lowering on ``by_label``.
    addressables: _Addressables

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RequireResolution:
    """One ``require("@scene_runtime/<stem>")`` site after resolution.

    ``ok`` is False when the stem is missing from ``by_stem`` or
    collides across modules; the orchestrator turns any non-OK row into
    a project-level fail-closed reason."""

    from_script: str          # source path of the requiring module
    stem: str                 # the stem the AI requested
    resolved_to: str | None   # planner ``module_path`` on success, None otherwise
    reason: str               # "ok" | "missing_stem" | "stem_collision"

    @property
    def ok(self) -> bool:
        return self.reason == "ok"


@dataclass(frozen=True)
class FailClosed:
    """A reason the conversion drops back to ``legacy``.

    PR3a only surfaces these; the routing-to-legacy fallback lives in
    PR3b's auto-mode plumbing. The compliance spike asserts on the
    ``kind`` field so the gate report can show why a project would have
    failed under ``auto``."""

    # "verifier" | "require_missing" | "require_collision" |
    # "runtime_bearing_collision" | "stub_strategy"
    # ``stub_strategy``: AI fell through to the stub generator on a
    # runtime-bearing module (backend disabled, transient error). The
    # auto-mode fallback in PR3b treats this as operationally equivalent
    # to a contract failure -- a stub can't host the runtime contract.
    kind: str
    detail: str


@dataclass
class ContractPipelineResult:
    """Aggregate result of one generic-runtime conversion."""

    transpilation: TranspilationResult
    require_resolutions: list[RequireResolution] = field(default_factory=list)
    fail_closed: list[FailClosed] = field(default_factory=list)
    runtime_bearing_paths: frozenset[Path] = field(default_factory=frozenset)

    # ---- spike accounting -------------------------------------------------

    @property
    def runtime_bearing_scripts(self) -> list[TranspiledScript]:
        return [
            s for s in self.transpilation.scripts
            if Path(s.source_path) in self.runtime_bearing_paths
        ]

    @property
    def first_attempt_pass_count(self) -> int:
        """Modules whose initial AI output was contract-compliant (no
        pre-reprompt warnings, no post-reprompt warnings)."""
        return sum(
            1 for s in self.runtime_bearing_scripts
            if not any(_is_contract_warning(w) for w in s.warnings)
        )

    @property
    def reprompt_rescued_count(self) -> int:
        """Modules whose initial output failed but the one-shot reprompt
        fixed it (has ``contract-verifier-pre`` warnings but no
        ``contract-verifier`` ones)."""
        rescued = 0
        for s in self.runtime_bearing_scripts:
            pre = any(w.startswith("contract-verifier-pre") for w in s.warnings)
            post = any(_is_post_reprompt_warning(w) for w in s.warnings)
            if pre and not post:
                rescued += 1
        return rescued

    @property
    def fail_closed_count(self) -> int:
        """Modules that still violate the contract after the reprompt
        (carry at least one ``contract-verifier`` post-reprompt warning)."""
        return sum(
            1 for s in self.runtime_bearing_scripts
            if any(_is_post_reprompt_warning(w) for w in s.warnings)
        )

    @property
    def total_runtime_bearing(self) -> int:
        return len(self.runtime_bearing_scripts)

    @property
    def pre_reprompt_pass_rate(self) -> float:
        """Fraction of runtime-bearing modules whose FIRST AI output
        passed the verifier (no reprompt needed). ``0.0`` when there are
        no runtime-bearing modules."""
        n = self.total_runtime_bearing
        return self.first_attempt_pass_count / n if n else 0.0

    @property
    def post_reprompt_pass_rate(self) -> float:
        """Fraction of runtime-bearing modules contract-compliant AFTER
        the one-shot reprompt (first-attempt-clean PLUS reprompt-rescued)."""
        n = self.total_runtime_bearing
        if n == 0:
            return 0.0
        return (self.first_attempt_pass_count + self.reprompt_rescued_count) / n


def _is_contract_warning(w: str) -> bool:
    """A contract-rule (a)-(h) warning from ``_verify_and_reprompt`` (pre OR
    post tag).

    EXCLUDES ``contract-verifier-player`` (paradigm B, rules ``p1``/``p2``):
    player rejects are NON-load-bearing cosmetic notes outside the a-h
    contract the compliance spike measures, so they must not perturb
    ``first_attempt_pass_count`` (which drops any script carrying a contract
    warning)."""
    return (
        w.startswith("contract-verifier")
        and not w.startswith("contract-verifier-player")
        # Phase 1 (relation #8): the ``im`` rule is NON-load-bearing (fails OPEN), like player
        # rejects, so it must not perturb the compliance-spike contract stats either.
        and not w.startswith("contract-verifier-impulse")
    )


def _is_post_reprompt_warning(w: str) -> bool:
    """A warning that survived the reprompt (NOT the ``-pre`` variant)."""
    return w.startswith("contract-verifier ") or w.startswith("contract-verifier:")


# ---------------------------------------------------------------------------
# Require resolution (generic-only allowlist pass)
# ---------------------------------------------------------------------------

# ``require("@scene_runtime/<stem>")``  -- the contract-pinned shape the
# generic prompt teaches. Slack on whitespace/quote style; the resolver
# fails closed if the AI emits something else (no fuzzy matching).
_RE_SCENE_RUNTIME_REQUIRE = re.compile(
    r"""require\s*\(\s*['"]@scene_runtime/([\w]+)['"]\s*\)""",
)

def _container_lookup_expr(stem: str) -> str:
    """A runtime require-target expression that locates a sibling module
    by file stem at load time -- the shape the rest of the pipeline emits
    for resolved sibling requires.

    Used as the ``by_stem`` fallback when the planner artifact carries no
    explicit ``module_path`` for a module. Replaces the historical
    script_id (raw .cs GUID) fallback, which produced an illegal
    ``require(<bareGUID>)`` ("Malformed number") in the generated module.
    """
    return (
        'game:GetService("ReplicatedStorage"):FindFirstChild('
        f'"{stem}", true) or '
        'game:GetService("ServerStorage"):FindFirstChild('
        f'"{stem}", true)'
    )


# Service roots a planner ``module_path`` can start with. A bare dotted path
# like ``ReplicatedStorage.Player`` is NOT valid as a top-level
# ``require(...)`` argument -- ``ReplicatedStorage`` is an unbound global at
# module scope (no ``local ReplicatedStorage = game:GetService(...)`` precedes
# it). Lowering the root to ``game:GetService("ReplicatedStorage")`` makes the
# expression self-contained, matching ``_container_lookup_expr``'s shape.
_REQUIRE_SERVICE_ROOTS = frozenset({
    "ReplicatedStorage", "ServerStorage", "ServerScriptService",
    "ReplicatedFirst", "StarterPlayer", "StarterGui", "Workspace",
})


def _service_rooted_require_target(target: str) -> str:
    """Lower a bare service-rooted dotted ``module_path`` to a self-contained
    ``game:GetService("<Service>").<rest>`` expression so it doesn't reference
    an unbound global at a top-level ``require``. Targets that are already an
    expression (``game:GetService(...)`` / ``_container_lookup_expr`` /
    parenthesized) are returned unchanged."""
    head = target.split(".", 1)[0]
    if "(" in head or ":" in head:
        return target
    root, dot, rest = target.partition(".")
    if dot and root in _REQUIRE_SERVICE_ROOTS:
        return f'game:GetService("{root}").{rest}'
    return target


def resolve_requires(
    scripts: list[TranspiledScript],
    by_stem: dict[str, str],
    collisions: dict[str, list[str]] | None = None,
) -> list[RequireResolution]:
    """Resolve every ``require("@scene_runtime/<stem>")`` against the
    planner's stem table.

    Args:
        scripts: Transpiled scripts to scan.
        by_stem: ``{stem -> planner ``module_path``}`` from
            ``scene_runtime_planner.build_require_graph``.
        collisions: ``{stem -> [script_id, ...]}`` for stems that mapped
            to more than one module. A require pointing at a collided
            stem fails closed.

    Returns:
        One ``RequireResolution`` row per require site (some may be
        ``ok``, some may be ``missing_stem`` or ``stem_collision``).
    """
    collisions = collisions or {}
    out: list[RequireResolution] = []
    for script in scripts:
        for m in _RE_SCENE_RUNTIME_REQUIRE.finditer(script.luau_source):
            stem = m.group(1)
            if stem in collisions:
                out.append(RequireResolution(
                    from_script=script.source_path,
                    stem=stem,
                    resolved_to=None,
                    reason="stem_collision",
                ))
                continue
            resolved = by_stem.get(stem)
            if resolved is None:
                out.append(RequireResolution(
                    from_script=script.source_path,
                    stem=stem,
                    resolved_to=None,
                    reason="missing_stem",
                ))
                continue
            out.append(RequireResolution(
                from_script=script.source_path,
                stem=stem,
                resolved_to=resolved,
                reason="ok",
            ))
    return out


def _apply_require_resolutions(
    scripts: list[TranspiledScript],
    resolutions: list[RequireResolution],
) -> None:
    """Rewrite ``require("@scene_runtime/<stem>") -> require(<module_path>)``
    in place on each ``TranspiledScript.luau_source``.

    Only ``reason == "ok"`` rows are applied. Missing-stem / stem-collision
    rows are left untouched so the orchestrator's fail-closed surface still
    carries the original literal (and the verifier's downstream signal
    keeps firing). The single-pass nature of the resolutions list means
    each ``(script, stem)`` is rewritten at most once -- safe to call
    after ``resolve_requires`` returns."""
    resolved_by_script: dict[str, dict[str, str]] = {}
    for r in resolutions:
        if r.reason != "ok" or r.resolved_to is None:
            continue
        resolved_by_script.setdefault(r.from_script, {})[r.stem] = r.resolved_to
    for script in scripts:
        rewrites = resolved_by_script.get(script.source_path)
        if not rewrites:
            continue
        src = script.luau_source
        for stem, target in rewrites.items():
            # Lower a bare service-rooted dotted path to a self-contained
            # ``game:GetService(...)`` expression so a top-level
            # ``require(ReplicatedStorage.Foo)`` doesn't reference an unbound
            # global (the HudControl/HostilePlane/Explosive crash).
            target = _service_rooted_require_target(target)
            # Match the contract idiom in single OR double quotes; mirror
            # ``_RE_SCENE_RUNTIME_REQUIRE`` so the resolver and rewriter
            # never disagree about what counts as a require site.
            src = re.sub(
                rf'''require\s*\(\s*['"]@scene_runtime/{re.escape(stem)}['"]\s*\)''',
                f'require({target})',
                src,
            )
        script.luau_source = src


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def transpile_with_contract(
    unity_project_path: str | Path,
    script_infos: list[ScriptInfo],
    *,
    scene_runtime: _SceneRuntimeArtifact,
    api_key: str = "",
    use_ai: bool = True,
    max_concurrent: int = 10,
    serialized_field_refs: dict[str, dict[str, str]] | None = None,
    parsed_scenes: list[ParsedScene] | None = None,
    prefab_library: PrefabLibrary | None = None,
    guid_index: GuidIndex | None = None,
) -> ContractPipelineResult:
    """Run the generic-runtime contract pipeline on ``script_infos``.

    Args:
        unity_project_path: Root of the Unity project.
        script_infos: Output of ``unity.script_analyzer.analyze_all_scripts``.
        scene_runtime: The ``scene_runtime`` artifact emitted by PR1's
            ``plan_scene_runtime`` phase. Must be the shape PR1 commits
            (``modules``, ``scenes``, ``prefabs``, ``domain_overrides``);
            unknown extra keys are ignored.
        api_key: Anthropic API key (optional -- CLI backend works without).
        use_ai / max_concurrent / serialized_field_refs: forwarded to
            ``transpile_scripts``.
        parsed_scenes / prefab_library / guid_index: the parsed Unity hierarchy
            inputs the child-ref resolver consumes to build the per-script
            ``ChildRefMap`` (resolving ``transform.GetChild(n)`` to named lookups
            before transpile). ``None``/empty leaves the map empty (no pre-rewrite).

    Returns:
        ``ContractPipelineResult`` carrying the transpile output plus
        per-require-site resolution results and a list of project-level
        fail-closed reasons. ``fail_closed`` is empty iff the project
        would convert cleanly under ``--scene-runtime=generic``.
    """
    modules: dict[str, _SceneRuntimeModule] = scene_runtime.get("modules", {}) or {}
    runtime_bearing_paths, bearing_collisions = _runtime_bearing_paths(
        modules, script_infos,
    )
    # The generic contract applies to every component class, not just the
    # placed/instance-backed ones — a MonoBehaviour spawned at runtime still
    # runs host-bound and must not ship as a legacy ``script.Parent`` Script.
    component_class_paths, component_collisions = _component_class_paths(
        modules, script_infos,
    )

    log.info(
        "[contract] %d component-class module(s) selected for the generic "
        "contract (%d of them instance-backed / boot at start)",
        len(component_class_paths), len(runtime_bearing_paths),
    )

    # Paradigm B (NON-load-bearing): the identified player controller's path,
    # keyed on the deterministic upstream ``has_character_controller`` signal
    # (NOT a transpiled-output fingerprint, §3). Threaded into transpilation so
    # the player script's prompt gets ``_PLAYER_CONTROLLER_DIRECTIVE``. Abstains
    # (empty) on 0/>1 CC-modules or any stem collision, mirroring
    # ``find_player_controllers``.
    player_controller_paths = _player_controller_paths(modules, script_infos)

    # Single producer of the #2 child-ref facts: resolve transform-rooted
    # GetChild(n) sites against the parsed hierarchy so the pre-rewrite emits
    # named lookups before transpile (and stamps the resolved/total tally for the
    # backstop). Empty when the parse inputs are absent -> no pre-rewrite.
    child_ref_map = build_child_ref_map(
        script_infos=script_infos,
        parsed_scenes=parsed_scenes,
        prefab_library=prefab_library,
        guid_index=guid_index,
    )

    transpilation = transpile_scripts(
        unity_project_path=unity_project_path,
        script_infos=script_infos,
        use_ai=use_ai,
        api_key=api_key,
        max_concurrent=max_concurrent,
        serialized_field_refs=serialized_field_refs,
        runtime_mode="generic",
        runtime_bearing_paths=runtime_bearing_paths,
        component_class_paths=component_class_paths,
        player_controller_paths=player_controller_paths,
        child_ref_map=child_ref_map,
    )

    # Build the stem-keyed require graph from the planner's modules table.
    by_stem, collisions = _build_require_graph(modules)
    require_resolutions = resolve_requires(
        transpilation.scripts, by_stem, collisions,
    )
    # Apply OK resolutions back to the script sources. ``resolve_requires``
    # is inspection-only -- without this rewrite the literal
    # ``require("@scene_runtime/<stem>")`` ships verbatim to Roblox where
    # the ``@scene_runtime`` alias fails to resolve. Missing-stem /
    # collision rows stay verbatim so the verifier's fail-closed signal
    # carries forward.
    _apply_require_resolutions(transpilation.scripts, require_resolutions)

    # Camera-facet lowering (allowlisted deterministic lowering pass, PR5):
    # route a flattened first-person DRONE/TURRET controller's look math onto
    # the SceneCameraInput runtime service so generic-mode FPS games yaw, not
    # just pitch. Structure-gated, never per-game; see camera_facet_lowering.py
    # and docs/design/camera-input-fidelity-plan.md. The PLAYER is owned by
    # paradigm C (the deterministic host authority in scene_runtime.luau, keyed
    # on the upstream ``has_character_controller`` signal), so the player script
    # is EXCLUDED from this pass (§3) -- it is never the camera/move writer here.
    # Player identity comes from the DETERMINISTIC UPSTREAM Unity signal (the
    # planner's per-module ``has_character_controller``), NOT a fingerprint of
    # the transpiled output -- the latter abstained silently on AI shape
    # variance, decoupling camera/movement/character (the systemic bug this
    # closes). ``_player_controller_paths`` fail-closes (``∅``) on 0 or >1
    # distinct CC-scripts.
    # Surface upstream player identity that did NOT cleanly bind, rather than
    # abstaining silently. ``cc_module_count`` is the number of distinct
    # CC-bearing scripts the planner saw on placed GameObjects.
    cc_module_count = sum(
        1 for m in modules.values()
        if isinstance(m, dict) and m.get("has_character_controller")
    )
    player_fail_closed: list[FailClosed] = []
    # Stale-artifact guard: a scene_runtime planned BEFORE this signal existed
    # carries NO ``has_character_controller`` key on any module. The upstream
    # identity can't be recomputed here (no scene access), so a resumed pre-fix
    # plan would silently skip player binding and report clean. Surface it so
    # the operator re-plans instead of shipping an unbound player.
    dict_mods = [m for m in modules.values() if isinstance(m, dict)]
    signal_present = any("has_character_controller" in m for m in dict_mods)
    # Gate on runtime_bearing too (present since PR1) so an artifact old enough
    # to also predate ``is_component_class`` still trips the guard (codex
    # re-review false-negative).
    has_runnable = any(
        m.get("is_component_class") or m.get("runtime_bearing")
        for m in dict_mods
    )
    if dict_mods and has_runnable and not signal_present:
        player_fail_closed.append(FailClosed(
            kind="player_signal_absent",
            detail=(
                "scene_runtime.modules carry no has_character_controller key "
                "(artifact predates the upstream player-binding signal); the "
                "player controller cannot be identified. Re-run plan_scene_"
                "runtime (re-convert) so the signal is stamped."
            ),
        ))
    # Re-source the player_unresolved fail-close on the POST-transpile
    # intersection (P1-c): ``player_controller_paths`` is keyed off the
    # PRE-transpile ``script_infos``; a player ``.cs`` that fails to read is
    # dropped from ``transpilation.scripts`` but stays in ``script_infos``.
    # Intersecting with the emitted paths restores the deleted
    # ``find_player_controllers`` POST-transpile fail-close: a transpile-dropped
    # player script => empty intersection => player_unresolved.
    emitted_player_paths = player_controller_paths & {
        Path(s.source_path) for s in transpilation.scripts
    }
    if cc_module_count > 1:
        player_fail_closed.append(FailClosed(
            kind="player_ambiguous",
            detail=(
                f"{cc_module_count} distinct scripts are co-located with a "
                f"Unity CharacterController; the generic FPS binding is "
                f"one-camera-per-client, so player identity is ambiguous and "
                f"no controller was bound."
            ),
        ))
    elif cc_module_count == 1 and not emitted_player_paths:
        player_fail_closed.append(FailClosed(
            kind="player_unresolved",
            detail=(
                "the CharacterController-bearing module has no uniquely "
                "matching transpiled script (stem mismatch / collision); the "
                "player controller was not bound."
            ),
        ))

    from converter.camera_facet_lowering import lower_camera_facet
    # Exclude the player (keyed on the deterministic upstream identity) from
    # camera-facet lowering -- paradigm C owns the player camera; this pass is
    # drone/turret-only (P1-a). Filtering by ``player_controller_paths`` (not an
    # AI fingerprint) means a strict-match player look method is never spliced.
    #
    # FAIL-CLOSE GATE makes a stray player lowering moot: when the player signal
    # is absent / ambiguous / unresolved, ``player_controller_paths`` is ∅ so
    # this filter excludes nothing -- a strict-look player-shaped script COULD
    # get ``self._cam:step`` spliced. But each of those cases appended a
    # ``player_signal_absent`` / ``player_ambiguous`` / ``player_unresolved``
    # row to ``player_fail_closed`` above, which the orchestrator turns into a
    # project-level fail-closed reason: the conversion is REJECTED and nothing
    # ships. So a lowering on the ∅ path can never reach a shipped build -- the
    # fail-close is the decisive gate, not this exclusion. (A 2nd exclusion path
    # is intentionally NOT added: it would risk the drone/turret path for no
    # shipped-output benefit.)
    lowered = lower_camera_facet(
        [s for s in transpilation.scripts
         if Path(s.source_path) not in player_controller_paths]
    )
    if lowered:
        log.info("[contract] camera-facet lowering routed %d controller(s) "
                 "to SceneCameraInput", lowered)

    # NOTE: paradigm C (the deterministic host authority in scene_runtime.luau,
    # keyed on the upstream ``has_character_controller`` signal) owns the
    # player's look + move; paradigm A is deleted, so there is no A-locator
    # abstention concept any more. The signal-based fail-closeds
    # (player_signal_absent / player_ambiguous / player_unresolved) key on C's
    # own identity and remain above.

    # Child-index handling is now done by the pre-transpile child-ref resolver
    # (``child_ref_resolver.build_child_ref_map`` + ``prerewrite_child_index``,
    # threaded into ``transpile_scripts`` above): transform-rooted
    # ``transform.GetChild(n)`` sites are resolved to named ``Find("<name>")``
    # lookups in the C# BEFORE the AI sees them, so no positional ordinal reaches
    # the output. The post-transpile ``lower_child_index`` pass is retired here;
    # the contract verifier's child-ordinal backstop fail-closes on any survivor
    # for a fully-resolved script. (``child_index_lowering.py`` stays — the
    # legacy packs still use it, and the backstop reuses ``source_has_child_index``.)

    # OnTriggerStay lowering (allowlisted deterministic lowering pass): the
    # transpiler collapses Unity OnTriggerStay onto the same ``.Touched`` EDGE
    # signal as OnTriggerEnter, so a player standing inside a turret's sight
    # volume (no fresh Touched edge) is never detected. Rewrite the specific
    # ``connectGameObjectSignal(go, "Touched", fn)`` binding whose immediately-
    # preceding origin comment is ``-- OnTriggerStay`` to the host's STAY-poll
    # primitive ``connectGameObjectSignalStay(go, fn)`` (slice 1.1). Comment-
    # keyed + binding-local, NEVER per-game; OnTriggerEnter/Exit and the
    # OnCollision* edge bindings are left untouched. See trigger_stay_lowering.py.
    from converter.trigger_stay_lowering import lower_trigger_stay
    lowered_stay = lower_trigger_stay(transpilation.scripts)
    if lowered_stay:
        log.info("[contract] OnTriggerStay lowering routed %d script(s) to "
                 "the connectGameObjectSignalStay poll primitive", lowered_stay)

    # Rifle rig-retarget lowering: consume each Camera.main-rooted
    # ``RigRootedRetargetFact`` (the resolver recorded it pre-transpile) by
    # injecting a per-instance real-Instance resolver method, rewriting the
    # consumer reads of ``self.<field>`` to call it, and neutralizing the AI's
    # camera-child Awake write. Stamps the ``rig_binding`` carrier for the
    # binding-present fail-closed verifier. Keys on the fact (deterministic
    # upstream), NEVER on the AI's ordinal output shape or a per-game string.
    from converter.rifle_rig_retarget_lowering import lower_rifle_rig_retarget
    lowered_retarget = lower_rifle_rig_retarget(transpilation.scripts, child_ref_map)
    if lowered_retarget:
        log.info("[contract] rifle rig-retarget lowering rebound %d script(s) "
                 "to the _MainCameraRig slot", lowered_retarget)

    # Roster-consumer re-lowering (allowlisted deterministic lowering pass,
    # Unit 4 Phase 2): rewrite each Addressables-label-roster consumer's
    # LoadDatabase/GetCharacter/dictionary/loaded to return the component object
    # graph read from the by_label tagged surface (Phase 1's emitted roster).
    # Keyed on the DETERMINISTIC upstream by_label fact (the module whose C# calls
    # Addressables.LoadAssetsAsync<T>("<L>", ...) for L in by_label), NEVER on an
    # AI-output fingerprint or a per-game string (D-P2-1/D-P2-2). Abstains on
    # empty by_label / no literal label; fail-closes on ambiguity / stale artifact.
    # COMPUTED here (the facts are local) but the rows are aggregated into
    # ``fail_closed`` only AFTER its definition below (it does not exist yet).
    from converter.roster_consumer_lowering import (
        RosterUnresolved,
        find_roster_consumers,
        lower_roster_consumers,
    )
    from converter.roster_assembly import resolve_roster_container_name
    addressables = scene_runtime.get("addressables") or {}
    by_label_raw = addressables.get("by_label") or {}
    by_label: dict[str, list[str]] = {
        k: v for k, v in by_label_raw.items()
        if isinstance(k, str) and isinstance(v, list)
    }
    csharp_by_path = {s.source_path: s.csharp_source for s in transpilation.scripts}
    roster_facts = find_roster_consumers(csharp_by_path, by_label)
    roster_fail_closed = _roster_fail_closed(roster_facts, by_label, scene_runtime)
    if not roster_fail_closed:
        # Diagnostic only -- the discovery key is the CollectionService tag, not
        # the container name; the emitted body does not embed it.
        container_name = resolve_roster_container_name(set())
        try:
            lowered_roster = lower_roster_consumers(
                transpilation.scripts, roster_facts, container_name,
            )
        except RosterUnresolved as exc:
            # A located fact whose LoadDatabase/GetCharacter anchors could not be
            # located -> fail closed (roster_unresolved), rather than shipping an
            # empty-loadout DB (E-P2-2). Drained through the same path as the
            # other roster rows at Site B below.
            roster_fail_closed.append(FailClosed(
                kind="roster_unresolved",
                detail=str(exc),
            ))
        else:
            if lowered_roster:
                log.info(
                    "[contract] roster re-lowering normalized %d label-roster "
                    "consumer(s) to the by_label tagged surface", lowered_roster,
                )

    # Aggregate fail-closed reasons. Verifier failures are recorded per
    # module via warnings; convert them to FailClosed rows here so the
    # orchestrator's caller has one place to read project status.
    # Player-binding rows (computed during the facet section above) lead so an
    # un-bound player is the first thing the operator sees.
    fail_closed: list[FailClosed] = list(player_fail_closed)
    # Roster-consumer rows (computed at Site A above, where the facts are local):
    # a roster ambiguity / stale-artifact / unresolved row REJECTS the conversion
    # rather than shipping a silently-unlowered (empty-loadout) place -- symmetric
    # with the player-binding fail-closeds (D-P2-7).
    fail_closed.extend(roster_fail_closed)
    # Surface stem collisions FIRST -- a colliding stem was never added to
    # the path sets, so the per-script verifier loop below can't flag it.
    # Without this surface the module silently disappears (codex P1 finding
    # on PR3a). Component-class collisions subsume runtime-bearing ones
    # (every placed MonoBehaviour is a component class), so iterate the
    # superset and dedupe by stem.
    seen_collision_stems: set[str] = set()
    for collision in (*bearing_collisions, *component_collisions):
        if collision.stem in seen_collision_stems:
            continue
        seen_collision_stems.add(collision.stem)
        fail_closed.append(FailClosed(
            kind="runtime_bearing_collision",
            detail=(
                f"component-class stem {collision.stem!r} matches "
                f"{len(collision.paths)} .cs files: "
                + ", ".join(p.name for p in collision.paths)
                + ". Disambiguate by renaming or adding "
                + "scene_runtime.modules entries with a discriminating "
                + "module_path (PR3b)."
            ),
        ))
    for script in transpilation.scripts:
        # Verify + fail-close every component class, not just placed ones:
        # a runtime-spawned MonoBehaviour that comes back broken (stub or
        # surviving violation) would still throw when first instantiated.
        if Path(script.source_path) not in component_class_paths:
            continue
        # PR3b: stub_strategy fail-closed (carry-over from PR3a P2 #1).
        # When the AI transpiler is unavailable / disabled / errored,
        # ``code_transpiler.transpile`` falls through to the stub
        # generator (``strategy="stub"``) which emits a placeholder
        # ``print(...)`` body. A component module can't host the
        # contract on stub output; ``auto`` mode must treat this as a
        # fail-closed signal to fall back to legacy.
        #
        # EXCEPTION: an INTENTIONALLY inert-stubbed component (a visual-only
        # water-shader / particle helper, OR an empty subclass of a dead base)
        # is a contract-valid inert ModuleScript (see ``_inert_component_stub``),
        # so it is a legitimate terminal state, not an AI failure. The
        # transpiler stamps ``intentional_inert_stub`` for those -- trust the
        # stamp (the empty-subclass verdict needs project context this site
        # lacks; recomputing ``_is_visual_only_script`` here would miss it and
        # turn a clean stub into a spurious stub_strategy fail-close). The
        # ``_is_visual_only_script`` recompute is retained as a backstop for any
        # stub path that predates the stamp. Only genuine fallthrough fails
        # closed.
        intentional_stub = getattr(script, "intentional_inert_stub", False)
        if (
            script.strategy != "ai"
            and not intentional_stub
            and not _is_visual_only_script(
                Path(script.source_path), script.csharp_source,
            )
        ):
            fail_closed.append(FailClosed(
                kind="stub_strategy",
                detail=(
                    f"{Path(script.source_path).name}: component-class "
                    f"module fell through to {script.strategy!r} strategy "
                    f"(AI unavailable). Stub modules cannot satisfy the "
                    f"runtime contract."
                ),
            ))
        post_warnings = [
            w for w in script.warnings if _is_post_reprompt_warning(w)
        ]
        if post_warnings:
            fail_closed.append(FailClosed(
                kind="verifier",
                detail=(
                    f"{Path(script.source_path).name}: "
                    f"{len(post_warnings)} violation(s) survived reprompt"
                ),
            ))
    for r in require_resolutions:
        if r.reason == "missing_stem":
            fail_closed.append(FailClosed(
                kind="require_missing",
                detail=(
                    f"{Path(r.from_script).name}: "
                    f"require('@scene_runtime/{r.stem}') has no matching "
                    f"module in the planner's by_stem table"
                ),
            ))
        elif r.reason == "stem_collision":
            fail_closed.append(FailClosed(
                kind="require_collision",
                detail=(
                    f"{Path(r.from_script).name}: "
                    f"require('@scene_runtime/{r.stem}') is ambiguous "
                    f"(multiple modules share the stem)"
                ),
            ))

    return ContractPipelineResult(
        transpilation=transpilation,
        require_resolutions=require_resolutions,
        fail_closed=fail_closed,
        runtime_bearing_paths=runtime_bearing_paths,
    )


def _roster_fail_closed(
    roster_facts: dict[str, RosterConsumerFact],
    by_label: dict[str, list[str]],
    scene_runtime: _SceneRuntimeArtifact,
) -> list[FailClosed]:
    """Roster-consumer fail-closed rows (pure). Mirrors the player-binding
    guards: surface ambiguity / a stale artifact rather than silently skipping
    the re-lowering and shipping an empty-loadout DB.

    ``roster_facts`` is already abstaining (empty when ``by_label`` is empty or
    labels are non-literal); this adds the PROJECT-level guards
    ``find_roster_consumers`` cannot see from a single module (D-P2-7)."""
    rows: list[FailClosed] = []

    # roster_ambiguous: >1 distinct module is the unique consumer of the SAME
    # label (never first-match-wins). Group the located facts by label.
    consumers_by_label: dict[str, list[str]] = {}
    for src, fact in roster_facts.items():
        consumers_by_label.setdefault(fact.label, []).append(src)
    for label, srcs in sorted(consumers_by_label.items()):
        if len(srcs) > 1:
            rows.append(FailClosed(
                kind="roster_ambiguous",
                detail=(
                    f"{len(srcs)} distinct modules load the same Addressables "
                    f"label {label!r} ({', '.join(sorted(srcs))}); the roster "
                    f"consumer is ambiguous and none was re-lowered. "
                    f"Disambiguate the loaders or the by_label mapping."
                ),
            ))

    # roster_signal_absent (STALE-ARTIFACT guard, analogous to
    # player_signal_absent): a non-empty ``by_label`` is expected (a roster IS
    # planned) but the artifact carries NO ``addressables`` block at all -- the
    # planner predates the Unit-4 addressables surface. We cannot recompute the
    # surface here (no scene access), so surface it instead of shipping an
    # empty-loadout DB. ``find_roster_consumers`` would silently return {}.
    addressables = scene_runtime.get("addressables")
    if by_label and addressables is None:
        rows.append(FailClosed(
            kind="roster_signal_absent",
            detail=(
                "a by_label roster is expected but scene_runtime carries no "
                "addressables block (artifact predates the Unit-4 addressables "
                "surface); the roster consumer cannot be re-lowered. Re-run "
                "plan_scene_runtime (re-convert) so the block is stamped."
            ),
        ))
    return rows


# ---------------------------------------------------------------------------
# Plan-to-path mapping
#
# PR1's ``scene_runtime.modules`` is keyed by .cs GUID. ``script_infos``
# is keyed by ``info.path`` (the .cs file path). The bridge between them
# is the GUID resolver -- which the spike harness already builds when it
# constructs the planner artifact, but we need to re-derive here so this
# module isn't dependent on planner-private state.
#
# Strategy: ``modules[script_id]["stem"]`` matches the file stem of the
# .cs path. Build ``{stem -> info.path}`` from script_infos, then walk
# modules picking out runtime-bearing ones and looking up their stems.
# When a stem collides (two .cs files with the same name in different
# folders) we cannot safely flip target -- log a warning and skip; the
# planner row will land in the collision list and force fail-closed
# downstream.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _BearingCollision:
    """A runtime-bearing planner row that couldn't be joined to a single
    ``info.path`` because more than one ``.cs`` file matches its stem.

    Surfaced from ``_runtime_bearing_paths`` so ``transpile_with_contract``
    can emit a ``FailClosed`` row even when the colliding modules never
    appear at a ``require("@scene_runtime/<stem>")`` site (the original
    "downstream require-resolution will fail" comment was incorrect for
    prefab-only or component-registry-only behaviours)."""

    stem: str
    paths: tuple[Path, ...]


def _runtime_bearing_paths(
    modules: dict[str, _SceneRuntimeModule],
    script_infos: list[ScriptInfo],
) -> tuple[frozenset[Path], list[_BearingCollision]]:
    """Map ``scene_runtime.modules`` -> ``(paths, collisions)``.

    ``paths`` is the set of ``info.path`` values for runtime-bearing
    MonoBehaviours we could unambiguously join (one ``.cs`` file per
    planner-stem). ``collisions`` enumerates the runtime-bearing rows we
    HAD to drop because the stem matched multiple ``.cs`` files; the
    orchestrator converts each into a project-level fail-closed reason."""
    # ``{stem -> list[info.path]}`` -- list because two scripts can share
    # the same stem in different folders.
    by_stem: dict[str, list[Path]] = {}
    for info in script_infos:
        stem = info.path.stem
        by_stem.setdefault(stem, []).append(info.path)

    return _join_module_paths(
        modules, by_stem, lambda m: bool(m.get("runtime_bearing")),
    )


def _component_class_paths(
    modules: dict[str, _SceneRuntimeModule],
    script_infos: list[ScriptInfo],
) -> tuple[frozenset[Path], list[_BearingCollision]]:
    """Like ``_runtime_bearing_paths`` but selects every component-class
    module, not just instance-backed ones.

    This is the set that drives the generic contract (ModuleScript target +
    generic prompt + verifier + fail-closed): in Unity every component runs
    host-bound whether authored into a scene or ``Instantiate()``-spawned,
    so a runtime-spawned MonoBehaviour must NOT be emitted as a legacy
    ``script.Parent`` Script. It is a SUPERSET of the runtime-bearing set.
    Placement (``runtime_bearing``) still governs only what the host boots
    at scene start, which is built from the planner's instance walk, not
    from this set.

    Selection is ``is_component_class OR runtime_bearing``: the OR encodes
    the invariant that every instance-backed module is a component, and
    keeps back-compat for ``scene_runtime`` artifacts serialized before the
    ``is_component_class`` field existed (a resume on such an artifact still
    routes placed MonoBehaviours to the generic contract as it did before,
    just without the new spawned-only coverage until re-planned)."""
    by_stem: dict[str, list[Path]] = {}
    for info in script_infos:
        by_stem.setdefault(info.path.stem, []).append(info.path)
    return _join_module_paths(
        modules,
        by_stem,
        lambda m: bool(m.get("is_component_class") or m.get("runtime_bearing")),
    )


def _player_controller_paths(
    modules: dict[str, _SceneRuntimeModule],
    script_infos: list[ScriptInfo],
) -> frozenset[Path]:
    """Return the UNIQUE player-controller ``info.path`` (or ``frozenset()``),
    keyed on the deterministic UPSTREAM Unity signal ``has_character_controller``
    (paradigm B directive targeting, §3). Mirrors ``find_player_controllers``'
    fail-closed abstention (``movement_facet_lowering.py:144-160``) so B targets
    EXACTLY the script paradigm C binds — no divergence.

    ``_join_module_paths`` does the stem->path JOIN but does NOT provide the
    unique-exactly-one abstention on its own (it only flags per-stem collisions
    and would add EVERY selected module's path). So the guard is added EXPLICITLY:
    1. count distinct CC-modules via the same predicate as the orchestrator's
       ``cc_module_count``; abstain (``frozenset()``) on count != 1 (0 -> non-FPS,
       >1 -> ambiguous);
    2. unpack the 2-tuple and abstain on any stem collision OR ``len(paths) != 1``
       (a join that did not resolve to exactly one path is an abstain, not a
       partial set);
    3. else return the single player path.

    Keyed on ``modules`` alone (no transpiled output): NO AI-output fingerprint.
    """
    cc_module_count = sum(
        1 for m in modules.values()
        if isinstance(m, dict) and m.get("has_character_controller")
    )
    if cc_module_count != 1:
        return frozenset()
    by_stem: dict[str, list[Path]] = {}
    for info in script_infos:
        by_stem.setdefault(info.path.stem, []).append(info.path)
    paths, collisions = _join_module_paths(
        modules,
        by_stem,
        lambda m: bool(m.get("has_character_controller")),
    )
    if collisions or len(paths) != 1:
        return frozenset()
    return paths


def _join_module_paths(
    modules: dict[str, _SceneRuntimeModule],
    by_stem: dict[str, list[Path]],
    selects: Callable[[_SceneRuntimeModule], bool],
) -> tuple[frozenset[Path], list[_BearingCollision]]:
    """Join modules matching ``selects`` to their unambiguous ``.cs`` path.
    Stem collisions (two .cs files share a stem) become ``_BearingCollision``
    rows the orchestrator turns into fail-closed reasons, rather than
    silently dropping the module to legacy."""
    paths: set[Path] = set()
    collisions: list[_BearingCollision] = []
    seen_collision_stems: set[str] = set()
    for module in modules.values():
        if not selects(module):
            continue
        stem = module.get("stem") or ""
        if not stem:
            continue
        candidates = by_stem.get(stem)
        if not candidates:
            continue
        if len(candidates) > 1:
            if stem not in seen_collision_stems:
                seen_collision_stems.add(stem)
                collisions.append(_BearingCollision(
                    stem=stem,
                    paths=tuple(sorted(candidates)),
                ))
                log.warning(
                    "[contract] stem %r appears on %d .cs files; cannot "
                    "select a path without disambiguation. Surfacing as a "
                    "project-level fail-closed reason.",
                    stem, len(candidates),
                )
            continue
        paths.add(candidates[0])
    return frozenset(paths), collisions


def _build_require_graph(
    modules: dict[str, _SceneRuntimeModule],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Build ``(by_stem, collisions)`` from the planner's modules table.

    Re-implements ``scene_runtime_planner.build_require_graph`` shape
    (the planner-private function isn't imported here on purpose -- it's
    PR1's internal helper, callable but contractually optional).
    ``by_stem[stem]`` is the planner's ``module_path`` when one exists;
    PR1's artifact omits ``module_path`` until PR3b, so for PR3a we use
    the script_id (the .cs GUID) as the resolved value. PR3b swaps in
    real module paths without changing this resolver's interface.
    """
    by_stem: dict[str, str] = {}
    collisions: dict[str, list[str]] = {}
    seen: dict[str, list[str]] = {}
    for script_id, mod in modules.items():
        stem = mod.get("stem") or ""
        if not stem:
            continue
        seen.setdefault(stem, []).append(script_id)
    for stem, ids in seen.items():
        if len(ids) == 1:
            mod = modules[ids[0]]
            # Prefer the planner's ``module_path`` when present. When it is
            # absent (PR1 artifacts omit it until the storage classifier
            # runs), the historical fallback used the script_id -- the .cs
            # GUID. But ``_apply_require_resolutions`` splices that value
            # verbatim into ``require(<value>)``, and a bare hex GUID is
            # illegal Luau ("Malformed number"): the converted module won't
            # load. Fall back instead to a runtime container lookup keyed by
            # the file stem -- the same shape the rest of the pipeline emits
            # for resolved sibling requires.
            resolved = mod.get("module_path") or _container_lookup_expr(stem)
            by_stem[stem] = resolved
        else:
            collisions[stem] = sorted(ids)
    return by_stem, collisions
