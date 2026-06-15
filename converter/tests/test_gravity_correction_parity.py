"""Cross-file PARITY test for the shared ``correctDynamicAssembly`` helper --
AC14 of the Phase-1 design (relation #8, scale-faithful gravity), slice 1.4.

The helper LOGIC lives in TWO emission sites and MUST stay identical:

  * the CANONICAL text -- ``autogen._GRAVITY_CORRECTION_HELPER_LUAU`` -- baked
    into the standalone ``SceneGravityCorrection`` server script (slice 1.2);
  * the MIRROR -- embedded in a ``do ... end`` block in
    ``converter/runtime/scene_runtime.luau`` and captured into the module-scope
    upvalue ``_gravityCorrectDynamicAssembly`` for the client clone-site hook
    (slice 1.3).

This test is the DRIFT GUARD: it normalizes whitespace, excludes the
wrapper-only lines, and asserts the load-bearing LOGIC is token-identical
between the two sites. It is NOT a tautology -- a one-token change to EITHER
helper (force formula, skip-rule, fact-resolution, representative-part
resolution, VectorForce shape, or the ``_ScaleGravityCorrected`` tag) trips it.

Per D-1.3a, the two RAW texts legitimately differ in exactly two ways, both of
which this test normalizes/excludes:

  (a) INDENTATION / whitespace -- the canonical text is 4-space-indented; the
      mirror is tab-indented AND sits one ``do``-block level deeper. Normalized
      away by stripping leading/trailing whitespace per line and collapsing
      internal runs.
  (b) the mirror's WRAPPER-ONLY lines -- the ``do`` / ``end`` block delimiters,
      the upvalue declaration + capture (``local _gravityCorrectDynamicAssembly``
      / ``_gravityCorrectDynamicAssembly = correctDynamicAssembly``), and the
      mirror's OWN ``local TAG = "_ScaleGravityCorrected"`` line. In the
      canonical text ``TAG`` is supplied by the surrounding server script
      (``local TAG = "_ScaleGravityCorrected"`` lives OUTSIDE the helper
      constant), so the canonical body has no ``local TAG`` line -- it is a
      wrapper-only line on the mirror side and is excluded from the comparison.

The module-level ``_isBasePart`` at scene_runtime.luau:373 (which D-1.3a notes
legitimately DIFFERS from the canonical helper's own ``_isBasePart``) is NOT
part of either compared body: the canonical helper's ``_isBasePart`` is a LOCAL
inside the helper constant and is mirrored verbatim INSIDE the ``do`` block, so
it IS compared; the unrelated module-level one is outside the extracted block.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from converter.autogen import _GRAVITY_CORRECTION_HELPER_LUAU  # noqa: E402

RUNTIME_PATH = Path(__file__).parent.parent / "runtime" / "scene_runtime.luau"

# Stable sentinel LINES bounding the mirrored helper body inside
# scene_runtime.luau. NOT fixed line numbers and NOT exact-byte anchors -- they
# are matched after ``splitlines()`` + ``strip()`` so the comparison is
# WHITESPACE/INDENT-INSENSITIVE: a wrapper-only reindent (tabs<->spaces, a
# changed ``do``-block depth) must NOT trip the hard-error path before the
# helper-body line-for-line comparison runs. Located by the upvalue-decl + ``do``
# opener and the upvalue-capture + ``end`` closer the mirror was deliberately
# wrapped in (slice 1.3, D-1.3a). Anything between these sentinels is the
# mirrored LOGIC (incl. the mirror's own ``local TAG`` line, which the normalizer
# excludes). Matched on the stripped line content, so leading indentation of the
# sentinels themselves is irrelevant.
_MIRROR_OPEN_DECL = "local _gravityCorrectDynamicAssembly"
_MIRROR_OPEN_DO = "do"
_MIRROR_CLOSE_CAPTURE = "_gravityCorrectDynamicAssembly = correctDynamicAssembly"
_MIRROR_CLOSE_END = "end"

# Wrapper-only LOGIC lines present on the mirror side but NOT in the canonical
# helper body (the canonical text gets its ``TAG`` from the surrounding server
# script). Excluded from the body comparison; their presence/absence is asserted
# separately so the exclusion can never silently swallow a real logic line.
_MIRROR_WRAPPER_ONLY_LINES = frozenset({'local TAG = "_ScaleGravityCorrected"'})


def _normalized_logic_lines(text: str) -> list[str]:
    """Whitespace-normalized, non-blank LOGIC lines of a Luau helper body.

    Strips leading/trailing whitespace per line (defeats the tabs-vs-spaces +
    extra ``do``-block indent difference) and collapses internal whitespace runs
    (so e.g. realignment of an expression does not register as drift). Blank
    lines are dropped (they carry no logic and the two sites space differently).
    """
    out: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        out.append(" ".join(stripped.split()))
    return out


def _extract_mirror_body() -> str:
    """The raw text of the mirrored helper, between the stable ``do``/``end``
    sentinel LINES. Located indent-insensitively (each line ``strip()``-ed before
    matching) so a wrapper-only reindent does NOT trip this hard-error path before
    the helper-body comparison; fails loudly (the sentinels are load-bearing) only
    if the wrapper SHAPE actually changed.

    The opener is the ``local _gravityCorrectDynamicAssembly`` decl immediately
    followed by a ``do`` line; the body runs up to (exclusive) the
    ``_gravityCorrectDynamicAssembly = correctDynamicAssembly`` capture line. The
    extracted body text preserves the original (indented) source lines so the
    normalizer in ``_normalized_logic_lines`` still does the whitespace collapse.
    """
    source = RUNTIME_PATH.read_text(encoding="utf-8")
    lines = source.splitlines()
    stripped = [ln.strip() for ln in lines]

    open_idx: int | None = None
    for i in range(len(stripped) - 1):
        if stripped[i] == _MIRROR_OPEN_DECL and stripped[i + 1] == _MIRROR_OPEN_DO:
            open_idx = i + 2  # body starts after the ``do`` line
            break

    close_idx: int | None = None
    if open_idx is not None:
        for j in range(open_idx, len(stripped)):
            if stripped[j] == _MIRROR_CLOSE_CAPTURE:
                close_idx = j  # body ends BEFORE the capture line
                break

    if open_idx is None or close_idx is None:
        raise AssertionError(
            "could not locate the mirrored correctDynamicAssembly do/end block "
            "in scene_runtime.luau via its stable sentinel lines -- the slice-1.3 "
            "wrapper shape changed; update the parity-test sentinels deliberately"
        )
    return "\n".join(lines[open_idx:close_idx])


def _canonical_body_lines() -> list[str]:
    return _normalized_logic_lines(_GRAVITY_CORRECTION_HELPER_LUAU)


def _mirror_body_lines() -> list[str]:
    return _normalized_logic_lines(_extract_mirror_body())


# ---------------------------------------------------------------------------
# AC14 -- the parity assertion proper
# ---------------------------------------------------------------------------


def test_mirror_body_extraction_is_unambiguous() -> None:
    """The stable sentinel lines locate exactly one mirrored block (indent-
    insensitively) and it is non-empty (a precondition for the parity comparison
    having teeth)."""
    source = RUNTIME_PATH.read_text(encoding="utf-8")
    stripped = [ln.strip() for ln in source.splitlines()]
    # Exactly one opener (decl line immediately followed by a ``do`` line).
    openers = sum(
        1
        for i in range(len(stripped) - 1)
        if stripped[i] == _MIRROR_OPEN_DECL and stripped[i + 1] == _MIRROR_OPEN_DO
    )
    assert openers == 1, f"expected exactly one mirror opener, found {openers}"
    captures = stripped.count(_MIRROR_CLOSE_CAPTURE)
    assert captures == 1, f"expected exactly one mirror capture line, found {captures}"
    assert _mirror_body_lines(), "mirror body extracted empty"


def test_canonical_and_mirror_helper_logic_are_token_identical() -> None:
    """AC14: the shared correctDynamicAssembly helper LOGIC is token-identical
    between autogen's canonical ``_GRAVITY_CORRECTION_HELPER_LUAU`` and the
    mirror in scene_runtime.luau.

    Comparison is over the WHOLE normalized body (not a curated token subset),
    so ANY logic-token drift -- a changed operator in the force formula, a
    flipped skip-rule, a renamed attribute -- trips it. The mirror's
    wrapper-only ``local TAG`` line (D-1.3a) is excluded; the indentation /
    tabs-vs-spaces difference is normalized away.
    """
    canonical = _canonical_body_lines()
    mirror_full = _mirror_body_lines()

    # Exclude the mirror's wrapper-only lines (its own ``local TAG`` decl, which
    # in the canonical text is supplied by the enclosing server script). Assert
    # each excluded line is actually present on the mirror so the exclusion set
    # never silently hides a divergence by listing a line that isn't there.
    for wrapper_line in _MIRROR_WRAPPER_ONLY_LINES:
        assert wrapper_line in mirror_full, (
            f"declared wrapper-only line {wrapper_line!r} is not in the mirror "
            "body -- the exclusion list is stale; re-derive it"
        )
    mirror = [ln for ln in mirror_full if ln not in _MIRROR_WRAPPER_ONLY_LINES]

    # Ordered, line-for-line equality -- catches not just token changes but any
    # reordering of the helper's statements between the two sites.
    assert mirror == canonical, (
        "gravity-correction helper DRIFT between autogen "
        "_GRAVITY_CORRECTION_HELPER_LUAU (canonical) and the scene_runtime.luau "
        "mirror.\n"
        f"  only in CANONICAL: {[l for l in canonical if l not in set(mirror)]}\n"
        f"  only in MIRROR:    {[l for l in mirror if l not in set(canonical)]}"
    )


def test_load_bearing_logic_tokens_present_in_both() -> None:
    """AC14 (explicit token coverage): the load-bearing LOGIC tokens the design
    enumerates are present in BOTH bodies. Redundant with the line-for-line
    equality above, but pins exactly WHICH contract tokens this guard protects,
    so a reviewer can see the formula/skip-rules/fact-resolution/tag are covered
    and a future refactor of the equality check cannot quietly drop them."""
    canonical_joined = "\n".join(_canonical_body_lines())
    mirror_joined = "\n".join(_mirror_body_lines())

    required_tokens = (
        # force formula (AssemblyMass*(g_live - desired)) + apply-at-com / world
        "local force = mass * (workspace.Gravity - desiredStuds)",
        "local desiredStuds = desiredBaseStuds * gravityScale",
        "local gravityScale = (useGravityAttr == false) and 0 or (gravityScaleAttr or 1.0)",
        "vf.ApplyAtCenterOfMass = true",
        "vf.RelativeTo = Enum.ActuatorRelativeTo.World",
        "vf.Force = Vector3.new(0, force, 0)",
        # class-agnostic representative-part / root resolution
        "representativePart = carrier.PrimaryPart",
        'or carrier:FindFirstChildWhichIsA("BasePart", true)',
        "local root = representativePart.AssemblyRootPart or representativePart",
        # skip-rules
        "if root:GetAttribute(TAG) ~= nil then",  # already-tagged
        "if root.Anchored then",  # skip-if-anchored
        'if ancestorModel:FindFirstChildWhichIsA("Humanoid") then',  # Humanoid (ancestor-Model form)
        "local mass = root.AssemblyMass",
        "if mass <= 0 then",  # AssemblyMass<=0 skip-without-tag
        # UseGravity=false full-cancel (gravityScale -> 0) is the formula line above;
        # fact resolution: root-first then ancestor-Model walk
        "local function factOf(carrier, key)",
        'local useGravityAttr = factOf(carrier, "UseGravity")',
        'local gravityScaleAttr = factOf(carrier, "GravityScale")',
        'local owner = carrier:FindFirstAncestorWhichIsA("Model")',
        # the _ScaleGravityCorrected tag
        "root:SetAttribute(TAG, true)",
    )
    for token in required_tokens:
        assert token in canonical_joined, (
            f"logic token missing from CANONICAL helper: {token!r}"
        )
        assert token in mirror_joined, (
            f"logic token missing from scene_runtime.luau MIRROR: {token!r}"
        )

    # The _Rigidbody2D exclusion + the class-agnostic _UnityMass scan live on the
    # SCAN surfaces (server script / client _correctClonedDynamics), NOT inside the
    # shared helper, so they are pinned by the slice-1.2/1.3 tests -- not here.

    # _ScaleGravityCorrected tag literal travels with the helper on the mirror
    # side (its own ``local TAG`` line); on the canonical side it is supplied by
    # the server script. Assert the literal is reachable from each site.
    assert '"_ScaleGravityCorrected"' in RUNTIME_PATH.read_text(encoding="utf-8")
