"""
convert_interactive.py -- Phase-by-phase interactive CLI for the
``/convert-unity`` Claude Code skill.

This is the *skill-facing* counterpart to ``u2r.py``.  Where ``u2r.py convert``
runs the entire pipeline end-to-end, this module exposes one Click subcommand
per skill phase so the skill can pause between phases for human input
(scene selection, material review, script review, upload configuration, etc.).

Each subcommand:

* Loads ``conversion_context.json`` from ``<output_dir>`` if it exists.
* Re-runs essential prerequisite pipeline phases to rebuild in-memory state
  (mirrors ``Pipeline.resume`` semantics, but stops at the requested phase).
* Runs the target phase via ``Pipeline._run_phase``.
* Saves ``conversion_context.json`` and writes a small ``.convert_state.json``
  marker so the skill can detect resumability.
* Emits a JSON summary on stdout for the skill to consume.

The skill-facing phase names group several internal pipeline phases:

==============  ===================================================
Skill phase     Internal pipeline phases (run in order)
==============  ===================================================
discover        parse
inventory       extract_assets
materials       convert_materials
transpile       transpile_scripts
validate        (no pipeline call — runs luau_validator on disk)
assemble        upload_assets, resolve_assets, convert_animations,
                convert_scene, write_output
upload          headless place publish via Open Cloud execute_luau
report          (no pipeline call — generates JSON report)
==============  ===================================================

The split mirrors how a human would walk through a Unity → Roblox conversion:
discover what's there, take inventory, decide on materials/scripts, then
assemble and ship.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import click

import config
from converter.pipeline import PHASES, Pipeline
from core.conversion_context import ConversionContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skill phase ↔ pipeline phase mapping
# ---------------------------------------------------------------------------

SKILL_PHASES: list[str] = [
    "discover",
    "inventory",
    "materials",
    "transpile",
    "validate",
    "assemble",
    "upload",
    "report",
]

# Map each skill phase to the *last* pipeline phase it runs.  Prerequisites
# are inferred from the global pipeline ordering in ``PHASES``.  Skill phases
# that don't correspond to a pipeline phase (validate, upload, report) are
# absent from this map.
SKILL_TO_PIPELINE_PHASE: dict[str, str] = {
    "discover": "parse",
    "inventory": "extract_assets",
    "materials": "convert_materials",
    "transpile": "transpile_scripts",
    # "assemble" is handled specially — it spans multiple pipeline phases.
}

STATE_FILENAME = ".convert_state.json"

# Roblox Open Cloud execute_luau accepts scripts up to ~4MB. Place-builder
# scripts larger than this must fall back to the runtime MeshLoader path.
MAX_EXECUTE_LUAU_BYTES = 4_000_000


# ---------------------------------------------------------------------------
# Helpers — state, context, JSON emission
# ---------------------------------------------------------------------------


def _state_path(output_dir: Path) -> Path:
    return output_dir / STATE_FILENAME


def _context_path(output_dir: Path) -> Path:
    return output_dir / "conversion_context.json"


def _load_skill_state(output_dir: Path) -> dict:
    sp = _state_path(output_dir)
    if sp.exists():
        try:
            return json.loads(sp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_skill_state(output_dir: Path, state: dict) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _state_path(output_dir).write_text(
        json.dumps(state, indent=2, default=str),
        encoding="utf-8",
    )


def _emit(data: dict) -> None:
    """Print a JSON document to stdout for the skill to consume."""
    click.echo(json.dumps(data, indent=2, default=str))


def _mark_skill_phase(output_dir: Path, phase: str, **extras: Any) -> None:
    """Record that a skill phase has run."""
    state = _load_skill_state(output_dir)
    state.setdefault("completed_skill_phases", [])
    if phase not in state["completed_skill_phases"]:
        state["completed_skill_phases"].append(phase)
    for k, v in extras.items():
        state[k] = v
    _save_skill_state(output_dir, state)


def _make_pipeline(
    unity_project_path: str | Path | None,
    output_dir: str | Path,
    *,
    skip_upload: bool = False,
) -> Pipeline:
    """Build a Pipeline and rehydrate ctx from disk if a previous run exists.

    Accepts ``unity_project_path=None`` for phases that only read state
    (e.g. ``status``, ``upload``, ``report``).  In that case the unity project
    path is recovered from the persisted ``ConversionContext``.
    """
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    ctx_path = _context_path(out)
    if unity_project_path is None:
        if not ctx_path.exists():
            raise click.UsageError(
                f"No conversion_context.json in {out}. "
                "Pass the unity project path or run 'discover' first."
            )
        prior = ConversionContext.load(ctx_path)
        unity_project_path = prior.unity_project_path
        if not unity_project_path:
            raise click.UsageError(
                f"conversion_context.json at {out} has no unity_project_path."
            )

    pipeline = Pipeline(
        unity_project_path=unity_project_path,
        output_dir=out,
        skip_upload=skip_upload,
    )
    if ctx_path.exists():
        pipeline.ctx = ConversionContext.load(ctx_path)
    return pipeline


def _run_through(pipeline: Pipeline, target_phase: str) -> None:
    """Run prerequisite phases then ``target_phase`` — but nothing after.

    This mirrors :meth:`Pipeline.resume` except it stops at *target_phase*
    instead of running everything to the end.  ``essential_phases`` are
    re-run unconditionally because they produce in-memory state in
    ``pipeline.state`` that is not persisted to disk.

    ``cloud_side_effect_phases`` (``upload_assets``, ``resolve_assets``) are
    *never* run as prerequisites — they touch the Roblox Open Cloud API and
    must only run when explicitly targeted by the ``assemble`` skill phase.
    Running them as silent prerequisites to ``materials`` or ``transpile``
    would leak quota / money on every review-phase invocation.
    """
    if target_phase not in PHASES:
        raise click.UsageError(
            f"Unknown pipeline phase '{target_phase}'. Valid: {PHASES}"
        )

    essential_phases = {
        "parse",
        "extract_assets",
        "convert_materials",
        "transpile_scripts",
        "convert_animations",
        "convert_scene",
    }
    cloud_side_effect_phases = {"upload_assets", "resolve_assets"}
    target_idx = PHASES.index(target_phase)
    for prior in PHASES[:target_idx]:
        if prior in cloud_side_effect_phases:
            # Only `assemble` is allowed to run these; never as a prerequisite
            # for a review phase like `materials` or `transpile`.
            continue
        if prior in essential_phases or prior not in pipeline.ctx.completed_phases:
            pipeline._run_phase(prior)
    pipeline._run_phase(target_phase)


def _next_skill_phase(completed: list[str]) -> str | None:
    for phase in SKILL_PHASES:
        if phase not in completed:
            return phase
    return None


def _ctx_summary(ctx: ConversionContext) -> dict:
    """Pull skill-relevant fields out of a ConversionContext."""
    return {
        "unity_project_path": ctx.unity_project_path,
        "selected_scene": Path(ctx.selected_scene).name if ctx.selected_scene else "",
        "scene_count": len(ctx.scene_paths),
        "total_game_objects": ctx.total_game_objects,
        "converted_parts": ctx.converted_parts,
        "total_scripts": ctx.total_scripts,
        "transpiled_scripts": ctx.transpiled_scripts,
        "total_materials": ctx.total_materials,
        "converted_materials": ctx.converted_materials,
        "uploaded_assets": len(ctx.uploaded_assets),
        "asset_upload_errors": len(ctx.asset_upload_errors),
        "warnings": len(ctx.warnings),
        "errors": len(ctx.errors),
        "completed_pipeline_phases": list(ctx.completed_phases),
    }


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose: bool) -> None:
    """Interactive Unity → Roblox conversion (one phase at a time).

    Used by the ``/convert-unity`` Claude Code skill.  For non-interactive
    end-to-end conversion, use ``u2r.py convert`` instead.
    """
    from utils.logging_config import setup_logging

    setup_logging(level="DEBUG" if verbose else "INFO")


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("unity_project_path", type=click.Path())
@click.argument("output_dir", type=click.Path())
@click.option("--install", is_flag=True, help="Auto-install missing packages.")
def preflight(unity_project_path: str, output_dir: str, install: bool) -> None:
    """Check Python version, packages, and Unity project validity."""
    import subprocess

    result: dict[str, Any] = {"phase": "preflight", "success": True}
    result["python_version"] = sys.version.split()[0]

    # Matches ``requires-python = ">=3.11"`` in pyproject.toml.
    if sys.version_info < (3, 11):
        result["success"] = False
        result["python_error"] = f"Python >= 3.11 required, got {sys.version}"

    # Hard dependencies — the pipeline cannot run without these.
    required = {
        "yaml": "PyYAML",
        "lxml": "lxml",
        "click": "click",
        "PIL": "Pillow",
        "trimesh": "trimesh",
        "lz4": "lz4",
        "numpy": "numpy",
        "requests": "requests",  # roblox/cloud_api.py imports this
    }
    # Soft dependencies — only needed for opt-in features (AI transpilation).
    # Missing these is reported as a warning, not a failure.
    optional = {
        "anthropic": "anthropic",  # Only needed for AI-assisted transpilation
    }

    def _import_missing(mapping: dict[str, str]) -> list[str]:
        missing: list[str] = []
        for mod, pkg in mapping.items():
            try:
                __import__(mod)
            except ImportError:
                missing.append(pkg)
        return missing

    missing = _import_missing(required)
    missing_optional = _import_missing(optional)

    if (missing or missing_optional) and install:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", *missing, *missing_optional],
            capture_output=True,
        )
        missing = _import_missing(required)
        missing_optional = _import_missing(optional)
        result["install_ran"] = True
    result["missing_packages"] = missing
    result["missing_optional_packages"] = missing_optional

    unity_path = Path(unity_project_path)
    result["unity_project_valid"] = (
        unity_path.is_dir() and (unity_path / "Assets").is_dir()
    )
    if not result["unity_project_valid"]:
        # Try one level deeper (nested Unity project layout).
        if unity_path.is_dir():
            for child in unity_path.iterdir():
                if child.is_dir() and (child / "Assets").is_dir():
                    result["unity_project_valid"] = True
                    result["nested_root"] = str(child)
                    break

    if not result["unity_project_valid"]:
        result["success"] = False
    if missing:
        result["success"] = False

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    result["output_dir"] = str(out.resolve())

    _emit(result)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("output_dir", type=click.Path())
def status(output_dir: str) -> None:
    """Show the current state of a conversion in progress."""
    out = Path(output_dir).resolve()
    skill_state = _load_skill_state(out)
    ctx_path = _context_path(out)

    if not ctx_path.exists() and not skill_state:
        _emit({
            "phase": "status",
            "status": "no_conversion",
            "message": f"No conversion in progress at {out}.",
        })
        return

    completed_skill = skill_state.get("completed_skill_phases", [])
    next_phase = _next_skill_phase(completed_skill)

    payload: dict[str, Any] = {
        "phase": "status",
        "status": "in_progress" if next_phase else "complete",
        "output_dir": str(out),
        "completed_skill_phases": completed_skill,
        "next_skill_phase": next_phase,
    }
    if ctx_path.exists():
        ctx = ConversionContext.load(ctx_path)
        payload["context"] = _ctx_summary(ctx)
        payload["errors"] = ctx.errors[-10:] if ctx.errors else []

    _emit(payload)


# ---------------------------------------------------------------------------
# discover  →  pipeline.parse
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("unity_project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("output_dir", type=click.Path())
@click.option("--scene", type=str, default=None,
              help="Specific .unity scene to select (path relative to project).")
def discover(unity_project_path: str, output_dir: str, scene: str | None) -> None:
    """Phase 1: parse Unity scenes and prefabs (pipeline phase: parse)."""
    pipeline = _make_pipeline(unity_project_path, output_dir)
    if scene:
        pipeline.ctx.selected_scene = scene

    try:
        _run_through(pipeline, "parse")
    except Exception as exc:
        _emit({
            "phase": "discover",
            "success": False,
            "errors": [str(exc)],
        })
        sys.exit(1)

    ctx = pipeline.ctx
    _mark_skill_phase(
        Path(output_dir).resolve(),
        "discover",
        unity_project_path=ctx.unity_project_path,
    )

    parsed = pipeline.state.parsed_scene
    payload: dict[str, Any] = {
        "phase": "discover",
        "success": True,
        "selected_scene": Path(ctx.selected_scene).name if ctx.selected_scene else "",
        "scene_count": len(ctx.scene_paths),
        "scene_paths": [str(Path(p).name) for p in ctx.scene_paths],
        "total_game_objects": ctx.total_game_objects,
        "errors": ctx.errors[-10:] if ctx.errors else [],
    }
    if parsed is not None:
        payload["roots"] = len(parsed.roots)
        payload["referenced_material_guids"] = len(parsed.referenced_material_guids)
        payload["referenced_mesh_guids"] = len(parsed.referenced_mesh_guids)

    _emit(payload)


# ---------------------------------------------------------------------------
# inventory  →  pipeline.extract_assets
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("unity_project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("output_dir", type=click.Path())
def inventory(unity_project_path: str, output_dir: str) -> None:
    """Phase 2: extract assets and build the asset manifest."""
    pipeline = _make_pipeline(unity_project_path, output_dir)
    try:
        _run_through(pipeline, "extract_assets")
    except Exception as exc:
        _emit({"phase": "inventory", "success": False, "errors": [str(exc)]})
        sys.exit(1)

    _mark_skill_phase(Path(output_dir).resolve(), "inventory")

    manifest = pipeline.state.asset_manifest
    by_kind: dict[str, int] = {}
    total_size_mb = 0.0
    if manifest is not None:
        by_kind = {k: len(v) for k, v in manifest.by_kind.items()}
        total_size_mb = round(manifest.total_size_bytes / 1_048_576, 1)

    guid = pipeline.state.guid_index
    guid_info: dict[str, Any] = {}
    if guid is not None:
        guid_info = {
            "total_resolved": guid.total_resolved,
            "total_meta_files": getattr(guid, "total_meta_files", 0),
        }

    _emit({
        "phase": "inventory",
        "success": True,
        "assets": {
            "total": sum(by_kind.values()),
            "total_size_mb": total_size_mb,
            "by_kind": by_kind,
        },
        "guid_index": guid_info,
        "fbx_bounding_boxes_computed": len(pipeline.ctx.fbx_bounding_boxes),
    })


# ---------------------------------------------------------------------------
# materials  →  pipeline.convert_materials
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("unity_project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("output_dir", type=click.Path())
def materials(unity_project_path: str, output_dir: str) -> None:
    """Phase 3a: map Unity materials to Roblox SurfaceAppearance."""
    pipeline = _make_pipeline(unity_project_path, output_dir)
    try:
        _run_through(pipeline, "convert_materials")
    except Exception as exc:
        _emit({"phase": "materials", "success": False, "errors": [str(exc)]})
        sys.exit(1)

    _mark_skill_phase(Path(output_dir).resolve(), "materials")

    ctx = pipeline.ctx
    mappings = pipeline.state.material_mappings or {}
    _emit({
        "phase": "materials",
        "success": True,
        "total": ctx.total_materials,
        "converted": ctx.converted_materials,
        "mappings_count": len(mappings),
        "warnings": ctx.warnings[-10:] if ctx.warnings else [],
    })


# ---------------------------------------------------------------------------
# transpile  →  pipeline.transpile_scripts
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("unity_project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("output_dir", type=click.Path())
@click.option("--api-key", type=str, default=None,
              help="Anthropic API key (string or path to a key file).")
@click.option("--no-ai", is_flag=True,
              help="Disable AI fallback; use rule-based transpilation only.")
def transpile(unity_project_path: str, output_dir: str,
              api_key: str | None, no_ai: bool) -> None:
    """Phase 3b: transpile C# scripts to Luau."""
    if api_key:
        ak = Path(api_key)
        key_value = ak.read_text().strip() if ak.is_file() else api_key.strip()
        config.ANTHROPIC_API_KEY = key_value
        # ``converter/pipeline.py`` does ``from config import ANTHROPIC_API_KEY``
        # at module load time, so mutating ``config.ANTHROPIC_API_KEY`` alone
        # leaves the pipeline module's local binding pointing at the old value
        # (typically ``None``).  Update it too so the transpiler actually sees
        # the key the user supplied on the CLI.
        from converter import pipeline as _pipeline_module
        _pipeline_module.ANTHROPIC_API_KEY = key_value
    if no_ai:
        config.USE_AI_TRANSPILATION = False

    pipeline = _make_pipeline(unity_project_path, output_dir)
    try:
        _run_through(pipeline, "transpile_scripts")
    except Exception as exc:
        _emit({"phase": "transpile", "success": False, "errors": [str(exc)]})
        sys.exit(1)

    _mark_skill_phase(Path(output_dir).resolve(), "transpile")

    ctx = pipeline.ctx
    result = pipeline.state.transpilation_result
    flagged_files: list[dict] = []
    if result is not None and hasattr(result, "scripts"):
        for ts in getattr(result, "scripts", []):
            if getattr(ts, "flagged_for_review", False):
                flagged_files.append({
                    "source": str(getattr(ts, "source_path", "")),
                    "output": getattr(ts, "output_filename", ""),
                    "confidence": round(getattr(ts, "confidence", 0.0), 2),
                    "warnings": getattr(ts, "warnings", [])[:5],
                })

    _emit({
        "phase": "transpile",
        "success": True,
        "total_scripts": ctx.total_scripts,
        "transpiled": ctx.transpiled_scripts,
        "flagged_for_review": len(flagged_files),
        "flagged_files": flagged_files[:20],
    })


# ---------------------------------------------------------------------------
# validate — runs the Luau validator over transpiled output on disk
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--write/--no-write", default=False,
              help="Write fixes back to disk (default: dry-run).")
def validate(output_dir: str, write: bool) -> None:
    """Phase 3c: run the Luau validator over transpiled scripts on disk."""
    out = Path(output_dir).resolve()

    candidates: list[Path] = []
    for sub in ("scripts", "luau", "Luau"):
        d = out / sub
        if d.is_dir():
            candidates.extend(d.glob("*.lua"))
            candidates.extend(d.glob("*.luau"))
    if not candidates:
        _emit({
            "phase": "validate",
            "success": False,
            "errors": [f"No .lua/.luau files found under {out}."],
        })
        return

    from converter.luau_validator import validate_and_fix

    files_with_fixes: list[dict] = []
    total_fixes = 0
    for path in sorted(candidates):
        source = path.read_text(encoding="utf-8")
        fixed_source, fixes = validate_and_fix(path.name, source)
        if fixes:
            files_with_fixes.append({
                "file": str(path.relative_to(out)),
                "fix_count": len(fixes),
                "fixes": fixes[:10],
            })
            total_fixes += len(fixes)
            if write and fixed_source != source:
                path.write_text(fixed_source, encoding="utf-8")

    _mark_skill_phase(out, "validate")

    _emit({
        "phase": "validate",
        "success": True,
        "files_scanned": len(candidates),
        "files_with_fixes": len(files_with_fixes),
        "total_fixes": total_fixes,
        "wrote_changes": write,
        "details": files_with_fixes[:50],
    })


# ---------------------------------------------------------------------------
# assemble  →  upload_assets, resolve_assets, convert_animations,
#              convert_scene, write_output
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("unity_project_path", type=click.Path(exists=True, file_okay=False))
@click.argument("output_dir", type=click.Path())
@click.option("--no-upload", is_flag=True,
              help="Skip asset upload (placeholder URLs in the .rbxlx).")
@click.option("--no-resolve", is_flag=True,
              help="Skip headless mesh resolution.")
@click.option("--api-key", type=str, default=None,
              help="Roblox Open Cloud API key (string or path to file).")
@click.option("--creator-id", type=str, default=None,
              help="Roblox Creator ID (number or path to file).")
def assemble(unity_project_path: str, output_dir: str,
             no_upload: bool, no_resolve: bool,
             api_key: str | None, creator_id: str | None) -> None:
    """Phase 4: upload assets, resolve, convert animations + scene, write .rbxlx."""
    if api_key:
        ak = Path(api_key)
        config.ROBLOX_API_KEY = (
            ak.read_text().strip() if ak.is_file() else api_key.strip()
        )
    if creator_id:
        cid = Path(creator_id)
        raw = cid.read_text().strip() if cid.is_file() else creator_id.strip()
        config.ROBLOX_CREATOR_ID = int(raw)

    pipeline = _make_pipeline(unity_project_path, output_dir, skip_upload=no_upload)

    # Run every prerequisite + the assembly phases in order, but stop at
    # write_output (don't trigger headless publish — that's the upload step).
    try:
        for phase in [
            "parse", "extract_assets", "upload_assets", "resolve_assets",
            "convert_materials", "transpile_scripts", "convert_animations",
            "convert_scene", "write_output",
        ]:
            if no_resolve and phase == "resolve_assets":
                continue
            pipeline._run_phase(phase)
    except Exception as exc:
        _emit({"phase": "assemble", "success": False, "errors": [str(exc)]})
        sys.exit(1)

    out = Path(output_dir).resolve()
    rbxlx_path = out / "converted_place.rbxlx"
    rbxlx_size_mb = (
        round(rbxlx_path.stat().st_size / 1_048_576, 2)
        if rbxlx_path.exists() else 0.0
    )

    _mark_skill_phase(out, "assemble", rbxlx_path=str(rbxlx_path))

    ctx = pipeline.ctx
    _emit({
        "phase": "assemble",
        "success": True,
        "rbxlx_path": str(rbxlx_path),
        "rbxlx_size_mb": rbxlx_size_mb,
        "parts_written": ctx.converted_parts,
        "scripts_written": ctx.transpiled_scripts,
        "uploaded_assets": len(ctx.uploaded_assets),
        "asset_upload_errors": len(ctx.asset_upload_errors),
        "warnings": ctx.warnings[-5:] if ctx.warnings else [],
    })


# ---------------------------------------------------------------------------
# upload — headless place publish via Roblox Open Cloud execute_luau
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--api-key", type=str, default=None,
              help="Roblox Open Cloud API key (string or path to file).")
@click.option("--universe-id", type=int, default=None,
              help="Roblox Universe ID (cached after first use).")
@click.option("--place-id", type=int, default=None,
              help="Roblox Place ID (cached after first use).")
def upload(output_dir: str, api_key: str | None,
           universe_id: int | None, place_id: int | None) -> None:
    """Phase 5: publish the .rbxlx to Roblox via headless place builder."""
    out = Path(output_dir).resolve()
    ctx_path = _context_path(out)
    if not ctx_path.exists():
        _emit({
            "phase": "upload",
            "success": False,
            "errors": [f"No conversion_context.json in {out}. Run 'assemble' first."],
        })
        sys.exit(1)

    rbxlx_path = out / "converted_place.rbxlx"
    if not rbxlx_path.exists():
        _emit({
            "phase": "upload",
            "success": False,
            "errors": [f"RBXLX not found: {rbxlx_path}. Run 'assemble' first."],
        })
        sys.exit(1)

    if api_key:
        ak = Path(api_key)
        config.ROBLOX_API_KEY = (
            ak.read_text().strip() if ak.is_file() else api_key.strip()
        )
    if not config.ROBLOX_API_KEY:
        _emit({
            "phase": "upload",
            "success": False,
            "errors": ["No Roblox API key. Pass --api-key or set ROBLOX_API_KEY."],
        })
        sys.exit(1)

    # Resolve universe/place IDs from, in priority order:
    #   1. CLI flags                           (user override)
    #   2. Persisted ConversionContext          (set by pipeline.resolve_assets
    #                                            when it auto-creates an experience)
    #   3. ``.roblox_ids.json``                 (written by pipeline.resolve_assets
    #                                            as the canonical ID cache)
    #   4. ``resolve_ids.json``                 (legacy cache written by this CLI)
    uid, pid = universe_id, place_id

    if not uid or not pid:
        try:
            prior_ctx = ConversionContext.load(ctx_path)
            uid = uid or prior_ctx.universe_id
            pid = pid or prior_ctx.place_id
        except Exception as exc:  # noqa: BLE001 — surfaced to user below if still missing
            log.debug("upload: could not read ctx for id fallback: %s", exc)

    ids_file = out / "resolve_ids.json"
    for cache_path in (out / ".roblox_ids.json", ids_file):
        if uid and pid:
            break
        if not cache_path.exists():
            continue
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            uid = uid or cached.get("universe_id")
            pid = pid or cached.get("place_id")
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("upload: could not read %s: %s", cache_path.name, exc)

    if not uid or not pid:
        _emit({
            "phase": "upload",
            "success": False,
            "errors": [
                "Roblox universe_id/place_id required. "
                "Create an experience at https://create.roblox.com and pass "
                "--universe-id and --place-id."
            ],
        })
        sys.exit(1)

    # Re-run the pipeline through convert_scene so we have rbx_place in memory
    # for the place builder.
    pipeline = _make_pipeline(None, out)
    pipeline.ctx.universe_id = uid
    pipeline.ctx.place_id = pid

    try:
        for phase in [
            "parse", "extract_assets", "convert_materials",
            "transpile_scripts", "convert_animations", "convert_scene",
        ]:
            pipeline._run_phase(phase)
    except Exception as exc:
        _emit({"phase": "upload", "success": False,
               "errors": [f"Failed to rebuild scene state: {exc}"]})
        sys.exit(1)

    rbx_place = pipeline.state.rbx_place
    if rbx_place is None:
        _emit({"phase": "upload", "success": False,
               "errors": ["rbx_place is empty after rebuilding scene state."]})
        sys.exit(1)

    from roblox.cloud_api import execute_luau
    from roblox.luau_place_builder import generate_place_luau_chunked

    chunks = generate_place_luau_chunked(rbx_place)
    total_size = sum(len(c) for c in chunks)

    script_file = out / "place_builder.luau"
    script_file.write_text("\n\n".join(chunks) if len(chunks) > 1 else chunks[0])

    if total_size > MAX_EXECUTE_LUAU_BYTES:
        _emit({
            "phase": "upload",
            "success": False,
            "errors": [
                f"Place builder script exceeds {MAX_EXECUTE_LUAU_BYTES // 1_000_000}MB limit "
                f"({total_size/1024/1024:.1f} MB). "
                "Use the local rbxlx with the runtime MeshLoader instead."
            ],
            "script_path": str(script_file),
        })
        sys.exit(1)

    chunk_results: list[dict] = []
    all_ok = True
    for i, chunk in enumerate(chunks):
        log.info("Executing place builder chunk %d/%d", i + 1, len(chunks))
        result = execute_luau(
            config.ROBLOX_API_KEY, uid, pid, chunk, timeout="300s",
        )
        ok = result is not None
        chunk_results.append({"chunk": i + 1, "ok": ok})
        if not ok:
            all_ok = False
            break

    pipeline.ctx.save(_context_path(out))

    if all_ok:
        # Only cache universe/place IDs after a successful publish — avoids
        # persisting bad IDs that would be silently reused next run.
        ids_file.write_text(json.dumps({"universe_id": uid, "place_id": pid}))
        _mark_skill_phase(out, "upload",
                          universe_id=uid, place_id=pid,
                          success=True)
    else:
        # Record the failure without advancing the workflow.  Do NOT add
        # "upload" to completed_skill_phases — that would let `status`
        # advance to `report` on a broken publish.
        state = _load_skill_state(out)
        state["last_upload_failure"] = {
            "universe_id": uid,
            "place_id": pid,
            "chunks": len(chunks),
        }
        _save_skill_state(out, state)

    _emit({
        "phase": "upload",
        "success": all_ok,
        "universe_id": uid,
        "place_id": pid,
        "chunks": len(chunks),
        "script_size_kb": round(total_size / 1024, 1),
        "script_path": str(script_file),
        "chunk_results": chunk_results,
    })


# ---------------------------------------------------------------------------
# report — write a JSON conversion report
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("output_dir", type=click.Path(exists=True, file_okay=False))
def report(output_dir: str) -> None:
    """Phase 6: generate a final JSON conversion report."""
    out = Path(output_dir).resolve()
    ctx_path = _context_path(out)
    if not ctx_path.exists():
        _emit({"phase": "report", "success": False,
               "errors": [f"No conversion_context.json in {out}."]})
        sys.exit(1)

    ctx = ConversionContext.load(ctx_path)
    skill_state = _load_skill_state(out)

    rbxlx_path = out / "converted_place.rbxlx"
    rbxlx_size_mb = (
        round(rbxlx_path.stat().st_size / 1_048_576, 2)
        if rbxlx_path.exists() else 0.0
    )

    report_data = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "unity_project_path": ctx.unity_project_path,
        "output_dir": str(out),
        "selected_scene": Path(ctx.selected_scene).name if ctx.selected_scene else "",
        "rbxlx_path": str(rbxlx_path) if rbxlx_path.exists() else None,
        "rbxlx_size_mb": rbxlx_size_mb,
        "stats": _ctx_summary(ctx),
        "completed_pipeline_phases": ctx.completed_phases,
        "completed_skill_phases": skill_state.get("completed_skill_phases", []),
        "universe_id": ctx.universe_id,
        "place_id": ctx.place_id,
        "experience_name": ctx.experience_name,
        "warnings": ctx.warnings,
        "errors": ctx.errors,
        "asset_upload_errors": ctx.asset_upload_errors,
    }

    report_path = out / "conversion_report.json"
    report_path.write_text(json.dumps(report_data, indent=2, default=str),
                           encoding="utf-8")

    _mark_skill_phase(out, "report", report_path=str(report_path))

    _emit({
        "phase": "report",
        "success": True,
        "report_path": str(report_path),
        "summary": report_data,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    cli()
