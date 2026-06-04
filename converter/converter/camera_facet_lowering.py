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
from typing import Protocol

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


def _find_look_method(stripped: str):
    """Return ``(body_start, body_len, param, pitch_field)`` for the look
    method, or ``None``. Scans on the comment/string-stripped source so
    fingerprints inside literals are never signals; offsets map 1:1 to the
    real source (the strip preserves length)."""
    for m in _METHOD_RE.finditer(stripped):
        body, body_start = _extract_function_body(stripped, m.end())
        if body is None:
            continue
        if not (_YAW_TURN_RE.search(body) and _CAM_PITCH_RE.search(body)):
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


def _step_body(param: str | None) -> str:
    arg = param or "0"
    return (
        "\n"
        '\tif not self._cam then\n'
        '\t\tself._cam = require(game:GetService("ReplicatedStorage")'
        ':WaitForChild("SceneCameraInput")).acquire()\n'
        "\t\tself._cam:configure({rig = self.gameObject})\n"
        "\tend\n"
        f"\tself._cam:step({arg})\n"
    )


def lower_camera_facet(scripts: list[_HasLuauSource]) -> int:
    """Lower the look facet of any flattened first-person controller in
    ``scripts`` onto the SceneCameraInput service. Returns the number of
    scripts modified."""
    changed = 0
    for s in scripts:
        src = s.luau_source or ""
        stripped = _strip_strings_and_comments(src)
        found = _find_look_method(stripped)
        if found is None:
            continue
        body_start, body_len, param, pitch_field = found

        new_src = src[:body_start] + _step_body(param) + src[body_start + body_len:]

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
