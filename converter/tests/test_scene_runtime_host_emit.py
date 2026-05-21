"""PR4: scene-runtime host emit + plan encoder + cross-domain report.

Python-side coverage for PR4's conversion-time integration:

  * ``generate_scene_runtime_plan_module`` encodes the planner artifact
    as a Luau-literal return that parses.
  * ``generate_scene_runtime_client_entrypoint`` /
    ``generate_scene_runtime_server_entrypoint`` produce shippable
    LocalScript / Script with the right parent paths.
  * ``compute_cross_domain_edges`` enumerates client<->server peer-
    component references; intra-domain refs do NOT appear.
  * ``render_cross_domain_report`` produces deterministic markdown.

Luau-harness behavioral coverage for the host runtime lives in
``test_scene_runtime_host_behavior.py`` (driven via the standalone
``luau`` interpreter when present).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import (  # noqa: E402
    _plan_to_luau,
    generate_scene_runtime_client_entrypoint,
    generate_scene_runtime_plan_module,
    generate_scene_runtime_server_entrypoint,
    render_cross_domain_report,
)
from converter.scene_runtime_domain import (  # noqa: E402
    compute_cross_domain_edges,
)


def _luau_available() -> bool:
    return shutil.which("luau") is not None


# ---------------------------------------------------------------------------
# _plan_to_luau encoder
# ---------------------------------------------------------------------------

class TestPlanToLuauEncoder:

    def test_primitives(self):
        assert _plan_to_luau(True) == "true"
        assert _plan_to_luau(False) == "false"
        assert _plan_to_luau(None) == "nil"
        assert _plan_to_luau(42) == "42"
        assert _plan_to_luau("hi") == "\"hi\""

    def test_string_escapes_double_quotes_and_newlines(self):
        out = _plan_to_luau("a\"b\nc")
        assert out == "\"a\\\"b\\nc\""

    def test_empty_collections(self):
        assert _plan_to_luau({}) == "{}"
        assert _plan_to_luau([]) == "{}"

    def test_dict_with_identifier_keys_unquoted(self):
        out = _plan_to_luau({"stem": "Foo", "domain": "client"})
        assert "stem = \"Foo\"" in out
        assert "domain = \"client\"" in out

    def test_dict_with_non_identifier_key_bracket_syntax(self):
        # Keys that contain ``:`` (e.g. scene_runtime instance ids
        # like ``"Scenes/A.unity:42"``) need bracket syntax.
        out = _plan_to_luau({"Scenes/A.unity:42": "x"})
        assert "[\"Scenes/A.unity:42\"] = \"x\"" in out

    def test_reserved_keyword_keys_bracket_syntax(self):
        # ``end`` is a Luau keyword; using it as a bare key would
        # parse as the statement terminator.
        out = _plan_to_luau({"end": 1, "function": 2})
        assert "[\"end\"]" in out
        assert "[\"function\"]" in out

    def test_unicode_identifier_keys_use_bracket_syntax(self):
        """R4-P2.2: Python's ``str.isidentifier()`` returns True for
        PEP 3131 Unicode identifiers (``"变量".isidentifier() == True``)
        but Luau's lexer rejects non-ASCII bare identifiers. The
        encoder must bracket-quote such keys so the embedded
        ``SceneRuntimePlan`` ModuleScript parses on the live host.
        """
        out = _plan_to_luau({"变量": 1, "naïve": 2, "ascii_ok": 3})
        # Non-ASCII keys must be bracket-quoted (string-keyed entry).
        assert "[\"变量\"]" in out, out
        assert "[\"naïve\"]" in out, out
        # An ASCII identifier still uses bare-key form.
        assert "ascii_ok = 3" in out, out
        # The bare-key form for the Unicode keys must NOT appear.
        assert "变量 = " not in out, out
        assert "naïve = " not in out, out

    @pytest.mark.skipif(not _luau_available(),
                        reason="luau interpreter not installed")
    def test_unicode_key_emit_parses_with_luau(self):
        """R4-P2.2: the encoded plan with Unicode keys must round-trip
        through standalone luau (the bracket-quoted form is required for
        the lexer to accept it)."""
        encoded = "return " + _plan_to_luau({"变量": "v", "naïve": 7})
        wrapper = (f"local p = (function() {encoded} end)(); "
                   "assert(type(p) == 'table'); "
                   "assert(p[\"变量\"] == 'v'); "
                   "assert(p[\"naïve\"] == 7); "
                   "print('ok')")
        with tempfile.NamedTemporaryFile(
            suffix=".luau", mode="w", delete=False,
        ) as fw:
            fw.write(wrapper)
            wpath = fw.name
        try:
            result = subprocess.run(
                ["luau", wpath], capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, (
                f"luau parse failed: stderr={result.stderr!r}, "
                f"stdout={result.stdout!r}"
            )
            assert "ok" in result.stdout
        finally:
            Path(wpath).unlink(missing_ok=True)

    def test_nested_dict_and_list(self):
        plan = {
            "modules": {
                "guid-1": {
                    "stem": "Foo",
                    "runtime_bearing": True,
                },
            },
            "scenes": {
                "Scenes/A.unity": {
                    "instances": [
                        {"instance_id": "Scenes/A.unity:1", "active": True},
                    ],
                    "lifecycle_order": ["Scenes/A.unity:1"],
                },
            },
        }
        out = _plan_to_luau(plan)
        # The presence of all the leaves is the test; parse correctness
        # is asserted below in TestPlanLuauParseable.
        for needle in ("modules", "Foo", "runtime_bearing = true",
                       "Scenes/A.unity:1", "lifecycle_order"):
            assert needle in out

    @pytest.mark.skipif(not _luau_available(),
                        reason="luau interpreter not installed")
    def test_encoded_plan_parses_with_luau(self):
        plan = {
            "modules": {
                "g1": {"stem": "Foo", "runtime_bearing": True,
                       "domain": "client", "module_path": "ReplicatedStorage.Foo"},
                "g2": {"stem": "Bar", "runtime_bearing": False},
            },
            "scenes": {
                "A.unity": {
                    "instances": [{
                        "instance_id": "A.unity:1",
                        "script_id": "g1",
                        "game_object_id": "A.unity:99",
                        "active": True, "enabled": True,
                        "config": {"speed": 5.0},
                    }],
                    "references": [{
                        "from": "A.unity:1",
                        "field": "target",
                        "index": None,
                        "target_kind": "asset",
                        "target_ref": "rbxassetid://123",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["A.unity:1"],
                },
            },
            "prefabs": {},
            "domain_overrides": {},
        }
        encoded = "return " + _plan_to_luau(plan)
        with tempfile.NamedTemporaryFile(
            suffix=".luau", mode="w", delete=False,
        ) as f:
            f.write(encoded)
            path = f.name
        try:
            # Wrap with a print so luau treats the file as runnable.
            wrapper = (f"local p = (function() {encoded} end)(); "
                       "assert(type(p) == 'table'); "
                       "assert(p.modules.g1.stem == 'Foo'); "
                       "assert(p.scenes['A.unity'].lifecycle_order[1] == 'A.unity:1'); "
                       "print('ok')")
            with tempfile.NamedTemporaryFile(
                suffix=".luau", mode="w", delete=False,
            ) as fw:
                fw.write(wrapper)
                wpath = fw.name
            result = subprocess.run(
                ["luau", wpath], capture_output=True, text=True, timeout=10,
            )
            assert result.returncode == 0, (
                f"luau exec failed: stderr={result.stderr!r}, "
                f"stdout={result.stdout!r}"
            )
            assert "ok" in result.stdout
        finally:
            Path(path).unlink(missing_ok=True)
            try:
                Path(wpath).unlink(missing_ok=True)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Entrypoint generators
# ---------------------------------------------------------------------------

class TestEntrypointGenerators:

    def test_client_entrypoint_parent_path_is_starter_player_scripts(self):
        script = generate_scene_runtime_client_entrypoint()
        assert script.name == "SceneRuntimeClient"
        assert script.script_type == "LocalScript"
        assert script.parent_path == "StarterPlayer.StarterPlayerScripts"
        # Sanity-check the source mentions the engine + plan.
        assert "SceneRuntime" in script.source
        assert "SceneRuntimePlan" in script.source
        assert "start(\"client\")" in script.source

    def test_server_entrypoint_parent_path_is_server_script_service(self):
        script = generate_scene_runtime_server_entrypoint()
        assert script.name == "SceneRuntimeServer"
        assert script.script_type == "Script"
        assert script.parent_path == "ServerScriptService"
        assert "start(\"server\")" in script.source

    def test_plan_module_parent_path_replicated_storage(self):
        script = generate_scene_runtime_plan_module({
            "modules": {}, "scenes": {}, "prefabs": {},
        })
        assert script.name == "SceneRuntimePlan"
        assert script.script_type == "ModuleScript"
        assert script.parent_path == "ReplicatedStorage"
        # Even an empty plan emits ``return {...}`` so the
        # ``require`` doesn't fail with a non-table return value.
        assert script.source.strip().startswith("--")
        assert "return" in script.source

    def test_entrypoints_split_module_path_on_dot_not_slash(self):
        """R2-P1.1: ``module_path`` is the dotted DataModel path. Both
        client and server entrypoints must split on ``"."`` so they can
        walk ``game:FindFirstChild(...)`` down the chain. Pre-fix the
        entrypoints used ``string.split(modulePath, "/")`` which silently
        accepted the planner's old ``"scripts/Foo.luau"`` shape but
        couldn't actually resolve a ModuleScript in production.
        """
        for gen in (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_server_entrypoint,
        ):
            src = gen().source
            assert 'string.split(modulePath, ".")' in src, (
                f"{gen.__name__} must split modulePath on '.'; source: {src}"
            )
            assert 'string.split(modulePath, "/")' not in src, (
                f"{gen.__name__} must NOT split modulePath on '/'; that "
                f"shape was the pre-R2 on-disk path the host couldn't "
                f"resolve in production"
            )

    def test_entrypoints_resolve_prefab_via_template_name_map(self):
        """R2-P1.2: prefab templates live under
        ``ReplicatedStorage.Templates`` keyed by bare prefab name.
        Entrypoints must look up the bare name via
        ``plan.prefabs[prefab_id].template_name`` rather than feeding
        the stable ``prefab_id`` (which includes a GUID + path) directly
        to ``Templates:FindFirstChild(...)``.
        """
        for gen in (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_server_entrypoint,
        ):
            src = gen().source
            assert "Templates" in src, (
                f"{gen.__name__} must read from ReplicatedStorage.Templates"
            )
            assert "ScenePrefabs" not in src, (
                f"{gen.__name__} must NOT reference the legacy "
                f"ScenePrefabs folder (typo from PR4 round 1)"
            )
            assert "template_name" in src, (
                f"{gen.__name__} must resolve via the plan's "
                f"template_name map (R2-P1.2)"
            )


# ---------------------------------------------------------------------------
# compute_cross_domain_edges
# ---------------------------------------------------------------------------

def _make_artifact_with_edge(
    src_domain: str, tgt_domain: str,
) -> dict:
    return {
        "modules": {
            "src": {"stem": "Src", "runtime_bearing": True,
                    "domain": src_domain, "module_path": "ReplicatedStorage.Src"},
            "tgt": {"stem": "Tgt", "runtime_bearing": True,
                    "domain": tgt_domain, "module_path": "ReplicatedStorage.Tgt"},
        },
        "scenes": {
            "A.unity": {
                "instances": [
                    {"instance_id": "A.unity:1", "script_id": "src",
                     "game_object_id": "A.unity:1", "active": True,
                     "enabled": True, "config": {}},
                    {"instance_id": "A.unity:2", "script_id": "tgt",
                     "game_object_id": "A.unity:2", "active": True,
                     "enabled": True, "config": {}},
                ],
                "references": [{
                    "from": "A.unity:1",
                    "field": "peer",
                    "index": None,
                    "target_kind": "component",
                    "target_ref": "A.unity:2",
                    "target_is_ui": False,
                }],
                "lifecycle_order": ["A.unity:1", "A.unity:2"],
            },
        },
        "prefabs": {},
        "domain_overrides": {},
    }


class TestComputeCrossDomainEdges:

    def test_cross_domain_edge_listed(self):
        plan = _make_artifact_with_edge("client", "server")
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert len(edges) == 1
        edge = edges[0]
        assert edge["from_script"] == "src"
        assert edge["to_script"] == "tgt"
        assert edge["from_domain"] == "client"
        assert edge["to_domain"] == "server"
        assert edge["field"] == "peer"
        assert edge["owner_kind"] == "scene"
        assert edge["owner_ref"] == "A.unity"

    def test_same_domain_no_edge(self):
        plan = _make_artifact_with_edge("client", "client")
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert edges == []

    def test_legacy_domain_skipped(self):
        # ``legacy`` is the fail-closed verdict from PR3b; the host
        # never instantiates these, so a cross-edge to one is not
        # actionable -- omit it from the report.
        plan = _make_artifact_with_edge("client", "legacy")
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert edges == []

    def test_non_component_target_kind_skipped(self):
        plan = _make_artifact_with_edge("client", "server")
        plan["scenes"]["A.unity"]["references"][0]["target_kind"] = "asset"
        plan["scenes"]["A.unity"]["references"][0]["target_ref"] = (
            "rbxassetid://1"
        )
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert edges == []

    def test_prefab_owner_kind(self):
        plan = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client", "module_path": "ReplicatedStorage.Src"},
                "tgt": {"stem": "Tgt", "runtime_bearing": True,
                        "domain": "server", "module_path": "ReplicatedStorage.Tgt"},
            },
            "scenes": {},
            "prefabs": {
                "guid:Prefabs/Foo.prefab": {
                    "name": "Foo",
                    "instances": [
                        {"instance_id": "p:1", "script_id": "src",
                         "game_object_id": "p:1", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "p:2", "script_id": "tgt",
                         "game_object_id": "p:2", "active": True,
                         "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "p:1", "field": "peer", "index": None,
                        "target_kind": "component",
                        "target_ref": "p:2",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["p:1", "p:2"],
                },
            },
            "domain_overrides": {},
        }
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert len(edges) == 1
        assert edges[0]["owner_kind"] == "prefab"
        assert edges[0]["owner_ref"] == "guid:Prefabs/Foo.prefab"


# ---------------------------------------------------------------------------
# render_cross_domain_report
# ---------------------------------------------------------------------------

class TestRenderCrossDomainReport:

    def test_empty_returns_empty_string(self):
        assert render_cross_domain_report([]) == ""

    def test_single_edge_renders_table_row(self):
        out = render_cross_domain_report([{
            "from_script": "Foo",
            "to_script": "Bar",
            "field": "target",
            "from_domain": "client",
            "to_domain": "server",
        }])
        assert "## Cross-domain references" in out
        assert "| Foo | client | target | -> | Bar | server |" in out
        assert "Total edges: 1" in out

    def test_multiple_edges_listed_in_order(self):
        edges = [
            {"from_script": "A", "to_script": "B", "field": "x",
             "from_domain": "client", "to_domain": "server"},
            {"from_script": "C", "to_script": "D", "field": "y",
             "from_domain": "server", "to_domain": "client"},
        ]
        out = render_cross_domain_report(edges)
        # Row order matches input order.
        a_pos = out.index("| A |")
        c_pos = out.index("| C |")
        assert a_pos < c_pos


# ---------------------------------------------------------------------------
# R4-P1.1 (absorbed PR5): per-place scene scoping for the embedded plan.
#
# The multi-scene pipeline writes one ``.rbxlx`` per scene; the embedded
# ``SceneRuntimePlan`` must carry only the active scene's block so the
# host's ``workspaceFind`` lookup doesn't try to bind components for
# sister places' scenes (which would resolve to nil + log warnings on
# every boot).
# ---------------------------------------------------------------------------

class TestPlanModulePerSceneScoping:
    """R4-P1.1: ``generate_scene_runtime_plan_module(..., scene_namespace=k)``
    embeds only ``scenes[k]`` -- modules / prefabs / domain_overrides /
    scriptable_objects stay project-scoped.
    """

    def _multi_scene_artifact(self) -> dict:
        return {
            "modules": {
                "a_sid": {"stem": "A", "runtime_bearing": True,
                          "domain": "client", "module_path": "RS.A"},
                "b_sid": {"stem": "B", "runtime_bearing": True,
                          "domain": "server", "module_path": "RS.B"},
            },
            "scenes": {
                "Assets/Scenes/Menu.unity": {
                    "instances": [{
                        "instance_id": "Assets/Scenes/Menu.unity:1",
                        "script_id": "a_sid",
                        "game_object_id": "Assets/Scenes/Menu.unity:1",
                        "active": True, "enabled": True, "config": {},
                    }],
                    "references": [],
                    "lifecycle_order": ["Assets/Scenes/Menu.unity:1"],
                },
                "Assets/Scenes/Gameplay.unity": {
                    "instances": [{
                        "instance_id": "Assets/Scenes/Gameplay.unity:2",
                        "script_id": "b_sid",
                        "game_object_id": "Assets/Scenes/Gameplay.unity:2",
                        "active": True, "enabled": True, "config": {},
                    }],
                    "references": [],
                    "lifecycle_order": ["Assets/Scenes/Gameplay.unity:2"],
                },
            },
            "prefabs": {},
            "domain_overrides": {},
        }

    def test_scene_namespace_filter_drops_sister_scenes(self):
        artifact = self._multi_scene_artifact()
        script = generate_scene_runtime_plan_module(
            artifact, scene_namespace="Assets/Scenes/Menu.unity",
        )
        # The Menu place's plan must carry the Menu scene block only.
        assert "Menu.unity:1" in script.source
        # Sister scene blocks must NOT appear -- pre-R4-P1.1 the entire
        # project plan was embedded into every place.
        assert "Gameplay.unity:2" not in script.source

    def test_modules_table_not_filtered_by_scene_namespace(self):
        """Modules are project-scoped (keyed by script_id). The host
        needs every module's domain/container regardless of which
        scene a particular instance lives in -- a prefab spawned at
        runtime from scene A may carry a component whose module is
        only structurally attached to scene B's hierarchy.
        """
        artifact = self._multi_scene_artifact()
        script = generate_scene_runtime_plan_module(
            artifact, scene_namespace="Assets/Scenes/Menu.unity",
        )
        # Both module rows survive the scene filter.
        assert "a_sid" in script.source
        assert "b_sid" in script.source

    def test_unknown_namespace_emits_empty_scenes(self):
        """A namespace not present in the planner artifact emits an
        empty ``scenes`` table; the host still loads and the
        runtime-bearing modules' instances simply never bind. This
        is the safe failure mode -- better than embedding a sister
        scene the host would try to wire.
        """
        artifact = self._multi_scene_artifact()
        script = generate_scene_runtime_plan_module(
            artifact, scene_namespace="Assets/Scenes/Missing.unity",
        )
        assert "Menu.unity:1" not in script.source
        assert "Gameplay.unity:2" not in script.source
        # Modules survive even when scenes scopes to nothing.
        assert "a_sid" in script.source

    def test_default_no_namespace_embeds_all_scenes(self):
        """Backwards compatibility: callers that don't pass
        ``scene_namespace`` get the pre-R4-P1.1 behaviour (full
        ``scenes`` table embedded). Single-scene runs and tests rely
        on this.
        """
        artifact = self._multi_scene_artifact()
        script = generate_scene_runtime_plan_module(artifact)
        assert "Menu.unity:1" in script.source
        assert "Gameplay.unity:2" in script.source

    def test_prefabs_and_overrides_not_filtered(self):
        """``prefabs`` and ``domain_overrides`` are project-scoped --
        the host can spawn a template prefab from any place.
        """
        artifact = self._multi_scene_artifact()
        artifact["prefabs"] = {
            "guid_xyz:Assets/Prefabs/Enemy.prefab": {
                "template_name": "Enemy",
                "instances": [], "references": [],
                "lifecycle_order": [],
            },
        }
        artifact["domain_overrides"] = {"a_sid": "client"}
        script = generate_scene_runtime_plan_module(
            artifact, scene_namespace="Assets/Scenes/Menu.unity",
        )
        assert "Enemy" in script.source
        assert "domain_overrides" in script.source


class TestComputeSceneNamespace:
    """Cross-module helper used by ``_subphase_inject_scene_runtime``
    to pick the per-place scene key. Stays in sync with the planner's
    internal key derivation.
    """

    def test_project_relative_path_returned(self, tmp_path):
        from converter.scene_runtime_planner import compute_scene_namespace
        scene = tmp_path / "Assets" / "Scenes" / "Main.unity"
        scene.parent.mkdir(parents=True)
        scene.write_text("", encoding="utf-8")
        ns = compute_scene_namespace(scene, tmp_path)
        assert ns == "Assets/Scenes/Main.unity"

    def test_absolute_fallback_when_outside_root(self, tmp_path):
        from converter.scene_runtime_planner import compute_scene_namespace
        outside = tmp_path / "Sibling.unity"
        outside.write_text("", encoding="utf-8")
        other_root = tmp_path / "OtherRoot"
        other_root.mkdir()
        ns = compute_scene_namespace(outside, other_root)
        # Outside the project root => absolute path is returned.
        assert ns.endswith("/Sibling.unity")

    def test_none_root_returns_absolute_posix(self, tmp_path):
        from converter.scene_runtime_planner import compute_scene_namespace
        scene = tmp_path / "Solo.unity"
        scene.write_text("", encoding="utf-8")
        ns = compute_scene_namespace(scene, None)
        assert ns.endswith("/Solo.unity")
        # No backslashes; forward-slashed for JSON round-trip parity.
        assert "\\" not in ns
