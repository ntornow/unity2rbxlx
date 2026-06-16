"""trigger_enter_lowering.py -- deterministic OnTriggerEnter/Exit edge re-bind.

The generic transpiler collapses Unity ``OnTriggerEnter``/``OnTriggerExit`` AND
the ``OnCollisionEnter``/``OnCollisionExit`` variants ALL onto the same
``.Touched``/``.TouchEnded`` EDGE signal:

    -- OnTriggerEnter(other): ...
    self.host:connectGameObjectSignal(self.gameObject, "Touched", function(other)
        ...
    end)

That is faithful for OnCollision* (which fire on the physical BODY -- the part a
projectile actually hits, the fix PR #198 preserves), but WRONG for the trigger
phases. Unity ``OnTriggerEnter``/``OnTriggerExit`` fire on the object's *trigger
collider* -- a proximity volume the converter stamps ``_IsTriggerVolume=true``
on (scene_converter.py, the isTrigger branch). ``connectGameObjectSignal``
resolves via ``getTouchPart(go)`` (preferTriggerVolume=false), which for a
BasePart-root object (a mine) returns the small visible BODY, so the callback
binds to the ~3-stud mesh instead of the marked ~7-stud trigger sphere. A mine's
1s fuse then only arms once the player is ON the body; at walk speed they leave
the blast radius before detonation (explodes, no damage).

This pass rewrites the *specific* ``connectGameObjectSignal(...)`` binding whose
immediately-preceding origin comment is ``-- OnTriggerEnter`` or
``-- OnTriggerExit`` into the trigger-preferring host method
``connectGameObjectTriggerSignal(...)`` (a pure METHOD-HEAD RENAME -- every
argument, including the ``"Touched"``/``"TouchEnded"`` signal name, is preserved
verbatim). That method resolves via ``getTouchPart(go, true)`` so a marked
trigger volume wins; when the object has NO marked volume it falls through to
the IDENTICAL passthrough/tier resolution, a safe degrade.

Runs in GENERIC mode only, inside ``contract_pipeline.transpile_with_contract``
on the RAW ``TranspiledScript.luau_source`` BEFORE comment-stripping -- AFTER
``lower_trigger_stay`` (so OnTriggerStay bindings are already renamed to
``connectGameObjectSignalStay``, which this pass's head regex does not match).
The ``-- OnTriggerEnter``/``-- OnTriggerExit`` origin comment is mandated by the
generic contract doc and is the lowering key.

EXACT, BINDING-LOCAL, comment-keyed (mirrors trigger_stay_lowering.py):
  * The key is the comment on the line IMMEDIATELY preceding the specific
    ``connectGameObjectSignal(...)`` binding -- not any substring elsewhere.
  * The token is the exact ``OnTriggerEnter`` / ``OnTriggerExit`` -- it must NOT
    match ``OnCollisionEnter``/``OnCollisionExit`` (which keep the body-bound
    edge for the projectile-damage fix) nor ``OnTriggerStay`` (already lowered
    to the Stay poll).
  * Pure head rename: NO argument surgery (unlike the Stay lowering, which drops
    ``"Touched", ``). Simpler and lower-risk.
  * Robust to ``self.host:`` and ``self.host.`` forms and leading whitespace.
  * Abstains when the call HEAD sits inside a Lua string / line comment / long-
    bracket string or block comment (reusing the Stay lowering's guards).
  * Idempotent: the rewritten head is ``connectGameObjectTriggerSignal(`` which
    does NOT contain a ``connectGameObjectSignal(`` match for the head regex
    (the inserted ``Trigger`` breaks the literal), so a second pass is a no-op.
"""

from __future__ import annotations

import re

# Reuse the binding-head regex and the string/comment abstain guards from the
# Stay lowering -- the same lexing utilities contract_verifier already imports
# from this module. Keeping them in one place avoids a divergent second copy.
from converter.trigger_stay_lowering import (
    _CONNECT_HEAD_RE,
    _HasLuauSource,
    _luau_pos_in_long_bracket,
    _luau_pos_is_code,
    _preceding_comment_line,
)

# The exact origin-comment tokens that gate the rewrite. Anchored to the start
# of the comment body so ``OnTriggerEnter`` / ``OnTriggerExit`` match while
# ``OnCollisionEnter``/``OnCollisionExit`` (start with ``OnCollision``) and
# ``OnTriggerStay`` (already lowered) do NOT. The ``\b`` stops a match on a
# hypothetical ``OnTriggerEntered``.
_ENTER_EXIT_COMMENT_RE = re.compile(r"^--\s*OnTrigger(?:Enter|Exit)\b")


def rewrite_trigger_enter_source(source: str) -> tuple[str, int]:
    """Rename each ``connectGameObjectSignal(`` binding whose immediately-
    preceding comment is ``-- OnTriggerEnter...`` or ``-- OnTriggerExit...`` to
    ``connectGameObjectTriggerSignal(`` (head rename only; all arguments
    preserved verbatim).

    Returns ``(new_source, count)`` where ``count`` is the number of bindings
    rewritten (0 -> ``source`` returned unchanged)."""
    count = 0
    out: list[str] = []
    cursor = 0  # index up to which output has been emitted

    for m in _CONNECT_HEAD_RE.finditer(source):
        head_start = m.start("head")
        # Skip heads inside a short string / ``--`` line comment (line-local) OR
        # inside a multi-line long-bracket string / block comment opened on an
        # earlier line. Either => leave untouched, never corrupt the payload.
        if not _luau_pos_is_code(source, head_start):
            continue
        if _luau_pos_in_long_bracket(source, head_start):
            continue
        # The origin comment must be on the line immediately above the binding.
        comment = _preceding_comment_line(source, m.start("indent"))
        if comment is None or not _ENTER_EXIT_COMMENT_RE.match(comment):
            continue
        # Pure head rename: emit everything up to the head, then the head with
        # the method name swapped, and resume the cursor at the end of the head
        # (the open paren). Every argument is preserved verbatim.
        new_head = m.group("head").replace(
            "connectGameObjectSignal", "connectGameObjectTriggerSignal", 1,
        )
        out.append(source[cursor:m.start("head")])
        out.append(new_head)
        cursor = m.end("head")
        count += 1

    if count == 0:
        return source, 0
    out.append(source[cursor:])
    return "".join(out), count


def lower_trigger_enter(scripts: list[_HasLuauSource]) -> int:
    """Rewrite each ``OnTriggerEnter``/``OnTriggerExit``-origin
    ``connectGameObjectSignal(go, "Touched"/"TouchEnded", fn)`` binding on every
    script's ``luau_source`` to the trigger-preferring host method
    ``connectGameObjectTriggerSignal(...)``. Returns the number of scripts
    modified.

    GENERAL rule: keyed on the guaranteed ``-- OnTriggerEnter`` / ``-- OnTriggerExit``
    origin comment immediately above the binding, NEVER on ``s.name`` -- applies
    to any trigger-Enter/Exit binding, not just the mine. OnCollision* keep their
    body-bound ``.Touched`` edge (their origin comments don't match), and
    OnTriggerStay was already lowered to the Stay poll before this pass runs."""
    changed = 0
    for s in scripts:
        src = s.luau_source or ""
        new_src, count = rewrite_trigger_enter_source(src)
        if count and new_src != src:
            s.luau_source = new_src
            changed += 1
    return changed
