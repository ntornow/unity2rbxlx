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
from core.roblox_types import RbxScript, ScriptType  # noqa: E402


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
    script_type: str = "ModuleScript",
) -> RbxScript:
    s = RbxScript(
        name=name, source=source,
        script_type=cast("ScriptType", script_type),
    )
    s.intrinsic_script_type = cast("ScriptType", script_type)
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


def _require(name: str) -> str:
    """Emitted-require Luau fragment that ``extract_require_edges`` reads
    as an edge to ``name``."""
    return f'require(script.Parent:FindFirstChild("{name}"))\n'


def _edges(
    scripts: list[RbxScript],
) -> dict[str, set[str]]:
    """Build the ``require_edges_by_name`` graph the prepass walks, the
    same way ``_maybe_run_topology_prepass`` does."""
    from converter.roblox_dead_modules import extract_require_edges

    known = frozenset(s.name for s in scripts if s.name)
    return {
        s.name: extract_require_edges(s.source, known)
        for s in scripts if s.name
    }


def _by_sid(
    modules: dict[str, dict[str, object]],
    scripts: list[RbxScript],
) -> dict[str, RbxScript]:
    """Build ``script_id -> RbxScript`` via the canonical join, the same
    way ``_maybe_run_topology_prepass`` does."""
    from converter.scene_runtime_planner import build_script_id_by_name

    by_name = {s.name: s for s in scripts if s.name}
    sid_by_name = build_script_id_by_name(
        scripts,
        cast("dict[str, object]", modules),
    )
    return {
        sid: by_name[name]
        for name, sid in sid_by_name.items()
        if name in by_name
    }


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
        """AC4: a helper module required only by a client ENTRY
        (LocalScript) via an emitted require surfaces a
        ``REPLICATED_STORAGE`` requirement.
        """
        modules = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "ClientA", _require("Helper"), script_type="LocalScript",
            ),
            _mk_script("Helper", "return {}"),
        ]
        domains = infer_module_domains(artifact, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
        )
        assert reqs.get("g-helper") == REPLICATED_STORAGE

    def test_both_sides_helper_marked_excluded(self) -> None:
        """AC1: a helper module required by BOTH a client LocalScript
        entry AND a server Script must be flagged ``__excluded__``.
        """
        modules = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-server", "ServerA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "ClientA", _require("Helper"), script_type="LocalScript",
            ),
            _mk_script(
                "ServerA", ".OnServerEvent\n" + _require("Helper"),
                script_type="Script",
            ),
            _mk_script("Helper", "return {}"),
        ]
        domains = infer_module_domains(artifact, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
        )
        assert reqs.get("g-helper") == "__excluded__"

    def test_unreached_helper_has_no_requirement(self) -> None:
        """AC5: a helper not in the client closure produces no entry.
        """
        modules = dict([
            _mk_module("g-server", "ServerA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "ServerA", ".OnServerEvent\n" + _require("Helper"),
                script_type="Script",
            ),
            _mk_script("Helper", "return {}"),
        ]
        domains = infer_module_domains(artifact, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
        )
        assert "g-helper" not in reqs

    def test_empty_edges_returns_empty(self) -> None:
        """No emitted-require graph => nothing reachable => empty map.
        Matches the legacy early-out.
        """
        modules = dict([_mk_module("g-client", "ClientA")])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "ClientA", "Players.LocalPlayer", script_type="LocalScript",
            ),
        ]
        domains = infer_module_domains(artifact, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name={},
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
        )
        assert reqs == {}

    def test_low_confidence_client_script_does_not_seed_server_subtree(
        self,
    ) -> None:
        """AC2: a Script with inferred ``domain==client`` but
        ``low_confidence==True`` (zero-signal ``networking=none``
        fallback) is NOT a client seed, so a server-only module it
        transitively requires gets NO reachability requirement.
        """
        modules = dict([
            _mk_module("g-weak", "WeakClient"),
            _mk_module("g-srvmod", "ServerMod"),
        ])
        artifact = _mk_artifact(modules)
        # WeakClient: a plain Script with no strong signal -> the
        # zero-signal fallback classifies it client+low_confidence.
        scripts = [
            _mk_script(
                "WeakClient", _require("ServerMod"), script_type="Script",
            ),
            _mk_script("ServerMod", "return {}"),
        ]
        domains = infer_module_domains(artifact, scripts)
        assert domains["g-weak"]["domain"] == "client"
        assert domains["g-weak"]["low_confidence"] is True
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
        )
        assert "g-srvmod" not in reqs

    def test_low_confidence_loader_role_script_does_not_seed_server_subtree(
        self,
    ) -> None:
        """Security regression (round 2, codex BLOCKING): a zero-signal
        plain ``Script`` with a loader-NAME (``Bootstrap``) infers
        ``domain==client + low_confidence==True``, and
        ``derive_module_lifecycle_role`` returns role ``"loader"`` for it.
        The lifecycle-role seed arm MUST gate on ``low_confidence is
        False`` so this row does NOT seed — otherwise its server-only
        require subtree (``ServerSecret``, which uses DataStoreService)
        leaks into ReplicatedStorage.
        """
        modules = dict([
            _mk_module("g-boot", "Bootstrap"),
            _mk_module("g-secret", "ServerSecret"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            # Bootstrap: a plain Script, no domain signal -> client +
            # low_confidence. Its name trips the broad ``is_loader`` regex,
            # so its lifecycle role is "loader".
            _mk_script(
                "Bootstrap", _require("ServerSecret"), script_type="Script",
            ),
            _mk_script(
                "ServerSecret",
                'game:GetService("DataStoreService")\nreturn {}',
            ),
        ]
        domains = infer_module_domains(artifact, scripts)
        assert domains["g-boot"]["domain"] == "client"
        assert domains["g-boot"]["low_confidence"] is True
        assert domains["g-secret"]["domain"] == "server"
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            # The role arm would seed g-boot WITHOUT the low_confidence
            # gate; pass the genuine "loader" role to exercise that arm.
            lifecycle_roles={"g-boot": "loader"},
        )
        # The low-confidence loader must NOT seed: the server module gets
        # NO reachability requirement and stays out of ReplicatedStorage.
        assert "g-secret" not in reqs

    def test_server_domain_module_client_required_gets_rs_transitively(
        self,
    ) -> None:
        """AC3 (cascade fix): a module with ``domain==server`` (e.g. a
        DataStoreService signal) that a client LocalScript transitively
        requires becomes client-visible — proving the domain-vs-placement
        mismatch is fixed. The intermediate is ALSO a server seed (its
        domain is server), so it + its deep helper are reached from both
        sides -> ``__excluded__``; the storage classifier routes
        ``__excluded__`` to ReplicatedStorage, so both are client-visible.
        Deep transitive coverage is exercised (DeepHelper, not just the
        direct require, gets a requirement).
        """
        modules = dict([
            _mk_module("g-entry", "LocalEntry"),
            _mk_module("g-srvdom", "ServerDomainMod"),
            _mk_module("g-deep", "DeepHelper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "LocalEntry", _require("ServerDomainMod"),
                script_type="LocalScript",
            ),
            # ServerDomainMod has a server-domain signal but is a
            # ModuleScript required by the client entry.
            _mk_script(
                "ServerDomainMod",
                "game:GetService(\"DataStoreService\")\n"
                + _require("DeepHelper"),
            ),
            _mk_script("DeepHelper", "return {}"),
        ]
        domains = infer_module_domains(artifact, scripts)
        assert domains["g-srvdom"]["domain"] == "server"
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
        )
        # Per-candidate self-exclusion: for candidate ServerDomainMod the
        # only server seed is ITSELF, excluded from its own server
        # closure -> not server-reached -> plain RS. DeepHelper IS reached
        # by the server seed ServerDomainMod -> both-sides -> __excluded__.
        # Both route to ReplicatedStorage at the storage classifier, so
        # the cascade-fix property holds: neither is left server-only.
        assert reqs.get("g-srvdom") == REPLICATED_STORAGE
        assert reqs.get("g-deep") == "__excluded__"

    def test_client_required_low_conf_helper_not_a_seed_gets_rs(self) -> None:
        """AC3 variant: a ModuleScript with NO domain signal — so it
        infers client-default + low_confidence, NOT a server seed — that
        is reached ONLY via the client LocalScript entry. Because no
        server Script seeds it, the client-reached helper + its deep dep
        both route to plain ``REPLICATED_STORAGE`` (no both-sides
        conflict). (Despite the historical ``SrvHelper`` name this row
        carries zero server signal; the name is incidental.)
        """
        modules = dict([
            _mk_module("g-entry", "LocalEntry2"),
            _mk_module("g-srvhelper", "SrvHelper"),
            _mk_module("g-deep", "Deep2"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "LocalEntry2", _require("SrvHelper"),
                script_type="LocalScript",
            ),
            # SrvHelper: a ModuleScript with no domain signal -> client
            # default (low_confidence) -> NOT a server seed. It is reached
            # only via the client entry.
            _mk_script("SrvHelper", _require("Deep2")),
            _mk_script("Deep2", "return {}"),
        ]
        domains = infer_module_domains(artifact, scripts)
        # Confirm the fixture is what the docstring claims: low-confidence
        # client-default, not a server verdict.
        assert domains["g-srvhelper"]["domain"] == "client"
        assert domains["g-srvhelper"]["low_confidence"] is True
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
        )
        assert reqs.get("g-srvhelper") == REPLICATED_STORAGE
        assert reqs.get("g-deep") == REPLICATED_STORAGE

    def test_transpile_ran_false_returns_empty(self) -> None:
        """AC6: with ``transpile_ran=False`` the function returns ``{}``
        even when ``RbxScript.source`` carries emitted requires on disk
        (byte-identical resume contract).
        """
        modules = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "ClientA", _require("Helper"), script_type="LocalScript",
            ),
            _mk_script("Helper", "return {}"),
        ]
        domains = infer_module_domains(artifact, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
            transpile_ran=False,
        )
        assert reqs == {}

    def test_local_script_module_row_never_constrained(self) -> None:
        """AC11: a LocalScript-backed module row that is in the client
        closure (it's a seed) receives NO reachability requirement —
        the candidate predicate restricts to intrinsic ``ModuleScript``,
        so rule-3 never reroutes a LocalScript to ReplicatedStorage.
        """
        modules = dict([
            _mk_module("g-hud", "Hud"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "Hud", _require("Helper"), script_type="LocalScript",
            ),
            _mk_script("Helper", "return {}"),
        ]
        domains = infer_module_domains(artifact, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=_by_sid(modules, scripts),
            lifecycle_roles={},
        )
        # The LocalScript row is a SEED, never a candidate.
        assert "g-hud" not in reqs
        # Its required helper IS a ModuleScript candidate -> RS.
        assert reqs.get("g-helper") == REPLICATED_STORAGE


class TestFinalizeTopologyContainersIdempotent:
    def test_finalize_twice_produces_same_row(self) -> None:
        """AC8: ``finalize_topology_containers`` must be safely
        re-runnable (PR1 invariant: classifier idempotency). Reachability
        hoist path included.
        """
        modules = dict([
            _mk_module("g-client", "ClientA"),
            _mk_module("g-helper", "Helper"),
        ])
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "ClientA", _require("Helper"),
                parent_path=STARTER_PLAYER_SCRIPTS,
                script_type="LocalScript",
            ),
            _mk_script("Helper", "return {}", parent_path=SERVER_STORAGE),
        ]
        domains = infer_module_domains(artifact, scripts)
        by_sid = _by_sid(modules, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=by_sid,
            lifecycle_roles={},
        )
        finalize_topology_containers(
            artifact, scripts, domains, reqs, script_by_sid=by_sid,
        )
        first_helper = dict(artifact["modules"]["g-helper"])
        first_helper_signals = dict(
            artifact["modules"]["g-helper"]["domain_signals"]
        )
        first_helper_parent = scripts[1].parent_path

        # Run again; result must match.
        finalize_topology_containers(
            artifact, scripts, domains, reqs, script_by_sid=by_sid,
        )
        assert dict(artifact["modules"]["g-helper"]) == first_helper
        assert (
            dict(artifact["modules"]["g-helper"]["domain_signals"])
            == first_helper_signals
        )
        assert scripts[1].parent_path == first_helper_parent

    def test_finalizer_collision_case_routes_correct_row_by_sid(
        self,
    ) -> None:
        """AC7: two modules sharing a ``class_name`` (distinct ``sid``s,
        distinct stems) where one is in the client closure — the prepass
        routes the reached one by ``sid`` and the finalizer mirrors
        ``container``/``module_path``/``parent_path`` onto the CORRECT
        row via ``script_by_sid`` (the class-name join would have dropped
        BOTH colliding rows).
        """
        # Both helper rows share class_name "Util" but have distinct
        # stems (file names) "UtilA" / "UtilB". script_by_sid joins on
        # the stem fallback, so each sid resolves to its own script.
        modules: dict[str, dict[str, object]] = {
            "g-entry": {
                "stem": "Entry", "class_name": "Entry",
                "runtime_bearing": True,
            },
            "g-util-a": {
                "stem": "UtilA", "class_name": "Util",
                "runtime_bearing": True,
            },
            "g-util-b": {
                "stem": "UtilB", "class_name": "Util",
                "runtime_bearing": True,
            },
        }
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "Entry", _require("UtilA"),
                parent_path=STARTER_PLAYER_SCRIPTS,
                script_type="LocalScript",
            ),
            _mk_script("UtilA", "return {}", parent_path=SERVER_STORAGE),
            _mk_script("UtilB", "return {}", parent_path=SERVER_STORAGE),
        ]
        domains = infer_module_domains(artifact, scripts)
        by_sid = _by_sid(modules, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=by_sid,
            lifecycle_roles={},
        )
        # Only the reached row (UtilA) gets a requirement.
        assert reqs.get("g-util-a") == REPLICATED_STORAGE
        assert "g-util-b" not in reqs

        finalize_topology_containers(
            artifact, scripts, domains, reqs, script_by_sid=by_sid,
        )
        # The CORRECT row is mirrored to ReplicatedStorage by sid.
        assert artifact["modules"]["g-util-a"]["container"] == REPLICATED_STORAGE
        assert (
            artifact["modules"]["g-util-a"]["module_path"]
            == "ReplicatedStorage.UtilA"
        )
        assert scripts[1].parent_path == REPLICATED_STORAGE
        # The non-reached colliding row stays put.
        assert scripts[2].parent_path == SERVER_STORAGE

    def test_finalizer_excluded_collision_row_gets_container_and_path(
        self,
    ) -> None:
        """Round 2, codex MAJOR: a class-name collision where BOTH a
        client entry AND a server script require the SAME helper (UtilA)
        -> the helper is reached from both sides -> ``__excluded__``. The
        base container/module_path stamping must be sid-aware so the
        colliding ``__excluded__`` row ends with ``domain=="excluded"``
        AND a correct ``container``/``module_path`` (not empty). The
        class-name join alone would have dropped the colliding row,
        leaving it with no container/module_path.
        """
        modules: dict[str, dict[str, object]] = {
            "g-entry": {
                "stem": "Entry", "class_name": "Entry",
                "runtime_bearing": True,
            },
            "g-srv": {
                "stem": "Srv", "class_name": "Srv",
                "runtime_bearing": True,
            },
            # UtilA / UtilB collide on class_name "Util" (distinct stems).
            "g-util-a": {
                "stem": "UtilA", "class_name": "Util",
                "runtime_bearing": True,
            },
            "g-util-b": {
                "stem": "UtilB", "class_name": "Util",
                "runtime_bearing": True,
            },
        }
        artifact = _mk_artifact(modules)
        scripts = [
            _mk_script(
                "Entry", _require("UtilA"),
                parent_path=STARTER_PLAYER_SCRIPTS,
                script_type="LocalScript",
            ),
            # Server script (DataStoreService signal) ALSO requires UtilA
            # -> both-sides reach -> UtilA becomes __excluded__.
            _mk_script(
                "Srv",
                'game:GetService("DataStoreService")\n' + _require("UtilA"),
                parent_path=SERVER_SCRIPT_SERVICE,
                script_type="Script",
            ),
            _mk_script("UtilA", "return {}", parent_path=SERVER_STORAGE),
            _mk_script("UtilB", "return {}", parent_path=SERVER_STORAGE),
        ]
        domains = infer_module_domains(artifact, scripts)
        assert domains["g-srv"]["domain"] == "server"
        by_sid = _by_sid(modules, scripts)
        reqs = derive_reachability_requirements(
            artifact, scripts, domains,
            require_edges_by_name=_edges(scripts),
            script_by_sid=by_sid,
            lifecycle_roles={},
        )
        assert reqs.get("g-util-a") == "__excluded__"

        finalize_topology_containers(
            artifact, scripts, domains, reqs, script_by_sid=by_sid,
        )
        util_a = artifact["modules"]["g-util-a"]
        assert util_a["domain"] == "excluded"
        # The colliding __excluded__ row still gets its container +
        # module_path (sid-aware base stamping), not empty.
        assert util_a["container"] == SERVER_STORAGE
        assert util_a["module_path"] == f"{SERVER_STORAGE}.UtilA"


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
            "_client_entry_seed_names",
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

    def test_prepass_join_unified_with_routing_on_class_name_collision(
        self,
    ) -> None:
        """Round 4 (R3 review P2-NEW-B). When two modules share a
        ``class_name`` but have DISTINCT stems, the lifecycle-role
        prepass MUST still reach the lifecycle assignment for both
        rows — same as the routing join in
        ``_decide_script_container_from_topology``.

        Pre-R4 the prepass joined on class_name only via
        ``build_scripts_by_class_name`` (collision-exclude on
        class_name), but the routing path joined on
        ``script_id_by_name`` (collision-exclude on BOTH class_name
        AND stem, with class_name → stem fallback). Disagreement
        case: two modules with colliding ``class_name="X"`` but
        distinct stems ``a`` / ``b`` were excluded from the prepass
        index (their script_class fell back to ``""``), which
        silently demoted their ``character_attached`` / ``loader``
        lifecycle roles to the default ``"auto_run"`` branch — the
        exact silent-demotion failure mode the slice-3
        degraded-service contract was designed to surface.

        R4 unifies the prepass on ``build_script_id_by_name`` (the
        canonical helper) and inverts it to a sid → RbxScript map.
        Both rows now reach the lifecycle assignment via the stem
        fallback the routing path already uses.

        This test pins the unification: two scripts named ``a`` and
        ``b`` (distinct stems) with colliding ``class_name="X"``
        and non-default lifecycle attrs MUST land in
        ``lifecycle_roles`` with their derived roles
        (``loader`` and ``character_attached``), NOT the empty-string
        default that ``derive_module_lifecycle_role`` returns when
        ``script_class`` is also empty.
        """
        from converter.pipeline import Pipeline

        # Two scripts: file stems "a" / "b" matching the modules'
        # distinct stem field. Their class_name both collide as "X" —
        # the collision excludes the class_name keyspace, so the
        # canonical helper falls back to stem (which is collision-free).
        scripts = [
            RbxScript(
                # script.name == module.stem ("a") triggers the stem
                # fallback inside ``build_script_id_by_name``.
                name="a",
                source="local p = game.Players.LocalPlayer",
                script_type="Script",
            ),
            RbxScript(
                name="b",
                source=(
                    "local p = game.Players.LocalPlayer\n"
                    "print(p.Character)"
                ),
                script_type="LocalScript",
            ),
        ]
        scene_runtime: dict[str, object] = {
            "modules": {
                "g-a": {
                    # Colliding class_name, distinct stem.
                    "stem": "a", "class_name": "X",
                    "runtime_bearing": True,
                    "is_loader": True, "character_attached": False,
                },
                "g-b": {
                    "stem": "b", "class_name": "X",
                    "runtime_bearing": True,
                    "is_loader": False, "character_attached": True,
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(scripts=scripts)
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None

        # Both rows reach lifecycle_roles via the canonical helper's
        # stem fallback (collision-exclude removed them from
        # class_name keyspace; stems "a"/"b" are collision-free).
        # If the prepass still used ``build_scripts_by_class_name``,
        # both sids would silently get ``""`` script_class and the
        # ``is_loader`` / ``character_attached`` gates inside
        # ``derive_module_lifecycle_role`` would NOT fire — they'd
        # demote to ``"auto_run"`` (the default for empty script_class
        # + client domain).
        assert out["lifecycle_roles"].get("g-a") == "loader", (
            f"g-a (class_name='X' collision, stem='a' unique) must "
            f"reach the loader lifecycle role via the canonical "
            f"stem-fallback join; got "
            f"{out['lifecycle_roles'].get('g-a')!r}"
        )
        assert out["lifecycle_roles"].get("g-b") == "character_attached", (
            f"g-b (class_name='X' collision, stem='b' unique) must "
            f"reach the character_attached lifecycle role via the "
            f"canonical stem-fallback join; got "
            f"{out['lifecycle_roles'].get('g-b')!r}"
        )

        # Routing parity check: ``script_id_by_name`` (the routing
        # path's join) ALSO resolved both scripts via the stem
        # fallback. Pin the contract that the prepass and routing
        # paths share ONE source of truth.
        assert out["script_id_by_name"].get("a") == "g-a"
        assert out["script_id_by_name"].get("b") == "g-b"

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
                "ClientA", _require("Helper"),
                parent_path=STARTER_PLAYER_SCRIPTS,
                script_type="LocalScript",
            ),
            _mk_script("Helper", "return {}", parent_path=SERVER_STORAGE),
        ]
        classify_scene_runtime_domains(artifact, scripts)
        helper_row = artifact["modules"]["g-helper"]
        assert helper_row["container"] == REPLICATED_STORAGE
        assert helper_row["module_path"] == "ReplicatedStorage.Helper"
        # Phase 2a slice 10: the parallel planner-row audit signal
        # ``domain_signals["reachability_forced_container"]`` was
        # retired alongside the topology consumer switch to
        # ``reachability_requirements[sid]``. The hoist observable is
        # still pinned by the container + module_path triple-write
        # above (invariant 10). The raw analysis fact lives on
        # ``TopologyInputs.reachability_requirements`` -- exercised
        # by the orchestrator end-to-end via the topology consumer
        # tests in ``test_scene_runtime_topology.py``.
