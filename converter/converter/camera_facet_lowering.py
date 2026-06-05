"""camera_facet_lowering.py -- generic-allowlist camera-facet lowering pass.

A deterministic, structure-gated lowering on the generic scene-runtime
allowlist (called from ``contract_pipeline.transpile_with_contract``). It
routes a transpiled first-person controller's *look facet* to the
hand-written ``SceneCameraInput`` runtime service, instead of leaving the
AI's per-game camera math (which drops the player's yaw -- see
docs/design/camera-input-fidelity-plan.md) in the emitted output.

This is a *lowering pass*, NOT a coherence pack: it is deterministic, gated
on a structural fingerprint (never ``s.name`` / per-game identity), and the
hard composition logic lives in the vetted runtime service, not here. See
the "deterministic lowering layer" section of scene-runtime-contract.md.

Cut (method-scoped recognize-and-splice -- not whole-class replace):
  * Replace the *look method's body* (the method whose body both yaw-only-
    ``PivotTo``s a body AND rebuilds a pitch-only camera CFrame) with a
    lazy-acquire + ``self._cam:step(dt)``.
  * Replace the recoil write (``self.<pitch> = self.<pitch> - N`` outside the
    look method) with ``self._cam:applyRecoil(...)``.
  * Leave Awake's ``self.cam = workspace.CurrentCamera`` read alias, the Shoot
    raycast, Move, ammo, events untouched (the controller still reads the rig
    pivot the service yaws + ``self.cam.CFrame`` for raycasts).

Idempotent: after lowering, neither fingerprint matches, so re-runs no-op.
"""

from __future__ import annotations

import re
from typing import Collection, Protocol

from converter.runtime_contract import (
    _extract_function_body,
    _strip_strings_and_comments,
)


class _HasLuauSource(Protocol):
    luau_source: str


# A colon-form method header up to (and including) the OPEN paren:
# ``function Class:Method(``. Colon form only (the transpiler emits
# ``function Player:Rotate(dt)``); dot-form is left alone so ``self`` is always
# implicit in the splice. ``.end()`` lands just after ``(`` -- exactly the
# ``args_start`` ``_extract_function_body`` expects.
_METHOD_RE = re.compile(
    r"function\s+[A-Za-z_]\w*\s*:\s*[A-Za-z_]\w*\s*\(",
)

# Yaw-only body turn: ``<obj>:PivotTo(<obj>:GetPivot() * CFrame.Angles(0, ...``.
# The literal 0 pitch slot is the yaw-only fingerprint.
_YAW_TURN_RE = re.compile(
    r":PivotTo\(\s*[\w.]+:GetPivot\(\)\s*\*\s*CFrame\.Angles\(\s*0\s*,",
)

# Pitch-only camera rebuild: ``<cam>.CFrame = CFrame.new(<pos>) *
# CFrame.Angles(<pitch>, 0, 0)`` -- yaw and roll literal 0.
_CAM_PITCH_RE = re.compile(
    r"\.CFrame\s*=\s*CFrame\.new\([^()]*(?:\([^()]*\)[^()]*)*\)\s*\*\s*"
    r"CFrame\.Angles\(\s*[^,]+?\s*,\s*0\s*,\s*0\s*\)",
)

# Pitch field, captured from the clamp idiom inside the look body:
# ``self.<pitch> = math.clamp(self.<pitch>, ...``.
_PITCH_CLAMP_RE = re.compile(
    r"self\.(?P<pitch>\w+)\s*=\s*math\.clamp\(\s*self\.(?P=pitch)\b",
)
# Fallback: the pitch field inside the camera rebuild's Angles term.
_PITCH_IN_CAM_RE = re.compile(
    r"CFrame\.Angles\(\s*math\.rad\(\s*self\.(?P<pitch>\w+)",
)

# Broadened look-method signals (used ONLY on the upstream-identified player
# script, never globally -- a non-player camera keeps the strict shape below).
# The strict ``_CAM_PITCH_RE`` requires an EXACT ``CFrame.new(...) *
# CFrame.Angles(...)`` rebuild, which the AI defeats with an intervening yaw
# term (``CFrame.new(pos) * (basePivot - basePivot.Position) * CFrame.Angles
# (pitch)``). Instead, key on two INVARIANTS of a mouse-look method that are
# robust to CFrame-composition shape: it reads the mouse delta AND it owns the
# camera. Requiring BOTH (not GetMouseDelta alone) excludes a "consume the
# first-frame spike" helper that reads the delta but never writes the camera.
_MOUSE_DELTA_RE = re.compile(r"GetMouseDelta\(")
_CAM_OWNER_RE = re.compile(
    r"CurrentCamera|CameraType\s*=\s*Enum\.CameraType\.Scriptable",
)


def _find_look_method(stripped: str, broadened: bool = False):
    """Return ``(body_start, body_len, param, pitch_field)`` for the look
    method, or ``None``. Scans on the comment/string-stripped source so
    fingerprints inside literals are never signals; offsets map 1:1 to the
    real source (the strip preserves length).

    ``broadened`` (set ONLY for the upstream-identified player script) ALSO
    accepts a method that reads ``GetMouseDelta()`` and owns the camera, so an
    AI-emitted CFrame shape the strict pitch-rebuild regex misses is still
    located. Non-player scripts pass ``broadened=False`` and keep the exact
    strict shape (byte-identical behavior; no new false positives)."""
    for m in _METHOD_RE.finditer(stripped):
        body, body_start = _extract_function_body(stripped, m.end())
        if body is None:
            continue
        strict = bool(_YAW_TURN_RE.search(body) and _CAM_PITCH_RE.search(body))
        broad = bool(
            broadened
            and _MOUSE_DELTA_RE.search(body)
            and _CAM_OWNER_RE.search(body)
        )
        if not (strict or broad):
            continue
        # First identifier in the (already-passed) arg list, if any.
        close = stripped.find(")", m.end())
        param = None
        if close != -1:
            pm = re.match(r"\s*([A-Za-z_]\w*)", stripped[m.end():close])
            if pm:
                param = pm.group(1)
        pitch_m = _PITCH_CLAMP_RE.search(body) or _PITCH_IN_CAM_RE.search(body)
        pitch_field = pitch_m.group("pitch") if pitch_m else None
        return body_start, len(body), param, pitch_field
    return None


def _recoil_re(pitch_field: str) -> re.Pattern[str]:
    # ``self.<pitch> = self.<pitch> [+-] <number>`` -- the recoil write.
    return re.compile(
        r"self\.%s\s*=\s*self\.%s\s*(?P<sign>[+-])\s*(?P<num>[0-9]+(?:\.[0-9]+)?)"
        % (re.escape(pitch_field), re.escape(pitch_field)),
    )


def _step_body(param: str | None, follow_character: bool | None = None) -> str:
    arg = param or "0"
    # Tri-state ``followCharacter`` so the singleton's eye state is deterministic
    # and never leaks across camera controllers (the service is one camera per
    # client):
    #   True  -> the player controller: eye follows the Roblox character.
    #   False -> a non-player camera (drone/turret) IN A CONVERSION THAT ALSO HAS
    #            a player: emit the flag explicitly so a later configure clears
    #            any prior player ``true`` instead of inheriting it (the phase-
    #            review stickiness finding).
    #   None  -> no player identified in this conversion: omit the key entirely
    #            so the emit stays byte-identical to the pre-followCharacter pass.
    if follow_character is None:
        config = "{rig = self.gameObject}"
    elif follow_character:
        config = "{rig = self.gameObject, followCharacter = true}"
    else:
        config = "{rig = self.gameObject, followCharacter = false}"
    return (
        "\n"
        '\tif not self._cam then\n'
        '\t\tself._cam = require(game:GetService("ReplicatedStorage")'
        ':WaitForChild("SceneCameraInput")).acquire()\n'
        f"\t\tself._cam:configure({config})\n"
        "\tend\n"
        f"\tself._cam:step({arg})\n"
    )


def lower_camera_facet(
    scripts: list[_HasLuauSource],
    follow_character_paths: "Collection[_HasLuauSource] | None" = None,
) -> int:
    """Lower the look facet of any flattened first-person controller in
    ``scripts`` onto the SceneCameraInput service. Returns the number of
    scripts modified.

    ``follow_character_paths`` is the (identity) collection of scripts
    identified as the player controller (from
    ``movement_facet_lowering.find_player_controllers``). For a script in that
    set, the emitted ``configure`` carries ``followCharacter = true`` (eye
    follows the Roblox character); for every other script the emit is
    byte-identical to before."""
    follow_ids = (
        {id(s) for s in follow_character_paths} if follow_character_paths else set()
    )
    changed = 0
    for s in scripts:
        src = s.luau_source or ""
        stripped = _strip_strings_and_comments(src)
        # The player controller gets the broadened locator (its look method may
        # carry an AI CFrame shape the strict regex misses); every other script
        # keeps the strict shape so non-player cameras are byte-identical.
        found = _find_look_method(stripped, broadened=id(s) in follow_ids)
        if found is None:
            continue
        body_start, body_len, param, pitch_field = found

        # Tri-state: the player -> True; a non-player camera when a player
        # exists in this conversion -> False (explicit, so it can't inherit a
        # stale singleton ``true``); no player at all -> None (byte-identical
        # omit, so non-FPS conversions are unchanged).
        if id(s) in follow_ids:
            follow: bool | None = True
        elif follow_ids:
            follow = False
        else:
            follow = None
        new_src = src[:body_start] + _step_body(param, follow) + src[body_start + body_len:]

        # Recoil writes (outside the now-replaced look body) -> applyRecoil.
        if pitch_field:
            def _repl(mm: "re.Match[str]") -> str:
                # original ``- N`` lowers pitch -> applyRecoil(-rad(N));
                # ``+ N`` -> applyRecoil(rad(N)). Preserve the sign.
                mag = mm.group("num")
                delta = f"-math.rad({mag})" if mm.group("sign") == "-" else f"math.rad({mag})"
                return f"if self._cam then self._cam:applyRecoil({delta}) end"
            new_src = _recoil_re(pitch_field).sub(_repl, new_src)

        if new_src != src:
            s.luau_source = new_src
            changed += 1
    return changed
