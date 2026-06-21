"""slider_fill_lowering.py -- deterministic GENERIC-mode slider-fill resolution.

Generic scene-runtime mode skips the legacy coherence packs
(``pipeline._subphase_cohere_scripts`` early-returns), so the legacy
``slider_fill_path_resize`` pack never fires on a generic conversion. The
Unity ``Slider.value`` resize is converted with a GUESSED fill child name
(``healthFrame:FindFirstChild("Fill")``) because the AI cannot know the real
fill child, so the health bar stays frozen in generic mode.

This pass ports the legacy pack's INLINE-resolution rewrite onto
``TranspiledScript.luau_source`` inside ``contract_pipeline.transpile_with_contract``
(the generic-mode equivalent of coherence packs), reusing the ONE string-level
implementation in ``slider_fill_common`` that the legacy pack also uses:

    local healthFill = healthFrame:FindFirstChild("Fill")
        ->  local healthFill = _resolveSliderFill(healthFrame)
            (+ a one-shot ``_resolveSliderFill`` helper injected per modified script)

``_resolveSliderFill`` reads the ``SliderFillElement`` attribute the UI translator
already stamps on the slider Frame (``ui_translator._apply_slider_properties``) --
a relative ``/``-path to the real fill child -- the DETERMINISTIC upstream signal,
NEVER an AI-output fingerprint or a per-game string. The existing
``if healthFill then ... .Size = ... end`` resize is left verbatim; a ``nil`` from
the helper makes that guard skip the resize (fail-soft-but-observable, no crash).

Scaffold mirrors ``trigger_enter_lowering`` / ``camera_mount_equip_lowering``:
operate on ``luau_source``, guard each rewrite with ``_luau_syntax_ok`` (revert on
a parse failure), idempotent (the rewritten RHS holds no guessed literal; the
helper is gated on its ``local function _resolveSliderFill`` definition token), and
a fail-loud coverage ``log.warning`` for any script whose guessed-fill resolution
survived the pass (shape drifted -> the bar would stay frozen, observable not
silent).

Pure: mutates only the ``luau_source`` of the scripts it is handed (the documented
lowering side effect, like the sibling ``lower_*`` passes).
"""

from __future__ import annotations

import logging
from typing import Protocol

from converter.rifle_rig_retarget_lowering import _luau_syntax_ok
from converter.slider_fill_common import (
    has_inline_guessed_fill,
    rewrite_inline_guessed_fill,
)

log = logging.getLogger(__name__)


class _HasLuauSourceAndName(Protocol):
    luau_source: str
    output_filename: str


def lower_slider_fill(scripts: list[_HasLuauSourceAndName]) -> int:
    """Rewrite every inlined guessed-fill resolution
    (``<x> = <frame>:FindFirstChild("Fill"|"Bar"|"Foreground")``) on each script's
    ``luau_source`` to ``<x> = _resolveSliderFill(<frame>)`` and inject the
    ``_resolveSliderFill`` helper once per modified script. Returns the number of
    scripts modified.

    Reuses ``slider_fill_common.rewrite_inline_guessed_fill`` (which also injects
    the helper) -- the SAME string-level implementation the legacy pack uses, so
    the two modes never diverge. Idempotent: the rewritten RHS holds no guessed
    literal and the helper is gated on its definition token, so a second pass makes
    no edits. A rewrite that produces unparseable Luau is reverted (the original
    ``luau_source`` is restored) so a bad edit never ships.

    Fail-loud coverage: any script that STILL has an inlined guessed-fill
    resolution after the rewrite (the AI reshaped the resolution enough that the
    structural gate abstained) is ``log.warning``-flagged so the silent abstain is
    observable rather than shipping a frozen bar."""
    changed = 0
    for s in scripts:
        original = s.luau_source or ""
        new_src, count = rewrite_inline_guessed_fill(original)
        if count and new_src != original:
            if _luau_syntax_ok(new_src):
                s.luau_source = new_src
                changed += 1
            else:
                # The rewrite produced unparseable Luau -> revert to the pre-edit
                # source so a bad edit never ships. The coverage guard below then
                # flags the surviving guessed-fill resolution loudly.
                log.warning(
                    "[slider_fill] '%s' slider-fill rewrite produced unparseable "
                    "Luau; reverted (bar stays frozen). Inspect the emitted fill "
                    "resolution.", s.output_filename,
                )
        # Fail-loud coverage on the FINAL committed source: a surviving inlined
        # guessed-fill resolution means the structural gate abstained (shape
        # drifted) -- warn so the freeze is observable, not silent.
        if has_inline_guessed_fill(s.luau_source or ""):
            log.warning(
                "[slider_fill] '%s' has an inlined guessed-fill resolution "
                "(<x> = <frame>:FindFirstChild(\"Fill\")) the lowering did not "
                "rewrite (shape drifted); slider bar will stay frozen. Inspect "
                "the emitted fill resolution.", s.output_filename,
            )
    return changed
