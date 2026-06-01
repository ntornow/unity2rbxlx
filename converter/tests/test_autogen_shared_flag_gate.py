"""Phase 2b reframe step 2 (slice 3) — gate the PlayerSetSharedFlag funnel.

Design doc (``scene-runtime-architecture-ir.md`` §"Phase 2b — cross-domain
authority (two bridge classes)", deliverable 2 + the Testing subsection's
"Slice 3 (gate + record)" bullet):

> Slice 3 gates autogen's funnel injection on a ``shared_flag_channels``
> fact (emit the ``PlayerSetSharedFlag`` funnel ONLY when the fact says a
> cross-domain shared-flag reader exists). The runtime funnel mechanism is
> KEPT. RESUME / absent-fact → the gate FAILS OPEN (funnel kept).

Two layers of coverage:

1. ``generate_game_server_script`` unit: the ``include_shared_flag_funnel``
   parameter splices the funnel in (default ``True``, byte-identical to the
   pre-slice-3 monolith) or omits ONLY that block (``False``), keeping
   ``PlayerShoot`` + ``PlayerGetItem`` + spawn handling unconditional.

2. ``_subphase_inject_autogen_scripts`` pipeline gate: reads ``present``
   off the TRANSIENT stash ``self.state.shared_flag_channels`` (step-2 R2:
   the gate's read site moved off ``ctx.scene_runtime["topology"]``, which
   was a production no-op — ``ctx.scene_runtime`` is the planner's
   pre-topology dict, and the ``topology`` block is built only on the merged
   local dict and never written back to ctx). ``present: False`` → funnel
   omitted; ``present: True`` / unset stash (legacy) / resume-shape → funnel
   kept (fail-safe). The ``if not existing_server_mgr`` guard injects once.

   See ``test_shared_flag_funnel_realpath.py`` for the REAL-WIRING
   integration test that drives the actual prepass→classify→inject path
   (the regression guard for the no-op these unit tests masked).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import generate_game_server_script  # noqa: E402
from converter.pipeline import Pipeline  # noqa: E402
from converter.scene_runtime_topology.shared_flag_channels import (  # noqa: E402
    FUNNEL_EVENT_NAME,
    SharedFlagChannel,
    SharedFlagChannels,
)
from core.roblox_types import RbxPlace  # noqa: E402


# The exact funnel signature lines. If the funnel is present, the source
# contains the RemoteEvent name AND the listener body; if omitted, neither.
_FUNNEL_EVENT_NAME = "PlayerSetSharedFlag"
_FUNNEL_LISTENER = "sharedFlagRemote.OnServerEvent:Connect"


# ---------------------------------------------------------------------------
# Layer 1 — generator unit
# ---------------------------------------------------------------------------

class TestGenerateGameServerScriptFunnelParam:
    def test_default_includes_funnel(self) -> None:
        """Default ``include_shared_flag_funnel=True`` keeps today's
        behavior — the funnel block is present."""
        src = generate_game_server_script().source
        assert _FUNNEL_EVENT_NAME in src
        assert _FUNNEL_LISTENER in src

    def test_explicit_true_includes_funnel(self) -> None:
        src = generate_game_server_script(
            include_shared_flag_funnel=True,
        ).source
        assert _FUNNEL_EVENT_NAME in src
        assert _FUNNEL_LISTENER in src

    def test_false_omits_only_funnel(self) -> None:
        """``include_shared_flag_funnel=False`` omits the funnel block
        but KEEPS PlayerShoot + PlayerGetItem + spawn handling."""
        src = generate_game_server_script(
            include_shared_flag_funnel=False,
        ).source
        assert _FUNNEL_EVENT_NAME not in src
        assert _FUNNEL_LISTENER not in src
        # The other two RemoteEvents + spawn handling stay.
        assert "PlayerShoot" in src
        assert "PlayerGetItem" in src
        assert "findSpawnPoint" in src
        assert "shootRemote.OnServerEvent:Connect" in src
        assert "getItemRemote.OnServerEvent:Connect" in src

    def test_include_true_byte_identical_to_concatenation(self) -> None:
        """The ``include=True`` source is exactly head + funnel + tail —
        the split is non-lossy (no separator drift)."""
        from converter import autogen

        expected = (
            autogen._GAME_SERVER_HEAD
            + autogen._SHARED_FLAG_FUNNEL_FRAGMENT
            + autogen._GAME_SERVER_TAIL
        )
        assert generate_game_server_script().source == expected

    def test_omitted_source_keeps_clean_separator(self) -> None:
        """With the funnel omitted, PlayerGetItem flows into the
        visual-hit-feedback section with a clean blank separator (the
        blank lives at the tail of the head fragment)."""
        src = generate_game_server_script(
            include_shared_flag_funnel=False,
        ).source
        assert (
            "getItemRemote.Parent = ReplicatedStorage\n\n"
            "-- Visual hit feedback\n"
        ) in src

    def test_script_name_and_type_unchanged_either_way(self) -> None:
        for include in (True, False):
            script = generate_game_server_script(
                include_shared_flag_funnel=include,
            )
            assert script.name == "GameServerManager"
            assert script.script_type == "Script"


# ---------------------------------------------------------------------------
# Layer 2 — pipeline gate at _subphase_inject_autogen_scripts
# ---------------------------------------------------------------------------

def _make_pipeline(tmp_path: Path) -> Pipeline:
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    return Pipeline(str(unity_project), str(output))


def _stage(
    pipeline: Pipeline,
    channels: SharedFlagChannels | None,
) -> None:
    """Stage a minimal place + TRANSIENT stash so
    ``_subphase_inject_autogen_scripts`` runs and injects
    GameServerManager.

    Step-2 R2: the gate reads ``state.shared_flag_channels`` (the
    run-scoped stash the prepass sets), NOT ``ctx.scene_runtime``. These
    Layer-2 tests therefore set the stash directly. ``None`` models the
    legacy / no-prepass case (stash never set)."""
    pipeline.state.rbx_place = RbxPlace()
    pipeline.state.shared_flag_channels = channels


def _game_server_source(pipeline: Pipeline) -> str:
    assert pipeline.state.rbx_place is not None
    mgrs = [
        s for s in pipeline.state.rbx_place.scripts
        if s.name == "GameServerManager"
    ]
    assert len(mgrs) == 1, (
        f"expected exactly one GameServerManager, got {len(mgrs)}"
    )
    return mgrs[0].source


def _channel(present: bool) -> SharedFlagChannels:
    return {
        FUNNEL_EVENT_NAME: SharedFlagChannel(
            read_names=["hasKey"] if present else [],
            reader_domains=["server"] if present else [],
            canonical_stores=["Character", "Player"],
            present=present,
        ),
    }


class TestSharedFlagFunnelGate:
    def test_present_false_omits_funnel(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        _stage(pipeline, _channel(present=False))
        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME not in src
        assert _FUNNEL_LISTENER not in src
        # The rest of GameServerManager is still injected.
        assert "PlayerShoot" in src
        assert "PlayerGetItem" in src

    def test_present_true_includes_funnel(self, tmp_path: Path) -> None:
        pipeline = _make_pipeline(tmp_path)
        _stage(pipeline, _channel(present=True))
        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME in src
        assert _FUNNEL_LISTENER in src

    def test_unset_stash_fails_open(self, tmp_path: Path) -> None:
        """Stash never set (legacy mode / no topology prepass) →
        fail-safe to today's unconditional injection."""
        pipeline = _make_pipeline(tmp_path)
        _stage(pipeline, None)  # stash unset
        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME in src
        assert _FUNNEL_LISTENER in src

    def test_stash_without_funnel_channel_fails_open(
        self, tmp_path: Path,
    ) -> None:
        """Stash present but missing the ``PlayerSetSharedFlag`` key (a
        future multi-funnel shape, or a partial fact) → fail-safe to
        funnel-included."""
        pipeline = _make_pipeline(tmp_path)
        _stage(pipeline, {})  # SharedFlagChannels with no funnel key
        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME in src

    def test_resume_shape_includes_funnel(self, tmp_path: Path) -> None:
        """Step-1 resume fail-open shape (``present: True`` with empty
        ``read_names``) → funnel kept."""
        pipeline = _make_pipeline(tmp_path)
        resume: SharedFlagChannels = {
            FUNNEL_EVENT_NAME: SharedFlagChannel(
                read_names=[],
                reader_domains=[],
                canonical_stores=["Character", "Player"],
                present=True,
            ),
        }
        _stage(pipeline, resume)
        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME in src
        assert _FUNNEL_LISTENER in src

    def test_helper_reads_present_directly(self, tmp_path: Path) -> None:
        """The typed accessor returns the stashed fact's ``present`` value
        and defaults True when the stash is unset or missing the funnel
        channel."""
        pipeline = _make_pipeline(tmp_path)
        pipeline.state.shared_flag_channels = _channel(present=False)
        assert pipeline._shared_flag_funnel_present() is False
        pipeline.state.shared_flag_channels = _channel(present=True)
        assert pipeline._shared_flag_funnel_present() is True
        pipeline.state.shared_flag_channels = None
        assert pipeline._shared_flag_funnel_present() is True
        # Stash present but no funnel channel → fail-safe True.
        pipeline.state.shared_flag_channels = {}
        assert pipeline._shared_flag_funnel_present() is True

    def test_idempotent_single_injection(self, tmp_path: Path) -> None:
        """The ``if not existing_server_mgr`` guard injects exactly once;
        a second run does not append a duplicate (and does not flip the
        funnel)."""
        pipeline = _make_pipeline(tmp_path)
        _stage(pipeline, _channel(present=False))
        pipeline._subphase_inject_autogen_scripts()
        pipeline._subphase_inject_autogen_scripts()
        assert pipeline.state.rbx_place is not None
        mgrs = [
            s for s in pipeline.state.rbx_place.scripts
            if s.name == "GameServerManager"
        ]
        assert len(mgrs) == 1
        assert _FUNNEL_EVENT_NAME not in mgrs[0].source
