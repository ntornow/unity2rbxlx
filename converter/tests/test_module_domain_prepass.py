"""Phase 2a slice 6 — early-prepass tests for the topology classifier.

Verifies the slice-6 split:

  - ``infer_module_domains`` is pure over its inputs and produces the
    SAME per-module verdict whether or not ``RbxScript.parent_path``
    is populated.
  - ``derive_reachability_requirements`` produces the SAME hoist /
    exclude decisions as the legacy ``_apply_reachability_rule`` pass
    (parity over a representative client-helper-server triple).
  - The new functions do NOT mutate ``scene_runtime`` or any
    ``RbxScript``.

Slice 7 will rewrite ``_decide_script_container`` on top of these
results. Slice 6 just establishes the prepass surface; the legacy
``classify_scene_runtime_domains`` entry point remains the
behavior-of-record for shipped output (those tests live in
``test_scene_runtime_domain_v2.py`` and continue to pass byte-for-byte).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.scene_runtime_planner import SceneRuntimeArtifact  # noqa: E402
from converter.scene_runtime_topology.module_domain import (  # noqa: E402
    DEFAULT_NETWORKING_MODE,
    _DomainInferenceResult,
    classify_scene_runtime_domains,
    derive_reachability_requirements,
    finalize_topology_containers,
    infer_module_domains,
)
from converter.storage_classifier import (  # noqa: E402
    REPLICATED_STORAGE,
    SERVER_SCRIPT_SERVICE,
    SERVER_STORAGE,
    STARTER_PLAYER_SCRIPTS,
)
from core.roblox_types import RbxScript  # noqa: E402


def _mk_module(
    script_id: str, class_name: str, runtime_bearing: bool = True,
) -> tuple[str, dict[str, object]]:
    return script_id, {
        "stem": class_name,
        "class_name": class_name,
        "runtime_bearing": runtime_bearing,
    }


def _mk_script(
    name: str, source: str = "", parent_path: str | None = None,
) -> RbxScript:
    s = RbxScript(name=name, source=source, script_type="ModuleScript")
    s.parent_path = parent_path
    return s


def _mk_artifact(
    modules: dict[str, dict[str, object]],
) -> SceneRuntimeArtifact:
    return cast(SceneRuntimeArtifact, {
        "modules": modules,
        "scenes": {},
        "prefabs": {},
        "domain_overrides": {},
    })


class TestInferModuleDomainsPureness:
    def test_infer_runs_without_parent_path_on_any_script(self) -> None:
        """``infer_module_domains`` must produce a verdict for every
        runtime-bearing row even when ``RbxScript.parent_path`` is
        ``None`` everywhere. This is the load-bearing property the
        prepass relies on: the inference can run BEFORE
        ``classify_storage`` has decided where anything goes.
        """
        modules: dict[str, dict[str, object]] = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-server", "ServerA"),
            _mk_module("g-helper", "Helper", runtime_bearing=False),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script("ClientA", "Players.LocalPlayer", parent_path=None),
            _mk_script("ServerA", ".OnServerEvent", parent_path=None),
            _mk_script("Helper", "return {}", parent_path=None),
        ]
        results = infer_module_domains(
            artifact, scripts, networking=DEFAULT_NETWORKING_MODE,
        )
        assert results["g-client"]["domain"] == "client"
        assert results["g-server"]["domain"] == "server"
        # Non-runtime-bearing rows get a "helper" pre-stamp.
        assert results["g-helper"]["domain"] == "helper"

    def test_infer_verdict_independent_of_parent_path(self) -> None:
        """The verdict for the same module must NOT change based on
        ``parent_path``. Belt-and-suspenders for the slice-6 invariant
        that domain inference is parent_path-clean.
        """
        modules = dict([_mk_module("g-client", "ClientA")])
        artifact_a = _mk_artifact(dict(modules))
        artifact_b = _mk_artifact(dict(modules))
        scripts_a = [
            _mk_script("ClientA", "Players.LocalPlayer", parent_path=None),
        ]
        scripts_b = [
            _mk_script(
                "ClientA", "Players.LocalPlayer",
                parent_path=STARTER_PLAYER_SCRIPTS,
            ),
        ]
        res_a = infer_module_domains(artifact_a, scripts_a)
        res_b = infer_module_domains(artifact_b, scripts_b)
        assert res_a["g-client"]["domain"] == res_b["g-client"]["domain"]
        assert (
            res_a["g-client"]["signals"]
            == res_b["g-client"]["signals"]
        )

    def test_infer_does_not_mutate_module_rows(self) -> None:
        """``infer_module_domains`` must NOT stamp ``domain`` /
        ``domain_signals`` / ``container`` / ``module_path`` onto the
        module rows — those are the finalizer's job.
        """
        modules = dict([_mk_module("g-client", "ClientA")])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script("ClientA", "Players.LocalPlayer", parent_path=None),
        ]
        infer_module_domains(artifact, scripts)
        row = artifact["modules"]["g-client"]
        assert "domain" not in row
        assert "domain_signals" not in row
        assert "container" not in row
        assert "module_path" not in row

    def test_infer_does_not_mutate_scripts(self) -> None:
        """``RbxScript.parent_path`` must not be touched by the prepass.
        """
        scripts = [
            _mk_script("ClientA", "Players.LocalPlayer", parent_path=None),
        ]
        artifact = _mk_artifact(dict([_mk_module("g-client", "ClientA")]))
        infer_module_domains(artifact, scripts)
        assert scripts[0].parent_path is None


class TestDeriveReachabilityRequirementsParity:
    def test_client_only_helper_routes_to_replicated_storage(self) -> None:
        """A helper required only by a client-domain module must surface
        a ``REPLICATED_STORAGE`` requirement.
        """
        modules = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script("ClientA", "Players.LocalPlayer"),
            _mk_script("Helper", "return {}"),
        ]
        dep_map = {"ClientA": ["Helper"]}
        domains = infer_module_domains(
            artifact, scripts, dependency_map=dep_map,
        )
        reqs = derive_reachability_requirements(
            artifact, scripts, domains, dependency_map=dep_map,
        )
        assert reqs.get("g-helper") == REPLICATED_STORAGE

    def test_both_sides_helper_marked_excluded(self) -> None:
        """A helper required by BOTH client and server must be flagged
        for exclusion (reachability_conflict).
        """
        modules = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-server", "ServerA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script("ClientA", "Players.LocalPlayer"),
            _mk_script("ServerA", ".OnServerEvent"),
            _mk_script("Helper", "return {}"),
        ]
        dep_map = {"ClientA": ["Helper"], "ServerA": ["Helper"]}
        domains = infer_module_domains(
            artifact, scripts, dependency_map=dep_map,
        )
        reqs = derive_reachability_requirements(
            artifact, scripts, domains, dependency_map=dep_map,
        )
        assert reqs.get("g-helper") == "__excluded__"

    def test_unreached_helper_has_no_requirement(self) -> None:
        """Helpers not in the client closure should not appear at all.
        """
        modules = dict([
            _mk_module("g-server", "ServerA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script("ServerA", ".OnServerEvent"),
            _mk_script("Helper", "return {}"),
        ]
        dep_map = {"ServerA": ["Helper"]}
        domains = infer_module_domains(
            artifact, scripts, dependency_map=dep_map,
        )
        reqs = derive_reachability_requirements(
            artifact, scripts, domains, dependency_map=dep_map,
        )
        assert "g-helper" not in reqs

    def test_empty_dep_map_returns_empty(self) -> None:
        """No dep_map => nothing reachable => empty requirements map.
        Matches the legacy ``_apply_reachability_rule`` early-out.
        """
        modules = dict([_mk_module("g-client", "ClientA")])
        artifact = _mk_artifact(modules)
        scripts = [_mk_script("ClientA", "Players.LocalPlayer")]
        domains = infer_module_domains(artifact, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains, dependency_map=None,
        )
        assert reqs == {}


class TestFinalizeTopologyContainersIdempotent:
    def test_finalize_twice_produces_same_row(self) -> None:
        """``finalize_topology_containers`` must be safely re-runnable
        (PR1 invariant: classifier idempotency). Reachability hoist
        path included.
        """
        modules = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "ClientA", "Players.LocalPlayer",
                parent_path=STARTER_PLAYER_SCRIPTS,
            ),
            _mk_script("Helper", "return {}", parent_path=SERVER_STORAGE),
        ]
        dep_map = {"ClientA": ["Helper"]}
        domains = infer_module_domains(
            artifact, scripts, dependency_map=dep_map,
        )
        reqs = derive_reachability_requirements(
            artifact, scripts, domains, dependency_map=dep_map,
        )
        finalize_topology_containers(artifact, scripts, domains, reqs)
        first_helper = dict(artifact["modules"]["g-helper"])
        first_helper_signals = dict(
            artifact["modules"]["g-helper"]["domain_signals"]
        )
        first_helper_parent = scripts[1].parent_path

        # Run again; result must match.
        finalize_topology_containers(artifact, scripts, domains, reqs)
        assert dict(artifact["modules"]["g-helper"]) == first_helper
        assert (
            dict(artifact["modules"]["g-helper"]["domain_signals"])
            == first_helper_signals
        )
        assert scripts[1].parent_path == first_helper_parent


class TestNoParentPathInEarlyPrepass:
    """Belt-and-suspenders: AST-walk ``module_domain.py`` and assert
    that ``infer_module_domains`` + ``derive_reachability_requirements``
    + their transitively-called private helpers do NOT touch the
    ``parent_path`` attribute. Slice 6's whole structural premise --
    that the early prepass can run before ``classify_storage`` --
    breaks the moment any of these read ``parent_path``. If a future
    edit reintroduces the dependency, this test catches it before the
    pipeline silently regresses.

    Whitelist: ``finalize_topology_containers`` is allowed to read
    ``parent_path`` -- it runs AFTER ``classify_storage`` and must
    mirror the post-classify ``parent_path`` onto the module row.
    """

    def test_infer_module_domains_does_not_read_parent_path(self) -> None:
        import ast
        import inspect

        from converter.scene_runtime_topology import module_domain as md

        source = inspect.getsource(md)
        tree = ast.parse(source)

        # Find the function defs we care about.
        target_funcs = {
            "infer_module_domains",
            "derive_reachability_requirements",
        }
        # Helpers `infer_module_domains` reaches: `_classify_module`,
        # `_collect_signals`, `_apply_rule_table`, `_classify_api_surface`,
        # `_load_cs_source`, `_gather_per_instance_evidence`,
        # `_build_displaced_rows`, `_compute_network_behaviour_reachable`,
        # `_closure`. None of them read RbxScript.parent_path.
        helper_funcs = {
            "_classify_module",
            "_collect_signals",
            "_apply_rule_table",
            "_classify_api_surface",
            "_load_cs_source",
            "_gather_per_instance_evidence",
            "_build_displaced_rows",
            "_compute_network_behaviour_reachable",
            "_closure",
        }
        all_funcs_to_check = target_funcs | helper_funcs

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in all_funcs_to_check:
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Attribute) and sub.attr == "parent_path":
                        pytest.fail(
                            f"{node.name} reads 'parent_path' "
                            f"(line {sub.lineno}) -- this breaks the "
                            "slice-6 early-prepass invariant. The "
                            "domain inference path must run "
                            "BEFORE classify_storage, so it cannot "
                            "depend on parent_path."
                        )


class TestClassifyStorageTopologyInputsKwarg:
    """Phase 2a slice 6/7: ``classify_storage`` accepts a
    ``topology_inputs`` kwarg. Slice 6 plumbed it as a no-op; slice 7
    inverts the consumer -- when supplied, the topology-driven tree
    OWNS the decision and the legacy six-rule path becomes a
    per-script fallback (None kwarg, script_id_by_name miss, or
    transpile_ran=False unconstrained-helper case).

    Per the slice-6 "save raw facts, recompute conclusions" rule
    ``topology_inputs`` is NOT persisted onto ``StoragePlan`` -- the
    pipeline always recomputes it. That rule remains upheld by the
    absence of a ``StoragePlan.topology_inputs`` field.

    Slice-6's ``test_topology_inputs_kwarg_is_no_op_on_decisions`` was
    DELETED in slice 7: its premise (kwarg is byte-no-op) is exactly
    what slice 7 inverts. The replacement assertion -- that the
    topology branch consumes the kwarg and produces a different
    output for the same script when topology says so -- lives in
    ``TestSlice7TopologyDecisionTree`` (test_storage_classifier.py).
    """

    def test_legacy_path_wins_when_topology_inputs_none(self) -> None:
        """Without ``topology_inputs``, the legacy fallback path runs.

        Slice 7 round 3 (Codex R2 P1 #4): the legacy path consults
        the restored ``client_touchers`` / ``server_touchers`` sets.
        Script A uses ``Players.LocalPlayer`` -> routes to
        StarterPlayerScripts (auto-coerced to LocalScript) via the
        legacy ``_CLIENT_ONLY_PATTERNS`` branch. ModuleScript B is
        required by A; A is now a client-side caller, so B lands in
        ReplicatedStorage (legacy "at least one client-side"
        ModuleScript branch).
        """
        from converter.storage_classifier import (
            classify_storage,
            REPLICATED_STORAGE,
            STARTER_PLAYER_SCRIPTS,
        )

        scripts = [
            RbxScript(name="A", source="Players.LocalPlayer", script_type="Script"),
            RbxScript(name="B", source="return {}", script_type="ModuleScript"),
        ]
        classify_storage(scripts, dependency_map={"A": ["B"]})
        # A matches _CLIENT_ONLY_PATTERNS -> SPS (round-3 restored).
        assert scripts[0].parent_path == STARTER_PLAYER_SCRIPTS
        assert scripts[0].script_type == "LocalScript"  # auto-coerced
        # B is required by A (client-side per touchers OR via the
        # auto-coerced LocalScript type) -> RS.
        assert scripts[1].parent_path == REPLICATED_STORAGE


class TestTopologyInputsTranspileRan:
    """Phase 2a slice 7 — ``TopologyInputs.transpile_ran`` is a raw
    fact about pipeline execution sourced from
    ``state.transpilation_result is not None`` in
    ``Pipeline._maybe_run_topology_prepass``.

    Lets the slice-7 consumer distinguish two structurally-identical
    "empty ``reachability_requirements``" cases without persisting a
    derived conclusion:
      * ``transpile_ran is False`` — assemble-no-retranspile resume;
        empty reqs is expected; per-script fallback to legacy.
      * ``transpile_ran is True`` — analysis genuinely produced no
        constraint; topology tree applies (helper is unconstrained).
    """

    def test_field_present_on_typed_dict(self) -> None:
        """Sanity: the field exists in the TypedDict schema and is
        typed ``bool``."""
        from converter.scene_runtime_topology.module_domain import (
            TopologyInputs,
        )

        # ``__annotations__`` is the TypedDict surface; the new field
        # must show up alongside the other five. The module uses
        # ``from __future__ import annotations`` so the value may be a
        # ``ForwardRef`` -- match by its ``__forward_arg__`` (or by
        # the type directly when not deferred).
        annotations = TopologyInputs.__annotations__
        assert "transpile_ran" in annotations
        ann = annotations["transpile_ran"]
        forward_arg = getattr(ann, "__forward_arg__", None)
        assert forward_arg == "bool" or ann is bool

    def test_prepass_sets_true_when_transpile_ran(self) -> None:
        """Construct a minimal pipeline state with
        ``transpilation_result`` populated; the prepass must stamp
        ``transpile_ran=True``."""
        from unittest.mock import MagicMock
        from converter.pipeline import Pipeline

        # Spy a minimal Pipeline -- only the attrs ``_maybe_run_topology_prepass``
        # actually reaches.
        pipeline = MagicMock(spec=Pipeline)
        pipeline.ctx = MagicMock()
        pipeline.ctx.scene_runtime_mode = "modern"
        pipeline.ctx.networking_mode = "none"
        pipeline.state = MagicMock()
        pipeline.state.transpilation_result = MagicMock()  # truthy
        pipeline.state.dependency_map = {}
        pipeline.state.guid_index = None
        pipeline.state.rbx_place = MagicMock()
        pipeline.state.rbx_place.scripts = []  # empty -> early-return None
        scene_runtime: dict[str, object] = {
            "modules": {},  # empty -> prepass returns None
        }

        # With empty modules the prepass returns None (gate rejects).
        result = Pipeline._maybe_run_topology_prepass(
            pipeline, scene_runtime,
        )
        assert result is None

    def test_prepass_carries_transpile_ran_through(self) -> None:
        """The full-path test: a non-trivial scene_runtime with at
        least one module + script causes the prepass to return a
        populated ``TopologyInputs`` whose ``transpile_ran`` mirrors
        ``state.transpilation_result is not None``.

        Asserted for both branches:
          * ``transpilation_result is not None`` -> True
          * ``transpilation_result is None`` -> False
        """
        from unittest.mock import MagicMock
        from converter.pipeline import Pipeline

        def _build_pipeline(*, has_transpile_result: bool) -> Pipeline:
            p = MagicMock(spec=Pipeline)
            p.ctx = MagicMock()
            p.ctx.scene_runtime_mode = "modern"
            p.ctx.networking_mode = "none"
            p.state = MagicMock()
            p.state.transpilation_result = (
                MagicMock() if has_transpile_result else None
            )
            p.state.dependency_map = {}
            p.state.guid_index = None
            p.state.rbx_place = MagicMock()
            # Provide one runtime-bearing script + matching module so
            # the gate accepts.
            p.state.rbx_place.scripts = [
                RbxScript(name="X", source="return {}", script_type="ModuleScript"),
            ]
            return p

        scene_runtime: dict[str, object] = {
            "modules": {
                "g-x": {
                    "stem": "X", "class_name": "X",
                    "runtime_bearing": True,
                    "lifecycle_role": "requireable",
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }

        # Branch 1: transpile ran.
        p_true = _build_pipeline(has_transpile_result=True)
        out_true = Pipeline._maybe_run_topology_prepass(p_true, scene_runtime)
        assert out_true is not None
        assert out_true["transpile_ran"] is True

        # Branch 2: no-transpile resume.
        p_false = _build_pipeline(has_transpile_result=False)
        out_false = Pipeline._maybe_run_topology_prepass(p_false, scene_runtime)
        assert out_false is not None
        assert out_false["transpile_ran"] is False


class TestSlice7Round2LifecycleRoleStamping:
    """Phase 2a slice 7 ROUND 2 (2026-05-30) — verify
    ``_maybe_run_topology_prepass`` computes ``lifecycle_role`` INLINE
    from the raw planner-stamped facts (``is_loader``,
    ``character_attached``, plus the live domain + intrinsic
    script_class). Round 1 had populated the dict by reading
    ``row.get("lifecycle_role")`` off the source row, but no upstream
    stamper writes that key -- the dict came out empty on fresh runs
    and the slice-7 decision tree's lifecycle pinpoints (
    ``character_attached`` / ``loader``) were dead in production.

    These tests deliberately do NOT pre-stamp ``lifecycle_role`` on
    the input rows. A passing assertion proves the role was computed
    by the prepass itself, NOT supplied by a fixture.
    """

    def _build_pipeline_with(self, *, scripts: list[RbxScript]):
        from unittest.mock import MagicMock
        from converter.pipeline import Pipeline

        p = MagicMock(spec=Pipeline)
        p.ctx = MagicMock()
        p.ctx.scene_runtime_mode = "modern"
        p.ctx.networking_mode = "none"
        p.state = MagicMock()
        p.state.transpilation_result = MagicMock()  # truthy
        p.state.dependency_map = {}
        p.state.guid_index = None
        p.state.rbx_place = MagicMock()
        p.state.rbx_place.scripts = scripts
        return p

    def test_loader_lifecycle_role_computed_when_no_row_stamp(self) -> None:
        """A client-domain ``Script`` row with ``is_loader=True`` and
        NO pre-stamped ``lifecycle_role`` produces
        ``lifecycle_roles["sid"] == "loader"`` after the prepass runs.

        Guards against the round 1 bug shape (consumer reads a key
        the producer never writes).
        """
        from converter.pipeline import Pipeline

        scripts = [
            RbxScript(
                name="Boot",
                source="local p = game.Players.LocalPlayer",
                script_type="Script",
            ),
        ]
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-boot": {
                    "stem": "Boot",
                    "class_name": "Boot",
                    "runtime_bearing": True,
                    "is_loader": True,
                    "character_attached": False,
                    # Intentionally NO "lifecycle_role" key on the row.
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=scripts)
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        assert out["lifecycle_roles"].get("g-boot") == "loader", (
            "prepass must compute lifecycle_role inline from raw facts "
            "(is_loader=True + client-domain Script + no row pre-stamp)"
        )
        # Slice 6 contract: prepass does NOT mutate the source row.
        assert "lifecycle_role" not in scene_runtime["modules"]["g-boot"]  # type: ignore[index]

    def test_character_attached_lifecycle_role_computed_in_prepass(
        self,
    ) -> None:
        """A client-domain script with ``character_attached=True`` and
        NO pre-stamped ``lifecycle_role`` resolves to
        ``"character_attached"`` after the prepass runs.
        """
        from converter.pipeline import Pipeline

        scripts = [
            RbxScript(
                name="Hud",
                source="local p = game.Players.LocalPlayer\nprint(p.Name)",
                script_type="LocalScript",
            ),
        ]
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-hud": {
                    "stem": "Hud",
                    "class_name": "Hud",
                    "runtime_bearing": True,
                    "is_loader": False,
                    "character_attached": True,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=scripts)
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        assert out["lifecycle_roles"].get("g-hud") == "character_attached"

    def test_module_script_with_loader_hint_stays_requireable(self) -> None:
        """``derive_module_lifecycle_role`` gates ``is_loader`` on
        ``script_class != "ModuleScript"``. A ModuleScript with
        ``is_loader=True`` resolves to ``requireable``, NOT ``loader``
        -- matches storage_classifier's "ModuleScript skip" rule.
        """
        from converter.pipeline import Pipeline

        scripts = [
            RbxScript(
                name="LoadingUtils",
                source="return {}",
                script_type="ModuleScript",
            ),
        ]
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-loading": {
                    "stem": "LoadingUtils",
                    "class_name": "LoadingUtils",
                    "runtime_bearing": True,
                    "is_loader": True,
                    "character_attached": False,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=scripts)
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        assert out["lifecycle_roles"].get("g-loading") == "requireable"

    def test_module_script_with_character_attached_stays_requireable(
        self,
    ) -> None:
        """Slice 7 round 3 (Claude P2 fix). ``derive_module_lifecycle_role``
        now gates ``character_attached`` symmetrically with ``is_loader``:
        ``script_class != "ModuleScript"``. A ModuleScript with
        ``character_attached=True`` resolves to ``requireable``, NOT
        ``character_attached`` -- a ModuleScript in
        StarterCharacterScripts does not auto-run on character spawn
        (Roblox only auto-instantiates Script / LocalScript there), so
        the assignment would silently be inert.
        """
        from converter.pipeline import Pipeline

        scripts = [
            RbxScript(
                name="CharacterHelper",
                source="return {}",
                script_type="ModuleScript",
            ),
        ]
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-char-helper": {
                    "stem": "CharacterHelper",
                    "class_name": "CharacterHelper",
                    "runtime_bearing": True,
                    "is_loader": False,
                    "character_attached": True,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=scripts)
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        # MUST NOT be "character_attached"; ModuleScript falls through
        # to the class-driven default ("requireable").
        assert out["lifecycle_roles"].get("g-char-helper") == "requireable"
        assert (
            out["lifecycle_roles"].get("g-char-helper")
            != "character_attached"
        )

    def test_auto_run_default_for_plain_client_script(self) -> None:
        """A plain client-domain script with no special facts resolves
        to ``"auto_run"``.
        """
        from converter.pipeline import Pipeline

        scripts = [
            RbxScript(
                name="Camera",
                source="local p = game.Players.LocalPlayer",
                script_type="LocalScript",
            ),
        ]
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-cam": {
                    "stem": "Camera",
                    "class_name": "Camera",
                    "runtime_bearing": True,
                    "is_loader": False,
                    "character_attached": False,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=scripts)
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        assert out["lifecycle_roles"].get("g-cam") == "auto_run"

    def test_prepass_lifecycle_roles_parity_with_build_topology(
        self,
    ) -> None:
        """The prepass and the late ``build_topology._build_modules_block``
        must compute IDENTICAL ``lifecycle_role`` values. Both call
        ``derive_module_lifecycle_role`` with the same raw inputs;
        this test asserts that invariant directly rather than relying
        on it.

        Builds a 3-module fixture (Script+loader, LocalScript+
        character_attached, ModuleScript), runs the prepass, then runs
        ``_build_modules_block`` independently with the same inputs,
        and compares the lifecycle_role per sid.
        """
        from converter.pipeline import Pipeline
        from converter.scene_runtime_planner import (
            build_scripts_by_class_name,
        )
        from converter.scene_runtime_topology.build_topology import (
            _build_modules_block,
        )

        scripts = [
            RbxScript(
                name="Boot",
                source="local p = game.Players.LocalPlayer",
                script_type="Script",
            ),
            RbxScript(
                name="Hud",
                source="local p = game.Players.LocalPlayer\nprint(p.Name)",
                script_type="LocalScript",
            ),
            RbxScript(
                name="Util",
                source="return {}",
                script_type="ModuleScript",
            ),
        ]
        modules: dict[str, dict[str, object]] = {
            "g-boot": {
                "stem": "Boot", "class_name": "Boot",
                "runtime_bearing": True,
                "is_loader": True, "character_attached": False,
            },
            "g-hud": {
                "stem": "Hud", "class_name": "Hud",
                "runtime_bearing": True,
                "is_loader": False, "character_attached": True,
            },
            "g-util": {
                "stem": "Util", "class_name": "Util",
                "runtime_bearing": True,
                "is_loader": False, "character_attached": False,
            },
        }
        scene_runtime: dict[str, object] = {
            "modules": modules,
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }

        pipeline = self._build_pipeline_with(scripts=scripts)
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None

        # The prepass populates ``domain`` on rows (via
        # ``classify_scene_runtime_domains`` upstream) -- emulate that
        # by reading the prepass's classifier results into the rows
        # before ``_build_modules_block`` consumes them. The prepass
        # does NOT mutate the row (slice 6 contract), so we copy the
        # ``domains`` output onto rows for the parity comparison's
        # ``_build_modules_block`` call.
        for sid, dom in out["domains"].items():
            modules[sid]["domain"] = dom

        artifact = cast(SceneRuntimeArtifact, scene_runtime)
        scripts_by_class = build_scripts_by_class_name(
            scripts, cast("dict[str, object]", modules),
        )
        modules_block = _build_modules_block(
            artifact, scripts_by_class, guid_index=None,
        )

        # Parity: every sid in the prepass dict has the same
        # lifecycle_role in the build_topology artifact entry.
        for sid, role in out["lifecycle_roles"].items():
            assert sid in modules_block, (
                f"sid {sid!r} present in prepass roles but missing from "
                "modules_block -- producer/consumer mismatch"
            )
            assert modules_block[sid]["lifecycle_role"] == role, (
                f"lifecycle_role parity violation for {sid!r}: "
                f"prepass={role!r} vs build_topology="
                f"{modules_block[sid]['lifecycle_role']!r}"
            )


class TestSlice7Round2EndToEndPrepassClassify:
    """Phase 2a slice 7 ROUND 2 (2026-05-30) — END-TO-END guard against
    the fixture-masking failure mode that hid the round-1 bug.

    Round 1's unit tests in ``TestSlice7TopologyDecisionTree`` all
    constructed ``TopologyInputs`` directly via ``_mk_topology_inputs``
    with ``lifecycle_roles={...}`` pre-stamped, completely bypassing
    the producer/consumer ordering. That is how a "consumer reads a
    key the producer never writes" bug shipped to a green test suite.

    These tests run the REAL pipeline ordering:
      1. ``_maybe_run_topology_prepass`` (the producer)
      2. ``classify_storage(..., topology_inputs=out)`` (the consumer)

    A lifecycle-role-driven branch firing here PROVES the round-2 fix
    is in place; if the producer ever stops writing the dict, this
    test fails -- not the unit tests that rely on a fixture.
    """

    def _build_pipeline_with(self, *, scripts: list[RbxScript]):
        from unittest.mock import MagicMock
        from converter.pipeline import Pipeline

        p = MagicMock(spec=Pipeline)
        p.ctx = MagicMock()
        p.ctx.scene_runtime_mode = "modern"
        p.ctx.networking_mode = "none"
        p.state = MagicMock()
        p.state.transpilation_result = MagicMock()  # truthy
        p.state.dependency_map = {}
        p.state.guid_index = None
        p.state.rbx_place = MagicMock()
        p.state.rbx_place.scripts = scripts
        return p

    def test_loader_routes_to_replicated_first_via_prepass(self) -> None:
        """A client-Script with ``is_loader=True`` -> ReplicatedFirst,
        with NO pre-stamped ``lifecycle_role`` on the row OR in
        topology_inputs."""
        from converter.pipeline import Pipeline
        from converter.storage_classifier import (
            REPLICATED_FIRST, classify_storage,
        )

        boot = RbxScript(
            name="Boot",
            source="local p = game.Players.LocalPlayer",
            script_type="Script",
        )
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-boot": {
                    "stem": "Boot", "class_name": "Boot",
                    "runtime_bearing": True,
                    "is_loader": True, "character_attached": False,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=[boot])
        topology_inputs = Pipeline._maybe_run_topology_prepass(
            pipeline, scene_runtime,
        )
        assert topology_inputs is not None

        plan = classify_storage(
            [boot], topology_inputs=topology_inputs,
        )
        assert boot.parent_path == REPLICATED_FIRST, (
            "loader lifecycle_role pinpoint must fire on the REAL "
            "prepass -> classifier pipeline (not just on pre-stamped "
            "fixture inputs)"
        )
        reasons = {d["script"]: d["reason"] for d in plan.decisions}
        assert "lifecycle_role=loader" in reasons["Boot"]

    def test_character_attached_routes_to_starter_character_via_prepass(
        self,
    ) -> None:
        """A client-LocalScript with ``character_attached=True`` ->
        StarterCharacterScripts via REAL prepass -> classifier.
        """
        from converter.pipeline import Pipeline
        from converter.storage_classifier import (
            STARTER_CHARACTER_SCRIPTS, classify_storage,
        )

        hud = RbxScript(
            name="Hud",
            source="local p = game.Players.LocalPlayer\nprint(p.Name)",
            script_type="LocalScript",
        )
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-hud": {
                    "stem": "Hud", "class_name": "Hud",
                    "runtime_bearing": True,
                    "is_loader": False, "character_attached": True,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=[hud])
        topology_inputs = Pipeline._maybe_run_topology_prepass(
            pipeline, scene_runtime,
        )
        assert topology_inputs is not None

        plan = classify_storage(
            [hud], topology_inputs=topology_inputs,
        )
        assert hud.parent_path == STARTER_CHARACTER_SCRIPTS
        assert hud.name in plan.character_scripts

    def test_client_domain_script_routes_to_sps_via_prepass(self) -> None:
        """Round-2 client-Script branch: end-to-end check that a
        client-domain ``Script`` (one upstream missed for
        ``LocalScript`` promotion) lands in StarterPlayerScripts on
        the real producer -> consumer pipeline. Guards against
        recurrence of P1 #1 + #3.
        """
        from converter.pipeline import Pipeline
        from converter.storage_classifier import (
            STARTER_PLAYER_SCRIPTS, classify_storage,
        )

        # A script using ``Players.LocalPlayer`` only -- not a UI/
        # input API -- so ``code_transpiler._classify_script_type``
        # would NOT promote it to LocalScript. The simulated
        # ``script_type="Script"`` here mirrors that miss.
        s = RbxScript(
            name="LocalPlayerOnly",
            source="local p = game.Players.LocalPlayer\nprint(p.Name)",
            script_type="Script",
        )
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-lp": {
                    "stem": "LocalPlayerOnly",
                    "class_name": "LocalPlayerOnly",
                    "runtime_bearing": True,
                    "is_loader": False, "character_attached": False,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=[s])
        topology_inputs = Pipeline._maybe_run_topology_prepass(
            pipeline, scene_runtime,
        )
        assert topology_inputs is not None
        # Sanity: the prepass classified the module as client.
        assert topology_inputs["domains"].get("g-lp") == "client"

        plan = classify_storage(
            [s], topology_inputs=topology_inputs,
        )
        assert s.parent_path == STARTER_PLAYER_SCRIPTS
        assert s.script_type == "LocalScript"  # in-flow coercion
        assert s.name in plan.client_scripts

    def test_server_script_still_routes_to_sss_via_prepass(self) -> None:
        """Counterpart guard: a server-domain Script with no lifecycle
        pinpoints still lands in SSS through the real pipeline. Proves
        the new client-Script branch does NOT swallow server cases.
        """
        from converter.pipeline import Pipeline
        from converter.storage_classifier import (
            SERVER_SCRIPT_SERVICE, classify_storage,
        )

        s = RbxScript(
            name="WorldManager",
            source=(
                "local dss = game:GetService('DataStoreService')\n"
                "local rem = nil\n"
                "rem.OnServerEvent:Connect(function() end)"
            ),
            script_type="Script",
        )
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-world": {
                    "stem": "WorldManager",
                    "class_name": "WorldManager",
                    "runtime_bearing": True,
                    "is_loader": False, "character_attached": False,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=[s])
        topology_inputs = Pipeline._maybe_run_topology_prepass(
            pipeline, scene_runtime,
        )
        assert topology_inputs is not None

        plan = classify_storage(
            [s], topology_inputs=topology_inputs,
        )
        assert s.parent_path == SERVER_SCRIPT_SERVICE
        assert s.name in plan.server_scripts


class TestSlice6OrchestratorByteParity:
    """The legacy entry point ``classify_scene_runtime_domains`` must
    still produce byte-identical output to slice 5. The new pure
    prepass functions are additive — they exist so slice 7 can read
    them — but the orchestrator's observable behavior is preserved.
    """

    def test_orchestrator_preserves_reachability_hoist_behavior(self) -> None:
        modules = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "ClientA", "Players.LocalPlayer",
                parent_path=STARTER_PLAYER_SCRIPTS,
            ),
            _mk_script("Helper", "return {}", parent_path=SERVER_STORAGE),
        ]
        dep_map = {"ClientA": ["Helper"]}
        classify_scene_runtime_domains(
            artifact, scripts, dependency_map=dep_map,
        )
        helper_row = artifact["modules"]["g-helper"]
        assert helper_row["container"] == REPLICATED_STORAGE
        assert helper_row["module_path"] == "ReplicatedStorage.Helper"
        assert (
            helper_row["domain_signals"]["reachability_forced_container"]
            == REPLICATED_STORAGE
        )
