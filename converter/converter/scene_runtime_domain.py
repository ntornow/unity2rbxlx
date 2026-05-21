"""scene_runtime_domain.py -- v2 execution-domain classifier for the
contract pipeline.

Runs after Phase 4a's storage classifier and after the PR1 planner has
seeded ``scene_runtime.modules``. For every runtime-bearing module
this assigns one of:

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
from typing import Iterable, TypedDict, cast

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
from converter.storage_classifier import (
    REPLICATED_STORAGE,
    SERVER_STORAGE,
)

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


# ---------------------------------------------------------------------------
# C# signal tables (v2 classifier).
#
# Patterns operate on the raw C# source text. Each row is
# (regex, signal_name, mirror_only). Mirror-only rows fire only when
# ``networking`` is ``"mirror"`` or ``"netcode"``; under ``"none"`` they
# are ignored.
# ---------------------------------------------------------------------------

_CSharpPattern = tuple[str, str, bool]


_CS_STRONG_CLIENT: tuple[_CSharpPattern, ...] = (
    # Mirror / Netcode client-side annotations.
    (r"\[ClientRpc\b", "ClientRpc", True),
    (r"\[Client\b\]", "ClientAttribute", True),
    (r"\[ClientCallback\b", "ClientCallback", True),
    # Unity UI namespace imports.
    (r"^\s*using\s+UnityEngine\.UI\s*;", "using_UnityEngine_UI", False),
    (r"^\s*using\s+TMPro\s*;", "using_TMPro", False),
    (r"^\s*using\s+UnityEngine\.EventSystems\s*;",
     "using_UnityEngine_EventSystems", False),
    # Input
    (r"\bInput\.(?:Get[A-Z]\w*|mousePosition|mouseScrollDelta|"
     r"touchCount|GetTouch|anyKey\b|anyKeyDown\b)",
     "Input_Get", False),
    # OnGUI methods (UI immediate-mode rendering — client-only).
    (r"\bvoid\s+OnGUI\s*\(\s*\)", "OnGUI_method", False),
    # PlayerPrefs
    (r"\bPlayerPrefs\.\w+\s*\(", "PlayerPrefs", False),
    # Cursor / Screen / Application.platform — client-only Unity APIs.
    (r"\bCursor\.(?:visible|lockState|SetCursor)\b", "Cursor_API", False),
    (r"\bScreen\.(?:width|height|fullScreen|orientation|"
     r"currentResolution|resolutions)\b",
     "Screen_API", False),
    (r"\bApplication\.platform\b", "Application_platform", False),
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
    # idiom is overwhelmingly client.
    (r"\bCamera\.main\b", "Camera_main", False),
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


# Moderate server signals are derived at the class-graph level (e.g.,
# require-graph reaches a NetworkBehaviour subclass) so they're not
# table-driven here. Reserved for future expansion.
_CS_MODERATE_SERVER: tuple[_CSharpPattern, ...] = ()


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


# Project-level Mirror/Netcode `using` import check for the
# `mirror_adoption_low` heuristic.
_RE_USING_MIRROR = re.compile(
    r"^\s*using\s+(?:Mirror|Unity\.Netcode)\b", re.MULTILINE,
)


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
) -> DomainClassifierReport:
    """Populate ``domain`` / ``container`` / ``module_path`` /
    ``domain_signals`` on every runtime-bearing module in
    ``scene_runtime.modules`` (mutated in place).

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
    ``scene_runtime.modules[*]``.
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

    scripts_by_class: dict[str, RbxScript] = {}
    for script in scripts:
        if script.name:
            scripts_by_class.setdefault(script.name, script)

    per_instance_evidence = _gather_per_instance_evidence(scenes, prefabs)

    # C# source text cached per script_id. We read on demand the first
    # time a module is classified; helper modules + non-runtime-bearing
    # rows don't pay the I/O.
    cs_source_cache: dict[str, str] = {}

    def _get_cs_source(script_id: str) -> str:
        if script_id in cs_source_cache:
            return cs_source_cache[script_id]
        text = _load_cs_source(script_id, guid_index)
        cs_source_cache[script_id] = text
        return text

    displaced: list[SceneRuntimeDisplacedInstance] = []
    low_confidence: list[str] = []
    excluded: list[str] = []

    # Pass 1: per-module classification (signals → rule table → override).
    for script_id, module in modules.items():
        # Pre-stamp helpers + non-runtime-bearing rows. Helpers (not
        # runtime-bearing) become ``"helper"``; the host runtime never
        # instantiates them but the module row still exists for require()
        # resolution.
        if not module.get("runtime_bearing"):
            # Don't overwrite a pre-existing helper marker on re-classify.
            module["domain"] = "helper"
            _stamp_container_and_path(module, scripts_by_class)
            continue

        verdict, signals, instance_rows = _classify_module(
            script_id, module, scripts_by_class,
            per_instance_evidence.get(script_id, []),
            overrides.get(script_id),
            _get_cs_source(script_id),
            networking,
        )
        module["domain"] = verdict
        module["domain_signals"] = signals
        _stamp_container_and_path(module, scripts_by_class)
        if signals.get("low_confidence"):
            low_confidence.append(script_id)
        if verdict == "excluded":
            excluded.append(script_id)
        if signals.get("override_applied") and signals.get("intra_class_conflict"):
            for row in instance_rows:
                displaced.append(row)

    # Pass 2: client-domain require-graph reachability.
    if dependency_map:
        _apply_reachability_rule(
            modules, dependency_map, scripts_by_class, excluded,
        )

    # Pass 3: mirror_adoption_low heuristic (after classification so we
    # have the runtime-bearing count post-overrides).
    mirror_low = False
    if networking in ("mirror", "netcode"):
        mirror_low = _check_mirror_adoption(
            modules, scripts_by_class, cs_source_cache, _get_cs_source,
        )

    # Pass 4: strict-classification violations enumeration. Always
    # computed, never raised — callers decide policy.
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
# Cross-domain edge enumeration (kept from PR3b; updated to skip
# ``"excluded"`` modules instead of ``"legacy"``).
# ---------------------------------------------------------------------------

class CrossDomainEdge(TypedDict):
    """One client<->server reference identified at conversion time."""

    from_instance: str
    to_instance: str
    from_script: str
    to_script: str
    field: str
    from_domain: str
    to_domain: str
    owner_kind: str
    owner_ref: str


# Domain values the cross-domain pass refuses to wire through.
# - Helpers don't run lifecycle, so they can't generate cross-domain refs.
# - Excluded modules aren't instantiated at all.
# - ``"legacy"`` is the pre-classifier-v2 spelling; preserved here as a
#   defensive skip for any on-disk plan that the migration pass hasn't
#   already rewritten.
_NON_RUNTIME_DOMAINS: frozenset[str] = frozenset(
    {"", "helper", "excluded", "legacy"},
)


def compute_cross_domain_edges(
    scene_runtime: SceneRuntimeArtifact,
) -> list[CrossDomainEdge]:
    """Enumerate every cross-domain serialized reference in the plan.

    A reference qualifies iff:
      - ``target_kind == "component"`` (peer-MonoBehaviour ref)
      - Both source and target instances resolve to modules with
        execution domains in ``{"client", "server"}``
      - The two domains differ

    Pure function; does not mutate ``scene_runtime``.
    """
    modules = scene_runtime.get("modules", {})
    scenes = scene_runtime.get("scenes", {})
    prefabs = scene_runtime.get("prefabs", {})

    instance_to_script: dict[str, str] = {}
    for scene in scenes.values():
        for inst in scene.get("instances", []):
            instance_to_script[inst["instance_id"]] = inst["script_id"]
    for prefab in prefabs.values():
        for inst in prefab.get("instances", []):
            instance_to_script[inst["instance_id"]] = inst["script_id"]

    out: list[CrossDomainEdge] = []

    def _scan(
        owner_kind: str,
        owner_ref: str,
        references: list[SceneRuntimeReference],
    ) -> None:
        for ref in references:
            if ref.get("target_kind") != "component":
                continue
            src_inst = ref.get("from", "")
            tgt_inst = ref.get("target_ref", "")
            src_sid = instance_to_script.get(src_inst, "")
            tgt_sid = instance_to_script.get(tgt_inst, "")
            if not src_sid or not tgt_sid:
                continue
            src_mod = modules.get(src_sid, {})
            tgt_mod = modules.get(tgt_sid, {})
            src_domain = src_mod.get("domain", "")
            tgt_domain = tgt_mod.get("domain", "")
            if (src_domain in _NON_RUNTIME_DOMAINS
                or tgt_domain in _NON_RUNTIME_DOMAINS):
                continue
            if src_domain == tgt_domain:
                continue
            out.append(CrossDomainEdge(
                from_instance=src_inst,
                to_instance=tgt_inst,
                from_script=src_sid,
                to_script=tgt_sid,
                field=ref.get("field", ""),
                from_domain=src_domain,
                to_domain=tgt_domain,
                owner_kind=owner_kind,
                owner_ref=owner_ref,
            ))

    for key, scene in scenes.items():
        _scan("scene", key, scene.get("references", []))
    for key, prefab in prefabs.items():
        _scan("prefab", key, prefab.get("references", []))

    return out


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
) -> tuple[str, SceneRuntimeDomainSignals, list[SceneRuntimeDisplacedInstance]]:
    """Return ``(domain, signals, displaced_instances)`` for one module."""
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
            # Clear fail_closed_reason if we're routing off excluded.
            if override != "excluded":
                signals.pop("fail_closed_reason", None)
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
) -> _SignalCounts:
    """Aggregate strong/moderate signal hits across all three channels.

    Each signal kind counts at most once. Same kind firing from multiple
    channels (e.g., both C# and Luau pinning client) still counts once
    per channel listing but contributes only once to the strong/moderate
    bucket count.
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
        if any(rx.search(luau_source) for rx in _CLIENT_RX):
            luau_signals.append("roblox_client_api")
            strong_client_kinds.add("roblox_client_api")
        if any(rx.search(luau_source) for rx in _SERVER_RX):
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
    if guid_index is None or not script_id:
        return ""
    try:
        path: Path | None = guid_index.resolve(script_id)
    except Exception:
        return ""
    if path is None or path.suffix != ".cs":
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


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
    if script is None:
        return
    container = script.parent_path or ""
    if container:
        module["container"] = container
    if script.name and container:
        module["module_path"] = f"{container}.{script.name}"


# ---------------------------------------------------------------------------
# Reachability rule (client require graph must not reach ServerStorage)
# ---------------------------------------------------------------------------

def _apply_reachability_rule(
    modules: dict[str, SceneRuntimeModule],
    dependency_map: dict[str, list[str]],
    scripts_by_class: dict[str, RbxScript],
    excluded: list[str],
) -> None:
    """For every client-domain module, walk its transitive require graph.

    Helpers required by client modules are forced to ``ReplicatedStorage``;
    a conflict (same helper required by both sides AND parked in
    ``ServerStorage``) excludes the helper from the runtime plan.
    """
    client_classes: set[str] = set()
    server_classes: set[str] = set()
    class_to_script_id: dict[str, str] = {}
    for script_id, module in modules.items():
        class_name = module.get("class_name", "")
        if not class_name:
            continue
        class_to_script_id.setdefault(class_name, script_id)
        verdict = module.get("domain")
        if verdict == "client":
            client_classes.add(class_name)
        elif verdict == "server":
            server_classes.add(class_name)

    for helper_class, script in scripts_by_class.items():
        client_seeds = client_classes - {helper_class}
        server_seeds = server_classes - {helper_class}
        helper_reached_by_client = (
            helper_class in _closure(client_seeds, dependency_map)
        )
        helper_reached_by_server = (
            helper_class in _closure(server_seeds, dependency_map)
        )
        if not helper_reached_by_client:
            continue
        current_container = script.parent_path or ""
        if current_container == SERVER_STORAGE:
            if helper_reached_by_server:
                # Conflict: both sides want this helper.
                module_id = class_to_script_id.get(helper_class)
                if module_id and module_id in modules:
                    module_row = modules[module_id]
                    module_row["domain"] = "excluded"
                    signals = cast(
                        SceneRuntimeDomainSignals,
                        module_row.get("domain_signals", {}),
                    )
                    signals["fail_closed_reason"] = "reachability_conflict"
                    module_row["domain_signals"] = signals
                    if module_id not in excluded:
                        excluded.append(module_id)
                continue
            # Client-only-reach: hoist to ReplicatedStorage.
            script.parent_path = REPLICATED_STORAGE
            module_id = class_to_script_id.get(helper_class)
            if module_id and module_id in modules:
                module_row = modules[module_id]
                module_row["container"] = REPLICATED_STORAGE
                signals = cast(
                    SceneRuntimeDomainSignals,
                    module_row.get("domain_signals", {}),
                )
                signals["reachability_forced_container"] = REPLICATED_STORAGE
                module_row["domain_signals"] = signals


def _closure(
    seeds: set[str], dependency_map: dict[str, list[str]],
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
    "compute_cross_domain_edges",
    "migrate_legacy_domain_values",
    "DomainClassifierReport",
    "CrossDomainEdge",
    "NetworkingMode",
    "NETWORKING_MODES",
    "DEFAULT_NETWORKING_MODE",
    "_GENERIC_CLIENT_API_PATTERNS",
    "_GENERIC_SERVER_API_PATTERNS",
)
