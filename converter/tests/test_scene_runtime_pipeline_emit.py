"""PR4: pipeline ``_subphase_inject_scene_runtime`` integration tests.

Asserts the conversion-time wiring:
  * Subphase runs ONLY under ``ctx.scene_runtime_mode == "generic"``.
  * Subphase is a no-op when no runtime-bearing modules exist.
  * Under generic with runtime-bearing modules, the five scripts land
    (SceneCameraInput, SceneRuntime, SceneRuntimePlan, SceneRuntimeClient,
    SceneRuntimeServer) with correct parent paths — SceneCameraInput is
    emitted UNCONDITIONALLY (P1-1/AC13), even when no script references it,
    because the client entrypoint requires it for the player authority.
  * Re-running the subphase replaces existing copies rather than
    duplicating (idempotency for ``--phase write_output`` resumes).
  * Cross-domain edges are stamped onto ``ctx.scene_runtime`` and
    appended to ``UNCONVERTED.md``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.conversion_context import ConversionContext  # noqa: E402
from core.roblox_types import RbxPlace, RbxScript  # noqa: E402
from converter.pipeline import Pipeline  # noqa: E402


def _make_pipeline_with_ctx(
    tmp_path: Path,
    scene_runtime_mode: str,
    scene_runtime: dict,
) -> Pipeline:
    """Build a Pipeline with the minimum state ``_subphase_inject_scene_runtime``
    reads. We don't run the full Pipeline.__init__; the subphase touches
    only ``self.ctx`` and ``self.state.rbx_place``.
    """
    p = Pipeline.__new__(Pipeline)
    p.ctx = ConversionContext(unity_project_path=str(tmp_path / "project"))
    p.ctx.scene_runtime_mode = scene_runtime_mode
    p.ctx.scene_runtime = scene_runtime
    p.output_dir = tmp_path
    p.output_dir.mkdir(parents=True, exist_ok=True)

    state = MagicMock()
    state.rbx_place = RbxPlace()
    state.rbx_place.scripts = []
    # ``_write_unconverted_md`` reads these; set them to None so the
    # writer's category aggregation produces an empty section list
    # unless a test seeds something explicitly.
    state.animation_result = None
    state.transpilation_result = None
    state.material_mappings = {}
    state.rbx_place.unconverted_components = []
    p.state = state
    return p


def _runtime_bearing_plan() -> dict:
    return {
        "modules": {
            "guid-foo": {
                "stem": "Foo",
                "runtime_bearing": True,
                "domain": "client",
                "module_path": "ReplicatedStorage.Foo",
                # Phase 2a slice 2: build_topology invariant 7 requires
                # both booleans on every runtime_bearing planner row.
                "character_attached": False,
                "is_loader": False,
            },
        },
        "scenes": {},
        "prefabs": {},
        "domain_overrides": {},
    }


class TestSubphaseGating:

    def test_legacy_mode_is_noop(self, tmp_path):
        p = _make_pipeline_with_ctx(
            tmp_path, "legacy", _runtime_bearing_plan(),
        )
        p._subphase_inject_scene_runtime()
        assert p.state.rbx_place.scripts == [], (
            "legacy mode must skip the subphase entirely"
        )

    def test_generic_no_runtime_bearing_is_noop(self, tmp_path):
        plan = {
            "modules": {
                "guid-helper": {
                    "stem": "Helper",
                    "runtime_bearing": False,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan)
        p._subphase_inject_scene_runtime()
        assert p.state.rbx_place.scripts == [], (
            "generic mode with no runtime-bearing modules must skip emit"
        )

    def test_generic_with_runtime_bearing_emits_five_scripts(self, tmp_path):
        # P1-1/AC13: SceneCameraInput is emitted UNCONDITIONALLY alongside the
        # host runtime (the client entrypoint requires it for the player
        # authority), growing the emit set from 4 to 5.
        p = _make_pipeline_with_ctx(
            tmp_path, "generic", _runtime_bearing_plan(),
        )
        p._subphase_inject_scene_runtime()
        names = sorted(s.name for s in p.state.rbx_place.scripts)
        assert names == [
            "SceneCameraInput",
            "SceneRuntime",
            "SceneRuntimeClient",
            "SceneRuntimePlan",
            "SceneRuntimeServer",
        ]

    def test_scene_camera_input_emitted_with_no_camera_token(self, tmp_path):
        # AC13: a generic runtime-bearing place whose scripts NEVER reference
        # ``SceneCameraInput`` (no look-method lowered to it) STILL emits the
        # SceneCameraInput ModuleScript — so the client entrypoint's
        # unconditional ``require(WaitForChild("SceneCameraInput"))`` never
        # stalls on a never-emitted module. Pins the real pipeline gate change.
        p = _make_pipeline_with_ctx(
            tmp_path, "generic", _runtime_bearing_plan(),
        )
        # Seed a pre-existing user script that does NOT mention the camera
        # service — the only SceneCameraInput token in the place comes from
        # the entrypoint require + the unconditional emit, never the gate.
        p.state.rbx_place.scripts.append(RbxScript(
            name="UserGameplay",
            source="-- a controller with no look method to lower",
            script_type="Script",
        ))
        p._subphase_inject_scene_runtime()
        names = {s.name for s in p.state.rbx_place.scripts}
        assert "SceneCameraInput" in names, (
            "SceneCameraInput must emit unconditionally even with no "
            f"camera-token script present; got {sorted(names)}"
        )

    def test_parent_paths_set_correctly(self, tmp_path):
        p = _make_pipeline_with_ctx(
            tmp_path, "generic", _runtime_bearing_plan(),
        )
        p._subphase_inject_scene_runtime()
        by_name = {s.name: s for s in p.state.rbx_place.scripts}
        assert by_name["SceneRuntime"].parent_path == "ReplicatedStorage"
        assert by_name["SceneRuntimePlan"].parent_path == "ReplicatedStorage"
        assert by_name["SceneRuntimeClient"].parent_path == (
            "StarterPlayer.StarterPlayerScripts"
        )
        assert by_name["SceneRuntimeServer"].parent_path == (
            "ServerScriptService"
        )
        assert by_name["SceneRuntime"].script_type == "ModuleScript"
        assert by_name["SceneRuntimePlan"].script_type == "ModuleScript"
        assert by_name["SceneRuntimeClient"].script_type == "LocalScript"
        assert by_name["SceneRuntimeServer"].script_type == "Script"


class TestIdempotency:

    def test_rerun_does_not_duplicate(self, tmp_path):
        p = _make_pipeline_with_ctx(
            tmp_path, "generic", _runtime_bearing_plan(),
        )
        p._subphase_inject_scene_runtime()
        names_first = sorted(s.name for s in p.state.rbx_place.scripts)
        # Pre-seed an old "edited" version to ensure replacement, not
        # append.
        p.state.rbx_place.scripts.append(RbxScript(
            name="UnrelatedAutogen",
            source="-- another script",
            script_type="Script",
        ))
        p._subphase_inject_scene_runtime()
        names_second = sorted(s.name for s in p.state.rbx_place.scripts)
        # Unrelated script survives, SceneRuntime* are not duplicated.
        assert names_second == names_first + ["UnrelatedAutogen"]
        runtime_count = sum(
            1 for s in p.state.rbx_place.scripts if s.name == "SceneRuntime"
        )
        assert runtime_count == 1


class TestCrossDomainReport:

    def test_no_edges_no_unconverted_write(self, tmp_path):
        # Plan has runtime-bearing but no cross-domain refs -> no
        # UNCONVERTED.md should be created.
        p = _make_pipeline_with_ctx(
            tmp_path, "generic", _runtime_bearing_plan(),
        )
        p._subphase_inject_scene_runtime()
        assert not (tmp_path / "UNCONVERTED.md").exists()
        assert p.ctx.scene_runtime.get("cross_domain_edges") == []

    def test_edges_written_to_unconverted_md(self, tmp_path):
        # R5-P1 fix: ``_subphase_inject_scene_runtime`` no longer writes
        # UNCONVERTED.md directly -- it stages edges on ctx and the
        # later ``_write_unconverted_md`` produces the file (single
        # source of truth). The full pipeline calls
        # ``_write_unconverted_md`` after the subphase; tests now
        # invoke both stages.
        plan = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client", "module_path": "ReplicatedStorage.Src"},
                "tgt": {"stem": "Tgt", "runtime_bearing": True,
                        "domain": "server", "module_path": "ReplicatedStorage.Tgt"},
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
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan)
        p._subphase_inject_scene_runtime()

        edges = p.ctx.scene_runtime["cross_domain_edges"]
        assert len(edges) == 1
        assert edges[0]["from_script"] == "src"
        assert edges[0]["to_script"] == "tgt"

        # Subphase no longer writes the file directly; the writer below
        # owns it.
        assert not (tmp_path / "UNCONVERTED.md").exists()

        p._write_unconverted_md()
        report = (tmp_path / "UNCONVERTED.md").read_text(encoding="utf-8")
        assert "Cross-domain references" in report
        # The report renders the canonical script_id (planner key), not
        # the stem -- the script_id is stable across renames whereas
        # stems can collide.
        assert "| src | client |" in report
        assert "| tgt | server |" in report

    def test_rerun_replaces_cross_domain_block(self, tmp_path):
        # First run writes the report; second run with same edges
        # should produce byte-stable output (single source of truth, no
        # accidental drift across reruns).
        plan_v1 = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client", "module_path": "ReplicatedStorage.Src"},
                "tgt": {"stem": "Tgt", "runtime_bearing": True,
                        "domain": "server", "module_path": "ReplicatedStorage.Tgt"},
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
                        "from": "A.unity:1", "field": "p", "index": None,
                        "target_kind": "component", "target_ref": "A.unity:2",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["A.unity:1", "A.unity:2"],
                },
            },
            "prefabs": {}, "domain_overrides": {},
        }
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan_v1)
        p._subphase_inject_scene_runtime()
        p._write_unconverted_md()
        first = (tmp_path / "UNCONVERTED.md").read_text(encoding="utf-8")
        # Re-run with same plan; report content should be byte-stable.
        p._subphase_inject_scene_runtime()
        p._write_unconverted_md()
        second = (tmp_path / "UNCONVERTED.md").read_text(encoding="utf-8")
        assert first == second
        # Only one cross-domain section.
        assert second.count("## Cross-domain references") == 1

    def test_unrelated_unconverted_content_coexists_with_cross_domain(
        self, tmp_path,
    ):
        # R5-P1 fix: ``_write_unconverted_md`` owns the file. Other
        # pipeline stages contribute via their result objects'
        # ``unconverted`` lists (the ``sections`` aggregator). The
        # cross-domain block coexists with those entries in a single
        # write, not via mid-pipeline appending.
        from converter.animation_converter import AnimationConversionResult

        plan = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client", "module_path": "ReplicatedStorage.Src"},
                "tgt": {"stem": "Tgt", "runtime_bearing": True,
                        "domain": "server", "module_path": "ReplicatedStorage.Tgt"},
            },
            "scenes": {
                "A.unity": {
                    "instances": [
                        {"instance_id": "a:1", "script_id": "src",
                         "game_object_id": "a:1", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "a:2", "script_id": "tgt",
                         "game_object_id": "a:2", "active": True,
                         "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "a:1", "field": "p", "index": None,
                        "target_kind": "component", "target_ref": "a:2",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["a:1", "a:2"],
                },
            },
            "prefabs": {}, "domain_overrides": {},
        }
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan)
        # Seed a standard unconverted entry through the supported channel.
        p.state.animation_result = AnimationConversionResult(unconverted=[
            {"category": "animator_controller",
             "item": "Enemy.controller",
             "reason": "binary-encoded"},
        ])
        p._subphase_inject_scene_runtime()
        p._write_unconverted_md()
        contents = (tmp_path / "UNCONVERTED.md").read_text(encoding="utf-8")
        # Both the animation entry and the cross-domain block survive.
        assert "## animator_controller" in contents
        assert "Enemy.controller" in contents
        assert "## Cross-domain references" in contents


# ---------------------------------------------------------------------------
# R2-P2: cross-domain report idempotency on edgeful->edge-free reruns.
# ---------------------------------------------------------------------------

class TestCrossDomainReportEdgeFreeRerun:
    """Codex round-2 P2 / R5-P1: when a re-run goes from edgeful to
    edge-free, the prior cross-domain block must be absent in the final
    UNCONVERTED.md. With the R5 fix the subphase no longer writes the
    file directly; ``_write_unconverted_md`` re-builds the file from
    scratch each time (sections + cross-domain edges off ctx), so the
    stale block can't survive a rerun.
    """

    def test_edgeful_then_edgefree_rerun_strips_stale_block(self, tmp_path):
        plan_edgeful = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client", "module_path": "ReplicatedStorage.Src"},
                "tgt": {"stem": "Tgt", "runtime_bearing": True,
                        "domain": "server", "module_path": "ReplicatedStorage.Tgt"},
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
                        "from": "A.unity:1", "field": "p", "index": None,
                        "target_kind": "component", "target_ref": "A.unity:2",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["A.unity:1", "A.unity:2"],
                },
            },
            "prefabs": {}, "domain_overrides": {},
        }
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan_edgeful)
        p._subphase_inject_scene_runtime()
        p._write_unconverted_md()
        # First run: marker present.
        contents = (tmp_path / "UNCONVERTED.md").read_text(encoding="utf-8")
        assert "## Cross-domain references" in contents

        # Swap to an edge-free plan and re-run.
        plan_edgefree = dict(plan_edgeful)
        plan_edgefree["scenes"] = {
            "A.unity": {
                "instances": plan_edgefree["scenes"]["A.unity"]["instances"],
                "references": [],
                "lifecycle_order": plan_edgefree["scenes"]["A.unity"][
                    "lifecycle_order"],
            },
        }
        p.ctx.scene_runtime = plan_edgefree
        p._subphase_inject_scene_runtime()
        p._write_unconverted_md()
        # With no edges and no other unconverted entries the writer
        # removes the file entirely.
        assert not (tmp_path / "UNCONVERTED.md").exists(), (
            f"edge-free rerun with no sections must remove the file"
        )

    def test_cross_domain_block_survives_write_unconverted_md(
        self, tmp_path,
    ):
        """R5-P1 regression: pre-fix ``_subphase_inject_scene_runtime``
        wrote the cross-domain block to UNCONVERTED.md mid-pipeline,
        and the LATER ``_write_unconverted_md`` rewrote the file from
        scratch off ``sections`` -- silently CLOBBERING the cross-
        domain block on every conversion. Post-fix the subphase only
        stages edges on ctx; the writer reads them off ctx so the
        block survives.
        """
        from converter.animation_converter import AnimationConversionResult

        plan = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client", "module_path": "ReplicatedStorage.Src"},
                "tgt": {"stem": "Tgt", "runtime_bearing": True,
                        "domain": "server", "module_path": "ReplicatedStorage.Tgt"},
            },
            "scenes": {
                "A.unity": {
                    "instances": [
                        {"instance_id": "a:1", "script_id": "src",
                         "game_object_id": "a:1", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "a:2", "script_id": "tgt",
                         "game_object_id": "a:2", "active": True,
                         "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "a:1", "field": "p", "index": None,
                        "target_kind": "component", "target_ref": "a:2",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["a:1", "a:2"],
                },
            },
            "prefabs": {}, "domain_overrides": {},
        }
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan)
        # Seed at least one non-cross-domain unconverted entry to
        # exercise the sections rewrite path (the clobber scenario).
        p.state.animation_result = AnimationConversionResult(unconverted=[
            {"category": "blend_tree", "item": "Move", "reason": "2D"},
        ])
        # Stage 1: the subphase computes edges and stages them on ctx.
        p._subphase_inject_scene_runtime()
        # Stage 2: pipeline's standard UNCONVERTED.md writer. This is
        # the call that pre-R5 silently wiped the cross-domain block
        # because it rebuilt the file from sections only.
        p._write_unconverted_md()
        contents = (tmp_path / "UNCONVERTED.md").read_text(encoding="utf-8")
        assert "## blend_tree" in contents, (
            f"section entry must be preserved; got: {contents}"
        )
        assert "## Cross-domain references" in contents, (
            f"R5-P1 regression: ``_write_unconverted_md`` must NOT "
            f"clobber the cross-domain block; got: {contents}"
        )
        # Resume-path (re-run both stages): block still there, no drift.
        first = contents
        p._subphase_inject_scene_runtime()
        p._write_unconverted_md()
        second = (tmp_path / "UNCONVERTED.md").read_text(encoding="utf-8")
        assert first == second, (
            f"resume path drifted: first={first!r} second={second!r}"
        )

    def test_edgefree_rerun_with_other_entries_drops_stale_cross_domain(
        self, tmp_path,
    ):
        # R5-P1 contract: ``_write_unconverted_md`` rewrites the file
        # from scratch. With no edges but other entries present (e.g.
        # an animation unconverted item), the rewrite contains those
        # entries but NO cross-domain block.
        from converter.animation_converter import AnimationConversionResult

        plan = _runtime_bearing_plan()  # no cross-domain edges
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan)
        p.state.animation_result = AnimationConversionResult(unconverted=[
            {"category": "animator_controller", "item": "Enemy.controller",
             "reason": "binary"},
        ])
        # Pretend a prior run left a stale cross-domain block on ctx.
        p.ctx.scene_runtime["cross_domain_edges"] = []
        p._subphase_inject_scene_runtime()
        p._write_unconverted_md()
        contents = (tmp_path / "UNCONVERTED.md").read_text(encoding="utf-8")
        assert "## animator_controller" in contents
        assert "Enemy.controller" in contents
        assert "## Cross-domain references" not in contents


# ---------------------------------------------------------------------------
# R2-P1.3: asset rewrite + scriptable_objects map populated in plan.
# ---------------------------------------------------------------------------

class TestAutogenDoesNotClobberUserNamedScripts:
    """Codex round-3 P2: ``_replace_or_add`` previously replaced any
    same-named script blindly. A user (or earlier converter pass)
    script named ``SceneRuntime`` / ``SceneRuntimePlan`` / ... would
    be silently dropped. The marker-gated replacement now keeps any
    same-name script whose source does NOT carry our autogen marker."""

    def test_user_named_scene_runtime_script_is_preserved(self, tmp_path):
        p = _make_pipeline_with_ctx(
            tmp_path, "generic", _runtime_bearing_plan(),
        )
        # Pre-seed a USER script named "SceneRuntime" that does NOT
        # carry our autogen marker. The subphase must leave it alone.
        user_script = RbxScript(
            name="SceneRuntime",
            source="-- USER OWNED do not delete\nreturn {}\n",
            script_type="ModuleScript",
            parent_path="ReplicatedStorage",
        )
        p.state.rbx_place.scripts.append(user_script)
        p._subphase_inject_scene_runtime()
        kept = [s for s in p.state.rbx_place.scripts if s.name == "SceneRuntime"]
        assert any(s is user_script for s in kept), (
            "user-owned SceneRuntime script must NOT be displaced; "
            f"current scripts: {[s.name for s in p.state.rbx_place.scripts]}"
        )

    def test_prior_autogen_scene_runtime_is_replaced(self, tmp_path):
        p = _make_pipeline_with_ctx(
            tmp_path, "generic", _runtime_bearing_plan(),
        )
        # A prior autogen artifact carries the marker -- replacement OK.
        prior = RbxScript(
            name="SceneRuntime",
            source="-- scene_runtime: PR4 generic host runtime\nold body\n",
            script_type="ModuleScript",
            parent_path="ReplicatedStorage",
        )
        p.state.rbx_place.scripts.append(prior)
        p._subphase_inject_scene_runtime()
        scene_runtime = [
            s for s in p.state.rbx_place.scripts if s.name == "SceneRuntime"
        ]
        assert len(scene_runtime) == 1, (
            f"prior autogen artifact must be replaced exactly once; "
            f"got: {[s.source[:40] for s in scene_runtime]}"
        )
        assert "old body" not in scene_runtime[0].source, (
            "fresh emit must overwrite prior autogen body"
        )


class TestAssetRefRewriteAndScriptableObjectsMap:
    """Codex round-2 P1.3 (contract resolution):
    - ``target_kind == "asset"`` refs persisted as Unity GUIDs must be
      rewritten to ``rbxassetid://...`` using ``ctx.uploaded_assets``.
    - ``target_kind == "scriptable_object"`` refs need an auxiliary
      ``scene_runtime.scriptable_objects`` map (guid -> dotted module
      path) the host runtime consults to require the live ModuleScript.
    """

    def test_asset_ref_rewritten_via_uploaded_assets(self, tmp_path):
        from core.unity_types import GuidEntry, GuidIndex
        sprite_abs = tmp_path / "Assets" / "Art" / "Diamond.png"
        sprite_abs.parent.mkdir(parents=True, exist_ok=True)
        sprite_abs.write_bytes(b"\x89PNG fake")
        sprite_guid = "ab" + "0" * 30
        idx = GuidIndex(project_root=tmp_path)
        idx.guid_to_entry[sprite_guid] = GuidEntry(
            guid=sprite_guid,
            asset_path=sprite_abs,
            relative_path=Path("Assets/Art/Diamond.png"),
            kind="texture",
        )
        idx.path_to_guid[sprite_abs.resolve()] = sprite_guid

        plan = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client",
                        "module_path": "ReplicatedStorage.Src"},
            },
            "scenes": {
                "A.unity": {
                    "instances": [
                        {"instance_id": "A.unity:1", "script_id": "src",
                         "game_object_id": "A.unity:1", "active": True,
                         "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "A.unity:1", "field": "icon", "index": None,
                        "target_kind": "asset", "target_ref": sprite_guid,
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["A.unity:1"],
                },
            },
            "prefabs": {}, "domain_overrides": {},
        }
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan)
        p.state.guid_index = idx
        p.ctx.uploaded_assets = {str(sprite_abs): "rbxassetid://42424242"}
        p._subphase_inject_scene_runtime()

        rewritten = (p.ctx.scene_runtime["scenes"]["A.unity"]
                     ["references"][0]["target_ref"])
        assert rewritten == "rbxassetid://42424242", (
            f"asset GUID must rewrite to rbxassetid url; got {rewritten!r}"
        )

    def test_unresolvable_asset_guid_left_in_place(self, tmp_path):
        from core.unity_types import GuidIndex
        idx = GuidIndex(project_root=tmp_path)
        plan = {
            "modules": {
                "src": {"stem": "Src", "runtime_bearing": True,
                        "domain": "client",
                        "module_path": "ReplicatedStorage.Src"},
            },
            "scenes": {
                "A.unity": {
                    "instances": [
                        {"instance_id": "A.unity:1", "script_id": "src",
                         "game_object_id": "A.unity:1", "active": True,
                         "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "A.unity:1", "field": "icon", "index": None,
                        "target_kind": "asset", "target_ref": "deadbeef" * 4,
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": ["A.unity:1"],
                },
            },
            "prefabs": {}, "domain_overrides": {},
        }
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan)
        p.state.guid_index = idx
        p.ctx.uploaded_assets = {"unrelated.png": "rbxassetid://1"}
        p._subphase_inject_scene_runtime()
        # GUID unchanged -- operator sees the unresolved reference.
        unchanged = (p.ctx.scene_runtime["scenes"]["A.unity"]
                     ["references"][0]["target_ref"])
        assert unchanged == "deadbeef" * 4

    def test_scriptable_object_map_populated_in_plan(self, tmp_path):
        from core.unity_types import GuidEntry, GuidIndex
        from converter.scriptable_object_converter import (
            AssetConversionResult, ConvertedAsset,
        )
        so_abs = tmp_path / "Assets" / "Data" / "Settings.asset"
        so_abs.parent.mkdir(parents=True, exist_ok=True)
        so_abs.write_text("placeholder", encoding="utf-8")
        so_guid = "1234" + "0" * 28
        idx = GuidIndex(project_root=tmp_path)
        idx.guid_to_entry[so_guid] = GuidEntry(
            guid=so_guid,
            asset_path=so_abs,
            relative_path=Path("Assets/Data/Settings.asset"),
            kind="data",
        )
        idx.path_to_guid[so_abs.resolve()] = so_guid

        so_result = AssetConversionResult()
        so_result.assets.append(ConvertedAsset(
            source_path=so_abs,
            asset_name="Settings",
            luau_source="return {}",
        ))

        plan = _runtime_bearing_plan()
        p = _make_pipeline_with_ctx(tmp_path, "generic", plan)
        p.state.guid_index = idx
        p.state.scriptable_objects = so_result
        # Pre-seed an RbxScript matching what write_output would have
        # added; storage_classifier would have routed it to RS.
        p.state.rbx_place.scripts.append(RbxScript(
            name="Settings", source="return {}",
            script_type="ModuleScript", parent_path="ReplicatedStorage",
        ))
        p._subphase_inject_scene_runtime()
        so_map = p.ctx.scene_runtime.get("scriptable_objects")
        assert so_map == {so_guid: "ReplicatedStorage.Settings"}, (
            f"scriptable_objects map must carry guid -> dotted module path; "
            f"got {so_map!r}"
        )
