"""Phase 2b reframe step 2 (R2) — REAL-WIRING regression guard for the
``PlayerSetSharedFlag`` funnel gate.

This is the test the R1 review was missing. The step-2 gate
(``_shared_flag_funnel_present``) originally read
``ctx.scene_runtime["topology"]["shared_flag_channels"]...``, but
``ctx.scene_runtime`` is the planner's PRE-topology dict — the
``topology`` block is built only on the MERGED LOCAL dict inside
``_classify_storage`` (``_merge_scene_runtime`` returns a fresh copy) and
is NEVER written back to ``ctx.scene_runtime`` (it is persisted to
``conversion_plan.json`` instead). So in real conversions that lookup
always MISSED → fail-open default → the funnel stayed unconditional. **The
gate was a production no-op.** The existing ``test_autogen_shared_flag_gate``
Layer-2 tests passed only by manually seeding ``ctx.scene_runtime
["topology"]`` (and now the transient stash), so they could not catch the
no-op — no test drove the real prepass→classify→inject path.

The R2 fix stashes the computed fact on the TRANSIENT run-scoped field
``state.shared_flag_channels`` inside ``_maybe_run_topology_prepass`` (the
point ``compute_shared_flag_channels`` runs), and the gate reads THAT.

These tests drive the PRODUCTION wiring end to end:

  1. Stage a real ``Pipeline`` (generic mode) with a real
     ``RbxPlace``, a ``transpilation_result`` whose Luau is the actual
     reader source, and a ``ctx.scene_runtime`` carrying the module rows
     — exactly the inputs ``materialize_and_classify`` hands to
     ``_classify_storage``.
  2. Call ``_classify_storage()`` — the REAL method that runs the
     topology prepass (``_maybe_run_topology_prepass`` →
     ``compute_shared_flag_channels`` → stash on ``state``). We do NOT
     seed ``ctx.scene_runtime["topology"]`` or the stash by hand.
  3. Call ``_subphase_inject_autogen_scripts()`` — the REAL injection
     site whose gate reads the stash.

Then assert the GameServerManager source includes/omits the funnel
correctly. ``test_no_reader_omits_funnel_via_real_path`` is the explicit
no-op regression guard: against the pre-R2 code (gate reading
``ctx.scene_runtime``) the stash would never be set from the real path
and the gate would fail open, so the funnel would WRONGLY be included
even though no cross-domain reader exists — i.e. this test fails on the
old code. (Verified manually by reverting the gate to read
``ctx.scene_runtime`` and confirming this test goes red while the seeded
Layer-2 tests stay green.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.code_transpiler import (  # noqa: E402
    TranspilationResult,
    TranspiledScript,
)
from converter.pipeline import Pipeline  # noqa: E402
from core.roblox_types import RbxPlace, RbxScript  # noqa: E402


_FUNNEL_EVENT_NAME = "PlayerSetSharedFlag"
_FUNNEL_LISTENER = "sharedFlagRemote.OnServerEvent:Connect"


def _make_pipeline(tmp_path: Path) -> Pipeline:
    unity_project = tmp_path / "unity"
    (unity_project / "Assets").mkdir(parents=True)
    output = tmp_path / "out"
    output.mkdir()
    return Pipeline(str(unity_project), str(output))


def _transpiled(name: str, luau_source: str) -> TranspiledScript:
    return TranspiledScript(
        source_path=f"{name}.cs",
        output_filename=f"{name}.luau",
        csharp_source="// src",
        luau_source=luau_source,
        strategy="ai",
        confidence=1.0,
        script_type="Script",
    )


def _stage_real_inputs(
    pipeline: Pipeline,
    *,
    reader_name: str,
    reader_luau: str,
    reader_domain: str,
) -> None:
    """Stage the inputs ``materialize_and_classify`` hands
    ``_classify_storage``: generic mode, a real ``RbxPlace`` with the
    reader script, a ``transpilation_result`` whose Luau is the reader
    source (the scan input), and a ``scene_runtime`` with the reader
    module pinned to ``reader_domain`` via ``domain_overrides`` (so the
    domain inference is deterministic without a full source-analysis
    fixture). NOTHING about ``topology`` or the transient stash is seeded
    — the prepass must produce it.
    """
    place = RbxPlace()
    place.scripts = [
        RbxScript(
            name=reader_name,
            source="// transpiled below",
            script_type="Script",
        ),
    ]
    pipeline.state.rbx_place = place

    pipeline.state.transpilation_result = TranspilationResult(
        scripts=[_transpiled(reader_name, reader_luau)],
    )

    sid = f"{reader_name}_sid"
    pipeline.ctx.scene_runtime_mode = "generic"
    pipeline.ctx.scene_runtime = {
        "modules": {
            sid: {
                "stem": reader_name,
                "class_name": reader_name,
                "runtime_bearing": True,
                "domain": reader_domain,
                "character_attached": False,
                "is_loader": False,
            },
        },
        # Pin the domain so ``infer_module_domains`` is deterministic.
        "domain_overrides": {sid: reader_domain},
    }


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


class TestSharedFlagFunnelRealPath:
    def test_no_reader_omits_funnel_via_real_path(
        self, tmp_path: Path,
    ) -> None:
        """REGRESSION GUARD (the no-op fix): a fresh generic-mode run with
        NO cross-domain shared-flag reader (the only ``GetAttribute`` read
        is in a CLIENT-domain script, which is same-domain to the funnel's
        client-origin write) → the prepass records ``present: False`` on
        the stash → the gate OMITS the funnel.

        Against the pre-R2 gate (reading ``ctx.scene_runtime["topology"]``)
        the stash is never consulted, the ctx lookup misses, the gate
        fails open, and the funnel is WRONGLY included — so this assertion
        fails on the old code. That is exactly the no-op this test guards.
        """
        pipeline = _make_pipeline(tmp_path)
        _stage_real_inputs(
            pipeline,
            reader_name="Hud",
            reader_luau='local v = plr:GetAttribute("hasKey")',
            reader_domain="client",  # same-domain → not cross-domain
        )

        # REAL prepass → compute_shared_flag_channels → stash on state.
        pipeline._classify_storage()

        # The stash was set by the real path (not seeded).
        assert pipeline.state.shared_flag_channels is not None
        channel = pipeline.state.shared_flag_channels[_FUNNEL_EVENT_NAME]
        assert channel["present"] is False, (
            "no cross-domain reader → prepass must record present=False"
        )

        # REAL injection site reads the stash → omits the funnel.
        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME not in src
        assert _FUNNEL_LISTENER not in src
        # The rest of GameServerManager is still injected.
        assert "PlayerShoot" in src
        assert "PlayerGetItem" in src

    def test_server_reader_includes_funnel_via_real_path(
        self, tmp_path: Path,
    ) -> None:
        """A fresh run WITH a cross-domain server reader of a shared flag
        (``Door:GetAttribute("hasKey")`` in a server-domain script) → the
        prepass records ``present: True`` → the gate INCLUDES the funnel.
        Drives the same real path as above; the only difference is the
        reader's domain.
        """
        pipeline = _make_pipeline(tmp_path)
        _stage_real_inputs(
            pipeline,
            reader_name="Door",
            reader_luau='local v = part:GetAttribute("hasKey")',
            reader_domain="server",  # cross-domain to the client-origin write
        )

        pipeline._classify_storage()

        assert pipeline.state.shared_flag_channels is not None
        channel = pipeline.state.shared_flag_channels[_FUNNEL_EVENT_NAME]
        assert channel["present"] is True, (
            "a server reader of a shared flag → prepass must record "
            "present=True"
        )
        assert "hasKey" in channel["read_names"]

        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME in src
        assert _FUNNEL_LISTENER in src

    def test_changed_signal_only_reader_includes_funnel_via_real_path(
        self, tmp_path: Path,
    ) -> None:
        """Fix 2 end to end: a server reader whose ONLY shared-flag access
        is ``:GetAttributeChangedSignal("hasKey")`` (the signal/watch form,
        no literal read) drives ``present: True`` through the real path →
        the funnel is included so the watched signal can fire.
        """
        pipeline = _make_pipeline(tmp_path)
        _stage_real_inputs(
            pipeline,
            reader_name="Door",
            reader_luau=(
                'part:GetAttributeChangedSignal("hasKey"):Connect(function() end)'
            ),
            reader_domain="server",
        )

        pipeline._classify_storage()

        assert pipeline.state.shared_flag_channels is not None
        channel = pipeline.state.shared_flag_channels[_FUNNEL_EVENT_NAME]
        assert channel["present"] is True
        assert "hasKey" in channel["read_names"]

        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME in src
        assert _FUNNEL_LISTENER in src

    def test_stash_reset_keeps_funnel_decision_scene_local(
        self, tmp_path: Path,
    ) -> None:
        """REGRESSION GUARD (Codex R2 P3, 2026-06-01): the funnel decision
        must be SCENE-LOCAL. A multi-scene driver reusing one Pipeline
        state could leak a prior scene's ``present=False`` verdict into a
        later scene that SKIPS classification (no user scripts → the
        ``_classify_storage`` no-scripts early-return), making
        ``write_output`` wrongly OMIT the funnel.

        The fix resets ``state.shared_flag_channels`` at the START of every
        ``_classify_storage`` attempt — before the early-return — so a
        skipped classify leaves the stash ``None`` → the gate fails open
        (keeps the funnel). Without the reset, the stash here would still
        hold scene A's ``present=False`` and the final assertion would
        fail (funnel wrongly omitted)."""
        pipeline = _make_pipeline(tmp_path)

        # Scene A: no cross-domain reader → classify records present=False.
        _stage_real_inputs(
            pipeline,
            reader_name="Hud",
            reader_luau='local v = plr:GetAttribute("hasKey")',
            reader_domain="client",
        )
        pipeline._classify_storage()
        assert pipeline.state.shared_flag_channels is not None
        assert (
            pipeline.state.shared_flag_channels[_FUNNEL_EVENT_NAME]["present"]
            is False
        )

        # Scene B on the SAME pipeline: no user scripts → classify takes
        # the no-scripts early-return and never runs the prepass. The
        # start-of-classify reset must clear A's stale verdict.
        assert pipeline.state.rbx_place is not None
        pipeline.state.rbx_place.scripts = []
        pipeline._classify_storage()
        assert pipeline.state.shared_flag_channels is None, (
            "the start-of-classify reset must clear the prior scene's "
            "verdict so a skipped classify is scene-local (None → fail-open)"
        )

        # write_output for scene B → gate reads None → fail-open → funnel
        # KEPT (not omitted from a leaked stale present=False).
        pipeline._subphase_inject_autogen_scripts()
        src = _game_server_source(pipeline)
        assert _FUNNEL_EVENT_NAME in src
        assert _FUNNEL_LISTENER in src
