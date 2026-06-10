"""verify_hook.py -- the single post-conversion verification entry point.

Slice 1.6 of the Phase-1 redesign net. After a conversion writes its
``.rbxlx``, ``u2r.py convert`` and ``convert_interactive.py assemble`` both
route through this module to run the NON-interactive ``smoke_test.run_smoke_test``
(boot + coarse WASD/mouse health-check) on the produced place, and fold its
BOOT/HEALTH verdict into the convert exit status.

This is NOT the interactive ``/e2e-test`` path (the rich ``*.behavior.json``
fixtures driven by a Claude-Code Studio MCP conversation). ``--verify`` here
means ONLY the headless ``run_smoke_test`` hook.

Auto-resolution rule (``_should_verify``): verification runs on AUTO only where
the platform is macOS AND a Studio binary resolves at ``config.STUDIO_PATH``.
Off otherwise (headless Linux CI, the plain CLI path). The platform/Studio
check gates BEFORE ``run_smoke_test`` is called, so non-macOS auto-mode never
calls it and gets a spurious ``status="error"``.

Player-bind acceptance gate: paradigm C binds the player on the deterministic
upstream ``_HasCharacterController`` signal, so the player-bind fields
(``wasd_works`` / ``mouse_moves_view``) are FATAL — an unbound player fails the
hook. Flipped in Phase 5 (Step-1b) alongside ``REQUIRE_PLAYER_BIND=1`` in the
workflow, after a fresh cold-Studio conversion proved camera+WASD+jump+shoot+
respawn all C-owned with paradigm A deleted.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

import config

# Player-bind acceptance gate — FLIPPED True in Phase 5 (Step-1b): paradigm C
# binds the player (camera/WASD/jump/shoot/respawn) on the deterministic upstream
# _HasCharacterController signal, verified C-owned on a fresh cold-Studio
# conversion with paradigm A deleted. An unbound player now fails the hook.
REQUIRE_PLAYER_BIND: bool = True


def studio_available() -> bool:
    """True when the host can run a non-interactive Studio smoke test.

    Requires macOS (``smoke_test.run_smoke_test`` returns ``status="error"``
    off-Darwin) AND a resolvable Studio binary at ``config.STUDIO_PATH`` — the
    SAME binary ``smoke_test.run_smoke_test`` launches (``smoke_test.STUDIO_BINARY``
    aliases ``config.STUDIO_PATH``, the single source of truth). So an overridden
    ``ROBLOX_STUDIO_PATH`` can never make availability disagree with the launch.

    The ``U2R_DISABLE_AUTO_VERIFY`` env var forces this False (tests set it so
    the slice-1.6 auto-verify never launches Studio, including in subprocess CLI
    tests — env vars inherit into child processes). It is a test-only override;
    production never sets it.
    """
    if os.environ.get("U2R_DISABLE_AUTO_VERIFY") == "1":
        return False
    if platform.system() != "Darwin":
        return False
    studio_path = Path(config.STUDIO_PATH)
    return studio_path.exists()


def _should_verify(
    output_dir: Path,
    *,
    verify: bool | None,
) -> bool:
    """Resolve whether the post-conversion smoke verify should run.

    ``verify`` is the tri-state ``--verify/--no-verify`` value:
      * ``True``  — explicit ``--verify``: a hard request (caller fails fast
        when no Studio resolves; this returns True regardless).
      * ``False`` — explicit ``--no-verify``: always off.
      * ``None``  — AUTO (the default): on only where ``studio_available()``.

    ``output_dir`` is accepted for signature stability with the call sites and
    future per-output policy; the auto rule keys off the platform + Studio
    binary, not the output contents.
    """
    if verify is False:
        return False
    if verify is True:
        return True
    return studio_available()


def resolve_verify_target(
    output_dir: Path,
    *,
    verify_scene: str | None,
) -> Path | None:
    """Pick the single ``.rbxlx`` the smoke test should boot.

    ``run_smoke_test`` takes ONE rbxlx path, but ``--scene all`` writes one
    ``.rbxlx`` per scene. Resolution order:
      1. ``--verify-scene <name>`` override (``<name>`` or ``<name>.rbxlx``).
      2. ``main.rbxlx`` when present (the primary gameplay scene; the CI gate
         targets this explicitly).
      3. the single produced ``.rbxlx`` for a single-scene conversion.

    Returns ``None`` when no usable target exists (caller reports it).
    """
    output_dir = Path(output_dir)

    if verify_scene:
        name = verify_scene
        if not name.lower().endswith(".rbxlx"):
            name = f"{name}.rbxlx"
        candidate = output_dir / name
        return candidate if candidate.exists() else None

    main_rbxlx = output_dir / "main.rbxlx"
    if main_rbxlx.exists():
        return main_rbxlx

    produced = sorted(output_dir.glob("*.rbxlx"))
    if len(produced) == 1:
        return produced[0]
    # Multi-scene with no main.rbxlx, or none produced: caller must disambiguate
    # via --verify-scene.
    return None


def boot_health_failed(status: str) -> bool:
    """Map a ``run_smoke_test`` status to a fatal boot/health verdict.

    Only the BOOT/HEALTH axis gates the convert exit status. The status set is
    ``{pass, fail, error, timeout, unknown}`` (smoke_test.py). Anything other
    than ``pass`` is a boot/health failure.
    """
    return status != "pass"
