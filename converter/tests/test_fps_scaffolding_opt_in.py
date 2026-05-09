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
