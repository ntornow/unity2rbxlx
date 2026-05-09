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
from collections.abc import Iterable
from typing import Any

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
from core.roblox_types import RbxPlace
from converter.animation_converter import AnimationConversionResult
from converter.code_transpiler import TranspilationResult
from converter.material_mapper import MaterialMapping
from converter.scriptable_object_converter import AssetConversionResult
from converter.sprite_extractor import SpriteExtractionResult

log = logging.getLogger(__name__)

# Ordered list of pipeline phases.
PHASES: list[str] = [
    "parse",
    "extract_assets",
    "moderate_assets",
    "upload_assets",
    "resolve_assets",
    "convert_materials",
    "transpile_scripts",
    "convert_animations",
    "convert_scene",
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
        coll_fid = getattr(p, "collision_fidelity", None)
        if coll_fid is not None and coll_fid != 0 and getattr(p, "mesh_id", None):
            return True
        children = getattr(p, "children", None) or []
        if children and _scene_needs_collision_recook(children):
            return True
    return False


@dataclass
class PipelineState:
    """Intermediate state passed between pipeline phases."""

    guid_index: GuidIndex | None = None
    parsed_scene: ParsedScene | None = None
    asset_manifest: AssetManifest | None = None
    material_mappings: dict[str, MaterialMapping] = field(default_factory=dict)
    transpilation_result: TranspilationResult | None = None
    animation_result: AnimationConversionResult | None = None
    rbx_place: RbxPlace | None = None
    prefab_library: PrefabLibrary | None = None
    dependency_map: dict[str, list[str]] = field(default_factory=dict)
    scriptable_objects: AssetConversionResult | None = None
    sprite_result: SpriteExtractionResult | None = None


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
        #      for any of the historic FPS-emitted script names.
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
        scripts_dir = self.output_dir / "scripts"
        if scripts_dir.is_dir():
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
        canonical = self.output_dir / "converted_place.rbxlx"
        if canonical.exists() and canonical not in place_files:
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

        # Shared phases: extract + upload assets, materials, scripts, animations
        # Use the first scene for initial parse (needed for asset extraction)
        self.ctx.selected_scene = str(scene_paths[0])
        from unity.scene_parser import parse_scene
        self.state.parsed_scene = parse_scene(scene_paths[0])
        self.ctx.total_game_objects = len(self.state.parsed_scene.all_nodes)

        # Run shared phases
        for phase in ["extract_assets", "upload_assets", "convert_materials",
                       "transpile_scripts", "convert_animations"]:
            self._run_phase(phase)

        # Per-scene: parse, convert, write
        for scene_path in scene_paths:
            scene_name = scene_path.stem
            log.info("[multi] === Converting scene: %s ===", scene_name)

            self.ctx.selected_scene = str(scene_path)
            try:
                self.state.parsed_scene = parse_scene(scene_path)
            except Exception as exc:
                log.warning("[multi] Failed to parse %s: %s", scene_name, exc)
                continue

            self.ctx.total_game_objects = len(self.state.parsed_scene.all_nodes)

            # Convert scene
            self._run_phase("convert_scene")

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
            self.ctx.warnings.append(f"[extract_assets] {msg}")

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
                self.ctx.warnings.append(f"[extract_assets] {msg}")

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
                                    guid = mat_ref.get('guid', '')
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
                                    mg = mat_ref.get("guid", "")
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

        # Build cross-script dependency map from type references
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
        """Route Unity animations to animator_runtime or inline TweenService.

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
        # Check if all uploaded meshes have been resolved, not just some
        uploaded_mesh_count = sum(
            1 for k in self.ctx.uploaded_assets
            if any(k.lower().endswith(ext) for ext in ('.fbx', '.obj'))
        ) if self.ctx.uploaded_assets else 0
        resolved_count = len(self.ctx.mesh_native_sizes) if self.ctx.mesh_native_sizes else 0
        all_meshes_resolved = (
            resolved_count > 0 and resolved_count >= uploaded_mesh_count
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
        already_resolved = self.ctx.mesh_native_sizes or {}
        mesh_assets = {} if all_meshes_resolved else {
            k: v for k, v in self.ctx.uploaded_assets.items()
            if any(k.lower().endswith(ext) for ext in ('.fbx', '.obj'))
            and k not in already_resolved
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
        # Process in batches to stay within script size limits
        from roblox.cloud_api import execute_luau

        batch_size = 20
        mesh_items = list(mesh_assets.items())
        all_results = []

        for batch_start in range(0, len(mesh_items), batch_size):
            batch = mesh_items[batch_start:batch_start + batch_size]
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
    model:Destroy(); task.wait(0.3)
end
return table.concat(allData, "\\n")'''

            result = execute_luau(api_key, universe_id, place_id, script)
            if result and result.get("state") == "COMPLETE":
                # Extract the return value from the result
                outputs = result.get("output", {})
                results_list = outputs.get("results", [])
                if results_list:
                    # The return value is in the first result
                    ret = results_list[0]
                    if isinstance(ret, dict):
                        text = ret.get("value", "")
                    else:
                        text = str(ret)
                    if text:
                        all_results.extend(text.strip().split("\n"))
                        log.info("[resolve_assets] Batch %d: resolved %d sub-meshes",
                                 batch_start // batch_size + 1, len(text.strip().split("\n")))
            else:
                log.warning("[resolve_assets] Batch %d failed", batch_start // batch_size + 1)

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

            # Merge into existing tables instead of replacing. A transient
            # batch failure during a force-rerun would otherwise shrink a
            # prior mostly-complete resolution by overwriting it with this
            # run's smaller result set.
            merged_sizes = {**already_resolved, **mesh_native_sizes}
            existing_hierarchies = self.ctx.mesh_hierarchies or {}
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

        # Load mesh native sizes if available in context
        mesh_native_sizes = {}
        raw_sizes = getattr(self.ctx, "mesh_native_sizes", None)
        if isinstance(raw_sizes, dict):
            for k, v in raw_sizes.items():
                if isinstance(v, (list, tuple)) and len(v) == 3:
                    mesh_native_sizes[k] = tuple(v)

        # Load mesh texture IDs if available in context
        mesh_texture_ids = getattr(self.ctx, "mesh_texture_ids", None) or {}

        # Pre-seed the scene converter's prefab cache to avoid re-parsing
        if self.state.prefab_library and self.state.guid_index:
            from converter.scene_converter import _prefab_lib_cache
            cache_key = str(self.state.guid_index.project_root)
            if cache_key not in _prefab_lib_cache:
                _prefab_lib_cache[cache_key] = self.state.prefab_library

        # Load mesh hierarchies from context (populated by Studio resolution)
        mesh_hierarchies = getattr(self.ctx, "mesh_hierarchies", None) or {}

        # Load FBX bounding boxes (fallback for InitialSize when Studio not available)
        fbx_bounding_boxes: dict[str, tuple[float, float, float]] = {}
        raw_bboxes = getattr(self.ctx, "fbx_bounding_boxes", None)
        if isinstance(raw_bboxes, dict):
            for k, v in raw_bboxes.items():
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
        "_subphase_emit_scripts_to_disk",
        "_subphase_cohere_scripts",
        "_classify_storage",
        "_bind_scripts_to_parts",
        "_subphase_inject_autogen_scripts",
        "_inject_runtime_modules",
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
        self._subphase_emit_scripts_to_disk()
        self._subphase_cohere_scripts()
        self._classify_storage()
        self._bind_scripts_to_parts()
        self._subphase_inject_autogen_scripts()
        self._inject_runtime_modules()
        self._generate_prefab_packages()
        self._subphase_encode_terrain()
        self._subphase_inject_mesh_loader()

        self._subphase_patch_setup_sounds()
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
        import re as _re_post
        raw_xml = rbxlx_path.read_text(encoding="utf-8")
        # Remove <Content name="..."><url>LOCAL_PATH</url></Content> entries
        # where the URL is a local path (contains / or \ but not rbxassetid)
        original_len = len(raw_xml)
        pattern = r'<Content name="[^"]*">\s*<url>[^<]*(?:/|\\)[^<]*</url>\s*</Content>'
        matches = list(_re_post.finditer(pattern, raw_xml))
        stripped = 0
        for m in matches:
            if "rbxassetid" not in m.group():
                stripped += 1
        if stripped:
            raw_xml = _re_post.sub(
                lambda m: "" if "rbxassetid" not in m.group() else m.group(),
                pattern, raw_xml,
            )
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
        if self.state.scriptable_objects:
            so_dir = scripts_dir / "scriptable_objects"
            so_dir.mkdir(parents=True, exist_ok=True)
            for asset in self.state.scriptable_objects.assets:
                (so_dir / f"{asset.asset_name}.luau").write_text(
                    asset.luau_source, encoding="utf-8",
                )

        if preserve_scripts:
            self._rehydrate_scripts_from_disk(scripts_dir)

        elif self.state.transpilation_result:
            from core.roblox_types import RbxScript
            for ts in self.state.transpilation_result.scripts:
                out_path = scripts_dir / ts.output_filename
                out_path.write_text(ts.luau_source, encoding="utf-8")
                self.state.rbx_place.scripts.append(RbxScript(
                    name=ts.output_filename.replace(".luau", ""),
                    source=ts.luau_source,
                    script_type=ts.script_type,
                    source_path=ts.output_filename,
                ))

        # Write animation scripts to output directory AND add to RbxPlace.
        if self.state.animation_result and self.state.animation_result.generated_scripts:
            from core.roblox_types import RbxScript
            anim_scripts_dir = scripts_dir / "animations"
            anim_scripts_dir.mkdir(parents=True, exist_ok=True)
            for script_name, luau_source in self.state.animation_result.generated_scripts:
                out_path = anim_scripts_dir / f"{script_name}.luau"
                out_path.write_text(luau_source, encoding="utf-8")
                self.state.rbx_place.scripts.append(RbxScript(
                    name=script_name,
                    source=luau_source,
                    script_type="Script",
                    source_path=f"animations/{script_name}.luau",
                ))
            log.info("[write_output] Wrote %d animation scripts",
                     len(self.state.animation_result.generated_scripts))

        # Write animation data ModuleScripts to ReplicatedStorage.
        if self.state.animation_result and self.state.animation_result.animation_data_modules:
            from core.roblox_types import RbxScript
            anim_data_dir = scripts_dir / "animation_data"
            anim_data_dir.mkdir(parents=True, exist_ok=True)
            for module_name, module_source in self.state.animation_result.animation_data_modules:
                out_path = anim_data_dir / f"{module_name}.luau"
                out_path.write_text(module_source, encoding="utf-8")
                self.state.rbx_place.scripts.append(RbxScript(
                    name=module_name,
                    source=module_source,
                    script_type="ModuleScript",
                    source_path=f"animation_data/{module_name}.luau",
                ))
            log.info("[write_output] Wrote %d animation data modules",
                     len(self.state.animation_result.animation_data_modules))

        # Attach ScriptableObject ModuleScripts on the fresh-transpile path.
        # Rehydration already picks them up from disk; dedupe by name.
        if self.state.scriptable_objects:
            from core.roblox_types import RbxScript
            existing = {s.name for s in self.state.rbx_place.scripts}
            added = 0
            for asset in self.state.scriptable_objects.assets:
                if asset.asset_name in existing:
                    continue
                self.state.rbx_place.scripts.append(RbxScript(
                    name=asset.asset_name,
                    source=asset.luau_source,
                    script_type="ModuleScript",
                    source_path=f"scriptable_objects/{asset.asset_name}.luau",
                ))
                added += 1
            if added:
                log.info("[write_output] Added %d ScriptableObject ModuleScripts", added)

    def _subphase_cohere_scripts(self) -> None:
        """Post-transpile script coherence: rewrite asset references, inject
        cross-script ``require()`` calls, and reclassify Script→ModuleScript
        based on require dependencies."""
        # Post-transpilation: rewrite asset references in scripts.
        from converter.script_asset_rewriter import rewrite_asset_references
        rewrites = rewrite_asset_references(
            self.state.rbx_place.scripts,
            self.ctx.uploaded_assets,
            self.state.guid_index,
        )
        if rewrites:
            log.info("[write_output] Rewrote asset references in %d scripts", rewrites)

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
        side_effect_modules = []
        for s in self.state.rbx_place.scripts:
            if s.script_type != "ModuleScript":
                continue
            if not any(_re.search(p, s.source) for p in _side_effect_patterns):
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
            from core.roblox_types import RbxScript
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
        fps_names = {
            "AutoFpsHudController", "FPSController", "HUDController",
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
        self.state.rbx_place.scripts = kept

        # The FPS HUD ScreenGui carries a name marker — drop it on
        # opt-out so the player doesn't inherit a stale HUD shell
        # with no controller wiring.
        original_guis = self.state.rbx_place.screen_guis
        kept_guis = [sg for sg in original_guis if sg.name != "HUD"]
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
            from core.roblox_types import RbxScript
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

    def _subphase_finalize_scripts_to_disk(self) -> None:
        """Write every script's final source back to disk. Runs after every
        in-memory mutation so the on-disk ``scripts/`` tree mirrors what
        gets serialized into the rbxlx."""
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
        for s in self.state.rbx_place.scripts:
            if getattr(s, "source_path", None):
                out_path = scripts_dir / s.source_path
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(s.source, encoding="utf-8")
                continue
            luau_path = scripts_dir / f"{s.name}.luau"
            anim_path = scripts_dir / "animations" / f"{s.name}.luau"
            if anim_path.exists():
                anim_path.write_text(s.source, encoding="utf-8")
            elif luau_path.exists() or not (scripts_dir / "animations").exists():
                luau_path.write_text(s.source, encoding="utf-8")


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

        out_path = self.output_dir / UNCONVERTED_FILENAME
        if not sections:
            if out_path.exists():
                out_path.unlink()
            return

        lines = [
            "# UNCONVERTED",
            "",
            "Features the converter deliberately dropped from this run. "
            "Each bullet is a feature that has no in-policy Roblox "
            "equivalent (or requires source data the converter can't "
            "parse yet). See TODO.md + the Phase 4 plan for roadmap.",
            "",
        ]
        for category in sorted(sections):
            lines.append(f"## {category}")
            lines.append("")
            for entry in sections[category]:
                item = entry.get("item", "?")
                reason = entry.get("reason", "")
                lines.append(f"- `{item}` — {reason}")
            lines.append("")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        log.info(
            "[write_output] UNCONVERTED.md written (%d entries across %d categories)",
            sum(len(v) for v in sections.values()), len(sections),
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
    ) -> Any:
        """Assemble the structured ConversionReport for write_output."""
        from converter.report_generator import (
            ConversionReport, AssetSummary, ScriptSummary, MaterialSummary,
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
        from core.roblox_types import RbxScript

        plan_lookup = self._load_storage_plan_for_rehydration()
        luau_files = sorted(scripts_dir.rglob("*.luau"))
        from_plan = 0
        for luau_path in luau_files:
            source = luau_path.read_text(encoding="utf-8")
            name = luau_path.stem

            if name in plan_lookup:
                script_type, parent_path = plan_lookup[name]
                from_plan += 1
            else:
                script_type = "Script"
                parent_path = None
                if source.rstrip().endswith("return " + name) or "\nreturn " in source:
                    script_type = "ModuleScript"
                elif "game.Players.LocalPlayer" in source or "UserInputService" in source:
                    script_type = "LocalScript"

            script = RbxScript(
                name=name,
                source=source,
                script_type=script_type,
                source_path=str(luau_path.relative_to(scripts_dir)),
            )
            if parent_path and hasattr(script, "parent_path"):
                script.parent_path = parent_path
            self.state.rbx_place.scripts.append(script)

        log.info(
            "[write_output] Rehydrated %d scripts from disk (%d via plan, %d via heuristic)",
            len(luau_files), from_plan, len(luau_files) - from_plan,
        )

    def _load_storage_plan_for_rehydration(self) -> dict[str, tuple[str, str | None]]:
        """Load conversion_plan.json into `name -> (script_type, parent_path)`.

        Returns {} on missing or malformed plan.
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
        category_map = [
            ("server_scripts",           "Script",       "ServerScriptService"),
            ("client_scripts",           "LocalScript",  "StarterPlayer.StarterPlayerScripts"),
            ("character_scripts",        "LocalScript",  "StarterPlayer.StarterCharacterScripts"),
            ("replicated_first_scripts", "ModuleScript", "ReplicatedFirst"),
            ("shared_modules",           "ModuleScript", "ReplicatedStorage"),
            ("server_modules",           "ModuleScript", "ServerStorage"),
        ]
        lookup: dict[str, tuple[str, str | None]] = {}
        for cat_key, script_type, parent_path in category_map:
            for name in plan.get(cat_key, []) or []:
                lookup[name] = (script_type, parent_path)
        return lookup

    def _classify_storage(self) -> None:
        """Phase 4a.5: run the storage classifier on populated scripts.

        Assigns each RbxScript a concrete ``parent_path`` based on call-graph
        analysis + client/server API detection. Persists the resulting plan
        to ``self.ctx.storage_plan`` and to ``conversion_plan.json`` in the
        output directory.

        Safe to call multiple times — the classifier is idempotent.
        """
        if self.state.rbx_place is None or not self.state.rbx_place.scripts:
            return

        from converter.storage_classifier import classify_storage
        import json as _json

        plan = classify_storage(
            self.state.rbx_place.scripts,
            dependency_map=self.state.dependency_map or None,
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

        plan_path = self.output_dir / "conversion_plan.json"
        plan_path.write_text(
            _json.dumps({
                "storage_plan": plan.to_dict(),
                "script_paths": script_paths,
                "animation_routing": animation_routing,
            }, indent=2),
            encoding="utf-8",
        )
        log.info(
            "[classify_storage] %d scripts classified (plan written to %s)",
            len(plan.decisions),
            plan_path.name,
        )

    def _bind_scripts_to_parts(self) -> None:
        """Bind transpiled scripts to their target parts using _ScriptClass attributes.

        In Unity, MonoBehaviour scripts are children of GameObjects. This method
        replicates that by moving scripts from the global place.scripts list to
        part.scripts, so they become children in the rbxlx hierarchy.

        Scripts placed as children of parts can use `script.Parent` to reference
        their target part directly — matching Unity's MonoBehaviour pattern.
        """
        from core.roblox_types import RbxScript

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

        # Disable unbound scripts that depend on script.Parent being a Part/Light/etc.
        # These scripts are prefab components that couldn't be bound to parts.
        # In SSS/RS, script.Parent is the service itself, so Position/CFrame/etc. will crash.
        import re
        _parent_part_patterns = [
            r'script\.Parent\.Position',
            r'script\.Parent\.CFrame',
            r'script\.Parent:FindFirstChild',
            r'script\.Parent\.Touched',
            r'script\.Parent\.AssemblyLinearVelocity',
            r'local \w+ = script\.Parent\b',  # alias like `local part = script.Parent`
        ]
        # Scripts that already gate on ``script.Parent:IsA(...)`` carry
        # their own conditional binding (smart-binding animation scripts
        # do this — see generate_tween_script's prefab_scoped path).
        # The blanket BasePart guard would short-circuit a Model-targeted
        # check before it ever runs, breaking the script's own logic.
        _self_guard_patterns = (
            re.compile(r'script\.Parent\s*:\s*IsA\s*\(\s*["\']Model["\']'),
            re.compile(r'script\.Parent\s*:\s*IsA\s*\(\s*["\']BasePart["\']'),
        )
        disabled_count = 0
        for s in list(self.state.rbx_place.scripts):
            if s.script_type == "ModuleScript":
                continue
            needs_parent_part = any(
                re.search(pat, s.source) for pat in _parent_part_patterns
            )
            if not needs_parent_part:
                continue
            if any(p.search(s.source) for p in _self_guard_patterns):
                # Self-guarded — let the script make its own decision.
                continue
            # Wrap script with a parent type check
            guard = ('-- Guard: this script expects script.Parent to be a BasePart\n'
                     'if not script.Parent:IsA("BasePart") then return end\n\n')
            s.source = guard + s.source
            disabled_count += 1
            log.debug("[write_output]   Added parent guard to '%s'", s.name)
        if disabled_count:
            log.info("[write_output] Added BasePart parent guards to %d unbound scripts", disabled_count)

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

        result = generate_prefab_packages(
            prefab_library=prefab_library,
            serialized_field_refs=self.ctx.serialized_field_refs or None,
            guid_index=self.state.guid_index,
            material_mappings=self.state.material_mappings,
            uploaded_assets=self.ctx.uploaded_assets,
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

    def _inject_runtime_modules(self) -> None:
        """Inject runtime library ModuleScripts when relevant features are detected.

        Scans the place's scripts and parts for features that need runtime support:
        - HasAnimator attribute → inject animator_runtime.luau
        - NavMeshAgent attributes → inject nav_mesh_runtime.luau
        - Canvas/ScreenGui elements → inject event_system.luau
        - CharacterController attributes → inject physics_bridge.luau
        """
        from core.roblox_types import RbxScript
        runtime_dir = Path(__file__).parent.parent / "runtime"
        injected = 0

        # Detect features from parts (recursively check all children)
        has_animator = False
        has_navmesh = False
        has_character_controller = False
        has_cinemachine = False
        has_sub_emitters = False
        # has_pickups removed — scripts propagated automatically now

        def _scan_parts(parts):
            nonlocal has_animator, has_navmesh, has_character_controller, has_cinemachine, has_sub_emitters
            for part in parts:
                attrs = getattr(part, "attributes", {})
                if attrs.get("HasAnimator"):
                    has_animator = True
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
        if has_animator:
            modules_to_inject.append(("AnimatorRuntime", "animator_runtime.luau"))
        if has_navmesh:
            modules_to_inject.append(("NavAgent", "nav_mesh_runtime.luau"))
        if has_ui:
            modules_to_inject.append(("EventSystem", "event_system.luau"))
        if has_character_controller:
            modules_to_inject.append(("CharacterBridge", "physics_bridge.luau"))
        if has_sub_emitters:
            modules_to_inject.append(("SubEmitterRuntime", "sub_emitter_runtime.luau"))
        # FPS scaffolding's auto-generated HUDController requires
        # ``EventDispatch.connectClient`` from this runtime module to
        # bridge RemoteEvent vs BindableEvent producers. Inject when
        # opted in via ``--scaffolding=fps`` so the require resolves;
        # without this the HUD load fails at WaitForChild on the
        # missing ModuleScript.
        if "fps" in self.scaffolding:
            modules_to_inject.append(("EventDispatch", "event_dispatch.luau"))

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
