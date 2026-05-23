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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from converter.code_transpiler import (
    TranspilationResult,
    TranspiledScript,
    transpile_scripts,
)
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


class _SceneRuntimeArtifact(TypedDict, total=False):
    """Top-level ``scene_runtime`` shape PR3a reads."""

    modules: dict[str, _SceneRuntimeModule]
    # PR3a doesn't read scenes / prefabs / domain_overrides directly --
    # they survive the dict round-trip but the orchestrator doesn't
    # inspect them. ``total=False`` keeps them optional in fixtures.
    scenes: dict[str, object]
    prefabs: dict[str, object]
    domain_overrides: dict[str, str]

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
    """A warning emitted by ``_verify_and_reprompt`` (pre OR post tag)."""
    return w.startswith("contract-verifier")


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

    Returns:
        ``ContractPipelineResult`` carrying the transpile output plus
        per-require-site resolution results and a list of project-level
        fail-closed reasons. ``fail_closed`` is empty iff the project
        would convert cleanly under ``--scene-runtime=generic``.
    """
    modules: dict[str, _SceneRuntimeModule] = scene_runtime.get("modules", {}) or {}
    runtime_bearing_paths, bearing_collisions = _runtime_bearing_paths(
        modules, script_infos, unity_project_path,
    )

    log.info(
        "[contract] %d runtime-bearing MonoBehaviour(s) selected from "
        "scene_runtime.modules",
        len(runtime_bearing_paths),
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

    # Aggregate fail-closed reasons. Verifier failures are recorded per
    # module via warnings; convert them to FailClosed rows here so the
    # orchestrator's caller has one place to read project status.
    fail_closed: list[FailClosed] = []
    # Surface runtime-bearing stem collisions FIRST -- they were never
    # part of ``runtime_bearing_paths``, so the per-script verifier loop
    # below couldn't have flagged them. Without this surface the modules
    # silently disappear (codex P1 finding on PR3a).
    for collision in bearing_collisions:
        fail_closed.append(FailClosed(
            kind="runtime_bearing_collision",
            detail=(
                f"runtime-bearing stem {collision.stem!r} matches "
                f"{len(collision.paths)} .cs files: "
                + ", ".join(p.name for p in collision.paths)
                + ". Disambiguate by renaming or adding "
                + "scene_runtime.modules entries with a discriminating "
                + "module_path (PR3b)."
            ),
        ))
    for script in transpilation.scripts:
        if Path(script.source_path) not in runtime_bearing_paths:
            continue
        # PR3b: stub_strategy fail-closed (carry-over from PR3a P2 #1).
        # When the AI transpiler is unavailable / disabled / errored,
        # ``code_transpiler.transpile`` falls through to the stub
        # generator (``strategy="stub"``) which emits a placeholder
        # ``print(...)`` body. A runtime-bearing module can't host the
        # contract on stub output; ``auto`` mode must treat this as a
        # fail-closed signal to fall back to legacy.
        if script.strategy != "ai":
            fail_closed.append(FailClosed(
                kind="stub_strategy",
                detail=(
                    f"{Path(script.source_path).name}: runtime-bearing "
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
    unity_project_root: str | Path,
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

    paths: set[Path] = set()
    collisions: list[_BearingCollision] = []
    seen_collision_stems: set[str] = set()
    for module in modules.values():
        if not module.get("runtime_bearing"):
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
                    "select a runtime-bearing path without disambiguation. "
                    "Surfacing as a project-level fail-closed reason.",
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
            resolved = mod.get("module_path") or ids[0]
            by_stem[stem] = resolved
        else:
            collisions[stem] = sorted(ids)
    return by_stem, collisions
