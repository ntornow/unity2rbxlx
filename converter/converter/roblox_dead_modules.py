"""roblox_dead_modules.py -- generic detection of Roblox-dead Unity modules.

A *Roblox-dead* module is a Unity script whose behavior has no Roblox runtime
equivalent: a rendering / shader / camera-effect helper that, once transpiled,
does nothing executable in Roblox. The canonical example is the SimpleFPS water
cluster (``WaterBase`` / ``Displace`` / ``PlanarReflection`` / ...), but the
detector is **generic** -- it never matches on class names or a hardcoded API
list. It derives the verdict entirely from mapping-coverage data and the
transpiled output's shape, so a renamed equivalent (``OceanShimmer`` with the
same body) is caught the same way.

The definition is **D3 (both-agree) + HARD VETO** (see
``docs/design/roblox-dead-module-routing-brief.md`` LOCKED DECISIONS):

  INPUT prior (weak):  the fraction of the module's C# API references that
    resolve to a REAL mapping (present in ``API_CALL_MAP`` / ``TYPE_MAP`` and
    whose value is NOT a ``-- ...: no equivalent`` comment stub) is low. Absent
    *and* ``no equivalent``-stub entries both count as unmapped.

  OUTPUT confirmation (decisive):  the module's POST-COHERENCE transpiled Luau
    body is inert -- only class-table boilerplate / comments / ``print`` /
    empty lifecycle handlers / empty ``Heartbeat:Connect`` TODO bodies /
    ``require`` of other modules. The converter's own deterministic stub
    markers (``-- ...: Unity visual/rendering effect (no Roblox equivalent)``
    and the inert-component-stub self-label) are strong signals.

  HARD VETO:  any single genuine Roblox effect in the body (``Instance.new``,
    ``.Parent =``, a property write to a real instance -- including a CHAINED
    receiver such as ``self.part.CFrame = ...`` or
    ``workspace.CurrentCamera.FieldOfView = ...`` -- a RemoteEvent /
    BindableEvent fire, or a DataStore / real-service mutation) ⇒ NOT dead,
    regardless of fraction.

    The brief also lists "a call that resolves to a genuinely-mapped API" as a
    veto. That is SUBSUMED by the structural veto above plus output-inertness:
    any genuinely-mapped Unity API the AI translates emits real Roblox code
    (an ``Instance.new`` / property write / service call), which the structural
    patterns already catch; a mapped call that translates to a pure value-read
    or a comment stub is not an OUTPUT effect and correctly leaves the body
    inert. So ``has_genuine_roblox_effect`` deliberately does NOT consult
    ``API_CALL_MAP`` / ``TYPE_MAP`` -- the structural body veto is the decisive,
    lower-false-positive signal.

A module is dead iff INPUT-prior agrees AND OUTPUT is inert AND no veto.

All inputs are pure data (C# source string + post-coherence Luau string +
the static mapping tables); the public functions are pure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from converter.api_mappings import API_CALL_MAP, TYPE_MAP


# ---------------------------------------------------------------------------
# Tunables (structural, not game-specific)
# ---------------------------------------------------------------------------

# INPUT prior (weak corroboration): a module is "dead-leaning" when its C# API
# surface is DOMINATED by unmapped APIs -- i.e. more than half of its distinct
# references are unmapped, so at most this fraction resolve to a real (non-stub)
# mapping. Empirically (SimpleFPS, real AI run) the Roblox-dead rendering
# cluster sits at 0.00-0.42 real coverage; this "dominated by unmapped" ceiling
# sits above it so every dead module clears the prior. The decisive separation
# from live gameplay comes from the OUTPUT-side veto / inertness (every live
# gameplay module produces a non-inert AI body regardless of its input
# coverage) PLUS the strategy gate in the pass (output inertness is only trusted
# for ``ai``/``stub`` strategies, never a ``rule_based`` fallback whose inert
# body is degraded-run noise). The prior is never the sole basis for a verdict.
_REAL_COVERAGE_DEAD_CEILING = 0.49

# A module with too few distinct API references to measure coverage reliably
# leans on the OUTPUT side alone (the input prior abstains rather than guesses).
_MIN_API_REFS_FOR_INPUT_PRIOR = 3


# ---------------------------------------------------------------------------
# Mapping-coverage helpers (input side)
# ---------------------------------------------------------------------------

# A mapping value that is a ``-- ...: no equivalent`` comment is a stub, NOT a
# real Roblox equivalent (GF5). Treat it as unmapped.
_NO_EQUIVALENT_STUB = re.compile(r"no equivalent", re.IGNORECASE)


def _is_real_mapping_value(value: str) -> bool:
    """True when a mapping table value is a genuine Roblox equivalent.

    A value that is a comment stub (starts with ``--``) is not executable
    Roblox code. A ``-- ...: no equivalent`` comment is explicitly dead. Other
    comment stubs (``-- EditorGUI: editor-only`` etc.) are deliberate strips of
    Unity-only behavior and also do not contribute real coverage.
    """
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.startswith("--"):
        return False
    if _NO_EQUIVALENT_STUB.search(stripped):
        return False
    return True


# Distinct dotted C# API references: ``Foo.Bar``, ``Foo.Bar.Baz`` (the leading
# Type.member is what the mapping tables key on). Also bare type identifiers for
# TYPE_MAP coverage. We collect tokens; coverage is measured against the
# longest mapping-table key that prefixes each token.
_DOTTED_REF = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)")
_BARE_TYPE = re.compile(r"\b([A-Z][A-Za-z0-9_]+)\b")


# Structural / declaration tokens that are NOT a behavioral API surface --
# every MonoBehaviour has these, so counting them would make a trivial empty
# class look "dominated by unmapped APIs". Excluded from the bare-type surface.
_STRUCTURAL_TOKENS = frozenset({
    "MonoBehaviour", "ScriptableObject", "Awake", "Start", "Update",
    "FixedUpdate", "LateUpdate", "OnEnable", "OnDisable", "OnDestroy",
    "Behaviour", "Component", "Object", "Coroutine", "IEnumerator",
    "System", "Collections", "Generic", "Serializable", "SerializeField",
    "HideInInspector", "RequireComponent", "Header", "Tooltip", "Range",
})


def _extract_csharp_api_refs(csharp_source: str) -> tuple[set[str], set[str]]:
    """Return ``(dotted_calls, bare_types)`` distinct C# API references.

    Strips line + block comments first so commented-out APIs do not inflate
    the surface.

    ``dotted_calls`` are the behavioral API surface (``Shader.EnableKeyword``,
    ``Camera.main``, ``GL.invertCulling``) -- what ``API_CALL_MAP`` keys on.
    The input prior is only MEASURED when there are enough of these; structural
    boilerplate alone (a trivial ``class X : MonoBehaviour { void Awake() {} }``)
    has none and the prior abstains rather than mislabeling it dead.

    ``bare_types`` are PascalCase type tokens (``RenderTexture``, ``Vector3``)
    measured against ``TYPE_MAP``, EXCLUDING structural/lifecycle tokens that
    every MonoBehaviour carries.
    """
    src = _strip_csharp_comments(csharp_source)
    # Drop ``using``/``namespace`` directives + the class's own name from the
    # surface -- they are declarations, not API references.
    own_names = {m.group(1) for m in _CLASS_DECL.finditer(src)}
    namespace_roots = {
        seg.strip()
        for line in _USING_DIRECTIVE.findall(src) + _NAMESPACE_DECL.findall(src)
        for seg in line.replace("using", "").replace("namespace", "")
        .strip(" ;").split(".")
    }
    src = _USING_DIRECTIVE.sub("", _NAMESPACE_DECL.sub("", src))

    dotted: set[str] = set()
    for m in _DOTTED_REF.finditer(src):
        ref = m.group(1)
        # A dotted ref whose root is a namespace segment (e.g.
        # ``UnityStandardAssets.Water``) is a qualified-name reference, not an
        # API call surface.
        if ref.split(".")[0] in namespace_roots:
            continue
        dotted.add(ref)
    bare: set[str] = set()
    for m in _BARE_TYPE.finditer(src):
        tok = m.group(1)
        if "." in tok or len(tok) <= 2:
            continue
        if tok in _STRUCTURAL_TOKENS or tok in own_names:
            continue
        if tok in namespace_roots:
            continue
        bare.add(tok)
    # Every segment of a dotted ref (``Shader`` AND ``EnableKeyword`` in
    # ``Shader.EnableKeyword``) is already represented by the dotted entry;
    # drop the FULL dotted member chain from bare. Stripping only the lead
    # leaked method-name tokens (``EnableKeyword``) into the bare-type surface,
    # inflating the unmapped count (a method name has no TYPE_MAP entry).
    dotted_segments = {seg for d in dotted for seg in d.split(".")}
    bare -= dotted_segments
    return dotted, bare


_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
# ``using X.Y.Z;`` / ``namespace X { ... }`` directives -- not an API surface.
_USING_DIRECTIVE = re.compile(r"^\s*using\s+[^\n;]+;", re.MULTILINE)
_NAMESPACE_DECL = re.compile(r"^\s*namespace\s+[^\n{]+", re.MULTILINE)
# The module's own type declaration -- the class name is not an API reference.
_CLASS_DECL = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)")


def _strip_csharp_comments(source: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


def _ref_resolves_to_real_mapping(ref: str) -> bool:
    """True when a C# API reference resolves to a real (non-stub) mapping.

    Checks the longest matching prefix in ``API_CALL_MAP`` (dotted calls) and
    an exact match in ``TYPE_MAP`` (types). A ``no equivalent`` / comment-stub
    value does NOT count (GF5).
    """
    # Type match (RenderTexture, Vector3, ...).
    type_val = TYPE_MAP.get(ref)
    if type_val is not None and _is_real_mapping_value(type_val):
        return True
    # Exact dotted-call match.
    call_val = API_CALL_MAP.get(ref)
    if call_val is not None and _is_real_mapping_value(call_val):
        return True
    # Prefix match: ``Camera.main.depthTextureMode`` should resolve via the
    # ``Camera.main`` key. Walk the dotted prefixes longest-first.
    parts = ref.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:cut])
        val = API_CALL_MAP.get(prefix)
        if val is not None and _is_real_mapping_value(val):
            return True
    return False


@dataclass(frozen=True)
class InputCoverage:
    """Result of the input-side mapping-coverage measurement."""

    total_refs: int
    real_refs: int
    measured: bool  # False when too few refs to judge (prior abstains)

    @property
    def real_fraction(self) -> float:
        if self.total_refs == 0:
            return 0.0
        return self.real_refs / self.total_refs

    @property
    def dead_leaning(self) -> bool:
        """True when the input prior agrees the module is dead-leaning.

        Abstains (returns False -> caller falls back to output-only) when there
        are too few refs to measure. When measured, dead-leaning means the real
        coverage is at or below the ceiling.
        """
        if not self.measured:
            return False
        return self.real_fraction <= _REAL_COVERAGE_DEAD_CEILING


def measure_input_coverage(csharp_source: str) -> InputCoverage:
    """Measure what fraction of a module's C# API surface is really mapped.

    The prior is MEASURED only when there are at least
    ``_MIN_API_REFS_FOR_INPUT_PRIOR`` distinct DOTTED API calls (the behavioral
    surface). A class with only structural boilerplate (no real calls) abstains,
    so a trivial empty MonoBehaviour is never mislabeled dead-leaning. Coverage
    is computed over dotted calls + (non-structural) bare types.
    """
    dotted, bare = _extract_csharp_api_refs(csharp_source)
    refs = dotted | bare
    total = len(refs)
    real = sum(1 for r in refs if _ref_resolves_to_real_mapping(r))
    # MEASURED when the cleaned API surface (dotted calls + non-structural bare
    # types, excluding ``using``/``namespace``/own-name) is large enough to
    # judge. A trivial empty MonoBehaviour has ~0 here and abstains.
    measured = total >= _MIN_API_REFS_FOR_INPUT_PRIOR
    return InputCoverage(total_refs=total, real_refs=real, measured=measured)


# ---------------------------------------------------------------------------
# Output-side inertness + hard veto (decisive)
# ---------------------------------------------------------------------------

# Converter-emitted deterministic dead markers (NOT regex-on-AI-output -- these
# are self-labels the transpiler stamps, see code_transpiler._inert_component_stub
# and the visual-only stub branch).
_STUB_MARKER_VISUAL = "Unity visual/rendering effect (no Roblox equivalent)"
_STUB_MARKER_INERT = "inert stub (host-instantiable, no-op)"

# A genuine Roblox effect -> hard veto. Each pattern matches executable Luau
# that mutates real Roblox state. Property writes to instances are matched as
# ``<ident>.Prop = `` where the LHS is not a local-declaration.
_VETO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bInstance\.new\b"),
    re.compile(r"\.Parent\s*="),
    # RemoteEvent / BindableEvent / RemoteFunction fire/invoke.
    re.compile(r":FireServer\b"),
    re.compile(r":FireClient\b"),
    re.compile(r":FireAllClients\b"),
    re.compile(r":Fire\b"),
    re.compile(r":Invoke\b"),
    re.compile(r":InvokeServer\b"),
    re.compile(r":InvokeClient\b"),
    # DataStore / persistence mutation.
    re.compile(r":SetAsync\b"),
    re.compile(r":UpdateAsync\b"),
    re.compile(r":IncrementAsync\b"),
    re.compile(r":RemoveAsync\b"),
    re.compile(r"GetDataStore\b"),
    # Tag / collection mutation, attribute writes, tween creation.
    re.compile(r":AddTag\b"),
    re.compile(r":SetAttribute\b"),
    re.compile(r":Create\(", ),  # TweenService:Create
    # Physics / movement application.
    re.compile(r":ApplyImpulse\b"),
    re.compile(r":Destroy\(\s*\w"),  # Destroying a real instance argument
)

# A property write to a real instance: ``obj.Prop = value`` where the receiver
# is NOT being locally declared on the same line. The receiver may be a CHAINED
# member access (``self.part.CFrame = ...``, ``workspace.CurrentCamera.FieldOfView
# = ...``), so capture the whole dotted receiver chain plus the final written
# property. Matched separately from the veto-pattern list so we can exclude
# ``local x = ...``, ``Cls.__index = Cls`` boilerplate, and the injected
# PrimaryPart fixup. Group 1 = receiver chain (may contain dots), group 2 =
# written property, group 3 = RHS.
_PROP_WRITE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^=].*)$"
)

# Boilerplate LHS targets that are class-table assignments, not instance writes.
_BOILERPLATE_PROP = frozenset({"__index", "new"})

# Converter-injected container-fixup idiom (NOT gameplay): the coherence pass
# stamps ``container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")``
# into hoisted ModuleScripts to satisfy the Model PrimaryPart contract. It is a
# deterministic guard, not a genuine gameplay effect, so it must not veto.
_INJECTED_PRIMARYPART_FIXUP = re.compile(r"FindFirstChildWhichIsA\(")


def _strip_luau_comments_and_strings(luau_source: str) -> str:
    """Blank out Luau comments + string literals so veto/effect detection does
    not match inside ``print("Instance.new")`` or ``-- .Parent =`` comments.
    """
    out: list[str] = []
    i = 0
    n = len(luau_source)
    while i < n:
        ch = luau_source[i]
        # Long comment / long string [[ ... ]] (no level support needed here).
        if luau_source.startswith("--[[", i):
            end = luau_source.find("]]", i + 4)
            i = n if end == -1 else end + 2
            out.append(" ")
            continue
        if luau_source.startswith("--", i):
            end = luau_source.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n and luau_source[i] != quote:
                if luau_source[i] == "\\":
                    i += 1
                i += 1
            i += 1
            out.append('""')
            continue
        if luau_source.startswith("[[", i):
            end = luau_source.find("]]", i + 2)
            i = n if end == -1 else end + 2
            out.append('""')
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_LUAU_LONG_COMMENT = re.compile(r"--\[\[.*?\]\]", re.DOTALL)
_LUAU_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_luau_comments_only(luau_source: str) -> str:
    """Blank Luau comments while preserving string literals (so a module name
    inside ``FindFirstChild("Name")`` survives for require-edge extraction)."""
    return _LUAU_LINE_COMMENT.sub("", _LUAU_LONG_COMMENT.sub(" ", luau_source))


def has_genuine_roblox_effect(luau_source: str) -> bool:
    """HARD VETO test: does the post-coherence Luau body do anything real?

    Operates on a comment/string-stripped copy so markers inside literals do
    not count. A single match vetoes the dead verdict.

    Matches genuine effects structurally: ``Instance.new`` / ``.Parent =`` /
    event fires / DataStore mutations (``_VETO_PATTERNS``) plus instance
    property writes -- including CHAINED receivers (``self.part.CFrame = ...``,
    ``workspace.CurrentCamera.FieldOfView = ...``) -- excluding class-table
    boilerplate (``__index`` / ``new``), local declarations, and the
    converter-injected PrimaryPart container fixup.

    Deliberately does NOT consult ``API_CALL_MAP`` / ``TYPE_MAP``: a
    genuinely-mapped Unity API the AI translates emits one of the structural
    effects above, so the brief's "mapped-API call vetoes" rule is subsumed by
    the structural test (see the module docstring).
    """
    code = _strip_luau_comments_and_strings(luau_source)
    for pat in _VETO_PATTERNS:
        if pat.search(code):
            return True
    # Instance property writes (excluding class-table boilerplate + locals +
    # the converter-injected PrimaryPart container fixup).
    for line in code.splitlines():
        m = _PROP_WRITE.match(line)
        if m is None:
            continue
        if m.group(2) in _BOILERPLATE_PROP:
            continue
        if m.group(2) == "PrimaryPart" and _INJECTED_PRIMARYPART_FIXUP.search(
            m.group(3)
        ):
            continue
        return True
    return False


def is_output_inert(luau_source: str) -> bool:
    """OUTPUT confirmation: the transpiled Luau body has no executable Roblox
    effect.

    Strong fast-path: the converter's own deterministic stub markers. Otherwise
    inertness is the absence of any genuine Roblox effect (the inverse of the
    veto). This keeps the metric structural (no fragile line-counting of AI
    output) -- a body with zero real effects IS inert.
    """
    if _STUB_MARKER_VISUAL in luau_source or _STUB_MARKER_INERT in luau_source:
        return True
    return not has_genuine_roblox_effect(luau_source)


# ---------------------------------------------------------------------------
# Input-side (C# source) veto -- used by the transpile-time gate, where no
# Luau exists yet. A C# body that creates objects, spawns prefabs, reads input,
# moves transforms, or applies physics is gameplay-bearing -> never a dead stub.
# Generic: matches behavioral C# API surface, not class names.
# ---------------------------------------------------------------------------

_CSHARP_GAMEPLAY_VETO: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bInstantiate\s*\("),
    re.compile(r"\bDestroy\s*\("),
    re.compile(r"\bGetComponent\b"),
    re.compile(r"\bAddComponent\b"),
    re.compile(r"\bOnCollision\w*\b"),
    re.compile(r"\bOnTrigger\w*\b"),
    re.compile(r"\bSendMessage\b"),
    re.compile(r"\bStartCoroutine\b"),
    re.compile(r"\bInput\.\w"),
    re.compile(r"\bKeyCode\.\w"),
    re.compile(r"\btransform\.position\b"),
    re.compile(r"\btransform\.rotation\b"),
    re.compile(r"\bAddForce\b"),
    re.compile(r"\b\w*\.velocity\b"),
    re.compile(r"\bRigidbody\b"),
)


def csharp_source_has_gameplay_effect(csharp_source: str) -> bool:
    """True when the C# source has gameplay-bearing behavior (input-side veto).

    Used by the transpile-time visual-only gate, which runs before any Luau
    exists. Comment-stripped so commented-out gameplay does not veto.
    """
    src = _strip_csharp_comments(csharp_source)
    return any(p.search(src) for p in _CSHARP_GAMEPLAY_VETO)


# ---------------------------------------------------------------------------
# Positive rendering / visual-effect API signal (input-side gate).
#
# The transpile-time stub gate is DESTRUCTIVE (it replaces a script's body with
# an inert stub BEFORE the AI ever sees it). Generic "low mapping coverage" is
# NOT a safe trigger on its own: a portable menu / save / scene controller
# (``PlayerPrefs`` + ``Application.Quit`` + ``SceneManager.LoadScene`` +
# ``Cursor.lockState``) also has low coverage yet carries real, transpilable
# behavior. So the gate additionally requires a POSITIVE rendering-API signal.
#
# CRITICAL SAFETY PROPERTY: the signal must require ACTUAL RENDERING-API USAGE
# -- a member access (``Type.member``), a method call, a render-target
# constructor, or a render lifecycle hook -- NEVER a bare type token that can
# appear in an ``using``/``namespace`` directive or a serialized FIELD
# DECLARATION. A ``MenuController`` that merely does
# ``using UnityEngine.Rendering.PostProcessing;`` or a gameplay script with only
# a serialized ``public Projector projector;`` field makes NO rendering call and
# is NOT provably dead at transpile time -- stubbing it would silently drop live
# code. Such modules, if truly inert, are caught downstream by the
# output-confirmed ``classify_module_dead``. Two layers enforce this:
#   1. ``using ...;`` / ``namespace ...`` lines are stripped before matching.
#   2. Every signal is a member-access / call / constructor / lifecycle form
#      (it includes a ``.`` member, a ``(`` call, or a hook method name) -- so a
#      bare ``Projector proj;`` declaration never matches.
#
# These are behavioral API surfaces, NOT class names, so a renamed rendering
# helper (``OceanShimmer``) is still caught by its rendering CALLS while a menu
# controller or a declaration-only field is left untouched.
# ---------------------------------------------------------------------------

# Lines whose only role is importing / namespacing a type. A bare rendering type
# token here (``using UnityEngine.Rendering.PostProcessing;``) is NOT usage.
_USING_OR_NAMESPACE_LINE = re.compile(
    r"^[ \t]*(?:using|namespace)\b[^\n]*$", re.MULTILINE
)

_RENDERING_API_SIGNALS: tuple[re.Pattern[str], ...] = (
    # --- Shader / material / low-level GPU member-access + call surfaces ------
    re.compile(r"\bShader\."),
    re.compile(r"\bMaterial\."),
    re.compile(r"\bGraphics\."),
    re.compile(r"\bGL\."),
    # ``.material`` / ``.sharedMaterial`` shader-handle access.
    re.compile(r"\.material\b"),
    re.compile(r"\.sharedMaterial\b"),
    # Material rendering-mode handle access (opaque/transparent shader path).
    re.compile(r"\.renderingMode\b"),
    # --- Render-target / command-buffer construction & usage -----------------
    # A RenderTexture CONSTRUCTOR (``new RenderTexture(`` / ``RenderTexture(``)
    # -- the bare type name alone (a field declaration) must NOT match.
    re.compile(r"\bRenderTexture\s*\("),
    re.compile(r"\bnew\s+RenderTexture\b"),
    re.compile(r"\bCommandBuffer\b"),
    re.compile(r"\.targetTexture\b"),
    re.compile(r"\bSupportsRenderTextureFormat\b"),
    # --- Camera / render-state member-access ---------------------------------
    re.compile(r"\.depthTextureMode\b"),
    re.compile(r"\.cullingMask\b"),
    re.compile(r"\.invertCulling\b"),
    re.compile(r"\.maximumLOD\b"),
    re.compile(r"\bCamera\.Render\b"),
    re.compile(r"\.Render\s*\("),
    # Global render state / environment (fog, ambient, skybox material, etc.).
    re.compile(r"\bRenderSettings\."),
    re.compile(r"\bLightmapSettings\."),
    # --- Render lifecycle hooks (declaring one IS rendering behavior) --------
    re.compile(r"\bOnWillRenderObject\b"),
    re.compile(r"\bOnRenderImage\b"),
    re.compile(r"\bOnPreRender\b"),
    re.compile(r"\bOnPostRender\b"),
    re.compile(r"\bOnPreCull\b"),
)


def csharp_source_has_rendering_api(csharp_source: str) -> bool:
    """True when the C# source actually USES a GPU / shader / camera-effect /
    render-target API that has no Roblox equivalent (input-side POSITIVE
    rendering signal).

    Requires real USAGE -- a member access, a method call, a render-target
    constructor, or a render lifecycle hook -- NEVER a bare type token in an
    ``using``/``namespace`` import or a serialized field DECLARATION. Comments
    and import/namespace lines are stripped first, and every signal is a
    member-access / call / lifecycle form, so a declaration-only field
    (``public Projector projector;``) does not trip the gate. This is the
    decisive safety property of the destructive transpile-time stub gate.
    """
    src = _USING_OR_NAMESPACE_LINE.sub("", _strip_csharp_comments(csharp_source))
    return any(p.search(src) for p in _RENDERING_API_SIGNALS)


def is_input_side_dead(csharp_source: str) -> bool:
    """Transpile-time (input-only) dead verdict for the visual-only gate.

    Dead-leaning iff ALL of: the C# has at least one POSITIVE rendering / visual
    -effect API signal (a GPU/shader/camera-effect/render-target API with no
    Roblox equivalent) AND no gameplay veto AND mapping coverage is dead-leaning.

    The positive rendering signal is decisive for SAFETY: this gate is
    destructive (it stubs the body before AI transpilation), so generic low
    coverage alone is NOT enough -- a portable menu / save / scene controller
    (``PlayerPrefs`` / ``SceneManager.LoadScene`` / ``Application.Quit`` /
    ``Cursor.lockState``) also has low coverage but must NOT be stubbed. The
    rendering signal isolates true rendering helpers. This REPLACES the old
    hardcoded class-name list with a generic behavioral test; the decisive
    output-side confirmation runs later in ``classify_module_dead``
    (post-coherence). A renamed rendering helper (``OceanShimmer``) with the
    same body shape is caught here by behavior, not name.

    BEST-EFFORT GATE (NOT the decisive authority). This input-only test is a
    transpile-time OPTIMIZATION: it lets the converter stub an obviously-dead
    rendering helper before spending an AI transpile on it, and -- decisively --
    it keeps the generic ``rule_based`` -> ``strategy="stub"`` fallback from
    mislabeling a real visual helper as a non-visual stub when AI is unavailable
    (the only edge this allowlist actually guards). The ``_RENDERING_API_SIGNALS``
    allowlist is therefore broad-but-finite and WILL miss some rendering APIs.
    That is acceptable: a rendering helper that slips this gate is still caught by
    the post-coherence, output-confirmed ``classify_module_dead`` for routing /
    prune. The decisive authority is ALWAYS the output-side D3 verdict, never this
    input-only allowlist.
    """
    if csharp_source_has_gameplay_effect(csharp_source):
        return False
    if not csharp_source_has_rendering_api(csharp_source):
        return False
    return measure_input_coverage(csharp_source).dead_leaning


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeadVerdict:
    """Per-module Roblox-dead verdict + the evidence behind it."""

    name: str
    is_dead: bool
    input_coverage: InputCoverage
    output_inert: bool
    vetoed: bool
    reason: str


def classify_module_dead(
    name: str,
    *,
    csharp_source: str,
    luau_source: str,
) -> DeadVerdict:
    """Decide whether a single module is Roblox-dead (D3 + hard veto).

    Dead iff the INPUT prior is MEASURED AND dead-leaning AND OUTPUT is inert
    AND no veto. The output confirmation is decisive in the negative direction
    (a module with a real effect is never dead), but it is NOT sufficient on its
    own: a module whose input prior ABSTAINS (too few API refs to measure)
    returns ``is_dead=False`` even on a fully-inert body -- abstention never
    licenses a dead verdict (it would over-flag content-free modules whose body
    is a valid-but-trivial ``return M``).
    """
    coverage = measure_input_coverage(csharp_source)
    vetoed = has_genuine_roblox_effect(luau_source)
    output_inert = (not vetoed) and is_output_inert(luau_source)

    if vetoed:
        return DeadVerdict(
            name=name, is_dead=False, input_coverage=coverage,
            output_inert=False, vetoed=True,
            reason="veto: body contains a genuine Roblox effect",
        )
    if not output_inert:
        return DeadVerdict(
            name=name, is_dead=False, input_coverage=coverage,
            output_inert=False, vetoed=False,
            reason="output not inert (executable Roblox body)",
        )
    # Output is inert + no veto. The INPUT prior must AGREE (both-agree, D3):
    # a measured, dead-leaning API surface (dominated by unmapped APIs). An
    # abstaining prior (too few API refs to measure -- e.g. an empty/trivial
    # module) is NOT a dead verdict: it would over-flag content-free modules
    # whose body happens to be a valid-but-trivial ``return M``.
    if not coverage.measured:
        return DeadVerdict(
            name=name, is_dead=False, input_coverage=coverage,
            output_inert=True, vetoed=False,
            reason=(
                "input prior abstains (too few API refs to judge); "
                "not flagged dead on output alone"
            ),
        )
    if not coverage.dead_leaning:
        return DeadVerdict(
            name=name, is_dead=False, input_coverage=coverage,
            output_inert=True, vetoed=False,
            reason=(
                f"input prior disagrees: real mapping coverage "
                f"{coverage.real_fraction:.0%} > {_REAL_COVERAGE_DEAD_CEILING:.0%}"
            ),
        )
    return DeadVerdict(
        name=name, is_dead=True, input_coverage=coverage,
        output_inert=True, vetoed=False,
        reason="dead: inert output + dead-leaning input prior",
    )


# ---------------------------------------------------------------------------
# Require-closure (prune safety)
# ---------------------------------------------------------------------------

# Injected require edge shape (script_coherence._module_require_body +
# contract pipeline resolve_requires + autogen / scaffolding emitted bodies):
# all forms reference the target module name inside a lookup-method call
# (``FindFirstChild`` / ``WaitForChild`` / ``FindFirstDescendant``) wrapped in
# ``require(...)``. ``WaitForChild`` is a real emitted module-require shape
# (shared_state_linter.py, autogen.py, scaffolding/fps.py), so the lookup method
# must be generalized -- a hardcoded ``FindFirstChild(`` misses those edges and
# a live module requiring a dead one via ``WaitForChild`` would be invisible
# (false-prune -> ``require(nil)`` crash).
_REQUIRE_EDGE = re.compile(
    r"require\s*\([\s\S]*?"
    r"(?:FindFirstChild|WaitForChild|FindFirstDescendant)\("
    r"\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']"
)
# Also catch dotted requires ``require(game.ReplicatedStorage.Name)`` /
# ``require(workspace.Foo.Inner)`` used in some legacy fixtures + emitted bodies.
# Capture EVERY dotted segment after the require root so the module-name segment
# can be matched against ``known_names`` (``workspace.Foo.Inner`` captures both
# ``Foo`` and ``Inner``; the ``known_names`` filter keeps only the real module
# name, accepting the safe over-capture).
_REQUIRE_DOTTED = re.compile(
    r"require\s*\(\s*[A-Za-z_][\w.():\"'\s]*?\)"
)
_DOTTED_SEGMENT = re.compile(r"\.([A-Za-z_][A-Za-z0-9_]*)")


def extract_require_edges(luau_source: str, known_names: frozenset[str]) -> set[str]:
    """Return the set of module names this Luau body ``require``s.

    Parses the FINAL EMITTED LUAU injected-require shape (NOT ``dependency_map``)
    so post-transpile injected edges are captured (LOCKED DECISION: closure from
    emitted Luau). Only names present in ``known_names`` are returned (drops
    runtime / service requires like ``ReplicatedStorage`` or ``@scene_runtime/``).

    Recognises every emitted lookup-method form -- ``FindFirstChild`` /
    ``WaitForChild`` / ``FindFirstDescendant`` -- plus dotted-path requires.

    Strips comments only (NOT string literals) -- the module name lives inside a
    ``FindFirstChild("Name")`` string literal, so it must survive. Comments are
    blanked so a commented-out require does not register an edge.
    """
    code = _strip_luau_comments_only(luau_source)
    edges: set[str] = set()
    for m in _REQUIRE_EDGE.finditer(code):
        nm = m.group(1)
        if nm in known_names:
            edges.add(nm)
    for m in _REQUIRE_DOTTED.finditer(code):
        # Match each dotted segment against known module names. The require
        # expression may name services / globals (``game``, ``workspace``,
        # ``ReplicatedStorage``) interleaved with the module name; only the
        # segment(s) in ``known_names`` register an edge.
        for seg in _DOTTED_SEGMENT.finditer(m.group(0)):
            nm = seg.group(1)
            if nm in known_names:
                edges.add(nm)
    return edges


@dataclass
class ClosurePruneResult:
    """Which dead modules are safe to prune vs must stay inert."""

    prunable: set[str] = field(default_factory=set)
    keep_inert: set[str] = field(default_factory=set)


def compute_prunable_dead(
    dead_names: frozenset[str],
    require_edges: dict[str, set[str]],
) -> ClosurePruneResult:
    """Partition dead modules into safe-to-prune vs must-stay-inert.

    A dead module is safe to prune ONLY when it is NOT (transitively) reachable
    via require-edges from any NON-pruned module -- i.e. from any LIVE (non-dead)
    module OR from any kept-inert dead module (LOCKED DECISION / GF8). Checking
    only DIRECT callers is insufficient: with edges ``live->A->B`` (both dead),
    A has a live caller so it stays kept-inert, but A still ``require``s B at
    module scope -- pruning B would turn A's surviving require into
    ``require(nil)`` and hard-crash. B must therefore be protected too.

    Algorithm (transitive closure over forward require-edges):
      1. Seed ``protected`` = dead modules DIRECTLY required by a live module.
      2. Transitively add every dead module forward-required by a protected
         module (a kept-inert module's requires must all survive).
      3. ``keep_inert`` = protected set; ``prunable`` = dead modules not
         protected (a fully-isolated dead closure with no live requirer
         anywhere).

    Args:
        dead_names: the set of Roblox-dead module names.
        require_edges: ``module_name -> set(names it requires)`` from the final
            emitted Luau (``extract_require_edges`` per module).

    Returns:
        ClosurePruneResult: ``prunable`` (drop from output) +
        ``keep_inert`` (dead but transitively required by a surviving module).
    """
    # Seed: dead modules DIRECTLY required by a LIVE (non-dead) module.
    protected: set[str] = set()
    for caller, callees in require_edges.items():
        if caller in dead_names:
            continue  # only live callers seed protection
        for callee in callees:
            if callee in dead_names:
                protected.add(callee)

    # Transitively protect every dead module forward-required by a protected
    # module: a kept-inert module keeps its module-scope ``require`` edges, so
    # each callee it requires must also survive.
    frontier = list(protected)
    while frontier:
        cur = frontier.pop()
        for callee in require_edges.get(cur, set()):
            if callee in dead_names and callee not in protected:
                protected.add(callee)
                frontier.append(callee)

    result = ClosurePruneResult()
    result.keep_inert = set(protected)
    result.prunable = {name for name in dead_names if name not in protected}
    return result
