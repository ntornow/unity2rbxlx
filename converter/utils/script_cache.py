"""
script_cache.py -- Shared check for the on-disk transpiled-scripts cache.

``Pipeline.write_output`` rehydrates Luau scripts from ``<output>/scripts/``
when ``transpile_scripts`` is skipped. If that directory is empty (output
dir was archived without ``scripts/`` or partially copied), the rehydrate
silently produces a place with no scripts. Callers that consider skipping
transpile must therefore verify the cache is intact first.

Used by ``u2r convert`` (publish-rebuild fallback), ``convert_interactive``
``assemble``, and ``convert_interactive`` ``upload``.
"""

from __future__ import annotations

from pathlib import Path


def count_top_level_scripts(output_dir: Path) -> int:
    """Count top-level ``scripts/*.luau`` files under ``output_dir``.

    This is the exact set the transpile cache is keyed on (subdirs like
    ``animations/`` are written by other phases). Returns 0 when ``scripts/``
    is absent. ``Pipeline`` records this post-prune so a later
    ``scripts_cache_intact`` compares like-for-like.
    """
    scripts = output_dir / "scripts"
    if not scripts.is_dir():
        return 0
    return sum(1 for f in scripts.glob("*.luau") if f.is_file())


def scripts_cache_intact(output_dir: Path, expected_count: int) -> bool:
    """True if the transpiled-script cache survived intact.

    Each transpiled C# script is emitted at the top level of ``scripts/``
    by ``convert_interactive transpile`` (and by the fresh-transpile branch
    of ``Pipeline.write_output``). Subdirectories (``animations/``,
    ``animation_data/``, ``packages/``, ``scriptable_objects/``) are
    written by other phases and have nothing to do with the gameplay
    transpilation.

    Counting ONLY top-level ``*.luau`` files (and comparing to the
    expected count from ``ConversionContext.transpiled_scripts``) catches
    partially-archived output dirs where only the subdirs survived: if
    the gameplay scripts are gone, retranspile rather than rehydrating
    a place with missing scripts.
    """
    if expected_count <= 0:
        return False
    return count_top_level_scripts(output_dir) >= expected_count
