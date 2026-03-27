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


def _split_top_level(s: str) -> list[str]:
    """Split a string by commas, respecting nested parentheses."""
    parts = []
    depth = 0
    current = []
    for c in s:
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
        elif c == ',' and depth == 0:
            parts.append(''.join(current))
            current = []
            continue
        current.append(c)
    parts.append(''.join(current))
    return parts


def _fix_ternary_in_line(line: str) -> str:
    """Fix C# ternary `cond ? true_expr : false_expr` in a single line.

    Scans for `?` that appears outside strings and parentheses,
    then finds the matching `:` to form `(if cond then true else false)`.
    Handles function calls with nested parens as conditions.
    """
    # Find all ? positions (not inside strings)
    in_string = False
    string_char = ''
    paren_depth = 0
    q_positions = []
    for i, c in enumerate(line):
        if in_string:
            if c == string_char and (i == 0 or line[i-1] != '\\'):
                in_string = False
            continue
        if c in ('"', "'"):
            in_string = True
            string_char = c
            continue
        if c == '(':
            paren_depth += 1
        elif c == ')':
            paren_depth -= 1
        elif c == '?' and paren_depth >= 0:
            # Check it's ` ? ` (with spaces, not ?. or ??)
            if i > 0 and i < len(line) - 1 and line[i-1] == ' ' and line[i+1] == ' ':
                q_positions.append(i)

    if not q_positions:
        return line

    # Process the LAST ternary first (to preserve positions)
    for q_pos in reversed(q_positions):
        # Find the condition: everything before ` ? ` back to the start of the expression
        # Walk backwards from q_pos-1 to find where the condition starts
        # The condition ends at q_pos-1 (the space before ?)

        # Find matching ` : ` after ` ? `
        colon_pos = None
        depth = 0
        for j in range(q_pos + 2, len(line)):
            c = line[j]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
                if depth < 0:
                    break  # We've gone past the enclosing parens
            elif c == ':' and depth == 0 and j > 0 and line[j-1] == ' ' and j < len(line) - 1 and line[j+1] == ' ':
                colon_pos = j
                break

        if colon_pos is None:
            continue

        # Find the end of false_expr: next comma or closing paren at same depth, or end of line
        end_pos = len(line)
        depth = 0
        for j in range(colon_pos + 2, len(line)):
            c = line[j]
            if c == '(':
                depth += 1
            elif c == ')':
                if depth == 0:
                    end_pos = j
                    break
                depth -= 1
            elif c == ',' and depth == 0:
                end_pos = j
                break

        # Find the start of the condition: walk backwards from q_pos
        # The condition is the expression before ` ? `
        # Walk back past balanced parens, words, dots, colons
        start_pos = q_pos - 1  # skip the space before ?
        while start_pos > 0 and line[start_pos - 1] == ' ':
            start_pos -= 1  # skip extra spaces
        # Now walk back to find where the condition expression starts
        depth = 0
        cond_start = start_pos
        for j in range(start_pos - 1, -1, -1):
            c = line[j]
            if c == ')':
                depth += 1
            elif c == '(':
                if depth == 0:
                    cond_start = j + 1
                    break
                depth -= 1
            elif c == ',' and depth == 0:
                cond_start = j + 1
                break
            elif c in (' ', '=') and depth == 0:
                cond_start = j + 1
                break
            cond_start = j

        cond = line[cond_start:q_pos - 1].strip()
        true_expr = line[q_pos + 2:colon_pos - 1].strip()
        false_expr = line[colon_pos + 2:end_pos].strip()

        if cond and true_expr and false_expr:
            replacement = f'(if {cond} then {true_expr} else {false_expr})'
            line = line[:cond_start] + replacement + line[end_pos:]

    return line


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
    source = _fix_structural_syntax(name, source, fixes)
    source = _fix_missing_end_keywords(name, source, fixes)
    # Second pass: catch patterns introduced by structural fixes
    if re.search(r'\bthis\.', source):
        source = re.sub(r'\bthis\.(\w+)', r'script.Parent.\1', source)
    # Second pass: :GetChild/:LookAt may appear from property getter expansion
    if ':GetChild(' in source:
        def _fix_get_child2(m):
            obj, idx = m.group(1), m.group(2).strip()
            if idx.isdigit():
                return f'{obj}:GetChildren()[{int(idx) + 1}]'
            return f'{obj}:GetChildren()[{idx} + 1]'
        source = re.sub(r'(\w+):GetChild\(([^)]+)\)', _fix_get_child2, source)
    if ':LookAt(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*):LookAt\(([^)]+)\)',
            r'\1.CFrame = CFrame.lookAt(\1.Position, \2)',
            source,
        )
    # Second pass: CFrame.LookVector/UpVector/RightVector assignments introduced by structural fixes
    if re.search(r'\.CFrame\.(?:LookVector|UpVector|RightVector)\s*=', source):
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.CFrame\.LookVector\s*=\s*([^\n]+)',
            r'\1.CFrame = CFrame.lookAt(\1.Position, \1.Position + \2)',
            source,
        )
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.CFrame\.UpVector\s*=\s*([^\n]+)',
            r'-- UpVector assignment: \1.CFrame = CFrame.lookAt(\1.Position, \1.Position + \2)',
            source,
        )
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.CFrame\.RightVector\s*=\s*([^\n]+)',
            r'-- RightVector: \1.CFrame rotation (manual construction needed)',
            source,
        )
        fixes.append("Fixed read-only CFrame vector assignments (second pass)")
    # Second pass: script.Parent:Dot(a, b) may be introduced by bare receiver fixes
    if 'script.Parent:Dot(' in source:
        source = re.sub(
            r'script\.Parent:Dot\((\.[\w.]+),\s*([^)]+)\)',
            r'script.Parent\1:Dot(\2)',
            source,
        )
        source = re.sub(
            r'script\.Parent:Dot\((\w+(?:\.\w+)*),\s*([^)]+)\)',
            r'\1:Dot(\2)',
            source,
        )
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
    Also comments out variables holding source code assigned via multiline strings
    that are later used in .Source assignments.
    """
    if not re.search(r'Instance\.new\(\s*["\'](?:Module|Local)?Script["\']\s*\)', source):
        return source
    if not re.search(r'\.Source\s*=', source):
        return source

    # First pass: collect variable names used in .Source = varName assignments
    source_var_names: set[str] = set()
    for m in re.finditer(r'\.Source\s*=\s*(\w+)\s*$', source, re.MULTILINE):
        source_var_names.add(m.group(1))

    lines = source.split("\n")
    result: list[str] = []
    in_script_creation = False
    in_multiline_string = False
    in_source_var_string = False  # Track multiline strings for source variables
    multiline_closer: str | None = None
    script_var: str | None = None

    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())

        # If inside a multi-line string, comment everything until the closer
        if in_multiline_string or in_source_var_string:
            result.append(f"{' ' * indent}-- {stripped}")
            if multiline_closer and multiline_closer in stripped:
                in_multiline_string = False
                in_source_var_string = False
                multiline_closer = None
            continue

        # Check for variable definitions that hold source code in multiline strings
        # e.g.: local clientShakeSource = [[
        if source_var_names:
            var_ml_match = re.match(
                r'local\s+(\w+)\s*=\s*(\[=*\[)', stripped
            )
            if var_ml_match and var_ml_match.group(1) in source_var_names:
                opener = var_ml_match.group(2)
                eq_count = opener.count("=")
                closer = "]" + "=" * eq_count + "]"
                result.append(f"{' ' * indent}-- [DISABLED: source code variable for runtime script]")
                result.append(f"{' ' * indent}-- {stripped}")
                if closer not in stripped:
                    in_source_var_string = True
                    multiline_closer = closer
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

    # Strip BOM characters
    source = source.replace('\ufeff', '')

    # Fix incomplete assignments where RHS is a comment from API mapping
    # Pattern: `local x = -- StringToHash: use string name...` → extract string arg or use nil
    # This happens when API_CALL_MAP maps a call to a comment string
    if re.search(r'local\s+\w+\s*=\s*--', source):
        # StringToHash specifically: try to extract the original string argument
        # local m_HashFoo = -- StringToHash: use string name directly as attribute key
        # The variable name often encodes the hash name: m_Hash<Name> → "Name"
        def _fix_hash_assignment(m):
            indent, varname, comment = m.group(1), m.group(2), m.group(3)
            # Try to extract name from variable: m_HashFoo → "Foo", hash_Foo → "Foo"
            hash_m = re.search(r'[Hh]ash_?([A-Z]\w*)', varname)
            if hash_m:
                return f'{indent}local {varname} = "{hash_m.group(1)}" {comment}'
            # Fallback: use variable name as string
            return f'{indent}local {varname} = "{varname}" {comment}'
        source = re.sub(
            r'^(\s*)local\s+(\w+)\s*=\s*(-- StringToHash[^\n]*)$',
            _fix_hash_assignment,
            source,
            flags=re.MULTILINE,
        )
        # Generic: local x = -- any comment → local x = nil -- comment
        source = re.sub(
            r'^(\s*local\s+\w+\s*=\s*)(-- .*)$',
            r'\1nil \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed incomplete 'local x = -- comment' assignments")

    # Fix C# typed field declarations that leaked through transpilation
    # Patterns: `bool canAttack;`, `float m_Speed;`, `Type varName;`, `Type varName`
    # Also handles: `UnityEvent OnDeath, OnDamage, OnHit`
    _CSHARP_FIELD_TYPES = (
        'bool', 'int', 'float', 'double', 'string', 'char', 'byte', 'short',
        'long', 'uint', 'ulong', 'ushort', 'decimal', 'void',
        'Vector3', 'Vector2', 'Quaternion', 'Color', 'Rect',
        'UnityEvent', 'Action', 'Func',
        'AnimatorStateInfo', 'Material', 'Checkpoint',
        'RandomAudioPlayer', 'CameraSettings', 'MeleeWeapon',
        'Damageable', 'MaterialPropertyBlock',
    )
    _type_pattern = '|'.join(re.escape(t) for t in _CSHARP_FIELD_TYPES)
    if re.search(rf'^\s*(?:{_type_pattern})\s+\w', source, re.MULTILINE):
        # Multi-variable: `UnityEvent OnDeath, OnDamage` → `local OnDeath, OnDamage = nil, nil`
        def _fix_typed_field(m):
            indent = m.group(1)
            varlist = m.group(3)
            names = [n.strip().rstrip(';') for n in varlist.split(',') if n.strip()]
            nils = ', '.join(['nil'] * len(names))
            return f'{indent}local {", ".join(names)} = {nils}'
        source = re.sub(
            rf'^(\s*)(?:{_type_pattern})(?:\.\w+)?\s+((\w+(?:\s*,\s*\w+)*))\s*;?\s*(--.*)?$',
            lambda m: _fix_typed_field(m) + (f'  {m.group(4)}' if m.group(4) else ''),
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed C# typed field declarations → local")

    # Fix C# `|=` compound bitwise OR (not valid in Luau)
    # `x |= expr` → `x = x or (expr)` (boolean context) or bit32.bor for numeric
    if '|=' in source:
        source = re.sub(
            r'(\w+)\s*\|=\s*(.+)',
            r'\1 = \1 or (\2)',
            source,
        )
        fixes.append("Fixed |= compound bitwise OR → or")

    # Fix default parameter values in function signatures (not valid in Luau)
    # `local function Foo(defaultValue)` where defaultValue is a literal
    # Pattern: function Name(true), function Name(false), function Name(0), function Name(Type.Value)
    if re.search(r'local function \w+\([^)]*(?:true|false|\d+|[A-Z]\w+\.\w+)\)', source):
        def _fix_default_params(m):
            full = m.group(0)
            func_prefix = full[:full.index('(')]
            params_str = full[full.index('(') + 1:full.rindex(')')]
            fixed_params = []
            for p in params_str.split(','):
                p = p.strip()
                # If the param is a literal (true/false/number/Enum.Value), replace with generic name
                if re.match(r'^(true|false|\d+(\.\d+)?|[A-Z]\w+\.\w+)$', p):
                    fixed_params.append('_defaultParam')
                else:
                    fixed_params.append(p)
            return f'{func_prefix}({", ".join(fixed_params)})'
        source = re.sub(
            r'local function \w+\([^)]*(?:true|false|\d+|[A-Z]\w+\.\w+)[^)]*\)',
            _fix_default_params,
            source,
        )
        fixes.append("Fixed default parameter values in function signatures")

    # Fix `m_:SetAttribute(...)` — missing receiver before colon
    # This comes from Animator.SetBool/SetFloat mapping where the animator variable was mangled
    if 'm_:' in source:
        source = re.sub(r'\bm_:(\w+)', r'script.Parent:\1', source)
        fixes.append("Fixed m_: missing receiver → script.Parent:")

    # Fix mangled table operations: `vartable.clear`, `vartable.insert(...)`, `vartable.remove(...)`
    # Pattern: `inventoryItemstable.clear` should be `table.clear(inventoryItems)`
    if 'table.clear' in source or 'table.insert' in source or 'table.remove' in source:
        # Fix: vartable.clear → table.clear(var)
        source = re.sub(
            r'(\w+)table\.clear\b',
            r'table.clear(\1)',
            source,
        )
        # Fix: vartable.insert(args) → table.insert(var, args)
        source = re.sub(
            r'(\w+)table\.insert\(([^)]*)\)',
            r'table.insert(\1, \2)',
            source,
        )
        # Fix: vartable.remove(args) → table.remove(var, args)
        source = re.sub(
            r'(\w+)table\.remove\(([^)]*)\)',
            r'table.remove(\1, \2)',
            source,
        )
        fixes.append("Fixed mangled table operations (vartable.X → table.X(var))")

    # Fix `.Parent =(nil, true)` and `.Parent =(parent, false)` — broken SetParent conversion
    # Unity SetParent(parent, worldPositionStays) → Roblox .Parent = parent
    if re.search(r'\.Parent\s*=\s*\(', source):
        source = re.sub(
            r'\.Parent\s*=\s*\((\w+(?:\.\w+)*),\s*(?:true|false)\)',
            r'.Parent = \1',
            source,
        )
        # .Parent =(nil, true) → .Parent = nil
        source = re.sub(
            r'\.Parent\s*=\s*\(nil,\s*(?:true|false)\)',
            '.Parent = nil',
            source,
        )
        fixes.append("Fixed .Parent =(val, bool) → .Parent = val")

    # Fix remaining C# cast syntax: (TypeName)expr → expr
    # Only match PascalCase type names to avoid catching Luau parenthesized expressions
    _CAST_TYPES = (
        'Damageable', 'DamageMessage', 'PlayerController', 'EnemyController',
        'Transform', 'GameObject', 'Component', 'MonoBehaviour',
        'Collider', 'Rigidbody', 'Renderer', 'AudioSource',
        'Animator', 'Camera', 'Light', 'Image', 'Button', 'Text',
        'RaycastHit', 'ContactPoint', 'Collision',
    )
    _cast_pattern = '|'.join(_CAST_TYPES)
    if re.search(rf'\((?:{_cast_pattern})(?:\.\w+)?\)', source):
        source = re.sub(
            rf'\(({_cast_pattern})(?:\.\w+)?\)\s*',
            '',
            source,
        )
        fixes.append("Stripped remaining C# type casts")

    # Fix comment-embedded variable names from type mapping
    # Pattern: "m_-- TypeComment: explanation" → comment out the whole line
    # This happens when TYPE_MAP replaces a type like NavMeshAgent with "-- comment"
    # and the variable name m_NavMeshAgent becomes m_-- NavMeshAgent: ...
    if 'm_--' in source:
        # Replace "m_-- Comment..." variable references with a local variable
        # m_-- NavMeshAgent: use Roblox PathfindingService → _agent
        # Order matters: property access BEFORE assignment (both contain '=')
        # m_-- NavMeshAgent... used as receiver in expressions (with .property)
        source = re.sub(r'm_-- NavMeshAgent[^.\n]*\.(\w+)', r'_agent.\1', source)
        # m_-- NavMeshAgent... in assignment (standalone = expr)
        source = re.sub(
            r'm_-- NavMeshAgent[^=\n]*=\s*([^\n]+)',
            r'_agent = \1',
            source,
        )
        # m_-- NavMeshAgent... used standalone (e.g., in conditions)
        source = re.sub(r'm_-- NavMeshAgent[^\n]*', '_agent', source)
        # Any other m_-- patterns → comment out
        source = re.sub(r'm_--[^\n]*', '-- (type comment removed)', source)
        fixes.append("Fixed comment-embedded variable names (m_--)")

    # Fix broken generic type in angle brackets: <-- TypeComment: explanation>()
    # Pattern: FindFirstChildOfClass<-- NavMeshAgent: ...>() → :FindFirstChildOfClass("Instance")
    if '<--' in source:
        source = re.sub(
            r':?FindFirstChildOfClass<--[^>]*>\(\)',
            ':FindFirstChildWhichIsA("Instance")',
            source,
        )
        # Other generic types with comments
        source = re.sub(r'<--[^>]*>', '', source)
        fixes.append("Fixed broken <-- generic type comments")

    # Fix {-- brace-comment blocks (C# block comment that starts with brace)
    if '{--' in source:
        source = re.sub(r'\{(--[^\n]*)', r'\1', source)
        fixes.append("Fixed {-- brace-comment blocks")

    # Fix .Try: method syntax (C# TryGetComponent pattern)
    if '.Try:' in source:
        source = re.sub(r'\.Try:(\w+)', r':\1', source)
        fixes.append("Fixed .Try: method syntax")

    # Comment out remaining C# declaration lines that leaked through
    # Lines with { get; }, [Attribute], Dictionary<K,V>, etc.
    if '{ get' in source or '{ set' in source:
        source = re.sub(
            r'^(\s*)(.+\{\s*get\s*;.*)\s*$',
            r'\1-- [C#] \2',
            source,
            flags=re.MULTILINE,
        )
    if re.search(r'^\s*\[(?:CreateAssetMenu|Serializable|System)\b', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)\[(?:CreateAssetMenu|Serializable|System)\b[^\]]*\]\s*$',
            r'\1-- [C#] \g<0>',
            source,
            flags=re.MULTILINE,
        )

    # Fix remaining 'ref' parameter prefix in function calls
    if re.search(r'\bref\s+\w+', source):
        source = re.sub(r'\bref\s+(\w+)', r'\1', source)
        fixes.append("Stripped remaining 'ref' parameter prefix")

    # Fix C# tuple unpacking: (type name, type name) = (expr, expr)
    # → local name, name = expr, expr
    if re.search(r'\(\w+\s+\w+\s*,\s*\w+\s+\w+\)\s*=\s*\(', source):
        def _fix_tuple(m):
            types_vars = m.group(1)
            values = m.group(2)
            # Extract variable names (skip types)
            parts = [p.strip() for p in types_vars.split(',')]
            var_names = []
            for part in parts:
                tokens = part.split()
                if len(tokens) >= 2:
                    var_names.append(tokens[-1])
                else:
                    var_names.append(tokens[0])
            return f'local {", ".join(var_names)} = {values}'
        source = re.sub(
            r'\((\w+\s+\w+(?:\s*,\s*\w+\s+\w+)*)\)\s*=\s*\(([^)]+)\)',
            _fix_tuple,
            source,
        )
        fixes.append("Fixed C# tuple unpacking")

    # Strip C# attributes that leaked into code (not comments)
    # [SyncVar(...)], [Watched], [Command], [ClientRpc], [Server], etc.
    _CSHARP_ATTRS = (
        'SyncVar', 'Watched', 'Command', 'ClientRpc', 'Server',
        'TargetRpc', 'SyncObject', 'ShowInInspector', 'PropertySpace',
        'PropertyOrder', 'InfoBox', 'FoldoutGroup', 'BoxGroup',
        'Tooltip', 'HideIf', 'ShowIf', 'OnValueChanged',
    )
    if re.search(r'^\s*\[(?:' + '|'.join(_CSHARP_ATTRS) + r')\b', source, re.MULTILINE):
        source = re.sub(
            r'^\s*\[(?:' + '|'.join(_CSHARP_ATTRS) + r')\b[^\]]*\]\s*',
            '',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Stripped C# networking/editor attributes")

    # Fix malformed SetAttribute with type: `SetAttribute Type varName` → `SetAttribute("varName", nil)`
    if re.search(r':SetAttribute\s+\w+\s+\w+', source):
        source = re.sub(
            r':SetAttribute\s+\w+\s+(\w+)\s*$',
            r':SetAttribute("\1", nil)',
            source,
            flags=re.MULTILINE,
        )
        # Also: SetAttribute local varName = expr → local varName = expr
        source = re.sub(
            r':SetAttribute\s+local\s+(\w+)\s*=\s*(.+)$',
            r'-- SetAttribute mapped\nlocal \1 = \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed malformed SetAttribute patterns")

    # Fix DontDestroyOnLoad → comment (no Roblox equivalent, instances persist by default)
    if 'DontDestroyOnLoad' in source or 'Dont.DestroyOnLoad' in source:
        source = re.sub(
            r'^(\s*)(?:DontDestroyOnLoad|Dont\.DestroyOnLoad)\s*\([^)]*\).*$',
            r'\1-- DontDestroyOnLoad not needed (Roblox instances persist)',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Removed DontDestroyOnLoad (not needed in Roblox)")

    # Fix () { anonymous object/lambda syntax → {} table literal
    if re.search(r'\(\)\s*\{', source):
        source = re.sub(r'\(\)\s*\{', '{', source)
        fixes.append("Fixed () { anonymous object → table literal")

    # Fix .Play()() and similar double-invocation patterns
    # Also handles Play()(args) → Play(args)
    # BUT skip GetChildren()(N) which is handled separately as array indexing
    if '()(' in source:
        source = re.sub(r'(?<!GetChildren)(?<!Destroy)\(\)\(([^)]*)\)', r'(\1)', source)
        fixes.append("Fixed double invocation ()()")

    # Fix :Destroy()(obj) pattern — misplaced receiver
    if re.search(r':Destroy\(\)\((\w+)', source):
        source = re.sub(r':Destroy\(\)\((\w+(?:\.\w+)*)\)', r' \1:Destroy()', source)
        fixes.append("Fixed :Destroy()(obj) → obj:Destroy()")

    # Strip C# float/double/decimal literal suffixes: 1.0F, 2f, 3.5d, 0.1m, 1L, 2UL
    # Must run BEFORE other numeric processing
    if re.search(r'\d[fFdDmM]\b', source):
        source = re.sub(r'(\d)[fFdDmM]\b', r'\1', source)
        fixes.append("Stripped C# numeric type suffixes (F/f/d/D/m/M)")

    # Fix C# shorthand float literals without leading zero: .02 → 0.02, .5 → 0.5
    # These come from C# like `.02f` where the `f` suffix is stripped leaving `.02`
    # The bare receiver fix would otherwise turn these into `script.Parent.02`
    if re.search(r'(?<=[=\s,(\[+\-*/])\.(\d)', source):
        source = re.sub(r'(?<=[=\s,(\[+\-*/])\.(\d)', r'0.\1', source)
        fixes.append("Added leading zero to C# shorthand float literals (.NN → 0.NN)")

    # Fix broken numeric property access: script.Parent.02 → 0.02
    # These come from C# float literals like 0.02f where the "0." is stripped
    if re.search(r'script\.Parent\.(\d)', source):
        source = re.sub(
            r'script\.Parent\.(\d+)\b',
            r'0.\1',
            source,
        )
        fixes.append("Fixed broken numeric property access (script.Parent.NN → 0.NN)")

    # Fix C# math methods that don't exist in Luau
    # math.roundToInt → math.round, math.floorToInt → math.floor, math.ceilToInt → math.ceil
    if 'ToInt' in source:
        source = re.sub(r'math\.roundToInt\b', 'math.round', source)
        source = re.sub(r'math\.floorToInt\b', 'math.floor', source)
        source = re.sub(r'math\.ceilToInt\b', 'math.ceil', source)
        fixes.append("Fixed C# math ToInt methods → Luau equivalents")

    # Fix .Sort() with .CompareTo() → table.sort with Luau comparison
    # Pattern: objectives.Sort((function(A, B) return A.Name.CompareTo(B.Name end))
    if '.CompareTo(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Sort\(\(function\((\w+),\s*(\w+)\)\s*return\s+\2\.(\w+)\.CompareTo\(\3\.(\w+)\s*end\)\)',
            r'table.sort(\1, function(\2, \3) return \2.\4 < \3.\5 end)',
            source,
        )
        # Standalone .CompareTo fallback
        source = re.sub(r'(\w+(?:\.\w+)*)\.CompareTo\(([^)]+)\)', r'((\1 < \2) and -1 or ((\1 == \2) and 0 or 1))', source)
        fixes.append("Fixed .CompareTo() → Luau comparison")

    # Comment out Unity-only enum references that have no Roblox equivalent
    _UNITY_ONLY_ENUMS = (
        'ForceMode', 'AnimatorUpdateMode', 'RigidbodyInterpolation',
        'QueryTriggerInteraction', 'ForceMode2D', 'CollisionDetectionMode',
        'RigidbodyConstraints', 'AnimatorCullingMode',
    )
    for enum_name in _UNITY_ONLY_ENUMS:
        if enum_name in source:
            # If it's a standalone argument like ForceMode.Impulse, remove it
            source = re.sub(
                rf',\s*{enum_name}\.\w+\)',
                ')',
                source,
            )
            # If it's an assignment like m_Rigidbody.interpolation = RigidbodyInterpolation.Interpolate
            source = re.sub(
                rf'^(\s*).*=\s*{enum_name}\.\w+\s*$',
                rf'\1-- [Unity] removed {enum_name} assignment (no Roblox equivalent)',
                source,
                flags=re.MULTILINE,
            )
            fixes.append(f"Removed Unity-only {enum_name} enum references")

    # Comment out Matrix4x4 operations (no Roblox equivalent)
    if 'Matrix4x4' in source:
        source = re.sub(
            r'^(\s*)(.+Matrix4x4\..+)$',
            r'\1-- [Unity] \2',
            source,
            flags=re.MULTILINE,
        )
        # Also comment out reflectionMat.mNN assignments (matrix element access)
        source = re.sub(
            r'^(\s*)(reflectionMat\.m\d\d\s*=.+)$',
            r'\1-- [Unity] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out Matrix4x4 operations (no Roblox equivalent)")

    # Fix bare :Dot() without receiver → Vector3 dot product
    # Pattern: -:Dot(normal, pos) → -normal:Dot(pos)
    # Also handles :Dot(.CFrame.RightVector, dir) where first arg starts with dot
    if re.search(r'[^.\w]:Dot\(', source):
        # First arg may start with dot (bare property from .transform removal)
        source = re.sub(
            r'([^.\w]):Dot\((\.[\w.]+),\s*([^)]+)\)',
            r'\1script.Parent\2:Dot(\3)',
            source,
        )
        source = re.sub(
            r'([^.\w]):Dot\((\w+(?:\.\w+)*),\s*([^)]+)\)',
            r'\1\2:Dot(\3)',
            source,
        )
        fixes.append("Fixed bare :Dot() → receiver:Dot()")

    # Fix script.Parent:Dot() → Vector3.new():Dot() pattern
    # script.Parent:Dot(vec1, vec2) → vec1:Dot(vec2)
    if 'script.Parent:Dot(' in source:
        source = re.sub(
            r'script\.Parent:Dot\((\.[\w.]+),\s*([^)]+)\)',
            r'script.Parent\1:Dot(\2)',
            source,
        )
        source = re.sub(
            r'script\.Parent:Dot\((\w+(?:\.\w+)*),\s*([^)]+)\)',
            r'\1:Dot(\2)',
            source,
        )
        fixes.append("Fixed script.Parent:Dot(a, b) → a:Dot(b)")

    # Comment out remaining C# class declarations
    source = re.sub(r'^([^\S\n]*)class\s+\w+.*$', r'\1-- \g<0>', source, flags=re.MULTILINE)

    # Comment out bare 'using' statements (C# imports without -- prefix)
    # Use [^\S\n]* instead of \s* to avoid matching across newlines
    source = re.sub(r'^([^\S\n]*)using\s+\w+', r'-- \g<0>', source, flags=re.MULTILINE)

    # Comment out 'namespace' declarations
    source = re.sub(r'^([^\S\n]*)namespace\s+\w+', r'-- \g<0>', source, flags=re.MULTILINE)

    # Comment out 'base.Method()' calls (C# base class calls)
    if 'base.' in source:
        source = re.sub(r'^(\s*)base\.(\w+)', r'\1-- base.\2', source, flags=re.MULTILINE)

    # Strip C# generic type parameters from method calls and expressions
    # e.g. :FindFirstChildOfClass<Player>() → :FindFirstChildOfClass("Player")
    #       GetComponent<Rigidbody>() → FindFirstChildWhichIsA("Rigidbody")
    if '<' in source and '>' in source:
        # Convert GetComponent<Type>() → :FindFirstChildWhichIsA("Type")
        source = re.sub(
            r'(?::|\.)GetComponent<(\w+)>\s*\(\)',
            r':FindFirstChildWhichIsA("\1")',
            source,
        )
        # Convert :FindFirstChildOfClass<Type>() → :FindFirstChildWhichIsA("Type")
        source = re.sub(
            r':FindFirstChildOfClass<(\w+)>\s*\(\)',
            r':FindFirstChildWhichIsA("\1")',
            source,
        )
        # Strip remaining generic type parameters from method calls: Method<Type>(...) → Method(...)
        source = re.sub(r'(\w+)<\w+(?:,\s*\w+)*>\s*\(', r'\1(', source)
        # Strip generic type in static method calls: Foo<Bar>.Method() → Foo.Method()
        source = re.sub(r'(\w+)<[A-Z]\w+(?:,\s*[A-Z]\w+)*>\.', r'\1.', source)
        # Strip generic type after parens: ():GetDescendants()<Type>() → :GetDescendants()
        source = re.sub(r'\(\)<[A-Z]\w+>\(\)', '()', source)
        # Strip C# type casts with generics: (TypeName<T>)expr → expr
        source = re.sub(r'\([A-Z]\w+<[A-Z]\w+>\)', '', source)
        if source != original:
            fixes.append("Stripped C# generic type parameters")

    # Fix Unity static method calls that appear as orphaned dot-prefixed calls
    # .Destroy(obj) → obj:Destroy()
    # .Destroy(obj, delay) → game:GetService("Debris"):AddItem(obj, delay)
    if re.search(r'^\s*\.Destroy\(', source, re.MULTILINE):
        # .Destroy(obj, delay) → Debris:AddItem(obj, delay)
        source = re.sub(
            r'^(\s*)\.Destroy\((\w+(?:\.\w+)*),\s*([^)]+)\)',
            r'\1game:GetService("Debris"):AddItem(\2, \3)',
            source,
            flags=re.MULTILINE,
        )
        # .Destroy(obj) → obj:Destroy()
        source = re.sub(
            r'^(\s*)\.Destroy\((\w+(?:\.\w+)*)\)',
            r'\1\2:Destroy()',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed orphaned .Destroy() calls")

    # .Clone(prefab, position, rotation) → prefab:Clone() + set parent and CFrame
    # .Clone(prefab) → prefab:Clone()
    if re.search(r'(?:^\s*\.Clone\(|=\s*\.Clone\(|=\s*script\.Parent:Clone\()', source, re.MULTILINE):
        # Helper to extract balanced parenthesized args (handles nested parens)
        def _extract_clone_args(line, start_keyword):
            idx = line.find(start_keyword)
            if idx < 0:
                return None
            paren_start = line.index('(', idx)
            depth = 0
            for j in range(paren_start, len(line)):
                if line[j] == '(':
                    depth += 1
                elif line[j] == ')':
                    depth -= 1
                    if depth == 0:
                        return line[paren_start + 1:j]
            return None

        # Pre-pass: join multiline .Clone( / .Destroy( calls onto a single line
        raw_lines = source.split('\n')
        joined_lines = []
        i = 0
        while i < len(raw_lines):
            line = raw_lines[i]
            if re.search(r'\.Clone\(|\.Destroy\(', line):
                # Count parens — if unbalanced, join with next lines
                depth = line.count('(') - line.count(')')
                while depth > 0 and i + 1 < len(raw_lines):
                    i += 1
                    line = line.rstrip() + ' ' + raw_lines[i].strip()
                    depth = line.count('(') - line.count(')')
            joined_lines.append(line)
            i += 1

        # Process line by line for Clone calls
        new_lines = []
        for line in joined_lines:
            stripped = line.strip()
            indent = len(line) - len(line.lstrip())
            ind = ' ' * indent

            # Assignment: local x = .Clone(...)  or local x = script.Parent:Clone(...)
            assign_m = re.match(r'^(\s*)local\s+(\w+)\s*=\s*(?:script\.Parent:Clone|\.Clone)\(', line)
            if assign_m:
                var = assign_m.group(2)
                for kw in ['.Clone(', 'script.Parent:Clone(']:
                    args_str = _extract_clone_args(line, kw)
                    if args_str is not None:
                        break
                if args_str is not None:
                    # Split on top-level commas only
                    parts = _split_top_level(args_str)
                    prefab = parts[0].strip()
                    if len(parts) >= 2:
                        pos = parts[1].strip()
                        new_lines.append(f'{ind}local {var} = {prefab}:Clone()')
                        new_lines.append(f'{ind}{var}.Parent = workspace')
                        new_lines.append(f'{ind}{var}.CFrame = CFrame.new({pos})')
                    else:
                        new_lines.append(f'{ind}local {var} = {prefab}:Clone()')
                    continue

            # Standalone: .Clone(...) or script.Parent:Clone(...)
            standalone_m = re.match(r'^(\s*)(?:script\.Parent:Clone|\.Clone)\(', line)
            if standalone_m:
                for kw in ['.Clone(', 'script.Parent:Clone(']:
                    args_str = _extract_clone_args(line, kw)
                    if args_str is not None:
                        break
                if args_str is not None:
                    parts = _split_top_level(args_str)
                    prefab = parts[0].strip()
                    if len(parts) >= 2:
                        pos = parts[1].strip()
                        new_lines.append(f'{ind}local _clone = {prefab}:Clone()')
                        new_lines.append(f'{ind}_clone.Parent = workspace')
                        new_lines.append(f'{ind}_clone.CFrame = CFrame.new({pos})')
                    else:
                        new_lines.append(f'{ind}{prefab}:Clone().Parent = workspace')
                    continue

            new_lines.append(line)

        source = '\n'.join(new_lines)
        fixes.append("Fixed orphaned .Clone() (Unity Instantiate) calls")

    # Fix '.Debris:AddItem' (orphaned dot) → game:GetService("Debris"):AddItem
    if '.Debris:AddItem' in source:
        source = re.sub(
            r'\.Debris:AddItem\(([^)]+)\)',
            r'game:GetService("Debris"):AddItem(\1)',
            source,
        )

    # Fix Debris:AddItem with missing object argument (only has delay)
    # Pattern: Debris:AddItem( time) or Debris:AddItem(time) where time is a single number/variable
    if 'Debris' in source and 'AddItem' in source:
        source = re.sub(
            r'(game:GetService\("Debris"\):AddItem\()\s+(\w+)\)',
            r'\1script.Parent, \2)',
            source,
        )

    # Fix bare ':Method()' or '.Property' calls without a receiver
    # e.g. ':FindFirstChildWhichIsA("Sound")' → 'script.Parent:FindFirstChildWhichIsA("Sound")'
    # Only match at the start of an expression (after =, return, (, or start of line with indent)
    if re.search(r'(?:^|=|return|\()\s*:', source, re.MULTILINE):
        source = re.sub(
            r'((?:^|=|return\s|,|\()\s*):(\w+)',
            r'\1script.Parent:\2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Added 'script.Parent' receiver to bare method calls")

    # Fix bare '.Property' access without a receiver (e.g., '.Position')
    if re.search(r'(?:^|=|return|\()\s*\.(?!\.)', source, re.MULTILINE):
        source = re.sub(
            r'((?:=|return\s|,|\()\s*)\.(\w+)',
            r'\1script.Parent.\2',
            source,
        )

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
    # Must capture dotted paths: obj.list.Contains(x) → table.find(obj.list, x)
    if '.Contains(' in source:
        source = re.sub(
            r'([\w.]+)\.Contains\(([^)]+)\)',
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
        # Plain ToString(): x.ToString() → tostring(x) (handles dotted paths and bracketed exprs)
        source = re.sub(r'(\w+(?:\.\w+)*(?:\[[^\]]*\])*)\.ToString\(\)', r'tostring(\1)', source)
        # Handle (expr).ToString() → tostring(expr)
        source = re.sub(r'\)\.ToString\(\)', ')', source)  # Remove .ToString() after closing paren
        fixes.append("Fixed '.ToString()' → 'tostring()'")

    # Fix '.Add(item)' → 'table.insert(tbl, item)' for list
    # Fix '.Add(key, value)' → 'tbl[key] = value' for dictionary
    if '.Add(' in source:
        def _fix_add(m: re.Match) -> str:
            obj = m.group(1)
            args = m.group(2)
            # If args contains a comma, treat as dict.Add(key, value)
            if ',' in args:
                parts = args.split(',', 1)
                return f'{obj}[{parts[0].strip()}] = {parts[1].strip()}'
            # Otherwise it's list.Add(item)
            return f'table.insert({obj}, {args})'
        source = re.sub(
            r'([\w.]+)\.Add\(([^)]+)\)',
            _fix_add,
            source,
        )
        fixes.append("Fixed '.Add()' → 'table.insert()' or dict assignment")

    # Fix '.Remove(item)' → 'table.remove(tbl, table.find(tbl, item))'
    if '.Remove(' in source and '.RemoveAt(' not in source:
        source = re.sub(
            r'([\w.]+)\.Remove\(([^)]+)\)',
            r'table.remove(\1, table.find(\1, \2))',
            source,
        )
        fixes.append("Fixed '.Remove()' → 'table.remove()'")

    # Fix '.RemoveAt(idx)' → 'table.remove(tbl, idx + 1)' (0-based → 1-based)
    if '.RemoveAt(' in source:
        source = re.sub(
            r'([\w.]+)\.RemoveAt\(([^)]+)\)',
            r'table.remove(\1, \2 + 1)',
            source,
        )
        fixes.append("Fixed '.RemoveAt()' → 'table.remove()'")

    # Fix '.Insert(idx, item)' → 'table.insert(tbl, idx + 1, item)'
    if '.Insert(' in source:
        source = re.sub(
            r'([\w.]+)\.Insert\(([^,]+),\s*([^)]+)\)',
            r'table.insert(\1, \2 + 1, \3)',
            source,
        )
        fixes.append("Fixed '.Insert()' → 'table.insert()'")

    # Fix '.IndexOf(item)' → 'table.find(tbl, item)'
    if '.IndexOf(' in source:
        source = re.sub(
            r'([\w.]+)\.IndexOf\(([^)]+)\)',
            r'(table.find(\1, \2) or 0) - 1',
            source,
        )
        fixes.append("Fixed '.IndexOf()' → 'table.find()'")

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
        # Null-conditional: obj?.Invoke(args) → if obj then obj:Fire(args) end
        source = re.sub(
            r'(\w+)\?\.Invoke\(([^)]*)\)',
            r'if \1 then \1:Fire(\2) end',
            source,
        )
        # Regular: obj.Invoke(args) → obj:Fire(args)
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
    # BUT: skip if the pattern already contains Lua pattern sequences (%s, %d, %w, etc.)
    # which indicates it's an intentional Lua pattern, not a literal from string.Replace.
    if 'string.gsub(' in source:
        _LUA_MAGIC = set('().%+-*?[]^$')

        def _escape_gsub_literal(m):
            full = m.group(0)
            pattern_str = m.group(1)
            # If pattern already contains Lua pattern classes (%s, %d, %w, %a, etc.)
            # or anchors used as patterns (^, $), it's already a proper Lua pattern
            if re.search(r'%[sdwalpucx]', pattern_str) or pattern_str.startswith('^') or pattern_str.endswith('$'):
                return full
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

    # Fix .Find("name") → :FindFirstChild("name") (Unity Transform.Find)
    if '.Find(' in source:
        source = re.sub(r'\.Find\(("?[^)"]+\s*"?)\)', r':FindFirstChild(\1)', source)

    # Fix '.:Method()' → ':Method()' (dangling dot before colon method)
    if '.:' in source:
        source = re.sub(r'\.:(\w+)', r':\1', source)

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

    # Fix: .transform (Unity) → remove (Roblox parts have Position/CFrame directly)
    if ".transform.position" in source:
        source = re.sub(r'\.transform\.position\b', '.Position', source)
    if ".transform.rotation" in source:
        source = re.sub(r'\.transform\.rotation\b', '.CFrame', source)
    if ".transform." in source:
        source = re.sub(r'\.transform\.', '.', source)

    # Clean up double dots from .gameObject/.transform removal
    # Only fix word..word (property access), NOT string concatenation (expr .. expr)
    if '..' in source:
        # word..Word or ]..Word (no spaces) → word.Word / ].Word (property access double-dot)
        source = re.sub(r'([\w\]\)])\.\.([A-Za-z_])', r'\1.\2', source)

    # Fix Unity Vector3 property names → Roblox equivalents
    # .normalized → .Unit (Roblox property for unit vector)
    if '.normalized' in source:
        source = re.sub(r'\.normalized\b', '.Unit', source)
        fixes.append("Fixed .normalized → .Unit")
    # .magnitude → .Magnitude (case fix)
    if re.search(r'\.magnitude\b(?![\s]*=)', source):
        source = re.sub(r'\.magnitude\b', '.Magnitude', source)
        fixes.append("Fixed .magnitude → .Magnitude")
    # .sqrMagnitude → custom expression (no Roblox equivalent)
    # obj.sqrMagnitude → obj:Dot(obj) (Vector3:Dot returns dot product, which equals squared magnitude for self)
    if '.sqrMagnitude' in source:
        # Standalone: obj.sqrMagnitude → obj:Dot(obj)
        def _fix_sqr_mag(m):
            obj = m.group(1)
            return f'{obj}:Dot({obj})'
        source = re.sub(r'([\w.]+(?:\([^)]*\))?)\.sqrMagnitude\b', _fix_sqr_mag, source)
        fixes.append("Fixed .sqrMagnitude → :Dot(self)")

    # Fix .eulerAngles → CFrame:ToEulerAnglesXYZ() approximation
    if '.eulerAngles' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.eulerAngles\b',
            r'Vector3.new(\1.CFrame:ToEulerAnglesXYZ())',
            source,
        )
        fixes.append("Fixed .eulerAngles → CFrame:ToEulerAnglesXYZ()")

    # Fix .localPosition → .Position (Roblox parts use Position relative to parent in most cases)
    if '.localPosition' in source:
        source = re.sub(r'\.localPosition\b', '.Position', source)
        fixes.append("Fixed .localPosition → .Position")

    # Fix .localScale → .Size (approximate mapping)
    if '.localScale' in source:
        source = re.sub(r'\.localScale\b', '.Size', source)
        fixes.append("Fixed .localScale → .Size")

    # Fix .localRotation → .CFrame (approximate)
    if '.localRotation' in source:
        source = re.sub(r'\.localRotation\b', '.CFrame', source)
        fixes.append("Fixed .localRotation → .CFrame")

    # Fix .SetParent(parent) → .Parent = parent
    if '.SetParent(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.SetParent\((\w+(?:\.\w+)*)\)',
            r'\1.Parent = \2',
            source,
        )
        # SetParent(parent, worldPositionStays) → .Parent = parent
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.SetParent\((\w+(?:\.\w+)*),\s*\w+\)',
            r'\1.Parent = \2',
            source,
        )
        fixes.append("Fixed .SetParent() → .Parent assignment")

    # Fix .childCount → #:GetChildren()
    if '.childCount' in source:
        source = re.sub(r'(\w+(?:\.\w+)*)\.childCount\b', r'#\1:GetChildren()', source)
        fixes.append("Fixed .childCount → #:GetChildren()")

    # Fix .GetSiblingIndex() → table.find(parent:GetChildren(), obj)
    if '.GetSiblingIndex' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.GetSiblingIndex\(\)',
            r'(table.find(\1.Parent:GetChildren(), \1) or 1)',
            source,
        )
        fixes.append("Fixed .GetSiblingIndex() → table.find")

    # Fix .SetAsLastSibling() → reparent to same parent (Roblox auto-reorders)
    if '.SetAsLastSibling' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.SetAsLastSibling\(\)',
            r'-- SetAsLastSibling: Roblox auto-orders children',
            source,
        )

    # Fix .isKinematic → .Anchored (Roblox equivalent)
    if '.isKinematic' in source:
        source = re.sub(r'\.isKinematic\b', '.Anchored', source)
        fixes.append("Fixed .isKinematic → .Anchored")

    # Fix Physics.AllLayers/DefaultRaycastLayers → remove (Roblox checks all by default)
    if 'Physics.' in source:
        source = re.sub(r',\s*Physics\.AllLayers\b', '', source)
        source = re.sub(r',\s*Physics\.DefaultRaycastLayers\b', '', source)
        source = re.sub(r'\bPhysics\.OverlapSphere\(([^,]+),\s*([^)]+)\)',
                        r'workspace:GetPartBoundsInRadius(\1, \2)', source)
        fixes.append("Fixed Physics.* API calls")

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

    # Fix: new Vector3(...) → Vector3.new(...) and other Roblox types
    _ROBLOX_NEW_TYPES = [
        "Vector3", "Vector2", "CFrame", "Color3", "Color3uint8",
        "UDim2", "UDim", "Rect", "Ray", "Region3", "Instance",
        "NumberSequence", "NumberSequenceKeypoint", "ColorSequence",
        "ColorSequenceKeypoint", "NumberRange", "BrickColor",
        "TweenInfo", "OverlapParams", "RaycastParams",
    ]
    for rtype in _ROBLOX_NEW_TYPES:
        if f"new {rtype}(" in source:
            source = re.sub(rf'\bnew\s+{rtype}\(', f'{rtype}.new(', source)

    # Fix: new Type[N] (C# array) → table.create(N) or {}
    if re.search(r'\bnew\s+\w+\[\d+\]', source):
        source = re.sub(r'\bnew\s+\w+\[(\d+)\]', r'table.create(\1)', source)
    if re.search(r'\bnew\s+\w+\[0\]', source):
        source = re.sub(r'\bnew\s+\w+\[0\]', '{}', source)

    # Fix: new Type[] {...} (C# array initializer) → {...}
    if re.search(r'\bnew\s+\w+\[\]\s*\{', source):
        source = re.sub(r'\bnew\s+\w+\[\]\s*\{', '{', source)

    # Fix: new Type(...) for non-Roblox types → comment or strip
    # new GameObject("name") → Instance.new("Part"); part.Name = "name"
    if 'new GameObject(' in source:
        source = re.sub(
            r'\bnew\s+GameObject\s*\(\s*("(?:[^"\\]|\\.)*")\s*\)',
            r'Instance.new("Part") -- Name: \1',
            source,
        )
        source = re.sub(r'\bnew\s+GameObject\s*\(\)', 'Instance.new("Part")', source)

    # Fix: new Type.SubType(...) → comment (dotted type names)
    if re.search(r'\bnew\s+\w+\.\w+\s*[\(\[]', source):
        source = re.sub(
            r'\bnew\s+(\w+\.\w+)\s*\(',
            r'--[[ new \1 ]] (',
            source,
        )
        source = re.sub(
            r'\bnew\s+\w+\.\w+\s*\[([^\]]+)\]',
            r'table.create(\1)',
            source,
        )

    # Fix: new Type[expr] where expr uses # (postfix in C#, prefix in Luau)
    # e.g., new Animator[keyNames#] → table.create(#keyNames)
    if re.search(r'\bnew\s+\w+\[\w+#\]', source):
        source = re.sub(
            r'\bnew\s+\w+\[(\w+)#\]',
            r'table.create(#\1)',
            source,
        )

    # Fix: new Type[expr] for remaining array patterns with complex expressions
    if re.search(r'\bnew\s+\w+\[[^\]]+\]', source):
        source = re.sub(
            r'\bnew\s+\w+\[([^\]]+)\]',
            r'table.create(\1)',
            source,
        )

    # Fix: "new Type variable" (C# field declaration with 'new' modifier) → "local variable = nil -- Type"
    if re.search(r'^\s*new\s+[A-Z]\w+\s+\w+\s*$', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)new\s+([A-Z]\w+)\s+(\w+)\s*$',
            r'\1local \3 = nil -- \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix: "new Type function(...)" → comment (broken C# constructor)
    if re.search(r'\bnew\s+[A-Z]\w+\s+function\b', source):
        source = re.sub(
            r'^(\s*)new\s+([A-Z]\w+)\s+function\b.*$',
            r'\1-- (removed broken constructor: \2)',
            source,
            flags=re.MULTILINE,
        )

    # Fix: "TypeName.local var = new ..." → "local var = nil -- TypeName"
    if re.search(r'^\s*\w+\.local\s+\w+\s*=\s*new\b', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)\w+\.local\s+(\w+)\s*=\s*new\b.*$',
            r'\1local \2 = nil -- (constructor removed)',
            source,
            flags=re.MULTILINE,
        )

    # Fix: remaining new Type(...) → comment
    if re.search(r'\bnew\s+[A-Z]\w+\s*\(', source):
        source = re.sub(
            r'\bnew\s+([A-Z]\w+)\s*\(',
            r'--[[ new \1 ]] (',
            source,
        )

    # Fix: .tag == "Tag" → CollectionService:HasTag(obj, "Tag")
    # Unity tags → Roblox CollectionService tags
    if '.tag ==' in source or '.tag ~=' in source:
        # obj.tag == "Tag" → CollectionService:HasTag(obj, "Tag")
        source = re.sub(
            r'(\w+)\.tag\s*==\s*("(?:[^"\\]|\\.)*")',
            r'game:GetService("CollectionService"):HasTag(\1, \2)',
            source,
        )
        # obj.tag ~= "Tag" → not CollectionService:HasTag(obj, "Tag")
        source = re.sub(
            r'(\w+)\.tag\s*~=\s*("(?:[^"\\]|\\.)*")',
            r'not game:GetService("CollectionService"):HasTag(\1, \2)',
            source,
        )
        # Ensure CollectionService is imported
        if 'CollectionService' in source and 'GetService("CollectionService")' not in source.split('\n')[0]:
            # Already using inline GetService, no separate import needed
            pass
        fixes.append("Fixed .tag comparison → CollectionService:HasTag()")

    # Fix: .CompareTag("Tag") → CollectionService:HasTag(obj, "Tag")
    if '.CompareTag(' in source:
        source = re.sub(
            r'(\w+)\.CompareTag\(("(?:[^"\\]|\\.)*")\)',
            r'game:GetService("CollectionService"):HasTag(\1, \2)',
            source,
        )
        fixes.append("Fixed .CompareTag() → CollectionService:HasTag()")

    # Fix: C# event delegate calls → BindableEvent:Fire()
    # Detect commented-out event declarations and convert matching function calls
    if '-- [#C] event ' in source:
        event_names = re.findall(r'-- \[#C\] event \w+ (\w+)', source)
        for event_name in event_names:
            # EventName(args) → script:SetAttribute("EventName", args) or just comment
            # For now, wrap in a nil-safe call pattern
            if re.search(rf'\b{event_name}\s*\(', source):
                # Create a BindableEvent pattern
                source = re.sub(
                    rf'\b{event_name}\s*\(([^)]*)\)',
                    rf'-- TODO: {event_name} event (\1)',
                    source,
                )
        if event_names:
            fixes.append("Commented out C# event delegate calls")

    # Fix: KeyCode.X → Enum.KeyCode.X (Roblox requires Enum prefix)
    if re.search(r'\bKeyCode\.\w+', source) and 'Enum.KeyCode' not in source:
        source = re.sub(r'\bKeyCode\.(\w+)', r'Enum.KeyCode.\1', source)
        fixes.append("Fixed KeyCode.X → Enum.KeyCode.X")

    # Fix: workspace.Current: → workspace.CurrentCamera: (truncated property name)
    if 'workspace.Current:' in source:
        source = source.replace('workspace.Current:', 'workspace.CurrentCamera:')
        fixes.append("Fixed workspace.Current → workspace.CurrentCamera")

    # Fix: .attachedRigidbody → the part itself (Roblox parts have built-in physics)
    if 'attachedRigidbody' in source:
        # obj.attachedRigidbody → obj (Roblox parts ARE their own rigidbodies)
        source = re.sub(r'(\w+)\.attachedRigidbody\b', r'\1', source)
        fixes.append("Fixed .attachedRigidbody → part itself")

    # Fix: .velocity → .AssemblyLinearVelocity (Roblox physics property)
    if re.search(r'\w\.velocity\b', source):
        source = re.sub(r'(\w)\.velocity\b', r'\1.AssemblyLinearVelocity', source)

    # Fix: .angularVelocity → .AssemblyAngularVelocity
    if '.angularVelocity' in source:
        source = re.sub(r'\.angularVelocity\b', '.AssemblyAngularVelocity', source)

    # Fix: .AddForceAtPosition → :ApplyImpulseAtPosition
    if '.AddForceAtPosition(' in source:
        source = re.sub(r'(\w+)\.AddForceAtPosition\(', r'\1:ApplyImpulseAtPosition(', source)

    # Fix: .collider (Unity) → the part itself (Roblox Touched gives the part directly)
    if '.collider' in source:
        source = re.sub(r'(\w+)\.collider\b', r'\1', source)
        fixes.append("Fixed .collider → part itself")

    # Fix: FindFirstChildWhichIsA("Rigidbody") → nil (parts don't have child Rigidbody)
    if 'FindFirstChildWhichIsA("Rigidbody")' in source:
        source = source.replace(
            ':FindFirstChildWhichIsA("Rigidbody")',
            ' -- Roblox parts have built-in physics'
        )

    # Fix: obj.position (lowercase) → obj.Position (Roblox PascalCase)
    # Only fix after word characters (instance access), not after Random/static types
    if '.position' in source:
        source = re.sub(r'(\w)\.position\b(?!\s*\()', r'\1.Position', source)
    # Fix: obj.name (lowercase) → obj.Name
    if re.search(r'\w\.name\b', source):
        source = re.sub(r'(\w)\.name\b(?!\s*\()', r'\1.Name', source)
    # Fix: obj.value (lowercase) → obj.Value
    if re.search(r'\w\.value\b', source):
        source = re.sub(r'(\w)\.value\b(?!\s*\()', r'\1.Value', source)
    # Fix: .text (lowercase) → .Text (Roblox PascalCase for TextLabel/TextButton)
    if '.text' in source:
        source = re.sub(r'([\w\)])\.text\b(?!\s*\()', r'\1.Text', source)
    # Fix: .rotation (C#) → .CFrame (Roblox uses CFrame for orientation)
    # Exclude Random.rotation (handled separately as CFrame.Angles conversion)
    if '.rotation' in source:
        def _fix_rotation(m):
            prefix = m.group(1)
            if prefix == 'Random':
                return m.group(0)  # keep Random.rotation intact
            return prefix + '.CFrame'
        source = re.sub(r'(\w+)\.rotation\b(?!\s*\()', _fix_rotation, source)

    # Fix: bare .forward/.right/.up → .CFrame.LookVector etc.
    # (after .transform stripping, these may remain without .transform prefix)
    if '.forward' in source:
        source = re.sub(r'(\w)\.forward\b(?!\s*\()', r'\1.CFrame.LookVector', source)
    if re.search(r'\w\.right\b', source):
        source = re.sub(r'(\w)\.right\b(?!\s*\()', r'\1.CFrame.RightVector', source)
    if re.search(r'\w\.up\b', source):
        source = re.sub(r'(\w)\.up\b(?!\s*\()', r'\1.CFrame.UpVector', source)

    # Fix: Ray.new(...) → just use origin/direction directly for workspace:Raycast
    # Ray.new is not a valid Roblox constructor
    if 'Ray.new(' in source:
        # local ray = Ray.new(origin, direction) → remove and use origin/direction directly
        # For now, comment out Ray.new and leave a note
        source = re.sub(
            r'\bRay\.new\(([^,]+),\s*([^)]+)\)',
            r'{Origin = \1, Direction = \2}',
            source,
        )
        fixes.append("Fixed Ray.new() → table with Origin/Direction")

    # Fix: :GetChild(n) → :GetChildren()[n+1] (Unity 0-based, Roblox 1-based)
    if ':GetChild(' in source:
        def _fix_get_child(m):
            obj = m.group(1)
            idx = m.group(2).strip()
            if idx.isdigit():
                return f'{obj}:GetChildren()[{int(idx) + 1}]'
            else:
                return f'{obj}:GetChildren()[{idx} + 1]'
        source = re.sub(r'(\w+):GetChild\(([^)]+)\)', _fix_get_child, source)
        fixes.append("Fixed :GetChild(n) → :GetChildren()[n+1]")

    # Fix: Gizmos.* → comment out (Unity editor-only debug drawing)
    if 'Gizmos.' in source:
        source = re.sub(r'^(\s*)(Gizmos\.\w+.*)$', r'\1-- \2 (Unity editor only)', source, flags=re.MULTILINE)
        fixes.append("Commented out Gizmos.* calls (Unity editor only)")

    # Fix: :PointToObjectSpace(pos) → .CFrame:PointToObjectSpace(pos)
    if ':PointToObjectSpace(' in source:
        source = re.sub(r'(\w+):PointToObjectSpace\(', r'\1.CFrame:PointToObjectSpace(', source)
        # Avoid double .CFrame.CFrame
        source = source.replace('.CFrame.CFrame:', '.CFrame:')
        fixes.append("Fixed :PointToObjectSpace → .CFrame:PointToObjectSpace")
    if ':PointToWorldSpace(' in source:
        source = re.sub(r'(\w+):PointToWorldSpace\(', r'\1.CFrame:PointToWorldSpace(', source)
        source = source.replace('.CFrame.CFrame:', '.CFrame:')
        fixes.append("Fixed :PointToWorldSpace → .CFrame:PointToWorldSpace")

    # Fix: part:Lerp(target, alpha) → part.CFrame:Lerp(target, alpha)
    # Only for non-CFrame/Vector3 receivers (those already have :Lerp)
    if ':Lerp(' in source:
        source = re.sub(
            r'(script\.Parent):Lerp\(',
            r'\1.CFrame:Lerp(',
            source,
        )
        fixes.append("Fixed :Lerp() → .CFrame:Lerp()")

    # Fix: part:Rotate(axis, angle) → part.CFrame = part.CFrame * CFrame.Angles(...)
    if ':Rotate(' in source:
        def _fix_rotate(m):
            obj = m.group(1)
            args = m.group(2)
            return f'{obj}.CFrame = {obj}.CFrame * CFrame.Angles(0, math.rad({args}), 0) -- :Rotate'
        source = re.sub(r'(\w+(?:\.\w+)*):Rotate\(([^)]+)\)', _fix_rotate, source)
        fixes.append("Fixed :Rotate() → CFrame rotation")

    # Fix: part:LookAt(target) → part.CFrame = CFrame.lookAt(part.Position, target)
    if ':LookAt(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*):LookAt\(([^)]+)\)',
            r'\1.CFrame = CFrame.lookAt(\1.Position, \2)',
            source,
        )
        fixes.append("Fixed :LookAt() → CFrame.lookAt()")

    # Fix: Type.tostring(x) → tostring(x) (wrong class prefix)
    if re.search(r'\w+\.tostring\(', source):
        source = re.sub(r'\w+\.tostring\(', 'tostring(', source)
        fixes.append("Fixed Type.tostring() → tostring()")

    # Fix: math.deg(1) → math.pi/180 (wrong conversion factor)
    if 'math.deg(1)' in source:
        source = source.replace('math.deg(1)', '(180 / math.pi)')
        fixes.append("Fixed math.deg(1) → 180/math.pi")

    # Fix: event subscription with arithmetic: event = event + handler → event:Connect(handler)
    # Pattern: EventName = EventName + FunctionName
    if re.search(r'(\w+)\s*=\s*\1\s*\+\s*(\w+)\s*$', source, re.MULTILINE):
        _event_words = {'Update', 'Changed', 'Event', 'Callback', 'Handler',
                        'Click', 'Touched', 'Death', 'Spawn'}
        # Words that suggest numeric variable, NOT event (avoid false positives)
        _numeric_prefixes = {'cur', 'max', 'min', 'total', 'count', 'num', 'sum'}
        def _fix_event_sub(m):
            indent, event, handler = m.group(1), m.group(2), m.group(3)
            # Skip if variable looks like a numeric counter (e.g. curHealth, maxAmmo)
            lower_event = event.lower()
            if any(lower_event.startswith(p) for p in _numeric_prefixes):
                return m.group(0)
            if any(w in event for w in _event_words):
                return f'{indent}{event}:Connect({handler})'
            return m.group(0)
        source = re.sub(
            r'^(\s*)(\w+)\s*=\s*\2\s*\+\s*(\w+)\s*$',
            _fix_event_sub,
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed event += subscription → :Connect()")

    # Fix: Input.GetAxis("AxisName") → proper Roblox input API
    # Also clean up broken GetGamepadState(...)("AxisName") from old API mapping
    if 'GetAxis' in source or 'GetGamepadState' in source:
        # Map axis names to proper Roblox input calls
        _axis_map = {
            'MouseX': 'UserInputService:GetMouseDelta().X',
            'MouseY': 'UserInputService:GetMouseDelta().Y',
            'Mouse X': 'UserInputService:GetMouseDelta().X',
            'Mouse Y': 'UserInputService:GetMouseDelta().Y',
            'Horizontal': '((UserInputService:IsKeyDown(Enum.KeyCode.D) and 1 or 0) - (UserInputService:IsKeyDown(Enum.KeyCode.A) and 1 or 0))',
            'Vertical': '((UserInputService:IsKeyDown(Enum.KeyCode.W) and 1 or 0) - (UserInputService:IsKeyDown(Enum.KeyCode.S) and 1 or 0))',
            'Mouse ScrollWheel': 'UserInputService:GetMouseDelta().Y',
        }
        # Fix broken GetGamepadState(...)("AxisName") pattern
        for axis_name, replacement in _axis_map.items():
            source = source.replace(
                f'UserInputService:GetGamepadState(Enum.UserInputType.Gamepad1)("{axis_name}")',
                replacement,
            )
        # Fix -- Input.GetAxis("AxisName") comments from API mapping
        for axis_name, replacement in _axis_map.items():
            source = re.sub(
                rf'-- Input\.GetAxis\("{re.escape(axis_name)}"\)',
                replacement,
                source,
            )
        # Generic fallback for other axis names
        source = re.sub(
            r'UserInputService:GetGamepadState\(Enum\.UserInputType\.Gamepad1\)\("([^"]+)"\)',
            r'0 -- Input.GetAxis("\1") not mapped',
            source,
        )
        source = re.sub(
            r'-- Input\.GetAxis\("([^"]+)"\)',
            r'0 -- Input.GetAxis("\1") not mapped',
            source,
        )
        fixes.append("Fixed Input.GetAxis() → proper Roblox input API")

    # Fix: .setActive(obj, bool) called as method → setActive(obj, bool) function call
    if '.setActive(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*):?\.setActive\(([^,]+),\s*([^)]+)\)',
            r'setActive(\2, \3)',
            source,
        )
        fixes.append("Fixed .setActive() method call → function call")

    # Fix Unity Raycast API → Roblox Raycast API
    # Unity: workspace:Raycast(ray, hit, range) → Roblox: workspace:Raycast(origin, direction * range)
    # Unity: workspace:Raycast(origin, direction, hit, range) → Roblox: workspace:Raycast(origin, direction * range)
    if 'Raycast(' in source:
        # Pattern: workspace:Raycast(ray, hit, range) → local hit = workspace:Raycast(ray.Origin, ray.Direction * range)
        source = re.sub(
            r'(\w+):Raycast\((\w+),\s*(\w+),\s*(\w+)\)',
            r'\1:Raycast(\2.Origin, \2.Direction * \4)',
            source,
        )
        # Pattern: workspace:Raycast(origin, direction, hit, range) → workspace:Raycast(origin, direction * range)
        source = re.sub(
            r'(\w+):Raycast\(([^,]+),\s*([^,]+),\s*(\w+),\s*(\w+)\)',
            r'\1:Raycast(\2, \3 * \5)',
            source,
        )
        fixes.append("Fixed Raycast API signature (Unity → Roblox)")

    # Fix math.acos with 2 args → dot product first
    # Unity: Vector3.Angle(a, b) → math.acos(a:Dot(b) / (a.Magnitude * b.Magnitude))
    if 'math.acos(' in source:
        source = re.sub(
            r'math\.acos\((\w+(?:\.\w+)*),\s*(\w+(?:\.\w+)*)\)',
            r'math.deg(math.acos(\1.Unit:Dot(\2.Unit)))',
            source,
        )
        fixes.append("Fixed math.acos(a, b) → dot product pattern")

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

    # Fix: postfix # operator → prefix # (arr# → #arr, from C# .Length conversion)
    if re.search(r'\w#(?!\w)', source):
        source = re.sub(r'(\w+)#(?!\w)', r'#\1', source)

    # Fix: C# for loops → Luau for loops
    # for (local i = 0; i < N; ++i) → for i = 0, N - 1 do
    if 'for (' in source:
        def _fix_for_loop(m: re.Match) -> str:
            indent = m.group(1)
            var = m.group(2)
            start = m.group(3)
            bound = m.group(4)
            # Fix postfix # operator: arr# → #arr
            if bound.endswith('#'):
                bound = '#' + bound[:-1]
            return f'{indent}for {var} = {start}, {bound} - 1 do'

        source = re.sub(
            r'^(\s*)for\s*\(\s*local\s+(\w+)\s*=\s*(\w+)\s*;\s*\w+\s*<\s*([^;]+?)\s*;\s*\+\+\w+\s*\)',
            _fix_for_loop,
            source,
            flags=re.MULTILINE,
        )

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

    # string.pack/string.unpack ARE available in Roblox Luau — no warning needed

    # Fix Unity method calls that use dot instead of colon syntax
    # These are common methods that must use : in Luau
    _DOT_TO_COLON_METHODS = [
        "Play", "Stop", "Pause", "Resume",
        "Clone", "Destroy", "ClearAllChildren",
        "SetPrimaryPartCFrame", "MoveTo",
        "WaitForChild", "FindFirstChild", "FindFirstChildWhichIsA",
        "FindFirstChildOfClass", "FindFirstAncestor",
        "GetChildren", "GetDescendants",
        "IsA", "IsDescendantOf", "IsAncestorOf",
        "SetAttribute", "GetAttribute",
        "PlayOneShot", "Rotate", "LookAt",
        "GetChild", "SetBool", "SetFloat", "SetInteger", "SetTrigger",
        "GetBool", "GetFloat", "GetInteger",
        "Move", "AddForce", "AddRelativeForce", "AddExplosionForce",
        "AddTorque", "Fire", "Connect", "Disconnect",
        "Raycast", "Kick", "LoadCharacter",
    ]
    for method in _DOT_TO_COLON_METHODS:
        pattern = f'.{method}('
        if pattern in source:
            # Fix when preceded by a word character or closing paren/bracket (receiver)
            source = re.sub(
                rf'([\w\)\]])\.\b{method}\b\(',
                rf'\1:{method}(',
                source,
            )

    # Fix Unity AudioSource properties → Roblox Sound properties
    if '.pitch' in source:
        source = re.sub(r'(\w+)\.pitch\b', r'\1.PlaybackSpeed', source)
    if '.volume' in source:
        source = re.sub(r'\.volume\b', '.Volume', source)
        fixes.append("Fixed .volume → .Volume")
    if '.loop' in source:
        source = re.sub(r'\.loop\b', '.Looped', source)
        fixes.append("Fixed .loop → .Looped")
    if '.isPlaying' in source:
        source = re.sub(r'\.isPlaying\b', '.IsPlaying', source)
        fixes.append("Fixed .isPlaying → .IsPlaying")
    # .clip.length → .TimeLength (audio clip duration)
    if '.clip.length' in source:
        source = re.sub(r'(\w+)\.clip\.length\b', r'\1.TimeLength', source)
        fixes.append("Fixed .clip.length → .TimeLength")
    # .clip = value → .SoundId = value (audio clip assignment)
    if '.clip' in source:
        source = re.sub(r'\.clip\b', '.SoundId', source)
        fixes.append("Fixed .clip → .SoundId")

    # Fix Unity physics: AddForce/AddRelativeForce → :ApplyImpulse
    if 'AddRelativeForce(' in source or 'AddForce(' in source:
        # rb:AddRelativeForce(vec, ForceMode.Impulse) → rb:ApplyImpulse(vec)
        source = re.sub(
            r'(\w+):AddRelativeForce\(([^,]+),\s*ForceMode\.\w+\)',
            r'\1:ApplyImpulse(\2)',
            source,
        )
        source = re.sub(
            r'(\w+):AddForce\(([^,]+),\s*ForceMode\.\w+\)',
            r'\1:ApplyImpulse(\2)',
            source,
        )
        # Without ForceMode → VectorForce (continuous force)
        source = re.sub(
            r'(\w+):AddRelativeForce\(([^)]+)\)',
            r'\1:ApplyImpulse(\2)',
            source,
        )
        source = re.sub(
            r'(\w+):AddForce\(([^)]+)\)',
            r'\1:ApplyImpulse(\2)',
            source,
        )

    # Fix Unity: AddExplosionForce → apply impulse away from explosion center
    if 'AddExplosionForce(' in source:
        source = re.sub(
            r'(\w+):AddExplosionForce\([^)]+\)',
            r'-- \g<0> (no direct Roblox equivalent; use Explosion instance)',
            source,
        )

    # Fix Unity Random.rotation → CFrame.new() * CFrame.Angles(...)
    if 'Random.rotation' in source:
        source = re.sub(
            r'Random\.rotation',
            'CFrame.Angles(math.random() * math.pi * 2, math.random() * math.pi * 2, math.random() * math.pi * 2)',
            source,
        )
    if 'Random.onUnitSphere' in source:
        source = re.sub(
            r'Random\.onUnitSphere',
            'Vector3.new(math.random() - 0.5, math.random() - 0.5, math.random() - 0.5).Unit',
            source,
        )

    # Fix SendMessage → SetAttribute (already partially handled, but clean up remnants)
    if 'SetAttributeOptions' in source:
        source = re.sub(
            r',\s*script\.Parent:SetAttributeOptions\.\w+',
            '',
            source,
        )

    # Fix lowercase .parent → .Parent
    if re.search(r'\.\bparent\b', source):
        source = re.sub(r'\.parent\b', '.Parent', source)

    # Fix .localEulerAngles → CFrame-based rotation
    if '.localEulerAngles' in source:
        source = re.sub(r'\.localEulerAngles\b', '.CFrame', source)

    # Fix Unity property names to Roblox PascalCase
    # .enabled → .Enabled (for Roblox instances)
    if '.enabled' in source:
        source = re.sub(r'\.enabled\b', '.Enabled', source)

    # .intensity → .Brightness (Unity Light.intensity → Roblox Light.Brightness)
    if '.intensity' in source:
        source = re.sub(r'\.intensity\b', '.Brightness', source)
        fixes.append("Fixed .intensity → .Brightness")

    # RunService.Stepped → RunService.Heartbeat (Stepped has different signature, Heartbeat is standard)
    if 'RunService.Stepped' in source:
        source = source.replace('RunService.Stepped', 'RunService.Heartbeat')
        fixes.append("Fixed RunService.Stepped → RunService.Heartbeat")

    # .CFrame.Position = value → .Position = value (CFrame.Position is read-only)
    if '.CFrame.Position =' in source:
        source = re.sub(r'(\w[\w.\[\]]*?)\.CFrame\.Position\s*=', r'\1.Position =', source)
        fixes.append("Fixed .CFrame.Position assignment → .Position")

    # .color → .Color (Unity Light/UI color property)
    if re.search(r'\.\bcolor\b', source):
        source = re.sub(r'\.color\b', '.Color', source)
        fixes.append("Fixed .color → .Color")

    # math.clamp01(x) → math.clamp(x, 0, 1) (not a Luau function)
    if 'math.clamp01(' in source:
        source = re.sub(r'math\.clamp01\(([^)]+)\)', r'math.clamp(\1, 0, 1)', source)
        fixes.append("Fixed math.clamp01() → math.clamp(x, 0, 1)")

    # GameObject:Destroy(obj, delay) → Debris:AddItem(obj, delay) / obj:Destroy()
    if 'GameObject:Destroy(' in source:
        # With delay: Debris:AddItem(obj, delay)
        source = re.sub(
            r'GameObject:Destroy\(([^,]+),\s*([^)]+)\)',
            r'game:GetService("Debris"):AddItem(\1, \2)',
            source,
        )
        # Without delay: obj:Destroy()
        source = re.sub(
            r'GameObject:Destroy\(([^)]+)\)',
            r'\1:Destroy()',
            source,
        )
        fixes.append("Fixed GameObject:Destroy() → :Destroy() / Debris:AddItem()")

    # FindFirstChildObjectOfType() → FindFirstChildOfClass("Instance") (invalid method)
    if 'FindFirstChildObjectOfType(' in source:
        source = re.sub(
            r':FindFirstChildObjectOfType\(\)',
            ':FindFirstChildWhichIsA("Instance")',
            source,
        )
        source = re.sub(
            r':FindFirstChildObjectOfType\(([^)]+)\)',
            r':FindFirstChildWhichIsA(\1)',
            source,
        )
        fixes.append("Fixed FindFirstChildObjectOfType → FindFirstChildWhichIsA")

    # .PlayDelayed(delay) → task.delay(delay, function() obj:Play() end)
    if '.PlayDelayed(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.PlayDelayed\(([^)]+)\)',
            r'task.delay(\2, function() \1:Play() end)',
            source,
        )
        fixes.append("Fixed .PlayDelayed() → task.delay + :Play()")

    # float.IsNaN(x) → (x ~= x) (NaN check in Luau)
    if 'float.IsNaN(' in source or 'float.IsInfinity(' in source:
        source = re.sub(r'float\.IsNaN\(([^)]+)\)', r'(\1 ~= \1)', source)
        source = re.sub(r'float\.IsInfinity\(([^)]+)\)', r'(\1 == math.huge or \1 == -math.huge)', source)
        fixes.append("Fixed float.IsNaN/IsInfinity → Luau equivalents")

    # Color.Lerp(a, b, t) → a:Lerp(b, t) (Roblox Color3:Lerp)
    if 'Color.Lerp(' in source:
        source = re.sub(r'Color\.Lerp\(([^,]+),\s*([^,]+),\s*([^)]+)\)', r'\1:Lerp(\2, \3)', source)
        fixes.append("Fixed Color.Lerp() → :Lerp()")

    # GetPartBoundsInRadiusNonAlloc → GetPartBoundsInRadius (NonAlloc not needed in Luau)
    if 'NonAlloc(' in source:
        source = source.replace('GetPartBoundsInRadiusNonAlloc', 'GetPartBoundsInRadius')
        source = source.replace('RaycastNonAlloc', 'Raycast')
        source = source.replace('SphereCastNonAlloc', 'SphereCast')
        source = source.replace('OverlapSphereNonAlloc', 'GetPartBoundsInRadius')
        fixes.append("Fixed NonAlloc methods → standard methods")

    # .maxDistance → .RollOffMaxDistance (Unity AudioSource.maxDistance → Roblox Sound)
    if '.maxDistance' in source:
        source = re.sub(r'\.maxDistance\b', '.RollOffMaxDistance', source)
        fixes.append("Fixed .maxDistance → .RollOffMaxDistance")

    # string.Empty → "" (not valid in Luau)
    if 'string.Empty' in source:
        source = source.replace('string.Empty', '""')
        fixes.append("Fixed string.Empty → empty string literal")

    # (.ToString() already handled earlier in the function)

    # Fix Unity class names in FindFirstChildWhichIsA/FindFirstChildOfClass → Roblox class names
    _UNITY_TO_ROBLOX_CLASS = {
        'AudioSource': 'Sound',
        'AudioListener': 'SoundGroup',
        'CharacterController': 'Humanoid',
        'Animator': 'AnimationController',
        'Rigidbody': 'BasePart',
        'MeshRenderer': 'MeshPart',
        'MeshFilter': 'MeshPart',
        'SkinnedMeshRenderer': 'MeshPart',
        'SphereCollider': 'BasePart',
        'BoxCollider': 'BasePart',
        'CapsuleCollider': 'BasePart',
        'Renderer': 'BasePart',
    }
    for unity_cls, rblx_cls in _UNITY_TO_ROBLOX_CLASS.items():
        if f'"{unity_cls}"' in source:
            source = source.replace(f'"{unity_cls}"', f'"{rblx_cls}"')
            fixes.append(f"Fixed Unity class '{unity_cls}' → Roblox '{rblx_cls}'")

    # :PlayOneShot(clip, volume) → :Play() (Unity AudioSource method → Roblox Sound)
    if ':PlayOneShot(' in source:
        # With volume: source:PlayOneShot(clip, volume) → source.Volume = volume; source:Play()
        source = re.sub(
            r'(\w+):PlayOneShot\([^,]+,\s*([^)]+)\)',
            r'\1.Volume = \2; \1:Play()',
            source,
        )
        # Without volume: source:PlayOneShot(clip) → source:Play()
        source = re.sub(
            r'(\w+):PlayOneShot\([^)]*\)',
            r'\1:Play()',
            source,
        )
        fixes.append("Fixed :PlayOneShot() → :Play()")

    # .StartsWith("str") → string.sub(var, 1, #"str") == "str"
    if '.StartsWith(' in source:
        def _fix_startswith(m):
            var, arg = m.group(1), m.group(2)
            return f'string.sub({var}, 1, #{arg}) == {arg}'
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.StartsWith\(("[^"]*")\)',
            _fix_startswith,
            source,
        )
        fixes.append("Fixed .StartsWith() → string.sub comparison")

    # .EndsWith("str") → string.sub(var, -#"str") == "str"
    if '.EndsWith(' in source:
        def _fix_endswith(m):
            var, arg = m.group(1), m.group(2)
            return f'string.sub({var}, -#{arg}) == {arg}'
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.EndsWith\(("[^"]*")\)',
            _fix_endswith,
            source,
        )
        fixes.append("Fixed .EndsWith() → string.sub comparison")

    # .Substring(start) → string.sub(var, start + 1) (0-based → 1-based)
    # .Substring(start, length) → string.sub(var, start + 1, start + length)
    if '.Substring(' in source:
        # Two-arg: .Substring(start, length)
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Substring\((\w+),\s*(\w+)\)',
            r'string.sub(\1, \2 + 1, \2 + \3)',
            source,
        )
        # One-arg: .Substring(start)
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Substring\((\w+)\)',
            r'string.sub(\1, \2 + 1)',
            source,
        )
        fixes.append("Fixed .Substring() → string.sub()")

    # .Trim() → string.match(var, "^%s*(.-)%s*$") or var:match(...)
    if '.Trim()' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Trim\(\)',
            r'string.match(\1, "^%%s*(.-)%%s*$")',
            source,
        )
        fixes.append("Fixed .Trim() → string.match trim pattern")

    # int.Parse(x) / float.Parse(x) → tonumber(x)
    if '.Parse(' in source:
        source = re.sub(r'\b(?:int|float|double|long|short|byte|ushort|uint|ulong)\.Parse\(([^)]+)\)', r'tonumber(\1)', source)
        fixes.append("Fixed Type.Parse() → tonumber()")

    # int.TryParse(x, out var) → tonumber(x) pattern
    if '.TryParse(' in source:
        source = re.sub(
            r'\b(?:int|float|double)\.TryParse\(([^,]+),\s*(?:out\s+)?(\w+)\)',
            r'\2 = tonumber(\1); \2 ~= nil',
            source,
        )
        fixes.append("Fixed Type.TryParse() → tonumber()")

    # StringBuilder → table.concat pattern (comment out broken usages)
    if 'StringBuilder' in source:
        source = re.sub(r'^\s*StringBuilder\s+\w+.*$', lambda m: f'-- {m.group(0).strip()} (no StringBuilder in Luau)', source, flags=re.MULTILINE)
        if '.AppendFormat(' in source or '.Append(' in source:
            source = re.sub(r'(\w+)\.AppendFormat\(', r'-- \1.AppendFormat(', source)
            source = re.sub(r'(\w+)\.Append\(', r'-- \1.Append(', source)
            source = re.sub(r'(\w+)\.EnsureCapacity\(', r'-- \1.EnsureCapacity(', source)
        fixes.append("Commented out StringBuilder usage (not available in Luau)")

    # `continue` keyword: valid in Roblox Luau (added 2021) — no fix needed

    # Fix 'require(expr or nil)' → safe require with nil check
    if 'or nil)' in source and 'require(' in source:
        source = re.sub(
            r'require\(([^)]+)\s+or\s+nil\)',
            r'(function() local m = \1; if m then return require(m) end; return nil end)()',
            source,
        )
        fixes.append("Fixed unsafe require(... or nil) pattern")

    # Fix remaining C# ternary operators: `condition ? a : b` → `if condition then a else b`
    # This catches ternaries the transpiler missed (e.g., function call conditions)
    # Uses line-by-line scanning to find `?` and `:` outside strings
    if ' ? ' in source:
        lines = source.split('\n')
        result_lines = []
        ternary_fixed = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith('--') or ' ? ' not in line:
                result_lines.append(line)
                continue
            # Find the ternary ? and : in the line (outside strings)
            # Scan for balanced ? : pattern
            new_line = _fix_ternary_in_line(line)
            if new_line != line:
                ternary_fixed = True
            result_lines.append(new_line)
        if ternary_fixed:
            source = '\n'.join(result_lines)
            fixes.append("Fixed remaining C# ternary operators")

    # Fix broken ternary patterns from C# `condition ? a : b` conversion failures
    # Pattern 1: `expr > (if VALUE then A else B)` → `(if expr > VALUE then A else B)`
    # Only match actual comparison operators (>=, <=, ==, ~=, >, <), NOT assignment (=)
    if re.search(r'(?:>=|<=|==|~=|[><])\s*\(if\s+', source):
        source = re.sub(
            r'(\S+)\s*(>=|<=|==|~=|[><])\s*\(if\s+(\S+)\s+then\s+(.+?)\s+else\s+(.+?)\)',
            r'(if \1 \2 \3 then \4 else \5)',
            source,
        )
        fixes.append("Fixed broken ternary expression")
    # Pattern 2: `func(args, (if VAL) > COMP then A else B)` — paren around 'if VAL' closed too early
    # e.g. `math.random(0, (if 1) > 0.5 then A else B)` → `(if math.random(0, 1) > 0.5 then A else B)`
    if re.search(r'\(if\s+\S+\)\s*[><=~]', source):
        lines = source.split('\n')
        new_lines = []
        for line in lines:
            m = re.search(r'(=\s*)(.*?)\(if\s+(\S+)\)\s*([><=~]+)\s*(\S+)\s+then\s+(.+?)\s+else\s+(.+)', line)
            if m:
                indent_assign = m.group(1)
                func_prefix = m.group(2)
                val = m.group(3)
                op = m.group(4)
                comp = m.group(5)
                true_expr = m.group(6)
                false_expr = m.group(7).rstrip(')')
                full_expr = func_prefix + val + ')'
                start = line[:m.start()]
                line = '%s%s(if %s %s %s then %s else %s)' % (
                    start, indent_assign, full_expr, op, comp, true_expr, false_expr)
            new_lines.append(line)
        source = '\n'.join(new_lines)
        fixes.append("Fixed broken ternary with misplaced paren")

    # Fix GetChildren()(N) → GetChildren()[N+1] (function call result indexed like function call)
    if 'GetChildren()(' in source:
        def _fix_getchildren_idx(m):
            idx = m.group(1)
            try:
                return 'GetChildren()[%d]' % (int(idx) + 1)
            except ValueError:
                return 'GetChildren()[%s + 1]' % idx
        source = re.sub(r'GetChildren\(\)\((\w+)\)', _fix_getchildren_idx, source)
        fixes.append("Fixed GetChildren()(N) → GetChildren()[N+1]")

    # Fix obj.game:GetService → game:GetService (stray object prefix before game:)
    if re.search(r'\w+\.game:GetService', source):
        source = re.sub(r'\w+\.game:GetService', 'game:GetService', source)
        fixes.append("Fixed obj.game:GetService → game:GetService")

    # Fix comment-embedded conditions: `if control-- comment: text then` → `if control then`
    if re.search(r'if\s+\w+--\s+\w+', source):
        source = re.sub(
            r'if\s+(\w+)--\s+[^\n]*?\s+then',
            r'if \1 then',
            source,
        )
        fixes.append("Fixed comment-embedded condition")

    # Fix mangled method names from transpilation
    if 'FindFirstChildOfClasssInChildren' in source:
        source = source.replace(
            'FindFirstChildOfClasssInChildren',
            'GetDescendants',
        )
        fixes.append("Fixed mangled FindFirstChildOfClasssInChildren → GetDescendants")

    # Fix stray type prefixes: `TypeName.local varName = ...` → `local varName = ...`
    if re.search(r'^\s*\w+\.local\s+', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)\w+\.local\s+(\w+)',
            r'\1local \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed stray type prefix before local declaration")

    # Fix 0-based array indexing with math.random: `arr[math.random(0, #arr)]` → `arr[math.random(1, #arr)]`
    if 'math.random(0,' in source:
        source = re.sub(
            r'math\.random\(0,\s*#(\w+)\)',
            r'math.random(1, #\1)',
            source,
        )
        fixes.append("Fixed 0-based math.random to 1-based for array indexing")

    # Fix float.PositiveInfinity/NegativeInfinity/MaxValue → math.huge
    if 'float.' in source:
        source = re.sub(r'\bfloat\.PositiveInfinity\b', 'math.huge', source)
        source = re.sub(r'\bfloat\.NegativeInfinity\b', '-math.huge', source)
        source = re.sub(r'\bfloat\.MaxValue\b', 'math.huge', source)
        source = re.sub(r'\bfloat\.MinValue\b', '-math.huge', source)
        source = re.sub(r'\bfloat\.Epsilon\b', '1e-7', source)
    if 'int.MaxValue' in source:
        source = source.replace('int.MaxValue', '2147483647')
    if 'int.MinValue' in source:
        source = source.replace('int.MinValue', '-2147483648')

    # Fix .Visible → .Enabled for Roblox Lights and .Visible → .Visible for GuiObjects (already correct)
    # In Roblox, Light objects use .Enabled, not .Visible
    # Pattern: obj that looks like a light (contains "Light", "light") .Visible → .Enabled
    if '.Visible' in source:
        # staffLight.Visible, m_Light.Visible → .Enabled
        source = re.sub(r'(\w*[Ll]ight\w*)\.Visible\b', r'\1.Enabled', source)
        # m_Collider.Visible → .CanCollide (colliders don't have Visible)
        source = re.sub(r'(\w*[Cc]ollider\w*)\.Visible\b', r'\1.CanCollide', source)
        # Renderer/emission/system.Visible → .Enabled (Roblox uses .Enabled for ParticleEmitter, etc.)
        source = re.sub(r'(\w*[Rr]enderer\w*)\.Visible\b', r'\1.Enabled', source)
        source = re.sub(r'(\w*[Ee]mission\w*)\.Visible\b', r'\1.Enabled', source)
        source = re.sub(r'(\w*[Ss]ystem\w*)\.Visible\b', r'\1.Enabled', source)
        # Generic fallback: any remaining .Visible on non-GUI objects → .Enabled
        # (Roblox GuiObjects do have .Visible, but most other objects use .Enabled)
        # Match after word chars or closing parens/brackets
        def _fix_visible(m):
            prefix = m.group(1)
            # Check if the context suggests a GUI object
            gui_keywords = ('gui', 'frame', 'label', 'button', 'text', 'image', 'scroll', 'menu', 'pause', 'hud', 'ui', 'canvas', 'panel')
            # Get the full line context for better detection
            start = m.start()
            line_start = m.string.rfind('\n', 0, start) + 1
            line = m.string[line_start:m.string.find('\n', start) if '\n' in m.string[start:] else len(m.string)]
            if any(kw in line.lower() for kw in gui_keywords):
                return m.group(0)
            return f'{prefix}.Enabled'
        source = re.sub(r'([\w\)\]]+)\.Visible\b', _fix_visible, source)

    # Fix math.lerp → mathLerp (math.lerp doesn't exist in Roblox Luau)
    if 'math.lerp' in source:
        source = source.replace('math.lerp', 'mathLerp')

    # Fix .RemoveRange(start, count) → loop with table.remove
    if '.RemoveRange(' in source:
        source = re.sub(
            r'(\w+)\.RemoveRange\(([^,]+),\s*([^)]+)\)',
            r'for _i = 1, \3 do table.remove(\1, \2 + 1) end -- RemoveRange',
            source,
        )
        fixes.append("Fixed .RemoveRange() → table.remove loop")

    # Fix .FindFirstChildOfClassOrThrow() → :FindFirstChildOfClass() (non-standard API)
    if 'FindFirstChildOfClassOrThrow' in source:
        source = source.replace('FindFirstChildOfClassOrThrow', 'FindFirstChildOfClass')
        fixes.append("Fixed FindFirstChildOfClassOrThrow → FindFirstChildOfClass")

    # Fix C# generic types in function parameters:
    # function Name(Dictionary<K,V> param) → function Name(param)
    # function Name(SyncDictionary<K, op, key, item) → function Name(op, key, item)
    if re.search(r'function\s+\w*\([^)]*<', source):
        # Strip type<generic> with closing >: Type<A,B> param → param
        source = re.sub(
            r'(\bfunction\s+\w*\([^)]*?)\w+<[^>]*>\s*(\w+)',
            r'\1\2',
            source,
        )
        # Strip type< without closing > (broken generics): Type<A, param) → param)
        source = re.sub(
            r'\w+<\w+(?:,\s*)?(?=\w+\))',
            '',
            source,
        )
        fixes.append("Stripped generic types from function parameters")

    # Fix C# reflection APIs that don't exist in Luau
    if '.GetFields()' in source or '.GetType()' in source:
        source = re.sub(r'^(\s*).*\.GetFields\(\).*$', r'\1-- (C# reflection removed)', source, flags=re.MULTILINE)
        source = re.sub(r'^(\s*).*\.GetCustomAttributes\(.*$', r'\1-- (C# reflection removed)', source, flags=re.MULTILINE)
        # GetType().Name → typeof(obj)
        source = re.sub(r'(\w+)\.GetType\(\)\.Name\b', r'typeof(\1)', source)
        source = re.sub(r'(\w+)\.GetType\(\)', r'typeof(\1)', source)
        fixes.append("Removed C# reflection API calls")

    # Fix .task.delay() pattern (broken event invocation)
    if '.task.delay()' in source:
        source = re.sub(r'(\w+)\.task\.delay\(\)', r'task.defer(\1)', source)
        fixes.append("Fixed .task.delay() → task.defer()")

    # Comment out Unity-only graphics/rendering API calls
    _UNITY_RENDER_APIS = (
        'Graphics.DrawMesh', 'Graphics.DrawMeshInstanced', 'Graphics.Blit',
        'Shader.SetGlobal', 'Shader.Find', 'Shader.PropertyToID',
        'GL.PushMatrix', 'GL.PopMatrix', 'GL.LoadOrtho', 'GL.Begin', 'GL.End',
        'RenderTexture.active', 'Camera.main.targetTexture',
    )
    _rendered = False
    for api in _UNITY_RENDER_APIS:
        if api in source:
            source = re.sub(
                rf'^(\s*)(.*{re.escape(api)}.*)$',
                r'\1-- [Unity render] \2',
                source,
                flags=re.MULTILINE,
            )
            _rendered = True
    if _rendered:
        fixes.append("Commented out Unity-only rendering API calls")

    # Fix .Sort() method → table.sort()
    if '.Sort(' in source:
        # Simple case: arr.Sort() → table.sort(arr)
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Sort\(\)\s*$',
            r'table.sort(\1)',
            source,
            flags=re.MULTILINE,
        )
        # With comparison function: arr.Sort(function...) → table.sort(arr, function...)
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Sort\((function\(.+)',
            r'table.sort(\1, \2',
            source,
        )
        fixes.append("Fixed .Sort() → table.sort()")

    # Fix C# property getter pattern: inline simple getters
    # Pattern: `local VAR = nil` followed by `local function get_VAR() return EXPR end`
    # Replace VAR = nil with VAR = EXPR (inline the getter)
    if 'local function get_' in source:
        lines = source.split('\n')
        new_lines = []
        skip_next = False
        for i, line in enumerate(lines):
            if skip_next:
                skip_next = False
                continue
            stripped = line.strip()
            # Look for: local VAR = nil [-- comment]
            m_nil = re.match(r'^(\s*)local\s+(\w+)\s*=\s*nil\b.*$', line)
            if m_nil and i + 1 < len(lines):
                indent, var_name = m_nil.group(1), m_nil.group(2)
                next_line = lines[i + 1].strip()
                # Check if next line is: local function get_VAR() return EXPR end
                m_getter = re.match(
                    rf'local\s+function\s+get_{re.escape(var_name)}\(\)\s*return\s+(.+?)\s+end$',
                    next_line,
                )
                if m_getter:
                    expr = m_getter.group(1)
                    new_lines.append(f'{indent}local {var_name} = {expr}')
                    skip_next = True  # Skip the getter function line
                    fixes.append(f"Inlined property getter for '{var_name}'")
                    continue
            new_lines.append(line)
        source = '\n'.join(new_lines)

    # Fix `dt` usage in task.wait() loops: `task.wait()` → `dt = task.wait()`
    # When `dt` is used in a while/for loop that has task.wait(), the dt should come
    # from task.wait()'s return value (not from RunService callback parameter)
    if 'task.wait()' in source and re.search(r'\bdt\b', source):
        lines = source.split('\n')
        in_loop = False
        loop_uses_dt = False
        loop_start = -1
        new_lines = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('--'):
                new_lines.append(line)
                continue
            # Detect while/for loops
            if re.search(r'\b(while|for)\b.*\bdo\b', stripped):
                in_loop = True
                loop_start = i
                loop_uses_dt = False
            if in_loop and re.search(r'\bdt\b', stripped) and 'task.wait' not in stripped:
                loop_uses_dt = True
            # Replace task.wait() with dt = task.wait() in loops that use dt
            if in_loop and stripped == 'task.wait()' and loop_uses_dt:
                indent = line[:len(line) - len(line.lstrip())]
                new_lines.append(f'{indent}dt = task.wait()')
                fixes.append("Fixed task.wait() → dt = task.wait() in loop using dt")
            else:
                new_lines.append(line)
            if stripped == 'end' and in_loop:
                in_loop = False
        source = '\n'.join(new_lines)

    # Fix read-only CFrame property assignments (LookVector, UpVector, RightVector)
    # obj.CFrame.LookVector = dir → obj.CFrame = CFrame.lookAt(obj.Position, obj.Position + dir)
    if re.search(r'\.CFrame\.(?:LookVector|UpVector|RightVector)\s*=', source):
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.CFrame\.LookVector\s*=\s*([^\n]+)',
            r'\1.CFrame = CFrame.lookAt(\1.Position, \1.Position + \2)',
            source,
        )
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.CFrame\.UpVector\s*=\s*([^\n]+)',
            r'-- UpVector assignment: \1.CFrame = CFrame.lookAt(\1.Position, \1.Position + \2)',
            source,
        )
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.CFrame\.RightVector\s*=\s*([^\n]+)',
            r'-- RightVector assignment: \1.CFrame rotation would need manual construction',
            source,
        )
        fixes.append("Fixed read-only CFrame vector property assignments")

    # Fix .Normalize() → .Unit (Vector3 is immutable in Roblox, no Normalize method)
    if '.Normalize()' in source:
        # obj.Normalize() used as statement → obj = obj.Unit
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Normalize\(\)',
            r'\1 = \1.Unit',
            source,
        )
        fixes.append("Fixed .Normalize() → .Unit assignment")

    # Fix FindFirstChildWhichIsA("Instance") → more meaningful default
    # "Instance" matches any child, which is meaningless. Use context to pick better type.
    if 'FindFirstChildWhichIsA("Instance")' in source:
        # If context mentions Animator/Animation → "AnimationController"
        # For NavMeshAgent → "Humanoid"
        # For Cinemachine → "Camera"
        # Default: just remove the filter and use FindFirstChild or GetChildren
        source = re.sub(
            r':FindFirstChildWhichIsA\("Instance"\)',
            ':FindFirstChildOfClass("BasePart")',
            source,
        )
        fixes.append("Fixed FindFirstChildWhichIsA('Instance') → meaningful type")

    # Fix .isTrigger assignment (not a Roblox property)
    if '.isTrigger' in source:
        source = re.sub(r'(\w+)\.isTrigger\s*=\s*true', r'\1.CanCollide = false', source)
        source = re.sub(r'(\w+)\.isTrigger\s*=\s*false', r'\1.CanCollide = true', source)
        source = re.sub(r'(\w+)\.isTrigger\b', r'(not \1.CanCollide)', source)
        fixes.append("Fixed .isTrigger → .CanCollide")

    # Fix .useGravity (not a Roblox property, Roblox uses Anchored)
    if '.useGravity' in source:
        source = re.sub(r'(\w+)\.useGravity\s*=\s*false', r'\1.Anchored = true', source)
        source = re.sub(r'(\w+)\.useGravity\s*=\s*true', r'\1.Anchored = false', source)
        fixes.append("Fixed .useGravity → .Anchored")

    # Fix .detectCollisions (not a Roblox property)
    if '.detectCollisions' in source:
        source = re.sub(r'(\w+)\.detectCollisions\s*=\s*(true|false)', r'\1.CanCollide = \2', source)
        fixes.append("Fixed .detectCollisions → .CanCollide")

    # Fix Debug.DrawLine/DrawRay (Unity editor-only, not in Roblox)
    if 'Debug.Draw' in source:
        source = re.sub(r'^(\s*)Debug\.Draw\w+\([^)]*\).*$', r'\1-- [Unity editor] \g<0>', source, flags=re.MULTILINE)
        fixes.append("Commented out Debug.Draw* calls")

    # Fix workspace.Gravity usage (it's a number in Roblox, not a Vector3)
    if 'workspace.Gravity:' in source:
        source = re.sub(r'workspace\.Gravity:Dot\(workspace\.Gravity\)', 'workspace.Gravity * workspace.Gravity', source)
        source = re.sub(r'workspace\.Gravity:(\w+)', r'Vector3.new(0, -workspace.Gravity, 0):\1', source)
        fixes.append("Fixed workspace.Gravity (number, not Vector3)")

    # Fix Raycast with wrong arg count (Unity signature vs Roblox)
    # Unity: Physics.Raycast(origin, dir, hit, dist, mask) → workspace:Raycast(origin, dir * dist, params)
    if 'workspace:Raycast(' in source:
        # Fix calls with too many args (5+) — Unity pattern
        def _fix_raycast(m):
            args = m.group(1)
            parts = [a.strip() for a in args.split(',')]
            if len(parts) >= 4:
                origin = parts[0]
                direction = parts[1]
                dist = parts[3] if len(parts) > 3 and re.match(r'[\w.]+$', parts[3]) else None
                if dist:
                    return f'workspace:Raycast({origin}, {direction} * {dist})'
                return f'workspace:Raycast({origin}, {direction})'
            return m.group(0)
        source = re.sub(
            r'workspace:Raycast\(([^)]+,[^)]+,[^)]+,[^)]+(?:,[^)]+)?)\)',
            _fix_raycast,
            source,
        )

    # Fix Camera.current → workspace.CurrentCamera
    if 'Camera.current' in source:
        source = re.sub(r'\bCamera\.current\b', 'workspace.CurrentCamera', source)
        fixes.append("Fixed Camera.current → workspace.CurrentCamera")

    # Fix Animator API methods → Roblox equivalents
    # animator:SetBool(hash, val) → obj:SetAttribute(hash, val)
    # animator:SetFloat(hash, val) → obj:SetAttribute(hash, val)
    # animator:SetTrigger(hash) → obj:SetAttribute(hash, true)
    # animator:GetBool(hash) → obj:GetAttribute(hash)
    # animator:GetFloat(hash) → obj:GetAttribute(hash)
    if re.search(r':Set(?:Bool|Float|Integer|Trigger)\(', source):
        source = re.sub(r'(\w+(?:\.\w+)*):SetBool\((\w+),\s*([^)]+)\)', r'\1:SetAttribute(\2, \3)', source)
        source = re.sub(r'(\w+(?:\.\w+)*):SetFloat\((\w+),\s*([^)]+)\)', r'\1:SetAttribute(\2, \3)', source)
        source = re.sub(r'(\w+(?:\.\w+)*):SetInteger\((\w+),\s*([^)]+)\)', r'\1:SetAttribute(\2, \3)', source)
        source = re.sub(r'(\w+(?:\.\w+)*):SetTrigger\((\w+)\)', r'\1:SetAttribute(\2, true)', source)
        fixes.append("Fixed Animator Set* methods → SetAttribute")
    if re.search(r':Get(?:Bool|Float|Integer)\(', source):
        source = re.sub(r'(\w+(?:\.\w+)*):GetBool\((\w+)\)', r'\1:GetAttribute(\2)', source)
        source = re.sub(r'(\w+(?:\.\w+)*):GetFloat\((\w+)\)', r'\1:GetAttribute(\2)', source)
        source = re.sub(r'(\w+(?:\.\w+)*):GetInteger\((\w+)\)', r'\1:GetAttribute(\2)', source)
        fixes.append("Fixed Animator Get* methods → GetAttribute")

    # Fix remaining bare `transform` variable → script.Parent
    # Only when 'transform' is used as a receiver (not defined locally)
    if re.search(r'\btransform\b', source) and 'local transform' not in source:
        source = re.sub(r'\btransform\b(?=\s*[.:])', 'script.Parent', source)
        source = re.sub(r'\btransform\b(?=\s*[,)])', 'script.Parent', source)
        fixes.append("Fixed bare 'transform' → 'script.Parent'")

    # Fix leading dot+space before method calls: `. obj:Method()` → `obj:Method()`
    if re.search(r'^\s*\.\s+\w', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)\.\s+(\w)',
            r'\1\2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed leading dot+space before method calls")

    # Fix comment-embedded conditions: `expr-- comment: text or then` → `expr then`
    # These come from API mappings that return comments, embedded in conditions
    if re.search(r'--[^-\n]*(?:or|and)\s+then\b', source):
        source = re.sub(
            r'--[^\n]*?(?=\s+or\s+then\b)',
            '',
            source,
        )
        # Also clean up orphaned `or then` at end of if conditions
        source = re.sub(r'\s+or\s+then\b', ' then', source)
        fixes.append("Fixed comment-embedded conditions")

    # Fix broken ternary: `local (if x = cond then A else B)` → `local x = (if cond then A else B)`
    if re.search(r'local\s+\(if\s+\w+\s*=', source):
        source = re.sub(
            r'local\s+\(if\s+(\w+)\s*=\s*(\w+)\s+then\s+(.+?)\s+else\s+(.+?)\)',
            r'local \1 = (if \2 then \3 else \4)',
            source,
        )
        fixes.append("Fixed broken ternary assignment syntax")

    # Fix `.updatePosition`, `.updateRotation` (NavMeshAgent properties, not in Roblox)
    if '.updatePosition' in source or '.updateRotation' in source:
        source = re.sub(r'\w+\.updatePosition\s*=\s*(true|false)', r'-- updatePosition = \1 (not in Roblox)', source)
        source = re.sub(r'\w+\.updateRotation\s*=\s*(true|false)', r'-- updateRotation = \1 (not in Roblox)', source)
        fixes.append("Commented out NavMeshAgent update flags")

    # Fix named parameters: `func(arg, name: value)` → `func(arg, value)`
    # C# named argument syntax is not valid in Luau
    if re.search(r'\w+:\s*\(if\b', source):
        source = re.sub(r'(\w+):\s*(\(if\b)', r'\2', source)
        fixes.append("Stripped C# named parameter syntax")

    # Fix stray `break` outside loops (from switch/case conversion)
    # Only remove `break` that isn't inside a for/while/repeat block
    lines = source.split('\n')
    new_lines = []
    loop_depth = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r'\b(for|while|repeat)\b', stripped):
            loop_depth += 1
        if stripped == 'end' and loop_depth > 0:
            loop_depth -= 1
        if stripped == 'break' and loop_depth == 0:
            new_lines.append(line.replace('break', '-- break (removed, not in loop)'))
            fixes.append("Removed stray break outside loop")
        else:
            new_lines.append(line)
    source = '\n'.join(new_lines)

    # Fix FindFirstChildOfClassInChildren → GetDescendants loop or FindFirstDescendant
    if 'FindFirstChildOfClassInChildren' in source:
        # Pattern with type arg: obj:FindFirstChildOfClassInChildren("Type") → find in descendants
        source = re.sub(
            r'(\w+(?:\.\w+)*):FindFirstChildOfClassInChildren\(([^)]+)\)',
            r'\1:FindFirstDescendant(\2)',
            source,
        )
        # Pattern without args: obj:FindFirstChildOfClassInChildren() → obj:GetDescendants()[1]
        source = re.sub(
            r'(\w+(?:\.\w+)*):FindFirstChildOfClassInChildren\(\)',
            r'\1:FindFirstChildOfClass("BasePart")',
            source,
        )
        fixes.append("Fixed FindFirstChildOfClassInChildren → FindFirstDescendant/FindFirstChildOfClass")

    # Comment out Unity rendering APIs (GL, Graphics, Shader.Set*, RenderTexture)
    # These have no Roblox equivalent
    if re.search(r'\bGL\.\w+|Graphics\.Draw|Shader\.Set|RenderTexture\.\w+', source):
        source = re.sub(
            r'^(\s*)(?!--)(.+?(?:\bGL\.\w+|Graphics\.Draw\w+|Shader\.Set\w+|RenderTexture\.\w+).*)$',
            r'\1-- [Unity render] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out Unity rendering APIs (GL/Graphics/Shader/RenderTexture)")

    # Comment out QualitySettings (Unity-only)
    if 'QualitySettings' in source:
        source = re.sub(
            r'^(\s*)(?!--)(.+?QualitySettings\.\w+.*)$',
            r'\1-- [Unity] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out QualitySettings (Unity-only)")

    # Fix .collider → the part itself (in Roblox, collision events give the BasePart directly)
    if '.collider' in source:
        # otherPart.collider → otherPart
        source = re.sub(r'(\w+)\.collider(?:Component)?\b', r'\1', source)
        fixes.append("Fixed .collider → part itself")

    # Fix .contacts[N].normal/.point → raycast-based alternative
    if '.contacts' in source:
        # col.contacts[0].normal → hit.Normal (from raycast)
        source = re.sub(r'(\w+)\.contacts\[\d+\]\.normal', r'\1.Normal or Vector3.yAxis', source)
        source = re.sub(r'(\w+)\.contacts\[\d+\]\.point', r'\1.Position', source)
        # #col.contacts → 1 (simplify to single contact)
        source = re.sub(r'#(\w+)\.contacts', '1', source)
        fixes.append("Fixed .contacts[N] → simplified contact data")

    # Fix .relativeVelocity → velocity difference
    if '.relativeVelocity' in source:
        source = re.sub(
            r'(\w+)\.relativeVelocity\b',
            r'\1.AssemblyLinearVelocity',
            source,
        )
        fixes.append("Fixed .relativeVelocity → .AssemblyLinearVelocity")

    # Fix Animator.deltaPosition/deltaRotation → nil (no Roblox equivalent)
    if 'deltaPosition' in source or 'deltaRotation' in source:
        source = re.sub(r'(\w+(?:\.\w+)*)\.deltaPosition\b', r'Vector3.zero', source)
        source = re.sub(r'(\w+(?:\.\w+)*)\.deltaRotation\b', r'CFrame.identity', source)
        fixes.append("Fixed Animator delta properties → zero values")

    # Fix Rigidbody.MovePosition/MoveRotation
    if '.MovePosition(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.MovePosition\(([^)]+)\)',
            r'\1.Position = \2',
            source,
        )
        fixes.append("Fixed .MovePosition() → .Position assignment")
    if '.MoveRotation(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.MoveRotation\(([^)]+)\)',
            r'\1.CFrame = \2',
            source,
        )
        fixes.append("Fixed .MoveRotation() → .CFrame assignment")

    # Fix SweepTest → Raycast approximation
    if '.SweepTest(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.SweepTest\(([^,]+),\s*(\w+),\s*([^)]+)\)',
            r'workspace:Raycast(\1.Position, \2 * \4)',
            source,
        )
        fixes.append("Fixed SweepTest → workspace:Raycast")

    # Fix .material property access (not directly available in Roblox)
    if re.search(r'\.material\b(?!\w)', source):
        # obj.material.color → obj.Color
        source = re.sub(r'(\w+)\.material\.color\b', r'\1.Color', source)
        # obj.material.SetFloat/GetFloat etc → comment out
        source = re.sub(
            r'^(\s*)(?!--)(.+\.material\.\w+\(.+)$',
            r'\1-- [Unity material] \2',
            source,
            flags=re.MULTILINE,
        )
        # Standalone material assignment → comment
        source = re.sub(r'(\w+)\.material\b', r'\1 --[[.material]]', source)
        fixes.append("Fixed .material property access")

    # Fix GetInstanceID() → tostring(obj) (unique identifier)
    if 'GetInstanceID' in source:
        source = re.sub(r'(\w+(?:\.\w+)*):?\.?GetInstanceID\(\)', r'tostring(\1)', source)
        fixes.append("Fixed GetInstanceID() → tostring()")

    # Comment out SceneLinkedSMB calls (Unity state machine behaviour, not in Roblox)
    if 'SceneLinkedSMB' in source and not re.search(r'--.*SceneLinkedSMB', source):
        source = re.sub(
            r'^(\s*)(?!--)(.+SceneLinkedSMB\.\w+.*)$',
            r'\1-- [Unity SMB] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out SceneLinkedSMB calls (Unity state machine)")

    # Fix animator.speed property (not in Roblox)
    if 'animator.speed' in source:
        source = re.sub(r'(\w+)\.animator\.speed\s*=\s*([^\n]+)', r'-- animator.speed = \2 (not in Roblox)', source)
        fixes.append("Commented out animator.speed assignment")

    if source != original:
        fixes.append("Fixed common API mistakes")
        log.info("  [%s] Fixed common API/syntax mistakes", name)

    return source


def _fix_structural_syntax(name: str, source: str, fixes: list[str]) -> str:
    """Fix structural Luau syntax issues: else if, ++/--, type declarations, etc."""
    original = source

    # Fix 'else if' → 'elseif' (Luau keyword)
    if 'else if ' in source:
        source = re.sub(r'\belse\s+if\b', 'elseif', source)
        fixes.append("Fixed 'else if' → 'elseif'")

    # Fix C# postfix ++/-- operators → Luau assignment
    # x++ or x-- at end of line or before ) or ;
    if re.search(r'\w+\+\+|\w+--', source):
        # i++ → i = i + 1 (standalone statement)
        source = re.sub(
            r'^(\s*)(\w+)\+\+\s*$',
            r'\1\2 = \2 + 1',
            source,
            flags=re.MULTILINE,
        )
        # i-- → i = i - 1 (standalone statement)
        source = re.sub(
            r'^(\s*)(\w+)--\s*$',
            r'\1\2 = \2 - 1',
            source,
            flags=re.MULTILINE,
        )
        # ++i at start of expression → (i = i + 1) — treat as standalone
        source = re.sub(
            r'^(\s*)\+\+(\w+)\s*$',
            r'\1\2 = \2 + 1',
            source,
            flags=re.MULTILINE,
        )
        # Inline x++ in expressions (e.g., arr[i++]) — harder, convert to separate line
        # For now, just handle the common arr[x++] pattern
        source = re.sub(
            r'(\w+)\[(\w+)\+\+\]',
            r'\1[\2]; \2 = \2 + 1 --[[post-increment]]',
            source,
        )
        fixes.append("Fixed C# ++/-- operators")

    # Fix C# inline type declarations inside method bodies
    # Pattern: "    Type varName" at start of line where Type is PascalCase
    # Common: Vector3 dir, float angle, RaycastHit hit, int count, etc.
    _CSHARP_TYPES = (
        'Vector3', 'Vector2', 'Quaternion', 'float', 'int', 'double', 'long',
        'short', 'byte', 'bool', 'string', 'char', 'RaycastHit', 'Ray',
        'Color', 'Color32', 'Bounds', 'Rect', 'Matrix4x4', 'Plane',
        'Collider', 'Rigidbody', 'Transform', 'GameObject', 'Component',
        'AudioSource', 'Animator', 'Renderer', 'Material', 'Texture',
        'ParticleSystem', 'Camera', 'WaitForSeconds', 'WaitForEndOfFrame',
        'WaitForFixedUpdate', 'WaitUntil', 'Coroutine', 'IEnumerator',
        'NavMeshAgent', 'NavMeshPath', 'CharacterController',
        'BoxCollider', 'SphereCollider', 'CapsuleCollider', 'MeshCollider',
        'Light', 'SpriteRenderer', 'Image', 'Text', 'Button',
        'RectTransform', 'Canvas', 'CanvasGroup',
    )
    for ctype in _CSHARP_TYPES:
        # "Type varName = expr" → "local varName = expr"
        pattern = rf'^(\s+){ctype}\s+(\w+)\s*='
        if re.search(pattern, source, re.MULTILINE):
            source = re.sub(pattern, r'\1local \2 =', source, flags=re.MULTILINE)
        # "Type var1, var2, var3" (multi-variable declaration) → "local var1, var2, var3 = nil, nil, nil"
        pattern = rf'^(\s+){ctype}\s+(\w+(?:\s*,\s*\w+)+)\s*$'
        if re.search(pattern, source, re.MULTILINE):
            def _multi_var_decl(m):
                indent = m.group(1)
                vars_str = m.group(2)
                var_names = [v.strip() for v in vars_str.split(',')]
                nils = ', '.join(['nil'] * len(var_names))
                return f'{indent}local {", ".join(var_names)} = {nils}'
            source = re.sub(pattern, _multi_var_decl, source, flags=re.MULTILINE)
        # "Type varName" (declaration without init) → "local varName = nil"
        pattern = rf'^(\s+){ctype}\s+(\w+)\s*$'
        if re.search(pattern, source, re.MULTILINE):
            source = re.sub(pattern, r'\1local \2 = nil', source, flags=re.MULTILINE)

    # Fix 'gameObject' references → script.Parent
    if re.search(r'\bgameObject\b', source):
        source = re.sub(r'\bgameObject\b', 'script.Parent', source)
        fixes.append("Fixed 'gameObject' → 'script.Parent'")

    # Fix 'this' keyword → script.Parent (standalone, not this.)
    if re.search(r'\bthis\b(?!\.)', source):
        source = re.sub(r'\bthis\b(?!\.)', 'script.Parent', source)

    # Strip C# [Attribute] annotations — both standalone lines and inline prefixes
    _ATTR_NAMES = (
        'Range', 'SerializeField', 'Header', 'Tooltip', 'Space',
        'HideInInspector', 'FormerlySerializedAs', 'RequireComponent',
        'CreateAssetMenu', 'Serializable', 'System', 'MenuItem',
        'AddComponentMenu', 'ExecuteInEditMode', 'ExecuteAlways',
        'DisallowMultipleComponent', 'DefaultExecutionOrder',
        'ContextMenu', 'ContextMenuItem',
    )
    _ATTR_PATTERN = '|'.join(_ATTR_NAMES)
    # Standalone attribute lines
    if re.search(rf'^\s*\[(?:{_ATTR_PATTERN})\b', source, re.MULTILINE):
        source = re.sub(
            rf'^\s*\[(?:{_ATTR_PATTERN})\b[^\]]*\]\s*\n',
            '',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Stripped C# [Attribute] annotations")
    # Inline attributes at start of line (before variable declarations)
    # e.g., "[SerializeField][Range(0.5, 3)] local ..." → "local ..."
    if re.search(rf'\[(?:{_ATTR_PATTERN})\b', source):
        source = re.sub(rf'\[(?:{_ATTR_PATTERN})\b[^\]]*\]\s*', '', source)
        fixes.append("Stripped inline C# [Attribute] annotations")

    # Fix: "TypeName varName = default" → "local varName = nil" (C# field with default)
    if '= default' in source:
        source = re.sub(
            r'^(\s*)[A-Z]\w+\s+(\w+)\s*=\s*default\b.*$',
            r'\1local \2 = nil',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed C# '= default' field declarations")

    # Fix 'IsKeyDownDown' → 'IsKeyDown' (doubled suffix from GetKeyDown mapping)
    if 'IsKeyDownDown' in source:
        source = source.replace('IsKeyDownDown', 'IsKeyDown')
        fixes.append("Fixed 'IsKeyDownDown' → 'IsKeyDown'")

    # Fix 'workspace:GetServerTimeNow()Scale' → timeScale variable
    # Time.timeScale in Unity has no direct Roblox equivalent — use a module variable
    if 'GetServerTimeNow()Scale' in source:
        source = re.sub(
            r'workspace:GetServerTimeNow\(\)Scale',
            '_timeScale',
            source,
        )
        # Inject _timeScale variable at top if not present
        if '_timeScale' in source and 'local _timeScale' not in source:
            lines = source.split('\n')
            # Find insertion point after service requires
            insert_idx = 0
            for i, line in enumerate(lines):
                if line.strip().startswith('local ') and 'GetService' in line:
                    insert_idx = i + 1
            lines.insert(insert_idx, 'local _timeScale = 1 -- Unity Time.timeScale equivalent')
            source = '\n'.join(lines)
        fixes.append("Fixed Time.timeScale conversion")

    # Fix remaining .transform references
    if '.transform' in source:
        # .transform.localPosition → .Position (already handled .transform.position above)
        source = re.sub(r'\.transform\.localPosition\b', '.Position', source)
        source = re.sub(r'\.transform\.localRotation\b', '.CFrame', source)
        source = re.sub(r'\.transform\.localScale\b', '.Size', source)
        source = re.sub(r'\.transform\.forward\b', '.CFrame.LookVector', source)
        source = re.sub(r'\.transform\.right\b', '.CFrame.RightVector', source)
        source = re.sub(r'\.transform\.up\b', '.CFrame.UpVector', source)
        source = re.sub(r'\.transform\.parent\b', '.Parent', source)
        # Bare .transform → remove (Instance IS its transform in Roblox)
        source = re.sub(r'\.transform\b', '', source)
        fixes.append("Removed .transform references")

    # Fix string concatenation: single dot between non-numeric expressions → ..
    # Pattern: "str" . var or var . "str" (C# + was partially converted to .)
    # Be careful not to match property access (word.word)
    if re.search(r'"\s*\.\s*(?:\(|[a-zA-Z])|(?:\)|[a-zA-Z0-9_])\s*\.\s*"', source):
        # "string" . expr → "string" .. expr
        source = re.sub(r'"\s*\.\s*(\(|\w)', r'" .. \1', source)
        # expr . "string" → expr .. "string"
        source = re.sub(r'(\)|\w)\s*\.\s*"', r'\1 .. "', source)
        fixes.append("Fixed string concat '.' → '..'")

    # Fix 'other' vs 'otherPart' mismatch in Touched handlers
    # When handler signature has 'otherPart' but body uses 'other'
    if 'otherPart' in source and re.search(r'\bother\b(?!Part)', source):
        # Only fix if 'other' is used as a variable (not part of another word)
        # and 'otherPart' appears as a function parameter
        if re.search(r'function\s*\(\s*otherPart\s*\)', source):
            source = re.sub(r'\bother\b(?!Part|\w)', 'otherPart', source)
            fixes.append("Fixed 'other' → 'otherPart' parameter name")

    # Fix task.spawn(FunctionName()) → task.spawn(FunctionName) (pass ref, don't call)
    # With args: task.spawn(Func(arg)) → task.spawn(function() Func(arg) end)
    if 'task.spawn(' in source:
        source = re.sub(
            r'task\.spawn\((\w+)\(\)\)',
            r'task.spawn(\1)',
            source,
        )
        # With args: task.spawn(Func(args)) → task.spawn(function() Func(args) end)
        source = re.sub(
            r'task\.spawn\((\w+)\(([^)]+)\)\)',
            r'task.spawn(function() \1(\2) end)',
            source,
        )

    # Fix task.delay arg order: task.delay(FuncName", time) → task.delay(time, FuncName)
    # Also handle malformed strings from broken transpilation
    if 'task.delay(' in source:
        # Fix malformed: task.delay(Name", time) → task.delay(time, Name)
        source = re.sub(
            r'task\.delay\((\w+)"\s*,\s*(\w+)\)',
            r'task.delay(\2, \1)',
            source,
        )
        # Fix wrong order: task.delay(FuncName, time) where first arg is not a number
        # Roblox task.delay(delayTime, callback) — if first arg is a word (not number), swap
        source = re.sub(
            r'task\.delay\(([A-Z]\w+)\s*,\s*(\w+)\)',
            r'task.delay(\2, \1)',
            source,
        )

    # Fix obj.#prop → #obj.prop (# operator in wrong position)
    # Also handles chained access: obj.Lines[idx].#TextList → #obj.Lines[idx].TextList
    if '.#' in source:
        source = re.sub(r'([\w.\[\]]+)\.#(\w+)', lambda m: '#' + m.group(1) + '.' + m.group(2), source)

    # Fix C# single-line property getters:
    # "Type Name { get { return expr; } }" → "local function get_Name() return expr end"
    if '{ get {' in source:
        def _fix_property_getter(m):
            indent = m.group(1)
            name = m.group(2).lstrip('.')
            expr = m.group(3).strip().rstrip(';')
            return f'{indent}local function get_{name}() return {expr} end'
        source = re.sub(
            r'^(\s*)(?:[\w.]+\s+)?\.?(\w+)\s*\{\s*get\s*\{\s*return\s+(.+?)\s*;?\s*\}\s*(?:set\s*\{[^}]*\}\s*)?\}',
            _fix_property_getter,
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Converted C# property getters to Luau functions")

    # Comment out remaining C# property declarations with get/set that couldn't be parsed
    # These have comment interruptions or complex patterns the regex above can't handle
    if re.search(r'^\s*(?:bool|int|float|string|[\w.]+)\s+\w.*\{\s*get\s*[\{;]', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)((?:bool|int|float|string|[\w.]+)\s+\w.*\{\s*get\s*[\{;].*)$',
            r'\1-- [#C] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out unparseable C# property declarations")

    # Fix C# expression-bodied members: "Type Name => expr" → "local function get_Name() return expr end"
    # Also handles: "local Name => expr" (from pre-processing)
    if '=>' in source:
        def _fix_expr_body(m):
            indent = m.group(1)
            name = m.group(2)
            expr = m.group(3).strip().rstrip(';')
            return f'{indent}local function get_{name}() return {expr} end'
        # "-- [#C] Type Name => expr" or "-- [#C] local Name => expr"
        source = re.sub(
            r'^(\s*)-- \[#C\]\s*(?:[\w.]+\s+)?(?:local\s+)?(\w+)\s+=>\s+(.+)$',
            _fix_expr_body,
            source,
            flags=re.MULTILINE,
        )
        # Uncommented: "Type Name => expr" or "local Name => expr"
        source = re.sub(
            r'^(\s*)(?:[\w.]+\s+)?(?:local\s+)?(\w+)\s+=>\s+(.+)$',
            _fix_expr_body,
            source,
            flags=re.MULTILINE,
        )

    # Fix undefined 'part' receiver → script.Parent in event handlers
    # 'part' is a common C# variable from GetComponent<Collider>() that doesn't exist in Luau
    if re.search(r'\bpart\.(Touched|TouchEnded|ChildAdded|ChildRemoved|Changed)\b', source):
        # Only replace if 'part' is not defined as a local variable
        if not re.search(r'\blocal\s+part\b', source):
            source = re.sub(
                r'\bpart\.(Touched|TouchEnded|ChildAdded|ChildRemoved|Changed)\b',
                r'script.Parent.\1',
                source,
            )
            fixes.append("Fixed undefined 'part' → 'script.Parent' in event handlers")

    # Fix comparison operator followed by unnecessary parens: ==("value") → == "value"
    if re.search(r'(==|~=)\s*\(("[^"]*")\)', source):
        source = re.sub(r'(==|~=)\s*\(("[^"]*")\)', r'\1 \2', source)
        fixes.append("Fixed operator parens: ==('value') → == 'value'")

    # Fix .Exists(predicate) → table.find pattern
    if '.Exists(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Exists\(([^)]+)\)',
            r'(table.find(\1, \2) ~= nil)',
            source,
        )
        fixes.append("Fixed .Exists() → table.find()")

    # Fix malformed table.find lambda: `table.find(tbl, function(x) ~= nil) return expr end)`
    # → `table.find(tbl, function(x) return expr end) ~= nil`
    if 'table.find' in source and 'function(' in source:
        source = re.sub(
            r'table\.find\(([^,]+),\s*function\((\w+)\)\s*~=\s*nil\)\s*return\s+(.+?)\s+end\)\)',
            r'(table.find(\1, function(\2) return \3 end) ~= nil)',
            source,
        )

    # Fix "obj.Parent =(value)" → "obj.Parent = value" (unnecessary parens)
    if '.Parent =(' in source:
        source = re.sub(r'\.Parent\s*=\s*\((\w+(?:\.\w+)*)\)', r'.Parent = \1', source)

    # Fix Unity FindObjectOfType/FindAnyObjectByType/FindObjectsOfType → workspace:FindFirstChildWhichIsA or GetDescendants
    if 'FindAnyObjectByType(' in source or 'FindObjectOfType(' in source:
        # FindAnyObjectByType<Type>() or FindObjectOfType<Type>() → workspace:FindFirstChildWhichIsA("Type")
        source = re.sub(
            r'FindAnyObjectByType\(\)',
            'workspace',
            source,
        )
        source = re.sub(
            r'FindObjectOfType\(\)',
            'workspace',
            source,
        )

    # Fix 0-based for loops → 1-based (Luau tables are 1-indexed)
    # Pattern: "for VAR = 0, #TBL - 1 do" with "TBL[VAR]" → "for VAR = 1, #TBL do" with "TBL[VAR]"
    # Also: "for VAR = 0, N - 1 do" with "TBL[VAR]" → adjust indices
    if re.search(r'for\s+\w+\s*=\s*0\s*,', source):
        lines = source.split('\n')
        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r'^(\s*)for\s+(\w+)\s*=\s*0\s*,\s*(.+?)\s*-\s*1\s+do\s*$', line)
            if m:
                indent, var, bound = m.group(1), m.group(2), m.group(3)
                # Find the scope of this for loop and check if var is used as table index
                # Rewrite to 1-based: for VAR = 1, BOUND do
                new_lines.append(f'{indent}for {var} = 1, {bound} do')
                fixes.append("Fixed 0-based for loop → 1-based")
            else:
                # Also handle "for VAR = 0, EXPR do" (without explicit -1)
                m2 = re.match(r'^(\s*)for\s+(\w+)\s*=\s*0\s*,\s*(\d+)\s*-\s*1\s+do\s*$', line)
                if m2:
                    indent, var, num = m2.group(1), m2.group(2), m2.group(3)
                    new_lines.append(f'{indent}for {var} = 1, {num} do')
                    fixes.append("Fixed 0-based for loop → 1-based")
                else:
                    new_lines.append(line)
            i += 1
        source = '\n'.join(new_lines)

    # Fix bare receiver: ".Property" or ".Method()" at expression start without object
    # Pattern: line starts with (optional indent) ".SomeProperty" → "script.Parent.SomeProperty"
    if re.search(r'^\s+\.[A-Z]\w*', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)\.([A-Z]\w*(?:\.\w+)*)',
            r'\1script.Parent.\2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed bare receiver → script.Parent")

    # Fix bare receiver in expressions: "= .Property" or "< .Property" or "> .Property"
    if re.search(r'[=<>+\-*/,]\s*\.[A-Z]\w*', source):
        source = re.sub(
            r'([=<>+\-*/,])\s*\.([A-Z])',
            r'\1 script.Parent.\2',
            source,
        )

    # Fix unassigned CFrame expressions (line is just "CFrame.Angles(...)" or "CFrame.new(...)")
    # These come from C# transform.Rotate()/transform.Translate() which mutate in-place
    # → "script.Parent.CFrame = script.Parent.CFrame * CFrame.Angles(...)"
    if re.search(r'^\s+CFrame\.Angles\(', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)CFrame\.Angles\((.+)\)\s*$',
            r'\1script.Parent.CFrame = script.Parent.CFrame * CFrame.Angles(\2)',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed unassigned CFrame.Angles → CFrame multiplication")

    if re.search(r'^\s+CFrame\.new\((.+)\)\s*$', source, re.MULTILINE):
        # CFrame.new(delta) → script.Parent.CFrame = script.Parent.CFrame + delta
        source = re.sub(
            r'^(\s+)CFrame\.new\((.+)\)\s*$',
            r'\1script.Parent.CFrame = script.Parent.CFrame + \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed unassigned CFrame.new → position offset")

    # Fix malformed list initialization: "--[[ new List ]] (N)" → "{}"
    if '--[[ new List ]]' in source:
        source = re.sub(
            r'--\[\[\s*new\s+List\s*\]\]\s*\(\d*\)',
            '{}',
            source,
        )
        fixes.append("Fixed list initialization → {}")

    # Fix C# new[] { ... } array initializer → { ... }
    if 'new[]' in source:
        source = re.sub(r'\bnew\[\]\s*\{', '{', source)
        fixes.append("Fixed C# new[] array initializer")

    # Fix bare `new Namespace.Type()` → `{}` (C# constructor that leaked through)
    if re.search(r'\bnew\s+[A-Z]', source):
        source = re.sub(
            r'\bnew\s+[\w.]+(?:Dictionary|List|HashSet|Queue|Stack|ArrayList)\s*\(\)',
            '{}',
            source,
        )
        # new Type{...} (object initializer without parens) → {...}
        source = re.sub(
            r'\bnew\s+[\w.]+\s*\{',
            '{',
            source,
        )
        # new Type() for other types → nil
        source = re.sub(
            r'\bnew\s+[\w.]+\s*\(\)',
            'nil --[[ new instance ]]',
            source,
        )
        fixes.append("Fixed bare C# new constructor")

    # Fix malformed for-in loops: `for _, x in expr( do)` → `for _, x in expr do`
    # Caused by C# foreach parentheses partially surviving conversion
    if '( do)' in source:
        source = re.sub(r'\(\s*do\)', ' do', source)
        fixes.append("Fixed malformed for-in loop parentheses")

    # Remove orphaned `end` after Start/Awake comment markers
    # Pattern: "-- Start: runs..." or "-- Awake: runs..." followed by code lines then bare `end`
    # The `end` is from the C# method closing brace and has no matching opener
    if '-- Start: runs' in source or '-- Awake: runs' in source:
        lines_tmp = source.split('\n')
        new_lines_tmp = []
        in_startup_block = False
        for i, line in enumerate(lines_tmp):
            stripped = line.strip()
            if '-- Start: runs' in stripped or '-- Awake: runs' in stripped:
                in_startup_block = True
                new_lines_tmp.append(line)
                continue
            if in_startup_block and stripped == 'end':
                # Check if next non-empty line is NOT indented more (i.e., this end closes the method)
                next_indent = -1
                for j in range(i + 1, len(lines_tmp)):
                    ns = lines_tmp[j].strip()
                    if ns and not ns.startswith('--'):
                        next_indent = len(lines_tmp[j]) - len(lines_tmp[j].lstrip())
                        break
                curr_indent = len(line) - len(line.lstrip())
                if next_indent <= curr_indent:
                    # This `end` closes the startup method — skip it
                    in_startup_block = False
                    continue
            if in_startup_block and stripped and not stripped.startswith('--'):
                # We're in a startup block with real code — don't auto-skip
                pass
            new_lines_tmp.append(line)
        source = '\n'.join(new_lines_tmp)
        fixes.append("Removed orphaned end after Start/Awake block")

    # Fix "obj.Parent =(expr; expr2)" (from C# inline post-increment in assignment)
    # Pattern: "x.Parent =(y; z = z + 1 --[[post-increment]])"
    if re.search(r'\.Parent\s*=\s*\([^;]+;', source):
        def _fix_parent_compound(m):
            indent = m.group(1)
            receiver = m.group(2)
            value = m.group(3).strip()
            rest = m.group(4).strip()
            # Remove trailing ) and --[[post-increment]]
            rest = re.sub(r'\s*--\[\[post-increment\]\]\s*\)?\s*$', '', rest)
            rest = rest.rstrip(')')
            return f'{indent}{receiver}.Parent = {value}\n{indent}{rest}'
        source = re.sub(
            r'^(\s*)([\w.]+)\.Parent\s*=\s*\(([^;]+);\s*(.+)\)\s*$',
            _fix_parent_compound,
            source,
            flags=re.MULTILINE,
        )

    # Fix: "while .Property" (bare receiver in while condition)
    if re.search(r'\bwhile\s+\.', source):
        source = re.sub(r'\bwhile\s+\.([\w.]+)', r'while script.Parent.\1', source)

    # Fix single-line C# if statements: "if (cond) stmt" → "if cond then stmt end"
    # Pattern: line starts with 'if' + parenthesized condition + non-then statement
    # e.g. "if (x > 0) return false" → "if (x > 0) then return false end"
    # Uses balanced paren matching to avoid matching incomplete conditions
    new_lines_slif = []
    for line in source.split('\n'):
        stripped = line.strip()
        if stripped.startswith('if ') or stripped.startswith('if('):
            m = re.match(r'^(\s*)if\s*\(', line)
            if m and ' then ' not in stripped and not stripped.endswith('then'):
                # Find the matching close paren
                start_idx = line.index('(')
                depth = 0
                close_idx = -1
                for ci in range(start_idx, len(line)):
                    if line[ci] == '(':
                        depth += 1
                    elif line[ci] == ')':
                        depth -= 1
                        if depth == 0:
                            close_idx = ci
                            break
                if close_idx > 0 and close_idx < len(line) - 1:
                    # There's content after the closing paren
                    after = line[close_idx + 1:].strip()
                    if after and not after.startswith('then') and not after.startswith('--'):
                        indent = m.group(1)
                        cond = line[start_idx:close_idx + 1]
                        line = f'{indent}if {cond} then {after} end'
                        fixes.append("Fixed single-line C# if statements (added then/end)")
        new_lines_slif.append(line)
    source = '\n'.join(new_lines_slif)

    # Fix missing 'then' after if/elseif conditions
    # Pattern: "if <condition>\n    <statement>" → "if <condition> then\n    <statement>"
    # This happens when C# code like "if (cond)\n    stmt;" has its braces removed but no 'then' added
    new_lines_mt = []
    lines_mt = source.split('\n')
    for i, line in enumerate(lines_mt):
        stripped = line.strip()
        # Check if line starts with 'if' or 'elseif' but doesn't end with 'then', 'do', or contain 'then' anywhere
        if re.match(r'^(if|elseif)\b', stripped) and not stripped.startswith('--'):
            # Skip lines that already have 'then' or are complete
            if ' then' not in stripped and not stripped.endswith('then'):
                # Check if a continuation line has 'then' (multiline condition)
                has_then_below = False
                for j in range(i + 1, min(i + 5, len(lines_mt))):
                    next_s = lines_mt[j].strip()
                    if next_s and ' then' in next_s or next_s.endswith('then'):
                        has_then_below = True
                        break
                    # If this non-empty line is NOT a continuation (not indented more), stop
                    if next_s and not next_s.startswith('--'):
                        curr_indent = len(line) - len(line.lstrip())
                        next_indent = len(lines_mt[j]) - len(lines_mt[j].lstrip())
                        if next_indent <= curr_indent:
                            break
                if not has_then_below:
                    # Check if the next non-empty line is indented (suggesting it's the body)
                    for j in range(i + 1, min(i + 3, len(lines_mt))):
                        next_s = lines_mt[j].strip()
                        if next_s and not next_s.startswith('--'):
                            curr_indent = len(line) - len(line.lstrip())
                            next_indent = len(lines_mt[j]) - len(lines_mt[j].lstrip())
                            if next_indent > curr_indent:
                                line = line.rstrip() + ' then'
                                fixes.append("Added missing 'then' after if/elseif")
                            break
        new_lines_mt.append(line)
    source = '\n'.join(new_lines_mt)

    # Fix missing 'end' after if/elseif blocks
    # Detect pattern: if block that transitions to unindented code without closing 'end'
    # This is a lightweight fix for the common case where C# "}" → "end" was dropped
    _fix_missing_ends_in_blocks(source, fixes)

    if source != original and not any('structural' in f.lower() for f in fixes):
        fixes.append("Fixed structural syntax issues")

    return source


def _fix_missing_ends_in_blocks(source: str, fixes: list[str]) -> str:
    """Detect and fix missing 'end' keywords in if/function blocks.

    Analyzes indentation to find blocks that are missing their closing 'end'.
    """
    # This is handled by _fix_missing_end_keywords — just a hook for future improvements
    return source


def _fix_missing_end_keywords(name: str, source: str, fixes: list[str]) -> str:
    """Fix missing `end` keywords by analyzing block structure.

    Scans for block openers (if/then, function, for/do, while/do, repeat)
    and ensures each has a matching `end` (or `until` for repeat).
    Also fixes stray `else` after `end` and stray closing braces.
    """
    # Normalize tabs to spaces (mixed tabs/spaces break indentation analysis)
    source = source.replace('\t', '    ')
    lines = source.split('\n')
    result = []
    original = source

    # First pass: fix `end` followed by `else` on next meaningful line
    # This pattern comes from C# "} else {" being split across lines
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()

        # Fix "} else" or bare "else" after an "end" line → merge into elseif/else
        if stripped == 'else' and len(result) > 0:
            # Look back for the previous non-empty line
            prev_idx = len(result) - 1
            while prev_idx >= 0 and not result[prev_idx].strip():
                prev_idx -= 1
            if prev_idx >= 0 and result[prev_idx].strip() == 'end':
                # Remove the 'end' and keep the 'else'
                indent = len(result[prev_idx]) - len(result[prev_idx].lstrip())
                result[prev_idx] = ' ' * indent + 'else'
                i += 1
                continue

        # Fix "};" or stray "}" that wasn't converted to "end"
        if stripped == '}' or stripped == '};':
            indent = len(lines[i]) - len(lines[i].lstrip())
            result.append(' ' * indent + 'end')
            i += 1
            continue

        result.append(lines[i])
        i += 1

    source = '\n'.join(result)

    # Pass 1b: placeholder — Connect end) fix moved to after structural fixes

    # Second pass: fix single-statement if/then blocks missing 'end'
    # Pattern: "if COND then" on one line, single statement on next line,
    # then a line at same or lower indent that is NOT end/else/elseif
    # → insert 'end' after the single statement
    source = _insert_missing_ends_for_single_statement_blocks(source, fixes)

    # Third pass: remove excess trailing `end` keywords from C# class closing braces
    # Count block openers vs closers — if there are more `end` than openers,
    # remove trailing `end` lines from the bottom of the script
    source = _remove_excess_trailing_ends(source, fixes)

    # Final pass: fix `end` → `end)` for `:Connect(function(` blocks
    # Uses indentation matching: the closing `end` for a Connect(function block
    # should be at the same indent level as the Connect line's body indent minus one level
    source = _fix_connect_closures(source, fixes)

    # Post-Connect pass: remove any remaining excess end/end) keywords
    # This catches cases where Connect fix preserved end) from AI output
    # that creates negative block balance
    source = _remove_excess_end_keywords(source, fixes)

    # Append missing `end` keywords at EOF if block depth is positive
    source = _append_missing_trailing_ends(source, fixes)

    if source != original:
        fixes.append("Fixed missing end keywords / stray braces")

    return source


def _append_missing_trailing_ends(source: str, fixes: list[str]) -> str:
    """Append missing `end` keywords at the end of a script.

    If the script ends with positive block depth (more openers than closers),
    append the needed `end` keywords. This is a safe fallback since unclosed
    blocks at EOF will always cause syntax errors.
    """
    lines = source.split('\n')
    depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('--'):
            continue
        if re.search(r'\bfunction\s*[\w.:(]', stripped):
            if not (stripped.endswith(' end') or stripped.endswith('\tend')):
                depth += 1
        if re.match(r'(?:if|elseif)\b.+\bthen\s*$', stripped) or (
                re.search(r'\bthen\s*$', stripped) and not re.match(r'(?:if|elseif)\b', stripped)
                and not re.search(r'\bfunction\b', stripped)):
            depth += 1
        if re.match(r'for\b.+\bdo\s*$', stripped):
            depth += 1
        if re.match(r'while\b.+\bdo\s*$', stripped):
            depth += 1
        if stripped == 'repeat':
            depth += 1
        if stripped == 'end' or stripped.startswith('end)'):
            depth -= 1
        if re.match(r'until\b', stripped):
            depth -= 1

    if depth > 0:
        for _ in range(depth):
            lines.append('end')
        fixes.append(f"Appended {depth} missing 'end' keyword(s) at EOF")
        return '\n'.join(lines)

    return source


def _remove_excess_end_keywords(source: str, fixes: list[str]) -> str:
    """Remove excess `end` or `end)` keywords that create negative block depth.

    Uses depth tracking to find `end`/`end)` lines where depth goes negative.
    These are orphaned closers from C# class/namespace braces or duplicate
    AI-generated closers.
    """
    lines = source.split('\n')
    depth = 0
    lines_to_remove = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith('--') or not stripped:
            continue

        # Count block openers
        if re.search(r'\bfunction\s*[\w.:(]', stripped):
            if not (stripped.endswith(' end') or stripped.endswith('\tend')):
                depth += 1
        if re.match(r'(?:if|elseif)\b.+\bthen\s*$', stripped) or (
                re.search(r'\bthen\s*$', stripped) and not re.match(r'(?:if|elseif)\b', stripped)
                and not re.search(r'\bfunction\b', stripped)):
            depth += 1
        if re.match(r'for\b.+\bdo\s*$', stripped):
            depth += 1
        if re.match(r'while\b.+\bdo\s*$', stripped):
            depth += 1
        if stripped == 'repeat':
            depth += 1

        # Count closers — mark for removal if depth would go negative
        if stripped == 'end' or stripped.startswith('end)'):
            depth -= 1
            if depth < 0:
                lines_to_remove.append(i)
                depth = 0  # Reset to prevent cascade
        if re.match(r'until\b', stripped):
            depth -= 1

    if lines_to_remove:
        for idx in reversed(lines_to_remove):
            lines.pop(idx)
        fixes.append(f"Removed {len(lines_to_remove)} excess end keyword(s)")

    return '\n'.join(lines)


def _fix_connect_closures(source: str, fixes: list[str]) -> str:
    """Fix bare `end` → `end)` for :Connect(function() blocks.

    Uses a two-pass approach:
    1. Find all :Connect(function( lines and record their indent
    2. For each Connect opener, find the last bare `end` at the same indent
       level (before the next non-blank non-comment line at same/lower indent
       that isn't `end`) and convert it to `end)`

    This runs after all structural fixes so the block structure is stable.
    """
    if ':Connect(function(' not in source:
        return source

    lines = source.split('\n')
    # Find all Connect(function( openers with their line index and indent
    connect_openers = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.search(r':Connect\(function\s*\(', stripped):
            indent = len(line) - len(line.lstrip())
            connect_openers.append((i, indent))

    if not connect_openers:
        return source

    # For each Connect opener, scan forward to find the matching `end`
    # that should become `end)`. The function body is indented deeper than
    # the Connect line. The matching `end` is the `end` at the Connect line's
    # indent level (or the body indent level, whichever is the function close).
    #
    # Strategy: track block nesting from the Connect line. The function's
    # closing `end` is when we return to the same nesting depth we had
    # at the Connect line (just the function block).
    ends_to_fix = set()  # line indices where `end` should become `end)`

    for open_idx, open_indent in connect_openers:
        # Find the matching end for the function opened by this Connect
        # We scan forward, tracking function depth (function opens +1, end -1)
        depth = 0
        found = False
        for j in range(open_idx, len(lines)):
            stripped = lines[j].strip()
            if stripped.startswith('--') or not stripped:
                continue

            # Count block openers
            if re.search(r'\bfunction\s*[\w.:(]', stripped):
                if not (stripped.endswith(' end') or stripped.endswith('\tend')):
                    depth += 1
            elif re.match(r'(?:if|elseif)\b.+\bthen\s*$', stripped) or (
                    re.search(r'\bthen\s*$', stripped) and not re.match(r'(?:if|elseif)\b', stripped)
                    and not re.search(r'\bfunction\b', stripped)):
                depth += 1
            elif re.match(r'for\b.+\bdo\s*$', stripped):
                depth += 1
            elif re.match(r'while\b.+\bdo\s*$', stripped):
                depth += 1
            elif stripped == 'repeat':
                depth += 1

            # Count block closers
            if stripped == 'end' or stripped.startswith('end)'):
                depth -= 1
                if depth == 0:
                    # This `end` closes the function opened by Connect
                    if stripped == 'end':
                        ends_to_fix.add(j)
                    found = True
                    break
            elif re.match(r'until\b', stripped):
                depth -= 1

        # If no matching end found and depth > 0, the function body is missing
        # its closing end. Add one at the appropriate indent level.
        if not found and depth > 0:
            # Insert `end)` after the last non-empty line before EOF or next Connect
            insert_idx = len(lines)
            for j in range(len(lines) - 1, open_idx, -1):
                if lines[j].strip():
                    insert_idx = j + 1
                    break
            lines.insert(insert_idx, ' ' * (open_indent) + 'end)')
            # Don't add to ends_to_fix since we inserted it directly

    # Apply fixes
    if ends_to_fix:
        for idx in sorted(ends_to_fix):
            line = lines[idx]
            indent = len(line) - len(line.lstrip()) if line.strip() else 0
            lines[idx] = ' ' * indent + 'end)'
        fixes.append(f"Fixed {len(ends_to_fix)} Connect(function) closure(s): end → end)")

    return '\n'.join(lines)


def _remove_excess_trailing_ends(source: str, fixes: list[str]) -> str:
    """Remove excess `end` keywords from the end of a script.

    C# class/namespace/method closing braces `}` get converted to `end` but
    have no matching block opener. Uses a stack-based approach to count
    actual block depth and removes orphaned trailing `end` keywords.
    """
    lines = source.split('\n')

    # Stack-based block depth tracking
    depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('--'):
            continue
        # Count block openers (skip single-line definitions like "function() ... end")
        if re.search(r'\bfunction\s*[\w.:(]', stripped):
            # Only count as opener if line does NOT also end with 'end'
            if not stripped.endswith(' end') and not stripped.endswith('\tend'):
                depth += 1
        if re.match(r'(?:if|elseif)\b.+\bthen\s*$', stripped) or (
                re.search(r'\bthen\s*$', stripped) and not re.match(r'(?:if|elseif)\b', stripped)
                and not re.search(r'\bfunction\b', stripped)):
            depth += 1
        if re.match(r'for\b.+\bdo\s*$', stripped):
            depth += 1
        if re.match(r'while\b.+\bdo\s*$', stripped):
            depth += 1
        if stripped == 'repeat':
            depth += 1
        # Count closers (standalone end, end), end),)
        if stripped == 'end' or stripped.startswith('end)'):
            depth -= 1
        if re.match(r'until\b', stripped):
            depth -= 1

    # If depth is negative, we have more closers than openers
    excess = -depth
    if excess <= 0:
        return source

    # Remove excess trailing `end` lines from the bottom
    removed = 0
    while removed < excess and lines:
        idx = len(lines) - 1
        while idx >= 0 and not lines[idx].strip():
            idx -= 1
        if idx < 0:
            break
        trailing = lines[idx].strip()
        if trailing == 'end' or trailing == 'end)':
            lines.pop(idx)
            removed += 1
        else:
            break

    if removed > 0:
        fixes.append(f"Removed {removed} excess trailing 'end' keywords")
        return '\n'.join(lines)

    return source


def _insert_missing_ends_for_single_statement_blocks(source: str, fixes: list[str]) -> str:
    """Insert missing `end` keywords using indentation-based block analysis.

    Uses a stack to track open blocks and their expected indent levels.
    When a line de-indents past an open block without a matching 'end',
    inserts the missing 'end'.

    This handles:
    - Single-statement if/then blocks: if cond then\n    stmt\n  (no end)
    - Nested if blocks that share a single 'end'
    - Early returns without closing 'end'
    """
    lines = source.split('\n')
    result = []
    changed = False
    # Stack of (indent_level, block_type) for open blocks
    block_stack: list[tuple[int, str]] = []

    def _get_indent(line):
        return len(line) - len(line.lstrip()) if line.strip() else -1

    def _is_block_opener(stripped):
        """Check if a line opens a block. Returns block type or None."""
        if stripped.startswith('--'):
            return None
        # "if ... then" (not single-line if...then...end)
        # Also handle continuation lines where `then` appears on next line of multi-line if
        if re.match(r'(?:if|elseif)\b.+\bthen\s*$', stripped):
            return 'if'
        if re.search(r'\bthen\s*$', stripped) and not re.match(r'(?:if|elseif)\b', stripped) and not re.search(r'\bfunction\b', stripped):
            return 'if'
        if stripped == 'else':
            return 'else'
        if re.match(r'for\b.+\bdo\s*$', stripped):
            return 'for'
        if re.match(r'while\b.+\bdo\s*$', stripped):
            return 'while'
        if re.match(r'repeat\s*$', stripped):
            return 'repeat'
        # function definitions: "function(...)", "local function Name(...)"
        if re.search(r'\bfunction\s+\w*\s*\([^)]*\)\s*$', stripped) or \
           re.search(r'\bfunction\s*\([^)]*\)\s*$', stripped):
            return 'function'
        return None

    for line in lines:
        stripped = line.strip()
        indent = _get_indent(line)

        if not stripped:
            result.append(line)
            continue

        # Skip comment-only lines for block tracking
        if stripped.startswith('--'):
            result.append(line)
            continue

        # Before adding this line, check if any open blocks need closing
        # based on indent level
        if indent >= 0 and stripped not in ('end', 'else') and not stripped.startswith('elseif '):
            while block_stack:
                top_indent, top_type = block_stack[-1]
                if indent <= top_indent:
                    # This line is at or before the block opener's indent
                    # The block should have been closed
                    result.append(' ' * (top_indent + 4) + 'end')
                    block_stack.pop()
                    changed = True
                else:
                    break

        # Handle 'end' keyword — pop the stack
        # If the end is at a lower indent than the top block, insert missing ends first
        if stripped == 'end':
            while len(block_stack) > 1:
                top_indent, top_type = block_stack[-1]
                # If this 'end' is at or below the opener's indent, the block
                # should be closed before this end takes effect
                if indent < top_indent:
                    result.append(' ' * top_indent + 'end')
                    block_stack.pop()
                    changed = True
                else:
                    break
            if block_stack:
                block_stack.pop()
            result.append(line)
            continue

        # Handle 'else' and 'elseif' — they close the current if-block
        # and open a new one at the same level
        if stripped == 'else' or stripped.startswith('elseif '):
            if block_stack and block_stack[-1][1] in ('if', 'else'):
                block_stack.pop()
            block_type = _is_block_opener(stripped)
            if block_type:
                block_stack.append((indent, block_type))
            result.append(line)
            continue

        # Check if this line opens a block
        block_type = _is_block_opener(stripped)
        if block_type:
            block_stack.append((indent, block_type))

        result.append(line)

    if changed:
        fixes.append("Inserted missing 'end' for unclosed blocks")

    return '\n'.join(result)


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
