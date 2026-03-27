"""
luau_validator.py -- Post-transpilation validation and fixup for Luau scripts.

Catches common AI transpilation mistakes that would cause runtime errors:
1. Leading prose/markdown that leaked through code fence stripping
2. Setting plugin-only or read-only properties (StreamingEnabled, Source, etc.)
3. Runtime Instance.new("LocalScript"/"Script") with .Source assignment
4. EnumItem comparison without .Value
5. Other known Roblox API misuse patterns
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def validate_and_fix(name: str, source: str) -> tuple[str, list[str]]:
    """Validate and fix common Luau issues in a transpiled script.

    Args:
        name: Script name (for logging).
        source: Luau source code.

    Returns:
        Tuple of (fixed_source, list_of_fixes_applied).
    """
    fixes: list[str] = []

    source = _strip_leading_prose(name, source, fixes)
    source = _fix_runtime_script_creation(name, source, fixes)
    source = _fix_plugin_only_properties(name, source, fixes)
    source = _fix_enum_comparisons(name, source, fixes)
    source = _fix_csharp_remnants(name, source, fixes)
    source = _fix_common_api_mistakes(name, source, fixes)
    source = _fix_startup_race_conditions(name, source, fixes)
    source = _inject_utility_functions(name, source, fixes)

    return source, fixes


def _strip_leading_prose(name: str, source: str, fixes: list[str]) -> str:
    """Remove any non-Luau prose at the start of the script.

    AI sometimes outputs explanatory text before the actual code.
    Valid Luau starts with: --, local, return, if, for, while, do, function,
    print, warn, error, task, game, workspace, script, require, or a bare identifier.
    """
    lines = source.split("\n")
    first_code_line = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        # Check if this line looks like valid Luau (not English prose)
        is_luau = (
            stripped.startswith("--") or
            stripped.startswith("local ") or
            stripped.startswith("return ") or
            stripped.startswith("if ") or
            stripped.startswith("for ") or
            stripped.startswith("while ") or
            stripped.startswith("do") or
            stripped.startswith("function ") or
            stripped.startswith("print(") or
            stripped.startswith("warn(") or
            stripped.startswith("error(") or
            stripped.startswith("task.") or
            stripped.startswith("game:") or
            stripped.startswith("workspace") or
            stripped.startswith("script") or
            stripped.startswith("require(")
        )
        if is_luau:
            first_code_line = i
            break
        # If it contains English prose markers, skip it
        if (re.search(r"['\u2018\u2019]s\b", stripped) or  # possessives
            re.search(r'\b(Here|Now|This|The|I |Note|Below|Above)\b', stripped) or
            stripped.endswith(":") or
            stripped.endswith(".") and not stripped.endswith("end.") or
            "converted" in stripped.lower() or
            "script" in stripped.lower() and "=" not in stripped):
            continue
        # Otherwise, assume it's code (identifier = ..., function call, etc.)
        first_code_line = i
        break

    if first_code_line > 0:
        removed = lines[:first_code_line]
        source = "\n".join(lines[first_code_line:])
        fixes.append(f"Stripped {first_code_line} lines of leading prose")
        log.info("  [%s] Stripped %d lines of leading prose", name, first_code_line)

    return source


def _fix_plugin_only_properties(name: str, source: str, fixes: list[str]) -> str:
    """Comment out lines that set plugin-only or read-only properties."""
    # Properties that can't be set from scripts at runtime
    plugin_only = [
        r'workspace\.StreamingEnabled\s*=',
        r'workspace\.StreamingMinRadius\s*=',
        r'workspace\.StreamingTargetRadius\s*=',
        r'\.Source\s*=\s*\[',    # Setting .Source on a script instance
        r'\.Source\s*=\s*"',     # Setting .Source on a script instance
        r'\.Source\s*=\s*\[\[',  # Setting .Source with multi-line string
    ]

    lines = source.split("\n")
    result = []
    fixed_count = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("--"):
            result.append(line)
            continue

        commented = False
        for pattern in plugin_only:
            if re.search(pattern, stripped):
                indent = len(line) - len(line.lstrip())
                result.append(f"{' ' * indent}-- [DISABLED: plugin-only property] {stripped}")
                commented = True
                fixed_count += 1
                break

        if not commented:
            result.append(line)

    if fixed_count:
        source = "\n".join(result)
        fixes.append(f"Commented out {fixed_count} plugin-only property assignments")
        log.info("  [%s] Commented out %d plugin-only property assignments", name, fixed_count)

    return source


def _fix_runtime_script_creation(name: str, source: str, fixes: list[str]) -> str:
    """Replace runtime Instance.new('LocalScript'/'Script') patterns.

    In Roblox, you can't create Script/LocalScript at runtime and set .Source.
    Comments out the entire block including multi-line string contents.
    """
    if not re.search(r'Instance\.new\(\s*["\'](?:Module|Local)?Script["\']\s*\)', source):
        return source
    if not re.search(r'\.Source\s*=', source):
        return source

    lines = source.split("\n")
    result = []
    in_script_creation = False
    in_multiline_string = False
    multiline_closer = None
    script_var = None

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # If inside a multi-line string, comment everything until the closer
        if in_multiline_string:
            result.append(f"{' ' * indent}-- {stripped}")
            if multiline_closer and multiline_closer in stripped:
                in_multiline_string = False
                multiline_closer = None
            continue

        # Detect start of script creation
        m = re.search(r'local\s+(\w+)\s*=\s*Instance\.new\(\s*["\'](?:Module|Local)?Script["\']\s*\)', stripped)
        if m:
            script_var = m.group(1)
            in_script_creation = True
            result.append(f"{' ' * indent}-- [DISABLED: cannot create scripts at runtime in Roblox]")
            result.append(f"{' ' * indent}-- {stripped}")
            continue

        # If tracking a script variable, comment out its property sets
        if in_script_creation and script_var and re.match(rf'^[\s]*{re.escape(script_var)}[\.\[]', stripped):
            result.append(f"{' ' * indent}-- {stripped}")

            # Check for multi-line string opener: [==[ or [=[ or [[
            ml_match = re.search(r'\[=*\[', stripped)
            if ml_match:
                opener = ml_match.group(0)
                # Compute the matching closer
                eq_count = opener.count("=")
                closer = "]" + "=" * eq_count + "]"
                if closer not in stripped:  # String doesn't close on same line
                    in_multiline_string = True
                    multiline_closer = closer

            # Check if this is the .Parent assignment (end of creation block)
            if f"{script_var}.Parent" in stripped:
                in_script_creation = False
                script_var = None
            continue

        result.append(line)

    source = "\n".join(result)
    fixes.append("Disabled runtime script creation (not supported in Roblox)")
    log.info("  [%s] Disabled runtime script creation", name)
    return source


def _fix_enum_comparisons(name: str, source: str, fixes: list[str]) -> str:
    """Fix comparisons of EnumItems with numbers.

    SavedQualityLevel returns an EnumItem, not a number.
    """
    # Pattern: variable >= NUMBER or variable <= NUMBER where variable is likely an EnumItem
    enum_properties = [
        "SavedQualityLevel",
        "QualityLevel",
    ]

    for prop in enum_properties:
        if prop in source:
            # Find the variable that holds this property
            m = re.search(rf'local\s+(\w+)\s*=.*{prop}', source)
            if m:
                var = m.group(1)
                # Replace comparisons: var >= N -> var.Value >= N, var <= N -> var.Value <= N
                source = re.sub(
                    rf'\b{re.escape(var)}\s*(>=|<=|>|<|==|~=)\s*(\d+)',
                    rf'{var}.Value \1 \2',
                    source,
                )
                fixes.append(f"Fixed EnumItem comparison for {prop}")
                log.info("  [%s] Fixed EnumItem comparison for %s", name, prop)

    return source


def _fix_csharp_remnants(name: str, source: str, fixes: list[str]) -> str:
    """Fix C# syntax remnants that sometimes survive AI transpilation."""
    original = source

    # Fix 'this.' prefix (C# self-reference, not valid in Luau)
    if re.search(r'\bthis\.', source):
        source = re.sub(r'\bthis\.(\w+)', r'script.Parent.\1', source)
        fixes.append("Replaced 'this.' with 'script.Parent.'")
        log.info("  [%s] Replaced 'this.' with 'script.Parent.'", name)

    # Fix 'null' keyword (C# null, Luau uses 'nil')
    if re.search(r'\bnull\b', source):
        source = re.sub(r'\bnull\b', 'nil', source)
        fixes.append("Replaced 'null' with 'nil'")
        log.info("  [%s] Replaced 'null' with 'nil'", name)

    # Fix 'true'/'false' with wrong case (C# True/False)
    if re.search(r'\bTrue\b', source):
        source = re.sub(r'\bTrue\b', 'true', source)
        fixes.append("Fixed 'True' → 'true'")
    if re.search(r'\bFalse\b', source):
        source = re.sub(r'\bFalse\b', 'false', source)
        fixes.append("Fixed 'False' → 'false'")

    # Fix 'void' return type annotation (C# remnant)
    source = re.sub(r'\bvoid\s+function\b', 'function', source)

    # Fix C# string interpolation $"..." that leaked through
    if re.search(r'\$"[^"]*\{', source):
        # Convert $"text {var}" or $"text {var:F2}" to string.format
        def _fix_interpolation(m):
            s = m.group(0)[2:-1]  # strip $" and "
            parts = re.split(r'\{([^}]+)\}', s)
            fmt_str = ""
            args = []
            for i, p in enumerate(parts):
                if i % 2 == 0:
                    fmt_str += p
                else:
                    # Handle format specifiers: {var:F2} → var with %.2f
                    if ":" in p:
                        var, spec = p.split(":", 1)
                        fmt_str += _format_spec_to_lua(spec)
                        args.append(var.strip())
                    else:
                        fmt_str += "%s"
                        args.append(f"tostring({p.strip()})")
            if args:
                return f'string.format("{fmt_str}", {", ".join(args)})'
            return f'"{fmt_str}"'
        source = re.sub(r'\$"[^"]*"', _fix_interpolation, source)
        fixes.append("Converted C# string interpolation to string.format")

    # Fix 'typeof(X)' (C# type check, no Luau equivalent — use typeof() Luau builtin or string)
    if re.search(r'\btypeof\s*\(\s*\w+\s*\)', source):
        # typeof(SomeType) in C# → "SomeType" in Luau (as a string for comparison)
        # But Luau has typeof() for runtime type checking, so context matters
        # Common pattern: typeof(X) == typeof(Y) → just compare types as strings
        source = re.sub(
            r'\btypeof\s*\(\s*(\w+)\s*\)\s*==\s*typeof\s*\(\s*(\w+)\s*\)',
            r'typeof(\1) == typeof(\2)',
            source,
        )
        # Standalone typeof(Type) where Type is a C# class → "Type" string
        source = re.sub(
            r'\btypeof\s*\(\s*([A-Z]\w+)\s*\)',
            r'"\1"',
            source,
        )
        fixes.append("Fixed 'typeof()' C# type expressions")

    # Fix '.Length' on strings/arrays (C# → Luau #)
    if '.Length' in source:
        # string.Length or array.Length → #string or #array
        source = re.sub(r'(\w+)\.Length\b', r'#\1', source)
        fixes.append("Fixed '.Length' → '#' operator")

    # Fix '.Count' on collections (C# → Luau #)
    if re.search(r'\w+\.Count\b', source):
        source = re.sub(r'(\w+)\.Count\b', r'#\1', source)
        fixes.append("Fixed '.Count' → '#' operator")

    # Fix '.Contains()' (C# → table.find or string.find)
    if '.Contains(' in source:
        source = re.sub(
            r'(\w+)\.Contains\(([^)]+)\)',
            r'table.find(\1, \2)',
            source,
        )
        fixes.append("Fixed '.Contains()' → 'table.find()'")

    # Fix string.format with C# positional placeholders: {0}, {1} → %s
    if 'string.format(' in source and re.search(r'\{\d+\}', source):
        def _fix_csharp_format_string(m):
            full = m.group(0)
            fmt = m.group(1)
            # Replace {0}, {1}, {0:F2} etc. with %s
            fixed_fmt = re.sub(r'\{(\d+)(?::[^}]+)?\}', '%s', fmt)
            return full.replace(f'"{fmt}"', f'"{fixed_fmt}"')
        source = re.sub(
            r'string\.format\(\s*"([^"]*\{\d+\}[^"]*)"',
            _fix_csharp_format_string,
            source,
        )
        fixes.append("Fixed C# string.Format positional placeholders to %s")

    # Fix '.ToString()' (C# → tostring())
    if '.ToString(' in source:
        # ToString with format specifier: x.ToString("F2") → string.format("%.2f", x)
        def _fix_tostring_format(m):
            var = m.group(1)
            spec = m.group(2)
            fmt = _format_spec_to_lua(spec)
            return f'string.format("{fmt}", {var})'
        source = re.sub(
            r'(\w+)\.ToString\(\s*"([^"]+)"\s*\)',
            _fix_tostring_format,
            source,
        )
        # Plain ToString(): x.ToString() → tostring(x)
        source = re.sub(r'(\w+)\.ToString\(\)', r'tostring(\1)', source)
        fixes.append("Fixed '.ToString()' → 'tostring()'")

    # Fix '.ContainsKey(key)' → '[key] ~= nil'
    if '.ContainsKey(' in source:
        source = re.sub(
            r'(\w+)\.ContainsKey\(([^)]+)\)',
            r'\1[\2] ~= nil',
            source,
        )
        fixes.append("Fixed '.ContainsKey()' → '[] ~= nil'")

    # Fix '.TryGetValue(key, out val)' → 'val = tbl[key]'
    if '.TryGetValue(' in source:
        source = re.sub(
            r'(\w+)\.TryGetValue\(([^,]+),\s*(?:out\s+)?(\w+)\)',
            r'\3 = \1[\2]',
            source,
        )
        fixes.append("Fixed '.TryGetValue()' → direct table access")

    # Fix Queue.Dequeue()/Stack.Pop() broken syntax from API map
    # Pattern: "var = tbl.table.remove(, 1)" → "var = table.remove(tbl, 1)"
    if 'table.remove(, 1)' in source or 'table.remove(, #)' in source:
        # Fix Dequeue: x = queue.table.remove(, 1) → x = table.remove(queue, 1)
        source = re.sub(
            r'(\w+)\.table\.remove\(\s*,\s*1\s*\)',
            r'table.remove(\1, 1)',
            source,
        )
        # Fix Pop: x = stack.table.remove(, #) → x = table.remove(stack, #stack)
        source = re.sub(
            r'(\w+)\.table\.remove\(\s*,\s*#\s*\)',
            r'table.remove(\1, #\1)',
            source,
        )
        fixes.append("Fixed Queue.Dequeue/Stack.Pop syntax")

    # Fix Peek: tbl.[1] or tbl[1] patterns from API map
    if '.[1]' in source:
        source = re.sub(r'(\w+)\.\[1\]', r'\1[1]', source)
        fixes.append("Fixed Queue.Peek syntax")

    # Fix '?.Invoke(' (C# null-conditional event invoke → :Fire())
    if '?.Invoke(' in source or '.Invoke(' in source:
        source = re.sub(r'(\w+)\?\.Invoke\(', r'if \1 then \1:Fire(', source)
        source = re.sub(r'(\w+)\.Invoke\(([^)]*)\)', r'\1:Fire(\2)', source)
        fixes.append("Fixed event '.Invoke()' → ':Fire()'")

    # Fix event += handler → event:Connect(handler)
    if re.search(r'\w+\s*\+=\s*\w+', source) and not re.search(r'\w+\s*\+=\s*\d', source):
        # Only convert += where both sides are identifiers (event subscription pattern)
        # Skip numeric += (already handled by compound assignment fix)
        source = re.sub(
            r'(\w+(?:\.\w+)*)\s*\+=\s*(\w+)\s*$',
            lambda m: (
                f'{m.group(1)}:Connect({m.group(2)})'
                if any(kw in m.group(1) for kw in ('Changed', 'Event', 'Signal', 'Touched', 'Added', 'Removing', 'Died', 'Activated', 'Clicked'))
                else f'{m.group(1)} = {m.group(1)} + {m.group(2)}'
            ),
            source,
            flags=re.MULTILINE,
        )

    # Fix event -= handler → comment (disconnection needs stored connection)
    if re.search(r'\w+\s*-=\s*\w+\s*$', source, re.MULTILINE):
        source = re.sub(
            r'(\w+(?:\.\w+)*)\s*-=\s*(\w+)\s*$',
            lambda m: (
                f'-- {m.group(1)} -= {m.group(2)} (store connection from :Connect to disconnect)'
                if any(kw in m.group(1) for kw in ('Changed', 'Event', 'Signal', 'Touched', 'Added', 'Removing', 'Died', 'Activated', 'Clicked'))
                else f'{m.group(1)} = {m.group(1)} - {m.group(2)}'
            ),
            source,
            flags=re.MULTILINE,
        )

    # Fix DOTween method calls → TweenService:Create
    if '.DOMove(' in source or '.DORotate(' in source or '.DOScale(' in source or '.DOFade(' in source or '.DOColor(' in source or '.DOLocalMove(' in source:
        # obj.DOMove(target, duration) → TweenService:Create(obj, TweenInfo.new(duration), {Position = target}):Play()
        source = re.sub(
            r'(\w+)\.DOMove\(([^,]+),\s*([^)]+)\)',
            r'TweenService:Create(\1, TweenInfo.new(\3), {Position = \2}):Play()',
            source,
        )
        source = re.sub(
            r'(\w+)\.DOLocalMove\(([^,]+),\s*([^)]+)\)',
            r'TweenService:Create(\1, TweenInfo.new(\3), {Position = \2}):Play()',
            source,
        )
        source = re.sub(
            r'(\w+)\.DORotate\(([^,]+),\s*([^)]+)\)',
            r'TweenService:Create(\1, TweenInfo.new(\3), {CFrame = CFrame.fromEulerAnglesXYZ(math.rad(\2.X), math.rad(\2.Y), math.rad(\2.Z))}):Play()',
            source,
        )
        source = re.sub(
            r'(\w+)\.DOScale\(([^,]+),\s*([^)]+)\)',
            r'TweenService:Create(\1, TweenInfo.new(\3), {Size = \2}):Play()',
            source,
        )
        source = re.sub(
            r'(\w+)\.DOFade\(([^,]+),\s*([^)]+)\)',
            r'TweenService:Create(\1, TweenInfo.new(\3), {Transparency = 1 - \2}):Play()',
            source,
        )
        source = re.sub(
            r'(\w+)\.DOColor\(([^,]+),\s*([^)]+)\)',
            r'TweenService:Create(\1, TweenInfo.new(\3), {Color = \2}):Play()',
            source,
        )
        fixes.append("Converted DOTween calls to TweenService:Create")

    # Fix string.gsub literal patterns: string.gsub(s, ".", repl) → escape Lua pattern chars
    # C# string.Replace does literal replacement, but Luau string.gsub uses patterns.
    # Escape Lua pattern magic characters: ( ) . % + - * ? [ ] ^ $
    if 'string.gsub(' in source:
        _LUA_MAGIC = set('().%+-*?[]^$')

        def _escape_gsub_literal(m):
            full = m.group(0)
            pattern_str = m.group(1)
            if any(c in _LUA_MAGIC for c in pattern_str):
                escaped = ''
                for c in pattern_str:
                    if c in _LUA_MAGIC:
                        escaped += '%' + c
                    else:
                        escaped += c
                return full.replace(f'"{pattern_str}"', f'"{escaped}"')
            return full
        source = re.sub(
            r'string\.gsub\([^,]+,\s*"([^"]+)"',
            _escape_gsub_literal,
            source,
        )

    # Fix SetActive: obj.SetActive(bool) → setActive(obj, bool)
    # This provides proper recursive enable/disable for the hierarchy
    if '.SetActive(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.SetActive\(([^)]+)\)',
            r'setActive(\1, \2)',
            source,
        )
        fixes.append("Fixed SetActive to use recursive setActive()")

    if source != original and not fixes:
        fixes.append("Fixed C# syntax remnants")

    return source


def _fix_common_api_mistakes(name: str, source: str, fixes: list[str]) -> str:
    """Fix common Roblox API mistakes in transpiled scripts."""
    original = source

    # Fix: .gameObject (C# Unity API) → the part itself or .Parent
    if ".gameObject" in source:
        # In Roblox, the equivalent of gameObject is usually the Part itself
        source = re.sub(r'(\w+)\.gameObject\b', r'\1', source)

    # Fix: .transform.position (Unity) → .Position (Roblox)
    if ".transform.position" in source:
        source = re.sub(r'\.transform\.position\b', '.Position', source)
    if ".transform.rotation" in source:
        source = re.sub(r'\.transform\.rotation\b', '.CFrame', source)

    # Fix: Animator.StringToHash("Name") → "Name" (Roblox uses strings, not hashes)
    if "StringToHash" in source:
        source = re.sub(
            r'Animator\.StringToHash\(\s*("(?:[^"\\]|\\.)*")\s*\)',
            r'\1',
            source,
        )

    # Fix: GetComponent<X>() (C# generic) → :FindFirstChildOfClass("X")
    if "GetComponent<" in source:
        source = re.sub(
            r'(\w+):?\.?GetComponent<(\w+)>\(\)',
            r'\1:FindFirstChildOfClass("\2")',
            source,
        )

    # Note: .SetActive() is now handled in _fix_csharp_remnants using recursive setActive()

    # Fix: Destroy() without colon (C# method call syntax)
    if re.search(r'\w+\.Destroy\(\)', source):
        source = re.sub(r'(\w+)\.Destroy\(\)', r'\1:Destroy()', source)

    # Fix: new Vector3(...) → Vector3.new(...)
    if "new Vector3(" in source:
        source = re.sub(r'\bnew\s+Vector3\(', 'Vector3.new(', source)
    if "new CFrame(" in source:
        source = re.sub(r'\bnew\s+CFrame\(', 'CFrame.new(', source)
    if "new Color3(" in source:
        source = re.sub(r'\bnew\s+Color3\(', 'Color3.new(', source)
    if "new Instance(" in source:
        source = re.sub(r'\bnew\s+Instance\(', 'Instance.new(', source)

    # Fix: += / -= / *= / /= operators (not valid in Luau)
    # Handles both simple vars and property access (obj.prop += expr)
    if re.search(r'\+=', source):
        source = re.sub(r'([\w.]+)\s*\+=\s*(.+)', r'\1 = \1 + \2', source)
    if re.search(r'-=', source):
        source = re.sub(r'([\w.]+)\s*-=\s*(.+)', r'\1 = \1 - \2', source)
    if re.search(r'\*=', source):
        source = re.sub(r'([\w.]+)\s*\*=\s*(.+)', r'\1 = \1 * \2', source)
    if re.search(r'/=', source):
        source = re.sub(r'([\w.]+)\s*/=\s*(.+)', r'\1 = \1 / \2', source)

    # Fix: semicolons at end of statements
    if ";" in source:
        # Remove trailing semicolons but not within strings
        lines = source.split("\n")
        result = []
        for line in lines:
            s = line.rstrip()
            if s.endswith(";") and not s.strip().startswith("--"):
                # Don't remove semicolons inside string literals
                quote_count = s.count('"') - s.count('\\"')
                if quote_count % 2 == 0:  # Not inside a string
                    s = s[:-1].rstrip()
            result.append(s)
        source = "\n".join(result)

    # Fix: "not expr == value" → "expr ~= value" (Luau precedence bug)
    # In Luau, `not x == y` parses as `(not x) == y`, not `not (x == y)`.
    # The correct form is `x ~= y`.
    if "not " in source:
        source = re.sub(
            r'\bnot\s+([\w.:]+(?:\([^)]*\))?)\s*==\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\w+)',
            r'\1 ~= \2',
            source,
        )
        # Also fix: "not expr ~= value" → "expr == value"
        source = re.sub(
            r'\bnot\s+([\w.:]+(?:\([^)]*\))?)\s*~=\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|\w+)',
            r'\1 == \2',
            source,
        )

    # Fix: Deprecated BodyMovers → modern equivalents
    deprecated_body_movers = {
        "BodyVelocity": "LinearVelocity",
        "BodyGyro": "AlignOrientation",
        "BodyPosition": "AlignPosition",
        "BodyForce": "VectorForce",
        "BodyThrust": "VectorForce",
        "BodyAngularVelocity": "AngularVelocity",
    }
    for old, new in deprecated_body_movers.items():
        if f'Instance.new("{old}"' in source or f"Instance.new('{old}'" in source:
            source = source.replace(
                f'Instance.new("{old}")',
                f'Instance.new("{new}") -- was {old} (deprecated)',
            )
            source = source.replace(
                f"Instance.new('{old}')",
                f"Instance.new('{new}') -- was {old} (deprecated)",
            )

    # Fix: string.unpack → buffer-based alternative (not available in Roblox Luau)
    if "string.unpack(" in source:
        source = re.sub(
            r'string\.unpack\(',
            '-- [WARNING: string.unpack not available in Roblox] string.unpack(',
            source,
        )
    if "string.pack(" in source:
        source = re.sub(
            r'string\.pack\(',
            '-- [WARNING: string.pack not available in Roblox] string.pack(',
            source,
        )

    if source != original:
        fixes.append("Fixed common API mistakes")
        log.info("  [%s] Fixed common API/syntax mistakes", name)

    return source


def _fix_startup_race_conditions(name: str, source: str, fixes: list[str]) -> str:
    """Add startup delay for scripts that immediately scan workspace.

    Scripts using workspace:GetDescendants() at the top level (not inside
    a function or event handler) may run before the workspace is fully loaded.
    Adding task.wait() ensures parts have loaded before scanning.
    """
    if "GetDescendants()" not in source:
        return source

    # Check if there's already a task.wait before the GetDescendants call
    lines = source.split("\n")
    needs_fix = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if "GetDescendants()" in stripped:
            # Check if this is at the top level (not inside a function body)
            # Simple heuristic: look at indentation level
            indent = len(line) - len(line.lstrip())
            if indent == 0:
                # Top-level GetDescendants — check for task.wait in preceding lines
                preceding = "\n".join(lines[max(0, i - 5):i])
                if "task.wait" not in preceding and "task.defer" not in preceding:
                    needs_fix = True
            break

    if needs_fix:
        # Insert task.wait(1) before the first GetDescendants call
        result_lines = []
        inserted = False
        for line in lines:
            if "GetDescendants()" in line and not inserted:
                result_lines.append("-- Wait for workspace to fully load before scanning")
                result_lines.append("task.wait(1)")
                result_lines.append("")
                inserted = True
            result_lines.append(line)
        source = "\n".join(result_lines)
        fixes.append("Added startup delay before workspace:GetDescendants()")
        log.info("  [%s] Added startup delay before GetDescendants", name)

    return source


def fix_gameplay_patterns(name: str, source: str) -> tuple[str, list[str]]:
    """Fix common gameplay script patterns for converter compatibility.

    Patches:
    1. Pickup detection: use IsPickup attribute + name pattern instead of exact name match
    2. Pickup collection: destroy parent Model, use ItemType attribute
    3. Placeholder sound IDs
    """
    fixes = []
    original = source

    # Fix pickup detection: obj.Name == "Pickup" → attribute/pattern match
    if 'obj.Name == "Pickup"' in source:
        source = source.replace(
            'obj.Name == "Pickup" and obj:IsA("BasePart")',
            'obj:IsA("BasePart") and (obj:GetAttribute("IsPickup") or string.find(string.lower(obj.Name), "pickup"))'
        )
        source = source.replace(
            'obj.Name == "Pickup"',
            '(obj:GetAttribute("IsPickup") or string.find(string.lower(obj.Name), "pickup"))'
        )
        fixes.append("Fixed pickup detection to use attribute/pattern matching")

    # Fix item name extraction for PickupTouchDetector
    if 'local itemName = part.Name' in source and 'IsPickup' not in source:
        source = source.replace(
            'local itemName = part.Name',
            'local itemName = part:GetAttribute("ItemType") or part.Name\n\tif itemName == "PickupTouchDetector" and part.Parent then\n\t\titemName = part.Parent.Name:gsub("Pickup", ""):gsub("pickup", "")\n\t\tif itemName == "" then itemName = "Generic" end\n\tend'
        )
        fixes.append("Fixed item name to use ItemType attribute")

    # Fix pickup destruction to destroy parent Model
    if 'part:Destroy()' in source and 'setupPickup' in source:
        source = source.replace(
            'part:Destroy()',
            'if part.Parent and part.Parent:IsA("Model") then part.Parent:Destroy() else part:Destroy() end'
        )
        fixes.append("Fixed pickup destruction to remove parent Model")

    # Remove placeholder sound IDs
    if 'rbxassetid://1905339338' in source:
        source = source.replace('"rbxassetid://1905339338"', '"rbxassetid://"')
        fixes.append("Removed placeholder sound ID")

    # Fix exact name matching for common prefab instances that get numbered
    # Unity instances: "Turret", "Turret (1)", "Turret (14)" etc.
    # Replace obj.Name == "X" with string.find(obj.Name, "^X") for common patterns
    import re
    for pattern_name in ["Turret", "Mine", "SpawnPoint", "HostilePlane", "Machine"]:
        exact = f'obj.Name == "{pattern_name}"'
        if exact in source:
            source = source.replace(exact, f'string.find(obj.Name, "^{pattern_name}")')
            fixes.append(f"Fixed {pattern_name} detection to match numbered instances")

    # Fix Door trigger detection: "DoorTrigger" may actually be "trigger" or "base"
    if 'obj.Name == "DoorTrigger"' in source:
        source = source.replace(
            'obj.Name == "DoorTrigger"',
            '(obj.Name == "DoorTrigger" or obj.Name == "trigger" or obj.Name == "base")'
        )
        fixes.append("Fixed door trigger detection to match actual part names")

    if source != original:
        return source, fixes
    return source, []


def _format_spec_to_lua(spec: str) -> str:
    """Convert C# format specifier to Lua string.format specifier."""
    spec = spec.strip()
    m = re.match(r'^[Ff](\d*)$', spec)
    if m:
        return f"%.{m.group(1) or '2'}f"
    m = re.match(r'^[Nn](\d*)$', spec)
    if m:
        d = m.group(1) or "2"
        return "%d" if d == "0" else f"%.{d}f"
    m = re.match(r'^[Dd](\d*)$', spec)
    if m:
        w = m.group(1)
        return f"%0{w}d" if w else "%d"
    m = re.match(r'^[Xx](\d*)$', spec)
    if m:
        w = m.group(1)
        return f"%0{w}x" if w else "%x"
    m = re.match(r'^[Pp](\d*)$', spec)
    if m:
        return f"%.{m.group(1) or '0'}f%%"
    if spec.upper().startswith("G"):
        return "%g"
    if spec.upper().startswith("E"):
        return "%e"
    return "%s"


def _inject_utility_functions(name: str, source: str, fixes: list[str]) -> str:
    """Inject utility function definitions if they are used but not defined.

    The transpiler normally handles utility injection, but the validator may
    introduce calls to utility functions (e.g. setActive) after transpilation.
    This pass ensures those functions are defined.
    """
    from converter.api_mappings import UTILITY_FUNCTIONS

    for func_name, func_body in UTILITY_FUNCTIONS.items():
        # Check if the function is called but not defined in the source
        call_pattern = func_name + "("
        def_pattern = f"local function {func_name}("
        if call_pattern in source and def_pattern not in source:
            # Inject at the top of the file, after service requires
            lines = source.split("\n")
            insert_idx = 0
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("local ") and "GetService" in stripped:
                    insert_idx = i + 1
                elif stripped.startswith("--") or stripped == "":
                    if i == insert_idx:
                        insert_idx = i + 1
                elif insert_idx > 0:
                    break
            lines.insert(insert_idx, "")
            lines.insert(insert_idx + 1, func_body)
            lines.insert(insert_idx + 2, "")
            source = "\n".join(lines)
            fixes.append(f"Injected utility function {func_name}")

    return source
