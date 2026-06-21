"""Regression: a transpile-skipped ``assemble`` (cache intact since #222 —
``transpilation_result is None``) must compute the SAME storage routing as a
fresh transpile, by rehydrating the transpile-derived ``dependency_map``
persisted on ``ConversionContext``.

The bug (spike-confirmed on SimpleFPS): ``state.dependency_map`` lived only on
the transient ``PipelineState`` (built inside ``transpile_scripts``), never
persisted. On a transpile-skip assemble it started empty AND
``transpile_ran = (transpilation_result is not None)`` was False, so the
topology-reachability path collapsed to legacy and a transitively
client-reachable ModuleScript (SimpleFPS ``Player``) misrouted into
ServerStorage -> client ``require(nil)``.

These tests drive the REAL path:
  transpile-side  -> real ``analyze_all_scripts`` builds the dep_map exactly as
                     ``transpile_scripts`` does -> mirrored onto ctx -> ctx.save()
  assemble-side   -> ConversionContext.load() on a fresh pipeline with
                     ``transpilation_result is None`` -> ``_classify_storage``.

NOT a hand-seeded dep_map dict (that would be tautological — see the design's
NEGATIVE-CONTROL requirement).

RED pre-fix: verified by reverting ONLY the pipeline.py edits (persist-to-ctx,
rehydrate-on-assemble, ``_topology_data_available`` gate) while KEEPING the
``ConversionContext.dependency_map`` field — i.e.
``git stash push -- converter/pipeline.py`` — so the field/round-trip work but
the routing collapses. The plan-equivalence assertion then fails RED:

  skip:  {... 'Player': 'ServerStorage' ...}
  fresh: {... 'Player': 'ReplicatedStorage' ...}

(``Player`` lands in ServerStorage on the transpile-skip assemble, exactly the
SimpleFPS regression.) The legacy fallback alone
(which routes a module DIRECTLY required by a client LocalScript to RS) does
NOT recover ``Player``: ``Player`` is only TRANSITIVELY client-reachable
(Hud[LocalScript] -> Mid -> Player) AND has a direct server caller
(GameManager), so the legacy heuristic sees it as "server-only" — only the
topology reachability closure (gated on the dep_map being available) hoists it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import TranspilationResult
from converter.pipeline import Pipeline
from converter.storage_classifier import (
    REPLICATED_STORAGE,
    SERVER_STORAGE,
    StoragePlan,
)
from core.conversion_context import ConversionContext
from core.roblox_types import RbxPlace, RbxScript
from unity.script_analyzer import analyze_all_scripts


# ---------------------------------------------------------------------------
# Fixture: a 4-module project where ``Player`` is ONLY transitively
# client-reachable AND has a direct server caller, so its correct (RS)
# placement depends on the topology reachability closure — not the legacy
# direct-caller heuristic.
#
#   Hud (LocalScript, client entry) --require--> Mid (Module)
#   Mid (Module)                     --require--> Player (Module)
#   GameManager (server Script)      --require--> Player (Module)
#
# C# field references drive the real analyzer's dependency_map; the emitted
# Luau ``require`` edges drive the assemble-time reachability closure.
# ---------------------------------------------------------------------------

_CS_FILES = {
    "Hud.cs": "using UnityEngine;\npublic class Hud : MonoBehaviour { public Mid mid; }",
    "Mid.cs": "using UnityEngine;\npublic class Mid : MonoBehaviour { public Player player; }",
    "Player.cs": "using UnityEngine;\npublic class Player : MonoBehaviour { }",
    "GameManager.cs": (
        "using UnityEngine;\n"
        "public class GameManager : MonoBehaviour { public Player player; }"
    ),
}


def _build_dependency_map_via_real_analyzer(unity_project: Path) -> dict[str, list[str]]:
    """Build the dependency_map exactly as ``Pipeline.transpile_scripts`` does
    (pipeline.py: the ``project_classes`` / ``referenced_types`` block), but via
    the REAL ``analyze_all_scripts`` over real C# files — so the persisted graph
    is the genuine transpile-time artifact, not a literal dict."""
    script_infos = analyze_all_scripts(str(unity_project))
    project_classes = {si.class_name for si in script_infos if si.class_name}
    dependency_map: dict[str, list[str]] = {}
    for si in script_infos:
        if si.class_name and si.referenced_types:
            deps = [
                t for t in si.referenced_types
                if t in project_classes and t != si.class_name
            ]
            if deps:
                dependency_map[si.class_name] = deps
    return dependency_map


def _write_cs_project(tmp_path: Path) -> Path:
    unity_project = tmp_path / "unity"
    assets = unity_project / "Assets"
    assets.mkdir(parents=True)
    for name, body in _CS_FILES.items():
        (assets / name).write_text(body)
    return unity_project


def _luau_scripts() -> list[RbxScript]:
    """The emitted-Luau side of the four modules. The ``require`` edges in the
    Luau bodies feed the assemble-time reachability closure."""
    return [
        RbxScript(
            name="Hud",
            source='local M = require(script.Parent:WaitForChild("Mid"))\nreturn 1',
            script_type="LocalScript",
        ),
        RbxScript(
            name="Mid",
            source='local P = require(script.Parent:WaitForChild("Player"))\nreturn {}',
            script_type="ModuleScript",
        ),
        RbxScript(name="Player", source="return {}", script_type="ModuleScript"),
        RbxScript(
            name="GameManager",
            source=(
                'local P = require(script.Parent:WaitForChild("Player"))\n'
                'game:GetService("Players")\nreturn 1'
            ),
            script_type="Script",
        ),
    ]


def _scene_runtime() -> dict[str, object]:
    def _row(name: str) -> dict[str, object]:
        return {
            "stem": name, "class_name": name, "runtime_bearing": True,
            "character_attached": False, "is_loader": False,
        }
    return {
        "modules": {
            "g-hud": _row("Hud"),
            "g-mid": _row("Mid"),
            "g-player": _row("Player"),
            "g-srv": _row("GameManager"),
        },
        "scenes": {}, "prefabs": {}, "domain_overrides": {},
    }


def _make_pipeline(tmp_path: Path, unity_project: Path) -> Pipeline:
    output = tmp_path / "out"
    output.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(str(unity_project), str(output))
    pipeline.ctx.scene_runtime_mode = "generic"
    place = RbxPlace()
    place.scripts = _luau_scripts()
    pipeline.state.rbx_place = place
    pipeline.ctx.scene_runtime = _scene_runtime()
    # Mirror the SimpleFPS pre-classify starting state: every module begins in
    # ServerStorage; only the topology hoist (or legacy direct-caller rule)
    # moves a script to RS.
    for s in place.scripts:
        if s.script_type == "ModuleScript":
            s.parent_path = SERVER_STORAGE
    return pipeline


def _plan_by_parent_path(pipeline: Pipeline) -> dict[str, str]:
    """The routing decision under test: script name -> final parent_path."""
    assert pipeline.state.rbx_place is not None
    return {
        s.name: s.parent_path
        for s in pipeline.state.rbx_place.scripts
    }


def _fresh_transpile_plan(tmp_path: Path, unity_project: Path) -> dict[str, str]:
    """Ground truth: classify with ``transpilation_result`` present and the
    dep_map on transient state (the fresh-transpile invocation shape)."""
    pipeline = _make_pipeline(tmp_path / "fresh", unity_project)
    pipeline.state.transpilation_result = TranspilationResult(
        scripts=[], total_transpiled=0, total_rule_based=0,
        total_ai=0, total_failed=0, total_flagged=0,
    )
    pipeline.state.dependency_map = _build_dependency_map_via_real_analyzer(
        unity_project,
    )
    pipeline._classify_storage()
    return _plan_by_parent_path(pipeline)


class TestDependencyMapPersistTranspileSkip:
    def test_skip_assemble_matches_fresh_via_persisted_dependency_map(
        self, tmp_path: Path,
    ):
        unity_project = _write_cs_project(tmp_path)

        # --- transpile-side: real analyzer -> dep_map -> mirror onto ctx -> save
        dep_map = _build_dependency_map_via_real_analyzer(unity_project)
        # Sanity: the real analyzer produced the transitive graph (not a
        # hand-seeded dict). Player is required by Mid (transitive client) AND
        # GameManager (server).
        assert dep_map == {
            "GameManager": ["Player"], "Hud": ["Mid"], "Mid": ["Player"],
        }, f"real analyzer dep_map drifted: {dep_map}"

        transpile_ctx = ConversionContext(unity_project_path=str(unity_project))
        # The production write site mirrors state.dependency_map onto
        # ctx.dependency_map; replicate that exact copy here.
        transpile_ctx.dependency_map = {k: list(v) for k, v in dep_map.items()}
        ctx_path = tmp_path / "conversion_context.json"
        transpile_ctx.save(ctx_path)

        # --- assemble-side: load ctx, transpile SKIPPED, classify
        reloaded = ConversionContext.load(ctx_path)
        assert reloaded.dependency_map == dep_map, (
            "dependency_map did not round-trip through save/load"
        )

        pipeline = _make_pipeline(tmp_path / "skip", unity_project)
        pipeline.ctx = reloaded
        pipeline.ctx.scene_runtime = _scene_runtime()
        pipeline.ctx.scene_runtime_mode = "generic"
        # Transpile skipped this invocation; transient state starts EMPTY (the
        # bug condition). The fix rehydrates ctx.dependency_map inside
        # _classify_storage.
        pipeline.state.transpilation_result = None
        pipeline.state.dependency_map = {}

        pipeline._classify_storage()
        skip_plan = _plan_by_parent_path(pipeline)

        fresh_plan = _fresh_transpile_plan(tmp_path, unity_project)

        # PRIMARY correctness bar: plan-equivalence — the transpile-skip plan
        # matches the fresh-transpile plan for ALL modules.
        assert skip_plan == fresh_plan, (
            f"transpile-skip plan != fresh-transpile plan\n"
            f"  skip:  {skip_plan}\n  fresh: {fresh_plan}"
        )
        # And specifically the regression target: Player (transitively
        # client-reachable, with a direct server caller) routes to RS, NOT SS.
        assert skip_plan["Player"] == REPLICATED_STORAGE, (
            f"Player misrouted on transpile-skip assemble: {skip_plan['Player']}"
        )

    def test_skip_assemble_fails_closed_without_persisted_dependency_map(
        self, tmp_path: Path,
    ):
        """``transpile_ran``-absent guard: with the persisted dep_map empty
        (old context, or a regression that re-empties the field), the topology
        reachability path must NOT fire — Player falls back to the legacy
        (server-only) verdict, NOT the topology RS hoist. Proves the fix is
        gated on the dep_map being present, not unconditionally on."""
        unity_project = _write_cs_project(tmp_path)

        empty_ctx = ConversionContext(unity_project_path=str(unity_project))
        # dependency_map left at its default {} (the old-context / re-emptied case)
        ctx_path = tmp_path / "conversion_context.json"
        empty_ctx.save(ctx_path)
        reloaded = ConversionContext.load(ctx_path)
        assert reloaded.dependency_map == {}

        pipeline = _make_pipeline(tmp_path / "skip_empty", unity_project)
        pipeline.ctx = reloaded
        pipeline.ctx.scene_runtime = _scene_runtime()
        pipeline.ctx.scene_runtime_mode = "generic"
        pipeline.state.transpilation_result = None
        pipeline.state.dependency_map = {}

        pipeline._classify_storage()
        plan = _plan_by_parent_path(pipeline)

        # Fail-closed: no dep_map -> topology gate stays shut -> Player keeps
        # the legacy server-only verdict (the pre-fix behavior, deliberately
        # preserved when no topology-quality graph is available).
        assert plan["Player"] == SERVER_STORAGE, (
            f"topology path fired without a persisted dep_map (not fail-closed): "
            f"Player -> {plan['Player']}"
        )

    def test_negative_control_shared_class_name_identity_stable(
        self, tmp_path: Path,
    ):
        """NEGATIVE CONTROL (identity-drift): a persisted dep_map keyed on a
        COLLIDING ``class_name`` must not corrupt routing — the caller-graph
        collision contract (``_detect_caller_graph_collisions``) EXCLUDES the
        colliding class from translation, so the shared-name edge is NOT
        blindly translated by grabbing one of the two scripts' caller sets.

        This proves routing keys off identity-stable joins (script_id /
        per-script require edges), not bare ``class_name`` coincidence:

          - ``Real`` (distinct, non-colliding) is directly client-reachable and
            routes to ReplicatedStorage by ITS OWN identity — present alongside
            the collision, it is unaffected by the colliding rows.
          - ``SharedA`` / ``SharedB`` share ``class_name="Shared"``. The
            collision contract excludes ``Shared`` from the caller_graph and
            routes the colliding ModuleScripts to ReplicatedStorage via the
            SAFE documented orphan fallback — NOT to a server container by
            mis-grabbing the other script's caller set. The key identity-
            stability property: the verdict for the colliding rows does NOT
            depend on WHICH of the two ``Shared`` scripts the name happens to
            resolve to (no name-coincidence server-misroute).
        """
        unity_project = tmp_path / "unity"
        (unity_project / "Assets").mkdir(parents=True)

        output = tmp_path / "out"
        output.mkdir(parents=True)
        pipeline = Pipeline(str(unity_project), str(output))

        place = RbxPlace()
        client = RbxScript(
            name="Hud",
            source=(
                'local R = require(script.Parent:WaitForChild("Real"))\n'
                'return 1'
            ),
            script_type="LocalScript",
        )
        real = RbxScript(name="Real", source="return {}", script_type="ModuleScript")
        # Two distinct scripts whose MODULE ROWS share class_name "Shared".
        shared_a = RbxScript(name="SharedA", source="return {}", script_type="ModuleScript")
        shared_b = RbxScript(name="SharedB", source="return {}", script_type="ModuleScript")
        for s in (real, shared_a, shared_b):
            s.parent_path = SERVER_STORAGE
        place.scripts = [client, real, shared_a, shared_b]
        pipeline.state.rbx_place = place

        scene_runtime: dict[str, object] = {
            "modules": {
                "g-hud": {
                    "stem": "Hud", "class_name": "Hud", "runtime_bearing": True,
                    "character_attached": False, "is_loader": False,
                },
                "g-real": {
                    "stem": "Real", "class_name": "Real", "runtime_bearing": True,
                    "character_attached": False, "is_loader": False,
                },
                "g-a": {
                    "stem": "SharedA", "class_name": "Shared", "runtime_bearing": True,
                    "character_attached": False, "is_loader": False,
                },
                "g-b": {
                    "stem": "SharedB", "class_name": "Shared", "runtime_bearing": True,
                    "character_attached": False, "is_loader": False,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }

        # Persist a dep_map keyed on BOTH a distinct name (Real) and the
        # COLLIDING name (Shared), then round-trip through save/load.
        ctx = ConversionContext(unity_project_path=str(unity_project))
        ctx.dependency_map = {"Hud": ["Real", "Shared"]}
        ctx_path = tmp_path / "conversion_context.json"
        ctx.save(ctx_path)
        pipeline.ctx = ConversionContext.load(ctx_path)
        pipeline.ctx.scene_runtime = scene_runtime
        pipeline.ctx.scene_runtime_mode = "generic"
        pipeline.state.transpilation_result = None
        pipeline.state.dependency_map = {}

        # Must not raise on the collision; the distinct module routes by its
        # own identity, and the colliding rows take the safe orphan fallback —
        # NEVER a server-misroute from name coincidence.
        pipeline._classify_storage()
        plan = {s.name: s.parent_path for s in place.scripts}
        assert plan["Real"] == REPLICATED_STORAGE, (
            f"distinct non-colliding module mis-routed alongside a collision: "
            f"Real -> {plan['Real']}"
        )
        # Identity-stable: the colliding rows must NOT be server-misrouted by
        # the shared-name dep_map edge resolving to the wrong script. The
        # collision contract sends them to RS via the orphan fallback — the key
        # property is that NEITHER lands in ServerStorage off a name grab.
        assert plan["SharedA"] != SERVER_STORAGE, (
            f"colliding row SharedA server-misrouted by name coincidence: "
            f"{plan['SharedA']}"
        )
        assert plan["SharedB"] != SERVER_STORAGE, (
            f"colliding row SharedB server-misrouted by name coincidence: "
            f"{plan['SharedB']}"
        )
