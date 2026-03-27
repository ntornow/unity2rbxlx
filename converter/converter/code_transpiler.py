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
) -> TranspilationResult:
    """Transpile a list of C# scripts to Luau.

    Args:
        unity_project_path: Root of the Unity project (for relative path resolution).
        script_infos: List of ScriptInfo objects from the script analyzer.
        use_ai: Whether to attempt AI transpilation for low-confidence scripts.
        api_key: Anthropic API key (required if use_ai is True).

    Returns:
        TranspilationResult with all transpiled scripts and summary counts.
    """
    result = TranspilationResult()

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

        # Determine script type from analyzer.
        script_type = _classify_script_type(csharp_source, info)

        luau = ""
        confidence = 0.0
        warnings: list[str] = []
        strategy = "rule_based"

        # Try AI first if enabled (produces much better Luau)
        if use_ai:
            backend = _find_transpiler() if not api_key else "anthropic_api"
            try:
                if backend == "claude_cli":
                    luau, confidence, warnings = _claude_cli_transpile(
                        csharp_source, class_name=info.class_name, script_type=script_type,
                    )
                    strategy = "ai"
                    result.total_ai += 1
                    log.info("  %s: transpiled via Claude CLI (confidence %.2f)",
                             script_path.name, confidence)
                elif backend == "anthropic_api" and api_key:
                    luau, confidence, warnings = _ai_transpile(
                        csharp_source, api_key, ANTHROPIC_MODEL,
                    )
                    strategy = "ai"
                    result.total_ai += 1
                    log.info("  %s: transpiled via Anthropic API (confidence %.2f)",
                             script_path.name, confidence)
            except Exception as exc:
                log.warning("AI transpilation failed for %s: %s, falling back to rule-based",
                            script_path.name, exc)
                warnings.append(f"AI transpilation failed: {exc}")
                luau = ""
                confidence = 0.0

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

        output_filename = script_path.stem + ".luau"

        ts = TranspiledScript(
            source_path=str(script_path),
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
# Multi-line construct preprocessing
# ---------------------------------------------------------------------------

def _preprocess_multiline_constructs(source: str) -> str:
    """Pre-process multi-line C# constructs before line-by-line transpilation.

    Handles: multi-line enums, property get/set bodies, out parameters,
    yield return, null-conditional chains, is-type patterns.
    """
    source = _preprocess_multiline_enums(source)
    source = _preprocess_property_bodies(source)
    source = _preprocess_out_params(source)
    source = _preprocess_yield_return(source)
    source = _preprocess_null_conditional(source)
    source = _preprocess_is_type_check(source)
    return source


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
    # Simple property access: obj?.Property (not followed by ( )
    # Convert to: (obj and obj.Property or nil)
    source = re.sub(
        r'(\w+)\?\.([\w]+)(?!\s*\()',
        r'(\1 and \1.\2 or nil)',
        source,
    )
    # Method call: obj?.Method(args) → if obj then obj.Method(args) end
    # This is harder in-line; convert to a safe-call pattern
    source = re.sub(
        r'(\w+)\?\.([\w]+)\(([^)]*)\)',
        r'(if \1 then \1.\2(\3) else nil)',
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

        # -- Remove using statements --
        if stripped.startswith("using "):
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
            r"^\s*(?:public|private|protected|internal)?\s*(?:static\s+)?(?:void|bool|int|float|double|string|IEnumerator|[\w<>\[\]]+)\s+(\w+)\s*\(([^)]*)\)\s*\{?\s*$",
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
        m_for = re.match(
            r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*(\d+)\s*;\s*\2\s*<\s*(\w+(?:\.\w+)?)\s*;\s*\2\+\+\s*\)",
            converted,
        )
        if m_for:
            converted = f"{m_for.group(1)}for {m_for.group(2)} = {m_for.group(3)}, {m_for.group(4)}-1 do"
        if not m_for:
            m_for_le = re.match(
                r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*(\d+)\s*;\s*\2\s*<=\s*(\w+(?:\.\w+)?)\s*;\s*\2\+\+\s*\)",
                converted,
            )
            if m_for_le:
                converted = f"{m_for_le.group(1)}for {m_for_le.group(2)} = {m_for_le.group(3)}, {m_for_le.group(4)} do"
                m_for = m_for_le
        if not m_for:
            m_for_dec = re.match(
                r"(\s*)for\s*\(\s*(?:int|var)?\s*(\w+)\s*=\s*(\w+(?:\.\w+)?)\s*;\s*\2\s*>=\s*(\d+)\s*;\s*\2--\s*\)",
                converted,
            )
            if m_for_dec:
                converted = f"{m_for_dec.group(1)}for {m_for_dec.group(2)} = {m_for_dec.group(3)}, {m_for_dec.group(4)}, -1 do"

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

        # Null-coalescing: expr ?? fallback → (if expr ~= nil then expr else fallback)
        converted = re.sub(
            r'(\w+)\s*\?\?\s*([^;,\)]+)',
            r'(if \1 ~= nil then \1 else \2)',
            converted,
        )

        # Lambda expressions: x => expr → function(x) return expr end
        # Simple single-param: x => x.Name → function(x) return x.Name end
        converted = re.sub(
            r'\b(\w+)\s*=>\s*([^,\)]+?)(?=\s*[,\)])',
            r'function(\1) return \2 end',
            converted,
        )

        # C# cast: (Type)expr -> expr
        converted = re.sub(r"\((?:int|float|double|bool|string)\)\s*", "", converted)
        # as Type -> (remove)
        converted = re.sub(r"\s+as\s+\w+", "", converted)

        # Ternary: a ? b : c -> if a then b else c (simple cases)
        converted = re.sub(
            r"(\w+)\s*\?\s*([^:]+):\s*(.+?)(?=\s*$|\s*;)",
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

    # Inject utility functions for Mathf helpers that are used in the output
    joined_output = "\n".join(output_lines)
    utils_needed: list[str] = []
    for func_name in UTILITY_FUNCTIONS:
        if func_name + "(" in joined_output:
            utils_needed.append(func_name)
    # mathDeltaAngle and mathLerpAngle depend on mathRepeat
    if ("mathDeltaAngle" in utils_needed or "mathLerpAngle" in utils_needed) and "mathRepeat" not in utils_needed:
        utils_needed.insert(0, "mathRepeat")
    if "mathLerpAngle" in utils_needed and "mathDeltaAngle" not in utils_needed:
        utils_needed.insert(0 if "mathRepeat" not in utils_needed else 1, "mathDeltaAngle")
    if utils_needed:
        header_lines.append("-- Math utility functions")
        for func_name in utils_needed:
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
        # Stray 'end' after variable declaration (from C# property getters)
        elif s == "end" and code_lines < 15:
            # Early 'end' is suspicious
            error_lines += 1
        # C# if without 'then'
        elif re.match(r"if\s*\(.+\)\s*$", s) and "then" not in s:
            error_lines += 1
        # Incomplete statements with C# patterns
        elif re.search(r"\.\w+\(.*\)\.\w+\(", s) and ":" not in s:
            error_lines += 1

    # If more than 20% of code lines have issues, it's broken
    if code_lines > 0 and error_lines / code_lines > 0.15:
        return True
    # Or if there are any hard errors
    if error_lines > 2:
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
You are an expert Unity C# to Roblox Luau transpiler. Convert the given script to idiomatic, RUNNABLE Roblox Luau. The output MUST parse and execute without errors.

Architecture:
- Server scripts live in ServerScriptService. Client scripts (using Input, Camera, LocalPlayer) are LocalScripts in StarterPlayerScripts.
- script.Parent is the service container, NOT a part. Do NOT use script.Parent to get a part.
- Find target parts using workspace:FindFirstChild("PartName", true) or workspace:GetDescendants().
- CRITICAL: If a script manages MULTIPLE objects of the same type (e.g. all Pickups, all Mines, all Turrets, all SpawnPoints, all Doors), you MUST loop over workspace:GetDescendants() and call a setup function for EACH matching instance. Do NOT use FindFirstChild for these - it only returns one. Use this pattern:
  local function setupThing(thing) ... end
  for _, obj in workspace:GetDescendants() do
    if obj.Name == "ThingName" and obj:IsA("BasePart") then setupThing(obj) end
  end
  workspace.DescendantAdded:Connect(function(obj)
    if obj.Name == "ThingName" and obj:IsA("BasePart") then task.defer(setupThing, obj) end
  end)
- IMPORTANT: The converter sets gameplay attributes on prefab instances. Use BOTH attribute checks AND name pattern matching for robust detection:
  - Pickups: check obj:GetAttribute("IsPickup") == true OR string.find(string.lower(obj.Name), "pickup"). The attribute "ItemType" contains the pickup type (e.g. "Rifle", "Health", "Ammo").
  - SpawnPoints: check obj:GetAttribute("IsSpawnPoint") == true OR string.find(string.lower(obj.Name), "spawnpoint") OR obj.Name == "SpawnPoint".
  - Mines: check obj:GetAttribute("IsMine") == true OR string.find(string.lower(obj.Name), "mine").
  - Pickup Models contain a transparent "PickupTouchDetector" Part with IsPickup=true and ItemType attribute.
  - When collecting a pickup, destroy the parent Model (not just the touch detector): if part.Parent:IsA("Model") then part.Parent:Destroy()
  - Use obj:GetAttribute("ItemType") to get the pickup type (e.g., "Rifle", "Health", "Ammo").
  Example pickup detection pattern:
  local function isPickup(obj)
      if obj:GetAttribute("IsPickup") then return true end
      if obj:IsA("BasePart") and string.find(string.lower(obj.Name), "pickup") then return true end
      return false
  end
  -- When handling pickup collection:
  local itemType = part:GetAttribute("ItemType") or part.Parent.Name:gsub("Pickup", "")
  if part.Parent and part.Parent:IsA("Model") then part.Parent:Destroy() else part:Destroy() end

Cross-Script Communication (CRITICAL - all scripts MUST follow these conventions):
- Player state is stored as Attributes on the CHARACTER MODEL (not the Player object):
  character:SetAttribute("HasKey", true/false)
  character:SetAttribute("HasWeapon", true/false)
  character:SetAttribute("HasItem_Battery", true/false)
  character:SetAttribute("HasItem_SmallBattery", true/false)
  character:SetAttribute("HasItem_MediumBattery", true/false)
  character:SetAttribute("HasItem_GasCan", true/false)
- Scripts that CHECK player items must use character:GetAttribute("HasItem_" .. itemName)
- Scripts that CHECK player key must use character:GetAttribute("HasKey")
- RemoteEvents for HUD updates go in ReplicatedStorage with EXACT names:
  "HealthUpdate" (server->client: fires with curHealth number)
  "AmmoUpdate" (server->client: fires with curAmmo number)
  "ItemUpdate" (server->client: fires with itemName string)
  "PlayerShoot" (client->server: fires with origin Vector3, direction Vector3)
  "PlayerGetItem" (client->server: fires with itemName string)
- Player health/ammo are managed SERVER-SIDE. The server Player script tracks per-player state.
- The client sends input events (shoot, pickup) via RemoteEvents. The server validates and applies changes.
- Turret/Mine/Pickup damage to player: use a BindableEvent "PlayerTakeDamage" in ReplicatedStorage.

Mouse/Cursor:
- Do NOT set MouseBehavior or MouseIconEnabled in this script at all. Mouse locking is handled by a separate menu/UI system. Simply omit any Cursor.lockState or Cursor.visible conversion entirely — do not generate any mouse lock code.

API Conversions:
- Debug.Log -> print, Debug.LogWarning/Error -> warn
- MonoBehaviour lifecycle: Awake/Start -> top-level code, Update -> RunService.Heartbeat:Connect
- Physics.Raycast -> workspace:Raycast(origin, direction * distance, RaycastParams.new())
- Instantiate(prefab) -> prefab:Clone(), Destroy(obj) -> obj:Destroy()
- GetComponent<T>() -> :FindFirstChildOfClass("T")
- transform.position -> part.Position, transform.rotation -> part.CFrame
- Input.GetKey -> UserInputService:IsKeyDown, Camera.main -> workspace.CurrentCamera
- Vector3/Vector2 -> same in Luau, Quaternion -> CFrame, Color -> Color3
- Time.deltaTime -> dt parameter from Heartbeat callback
- StartCoroutine -> task.spawn, yield return new WaitForSeconds(n) -> task.wait(n)
- foreach (Type x in collection) -> for _, x in collection do
- null -> nil, != -> ~=, && -> and, || -> or, ! -> not
- Mathf.X -> math.x (lowercase)
- OnCollisionEnter/OnTriggerEnter -> part.Touched:Connect(function(otherPart) ... end)
- SceneManager.LoadScene -> TeleportService:TeleportAsync(game.PlaceId, players)

Additional API patterns:
- UnityEvent.AddListener(callback) -> event.Event:Connect(callback)
- UnityEvent.Invoke() -> event:Fire()
- SendMessage("MethodName") -> part:SetAttribute("MethodName", true) (use attributes for inter-component comms)
- PlayableDirector.Play() -> trigger animation sequence via BindableEvent
- Input.GetButton/GetButtonDown -> UserInputService:IsKeyDown / InputBegan
- gameObject.SetActive(false) -> set Transparency=1, CanCollide=false
- GetComponent<Rigidbody>().velocity -> part.AssemblyLinearVelocity
- Rigidbody.AddForce -> part:ApplyImpulse(force) or part.AssemblyLinearVelocity += force
- NavMeshAgent.SetDestination -> use PathfindingService:CreatePath() (require NavAgent module)

Critical Luau syntax rules:
- NO braces {} for blocks, use then/do/end
- NO semicolons
- NO type annotations - use 'local x = 5'
- NO access modifiers
- NO compound assignment operators (+=, -=, *=) - use x = x + 1
- Functions: 'local function name(args)' not 'void name(args)'
- if/elseif/else/end
- Comments: -- not //
- String concat: .. not +
- Not equal: ~= not !=
- Array length: #array not array.Length
- Ternary: if cond then a else b (inline, no ? :)
- for i = 0, n-1 do (not for(int i=0; i<n; i++))

Output ONLY valid Luau code. No markdown fences. No explanations.
"""


def _ai_transpile(
    csharp_source: str,
    api_key: str,
    model: str,
) -> tuple[str, float, list[str]]:
    """Transpile C# source to Luau using the Claude API.

    Args:
        csharp_source: The C# source code.
        api_key: Anthropic API key.
        model: Model identifier.

    Returns:
        Tuple of (luau_source, confidence, warnings).

    Raises:
        RuntimeError: If the API call fails after retries.
    """
    warnings: list[str] = []

    # Check cache first.
    cache_key = _cache_key(csharp_source, model)
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

    try:
        response = client.messages.create(
            model=model,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=_AI_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Convert this Unity C# script to Roblox Luau:\n\n```csharp\n{csharp_source}\n```",
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
    cache_key = _cache_key(csharp_source + class_name + script_type, "claude-cli-v2")
    cached = _load_cache(cache_key)
    if cached is not None:
        log.debug("Claude CLI cache hit for %s", cache_key[:12])
        return cached["luau"], cached["confidence"], cached.get("warnings", [])

    # Check claude is available
    claude_path = shutil.which("claude")
    if not claude_path:
        raise RuntimeError("'claude' CLI not found on PATH")

    # Add context about this specific script
    context = ""
    if class_name:
        context += f"\nThis script is class '{class_name}'."
    if script_type == "LocalScript":
        context += "\nThis must be a LocalScript (runs on the client). Do NOT use server-only APIs like RemoteEvent:FireClient or game:GetService('ServerStorage'). Use only client APIs."
    else:
        context += "\nThis is a server Script in ServerScriptService."
    context += f"\nTo find game objects this script should control, search workspace:GetDescendants() for parts/models with relevant names."

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
