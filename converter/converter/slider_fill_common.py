"""slider_fill_common.py -- shared, STRING-LEVEL primitives for the guessed-fill
slider rewrite.

The Unity ``Slider.value`` resize (``fill.Size = ...``) is converted with a
GUESSED fill child name (``:FindFirstChild("Fill"|"Bar"|"Foreground")``) because
the AI cannot know the real fill child. The deterministic upstream signal is the
``SliderFillElement`` attribute the UI translator already stamps on the slider
Frame (a relative ``/``-separated descendant path to the real fill). The fix
RESOLVES the fill via that attribute at runtime instead of the guessed literal.

There are two emitted shapes, both handled here at the SOURCE-STRING level so the
LEGACY coherence pack (``script_coherence_packs.py``, operates on
``RbxScript.source``) and the GENERIC contract-pipeline lowering
(``slider_fill_lowering.py``, operates on ``TranspiledScript.luau_source``) share
ONE implementation:

  * the INLINE resolution shape (the faithful OOP HUD, no ``setSliderValue``):
    ``local healthFill = healthFrame:FindFirstChild("Fill")`` -- rewritten here to
    ``local healthFill = _resolveSliderFill(healthFrame)`` + a one-shot helper.
  * the LEGACY free-function ``setSliderValue`` body shape -- detected here (so
    the inline path abstains on it) but rewritten by the legacy pack's own
    canonical-setter span splice (that splice is not shared; it is legacy-only).

Every public function takes and returns a SOURCE STRING (``src: str``) and never
mutates a wrapper object -- the two callers apply them to their respective source
field. Structural / attribute-keyed (never a per-game string, never an AI-output
fingerprint), idempotent, and abstains (leaves the literal + fires a coverage
warning upstream) on any shape it cannot rewrite unambiguously.
"""

from __future__ import annotations

import re

# The guessed-fill anchor tokens (any one identifies the buggy body shape). Shared
# by the inline detector and the coverage guard.
_SLIDER_GUESS_FILL_SIGS = (
    'slider:FindFirstChild("Fill")',
    'FindFirstChild("Bar")',
    'FindFirstChild("Foreground")',
)

# Unique injection token (idempotency gate): the helper DEFINITION (not a call
# site -- the rewrite emits call sites, so gating on the bare name would skip the
# definition injection).
_SLIDER_FILL_HELPER_TOKEN = "_resolveSliderFill"
_SLIDER_FILL_HELPER_DEF = "local function _resolveSliderFill"

# Module-level helper injected near the top of a rewritten script. Reads
# ``SliderFillElement`` off the frame, walks the slash-separated path with a
# fail-loud empty-segment guard (same technique as the canonical setter), and
# returns the fill GuiObject or nil. nil -> the existing ``if healthFill then``
# guard skips the resize (fail-soft-but-observable, no crash).
_SLIDER_FILL_HELPER = '''local function _resolveSliderFill(frame)
\tif not frame then return nil end
\tlocal path = frame:GetAttribute("SliderFillElement")
\tif type(path) ~= "string" or path == "" then
\t\twarn("[_resolveSliderFill] frame '" .. frame.Name .. "' has no SliderFillElement attribute; fill not resolved")
\t\treturn nil
\tend
\tlocal fill = frame
\tlocal cursor = 1
\twhile cursor <= #path + 1 do
\t\tlocal nextSlash = string.find(path, "/", cursor, true)
\t\tlocal segment
\t\tif nextSlash then
\t\t\tsegment = string.sub(path, cursor, nextSlash - 1)
\t\t\tcursor = nextSlash + 1
\t\telse
\t\t\tsegment = string.sub(path, cursor)
\t\t\tcursor = #path + 2
\t\tend
\t\tif segment == "" then
\t\t\twarn("[_resolveSliderFill] frame '" .. frame.Name .. "' fill path '" .. path .. "' has an empty segment; fill not resolved")
\t\t\treturn nil
\t\tend
\t\tfill = fill and fill:FindFirstChild(segment)
\t\tif not fill then
\t\t\twarn("[_resolveSliderFill] frame '" .. frame.Name .. "' fill path '" .. path .. "' did not resolve (segment '" .. segment .. "')")
\t\t\treturn nil
\t\tend
\tend
\treturn fill
end'''


# An inlined guessed-fill RESOLUTION assignment. Matches one statement line:
#   [local ]<lhs> = <rhs containing :FindFirstChild("Fill"|"Bar"|"Foreground")>
# Structural (no hardcoded ``healthFill``/``healthFrame``/``Health``); the LHS
# and the frame receiver are captured generically.
_INLINE_GUESS_FILL_RE = re.compile(
    r'^(?P<indent>[ \t]*)'
    r'(?P<decl>local\s+)?'
    r'(?P<lhs>[A-Za-z_]\w*)'
    r'\s*=\s*'
    r'(?P<rhs>[^\n]*:FindFirstChild\(\s*"(?:Fill|Bar|Foreground)"\s*\)[^\n]*?)'
    r'[ \t]*$',
    re.MULTILINE,
)

# The frame receiver of a guessed-fill ``:FindFirstChild`` call (the LAST such
# call in the RHS, e.g. the resolved-most expression in an ``a or b`` chain).
# Receiver is an identifier optionally with ``.field`` / ``[index]`` access.
_FILL_RECEIVER_RE = re.compile(
    r'([A-Za-z_][\w.]*(?:\[[^\]\n]+\])?)\s*:FindFirstChild\(\s*"(?:Fill|Bar|Foreground)"\s*\)'
)

# Any guessed-fill resolution literal (no receiver constraint) -- the broadened
# coverage signal so an un-rewritten inlined shape is flagged, not abstained.
_ANY_GUESS_FILL_RE = re.compile(r':FindFirstChild\(\s*"(?:Fill|Bar|Foreground)"\s*\)')


def has_guessed_fill(src: str) -> bool:
    """True if ``src`` contains any guessed-fill anchor token (legacy
    ``setSliderValue`` body shape OR the inlined resolution shape)."""
    return any(sig in src for sig in _SLIDER_GUESS_FILL_SIGS) or bool(
        _ANY_GUESS_FILL_RE.search(src)
    )


def free_function_setter_start(src: str, from_index: int = 0) -> int:
    """Index of the next FREE-FUNCTION ``setSliderValue`` definition start at or
    after ``from_index``, or -1.

    Covers ``local function setSliderValue`` and ``function setSliderValue``.
    The apply rewrites ONLY this form, because the canonical replacement body
    is itself a free function -- splicing it over a method-form definition
    (``function T:setSliderValue``) would break the call contract. A
    method-form guessed-fill HUD is therefore left for the coverage guard to
    flag LOUDLY rather than silently mangled.

    Returns the EARLIEST matching start so a file with several free-function
    setters is walked left-to-right (each span rewritten independently -- a
    second still-guessed setter is never silently skipped just because an
    earlier one was rewritten)."""
    best = -1
    for anchor in (
        "local function setSliderValue",
        "function setSliderValue",
    ):
        i = src.find(anchor, from_index)
        if i != -1 and (best == -1 or i < best):
            best = i
    return best


def has_slider_setter(src: str) -> bool:
    """True if ``src`` defines a ``setSliderValue`` in ANY form -- free-function
    (``[local ]function setSliderValue``) OR method (``:setSliderValue``).

    The broadened helper-name anchor shared by the detector and the coverage
    guard so a method-form HUD is never silently skipped by both."""
    return free_function_setter_start(src) != -1 or ":setSliderValue" in src


def has_inline_guessed_fill(src: str) -> bool:
    """True if ``src`` has an inlined guessed-fill RESOLUTION assignment in a
    script that defines NO ``setSliderValue`` (the generic OOP HUD shape).

    A script that DOES define a ``setSliderValue`` (free-function OR method form)
    is the LEGACY shape -- its guessed-fill resolution lives inside the setter
    body and is owned by the legacy span rewrite / coverage guard, never the
    inline path. Gating the inline path on the ABSENCE of any setSliderValue
    keeps the two shapes disjoint (no double-handling, no mangling a method-form
    body the legacy guard means to flag)."""
    if has_slider_setter(src):
        return False
    return _INLINE_GUESS_FILL_RE.search(src) is not None


def inject_slider_fill_helper(src: str) -> str:
    """Inject ``_resolveSliderFill`` once, after the leading ``require``/local
    header so it precedes its first use. Idempotent: gated on the helper token.

    Placement: after the last top-level (column-0) ``local ``/``require`` header
    line at the top of the file, else at the very top. The exact offset is not
    load-bearing (Luau hoists the resolution at call time, not parse time) -- we
    only need the helper defined before ``HudControl:Awake`` runs."""
    if _SLIDER_FILL_HELPER_DEF in src:
        return src
    lines = src.split("\n")
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if line[:1] in (" ", "\t"):
            # An indented line means the header block ended; stop.
            break
        if stripped.startswith("local ") or stripped.startswith("require"):
            insert_at = i + 1
        elif stripped == "":
            continue
        else:
            break
    block = _SLIDER_FILL_HELPER.split("\n")
    new_lines = lines[:insert_at] + ["", *block, ""] + lines[insert_at:]
    return "\n".join(new_lines)


def rewrite_inline_guessed_fill(src: str) -> tuple[str, int]:
    """Rewrite every inlined guessed-fill RESOLUTION assignment (outside a
    free-function ``setSliderValue`` body) to resolve via the injected helper:

        local healthFill = healthFrame:FindFirstChild("Fill")
        local healthFill = healthFrame and healthFrame:FindFirstChild("Fill")
            ->  local healthFill = _resolveSliderFill(healthFrame)

    Generic: the LHS and the frame receiver are captured structurally (no
    hardcoded names). Returns ``(new_src, count)``. Injects the helper once if
    any rewrite happened. Idempotent: the rewritten RHS holds no guessed-fill
    literal, so a second pass matches nothing.

    No-op when the script defines a ``setSliderValue`` (free OR method form):
    that is the LEGACY shape, owned by the canonical-setter span rewrite, and
    its guessed-fill resolution must NOT be touched by the inline path."""
    if has_slider_setter(src):
        return src, 0

    count = 0

    def _sub(m: "re.Match[str]") -> str:
        nonlocal count
        rhs = m.group("rhs")
        recv = _FILL_RECEIVER_RE.search(rhs)
        if recv is None:
            # No simple receiver (e.g. a parenthesised expr) -- abstain so the
            # coverage guard flags it rather than emitting a broken call.
            return m.group(0)
        frame_expr = recv.group(1)
        count += 1
        indent = m.group("indent")
        decl = m.group("decl") or ""
        lhs = m.group("lhs")
        return (
            f"{indent}{decl}{lhs} = "
            f"{_SLIDER_FILL_HELPER_TOKEN}({frame_expr})"
        )

    new_src = _INLINE_GUESS_FILL_RE.sub(_sub, src)
    if count:
        new_src = inject_slider_fill_helper(new_src)
    return new_src, count
