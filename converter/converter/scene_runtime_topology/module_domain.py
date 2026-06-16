"""module_domain — per-module execution-domain classification.

Relocated in Phase 1 from ``converter/scene_runtime_domain.py``. The
classifier guts are byte-identical with the pre-Phase-1 source; only
the file's home + docstring + ``__all__`` shape changed.

Phase 2a will refactor this module SLICE BY SLICE (storage mutations
move out to ``script_storage``, signal tables consolidate, etc.). For
Phase 1 the goal is structural: the topology package owns domain
classification.

Phase 2a slice 6 split the public entry point into three pure-ish
functions that can run before ``classify_storage`` and a single late
finalizer that runs after. The legacy entry point
``classify_scene_runtime_domains`` is preserved as a thin orchestrator
of those four pieces, byte-identical in behavior to the pre-split
implementation:

  - ``infer_module_domains`` (early, pure) -- per-module domain inference
    via the 7-rule signal table. NO ``parent_path`` reads, NO mutation
    of ``scene_runtime``.
  - ``derive_reachability_requirements`` (early, pure) -- decides which
    helper modules MUST land in ReplicatedStorage (client require-graph
    reach) or excluded (both-sides conflict). NO ``parent_path`` reads,
    NO mutation.
  - ``finalize_topology_containers`` (late, mutating) -- stamps
    ``domain`` / ``domain_signals`` / ``container`` / ``module_path`` /
    ``reachability_forced_container`` onto every module row using the
    early-derived domain map + reachability requirements + the now-
    final ``parent_path`` from ``classify_storage``. This is the ONLY
    place ``parent_path`` is read by the topology classifier today.
  - ``classify_scene_runtime_domains`` -- coordinator. Calls the three
    above in order and builds the ``DomainClassifierReport``.

Slice-6 split rationale: ``_classify_module`` consumes only the C#
source, post-transpile Luau, per-instance evidence, overrides, and
networking-mode flags — it never reads ``parent_path``. By extracting
that pure piece (``infer_module_domains``) we can run it BEFORE
``classify_storage`` and feed the resulting domain map back into
storage decision-making (slice 7 will rewrite ``_decide_script_container``
on top of that channel). Slice 6 itself just plumbs the prepass; the
storage decision tree is unchanged, gated on a kwarg fork in
``classify_storage``.

Public entry point: ``classify_scene_runtime_domains``. Runs after the
storage classifier seeds ``scene_runtime.modules`` and after the
planner has populated instance / reference rows. Assigns each
runtime-bearing module one of:

  - ``"client"``   -- per the design doc rule table.
  - ``"server"``   -- per the rule table.
  - ``"helper"``   -- not runtime-bearing; pure utility module.
  - ``"excluded"`` -- runtime-bearing but unresolvable (Rule-1 / Rule-4
                      / reachability conflict). Recorded in the report;
                      the host runtime never instantiates it.

The v2 classifier consumes **three signal channels**:

  1. **C# source** (the strongest channel): looked up per-module via the
     ``guid_index`` mapping ``script_id -> asset_path``. The C# tables
     fire on Unity-specific patterns (``using UnityEngine.UI``,
     ``[SerializeField] Text``, ``Input.Get*``, ``[ServerRpc]``, ...).

  2. **Post-transpile Luau** (legacy PR3b channel; kept). The classifier
     still scans the post-transpile body for Roblox-flavoured signals
     (``Players.LocalPlayer``, ``:FireServer(``, ``.OnServerEvent``, ...).

  3. **Per-instance evidence**: the planner stamps
     ``instance_owner_is_ui`` per-instance when the host GameObject lives
     in a Canvas subtree, and ``target_is_ui`` on UI-bearing refs. Both
     contribute STRONG CLIENT signals (the design doc lists them on par
     with ``[SerializeField] Text``).

Signals are then bucketed into **strong** / **moderate** counts and
resolved through the 7-rule table in the design doc. Operator overrides
apply after the rule table with the rule-specific asymmetry from
§"Operator override" (Rule-1 ``excluded`` accepts only ``"excluded"``;
Rule-4 ``excluded`` and all other verdicts accept ``"client"`` /
``"server"`` / ``"excluded"``).

See ``converter/docs/design/scene-runtime-domain-signals.md`` for the
full spec.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Iterable, Mapping, TypedDict, cast

from core.roblox_types import RbxScript
from core.unity_types import GuidIndex

from converter.scene_runtime_planner import (
    SceneRuntimeArtifact,
    SceneRuntimeDisplacedInstance,
    SceneRuntimeDomainSignals,
    SceneRuntimeInstance,
    SceneRuntimeModule,
    SceneRuntimePrefab,
    SceneRuntimeReference,
    SceneRuntimeScene,
)
from converter.scene_runtime_topology.cross_domain_edges import (
    CrossDomainEdge,
)
from converter.scene_runtime_topology.shared_flag_channels import (
    SharedFlagChannels,
)
from converter.storage_classifier import (
    REPLICATED_STORAGE,
    SERVER_SCRIPT_SERVICE,
    SERVER_STORAGE,
)


# Both ServerStorage and ServerScriptService are invisible to the client,
# so a client-reachable helper parked in either container has the same
# "client cannot require" problem and gets hoisted to ReplicatedStorage.
# Pre-P1.1 the reachability pass only checked ServerStorage; helpers in
# ServerScriptService stayed there and silently broke the client require
# graph at runtime.
_SERVER_CONTAINERS_FOR_REACHABILITY: frozenset[str] = frozenset({
    SERVER_STORAGE,
    SERVER_SCRIPT_SERVICE,
})

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Networking mode
# ---------------------------------------------------------------------------

# Valid values for the ``--networking`` CLI flag.
# - ``"none"``: single-player Unity ports. Default fallback = client.
# - ``"mirror"``: Mirror-using networked Unity games. Mirror-only signals
#     (e.g. ``[ServerRpc]``, ``[SyncVar]``) fire. Default fallback = server.
# - ``"netcode"``: Unity.Netcode-using networked games. Same fallback
#     behaviour as Mirror (server-authoritative).
NetworkingMode = str  # "none" | "mirror" | "netcode"
NETWORKING_MODES: tuple[str, ...] = ("none", "mirror", "netcode")
DEFAULT_NETWORKING_MODE: str = "none"


# ---------------------------------------------------------------------------
# Luau signal tables (kept from PR3b; back-compat name retained for the
# legacy-tables-byte-frozen tests in test_scene_runtime_domain.py).
# ---------------------------------------------------------------------------

_GENERIC_CLIENT_API_PATTERNS: tuple[str, ...] = (
    # Local-player handles
    r"Players\.LocalPlayer\b",
    r'GetService\(\s*["\']Players["\']\s*\)\.LocalPlayer\b',
    r"\bLocalPlayer\.Character\b",
    r"\.PlayerGui\b",
    # Input
    r'GetService\(\s*["\']UserInputService["\']\s*\)',
    r"\bUserInputService\b",
    r'GetService\(\s*["\']ContextActionService["\']\s*\)',
    r'GetService\(\s*["\']GuiService["\']\s*\)',
    # Camera + render loop (client-only RunService signals)
    r"workspace\.CurrentCamera\b",
    r"game\.Workspace\.CurrentCamera\b",
    r"\bRenderStepped\b",
    r"\bBindToRenderStep\b",
    r"\bIsClient\(\)",
    # UI roots
    r'GetService\(\s*["\']StarterGui["\']\s*\)',
    r"\bStarterGui\b",
    # Network: client outbound + client inbound
    r":FireServer\(",
    r":InvokeServer\(",
    r"\.OnClientEvent\b",
    r"\.OnClientInvoke\b",
    # Mouse handles
    r"\bmouse\.Hit\b",
    r"\bmouse\.Target\b",
)

_GENERIC_SERVER_API_PATTERNS: tuple[str, ...] = (
    # Network: server-side dispatch
    r"\.OnServerEvent\b",
    r"\.OnServerInvoke\b",
    r":FireClient\(",
    r":FireAllClients\(",
    r":InvokeClient\(",
    # Server-only services
    r'GetService\(\s*["\']DataStoreService["\']\s*\)',
    r'GetService\(\s*["\']MessagingService["\']\s*\)',
    r'GetService\(\s*["\']ServerStorage["\']\s*\)',
    r'GetService\(\s*["\']ServerScriptService["\']\s*\)',
    r"\bIsServer\(\)",
)

# Compiled once at module level.
_CLIENT_RX = tuple(re.compile(p) for p in _GENERIC_CLIENT_API_PATTERNS)
_SERVER_RX = tuple(re.compile(p) for p in _GENERIC_SERVER_API_PATTERNS)


def _long_bracket_open(source: str, i: int) -> int | None:
    """If ``source[i:]`` opens a Luau long bracket ``[[`` / ``[=[`` / ``[==[`` …,
    return the ``=`` level; else None. ``i`` must point at the first ``[``."""
    if i >= len(source) or source[i] != "[":
        return None
    j = i + 1
    while j < len(source) and source[j] == "=":
        j += 1
    if j < len(source) and source[j] == "[":
        return j - (i + 1)  # number of ``=`` between the brackets
    return None


def _skip_long_bracket(source: str, i: int, level: int) -> int:
    """Return the index just past the closing ``]=*]`` of the long bracket that
    opens at ``i`` with ``level`` equals signs (unterminated → end of source)."""
    close = "]" + "=" * level + "]"
    end = source.find(close, i + level + 2)
    return len(source) if end == -1 else end + len(close)


def _strip_luau_noise(source: str) -> str:
    """Replace Luau COMMENTS and LONG-BRACKET strings with spaces before the
    domain-signal scan, KEEPING short quoted strings (the API patterns key off
    their string args, e.g. ``GetService("ServerStorage")``).

    A single lexical pass — so a ``--`` or paren inside a quoted string isn't
    mistaken for a comment/grouping, and a ``require(`` / API token inside a
    comment or ``[[..]]`` string is removed rather than scanned or (worse)
    consumed across (codex review: the scan must not fire on, or
    ``_strip_require_calls`` over-consume across, commented/long-bracket
    regions)."""
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        c = source[i]
        if c == '"' or c == "'":  # quoted string — keep verbatim
            out.append(c)
            i += 1
            while i < n:
                d = source[i]
                out.append(d)
                i += 1
                if d == "\\" and i < n:
                    out.append(source[i])
                    i += 1
                elif d == c:
                    break
            continue
        if c == "[":  # possible long-bracket string
            level = _long_bracket_open(source, i)
            if level is not None:
                i = _skip_long_bracket(source, i, level)
                out.append(" ")
                continue
        if c == "-" and i + 1 < n and source[i + 1] == "-":  # comment
            level = _long_bracket_open(source, i + 2)
            if level is not None:  # long comment --[[ ]]
                i = _skip_long_bracket(source, i + 2, level)
            else:  # line comment -- ...
                while i < n and source[i] != "\n":
                    i += 1
            out.append(" ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _string_content_mask(text: str) -> list[bool]:
    """Mark every character that lies INSIDE a quoted-string literal's content
    (between the delimiters, escapes included) as ``True``; code positions and
    the quote delimiters themselves are ``False``.

    Callers pass text that has already had comments + long-bracket strings
    removed (``_strip_luau_noise``); only short ``"..."`` / ``'...'`` strings
    remain. The mask lets the API scan count a pattern only when its match
    STARTS in code — so a literal like ``"x.OnServerEvent"`` or
    ``'GetService("ServerStorage")'`` is data, not a signal, while a real
    ``GetService("ServerStorage")`` CALL (which starts at the ``G`` in code,
    its string arg merely nested) still fires."""
    mask = [False] * len(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"' or c == "'":
            i += 1
            while i < n:
                d = text[i]
                if d == "\\" and i + 1 < n:
                    mask[i] = True
                    mask[i + 1] = True
                    i += 2
                    continue
                if d == c:
                    break
                mask[i] = True
                i += 1
            i += 1  # past the closing delimiter
            continue
        i += 1
    return mask


def _api_pattern_fires(rx: "re.Pattern[str]", text: str, in_string: list[bool]) -> bool:
    """True iff ``rx`` matches ``text`` with the match starting in CODE (not
    inside a string literal). Token-aware replacement for a bare
    ``rx.search(text)`` so string CONTENTS never manufacture an API signal."""
    return any(not in_string[m.start()] for m in rx.finditer(text))


def _strip_require_calls(source: str) -> str:
    """Blank out ``require(...)`` call expressions before the Luau domain-signal
    scan.

    A ``require`` argument is a MODULE-RESOLUTION path, not domain logic — and
    the generic transpiler routinely emits a defensive
    ``require(RS:FindFirstChild("X") or game:GetService("ServerStorage")
    :FindFirstChild("X"))`` fallback for a cross-script require. Scanning that
    fallback for API signals is a category error: the ``GetService("ServerStorage")``
    in it would otherwise count as a STRONG server signal and fail-close an
    obvious client module (e.g. ``HudControl``, a UI HUD) to ``excluded`` — which
    in generic mode is a dead emit (the boot loop never constructs it). Strips
    for BOTH channels (a require path is never client- OR server-domain evidence).
    Balanced-paren aware so a nested ``GetService(...)`` inside the args is
    consumed with it. Matches only a STANDALONE ``require`` (not ``myRequire(``
    / ``x.require(``), and on an unterminated ``require(`` keeps the remainder
    verbatim rather than blanking every downstream signal.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        # Skip quoted strings verbatim: a ``require(`` (or its args) that lives
        # INSIDE a string literal is data, not a call (codex review: a literal
        # ``"require(" .. game:GetService("ServerStorage")`` must not strip the
        # real GetService). Comments / long brackets are already gone via
        # _strip_luau_noise, so only short strings remain to guard here.
        if c == '"' or c == "'":
            out.append(c)
            i += 1
            while i < n:
                d = source[i]
                out.append(d)
                i += 1
                if d == "\\" and i < n:
                    out.append(source[i])
                    i += 1
                elif d == c:
                    break
            continue
        # Standalone ``require`` identifier (not ``myRequire`` / ``x.require`` /
        # ``x:require``), optional whitespace, then ``(``.
        if (
            c == "r"
            and source.startswith("require", i)
            and (i == 0 or not (source[i - 1].isalnum() or source[i - 1] in "_.:"))
        ):
            k = i + len("require")
            while k < n and source[k].isspace():  # any whitespace, incl. newline
                k += 1
            if k < n and source[k] == "(":
                depth = 0
                j = k
                closed = False
                in_str: str | None = None
                escaped = False
                while j < n:
                    d = source[j]
                    if in_str is not None:
                        if escaped:
                            escaped = False
                        elif d == "\\":
                            escaped = True
                        elif d == in_str:
                            in_str = None
                    elif d in ("'", '"'):
                        in_str = d
                    elif d == "(":
                        depth += 1
                    elif d == ")":
                        depth -= 1
                        if depth == 0:
                            j += 1
                            closed = True
                            break
                    j += 1
                if not closed:
                    # Unterminated require(...): keep the tail verbatim so
                    # downstream signals survive.
                    out.append(source[i:])
                    return "".join(out)
                i = j  # drop the whole require(...) span
                continue
        out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# C# signal tables (v2 classifier).
#
# Patterns operate on the raw C# source text. Each row is
# (regex, signal_name, mirror_only). Mirror-only rows fire only when
# ``networking`` is ``"mirror"`` or ``"netcode"``; under ``"none"`` they
# are ignored.
# ---------------------------------------------------------------------------

_CSharpPattern = tuple[str, str, bool]


# --- C# `using` import regex helpers ---
#
# A "using" line in C# can take several shapes the design doc all treats
# as "this script imports namespace N":
#   - ``using N;``                 -- ordinary import.
#   - ``using static N.Member;``   -- static import of a member of N.
#   - ``using Alias = N;``         -- alias for N (or any descendant).
#   - ``using Alias = N.Sub;``     -- alias for a sub-namespace.
# The codex P1 finding noted the old matcher only accepted form 1. We
# expand to all four. The helper composes the alternation so each
# namespace target only has to list its dotted prefix once.

def _using_rx(namespace: str) -> str:
    """Return a regex (string) matching any of the four C# ``using``
    forms against ``namespace`` (a dotted name, e.g. ``"UnityEngine.UI"``).

    All four forms must:
      - sit at start of a (multiline) line modulo whitespace,
      - end with a ``;``,
    """
    ns = re.escape(namespace)
    return (
        r"^\s*using\s+(?:"
        # plain or static import: optional `static`, then namespace,
        # optional `.Anything`, then `;`
        rf"(?:static\s+)?{ns}(?:\.[\w.]+)?"
        r"|"
        # alias import: `Alias = namespace(.Anything)?`
        rf"[A-Za-z_]\w*\s*=\s*{ns}(?:\.[\w.]+)?"
        r")\s*;"
    )


_CS_STRONG_CLIENT: tuple[_CSharpPattern, ...] = (
    # Mirror / Netcode client-side annotations.
    (r"\[ClientRpc\b", "ClientRpc", True),
    (r"\[Client\b\]", "ClientAttribute", True),
    (r"\[ClientCallback\b", "ClientCallback", True),
    # Unity UI namespace imports. The four `using` forms (plain, static,
    # alias, alias-to-subspace) all count; see ``_using_rx``.
    (_using_rx("UnityEngine.UI"), "using_UnityEngine_UI", False),
    (_using_rx("TMPro"), "using_TMPro", False),
    (_using_rx("UnityEngine.EventSystems"),
     "using_UnityEngine_EventSystems", False),
    # Input. Anchor to start-of-expression so `someClass.Input.GetKey(...)`
    # does NOT fire (the receiver `Input` is a Unity static type, not an
    # instance member; `\b` alone permits a leading `.` member-access).
    (r"(?<![.\w])Input\.(?:Get[A-Z]\w*|mousePosition|mouseScrollDelta|"
     r"touchCount|GetTouch|anyKey\b|anyKeyDown\b)",
     "Input_Get", False),
    # OnGUI methods (UI immediate-mode rendering — client-only).
    (r"\bvoid\s+OnGUI\s*\(\s*\)", "OnGUI_method", False),
    # PlayerPrefs — same start-of-expression anchor as Input.
    (r"(?<![.\w])PlayerPrefs\.\w+\s*\(", "PlayerPrefs", False),
    # Cursor / Screen / Application.platform — client-only Unity APIs.
    # Same anchor: a user type called Cursor / Screen / Application via
    # member access must not fire.
    (r"(?<![.\w])Cursor\.(?:visible|lockState|SetCursor)\b", "Cursor_API", False),
    (r"(?<![.\w])Screen\.(?:width|height|fullScreen|orientation|"
     r"currentResolution|resolutions)\b",
     "Screen_API", False),
    (r"(?<![.\w])Application\.platform\b", "Application_platform", False),
    # [SerializeField] field types pointing at UI components. Match on
    # the field type after [SerializeField] but before the field name.
    # We approximate with a lookbehind-free pattern: any [SerializeField]
    # whose next non-attribute token names a known UI type. Robust enough
    # for asset-store-style C# (SerializeField on its own line or inline
    # with the type).
    (r"\[SerializeField\][^;{}]*?\b"
     r"(?:Text|Image|RawImage|Slider|Button|RectTransform|"
     r"TMP_Text|TextMeshProUGUI|TextMeshPro|CanvasGroup|"
     r"Canvas|ScrollRect|Toggle|Dropdown|InputField|TMP_InputField)\b",
     "SerializeField_UI_type", False),
)


_CS_MODERATE_CLIENT: tuple[_CSharpPattern, ...] = (
    # Camera.main — per-player camera. Server scripts can read it but the
    # idiom is overwhelmingly client. Start-of-expression anchor: a user
    # type called Camera reached via member access must not fire.
    (r"(?<![.\w])Camera\.main\b", "Camera_main", False),
    # Animator playback APIs — see design doc rationale (moderate).
    (r"\bAnimator\b[^;]*\.(?:SetBool|SetFloat|SetInteger|SetTrigger|"
     r"CrossFade|Play|ResetTrigger)\s*\(",
     "Animator_playback", False),
    # GetComponent<Animator>().Play(...) variant
    (r"GetComponent<\s*Animator\s*>\(\)\.(?:SetBool|SetFloat|SetInteger|"
     r"SetTrigger|CrossFade|Play|ResetTrigger)\s*\(",
     "Animator_playback_GetComponent", False),
)


_CS_STRONG_SERVER: tuple[_CSharpPattern, ...] = (
    # Mirror-only annotations.
    (r"\[ServerRpc\b", "ServerRpc", True),
    (r"\[Server\b\]", "ServerAttribute", True),
    (r"\[ServerCallback\b", "ServerCallback", True),
    (r"\[Command\b", "Command", True),  # Mirror legacy alias
    (r"\[SyncVar\b", "SyncVar", True),
    # NetworkBehaviour subclass (Mirror / Netcode).
    (r":\s*(?:NetworkBehaviour|Mirror\.NetworkBehaviour|"
     r"Unity\.Netcode\.NetworkBehaviour)\b",
     "NetworkBehaviour_subclass", True),
)


# Moderate server signals come in two flavours:
#   1. Regex-tabled (none today; reserved for future expansion).
#   2. Graph-derived: stamped via ``moderate_server_extra`` injected into
#      ``_collect_signals``. The require-graph-reaches-NetworkBehaviour
#      signal (``network_behaviour_reachable``) is graph-derived because
#      it depends on cross-module data the per-module pass can't see.
_CS_MODERATE_SERVER: tuple[_CSharpPattern, ...] = ()


# Name of the graph-derived moderate-server signal. Kept as a constant so
# the planner-side stamping pass and the mirror_adoption_low annotated-
# kind list can both reference it without string drift.
_NETWORK_BEHAVIOUR_REACHABLE = "network_behaviour_reachable"


def _compile_cs_table(
    patterns: tuple[_CSharpPattern, ...],
) -> tuple[tuple[re.Pattern[str], str, bool], ...]:
    return tuple(
        (re.compile(p, re.MULTILINE), name, mirror_only)
        for p, name, mirror_only in patterns
    )


_CS_STRONG_CLIENT_RX = _compile_cs_table(_CS_STRONG_CLIENT)
_CS_MODERATE_CLIENT_RX = _compile_cs_table(_CS_MODERATE_CLIENT)
_CS_STRONG_SERVER_RX = _compile_cs_table(_CS_STRONG_SERVER)
_CS_MODERATE_SERVER_RX = _compile_cs_table(_CS_MODERATE_SERVER)


# NetworkBehaviour detection: any class declaration that names
# NetworkBehaviour (bare, Mirror-qualified, or Unity.Netcode-qualified)
# in its base list. Match the colon + base list up through end of
# class header (open brace) so we tolerate
#     ``class Foo : NetworkBehaviour, IFoo {``
# and
#     ``class Foo : IBar, NetworkBehaviour {``
# This is the planner-side detector for P1.3 (require graph reaches
# NetworkBehaviour). Compiled once.
_RE_NETWORK_BEHAVIOUR_CLASS = re.compile(
    r"^\s*(?:public\s+|internal\s+|private\s+|protected\s+|"
    r"abstract\s+|sealed\s+|static\s+|partial\s+|unsafe\s+)*"
    r"class\s+[A-Za-z_]\w*"  # class name
    r"(?:\s*<[^>]*>)?"  # optional generic parameter list
    r"\s*:\s*[^{]*?"  # colon + base list (anything up to opening brace)
    r"\b(?:NetworkBehaviour|Mirror\.NetworkBehaviour|"
    r"Unity\.Netcode\.NetworkBehaviour)\b",
    re.MULTILINE,
)


# Project-level Mirror/Netcode `using` import check for the
# `mirror_adoption_low` heuristic. Same expanded-form coverage as
# the per-module using patterns (plain / static / alias).
_RE_USING_MIRROR = re.compile(
    r"^\s*using\s+(?:"
    r"(?:static\s+)?(?:Mirror|Unity\.Netcode)(?:\.[\w.]+)?"
    r"|"
    r"[A-Za-z_]\w*\s*=\s*(?:Mirror|Unity\.Netcode)(?:\.[\w.]+)?"
    r")\s*;",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# C# source pre-scrubber.
#
# Codex P1 finding: raw regex over the C# text false-positives on
# commented or stringified API calls (``// using UnityEngine.UI;``,
# ``"Input.GetKey"``). We strip the noisy spans BEFORE regex matching.
#
# We don't aim for a real lexer — that would require tracking
# preprocessor directives, escape handling for interpolated strings'
# embedded expressions, etc. Instead, we replace the contents of
# comments and string/char literals with spaces. Newlines are
# preserved so multi-line tracking by ``re.MULTILINE`` stays aligned
# with source line numbers (useful for any future signal-location
# reporting and avoids accidentally splicing tokens across lines).
# ---------------------------------------------------------------------------

def _strip_cs_noise(src: str, *, preserve_strings: bool = False) -> str:
    """Replace comments (always) and string + char literals (unless
    ``preserve_strings``) in ``src`` with spaces.

    Returns a same-length string (modulo trailing-NL invariants) so
    regex line/column reporting still maps roughly back to original
    source. Newlines are preserved verbatim so ``re.MULTILINE`` anchors
    line up with the original.

    ``preserve_strings=True`` blanks ONLY comments — string + char
    literals are emitted VERBATIM (``src[start:i]`` unchanged). Used by
    the Animator-write parser (``animation_driver_analyzer``), whose
    target param names live ONLY as string literals (e.g.
    ``SetBool("open", …)``). Comment stripping is still wanted so a
    ``// doorAnim.SetBool("open")`` comment can't match. The default
    (``False``) preserves every existing caller's behavior (back-compat).

    Handles:
      - ``// line comments`` (terminated by newline).
      - ``/* block comments */`` (terminated by ``*/``).
      - ``"strings"`` with ``\\`` escapes (single line).
      - ``@"verbatim strings"`` (multi-line; ``""`` is an escaped quote).
      - ``$"interpolated strings"`` (treated like regular strings;
        embedded ``{...}`` content is NOT preserved — design tradeoff:
        a Mirror RPC name interpolated into a log message would no
        longer fire, but a stringified ``"Input.GetKey"`` won't either).
      - ``'c'`` character literals.

    NOT handled (acceptable for grep-style signal extraction):
      - ``#if`` / ``#endif`` preprocessor regions (we don't pre-process).
      - Raw string literals (``$@""...""``) — exotic.
      - Identifiers prefixed with ``@`` (e.g. ``@class`` to escape the
        keyword). The leading ``@`` doesn't change matching.
    """
    if not src:
        return src
    out: list[str] = []
    n = len(src)
    i = 0
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        # Line comment
        if c == "/" and nxt == "/":
            # Scrub until end of line; keep the newline.
            j = src.find("\n", i + 2)
            if j == -1:
                j = n
            out.append(" " * (j - i))
            i = j
            continue
        # Block comment
        if c == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            if j == -1:
                # Unterminated block comment: scrub to end.
                # Preserve newlines.
                for ch in src[i:]:
                    out.append("\n" if ch == "\n" else " ")
                i = n
                continue
            # Scrub i..j+2, preserving newlines so re.MULTILINE lines up.
            for ch in src[i:j + 2]:
                out.append("\n" if ch == "\n" else " ")
            i = j + 2
            continue
        # Verbatim string: @"..." (and interpolated-verbatim $@"...").
        if (
            c == "@"
            and nxt == "\""
        ) or (
            c == "$" and i + 2 < n and src[i + 1] == "@" and src[i + 2] == "\""
        ):
            # Opening quote position.
            start = i
            i = i + 2 if c == "@" else i + 3
            # Scan for closing `"` that isn't `""`.
            while i < n:
                if src[i] == "\"" and src[i + 1:i + 2] == "\"":
                    i += 2
                    continue
                if src[i] == "\"":
                    i += 1
                    break
                i += 1
            if preserve_strings:
                out.append(src[start:i])
            else:
                for ch in src[start:i]:
                    out.append("\n" if ch == "\n" else " ")
            continue
        # Regular string or interpolated string: "..." or $"..."
        if c == "\"" or (c == "$" and nxt == "\""):
            start = i
            i = i + 1 if c == "\"" else i + 2
            while i < n:
                ch = src[i]
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == "\"":
                    i += 1
                    break
                if ch == "\n":
                    # Unterminated string: bail out at end-of-line.
                    break
                i += 1
            if preserve_strings:
                out.append(src[start:i])
            else:
                for sch in src[start:i]:
                    out.append("\n" if sch == "\n" else " ")
            continue
        # Char literal: '...' with `\` escapes.
        if c == "'":
            start = i
            i += 1
            while i < n:
                ch = src[i]
                if ch == "\\" and i + 1 < n:
                    i += 2
                    continue
                if ch == "'":
                    i += 1
                    break
                if ch == "\n":
                    break
                i += 1
            if preserve_strings:
                out.append(src[start:i])
            else:
                for cch in src[start:i]:
                    out.append("\n" if cch == "\n" else " ")
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Report payload
# ---------------------------------------------------------------------------

class DomainClassifierReport(TypedDict):
    """Side-channel output of ``classify_scene_runtime_domains``.

    Surfaced onto ``scene_runtime`` in ``_classify_storage`` and read by
    the conversion-report writer.

    - ``displaced_instances`` -- instances disagreeing with their class's
      final domain (operator-pinned conflicts).
    - ``low_confidence_modules`` -- script_ids stamped low_confidence
      (zero-signal fallback). Operator may want to pin via
      ``domain_overrides``.
    - ``excluded_modules`` -- script_ids the classifier kicked to
      ``"excluded"`` (Rule-1, Rule-4, reachability, override-rejected).
    - ``fail_closed_modules`` -- legacy alias for ``excluded_modules``.
      Retained one release for downstream consumers; the conversion
      report writes ``excluded_modules``.
    - ``mirror_adoption_low`` -- present + True when ``--networking=
      mirror|netcode`` was declared but the project's netcode-annotation
      density falls below the heuristic threshold.
    - ``strict_violations`` -- per the design doc §"Strict mode": modules
      that would block transpile under ``--strict-classification`` (i.e.,
      any low_confidence / excluded module after override application).
    """

    displaced_instances: list[SceneRuntimeDisplacedInstance]
    low_confidence_modules: list[str]
    excluded_modules: list[str]
    fail_closed_modules: list[str]
    mirror_adoption_low: bool
    strict_violations: list[str]


# ---------------------------------------------------------------------------
# Slice 6 early prepass: domain inference + reachability requirements.
#
# These two functions are PURE OVER THEIR INPUTS — no ``parent_path``
# reads, no mutation of ``scene_runtime``. They can run BEFORE
# ``classify_storage`` so storage decisions can consult their output.
# The legacy entry point ``classify_scene_runtime_domains`` is rebuilt
# on top of these (plus ``finalize_topology_containers``) and is
# byte-identical in observable behavior.
# ---------------------------------------------------------------------------


class _DomainInferenceResult(TypedDict):
    """Per-module output of ``infer_module_domains``.

    Carries everything the late finalizer needs to mirror onto the
    module row — domain verdict, signals, and the displaced/excluded
    bookkeeping the legacy pipeline accumulates. Defined as a TypedDict
    rather than a tuple so the keys stay self-documenting where slice
    7's storage decision tree will read them.
    """

    domain: str
    signals: SceneRuntimeDomainSignals
    displaced_instances: list[SceneRuntimeDisplacedInstance]
    low_confidence: bool
    excluded: bool


class TopologyInputs(TypedDict):
    """Output of the slice-6 prepass; input to ``classify_storage``'s
    topology-driven branch (slice 7).

    Composed by ``Pipeline._maybe_run_topology_prepass`` immediately
    BEFORE ``classify_storage`` runs. Carries everything slice 7's
    ``_decide_script_container_from_topology`` will read — per-module
    domain verdict, reachability requirement, and the lookup indices
    that map ``RbxScript.name`` ↔ ``script_id`` and ``script_id`` →
    caller list. All entries are derived purely from
    ``scene_runtime`` + the planner's ``dependency_map`` — NO
    ``parent_path`` reads.

    Phase 2b carries the cross-domain facts on this dict:
    ``cross_domain_edges`` (Class 1, component-ref) +
    ``shared_flag_channels`` (Class 2, dynamic shared-flag funnel). They
    are produced in ``_maybe_run_topology_prepass`` so the post-transpile
    reader scan / enrichment that backs them runs in the same scope as
    the prepass (with ``state.transpilation_result``, ``domains``,
    ``script_id_by_name`` all in hand). ``build_topology`` becomes
    pure-assembly: it reads these fields off ``TopologyInputs`` and
    writes them straight into the artifact dict; the producers are no
    longer called from inside the assembler.

    **Phase 2b reframe (2026-06-01).** The empirical whole-plan review
    split cross-domain authority into two bridge classes. The slices-1-2
    ``cross_domain_edge_candidates`` bucket (the
    ``compute_shared_attribute_candidates`` fan-out) mis-modeled the
    dynamic shared-flag class AS the static component-ref class and was
    RETIRED. It is replaced by ``shared_flag_channels`` — the channel
    fact (read-name set + reader domains + canonical store + present
    gate). See ``shared_flag_channels.py``.

    The fields hold:
      - ``cross_domain_edges``: fully-resolved component-ref (Class 1)
        edges with ``from_*`` AND ``to_*`` populated.
        ``bridge_member_scripts`` carries caller / listener /
        ``anim_listener`` rows (``edge_enrichment`` fills these).
      - ``shared_flag_channels``: the ``PlayerSetSharedFlag`` channel
        record (Class 2). Recomputed from the live reader scan every run.

    Threaded through ``classify_storage`` as a no-op-on-default kwarg;
    NOT persisted (slice 6 rule: save raw facts, never persist derived
    conclusions). Always recomputed from current operator inputs via
    ``Pipeline._maybe_run_topology_prepass`` on every invocation,
    including ``--phase=write_output`` resumes (the prepass is in
    ``ESSENTIAL_PHASES``). The Phase 2b cross-domain fields are likewise
    recomputed every run from ``scene_runtime`` + the producer functions
    + the live ``transpilation_result``, per the slice-6 "save raw facts,
    recompute conclusions" rule (the ``caller_graph``-style recompute
    pattern; there is no preserve path for them).
    """

    # ``script_id`` -> domain verdict from ``infer_module_domains``.
    domains: dict[str, str]
    # ``script_id`` -> required container ("ReplicatedStorage" or
    # "__excluded__"). Helpers not listed are unconstrained.
    reachability_requirements: dict[str, str]
    # ``script_id`` -> ``lifecycle_role`` (planner-derived; available
    # from the module row). Empty under legacy mode.
    lifecycle_roles: dict[str, str]
    # ``RbxScript.name`` -> ``script_id``. Built via the canonical
    # ``build_script_id_by_name`` helper -- honors the
    # degraded-service contract on colliding class_names + stems.
    script_id_by_name: dict[str, str]
    # ``script_id`` -> list of caller ``script_id``s. Built from the
    # planner's ``dependency_map`` via the canonical
    # ``_resolve_caller_graph`` helper in ``build_topology.py``.
    caller_graph: dict[str, list[str]]
    # Phase 2a slice 7 -- raw fact: did the transpile phase run on this
    # pipeline invocation? Sourced from
    # ``state.transpilation_result is not None`` in
    # ``Pipeline._maybe_run_topology_prepass``. NOT a derived
    # conclusion; lets slice 7's ``_decide_script_container_from_topology``
    # distinguish two structurally-identical "empty
    # ``reachability_requirements``" cases:
    #   * ``transpile_ran is False`` (assemble-no-retranspile resume):
    #     empty is expected because ``dependency_map`` is empty, so
    #     ``derive_reachability_requirements`` returns ``{}``. Per the
    #     slice-6 "save raw facts, recompute conclusions" rule the
    #     consumer falls back to the legacy six-rule path PER-SCRIPT
    #     for modules not covered by topology (the unconstrained-helper
    #     fallback). Byte-identical to slice-5 behavior on resume.
    #   * ``transpile_ran is True``: empty
    #     ``reachability_requirements[sid]`` means the analysis genuinely
    #     produced no constraint -- the consumer proceeds through the
    #     topology decision tree (helper is unconstrained, fall through
    #     to ModuleScript caller-domain routing).
    # See ``scene-runtime-architecture-ir.md`` §"TopologyInputs shape
    # -- transpile_ran" and §"Unconstrained-helper fallback contract".
    transpile_ran: bool
    # Phase 2b (Class 1): produced + enriched by
    # ``Pipeline._maybe_run_topology_prepass``. Fully-resolved
    # component-ref cross-domain edges (one row per peer-MonoBehaviour
    # serialized reference whose endpoints sit in different runtime
    # domains). ``build_topology`` reads from here and writes straight
    # into the artifact -- it no longer calls the producer itself.
    # ``bridge_member_scripts`` populated by ``edge_enrichment``.
    cross_domain_edges: list[CrossDomainEdge]
    # Phase 2b (Class 2): the ``PlayerSetSharedFlag`` funnel channel
    # fact, computed by ``compute_shared_flag_channels`` from the live
    # reader scan. Records the literal flag names read cross-domain, the
    # reader domains, the constant canonical store, and the ``present``
    # gate. Recompute-only (``caller_graph``-style; no preserve path);
    # fails open on a no-transpile resume (``present: True`` with empty
    # ``read_names``). Replaces the retired
    # ``cross_domain_edge_candidates`` bucket. See
    # ``shared_flag_channels.py``.
    shared_flag_channels: SharedFlagChannels


def infer_module_domains(
    scene_runtime: SceneRuntimeArtifact,
    scripts: Iterable[RbxScript],
    *,
    dependency_map: dict[str, list[str]] | None = None,
    guid_index: GuidIndex | None = None,
    networking: str = DEFAULT_NETWORKING_MODE,
) -> dict[str, _DomainInferenceResult]:
    """Return ``{script_id: _DomainInferenceResult}`` for every module row.

    Pure over its inputs:
      - reads ``scene_runtime.modules`` / ``.scenes`` / ``.prefabs`` /
        ``.domain_overrides`` for signal collection ONLY.
      - reads ``RbxScript.source`` (Luau post-transpile body) through
        ``scripts_by_class``.
      - reads C# source via ``guid_index`` when present.
      - does NOT read ``RbxScript.parent_path``. (Belt-and-suspenders:
        the ``test_module_domain_no_parent_path_in_inference`` AST
        check asserts no ``parent_path`` token appears in any function
        reachable from here.)
      - does NOT mutate ``scene_runtime`` or any ``RbxScript``.

    The result is consumed by:
      - ``derive_reachability_requirements`` (early, immediately after).
      - ``finalize_topology_containers`` (late, after ``classify_storage``).
      - slice 7's ``_decide_script_container`` rewrite (storage decisions
        consult ``domain`` directly instead of re-running the API regex).

    Helper / non-runtime-bearing rows produce a ``"helper"`` verdict
    with empty signals (matching what ``_stamp_container_and_path``
    would have stamped for them in the legacy single-pass path).
    """
    if networking not in NETWORKING_MODES:
        raise ValueError(
            f"unknown networking mode {networking!r}; "
            f"expected one of {NETWORKING_MODES}"
        )

    modules = scene_runtime.get("modules", {})
    scenes = scene_runtime.get("scenes", {})
    prefabs = scene_runtime.get("prefabs", {})
    overrides = scene_runtime.get("domain_overrides", {})

    from converter.scene_runtime_planner import (
        build_scripts_by_class_name,
    )
    scripts_list = list(scripts)
    scripts_by_class = build_scripts_by_class_name(
        scripts_list, cast("dict", modules),
    )

    per_instance_evidence = _gather_per_instance_evidence(scenes, prefabs)

    cs_source_cache: dict[str, str] = {}

    def _get_cs_source(script_id: str) -> str:
        if script_id in cs_source_cache:
            return cs_source_cache[script_id]
        text = _load_cs_source(script_id, guid_index)
        cs_source_cache[script_id] = text
        return text

    network_reachable: set[str] = _compute_network_behaviour_reachable(
        modules, dependency_map, _get_cs_source, networking,
    )

    out: dict[str, _DomainInferenceResult] = {}
    for script_id, module in modules.items():
        if not module.get("runtime_bearing"):
            # Pre-stamp helpers + non-runtime-bearing rows. The finalizer
            # mirrors these onto the module row plus the (post-classify_storage)
            # container/module_path.
            out[script_id] = _DomainInferenceResult(
                domain="helper",
                signals=cast(SceneRuntimeDomainSignals, {}),
                displaced_instances=[],
                low_confidence=False,
                excluded=False,
            )
            continue

        extra_moderate_server: tuple[str, ...] = (
            (_NETWORK_BEHAVIOUR_REACHABLE,)
            if script_id in network_reachable
            else ()
        )

        verdict, signals, instance_rows = _classify_module(
            script_id, module, scripts_by_class,
            per_instance_evidence.get(script_id, []),
            overrides.get(script_id),
            _get_cs_source(script_id),
            networking,
            extra_moderate_server=extra_moderate_server,
        )
        low_conf = bool(signals.get("low_confidence"))
        excluded = verdict == "excluded"
        displaced: list[SceneRuntimeDisplacedInstance] = []
        if signals.get("override_applied") and signals.get("intra_class_conflict"):
            displaced = list(instance_rows)

        out[script_id] = _DomainInferenceResult(
            domain=verdict,
            signals=signals,
            displaced_instances=displaced,
            low_confidence=low_conf,
            excluded=excluded,
        )

    return out


def _client_entry_seed_names(
    script_by_sid: dict[str, RbxScript],
    domain_results: dict[str, _DomainInferenceResult],
    lifecycle_roles: dict[str, str],
) -> set[str]:
    """Return the set of client-entry script NAMES.

    A script is a client ENTRY POINT (the seed of the client
    require-reachability closure) when ANY of:
      - its intrinsic class is ``LocalScript`` (a structural client
        entry that auto-runs on the client — definitively client by
        type, so EXEMPT from the low_confidence gate), OR
      - its gated lifecycle role is ``character_attached`` or ``loader``
        AND its inferred domain verdict is NOT low-confidence. The roles
        only fire for ``script_class in {Script, LocalScript}`` AND
        ``domain == "client"`` (the gate is applied where the role is
        computed, ``lifecycle_roles.py``), but ``derive_module_lifecycle_role``
        returns ``"loader"`` for a plain ``Script`` whenever the inferred
        domain is ``client`` — and a zero-signal SERVER ``Script`` named
        ``Bootstrap``/``Loader`` (``is_loader`` is a broad NAME regex) is
        inferred ``client + low_confidence=True``. Without the
        ``low_confidence is False`` gate such a row would seed and leak
        its server-only require subtree into ReplicatedStorage, OR
      - its intrinsic class is ``Script`` AND its inferred domain is
        ``client`` AND that verdict is NOT low-confidence (the
        ``low_confidence is False`` clause keeps a zero-signal
        ``networking=none`` client default from seeding a server-only
        require subtree into the client closure).

    Net rule: intrinsic ``LocalScript`` seeds unconditionally; otherwise
    a script seeds only when ``low_confidence is False`` AND (role in
    ``{loader, character_attached}`` OR (intrinsic ``Script`` AND
    ``domain == "client"``)).

    Pure: reads only the intrinsic script class, the domain verdict +
    low_confidence flag, and the (already-gated) lifecycle role. Does
    NOT read ``parent_path``. Does NOT mutate.
    """
    from converter.scene_runtime_planner import derive_intrinsic_script_class

    seeds: set[str] = set()
    for sid, script in script_by_sid.items():
        if not script.name:
            continue
        cls = derive_intrinsic_script_class(script)
        result = domain_results.get(sid)
        domain = result["domain"] if result is not None else ""
        low_confidence = (
            result["low_confidence"] if result is not None else True
        )
        if cls == "LocalScript":
            seeds.add(script.name)
            continue
        if low_confidence is not False:
            # Every non-intrinsic-LocalScript arm requires a confident
            # verdict — a low-confidence inference can mis-route a
            # server-only subtree into the client closure.
            continue
        if lifecycle_roles.get(sid) in ("character_attached", "loader"):
            seeds.add(script.name)
            continue
        if cls == "Script" and domain == "client":
            seeds.add(script.name)
    return seeds


def derive_reachability_requirements(
    scene_runtime: SceneRuntimeArtifact,
    scripts: Iterable[RbxScript],
    domain_results: dict[str, _DomainInferenceResult],
    *,
    require_edges_by_name: dict[str, set[str]],
    script_by_sid: dict[str, RbxScript],
    lifecycle_roles: dict[str, str],
    transpile_ran: bool = True,
) -> dict[str, str]:
    """Return ``{script_id: required_container}`` for helper MODULES
    that the client require-graph reaches.

    Runs entirely in script-NAME / ``sid`` space: client/server seeds
    are resolved to script names, the closure walks the canonical
    emitted-require graph ``require_edges_by_name`` (name -> {name}),
    and reached names are mapped back to ``script_id`` via the candidate
    loop (which already holds both).

    Pure: reads ``domain_results[*]["domain"]`` + ``["low_confidence"]``,
    reads ``script_by_sid[sid].name`` +
    ``derive_intrinsic_script_class(script_by_sid[sid])``, reads
    ``lifecycle_roles[sid]``. Does NOT read ``parent_path``. Does NOT
    mutate anything.

    Required-container value is either:
      - ``REPLICATED_STORAGE`` -- helper reached by client only; must
        live where the client require-graph can see it.
      - ``"__excluded__"`` -- helper reached by BOTH client and server
        require-graphs. The finalizer routes these to ``"excluded"``
        domain with ``fail_closed_reason="reachability_conflict"``.

    Helpers not in the map are unconstrained — storage_classifier's
    legacy decision tree picks their container.

    Resume gate: on a no-retranspile resume (``transpile_ran is False``)
    or when the emitted-require graph is empty, return ``{}`` — the same
    byte-identical legacy fallback the previous empty-``dependency_map``
    guard produced.
    """
    if not transpile_ran or not require_edges_by_name:
        return {}

    from converter.scene_runtime_planner import derive_intrinsic_script_class

    modules = scene_runtime.get("modules", {})

    domain_by_sid: dict[str, str] = {}
    for sid, result in domain_results.items():
        domain_by_sid[sid] = result["domain"]

    client_seed_names = _client_entry_seed_names(
        script_by_sid, domain_results, lifecycle_roles,
    )
    server_seed_names: set[str] = set()
    for sid, _module in modules.items():
        if domain_by_sid.get(sid) != "server":
            continue
        script = script_by_sid.get(sid)
        if script is None or not script.name:
            continue
        server_seed_names.add(script.name)

    requirements: dict[str, str] = {}
    for sid, _module in modules.items():
        script = script_by_sid.get(sid)
        if script is None:
            continue
        if derive_intrinsic_script_class(script) != "ModuleScript":
            continue
        name = script.name
        reached_by_client = name in _closure(
            client_seed_names - {name}, require_edges_by_name,
        )
        if not reached_by_client:
            continue
        reached_by_server = name in _closure(
            server_seed_names - {name}, require_edges_by_name,
        )
        if reached_by_server:
            requirements[sid] = "__excluded__"
        else:
            requirements[sid] = REPLICATED_STORAGE
    return requirements


def finalize_topology_containers(
    scene_runtime: SceneRuntimeArtifact,
    scripts: Iterable[RbxScript],
    domain_results: dict[str, _DomainInferenceResult],
    reachability_requirements: dict[str, str],
    *,
    script_by_sid: dict[str, RbxScript],
) -> list[str]:
    """Late finalizer: mirror domain verdicts + container/module_path
    onto module rows after ``classify_storage`` has stamped final
    ``parent_path`` values on every ``RbxScript``.

    Mutates ``scene_runtime.modules[*]`` (stamps ``domain`` /
    ``domain_signals`` / ``container`` / ``module_path`` /
    ``reachability_forced_container``) AND, when a reachability
    requirement says to hoist, mutates ``RbxScript.parent_path`` to
    match — this preserves the legacy ``_apply_reachability_rule``
    behavior verbatim. Slice 7 will move the hoist into the early
    storage decision tree so this late mutation becomes vestigial;
    until then, byte-identical observable behavior REQUIRES the
    finalizer mirror the legacy mutation.

    Returns the FINAL list of excluded ``script_id``s (early-inferred
    excluded + reachability_conflict additions) so the caller can
    compose the report.

    Idempotent: re-running yields the same module rows + script
    parent_paths (the reachability hoist short-circuits when the
    helper is already in ``REPLICATED_STORAGE``, and the per-row
    stamping is pure overwrite).
    """
    modules = scene_runtime.get("modules", {})
    from converter.scene_runtime_planner import (
        build_scripts_by_class_name,
    )
    scripts_list = list(scripts)
    scripts_by_class = build_scripts_by_class_name(
        scripts_list, cast("dict", modules),
    )

    excluded: list[str] = []

    def _stamp_base(script_id: str, module: SceneRuntimeModule) -> None:
        # Prefer the sid-keyed script so a colliding ``class_name`` row
        # (dropped by ``build_scripts_by_class_name``) still gets its
        # container/module_path; fall back to the class-name join when
        # the sid lookup misses (keeps non-colliding behavior identical).
        script = script_by_sid.get(script_id)
        if script is not None:
            _stamp_container_and_path_from_script(module, script)
        else:
            _stamp_container_and_path(module, scripts_by_class)

    for script_id, module in modules.items():
        result = domain_results.get(script_id)
        if result is None:
            # No early inference for this row (shouldn't happen for a
            # legitimate caller, but be defensive).
            _stamp_base(script_id, module)
            continue

        # Stamp domain + signals first.
        if not module.get("runtime_bearing"):
            module["domain"] = "helper"
            _stamp_base(script_id, module)
            continue

        module["domain"] = result["domain"]
        module["domain_signals"] = result["signals"]
        _stamp_base(script_id, module)

        if result["excluded"]:
            excluded.append(script_id)

    # Apply reachability decisions atomically (mirrors what the legacy
    # ``_apply_reachability_rule`` did inline). Resolve the row's script
    # via ``script_by_sid`` (keyed on ``script_id``), NOT the class-name
    # join — the class-name join silently dropped colliding rows (DD7).
    for script_id, requirement in reachability_requirements.items():
        module_row = modules.get(script_id)
        if module_row is None:
            continue
        script = script_by_sid.get(script_id)
        if script is None:
            continue

        if requirement == "__excluded__":
            # Only mark as reachability_conflict if the helper is
            # currently in a server-invisible container (i.e. the
            # legacy rule would have actually fired). This preserves
            # the exact predicate the legacy rule used: ``current_container
            # in _SERVER_CONTAINERS_FOR_REACHABILITY``.
            current_container = script.parent_path or ""
            if current_container not in _SERVER_CONTAINERS_FOR_REACHABILITY:
                continue
            module_row["domain"] = "excluded"
            signals = cast(
                SceneRuntimeDomainSignals,
                module_row.get("domain_signals", {}),
            )
            signals["fail_closed_reason"] = "reachability_conflict"
            module_row["domain_signals"] = signals
            if script_id not in excluded:
                excluded.append(script_id)
            continue

        # Hoist requirement: REPLICATED_STORAGE. Same predicate gate.
        current_container = script.parent_path or ""
        if current_container not in _SERVER_CONTAINERS_FOR_REACHABILITY:
            continue
        # Atomic triple-write: script.parent_path + module.container +
        # module.module_path. Slice 4 round 2 codified this as
        # invariant 10 — see the docstring on
        # ``_apply_reachability_rule`` for the empty-name guard
        # discussion.
        #
        # Phase 2a slice 10: the parallel
        # ``signals["reachability_forced_container"] = REPLICATED_STORAGE``
        # write was retired. ``build_topology._build_modules_block``
        # now derives ``reachability_required_container`` from the
        # raw ``reachability_requirements`` map normalized through the
        # same gate above, so the planner-row audit signal had no
        # remaining production consumer (only the three external
        # tests migrated in slice 10 commit 3 still asserted it).
        script.parent_path = REPLICATED_STORAGE
        module_row["container"] = REPLICATED_STORAGE
        module_row["module_path"] = (
            f"{REPLICATED_STORAGE}.{script.name}"
        )

    return excluded


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def classify_scene_runtime_domains(
    scene_runtime: SceneRuntimeArtifact,
    scripts: Iterable[RbxScript],
    *,
    dependency_map: dict[str, list[str]] | None = None,
    guid_index: GuidIndex | None = None,
    networking: str = DEFAULT_NETWORKING_MODE,
    strict: bool = False,
    transpile_ran: bool = True,
) -> DomainClassifierReport:
    """Populate ``domain`` / ``container`` / ``module_path`` /
    ``domain_signals`` on every runtime-bearing module in
    ``scene_runtime.modules`` (mutated in place).

    Phase 2a slice 6: this function is a thin orchestrator on top of
    ``infer_module_domains`` + ``derive_reachability_requirements`` +
    ``finalize_topology_containers``. The observable behavior is
    byte-identical to slice 5 — the split is internal so slice 7's
    storage decision tree can consume the same prepass output.

    ``scripts`` must already carry their final ``parent_path`` (set by
    ``storage_classifier.classify_storage``). ``dependency_map`` is the
    same ``class_name -> [required_class_names]`` mapping
    ``classify_storage`` uses; the classifier reuses it for the client-
    reachability rule.

    ``guid_index`` provides the path lookup the C# signal channel
    needs. When ``None`` the C# channel is skipped and the classifier
    falls back to Luau-only signals (back-compat with tests that don't
    plumb a guid_index).

    ``networking`` selects the active signal set + the zero-signal
    fallback (see design doc §"Target model"). ``"none"`` (default)
    drops Mirror-only signals and falls back to ``"client"``; ``"mirror"``
    and ``"netcode"`` enable Mirror-only signals and fall back to
    ``"server"``.

    ``strict`` (False by default) gates the strict-classification check.
    The check itself does NOT raise here; callers (pipeline.py) decide
    whether to abort. The report exposes ``strict_violations`` so the
    caller can format an actionable error message.

    Pure function over its inputs except for the in-place mutation of
    ``scene_runtime.modules[*]`` and (for the reachability-hoist path)
    ``RbxScript.parent_path``.
    """
    if networking not in NETWORKING_MODES:
        raise ValueError(
            f"unknown networking mode {networking!r}; "
            f"expected one of {NETWORKING_MODES}"
        )

    scripts_list = list(scripts)

    # Early prepass: domain inference + reachability requirements. Both
    # are pure over their inputs — see their docstrings for the
    # parent_path invariants.
    domain_results = infer_module_domains(
        scene_runtime, scripts_list,
        dependency_map=dependency_map,
        guid_index=guid_index,
        networking=networking,
    )

    # Build the script-name/sid-space inputs the reachability closure +
    # the finalizer share, using the SAME recipes the pipeline uses (so
    # the orchestrator path and the pipeline prepass path agree).
    from converter.roblox_dead_modules import extract_require_edges
    from converter.scene_runtime_planner import (
        build_script_id_by_name,
        derive_intrinsic_script_class,
    )
    from converter.scene_runtime_topology.lifecycle_roles import (
        derive_module_lifecycle_role,
    )

    modules = scene_runtime.get("modules", {})
    scripts_by_name: dict[str, RbxScript] = {
        s.name: s for s in scripts_list if s.name
    }
    script_id_by_name = build_script_id_by_name(
        scripts_list,
        cast("dict[str, SceneRuntimeModule | dict[str, object]]", modules),
    )
    script_by_sid: dict[str, RbxScript] = {
        sid: scripts_by_name[script_name]
        for script_name, sid in script_id_by_name.items()
        if script_name in scripts_by_name
    }
    lifecycle_roles: dict[str, str] = {}
    for sid, row in modules.items():
        script = script_by_sid.get(sid)
        script_class = derive_intrinsic_script_class(script)
        module_domain = (
            domain_results[sid]["domain"] if sid in domain_results else ""
        )
        role = derive_module_lifecycle_role(
            domain=module_domain,
            script_class=script_class,
            character_attached=bool(row.get("character_attached", False)),
            is_loader=bool(row.get("is_loader", False)),
        )
        lifecycle_roles[sid] = role
    known_names = frozenset(s.name for s in scripts_list if s.name)
    require_edges_by_name: dict[str, set[str]] = {
        s.name: extract_require_edges(s.source, known_names)
        for s in scripts_list if s.name
    }

    requirements = derive_reachability_requirements(
        scene_runtime, scripts_list, domain_results,
        require_edges_by_name=require_edges_by_name,
        script_by_sid=script_by_sid,
        lifecycle_roles=lifecycle_roles,
        transpile_ran=transpile_ran,
    )

    # Late finalizer: stamp the early-inferred domains + reachability
    # decisions onto the module rows + (for the hoist path) onto the
    # ``RbxScript.parent_path``. Returns the final excluded list.
    excluded = finalize_topology_containers(
        scene_runtime, scripts_list, domain_results, requirements,
        script_by_sid=script_by_sid,
    )

    # Collect low_confidence + displaced from the early prepass results.
    low_confidence: list[str] = []
    displaced: list[SceneRuntimeDisplacedInstance] = []
    for script_id, result in domain_results.items():
        if result["low_confidence"]:
            low_confidence.append(script_id)
        displaced.extend(result["displaced_instances"])

    # Mirror adoption check: requires a recomputed cs_source_cache. The
    # cache the early prepass built was discarded — rebuild it here to
    # preserve the legacy behavior. Skipped under --networking=none.
    mirror_low = False
    if networking in ("mirror", "netcode"):
        modules = scene_runtime.get("modules", {})
        from converter.scene_runtime_planner import (
            build_scripts_by_class_name,
        )
        scripts_by_class = build_scripts_by_class_name(
            scripts_list, cast("dict", modules),
        )
        cs_source_cache: dict[str, str] = {}

        def _get_cs_source(script_id: str) -> str:
            if script_id in cs_source_cache:
                return cs_source_cache[script_id]
            text = _load_cs_source(script_id, guid_index)
            cs_source_cache[script_id] = text
            return text

        # Prewarm the cache with every runtime-bearing module so the
        # imports-zero check sees the same set as the legacy pass.
        for script_id, module in modules.items():
            if module.get("runtime_bearing"):
                _get_cs_source(script_id)
        mirror_low = _check_mirror_adoption(
            modules, scripts_by_class, cs_source_cache, _get_cs_source,
        )

    strict_violations = sorted(set(low_confidence) | set(excluded))

    return DomainClassifierReport(
        displaced_instances=displaced,
        low_confidence_modules=low_confidence,
        excluded_modules=excluded,
        fail_closed_modules=excluded,
        mirror_adoption_low=mirror_low,
        strict_violations=strict_violations,
    )


# ---------------------------------------------------------------------------
# Artifact migration: domain == "legacy" -> "excluded".
#
# Applied to on-disk plans on first read. Idempotent.
# ---------------------------------------------------------------------------

def migrate_legacy_domain_values(scene_runtime: SceneRuntimeArtifact) -> int:
    """Mutate ``scene_runtime.modules[*].domain`` in place, rewriting any
    ``"legacy"`` value to ``"excluded"``. Returns the count of rows
    migrated. Idempotent: re-running yields 0.

    Also rewrites ``fail_closed_reason="both_side_api"`` /
    ``"intra_class_conflict"`` rows untouched (their reasons still apply
    under the new model) but stamps a migration breadcrumb so the
    conversion report can surface the prior PR3b verdict.
    """
    modules = scene_runtime.get("modules", {})
    count = 0
    for module in modules.values():
        if module.get("domain") == "legacy":
            module["domain"] = "excluded"
            signals = cast(
                SceneRuntimeDomainSignals,
                module.get("domain_signals", {}),
            )
            # Breadcrumb so the report can show "migrated from PR3b legacy".
            signals.setdefault(
                "fail_closed_reason", "legacy_artifact_migrated",
            )
            module["domain_signals"] = signals
            count += 1
    return count


# ---------------------------------------------------------------------------
# Per-module classification: the design doc's 7-rule table.
# ---------------------------------------------------------------------------

class _SignalCounts(TypedDict):
    strong_client: int
    strong_server: int
    moderate_client: int
    moderate_server: int
    cs_signals: list[str]
    luau_signals: list[str]
    instance_signals: list[str]


def _classify_module(
    script_id: str,
    module: SceneRuntimeModule,
    scripts_by_class: dict[str, RbxScript],
    instance_evidence: list["_InstanceEvidence"],
    override: str | None,
    cs_source: str,
    networking: str,
    *,
    extra_moderate_server: tuple[str, ...] = (),
) -> tuple[str, SceneRuntimeDomainSignals, list[SceneRuntimeDisplacedInstance]]:
    """Return ``(domain, signals, displaced_instances)`` for one module.

    ``extra_moderate_server`` is the tuple of moderate-server signal kind
    names sourced from the project-level pre-pass (graph-derived signals
    that can't be answered by single-module C# scanning, e.g.
    ``network_behaviour_reachable``). The signals get folded into the
    moderate-server bucket alongside any table-driven hits.
    """
    class_name = module.get("class_name", "")
    script = scripts_by_class.get(class_name)
    luau_source = script.source if script and script.source else ""

    # Compute UI-bearing instance status BEFORE we run _collect_signals,
    # so we can decide whether to count per-instance UI as a strong-client
    # signal (consistent across all instances) or treat it as an intra-
    # class conflict (some instances UI, some not, and no code-level
    # strong signal from C#/Luau pins the class).
    instance_ui = [
        ev for ev in instance_evidence
        if ev.has_ui_ref or ev.owner_is_ui
    ]
    instance_nonui = [
        ev for ev in instance_evidence
        if not (ev.has_ui_ref or ev.owner_is_ui)
    ]
    multi_instance = len(instance_evidence) > 1
    ui_consistent = (
        not multi_instance
        or len(instance_nonui) == 0
        or len(instance_ui) == 0
    )

    # Pass 1: collect code-level signals (C# + Luau) WITHOUT the per-
    # instance UI signal so we can decide whether to fire the conflict.
    code_counts = _collect_signals(
        cs_source, luau_source, [], networking,
        extra_moderate_server=extra_moderate_server,
    )
    has_code_strong = (
        code_counts["strong_client"] + code_counts["strong_server"] > 0
    )

    # Intra-class conflict: instances disagree about UI evidence AND no
    # code-level strong signal pins the class. (When code-level strong
    # signals are present they're authoritative — a script that calls
    # ``Players.LocalPlayer`` is client regardless of where its instances
    # live.) Preserves PR3b's intra-class conflict semantics under the
    # new model.
    intra_conflict = (
        not has_code_strong
        and multi_instance
        and len(instance_ui) > 0
        and len(instance_nonui) > 0
    )

    # Pass 2: full signal collection. Skip per-instance UI when we've
    # determined an intra-class conflict — otherwise the conflict would
    # be drowned out by the (always-strong) target_is_ui signal.
    if intra_conflict:
        counts = code_counts
    else:
        counts = _collect_signals(
            cs_source, luau_source, instance_evidence, networking,
            extra_moderate_server=extra_moderate_server,
        )

    api = _classify_api_surface(luau_source)  # legacy field, kept for tests
    any_ui = (
        bool(instance_ui)
        or "instance_owner_is_ui" in counts["instance_signals"]
        or "target_is_ui" in counts["instance_signals"]
    )

    signals: SceneRuntimeDomainSignals = {
        "api_surface": api,
        "ui_signal": any_ui,
        "strong_client": counts["strong_client"],
        "strong_server": counts["strong_server"],
        "moderate_client": counts["moderate_client"],
        "moderate_server": counts["moderate_server"],
        "cs_signals": list(counts["cs_signals"]),
        "luau_signals": list(counts["luau_signals"]),
        "instance_signals": list(counts["instance_signals"]),
    }

    # Apply the 7-rule table.
    rule, base_verdict, fail_reason, low_conf = _apply_rule_table(
        counts, networking,
    )
    signals["rule_applied"] = rule
    if low_conf:
        signals["low_confidence"] = True
    if fail_reason:
        signals["fail_closed_reason"] = fail_reason

    # Intra-class conflict short-circuits when the rule table didn't
    # already exclude on stronger evidence. Without an override, surface
    # as excluded (matches PR3b semantics).
    if intra_conflict and base_verdict not in ("excluded",):
        signals["intra_class_conflict"] = True
        if not override:
            signals["fail_closed_reason"] = "intra_class_conflict"
            # Pop low_confidence — intra_conflict is a stronger reason.
            signals.pop("low_confidence", None)
            return "excluded", signals, []

    # Operator override. Asymmetry from design doc §"Operator override":
    # - Rule-1 ``excluded`` (both strong sides): only ``"excluded"`` is
    #   accepted. Other override values are REJECTED (verdict stays
    #   ``"excluded"``, ``override_rejected`` stamped).
    # - All other verdicts (including Rule-4 ``excluded``): override may
    #   be ``"client"`` | ``"server"`` | ``"excluded"``.
    if override is not None:
        if base_verdict == "excluded" and rule == 1:
            # Rule-1: code disagrees with itself. Only ACK-and-skip allowed.
            if override == "excluded":
                signals["override_applied"] = True
                return "excluded", signals, []
            signals["override_rejected"] = True
            return "excluded", signals, []
        if override in ("client", "server", "excluded"):
            signals["override_applied"] = True
            # If there was an intra-class conflict and the operator pinned
            # a side, emit the displaced report.
            if intra_conflict:
                displaced = _build_displaced_rows(
                    script_id, override, instance_ui, instance_nonui,
                )
                # The override is the resolution; clear low_confidence
                # because the operator made the call.
                signals.pop("low_confidence", None)
                return override, signals, displaced
            # Clear low_confidence flag when an operator pin replaces it.
            signals.pop("low_confidence", None)
            # Routing a formerly-``excluded`` module onto a side: the runtime
            # only constructs ``client``/``server``, so the pin is what makes it
            # ship. PRESERVE the original fail-closed reason as an audit trail
            # (do NOT silently drop it — the operator forced a side onto a
            # verdict the classifier couldn't resolve, and the opposite-side
            # behavior will NOT run). Move it off ``fail_closed_reason`` (which
            # is excluded-only by contract) onto a sticky field + warn. Rule-1
            # never reaches here (rejected above), so this only covers the
            # ambiguity classes (Rule-4 moderate-only, Rule-7 low-confidence).
            if override != "excluded":
                reason = signals.pop("fail_closed_reason", None)
                if base_verdict == "excluded":
                    signals["override_routed_off_excluded"] = True
                    if reason is not None:
                        signals["overridden_excluded_reason"] = reason
                    log.warning(
                        "[scene_runtime] %s: operator override pinned a "
                        "formerly-excluded module (%s) to %r; opposite-side "
                        "behavior will not run — confirm the source is not "
                        "genuinely dual-domain",
                        script_id, reason or "unresolved", override,
                    )
            return override, signals, []
        # Unknown override value: ignore (treat as no override). Should
        # have been validated upstream.
        log.warning(
            "[scene_runtime] unknown override value %r for %s; ignoring",
            override, script_id,
        )

    return base_verdict, signals, []


def _collect_signals(
    cs_source: str,
    luau_source: str,
    instance_evidence: list["_InstanceEvidence"],
    networking: str,
    *,
    extra_moderate_server: tuple[str, ...] = (),
) -> _SignalCounts:
    """Aggregate strong/moderate signal hits across all three channels.

    Each signal kind counts at most once. Same kind firing from multiple
    channels (e.g., both C# and Luau pinning client) still counts once
    per channel listing but contributes only once to the strong/moderate
    bucket count.

    ``extra_moderate_server`` lets the caller inject moderate-server
    signal kinds whose source isn't the per-module signal channels
    (graph-derived signals like ``network_behaviour_reachable``). They
    are appended to ``cs_signals`` (closest existing channel) and added
    to the moderate-server bucket.
    """
    cs_signals: list[str] = []
    luau_signals: list[str] = []
    instance_signals: list[str] = []
    strong_client_kinds: set[str] = set()
    strong_server_kinds: set[str] = set()
    moderate_client_kinds: set[str] = set()
    moderate_server_kinds: set[str] = set()

    mirror_mode = networking in ("mirror", "netcode")

    # --- C# channel ---
    if cs_source:
        for rx, name, mirror_only in _CS_STRONG_CLIENT_RX:
            if mirror_only and not mirror_mode:
                continue
            if rx.search(cs_source):
                cs_signals.append(name)
                strong_client_kinds.add(name)
        for rx, name, mirror_only in _CS_MODERATE_CLIENT_RX:
            if mirror_only and not mirror_mode:
                continue
            if rx.search(cs_source):
                cs_signals.append(name)
                moderate_client_kinds.add(name)
        for rx, name, mirror_only in _CS_STRONG_SERVER_RX:
            if mirror_only and not mirror_mode:
                continue
            if rx.search(cs_source):
                cs_signals.append(name)
                strong_server_kinds.add(name)
        for rx, name, mirror_only in _CS_MODERATE_SERVER_RX:
            if mirror_only and not mirror_mode:
                continue
            if rx.search(cs_source):
                cs_signals.append(name)
                moderate_server_kinds.add(name)

    # --- Luau channel (post-transpile) ---
    # Roblox-flavoured patterns are STRONG signals per the design doc
    # §"Strong client signals" / §"Strong server signals" tables.
    if luau_source:
        # Scan CODE only: strip comments + long-bracket strings (lexically, so
        # quoted string args survive), then strip module-resolution
        # ``require(...)`` paths — neither commented tokens nor a converter-
        # emitted ServerStorage require-fallback should count as a signal
        # (see _strip_luau_noise / _strip_require_calls).
        scan_src = _strip_require_calls(_strip_luau_noise(luau_source))
        # Token-aware: a pattern only counts when its match starts in CODE, so
        # an API token sitting inside a string literal (data) is not a signal.
        in_string = _string_content_mask(scan_src)
        if any(_api_pattern_fires(rx, scan_src, in_string) for rx in _CLIENT_RX):
            luau_signals.append("roblox_client_api")
            strong_client_kinds.add("roblox_client_api")
        if any(_api_pattern_fires(rx, scan_src, in_string) for rx in _SERVER_RX):
            luau_signals.append("roblox_server_api")
            strong_server_kinds.add("roblox_server_api")

    # --- Per-instance channel ---
    # instance_owner_is_ui is the strongest available client signal per
    # the design doc — "Script attached to GameObject owning a Canvas".
    owner_is_ui = any(getattr(ev, "owner_is_ui", False) for ev in instance_evidence)
    target_is_ui = any(ev.has_ui_ref for ev in instance_evidence)
    if owner_is_ui:
        instance_signals.append("instance_owner_is_ui")
        strong_client_kinds.add("instance_owner_is_ui")
    if target_is_ui:
        instance_signals.append("target_is_ui")
        strong_client_kinds.add("target_is_ui")

    # --- Graph-derived moderate-server signals ---
    # Stamped by the caller after the project-level require-graph walk.
    # Listed in cs_signals (it's the closest existing channel: the
    # underlying evidence is the C# class hierarchy of a transitively-
    # required module).
    for kind in extra_moderate_server:
        if kind not in moderate_server_kinds:
            cs_signals.append(kind)
            moderate_server_kinds.add(kind)

    return {
        "strong_client": len(strong_client_kinds),
        "strong_server": len(strong_server_kinds),
        "moderate_client": len(moderate_client_kinds),
        "moderate_server": len(moderate_server_kinds),
        "cs_signals": cs_signals,
        "luau_signals": luau_signals,
        "instance_signals": instance_signals,
    }


def _apply_rule_table(
    counts: _SignalCounts, networking: str,
) -> tuple[int, str, str, bool]:
    """Return ``(rule_number, verdict, fail_reason, low_confidence)``.

    See design doc §"Resolution rules" for the table.
    """
    sc = counts["strong_client"]
    ss = counts["strong_server"]
    mc = counts["moderate_client"]
    ms = counts["moderate_server"]

    # Rule 1: both strong sides → excluded (unresolvable).
    if sc > 0 and ss > 0:
        return 1, "excluded", "both_side_api", False
    # Rule 2: strong client only → client.
    if sc > 0 and ss == 0:
        return 2, "client", "", False
    # Rule 3: strong server only → server.
    if ss > 0 and sc == 0:
        return 3, "server", "", False
    # Rule 4: moderate-only with both sides → excluded.
    if sc == 0 and ss == 0 and mc > 0 and ms > 0:
        return 4, "excluded", "moderate_only_ambiguity", False
    # Rule 5: moderate client only → client.
    if sc == 0 and ss == 0 and mc > 0 and ms == 0:
        return 5, "client", "", False
    # Rule 6: moderate server only → server.
    if sc == 0 and ss == 0 and ms > 0 and mc == 0:
        return 6, "server", "", False
    # Rule 7: all zero — mode-dependent fallback.
    if networking == "none":
        return 7, "client", "", True
    # mirror / netcode → server-authoritative fallback.
    return 7, "server", "", True


def _classify_api_surface(source: str) -> str:
    """Return ``"client"`` / ``"server"`` / ``"both"`` / ``"neither"``
    for a Luau body. Kept verbatim from PR3b for back-compat with the
    legacy-tables-byte-frozen test in test_scene_runtime_domain.py.
    """
    if not source:
        return "neither"
    has_client = any(rx.search(source) for rx in _CLIENT_RX)
    has_server = any(rx.search(source) for rx in _SERVER_RX)
    if has_client and has_server:
        return "both"
    if has_client:
        return "client"
    if has_server:
        return "server"
    return "neither"


# ---------------------------------------------------------------------------
# C# source loading
# ---------------------------------------------------------------------------

def _read_cs_text(
    script_id: str, guid_index: GuidIndex | None,
) -> str | None:
    """Resolve ``script_id`` (a .cs file GUID) to its RAW on-disk text.

    Returns ``None`` (no scrubbing applied) when:
      - ``guid_index`` is ``None`` (tests, or pipelines without a
        Unity project root),
      - the script id isn't a real GUID known to the index,
      - the resolved path isn't a .cs file,
      - the file can't be read.

    Shared read path for ``_load_cs_source`` and
    ``_load_cs_source_preserving_strings`` (DP1) — one resolve/suffix/
    read-error path, two scrub modes applied by the callers.
    """
    if guid_index is None or not script_id:
        return None
    try:
        path: Path | None = guid_index.resolve(script_id)
    except Exception:
        return None
    if path is None or path.suffix != ".cs":
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _load_cs_source(
    script_id: str, guid_index: GuidIndex | None,
) -> str:
    """Resolve ``script_id`` (a .cs file GUID) to its on-disk text.

    Returns the empty string when:
      - ``guid_index`` is ``None`` (tests, or pipelines without a
        Unity project root),
      - the script id isn't a real GUID known to the index,
      - the resolved path isn't a .cs file,
      - the file can't be read.
    """
    raw = _read_cs_text(script_id, guid_index)
    if raw is None:
        return ""
    # Strip comments / string + char literals BEFORE returning so every
    # downstream regex pass sees only code tokens. Same-length output
    # keeps line/column anchors stable for re.MULTILINE.
    return _strip_cs_noise(raw)


def _load_cs_source_preserving_strings(
    script_id: str, guid_index: GuidIndex | None,
) -> str:
    """Like ``_load_cs_source`` but strips comments only (keeps string +
    char literals VERBATIM) so the Animator-write parser
    (``animation_driver_analyzer.extract_animator_param_writes``) can read
    param names that live only as string literals (``SetBool("open", …)``).

    Returns ``""`` on the same conditions as ``_load_cs_source``
    (``None`` guid_index, unknown guid, non-.cs path, unreadable file).
    """
    raw = _read_cs_text(script_id, guid_index)
    if raw is None:
        return ""
    return _strip_cs_noise(raw, preserve_strings=True)


# ---------------------------------------------------------------------------
# Per-instance evidence
# ---------------------------------------------------------------------------

class _InstanceEvidence:
    """One instance's contribution to the class's evidence pool."""

    __slots__ = (
        "owner_kind", "owner_ref", "instance_id", "game_object_id",
        "script_id", "has_ui_ref", "owner_is_ui",
    )

    def __init__(
        self,
        owner_kind: str,
        owner_ref: str,
        instance_id: str,
        game_object_id: str,
        script_id: str,
        has_ui_ref: bool,
        owner_is_ui: bool,
    ) -> None:
        self.owner_kind = owner_kind
        self.owner_ref = owner_ref
        self.instance_id = instance_id
        self.game_object_id = game_object_id
        self.script_id = script_id
        self.has_ui_ref = has_ui_ref
        self.owner_is_ui = owner_is_ui


def _gather_per_instance_evidence(
    scenes: dict[str, SceneRuntimeScene],
    prefabs: dict[str, SceneRuntimePrefab],
) -> dict[str, list[_InstanceEvidence]]:
    """Walk every instance in every scene + prefab; group by ``script_id``."""
    out: dict[str, list[_InstanceEvidence]] = {}

    def _scan(
        owner_kind: str,
        owner_ref: str,
        instances: list[SceneRuntimeInstance],
        references: list[SceneRuntimeReference],
    ) -> None:
        ui_by_instance: dict[str, bool] = {}
        for ref in references:
            if ref.get("target_is_ui"):
                ui_by_instance[ref["from"]] = True
        for inst in instances:
            # ``instance_owner_is_ui`` lives in the extra fields stamped
            # by the planner (total=False) — read it via .get on the
            # underlying dict.
            inst_dict = cast(dict[str, object], inst)
            owner_is_ui = bool(inst_dict.get("instance_owner_is_ui", False))
            evidence = _InstanceEvidence(
                owner_kind=owner_kind,
                owner_ref=owner_ref,
                instance_id=inst["instance_id"],
                game_object_id=inst["game_object_id"],
                script_id=inst["script_id"],
                has_ui_ref=ui_by_instance.get(inst["instance_id"], False),
                owner_is_ui=owner_is_ui,
            )
            out.setdefault(inst["script_id"], []).append(evidence)

    for key, scene in scenes.items():
        _scan("scene", key, scene.get("instances", []),
              scene.get("references", []))
    for key, prefab in prefabs.items():
        _scan("prefab", key, prefab.get("instances", []),
              prefab.get("references", []))
    return out


def _build_displaced_rows(
    script_id: str,
    effective_domain: str,
    ui_evidence: list[_InstanceEvidence],
    nonui_evidence: list[_InstanceEvidence],
) -> list[SceneRuntimeDisplacedInstance]:
    """Compose displaced-instance rows for the conversion report."""
    rows: list[SceneRuntimeDisplacedInstance] = []
    for ev in ui_evidence:
        if effective_domain != "client":
            rows.append({
                "owner_kind": ev.owner_kind,
                "owner_ref": ev.owner_ref,
                "scene": ev.owner_ref,
                "instance_id": ev.instance_id,
                "game_object_id": ev.game_object_id,
                "script_id": script_id,
                "effective_domain": effective_domain,
                "inferred_domain": "client",
            })
    for ev in nonui_evidence:
        if effective_domain != "server":
            rows.append({
                "owner_kind": ev.owner_kind,
                "owner_ref": ev.owner_ref,
                "scene": ev.owner_ref,
                "instance_id": ev.instance_id,
                "game_object_id": ev.game_object_id,
                "script_id": script_id,
                "effective_domain": effective_domain,
                "inferred_domain": "server",
            })
    return rows


# ---------------------------------------------------------------------------
# container / module_path stamping
# ---------------------------------------------------------------------------

def _stamp_container_and_path(
    module: SceneRuntimeModule, scripts_by_class: dict[str, RbxScript],
) -> None:
    """Copy storage_classifier's parent_path onto the module row, plus the
    dotted DataModel path the host runtime requires().
    """
    script = scripts_by_class.get(module.get("class_name", ""))
    _stamp_container_and_path_from_script(module, script)


def _stamp_container_and_path_from_script(
    module: SceneRuntimeModule, script: RbxScript | None,
) -> None:
    """Stamp ``container`` / ``module_path`` from a directly-resolved
    ``RbxScript`` (sid-aware path).

    Carries the same body as the class-name-keyed
    ``_stamp_container_and_path`` so a row whose ``class_name`` collides
    with a sibling — and is therefore DROPPED by
    ``build_scripts_by_class_name`` — still gets its container/module_path
    when the caller can resolve the script by ``script_id``.
    """
    if script is None:
        return
    container = script.parent_path or ""
    if container:
        module["container"] = container
    if script.name and container:
        module["module_path"] = f"{container}.{script.name}"


# ---------------------------------------------------------------------------
# Reachability rule (client require graph must not reach ServerStorage)
#
# Phase 2a slice 11: the legacy ``_apply_reachability_rule`` was deleted
# here. It had no callers since slice 6 (its docstring promised slice 7
# would delete it; the deletion was deferred for flag-day safety on a
# function that turned out to have no out-of-tree callers either). Its
# behavior was split into two pieces during slice 6:
#
#   - ``derive_reachability_requirements`` runs EARLY (before
#     ``classify_storage``) and computes the
#     ``{script_id: required_container}`` mapping. PURE: no
#     ``parent_path`` reads, no mutation.
#   - ``finalize_topology_containers`` runs LATE (after
#     ``classify_storage``) and applies the requirements atomically
#     onto module rows + ``RbxScript.parent_path``, preserving the
#     legacy predicate gate
#     (``current_container in _SERVER_CONTAINERS_FOR_REACHABILITY``)
#     so the behavior delta is zero.
# ---------------------------------------------------------------------------


def _compute_network_behaviour_reachable(
    modules: dict[str, SceneRuntimeModule],
    dependency_map: dict[str, list[str]] | None,
    get_cs_source,  # type: ignore[no-untyped-def]
    networking: str,
) -> set[str]:
    """Return the set of ``script_id``s whose transitive require graph
    reaches a NetworkBehaviour subclass (excluding the script itself).

    Implements the design doc's graph-derived moderate-server signal
    (``network_behaviour_reachable``). Skipped under
    ``--networking=none`` — the signal is mirror-only per the design
    doc's signal table.

    A module's own NetworkBehaviour-ness is already a STRONG-SERVER
    signal (Rule-3 ``NetworkBehaviour_subclass``); this pass is about
    indirect server-affinity via require edges.

    Returns ``set()`` when dependency_map is empty or under
    ``--networking=none``.
    """
    if not dependency_map or networking not in ("mirror", "netcode"):
        return set()

    # Build {class_name: is_network_behaviour}. We scan the (already-
    # scrubbed-of-comments) C# source for the class-declaration regex.
    class_to_script_id: dict[str, str] = {}
    for script_id, module in modules.items():
        class_name = module.get("class_name", "")
        if class_name:
            class_to_script_id.setdefault(class_name, script_id)

    is_nb: dict[str, bool] = {}
    for class_name, script_id in class_to_script_id.items():
        src = get_cs_source(script_id)
        is_nb[class_name] = bool(
            src and _RE_NETWORK_BEHAVIOUR_CLASS.search(src)
        )

    # For each runtime-bearing module: walk its require closure, skip
    # the module itself, see whether any reached class is NB.
    reachable: set[str] = set()
    for script_id, module in modules.items():
        if not module.get("runtime_bearing"):
            continue
        class_name = module.get("class_name", "")
        if not class_name:
            continue
        # Walk strictly downstream — exclude the seed class so a
        # NetworkBehaviour itself doesn't stamp the moderate-server
        # signal (it already trips strong-server via the table).
        seeds: set[str] = set(dependency_map.get(class_name, ()))
        closure = _closure(seeds, dependency_map)
        # Add the immediate seeds too (``_closure`` returns the visited
        # set after popping from a stack seeded with `seeds`, which
        # already includes them — sanity check by union).
        closure = closure | seeds
        # Don't count the module's own class even if its require graph
        # circles back.
        closure.discard(class_name)
        if any(is_nb.get(c) for c in closure):
            reachable.add(script_id)

    return reachable


def _closure(
    seeds: set[str], dependency_map: Mapping[str, Iterable[str]],
) -> set[str]:
    """Transitive closure of ``seeds`` under ``dependency_map``."""
    visited: set[str] = set()
    stack: list[str] = list(seeds)
    while stack:
        cur = stack.pop()
        if cur in visited:
            continue
        visited.add(cur)
        for dep in dependency_map.get(cur, ()):
            if dep not in visited:
                stack.append(dep)
    return visited


# ---------------------------------------------------------------------------
# mirror_adoption_low heuristic.
# ---------------------------------------------------------------------------

def _check_mirror_adoption(
    modules: dict[str, SceneRuntimeModule],
    scripts_by_class: dict[str, RbxScript],
    cs_source_cache: dict[str, str],
    get_cs_source,  # type: ignore[no-untyped-def]
) -> bool:
    """Return True when the project declared ``--networking=mirror|netcode``
    but adoption signals are too sparse.

    Threshold (from design doc §"Mirror-mode adoption heuristic"):
      - Annotated classes count < ``max(2, ceil(0.05 × runtime_bearing))``, OR
      - Project has zero ``using Mirror`` / ``using Unity.Netcode``
        imports across all C# files in the cache.

    Either condition fires the warning; the caller surfaces it in the
    conversion report (does NOT block conversion).
    """
    runtime_bearing_count = 0
    annotated_count = 0
    for script_id, module in modules.items():
        if not module.get("runtime_bearing"):
            continue
        runtime_bearing_count += 1
        # Annotated = at least one Mirror-only signal fired (ServerRpc,
        # ClientRpc, NetworkBehaviour subclass, SyncVar, etc.). Read off
        # the persisted signals.
        signals = cast(
            SceneRuntimeDomainSignals,
            module.get("domain_signals", {}),
        )
        cs_signals = signals.get("cs_signals", []) or []
        mirror_annotation_kinds = (
            "ServerRpc", "ClientRpc", "Server", "ServerAttribute",
            "Client", "ClientAttribute", "ServerCallback",
            "ClientCallback", "NetworkBehaviour_subclass",
            "SyncVar", "Command",
        )
        if any(s in mirror_annotation_kinds for s in cs_signals):
            annotated_count += 1

    if runtime_bearing_count == 0:
        return False

    threshold = max(2, math.ceil(0.05 * runtime_bearing_count))
    if annotated_count < threshold:
        return True

    # Imports-zero check: scan every loaded C# source for `using Mirror`
    # or `using Unity.Netcode`. We're scanning the cache (already-loaded
    # sources) — modules not yet visited won't have been read. For the
    # heuristic that's acceptable: if the project uses Mirror anywhere
    # important enough to land in scene_runtime.modules, at least one
    # runtime-bearing module's C# source is in cache.
    has_mirror_using = False
    for src in cs_source_cache.values():
        if src and _RE_USING_MIRROR.search(src):
            has_mirror_using = True
            break
    if not has_mirror_using:
        return True

    return False


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = (
    "classify_scene_runtime_domains",
    "derive_reachability_requirements",
    "finalize_topology_containers",
    "infer_module_domains",
    "migrate_legacy_domain_values",
    "DomainClassifierReport",
    "NetworkingMode",
    "NETWORKING_MODES",
    "DEFAULT_NETWORKING_MODE",
    "TopologyInputs",
    "_GENERIC_CLIENT_API_PATTERNS",
    "_GENERIC_SERVER_API_PATTERNS",
)
