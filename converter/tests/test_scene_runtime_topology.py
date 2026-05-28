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
)
from converter.scene_runtime_topology.cross_domain_edges import (  # noqa: E402
    deterministic_edge_id,
)
from converter.scene_runtime_topology.lifecycle_roles import (  # noqa: E402
    LIFECYCLE_ROLES,
    derive_module_lifecycle_role,
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
    return RbxScript(name=name, source="-- empty", script_type=script_type)


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

    Phase 1's narrowing only resolves animation drivers whose owning
    MonoBehaviour has a serialized ``[SerializeField] Animator`` field
    captured by the scene-runtime planner's reference walk
    (``scene_runtime_planner._split_config_and_refs``). MBs that access
    their Animator via a property / runtime getter (e.g.
    ``transform.parent.Find("door").GetComponent<Animator>()`` in
    SimpleFPS's Door.cs) have no serialized ref, so Phase 1's
    ``resolve_driver`` (animation_routing.py:309-346) returns ``None``
    and the driver lands ``routing_status="unresolved"`` with a
    server-safe fallback placement. Phase 2's C#-source narrowing
    closes that gap (design doc §Phase 2a + the resolver docstring).

    Today SimpleFPS happens to have zero MBs with serialized Animator
    fields — Door, HostilePlane, and PlaneHolder all use property /
    runtime-getter access. So this test asserts the SAFETY-OF-FALLBACK
    contract rather than the resolution contract:

      1. ``scene_runtime.topology`` block emitted under generic mode,
         with non-empty ``animation_drivers``.
      2. ``topology.modules`` includes Door with ``stem="Door"`` and
         ``domain="client"`` — proves the v2 classifier sees Door as
         client-domain even though the animation driver can't yet be
         resolved to it.
      3. Every driver carries an EXPLICIT ``routing_status`` from
         ``{"resolved","unresolved","orphan"}`` — no ``__orphan__``
         sentinels (codex B1 fix).
      4. For every emitted ``Anim_*`` row, the live ``RbxScript``
         placement is consistent with the topology decision:
           * ``resolved + client → LocalScript`` in
             ``StarterPlayer.StarterPlayerScripts``,
           * ``resolved + server → Script`` in ``ServerScriptService``,
           * ``unresolved / orphan → Script`` in
             ``ServerScriptService`` (Phase 1's safe fallback).
      5. Invariant 3 holds: no duplicate ``Anim_*`` names in
         ``rbx_place.scripts`` (the topology artifact's ``stable_id``
         keying makes the double-emission from
         ``_attach_prefab_scoped_animation_scripts_to_templates`` +
         ``_attach_monobehaviour_scripts_to_templates`` structurally
         impossible).

    When Phase 2 lands a serialized- or source-narrowed Door driver,
    update this test to additionally assert Door's drivers go
    resolved + client + LocalScript.
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
    # domain='client'. Phase 1 can't yet route Door's animation driver
    # (property-based Animator access — Phase 2's job), but the
    # classifier itself must still see Door as client-domain so once
    # the narrowing extension lands the topology decision flips
    # mechanically.
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
    # Driver-status contract: each family MUST have ≥1 emitted row;
    # EVERY row in that family MUST be ``routing_status="unresolved"``
    # today (Phase 1 narrowing limit). Why per-family rather than
    # global: only these 3 families are documented as the canonical
    # SimpleFPS broken set (design doc §Phase 1 + scene-runtime-pr148-
    # followups.md). Other autoplay clips a future SimpleFPS asset
    # update might add can be either resolved or unresolved without
    # invalidating Phase 1's contract — only the named families
    # carry Phase 1's "intended-permanent-server" classification
    # (HostilePlane/PlaneHolder) or "Phase-2-will-fix" classification
    # (Door).
    #
    # When Phase 2 source-narrowing lands, change the ``"unresolved"``
    # gate to ``"resolved"`` for the Door family AND assert
    # LocalScript + StarterPlayer.StarterPlayerScripts placement.
    # HostilePlane + PlaneHolder remain unresolved + server (their
    # intended-permanent state — see followup doc).
    # ------------------------------------------------------------------
    _expected_unresolved_anim_prefixes = (
        "Anim_Door_",
        "Anim_HostilePlane_",
        "Anim_PlaneHolder_",
    )
    sid_by_script_name: dict[str, str] = {
        script_name: sid
        for sid, script_name in script_name_by_stable_id.items()
        if script_name
    }
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
                f"'unresolved' — Phase 1 narrowing limit. When Phase "
                f"2 source-narrowing lands this assertion needs "
                f"updating for the Door family; see "
                f"scene-runtime-pr148-followups.md)"
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
        pipeline.state = SimpleNamespace(
            rbx_place=rbx_place,
            animation_result=anim_result,
            guid_index=None,
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

    def test_empty_emitted_animations_skips_topology_build(
        self, tmp_path: Path,
    ) -> None:
        """When ``animation_result.emitted_animations`` is empty, the
        method short-circuits before calling build_topology. Verifies
        scene_runtime doesn't get a spurious ``topology`` key written.
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
        # No topology key persisted; no RbxScript mutated.
        assert "topology" not in scene_runtime
