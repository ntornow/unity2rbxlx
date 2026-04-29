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
from converter.api_mappings import (
    API_CALL_MAP,
    LIFECYCLE_MAP,
    SERVICE_IMPORTS,
    TYPE_MAP,
    UTILITY_FUNCTIONS,
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
    ai_results: dict[str, tuple[str, float, list[str]]] = {}  # class_name → (luau, confidence, warnings)

    # Map every pending script to a unique stem so the dep-graph code has
    # a stable key. Prefer the class name (keeps cross-reference matching
    # idiomatic); when two scripts share one, disambiguate by appending a
    # short path-based suffix so the second script still gets its own AI
    # pass — silently dropping duplicates would regress to the pre-PR-4
    # behaviour for legitimate cases like two Utils.cs in different dirs.
    file_sources: dict[str, str] = {}
    info_by_stem: dict[str, tuple[Any, str, str]] = {}
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
                        ai_results[info.class_name or str(info.path)] = outcome
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
                    ai_results[info.class_name or str(info.path)] = outcome
                    if luau and confidence >= 0.1:
                        transpiled_luau[stem_out] = luau
                    result.total_ai += 1
                    log.info("  %s: transpiled via %s (confidence %.2f)",
                             info.path.name, backend, confidence)

    # Phase 3: Assemble results, falling back to rule-based where AI didn't produce output
    for info, csharp_source, script_type in pending_scripts:
        key = info.class_name or str(info.path)
        luau, confidence, warnings, strategy = "", 0.0, [], "rule_based"

        if key in ai_results:
            luau, confidence, warnings = ai_results[key]
            strategy = "ai"

        # Phase 5.11: pre-AI pattern warnings — prepend before any AI/stub
        # warnings so a reader sees structural hazards first. The patterns
        # don't depend on transpile output and apply uniformly across
        # rule-based, AI, and stub strategies.
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


# ---------------------------------------------------------------------------
# Multi-line construct preprocessing
# ---------------------------------------------------------------------------

def _preprocess_multiline_constructs(source: str) -> str:
    """Pre-process multi-line C# constructs before line-by-line transpilation.

    Handles: multi-line enums, property get/set bodies, out parameters,
    yield return, null-conditional chains, is-type patterns.
    """
    source = _preprocess_conditional_compilation(source)
    source = _preprocess_multiline_enums(source)
    source = _preprocess_property_bodies(source)
    source = _preprocess_out_params(source)
    source = _preprocess_yield_return(source)
    source = _preprocess_null_conditional(source)
    source = _preprocess_is_type_check(source)
    source = _preprocess_async_await(source)
    source = _preprocess_using_blocks(source)
    source = _preprocess_lock_blocks(source)
    return source


def _preprocess_conditional_compilation(source: str) -> str:
    """Strip #if UNITY_EDITOR / #if UNITY_STANDALONE blocks entirely.

    These blocks contain editor-only or platform-specific code that won't
    work in Roblox. We keep the #else/#elif content (if any) as fallback.
    """
    # Symbols that should be stripped (editor/platform-specific)
    strip_symbols = {
        "UNITY_EDITOR", "UNITY_EDITOR_WIN", "UNITY_EDITOR_OSX",
        "UNITY_STANDALONE", "UNITY_STANDALONE_WIN", "UNITY_STANDALONE_OSX",
        "UNITY_ANDROID", "UNITY_IOS", "UNITY_WEBGL",
        "UNITY_PS4", "UNITY_PS5", "UNITY_XBOXONE", "UNITY_SWITCH",
        "DEBUG", "DEVELOPMENT_BUILD",
    }

    lines = source.split("\n")
    result = []
    skip_depth = 0  # depth of nested #if blocks we're skipping
    # Stack tracks whether each nesting level has a "skip else" flag
    # When we see #if !UNITY_EDITOR, we keep the block but need to skip #else
    skip_else_stack: list[bool] = []

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("#if ") or stripped.startswith("#if\t"):
            raw_symbol = stripped[4:].strip()
            negated = raw_symbol.startswith("!")
            symbol = raw_symbol.lstrip("!").strip()

            if skip_depth > 0:
                # Already skipping — increase depth
                skip_depth += 1
                skip_else_stack.append(False)
                continue

            if symbol in strip_symbols:
                if negated:
                    # #if !UNITY_EDITOR → keep block, skip #else
                    skip_else_stack.append(True)  # mark: skip the else
                    continue
                else:
                    # #if UNITY_EDITOR → skip block, keep #else
                    skip_depth = 1
                    skip_else_stack.append(False)
                    continue

            # Keep #if blocks for unknown symbols (comment them)
            skip_else_stack.append(False)
            result.append(line)
            continue

        if stripped == "#else":
            if skip_depth == 1:
                # We were skipping the #if block, now keep the #else content
                skip_depth = 0
                continue
            if skip_depth > 1:
                continue
            # Check if we need to skip the else (negated #if)
            if skip_else_stack and skip_else_stack[-1]:
                skip_depth = 1
                continue
            result.append(line)
            continue

        if stripped.startswith("#elif"):
            if skip_depth == 1:
                # Treat as end of stripped block, keep elif content
                skip_depth = 0
                continue
            if skip_depth > 1:
                continue
            # Negated blocks: skip elif too
            if skip_else_stack and skip_else_stack[-1]:
                skip_depth = 1
                continue
            result.append(line)
            continue

        if stripped == "#endif":
            if skip_depth > 0:
                skip_depth -= 1
                if skip_else_stack:
                    skip_else_stack.pop()
                continue
            if skip_else_stack:
                skip_else_stack.pop()
            # Don't add the #endif line to output
            continue

        if skip_depth > 0:
            continue

        result.append(line)

    return "\n".join(result)


def _preprocess_multiline_enums(source: str) -> str:
    """Convert multi-line enum declarations to single-line for the line parser."""
    result = []
    lines = source.split("\n")
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        # Match enum declaration: "enum Name {" or "[public] enum Name {"
        m = re.match(r'^(\s*)(?:public\s+|private\s+|protected\s+|internal\s+)?enum\s+(\w+)\s*\{?\s*$', lines[i])
        if m:
            indent = m.group(1)
            enum_name = m.group(2)
            members = []
            i += 1
            # Skip opening brace if on next line
            if i < len(lines) and lines[i].strip() == "{":
                i += 1
            # Collect members until closing brace
            while i < len(lines):
                s = lines[i].strip()
                if s == "}" or s == "};":
                    break
                if s and not s.startswith("//") and not s.startswith("["):
                    # Clean up trailing commas
                    s = s.rstrip(",").strip()
                    if s:
                        members.append(s)
                i += 1
            # Emit as single-line enum that the line parser can handle
            members_str = ", ".join(members)
            result.append(f"{indent}enum {enum_name} {{{members_str}}}")
            i += 1  # skip closing brace
            continue
        result.append(lines[i])
        i += 1
    return "\n".join(result)


def _preprocess_property_bodies(source: str) -> str:
    """Convert C# properties with get/set bodies to Luau getter/setter functions."""
    lines = source.split("\n")
    result = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        # Match property declaration with body: "public Type Name {"
        # but NOT auto-properties (those have get; set; on the same line)
        m = re.match(
            r'^(\s*)(?:public|private|protected|internal)\s+(?:static\s+)?(?:override\s+)?'
            r'(\w+(?:<[\w,\s]+>)?(?:\?)?)\s+(\w+)\s*$',
            lines[i],
        )
        if m and i + 1 < len(lines) and lines[i + 1].strip() == "{":
            indent = m.group(1)
            prop_type = m.group(2)
            prop_name = m.group(3)
            # Look ahead for get/set
            j = i + 2
            brace_depth = 1
            has_get_body = False
            has_set_body = False
            get_body_lines = []
            set_body_lines = []
            current_accessor = None
            accessor_depth = 0

            while j < len(lines) and brace_depth > 0:
                s = lines[j].strip()
                if s.startswith("get"):
                    has_get_body = True
                    current_accessor = "get"
                    accessor_depth = 0
                    if "{" in s:
                        accessor_depth += s.count("{") - s.count("}")
                        # Single-line get: "get { return x; }"
                        body_match = re.search(r'get\s*\{\s*return\s+(.+?)\s*;\s*\}', s)
                        if body_match:
                            get_body_lines.append(body_match.group(1))
                            current_accessor = None
                    j += 1
                    continue
                elif s.startswith("set"):
                    has_set_body = True
                    current_accessor = "set"
                    accessor_depth = 0
                    if "{" in s:
                        accessor_depth += s.count("{") - s.count("}")
                    j += 1
                    continue

                if current_accessor:
                    accessor_depth += s.count("{") - s.count("}")
                    if accessor_depth <= 0:
                        current_accessor = None
                    elif current_accessor == "get":
                        body = re.sub(r'\breturn\s+', '', s).rstrip(";").strip()
                        if body and body != "{" and body != "}":
                            get_body_lines.append(body)
                    j += 1
                    continue

                brace_depth += s.count("{") - s.count("}")
                j += 1

            if has_get_body or has_set_body:
                # Emit as a local variable with comment
                result.append(f"{indent}local {prop_name} = nil -- {prop_type} property")
                if get_body_lines:
                    getter_expr = get_body_lines[0] if len(get_body_lines) == 1 else "nil"
                    result.append(f"{indent}local function get_{prop_name}() return {getter_expr} end")
                i = j
                continue

        result.append(lines[i])
        i += 1
    return "\n".join(result)


def _preprocess_out_params(source: str) -> str:
    """Handle C# out parameters.

    - Strip 'out' keyword from method parameter declarations
    - Convert 'out var x' and 'out Type x' in call sites to just 'x'
    - Handle common patterns like Physics.Raycast(... out hit)
    """
    # Strip 'out' from method call arguments: Method(a, out var x) → Method(a, x)
    source = re.sub(r'\bout\s+var\s+(\w+)', r'\1', source)
    source = re.sub(r'\bout\s+\w+\s+(\w+)', r'\1', source)
    # Strip bare 'out' before variable: Method(a, out x) → Method(a, x)
    source = re.sub(r'\bout\s+(\w+)(?=\s*[,\)])', r'\1', source)
    # Strip 'ref' keyword similarly
    source = re.sub(r'\bref\s+(\w+)(?=\s*[,\)])', r'\1', source)
    return source


def _preprocess_yield_return(source: str) -> str:
    """Convert yield return patterns to task.wait equivalents."""
    # yield return new WaitForSeconds(N) → task.wait(N)
    source = re.sub(
        r'\byield\s+return\s+new\s+WaitForSeconds\s*\(([^)]+)\)',
        r'task.wait(\1)',
        source,
    )
    # yield return new WaitForEndOfFrame() → RunService.RenderStepped:Wait()
    source = re.sub(
        r'\byield\s+return\s+new\s+WaitForEndOfFrame\s*\(\s*\)',
        'game:GetService("RunService").RenderStepped:Wait()',
        source,
    )
    # yield return new WaitForFixedUpdate() → RunService.Heartbeat:Wait()
    source = re.sub(
        r'\byield\s+return\s+new\s+WaitForFixedUpdate\s*\(\s*\)',
        'game:GetService("RunService").Heartbeat:Wait()',
        source,
    )
    # yield return new WaitUntil(() => condition) → repeat task.wait() until condition
    source = re.sub(
        r'\byield\s+return\s+new\s+WaitUntil\s*\(\s*\(\)\s*=>\s*(.+?)\s*\)',
        r'repeat task.wait() until \1',
        source,
    )
    # yield return null → task.wait()
    source = re.sub(r'\byield\s+return\s+null\s*;?', 'task.wait()', source)
    # yield break → return
    source = re.sub(r'\byield\s+break\s*;?', 'return', source)
    # Generic yield return expr → task.wait()
    source = re.sub(r'\byield\s+return\s+[^;]+;?', 'task.wait()', source)
    return source


def _preprocess_null_conditional(source: str) -> str:
    """Decompose ?. null-conditional chains to nil-check patterns.

    obj?.Property → (obj and obj.Property or nil)
    obj?.Method() → if obj then obj:Method() end
    """
    # Method call FIRST (more specific): obj?.Method(args) → if obj then obj.Method(args) end
    source = re.sub(
        r'(\w+)\?\.([\w]+)\(([^)]*)\)',
        r'(if \1 then \1.\2(\3) else nil)',
        source,
    )
    # Simple property access: obj?.Property (not followed by ( or word char)
    # Convert to: (obj and obj.Property or nil)
    source = re.sub(
        r'(\w+)\?\.([\w]+)\b(?!\s*\()',
        r'(\1 and \1.\2 or nil)',
        source,
    )
    # Null-coalescing assignment: x ??= expr → if x == nil then x = expr end
    source = re.sub(
        r'(\w+)\s*\?\?=\s*([^;]+);?',
        r'if \1 == nil then \1 = \2 end',
        source,
    )
    return source


def _preprocess_is_type_check(source: str) -> str:
    """Convert C# 'is' type-check patterns to Luau equivalents.

    obj is Type → obj:IsA("Type") (for Unity/Roblox types)
    obj is null → obj == nil
    obj is not null → obj ~= nil
    """
    # is not null → ~= nil
    source = re.sub(r'(\w+)\s+is\s+not\s+null\b', r'\1 ~= nil', source)
    # is null → == nil
    source = re.sub(r'(\w+)\s+is\s+null\b', r'\1 == nil', source)
    # is Type varName (pattern matching) → typeof(obj) == "Type" and assign
    source = re.sub(
        r'(\w+)\s+is\s+([A-Z]\w+)\s+(\w+)',
        r'\1 ~= nil and typeof(\1) == "\2" -- local \3 = \1',
        source,
    )
    # Simple is Type (without variable binding)
    source = re.sub(
        r'(\w+)\s+is\s+([A-Z]\w+)\b(?!\s+\w)',
        r'typeof(\1) == "\2"',
        source,
    )
    return source


def _preprocess_async_await(source: str) -> str:
    """Convert C# async/await patterns to Luau task.spawn equivalents.

    async Task MethodName() → function MethodName()
    async void MethodName() → function MethodName()
    await Task.Delay(ms) → task.wait(ms/1000)
    await Task.Yield() → task.wait()
    await someTask → someTask (strip await, it's already sync in Luau)
    """
    # Strip 'async' keyword from method declarations
    source = re.sub(r'\basync\s+Task(?:<\w+>)?\s+', '', source)
    source = re.sub(r'\basync\s+void\s+', '', source)
    source = re.sub(r'\basync\s+', '', source)

    # await Task.Delay(ms) → task.wait(ms/1000)
    def _convert_delay(m: re.Match) -> str:
        ms = m.group(1).strip()
        try:
            val = float(ms)
            return f"task.wait({val / 1000})"
        except ValueError:
            return f"task.wait({ms} / 1000)"

    source = re.sub(r'\bawait\s+Task\.Delay\((.+?)\)', _convert_delay, source)

    # await Task.Yield() → task.wait()
    source = re.sub(r'\bawait\s+Task\.Yield\(\)', 'task.wait()', source)

    # await UniTask.Delay(ms) → task.wait(ms/1000)
    source = re.sub(r'\bawait\s+UniTask\.Delay\((.+?)\)', _convert_delay, source)

    # await UniTask.Yield() → task.wait()
    source = re.sub(r'\bawait\s+UniTask\.Yield\(\)', 'task.wait()', source)

    # Generic await expr → expr (strip await keyword)
    source = re.sub(r'\bawait\s+', '', source)

    return source


def _preprocess_using_blocks(source: str) -> str:
    """Strip C# 'using' resource disposal blocks.

    using (var x = new Something()) { ... } → var x = new Something(); ...
    Luau has no IDisposable; just inline the variable and body.
    """
    lines = source.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()
        # Match: using (var/Type name = expr) {
        m = re.match(r'^(\s*)using\s*\((.+)\)\s*\{\s*$', line)
        if m:
            indent = m.group(1)
            inner = m.group(2).strip()
            result.append(f"{indent}{inner}")
            result.append(f"{indent}do")
            continue
        # using var x = expr; (C# 8 simplified using declaration)
        if re.match(r'^\s*using\s+var\s+', stripped):
            result.append(line.replace('using ', '', 1))
            continue
        result.append(line)
    return "\n".join(result)


def _preprocess_lock_blocks(source: str) -> str:
    """Strip C# lock statements (Luau is single-threaded).

    lock (obj) { ... } → { ... }
    """
    source = re.sub(r'\block\s*\([^)]*\)\s*\{', '{', source)
    return source


# ---------------------------------------------------------------------------
# Rule-based transpilation
# ---------------------------------------------------------------------------

def _rule_based_transpile(
    csharp_source: str,
    api_mappings: dict[str, str] | None = None,
) -> tuple[str, float, list[str]]:
    """DEPRECATED: Legacy regex-based transpiler.

    Kept as a thin wrapper around the stub generator for backward
    compatibility with tests that call it directly. New code should
    use AI transpilation (the default path) which produces superior
    output. The 500+ lines of regex patterns were removed because:
    - AI transpilation handles generics, LINQ, delegates, async/await
    - Regex patterns caused catastrophic backtracking (13 min hangs)
    - Dual maintenance (regex + API mappings + validator) was wasteful

    Returns a stub module with the original C# commented out.
    """
    # Extract class name from source for the stub
    import re as _re
    class_match = _re.search(r'class\s+(\w+)', csharp_source)
    class_name = class_match.group(1) if class_match else "Module"

    # Generate stub: comment out C# source, create skeleton module
    lines = []
    lines.append(f"-- Auto-generated stub for {class_name}")
    lines.append(f"-- AI transpilation recommended for full conversion")
    lines.append(f"local {class_name} = {{}}")
    lines.append("")
    for cs_line in csharp_source.split("\n"):
        lines.append(f"-- [C#] {cs_line}")
    lines.append("")
    lines.append(f"return {class_name}")

    return "\n".join(lines), 0.3, ["Stub generated (AI transpilation unavailable)"]

def _sanitize_luau(source: str) -> str:
    """Final safety pass: comment out lines with invalid Luau syntax.

    Ensures the output always parses, even if some logic is lost.
    """
    lines = source.split("\n")
    result = []
    for line in lines:
        stripped = line.strip()

        # Already a comment or empty
        if not stripped or stripped.startswith("--"):
            result.append(line)
            continue

        # Detect lines that will definitely cause Luau parse errors
        is_invalid = False

        # C# array declarations: Type[] var
        if re.search(r"\w+\[\]\s+\w+", stripped):
            is_invalid = True
        # C# generic types: List<Type>, Dictionary<K,V>
        if re.search(r"\w+<\w+[,\s]*\w*>\s+\w+", stripped) and "FindFirstChild" not in stripped:
            is_invalid = True
        # C# cast: (Type)expr
        if re.match(r"^\s*\(\w+\)\s*\w+", stripped):
            is_invalid = True
        # Stray C# keywords that aren't valid Luau
        if re.match(r"^\s*(delegate|event|interface|struct|enum|abstract|override|virtual|sealed|readonly|volatile|unsafe|fixed|checked|unchecked|implicit|explicit|operator|extern)\b", stripped):
            is_invalid = True
        # C# property accessors
        if re.match(r"^\s*(get|set)\s*$", stripped):
            is_invalid = True
        # Lines starting with C# type annotations without local
        if re.match(r"^\s*(void|int|float|double|bool|string|char|byte|short|long|uint|ulong)\s+\w+\s*\(", stripped):
            is_invalid = True
        # C# switch/case with braces (only if not already converted)
        if re.match(r"^\s*switch\s*\(", stripped) and "-- switch" not in stripped:
            is_invalid = True
        if re.match(r"^\s*case\s+.+:", stripped) and "then" not in stripped and "-- " not in stripped:
            is_invalid = True
        # Stray semicolons on their own
        if stripped == ";":
            continue
        # C# object initializers: new Type { ... }
        if re.search(r"\bnew\s+\w+\s*\{", stripped):
            is_invalid = True
        # C# lambda: =>
        if "=>" in stripped and "-- " not in stripped:
            is_invalid = True
        # using statements that weren't caught
        if re.match(r"^\s*using\s+", stripped):
            is_invalid = True

        if is_invalid:
            indent = len(line) - len(line.lstrip())
            result.append(f"{' ' * indent}-- [C#] {stripped}")
        else:
            result.append(line)

    return "\n".join(result)


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


def _has_syntax_errors(luau_source: str) -> bool:
    """Quick heuristic check if Luau source has obvious syntax errors."""
    code_lines = 0
    error_lines = 0
    for line in luau_source.split("\n"):
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        code_lines += 1
        # C# method signatures
        if re.match(r"(?:void|bool|int|float|string|IEnumerator|GameObject|Transform)\s+\w+\s*\(", s):
            error_lines += 1
        # Stray closing braces
        elif s == "}":
            error_lines += 1
        # C# access modifiers as statements
        elif re.match(r"(?:public|private|protected)\s+", s):
            error_lines += 1
        # C# array types
        elif re.search(r"\w+\[\]\s+\w+", s):
            error_lines += 1
        # C# generic types in code (not comments)
        elif re.search(r"<\w+>", s) and "FindFirstChild" not in s:
            error_lines += 1
        # Stray 'end' at the very start (from C# property getters) — only if first 3 code lines
        elif s == "end" and code_lines < 4:
            error_lines += 1
        # C# if without 'then'
        elif re.match(r"if\s*\(.+\)\s*$", s) and "then" not in s:
            error_lines += 1
        # Incomplete statements with C# patterns
        elif re.search(r"\.\w+\(.*\)\.\w+\(", s) and ":" not in s:
            error_lines += 1

    # If more than 25% of code lines have issues, it's broken
    if code_lines > 0 and error_lines / code_lines > 0.25:
        return True
    # Or if there are many hard errors (allow some from C# brace remnants)
    if error_lines > 4:
        return True
    return False


def _convert_interpolated_string(content: str) -> str:
    """Convert C# interpolated string content to string.format.

    Handles format specifiers: {value:F2} → %.2f, {n:N0} → %d, {x:X} → %x, etc.
    """
    parts = re.findall(r"\{([^}]+)\}", content)
    if not parts:
        return f'"{content}"'

    # Build format string and argument list
    fmt_parts = []
    arg_list = []
    last_end = 0

    for m in re.finditer(r"\{([^}]+)\}", content):
        fmt_parts.append(content[last_end:m.start()])
        expr = m.group(1)
        last_end = m.end()

        # Check for format specifier: "expr:spec"
        if ":" in expr:
            var, spec = expr.split(":", 1)
            fmt_spec = _csharp_format_to_lua(spec)
            fmt_parts.append(fmt_spec)
            arg_list.append(var.strip())
        else:
            fmt_parts.append("%s")
            arg_list.append(f"tostring({expr.strip()})")

    fmt_parts.append(content[last_end:])
    fmt_str = "".join(fmt_parts)
    args = ", ".join(arg_list)
    return f'string.format("{fmt_str}", {args})'


def _csharp_format_to_lua(spec: str) -> str:
    """Convert C# format specifier to Lua string.format specifier.

    F2 → %.2f, N0 → %d, X → %x, D3 → %03d, P → %.0f%%, etc.
    """
    spec = spec.strip()
    s_upper = spec.upper()

    # Fixed-point: F, F2, F3 → %.Nf
    m = re.match(r'^[Ff](\d*)$', spec)
    if m:
        decimals = m.group(1) or "2"
        return f"%.{decimals}f"

    # Number with grouping: N, N0, N2 → %.Nf (no grouping in Lua)
    m = re.match(r'^[Nn](\d*)$', spec)
    if m:
        decimals = m.group(1) or "2"
        if decimals == "0":
            return "%d"
        return f"%.{decimals}f"

    # Decimal: D, D3 → %d or %03d
    m = re.match(r'^[Dd](\d*)$', spec)
    if m:
        width = m.group(1)
        if width:
            return f"%0{width}d"
        return "%d"

    # Hex: X, X2 → %x, %02x
    m = re.match(r'^[Xx](\d*)$', spec)
    if m:
        width = m.group(1)
        if width:
            return f"%0{width}x"
        return "%x"

    # Percent: P, P0 → multiply by 100 and add %
    m = re.match(r'^[Pp](\d*)$', spec)
    if m:
        decimals = m.group(1) or "0"
        return f"%.{decimals}f%%"

    # General: G, E → %g, %e
    if s_upper.startswith("G"):
        return "%g"
    if s_upper.startswith("E"):
        return "%e"

    # Default: just use %s
    return "%s"


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
  **IMPORTANT**: `.Touched` only works on BasePart instances, NOT Models. If `script.Parent` is a Model, find the first BasePart child: `local part = script.Parent:FindFirstChildWhichIsA("BasePart")`

Core:
- `Debug.Log/LogWarning/LogError` → `print` / `warn`
- `Instantiate(prefab)` → `prefab:Clone(); clone.Parent = workspace`
- `Destroy(obj)` / `Destroy(obj, delay)` → `obj:Destroy()` / `game:GetService("Debris"):AddItem(obj, delay)`
- `GetComponent<T>()` → `:FindFirstChildWhichIsA("T")`
- `transform.position` → `part.Position`, `transform.rotation` → `part.CFrame`
- `transform.forward/right/up` → `part.CFrame.LookVector/RightVector/UpVector`
- `transform.localScale` → `part.Size`
- `gameObject.SetActive(false)` → set `Transparency=1, CanCollide=false` (or use recursive helper)
- `gameObject` / `this` → `script.Parent` (the part/model the script is parented to)

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
- Use `character:FindFirstChild("Head").Position + Vector3.new(0, 0.5, 0)` for camera position
- Track yaw (mouse X) and pitch (mouse Y) as separate angles, combine into camera CFrame:
  `camera.CFrame = CFrame.new(headPos) * CFrame.Angles(0, yawAngle, 0) * CFrame.Angles(pitchAngle, 0, 0)`
- Do NOT modify `rootPart.CFrame` for rotation — this conflicts with Roblox's Humanoid movement controller
- For camera-relative movement, compute direction from camera yaw:
  `local moveDir = (CFrame.Angles(0, yawAngle, 0) * inputDir).Unit; humanoid:Move(moveDir)`

Physics:
- `Physics.Raycast(origin, dir, dist)` → `workspace:Raycast(origin, dir * dist, RaycastParams.new())`
- `Rigidbody.velocity` → `part.AssemblyLinearVelocity`
- `Rigidbody.AddForce` → `part:ApplyImpulse(force)`
- `Rigidbody.isKinematic` → `part.Anchored`
- `Physics.OverlapSphere` → `workspace:GetPartBoundsInRadius(center, radius)`

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
- `Animator.SetBool/SetFloat/SetInteger/SetTrigger` → `humanoid:SetAttribute("ParamName", value)`
- `Animator.Play("StateName")` → `animTrack:Play()`

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
    cache_key = _cache_key(csharp_source + class_name + script_type + project_context + _prompt_hash, model)
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
    cache_key = _cache_key(csharp_source + class_name + script_type + project_context + _prompt_hash, "claude-cli-v4")
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

    try:
        result = subprocess.run(
            [claude_path, "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"claude CLI exited with code {result.returncode}: {result.stderr[:200]}")
        luau_source = result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude CLI timed out after 120s")
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

    Phase 5.12 harmonization: when the source isn't clearly client-side
    and isn't a MonoBehaviour/NetworkBehaviour, default to ``ModuleScript``
    (matching the source repo). The downstream ``script_coherence``
    pass already promotes required-by-others scripts to ``ModuleScript``;
    starting from ``ModuleScript`` for non-MonoBehaviour code prevents
    spurious "reclassified to ModuleScript" reclassifications. Genuine
    server gameplay (MonoBehaviour with no client APIs) still resolves
    to ``Script``.
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
# Phase 5.11: pre-AI C# pattern analysis
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
