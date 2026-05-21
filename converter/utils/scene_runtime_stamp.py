"""scene_runtime_stamp.py -- ``.scene-runtime-mode`` output-dir sidecar.

Part of PR3b. A generic-mode run produces output that differs from
legacy on three persisted surfaces (``storage_plan``, ``.luau`` bodies,
``conversion_plan.json``) — per-surface isolation between modes is
whack-a-mole. The contract pins ``--scene-runtime=legacy|auto|generic``
at the run's identity, recorded in a single sidecar at the output
root: ``<output>/.scene-runtime-mode`` (plain text, one line).

Why NOT under ``scripts/``: ``Pipeline.write_output`` rehydrates Luau
scripts by globbing ``scripts/**/*.luau``; the stamp must live where
that glob can't accidentally pick it up.

The contract:

  - **Write** the stamp once per conversion run, at the front door,
    BEFORE the first phase. ``--clean`` is its own surface — see
    ``apply_clean_directive``.
  - **Check** the stamp at every conversion front door (``u2r convert``,
    ``u2r publish``, ``u2r eval``, ``convert_interactive assemble``,
    ``convert_interactive upload``) **before** ``scripts_cache_intact``
    decides whether to skip transpile. A mismatch refuses to proceed
    incrementally — the operator must either rerun with the original
    mode or pass ``--clean`` to wipe and rebuild.

Stamp file format: a single line, no newline, one of
``"legacy"`` / ``"auto"`` / ``"generic"``. Absent file is treated as
the legacy mode (forward-compat with pre-PR3b output directories).
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)


SceneRuntimeMode = Literal["legacy", "auto", "generic"]


STAMP_BASENAME: str = ".scene-runtime-mode"


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def read_scene_runtime_stamp(output_dir: Path) -> SceneRuntimeMode:
    """Return the persisted mode for ``output_dir`` (``"legacy"`` if
    absent or unreadable — pre-PR3b output dirs predate the stamp and
    are therefore considered legacy).

    Never raises: a malformed stamp falls back to ``"legacy"`` with a
    warning. The mismatch guard catches the resulting disagreement when
    a non-legacy mode is requested.
    """
    stamp = output_dir / STAMP_BASENAME
    if not stamp.is_file():
        return "legacy"
    try:
        raw = stamp.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning(
            "[scene_runtime_stamp] %s unreadable (%s); treating as legacy",
            stamp, exc,
        )
        return "legacy"
    if raw in ("legacy", "auto", "generic"):
        # mypy/pyright can't narrow the Literal across a `in` check on a
        # tuple literal of strings; the explicit branches do.
        if raw == "legacy":
            return "legacy"
        if raw == "auto":
            return "auto"
        return "generic"
    log.warning(
        "[scene_runtime_stamp] %s contains unrecognized value %r; "
        "treating as legacy",
        stamp, raw,
    )
    return "legacy"


def write_scene_runtime_stamp(
    output_dir: Path, mode: SceneRuntimeMode,
) -> None:
    """Persist ``mode`` to the output directory's stamp file.

    Creates ``output_dir`` if missing. A repeat write with the same
    mode is a no-op for callers' purposes — the bytes are stable.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / STAMP_BASENAME).write_text(mode, encoding="utf-8")
    log.debug("[scene_runtime_stamp] stamped %s = %s", output_dir, mode)


# ---------------------------------------------------------------------------
# Mismatch guard
# ---------------------------------------------------------------------------

class SceneRuntimeModeMismatch(RuntimeError):
    """Raised at front doors when the requested mode disagrees with the
    output directory's stamp. Carries the offending paths + modes so
    callers can render a helpful CLI error without re-reading the
    stamp.
    """

    def __init__(
        self,
        output_dir: Path,
        requested: SceneRuntimeMode,
        stamped: SceneRuntimeMode,
    ) -> None:
        super().__init__(
            f"scene-runtime mode mismatch at {output_dir}: stamped "
            f"{stamped!r}, requested {requested!r}. Pass --clean to "
            f"wipe and rebuild, or re-run with --scene-runtime={stamped}."
        )
        self.output_dir = output_dir
        self.requested = requested
        self.stamped = stamped


def check_scene_runtime_mode_match(
    output_dir: Path, requested: SceneRuntimeMode,
) -> SceneRuntimeMode:
    """Return the stamp on ``output_dir`` if it equals ``requested``;
    raise ``SceneRuntimeModeMismatch`` otherwise.

    Convenience for front-door call sites that want a single guarded
    read. An absent stamp is interpreted as legacy — incremental
    generic builds against a legacy output dir refuse to proceed.
    """
    stamped = read_scene_runtime_stamp(output_dir)
    if stamped != requested:
        raise SceneRuntimeModeMismatch(output_dir, requested, stamped)
    return stamped


# ---------------------------------------------------------------------------
# --clean
# ---------------------------------------------------------------------------

def apply_clean_directive(
    output_dir: Path, requested: SceneRuntimeMode,
) -> None:
    """Wipe ``output_dir`` if it exists and rewrite the mode stamp.

    Called when the operator passes ``--clean`` at any front door. The
    name is intentional: this is the **only** path that destroys
    existing output, and it requires explicit opt-in. Subdirectories
    are removed; the stamp is then written so the subsequent fresh
    transpile can proceed without re-tripping the mismatch guard.

    ``output_dir.parent`` is not touched. The directory is recreated
    empty.
    """
    if output_dir.exists():
        if not output_dir.is_dir():
            raise NotADirectoryError(
                f"--clean target is not a directory: {output_dir}"
            )
        for child in output_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    write_scene_runtime_stamp(output_dir, requested)
    log.info(
        "[scene_runtime_stamp] --clean wiped %s and re-stamped as %s",
        output_dir, requested,
    )


def guard_or_clean_output_dir(
    output_dir: Path,
    requested: SceneRuntimeMode,
    clean: bool = False,
) -> None:
    """Front-door entry point. Call once at the top of every conversion
    command BEFORE ``scripts_cache_intact()`` decides whether to skip
    transpile.

    Decision table:

      - ``clean=True``                → wipe ``output_dir`` + restamp
                                         as ``requested``.
      - dir absent                    → create + stamp ``requested``
                                         (fresh run, idempotent).
      - dir present, stamp absent     → if ``requested == "legacy"``,
                                         upgrade by writing the stamp;
                                         otherwise raise (pre-PR3b
                                         dirs are legacy by definition).
      - dir present, stamp matches    → no-op.
      - dir present, stamp mismatches → raise ``SceneRuntimeModeMismatch``.

    Caller should let the raised mismatch bubble up to the click handler;
    the message is operator-readable and points at ``--clean``.
    """
    if clean:
        apply_clean_directive(output_dir, requested)
        return
    if not output_dir.exists():
        write_scene_runtime_stamp(output_dir, requested)
        return
    stamp_file = output_dir / STAMP_BASENAME
    if not stamp_file.is_file():
        if requested == "legacy":
            write_scene_runtime_stamp(output_dir, "legacy")
            return
        raise SceneRuntimeModeMismatch(output_dir, requested, "legacy")
    stamped = read_scene_runtime_stamp(output_dir)
    if stamped == requested:
        return
    raise SceneRuntimeModeMismatch(output_dir, requested, stamped)


__all__ = (
    "SceneRuntimeMode",
    "SceneRuntimeModeMismatch",
    "STAMP_BASENAME",
    "apply_clean_directive",
    "check_scene_runtime_mode_match",
    "guard_or_clean_output_dir",
    "read_scene_runtime_stamp",
    "write_scene_runtime_stamp",
)
