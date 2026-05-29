"""Phase 2a slice 8 — autogen-classify gap acceptance gate.

Design doc (``scene-runtime-architecture-ir.md`` §"Slice 8") declares:

> **Acceptance gate (CRITICAL):** golden-output diff over bundled test
> projects shows **zero** ``parent_path`` drift on autogen / runtime-
> injection scripts. Autogen scripts (``GameServerManager``,
> ``CollisionGroupSetup``, ``NavAgent``, ``EventSystem``,
> ``CinemachineRuntime``, etc.) get appended AFTER classify today; the
> lift means they're either never classified or need a follow-on
> classify-pass before ``write_output``.

Slice 8 chose Option (b) — the late-append safety net
(``_classify_late_appended_scripts``) stamps the rbxlx_writer default
container on any script whose generator left ``parent_path = None``.

Round 2 hardening (Option a from the R1 review): the test exercises
the REAL ``_subphase_inject_*`` paths so a new autogen factory wired
into one of those subphases is caught automatically by the drift
check — not silently dropped because the test fixture didn't know to
list it. The synthetic ``RbxScript`` stubs the R1 test used couldn't
detect that drift mode.

The pre-injection state below is engineered to trip every detector
in the three injection subphases:

- ``_subphase_inject_autogen_scripts``:
  - ``UnityLayer`` attribute on a part → ``CollisionGroupSetup``.
  - ``GameServerManager`` always emits (no precondition).
  - MeshPart with ``collision_fidelity != 0`` and a ``mesh_id`` →
    ``CollisionFidelityRecook``.
  - ``_MainCameraRig`` attribute on a part → ``CameraRigFollower``.
  - A user ModuleScript with ``RenderStepped:Connect`` →
    ``ClientBootstrap`` (LocalScript that requires side-effect modules).
- ``_inject_runtime_modules``:
  - ``_HasNavMeshAgent`` attribute on a part → ``NavAgent``.
  - At least one ``RbxScreenGui`` in the place → ``EventSystem``.
  - ``_HasCharacterController`` attribute → ``CharacterBridge``.
  - ``_HasSubEmitters`` attribute on a particle emitter →
    ``SubEmitterRuntime``.
  - A user script with object-pool patterns (``pool.GetNew`` etc.)
    → ``ObjectPool``.
  - ``CinemachineVCam`` attribute → ``CinemachineRuntime``.
- ``_subphase_inject_scene_runtime``:
  - ``ctx.scene_runtime_mode == "generic"`` AND ``ctx.scene_runtime``
    carries at least one runtime-bearing module → ``SceneRuntime``,
    ``SceneRuntimePlan``, ``SceneRuntimeClient``, ``SceneRuntimeServer``.

After all three injection subphases run on this engineered state,
``_classify_late_appended_scripts`` runs and the golden table below
pins every known autogen / runtime / scene-runtime script's
``script_type`` + ``parent_path``. ANY drift fails this gate with a
concrete diff.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.pipeline import Pipeline  # noqa: E402
from core.roblox_types import (  # noqa: E402
    RbxPart,
    RbxParticleEmitter,
    RbxPlace,
    RbxScreenGui,
    RbxScript,
)


# ---------------------------------------------------------------------------
# Golden table: every well-known autogen / runtime / scene-runtime
# script the converter injects, paired with the parent_path the
# rbxlx_writer default routing would have chosen (and therefore the
# parent_path the slice-8 safety net stamps explicitly).
#
# Sources:
# - autogen.py (`generate_*` factories: lines 240, 309, 426, 486, 595,
#   939, 949)
# - pipeline.py `_inject_runtime_modules` (lines 5285+, 5299+) — runtime
#   library ModuleScripts + CinemachineRuntime LocalScript
# - pipeline.py `_subphase_inject_autogen_scripts` ClientBootstrap
#   (line 3098+) — LocalScript with no explicit parent_path
#
# Drift detection: today's behavior comes from the rbxlx_writer
# fallback table at `roblox/rbxlx_writer.py:1620-1632`. The lift +
# safety-net path must produce the same routing AND stamp the field
# explicitly. ANY drift here is a design-doc acceptance-gate failure.
# ---------------------------------------------------------------------------

GOLDEN_PARENT_PATHS: dict[str, tuple[str, str]] = {
    # name → (script_type, expected_parent_path)
    # Autogen Scripts — no explicit parent_path → SSS via writer default
    "GameServerManager":         ("Script",      "ServerScriptService"),
    "CollisionGroupSetup":       ("Script",      "ServerScriptService"),
    "CollisionFidelityRecook":   ("Script",      "ServerScriptService"),
    # Autogen LocalScripts — no explicit parent_path → SPS via writer default
    "CameraRigFollower":         ("LocalScript", "StarterPlayer.StarterPlayerScripts"),
    "ClientBootstrap":           ("LocalScript", "StarterPlayer.StarterPlayerScripts"),
    # Runtime library ModuleScripts — no explicit parent_path → RS via writer default
    "NavAgent":                  ("ModuleScript", "ReplicatedStorage"),
    "EventSystem":               ("ModuleScript", "ReplicatedStorage"),
    "CharacterBridge":           ("ModuleScript", "ReplicatedStorage"),
    "ObjectPool":                ("ModuleScript", "ReplicatedStorage"),
    "SubEmitterRuntime":         ("ModuleScript", "ReplicatedStorage"),
    # CinemachineRuntime LocalScript — no explicit parent_path → SPS
    "CinemachineRuntime":        ("LocalScript", "StarterPlayer.StarterPlayerScripts"),
    # Scene-runtime host runtime module — stamped explicitly in
    # _subphase_inject_scene_runtime (pipeline.py:5587-5591). Round 2
    # caught this gap: the R1 synthetic-stub fixture omitted it
    # entirely because the test didn't run real injection.
    "SceneRuntime":              ("ModuleScript", "ReplicatedStorage"),
    # Scene-runtime entrypoints — generators set parent_path EXPLICITLY
    # in autogen.py:599/943/953. The safety net must NOT touch these.
    "SceneRuntimePlan":          ("ModuleScript", "ReplicatedStorage"),
    "SceneRuntimeClient":        ("LocalScript", "StarterPlayer.StarterPlayerScripts"),
    "SceneRuntimeServer":        ("Script",      "ServerScriptService"),
}


def _make_pipeline(tmp_path: Path) -> Pipeline:
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    return Pipeline(str(unity_project), str(output))


def _engineer_pre_injection_state(pipeline: Pipeline) -> None:
    """Construct a ``RbxPlace`` and ``ctx.scene_runtime`` state that trips
    every detector in the three ``_subphase_inject_*`` paths.

    This is the round 2 hardening (Option a): instead of pre-stamping
    synthetic stubs, build state that makes the REAL injection
    subphases produce the autogen scripts we want to classify-check.
    A new autogen factory added to one of these subphases will appear
    in ``state.rbx_place.scripts`` after injection and be caught by
    the drift check below (whereas the R1 synthetic-stub test would
    silently miss it).
    """
    place = RbxPlace()

    # ---- Workspace parts engineered to trip every attribute detector ----
    # _subphase_inject_autogen_scripts:
    #   UnityLayer → CollisionGroupSetup
    #   _MainCameraRig → CameraRigFollower
    # _inject_runtime_modules:
    #   _HasNavMeshAgent → NavAgent
    #   _HasCharacterController → CharacterBridge
    #   CinemachineVCam → CinemachineRuntime
    #   particle_emitters with _HasSubEmitters → SubEmitterRuntime
    detector_part = RbxPart(
        name="DetectorPart",
        attributes={
            "UnityLayer": "Default",
            "_MainCameraRig": True,
            "_HasNavMeshAgent": True,
            "_HasCharacterController": True,
            "CinemachineVCam": True,
        },
        particle_emitters=[
            RbxParticleEmitter(attributes={"_HasSubEmitters": True}),
        ],
    )

    # _scene_needs_collision_recook walks parts looking for a MeshPart with
    # collision_fidelity != 0/None AND mesh_id set. Add a MeshPart sibling
    # so CollisionFidelityRecook fires.
    mesh_part = RbxPart(
        name="MeshDetectorPart",
        class_name="MeshPart",
        mesh_id="rbxassetid://0",
        collision_fidelity=1,  # Hull
    )

    place.workspace_parts = [detector_part, mesh_part]

    # ---- ScreenGui → has_ui → EventSystem ----
    place.screen_guis = [RbxScreenGui(name="HUD")]

    # ---- User scripts that trigger ClientBootstrap + ObjectPool ----
    # ClientBootstrap requires at least one ModuleScript with a side-
    # effect pattern (RenderStepped:Connect / Heartbeat:Connect / etc.).
    user_side_effect_module = RbxScript(
        name="UserSideEffectModule",
        source=(
            "local RunService = game:GetService('RunService')\n"
            "RunService.RenderStepped:Connect(function() end)\n"
            "return {}\n"
        ),
        script_type="ModuleScript",
    )
    # ObjectPool fires when ANY transpiled script source mentions ``pool``
    # AND one of the pool API verbs (GetNew / pool.Free / pool.Get).
    user_pool_script = RbxScript(
        name="UserPoolUser",
        source=(
            "local pool = require(script.Pool)\n"
            "local obj = pool:GetNew()\n"
        ),
        script_type="Script",
    )
    place.scripts = [user_side_effect_module, user_pool_script]

    pipeline.state.rbx_place = place

    # ---- Scene-runtime: generic mode with a runtime-bearing module ----
    # _subphase_inject_scene_runtime only fires when scene_runtime_mode
    # is "generic" AND the plan carries at least one runtime-bearing
    # module. Stage a minimal plan that satisfies both.
    pipeline.ctx.scene_runtime_mode = "generic"
    pipeline.ctx.scene_runtime = {
        "modules": {
            "fixture_stable_id_0": {
                "stem": "FixtureBehaviour",
                "runtime_bearing": True,
                "domain": "client",
            },
        },
    }


def _run_real_injection_subphases(pipeline: Pipeline) -> None:
    """Drive the three real ``_subphase_inject_*`` paths in the same
    order ``write_output`` does. This is the load-bearing hardening:
    new autogen factories wired into one of these subphases must
    appear in ``rbx_place.scripts`` after this call OR the drift
    check below catches the mismatch.
    """
    pipeline._subphase_inject_autogen_scripts()
    pipeline._inject_runtime_modules()
    pipeline._subphase_inject_scene_runtime()


class TestAutogenClassifyAcceptanceGate:
    """Slice 8 acceptance gate (design doc §Slice 8 — CRITICAL):
    ZERO ``parent_path`` drift on autogen / runtime-injection scripts
    after the lift. The Option (b) safety net stamps the
    rbxlx_writer-default container explicitly; this test pins what
    "default" means for every known autogen script.

    Round 2 hardening: exercises the real ``_subphase_inject_*`` paths
    so a new autogen factory drifting silently in or out of the
    injection set is caught — the R1 synthetic-stub test would have
    missed that drift mode.
    """

    def test_every_known_autogen_script_gets_golden_parent_path(
        self, tmp_path: Path,
    ) -> None:
        pipeline = _make_pipeline(tmp_path)
        _engineer_pre_injection_state(pipeline)

        # Drive the three real injection subphases — this is the
        # hardening: new autogen factories wired into one of these
        # paths land in the rbx_place.scripts list and are surfaced
        # to the drift check below. Pre-R2 the test stamped a
        # synthetic list and silently missed that drift mode.
        _run_real_injection_subphases(pipeline)

        # Mirror write_output's ordering: the safety-net pass runs
        # AFTER the three injection subphases.
        pipeline._classify_late_appended_scripts()

        assert pipeline.state.rbx_place is not None
        actual = {s.name: (s.script_type, s.parent_path)
                  for s in pipeline.state.rbx_place.scripts}

        drift: list[str] = []
        for name, (expected_type, expected_path) in (
            GOLDEN_PARENT_PATHS.items()
        ):
            if name not in actual:
                drift.append(
                    f"  {name}: NOT INJECTED by real "
                    f"_subphase_inject_* paths (a detector regressed, "
                    f"a factory drifted, or the test state needs "
                    f"updating for a new precondition)"
                )
                continue
            got_type, got_path = actual[name]
            if got_type != expected_type:
                drift.append(
                    f"  {name}: script_type drift "
                    f"(expected {expected_type!r}, got {got_type!r})"
                )
            if got_path != expected_path:
                drift.append(
                    f"  {name}: parent_path drift "
                    f"(expected {expected_path!r}, got {got_path!r})"
                )

        assert not drift, (
            "Slice 8 acceptance gate — parent_path drift on autogen "
            "/ runtime-injection scripts:\n" + "\n".join(drift)
        )

    def test_explicit_parent_paths_preserved_by_safety_net(
        self, tmp_path: Path,
    ) -> None:
        """The scene-runtime entrypoint factories set parent_path
        EXPLICITLY (autogen.py:599 / 943 / 953). The safety-net pass
        must NOT overwrite an explicitly-set parent_path — this is the
        "scripts with explicit parent_path pass through untouched"
        contract.

        Exercises the real ``_subphase_inject_scene_runtime`` path so a
        regression that drops the explicit ``parent_path`` from the
        generator (or the subphase) is caught at the boundary, not
        just at the generator unit-test level.
        """
        pipeline = _make_pipeline(tmp_path)
        _engineer_pre_injection_state(pipeline)

        # Drive ONLY the scene-runtime injection — other injections
        # are exercised in the test above; here we want a tight focus
        # on "the safety net does not touch explicit parent_paths".
        pipeline._subphase_inject_scene_runtime()

        assert pipeline.state.rbx_place is not None
        pre_paths: dict[str, str | None] = {
            s.name: s.parent_path
            for s in pipeline.state.rbx_place.scripts
            if s.name in ("SceneRuntimeClient", "SceneRuntimeServer",
                           "SceneRuntimePlan", "SceneRuntime")
        }
        # The generators stamp explicit paths; sanity-check we got them.
        assert pre_paths.get("SceneRuntimeClient") == (
            "StarterPlayer.StarterPlayerScripts"
        ), pre_paths
        assert pre_paths.get("SceneRuntimeServer") == "ServerScriptService", pre_paths
        assert pre_paths.get("SceneRuntimePlan") == "ReplicatedStorage", pre_paths

        pipeline._classify_late_appended_scripts()

        # Post-state must match pre-state — the safety net did not
        # touch already-explicit parent_paths.
        post_paths: dict[str, str | None] = {
            s.name: s.parent_path
            for s in pipeline.state.rbx_place.scripts
            if s.name in pre_paths
        }
        for name, pre in pre_paths.items():
            assert post_paths[name] == pre, (
                f"Safety net overwrote explicit parent_path on {name}: "
                f"{pre!r} → {post_paths[name]!r}"
            )
