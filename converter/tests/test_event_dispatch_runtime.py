"""Tests for the ``runtime/event_dispatch.luau`` ModuleScript and its
auto-injection by the pipeline when FPS scaffolding is opted in.

Pre-PR, ``connectClient(evt, handler)`` was inlined inside the
auto-generated HUDController LocalScript — every consumer that wanted
the BindableEvent vs RemoteEvent fork had to copy the body or accept
the duplication. PR #3 of the FPS extraction work moves the helper
into a shared runtime ModuleScript:

- ``runtime/event_dispatch.luau`` — emits ``EventDispatch.connectClient``.
- ``Pipeline._inject_runtime_modules`` adds the module to
  ``ReplicatedStorage`` whenever ``"fps" in self.scaffolding``, so
  the HUDController's ``require(...:WaitForChild("EventDispatch"))``
  resolves at runtime.
- ``generate_hud_client_script`` no longer inlines the helper; it
  ``require()``\\s the runtime module instead.

Tests pin: the runtime module's source carries the canonical helper,
the HUD controller script ``require``\\s instead of inlining, and the
pipeline auto-injects the module on opt-in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from converter.pipeline import Pipeline
from converter.scaffolding.fps import generate_hud_client_script
from core.roblox_types import RbxPlace, RbxScript


RUNTIME_DIR = Path(__file__).parent.parent / "runtime"


class TestEventDispatchModuleSource:
    """The on-disk runtime ModuleScript exists with the canonical
    ``EventDispatch.connectClient`` shape. Pinned so an accidental
    refactor that drops the function or renames it can't slip past."""

    def test_module_file_exists(self) -> None:
        path = RUNTIME_DIR / "event_dispatch.luau"
        assert path.exists(), (
            f"runtime/event_dispatch.luau missing — "
            "auto-injection will fail at WaitForChild"
        )

    def test_module_exposes_connectclient_via_table_export(self) -> None:
        path = RUNTIME_DIR / "event_dispatch.luau"
        source = path.read_text(encoding="utf-8")
        # Module table + named function + return.
        assert "local EventDispatch = {}" in source
        assert "function EventDispatch.connectClient" in source
        assert "return EventDispatch" in source.splitlines()[-3:][0:5] or (
            "return EventDispatch" in source
        )

    def test_connectclient_dispatches_on_instance_class(self) -> None:
        """The body must fork on ``BindableEvent`` vs ``RemoteEvent``.
        Hard-coding either one breaks the producer-side flexibility
        that motivated the helper in the first place."""
        source = (RUNTIME_DIR / "event_dispatch.luau").read_text(
            encoding="utf-8",
        )
        assert 'evt:IsA("BindableEvent")' in source
        assert "evt.Event:Connect(handler)" in source
        assert 'evt:IsA("RemoteEvent")' in source
        assert "evt.OnClientEvent:Connect(handler)" in source

    def test_connectclient_handles_nil_silently(self) -> None:
        """Callers pass ``ReplicatedStorage:WaitForChild(name, timeout)``
        which can return nil on timeout. The helper must no-op cleanly
        rather than erroring."""
        source = (RUNTIME_DIR / "event_dispatch.luau").read_text(
            encoding="utf-8",
        )
        # First non-comment line of the body should be a nil guard.
        assert "if not evt then" in source
        assert "return" in source  # bare return after the nil guard


class TestHudClientScriptRequiresEventDispatch:
    """The auto-generated HUDController LocalScript no longer inlines
    ``connectClient`` — it requires the runtime module instead."""

    def test_hud_script_requires_event_dispatch(self) -> None:
        script = generate_hud_client_script()
        assert 'require(ReplicatedStorage:WaitForChild("EventDispatch"))' in (
            script.source
        )
        # The local-rebind is what call sites use throughout the body.
        assert "local connectClient = EventDispatch.connectClient" in (
            script.source
        )

    def test_hud_script_no_longer_inlines_helper(self) -> None:
        """The historic inline definition must be gone — otherwise
        the require would shadow it ambiguously and bloat the file
        without purpose."""
        script = generate_hud_client_script()
        assert "local function connectClient(evt, handler)" not in (
            script.source
        )

    def test_hud_script_call_sites_still_use_helper(self) -> None:
        """Body still calls ``connectClient(...)`` — the rebind from
        the require must reach every existing dispatch site."""
        script = generate_hud_client_script()
        assert script.source.count("connectClient(") >= 3, (
            "fewer than 3 connectClient call sites — the body lost "
            "its HealthUpdate / AmmoUpdate / ItemUpdate dispatches"
        )


class TestPipelineInjectsEventDispatchOnOptIn:
    """Pipeline auto-injects ``EventDispatch`` ModuleScript when
    ``"fps" in self.scaffolding`` so the HUDController's require
    resolves at runtime."""

    @staticmethod
    def _make_pipeline_with_fps_opt_in(
        tmp_path: Path,
        *,
        scaffolding: list[str] | None = None,
    ) -> Pipeline:
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()
        pl = Pipeline(
            unity_project_path=project,
            output_dir=out,
            scaffolding=scaffolding,
        )
        pl.state.rbx_place = RbxPlace(
            scripts=[],
            workspace_parts=[],
            screen_guis=[],
        )
        return pl

    def test_event_dispatch_injected_on_fps_opt_in(
        self, tmp_path: Path,
    ) -> None:
        pl = self._make_pipeline_with_fps_opt_in(
            tmp_path, scaffolding=["fps"],
        )
        pl._inject_runtime_modules()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "EventDispatch" in names, (
            "EventDispatch ModuleScript must be auto-injected when "
            "--scaffolding=fps is set; otherwise the auto-generated "
            "HUDController crashes on require"
        )

    def test_event_dispatch_not_injected_without_fps_opt_in(
        self, tmp_path: Path,
    ) -> None:
        """No opt-in → no auto-injection. The module is only useful
        for the HUDController's require, so emitting it on every
        conversion would just bloat non-FPS places."""
        pl = self._make_pipeline_with_fps_opt_in(tmp_path, scaffolding=None)
        pl._inject_runtime_modules()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "EventDispatch" not in names

    def test_event_dispatch_injected_as_modulescript(
        self, tmp_path: Path,
    ) -> None:
        """Must be a ModuleScript so ``require()`` works. A LocalScript
        or Script wouldn't be require-able from the HUDController."""
        pl = self._make_pipeline_with_fps_opt_in(
            tmp_path, scaffolding=["fps"],
        )
        pl._inject_runtime_modules()
        ed = next(
            s for s in pl.state.rbx_place.scripts if s.name == "EventDispatch"
        )
        assert ed.script_type == "ModuleScript"

    def test_event_dispatch_idempotent_on_repeat_inject(
        self, tmp_path: Path,
    ) -> None:
        """Calling ``_inject_runtime_modules`` twice must not append
        a duplicate EventDispatch — the existing-script guard must
        match by name (matching the convention for other runtime
        modules in the same loop)."""
        pl = self._make_pipeline_with_fps_opt_in(
            tmp_path, scaffolding=["fps"],
        )
        pl._inject_runtime_modules()
        pl._inject_runtime_modules()
        ed_count = sum(
            1 for s in pl.state.rbx_place.scripts if s.name == "EventDispatch"
        )
        assert ed_count == 1


class TestRehydrateSkipsAutogenScripts:
    """Codex finding [P2] (PR #70 round 1): the preserve-scripts
    rehydrate path was loading every ``.luau`` under ``scripts/`` —
    including converter-emitted files from the prior run. That
    silently carried forward last run's FPS scaffolding even when
    the new run opts out via no ``--scaffolding=fps``, and tripped
    ``detect_fps_game`` on the rehydrated GameServerManager so the
    rbxlx writer locked the camera to first person on non-FPS
    rebuilds.

    Rehydrate now skips files whose head comment matches a known
    auto-gen marker. The corresponding inject subphases re-emit
    each script cleanly when their gate (scaffolding flag, scene
    predicate) opts in for the new run.
    """

    @staticmethod
    def _make_pipeline(tmp_path: Path) -> Pipeline:
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()
        pl = Pipeline(unity_project_path=project, output_dir=out)
        pl.state.rbx_place = RbxPlace(
            scripts=[],
            workspace_parts=[],
            screen_guis=[],
        )
        return pl

    @pytest.mark.parametrize(
        "filename,marker",
        [
            ("AutoFpsHudController.luau", "-- HUD Controller (auto-generated)\n"),
            ("FPSController.luau", "-- FPS Client Controller (auto-generated)\n"),
            ("GameServerManager.luau", "-- Game Server Manager (auto-generated by Unity converter)\n"),
            ("CollisionFidelityRecook.luau", "-- CollisionFidelityRecook (auto-generated)\n"),
            ("CollisionGroupSetup.luau", "-- CollisionGroup Setup (auto-generated from Unity layers)\n"),
            ("EventDispatch.luau", "-- EventDispatch: cross-class connect helper for client event listeners.\n"),
            ("ClientBootstrap.luau", "-- Auto-generated bootstrap: require modules with side-effects\n"),
            ("Anim_Door_door_open.luau", "-- Auto-generated animation script\n"),
        ],
    )
    def test_rehydrate_skips_known_autogen_files(
        self,
        tmp_path: Path,
        filename: str,
        marker: str,
    ) -> None:
        pl = self._make_pipeline(tmp_path)
        scripts_dir = pl.output_dir / "scripts"
        scripts_dir.mkdir()
        # Auto-gen file from a prior run, with the canonical marker
        # at the top of the source.
        (scripts_dir / filename).write_text(
            marker + "local _ = 1  -- body content\n"
        )
        pl._rehydrate_scripts_from_disk(scripts_dir)
        # Rehydrate must NOT have pulled the auto-gen file forward —
        # the inject subphases for THIS run will re-emit if their
        # gates fire.
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert Path(filename).stem not in names, (
            f"auto-gen {filename!r} was rehydrated despite carrying "
            "the canonical marker — opt-out reruns will silently "
            "preserve last run's scaffolding"
        )

    def test_rehydrate_keeps_user_authored_scripts(
        self, tmp_path: Path,
    ) -> None:
        """The skip is content-based, not name-based: a user script
        named ``HUDController.luau`` (no marker) survives rehydrate
        even though the converter's auto-gen used to share the name.
        """
        pl = self._make_pipeline(tmp_path)
        scripts_dir = pl.output_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "HUDController.luau").write_text(
            "-- User-authored HUDController (transpiled from .cs)\n"
            "local x = 1\n"
        )
        (scripts_dir / "Player.luau").write_text(
            "-- User Player script\n"
            "local UserInputService = game:GetService('UserInputService')\n"
        )
        pl._rehydrate_scripts_from_disk(scripts_dir)
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "HUDController" in names
        assert "Player" in names

    def test_rehydrate_classifies_user_hudcontroller_correctly(
        self, tmp_path: Path,
    ) -> None:
        """Stronger pin on the user-authored case: the rehydrate
        path keeps user HUDController.luau AND classifies it via
        the heuristic (LocalScript when the body uses LocalPlayer/
        UserInputService)."""
        pl = self._make_pipeline(tmp_path)
        scripts_dir = pl.output_dir / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "HUDController.luau").write_text(
            "-- User HUDController\n"
            "local UserInputService = game:GetService('UserInputService')\n"
            "UserInputService.InputBegan:Connect(function() end)\n"
        )
        pl._rehydrate_scripts_from_disk(scripts_dir)
        user_hud = next(
            (s for s in pl.state.rbx_place.scripts if s.name == "HUDController"),
            None,
        )
        assert user_hud is not None
        assert user_hud.script_type == "LocalScript"
