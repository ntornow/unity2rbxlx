"""Phase 1 unit + integration tests for ``scene_runtime_topology``.

Covers the 6 test categories from the design doc
(``converter/docs/design/scene-runtime-architecture-ir.md`` §Testing
Phase 1):

  1. Topology emission (artifact shape: ``build_topology`` output)
  2. 6 invariant violations from ``build_topology._enforce_invariants``
  3. ``routing_status`` path coverage (resolved / unresolved / orphan)
  4. ``stable_id`` injectivity (segment escaping is injective per codex W6)
  5. ``cross_domain_edges`` deterministic id format
  6. ``lifecycle_roles.derive_module_lifecycle_role`` branch coverage

Slice 11 wires the doc-mandated SimpleFPS cold-conversion integration
test in-line (see ``test_simplefps_door_lands_as_localscript_in_starter_player_scripts``):
runs ``Pipeline.run_all()`` against the SimpleFPS submodule and asserts
the topology authority's contracts on the live conversion artifact +
``RbxScript`` metadata. Marked ``@pytest.mark.slow`` so the fast suite
stays cheap. Phase 1 otherwise uses synthesized inline fixtures only —
no frozen-fixture round-trips per design doc lines 528-532 (that's
Phase 2a).

References: design doc §Phase 1 + §Testing Phase 1.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.roblox_types import ScriptType  # noqa: E402
from core.unity_types import GuidIndex  # noqa: E402
from converter.scene_runtime_planner import (  # noqa: E402
    SceneRuntimeArtifact,
)
from converter.scene_runtime_topology.animation_routing import (  # noqa: E402
    AnimationDomain,
    AnimationObservedTarget,
    AnimationRoutingStatus,
    NO_CTRL_KEY,
    ORPHAN_SCOPE,
    build_animation_driver_entry,
    compute_stable_id,
    derive_observed_target,
    resolve_driver,
)
from converter.scene_runtime_topology.build_topology import (  # noqa: E402
    EmittedAnimation,
    TopologyInvariantError,
    build_topology,
    callers_of,
)
from converter.scene_runtime_topology.cross_domain_edges import (  # noqa: E402
    compute_cross_domain_edges,
    deterministic_edge_id,
)
from converter.scene_runtime_topology.lifecycle_roles import (  # noqa: E402
    LIFECYCLE_ROLES,
    derive_module_lifecycle_role,
)
from converter.scene_runtime_topology.shared_flag_channels import (  # noqa: E402
    compute_shared_flag_channels,
)
from core.roblox_types import RbxScript  # noqa: E402


# ---------------------------------------------------------------------------
# Fixture builders. Each one returns the minimal valid input shape for a
# topology assembly so tests can read top-to-bottom in one screen
# (mirrors ``test_scene_runtime_domain_v2.py``'s _mk_* helpers).
# ---------------------------------------------------------------------------

def _mk_module(
    stem: str, domain: str, *, class_name: str | None = None,
    character_attached: bool = False, is_loader: bool = False,
) -> dict[str, object]:
    """Return one ``scene_runtime.modules`` row.

    Phase 2a slice 2: every runtime_bearing row must carry
    ``character_attached`` + ``is_loader`` booleans (build_topology
    invariant 7). Defaults are False so most tests don't have to
    plumb them; tests that exercise the
    ``character_attached==True`` / ``is_loader==True`` branches pass
    the kwargs explicitly.
    """
    return {
        "stem": stem,
        "class_name": class_name if class_name is not None else stem,
        "runtime_bearing": True,
        "domain": domain,
        "character_attached": character_attached,
        "is_loader": is_loader,
    }


def _mk_artifact(
    modules: dict[str, dict[str, object]] | None = None,
    scenes: dict[str, dict[str, object]] | None = None,
    prefabs: dict[str, dict[str, object]] | None = None,
) -> SceneRuntimeArtifact:
    return cast(SceneRuntimeArtifact, {
        "modules": modules or {},
        "scenes": scenes or {},
        "prefabs": prefabs or {},
        "domain_overrides": {},
    })


def _mk_rbx_script(
    name: str, script_type: ScriptType = "Script",
) -> RbxScript:
    s = RbxScript(name=name, source="-- empty", script_type=script_type)
    s.intrinsic_script_type = script_type
    return s


def _reach_kwargs(
    modules: dict[str, object],
    scripts: list[RbxScript],
) -> dict[str, object]:
    """Build the script-name/sid-space kwargs
    ``derive_reachability_requirements`` now takes, the same way
    ``_maybe_run_topology_prepass`` does."""
    from converter.roblox_dead_modules import extract_require_edges
    from converter.scene_runtime_planner import build_script_id_by_name

    by_name = {s.name: s for s in scripts if s.name}
    sid_by_name = build_script_id_by_name(
        scripts, cast("dict[str, object]", modules),
    )
    script_by_sid = {
        sid: by_name[name]
        for name, sid in sid_by_name.items()
        if name in by_name
    }
    known = frozenset(s.name for s in scripts if s.name)
    edges = {
        s.name: extract_require_edges(s.source, known)
        for s in scripts if s.name
    }
    return {
        "require_edges_by_name": edges,
        "script_by_sid": script_by_sid,
        "lifecycle_roles": {},
    }


def _failopen_sfc():
    """The fail-open ``shared_flag_channels`` block (no reader scan in
    hand). ``compute_shared_flag_channels(transpiled_scripts=None, ...)``
    records ``present: True`` with empty ``read_names`` — the same shape
    the resume path and the ``build_topology`` back-compat path produce.
    """
    return compute_shared_flag_channels(
        transpiled_scripts=None, script_id_by_name={}, domains={},
    )


def _door_shape_artifact(
    *,
    door_domain: str = "client",
) -> tuple[SceneRuntimeArtifact, str, str]:
    """One prefab + one MonoBehaviour with an Animator ref.

    Returns ``(artifact, prefab_id, mb_script_id)`` so a test can pass
    ``prefab_id`` as ``scope_ref`` to an EmittedAnimation row.
    """
    door_script_id = "guid-door"
    animator_script_id = "guid-animator-target"
    prefab_id = "guid-door-prefab:Assets/Prefabs/Door.prefab"
    mb_instance = "P:1"
    animator_instance = "P:2"

    artifact = _mk_artifact(
        modules={
            door_script_id: _mk_module("Door", door_domain),
            animator_script_id: _mk_module(
                "AnimatorTarget", door_domain, class_name="AnimatorTarget",
            ),
        },
        prefabs={
            prefab_id: {
                "name": "Door",
                "template_name": "Door",
                "instances": [
                    {
                        "instance_id": mb_instance,
                        "script_id": door_script_id,
                        "game_object_id": "P:go-1",
                        "active": True, "enabled": True, "config": {},
                    },
                    {
                        "instance_id": animator_instance,
                        "script_id": animator_script_id,
                        "game_object_id": "P:go-2",
                        "active": True, "enabled": True, "config": {},
                    },
                ],
                "references": [
                    {
                        "from": mb_instance,
                        "field": "animator",
                        "index": None,
                        "target_kind": "component",
                        "target_ref": animator_instance,
                        "target_is_ui": False,
                        "target_component_type": "Animator",
                    },
                ],
                "lifecycle_order": [],
            },
        },
    )
    return artifact, prefab_id, door_script_id


# ===========================================================================
# CATEGORY 1: topology emission (build_topology output shape)
# ===========================================================================


class TestTopologyEmissionShape:
    """Asserts the artifact shape returned by ``build_topology``.

    Refs: design doc §"The topology artifact" (lines 174-240),
    ``build_topology.py:223`` (coordinator entry).
    """

    def test_empty_inputs_produce_empty_artifact(self) -> None:
        """No modules + no emissions → all three blocks empty.

        Refs: ``build_topology.build_topology`` (line 223),
        ``_build_modules_block`` (275), ``_build_animation_drivers_block``
        (351).
        """
        artifact = build_topology(
            scene_runtime=_mk_artifact(),
            emitted_animations=[],
            scripts_by_class={},
        )
        assert artifact["modules"] == {}
        assert artifact["animation_drivers"] == {}
        assert artifact["cross_domain_edges"] == []

    def test_single_client_module_emits_matching_block(self) -> None:
        """A single client-domain module produces one ``modules`` row.

        Refs: ``_build_modules_block`` (build_topology.py:275-348);
        invariant 4 (line 567) validates ``lifecycle_role`` enum.
        """
        sr = _mk_artifact(
            modules={"guid-x": _mk_module("HudControl", "client")},
        )
        scripts_by_class = {
            "HudControl": _mk_rbx_script("HudControl", "LocalScript"),
        }
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class=scripts_by_class,
        )
        modules = artifact["modules"]
        assert "guid-x" in modules
        entry = modules["guid-x"]
        assert entry["stem"] == "HudControl"
        assert entry["domain"] == "client"
        assert entry["script_class"] == "LocalScript"
        # LocalScript + non-character/loader → auto_run per
        # ``derive_module_lifecycle_role`` (lifecycle_roles.py:65).
        assert entry["lifecycle_role"] == "auto_run"
        # Phase 2a slice 2: both bool inputs mirrored on the topology
        # entry so slice 5's storage_classifier consumer reads a single
        # canonical surface.
        assert entry["character_attached"] is False
        assert entry["is_loader"] is False

    def test_character_attached_planner_input_drives_lifecycle_role(
        self,
    ) -> None:
        """When the planner row carries ``character_attached=True``,
        build_topology's `_build_modules_block` reads it (NOT
        hardcoded False) and the derived ``lifecycle_role`` becomes
        ``"character_attached"``.

        Phase 2a slice 2 — the regression this guards: pre-slice-2
        the inputs were always False at the call site
        (build_topology.py:314-315), so this output was unreachable.

        Refs: build_topology.py `_build_modules_block` post-slice-2;
        lifecycle_roles.py:105-106 (priority branch).
        """
        sr = _mk_artifact(modules={
            "guid-pchar": _mk_module(
                "PlayerCharScript", "client",
                character_attached=True,
            ),
        })
        scripts_by_class = {
            "PlayerCharScript": _mk_rbx_script(
                "PlayerCharScript", "LocalScript",
            ),
        }
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class=scripts_by_class,
        )
        entry = artifact["modules"]["guid-pchar"]
        assert entry["character_attached"] is True
        assert entry["is_loader"] is False
        assert entry["lifecycle_role"] == "character_attached"

    def test_is_loader_planner_input_drives_lifecycle_role(self) -> None:
        """When the planner row carries ``is_loader=True``,
        build_topology's `_build_modules_block` reads it and the
        derived ``lifecycle_role`` becomes ``"loader"``.

        Phase 2a slice 2 — pre-slice-2 the input was hardcoded False
        so this output was unreachable.

        Refs: build_topology.py `_build_modules_block` post-slice-2;
        lifecycle_roles.py:107-108 (priority branch).
        """
        sr = _mk_artifact(modules={
            "guid-boot": _mk_module(
                "BootSplash", "client", is_loader=True,
            ),
        })
        scripts_by_class = {
            "BootSplash": _mk_rbx_script("BootSplash", "LocalScript"),
        }
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class=scripts_by_class,
        )
        entry = artifact["modules"]["guid-boot"]
        assert entry["character_attached"] is False
        assert entry["is_loader"] is True
        assert entry["lifecycle_role"] == "loader"

    def test_reachability_pair_empty_when_rule_did_not_fire(self) -> None:
        """Slice 4 (narrowed by slice 9b): a module with no
        reachability rule firing has both reachability fields empty.
        The planner's ``_apply_reachability_rule`` mutates these
        atomically only on client-required helpers in server
        containers; everywhere else the planner leaves them absent
        and topology mirrors that as empty strings.

        Refs: build_topology.py reachability-pair stamp;
        Phase 2a slice 4 + slice 9b (dropped
        ``reachability_forced_container`` mirror).
        """
        sr = _mk_artifact(modules={
            "guid-x": _mk_module("HudControl", "client"),
        })
        scripts_by_class = {
            "HudControl": _mk_rbx_script("HudControl", "LocalScript"),
        }
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class=scripts_by_class,
        )
        entry = artifact["modules"]["guid-x"]
        assert entry["reachability_required_container"] == ""
        assert entry["module_path"] == ""
        assert "reachability_forced_container" not in entry

    def test_planner_rule_end_to_end_satisfies_invariant_10(self) -> None:
        """Slice 4 round 1 review (Claude P1.3); slice 9b narrows
        invariant 10 to module_path ↔ container coherence: drive the
        planner's ``_apply_reachability_rule`` end-to-end (via
        ``classify_scene_runtime_domains``) on a client-module +
        server-container-helper + require-edge fixture. Assert the
        planner produces a stamp that the narrowed invariant 10
        accepts AND that the topology entry surfaces the required
        container.

        Without this, a planner regression that splits the rewrite
        into a non-atomic shape (the exact codex P1.1 failure mode)
        would not be caught by the unit tests — they seed
        planner-side fields directly and never exercise the rule.

        Refs: module_domain.py:_apply_reachability_rule;
        build_topology.py invariant 10 (slice 9b narrowed).
        """
        from converter.scene_runtime_domain import (
            classify_scene_runtime_domains,
            derive_reachability_requirements,
            infer_module_domains,
        )
        # Set up: client module (HudControl) that requires a helper
        # (HelperLib) — helper starts in ServerStorage (the pre-rule
        # state), rule should hoist it to ReplicatedStorage.
        sr = _mk_artifact(modules={
            "guid-hud": _mk_module("HudControl", "client"),
            "guid-helper": {
                "stem": "HelperLib", "class_name": "HelperLib",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
        })
        # Helper script lives in ServerStorage pre-rule; rule should
        # hoist it.
        helper_script = RbxScript(
            name="HelperLib", source="-- helper",
            script_type="ModuleScript",
            parent_path="ServerStorage",
        )
        hud_script = RbxScript(
            name="HudControl",
            source='require(script.Parent:FindFirstChild("HelperLib"))',
            script_type="LocalScript",
        )
        hud_script.intrinsic_script_type = "LocalScript"
        scripts = [helper_script, hud_script]

        # Run the planner's classification (includes
        # _apply_reachability_rule). The client closure now seeds from
        # the LocalScript entry + the emitted-require edge.
        classify_scene_runtime_domains(
            cast("dict", sr),
            scripts,
        )

        # Phase 2a slice 10: ``reachability_required_container`` now
        # sources from ``TopologyInputs.reachability_requirements``
        # normalized through the late-hoist predicate gate (see
        # ``_normalize_reachability_requirement``), not the retired
        # ``domain_signals.reachability_forced_container`` audit
        # signal. Recompute the requirements the way
        # ``_maybe_run_topology_prepass`` does for production calls
        # and thread them into build_topology so this end-to-end test
        # asserts on the new source.
        #
        # Note: ``classify_scene_runtime_domains`` already mutated
        # ``helper_script.parent_path`` to ``"ReplicatedStorage"`` via
        # the late hoist in ``finalize_topology_containers``, so the
        # ``infer_module_domains`` call below is purely to recover the
        # ``domain_results`` shape ``derive_reachability_requirements``
        # consumes (it doesn't read parent_path; per its docstring).
        domain_results = infer_module_domains(
            cast("dict", sr),
            scripts,
        )
        reqs = derive_reachability_requirements(
            cast("dict", sr),
            scripts,
            domain_results,
            **_reach_kwargs(sr["modules"], scripts),  # type: ignore[arg-type]
        )

        # Now build topology and assert invariant 10 passes (no abort).
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={
                "HudControl": hud_script,
                "HelperLib": helper_script,
            },
            reachability_requirements=reqs,
        )
        # The rule should have fired on HelperLib: ``script.parent_path``,
        # ``module_row["container"]``, and ``module_row["module_path"]``
        # all moved to ``ReplicatedStorage`` in lockstep (invariant 10's
        # narrowed coherence check passes). The topology entry's
        # ``reachability_required_container`` is the slice-10 normalized
        # surface: ``""`` because by build_topology read time the
        # late-hoist arm has already moved ``parent_path`` OUT of the
        # gated set (``_SERVER_CONTAINERS_FOR_REACHABILITY``), matching
        # today's PRODUCTION behavior where slice 7's
        # ``_decide_script_container_from_topology`` pre-empts the late
        # hoist arm via ``s.parent_path = "ReplicatedStorage"`` and the
        # audit signal stayed empty. The historical test value
        # ``"ReplicatedStorage"`` captured a vestigial signal the late
        # arm wrote when slice 7 was bypassed; slice 10 surfaces the
        # production-aligned value instead.
        helper_entry = artifact["modules"]["guid-helper"]
        assert helper_entry["reachability_required_container"] == ""
        assert helper_entry["module_path"] == (
            "ReplicatedStorage.HelperLib"
        )
        # Slice 9b dropped the parallel ``reachability_forced_container``
        # mirror from the topology entry; slice 10 retired the
        # planner-row audit signal write.
        assert "reachability_forced_container" not in helper_entry

    def test_planner_rule_invisible_to_empty_name_scripts(self) -> None:
        """Slice 4 round 2 review (Claude P1.A investigation):
        empty-name RbxScripts are filtered out at
        ``script_by_name`` construction (the ``if script.name:``
        guard) so they cannot reach ``_apply_reachability_rule``.
        Result: a helper module whose corresponding script has empty
        name is invisible to the rule — the module stays in its
        pre-rule state.

        This pins the property that round 2 verified: the rule's
        atomic triple-write below the upstream filter does NOT need
        an additional empty-name gate inside the loop body. The
        invariant 10 atomicity check codified at the topology layer
        is the catch-all for any future regression that lets empty-
        name scripts through (the half-stamped row would fail
        closed there).

        Refs: module_domain.py script_by_name filter (line 574);
        Phase 2a slice 4 round 2 review.
        """
        from converter.scene_runtime_domain import (
            classify_scene_runtime_domains,
        )
        sr = _mk_artifact(modules={
            "guid-hud": _mk_module("HudControl", "client"),
            "guid-noname-helper": {
                "stem": "NoNameHelper", "class_name": "NoNameHelper",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
        })
        # Helper script has empty name (synthetic / stub).
        helper_script = RbxScript(
            name="", source="-- helper",
            script_type="ModuleScript",
            parent_path="ServerStorage",
        )
        hud_script = RbxScript(
            name="HudControl",
            source='require(script.Parent:FindFirstChild("NoNameHelper"))',
            script_type="LocalScript",
        )
        hud_script.intrinsic_script_type = "LocalScript"
        scripts = [helper_script, hud_script]

        classify_scene_runtime_domains(
            cast("dict", sr),
            scripts,
        )

        # The helper stays in its pre-rule state — rule never fired
        # because the empty-name script was filtered upstream.
        helper_module = sr["modules"]["guid-noname-helper"]  # type: ignore[index]
        # Domain stays "helper" (initial state for non-runtime-bearing
        # modules per _classify_module's helper short-circuit).
        assert helper_module.get("domain") == "helper"
        # Phase 2a slice 11: the parallel audit-signal assertion on
        # ``domain_signals["reachability_forced_container"]`` was
        # dropped -- slice 10 retired that planner-row write surface.
        # The ``domain == "helper"`` assertion above is the load-
        # bearing pin; the legacy hoist observable lives in the
        # triple-write at ``finalize_topology_containers``
        # (``module_domain.py:937-959``), which on this fixture never
        # fires because the empty-name filter upstream gates it out.

    def test_planner_rule_fires_when_class_name_differs_from_file_stem(
        self,
    ) -> None:
        """Slice 4 round 2 review (Claude P1.B): the planner's
        ``scripts_by_class`` index conflated ``script.name`` (file
        stem) with ``class_name`` (C# class declaration). When the
        two differ (file ``Bootstrap.cs`` containing ``class
        GameInit``), the rule's lookup silently missed and the
        helper was never hoisted even though client modules required
        it.

        Round 2 fix: build ``scripts_by_class`` from the modules
        dict, joining on ``class_name`` with fallback to ``stem``.
        This test pins the fix by setting up a helper whose module
        row has ``class_name="GameInit"`` while the corresponding
        RbxScript has ``name="Bootstrap"`` (the file stem).

        Refs: module_domain.py scripts_by_class join logic;
        Phase 2a slice 4 round 2 review (Claude P1.B).
        """
        from converter.scene_runtime_domain import (
            classify_scene_runtime_domains,
        )
        sr = _mk_artifact(modules={
            "guid-hud": _mk_module("HudControl", "client"),
            "guid-bootstrap": {
                "stem": "Bootstrap",        # file stem
                "class_name": "GameInit",   # C# class name
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
        })
        # RbxScript with name == file stem (NOT == class_name).
        helper_script = RbxScript(
            name="Bootstrap",
            source="-- bootstrap",
            script_type="ModuleScript",
            parent_path="ServerStorage",
        )
        # The emitted require keys by the SCRIPT NAME (file stem
        # "Bootstrap"), the canonical emitted-require keyspace — NOT the
        # C# class_name. This is exactly the keyspace the new closure
        # runs in, so the class_name-vs-stem mismatch is structurally
        # neutralized: the row joins to its script via the stem fallback
        # in build_script_id_by_name.
        hud_script = RbxScript(
            name="HudControl",
            source='require(script.Parent:FindFirstChild("Bootstrap"))',
            script_type="LocalScript",
        )
        hud_script.intrinsic_script_type = "LocalScript"
        scripts = [helper_script, hud_script]

        classify_scene_runtime_domains(
            cast("dict", sr),
            scripts,
        )

        # Rule should have fired: helper hoisted to ReplicatedStorage,
        # module_path uses script.name (file stem), and the triple is
        # consistent for invariant 10. Phase 2a slice 10: the parallel
        # planner-row audit signal
        # ``domain_signals["reachability_forced_container"]`` was
        # retired; the hoist observable is pinned by ``container`` +
        # ``module_path`` + ``helper_script.parent_path``. The
        # class_name-vs-stem-conflation fix this test guards is still
        # exercised end-to-end by those three assertions.
        helper_module = sr["modules"]["guid-bootstrap"]  # type: ignore[index]
        assert helper_module.get("container") == "ReplicatedStorage"
        assert helper_module.get("module_path") == "ReplicatedStorage.Bootstrap"
        assert helper_script.parent_path == "ReplicatedStorage"

    def test_build_scripts_by_class_name_excludes_collisions(
        self,
    ) -> None:
        """Slice 4 round 4 review (Claude P1.1): when two
        ``SceneRuntimeModule`` rows share a ``class_name`` (e.g. two
        ``Utils.cs`` files declaring ``class Utils``), the helper
        EXCLUDES the colliding name from the index. Both modules'
        downstream lookups fall through to safe defaults rather than
        the first-write-wins case silently stamping the WRONG
        script's metadata onto the second module.

        Mirrors slice 3 round 2's degraded-service contract in
        ``_detect_caller_graph_collisions``.

        Refs: scene_runtime_planner.build_scripts_by_class_name
        collision exclusion; Phase 2a slice 4 round 4 review.
        """
        from converter.scene_runtime_planner import (
            build_scripts_by_class_name,
        )
        modules: dict[str, dict[str, object]] = {
            "guid-first": {"stem": "Utils", "class_name": "Utils"},
            "guid-second": {
                # Different stem → different file, same class_name.
                "stem": "Utils2", "class_name": "Utils",
            },
            "guid-noconflict": {"stem": "Foo", "class_name": "Foo"},
        }
        scripts = [
            RbxScript(name="Utils", source="", script_type="ModuleScript"),
            RbxScript(name="Utils2", source="", script_type="LocalScript"),
            RbxScript(name="Foo", source="", script_type="Script"),
        ]
        result = build_scripts_by_class_name(scripts, modules)
        # Colliding class_name is EXCLUDED entirely (neither
        # first-write nor last-write wins — both modules fall through
        # to ModuleScript defaults downstream).
        assert "Utils" not in result
        # Non-colliding class_name passes through normally.
        assert "Foo" in result
        assert result["Foo"].name == "Foo"

    def test_build_scripts_by_class_name_helper(self) -> None:
        """Slice 4 round 3 review (Claude P1.A): the shared
        ``build_scripts_by_class_name`` helper joins modules' class_name
        to scripts via a primary-then-fallback strategy. Direct unit
        test of the helper covers all three join cases.

        Refs: scene_runtime_planner.build_scripts_by_class_name;
        Phase 2a slice 4 round 3 review.
        """
        from converter.scene_runtime_planner import (
            build_scripts_by_class_name,
        )
        modules: dict[str, dict[str, object]] = {
            "guid-foo": {"stem": "Foo", "class_name": "Foo"},
            "guid-boot": {"stem": "Bootstrap", "class_name": "GameInit"},
            "guid-orphan": {"stem": "Orphan", "class_name": "Orphan"},
            "guid-empty-cn": {"stem": "Whatever", "class_name": ""},
        }
        scripts = [
            RbxScript(name="Foo", source="", script_type="Script"),
            # GameInit has no script named "GameInit"; fallback to
            # the Bootstrap script (matches module.stem).
            RbxScript(name="Bootstrap", source="", script_type="ModuleScript"),
            # Orphan has no matching script (neither name nor stem
            # matches an existing script) — entry omitted.
            # No script for guid-empty-cn either, but it's skipped
            # at class_name == "" check.
        ]
        result = build_scripts_by_class_name(scripts, modules)
        # Direct match.
        assert "Foo" in result
        assert result["Foo"].name == "Foo"
        # Fallback match: class_name → stem.
        assert "GameInit" in result
        assert result["GameInit"].name == "Bootstrap"
        # No match: omitted.
        assert "Orphan" not in result
        # Empty class_name: never joined.
        assert "" not in result

    def test_topology_build_modules_handles_class_name_stem_mismatch(
        self, tmp_path: Path,
    ) -> None:
        """Slice 4 round 3 review (Claude P1.A end-to-end): when
        ``_build_modules_block`` is invoked via the pipeline (which
        now uses the shared helper), a module with
        ``class_name="GameInit"`` and a script with
        ``name="Bootstrap"`` correctly resolves through the fallback
        join and the topology row emits ``script_class`` matching
        the actual RbxScript.script_type.

        Pre-round-3 the pipeline-built scripts_by_class was keyed by
        ``script.name``, so ``_build_modules_block.scripts_by_class.get
        ("GameInit")`` returned None, falling through to
        ``script_class="ModuleScript"`` regardless of the actual
        script_type. This test pins the corrected behavior.

        Refs: pipeline.py:_build_and_apply_topology + scene_runtime_
        planner.build_scripts_by_class_name; Phase 2a slice 4 round 3.
        """
        from types import SimpleNamespace
        from converter.pipeline import Pipeline
        from converter.animation_converter import AnimationConversionResult
        from converter.code_transpiler import TranspilationResult
        from core.roblox_types import RbxPlace, RbxScript
        artifact = _mk_artifact(modules={
            # Module declares class_name="GameInit" but file stem
            # is "Bootstrap" (the script's name).
            "guid-init": {
                "stem": "Bootstrap",
                "class_name": "GameInit",
                "runtime_bearing": True,
                "domain": "client",
                "character_attached": False,
                "is_loader": False,
            },
        })
        scene_runtime = cast("dict[str, object]", artifact)

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        # Script.name == "Bootstrap" (file stem), NOT class_name.
        # script_type is LocalScript — topology should reflect it.
        rbx_place.scripts = [
            RbxScript(name="Bootstrap", source="", script_type="LocalScript"),
        ]
        anim_result = AnimationConversionResult()
        anim_result.emitted_animations = []
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,
            guid_index=None,
            dependency_map={},
            transpilation_result=TranspilationResult(),
        )

        plan = TestApplyTopologyToRbxScripts._mk_plan()
        pipeline._build_and_apply_topology(scene_runtime, plan)

        topo_module = scene_runtime["topology"]["modules"]["guid-init"]  # type: ignore[index]
        # Pre-fix this would have defaulted to "ModuleScript"; post-fix
        # the fallback join finds the Bootstrap script and surfaces its
        # actual script_type.
        assert topo_module["script_class"] == "LocalScript"

    def test_invariant_10_module_path_equals_container_is_legal(
        self,
    ) -> None:
        """Slice 4 round 1 review (Claude P1.2): invariant 10 must
        accept ``module_path == reachability_required_container``
        (a top-level container row with no module suffix). The
        pre-fix ``startswith(f"{required}.")`` check rejected this
        legitimate shape AND would have false-positively accepted a
        sibling-container prefix.

        Refs: build_topology.py invariant 10 module_path check;
        Phase 2a slice 4 round 1 review.
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {
                "guid-helper": {
                    "stem": "Helper", "domain": "helper",
                    "script_class": "ModuleScript",
                    "lifecycle_role": "requireable",
                    "character_attached": False, "is_loader": False,
                    "bridge_group_id": None, "provenance": {},
                    "reachability_required_container": "ReplicatedStorage",
                    "module_path": "ReplicatedStorage",  # exact match — legal
                },
            },
            "animation_drivers": {},
            "cross_domain_edges": [],
            "caller_graph": {},
        }
        # Should not raise.
        _enforce_invariants(
            cast("dict", artifact),
            emitted_animations=[],
            scene_runtime=_mk_artifact(),
        )

    def test_invariant_10_rejects_sibling_container_prefix(
        self,
    ) -> None:
        """Slice 4 round 1 review (Claude P1.2): the relaxed
        ``module_path == required OR startswith(required + ".")`` check
        must NOT false-positively accept a sibling-container prefix
        like ``"ReplicatedStorageOther.Helper"`` (which would slip
        through a bare ``startswith(required)`` without the dot).
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {
                "guid-helper": {
                    "stem": "Helper", "domain": "helper",
                    "script_class": "ModuleScript",
                    "lifecycle_role": "requireable",
                    "character_attached": False, "is_loader": False,
                    "bridge_group_id": None, "provenance": {},
                    "reachability_required_container": "ReplicatedStorage",
                    "module_path": "ReplicatedStorageOther.Helper",
                },
            },
            "animation_drivers": {},
            "cross_domain_edges": [],
            "caller_graph": {},
        }
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=[],
                scene_runtime=_mk_artifact(),
            )
        assert "invariant 10" in str(excinfo.value)

    def test_door_shape_emits_resolved_animation_driver(self) -> None:
        """Door scenario: 1 prefab + 1 MonoBehaviour holding an Animator
        ref + 1 emitted animation → resolved driver + client placement.

        Refs: design doc lines 198-213 (animation_drivers entry shape),
        ``resolve_driver`` (animation_routing.py:269),
        ``build_animation_driver_entry`` (line 359).
        """
        sr, prefab_id, driver_guid = _door_shape_artifact(
            door_domain="client",
        )
        emitted: list[EmittedAnimation] = [{
            "scope_kind": "prefab",
            "scope_ref": prefab_id,
            "scope_display": "Door",
            "ctrl_key": "Door",
            "clip_disp": "open",
            "script_name": "Anim_Door_door_open",
            "observed_attribute": "open",
            "curve_paths": ["door"],
            "prefab_scoped": True,
        }]
        scripts_by_class = {
            "Door": _mk_rbx_script("Door", "LocalScript"),
            "AnimatorTarget": _mk_rbx_script(
                "AnimatorTarget", "ModuleScript",
            ),
        }
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=emitted,
            scripts_by_class=scripts_by_class,
        )
        drivers = artifact["animation_drivers"]
        assert len(drivers) == 1
        # stable_id keys on prefab_id (planner-stable), not bare name.
        sid = compute_stable_id(prefab_id, "Door", "open")
        assert sid in drivers
        entry = drivers[sid]
        assert entry["routing_status"] == "resolved"
        assert entry["driver_module_guid"] == driver_guid
        assert entry["domain"] == "client"
        assert entry["script_class"] == "LocalScript"
        assert entry["lifecycle_role"] == "auto_run"
        assert entry["observed_attribute"] == "open"
        assert entry["bridge_group_id"] is None
        assert entry["observed_target"]["kind"] == "child"
        assert entry["observed_target"]["name"] == "door"


# ===========================================================================
# CATEGORY 2: 6 invariant violations
# ===========================================================================


class TestTopologyInvariants:
    """Each invariant gets at least one direct test that trips it.

    Refs: ``build_topology._enforce_invariants`` (line 458),
    invariants 1-6 documented at the top of build_topology.py.
    """

    def test_invariant_1_resolved_anim_references_unknown_module(
        self,
    ) -> None:
        """Resolved driver guid not in modules block.

        We force the violation by post-mutating the artifact AFTER an
        otherwise-valid resolved driver entry is built. The cleanest
        path is to invoke ``_enforce_invariants`` directly with a
        crafted artifact, mirroring the helper-test style in
        ``test_scene_runtime_domain_v2.py``.
        Refs: build_topology.py:482-507.
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {},  # driver guid not present → invariant 1
            "animation_drivers": {
                "Door::__none__::open": {
                    "stable_id": "Door::__none__::open",
                    "routing_status": "resolved",
                    "driver_module_guid": "guid-missing",
                    "domain": "client",
                    "script_class": "LocalScript",
                    "lifecycle_role": "auto_run",
                    "observed_attribute": "open",
                    "observed_target": {
                        "kind": "self", "name": "", "scope": "workspace",
                    },
                    "bridge_group_id": None,
                },
            },
            "cross_domain_edges": [],
        }
        emitted: list[EmittedAnimation] = [{
            "scope_kind": "scene", "scope_ref": "Door",
            "scope_display": "Door",
            "ctrl_key": "", "clip_disp": "open",
            "script_name": "Anim_Door_door_open",
            "observed_attribute": "open",
            "curve_paths": [], "prefab_scoped": False,
        }]
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=emitted,
                scene_runtime=_mk_artifact(),
            )
        assert "invariant 1" in str(excinfo.value)

    def test_invariant_2_edge_with_non_runtime_domain(self) -> None:
        """``compute_cross_domain_edges`` filters non-runtime domains, but
        the invariant is defense-in-depth; we exercise it directly.

        Refs: build_topology.py:513-523.
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {},
            "animation_drivers": {},
            "cross_domain_edges": [
                {
                    "id": "P:1::field::P:2",
                    "from_instance": "P:1", "to_instance": "P:2",
                    "from_script": "g1", "to_script": "g2",
                    "field": "ref",
                    "from_domain": "helper",  # <- non-runtime
                    "to_domain": "server",
                    "owner_kind": "scene", "owner_ref": "X.unity",
                },
            ],
        }
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=[],
                scene_runtime=_mk_artifact(),
            )
        assert "invariant 2" in str(excinfo.value)

    def test_invariant_3_duplicate_stable_id_in_emissions(self) -> None:
        """Two emissions colliding on stable_id.

        Refs: build_topology.py:530-565.
        """
        sr, prefab_id, _ = _door_shape_artifact()
        emitted: list[EmittedAnimation] = [
            {
                "scope_kind": "prefab", "scope_ref": prefab_id,
                "scope_display": "Door", "ctrl_key": "Door",
                "clip_disp": "open", "script_name": "Anim_Door_door_open",
                "observed_attribute": "open", "curve_paths": ["door"],
                "prefab_scoped": True,
            },
            # Same scope_ref + ctrl_key + clip_disp → same stable_id.
            {
                "scope_kind": "prefab", "scope_ref": prefab_id,
                "scope_display": "Door", "ctrl_key": "Door",
                "clip_disp": "open",
                "script_name": "Anim_Door_door_open_duplicate",
                "observed_attribute": "open", "curve_paths": ["door"],
                "prefab_scoped": True,
            },
        ]
        with pytest.raises(TopologyInvariantError) as excinfo:
            build_topology(
                scene_runtime=sr,
                emitted_animations=emitted,
                scripts_by_class={
                    "Door": _mk_rbx_script("Door", "LocalScript"),
                    "AnimatorTarget": _mk_rbx_script(
                        "AnimatorTarget", "ModuleScript",
                    ),
                },
            )
        assert "invariant 3" in str(excinfo.value)

    def test_invariant_4_module_has_lifecycle_role_outside_enum(
        self,
    ) -> None:
        """Synthesize a module row whose ``lifecycle_role`` is not in the
        closed enum.

        Refs: build_topology.py:567-589, lifecycle_roles.LIFECYCLE_ROLES.
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {
                "g-bad": {
                    "stem": "Bogus",
                    "domain": "client",
                    "script_class": "LocalScript",
                    "lifecycle_role": "not_a_real_role",
                    "bridge_group_id": None,
                    "provenance": {},
                },
            },
            "animation_drivers": {},
            "cross_domain_edges": [],
        }
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=[],
                scene_runtime=_mk_artifact(),
            )
        assert "invariant 4" in str(excinfo.value)

    def test_invariant_5_bridge_group_id_not_in_edges(self) -> None:
        """A module references a bridge_group_id that no edge declares.

        Refs: build_topology.py:591-610.
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {
                "g": {
                    "stem": "Mod", "domain": "client",
                    "script_class": "LocalScript",
                    "lifecycle_role": "auto_run",
                    "bridge_group_id": "nonexistent-edge",
                    "provenance": {},
                },
            },
            "animation_drivers": {},
            "cross_domain_edges": [],
        }
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=[],
                scene_runtime=_mk_artifact(),
            )
        assert "invariant 5" in str(excinfo.value)

    def test_invariant_6_driver_module_has_non_runtime_domain(self) -> None:
        """Resolved animation whose driver module's domain is ``helper``.

        Constructed directly because ``resolve_driver`` filters
        non-runtime drivers earlier; invariant 6 is the defense-in-depth
        layer.
        Refs: build_topology.py:498-507, animation_routing.py:344-346.
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {
                "g-helper-driver": {
                    "stem": "HelperDriver",
                    "domain": "helper",  # <- non-runtime
                    "script_class": "ModuleScript",
                    "lifecycle_role": "requireable",
                    "bridge_group_id": None,
                    "provenance": {},
                },
            },
            "animation_drivers": {
                "Door::__none__::open": {
                    "stable_id": "Door::__none__::open",
                    "routing_status": "resolved",
                    "driver_module_guid": "g-helper-driver",
                    "domain": "client",  # mismatch is fine for inv 6 setup
                    "script_class": "LocalScript",
                    "lifecycle_role": "auto_run",
                    "observed_attribute": "open",
                    "observed_target": {
                        "kind": "self", "name": "", "scope": "workspace",
                    },
                    "bridge_group_id": None,
                },
            },
            "cross_domain_edges": [],
        }
        emitted: list[EmittedAnimation] = [{
            "scope_kind": "scene", "scope_ref": "Door",
            "scope_display": "Door",
            "ctrl_key": "", "clip_disp": "open",
            "script_name": "Anim_Door_door_open",
            "observed_attribute": "open",
            "curve_paths": [], "prefab_scoped": False,
        }]
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=emitted,
                scene_runtime=_mk_artifact(),
            )
        assert "invariant 6" in str(excinfo.value)

    def test_invariant_7_missing_character_attached_field(self) -> None:
        """Runtime-bearing planner row lacking `character_attached` fails
        closed. Reads scene_runtime["modules"] (planner input), not the
        topology output — the check exists because _build_modules_block
        defaults missing values to False, which would silently produce a
        wrong lifecycle_role.

        Refs: build_topology.py invariant 7 block; Phase 2a slice 2.
        """
        sr = _mk_artifact(modules={
            "guid-x": {
                "stem": "Foo", "class_name": "Foo", "runtime_bearing": True,
                "domain": "client",
                # `character_attached` deliberately omitted; `is_loader`
                # present so the test exercises ONLY the
                # character_attached branch.
                "is_loader": False,
            },
        })
        with pytest.raises(TopologyInvariantError) as excinfo:
            build_topology(
                scene_runtime=sr, emitted_animations=[], scripts_by_class={},
            )
        msg = str(excinfo.value)
        assert "invariant 7" in msg
        assert "character_attached" in msg

    def test_invariant_7_missing_is_loader_field(self) -> None:
        """Runtime-bearing planner row lacking `is_loader` fails closed.

        Refs: build_topology.py invariant 7 block.
        """
        sr = _mk_artifact(modules={
            "guid-x": {
                "stem": "Foo", "class_name": "Foo", "runtime_bearing": True,
                "domain": "client",
                "character_attached": False,
                # `is_loader` deliberately omitted.
            },
        })
        with pytest.raises(TopologyInvariantError) as excinfo:
            build_topology(
                scene_runtime=sr, emitted_animations=[], scripts_by_class={},
            )
        msg = str(excinfo.value)
        assert "invariant 7" in msg
        assert "is_loader" in msg

    def test_invariant_7_skips_non_runtime_bearing_rows(self) -> None:
        """Helper rows (runtime_bearing=False) are exempt from invariant
        7 — they have no lifecycle role to derive, so the inputs aren't
        required. This is the migration-discipline path for legacy
        helper artifacts that pre-date slice 2.

        Refs: build_topology.py invariant 7 block (guarded on
        ``runtime_bearing``).
        """
        sr = _mk_artifact(modules={
            "guid-helper": {
                "stem": "Helper", "class_name": "Helper",
                "runtime_bearing": False,
                # No character_attached, no is_loader; should be allowed.
            },
        })
        # Should not raise.
        artifact = build_topology(
            scene_runtime=sr, emitted_animations=[], scripts_by_class={},
        )
        assert "guid-helper" in artifact["modules"]

    # Slice 9b deleted two invariant-10 lockstep tests
    # (``test_invariant_10_reachability_required_without_forced_aborts``,
    # ``test_invariant_10_reachability_divergent_values_aborts``)
    # because the parallel ``reachability_forced_container`` mirror
    # was dropped from ``TopologyModuleEntry`` and the
    # required-vs-forced lockstep arm was tautological (same
    # _build_modules_block loop set both fields from the same source,
    # so they could not legitimately diverge). The surviving
    # invariant-10 arm — ``module_path`` ↔ container coherence — is
    # still covered by ``test_invariant_10_module_path_must_start_with_container``
    # and ``test_invariant_10_rejects_sibling_container_prefix``.

    def test_invariant_10_module_path_must_start_with_container(
        self,
    ) -> None:
        """Slice 4: when reachability fired, ``module_path`` MUST
        start with the rule's container value. Pre-slice-4 codex P1.1
        at module_domain.py:1266-1278 fixed a host-resolve bug where
        the rule moved the container but left module_path pointing at
        the old location. Invariant 10 codifies the constraint.
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {
                "guid-helper": {
                    "stem": "Helper", "domain": "helper",
                    "script_class": "ModuleScript",
                    "lifecycle_role": "requireable",
                    "character_attached": False, "is_loader": False,
                    "bridge_group_id": None, "provenance": {},
                    "reachability_required_container": "ReplicatedStorage",
                    "module_path": "ServerStorage.Helper",  # stale
                },
            },
            "animation_drivers": {},
            "cross_domain_edges": [],
            "caller_graph": {},
        }
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=[],
                scene_runtime=_mk_artifact(),
            )
        msg = str(excinfo.value)
        assert "invariant 10" in msg
        assert "module_path" in msg

    def test_invariant_8_loader_role_with_false_is_loader_aborts(
        self,
    ) -> None:
        """``lifecycle_role="loader"`` with ``is_loader=False`` is
        structurally impossible from `derive_module_lifecycle_role` but
        an external-provenance artifact (hand-edited plan, future
        derivation regression) could produce it. Invariant 8 catches
        that drift.

        Refs: build_topology.py invariant 8 block; Phase 2a slice 2
        round 3.
        """
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {
                "guid-x": {
                    "stem": "Loader", "domain": "client",
                    "script_class": "LocalScript",
                    "lifecycle_role": "loader",
                    "character_attached": False,
                    "is_loader": False,  # contradicts the role
                    "bridge_group_id": None,
                    "provenance": {},
                },
            },
            "animation_drivers": {},
            "cross_domain_edges": [],
        }
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=[],
                scene_runtime=_mk_artifact(modules={
                    "guid-x": _mk_module(
                        "Loader", "client", is_loader=False,
                    ),
                }),
            )
        msg = str(excinfo.value)
        assert "invariant 8" in msg
        assert "is_loader=False" in msg

    def test_invariant_8_loader_role_with_server_domain_aborts(
        self,
    ) -> None:
        """A "loader" role on a server-domain module violates
        invariant 8: ReplicatedFirst is client-only by definition."""
        from converter.scene_runtime_topology.build_topology import (
            _enforce_invariants,
        )
        artifact = {
            "modules": {
                "guid-x": {
                    "stem": "Loader", "domain": "server",
                    "script_class": "Script",
                    "lifecycle_role": "loader",
                    "character_attached": False,
                    "is_loader": True,
                    "bridge_group_id": None,
                    "provenance": {},
                },
            },
            "animation_drivers": {},
            "cross_domain_edges": [],
        }
        with pytest.raises(TopologyInvariantError) as excinfo:
            _enforce_invariants(
                cast("dict", artifact),
                emitted_animations=[],
                scene_runtime=_mk_artifact(modules={
                    "guid-x": _mk_module(
                        "Loader", "server", is_loader=True,
                    ),
                }),
            )
        msg = str(excinfo.value)
        assert "invariant 8" in msg
        assert "client-domain" in msg

    def test_invariant_8_allows_is_loader_true_with_auto_run_role(
        self,
    ) -> None:
        """The deliberate raw-hint-vs-gated-decision divergence:
        ``is_loader=True`` may legitimately coexist with
        ``lifecycle_role="auto_run"`` when a gate fires (e.g. a
        server-domain script whose stem matches the loader regex).
        Invariant 8 only enforces ONE direction — `loader → bools`,
        not `bool → loader`.

        Refs: TopologyModuleEntry field-semantic contract docstring;
        build_topology.py invariant 8.
        """
        # Server-domain "BootstrapServer.cs" matches REPLICATED_FIRST_HINTS
        # but the loader gate drops it (domain != "client"), so the role
        # falls through to "auto_run". The raw is_loader=True remains on
        # the topology entry as audit info.
        sr = _mk_artifact(modules={
            "guid-srv": _mk_module(
                "BootstrapServer", "server", is_loader=True,
            ),
        })
        scripts_by_class = {
            "BootstrapServer": _mk_rbx_script(
                "BootstrapServer", "Script",
            ),
        }
        # Should NOT raise. Build + assert the deliberate divergence.
        artifact = build_topology(
            scene_runtime=sr, emitted_animations=[],
            scripts_by_class=scripts_by_class,
        )
        entry = artifact["modules"]["guid-srv"]
        assert entry["is_loader"] is True
        assert entry["lifecycle_role"] == "auto_run"

    def test_backfill_lifecycle_role_inputs_unblocks_resumed_pre_slice2_plan(
        self,
    ) -> None:
        """The migration helper makes a pre-slice-2 scene_runtime artifact
        invariant-7-clean. Replicates the user-resume scenario the Claude
        review (round 2 on slice 2) flagged as P1: an on-disk plan
        without the two new fields would otherwise hard-abort when
        build_topology runs.

        Verifies single-source-of-truth: `is_loader` is derived from the
        same REPLICATED_FIRST_HINTS regex the planner uses, so a backfill
        of a 'Loader.cs' stem produces is_loader=True (matches what a
        fresh replan would emit on the same project). Pairs that
        bool with a LocalScript script-class binding so
        ``derive_module_lifecycle_role`` returns ``"loader"`` (the
        ModuleScript path correctly falls through to ``"requireable"``
        per the codex P2 gate — covered separately in
        ``TestLifecycleRoleDerivation``).

        Refs: scene_runtime_planner.backfill_lifecycle_role_inputs;
        pipeline._classify_storage call site.
        """
        from converter.scene_runtime_planner import (
            backfill_lifecycle_role_inputs,
        )
        # Pre-slice-2 shape: runtime_bearing module without the new keys.
        # `LevelLoader` matches REPLICATED_FIRST_HINTS, so the backfill
        # should stamp is_loader=True.
        sr = _mk_artifact(modules={
            "guid-x": {
                "stem": "LevelLoader",
                "class_name": "LevelLoader",
                "runtime_bearing": True,
                "domain": "client",
            },
            "guid-helper": {
                # Non-runtime-bearing row — backfill must skip it.
                "stem": "Helper", "class_name": "Helper",
                "runtime_bearing": False,
            },
        })
        count = backfill_lifecycle_role_inputs(cast("dict", sr))
        assert count == 1  # Only the runtime_bearing row mutated.
        runtime_row = sr["modules"]["guid-x"]  # type: ignore[index]
        assert runtime_row["character_attached"] is False
        assert runtime_row["is_loader"] is True  # regex matched on 'Loader'
        # Non-runtime-bearing row untouched.
        helper_row = sr["modules"]["guid-helper"]  # type: ignore[index]
        assert "character_attached" not in helper_row
        assert "is_loader" not in helper_row
        # And the now-backfilled artifact survives invariant 7.
        # Pair with a LocalScript binding so lifecycle_role derives
        # to "loader" (the executable-script branch).
        scripts_by_class = {
            "LevelLoader": _mk_rbx_script("LevelLoader", "LocalScript"),
        }
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class=scripts_by_class,
        )
        assert artifact["modules"]["guid-x"]["lifecycle_role"] == "loader"
        # Idempotent: re-running the backfill yields 0 mutations.
        assert backfill_lifecycle_role_inputs(cast("dict", sr)) == 0


# ===========================================================================
# CATEGORY 3: routing_status path coverage
# ===========================================================================


class TestRoutingStatusCoverage:
    """resolve_driver / build_animation_drivers status branches.

    Refs: animation_routing.py:269-346, build_topology.py:373-448.
    """

    def test_same_scope_single_driver_resolves(self) -> None:
        """One prefab, one Animator-referencing MB → ``resolved``.

        Refs: animation_routing.py:332 (len(candidate_mbs)==1 branch).
        """
        sr, prefab_id, driver_guid = _door_shape_artifact(
            door_domain="client",
        )
        result = resolve_driver(
            sr, scope_kind="prefab", scope_ref=prefab_id,
        )
        assert result is not None
        guid, domain = result
        assert guid == driver_guid
        assert domain == "client"

    def test_same_scope_multi_driver_unresolved(self) -> None:
        """Two distinct MBs both serializing an Animator → ambiguous.

        Refs: animation_routing.py:332 (len != 1 returns None);
        build_topology.py:399-402 (status stamped 'unresolved').
        """
        prefab_id = "guid-prefab:Assets/Prefabs/Multi.prefab"
        sr = _mk_artifact(
            modules={
                "guid-mb-a": _mk_module("DriverA", "client"),
                "guid-mb-b": _mk_module("DriverB", "client"),
                "guid-anim": _mk_module(
                    "AnimatorTarget", "client", class_name="AnimatorTarget",
                ),
            },
            prefabs={
                prefab_id: {
                    "name": "Multi",
                    "template_name": "Multi",
                    "instances": [
                        {
                            "instance_id": "P:1", "script_id": "guid-mb-a",
                            "game_object_id": "P:go-1",
                            "active": True, "enabled": True, "config": {},
                        },
                        {
                            "instance_id": "P:2", "script_id": "guid-mb-b",
                            "game_object_id": "P:go-2",
                            "active": True, "enabled": True, "config": {},
                        },
                        {
                            "instance_id": "P:3", "script_id": "guid-anim",
                            "game_object_id": "P:go-3",
                            "active": True, "enabled": True, "config": {},
                        },
                    ],
                    "references": [
                        {
                            "from": "P:1", "field": "animator",
                            "index": None, "target_kind": "component",
                            "target_ref": "P:3", "target_is_ui": False,
                            "target_component_type": "Animator",
                        },
                        {
                            "from": "P:2", "field": "animator",
                            "index": None, "target_kind": "component",
                            "target_ref": "P:3", "target_is_ui": False,
                            "target_component_type": "Animator",
                        },
                    ],
                    "lifecycle_order": [],
                },
            },
        )
        result = resolve_driver(
            sr, scope_kind="prefab", scope_ref=prefab_id,
        )
        assert result is None  # multi-driver collapses to None

        # And the build coordinator stamps `unresolved` with empty guid
        # and fallback server placement.
        emitted: list[EmittedAnimation] = [{
            "scope_kind": "prefab", "scope_ref": prefab_id,
            "scope_display": "Multi",
            "ctrl_key": "Multi", "clip_disp": "play",
            "script_name": "Anim_Multi_play",
            "observed_attribute": "play",
            "curve_paths": [], "prefab_scoped": True,
        }]
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=emitted,
            scripts_by_class={
                "DriverA": _mk_rbx_script("DriverA", "LocalScript"),
                "DriverB": _mk_rbx_script("DriverB", "LocalScript"),
                "AnimatorTarget": _mk_rbx_script(
                    "AnimatorTarget", "ModuleScript",
                ),
            },
        )
        sid = compute_stable_id(prefab_id, "Multi", "play")
        entry = artifact["animation_drivers"][sid]
        assert entry["routing_status"] == "unresolved"
        assert entry["driver_module_guid"] == ""
        assert entry["domain"] == "server"  # fallback per design doc
        assert entry["script_class"] == "Script"

    def test_orphan_clip_routes_orphan(self) -> None:
        """``scope_kind="orphan"`` produces routing_status="orphan".

        Refs: animation_routing.py:310-311 (orphan short-circuit),
        build_topology.py:390-393 (orphan branch in builder).
        """
        emitted: list[EmittedAnimation] = [{
            "scope_kind": "orphan", "scope_ref": "",
            "scope_display": "_orphans_",
            "ctrl_key": "", "clip_disp": "FloatingClip",
            "script_name": "Anim__orphans___FloatingClip",
            "observed_attribute": "",
            "curve_paths": [], "prefab_scoped": False,
        }]
        artifact = build_topology(
            scene_runtime=_mk_artifact(),
            emitted_animations=emitted,
            scripts_by_class={},
        )
        # stable_id keys on ORPHAN_SCOPE sentinel for empty scope_ref.
        sid = compute_stable_id(ORPHAN_SCOPE, None, "FloatingClip")
        entry = artifact["animation_drivers"][sid]
        assert entry["routing_status"] == "orphan"
        assert entry["driver_module_guid"] == ""
        assert entry["domain"] == "server"


# ===========================================================================
# CATEGORY 3b: Phase-2 source-narrowing (0-ref) resolution (door run)
# ===========================================================================


def _mk_guid_index_with_sources(
    tmp_path: Path, sources: dict[str, str],
) -> GuidIndex:
    """Build a real ``GuidIndex`` mapping each guid → an on-disk .cs file
    whose contents are ``sources[guid]``. Used by the Phase-2 narrowing
    tests (``resolve_driver`` reads C# source via the index).
    """
    from core.unity_types import GuidEntry
    idx = GuidIndex(project_root=tmp_path)
    for guid, text in sources.items():
        p = tmp_path / f"{guid}.cs"
        p.write_text(text, encoding="utf-8")
        idx.guid_to_entry[guid] = GuidEntry(
            guid=guid, asset_path=p, relative_path=Path(f"{guid}.cs"),
            kind="script",
        )
    return idx


def _mk_zero_ref_scope_artifact(
    *,
    mbs: dict[str, str],
) -> tuple[SceneRuntimeArtifact, str]:
    """One prefab with N MonoBehaviour instances and ZERO Animator
    serialized references (the SimpleFPS Door 0-ref case). ``mbs`` maps
    script_id → domain. Returns ``(artifact, prefab_id)``.
    """
    prefab_id = "guid-zr-prefab:Assets/Prefabs/ZeroRef.prefab"
    instances = []
    modules: dict[str, dict[str, object]] = {}
    for i, (sid, domain) in enumerate(mbs.items(), start=1):
        modules[sid] = _mk_module(f"MB{i}", domain, class_name=f"MB{i}")
        instances.append({
            "instance_id": f"P:{i}", "script_id": sid,
            "game_object_id": f"P:go-{i}",
            "active": True, "enabled": True, "config": {},
        })
    artifact = _mk_artifact(
        modules=modules,
        prefabs={
            prefab_id: {
                "name": "ZeroRef",
                "template_name": "ZeroRef",
                "instances": instances,
                "references": [],  # ZERO Animator refs → candidate_mbs empty
                "lifecycle_order": [],
            },
        },
    )
    return artifact, prefab_id


class TestPhase2SourceNarrowing:
    """``resolve_driver`` Phase-2 0-ref narrowing + ``_narrow_driver_by_param_writes``.

    Refs: animation_routing.py (split ``len(candidate_mbs)`` branch +
    ``_narrow_driver_by_param_writes``); animation_driver_analyzer.py;
    module_domain._load_cs_source_preserving_strings. Decisions D10–D13,
    DP3–DP5.
    """

    def test_zero_ref_matching_mb_resolves_client(self, tmp_path: Path) -> None:
        """0-ref scope + the clip's ``observed_attribute`` written by a
        client MB → ``(guid, "client")`` (the Door bug fix)."""
        artifact, prefab_id = _mk_zero_ref_scope_artifact(
            mbs={"guid-door": "client"},
        )
        idx = _mk_guid_index_with_sources(tmp_path, {
            "guid-door": (
                "private Animator doorAnim { get { return null; } }\n"
                'void F(){ doorAnim.SetBool("open", value); }\n'
            ),
        })
        result = resolve_driver(
            artifact, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=idx, observed_attribute="open",
        )
        assert result == ("guid-door", "client")

    def test_zero_ref_empty_observed_attribute_returns_none(
        self, tmp_path: Path,
    ) -> None:
        """D12 fail-fast: autoplay clip (empty observed_attribute) → None,
        BEFORE any source read."""
        artifact, prefab_id = _mk_zero_ref_scope_artifact(
            mbs={"guid-door": "client"},
        )
        idx = _mk_guid_index_with_sources(tmp_path, {
            "guid-door": 'void F(){ doorAnim.SetBool("open", value); }\n',
        })
        result = resolve_driver(
            artifact, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=idx, observed_attribute="",
        )
        assert result is None

    def test_zero_ref_two_mbs_write_param_returns_none(
        self, tmp_path: Path,
    ) -> None:
        """Two scope MBs both write the param → ambiguous → None (no
        silent mis-selection)."""
        artifact, prefab_id = _mk_zero_ref_scope_artifact(
            mbs={"guid-a": "client", "guid-b": "server"},
        )
        write = (
            "private Animator anim { get { return null; } }\n"
            'void F(){ anim.SetBool("open", v); }\n'
        )
        idx = _mk_guid_index_with_sources(tmp_path, {
            "guid-a": write, "guid-b": write,
        })
        result = resolve_driver(
            artifact, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=idx, observed_attribute="open",
        )
        assert result is None

    def test_zero_ref_guid_index_none_returns_none(self) -> None:
        """No Unity root → no source → None (Phase-1-only behavior, edge 7)."""
        artifact, prefab_id = _mk_zero_ref_scope_artifact(
            mbs={"guid-door": "client"},
        )
        result = resolve_driver(
            artifact, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=None, observed_attribute="open",
        )
        assert result is None

    def test_two_serialized_refs_skip_phase2(self, tmp_path: Path) -> None:
        """≥2 serialized Animator refs → ``len(candidate_mbs) >= 2`` →
        return None WITHOUT running Phase-2 (D11/DP4). Even though both
        MBs' source writes the param, Phase-2 never reads it."""
        prefab_id = "guid-multi:Assets/Prefabs/Multi.prefab"
        sr = _mk_artifact(
            modules={
                "guid-mb-a": _mk_module("DriverA", "client"),
                "guid-mb-b": _mk_module("DriverB", "client"),
                "guid-anim": _mk_module(
                    "AnimTarget", "client", class_name="AnimTarget",
                ),
            },
            prefabs={
                prefab_id: {
                    "name": "Multi", "template_name": "Multi",
                    "instances": [
                        {"instance_id": "P:1", "script_id": "guid-mb-a",
                         "game_object_id": "P:go-1", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "P:2", "script_id": "guid-mb-b",
                         "game_object_id": "P:go-2", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "P:3", "script_id": "guid-anim",
                         "game_object_id": "P:go-3", "active": True,
                         "enabled": True, "config": {}},
                    ],
                    "references": [
                        {"from": "P:1", "field": "animator", "index": None,
                         "target_kind": "component", "target_ref": "P:3",
                         "target_is_ui": False,
                         "target_component_type": "Animator"},
                        {"from": "P:2", "field": "animator", "index": None,
                         "target_kind": "component", "target_ref": "P:3",
                         "target_is_ui": False,
                         "target_component_type": "Animator"},
                    ],
                    "lifecycle_order": [],
                },
            },
        )
        write = (
            "[SerializeField] Animator anim;\n"
            'void F(){ anim.SetInteger("actionNumber", n); }\n'
        )
        idx = _mk_guid_index_with_sources(tmp_path, {
            "guid-mb-a": write, "guid-mb-b": write,
        })
        result = resolve_driver(
            sr, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=idx, observed_attribute="actionNumber",
        )
        assert result is None  # ≥2-ref branch: Phase-2 not run (D11)

    def test_zero_ref_no_param_match_returns_none(
        self, tmp_path: Path,
    ) -> None:
        """The clip's observed_attribute isn't written by any scope MB →
        None → server fallback (D8)."""
        artifact, prefab_id = _mk_zero_ref_scope_artifact(
            mbs={"guid-door": "client"},
        )
        idx = _mk_guid_index_with_sources(tmp_path, {
            "guid-door": (
                "private Animator anim { get { return null; } }\n"
                'void F(){ anim.SetBool("open", v); }\n'
            ),
        })
        result = resolve_driver(
            artifact, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=idx, observed_attribute="actionNumber",
        )
        assert result is None

    def test_zero_ref_same_module_multiple_instances_dedup_resolves_once(
        self, tmp_path: Path,
    ) -> None:
        """The SAME module placed by MULTIPLE instances in the 0-ref scope
        is counted ONCE (dedup in ``_narrow_driver_by_param_writes`` via
        ``dict.fromkeys``) → resolves to that single driver, NOT the
        ``len(matched) != 1`` ambiguous-None branch (DP3)."""
        prefab_id = "guid-dup:Assets/Prefabs/Dup.prefab"
        sr = _mk_artifact(
            modules={"guid-door": _mk_module("Door", "client")},
            prefabs={
                prefab_id: {
                    "name": "Dup", "template_name": "Dup",
                    # Two instances → ONE shared script_id (same MB class
                    # placed twice). Without dedup this would yield two
                    # matched script_ids → ambiguous-None.
                    "instances": [
                        {"instance_id": "P:1", "script_id": "guid-door",
                         "game_object_id": "P:go-1", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "P:2", "script_id": "guid-door",
                         "game_object_id": "P:go-2", "active": True,
                         "enabled": True, "config": {}},
                    ],
                    "references": [],  # 0-ref → Phase-2 path
                    "lifecycle_order": [],
                },
            },
        )
        idx = _mk_guid_index_with_sources(tmp_path, {
            "guid-door": (
                "private Animator doorAnim { get { return null; } }\n"
                'void F(){ doorAnim.SetBool("open", value); }\n'
            ),
        })
        result = resolve_driver(
            sr, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=idx, observed_attribute="open",
        )
        assert result == ("guid-door", "client")

    def test_zero_ref_matching_server_mb_stays_server(
        self, tmp_path: Path,
    ) -> None:
        """A uniquely-matched driver whose module domain is ``server``
        passes through as ``(guid, "server")`` — the resolver inherits the
        MB's domain and does NOT force it to client (D8)."""
        artifact, prefab_id = _mk_zero_ref_scope_artifact(
            mbs={"guid-srv": "server"},
        )
        idx = _mk_guid_index_with_sources(tmp_path, {
            "guid-srv": (
                "private Animator anim { get { return null; } }\n"
                'void F(){ anim.SetInteger("phase", n); }\n'
            ),
        })
        result = resolve_driver(
            artifact, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=idx, observed_attribute="phase",
        )
        assert result == ("guid-srv", "server")

    def test_zero_ref_empty_guid_index_resolves_nothing_returns_none(
        self, tmp_path: Path,
    ) -> None:
        """An empty (but constructed) ``GuidIndex`` resolves no guid →
        ``_load_cs_source_preserving_strings`` returns "" for every scope
        MB → no match → None. Distinct from ``guid_index=None`` (which
        short-circuits earlier): here the index object exists but is empty."""
        artifact, prefab_id = _mk_zero_ref_scope_artifact(
            mbs={"guid-door": "client"},
        )
        empty_idx = GuidIndex(project_root=tmp_path)  # no guid_to_entry
        assert empty_idx.resolve("guid-door") is None  # resolves nothing
        result = resolve_driver(
            artifact, scope_kind="prefab", scope_ref=prefab_id,
            guid_index=empty_idx, observed_attribute="open",
        )
        assert result is None


# ===========================================================================
# CATEGORY 4: stable_id injectivity
# ===========================================================================


class TestStableIdInjectivity:
    """``compute_stable_id`` is injective across distinct segment tuples.

    Refs: animation_routing.py:147-197 (_escape_segment + compute_stable_id),
    codex W6 fix in the module docstring.
    """

    def test_separator_segment_pairs_do_not_collide(self) -> None:
        """``("A", "B:C", "D")`` and ``("A:B", "C", "D")`` were the W6
        collision example: without percent-encoding both would render as
        ``"A:B:C:D"``. With encoding they are distinct.
        """
        sid1 = compute_stable_id("A", "B:C", "D")
        sid2 = compute_stable_id("A:B", "C", "D")
        assert sid1 != sid2

    def test_percent_in_name_round_trips_via_quote(self) -> None:
        """``unquote(quote(name))`` round-trips for representative Unity
        names. Documents that ``_escape_segment`` keeps the inverse map
        clean for diagnostic / report code that wants the display form.
        """
        from urllib.parse import unquote
        for name in [
            "Door",
            "Path/With/Slashes",
            "Has:Colon",
            "100%Damage",
            "Üñîçødé",
        ]:
            assert unquote(quote(name, safe="", encoding="utf-8")) == name

    def test_no_ctrl_key_substitutes_sentinel(self) -> None:
        """``compute_stable_id(scope, None, clip)`` substitutes
        ``__none__`` so unresolved-controller clips have a stable key.

        Refs: animation_routing.py:60 (NO_CTRL_KEY), 192 (substitution).
        """
        sid = compute_stable_id("scope-x", None, "Clip")
        # Encoded "__none__" between the two colons.
        assert f":{NO_CTRL_KEY}:" in sid


# ===========================================================================
# CATEGORY 5: cross_domain_edges deterministic id
# ===========================================================================


class TestCrossDomainEdgeId:
    """``deterministic_edge_id`` format + uniqueness.

    Refs: cross_domain_edges.py:62-76.
    """

    def test_format_is_documented_triple(self) -> None:
        """``<from>::<field>::<to>`` literal format per docstring."""
        assert deterministic_edge_id("P:1", "animator", "P:2") == (
            "P:1::animator::P:2"
        )

    def test_distinct_triples_produce_distinct_ids(self) -> None:
        """Two edges differing in any one of (from, field, to) → distinct
        ids. ``compute_cross_domain_edges`` requires this for invariant 5
        to be meaningful.
        """
        a = deterministic_edge_id("P:1", "fx", "P:2")
        b = deterministic_edge_id("P:1", "fy", "P:2")  # field differs
        c = deterministic_edge_id("P:3", "fx", "P:2")  # from differs
        d = deterministic_edge_id("P:1", "fx", "P:4")  # to differs
        assert len({a, b, c, d}) == 4

    def test_identical_triple_returns_identical_id(self) -> None:
        """Determinism: same triple twice → same id. The docstring notes
        this collapse is correct (one MB's one field pointing at one
        peer IS one edge).
        """
        assert deterministic_edge_id("P:1", "f", "P:2") == (
            deterministic_edge_id("P:1", "f", "P:2")
        )


# ===========================================================================
# CATEGORY 5b: Phase 2b slice 1 — extended edge schema + shared-attribute
# candidates
# ===========================================================================


def _mk_edge_artifact(
    *,
    src_domain: str = "client",
    tgt_domain: str = "server",
    src_class: str = "Door",
    tgt_class: str = "Anim",
    field: str = "open",
) -> dict[str, object]:
    """Synthesize a 1-scene plan with one component-ref reference."""
    return {
        "modules": {
            "src": {
                "stem": src_class, "class_name": src_class,
                "runtime_bearing": True, "domain": src_domain,
                "module_path": f"ReplicatedStorage.{src_class}",
            },
            "tgt": {
                "stem": tgt_class, "class_name": tgt_class,
                "runtime_bearing": True, "domain": tgt_domain,
                "module_path": f"ReplicatedStorage.{tgt_class}",
            },
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
                    "field": field,
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


class TestPhase2bSlice1ExtendedSchema:
    """Phase 2b: ``CrossDomainEdge`` (Class 1, component-ref) schema.

    Component-ref edges carry the ``kind`` / ``resolution`` /
    ``bridge_member_scripts`` / ``payload`` fields. (The slices-1-2
    shared-attribute seed/candidate machinery — Class 2 mis-modeled as
    Class 1 — was RETIRED in the 2026-06-01 reframe; see
    ``TestSharedFlagChannels`` for the replacement Class-2 fact.)

    Refs: cross_domain_edges.py, design doc Phase 2b section.
    """

    def test_component_ref_edge_has_new_schema_fields(self) -> None:
        """A synthesized cross-domain component-ref edge carries
        ``kind == "attribute_write"``, the new ``resolution`` with
        ``strategy == "remote_event_bridge"``, an event name following
        the ``<owner>_Set<Field>`` scheme (design doc L239 / L907),
        an empty ``bridge_member_scripts`` (slice 2 fills it), and a
        ``payload`` whose ``attribute_name`` matches the field.
        """
        plan = _mk_edge_artifact(
            src_class="Door", field="open",
            src_domain="client", tgt_domain="server",
        )
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert len(edges) == 1
        edge = edges[0]
        assert edge["kind"] == "attribute_write"
        assert edge["resolution"]["strategy"] == "remote_event_bridge"
        # ``<owner>_Set<Field>``: owner is Door's class_name, field is
        # ``open`` (capitalize first letter only).
        assert edge["resolution"]["event_name"] == "Door_SetOpen"
        assert edge["bridge_member_scripts"] == []
        assert edge["payload"]["attribute_name"] == "open"
        assert edge["payload"]["schema"] == "unknown"
        # Flat fields preserved (no nested producer/consumer restructure
        # in slice 1).
        assert edge["from_script"] == "src"
        assert edge["to_script"] == "tgt"
        assert edge["field"] == "open"


# ===========================================================================
# CATEGORY 6: lifecycle_roles derivation
# ===========================================================================


class TestLifecycleRoleDerivation:
    """Every branch of ``derive_module_lifecycle_role``.

    Refs: lifecycle_roles.py:65-115. Priority order:
    character_attached > is_loader > script_class.
    """

    def test_character_attached_wins(self) -> None:
        """``character_attached=True`` overrides every other input."""
        role = derive_module_lifecycle_role(
            domain="client", script_class="LocalScript",
            character_attached=True, is_loader=False,
        )
        assert role == "character_attached"

    def test_loader_wins_over_script_class(self) -> None:
        """``is_loader=True`` overrides class-driven defaults for
        ``Script`` / ``LocalScript`` — ReplicatedFirst placement assumes
        the script auto-runs."""
        role = derive_module_lifecycle_role(
            domain="client", script_class="LocalScript",
            character_attached=False, is_loader=True,
        )
        assert role == "loader"

    def test_is_loader_with_server_domain_falls_through_to_auto_run(
        self,
    ) -> None:
        """``is_loader=True`` is honored ONLY when ``domain="client"``.

        ``loader`` routes to ReplicatedFirst, a client-only container —
        the role docstring declares it "always client-domain." A
        runtime-bearing server module whose name happens to match the
        loader regex (e.g. a server-side ``BootstrapServer.cs``) must
        fall through to ``"auto_run"`` (its Script default), NOT
        ``"loader"``.

        Without this gate the topology emits self-contradictory rows
        (``domain="server", lifecycle_role="loader"``) that any
        downstream consumer trusting ``lifecycle_role`` would route to
        the wrong container (codex review 2026-05-28 P2 on slice 2
        round 1).

        Refs: lifecycle_roles.py loader branch domain gate.
        """
        role = derive_module_lifecycle_role(
            domain="server", script_class="Script",
            character_attached=False, is_loader=True,
        )
        assert role == "auto_run"

    def test_character_attached_with_server_domain_falls_through(
        self,
    ) -> None:
        """``character_attached=True`` is honored ONLY when
        ``domain="client"``.

        ``character_attached`` routes to StarterCharacterScripts, a
        client-only container — the role docstring declares it "always
        client-domain." A runtime-bearing server module flagged
        character_attached (shouldn't happen in production, but could
        appear in a malformed external artifact) must fall through to
        its class-driven default, NOT silently emit a
        client-only-container role on a server module.

        Refs: lifecycle_roles.py character_attached branch domain gate.
        """
        role = derive_module_lifecycle_role(
            domain="server", script_class="Script",
            character_attached=True, is_loader=False,
        )
        assert role == "auto_run"

    def test_is_loader_with_module_script_falls_through_to_requireable(
        self,
    ) -> None:
        """``is_loader=True`` does NOT promote a ``ModuleScript`` to
        ``"loader"``. A ModuleScript can't auto-run, so ReplicatedFirst
        placement is meaningless for it — matches
        ``storage_classifier._decide_script_container``'s explicit
        ``script_type != "ModuleScript"`` gate.

        Without this gate the topology row's ``lifecycle_role`` would
        disagree with what storage_classifier actually places (codex
        review 2026-05-28 P2 on slice 2: a ``LoadingUtils`` helper
        required by a real Loader script lands in ReplicatedStorage
        under the storage path, but the topology row would have
        emitted ``lifecycle_role="loader"`` pre-gate).

        Refs: lifecycle_roles.py:107-108 (gated branch);
        storage_classifier.py:319 (the parallel gate).
        """
        role = derive_module_lifecycle_role(
            domain="client", script_class="ModuleScript",
            character_attached=False, is_loader=True,
        )
        assert role == "requireable"

    def test_local_script_routes_auto_run(self) -> None:
        role = derive_module_lifecycle_role(
            domain="client", script_class="LocalScript",
            character_attached=False, is_loader=False,
        )
        assert role == "auto_run"

    def test_script_routes_auto_run(self) -> None:
        role = derive_module_lifecycle_role(
            domain="server", script_class="Script",
            character_attached=False, is_loader=False,
        )
        assert role == "auto_run"

    def test_module_script_routes_requireable(self) -> None:
        role = derive_module_lifecycle_role(
            domain="client", script_class="ModuleScript",
            character_attached=False, is_loader=False,
        )
        assert role == "requireable"

    def test_helper_module_routes_requireable(self) -> None:
        """Helper domain + ModuleScript → requireable. Matches the
        docstring's "never instantiate but require-target shape" rule.
        """
        role = derive_module_lifecycle_role(
            domain="helper", script_class="ModuleScript",
            character_attached=False, is_loader=False,
        )
        assert role == "requireable"

    def test_excluded_module_routes_requireable(self) -> None:
        """Excluded modules fall to ``requireable`` so a downstream
        consumer doesn't auto-run them.
        Refs: lifecycle_roles.py:108-115 (safe-default branch).
        """
        role = derive_module_lifecycle_role(
            domain="excluded", script_class="ModuleScript",
            character_attached=False, is_loader=False,
        )
        assert role == "requireable"

    def test_all_returned_roles_are_in_closed_enum(self) -> None:
        """Belt-and-suspenders: every branch's return value is a member of
        ``LIFECYCLE_ROLES``. Mirrors invariant 4 in build_topology.
        """
        sampled = {
            derive_module_lifecycle_role(
                domain="client", script_class="LocalScript",
                character_attached=True, is_loader=False,
            ),
            derive_module_lifecycle_role(
                domain="client", script_class="LocalScript",
                character_attached=False, is_loader=True,
            ),
            derive_module_lifecycle_role(
                domain="server", script_class="Script",
                character_attached=False, is_loader=False,
            ),
            derive_module_lifecycle_role(
                domain="client", script_class="ModuleScript",
                character_attached=False, is_loader=False,
            ),
        }
        assert sampled <= set(LIFECYCLE_ROLES)


# ===========================================================================
# Integration (slice 11 — SimpleFPS cold conversion).
# ===========================================================================


from tests._project_paths import SIMPLEFPS_PATH, is_populated  # noqa: E402


@pytest.mark.slow
@pytest.mark.skipif(
    not is_populated(SIMPLEFPS_PATH),
    reason="SimpleFPS test project not available",
)
def test_simplefps_topology_authority_contract_on_cold_conversion(
    tmp_path: Path,
) -> None:
    """SimpleFPS cold-conversion integration test for Phase 1's topology
    authority (design doc §Testing Phase 1 — restated to match actual
    Phase 1 narrowing scope).

    Runs a fresh ``Pipeline.run_all()`` cold conversion on SimpleFPS
    (no upload, no AI transpilation) and asserts what Phase 1's
    topology authority ACTUALLY delivers — distinct from what Phase 2
    will deliver.

    Phase-1's narrowing only resolves animation drivers whose owning
    MonoBehaviour has a serialized ``[SerializeField] Animator`` field
    captured by the scene-runtime planner's reference walk. MBs that
    access their Animator via a property / runtime getter (e.g.
    ``transform.parent.Find("door").GetComponent<Animator>()`` in
    SimpleFPS's Door.cs) have no serialized ref, so Phase-1 returns
    ``None``. Phase-2's C#-source narrowing (door run) closes that gap:
    it matches the clip's ``observed_attribute`` against each scope MB's
    ``Set*("<param>")`` Animator writes and inherits the matching MB's
    domain. Door.cs writes ``doorAnim.SetBool("open", …)``, so the Door
    clips now resolve to Door's CLIENT domain (LocalScript in
    StarterPlayer.StarterPlayerScripts).

    HostilePlane / PlaneHolder stay UNRESOLVED + server: they are
    autoplay clips with empty ``observed_attribute`` (D12 fail-fast), and
    PlaneHolder's Machine.cs writes on a SERIALIZED Animator field so the
    ≥1-candidate scope skips Phase-2 entirely (D11/DP4). This test asserts
    both the resolution contract (Door) and the fallback contract
    (HostilePlane/PlaneHolder):

      1. ``scene_runtime.topology`` block emitted under generic mode,
         with non-empty ``animation_drivers``.
      2. ``topology.modules`` includes Door with ``stem="Door"`` and
         ``domain="client"`` — the domain Phase-2 narrowing inherits.
      3. Every driver carries an EXPLICIT ``routing_status`` from
         ``{"resolved","unresolved","orphan"}`` — no ``__orphan__``
         sentinels (codex B1 fix).
      4. For every emitted ``Anim_*`` row, the live ``RbxScript``
         placement is consistent with the topology decision:
           * ``resolved + client → LocalScript`` in
             ``StarterPlayer.StarterPlayerScripts``,
           * ``resolved + server → Script`` in ``ServerScriptService``,
           * ``unresolved / orphan → Script`` in
             ``ServerScriptService`` (safe fallback).
      5. Anim_Door_* resolve + client + LocalScript; Anim_HostilePlane_*
         / Anim_PlaneHolder_* stay unresolved + server.
      6. Invariant 3 holds: no duplicate ``Anim_*`` names in
         ``rbx_place.scripts``.
    """
    import config
    old_ai = config.USE_AI_TRANSPILATION
    config.USE_AI_TRANSPILATION = False
    try:
        from converter.pipeline import Pipeline
        pipeline = Pipeline(
            unity_project_path=SIMPLEFPS_PATH,
            output_dir=tmp_path,
            skip_upload=True,
        )
        # The topology authority gates on scene_runtime_mode != "legacy"
        # (pipeline._classify_storage:3985). Legacy mode bypasses
        # ``_build_and_apply_topology`` entirely; generic mode is what
        # /convert-unity uses for real conversions.
        pipeline.ctx.scene_runtime_mode = "generic"
        pipeline.run_all()
    finally:
        config.USE_AI_TRANSPILATION = old_ai

    # ------------------------------------------------------------------
    # Assertion 1: topology block emitted with non-empty drivers.
    # ------------------------------------------------------------------
    import json as _json
    plan_path = tmp_path / "conversion_plan.json"
    assert plan_path.exists(), (
        "conversion_plan.json must be written after Pipeline.run_all()"
    )
    plan = _json.loads(plan_path.read_text())
    scene_runtime = plan.get("scene_runtime", {})
    topology = scene_runtime.get("topology", {})
    assert topology, (
        "scene_runtime.topology block missing — pipeline._build_and_apply_topology "
        "didn't run (regression in the slice 8 wire-in?)"
    )
    modules_block = topology.get("modules", {})
    drivers_block = topology.get("animation_drivers", {})
    assert drivers_block, (
        "topology.animation_drivers empty — SimpleFPS has Door + HostilePlane "
        "+ PlaneHolder animations so emitted_animations should be non-empty"
    )

    # ------------------------------------------------------------------
    # Assertion 2: Door appears in topology.modules with stem='Door' and
    # domain='client'. Phase-2 source-narrowing routes Door's animation
    # driver (property-based Animator access) to this client domain, so
    # the classifier MUST see Door as client-domain for the narrowing to
    # inherit the right domain (asserted resolved+client in Assertion 5).
    # ------------------------------------------------------------------
    door_modules = [
        (guid, entry) for guid, entry in modules_block.items()
        if entry.get("stem") == "Door"
    ]
    assert door_modules, (
        "topology.modules has no entry with stem='Door' — "
        "classifier-v2 regression?"
    )
    for guid, entry in door_modules:
        assert entry.get("domain") == "client", (
            f"Door module {guid}: domain={entry.get('domain')!r} "
            f"(expected 'client' — Door.cs is client-domain in classifier-v2)"
        )

    # ------------------------------------------------------------------
    # Assertion 3: every driver carries an explicit routing_status from
    # the closed set. Guards against codex B1's __orphan__ sentinel
    # regression.
    # ------------------------------------------------------------------
    _allowed_statuses = {"resolved", "unresolved", "orphan"}
    for sid, entry in drivers_block.items():
        status = entry.get("routing_status")
        assert status in _allowed_statuses, (
            f"driver {sid}: routing_status={status!r} not in "
            f"{sorted(_allowed_statuses)} — codex B1 regression?"
        )

    # ------------------------------------------------------------------
    # Map stable_id → script_name from emitted_animations so we can
    # cross-check each driver's topology decision against the live
    # RbxScript placement (Assertion 4). Mirrors the keying in
    # pipeline._build_and_apply_topology (line 4181); any change there
    # forces an update here too.
    # ------------------------------------------------------------------
    animation_result = pipeline.state.animation_result
    assert animation_result is not None, (
        "pipeline.state.animation_result is None — animation_converter "
        "didn't run (or wasn't reachable from the topology coordinator)"
    )
    script_name_by_stable_id: dict[str, str] = {}
    for row in animation_result.emitted_animations:
        scope_ref = row.get("scope_ref", "")
        scope_segment = scope_ref if scope_ref else ORPHAN_SCOPE
        sid = compute_stable_id(
            scope_segment,
            row.get("ctrl_key", "") or None,
            row.get("clip_disp", ""),
        )
        script_name_by_stable_id[sid] = row.get("script_name", "")

    rbx_place = pipeline.state.rbx_place
    assert rbx_place is not None, "pipeline.state.rbx_place must be populated"
    scripts_by_name: dict[str, RbxScript] = {
        s.name: s for s in rbx_place.scripts if s.name
    }

    # ------------------------------------------------------------------
    # Assertion 4: every driver maps to a live RbxScript whose placement
    # is consistent with the topology decision. resolved-client →
    # LocalScript in StarterPlayer.StarterPlayerScripts; everything else
    # stays Script in ServerScriptService (Phase 1's safe fallback).
    #
    # Crucially, a driver with no matching script_name OR no matching
    # RbxScript FAILS the test (rather than being silently skipped).
    # ``pipeline._build_and_apply_topology`` already treats both as
    # consumer drift and warns per row (pipeline.py:4218-4230); if we
    # let the test ``continue`` we'd allow a partial wiring break to
    # pass silently — codex review finding.
    # ------------------------------------------------------------------
    for sid, entry in drivers_block.items():
        script_name = script_name_by_stable_id.get(sid, "")
        assert script_name, (
            f"driver {sid!r}: no emitted_animations row maps to this "
            f"stable_id — emit→artifact key drift between "
            f"animation_converter and build_topology"
        )
        script = scripts_by_name.get(script_name)
        assert script is not None, (
            f"driver {sid!r} → script_name={script_name!r} has no "
            f"matching RbxScript in rbx_place — animation_result → "
            f"rbx_place wiring drift"
        )
        status = entry.get("routing_status")
        entry_domain = entry.get("domain")
        if status == "resolved" and entry_domain == "client":
            assert script.script_type == "LocalScript", (
                f"{script_name}: script_type={script.script_type!r} "
                f"(driver resolved + client → expected 'LocalScript' from "
                f"pipeline._build_and_apply_topology)"
            )
            assert script.parent_path == "StarterPlayer.StarterPlayerScripts", (
                f"{script_name}: parent_path={script.parent_path!r} "
                f"(expected 'StarterPlayer.StarterPlayerScripts')"
            )
        else:
            assert script.script_type == "Script", (
                f"{script_name}: script_type={script.script_type!r} "
                f"(driver routing_status={status!r} domain={entry_domain!r} "
                f"→ expected fallback 'Script')"
            )
            assert script.parent_path == "ServerScriptService", (
                f"{script_name}: parent_path={script.parent_path!r} "
                f"(driver routing_status={status!r} domain={entry_domain!r} "
                f"→ expected fallback 'ServerScriptService')"
            )

    # ------------------------------------------------------------------
    # Assertion 5: pin each known-broken SimpleFPS prefab family by
    # PREFIX. Without family-level pinning, a regression that silently
    # drops Door / HostilePlane / PlaneHolder emissions but keeps some
    # unrelated Anim_* would slip past Assertion 4 — codex review.
    #
    # Why prefix and not full name: animation_converter synthesizes
    # script names from ``f"Anim_{scope}_{ctrl_key}_{clip_disp}"``
    # (animation_converter.py:2065), and ``ctrl_key`` / ``clip_disp``
    # are collision-disambiguated by ``_disambiguate_by_source()`` —
    # which appends an 8-char sha8 if any project elsewhere ships a
    # same-named controller/clip. Pinning full names couples this test
    # to the disambiguator's tiebreak ordering. Prefix pinning matches
    # whatever the controller produces while still catching the
    # "family disappeared entirely" case.
    #
    # Driver-status contract (POST Phase-2 source-narrowing, door run):
    # each family MUST have ≥1 emitted row.
    #   * Anim_Door_*  → RESOLVED + client + LocalScript in
    #     StarterPlayer.StarterPlayerScripts. Door's Animator is a runtime
    #     getter (``transform.parent.Find("door").GetComponent<Animator>()``,
    #     no serialized ref), so Phase-1 returns None; Phase-2's C#-source
    #     narrowing matches the clip's observed_attribute ("open") against
    #     Door.cs's ``doorAnim.SetBool("open", …)`` write and inherits
    #     Door's client domain (D10–D13).
    #   * Anim_HostilePlane_* / Anim_PlaneHolder_* → UNRESOLVED + server.
    #     These are autoplay clips with empty observed_attribute (D12
    #     fail-fast) — and PlaneHolder's ``Machine.cs`` writes
    #     ``planeHolder.SetInteger("actionNumber")`` on a SERIALIZED
    #     Animator field, so candidate_mbs is non-empty and Phase-2's
    #     0-ref-only scoping (D11/DP4) never runs for it. Both guards keep
    #     them on the intended-permanent server fallback (acceptance 10).
    # ------------------------------------------------------------------
    _expected_resolved_client_anim_prefixes = (
        "Anim_Door_",
    )
    _expected_unresolved_anim_prefixes = (
        "Anim_HostilePlane_",
        "Anim_PlaneHolder_",
    )
    sid_by_script_name: dict[str, str] = {
        script_name: sid
        for sid, script_name in script_name_by_stable_id.items()
        if script_name
    }
    for prefix in _expected_resolved_client_anim_prefixes:
        family_scripts = [
            s for s in rbx_place.scripts
            if s.name and s.name.startswith(prefix)
        ]
        assert family_scripts, (
            f"no Anim_* script with prefix {prefix!r} found in "
            f"rbx_place.scripts — animation_converter regression "
            f"(family disappeared from SimpleFPS output)"
        )
        for script in family_scripts:
            sid = sid_by_script_name.get(script.name, "")
            assert sid, (
                f"{script.name}: no stable_id in emitted_animations — "
                f"emit/artifact drift"
            )
            entry = drivers_block.get(sid, {})
            assert entry.get("routing_status") == "resolved", (
                f"{script.name}: routing_status="
                f"{entry.get('routing_status')!r} (expected "
                f"'resolved' — Phase-2 source-narrowing should match "
                f"Door.cs doorAnim.SetBool(\"open\") to the clip's "
                f"observed_attribute)"
            )
            assert entry.get("domain") == "client", (
                f"{script.name}: domain={entry.get('domain')!r} "
                f"(expected 'client' — inherited from Door's domain)"
            )
            assert script.script_type == "LocalScript", (
                f"{script.name}: script_type={script.script_type!r} "
                f"(expected 'LocalScript' — resolved client driver)"
            )
            assert script.parent_path == "StarterPlayer.StarterPlayerScripts", (
                f"{script.name}: parent_path={script.parent_path!r} "
                f"(expected 'StarterPlayer.StarterPlayerScripts')"
            )
    for prefix in _expected_unresolved_anim_prefixes:
        family_scripts = [
            s for s in rbx_place.scripts
            if s.name and s.name.startswith(prefix)
        ]
        assert family_scripts, (
            f"no Anim_* script with prefix {prefix!r} found in "
            f"rbx_place.scripts — animation_converter regression "
            f"(family disappeared from SimpleFPS output)"
        )
        for script in family_scripts:
            sid = sid_by_script_name.get(script.name, "")
            assert sid, (
                f"{script.name}: no stable_id in emitted_animations — "
                f"emit/artifact drift"
            )
            entry = drivers_block.get(sid, {})
            assert entry.get("routing_status") == "unresolved", (
                f"{script.name}: routing_status="
                f"{entry.get('routing_status')!r} (expected "
                f"'unresolved' — autoplay clip with empty "
                f"observed_attribute (D12) / serialized-ref scope (D11); "
                f"intended-permanent server fallback)"
            )
            assert script.script_type == "Script", (
                f"{script.name}: script_type={script.script_type!r} "
                f"(expected 'Script' — unresolved driver → server fallback)"
            )
            assert script.parent_path == "ServerScriptService", (
                f"{script.name}: parent_path={script.parent_path!r} "
                f"(expected 'ServerScriptService')"
            )

    # ------------------------------------------------------------------
    # Assertion 6: no duplicate Anim_* names. Invariant 3 in
    # build_topology keys on stable_id; the topology authority makes
    # duplicate emissions structurally impossible.
    # ------------------------------------------------------------------
    anim_names = [
        s.name for s in rbx_place.scripts
        if s.name and s.name.startswith("Anim_")
    ]
    duplicates = sorted({n for n in anim_names if anim_names.count(n) > 1})
    assert not duplicates, (
        f"duplicate Anim_* names in rbx_place.scripts: {duplicates} — "
        f"invariant 3 in build_topology._enforce_invariants should make "
        f"this structurally impossible"
    )


# ===========================================================================
# Supplemental: observed_target derivation kinds (small, but documents
# the contract that drives the integration test's child-vs-descendant
# placement decisions).
# ===========================================================================


class TestDeriveObservedTarget:
    """Three kinds of observed_target per animation_routing.py:200-222."""

    def test_empty_paths_is_self(self) -> None:
        target: AnimationObservedTarget = derive_observed_target(
            [""], prefab_scoped=True,
        )
        assert target["kind"] == "self"
        assert target["scope"] == "self.gameObject"

    def test_single_simple_path_is_child(self) -> None:
        target = derive_observed_target(
            ["door"], prefab_scoped=True,
        )
        assert target["kind"] == "child"
        assert target["name"] == "door"

    def test_slashed_path_is_descendant(self) -> None:
        target = derive_observed_target(
            ["body/door/hinge"], prefab_scoped=False,
        )
        assert target["kind"] == "descendant"
        assert target["scope"] == "workspace"


# ===========================================================================
# Supplemental: build_animation_driver_entry round-trip + invariants
# 1+6 happy path (the resolved branch the door test covers but with the
# direct call surface, so regressions in the helper are caught even when
# build_topology's coordinator changes).
# ===========================================================================


class TestBuildAnimationDriverEntry:
    """Exercises ``build_animation_driver_entry`` directly.

    Refs: animation_routing.py:359-403.
    """

    def test_client_driver_yields_local_script(self) -> None:
        entry = build_animation_driver_entry(
            stable_id="Door::open::open",
            routing_status=cast(AnimationRoutingStatus, "resolved"),
            driver_module_guid="guid-door",
            domain=cast(AnimationDomain, "client"),
            observed_attribute="open",
            observed_target={
                "kind": "child", "name": "door", "scope": "self.gameObject",
            },
        )
        assert entry["script_class"] == "LocalScript"
        assert entry["domain"] == "client"
        assert entry["lifecycle_role"] == "auto_run"
        assert entry["bridge_group_id"] is None

    def test_server_driver_yields_script(self) -> None:
        entry = build_animation_driver_entry(
            stable_id="Boss::__none__::roar",
            routing_status=cast(AnimationRoutingStatus, "resolved"),
            driver_module_guid="guid-boss",
            domain=cast(AnimationDomain, "server"),
            observed_attribute="roar",
            observed_target={
                "kind": "self", "name": "", "scope": "self.gameObject",
            },
        )
        assert entry["script_class"] == "Script"
        assert entry["domain"] == "server"


# ===========================================================================
# CATEGORY 7 (added by post-slice-10 review): Slice 9 consumer wiring.
#
# These tests prove the load-bearing claim "topology owns RbxScript
# metadata" by driving Pipeline._build_and_apply_topology directly and
# asserting:
#   - resolved client driver flips RbxScript.script_type Script
#     → LocalScript + stamps parent_path = StarterPlayer.StarterPlayerScripts
#   - the storage_plan buckets are patched in lockstep (move from
#     server_scripts → client_scripts) so the on-disk plan can't drift
#     from the live RbxScript metadata (codex F1 fix)
#   - resolved server driver stamps parent_path = ServerScriptService
#     and leaves plan buckets unchanged
#   - unresolved + orphan rows preserve today's server placement
#     (routing_status field is the audit trail; invariants skip them)
#   - mismatched script_name (animation_drivers row has no live
#     RbxScript) increments the "unmatched" counter, not silently
#     skipped (codex F4)
#
# These exercise the actual pipeline subphase that Phase 1's Door fix
# depends on — distinct from the skipped SimpleFPS e2e stub at line
# 841 (which needs a full Unity conversion to PROVE Door's
# end-to-end flow at runtime).
# ===========================================================================


# ===========================================================================
# CATEGORY 7: caller_graph curation (Phase 2a slice 3)
# ===========================================================================

class TestCallerGraphCuration:
    """``_build_caller_graph_block`` + the ``callers_of`` accessor.

    Refs: build_topology.py:_build_caller_graph_block, callers_of;
    Phase 2a slice 3 in the design doc.
    """

    def test_empty_dependency_map_emits_empty_graph(self) -> None:
        """``dependency_map=None`` and ``dependency_map={}`` both produce
        an empty ``caller_graph`` — back-compat for Phase 1 callers and
        legacy-mode invocations."""
        sr = _mk_artifact(modules={
            "guid-foo": _mk_module("Foo", "client"),
        })
        artifact_none = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map=None,
        )
        artifact_empty = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map={},
        )
        assert artifact_none["caller_graph"] == {}
        assert artifact_empty["caller_graph"] == {}

    def test_inverts_outgoing_to_incoming_edges(self) -> None:
        """Planner's outgoing form ``{Foo: [Bar]}`` (Foo requires Bar)
        curates to incoming form ``{guid-bar: [guid-foo]}`` (Bar is
        required-by Foo). Translation by class_name → script_id index
        from ``scene_runtime.modules``."""
        sr = _mk_artifact(modules={
            "guid-foo": _mk_module("Foo", "client"),
            "guid-bar": _mk_module("Bar", "client"),
        })
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map={"Foo": ["Bar"]},
        )
        graph = artifact["caller_graph"]
        assert graph == {"guid-bar": ["guid-foo"]}

    def test_multiple_callers_aggregate_under_one_callee(self) -> None:
        """Two callers requiring the same callee land in the callee's
        list. Duplicates from a caller requiring the same callee twice
        are deduplicated."""
        sr = _mk_artifact(modules={
            "guid-foo": _mk_module("Foo", "client"),
            "guid-baz": _mk_module("Baz", "client"),
            "guid-bar": _mk_module("Bar", "client"),
        })
        # Both Foo and Baz require Bar; Foo also lists Bar twice.
        dep_map = {"Foo": ["Bar", "Bar"], "Baz": ["Bar"]}
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map=dep_map,
        )
        callers = sorted(artifact["caller_graph"]["guid-bar"])
        assert callers == ["guid-baz", "guid-foo"]

    def test_unknown_class_in_dep_map_skipped(self) -> None:
        """A class in dependency_map that has no corresponding
        ``scene_runtime.modules`` row is skipped (no topology row to
        reference). Skips both caller-side (caller not in modules) and
        callee-side (callee not in modules)."""
        sr = _mk_artifact(modules={
            "guid-foo": _mk_module("Foo", "client"),
        })
        # Foo→External: callee not in modules; skipped.
        # External→Foo: caller not in modules; skipped.
        dep_map = {"Foo": ["External"], "External": ["Foo"]}
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map=dep_map,
        )
        # Neither edge survived translation → empty graph.
        assert artifact["caller_graph"] == {}

    def test_callers_of_helper_returns_list_for_known_id(self) -> None:
        """``callers_of(script_id, caller_graph)`` returns the caller
        list for a known script_id. Empty list for unknown id (orphan
        module — design doc decision tree treats no-callers same as
        absent from graph)."""
        graph = {"guid-bar": ["guid-foo", "guid-baz"]}
        assert callers_of("guid-bar", graph) == ["guid-foo", "guid-baz"]
        assert callers_of("guid-unknown", graph) == []

    def test_callers_of_returns_fresh_list(self) -> None:
        """``callers_of`` returns a fresh list so a consumer can mutate
        without affecting the artifact."""
        graph = {"guid-bar": ["guid-foo"]}
        result = callers_of("guid-bar", graph)
        result.append("guid-mutated")
        # Original graph unchanged.
        assert graph["guid-bar"] == ["guid-foo"]

    def test_class_name_collision_excluded_from_caller_graph(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Slice 3 round 2 (codex P1 / Claude P1) degraded-service
        contract: when two modules share a class_name AND that name
        appears in dependency_map, the colliding class is EXCLUDED
        from caller_graph translation (rather than aborting the
        build). Both caller-side and callee-side appearances are
        skipped. The colliding scripts appear as orphan rows (no
        callers) in the curated view; slice 5's decision tree
        falls back to ReplicatedStorage. A warning is logged per
        collision.

        Refs: build_topology._detect_caller_graph_collisions;
        Phase 2a slice 3 round 2 review.
        """
        import logging
        sr = _mk_artifact(modules={
            "guid-first": _mk_module("Utils", "client"),
            "guid-second": _mk_module(
                "Utils", "client", class_name="Utils",
            ),
            "guid-caller": _mk_module("Caller", "client"),
            "guid-target": _mk_module("Target", "client"),
        })
        caplog.set_level(
            logging.WARNING,
            logger="converter.scene_runtime_topology.build_topology",
        )
        # Caller→Utils (collision touch) + Caller→Target (no collision).
        # Build should succeed; only the non-colliding edge should land
        # in the graph.
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map={"Caller": ["Utils", "Target"]},
        )
        # Utils collision excluded; Target edge preserved.
        assert artifact["caller_graph"] == {
            "guid-target": ["guid-caller"],
        }
        # Warning logged with the offending class_name + remediation.
        warnings = [
            r for r in caplog.records
            if r.levelno >= logging.WARNING
        ]
        assert any("Utils" in r.getMessage() for r in warnings), (
            f"expected warning naming the colliding class: {warnings!r}"
        )

    def test_class_name_collision_as_caller_also_excluded(
        self,
    ) -> None:
        """The exclusion applies symmetrically: a colliding class as
        the CALLER side of dependency_map is also skipped (we can't
        determine which Utils script_id authored the edge)."""
        sr = _mk_artifact(modules={
            "guid-first": _mk_module("Utils", "client"),
            "guid-second": _mk_module(
                "Utils", "client", class_name="Utils",
            ),
            "guid-target": _mk_module("Target", "client"),
        })
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map={"Utils": ["Target"]},
        )
        # No edges in caller_graph — Utils caller is excluded.
        assert artifact["caller_graph"] == {}

    def test_collision_not_in_dep_map_is_harmless(self) -> None:
        """Collision that doesn't affect dependency_map is harmless
        and produces no warning + no exclusion — the lossy translation
        only matters when the colliding name is actually used.

        Slice 3 round 2 refinement of the round 1 invariant 9 test.
        """
        sr = _mk_artifact(modules={
            "guid-first": _mk_module("Utils", "client"),
            "guid-second": _mk_module(
                "Utils", "client", class_name="Utils",
            ),
            "guid-caller": _mk_module("Caller", "client"),
            "guid-target": _mk_module("Target", "client"),
        })
        # `Utils` colliding but not in dep_map — no exclusion.
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map={"Caller": ["Target"]},
        )
        assert artifact["caller_graph"] == {
            "guid-target": ["guid-caller"],
        }

    def test_helper_module_included_in_caller_graph(self) -> None:
        """Slice 3 round 1 contract: non-runtime-bearing helpers ARE
        included in caller_graph (keys and values). A helper required
        by a runtime-bearing client script is a real edge slice 5's
        decision tree needs (the helper's domain stays "helper" but
        its placement is still informed by who requires it)."""
        sr = _mk_artifact(modules={
            "guid-caller": _mk_module("Caller", "client"),
            "guid-helper": {
                "stem": "Helper", "class_name": "Helper",
                "runtime_bearing": False,
                # Non-runtime-bearing rows are exempt from invariant 7;
                # they CAN omit character_attached / is_loader.
            },
        })
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={},
            dependency_map={"Caller": ["Helper"]},
        )
        # The helper is keyed in caller_graph despite runtime_bearing=False.
        assert artifact["caller_graph"] == {
            "guid-helper": ["guid-caller"],
        }


class TestApplyTopologyToRbxScripts:
    """Drive ``Pipeline._build_and_apply_topology`` against synthetic
    fixtures + assert the RbxScript mutations + plan-bucket patches
    that the design doc + codex review require.
    """

    @staticmethod
    def _mk_pipeline(
        *,
        scripts: list[RbxScript],
        emitted_animations: list[EmittedAnimation],
        tmp_path: Path,
    ):
        """Build the minimum Pipeline + state shape ``_build_and_apply_topology``
        reads. We avoid invoking the heavy ``__init__`` (which expects a
        real Unity project + output dir) by constructing in place via
        ``__new__`` + setting only the attributes the method touches.
        """
        from converter.pipeline import Pipeline
        from core.roblox_types import RbxPlace
        from converter.animation_converter import AnimationConversionResult

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        # The method reads: self.state.rbx_place.scripts +
        # self.state.animation_result.emitted_animations +
        # self.state.guid_index. ``state`` is a SimpleNamespace-shaped
        # bag in the real Pipeline; here we forge it with the same
        # attribute access pattern.
        from types import SimpleNamespace
        rbx_place = RbxPlace()
        rbx_place.scripts = scripts
        anim_result = AnimationConversionResult()
        anim_result.emitted_animations = emitted_animations
        from converter.code_transpiler import TranspilationResult
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,
            guid_index=None,
            # Phase 2a slice 3 added `dependency_map` to the
            # _build_and_apply_topology read set so build_topology can
            # curate the caller_graph. Real PipelineState defaults to
            # an empty dict (pipeline.py:126); mirror that here so the
            # SimpleNamespace forge matches the prod shape.
            dependency_map={},
            # Slice 3 round 6 (codex P2): the caller_graph
            # preservation signal switched from dep_map truthiness to
            # ``transpilation_result is not None``. Populate it so
            # these tests' fresh-build path runs as expected.
            transpilation_result=TranspilationResult(),
        )
        return pipeline

    @staticmethod
    def _mk_plan(*, server_scripts: list[str] | None = None) -> "object":
        """Return a fresh StoragePlan with the given server bucket.

        Animation scripts default to server_scripts (today's
        classify_storage behaviour for Anim_* names). The test then
        asserts topology moves them to client_scripts.
        """
        from converter.storage_classifier import StoragePlan
        return StoragePlan(
            server_scripts=list(server_scripts or []),
        )

    def test_resolved_client_driver_flips_script_to_localscript_and_updates_plan(
        self, tmp_path: Path,
    ) -> None:
        """The Door fix: client-driven Animator → Anim_* becomes
        LocalScript in StarterPlayerScripts, AND the plan buckets move.

        Mirrors the doc's Phase 1 §Testing integration assertion (lines
        518-523) but at the unit level without needing a full Unity
        conversion.
        """
        artifact, prefab_id, _ = _door_shape_artifact(door_domain="client")
        scene_runtime = cast("dict[str, object]", artifact)

        emission: EmittedAnimation = {
            "scope_kind": "prefab",
            "scope_ref": prefab_id,
            "scope_display": "Door",
            "ctrl_key": "door",
            "clip_disp": "open",
            "script_name": "Anim_Door_door_open",
            "observed_attribute": "open",
            "curve_paths": ["door"],
            "prefab_scoped": True,
        }
        anim_script = _mk_rbx_script("Anim_Door_door_open", "Script")
        # Some unrelated server script in the plan to verify the patch
        # is targeted (doesn't accidentally clear the bucket).
        other_server = _mk_rbx_script("GameManager", "Script")
        plan = self._mk_plan(server_scripts=[
            "Anim_Door_door_open", "GameManager",
        ])

        pipeline = self._mk_pipeline(
            scripts=[anim_script, other_server],
            emitted_animations=[emission],
            tmp_path=tmp_path,
        )
        pipeline._build_and_apply_topology(scene_runtime, plan)

        # F1 + the Door fix: in-memory RbxScript flipped.
        assert anim_script.script_type == "LocalScript"
        assert getattr(anim_script, "parent_path", "") == (
            "StarterPlayer.StarterPlayerScripts"
        )
        # Unrelated server script untouched.
        assert other_server.script_type == "Script"
        # F1: plan buckets patched in lockstep.
        assert "Anim_Door_door_open" not in plan.server_scripts
        assert "Anim_Door_door_open" in plan.client_scripts
        assert "GameManager" in plan.server_scripts  # unchanged
        # Audit trail recorded with the same shape classify_storage
        # writes (script / script_type / container / reason +
        # ``source`` discriminator). Forward-compat: a downstream
        # consumer iterating ``plan.decisions`` can index the
        # canonical 4 keys uniformly across both sources.
        moves = [d for d in plan.decisions if d.get("script") == "Anim_Door_door_open"]
        assert len(moves) == 1
        assert moves[0]["script_type"] == "LocalScript"
        assert moves[0]["container"] == "StarterPlayer.StarterPlayerScripts"
        assert moves[0]["source"] == "topology"
        assert "topology" in moves[0]["reason"]
        # The persisted artifact lands at scene_runtime["topology"].
        assert "topology" in scene_runtime
        topology = cast("dict[str, object]", scene_runtime["topology"])
        assert "animation_drivers" in topology

    def test_resolved_server_driver_stamps_parent_path_without_bucket_move(
        self, tmp_path: Path,
    ) -> None:
        """Server-driven Animator → Anim_* stays Script in
        ServerScriptService; plan buckets unchanged.

        Confirms the "explicit server stamp" path doesn't accidentally
        flip into client_scripts.
        """
        artifact, prefab_id, _ = _door_shape_artifact(door_domain="server")
        scene_runtime = cast("dict[str, object]", artifact)
        emission: EmittedAnimation = {
            "scope_kind": "prefab",
            "scope_ref": prefab_id,
            "scope_display": "Door",
            "ctrl_key": "door",
            "clip_disp": "open",
            "script_name": "Anim_Door_door_open",
            "observed_attribute": "open",
            "curve_paths": ["door"],
            "prefab_scoped": True,
        }
        anim_script = _mk_rbx_script("Anim_Door_door_open", "Script")
        plan = self._mk_plan(server_scripts=["Anim_Door_door_open"])
        pipeline = self._mk_pipeline(
            scripts=[anim_script],
            emitted_animations=[emission],
            tmp_path=tmp_path,
        )
        pipeline._build_and_apply_topology(scene_runtime, plan)

        assert anim_script.script_type == "Script"
        assert getattr(anim_script, "parent_path", "") == "ServerScriptService"
        # No bucket move for server-resolved.
        assert "Anim_Door_door_open" in plan.server_scripts
        assert "Anim_Door_door_open" not in plan.client_scripts

    def test_unresolved_row_preserves_server_placement_no_bucket_move(
        self, tmp_path: Path,
    ) -> None:
        """No driver in scope → routing_status="unresolved" → RbxScript
        keeps today's Script/ServerScriptService default; plan buckets
        unchanged. The audit trail lives in scene_runtime.topology
        (the routing_status field is visible in the artifact).
        """
        # Scope with zero Animator-typed refs → resolve_driver returns
        # None → routing_status="unresolved".
        artifact = _mk_artifact(
            modules={"guid-other": _mk_module("Other", "client")},
            prefabs={
                "guid-other-prefab:Assets/Prefabs/Other.prefab": {
                    "name": "Other",
                    "template_name": "Other",
                    "instances": [],
                    "references": [],
                    "lifecycle_order": [],
                },
            },
        )
        scene_runtime = cast("dict[str, object]", artifact)
        emission: EmittedAnimation = {
            "scope_kind": "prefab",
            "scope_ref": "guid-other-prefab:Assets/Prefabs/Other.prefab",
            "scope_display": "Other",
            "ctrl_key": "ctrl",
            "clip_disp": "wave",
            "script_name": "Anim_Other_ctrl_wave",
            "observed_attribute": "",
            "curve_paths": ["arm"],
            "prefab_scoped": True,
        }
        anim_script = _mk_rbx_script("Anim_Other_ctrl_wave", "Script")
        plan = self._mk_plan(server_scripts=["Anim_Other_ctrl_wave"])
        pipeline = self._mk_pipeline(
            scripts=[anim_script],
            emitted_animations=[emission],
            tmp_path=tmp_path,
        )
        pipeline._build_and_apply_topology(scene_runtime, plan)

        assert anim_script.script_type == "Script"  # unchanged
        assert "Anim_Other_ctrl_wave" in plan.server_scripts  # unchanged
        assert "Anim_Other_ctrl_wave" not in plan.client_scripts
        # Audit visibility: the artifact still records the row + status.
        topology = cast("dict[str, object]", scene_runtime["topology"])
        drivers = cast("dict[str, dict[str, object]]", topology["animation_drivers"])
        sid = compute_stable_id(
            "guid-other-prefab:Assets/Prefabs/Other.prefab",
            "ctrl", "wave",
        )
        assert sid in drivers
        assert drivers[sid]["routing_status"] == "unresolved"

    def test_orphan_row_preserves_server_placement(
        self, tmp_path: Path,
    ) -> None:
        """Project-wide orphan clip → routing_status="orphan" → RbxScript
        keeps Script/ServerScriptService; plan buckets unchanged.
        """
        artifact = _mk_artifact()
        scene_runtime = cast("dict[str, object]", artifact)
        emission: EmittedAnimation = {
            "scope_kind": "orphan",
            "scope_ref": "",
            "scope_display": ORPHAN_SCOPE,
            "ctrl_key": "",
            "clip_disp": "FloatingClip",
            "script_name": "Anim_FloatingClip",
            "observed_attribute": "",
            "curve_paths": [""],
            "prefab_scoped": False,
        }
        anim_script = _mk_rbx_script("Anim_FloatingClip", "Script")
        plan = self._mk_plan(server_scripts=["Anim_FloatingClip"])
        pipeline = self._mk_pipeline(
            scripts=[anim_script],
            emitted_animations=[emission],
            tmp_path=tmp_path,
        )
        pipeline._build_and_apply_topology(scene_runtime, plan)

        assert anim_script.script_type == "Script"
        assert "Anim_FloatingClip" in plan.server_scripts
        # Routing status visible in the artifact.
        topology = cast("dict[str, object]", scene_runtime["topology"])
        drivers = cast("dict[str, dict[str, object]]", topology["animation_drivers"])
        sid = compute_stable_id(ORPHAN_SCOPE, None, "FloatingClip")
        assert drivers[sid]["routing_status"] == "orphan"

    def test_unmatched_script_name_increments_counter_and_warns(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Codex F4 fix: when animation_drivers has a row but the
        named RbxScript isn't in rbx_place (consumer drift),
        ``_build_and_apply_topology`` LOGS A WARNING per row + records
        it in the summary log — no silent skip.
        """
        artifact, prefab_id, _ = _door_shape_artifact(door_domain="client")
        scene_runtime = cast("dict[str, object]", artifact)
        emission: EmittedAnimation = {
            "scope_kind": "prefab",
            "scope_ref": prefab_id,
            "scope_display": "Door",
            "ctrl_key": "door",
            "clip_disp": "open",
            "script_name": "Anim_Door_door_open",
            "observed_attribute": "open",
            "curve_paths": ["door"],
            "prefab_scoped": True,
        }
        # NO RbxScript with this name in rbx_place — simulates drift.
        plan = self._mk_plan()
        pipeline = self._mk_pipeline(
            scripts=[],
            emitted_animations=[emission],
            tmp_path=tmp_path,
        )

        import logging
        with caplog.at_level(logging.INFO, logger="converter.pipeline"):
            pipeline._build_and_apply_topology(scene_runtime, plan)

        warning_lines = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        # The per-row warning fires for the unmatched stable_id.
        assert any(
            "Anim_Door_door_open" in m and "no matching RbxScript" in m
            for m in warning_lines
        ), warning_lines
        # The summary log includes the unmatched count (info level).
        info_lines = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "unmatched" in m and "1 unmatched" in m for m in info_lines
        ) or any(
            "1 unmatched" in m for m in caplog.text.splitlines()
        ), caplog.text

    def test_empty_emitted_animations_still_builds_topology(
        self, tmp_path: Path,
    ) -> None:
        """Slice 3 round 1 review (codex P2): when
        ``animation_result.emitted_animations`` is empty, the method
        STILL builds the topology artifact — caller_graph (slice 3),
        modules block, and cross_domain_edges are independent of
        animations. Pre-slice-3-round-1 the method short-circuited
        here, which silently dropped the caller_graph for any
        no-animation project.

        Refs: pipeline.py:_build_and_apply_topology slice 3 round 1
        fix; design doc Phase 2a slice 3.
        """
        artifact = _mk_artifact()
        scene_runtime = cast("dict[str, object]", artifact)
        plan = self._mk_plan()
        pipeline = self._mk_pipeline(
            scripts=[_mk_rbx_script("X", "Script")],
            emitted_animations=[],
            tmp_path=tmp_path,
        )
        pipeline._build_and_apply_topology(scene_runtime, plan)
        # Topology block IS now written (with empty animation_drivers
        # + caller_graph). scene_runtime has the empty modules
        # scenario from _mk_artifact() so all blocks are empty too,
        # but the KEY exists.
        assert "topology" in scene_runtime
        topo = cast(dict, scene_runtime["topology"])
        assert topo.get("animation_drivers") == {}
        assert topo.get("modules") == {}
        assert topo.get("caller_graph") == {}

    def test_resume_with_animation_result_none_preserves_animation_drivers(
        self, tmp_path: Path,
    ) -> None:
        """Slice 3 round 4 (Claude P1.A/P1.B): on resume where
        convert_animations didn't run, ``state.animation_result``
        is None and the persisted ``scene_runtime.topology`` block
        carries animation_drivers from a prior conversion. The
        round-4 fix preserves the animation_drivers block verbatim
        while REBUILDING modules + caller_graph + cross_domain_edges
        from current state — avoids both (1) silent erasure of prior
        drivers (round 3 P1) and (2) stale caller_graph alongside
        fresh dependency_map (round 4 P1.B).

        Refs: pipeline.py:_build_and_apply_topology round 4 fix;
        build_topology.py ``preserved_animation_drivers`` contract.
        """
        from types import SimpleNamespace
        from converter.pipeline import Pipeline
        from core.roblox_types import RbxPlace
        artifact = _mk_artifact(modules={
            "guid-foo": _mk_module("Foo", "client"),
        })
        scene_runtime = cast("dict[str, object]", artifact)
        # Pre-existing topology block from a prior conversion run.
        prior_animation_drivers = {
            "guid-prior:Door:open": {
                "stable_id": "guid-prior:Door:open",
                "routing_status": "resolved",
                "driver_module_guid": "guid-prior-door",
                "domain": "client",
                "script_class": "LocalScript",
                "lifecycle_role": "auto_run",
                "observed_attribute": "Open",
                "observed_target": {
                    "kind": "self", "name": "", "scope": "workspace",
                },
                "bridge_group_id": None,
            },
        }
        prior_topology = {
            "modules": {},  # stale — should get rebuilt
            "animation_drivers": prior_animation_drivers,
            "cross_domain_edges": [],
            "caller_graph": {},  # stale — should get rebuilt
        }
        # The prior topology MUST include a module entry for the
        # driver_module_guid `guid-prior-door` — invariant 1 will fire
        # otherwise. For the resume test, add the prior driver module
        # to scene_runtime.modules so the freshly-built modules_block
        # includes it.
        scene_runtime["modules"]["guid-prior-door"] = _mk_module(  # type: ignore[index]
            "PriorDoor", "client",
        )
        scene_runtime["topology"] = prior_topology

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        rbx_place.scripts = [_mk_rbx_script("X", "Script")]
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=None,  # <-- the resume signature
            guid_index=None,
            dependency_map={},
            transpilation_result=None,  # transpile didn't run on resume
        )

        plan = TestApplyTopologyToRbxScripts._mk_plan()
        pipeline._build_and_apply_topology(scene_runtime, plan)

        # Topology block IS rebuilt (not preserved as-is)…
        rebuilt = scene_runtime["topology"]
        assert rebuilt is not prior_topology
        # …and modules block reflects CURRENT scene_runtime (has Foo + PriorDoor)
        assert "guid-foo" in rebuilt["modules"]  # type: ignore[index]
        assert "guid-prior-door" in rebuilt["modules"]  # type: ignore[index]
        # …animation_drivers preserved verbatim from prior topology
        assert (
            rebuilt["animation_drivers"]  # type: ignore[index]
            == prior_animation_drivers
        )

    def test_retranspile_with_no_edges_overwrites_prior_caller_graph(
        self, tmp_path: Path,
    ) -> None:
        """Slice 3 round 6 (codex P2): a GENUINE retranspile that
        removed the last cross-script reference ALSO leaves
        ``state.dependency_map`` empty. Pre-round-6 my dep_map
        truthiness signal would silently preserve the prior populated
        caller_graph in that case — carrying forward stale callers
        that no longer exist in the current source.

        Round 6 fix: use ``state.transpilation_result is not None``
        as the "did transpile run this invocation?" signal instead.
        Both transpilation_result + dependency_map are set in the
        same code path (pipeline.py:1942-1971); a populated
        transpilation_result with empty dep_map is the legitimate
        "ran with no edges" case — emit empty caller_graph fresh.
        """
        from types import SimpleNamespace
        from converter.pipeline import Pipeline
        from converter.animation_converter import AnimationConversionResult
        from converter.code_transpiler import TranspilationResult
        from core.roblox_types import RbxPlace
        artifact = _mk_artifact(modules={
            "guid-foo": _mk_module("Foo", "client"),
        })
        scene_runtime = cast("dict[str, object]", artifact)
        # Prior conversion produced a populated caller_graph.
        prior_caller_graph = {"guid-stale": ["guid-stale-caller"]}
        prior_topology = {
            "modules": {},
            "animation_drivers": {},
            "cross_domain_edges": [],
            "caller_graph": prior_caller_graph,
        }
        scene_runtime["topology"] = prior_topology

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        rbx_place.scripts = [_mk_rbx_script("Foo", "LocalScript")]
        anim_result = AnimationConversionResult()
        anim_result.emitted_animations = []
        # Key: transpilation_result IS populated → transpile ran
        # this invocation. dep_map is empty because no cross-script
        # references exist in current source.
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,
            guid_index=None,
            dependency_map={},
            transpilation_result=TranspilationResult(),
        )

        plan = TestApplyTopologyToRbxScripts._mk_plan()
        pipeline._build_and_apply_topology(scene_runtime, plan)

        # Empty caller_graph (no stale carry-forward).
        topo = scene_runtime["topology"]
        assert topo["caller_graph"] == {}  # type: ignore[index]

    def test_invariant_1_catches_stale_preserved_anim_driver_domain(
        self,
    ) -> None:
        """Slice 3 round 6 (codex P1): preserved animation_drivers
        copy entries verbatim while modules_block is rebuilt fresh.
        If ``domain_overrides`` / ``networking_mode`` changed between
        runs, a driver module's domain shifts but the preserved
        animation driver row keeps the old domain. Invariant 1's
        equality check (re-added in round 6) catches the
        self-contradictory pair and aborts with a clear remediation
        message.

        Refs: build_topology.py invariant 1 equality re-add;
        Phase 2a slice 3 round 6 review.
        """
        # Modules block says driver is server-domain.
        sr = _mk_artifact(modules={
            "guid-driver": _mk_module("Driver", "server"),
        })
        scripts_by_class = {
            "Driver": _mk_rbx_script("Driver", "Script"),
        }
        # Preserved animation_drivers row says client (stale).
        stale_drivers = {
            "scope:Driver:open": {
                "stable_id": "scope:Driver:open",
                "routing_status": "resolved",
                "driver_module_guid": "guid-driver",
                "domain": "client",  # stale
                "script_class": "LocalScript",  # stale
                "lifecycle_role": "auto_run",
                "observed_attribute": "open",
                "observed_target": {
                    "kind": "self", "name": "", "scope": "workspace",
                },
                "bridge_group_id": None,
            },
        }
        with pytest.raises(TopologyInvariantError) as excinfo:
            build_topology(
                scene_runtime=sr,
                emitted_animations=[],
                scripts_by_class=scripts_by_class,
                preserved_animation_drivers=stale_drivers,
            )
        msg = str(excinfo.value)
        assert "invariant 1" in msg
        assert "Re-run from convert_animations" in msg

    def test_assemble_no_retranspile_preserves_caller_graph(
        self, tmp_path: Path,
    ) -> None:
        """Slice 3 round 5 (codex P2): when ``transpile_scripts`` is
        skipped (assemble-without-retranspile workflow), the planner's
        ``state.dependency_map`` stays empty for legitimate reasons
        (the scripts cache is intact; no new edges to learn). Pre-
        round-5 the empty dep_map caused build_topology to emit
        ``caller_graph={}`` and silently overwrite the prior populated
        graph in ``scene_runtime.topology``.

        Round 5 fix: detect the empty-dep_map + prior-caller_graph
        case and pass the prior block via
        ``preserved_caller_graph``; build_topology uses it verbatim.

        Refs: pipeline.py:_build_and_apply_topology round 5 fix;
        build_topology.py ``preserved_caller_graph`` contract.
        """
        from types import SimpleNamespace
        from converter.pipeline import Pipeline
        from converter.animation_converter import AnimationConversionResult
        from core.roblox_types import RbxPlace
        artifact = _mk_artifact(modules={
            "guid-foo": _mk_module("Foo", "client"),
        })
        scene_runtime = cast("dict[str, object]", artifact)
        # Prior conversion produced a populated caller_graph.
        prior_caller_graph = {"guid-target": ["guid-caller"]}
        prior_topology = {
            "modules": {},
            "animation_drivers": {},
            "cross_domain_edges": [],
            "caller_graph": prior_caller_graph,
        }
        scene_runtime["topology"] = prior_topology

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        rbx_place.scripts = [_mk_rbx_script("Foo", "LocalScript")]
        anim_result = AnimationConversionResult()
        anim_result.emitted_animations = []
        # transpilation_result=None: transpile_scripts didn't run
        # this invocation (assemble-no-retranspile signature).
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,
            guid_index=None,
            dependency_map={},
            transpilation_result=None,
        )

        plan = TestApplyTopologyToRbxScripts._mk_plan()
        pipeline._build_and_apply_topology(scene_runtime, plan)

        # Topology rebuilt; caller_graph preserved from prior.
        topo = scene_runtime["topology"]
        assert topo["caller_graph"] == prior_caller_graph  # type: ignore[index]

    def test_fresh_no_animations_still_builds_caller_graph(
        self, tmp_path: Path,
    ) -> None:
        """Slice 3 round 4 — fresh conversion with no animations
        (animation_result populated but emitted_animations==[]).
        Pre-round-4 the `animation_result is None` short-circuit
        also fired here in some code paths, regressing slice 3
        round 1's "topology always built" goal. Round 4 distinguishes
        by checking animation_result identity vs emission contents.

        Verifies: caller_graph IS built; topology block IS written;
        animation_drivers is empty (no fresh emissions).
        """
        from types import SimpleNamespace
        from converter.pipeline import Pipeline
        from converter.animation_converter import AnimationConversionResult
        from core.roblox_types import RbxPlace
        artifact = _mk_artifact(modules={
            "guid-caller": _mk_module("Caller", "client"),
            "guid-target": _mk_module("Target", "client"),
        })
        scene_runtime = cast("dict[str, object]", artifact)

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        rbx_place.scripts = [_mk_rbx_script("Caller", "LocalScript")]
        anim_result = AnimationConversionResult()
        anim_result.emitted_animations = []  # populated but empty
        from converter.code_transpiler import TranspilationResult
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,  # <-- populated, not None
            guid_index=None,
            dependency_map={"Caller": ["Target"]},
            transpilation_result=TranspilationResult(),
        )

        plan = TestApplyTopologyToRbxScripts._mk_plan()
        pipeline._build_and_apply_topology(scene_runtime, plan)

        # Topology block written, caller_graph populated, animation_drivers empty.
        topo = scene_runtime["topology"]
        assert topo["caller_graph"] == {"guid-target": ["guid-caller"]}  # type: ignore[index]
        assert topo["animation_drivers"] == {}  # type: ignore[index]


# ===========================================================================
# Phase 2a slice 5 round 2: immutable intrinsic_script_type tests
# ===========================================================================


class TestIntrinsicScriptTypeRoundTwoContract:
    """Round 2 deliverable: ``RbxScript.intrinsic_script_type`` is the
    immutable transpile-time class signal, and the topology artifact's
    ``script_class`` reflects that immutable field rather than the
    mutable ``script_type`` that ``classify_storage`` reassigns.

    These tests are the behavior witnesses the synthesis required.
    """

    def test_transpiler_stamps_intrinsic_field_on_emit(
        self, tmp_path: Path,
    ) -> None:
        """ROUND 2 — Test B (transpiler stamp).

        ``_subphase_emit_scripts_to_disk`` is the canonical
        ``RbxScript`` construction site for transpiled scripts. It
        consumes ``TranspilationResult.scripts[*].script_type`` (the
        output of ``code_transpiler._classify_script_type``) and MUST
        stamp the same value into the new immutable
        ``intrinsic_script_type`` field at construction time.

        After the emit subphase runs, every RbxScript produced from
        the transpilation result carries a non-None
        ``intrinsic_script_type`` that equals the original
        ``ts.script_type`` — even before classify_storage gets a chance
        to mutate the live ``script_type``.
        """
        from converter.pipeline import Pipeline
        from converter.code_transpiler import (
            TranspilationResult,
            TranspiledScript,
        )
        from core.roblox_types import RbxPlace

        unity_project = tmp_path / "unity"
        (unity_project / "Assets").mkdir(parents=True)
        output = tmp_path / "out"
        output.mkdir()

        pipeline = Pipeline(str(unity_project), str(output))
        pipeline.state.rbx_place = RbxPlace()
        pipeline.state.transpilation_result = TranspilationResult(
            scripts=[
                TranspiledScript(
                    source_path="Assets/Foo.cs",
                    output_filename="Foo.luau",
                    csharp_source="// stub",
                    luau_source="-- Foo\nprint('foo')\n",
                    strategy="rule_based",
                    confidence=1.0,
                    script_type="Script",
                ),
                TranspiledScript(
                    source_path="Assets/Bar.cs",
                    output_filename="Bar.luau",
                    csharp_source="// stub",
                    luau_source="-- Bar\nlocal M = {}\nreturn M\n",
                    strategy="rule_based",
                    confidence=1.0,
                    script_type="ModuleScript",
                ),
            ],
            total_transpiled=2,
            total_rule_based=2,
        )

        pipeline._subphase_emit_scripts_to_disk()

        by_name = {s.name: s for s in pipeline.state.rbx_place.scripts}
        assert "Foo" in by_name and "Bar" in by_name

        # Both fields agree IMMEDIATELY post-transpile (no classifier
        # has run yet).
        assert by_name["Foo"].script_type == "Script"
        assert by_name["Foo"].intrinsic_script_type == "Script"
        assert by_name["Bar"].script_type == "ModuleScript"
        assert by_name["Bar"].intrinsic_script_type == "ModuleScript"

        # The contract: mutating script_type post-construction (the way
        # classify_storage does) MUST NOT change intrinsic_script_type.
        by_name["Foo"].script_type = "LocalScript"  # simulate coercion
        assert by_name["Foo"].intrinsic_script_type == "Script", (
            "intrinsic_script_type must be immutable across post-"
            "construction mutations of script_type"
        )

    def test_topology_artifact_script_class_reflects_intrinsic(
        self,
    ) -> None:
        """ROUND 2 — Test C (persisted artifact uses intrinsic value).

        Build a topology with an RbxScript whose ``script_type`` has
        been mutated post-construction (simulating
        ``classify_storage``'s Script→LocalScript coercion). The
        topology artifact's ``modules[*].script_class`` MUST reflect
        the intrinsic (pre-mutation) value, NOT the mutated one.

        This is the round-2 contract witness: the helper genuinely
        reads ``intrinsic_script_type``, and the persisted artifact
        carries the intrinsic value all the way through.
        """
        sr = _mk_artifact(
            modules={"guid-x": _mk_module("HudControl", "client")},
        )
        # Post-classify_storage shape: script_type was reassigned to
        # LocalScript by the StarterPlayerScripts coercion, but the
        # immutable intrinsic_script_type still holds the original
        # transpiler decision ("Script").
        script = RbxScript(
            name="HudControl",
            source="-- empty",
            script_type="LocalScript",          # post-mutation
            intrinsic_script_type="Script",     # original transpile-time
        )
        scripts_by_class = {"HudControl": script}

        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class=scripts_by_class,
        )

        # The artifact's script_class field MUST be the intrinsic value.
        # If this assertion flips to "LocalScript" the helper has
        # regressed to reading the mutable field — the round-1 bug.
        assert artifact["modules"]["guid-x"]["script_class"] == "Script"

    def test_resume_with_cached_animation_drivers_does_not_abort(
        self, tmp_path: Path,
    ) -> None:
        """ROUND 2 — Test D (resume regression GUARD).

        The round-1 regression: when resume rehydrated a
        scene_runtime with cached resolved ``animation_drivers``
        alongside fresh ``modules`` rows with empty ``domain`` (because
        the pre-classifier first build hadn't stamped them yet),
        ``build_topology`` aborted on invariant 6/1.

        Option 3 (the round-2 fix) doesn't introduce that path —
        build_topology only runs ONCE, post-classifier, so module
        domain is populated. This test guards against future
        regressions that re-introduce a pre-classifier build by
        asserting that the post-classifier shape (resolved cached
        animation_drivers + populated module domains) builds cleanly.

        The check is cheap and protects slice 6 if any future change
        moves the build earlier.
        """
        # Reuse the door-shape fixture: it sets a client-domain module
        # with a populated ``domain`` field, which is the post-
        # classifier shape on resume.
        artifact, prefab_id, door_script_id = _door_shape_artifact(
            door_domain="client",
        )
        scene_runtime = cast("dict[str, object]", artifact)

        # Seed a cached resolved animation_drivers block (the shape
        # ``_merge_scene_runtime`` would rehydrate from disk on a
        # resume). Mirror the existing slice-3-round-4 preserve fixture
        # shape (``test_resume_with_animation_result_none_preserves_animation_drivers``)
        # so the cached block carries every field invariants 1-4
        # cross-check.
        cached_sid = compute_stable_id(prefab_id, "door", "open")
        cached_topology = {
            "modules": {},          # stale — gets rebuilt below
            "animation_drivers": {
                cached_sid: {
                    "stable_id": cached_sid,
                    "routing_status": "resolved",
                    "driver_module_guid": door_script_id,
                    "domain": "client",
                    "script_class": "LocalScript",
                    "lifecycle_role": "auto_run",
                    "observed_attribute": "open",
                    "observed_target": {
                        "kind": "self", "name": "", "scope": "workspace",
                    },
                    "bridge_group_id": None,
                },
            },
            "caller_graph": {},     # stale — gets rebuilt below
            "cross_domain_edges": [],
        }
        scene_runtime["topology"] = cached_topology

        anim_script = _mk_rbx_script("Anim_Door_door_open", "Script")
        plan = TestApplyTopologyToRbxScripts._mk_plan(server_scripts=[
            "Anim_Door_door_open",
        ])

        # Resume shape: animation_result is None (no fresh emission run)
        # but cached animation_drivers exists in scene_runtime["topology"].
        # _build_and_apply_topology should preserve the cached block
        # and NOT abort on any invariant.
        from converter.pipeline import Pipeline
        from core.roblox_types import RbxPlace
        from converter.animation_converter import AnimationConversionResult
        from types import SimpleNamespace
        from converter.code_transpiler import TranspilationResult

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        rbx_place.scripts = [anim_script]
        # animation_result=None signals "resume / no fresh emissions";
        # _build_and_apply_topology preserves animation_drivers from
        # the cached topology block.
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=None,
            guid_index=None,
            dependency_map={},
            # transpilation_result=None signals "no retranspile this run";
            # preserve caller_graph too.
            transpilation_result=None,
        )

        # This must NOT raise. Round-1's pre-classifier first build
        # would abort here on invariant 6 (cached resolved drivers
        # alongside modules with empty domain) — Option 3 doesn't
        # introduce that path so the call succeeds cleanly.
        pipeline._build_and_apply_topology(scene_runtime, plan)

        # Cached animation_drivers preserved verbatim (the slice-3
        # round-4 contract): the rebuilt topology carries the same
        # block as the cached one.
        topo = scene_runtime["topology"]
        assert isinstance(topo, dict)
        drivers = topo["animation_drivers"]
        assert cached_sid in drivers
        assert drivers[cached_sid]["routing_status"] == "resolved"


# ===========================================================================
# Phase 2a slice 9a: TopologyInputs plumbing + #10 fold-in
# ===========================================================================


class TestSlice9aTopologyInputsPlumbing:
    """Slice 9a deliverables:

    1. ``_build_and_apply_topology`` receives ``topology_inputs`` (from
       the prepass) and threads its inverted ``script_id_by_name``
       through ``build_topology`` as ``script_by_sid``.
    2. ``--phase=write_output`` resume reproduces the same artifact
       (no persistence needed; the prepass re-runs because
       ``materialize_and_classify`` is essential).
    3. Followup task #10 fold-in: two modules with colliding
       ``class_name`` but distinct stems get correct script_class on
       the topology entry (where today they'd silently fall through
       to ``"ModuleScript"`` because ``scripts_by_class`` excludes
       colliding class_names).

    The byte-equivalence of ``module_path`` on the topology entry
    is NOT tested here — that's slice 9b's domain. Slice 9a
    preserves today's stamped-field semantics; only the
    script_class join changes. Slice 9b additionally dropped the
    parallel ``reachability_forced_container`` mirror from
    ``TopologyModuleEntry``; the planner-row audit signal in
    ``domain_signals`` is unchanged.
    """

    @staticmethod
    def _mk_pipeline_with_topology_inputs(
        *, scripts: list[RbxScript], tmp_path: Path,
    ):
        """Build a Pipeline forge identical to
        ``TestApplyTopologyToRbxScripts._mk_pipeline`` but parameterised
        for slice 9a's tests (no emitted_animations needed)."""
        from types import SimpleNamespace
        from converter.pipeline import Pipeline
        from converter.animation_converter import AnimationConversionResult
        from converter.code_transpiler import TranspilationResult
        from core.roblox_types import RbxPlace
        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        rbx_place.scripts = scripts
        anim_result = AnimationConversionResult()
        anim_result.emitted_animations = []
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,
            guid_index=None,
            dependency_map={},
            transpilation_result=TranspilationResult(),
        )
        return pipeline

    @staticmethod
    def _mk_topology_inputs(*, script_id_by_name: dict[str, str]):
        """Synthesize a minimal ``TopologyInputs`` for tests that drive
        ``_build_and_apply_topology`` directly. Only
        ``script_id_by_name`` is consumed by the slice 9a code path;
        the other fields default to empty/legacy.
        """
        from converter.scene_runtime_topology.module_domain import (
            TopologyInputs,
        )
        return TopologyInputs(
            domains={},
            reachability_requirements={},
            lifecycle_roles={},
            script_id_by_name=script_id_by_name,
            caller_graph={},
            transpile_ran=True,
            cross_domain_edges=[],
            shared_flag_channels=_failopen_sfc(),
        )

    def test_plumbing_passes_topology_inputs_through_to_modules_block(
        self, tmp_path: Path,
    ) -> None:
        """Slice 9a deliverable 1: when ``topology_inputs`` is provided,
        ``_build_and_apply_topology`` builds a ``script_by_sid`` map
        from ``topology_inputs.script_id_by_name`` and threads it to
        ``build_topology``, which uses it in ``_build_modules_block``.

        Witness: a script flagged with a non-default
        ``intrinsic_script_type`` on the rbx_place side maps to the
        topology entry's ``script_class`` via the script_id join. If
        the kwarg weren't being plumbed through, the legacy
        class_name join would still pick the same script for the
        simple-non-colliding case — so this test pairs with the
        collision test below for full coverage of the new path.
        """
        sr = _mk_artifact(
            modules={"guid-hud": _mk_module("HudControl", "client")},
        )
        scene_runtime = cast("dict[str, object]", sr)
        hud_script = _mk_rbx_script("HudControl", "LocalScript")
        hud_script.intrinsic_script_type = "LocalScript"

        pipeline = self._mk_pipeline_with_topology_inputs(
            scripts=[hud_script], tmp_path=tmp_path,
        )
        # Plan must be empty (no animation drivers in this fixture).
        from converter.storage_classifier import StoragePlan
        plan = StoragePlan()

        topology_inputs = self._mk_topology_inputs(
            script_id_by_name={"HudControl": "guid-hud"},
        )
        pipeline._build_and_apply_topology(
            scene_runtime, plan, topology_inputs=topology_inputs,
        )
        topo = scene_runtime["topology"]
        assert isinstance(topo, dict)
        assert "guid-hud" in topo["modules"]  # type: ignore[index]
        # Slice 9a #10 fold-in: the script_id join landed the same
        # script the class_name join would have, so script_class is
        # preserved at "LocalScript". Slice 9b dropped the
        # parallel ``reachability_forced_container`` mirror; here we
        # just assert the new join didn't regress the script_class
        # output.
        entry = topo["modules"]["guid-hud"]  # type: ignore[index]
        assert entry["script_class"] == "LocalScript"

    def test_resume_parity_assemble_no_retranspile_reuses_prepass_output(
        self, tmp_path: Path,
    ) -> None:
        """Slice 9a deliverable 2: an assemble-no-retranspile resume
        (``transpilation_result is None``, fresh prepass run) produces
        a topology artifact whose ``modules`` block has the same
        ``script_class`` as a fresh run.

        Slice 6 + 9a guarantee this structurally: the prepass runs on
        every invocation because ``materialize_and_classify`` is in
        ESSENTIAL_PHASES (pipeline.py:612). ``topology_inputs`` is NOT
        persisted onto ``StoragePlan``; it's recomputed each run from
        ``scene_runtime`` + ``rbx_place.scripts``.
        """
        from types import SimpleNamespace
        from converter.pipeline import Pipeline
        from converter.animation_converter import AnimationConversionResult
        from core.roblox_types import RbxPlace
        from converter.storage_classifier import StoragePlan

        sr = _mk_artifact(
            modules={"guid-hud": _mk_module("HudControl", "client")},
        )
        scene_runtime_fresh = cast("dict[str, object]", sr)
        scene_runtime_resume = cast(
            "dict[str, object]",
            _mk_artifact(
                modules={"guid-hud": _mk_module("HudControl", "client")},
            ),
        )
        hud_script_fresh = _mk_rbx_script("HudControl", "LocalScript")
        hud_script_fresh.intrinsic_script_type = "LocalScript"
        hud_script_resume = _mk_rbx_script("HudControl", "LocalScript")
        hud_script_resume.intrinsic_script_type = "LocalScript"

        topology_inputs = self._mk_topology_inputs(
            script_id_by_name={"HudControl": "guid-hud"},
        )

        # Fresh run.
        fresh_pipeline = self._mk_pipeline_with_topology_inputs(
            scripts=[hud_script_fresh], tmp_path=tmp_path,
        )
        fresh_pipeline._build_and_apply_topology(
            scene_runtime_fresh, StoragePlan(),
            topology_inputs=topology_inputs,
        )

        # Resume run: simulate assemble-no-retranspile by setting
        # ``transpilation_result=None`` (the slice 3 round 6 signal).
        # The prepass would still produce the same topology_inputs in
        # production because script_id_by_name is recomputed from
        # rbx_place.scripts + modules, which are identical here.
        resume_pipeline = Pipeline.__new__(Pipeline)
        resume_pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        rbx_place.scripts = [hud_script_resume]
        anim_result = AnimationConversionResult()
        anim_result.emitted_animations = []
        resume_pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,
            guid_index=None,
            dependency_map={},
            transpilation_result=None,  # resume signal
        )
        resume_pipeline._build_and_apply_topology(
            scene_runtime_resume, StoragePlan(),
            topology_inputs=topology_inputs,
        )

        # Same script_class on both runs.
        fresh_entry = scene_runtime_fresh["topology"]["modules"]["guid-hud"]  # type: ignore[index]
        resume_entry = scene_runtime_resume["topology"]["modules"]["guid-hud"]  # type: ignore[index]
        assert fresh_entry["script_class"] == resume_entry["script_class"]
        # Slice 9a invariant: module_path is byte-equivalent across
        # fresh + resume (it reads off the stamped row fields).
        # Slice 9b dropped ``reachability_forced_container`` from the
        # topology entry, so it's no longer part of the resume-parity
        # surface; ``reachability_required_container`` carries the
        # full semantic.
        assert fresh_entry["module_path"] == resume_entry["module_path"]
        assert (
            fresh_entry["reachability_required_container"]
            == resume_entry["reachability_required_container"]
        )
        assert "reachability_forced_container" not in fresh_entry
        assert "reachability_forced_container" not in resume_entry

    def test_followup_10_colliding_class_name_distinct_stems_resolves_via_sid_join(
        self, tmp_path: Path,
    ) -> None:
        """Slice 9a deliverable 3 (#10 fold-in): two modules with
        colliding ``class_name`` but distinct stems both reach the
        topology entry with the correct ``script_class``.

        Pre-fold-in: ``scripts_by_class`` (built via
        ``build_scripts_by_class_name``) excludes the colliding
        class_name entirely per the slice-3 degraded-service
        contract, so ``_build_modules_block`` silently fell through
        to ``derive_intrinsic_script_class(None) == "ModuleScript"``
        for BOTH rows. ``build_script_id_by_name`` already uses the
        SAME class_name + stem (with collision exclusion on BOTH
        keyspaces) join — when class_name collides but stems are
        distinct, the stem fallback resolves both rows correctly.

        Inverting ``script_id_by_name`` into ``script_by_sid`` carries
        that resolution through to ``_build_modules_block``: BOTH
        rows now get their intrinsic script_class from their actual
        script row.

        Source-of-truth pin (Codex prediction): "asymmetric joins if
        ``script_class`` keeps old class-name-only lookup at
        ``build_topology.py:529``" — this test closes that P1.
        """
        # Two modules with colliding class_name "Shared" but distinct
        # stems "Bootstrap" + "GameInit". Real-world cause: two
        # ``.cs`` files declaring ``class Shared`` (e.g. nested
        # partial classes, or a sloppy refactor); the planner gives
        # them distinct stems but the same class_name.
        sr = _mk_artifact(modules={
            "guid-bootstrap": _mk_module(
                "Bootstrap", "client", class_name="Shared",
            ),
            "guid-gameinit": _mk_module(
                "GameInit", "server", class_name="Shared",
            ),
        })
        scene_runtime = cast("dict[str, object]", sr)
        # Scripts emitted with FILE STEM as name (the convention
        # ``code_transpiler`` follows when class != stem) but
        # distinct intrinsic_script_type values.
        bootstrap_script = _mk_rbx_script("Bootstrap", "LocalScript")
        bootstrap_script.intrinsic_script_type = "LocalScript"
        gameinit_script = _mk_rbx_script("GameInit", "Script")
        gameinit_script.intrinsic_script_type = "Script"

        pipeline = self._mk_pipeline_with_topology_inputs(
            scripts=[bootstrap_script, gameinit_script],
            tmp_path=tmp_path,
        )
        from converter.storage_classifier import StoragePlan
        plan = StoragePlan()

        # ``build_script_id_by_name`` would resolve via the stem
        # fallback (class_name "Shared" excluded; stems "Bootstrap"
        # / "GameInit" distinct and not colliding). Synthesize the
        # output here so the test is hermetic w.r.t. the
        # planner-side helper but mirrors what the real prepass
        # produces in this collision shape.
        topology_inputs = self._mk_topology_inputs(
            script_id_by_name={
                "Bootstrap": "guid-bootstrap",
                "GameInit": "guid-gameinit",
            },
        )
        pipeline._build_and_apply_topology(
            scene_runtime, plan, topology_inputs=topology_inputs,
        )

        topo = scene_runtime["topology"]
        assert isinstance(topo, dict)
        # BOTH rows get their actual script_class from the script_id
        # join — pre-fold-in BOTH would have been "ModuleScript".
        bootstrap_entry = topo["modules"]["guid-bootstrap"]  # type: ignore[index]
        gameinit_entry = topo["modules"]["guid-gameinit"]  # type: ignore[index]
        assert bootstrap_entry["script_class"] == "LocalScript"
        assert gameinit_entry["script_class"] == "Script"

    def test_followup_10_legacy_class_name_join_runs_when_topology_inputs_is_None(
        self, tmp_path: Path,
    ) -> None:
        """Back-compat: ``script_by_sid=None`` (the default for callers
        that don't carry topology_inputs) → ``_build_modules_block``
        falls back to the legacy class_name join via
        ``scripts_by_class``. Demonstrates the kwarg is opt-in and
        does not regress callers that haven't migrated.
        """
        sr = _mk_artifact(
            modules={"guid-hud": _mk_module("HudControl", "client")},
        )
        scene_runtime = cast("dict[str, object]", sr)
        hud_script = _mk_rbx_script("HudControl", "LocalScript")
        hud_script.intrinsic_script_type = "LocalScript"
        pipeline = self._mk_pipeline_with_topology_inputs(
            scripts=[hud_script], tmp_path=tmp_path,
        )
        from converter.storage_classifier import StoragePlan
        # No topology_inputs passed — kwarg defaults to None.
        pipeline._build_and_apply_topology(
            scene_runtime, StoragePlan(),
        )
        topo = scene_runtime["topology"]
        assert isinstance(topo, dict)
        entry = topo["modules"]["guid-hud"]  # type: ignore[index]
        # Legacy class_name join still resolved HudControl correctly
        # (no collision), so script_class is "LocalScript".
        assert entry["script_class"] == "LocalScript"


class TestSlice9bR1DegenerateFixture:
    """Slice 9b R1 fold-in: the slice-9a ``assert topology_inputs is
    not None`` at the ``_classify_storage`` topology branch was
    converted to a conditional skip. ``_maybe_run_topology_prepass``
    returns ``None`` when ``rbx_place.scripts`` is empty
    (pipeline.py:_maybe_run_topology_prepass:4441); the existing
    ``_classify_storage`` early return at line 4193 covers the same
    case at the entry, but a future caller that bypassed the entry
    guard (or a regression that moved it) would have crashed on the
    assert. The conditional skip is the defensive equivalent.

    Pairs with the slice-9b doc rationale (scene-runtime-architecture
    -ir.md slice plan): "hoist scripts == [] check above
    ``assert topology_inputs is not None`` at the new call site, OR
    conditional the assert."
    """

    def test_classify_storage_topology_branch_skipped_when_prepass_returns_none(
        self, tmp_path: Path,
    ) -> None:
        """Drive ``_classify_storage`` end-to-end with the degenerate
        shape (non-empty modules + empty rbx_place.scripts). The
        method's line-4193 early return fires today; this test
        additionally verifies the conditional skip at the topology
        branch so a future refactor that moves the early return
        cannot reintroduce the AssertionError.

        Strategy: drive the inner topology branch directly with the
        same precondition shape (``topology_inputs=None``) the
        degenerate path produces, and assert no ``AssertionError``
        is raised and no topology block is written.
        """
        # Simulate the post-classify state where the prepass returned
        # None (e.g. empty scripts). The conditional skip should
        # noop rather than assert.
        sr = _mk_artifact(
            modules={"guid-hud": _mk_module("HudControl", "client")},
        )
        scene_runtime = cast("dict[str, object]", sr)
        hud_script = _mk_rbx_script("HudControl", "LocalScript")
        from converter.storage_classifier import StoragePlan
        from tests.test_scene_runtime_topology import (
            TestSlice9aTopologyInputsPlumbing,
        )
        pipeline = (
            TestSlice9aTopologyInputsPlumbing._mk_pipeline_with_topology_inputs(
                scripts=[hud_script], tmp_path=tmp_path,
            )
        )
        # Call without topology_inputs to mirror the post-prepass-None
        # branch's effective signal. The classify_storage caller
        # would skip the topology call entirely; here we assert the
        # underlying ``_build_and_apply_topology`` is also resilient
        # to the ``topology_inputs=None`` default (the slice-9a
        # back-compat path).
        pipeline._build_and_apply_topology(
            scene_runtime, StoragePlan(),  # topology_inputs defaults to None
        )
        # The build_topology branch ran via the legacy class_name
        # join (back-compat) — module entry is present and no crash.
        topo = scene_runtime["topology"]
        assert isinstance(topo, dict)
        assert "guid-hud" in topo["modules"]  # type: ignore[index]

    def test_classify_storage_skips_topology_with_empty_scripts(
        self, tmp_path: Path,
    ) -> None:
        """Direct regression: drive ``_classify_storage`` with
        ``rbx_place.scripts = []`` and non-empty ``modules`` in
        ``scene_runtime``. The method must early-return at line
        4193 (no plan written, no crash) regardless of the topology
        branch's downstream guard.
        """
        from types import SimpleNamespace
        from converter.pipeline import Pipeline
        from converter.animation_converter import AnimationConversionResult
        from converter.code_transpiler import TranspilationResult
        from core.roblox_types import RbxPlace

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.output_dir = tmp_path
        rbx_place = RbxPlace()
        rbx_place.scripts = []  # the degenerate shape
        anim_result = AnimationConversionResult()
        anim_result.emitted_animations = []
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,
            guid_index=None,
            dependency_map={},
            transpilation_result=TranspilationResult(),
        )
        pipeline.ctx = SimpleNamespace(
            scene_runtime_mode="generic",
            scene_runtime={
                "modules": {"guid-hud": _mk_module("HudControl", "client")},
            },
            warnings=[],
            networking_mode="none",
            strict_classification=False,
            storage_plan=None,
        )
        # No AssertionError, no crash; method must early return.
        pipeline._classify_storage()
        # No plan written: storage_plan stays None (early-return
        # before any classify_storage call).
        assert pipeline.ctx.storage_plan is None


class TestSlice10ReachabilityRequirementsNormalization:
    """Phase 2a slice 10: ``build_topology._build_modules_block`` reads
    ``reachability_required_container`` from
    ``TopologyInputs.reachability_requirements[sid]`` normalized
    through the same predicate gate
    ``finalize_topology_containers`` uses
    (``current_container in _SERVER_CONTAINERS_FOR_REACHABILITY``).

    These tests pin all four normalization cases AND assert
    byte-equivalence to today's planner-row audit signal observable
    for the same upstream → read-site flow. Crucially, the fixtures
    do NOT pre-stamp ``domain_signals["reachability_forced_container"]``
    on the module rows — they exercise the real upstream producer
    (``derive_reachability_requirements``) so a producer/consumer
    mismatch would surface as a test failure (the slice-7 lesson the
    brief calls out: pre-stamped fixtures mask producer/consumer
    asymmetries).
    """

    def _build_artifact(
        self, *, helper_parent_path: str, dep_map: dict[str, list[str]],
        requirements: dict[str, str] | None = None,
    ):
        """Construct a scene_runtime + RbxScripts pair with one
        ClientA module + one Helper module, where helper_script is
        seated in ``helper_parent_path``. When ``requirements`` is
        provided, it pins the slice-10 reachability_requirements map
        directly; when ``None``, the caller is expected to derive it.
        Returns ``(scene_runtime, scripts, helper_script)``.
        """
        sr = _mk_artifact(modules={
            "guid-client": _mk_module("ClientA", "client"),
            "guid-helper": {
                "stem": "Helper", "class_name": "Helper",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
        })
        helper_script = RbxScript(
            name="Helper", source="-- helper",
            script_type="ModuleScript",
            parent_path=helper_parent_path,
        )
        client_script = _mk_rbx_script("ClientA", "LocalScript")
        return sr, [client_script, helper_script], helper_script

    def _build_topology_with_reqs(
        self,
        sr,
        scripts,
        helper_script: RbxScript,
        client_script: RbxScript,
        *,
        reachability_requirements: dict[str, str] | None,
    ):
        """Helper: build the topology block directly with the supplied
        requirements map. Mirrors the production wiring at
        ``pipeline._build_and_apply_topology``.
        """
        from converter.scene_runtime_topology.build_topology import (
            build_topology,
        )
        return build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={
                "ClientA": client_script,
                "Helper": helper_script,
            },
            reachability_requirements=reachability_requirements,
        )

    def test_case_1_requirement_missing_emits_empty_string(self) -> None:
        """Helper not in ``reachability_requirements`` (non-helper,
        or unconstrained helper) -> ``""``. Matches today's empty
        audit signal for the same rows.
        """
        sr, scripts, helper = self._build_artifact(
            helper_parent_path="ServerStorage",
            dep_map={},
        )
        artifact = self._build_topology_with_reqs(
            sr, scripts, helper, scripts[0],
            # Empty map: helper is not in the dict.
            reachability_requirements={},
        )
        entry = artifact["modules"]["guid-helper"]
        assert entry["reachability_required_container"] == ""

    def test_case_2_excluded_sentinel_emits_empty_string(self) -> None:
        """``reachability_requirements[sid] == "__excluded__"`` (helper
        reached by BOTH client and server require-graphs) -> ``""``.

        Today: the conflict path in ``finalize_topology_containers``
        stamps ``fail_closed_reason`` but NEVER writes
        ``reachability_forced_container``, so the audit signal stays
        empty. Slice 10 collapses the sentinel to ``""`` at the read
        site (the conflict semantic is owned by
        ``fail_closed_reason``, not the topology entry surface).
        """
        sr, scripts, helper = self._build_artifact(
            helper_parent_path="ServerStorage",
            dep_map={},
        )
        artifact = self._build_topology_with_reqs(
            sr, scripts, helper, scripts[0],
            reachability_requirements={"guid-helper": "__excluded__"},
        )
        entry = artifact["modules"]["guid-helper"]
        assert entry["reachability_required_container"] == ""

    def test_case_3_replicated_storage_with_gated_container_emits_replicated_storage(
        self,
    ) -> None:
        """``requirement == "ReplicatedStorage"`` AND helper currently
        in ``_SERVER_CONTAINERS_FOR_REACHABILITY`` (the late-hoist
        gate fires) -> ``"ReplicatedStorage"``. Mirrors today's
        planner audit signal stamp from the late hoist arm
        (module_domain.py:947-955 pre-slice-10).

        Fixture invariant 10 setup: invariant 10 enforces
        ``module_path.startswith(f"{required}.")``; when the gate
        fires the late hoist arm rewrites ``module_path`` to
        ``"ReplicatedStorage.<name>"`` in lockstep. We pre-stamp
        ``module_path`` on the synthetic module row to satisfy that
        coherence, then assert the slice-10 read site surfaces
        ``"ReplicatedStorage"`` from the normalization.
        """
        from converter.scene_runtime_topology.module_domain import (
            _SERVER_CONTAINERS_FOR_REACHABILITY,
        )
        # Both gated containers exercise the same arm; covering both
        # pins the predicate identity (codex P1.2 from slice 4: the
        # legacy check missed ServerScriptService).
        for gated in sorted(_SERVER_CONTAINERS_FOR_REACHABILITY):
            sr, scripts, helper = self._build_artifact(
                helper_parent_path=gated,
                dep_map={},
            )
            # Pre-stamp module_path to mirror the late-hoist
            # triple-write (script.parent_path + module.container +
            # module.module_path move in lockstep). The gate check
            # at the read site uses script.parent_path; the late hoist
            # arm would have mutated parent_path AND module_path; for
            # this synthetic fixture we manually establish the
            # pre-hoist parent_path (the gate input) but keep the
            # post-hoist module_path (invariant 10 input) coherent.
            modules = sr["modules"]
            modules["guid-helper"]["module_path"] = (  # type: ignore[index]
                "ReplicatedStorage.Helper"
            )
            artifact = self._build_topology_with_reqs(
                sr, scripts, helper, scripts[0],
                reachability_requirements={
                    "guid-helper": "ReplicatedStorage",
                },
            )
            entry = artifact["modules"]["guid-helper"]
            assert entry["reachability_required_container"] == (
                "ReplicatedStorage"
            ), (
                f"gated={gated!r}: expected 'ReplicatedStorage', got "
                f"{entry['reachability_required_container']!r}"
            )

    def test_case_4_replicated_storage_with_nonserver_container_emits_empty_string(
        self,
    ) -> None:
        """``requirement == "ReplicatedStorage"`` AND helper already
        in a non-gated container -> ``""``. Mirrors today's behavior:
        the late hoist arm's gate at module_domain.py:939
        short-circuits when ``current_container`` is not in
        ``_SERVER_CONTAINERS_FOR_REACHABILITY``, so the audit signal
        stays empty. The semantic alignment with slice 7's
        production behavior (the
        ``_decide_script_container_from_topology`` pre-empt also
        leaves parent_path = ReplicatedStorage when the requirement
        fires).
        """
        for non_gated in [
            "ReplicatedStorage",
            "Workspace",
            "StarterPlayer.StarterPlayerScripts",
            "ReplicatedFirst",
            "",  # the empty-container case (defensively normalize)
        ]:
            sr, scripts, helper = self._build_artifact(
                helper_parent_path=non_gated,
                dep_map={},
            )
            artifact = self._build_topology_with_reqs(
                sr, scripts, helper, scripts[0],
                reachability_requirements={
                    "guid-helper": "ReplicatedStorage",
                },
            )
            entry = artifact["modules"]["guid-helper"]
            assert entry["reachability_required_container"] == "", (
                f"non_gated={non_gated!r}: expected '', got "
                f"{entry['reachability_required_container']!r}"
            )

    def test_byte_equivalence_to_legacy_audit_signal_through_real_upstream(
        self,
    ) -> None:
        """Drive ``derive_reachability_requirements`` (the real
        upstream producer) end-to-end on a fixture that exercises
        each of the 4 normalization cases, then compare the
        slice-10 normalized output against the legacy audit signal
        the pre-slice-10 ``finalize_topology_containers`` would have
        stamped for the SAME pipeline run.

        The legacy audit signal value is what
        ``finalize_topology_containers`` writes when its gate fires:
        the gate checks ``script.parent_path`` AT THE TIME OF THE
        FINALIZER. After slice 7's
        ``_decide_script_container_from_topology`` runs in production,
        ``parent_path`` is already at the requirement's target — so
        the gate short-circuits and the audit signal stayed empty.
        This test isolates the producer/consumer flow on a fixture
        with parent_path already at "ServerStorage" (the slice-4
        seed shape) so the gate would have fired pre-slice-10 — and
        asserts the slice-10 normalized output matches that
        post-gate signal byte-for-byte.

        Slice 7 lesson (per the slice 10 brief): the normalization
        test must exercise the real upstream → read-site flow, not
        pre-stamp ``domain_signals`` directly.
        """
        from converter.scene_runtime_domain import (
            derive_reachability_requirements,
            infer_module_domains,
        )

        # Set up: 4 helpers exercising all 4 cases.
        sr = _mk_artifact(modules={
            # Case 1: non-helper / unconstrained — no dep edge
            "guid-client": _mk_module("ClientA", "client"),
            "guid-helper-unconstrained": {
                "stem": "Unconstrained", "class_name": "Unconstrained",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
            # Case 2: helper reached by BOTH client and server
            "guid-server": _mk_module("ServerA", "server"),
            "guid-helper-conflict": {
                "stem": "Conflict", "class_name": "Conflict",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
            # Case 3: client-only, in gated container — gate fires
            "guid-helper-hoist": {
                "stem": "NeedsHoist", "class_name": "NeedsHoist",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
            # Case 4: client-only, already in non-gated container
            "guid-helper-already-rs": {
                "stem": "AlreadyRS", "class_name": "AlreadyRS",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
        })
        # Pin the runtime-bearing modules' domains via the operator
        # override surface so ``infer_module_domains`` doesn't fall
        # through to the zero-signal path (the synthetic empty C#
        # sources don't carry strong signals). Helpers are
        # ``runtime_bearing=False`` so they short-circuit to
        # ``domain="helper"`` regardless.
        sr["domain_overrides"] = {  # type: ignore[index]
            "guid-client": "client",
            "guid-server": "server",
        }

        # Scripts: helper-hoist starts in ServerStorage (gated);
        # helper-already-rs starts in ReplicatedStorage (non-gated).
        # Emitted-require edges (the new canonical closure source):
        #   ClientA (LocalScript entry) -> NeedsHoist, AlreadyRS, Conflict
        #   ServerA (server-domain Script) -> Conflict
        def _req(*names: str) -> str:
            return "\n".join(
                f'require(script.Parent:FindFirstChild("{n}"))'
                for n in names
            )

        client_script = RbxScript(
            name="ClientA",
            source=_req("NeedsHoist", "AlreadyRS", "Conflict"),
            script_type="LocalScript",
        )
        client_script.intrinsic_script_type = "LocalScript"
        server_script = RbxScript(
            name="ServerA", source=_req("Conflict"),
            script_type="Script",
        )
        server_script.intrinsic_script_type = "Script"
        helper_unconstrained = RbxScript(
            name="Unconstrained", source="-- u",
            script_type="ModuleScript",
            parent_path="ServerStorage",
        )
        helper_conflict = RbxScript(
            name="Conflict", source="-- c",
            script_type="ModuleScript",
            parent_path="ServerStorage",
        )
        helper_hoist = RbxScript(
            name="NeedsHoist", source="-- h",
            script_type="ModuleScript",
            parent_path="ServerStorage",
        )
        helper_already_rs = RbxScript(
            name="AlreadyRS", source="-- r",
            script_type="ModuleScript",
            parent_path="ReplicatedStorage",
        )
        scripts = [
            client_script, server_script,
            helper_unconstrained, helper_conflict,
            helper_hoist, helper_already_rs,
        ]

        # Real upstream: produce ``reachability_requirements`` the
        # way ``_maybe_run_topology_prepass`` does.
        domain_results = infer_module_domains(
            cast("dict", sr),
            scripts,
        )
        reqs = derive_reachability_requirements(
            cast("dict", sr),
            scripts,
            domain_results,
            **_reach_kwargs(sr["modules"], scripts),  # type: ignore[arg-type]
        )

        # The producer should have classified:
        #   - Unconstrained: NOT in reqs (no client dep edge)
        #   - Conflict: "__excluded__" (both client + server reach it)
        #   - NeedsHoist: "ReplicatedStorage" (client-only reach)
        #   - AlreadyRS: "ReplicatedStorage" (client-only reach)
        assert "guid-helper-unconstrained" not in reqs
        assert reqs.get("guid-helper-conflict") == "__excluded__"
        assert reqs.get("guid-helper-hoist") == "ReplicatedStorage"
        assert reqs.get("guid-helper-already-rs") == "ReplicatedStorage"

        # Pre-stamp module_path on the rows whose normalization will
        # emit a non-empty ``reachability_required_container`` to
        # satisfy invariant 10's ``module_path`` <-> required-container
        # coherence check. In production this stamping happens via
        # ``_stamp_container_and_path`` + the late hoist arm inside
        # ``finalize_topology_containers``; we mimic the post-finalizer
        # module_path while keeping ``script.parent_path`` at the
        # pre-finalizer value the legacy audit signal's gate would
        # have checked. This pins the producer/consumer link
        # (``derive_reachability_requirements`` → slice-10 normalized
        # read) without entangling the test with the late finalizer's
        # parent_path mutation.
        modules = sr["modules"]
        modules["guid-helper-hoist"]["module_path"] = (  # type: ignore[index]
            "ReplicatedStorage.NeedsHoist"
        )

        # Build topology BEFORE running the late finalizer (so
        # parent_path is still the pre-hoist value the legacy audit
        # signal's gate would have checked).
        from converter.scene_runtime_topology.build_topology import (
            build_topology,
        )
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={
                "ClientA": client_script,
                "ServerA": server_script,
                "Unconstrained": helper_unconstrained,
                "Conflict": helper_conflict,
                "NeedsHoist": helper_hoist,
                "AlreadyRS": helper_already_rs,
            },
            reachability_requirements=reqs,
        )

        # Byte-equivalent to the legacy audit signal observable for
        # the matching upstream state:
        #   - Unconstrained: today's audit = "" (no gate). Slice 10 = ""
        #     (requirement missing -> "").
        #   - Conflict: today's audit = "" (conflict arm at
        #     module_domain.py:917-935 stamps fail_closed_reason but
        #     NEVER reachability_forced_container).
        #     Slice 10 = "" ("__excluded__" -> "").
        #   - NeedsHoist: today's audit = "ReplicatedStorage" (the
        #     hoist arm fires: parent_path="ServerStorage" is in the
        #     gated set). Slice 10 = "ReplicatedStorage" (case 3).
        #   - AlreadyRS: today's audit = "" (gate at
        #     module_domain.py:939 short-circuits: parent_path
        #     ="ReplicatedStorage" is NOT in the gated set).
        #     Slice 10 = "" (case 4).
        expected = {
            "guid-helper-unconstrained": "",
            "guid-helper-conflict": "",
            "guid-helper-hoist": "ReplicatedStorage",
            "guid-helper-already-rs": "",
        }
        for sid, want in expected.items():
            got = artifact["modules"][sid]["reachability_required_container"]
            assert got == want, (
                f"sid={sid!r}: slice-10 normalized output {got!r} "
                f"diverges from legacy audit signal {want!r}"
            )

    def test_normalize_helper_is_pure_and_handles_unrecognized_values(
        self,
    ) -> None:
        """Direct unit test on the normalization helper itself.

        Pinning purity (no module state mutation) and the
        defensive fall-through for unrecognized requirement values.
        Today's universe is ``{REPLICATED_STORAGE, "__excluded__"}``
        per ``derive_reachability_requirements``; the helper
        collapses anything else to ``""`` so a future producer
        adding a new value doesn't silently surface a bogus
        container on the topology entry without an explicit
        opt-in.
        """
        from converter.scene_runtime_topology.build_topology import (
            _normalize_reachability_requirement,
        )
        from converter.scene_runtime_topology.module_domain import (
            _SERVER_CONTAINERS_FOR_REACHABILITY,
        )

        # None / missing.
        assert _normalize_reachability_requirement(None, "") == ""
        assert _normalize_reachability_requirement(None, "ServerStorage") == ""

        # Sentinel.
        assert _normalize_reachability_requirement("__excluded__", "") == ""
        assert _normalize_reachability_requirement(
            "__excluded__", "ServerStorage",
        ) == ""

        # ReplicatedStorage + gated -> ReplicatedStorage.
        for gated in _SERVER_CONTAINERS_FOR_REACHABILITY:
            assert _normalize_reachability_requirement(
                "ReplicatedStorage", gated,
            ) == "ReplicatedStorage"

        # ReplicatedStorage + non-gated -> "".
        for non_gated in [
            "ReplicatedStorage", "Workspace",
            "StarterPlayer.StarterPlayerScripts", "ReplicatedFirst", "",
        ]:
            assert _normalize_reachability_requirement(
                "ReplicatedStorage", non_gated,
            ) == ""

        # Unrecognized value -> "" (defensive fall-through).
        assert _normalize_reachability_requirement(
            "SomeFutureContainer", "ServerStorage",
        ) == ""


class TestSlice10R2NoTranspileResumeSemantics:
    """Phase 2a slice 10 R2 (Option Y -- accept + document + test-pin).

    R1 review surfaced a documented regression: on a no-transpile
    resume (``--phase=write_output``, ``state.transpilation_result``
    is ``None``), ``derive_reachability_requirements`` returns ``{}``
    (its ``if not dependency_map: return {}`` contract at
    ``module_domain.py:782-783``). Slice 10's ``_build_modules_block``
    read site takes the ``reachability_requirements is not None``
    branch with an empty dict, so every ``.get(sid)`` is ``None``
    and the normalization helper collapses ``None`` to ``""``.

    Pre-slice-10 the planner-row audit signal
    ``domain_signals["reachability_forced_container"]`` was persisted
    across resumes and would have surfaced ``"ReplicatedStorage"`` for
    helpers the late-hoist rule had previously rewritten. Slice 10's
    new read site re-derives instead of persisting, so on resume
    EVERY ``reachability_required_container`` regenerates to ``""``.

    Synthesis decision (``slice-10-r1-decision.md``): accept the trade
    (consistent with slice 6's "empty reqs on no-transpile resume is
    acceptable" precedent + slice 3's ``preserved_caller_graph``),
    document at the read site, and pin the behavior with these tests
    so a future re-persistence attempt fails loudly.
    """

    def _build_resume_topology_inputs(
        self, *, script_id_by_name: dict[str, str],
    ):
        """Synthesize a ``TopologyInputs`` exactly as
        ``_maybe_run_topology_prepass`` produces it on a no-transpile
        resume: ``transpile_ran=False`` (because
        ``state.transpilation_result is None``) AND
        ``reachability_requirements={}`` (because
        ``derive_reachability_requirements`` returned ``{}`` when
        handed an empty ``dependency_map``).
        """
        from converter.scene_runtime_topology.module_domain import (
            TopologyInputs,
        )
        return TopologyInputs(
            domains={},
            reachability_requirements={},
            lifecycle_roles={},
            script_id_by_name=script_id_by_name,
            caller_graph={},
            transpile_ran=False,
            cross_domain_edges=[],
            shared_flag_channels=_failopen_sfc(),
        )

    def test_resume_regenerates_required_container_to_empty_string_for_all_modules(
        self, tmp_path: Path,
    ) -> None:
        """PIN: on a no-transpile resume the read site emits
        ``reachability_required_container == ""`` for EVERY module,
        regardless of whether the late-hoist rule would have fired
        during a fresh run.

        The fixture seeds a helper at ``parent_path="ServerStorage"``
        (the gated container that would have triggered the late-hoist
        rule in a fresh run and stamped the legacy audit signal with
        ``"ReplicatedStorage"``). On resume the read site cannot
        observe that signal because ``reachability_requirements`` is
        empty, so the normalization collapses to ``""``. If a future
        change accidentally restores persistence (e.g. by reviving the
        ``domain_signals["reachability_forced_container"]`` fallback,
        or by adding an artifact-side persist hook for
        ``reachability_requirements``), this test fails with a clear
        signal -- pinning the documented semantics.
        """
        sr = _mk_artifact(modules={
            "guid-client": _mk_module("ClientA", "client"),
            "guid-helper": {
                "stem": "Helper", "class_name": "Helper",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
        })
        scene_runtime = cast("dict[str, object]", sr)
        client_script = _mk_rbx_script("ClientA", "LocalScript")
        helper_script = RbxScript(
            name="Helper", source="-- helper",
            script_type="ModuleScript",
            # Gated container -- in a fresh run, the late-hoist rule
            # would have fired here and stamped the legacy audit signal
            # with "ReplicatedStorage".
            parent_path="ServerStorage",
        )

        pipeline = (
            TestSlice9aTopologyInputsPlumbing
            ._mk_pipeline_with_topology_inputs(
                scripts=[client_script, helper_script],
                tmp_path=tmp_path,
            )
        )
        # Simulate the resume signal at the pipeline state level too,
        # for parity with the production wiring at
        # ``pipeline.py:_build_topology_inputs`` (transpile_ran ==
        # ``state.transpilation_result is not None``). Not consulted by
        # the artifact read site (per the slice 10 R2 documentation),
        # but mirroring production keeps the fixture honest.
        pipeline.state.transpilation_result = None

        from converter.storage_classifier import StoragePlan
        topology_inputs = self._build_resume_topology_inputs(
            script_id_by_name={
                "ClientA": "guid-client",
                "Helper": "guid-helper",
            },
        )
        pipeline._build_and_apply_topology(
            scene_runtime, StoragePlan(),
            topology_inputs=topology_inputs,
        )

        topo = scene_runtime["topology"]
        assert isinstance(topo, dict)
        modules_block = topo["modules"]  # type: ignore[index]
        assert isinstance(modules_block, dict)

        # PIN: every module's reachability_required_container is "".
        # Including the helper that, in a fresh run, would have carried
        # "ReplicatedStorage" via the late-hoist rule.
        for sid, entry in modules_block.items():
            assert entry["reachability_required_container"] == "", (
                f"sid={sid!r}: expected '' on no-transpile resume, "
                f"got {entry['reachability_required_container']!r}. "
                f"If this assertion fails, the slice 10 R2 documented "
                f"semantic (no-transpile resume regenerates "
                f"reachability_required_container to '' for ALL "
                f"modules) has changed. Update both the docstring at "
                f"build_topology.py:_build_modules_block AND this test "
                f"together -- they are intentionally locked in step."
            )

    def test_storage_classifier_routing_unaffected_by_resume_empty_required_container(
        self,
    ) -> None:
        """The companion claim documented at the read site: storage
        routing is unaffected by the resume-empty regeneration because
        the storage classifier reads
        ``topology_inputs["reachability_requirements"]`` DIRECTLY
        (``storage_classifier.py:645``), not via the topology entry's
        ``reachability_required_container`` field.

        Phase 2a slice 11 P3 fix: rewritten to drive ``classify_storage``
        (the OUTER gate) instead of probing
        ``_decide_script_container_from_topology`` directly. Calling the
        inner helper bypassed the slice-6 unconstrained-helper short-
        circuit at ``storage_classifier.py:575-587`` -- the gate that
        ACTUALLY runs on a no-transpile resume routes a
        ``ModuleScript`` with ``transpile_ran=False`` AND no
        reachability requirement to ``_decide_script_container_legacy``.
        The original test asserted a property the production path does
        NOT exhibit on resume; the rewrite asserts the property the
        production path DOES exhibit.

        Fixture: ``transpile_ran=False``, empty
        ``reachability_requirements``, helper ``ModuleScript`` with one
        client caller. Expected routing: helper lands in
        ``ReplicatedStorage`` via the LEGACY caller-domain fallback,
        with the exact reason string emitted by
        ``_decide_script_container_legacy`` at
        ``storage_classifier.py:783-786``. The reason must NOT carry
        the ``"topology:"`` prefix emitted by the topology decision
        tree at ``storage_classifier.py:638-706`` -- the gate routed
        around it.

        If a future change re-routes the classifier to consult the
        topology entry's ``reachability_required_container`` (or
        otherwise lets the topology tree run on a no-transpile resume
        with empty requirements), this test will fail because the
        emitted reason would carry the ``"topology:"`` prefix.
        """
        from converter.scene_runtime_topology.module_domain import (
            TopologyInputs,
        )
        from converter.storage_classifier import classify_storage

        helper_script = RbxScript(
            name="Helper", source="-- helper",
            script_type="ModuleScript",
            parent_path="ServerStorage",
        )
        client_script = RbxScript(
            name="ClientA",
            source=(
                'local Players = game:GetService("Players")\n'
                'local lp = Players.LocalPlayer\n'
                'local Helper = require(game:GetService(\n'
                '    "ReplicatedStorage"\n'
                '):FindFirstChild("Helper"))'
            ),
            script_type="LocalScript",
            parent_path="StarterPlayer.StarterPlayerScripts",
        )
        topology_inputs: TopologyInputs = {
            "domains": {"guid-client": "client", "guid-helper": "helper"},
            # Empty dict: the no-transpile resume signature from
            # ``derive_reachability_requirements``'s
            # ``if not dependency_map: return {}`` contract.
            "reachability_requirements": {},
            "lifecycle_roles": {
                "guid-client": "",
                "guid-helper": "",
            },
            "script_id_by_name": {
                "ClientA": "guid-client",
                "Helper": "guid-helper",
            },
            "caller_graph": {"guid-helper": ["guid-client"]},
            # No-transpile resume signal. Combined with the empty
            # requirements map + ``script_type == "ModuleScript"`` this
            # triggers the slice-6 unconstrained-helper gate at
            # ``storage_classifier.py:575-587`` -- the fall-through to
            # ``_decide_script_container_legacy``.
            "transpile_ran": False,
        }

        plan = classify_storage(
            [client_script, helper_script],
            topology_inputs=topology_inputs,
        )

        # Helper lands at ReplicatedStorage via the legacy caller-
        # domain ModuleScript path -- byte-equivalent to the pre-
        # slice-10 resume outcome.
        assert helper_script.parent_path == "ReplicatedStorage", (
            f"Expected ReplicatedStorage from legacy caller-domain "
            f"ModuleScript fallback, got {helper_script.parent_path!r}"
        )

        # The reason MUST be the exact legacy caller-domain string
        # emitted by ``_decide_script_container_legacy`` at
        # ``storage_classifier.py:783-786``. If the topology tree had
        # run instead, the reason would start with the ``"topology:"``
        # prefix emitted at ``storage_classifier.py:638-706``.
        helper_reasons = {
            d["script"]: d["reason"]
            for d in plan.decisions
            if d["script"] == "Helper"
        }
        helper_reason = helper_reasons["Helper"]
        assert helper_reason == (
            "required by 1 caller(s), at least one client-side"
        ), (
            "Expected the legacy caller-domain reason from "
            "_decide_script_container_legacy; the unconstrained-helper "
            "gate should have routed around the topology tree. "
            f"Got: {helper_reason!r}"
        )
        assert not helper_reason.startswith("topology:"), (
            "Reason carries the topology-tree prefix -- the slice-6 "
            "unconstrained-helper gate did NOT route to legacy on "
            "transpile_ran=False + empty reachability_requirements. "
            f"Reason: {helper_reason!r}"
        )

    def test_artifact_field_carries_no_persistence_machinery(
        self,
    ) -> None:
        """Slice 10 R2 hard constraint: NO new persistence on the
        artifact entry. The output ``TopologyModuleEntry`` carries
        ``reachability_required_container`` as a derived field
        only; no shadow copy of ``reachability_requirements`` and
        no ``transpile_ran`` mirror.

        This test pins the artifact shape: the emitted module entry
        has exactly the slice-9b post-cleanup keys (no
        ``reachability_forced_container``, no
        ``reachability_requirements`` shadow, no ``transpile_ran``).
        If a future slice adds a persistence hook to work around the
        resume regression, this assertion fails with a clear signal.
        """
        from converter.scene_runtime_topology.build_topology import (
            build_topology,
        )

        sr = _mk_artifact(modules={
            "guid-helper": {
                "stem": "Helper", "class_name": "Helper",
                "runtime_bearing": False,
                "is_loader": False, "character_attached": False,
            },
        })
        helper_script = RbxScript(
            name="Helper", source="-- helper",
            script_type="ModuleScript",
            parent_path="ServerStorage",
        )
        artifact = build_topology(
            scene_runtime=cast(SceneRuntimeArtifact, sr),
            emitted_animations=[],
            scripts_by_class={"Helper": helper_script},
            reachability_requirements={},
        )
        entry = artifact["modules"]["guid-helper"]
        # The dropped slice-9b field MUST NOT have been revived.
        assert "reachability_forced_container" not in entry
        # NO shadow copies of upstream raw facts on the artifact entry.
        assert "reachability_requirements" not in entry
        assert "transpile_ran" not in entry


class TestPhase2bSlice2EnrichmentAndRelocation:
    """Phase 2b — the Class-1 component-ref producer + ``edge_enrichment``
    relocate into ``Pipeline._maybe_run_topology_prepass``;
    ``TopologyInputs`` carries ``cross_domain_edges`` +
    ``shared_flag_channels``; ``build_topology`` is pure-assembly; the
    duplicate-event_name invariant + sorted iteration + empty-field
    rejection hold.

    Reframe (2026-06-01): the shared-attribute candidate fan-out + its
    enrichment/invariants were RETIRED (Class-2 mis-modeled as Class 1);
    see ``TestSharedFlagChannels`` for the Class-2 replacement.

    Refs: design doc Phase 2b section.
    """

    @staticmethod
    def _build_pipeline_with(
        *,
        scripts: list[RbxScript],
        transpilation_result_scripts: "list | None" = None,
    ):
        """Synthesize a minimal Pipeline spy that reaches the
        ``_maybe_run_topology_prepass`` body. Mirrors the pattern from
        ``test_module_domain_prepass.py``'s ``TestSlice7Round2*``.

        ``transpilation_result_scripts`` controls the Luau-scan branch
        of slice 2 enrichment:
          - ``None`` -> ``state.transpilation_result is None``
            (resume case); consumer rows stay empty.
          - non-None list -> ``state.transpilation_result`` is set
            with the supplied scripts; the Luau-scan pass runs.
        """
        from unittest.mock import MagicMock
        from converter.code_transpiler import TranspilationResult
        from converter.pipeline import Pipeline

        p = MagicMock(spec=Pipeline)
        p.ctx = MagicMock()
        p.ctx.scene_runtime_mode = "modern"
        p.ctx.networking_mode = "none"
        p.state = MagicMock()
        if transpilation_result_scripts is None:
            p.state.transpilation_result = None
        else:
            tr = TranspilationResult()
            tr.scripts = transpilation_result_scripts
            p.state.transpilation_result = tr
        p.state.dependency_map = {}
        p.state.guid_index = None
        p.state.rbx_place = MagicMock()
        p.state.rbx_place.scripts = scripts
        return p

    def test_topology_inputs_carries_edges(self) -> None:
        """``_maybe_run_topology_prepass`` produces a ``TopologyInputs``
        whose ``cross_domain_edges`` (Class 1, component-ref) are
        populated from the structural producer + enrichment, and whose
        ``shared_flag_channels`` (Class 2) fact is recorded.

        R1 P1-A fix (2026-05-31): the script sources carry REAL client /
        server signals so ``infer_module_domains`` (fed into the producer
        via ``domains_override``) returns the expected client/server
        split for the cross-domain edge to be detected.
        """
        from converter.pipeline import Pipeline

        door = RbxScript(
            name="Door",
            source=(
                "local rs = game:GetService(\"RunService\")\n"
                "rs.RenderStepped:Connect(function() end)\n"
            ),
            script_type="LocalScript",
        )
        anim = RbxScript(
            name="Anim",
            source=(
                "local rs = game:GetService(\"ReplicatedStorage\")\n"
                "local re = rs:WaitForChild(\"E\")\n"
                "re.OnServerEvent:Connect(function() end)\n"
            ),
            script_type="Script",
        )
        scene_runtime: dict[str, object] = {
            "modules": {
                "door_sid": {
                    "stem": "Door", "class_name": "Door",
                    "runtime_bearing": True, "domain": "client",
                    "character_attached": False, "is_loader": False,
                    "module_path": "ReplicatedStorage.Door",
                },
                "anim_sid": {
                    "stem": "Anim", "class_name": "Anim",
                    "runtime_bearing": True, "domain": "server",
                    "character_attached": False, "is_loader": False,
                    "module_path": "ReplicatedStorage.Anim",
                },
            },
            "scenes": {
                "Mixed.unity": {
                    "instances": [
                        {"instance_id": "Mixed.unity:1",
                         "script_id": "door_sid",
                         "game_object_id": "Mixed.unity:1",
                         "active": True, "enabled": True, "config": {}},
                        {"instance_id": "Mixed.unity:2",
                         "script_id": "anim_sid",
                         "game_object_id": "Mixed.unity:2",
                         "active": True, "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "Mixed.unity:1",
                        "field": "open",
                        "index": None,
                        "target_kind": "component",
                        "target_ref": "Mixed.unity:2",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": [
                        "Mixed.unity:1", "Mixed.unity:2",
                    ],
                },
            },
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(
            scripts=[door, anim],
            transpilation_result_scripts=[],
        )
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        # Component-ref edge present in the edges bucket on TopologyInputs.
        assert len(out["cross_domain_edges"]) == 1
        edge = out["cross_domain_edges"][0]
        assert edge["from_script"] == "door_sid"
        assert edge["to_script"] == "anim_sid"
        # Class-2 shared-flag channel fact recorded. No cross-domain
        # GetAttribute reader in these (empty) transpiled scripts -> the
        # funnel channel is recorded but not present.
        assert "shared_flag_channels" in out
        channel = out["shared_flag_channels"]["PlayerSetSharedFlag"]
        assert channel["present"] is False
        assert channel["read_names"] == []

    def test_build_topology_consumes_edges_from_inputs(self) -> None:
        """``build_topology`` no longer calls the producer when
        ``cross_domain_edges_input`` is supplied: the artifact carries
        EXACTLY what was passed in, not what the producer would derive
        from ``scene_runtime``.

        Injects an artificial edge that does NOT correspond to any
        reference in the plan; if ``build_topology`` were still
        calling the producer it would emit zero edges from this plan.
        Asserting the artificial edge survives proves the read site
        consults the input parameter.
        """
        # Plan with zero scenes/prefabs, so the producer would emit
        # zero edges if invoked.
        sr = _mk_artifact()
        injected_edge = {
            "id": "test::injected::edge",
            "kind": "attribute_write",
            "from_instance": "i1", "to_instance": "i2",
            "from_script": "sid_a", "to_script": "sid_b",
            "field": "open",
            "from_domain": "client", "to_domain": "server",
            "owner_kind": "scene", "owner_ref": "X.unity",
            "resolution": {
                "strategy": "remote_event_bridge",
                "event_name": "Test_SetOpen",
            },
            "bridge_member_scripts": [
                {"role": "client_caller", "ref": "sid_a"},
                {"role": "server_listener",
                 "ref": "__bridge_listener_server__Test_SetOpen"},
                {"role": "anim_listener", "ref": "sid_b"},
            ],
            "payload": {"attribute_name": "open", "schema": "unknown"},
        }
        # Provide modules so invariant 7 isn't tripped + the candidate
        # ref invariant accepts ``sid_a``/``sid_b``.
        sr_dict = cast(dict[str, object], sr)
        sr_dict["modules"] = {
            "sid_a": {
                "stem": "A", "class_name": "A",
                "runtime_bearing": True, "domain": "client",
                "character_attached": False, "is_loader": False,
            },
            "sid_b": {
                "stem": "B", "class_name": "B",
                "runtime_bearing": True, "domain": "server",
                "character_attached": False, "is_loader": False,
            },
        }
        artifact = build_topology(
            scene_runtime=cast(SceneRuntimeArtifact, sr_dict),
            emitted_animations=[],
            scripts_by_class={
                "A": _mk_rbx_script("A", "LocalScript"),
                "B": _mk_rbx_script("B", "Script"),
            },
            cross_domain_edges_input=[cast(  # type: ignore[arg-type]
                "object", injected_edge,
            )],
            shared_flag_channels_input=_failopen_sfc(),
        )
        # The artifact carries the INJECTED edge verbatim.
        assert len(artifact["cross_domain_edges"]) == 1
        assert artifact["cross_domain_edges"][0]["id"] == "test::injected::edge"

    def test_enrichment_populates_bridge_members_component_ref(self) -> None:
        """A CLIENT-originated component-ref edge is enriched with a
        3-member bridge unit: ``client_caller`` = ``from_script``,
        ``server_listener`` = synthesized id from the helper,
        ``anim_listener`` = ``to_script``. (R2: this is now the
        client->server direction case; see
        ``test_enrichment_handles_server_to_client_direction`` for the
        opposite direction.)
        """
        from converter.pipeline import Pipeline
        from converter.scene_runtime_topology.bridge_emit import (
            synthesize_listener_id,
        )

        # R1 P1-A fix (2026-05-31): script sources carry real
        # client / server signals so ``infer_module_domains`` returns
        # the expected client/server split. The producers now consult
        # the inferred ``domains_override`` instead of the stamped
        # ``"domain"`` value on the modules dict.
        door = RbxScript(
            name="Door",
            source=(
                "local rs = game:GetService(\"RunService\")\n"
                "rs.RenderStepped:Connect(function() end)\n"
            ),
            script_type="LocalScript",
        )
        anim = RbxScript(
            name="Anim",
            source=(
                "local rs = game:GetService(\"ReplicatedStorage\")\n"
                "local re = rs:WaitForChild(\"E\")\n"
                "re.OnServerEvent:Connect(function() end)\n"
            ),
            script_type="Script",
        )
        scene_runtime: dict[str, object] = {
            "modules": {
                "door_sid": {
                    "stem": "Door", "class_name": "Door",
                    "runtime_bearing": True, "domain": "client",
                    "character_attached": False, "is_loader": False,
                    "module_path": "ReplicatedStorage.Door",
                },
                "anim_sid": {
                    "stem": "Anim", "class_name": "Anim",
                    "runtime_bearing": True, "domain": "server",
                    "character_attached": False, "is_loader": False,
                    "module_path": "ReplicatedStorage.Anim",
                },
            },
            "scenes": {
                "A.unity": {
                    "instances": [
                        {"instance_id": "A.unity:1",
                         "script_id": "door_sid",
                         "game_object_id": "A.unity:1",
                         "active": True, "enabled": True, "config": {}},
                        {"instance_id": "A.unity:2",
                         "script_id": "anim_sid",
                         "game_object_id": "A.unity:2",
                         "active": True, "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "A.unity:1",
                        "field": "open",
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
        pipeline = self._build_pipeline_with(
            scripts=[door, anim],
            transpilation_result_scripts=[],
        )
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        edge = out["cross_domain_edges"][0]
        members = edge["bridge_member_scripts"]
        roles_to_refs = {m["role"]: m["ref"] for m in members}
        assert roles_to_refs["client_caller"] == "door_sid"
        assert roles_to_refs["server_listener"] == synthesize_listener_id(
            "Door_SetOpen", direction="client_to_server",
        )
        assert roles_to_refs["anim_listener"] == "anim_sid"

    # ------------------------------------------------------------------
    # Direction-aware enrichment of component-ref edges: the enrichment
    # branches on ``from_domain`` so a server-originated edge gets a
    # ``server_caller`` + ``client_listener`` pair (not the
    # client->server shape).
    # ------------------------------------------------------------------

    def test_enrichment_handles_server_to_client_direction(self) -> None:
        """A SERVER-originated component-ref edge enriches with
        ``server_caller`` (= ``from_script``) + a synthesized
        ``client_listener`` (id prefixed
        ``__bridge_listener_client__``) + ``anim_listener``
        (direction-independent = ``to_script``).

        Direct ``enrich_cross_domain_edges`` call: synthesizes a
        pre-built ``CrossDomainEdge`` whose ``from_domain == "server"``
        and asserts the enrichment branches on direction.
        """
        from converter.scene_runtime_topology.bridge_emit import (
            SYNTHESIZED_CLIENT_LISTENER_ID_PREFIX,
            synthesize_listener_id,
        )
        from converter.scene_runtime_topology.cross_domain_edges import (
            CrossDomainEdge,
            PayloadSpec,
            ResolutionSpec,
        )
        from converter.scene_runtime_topology.edge_enrichment import (
            enrich_cross_domain_edges,
        )

        edge = CrossDomainEdge(
            id="A.unity:1::flag::A.unity:2",
            kind="attribute_write",
            from_instance="A.unity:1",
            to_instance="A.unity:2",
            from_script="pickup_sid",   # server-side producer
            to_script="hud_sid",        # client-side consumer
            field="flag",
            from_domain="server",
            to_domain="client",
            owner_kind="scene",
            owner_ref="A.unity",
            resolution=ResolutionSpec(
                strategy="remote_event_bridge",
                event_name="Pickup_SetFlag",
            ),
            bridge_member_scripts=[],
            payload=PayloadSpec(attribute_name="flag", schema="unknown"),
        )
        enriched_edges = enrich_cross_domain_edges(
            edges=[edge],
        )
        assert len(enriched_edges) == 1
        members = enriched_edges[0]["bridge_member_scripts"]
        roles_to_refs = {m["role"]: m["ref"] for m in members}
        # Server-originated -> server_caller + client_listener +
        # anim_listener (direction-independent).
        assert set(roles_to_refs.keys()) == {
            "server_caller", "client_listener", "anim_listener",
        }
        assert roles_to_refs["server_caller"] == "pickup_sid"
        assert roles_to_refs["client_listener"] == synthesize_listener_id(
            "Pickup_SetFlag", direction="server_to_client",
        )
        assert roles_to_refs["client_listener"].startswith(
            SYNTHESIZED_CLIENT_LISTENER_ID_PREFIX,
        )
        assert roles_to_refs["anim_listener"] == "hud_sid"
        # The synthesized id is DIFFERENT from the client_to_server
        # shape -- two distinct prefixes per direction.
        assert roles_to_refs["client_listener"] != synthesize_listener_id(
            "Pickup_SetFlag", direction="client_to_server",
        )
        # Resolution stays ``remote_event_bridge`` (not downgraded);
        # only unknown-direction rows downgrade to ``excluded``.
        assert enriched_edges[0]["resolution"]["strategy"] == (
            "remote_event_bridge"
        )

    def test_enrichment_excludes_edge_with_unknown_direction(self) -> None:
        """A pre-built edge whose ``from_domain`` is not a known
        runtime domain (``""``, ``"helper"``, etc.) is downgraded to
        ``resolution.strategy == "excluded"`` and gets an EMPTY
        ``bridge_member_scripts``. Slice 3 skips ``excluded`` rows.

        Defensive: producers already filter ``NON_RUNTIME_DOMAINS``,
        but on-disk plans / direct callers / future producers may
        feed such rows; the downgrade keeps the artifact
        well-formed.
        """
        from converter.scene_runtime_topology.cross_domain_edges import (
            CrossDomainEdge,
            PayloadSpec,
            ResolutionSpec,
        )
        from converter.scene_runtime_topology.edge_enrichment import (
            enrich_cross_domain_edges,
        )

        # An unknown-direction component-ref edge.
        bad_edge = CrossDomainEdge(
            id="X:1::peer::X:2",
            kind="attribute_write",
            from_instance="X:1", to_instance="X:2",
            from_script="a", to_script="b",
            field="peer",
            from_domain="",  # unknown direction.
            to_domain="server",
            owner_kind="scene", owner_ref="X.unity",
            resolution=ResolutionSpec(
                strategy="remote_event_bridge",
                event_name="A_SetPeer",
            ),
            bridge_member_scripts=[],
            payload=PayloadSpec(attribute_name="peer", schema="unknown"),
        )
        enriched_edges = enrich_cross_domain_edges(
            edges=[bad_edge],
        )
        # Component-ref edge: downgraded + empty bridge.
        assert len(enriched_edges) == 1
        assert enriched_edges[0]["resolution"]["strategy"] == "excluded"
        assert enriched_edges[0]["bridge_member_scripts"] == []
        # event_name preserved for debug triage.
        assert enriched_edges[0]["resolution"]["event_name"] == "A_SetPeer"



    def test_event_name_invariant_fires_only_on_semantic_collisions(
        self,
    ) -> None:
        """The component-ref event_name invariant fires ONLY on a
        SEMANTIC collision: two edges sharing the same ``event_name`` but
        with DIFFERENT ``payload.attribute_name``. Edges sharing both
        ``event_name`` AND ``payload.attribute_name`` are the same
        logical bridge instantiated multiple times -- no abort.

        Reframe (2026-06-01): the invariant is scoped to the
        ``cross_domain_edges`` bucket only (the candidate bucket was
        retired). This test exercises the true-collision case: two
        component-ref edges both carrying ``event_name="Shared_Set"`` but
        with different ``payload.attribute_name`` ("open" vs "locked").
        """
        sr = _mk_artifact()
        sr_dict = cast(dict[str, object], sr)
        sr_dict["modules"] = {
            "sid_a": {
                "stem": "A", "class_name": "A",
                "runtime_bearing": True, "domain": "client",
                "character_attached": False, "is_loader": False,
            },
            "sid_b": {
                "stem": "B", "class_name": "B",
                "runtime_bearing": True, "domain": "server",
                "character_attached": False, "is_loader": False,
            },
        }
        edge_open = {
            "id": "test::injected::open",
            "kind": "attribute_write",
            "from_instance": "i1", "to_instance": "i2",
            "from_script": "sid_a", "to_script": "sid_b",
            "field": "open",
            "from_domain": "client", "to_domain": "server",
            "owner_kind": "scene", "owner_ref": "X.unity",
            "resolution": {
                "strategy": "remote_event_bridge",
                "event_name": "Shared_Set",
            },
            "bridge_member_scripts": [],
            "payload": {"attribute_name": "open", "schema": "unknown"},
        }
        edge_locked = {
            "id": "test::injected::locked",
            "kind": "attribute_write",
            "from_instance": "i3", "to_instance": "i4",
            "from_script": "sid_a", "to_script": "sid_b",
            "field": "locked",
            "from_domain": "client", "to_domain": "server",
            "owner_kind": "scene", "owner_ref": "X.unity",
            "resolution": {
                "strategy": "remote_event_bridge",
                # Same event_name as edge_open but DIFFERENT
                # attribute_name -> semantic collision -> abort.
                "event_name": "Shared_Set",
            },
            "bridge_member_scripts": [],
            "payload": {"attribute_name": "locked", "schema": "unknown"},
        }
        with pytest.raises(TopologyInvariantError) as exc:
            build_topology(
                scene_runtime=cast(SceneRuntimeArtifact, sr_dict),
                emitted_animations=[],
                scripts_by_class={
                    "A": _mk_rbx_script("A", "LocalScript"),
                    "B": _mk_rbx_script("B", "Script"),
                },
                cross_domain_edges_input=[
                    cast("object", edge_open),  # type: ignore[arg-type]
                    cast("object", edge_locked),  # type: ignore[arg-type]
                ],
                shared_flag_channels_input=_failopen_sfc(),
            )
        assert "semantic collision on cross-domain event_name" in str(exc.value)
        assert "Shared_Set" in str(exc.value)
        # The abort message names the heterogeneous attribute_name set so
        # triage can see WHAT collided.
        assert "'open'" in str(exc.value)
        assert "'locked'" in str(exc.value)

    def test_fresh_run_emits_edges_via_domains_override(self) -> None:
        """P1-A regression guard (R1 fix, 2026-05-31): on a fresh run,
        ``scene_runtime["modules"][sid]["domain"]`` is EMPTY -- the
        classifier hasn't stamped it back yet at the moment
        ``_maybe_run_topology_prepass`` calls the producers. Without
        the ``domains_override`` kwarg the producers see ``""`` for
        every src+tgt domain and the ``NON_RUNTIME_DOMAINS`` filter
        drops every otherwise-valid cross-domain edge.

        We synthesize the fresh-run state by leaving every module's
        ``domain`` as ``""`` AND giving the scripts real signals so
        ``infer_module_domains`` (which the prepass calls) populates
        the local ``domains`` dict the override is fed from. The
        edges bucket must be NON-EMPTY at the prepass output.
        """
        from converter.pipeline import Pipeline

        door = RbxScript(
            name="Door",
            source=(
                "local rs = game:GetService(\"RunService\")\n"
                "rs.RenderStepped:Connect(function() end)\n"
            ),
            script_type="LocalScript",
        )
        anim = RbxScript(
            name="Anim",
            source=(
                "local rs = game:GetService(\"ReplicatedStorage\")\n"
                "local re = rs:WaitForChild(\"E\")\n"
                "re.OnServerEvent:Connect(function() end)\n"
            ),
            script_type="Script",
        )
        # Fresh-run shape: EVERY module's ``domain`` is empty string.
        # The classifier (which would stamp these) runs AFTER the
        # prepass. Pre-R1 producers read these blanks and dropped
        # every edge.
        scene_runtime: dict[str, object] = {
            "modules": {
                "door_sid": {
                    "stem": "Door", "class_name": "Door",
                    "runtime_bearing": True, "domain": "",
                    "character_attached": False, "is_loader": False,
                    "module_path": "ReplicatedStorage.Door",
                },
                "anim_sid": {
                    "stem": "Anim", "class_name": "Anim",
                    "runtime_bearing": True, "domain": "",
                    "character_attached": False, "is_loader": False,
                    "module_path": "ReplicatedStorage.Anim",
                },
            },
            "scenes": {
                "Fresh.unity": {
                    "instances": [
                        {"instance_id": "Fresh.unity:1",
                         "script_id": "door_sid",
                         "game_object_id": "Fresh.unity:1",
                         "active": True, "enabled": True, "config": {}},
                        {"instance_id": "Fresh.unity:2",
                         "script_id": "anim_sid",
                         "game_object_id": "Fresh.unity:2",
                         "active": True, "enabled": True, "config": {}},
                    ],
                    "references": [{
                        "from": "Fresh.unity:1",
                        "field": "open",
                        "index": None,
                        "target_kind": "component",
                        "target_ref": "Fresh.unity:2",
                        "target_is_ui": False,
                    }],
                    "lifecycle_order": [
                        "Fresh.unity:1", "Fresh.unity:2",
                    ],
                },
            },
            "prefabs": {},
            "domain_overrides": {},
        }
        pipeline = self._build_pipeline_with(
            scripts=[door, anim],
            transpilation_result_scripts=[],
        )
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        # The edge MUST be present despite the empty
        # ``domain`` stamps -- override kicks in.
        edges = out["cross_domain_edges"]
        assert len(edges) == 1, (
            f"P1-A regression: prepass dropped all cross-domain edges "
            f"on fresh-run state (modules[*].domain=='\"\"'). The "
            f"producers should have consulted ``domains_override``. "
            f"Got edges={edges!r}."
        )
        edge = edges[0]
        assert edge["from_script"] == "door_sid"
        assert edge["to_script"] == "anim_sid"
        assert edge["from_domain"] == "client"
        assert edge["to_domain"] == "server"


    def test_multi_door_cross_domain_no_invariant_abort(self) -> None:
        """P1-B regression guard (R1 fix, 2026-05-31): two component-ref
        cross-domain edges sharing the SAME ``<owner>_Set<Field>``
        event_name derivation (e.g. two Door MonoBehaviours each
        referencing an Animator on the same ``open`` field) AND the
        same ``payload.attribute_name="open"`` are intentional reuse
        of one logical bridge across multiple instances, NOT a
        semantic collision.

        Pre-R1 this aborted any scene with more than one cross-domain
        Door reference -- a common case in any multi-door level.
        """
        # Two doors both ref an Animator on field 'open'. Both edges
        # derive event_name='Door_SetOpen' and payload.attribute_name='open'.
        edge_one = {
            "id": "deterministic::edge::1",
            "kind": "attribute_write",
            "from_instance": "S:1", "to_instance": "S:2",
            "from_script": "door_sid", "to_script": "anim_sid",
            "field": "open",
            "from_domain": "client", "to_domain": "server",
            "owner_kind": "scene", "owner_ref": "X.unity",
            "resolution": {
                "strategy": "remote_event_bridge",
                "event_name": "Door_SetOpen",
            },
            "bridge_member_scripts": [
                {"role": "client_caller", "ref": "door_sid"},
                {"role": "server_listener",
                 "ref": "__bridge_listener_server__Door_SetOpen"},
                {"role": "anim_listener", "ref": "anim_sid"},
            ],
            "payload": {"attribute_name": "open", "schema": "unknown"},
        }
        edge_two = {
            **edge_one,
            "id": "deterministic::edge::2",
            "from_instance": "S:3", "to_instance": "S:4",
        }

        sr = _mk_artifact(modules={
            "door_sid": {
                "stem": "Door", "class_name": "Door",
                "runtime_bearing": True, "domain": "client",
                "character_attached": False, "is_loader": False,
            },
            "anim_sid": {
                "stem": "Anim", "class_name": "Anim",
                "runtime_bearing": True, "domain": "server",
                "character_attached": False, "is_loader": False,
            },
        })
        # No abort -- both edges describe the same logical bridge.
        artifact = build_topology(
            scene_runtime=sr,
            emitted_animations=[],
            scripts_by_class={
                "Door": _mk_rbx_script("Door", "LocalScript"),
                "Anim": _mk_rbx_script("Anim", "Script"),
            },
            cross_domain_edges_input=cast(
                "list[object]", [edge_one, edge_two],
            ),
            shared_flag_channels_input=_failopen_sfc(),
        )
        edges_block = artifact["cross_domain_edges"]
        assert len(edges_block) == 2


    def test_synthesize_listener_id_deterministic(self) -> None:
        """The synthesized listener id is stable across calls given
        the same ``(event_name, direction)`` pair. Slice 3's emitter
        must produce the same id slice 2 writes into
        ``bridge_member_scripts[*].ref`` -- this test pins that
        determinism so a hash/seed/random slipping into the helper
        would fail loudly.

        R2 (2026-05-31): the helper now takes a ``direction`` kwarg
        and emits per-direction prefixes; this test pins both shapes.
        """
        from converter.scene_runtime_topology.bridge_emit import (
            SYNTHESIZED_CLIENT_LISTENER_ID_PREFIX,
            SYNTHESIZED_SERVER_LISTENER_ID_PREFIX,
            synthesize_listener_id,
        )

        # client -> server direction: server listener prefix.
        a1 = synthesize_listener_id(
            "PickupItemEvent", direction="client_to_server",
        )
        a2 = synthesize_listener_id(
            "PickupItemEvent", direction="client_to_server",
        )
        assert a1 == a2
        assert a1.startswith(SYNTHESIZED_SERVER_LISTENER_ID_PREFIX)
        # Different event_names produce different ids.
        b = synthesize_listener_id(
            "Door_SetOpen", direction="client_to_server",
        )
        assert b != a1
        assert b.startswith(SYNTHESIZED_SERVER_LISTENER_ID_PREFIX)
        # server -> client direction: client listener prefix (distinct
        # from the server prefix so dumps make direction visible).
        c = synthesize_listener_id(
            "PickupItemEvent", direction="server_to_client",
        )
        assert c.startswith(SYNTHESIZED_CLIENT_LISTENER_ID_PREFIX)
        assert c != a1

    def test_derive_event_name_rejects_empty_field(self) -> None:
        """Slice 2 P3 carry-forward from slice 1: a component-ref edge
        whose ``field`` is empty must NOT produce a fragile
        ``<owner>_Set`` event name. The producer drops the row;
        ``_derive_event_name_from_owner_field`` returns ``None`` on
        the empty input.
        """
        from converter.scene_runtime_topology.cross_domain_edges import (
            _derive_event_name_from_owner_field,
        )

        # Helper-level: empty field -> None.
        assert _derive_event_name_from_owner_field("Door", "") is None
        # Non-empty field still works.
        assert _derive_event_name_from_owner_field("Door", "open") == (
            "Door_SetOpen"
        )

        # End-to-end: a plan whose only reference has ``field=""`` emits
        # ZERO edges (the row is dropped, not coerced to ``Door_Set``).
        plan = _mk_edge_artifact(
            src_class="Door", field="",
            src_domain="client", tgt_domain="server",
        )
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        assert edges == []

    def test_producers_use_sorted_iteration_order(self) -> None:
        """``compute_cross_domain_edges`` iterates scenes in SORTED key
        order so its output is byte-stable across upstream dict
        insertion-order changes.

        We synthesize a plan with two scenes inserted in REVERSE
        alphabetic order; the producer emits rows in ALPHABETIC order.
        """
        plan: dict[str, object] = {
            "modules": {
                "door_sid": {
                    "stem": "Door", "class_name": "Door",
                    "runtime_bearing": True, "domain": "client",
                    "module_path": "ReplicatedStorage.Door",
                },
                "anim_sid": {
                    "stem": "Anim", "class_name": "Anim",
                    "runtime_bearing": True, "domain": "server",
                    "module_path": "ReplicatedStorage.Anim",
                },
                "pickup_sid": {
                    "stem": "Pickup", "class_name": "Pickup",
                    "runtime_bearing": True, "domain": "client",
                    "module_path": "ReplicatedStorage.Pickup",
                },
            },
            # Insertion order = ['Z.unity', 'A.unity']; sorted = ['A', 'Z'].
            "scenes": {
                "Z.unity": {
                    "instances": [
                        {"instance_id": "Z.unity:1", "script_id": "door_sid",
                         "game_object_id": "Z.unity:1", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "Z.unity:2", "script_id": "anim_sid",
                         "game_object_id": "Z.unity:2", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "Z.unity:3", "script_id": "pickup_sid",
                         "game_object_id": "Z.unity:3", "active": True,
                         "enabled": True, "config": {"itemName": "Z"}},
                    ],
                    "references": [{
                        "from": "Z.unity:1", "field": "open",
                        "index": None, "target_kind": "component",
                        "target_ref": "Z.unity:2", "target_is_ui": False,
                    }],
                    "lifecycle_order": [
                        "Z.unity:1", "Z.unity:2", "Z.unity:3",
                    ],
                },
                "A.unity": {
                    "instances": [
                        {"instance_id": "A.unity:1", "script_id": "door_sid",
                         "game_object_id": "A.unity:1", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "A.unity:2", "script_id": "anim_sid",
                         "game_object_id": "A.unity:2", "active": True,
                         "enabled": True, "config": {}},
                        {"instance_id": "A.unity:3", "script_id": "pickup_sid",
                         "game_object_id": "A.unity:3", "active": True,
                         "enabled": True, "config": {"itemName": "A"}},
                    ],
                    "references": [{
                        "from": "A.unity:1", "field": "open",
                        "index": None, "target_kind": "component",
                        "target_ref": "A.unity:2", "target_is_ui": False,
                    }],
                    "lifecycle_order": [
                        "A.unity:1", "A.unity:2", "A.unity:3",
                    ],
                },
            },
            "prefabs": {},
            "domain_overrides": {},
        }
        edges = compute_cross_domain_edges(plan)  # type: ignore[arg-type]
        # Component-ref edges: ``A.unity`` first (sorted key), then ``Z.unity``.
        assert [e["owner_ref"] for e in edges] == ["A.unity", "Z.unity"]


# ===========================================================================
# CATEGORY 5c: Phase 2b reframe (2026-06-01) — shared_flag_channels (Class 2)
# ===========================================================================


def _mk_transpiled(
    output_filename: str,
    luau_source: str,
    script_type: str = "Script",
):
    """One ``TranspiledScript`` for the reader scan."""
    from converter.code_transpiler import TranspiledScript

    return TranspiledScript(
        source_path=output_filename.replace(".luau", ".cs"),
        output_filename=output_filename,
        csharp_source="// src",
        luau_source=luau_source,
        strategy="ai",
        confidence=1.0,
        script_type=script_type,
    )


class TestSharedFlagChannels:
    """Phase 2b reframe Class 2: the ``PlayerSetSharedFlag`` funnel
    channel fact recorded by ``compute_shared_flag_channels``.

    The funnel write is client-originated (``mirrorFlag`` fires from the
    client; the server's ``OnServerEvent`` listener sets the attribute),
    so a ``GetAttribute("...")`` read qualifies as cross-domain when its
    reader resolves to a SERVER domain. The fact is recompute-only
    (``caller_graph``-style, never persisted) and FAILS OPEN on a
    no-transpile resume so the step-2 gate never disables a needed funnel.

    Refs: shared_flag_channels.py; design doc Phase 2b deliverable 2.
    """

    def test_shared_flag_channels_recorded_from_reader_scan(self) -> None:
        """A server-domain ``GetAttribute("hasKey")`` reader puts
        ``hasKey`` in ``read_names`` and sets ``present: True``."""
        scripts = [
            _mk_transpiled("Door.luau", 'local v = part:GetAttribute("hasKey")'),
        ]
        out = compute_shared_flag_channels(
            transpiled_scripts=scripts,
            script_id_by_name={"Door": "door_sid"},
            domains={"door_sid": "server"},
        )
        channel = out["PlayerSetSharedFlag"]
        assert channel["present"] is True
        assert "hasKey" in channel["read_names"]
        assert channel["reader_domains"] == ["server"]

    def test_shared_flag_channels_matches_get_attribute_changed_signal(
        self,
    ) -> None:
        """A server reader whose ONLY shared-flag access is the SIGNAL
        form ``:GetAttributeChangedSignal("hasKey")`` (no literal
        ``:GetAttribute("hasKey")`` read) is still a cross-domain reader:
        ``hasKey`` lands in ``read_names`` and ``present: True``.

        Pre-fix the scan matched only ``:GetAttribute(`` — the
        ``ChangedSignal`` infix broke that substring AND the regex, so a
        reader that only WATCHES the flag (rather than reading its current
        value) was invisible → ``present: False`` → the funnel was wrongly
        dropped → the watched signal would never fire. Phase 3 deliverable
        #2 explicitly lists ``GetAttributeChangedSignal`` readers.
        Claude R1 P2, 2026-06-01.
        """
        scripts = [
            _mk_transpiled(
                "Door.luau",
                'part:GetAttributeChangedSignal("hasKey"):Connect(fn)',
            ),
        ]
        out = compute_shared_flag_channels(
            transpiled_scripts=scripts,
            script_id_by_name={"Door": "door_sid"},
            domains={"door_sid": "server"},
        )
        channel = out["PlayerSetSharedFlag"]
        assert channel["present"] is True
        assert "hasKey" in channel["read_names"]
        assert channel["reader_domains"] == ["server"]

    def test_shared_flag_channels_empty_when_no_cross_domain_reader(
        self,
    ) -> None:
        """A same-domain (client) reader does NOT qualify: the funnel
        write originates client-side, so a client-side read is not a
        cross-domain read. ``present: False`` and ``read_names: []``."""
        scripts = [
            _mk_transpiled("Hud.luau", 'local v = plr:GetAttribute("hasKey")'),
        ]
        out = compute_shared_flag_channels(
            transpiled_scripts=scripts,
            script_id_by_name={"Hud": "hud_sid"},
            domains={"hud_sid": "client"},
        )
        channel = out["PlayerSetSharedFlag"]
        assert channel["present"] is False
        assert channel["read_names"] == []
        assert channel["reader_domains"] == []

    def test_shared_flag_channels_excludes_non_runtime_reader(self) -> None:
        """A reader whose domain is a NON_RUNTIME domain
        (``"excluded"``/``"helper"``) is never emitted at runtime, so it
        must NOT pollute ``read_names`` or set ``present``. If it is the
        ONLY reader, ``present: False``. Codex R1 P2-A, 2026-06-01."""
        for nonruntime in ("excluded", "helper"):
            scripts = [
                _mk_transpiled(
                    "Dead.luau", 'local v = part:GetAttribute("hasKey")',
                ),
            ]
            out = compute_shared_flag_channels(
                transpiled_scripts=scripts,
                script_id_by_name={"Dead": "dead_sid"},
                domains={"dead_sid": nonruntime},
            )
            channel = out["PlayerSetSharedFlag"]
            assert channel["present"] is False, nonruntime
            assert channel["read_names"] == [], nonruntime
            assert channel["reader_domains"] == [], nonruntime

    def test_shared_flag_channels_rejects_non_runtime_attr_names(
        self,
    ) -> None:
        """The scan must match the RUNTIME ALLOWLIST. The
        ``PlayerSetSharedFlag`` server listener in ``autogen.py`` does
        ``if #flagName > 64 or not string.match(flagName, "^[%w_]+$")
        then return end`` — names with spaces or hyphens are NOT valid
        Roblox attribute names and the runtime DROPS them, so they must
        NOT be recorded. Identifier-safe names (``\\w+``, incl. a leading
        digit) ARE recorded; a name > 64 chars is dropped (length cap).

        SUPERSEDES ``test_shared_flag_channels_matches_non_identifier_flag_names``
        (the slice-2 R4 broadening), which asserted the now-incorrect
        behavior of recording impossible attribute names. Codex R2 P2,
        2026-06-01."""
        long_name = "h" * 65  # > 64 -> runtime drops it
        scripts = [
            _mk_transpiled(
                "Door.luau",
                'local a = part:GetAttribute("hasRed Key")\n'   # space -> dropped
                'local b = part:GetAttribute("hasKey-A")\n'     # hyphen -> dropped
                'local c = part:GetAttribute("has3rdGem")\n'    # \\w+ -> kept
                'local d = part:GetAttribute("hasKey")\n'       # \\w+ -> kept
                f'local e = part:GetAttribute("{long_name}")',  # >64 -> dropped
            ),
        ]
        out = compute_shared_flag_channels(
            transpiled_scripts=scripts,
            script_id_by_name={"Door": "door_sid"},
            domains={"door_sid": "server"},
        )
        channel = out["PlayerSetSharedFlag"]
        assert channel["present"] is True
        # Non-runtime names are NOT recorded (runtime would drop them).
        assert "hasRed Key" not in channel["read_names"]
        assert "hasKey-A" not in channel["read_names"]
        assert long_name not in channel["read_names"]
        # Identifier-safe names ARE recorded.
        assert "has3rdGem" in channel["read_names"]
        assert "hasKey" in channel["read_names"]

    def test_shared_flag_channels_fail_open_on_any_unmappable_reader(
        self,
    ) -> None:
        """An unmappable reader with a qualifying read fails open
        (``present=True``) REGARDLESS of its pre-coherence ``script_type``.

        Codex R2↔R3 oscillation resolution (2026-06-01): the
        ``script_type`` available here is PRE-COHERENCE and NOT
        authoritative — ``_fix_client_server_classification`` can flip
        LocalScript→Script later — so we do NOT use it to suppress
        fail-open. Fail-open is conservative (keeps the funnel, == today's
        unconditional behavior); over-reporting a genuinely-client reader
        is the lesser evil vs wrongly suppressing a reclassified server
        reader. Authoritative domain resolution is deferred (see TODO.md
        pre/post-coherence follow-up). A scene with NO qualifying read
        still yields ``present=False`` — fail-open is specific to
        unmappable-WITH-read, not blanket."""
        for stype in ("LocalScript", "Script", "ModuleScript"):
            out = compute_shared_flag_channels(
                transpiled_scripts=[
                    _mk_transpiled(
                        "Reader.luau",
                        'local v = x:GetAttribute("hasKey")',
                        script_type=stype,
                    ),
                ],
                script_id_by_name={},  # unmappable
                domains={},
            )
            assert out["PlayerSetSharedFlag"]["present"] is True, stype
            # No domain to attribute it to -> name set stays unpolluted.
            assert out["PlayerSetSharedFlag"]["read_names"] == []

        # Specificity guard: unmappable reader with NO GetAttribute read
        # -> still present=False (fail-open is not blanket).
        none = compute_shared_flag_channels(
            transpiled_scripts=[
                _mk_transpiled("Quiet.luau", "local v = 1", script_type="Script"),
            ],
            script_id_by_name={},
            domains={},
        )
        assert none["PlayerSetSharedFlag"]["present"] is False

    def test_shared_flag_channels_two_reads_on_one_line(self) -> None:
        """Two ``GetAttribute`` reads on one line are captured as two
        separate names, not merged into one span. Trivially safe with the
        ``\\w+`` capture, but pin it. Claude R2 P3 follow-up."""
        scripts = [
            _mk_transpiled(
                "Door.luau",
                'a:GetAttribute("hasA") b:GetAttribute("hasB")',
            ),
        ]
        out = compute_shared_flag_channels(
            transpiled_scripts=scripts,
            script_id_by_name={"Door": "door_sid"},
            domains={"door_sid": "server"},
        )
        assert out["PlayerSetSharedFlag"]["read_names"] == ["hasA", "hasB"]

    def test_shared_flag_channels_fail_open_on_unmappable_reader(
        self,
    ) -> None:
        """An unmappable reader (name absent from ``script_id_by_name``,
        e.g. dropped by ``build_script_id_by_name`` on a class/stem
        collision) that HAS a qualifying ``GetAttribute`` read must NOT
        become negative evidence. As the only reader it FAILS OPEN:
        ``present: True``; ``read_names`` stays empty (unattributable).
        A scene with NO GetAttribute reads still yields ``present: False``
        on a fresh run, so fail-open is specific to the
        unmappable-but-has-read case. Codex R1 P3, 2026-06-01."""
        # Unmappable reader with a qualifying read -> fail open.
        scripts = [
            _mk_transpiled(
                "Ambiguous.luau", 'local v = part:GetAttribute("hasKey")',
            ),
        ]
        out = compute_shared_flag_channels(
            transpiled_scripts=scripts,
            script_id_by_name={},  # name not present -> unmappable
            domains={},
        )
        channel = out["PlayerSetSharedFlag"]
        assert channel["present"] is True
        assert channel["read_names"] == []

        # Fresh run, no GetAttribute reads anywhere -> stays False
        # (fail-open is NOT blanket).
        no_reads = compute_shared_flag_channels(
            transpiled_scripts=[
                _mk_transpiled("Plain.luau", "local v = 1 + 2"),
            ],
            script_id_by_name={"Plain": "plain_sid"},
            domains={"plain_sid": "server"},
        )
        assert no_reads["PlayerSetSharedFlag"]["present"] is False

    def test_shared_flag_channels_resume_fail_open(self) -> None:
        """On a no-transpile resume (``transpiled_scripts is None``) the
        scan source is absent. The fact FAILS OPEN: ``present: True``
        with empty ``read_names`` so the step-2 gate does NOT disable the
        funnel on missing evidence."""
        out = compute_shared_flag_channels(
            transpiled_scripts=None,
            script_id_by_name={},
            domains={},
        )
        channel = out["PlayerSetSharedFlag"]
        assert channel["present"] is True
        assert channel["read_names"] == []

    def test_canonical_stores_constant(self) -> None:
        """``canonical_stores`` is the CONSTANT ``["Character", "Player"]``
        (sorted) — what the funnel writes per autogen.py:174-176 — NOT a
        per-read derivation. Holds on both fresh and resume paths."""
        fresh = compute_shared_flag_channels(
            transpiled_scripts=[
                _mk_transpiled(
                    "Door.luau", 'local v = part:GetAttribute("hasKey")',
                ),
            ],
            script_id_by_name={"Door": "door_sid"},
            domains={"door_sid": "server"},
        )
        resume = compute_shared_flag_channels(
            transpiled_scripts=None, script_id_by_name={}, domains={},
        )
        assert fresh["PlayerSetSharedFlag"]["canonical_stores"] == [
            "Character", "Player",
        ]
        assert resume["PlayerSetSharedFlag"]["canonical_stores"] == [
            "Character", "Player",
        ]

    def test_shared_flag_channels_not_persisted(self) -> None:
        """Recompute-only contract (``caller_graph``-style): the fact is
        rebuilt from the live reader scan every run, NEVER read back from
        a prior on-disk plan as authoritative. There is no
        ``shared_flag_channels`` field on ``StoragePlan`` and no
        ``preserved_shared_flag_channels`` path on ``build_topology``.

        Proof: a prior topology block on ``scene_runtime`` carrying a
        STALE ``shared_flag_channels`` is IGNORED — the prepass recomputes
        from the current scan and the result reflects the CURRENT reader
        set, not the stale persisted one.
        """
        # StoragePlan must not carry the field (recompute-only).
        from converter.storage_classifier import StoragePlan
        import dataclasses

        plan_fields = {f.name for f in dataclasses.fields(StoragePlan)}
        assert "shared_flag_channels" not in plan_fields

        # build_topology has no preserve path for the fact.
        import inspect
        sig = inspect.signature(build_topology)
        assert "preserved_shared_flag_channels" not in sig.parameters

        # Recompute beats stale persisted value: drive the prepass with a
        # prior topology block carrying a STALE present=True channel, but
        # a fresh scan with NO cross-domain reader. The output must
        # reflect the fresh scan (present=False), not the stale block.
        from converter.pipeline import Pipeline
        helper = TestPhase2bSlice2EnrichmentAndRelocation
        hud = RbxScript(
            name="Hud",
            source="-- client",
            script_type="LocalScript",
        )
        scene_runtime: dict[str, object] = {
            "modules": {
                "hud_sid": {
                    "stem": "Hud", "class_name": "Hud",
                    "runtime_bearing": True, "domain": "client",
                    "character_attached": False, "is_loader": False,
                    "module_path": "ReplicatedStorage.Hud",
                },
            },
            "scenes": {},
            "prefabs": {},
            "domain_overrides": {},
            # STALE persisted topology block — must be ignored.
            "topology": {
                "shared_flag_channels": {
                    "PlayerSetSharedFlag": {
                        "read_names": ["hasStaleFlag"],
                        "reader_domains": ["server"],
                        "canonical_stores": ["Character", "Player"],
                        "present": True,
                    },
                },
            },
        }
        # Fresh transpile whose only read is a CLIENT-domain read (not
        # cross-domain) -> recompute yields present=False.
        transpiled = [
            _mk_transpiled("Hud.luau", 'local v = plr:GetAttribute("hasKey")'),
        ]
        pipeline = helper._build_pipeline_with(
            scripts=[hud],
            transpilation_result_scripts=transpiled,
        )
        out = Pipeline._maybe_run_topology_prepass(pipeline, scene_runtime)
        assert out is not None
        channel = out["shared_flag_channels"]["PlayerSetSharedFlag"]
        # Recomputed from the fresh scan, NOT the stale persisted block.
        assert channel["present"] is False
        assert "hasStaleFlag" not in channel["read_names"]
