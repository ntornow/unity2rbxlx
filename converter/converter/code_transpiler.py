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
import re
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
) -> TranspilationResult:
    """Transpile a list of C# scripts to Luau.

    Args:
        unity_project_path: Root of the Unity project (for relative path resolution).
        script_infos: List of ScriptInfo objects from the script analyzer.
        use_ai: Whether to attempt AI transpilation for low-confidence scripts.
        api_key: Anthropic API key (required if use_ai is True).
        max_concurrent: Max concurrent API calls for AI transpilation.

    Returns:
        TranspilationResult with all transpiled scripts and summary counts.
    """
    result = TranspilationResult()

    # Build project context for AI transpilation
    project_context = _build_project_context(script_infos)

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

    # Phase 2: AI transpilation (concurrent for API, serial for CLI)
    ai_results: dict[str, tuple[str, float, list[str]]] = {}  # class_name → (luau, confidence, warnings)

    if use_ai and pending_scripts:
        backend = _find_transpiler() if not api_key else "anthropic_api"

        if backend == "anthropic_api" and api_key and len(pending_scripts) > 1:
            # Concurrent API calls for better throughput
            from concurrent.futures import ThreadPoolExecutor, as_completed
            futures = {}
            with ThreadPoolExecutor(max_workers=min(max_concurrent, len(pending_scripts))) as executor:
                for info, csharp_source, script_type in pending_scripts:
                    future = executor.submit(
                        _ai_transpile,
                        csharp_source, api_key, ANTHROPIC_MODEL,
                        class_name=info.class_name,
                        script_type=script_type,
                        project_context=project_context,
                    )
                    futures[future] = info
                for future in as_completed(futures):
                    info = futures[future]
                    try:
                        luau, confidence, warnings = future.result()
                        ai_results[info.class_name or str(info.path)] = (luau, confidence, warnings)
                        result.total_ai += 1
                        log.info("  %s: transpiled via Anthropic API (confidence %.2f)",
                                 info.path.name, confidence)
                    except Exception as exc:
                        log.warning("AI transpilation failed for %s: %s",
                                    info.path.name, exc)
        else:
            # Serial transpilation (CLI or single script)
            for info, csharp_source, script_type in pending_scripts:
                try:
                    if backend == "claude_cli":
                        luau, confidence, warnings = _claude_cli_transpile(
                            csharp_source,
                            class_name=info.class_name,
                            script_type=script_type,
                            project_context=project_context,
                        )
                    elif backend == "anthropic_api" and api_key:
                        luau, confidence, warnings = _ai_transpile(
                            csharp_source, api_key, ANTHROPIC_MODEL,
                            class_name=info.class_name,
                            script_type=script_type,
                            project_context=project_context,
                        )
                    else:
                        continue
                    ai_results[info.class_name or str(info.path)] = (luau, confidence, warnings)
                    result.total_ai += 1
                    log.info("  %s: transpiled via %s (confidence %.2f)",
                             info.path.name, backend, confidence)
                except Exception as exc:
                    log.warning("AI transpilation failed for %s: %s, falling back to rule-based",
                                info.path.name, exc)

    # Phase 3: Assemble results, falling back to rule-based where AI didn't produce output
    for info, csharp_source, script_type in pending_scripts:
        key = info.class_name or str(info.path)
        luau, confidence, warnings, strategy = "", 0.0, [], "rule_based"

        if key in ai_results:
            luau, confidence, warnings = ai_results[key]
            strategy = "ai"

        # Fall back to rule-based if AI didn't run or failed
        if not luau or confidence < 0.1:
            luau, confidence, warnings = _rule_based_transpile(csharp_source, API_CALL_MAP)
            strategy = "rule_based"
            result.total_rule_based += 1

        # Final safety net: if rule-based output still looks broken,
        # use a guaranteed-valid stub instead
        if strategy == "rule_based" and _has_syntax_errors(luau):
            from converter.stub_generator import generate_stub
            luau = generate_stub(csharp_source, info)
            strategy = "stub"
            confidence = 0.3
            warnings.append("Used stub generator (rule-based output had syntax errors)")

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
    # yield return new WaitForEndOfFrame() → task.wait()
    source = re.sub(
        r'\byield\s+return\s+new\s+WaitForEndOfFrame\s*\(\s*\)',
        'task.wait()',
        source,
    )
    # yield return new WaitForFixedUpdate() → task.wait()
    source = re.sub(
        r'\byield\s+return\s+new\s+WaitForFixedUpdate\s*\(\s*\)',
        'task.wait()',
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
    """Transpile C# source to Luau using regex-based pattern matching.

    Args:
        csharp_source: The C# source code.
        api_mappings: The API_CALL_MAP to use for substitution.

    Returns:
        Tuple of (luau_source, confidence, warnings).
    """
    if api_mappings is None:
        api_mappings = API_CALL_MAP
    warnings: list[str] = []

    # Pre-process: handle multi-line constructs before line-by-line pass
    csharp_source = _preprocess_multiline_constructs(csharp_source)

    lines = csharp_source.split("\n")
    output_lines: list[str] = []
    services_needed: set[str] = set()
    matched_patterns = 0
    total_code_lines = 0

    # Stateful tracking for switch/case blocks
    in_switch = False
    switch_var = ""
    first_case = True

    for line in lines:
        stripped = line.strip()

        # Skip empty lines (preserve them).
        if not stripped:
            output_lines.append("")
            continue

        total_code_lines += 1

        # -- Remove using import statements (not 'using' blocks which are preprocessed) --
        if stripped.startswith("using ") and not stripped.startswith("using ("):
            output_lines.append(f"-- {stripped}")
            matched_patterns += 1
            continue

        # -- Remove namespace declarations --
        if stripped.startswith("namespace "):
            output_lines.append(f"-- {stripped}")
            matched_patterns += 1
            continue

        # -- Convert class declarations --
        m = re.match(
            r"^\s*(?:public\s+)?(?:abstract\s+|sealed\s+|partial\s+)?class\s+(\w+)\s*(?::\s*([\w,\s]+))?\s*\{?\s*$",
            line,
        )
        if m:
            class_name = m.group(1)
            base_class = m.group(2) or ""
            output_lines.append(f"-- class {class_name} : {base_class}")
            output_lines.append(f"local {class_name} = {{}}")
            matched_patterns += 1
            continue

        # -- Convert interface declarations -> comment (no Luau equivalent)
        m_iface = re.match(
            r"^\s*(?:public\s+)?interface\s+(\w+)\s*(?::\s*\w+)?\s*\{?\s*$",
            line,
        )
        if m_iface:
            output_lines.append(f"-- interface {m_iface.group(1)} (no Luau equivalent)")
            matched_patterns += 1
            continue

        # -- Convert struct declarations -> table
        m_struct = re.match(
            r"^\s*(?:public\s+)?(?:readonly\s+)?struct\s+(\w+)\s*(?::\s*\w+)?\s*\{?\s*$",
            line,
        )
        if m_struct:
            output_lines.append(f"-- struct {m_struct.group(1)}")
            output_lines.append(f"local {m_struct.group(1)} = {{}}")
            matched_patterns += 1
            continue

        # -- Convert lifecycle methods --
        lifecycle_match = False
        for hook, replacement in LIFECYCLE_MAP.items():
            pattern = rf"(?:void|IEnumerator)\s+{re.escape(hook)}\s*\("
            if re.search(pattern, line):
                output_lines.append(f"{replacement}")
                lifecycle_match = True
                matched_patterns += 1
                # Track required services.
                if "RunService" in replacement:
                    services_needed.add("RunService")
                break
        if lifecycle_match:
            continue

        # -- Skip C# attributes like [Range(...)], [HideInInspector], [SerializeField] --
        if re.match(r"^\s*\[[\w(,.\s\"\')\]]+\]\s*$", stripped):
            matched_patterns += 1
            continue

        # -- Skip #if / #endif preprocessor directives --
        if stripped.startswith("#"):
            output_lines.append(f"-- {stripped}")
            matched_patterns += 1
            continue

        # -- Switch/case stateful conversion --
        m_switch = re.match(r"^\s*switch\s*\((.+)\)\s*\{?\s*$", stripped)
        if m_switch:
            in_switch = True
            switch_var = m_switch.group(1).strip()
            first_case = True
            matched_patterns += 1
            continue

        if in_switch:
            # case "value": or case EnumValue: or case 0:
            m_case = re.match(r'^\s*case\s+(.+?):\s*$', stripped)
            if m_case:
                case_val = m_case.group(1).strip()
                indent = len(line) - len(line.lstrip())
                if first_case:
                    output_lines.append(f"{' ' * indent}if {switch_var} == {case_val} then")
                    first_case = False
                else:
                    output_lines.append(f"{' ' * indent}elseif {switch_var} == {case_val} then")
                matched_patterns += 1
                continue
            if re.match(r'^\s*default\s*:\s*$', stripped):
                indent = len(line) - len(line.lstrip())
                output_lines.append(f"{' ' * indent}else")
                matched_patterns += 1
                continue
            if stripped == "break" or stripped == "break;":
                matched_patterns += 1
                continue
            # End of switch block
            if stripped == "}" or stripped == "end":
                in_switch = False
                switch_var = ""
                first_case = True
                output_lines.append(f"{' ' * (len(line) - len(line.lstrip()))}end")
                matched_patterns += 1
                continue

        # -- C# property get/set blocks -> skip --
        if re.match(r"^\s*(?:get|set)\s*\{", stripped):
            continue

        # -- C# auto-properties: "[access] Type Name { get; set; }" -> local Name = nil
        m_autoprop = re.match(
            r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:override\s+)?(\w+(?:<[\w,\s]+>)?(?:\?)?)\s+(\w+)\s*\{\s*get;\s*(?:(?:private\s+)?set;)?\s*\}",
            stripped,
        )
        if m_autoprop:
            output_lines.append(f"    local {m_autoprop.group(2)} = nil -- {m_autoprop.group(1)} property")
            matched_patterns += 1
            continue

        # -- C# enum declaration -> convert to table
        m_enum = re.match(r"^\s*enum\s+(\w+)\s*\{([^}]*)\}", stripped)
        if m_enum:
            enum_name = m_enum.group(1)
            members = [m.strip() for m in m_enum.group(2).split(",") if m.strip()]
            output_lines.append(f"local {enum_name} = {{")
            for i, member in enumerate(members):
                # Handle "Name = value" and plain "Name"
                if "=" in member:
                    name, val = member.split("=", 1)
                    output_lines.append(f"    {name.strip()} = {val.strip()},")
                else:
                    output_lines.append(f"    {member} = {i},")
            output_lines.append("}")
            matched_patterns += 1
            continue

        # -- Pure C# field declarations without = (no initializer) -> comment out --
        # e.g. "private AudioSource source;" or "public GameObject explosion;"
        m_field = re.match(
            r"^\s*(?:public|private|protected|internal)\s+(?:static\s+)?(\w+(?:<[\w,\s]+>)?(?:\[\])?)\s+(\w+)\s*;?\s*$",
            stripped,
        )
        if m_field:
            output_lines.append(f"    local {m_field.group(2)} = nil -- {m_field.group(1)}")
            matched_patterns += 1
            continue

        # -- C# method signatures -> convert to local function --
        m_method = re.match(
            r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:override\s+)?(?:virtual\s+)?(?:async\s+)?(?:void|bool|int|float|double|string|IEnumerator|Task|[\w<>\[\].]+)\s+(\w+)\s*\(\s*([^)]*)\)\s*\{?\s*$",
            stripped,
        )
        if m_method and m_method.group(1) not in ("if", "while", "for", "switch", "catch"):
            func_name = m_method.group(1)
            params = m_method.group(2)
            # Convert C# params to Luau params (strip types)
            luau_params = []
            if params.strip():
                for p in params.split(","):
                    p = p.strip()
                    parts = p.split()
                    if len(parts) >= 2:
                        luau_params.append(parts[-1])
                    elif parts:
                        luau_params.append(parts[0])
            indent = len(line) - len(line.lstrip())
            output_lines.append(f"{' ' * indent}local function {func_name}({', '.join(luau_params)})")
            matched_patterns += 1
            continue

        # -- Apply line-level transformations --
        converted = line

        # C-style comments // -> --
        converted = re.sub(r"//(.*)$", r"--\1", converted)

        # Remove access modifiers and C# keywords with no Luau equivalent
        converted = re.sub(r"\b(public|private|protected|internal)\s+", "", converted)
        converted = re.sub(r"\b(static|override|virtual|abstract|sealed|readonly|const|partial|volatile)\s+", "", converted)

        # Float literal suffixes: 5f -> 5, 0.8f -> 0.8
        converted = re.sub(r"(\d+(?:\.\d+)?)f\b", r"\1", converted)

        # for loop: must run BEFORE variable declaration regex (which would convert "int i = 0")
        # Pattern: for (int i = START; i < END; i++) → for i = START, END-1 do
        m_for = re.match(
            r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*(\d+)\s*;\s*\2\s*<\s*(\w+(?:\.\w+)?)\s*;\s*\2\+\+\s*\)",
            converted,
        )
        if m_for:
            converted = f"{m_for.group(1)}for {m_for.group(2)} = {m_for.group(3)}, {m_for.group(4)}-1 do"
        if not m_for:
            # Pattern: for (int i = 0; i <= END; i++) → for i = 0, END do
            m_for_le = re.match(
                r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*(\d+)\s*;\s*\2\s*<=\s*(\w+(?:\.\w+)?)\s*;\s*\2\+\+\s*\)",
                converted,
            )
            if m_for_le:
                converted = f"{m_for_le.group(1)}for {m_for_le.group(2)} = {m_for_le.group(3)}, {m_for_le.group(4)} do"
                m_for = m_for_le
        if not m_for:
            # Pattern: for (int i = START; i >= END; i--) → for i = START, END, -1 do
            m_for_dec = re.match(
                r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*(\w+(?:\.\w+)?)\s*;\s*\2\s*>=\s*(\d+)\s*;\s*\2--\s*\)",
                converted,
            )
            if m_for_dec:
                converted = f"{m_for_dec.group(1)}for {m_for_dec.group(2)} = {m_for_dec.group(3)}, {m_for_dec.group(4)}, -1 do"
        if not m_for:
            # Expression-based init: for (int i = expr; i < END; i++) → for i = expr, END-1 do
            m_for_expr = re.match(
                r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*([^;]+?)\s*;\s*\2\s*<\s*([^;]+?)\s*;\s*\2\+\+\s*\)",
                converted,
            )
            if m_for_expr:
                converted = f"{m_for_expr.group(1)}for {m_for_expr.group(2)} = {m_for_expr.group(3)}, {m_for_expr.group(4)}-1 do"
                m_for = m_for_expr
        if not m_for:
            # Expression-based decrement: for (int i = expr; i >= END; i--) → for i = expr, END, -1 do
            m_for_expr_dec = re.match(
                r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*([^;]+?)\s*;\s*\2\s*>=\s*([^;]+?)\s*;\s*\2--\s*\)",
                converted,
            )
            if m_for_expr_dec:
                converted = f"{m_for_expr_dec.group(1)}for {m_for_expr_dec.group(2)} = {m_for_expr_dec.group(3)}, {m_for_expr_dec.group(4)}, -1 do"
                m_for = m_for_expr_dec
        if not m_for:
            # Custom step: for (int i = START; i < END; i += STEP) → for i = START, END-1, STEP do
            m_for_step = re.match(
                r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*([^;]+?)\s*;\s*\2\s*<\s*([^;]+?)\s*;\s*\2\s*\+=\s*(\d+)\s*\)",
                converted,
            )
            if m_for_step:
                converted = f"{m_for_step.group(1)}for {m_for_step.group(2)} = {m_for_step.group(3)}, {m_for_step.group(4)}-1, {m_for_step.group(5)} do"
                m_for = m_for_step
        if not m_for:
            # Custom negative step: for (int i = START; i > END; i -= STEP) → for i = START, END+1, -STEP do
            m_for_neg_step = re.match(
                r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*([^;]+?)\s*;\s*\2\s*>\s*([^;]+?)\s*;\s*\2\s*-=\s*(\d+)\s*\)",
                converted,
            )
            if m_for_neg_step:
                converted = f"{m_for_neg_step.group(1)}for {m_for_neg_step.group(2)} = {m_for_neg_step.group(3)}, {m_for_neg_step.group(4)}+1, -{m_for_neg_step.group(5)} do"
                m_for = m_for_neg_step

        # C# variable declarations with types -> local
        # "Type varName = value" -> "local varName = value"
        converted = re.sub(
            r"\b(?:var|int|float|double|bool|string|long|byte|short|uint)\s+(\w+)\s*=",
            r"local \1 =",
            converted,
        )
        # Complex types: "GameObject varName = ..." -> "local varName = ..."
        converted = re.sub(
            r"\b(?:GameObject|Transform|Vector3|Vector2|Quaternion|Color|"
            r"Rigidbody|AudioSource|AudioClip|Animator|Camera|Collider|"
            r"CharacterController|RaycastHit|Ray|Bounds|Rect|Material|"
            r"Texture2D|Sprite|Mesh|NavMeshAgent|Canvas|RectTransform|"
            r"Image|Button|Text|Slider|Toggle)\s+(\w+)\s*=",
            r"local \1 =",
            converted,
        )
        # Array types: "Type[] varName = ..." -> "local varName = ..."
        converted = re.sub(
            r"\b\w+\[\]\s+(\w+)\s*=",
            r"local \1 =",
            converted,
        )
        # Generic types: "List<T> varName = ..." -> "local varName = ..."
        converted = re.sub(
            r"\b\w+<[^>]+>\s+(\w+)\s*=",
            r"local \1 =",
            converted,
        )
        # Custom PascalCase types: "MyClass varName = ..." -> "local varName = ..."
        # Only match UpperCamelCase identifiers followed by a lowercase variable name
        # to avoid false positives with Luau keywords or function calls.
        converted = re.sub(
            r"\b(?!local\b|return\b|function\b|if\b|else\b|end\b|for\b|while\b|repeat\b|until\b|then\b|do\b|not\b|and\b|or\b|true\b|false\b|nil\b)[A-Z]\w+\s+([a-z]\w*)\s*=",
            r"local \1 =",
            converted,
        )

        # Operator substitution.
        converted = re.sub(r"!=", "~=", converted)
        converted = re.sub(r"(?<!\w)&&(?!\w)", " and ", converted)
        converted = re.sub(r"(?<!\w)\|\|(?!\w)", " or ", converted)
        # Negate operator: !expr -> not expr (but not != which is already handled).
        converted = re.sub(r"(?<!=)!(?!=)(\w)", r"not \1", converted)

        # Null -> nil.
        converted = re.sub(r"\bnull\b", "nil", converted)

        # throw new X("...") → error("...") — must run BEFORE new keyword removal
        converted = re.sub(r'\bthrow\s+new\s+\w+\(([^)]*)\)', r'error(\1)', converted)
        converted = re.sub(r'\bthrow\s+(\w+)', r'error(\1)', converted)

        # new keyword removal for constructors
        # Array initialization: "new Type[] { ... }" → "{ ... }"
        converted = re.sub(r"\bnew\s+\w+\[\]\s*", "", converted)
        # List/Dict initialization: "new List<T>()" → "{}"
        converted = re.sub(r"\bnew\s+\w+<[^>]+>\(\)", "{}", converted)
        # Dict initializer: "new Dictionary<K,V> { { "key", val }, ... }" → { ["key"] = val, ... }
        converted = re.sub(
            r'\bnew\s+Dictionary<[^>]+>\s*\{',
            '{',
            converted,
        )
        # List initializer: "new List<T> { a, b, c }" → "{ a, b, c }"
        converted = re.sub(r'\bnew\s+List<[^>]+>\s*\{', '{', converted)
        # HashSet initializer: "new HashSet<T> { a, b }" → "{ a, b }"
        converted = re.sub(r'\bnew\s+HashSet<[^>]+>\s*\{', '{', converted)
        converted = re.sub(r"\bnew\s+(Vector3|Vector2|Color|CFrame|Ray)\(", r"\1.new(", converted)
        converted = re.sub(r"\bnew\s+\w+\(", "(", converted)  # other constructors

        # C# verbatim string @"..." → "..." (remove @ prefix)
        converted = re.sub(r'@"', '"', converted)
        # C# params keyword → strip
        converted = re.sub(r'\bparams\s+', '', converted)

        # nameof(X) → "X" (C# compile-time string of identifier)
        converted = re.sub(r'\bnameof\s*\(\s*([\w.]+)\s*\)', r'"\1"', converted)

        # C# typeof(Type) → "Type" (reflection type object → string name)
        converted = re.sub(r'\btypeof\s*\(\s*([A-Z]\w*)\s*\)', r'"\1"', converted)

        # C# default(Type) → nil (default value expression)
        converted = re.sub(r'\bdefault\s*\(\s*\w+\s*\)', 'nil', converted)

        # Null-coalescing: expr ?? fallback → (if expr ~= nil then expr else fallback)
        converted = re.sub(
            r'(\w+)\s*\?\?\s*([^;,\)]+)',
            r'(if \1 ~= nil then \1 else \2)',
            converted,
        )

        # Lambda expressions: x => expr → function(x) return expr end
        # Multi-line lambda with block: (x, y) => { → function(x, y)
        # Use lookbehind for ( to avoid consuming the outer call paren
        converted = re.sub(
            r'(?<=\()(\w+(?:\s*,\s*\w+)*)\)\s*=>\s*\{',
            r'function(\1)',
            converted,
        )
        # Single-param block lambda: x => { → function(x)
        converted = re.sub(
            r'\b(\w+)\s*=>\s*\{',
            r'function(\1)',
            converted,
        )
        # Multi-param expression lambda: (x, y) => expr → function(x, y) return expr end
        converted = re.sub(
            r'(?<=\()(\w+(?:\s*,\s*\w+)*)\)\s*=>\s*([^,\){]+?)(?=\s*[,\)])',
            r'function(\1) return \2 end',
            converted,
        )
        # Simple single-param: x => x.Name → function(x) return x.Name end
        converted = re.sub(
            r'\b(\w+)\s*=>\s*([^,\)]+?)(?=\s*[,\)])',
            r'function(\1) return \2 end',
            converted,
        )

        # C# cast: (Type)expr -> expr (primitive + numeric + common Unity types)
        # Cast must be followed by a word char or ( — not by ) ; , or EOL
        converted = re.sub(
            r"\((?:int|uint|float|double|decimal|bool|string|byte|sbyte"
            r"|short|ushort|long|ulong|char|object)\)"
            r"(?=\s*[\w(])",
            "", converted,
        )
        # PascalCase type casts: (Transform)obj, (Collider)hit
        # More restrictive: must be preceded by = or , or ( and followed by word
        converted = re.sub(
            r"(?<=[=(,\s])\(([A-Z]\w*)\)(?=\s*\w)",
            "", converted,
        )
        # as Type -> (remove)
        converted = re.sub(r"\s+as\s+\w+", "", converted)

        # Ternary: a ? b : c -> if a then b else c
        # Handles complex conditions like (a > b), a == b, etc.
        # Match: condition ? true_expr : false_expr
        # The condition can be: parenthesized expr, comparison, or simple word
        # IMPORTANT: Only match (cond) ? when:
        # 1. cond doesn't contain commas (those are function args, not conditions)
        # 2. The ( is NOT preceded by a word char (to avoid matching func(0) ?)
        converted = re.sub(
            r"(?<!\w)\(([^),]+)\)\s*\?\s*([^:]+):\s*(.+?)(?=\s*$|\s*;)",
            r"(if (\1) then \2 else \3)",
            converted,
        )
        converted = re.sub(
            r"(\w+(?:\.\w+)*(?:\s*[<>=!~]+\s*\w+(?:\.\w+)*)?)\s*\?\s*([^:]+):\s*(.+?)(?=\s*$|\s*;)",
            r"(if \1 then \2 else \3)",
            converted,
        )
        # try/catch -> pcall
        converted = re.sub(r"\btry\s*\{?\s*$", "local ok, err = pcall(function()", converted)
        converted = re.sub(r"\bcatch\s*\(\s*\w+\s+(\w+)\s*\)\s*\{?\s*$",
                           r"end)\nif not ok then\n    local \1 = err", converted)
        converted = re.sub(r"\bcatch\s*\{?\s*$", "end)\nif not ok then", converted)
        converted = re.sub(r"\bfinally\s*\{?\s*$", "end -- finally", converted)

        # switch/case handled by stateful converter above; catch any remaining
        # bare break statements from switch blocks
        if stripped == "break" or stripped == "break;":
            if in_switch:
                matched_patterns += 1
                continue

        # foreach: "foreach (Type item in collection)" -> "for _, item in collection do"
        # Also handles generic types: "foreach (KeyValuePair<X,Y> item in collection)"
        converted = re.sub(
            r"\bforeach\s*\(\s*\w+(?:<[^>]+>)?\s+(\w+)\s+in\s+([^\)]+)\s*\)",
            r"for _, \1 in \2 do",
            converted,
        )

        # Convert C# string interpolation $"..." to string.format.
        converted = re.sub(
            r'\$"([^"]*)"',
            lambda m: _convert_interpolated_string(m.group(1)),
            converted,
        )

        # String concatenation with + → .. (only when one side is a string literal)
        converted = re.sub(r'("[^"]*")\s*\+\s*', r'\1 .. ', converted)
        converted = re.sub(r'\s*\+\s*("[^"]*")', r' .. \1', converted)

        # -- API call substitution --
        for unity_api, roblox_api in api_mappings.items():
            if unity_api in converted:
                converted = converted.replace(unity_api, roblox_api)
                matched_patterns += 1
                # Track service imports.
                for svc in SERVICE_IMPORTS:
                    if svc in roblox_api:
                        services_needed.add(svc)

        # -- if/else/elseif conversion --
        # "} else if (cond) {" → "elseif cond then"
        converted = re.sub(
            r"}\s*else\s+if\s*\((.+)\)\s*\{?",
            r"elseif \1 then",
            converted,
        )
        # "} else {" → "else"
        converted = re.sub(r"}\s*else\s*\{?", "else", converted)
        # "if (condition) {" → "if condition then"
        converted = re.sub(
            r"\bif\s*\((.+)\)\s*\{?$",
            r"if \1 then",
            converted,
        )
        # "while (condition) {" → "while condition do"
        converted = re.sub(
            r"\bwhile\s*\((.+)\)\s*\{?$",
            r"while \1 do",
            converted,
        )
        # "do {" → "repeat" (do-while loops)
        converted = re.sub(r"^\s*do\s*\{?\s*$", "repeat", converted)
        # "} while (condition);" → "until not (condition)"
        converted = re.sub(
            r"}\s*while\s*\((.+)\)\s*;?",
            r"until not (\1)",
            converted,
        )

        # -- Closing braces -> end --
        if converted.strip() == "}":
            converted = converted.replace("}", "end")
            matched_patterns += 1

        # -- Opening braces -- (remove standalone {)
        if converted.strip() == "{":
            continue

        # Remove trailing braces on non-standalone lines
        converted = re.sub(r"\s*\{\s*$", "", converted)

        # -- Semicolons --
        converted = converted.rstrip(";").rstrip()

        # Skip empty lines that resulted from stripping
        if not converted.strip():
            output_lines.append("")
            continue

        output_lines.append(converted)

    # Prepend service imports.
    header_lines: list[str] = []
    if services_needed:
        header_lines.append("-- Services")
        for svc in sorted(services_needed):
            if svc in SERVICE_IMPORTS:
                header_lines.append(SERVICE_IMPORTS[svc])
        header_lines.append("")

    # Inject utility functions that are used in the output
    joined_output = "\n".join(output_lines)
    utils_needed: list[str] = []
    for func_name in UTILITY_FUNCTIONS:
        if func_name + "(" in joined_output:
            utils_needed.append(func_name)
    # Dependency resolution: mathDeltaAngle and mathLerpAngle depend on mathRepeat
    if ("mathDeltaAngle" in utils_needed or "mathLerpAngle" in utils_needed) and "mathRepeat" not in utils_needed:
        utils_needed.insert(0, "mathRepeat")
    if "mathLerpAngle" in utils_needed and "mathDeltaAngle" not in utils_needed:
        utils_needed.insert(0 if "mathRepeat" not in utils_needed else 1, "mathDeltaAngle")
    if utils_needed:
        # Group utilities by category for readability
        math_utils = [u for u in utils_needed if u.startswith("math")]
        linq_utils = [u for u in utils_needed if u.startswith("linq")]
        vec3_utils = [u for u in utils_needed if u.startswith("vec3")]
        other_utils = [u for u in utils_needed if u not in math_utils + linq_utils + vec3_utils]
        for label, group in [
            ("-- Math utility functions", math_utils),
            ("-- LINQ utility functions", linq_utils),
            ("-- Vector3 utility functions", vec3_utils),
            ("-- Utility functions", other_utils),
        ]:
            if group:
                header_lines.append(label)
                for func_name in group:
                    if func_name in UTILITY_FUNCTIONS:
                        header_lines.append(UTILITY_FUNCTIONS[func_name])
                        header_lines.append("")

    luau_source = "\n".join(header_lines + output_lines)

    # Safety pass: comment out any lines that still contain C# syntax
    # that would cause Luau parse errors
    luau_source = _sanitize_luau(luau_source)

    # Compute confidence based on how many patterns matched.
    if total_code_lines > 0:
        confidence = min(1.0, matched_patterns / max(total_code_lines * 0.5, 1))
    else:
        confidence = 0.5

    # Cap confidence -- rule-based never claims perfect conversion.
    confidence = min(confidence, 0.85)

    return luau_source, confidence, warnings


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
- `Camera.CameraType` must be `Enum.CameraType.Scriptable` before script-controlled camera works.

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

    # Check cache first.
    cache_key = _cache_key(csharp_source + class_name + script_type + project_context, model)
    cached = _load_cache(cache_key)
    if cached is not None:
        log.debug("AI transpilation cache hit for %s", cache_key[:12])
        return cached["luau"], cached["confidence"], cached.get("warnings", [])

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
    import subprocess
    import shutil

    warnings: list[str] = []

    # Check cache first (include class_name and script_type in key).
    cache_key = _cache_key(csharp_source + class_name + script_type + project_context, "claude-cli-v3")
    cached = _load_cache(cache_key)
    if cached is not None:
        log.debug("Claude CLI cache hit for %s", cache_key[:12])
        return cached["luau"], cached["confidence"], cached.get("warnings", [])

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

    # Score confidence.
    confidence = 0.75
    if "local " in luau_source and "end" in luau_source:
        confidence = 0.85
    if "game:GetService" in luau_source:
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

    # No MonoBehaviour base -> ModuleScript.
    if "monobehaviour" not in source_lower and "networkbehaviour" not in source_lower:
        if "class " in csharp_source:
            return "ModuleScript"

    # Default to server Script.
    return "Script"


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
