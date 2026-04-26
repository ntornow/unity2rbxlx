"""
u2r.py -- Click-based CLI for the Unity -> Roblox converter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from unity.yaml_parser import is_text_yaml as _is_text_yaml
from utils.credentials import resolve_credential as _resolve_credential
from utils.logging_config import setup_logging
from utils.script_cache import scripts_cache_intact


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def main(verbose: bool) -> None:
    """Unity to Roblox Game Converter."""
    setup_logging(level="DEBUG" if verbose else "INFO")


@main.command()
@click.argument("unity_project", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), default="./output",
              help="Output directory")
@click.option("--scene", "-s", type=str, default=None,
              help="Scene to convert (path, or 'all' for every scene)")
@click.option("--phase", type=str, default=None,
              help="Resume from a specific phase")
@click.option("--no-upload", is_flag=True, help="Skip asset upload")
@click.option("--no-ai", is_flag=True, help="Disable AI transpilation")
@click.option("--no-resolve", is_flag=True, help="Skip headless mesh resolution")
@click.option("--api-key", type=str, default=None,
              help="Roblox Open Cloud API key (string or path to file)")
@click.option("--creator-id", type=str, default=None,
              help="Roblox Creator ID (number or path to file)")
@click.option("--universe-id", type=int, default=None,
              help="Roblox Universe ID for mesh resolution (reuse across runs)")
@click.option("--place-id", type=int, default=None,
              help="Roblox Place ID for mesh resolution (reuse across runs)")
def convert(
    unity_project: str,
    output: str,
    scene: str | None,
    phase: str | None,
    no_upload: bool,
    no_ai: bool,
    no_resolve: bool,
    api_key: str | None,
    creator_id: str | None,
    universe_id: int | None,
    place_id: int | None,
) -> None:
    """Convert a Unity project to a Roblox experience.

    Examples:

      python u2r.py convert path/to/UnityProject -o ./output

      python u2r.py convert path/to/UnityProject -o ./output --api-key YOUR_KEY --creator-id 12345

      python u2r.py convert path/to/UnityProject -o ./output --api-key ./apikey --creator-id ./creator_id
    """
    import config
    from converter.pipeline import Pipeline

    project_path = Path(unity_project).resolve()
    output_path = Path(output).resolve()
    output_path.mkdir(parents=True, exist_ok=True)

    # Load API key: from --api-key (string or file), env var, or auto-discover
    import os
    resolved_key = _resolve_credential(api_key, "ROBLOX_API_KEY", "apikey", project_path)
    if resolved_key:
        config.ROBLOX_API_KEY = resolved_key

    # Load creator ID: from --creator-id (string or file), env var, or auto-discover
    resolved_cid = _resolve_credential(creator_id, "ROBLOX_CREATOR_ID", "creator_id", project_path)
    if resolved_cid:
        config.ROBLOX_CREATOR_ID = int(resolved_cid)

    if no_ai:
        config.USE_AI_TRANSPILATION = False

    pipeline = Pipeline(
        unity_project_path=project_path,
        output_dir=output_path,
        skip_upload=no_upload,
    )

    # Plumb --universe-id / --place-id into the pipeline context so the
    # resolve_assets phase can run headless mesh resolution. Without this,
    # the pipeline's resolve_assets warns "no IDs supplied" and the local
    # rbxlx ends up with raw Model IDs (which Studio fails to fetch).
    if universe_id:
        pipeline.context.universe_id = universe_id
    if place_id:
        pipeline.context.place_id = place_id

    # Load previously uploaded assets if available
    uploaded_file = output_path / "uploaded_assets.json"
    if uploaded_file.exists():
        import json
        pipeline.context.uploaded_assets = json.loads(uploaded_file.read_text())
        click.echo(f"Loaded {len(pipeline.context.uploaded_assets)} previously uploaded assets")

    if scene and scene.lower() == "all":
        # Multi-scene mode: convert all scenes in the project
        pipeline.run_all_scenes()
        click.echo(f"\nMulti-scene conversion complete. Output: {output_path}")
        for scene_name, stats in pipeline.context.scenes_metadata.items():
            click.echo(f"  {scene_name}: {stats.get('parts', 0)} parts, {stats.get('scripts', 0)} scripts")
        click.echo(f"\n  Total scenes converted: {len(pipeline.context.scenes_metadata)}")
        return

    if scene:
        pipeline.context.selected_scene = scene

    if phase:
        pipeline.resume(phase)
    else:
        pipeline.run_all()

    click.echo(f"\nConversion complete. Output: {output_path}")
    click.echo(f"  Scene: {Path(pipeline.context.selected_scene).name}")
    click.echo(f"  Parts: {pipeline.context.converted_parts}")
    click.echo(f"  Scripts: {pipeline.context.transpiled_scripts}")
    click.echo(f"  Materials: {pipeline.context.converted_materials}/{pipeline.context.total_materials}")
    if pipeline.context.total_animations:
        click.echo(f"  Animations: {pipeline.context.converted_animations} scripts from {pipeline.context.total_animations} clips")
    uploaded = len(pipeline.context.uploaded_assets)
    errors = len(pipeline.context.asset_upload_errors)
    if uploaded or errors:
        click.echo(f"  Assets uploaded: {uploaded} ({errors} errors)")
    if pipeline.context.warnings:
        click.echo(f"  Warnings: {len(pipeline.context.warnings)}")

    # Headless place publishing: generate Luau script, execute via Open Cloud,
    # and save the place to Roblox with proper mesh geometry embedded.
    rbxlx_file = output_path / "converted_place.rbxlx"
    has_meshes = any(
        p.lower().endswith(('.fbx', '.obj'))
        for p in pipeline.context.uploaded_assets
    ) if pipeline.context.uploaded_assets else False

    if has_meshes and not no_upload and not no_resolve and resolved_key:
        click.echo("\n--- Publishing to Roblox (headless mesh resolution) ---")
        from roblox.id_cache import read_ids, write_ids
        from roblox.place_publisher import publish_place

        uid, pid = universe_id, place_id
        if not uid or not pid:
            cached_uid, cached_pid = read_ids(output_path)
            if cached_uid and cached_pid:
                uid, pid = cached_uid, cached_pid
                click.echo(f"  Reusing universe={uid} place={pid}")
        if not uid or not pid:
            click.echo("  No universe/place IDs found.")
            click.echo("  Create an experience at https://create.roblox.com and pass:")
            click.echo("    --universe-id YOUR_UNIVERSE_ID --place-id YOUR_PLACE_ID")
            click.echo("  IDs will be cached for future runs.")
            click.echo(f"\n  To validate: python u2r.py validate {rbxlx_file}")
            return

        click.echo(f"  Publishing on universe={uid} place={pid}...")
        result = publish_place(
            resolved_key, uid, pid, pipeline.state.rbx_place, output_path,
        )
        click.echo(f"  Script size: {result.total_bytes:,} chars "
                   f"({result.total_bytes/1024:.0f} KB), {result.chunks} chunk(s)")
        click.echo(f"  Script saved to: {result.script_path}")

        if result.exceeded_limit:
            click.echo(f"  WARNING: {result.error}")
            click.echo(f"  Script saved to: {result.script_path} (for manual execution in Studio)")
        elif result.success:
            # Cache IDs only after a successful publish so a typo'd /
            # unauthorized place ID doesn't get stickily reused next run.
            write_ids(output_path, uid, pid)
            click.echo("  Place published successfully!")
            click.echo("\n  Open in Studio: File → Open from Roblox → select the experience")
            click.echo("  Meshes render as proper 3D geometry in edit mode.")
        else:
            click.echo(f"  {result.error}")
            click.echo("  Headless execution failed. The rbxlx still works with runtime MeshLoader.")

    click.echo(f"\n  Local rbxlx: {rbxlx_file}")
    click.echo(f"  To validate: python u2r.py validate {rbxlx_file}")


@main.command()
@click.argument("output_dir", type=click.Path(exists=True))
@click.option("--api-key", type=str, default=None, help="Roblox API key (string or path to file)")
@click.option("--creator-id", type=str, default=None, help="Roblox Creator ID (number or path to file)")
@click.option("--universe-id", type=int, default=None)
@click.option("--place-id", type=int, default=None)
def publish(
    output_dir: str,
    api_key: str | None,
    creator_id: str | None,
    universe_id: int | None,
    place_id: int | None,
) -> None:
    """Publish a previously converted place to Roblox with proper meshes.

    Replays the cached chunks at ``<output>/place_builder_chunks.json`` if
    present (fast path, no Unity project required). Falls back to a fresh
    Pipeline rebuild from the saved ConversionContext if the chunk cache is
    missing — that path requires the original Unity project at the path
    recorded in ``conversion_context.json`` and a creator ID for fresh
    uploads (``--creator-id`` or ``ROBLOX_CREATOR_ID``).
    """
    import config
    output_path = Path(output_dir).resolve()

    from roblox.id_cache import read_ids, write_ids
    from roblox.place_publisher import publish_cached_chunks, publish_place
    from converter.pipeline import Pipeline
    from core.conversion_context import ConversionContext

    # Credential autodiscovery: prefer files next to output_dir, then fall
    # back to the Unity project path recorded in conversion_context.json so
    # the publish flow finds the same apikey/creator_id files that
    # u2r.convert / convert_interactive.assemble already used.
    resolved_key = _resolve_credential(api_key, "ROBLOX_API_KEY", "apikey", output_path)
    resolved_cid = _resolve_credential(creator_id, "ROBLOX_CREATOR_ID", "creator_id", output_path)
    if not resolved_key or not resolved_cid:
        ctx_path_for_creds = output_path / "conversion_context.json"
        if ctx_path_for_creds.exists():
            try:
                _peek = ConversionContext.load(ctx_path_for_creds)
                if _peek.unity_project_path:
                    project_anchor = Path(_peek.unity_project_path)
                    if not resolved_key:
                        resolved_key = _resolve_credential(
                            api_key, "ROBLOX_API_KEY", "apikey", project_anchor,
                        )
                    if not resolved_cid:
                        resolved_cid = _resolve_credential(
                            creator_id, "ROBLOX_CREATOR_ID", "creator_id", project_anchor,
                        )
            except Exception as exc:  # noqa: BLE001 — diagnostic only
                click.echo(f"  (could not peek context for cred autodiscovery: {exc})")

    if not resolved_key:
        click.echo("ERROR: API key required. Pass --api-key or create apikey file."); return
    config.ROBLOX_API_KEY = resolved_key
    if resolved_cid:
        config.ROBLOX_CREATOR_ID = int(resolved_cid)

    uid, pid = universe_id, place_id
    if not uid or not pid:
        cached_uid, cached_pid = read_ids(output_path)
        uid = uid or cached_uid
        pid = pid or cached_pid
    if not uid or not pid:
        click.echo("ERROR: --universe-id and --place-id required (or cached in .roblox_ids.json)"); return

    # Fast path: replay cached chunks. Works on archived/moved output dirs
    # whose Unity project is gone. Falls back to a Pipeline rebuild only if
    # the chunk cache is missing.
    click.echo(f"Publishing to universe={uid} place={pid}...")
    result = publish_cached_chunks(resolved_key, uid, pid, output_path)
    if result is None:
        click.echo("No cached chunks found — rebuilding from Unity project.")
        ctx_path = output_path / "conversion_context.json"
        if not ctx_path.exists():
            click.echo(f"ERROR: No conversion_context.json in {output_path}. Run 'convert' first."); return
        prior_ctx = ConversionContext.load(ctx_path)
        if not prior_ctx.unity_project_path:
            click.echo("ERROR: conversion_context.json has no unity_project_path."); return
        if not Path(prior_ctx.unity_project_path).is_dir():
            click.echo(
                f"ERROR: Unity project path missing: {prior_ctx.unity_project_path}\n"
                "Re-run 'u2r.py convert' to regenerate the chunk cache."
            ); return

        pipeline = Pipeline(
            unity_project_path=prior_ctx.unity_project_path,
            output_dir=output_path,
            skip_binary_rbxl=True,
        )
        pipeline.ctx = prior_ctx
        pipeline.ctx.universe_id = uid
        pipeline.ctx.place_id = pid

        # Run prereqs through extract_assets so we can check whether any
        # assets actually need uploading before deciding whether to demand
        # a creator_id. Outputs whose ctx.uploaded_assets already covers
        # the manifest don't need creator_id; this is the
        # rebuild-from-fully-uploaded-state workflow.
        pipeline.run_through("extract_assets")

        # Mirror upload_assets's eligibility filter: extensions per kind,
        # blocklist entries from .upload_blocklist, and the dedupe against
        # ctx.uploaded_assets. Without matching that filter, this precheck
        # would false-positive on output dirs whose pending assets are
        # blocklisted or whose extensions aren't supported uploads.
        UPLOADABLE_EXTENSIONS = {
            "texture": {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".tif", ".tiff", ".psd"},
            "mesh": {".fbx", ".obj"},
            "audio": {".mp3", ".ogg", ".wav", ".flac"},
        }
        blocklist: set[str] = set()
        blocklist_file = output_path / ".upload_blocklist"
        if blocklist_file.exists():
            blocklist = {
                line.strip() for line in blocklist_file.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }

        manifest = pipeline.state.asset_manifest
        pending_uploads: list = []
        unresolved_uploaded_meshes: list[str] = []
        if manifest is not None:
            already_uploaded = set(pipeline.ctx.uploaded_assets.keys())
            for asset in manifest.assets:
                exts = UPLOADABLE_EXTENSIONS.get(asset.kind)
                if exts is None or asset.path.suffix.lower() not in exts:
                    continue
                rel = str(asset.relative_path)
                if rel in already_uploaded or rel in blocklist:
                    continue
                pending_uploads.append(asset)
            # resolve_assets needs creator_id when mesh uploads have not all
            # been resolved. Comparing counts against the on-disk resolution
            # table catches the partial-resolution case where the previous
            # heuristic (just "is mesh_native_sizes empty?") would let a
            # publish ship placeholder MeshIds for the unresolved ones.
            uploaded_meshes = [
                p for p in pipeline.ctx.uploaded_assets
                if p.lower().endswith((".fbx", ".obj"))
                and p not in blocklist
            ]
            resolved = pipeline.ctx.mesh_native_sizes
            unresolved_uploaded_meshes = [
                p for p in uploaded_meshes if p not in resolved
            ]

        if (pending_uploads or unresolved_uploaded_meshes) and config.ROBLOX_CREATOR_ID is None:
            reasons = []
            if pending_uploads:
                reasons.append(f"{len(pending_uploads)} assets need upload")
            if unresolved_uploaded_meshes:
                reasons.append(
                    f"{len(unresolved_uploaded_meshes)} uploaded meshes not yet resolved"
                )
            click.echo(
                f"ERROR: {' and '.join(reasons)}, but --creator-id "
                "(or ROBLOX_CREATOR_ID) is not set. Pass --creator-id, "
                "set the env var, or place a 'creator_id' file under "
                "the output directory's parent."
            ); return

        # Skip already-completed prereqs on the second run_through. Cloud
        # phases get force_rerun so a fully-uploaded ctx still triggers
        # resolve_assets / re-emits the rbxlx with current asset state.
        skip: set[str] = {"parse", "extract_assets"}
        if (
            "transpile_scripts" in pipeline.ctx.completed_phases
            and scripts_cache_intact(output_path, pipeline.ctx.transpiled_scripts)
        ):
            skip.add("transpile_scripts")
        force_rerun = {"moderate_assets", "upload_assets", "resolve_assets"}
        pipeline.run_through("write_output", skip=skip, force_rerun=force_rerun)

        if pipeline.state.rbx_place is None:
            click.echo("ERROR: rbx_place is empty after rebuilding scene state."); return

        result = publish_place(
            resolved_key, uid, pid, pipeline.state.rbx_place, output_path,
        )

    click.echo(f"Script size: {result.total_bytes:,} chars "
               f"({result.total_bytes/1024:.0f} KB), {result.chunks} chunk(s)")
    if result.exceeded_limit:
        click.echo(f"WARNING: {result.error}")
        click.echo(f"Script saved to: {result.script_path}")
    elif result.success:
        write_ids(output_path, uid, pid)
        click.echo("Place published successfully!")
        click.echo("Open in Studio: File → Open from Roblox → select the experience")
    else:
        click.echo(f"Publication failed: {result.error}")


@main.command()
@click.argument("unity_project", type=click.Path(exists=True))
def analyze(unity_project: str) -> None:
    """Analyze a Unity project without converting."""
    from unity.scene_parser import parse_scene
    from unity.prefab_parser import parse_prefabs
    from unity.guid_resolver import build_guid_index
    from unity.asset_extractor import extract_assets
    from unity.script_analyzer import analyze_all_scripts

    project = Path(unity_project).resolve()
    # Auto-detect nested Unity project root
    if not (project / "Assets").is_dir():
        for child in project.iterdir():
            if child.is_dir() and (child / "Assets").is_dir():
                click.echo(f"Auto-detected Unity project root: {child.name}")
                project = child
                break
    click.echo(f"Analyzing: {project}")

    # GUID index
    guid_index = build_guid_index(project)
    click.echo(f"\nGUID Index: {guid_index.total_resolved} entries")
    if guid_index.duplicate_guids:
        click.echo(f"  Duplicate GUIDs: {len(guid_index.duplicate_guids)}")

    # Assets
    manifest = extract_assets(project, guid_index)
    click.echo(f"\nAssets: {len(manifest.assets)} files ({manifest.total_size_bytes / 1024 / 1024:.1f} MB)")
    for kind, assets in sorted(manifest.by_kind.items()):
        click.echo(f"  {kind}: {len(assets)}")

    # Scenes
    scenes = list((project / "Assets").rglob("*.unity")) if (project / "Assets").exists() else []
    click.echo(f"\nScenes: {len(scenes)}")
    for s in scenes:
        is_text = _is_text_yaml(s)
        status = "text YAML" if is_text else "BINARY (requires UnityPy)"
        click.echo(f"  {s.relative_to(project)}: {status}")
        if is_text:
            parsed = parse_scene(s)
            click.echo(f"    Roots: {len(parsed.roots)}, Total nodes: {len(parsed.all_nodes)}")
            click.echo(f"    Meshes: {len(parsed.referenced_mesh_guids)}, "
                       f"Materials: {len(parsed.referenced_material_guids)}")
            click.echo(f"    Prefab instances: {len(parsed.prefab_instances)}")
        else:
            try:
                from unity.binary_scene_parser import parse_binary_scene
                parsed = parse_binary_scene(s)
                click.echo(f"    Roots: {len(parsed.roots)}, Total nodes: {len(parsed.all_nodes)}")
            except Exception:
                click.echo(f"    (could not parse - install UnityPy)")

    # Prefabs
    library = parse_prefabs(project)
    click.echo(f"\nPrefabs: {len(library.prefabs)}")

    # Scripts
    scripts = analyze_all_scripts(project)
    click.echo(f"\nScripts: {len(scripts)}")
    type_counts: dict[str, int] = {}
    for s in scripts:
        type_counts[s.suggested_type] = type_counts.get(s.suggested_type, 0) + 1
    for t, c in sorted(type_counts.items()):
        click.echo(f"  {t}: {c}")


@main.command()
@click.argument("rbxlx_file", type=click.Path(exists=True))
def validate(rbxlx_file: str) -> None:
    """Validate a generated .rbxlx file for Roblox compatibility."""
    import xml.etree.ElementTree as ET

    rbxlx_path = Path(rbxlx_file)
    click.echo(f"Validating: {rbxlx_path}")

    try:
        tree = ET.parse(str(rbxlx_path))
    except ET.ParseError as e:
        click.echo(f"  ERROR: Invalid XML: {e}", err=True)
        return

    root = tree.getroot()
    if root.tag != "roblox":
        click.echo(f"  ERROR: Root element is '{root.tag}', expected 'roblox'", err=True)
        return

    # Count elements
    stats: dict[str, int] = {}
    for item in tree.iter("Item"):
        cls = item.get("class", "")
        stats[cls] = stats.get(cls, 0) + 1

    # Check for invalid classes
    valid_classes = {
        # Services
        "Workspace", "Terrain", "Lighting", "ServerScriptService",
        "ReplicatedStorage", "ReplicatedFirst", "ServerStorage",
        "StarterGui", "StarterPlayer", "StarterPlayerScripts", "StarterCharacterScripts",
        # Core instances
        "Part", "MeshPart", "Model", "SpawnLocation", "Camera",
        # Lights
        "PointLight", "SpotLight", "SurfaceLight",
        # Audio
        "Sound",
        # Scripts
        "Script", "LocalScript", "ModuleScript",
        # UI
        "ScreenGui", "Frame", "TextLabel", "TextButton", "ImageLabel", "ImageButton",
        "UIListLayout", "UIGridLayout",
        # Appearance
        "SurfaceAppearance", "Texture", "Decal", "Sky",
        # Events
        "RemoteEvent", "RemoteFunction", "BindableEvent",
        # Effects
        "ParticleEmitter", "Fire", "Smoke", "Sparkles", "Highlight",
        "Trail", "Beam", "Attachment",
        "BloomEffect", "ColorCorrectionEffect", "DepthOfFieldEffect",
        "SunRaysEffect", "Atmosphere",
        # Physics
        "WeldConstraint", "HingeConstraint", "SpringConstraint",
        "BallSocketConstraint",
        # Containers
        "Folder",
    }
    invalid = {cls for cls in stats if cls not in valid_classes}

    # Check for local file paths
    local_paths = 0
    for item in tree.iter("Content"):
        url = item.find("url")
        if url is not None and url.text and "/" in url.text and "rbxassetid" not in url.text:
            local_paths += 1

    # Check for string Materials (should be tokens)
    string_mats = sum(1 for e in tree.iter("string") if e.get("name") == "Material")

    # Check MeshParts for missing MeshId
    mesh_parts_total = 0
    mesh_parts_with_id = 0
    for item in tree.iter("Item"):
        if item.get("class") == "MeshPart":
            mesh_parts_total += 1
            props = item.find("Properties")
            if props is not None:
                for content in props.iter("Content"):
                    if content.get("name") == "MeshId":
                        url_el = content.find("url")
                        if url_el is not None and url_el.text and "rbxassetid" in url_el.text:
                            mesh_parts_with_id += 1
                            break

    mesh_parts_missing = mesh_parts_total - mesh_parts_with_id

    # Count SurfaceAppearances with actual textures
    sa_total = stats.get("SurfaceAppearance", 0)
    sa_with_textures = 0
    for item in tree.iter("Item"):
        if item.get("class") == "SurfaceAppearance":
            props = item.find("Properties")
            if props is not None:
                for content in props.iter("Content"):
                    url_el = content.find("url")
                    if url_el is not None and url_el.text and "rbxassetid" in url_el.text:
                        sa_with_textures += 1
                        break

    # Results
    total = sum(stats.values())
    issues = len(invalid) + local_paths + string_mats

    click.echo(f"\n  Total elements: {total}")
    click.echo(f"  Parts: {stats.get('Part', 0)}")
    click.echo(f"  MeshParts: {stats.get('MeshPart', 0)}")
    click.echo(f"  Models: {stats.get('Model', 0)}")
    click.echo(f"  Scripts: {stats.get('Script', 0) + stats.get('LocalScript', 0) + stats.get('ModuleScript', 0)}")
    click.echo(f"  SurfaceAppearances: {sa_total} ({sa_with_textures} with textures)")
    click.echo(f"  Sounds: {stats.get('Sound', 0)}")
    click.echo(f"  Lights: {stats.get('PointLight', 0) + stats.get('SpotLight', 0)}")
    click.echo(f"  Sky: {stats.get('Sky', 0)}")

    # Warnings (not errors, but helpful)
    warnings = []
    if mesh_parts_missing > 0:
        warnings.append(f"    {mesh_parts_missing}/{mesh_parts_total} MeshParts missing MeshId (need asset upload + resolution)")
    if sa_total == 0 and mesh_parts_total > 0:
        warnings.append(f"    No SurfaceAppearances (need asset upload for textures)")
    if stats.get("Sky", 0) == 0:
        warnings.append(f"    No Sky (need skybox texture upload)")

    if issues == 0 and not warnings:
        click.echo(f"\n  ✓ No issues found")
    elif issues == 0 and warnings:
        click.echo(f"\n  ✓ No errors found")
        click.echo(f"\n  Warnings ({len(warnings)}):")
        for w in warnings:
            click.echo(w)
    else:
        click.echo(f"\n  Issues found: {issues}")
        if invalid:
            click.echo(f"    Invalid classes: {invalid}")
        if local_paths:
            click.echo(f"    Local file paths: {local_paths}")
        if string_mats:
            click.echo(f"    String materials (should be tokens): {string_mats}")
        if warnings:
            click.echo(f"\n  Warnings ({len(warnings)}):")
            for w in warnings:
                click.echo(w)


@main.command()
@click.argument("output_dir", type=click.Path(exists=True))
def resolve(output_dir: str) -> None:
    """Generate Studio resolution scripts for uploaded assets.

    After uploading assets, run this command to generate Luau scripts
    that resolve Model IDs → real MeshIds and Decal IDs → Image IDs.
    Execute these in Roblox Studio's command bar.
    """
    import json
    from roblox.studio_resolver import (
        generate_mesh_resolution_luau,
        generate_texture_resolution_luau,
    )

    out = Path(output_dir)
    ctx_path = out / "conversion_context.json"
    if not ctx_path.exists():
        click.echo("Error: conversion_context.json not found. Run 'convert' first.", err=True)
        return

    ctx = json.loads(ctx_path.read_text())
    uploaded = ctx.get("uploaded_assets", {})
    if not uploaded:
        click.echo("No uploaded assets found. Run conversion with --api-key-file first.")
        return

    mesh_count = sum(1 for p in uploaded if p.lower().endswith(('.fbx', '.obj')))
    tex_count = sum(1 for p in uploaded if p.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.tga', '.psd')))

    click.echo(f"Uploaded assets: {len(uploaded)} ({mesh_count} meshes, {tex_count} textures)")

    # Generate mesh resolution scripts
    mesh_scripts = generate_mesh_resolution_luau(uploaded, batch_size=20)
    for i, script in enumerate(mesh_scripts):
        script_path = out / f"resolve_meshes_{i+1}.luau"
        script_path.write_text(script, encoding="utf-8")
    if mesh_scripts:
        click.echo(f"Generated {len(mesh_scripts)} mesh resolution script(s)")

    # Generate texture resolution scripts
    tex_scripts = generate_texture_resolution_luau(uploaded, batch_size=20)
    for i, script in enumerate(tex_scripts):
        script_path = out / f"resolve_textures_{i+1}.luau"
        script_path.write_text(script, encoding="utf-8")
    if tex_scripts:
        click.echo(f"Generated {len(tex_scripts)} texture resolution script(s)")

    if not mesh_scripts and not tex_scripts:
        click.echo("No resolution scripts needed.")
        return

    click.echo(f"\nNext steps:")
    click.echo(f"  1. Open the converted place in Roblox Studio")
    click.echo(f"  2. Open the Command Bar (View > Command Bar)")
    click.echo(f"  3. Run each resolve_*.luau script and save the output")
    click.echo(f"  4. Update conversion_context.json with the resolved data")
    click.echo(f"  5. Re-run: python u2r.py convert <project> -o {output_dir} --phase convert_scene")


@main.command()
@click.argument("unity_project", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), default="./output")
@click.option("--scene", type=str, default=None, help="Scene path relative to project")
@click.option("--visual", is_flag=True, help="Run visual comparison (capture screenshots + SSIM)")
@click.option("--unity-screenshot", type=click.Path(), default=None,
              help="Path to an existing Unity screenshot (skip capture)")
@click.option("--roblox-screenshot", type=click.Path(), default=None,
              help="Path to an existing Roblox screenshot (skip capture)")
@click.option("--crop/--no-crop", default=True,
              help="Crop viewport margins before SSIM (default: crop)")
@click.option("--crop-margin", type=float, default=0.1,
              help="Fraction of image to remove from each edge (0.0-0.5)")
def compare(
    unity_project: str,
    output: str,
    scene: str | None,
    visual: bool,
    unity_screenshot: str | None,
    roblox_screenshot: str | None,
    crop: bool,
    crop_margin: float,
) -> None:
    """Run comparison between Unity and Roblox versions.

    Without --visual, generates a state comparison report (object counts,
    component types, etc.) between the Unity project and its converted
    Roblox place.

    With --visual, also captures or uses provided screenshots and computes
    SSIM (structural similarity) scores with a diff heatmap.

    Examples:

      python u2r.py compare path/to/UnityProject -o ./output

      python u2r.py compare path/to/UnityProject -o ./output --visual

      python u2r.py compare path/to/UnityProject -o ./output --visual \\
          --unity-screenshot unity.png --roblox-screenshot roblox.png

      python u2r.py compare path/to/UnityProject -o ./output --visual --no-crop
    """
    from pathlib import Path
    import json

    project_path = Path(unity_project).resolve()
    output_path = Path(output).resolve()
    comparison_dir = output_path / "comparison"
    comparison_dir.mkdir(parents=True, exist_ok=True)

    # Find the scene to compare
    if scene is None:
        scenes = list(project_path.glob("**/*.unity"))
        yaml_scenes = [s for s in scenes if _is_text_yaml(s)]
        if yaml_scenes:
            scene_file = yaml_scenes[0]
            scene = str(scene_file.relative_to(project_path))
        else:
            click.echo("No text YAML scenes found in project")
            return
    else:
        scene_file = project_path / scene

    click.echo(f"Comparing: {scene}")

    # ------------------------------------------------------------------
    # State comparison (always runs)
    # ------------------------------------------------------------------

    cam_info: dict | None = None

    # Parse Unity scene
    try:
        from unity.scene_parser import parse_scene
        parsed = parse_scene(scene_file)

        unity_stats = {
            "roots": len(parsed.roots),
            "total_nodes": len(parsed.all_nodes),
            "prefab_instances": len(parsed.prefab_instances),
            "materials": len(parsed.referenced_material_guids),
            "meshes": len(parsed.referenced_mesh_guids),
        }

        # Count components
        comp_counts: dict[str, int] = {}
        def _count_comps(node):
            for comp in node.components:
                ct = comp.component_type
                comp_counts[ct] = comp_counts.get(ct, 0) + 1
            for child in node.children:
                _count_comps(child)
        for root in parsed.roots:
            _count_comps(root)
        unity_stats["components"] = comp_counts

        # Camera info
        from comparison.screenshot_capture import get_scene_camera_info
        cam_info = get_scene_camera_info(project_path, scene)
        if cam_info:
            unity_stats["camera"] = cam_info
            click.echo(f"  Camera: {cam_info['name']} at {cam_info['position']}")

        click.echo(f"  Nodes: {unity_stats['total_nodes']}")
        click.echo(f"  Prefab instances: {unity_stats['prefab_instances']}")
        click.echo(f"  Materials: {unity_stats['materials']}")
        click.echo(f"  Meshes: {unity_stats['meshes']}")
        click.echo(f"  Components: {dict(sorted(comp_counts.items()))}")

    except Exception as exc:
        click.echo(f"  Failed to parse scene: {exc}")
        unity_stats = {"error": str(exc)}

    # Check for converted rbxlx
    rbxlx = output_path / "converted_place.rbxlx"
    roblox_stats: dict = {}
    if rbxlx.exists():
        click.echo(f"\nConverted place: {rbxlx}")
        try:
            import xml.etree.ElementTree as ET
            tree = ET.parse(rbxlx)
            root = tree.getroot()

            class_counts: dict[str, int] = {}
            for item in root.iter("Item"):
                cls = item.get("class", "Unknown")
                class_counts[cls] = class_counts.get(cls, 0) + 1

            roblox_stats["classes"] = class_counts
            total_parts = class_counts.get("Part", 0) + class_counts.get("MeshPart", 0)
            click.echo(f"  Total parts: {total_parts}")
            click.echo(f"  Models: {class_counts.get('Model', 0)}")
            click.echo(f"  Scripts: {class_counts.get('Script', 0)}")
            click.echo(f"  LocalScripts: {class_counts.get('LocalScript', 0)}")
            click.echo(f"  Lights: {class_counts.get('PointLight', 0) + class_counts.get('SpotLight', 0)}")
            click.echo(f"  Sounds: {class_counts.get('Sound', 0)}")

        except Exception as exc:
            click.echo(f"  Failed to parse rbxlx: {exc}")
            roblox_stats = {"error": str(exc)}
    else:
        click.echo(f"\nNo converted place found at {rbxlx}")
        click.echo("Run: python u2r.py convert <project> -o <output> first")

    # ------------------------------------------------------------------
    # Visual comparison (--visual flag)
    # ------------------------------------------------------------------

    visual_results: dict = {}

    if visual:
        click.echo("\n--- Visual Comparison ---")

        from comparison.screenshot_capture import (
            run_unity_screenshot,
            capture_roblox_screenshot,
            unity_camera_to_roblox,
        )
        from comparison.visual_diff import compare_images

        # --- Unity screenshot ---
        unity_img_path: Path | None = None
        if unity_screenshot:
            unity_img_path = Path(unity_screenshot).resolve()
            if not unity_img_path.exists():
                click.echo(f"  Unity screenshot not found: {unity_img_path}")
                unity_img_path = None
            else:
                click.echo(f"  Using Unity screenshot: {unity_img_path}")
        else:
            click.echo("  Capturing Unity screenshot (batch mode)...")
            cam_pos = None
            cam_rot = None
            if cam_info:
                cam_pos = cam_info.get("position")
                cam_rot = cam_info.get("rotation_euler")

            unity_img_path = run_unity_screenshot(
                unity_project_path=project_path,
                scene_path=scene,
                output_path=comparison_dir / "unity_screenshot.png",
                camera_position=cam_pos,
                camera_rotation=cam_rot,
            )
            if unity_img_path:
                click.echo(f"  Unity screenshot saved: {unity_img_path}")
            else:
                click.echo("  Unity screenshot capture failed (Unity Editor not found or batch mode error)")
                click.echo("  Tip: Provide a screenshot manually with --unity-screenshot <path>")

        # --- Roblox screenshot ---
        roblox_img_path: Path | None = None
        if roblox_screenshot:
            roblox_img_path = Path(roblox_screenshot).resolve()
            if not roblox_img_path.exists():
                click.echo(f"  Roblox screenshot not found: {roblox_img_path}")
                roblox_img_path = None
            else:
                click.echo(f"  Using Roblox screenshot: {roblox_img_path}")
        else:
            # Generate the camera positioning script for matched viewpoints
            roblox_cam = None
            if cam_info:
                roblox_cam = unity_camera_to_roblox(
                    position=cam_info["position"],
                    rotation_euler=cam_info["rotation_euler"],
                    fov=cam_info.get("fov", 70.0),
                )
                click.echo(f"  Matched camera -> Roblox pos={roblox_cam['position']}, "
                            f"rot={roblox_cam['rotation']}, fov={roblox_cam['fov']}")

            roblox_expected = capture_roblox_screenshot(
                output_path=comparison_dir / "roblox_screenshot.png",
                camera_position=roblox_cam["position"] if roblox_cam else None,
                camera_rotation=roblox_cam["rotation"] if roblox_cam else None,
                fov=roblox_cam["fov"] if roblox_cam else 70.0,
            )

            # Check if a Roblox screenshot was previously saved
            if roblox_expected and roblox_expected.exists():
                roblox_img_path = roblox_expected
                click.echo(f"  Using existing Roblox screenshot: {roblox_img_path}")
            else:
                click.echo(f"  Roblox screenshot not yet captured.")
                if roblox_cam:
                    script_path = comparison_dir / "position_camera.luau"
                    click.echo(f"  Camera script: {script_path}")
                click.echo(f"  To capture: use mcp__Roblox_Studio__screen_capture")
                click.echo(f"  Then save to: {roblox_expected}")
                click.echo(f"  Or re-run with: --roblox-screenshot <path>")

        # --- Compute visual diff ---
        if unity_img_path and roblox_img_path:
            click.echo("\n  Computing visual similarity...")
            try:
                visual_results = compare_images(
                    image_a_path=unity_img_path,
                    image_b_path=roblox_img_path,
                    output_dir=comparison_dir,
                    crop=crop,
                    crop_margin=crop_margin,
                )

                click.echo(f"\n  SSIM (grayscale): {visual_results['ssim']:.4f}")
                click.echo(f"  SSIM (RGB):       {visual_results['ssim_rgb']:.4f}")
                click.echo(f"  Pixel diff:       {visual_results['pixel_diff_pct']:.1f}%")
                click.echo(f"  Quality:          {visual_results['quality_label']}")
                click.echo(f"  Heatmap:          {visual_results['heatmap_path']}")

            except Exception as exc:
                click.echo(f"  Visual comparison failed: {exc}")
                visual_results = {"error": str(exc)}
        elif visual:
            click.echo("\n  Skipping SSIM: need both Unity and Roblox screenshots.")
            click.echo("  Provide with --unity-screenshot and --roblox-screenshot")

    # ------------------------------------------------------------------
    # Save comparison report
    # ------------------------------------------------------------------

    report = {
        "scene": scene,
        "unity": unity_stats,
        "roblox": roblox_stats,
    }
    if visual_results:
        report["visual"] = visual_results

    report_path = comparison_dir / "comparison_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    click.echo(f"\nReport saved: {report_path}")


@main.command("audit-assets")
@click.argument("output_dir", type=click.Path(exists=True))
@click.option("--api-key", type=str, default=None,
              help="Roblox Open Cloud API key (string or path to file).")
@click.option("--fail-on-reject", is_flag=True,
              help="Exit non-zero if any asset is rejected.")
def audit_assets(output_dir: str, api_key: str | None, fail_on_reject: bool) -> None:
    """Probe every uploaded asset in conversion_context.json for moderation.

    Hits the Roblox assets metadata endpoint for each `rbxassetid://NUM`
    entry in the context and classifies the result as approved / rejected
    / unknown. Use this to catch post-upload moderation rejections (e.g.
    music that uploads cleanly but is blocked at runtime with HTTP 403)
    before they leak into a published place.
    """
    from pathlib import Path
    import json
    import sys

    import config
    from roblox.cloud_api import probe_asset_availability
    from core.conversion_context import ConversionContext

    out = Path(output_dir).resolve()
    ctx_path = out / "conversion_context.json"
    if not ctx_path.exists():
        click.echo(f"No conversion_context.json at {out}", err=True)
        sys.exit(2)

    if api_key:
        ak = Path(api_key)
        config.ROBLOX_API_KEY = (
            ak.read_text().strip() if ak.is_file() else api_key.strip()
        )
    if not config.ROBLOX_API_KEY:
        click.echo("No Roblox API key. Pass --api-key or set ROBLOX_API_KEY.", err=True)
        sys.exit(2)

    ctx = ConversionContext.load(ctx_path)
    rejected: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    approved = 0

    import time as _time
    total = len(ctx.uploaded_assets)
    click.echo(f"Probing {total} uploaded assets...")
    for i, (key, url) in enumerate(ctx.uploaded_assets.items(), 1):
        numeric = "".join(ch for ch in str(url) if ch.isdigit())
        if not numeric:
            rejected.append((key, f"{url} (non-numeric ID)"))
            continue
        status = probe_asset_availability(numeric, config.ROBLOX_API_KEY)
        if status == "rejected":
            rejected.append((key, url))
        elif status == "unknown":
            unknown.append((key, url))
        else:
            approved += 1
        if i % 25 == 0:
            click.echo(f"  {i}/{total}...")
        # Throttle: Roblox Open Cloud assets endpoint rate-limits aggressively
        # (~60 req/min for metadata reads). Sleep between calls so the sweep
        # doesn't cascade into 429s that would get misclassified as "unknown".
        _time.sleep(1.1)

    click.echo("")
    click.echo(f"Approved: {approved}")
    click.echo(f"Rejected: {len(rejected)}")
    click.echo(f"Unknown:  {len(unknown)}")

    if rejected:
        click.echo("\nRejected assets:")
        for key, url in rejected:
            click.echo(f"  {key} -> {url}")

    report_path = out / "asset_audit.json"
    report_path.write_text(json.dumps({
        "approved_count": approved,
        "rejected": [{"path": k, "url": u} for k, u in rejected],
        "unknown": [{"path": k, "url": u} for k, u in unknown],
    }, indent=2))
    click.echo(f"\nReport: {report_path}")

    if fail_on_reject and rejected:
        sys.exit(1)


@main.command("eval")
@click.option("--output", "-o", type=click.Path(), default="./eval_output",
              help="Directory for per-project conversion outputs.")
@click.option("--baseline", type=click.Path(), default=None,
              help="Path to write eval_baseline.json (default: eval_output/eval_baseline.json).")
def eval_cmd(output: str, baseline: str | None) -> None:
    """Convert all test projects and capture quality metrics.

    Converts every populated project under ../test_projects/ with --no-upload,
    measures structural / script / material quality metrics, and writes an
    eval JSON file. Use `eval-diff` to compare against a previous baseline.
    """
    from pathlib import Path
    import json
    import time as _time
    import xml.etree.ElementTree as ET

    from converter.pipeline import Pipeline
    import config

    config.USE_AI_TRANSPILATION = False  # deterministic rule-based for eval

    test_projects_dir = Path(__file__).parent.parent / "test_projects"
    out_root = Path(output).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    projects: list[tuple[str, Path]] = []
    if test_projects_dir.is_dir():
        for child in sorted(test_projects_dir.iterdir()):
            if not child.is_dir():
                continue
            if (child / "Assets").is_dir():
                projects.append((child.name, child))
            else:
                for sub in child.iterdir():
                    if sub.is_dir() and (sub / "Assets").is_dir():
                        projects.append((child.name, sub))
                        break

    if not projects:
        click.echo("No populated test projects found under ../test_projects/")
        return

    click.echo(f"Evaluating {len(projects)} projects...")
    results: dict[str, dict] = {}

    for name, project_path in projects:
        click.echo(f"\n--- {name} ---")
        proj_out = out_root / name
        proj_out.mkdir(parents=True, exist_ok=True)

        t0 = _time.monotonic()
        try:
            pipeline = Pipeline(
                unity_project_path=project_path,
                output_dir=proj_out,
                skip_upload=True,
            )
            pipeline.run_all()
            elapsed = _time.monotonic() - t0

            ctx = pipeline.ctx
            metrics: dict[str, Any] = {
                "status": "ok",
                "conversion_time_s": round(elapsed, 1),
                "total_game_objects": ctx.total_game_objects,
                "converted_parts": ctx.converted_parts,
                "total_scripts": ctx.total_scripts,
                "transpiled_scripts": ctx.transpiled_scripts,
                "script_ratio": round(ctx.transpiled_scripts / max(ctx.total_scripts, 1), 3),
                "total_materials": ctx.total_materials,
                "converted_materials": ctx.converted_materials,
                "total_animations": ctx.total_animations,
                "converted_animations": ctx.converted_animations,
                "warnings": len(ctx.warnings),
                "errors": len(ctx.errors),
            }

            # Parse rbxlx for structural metrics
            rbxlx_path = proj_out / "converted_place.rbxlx"
            if rbxlx_path.exists():
                metrics["rbxlx_size_kb"] = round(rbxlx_path.stat().st_size / 1024, 1)
                try:
                    tree = ET.parse(str(rbxlx_path))
                    classes: dict[str, int] = {}
                    for item in tree.iter("Item"):
                        cls = item.get("class", "")
                        classes[cls] = classes.get(cls, 0) + 1

                    metrics["parts"] = classes.get("Part", 0)
                    metrics["mesh_parts"] = classes.get("MeshPart", 0)
                    metrics["models"] = classes.get("Model", 0)
                    metrics["scripts_in_rbxlx"] = (
                        classes.get("Script", 0)
                        + classes.get("LocalScript", 0)
                        + classes.get("ModuleScript", 0)
                    )
                    metrics["sounds"] = classes.get("Sound", 0)
                    metrics["lights"] = (
                        classes.get("PointLight", 0)
                        + classes.get("SpotLight", 0)
                        + classes.get("SurfaceLight", 0)
                    )

                    # SurfaceAppearance texture coverage
                    sa_total = sa_textured = 0
                    for item in tree.iter("Item"):
                        if item.get("class") == "SurfaceAppearance":
                            sa_total += 1
                            props = item.find("Properties")
                            if props is not None:
                                cm = props.find('Content[@name="ColorMap"]')
                                url = cm.find("url") if cm is not None else None
                                if url is not None and url.text and url.text.strip():
                                    sa_textured += 1
                    metrics["surface_appearances"] = sa_total
                    metrics["sa_with_textures"] = sa_textured

                    # Sounds with valid SoundId
                    snd_ok = snd_empty = 0
                    for item in tree.iter("Item"):
                        if item.get("class") == "Sound":
                            props = item.find("Properties")
                            if props is not None:
                                content = props.find('Content[@name="SoundId"]')
                                url_elem = content.find("url") if content is not None else None
                                if url_elem is not None and url_elem.text and url_elem.text.strip():
                                    snd_ok += 1
                                else:
                                    snd_empty += 1
                    metrics["sounds_with_id"] = snd_ok
                    metrics["sounds_empty"] = snd_empty
                except ET.ParseError as exc:
                    metrics["rbxlx_parse_error"] = str(exc)

            # Count validator fixes from conversion report
            report_path = proj_out / "conversion_report.json"
            if report_path.exists():
                try:
                    report = json.loads(report_path.read_text())
                    metrics["validator_fixes"] = report.get("validator_fixes", 0)
                except (json.JSONDecodeError, KeyError):
                    pass

            # Count TODO placeholders and C# residue in transpiled scripts
            scripts_dir = proj_out / "scripts"
            if scripts_dir.exists():
                import re as _re
                todo_count = 0
                csharp_residue = 0
                for script_file in scripts_dir.rglob("*.luau"):
                    text = script_file.read_text(encoding="utf-8", errors="replace")
                    todo_count += len(_re.findall(
                        r'--\s*TODO[:\s]', text, _re.IGNORECASE,
                    ))
                    csharp_residue += len(_re.findall(
                        r'\b(?:GetComponent|AddComponent|FindObjectOfType|'
                        r'SendMessage|BroadcastMessage)\s*[<(]',
                        text,
                    ))
                metrics["todo_placeholders"] = todo_count
                metrics["csharp_residue"] = csharp_residue

        except Exception as exc:
            elapsed = _time.monotonic() - t0
            metrics = {
                "status": "error",
                "conversion_time_s": round(elapsed, 1),
                "error": str(exc),
            }

        results[name] = metrics
        click.echo(f"  status={metrics['status']}  time={metrics.get('conversion_time_s', '?')}s")
        if metrics["status"] == "ok":
            click.echo(
                f"  parts={metrics.get('parts', 0)}+{metrics.get('mesh_parts', 0)} "
                f"scripts={metrics.get('transpiled_scripts', 0)}/{metrics.get('total_scripts', 0)} "
                f"SA={metrics.get('sa_with_textures', 0)}/{metrics.get('surface_appearances', 0)} "
                f"sounds={metrics.get('sounds_with_id', 0)}/{metrics.get('sounds', 0)}"
            )

    # Write results
    baseline_path = Path(baseline) if baseline else out_root / "eval_baseline.json"
    eval_data = {
        "generated_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "commit": _get_git_commit(),
        "projects": results,
    }
    baseline_path.write_text(json.dumps(eval_data, indent=2), encoding="utf-8")
    click.echo(f"\nEval written to: {baseline_path}")

    # Summary
    ok = sum(1 for m in results.values() if m["status"] == "ok")
    err = sum(1 for m in results.values() if m["status"] == "error")
    click.echo(f"\n{ok} succeeded, {err} failed out of {len(results)} projects")


@main.command("eval-diff")
@click.argument("baseline", type=click.Path(exists=True))
@click.argument("current", type=click.Path(exists=True))
@click.option("--fail-on-regression", is_flag=True,
              help="Exit non-zero if any metric regressed.")
def eval_diff(baseline: str, current: str, fail_on_regression: bool) -> None:
    """Compare two eval JSON files and report regressions/improvements.

    Metrics where HIGHER is better: converted_parts, transpiled_scripts,
    script_ratio, sa_with_textures, sounds_with_id.

    Metrics where LOWER is better: errors, warnings, sounds_empty,
    conversion_time_s.
    """
    import json

    with open(baseline) as f:
        base = json.load(f)
    with open(current) as f:
        curr = json.load(f)

    higher_is_better = {
        "converted_parts", "transpiled_scripts", "script_ratio",
        "sa_with_textures", "sounds_with_id", "parts", "mesh_parts",
        "scripts_in_rbxlx", "surface_appearances", "converted_materials",
    }
    lower_is_better = {
        "errors", "warnings", "sounds_empty", "conversion_time_s",
        "todo_placeholders", "csharp_residue",
    }

    regressions: list[str] = []
    improvements: list[str] = []

    base_projects = base.get("projects", {})
    curr_projects = curr.get("projects", {})

    click.echo(f"Baseline: {base.get('commit', '?')} ({base.get('generated_at', '?')})")
    click.echo(f"Current:  {curr.get('commit', '?')} ({curr.get('generated_at', '?')})")
    click.echo("")

    all_projects = sorted(set(base_projects) | set(curr_projects))
    for proj in all_projects:
        bp = base_projects.get(proj, {})
        cp = curr_projects.get(proj, {})

        if not bp:
            click.echo(f"  {proj}: NEW (not in baseline)")
            continue
        if not cp:
            click.echo(f"  {proj}: MISSING (was in baseline)")
            regressions.append(f"{proj}: missing from current eval")
            continue

        if bp.get("status") == "error" and cp.get("status") == "ok":
            improvements.append(f"{proj}: was error, now ok")
        elif bp.get("status") == "ok" and cp.get("status") == "error":
            regressions.append(f"{proj}: was ok, now error: {cp.get('error', '?')}")
            continue

        diffs: list[str] = []
        for key in sorted(set(bp) | set(cp)):
            if key in ("status", "error", "rbxlx_parse_error", "validator_fixes"):
                continue
            bv = bp.get(key)
            cv = cp.get(key)
            if bv is None or cv is None:
                continue
            if not isinstance(bv, (int, float)) or not isinstance(cv, (int, float)):
                continue
            if bv == cv:
                continue

            delta = cv - bv
            pct = f"{delta/abs(bv)*100:+.1f}%" if bv != 0 else "new"

            if key in higher_is_better:
                tag = "improved" if delta > 0 else "REGRESSED"
            elif key in lower_is_better:
                tag = "improved" if delta < 0 else "REGRESSED"
            else:
                tag = "changed"

            line = f"    {key}: {bv} → {cv} ({pct}) [{tag}]"
            diffs.append(line)
            if tag == "REGRESSED":
                regressions.append(f"{proj}.{key}: {bv} → {cv}")
            elif tag == "improved":
                improvements.append(f"{proj}.{key}: {bv} → {cv}")

        if diffs:
            click.echo(f"  {proj}:")
            for d in diffs:
                click.echo(d)
        else:
            click.echo(f"  {proj}: no changes")

    click.echo(f"\nImprovements: {len(improvements)}")
    click.echo(f"Regressions:  {len(regressions)}")

    if regressions:
        click.echo("\nREGRESSIONS:")
        for r in regressions:
            click.echo(f"  - {r}")

    if fail_on_regression and regressions:
        sys.exit(1)


@main.command("visual-compare")
@click.argument("unity_project", type=click.Path(exists=True))
@click.option("--output", "-o", type=click.Path(), default="./output",
              help="Output directory (where converted_place.rbxlx lives).")
@click.option("--scene", type=str, default=None,
              help="Scene path relative to project (auto-detected if omitted).")
@click.option("--save-dir", type=click.Path(), default=None,
              help="Directory for screenshots and diff output (default: output/comparison).")
def visual_compare(unity_project: str, output: str, scene: str | None,
                   save_dir: str | None) -> None:
    """Side-by-side visual comparison: read Unity scene camera, match in Roblox Studio.

    Reads the game camera position from the Unity scene file, converts it
    to Roblox coordinates, positions Roblox Studio's camera at the
    equivalent viewpoint, and captures screenshots from both. Then runs
    SSIM (structural similarity) between the two images.

    Requires:
    - Unity Editor open with the project loaded (for computer-use screenshot)
    - Roblox Studio open with converted_place.rbxlx (for MCP screen capture)

    The command outputs SSIM score and saves:
    - unity_screenshot.png — cropped Unity viewport
    - roblox_screenshot.png — Studio viewport capture
    - diff_heatmap.png — pixel-level difference visualization
    """
    from pathlib import Path
    import json

    project_path = Path(unity_project).resolve()
    output_path = Path(output).resolve()
    comparison_dir = Path(save_dir) if save_dir else output_path / "comparison"
    comparison_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 1: Find scene and read Unity camera ---
    if scene is None:
        scenes = list(project_path.glob("**/*.unity"))
        yaml_scenes = [s for s in scenes if _is_text_yaml(s)]
        if yaml_scenes:
            scene = str(yaml_scenes[0].relative_to(project_path))
        else:
            click.echo("No text YAML scenes found.")
            return

    click.echo(f"Scene: {scene}")

    from comparison.screenshot_capture import (
        get_scene_camera_info,
        unity_camera_to_roblox,
        generate_roblox_camera_luau,
    )

    cam_info = get_scene_camera_info(project_path, scene)
    if not cam_info:
        click.echo("Could not find camera in scene. Specify --scene.")
        return

    click.echo(f"Unity camera: {cam_info['name']} at {cam_info['position']}")

    roblox_cam = unity_camera_to_roblox(
        cam_info["position"],
        cam_info.get("rotation_euler", (0, 0, 0)),
        cam_info.get("fov", 60),
    )
    click.echo(f"Roblox camera: pos={roblox_cam['position']} fov={roblox_cam['fov']}")

    # --- Step 2: Position Roblox Studio camera via MCP ---
    luau_script = generate_roblox_camera_luau(
        roblox_cam["position"],
        roblox_cam["rotation"],
        roblox_cam["fov"],
    )
    click.echo("\nRoblox camera Luau script (paste into Studio command bar or run via MCP):")
    click.echo(f"  {luau_script.strip()}")

    # --- Step 3: Save camera info for external tools ---
    camera_data = {
        "unity": {
            "name": cam_info["name"],
            "position": cam_info["position"],
            "rotation_euler": cam_info.get("rotation_euler"),
            "fov": cam_info.get("fov", 60),
        },
        "roblox": roblox_cam,
        "luau_script": luau_script,
    }
    camera_path = comparison_dir / "camera_match.json"
    camera_path.write_text(json.dumps(camera_data, indent=2), encoding="utf-8")
    click.echo(f"\nCamera data saved: {camera_path}")

    # --- Step 4: Instructions for manual capture ---
    click.echo("\n--- Capture Instructions ---")
    click.echo("1. In Unity: navigate Scene view to the camera position, or enter Play mode")
    click.echo("2. In Roblox Studio: run the Luau script above in the command bar")
    click.echo("3. Take screenshots of both viewports")
    click.echo(f"4. Run: python3 u2r.py compare {unity_project} -o {output} --visual \\")
    click.echo(f"     --unity-screenshot <unity.png> --roblox-screenshot <roblox.png>")

    # --- Step 5: If screenshots already exist, run SSIM ---
    unity_img = comparison_dir / "unity_screenshot.png"
    roblox_img = comparison_dir / "roblox_screenshot.png"
    if unity_img.exists() and roblox_img.exists():
        click.echo("\nFound existing screenshots — running SSIM comparison...")
        from comparison.visual_diff import compare_images
        results = compare_images(
            str(unity_img), str(roblox_img),
            output_dir=str(comparison_dir),
        )
        if results:
            click.echo(f"  SSIM: {results.get('ssim', 'N/A')}")
            click.echo(f"  Diff saved: {comparison_dir / 'diff_heatmap.png'}")


@main.command("smoke-test")
@click.argument("rbxlx_file", type=click.Path(exists=True))
@click.option("--timeout", type=int, default=180,
              help="Max seconds to wait for health-check completion (default: 180)")
@click.option("--output-dir", "-o", type=click.Path(), default=None,
              help="Directory for smoke test artifacts (default: alongside rbxlx)")
@click.option("--no-screenshot", is_flag=True, help="Skip screenshot capture")
@click.option("--keep-injected", is_flag=True, help="Keep the injected rbxlx file after test")
def smoke_test(
    rbxlx_file: str,
    timeout: int,
    output_dir: str | None,
    no_screenshot: bool,
    keep_injected: bool,
) -> None:
    """Run a smoke test: open place in Studio, enter Play, capture results.

    Opens the given .rbxlx in Roblox Studio, injects a health-check script,
    enters Play mode, waits for script output in Studio logs, takes a
    screenshot, and produces a pass/fail report.

    Requires macOS with Roblox Studio installed and logged in.

    Examples:

      python u2r.py smoke-test output/SimpleFPS/converted_place.rbxlx

      python u2r.py smoke-test converted_place.rbxlx --timeout 300 -o ./smoke_results
    """
    from smoke_test import run_smoke_test, format_report

    out = Path(output_dir) if output_dir else None
    report = run_smoke_test(
        rbxlx_path=rbxlx_file,
        timeout=timeout,
        screenshot=not no_screenshot,
        output_dir=out,
        keep_injected=keep_injected,
    )

    click.echo(format_report(report))

    if report.status != "pass":
        sys.exit(1)


def _get_git_commit() -> str:
    """Return the current short git commit hash, or 'unknown'."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    main()
