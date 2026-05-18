"""
code_transpiler.py -- C# to Luau transpilation with dual strategy.

Provides both a fast rule-based regex transpiler and an AI-powered transpiler
(via Claude API). The rule-based approach handles common Unity API patterns;
the AI transpiler handles complex logic and edge cases.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config import (
    ANTHROPIC_MODEL,
    ANTHROPIC_MAX_TOKENS,
    LLM_CACHE_DIR,
    LLM_CACHE_ENABLED,
    LLM_CACHE_TTL_SECONDS,
    TRANSPILATION_CONFIDENCE_THRESHOLD,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TranspiledScript:
    """Result of transpiling a single C# script to Luau."""
    source_path: str
    output_filename: str
    csharp_source: str
    luau_source: str
    strategy: str  # "rule_based", "ai", "hybrid"
    confidence: float  # 0.0 - 1.0
    warnings: list[str] = field(default_factory=list)
    flagged_for_review: bool = False
    script_type: str = "Script"  # Script, LocalScript, ModuleScript


@dataclass
class TranspilationResult:
    """Aggregate result of transpiling all scripts in a project."""
    scripts: list[TranspiledScript] = field(default_factory=list)
    total_transpiled: int = 0
    total_rule_based: int = 0
    total_ai: int = 0
    total_failed: int = 0
    total_flagged: int = 0
    # Orphan :GetAttribute() reads found by the post-transpile shared-state
    # linter, in UNCONVERTED.md entry shape ({category, item, reason}).
    shared_state_warnings: list[dict[str, str]] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Summary counts for reporting."""
        return {
            "total": self.total_transpiled,
            "rule_based": self.total_rule_based,
            "ai": self.total_ai,
            "failed": self.total_failed,
            "flagged": self.total_flagged,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def transpile_scripts(
    unity_project_path: str | Path,
    script_infos: list[Any],
    use_ai: bool = True,
    api_key: str = "",
    max_concurrent: int = 10,
    serialized_field_refs: dict[str, dict[str, str]] | None = None,
) -> TranspilationResult:
    """Transpile a list of C# scripts to Luau.

    Args:
        unity_project_path: Root of the Unity project (for relative path resolution).
        script_infos: List of ScriptInfo objects from the script analyzer.
        use_ai: Whether to attempt AI transpilation for low-confidence scripts.
        api_key: Anthropic API key (required if use_ai is True).
        max_concurrent: Max concurrent API calls for AI transpilation.
        serialized_field_refs: Phase 4.9 output — ``{relative_cs_path:
            {field_name: prefab_or_audio_ref}}``. When provided, each
            script's prompt gets the relevant subset appended so the AI
            can emit real ``ReplicatedStorage.Templates:WaitForChild(...)``
            calls for inspector-assigned prefab fields instead of ``nil``.

    Returns:
        TranspilationResult with all transpiled scripts and summary counts.
    """
    result = TranspilationResult()

    # Build project context for AI transpilation
    project_context = _build_project_context(script_infos)
    serialized_field_refs = serialized_field_refs or {}

    # Phase 1: Classify scripts and read source
    pending_scripts: list[tuple[Any, str, str]] = []  # (info, csharp_source, script_type)
    for info in script_infos:
        script_path = info.path
        try:
            csharp_source = script_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.warning("Could not read script %s: %s", script_path, exc)
            result.total_failed += 1
            continue

        # Auto-stub visual/rendering scripts that can't work in Roblox
        if _is_visual_only_script(script_path, csharp_source):
            result.scripts.append(TranspiledScript(
                source_path=str(script_path),
                output_filename=script_path.stem + ".luau",
                csharp_source=csharp_source,
                luau_source=f'-- {script_path.stem}: Unity visual/rendering effect (no Roblox equivalent)\nprint("{script_path.stem} loaded")',
                strategy="stub",
                confidence=1.0,
                script_type="Script",
            ))
            result.total_transpiled += 1
            result.total_rule_based += 1
            log.info("  %s: auto-stubbed (visual/rendering only)", script_path.name)
            continue

        script_type = _classify_script_type(csharp_source, info)
        pending_scripts.append((info, csharp_source, script_type))

    # Phase 2: AI transpilation — dependency-aware, processed in level order
    # so each script's prompt can include the already-transpiled Luau of
    # its direct dependencies (Phase 4.3.1). Within a level scripts have
    # no dep on each other, so concurrent execution is preserved.
    #
    # Key on the disambiguated stem (which is unique per script) rather
    # than ``info.class_name``. Two ``Utils.cs`` files in different
    # folders share a class name; storing AI outputs by ``class_name``
    # alone would overwrite the first script's Luau with the second's
    # — Phase 3 then handed the same source file to both. The stem
    # already has path-suffix disambiguation built in (lines below).
    ai_results: dict[str, tuple[str, float, list[str]]] = {}

    # Map every pending script to a unique stem so the dep-graph code has
    # a stable key. Prefer the class name (keeps cross-reference matching
    # idiomatic); when two scripts share one, disambiguate by appending a
    # short path-based suffix so the second script still gets its own AI
    # pass — silently dropping duplicates would regress to the pre-PR-4
    # behaviour for legitimate cases like two Utils.cs in different dirs.
    file_sources: dict[str, str] = {}
    info_by_stem: dict[str, tuple[Any, str, str]] = {}
    # Phase 3 needs to look up the AI result for each (info, csharp, type)
    # tuple it processes; that lookup must hit the same stem we stored
    # under, so remember stem-by-path here.
    stem_by_path: dict[str, str] = {}
    for info, csharp_source, script_type in pending_scripts:
        base_stem = info.class_name or info.path.stem
        stem = base_stem
        if stem in file_sources:
            suffix = hashlib.sha1(str(info.path).encode()).hexdigest()[:6]
            stem = f"{base_stem}__{suffix}"
            log.debug(
                "Duplicate stem %r; disambiguated as %r (path=%s)",
                base_stem, stem, info.path,
            )
        file_sources[stem] = csharp_source
        info_by_stem[stem] = (info, csharp_source, script_type)
        stem_by_path[str(info.path)] = stem

    dep_graph, _class_map = _build_dependency_graph(file_sources)
    dep_levels = _compute_dependency_levels(dep_graph)
    transpiled_luau: dict[str, str] = {}  # stem → luau, grows across levels

    if use_ai and pending_scripts:
        backend = _find_transpiler() if not api_key else "anthropic_api"

        def _transpile_one(stem: str) -> tuple[str, tuple[str, float, list[str]] | None]:
            triple = info_by_stem.get(stem)
            if triple is None:
                return stem, None
            info, csharp_source, script_type = triple
            scoped = _build_scoped_context(stem, dep_graph, file_sources, transpiled_luau)
            field_ctx = _build_serialized_field_context(
                info.path, unity_project_path, serialized_field_refs,
            )
            context_parts = [project_context, scoped, field_ctx]
            context = "\n\n".join(p for p in context_parts if p)
            try:
                if backend == "claude_cli":
                    luau, confidence, warnings = _claude_cli_transpile(
                        csharp_source,
                        class_name=info.class_name,
                        script_type=script_type,
                        project_context=context,
                    )
                elif backend == "anthropic_api" and api_key:
                    luau, confidence, warnings = _ai_transpile(
                        csharp_source, api_key, ANTHROPIC_MODEL,
                        class_name=info.class_name,
                        script_type=script_type,
                        project_context=context,
                    )
                else:
                    return stem, None
                return stem, (luau, confidence, warnings)
            except Exception as exc:
                log.warning("AI transpilation failed for %s: %s",
                            info.path.name, exc)
                return stem, None

        can_parallelize = backend == "anthropic_api" and api_key and len(pending_scripts) > 1
        for level_idx, level_stems in enumerate(dep_levels):
            if not level_stems:
                continue
            log.info("[transpile] Level %d/%d: %d scripts",
                     level_idx + 1, len(dep_levels), len(level_stems))
            if can_parallelize and len(level_stems) > 1:
                from concurrent.futures import ThreadPoolExecutor, as_completed
                with ThreadPoolExecutor(
                    max_workers=min(max_concurrent, len(level_stems)),
                ) as executor:
                    futures = {
                        executor.submit(_transpile_one, stem): stem
                        for stem in level_stems
                    }
                    for future in as_completed(futures):
                        stem, outcome = future.result()
                        if outcome is None:
                            continue
                        info = info_by_stem[stem][0]
                        luau, confidence, warnings = outcome
                        ai_results[stem] = outcome
                        # Only feed high-enough-confidence Luau into the
                        # dep-context cache; Phase 3 replaces anything
                        # under 0.1 with a stub, so publishing it here
                        # would give later dependents a prompt grounded
                        # in methods that never actually land on disk.
                        if luau and confidence >= 0.1:
                            transpiled_luau[stem] = luau
                        result.total_ai += 1
                        log.info("  %s: transpiled via Anthropic API (confidence %.2f)",
                                 info.path.name, confidence)
            else:
                for stem in level_stems:
                    stem_out, outcome = _transpile_one(stem)
                    if outcome is None:
                        continue
                    info = info_by_stem[stem_out][0]
                    luau, confidence, warnings = outcome
                    ai_results[stem_out] = outcome
                    if luau and confidence >= 0.1:
                        transpiled_luau[stem_out] = luau
                    result.total_ai += 1
                    log.info("  %s: transpiled via %s (confidence %.2f)",
                             info.path.name, backend, confidence)

    # Phase 3: Assemble results, falling back to rule-based where AI didn't produce output.
    # Use the disambiguated stem (path-keyed) so two scripts with the same
    # class name don't collide on lookup.
    for info, csharp_source, script_type in pending_scripts:
        key = stem_by_path.get(str(info.path), info.class_name or str(info.path))
        luau, confidence, warnings, strategy = "", 0.0, [], "rule_based"

        if key in ai_results:
            luau, confidence, warnings = ai_results[key]
            strategy = "ai"

        # Pre-AI pattern warnings — prepend before any AI/stub warnings
        # so a reader sees structural hazards first. The patterns don't
        # depend on transpile output and apply uniformly across rule-
        # based, AI, and stub strategies.
        pattern_warnings = _analyze_csharp_patterns(csharp_source)
        if pattern_warnings:
            warnings = pattern_warnings + warnings

        # Fall back to stub if AI didn't run or failed. The stub comments
        # out the original C# as reference and generates a minimal module
        # skeleton. This is intentionally minimal — AI transpilation is the
        # primary path and the stub is only a safety net for offline/no-key
        # scenarios. The old regex-based transpiler was removed because AI
        # produces superior output and the regex patterns were a maintenance
        # burden (catastrophic backtracking, 500+ lines of fragile patterns).
        if not luau or confidence < 0.1:
            from converter.stub_generator import generate_stub
            luau = generate_stub(csharp_source, info)
            strategy = "stub"
            confidence = 0.3
            result.total_rule_based += 1
            warnings.append("AI unavailable — used stub generator (run with API key for full transpilation)")

        # Phase 4.4: surface C# methods missing from the Luau output
        # as warnings. Only runs on AI-transpiled scripts — stubs and
        # rule-based output don't round-trip C# methods meaningfully.
        if strategy == "ai" and luau:
            from converter.transpile_diagnostics import check_method_completeness
            missing = check_method_completeness(
                csharp_source, luau, source_name=info.path.name,
            )
            if missing:
                warnings.extend(missing)

        # Flag for manual review if confidence is still low.
        flagged = confidence < TRANSPILATION_CONFIDENCE_THRESHOLD

        output_filename = info.path.stem + ".luau"

        ts = TranspiledScript(
            source_path=str(info.path),
            output_filename=output_filename,
            csharp_source=csharp_source,
            luau_source=luau,
            strategy=strategy,
            confidence=confidence,
            warnings=warnings,
            flagged_for_review=flagged,
            script_type=script_type,
        )
        result.scripts.append(ts)
        result.total_transpiled += 1
        if flagged:
            result.total_flagged += 1

    log.info(
        "Transpilation complete: %d total, %d rule-based, %d AI, %d flagged, %d failed",
        result.total_transpiled,
        result.total_rule_based,
        result.total_ai,
        result.total_flagged,
        result.total_failed,
    )
    return result


# ---------------------------------------------------------------------------
# Phase 4.3.1 — dependency-aware context
#
# The old flow fed every script the same flat "project classes" list. That left
# the AI blind to sibling scripts' exports, so cross-script state guesses
# (Door reading ``character:GetAttribute("hasKey")`` while Player keeps a
# module-local ``gotKey``) silently split into two channels that nothing
# bridged. See TODO.md "Cross-script shared-state transpilation gap".
#
# These helpers build a C#-level dependency graph from the pending script
# sources, topologically sort it (alphabetical tie-break for determinism),
# and produce a per-script scoped context that includes every direct
# dependency's already-transpiled Luau plus 1-hop transitive signatures.
# ---------------------------------------------------------------------------


# ``/* ... */`` and ``// ...`` comments, plus ``"..."`` / ``'...'`` string
# literals, must be stripped before pattern-matching on C# source —
# otherwise "// Player" or "class Foo // note" injects phantom refs into
# the dependency graph. Handles escapes; collapses the literal to an
# empty replacement so character offsets don't get wildly shifted for
# downstream error reporting (none yet, but future-proof).
_CSHARP_COMMENT_OR_STRING = re.compile(
    r"""
    //[^\n]*            # line comment
    | /\*.*?\*/         # block comment (non-greedy)
    | @"(?:[^"]|"")*"   # verbatim string
    | "(?:\\.|[^"\\])*" # regular string
    | '(?:\\.|[^'\\])*' # char literal
    """,
    re.DOTALL | re.VERBOSE,
)


def _strip_comments_and_strings(source: str) -> str:
    """Remove C# comments + string/char literals from ``source``.

    Each match collapses to a single space to keep word boundaries
    intact for the subsequent regex scans.
    """
    return _CSHARP_COMMENT_OR_STRING.sub(" ", source)


def _extract_class_names(source: str) -> set[str]:
    """Return the class/struct/enum/interface names defined in a C# file.

    Comments and string literals are stripped first so ``// class Foo``
    or ``"class Bar"`` don't register as real type declarations.
    """
    clean = _strip_comments_and_strings(source)
    names: set[str] = set()
    for match in re.finditer(
        r"(?:public\s+|internal\s+|private\s+)?"
        r"(?:abstract\s+|static\s+|sealed\s+|partial\s+)*"
        r"(?:class|struct|enum|interface)\s+(\w+)",
        clean,
    ):
        names.add(match.group(1))
    return names


def _extract_references(source: str, all_class_names: set[str]) -> set[str]:
    """Names from ``all_class_names`` that appear in ``source`` and aren't
    declared there. Comments + string literals are stripped first so
    references inside ``// TODO: Player``, log messages like
    ``"Player not found"``, etc. don't pollute the graph.
    """
    defined = _extract_class_names(source)
    clean = _strip_comments_and_strings(source)
    refs: set[str] = set()
    for name in all_class_names - defined:
        if re.search(rf"\b{re.escape(name)}\b", clean):
            refs.add(name)
    return refs


def _build_dependency_graph(
    file_sources: dict[str, str],
) -> tuple[dict[str, set[str]], dict[str, str]]:
    """Build ``stem -> {stems it depends on}`` + ``class_name -> stem``.

    ``file_sources`` maps file stem (class name / filename-without-ext) to
    the raw C# source. Self-references are filtered.
    """
    class_to_stem: dict[str, str] = {}
    for stem, source in file_sources.items():
        for name in _extract_class_names(source):
            class_to_stem[name] = stem

    all_class_names = set(class_to_stem.keys())
    graph: dict[str, set[str]] = {stem: set() for stem in file_sources}
    for stem, source in file_sources.items():
        for ref_class in _extract_references(source, all_class_names):
            dep_stem = class_to_stem[ref_class]
            if dep_stem != stem:
                graph[stem].add(dep_stem)
    return graph, class_to_stem


def _topological_sort(graph: dict[str, set[str]]) -> list[str]:
    """Return nodes in dependency order — deps first, cycles broken
    arbitrarily. Deterministic: iteration order + ``sorted()`` mean the
    same input always produces the same output.
    """
    visited: set[str] = set()
    on_stack: set[str] = set()
    order: list[str] = []

    def _visit(node: str) -> None:
        if node in visited or node in on_stack:
            return
        on_stack.add(node)
        for dep in sorted(graph.get(node, set())):
            _visit(dep)
        on_stack.discard(node)
        visited.add(node)
        order.append(node)

    for node in sorted(graph):
        _visit(node)
    return order


def _compute_dependency_levels(
    graph: dict[str, set[str]],
) -> list[list[str]]:
    """Group stems into levels where all of a node's deps land in earlier
    levels. Scripts in the same level have no dep on each other, so they
    can run concurrently without starving the dependency-aware prompt.
    """
    order = _topological_sort(graph)
    level_of: dict[str, int] = {}
    for stem in order:
        deps = graph.get(stem, set())
        if not deps:
            level_of[stem] = 0
        else:
            level_of[stem] = max(
                (level_of[d] for d in deps if d in level_of), default=-1,
            ) + 1

    levels: dict[int, list[str]] = {}
    for stem, lvl in level_of.items():
        levels.setdefault(lvl, []).append(stem)
    # Sort within level for deterministic ordering; level 0 first.
    return [sorted(levels[k]) for k in sorted(levels)]


def _build_scoped_context(
    stem: str,
    graph: dict[str, set[str]],
    file_sources: dict[str, str],
    transpiled_luau: dict[str, str],
) -> str:
    """Scoped prompt context: direct deps' Luau + 1-hop transitive sigs.

    Direct deps get their already-transpiled Luau inline (so the AI can
    see method signatures and call them with ``require()``). Transitive
    deps get a class/method summary only — full source would blow the
    context window on projects with hundreds of scripts.
    """
    direct = graph.get(stem, set())
    transitive: set[str] = set()
    for dep in direct:
        transitive |= graph.get(dep, set())
    transitive -= direct
    transitive.discard(stem)

    parts: list[str] = []
    for dep in sorted(direct):
        if dep in transpiled_luau:
            parts.append(
                f"--- Already-transpiled dependency: {dep}.luau ---\n"
                f"```luau\n{transpiled_luau[dep]}\n```"
            )
        elif dep in file_sources:
            parts.append(
                f"--- Dependency (not yet transpiled): {dep}.cs ---\n"
                f"```csharp\n{file_sources[dep]}\n```"
            )

    for dep in sorted(transitive):
        source = file_sources.get(dep)
        if not source:
            continue
        classes = re.findall(r"(?:public\s+)?(?:abstract\s+)?(?:static\s+)?class\s+(\w+)", source)
        methods = re.findall(r"public\s+(?:static\s+)?[\w<>\[\]]+\s+(\w+)\s*\(", source)
        if classes or methods:
            class_str = ", ".join(dict.fromkeys(classes)) if classes else dep
            method_str = ", ".join(dict.fromkeys(methods)) if methods else "none"
            parts.append(
                f"--- Transitive ref: {dep} (classes: {class_str}; public methods: {method_str}) ---"
            )

    return "\n\n".join(parts)


def _build_serialized_field_context(
    script_path: Path,
    unity_project_path: str | Path,
    serialized_field_refs: dict[str, dict[str, str]],
) -> str:
    """Render the 4.9 serialized-field-refs for ``script_path`` as a
    prompt section. Returns empty string when no refs are available.

    ``serialized_field_refs`` is keyed on paths relative to
    ``unity_project_path`` (see ``serialize_for_context``), so we
    recompute the relative key here to look it up.
    """
    if not serialized_field_refs:
        return ""
    try:
        rel = script_path.resolve().relative_to(Path(unity_project_path).resolve())
    except (ValueError, OSError):
        rel = script_path
    fields = serialized_field_refs.get(str(rel)) or serialized_field_refs.get(
        str(script_path)
    )
    if not fields:
        return ""
    lines = [
        "--- Inspector-assigned serialized fields on this MonoBehaviour ---",
        "(Use ReplicatedStorage.Templates:WaitForChild(name) for prefab refs,",
        " and the audio asset path directly for audio refs.)",
    ]
    for field_name, target in sorted(fields.items()):
        lines.append(f"  {field_name} -> {target}")
    return "\n".join(lines)


def _build_project_context(script_infos: list[Any]) -> str:
    """Build a concise project context string for AI transpilation.

    Includes the list of project classes and their base types / interfaces
    so the AI understands cross-script relationships.
    """
    # Collect class names and their types
    classes = []
    for si in script_infos:
        if si.class_name:
            base = ""
            if hasattr(si, 'base_class') and si.base_class:
                base = f" : {si.base_class}"
            classes.append(f"  - {si.class_name}{base}")

    if not classes:
        return ""

    # Cap at a reasonable size to fit in context window
    if len(classes) > 100:
        classes = classes[:100]
        classes.append(f"  - ... and {len(script_infos) - 100} more")

    lines = ["Project classes (other scripts in this game — reference by name with require()):"]
    lines.extend(classes)
    return '\n'.join(lines)


def _is_visual_only_script(script_path: Path, source: str) -> bool:
    """Detect scripts that are Unity rendering/visual-only and can't work in Roblox.

    Conservative: only stub scripts that genuinely have no Roblox equivalent.
    Gameplay scripts (collision, resetting, timers) should always be transpiled.
    """
    name = script_path.stem.lower()

    # Gameplay indicators — if present, NEVER stub
    gameplay_indicators = [
        "OnCollision", "OnTrigger", "Rigidbody", "GetComponent",
        "Instantiate", "Destroy(", "SendMessage", "StartCoroutine",
        "Input.", "KeyCode.", "transform.position", "transform.rotation",
        "AddForce", "velocity", "Health", "Damage", "Timer",
    ]
    if any(ind in source for ind in gameplay_indicators):
        return False

    # Known visual-only script name patterns (rendering/shader effects only)
    visual_keywords = [
        "planarreflection", "specularlighting", "waterbase", "watertile",
        "waterbasic", "gerstnerdisplace", "displace", "meshcontainer",
        "planetexture",
    ]
    if name in visual_keywords:
        return True

    # Scripts that primarily manipulate shaders/renderers/materials
    shader_indicators = ["Shader.", "Material.", "Renderer.", "renderer.material",
                         "OnRenderImage", "OnWillRenderObject", "RenderTexture",
                         "Graphics.Blit"]
    shader_count = sum(1 for s in shader_indicators if s in source)
    if shader_count >= 2:
        return True

    return False


# ---------------------------------------------------------------------------
# AI-powered transpilation
# ---------------------------------------------------------------------------

_AI_SYSTEM_PROMPT = """\
You are an expert Unity C# to Roblox Luau transpiler. Convert the given C# script to idiomatic, RUNNABLE Roblox Luau code. The output MUST parse with luau-analyze and execute without errors.

CRITICAL: Output ONLY valid Luau code. No markdown fences. No explanations. No prose.

## Roblox Architecture

Scripts run in these containers:
- **Server Scripts**: ServerScriptService (game logic, physics, damage, spawning)
- **LocalScripts**: StarterPlayerScripts / StarterGui (input, camera, UI, HUD)
- **ModuleScripts**: ReplicatedStorage (shared libraries, data, utility classes)

Script context: `script.Parent` is the service container. Find game objects via:
- `workspace:FindFirstChild("Name", true)` for a specific object
- `workspace:GetDescendants()` loop for multiple objects of the same type
- `game:GetService("CollectionService"):GetTagged("Tag")` for tagged objects

## Unity → Roblox API Mapping

Lifecycle:
- Awake/Start → top-level initialization code
- Update → `RunService.Heartbeat:Connect(function(dt) ... end)`
- FixedUpdate → `RunService.Heartbeat:Connect(function(dt) ... end)`
- OnDestroy → `script.Destroying:Connect(function() ... end)` or Maid pattern
- OnEnable/OnDisable → manual enable/disable via attributes
- OnCollisionEnter/OnTriggerEnter → `part.Touched:Connect(function(otherPart) ... end)`
  **IMPORTANT — Model trigger parts**: `.Touched` only works on BasePart instances, NOT Models. When `script.Parent` is a Model that mixes visible mesh Parts with an invisible trigger zone (Transparency=1, common Unity pattern — Collider/Trigger/Detector child holding the proximity sphere alongside visible MeshParts), `:FindFirstChildWhichIsA("BasePart")` returns the WRONG one — typically the trigger zone because it's listed first in the children. Emit a tiered finder helper at the top of the script and use that:
```lua
-- Tier 1: a Part the prefab author NAMED as the trigger. Common Unity
-- conventions across projects: Collider, Trigger, TriggerZone, Detector,
-- Sensor, Hitbox, Range, ProximityVolume, PickupTouchDetector. Names
-- vary per project — don't assume any single one.
local TRIGGER_NAMES = {"Colliders", "Collider", "Trigger", "TriggerZone", "Detector",
    "Sensor", "Hitbox", "Range", "ProximityVolume", "PickupTouchDetector"}
local function findTriggerPart(parent)
    for _, n in ipairs(TRIGGER_NAMES) do
        local p = parent:FindFirstChild(n)
        if p and p:IsA("BasePart") then return p end
    end
    -- Tier 2: the LARGEST invisible BasePart child (no mesh, fully transparent).
    -- Volume-ranked so the script doesn't bind to tiny per-slot Parts
    -- (SimpleFPS Machine has 1x1x1 "Item N" markers alongside a much
    -- larger "Colliders" trigger; first-match would pick the wrong one).
    local best, bestVol = nil, -1
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("BasePart") and c.Transparency >= 1 and not c:IsA("MeshPart") then
            local v = c.Size.X * c.Size.Y * c.Size.Z
            if v > bestVol then best, bestVol = c, v end
        end
    end
    return best
end
local function findVisualTarget(parent)
    -- Tier 1: child Model (Unity pickups often wrap visible meshes in a Model).
    -- Skip MinimapIcon / UI Models that have no real geometry.
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("Model") and c.Name ~= "MinimapIcon" and c:FindFirstChildWhichIsA("BasePart") then
            return c
        end
    end
    -- Tier 2: a MeshPart (mesh implies visual intent).
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("MeshPart") then return c end
    end
    -- Tier 3: a non-trigger BasePart. ``Transparency < 1`` is an
    -- imperfect proxy (Decal-only Parts can have Transparency=1, debug
    -- colliders can be semi-transparent), but it's the best the runtime
    -- can do without compile-time prefab info — and tiers 1 and 2 catch
    -- the common cases first.
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("BasePart") and c.Transparency < 1 then return c end
    end
    -- Tier 4: any BasePart, last resort.
    return parent:FindFirstChildWhichIsA("BasePart")
end
```
Then use `findTriggerPart(model)` for `OnTriggerEnter` Touched handlers and `findVisualTarget(model)` for animations / `transform.X` access. For movement (rotate, lerp, follow path), use `model:PivotTo(model:GetPivot() * delta)` on the whole Model — do NOT animate a single child Part (siblings stay where they are).

Core:
- `Debug.Log/LogWarning/LogError` → `print` / `warn`
- `Instantiate(prefab)` → `prefab:Clone(); clone.Parent = workspace`
- `Destroy(obj)` / `Destroy(obj, delay)` → `obj:Destroy()` / `game:GetService("Debris"):AddItem(obj, delay)`
- `GetComponent<T>()` → `:FindFirstChildWhichIsA("T")`
- `gameObject.SetActive(false)` → set `Transparency=1, CanCollide=false` (or use recursive helper)
- `gameObject` / `this` → `script.Parent` (the part OR model the script is parented to — see "Model vs Part dispatch" below)

### Model vs Part dispatch
Unity's `transform.X` is one API regardless of whether the GameObject has a single mesh or a hierarchy. In Roblox, BasePart and Model have **different** APIs, so every `transform.X` translation needs to dispatch on `script.Parent:IsA("Model")`. Helper pattern at the top of the script:
```lua
-- Capture container at script init. If the script may reparent itself
-- at runtime (rare — only do this if the C# explicitly does
-- ``transform.SetParent(...)`` on its own GameObject), inline
-- ``script.Parent`` at each call site instead of capturing.
local container = script.Parent
-- Without a PrimaryPart, ``Model:PivotTo`` and ``:GetPivot`` use the
-- model's bounding-box centre as the rotation pivot — which means
-- compose-rotation patterns like ``setCFrame(getCFrame() * Angles(...))``
-- rotate around the wrong axis. Pin a PrimaryPart at script init so the
-- pivot matches the GameObject origin the rest of the code expects.
if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end
local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end
local function setCFrame(cf)
    if container:IsA("Model") then container:PivotTo(cf) else container.CFrame = cf end
end
local function getPosition() return getCFrame().Position end
```
Then:
- `transform.position` → `getPosition()` (read) or `setCFrame(CFrame.new(newPos))` (write)
- `transform.rotation` → `getCFrame()` (read) or `setCFrame(getCFrame() * rot)` (compose)
- `transform.forward/right/up` → `getCFrame().LookVector/RightVector/UpVector`
- `transform.localScale` → `container.Size` for BasePart, `container:ScaleTo(s)` for Model (Models have no Size)
Skip the helpers when you can prove `script.Parent` is always a BasePart (the script lives next to a single MeshPart, no Model wrapper). Otherwise emit the dispatch — picking a child Part and reading/writing its CFrame moves only that child, not the model.

Input:
- `Input.GetKey/GetKeyDown/GetKeyUp` → `UserInputService:IsKeyDown(Enum.KeyCode.X)`
- `Input.GetMouseButton` → `UserInputService:IsMouseButtonPressed(Enum.UserInputType.MouseButton1)`
- `Input.GetAxis("Horizontal"/"Vertical")` → poll WASD keys manually
- `Camera.main` → `workspace.CurrentCamera`
- **IMPORTANT**: Roblox reserves Escape key for the system menu. Do NOT use `Enum.KeyCode.Escape` for pause menus. Use `Enum.KeyCode.P` or `Enum.KeyCode.Tab` instead, or remove ESC handlers entirely.

UI:
- Unity Canvas GameObjects become Roblox ScreenGuis. When a script references its own Canvas, find it via `playerGui:FindFirstChildOfClass("ScreenGui")` or by the Canvas's actual name — NOT by the script's class name.
- **NEVER** use `WaitForChild` with the script's own class name to find a ScreenGui. Use `FindFirstChildOfClass("ScreenGui")` instead. If the ScreenGui hasn't loaded yet, use `playerGui:WaitForChild("ScreenGui", 10)` with a timeout, NOT infinite yield.

Camera (FPS/First-Person):
- Set `camera.CameraType = Enum.CameraType.Scriptable` before controlling the camera
- **CRITICAL**: In Unity, the camera is a child of the player and auto-follows position/rotation. In Roblox, the camera is INDEPENDENT — you must explicitly position it at the character's head every frame.
- Use `character:FindFirstChild("HumanoidRootPart").Position + Vector3.new(0, 1.5, 0)` for camera position — **NOT** `Head.Position`. The Head bone bobs every step under Roblox's default walking animation; tracking it propagates that bob into the camera and produces a visible per-step shiver. The HumanoidRootPart is the physics root and is animation-stable; offset upward by ~1.5 studs to land near eye level.
- Track yaw (mouse X) and pitch (mouse Y) as separate angles, combine into camera CFrame:
  `camera.CFrame = CFrame.new(headPos) * CFrame.Angles(0, yawAngle, 0) * CFrame.Angles(pitchAngle, 0, 0)`
- **Mouse look (canonical FPS pattern)**: read `UserInputService:GetMouseDelta()` every frame inside the RenderStepped callback — NOT `UserInputService.InputChanged` with `input.Delta`. `GetMouseDelta()` is purpose-built for FPS: it returns the accumulated pixel delta since the last call and is reliable while `MouseBehavior = LockCenter`. The `InputChanged` MouseMovement event with `LockCenter` is flaky and loses deltas under high frame rates.
  ```lua
  local MOUSE_RAD_PER_PIXEL = 0.006  -- tune; ~0.003-0.012 feels right
  RunService.RenderStepped:Connect(function(dt)
      local d = UserInputService:GetMouseDelta()
      yawAngle = yawAngle - d.X * MOUSE_RAD_PER_PIXEL
      pitchAngle = math.clamp(pitchAngle - d.Y * MOUSE_RAD_PER_PIXEL, math.rad(minAngle), math.rad(maxAngle))
      -- ... then position camera from yaw/pitch
  end)
  ```
  Do NOT translate Unity's `sensitivity * Time.deltaTime * Input.GetAxis("MouseX")` literally — Unity's `GetAxis("Mouse*")` returns a smoothed/calibrated value, not raw pixels, so applying the Unity sensitivity directly to Roblox raw deltas produces ~50x too-fast (or 0 if InputChanged drops deltas) rotation. The radians-per-pixel constant above is the right scale for raw deltas.
- Do NOT modify `rootPart.CFrame` for rotation — this conflicts with Roblox's Humanoid movement controller
- For camera-relative movement, compute direction from camera yaw:
  `local moveDir = (CFrame.Angles(0, yawAngle, 0) * inputDir).Unit; humanoid:Move(moveDir)`

Character movement speed (CRITICAL — Unity m/s vs Roblox studs/s):
- Unity expresses controller speed in **m/s** (e.g., `public float speed = 6;`); Roblox `Humanoid.WalkSpeed` is in **studs/sec**. The pipeline constant is `STUDS_PER_METER = 3.571`, so the canonical translation is:
  ```lua
  -- once, at character bind (onCharacter / CharacterAdded)
  humanoid.WalkSpeed = speed * 3.571  -- studs/sec
  humanoid.JumpHeight = jumpSpeed * 3.571
  -- then every frame, pass a UNIT direction (magnitude <= 1) — never scale it by speed:
  humanoid:Move(moveDirection)
  ```
- Do NOT emit `humanoid:Move(moveDir * speed)` or `humanoid:Move(moveDir * (speed / 16))`. `Humanoid:Move(direction)` takes a direction whose magnitude is clamped to 1; multiplying by speed has no effect, and dividing by 16 (a magic number that conflates Roblox default WalkSpeed with Unity m/s) yields an effective walk speed of ~0.375 × WalkSpeed = far too slow. Configure WalkSpeed once; pass unit direction every frame.

Physics:
- `Physics.Raycast(origin, dir, dist)` → `workspace:Raycast(origin, dir * dist, RaycastParams.new())`
- `Rigidbody.velocity` → `part.AssemblyLinearVelocity`
- `Rigidbody.AddForce` → `part:ApplyImpulse(force)`
- `Rigidbody.isKinematic` → `part.Anchored`
- `Physics.OverlapSphere` → `workspace:GetPartBoundsInRadius(center, radius)`

Distance & radius units (CRITICAL — Unity m vs Roblox studs):
- Every Unity physics distance and radius is in **metres**: `Physics.OverlapSphere(p, r)`, `Physics.Raycast(o, d, maxDistance)`, `Physics.SphereCast`, `Vector3.Distance(a,b) < r`, `RaycastHit.distance`, `Collider.bounds.extents`. Roblox `workspace:GetPartBoundsInRadius`, `workspace:Raycast`, and any spatial comparison use **studs**.
- Multiply every Unity metre value by `STUDS_PER_METER = 3.571` when translating, or emit the literal Lua constant inline:
  ```lua
  -- Unity: Physics.OverlapSphere(transform.position, 2)
  local hit = workspace:GetPartBoundsInRadius(getPosition(), 2 * 3.571)
  -- Unity: Physics.Raycast(o, d, 100)
  local r = workspace:Raycast(o, d.Unit * (100 * 3.571), params)
  ```
- Do NOT leave literal Unity-metre numbers in the emitted Roblox code. A 2-metre explosion radius rendered as `, 2)` in Roblox studs reaches barely past the mine's own collider — the player's HumanoidRootPart sits ~3 studs above the floor and would be **outside** a 2-stud sphere centred on the mine. The damage call silently does nothing. Same failure mode applies to every Unity-metres-in-Roblox-studs translation — scale all of them.

Events & Communication:
- `UnityEvent.AddListener(cb)` → `event:Connect(cb)`
- `event?.Invoke(args)` → `if event then event:Fire(args) end`
- `SendMessage("Method")` → `part:SetAttribute("Method", true)` (use Attributes for inter-component communication)
- `StartCoroutine(Func())` → `task.spawn(Func)`
- `yield return new WaitForSeconds(n)` → `task.wait(n)`
- `yield return null` → `task.wait()`

Cross-script shared state (CRITICAL — avoids silent split-state bugs):
- When a dependency you see in the provided context exports a getter
  (e.g. `Player.hasKey = function() return gotKey end`), call it via
  `require(script.Parent.Player).hasKey()` — do NOT guess
  `character:GetAttribute("hasKey")`. Attribute reads with no matching
  writer elsewhere in the project produce silent nil/false values that
  never update.
- Only use `:GetAttribute`/`:SetAttribute` for state the CURRENT script
  owns and writes on the same instance. Cross-script queries go through
  a ModuleScript method.
- If you must expose shared boolean state via attributes, write it on
  BOTH the owning-script side (every assignment site) AND the consumer
  side. Never one without the other.

Unconverted methods (when a C# method has no faithful Luau translation):
- Emit a stub Luau function with `-- UNCONVERTED: <short reason>` as the
  body. Do NOT silently drop methods. Reasons: reflection, unsafe code,
  editor-only APIs, or genuinely no Roblox equivalent.
- Example: `function MyClass:TakeScreenshot()\n  -- UNCONVERTED: Application.CaptureScreenshot has no Roblox equivalent\nend`

Property metamethods (C# auto-properties with side-effect getters/setters):
- When the C# property does non-trivial work in `get`/`set`, emit
  `__index`/`__newindex` metamethods on the backing table — NOT plain
  field aliases. Plain aliases lose the side effect at call time.

Networking (for multiplayer Unity games):
- `[Command]` methods → `RemoteEvent:FireServer()`
- `[ClientRpc]` methods → `RemoteEvent:FireAllClients()`
- `[SyncVar]` fields → `SetAttribute()`/`GetAttribute()` with `GetAttributeChangedSignal()`
- `NetworkBehaviour` → Script with RemoteEvent-based communication

Navigation:
- `NavMeshAgent.SetDestination(pos)` → `PathfindingService:CreatePath():ComputeAsync(start, pos)`
- `NavMeshAgent.speed/stoppingDistance` → store as attributes, use in movement logic

Animation:
- Skeletal/character animation is NOT supported (Roblox has no automated
  skinned-mesh pipeline) — there is no animation runtime.
- `Animator.*` calls (`SetBool`/`SetFloat`/`SetInteger`/`SetTrigger`/
  `ResetTrigger`/`Play`/`CrossFade`) become `SetAttribute("ParamName", value)`
  on the host: the parameter is recorded but drives no skeleton — an inert
  degradation, not real animation. Never emit AnimationTrack playback or
  Animator/AnimationController runtime code.

Audio:
- `AudioSource.Play()` → `sound:Play()`
- `AudioSource.clip` → `sound.SoundId`
- `AudioSource.volume/pitch/loop` → `sound.Volume/PlaybackSpeed/Looped`

String Operations:
- `string.Format("{0} {1}", a, b)` → `string.format("%s %s", a, b)`
- `$"text {expr}"` → `string.format("text %s", tostring(expr))` or `"text " .. tostring(expr)`
- `.StartsWith("s")` → `string.sub(var, 1, #"s") == "s"`
- `.EndsWith("s")` → `string.sub(var, -#"s") == "s"`
- `.Substring(start, len)` → `string.sub(var, start+1, start+len)` (0→1 indexing)
- `.Trim()` → `string.match(var, "^%s*(.-)%s*$")`
- `.Contains("s")` → `string.find(var, "s") ~= nil`
- `.Split(char)` → `string.split(var, char)`

Collections:
- `List<T>` / `T[]` → Luau table `{}`
- `.Add(x)` → `table.insert(tbl, x)`
- `.Remove(x)` → `table.remove(tbl, table.find(tbl, x))`
- `.Contains(x)` → `table.find(tbl, x) ~= nil`
- `.Count` / `.Length` → `#tbl`
- `Dictionary<K,V>` → Luau table `{}`
- `.ContainsKey(k)` → `tbl[k] ~= nil`
- `.TryGetValue(k, out v)` → `local v = tbl[k]; if v ~= nil then`
- `foreach (var x in coll)` → `for _, x in coll do`
- LINQ methods → implement inline: `.Where` → filter loop, `.Select` → map loop, `.Any` → find loop

Math:
- `Mathf.X` → `math.x` (lowercase: `math.abs`, `math.floor`, `math.clamp`, etc.)
- `Mathf.Lerp(a,b,t)` → `a + (b - a) * t`
- `Mathf.Infinity` → `math.huge`
- `Random.Range(a,b)` → `math.random(a, b)` (integers) or `math.random() * (b-a) + a` (floats)
- `Vector3.Distance(a,b)` → `(a - b).Magnitude`
- `Vector3.Dot(a,b)` → `a:Dot(b)`
- `Vector3.Cross(a,b)` → `a:Cross(b)`
- `Vector3.normalized` → `vec.Unit`
- `Vector3.magnitude` → `vec.Magnitude`
- `Quaternion.Euler(x,y,z)` → `CFrame.Angles(math.rad(x), math.rad(y), math.rad(z))`
- `Quaternion.LookRotation(fwd)` → `CFrame.lookAt(pos, pos + fwd)`

## Luau Syntax Rules (MUST follow exactly)

- NO `{}` for blocks — use `then`/`do`/`end`
- NO semicolons
- NO type annotations — `local x = 5` not `int x = 5`
- NO access modifiers (`public`, `private`, `protected`, `static`)
- NO compound assignment — `x = x + 1` not `x += 1`
- NO C# operators: `&&` → `and`, `||` → `or`, `!expr` → `not expr`, `!=` → `~=`
- NO `null` — use `nil`
- Functions: `local function name(args) ... end`
- String concatenation: `..` not `+`
- Array length: `#arr` not `arr.Length`
- Ternary: `if cond then a else b` (Luau if-expression, not `? :`)
- For loops: `for i = 0, n-1 do` not `for(int i=0; i<n; i++)`
- For-each: `for _, x in items do` not `foreach (var x in items)`
- Comments: `--` not `//`
- Block comments: `--[[ ]]` not `/* */`
- Tables are 1-indexed (convert 0-based C# indexing to 1-based)
- `continue` IS valid in Roblox Luau
- Bitwise ops: use `bit32.band()`, `bit32.bor()`, `bit32.lshift()`, `bit32.rshift()`

## Script Structure Pattern

For a typical MonoBehaviour conversion, produce:

```
-- Services
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
-- (only import services actually used)

-- Module (if this is a shared class used by other scripts)
local ClassName = {}

-- Configuration (from serialized fields)
local speed = 10
local maxHealth = 100

-- State
local currentHealth = maxHealth

-- Functions (from C# methods)
local function takeDamage(amount)
    currentHealth = currentHealth - amount
    if currentHealth <= 0 then
        -- handle death
    end
end

-- Lifecycle
RunService.Heartbeat:Connect(function(dt)
    -- Update logic
end)

return ClassName  -- (only for ModuleScripts)
```

## Important Notes
- Convert the ENTIRE script faithfully. Do not skip methods or simplify logic.
- Preserve all game logic, conditions, and calculations.
- If a Unity API has no Roblox equivalent, comment it out with `-- [Unity-only] original code`.
- Do NOT add explanatory comments unless the conversion is non-obvious.
- Do NOT stub or skip complex methods — convert them fully.
- Interfaces/abstract classes → ModuleScript with table of functions.
- Enums → table with named numeric values: `local MyEnum = { ValueA = 0, ValueB = 1 }`.
- C# events → BindableEvent or simple callback tables.
- C# properties with getters/setters → local variables with getter/setter functions if logic exists, otherwise just local variables.
"""


def _ai_transpile(
    csharp_source: str,
    api_key: str,
    model: str,
    class_name: str = "",
    script_type: str = "Script",
    project_context: str = "",
) -> tuple[str, float, list[str]]:
    """Transpile C# source to Luau using the Claude API.

    Args:
        csharp_source: The C# source code.
        api_key: Anthropic API key.
        model: Model identifier.
        class_name: Name of the C# class being transpiled.
        script_type: Target script type (Script, LocalScript, ModuleScript).
        project_context: Additional context about the project (classes, dependencies).

    Returns:
        Tuple of (luau_source, confidence, warnings).

    Raises:
        RuntimeError: If the API call fails after retries.
    """
    warnings: list[str] = []

    # Check cache first (include system prompt hash so prompt changes invalidate cache).
    _prompt_hash = hashlib.sha256(_AI_SYSTEM_PROMPT.encode()).hexdigest()[:16]
    cache_key = _ai_cache_key(
        csharp_source=csharp_source,
        class_name=class_name,
        script_type=script_type,
        project_context=project_context,
        prompt_hash=_prompt_hash,
        model=model,
    )
    cached = _load_cache(cache_key)
    if cached is not None:
        # Verify cached result passes lint — old caches may have syntax errors
        luau_cached = cached["luau"]
        cached_errors = _luau_syntax_check(luau_cached)
        if not cached_errors:
            log.debug("AI transpilation cache hit for %s (lint clean)", cache_key[:12])
            return luau_cached, cached["confidence"], cached.get("warnings", [])
        else:
            log.info("  [%s] Cache hit but %d syntax error(s) — re-transpiling",
                     class_name, len(cached_errors))

    # Call the API.
    try:
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        raise RuntimeError(
            "anthropic package is required for AI transpilation. "
            "Install it with: pip install anthropic"
        )

    client = anthropic.Anthropic(api_key=api_key)

    # Build the user message with context
    user_msg = ""
    if class_name:
        user_msg += f"Class: `{class_name}`\n"
    if script_type == "LocalScript":
        user_msg += "Target: **LocalScript** (client-side only — no server APIs).\n"
    elif script_type == "ModuleScript":
        user_msg += "Target: **ModuleScript** (shared library — must return a table).\n"
    else:
        user_msg += "Target: **Server Script**.\n"
    if project_context:
        user_msg += f"\n{project_context}\n"
    user_msg += f"\nConvert this Unity C# script to Roblox Luau:\n\n```csharp\n{csharp_source}\n```"

    try:
        response = client.messages.create(
            model=model,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=_AI_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": user_msg,
                },
            ],
        )
    except Exception as exc:
        raise RuntimeError(f"Claude API call failed: {exc}") from exc

    # Extract the Luau code from the response.
    luau_source = ""
    for block in response.content:
        if hasattr(block, "text"):
            luau_source += block.text

    # Strip markdown code fences if present.
    luau_source = _strip_code_fences(luau_source)

    # Lint check + reprompt loop: run luau-analyze to catch syntax errors,
    # then ask Claude to fix them if any are found.
    luau_source, lint_warnings = _lint_and_fix(
        luau_source, class_name=class_name, original_csharp=csharp_source,
    )
    warnings.extend(lint_warnings)

    # AI transpilation gets a baseline confidence based on output quality.
    confidence = 0.75

    # Boost confidence for valid Luau indicators
    if "local " in luau_source and "end" in luau_source:
        confidence = 0.85
    if "game:GetService" in luau_source:
        confidence = min(confidence + 0.05, 0.95)
    # Boost for Roblox-specific patterns
    if "workspace" in luau_source or ":FindFirstChild" in luau_source:
        confidence = min(confidence + 0.03, 0.95)

    # Penalize for residual C# patterns
    if "GetComponent<" in luau_source:
        confidence -= 0.15
        warnings.append("Residual C# generic syntax: GetComponent<>")
    if "+=" in luau_source or "-=" in luau_source:
        confidence -= 0.05
        warnings.append("Residual compound assignment (+=/-=)")
    if re.search(r'\bvoid\b', luau_source):
        confidence -= 0.1
        warnings.append("Residual C# void keyword")
    if re.search(r'\bnull\b', luau_source):
        confidence -= 0.05
    if ";" in luau_source and luau_source.count(";") > 3:
        confidence -= 0.05
    confidence = max(0.3, min(0.95, confidence))

    # Save to cache.
    _save_cache(cache_key, {
        "luau": luau_source,
        "confidence": confidence,
        "warnings": warnings,
    })

    return luau_source, confidence, warnings


def _strip_code_fences(text: str) -> str:
    """Remove markdown code fences and surrounding prose from AI output.

    Handles cases where the AI returns explanatory text before/after/between
    code blocks.  We extract the *largest* fenced code block and return it,
    falling back to stripping outer fences only if no inner block is found.
    """
    # Strategy 1: find all fenced code blocks and pick the largest.
    blocks = re.findall(
        r"```(?:luau|lua)?\s*\n(.*?)```",
        text,
        flags=re.DOTALL,
    )
    if blocks:
        # Return the largest block (most likely the main script).
        return max(blocks, key=len).strip()

    # Strategy 2: no fenced blocks found — strip outer fences if present.
    text = re.sub(r"^```(?:luau|lua)?\s*\n?", "", text, count=1)
    text = re.sub(r"\n?```\s*$", "", text, count=1)
    return text.strip()


# ---------------------------------------------------------------------------
# Claude Code CLI transpilation
# ---------------------------------------------------------------------------

def _claude_cli_transpile(
    csharp_source: str,
    class_name: str = "",
    script_type: str = "Script",
    project_context: str = "",
) -> tuple[str, float, list[str]]:
    """Transpile C# to Luau by invoking Claude Code CLI.

    Requires 'claude' to be on PATH. No API key needed since Claude Code
    handles its own authentication.

    Returns:
        Tuple of (luau_source, confidence, warnings).
    """
    import shutil

    warnings: list[str] = []

    # Check cache first (include class_name, script_type, and system prompt hash in key).
    _prompt_hash = hashlib.sha256(_AI_SYSTEM_PROMPT.encode()).hexdigest()[:16]
    cache_key = _ai_cache_key(
        csharp_source=csharp_source,
        class_name=class_name,
        script_type=script_type,
        project_context=project_context,
        prompt_hash=_prompt_hash,
        model="claude-cli-v4",
    )
    cached = _load_cache(cache_key)
    if cached is not None:
        # Verify cached result passes lint — old caches may have syntax errors
        luau_cached = cached["luau"]
        cached_errors = _luau_syntax_check(luau_cached)
        if not cached_errors:
            log.debug("Claude CLI cache hit for %s (lint clean)", cache_key[:12])
            return luau_cached, cached["confidence"], cached.get("warnings", [])
        else:
            log.info("  [%s] Cache hit but %d syntax error(s) — re-transpiling",
                     class_name, len(cached_errors))

    # Check claude is available
    claude_path = shutil.which("claude")
    if not claude_path:
        raise RuntimeError("'claude' CLI not found on PATH")

    # Build context about this script and project
    context = ""
    if class_name:
        context += f"\nClass: `{class_name}`."
    if script_type == "LocalScript":
        context += "\nTarget: **LocalScript** (client-side only — no server APIs)."
    elif script_type == "ModuleScript":
        context += "\nTarget: **ModuleScript** (must return a table)."
    else:
        context += "\nTarget: **Server Script**."
    if project_context:
        context += f"\n{project_context}"

    prompt = (
        f"{_AI_SYSTEM_PROMPT}\n{context}\n\n"
        f"Convert this Unity C# script to Roblox Luau:\n\n"
        f"```csharp\n{csharp_source}\n```"
    )

    claude_timeout = 600
    try:
        result = subprocess.run(
            [claude_path, "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=claude_timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude CLI exited with code {result.returncode}: {result.stderr[:200]}")
        luau_source = result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI timed out after {claude_timeout}s")
    except FileNotFoundError:
        raise RuntimeError("'claude' CLI not found")

    # Strip code fences if present.
    luau_source = _strip_code_fences(luau_source)

    # Lint check + reprompt loop: run luau-analyze to catch syntax errors,
    # then ask Claude to fix them if any are found.
    luau_source, lint_warnings = _lint_and_fix(
        luau_source, class_name=class_name, original_csharp=csharp_source,
    )
    warnings.extend(lint_warnings)

    # Score confidence.
    confidence = 0.75
    if "local " in luau_source and "end" in luau_source:
        confidence = 0.85
    if "game:GetService" in luau_source:
        confidence = min(confidence + 0.05, 0.95)
    # Boost confidence if lint-clean
    if not lint_warnings:
        confidence = min(confidence + 0.05, 0.95)

    # Cache result.
    _save_cache(cache_key, {
        "luau": luau_source,
        "confidence": confidence,
        "warnings": warnings,
    })

    return luau_source, confidence, warnings


def _find_transpiler() -> str:
    """Determine which AI transpiler backend is available.

    Returns: "claude_cli", "anthropic_api", or "none".
    """
    import shutil

    # Prefer Claude Code CLI (no API key needed)
    if shutil.which("claude"):
        return "claude_cli"

    # Fall back to Anthropic API if key is available
    from config import ANTHROPIC_API_KEY
    if ANTHROPIC_API_KEY:
        try:
            import anthropic  # noqa: F401
            return "anthropic_api"
        except ImportError:
            pass

    return "none"


# ---------------------------------------------------------------------------
# Script type classification
# ---------------------------------------------------------------------------

def _classify_script_type(csharp_source: str, info: Any) -> str:
    """Classify a script as Script, LocalScript, or ModuleScript.

    Based on content analysis of the C# source and analyzer metadata.

    When the source isn't clearly client-side and isn't a MonoBehaviour
    or NetworkBehaviour, default to ``ModuleScript``. The downstream
    ``script_coherence`` pass already promotes required-by-others
    scripts to ``ModuleScript``; starting from ``ModuleScript`` for
    non-MonoBehaviour code prevents spurious "reclassified to
    ModuleScript" reclassifications. Genuine server gameplay
    (MonoBehaviour with no client APIs) still resolves to ``Script``.
    """
    # Use the analyzer's suggestion as the primary signal.
    if hasattr(info, "suggested_type"):
        suggested = info.suggested_type
        if suggested in ("Script", "LocalScript", "ModuleScript"):
            return suggested

    # Fallback: analyze the source directly.
    source_lower = csharp_source.lower()

    # Client-side indicators -> LocalScript.
    client_indicators = [
        "input.getkey", "input.getkeydown", "input.getaxis",
        "input.mouseposition", "camera.main",
        "userinputservice", "screengui", "canvas",
        "unityengine.ui",  # Unity UI namespace (Text, Image, Button, etc.)
        "cursor.lockstate", "cursor.visible",
        "screen.width", "screen.height",
    ]
    if any(indicator in source_lower for indicator in client_indicators):
        return "LocalScript"

    is_mono = (
        "monobehaviour" in source_lower
        or "networkbehaviour" in source_lower
    )

    # MonoBehaviour-derived gameplay code on the server.
    if is_mono:
        return "Script"

    # Everything else (utility classes, plain C# files, no MonoBehaviour
    # base) defaults to ModuleScript. This matches source-repo behavior
    # and avoids the downstream coherence pass having to promote bare
    # utility scripts post-hoc.
    return "ModuleScript"


# ---------------------------------------------------------------------------
# Pre-AI C# pattern analysis
#
# Surface high-impact patterns that frequently produce Luau the AI can't
# faithfully transpile. Warnings are added to TranspiledScript.warnings so
# they show up in the conversion report ahead of method-completeness
# diagnostics — the user sees them BEFORE they hit the broken behavior.
# ---------------------------------------------------------------------------

# Each entry: (category, regex, human-readable warning).
# Patterns are ordered by frequency in real-world Unity projects so the
# first match for a category surfaces the most-likely-relevant snippet.
_CSHARP_PATTERN_RULES: list[tuple[str, str, str]] = [
    (
        "linq",
        r"\b(?:using\s+System\.Linq|\.(?:Select|Where|OrderBy|OrderByDescending|"
        r"GroupBy|Aggregate|FirstOrDefault|SingleOrDefault|ToList|ToArray|ToDictionary|"
        r"Distinct|Skip|Take|Zip)\s*\()",
        "LINQ usage detected (System.Linq or .Select/.Where/.OrderBy/etc.); "
        "Roblox/Luau has no LINQ — verify the AI used UTILITY_FUNCTIONS or "
        "explicit loops",
    ),
    (
        "async",
        r"\b(?:async\s+(?:Task|void)|await\s+|Task<[A-Za-z_]|Task\.Run\s*\(|"
        r"Task\.Delay\s*\(|UniTask)",
        "async/await or Task<T> detected; Luau coroutines are similar but not "
        "identical — verify the AI translated to coroutine.wrap / task.wait",
    ),
    (
        "networking",
        # Mix of word-boundary identifiers and bracketed attributes — the
        # \[Command\] alternation lives outside the \b group because '['
        # is non-word and \b would never match before it.
        r"(?:\b(?:UnityWebRequest|UnityEngine\.Networking|NetworkBehaviour|"
        r"RpcTarget|PhotonView|Mirror\.NetworkServer|Mirror\.NetworkClient)\b|"
        r"\[(?:Command|ClientRpc|ServerRpc)\])",
        "Unity Networking/Mirror/Photon API detected; map manually to Roblox "
        "RemoteEvent / RemoteFunction — the AI cannot infer authority topology",
    ),
    (
        "reflection",
        r"\b(?:typeof\s*\(|GetType\s*\(\s*\)|Activator\.CreateInstance|"
        r"System\.Reflection\.|FieldInfo|MethodInfo|PropertyInfo)",
        "Reflection API detected (typeof / GetType / System.Reflection); "
        "Luau has no equivalent — refactor to explicit dispatch",
    ),
    (
        "threading",
        r"\b(?:System\.Threading\.|new\s+Thread\s*\(|ThreadPool\.|"
        r"Interlocked\.|\block\s*\(|Mutex|Semaphore)",
        "Threading primitives detected; Luau is single-threaded with task.spawn "
        "+ events — verify shared-state isn't assumed atomic",
    ),
    (
        "unsafe_or_pointers",
        r"\b(?:unsafe\s*(?:\{|public|private|internal)|fixed\s*\(|stackalloc\b|"
        r"\bIntPtr\b|Marshal\.)",
        "Unsafe code or unmanaged pointers detected (unsafe / fixed / IntPtr / "
        "Marshal); Luau has no equivalent — manual rewrite required",
    ),
]


def _analyze_csharp_patterns(csharp_source: str) -> list[str]:
    """Return human-readable warnings for high-impact C# patterns that
    frequently mistranspile.

    Six categories: LINQ, async/await, Unity Networking / Mirror / Photon,
    reflection, threading, and unsafe/unmanaged pointers. One warning per
    category triggered.

    Strips ``//`` and ``/* */`` comments before matching so commented-out
    code doesn't trigger false positives. (String literals are NOT
    stripped — patterns rarely appear inside strings, and stripping would
    require a full lexer.)
    """
    if not csharp_source:
        return []

    # Drop // line comments and /* ... */ block comments. Conservative —
    # ignores escapes inside string literals (rare in real C#).
    cleaned = re.sub(r"//[^\n]*", "", csharp_source)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)

    warnings: list[str] = []
    for _category, pattern, message in _CSHARP_PATTERN_RULES:
        if re.search(pattern, cleaned):
            warnings.append(message)
    return warnings


# ---------------------------------------------------------------------------
# Luau lint check + AI reprompt loop
# ---------------------------------------------------------------------------

def _luau_syntax_check(luau_source: str) -> list[str]:
    """Run luau-analyze on the source and return SyntaxError lines.

    Returns an empty list if the source is syntactically valid.
    Only reports SyntaxError lines — TypeErrors for unknown Roblox globals
    are expected and filtered out.
    """
    import shutil
    analyzer = shutil.which("luau-analyze")
    if not analyzer:
        return []  # No analyzer available, skip check

    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".luau", delete=False, encoding="utf-8") as f:
        f.write(luau_source)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [analyzer, tmp_path],
            capture_output=True, text=True, timeout=10,
        )
        # Extract only SyntaxError lines
        errors = []
        for line in result.stdout.splitlines() + result.stderr.splitlines():
            if "SyntaxError" in line:
                # Strip the temp file path for cleaner error messages
                clean = line.replace(tmp_path, "script")
                errors.append(clean)
        return errors
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    finally:
        os.unlink(tmp_path)


def _reprompt_fix(luau_source: str, syntax_errors: list[str], original_csharp: str = "") -> str:
    """Ask Claude CLI to fix syntax errors in transpiled Luau code.

    Sends the broken Luau source + error messages to Claude with a focused
    fix prompt. Returns the corrected source.
    """
    import shutil
    claude_path = shutil.which("claude")
    if not claude_path:
        return luau_source  # Can't reprompt without CLI

    error_text = "\n".join(syntax_errors[:10])  # Cap at 10 errors

    prompt = (
        "The following Luau script has syntax errors reported by luau-analyze. "
        "Fix ALL the syntax errors and return ONLY the corrected Luau code. "
        "No markdown fences. No explanations.\n\n"
        "SYNTAX ERRORS:\n"
        f"{error_text}\n\n"
        "BROKEN LUAU SOURCE:\n"
        f"{luau_source}"
    )

    try:
        result = subprocess.run(
            [claude_path, "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0 and result.stdout.strip():
            fixed = _strip_code_fences(result.stdout.strip())
            # Sanity check: fixed version should still have key Luau patterns
            if "end" in fixed and len(fixed) > len(luau_source) * 0.3:
                return fixed
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return luau_source  # Return original if reprompt failed


def _lint_and_fix(luau_source: str, class_name: str = "",
                  original_csharp: str = "", max_retries: int = 2) -> tuple[str, list[str]]:
    """Run luau-analyze, reprompt Claude to fix syntax errors if found.

    Returns (fixed_source, warnings).
    """
    warnings = []
    for attempt in range(max_retries + 1):
        errors = _luau_syntax_check(luau_source)
        if not errors:
            if attempt > 0:
                log.info("  [%s] Lint clean after %d reprompt(s)", class_name, attempt)
            return luau_source, warnings

        if attempt < max_retries:
            log.info("  [%s] luau-analyze found %d syntax error(s), reprompting (attempt %d)...",
                     class_name, len(errors), attempt + 1)
            luau_source = _reprompt_fix(luau_source, errors, original_csharp)
        else:
            log.warning("  [%s] %d syntax error(s) remain after %d reprompt(s): %s",
                        class_name, len(errors), max_retries, errors[0])
            warnings.append(f"luau-analyze: {len(errors)} syntax errors remain after {max_retries} reprompts")

    return luau_source, warnings


# ---------------------------------------------------------------------------
# LLM cache
# ---------------------------------------------------------------------------

def _cache_key(source: str, model: str) -> str:
    """Generate a SHA-256 cache key for a source + model combination."""
    content = f"{model}:{source}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _ai_cache_key(
    *,
    csharp_source: str,
    class_name: str,
    script_type: str,
    project_context: str,
    prompt_hash: str,
    model: str,
) -> str:
    """Build a cache key from the AI-transpile inputs without collisions.

    Concatenating variable-length fields without separators lets
    distinct inputs share a prehash:
    ``("ab", "c", ...)`` and ``("a", "bc", ...)`` both yield "abc...".
    Encode each field's length explicitly so the field boundaries are
    unambiguous regardless of content.
    """
    parts = [
        model, csharp_source, class_name, script_type,
        project_context, prompt_hash,
    ]
    framed = "".join(f"{len(p)}:{p}|" for p in parts)
    return hashlib.sha256(framed.encode("utf-8")).hexdigest()


def _cache_path(key: str) -> Path:
    """Return the file path for a cache entry."""
    cache_dir = Path(LLM_CACHE_DIR)
    return cache_dir / f"{key}.json"


def _load_cache(key: str) -> dict[str, Any] | None:
    """Load a cached transpilation result if it exists and is not expired."""
    if not LLM_CACHE_ENABLED:
        return None

    path = _cache_path(key)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    # Check TTL.
    cached_at = data.get("cached_at", 0)
    if time.time() - cached_at > LLM_CACHE_TTL_SECONDS:
        return None

    return data


def _save_cache(key: str, data: dict[str, Any]) -> None:
    """Save a transpilation result to the cache."""
    if not LLM_CACHE_ENABLED:
        return

    path = _cache_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)

    data["cached_at"] = time.time()
    try:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as exc:
        log.debug("Failed to write cache: %s", exc)
