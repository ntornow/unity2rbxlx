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
from typing import Any

import config as _config
from config import (
    ANTHROPIC_API_KEY,
    OUTPUT_DIR,
    RBXLX_OUTPUT_FILENAME,
)
from core.conversion_context import ConversionContext
from core.unity_types import (
    AssetManifest,
    GuidIndex,
    ParsedScene,
)
from core.roblox_types import RbxPlace

log = logging.getLogger(__name__)

# Ordered list of pipeline phases.
PHASES: list[str] = [
    "parse",
    "extract_assets",
    "upload_assets",
    "convert_materials",
    "transpile_scripts",
    "convert_animations",
    "convert_scene",
    "write_output",
]


@dataclass
class PipelineState:
    """Intermediate state passed between pipeline phases."""

    guid_index: GuidIndex | None = None
    parsed_scene: ParsedScene | None = None
    asset_manifest: AssetManifest | None = None
    material_mappings: dict[str, Any] = field(default_factory=dict)
    transpilation_result: Any = None
    animation_result: Any = None
    rbx_place: RbxPlace | None = None
    prefab_library: Any = None
    dependency_map: dict[str, list[str]] = field(default_factory=dict)


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
    ) -> None:
        self.unity_project_path = self._find_unity_root(Path(unity_project_path).resolve())
        self.output_dir = Path(output_dir or OUTPUT_DIR).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.skip_upload = skip_upload

        self.ctx = ConversionContext(
            unity_project_path=str(self.unity_project_path),
        )
        self.state = PipelineState()

        self._context_path = self.output_dir / "conversion_context.json"

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

        # Reload context if available.
        if self._context_path.exists():
            self.ctx = ConversionContext.load(self._context_path)
            log.info("Loaded persisted context from %s", self._context_path)

        idx = PHASES.index(phase)
        log.info("=== Resuming pipeline from phase '%s' ===", phase)

        # Always re-run phases that produce in-memory state not persisted to context.
        essential_phases = {"parse", "extract_assets", "convert_materials",
                           "transpile_scripts", "convert_animations", "convert_scene"}
        for prior_phase in PHASES[:idx]:
            if prior_phase not in self.ctx.completed_phases or prior_phase in essential_phases:
                log.info("Running prerequisite phase '%s'", prior_phase)
                self._run_phase(prior_phase)

        # Run the requested phase and everything after it.
        for remaining_phase in PHASES[idx:]:
            self._run_phase(remaining_phase)

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

        # Pre-compute FBX bounding boxes via trimesh for InitialSize fallback.
        # This runs only when mesh_native_sizes (from Studio resolution) are
        # not yet available, so the convert_scene phase has real geometry data
        # instead of assuming every mesh is a 1-unit cube.
        if not self.ctx.mesh_native_sizes:
            self._compute_fbx_bounding_boxes()

    def _compute_fbx_bounding_boxes(self) -> None:
        """Scan all mesh assets and compute bounding boxes via trimesh."""
        manifest = self.state.asset_manifest
        if not manifest:
            return

        from converter.mesh_processor import get_mesh_info

        mesh_assets = [a for a in manifest.assets if a.kind == "mesh"]
        if not mesh_assets:
            return

        computed = 0
        for asset in mesh_assets:
            rel_key = str(asset.relative_path)
            # Skip if already computed (e.g. from a resumed context)
            if rel_key in self.ctx.fbx_bounding_boxes:
                computed += 1
                continue
            info = get_mesh_info(asset.path)
            bbox = info.get("bounding_box")
            if bbox and isinstance(bbox, tuple) and len(bbox) == 3:
                # Only store non-trivial bounding boxes (not the 1,1,1 fallback)
                if not (bbox[0] == 1.0 and bbox[1] == 1.0 and bbox[2] == 1.0
                        and info.get("face_count", 0) == 0):
                    self.ctx.fbx_bounding_boxes[rel_key] = list(bbox)
                    computed += 1

        if computed:
            log.info("[extract_assets] Computed FBX bounding boxes for %d meshes", computed)

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

        for kind, uploader, extensions in [
            ("texture", upload_image, {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".psd"}),
            ("mesh", upload_mesh, {".fbx", ".obj"}),
            ("audio", upload_audio, {".mp3", ".ogg", ".wav", ".flac"}),
        ]:
            assets = manifest.by_kind.get(kind, [])
            eligible = [a for a in assets if a.path.suffix.lower() in extensions]
            log.info("[upload_assets] Uploading %d %s assets...", len(eligible), kind)

            for asset in eligible:
                rel = str(asset.relative_path)
                if rel in uploaded:
                    continue  # Already uploaded (resume support)

                upload_path = asset.path
                name = asset.path.stem

                # Auto-convert non-PNG/JPG formats to PNG before uploading
                if kind == "texture" and asset.path.suffix.lower() in (".bmp", ".tga", ".tif", ".tiff", ".psd"):
                    try:
                        png_path = convert_dir / (asset.path.stem + ".png")
                        upload_path = convert_to_png(asset.path, png_path)
                    except Exception as exc:
                        log.warning("[upload_assets] Failed to convert %s to PNG: %s", asset.path.name, exc)
                        self.ctx.asset_upload_errors.append(rel)
                        continue

                result = uploader(upload_path, api_key, creator_id, creator_type, name)
                if result:
                    uploaded[rel] = f"rbxassetid://{result}"
                    log.info("[upload_assets]   %s -> rbxassetid://%s  (source: %s)", name, result, rel)
                else:
                    log.warning("[upload_assets]   FAILED: %s  (source: %s)", name, rel)
                    self.ctx.asset_upload_errors.append(rel)
                time.sleep(0.3)  # Rate limit (Roblox Open Cloud allows ~60 req/min)

        log.info("[upload_assets] %d assets uploaded, %d errors",
                 len(uploaded), len(self.ctx.asset_upload_errors))

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
        from utils.image_processing import extract_channel, invert_image, convert_to_png
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
                    elif op.operation == "invert_a":
                        invert_image(actual_source, output)
                    elif op.operation == "copy":
                        import shutil
                        shutil.copy2(source, output)
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
            api_key=ANTHROPIC_API_KEY,
        )
        self.ctx.transpiled_scripts = self.state.transpilation_result.total_transpiled
        log.info(
            "[transpile_scripts] %d / %d scripts transpiled",
            self.ctx.transpiled_scripts,
            self.ctx.total_scripts,
        )

    def convert_animations(self) -> None:
        """Phase 5a: Convert Unity animations to Roblox TweenService scripts."""
        log.info("[convert_animations] Discovering and converting animations ...")
        from converter.animation_converter import convert_animations as _convert_anims

        self.state.animation_result = _convert_anims(
            unity_project_path=self.unity_project_path,
            guid_index=self.state.guid_index,
        )
        self.ctx.total_animations = self.state.animation_result.total_clips
        self.ctx.converted_animations = self.state.animation_result.total_scripts_generated
        log.info(
            "[convert_animations] %d clips, %d controllers, %d scripts generated",
            self.state.animation_result.total_clips,
            self.state.animation_result.total_controllers,
            self.state.animation_result.total_scripts_generated,
        )

    def convert_scene(self) -> None:
        """Phase 5b: Convert the parsed scene hierarchy to Roblox parts."""
        log.info("[convert_scene] Converting scene hierarchy ...")
        from converter.scene_converter import convert_scene  # type: ignore[import-untyped]

        if self.state.parsed_scene is None:
            log.warning("[convert_scene] No parsed scene -- skipping")
            return

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

    def write_output(self) -> None:
        """Phase 6: Serialize the Roblox place to disk."""
        log.info("[write_output] Writing output ...")

        if self.state.rbx_place is None:
            log.warning("[write_output] No RbxPlace -- skipping")
            return

        # Write transpiled scripts to output directory AND add to RbxPlace.
        scripts_dir = self.output_dir / "scripts"
        # Clean old scripts from previous runs to avoid stale files
        if scripts_dir.exists():
            import shutil
            shutil.rmtree(scripts_dir)
        scripts_dir.mkdir(parents=True, exist_ok=True)

        if self.state.transpilation_result:
            from core.roblox_types import RbxScript
            from converter.luau_validator import validate_and_fix, fix_gameplay_patterns
            total_fixes = 0
            for ts in self.state.transpilation_result.scripts:
                # Validate and fix common AI transpilation issues.
                fixed_source, fixes = validate_and_fix(ts.output_filename, ts.luau_source)
                if fixes:
                    ts.luau_source = fixed_source
                    total_fixes += len(fixes)
                # Fix gameplay-specific patterns (pickup detection, etc.)
                fixed_source, gp_fixes = fix_gameplay_patterns(ts.output_filename, ts.luau_source)
                if gp_fixes:
                    ts.luau_source = fixed_source
                    total_fixes += len(gp_fixes)
                out_path = scripts_dir / ts.output_filename
                out_path.write_text(ts.luau_source, encoding="utf-8")
                self.state.rbx_place.scripts.append(RbxScript(
                    name=ts.output_filename.replace(".luau", ""),
                    source=ts.luau_source,
                    script_type=ts.script_type,
                ))
            if total_fixes:
                log.info("[write_output] Applied %d Luau validation fixes", total_fixes)

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
                ))
            log.info("[write_output] Wrote %d animation data modules",
                     len(self.state.animation_result.animation_data_modules))

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

        # Bind scripts to their target parts using _ScriptClass attributes.
        # In Unity, MonoBehaviours are children of GameObjects. We replicate
        # this by placing scripts as children of their target parts, which
        # allows scripts to use script.Parent to reference their part.
        self._bind_scripts_to_parts()

        # Auto-generate collision group setup if Unity layers are used.
        from converter.fps_client_generator import generate_collision_group_script
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
        from converter.fps_client_generator import generate_game_server_script
        existing_server_mgr = [s for s in self.state.rbx_place.scripts if s.name == "GameServerManager"]
        if not existing_server_mgr:
            self.state.rbx_place.scripts.append(generate_game_server_script())
            log.info("[write_output] Injected GameServerManager script")

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
        side_effect_modules = []
        for s in self.state.rbx_place.scripts:
            if s.script_type != "ModuleScript":
                continue
            if any(_re.search(p, s.source) for p in _side_effect_patterns):
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
                bootstrap_lines.append('-- Hide local character for first-person view')
                bootstrap_lines.append('local Players = game:GetService("Players")')
                bootstrap_lines.append('local lp = Players.LocalPlayer')
                bootstrap_lines.append('local function hideCharacter(char)')
                bootstrap_lines.append('    if not char then return end')
                bootstrap_lines.append('    for _, part in char:GetDescendants() do')
                bootstrap_lines.append('        if part:IsA("BasePart") or part:IsA("Decal") or part:IsA("MeshPart") then')
                bootstrap_lines.append('            part.LocalTransparencyModifier = 1')
                bootstrap_lines.append('        end')
                bootstrap_lines.append('    end')
                bootstrap_lines.append('    char.DescendantAdded:Connect(function(desc)')
                bootstrap_lines.append('        if desc:IsA("BasePart") or desc:IsA("Decal") or desc:IsA("MeshPart") then')
                bootstrap_lines.append('            desc.LocalTransparencyModifier = 1')
                bootstrap_lines.append('        end')
                bootstrap_lines.append('    end)')
                bootstrap_lines.append('end')
                bootstrap_lines.append('hideCharacter(lp.Character)')
                bootstrap_lines.append('lp.CharacterAdded:Connect(hideCharacter)')
                bootstrap_lines.append('')
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

        # Auto-generate client scripts and HUD for FPS-style games.
        from converter.fps_client_generator import inject_fps_scripts
        fps_added = inject_fps_scripts(self.state.rbx_place)
        if fps_added:
            log.info("[write_output] Auto-generated %d FPS client scripts/GUIs", fps_added)

        # Inject runtime library modules when relevant features are detected.
        self._inject_runtime_modules()

        # Encode terrain heightmap data into SmoothGrid binary for rbxlx embedding.
        # Also save a Luau script as fallback for environments without UnityPy.
        if self.state.rbx_place.terrains:
            from converter.terrain_converter import read_unity_terrain, generate_terrain_luau
            for terrain_obj in self.state.rbx_place.terrains:
                if terrain_obj.terrain_data_guid and self.state.guid_index:
                    td_path = self.state.guid_index.resolve(terrain_obj.terrain_data_guid)
                    if td_path and td_path.exists():
                        terrain_data = read_unity_terrain(td_path)
                        if terrain_data:
                            from core.coordinate_system import unity_to_roblox_pos
                            rpos = unity_to_roblox_pos(*terrain_obj.position)
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
                            # Generate terrain FillBlock script and embed in rbxlx
                            luau = generate_terrain_luau(terrain_data, rpos, voxel_size=16)
                            terrain_path = self.output_dir / "generate_terrain.luau"
                            terrain_path.write_text(luau, encoding="utf-8")
                            log.info("[write_output] Terrain script saved to %s (%d chars)",
                                     terrain_path.name, len(luau))
                            # Inject as a server script so terrain generates on game start
                            from core.roblox_types import RbxScript
                            terrain_script = RbxScript(
                                name="TerrainGenerator",
                                source=(
                                    "-- Auto-generated terrain from Unity heightmap\n"
                                    "-- Runs once on server start, then disables itself\n"
                                    "if script:GetAttribute('TerrainGenerated') then return end\n\n"
                                    + luau + "\n\n"
                                    "script:SetAttribute('TerrainGenerated', true)\n"
                                    "script.Disabled = true\n"
                                ),
                                script_type="Script",
                            )
                            self.state.rbx_place.scripts.append(terrain_script)
                            log.info("[write_output] TerrainGenerator script embedded in rbxlx")

        # Generate MeshLoader script to load meshes at runtime.
        # Uploaded mesh Model IDs need InsertService resolution to get real MeshIds.
        # Textures are uploaded as Image type and work directly — no resolution needed.
        if self.ctx.uploaded_assets:
            from core.roblox_types import RbxScript
            mesh_loader = '''-- Auto-generated mesh loader
-- Resolves Model IDs → real MeshIds, then rebuilds MeshParts with proper meshes
if script:GetAttribute("MeshesLoaded") then return end

local AssetService = game:GetService("AssetService")
local InsertService = game:GetService("InsertService")
local loaded = 0
local failed = 0

-- Step 1: Collect unique Model IDs to resolve
local modelToMesh = {}
local uniqueMeshIds = {}
local meshIdSet = {}

for _, part in workspace:GetDescendants() do
    if part:IsA("MeshPart") then
        local mid = part:GetAttribute("_MeshId")
        if mid and not meshIdSet[mid] then
            meshIdSet[mid] = true
            table.insert(uniqueMeshIds, mid)
        end
    end
end

-- Step 2: Resolve Model IDs → real MeshIds via InsertService:LoadAsset
local meshResolved = 0
for _, modelUrl in uniqueMeshIds do
    local numId = tonumber(modelUrl:match("(%d+)"))
    if numId then
        local ok, model = pcall(function() return InsertService:LoadAsset(numId) end)
        if ok and model then
            for _, desc in model:GetDescendants() do
                if desc:IsA("MeshPart") and desc.MeshId ~= "" then
                    modelToMesh[modelUrl] = {
                        meshId = desc.MeshId,
                        initialSize = desc.Size,
                    }
                    meshResolved = meshResolved + 1
                    break
                end
            end
            model:Destroy()
        end
    end
    task.wait()
end
print(string.format("MeshLoader: resolved %d/%d mesh Model IDs", meshResolved, #uniqueMeshIds))

-- Step 3: Replace placeholder MeshParts with real meshes
for _, part in workspace:GetDescendants() do
    if part:IsA("MeshPart") and part:GetAttribute("_MeshId") then
        local resolved = modelToMesh[part:GetAttribute("_MeshId")]
        if not resolved then failed = failed + 1; continue end

        local ok, newPart = pcall(function()
            return AssetService:CreateMeshPartAsync(resolved.meshId)
        end)
        if ok and newPart then
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
            if scaleX and scaleY and scaleZ and resolved.initialSize then
                local init = resolved.initialSize
                newPart.Size = Vector3.new(
                    init.X * math.abs(scaleX),
                    init.Y * math.abs(scaleY),
                    init.Z * math.abs(scaleZ)
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
        else
            failed = failed + 1
        end

        if loaded % 20 == 0 then task.wait() end
    end
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

        # Final validation pass: apply validator fixes to all scripts one last time
        # (catches patterns introduced by require injection, reclassification, etc.)
        from converter.luau_validator import validate_and_fix
        final_fixes = 0
        for s in self.state.rbx_place.scripts:
            fixed_source, fixes = validate_and_fix(s.name, s.source)
            if fixes:
                s.source = fixed_source
                final_fixes += len(fixes)
        if final_fixes:
            log.info("[write_output] Final validation pass applied %d fixes", final_fixes)

        # Final write: ensure .luau files on disk match the fully processed sources
        # (after require injection, reclassification, and all other post-processing)
        scripts_dir = self.output_dir / "scripts"
        for s in self.state.rbx_place.scripts:
            # Check both direct and animations subdirectory
            luau_path = scripts_dir / f"{s.name}.luau"
            anim_path = scripts_dir / "animations" / f"{s.name}.luau"
            if anim_path.exists():
                anim_path.write_text(s.source, encoding="utf-8")
            elif luau_path.exists() or not (scripts_dir / "animations").exists():
                luau_path.write_text(s.source, encoding="utf-8")

        # Write the RBXLX file.
        import config as _cfg_mod
        rbxlx_path = self.output_dir / _cfg_mod.RBXLX_OUTPUT_FILENAME
        from roblox.rbxlx_writer import write_rbxlx
        result = write_rbxlx(self.state.rbx_place, rbxlx_path)
        log.info("[write_output] RBXLX: %s (%d parts, %d scripts)",
                 rbxlx_path, result.get("parts_written", 0),
                 result.get("scripts_written", 0))

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

        # Write conversion report.
        import json as _json
        # Collect script type breakdown
        script_types = {"Script": 0, "LocalScript": 0, "ModuleScript": 0}
        for s in (self.state.rbx_place.scripts or []):
            st = getattr(s, "script_type", "Script")
            script_types[st] = script_types.get(st, 0) + 1

        report = {
            "project": str(self.unity_project_path),
            "scene": self.ctx.selected_scene,
            "output": str(self.output_dir),
            "stats": {
                "game_objects": self.ctx.total_game_objects,
                "parts": self.ctx.converted_parts,
                "scripts": self.ctx.transpiled_scripts,
                "script_types": script_types,
                "materials": f"{self.ctx.converted_materials}/{self.ctx.total_materials}",
                "animations": self.ctx.converted_animations,
                "uploaded_assets": len(self.ctx.uploaded_assets),
                "upload_errors": len(self.ctx.asset_upload_errors),
                "terrains": len(self.state.rbx_place.terrains),
                "screen_guis": len(self.state.rbx_place.screen_guis),
                "streaming_enabled": self.ctx.converted_parts > 5000,
            },
            "elements": result,
            "errors": self.ctx.errors,
            "upload_errors": self.ctx.asset_upload_errors[:20],  # Cap for readability
            "needs_resolution": len(self.ctx.uploaded_assets) > 0 and not self.ctx.mesh_native_sizes,
        }
        report_path = self.output_dir / "conversion_report.json"
        report_path.write_text(_json.dumps(report, indent=2), encoding="utf-8")

        # Persist context.
        self.ctx.save(self._context_path)
        log.info("[write_output] Context saved to %s", self._context_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
                    if class_name in script_by_name and class_name not in bound_script_names:
                        script = script_by_name[class_name]
                        # Only bind Server scripts and LocalScripts to parts
                        # ModuleScripts stay in ReplicatedStorage for require()
                        if script.script_type != "ModuleScript":
                            part.scripts.append(script)
                            bound_script_names.add(class_name)
                            bound_count += 1
                            log.debug("[write_output]   Bound '%s' to part '%s'",
                                      class_name, part.name)

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
        disabled_count = 0
        for s in list(self.state.rbx_place.scripts):
            if s.script_type == "ModuleScript":
                continue
            needs_parent_part = any(
                re.search(pat, s.source) for pat in _parent_part_patterns
            )
            if needs_parent_part:
                # Wrap script with a parent type check
                guard = ('-- Guard: this script expects script.Parent to be a BasePart\n'
                         'if not script.Parent:IsA("BasePart") then return end\n\n')
                s.source = guard + s.source
                disabled_count += 1
                log.debug("[write_output]   Added parent guard to '%s'", s.name)
        if disabled_count:
            log.info("[write_output] Added BasePart parent guards to %d unbound scripts", disabled_count)

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
