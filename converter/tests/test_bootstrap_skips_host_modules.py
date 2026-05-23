"""Bootstrap emitter must skip host-owned modules under generic mode.

Under ``--scene-runtime=generic`` PR4's host runtime (``scene_runtime
.luau``) owns the lifecycle of every runtime-bearing MonoBehaviour: it
requires them, instantiates the returned class table, and drives their
``Awake`` / ``OnEnable`` / ``Update`` calls. If the legacy
``ClientBootstrap`` ALSO requires them first, two things break:

  1. The module's top-level ``return ClassName`` exits cleanly under
     the contract (no side effects), so the bootstrap require silently
     no-ops. Harmless on its own.
  2. But the legacy require also caches the module in
     ``ReplicatedStorage._loadedModules`` (Luau's per-require memoization),
     and any conversion that DID accidentally retain top-level
     side-effect code (e.g. a verifier escape) fires those effects
     under the bootstrap rather than under the host -- double-loading
     the module and reordering effects vs the contract's lifecycle
     order.

The host runtime is the only legitimate ``require`` site for a
runtime-bearing module. The bootstrap emitter must exclude them.

Legacy mode preserves the full bootstrap list -- the filter is gated
on ``ctx.scene_runtime_mode == 'generic'`` so legacy emit stays
byte-identical to pre-PR3a behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.pipeline import Pipeline
from core.roblox_types import RbxPlace, RbxScript


def _make_pipeline(tmp_path: Path) -> Pipeline:
    project = tmp_path / "fakeproject"
    (project / "Assets").mkdir(parents=True)
    out = tmp_path / "output"
    out.mkdir()
    return Pipeline(
        unity_project_path=project,
        output_dir=out,
    )


def _side_effect_module(name: str, *, source: str | None = None) -> RbxScript:
    """A ModuleScript that matches one of the side-effect patterns
    (``RenderStepped:Connect``). Without a match the bootstrap doesn't
    list it regardless of any filter."""
    return RbxScript(
        name=name,
        source=source or (
            "local m = {}\n"
            "game:GetService('RunService').RenderStepped:Connect(function() end)\n"
            "return m\n"
        ),
        script_type="ModuleScript",
    )


def _bootstrap_module_names(pl: Pipeline) -> list[str]:
    """Extract the side-effect module names from the emitted
    ClientBootstrap LocalScript. Each module is required via
    ``RS:WaitForChild("<name>", 10)``; pull those names out."""
    bootstrap = next(
        (s for s in pl.state.rbx_place.scripts if s.name == "ClientBootstrap"),
        None,
    )
    if bootstrap is None:
        return []
    names = []
    import re
    for m in re.finditer(r'RS:WaitForChild\("([^"]+)"', bootstrap.source):
        names.append(m.group(1))
    return names


class TestGenericModeSkipsRuntimeBearingModules:

    def test_runtime_bearing_module_excluded_from_bootstrap(
        self, tmp_path: Path,
    ) -> None:
        pl = _make_pipeline(tmp_path)
        pl.ctx.scene_runtime_mode = "generic"
        pl.ctx.scene_runtime = {
            "modules": {
                "guid-a": {
                    "stem": "FireLight",
                    "class_name": "FireLight",
                    "runtime_bearing": True,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        pl.state.rbx_place = RbxPlace(
            scripts=[_side_effect_module("FireLight")],
        )

        pl._subphase_inject_autogen_scripts()

        names = _bootstrap_module_names(pl)
        assert "FireLight" not in names, (
            "Runtime-bearing module FireLight must NOT be required by "
            "ClientBootstrap -- the host runtime owns its lifecycle. "
            "Double-loading caused the SimpleFPS line-14 crash."
        )

    def test_non_runtime_bearing_module_still_required(
        self, tmp_path: Path,
    ) -> None:
        pl = _make_pipeline(tmp_path)
        pl.ctx.scene_runtime_mode = "generic"
        pl.ctx.scene_runtime = {
            "modules": {
                "guid-a": {
                    "stem": "FireLight",
                    "class_name": "FireLight",
                    "runtime_bearing": True,
                },
                "guid-b": {
                    "stem": "FrameTimer",
                    "class_name": "FrameTimer",
                    "runtime_bearing": False,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        pl.state.rbx_place = RbxPlace(
            scripts=[
                _side_effect_module("FireLight"),
                _side_effect_module("FrameTimer"),
            ],
        )

        pl._subphase_inject_autogen_scripts()

        names = _bootstrap_module_names(pl)
        assert "FireLight" not in names
        assert "FrameTimer" in names, (
            "Non-runtime-bearing side-effect modules must STILL be "
            "required by the bootstrap -- the host runtime doesn't "
            "manage them, so the legacy require path is correct."
        )

    def test_module_with_no_scene_runtime_entry_still_required(
        self, tmp_path: Path,
    ) -> None:
        """A side-effect module that exists in ``rbx_place.scripts`` but
        has no row in ``scene_runtime.modules`` (e.g. autogen
        bootstrap scripts) must NOT be skipped. The filter only fires
        on stems the planner explicitly marked runtime-bearing."""
        pl = _make_pipeline(tmp_path)
        pl.ctx.scene_runtime_mode = "generic"
        pl.ctx.scene_runtime = {
            "modules": {},
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        pl.state.rbx_place = RbxPlace(
            scripts=[_side_effect_module("InputManager")],
        )

        pl._subphase_inject_autogen_scripts()

        names = _bootstrap_module_names(pl)
        assert "InputManager" in names


class TestLegacyModeBootstrapUnchanged:
    """Byte-equivalence guard: under legacy mode the bootstrap filter
    must be a no-op. The full pre-PR3a require list is preserved even
    when ``scene_runtime.modules`` happens to be populated (e.g. by a
    PR1-only plan_scene_runtime that ran without --scene-runtime=generic).
    """

    def test_legacy_mode_includes_modules_planner_marked_runtime_bearing(
        self, tmp_path: Path,
    ) -> None:
        pl = _make_pipeline(tmp_path)
        # ctx.scene_runtime_mode defaults to "legacy".
        assert pl.ctx.scene_runtime_mode == "legacy"
        # Even with a runtime-bearing planner row, legacy mode must
        # still require the module. The host runtime isn't active under
        # legacy.
        pl.ctx.scene_runtime = {
            "modules": {
                "guid-a": {
                    "stem": "FireLight",
                    "class_name": "FireLight",
                    "runtime_bearing": True,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        pl.state.rbx_place = RbxPlace(
            scripts=[_side_effect_module("FireLight")],
        )

        pl._subphase_inject_autogen_scripts()

        names = _bootstrap_module_names(pl)
        assert "FireLight" in names, (
            "Legacy mode must NOT filter runtime-bearing modules from "
            "the bootstrap. The filter is generic-mode-only; legacy "
            "emit is byte-identical to pre-PR3a behaviour."
        )

    def test_legacy_mode_with_empty_scene_runtime_unchanged(
        self, tmp_path: Path,
    ) -> None:
        """The most common case (legacy run with no plan_scene_runtime
        artifact): the bootstrap emitter never inspects
        ``scene_runtime`` -- the gate on ``scene_runtime_mode ==
        'generic'`` short-circuits before any dict lookups."""
        pl = _make_pipeline(tmp_path)
        # Empty ctx (matches the legacy default).
        pl.ctx.scene_runtime = {}
        pl.state.rbx_place = RbxPlace(
            scripts=[_side_effect_module("FireLight")],
        )

        pl._subphase_inject_autogen_scripts()

        names = _bootstrap_module_names(pl)
        assert "FireLight" in names


class TestFilterCoexistsWithFpsAntiPattern:
    """The new runtime-bearing skip runs BEFORE the existing
    anti-FPS skip. Both filters are independent: a module can be
    excluded by either (or both), and the more-specific log line wins.
    """

    def test_runtime_bearing_skipped_before_anti_fps_check(
        self, tmp_path: Path,
    ) -> None:
        pl = _make_pipeline(tmp_path)
        pl.ctx.scene_runtime_mode = "generic"
        pl.ctx.scene_runtime = {
            "modules": {
                "guid-a": {
                    "stem": "MenuMouseUnlock",
                    "class_name": "MenuMouseUnlock",
                    "runtime_bearing": True,
                },
            },
            "scenes": {}, "prefabs": {}, "domain_overrides": {},
        }
        # Module hits both the side-effect pattern AND the anti-FPS
        # pattern. It must be skipped regardless of which filter the
        # implementation evaluates first.
        anti_fps_module = RbxScript(
            name="MenuMouseUnlock",
            source=(
                "local UIS = game:GetService('UserInputService')\n"
                "UIS.InputBegan:Connect(function() end)\n"
                "UIS.MouseBehavior = Enum.MouseBehavior.Default\n"
                "return {}\n"
            ),
            script_type="ModuleScript",
        )
        # Trigger has_fps_controller via a separate LocalScript that
        # locks the mouse.
        fps_controller = RbxScript(
            name="FpsCtl",
            source=(
                "local UIS = game:GetService('UserInputService')\n"
                "UIS.MouseBehavior = Enum.MouseBehavior.LockCenter\n"
            ),
            script_type="LocalScript",
        )
        pl.state.rbx_place = RbxPlace(scripts=[anti_fps_module, fps_controller])

        pl._subphase_inject_autogen_scripts()

        names = _bootstrap_module_names(pl)
        assert "MenuMouseUnlock" not in names
