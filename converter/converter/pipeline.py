"""
pipeline.py -- Phase orchestration for the Unity -> Roblox conversion pipeline.

Coordinates parsing, asset extraction, material mapping, script transpilation,
scene conversion, and output generation in a deterministic, resumable sequence.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Any, cast

import config as _config
from config import (
    OUTPUT_DIR,
    RBXLX_OUTPUT_FILENAME,
)
from core.conversion_context import ConversionContext
from core.unity_types import (
    AssetManifest,
    GuidIndex,
    ParsedScene,
    PrefabLibrary,
)
from core.roblox_types import RbxPlace, RbxScript, ScriptType
from converter.animation_converter import AnimationConversionResult
from converter.code_transpiler import TranspilationResult
from converter.material_mapper import MaterialMapping
from converter.scriptable_object_converter import AssetConversionResult
from converter.sprite_extractor import SpriteExtractionResult
from unity.yaml_parser import ref_guid

log = logging.getLogger(__name__)

# Ordered list of pipeline phases.
PHASES: list[str] = [
    "parse",
    "extract_assets",
    # Plan the scene runtime artifact off parsed scenes + prefabs before
    # any script-touching phase runs. Inert by default in PR1 — only the
    # planner data lands in conversion_plan.json; the legacy transpile
    # path doesn't consume it. PR3a opts in when ``--scene-runtime=generic``
    # is requested.
    "plan_scene_runtime",
    "moderate_assets",
    "upload_assets",
    "resolve_assets",
    "convert_materials",
    "transpile_scripts",
    "convert_animations",
    "convert_scene",
    # Phase 2a slice 8: ``materialize_and_classify`` lifts script
    # materialization (emit-to-disk), the post-transpile coherence pass,
    # and storage classification out of ``write_output`` into a sibling
    # phase. The phase is empty in this slice's first commit; subsequent
    # commits lift the three subphases in order. After the lift,
    # ``write_output`` consumes the persisted ``StoragePlan`` and a
    # populated ``rbx_place.scripts`` instead of computing them itself.
    # Placement rationale: must run AFTER ``convert_scene`` because
    # ``rbx_place`` (the script container) is only created there.
    "materialize_and_classify",
    "write_output",
]


def _carry_unconverted(
    animation_result: Any, entries: list[dict[str, str]],
) -> None:
    """Append entries onto ``animation_result.unconverted`` so the existing
    PR 2b UNCONVERTED.md writer picks them up. Materials use
    MaterialMapping.warnings (a different channel); this helper exists
    because prefab-package drops don't own a dataclass of their own and
    writing a new aggregation channel just for them is overkill.
    """
    if animation_result is None or not entries:
        return
    carrier = getattr(animation_result, "unconverted", None)
    if carrier is None:
        return
    carrier.extend(entries)


def _scene_needs_collision_recook(parts: list) -> bool:
    """Walk the part tree and return True if any MeshPart has a
    non-Default ``collision_fidelity`` set.

    Used by ``_subphase_inject_autogen_scripts`` to decide whether to
    add the ``CollisionFidelityRecook`` script. Most projects will
    need it (door frames, archways, fences, prefab models all set
    Hull or PreciseConvexDecomposition); skipping the inject when no
    parts need it keeps the script out of all-cube/Block-fidelity
    scenes for a slightly smaller place file.
    """
    for p in parts:
        if p.collision_fidelity not in (None, 0) and p.mesh_id:
            return True
        if p.children and _scene_needs_collision_recook(p.children):
            return True
    return False


def _contract_failure_errors(fail_closed: "list") -> list[str]:
    """Render each scene-runtime ``FailClosed`` row as a conversion-error
    string. Pure: returns a fresh list, mutates nothing. The caller appends
    these to ``ctx.errors`` (deduped) so a fail-closed generic conversion
    reports ``success=False`` instead of shipping a place that throws at
    boot. See the Fix-#15 Root-A gating in ``transpile_scripts``."""
    return [
        f"scene-runtime contract failed closed ({fc.kind}): {fc.detail}"
        for fc in fail_closed
    ]


@dataclass
class PipelineState:
    """Intermediate state passed between pipeline phases."""

    guid_index: GuidIndex | None = None
    parsed_scene: ParsedScene | None = None
    # All scenes parsed by the multi-scene driver before
    # ``plan_scene_runtime`` runs — the planner needs every scene in one
    # call so the per-scene namespacing in the artifact is consistent. In
    # single-scene mode this stays empty and the planner reads from
    # ``parsed_scene`` instead.
    all_parsed_scenes: list[ParsedScene] = field(default_factory=list)
    asset_manifest: AssetManifest | None = None
    material_mappings: dict[str, MaterialMapping] = field(default_factory=dict)
    transpilation_result: TranspilationResult | None = None
    animation_result: AnimationConversionResult | None = None
    rbx_place: RbxPlace | None = None
    prefab_library: PrefabLibrary | None = None
    dependency_map: dict[str, list[str]] = field(default_factory=dict)
    scriptable_objects: AssetConversionResult | None = None
    sprite_result: SpriteExtractionResult | None = None
    # Output of converter/semantic_validators.run_semantic_validators
    # — surfaced via conversion_report.json under ``semantic_warnings``.
    semantic_report: object | None = None


class Pipeline:
    """Orchestrates the full Unity -> Roblox conversion pipeline.

    Usage::

        pipeline = Pipeline("path/to/unity/project", "path/to/output")
        pipeline.run_all()

    To resume from a specific phase after a failure::

        pipeline.resume("convert_materials")
    """

    def __init__(
        self,
        unity_project_path: str | Path,
        output_dir: str | Path | None = None,
        skip_upload: bool = False,
        skip_binary_rbxl: bool = False,
        scaffolding: frozenset[str] | None = None,
    ) -> None:
        self.unity_project_path = self._find_unity_root(Path(unity_project_path).resolve())
        self.output_dir = Path(output_dir or OUTPUT_DIR).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.skip_upload = skip_upload
        # True on the interactive `upload` rebuild (publishes via
        # execute_luau; never reads the .rbxl file).
        self.skip_binary_rbxl = skip_binary_rbxl

        self.ctx = ConversionContext(
            unity_project_path=str(self.unity_project_path),
        )
        self.state = PipelineState()

        self._context_path = self.output_dir / "conversion_context.json"

        # Opt-in genre scaffolding persisted on the context so resumed
        # builds (publish rebuild path, interactive upload re-runs,
        # assemble against an existing output dir) reproduce the same
        # place contents. Empty by default — the converter makes no
        # game-genre assumptions. Currently recognised:
        #   - ``"fps"`` → inject FPS client controller LocalScript,
        #     HUD ScreenGui, and HUDController LocalScript via
        #     ``scaffolding.fps.inject_fps_scripts``.
        # Pass via ``u2r.py convert --scaffolding=fps`` or merge in via
        # :meth:`apply_scaffolding` after rehydrating ctx from disk.
        #
        # ``_init_scaffolding`` keeps the caller's constructor request
        # alive across ctx swaps inside :meth:`resume` (which loads ctx
        # from disk and replaces ``self.ctx`` wholesale). Without this
        # snapshot, ``u2r.py convert --phase write_output --scaffolding=fps``
        # would silently revert to whatever was persisted in
        # ``conversion_context.json`` — making the new flag a no-op on
        # the most common resume entry point.
        self._init_scaffolding: tuple[str, ...] = tuple(
            sorted({str(s).strip().lower() for s in (scaffolding or ()) if str(s).strip()})
        )
        if self._init_scaffolding:
            # Route through apply_scaffolding so unknown-name validation
            # fires here too — otherwise a typo'd
            # ``Pipeline(scaffolding=["fsps"])`` would persist silently.
            self.apply_scaffolding(self._init_scaffolding)

        # ``_fps_artifacts_at_init`` caches the backward-compat
        # migration signal BEFORE ``_subphase_emit_scripts_to_disk``
        # wipes ``scripts/`` (with ``--retranspile``). Default False
        # at construction — only resume/rebuild paths re-snapshot
        # this with a properly-loaded ctx (so the rbxlx scan can
        # scope to ``ctx.selected_scene`` for multi-scene runs).
        # Fresh ``run_all()`` doesn't trigger migration anyway
        # (``_is_resume`` stays False), so the init-time default of
        # False is safe.
        self._fps_artifacts_at_init: bool = False

        # ``_is_resume`` flags an EXPLICIT resume/rebuild (set True
        # by :meth:`resume` and the publish-rebuild path in u2r.py
        # before running). The backward-compat FPS migration only
        # fires when this flag is True — not when ``run_all()`` is
        # invoked against an existing output dir, which is a
        # full-conversion rerun and should honour the new opt-in
        # default.
        #
        # Default False at construction. Setters: ``resume()``, and
        # external callers (``u2r.py publish`` rebuild fallback)
        # that explicitly mean "this is a rebuild from persisted
        # state, not a fresh conversion".
        self._is_resume: bool = False

    def _add_warning(self, warning: str) -> None:
        """Append *warning* to ``ctx.warnings`` once.

        Phases like ``extract_assets`` can re-run multiple times (resume,
        ``convert_interactive assemble`` retries, multi-scene loops).
        Without dedupe, every re-run appends the same warning text to
        the same list, so the final report shows N copies of one
        failure. Dedupe by exact-string match — the warnings list is a
        human-facing summary, not a structured event log.
        """
        if warning not in self.ctx.warnings:
            self.ctx.warnings.append(warning)

    @property
    def scaffolding(self) -> frozenset[str]:
        """Return the active genre-scaffolding set as a frozenset.

        Reads from ``self.ctx.scaffolding`` so resumed builds (which
        rehydrate ``self.ctx`` from disk) automatically pick up
        whatever scaffolding was requested at conversion time. Callers
        must NOT cache this — :class:`ConversionContext` reload may
        replace ``self.ctx`` mid-flight.
        """
        return frozenset(self.ctx.scaffolding or ())

    # Marker comments at the top of every auto-generated FPS script.
    # Match against file CONTENT (not just filename) because a user's
    # own Unity ``HUDController.cs`` / ``FpsClient.cs`` would transpile
    # to identically-named ``.luau`` files in this output dir, and the
    # backward-compat migration must not misclassify those as evidence
    # of a pre-PR FPS conversion.
    _FPS_AUTOGEN_MARKERS: tuple[str, ...] = (
        "-- HUD Controller (auto-generated)",
        "-- FPS Client Controller (auto-generated)",
    )

    def _fps_artifacts_on_disk(self) -> bool:
        """Return True if this output dir already contains FPS scripts
        emitted by a pre-scaffolding-flag conversion run.

        Used by the backward-compat migration in
        :meth:`_subphase_inject_autogen_scripts` to distinguish
        "resumed from a pre-PR FPS conversion" (where we should
        re-emit the FPS scripts) from "fresh post-PR conversion"
        (where the user must opt in explicitly).

        Checks file CONTENT for the auto-generated header comments
        rather than just file names, so a Unity project that ships
        its own ``HUDController.cs`` or ``FpsClient.cs`` (transpiled
        to identically-named .luau files in ``scripts/``) doesn't
        falsely trigger the migration on a fresh conversion.
        """
        # Two signals — the user keeps either to count as a true
        # pre-PR FPS output:
        #   1. ``scripts/<name>.luau`` carrying the auto-gen marker
        #      for any of the historic FPS-emitted script names —
        #      ONLY honoured for single-scene runs. Multi-scene runs
        #      (``run_all_scenes``) rewrite the same ``scripts/``
        #      cache for whichever scene converted last, so its
        #      contents aren't scoped to ``ctx.selected_scene``;
        #      using it would migrate non-FPS scenes too.
        #   2. The rbxlx output itself contains the auto-gen marker
        #      string. Survives cache pruning — users who archive or
        #      shrink an output dir tend to keep the rbxlx as the
        #      canonical artifact even when the scripts cache goes.
        # Either signal flips True; user-authored .cs/.luau files
        # transpiled into the scripts dir don't carry the marker.
        #
        # ``.rbxl`` is intentionally NOT a fallback target: our binary
        # writer LZ4-compresses script source inside PROP chunks, so
        # the marker comment is not reliably present as a UTF-8
        # substring. Users who keep only the binary file lose the
        # migration signal — documented as a known limitation; the
        # workaround is to pass ``--scaffolding=fps`` explicitly on
        # rebuild, which the publish CLI surfaces.
        #
        # Multi-scene detection: ``ctx.selected_scene`` alone is NOT a
        # reliable signal — it's set on every run including ordinary
        # single-scene conversions (``Pipeline.run_all`` populates it
        # at line 710). The discriminator is ``scenes_metadata``,
        # which is only populated by ``run_all_scenes``'s per-scene
        # loop and persists across resumes. Falling back to the disk
        # shape catches the rare case where ctx was wiped but per-
        # scene rbxlx files remain.
        is_multi_scene = bool(self.ctx.scenes_metadata) or (
            sum(
                1 for p in self.output_dir.glob("*.rbxlx")
                if p.name != "converted_place.rbxlx"
            )
            >= 1
        )
        scripts_dir = self.output_dir / "scripts"
        if scripts_dir.is_dir() and not is_multi_scene:
            # Recognised auto-gen filenames across pipeline eras:
            #   - ``HUDController.luau`` (pre-rename HUD listener)
            #   - ``AutoFpsHudController.luau`` (post-rename HUD listener)
            #   - ``FpsClient.luau`` (legacy controller stub name)
            #   - ``FPSController.luau`` (the actual generated
            #     controller name from ``generate_fps_client_script``)
            candidates = (
                "HUDController.luau",
                "AutoFpsHudController.luau",
                "FpsClient.luau",
                "FPSController.luau",
            )
            for name in candidates:
                path = scripts_dir / name
                if not path.exists():
                    continue
                try:
                    # Only read the first ~256 bytes — markers always live
                    # in the first comment line.
                    head = path.read_text(encoding="utf-8", errors="replace")[:256]
                except OSError:
                    continue
                if any(marker in head for marker in self._FPS_AUTOGEN_MARKERS):
                    return True

        # Fallback: scan the rbxlx for the marker. Scope matters for
        # multi-scene output dirs (``run_all_scenes`` writes per-scene
        # files like ``main.rbxlx`` and ``menu.rbxlx``) — a marker in
        # ``main.rbxlx`` shouldn't migrate the whole project to
        # ``scaffolding=['fps']`` if only the main scene was FPS-shaped
        # and the menu wasn't. Prefer the SELECTED-scene-specific
        # rbxlx when available, fall back to the canonical
        # single-scene name, and only glob ``*.rbxlx`` as a
        # last-resort safety net (a multi-scene rebuild with no
        # selected scene set).
        place_files: list[Path] = []
        if self.ctx.selected_scene:
            scene_stem = Path(self.ctx.selected_scene).stem
            scoped = self.output_dir / f"{scene_stem}.rbxlx"
            if scoped.exists():
                place_files.append(scoped)
        if not place_files:
            canonical = self.output_dir / "converted_place.rbxlx"
            if canonical.exists():
                place_files.append(canonical)
        if not place_files:
            # Last resort for unscoped multi-scene rebuilds; matches
            # the conservative pre-scoped behaviour but only when no
            # scene-specific signal is available.
            place_files.extend(self.output_dir.glob("*.rbxlx"))
        for place_file in place_files:
            if self._file_contains_any_marker(place_file):
                return True
        return False

    def _file_contains_any_marker(self, path: Path) -> bool:
        """Stream-search *path* for any FPS auto-gen marker.

        Reads in 64KB chunks so a multi-MB rbxlx doesn't load fully
        into memory just for a substring check. Reads with
        ``errors="replace"`` so the binary rbxl format (which embeds
        the same marker text in its compressed source blocks) doesn't
        trip a UnicodeDecodeError. Bridges the chunk boundary by
        keeping the last ``len(longest_marker) - 1`` bytes from the
        previous chunk.
        """
        markers = self._FPS_AUTOGEN_MARKERS
        if not markers:
            return False
        max_marker_len = max(len(m) for m in markers)
        try:
            with path.open("rb") as f:
                tail = b""
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        return False
                    blob = tail + chunk
                    text = blob.decode("utf-8", errors="replace")
                    for marker in markers:
                        if marker in text:
                            return True
                    # Keep the last (max_marker_len - 1) bytes so the
                    # next iteration sees markers that straddle the
                    # boundary.
                    tail = blob[-(max_marker_len - 1):] if max_marker_len > 1 else b""
        except OSError:
            return False

    # Scaffolding names the pipeline knows how to inject. Unknown
    # names are accepted (forward-compat with future genres) but
    # logged at WARN level so a typo like ``--scaffolding=fsps``
    # surfaces in the conversion logs instead of silently persisting
    # an inert no-op into ``conversion_context.json``.
    _KNOWN_SCAFFOLDING: frozenset[str] = frozenset({"fps"})

    def apply_scaffolding(self, scaffolding: Iterable[str] | None) -> None:
        """Merge *scaffolding* into ``self.ctx.scaffolding``.

        Idempotent and additive — call after rehydrating ``self.ctx``
        from disk (e.g. in ``_make_pipeline``) to honor a NEW caller
        request without dropping previously persisted entries.
        Empty/None inputs are no-ops, so resume paths that don't pass
        ``--scaffolding`` simply preserve the persisted set.

        Logs a warning for unknown scaffolding names — the value is
        still persisted (forward-compat for future genres), but the
        log helps users catch typos like ``--scaffolding=fsps``
        instead of silently writing an inert entry into
        ``conversion_context.json``.
        """
        if not scaffolding:
            return
        normalised = {
            str(s).strip().lower() for s in scaffolding if str(s).strip()
        }
        unknown = normalised - self._KNOWN_SCAFFOLDING
        if unknown:
            log.warning(
                "[scaffolding] Unknown scaffolding name(s) %s — "
                "persisting them anyway (forward-compat) but the "
                "pipeline currently only honours %s. Check for typos.",
                sorted(unknown),
                sorted(self._KNOWN_SCAFFOLDING),
            )
        merged = set(self.ctx.scaffolding or ()) | normalised
        self.ctx.scaffolding = sorted(merged)

    @staticmethod
    def _find_unity_root(path: Path) -> Path:
        """Find the actual Unity project root (directory containing Assets/).

        If the given path doesn't have an Assets/ subdirectory, search one
        level deep for a subdirectory that does.  This handles projects like
        ChopChop (``UOP1_Project/``) or PrefabWorkflows.
        """
        if (path / "Assets").is_dir():
            return path
        for child in path.iterdir():
            if child.is_dir() and (child / "Assets").is_dir():
                log.info("Auto-detected Unity project root: %s", child.name)
                return child
        return path  # fall back to original

    @property
    def context(self) -> ConversionContext:
        return self.ctx

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_all(self) -> ConversionContext:
        """Execute every phase in order and return the final context."""
        log.info("=== Starting full pipeline for %s ===", self.unity_project_path)
        start = time.monotonic()

        for phase in PHASES:
            self._run_phase(phase)

        elapsed = time.monotonic() - start
        log.info("=== Pipeline complete in %.1f s ===", elapsed)
        return self.ctx

    def run_all_scenes(self) -> ConversionContext:
        """Convert every scene in the project to separate .rbxlx files.

        Shared phases (parse GUID index, extract/upload assets, materials,
        scripts, animations) run once.  Scene-specific phases (parse scene,
        convert scene, write output) run per-scene.
        """
        log.info("=== Starting multi-scene pipeline for %s ===", self.unity_project_path)
        start = time.monotonic()

        # Phase 1: build GUID index (runs scene discovery too with a dummy)
        from unity.guid_resolver import build_guid_index
        self.state.guid_index = build_guid_index(self.unity_project_path)
        log.info("[multi] GUID index: %d entries", self.state.guid_index.total_resolved)

        # Discover all scene files
        scene_paths = sorted(
            (self.unity_project_path / "Assets").rglob("*.unity")
        )
        if not scene_paths:
            log.warning("[multi] No .unity scene files found")
            return self.ctx

        log.info("[multi] Found %d scenes to convert", len(scene_paths))

        # Parse EVERY scene up front. The plan_scene_runtime phase below
        # needs the full set in one call so the per-scene namespacing in
        # the artifact is consistent, and the per-scene loop further down
        # gets to reuse the parsed result instead of re-parsing. Pre-PR1
        # only the first scene was parsed before shared phases ran; that
        # made the planner blind to every other scene's MonoBehaviours.
        from unity.scene_parser import parse_scene
        all_parsed: list[ParsedScene] = []
        for scene_path in scene_paths:
            try:
                all_parsed.append(parse_scene(scene_path))
            except Exception as exc:
                log.warning("[multi] Skipping unparseable %s: %s",
                            scene_path.name, exc)
        if not all_parsed:
            log.warning("[multi] All scenes failed to parse")
            return self.ctx
        self.state.all_parsed_scenes = all_parsed
        # Pre-select the first parseable scene as the "current" parsed
        # scene for shared phases that consume a single scene
        # (extract_assets reads textures referenced by it).
        self.state.parsed_scene = all_parsed[0]
        self.ctx.selected_scene = str(all_parsed[0].scene_path)
        self.ctx.total_game_objects = len(self.state.parsed_scene.all_nodes)

        # Run shared phases. ``resolve_assets`` belongs here too: without
        # it, every per-scene ``convert_scene`` below would emit raw
        # uploaded-mesh Model IDs that Studio cannot fetch as MeshIds —
        # the multi-scene path used to silently skip resolution and
        # produced visibly broken places. ``resolve_assets`` no-ops when
        # ``--no-upload`` is set or no universe/place IDs are configured.
        # ``plan_scene_runtime`` sits between extract_assets (which lazy-
        # loads the prefab library) and transpile_scripts (PR3a will
        # consume the artifact) — same slot as the single-scene driver.
        for phase in ["extract_assets", "plan_scene_runtime",
                       "upload_assets", "convert_materials",
                       "transpile_scripts", "convert_animations",
                       "resolve_assets"]:
            self._run_phase(phase)

        # Per-scene: convert, write. Reuses scenes pre-parsed above —
        # parsing every scene twice was the pre-PR1 status quo and is the
        # one cost the planner's "all scenes up front" move eliminates.
        parsed_by_path: dict[str, ParsedScene] = {
            str(p.scene_path): p for p in all_parsed
        }
        for scene_path in scene_paths:
            scene_name = scene_path.stem
            log.info("[multi] === Converting scene: %s ===", scene_name)

            self.ctx.selected_scene = str(scene_path)
            parsed = parsed_by_path.get(str(scene_path))
            if parsed is None:
                # Lost a scene to a parse failure above; nothing to convert.
                log.warning("[multi] Skipping %s — pre-parse missing",
                            scene_name)
                continue
            self.state.parsed_scene = parsed
            self.ctx.total_game_objects = len(self.state.parsed_scene.all_nodes)

            # Convert scene
            self._run_phase("convert_scene")

            # Phase 2a slice 8 (round 2): the single-scene path runs
            # ``materialize_and_classify`` between ``convert_scene`` and
            # ``write_output`` via ``run_through`` honoring the PHASES
            # ordering. The multi-scene loop drove ``_run_phase`` directly
            # and silently skipped the lifted phase per scene — every
            # per-scene rbxlx lost the script-set materialization +
            # classification pass + the late-append safety net stamp.
            # ``materialize_and_classify`` is in ``ESSENTIAL_PHASES`` so
            # it re-runs cleanly per scene (no completed-phase short-
            # circuit on the second iteration).
            self._run_phase("materialize_and_classify")

            # Write output with scene-specific filename
            original_filename = RBXLX_OUTPUT_FILENAME
            try:
                import config as _cfg
                _cfg.RBXLX_OUTPUT_FILENAME = f"{scene_name}.rbxlx"
                self._run_phase("write_output")
            finally:
                _cfg.RBXLX_OUTPUT_FILENAME = original_filename

            self.ctx.scenes_metadata[scene_name] = {
                "parts": self.ctx.converted_parts,
                "scripts": self.ctx.transpiled_scripts,
                "game_objects": self.ctx.total_game_objects,
            }

        elapsed = time.monotonic() - start
        log.info("=== Multi-scene pipeline complete in %.1f s (%d scenes) ===",
                 elapsed, len(scene_paths))
        return self.ctx

    # Phases whose primary outputs live in self.state (in-memory) rather than
    # ConversionContext (on disk), so they MUST re-run on every resumed
    # invocation even if ctx.completed_phases marks them done.
    ESSENTIAL_PHASES: frozenset[str] = frozenset({
        "parse", "extract_assets", "convert_materials",
        "transpile_scripts", "convert_animations", "convert_scene",
        # Phase 2a slice 8: ``materialize_and_classify`` populates
        # ``state.rbx_place.scripts`` (in-memory) which write_output
        # consumes — it must re-run on every resumed invocation so a
        # ``--phase=write_output`` resume gets a populated script list.
        # The lifted emit subphase's preserve_scripts/rehydrate path
        # handles the "transpile was skipped" branch on resume.
        "materialize_and_classify",
    })

    def run_through(
        self,
        target_phase: str,
        *,
        skip: set[str] | frozenset[str] | None = None,
        force_rerun: set[str] | frozenset[str] | None = None,
        run_after: bool = False,
    ) -> None:
        """Run prerequisites for ``target_phase``, then the target itself.

        Prerequisites (phases earlier than ``target_phase``) run if they are
        in :attr:`ESSENTIAL_PHASES`, in ``force_rerun``, or not yet
        completed per ``ctx.completed_phases``. ``skip`` overrides all of
        the above — listed phases never run.

        ``target_phase`` itself always runs unless it is in ``skip``.

        ``force_rerun`` exists for retry semantics: ``assemble`` re-runs the
        cloud side-effect phases (``moderate_assets``, ``upload_assets``,
        ``resolve_assets``) on every invocation so a second ``assemble``
        call after fixing credentials or changing assets actually re-uploads
        rather than silently skipping the cloud work.

        If ``run_after`` is True, every phase after ``target_phase`` is also
        run unconditionally (modulo ``skip``) — this matches
        :meth:`resume`'s "redo this phase and everything after" contract.
        """
        if target_phase not in PHASES:
            raise ValueError(
                f"Unknown phase '{target_phase}'. Valid phases: {PHASES}"
            )
        skip = set(skip or ())
        force_rerun = set(force_rerun or ())
        target_idx = PHASES.index(target_phase)

        for prior in PHASES[:target_idx]:
            if prior in skip:
                continue
            if (
                prior in self.ESSENTIAL_PHASES
                or prior in force_rerun
                or prior not in self.ctx.completed_phases
            ):
                self._run_phase(prior)

        if target_phase not in skip:
            self._run_phase(target_phase)

        if run_after:
            # Resume contract: every later phase runs unconditionally so the
            # user gets a clean re-execution from the target forward.
            for remaining in PHASES[target_idx + 1:]:
                if remaining in skip:
                    continue
                self._run_phase(remaining)

    def resume(self, phase: str) -> ConversionContext:
        """Resume the pipeline from *phase*, re-running it and all subsequent phases.

        Earlier phases must have already completed (their results are loaded
        from the persisted context).

        Raises:
            ValueError: If *phase* is not a known phase name.
        """
        if phase not in PHASES:
            raise ValueError(
                f"Unknown phase '{phase}'. Valid phases: {PHASES}"
            )

        if self._context_path.exists():
            loaded = ConversionContext.load(self._context_path)
            log.info("Loaded persisted context from %s", self._context_path)
            # Validate the persisted ctx matches THIS Pipeline's
            # project before treating the load as an authoritative
            # resume. ``u2r.py convert <new-project> -o <old-output>
            # --phase write_output`` would otherwise silently apply
            # FPS migration / persisted scaffolding from the old
            # project. Mismatch → load the ctx for state but flag
            # the resume as cross-project so the FPS migration
            # suppresses itself.
            same_project = bool(loaded.unity_project_path) and (
                Path(loaded.unity_project_path).resolve()
                == self.unity_project_path
            )
            self.ctx = loaded
            self._is_resume = same_project
            # Re-take the FPS artifact snapshot now that ctx carries
            # ``selected_scene`` — the snapshot logic scopes the
            # rbxlx scan to the selected-scene-specific output file
            # for multi-scene runs. The init-time snapshot (taken
            # with a fresh empty ctx) couldn't see that scope.
            self._fps_artifacts_at_init = self._fps_artifacts_on_disk()
            if not same_project:
                # Drop the prior project's persisted scaffolding too:
                # a cross-project resume that inherits ``["fps"]`` from
                # ProjectA's ctx would inject FPS scaffolding into
                # ProjectB even though the mismatch warning explicitly
                # warned about cross-project leakage. Clearing the
                # field is the simplest safe behaviour — the caller
                # can re-pass ``--scaffolding=fps`` via the
                # constructor's ``scaffolding`` arg if they actually
                # want it for the new project (and that re-application
                # happens below).
                if self.ctx.scaffolding:
                    log.warning(
                        "[resume] Clearing persisted scaffolding %r "
                        "from cross-project ctx (was for %r, this "
                        "Pipeline targets %r).",
                        list(self.ctx.scaffolding),
                        loaded.unity_project_path,
                        str(self.unity_project_path),
                    )
                    self.ctx.scaffolding = []
                log.warning(
                    "[resume] Persisted ctx targets %r but this "
                    "Pipeline is configured for %r. Loading state "
                    "for resume but suppressing same-project "
                    "migrations to avoid cross-project leakage.",
                    loaded.unity_project_path,
                    str(self.unity_project_path),
                )
            # Re-apply the constructor's scaffolding request after the
            # ctx swap so ``u2r.py convert --phase write_output
            # --scaffolding=fps`` actually injects FPS scaffolding even
            # when the persisted ctx didn't have it. Additive merge —
            # persisted entries are kept, the new request adds to them.
            if self._init_scaffolding:
                self.apply_scaffolding(self._init_scaffolding)

        log.info("=== Resuming pipeline from phase '%s' ===", phase)
        self.run_through(phase, run_after=True)
        log.info("=== Resume complete ===")
        return self.ctx

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    def parse(self) -> None:
        """Phase 1: Parse the Unity project -- GUID resolution and scene parsing."""
        log.info("[parse] Resolving GUIDs ...")
        from unity.guid_resolver import build_guid_index  # type: ignore[import-untyped]

        self.state.guid_index = build_guid_index(self.unity_project_path)
        log.info(
            "[parse] GUID index: %d entries",
            self.state.guid_index.total_resolved,
        )

        # Discover scene files and parse the first (or selected) one.
        scene_paths = sorted(
            (self.unity_project_path / "Assets").rglob("*.unity")
        )
        self.ctx.scene_paths = [str(p) for p in scene_paths]

        if not scene_paths:
            log.warning("[parse] No .unity scene files found")
            return

        if self.ctx.selected_scene:
            selected = Path(self.ctx.selected_scene)
            # Resolve relative scene paths against the Unity project root
            if not selected.is_absolute() and not selected.exists():
                project_relative = self.unity_project_path / selected
                if project_relative.exists():
                    selected = project_relative
        else:
            # Smart scene selection: prefer text YAML, prefer "main" or "level" names
            from unity.yaml_parser import is_text_yaml as _is_text
            text_scenes = [s for s in scene_paths if _is_text(s)]
            candidates = text_scenes if text_scenes else scene_paths

            # Score scenes by name relevance — prefer gameplay scenes
            def _scene_score(p: Path) -> int:
                name = p.stem.lower()
                score = 0
                # Strong positive signals
                if "main" in name and "menu" not in name: score += 10
                if "level" in name and ("1" in name or "01" in name): score += 12
                if "level" in name: score += 8
                # Moderate positive signals
                if "dungeon" in name: score += 6
                if "boss" in name: score += 4
                if "arena" in name or "battle" in name: score += 4
                if "island" in name or "world" in name: score += 3
                if "demo" in name: score += 2
                if "static" in name: score += 1
                # Negative signals (non-gameplay scenes)
                if "game" in name and "post" not in name and "menu" not in name: score += 5
                if "post" in name: score -= 2
                if "menu" in name: score -= 5
                if "select" in name or "char" in name: score -= 3
                if "startup" in name or "loading" in name: score -= 4
                if "test" in name and "level" not in name: score -= 5
                if "debug" in name: score -= 5
                if "benchmark" in name: score -= 3
                if "loader" in name: score -= 3
                if "prefab" in name: score -= 8
                if "transition" in name: score -= 3
                return score

            selected = max(candidates, key=_scene_score) if candidates else scene_paths[0]
        self.ctx.selected_scene = str(selected)

        if len(scene_paths) > 1:
            log.warning(
                "[parse] Found %d scenes — converting '%s' only. Use --scene to select a different one.",
                len(scene_paths), selected.name,
            )

        log.info("[parse] Parsing scene: %s", selected.name)
        from unity.scene_parser import parse_scene  # type: ignore[import-untyped]

        self.state.parsed_scene = parse_scene(selected)
        self.ctx.total_game_objects = len(self.state.parsed_scene.all_nodes)
        log.info(
            "[parse] Scene parsed: %d GameObjects, %d roots",
            len(self.state.parsed_scene.all_nodes),
            len(self.state.parsed_scene.roots),
        )

    def extract_assets(self) -> None:
        """Phase 2: Discover and catalog all project assets."""
        log.info("[extract_assets] Building asset manifest ...")
        from unity.asset_extractor import extract_assets  # type: ignore[import-untyped]

        self.state.asset_manifest = extract_assets(
            self.unity_project_path,
            guid_index=self.state.guid_index,
        )
        log.info(
            "[extract_assets] %d assets found (%.1f MB)",
            len(self.state.asset_manifest.assets),
            self.state.asset_manifest.total_size_bytes / (1024 * 1024),
        )

        # ScriptableObject .asset -> Luau ModuleScripts, held in state.
        # The disk write happens in write_output after scripts_dir is
        # (possibly) wiped, so the disk layout matches the rbxlx.
        try:
            from converter.scriptable_object_converter import convert_asset_files
            so_result = convert_asset_files(self.unity_project_path)
            if so_result.converted:
                self.state.scriptable_objects = so_result
                log.info(
                    "[extract_assets] Converted %d ScriptableObject .asset files",
                    so_result.converted,
                )
        except Exception as exc:
            # Keep a broad except so a third-party parser bug doesn't torch
            # the whole pipeline — but emit at WARNING level (not debug) and
            # record to ctx.warnings so users see that some .asset files
            # didn't become ModuleScripts. Default log level hides debug.
            msg = f"ScriptableObject conversion failed: {exc}"
            log.warning("[extract_assets] %s", msg)
            self._add_warning(f"[extract_assets] {msg}")

        # Slice spritesheet textures into <output>/sprites/; expose the
        # GUID -> file map on ctx for SpriteRenderer consumers.
        if self.state.guid_index:
            try:
                from converter.sprite_extractor import extract_sprites
                sprite_result = extract_sprites(self.state.guid_index, self.output_dir)
                if sprite_result.total_sprites_extracted:
                    self.state.sprite_result = sprite_result
                    self.ctx.sprite_guid_to_file = {
                        k: str(v) for k, v in sprite_result.sprite_guid_to_file.items()
                    }
                    log.info(
                        "[extract_assets] Extracted %d sprites from %d spritesheets",
                        sprite_result.total_sprites_extracted,
                        sprite_result.total_spritesheets,
                    )
                for w in sprite_result.warnings:
                    log.warning("[extract_assets] Sprite: %s", w)
            except Exception as exc:
                # Same rationale as above: broad except to isolate third-party
                # failures, but visible WARNING and persisted to ctx so the
                # missing sprites surface in the final report.
                msg = f"Sprite extraction failed: {exc}"
                log.warning("[extract_assets] %s", msg)
                self._add_warning(f"[extract_assets] {msg}")

        # Pre-compute FBX bounding boxes via trimesh for InitialSize fallback.
        # This runs only when mesh_native_sizes (from Studio resolution) are
        # not yet available, so the convert_scene phase has real geometry data
        # instead of assuming every mesh is a 1-unit cube.
        if not self.ctx.mesh_native_sizes:
            self._compute_fbx_bounding_boxes()

        # Phase 4.9: serialized-field refs off MonoBehaviour components.
        # Feeds the transpiler (so AI knows which fields point at prefabs)
        # and 4.10 prefab packages. Persisted into conversion_context.json.
        self._extract_serialized_field_refs()

    def _extract_serialized_field_refs(self) -> None:
        """Phase 4.9 — gather prefab + audio references off MonoBehaviours.

        The prefab library is normally lazy-loaded in ``convert_materials``,
        but that runs AFTER transpile_scripts — by which point this phase
        needs to have surfaced its refs. Trigger prefab parsing here so
        the walk sees every MonoBehaviour, not just the scene's.
        """
        from converter.serialized_field_extractor import (
            extract_serialized_field_refs, serialize_for_context,
        )

        if self.state.prefab_library is None:
            try:
                from unity.prefab_parser import parse_prefabs
                self.state.prefab_library = parse_prefabs(self.unity_project_path)
            except Exception as exc:
                log.warning(
                    "[extract_assets] Could not parse prefabs for "
                    "serialized-field extraction: %s", exc,
                )

        scenes = [self.state.parsed_scene] if self.state.parsed_scene else []
        refs = extract_serialized_field_refs(
            parsed_scenes=scenes,
            prefab_library=self.state.prefab_library,
            guid_index=self.state.guid_index,
        )
        if not refs:
            return
        self.ctx.serialized_field_refs = serialize_for_context(
            refs, project_root=self.unity_project_path,
        )
        total = sum(len(v) for v in refs.values())
        log.info(
            "[extract_assets] Serialized field refs: %d scripts, %d fields",
            len(refs), total,
        )

    def plan_scene_runtime(self) -> None:
        """Phase: build the project-level ``scene_runtime`` artifact.

        Reads parsed scenes + the prefab library (lazy-loaded earlier by
        ``extract_assets``) and emits the deterministic snapshot the host
        runtime will consume in PR4. The artifact lands on
        ``self.ctx.scene_runtime`` and round-trips through
        ``conversion_context.json`` plus the persistence merge inside
        ``_classify_storage`` so resumes reproduce it verbatim.

        Inert by default in PR1 — only the planner data is written; no
        legacy phase consumes it. PR3a opts in under
        ``--scene-runtime=generic``.
        """
        from converter.scene_runtime_planner import plan_scene_runtime as _plan

        # ``self.state.parsed_scene`` is the single-scene path; the
        # multi-scene driver pre-parses every scene into
        # ``self.state.all_parsed_scenes`` before invoking this phase so a
        # single planner call sees them all (per-scene namespacing is
        # already a planner invariant).
        scenes_attr = getattr(self.state, "all_parsed_scenes", None)
        scenes: list[ParsedScene]
        if scenes_attr:
            scenes = list(scenes_attr)
        elif self.state.parsed_scene is not None:
            scenes = [self.state.parsed_scene]
        else:
            scenes = []

        if self.state.prefab_library is None:
            # Multi-scene's pre-extract entry skipped the lazy load; force
            # it here so prefab subplans are populated. Same fallback the
            # serialized-field walk takes in ``_extract_serialized_field_refs``.
            try:
                from unity.prefab_parser import parse_prefabs
                self.state.prefab_library = parse_prefabs(self.unity_project_path)
            except Exception as exc:
                log.warning(
                    "[plan_scene_runtime] Could not parse prefabs: %s", exc,
                )

        artifact = _plan(
            parsed_scenes=scenes,
            prefab_library=self.state.prefab_library,
            guid_index=self.state.guid_index,
            unity_project_root=self.unity_project_path,
        )
        # Persist directly on the context; the structural type belongs to
        # the planner module (avoids a core→converter dependency).
        self.ctx.scene_runtime = dict(artifact)

        modules = artifact["modules"]
        runtime_bearing = sum(
            1 for m in modules.values() if m.get("runtime_bearing")
        )
        log.info(
            "[plan_scene_runtime] %d modules (%d runtime-bearing), "
            "%d scene(s), %d prefab(s)",
            len(modules), runtime_bearing,
            len(artifact["scenes"]), len(artifact["prefabs"]),
        )

        # Strict-classification early gate (PR135 P1.2). The codex review
        # found that --strict-classification was firing in
        # ``_classify_storage`` -- which runs INSIDE ``write_output`` AFTER
        # ``transpile_scripts`` has already emitted Luau to disk. The
        # design doc requires strict mode to block BEFORE transpile so
        # the operator never pays the (expensive) AI transpile cost on a
        # plan that won't ship.
        #
        # The classifier verdicts that matter for strict mode (excluded
        # / low_confidence from Rule 1/4/7) come from C# signals + per-
        # instance evidence -- both already available at this point.
        # The Luau channel (post-transpile) hasn't run yet, but it can
        # only ADD signals; nothing it could add would promote a module
        # OUT of strict-violations. Helpers and reachability run in
        # ``_classify_storage`` later -- not gates here.
        #
        # We dry-run the classifier on a deep copy of the artifact so
        # the real run in ``_classify_storage`` (with Luau sources +
        # dependency_map) is the source of truth.
        if (
            self.ctx.scene_runtime_mode != "legacy"
            and bool(getattr(self.ctx, "strict_classification", False))
        ):
            self._enforce_strict_classification_early(artifact)

    def _enforce_strict_classification_early(
        self, artifact: "Mapping[str, object]",
    ) -> None:
        """Run a dry-run domain classification and raise on strict
        violations. Defense-in-depth duplicate in ``_classify_storage``
        is left in place; this is the primary gate.
        """
        import copy as _copy
        from converter.scene_runtime_domain import (
            classify_scene_runtime_domains,
        )

        # Deep copy so the real run in _classify_storage stays
        # authoritative. The classifier mutates ``modules`` in place.
        dry_artifact = _copy.deepcopy(artifact)
        networking = getattr(self.ctx, "networking_mode", "none")
        report = classify_scene_runtime_domains(
            cast("dict", dry_artifact),
            scripts=[],  # no Luau channel yet (pre-transpile)
            dependency_map=None,  # reachability runs post-transpile
            guid_index=self.state.guid_index,
            networking=networking,
            strict=True,
        )
        if report["strict_violations"]:
            violations = "\n  - ".join(report["strict_violations"])
            raise RuntimeError(
                "--strict-classification: domain classifier left "
                f"{len(report['strict_violations'])} runtime-bearing "
                "module(s) unresolved (checked BEFORE transpile). "
                "Add scene_runtime.domain_overrides entries (or split "
                "the source class) before re-running:\n  - "
                + violations
            )

    def _compute_fbx_bounding_boxes(self) -> None:
        """Scan all mesh assets and compute bounding boxes.

        Uses direct FBX binary parsing for .fbx files (since trimesh cannot
        load FBX), and trimesh for other formats (.obj, .glb).

        Skips FBX files whose import configuration has a non-trivial unit ratio
        (USF ≠ OriginalUSF with useFileScale=1), as the sizing math for those
        files produces incorrect results from raw vertex bounds.
        """
        manifest = self.state.asset_manifest
        if not manifest:
            return

        from converter.mesh_processor import get_mesh_info, read_fbx_vertex_bounds

        mesh_assets = [a for a in manifest.assets if a.kind == "mesh"]
        if not mesh_assets:
            return

        computed = 0
        for asset in mesh_assets:
            rel_key = str(asset.relative_path)
            if rel_key in self.ctx.fbx_bounding_boxes:
                computed += 1
                continue

            bbox = None

            if asset.path.suffix.lower() == ".fbx":
                # Skip FBX files with non-trivial unit ratio — their vertex
                # coordinates are in unexpected units that produce wrong sizes.
                from converter.scene_converter import _get_fbx_unit_ratio
                guid = None
                if self.state.guid_index:
                    guid = self.state.guid_index.guid_for_path(asset.path)
                if guid:
                    ratio = _get_fbx_unit_ratio(guid, self.state.guid_index)
                    if abs(ratio - 1.0) > 0.01:
                        continue  # skip this mesh

                fbx_info = read_fbx_vertex_bounds(asset.path)
                if fbx_info:
                    bbox = fbx_info["bounding_box"]
            else:
                info = get_mesh_info(asset.path)
                raw = info.get("bounding_box")
                if raw and isinstance(raw, tuple) and len(raw) == 3:
                    if not (raw[0] == 1.0 and raw[1] == 1.0 and raw[2] == 1.0
                            and info.get("face_count", 0) == 0):
                        bbox = raw

            if bbox:
                self.ctx.fbx_bounding_boxes[rel_key] = list(bbox)
                computed += 1

        if computed:
            log.info("[extract_assets] Computed FBX bounding boxes for %d meshes", computed)

    def moderate_assets(self) -> None:
        """Phase 2.5: Screen assets for safety violations before upload.

        Checks filenames, script content, and audio names against Roblox's
        Community Standards to prevent account moderation. Violations are
        auto-added to the upload blocklist; warnings are logged.
        """
        if self.skip_upload:
            log.info("[moderate_assets] Skipping (--no-upload)")
            return

        manifest = self.state.asset_manifest
        if not manifest:
            return

        from converter.asset_moderator import moderate_assets, write_report

        project_name = self.unity_project_path.name
        scripts_dir = self.unity_project_path / "Assets"
        report = moderate_assets(manifest, project_name, scripts_dir)

        # Write report
        report_path = write_report(report, self.output_dir)

        # Log summary
        log.info(
            "[moderate_assets] Screened %d assets: %d OK, %d warnings, %d violations",
            report.checked, report.ok, report.warnings, report.violations,
        )

        if report.violations > 0 or report.warnings > 0:
            for f in report.findings:
                if f.classification == "VIOLATION":
                    log.warning(
                        "[moderate_assets] VIOLATION: %s — %s [%s]",
                        f.relative_path, f.evidence, ", ".join(f.standards),
                    )
                elif f.classification == "WARNING":
                    log.warning(
                        "[moderate_assets] WARNING: %s — %s [%s]",
                        f.relative_path, f.evidence, ", ".join(f.standards),
                    )

        # Auto-blocklist violations
        if report.violations > 0:
            blocklist_file = self.output_dir / ".upload_blocklist"
            existing = set()
            if blocklist_file.exists():
                existing = set(blocklist_file.read_text().splitlines())
            new_blocks = [
                f.relative_path for f in report.findings
                if f.classification == "VIOLATION" and f.relative_path not in existing
            ]
            if new_blocks:
                with open(blocklist_file, "a") as fh:
                    for b in new_blocks:
                        fh.write(b + "\n")
                log.info(
                    "[moderate_assets] Added %d violation(s) to upload blocklist",
                    len(new_blocks),
                )

        log.info("[moderate_assets] Report: %s", report_path)

    def upload_assets(self) -> None:
        """Phase 3: Upload all assets (textures, meshes, audio) to Roblox."""
        if self.skip_upload:
            log.info("[upload_assets] Skipping (--no-upload)")
            return

        import config
        api_key = config.ROBLOX_API_KEY
        creator_id = str(config.ROBLOX_CREATOR_ID or "")
        creator_type = config.ROBLOX_CREATOR_TYPE

        if not api_key:
            log.warning("[upload_assets] No API key configured -- skipping uploads")
            return
        if not creator_id:
            log.warning("[upload_assets] No creator ID configured -- skipping uploads")
            return

        from roblox.cloud_api import upload_image, upload_mesh, upload_audio
        from utils.image_processing import convert_to_png
        import time

        manifest = self.state.asset_manifest
        if not manifest:
            return

        uploaded = self.ctx.uploaded_assets
        convert_dir = self.output_dir / "converted_textures"
        convert_dir.mkdir(parents=True, exist_ok=True)

        # Compute which texture source paths belong to materials that
        # render with transparency (cutout, fade, transparent). Only those
        # textures get their alpha channel preserved; everything else is
        # stripped to RGB to prevent spurious transparency from mask
        # channels (roughness/metalness/specular packed into alpha).
        #
        # upload_assets runs BEFORE convert_materials, so we can't use the
        # full material_mappings. Instead, scan every .mat file in the
        # project and flag textures referenced by materials whose shader
        # fileID is a legacy cutout/transparent variant, or whose _Mode
        # is Cutout/Fade/Transparent.
        alpha_texture_paths: set[str] = set()
        if self.state.guid_index:
            import re as _re
            from converter.material_mapper import (
                _BUILTIN_CUTOUT_SHADER_IDS,
                _BUILTIN_TRANSPARENT_SHADER_IDS,
            )
            for mat_file in self.unity_project_path.rglob("*.mat"):
                try:
                    text = mat_file.read_text(errors="replace")
                except OSError:
                    continue
                # Shader fileID check
                sm = _re.search(r"m_Shader:\s*\{fileID:\s*(\d+)", text)
                shader_id = int(sm.group(1)) if sm else 0
                is_transparent = (
                    shader_id in _BUILTIN_CUTOUT_SHADER_IDS
                    or shader_id in _BUILTIN_TRANSPARENT_SHADER_IDS
                )
                # _Mode check for Standard/URP/HDRP
                if not is_transparent:
                    mm = _re.search(r"-\s*_Mode:\s*(\d+)", text)
                    if mm and int(mm.group(1)) > 0:
                        is_transparent = True
                if not is_transparent:
                    continue
                # Record every color-map texture referenced by this material
                for tex_key in ("_MainTex", "_BaseMap", "_BaseColorMap"):
                    tm = _re.search(
                        rf"- {tex_key}:\s*\n\s+m_Texture:\s*\{{fileID:\s*\d+,\s*guid:\s*([0-9a-f]+)",
                        text,
                    )
                    if tm:
                        tex_path = self.state.guid_index.resolve(tm.group(1))
                        if tex_path:
                            alpha_texture_paths.add(str(tex_path.resolve()))

        # Collected for a post-upload moderation audit. We probe only newly
        # uploaded assets (not cached entries from a previous run) so the
        # audit cost stays proportional to the new work.
        new_uploads: list[tuple[str, str]] = []

        for kind, uploader, extensions in [
            ("texture", upload_image, {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".psd"}),
            ("mesh", upload_mesh, {".fbx", ".obj"}),
            ("audio", upload_audio, {".mp3", ".ogg", ".wav", ".flac"}),
        ]:
            assets = manifest.by_kind.get(kind, [])
            eligible = [a for a in assets if a.path.suffix.lower() in extensions]
            log.info("[upload_assets] Uploading %d %s assets...", len(eligible), kind)

            # Asset upload blocklist: relative paths that should NEVER be
            # re-uploaded (e.g. user flagged a bad asset, or Roblox returned
            # a problematic asset ID). Read from
            # ``<output_dir>/.upload_blocklist`` — one relative path per line.
            blocklist_file = self.output_dir / ".upload_blocklist"
            blocklist: set[str] = set()
            if blocklist_file.exists():
                blocklist = {line.strip() for line in blocklist_file.read_text().splitlines() if line.strip() and not line.startswith("#")}

            for asset in eligible:
                rel = str(asset.relative_path)
                if rel in uploaded:
                    continue  # Already uploaded (resume support)
                if rel in blocklist:
                    log.info("[upload_assets] Skipping blocklisted asset: %s", rel)
                    continue

                upload_path = asset.path
                name = asset.path.stem

                # Fix mesh handedness: Unity (left-handed) vs Roblox
                # (right-handed). Negates X and Y in FBX vertices,
                # equivalent to 180° rotation around Z (vertical).
                # Preserves triangle winding (no backface culling)
                # and keeps text right-side up.
                if kind == "mesh" and asset.path.suffix.lower() == ".fbx":
                    from converter.fbx_binary import mirror_fbx_handedness
                    mirror_dir = self.output_dir / "mirrored_meshes"
                    mirror_dir.mkdir(parents=True, exist_ok=True)
                    mirrored_path = mirror_dir / asset.path.name
                    if mirror_fbx_handedness(asset.path, mirrored_path):
                        upload_path = mirrored_path

                # Determine whether this texture needs its alpha channel
                # preserved. Alpha is only kept for textures that feed
                # into materials with a transparent/cutout alpha_mode —
                # everything else strips alpha to avoid spurious
                # transparency from mask channels (roughness/metalness/
                # specular packed into alpha).
                needs_alpha = False
                if kind == "texture":
                    needs_alpha = str(asset.path.resolve()) in alpha_texture_paths

                # Auto-convert non-PNG/JPG formats to PNG before uploading
                if kind == "texture" and asset.path.suffix.lower() in (".bmp", ".tga", ".tif", ".tiff", ".psd"):
                    try:
                        png_path = convert_dir / (asset.path.stem + ".png")
                        upload_path = convert_to_png(asset.path, png_path, preserve_alpha=needs_alpha)
                    except Exception as exc:
                        log.warning("[upload_assets] Failed to convert %s to PNG: %s", asset.path.name, exc)
                        self.ctx.asset_upload_errors.append(rel)
                        continue

                result = uploader(upload_path, api_key, creator_id, creator_type, name)
                if result:
                    uploaded[rel] = f"rbxassetid://{result}"
                    log.info("[upload_assets]   %s -> rbxassetid://%s  (source: %s)", name, result, rel)
                    new_uploads.append((rel, result))
                else:
                    log.warning("[upload_assets]   FAILED: %s  (source: %s)", name, rel)
                    self.ctx.asset_upload_errors.append(rel)
                time.sleep(0.3)  # Rate limit (Roblox Open Cloud allows ~60 req/min)

        # Second pass: synthesise + upload meshes embedded in legacy
        # ``.prefab``/``.asset`` files (Unity NativeFormatImporter format).
        # The standard mesh loop above only handles external ``.fbx``/
        # ``.obj`` -- pre-this-pass, mines/decorative props whose geometry
        # lives inside a ``.prefab`` rendered as cube-decal Parts. See
        # ``unity.embedded_mesh_extractor`` for the decoder rationale.
        self._upload_embedded_meshes(
            uploaded, new_uploads, api_key, creator_id, creator_type,
        )

        log.info("[upload_assets] %d assets uploaded, %d errors",
                 len(uploaded), len(self.ctx.asset_upload_errors))

        # Post-upload moderation audit: probe newly-uploaded assets (audio
        # and images get moderation-rejected most often) and strip any that
        # come back rejected, so the rbxlx writer doesn't embed broken IDs.
        # We only check new uploads, not cached entries from previous runs,
        # to keep the audit cost proportional to new work. The audit fails
        # soft — if the metadata endpoint can't make up its mind, we assume
        # the asset is fine and leave it in place.
        self._audit_new_uploads(new_uploads, api_key)

    def _upload_embedded_meshes(
        self,
        uploaded: dict[str, str],
        new_uploads: list[tuple[str, str]],
        api_key: str,
        creator_id: str,
        creator_type: str,
    ) -> None:
        """Synthesise + upload meshes embedded in ``.prefab``/``.asset`` files.

        Walks the parsed scene + prefab library for ``MeshFilter`` references
        whose GUID resolves to a ``.prefab``/``.asset`` (not ``.fbx``/``.obj``).
        For each unique ``(asset_path, file_id)``, decodes the embedded
        geometry, synthesises an ASCII OBJ, and uploads as a ``Model`` asset
        under the synthetic key ``f"{rel_path}#{file_id}"``. ``_resolve_mesh_id``
        and ``studio_resolver`` both check this key shape so the resolved
        ``MeshId`` flows into the rbxlx exactly like a real FBX upload.

        Failure modes from ``parse_embedded_mesh`` are logged once per
        ``(source_key, reason)`` -- face-decal fallback remains the
        recovery path for anything we cannot decode.
        """
        from collections import Counter
        from unity.embedded_mesh_extractor import (
            ExtractionFailure,
            parse_embedded_mesh,
            reset_cache as _reset_extractor_cache,
            synthesize_fbx,
        )
        from roblox.cloud_api import upload_mesh

        guid_index = self.state.guid_index
        parsed_scene = self.state.parsed_scene
        prefab_library = self.state.prefab_library
        if guid_index is None:
            return

        # Pick any modern-FBX (version 7.x) file in the project as the
        # structural template that ``synthesize_fbx`` will mutate to hold
        # each embedded mesh's geometry. Roblox's Open Cloud assets API
        # rejects ``model/obj`` uploads (``"Creating Model from a model/obj
        # file is not supported yet."``), so we clone a known-good FBX
        # and swap its Vertices + PolygonVertexIndex.
        #
        # Legacy FBX 6.x (``Version5`` root) files embed geometry inside
        # the ``Model`` node and have no separate ``Geometry`` object --
        # skip them. ``_find_geometry_nodes`` returns empty, which is
        # the signal we use here.
        from converter.fbx_binary import _find_geometry_nodes, read_fbx

        manifest = self.state.asset_manifest
        template_fbx: Path | None = None
        if manifest is not None:
            for asset in manifest.by_kind.get("mesh", []):
                if asset.path.suffix.lower() != ".fbx" or not asset.path.exists():
                    continue
                try:
                    _ver, _roots, _footer = read_fbx(asset.path)
                except Exception:
                    continue
                if _find_geometry_nodes(_roots):
                    template_fbx = asset.path
                    log.info(
                        "[upload_assets] Using %s as embedded-mesh template",
                        asset.relative_path,
                    )
                    break
        if template_fbx is None:
            log.warning(
                "[upload_assets] No modern-FBX (7.x) template available; "
                "skipping embedded mesh uploads. The pipeline's face-decal "
                "fallback will still render these Parts, but without their "
                "real geometry."
            )
            return

        # Fresh extractor cache per ``upload_assets`` invocation so a
        # rerun never serves stale geometry from a previous run.
        _reset_extractor_cache()

        # Collect unique ``(resolved_path, file_id)`` -> relative_path
        # for every MeshFilter that targets a legacy embedded mesh.
        pairs: dict[tuple[Path, str], Path] = {}

        def _maybe_collect(mesh_guid: str | None, mesh_file_id: str | None) -> None:
            if not mesh_guid or not mesh_file_id:
                return
            resolved = guid_index.resolve(mesh_guid)
            if resolved is None:
                return
            if resolved.suffix.lower() not in (".prefab", ".asset"):
                return
            rel = guid_index.resolve_relative(mesh_guid)
            pairs[(resolved.resolve(), str(mesh_file_id))] = (
                rel if rel is not None else Path(resolved.name)
            )

        if parsed_scene is not None:
            # ``all_nodes`` is a ``dict[str, SceneNode]``; iterate ``.values()``
            # so we get nodes, not file-ID strings. The previous iteration
            # silently missed any embedded mesh referenced by a scene-level
            # MeshFilter (prefab-library walks below still worked, which
            # masked the bug for SimpleFPS).
            for node in parsed_scene.all_nodes.values():
                _maybe_collect(
                    getattr(node, "mesh_guid", None),
                    getattr(node, "mesh_file_id", None),
                )

        def _walk_prefab(node: object) -> None:
            if node is None:
                return
            _maybe_collect(
                getattr(node, "mesh_guid", None),
                getattr(node, "mesh_file_id", None),
            )
            for child in getattr(node, "children", []) or []:
                _walk_prefab(child)

        if prefab_library is not None:
            for tpl in prefab_library.by_name.values():
                _walk_prefab(getattr(tpl, "root", None))

        if not pairs:
            return

        log.info(
            "[upload_assets] Uploading %d embedded-mesh assets (.prefab/.asset)...",
            len(pairs),
        )

        embedded_dir = self.output_dir / "embedded_meshes"
        embedded_dir.mkdir(parents=True, exist_ok=True)
        failure_summary: Counter[tuple[str, str]] = Counter()

        # Honour the same ``.upload_blocklist`` file the main mesh loop
        # reads -- ``_audit_new_uploads`` writes synthetic embedded keys
        # there when Roblox moderation rejects a synthesised mesh, and
        # without this check we'd just re-upload the same rejected
        # geometry on every assemble.
        blocklist_file = self.output_dir / ".upload_blocklist"
        blocklist: set[str] = set()
        if blocklist_file.exists():
            blocklist = {
                line.strip() for line in blocklist_file.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }

        for (path, file_id), rel in pairs.items():
            synthetic_key = f"{rel}#{file_id}"
            if synthetic_key in uploaded:
                continue
            if synthetic_key in blocklist:
                log.info(
                    "[upload_assets] Skipping blocklisted embedded mesh: %s",
                    synthetic_key,
                )
                continue
            result = parse_embedded_mesh(path, file_id)
            if isinstance(result, ExtractionFailure):
                # Deduped warning per ``(source_key, reason)`` -- the
                # face-decal fallback in scene_converter already
                # handles the render side.
                failure_summary[(synthetic_key, result.reason)] += 1
                continue
            try:
                fbx_bytes = synthesize_fbx(result, template_fbx)
            except ValueError as exc:
                log.warning(
                    "[upload_assets]   FBX synthesis failed for %s#%s: %s",
                    path.stem, file_id, exc,
                )
                self.ctx.asset_upload_errors.append(synthetic_key)
                continue
            fbx_path = embedded_dir / f"{path.stem}__{file_id}.fbx"
            fbx_path.write_bytes(fbx_bytes)
            asset_id = upload_mesh(
                fbx_path, api_key, creator_id, creator_type, name=result.name,
            )
            if asset_id:
                uploaded[synthetic_key] = f"rbxassetid://{asset_id}"
                log.info(
                    "[upload_assets]   embedded %s#%s -> rbxassetid://%s",
                    path.stem, file_id, asset_id,
                )
                new_uploads.append((synthetic_key, asset_id))
            else:
                log.warning(
                    "[upload_assets]   FAILED embedded %s#%s",
                    path.stem, file_id,
                )
                self.ctx.asset_upload_errors.append(synthetic_key)
            time.sleep(0.3)  # Same rate limit as the main mesh loop.

        for (key, reason), count in failure_summary.items():
            log.warning(
                "[upload_assets] Skipped embedded mesh %s (%d node ref(s)): %s",
                key, count, reason,
            )

    def _audit_new_uploads(
        self,
        new_uploads: list[tuple[str, str]],
        api_key: str,
    ) -> None:
        """Probe newly-uploaded assets for moderation rejection and strip
        any that are rejected. No-op for empty input or missing API key.
        """
        if not new_uploads or not api_key:
            return

        from roblox.cloud_api import probe_asset_availability
        uploaded = self.ctx.uploaded_assets

        rejected: list[tuple[str, str]] = []
        for rel, asset_id in new_uploads:
            status = probe_asset_availability(asset_id, api_key)
            if status == "rejected":
                rejected.append((rel, asset_id))
            time.sleep(1.1)  # Throttle: metadata endpoint rate-limits hard.

        if rejected:
            log.warning(
                "[upload_assets] Stripping %d moderation-rejected asset(s) "
                "from uploaded_assets so they don't leak into the rbxlx:",
                len(rejected),
            )
            # Also append to the blocklist so the next run doesn't re-upload
            # these — repeated moderation hits on the same content can trigger
            # account-level moderation on the uploader.
            blocklist_file = self.output_dir / ".upload_blocklist"
            existing = set()
            if blocklist_file.exists():
                existing = {line.strip() for line in blocklist_file.read_text().splitlines()}
            new_lines = []
            for rel, asset_id in rejected:
                log.warning("  REJECTED: %s -> rbxassetid://%s", rel, asset_id)
                uploaded.pop(rel, None)
                self.ctx.asset_upload_errors.append(f"{rel} (moderation rejected)")
                if rel not in existing:
                    new_lines.append(rel)
            if new_lines:
                header = "" if blocklist_file.exists() else "# Auto-populated: assets that triggered Roblox moderation.\n"
                with open(blocklist_file, "a") as f:
                    if header:
                        f.write(header)
                    for line in new_lines:
                        f.write(line + "\n")
                log.warning("[upload_assets] Added %d path(s) to %s", len(new_lines), blocklist_file)

    def convert_materials(self) -> None:
        """Phase 4: Map Unity materials to Roblox SurfaceAppearance."""
        log.info("[convert_materials] Mapping materials ...")
        from converter.material_mapper import map_materials  # type: ignore[import-untyped]

        if self.state.parsed_scene is None:
            log.warning("[convert_materials] No parsed scene -- skipping")
            return

        referenced_guids = set(self.state.parsed_scene.referenced_material_guids)

        # Also collect material GUIDs from prefab MeshRenderer components
        from unity.prefab_parser import parse_prefabs
        try:
            if self.state.prefab_library is None:
                self.state.prefab_library = parse_prefabs(self.unity_project_path)
            prefab_lib = self.state.prefab_library
            for pname, prefab in prefab_lib.by_name.items():
                def _collect_mat_guids(node):
                    if node is None:
                        return
                    for comp in getattr(node, 'components', []):
                        if comp.component_type in ('MeshRenderer', 'SkinnedMeshRenderer'):
                            for mat_ref in comp.properties.get('m_Materials', []):
                                if isinstance(mat_ref, dict):
                                    guid = (ref_guid(mat_ref) or '')
                                    if guid and guid != '0000000000000000f000000000000000':
                                        referenced_guids.add(guid)
                    for child in getattr(node, 'children', []):
                        _collect_mat_guids(child)
                _collect_mat_guids(prefab.root)
        except Exception as exc:
            log.warning("[convert_materials] Could not collect prefab material GUIDs: %s", exc)

        # Also pick up .mat files that live in the same Materials/ sibling
        # folder as any referenced FBX. Unity's "search materials" importer
        # setting auto-links these to FBX material slots even though they
        # aren't referenced in scene YAML — and we need them mapped so
        # cutout/transparent alpha is correctly detected for sub-meshes
        # like the chainlink fence.
        if self.state.asset_manifest and self.state.guid_index:
            import re as _re
            extra_from_siblings = 0
            for asset in self.state.asset_manifest.by_kind.get("mesh", []):
                if asset.path.suffix.lower() != ".fbx":
                    continue
                mat_dir = asset.path.parent / "Materials"
                if not mat_dir.is_dir():
                    continue
                for mat_meta in mat_dir.glob("*.mat.meta"):
                    try:
                        m = _re.search(r"guid:\s*([0-9a-f]+)", mat_meta.read_text(errors="replace"))
                    except OSError:
                        continue
                    if m:
                        g = m.group(1)
                        if g not in referenced_guids:
                            referenced_guids.add(g)
                            extra_from_siblings += 1
            if extra_from_siblings:
                log.info("[convert_materials] Added %d sibling Materials/ GUIDs", extra_from_siblings)

        log.info("[convert_materials] Found %d material GUIDs (scene + prefabs)", len(referenced_guids))
        self.state.material_mappings = map_materials(
            unity_project_path=self.unity_project_path,
            guid_index=self.state.guid_index,
            referenced_guids=referenced_guids,
            output_dir=self.output_dir,
            uploaded_assets=self.ctx.uploaded_assets,
        )
        self.ctx.total_materials = len(referenced_guids)
        self.ctx.converted_materials = len(self.state.material_mappings)

        # Execute queued texture operations (channel extraction, inversion)
        # and upload the results if we have an API key.
        from utils.image_processing import (
            extract_channel, invert_image, convert_to_png,
            bake_ao, threshold_alpha, to_grayscale,
            offset_image, scale_normal_map,
        )
        ops_done = 0
        for guid, mapping in self.state.material_mappings.items():
            for op in getattr(mapping, "texture_operations", []):
                try:
                    source = Path(op.source_path)
                    output = Path(op.output_path)
                    if not source.exists():
                        continue
                    # Convert non-PNG source to PNG first if needed
                    actual_source = source
                    if source.suffix.lower() in (".tif", ".tiff", ".psd", ".bmp", ".tga"):
                        try:
                            png_tmp = output.parent / (source.stem + "_tmp.png")
                            actual_source = convert_to_png(source, png_tmp)
                        except Exception as conv_exc:
                            log.warning("[convert_materials] Failed to convert %s to PNG, trying original: %s",
                                        source.name, conv_exc)
                    if op.operation == "extract_r":
                        extract_channel(actual_source, "R", output)
                    elif op.operation == "extract_a":
                        extract_channel(actual_source, "A", output)
                    elif op.operation == "invert_a":
                        invert_image(actual_source, output)
                    elif op.operation == "copy":
                        import shutil
                        shutil.copy2(source, output)
                    elif op.operation == "bake_ao":
                        # Source is the AO map; we overlay onto the material's
                        # current color map if one exists, otherwise skip.
                        color_map = mapping.color_map_path
                        if color_map and Path(color_map).exists():
                            bake_ao(color_map, actual_source, output,
                                    strength=op.ao_strength)
                            mapping.color_map_path = str(output)
                        else:
                            mapping.warnings.append(
                                "bake_ao: no color map to composite onto; skipped"
                            )
                    elif op.operation == "threshold_alpha":
                        threshold_alpha(actual_source, output, cutoff=op.alpha_cutoff)
                    elif op.operation == "to_grayscale":
                        to_grayscale(actual_source, output)
                    else:
                        log.debug("[convert_materials] Unknown texture op: %s", op.operation)
                        continue

                    # Optional post-ops. Chain offset and normal-scale onto
                    # whatever the op just produced (or copy for passthrough).
                    post_in = output if output.exists() else actual_source
                    if op.pixel_offset is not None:
                        offset_image(post_in, output, op.pixel_offset)
                    if op.normal_scale is not None and op.normal_scale != 1.0:
                        scale_normal_map(post_in, output, op.normal_scale)
                    ops_done += 1
                except Exception as exc:
                    log.warning("[convert_materials] Texture op failed: %s: %s", op.operation, exc)

        if ops_done:
            log.info("[convert_materials] Executed %d texture processing operations", ops_done)
        log.info(
            "[convert_materials] %d / %d materials mapped",
            self.ctx.converted_materials,
            self.ctx.total_materials,
        )

        # Phase 4.8: bake per-mesh vertex colors into the albedo texture for
        # any material flagged uses_vertex_colors. Runs after texture ops so
        # the baker sees the final color_map_path.
        self._bake_vertex_colors()

    def _bake_vertex_colors(self) -> None:
        """Bake Unity per-vertex colors into albedo textures for flagged
        materials (Phase 4.8). Graceful fallback when pyassimp is absent —
        each affected material gets a warning surfaced into UNCONVERTED.md
        rather than crashing the run.
        """
        flagged = [
            (guid, mapping) for guid, mapping
            in (self.state.material_mappings or {}).items()
            if getattr(mapping, "uses_vertex_colors", False)
        ]
        if not flagged:
            return

        log.info("[vertex_color_bake] %d materials flagged", len(flagged))
        try:
            from converter.vertex_color_baker import bake_vertex_colors_batch
        except ImportError as exc:
            log.warning("[vertex_color_bake] baker unavailable: %s", exc)
            for _, mapping in flagged:
                mapping.warnings.append(
                    "Vertex-color baking skipped: vertex_color_baker module unavailable"
                )
            return

        # Find mesh referrers for each flagged material. A MeshRenderer
        # with ``m_Materials`` entry pointing at this GUID and a sibling
        # MeshFilter with ``m_Mesh`` → FBX gives us a (mesh, material)
        # pair to bake.
        scene = self.state.parsed_scene
        prefab_library = self.state.prefab_library
        guid_index = self.state.guid_index
        if guid_index is None or scene is None:
            log.info("[vertex_color_bake] missing guid_index or scene — skipped")
            for _, mapping in flagged:
                mapping.warnings.append(
                    "Vertex-color baking skipped: scene/guid_index not available"
                )
            return

        # Invert: material guid → set[(mesh_path, mesh_file_id)]. Threading
        # ``mesh_file_id`` through means an FBX with multiple sub-meshes
        # gets one bake per (mesh_path, mesh_file_id) pair rather than one
        # combined bake for the whole FBX file.
        material_to_meshes: dict[str, set[tuple[Path, str]]] = {}

        def _walk_scene_nodes(nodes):
            for node in nodes:
                mesh_guid = getattr(node, "mesh_guid", None)
                if mesh_guid:
                    mesh_path = guid_index.resolve(mesh_guid)
                    if mesh_path and mesh_path.exists():
                        mesh_file_id = getattr(node, "mesh_file_id", None) or ""
                        for comp in getattr(node, "components", []):
                            if comp.component_type not in (
                                "MeshRenderer", "SkinnedMeshRenderer",
                            ):
                                continue
                            for mat_ref in (comp.properties.get("m_Materials") or []):
                                if isinstance(mat_ref, dict):
                                    mg = (ref_guid(mat_ref) or "")
                                    if mg:
                                        material_to_meshes.setdefault(mg, set()).add(
                                            (mesh_path, mesh_file_id)
                                        )
                _walk_scene_nodes(getattr(node, "children", []))

        _walk_scene_nodes(list(scene.all_nodes.values()))
        if prefab_library is not None:
            for prefab in getattr(prefab_library, "prefabs", []):
                root = getattr(prefab, "root", None)
                if root is not None:
                    _walk_scene_nodes([root])

        pairs: list[tuple[Path, Path, str | None]] = []
        material_pair_index: list[Any] = []  # MaterialMapping per pair, for routing back
        for guid, mapping in flagged:
            meshes = material_to_meshes.get(guid, set())
            if not meshes:
                mapping.warnings.append(
                    "Vertex-color baking skipped: no mesh referrers found for this material"
                )
                continue
            # Prefer the local albedo path (captured pre-upload) over
            # the current color_map_path, which is an ``rbxassetid://``
            # URL once uploads have run.
            color_map = (
                getattr(mapping, "local_color_map_path", None)
                or mapping.color_map_path
            )
            if not color_map:
                # No albedo — caller would need standalone baking; defer
                # that path (rare) so 4.8 stays narrow.
                mapping.warnings.append(
                    "Vertex-color baking skipped: no color_map_path on material (standalone baking not wired)"
                )
                continue
            albedo = Path(color_map)
            if not albedo.exists():
                mapping.warnings.append(
                    f"Vertex-color baking skipped: albedo path missing at {albedo}"
                )
                continue

            # Vertex colors are mesh-specific. Every unique
            # (mesh_path, mesh_file_id) referrer also gets a keyed PNG.
            # The mapping itself points at a "combined" bake of the
            # representative FBX (whole-FBX, no sub-mesh ID) so a single
            # SurfaceAppearance still covers every sub-mesh that uses
            # this material. Per-sub-mesh PNGs land alongside so a
            # follow-up per-part rebinding pass can split.
            sorted_meshes = sorted(
                meshes, key=lambda mp: (str(mp[0]), mp[1] or ""),
            )
            rep_mesh, rep_fid = sorted_meshes[0]
            # Primary (combined) entry — drives the mapping's color_map_path.
            pairs.append((rep_mesh, albedo, None))
            material_pair_index.append(mapping)
            # Auxiliary keyed entries — produce one PNG per (mesh, sub-mesh)
            # without rebinding the mapping.
            for mesh_path, mesh_fid in sorted_meshes:
                if not mesh_fid:
                    continue
                pairs.append((mesh_path, albedo, mesh_fid))
                material_pair_index.append(None)
            if len(sorted_meshes) > 1:
                others = ", ".join(
                    f"{mp.name}:{fid or '-'}" for mp, fid in sorted_meshes[1:]
                )
                mapping.warnings.append(
                    f"Vertex-color baking used combined bake of "
                    f"'{rep_mesh.name}'; "
                    f"other (mesh, sub-mesh) pairs sharing this material "
                    f"each baked to distinct PNGs alongside (per-part "
                    f"rebinding not wired): {others}"
                )

        if not pairs:
            return

        out_dir = self.output_dir / "textures" / "vertex_baked"
        try:
            result = bake_vertex_colors_batch(pairs, out_dir)
        except Exception as exc:
            log.warning("[vertex_color_bake] batch failed: %s", exc)
            for mapping in material_pair_index:
                if mapping is not None:
                    mapping.warnings.append(f"Vertex-color baking failed: {exc}")
            return

        log.info(
            "[vertex_color_bake] %d total, %d baked, %d no_colors, %d skipped",
            result.total, result.baked, result.no_colors, result.skipped,
        )

        for entry, mapping in zip(result.entries, material_pair_index):
            # Secondary sub-mesh entries have mapping=None — they bake
            # additional PNGs into the output dir for follow-up per-part
            # materialization but don't rebind the mapping (Roblox
            # SurfaceAppearance is per-material, not per-sub-mesh).
            if mapping is None:
                continue
            if entry.baked and entry.output_path:
                mapping.color_map_path = str(entry.output_path)
            elif not entry.has_vertex_colors:
                # Most common outcome when the shader says vertex colors
                # but the FBX mesh doesn't actually store them. Log as
                # informational, not a hard warning.
                continue
            elif entry.error:
                mapping.warnings.append(
                    f"Vertex-color baking failed for {entry.mesh_path.name}: {entry.error}"
                )

    def transpile_scripts(self) -> None:
        """Phase 4: Transpile C# scripts to Luau."""
        log.info("[transpile_scripts] Analyzing scripts ...")
        from unity.script_analyzer import analyze_all_scripts  # type: ignore[import-untyped]
        from converter.code_transpiler import transpile_scripts  # type: ignore[import-untyped]

        script_infos = analyze_all_scripts(self.unity_project_path)
        self.ctx.total_scripts = len(script_infos)

        if not script_infos:
            log.info("[transpile_scripts] No runtime scripts found")
            return

        # Build cross-script dependency map from type references.
        project_classes = {si.class_name for si in script_infos if si.class_name}
        for si in script_infos:
            if si.class_name and si.referenced_types:
                deps = [t for t in si.referenced_types if t in project_classes and t != si.class_name]
                if deps:
                    self.state.dependency_map[si.class_name] = deps
        if self.state.dependency_map:
            total_deps = sum(len(v) for v in self.state.dependency_map.values())
            log.info("[transpile_scripts] Built dependency map: %d scripts with %d cross-references",
                     len(self.state.dependency_map), total_deps)

        if self.ctx.scene_runtime_mode == "generic":
            # Generic path: route through the contract pipeline so
            # runtime-bearing MonoBehaviours get the generic prompt,
            # ModuleScript target flip, verifier + reprompt, and
            # require-resolution pass. PR3a built the orchestrator;
            # this wiring completes PR3a's "runtime_mode threaded
            # through transpiler" deliverable (the legacy entry never
            # passed ``runtime_mode``, so without this branch every
            # ``--scene-runtime=generic`` run silently transpiled in
            # legacy mode and produced non-compliant modules).
            from converter.contract_pipeline import transpile_with_contract
            contract_result = transpile_with_contract(
                unity_project_path=self.unity_project_path,
                script_infos=script_infos,
                scene_runtime=self.ctx.scene_runtime,
                use_ai=_config.USE_AI_TRANSPILATION,
                api_key=_config.ANTHROPIC_API_KEY,
                serialized_field_refs=self.ctx.serialized_field_refs or None,
            )
            self.state.transpilation_result = contract_result.transpilation
            # Plumb contract telemetry to ctx so downstream consumers
            # (PR5 auto-mode fallback decision, post-run reports) can
            # read it without re-running the orchestrator. ``setdefault``
            # preserves rows from prior runs in a resume flow; the
            # generic-mode pipeline only transpiles once per conversion
            # but resume paths replay the phase.
            fail_closed_rows = self.ctx.scene_runtime.setdefault(
                "contract_fail_closed", [],
            )
            assert isinstance(fail_closed_rows, list)
            fail_closed_rows.extend(
                {"kind": fc.kind, "detail": fc.detail}
                for fc in contract_result.fail_closed
            )
            # Fail-closed gating: a component module that still violates the
            # contract after reprompt (or fell through to a stub) cannot host
            # the generic runtime, so the converted place throws at boot.
            # Promote each fail-closed reason to a real conversion error so
            # ``conversion_report.success`` reports the truth (it is
            # ``len(ctx.errors) == 0``) instead of shipping a broken place
            # green. We deliberately do NOT fall back to legacy: legacy emit
            # is the ``script.Parent`` form that crashes these MonoBehaviours.
            # Membership-gated so a resume replay of this phase does not
            # double-count the same reason.
            for msg in _contract_failure_errors(contract_result.fail_closed):
                if msg not in self.ctx.errors:
                    self.ctx.errors.append(msg)
            # ``runtime_bearing_paths`` is a frozenset[Path]; JSON the
            # ctx serializes through can't carry either type. Store a
            # sorted list of strings so a resume round-trip is stable.
            self.ctx.scene_runtime["runtime_bearing_paths"] = sorted(
                str(p) for p in contract_result.runtime_bearing_paths
            )
        else:
            # Legacy path -- must stay byte-identical to pre-PR3a
            # behaviour. Do NOT thread ``runtime_mode`` or any other
            # new kwargs here; legacy emit is a tested invariant.
            self.state.transpilation_result = transpile_scripts(
                unity_project_path=self.unity_project_path,
                script_infos=script_infos,
                use_ai=_config.USE_AI_TRANSPILATION,
                api_key=_config.ANTHROPIC_API_KEY,
                serialized_field_refs=self.ctx.serialized_field_refs or None,
            )

        self.ctx.transpiled_scripts = self.state.transpilation_result.total_transpiled
        log.info(
            "[transpile_scripts] %d / %d scripts transpiled",
            self.ctx.transpiled_scripts,
            self.ctx.total_scripts,
        )

        from converter.shared_state_linter import lint_and_rewrite
        self.state.transpilation_result.shared_state_warnings = lint_and_rewrite(
            self.state.transpilation_result.scripts
        )

    def convert_animations(self) -> None:
        """Route Unity animations to inline TweenService scripts.

        Transform-only clips become inline TweenService Scripts; humanoid/
        skeletal clips are unsupported and surfaced to UNCONVERTED.md.

        When a parsed scene is available, pass it so the converter can
        filter controllers to those actually referenced and scene-scope
        the emitted module names.

        Union prefab-derived animator controller GUIDs into the scene
        set before invoking the converter; most projects keep Animators
        inside prefabs, so without this step the scene's set is empty
        and scene scoping never activates.
        """
        log.info("[convert_animations] Discovering and converting animations ...")
        from converter.animation_converter import convert_animations as _convert_anims
        from unity.prefab_parser import aggregate_prefab_controller_refs

        parsed_scenes = [self.state.parsed_scene] if self.state.parsed_scene else None
        if parsed_scenes and self.state.prefab_library is not None:
            for scene in parsed_scenes:
                added = aggregate_prefab_controller_refs(
                    scene, self.state.prefab_library,
                )
                if added:
                    log.info(
                        "[convert_animations] aggregated %d prefab-referenced "
                        "controller GUID(s) into scene %s",
                        added, scene.scene_path.name,
                    )
        self.state.animation_result = _convert_anims(
            unity_project_path=self.unity_project_path,
            guid_index=self.state.guid_index,
            parsed_scenes=parsed_scenes,
            prefab_library=self.state.prefab_library,
        )
        self.ctx.total_animations = self.state.animation_result.total_clips
        self.ctx.converted_animations = self.state.animation_result.total_scripts_generated
        log.info(
            "[convert_animations] %d clips, %d controllers, %d scripts generated",
            self.state.animation_result.total_clips,
            self.state.animation_result.total_controllers,
            self.state.animation_result.total_scripts_generated,
        )

    def resolve_assets(self) -> None:
        """Phase 3b: Resolve uploaded mesh Model IDs to real MeshIds + InitialSizes.

        Uses the Roblox Luau Execution API to run InsertService:LoadAsset on
        each uploaded mesh Model ID, extracting the real MeshId, InitialSize,
        TextureID, and position data.  Results are stored in the conversion
        context (mesh_native_sizes, mesh_hierarchies) for use by convert_scene.

        Skips if mesh data is already populated (from a previous run or manual
        resolve step).
        """
        # Check whether EVERY currently-uploaded mesh has a resolved entry.
        # Count-based checks were not enough: a previous run that resolved
        # {A, B, C} could leave mesh_native_sizes with 3 entries; if the
        # current uploaded_assets is {D, E} after the user swapped meshes,
        # resolved_count (3) >= uploaded_mesh_count (2) would falsely
        # report "all resolved" and skip resolution of D and E entirely.
        #
        # Mesh-key predicate accepts both ``.fbx``/``.obj`` keys (the
        # external-mesh upload path) and synthetic ``<rel>#<file_id>``
        # keys (the embedded-mesh upload path -- see
        # ``unity.embedded_mesh_extractor``). Hoisted to
        # ``core.asset_keys`` so the Studio resolver and the scene
        # converter use the same definition.
        from core.asset_keys import is_mesh_asset_key as _is_mesh_key

        uploaded_mesh_keys = {
            k for k in self.ctx.uploaded_assets if _is_mesh_key(k)
        }
        uploaded_mesh_count = len(uploaded_mesh_keys)
        resolved_count = len(self.ctx.mesh_native_sizes)
        all_meshes_resolved = (
            uploaded_mesh_count > 0
            and uploaded_mesh_keys.issubset(self.ctx.mesh_native_sizes.keys())
        )
        if all_meshes_resolved:
            log.info(
                "[resolve_assets] Mesh resolution data already present "
                "(%d/%d meshes) — skipping mesh resolve, but still "
                "validating uid/pid below so a retarget refreshes the "
                "shared ID cache.",
                resolved_count, uploaded_mesh_count,
            )

        if self.skip_upload:
            log.info("[resolve_assets] Skipping (--no-upload)")
            return

        import config
        api_key = config.ROBLOX_API_KEY
        creator_id = str(config.ROBLOX_CREATOR_ID or "")

        if not api_key or not creator_id:
            log.warning("[resolve_assets] No API key or creator ID — cannot resolve meshes headlessly")
            return

        # Ensure we have a universe/place to execute Luau on.
        universe_id = self.ctx.universe_id
        place_id = self.ctx.place_id

        # Try to recover IDs from a persistent cache file (survives context resets)
        if not universe_id or not place_id:
            from roblox.id_cache import read_ids
            cached_uid, cached_pid = read_ids(self.output_dir)
            if cached_uid and cached_pid:
                universe_id = cached_uid
                place_id = cached_pid
                self.ctx.universe_id = universe_id
                self.ctx.place_id = place_id
                log.info("[resolve_assets] Recovered IDs from cache: universe=%s place=%s",
                         universe_id, place_id)

        # ID cache write deferred until we either finish a resolve OR
        # confirm there's nothing to resolve. Writing premature IDs at
        # phase entry would poison the shared cache for later u2r publish
        # / interactive upload commands if assemble was invoked with a
        # typo'd or unauthorized experience ID.

        # Find uploaded mesh assets (Model IDs from cloud upload). Skip
        # ones already resolved so a force-rerun doesn't redo them — and
        # so transient batch failures can't shrink a prior resolution.
        # When ALL meshes are already resolved, fall through to the
        # no-mesh validation+cache-refresh path below so a retarget still
        # updates .roblox_ids.json.
        already_resolved = self.ctx.mesh_native_sizes
        mesh_assets = {} if all_meshes_resolved else {
            k: v for k, v in self.ctx.uploaded_assets.items()
            if _is_mesh_key(k) and k not in already_resolved
        }

        # No universe/place IDs. Open Cloud does not support universe
        # creation via API-key auth, so we cannot auto-provision. The
        # behaviour split:
        #   * If any uploaded mesh is unresolved, halt: writing
        #     converted_place.rbxlx with raw Model IDs produces a
        #     visibly broken artifact (Studio's MeshContentProvider
        #     can't fetch Model IDs as MeshIds, geometry vanishes,
        #     and the spawned character cannot move because no
        #     floor loads). The previous silent-warning path
        #     understated the consequence and let users open a
        #     dead-on-arrival rbxlx without realising why.
        #   * If there are no unresolved meshes (mesh-free project,
        #     or fully resolved on a prior run), keep going: the
        #     cache-refresh below also no-ops without IDs, so this
        #     is an honest skip.
        if not universe_id or not place_id:
            if mesh_assets:
                raise RuntimeError(
                    "[resolve_assets] Cannot finalize converted_place.rbxlx: "
                    f"{len(mesh_assets)} uploaded mesh(es) still carry "
                    "Roblox Model IDs that Studio cannot fetch directly. "
                    "Pass --universe-id / --place-id to assemble (or run "
                    "'upload' once with IDs to populate "
                    "<output>/.roblox_ids.json, then rerun assemble). "
                    "Without IDs the local rbxlx loads empty in Studio "
                    "(MeshContentProvider 'could not fetch') and the "
                    "spawned character cannot move because no floor "
                    "geometry resolves. Create an experience at "
                    "https://create.roblox.com/dashboard/creations "
                    "(Baseplate) and copy the IDs from the URL: "
                    ".../experiences/<UNIVERSE_ID>/places/<PLACE_ID>/configure. "
                    "Use --no-upload to skip cloud work entirely."
                )
            log.info(
                "[resolve_assets] No universe/place IDs supplied and no "
                "unresolved meshes — skipping cache-refresh validation."
            )
            return
        if not mesh_assets:
            log.info(
                "[resolve_assets] No new mesh assets to resolve "
                "(%d already resolved)", len(already_resolved),
            )
            # Validate uid/pid against Open Cloud before caching. Without
            # the validation call, a typo'd retarget on a mesh-free output
            # would silently poison .roblox_ids.json. Without ANY cache
            # write, retargets on mesh-free outputs would never refresh
            # the cache, so a later publish would target the prior
            # experience. The minimal execute_luau call here resolves both.
            from roblox.cloud_api import execute_luau
            ok = execute_luau(
                api_key, universe_id, place_id, "return 'ok'", timeout="60s",
            )
            if ok is not None:
                from roblox.id_cache import write_ids
                write_ids(self.output_dir, universe_id, place_id)
                log.info(
                    "[resolve_assets] uid=%s pid=%s validated; cache refreshed",
                    universe_id, place_id,
                )
            else:
                log.warning(
                    "[resolve_assets] uid=%s pid=%s did not authenticate; "
                    "cache NOT refreshed", universe_id, place_id,
                )
            return

        log.info("[resolve_assets] Resolving %d mesh assets via Luau Execution API...", len(mesh_assets))

        # Build resolve script: LoadAsset each Model ID, extract MeshPart data
        # Process in small batches with retries — Roblox's per-script Luau
        # Execution timeout will kill a script that does too many LoadAsset
        # calls in one go, and the API itself returns transient internal-
        # error timeouts under load. Both classes of failure are recovered
        # from by re-running with smaller batches and a retry loop.
        from roblox.cloud_api import execute_luau

        batch_size = 4
        max_retries = 3
        mesh_items = list(mesh_assets.items())
        all_results = []

        for batch_start in range(0, len(mesh_items), batch_size):
            batch = mesh_items[batch_start:batch_start + batch_size]
            batch_num = batch_start // batch_size + 1
            models_lua = ",\n".join(
                f'    {{id={v.replace("rbxassetid://", "")}, path="{k}"}}'
                for k, v in batch
            )
            script = f'''local InsertService = game:GetService("InsertService")
local models = {{
{models_lua}
}}
local allData = {{}}
for _, entry in models do
    local ok, model = pcall(InsertService.LoadAsset, InsertService, entry.id)
    if not ok then continue end
    for _, d in model:GetDescendants() do
        if d:IsA("MeshPart") then
            local sz = d.Size; local pos = d.Position
            table.insert(allData, string.format("%s|%s|%s|%.4f,%.4f,%.4f|%.4f,%.4f,%.4f|%s",
                entry.path, d.Name, d.MeshId, sz.X, sz.Y, sz.Z, pos.X, pos.Y, pos.Z,
                d.TextureID ~= "" and d.TextureID or ""))
        end
    end
    model:Destroy()
end
return table.concat(allData, "\\n")'''

            success = False
            for attempt in range(1, max_retries + 1):
                result = execute_luau(api_key, universe_id, place_id, script)
                if result and result.get("state") == "COMPLETE":
                    outputs = result.get("output", {})
                    results_list = outputs.get("results", [])
                    if results_list:
                        ret = results_list[0]
                        if isinstance(ret, dict):
                            text = ret.get("value", "")
                        else:
                            text = str(ret)
                        if text:
                            lines = text.strip().split("\n")
                            all_results.extend(lines)
                            log.info(
                                "[resolve_assets] Batch %d (attempt %d): "
                                "resolved %d sub-meshes",
                                batch_num, attempt, len(lines),
                            )
                            success = True
                            break
                log.warning(
                    "[resolve_assets] Batch %d attempt %d/%d failed",
                    batch_num, attempt, max_retries,
                )
            if not success:
                log.warning(
                    "[resolve_assets] Batch %d giving up after %d attempts "
                    "— those meshes stay unresolved; re-run resolve_assets "
                    "to retry just the leftovers.",
                    batch_num, max_retries,
                )

        # Parse results into mesh_native_sizes and mesh_hierarchies
        if all_results:
            mesh_native_sizes = {}
            mesh_hierarchies = {}
            for line in all_results:
                parts = line.split("|")
                if len(parts) < 5:
                    continue
                path, name, mesh_id, size_str, pos_str = parts[:5]
                texture_id = parts[5] if len(parts) > 5 else ""
                try:
                    sx, sy, sz = [float(x) for x in size_str.split(",")]
                    px, py, pz = [float(x) for x in pos_str.split(",")]
                except (ValueError, IndexError):
                    continue
                if path not in mesh_native_sizes:
                    mesh_native_sizes[path] = [sx, sy, sz]
                if path not in mesh_hierarchies:
                    mesh_hierarchies[path] = []
                entry = {"name": name, "meshId": mesh_id,
                         "size": [sx, sy, sz], "position": [px, py, pz]}
                if texture_id:
                    entry["textureId"] = texture_id
                mesh_hierarchies[path].append(entry)

            # Invariant: synthetic embedded-mesh keys MUST resolve to
            # exactly one sub-mesh (our synthesised FBX has a single
            # Geometry node by construction in
            # ``unity.embedded_mesh_extractor.synthesize_fbx``). When
            # the resolver returns more, the FBX template-cleanup
            # leaked extra Geometries and ``sub_meshes[0]`` would
            # silently bind to whichever Geometry the upload happened
            # to enumerate first -- a correctness-by-coincidence bug.
            # Loud-fail so the next leak is caught at conversion time,
            # not via a Studio playtest.
            from core.asset_keys import is_embedded_mesh_key
            for k, subs in mesh_hierarchies.items():
                if is_embedded_mesh_key(k) and len(subs) != 1:
                    log.warning(
                        "[resolve_assets] Embedded-mesh key %r resolved to "
                        "%d sub-meshes (expected exactly 1). The FBX "
                        "template likely shipped extra Geometry nodes that "
                        "weren't stripped; sub_meshes[0] binding is now "
                        "non-deterministic and may bind to the wrong "
                        "geometry. Names: %s",
                        k, len(subs), [s.get("name") for s in subs],
                    )

            # Merge into existing tables instead of replacing. A transient
            # batch failure during a force-rerun would otherwise shrink a
            # prior mostly-complete resolution by overwriting it with this
            # run's smaller result set.
            merged_sizes = {**already_resolved, **mesh_native_sizes}
            existing_hierarchies = self.ctx.mesh_hierarchies
            merged_hierarchies = {**existing_hierarchies, **mesh_hierarchies}
            self.ctx.mesh_native_sizes = merged_sizes
            self.ctx.mesh_hierarchies = merged_hierarchies
            log.info(
                "[resolve_assets] Resolved %d new meshes (total %d, %d sub-meshes)",
                len(mesh_native_sizes), len(merged_sizes),
                sum(len(v) for v in merged_hierarchies.values()),
            )
            # Persist IDs only AFTER a successful resolve so we know the
            # uid/pid pair actually authenticated against Open Cloud.
            from roblox.id_cache import write_ids
            write_ids(self.output_dir, universe_id, place_id)
        else:
            log.warning("[resolve_assets] No mesh resolution data obtained")

    def convert_scene(self) -> None:
        """Convert the parsed scene hierarchy to Roblox parts."""
        log.info("[convert_scene] Converting scene hierarchy ...")
        from converter.scene_converter import convert_scene  # type: ignore[import-untyped]

        if self.state.parsed_scene is None:
            log.warning("[convert_scene] No parsed scene -- skipping")
            return

        # Ensure material_mappings are populated (needed when resuming from this phase)
        if not self.state.material_mappings and self.state.guid_index:
            log.info("[convert_scene] Re-running material mapping (skipped phase resume)")
            from converter.material_mapper import map_materials
            referenced_guids = set()
            if self.state.parsed_scene:
                referenced_guids.update(self.state.parsed_scene.referenced_material_guids)
            if self.state.prefab_library:
                referenced_guids.update(self.state.prefab_library.referenced_material_guids)
            self.state.material_mappings = map_materials(
                unity_project_path=self.unity_project_path,
                guid_index=self.state.guid_index,
                referenced_guids=referenced_guids,
                output_dir=self.output_dir,
                uploaded_assets=self.ctx.uploaded_assets,
            )
            log.info("[convert_scene] Loaded %d material mappings", len(self.state.material_mappings))

        # Load mesh native sizes if available in context.
        # JSON-loaded entries may have stale shapes (e.g. truncated lists);
        # validate per-value while trusting the dataclass-guaranteed dict.
        mesh_native_sizes = {}
        for k, v in self.ctx.mesh_native_sizes.items():
            if isinstance(v, (list, tuple)) and len(v) == 3:
                mesh_native_sizes[k] = tuple(v)

        # Load mesh texture IDs if available in context
        mesh_texture_ids = self.ctx.mesh_texture_ids

        # Pre-seed the scene converter's prefab cache to avoid re-parsing
        if self.state.prefab_library and self.state.guid_index:
            from converter.scene_converter import _prefab_lib_cache
            cache_key = str(self.state.guid_index.project_root)
            if cache_key not in _prefab_lib_cache:
                _prefab_lib_cache[cache_key] = self.state.prefab_library

        # Load mesh hierarchies from context (populated by Studio resolution)
        mesh_hierarchies = self.ctx.mesh_hierarchies

        # Load FBX bounding boxes (fallback for InitialSize when Studio not available).
        # Validate per-value: JSON load may carry stale-shape entries.
        fbx_bounding_boxes: dict[str, tuple[float, float, float]] = {}
        for k, v in self.ctx.fbx_bounding_boxes.items():
            if isinstance(v, (list, tuple)) and len(v) == 3:
                fbx_bounding_boxes[k] = tuple(v)

        self.state.rbx_place = convert_scene(
            parsed_scene=self.state.parsed_scene,
            guid_index=self.state.guid_index,
            asset_manifest=self.state.asset_manifest,
            material_mappings=self.state.material_mappings,
            uploaded_assets=self.ctx.uploaded_assets,
            mesh_native_sizes=mesh_native_sizes,
            mesh_texture_ids=mesh_texture_ids,
            mesh_hierarchies=mesh_hierarchies,
            fbx_bounding_boxes=fbx_bounding_boxes,
            unity_project_root=self.unity_project_path,
            # PR3c: thread the planner artifact + mode through so the
            # generic-only inactive-retention carve-out in ``_convert_node``
            # can see which inactive GameObjects the host runtime needs to
            # bind. Legacy mode passes an unused artifact (the carve-out
            # gates on mode first), so legacy emit stays byte-identical.
            scene_runtime=self.ctx.scene_runtime,
            scene_runtime_mode=self.ctx.scene_runtime_mode,
        )
        # Count all parts recursively (including nested prefab children)
        def _count_parts(parts):
            total = 0
            for p in parts:
                total += 1
                if hasattr(p, "children"):
                    total += _count_parts(p.children)
            return total
        self.ctx.converted_parts = _count_parts(self.state.rbx_place.workspace_parts)
        log.info(
            "[convert_scene] %d total parts (%d top-level)",
            self.ctx.converted_parts,
            len(self.state.rbx_place.workspace_parts),
        )

    SUBPHASE_ORDER: tuple[str, ...] = (
        # ``_subphase_emit_scripts_to_disk`` lifted to
        # ``materialize_and_classify`` in slice 8 commit 2.
        # ``_subphase_cohere_scripts`` lifted in slice 8 commit 3.
        # ``_classify_storage`` lifted in slice 8 commit 4.
        "_bind_scripts_to_parts",
        "_subphase_inject_autogen_scripts",
        "_inject_runtime_modules",
        "_subphase_inject_scene_runtime",
        # Phase 2a slice 8 commit 5 — Option (b) safety net. Runs AFTER
        # the three injection subphases that append scripts to
        # ``rbx_place.scripts`` post-classify. Stamps an explicit
        # ``parent_path`` on any late-appended script whose generator
        # left the field as ``None``, freezing the rbxlx_writer default
        # routing into the storage plan as an explicit decision.
        "_classify_late_appended_scripts",
        "_generate_prefab_packages",
        "_subphase_encode_terrain",
        "_subphase_inject_mesh_loader",
        "_subphase_patch_setup_sounds",
        "_subphase_finalize_scripts_to_disk",
    )
    """Order in which write_output() invokes its subphases.

    Each subphase mutates ``self.state.rbx_place`` and/or writes files to
    ``self.output_dir``. Ordering is load-bearing:
      - cohere_scripts must run after emit_scripts_to_disk (needs scripts in place)
      - classify_storage must run after cohere_scripts (Script→ModuleScript reclassification
        affects which storage container each script belongs in)
      - inject_autogen_scripts must run after classify_storage (autogen scripts
        need to know about FPS controllers to skip clobbering modules)
      - encode_terrain reads ``state.rbx_place.terrain_world_offset`` set by convert_scene
      - finalize_scripts_to_disk must run last so on-disk scripts/ matches
        the in-memory state about to be serialized.
    A test asserts the actual call sequence in write_output matches this tuple.
    """

    def _delete_pruned_script_from_disk(self, script: RbxScript) -> None:
        """Delete a pruned script's cached ``.luau`` file from disk.

        When a script is pruned from ``rbx_place.scripts`` (legacy
        artifact or stale gameplay runtime module), the assemble /
        publish rebuild paths rehydrate scripts from disk, so the
        in-memory prune doesn't stick unless the on-disk file goes too.

        Uses ``source_path`` when set (preserves nested-dir routing),
        otherwise falls back to ``<name>.luau`` at the top of
        ``scripts/`` and the ``animations/`` subdir.
        """
        scripts_dir = self.output_dir / "scripts"
        if not scripts_dir.is_dir():
            return
        candidates: list[Path] = []
        if script.source_path:
            candidates.append(scripts_dir / script.source_path)
        candidates.append(scripts_dir / f"{script.name}.luau")
        candidates.append(scripts_dir / "animations" / f"{script.name}.luau")
        for candidate in candidates:
            if candidate.is_file():
                try:
                    candidate.unlink()
                    log.info(
                        "[prune] Deleted stale on-disk script: %s",
                        candidate.relative_to(self.output_dir),
                    )
                except OSError as exc:
                    log.warning(
                        "[prune] Failed to unlink %s: %s",
                        candidate, exc,
                    )

    MATERIALIZE_AND_CLASSIFY_ORDER: tuple[str, ...] = (
        # Phase 2a slice 8: lifted out of ``write_output``. ``emit`` lifted
        # in commit 2; ``cohere`` lifted in commit 3; ``classify`` lifted
        # in commit 4.
        "_subphase_emit_scripts_to_disk",
        "_subphase_cohere_scripts",
        "_classify_storage",
    )
    """Order in which :meth:`materialize_and_classify` invokes its subphases.

    Empty in slice 8 commit 1 (phase introduced empty). Subsequent
    commits lift ``_subphase_emit_scripts_to_disk``,
    ``_subphase_cohere_scripts``, and ``_classify_storage`` into it from
    ``SUBPHASE_ORDER`` in that order. Ordering rationale (carried over
    from ``SUBPHASE_ORDER``):

    - cohere must run AFTER emit (needs scripts in place)
    - classify must run AFTER cohere (Script→ModuleScript reclassification
      affects which storage container each script belongs in)
    """

    def materialize_and_classify(self) -> None:
        """Phase: materialize the script set + cohere + classify storage.

        Phase 2a slice 8: lifts the three subphases
        (:meth:`_subphase_emit_scripts_to_disk`,
        :meth:`_subphase_cohere_scripts`, :meth:`_classify_storage`) out
        of :meth:`write_output` so a single ordered phase computes the
        authoritative script set + storage plan upstream of
        ``write_output``. ``write_output`` then consumes the persisted
        ``StoragePlan`` and the populated ``rbx_place.scripts`` instead
        of computing them itself.

        Slice 8 lifts the subphases over multiple commits; this method
        is the orchestration hook. The first commit introduces the phase
        empty; subsequent commits move emit → cohere → classify into it.
        """
        log.info("[materialize_and_classify] Starting ...")

        if self.state.rbx_place is None:
            log.warning(
                "[materialize_and_classify] No RbxPlace -- skipping "
                "(convert_scene was a no-op or hasn't run)"
            )
            return

        # Subphases run in the order declared in
        # :data:`MATERIALIZE_AND_CLASSIFY_ORDER`. Ordering is load-bearing:
        # cohere mutates script_type which classify reads; emit must run
        # first because cohere/classify both walk ``rbx_place.scripts``.
        self._subphase_emit_scripts_to_disk()
        self._subphase_cohere_scripts()
        self._classify_storage()

    def write_output(self) -> None:
        """Phase 6: Serialize the Roblox place to disk.

        Orchestrates the subphases listed in :data:`SUBPHASE_ORDER`.
        Each subphase is a separate method so it can be invoked or
        mocked in isolation by tests.
        """
        log.info("[write_output] Writing output ...")

        if self.state.rbx_place is None:
            log.warning("[write_output] No RbxPlace -- skipping")
            return

        # write_output is the assembly + serialization pipeline. Each subphase
        # below mutates self.state.rbx_place and/or writes files to self.output_dir.
        # Order is load-bearing — see SUBPHASE_ORDER for dependency rationale.
        # Phase 2a slice 8 commits 2-4: ``_subphase_emit_scripts_to_disk``,
        # ``_subphase_cohere_scripts``, and ``_classify_storage`` are owned
        # by ``materialize_and_classify``; write_output consumes the cohered
        # ``rbx_place.scripts`` list and the persisted ``StoragePlan``.
        self._bind_scripts_to_parts()
        self._subphase_inject_autogen_scripts()
        self._inject_runtime_modules()
        self._subphase_inject_scene_runtime()
        self._classify_late_appended_scripts()
        self._generate_prefab_packages()
        self._subphase_encode_terrain()
        self._subphase_inject_mesh_loader()

        self._subphase_patch_setup_sounds()

        # Semantic post-transpile validator. Catches the class of bugs
        # that pass Luau syntax but break at runtime in Roblox-specific
        # ways (weapon clones parented to character descendants, anchored
        # parts with no weld, sub-meter Vector3 literals used in a
        # studs-space context, hardcoded camera heights, etc.). Surfaces
        # warnings to ``conversion_report.json.semantic_warnings``; no
        # auto-fix — the rules over-detect by design, so a programmatic
        # rewrite would regress legitimate stud-space sub-unit offsets
        # (e.g. ``head.Position + Vector3.new(0, 0.5, 0)`` is correct).
        #
        # Runs AFTER every script-mutating subphase (cohere, autogen,
        # mesh-loader injection, setup-sounds patches) so the line
        # numbers and snippets the validator reports match the source
        # that gets written to disk and serialised into the rbxlx.
        #
        # ``_bind_scripts_to_parts`` moves MonoBehaviour-style gameplay
        # scripts out of the flat ``rbx_place.scripts`` list onto their
        # owning parts. Collect every script in the tree so the
        # validator sees the same set the rbxlx writer will serialise.
        from converter.semantic_validators import run_semantic_validators
        all_scripts = self._collect_all_scripts()
        semantic_report = run_semantic_validators(all_scripts)
        self.state.semantic_report = semantic_report
        if semantic_report.issues:
            log.info(
                "[write_output] semantic validator: %d warning(s) "
                "across %d rule(s)",
                len(semantic_report.issues),
                len(semantic_report.counts_by_rule),
            )
            for rule, count in semantic_report.counts_by_rule.items():
                log.info("[write_output]   %s: %d", rule, count)

        self._subphase_finalize_scripts_to_disk()

        # Write the RBXLX file.
        import config as _cfg_mod
        rbxlx_path = self.output_dir / _cfg_mod.RBXLX_OUTPUT_FILENAME
        from roblox.rbxlx_writer import write_rbxlx
        result = write_rbxlx(self.state.rbx_place, rbxlx_path)
        log.info("[write_output] RBXLX: %s (%d parts, %d scripts)",
                 rbxlx_path, result.get("parts_written", 0),
                 result.get("scripts_written", 0))

        # Sibling .rbxl for the Open Cloud place endpoint (binary-only).
        if self.skip_binary_rbxl:
            log.debug("[write_output] skip_binary_rbxl set; skipping binary .rbxl")
        else:
            try:
                from roblox.rbxl_binary_writer import xml_to_binary
                rbxl_path = xml_to_binary(rbxlx_path)
                log.info("[write_output] Binary RBXL: %s (%.1f KB)",
                         rbxl_path, rbxl_path.stat().st_size / 1024)
            except ImportError:
                log.debug("[write_output] lz4 not installed; skipping binary .rbxl")
            except Exception as exc:
                log.warning("[write_output] Binary .rbxl conversion failed: %s", exc)

        # Verify transform accuracy: compare Unity scene positions to rbxlx output.
        # Logs errors for any object with >10° rotation error or >2m position error.
        try:
            from tools.transform_audit import parse_rbxlx, parse_unity_scene_transforms, compare_transforms
            scene_path = self.state.scene_path or (
                Path(self.ctx.selected_scene) if self.ctx.selected_scene else None
            )
            if scene_path and Path(scene_path).exists() and rbxlx_path.exists():
                roblox_data = parse_rbxlx(str(rbxlx_path))
                unity_data = parse_unity_scene_transforms(str(scene_path))
                discrepancies = compare_transforms(
                    unity_data, roblox_data,
                    pos_threshold=999999, rot_threshold=10.0,
                )
                rot_errors = [d for d in discrepancies if d['rot_error_deg'] > 10.0]
                if rot_errors:
                    log.warning("[write_output] Transform audit: %d objects with >10° rotation error", len(rot_errors))
                    for d in rot_errors[:10]:
                        log.warning("  %s: %.1f° rotation error (path: %s)",
                                   d['name'], d['rot_error_deg'], d.get('path', '?'))
                else:
                    log.info("[write_output] Transform audit: all rotations within 10° tolerance")
        except Exception as exc:
            log.debug("[write_output] Transform audit skipped: %s", exc)

        # Post-process: strip local file paths from SurfaceAppearance textures.
        # Done via regex on raw XML to preserve CDATA sections in scripts.
        # We only strip URLs that look like filesystem paths — Roblox
        # ``rbxassetid://`` URLs and arbitrary ``http(s)://`` references
        # are left alone (Studio resolves the former and tolerates the
        # latter; a previous "anything-with-a-slash" filter would have
        # eaten valid HTTP asset URLs along with local paths).
        import re as _re_post
        raw_xml = rbxlx_path.read_text(encoding="utf-8")
        pattern = r'<Content name="[^"]*">\s*<url>([^<]*)</url>\s*</Content>'

        def _is_local_path(url: str) -> bool:
            url = url.strip()
            if not url:
                return False
            if url.startswith(("rbxassetid://", "http://", "https://")):
                return False
            # Local indicators: contains ``/`` or ``\``, or a Windows
            # drive letter, or an explicit file:// scheme.
            return (
                url.startswith("file://")
                or "/" in url
                or "\\" in url
                or (len(url) >= 2 and url[1] == ":")
            )

        def _strip_if_local(m: "_re_post.Match[str]") -> str:
            return "" if _is_local_path(m.group(1)) else m.group(0)

        stripped = sum(
            1 for m in _re_post.finditer(pattern, raw_xml)
            if _is_local_path(m.group(1))
        )
        if stripped:
            raw_xml = _re_post.sub(pattern, _strip_if_local, raw_xml)
            rbxlx_path.write_text(raw_xml, encoding="utf-8")
            log.info("[write_output] Stripped %d invalid local texture paths from SurfaceAppearances", stripped)

        # UNCONVERTED.md — human-readable log of features the converter
        # deliberately dropped (e.g. binary .controller files that need
        # UnityPy text-export, 2D blend trees). Sources contribute via
        # their result objects' ``unconverted`` list.
        self._write_unconverted_md()

        # Structured conversion report (see converter.report_generator).
        # The interactive report() command decorates this file in place.
        report_path = self.output_dir / "conversion_report.json"
        structured = self._build_conversion_report(rbxlx_path, result, report_path)
        from converter.report_generator import generate_report
        generate_report(structured, report_path, print_summary=False)

        # Persist context.
        self.ctx.save(self._context_path)
        log.info("[write_output] Context saved to %s", self._context_path)
    def _subphase_emit_scripts_to_disk(self) -> None:
        """Materialize transpiled, animation, and ScriptableObject scripts onto disk
        and into ``state.rbx_place.scripts``. Honors the ``preserve_scripts``
        path that lets users hand-edit Luau between assemble and upload."""
        # Write transpiled scripts to output directory AND add to RbxPlace.
        scripts_dir = self.output_dir / "scripts"
        # When transpile_scripts was skipped (e.g. user hand-edited Luau during
        # the review step and then ran assemble without --retranspile), preserve
        # the existing scripts directory so hand-edits survive.
        preserve_scripts = (
            "transpile_scripts" in self.ctx.completed_phases
            and not getattr(self, "_retranspile", False)
            and scripts_dir.exists()
            and not self.state.transpilation_result
        )
        if not preserve_scripts:
            if scripts_dir.exists():
                import shutil
                shutil.rmtree(scripts_dir)
        scripts_dir.mkdir(parents=True, exist_ok=True)

        # ScriptableObject ModuleScripts: write to disk *after* the optional
        # rmtree so the files survive into the output. Both the fresh-transpile
        # and preserved-script paths end up with the same files on disk.
        # On the preserve path, only backfill files that aren't already
        # there — never clobber a hand-edited ScriptableObject module.
        so_unique_names: dict[int, str] = {}
        if self.state.scriptable_objects:
            from converter.scriptable_object_converter import resolve_unique_asset_names
            so_unique_names = resolve_unique_asset_names(
                self.state.scriptable_objects.assets
            )
            so_dir = scripts_dir / "scriptable_objects"
            so_dir.mkdir(parents=True, exist_ok=True)
            for asset in self.state.scriptable_objects.assets:
                stem = so_unique_names[id(asset)]
                out_path = so_dir / f"{stem}.luau"
                if preserve_scripts and out_path.exists():
                    continue
                out_path.write_text(asset.luau_source, encoding="utf-8")

        if preserve_scripts:
            self._rehydrate_scripts_from_disk(scripts_dir)

        elif self.state.transpilation_result:
            for ts in self.state.transpilation_result.scripts:
                out_path = scripts_dir / ts.output_filename
                out_path.write_text(ts.luau_source, encoding="utf-8")
                self.state.rbx_place.scripts.append(RbxScript(
                    name=ts.output_filename.replace(".luau", ""),
                    source=ts.luau_source,
                    script_type=ts.script_type,
                    # Phase 2a slice 5 round 2: stamp the intrinsic
                    # class at transpile-time. ``ts.script_type`` is
                    # the output of ``code_transpiler._classify_script_type``
                    # (plus generic-runtime override), which is the
                    # pre-classifier signal we want to preserve.
                    # ``classify_storage`` later mutates the live
                    # ``script_type`` field but leaves
                    # ``intrinsic_script_type`` untouched, so
                    # ``derive_intrinsic_script_class`` can read the
                    # original C# code-analysis decision.
                    intrinsic_script_type=ts.script_type,
                    source_path=ts.output_filename,
                ))

        # Write animation scripts to output directory AND add to RbxPlace.
        if self.state.animation_result and self.state.animation_result.generated_scripts:
            anim_scripts_dir = scripts_dir / "animations"
            anim_scripts_dir.mkdir(parents=True, exist_ok=True)
            for script_name, luau_source in self.state.animation_result.generated_scripts:
                out_path = anim_scripts_dir / f"{script_name}.luau"
                out_path.write_text(luau_source, encoding="utf-8")
                self.state.rbx_place.scripts.append(RbxScript(
                    name=script_name,
                    source=luau_source,
                    script_type="Script",
                    # Phase 2a slice 5 round 2: animation_converter
                    # emits Anim_* scripts as plain ``"Script"`` at
                    # birth. ``_build_and_apply_topology`` may later
                    # flip ``script_type`` to ``"LocalScript"`` when
                    # the driver lives on the client; the intrinsic
                    # value remains the original ``"Script"``.
                    intrinsic_script_type="Script",
                    source_path=f"animations/{script_name}.luau",
                ))
            log.info("[write_output] Wrote %d animation scripts",
                     len(self.state.animation_result.generated_scripts))

        # Attach ScriptableObject ModuleScripts on the fresh-transpile path.
        # Rehydration already picks them up from disk; dedupe by name. Names
        # were resolved above to disambiguate folder collisions (Audio/Settings
        # vs Graphics/Settings) so the rbx_place.scripts list keeps both.
        if self.state.scriptable_objects:
            existing = {s.name for s in self.state.rbx_place.scripts}
            added = 0
            for asset in self.state.scriptable_objects.assets:
                stem = so_unique_names[id(asset)]
                if stem in existing:
                    continue
                self.state.rbx_place.scripts.append(RbxScript(
                    name=stem,
                    source=asset.luau_source,
                    script_type="ModuleScript",
                    # Phase 2a slice 5 round 2: ScriptableObjects are
                    # always ModuleScripts by definition; the intrinsic
                    # value mirrors the constructed ``script_type``.
                    intrinsic_script_type="ModuleScript",
                    source_path=f"scriptable_objects/{stem}.luau",
                ))
                existing.add(stem)
                added += 1
            if added:
                log.info("[write_output] Added %d ScriptableObject ModuleScripts", added)

    def _subphase_cohere_scripts(self) -> None:
        """Post-transpile script coherence: rewrite asset references, inject
        cross-script ``require()`` calls, and reclassify Script→ModuleScript
        based on require dependencies."""
        # Post-transpilation: rewrite asset references in scripts.
        # ``rewrite_asset_references`` is on the generic scene-runtime
        # contract allowlist (see docs/design/scene-runtime-contract.md),
        # so it runs unconditionally in BOTH modes.
        from converter.script_asset_rewriter import rewrite_asset_references
        rewrites = rewrite_asset_references(
            self.state.rbx_place.scripts,
            self.ctx.uploaded_assets,
            self.state.guid_index,
        )
        if rewrites:
            log.info("[write_output] Rewrote asset references in %d scripts", rewrites)

        # The remaining passes — cross-script require injection and the
        # require-reclassification / coherence packs (``run_packs``) — are
        # LEGACY repair passes. The scene-runtime contract is an allowlist:
        # under generic mode ALL legacy repair passes are OFF (see
        # docs/design/scene-runtime-contract.md:151-169, which names
        # ``script_coherence_packs`` and ``fix_require_classifications``
        # explicitly). Generic resolves requires via the contract
        # pipeline's own ``resolve_requires``; running the legacy packs
        # in generic injects dead ``script.Parent``-based blocks into
        # ModuleScripts (e.g. the door-tween pack on the Door module).
        if self.ctx.scene_runtime_mode == "generic":
            log.info(
                "[write_output] Skipping legacy require-injection and "
                "coherence packs in generic scene-runtime mode "
                "(contract allowlist; requires resolved by contract pipeline)"
            )
            return

        # Inject require() calls for cross-script class dependencies.
        if self.state.dependency_map and self.state.rbx_place.scripts:
            from converter.script_coherence import inject_require_calls
            injected = inject_require_calls(
                self.state.rbx_place.scripts,
                self.state.dependency_map,
            )
            if injected:
                log.info("[write_output] Injected %d cross-script require() calls", injected)
                # Re-write .luau files to disk with injected requires
                scripts_dir = self.output_dir / "scripts"
                for s in self.state.rbx_place.scripts:
                    luau_path = scripts_dir / f"{s.name}.luau"
                    if luau_path.exists():
                        luau_path.write_text(s.source, encoding="utf-8")

        # Post-transpilation: fix script types based on cross-script dependencies.
        from converter.script_coherence import fix_require_classifications
        fixes = fix_require_classifications(self.state.rbx_place.scripts)
        if fixes:
            log.info("[write_output] Reclassified %d scripts based on require() dependencies", fixes)

    # Default container fallbacks for ``_classify_late_appended_scripts``.
    # Mirrors the rbxlx_writer's script_type-based fallback (see
    # ``roblox/rbxlx_writer.py`` :1620-1632) so this safety-net stamps
    # the same container the serializer would have implicitly routed to.
    _LATE_APPEND_DEFAULT_PARENT: dict[str, str] = {
        "LocalScript": "StarterPlayer.StarterPlayerScripts",
        "ModuleScript": "ReplicatedStorage",
        "Script": "ServerScriptService",
    }

    def _classify_late_appended_scripts(self) -> None:
        """Phase 2a slice 8 commit 5 — Option (b) safety-net classify pass.

        Stamps an explicit ``parent_path`` on any script in
        ``rbx_place.scripts`` whose generator left the field as ``None``.
        Mirrors the ``script_type`` -> container fallback the rbxlx
        writer applies for unrouted scripts so this pass is byte-
        equivalent to today's behavior: late-appended scripts
        (GameServerManager / CollisionGroupSetup / CollisionFidelityRecook
        / NavAgent / EventSystem / CharacterBridge / ObjectPool /
        CinemachineRuntime / ClientBootstrap, …) get the SAME container
        they had under the implicit-default routing today, just
        explicitly stamped.

        The point of this pass: make the implicit "rbxlx_writer default
        wins for unrouted scripts" contract VISIBLE in the data model.
        After this pass every script in ``rbx_place.scripts`` carries an
        explicit ``parent_path`` — no script is silently routed by a
        fallback during serialization. This:

        - Pins late-appended scripts' container at slice-8 boundary so
          a later refactor of the rbxlx writer can't drift their routing.
        - Lets golden-output tests assert ZERO ``parent_path`` drift on
          autogen / runtime-injection scripts after the lift (acceptance
          gate from the design doc).
        - Documents that Option (a) — moving autogen-script construction
          earlier so they go through the full classifier — is the
          long-term direction; (b) is the small, low-risk safety net
          shipped first.

        Idempotent: scripts that already carry an explicit ``parent_path``
        (the SceneRuntime* entrypoints and the SceneRuntimePlan module,
        plus any rehydrated script with a plan entry) pass through
        untouched.

        Why not run the full ``_classify_storage`` again? Two reasons:
        (1) the topology apply pass mutates Anim_* placement and writes
        the on-disk plan; rerunning it post-injection would double-apply
        topology + rewrite the plan to include autogen names, neither
        of which is desired for a "freeze defaults" pass. (2) Running
        the full classifier over the augmented set could re-route the
        runtime ModuleScripts (NavAgent, EventSystem, …) through the
        topology decision tree, but they aren't in topology
        ``script_id_by_name`` (added post-prepass) so the topology path
        falls back to legacy per-script anyway. Stamping the rbxlx
        writer default explicitly captures the same observable behavior
        with no cross-script side effects.
        """
        if self.state.rbx_place is None or not self.state.rbx_place.scripts:
            return

        stamped = 0
        for s in self.state.rbx_place.scripts:
            if getattr(s, "parent_path", None):
                continue
            fallback = self._LATE_APPEND_DEFAULT_PARENT.get(s.script_type)
            if fallback is None:
                # Unknown script_type — let rbxlx_writer's fallback
                # decide (it has the same Script default we'd pick).
                continue
            s.parent_path = fallback
            stamped += 1

        if stamped:
            log.info(
                "[write_output] Late-append classify stamped explicit "
                "parent_path on %d script(s) (Option (b) safety net)",
                stamped,
            )

    def _subphase_inject_autogen_scripts(self) -> None:
        """Synthesize project-bootstrap scripts: collision-group setup,
        GameServerManager spawn handling, ClientBootstrap that requires
        side-effect ModuleScripts, and FPS controller scripts/HUD."""
        # Run the FPS heuristic against USER scripts only — before the
        # autogen GameServerManager (which contains both ``PlayerShoot``
        # and ``RemoteEvent`` to wire up its generic spawn flow) lands
        # in ``place.scripts``. Otherwise ``detect_fps_game`` matches
        # the converter's own autogen and the soft hint fires on every
        # non-FPS conversion.
        from converter.scaffolding.fps import detect_fps_game
        looks_fps = detect_fps_game(self.state.rbx_place)

        # Backward-compat migration: an output directory created before
        # ``ConversionContext.scaffolding`` existed rehydrates with an
        # empty list, so a publish/upload re-run would silently drop
        # the FPS scripts the original conversion auto-injected.
        #
        # Three required signals:
        #   1. ``self.scaffolding`` is empty (no explicit opt-in this run)
        #   2. ``self._fps_artifacts_at_init`` — pre-existing FPS auto-gen
        #      scripts were on disk at init time (cached because
        #      ``emit_scripts_to_disk`` may have wiped ``scripts/`` by
        #      the time this subphase runs).
        #   3. ``self._is_resume`` — the persisted ctx's unity project
        #      matches this Pipeline's, so the on-disk scripts belong
        #      to a TRUE resume, not a fresh convert into a dir that
        #      happens to hold leftover FPS scripts from another project.
        if (
            not self.scaffolding
            and self._fps_artifacts_at_init
            and self._is_resume
        ):
            log.warning(
                "[write_output] Migrating pre-scaffolding output dir: "
                "found previously-emitted FPS scripts on disk and no "
                "explicit scaffolding was persisted. Inferring "
                "scaffolding=['fps'] to preserve auto-injected FPS "
                "controller/HUD. Pin this with --scaffolding=fps on "
                "future runs to make it explicit."
            )
            self.apply_scaffolding(["fps"])

        # Auto-generate collision group setup if Unity layers are used.
        from converter.autogen import generate_collision_group_script
        has_layers = False
        def _check_layers(parts):
            nonlocal has_layers
            for p in parts:
                if getattr(p, "attributes", {}).get("UnityLayer"):
                    has_layers = True
                    return
                for child in (getattr(p, "children", None) or []):
                    _check_layers([child])
                    if has_layers:
                        return
        _check_layers(self.state.rbx_place.workspace_parts or [])
        if has_layers:
            self.state.rbx_place.scripts.append(generate_collision_group_script())
            log.info("[write_output] Injected CollisionGroupSetup script")

        # Auto-generate game server manager (spawn system, player init).
        from converter.autogen import generate_game_server_script
        existing_server_mgr = [s for s in self.state.rbx_place.scripts if s.name == "GameServerManager"]
        if not existing_server_mgr:
            self.state.rbx_place.scripts.append(generate_game_server_script())
            log.info("[write_output] Injected GameServerManager script")

        # Auto-generate CollisionFidelityRecook server script when ANY
        # MeshPart in the scene has a non-Default ``collision_fidelity``.
        # The rbxlx_writer attaches a ``_DesiredCollisionFidelity``
        # attribute on those parts; the script reads it at game start
        # and recreates the part via CreateMeshPartAsync to actually
        # cook the collision mesh. Without this, locally-loaded rbxlx
        # files leave Hull/PreciseConvexDecomposition parts with Box
        # collision (Roblox doesn't re-cook on property assignment),
        # producing invisible bounding-box blockers behind hollow
        # shapes like door frames.
        from converter.autogen import (
            generate_collision_fidelity_recook_script,
        )
        existing_recook = [
            s for s in self.state.rbx_place.scripts
            if s.name == "CollisionFidelityRecook"
        ]
        if not existing_recook and _scene_needs_collision_recook(
            self.state.rbx_place.workspace_parts or []
        ):
            self.state.rbx_place.scripts.append(
                generate_collision_fidelity_recook_script()
            )
            log.info("[write_output] Injected CollisionFidelityRecook script")

        # Auto-generate the CameraRigFollower client script when the
        # scene contains a Unity main-camera rig. ``scene_converter``
        # tags the converted main-camera Model with ``_MainCameraRig``;
        # this script pivots that Model onto workspace.CurrentCamera each
        # frame so camera-child objects (weapon slots, viewmodels) ride
        # the player's view as they did under the Unity transform tree.
        from converter.autogen import generate_camera_rig_follower_script
        has_camera_rig = False
        def _check_camera_rig(parts):
            nonlocal has_camera_rig
            for p in parts:
                if getattr(p, "attributes", {}).get("_MainCameraRig"):
                    has_camera_rig = True
                    return
                for child in (getattr(p, "children", None) or []):
                    _check_camera_rig([child])
                    if has_camera_rig:
                        return
        _check_camera_rig(self.state.rbx_place.workspace_parts or [])
        existing_rig_follower = [
            s for s in self.state.rbx_place.scripts
            if s.name == "CameraRigFollower"
        ]
        if has_camera_rig and not existing_rig_follower:
            self.state.rbx_place.scripts.append(
                generate_camera_rig_follower_script()
            )
            log.info("[write_output] Injected CameraRigFollower script")

        # Bootstrap: generate a LocalScript that requires ModuleScripts with
        # side-effects (RenderStepped/Heartbeat connections, mouse lock, etc.)
        # These modules need to be required at startup to activate their logic.
        import re as _re
        _side_effect_patterns = [
            r'RenderStepped:Connect',
            r'Heartbeat:Connect',
            r'MouseBehavior\s*=\s*Enum\.MouseBehavior\.LockCenter',
            r'InputBegan:Connect',
        ]
        # Anti-FPS patterns: modules that re-enable the mouse cursor or unlock
        # the mouse at init time clobber the FPS controller's setup. If any
        # script sets MouseBehavior=LockCenter (an FPS controller), exclude
        # such modules from the bootstrap — they should only run when the
        # player explicitly navigates to a menu, not unconditionally on Play.
        _anti_fps_patterns = [
            r'MouseIconEnabled\s*=\s*true',
            r'MouseBehavior\s*=\s*Enum\.MouseBehavior\.Default',
        ]
        # An FPS controller will lock the mouse via
        # ``MouseBehavior.LockCenter``. If any existing script already
        # does that, we filter anti-FPS modules. ``--scaffolding=fps``
        # ALSO injects an FPS controller later in this same subphase
        # (``inject_fps_scripts`` runs after this filter), so honour
        # the opt-in here too — otherwise a side-effect module that
        # sets ``MouseBehavior.Default`` slips through and clobbers
        # the soon-to-be-injected controller's mouse lock at runtime.
        has_fps_controller = (
            "fps" in self.scaffolding
            or any(
                _re.search(r'MouseBehavior\s*=\s*Enum\.MouseBehavior\.LockCenter', s.source)
                for s in self.state.rbx_place.scripts
            )
        )
        # Generic-runtime mode: PR4's host runtime owns the lifecycle of
        # every runtime-bearing MonoBehaviour. If the legacy bootstrap
        # also requires them, their top-level code fires once via the
        # bootstrap and then ``host.require`` instantiates them again —
        # double-loading the module AND violating the contract's
        # "no top-level side effects" rule (the AI emitted side-effect-
        # free class tables; only the host knows how to wire them).
        # Under legacy mode ``runtime_bearing_stems`` is empty and this
        # filter is a no-op, preserving the byte-identical legacy emit
        # invariant.
        runtime_bearing_stems: set[str] = set()
        if self.ctx.scene_runtime_mode == "generic":
            sr_modules_obj = (self.ctx.scene_runtime or {}).get("modules", {})
            if isinstance(sr_modules_obj, dict):
                for module in sr_modules_obj.values():
                    if not isinstance(module, dict):
                        continue
                    if not module.get("runtime_bearing"):
                        continue
                    stem = module.get("stem")
                    if isinstance(stem, str) and stem:
                        runtime_bearing_stems.add(stem)
        side_effect_modules = []
        for s in self.state.rbx_place.scripts:
            if s.script_type != "ModuleScript":
                continue
            if not any(_re.search(p, s.source) for p in _side_effect_patterns):
                continue
            if s.name in runtime_bearing_stems:
                log.info(
                    "[write_output] Skipping bootstrap require of '%s' "
                    "(runtime-bearing — host runtime owns lifecycle)",
                    s.name,
                )
                continue
            if has_fps_controller and any(
                _re.search(p, s.source) for p in _anti_fps_patterns
            ):
                log.info(
                    "[write_output] Skipping bootstrap require of '%s' "
                    "(would clobber FPS controller mouse state)",
                    s.name,
                )
                continue
            side_effect_modules.append(s.name)

        if side_effect_modules:
            bootstrap_lines = ['-- Auto-generated bootstrap: require modules with side-effects']
            bootstrap_lines.append('local RS = game:GetService("ReplicatedStorage")')
            bootstrap_lines.append('')
            # If any module uses Scriptable camera (FPS-style), set it up before requiring
            has_camera_control = any(
                'camera.CFrame' in s.source or 'CurrentCamera' in s.source
                for s in self.state.rbx_place.scripts
                if s.name in side_effect_modules
            )
            if has_camera_control:
                bootstrap_lines.append('-- Set camera to Scriptable so game scripts can control it')
                bootstrap_lines.append('local camera = workspace.CurrentCamera')
                bootstrap_lines.append('camera.CameraType = Enum.CameraType.Scriptable')
                bootstrap_lines.append('')
                # First-person body/accessory hiding + spawn floor-snap live in
                # script_coherence._disable_default_controls_in_fps_scripts so
                # they ride along with the FPS LocalScript itself rather than
                # the bootstrap. The bootstrap's `has_camera_control` only
                # inspects `side_effect_modules`, which excludes FPS LocalScripts,
                # so any logic placed here would have shipped dead.
            for i, mod in enumerate(side_effect_modules):
                var = f'mod{i}'
                bootstrap_lines.append(f'local {var} = RS:WaitForChild("{mod}", 10)')
                bootstrap_lines.append(f'if {var} then')
                bootstrap_lines.append(f'    local ok{i}, err{i} = pcall(require, {var})')
                bootstrap_lines.append(f'    if not ok{i} then warn("[Bootstrap] {mod}: " .. tostring(err{i})) end')
                bootstrap_lines.append(f'end')
                bootstrap_lines.append('')
            self.state.rbx_place.scripts.append(RbxScript(
                name="ClientBootstrap",
                source="\n".join(bootstrap_lines),
                script_type="LocalScript",
            ))
            log.info("[write_output] Bootstrap LocalScript requires %d side-effect modules: %s",
                     len(side_effect_modules), ", ".join(side_effect_modules))

        # FPS scaffolding is opt-in — pass ``--scaffolding=fps`` to
        # request the auto-generated FPS client controller, HUD
        # ScreenGui, and HUDController LocalScript. Default behaviour
        # is no game-genre assumptions: non-FPS projects (Gamekit3D,
        # BoatAttack, ChopChop, RedRunner) get a clean conversion
        # without unwanted UI/input scripts injected.
        #
        # ``looks_fps`` was computed above against the user-scripts-only
        # snapshot, so the soft hint (in the else branch) doesn't fire
        # on every conversion just because the autogen GameServerManager
        # mentions ``PlayerShoot`` + ``RemoteEvent``.
        # ``is_fps_game`` drives FPS-related scene flags downstream
        # (e.g. ``StarterPlayer.CameraMode = LockFirstPerson`` in the
        # rbxlx writer). Set it whenever EITHER the heuristic matched
        # user content OR the caller explicitly opted into FPS
        # scaffolding — the user-or-heuristic disjunction matches the
        # pre-refactor behaviour for projects that ship their own
        # controller, AND respects ``--scaffolding=fps`` runs whose
        # user scripts don't trip the heuristic. Tying this to
        # injection alone regresses both cases (explicit opt-in
        # without heuristic match, and projects with their own
        # controller that just need the camera flag).
        if looks_fps or "fps" in self.scaffolding:
            self.state.rbx_place.is_fps_game = True

        if "fps" in self.scaffolding:
            from converter.scaffolding.fps import inject_fps_scripts
            fps_added = inject_fps_scripts(self.state.rbx_place)
            if fps_added:
                log.info(
                    "[write_output] Auto-generated %d FPS client scripts/GUIs "
                    "(--scaffolding=fps)", fps_added,
                )
        else:
            # Opt-out cleanup: remove auto-gen FPS scripts that may
            # have been rehydrated from a prior --scaffolding=fps run.
            # Without this, the rehydrate would silently carry the
            # last run's HUDController/FPSController forward even
            # after the user toggled the flag off.
            removed = self._remove_rehydrated_fps_autogen()
            if removed:
                log.info(
                    "[write_output] Removed %d rehydrated FPS auto-gen "
                    "script(s) — current run did not pass "
                    "--scaffolding=fps",
                    removed,
                )
            if looks_fps:
                log.info(
                    "[write_output] Heuristic detected FPS-style scripts; "
                    "skipping auto-injected FPS controller/HUD. Pass "
                    "--scaffolding=fps to opt in."
                )

    def _remove_rehydrated_fps_autogen(self) -> int:
        """Drop FPS-only auto-gen scripts and the HUD ScreenGui that
        were rehydrated from a prior ``--scaffolding=fps`` run.

        Called from ``_subphase_inject_autogen_scripts`` on the
        opt-out branch — the user toggled FPS off but the rehydrate
        loaded last run's auto-gen files. Pruning here makes the
        opt-out effective without breaking the review flow's
        general edit-preservation contract (other auto-gen scripts
        — GameServerManager, CollisionGroupSetup, etc. — stay).

        Marker-based, name-aware: matches the FPS-specific header
        comments AND the canonical names so user-authored files of
        the same name (without the marker) are left alone.
        """
        if self.state.rbx_place is None:
            return 0
        fps_markers = (
            "-- HUD Controller (auto-generated)",
            "-- FPS Client Controller (auto-generated)",
        )
        # Recognised FPS auto-gen script names across pipeline eras:
        #   - ``AutoFpsHudController``: post-rename HUD listener.
        #   - ``HUDController``: pre-rename HUD listener (legacy).
        #   - ``FPSController``: actual emitted controller (caps).
        #   - ``FpsClient``: alternate legacy controller name in
        #     ``_fps_artifacts_on_disk`` migration list — kept here
        #     so opt-out reruns prune that filename too if a prior
        #     conversion happened to write it.
        fps_names = {
            "AutoFpsHudController", "FPSController", "HUDController",
            "FpsClient",
        }
        original = self.state.rbx_place.scripts
        kept = [
            s for s in original
            if not (
                s.name in fps_names
                and any(m in s.source[:512] for m in fps_markers)
            )
        ]
        removed_scripts = len(original) - len(kept)
        # PR #75 codex round preempt: also delete pruned scripts from
        # disk so the next resume's rehydrate doesn't resurrect them
        # (matching PR #74 round-10 [P2] behaviour for the adapter-
        # mode pre-pass). The opt-out branch doesn't carry the
        # adapter-mode injected counter; the disk delete is purely
        # cosmetic for the rehydrate invariant.
        for s in original:
            if s not in kept:
                self._delete_pruned_script_from_disk(s)
        self.state.rbx_place.scripts = kept

        # The FPS HUD ScreenGui is identified by a marker attribute
        # (``_AutoFpsHud``) the generator stamps on it, NOT by its
        # name. A user-authored ScreenGui named ``HUD`` (e.g. from
        # Canvas/UI conversion) doesn't carry the marker and is
        # preserved through opt-out runs.
        original_guis = self.state.rbx_place.screen_guis
        kept_guis = [
            sg for sg in original_guis
            if not (
                sg.name == "HUD"
                and getattr(sg, "attributes", {}).get("_AutoFpsHud")
            )
        ]
        removed_guis = len(original_guis) - len(kept_guis)
        self.state.rbx_place.screen_guis = kept_guis
        return removed_scripts + removed_guis

    def _subphase_encode_terrain(self) -> None:
        """Encode each terrain's heightmap into Roblox SmoothGrid binary and
        register a FillBlock Luau body for headless publish."""
        # Encode terrain heightmap data into SmoothGrid binary for rbxlx embedding.
        # Also save a Luau script as fallback for environments without UnityPy.
        if self.state.rbx_place.terrains:
            from converter.terrain_converter import read_unity_terrain, generate_terrain_luau
            for terrain_obj in self.state.rbx_place.terrains:
                guid = terrain_obj.terrain_data_guid
                if not guid:
                    log.warning("[write_output] Terrain heightmap missing: terrain_data_guid is empty. "
                                "Place will have an empty Terrain shell with no SmoothGrid.")
                    continue
                if not self.state.guid_index:
                    log.warning("[write_output] Terrain heightmap missing: GUID index unavailable for %s. "
                                "Place will have an empty Terrain shell with no SmoothGrid.", guid)
                    continue
                td_path = self.state.guid_index.resolve(guid)
                if not td_path:
                    log.warning("[write_output] Terrain heightmap missing: GUID %s did not resolve to any file. "
                                "Place will have an empty Terrain shell with no SmoothGrid.", guid)
                    continue
                if not td_path.exists():
                    log.warning("[write_output] Terrain heightmap missing: %s does not exist on disk. "
                                "Place will have an empty Terrain shell with no SmoothGrid. "
                                "If this file is Git LFS-tracked, run `git lfs pull` to fetch it.",
                                td_path)
                    continue
                # Detect Git LFS pointer files (small text stub starting with the LFS spec line).
                try:
                    head = td_path.read_bytes()[:64]
                except OSError as exc:
                    log.warning("[write_output] Terrain heightmap unreadable at %s: %s. "
                                "Place will have an empty Terrain shell with no SmoothGrid.", td_path, exc)
                    continue
                if head.startswith(b"version https://git-lfs.github.com/spec/v1"):
                    log.warning("[write_output] Terrain heightmap %s is an unfetched Git LFS pointer "
                                "(stub size %d bytes). Place will have an empty Terrain shell with no SmoothGrid. "
                                "Run `git lfs pull` to fetch the actual binary, then re-run conversion.",
                                td_path, td_path.stat().st_size)
                    continue
                terrain_data = read_unity_terrain(td_path)
                if not terrain_data:
                    log.warning("[write_output] Terrain heightmap at %s could not be parsed "
                                "(read_unity_terrain returned None — UnityPy missing or unsupported format). "
                                "Place will have an empty Terrain shell with no SmoothGrid.", td_path)
                    continue
                from core.coordinate_system import unity_to_roblox_pos
                # Use the terrain world offset (includes parent chain)
                # computed during scene conversion, not just local position.
                rpos = unity_to_roblox_pos(*self.state.rbx_place.terrain_world_offset)
                # Encode terrain voxels into rbxlx binary format
                try:
                    from roblox.terrain_encoder import encode_smooth_grid, encode_physics_grid
                    terrain_obj.smooth_grid = encode_smooth_grid(
                        terrain_data["heights"],
                        terrain_data["resolution"],
                        terrain_data["scale"],
                        rpos,
                        layer_names=terrain_data.get("layers"),
                        splat_alphas=terrain_data.get("splat_alphas"),
                        splat_resolution=terrain_data.get("splat_resolution", 0),
                    )
                    terrain_obj.physics_grid = encode_physics_grid()
                    log.info("[write_output] Terrain SmoothGrid encoded for rbxlx embedding")
                except Exception as exc:
                    log.warning("[write_output] Failed to encode terrain SmoothGrid: %s", exc)
                # Save terrain FillBlock script as a standalone file (for inspection)
                # AND register the body for headless publish consumption. The Open
                # Cloud Luau Execution API cannot set the SmoothGrid BinaryString,
                # so the headless place builder needs the FillBlock fallback.
                #
                # Crucially: the FillBlock body is NOT added to place.scripts. If
                # it were, every Studio open would run a server script that begins
                # with `t:Clear()` followed by ~9000 voxel_size=16 FillBlocks —
                # wiping the high-fidelity SmoothGrid and replacing it with the
                # coarse fallback. We instead store it on
                # ``place.headless_terrain_scripts`` (a separate list) which the
                # luau_place_builder reads but the rbxlx writer ignores. Multiple
                # terrains contribute multiple entries (preserving all of them
                # during headless bake — the previous single-named-script design
                # silently dropped terrains 2+).
                luau = generate_terrain_luau(terrain_data, rpos, voxel_size=16)
                terrain_path = self.output_dir / f"generate_terrain_{len(self.state.rbx_place.headless_terrain_scripts) + 1}.luau"
                terrain_path.write_text(luau, encoding="utf-8")
                log.info("[write_output] Terrain script saved to %s (%d chars)",
                         terrain_path.name, len(luau))
                self.state.rbx_place.headless_terrain_scripts.append(luau)

    def _subphase_inject_mesh_loader(self) -> None:
        """Inject the auto-generated MeshLoader Script that calls
        ``CreateMeshPartAsync`` for placeholder MeshParts when mesh
        resolution data is unavailable (i.e. resolve_assets did not run)."""
        # MeshLoader: only inject if mesh resolution data is NOT available.
        # When resolve_assets has run, real MeshIds are already in the rbxlx
        # and no runtime loading is needed. The MeshLoader would actively harm
        # rendering by replacing working meshes with potentially broken ones.
        if self.ctx.uploaded_assets and not self.ctx.mesh_hierarchies:
            mesh_loader = '''-- Auto-generated mesh loader
-- Replaces placeholder MeshParts with proper mesh geometry via CreateMeshPartAsync.
-- Handles both real MeshIds (post-resolution) and Model IDs (pre-resolution).
if script:GetAttribute("MeshesLoaded") then return end

local AssetService = game:GetService("AssetService")
local InsertService = game:GetService("InsertService")
local loaded = 0
local failed = 0

-- Cache: meshIdUrl → {meshId, initialSize} to avoid redundant loads
local meshCache = {}

local function resolveMeshId(url)
    if meshCache[url] then return meshCache[url] end

    local numId = tonumber(url:match("(%d+)"))
    if not numId then return nil end

    -- Try 1: CreateMeshPartAsync directly (works for real MeshIds)
    local ok, mp = pcall(function() return AssetService:CreateMeshPartAsync(url) end)
    if ok and mp then
        local entry = { meshId = url, initialSize = mp.Size, meshPart = mp }
        meshCache[url] = entry
        return entry
    end

    -- Try 2: LoadAsset (works for Model IDs that wrap a MeshPart)
    local ok2, model = pcall(function() return InsertService:LoadAsset(numId) end)
    if ok2 and model then
        for _, desc in model:GetDescendants() do
            if desc:IsA("MeshPart") and desc.MeshId ~= "" then
                local realId = desc.MeshId
                local entry = { meshId = realId, initialSize = desc.Size }
                meshCache[url] = entry
                model:Destroy()
                return entry
            end
        end
        model:Destroy()
    end

    meshCache[url] = false
    return nil
end

-- Collect parts to process (snapshot list to avoid mutation during iteration)
local partsToProcess = {}
for _, part in workspace:GetDescendants() do
    if part:IsA("MeshPart") and part:GetAttribute("_MeshId") then
        table.insert(partsToProcess, part)
    end
end

print(string.format("MeshLoader: processing %d MeshParts", #partsToProcess))

for _, part in partsToProcess do
    if not part.Parent then continue end
    local meshUrl = part:GetAttribute("_MeshId")
    local resolved = resolveMeshId(meshUrl)
    if not resolved then failed = failed + 1; continue end

    -- Create the mesh part (reuse cached meshPart if first use, else create new)
    local newPart
    if resolved.meshPart then
        newPart = resolved.meshPart
        resolved.meshPart = nil  -- only reuse once
    else
        local ok, mp = pcall(function() return AssetService:CreateMeshPartAsync(resolved.meshId) end)
        if not ok then failed = failed + 1; continue end
        newPart = mp
    end

    newPart.Name = part.Name
    newPart.CFrame = part.CFrame
    newPart.Anchored = part.Anchored
    newPart.CanCollide = part.CanCollide
    newPart.Color = part.Color
    newPart.Material = part.Material
    newPart.Transparency = part.Transparency
    newPart.CastShadow = part.CastShadow

    -- Compute proper size using stored scale attributes
    local scaleX = part:GetAttribute("_ScaleX")
    local scaleY = part:GetAttribute("_ScaleY")
    local scaleZ = part:GetAttribute("_ScaleZ")
    if scaleX and scaleY and scaleZ then
        local init = resolved.initialSize
        newPart.Size = Vector3.new(
            init.X * scaleX,
            init.Y * scaleY,
            init.Z * scaleZ
        )
    else
        newPart.Size = part.Size
    end

    -- Copy non-internal attributes
    for name, value in pairs(part:GetAttributes()) do
        if string.sub(name, 1, 1) ~= "_" then
            newPart:SetAttribute(name, value)
        end
    end

    -- Reparent all children (SurfaceAppearance, scripts, etc.)
    for _, child in part:GetChildren() do
        pcall(function() child.Parent = newPart end)
    end

    newPart.Parent = part.Parent
    part:Destroy()
    loaded = loaded + 1

    if loaded % 20 == 0 then task.wait() end
end

print(string.format("MeshLoader: %d loaded, %d failed", loaded, failed))
script:SetAttribute("MeshesLoaded", true)
script.Disabled = true
'''
            self.state.rbx_place.scripts.append(RbxScript(
                name="MeshLoader",
                source=mesh_loader,
                script_type="Script",
            ))
            log.info("[write_output] MeshLoader script embedded for %d mesh assets",
                     sum(1 for p in self.ctx.uploaded_assets if Path(p).suffix.lower() in ('.fbx', '.obj')))

    def _subphase_patch_setup_sounds(self) -> None:
        """Patch Player-style scripts that call ``setupSounds`` to also search
        the bound Part's children for Sound instances."""
        # Patch scripts that use setupSounds: also search script.Parent for
        # Sound children (sounds from MonoBehaviour AudioClip fields are placed
        # as children of the bound Part, not the character).
        for s in self.state.rbx_place.scripts:
            if "setupSounds" in s.source and "script.Parent" not in s.source:
                s.source = s.source.replace(
                    "setupSounds(character)",
                    "setupSounds(character)\n    -- Also search bound Part for sounds from MonoBehaviour fields\n    if script.Parent and script.Parent:IsA(\"BasePart\") then\n        setupSounds(script.Parent)\n    end",
                )

    def _collect_all_scripts(self) -> list:
        """Return every script reachable from ``rbx_place`` for
        semantic validation. Walks the flat list AND scripts under
        ``workspace_parts`` / ``replicated_templates`` so part-bound
        MonoBehaviour scripts get validated too.

        Deduplicated by ``(name, source)`` rather than ``id()`` —
        ``_attach_monobehaviour_scripts_to_templates`` and the
        prefab-scoped animation copy passes clone one Luau body onto
        many parts/templates. Without source-level dedup, the same
        warning would get reported once per scene instance instead of
        once per logical script. The on-disk finalize step still
        flushes by ``id()`` (its concern is "every distinct object's
        source has been persisted"), so the two walks intentionally
        differ.
        """
        seen_keys: set[tuple] = set()
        out: list = []

        def _push(s: object) -> None:
            name = getattr(s, "name", "") or ""
            source = getattr(s, "source", "") or ""
            key = (name, source)
            if key in seen_keys:
                return
            seen_keys.add(key)
            out.append(s)

        for s in self.state.rbx_place.scripts or []:
            _push(s)

        def _walk(parts) -> None:
            for part in parts or []:
                for s in getattr(part, "scripts", None) or []:
                    _push(s)
                _walk(getattr(part, "children", None))

        _walk(getattr(self.state.rbx_place, "workspace_parts", None))
        _walk(getattr(self.state.rbx_place, "replicated_templates", None))
        _walk(getattr(self.state.rbx_place, "server_storage_parts", None))
        return out

    def _subphase_finalize_scripts_to_disk(self) -> None:
        """Write every script's final source back to disk. Runs after every
        in-memory mutation so the on-disk ``scripts/`` tree mirrors what
        gets serialized into the rbxlx.

        PR #74 codex round-5 [P3]: walks part-bound scripts too (not
        just ``rbx_place.scripts``). The rehydration prune pass and
        any future post-binding mutation can change the source on a
        bound-script clone WITHOUT touching its global counterpart,
        so a global-only walk would let the on-disk ``scripts/*.luau``
        cache drift from the in-memory state. Per-script identity
        dedup keeps the work O(scripts), not O(scripts × parts).
        """
        # Final write: ensure .luau files on disk match the fully processed
        # sources (after require injection, reclassification, and all other
        # post-processing). Prefer the explicit source_path set by rehydration
        # and the fresh-write branches so nested-dir scripts
        # (animations/, animation_data/, scriptable_objects/) round-trip back
        # to their original location. Fall back to the top-level/animations
        # heuristic only for scripts injected in-memory later in write_output
        # (bootstrap, FPS controller, runtime libs) that never had a disk
        # path to begin with.
        scripts_dir = self.output_dir / "scripts"

        def _flush(s: object) -> None:
            source = getattr(s, "source", None)
            if source is None:
                return
            source_path = getattr(s, "source_path", None)
            if source_path:
                out_path = scripts_dir / source_path
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(source, encoding="utf-8")
                return
            name = getattr(s, "name", None)
            if not name:
                return
            luau_path = scripts_dir / f"{name}.luau"
            anim_path = scripts_dir / "animations" / f"{name}.luau"
            if anim_path.exists():
                anim_path.write_text(source, encoding="utf-8")
            elif luau_path.exists() or not (scripts_dir / "animations").exists():
                luau_path.write_text(source, encoding="utf-8")

        # Identity-dedup so a script that's both in the global list
        # AND bound to a part (the first-bind shares the RbxScript
        # ref via ``part.scripts.append(script)``) doesn't write
        # twice. ``id()`` keys handle the dataclass eq=True case too.
        seen: set[int] = set()

        def _flush_unique(s: object) -> None:
            key = id(s)
            if key in seen:
                return
            seen.add(key)
            _flush(s)

        for s in self.state.rbx_place.scripts:
            _flush_unique(s)

        def _walk(parts: list) -> None:
            for part in parts:
                for s in getattr(part, "scripts", None) or []:
                    _flush_unique(s)
                children = getattr(part, "children", None)
                if children:
                    _walk(children)

        _walk(getattr(self.state.rbx_place, "workspace_parts", None) or [])
        _walk(getattr(self.state.rbx_place, "replicated_templates", None) or [])


    def _write_unconverted_md(self) -> None:
        """Aggregate ``unconverted`` entries from result objects into
        ``UNCONVERTED.md``. When nothing is unconverted, the file is
        removed so stale state from prior runs doesn't linger.
        """
        from config import UNCONVERTED_FILENAME

        sections: dict[str, list[dict[str, str]]] = {}
        if self.state.animation_result is not None:
            entries = getattr(self.state.animation_result, "unconverted", None) or []
            for entry in entries:
                category = entry.get("category", "misc")
                sections.setdefault(category, []).append(entry)

        if self.state.transpilation_result is not None:
            for entry in getattr(self.state.transpilation_result, "shared_state_warnings", []) or []:
                category = entry.get("category", "shared_state")
                sections.setdefault(category, []).append(entry)

        # Material warnings surface the "drop" side of the mapper —
        # unsupported shaders, specular-workflow approximations, AO
        # skips, missing LFS textures. Each warning becomes an entry
        # keyed by the material name.
        for guid, mapping in (self.state.material_mappings or {}).items():
            for warning in getattr(mapping, "warnings", []) or []:
                sections.setdefault("material", []).append({
                    "category": "material",
                    "item": getattr(mapping, "material_name", guid),
                    "reason": warning,
                })

        # Scene conversion records dropped Unity component types —
        # _SKIP_TYPES (no Roblox equivalent) plus unhandled types with no
        # converter mapping. Each entry already carries its own category.
        if self.state.rbx_place is not None:
            for entry in getattr(self.state.rbx_place, "unconverted_components", None) or []:
                category = entry.get("category", "component")
                sections.setdefault(category, []).append(entry)

        # R5-P1 fix: the scene-runtime cross-domain edge block is a
        # cross-domain artefact, not a category in ``sections``. Pre-R5
        # ``_subphase_inject_scene_runtime`` wrote it directly mid-
        # pipeline, but this writer runs LATER and rewrote the file from
        # scratch -- silently clobbering the cross-domain block on every
        # rerun. The fix: ``_subphase_inject_scene_runtime`` stages the
        # edges on ``ctx.scene_runtime["cross_domain_edges"]`` only, and
        # the final UNCONVERTED.md write happens here -- the single
        # source of truth for the file's contents.
        cross_domain_edges = []
        scene_runtime_ctx = self.ctx.scene_runtime or {}
        if isinstance(scene_runtime_ctx, dict):
            raw_edges = scene_runtime_ctx.get("cross_domain_edges") or []
            if isinstance(raw_edges, list):
                cross_domain_edges = [dict(e) for e in raw_edges if isinstance(e, dict)]

        out_path = self.output_dir / UNCONVERTED_FILENAME
        if not sections and not cross_domain_edges:
            if out_path.exists():
                out_path.unlink()
            return

        lines: list[str] = []
        if sections:
            lines.extend([
                "# UNCONVERTED",
                "",
                "Features dropped from this specific conversion run. Each "
                "bullet had no in-policy Roblox equivalent, or required "
                "source data the converter cannot parse yet. For the static "
                "catalog of known gaps see `docs/UNSUPPORTED.md`; for roadmap "
                "items see `TODO.md`.",
                "",
            ])
            for category in sorted(sections):
                lines.append(f"## {category}")
                lines.append("")
                for entry in sections[category]:
                    item = entry.get("item", "?")
                    reason = entry.get("reason", "")
                    lines.append(f"- `{item}` — {reason}")
                lines.append("")
        elif cross_domain_edges:
            # No standard unconverted entries, but cross-domain edges
            # still need a file. Emit a minimal header so the cross-
            # domain block has context.
            lines.extend([
                "# UNCONVERTED",
                "",
                "Cross-domain references the host runtime injects ``nil`` "
                "for at start. See the table below.",
                "",
            ])

        if cross_domain_edges:
            from converter.autogen import render_cross_domain_report
            report_md = render_cross_domain_report(cross_domain_edges)
            if report_md:
                # render_cross_domain_report already terminates with a
                # newline; splitlines() drops the trailing blank so the
                # join doesn't double-newline.
                lines.extend(report_md.rstrip("\n").split("\n"))
                lines.append("")

        out_path.write_text("\n".join(lines), encoding="utf-8")
        log.info(
            "[write_output] UNCONVERTED.md written "
            "(%d entries across %d categories, %d cross-domain edges)",
            sum(len(v) for v in sections.values()), len(sections),
            len(cross_domain_edges),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_method_warnings(self) -> list[str]:
        """Pull method-completeness warnings off transpiled scripts.

        ``code_transpiler`` tags each AI-transpiled script's warnings
        with a leading ``[<filename>]`` when method-completeness finds
        a drop. Collect those here so the conversion report surfaces
        them without the caller having to walk scripts themselves.
        """
        tr = self.state.transpilation_result
        if tr is None:
            return []
        warnings: list[str] = []
        for script in getattr(tr, "scripts", []):
            for w in getattr(script, "warnings", []) or []:
                if "missing from Luau output" in w:
                    warnings.append(w)
        return warnings

    def _build_script_summary(self) -> "ScriptSummary":
        """Project the live ``TranspilationResult`` onto ``ScriptSummary``.

        ``ScriptSummary`` exposes ai/flagged/skipped counts and the list
        of flagged script names — all derivable from ``state.transpilation_result``,
        not from the bare ``ctx.transpiled_scripts`` count. Keeping this
        single mapping means the report and the live result can't drift.
        """
        from converter.report_generator import ScriptSummary
        tr = self.state.transpilation_result
        if tr is None:
            total = self.ctx.transpiled_scripts
            return ScriptSummary(
                total=total,
                succeeded=total,
                method_completeness_warnings=self._collect_method_warnings(),
            )
        flagged_scripts = [
            Path(s.source_path).name
            for s in tr.scripts
            if s.flagged_for_review
        ]
        return ScriptSummary(
            total=tr.total_transpiled,
            succeeded=tr.total_transpiled - tr.total_failed,
            flagged_for_review=tr.total_flagged,
            skipped=tr.total_failed,
            ai_transpiled=tr.total_ai,
            flagged_scripts=flagged_scripts,
            method_completeness_warnings=self._collect_method_warnings(),
        )

    def _build_conversion_report(
        self, rbxlx_path: Path, result: dict, report_path: Path
    ) -> "ConversionReport":
        """Assemble the structured ConversionReport for write_output."""
        from converter.report_generator import (
            ConversionReport, AssetSummary, MaterialSummary,
            ComponentSummary, SceneSummary, OutputSummary,
        )
        script_types = {"Script": 0, "LocalScript": 0, "ModuleScript": 0}
        for s in (self.state.rbx_place.scripts or []):
            st = getattr(s, "script_type", "Script")
            script_types[st] = script_types.get(st, 0) + 1

        selected_scene = ""
        if self.ctx.selected_scene:
            p = Path(self.ctx.selected_scene)
            if p.is_absolute():
                try:
                    selected_scene = str(p.relative_to(self.unity_project_path))
                except ValueError:
                    selected_scene = p.name
            else:
                selected_scene = str(p)

        from converter.report_generator import (
            SemanticIssueSummary, SemanticWarningsSummary,
        )
        semantic_summary = SemanticWarningsSummary()
        sr = getattr(self.state, "semantic_report", None)
        if sr is not None:
            issues = getattr(sr, "issues", None) or []
            counts = getattr(sr, "counts_by_rule", {}) or {}
            semantic_summary = SemanticWarningsSummary(
                total=len(issues),
                counts_by_rule=dict(counts),
                issues=[
                    SemanticIssueSummary(
                        severity=i.severity,
                        rule=i.rule,
                        script=i.script,
                        line=i.line,
                        snippet=i.snippet,
                        explanation=i.explanation,
                        suggested_fix=i.suggested_fix,
                        confidence=i.confidence,
                    )
                    for i in issues
                ],
            )

        return ConversionReport(
            unity_project_path=str(self.unity_project_path),
            output_dir=str(self.output_dir),
            success=len(self.ctx.errors) == 0,
            errors=list(self.ctx.errors),
            warnings=list(self.ctx.warnings),
            assets=AssetSummary(
                total=len(self.ctx.uploaded_assets),
                by_kind={**script_types, "upload_errors": len(self.ctx.asset_upload_errors)},
            ),
            scripts=self._build_script_summary(),
            materials=MaterialSummary(
                total=self.ctx.total_materials,
                fully_converted=self.ctx.converted_materials,
            ),
            scene=SceneSummary(
                selected_scene=selected_scene,
                total_game_objects=self.ctx.total_game_objects,
            ),
            components=ComponentSummary(converted=self.ctx.converted_parts),
            output=OutputSummary(
                rbxl_path=str(rbxlx_path),
                parts_written=result.get("parts_written", 0),
                scripts_in_place=result.get("scripts_written", 0),
                report_path=str(report_path),
            ),
            semantic_warnings=semantic_summary,
        )

    # Marker substrings that identify converter-emitted scripts.
    # Used by ``detect_fps_game`` to skip auto-gen files (the
    # GameServerManager mentions ``PlayerShoot`` + ``RemoteEvent``
    # to wire up its generic spawn flow, so unfiltered detection
    # false-positives every conversion). User edits to auto-gen
    # scripts still come through rehydrate as user-authored content;
    # only the heuristic skips them.
    _AUTOGEN_MARKERS: tuple[str, ...] = (
        "-- HUD Controller (auto-generated)",
        "-- FPS Client Controller (auto-generated)",
        "-- CollisionFidelityRecook (auto-generated)",
        "-- CollisionGroup Setup (auto-generated from Unity layers)",
        "-- Game Server Manager (auto-generated by Unity converter)",
        "-- Camera Rig Follower (auto-generated by Unity converter)",
        "-- EventDispatch: cross-class connect helper",
        "-- Auto-generated bootstrap:",
        "-- Auto-generated animation script",
        "-- Auto-generated Animator State Machine",
        "-- Auto-generated mesh loader",
    )

    def _rehydrate_scripts_from_disk(self, scripts_dir: Path) -> None:
        """Populate rbx_place.scripts from disk for the preserved-scripts path.

        Uses the previous run's conversion_plan.json for script_type and
        parent_path; falls back to content heuristics for unclassified files.

        Records each script's relative disk path so the final rewrite loop in
        write_output can put edits back in nested subdirs (animations/,
        animation_data/, scriptable_objects/) instead of defaulting every
        file to the top-level scripts/ dir.

        Rehydrates ALL ``.luau`` files including converter-emitted ones
        — the review flow lets users hand-edit auto-gen scripts
        between assemble and upload, and skipping them would silently
        discard those edits. Opt-out behaviour (``--scaffolding=fps``
        OFF after a prior FPS run) is handled separately by
        ``_subphase_inject_autogen_scripts``, which removes rehydrated
        FPS auto-gen scripts when scaffolding doesn't include ``fps``.
        """

        plan_lookup = self._load_storage_plan_for_rehydration()
        luau_files = sorted(scripts_dir.rglob("*.luau"))
        from_plan = 0
        rehydrated = 0
        for luau_path in luau_files:
            name = luau_path.stem

            # Skip retired skeletal-animation artifacts. An output dir from a
            # pre-retirement run may still hold AnimationData_* /
            # AnimBootstrap_* / character_animator files; rehydrating them
            # would resurrect the retired feature. These are converter-internal
            # names that cannot collide with a user clip's Anim_* inline-tween
            # script (Anim_<ctrl>_<clip>), which is kept.
            if (name.startswith("AnimationData_")
                    or name.startswith("AnimBootstrap_")
                    or name == "character_animator"):
                continue

            source = luau_path.read_text(encoding="utf-8")

            # Phase 2a slice 5 round 3: ``intrinsic_script_type`` is
            # restored from the on-disk plan when the producing run
            # stamped it. ``None`` for orphans (no plan row) and for
            # rows that pre-date round 3 — both cases leave the field
            # unset, falling through to the documented
            # ``derive_intrinsic_script_class`` heuristic.
            intrinsic_type: str | None = None
            if name in plan_lookup:
                script_type, parent_path, intrinsic_type = plan_lookup[name]
                from_plan += 1
            else:
                script_type = "Script"
                parent_path = None
                if source.rstrip().endswith("return " + name) or "\nreturn " in source:
                    script_type = "ModuleScript"
                elif "game.Players.LocalPlayer" in source or "UserInputService" in source:
                    script_type = "LocalScript"

            # ``intrinsic_script_type`` is a ``ScriptType | None``
            # literal; only forward a known-valid value.
            intrinsic_arg: ScriptType | None = (
                cast(ScriptType, intrinsic_type)
                if intrinsic_type in ("Script", "LocalScript", "ModuleScript")
                else None
            )
            script = RbxScript(
                name=name,
                source=source,
                script_type=cast(ScriptType, script_type),
                intrinsic_script_type=intrinsic_arg,
                source_path=str(luau_path.relative_to(scripts_dir)),
            )
            if parent_path and hasattr(script, "parent_path"):
                script.parent_path = parent_path
            self.state.rbx_place.scripts.append(script)
            rehydrated += 1

        log.info(
            "[write_output] Rehydrated %d scripts from disk (%d via plan, %d via heuristic)",
            rehydrated, from_plan, rehydrated - from_plan,
        )

    def _load_storage_plan_for_rehydration(
        self,
    ) -> dict[str, tuple[str, str | None, str | None]]:
        """Load conversion_plan.json into
        ``name -> (script_type, parent_path, intrinsic_script_type)``.

        Returns {} on missing or malformed plan.

        Phase 2a slice 5 round 3: ``intrinsic_script_type`` is the
        immutable transpile-time class persisted on each
        ``storage_plan.decisions[]`` row. It is read from the decisions
        rowset (NOT the bucket map, which only reflects the post-
        classifier ``script_type``). If a row is missing the field
        (older serialized plan), ``None`` is returned and the rehydrate
        path leaves ``RbxScript.intrinsic_script_type`` unset, falling
        back to the documented ``script_type`` heuristic in
        ``derive_intrinsic_script_class``. Additive, backward-compat.
        """
        plan_path = self.output_dir / "conversion_plan.json"
        if not plan_path.exists():
            return {}

        import json as _json
        try:
            raw = _json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.debug("[rehydrate] conversion_plan.json unreadable: %s", exc)
            return {}

        plan = raw.get("storage_plan") or {}
        # Build a per-script ``intrinsic_script_type`` lookup from the
        # decisions rowset. A given script may have multiple decision
        # rows (classifier + topology override); the LAST row wins so
        # the topology authority's stamp survives. ``None`` for rows
        # whose stamp was unknown at classification time.
        intrinsic_by_name: dict[str, str | None] = {}
        for row in plan.get("decisions", []) or []:
            if not isinstance(row, dict):
                continue
            sname_obj = row.get("script")
            if not isinstance(sname_obj, str) or not sname_obj:
                continue
            # ``get`` returns ``None`` if the row pre-dates round 3 —
            # falls through to the heuristic fallback.
            val_obj = row.get("intrinsic_script_type")
            val = val_obj if isinstance(val_obj, str) else None
            intrinsic_by_name[sname_obj] = val

        category_map = [
            ("server_scripts",           "Script",       "ServerScriptService"),
            ("client_scripts",           "LocalScript",  "StarterPlayer.StarterPlayerScripts"),
            ("character_scripts",        "LocalScript",  "StarterPlayer.StarterCharacterScripts"),
            ("replicated_first_scripts", "ModuleScript", "ReplicatedFirst"),
            ("shared_modules",           "ModuleScript", "ReplicatedStorage"),
            ("server_modules",           "ModuleScript", "ServerStorage"),
        ]
        lookup: dict[str, tuple[str, str | None, str | None]] = {}
        for cat_key, script_type, parent_path in category_map:
            for name in plan.get(cat_key, []) or []:
                lookup[name] = (
                    script_type,
                    parent_path,
                    intrinsic_by_name.get(name),
                )
        return lookup

    def _classify_storage(self) -> None:
        """Phase 4a.5: run the storage classifier on populated scripts.

        Assigns each RbxScript a concrete ``parent_path`` based on call-graph
        analysis + client/server API detection. Persists the resulting plan
        to ``self.ctx.storage_plan`` and to ``conversion_plan.json`` in the
        output directory.

        Phase 2a slice 6: runs the scene-runtime topology PREPASS
        (``infer_module_domains`` + ``derive_reachability_requirements``)
        before ``classify_storage`` so slice 7's storage decision tree
        can read per-module domain verdicts + reachability requirements
        as inputs. The prepass uses ``self.ctx.scene_runtime`` (planner
        artifact, available since the ``plan_scene_runtime`` phase) +
        the on-disk merged ``domain_overrides`` (so operator-supplied
        overrides flow through the prepass too). The prepass is gated
        identically to the late classifier today — generic mode only,
        and only when ``scene_runtime.modules`` is non-empty + the
        ``__skip_domain_classifier__`` probe flag is unset. Slice 6's
        kwarg into ``classify_storage`` defaults to ``None`` (legacy
        path); slice 7 will flip the consumer to the prepass output.

        Safe to call multiple times — the classifier is idempotent.
        """
        if self.state.rbx_place is None or not self.state.rbx_place.scripts:
            return

        from converter.storage_classifier import classify_storage
        import json as _json

        # Phase 2a slice 6: pre-merge scene_runtime so the prepass runs
        # against the same artifact the legacy classifier sees (sticky
        # ``domain_overrides`` + legacy-domain migration + any unknown
        # on-disk keys preserved forward-compat). The same merged dict
        # is reused after ``classify_storage`` -- _merge_scene_runtime
        # is idempotent in its observable output.
        plan_path = self.output_dir / "conversion_plan.json"
        scene_runtime = self._merge_scene_runtime(plan_path)

        # Phase 2a slice 6: early prepass. Same gate as the late classifier
        # (see the comment block in the post-classify_storage branch
        # below). Produces the per-module domain + reachability-requirement
        # map slice 7 will consume from inside ``classify_storage``.
        topology_inputs = self._maybe_run_topology_prepass(scene_runtime)

        plan = classify_storage(
            self.state.rbx_place.scripts,
            dependency_map=self.state.dependency_map or None,
            topology_inputs=topology_inputs,
        )
        self.ctx.storage_plan = plan

        # Record each script's subdir so rehydration can route it back.
        script_paths: dict[str, str] = {}
        scripts_dir = self.output_dir / "scripts"
        if scripts_dir.is_dir():
            for luau_path in sorted(scripts_dir.rglob("*.luau")):
                script_paths.setdefault(
                    luau_path.stem, str(luau_path.relative_to(scripts_dir)),
                )

        # Animation routing (Phase 4.5): per-clip target + reason.
        animation_routing: dict[str, dict[str, dict[str, str]]] = {}
        if self.state.animation_result is not None:
            animation_routing = getattr(self.state.animation_result, "routing", {}) or {}

        # ``plan_path`` + ``scene_runtime`` already computed pre-classify_storage
        # (see slice-6 prepass block above). ``_merge_scene_runtime`` is
        # behavior-equivalent at this point in the run -- on-disk plan
        # hasn't been rewritten yet, so re-running it here would produce
        # the same dict. Slice 6 deduplicates the call.

        # PR3b: stamp per-module ``domain`` / ``container`` / ``module_path``
        # / ``domain_signals`` once the storage classifier has finalized
        # every script's ``parent_path``. **Gated on
        # ``ctx.scene_runtime_mode != "legacy"`` so the legacy emit path
        # stays byte-identical** (per PR3a's "default output byte-identical"
        # invariant). The reachability sub-pass mutates RbxScript.parent_path
        # in service of the contract pipeline -- running it under legacy
        # would shift script placement for ANY project whose code matches
        # PR3b's new generic-only client patterns (RenderStepped, etc.)
        # without an operator opt-in, which is exactly what PR3a's
        # invariant prohibits.
        #
        # Tests can override the gate either by setting
        # ``ctx.scene_runtime_mode = "generic"`` or, for the narrow
        # case of probing classify_storage in isolation, by setting
        # ``scene_runtime["__skip_domain_classifier__"] = True``.
        if (
            self.ctx.scene_runtime_mode != "legacy"
            and scene_runtime.get("modules")
            and not scene_runtime.get("__skip_domain_classifier__")
        ):
            from converter.scene_runtime_domain import (
                classify_scene_runtime_domains,
                migrate_legacy_domain_values,
            )
            from converter.scene_runtime_planner import (
                backfill_lifecycle_role_inputs,
            )
            # Migrate any pre-v2 ``"legacy"`` domain values lurking in the
            # on-disk plan we just merged (the on-disk plan may have been
            # produced by an older converter run). Idempotent.
            migrate_legacy_domain_values(cast("dict", scene_runtime))
            # Phase 2a slice 2: backfill ``character_attached`` /
            # ``is_loader`` on runtime-bearing module rows that pre-date
            # slice 2. Without this, a user resuming a pre-slice-2
            # conversion hits ``build_topology`` invariant 7 on every
            # runtime-bearing row. The backfill uses the same
            # ``REPLICATED_FIRST_HINTS`` regex the planner stamps with,
            # so a resume produces the same artifact a fresh replan
            # would. Idempotent.
            backfill_lifecycle_role_inputs(cast("dict", scene_runtime))
            networking = getattr(self.ctx, "networking_mode", "none")
            strict = bool(getattr(self.ctx, "strict_classification", False))
            report = classify_scene_runtime_domains(
                cast("dict", scene_runtime),
                self.state.rbx_place.scripts,
                dependency_map=self.state.dependency_map or None,
                guid_index=self.state.guid_index,
                networking=networking,
                strict=strict,
            )
            scene_runtime["displaced_instances"] = report["displaced_instances"]
            scene_runtime["low_confidence_modules"] = report["low_confidence_modules"]
            scene_runtime["excluded_modules"] = report["excluded_modules"]
            if report["mirror_adoption_low"]:
                scene_runtime["mirror_adoption_low"] = True
                self.ctx.warnings.append(
                    f"[scene_runtime] --networking={networking} declared "
                    "but adoption signals are sparse: few netcode annotations "
                    "and/or zero `using Mirror`/`using Unity.Netcode` imports. "
                    "Most modules will fall through to the server default. "
                    "Consider --networking=none or expand annotations. "
                    "See conversion report for module-level detail."
                )
            # Strict mode defense-in-depth (PR135 P1.2). The primary
            # gate is in ``plan_scene_runtime`` (pre-transpile) so an
            # operator never pays the AI transpile cost on a plan that
            # won't ship. This late check should NEVER fire when the
            # early gate is reachable; it's kept so a code path that
            # skips ``plan_scene_runtime`` (e.g. a phase resume that
            # rehydrates ``ctx.scene_runtime`` from disk without re-
            # planning) still surfaces strict violations rather than
            # silently writing partial output.
            if strict and report["strict_violations"]:
                violations = "\n  - ".join(report["strict_violations"])
                raise RuntimeError(
                    "--strict-classification: domain classifier left "
                    f"{len(report['strict_violations'])} runtime-bearing "
                    "module(s) unresolved (late check; the primary gate "
                    "in plan_scene_runtime should have caught this). "
                    "Add scene_runtime.domain_overrides entries (or "
                    "split the source class) before re-running:\n  - "
                    + violations
                )

            # Scene-runtime topology (Phase 1, PR #148). Runs AFTER the
            # classifier has populated ``modules[*].domain`` and BEFORE
            # the on-disk plan is written. Consumes the per-emission
            # rowset animation_converter accumulated + the script
            # objects' final ``script_type`` to build the topology
            # artifact, then applies the artifact's animation_drivers
            # decisions to the corresponding ``Anim_*`` RbxScripts
            # (script_type + parent_path) so the topology is the
            # authority for animation placement.
            #
            # ``plan`` is passed so the same call can patch the legacy
            # storage_plan buckets in-place (move topology-flipped
            # Anim_* names from server_scripts → client_scripts). This
            # honors the design doc's §Migration discipline rule:
            # "Each phase deletes the displaced logic in the same PR
            # that wires the new consumer." Without this, the on-disk
            # plan contradicts the live RbxScript metadata + the
            # scene_runtime.topology block.
            #
            # Phase 2a slice 9a: ``topology_inputs`` from the prepass
            # is passed through. Resume contract: on a
            # ``--phase=write_output`` (or any) resume,
            # ``materialize_and_classify`` is in ``ESSENTIAL_PHASES``
            # (pipeline.py:612) so this method (``_classify_storage``)
            # re-runs, ``_maybe_run_topology_prepass`` re-runs, and a
            # fresh ``TopologyInputs`` is produced for this call —
            # no persistence to ``StoragePlan`` is needed (and would
            # violate slice 6's "save raw facts, recompute conclusions"
            # rule, which keeps ``reachability_requirements`` /
            # ``domains`` recomputed every run). Same gates fire on
            # both the prepass and this branch (``scene_runtime_mode
            # != "legacy"`` + non-empty ``modules`` + no
            # ``__skip_domain_classifier__``) plus ``rbx_place``
            # presence (checked inside both methods), so
            # ``topology_inputs`` is non-None whenever this call site
            # is reached; the kwarg's ``None`` default exists so unit
            # tests + future callers can still invoke the method
            # without forcing the prepass dependency.
            #
            # Phase 2a slice 9b R1 fold-in: the prepass also returns
            # ``None`` when ``rbx_place.scripts`` is empty
            # (``_maybe_run_topology_prepass`` line ~4440). The
            # ``_classify_storage`` early-return at line ~4193 today
            # covers the same condition, so this branch's gate
            # (``modules`` non-empty + not legacy + not probe-skip)
            # cannot legitimately be reached with empty scripts.
            # Guard defensively anyway — a future caller that bypasses
            # the early return (e.g. unit test rebinding state mid-
            # method, an injected mid-method hook) should NOT crash
            # the topology branch, just skip it. The check is
            # conservative: ``topology_inputs is None`` is exactly the
            # signal that the prepass declined to run.
            if topology_inputs is not None:
                self._build_and_apply_topology(
                    scene_runtime, plan,
                    topology_inputs=topology_inputs,
                )

        plan_path.write_text(
            _json.dumps({
                "storage_plan": plan.to_dict(),
                "script_paths": script_paths,
                "animation_routing": animation_routing,
                "scene_runtime": scene_runtime,
            }, indent=2),
            encoding="utf-8",
        )
        log.info(
            "[classify_storage] %d scripts classified (plan written to %s)",
            len(plan.decisions),
            plan_path.name,
        )

    def _maybe_run_topology_prepass(
        self,
        scene_runtime: dict[str, object],
    ) -> "TopologyInputs | None":
        """Slice-6 early prepass: produce ``TopologyInputs`` BEFORE
        ``classify_storage`` runs.

        Returns ``None`` when the same gate the late classifier uses
        rejects the call -- legacy mode, no ``modules`` block, or the
        ``__skip_domain_classifier__`` probe flag is set. Slice 6's
        ``classify_storage`` treats ``topology_inputs=None`` as the
        legacy decision path (byte-identical to slice 5).

        When the gate accepts, this function:
          - Backfills lifecycle-role inputs on pre-slice-2 module rows.
          - Runs ``infer_module_domains`` -- pure, no ``parent_path``.
          - Runs ``derive_reachability_requirements`` -- pure.
          - Builds ``script_id_by_name`` via the canonical helper.
          - Builds ``caller_graph`` via the canonical
            ``resolve_caller_graph`` helper (honors the
            ``transpilation_result is None`` preserve rule -- same
            signal ``_build_and_apply_topology`` uses).
          - Reads ``lifecycle_role`` off each module row (when set
            already by ``plan_scene_runtime``).

        The returned ``TopologyInputs`` is NOT persisted onto
        ``StoragePlan``. Per the slice-6 "save raw facts, recompute
        conclusions" rule, ``domains`` and ``reachability_requirements``
        depend on operator-editable inputs
        (``scene_runtime.domain_overrides``, ``networking_mode``) +
        current source, so they are recomputed by this function on
        every run -- including assemble-no-retranspile resumes. On
        resume, ``state.dependency_map`` is empty (set only inside
        ``transpile_scripts``); ``derive_reachability_requirements``
        intentionally returns ``{}`` in that case, and slice 7's
        consumer falls back to the "unconstrained helper" path for
        any module not present in the requirements map. This is the
        same trade slice 3 already accepts for ``caller_graph``;
        ``caller_graph`` itself remains persisted via
        ``resolve_caller_graph``'s ``preserved_caller_graph`` path,
        which is the explicit non-recomputable exception (it depends
        on the transpile-time ``dependency_map`` surface that is
        absent on no-transpile resumes). See ``slice-6.md`` handoff
        for the rule + the two-point Codex amendment.
        """
        if self.ctx.scene_runtime_mode == "legacy":
            return None
        modules = scene_runtime.get("modules")
        if not modules:
            return None
        if scene_runtime.get("__skip_domain_classifier__"):
            return None
        if self.state.rbx_place is None or not self.state.rbx_place.scripts:
            return None

        from converter.scene_runtime_domain import (
            derive_reachability_requirements,
            infer_module_domains,
            migrate_legacy_domain_values,
        )
        from converter.scene_runtime_planner import (
            backfill_lifecycle_role_inputs,
            build_script_id_by_name,
            derive_intrinsic_script_class,
        )
        from converter.scene_runtime_topology.build_topology import (
            resolve_caller_graph,
        )
        from converter.scene_runtime_topology.lifecycle_roles import (
            derive_module_lifecycle_role,
        )
        from converter.scene_runtime_topology.module_domain import (
            TopologyInputs,
        )

        # Migration + backfill mirror what the post-classify_storage
        # branch does today (and are idempotent), so doing them here
        # too keeps slice 5's "consistent classifier inputs across
        # both passes" invariant.
        migrate_legacy_domain_values(cast("SceneRuntimeArtifact", scene_runtime))
        backfill_lifecycle_role_inputs(scene_runtime)

        networking = getattr(self.ctx, "networking_mode", "none")

        # Pre-classify-storage caller_graph derivation. Slice 3 round 5
        # codified the ``state.transpilation_result is not None``
        # signal as "did transpile run this invocation" -- empty
        # dependency_map alongside a populated transpilation_result is
        # the legitimate "fresh ran with no edges" case (don't
        # preserve); populated dep_map alongside a None transpilation_result
        # is the resume case (preserve the prior block).
        preserved_caller_graph: dict[str, list[str]] | None
        if self.state.transpilation_result is not None:
            preserved_caller_graph = None
        else:
            prior_topology_obj = scene_runtime.get("topology", {})
            prior_topology = (
                prior_topology_obj
                if isinstance(prior_topology_obj, dict) else {}
            )
            pcg = prior_topology.get("caller_graph", {})
            preserved_caller_graph = (
                pcg if isinstance(pcg, dict) and pcg else None
            )

        domain_results = infer_module_domains(
            cast("SceneRuntimeArtifact", scene_runtime),
            self.state.rbx_place.scripts,
            dependency_map=self.state.dependency_map or None,
            guid_index=self.state.guid_index,
            networking=networking,
        )
        reqs = derive_reachability_requirements(
            cast("SceneRuntimeArtifact", scene_runtime),
            self.state.rbx_place.scripts,
            domain_results,
            dependency_map=self.state.dependency_map or None,
        )
        caller_graph = resolve_caller_graph(
            cast("SceneRuntimeArtifact", scene_runtime),
            self.state.dependency_map or None,
            preserved_caller_graph=preserved_caller_graph,
        )
        script_id_by_name = build_script_id_by_name(
            self.state.rbx_place.scripts,
            cast("dict[str, SceneRuntimeModule | dict[str, object]]", modules),
        )

        domains: dict[str, str] = {
            sid: res["domain"] for sid, res in domain_results.items()
        }
        # Slice 7 round 2 (Codex P1 #2 fix): compute ``lifecycle_role``
        # INLINE here so the storage classifier's slice-7 decision tree
        # actually sees a populated dict on a fresh run. Pre-round-2
        # this dict was populated by reading
        # ``row.get("lifecycle_role")``, but no upstream stamper writes
        # that key onto the source row -- ``build_topology._build_modules_block``
        # computes it for the artifact entry but does not mutate the
        # row, and the planner stamps only the RAW inputs
        # (``is_loader`` / ``character_attached``). So the prepass
        # produced an empty dict, the consumer at
        # ``storage_classifier._decide_script_container_from_topology``
        # got ``""`` for every sid, and the ``character_attached`` /
        # ``loader`` branches were dead in production. Unit tests
        # passed because ``_mk_topology_inputs`` pre-stamped the dict
        # directly, bypassing the producer/consumer ordering.
        #
        # Option A.2 from the round 1 decision doc: recompute inline
        # via ``derive_module_lifecycle_role`` -- no row mutation. The
        # late ``build_topology._build_modules_block`` also calls
        # ``derive_module_lifecycle_role`` with the same inputs, so
        # the prepass dict and the artifact entry are byte-identical.
        # See ``slice-7-r1-decision.md`` for the verification chain.
        #
        # Round 4 (R3 review P2-NEW-B): unify the prepass join key with
        # the routing join key. Pre-R4 the prepass joined on class_name
        # only (via ``build_scripts_by_class_name``) but the routing
        # path in ``_decide_script_container_from_topology`` joined on
        # ``script_id_by_name`` (class_name with stem fallback, both
        # collision-excluded). Disagreement case: two modules with
        # colliding ``class_name`` but distinct stems pass routing's
        # lookup via the stem fallback, but the prepass excludes them
        # both (class_name collision) and silently demotes
        # ``character_attached`` / ``loader`` to the default role —
        # exactly the failure mode the slice-3 contract was designed
        # to surface.
        #
        # Fix (option c per memory's "canonical contract" rule):
        # invert ``script_id_by_name`` to build a ``script_by_sid``
        # map keyed on the same join the routing path uses. One source
        # of truth for class_name + stem + collision exclusion across
        # both consumers. ``build_scripts_by_class_name`` is still
        # used downstream by ``_build_and_apply_topology`` /
        # ``_build_modules_block`` for the class_name keyspace there
        # (see TODO in this method's R4 commit message for the
        # symmetric follow-up).
        modules_dict = cast("dict[str, dict[str, object]]", modules)
        scripts_by_name: dict[str, RbxScript] = {
            s.name: s for s in self.state.rbx_place.scripts if s.name
        }
        # Invert ``script_id_by_name`` (``script.name -> sid``) to
        # ``sid -> RbxScript``. Both directions share the SAME
        # collision-exclusion contract because they're derived from the
        # same producer. Modules whose class_name + stem both collide
        # (or both miss) are absent from ``script_id_by_name`` and
        # therefore also absent from ``script_by_sid`` — the prepass
        # then drops them to ``script_class=""`` /
        # ``derive_intrinsic_script_class(None)``, which is the
        # safe-default outcome the slice-3 contract specifies.
        script_by_sid: dict[str, RbxScript] = {
            sid: scripts_by_name[script_name]
            for script_name, sid in script_id_by_name.items()
            if script_name in scripts_by_name
        }
        lifecycle_roles: dict[str, str] = {}
        for sid, row in modules_dict.items():
            script = script_by_sid.get(sid)
            script_class = derive_intrinsic_script_class(script)
            module_domain = domains.get(sid, "")
            character_attached = bool(row.get("character_attached", False))
            is_loader = bool(row.get("is_loader", False))
            role = derive_module_lifecycle_role(
                domain=module_domain,
                script_class=script_class,
                character_attached=character_attached,
                is_loader=is_loader,
            )
            lifecycle_roles[sid] = role

        return TopologyInputs(
            domains=domains,
            reachability_requirements=reqs,
            lifecycle_roles=lifecycle_roles,
            script_id_by_name=script_id_by_name,
            caller_graph=caller_graph,
            # Slice 7: raw fact -- did transpile run this invocation?
            # ``state.transpilation_result is not None`` is the
            # canonical signal (see slice 3 round 5 + slice 6 handoff).
            # Lets the consumer distinguish "no-transpile resume with
            # degraded reachability_requirements" from "real
            # classification bug." Per the slice-6 persistence rule
            # this is a RAW FACT about pipeline execution, not a
            # derived conclusion, so it is safe to carry.
            transpile_ran=self.state.transpilation_result is not None,
        )

    def _build_and_apply_topology(
        self,
        scene_runtime: dict[str, object],
        plan: "StoragePlan",
        *,
        topology_inputs: "TopologyInputs | None" = None,
    ) -> None:
        """Phase 1, PR #148 of the scene-runtime topology authority refactor.

        Build the topology artifact + apply its animation_drivers decisions
        to the corresponding ``Anim_*`` RbxScripts. Called inside
        ``_classify_storage`` AFTER ``classify_scene_runtime_domains``
        populates module domains + BEFORE the on-disk plan is written.

        ``topology_inputs`` (Phase 2a slice 9a): the same
        ``TopologyInputs`` row ``_maybe_run_topology_prepass`` produced
        earlier in ``_classify_storage`` (or ``None`` if the prepass
        gate rejected the call -- legacy mode / no ``modules`` block /
        ``__skip_domain_classifier__`` probe). Plumbed through so this
        method can consume the canonical ``script_id_by_name`` index
        (and the inverted ``script_by_sid`` derived from it) without
        re-deriving via the legacy class_name-only join — closes the
        same asymmetric-join hole slice 7 round 4 fixed at the prepass
        boundary, this time on the late ``_build_modules_block`` side.
        Per slice 6's "save raw facts, recompute conclusions" rule
        ``TopologyInputs`` itself is NOT persisted to ``StoragePlan``;
        plumbing means in-memory pass-through, and on a
        ``--phase=write_output`` resume the prepass re-runs (because
        ``materialize_and_classify`` is essential) and produces a fresh
        ``TopologyInputs`` for this call.

        The artifact lands at ``scene_runtime["topology"]`` per design
        doc open-question D4 (option b). Consumers should read through
        this method's outputs / the package's accessor surface — not by
        indexing the dict directly — so a future relocation to a
        sidecar file is a one-file change.

        For each ``Anim_*`` script the topology returns a
        ``routing_status`` + ``script_class``. Resolved entries override
        the RbxScript's ``script_type`` (Script→LocalScript for client
        drivers) and ``parent_path`` (ServerScriptService →
        StarterPlayer.StarterPlayerScripts). Unresolved + orphan
        entries leave the RbxScript at today's server placement.

        ``plan`` is also patched in-place to reflect the topology
        overrides: a Script→LocalScript flip moves the script name from
        ``plan.server_scripts`` to ``plan.client_scripts`` so the
        on-disk ``conversion_plan.json`` doesn't contradict the live
        RbxScript metadata or the persisted ``scene_runtime.topology``
        block. This honors the design doc's §Migration discipline
        ("Each phase deletes the displaced logic in the same PR that
        wires the new consumer") for the Anim_* slice; non-animation
        scripts continue through classify_storage's regex pass until
        Phase 2a wires script_storage as a bound consumer.
        """
        if self.state.rbx_place is None:
            return

        # Phase 2a slice 3 round 4 fix (Claude P1.A + P1.B): the
        # round-3 "early return when animation_result is None" guard
        # over-fired in two ways. (1) It fired on legitimate
        # fresh-no-animations paths (--no-animations flag, projects
        # without .anim files, test paths skipping convert_animations)
        # and regressed slice 3 round 1's "topology always built"
        # goal. (2) It preserved the WHOLE persisted topology on
        # resume, including a now-stale caller_graph alongside a
        # freshly-rebuilt state.dependency_map — silent divergence
        # by construction.
        #
        # The structural fix is to ALWAYS rebuild modules + edges +
        # caller_graph from current state, AND preserve the prior
        # animation_drivers block ONLY when we lack fresh emission
        # data. The prior topology block lives at
        # ``scene_runtime["topology"]`` after ``_merge_scene_runtime``
        # rehydrates it from a previous ``conversion_plan.json``;
        # ``animation_result is None`` is the signal for "no fresh
        # emissions available."
        prior_topology = scene_runtime.get("topology", {})
        if not isinstance(prior_topology, dict):
            prior_topology = {}

        if self.state.animation_result is not None:
            # Fresh animation data available (covers fresh-with-anims
            # AND fresh-no-anims — emitted_animations is just empty
            # in the latter).
            emitted_animations: list = list(
                self.state.animation_result.emitted_animations,
            )
            preserved_animation_drivers = None
        else:
            # No fresh animation data this build. If the persisted
            # topology has a prior animation_drivers block, preserve
            # it; otherwise emit empty (first-time-no-anims resume).
            emitted_animations = []
            pad = prior_topology.get("animation_drivers", {})
            preserved_animation_drivers = (
                pad if isinstance(pad, dict) and pad else None
            )

        # Phase 2a slice 3 round 5 (codex P2): on
        # assemble-without-retranspile workflows, ``transpile_scripts``
        # doesn't rerun and ``state.dependency_map`` stays empty —
        # preserve the prior ``caller_graph`` so we don't overwrite
        # it with ``{}``.
        #
        # Round 6 (codex P2): use ``transpilation_result`` rather
        # than the dep_map's truthiness as the "did transpile run
        # this invocation?" signal. A genuine retranspile that
        # removes the last cross-script reference ALSO leaves
        # dep_map empty — using dep_map alone would silently carry
        # forward stale callers in that case. ``transpilation_result``
        # is set IFF transpile_scripts ran this invocation (both are
        # populated in the same code path, pipeline.py:1942-1971);
        # an empty dep_map alongside a populated transpilation_result
        # is the legitimate "ran with no edges" case (use the empty
        # fresh graph, don't preserve).
        if self.state.transpilation_result is not None:
            preserved_caller_graph = None
        else:
            pcg = prior_topology.get("caller_graph", {})
            preserved_caller_graph = (
                pcg if isinstance(pcg, dict) and pcg else None
            )

        from converter.scene_runtime_topology.build_topology import (
            build_topology,
        )

        # Phase 2a slice 4 round 3 review (Claude P1.A): use the
        # shared ``build_scripts_by_class_name`` helper so the
        # pipeline + planner share ONE source of truth for the
        # class_name → script join. Pre-fix the pipeline keyed by
        # ``script.name`` (file stem) but downstream consumers
        # (build_topology._build_modules_block) looked up via
        # module rows' ``class_name`` — same name-vs-class-name
        # conflation slice 4 round 2 fixed in the planner.
        from converter.scene_runtime_planner import (
            build_scripts_by_class_name,
        )
        modules_in = scene_runtime.get("modules", {}) or {}
        scripts_by_class = build_scripts_by_class_name(
            self.state.rbx_place.scripts,
            cast("dict", modules_in),
        )

        # Phase 2a slice 9a (followup task #10 fold-in): invert
        # ``topology_inputs.script_id_by_name`` (``s.name -> sid``,
        # built via the canonical ``build_script_id_by_name`` helper
        # which honors collision exclusion on BOTH the class_name and
        # stem keyspaces) into ``sid -> RbxScript``. Pass to
        # ``build_topology`` so ``_build_modules_block`` can join on
        # ``script_id`` directly instead of the class_name-only
        # ``scripts_by_class`` lookup — closes the same
        # asymmetric-join hole slice 7 round 4 fixed at the prepass
        # boundary, this time on the late-assembly side. Modules
        # whose class_name + stem both collide (or both miss) are
        # absent from ``script_id_by_name`` and therefore also absent
        # from ``script_by_sid``; ``_build_modules_block`` then falls
        # through to ``derive_intrinsic_script_class(None)`` ->
        # ``"ModuleScript"``, the same safe-default outcome
        # ``scripts_by_class`` already produces for those rows.
        script_by_sid: dict[str, RbxScript] | None = None
        # Phase 2a slice 10: also plumb the raw analysis output so
        # ``_build_modules_block`` reads ``reachability_required_container``
        # from ``TopologyInputs.reachability_requirements`` rather than
        # the planner-row audit signal. See the slice-10 block comment
        # in ``build_topology._build_modules_block``.
        reachability_requirements: dict[str, str] | None = None
        if topology_inputs is not None:
            scripts_by_name: dict[str, RbxScript] = {
                s.name: s for s in self.state.rbx_place.scripts if s.name
            }
            script_by_sid = {
                sid: scripts_by_name[script_name]
                for script_name, sid in topology_inputs["script_id_by_name"].items()
                if script_name in scripts_by_name
            }
            reachability_requirements = (
                topology_inputs["reachability_requirements"]
            )

        try:
            artifact = build_topology(
                scene_runtime=cast(
                    "SceneRuntimeArtifact", scene_runtime,
                ),
                emitted_animations=emitted_animations,
                scripts_by_class=scripts_by_class,
                guid_index=self.state.guid_index,
                # Phase 2a slice 3: pass the planner's class-keyed
                # dependency_map so build_topology can curate it into
                # the artifact's `caller_graph` (script_id-keyed
                # incoming-edge view). `None` is treated as empty
                # graph — back-compat for callers pre-dating slice 3.
                dependency_map=self.state.dependency_map or None,
                # Phase 2a slice 3 round 4: preserve prior
                # animation_drivers on resume / no-fresh-emissions
                # builds (caller computed above). Build_topology
                # uses the preserved block verbatim and skips
                # invariant 3 — see its docstring for the contract.
                preserved_animation_drivers=preserved_animation_drivers,
                # Phase 2a slice 3 round 5: preserve prior
                # caller_graph on assemble-no-retranspile workflows
                # where state.dependency_map is empty. Without this
                # the prior populated graph would be overwritten
                # with {} on every assemble rerun.
                preserved_caller_graph=preserved_caller_graph,
                # Phase 2a slice 9a (#10 fold-in): script_id-keyed
                # join for ``_build_modules_block`` (see block
                # comment above for the asymmetric-join rationale).
                script_by_sid=script_by_sid,
                # Phase 2a slice 10: raw analysis output for the
                # topology entry's ``reachability_required_container``
                # surface. See block comment above.
                reachability_requirements=reachability_requirements,
            )
        except Exception as exc:
            # Topology invariants are fail-closed by design, but
            # surfacing the exact context here helps an operator triage
            # before the build aborts.
            log.error(
                "[topology] build_topology raised: %s — aborting build "
                "before the on-disk plan is finalized.", exc,
            )
            raise

        # Persist the artifact under the scene_runtime block. Consumers
        # (animation_converter post-emission, contract_pipeline in
        # Phase 3) read through this key.
        scene_runtime["topology"] = cast("object", artifact)

        # Apply animation_drivers decisions to the matching Anim_*
        # RbxScripts. Today's _subphase_emit_scripts_to_disk created
        # them all as ``script_type="Script"``; topology overrides
        # ``Script→LocalScript`` for client-driven scripts + stamps the
        # corresponding ``parent_path``. Unresolved + orphan entries
        # preserve today's server placement.
        animation_drivers = artifact.get("animation_drivers", {})
        if not animation_drivers:
            return
        # The application loop below maps emissions → drivers by
        # script_name. On resume (animation_result is None) we have
        # preserved drivers but no fresh emissions to walk. The
        # persisted animation_drivers in scene_runtime["topology"]
        # survives the rebuild and downstream readers consult it;
        # RbxScripts on resume rely on their persisted parent_path
        # from the cached conversion. Skipping the apply loop here is
        # the slice-3-scoped fix; deeper resume semantics for
        # RbxScript application are pre-existing scope (slice 3 round
        # 4 fix didn't introduce this resume gap).
        if self.state.animation_result is None:
            return
        # Index drivers by script_name so the apply loop is O(n).
        drivers_by_script_name: dict[str, dict[str, object]] = {}
        for row in self.state.animation_result.emitted_animations:
            script_name = row.get("script_name", "")
            if not script_name:
                continue
            from converter.scene_runtime_topology.animation_routing import (
                compute_stable_id, ORPHAN_SCOPE,
            )
            _scope_ref = row.get("scope_ref", "")
            _scope_segment = _scope_ref if _scope_ref else ORPHAN_SCOPE
            sid = compute_stable_id(
                _scope_segment,
                row.get("ctrl_key", "") or None,
                row.get("clip_disp", ""),
            )
            entry = animation_drivers.get(sid)
            if entry is not None:
                drivers_by_script_name[script_name] = cast(
                    "dict[str, object]", entry,
                )
        # Walk the live RbxScripts once; for each one with a matching
        # animation_drivers entry, apply the topology decision AND patch
        # the storage_plan bucket so the on-disk artifact stays
        # consistent with the in-memory state.
        # Counts (F4): summary log distinguishes "we resolved a driver
        # and the script existed" (applied) from "driver resolved but
        # the named script wasn't in rbx_place" (unmatched — consumer
        # drift) from "topology emitted a row but the routing_status
        # was unresolved/orphan" (skipped — Phase 1 acknowledged gap).
        # Without these counters a silent zero-mutation case looks the
        # same as a healthy run.
        applied_client = 0
        applied_server = 0
        skipped_unresolved = 0
        skipped_orphan = 0
        unmatched = 0
        scripts_by_name: dict[str, RbxScript] = {
            s.name: s for s in self.state.rbx_place.scripts if s.name
        }
        for sname, entry in drivers_by_script_name.items():
            routing_status = entry.get("routing_status", "")
            if routing_status == "unresolved":
                skipped_unresolved += 1
                continue
            if routing_status == "orphan":
                skipped_orphan += 1
                continue
            script = scripts_by_name.get(sname)
            if script is None:
                # Topology said this script should be routed, but no
                # RbxScript with that name exists. Indicates upstream
                # drift between animation_converter's
                # ``generated_scripts`` and the script object list. Log
                # individually + count.
                unmatched += 1
                log.warning(
                    "[topology] animation_drivers row %r has no "
                    "matching RbxScript in rbx_place — consumer drift?",
                    sname,
                )
                continue
            script_class_obj = entry.get("script_class", "")
            domain_obj = entry.get("domain", "")
            script_class = (
                script_class_obj if isinstance(script_class_obj, str) else ""
            )
            domain = domain_obj if isinstance(domain_obj, str) else ""
            if script_class == "LocalScript" and domain == "client":
                script.script_type = "LocalScript"
                script.parent_path = "StarterPlayer.StarterPlayerScripts"
                applied_client += 1
                # F1: patch the storage_plan buckets so the on-disk
                # plan matches the live RbxScript metadata. Remove
                # ALL occurrences from server_scripts (the
                # ``while``-loop guards against a corrupted/duplicated
                # bucket; classifier output is unique today, but this
                # is the kind of resilience the audit trail benefits
                # from — see codex iter-1 review).
                while sname in plan.server_scripts:
                    plan.server_scripts.remove(sname)
                if sname not in plan.client_scripts:
                    plan.client_scripts.append(sname)
                # Audit trail: storage_plan.decisions records the
                # final placement using the SAME schema classifier
                # writes (``script`` / ``script_type`` / ``container``
                # / ``reason``) with a ``source="topology"``
                # discriminator. Consumers iterating ``decisions`` can
                # index any of the canonical 4 keys uniformly across
                # both sources. ``intrinsic_script_type`` is the
                # script's immutable transpile-time class
                # (slice 5 round 3) — preserve it from the live
                # RbxScript so resume-rehydration restores it instead
                # of falling back to the post-coercion ``script_type``.
                plan.decisions.append({
                    "script": sname,
                    "script_type": "LocalScript",
                    "intrinsic_script_type": script.intrinsic_script_type,
                    "container": "StarterPlayer.StarterPlayerScripts",
                    "reason": (
                        "topology: animation_drivers driver_domain=client "
                        f"(driver_module_guid={entry.get('driver_module_guid', '')})"
                    ),
                    "source": "topology",
                })
            elif script_class == "Script" and domain == "server":
                # Already script_type="Script"; just stamp parent_path
                # so storage_classifier doesn't second-guess via its
                # regex pass.
                script.script_type = "Script"
                script.parent_path = "ServerScriptService"
                applied_server += 1
                # No plan-bucket change needed — already in
                # ``server_scripts`` from the legacy classifier pass.

        log.info(
            "[topology] animation_drivers applied: "
            "%d client, %d server (resolved); "
            "%d unresolved, %d orphan (preserved server fallback); "
            "%d unmatched (consumer drift).",
            applied_client, applied_server,
            skipped_unresolved, skipped_orphan,
            unmatched,
        )

    def _merge_scene_runtime(self, plan_path: Path) -> dict[str, object]:
        """Compose the ``scene_runtime`` block written into
        ``conversion_plan.json``.

        Structural sub-blocks (``modules`` / ``scenes`` / ``prefabs``) come
        from ``ctx.scene_runtime`` — recomputed each run by the
        ``plan_scene_runtime`` phase. ``domain_overrides`` is **sticky**:
        if an operator edited it into a prior on-disk plan, the value
        wins over whatever the planner produced (planner emits ``{}``;
        PR3b's classifier never touches this key). Unknown keys present
        on disk are preserved too — forward-compatible with future
        schema extensions without re-touching every consumer.
        """
        import json as _json

        merged: dict[str, object] = dict(self.ctx.scene_runtime or {})

        # On-disk plan may carry a sticky ``domain_overrides`` block from
        # an operator edit or a previous run. Load it; ignore parse
        # failures (worst case we lose the override, which is recorded in
        # ctx.warnings rather than crashing classify_storage).
        on_disk: dict[str, object] = {}
        if plan_path.exists():
            try:
                raw = _json.loads(plan_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    sr = raw.get("scene_runtime")
                    if isinstance(sr, dict):
                        on_disk = cast("dict[str, object]", sr)
            except Exception as exc:
                log.debug(
                    "[classify_storage] scene_runtime merge: "
                    "ignoring unreadable plan: %s", exc,
                )

        # Sticky domain_overrides: on-disk value wins when present, even
        # when empty — operator may have deliberately cleared it.
        if "domain_overrides" in on_disk:
            merged["domain_overrides"] = on_disk["domain_overrides"]
        elif "domain_overrides" not in merged:
            merged["domain_overrides"] = {}

        # Forward-compat: anything else the on-disk plan carries under
        # scene_runtime that we don't recompute (future schema additions)
        # is preserved unless the planner output explicitly overwrites it.
        for key, value in on_disk.items():
            if key not in merged:
                merged[key] = value

        # Classifier-v2 migration: rewrite pre-v2 ``domain="legacy"`` rows
        # in the merged modules block. Idempotent. Cheap (dict scan).
        try:
            from converter.scene_runtime_domain import (
                migrate_legacy_domain_values,
            )
            migrated = migrate_legacy_domain_values(
                cast("dict", merged),
            )
            if migrated:
                log.info(
                    "[classify_storage] migrated %d legacy domain row(s) "
                    "from on-disk plan to 'excluded'", migrated,
                )
        except Exception as exc:
            log.debug(
                "[classify_storage] legacy-domain migration skipped: %s", exc,
            )

        return merged

    def _bind_scripts_to_parts(self) -> None:
        """Bind transpiled scripts to their target parts using _ScriptClass attributes.

        In Unity, MonoBehaviour scripts are children of GameObjects. This method
        replicates that by moving scripts from the global place.scripts list to
        part.scripts, so they become children in the rbxlx hierarchy.

        Scripts placed as children of parts can use `script.Parent` to reference
        their target part directly — matching Unity's MonoBehaviour pattern.
        """

        # Build index: script class name → RbxScript
        script_by_name: dict[str, RbxScript] = {}
        for s in self.state.rbx_place.scripts:
            script_by_name[s.name] = s

        # Walk all parts to find _ScriptClass attributes
        bound_count = 0
        bound_script_names: set[str] = set()

        def _bind_to_tree(parts: list) -> None:
            nonlocal bound_count
            for part in parts:
                # Check for _ScriptClass attribute (set by MonoBehaviour extraction)
                script_classes = set()
                for key, value in (getattr(part, "attributes", None) or {}).items():
                    if key == "_ScriptClass" and isinstance(value, str):
                        script_classes.add(value)

                # Also check for multiple MonoBehaviours stored as _ScriptClass_N
                for key in list((getattr(part, "attributes", None) or {}).keys()):
                    if key.startswith("_ScriptClass"):
                        val = part.attributes[key]
                        if isinstance(val, str):
                            script_classes.add(val)

                for class_name in script_classes:
                    if class_name in script_by_name:
                        script = script_by_name[class_name]
                        # Only bind Server scripts to parts.
                        # ModuleScripts stay in ReplicatedStorage for require().
                        # LocalScripts go to StarterPlayerScripts (they don't
                        # execute when parented to workspace Parts).
                        # Skip stub scripts (AI unavailable).
                        if script.script_type == "Script" and "AI transpilation recommended" not in script.source:
                            # Clone the script for each instance so all prefab
                            # variants get their inherited MonoBehaviour scripts
                            if class_name in bound_script_names:
                                clone = RbxScript(
                                    name=script.name,
                                    source=script.source,
                                    script_type=script.script_type,
                                )
                                part.scripts.append(clone)
                            else:
                                part.scripts.append(script)
                                bound_script_names.add(class_name)
                            bound_count += 1
                            log.debug("[write_output]   Bound '%s' to part '%s'",
                                      class_name, part.name)
                            # Trigger heuristic: any invisible MeshPart that
                            # carries a server Script is acting as a detection
                            # zone (Door's ``base``, Pickup's bounding cube,
                            # etc.). _convert_prefab_node skips collider
                            # processing entirely so the part inherits the
                            # mesh's bounding box as its CanCollide=true
                            # collision volume — a 21-stud invisible cube the
                            # player can't walk through. Force CanCollide=False
                            # here once the script binding confirms the
                            # trigger role; Touched still fires (CanTouch
                            # defaults to true) so Door/Pickup logic works.
                            if (
                                getattr(part, "class_name", None) == "MeshPart"
                                and (getattr(part, "transparency", 0) or 0) >= 1.0
                                and getattr(part, "can_collide", False)
                            ):
                                part.can_collide = False
                                log.debug(
                                    "[write_output]   Forced CanCollide=False on "
                                    "invisible MeshPart '%s' (carries server Script '%s')",
                                    part.name, class_name,
                                )

                # Recurse into children
                if getattr(part, "children", None):
                    _bind_to_tree(part.children)

        _bind_to_tree(self.state.rbx_place.workspace_parts or [])

        # Remove bound scripts from the global list (they're now part children)
        if bound_script_names:
            self.state.rbx_place.scripts = [
                s for s in self.state.rbx_place.scripts
                if s.name not in bound_script_names
            ]
            log.info("[write_output] Bound %d scripts to their target parts", bound_count)

        # Guard unbound scripts whose source actually reads BasePart-only
        # properties off ``script.Parent`` and end up in a service-typed
        # container (SSS/RS) where ``script.Parent`` is the service. The
        # ``requires_part_parent`` field is computed once in
        # script_coherence._detect_part_parent_requirement so this stage
        # is a contract check, not a heuristic.
        #
        # Replaces a regex-based guess that pattern-matched any script
        # mentioning ``script.Parent:FindFirstChild``, ``script.Parent.X``,
        # or ``local x = script.Parent`` — those are generic Roblox
        # idioms that don't require a BasePart parent, so LocalScripts
        # routed to PlayerScripts (where ``script.Parent`` is
        # PlayerScripts, never a BasePart) were silently disabled by
        # the catch-all guard. The breakage was nondeterministic across
        # AI transpile runs (latent since commit d31effc, 2026-03-29).
        # See ``RbxScript.requires_part_parent`` for the rationale.
        import re
        # Self-guarded scripts (smart-binding animation scripts already
        # gate on ``script.Parent:IsA(...)``) still skip the wrap. The
        # blanket BasePart guard would short-circuit their own Model
        # check before it ran.
        _self_guard_patterns = (
            re.compile(r'script\.Parent\s*:\s*IsA\s*\(\s*["\']Model["\']'),
            re.compile(r'script\.Parent\s*:\s*IsA\s*\(\s*["\']BasePart["\']'),
        )
        # Containers whose ``script.Parent`` is NOT a BasePart but also
        # not service-typed. Scripts routed here that happen to be
        # flagged ``requires_part_parent`` are a misconfiguration: they
        # should have been Part-bound. Don't silently guard them; warn
        # loudly so the conversion report makes the misroute visible.
        _NON_PART_PLAYER_CONTAINERS = (
            "StarterPlayer.StarterPlayerScripts",
            "StarterPlayer.StarterCharacterScripts",
            "StarterGui",
        )
        disabled_count = 0
        mis_routed_warn_count = 0
        for s in list(self.state.rbx_place.scripts):
            if s.script_type == "ModuleScript":
                continue
            if not s.requires_part_parent:
                continue
            if any(p.search(s.source) for p in _self_guard_patterns):
                # Self-guarded — let the script make its own decision.
                continue
            parent = s.parent_path or ""
            if any(parent.startswith(prefix) for prefix in _NON_PART_PLAYER_CONTAINERS):
                # Surfaces in conversion_report.json.warnings so the
                # misroute is observable, instead of being papered over
                # by a guard that silently no-ops the entire client.
                msg = (
                    f"[write_output] script '{s.name}' is flagged "
                    f"requires_part_parent but routed to {parent} — "
                    f"runtime BasePart access will fail. Bind the script "
                    f"to a Part or relocate it; the guard intentionally "
                    f"does NOT silently disable client/character scripts."
                )
                log.warning(msg)
                self._add_warning(msg)
                mis_routed_warn_count += 1
                continue
            # Wrap script with the runtime guard — last resort for
            # genuinely-Part-bound scripts that landed in SSS/RS without
            # a Part to attach to (unbound prefab components).
            guard = ('-- Guard: this script expects script.Parent to be a BasePart\n'
                     'if not script.Parent:IsA("BasePart") then return end\n\n')
            s.source = guard + s.source
            disabled_count += 1
            log.debug("[write_output]   Added parent guard to '%s' (parent_path=%s)", s.name, parent)
        if disabled_count:
            log.info("[write_output] Added BasePart parent guards to %d unbound scripts", disabled_count)
        if mis_routed_warn_count:
            log.warning(
                "[write_output] %d script(s) flagged requires_part_parent "
                "but routed to client/character containers — see warnings above.",
                mis_routed_warn_count,
            )

    def _generate_prefab_packages(self) -> None:
        """Phase 4.10 — emit referenced prefabs as Models in
        ReplicatedStorage.Templates, plus a thin PrefabSpawner helper.

        Filters by ``ctx.serialized_field_refs`` (from PR 4 / Phase
        4.9) so only prefabs that scripts actually reference get
        emitted — preventing the rbxlx from bloating with every
        parsed prefab in the project.
        """
        from converter.prefab_packages import (
            generate_prefab_packages, write_packages_manifest,
        )

        prefab_library = self.state.prefab_library
        if prefab_library is None or not getattr(prefab_library, "prefabs", None):
            return

        # Opt-in escape hatch for projects whose runtime scripts were
        # tuned against the historical centroid-pivot behaviour. Set
        # ``U2R_LEGACY_PREFAB_PIVOT=1`` (or the equivalent setting in
        # ``config``) to preserve the old wipe-to-identity contract for
        # one release cycle while migrating.
        import os
        legacy_pivot = (
            os.environ.get("U2R_LEGACY_PREFAB_PIVOT", "").lower()
            in {"1", "true", "yes"}
        )
        result = generate_prefab_packages(
            prefab_library=prefab_library,
            serialized_field_refs=self.ctx.serialized_field_refs or None,
            guid_index=self.state.guid_index,
            material_mappings=self.state.material_mappings,
            uploaded_assets=self.ctx.uploaded_assets,
            legacy_prefab_pivot=legacy_pivot,
        )

        if not result.templates:
            if result.unconverted:
                # Surface drops to UNCONVERTED.md via the shared writer —
                # the animation_result channel is the only carrier right
                # now, so append there. Same pattern as PR 3 materials.
                _carry_unconverted(self.state.animation_result, result.unconverted)
            return

        self.state.rbx_place.replicated_templates.extend(result.templates)
        if result.spawner_script is not None:
            self.state.rbx_place.scripts.append(result.spawner_script)
        if result.unconverted:
            _carry_unconverted(self.state.animation_result, result.unconverted)

        # Attach a copy of every prefab-scoped animation script under its
        # template so cloning ``ReplicatedStorage.Templates.<Prefab>``
        # carries the animation driver. Phase 5.9 baked the prefab name
        # into the script_name (Anim_<Prefab>_<Ctrl>_<Clip>) so names
        # dedupe across scene instances, but write_output left every
        # generated script in the place's flat list — clones left them
        # behind. We *copy* (not move): the flat-list version still
        # drives prefabs that are scene-baked rather than runtime-cloned
        # (``scene_converter._convert_prefab_instance`` expands those
        # inline into ``workspace_parts`` without attaching a script).
        # The script body uses smart binding (script.Parent if it's a
        # part/model, else workspace search) so the same source works in
        # both contexts without races between the two copies.
        self._attach_prefab_scoped_animation_scripts_to_templates()

        # Same problem, different script source: MonoBehaviour scripts
        # (TurretBullet, PlaneBullet, Pickup, etc.) are bound to scene-
        # level parts via ``_bind_scripts_to_parts`` BEFORE templates
        # are generated. By the time we get here, the script has been
        # moved out of the flat list and into a scene part's ``.scripts``
        # list. The template that ``generate_prefab_packages`` just
        # emitted carries ``_ScriptClass`` attributes but no Script
        # children — so ``Templates:Clone()`` at runtime returns a part
        # with no behaviour. Concrete case: SimpleFPS TurretBullet
        # template is a bare red cube with no flight/damage code, so
        # turret-fired bullets fall to the ground inert.
        self._attach_monobehaviour_scripts_to_templates()

        # Persist a small manifest under packages/ — closes the packages
        # half of Phase 4.11's disk-rewrite deferred item.
        try:
            write_packages_manifest(self.output_dir, result.manifest)
        except OSError as exc:
            log.warning("[prefab_packages] manifest write failed: %s", exc)

        log.info(
            "[write_output] Emitted %d prefab templates into "
            "ReplicatedStorage.Templates (%d in manifest)",
            len(result.templates), result.manifest.get("total_templates", 0),
        )

    def _attach_prefab_scoped_animation_scripts_to_templates(self) -> None:
        """Attach copies of prefab-scoped animation scripts under their
        templates without removing the originals from the flat list.

        Reads ``animation_result.script_scopes`` (built when the controller
        lives inside a PrefabTemplate). For every (script_name, template_name)
        pair, if both the script and the template exist on the place,
        deep-copy the script and append the copy to ``template.scripts``.
        The original stays in ``rbx_place.scripts`` so prefabs that were
        expanded inline by ``scene_converter`` still get a driver via
        the same workspace lookup pattern they relied on before this
        pass landed. Scripts that don't match any template (the prefab
        was filtered out by ``serialized_field_refs``) stay in the flat
        list only.
        """
        anim = self.state.animation_result
        if anim is None or not getattr(anim, "script_scopes", None):
            return

        templates_by_name = {
            t.name: t for t in self.state.rbx_place.replicated_templates
        }
        scripts_by_name = {
            s.name: s for s in self.state.rbx_place.scripts
        }

        from copy import copy as _shallow_copy
        attached = 0
        for script_name, template_name in anim.script_scopes.items():
            template = templates_by_name.get(template_name)
            script = scripts_by_name.get(script_name)
            if template is None or script is None:
                continue
            # Independent RbxScript so storage_classifier's parent_path
            # mutation on the flat-list copy doesn't accidentally retag
            # the template-attached copy. Source/name are shared (same
            # smart-binding body works in both contexts).
            template_copy = _shallow_copy(script)
            template_copy.parent_path = None
            template.scripts.append(template_copy)
            attached += 1

        if attached:
            log.info(
                "[write_output] Attached %d prefab-scoped animation "
                "script(s) under ReplicatedStorage.Templates.<Prefab>",
                attached,
            )

    def _attach_monobehaviour_scripts_to_templates(self) -> None:
        """Attach MonoBehaviour scripts under their prefab template parts.

        Mirror of :meth:`_attach_prefab_scoped_animation_scripts_to_templates`
        but for arbitrary ``_ScriptClass`` bindings. Walks every part in
        every prefab template, finds parts with ``_ScriptClass`` (or
        ``_ScriptClass_N``) attributes, and clones the matching script
        body onto the part. Searches BOTH the flat ``place.scripts``
        list and every workspace part's ``.scripts`` (since
        ``_bind_scripts_to_parts`` may have already moved the script
        out of the flat list into a scene-level part).
        """
        from copy import copy as _shallow_copy

        templates = getattr(self.state.rbx_place, "replicated_templates", None)
        if not templates:
            return

        # Build script-by-name index from EVERY location a script could
        # currently live: the flat list + every workspace part's
        # ``.scripts`` (recursive). First-found wins; ties broken by
        # the flat list (most authoritative source).
        #
        # PR #75 codex round-5 [P2]: index is Script-only. A user-
        # authored MonoBehaviour transpiles to ``script_type="Script"``;
        # the canonical ``EventDispatch`` ModuleScript that
        # ``_inject_runtime_modules`` adds shares the user's class
        # name and would otherwise win the flat-list ``setdefault``
        # (because adapter injection appends to the flat list before
        # this pass runs in ``_generate_prefab_packages``). The
        # subsequent ``script_type != "Script"`` guard would then
        # silently skip the user's behaviour on every template clone.
        # Excluding non-Script entries up-front keeps the canonical
        # out of the index and lets the workspace walk surface the
        # user's Script.
        scripts_by_name: dict[str, RbxScript] = {}

        def _collect(parts: list) -> None:
            for p in parts or []:
                for s in getattr(p, "scripts", None) or []:
                    if s.script_type != "Script":
                        continue
                    if s.name not in scripts_by_name:
                        scripts_by_name[s.name] = s
                _collect(getattr(p, "children", None) or [])

        # Flat list first (overrides any scene-attached duplicate).
        for s in self.state.rbx_place.scripts or []:
            if s.script_type != "Script":
                continue
            scripts_by_name.setdefault(s.name, s)
        _collect(self.state.rbx_place.workspace_parts or [])

        attached = 0

        def _walk(parts: list) -> None:
            nonlocal attached
            for part in parts:
                attrs = getattr(part, "attributes", None) or {}
                # ``_ScriptClass`` plus optional ``_ScriptClass_N`` for
                # multi-MonoBehaviour GameObjects (Unity allows several
                # MonoBehaviour components on one GO).
                classes: set[str] = set()
                for key, val in attrs.items():
                    if key.startswith("_ScriptClass") and isinstance(val, str):
                        classes.add(val)
                # Skip when the part already has the script (idempotent
                # under re-run, and avoids duplicate attachment if a
                # future pass also wires scripts onto templates).
                existing_names = {s.name for s in getattr(part, "scripts", None) or []}
                for class_name in classes:
                    if class_name in existing_names:
                        continue
                    source_script = scripts_by_name.get(class_name)
                    if source_script is None:
                        continue
                    # Skip non-Script types — LocalScripts/ModuleScripts
                    # don't belong as direct children of a workspace
                    # part by convention (LocalScripts live under
                    # StarterPlayerScripts, ModuleScripts in RS).
                    if source_script.script_type != "Script":
                        continue
                    # Skip AI-stubbed scripts (no AI key) so the template
                    # doesn't ship a stub that would shadow a real
                    # runtime implementation.
                    if "AI transpilation recommended" in source_script.source:
                        continue
                    clone = _shallow_copy(source_script)
                    clone.parent_path = None
                    part.scripts.append(clone)
                    attached += 1
                _walk(getattr(part, "children", None) or [])

        _walk(templates)

        if attached:
            log.info(
                "[write_output] Attached %d MonoBehaviour script(s) under "
                "ReplicatedStorage.Templates.<Prefab>",
                attached,
            )

    def _inject_runtime_modules(self) -> None:
        """Inject runtime library ModuleScripts when relevant features are detected.

        Scans the place's scripts and parts for features that need runtime support:
        - NavMeshAgent attributes → inject nav_mesh_runtime.luau
        - Canvas/ScreenGui elements → inject event_system.luau
        - CharacterController attributes → inject physics_bridge.luau
        """
        runtime_dir = Path(__file__).parent.parent / "runtime"
        injected = 0

        # Detect features from parts (recursively check all children)
        has_navmesh = False
        has_character_controller = False
        has_cinemachine = False
        has_sub_emitters = False
        # has_pickups removed — scripts propagated automatically now

        def _scan_parts(parts):
            nonlocal has_navmesh, has_character_controller, has_cinemachine, has_sub_emitters
            for part in parts:
                attrs = getattr(part, "attributes", {})
                if attrs.get("_HasNavMeshAgent"):
                    has_navmesh = True
                if attrs.get("_HasCharacterController"):
                    has_character_controller = True
                if attrs.get("CinemachineVCam"):
                    has_cinemachine = True
                # IsPickup detection removed — scripts propagated automatically
                # Check particle emitters for sub-emitter attributes
                for pe in getattr(part, "particle_emitters", None) or []:
                    pe_attrs = getattr(pe, "attributes", {})
                    if pe_attrs.get("_HasSubEmitters"):
                        has_sub_emitters = True
                children = getattr(part, "children", None) or []
                if children:
                    _scan_parts(children)

        _scan_parts(self.state.rbx_place.workspace_parts or [])

        has_ui = len(self.state.rbx_place.screen_guis) > 0

        # Inject runtime modules as ModuleScripts in ReplicatedStorage
        modules_to_inject = []
        if has_navmesh:
            modules_to_inject.append(("NavAgent", "nav_mesh_runtime.luau"))
        if has_ui:
            modules_to_inject.append(("EventSystem", "event_system.luau"))
        if has_character_controller:
            modules_to_inject.append(("CharacterBridge", "physics_bridge.luau"))
        if has_sub_emitters:
            modules_to_inject.append(("SubEmitterRuntime", "sub_emitter_runtime.luau"))

        # Detect object pooling patterns in transpiled scripts
        has_pool = any(
            "pool" in s.source.lower() and ("GetNew" in s.source or "pool.Free" in s.source or "pool.Get" in s.source)
            for s in self.state.rbx_place.scripts
        )
        if has_pool:
            modules_to_inject.append(("ObjectPool", "object_pool.luau"))

        # PickupRuntime removed — pickup scripts are now properly propagated
        # from base prefabs to variants via _bind_scripts_to_parts cloning.
        if has_cinemachine:
            # Cinemachine is a LocalScript (runs on client for camera control)
            cinemachine_path = runtime_dir / "cinemachine_runtime.luau"
            if cinemachine_path.exists():
                source = cinemachine_path.read_text(encoding="utf-8")
                existing = [s for s in self.state.rbx_place.scripts if s.name == "CinemachineRuntime"]
                if not existing:
                    self.state.rbx_place.scripts.append(RbxScript(
                        name="CinemachineRuntime",
                        source=source,
                        script_type="LocalScript",
                    ))
                    injected += 1

        for module_name, filename in modules_to_inject:
            module_path = runtime_dir / filename
            if module_path.exists():
                source = module_path.read_text(encoding="utf-8")
                # Check if already injected (avoid duplicates)
                existing = [s for s in self.state.rbx_place.scripts if s.name == module_name]
                if not existing:
                    self.state.rbx_place.scripts.append(RbxScript(
                        name=module_name,
                        source=source,
                        script_type="ModuleScript",
                    ))
                    injected += 1

        if injected:
            log.info("[write_output] Injected %d runtime library modules", injected)

    def _subphase_inject_scene_runtime(self) -> None:
        """PR4: emit the scene-runtime host runtime + plan + entrypoints.

        Only fires when ``ctx.scene_runtime_mode == "generic"`` and the
        plan carries at least one runtime-bearing module. Injects four
        scripts:

          - ``SceneRuntime`` (ReplicatedStorage ModuleScript) -- the
            host engine from ``converter/runtime/scene_runtime.luau``.
          - ``SceneRuntimePlan`` (ReplicatedStorage ModuleScript) -- the
            per-place plan, derived from ``conversion_plan.json``.
          - ``SceneRuntimeClient`` (StarterPlayerScripts LocalScript) --
            wires the engine to client services + ``start("client")``.
          - ``SceneRuntimeServer`` (ServerScriptService Script) --
            mirrors the client entrypoint for the ``server`` domain.

        Also stamps the conversion-time cross-domain edge report onto
        ``ctx.scene_runtime`` and appends it to ``UNCONVERTED.md``.
        The host runtime applies the v1 nil-injection policy at start;
        the conversion-time emit lets operators see the boundary
        before they ship the place.

        Idempotent: re-runs (incremental ``--phase write_output``)
        overwrite previous SceneRuntime* scripts in place.
        """
        if self.ctx.scene_runtime_mode != "generic":
            return
        if self.state.rbx_place is None:
            return

        scene_runtime = self.ctx.scene_runtime or {}
        modules = scene_runtime.get("modules", {})
        runtime_bearing = [
            sid for sid, row in modules.items()
            if row.get("runtime_bearing")
        ]
        if not runtime_bearing:
            log.info(
                "[write_output] scene-runtime generic mode: no "
                "runtime-bearing modules; skipping host runtime emit"
            )
            return

        from converter.autogen import (
            generate_scene_runtime_client_entrypoint,
            generate_scene_runtime_plan_module,
            generate_scene_runtime_server_entrypoint,
        )
        from converter.scene_runtime_domain import compute_cross_domain_edges

        runtime_dir = Path(__file__).parent.parent / "runtime"
        host_path = runtime_dir / "scene_runtime.luau"
        existing_names = {s.name for s in self.state.rbx_place.scripts}

        # R3-P2: a user (or earlier converter pass) script named e.g.
        # ``SceneRuntime`` must NOT be silently displaced by the autogen
        # emit. Replace only scripts whose source carries the autogen
        # marker we stamped on prior runs (and the runtime engine source
        # which we identify by the comment at its top). The marker shape
        # matches what each generator emits.
        _AUTOGEN_MARKERS: dict[str, str] = {
            "SceneRuntime": "-- scene_runtime: PR4 generic host runtime",
            "SceneRuntimePlan": (
                "-- SceneRuntimePlan (auto-generated by Unity converter; PR4)"
            ),
            "SceneRuntimeClient": (
                "-- SceneRuntimeClient (auto-generated; "
                "PR4 scene-runtime host entrypoint)"
            ),
            "SceneRuntimeServer": (
                "-- SceneRuntimeServer (auto-generated; "
                "PR4 scene-runtime host entrypoint)"
            ),
        }

        def _replace_or_add(script: RbxScript) -> None:
            marker = _AUTOGEN_MARKERS.get(script.name, "")
            new_scripts: list[RbxScript] = []
            user_owned = False
            for s in self.state.rbx_place.scripts:
                if s.name != script.name:
                    new_scripts.append(s)
                    continue
                # Same-name collision. Drop only if it's a prior autogen
                # artifact (carries our marker); otherwise keep the
                # user-owned script and skip the emit.
                if marker and marker in (s.source or ""):
                    continue
                new_scripts.append(s)
                user_owned = True
            if user_owned:
                log.warning(
                    "[write_output] scene-runtime generic: a non-autogen "
                    "script named %r already exists; skipping autogen "
                    "emit to avoid clobbering user-owned content",
                    script.name,
                )
                self.state.rbx_place.scripts = new_scripts
                return
            self.state.rbx_place.scripts = new_scripts
            self.state.rbx_place.scripts.append(script)

        if host_path.exists():
            host_source = host_path.read_text(encoding="utf-8")
            _replace_or_add(RbxScript(
                name="SceneRuntime",
                source=host_source,
                script_type="ModuleScript",
                parent_path="ReplicatedStorage",
            ))
        else:
            log.warning(
                "[write_output] scene-runtime host runtime missing at %s; "
                "skipping (the place will fail to bind runtime-bearing "
                "modules at start)", host_path,
            )
            return

        # R2-P1.3 (contract resolution): rewrite asset/SO refs before
        # embedding the plan. Assets become ``rbxassetid://...``;
        # ScriptableObjects get a ``guid -> dotted module path`` map the
        # host runtime consults at ref-resolution time. Both mutations
        # happen in-place on ``scene_runtime`` so the embedded plan
        # ModuleScript reflects the final shape.
        self._build_scriptable_object_module_map(scene_runtime)
        self._rewrite_scene_runtime_asset_refs(scene_runtime)

        _replace_or_add(generate_scene_runtime_plan_module(
            cast("dict", scene_runtime),
        ))
        _replace_or_add(generate_scene_runtime_client_entrypoint())
        _replace_or_add(generate_scene_runtime_server_entrypoint())

        edges = compute_cross_domain_edges(
            cast("dict", scene_runtime),
        )
        # R5-P1 fix: store edges on ctx ONLY. The actual UNCONVERTED.md
        # write is owned by ``_write_unconverted_md`` (single source of
        # truth for the file). Pre-R5 this subphase wrote the cross-
        # domain block directly here, but ``_write_unconverted_md``
        # runs LATER in write_output and rewrites the file from scratch
        # -- so the mid-pipeline append got clobbered every time. Tests
        # and downstream consumers still read the edges via
        # ``ctx.scene_runtime["cross_domain_edges"]``.
        scene_runtime["cross_domain_edges"] = list(edges)
        if edges:
            log.info(
                "[write_output] scene-runtime generic: %d cross-domain "
                "edges staged for UNCONVERTED.md", len(edges),
            )
        injected_total = 4
        if "SceneRuntime" in existing_names:
            injected_total -= 0  # we always replace, so count remains
        log.info(
            "[write_output] scene-runtime generic: injected host runtime, "
            "plan, and entrypoints (%d runtime-bearing modules)",
            len(runtime_bearing),
        )

    def _build_scriptable_object_module_map(
        self, scene_runtime: dict[str, object],
    ) -> None:
        """R2-P1.3: build ``scene_runtime.scriptable_objects`` -- a
        ``guid -> dotted DataModel path`` map covering every emitted SO
        ModuleScript. The host runtime resolves ``scriptable_object``
        ref rows by looking the persisted GUID up in this map and
        requiring the resulting module path.

        Source of truth: ``self.state.scriptable_objects`` (the SO
        converter result) + ``self.state.guid_index`` (path -> GUID).
        Container resolves via the SO RbxScript's ``parent_path`` -- the
        same shape ``_stamp_container_and_path`` writes for runtime-
        bearing modules.

        Idempotent: rebuilds the map every run; legacy plans that lacked
        the map still resolve via the runtime's fallback passthrough
        (returns ``nil`` rather than crashing).
        """
        from converter.scriptable_object_converter import (
            AssetConversionResult,
            resolve_unique_asset_names,
        )
        so_state = getattr(self.state, "scriptable_objects", None)
        if not isinstance(so_state, AssetConversionResult):
            return
        if not so_state.assets:
            return
        guid_index = getattr(self.state, "guid_index", None)
        if guid_index is None:
            return
        unique = resolve_unique_asset_names(so_state.assets)
        # Reverse-lookup: source_path -> guid. Resolved paths in the
        # GuidIndex may be absolute; the SO converter records the absolute
        # path too, so reuse ``guid_for_path`` (which normalises via
        # ``Path.resolve()``) where possible. Build the explicit reverse
        # map here so a single absolute-vs-relative skew doesn't drop the
        # entire scriptable_objects map.
        path_to_guid: dict[Path, str] = {}
        for guid, entry in guid_index.guid_to_entry.items():
            path_to_guid[entry.asset_path] = guid
            try:
                path_to_guid[entry.asset_path.resolve()] = guid
            except (OSError, RuntimeError):
                pass
        # rbx_place.scripts carries the final parent_path each SO was
        # routed to by storage_classifier. Build a stem -> parent_path
        # lookup keyed by the unique-asset-name resolver's stems.
        place_scripts_by_name: dict[str, str] = {}
        for script in self.state.rbx_place.scripts:
            container = getattr(script, "parent_path", None) or "ReplicatedStorage"
            place_scripts_by_name[script.name] = container
        so_map: dict[str, str] = {}
        for asset in so_state.assets:
            stem = unique.get(id(asset))
            if not stem:
                continue
            guid = path_to_guid.get(asset.source_path)
            if not guid:
                try:
                    guid = path_to_guid.get(asset.source_path.resolve())
                except (OSError, RuntimeError):
                    guid = None
            if not guid:
                continue
            container = place_scripts_by_name.get(stem, "ReplicatedStorage")
            so_map[guid] = f"{container}.{stem}"
        if so_map:
            scene_runtime["scriptable_objects"] = so_map
            log.info(
                "[write_output] scene-runtime generic: emitted %d "
                "scriptable_object refs in plan map", len(so_map),
            )

    def _rewrite_scene_runtime_asset_refs(
        self, scene_runtime: dict[str, object],
    ) -> None:
        """R2-P1.3: rewrite every ``target_kind == "asset"`` reference's
        ``target_ref`` from raw Unity GUID to ``rbxassetid://...`` using
        ``ctx.uploaded_assets`` as the source of truth. The contract
        states asset refs arrive at the runtime in rbxassetid form;
        pre-fix the planner persisted the GUID and no later pass
        rewrote it, so module fields backed by sprites/sounds got the
        literal GUID string instead of a usable asset id.

        Idempotent: refs already in ``rbxassetid://`` form are left
        untouched. Unresolvable GUIDs (no entry in uploaded_assets) keep
        the raw GUID so the operator sees the unresolved reference
        rather than silently dropping the ref.
        """
        from core.unity_types import GuidIndex
        uploaded = self.ctx.uploaded_assets or {}
        guid_index = getattr(self.state, "guid_index", None)
        if not uploaded or not isinstance(guid_index, GuidIndex):
            return

        def _resolve(guid: str) -> str | None:
            path = guid_index.resolve(guid)
            if path is None:
                return None
            # uploaded_assets is keyed by string (project-relative or
            # absolute path, depending on the producer). Try the resolved
            # path string first, then a string-cast pass.
            for key in (str(path), path.as_posix()):
                if key in uploaded:
                    return uploaded[key]
            return None

        def _rewrite_ref_list(refs: list) -> int:
            n = 0
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                if ref.get("target_kind") != "asset":
                    continue
                target = ref.get("target_ref", "")
                if not isinstance(target, str) or target.startswith("rbxassetid://"):
                    continue
                asset_url = _resolve(target)
                if asset_url:
                    ref["target_ref"] = asset_url
                    n += 1
            return n

        total = 0
        scenes = scene_runtime.get("scenes", {})
        if isinstance(scenes, dict):
            for scene in scenes.values():
                if isinstance(scene, dict):
                    refs = scene.get("references", [])
                    if isinstance(refs, list):
                        total += _rewrite_ref_list(refs)
        prefabs = scene_runtime.get("prefabs", {})
        if isinstance(prefabs, dict):
            for prefab in prefabs.values():
                if isinstance(prefab, dict):
                    refs = prefab.get("references", [])
                    if isinstance(refs, list):
                        total += _rewrite_ref_list(refs)
        if total:
            log.info(
                "[write_output] scene-runtime generic: rewrote %d asset "
                "refs to rbxassetid form", total,
            )

    def _run_phase(self, phase: str) -> None:
        """Execute a single phase with logging and context tracking."""
        log.info("--- Phase: %s ---", phase)
        self.ctx.current_phase = phase
        start = time.monotonic()

        handler = getattr(self, phase, None)
        if handler is None:
            raise ValueError(f"No handler for phase '{phase}'")

        try:
            handler()
        except Exception as exc:
            self.ctx.errors.append(f"Phase '{phase}' failed: {exc}")
            self.ctx.save(self._context_path)
            log.error("Phase '%s' failed: %s", phase, exc, exc_info=True)
            raise

        elapsed = time.monotonic() - start
        self.ctx.mark_phase_complete(phase)
        self.ctx.save(self._context_path)
        log.info("--- Phase '%s' complete (%.2f s) ---", phase, elapsed)
