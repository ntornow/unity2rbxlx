"""Tests for the opt-in FPS scaffolding flag.

Pre-refactor, ``Pipeline._subphase_inject_autogen_scripts`` auto-detected
FPS-style scripts via ``detect_fps_game`` and injected an FPS client
controller, HUD ScreenGui, and HUDController LocalScript on every
matching project. Any non-FPS project whose scripts happened to mention
``Raycast`` and ``ammo`` got those scripts dropped on top of its scene
without consent.

The new contract: scaffolding is opt-in. ``--scaffolding=fps`` (or the
equivalent ``Pipeline(scaffolding=frozenset({"fps"}))`` constructor
argument) requests the FPS scripts; with no flag the converter makes no
game-genre assumptions and emits nothing genre-specific.

These tests pin the new contract by exercising the inject-autogen
subphase directly with a synthetic ``RbxPlace`` whose scripts match the
FPS heuristic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from converter.pipeline import Pipeline
from core.roblox_types import RbxPlace, RbxScript


def _fps_shaped_place() -> RbxPlace:
    """Build a minimal RbxPlace whose scripts match every branch of
    ``detect_fps_game`` (PlayerShoot RemoteEvent, Raycast+ammo+shoot,
    curHealth+gotWeapon+Raycast). Pre-refactor, this place would have
    triggered every FPS auto-inject branch."""
    return RbxPlace(
        scripts=[
            RbxScript(
                name="Player",
                source=(
                    'local PlayerShoot = ReplicatedStorage:WaitForChild("PlayerShoot")\n'
                    'PlayerShoot.OnServerEvent:Connect(function() end)\n'
                    'local function shoot() workspace:Raycast(...) end\n'
                    'local curAmmo = 30\n'
                    'local curHealth = 100\n'
                    'local gotWeapon = true\n'
                ),
                script_type="Script",
            ),
        ],
        workspace_parts=[],
        screen_guis=[],
    )


@pytest.fixture
def fps_pipeline_factory(tmp_path: Path):
    """Build a Pipeline scoped to a tmp output dir with the synthetic
    FPS-shaped place pre-populated. Returns a factory because each test
    constructs the pipeline with different ``scaffolding`` arguments.
    """

    def _make(scaffolding: frozenset[str] | None) -> Pipeline:
        # Pipeline expects a real Unity project root for ``_find_unity_root``.
        # A fake root with an empty Assets/ subdir satisfies that without
        # requiring any actual Unity content.
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        pl = Pipeline(
            unity_project_path=project,
            output_dir=out,
            scaffolding=scaffolding,
        )
        pl.state.rbx_place = _fps_shaped_place()
        return pl

    return _make


class TestFpsScaffoldingDefaultOff:
    """Default behaviour: no scaffolding flag → no FPS injection, even
    when the heuristic would otherwise match."""

    def test_default_pipeline_does_not_inject_fps_scripts(
        self, fps_pipeline_factory,
    ) -> None:
        pl = fps_pipeline_factory(None)
        assert pl.scaffolding == frozenset()
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "FpsClient" not in names
        assert "HUDController" not in names

    def test_empty_scaffolding_set_does_not_inject(
        self, fps_pipeline_factory,
    ) -> None:
        """Passing ``scaffolding=frozenset()`` explicitly should match
        the default — no auto-inject."""
        pl = fps_pipeline_factory(frozenset())
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "FpsClient" not in names
        assert "HUDController" not in names
        assert all(sg.name != "HUD" for sg in pl.state.rbx_place.screen_guis)

    def test_unrelated_scaffolding_does_not_inject_fps(
        self, fps_pipeline_factory,
    ) -> None:
        """A future scaffolding name (e.g. ``"platformer"``) shouldn't
        accidentally enable FPS scripts."""
        pl = fps_pipeline_factory(frozenset({"platformer"}))
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "FpsClient" not in names
        assert "HUDController" not in names


class TestFpsScaffoldingOptIn:
    """Explicit ``scaffolding={"fps"}`` → inject the FPS controller,
    HUD ScreenGui, and HUDController as before the refactor."""

    def test_fps_scaffolding_injects_fps_scripts(
        self, fps_pipeline_factory,
    ) -> None:
        pl = fps_pipeline_factory(frozenset({"fps"}))
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        # Either the FpsClient LocalScript or some equivalent FPS
        # controller marker should be present after opt-in.
        assert "AutoFpsHudController" in names, (
            "AutoFpsHudController must be injected when --scaffolding=fps"
        )

    def test_fps_scaffolding_injects_hud_screengui(
        self, fps_pipeline_factory,
    ) -> None:
        pl = fps_pipeline_factory(frozenset({"fps"}))
        pl._subphase_inject_autogen_scripts()
        gui_names = {sg.name for sg in pl.state.rbx_place.screen_guis}
        assert "HUD" in gui_names

    def test_fps_scaffolding_alongside_other_scaffolding(
        self, fps_pipeline_factory,
    ) -> None:
        """Multi-value scaffolding should still pick up ``fps``."""
        pl = fps_pipeline_factory(frozenset({"fps", "future_genre"}))
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "AutoFpsHudController" in names


class TestPipelineScaffoldingPlumbing:
    """The ``scaffolding`` keyword arg flows from Pipeline.__init__ to
    the inject-autogen subphase. Pin the surface so a future refactor
    that drops the keyword fails loudly."""

    def test_scaffolding_default_is_empty_frozenset(
        self, tmp_path: Path,
    ) -> None:
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        pl = Pipeline(unity_project_path=project, output_dir=tmp_path / "out")
        assert pl.scaffolding == frozenset()
        assert isinstance(pl.scaffolding, frozenset)

    def test_scaffolding_accepts_iterable(self, tmp_path: Path) -> None:
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        pl = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path / "out",
            scaffolding=frozenset({"fps"}),
        )
        assert "fps" in pl.scaffolding


class TestScaffoldingPersistence:
    """``scaffolding`` is stored on ``ConversionContext`` so it survives
    a JSON round-trip. Resumed builds (``u2r.py publish`` rebuild path,
    ``convert_interactive upload`` against an existing assemble, or a
    second ``assemble`` call without re-passing ``--scaffolding``) must
    reproduce the same place contents — they look up scaffolding on
    the rehydrated ctx, not from a fresh empty Pipeline default.
    """

    def test_scaffolding_round_trips_through_json(
        self, tmp_path: Path,
    ) -> None:
        from core.conversion_context import ConversionContext

        ctx = ConversionContext(unity_project_path="/x")
        ctx.scaffolding = ["fps", "puzzle"]

        path = tmp_path / "ctx.json"
        ctx.save(path)
        loaded = ConversionContext.load(path)
        assert loaded.scaffolding == ["fps", "puzzle"]

    def test_pipeline_reads_scaffolding_from_rehydrated_ctx(
        self, tmp_path: Path,
    ) -> None:
        """Simulates the ``u2r.py publish`` rebuild path: a fresh
        Pipeline gets ``ctx`` swapped in from disk. The new ctx
        carries scaffolding; the property must reflect it without any
        explicit re-init."""
        from core.conversion_context import ConversionContext

        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        pl = Pipeline(unity_project_path=project, output_dir=tmp_path / "out")
        assert pl.scaffolding == frozenset()  # default

        prior = ConversionContext(
            unity_project_path=str(project),
            scaffolding=["fps"],
        )
        pl.ctx = prior  # mirrors u2r.py publish rebuild path
        assert pl.scaffolding == frozenset({"fps"})

    def test_apply_scaffolding_is_additive(self, tmp_path: Path) -> None:
        """A follow-up ``assemble`` call passing
        ``--scaffolding=puzzle`` must NOT drop the ``fps`` entry that
        was persisted from a prior run; the merge is additive."""
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        pl = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path / "out",
            scaffolding=["fps"],
        )
        pl.apply_scaffolding(["puzzle"])
        assert pl.scaffolding == frozenset({"fps", "puzzle"})

    def test_apply_scaffolding_none_is_noop(self, tmp_path: Path) -> None:
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        pl = Pipeline(
            unity_project_path=project,
            output_dir=tmp_path / "out",
            scaffolding=["fps"],
        )
        pl.apply_scaffolding(None)
        assert pl.scaffolding == frozenset({"fps"})
        pl.apply_scaffolding([])
        assert pl.scaffolding == frozenset({"fps"})

    def test_unknown_scaffolding_name_warns_but_persists(
        self, tmp_path: Path, caplog,
    ) -> None:
        """Codex finding [P3] (PR #69 round 4): a typo like
        ``--scaffolding=fsps`` was previously silent — the value
        landed in ``conversion_context.json`` and the conversion
        ran with no FPS scaffolding (the typo isn't ``"fps"``).
        Validating against the known set logs a warning so the
        typo surfaces in conversion logs.

        The value is still persisted (forward-compat for genres
        added in future) — only the warn-on-unknown gate matters.
        """
        import logging

        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        with caplog.at_level(logging.WARNING):
            pl = Pipeline(
                unity_project_path=project,
                output_dir=tmp_path / "out",
                scaffolding=["fsps"],
            )
        assert "fsps" in pl.scaffolding  # persisted forward-compat
        assert "Unknown scaffolding name" in caplog.text
        assert "fsps" in caplog.text

    def test_known_scaffolding_name_does_not_warn(
        self, tmp_path: Path, caplog,
    ) -> None:
        """``--scaffolding=fps`` is the canonical opt-in — must not
        trigger the unknown-name warning."""
        import logging

        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        with caplog.at_level(logging.WARNING):
            Pipeline(
                unity_project_path=project,
                output_dir=tmp_path / "out",
                scaffolding=["fps"],
            )
        assert "Unknown scaffolding name" not in caplog.text

    def test_apply_scaffolding_normalises_input(self, tmp_path: Path) -> None:
        """Strip + lowercase, just like the CLI parser. Avoids
        case-sensitivity surprises across CLI vs interactive paths."""
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        pl = Pipeline(unity_project_path=project, output_dir=tmp_path / "out")
        pl.apply_scaffolding([" FPS ", "  ", "Puzzle"])
        assert pl.scaffolding == frozenset({"fps", "puzzle"})

    def test_resume_reapplies_constructor_scaffolding_after_ctx_swap(
        self, tmp_path: Path,
    ) -> None:
        """``u2r.py convert --phase write_output --scaffolding=fps``
        constructs a Pipeline with the new flag, then calls
        ``resume()`` which loads ``conversion_context.json`` from disk
        and replaces ``self.ctx``. The persisted ctx may not carry
        ``fps``; the constructor's request must still survive the
        swap so the resume actually injects the FPS scaffolding.
        """
        from core.conversion_context import ConversionContext

        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()

        # Persist a prior ctx with NO scaffolding entry — mimics a
        # conversion that ran before --scaffolding existed.
        prior = ConversionContext(unity_project_path=str(project))
        prior.save(out / "conversion_context.json")

        # Construct Pipeline with the new flag, then mimic the ctx
        # swap that ``resume()`` performs.
        pl = Pipeline(
            unity_project_path=project,
            output_dir=out,
            scaffolding=["fps"],
        )
        # Sanity: the constructor's request is on ctx pre-swap.
        assert pl.scaffolding == frozenset({"fps"})

        # Simulate resume()'s ctx reload + re-apply.
        pl.ctx = ConversionContext.load(out / "conversion_context.json")
        # Without re-application, the swap would clobber the request.
        assert pl.scaffolding == frozenset()
        # The Pipeline keeps the constructor's snapshot for re-application.
        pl.apply_scaffolding(pl._init_scaffolding)
        assert pl.scaffolding == frozenset({"fps"})


class TestFpsHeuristicNoFalsePositiveOnAutogen:
    """The FPS heuristic must check USER scripts only — not the
    converter's own auto-generated GameServerManager (which contains
    both ``PlayerShoot`` and ``RemoteEvent`` to wire up its generic
    spawn flow). Otherwise the soft hint fires on every conversion,
    not just genuine FPS-shaped projects.
    """

    def test_detector_does_not_fire_on_autogen_only_place(
        self, tmp_path: Path,
    ) -> None:
        """A place containing ONLY the converter's autogen scripts
        (GameServerManager, CollisionFidelityRecook, etc.) must not
        trigger ``detect_fps_game``. The detector skips files
        carrying any of ``_AUTOGEN_MARKERS``, so user-authored
        content is the only thing it considers — the converter's
        generic spawn-flow code (``PlayerShoot`` + ``RemoteEvent``)
        no longer false-positives.
        """
        from converter.autogen import (
            generate_collision_fidelity_recook_script,
            generate_game_server_script,
        )
        from converter.scaffolding.fps import detect_fps_game

        place = RbxPlace(
            scripts=[
                generate_game_server_script(),
                generate_collision_fidelity_recook_script(),
            ],
            workspace_parts=[],
            screen_guis=[],
        )
        looks_fps = detect_fps_game(place)
        assert looks_fps is False, (
            "detector should skip auto-gen scripts — only user "
            "content drives the heuristic"
        )

    def test_is_fps_game_set_on_heuristic_match_without_opt_in(
        self, fps_pipeline_factory,
    ) -> None:
        """Pre-refactor regression check: a project whose user scripts
        already trip ``detect_fps_game`` (e.g. ships its own controller
        and HUD) but DOES NOT pass ``--scaffolding=fps`` must still get
        ``is_fps_game = True``. That flag drives downstream scene
        settings — ``StarterPlayer.CameraMode = LockFirstPerson`` in
        the rbxlx writer — independently of HUD/controller injection.
        """
        pl = fps_pipeline_factory(None)  # no opt-in
        # Default fixture place trips detect_fps_game (PlayerShoot
        # RemoteEvent + Raycast + ammo + curHealth + gotWeapon).
        pl._subphase_inject_autogen_scripts()
        assert getattr(pl.state.rbx_place, "is_fps_game", False) is True

    def test_is_fps_game_set_on_explicit_opt_in_without_heuristic_match(
        self, tmp_path: Path,
    ) -> None:
        """Pre-refactor regression check: a project that passes
        ``--scaffolding=fps`` but whose user scripts don't trip the
        FPS heuristic must still get ``is_fps_game = True``. The
        explicit caller opt-in is authoritative; the heuristic was
        always best-effort."""
        from core.roblox_types import RbxPlace as _Place

        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()

        pl = Pipeline(
            unity_project_path=project,
            output_dir=out,
            scaffolding=["fps"],
        )
        # Place with NO user FPS markers — heuristic returns False.
        pl.state.rbx_place = _Place(
            scripts=[
                RbxScript(
                    name="Hello",
                    source='print("hello world")',
                    script_type="LocalScript",
                ),
            ],
            workspace_parts=[],
            screen_guis=[],
        )
        pl._subphase_inject_autogen_scripts()
        assert getattr(pl.state.rbx_place, "is_fps_game", False) is True

    def test_is_fps_game_stays_false_for_non_fps_default(
        self, tmp_path: Path,
    ) -> None:
        """Non-FPS project, no opt-in, no FPS-shaped scripts: the flag
        stays unset so the rbxlx writer doesn't apply FPS-only scene
        settings to a generic project."""
        from core.roblox_types import RbxPlace as _Place

        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "out"
        out.mkdir()

        pl = Pipeline(unity_project_path=project, output_dir=out)
        pl.state.rbx_place = _Place(
            scripts=[],
            workspace_parts=[],
            screen_guis=[],
        )
        pl._subphase_inject_autogen_scripts()
        assert getattr(pl.state.rbx_place, "is_fps_game", False) is False

    def test_subphase_runs_detector_before_autogen_inject(
        self, fps_pipeline_factory,
    ) -> None:
        """The subphase computes ``looks_fps`` BEFORE appending any
        autogen scripts so the heuristic only sees user content. A
        place with NO user FPS markers shouldn't get the soft hint
        (or be marked ``is_fps_game``) just because GameServerManager
        landed in ``place.scripts`` mid-subphase."""
        # Empty place — no user FPS markers anywhere.
        pl = fps_pipeline_factory(None)
        pl.state.rbx_place = RbxPlace(
            scripts=[],
            workspace_parts=[],
            screen_guis=[],
        )
        pl._subphase_inject_autogen_scripts()
        # GameServerManager + CollisionFidelityRecook may be appended,
        # but the FPS hint shouldn't have triggered: is_fps_game stays
        # falsy and no FPS controller / HUD landed.
        assert not getattr(pl.state.rbx_place, "is_fps_game", False)
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "HUDController" not in names
        assert "FpsClient" not in names


class TestBackwardCompatMigration:
    """Output directories created before this PR have
    ``conversion_context.json`` files with no ``scaffolding`` field
    AND ``scripts/HUDController.luau`` (or ``FpsClient.luau``) on
    disk from the prior pipeline's auto-inject. Resuming/rebuilding
    those would silently drop the FPS scripts. The subphase migrates
    by inferring ``scaffolding=["fps"]`` when ALL of:
       1. ``self.scaffolding`` is empty (no explicit opt-in),
       2. ``scripts/HUDController.luau`` or ``scripts/FpsClient.luau``
          exists on disk (evidence of a pre-PR FPS conversion).

    The on-disk signal is reliable because those filenames are ONLY
    written by ``scaffolding.fps.inject_fps_scripts`` — they don't appear on a
    fresh non-FPS conversion. Distinguishes "resumed from pre-PR FPS
    conversion" from "fresh post-PR run" without false positives.
    """

    @staticmethod
    def _make_pipeline_with_disk_artifacts(
        tmp_path: Path,
        files: dict[str, str],
        *,
        is_resume: bool = True,
    ) -> Pipeline:
        """Pre-write disk artifacts BEFORE constructing the Pipeline,
        so ``_fps_artifacts_at_init`` (snapshotted in __init__) sees
        them.

        Sets ``_is_resume = True`` by default to mirror real resume
        flows (where ``Pipeline.resume()`` or
        ``convert_interactive._make_pipeline``'s ctx-swap branch
        marks the flag). Pass ``is_resume=False`` for tests that
        simulate a fresh ``run_all`` against an output dir with
        leftover FPS scripts.
        """
        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        scripts_dir = out / "scripts"
        scripts_dir.mkdir()
        for name, content in files.items():
            (scripts_dir / name).write_text(content)

        pl = Pipeline(unity_project_path=project, output_dir=out)
        pl.state.rbx_place = _fps_shaped_place()
        pl._is_resume = is_resume
        # Mirror the real resume paths (``Pipeline.resume`` /
        # ``convert_interactive._make_pipeline`` / ``u2r.py publish``
        # rebuild): re-snapshot the FPS-artifact signal AFTER the
        # ctx swap so the rbxlx scan respects ``ctx.selected_scene``.
        if is_resume:
            pl._fps_artifacts_at_init = pl._fps_artifacts_on_disk()
        return pl

    def test_migration_infers_fps_when_hud_script_on_disk(
        self, tmp_path: Path,
    ) -> None:
        # Pre-write the disk artifact, then construct the Pipeline so
        # the at-init snapshot picks up the file.
        pl = self._make_pipeline_with_disk_artifacts(tmp_path, {
            "HUDController.luau": (
                "-- HUD Controller (auto-generated)\n"
                "-- Updates health bar, ammo counter, and item indicators\n"
            ),
        })
        assert pl.scaffolding == frozenset()
        pl._subphase_inject_autogen_scripts()
        # Migration kicks in — scaffolding now carries fps.
        assert "fps" in pl.scaffolding
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "AutoFpsHudController" in names

    def test_migration_infers_fps_when_fpsclient_on_disk(
        self, tmp_path: Path,
    ) -> None:
        """Either canonical auto-generated script triggers the migration."""
        pl = self._make_pipeline_with_disk_artifacts(tmp_path, {
            "FpsClient.luau": (
                "-- FPS Client Controller (auto-generated)\n"
                "-- WASD movement + mouse look + raycast shooting\n"
            ),
        })
        pl._subphase_inject_autogen_scripts()
        assert "fps" in pl.scaffolding

    def test_migration_skips_fresh_conversion(
        self, fps_pipeline_factory,
    ) -> None:
        """A fresh conversion has no FPS scripts on disk yet — even
        if the user content trips ``detect_fps_game``. The migration
        must NOT fire, otherwise the opt-in contract is broken on
        the standard pipeline path (``completed_phases`` was a bad
        signal — every phase marks complete on first run too)."""
        pl = fps_pipeline_factory(None)
        # No scripts/ dir, no FPS artifacts — fresh run.
        pl._subphase_inject_autogen_scripts()
        assert pl.scaffolding == frozenset()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "HUDController" not in names

    def test_migration_skips_when_unrelated_scripts_on_disk(
        self, fps_pipeline_factory,
    ) -> None:
        """A scripts/ dir full of user scripts that aren't
        HUDController/FpsClient must NOT trigger the migration."""
        pl = fps_pipeline_factory(None)
        scripts_dir = pl.output_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "Player.luau").write_text("-- user player script\n")
        (scripts_dir / "Door.luau").write_text("-- user door script\n")
        pl._subphase_inject_autogen_scripts()
        assert pl.scaffolding == frozenset()

    def test_migration_recognises_legacy_fpscontroller_filename(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (PR #69 round 2): the controller script
        is emitted as ``FPSController.luau`` (caps), not
        ``FpsClient.luau``. A pre-PR output dir whose HUD script was
        pruned but whose controller script remains must still
        trigger the migration."""
        pl = self._make_pipeline_with_disk_artifacts(tmp_path, {
            "FPSController.luau": (
                "-- FPS Client Controller (auto-generated)\n"
                "-- WASD movement + mouse look + raycast shooting\n"
            ),
        })
        pl._subphase_inject_autogen_scripts()
        assert "fps" in pl.scaffolding

    def test_migration_scoped_to_selected_scene_rbxlx(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (PR #69 round 5): in a multi-scene
        output dir, a marker in ``main.rbxlx`` (the FPS scene) must
        NOT migrate the whole conversion when the selected scene is
        ``menu`` (a non-FPS scene). The scan looks at the
        selected-scene-specific rbxlx first.

        Tests ``_fps_artifacts_on_disk`` directly with a
        scene-scoped ctx — the snapshot is taken AFTER ctx is
        loaded in real resume paths.
        """
        project = tmp_path / "Project"
        scenes_dir = project / "Assets" / "Scenes"
        scenes_dir.mkdir(parents=True)
        (scenes_dir / "main.unity").touch()
        (scenes_dir / "menu.unity").touch()
        out = tmp_path / "output"
        out.mkdir()
        # main.rbxlx has the marker; menu.rbxlx does not.
        (out / "main.rbxlx").write_text(
            '<roblox><Item><ProtectedString><![CDATA[\n'
            '-- HUD Controller (auto-generated)\n'
            ']]></ProtectedString></Item></roblox>\n'
        )
        (out / "menu.rbxlx").write_text(
            '<roblox><Item><ProtectedString><![CDATA[\n'
            '-- User-authored menu logic\n'
            ']]></ProtectedString></Item></roblox>\n'
        )

        pl = Pipeline(unity_project_path=project, output_dir=out)
        # Set selected_scene to the non-FPS one (mimics the post-
        # ctx-swap state of a real resume).
        pl.ctx.selected_scene = str(scenes_dir / "menu.unity")
        # Scoped to menu.rbxlx (which has no marker) → no signal.
        assert pl._fps_artifacts_on_disk() is False
        # Now flip the selected scene to the FPS one — same dir,
        # different scope, signal flips to True.
        pl.ctx.selected_scene = str(scenes_dir / "main.unity")
        assert pl._fps_artifacts_on_disk() is True

    def test_migration_ignores_shared_scripts_cache_in_multi_scene(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (PR #70 round 5/6): in multi-scene output
        dirs, ``scripts/`` is a shared cache populated by whichever
        scene converted last. A leftover ``HUDController.luau`` from
        a prior FPS-scene conversion must NOT migrate the menu-scene
        resume to ``scaffolding=['fps']``.

        The discriminator is ``ctx.scenes_metadata`` (populated only
        by ``run_all_scenes``) — NOT ``ctx.selected_scene``, which is
        set on every run including single-scene conversions and so
        would falsely gate off the scripts-dir signal for legitimate
        single-scene resumes.
        """
        project = tmp_path / "Project"
        scenes_dir = project / "Assets" / "Scenes"
        scenes_dir.mkdir(parents=True)
        (scenes_dir / "main.unity").touch()
        (scenes_dir / "menu.unity").touch()
        out = tmp_path / "output"
        out.mkdir()
        # Shared scripts cache: an FPS-scene leftover sits here.
        scripts_dir = out / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "HUDController.luau").write_text(
            "-- HUD Controller (auto-generated)\n"
        )
        # main.rbxlx + menu.rbxlx — multi-scene shape; neither has
        # the FPS marker (the prior FPS scene's scripts/ cache is the
        # only stale artifact).
        (out / "main.rbxlx").write_text(
            '<roblox version="4"></roblox>\n'
        )
        (out / "menu.rbxlx").write_text(
            '<roblox version="4"></roblox>\n'
        )

        pl = Pipeline(unity_project_path=project, output_dir=out)
        # Mimic the post-resume state: ``run_all_scenes`` populated
        # scenes_metadata for both scenes; selected_scene is the
        # currently-targeted one.
        pl.ctx.scenes_metadata = {
            "main": {"parts": 0, "scripts": 0, "game_objects": 0},
            "menu": {"parts": 0, "scripts": 0, "game_objects": 0},
        }
        pl.ctx.selected_scene = str(scenes_dir / "menu.unity")
        # Shared scripts/ cache must not signal migration when in a
        # true multi-scene state — rbxlx is the authoritative scope.
        assert pl._fps_artifacts_on_disk() is False

    def test_migration_honours_scripts_cache_in_single_scene(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (PR #70 round 6): a real single-scene
        resume populates ``ctx.selected_scene`` too — keying multi-
        scene off ``selected_scene`` would silently drop FPS HUD/
        controller migration on legitimate same-project resumes.

        ``scenes_metadata`` is the precise discriminator: empty
        means single-scene, populated means ``run_all_scenes``.
        """
        project = tmp_path / "Project"
        scenes_dir = project / "Assets" / "Scenes"
        scenes_dir.mkdir(parents=True)
        (scenes_dir / "main.unity").touch()
        out = tmp_path / "output"
        out.mkdir()
        # Single-scene shape: only converted_place.rbxlx (no marker)
        # plus a pre-flag FPS leftover in scripts/.
        scripts_dir = out / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "HUDController.luau").write_text(
            "-- HUD Controller (auto-generated)\n"
        )
        (out / "converted_place.rbxlx").write_text(
            '<roblox version="4"></roblox>\n'
        )

        pl = Pipeline(unity_project_path=project, output_dir=out)
        # selected_scene IS set even on single-scene resumes — the
        # codex round-6 finding pinned this exact failure mode.
        pl.ctx.selected_scene = str(scenes_dir / "main.unity")
        # scenes_metadata stays empty → single-scene → scripts/ cache
        # signal must fire so legitimate FPS resumes still migrate.
        assert pl.ctx.scenes_metadata == {}
        assert pl._fps_artifacts_on_disk() is True

    def test_migration_skips_canonical_rbxlx_when_scoped_exists(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P3] (PR #70 round 5): when a scene-scoped
        rbxlx exists (``<scene>.rbxlx``), the fallback must NOT also
        scan ``converted_place.rbxlx``. Otherwise a stale canonical
        snapshot from a prior single-scene FPS conversion would
        contaminate a multi-scene non-FPS scene resume.
        """
        project = tmp_path / "Project"
        scenes_dir = project / "Assets" / "Scenes"
        scenes_dir.mkdir(parents=True)
        (scenes_dir / "menu.unity").touch()
        out = tmp_path / "output"
        out.mkdir()
        # Stale canonical rbxlx with the FPS marker (from a prior
        # single-scene FPS conversion the user has since pivoted).
        (out / "converted_place.rbxlx").write_text(
            '<roblox version="4">\n'
            '  <Item class="Script">\n'
            '    <Properties>\n'
            '      <ProtectedString name="Source"><![CDATA[\n'
            '-- HUD Controller (auto-generated)\n'
            ']]></ProtectedString>\n'
            '    </Properties>\n'
            '  </Item>\n'
            '</roblox>\n'
        )
        # Fresh menu.rbxlx — the new scene-scoped artifact, no marker.
        (out / "menu.rbxlx").write_text(
            '<roblox version="4"></roblox>\n'
        )

        pl = Pipeline(unity_project_path=project, output_dir=out)
        pl.ctx.selected_scene = str(scenes_dir / "menu.unity")
        # Scoped match exists; canonical must NOT be scanned.
        assert pl._fps_artifacts_on_disk() is False

    def test_migration_recognises_multi_scene_rbxlx_filenames(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (PR #69 round 2): ``run_all_scenes``
        writes per-scene rbxlx files (e.g. ``main.rbxlx``,
        ``menu.rbxlx``) rather than ``converted_place.rbxlx``. The
        rbxlx fallback must scan every ``*.rbxlx`` in the output dir,
        not just the canonical name, otherwise multi-scene rebuilds
        with a pruned scripts cache silently lose FPS scaffolding."""
        from core.conversion_context import ConversionContext

        project = tmp_path / "Project"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        # Multi-scene output: no converted_place.rbxlx; per-scene files.
        (out / "main.rbxlx").write_text(
            '<roblox version="4">\n'
            '  <Item class="Script">\n'
            '    <Properties>\n'
            '      <ProtectedString name="Source"><![CDATA[\n'
            '-- HUD Controller (auto-generated)\n'
            ']]></ProtectedString>\n'
            '    </Properties>\n'
            '  </Item>\n'
            '</roblox>\n'
        )
        (out / "menu.rbxlx").write_text(
            '<roblox version="4"></roblox>\n'
        )
        prior = ConversionContext(unity_project_path=str(project))
        prior.save(out / "conversion_context.json")

        pl = Pipeline(unity_project_path=project, output_dir=out)
        pl._is_resume = True
        # Mirror the real resume paths: re-snapshot after the
        # mocked ctx swap so the rbxlx scan respects ctx state.
        pl._fps_artifacts_at_init = pl._fps_artifacts_on_disk()
        # Glob matched main.rbxlx → migration fires.
        assert pl._fps_artifacts_at_init is True
        pl.state.rbx_place = _fps_shaped_place()
        pl._subphase_inject_autogen_scripts()
        assert "fps" in pl.scaffolding

    def test_migration_signal_falls_back_to_rbxlx_when_scripts_pruned(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (PR #69 round 1): a user who archived
        or pruned ``scripts/`` after a pre-PR FPS conversion still
        has the canonical rbxlx output. The migration must find the
        FPS auto-gen marker in that rbxlx so rebuild paths
        (``u2r.py publish`` rebuild, interactive ``upload``) don't
        silently drop the FPS scripts.
        """
        from core.conversion_context import ConversionContext

        project = tmp_path / "Project"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        # No scripts/ dir — pruned. rbxlx remains with the marker
        # embedded in an inline ProtectedString source block.
        (out / "converted_place.rbxlx").write_text(
            '<roblox version="4">\n'
            '  <Item class="Script">\n'
            '    <Properties>\n'
            '      <string name="Name">HUDController</string>\n'
            '      <ProtectedString name="Source"><![CDATA[\n'
            '-- HUD Controller (auto-generated)\n'
            '-- Updates health bar, ammo counter, and item indicators\n'
            ']]></ProtectedString>\n'
            '    </Properties>\n'
            '  </Item>\n'
            '</roblox>\n'
        )
        prior = ConversionContext(unity_project_path=str(project))
        prior.save(out / "conversion_context.json")

        pl = Pipeline(unity_project_path=project, output_dir=out)
        pl.state.rbx_place = _fps_shaped_place()
        # Simulate the explicit-resume mark that real publish/
        # interactive paths set after the ctx swap.
        pl._is_resume = True
        # Mirror the real resume paths: re-snapshot after the
        # mocked ctx swap so the rbxlx scan respects ctx state.
        pl._fps_artifacts_at_init = pl._fps_artifacts_on_disk()
        # rbxlx fallback caught the marker even though scripts/ is gone.
        assert pl._fps_artifacts_at_init is True
        pl._subphase_inject_autogen_scripts()
        assert "fps" in pl.scaffolding

    def test_migration_skips_when_rbxlx_lacks_autogen_marker(
        self, tmp_path: Path,
    ) -> None:
        """A rbxlx that doesn't carry the FPS auto-gen marker (e.g.
        a non-FPS conversion's output) must NOT trigger the
        migration via the new fallback signal. User-authored scripts
        bundled in the rbxlx don't carry the canonical marker."""
        from core.conversion_context import ConversionContext

        project = tmp_path / "Project"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        (out / "converted_place.rbxlx").write_text(
            '<roblox version="4">\n'
            '  <Item class="Script">\n'
            '    <Properties>\n'
            '      <string name="Name">UserScript</string>\n'
            '      <ProtectedString name="Source"><![CDATA[\n'
            '-- User-authored gameplay script\n'
            'local x = 1\n'
            ']]></ProtectedString>\n'
            '    </Properties>\n'
            '  </Item>\n'
            '</roblox>\n'
        )
        prior = ConversionContext(unity_project_path=str(project))
        prior.save(out / "conversion_context.json")

        pl = Pipeline(unity_project_path=project, output_dir=out)
        pl._is_resume = True
        # Mirror the real resume paths: re-snapshot after the
        # mocked ctx swap so the rbxlx scan respects ctx state.
        pl._fps_artifacts_at_init = pl._fps_artifacts_on_disk()
        assert pl._fps_artifacts_at_init is False
        pl.state.rbx_place = _fps_shaped_place()
        pl._subphase_inject_autogen_scripts()
        assert pl.scaffolding == frozenset()

    def test_migration_signal_handles_chunk_boundary_marker(
        self, tmp_path: Path,
    ) -> None:
        """The streaming scanner reads the rbxlx in 64KB chunks. A
        marker straddling the boundary between two chunks must still
        be detected — the scanner keeps the last
        (max_marker_len - 1) bytes from the prior chunk to bridge.
        """
        project = tmp_path / "Project"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        # Pad to push the marker across the 64KB chunk boundary.
        # Marker is 33 chars; pad to land it at offset 65520 so it
        # spans bytes 65520..65553 (across the 65536 boundary).
        marker = "-- HUD Controller (auto-generated)"
        pad_len = 65520
        content = b"x" * pad_len + marker.encode("utf-8") + b"y" * 1024
        (out / "converted_place.rbxlx").write_bytes(content)

        pl = Pipeline(unity_project_path=project, output_dir=out)
        pl._is_resume = True
        # Mirror the real resume paths: re-snapshot after the
        # mocked ctx swap so the rbxlx scan respects ctx state.
        pl._fps_artifacts_at_init = pl._fps_artifacts_on_disk()
        # Scanner found the boundary-spanning marker.
        assert pl._fps_artifacts_at_init is True

    def test_migration_skips_user_authored_hudcontroller(
        self, tmp_path: Path,
    ) -> None:
        """A Unity project that ships its own ``HUDController.cs`` will
        transpile to ``HUDController.luau`` in ``scripts/`` on every
        conversion. The migration must NOT misclassify that file as
        evidence of a pre-PR FPS conversion — the auto-generated
        marker comment is the discriminator."""
        pl = self._make_pipeline_with_disk_artifacts(tmp_path, {
            # User-authored content — no auto-generated marker.
            "HUDController.luau": (
                '-- User HUD controller (transpiled from HUDController.cs)\n'
                'local Players = game:GetService("Players")\n'
            ),
        })
        pl._subphase_inject_autogen_scripts()
        assert pl.scaffolding == frozenset()

    def test_migration_skips_full_convert_rerun(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (round 9): a same-project rerun via
        ``u2r.py convert``/``Pipeline.run_all()`` must NOT migrate
        even when FPS scripts exist on disk. Full conversions
        regenerate from source — the user opted out of scaffolding
        by NOT passing ``--scaffolding=fps``, and the new opt-in
        default takes precedence over preserving leftover artifacts.

        Migration only fires for EXPLICIT resume paths
        (``Pipeline.resume()`` or the publish-rebuild fallback);
        ``run_all`` leaves ``_is_resume = False``.
        """
        pl = self._make_pipeline_with_disk_artifacts(
            tmp_path,
            {
                "HUDController.luau": (
                    "-- HUD Controller (auto-generated)\n"
                    "-- legacy artifact from prior FPS run\n"
                ),
            },
            is_resume=False,  # full convert, not a resume
        )
        # Disk signal IS present (verified via direct scan), but the
        # cached snapshot stays False because the fixture only
        # re-snapshots on resume — mirroring ``run_all`` which never
        # snapshots either. Both gates false → no migration.
        assert pl._fps_artifacts_on_disk() is True
        assert pl._fps_artifacts_at_init is False
        assert pl._is_resume is False
        pl._subphase_inject_autogen_scripts()
        assert pl.scaffolding == frozenset()

    def test_cross_project_resume_clears_persisted_scaffolding(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P1] (PR #69 round 4): a cross-project resume
        must NOT inherit the prior project's persisted scaffolding.
        Otherwise ``u2r.py convert ProjectB -o ./projectA-output
        --phase write_output`` would inject ProjectA's FPS HUD into
        ProjectB despite the project-mismatch warning.
        """
        from core.conversion_context import ConversionContext

        project_a = tmp_path / "ProjectA"
        (project_a / "Assets").mkdir(parents=True)
        project_b = tmp_path / "ProjectB"
        (project_b / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        # Persist ctx for ProjectA with FPS scaffolding marked.
        prior = ConversionContext(unity_project_path=str(project_a))
        prior.scaffolding = ["fps"]
        prior.save(out / "conversion_context.json")

        # Pipeline init for ProjectB resuming from ProjectA's ctx.
        pl = Pipeline(unity_project_path=project_b, output_dir=out)
        try:
            pl.resume("write_output")
        except Exception:
            pass
        # Cross-project → resume flag stays False AND persisted
        # scaffolding is cleared so it doesn't leak into ProjectB.
        assert pl._is_resume is False
        assert pl.scaffolding == frozenset()

    def test_resume_validates_project_match(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (round 10): ``Pipeline.resume()`` setting
        ``_is_resume = True`` unconditionally allowed a cross-project
        ``u2r.py convert <new-project> -o <old-output> --phase ...``
        to inherit migration/scaffolding from the persisted ctx of
        the old project. Validate the persisted ``unity_project_path``
        matches THIS Pipeline's project before classifying as a true
        same-project resume.
        """
        from core.conversion_context import ConversionContext

        project_a = tmp_path / "ProjectA"
        (project_a / "Assets").mkdir(parents=True)
        project_b = tmp_path / "ProjectB"
        (project_b / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        # Persist ctx for ProjectA with FPS scaffolding marked.
        prior = ConversionContext(unity_project_path=str(project_a))
        prior.scaffolding = ["fps"]
        prior.save(out / "conversion_context.json")
        # Drop a legacy FPS artifact that would normally trigger migration.
        scripts_dir = out / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "HUDController.luau").write_text(
            "-- HUD Controller (auto-generated)\n"
        )

        # Pipeline init for ProjectB — different project. Resume
        # against this output dir.
        pl = Pipeline(unity_project_path=project_b, output_dir=out)
        # Force an attempt at resume() — but resume() only runs phases,
        # so we manually trigger the resume() path's ctx-load logic.
        # Easiest: call resume("write_output") which goes through the
        # ctx-load + project-match validation. But run_through doesn't
        # need the actual phases to run for the test — we can stop
        # before any phase by mocking or by inspecting state right
        # after the load.
        try:
            pl.resume("write_output")
        except Exception:
            # Phases will fail without real Unity content; we only
            # care about the post-load state.
            pass
        # Cross-project resume → same-project flag must be False.
        assert pl._is_resume is False

    def test_resume_marks_same_project_as_resume(
        self, tmp_path: Path,
    ) -> None:
        """Same-project resume → ``_is_resume`` True (the migration
        path is allowed)."""
        from core.conversion_context import ConversionContext

        project = tmp_path / "Project"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()
        prior = ConversionContext(unity_project_path=str(project))
        prior.save(out / "conversion_context.json")

        pl = Pipeline(unity_project_path=project, output_dir=out)
        try:
            pl.resume("write_output")
        except Exception:
            pass
        assert pl._is_resume is True

    def test_migration_fires_only_on_explicit_resume(
        self, tmp_path: Path,
    ) -> None:
        """Same disk state, ``is_resume=True`` (the explicit-resume
        marker set by ``Pipeline.resume()`` and the publish-rebuild
        path) → migration fires. Combined with the previous test,
        this pins the resume-vs-rerun discriminator."""
        pl = self._make_pipeline_with_disk_artifacts(
            tmp_path,
            {
                "HUDController.luau": (
                    "-- HUD Controller (auto-generated)\n"
                ),
            },
            is_resume=True,
        )
        pl._subphase_inject_autogen_scripts()
        assert "fps" in pl.scaffolding

    def test_migration_fires_only_on_autogen_marker(
        self, tmp_path: Path,
    ) -> None:
        """The auto-generated marker comment IS the discriminator —
        flip the migration on by adding the marker to a same-named
        file. Same file path, different content → different result."""
        pl = self._make_pipeline_with_disk_artifacts(tmp_path, {
            # Auto-generated content — has the canonical marker.
            "HUDController.luau": (
                '-- HUD Controller (auto-generated)\n'
                '-- Updates health bar, ammo counter, and item indicators\n'
            ),
        })
        pl._subphase_inject_autogen_scripts()
        assert "fps" in pl.scaffolding


class TestFpsScaffoldingClobberProtection:
    """Codex finding [P2] (round 10): the bootstrap-filter in
    ``_subphase_inject_autogen_scripts`` runs BEFORE
    ``inject_fps_scripts``, so on a fresh ``--scaffolding=fps`` run
    ``has_fps_controller`` was False and anti-FPS modules (those
    that set ``MouseBehavior=Default`` or ``MouseIconEnabled=true``)
    didn't get filtered out of ``ClientBootstrap``'s require list.
    Those modules then clobbered the FPS controller's mouse lock at
    runtime.

    Fix: ``has_fps_controller`` now also considers
    ``"fps" in self.scaffolding`` — opt-in is treated as evidence
    that an FPS controller will be injected, so anti-FPS modules
    are filtered preemptively.
    """

    def test_anti_fps_module_filtered_when_fps_opt_in(
        self, tmp_path: Path,
    ) -> None:
        from core.roblox_types import RbxPlace as _Place

        project = tmp_path / "fakeproject"
        (project / "Assets").mkdir(parents=True)
        out = tmp_path / "output"
        out.mkdir()

        anti_fps = RbxScript(
            name="MenuController",
            source=(
                "-- Menu controller (sets default mouse on enter)\n"
                "local UserInputService = game:GetService('UserInputService')\n"
                "UserInputService.MouseBehavior = Enum.MouseBehavior.Default\n"
                "UserInputService.MouseIconEnabled = true\n"
                "local function onEnter() end\n"
                "RunService.Heartbeat:Connect(onEnter)\n"
            ),
            script_type="ModuleScript",
        )
        pl = Pipeline(
            unity_project_path=project,
            output_dir=out,
            scaffolding=["fps"],
        )
        pl.state.rbx_place = _Place(
            scripts=[anti_fps],
            workspace_parts=[],
            screen_guis=[],
        )
        pl._subphase_inject_autogen_scripts()
        # ClientBootstrap shouldn't require the anti-FPS module.
        bootstrap = next(
            (s for s in pl.state.rbx_place.scripts if s.name == "ClientBootstrap"),
            None,
        )
        if bootstrap is not None:
            assert "MenuController" not in bootstrap.source, (
                "anti-FPS MenuController slipped into ClientBootstrap "
                "even though --scaffolding=fps was set"
            )


class TestInjectFpsScriptsIdempotent:
    """``inject_fps_scripts`` must be safe to call against a place
    that already contains a HUDController from a prior run. Without
    a guard, a second ``assemble``/``publish`` rebuild against an
    existing FPS output dir appends a duplicate HUDController — both
    listeners fire on the same HUD events and double-update health/
    ammo/items.
    """

    def test_does_not_double_inject_hud_controller(self) -> None:
        from converter.scaffolding.fps import (
            inject_fps_scripts, generate_hud_client_script,
        )

        # Place already has the auto-gen AutoFpsHudController
        # (rehydrated from prior run).
        place = RbxPlace(
            scripts=[generate_hud_client_script()],
            workspace_parts=[],
            screen_guis=[],
        )
        inject_fps_scripts(place)
        hud_count = sum(
            1 for s in place.scripts if s.name == "AutoFpsHudController"
        )
        assert hud_count == 1, (
            f"expected 1 AutoFpsHudController, got {hud_count} — "
            "duplicate injection on rerun double-fires HUD updates"
        )

    def test_dedupe_recognises_legacy_hudcontroller_name(self) -> None:
        """Codex finding [P2] (round 8): a pre-PR rebuild path may
        rehydrate the legacy ``HUDController.luau`` (auto-gen, with
        marker) into ``place.scripts``. Name-only matching against
        the new ``AutoFpsHudController`` would miss that legacy
        script and append a SECOND auto-gen HUD listener — both
        run, double-handling HealthUpdate/AmmoUpdate/ItemUpdate.

        Marker-based dedupe (across both canonical names) keeps the
        legacy script as the single auto-gen listener.
        """
        from converter.scaffolding.fps import inject_fps_scripts

        place = RbxPlace(
            scripts=[
                # Legacy auto-gen HUDController rehydrated from disk
                # (pre-PR conversions wrote this exact name + marker).
                RbxScript(
                    name="HUDController",
                    source=(
                        "-- HUD Controller (auto-generated)\n"
                        "-- legacy script from a pre-PR FPS conversion\n"
                    ),
                    script_type="LocalScript",
                ),
            ],
            workspace_parts=[],
            screen_guis=[],
        )
        inject_fps_scripts(place)
        # Total auto-gen HUD listeners (matched by marker, any name)
        # must remain 1 — no duplicate appended under the new name.
        autogen_huds = [
            s for s in place.scripts
            if "-- HUD Controller (auto-generated)" in s.source
        ]
        assert len(autogen_huds) == 1, (
            f"expected 1 auto-gen HUD listener, got {len(autogen_huds)} "
            "— legacy HUDController name not recognised"
        )

    def test_first_invocation_still_injects_hud(self) -> None:
        """A truly fresh place still gets the AutoFpsHudController —
        the guard short-circuits only when one already exists."""
        from converter.scaffolding.fps import inject_fps_scripts

        place = RbxPlace(scripts=[], workspace_parts=[], screen_guis=[])
        inject_fps_scripts(place)
        hud_count = sum(
            1 for s in place.scripts if s.name == "AutoFpsHudController"
        )
        assert hud_count == 1

    def test_user_authored_hudcontroller_does_not_suppress_inject(
        self,
    ) -> None:
        """Codex finding [P2] (round 6): a project with its own
        ``HUDController.cs`` (transpiled to a HUDController LocalScript)
        must NOT cause the auto-generated FPS HUD listener to be
        skipped. With the rename to ``AutoFpsHudController``, the two
        scripts coexist on disk under different filenames — the user's
        custom HUD logic and the auto-generated event-listener wiring
        for the FPS HUD ScreenGui both run."""
        from converter.scaffolding.fps import inject_fps_scripts

        place = RbxPlace(
            scripts=[
                # User-authored HUDController — different content,
                # serves the user's gameplay (e.g. a weapon-wheel HUD).
                RbxScript(
                    name="HUDController",
                    source=(
                        "-- User HUDController (from HUDController.cs)\n"
                        "local weaponWheel = workspace:WaitForChild('WeaponWheel')\n"
                    ),
                    script_type="LocalScript",
                ),
            ],
            workspace_parts=[],
            screen_guis=[],
        )
        inject_fps_scripts(place)
        # User's HUDController is preserved (not replaced).
        user_huds = [
            s for s in place.scripts if s.name == "HUDController"
        ]
        assert len(user_huds) == 1
        assert "weaponWheel" in user_huds[0].source
        # Auto-gen is added under the distinct AutoFpsHudController name.
        auto_huds = [
            s for s in place.scripts if s.name == "AutoFpsHudController"
        ]
        assert len(auto_huds) == 1
        assert "-- HUD Controller (auto-generated)" in auto_huds[0].source
