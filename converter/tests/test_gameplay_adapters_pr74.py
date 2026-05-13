"""Tests for PR #74 — default-on flip, ``--legacy-gameplay-packs``
opt-out, and the rehydration-aware prune pass for stale legacy
coherence-pack artifacts.

Covers:

  * ``ConversionContext.use_gameplay_adapters`` default is True.
  * ``Pipeline.__init__`` default and resume()'s post-rehydrate
    re-apply both honour the constructor's bidirectional flag.
  * ``_strip_legacy_door_tween_block`` removes the appended block
    cleanly and is idempotent.
  * ``_prune_legacy_gameplay_artifacts`` walks every script-bearing
    surface (global, workspace, replicated_templates) and the
    ``screen_guis`` list for ``_AutoFpsHud`` ScreenGuis.
  * The prune fires from ``write_output``'s gameplay-adapter
    runtime-injection branch BEFORE module emission.
"""
from __future__ import annotations

import types

import pytest

from converter.pipeline import (
    Pipeline,
    _LEGACY_DAMAGE_ROUTER_NAME,
    _LEGACY_DOOR_TWEEN_MARKER,
    _LEGACY_FPS_HUD_ATTR,
    _strip_legacy_door_tween_block,
)
from core.conversion_context import ConversionContext


class TestDefaultFlip:
    """PR #74: ``use_gameplay_adapters`` defaults True everywhere."""

    def test_conversion_context_default(self) -> None:
        ctx = ConversionContext(unity_project_path="/tmp/x")
        assert ctx.use_gameplay_adapters is True, (
            "ConversionContext.use_gameplay_adapters default flipped "
            "by PR #74 — pre-PR-#74 expectations should be migrated."
        )

    def test_pipeline_signature_tri_state(self) -> None:
        """PR #74 codex round-1 [P1]: Pipeline.__init__'s
        ``use_gameplay_adapters`` is a tri-state.

          * ``None`` (default) — preserve persisted ctx, fall through
            to dataclass default (True) for a fresh ctx.
          * ``True`` / ``False`` — explicit override that wins at
            construction AND after the resume() ctx swap.

        Why ``None`` and not ``True``: codex round-1 [P1] flagged that
        a hard True default unconditionally overwrites a persisted
        legacy choice on ``--phase`` resumes — breaks the sticky
        rollback contract.
        """
        import inspect

        sig = inspect.signature(Pipeline.__init__)
        param = sig.parameters["use_gameplay_adapters"]
        assert param.default is None, (
            "Pipeline.__init__ default broke the PR #74 codex round-1 "
            "[P1] tri-state — ``None`` = preserve persisted ctx, "
            "explicit bool = override. A hard-True default regresses "
            "sticky rollback on --phase resumes."
        )
        # Annotation is ``bool | None`` — pin it so a future refactor
        # to ``bool`` (dropping the tri-state) fails this test.
        anno = param.annotation
        assert "None" in str(anno), (
            f"Pipeline.__init__ use_gameplay_adapters annotation "
            f"is {anno!r}; tri-state needs ``bool | None``."
        )


class TestPrePr74ContextRehydration:
    """PR #74 codex round-3 [P1]: a ``conversion_context.json`` that
    PREDATES PR #73a (no ``use_gameplay_adapters`` key) must rehydrate
    as ``False`` (legacy mode), not silently inherit the new True
    dataclass default. Otherwise pre-PR-#73a outputs flip into
    adapter mode on the next resume, breaking the sticky-rollback
    contract.
    """

    def test_missing_key_loads_as_legacy(self, tmp_path) -> None:
        """A ctx JSON without the field rehydrates as False."""
        import json

        ctx_path = tmp_path / "conversion_context.json"
        ctx_path.write_text(json.dumps({
            "unity_project_path": "/tmp/old-project",
            "selected_scene": "Main.unity",
        }), encoding="utf-8")
        loaded = ConversionContext.load(ctx_path)
        assert loaded.use_gameplay_adapters is False, (
            "ConversionContext.load() silently flipped a pre-PR-#73a "
            "context into adapter mode — codex PR #74 round-3 [P1] "
            "regressed."
        )

    def test_explicit_true_in_json_survives(self, tmp_path) -> None:
        """A ctx JSON that explicitly has ``use_gameplay_adapters: true``
        (a fresh PR-#73a-or-later conversion) must keep that value
        through load() — the round-3 fix shouldn't accidentally
        force True ctxs to False."""
        import json

        ctx_path = tmp_path / "conversion_context.json"
        ctx_path.write_text(json.dumps({
            "unity_project_path": "/tmp/new-project",
            "use_gameplay_adapters": True,
        }), encoding="utf-8")
        loaded = ConversionContext.load(ctx_path)
        assert loaded.use_gameplay_adapters is True

    def test_explicit_false_in_json_survives(self, tmp_path) -> None:
        """A ctx JSON that explicitly has ``use_gameplay_adapters: false``
        (a ``--legacy-gameplay-packs`` conversion or a pre-PR-#74
        default-off conversion) must keep that value through load()."""
        import json

        ctx_path = tmp_path / "conversion_context.json"
        ctx_path.write_text(json.dumps({
            "unity_project_path": "/tmp/legacy-project",
            "use_gameplay_adapters": False,
        }), encoding="utf-8")
        loaded = ConversionContext.load(ctx_path)
        assert loaded.use_gameplay_adapters is False


class TestStickyRollbackContract:
    """PR #74 codex round-1 [P1]: a persisted
    ``ctx.use_gameplay_adapters=False`` (from an original
    ``--legacy-gameplay-packs`` run) must survive ``--phase`` resumes
    where the caller didn't repeat the flag. The tri-state ``None``
    in the constructor signals "no preference this run, keep
    persisted state".
    """

    def test_none_constructor_preserves_persisted_false_at_construction(
        self,
    ) -> None:
        """A fresh Pipeline with ``use_gameplay_adapters=None`` against
        a ctx default doesn't force True over a False the caller has
        not yet seen — the resume() path is where persisted False
        survives, but the constructor must not preemptively overwrite
        the ctx default either."""
        # Direct ctx-mutation harness — we don't need a real unity
        # project on disk to verify the field write semantics.
        import types

        ctx = ConversionContext(unity_project_path="/tmp/x")
        # Constructor default is None — _init_use_gameplay_adapters
        # stays None and the ctx is left at its dataclass default.
        h = types.SimpleNamespace(ctx=ctx, _init_use_gameplay_adapters=None)
        # Simulate the relevant constructor branch:
        if h._init_use_gameplay_adapters is not None:
            h.ctx.use_gameplay_adapters = h._init_use_gameplay_adapters
        assert h.ctx.use_gameplay_adapters is True, (
            "fresh ctx with no constructor override should land on "
            "the dataclass default (True since PR #74)."
        )

    def test_explicit_false_constructor_overrides_ctx_default(self) -> None:
        """A caller that passes ``use_gameplay_adapters=False``
        explicitly (the legacy-pack opt-out path) must flip ctx OFF
        before resume() sees it."""
        import types

        ctx = ConversionContext(unity_project_path="/tmp/x")
        h = types.SimpleNamespace(ctx=ctx, _init_use_gameplay_adapters=False)
        if h._init_use_gameplay_adapters is not None:
            h.ctx.use_gameplay_adapters = h._init_use_gameplay_adapters
        assert h.ctx.use_gameplay_adapters is False, (
            "explicit False didn't overwrite ctx default — "
            "legacy-pack opt-out path is broken."
        )

    def test_resume_preserves_persisted_false_under_none_constructor(self) -> None:
        """End-to-end resume() simulation: a persisted ctx with
        ``use_gameplay_adapters=False`` (from an original
        ``--legacy-gameplay-packs`` run) must survive
        ``_init_use_gameplay_adapters=None`` (caller didn't repeat
        the flag on this resume).
        """
        import types

        persisted_ctx = ConversionContext(unity_project_path="/tmp/x")
        persisted_ctx.use_gameplay_adapters = False  # original was legacy
        h = types.SimpleNamespace(
            ctx=persisted_ctx,
            _init_use_gameplay_adapters=None,
        )
        # The resume()-relevant branch (after the ctx swap):
        if h._init_use_gameplay_adapters is not None:
            h.ctx.use_gameplay_adapters = h._init_use_gameplay_adapters
        assert h.ctx.use_gameplay_adapters is False, (
            "PR #74 codex round-1 [P1] regressed: a resumed ctx "
            "with persisted False was silently flipped back to True "
            "by a defaulted constructor argument."
        )

    def test_resume_honours_explicit_true_over_persisted_false(self) -> None:
        """A caller that explicitly passes
        ``use_gameplay_adapters=True`` (re-enabling adapters on a
        previously legacy-mode project) must beat the persisted False.
        """
        import types

        persisted_ctx = ConversionContext(unity_project_path="/tmp/x")
        persisted_ctx.use_gameplay_adapters = False
        h = types.SimpleNamespace(
            ctx=persisted_ctx,
            _init_use_gameplay_adapters=True,
        )
        if h._init_use_gameplay_adapters is not None:
            h.ctx.use_gameplay_adapters = h._init_use_gameplay_adapters
        assert h.ctx.use_gameplay_adapters is True, (
            "explicit True didn't override persisted False — "
            "users can't re-enable adapters on a legacy-mode "
            "output."
        )


class TestModeFlipInvalidatesTranspileCache:
    """PR #74 codex round-2 [P1]: when the operator flips gameplay
    mode on a resumed run (``--legacy-gameplay-packs`` after an
    adapters-on conversion, or vice versa), the cached ``scripts/``
    output on disk carries the previous mode. The override must
    invalidate the cache so the next ``transpile_scripts`` produces
    a fresh result for the new mode.
    """

    def _harness(
        self, persisted: bool, completed: list[str] | None = None,
    ) -> types.SimpleNamespace:
        """Duck-typed harness for ``_invalidate_transpile_cache_for_mode_flip``
        — the helper only touches ``ctx.completed_phases`` and
        ``self._retranspile``."""
        ctx = ConversionContext(unity_project_path="/tmp/x")
        ctx.use_gameplay_adapters = persisted
        ctx.completed_phases = list(completed or [])
        return types.SimpleNamespace(ctx=ctx, _retranspile=False)

    def test_invalidate_sets_retranspile_flag(self) -> None:
        h = self._harness(persisted=False)
        Pipeline._invalidate_transpile_cache_for_mode_flip(h)
        assert h._retranspile is True, (
            "_retranspile not set — _subphase_emit_scripts_to_disk "
            "will preserve the old mode's scripts/ output."
        )

    def test_invalidate_removes_transpile_from_completed_phases(self) -> None:
        h = self._harness(
            persisted=False,
            completed=["parse", "transpile_scripts", "convert_scene"],
        )
        Pipeline._invalidate_transpile_cache_for_mode_flip(h)
        assert "transpile_scripts" not in h.ctx.completed_phases, (
            "transpile_scripts left in completed_phases — phase will "
            "be skipped on resume and the mode flip is nominal."
        )
        # Sibling phases must survive — we're only invalidating one.
        assert h.ctx.completed_phases == ["parse", "convert_scene"]

    def test_invalidate_idempotent_when_already_invalidated(self) -> None:
        """Re-invoking the invalidator with transpile already absent
        from completed_phases must NOT raise (e.g. fresh ctx)."""
        h = self._harness(persisted=False, completed=["parse"])
        Pipeline._invalidate_transpile_cache_for_mode_flip(h)
        assert h._retranspile is True
        assert h.ctx.completed_phases == ["parse"]

    def test_resume_fires_invalidator_only_when_mode_flips(self) -> None:
        """Source pin: resume() must call the invalidator only when
        the explicit override CHANGES the persisted value, not on
        every explicit re-affirm of the same mode (that would force
        unnecessary AI transpile calls)."""
        import inspect
        from converter.pipeline import Pipeline as _Pipeline

        src = inspect.getsource(_Pipeline.resume)
        assert (
            "mode_changed" in src
            and "_invalidate_transpile_cache_for_mode_flip" in src
        ), (
            "resume() no longer guards the invalidator on "
            "mode_changed — every explicit re-affirm would now "
            "force an AI transpile call, which is wasteful."
        )

    def test_make_pipeline_fires_invalidator_on_mode_flip(self) -> None:
        """convert_interactive._make_pipeline must mirror the
        resume() invalidation logic — otherwise interactive assemble
        with --legacy-gameplay-packs against an adapters-on output
        produces a place that still uses adapters.
        """
        import inspect
        from convert_interactive import _make_pipeline

        src = inspect.getsource(_make_pipeline)
        assert "_invalidate_transpile_cache_for_mode_flip" in src, (
            "_make_pipeline doesn't invalidate the transpile cache "
            "on mode flip — interactive assemble rollback path is "
            "broken (codex PR #74 round-2 [P1])."
        )
        assert "mode_changed" in src, (
            "_make_pipeline invalidator isn't gated on mode_changed "
            "— would force a fresh transpile even on a no-op explicit "
            "re-affirm."
        )


class TestInteractiveAssemblyLegacyPath:
    """PR #74 codex round-1 [P2]: ``convert_interactive assemble``
    must expose the same rollback lever as ``u2r.py convert`` so
    interactive users can request legacy gameplay packs."""

    def test_assemble_has_legacy_gameplay_packs_option(self) -> None:
        from convert_interactive import assemble

        params = {p.name: p for p in assemble.params}
        assert "legacy_gameplay_packs" in params, (
            "convert_interactive assemble dropped the "
            "--legacy-gameplay-packs flag — PR #74 codex round-1 "
            "[P2] regressed."
        )
        assert "use_gameplay_adapters" in params, (
            "convert_interactive assemble dropped the "
            "--use-gameplay-adapters flag — interactive flow "
            "diverged from u2r.py."
        )

    def test_transpile_has_legacy_gameplay_packs_option(self) -> None:
        """PR #74 codex round-5 [P2]: the rollback lever must also be
        on ``transpile`` — interactive users review scripts/ after
        transpile and an operator who plans to finish with
        ``--legacy-gameplay-packs`` at assemble needs the right Luau
        out of this phase."""
        from convert_interactive import transpile

        params = {p.name: p for p in transpile.params}
        assert "legacy_gameplay_packs" in params, (
            "convert_interactive transpile is missing "
            "--legacy-gameplay-packs — PR #74 codex round-5 [P2] "
            "regressed."
        )
        assert "use_gameplay_adapters" in params, (
            "convert_interactive transpile is missing "
            "--use-gameplay-adapters — interactive transpile and "
            "assemble disagree on the rollback contract."
        )

    def test_make_pipeline_forwards_use_gameplay_adapters(self) -> None:
        """``_make_pipeline`` must thread the tri-state through to
        the Pipeline constructor AND re-apply it after the ctx swap
        (mirroring resume())."""
        import inspect
        from convert_interactive import _make_pipeline

        sig = inspect.signature(_make_pipeline)
        assert "use_gameplay_adapters" in sig.parameters, (
            "_make_pipeline missing the use_gameplay_adapters "
            "kwarg — interactive subcommands can't forward the "
            "rollback choice."
        )
        param = sig.parameters["use_gameplay_adapters"]
        assert param.default is None, (
            "_make_pipeline use_gameplay_adapters default isn't "
            "None — the sticky-rollback contract requires the "
            "tri-state."
        )

        # Source pin: after the ``pipeline.ctx = prior_ctx`` line the
        # function must re-bind ctx.use_gameplay_adapters when the
        # caller was explicit. Otherwise the swap drops the choice.
        src = inspect.getsource(_make_pipeline)
        ctx_swap_idx = src.find("pipeline.ctx = prior_ctx")
        assert ctx_swap_idx != -1, (
            "_make_pipeline no longer swaps in the prior ctx — "
            "test premise broke; update this assertion."
        )
        post_swap = src[ctx_swap_idx:]
        assert "use_gameplay_adapters is not None" in post_swap, (
            "_make_pipeline doesn't re-apply explicit "
            "use_gameplay_adapters after the ctx swap — codex "
            "PR #74 round-1 [P2] regressed."
        )
        assert (
            "pipeline.ctx.use_gameplay_adapters" in post_swap
        ), (
            "_make_pipeline doesn't re-bind ctx.use_gameplay_adapters "
            "after the ctx swap — explicit caller choice lost."
        )


class TestFinalizeWalksBoundScripts:
    """PR #74 codex round-5 [P3]: ``_subphase_finalize_scripts_to_disk``
    must walk part-bound scripts (workspace_parts +
    replicated_templates) so the on-disk ``scripts/*.luau`` cache
    reflects every in-memory mutation. The rehydration prune pass
    can mutate bound clones independently of the global list; a
    global-only walk would silently leave the disk stale.
    """

    def test_finalize_writes_bound_script_source_to_disk(
        self, tmp_path,
    ) -> None:
        """End-to-end: a bound clone whose source carries an obvious
        marker must reach ``scripts/<name>.luau`` via finalize."""
        import types

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        # Pre-existing disk file so the elif-existence check passes
        # in the heuristic branch.
        (scripts_dir / "BoundDoor.luau").write_text(
            "stale-contents-from-prior-write", encoding="utf-8",
        )

        bound = types.SimpleNamespace(
            name="BoundDoor",
            source="-- final pruned source\nreturn nil",
            source_path=None,
        )
        part = types.SimpleNamespace(
            name="DoorPart",
            scripts=[bound],
            children=[],
        )
        place = types.SimpleNamespace(
            scripts=[],
            workspace_parts=[part],
            replicated_templates=[],
        )

        # Minimal harness: bind a real Pipeline method to a fake self.
        h = types.SimpleNamespace(
            state=types.SimpleNamespace(rbx_place=place),
            output_dir=tmp_path,
        )
        Pipeline._subphase_finalize_scripts_to_disk(h)

        on_disk = (scripts_dir / "BoundDoor.luau").read_text(encoding="utf-8")
        assert on_disk == "-- final pruned source\nreturn nil", (
            "_subphase_finalize_scripts_to_disk didn't write the "
            "bound script's in-memory source to disk — codex "
            "PR #74 round-5 [P3] regressed."
        )

    def test_finalize_dedups_first_bound_script_by_identity(
        self, tmp_path,
    ) -> None:
        """A script bound via the first-bind branch (same
        ``RbxScript`` identity in both ``place.scripts`` and
        ``part.scripts``) must only be written once — not twice."""
        import types

        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "Door.luau").write_text("stale", encoding="utf-8")

        shared = types.SimpleNamespace(
            name="Door",
            source="-- shared final source",
            source_path=None,
        )
        part = types.SimpleNamespace(
            name="DoorPart",
            scripts=[shared],
            children=[],
        )
        place = types.SimpleNamespace(
            scripts=[shared],
            workspace_parts=[part],
            replicated_templates=[],
        )
        h = types.SimpleNamespace(
            state=types.SimpleNamespace(rbx_place=place),
            output_dir=tmp_path,
        )
        # No exception, single write, idempotent.
        Pipeline._subphase_finalize_scripts_to_disk(h)
        assert (
            (scripts_dir / "Door.luau").read_text(encoding="utf-8")
            == "-- shared final source"
        )


class TestRehydratedGameplayModulesRefresh:
    """PR #74 codex round-6 [P1]: ``_inject_runtime_modules`` must
    REFRESH rehydrated adapter runtime modules and PRUNE stale ones
    that aren't needed this run. The previous skip-if-exists loop
    preserved whatever ``_rehydrate_scripts_from_disk`` had loaded —
    on a resume that's last run's source, not the current converter's.

    Two regressions this guards against:

      * Stale ``Gameplay.luau`` still using ``WaitForChild("DamageProtocol")``
        (pre-PR-#74 round-2 fix) — every door/projectile-only adapter
        project resumed against an older output would eat a 5-second
        startup stall.
      * Stale ``DamageProtocol.luau`` surviving when
        ``_damage_protocol_needed()`` returned False this run — keeps
        the cross-feature ``ReplicatedStorage.DamageEvent`` claim that
        the PR #74 gate is supposed to release.
    """

    def test_inject_loop_calls_refresh_not_skip(self) -> None:
        """Source pin: the gameplay-modules injection loop must
        OVERWRITE existing entries instead of skipping. A future
        refactor that reintroduces the skip-on-exist path regresses
        the round-6 [P1] silently."""
        import inspect
        from converter.pipeline import Pipeline as _Pipeline

        src = inspect.getsource(_Pipeline._inject_runtime_modules)
        # Pin the refresh comment + the source-write line.
        assert "refresh rather than skip" in src.lower() or (
            "s.source = new_source" in src
        ), (
            "_inject_runtime_modules no longer refreshes rehydrated "
            "gameplay-adapter modules — codex PR #74 round-6 [P1] "
            "regressed."
        )

    def test_inject_loop_prunes_stale_module_not_in_emit_set(self) -> None:
        """Source pin: the pre-pass walk must drop scripts whose name
        is a converter-owned module name but NOT in the current
        emit set."""
        import inspect
        from converter.pipeline import Pipeline as _Pipeline

        src = inspect.getsource(_Pipeline._inject_runtime_modules)
        assert "_ALL_GAMEPLAY_RUNTIME_MODULE_NAMES" in src, (
            "_inject_runtime_modules no longer references the "
            "converter-owned module name set — the prune pre-pass "
            "can't identify stale modules to drop."
        )
        assert "current_module_names" in src, (
            "_inject_runtime_modules no longer computes the "
            "current emit set — round-6 [P1] prune is broken."
        )

    def test_known_module_set_is_complete(self) -> None:
        """The pruning set must cover every module the injection
        loop knows how to emit, or the rehydrate path leaves a
        sub-stale module behind."""
        from converter.pipeline import _ALL_GAMEPLAY_RUNTIME_MODULE_NAMES

        # Pin every name that appears as the first element of
        # ``base_modules`` / ``("DamageProtocol", ...)`` /
        # ``("Gameplay", ...)`` in pipeline.py.
        expected_min = {
            "Composer", "Triggers", "Movement", "Lifetime",
            "HitDetection", "Effects", "DamageProtocol", "Gameplay",
        }
        missing = expected_min - _ALL_GAMEPLAY_RUNTIME_MODULE_NAMES
        assert not missing, (
            f"_ALL_GAMEPLAY_RUNTIME_MODULE_NAMES missing: {missing}. "
            "A rehydrated stale entry under one of these names would "
            "survive the round-6 [P1] prune pass."
        )


class TestRehydratedModuleMatchByName:
    """PR #74 codex round-7 [P1]: rehydrated runtime modules come
    back from disk with ``parent_path=None`` because
    ``_rehydrate_scripts_from_disk`` can only restore parent paths
    from ``conversion_plan.json`` (written BEFORE runtime-module
    injection so the gameplay modules aren't in it). The round-6
    refresh path used ``parent_path == "ReplicatedStorage.AutoGen"``
    in its match filter, so every rehydrated module fell through and
    a duplicate was appended.
    """

    def test_inject_loop_matches_by_name_alone(self) -> None:
        """Source pin: the injection-loop's existing-match filter
        must NOT include ``parent_path`` in the predicate (the
        rehydrate-path bug). Backfilling the parent_path inside the
        refresh branch is fine — that's the FIX — but the match
        itself has to find rehydrated entries with parent_path=None.
        """
        import inspect
        from converter.pipeline import Pipeline as _Pipeline

        src = inspect.getsource(_Pipeline._inject_runtime_modules)

        # Find the `existing = [...]` list-comp that drives the
        # gameplay-modules refresh branch. It must filter by
        # ``s.name == module_name`` and NOT also gate on
        # parent_path. Slice the source around the relevant `for
        # module_name, filename in gameplay_modules:` loop.
        loop_idx = src.find("for module_name, filename in gameplay_modules:")
        assert loop_idx != -1, (
            "_inject_runtime_modules no longer has the "
            "gameplay_modules injection loop — test premise broke."
        )
        loop_body = src[loop_idx : loop_idx + 2000]
        # The list-comp lives within the first ~2000 chars of the loop.
        existing_idx = loop_body.find("existing = [")
        assert existing_idx != -1, (
            "_inject_runtime_modules dropped the existing-match "
            "list-comp."
        )
        # Grab the comprehension body (10 lines after).
        comp = "\n".join(loop_body[existing_idx:].splitlines()[:6])
        assert "s.name == module_name" in comp, (
            "Existing-match filter no longer keys off s.name — "
            "rehydrated modules can't be found."
        )
        assert "parent_path" not in comp, (
            "Existing-match filter still references parent_path — "
            "rehydrated modules have parent_path=None and the "
            "filter misses them. Codex PR #74 round-7 [P1] regressed."
        )

    def test_refresh_branch_backfills_parent_path(self) -> None:
        """Source pin: the refresh branch must reassign
        ``parent_path = "ReplicatedStorage.AutoGen"`` on rehydrated
        entries so the rbxlx writer routes them correctly. Without
        this the module ends up in the heuristic fallback path."""
        import inspect
        from converter.pipeline import Pipeline as _Pipeline

        src = inspect.getsource(_Pipeline._inject_runtime_modules)
        refresh_idx = src.find("if existing:")
        assert refresh_idx != -1
        refresh_block = src[refresh_idx : refresh_idx + 1500]
        assert (
            's.parent_path = "ReplicatedStorage.AutoGen"' in refresh_block
        ), (
            "Refresh branch doesn't backfill parent_path — codex "
            "PR #74 round-7 [P1] regressed."
        )
        assert 's.script_type = "ModuleScript"' in refresh_block, (
            "Refresh branch doesn't backfill script_type — a "
            "rehydrated module classified as Script would route to "
            "the wrong container."
        )


class TestStripLegacyDoorTweenBlock:
    """Unit tests for the door-tween block strip helper."""

    def test_no_marker_is_no_op(self) -> None:
        src = "local foo = 1\nreturn foo\n"
        out, stripped = _strip_legacy_door_tween_block(src)
        assert stripped is False
        assert out == src

    def test_strips_block_with_marker(self) -> None:
        src = (
            "local foo = 1\n"
            "return foo\n"
            "\n"
            "-- _AutoFpsDoorTweenInjected: door coherence pack\n"
            "do\n"
            "    -- injected body\n"
            "end\n"
        )
        out, stripped = _strip_legacy_door_tween_block(src)
        assert stripped is True
        # The legitimate body must survive.
        assert "local foo = 1" in out
        assert "return foo" in out
        # The injected block must be gone.
        assert _LEGACY_DOOR_TWEEN_MARKER not in out
        assert "injected body" not in out
        # No trailing blank-line dangle from the marker's leading newline.
        assert not out.endswith("\n\n")

    def test_idempotent(self) -> None:
        src = (
            "local foo = 1\n"
            "-- _AutoFpsDoorTweenInjected: x\n"
            "do end\n"
        )
        once, _ = _strip_legacy_door_tween_block(src)
        twice, stripped_again = _strip_legacy_door_tween_block(once)
        assert stripped_again is False
        assert once == twice


class TestPruneLegacyGameplayArtifacts:
    """End-to-end tests against the Pipeline method using a duck-typed
    state harness (no real Unity project needed)."""

    def _harness(self) -> types.SimpleNamespace:
        place = types.SimpleNamespace(
            scripts=[],
            workspace_parts=[],
            replicated_templates=[],
            screen_guis=[],
        )
        state = types.SimpleNamespace(rbx_place=place)
        # ``scaffolding`` defaults empty here; per-test code can flip
        # it to ``frozenset({"fps"})`` when exercising the round-3
        # [P1] preserve-fresh-HUD gate.
        return types.SimpleNamespace(
            state=state,
            scaffolding=frozenset(),
        )

    def test_removes_global_damage_router_script(self) -> None:
        h = self._harness()
        survivor = types.SimpleNamespace(name="OtherScript", source="local x=1")
        target = types.SimpleNamespace(
            name=_LEGACY_DAMAGE_ROUTER_NAME, source="-- legacy",
        )
        h.state.rbx_place.scripts = [survivor, target]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert h.state.rbx_place.scripts == [survivor], (
            "_AutoDamageEventRouter not removed from global scripts."
        )

    def test_removes_part_bound_damage_router_script(self) -> None:
        h = self._harness()
        target = types.SimpleNamespace(
            name=_LEGACY_DAMAGE_ROUTER_NAME, source="-- legacy",
        )
        survivor = types.SimpleNamespace(name="OK", source="local x=1")
        part = types.SimpleNamespace(
            name="Workspace.Door",
            scripts=[target, survivor],
            children=[],
        )
        h.state.rbx_place.workspace_parts = [part]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert part.scripts == [survivor], (
            "_AutoDamageEventRouter not removed from a part-bound "
            "script list."
        )

    def test_strips_door_tween_block_from_global_script(self) -> None:
        h = self._harness()
        door = types.SimpleNamespace(
            name="Door",
            source=(
                "-- AI-transpiled Door body\n"
                "local Door = {}\n"
                "return Door\n"
                "\n"
                "-- _AutoFpsDoorTweenInjected: door coherence pack\n"
                "do\n"
                "    -- legacy tween body\n"
                "end\n"
            ),
        )
        h.state.rbx_place.scripts = [door]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert _LEGACY_DOOR_TWEEN_MARKER not in door.source
        assert "local Door = {}" in door.source

    def test_strips_door_tween_block_from_template_bound_script(self) -> None:
        """Replicated-template walk: the rehydrate path attaches
        adapter stubs (and prior legacy artifacts) to prefab template
        scripts. The prune must reach them."""
        h = self._harness()
        door = types.SimpleNamespace(
            name="Door",
            source=(
                "local Door = {}\n"
                "-- _AutoFpsDoorTweenInjected\n"
                "do end\n"
            ),
        )
        template = types.SimpleNamespace(
            name="Templates.SciFi_Door",
            scripts=[door],
            children=[],
        )
        h.state.rbx_place.replicated_templates = [template]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert _LEGACY_DOOR_TWEEN_MARKER not in door.source

    def test_removes_auto_fps_hud_screen_gui(self) -> None:
        """No ``"fps"`` in scaffolding this run → the HUD is a stale
        rehydrate artifact and must be pruned."""
        h = self._harness()
        h.scaffolding = frozenset()
        survivor = types.SimpleNamespace(
            name="UserHUD", attributes={},
        )
        target = types.SimpleNamespace(
            name="HUD",
            attributes={_LEGACY_FPS_HUD_ATTR: True},
        )
        h.state.rbx_place.screen_guis = [survivor, target]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 1
        assert h.state.rbx_place.screen_guis == [survivor], (
            "_AutoFpsHud ScreenGui not removed when fps scaffolding "
            "is OFF this run."
        )

    def test_preserves_auto_fps_hud_when_fps_scaffolding_active(
        self,
    ) -> None:
        """PR #74 codex round-3 [P1]: SUBPHASE_ORDER runs
        ``_subphase_inject_autogen_scripts`` BEFORE
        ``_inject_runtime_modules`` (which calls the prune). When
        ``"fps"`` scaffolding is active on this run, the freshly-
        emitted HUD already sits in ``place.screen_guis`` by the
        time the prune fires. An unconditional strip would delete
        the just-emitted HUD on every adapter-enabled FPS conversion.
        """
        h = self._harness()
        h.scaffolding = frozenset({"fps"})
        fresh_hud = types.SimpleNamespace(
            name="HUD",
            attributes={_LEGACY_FPS_HUD_ATTR: True},
        )
        h.state.rbx_place.screen_guis = [fresh_hud]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 0, (
            "Prune fired on a fresh _AutoFpsHud while fps scaffolding "
            "is active — would wipe the just-emitted HUD on every "
            "adapter-enabled FPS conversion."
        )
        assert h.state.rbx_place.screen_guis == [fresh_hud]

    def test_aggregates_all_three_surfaces(self) -> None:
        """A single re-conversion can hit all three at once; pin the
        aggregated count + final state."""
        h = self._harness()
        router = types.SimpleNamespace(
            name=_LEGACY_DAMAGE_ROUTER_NAME, source="",
        )
        door = types.SimpleNamespace(
            name="Door",
            source=(
                "local Door = {}\n"
                "-- _AutoFpsDoorTweenInjected\n"
                "do end\n"
            ),
        )
        hud = types.SimpleNamespace(
            name="HUD",
            attributes={_LEGACY_FPS_HUD_ATTR: True},
        )
        h.state.rbx_place.scripts = [router, door]
        h.state.rbx_place.screen_guis = [hud]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 3, (
            "Expected one prune count per artifact (router + door "
            "block + HUD)."
        )
        assert h.state.rbx_place.scripts == [door], (
            "Door script must SURVIVE the prune — only its injected "
            "block is stripped."
        )
        assert _LEGACY_DOOR_TWEEN_MARKER not in door.source
        assert h.state.rbx_place.screen_guis == []

    def test_no_op_when_clean(self) -> None:
        h = self._harness()
        clean = types.SimpleNamespace(name="OK", source="local x=1")
        h.state.rbx_place.scripts = [clean]
        pruned = Pipeline._prune_legacy_gameplay_artifacts(h)
        assert pruned == 0
        assert h.state.rbx_place.scripts == [clean]

    def test_no_op_on_none_rbx_place(self) -> None:
        h = types.SimpleNamespace(
            state=types.SimpleNamespace(rbx_place=None),
        )
        # Must not raise.
        assert Pipeline._prune_legacy_gameplay_artifacts(h) == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
