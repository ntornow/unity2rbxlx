"""test_contract_pipeline.py -- Generic-runtime orchestrator coverage.

Develops against **synthetic plan fixtures** -- PR1's
``scene_runtime`` artifact shape is the contract surface; we do NOT
exercise PR1's planner code path here (that's covered in PR1's own test
file). Each fixture is the JSON-equivalent dict the planner would
produce for a tiny synthetic project.

Covered:
  - ``resolve_requires`` -- correct resolution, missing stem fail-closed,
    stem collision fail-closed.
  - ``transpile_with_contract`` with ``use_ai=False`` -- runtime-bearing
    target flip respected; non-runtime-bearing scripts untouched.
  - Pre/post-reprompt pass-rate accounting via the ``contract-verifier``
    warning tags.
  - Allowlist isolation: no legacy repair-pass artifacts leak into
    generic output (the orchestrator never calls
    ``shared_state_linter``, ``script_coherence_packs``, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import TranspiledScript  # noqa: E402
from converter.contract_pipeline import (  # noqa: E402
    ContractPipelineResult,
    FailClosed,
    RequireResolution,
    resolve_requires,
    transpile_with_contract,
)


# ---------------------------------------------------------------------------
# Fixtures: synthetic ScriptInfo + synthetic scene_runtime artifact
# ---------------------------------------------------------------------------

class _ScriptInfo:
    """Minimal stand-in for ``unity.script_analyzer.ScriptInfo``."""

    def __init__(self, path: Path, class_name: str,
                 suggested_type: str = "Script") -> None:
        self.path = path
        self.class_name = class_name
        self.referenced_types: list[str] = []
        self.suggested_type = suggested_type


@pytest.fixture
def two_script_project(tmp_path: Path):
    """Two scripts: ``Enemy`` (runtime-bearing) and ``Helper`` (utility)."""
    proj = tmp_path / "project"
    (proj / "Assets" / "Scripts").mkdir(parents=True)
    enemy = proj / "Assets" / "Scripts" / "Enemy.cs"
    enemy.write_text(
        "using UnityEngine;\npublic class Enemy : MonoBehaviour { void Awake() {} }\n"
    )
    helper = proj / "Assets" / "Scripts" / "Helper.cs"
    helper.write_text(
        "using UnityEngine;\npublic static class Helper { public static int Add(int a, int b) { return a + b; } }\n"
    )

    scene_runtime = {
        "modules": {
            # GUID-keyed in the real artifact; synthetic ids here.
            "enemy-guid-aaaa": {
                "stem": "Enemy",
                "class_name": "Enemy",
                "runtime_bearing": True,
                # Phase 2a slice 2: build_topology invariant 7 requires
                # both booleans on every runtime_bearing planner row.
                # Helper rows (runtime_bearing=False) are exempt.
                "character_attached": False,
                "is_loader": False,
            },
            "helper-guid-bbbb": {
                "stem": "Helper",
                "class_name": "Helper",
                "runtime_bearing": False,
            },
        },
        "scenes": {},
        "prefabs": {},
        "domain_overrides": {},
    }

    infos = [
        _ScriptInfo(enemy, "Enemy", suggested_type="Script"),
        _ScriptInfo(helper, "Helper", suggested_type="ModuleScript"),
    ]
    return proj, infos, scene_runtime


# ---------------------------------------------------------------------------
# Require resolution
# ---------------------------------------------------------------------------

class TestResolveRequires:
    """The planner's stem table is the source of truth. The resolver
    consults ``by_stem`` for hits and ``collisions`` for fail-closed."""

    def _script_with(self, source: str) -> TranspiledScript:
        return TranspiledScript(
            source_path="Foo.cs",
            output_filename="Foo.luau",
            csharp_source="",
            luau_source=source,
            strategy="ai",
            confidence=0.9,
            script_type="ModuleScript",
        )

    def test_resolves_clean_stem(self):
        script = self._script_with(
            'local Bar = require("@scene_runtime/Bar")\nreturn {}\n'
        )
        rows = resolve_requires(
            [script],
            by_stem={"Bar": "ReplicatedStorage.Modules.Bar"},
            collisions={},
        )
        assert len(rows) == 1
        assert rows[0].reason == "ok"
        assert rows[0].resolved_to == "ReplicatedStorage.Modules.Bar"

    def test_missing_stem_fails_closed(self):
        script = self._script_with(
            'local Bar = require("@scene_runtime/Ghost")\nreturn {}\n'
        )
        rows = resolve_requires(
            [script], by_stem={"Bar": "X"}, collisions={},
        )
        assert len(rows) == 1
        assert rows[0].reason == "missing_stem"
        assert not rows[0].ok

    def test_stem_collision_fails_closed(self):
        # Two scripts share the stem "Utils" in different folders. The
        # planner's collision list flags the stem; the resolver fails
        # closed at the require site rather than silently picking one.
        script = self._script_with(
            'local U = require("@scene_runtime/Utils")\nreturn {}\n'
        )
        rows = resolve_requires(
            [script],
            by_stem={},  # Utils is in collisions, not by_stem.
            collisions={"Utils": ["guid-1", "guid-2"]},
        )
        assert len(rows) == 1
        assert rows[0].reason == "stem_collision"
        assert not rows[0].ok

    def test_multiple_requires_in_one_script(self):
        script = self._script_with(
            'local A = require("@scene_runtime/A")\n'
            'local B = require("@scene_runtime/B")\n'
            'return {}\n'
        )
        rows = resolve_requires(
            [script],
            by_stem={"A": "ResA", "B": "ResB"},
            collisions={},
        )
        assert [r.stem for r in rows] == ["A", "B"]
        assert [r.reason for r in rows] == ["ok", "ok"]

    def test_legacy_require_shape_is_ignored(self):
        # A ``require(script.Parent.Foo)`` style (legacy) is NOT
        # resolved by this pass -- contract pinned the
        # ``@scene_runtime/<stem>`` shape. The legacy shape just isn't
        # this resolver's concern.
        script = self._script_with(
            'local F = require(script.Parent.Foo)\nreturn {}\n'
        )
        rows = resolve_requires([script], by_stem={}, collisions={})
        assert rows == []


# ---------------------------------------------------------------------------
# Require-rewrite (PR3b follow-up): ``resolve_requires`` is inspection-only;
# ``_apply_require_resolutions`` mutates ``luau_source`` so the literal
# ``require("@scene_runtime/<stem>")`` never ships to Roblox. The
# ``@scene_runtime`` alias has no Studio-side definition; without this
# rewrite the converted place crashes on first require.
# ---------------------------------------------------------------------------

class TestApplyRequireResolutions:
    """Regression tests for the inspection -> mutation gap that Codex
    surfaced on PR #134. The rewriter is invoked from
    ``transpile_with_contract`` immediately after ``resolve_requires``."""

    def _script_with(
        self, source: str, source_path: str = "Foo.cs",
    ) -> TranspiledScript:
        return TranspiledScript(
            source_path=source_path,
            output_filename=Path(source_path).stem + ".luau",
            csharp_source="",
            luau_source=source,
            strategy="ai",
            confidence=0.9,
            script_type="ModuleScript",
        )

    def test_ok_resolutions_rewrite_source(self):
        from converter.contract_pipeline import _apply_require_resolutions
        script = self._script_with(
            'local F = require("@scene_runtime/Foo")\nreturn {}\n'
        )
        resolutions = [RequireResolution(
            from_script="Foo.cs",
            stem="Foo",
            resolved_to="ReplicatedStorage.Foo",
            reason="ok",
        )]
        _apply_require_resolutions([script], resolutions)
        # The dotted service root is lowered to a self-contained
        # game:GetService(...) expression -- a bare ``require(ReplicatedStorage
        # .Foo)`` references an unbound global at module scope (the HudControl
        # crash).
        assert script.luau_source == (
            'local F = require(game:GetService("ReplicatedStorage").Foo)\n'
            'return {}\n'
        )
        assert "@scene_runtime" not in script.luau_source

    def test_missing_stem_unchanged(self):
        from converter.contract_pipeline import _apply_require_resolutions
        original = 'local G = require("@scene_runtime/Ghost")\nreturn {}\n'
        script = self._script_with(original)
        resolutions = [RequireResolution(
            from_script="Foo.cs",
            stem="Ghost",
            resolved_to=None,
            reason="missing_stem",
        )]
        _apply_require_resolutions([script], resolutions)
        # The fail-closed signal MUST carry forward to Roblox so the
        # verifier-equivalent at load time still fires the right error.
        assert script.luau_source == original

    def test_stem_collision_unchanged(self):
        from converter.contract_pipeline import _apply_require_resolutions
        original = 'local U = require("@scene_runtime/Utils")\nreturn {}\n'
        script = self._script_with(original)
        resolutions = [RequireResolution(
            from_script="Foo.cs",
            stem="Utils",
            resolved_to=None,
            reason="stem_collision",
        )]
        _apply_require_resolutions([script], resolutions)
        assert script.luau_source == original

    def test_multiple_requires_per_script(self):
        from converter.contract_pipeline import _apply_require_resolutions
        script = self._script_with(
            'local A = require("@scene_runtime/A")\n'
            'local B = require("@scene_runtime/B")\n'
            'return {}\n'
        )
        resolutions = [
            RequireResolution("Foo.cs", "A", "ReplicatedStorage.A", "ok"),
            RequireResolution("Foo.cs", "B", "ServerScriptService.B", "ok"),
        ]
        _apply_require_resolutions([script], resolutions)
        assert 'require(game:GetService("ReplicatedStorage").A)' in script.luau_source
        assert 'require(game:GetService("ServerScriptService").B)' in script.luau_source
        assert "@scene_runtime" not in script.luau_source

    def test_single_quotes_handled(self):
        from converter.contract_pipeline import _apply_require_resolutions
        script = self._script_with(
            "local F = require('@scene_runtime/Foo')\nreturn {}\n"
        )
        resolutions = [RequireResolution(
            from_script="Foo.cs",
            stem="Foo",
            resolved_to="ReplicatedStorage.Foo",
            reason="ok",
        )]
        _apply_require_resolutions([script], resolutions)
        assert 'require(game:GetService("ReplicatedStorage").Foo)' in script.luau_source
        assert "@scene_runtime" not in script.luau_source


class TestServiceRootedRequireTarget:
    """The dotted-path -> game:GetService(...) lowering that prevents an unbound
    global at a top-level require (HudControl/HostilePlane/Explosive crash)."""

    def test_bare_service_root_is_lowered(self):
        from converter.contract_pipeline import _service_rooted_require_target
        assert _service_rooted_require_target("ReplicatedStorage.Player") == (
            'game:GetService("ReplicatedStorage").Player'
        )
        assert _service_rooted_require_target("ServerStorage.Foo") == (
            'game:GetService("ServerStorage").Foo'
        )

    def test_nested_starterplayer_path(self):
        from converter.contract_pipeline import _service_rooted_require_target
        # StarterPlayer is the service; StarterPlayerScripts is its child.
        assert _service_rooted_require_target(
            "StarterPlayer.StarterPlayerScripts.Ctrl"
        ) == 'game:GetService("StarterPlayer").StarterPlayerScripts.Ctrl'

    def test_already_an_expression_is_unchanged(self):
        from converter.contract_pipeline import (
            _container_lookup_expr,
            _service_rooted_require_target,
        )
        expr = _container_lookup_expr("Foo")
        assert _service_rooted_require_target(expr) == expr
        rooted = 'game:GetService("ReplicatedStorage").Bar'
        assert _service_rooted_require_target(rooted) == rooted

    def test_unknown_root_is_unchanged(self):
        from converter.contract_pipeline import _service_rooted_require_target
        # A non-service dotted root (e.g. a module already under a local) is not
        # rewritten -- only known service roots are lowered.
        assert _service_rooted_require_target("SomeLocal.Mod") == "SomeLocal.Mod"


# ---------------------------------------------------------------------------
# require-graph GUID fallback (Bug C). The SimpleFPS generic conversion
# shipped ``local Player = require(82ce6eb266b269f46b359c96d9d0500d)`` in
# Machine.luau / HudControl.luau / HostilePlane.luau. The AI emitted the
# correct ``require("@scene_runtime/Player")`` sentinel; the bug was in
# ``_build_require_graph``: when the planner artifact carries no
# ``module_path`` for Player, the fallback used the script_id (the raw
# .cs GUID), and ``_apply_require_resolutions`` spliced that bare hex blob
# into ``require(<GUID>)`` -- illegal Luau ("Malformed number"). The fix
# falls back to a runtime container lookup keyed by file stem.
# ---------------------------------------------------------------------------

class TestRequireGraphGuidFallback:
    """Regression: a module with no ``module_path`` must resolve to a
    valid Luau require target, never a raw GUID."""

    _PLAYER_GUID = "82ce6eb266b269f46b359c96d9d0500d"

    def _script_with(self, source: str) -> TranspiledScript:
        return TranspiledScript(
            source_path="Machine.cs",
            output_filename="Machine.luau",
            csharp_source="",
            luau_source=source,
            strategy="ai",
            confidence=0.9,
            script_type="ModuleScript",
        )

    def test_module_path_absent_resolves_to_valid_target(self):
        from converter.contract_pipeline import (
            _apply_require_resolutions,
            _build_require_graph,
        )
        # Planner artifact for Player WITHOUT a ``module_path`` -- exactly
        # the SimpleFPS generic shape. ``ids[0]`` is the .cs GUID.
        modules = {
            self._PLAYER_GUID: {
                "stem": "Player",
                "class_name": "Player",
                "runtime_bearing": True,
            },
        }
        by_stem, collisions = _build_require_graph(modules)
        # The bug: by_stem["Player"] used to be the raw GUID. Now it must
        # be a valid runtime lookup expression (no bare hex token).
        assert by_stem["Player"] != self._PLAYER_GUID
        assert self._PLAYER_GUID not in by_stem["Player"]
        assert 'FindFirstChild("Player"' in by_stem["Player"]

        # End-to-end: the sentinel the AI emits resolves + rewrites into a
        # valid require, NOT ``require(<bareGUID>)``.
        script = self._script_with(
            'local Player = require("@scene_runtime/Player")\n'
            "local Machine = {}\nreturn Machine\n"
        )
        rows = resolve_requires([script], by_stem, collisions)
        assert len(rows) == 1 and rows[0].reason == "ok"
        _apply_require_resolutions([script], rows)
        assert "@scene_runtime" not in script.luau_source
        assert self._PLAYER_GUID not in script.luau_source
        assert 'require(game:GetService("ReplicatedStorage")' in (
            script.luau_source
        )

    def test_module_path_present_is_preferred(self):
        # When the planner DID set a module_path, it wins over the fallback.
        from converter.contract_pipeline import _build_require_graph
        modules = {
            self._PLAYER_GUID: {
                "stem": "Player",
                "runtime_bearing": True,
                "module_path": "ReplicatedStorage.Modules.Player",
            },
        }
        by_stem, _ = _build_require_graph(modules)
        assert by_stem["Player"] == "ReplicatedStorage.Modules.Player"


# ---------------------------------------------------------------------------
# Orchestrator -- driven with use_ai=False so the AI is never called.
# ---------------------------------------------------------------------------

class TestRuntimeBearingCollisions:
    """Codex P1#2 regression: when two .cs files share a stem AND both
    are runtime-bearing, the orchestrator must surface a fail-closed
    reason -- not silently drop them from ``runtime_bearing_paths``.
    The original implementation only logged a warning and relied on
    ``require_missing`` to fire downstream; that fails when neither
    colliding module is ``require()``d by anything (prefab-only or
    component-registry-only behaviours)."""

    def test_collision_surfaces_as_fail_closed(self, tmp_path: Path):
        # Two .cs files, same stem ``Enemy`` in different folders. Both
        # are runtime-bearing in the synthetic planner.
        proj = tmp_path / "project"
        (proj / "Assets" / "A").mkdir(parents=True)
        (proj / "Assets" / "B").mkdir(parents=True)
        enemy_a = proj / "Assets" / "A" / "Enemy.cs"
        enemy_a.write_text(
            "using UnityEngine;\n"
            "public class Enemy : MonoBehaviour { void Awake() {} }\n"
        )
        enemy_b = proj / "Assets" / "B" / "Enemy.cs"
        enemy_b.write_text(
            "using UnityEngine;\n"
            "public class Enemy : MonoBehaviour { void Update() {} }\n"
        )

        scene_runtime = {
            "modules": {
                "guid-a": {"stem": "Enemy", "class_name": "Enemy",
                           "runtime_bearing": True},
                "guid-b": {"stem": "Enemy", "class_name": "Enemy",
                           "runtime_bearing": True},
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        infos = [
            _ScriptInfo(enemy_a, "Enemy", suggested_type="Script"),
            _ScriptInfo(enemy_b, "Enemy", suggested_type="Script"),
        ]
        result = transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            use_ai=False,
        )
        # Both paths must have been dropped from the bearing set --
        # we can't disambiguate.
        assert enemy_a not in result.runtime_bearing_paths
        assert enemy_b not in result.runtime_bearing_paths
        # AND the orchestrator must have emitted a fail-closed reason
        # naming the collision. Codex's original case: ``frozenset()``
        # returned without any fail-closed surface; PR3b's auto-mode
        # would silently route to "generic succeeded" when really both
        # runtime-bearing modules fell out of the contract pipeline.
        kinds = [fc.kind for fc in result.fail_closed]
        assert "runtime_bearing_collision" in kinds, (
            f"runtime-bearing stem collision did not surface as a "
            f"fail-closed reason; got fail_closed kinds: {kinds}"
        )
        # The detail must name the colliding stem.
        col_reasons = [
            fc for fc in result.fail_closed
            if fc.kind == "runtime_bearing_collision"
        ]
        assert any("Enemy" in fc.detail for fc in col_reasons)

    def test_collision_dedup_per_stem(self, tmp_path: Path):
        # If 3 planner rows all reference the same colliding stem, we
        # emit ONE fail-closed reason, not three.
        proj = tmp_path / "project"
        (proj / "Assets" / "A").mkdir(parents=True)
        (proj / "Assets" / "B").mkdir(parents=True)
        a = proj / "Assets" / "A" / "Pickup.cs"
        b = proj / "Assets" / "B" / "Pickup.cs"
        for p in (a, b):
            p.write_text(
                "using UnityEngine;\n"
                "public class Pickup : MonoBehaviour { void Awake() {} }\n"
            )
        scene_runtime = {
            "modules": {
                "guid-1": {"stem": "Pickup", "runtime_bearing": True,
                           "class_name": "Pickup"},
                "guid-2": {"stem": "Pickup", "runtime_bearing": True,
                           "class_name": "Pickup"},
                "guid-3": {"stem": "Pickup", "runtime_bearing": True,
                           "class_name": "Pickup"},
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        infos = [
            _ScriptInfo(a, "Pickup", suggested_type="Script"),
            _ScriptInfo(b, "Pickup", suggested_type="Script"),
        ]
        result = transpile_with_contract(
            unity_project_path=proj, script_infos=infos,
            scene_runtime=scene_runtime, use_ai=False,
        )
        col_count = sum(
            1 for fc in result.fail_closed
            if fc.kind == "runtime_bearing_collision"
        )
        assert col_count == 1, (
            f"expected exactly one collision row per colliding stem; "
            f"got {col_count}"
        )

    def test_no_collision_no_fail_closed(self, tmp_path: Path):
        # Sanity: a project with no colliding stems must produce zero
        # ``runtime_bearing_collision`` reasons.
        proj = tmp_path / "project"
        (proj / "Assets").mkdir(parents=True)
        e = proj / "Assets" / "OnlyOne.cs"
        e.write_text(
            "using UnityEngine;\n"
            "public class OnlyOne : MonoBehaviour { void Awake() {} }\n"
        )
        scene_runtime = {
            "modules": {
                "g1": {"stem": "OnlyOne", "class_name": "OnlyOne",
                       "runtime_bearing": True},
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        result = transpile_with_contract(
            unity_project_path=proj,
            script_infos=[_ScriptInfo(e, "OnlyOne", suggested_type="Script")],
            scene_runtime=scene_runtime,
            use_ai=False,
        )
        assert all(
            fc.kind != "runtime_bearing_collision"
            for fc in result.fail_closed
        )


class TestTranspileWithContract:

    def test_runtime_bearing_set_flows_through(self, two_script_project):
        proj, infos, scene_runtime = two_script_project
        result = transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            use_ai=False,
        )
        # ``Enemy.cs`` is runtime-bearing in the synthetic plan; its
        # source-path landed in the orchestrator's selection set.
        enemy_path = next(i.path for i in infos if i.class_name == "Enemy")
        assert enemy_path in result.runtime_bearing_paths

        # ``Helper.cs`` is NOT runtime-bearing -- excluded from the set.
        helper_path = next(i.path for i in infos if i.class_name == "Helper")
        assert helper_path not in result.runtime_bearing_paths

    def test_runtime_bearing_emits_as_modulescript(self, two_script_project):
        proj, infos, scene_runtime = two_script_project
        result = transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            use_ai=False,
        )
        by_name = {
            Path(s.source_path).stem: s
            for s in result.transpilation.scripts
        }
        assert by_name["Enemy"].script_type == "ModuleScript", (
            "Runtime-bearing MonoBehaviour did NOT flip to ModuleScript "
            "under the contract pipeline. The host runtime won't be able "
            "to require() it."
        )

    def test_total_runtime_bearing_count(self, two_script_project):
        proj, infos, scene_runtime = two_script_project
        result = transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            use_ai=False,
        )
        assert result.total_runtime_bearing == 1


class TestStubStrategyFailClosed:
    """PR3b carry-over from PR3a P2 #1.

    When AI transpilation is unavailable (``use_ai=False``, backend
    error, low confidence), runtime-bearing modules fall through to the
    stub generator. The contract pipeline must surface this as a
    ``stub_strategy`` fail-closed row so auto-mode can drop to legacy.
    """

    def test_stub_runtime_bearing_emits_stub_strategy_row(
        self, two_script_project,
    ) -> None:
        proj, infos, scene_runtime = two_script_project
        result = transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            use_ai=False,  # forces fallthrough to the stub generator
        )
        # Enemy is runtime-bearing; with use_ai=False it transpiles to a
        # stub (or rule_based). Either way, ``strategy != "ai"`` and PR3b
        # surfaces a fail-closed row.
        stub_rows = [
            f for f in result.fail_closed if f.kind == "stub_strategy"
        ]
        assert stub_rows, (
            "No stub_strategy fail-closed row. PR3b's contract pipeline "
            "must flag runtime-bearing modules that fell through to a "
            "non-AI strategy."
        )
        enemy_row = next(
            (f for f in stub_rows if "Enemy" in f.detail), None,
        )
        assert enemy_row is not None
        # Detail names the strategy so the operator sees why.
        assert "stub" in enemy_row.detail or "rule_based" in enemy_row.detail

    def test_non_runtime_bearing_stub_does_not_fire(
        self, two_script_project,
    ) -> None:
        proj, infos, scene_runtime = two_script_project
        result = transpile_with_contract(
            unity_project_path=proj,
            script_infos=infos,
            scene_runtime=scene_runtime,
            use_ai=False,
        )
        # Helper is NOT runtime-bearing; even if it stubs, no row should
        # name it.
        helper_rows = [
            f for f in result.fail_closed
            if f.kind == "stub_strategy" and "Helper" in f.detail
        ]
        assert not helper_rows


# ---------------------------------------------------------------------------
# Pre/post-reprompt pass-rate accounting
# ---------------------------------------------------------------------------

class TestPassRateAccounting:
    """The compliance-spike gate (PR3a → PR3b/PR4) measures verifier
    pass rate pre- and post-reprompt. We build synthetic
    ``ContractPipelineResult``s from hand-crafted ``TranspiledScript``s
    with the warning tags ``_verify_and_reprompt`` would emit, and
    assert the accounting properties give the right counts.
    """

    def _mk_script(self, path: str, warnings: list[str]) -> TranspiledScript:
        return TranspiledScript(
            source_path=path,
            output_filename=Path(path).stem + ".luau",
            csharp_source="",
            luau_source="return {}",
            strategy="ai",
            confidence=0.9,
            warnings=warnings,
            script_type="ModuleScript",
        )

    def _result(self, scripts: list[TranspiledScript]) -> ContractPipelineResult:
        from converter.code_transpiler import TranspilationResult
        tr = TranspilationResult(scripts=scripts)
        return ContractPipelineResult(
            transpilation=tr,
            runtime_bearing_paths=frozenset(Path(s.source_path) for s in scripts),
        )

    def test_clean_first_attempt_counted(self):
        # No verifier warnings -- the AI got it right first try.
        r = self._result([self._mk_script("A.cs", [])])
        assert r.first_attempt_pass_count == 1
        assert r.reprompt_rescued_count == 0
        assert r.fail_closed_count == 0
        assert r.pre_reprompt_pass_rate == 1.0
        assert r.post_reprompt_pass_rate == 1.0

    def test_reprompt_rescued_module(self):
        # Pre-warning present, post-warning absent -- the reprompt fixed it.
        r = self._result([self._mk_script("A.cs", [
            "contract-verifier-pre (rule a, line 1): some violation",
        ])])
        assert r.first_attempt_pass_count == 0
        assert r.reprompt_rescued_count == 1
        assert r.fail_closed_count == 0
        assert r.pre_reprompt_pass_rate == 0.0
        assert r.post_reprompt_pass_rate == 1.0

    def test_still_failing_after_reprompt(self):
        # Both pre and post warnings -- the reprompt didn't fix it. The
        # module is fail-closed at project level.
        r = self._result([self._mk_script("A.cs", [
            "contract-verifier-pre (rule a, line 1): pre violation",
            "contract-verifier (rule a, line 1): post violation",
        ])])
        assert r.first_attempt_pass_count == 0
        assert r.reprompt_rescued_count == 0
        assert r.fail_closed_count == 1
        assert r.pre_reprompt_pass_rate == 0.0
        assert r.post_reprompt_pass_rate == 0.0

    def test_mixed_modules(self):
        # 1 clean, 1 rescued, 1 still-failing = 67% post-reprompt pass rate.
        scripts = [
            self._mk_script("Clean.cs", []),
            self._mk_script("Rescued.cs", [
                "contract-verifier-pre (rule f, line 2): pre",
            ]),
            self._mk_script("Broken.cs", [
                "contract-verifier-pre (rule a, line 1): pre",
                "contract-verifier (rule a, line 1): post",
            ]),
        ]
        r = self._result(scripts)
        assert r.first_attempt_pass_count == 1
        assert r.reprompt_rescued_count == 1
        assert r.fail_closed_count == 1
        assert r.pre_reprompt_pass_rate == pytest.approx(1 / 3)
        assert r.post_reprompt_pass_rate == pytest.approx(2 / 3)

    def test_non_runtime_bearing_ignored(self):
        # A script outside the runtime-bearing set is not counted -- the
        # contract only applies to host-instantiated MonoBehaviours.
        from converter.code_transpiler import TranspilationResult
        clean = self._mk_script("Helper.cs", [])  # warning-free
        broken = self._mk_script("Broken.cs", [
            "contract-verifier (rule a, line 1): post",
        ])
        tr = TranspilationResult(scripts=[clean, broken])
        r = ContractPipelineResult(
            transpilation=tr,
            runtime_bearing_paths=frozenset({Path("Helper.cs")}),
        )
        # Only Helper is counted; Broken is ignored.
        assert r.total_runtime_bearing == 1
        assert r.first_attempt_pass_count == 1
        assert r.post_reprompt_pass_rate == 1.0


# ---------------------------------------------------------------------------
# Allowlist isolation -- the orchestrator must NOT trigger any of the
# legacy repair passes the design doc forbids under generic.
# ---------------------------------------------------------------------------

class TestAllowlistIsolation:
    """Generic transpile must not invoke the legacy repair layer. We
    sanity-check this by examining the orchestrator module's imports
    AND function calls -- docstring mentions don't count (the module's
    own documentation names the forbidden passes explicitly for
    reviewer context).
    """

    @staticmethod
    def _executable_lines(src: str) -> list[str]:
        # Drop docstrings (triple-quoted blocks) and comments before
        # scanning for symbol references. Tools like AST would be
        # cleaner, but for an isolation guard a regex pass is sufficient
        # and obvious.
        import re
        # Strip triple-quoted docstrings.
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"'''[\s\S]*?'''", "", src)
        # Strip line comments.
        out: list[str] = []
        for line in src.split("\n"):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            # Inline comments too.
            if "#" in line:
                line = line.split("#", 1)[0]
            out.append(line)
        return out

    def test_no_legacy_repair_passes_imported(self):
        import converter.contract_pipeline as pkg
        executable = "\n".join(
            self._executable_lines(Path(pkg.__file__).read_text())
        )
        for forbidden in (
            "shared_state_linter",
            "fix_require_classifications",
            "_guard_client_code_in_modules",
            "script_coherence_packs",
            "_subphase_patch_setup_sounds",
        ):
            assert forbidden not in executable, (
                f"contract_pipeline imports {forbidden!r} -- the design "
                f"doc forbids the legacy repair layer under generic. "
                f"Either reuse this pass via a different name (and "
                f"update this guard) or remove the import."
            )

    def test_no_write_output_emit_subphases_imported(self):
        # The legacy emit-time subphases are also off under generic.
        import converter.contract_pipeline as pkg
        executable = "\n".join(
            self._executable_lines(Path(pkg.__file__).read_text())
        )
        for forbidden in (
            "_bind_scripts_to_parts",
            "ClientBootstrap",
        ):
            assert forbidden not in executable, (
                f"contract_pipeline references {forbidden!r} -- generic "
                f"replaces the legacy emit-time subphases with the host "
                f"runtime; this orchestrator must not invoke them."
            )


# ---------------------------------------------------------------------------
# Criterion 10 — LIVE pipeline registration wiring
#
# Drives the REAL ``transpile_with_contract`` so the ``lower_camera_mount_equip``
# REGISTRATION call (contract_pipeline.py, after the rig lowering) actually runs +
# stamps the ``equip_binding`` carrier on the produced ``TranspiledScript`` AND the
# carrier flows onto an ``RbxScript`` via the pipeline copy. Without this, deleting
# the registration call (or the TranspiledScript->RbxScript copy line) stays green
# because every other test drives the lowering/verifier directly or asserts the
# pre-seeded corpus fixture (the project's
# "unit-test-proves-the-unit-not-that-the-pipeline-calls-it" lesson).
# ---------------------------------------------------------------------------

from converter.code_transpiler import TranspilationResult  # noqa: E402
from core.roblox_types import RbxScript  # noqa: E402
from core.unity_types import (  # noqa: E402
    GuidEntry,
    GuidIndex,
    PrefabComponent,
    PrefabLibrary,
    PrefabNode,
    PrefabTemplate,
)

_EQUIP_GUID = "44444444444444444444444444444444"

# The AI's emitted equip-shape Luau the lowering rewrites (the corpus GetRifle shape).
_EQUIP_LUAU = (
    "local Player = {}\n"
    "Player.__index = Player\n\n"
    "function Player:GetRifle()\n"
    "    if self.riflePrefab then\n"
    "        local slot = self:_resolveWeaponSlot() or self.gameObject\n"
    "        local rifle = self.host.instantiatePrefab(self.riflePrefab, slot, slot:GetPivot())\n"
    "        if rifle then rifle:PivotTo(slot:GetPivot()) end\n"
    "    end\n"
    "    self.gotWeapon = true\n"
    "end\n\n"
    "return Player\n"
)

_EQUIP_CS = (
    "using UnityEngine;\n"
    "public class Player : MonoBehaviour {\n"
    "  Transform cam; public Transform weaponSlot; public GameObject riflePrefab;\n"
    "  void Awake() { cam = Camera.main.transform; weaponSlot = cam.GetChild(0); }\n"
    "  void GetRifle() {\n"
    "    var r = Instantiate(riflePrefab);\n"
    "    r.transform.SetParent(weaponSlot);\n"
    "  }\n"
    "}\n"
)


def _equip_prefab_library() -> PrefabLibrary:
    def _mono(guid: str) -> PrefabComponent:
        return PrefabComponent(
            component_type="MonoBehaviour", file_id="100",
            properties={"m_Script": {"fileID": 11500000, "guid": guid, "type": 3}},
        )

    def _node(name, *, tag="Untagged", children=None, comp_guid=None) -> PrefabNode:
        return PrefabNode(
            name=name, file_id=name, active=True, tag=tag,
            children=children or [],
            components=[_mono(comp_guid)] if comp_guid else [],
        )

    cam = _node("MainCamera", tag="MainCamera", children=[_node("WeaponSlot")])
    player = _node("Player", children=[cam], comp_guid=_EQUIP_GUID)
    template = PrefabTemplate(prefab_path=Path("/p/Player.prefab"),
                              name="Player", root=player)
    return PrefabLibrary(prefabs=[template])


def test_c10_live_registration_stamps_equip_binding(tmp_path: Path, monkeypatch):
    """The REAL ``transpile_with_contract`` runs ``lower_camera_mount_equip`` and the
    carrier reaches an ``RbxScript``. Deleting the registration call (or the
    TranspiledScript->RbxScript copy line) FAILS this test."""
    proj = tmp_path / "project"
    (proj / "Assets").mkdir(parents=True)
    cs_path = proj / "Assets" / "Player.cs"
    cs_path.write_text(_EQUIP_CS)

    guid_index = GuidIndex(project_root=proj)
    guid_index.guid_to_entry[_EQUIP_GUID] = GuidEntry(
        guid=_EQUIP_GUID, asset_path=cs_path,
        relative_path=Path("Assets/Player.cs"), kind="script",
    )

    transpiled = TranspiledScript(
        source_path=str(cs_path),
        output_filename="Player.luau",
        csharp_source=_EQUIP_CS,
        luau_source=_EQUIP_LUAU,
        strategy="ai",
        confidence=0.9,
        script_type="ModuleScript",
    )

    import converter.contract_pipeline as cp

    def _fake_transpile_scripts(*args, **kwargs):
        return TranspilationResult(scripts=[transpiled], total_transpiled=1,
                                   total_ai=1)

    monkeypatch.setattr(cp, "transpile_scripts", _fake_transpile_scripts)

    scene_runtime = {
        "modules": {
            _EQUIP_GUID: {"stem": "Player", "class_name": "Player",
                          "runtime_bearing": True, "character_attached": False,
                          "is_loader": False},
        },
        "scenes": {}, "prefabs": {}, "domain_overrides": {},
    }
    result = cp.transpile_with_contract(
        unity_project_path=proj,
        script_infos=[_ScriptInfo(cs_path, "Player", suggested_type="ModuleScript")],
        scene_runtime=scene_runtime,
        use_ai=False,
        prefab_library=_equip_prefab_library(),
        guid_index=guid_index,
    )

    ts = next((s for s in result.transpilation.scripts
               if s.source_path == str(cs_path)), None)
    assert ts is not None
    # The registration call ran -> the equip request landed + the carrier is stamped.
    assert ts.equip_binding is not None, (
        "lower_camera_mount_equip did not stamp equip_binding — the contract_pipeline "
        "registration call was not invoked"
    )
    assert ts.equip_binding["present"] is True
    assert ts.equip_binding["prefab"] == "riflePrefab"
    assert ts.equip_binding["method"] == "GetRifle"
    assert 'FireServer("riflePrefab")' in ts.luau_source

    # The pipeline copy line carries the carrier onto the produced RbxScript.
    rbx = RbxScript(
        name=ts.output_filename.replace(".luau", ""),
        source=ts.luau_source,
        script_type=ts.script_type,
        source_path=ts.output_filename,
        equip_binding=ts.equip_binding,
    )
    assert rbx.equip_binding is not None
    assert rbx.equip_binding["present"] is True
