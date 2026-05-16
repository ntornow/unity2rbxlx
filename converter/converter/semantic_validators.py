"""semantic_validators.py — post-transpile semantic checks.

These rules scan post-transpile Luau output for patterns that compile
fine through ``luau-analyze`` but break at runtime in Roblox-specific
ways. The classic example is an FPS view-model parented to a character
descendant: ``rifle.Parent = head`` parses, runs, and renders nothing —
because ``PlayerModule.CameraModule.TransparencyController`` writes
``LocalTransparencyModifier = 1`` on close-to-camera character children
every frame.

The runtime-side fix for that specific bug shipped in the SimpleFPS
output, but the same class of bug recurs on any FPS-style Unity project
until the converter learns to detect it. This module is the detection
half — it emits structured warnings only. Automatic rewriting is
deliberately out of scope: the rules over-detect by design (sub-meter
literals are sometimes legitimate stud-space offsets), so a programmatic
rewrite would regress correct code. The FPS-weapon-mount class of bug
gets a targeted fix via the ``fps_weapon_mount_inject`` patch pack in
``script_coherence_packs.py``, not via this module.

Public entry point: :func:`run_semantic_validators`. Returns a list of
:class:`SemanticIssue` records.

Each rule is one function with a narrow trigger pattern so false-positive
risk stays low. Severity defaults to ``warning``; promote via ``--strict``
in CI to gate merges.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from core.roblox_types import RbxScript

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issue record
# ---------------------------------------------------------------------------


@dataclass
class SemanticIssue:
    """One semantic warning surfaced by a rule.

    Serialised directly into ``conversion_report.json.semantic_warnings``
    via ``asdict``; fields are stable wire format for downstream tools.
    """
    severity: str          # "warning" | "error"
    rule: str              # rule name (e.g. "weapon_clone_in_character")
    script: str            # script name (no extension)
    line: int              # 1-based line number into the Luau source
    snippet: str           # the offending line, trimmed
    explanation: str
    suggested_fix: str
    confidence: str = "high"  # "high" | "medium" | "low"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# FPS markers anchor the LocalScript-side rules (1, 5). A script that
# does not lock the mouse or anchor to ``LocalPlayer`` is highly
# unlikely to be an FPS view-model controller; firing rule 1 on a
# generic third-person script would be a false positive.
_FPS_MARKERS = (
    "MouseBehavior.LockCenter",
    "Cursor.lockState",
    "Enum.MouseBehavior.LockCenter",
)


def _is_fps_script(source: str, stripped: str | None = None) -> bool:
    """Detect FPS LocalScripts via Mouse-lock markers.

    The check ignores comments AND string literals (quoted strings
    AND long-bracket ``[[...]]`` strings) — a harmless ``local hint =
    "Enum.MouseBehavior.LockCenter"`` or
    ``-- Enum.MouseBehavior.LockCenter`` must not classify a script as
    FPS. ``_strip_comments`` preserves string content (so other rules
    can read it), so we additionally blank both string shapes here.
    """
    base = stripped if stripped is not None else _strip_comments(source)
    # Long-bracket strings ``[[ ... ]]`` / ``[=[ ... ]=]``.
    def _blank_long_bracket(m: re.Match[str]) -> str:
        text = m.group(0)
        return "".join("\n" if ch == "\n" else " " for ch in text)
    base = re.sub(
        r"\[(=*)\[.*?\]\1\]", _blank_long_bracket, base, flags=re.DOTALL,
    )
    # Quoted single-line strings.
    blanked = re.sub(
        r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'',
        lambda m: '"' + " " * (len(m.group(0)) - 2) + '"',
        base,
    )
    return any(marker in blanked for marker in _FPS_MARKERS)


def _line_of(source: str, offset: int) -> int:
    """1-based line number containing the byte at ``offset``."""
    return source.count("\n", 0, offset) + 1


def _snippet_at(source: str, line_no: int, max_len: int = 200) -> str:
    """Return the trimmed contents of a 1-based line."""
    lines = source.split("\n")
    if 1 <= line_no <= len(lines):
        snippet = lines[line_no - 1].strip()
        if len(snippet) > max_len:
            snippet = snippet[: max_len - 3] + "..."
        return snippet
    return ""


def _blank_strings(source: str) -> str:
    """Blank both single-line quoted strings AND long-bracket strings
    (``[[ ... ]]``), preserving offsets and newlines. Used by rules
    that key off code structure rather than string content — without
    this, a string literal like ``"p.Anchored = false"`` would
    false-positive ``anchored_false_no_weld``.
    """
    def _blank_long_bracket(m: re.Match[str]) -> str:
        return "".join("\n" if ch == "\n" else " " for ch in m.group(0))
    out = re.sub(
        r"\[(=*)\[.*?\]\1\]", _blank_long_bracket, source, flags=re.DOTALL,
    )
    return re.sub(
        r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'',
        lambda m: '"' + " " * (len(m.group(0)) - 2) + '"',
        out,
    )


def _strip_comments(source: str) -> str:
    """Replace Luau comments with spaces, preserving offsets.

    Detection rules walk the post-transpile source. Comments routinely
    contain phrases like ``rifle.Parent = head`` (in explanatory notes)
    that would false-positive rule 1. We keep offsets so ``_line_of``
    still maps cleanly to the original source for reporting.

    Tracks string state (single quotes, double quotes, long-bracket
    strings ``[[ ... ]]``) so ``--`` inside a string isn't mistaken for
    a comment start. Without this, code like
    ``local msg = "--"; rifle.Parent = character.Head`` blanks
    everything after the ``--`` inside the string.
    """
    out = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]

        # Long-form bracket string ``[[ ... ]]`` / ``[=[ ... ]=]``.
        # Detect at top level (not inside a comment, not inside a
        # quoted string — the loop body is only reached at top level).
        if ch == "[" and i + 1 < n:
            j = i + 1
            eq_count = 0
            while j < n and source[j] == "=":
                eq_count += 1
                j += 1
            if j < n and source[j] == "[":
                close = "]" + "=" * eq_count + "]"
                end = source.find(close, j + 1)
                if end == -1:
                    end = n
                else:
                    end += len(close)
                # Keep the string content verbatim (only comments
                # should be blanked).
                out.append(source[i:end])
                i = end
                continue

        # Single-line string. Walk to the matching close quote,
        # respecting backslash escapes.
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            j = i + 1
            while j < n:
                if source[j] == "\\" and j + 1 < n:
                    out.append(source[j:j + 2])
                    j += 2
                    continue
                if source[j] == quote:
                    out.append(source[j])
                    j += 1
                    break
                if source[j] == "\n":
                    # Unterminated string — Luau syntax error, but
                    # don't hang. Stop at the newline.
                    break
                out.append(source[j])
                j += 1
            i = j
            continue

        # Long-form bracket comment ``--[[ ... ]]`` / ``--[=[ ... ]=]``
        if source.startswith("--[", i):
            j = i + 3
            eq_count = 0
            while j < n and source[j] == "=":
                eq_count += 1
                j += 1
            if j < n and source[j] == "[":
                close = "]" + "=" * eq_count + "]"
                end = source.find(close, j + 1)
                if end == -1:
                    end = n
                else:
                    end += len(close)
                for c in source[i:end]:
                    out.append("\n" if c == "\n" else " ")
                i = end
                continue
        # Line comment ``-- ...``
        if source.startswith("--", i):
            j = source.find("\n", i)
            if j == -1:
                out.append(" " * (n - i))
                i = n
            else:
                out.append(" " * (j - i))
                i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Rule 1: weapon_clone_in_character
# ---------------------------------------------------------------------------

# A cloned thing whose Parent is later set to a character descendant. The
# detector is intentionally narrow: only matches obvious patterns where
# the parent expression's text contains a known character anchor or the
# script's own ``weaponSlot.Parent`` idiom. ``.PrimaryPart`` alone would
# false-positive on arbitrary models (weapon rigs, props), so we only
# match it when prefixed by ``character`` / ``Character`` /
# ``LocalPlayer.Character``.
_CHARACTER_ANCHOR_RE = re.compile(
    r"\b("
    r"character\.(?:Head|Torso|HumanoidRootPart|LeftHand|RightHand|UpperTorso|LowerTorso|PrimaryPart)"
    r"|Character\.(?:Head|Torso|HumanoidRootPart|LeftHand|RightHand|UpperTorso|LowerTorso|PrimaryPart)"
    r"|LocalPlayer\.Character(?:\.\w+)*"
    r"|weaponSlot\.Parent\b"
    r"|head\b"
    r"|hrp\b"
    r"|rootPart\b"
    r")"
)

# Variable name on the LHS of a ``... :Clone()`` assignment. The RHS
# can be a chained accessor — ``foo``, ``foo.bar``, ``foo:Method()``,
# ``foo:WaitForChild("X")``, ``foo:FindFirstChild("X"):Clone()`` and
# combinations — so we deliberately permit ``()``, ``:``, and quoted
# arg blobs between the leading identifier and the trailing ``:Clone()``.
_CLONE_ASSIGN_RE = re.compile(
    r"^[ \t]*(?:local\s+)?([A-Za-z_]\w*)\s*=\s*"
    r"[A-Za-z_][\w\.\[\]\"':\(\)\s,]*?"
    r":\s*Clone\s*\(\s*\)",
    re.MULTILINE,
)

# Parent-write whose LHS is ``<var>.Parent``.
_PARENT_WRITE_RE = re.compile(
    r"^[ \t]*([A-Za-z_]\w*)\s*\.\s*Parent\s*=\s*(.+?)\s*$",
    re.MULTILINE,
)


def _rule_weapon_clone_in_character(
    script: "RbxScript", source: str, stripped: str,
) -> list[SemanticIssue]:
    if not _is_fps_script(source, stripped):
        return []

    cloned_vars: set[str] = set()
    for m in _CLONE_ASSIGN_RE.finditer(stripped):
        cloned_vars.add(m.group(1))
    if not cloned_vars:
        return []

    issues: list[SemanticIssue] = []
    for m in _PARENT_WRITE_RE.finditer(stripped):
        target_var = m.group(1)
        rhs = m.group(2)
        if target_var not in cloned_vars:
            continue
        if not _CHARACTER_ANCHOR_RE.search(rhs):
            continue
        line_no = _line_of(source, m.start())
        issues.append(
            SemanticIssue(
                severity="warning",
                rule="weapon_clone_in_character",
                script=script.name,
                line=line_no,
                snippet=_snippet_at(source, line_no),
                explanation=(
                    "The cloned object ends up under the character. Roblox's "
                    "built-in PlayerModule.CameraModule.TransparencyController "
                    "writes LocalTransparencyModifier = 1 on close-to-camera "
                    "character descendants every frame, making the object "
                    "invisible in first person."
                ),
                suggested_fix=(
                    "Parent the clone to workspace and drive its CFrame from "
                    "camera.CFrame in a Heartbeat hook."
                ),
                confidence="high",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Rule 2: anchored_false_no_weld
# ---------------------------------------------------------------------------

_ANCHORED_FALSE_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*\.\s*Anchored\s*=\s*false\b",
)


def _rule_anchored_false_no_weld(
    script: "RbxScript", source: str, stripped: str,
) -> list[SemanticIssue]:
    if not _is_fps_script(source, stripped):
        return []

    # Only fire if the script clones something. Generic Anchored=false
    # in physics setup is correct; it's the camera-attached / cloned
    # case we care about.
    if "Clone()" not in stripped and ":Clone()" not in stripped:
        return []

    # has_weld scans ``stripped`` (with string content) so
    # ``Instance.new("WeldConstraint")`` is detectable. The
    # Anchored = false scan blanks string content separately so that
    # ``local msg = "p.Anchored = false"`` doesn't trigger.
    has_weld = (
        "WeldConstraint" in stripped
        or "Motor6D" in stripped
        or 'Instance.new("Weld' in stripped
    )
    code_only = _blank_strings(stripped)

    issues: list[SemanticIssue] = []
    for m in _ANCHORED_FALSE_RE.finditer(code_only):
        if has_weld:
            # Still warn if the assignment is structurally distant from
            # the weld — but as low-confidence. For now skip: avoids
            # the most common false positive.
            continue
        line_no = _line_of(source, m.start())
        issues.append(
            SemanticIssue(
                severity="warning",
                rule="anchored_false_no_weld",
                script=script.name,
                line=line_no,
                snippet=_snippet_at(source, line_no),
                explanation=(
                    "An object set to Anchored = false has no WeldConstraint "
                    "or Motor6D anywhere in this script. In an FPS context, "
                    "this means gravity will pull the object away from the "
                    "camera-driven pose."
                ),
                suggested_fix=(
                    "Set Anchored = true and update CFrame per Heartbeat, or "
                    "add a WeldConstraint linking the clone to an anchor part."
                ),
                confidence="medium",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Rule 3: suspicious_meter_literal
# ---------------------------------------------------------------------------

# Numeric literal (signed float or int). Disallows expressions so we
# avoid touching ``Vector3.new(myX, myY, myZ)``.
_NUM = r"-?\d+(?:\.\d+)?"
_VECTOR3_LITERAL_RE = re.compile(
    rf"Vector3\.new\s*\(\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*\)"
)
_CFRAME3_LITERAL_RE = re.compile(
    rf"CFrame\.new\s*\(\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*\)"
)

# Spatial-context keywords. Triggered when the literal is on a position-
# typed RHS or argument.
_SPATIAL_CONTEXT_RE = re.compile(
    r"\b("
    r"\.Position\b"
    r"|\.localPosition\b"
    r"|\.position\b"
    r"|:PivotTo\b"
    r"|\.CFrame\b"
    r")"
)


def _looks_like_meter_literal(x: float, y: float, z: float) -> bool:
    """All three components are in the sub-meter range AND at least one
    is nonzero. Filters direction-unit vectors (0,1,0), zero vectors,
    and stud-scale values.

    Threshold is intentionally tight (max magnitude < 1.0) — the Unity-
    meter bug case the rule was built for has components in [-0.7, 0.7]
    (SimpleFPS ``weaponSlot.localPosition = (0.4, -0.64, 0.6)``).
    Loosening to ``< 5.0`` over-reports common stud offsets like
    ``hit.Position + Vector3.new(0, 3, 0)`` as needing scale-up.
    """
    mags = (abs(x), abs(y), abs(z))
    if max(mags) >= 1.0:
        return False
    if max(mags) <= 0.01:
        return False
    return True


def _has_studs_context(source: str) -> bool:
    """The script's other positional values look like studs (>= 10).

    Rough heuristic: if we see any literal Vector3 or CFrame with a
    magnitude >= 10, the script is operating in studs and a sub-1
    literal is anomalous. If nothing in the script is in studs, the
    whole script may already be in a meter convention (rare in
    transpile output, but suppresses the warning).
    """
    for m in _VECTOR3_LITERAL_RE.finditer(source):
        x, y, z = map(float, m.groups())
        if max(abs(x), abs(y), abs(z)) >= 10.0:
            return True
    for m in _CFRAME3_LITERAL_RE.finditer(source):
        x, y, z = map(float, m.groups())
        if max(abs(x), abs(y), abs(z)) >= 10.0:
            return True
    return False


def _rule_suspicious_meter_literal(
    script: "RbxScript", source: str, stripped: str,
) -> list[SemanticIssue]:
    has_studs = _has_studs_context(stripped)
    issues: list[SemanticIssue] = []

    for pattern, ctor in ((_VECTOR3_LITERAL_RE, "Vector3"),
                          (_CFRAME3_LITERAL_RE, "CFrame")):
        for m in pattern.finditer(stripped):
            x, y, z = map(float, m.groups())
            if not _looks_like_meter_literal(x, y, z):
                continue
            # Spatial-context window: the literal must appear within a
            # spatial-keyword neighbourhood. Look at the line + the
            # preceding line (PivotTo / .CFrame = often spans lines).
            line_no = _line_of(source, m.start())
            lines = stripped.split("\n")
            window_start = max(0, line_no - 2)
            window_end = min(len(lines), line_no + 1)
            window = "\n".join(lines[window_start:window_end])
            if not _SPATIAL_CONTEXT_RE.search(window):
                continue
            if not has_studs:
                # No studs-context anchor → low confidence, skip.
                continue
            issues.append(
                SemanticIssue(
                    severity="warning",
                    rule="suspicious_meter_literal",
                    script=script.name,
                    line=line_no,
                    snippet=_snippet_at(source, line_no),
                    explanation=(
                        f"{ctor}.new({x:g}, {y:g}, {z:g}) looks like Unity "
                        "meters (all components |v| < 5) used in a spatial "
                        "context, but the rest of the script uses studs "
                        "(values >= 10). The object will appear ~3.6x too "
                        "close to its anchor."
                    ),
                    suggested_fix=(
                        "Multiply each component by STUDS_PER_METER (3.571) "
                        "if the source value is Unity meters. The warning "
                        "can also be a false positive on legitimate stud-"
                        "space sub-unit offsets (e.g. a half-stud camera "
                        "lift above the head); inspect before rewriting."
                    ),
                    confidence="medium",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Rule 4: attachment_as_render_parent
# ---------------------------------------------------------------------------

_INSTANCE_NEW_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*=\s*Instance\.new\s*\(\s*[\"'](\w+)[\"']",
)

# Roblox BasePart subclasses — instances of these don't render under
# Attachments. Effect instances (ParticleEmitter, Trail, Beam, Light,
# Sound) DO render under Attachments and must not trip this rule.
_BASEPART_CLASSNAMES = frozenset({
    "Part", "MeshPart", "WedgePart", "CornerWedgePart",
    "TrussPart", "Seat", "VehicleSeat", "SpawnLocation",
    "UnionOperation", "NegateOperation",
})


def _rule_attachment_as_render_parent(
    script: "RbxScript", source: str, stripped: str,
) -> list[SemanticIssue]:
    # Collect every Instance.new declaration so we can tell parts from
    # effects. A BasePart variable parented to an Attachment is invisible;
    # a ParticleEmitter/Trail/Beam under an Attachment is the canonical
    # supported pattern.
    var_class: dict[str, str] = {}
    for m in _INSTANCE_NEW_RE.finditer(stripped):
        var_class[m.group(1)] = m.group(2)

    attachment_vars = {v for v, c in var_class.items() if c == "Attachment"}
    if not attachment_vars:
        return []

    issues: list[SemanticIssue] = []
    for m in _PARENT_WRITE_RE.finditer(stripped):
        target_var = m.group(1)
        rhs = m.group(2).strip()
        # Only fire when we KNOW the LHS is a BasePart. If we can't
        # trace the variable to an Instance.new("Part"/...) call we
        # skip — false negatives are better than warning users about
        # legitimate emitter.Parent = attachment patterns.
        if var_class.get(target_var) not in _BASEPART_CLASSNAMES:
            continue
        for av in attachment_vars:
            if rhs == av or rhs.startswith(av + "."):
                line_no = _line_of(source, m.start())
                issues.append(
                    SemanticIssue(
                        severity="warning",
                        rule="attachment_as_render_parent",
                        script=script.name,
                        line=line_no,
                        snippet=_snippet_at(source, line_no),
                        explanation=(
                            "BasePart parented to an Attachment instance. "
                            "Roblox does not render BaseParts under Attachments — "
                            "the object will be invisible at runtime."
                        ),
                        suggested_fix=(
                            "Parent to workspace (or a Model under workspace) "
                            "and drive the BasePart's CFrame from the "
                            "attachment's WorldCFrame."
                        ),
                        confidence="high",
                    )
                )
                break
    return issues


# ---------------------------------------------------------------------------
# Rule 5: camera_attached_no_per_frame
# ---------------------------------------------------------------------------

_CAMERA_CFRAME_USE_RE = re.compile(
    r"\bcamera\.CFrame\b|\bworkspace\.CurrentCamera\.CFrame\b"
)
_RUNSERVICE_CONNECT_RE = re.compile(
    r"RunService\s*\.\s*(?:Heartbeat|RenderStepped|Stepped)\s*:\s*Connect\b"
)


def _collect_runservice_callback_bodies(source: str) -> list[str]:
    """Return every ``RunService.X:Connect(function ... end)`` callback
    body as a substring. Counts ``function``/``if``/``for``/``while``
    / ``do`` opens against ``end`` closes so an inner ``if cond then
    ... end`` doesn't prematurely close the body.

    ``for``/``while`` consume their following ``do`` (which would
    otherwise be counted as a standalone block open). A pure
    ``do ... end`` is counted normally.
    """
    out: list[str] = []
    body_open_re = re.compile(
        r"RunService\s*\.\s*\w+\s*:\s*Connect\s*\(\s*function\s*\([^)]*\)"
    )
    open_re = re.compile(r"\b(?:function|if|for|while|do)\b")
    end_re = re.compile(r"\bend\b")
    for cm in body_open_re.finditer(source):
        i = cm.end()
        depth = 1
        last = len(source)
        pos = i
        loop_pending_do = 0
        while pos < len(source):
            om = open_re.search(source, pos)
            em = end_re.search(source, pos)
            if em is None:
                break
            if om is not None and om.start() < em.start():
                kw = om.group()
                if kw == "do" and loop_pending_do > 0:
                    loop_pending_do -= 1
                else:
                    depth += 1
                    if kw in ("for", "while"):
                        loop_pending_do += 1
                pos = om.end()
                continue
            depth -= 1
            if depth == 0:
                last = em.start()
                break
            pos = em.end()
        out.append(source[i:last])
    return out


def _rule_camera_attached_no_per_frame(
    script: "RbxScript", source: str, stripped: str,
) -> list[SemanticIssue]:
    if not _is_fps_script(source, stripped):
        return []
    matches = list(_CAMERA_CFRAME_USE_RE.finditer(stripped))
    if not matches:
        return []
    # Of the matches, flag the FIRST placement (assigns CFrame or
    # calls :PivotTo) whose target variable has no per-frame update.
    # Otherwise we'd flag every ``camera.CFrame * Vector3`` raycast
    # computation.
    #
    # Per-variable check (not global ``any RunService:Connect``): a
    # variable is "updated per frame" iff its ``<var>:PivotTo`` /
    # ``<var>.CFrame =`` placement appears more than once in the script.
    # Scripts with an unrelated Heartbeat loop don't suppress this rule;
    # only scripts that actually re-place the same variable do.
    #
    # Exclude lines where the camera itself is being assigned (e.g.
    # ``camera.CFrame = ...`` in CharacterAdded). The rule fires on
    # OTHER objects positioned via camera.CFrame, not on the camera
    # initialising itself — ``hardcoded_camera_height`` covers that.
    target_var_re = re.compile(
        r"([A-Za-z_]\w*)\s*(?::\s*PivotTo\s*\(|\.\s*CFrame\s*=)"
    )
    # Collect every ``RunService.X:Connect(function ... end)`` callback
    # body so we can check that a variable's placement actually happens
    # INSIDE one — two one-shot placements (equip + ADS toggle) with
    # an unrelated Heartbeat for cooldowns must not be treated as a
    # per-frame update.
    callback_bodies = _collect_runservice_callback_bodies(stripped)
    issues: list[SemanticIssue] = []
    source_lines = source.split("\n")
    for m in matches:
        line_no = _line_of(source, m.start())
        line_text = _snippet_at(source, line_no)
        if re.match(
            r"\s*(?:workspace\.CurrentCamera|camera)\s*\.\s*(?:CFrame|Position)\s*=",
            line_text,
        ):
            continue
        # Look across the current line + 2 preceding lines for the
        # placement keyword — common Luau format wraps the call across
        # lines:
        #   rifle:PivotTo(
        #       camera.CFrame * CFrame.new(0, 0, -3)
        #   )
        window_start = max(0, line_no - 3)
        window = "\n".join(source_lines[window_start:line_no])
        if not (
            "PivotTo" in window
            or ".CFrame =" in window
            or ".Position =" in window
        ):
            continue
        # Find the placed variable in the window.
        var_m = target_var_re.search(window)
        if var_m is not None:
            var_name = var_m.group(1)
            # Count occurrences of ``<var>:PivotTo(`` / ``<var>.CFrame=``
            # in the whole stripped source. > 1 = per-frame update path.
            var_placement_re = re.compile(
                rf"\b{re.escape(var_name)}\s*(?::\s*PivotTo\s*\(|\.\s*CFrame\s*=)"
            )
            # The variable is "per-frame driven" only if at least one
            # of its placements lives INSIDE a RunService callback body.
            if any(
                var_placement_re.search(body) for body in callback_bodies
            ):
                continue
            issues.append(
                SemanticIssue(
                    severity="warning",
                    rule="camera_attached_no_per_frame",
                    script=script.name,
                    line=line_no,
                    snippet=line_text,
                    explanation=(
                        "Object placed via camera.CFrame but the script "
                        "never re-places it inside a RunService callback. "
                        "The object will be positioned once and won't "
                        "track the camera as it rotates."
                    ),
                    suggested_fix=(
                        "Wrap the placement in "
                        "RunService.Heartbeat:Connect so the object "
                        "follows the view direction every frame."
                    ),
                    confidence="high",
                )
            )
            break
    return issues


# ---------------------------------------------------------------------------
# Rule 6: hardcoded_camera_height
# ---------------------------------------------------------------------------

# ``camera.CFrame = CFrame.new(<expr>.Position + Vector3.new(0, K, 0)``
# style — also matches ``workspace.CurrentCamera.CFrame = ...`` (the
# transpiler can emit either form depending on whether ``camera`` is
# aliased at the top of the script).
_CAM_HEIGHT_RE = re.compile(
    r"(?:camera|workspace\.CurrentCamera)\.CFrame\s*=[^\n]+?\.Position\s*\+\s*"
    rf"Vector3\.new\s*\(\s*0\s*,\s*({_NUM})\s*,\s*0\s*\)"
)


def _rule_hardcoded_camera_height(
    script: "RbxScript", source: str, stripped: str,
    *,
    unity_camera_height_studs: float | None = None,
) -> list[SemanticIssue]:
    """Without knowing the Unity-source camera height, the best we can
    do is warn when the literal looks too small for studs (< 1.0) —
    suggesting a meter value was emitted verbatim."""
    issues: list[SemanticIssue] = []
    for m in _CAM_HEIGHT_RE.finditer(stripped):
        k = float(m.group(1))
        line_no = _line_of(source, m.start())
        # Flag values that look like Unity meters (sub-1 magnitudes,
        # or > 0 but < 5 when we know the expected studs value).
        suspicious = abs(k) > 0 and abs(k) < 1.0
        if unity_camera_height_studs is not None:
            suspicious = abs(k - unity_camera_height_studs) > 0.5
        if not suspicious:
            continue
        issues.append(
            SemanticIssue(
                severity="warning",
                rule="hardcoded_camera_height",
                script=script.name,
                line=line_no,
                snippet=_snippet_at(source, line_no),
                explanation=(
                    f"Camera placed with a hardcoded Y offset of {k:g}. "
                    "Values below 1.0 stud are almost certainly Unity "
                    "meters left unscaled — the camera will sit at the "
                    "anchor part's origin."
                ),
                suggested_fix=(
                    "Read the Unity Camera.localPosition from the prefab "
                    "and multiply by STUDS_PER_METER (3.571)."
                ),
                confidence="medium",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


RuleFn = Callable[
    ["RbxScript", str, str],
    list[SemanticIssue],
]

_RULES: tuple[tuple[str, RuleFn], ...] = (
    ("weapon_clone_in_character", _rule_weapon_clone_in_character),
    ("anchored_false_no_weld", _rule_anchored_false_no_weld),
    ("suspicious_meter_literal", _rule_suspicious_meter_literal),
    ("attachment_as_render_parent", _rule_attachment_as_render_parent),
    ("camera_attached_no_per_frame", _rule_camera_attached_no_per_frame),
    ("hardcoded_camera_height", _rule_hardcoded_camera_height),
)


@dataclass
class SemanticReport:
    """Aggregate output of one pipeline pass through the validators."""
    issues: list[SemanticIssue] = field(default_factory=list)

    @property
    def counts_by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for issue in self.issues:
            out[issue.rule] = out.get(issue.rule, 0) + 1
        return out

    def to_dict(self) -> dict:
        return {
            "total": len(self.issues),
            "counts_by_rule": self.counts_by_rule,
            "issues": [i.to_dict() for i in self.issues],
        }


def run_semantic_validators(
    scripts: list["RbxScript"],
    *,
    enabled_rules: set[str] | None = None,
) -> SemanticReport:
    """Run every registered rule against every script.

    Args:
        scripts: The post-transpile Luau scripts.
        enabled_rules: When provided, only rules in the set run. Useful
            for tests targeting one rule.

    Returns:
        SemanticReport with the union of issues. Order is deterministic:
        script name → rule order → first-match line.
    """
    report = SemanticReport()
    # Rules that key off ``Instance.new("...")`` / ``"WeldConstraint"``
    # string arguments need string content preserved. Every other rule
    # pattern-matches Luau code that should NOT include text from
    # string literals; we pass them a string-blanked variant to avoid
    # false positives like ``local msg = "p.Anchored = false"``.
    _RULES_NEEDING_STRINGS = {
        "attachment_as_render_parent",  # ``Instance.new("Attachment")``
        "anchored_false_no_weld",       # ``Instance.new("WeldConstraint")``
    }
    for script in sorted(scripts, key=lambda s: s.name):
        source = script.source or ""
        if not source.strip():
            continue
        stripped = _strip_comments(source)
        stripped_no_strings = _blank_strings(stripped)
        for name, fn in _RULES:
            if enabled_rules is not None and name not in enabled_rules:
                continue
            stripped_for_rule = (
                stripped if name in _RULES_NEEDING_STRINGS
                else stripped_no_strings
            )
            try:
                hits = fn(script, source, stripped_for_rule)
            except Exception as exc:
                log.warning(
                    "[semantic_validators] rule %s raised on %s: %s",
                    name, script.name, exc,
                )
                continue
            report.issues.extend(hits)
    return report
