"""Tests for the ``runtime/gameplay/event_dispatch.luau`` ModuleScript
and its auto-injection by the pipeline when FPS scaffolding is opted in.

PR #75 reorganization
---------------------
Pre-PR-#75, ``runtime/event_dispatch.luau`` was emitted directly under
``ReplicatedStorage`` as ``AutoFpsEventDispatch``. PR #75 moves the
canonical module under ``runtime/gameplay/`` so it sits next to the
other gameplay runtime modules, parents it at
``ReplicatedStorage.AutoGen.EventDispatch``, and emits a tiny compat
alias at the historic ``ReplicatedStorage.AutoFpsEventDispatch``
location.

Pinned invariants:

  - Canonical module file lives at ``runtime/gameplay/event_dispatch.luau``
    and carries the ``@@GAMEPLAY_RUNTIME_MODULE@@`` first-line marker.
  - Pipeline emits canonical at ``ReplicatedStorage.AutoGen`` and the
    alias at ``ReplicatedStorage.AutoFpsEventDispatch`` (alias body is
    a ``WaitForChild`` chain that proxies to the canonical).
  - Alias overwrite policy: skip emission when a non-ModuleScript
    Instance already occupies the alias name; refresh in place when a
    ModuleScript is there.
  - Opt-out branch (rerun without ``--scaffolding=fps``) prunes both
    canonical and alias, matched by ``_is_converter_gameplay_runtime_module``
    so user-authored ``EventDispatch.cs`` transpilations survive.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from converter.pipeline import (
    Pipeline,
    _EVENT_DISPATCH_ALIAS_BODY,
    _EVENT_DISPATCH_ALIAS_MARKER,
    _EVENT_DISPATCH_ALIAS_NAME,
    _EVENT_DISPATCH_CANONICAL_NAME,
)
from converter.scaffolding.fps import generate_hud_client_script
from core.roblox_types import RbxPlace, RbxScript


RUNTIME_DIR = Path(__file__).parent.parent / "runtime"
GAMEPLAY_RUNTIME_DIR = RUNTIME_DIR / "gameplay"


class TestEventDispatchModuleSource:
    """The on-disk runtime ModuleScript exists with the canonical
    ``EventDispatch.connectClient`` shape. Pinned so an accidental
    refactor that drops the function or renames it can't slip past."""

    def test_module_file_exists(self) -> None:
        path = GAMEPLAY_RUNTIME_DIR / "event_dispatch.luau"
        assert path.exists(), (
            f"runtime/gameplay/event_dispatch.luau missing — "
            "auto-injection will fail at the canonical source read"
        )

    def test_module_carries_gameplay_runtime_marker(self) -> None:
        """PR #75: the canonical module must carry the
        ``@@GAMEPLAY_RUNTIME_MODULE@@`` first-line marker so the
        rehydrate predicate (``_is_converter_gameplay_runtime_module``)
        recognises it across resume runs without false-positiving on a
        user-authored ``EventDispatch.cs`` transpilation.
        """
        path = GAMEPLAY_RUNTIME_DIR / "event_dispatch.luau"
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        assert "@@GAMEPLAY_RUNTIME_MODULE@@" in first_line

    def test_module_exposes_connectclient_via_table_export(self) -> None:
        path = GAMEPLAY_RUNTIME_DIR / "event_dispatch.luau"
        source = path.read_text(encoding="utf-8")
        # Module table + named function + return.
        assert "local EventDispatch = {}" in source
        assert "function EventDispatch.connectClient" in source
        assert "return EventDispatch" in source

    def test_connectclient_dispatches_on_instance_class(self) -> None:
        """The body must fork on ``BindableEvent`` vs ``RemoteEvent``.
        Hard-coding either one breaks the producer-side flexibility
        that motivated the helper in the first place."""
        source = (GAMEPLAY_RUNTIME_DIR / "event_dispatch.luau").read_text(
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
        source = (GAMEPLAY_RUNTIME_DIR / "event_dispatch.luau").read_text(
            encoding="utf-8",
        )
        # First non-comment line of the body should be a nil guard.
        assert "if not evt then" in source
        assert "return" in source  # bare return after the nil guard

    def test_pre_pr75_top_level_module_is_gone(self) -> None:
        """PR #75 deletes ``runtime/event_dispatch.luau`` (moved under
        ``runtime/gameplay/``). Leaving the legacy file behind would
        let ``scaffolding/fps.py`` or any out-of-date import re-read
        the pre-PR-#75 body — which lacks the
        ``@@GAMEPLAY_RUNTIME_MODULE@@`` marker — and inject a stale
        copy that the rehydrate predicate would silently leave in
        place across the next refresh.
        """
        assert not (RUNTIME_DIR / "event_dispatch.luau").exists(), (
            "Stale runtime/event_dispatch.luau should have been deleted "
            "by PR #75 — canonical lives under runtime/gameplay/."
        )


class TestHudClientScriptRequiresEventDispatch:
    """The auto-generated HUDController LocalScript no longer inlines
    ``connectClient`` — it requires the runtime module instead.

    PR #75 keeps the body unchanged (still pins ``AutoFpsEventDispatch``)
    so already-converted outputs keep resolving via the alias. PR #78
    will switch to ``AutoGen.EventDispatch`` and retire the alias.
    """

    def test_hud_script_requires_event_dispatch_via_alias(self) -> None:
        script = generate_hud_client_script()
        assert 'require(ReplicatedStorage:WaitForChild("AutoFpsEventDispatch"))' in (
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


class TestPipelineInjectsEventDispatchOnOptIn:
    """Pipeline auto-injects canonical ``EventDispatch`` under
    ``ReplicatedStorage.AutoGen`` plus the ``AutoFpsEventDispatch``
    alias when ``"fps" in self.scaffolding`` so the HUDController's
    ``require`` resolves at runtime.
    """

    def test_canonical_and_alias_injected_on_fps_opt_in(
        self, tmp_path: Path,
    ) -> None:
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl._inject_runtime_modules()
        names = [s.name for s in pl.state.rbx_place.scripts]
        assert _EVENT_DISPATCH_CANONICAL_NAME in names, (
            "AutoGen.EventDispatch missing — HUDController will fail "
            "at the canonical require"
        )
        assert _EVENT_DISPATCH_ALIAS_NAME in names, (
            "AutoFpsEventDispatch alias missing — already-converted "
            "HUD bodies pinning the historic name will fail to require"
        )

    def test_canonical_parented_at_autogen(self, tmp_path: Path) -> None:
        """Canonical must be parented at ``ReplicatedStorage.AutoGen``
        so it cannot collide with a user-authored ``EventDispatch.cs``
        that transpiles directly under ``ReplicatedStorage``.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl._inject_runtime_modules()
        canonical = next(
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_CANONICAL_NAME
        )
        assert canonical.parent_path == "ReplicatedStorage.AutoGen"
        assert canonical.script_type == "ModuleScript"

    def test_canonical_uses_autogen_subdir_source_path(
        self, tmp_path: Path,
    ) -> None:
        """The canonical writes to ``scripts/AutoGen/EventDispatch.luau``
        so a user-authored ``EventDispatch.cs`` transpilation at
        ``scripts/EventDispatch.luau`` survives the finalize/rehydrate
        cycle without collision. Pinned because the on-disk collision
        is the only realistic failure mode for a user-authored
        ``EventDispatch.cs``.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl._inject_runtime_modules()
        canonical = next(
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_CANONICAL_NAME
        )
        assert canonical.source_path == "AutoGen/EventDispatch.luau"

    def test_alias_body_uses_waitforchild_chain(self, tmp_path: Path) -> None:
        """The alias body must use a two-step ``WaitForChild`` chain
        (``AutoGen`` -> ``EventDispatch``) — not a direct dot-chain —
        so early callers don't race load order.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl._inject_runtime_modules()
        alias = next(
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        )
        assert ':WaitForChild("AutoGen")' in alias.source
        assert ':WaitForChild("EventDispatch")' in alias.source
        # Direct dot-chains skipped — the design-doc-mandated form is
        # ``GetService("ReplicatedStorage"):WaitForChild(...):WaitForChild(...)``.
        # Strip comment lines before scanning so the comment reference to
        # the canonical path doesn't false-positive.
        code_lines = [
            line for line in alias.source.splitlines()
            if not line.lstrip().startswith("--")
        ]
        code = "\n".join(code_lines)
        assert "ReplicatedStorage.AutoGen.EventDispatch" not in code, (
            "Alias body must use WaitForChild chain, not direct dot-chain "
            f"-- got executable lines:\n{code}"
        )
        # Marker so the prune predicate / rehydrate path can distinguish
        # an emitted alias from a user-authored same-named ModuleScript.
        assert _EVENT_DISPATCH_ALIAS_MARKER in alias.source

    def test_alias_is_modulescript(self, tmp_path: Path) -> None:
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl._inject_runtime_modules()
        alias = next(
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        )
        assert alias.script_type == "ModuleScript"

    def test_neither_injected_without_fps_opt_in(self, tmp_path: Path) -> None:
        """No opt-in → no auto-injection. Both canonical and alias
        only matter for the HUDController's require chain, so emitting
        them on every conversion would just bloat non-FPS places.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=None)
        pl._inject_runtime_modules()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert _EVENT_DISPATCH_CANONICAL_NAME not in names
        assert _EVENT_DISPATCH_ALIAS_NAME not in names

    def test_user_authored_event_dispatch_coexists_with_canonical(
        self, tmp_path: Path,
    ) -> None:
        """PR #75: a project that ships its own ``EventDispatch.cs``
        transpiles to a top-level ``EventDispatch.luau`` parented under
        ``ReplicatedStorage`` (not under ``AutoGen``). The canonical
        EventDispatch coexists with it — same name, different
        ``parent_path`` — because the predicate that decides which
        entries to refresh keys off ``parent_path`` AND the
        ``@@GAMEPLAY_RUNTIME_MODULE@@`` marker. The user's script
        carries neither signal, so it's left alone.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name="EventDispatch",
                source=(
                    "-- User-authored EventDispatch (different purpose)\n"
                    "local M = {}\nreturn M\n"
                ),
                script_type="LocalScript",
            )
        )
        pl._inject_runtime_modules()
        ed_entries = [
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_CANONICAL_NAME
        ]
        # Two ``EventDispatch`` entries with distinct parent_paths.
        assert len(ed_entries) == 2
        user = next(
            s for s in ed_entries
            if s.script_type == "LocalScript"
        )
        canonical = next(
            s for s in ed_entries
            if s.script_type == "ModuleScript"
        )
        assert canonical.parent_path == "ReplicatedStorage.AutoGen"
        # User script untouched: original source preserved.
        assert "-- User-authored EventDispatch" in user.source
        # User script gets the top-level disk path; canonical goes
        # under AutoGen/ to avoid collision.
        assert canonical.source_path == "AutoGen/EventDispatch.luau"

    def test_canonical_idempotent_on_repeat_inject(
        self, tmp_path: Path,
    ) -> None:
        """Calling ``_inject_runtime_modules`` twice must not append a
        duplicate canonical EventDispatch — the refresh-in-place path
        keys off ``_is_converter_gameplay_runtime_module``.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl._inject_runtime_modules()
        pl._inject_runtime_modules()
        canonical_count = sum(
            1 for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_CANONICAL_NAME
        )
        alias_count = sum(
            1 for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        )
        assert canonical_count == 1
        assert alias_count == 1


class TestRehydrateRound1Regressions:
    """PR #75 codex round-1 regression pins.

    [P1] The canonical's on-disk filename stem MUST match the in-memory
    ``name`` ("EventDispatch") so the rehydrate path
    (``_rehydrate_scripts_from_disk``) reads it back under the same
    ``name`` the refresh/prune predicates key on. A lowercase
    ``event_dispatch.luau`` filename would deserialize as
    ``name="event_dispatch"`` and break idempotency on every resume.

    [P2] The alias refresh path MUST pin ``parent_path`` to
    ``ReplicatedStorage`` even when an existing entry has been
    classified elsewhere (e.g. a user-authored
    ``AutoFpsEventDispatch.cs`` routed to ``ServerStorage`` by the
    storage classifier). Without the pin the alias would refresh in
    place but stay at the classified location, leaving the
    historic ``ReplicatedStorage.AutoFpsEventDispatch`` path
    unoccupied and pre-PR-#75 HUD requires unable to resolve.
    """

    def test_canonical_filename_stem_matches_in_memory_name(
        self, tmp_path: Path,
    ) -> None:
        """The disk filename in ``source_path`` MUST be
        ``EventDispatch.luau`` (CapCase stem), not the lowercase
        ``event_dispatch.luau`` — rehydration derives the in-memory
        ``name`` from ``Path.stem``.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl._inject_runtime_modules()
        canonical = next(
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_CANONICAL_NAME
        )
        # Filename component (everything after the last slash) is
        # what ``Path.stem`` keys off after the rehydrate ``rglob``.
        stem = Path(canonical.source_path).stem
        assert stem == _EVENT_DISPATCH_CANONICAL_NAME, (
            f"on-disk stem {stem!r} would deserialize as a different "
            f"name than the predicate-keyed {_EVENT_DISPATCH_CANONICAL_NAME!r}; "
            "rehydrate would never recognise the canonical and would "
            "append a duplicate on every resume"
        )

    def test_marker_alias_misclassified_to_server_storage_is_rescued(
        self, tmp_path: Path,
    ) -> None:
        """Round-2 reconciliation: a rehydrated alias that carries
        OUR alias marker (e.g. our own previous emit) but got moved
        to ``ServerStorage`` by the storage classifier — perhaps no
        HUD caller observed in this run — must be rescued back to
        the alias path via the marker-recognition fallback. Without
        the rescue the path-scoped policy would skip the refresh and
        append a fresh alias, leaving a duplicate at the wrong
        location.
        """
        from converter.pipeline import _EVENT_DISPATCH_ALIAS_BODY
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        # Our previous alias emit moved to ServerStorage by classifier.
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source=_EVENT_DISPATCH_ALIAS_BODY,  # carries marker
                script_type="ModuleScript",
                parent_path="ServerStorage",
            )
        )
        pl._inject_runtime_modules()
        # Exactly one alias entry — the rescued one.
        alias_entries = [
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        ]
        assert len(alias_entries) == 1
        # parent_path pulled back so the rbxlx writer routes to RS
        # (None ⇒ ModuleScript default of ReplicatedStorage).
        assert alias_entries[0].parent_path in (None, "ReplicatedStorage")

    def test_unmarked_modulescript_at_server_storage_is_left_alone(
        self, tmp_path: Path,
    ) -> None:
        """Round-2 [P2]: a user-authored ModuleScript named
        ``AutoFpsEventDispatch`` parked in ``ServerStorage`` (i.e.
        not at the alias path) is OUT of the overwrite policy.
        Path-scoping leaves it untouched, and a fresh alias is
        emitted at ReplicatedStorage.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source="-- user-authored, not converter-marker\n",
                script_type="ModuleScript",
                parent_path="ServerStorage",
            )
        )
        pl._inject_runtime_modules()
        alias_entries = [
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        ]
        # Two coexist: user at ServerStorage, alias at RS.
        assert len(alias_entries) == 2
        user_entry = next(
            s for s in alias_entries
            if s.parent_path == "ServerStorage"
        )
        alias_entry = next(
            s for s in alias_entries
            if s.parent_path in (None, "ReplicatedStorage")
        )
        # User's source preserved verbatim.
        assert "-- user-authored, not converter-marker" in user_entry.source
        # Alias body has the WaitForChild chain.
        assert ":WaitForChild(\"AutoGen\")" in alias_entry.source

    def test_alias_fresh_emit_has_source_path(
        self, tmp_path: Path,
    ) -> None:
        """Round-2 codex [P3]: fresh-emit alias must carry a
        ``source_path`` so the finalize-to-disk path writes
        ``scripts/AutoFpsEventDispatch.luau`` regardless of whether
        ``scripts/animations/`` exists. The name-based fallback in
        ``_subphase_finalize_scripts_to_disk`` skips no-source_path
        writes when an animations/ cache is present; without the
        explicit source_path the alias would never reach disk on
        projects with animation output, breaking preserve-scripts
        resume.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl._inject_runtime_modules()
        alias = next(
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        )
        assert alias.source_path == f"{_EVENT_DISPATCH_ALIAS_NAME}.luau"

    def test_lowercase_canonical_migrated_on_fps_run(
        self, tmp_path: Path,
    ) -> None:
        """Round-2 codex [P3]: an output produced by the round-1-buggy
        commit on a case-sensitive filesystem has a stale
        ``scripts/AutoGen/event_dispatch.luau`` (lowercase stem).
        Rehydrate surfaces it as ``name="event_dispatch"`` — outside
        the canonical name set, so the pre-pass leaves it. The
        canonical refresh path must sweep it out before emitting the
        CapCase stem; otherwise the upgrade leaves a dead duplicate
        forever.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        # Simulated rehydrate of a pre-round-1-fix lowercase canonical.
        legacy_marker_body = (
            "-- @@GAMEPLAY_RUNTIME_MODULE@@ converter-owned (EventDispatch)\n"
            "local EventDispatch = {}\n"
            "return EventDispatch\n"
        )
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name="event_dispatch",
                source=legacy_marker_body,
                script_type="ModuleScript",
                parent_path="ReplicatedStorage.AutoGen",
            )
        )
        pl._inject_runtime_modules()
        names = [s.name for s in pl.state.rbx_place.scripts]
        # Lowercase variant pruned.
        assert "event_dispatch" not in names
        # CapCase canonical present (the fresh emit).
        assert _EVENT_DISPATCH_CANONICAL_NAME in names

    def test_lowercase_user_script_not_swept_by_migration(
        self, tmp_path: Path,
    ) -> None:
        """The migration sweep MUST NOT touch a user-authored script
        that happens to be named ``event_dispatch`` but carries no
        ``@@GAMEPLAY_RUNTIME_MODULE@@`` marker and no legacy header.
        Marker-gated detection keeps user code intact.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name="event_dispatch",
                source="-- user's event_dispatch helper (snake_case)\nreturn true\n",
                script_type="ModuleScript",
            )
        )
        pl._inject_runtime_modules()
        names = [s.name for s in pl.state.rbx_place.scripts]
        assert "event_dispatch" in names

    def test_alias_refresh_keeps_none_parent_path_untouched(
        self, tmp_path: Path,
    ) -> None:
        """A rehydrated alias with ``parent_path=None`` (e.g. an entry
        pre-classification or a fresh emit) must NOT be touched by
        the refresh — the rbxlx writer's default routes None
        ModuleScripts to ReplicatedStorage, which is where the alias
        belongs.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source=_EVENT_DISPATCH_ALIAS_BODY,
                script_type="ModuleScript",
                parent_path=None,
            )
        )
        pl._inject_runtime_modules()
        alias = next(
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        )
        assert alias.parent_path is None


class TestAliasOverwritePolicy:
    """PR #75 alias overwrite policy:

      - non-ModuleScript at the alias name → log + skip emission
      - existing ModuleScript at the alias name → refresh in place
    """

    def test_non_module_at_alias_path_skips_emission(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If a user's Script/LocalScript already occupies
        ``AutoFpsEventDispatch``, the converter logs a warning and
        skips the alias emission — the user's content wins.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        # User's stale Script at the alias name (e.g. from a manual
        # hand-edit, or a leftover non-ModuleScript Instance from a
        # pre-PR-#75 era).
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source="-- user-authored Script at the alias name\n",
                script_type="Script",
            )
        )
        import logging
        with caplog.at_level(logging.WARNING):
            pl._inject_runtime_modules()
        # Original non-ModuleScript entry preserved exactly once —
        # no alias ModuleScript appended.
        alias_entries = [
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        ]
        assert len(alias_entries) == 1
        assert alias_entries[0].script_type == "Script"
        assert "-- user-authored Script at the alias name" in (
            alias_entries[0].source
        )
        # Warning surfaced so operators see the collision.
        assert any(
            "Skipping AutoFpsEventDispatch alias emission" in r.getMessage()
            for r in caplog.records
        )

    def test_existing_module_at_alias_path_is_refreshed(
        self, tmp_path: Path,
    ) -> None:
        """A pre-PR-#75 ``AutoFpsEventDispatch`` ModuleScript (which
        carried the full ``connectClient`` body) gets refreshed in
        place to the new alias body (a ``WaitForChild`` chain).
        Refresh — not append — keeps the on-disk path stable across
        resume runs.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        legacy_body = (
            "-- EventDispatch: cross-class connect helper for client event listeners.\n"
            "local EventDispatch = {}\n"
            "function EventDispatch.connectClient(evt, handler)\n"
            "  -- legacy body\n"
            "end\n"
            "return EventDispatch\n"
        )
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source=legacy_body,
                script_type="ModuleScript",
            )
        )
        pl._inject_runtime_modules()
        alias_entries = [
            s for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        ]
        # Single entry — refreshed in place.
        assert len(alias_entries) == 1
        # Body replaced with the WaitForChild chain.
        assert ':WaitForChild("AutoGen")' in alias_entries[0].source
        assert "-- legacy body" not in alias_entries[0].source

    def test_alias_idempotent_when_already_current(
        self, tmp_path: Path,
    ) -> None:
        """Running the inject when the alias body already matches the
        canonical alias body produces zero mutations — important so
        an idempotent re-emit doesn't churn the on-disk file.
        """
        pl = _make_pipeline_with_fps_opt_in(tmp_path, scaffolding=["fps"])
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source=_EVENT_DISPATCH_ALIAS_BODY,
                script_type="ModuleScript",
            )
        )
        before_source = pl.state.rbx_place.scripts[0].source
        pl._inject_runtime_modules()
        after_source = next(
            s.source for s in pl.state.rbx_place.scripts
            if s.name == _EVENT_DISPATCH_ALIAS_NAME
        )
        assert before_source == after_source


class TestFpsOptOutPrunesRehydratedScaffolding:
    """Codex finding [P2] (PR #70 round 2): the preserve-scripts
    rehydrate loads every ``.luau`` from ``scripts/`` (so user edits
    survive between assemble and upload). When a follow-up run
    drops ``--scaffolding=fps``, the rehydrated FPS auto-gen scripts
    would silently carry forward unless the inject subphase prunes
    them.

    The opt-out branch in ``_subphase_inject_autogen_scripts`` calls
    ``_remove_rehydrated_fps_autogen`` to drop FPS-only auto-gen
    (HUDController/AutoFpsHudController/FPSController carrying the
    canonical marker, plus the HUD ScreenGui). Other auto-gen scripts
    — GameServerManager, CollisionGroupSetup, etc. — stay (they're
    always needed; the user may have hand-edited them).

    PR #75 extends the prune set to include the canonical
    ``AutoGen.EventDispatch`` (matched via predicate so user-authored
    ``EventDispatch.cs`` transpilations survive).
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

    def test_opt_out_drops_rehydrated_fps_autogen(
        self, tmp_path: Path,
    ) -> None:
        from core.roblox_types import RbxScreenGui

        pl = self._make_pipeline(tmp_path)
        # Simulate rehydrated FPS auto-gen from a prior run.
        pl.state.rbx_place.scripts.extend([
            RbxScript(
                name="AutoFpsHudController",
                source="-- HUD Controller (auto-generated)\nlocal _ = 1\n",
                script_type="LocalScript",
            ),
            RbxScript(
                name="FPSController",
                source="-- FPS Client Controller (auto-generated)\nlocal _ = 1\n",
                script_type="LocalScript",
            ),
            # GameServerManager stays — not FPS-specific.
            RbxScript(
                name="GameServerManager",
                source=(
                    "-- Game Server Manager (auto-generated by Unity converter)\n"
                    "local _ = 1\n"
                ),
                script_type="Script",
            ),
        ])
        autogen_hud = RbxScreenGui(name="HUD", elements=[])
        autogen_hud.attributes["_AutoFpsHud"] = True
        pl.state.rbx_place.screen_guis.append(autogen_hud)

        # Run the inject subphase WITHOUT scaffolding=["fps"].
        pl._subphase_inject_autogen_scripts()

        names = {s.name for s in pl.state.rbx_place.scripts}
        gui_names = {sg.name for sg in pl.state.rbx_place.screen_guis}
        # FPS auto-gen pruned.
        assert "AutoFpsHudController" not in names
        assert "FPSController" not in names
        assert "HUD" not in gui_names
        # GameServerManager preserved (re-emitted by its own subphase
        # too — but at least one copy stays).
        assert "GameServerManager" in names

    def test_opt_out_drops_legacy_fpsclient(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P2] (PR #70 round 4): the migration's
        recognised-filename list includes the legacy ``FpsClient``
        name (alternate controller filename from a pre-PR era). The
        opt-out prune must match the same set, otherwise rebuilding
        an old output dir without ``--scaffolding=fps`` leaves the
        legacy controller in ``rbx_place`` even though the heuristic
        skips its auto-gen marker, so the place ships an unwired
        controller without the FPS camera flags.
        """
        pl = self._make_pipeline(tmp_path)
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name="FpsClient",
                source=(
                    "-- FPS Client Controller (auto-generated)\n"
                    "-- legacy alternate name from a pre-PR conversion\n"
                ),
                script_type="LocalScript",
            )
        )
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "FpsClient" not in names

    def test_opt_out_keeps_user_script_named_alias_outside_path(
        self, tmp_path: Path,
    ) -> None:
        """Round-2 codex [P2]: a user-authored ``Script`` named
        ``AutoFpsEventDispatch`` parked in ``ServerScriptService``
        (not at the alias path) must survive the opt-out prune. The
        marker-gated pruner rejects it (wrong script_type, wrong
        parent_path, no marker), preserving user code.
        """
        pl = self._make_pipeline(tmp_path)
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source="-- user-authored Script, no converter marker\n",
                script_type="Script",
                parent_path="ServerScriptService",
            )
        )
        pl._subphase_inject_autogen_scripts()
        names = [s.name for s in pl.state.rbx_place.scripts]
        assert _EVENT_DISPATCH_ALIAS_NAME in names

    def test_opt_out_keeps_user_module_named_alias_at_replicated(
        self, tmp_path: Path,
    ) -> None:
        """Round-2 codex [P2] companion: a user-authored ModuleScript
        named ``AutoFpsEventDispatch`` parked at ReplicatedStorage
        but lacking the converter alias marker must also survive the
        opt-out prune. The pruner gates on marker presence; user
        content without the marker is left alone even at the alias
        path.
        """
        pl = self._make_pipeline(tmp_path)
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source="-- user-authored ModuleScript, no marker\nreturn {}\n",
                script_type="ModuleScript",
                parent_path="ReplicatedStorage",
            )
        )
        pl._subphase_inject_autogen_scripts()
        names = [s.name for s in pl.state.rbx_place.scripts]
        assert _EVENT_DISPATCH_ALIAS_NAME in names

    def test_opt_out_drops_autogen_event_dispatch_alias(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P3] (PR #70 round 6) + PR #75 extension:
        the alias ``AutoFpsEventDispatch`` ModuleScript must be
        pruned on opt-out. It's only injected for FPS scaffolding,
        so a rehydrated copy on a non-FPS rerun is stale converter-
        owned compat code that would ship to the user's place.

        ``AutoFpsEventDispatch`` is named-only pruned (the ``AutoFps``
        prefix is converter-namespace-owned).
        """
        pl = self._make_pipeline(tmp_path)
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_ALIAS_NAME,
                source=_EVENT_DISPATCH_ALIAS_BODY,
                script_type="ModuleScript",
            )
        )
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert _EVENT_DISPATCH_ALIAS_NAME not in names

    def test_opt_out_drops_canonical_event_dispatch_under_autogen(
        self, tmp_path: Path,
    ) -> None:
        """PR #75: the canonical ``AutoGen.EventDispatch`` is FPS-
        scaffolding-only too. A rehydrated copy on a non-FPS rerun
        must be pruned — matched via the predicate (parent_path,
        marker, or legacy header) so user-authored ``EventDispatch.cs``
        transpilations survive.
        """
        pl = self._make_pipeline(tmp_path)
        canonical_body = (
            (GAMEPLAY_RUNTIME_DIR / "event_dispatch.luau").read_text(
                encoding="utf-8",
            )
        )
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name=_EVENT_DISPATCH_CANONICAL_NAME,
                source=canonical_body,
                script_type="ModuleScript",
                parent_path="ReplicatedStorage.AutoGen",
            )
        )
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert _EVENT_DISPATCH_CANONICAL_NAME not in names

    def test_opt_out_keeps_user_authored_event_dispatch_module(
        self, tmp_path: Path,
    ) -> None:
        """A user-authored ``EventDispatch`` (no marker, no AutoGen
        parent_path) must survive opt-out. The predicate rejects it
        (no marker, no canonical parent_path, no legacy header
        prefix) so the prune leaves it alone.
        """
        pl = self._make_pipeline(tmp_path)
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name="EventDispatch",
                source="-- User-authored EventDispatch module\n",
                script_type="ModuleScript",
            )
        )
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "EventDispatch" in names

    def test_opt_out_keeps_user_authored_hud_screengui(
        self, tmp_path: Path,
    ) -> None:
        """Codex finding [P1] (PR #70 round 3): a Canvas-converted HUD
        ScreenGui (user content) named ``HUD`` must NOT be deleted by
        the FPS opt-out cleanup. The auto-gen ScreenGui carries an
        ``_AutoFpsHud`` marker attribute; user-authored ones don't.
        """
        from core.roblox_types import RbxScreenGui

        pl = self._make_pipeline(tmp_path)
        # User-authored HUD (no marker attr).
        user_hud = RbxScreenGui(name="HUD", elements=[])
        pl.state.rbx_place.screen_guis.append(user_hud)
        # Run opt-out.
        pl._subphase_inject_autogen_scripts()
        gui_names = {sg.name for sg in pl.state.rbx_place.screen_guis}
        assert "HUD" in gui_names

    def test_opt_out_drops_autogen_hud_screengui(
        self, tmp_path: Path,
    ) -> None:
        """Companion: an auto-gen HUD ScreenGui (carrying the
        ``_AutoFpsHud`` marker) IS dropped on opt-out so the player
        doesn't inherit a stale shell with no controller wiring."""
        from core.roblox_types import RbxScreenGui

        pl = self._make_pipeline(tmp_path)
        autogen_hud = RbxScreenGui(name="HUD", elements=[])
        autogen_hud.attributes["_AutoFpsHud"] = True
        pl.state.rbx_place.screen_guis.append(autogen_hud)
        pl._subphase_inject_autogen_scripts()
        gui_names = {sg.name for sg in pl.state.rbx_place.screen_guis}
        assert "HUD" not in gui_names

    def test_opt_out_keeps_user_authored_hudcontroller(
        self, tmp_path: Path,
    ) -> None:
        """User-authored HUDController (no auto-gen marker) survives
        opt-out even when its name matches the legacy auto-gen file.
        The marker is the discriminator."""
        pl = self._make_pipeline(tmp_path)
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name="HUDController",
                source=(
                    "-- User-authored HUDController (custom HUD)\n"
                    "local _ = 1\n"
                ),
                script_type="LocalScript",
            )
        )
        pl._subphase_inject_autogen_scripts()
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "HUDController" in names

    def test_opt_in_does_not_prune(self, tmp_path: Path) -> None:
        """When ``--scaffolding=fps`` IS set, the prune path doesn't
        run — the inject path replaces/dedupes auto-gen scripts via
        the existing marker check."""
        pl = self._make_pipeline(tmp_path)
        pl._init_scaffolding = ("fps",)
        pl.ctx.scaffolding = ["fps"]
        pl.state.rbx_place.scripts.append(
            RbxScript(
                name="AutoFpsHudController",
                source="-- HUD Controller (auto-generated)\nlocal _ = 1\n",
                script_type="LocalScript",
            )
        )
        pl._subphase_inject_autogen_scripts()
        # Still present (the dedupe in inject_fps_scripts kept it
        # rather than appending a duplicate).
        names = {s.name for s in pl.state.rbx_place.scripts}
        assert "AutoFpsHudController" in names
