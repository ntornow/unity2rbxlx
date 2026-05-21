"""
test_seam_injector.py -- Inject Luau test seams into emitted scripts.

Roblox's ``UserInputService`` is a locked Instance whose service-side method
lookups can't be replaced from Luau (``UIS.GetMouseDelta = ...`` looks like
it succeeds but the engine ignores it and goes straight to the native
implementation). And the Studio MCP plugin's synthesized mouse-motion
events carry ``Delta = (0, 0)`` — meaning offline Studio-driven tests have
no way to exercise the camera-yaw code path that polls
``UserInputService:GetMouseDelta()`` every frame.

This pass adds a one-line indirection: every call to
``UserInputService:GetMouseDelta()`` is rewritten to call a tiny
``_getMouseDelta()`` helper that checks ``_G._mockMouseDelta`` first
(one-shot, cleared after read) and falls back to the real call. When the
global is nil — i.e. in production — the helper is a single extra
table lookup and the function tail-calls into the real service method,
so the runtime cost is effectively zero.

Tests stuff a value into ``_G._mockMouseDelta`` and observe the camera
yaw change as if the user had moved the mouse:

    _G._mockMouseDelta = Vector2.new(200, 0)
    task.wait(0.05)  -- one heartbeat for the rotate() loop
    assert(camera.CFrame.LookVector ~= startLook)
"""

from __future__ import annotations

import logging
import re

from core.roblox_types import RbxScript

log = logging.getLogger(__name__)

_HELPER_MARKER = "_getMouseDelta"

# The injected helper. UserInputService is referenced by name on the
# assumption that the script already declared ``local UserInputService = ...``
# above. The injector verifies that before splicing — see _inject_into_source.
_HELPER_BLOCK = """\
-- u2r: test seam for offline mouse-look tests. Production behavior
-- unchanged when ``_G._mockMouseDelta`` is nil. See
-- converter/test_seam_injector.py for the rationale.
local function _getMouseDelta()
\tlocal _md = rawget(_G, "_mockMouseDelta")
\tif _md ~= nil then
\t\t_G._mockMouseDelta = nil
\t\treturn _md
\tend
\treturn UserInputService:GetMouseDelta()
end
"""

# Matches both ``UserInputService:GetMouseDelta()`` and a leading
# ``game:GetService("UserInputService"):GetMouseDelta()`` form — the
# latter shows up occasionally in transpiled scripts that inline the
# service lookup.
_CALL_RX = re.compile(
    r"\b(?:UserInputService"
    r"|game:GetService\(\s*['\"]UserInputService['\"]\s*\))"
    r":GetMouseDelta\s*\(\s*\)"
)

_UIS_LOCAL_RX = re.compile(
    r"^\s*local\s+UserInputService\s*=\s*game:GetService\(\s*['\"]"
    r"UserInputService['\"]\s*\)\s*$",
    re.MULTILINE,
)


def _inject_into_source(source: str) -> str | None:
    """Rewrite a single script's source, or return None if no change needed.

    Returns the new source when the script calls ``GetMouseDelta`` and
    hasn't already been instrumented. Returns ``None`` when nothing
    needs to change so callers can cheaply skip the disk write.
    """
    if _CALL_RX.search(source) is None:
        return None
    if _HELPER_MARKER in source:
        # Idempotent: a re-run of write_output (e.g. resume from a phase
        # after script generation) should not stack a second helper.
        return None

    # Rewrite the user's call sites FIRST, then prepend the helper.
    # Doing it in this order leaves the helper's own
    # ``return UserInputService:GetMouseDelta()`` intact — substituting
    # over the already-spliced source would rewrite that line too and
    # create infinite recursion when ``_G._mockMouseDelta`` is nil.
    rewritten = _CALL_RX.sub("_getMouseDelta()", source)

    # Splice the helper immediately after the script's
    # ``local UserInputService = ...`` line if there is one — keeps it
    # close to the related code and ensures the helper's reference to
    # UserInputService resolves to that local. Otherwise inject at the
    # top with its own service grab.
    m = _UIS_LOCAL_RX.search(rewritten)
    if m:
        insert_at = m.end()
        return rewritten[:insert_at] + "\n\n" + _HELPER_BLOCK + rewritten[insert_at:]

    prelude = (
        'local UserInputService = game:GetService("UserInputService")\n\n'
        + _HELPER_BLOCK + "\n"
    )
    return prelude + rewritten


def inject_test_seams(scripts: list[RbxScript]) -> int:
    """Inject the mouse-delta test seam into every relevant script.

    Returns the number of scripts mutated. A return value of 0 is normal
    for projects that don't poll the mouse (most of them).
    """
    changed = 0
    for s in scripts:
        if not getattr(s, "source", None):
            continue
        new_source = _inject_into_source(s.source)
        if new_source is not None:
            s.source = new_source
            changed += 1
    return changed
