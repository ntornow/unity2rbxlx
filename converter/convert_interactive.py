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
validate        (no pipeline call — runs luau-analyze syntax check)
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
from collections.abc import Iterable
from typing import Any

import click

import config
from converter.pipeline import PHASES, Pipeline
from core.conversion_context import ConversionContext
from utils.credentials import resolve_credential as _resolve_credential
from utils.script_cache import scripts_cache_intact

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
    skip_binary_rbxl: bool = False,
    scaffolding: Iterable[str] | None = None,
    use_gameplay_adapters: bool | None = None,
) -> Pipeline:
    """Build a Pipeline and rehydrate ctx from disk if a previous run exists.

    Accepts ``unity_project_path=None`` for phases that only read state
    (e.g. ``status``, ``upload``, ``report``).  In that case the unity project
    path is recovered from the persisted ``ConversionContext``.

    *scaffolding* is the caller's NEW request (e.g. ``--scaffolding=fps``
    on this invocation). It's merged with whatever was persisted in
    ``conversion_context.json`` from prior runs — additive so a follow-up
    ``upload`` against an existing assemble doesn't drop the previously
    requested FPS scripts/HUD.

    *use_gameplay_adapters* is the same tri-state the Pipeline
    constructor takes (PR #74 codex round-1 [P1]): ``None`` means the
    caller had no preference for this invocation, so the persisted
    ``ctx.use_gameplay_adapters`` wins on rehydration; ``True`` /
    ``False`` is an explicit override that beats persisted state.
    Forwarded twice — once to the constructor (so a fresh ctx picks
    the right default) AND once AFTER the ``pipeline.ctx = prior_ctx``
    swap below (so the rehydrated ctx also honours the explicit
    choice). Codex PR #74 round-1 [P2] flagged that without
    rebinding, the only interactive entrypoints lose the rollback
    lever the u2r.py CLI exposes.
    """
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    ctx_path = _context_path(out)
    requested_by_caller = unity_project_path is not None
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
        skip_binary_rbxl=skip_binary_rbxl,
        scaffolding=scaffolding,
        use_gameplay_adapters=use_gameplay_adapters,
    )
    if ctx_path.exists():
        prior_ctx = ConversionContext.load(ctx_path)
        # Refuse to silently mix a fresh Unity project with a context saved
        # for a different one: stale selected_scene / GUID index / upload IDs
        # would feed the new project and corrupt its output. Only compare
        # when the caller explicitly named a project — the None branch above
        # deliberately recovers the path from prior_ctx.
        if requested_by_caller and prior_ctx.unity_project_path and (
            Path(prior_ctx.unity_project_path).resolve()
            != Path(unity_project_path).resolve()
        ):
            raise click.UsageError(
                f"{ctx_path} has conversion state for "
                f"{prior_ctx.unity_project_path}, but this command was "
                f"invoked with {unity_project_path}. Use a fresh output "
                f"directory or delete {out} and start over."
            )
        pipeline.ctx = prior_ctx
        # Mark as explicit resume so the FPS migration treats on-disk
        # FPS scripts as legitimately preserved (not stale leftovers
        # from a foreign-project conversion that shared this dir).
        pipeline._is_resume = True
        # Re-snapshot the FPS migration signal AFTER the ctx swap so
        # the rbxlx scan can scope to ``ctx.selected_scene`` for
        # multi-scene runs.
        if hasattr(pipeline, "_fps_artifacts_on_disk"):
            pipeline._fps_artifacts_at_init = pipeline._fps_artifacts_on_disk()
        # Re-merge the caller's scaffolding request after the ctx swap
        # — the rehydrated ctx may carry persisted entries; the new
        # request adds to them (additive, idempotent).
        pipeline.apply_scaffolding(scaffolding)
        # Re-apply the caller's EXPLICIT gameplay-adapter choice after
        # the ctx swap, mirroring :meth:`Pipeline.resume`'s
        # post-swap re-application. ``None`` means "no preference this
        # run" so the persisted value stays — that's the sticky
        # rollback contract codex PR #74 round-1 [P1] anchored on.
        #
        # PR #74 codex round-2 [P1]: if the explicit override CHANGES
        # the persisted value, invalidate the cached transpile output
        # on disk. ``_subphase_emit_scripts_to_disk`` preserves
        # ``scripts/`` whenever ``transpile_scripts`` is in
        # ``completed_phases`` and ``--retranspile`` wasn't passed;
        # without invalidation, a flip from adapters→legacy (or vice
        # versa) silently keeps the previous mode's ``.luau`` cache
        # and the rebuilt place stays in the old mode.
        if use_gameplay_adapters is not None:
            mode_changed = (
                pipeline.ctx.use_gameplay_adapters != use_gameplay_adapters
            )
            pipeline.ctx.use_gameplay_adapters = use_gameplay_adapters
            if mode_changed:
                pipeline._invalidate_transpile_cache_for_mode_flip()
    return pipeline


# Cloud-side-effect phases are NEVER run as prerequisites for a review-only
# command like materials/transpile/validate. They hit Roblox Open Cloud and
# would leak quota/money on every silent invocation. Only the `assemble`
# skill phase is allowed to run them, by NOT passing them in `skip`.
_CLOUD_SIDE_EFFECT_PHASES: frozenset[str] = frozenset({
    "upload_assets", "resolve_assets",
})


def _run_through(pipeline: Pipeline, target_phase: str) -> None:
    """Run prerequisite phases then ``target_phase`` — but nothing after.

    Thin wrapper around :meth:`Pipeline.run_through` that excludes the cloud
    side-effect phases as silent prerequisites.
    """
    if target_phase not in PHASES:
        raise click.UsageError(
            f"Unknown pipeline phase '{target_phase}'. Valid: {PHASES}"
        )
    pipeline.run_through(target_phase, skip=_CLOUD_SIDE_EFFECT_PHASES)


def _next_skill_phase(completed: list[str]) -> str | None:
    for phase in SKILL_PHASES:
        if phase not in completed:
            return phase
    return None


def _relative_scene_path(scene_path: str, unity_project_path: str) -> str:
    """Return scene path relative to the Unity project root for disambiguation."""
    if not scene_path:
        return ""
    p = Path(scene_path)
    if not p.is_absolute():
        return str(p)
    try:
        return str(p.relative_to(unity_project_path))
    except ValueError:
        return p.name


def _ctx_summary(ctx: ConversionContext) -> dict:
    """Pull skill-relevant fields out of a ConversionContext."""
    return {
        "unity_project_path": ctx.unity_project_path,
        "selected_scene": _relative_scene_path(ctx.selected_scene, ctx.unity_project_path),
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
    # Keep this list in sync with the actual `import` statements in real source
    # (not .venv/). `lxml` and `lz4` used to be listed here but no module under
    # converter/, unity/, roblox/, runtime/, core/, or utils/ imports them; they
    # were ghost deps causing false-negative preflight failures on clean clones.
    required = {
        "yaml": "PyYAML",
        "click": "click",
        "PIL": "Pillow",
        "trimesh": "trimesh",
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
        "selected_scene": _relative_scene_path(ctx.selected_scene, ctx.unity_project_path),
        "scene_count": len(ctx.scene_paths),
        "scene_paths": [_relative_scene_path(p, ctx.unity_project_path) for p in ctx.scene_paths],
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
@click.option("--use-gameplay-adapters/--no-use-gameplay-adapters",
              default=True,
              help="Route door / projectile / damage patterns through "
              "the gameplay-adapter pipeline. Default on as of PR #74. "
              "Mutually exclusive with --legacy-gameplay-packs. Codex "
              "PR #74 round-5 [P2]: the rollback lever must be on "
              "``transpile`` too, not only ``assemble`` — interactive "
              "users review their scripts/ after transpile, and "
              "operators who intend to finish with --legacy-gameplay-packs "
              "need the right Luau out of this phase.")
@click.option("--legacy-gameplay-packs", is_flag=True, default=False,
              help="Force the legacy script_coherence_packs pipeline. "
              "Mutually exclusive with --use-gameplay-adapters.")
def transpile(unity_project_path: str, output_dir: str,
              api_key: str | None, no_ai: bool,
              use_gameplay_adapters: bool,
              legacy_gameplay_packs: bool) -> None:
    """Phase 3b: transpile C# scripts to Luau."""
    if api_key:
        ak = Path(api_key)
        key_value = ak.read_text().strip() if ak.is_file() else api_key.strip()
        config.ANTHROPIC_API_KEY = key_value
    if no_ai:
        config.USE_AI_TRANSPILATION = False

    # PR #74 codex round-5 [P2]: resolve the gameplay-mode tri-state
    # the same way ``assemble`` (and ``u2r.py convert``) does. Only
    # forward an explicit bool when the user passed a flag on the
    # CLI; otherwise ``None`` so the persisted ctx wins on rehydrate.
    ctx_click = click.get_current_context()
    adapter_source = ctx_click.get_parameter_source("use_gameplay_adapters")
    adapter_explicit = (
        adapter_source == click.core.ParameterSource.COMMANDLINE
    )
    pipeline_use_gameplay_adapters: bool | None
    if legacy_gameplay_packs:
        if adapter_explicit and use_gameplay_adapters:
            raise click.UsageError(
                "--use-gameplay-adapters and --legacy-gameplay-packs "
                "are mutually exclusive. Pass exactly one or neither.",
            )
        pipeline_use_gameplay_adapters = False
    elif adapter_explicit:
        pipeline_use_gameplay_adapters = use_gameplay_adapters
    else:
        pipeline_use_gameplay_adapters = None

    pipeline = _make_pipeline(
        unity_project_path, output_dir,
        use_gameplay_adapters=pipeline_use_gameplay_adapters,
    )
    try:
        _run_through(pipeline, "transpile_scripts")
    except Exception as exc:
        _emit({"phase": "transpile", "success": False, "errors": [str(exc)]})
        sys.exit(1)

    _mark_skill_phase(Path(output_dir).resolve(), "transpile")

    ctx = pipeline.ctx
    result = pipeline.state.transpilation_result

    # Persist Luau sources to disk so the subsequent `validate` command
    # (which reads from scripts/*.luau) and the preserved-scripts assemble
    # path (which rehydrates from disk when transpile_scripts is skipped)
    # both see this run's output. write_output's fresh-transpile branch
    # does the same write; doing it here keeps the transpile->validate
    # workflow correct even when the user hasn't run assemble yet.
    # Scoped wipe: clear only top-level stale .luau so animations/,
    # animation_data/, and scriptable_objects/ subdirs written by other
    # phases survive.
    #
    # PR #74 codex round-9 [P2]: a mode-flip run that produces zero
    # transpiled scripts (Unity project no longer has runtime C#
    # files, or every script matched an adapter and the legacy mode
    # has no fallback) must STILL wipe the stale previous-mode
    # ``.luau`` cache. ``_make_pipeline``'s
    # ``_invalidate_transpile_cache_for_mode_flip()`` sets
    # ``pipeline._retranspile = True`` whenever the explicit override
    # differs from the persisted ctx; honour that signal here too,
    # not just the ``result.scripts`` branch.
    forced_wipe = bool(getattr(pipeline, "_retranspile", False))
    has_scripts = bool(result is not None and getattr(result, "scripts", None))
    if forced_wipe or has_scripts:
        scripts_dir = Path(output_dir).resolve() / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        for stale in scripts_dir.glob("*.luau"):
            stale.unlink()
        if has_scripts:
            for ts in result.scripts:
                (scripts_dir / ts.output_filename).write_text(
                    ts.luau_source, encoding="utf-8",
                )

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

    # Phase 4.4 extension: recurse into subdirs so the luau-analyze
    # gate covers newly-added paths — scripts/animations/ from PR 2,
    # scripts/animation_data/ from PR 2, scripts/packages/ from PR 5,
    # scripts/scriptable_objects/ from Phase 3.
    candidates: list[Path] = []
    for sub in ("scripts", "luau", "Luau"):
        d = out / sub
        if d.is_dir():
            candidates.extend(d.rglob("*.lua"))
            candidates.extend(d.rglob("*.luau"))
    if not candidates:
        _emit({
            "phase": "validate",
            "success": False,
            "errors": [f"No .lua/.luau files found under {out}."],
        })
        return

    import subprocess, shutil
    analyzer = shutil.which("luau-analyze")

    files_with_fixes: list[dict] = []
    total_fixes = 0
    for path in sorted(candidates):
        if analyzer:
            result = subprocess.run(
                [analyzer, str(path)],
                capture_output=True, text=True, timeout=10,
            )
            errors = [l for l in (result.stdout + result.stderr).splitlines()
                      if "SyntaxError" in l]
            if errors:
                files_with_fixes.append({
                    "file": str(path.relative_to(out)),
                    "fix_count": len(errors),
                    "fixes": errors[:10],
                })
                total_fixes += len(errors)

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
@click.option("--retranspile", is_flag=True,
              help="Force re-transpilation even if scripts were already transpiled. "
              "Without this flag, hand-edited Luau scripts in output_dir/scripts/ "
              "are preserved.")
@click.option("--api-key", type=str, default=None,
              help="Roblox Open Cloud API key (string or path to file).")
@click.option("--creator-id", type=str, default=None,
              help="Roblox Creator ID (number or path to file).")
@click.option("--universe-id", type=int, default=None,
              help="Roblox Universe ID for headless mesh resolution. "
              "Required for the local converted_place.rbxlx to be valid "
              "when the project has FBX/OBJ meshes. Cached after the "
              "first successful publish in <output>/.roblox_ids.json.")
@click.option("--place-id", type=int, default=None,
              help="Roblox Place ID for headless mesh resolution "
              "(see --universe-id).")
@click.option("--scaffolding", type=str, default=None,
              help="Comma-separated genre scaffolding to inject (e.g. "
              "'fps' to add the FPS client controller + HUD ScreenGui + "
              "HUDController). Default: none. Persisted in "
              "conversion_context.json so subsequent ``upload`` re-runs "
              "against the same output dir reproduce the same scripts.")
@click.option("--use-gameplay-adapters/--no-use-gameplay-adapters",
              default=True,
              help="Route door / projectile / damage patterns through "
              "the gameplay-adapter pipeline. Default on as of PR #74. "
              "Mutually exclusive with --legacy-gameplay-packs. Mirrors "
              "u2r.py convert so interactive assemble has the same "
              "rollback lever (codex PR #74 round-1 [P2]).")
@click.option("--legacy-gameplay-packs", is_flag=True, default=False,
              help="Force the legacy script_coherence_packs pipeline; "
              "disables every adapter runtime module. Rollback lever "
              "for the PR #74 default-on flip in the interactive "
              "assemble flow. Mutually exclusive with "
              "--use-gameplay-adapters.")
def assemble(unity_project_path: str, output_dir: str,
             no_upload: bool, no_resolve: bool, retranspile: bool,
             api_key: str | None, creator_id: str | None,
             universe_id: int | None, place_id: int | None,
             scaffolding: str | None,
             use_gameplay_adapters: bool,
             legacy_gameplay_packs: bool) -> None:
    """Phase 4: upload assets, resolve, convert animations + scene, write .rbxlx."""
    # Resolve credentials from CLI -> env -> file (same precedence as u2r.py)
    # so users get the documented auto-discovery behavior. Without this,
    # assemble without --api-key silently ran with an empty config, every
    # upload_assets call no-opped, and the final payload reported
    # success=True with zero uploads.
    project_path = Path(unity_project_path).resolve()
    resolved_key = _resolve_credential(api_key, "ROBLOX_API_KEY", "apikey", project_path)
    if resolved_key:
        config.ROBLOX_API_KEY = resolved_key
    resolved_cid = _resolve_credential(
        creator_id, "ROBLOX_CREATOR_ID", "creator_id", project_path,
    )
    if resolved_cid:
        config.ROBLOX_CREATOR_ID = int(resolved_cid)

    # Fail fast when uploads are intended but creds can't be found.
    # Reliably detecting "this rerun has no new cloud work" requires
    # comparing the on-disk asset manifest against ctx.uploaded_assets,
    # which we don't have until extract_assets runs. Rather than guess
    # with a heuristic that misses new assets, we ask users to pass
    # ``--no-upload`` explicitly when they want an offline rerun against
    # an already-published output directory. That's the same flag they'd
    # use for a fresh offline conversion, so it's discoverable.
    if not no_upload and (
        not config.ROBLOX_API_KEY or config.ROBLOX_CREATOR_ID is None
    ):
        _emit({"phase": "assemble", "success": False, "errors": [
            "Roblox Open Cloud credentials not found. Pass --api-key and "
            "--creator-id, set ROBLOX_API_KEY and ROBLOX_CREATOR_ID env "
            "vars, place 'apikey' and 'creator_id' files in the Unity "
            "project parent or current working directory, or pass "
            "--no-upload to skip asset upload (use this when reassembling "
            "an output directory whose uploads are already complete)."
        ]})
        sys.exit(1)

    scaffolding_list = [
        s.strip().lower() for s in (scaffolding or "").split(",") if s.strip()
    ]
    # Resolve the gameplay-mode tri-state the same way ``u2r.py
    # convert`` does (codex PR #74 round-1 [P1] / [P2]). Only forward
    # an explicit bool when the user passed a flag on the CLI —
    # otherwise ``None`` so a re-assemble of an output originally
    # converted with ``--legacy-gameplay-packs`` keeps its rollback
    # choice sticky.
    ctx_click = click.get_current_context()
    adapter_source = ctx_click.get_parameter_source("use_gameplay_adapters")
    adapter_explicit = (
        adapter_source == click.core.ParameterSource.COMMANDLINE
    )
    pipeline_use_gameplay_adapters: bool | None
    if legacy_gameplay_packs:
        if adapter_explicit and use_gameplay_adapters:
            raise click.UsageError(
                "--use-gameplay-adapters and --legacy-gameplay-packs "
                "are mutually exclusive. --legacy-gameplay-packs is "
                "the rollback opt-out for the PR #74 default-on flip; "
                "pass exactly one or neither.",
            )
        pipeline_use_gameplay_adapters = False
    elif adapter_explicit:
        pipeline_use_gameplay_adapters = use_gameplay_adapters
    else:
        pipeline_use_gameplay_adapters = None

    pipeline = _make_pipeline(
        unity_project_path, output_dir,
        skip_upload=no_upload,
        scaffolding=scaffolding_list,
        use_gameplay_adapters=pipeline_use_gameplay_adapters,
    )
    # PR #74 codex round-8 [P2]: OR with the existing value rather
    # than overwrite. ``_make_pipeline`` calls
    # ``_invalidate_transpile_cache_for_mode_flip()`` when the
    # explicit gameplay-mode override differs from the persisted
    # ctx; that sets ``pipeline._retranspile = True`` so
    # ``_subphase_emit_scripts_to_disk`` wipes the previous mode's
    # ``scripts/`` cache. An unconditional ``pipeline._retranspile =
    # retranspile`` write here clobbered that ``True`` back to the
    # caller's flag — and if ``transpile_scripts`` runs zero scripts
    # this pass (Unity project no longer has runtime C# files),
    # the preserve-scripts fallback rehydrates the stale cache.
    pipeline._retranspile = retranspile or getattr(
        pipeline, "_retranspile", False,
    )

    # Plumb --universe-id / --place-id into ctx so resolve_assets can run
    # headless mesh resolution on the first assemble invocation. Without
    # these (or a previously-cached pair in <output>/.roblox_ids.json),
    # resolve_assets has no way to call CreateMeshPartAsync and the local
    # converted_place.rbxlx ends up with raw Model IDs that Studio fails
    # to fetch — see the hard-fail in pipeline.resolve_assets.
    if universe_id:
        pipeline.ctx.universe_id = universe_id
    if place_id:
        pipeline.ctx.place_id = place_id

    # When transpile_scripts already ran and --retranspile is not set, skip
    # re-transpilation so hand-edited Luau scripts are preserved. But only
    # if the on-disk cache survived: an archived/partially-copied output
    # dir without scripts/ would otherwise rehydrate nothing.
    out_path = Path(output_dir).resolve()
    skip: set[str] = set()
    if (
        not retranspile
        and "transpile_scripts" in pipeline.ctx.completed_phases
        and scripts_cache_intact(out_path, pipeline.ctx.transpiled_scripts)
    ):
        skip.add("transpile_scripts")
    if no_resolve:
        skip.add("resolve_assets")
    # Cloud side-effect phases must re-run on every assemble invocation so
    # the second pass picks up newly-discovered assets and resolves any
    # uploaded meshes that had no MeshIds yet. Note: upload_assets dedupes
    # by relative path against ctx.uploaded_assets — it does NOT detect
    # in-place content edits to the same file. Users who change a mesh or
    # texture in place must remove its entry from ctx.uploaded_assets (or
    # delete conversion_context.json) to force a re-upload. Each phase
    # self-gates on ``--no-upload`` / missing creds, so listing them here
    # is safe even when uploads should no-op.
    force_rerun = {"moderate_assets", "upload_assets", "resolve_assets"}
    try:
        pipeline.run_through("write_output", skip=skip, force_rerun=force_rerun)
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
    """Publish the .rbxlx to Roblox via headless place builder."""
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
    #   1. CLI flags                  (user override)
    #   2. shared roblox.id_cache      (.roblox_ids.json, with legacy fallback)
    #   3. Persisted ConversionContext (older snapshot)
    #
    # The shared cache wins over ctx because ``u2r publish`` updates the
    # cache after a successful retarget but does not always update ctx in
    # the same output dir. Reading ctx first would silently route a later
    # interactive ``upload`` to the previous experience.
    from roblox.id_cache import read_ids
    uid, pid = universe_id, place_id

    if not uid or not pid:
        cached_uid, cached_pid = read_ids(out)
        uid = uid or cached_uid
        pid = pid or cached_pid

    if not uid or not pid:
        try:
            prior_ctx = ConversionContext.load(ctx_path)
            uid = uid or prior_ctx.universe_id
            pid = pid or prior_ctx.place_id
        except Exception as exc:  # noqa: BLE001 — surfaced to user below if still missing
            log.debug("upload: could not read ctx for id fallback: %s", exc)

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

    # Re-run the pipeline through convert_scene so we have rbx_place in
    # memory for the place builder. Publish goes via execute_luau, so the
    # binary .rbxl is never read.
    pipeline = _make_pipeline(None, out, skip_binary_rbxl=True)
    pipeline.ctx.universe_id = uid
    pipeline.ctx.place_id = pid

    # Rebuild rbx_place via the pipeline. Cloud side-effect phases
    # (moderate_assets, upload_assets, resolve_assets) are skipped because
    # `upload` re-publishes existing state, not re-uploads. transpile_scripts
    # is skipped if already completed AND the on-disk script cache survived
    # so hand-edited Luau in <output>/scripts/ is preserved (write_output
    # rehydrates from disk). Without the cache check, an archived output
    # dir without scripts/ would publish a place with no scripts.
    skip: set[str] = {"moderate_assets", "upload_assets", "resolve_assets"}
    if (
        "transpile_scripts" in pipeline.ctx.completed_phases
        and scripts_cache_intact(out, pipeline.ctx.transpiled_scripts)
    ):
        skip.add("transpile_scripts")
    try:
        pipeline.run_through("write_output", skip=skip)
    except Exception as exc:
        _emit({"phase": "upload", "success": False,
               "errors": [f"Failed to rebuild scene state: {exc}"]})
        sys.exit(1)

    rbx_place = pipeline.state.rbx_place
    if rbx_place is None:
        _emit({"phase": "upload", "success": False,
               "errors": ["rbx_place is empty after rebuilding scene state."]})
        sys.exit(1)

    # Prefer the place file-upload path: it preserves CollisionFidelity and
    # other Plugin-gated MeshPart properties that the chunked execute_luau
    # builder cannot set. Open Cloud /universes/v1 only accepts binary
    # .rbxl (XML .rbxlx returns 400), so prefer that. Falls back to
    # publish_place only when no place file is on disk.
    from roblox.place_publisher import publish_place, publish_place_file

    rbxl_for_upload = out / "converted_place.rbxl"
    rbxlx_for_upload = out / "converted_place.rbxlx"
    file_for_upload = rbxl_for_upload if rbxl_for_upload.exists() else (
        rbxlx_for_upload if rbxlx_for_upload.exists() else None
    )
    if file_for_upload is not None:
        publish_result = publish_place_file(
            config.ROBLOX_API_KEY, uid, pid, file_for_upload,
        )
    else:
        publish_result = publish_place(
            config.ROBLOX_API_KEY, uid, pid, rbx_place, out,
        )

    pipeline.ctx.save(_context_path(out))

    if publish_result.exceeded_limit:
        _emit({
            "phase": "upload",
            "success": False,
            "errors": [publish_result.error],
            "script_path": str(publish_result.script_path),
        })
        sys.exit(1)

    if publish_result.success:
        # Only cache universe/place IDs after a successful publish — avoids
        # persisting bad IDs that would be silently reused next run.
        from roblox.id_cache import write_ids
        write_ids(out, uid, pid)

        # Self-heal: if the local converted_place.rbxlx was written before
        # universe/place IDs were available (legacy first-run path, when
        # users called assemble with no IDs and got a silent skip), the
        # MeshParts in it still carry raw Model IDs that Studio fails to
        # fetch. Now that the publish has confirmed the IDs work, re-run
        # resolve_assets + write_output so the local rbxlx matches what
        # was just published. This rewrites the file in place.
        uploaded_meshes = sum(
            1 for k in (pipeline.ctx.uploaded_assets or {})
            if any(k.lower().endswith(ext) for ext in ('.fbx', '.obj'))
        )
        resolved_meshes = len(pipeline.ctx.mesh_native_sizes or {})
        if uploaded_meshes and resolved_meshes < uploaded_meshes:
            log.info(
                "[upload] Self-heal: %d unresolved mesh(es) detected in "
                "local rbxlx — re-running resolve_assets + write_output "
                "so the file matches the published place.",
                uploaded_meshes - resolved_meshes,
            )
            try:
                heal = _make_pipeline(None, out, skip_binary_rbxl=True)
                heal.ctx.universe_id = uid
                heal.ctx.place_id = pid
                heal.run_through(
                    "write_output",
                    skip={"transpile_scripts", "moderate_assets",
                          "upload_assets"},
                    force_rerun={"resolve_assets", "convert_scene",
                                 "write_output"},
                )
            except Exception as exc:  # noqa: BLE001 — non-fatal
                log.warning(
                    "[upload] Self-heal failed: %s. The published place "
                    "is correct; re-run 'assemble' to fix the local "
                    "rbxlx.", exc,
                )

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
            "chunks": publish_result.chunks,
        }
        _save_skill_state(out, state)

    _emit({
        "phase": "upload",
        "success": publish_result.success,
        "universe_id": uid,
        "place_id": pid,
        "chunks": publish_result.chunks,
        "script_size_kb": round(publish_result.total_bytes / 1024, 1),
        "script_path": str(publish_result.script_path),
        "chunk_results": publish_result.chunk_results,
        "warning": (
            "Publishing a fresh rebuild of the scene, not the local .rbxlx. "
            "Any manual edits to converted_place.rbxlx or place_builder.luau "
            "are not reflected in the uploaded place."
        ),
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

    # Augment the structured report written by pipeline.write_output with
    # skill-only fields. Routed through report_generator.augment_report so
    # there's one reporting path — no parallel json.loads/update/dumps dance
    # that could drift from the pipeline's schema.
    from converter.report_generator import augment_report

    report_path = out / "conversion_report.json"
    report_data = augment_report(report_path, {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "unity_project_path": ctx.unity_project_path,
        "output_dir": str(out),
        "selected_scene": _relative_scene_path(ctx.selected_scene, ctx.unity_project_path),
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
    })

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
