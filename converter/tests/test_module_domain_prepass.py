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
