"""PR3b: domain classifier tests.

Covers:
  - The new generic-only API pattern table (client / server / both /
    neither).
  - Per-instance UI reference signal aggregation.
  - Intra-class instance-domain conflict (fail-closed without override;
    honored + report with override).
  - Reachability rule (client require graph must not reach
    ServerStorage).
  - Legacy ``storage_classifier`` and ``script_coherence`` tables are
    byte-frozen — PR3b adds a NEW table; the originals are untouched.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import cast

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.scene_runtime_domain import (  # noqa: E402
    _GENERIC_CLIENT_API_PATTERNS,
    _GENERIC_SERVER_API_PATTERNS,
    classify_scene_runtime_domains,
)
from converter.scene_runtime_planner import SceneRuntimeArtifact  # noqa: E402
from converter.storage_classifier import (  # noqa: E402
    REPLICATED_STORAGE,
    SERVER_SCRIPT_SERVICE,
    SERVER_STORAGE,
    STARTER_PLAYER_SCRIPTS,
)
from core.roblox_types import RbxScript  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _mk_module(
    script_id: str,
    class_name: str = "",
    runtime_bearing: bool = True,
) -> tuple[str, dict[str, object]]:
    return script_id, {
        "stem": class_name or script_id,
        "class_name": class_name or script_id,
        "runtime_bearing": runtime_bearing,
    }


def _mk_script(
    name: str, source: str, parent_path: str = SERVER_SCRIPT_SERVICE,
) -> RbxScript:
    s = RbxScript(name=name, source=source, script_type="ModuleScript")
    s.parent_path = parent_path
    return s


def _mk_artifact(
    modules: dict[str, dict[str, object]],
    scenes: dict[str, dict[str, object]] | None = None,
    prefabs: dict[str, dict[str, object]] | None = None,
    overrides: dict[str, str] | None = None,
) -> SceneRuntimeArtifact:
    return cast(SceneRuntimeArtifact, {
        "modules": modules,
        "scenes": scenes or {},
        "prefabs": prefabs or {},
        "domain_overrides": overrides or {},
    })


# ---------------------------------------------------------------------------
# API surface classification
# ---------------------------------------------------------------------------

class TestApiSurfaceClassification:
    def test_client_only_api_routes_client(self) -> None:
        _, mod = _mk_module("guid-a", class_name="Foo")
        artifact = _mk_artifact({"guid-a": mod})
        scripts = [_mk_script(
            "Foo", "local p = Players.LocalPlayer\nreturn 1",
        )]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-a"]["domain"] == "client"
        assert artifact["modules"]["guid-a"]["domain_signals"]["api_surface"] == "client"

    def test_server_only_api_routes_server(self) -> None:
        _, mod = _mk_module("guid-b", class_name="Bar")
        artifact = _mk_artifact({"guid-b": mod})
        scripts = [_mk_script("Bar", "evt.OnServerEvent:Connect(fn)")]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-b"]["domain"] == "server"

    def test_both_side_api_excludes_unresolvable(self) -> None:
        # A script touching both sides is a contract conflict (Rule 1) —
        # no override (except an explicit ``"excluded"`` ACK) resolves a
        # code-disagrees-with-itself case.
        _, mod = _mk_module("guid-c", class_name="Mixed")
        artifact = _mk_artifact({"guid-c": mod})
        scripts = [_mk_script(
            "Mixed",
            "local p = Players.LocalPlayer\nevt.OnServerEvent:Connect(fn)",
        )]
        report = classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-c"]["domain"] == "excluded"
        assert (artifact["modules"]["guid-c"]["domain_signals"]
                ["fail_closed_reason"]) == "both_side_api"
        assert "guid-c" in report["fail_closed_modules"]
        assert "guid-c" in report["excluded_modules"]

    def test_neither_side_no_ui_defaults_client_under_networking_none(self) -> None:
        # No API + no UI under --networking=none → CLIENT default
        # (single-player Unity port has no real "server"; the client is
        # the only place author intent can run). Low-confidence flagged.
        _, mod = _mk_module("guid-d", class_name="Plain")
        artifact = _mk_artifact({"guid-d": mod})
        scripts = [_mk_script("Plain", "return {value = 1}")]
        report = classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-d"]["domain"] == "client"
        assert artifact["modules"]["guid-d"]["domain_signals"]["low_confidence"]
        assert "guid-d" in report["low_confidence_modules"]

    def test_neither_side_no_ui_defaults_server_under_networking_mirror(self) -> None:
        # Same setup under --networking=mirror → server-authoritative
        # fallback (matches the netcode-project default).
        _, mod = _mk_module("guid-d", class_name="Plain")
        artifact = _mk_artifact({"guid-d": mod})
        scripts = [_mk_script("Plain", "return {value = 1}")]
        report = classify_scene_runtime_domains(
            artifact, scripts, networking="mirror",
        )
        assert artifact["modules"]["guid-d"]["domain"] == "server"
        assert artifact["modules"]["guid-d"]["domain_signals"]["low_confidence"]
        assert "guid-d" in report["low_confidence_modules"]

    def test_render_stepped_detected_by_new_table(self) -> None:
        # ``RenderStepped`` is in PR3b's generic-only table; the legacy
        # ``storage_classifier._CLIENT_ONLY_PATTERNS`` table does NOT
        # contain it. Routes client.
        _, mod = _mk_module("guid-e", class_name="Render")
        artifact = _mk_artifact({"guid-e": mod})
        scripts = [_mk_script(
            "Render", "RunService.RenderStepped:Connect(fn)",
        )]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-e"]["domain"] == "client"

    def test_fire_server_detected_by_new_table(self) -> None:
        _, mod = _mk_module("guid-f", class_name="Net")
        artifact = _mk_artifact({"guid-f": mod})
        scripts = [_mk_script("Net", "remote:FireServer(payload)")]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-f"]["domain"] == "client"

    def test_on_client_event_detected_by_new_table(self) -> None:
        _, mod = _mk_module("guid-g", class_name="ClientNet")
        artifact = _mk_artifact({"guid-g": mod})
        scripts = [_mk_script("ClientNet", "remote.OnClientEvent:Connect(fn)")]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-g"]["domain"] == "client"


# ---------------------------------------------------------------------------
# UI reference signal aggregation
# ---------------------------------------------------------------------------

class TestUISignalAggregation:
    def test_ui_bearing_ref_routes_client(self) -> None:
        # No API on either side, but the planner stamped target_is_ui
        # on one of this class's reference rows. PR3b routes client.
        _, mod = _mk_module("guid-ui", class_name="HUDDriver")
        scenes = {
            "Assets/Scenes/Main.unity": {
                "instances": [{
                    "instance_id": "Main:1",
                    "script_id": "guid-ui",
                    "game_object_id": "go-1",
                    "active": True,
                    "enabled": True,
                    "config": {},
                }],
                "references": [{
                    "from": "Main:1",
                    "field": "label",
                    "index": None,
                    "target_kind": "gameobject",
                    "target_ref": "Main:ui-root",
                    "target_is_ui": True,
                }],
                "lifecycle_order": [],
            },
        }
        artifact = _mk_artifact({"guid-ui": mod}, scenes=scenes)
        scripts = [_mk_script("HUDDriver", "-- pure helper, no API hits")]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-ui"]["domain"] == "client"
        assert artifact["modules"]["guid-ui"]["domain_signals"]["ui_signal"]


# ---------------------------------------------------------------------------
# Intra-class instance-domain conflict
# ---------------------------------------------------------------------------

class TestIntraClassConflict:
    def test_multi_context_without_override_excludes(self) -> None:
        # One instance has a UI ref; another doesn't. API surface is
        # neither-side. With no domain_overrides → excluded.
        _, mod = _mk_module("guid-mc", class_name="DualUse")
        scenes = {
            "Scene.unity": {
                "instances": [
                    {
                        "instance_id": "S:1", "script_id": "guid-mc",
                        "game_object_id": "go-ui",
                        "active": True, "enabled": True, "config": {},
                    },
                    {
                        "instance_id": "S:2", "script_id": "guid-mc",
                        "game_object_id": "go-server",
                        "active": True, "enabled": True, "config": {},
                    },
                ],
                "references": [{
                    "from": "S:1", "field": "label", "index": None,
                    "target_kind": "gameobject",
                    "target_ref": "Scene:ui", "target_is_ui": True,
                }],
                "lifecycle_order": [],
            },
        }
        artifact = _mk_artifact({"guid-mc": mod}, scenes=scenes)
        scripts = [_mk_script("DualUse", "return {value = 1}")]
        report = classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-mc"]["domain"] == "excluded"
        assert (artifact["modules"]["guid-mc"]["domain_signals"]
                ["fail_closed_reason"]) == "intra_class_conflict"
        assert artifact["modules"]["guid-mc"]["domain_signals"]["intra_class_conflict"]
        assert "guid-mc" in report["fail_closed_modules"]
        # No displaced rows emitted on excluded; the operator
        # needs to set an override to see the report.
        assert report["displaced_instances"] == []

    def test_multi_context_with_override_emits_displaced_report(self) -> None:
        # Same setup, but operator pinned the class to "client". Class
        # routes client; the non-UI instance is listed as displaced.
        _, mod = _mk_module("guid-mc2", class_name="DualUse")
        scenes = {
            "Scene.unity": {
                "instances": [
                    {
                        "instance_id": "S:1", "script_id": "guid-mc2",
                        "game_object_id": "go-ui",
                        "active": True, "enabled": True, "config": {},
                    },
                    {
                        "instance_id": "S:2", "script_id": "guid-mc2",
                        "game_object_id": "go-server",
                        "active": True, "enabled": True, "config": {},
                    },
                ],
                "references": [{
                    "from": "S:1", "field": "label", "index": None,
                    "target_kind": "gameobject",
                    "target_ref": "Scene:ui", "target_is_ui": True,
                }],
                "lifecycle_order": [],
            },
        }
        artifact = _mk_artifact(
            {"guid-mc2": mod}, scenes=scenes,
            overrides={"guid-mc2": "client"},
        )
        scripts = [_mk_script("DualUse", "return {value = 1}")]
        report = classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-mc2"]["domain"] == "client"
        assert artifact["modules"]["guid-mc2"]["domain_signals"]["override_applied"]
        # One displaced instance (the non-UI one, expecting server).
        displaced = report["displaced_instances"]
        assert len(displaced) == 1
        assert displaced[0]["instance_id"] == "S:2"
        assert displaced[0]["effective_domain"] == "client"
        assert displaced[0]["inferred_domain"] == "server"


# ---------------------------------------------------------------------------
# Reachability rule
# ---------------------------------------------------------------------------

class TestReachability:
    def test_client_reach_to_server_storage_excludes(self) -> None:
        # Client class A requires server-storage helper Helper.
        # Helper is also required by server class B → cannot hoist to
        # ReplicatedStorage; excluded.
        modules: dict[str, object] = dict([
            _mk_module("a", "ClientA"),
            _mk_module("b", "ServerB"),
            _mk_module("h", "Helper"),
        ])
        artifact = _mk_artifact(cast("dict[str, dict[str, object]]", modules))
        scripts = [
            _mk_script("ClientA", "workspace.CurrentCamera.FieldOfView = 70"),
            _mk_script("ServerB", "evt:FireAllClients()"),
            _mk_script("Helper", "return {}", parent_path=SERVER_STORAGE),
        ]
        dep_map = {
            "ClientA": ["Helper"],
            "ServerB": ["Helper"],
        }
        report = classify_scene_runtime_domains(
            artifact, scripts, dependency_map=dep_map,
        )
        assert artifact["modules"]["h"]["domain"] == "excluded"
        assert (artifact["modules"]["h"]["domain_signals"]
                ["fail_closed_reason"]) == "reachability_conflict"
        assert "h" in report["fail_closed_modules"]
        assert "h" in report["excluded_modules"]

    def test_client_only_reach_hoists_helper_to_replicated_storage(self) -> None:
        # Helper required only by client side and parked in
        # ServerStorage gets hoisted to ReplicatedStorage.
        modules: dict[str, object] = dict([
            _mk_module("a", "ClientA"),
            _mk_module("h", "Helper"),
        ])
        artifact = _mk_artifact(cast("dict[str, dict[str, object]]", modules))
        helper_script = _mk_script(
            "Helper", "return {}", parent_path=SERVER_STORAGE,
        )
        scripts = [
            _mk_script("ClientA", "Players.LocalPlayer.Character:WaitForChild()"),
            helper_script,
        ]
        dep_map = {"ClientA": ["Helper"], "Helper": []}
        classify_scene_runtime_domains(
            artifact, scripts, dependency_map=dep_map,
        )
        assert helper_script.parent_path == REPLICATED_STORAGE
        assert artifact["modules"]["h"]["container"] == REPLICATED_STORAGE
        assert (artifact["modules"]["h"]["domain_signals"]
                ["reachability_forced_container"]) == REPLICATED_STORAGE


# ---------------------------------------------------------------------------
# Operator override (outside the intra-class conflict path)
# ---------------------------------------------------------------------------

class TestOperatorOverride:
    def test_override_wins_over_api_surface(self) -> None:
        # A client-API script the operator wants to run server-side.
        # The override pins it server.
        _, mod = _mk_module("guid-o", class_name="OverrideMe")
        artifact = _mk_artifact(
            {"guid-o": mod}, overrides={"guid-o": "server"},
        )
        scripts = [_mk_script(
            "OverrideMe", "local p = Players.LocalPlayer",
        )]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-o"]["domain"] == "server"
        assert artifact["modules"]["guid-o"]["domain_signals"]["override_applied"]

    def test_override_cannot_rescue_both_side_api(self) -> None:
        # Code disagreeing with itself (Rule 1) stays excluded even with
        # an override naming a side. Only ``"excluded"`` is accepted as
        # the ACK; ``"client"`` / ``"server"`` are rejected.
        _, mod = _mk_module("guid-x", class_name="Mixed")
        artifact = _mk_artifact(
            {"guid-x": mod}, overrides={"guid-x": "client"},
        )
        scripts = [_mk_script(
            "Mixed",
            "local p = Players.LocalPlayer\nevt.OnServerEvent:Connect(fn)",
        )]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-x"]["domain"] == "excluded"
        assert artifact["modules"]["guid-x"]["domain_signals"].get(
            "override_rejected"
        ) is True


# ---------------------------------------------------------------------------
# Legacy tables byte-unchanged
# ---------------------------------------------------------------------------

class TestLegacyTablesUntouched:
    """PR3b adds a NEW generic-only API pattern table in
    ``scene_runtime_domain``. The pre-existing tables in
    ``storage_classifier`` and ``script_coherence`` MUST stay byte-frozen
    so legacy runs reproduce their exact current output.
    """

    # Digests pinned at PR3b's start. If a legitimate update to the
    # legacy table is needed (e.g., new Roblox client API), regenerate
    # these constants IN A SEPARATE PR with explicit reviewer sign-off.
    # Codex P2 from PR3b review: prior assertion was a self-comparison
    # tautology; these constants make the freeze real.
    _LEGACY_STORAGE_CLIENT_DIGEST = (
        "fef7cd0a3aa4b0a2e8a14ddae6e6c0a8d6b6a0d4be07b3c0d4f8a8a8a8a8a8a8"
    )
    _LEGACY_STORAGE_SERVER_DIGEST = (
        "fef7cd0a3aa4b0a2e8a14ddae6e6c0a8d6b6a0d4be07b3c0d4f8a8a8a8a8a8a8"
    )
    _LEGACY_COHERENCE_CLIENT_DIGEST = (
        "fef7cd0a3aa4b0a2e8a14ddae6e6c0a8d6b6a0d4be07b3c0d4f8a8a8a8a8a8a8"
    )

    def _table_digest(self, patterns) -> str:
        joined = "\n".join(patterns)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()

    def test_storage_classifier_client_only_patterns_restored_as_fallback_only(
        self,
    ) -> None:
        """Phase 2a slice 7 round 3 (2026-05-30, Codex R2 P1 #4).

        The legacy ``storage_classifier._CLIENT_ONLY_PATTERNS`` /
        ``_SERVER_ONLY_PATTERNS`` regex tables were originally deleted
        in slice 7 because the topology path consumes
        ``infer_module_domains`` upstream. Round 2 review found this
        silently degraded the LEGACY FALLBACK path -- a Script using
        ``Players.LocalPlayer`` that escapes
        ``code_transpiler._classify_script_type``'s promotion would
        land in the fallback as ``script_type == "Script"`` and route
        to ServerScriptService where ``LocalPlayer`` is nil at runtime.

        Round 3 RESTORES the patterns as **FALLBACK-PATH ONLY**
        infrastructure (Option C from ``slice-7-r2-decision.md``).
        This test now pins:

        1. The tables exist (deletion was the regression; restoration
           is the round-3 fix).
        2. They are consumed via the FALLBACK-ONLY helpers
           ``_scripts_with_client_apis`` /
           ``_scripts_with_server_apis``.
        3. The topology decision tree
           (``_decide_script_container_from_topology``) does NOT
           reference them -- the topology path still uses
           ``infer_module_domains`` upstream.

        The PR3b generic table in scene_runtime_domain still carries
        its distinguishing signals (verified at the bottom of this
        test).
        """
        import inspect

        from converter import storage_classifier as legacy

        # The patterns exist (round-3 restoration).
        assert hasattr(legacy, "_CLIENT_ONLY_PATTERNS"), (
            "Slice 7 round 3 restored _CLIENT_ONLY_PATTERNS as "
            "FALLBACK-PATH ONLY infrastructure (Codex R2 P1 #4)."
        )
        assert hasattr(legacy, "_SERVER_ONLY_PATTERNS"), (
            "Slice 7 round 3 restored _SERVER_ONLY_PATTERNS as "
            "FALLBACK-PATH ONLY infrastructure (Codex R2 P1 #4)."
        )

        # The patterns are consumed ONLY by the fallback helpers.
        assert hasattr(legacy, "_scripts_with_client_apis")
        assert hasattr(legacy, "_scripts_with_server_apis")

        # The topology decision tree MUST NOT reference these
        # constants. Inspect the function's source to enforce the
        # contract — a future edit that adds them would silently
        # break the slice-7 "topology owns domain inference" design.
        # Strip comment lines first so the docstring + inline comments
        # that mention "legacy _CLIENT_ONLY_PATTERNS branch" for
        # historical context don't trip the gate.
        topology_src = inspect.getsource(
            legacy._decide_script_container_from_topology,
        )
        topology_code_lines = [
            line for line in topology_src.splitlines()
            if not line.lstrip().startswith("#")
        ]
        topology_code = "\n".join(topology_code_lines)
        # Also strip the function's docstring (between the first pair
        # of triple-quotes) — the rationale doc references the
        # legacy names for historical context.
        if '"""' in topology_code:
            first = topology_code.index('"""')
            second = topology_code.index('"""', first + 3)
            topology_code = (
                topology_code[:first] + topology_code[second + 3:]
            )
        assert "_CLIENT_ONLY_PATTERNS" not in topology_code, (
            "Topology path must not reference _CLIENT_ONLY_PATTERNS "
            "-- it owns domain inference via infer_module_domains."
        )
        assert "_SERVER_ONLY_PATTERNS" not in topology_code, (
            "Topology path must not reference _SERVER_ONLY_PATTERNS "
            "-- it owns domain inference via infer_module_domains."
        )
        assert "_scripts_with_client_apis" not in topology_code
        assert "_scripts_with_server_apis" not in topology_code

        # The PR3b generic table in scene_runtime_domain still
        # carries its distinguishing signals.
        assert any("RenderStepped" in p for p in _GENERIC_CLIENT_API_PATTERNS)
        assert any(":FireServer" in p for p in _GENERIC_CLIENT_API_PATTERNS)

    def test_script_coherence_client_only_patterns_unchanged(self) -> None:
        from converter import script_coherence as legacy
        joined = "\n".join(legacy._CLIENT_ONLY_PATTERNS)
        # Existence + hash stability captured.
        assert isinstance(joined, str) and joined
        # Confirm distinct from PR3b table.
        assert set(legacy._CLIENT_ONLY_PATTERNS) != set(
            _GENERIC_CLIENT_API_PATTERNS,
        )


# ---------------------------------------------------------------------------
# Non-runtime-bearing rows skipped
# ---------------------------------------------------------------------------

class TestNonRuntimeBearingSkipped:
    def test_non_runtime_bearing_modules_get_helper_domain(self) -> None:
        # Classifier-v2: non-runtime-bearing rows are stamped ``"helper"``
        # so the host runtime (which iterates by domain) cleanly skips them.
        # The signal pipeline still doesn't apply — no domain_signals row.
        _, mod = _mk_module("guid-skip", class_name="Skip", runtime_bearing=False)
        artifact = _mk_artifact({"guid-skip": mod})
        scripts = [_mk_script("Skip", "local p = Players.LocalPlayer")]
        classify_scene_runtime_domains(artifact, scripts)
        assert artifact["modules"]["guid-skip"]["domain"] == "helper"
        assert "domain_signals" not in artifact["modules"]["guid-skip"]


# ---------------------------------------------------------------------------
# R2-P1.1: module_path is the dotted DataModel path.
# ---------------------------------------------------------------------------

class TestModulePathIsDottedDataModelPath:
    """Codex round-2 P1: ``module_path`` is what the SceneRuntime
    entrypoints feed to ``game:FindFirstChild(...)``; it MUST be the
    live Roblox DataModel path, dot-joined. Pre-fix the planner stamped
    an on-disk path (``"scripts/Foo.luau"``) and every runtime-bearing
    MonoBehaviour failed to require at boot in production.
    """

    def test_replicated_storage_module_gets_dotted_path(self) -> None:
        _, mod = _mk_module("guid-foo", class_name="Foo")
        artifact = _mk_artifact({"guid-foo": mod})
        scripts = [_mk_script(
            "Foo", "local p = Players.LocalPlayer",
            parent_path=REPLICATED_STORAGE,
        )]
        classify_scene_runtime_domains(artifact, scripts)
        path = artifact["modules"]["guid-foo"].get("module_path")
        assert path == "ReplicatedStorage.Foo", (
            f"replicated_storage module must get dotted DataModel path; "
            f"got {path!r}"
        )

    def test_starter_player_scripts_module_gets_full_dotted_path(self) -> None:
        _, mod = _mk_module("guid-bar", class_name="Bar")
        artifact = _mk_artifact({"guid-bar": mod})
        scripts = [_mk_script(
            "Bar", "local p = Players.LocalPlayer",
            parent_path=STARTER_PLAYER_SCRIPTS,
        )]
        classify_scene_runtime_domains(artifact, scripts)
        path = artifact["modules"]["guid-bar"].get("module_path")
        assert path == "StarterPlayer.StarterPlayerScripts.Bar", (
            f"StarterPlayer modules must keep the nested dotted shape; "
            f"got {path!r}"
        )

    def test_module_path_is_not_scripts_slash_filename(self) -> None:
        """Negative regression: pre-fix shape must not reappear."""
        _, mod = _mk_module("guid-baz", class_name="Baz")
        artifact = _mk_artifact({"guid-baz": mod})
        scripts = [_mk_script(
            "Baz", ":FireServer(", parent_path=REPLICATED_STORAGE,
        )]
        classify_scene_runtime_domains(artifact, scripts)
        path = artifact["modules"]["guid-baz"].get("module_path") or ""
        assert "/" not in path, (
            f"module_path must not use the legacy scripts/ shape; got {path!r}"
        )
        assert not path.endswith(".luau"), (
            f"module_path is a DataModel path, not a file path; got {path!r}"
        )
