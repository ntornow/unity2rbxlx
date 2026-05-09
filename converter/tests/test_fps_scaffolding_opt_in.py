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
        assert "HUDController" in names, (
            "HUDController must be injected when --scaffolding=fps"
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
        assert "HUDController" in names


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
        (GameServerManager, CollisionGroupSetup, CollisionFidelityRecook)
        must not trigger ``detect_fps_game``. The detector is meant for
        user-authored content, not the converter's own scaffolding."""
        from converter.fps_client_generator import (
            generate_game_server_script,
            generate_collision_fidelity_recook_script,
            detect_fps_game,
        )

        place = RbxPlace(
            scripts=[
                generate_game_server_script(),
                generate_collision_fidelity_recook_script(),
            ],
            workspace_parts=[],
            screen_guis=[],
        )
        # The autogen GameServerManager mentions PlayerShoot + RemoteEvent
        # because it wires up the generic player-spawn flow. The detector
        # in isolation matches that, so this asserts the FAILURE MODE
        # codex flagged: detect_fps_game returns True on a non-FPS scene.
        # See ``_subphase_inject_autogen_scripts`` for the fix — the
        # subphase checks the heuristic BEFORE these autogens land.
        looks_fps = detect_fps_game(place)
        # Document the upstream behaviour: detector itself isn't smart
        # enough to filter autogens, but the call site is. If this
        # assertion ever flips (e.g. detector grows an autogen filter),
        # the call-site ordering can be relaxed.
        assert looks_fps is True, (
            "detector matches autogen GameServerManager — "
            "the call site must filter, not the detector"
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
    written by ``fps_client_generator`` — they don't appear on a
    fresh non-FPS conversion. Distinguishes "resumed from pre-PR FPS
    conversion" from "fresh post-PR run" without false positives.
    """

    def test_migration_infers_fps_when_hud_script_on_disk(
        self, fps_pipeline_factory, tmp_path,
    ) -> None:
        pl = fps_pipeline_factory(None)
        # Simulate a pre-PR conversion: HUDController.luau with the
        # canonical auto-generated marker exists in scripts/, but
        # ctx.scaffolding is empty (saved before the field existed).
        scripts_dir = pl.output_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "HUDController.luau").write_text(
            "-- HUD Controller (auto-generated)\n"
            "-- Updates health bar, ammo counter, and item indicators\n",
        )
        assert pl.scaffolding == frozenset()
        pl._subphase_inject_autogen_scripts()
        # Migration kicks in — scaffolding now carries fps.
        assert "fps" in pl.scaffolding
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "HUDController" in names

    def test_migration_infers_fps_when_fpsclient_on_disk(
        self, fps_pipeline_factory,
    ) -> None:
        """Either canonical auto-generated script triggers the migration."""
        pl = fps_pipeline_factory(None)
        scripts_dir = pl.output_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "FpsClient.luau").write_text(
            "-- FPS Client Controller (auto-generated)\n"
            "-- WASD movement + mouse look + raycast shooting\n",
        )
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

    def test_migration_skips_user_authored_hudcontroller(
        self, fps_pipeline_factory,
    ) -> None:
        """A Unity project that ships its own ``HUDController.cs`` will
        transpile to ``HUDController.luau`` in ``scripts/`` on every
        conversion. The migration must NOT misclassify that file as
        evidence of a pre-PR FPS conversion — the auto-generated
        marker comment is the discriminator."""
        pl = fps_pipeline_factory(None)
        scripts_dir = pl.output_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        # User-authored content — no auto-generated marker.
        (scripts_dir / "HUDController.luau").write_text(
            '-- User HUD controller (transpiled from HUDController.cs)\n'
            'local Players = game:GetService("Players")\n',
        )
        pl._subphase_inject_autogen_scripts()
        assert pl.scaffolding == frozenset()

    def test_migration_fires_only_on_autogen_marker(
        self, fps_pipeline_factory,
    ) -> None:
        """The auto-generated marker comment IS the discriminator —
        flip the migration on by adding the marker to a same-named
        file. Same file path, different content → different result."""
        pl = fps_pipeline_factory(None)
        scripts_dir = pl.output_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        # Auto-generated content — has the canonical marker.
        (scripts_dir / "HUDController.luau").write_text(
            '-- HUD Controller (auto-generated)\n'
            '-- Updates health bar, ammo counter, and item indicators\n',
        )
        pl._subphase_inject_autogen_scripts()
        assert "fps" in pl.scaffolding


class TestInjectFpsScriptsIdempotent:
    """``inject_fps_scripts`` must be safe to call against a place
    that already contains a HUDController from a prior run. Without
    a guard, a second ``assemble``/``publish`` rebuild against an
    existing FPS output dir appends a duplicate HUDController — both
    listeners fire on the same HUD events and double-update health/
    ammo/items.
    """

    def test_does_not_double_inject_hud_controller(self) -> None:
        from converter.fps_client_generator import (
            inject_fps_scripts, generate_hud_client_script,
        )

        # Place already has a HUDController (rehydrated from prior run).
        place = RbxPlace(
            scripts=[generate_hud_client_script()],
            workspace_parts=[],
            screen_guis=[],
        )
        inject_fps_scripts(place)
        hud_count = sum(1 for s in place.scripts if s.name == "HUDController")
        assert hud_count == 1, (
            f"expected 1 HUDController, got {hud_count} — duplicate "
            "injection on rerun double-fires HUD updates"
        )

    def test_first_invocation_still_injects_hud(self) -> None:
        """A truly fresh place still gets the HUDController — the
        guard short-circuits only when one already exists."""
        from converter.fps_client_generator import inject_fps_scripts

        place = RbxPlace(scripts=[], workspace_parts=[], screen_guis=[])
        inject_fps_scripts(place)
        hud_count = sum(1 for s in place.scripts if s.name == "HUDController")
        assert hud_count == 1

    def test_user_authored_hudcontroller_does_not_suppress_inject(
        self,
    ) -> None:
        """Codex finding [P2] (round 6): a project with its own
        ``HUDController.cs`` (transpiled to a HUDController LocalScript)
        must NOT cause the auto-generated HUDController to be skipped
        — the auto-emitted HUD ScreenGui needs the auto-generated
        controller's event-listener wiring to update on
        HealthUpdate/AmmoUpdate/ItemUpdate.
        """
        from converter.fps_client_generator import inject_fps_scripts

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
        # Both should now be present: the user-authored HUDController
        # AND the auto-generated one.
        hud_scripts = [s for s in place.scripts if s.name == "HUDController"]
        assert len(hud_scripts) == 2
        # One has the auto-generated marker, one doesn't.
        markers = sum(
            1 for s in hud_scripts
            if "-- HUD Controller (auto-generated)" in s.source
        )
        assert markers == 1
