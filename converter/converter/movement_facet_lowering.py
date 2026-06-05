"""movement_facet_lowering.py -- generic-allowlist player movement lowering.

A deterministic, structure-gated lowering on the generic scene-runtime
allowlist (called from ``contract_pipeline.transpile_with_contract``, AFTER
identifying the player but BEFORE ``lower_camera_facet`` erases the camera
fingerprint). It retargets the converted player controller's WASD movement
from the vestigial scene rig Part (``self.gameObject:PivotTo(...)``) onto the
Roblox character's ``Humanoid:Move(...)``, so Roblox physics owns
gravity/collision/floor (required by the ``FloorMaterial`` spawn oracle and to
kill the unbounded sink of the collision-less rig Part).

This is a *lowering pass*, NOT a coherence pack: it is deterministic, gated on
structure (never ``s.name`` / per-game identity), biased to PRECISION over
coverage (the retarget is destructive, so it abstains when the positive
evidence is weak), and the canonical movement body is fixed here, not in the AI
prompt. See docs/design/camera-input-fidelity-plan.md and the generic
player-binding design.

Player IDENTITY is NOT derived here: ``find_player_controllers`` consumes the
deterministic UPSTREAM Unity signal -- the planner's per-module
``has_character_controller`` flag (a script co-located with a Unity
``CharacterController`` component, the engine-level "this is the avatar"
signal). This replaces the former 3-signal fingerprint of the *transpiled
output*, which abstained silently whenever the AI emitted a valid-but-different
shape (helper-wrapped WASD, an extra yaw term in the camera CFrame), silently
decoupling camera/movement/character. The upstream signal is robust to AI
transpile-shape variance. Fail-closed: 0 or >1 distinct CC-scripts => [].

Because identity is upstream, the WASD method LOCATOR below runs only on the
known-player script, so it can be broad (catch helper-wrapped reads) without
risking a cross-script false positive: a colon-method reading >=3 distinct WASD
key codes AND carrying a locomotion side-effect.

Idempotent: the lowered body calls ``getYawBasis():VectorToWorldSpace`` +
``Humanoid:Move``, so a re-run detects the marker and skips.
"""

from __future__ import annotations

import re
from pathlib import Path

from converter.camera_facet_lowering import _HasLuauSource, _METHOD_RE
from converter.runtime_contract import (
    _extract_function_body,
    _strip_strings_and_comments,
)

# A WASD key reference: ``Enum.KeyCode.W|A|S|D``. Counted body-wide on the
# lexer-blanked source; >=3 *distinct* letters is the move-input signal. Unlike
# the retired identity matcher, this does NOT require an inline ``IsKeyDown(``
# wrapper -- the AI frequently factors the reads through a helper
# (``self:_axis(Enum.KeyCode.D, Enum.KeyCode.A)``), where the key codes still
# appear as literal args. Since this runs only on the upstream-identified
# player script, the broader match cannot mis-fire on a non-player script.
_WASD_RE = re.compile(
    r"Enum\.KeyCode\.(?P<key>[WASD])\b",
)

# A locomotion side-effect: the move method DOES something with movement
# (drives a Humanoid / jumps / pivots the rig / reads ground state). Required
# alongside the WASD reads so a pure-input helper (``ReadMoveInput()`` that only
# reads keys and returns a vector) cannot steal the match from the method that
# actually moves the character (codex adversarial finding).
_LOCOMOTION_RE = re.compile(
    r":Move\(|\.Jump\b|:PivotTo\(|\.FloorMaterial\b",
)


def _wasd_method_bodies(stripped: str) -> list[tuple[int, int, str | None]]:
    """Return ``(body_start, body_len, param)`` for EVERY colon-method whose
    body reads >=3 distinct WASD key codes. Scans the comment/string-stripped
    source so reads inside literals never count; offsets map 1:1 to the real
    source (the strip is length-preserving). Returning ALL such methods (not
    just the first) lets the caller fail closed when a script has more than one
    -- an ambiguous shape we refuse to guess at (D5 abstain-on-ambiguity)."""
    out: list[tuple[int, int, str | None]] = []
    for m in _METHOD_RE.finditer(stripped):
        body, body_start = _extract_function_body(stripped, m.end())
        if body is None:
            continue
        keys = {mm.group("key") for mm in _WASD_RE.finditer(body)}
        if len(keys) < 3:
            continue
        # Require a locomotion side-effect so a pure-input helper (reads WASD,
        # returns a vector, no movement) can't be mistaken for the move method.
        if not _LOCOMOTION_RE.search(body):
            continue
        # First identifier in the (already-passed) arg list, if any.
        close = stripped.find(")", m.end())
        param = None
        if close != -1:
            pm = re.match(r"\s*([A-Za-z_]\w*)", stripped[m.end():close])
            if pm:
                param = pm.group(1)
        out.append((body_start, len(body), param))
    return out


def _wasd_method_body(stripped: str):
    """Return the SINGLE WASD method ``(body_start, body_len, param)``, or
    ``None`` if zero OR more than one colon-method reads >=3 distinct WASD
    keys. More-than-one is fail-closed: a script with two move methods is
    ambiguous, so we lower neither (and, upstream, it is not a player)."""
    bodies = _wasd_method_bodies(stripped)
    if len(bodies) != 1:
        return None
    return bodies[0]


def _script_stem(s: _HasLuauSource) -> str:
    """The .cs file stem for a transpiled script -- the key that joins it to a
    planner ``SceneRuntimeModule`` (which is keyed by GUID but carries
    ``stem``). Prefer ``source_path`` (the canonical .cs path); fall back to
    ``output_filename`` (``<stem>.luau``)."""
    sp = getattr(s, "source_path", "") or ""
    if sp:
        return Path(sp).stem
    of = getattr(s, "output_filename", "") or ""
    return of[:-5] if of.endswith(".luau") else of


def find_player_controllers(
    scripts: list[_HasLuauSource],
    modules: "dict[str, dict] | None" = None,
) -> list[_HasLuauSource]:
    """Return the UNIQUE player-controller script identified by the
    deterministic UPSTREAM Unity signal -- the planner module flagged
    ``has_character_controller`` (a script co-located with a Unity
    ``CharacterController`` on a placed GameObject) -- mapped to its transpiled
    script by file stem. Fail-closed (``[]``) when ZERO or MORE THAN ONE
    distinct script carries the signal (a split-/multi-controller game abstains
    rather than guess), or when the single flagged module can't be matched to
    exactly one transpiled script.

    Identity is NO LONGER a fingerprint of the transpiled output: that abstained
    silently the moment the AI emitted a valid-but-different shape (the systemic
    failure this fix closes). ``modules`` is ``scene_runtime["modules"]``;
    callers without it (legacy unit harnesses) get ``[]``."""
    if not modules:
        return []
    cc_modules = [
        m for m in modules.values()
        if isinstance(m, dict) and m.get("has_character_controller")
    ]
    # Unique AND exclusive: exactly one distinct CC-bearing script is the
    # player. 0 (non-FPS) or >1 (ambiguous) => fail closed.
    if len(cc_modules) != 1:
        return []
    stem = cc_modules[0].get("stem") or ""
    if not stem:
        return []
    matches = [s for s in scripts if _script_stem(s) == stem]
    if len(matches) != 1:
        return []
    return matches


def _move_body(param: str | None) -> str:
    """The canonical character-Humanoid move body (whole-method-body replace).
    Lazy-acquires ``_cam`` with ``followCharacter = true`` so the eye follows
    the character regardless of method order vs ``Rotate`` on frame 1; reads
    the service's yaw basis; drives the LocalPlayer.Character Humanoid."""
    arg = param or "dt"
    return (
        "\n"
        '\tlocal UIS = game:GetService("UserInputService")\n'
        "\tif not self._cam then\n"
        '\t\tself._cam = require(game:GetService("ReplicatedStorage")'
        ':WaitForChild("SceneCameraInput")).acquire()\n'
        "\t\tself._cam:configure({rig = self.gameObject, followCharacter = true})\n"
        "\tend\n"
        '\tlocal lp = game:GetService("Players").LocalPlayer\n'
        "\tlocal char = lp and lp.Character\n"
        '\tlocal hum = char and char:FindFirstChildOfClass("Humanoid")\n'
        "\tif not hum then return end\n"
        "\tlocal h = 0\n"
        "\tif UIS:IsKeyDown(Enum.KeyCode.D) then h += 1 end\n"
        "\tif UIS:IsKeyDown(Enum.KeyCode.A) then h -= 1 end\n"
        "\tlocal v = 0\n"
        "\tif UIS:IsKeyDown(Enum.KeyCode.W) then v += 1 end\n"
        "\tif UIS:IsKeyDown(Enum.KeyCode.S) then v -= 1 end\n"
        "\tlocal dir = self._cam:getYawBasis():VectorToWorldSpace("
        "Vector3.new(h, 0, -v))\n"
        "\tif dir.Magnitude > 0 then hum:Move(dir.Unit, false) "
        "else hum:Move(Vector3.zero, false) end\n"
        "\tif UIS:IsKeyDown(Enum.KeyCode.Space) then hum.Jump = true end\n"
    )


def lower_movement_facet(players: list[_HasLuauSource]) -> int:
    """Whole-body-replace each player's WASD method with the canonical
    character-Humanoid move body. Returns the number of scripts modified.

    Idempotency is **method-scoped**, NOT file-global: we locate the WASD
    method first, then skip the rewrite only if THAT method's body already
    carries both lowered markers (``getYawBasis():VectorToWorldSpace`` +
    ``:Move(``). A file-global scan would let an unrelated ``:Move(`` (e.g. on
    some other instance) suppress a needed first lowering -- a false skip the
    method-scoped check cannot make.

    Fail-closed on multiple WASD methods: ``_wasd_method_body`` returns
    ``None`` when a script has >1 colon-method reading >=3 distinct WASD keys,
    so that script is left untouched (the same ambiguity gate
    ``find_player_controllers`` already applies)."""
    changed = 0
    for s in players:
        src = s.luau_source or ""
        stripped = _strip_strings_and_comments(src)
        found = _wasd_method_body(stripped)
        if found is None:
            continue
        body_start, body_len, param = found
        # Method-scoped idempotency: only skip if the WASD method's OWN body
        # already carries both lowered markers.
        method_body = src[body_start:body_start + body_len]
        if (
            "getYawBasis():VectorToWorldSpace" in method_body
            and ":Move(" in method_body
        ):
            continue
        new_src = src[:body_start] + _move_body(param) + src[body_start + body_len:]
        if new_src != src:
            s.luau_source = new_src
            changed += 1
    return changed
