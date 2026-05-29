"""Tests: Phase 2a slice 5 round 3 — build→classify→rehydrate→re-build
parity for the topology artifact's ``script_class`` field.

The round-3 fix closed a cycle round 2 left open: ``RbxScript`` was
constructed by ``_rehydrate_scripts_from_disk`` without an
``intrinsic_script_type`` stamp, so on resume the
``derive_intrinsic_script_class`` fallback read the post-classifier
``script_type`` and produced a different ``script_class`` than the
fresh build had produced. Specifically:

  1. Fresh build: transpile stamps ``intrinsic_script_type="Script"``;
     ``classify_storage`` mutates ``script_type`` to ``LocalScript``
     (StarterPlayer coercion); topology's ``script_class`` reads
     intrinsic → ``"Script"``.
  2. Resume: rehydration reads ``script_type`` from the post-classifier
     bucket map → ``LocalScript``; ``intrinsic_script_type`` left
     ``None`` → fallback fires → topology's ``script_class`` becomes
     ``"LocalScript"``.

That's the cycle slice 5 set out to break. The fix: persist
``intrinsic_script_type`` on each ``StoragePlan.decisions[]`` row at
classify time, then restore it on every ``RbxScript`` constructed by
``_rehydrate_scripts_from_disk``. Build→classify→rehydrate→re-build
must now yield byte-identical ``script_class`` for every module.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.scene_runtime_planner import build_scripts_by_class_name  # noqa: E402
from converter.scene_runtime_topology.build_topology import (  # noqa: E402
    SceneRuntimeArtifact,
    build_topology,
)
from converter.storage_classifier import classify_storage  # noqa: E402
from core.roblox_types import RbxPlace, RbxScript  # noqa: E402


def _make_pipeline(tmp_path: Path):
    """Helper mirrors test_rehydration_plan._make_pipeline."""
    from converter.pipeline import Pipeline

    project = tmp_path / "proj"
    (project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir(parents=True)
    pipeline = Pipeline(
        unity_project_path=project, output_dir=output, skip_upload=True,
    )
    pipeline.state.rbx_place = RbxPlace()
    return pipeline


def _mk_scene_runtime() -> SceneRuntimeArtifact:
    """One client-domain module whose script_class WILL be coerced by
    classify_storage from ``Script`` → ``LocalScript``.

    ``HudControl`` matches the client-only API pattern (it uses
    ``Players.LocalPlayer``) but the transpiler stamps it as ``Script``
    intrinsically. ``classify_storage`` then mutates ``script_type``
    to ``LocalScript`` because the regex client-only pass demotes it
    into ``StarterPlayer.StarterPlayerScripts``. The topology layer's
    ``script_class`` must surface the INTRINSIC ``Script``, not the
    coerced ``LocalScript`` — both on fresh build and on rebuild after
    persist+rehydrate.

    A second ModuleScript ``Settings`` is included as a control: it
    is not coerced (ModuleScripts pass through untouched) so its
    parity is the trivial case.
    """
    return cast(SceneRuntimeArtifact, {
        "modules": {
            "guid-hud": {
                "stem": "HudControl",
                "class_name": "HudControl",
                "runtime_bearing": True,
                "domain": "client",
                "character_attached": False,
                "is_loader": False,
            },
            "guid-settings": {
                "stem": "Settings",
                "class_name": "Settings",
                "runtime_bearing": True,
                "domain": "client",
                "character_attached": False,
                "is_loader": False,
            },
        },
        "scenes": {},
        "prefabs": {},
        "domain_overrides": {},
    })


def _mk_scripts() -> list[RbxScript]:
    """Construct the two scripts the way the transpiler does — with
    intrinsic_script_type stamped at construction time.

    HudControl: intrinsic Script + client-only API in source so
    classify_storage coerces script_type → LocalScript.
    Settings: ModuleScript (no coercion path).
    """
    return [
        RbxScript(
            name="HudControl",
            source=(
                "local Players = game:GetService('Players')\n"
                "local player = Players.LocalPlayer\n"
                "print(player.Name)\n"
            ),
            script_type="Script",
            intrinsic_script_type="Script",
        ),
        RbxScript(
            name="Settings",
            source="local M = {}\nreturn M\n",
            script_type="ModuleScript",
            intrinsic_script_type="ModuleScript",
        ),
    ]


def test_topology_script_class_survives_classify_rehydrate_cycle(
    tmp_path: Path,
) -> None:
    """The core round-3 invariant: build→classify→persist→rehydrate
    →re-build yields byte-identical ``script_class`` for every module.

    Fixture is purpose-built so the cycle has a witness: ``HudControl``
    is intrinsically a ``Script`` but gets coerced to ``LocalScript``
    by classify_storage's post-decision auto-correction (originally
    triggered by the regex client-only-API pass; slice 7 migrated the
    witness to ``lifecycle_role == "character_attached"`` which routes
    to ``StarterCharacterScripts`` and triggers the same coercion).
    Pre-round-3 the rebuild's ``script_class`` flipped to
    ``"LocalScript"`` (round-2 fallback path) — this test fails on
    that regression. Post-round-3 it stays at ``"Script"`` because the
    intrinsic survives the round trip on the ``StoragePlan.decisions[]``
    row.
    """
    pipeline = _make_pipeline(tmp_path)
    scripts = _mk_scripts()

    # ---- STEP 1: snapshot intrinsic values BEFORE classify_storage. ----
    # The fresh build's contract: classify_storage may mutate
    # ``script_type`` but never ``intrinsic_script_type``. This snapshot
    # is the invariant the round-trip must preserve.
    intrinsic_pre = {s.name: s.intrinsic_script_type for s in scripts}

    # Phase 2a slice 7 (2026-05-30): the regex-API client-only paths
    # were deleted. The "Script -> LocalScript coercion" witness now
    # uses ``lifecycle_role == "character_attached"`` (a topology
    # signal) to route HudControl to StarterCharacterScripts, which
    # triggers the post-decision auto-correction (Script ->
    # LocalScript) in classify_storage. Same mutation surface, same
    # rehydration round-trip; different upstream trigger.
    from converter.scene_runtime_topology.module_domain import (
        TopologyInputs,
    )
    topology_inputs: TopologyInputs = {
        "domains": {"guid-hud": "client", "guid-settings": "client"},
        "reachability_requirements": {},
        "lifecycle_roles": {"guid-hud": "character_attached"},
        "script_id_by_name": {
            "HudControl": "guid-hud", "Settings": "guid-settings",
        },
        "caller_graph": {},
        "transpile_ran": True,
    }

    # ---- STEP 2: classify (mutates script_type but not intrinsic). ----
    plan = classify_storage(scripts, topology_inputs=topology_inputs)
    # Witness: classify_storage demoted HudControl's mutable script_type
    # to LocalScript (StarterCharacter container under slice 7's
    # topology path) while leaving the immutable intrinsic_script_type
    # at the transpiler's "Script".
    hud = next(s for s in scripts if s.name == "HudControl")
    assert hud.script_type == "LocalScript", (
        "fixture invariant: HudControl must trip the character_attached "
        "coercion path so the parity test has a witness"
    )
    assert hud.intrinsic_script_type == "Script", (
        "intrinsic_script_type is immutable across classify_storage"
    )

    # ---- STEP 3: build the FIRST topology artifact. ----
    sr_first = _mk_scene_runtime()
    scripts_by_class_first = build_scripts_by_class_name(
        scripts, cast("dict[str, object]", sr_first["modules"]),
    )
    artifact_first = build_topology(
        scene_runtime=sr_first,
        emitted_animations=[],
        scripts_by_class=scripts_by_class_first,
    )
    script_class_first = {
        sid: row["script_class"]
        for sid, row in artifact_first["modules"].items()
    }
    # Sanity: the artifact reflects the intrinsic — HudControl is
    # ``"Script"`` even though script_type was mutated to LocalScript.
    assert script_class_first == {
        "guid-hud": "Script",
        "guid-settings": "ModuleScript",
    }

    # ---- STEP 4: persist the StoragePlan exactly as the pipeline does.
    # The persisted shape must include the intrinsic_script_type field
    # on every decisions[] row (round-3 deliverable 1). Without it,
    # rehydration has no way to restore the immutable stamp.
    plan_path = pipeline.output_dir / "conversion_plan.json"
    plan_path.write_text(
        json.dumps({"storage_plan": plan.to_dict()}, indent=2),
        encoding="utf-8",
    )
    persisted = json.loads(plan_path.read_text(encoding="utf-8"))
    decisions_by_name = {
        row["script"]: row
        for row in persisted["storage_plan"]["decisions"]
    }
    assert decisions_by_name["HudControl"]["intrinsic_script_type"] == "Script"
    assert decisions_by_name["Settings"]["intrinsic_script_type"] == "ModuleScript"
    # And the mutable column shows the coercion (so the test is
    # genuinely demonstrating the two fields diverge on disk).
    assert decisions_by_name["HudControl"]["script_type"] == "LocalScript"

    # ---- STEP 5: rehydrate. Write the .luau bodies + read them back. ----
    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    for s in scripts:
        (scripts_dir / f"{s.name}.luau").write_text(s.source, encoding="utf-8")
    pipeline._rehydrate_scripts_from_disk(scripts_dir)
    rehydrated = pipeline.state.rbx_place.scripts
    rehydrated_by_name = {s.name: s for s in rehydrated}

    # The crucial assertion for round 3: the rehydrated RbxScript
    # carries the intrinsic_script_type that the transpile-time
    # snapshot captured. Pre-round-3 this would be ``None`` for both
    # scripts (rehydration never stamped it).
    for name, expected in intrinsic_pre.items():
        assert rehydrated_by_name[name].intrinsic_script_type == expected, (
            f"{name}: rehydrated intrinsic_script_type "
            f"{rehydrated_by_name[name].intrinsic_script_type!r} "
            f"!= pre-classify intrinsic {expected!r}"
        )

    # Witness: rehydrated ``script_type`` is the post-classifier value
    # (LocalScript for HudControl) — that's the bug condition. The
    # round-3 fix means the topology layer should NOT be confused by
    # this divergence because it reads the intrinsic field.
    assert rehydrated_by_name["HudControl"].script_type == "LocalScript"

    # ---- STEP 6: rebuild the topology artifact from rehydrated scripts. -
    sr_second = _mk_scene_runtime()
    scripts_by_class_second = build_scripts_by_class_name(
        rehydrated, cast("dict[str, object]", sr_second["modules"]),
    )
    artifact_second = build_topology(
        scene_runtime=sr_second,
        emitted_animations=[],
        scripts_by_class=scripts_by_class_second,
    )
    script_class_second = {
        sid: row["script_class"]
        for sid, row in artifact_second["modules"].items()
    }

    # ---- STEP 7: parity. ----
    # The rebuild's script_class MUST be byte-identical to the fresh
    # build's. Pre-round-3 the HudControl entry would flip from
    # "Script" to "LocalScript" because the intrinsic field was lost
    # on rehydration — that regression is exactly what this test
    # blocks.
    assert script_class_second == script_class_first, (
        "build→classify→persist→rehydrate→re-build broke "
        "script_class parity. The intrinsic_script_type round-trip "
        "is the load-bearing invariant — verify the round-3 stamp "
        "+ rehydration plumbing in storage_classifier.py and "
        "pipeline._load_storage_plan_for_rehydration."
    )
    # Spell out the specific Script→LocalScript-coerced witness so a
    # future reader sees what regression the test guards against.
    assert script_class_second["guid-hud"] == "Script"


def test_rehydration_falls_back_when_decisions_lack_intrinsic(
    tmp_path: Path,
) -> None:
    """Backward-compat guard: a plan written by a pre-round-3 converter
    has ``decisions[]`` rows without ``intrinsic_script_type``. The
    rehydrate path must leave ``RbxScript.intrinsic_script_type`` unset
    (``None``) so the documented heuristic fallback in
    ``derive_intrinsic_script_class`` still applies — exactly the
    behaviour rounds 1-2 shipped.
    """
    pipeline = _make_pipeline(tmp_path)
    # Pre-round-3 plan shape: decisions[] rows omit
    # ``intrinsic_script_type``. Buckets carry the post-classifier
    # script_type as before.
    legacy_plan = {
        "storage_plan": {
            "server_scripts": ["Legacy"],
            "client_scripts": [],
            "character_scripts": [],
            "replicated_first_scripts": [],
            "shared_modules": [],
            "server_modules": [],
            "decisions": [
                {
                    "script": "Legacy",
                    "script_type": "Script",
                    "container": "ServerScriptService",
                    "reason": "server Script (default)",
                    "source": "classifier",
                },
            ],
        },
    }
    (pipeline.output_dir / "conversion_plan.json").write_text(
        json.dumps(legacy_plan), encoding="utf-8",
    )

    scripts_dir = pipeline.output_dir / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "Legacy.luau").write_text("print('legacy')\n", encoding="utf-8")
    pipeline._rehydrate_scripts_from_disk(scripts_dir)

    rehydrated = pipeline.state.rbx_place.scripts[0]
    assert rehydrated.name == "Legacy"
    assert rehydrated.script_type == "Script"
    # Pre-round-3 row → field unset → fallback path stays available.
    assert rehydrated.intrinsic_script_type is None
