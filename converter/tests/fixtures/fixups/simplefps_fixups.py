"""Step 4c (Reactive fixups) for SimpleFPS.

Called by ``test_simplefps_assembly_with_cached_ids`` AFTER
``Pipeline.run_all()`` to codify the project-specific post-transpile
patches that /convert-unity's Step 4c would otherwise apply by hand.

See ./README.md for the rationale and refresh procedure.

Currently a no-op — populate as e2e fixtures expose gaps the
deterministic pipeline doesn't close. Candidates from the
2026-05-24 cold e2e run:

  - mouse-input channel for generic mode (mouse_yaw / mouse_pitch
    fixtures fail because the converted camera controller doesn't
    consume the E2E mouse delta attributes). Pending Task #17 from
    the prior session checkpoint.

  - viewmodel attachment to the camera (rifle_visible_in_viewport).

  - turret/mine touch wiring under generic mode (mine_damages_player,
    turrets_target_and_damage_player).

These are pending design work, not drop-in patches — record them
here as TODOs until the fix shape is decided.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.conversion_context import ConversionContext


def apply(output_dir: Path, ctx: "ConversionContext") -> None:
    """Apply SimpleFPS-specific reactive fixups. No-op today.

    When populating: each patch should be a small, surgical file edit
    that documents WHY it's needed (link back to the e2e fixture or
    the design doc that demanded it).
    """
    return None
