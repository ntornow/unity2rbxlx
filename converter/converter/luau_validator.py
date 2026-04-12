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
    _had_trailing_newline = source.endswith('\n')

    source = _strip_leading_prose(name, source, fixes)
    source = _fix_runtime_script_creation(name, source, fixes)
    source = _fix_plugin_only_properties(name, source, fixes)
    source = _fix_enum_comparisons(name, source, fixes)
    source = _fix_csharp_remnants(name, source, fixes)
    source = _fix_common_api_mistakes(name, source, fixes)
    source = _fix_structural_syntax(name, source, fixes)
    source = _fix_missing_end_keywords(name, source, fixes)
    source = _fix_missing_function_end(name, source, fixes)
    source = _fix_undefined_module_return(name, source, fixes)
    source = _fix_missing_module_return(name, source, fixes)
    source = _fix_nil_typed_variables(name, source, fixes)
    source = _fix_module_script_parent_access(name, source, fixes)
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
    # Final pass: fix `and then` patterns that may have been introduced by structural fixes
    if re.search(r'\band\s+then\s*\n', source):
        source = re.sub(
            r'\band\s+then\s*\n(\s+)(.*?\))\s*$',
            r'and\n\1\2 then',
            source,
            flags=re.MULTILINE,
        )
    if re.search(r'\band\s+then\b', source):
        source = re.sub(r'\band\s+then\b', 'then', source)
    # Comment out orphaned continuation lines from multi-line expressions
    # where the first line was commented out (e.g., `-- [Unity] if ... or\n  not expr)`)
    new_lines_cont = []
    prev_was_comment_continuing = False
    for line in source.split('\n'):
        stripped = line.strip()
        if prev_was_comment_continuing:
            # This line is a continuation — comment it out too
            indent = line[:len(line) - len(line.lstrip())]
            new_lines_cont.append(f'{indent}-- [continuation] {stripped}')
            # Check if this continuation also continues (ends with `or`/`and` before `)`)
            prev_was_comment_continuing = bool(re.search(r'\b(?:or|and)\s*$', stripped))
        else:
            new_lines_cont.append(line)
        # Check if current (original) line is a comment that ends with `or` or `and`
        if stripped.startswith('--') and re.search(r'\b(?:or|and)\s*$', stripped):
            prev_was_comment_continuing = True
        elif not prev_was_comment_continuing:
            prev_was_comment_continuing = False
    source = '\n'.join(new_lines_cont)

    # Comment out orphaned end when a block-opening statement was auto-commented
    # Strategy: match indent of `-- [tag] if/using` with the next `end` at same indent
    # The indent match ensures we remove the correct `end` for the commented block
    lines_iffix = source.split('\n')
    new_lines_iffix = []
    changed_iffix = False
    i = 0
    while i < len(lines_iffix):
        stripped = lines_iffix[i].strip()
        comment_indent = len(lines_iffix[i]) - len(lines_iffix[i].lstrip())
        is_commented_block_opener = (
            stripped.startswith('--') and
            re.search(r'\[(?:Unity|broken|C#|#C)', stripped) and
            (re.search(r'\bif\b.*\bthen\b', stripped) or
             re.search(r'\busing\b\s*\(', stripped))
        )
        if is_commented_block_opener:
            new_lines_iffix.append(lines_iffix[i])
            j = i + 1
            found_end = False
            pending_lines = []
            while j < len(lines_iffix) and j - i <= 10:
                inner = lines_iffix[j].strip()
                end_indent = len(lines_iffix[j]) - len(lines_iffix[j].lstrip())
                if inner == 'end' and end_indent == comment_indent:
                    # Same indent — check if a real block opener above needs this end
                    # Scan backwards from comment to find unclosed real openers at same indent
                    has_unclosed_real = False
                    rdepth = 0
                    for k in range(i - 1, max(i - 20, -1), -1):
                        kl = lines_iffix[k].strip()
                        ki = len(lines_iffix[k]) - len(lines_iffix[k].lstrip())
                        if kl.startswith('--') or not kl:
                            continue
                        if ki == comment_indent and (kl == 'end' or kl.startswith('end ')):
                            rdepth += 1  # this end closes an opener above
                        if ki == comment_indent and re.search(
                            r'\b(?:if\b.*\bthen|for\b.*\bdo|while\b.*\bdo|repeat\b)', kl):
                            if rdepth > 0:
                                rdepth -= 1
                            else:
                                has_unclosed_real = True
                                break
                        if ki < comment_indent:
                            break  # went to shallower indent, stop
                    # Also check forward: are there real block openers between comment and end?
                    if not has_unclosed_real:
                        for pl in pending_lines:
                            pls = pl.strip()
                            if not pls.startswith('--') and pls:
                                if re.search(r'\b(?:if\b.*\bthen|for\b.*\bdo|while\b.*\bdo|function\b)', pls):
                                    has_unclosed_real = True
                                    break
                    if not has_unclosed_real:
                        for pl in pending_lines:
                            new_lines_iffix.append(pl)
                        indent_str = lines_iffix[j][:end_indent]
                        new_lines_iffix.append(f'{indent_str}-- [orphaned end] end')
                        j += 1
                        found_end = True
                        changed_iffix = True
                    break
                elif inner == 'end' and end_indent < comment_indent:
                    # End at shallower indent → belongs to enclosing block, stop
                    break
                else:
                    pending_lines.append(lines_iffix[j])
                    j += 1
            if not found_end:
                for pl in pending_lines:
                    new_lines_iffix.append(pl)
                i += 1 + len(pending_lines)
            else:
                i = j
        else:
            new_lines_iffix.append(lines_iffix[i])
            i += 1
    if changed_iffix:
        source = '\n'.join(new_lines_iffix)
        fixes.append("Commented out orphaned end from commented-out block opener")

    source = _fix_startup_race_conditions(name, source, fixes)
    source = _inject_utility_functions(name, source, fixes)
    source = _disable_broken_scripts(name, source, fixes)

    # Remove duplicate trailing return statements (may have blank lines between)
    lines = source.rstrip().split('\n')
    returns_at_end = []
    for i in range(len(lines) - 1, max(len(lines) - 10, -1), -1):
        s = lines[i].strip()
        if s.startswith('return '):
            returns_at_end.append(i)
        elif s:  # non-empty, non-return line — stop
            break
    if len(returns_at_end) > 1:
        # Keep the first return (earliest in file), remove the rest
        for idx in returns_at_end[:-1]:  # remove all except the last (earliest)
            lines[idx] = ''
        fixes.append("Removed duplicate trailing return")
    source = '\n'.join(lines)
    if _had_trailing_newline:
        source += '\n'

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

    # Join multi-line C# ternary: `expr)\n    ? value_a\n    : value_b` → single line
    if re.search(r'^\s+\?\s+\w', source, re.MULTILINE):
        lines_tern = source.split('\n')
        new_lines_tern = []
        i_tern = 0
        while i_tern < len(lines_tern):
            line_t = lines_tern[i_tern]
            # Check if next line starts with `? ` (C# ternary continuation)
            if (i_tern + 1 < len(lines_tern)
                    and re.match(r'^\s+\?\s+', lines_tern[i_tern + 1])):
                # Join this line + ? line + : line
                joined = line_t.rstrip()
                i_tern += 1
                while i_tern < len(lines_tern):
                    cont = lines_tern[i_tern].strip()
                    if cont.startswith('?') or cont.startswith(':'):
                        joined = joined + ' ' + cont
                        i_tern += 1
                    else:
                        break
                new_lines_tern.append(joined)
            else:
                new_lines_tern.append(line_t)
                i_tern += 1
        source = '\n'.join(new_lines_tern)

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

    # Fix comment inside bracket access: `lookup[-- comment(expr)]` → `lookup[expr]`
    # Happens when StringToHash or similar was mapped to a comment inside brackets
    if re.search(r'\[-- \w+.*\(', source):
        source = re.sub(
            r'\[-- [^\n]*?\(([^)]+)\)\]',
            r'[\1]',
            source,
        )
        fixes.append("Fixed comment inside bracket access")

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
        'NavMeshData', 'NavMeshDataInstance', 'NavMeshBuildSettings',
        'NavMeshBuildMarkup', 'NavMeshBuildSource',
        'CharacterController', 'Animator', 'ParticleSystem',
        'AudioClip', 'AudioMixer', 'AudioMixerGroup',
        'Transform', 'GameObject', 'Camera',
        'Coroutine', 'WaitForSeconds', 'IEnumerator',
        'Texture2D', 'RenderTexture', 'Sprite', 'Mesh',
        'System', 'Type', 'List', 'Array', 'HashSet',
        'Dictionary', 'Queue', 'Stack',
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
    # Also handle `Type var = expr` (with assignment)
    if re.search(rf'^\s*(?:{_type_pattern})\??\s+\w+\s*=', source, re.MULTILINE):
        source = re.sub(
            rf'^(\s*)(?:{_type_pattern})\??\s+(\w+)\s*=\s*(.+)$',
            r'\1local \2 = \3',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed C# typed declarations with assignment → local")
    # Handle nullable types: `Type? var` or `Type? var = expr`
    if re.search(r'^\s*[A-Z]\w+\?\s+\w+', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)[A-Z]\w+\?\s+(\w+)\s*$',
            r'\1local \2 = nil',
            source,
            flags=re.MULTILINE,
        )
        source = re.sub(
            r'^(\s*)[A-Z]\w+\?\s+(\w+)\s*=\s*(.+)$',
            r'\1local \2 = \3',
            source,
            flags=re.MULTILINE,
        )

    # Fix C# multi-variable init on one line: `local x1 = 0, x2 = 0, y1 = 0`
    # → separate declarations: `local x1 = 0\nlocal x2 = 0\nlocal y1 = 0`
    if re.search(r'^\s*local\s+\w+\s*=\s*\S+,\s*\w+\s*=', source, re.MULTILINE):
        def _split_multi_init(m):
            indent = m.group(1)
            rest = m.group(2)
            # Split on ", varname =" pattern
            parts = re.split(r',\s*(?=\w+\s*=)', rest)
            lines = []
            for part in parts:
                part = part.strip()
                if '=' in part:
                    lines.append(f'{indent}local {part}')
                else:
                    lines.append(f'{indent}local {part} = nil')
            return '\n'.join(lines)
        source = re.sub(
            r'^(\s*)local\s+(\w+\s*=\s*\S+(?:,\s*\w+\s*=\s*\S+)+)\s*$',
            _split_multi_init,
            source,
            flags=re.MULTILINE,
        )

    # Fix C# `|=` compound bitwise OR (not valid in Luau)
    # `x |= expr` → `x = x or (expr)` (boolean context) or bit32.bor for numeric
    if '|=' in source:
        source = re.sub(
            r'(\w+)\s*\|=\s*(.+)',
            r'\1 = \1 or (\2)',
            source,
        )
        fixes.append("Fixed |= compound bitwise OR → or")

    # Fix `local function Keyframe(number, ...)` — broken constructor from new Keyframe(...)
    # Must run BEFORE default-params fix which would replace the numbers
    if re.search(r'local function Keyframe\(\d', source):
        source = re.sub(
            r'^(\s*)local function Keyframe\(\d.*$',
            lambda m: f'{m.group(1)}-- [Unity Keyframe] {m.group(0).strip()}',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out broken Keyframe constructor")

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
    # Pattern: "local varName-- TypeComment: explanation = value" → "local varName = value -- comment"
    # This happens when TYPE_MAP replaces a type in the middle of a variable name
    # e.g., m_IgnoreNavMeshAgent → m_Ignore-- NavMeshAgent: use Roblox PathfindingService
    # NOTE: only match varName ending with a letter/digit (not `m_--` which is handled separately below)
    if re.search(r'[a-zA-Z0-9]-- [A-Z]', source) or re.search(r'\w+--\s*\w[^=\n]*=\s*\w', source):
        def _fix_embedded_comment(m):
            indent = m.group(1)
            varname = m.group(2)
            comment = m.group(3)
            value = m.group(4)
            return f'{indent}local {varname} = {value} -- {comment}'
        source = re.sub(
            r'^(\s*)local\s+(\w*[a-zA-Z0-9])--\s*([^=\n]+?)\s*=\s*([^\n]+)$',
            _fix_embedded_comment,
            source,
            flags=re.MULTILINE,
        )
        # Handle: `_var-- comment: text = value` (non-local, with assignment)
        # e.g., `_agent-- isStopped: track manually = false`
        # Exclude m_-- which is handled by a separate dedicated handler below
        source = re.sub(
            r'^(\s*)(\w+[a-zA-Z0-9])--\s*\w[^=\n]*=\s*([^\n]+)$',
            r'\1\2 = \3 -- (property comment removed)',
            source,
            flags=re.MULTILINE,
        )
        # Also handle bare references in non-local context: varName-- TypeComment: → varName
        # Only uppercase type names to avoid catching lowercase properties in conditions
        source = re.sub(r'(\w*[a-zA-Z0-9])-- [A-Z]\w+:[^\n]*', r'\1', source)

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

    # Fix `_-- Comment: text` pattern (underscore + comment from method mapping)
    # e.g., `_-- PlayableDirector.Play: trigger animation sequence`
    if re.search(r'^\s+_-- ', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)_-- (.*)$',
            r'\1-- \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed _-- comment pattern")

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

    # Strip C# float/double/decimal literal suffixes: 1.0F, 2f, 3.5d, 0.1m
    # Must run BEFORE other numeric processing.
    # Two patterns to avoid corrupting Luau format specifiers (%.1f, %d):
    # 1. After decimal numbers: 1.0f, 3.5d, 0.07F
    # 2. After integers preceded by whitespace/operator: 2F, 10f (not part of %2f)
    _did_strip = False
    if re.search(r'\d\.\d+[fFdDmM]\b', source):
        source = re.sub(r'(\d\.\d+)[fFdDmM]\b', r'\1', source)
        _did_strip = True
    if re.search(r'(?<=[=\s,(*+\-/])(\d+)[fFdDmM]\b', source):
        source = re.sub(r'(?<=[=\s,(*+\-/])(\d+)[fFdDmM]\b', r'\1', source)
        _did_strip = True
    if _did_strip:
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
    # Also after keywords: if, not, or, and, while, elseif
    if re.search(r'(?:^|=|return|\()\s*:', source, re.MULTILINE):
        source = re.sub(
            r'((?:^|=|return\s|,|\()\s*):(\w+)',
            r'\1script.Parent:\2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Added 'script.Parent' receiver to bare method calls")
    # Bare ':Method()' after keywords (if, not, or, and, while, elseif)
    if re.search(r'\b(?:if|not|or|and|while|elseif)\s+:', source):
        source = re.sub(
            r'(\b(?:if|not|or|and|while|elseif)\s+):(\w+)',
            r'\1script.Parent:\2',
            source,
        )
        fixes.append("Added 'script.Parent' receiver to bare method calls after keywords")

    # Fix bare '.Property' access without a receiver (e.g., '.Position')
    if re.search(r'(?:^|=|return|\()\s*\.(?!\.)', source, re.MULTILINE):
        source = re.sub(
            r'((?:=|return\s|,|\()\s*)\.(\w+)',
            r'\1script.Parent.\2',
            source,
        )
    # Bare '.Property' after keywords (if, not, or, and, while, elseif)
    if re.search(r'\b(?:if|not|or|and|while|elseif)\s+\.[A-Za-z]', source):
        source = re.sub(
            r'(\b(?:if|not|or|and|while|elseif)\s+)\.([A-Za-z]\w*)',
            r'\1script.Parent.\2',
            source,
        )
        fixes.append("Added 'script.Parent' receiver to bare property after keyword")

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
            # If args contains `{`, it's a table literal — use table.insert (don't split on comma)
            if '{' in args:
                return f'table.insert({obj}, {args})'
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

    # Fix broken null-conditional method calls from transpiler word-boundary backtrack
    # Pattern: (obj and obj.PARTIAL or nil)REST(args) → if obj then obj:PARTIALREST(args) end
    # This happens when the property regex steals chars from the method name
    if ' or nil)' in source and re.search(r'\(\w+ and \w+\.\w+ or nil\)\w+\(', source):
        def _fix_broken_null_cond(m):
            obj = m.group(1)
            partial = m.group(2)
            rest = m.group(3)
            args = m.group(4)
            method = partial + rest
            # Invoke → Fire (Roblox event pattern)
            if method == 'Invoke':
                return f'if {obj} then {obj}:Fire({args}) end'
            return f'if {obj} then {obj}:{method}({args}) end'
        source = re.sub(
            r'\((\w+) and \1\.(\w+) or nil\)(\w+)\(([^)]*)\)',
            _fix_broken_null_cond,
            source,
        )
        fixes.append("Fixed broken null-conditional method pattern")

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

    # Fix `workspace:GetServerTimeNow()AsDouble` → `workspace:GetServerTimeNow()`
    # `.AsDouble` / `.AsFloat` / `.TotalSeconds` are C# DateTime/TimeSpan properties
    if re.search(r'GetServerTimeNow\(\)\w', source):
        source = re.sub(r'GetServerTimeNow\(\)(AsDouble|AsFloat|TotalSeconds|SinceLevelLoad)\b', 'GetServerTimeNow()', source)
        fixes.append("Fixed GetServerTimeNow()AsDouble → GetServerTimeNow()")
    # Fix `GetServerTimeNow(, ...)` → `GetServerTimeNow(), ...` (broken arg list from API replacement)
    if 'GetServerTimeNow(,' in source:
        source = source.replace('GetServerTimeNow(,', 'GetServerTimeNow(),')
        fixes.append("Fixed GetServerTimeNow(, → GetServerTimeNow(),")

    # Fix `error(new) System.XxxException("msg")` → `error("msg")`
    if 'error(new)' in source:
        source = re.sub(
            r'error\(new\)\s*System\.\w+Exception\(([^)]*)\)',
            r'error(\1)',
            source,
        )
        fixes.append("Fixed error(new) System.Exception → error()")

    # Fix C# `/* */` block comments → Luau `--[[ ]]`
    if '/*' in source:
        source = re.sub(r'/\*', '--[[', source)
        source = re.sub(r'\*/', ']]', source)
        fixes.append("Fixed C# /* */ block comments → --[[ ]]")

    # Fix `catch (System.Exception e)` after pcall → proper error handling
    if 'catch (' in source:
        source = re.sub(
            r'\)\s*\n\s*catch\s*\([^)]*\)\s*\n',
            ')\nif not ok then\n',
            source,
        )
        fixes.append("Fixed catch block → pcall error handling")

    # Fix `Math.IEEERemainder(x, y)` → `x % y` (Luau modulo)
    if 'Math.IEEERemainder' in source:
        source = re.sub(
            r'Math\.IEEERemainder\(([^,]+),\s*([^)]+)\)',
            r'(\1) % (\2)',
            source,
        )
        fixes.append("Fixed Math.IEEERemainder → modulo %")

    # Fix Unity Material API calls: mat.GetVector/GetFloat/GetColor → stub values
    # Only match material-related variable names to avoid clobbering Animator.GetFloat → GetAttribute
    _MAT_VARS = r'(?:mat|material|m_Material|m_Mat|sharedMaterial|baseMaterial)'
    if re.search(rf'{_MAT_VARS}[.:]\bGet(?:Vector|Float|Color|Int|Texture)\b\(', source):
        source = re.sub(rf'({_MAT_VARS})[.:]GetVector\([^)]*\)', 'Vector3.zero', source)
        source = re.sub(rf'({_MAT_VARS})[.:]GetFloat\([^)]*\)', '1.0', source)
        source = re.sub(rf'({_MAT_VARS})[.:]GetColor\([^)]*\)', 'Color3.new(1, 1, 1)', source)
        source = re.sub(rf'({_MAT_VARS})[.:]GetInt\([^)]*\)', '0', source)
        source = re.sub(rf'({_MAT_VARS})[.:]GetTexture\([^)]*\)', 'nil', source)
        fixes.append("Fixed Unity Material Get* API calls → stub values")

    # Join multi-line tuple assignments: `= (\nexpr,\nexpr)` → single line
    # Uses paren depth tracking to handle nested function calls like (func(a), func(b))
    lines_tup = source.split('\n')
    new_lines_tup = []
    i_tup = 0
    while i_tup < len(lines_tup):
        line_t = lines_tup[i_tup]
        # Check if line has `= (` or `return (` where parens don't balance on this line
        m_tup = re.search(r'(?:=|return)\s*\(', line_t)
        if m_tup and '-- ' not in line_t[:m_tup.start()]:
            # Count paren depth from the opening paren
            depth = 0
            for ch in line_t[m_tup.start():]:
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
            if depth > 0 and (line_t.rstrip().endswith(',') or line_t.rstrip().endswith('(')):
                # Multi-line tuple — join continuation lines until balanced
                joined = line_t
                i_tup += 1
                while i_tup < len(lines_tup) and depth > 0:
                    continuation = lines_tup[i_tup].strip()
                    joined = joined.rstrip() + ' ' + continuation
                    for ch in continuation:
                        if ch == '(':
                            depth += 1
                        elif ch == ')':
                            depth -= 1
                    i_tup += 1
                new_lines_tup.append(joined)
                continue
        new_lines_tup.append(line_t)
        i_tup += 1
    source = '\n'.join(new_lines_tup)

    # Fix bare tuple assignment: `local x = (a, b, c, d)` → `local x = {a, b, c, d}`
    # Uses balanced paren matching to handle nested function calls like (func(a), func(b))
    def _fix_tuple_line(line):
        """Convert tuple assignment/return on a single line using balanced paren matching."""
        # Match `= (` or `return (`
        m_eq = re.match(r'^(\s*)((?:local\s+)?\w+(?:\.\w+)*)\s*=\s*\(', line)
        m_ret = re.match(r'^(\s*)return\s*\(', line) if not m_eq else None
        if not m_eq and not m_ret:
            return line
        m = m_eq or m_ret
        # Find the opening paren position
        start = line.index('(', m.start())
        # Walk to find the matching close paren
        depth = 0
        has_comma_at_top = False
        for i in range(start, len(line)):
            ch = line[i]
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    contents = line[start + 1:i]
                    # Check for commas at top-level depth
                    d2 = 0
                    for c2 in contents:
                        if c2 == '(':
                            d2 += 1
                        elif c2 == ')':
                            d2 -= 1
                        elif c2 == ',' and d2 == 0:
                            has_comma_at_top = True
                            break
                    if has_comma_at_top:
                        # Replace outer parens with braces
                        return line[:start] + '{' + contents + '}' + line[i + 1:]
                    break
            elif ch == ',':
                if depth == 1:
                    has_comma_at_top = True
        return line
    new_lines_tuple = []
    for line in source.split('\n'):
        stripped = line.strip()
        if ('= (' in line or stripped.startswith('return (')) and ',' in line:
            line = _fix_tuple_line(line)
        new_lines_tuple.append(line)
    source = '\n'.join(new_lines_tuple)

    # Fix undefined `col` variable → `otherPart` (C# `Collision col` parameter)
    # Only when: (1) `col` is not locally defined, (2) there's a Touched handler in the script,
    # (3) `col` is not a function parameter
    if (re.search(r'\bcol\b(?=[\.:])(?!.*--)', source)
            and 'local col' not in source
            and 'Touched' in source
            and not re.search(r'function\s*\([^)]*\bcol\b[^)]*\)', source)):
        source = re.sub(r'\bcol\b(?=[\.:])(?!.*--)', 'otherPart', source)
        fixes.append("Fixed undefined 'col' variable → 'otherPart'")

    # Fix `--variable` prefix decrement that becomes a comment in Luau
    # Pattern: `--varName` at start of line (standalone) — this is a prefix decrement, not a comment
    # But only if it matches a known pattern like `--zoomSelector` (preceded by `++zoomSelector`)
    if re.search(r'^\s*--(\w+)\s*$', source, re.MULTILINE):
        # Check if there's a corresponding `++var` pattern nearby (confirms it's a pre-decrement)
        lines = source.split('\n')
        new_lines = []
        for line in lines:
            m = re.match(r'^(\s*)--(\w+)\s*$', line)
            if m and f'++{m.group(2)}' in source:
                # This is a pre-decrement, not a comment
                new_lines.append(f'{m.group(1)}{m.group(2)} = {m.group(2)} - 1')
            else:
                new_lines.append(line)
        if new_lines != lines:
            source = '\n'.join(new_lines)
            fixes.append("Fixed --variable prefix decrement (was parsed as comment)")

    # Fix `: base(...)` C# constructor chaining → comment out
    if re.search(r'^\s*:\s*base\s*\(', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*):\s*base\s*\([^)]*\)\s*$',
            r'\1-- [C#] base constructor call removed',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out C# base() constructor calls")

    # Fix `for (; ;)` → `while true do` (C# infinite loop)
    if 'for (; ;' in source or 'for (;;' in source:
        source = re.sub(r'for\s*\(\s*;\s*;\s*\)', 'while true do', source)
        fixes.append("Fixed for(;;) → while true do")

    # Fix `nil` as function parameter name → `_param`
    if re.search(r'function\s+\w+\(nil\)', source) or re.search(r'function\s*\(nil\)', source):
        source = re.sub(r'function\s+(\w+)\(nil\)', r'function \1(_param)', source)
        source = re.sub(r'function\s*\(nil\)', r'function(_param)', source)
        fixes.append("Fixed nil as function parameter name → _param")

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
    # :Magnitude() → .Magnitude (property, not method)
    if ':Magnitude()' in source:
        source = source.replace(':Magnitude()', '.Magnitude')
        fixes.append("Fixed :Magnitude() → .Magnitude (property, not method)")
    # .Magnitude() → .Magnitude (not a function call)
    if '.Magnitude()' in source:
        source = source.replace('.Magnitude()', '.Magnitude')
        fixes.append("Fixed .Magnitude() → .Magnitude (property, not method)")
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
    # Also handle StringToHash(variable) → variable
    if "StringToHash" in source:
        source = re.sub(
            r'Animator\.StringToHash\(\s*("(?:[^"\\]|\\.)*")\s*\)',
            r'\1',
            source,
        )
        # Handle non-literal args: StringToHash(expr) → expr
        source = re.sub(
            r'Animator\.StringToHash\(([^)]+)\)',
            r'\1',
            source,
        )
        # Also handle bare StringToHash without Animator prefix
        source = re.sub(
            r'(?<!\w)StringToHash\(([^)]+)\)',
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

    # Fix: `new -- Comment (...)` → `nil -- Comment` (type was replaced by comment)
    if 'new --' in source:
        source = re.sub(r'\bnew\s+(-- [^\n]*?)\s*\([^)]*\)', r'nil \1', source)

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
        # obj.attachedRigidbody, arr[n].attachedRigidbody → obj, arr[n]
        source = re.sub(r'([\w.\[\]]+)\.attachedRigidbody\b', r'\1', source)
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

    # Fix: FindFirstChildWhichIsA("Rigidbody") → part itself (Roblox parts are their own physics)
    # e.g. obj:FindFirstChildWhichIsA("Rigidbody").Anchored = true → obj.Anchored = true
    if 'FindFirstChildWhichIsA("Rigidbody")' in source:
        source = source.replace(
            ':FindFirstChildWhichIsA("Rigidbody")',
            ''
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
        # Pattern: workspace:Raycast(ray, hit, range) → workspace:Raycast(ray.Origin, ray.Direction * range)
        # But skip if the second arg is 'rayParams' or ends with 'Params' (already Roblox format)
        def _fix_raycast_3arg(m):
            obj, a, b, c = m.group(1), m.group(2), m.group(3), m.group(4)
            if b.endswith('Params') or b == 'rayParams':
                return m.group(0)  # Already Roblox format
            return f'{obj}:Raycast({a}.Origin, {a}.Direction * {c})'
        source = re.sub(
            r'(\w+):Raycast\((\w+),\s*(\w+),\s*(\w+)\)',
            _fix_raycast_3arg,
            source,
        )
        # Pattern: workspace:Raycast(origin, direction, hit, range) → workspace:Raycast(origin, direction * range)
        source = re.sub(
            r'(\w+):Raycast\(([^,]+),\s*([^,]+),\s*(\w+),\s*(\w+)\)',
            r'\1:Raycast(\2, \3 * \5)',
            source,
        )
        # Fix broken pattern: Raycast(x.Origin, x.Direction * params)
        # where x is a Vector3 (not a Ray) — the .Origin/.Direction fields don't exist
        # Look for a 'direction' variable defined nearby and use it
        def _fix_broken_ray_fields(m):
            full = m.group(0)
            var = m.group(1)
            params = m.group(2)
            # Check if 'direction' is defined in the source
            if re.search(r'\blocal\s+direction\b', source):
                return f':Raycast({var}, direction, {params})'
            return full  # Can't fix safely
        source = re.sub(
            r':Raycast\((\w+)\.Origin,\s*\1\.Direction\s*\*\s*(\w+)\)',
            _fix_broken_ray_fields,
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

    # Pre-process: join multi-line C# for-loops into single lines
    # Pattern: `for (` without closing `)` → join with next lines until `)` found
    if 'for (' in source:
        lines_ml = source.split('\n')
        new_lines_ml = []
        i_ml = 0
        while i_ml < len(lines_ml):
            line = lines_ml[i_ml]
            if re.match(r'^\s*for\s*\(', line) and ')' not in line:
                # Join subsequent lines until we find the closing )
                joined = line.rstrip()
                for j_ml in range(i_ml + 1, min(i_ml + 5, len(lines_ml))):
                    joined += ' ' + lines_ml[j_ml].strip()
                    if ')' in lines_ml[j_ml]:
                        i_ml = j_ml
                        break
                new_lines_ml.append(joined)
            else:
                new_lines_ml.append(line)
            i_ml += 1
        source = '\n'.join(new_lines_ml)

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

        # Match both prefix ++i and postfix i++
        source = re.sub(
            r'^(\s*)for\s*\(\s*local\s+(\w+)\s*=\s*(\w+)\s*;\s*\w+\s*<\s*([^;]+?)\s*;\s*(?:\+\+\w+|\w+\+\+)\s*\)',
            _fix_for_loop,
            source,
            flags=re.MULTILINE,
        )
        # Also handle <= bound: for (local i = 0; i <= N; ++i/i++) → for i = 0, N do
        def _fix_for_loop_le(m: re.Match) -> str:
            indent = m.group(1)
            var = m.group(2)
            start = m.group(3)
            bound = m.group(4)
            if bound.endswith('#'):
                bound = '#' + bound[:-1]
            return f'{indent}for {var} = {start}, {bound} do'
        source = re.sub(
            r'^(\s*)for\s*\(\s*local\s+(\w+)\s*=\s*(\w+)\s*;\s*\w+\s*<=\s*([^;]+?)\s*;\s*(?:\+\+\w+|\w+\+\+)\s*\)',
            _fix_for_loop_le,
            source,
            flags=re.MULTILINE,
        )
        # Handle decrementing: for (local i = N; i >= 0; i--/--i) → for i = N, 0, -1 do
        def _fix_for_loop_dec(m: re.Match) -> str:
            indent = m.group(1)
            var = m.group(2)
            start = m.group(3)
            bound = m.group(4)
            return f'{indent}for {var} = {start}, {bound}, -1 do'
        source = re.sub(
            r'^(\s*)for\s*\(\s*local\s+(\w+)\s*=\s*([^;]+?)\s*;\s*\w+\s*>=\s*([^;]+?)\s*;\s*(?:--\w+|\w+--)\s*\)',
            _fix_for_loop_dec,
            source,
            flags=re.MULTILINE,
        )
        # Handle for(;;) → while true do (infinite loop, already in csharp_remnants but ensure it's caught here too)
        source = re.sub(r'for\s*\(\s*;\s*;\s*\)', 'while true do', source)

        # Handle decrementing without 'local': for (i = N; i > 0; i--) → for i = N, 1, -1 do
        source = re.sub(
            r'^(\s*)for\s*\(\s*(\w+)\s*=\s*([^;]+?)\s*;\s*\w+\s*>\s*([^;]+?)\s*;\s*(?:--\w+|\w+--)\s*\)',
            lambda m: f'{m.group(1)}for {m.group(2)} = {m.group(3)}, {m.group(4)} + 1, -1 do',
            source,
            flags=re.MULTILINE,
        )

        # Handle custom step: for (local i = 0; i < N; i = i + step) → for i = 0, N - 1, step do
        def _fix_for_loop_custom_step(m):
            indent = m.group(1)
            var = m.group(2)
            start = m.group(3)
            bound = m.group(4)
            step = m.group(5)
            if bound.endswith('#'):
                bound = '#' + bound[:-1]
            return f'{indent}for {var} = {start}, {bound} - 1, {step} do'
        source = re.sub(
            r'^(\s*)for\s*\(\s*(?:local\s+)?(\w+)\s*=\s*([^;]+?)\s*;\s*\w+\s*<\s*([^;]+?)\s*;\s*\w+\s*=\s*\w+\s*\+\s*([^)]+?)\s*\)',
            _fix_for_loop_custom_step,
            source,
            flags=re.MULTILINE,
        )

        # Handle decrementing with custom step: for (local i = N; i >= 0; i = i - step) → for i = N, 0, -step do
        source = re.sub(
            r'^(\s*)for\s*\(\s*(?:local\s+)?(\w+)\s*=\s*([^;]+?)\s*;\s*\w+\s*>=\s*([^;]+?)\s*;\s*\w+\s*=\s*\w+\s*-\s*([^)]+?)\s*\)',
            lambda m: f'{m.group(1)}for {m.group(2)} = {m.group(3)}, {m.group(4)}, -{m.group(5)} do',
            source,
            flags=re.MULTILINE,
        )

        # Fallback: any remaining `for (...)` that wasn't matched — convert to while loop
        # for (init; cond; incr) → init \n while cond do \n ... incr \n end
        # This is a last resort for complex patterns
        if re.search(r'^\s*for\s*\(', source, re.MULTILINE):
            def _fix_for_to_while(m):
                indent = m.group(1)
                init = m.group(2).strip()
                cond = m.group(3).strip()
                incr = m.group(4).strip()
                # Remove 'local' from init if present (already handled)
                init = re.sub(r'^local\s+', '', init)
                # Fix C# conditions
                cond = cond.replace('!=', '~=')
                # Build while loop
                if init:
                    return f'{indent}local {init}\n{indent}while {cond} do'
                return f'{indent}while {cond} do'
            source = re.sub(
                r'^(\s*)for\s*\(\s*([^;]*?)\s*;\s*([^;]*?)\s*;\s*([^)]*?)\s*\)',
                _fix_for_to_while,
                source,
                flags=re.MULTILINE,
            )
            fixes.append("Converted remaining C# for-loops to while loops")

        # Final fallback: comment out any remaining `for (...)` that couldn't be converted
        if re.search(r'^\s*for\s*\(', source, re.MULTILINE):
            source = re.sub(
                r'^(\s*)for\s*\([^)]*\)',
                r'\1-- [C# for-loop] while true do -- TODO: manual conversion needed',
                source,
                flags=re.MULTILINE,
            )
            fixes.append("Commented out unparseable C# for-loops")

    # Fix C# sizeof() → literal values
    if 'sizeof(' in source:
        source = re.sub(r'\bsizeof\(int\)', '4', source)
        source = re.sub(r'\bsizeof\(float\)', '4', source)
        source = re.sub(r'\bsizeof\(double\)', '8', source)
        source = re.sub(r'\bsizeof\(byte\)', '1', source)
        source = re.sub(r'\bsizeof\(short\)', '2', source)
        source = re.sub(r'\bsizeof\(long\)', '8', source)
        source = re.sub(r'\bsizeof\(\w+\)', '4', source)  # fallback
        fixes.append("Fixed sizeof() → literal values")

    # Fix C# && → and (Luau boolean operator)
    if '&&' in source:
        source = re.sub(r'&&', ' and ', source)
        fixes.append("Fixed C# && → and")

    # Fix C# || → or (Luau boolean operator)
    if '||' in source:
        source = re.sub(r'\|\|', ' or ', source)
        fixes.append("Fixed C# || → or")

    # Fix C# !(expr) → not (expr) (Luau negation)
    if '!(' in source:
        source = re.sub(r'!\(', 'not (', source)
        fixes.append("Fixed C# !(expr) → not (expr)")

    # Fix: semicolons at end of statements
    if ";" in source:
        # Remove trailing semicolons but not within strings
        lines = source.split("\n")
        result = []
        for line in lines:
            s = line.rstrip()
            if not s.strip().startswith("--"):
                # Don't modify inside string literals
                quote_count = s.count('"') - s.count('\\"')
                if quote_count % 2 == 0:  # Not inside a string
                    # Handle "stmt;    }" pattern — strip semicollon + trailing brace
                    m_semi_brace = re.match(r'^(.*\S)\s*;\s*\}\s*$', s)
                    if m_semi_brace:
                        s = m_semi_brace.group(1)
                    elif s.endswith(";"):
                        s = s[:-1].rstrip()
                    # Strip trailing colon from broken ternary: `expr() :` → `expr()`
                    if s.rstrip().endswith(' :') or s.rstrip().endswith('\t:'):
                        s = s.rstrip()[:-1].rstrip()
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
        "PlayRandomClip", "ApplyImpulse", "ApplyImpulseAtPosition",
        "GetPartBoundsInRadius", "GetPartBoundsInBox",
        "PointToObjectSpace", "PointToWorldSpace",
        "FindFirstDescendant", "HasTag",
    ]
    for method in _DOT_TO_COLON_METHODS:
        if f'.{method}' in source:
            # Fix when preceded by a word character or closing paren/bracket (receiver)
            # Allow optional whitespace before `(` to catch `.Method (` patterns
            source = re.sub(
                rf'([\w\)\]])\.\b{method}\b\s*\(',
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

    # Reposition the WeaponSlot to a typical FPS hand position so the
    # equipped weapon doesn't clip into the camera. Default (0, 0.5, -1)
    # puts it dead center in front of the head.
    if 'CFrame.new(0, 0.5, -1)' in source and 'weaponslot' in source.lower():
        source = source.replace(
            'CFrame.new(0, 0.5, -1)',
            'CFrame.new(0.6, -0.4, -2.0)',
        )
        fixes.append("Repositioned WeaponSlot for FPS view (right, down, forward)")

    # MonoBehaviour serialized fields transpile to `script:GetAttribute("X")`,
    # but the converter writes those attributes on the host Part/Model, not on
    # the Script instance. Walk up from the script to find the attribute.
    #
    # Only rewrite reads that look like serialized-field resolution at the top
    # of the script: `local VAR = script:GetAttribute("NAME")` or the same
    # with an `or DEFAULT` fallback. Script-local marker attributes set with
    # `script:SetAttribute("_X", ...)` earlier in the same file are NOT
    # rewritten — they're legitimate self-state and the walk-up would silently
    # redirect them to a parent.
    self_set_pattern = re.compile(r'script:SetAttribute\("([^"]+)"')
    self_set_names = set(self_set_pattern.findall(source))

    def _replace_field_read(match: re.Match) -> str:
        attr_name = match.group(1).strip('"')
        if attr_name in self_set_names:
            return match.group(0)
        return (
            f'(function(_n) local _o = script.Parent while _o do '
            f'local _v = _o:GetAttribute(_n) if _v ~= nil then return _v end '
            f'_o = _o.Parent end return nil end)("{attr_name}")'
        )

    field_read_pattern = re.compile(
        r'^(\s*local\s+\w+\s*=\s*)script:GetAttribute\(("[^"]+")\)',
        re.MULTILINE,
    )
    if field_read_pattern.search(source):
        source = field_read_pattern.sub(
            lambda m: m.group(1) + _replace_field_read(re.match(r'"([^"]+)"', m.group(2))),
            source,
        )
        fixes.append("Rewrote script:GetAttribute top-level field reads → walk-up lookup")

    # Pickup script: delay Destroy() so the player has time to clone the
    # picked-up item before it disappears. The Player script listens to
    # an attribute and runs async, so an immediate Destroy makes the
    # source unavailable.
    if 'character:SetAttribute("GetItem"' in source and 'script.Parent:Destroy()' in source:
        source = source.replace(
            '-- Send item to player\n\tcharacter:SetAttribute("GetItem", itemName)\n\n\t-- Destroy pickup\n\tscript.Parent:Destroy()',
            '-- Send item to player\n\tcharacter:SetAttribute("GetItem", itemName)\n\n\t-- Delay destroy so player can clone before the item disappears\n\ttask.delay(0.5, function() if script and script.Parent then script.Parent:Destroy() end end)',
        )
        fixes.append("Delayed Pickup Destroy() so player can clone item first")

    # Pickup character resolution: walk up the hierarchy to find the
    # character Model, not just otherPart.Parent (which fails for
    # accessory/hat sub-parts).
    if 'local character = otherPart.Parent' in source and 'FindFirstChildWhichIsA("Humanoid")' in source:
        source = source.replace(
            'local character = otherPart.Parent\n\tlocal humanoid = character and character:FindFirstChildWhichIsA("Humanoid")',
            'local character = otherPart:FindFirstAncestorOfClass("Model")\n\twhile character and not character:FindFirstChildWhichIsA("Humanoid") do\n\t\tcharacter = character:FindFirstAncestorOfClass("Model")\n\tend\n\tlocal humanoid = character and character:FindFirstChildWhichIsA("Humanoid")',
        )
        fixes.append("Fixed Pickup character resolution to walk up hierarchy")

    # Pickup → Player communication via RemoteEvent + attribute fallback.
    # Create the RemoteEvent at script load time (outside the Touched handler)
    # so the client's WaitForChild resolves before the first touch. Also add
    # a `_fired` debounce so the server only fires once per pickup even while
    # `Touched` spams during the 0.5s destroy-delay window (character overlaps
    # the trigger for multiple physics steps).
    if 'character:SetAttribute("GetItem"' in source and 'GetPlayerFromCharacter' in source:
        if '_PICKUP_REMOTE_INIT' not in source:
            init_block = (
                '-- _PICKUP_REMOTE_INIT\n'
                'local _RS = game:GetService("ReplicatedStorage")\n'
                'local _re = _RS:FindFirstChild("ItemPickupEvent")\n'
                'if not _re then\n'
                '\t_re = Instance.new("RemoteEvent")\n'
                '\t_re.Name = "ItemPickupEvent"\n'
                '\t_re.Parent = _RS\n'
                'end\n'
                'local _fired = false\n\n'
            )
            source = init_block + source
        source = source.replace(
            '-- Send item to player\n\tcharacter:SetAttribute("GetItem", itemName)',
            '-- Send item to player via RemoteEvent + attribute (both for reliability)\n\tif _fired then return end\n\t_fired = true\n\tprint("[Pickup] firing", itemName, "to", player.Name)\n\t_re:FireClient(player, itemName)\n\tcharacter:SetAttribute("GetItem", itemName)',
        )
        fixes.append("Pickup uses RemoteEvent + attribute with Touched debounce")

    # Player listener: also listen on RemoteEvent for item pickups.
    # Use a global function reference so it works regardless of injection point.
    if 'GetAttributeChangedSignal("GetItem")' in source and '_REMOTE_PICKUP_LISTENER' not in source:
        # Inject RemoteEvent listener that calls getItem.
        # Use _G or rawget to allow late-binding to getItem.
        injection = (
            '\n-- _REMOTE_PICKUP_LISTENER\n'
            'task.spawn(function()\n'
            '    local _re = game:GetService("ReplicatedStorage"):WaitForChild("ItemPickupEvent")\n'
            '    print("[Player] RemoteEvent listener connected")\n'
            '    _re.OnClientEvent:Connect(function(itemName)\n'
            '        print("[Player] OnClientEvent received:", itemName)\n'
            '        if getItem then\n'
            '            getItem(itemName)\n'
            '        else\n'
            '            print("[Player] getItem is nil!")\n'
            '        end\n'
            '    end)\n'
            'end)\n'
        )
        # Insert AFTER the first GetAttributeChangedSignal line so getItem
        # is already in scope (it's declared above the listener).
        idx = source.find('character:GetAttributeChangedSignal("GetItem")')
        if idx >= 0:
            # Find end of the listener block (look for "end)" after idx)
            end_idx = source.find('end)\n', idx)
            if end_idx >= 0:
                end_idx += len('end)\n')
                source = source[:end_idx] + injection + source[end_idx:]
                fixes.append("Added RemoteEvent listener for item pickups")

    # Player.luau's setupSounds() walks `script.Parent` for Sound descendants,
    # but when the script is a ModuleScript in ReplicatedStorage the bound Part
    # (holding the Unity-serialized AudioClip sounds as Sound children) lives
    # under Workspace, not under script.Parent. Broaden the search so it also
    # scans workspace for a matching host Part.
    if 'local function setupSounds()' in source and '_SETUP_SOUNDS_BROAD' not in source:
        source = source.replace(
            'local function setupSounds()\n    local parent = script.Parent',
            'local function setupSounds()\n    -- _SETUP_SOUNDS_BROAD: also search Workspace for the bound host Part\n    local parent = script.Parent\n    if parent and not parent:FindFirstChildWhichIsA("Sound", true) then\n        for _, _cand in ipairs(workspace:GetDescendants()) do\n            if _cand:IsA("BasePart") and _cand.Name == "Player" and _cand:FindFirstChildWhichIsA("Sound") then\n                parent = _cand\n                break\n            end\n        end\n    end',
        )
        fixes.append("Broadened setupSounds() to search Workspace for the host Part's Sound children")

    # Shoot: remove the redundant _isMouseButtonDown early-exit. shoot() is
    # called from an InputBegan(MouseButton1) handler, so the polling check
    # races and returns false, preventing any shots from ever firing.
    if 'if not _isMouseButtonDown(Enum.UserInputType.MouseButton1) then return end' in source:
        source = source.replace(
            '    if not _isMouseButtonDown(Enum.UserInputType.MouseButton1) then return end\n',
            '',
        )
        fixes.append("Removed _isMouseButtonDown early-exit in shoot() (called from InputBegan)")

    # getRifle: if the pickup Touched fires repeatedly before the script
    # destroys itself, the client equips many duplicate rifles. Short-circuit
    # if a weapon was already equipped this life.
    if 'local function getRifle()' in source and 'if gotWeapon then return end' not in source:
        source = source.replace(
            'local function getRifle()\n',
            'local function getRifle()\n    if gotWeapon then return end\n',
        )
        fixes.append("getRifle early-return when gotWeapon already true (prevents duplicate equip)")

    # When script looks up "riflePrefab" (Unity field reference) and the
    # Model wasn't created (no mesh_hierarchies during conversion), fall
    # back to searching for the rifle Model anywhere in workspace.
    # Idempotent: skip if the wrapper function is already present.
    if ('FindFirstChild("riflePrefab"' in source
            and '_RIFLE_LOOKUP_WRAPPED' not in source):
        source = source.replace(
            'workspace:FindFirstChild("riflePrefab", true)',
            '(function() -- _RIFLE_LOOKUP_WRAPPED\n'
            '    local _rp = workspace:FindFirstChild("riflePrefab", true)\n'
            '    if _rp then return _rp end\n'
            '    for _, _d in workspace:GetDescendants() do\n'
            '        if _d.Name == "Rifle" and _d:IsA("Model") then return _d end\n'
            '    end\n'
            '    return nil\n'
            'end)()',
        )
        fixes.append("Added Rifle Model fallback for missing riflePrefab field reference")

    # Fix incomplete Model equip patterns: when a Model is cloned and
    # parented to a Part (weapon slot), its children are unanchored at
    # (0,0,0) and don't follow the parent. Inject proper PivotTo + Weld.
    if 'rifle:IsA("Model")' in source and 'rifle:ScaleTo' in source:
        # ScaleTo MUST be called BEFORE welding, otherwise welds capture
        # the unscaled positions and prevent the scale from taking effect.
        source = re.sub(
            r'(\s*)elseif\s+rifle:IsA\("Model"\)\s+then\s*\n(\s*)rifle:ScaleTo\([^)]+\)\s*\n\s*end',
            (r'\1elseif rifle:IsA("Model") then\n'
             r'\2-- Set primary part for positioning\n'
             r'\2if not rifle.PrimaryPart then\n'
             r'\2    rifle.PrimaryPart = rifle:FindFirstChildWhichIsA("BasePart")\n'
             r'\2end\n'
             r'\2-- Unanchor parts first so PivotTo/ScaleTo can move them\n'
             r'\2for _, _p in rifle:GetDescendants() do\n'
             r'\2    if _p:IsA("BasePart") then\n'
             r'\2        _p.Anchored = false\n'
             r'\2        _p.CanCollide = false\n'
             r'\2        _p.Massless = true\n'
             r'\2    end\n'
             r'\2end\n'
             r'\2-- Scale BEFORE welding (welds would freeze positions)\n'
             r'\2rifle:ScaleTo(0.2)\n'
             r'\2-- Move to weapon slot then weld each part\n'
             r'\2rifle:PivotTo(weaponSlot.CFrame)\n'
             r'\2for _, _p in rifle:GetDescendants() do\n'
             r'\2    if _p:IsA("BasePart") then\n'
             r'\2        local _w = Instance.new("WeldConstraint")\n'
             r'\2        _w.Part0 = weaponSlot\n'
             r'\2        _w.Part1 = _p\n'
             r'\2        _w.Parent = _p\n'
             r'\2        -- Force visible (bootstrap excludes WeaponSlot from hide)\n'
             r'\2        _p.LocalTransparencyModifier = 0\n'
             r'\2    end\n'
             r'\2end\n'
             r'\1end'),
            source,
        )
        fixes.append("Fixed Model equip: ScaleTo before WeldConstraints")

    # Auto-create ClickDetector when scripts reference it.
    # Unity OnMouseDown/OnMouseEnter are lifecycle events on any collider, but
    # Roblox requires an explicit ClickDetector child instance.
    if 'ClickDetector' in source and 'Instance.new("ClickDetector")' not in source:
        # Inject ClickDetector creation at top of script
        inject = ('local ClickDetector = script.Parent:FindFirstChildOfClass("ClickDetector")\n'
                  'if not ClickDetector then\n'
                  '    ClickDetector = Instance.new("ClickDetector")\n'
                  '    ClickDetector.Parent = script.Parent\n'
                  'end\n')
        # Insert after the last local/require/GetService line
        lines = source.split('\n')
        insert_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()
            if (stripped.startswith('local ') or stripped.startswith('--') or
                    stripped == '' or 'GetService' in stripped or 'require(' in stripped):
                insert_idx = i + 1
            else:
                break
        lines.insert(insert_idx, inject)
        source = '\n'.join(lines)
        fixes.append("Auto-created ClickDetector for OnMouseDown/OnMouseEnter events")

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
        # Handle bare receiver: line starting with `.CFrame.Position =`
        source = re.sub(
            r'^(\s*)\.CFrame\.Position\s*=',
            r'\1script.Parent.Position =',
            source,
            flags=re.MULTILINE,
        )
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

    # InvokeRepeating("method", delay, interval) → task.spawn repeating loop
    if 'InvokeRepeating(' in source:
        def _fix_invoke_repeating(m):
            indent = m.group(1) or ''
            receiver = m.group(2) or ''
            args = m.group(3)
            # Try to parse args: "methodName", delay, interval
            parts = [p.strip() for p in args.split(',')]
            if len(parts) >= 3:
                method = parts[0].strip('"').strip("'")
                delay = parts[1]
                interval = parts[2]
                return (f'{indent}task.spawn(function()\n'
                        f'{indent}    task.wait({delay})\n'
                        f'{indent}    while true do\n'
                        f'{indent}        if {receiver or "script.Parent"} and {receiver or "script.Parent"}.Parent then\n'
                        f'{indent}            -- {method}()\n'
                        f'{indent}        end\n'
                        f'{indent}        task.wait({interval})\n'
                        f'{indent}    end\n'
                        f'{indent}end)')
            return m.group(0)
        source = re.sub(
            r'^(\s*)(?:(\w+(?:\.\w+)*)[\.:])?\s*InvokeRepeating\(([^)]+)\)',
            _fix_invoke_repeating,
            source,
            flags=re.MULTILINE,
        )
        # Also handle bare -- InvokeRepeating: comment pattern (from prior conversions)
        source = re.sub(
            r'-- InvokeRepeating: use task\.spawn with while loop and task\.wait"([^"]*)"',
            r'-- InvokeRepeating: \1 (use task.spawn loop)',
            source,
        )
        fixes.append("Fixed InvokeRepeating → task.spawn loop")

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
        'Collider': 'BasePart',
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

    # Fix `(if ... then ... else ... end)` → `(if ... then ... else ...)`
    # Luau if-expressions don't use `end`
    if re.search(r'\(if\b.+\belse\b.+\bend\)', source):
        source = re.sub(r'\(if\b(.+?)\belse\b(.+?)\s+end\)', r'(if\1else\2)', source)
        fixes.append("Removed `end` from if-expression (ternary)")

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
    # Fix `if (expr) -- comment then` → `if (expr) then -- comment`
    if re.search(r'if\s+\(.+\)\s+--\s+.+\s+then\s*$', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*if\s+\(.+?\))\s+--(.*?)\s+then\s*$',
            r'\1 then --\2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed if-condition with comment before then")

    # Fix unbalanced parens in if-conditions: `if ((expr) then` → `if (expr) then`
    # Count open/close parens between 'if' and 'then' and remove excess opening parens
    new_lines_pbal = []
    for line in source.split('\n'):
        stripped = line.strip()
        if stripped.startswith('if ') and ' then' in stripped:
            # Extract the condition part between 'if' and 'then'
            m_pbal = re.match(r'^(\s*if\s+)(.*?)(\s+then.*)$', line)
            if m_pbal:
                cond = m_pbal.group(2)
                opens = cond.count('(')
                closes = cond.count(')')
                # Remove excess leading '(' to balance
                while opens > closes and cond.startswith('('):
                    cond = cond[1:]
                    opens -= 1
                if opens != m_pbal.group(2).count('('):
                    line = m_pbal.group(1) + cond + m_pbal.group(3)
        new_lines_pbal.append(line)
    source = '\n'.join(new_lines_pbal)

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

    # Fix .RemoveAll(predicate) → filter in-place
    # C# List<T>.RemoveAll takes a Predicate<T> — complex to convert, comment out
    if '.RemoveAll(' in source:
        source = re.sub(
            r'^(\s*)(.+)\.RemoveAll\(.*$',
            r'\1-- [C# RemoveAll] \2.RemoveAll(...)',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out .RemoveAll() (C# List predicate)")

    # Fix .RemoveRange(start, count) → loop with table.remove
    if '.RemoveRange(' in source:
        source = re.sub(
            r'(\w+)\.RemoveRange\(([^,]+),\s*([^)]+)\)',
            r'for _i = 1, \3 do table.remove(\1, \2 + 1) end -- RemoveRange',
            source,
        )
        fixes.append("Fixed .RemoveRange() → table.remove loop")

    # Fix AnimationCurve.* → commented out (Unity-specific, no Roblox equivalent)
    if 'AnimationCurve.' in source:
        source = re.sub(
            r'^(\s*)(local\s+\w+\s*=\s*)?AnimationCurve\.\w+\([^)]*\)\s*$',
            lambda m: f'{m.group(1)}-- [Unity AnimationCurve] {m.group(0).strip()}',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out AnimationCurve calls")


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
    # General C# named arguments: `, name: value` or `(name: value` → strip name:
    if re.search(r'[,(]\s*[a-z]\w*:\s+\w', source):
        def _strip_named_arg(line):
            s = line.strip()
            if s.startswith('--') or '= {' in line or s.startswith('{'):
                return line
            return re.sub(r'([,(]\s*)\b([a-z]\w*):\s+', r'\1', line)
        source = '\n'.join(_strip_named_arg(l) for l in source.split('\n'))
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

    # Comment out Unity rendering APIs (GL, Graphics, Shader.*, RenderTexture)
    # These have no Roblox equivalent
    if re.search(r'\bGL\.\w+|Graphics\.Draw|Shader\.\w+|RenderTexture\.\w+', source):
        source = re.sub(
            r'^(\s*)(?!--)(.+?(?:\bGL\.\w+|Graphics\.Draw\w+|Shader\.\w+|RenderTexture\.\w+).*)$',
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

    # Comment out Unity camera rendering properties (not in Roblox)
    _UNITY_CAMERA_PROPS = (
        'projectionMatrix', 'worldToCameraMatrix', 'cullingMask', 'targetTexture',
        'depthTextureMode', 'renderingPath', 'clearFlags', 'ResetWorldToCameraMatrix',
        'CalculateObliqueMatrix', 'pixelRect', 'aspect',
    )
    for prop in _UNITY_CAMERA_PROPS:
        if prop in source:
            # Comment out lines with these properties (assignment or access)
            source = re.sub(
                rf'^(\s*)(?!--)(.+\.{prop}\b.*)$',
                rf'\1-- [Unity camera] \2',
                source,
                flags=re.MULTILINE,
            )
    if 'CameraClearFlags' in source:
        source = re.sub(
            r'^(\s*)(?!--)(.+CameraClearFlags\.\w+.*)$',
            r'\1-- [Unity camera] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out Unity camera rendering properties")

    # Fix comment-embedded property access: obj-- comment: text → comment/fix the whole line
    # Pattern: obj:FindFirstChildWhichIsA("BasePart")-- sharedMaterial: ...SetTexture(...)
    if '-- sharedMaterial' in source:
        source = re.sub(
            r'^(\s*)(.+-- sharedMaterial:.+)$',
            r'\1-- [Unity material] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out sharedMaterial access lines")

    # Fix inline `-- materials: use SurfaceAppearance` comment breaking expressions
    # Pattern: `renderer-- materials: use SurfaceAppearance` or `renderer-- materials: use SurfaceAppearance[1]`
    # These come from API_CALL_MAP mapping `.materials` → comment
    if '-- materials:' in source:
        # Assignment: `var = expr-- materials:...` → comment whole line
        source = re.sub(
            r'^(\s*)(\S.+)-- materials: use SurfaceAppearance(\[?\d*\]?)(.*)$',
            r'\1-- [Unity materials] \2\4',
            source,
            flags=re.MULTILINE,
        )
        # for-in loops: `for _, x in expr-- materials:... do` → comment whole line
        source = re.sub(
            r'^(\s*for\b.+)-- materials:[^\n]+$',
            r'\1-- [Unity materials] (commented out)',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed inline -- materials: comment breaking expressions")

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

    # Fix Vector3 lowercase component access: .x → .X, .y → .Y, .z → .Z
    # Roblox Vector3/CFrame uses uppercase .X .Y .Z
    # Only convert when preceded by word char (not `max.y` style false positives)
    # Skip common false positives: .xy, .xyz, text, index, hex patterns
    if re.search(r'\w\.[xyz]\b', source):
        # Fix obj.x/y/z at end of line or before operators/close-parens
        source = re.sub(r'(\w)\.(x)\b(?!\w)', r'\1.X', source)
        source = re.sub(r'(\w)\.(y)\b(?!\w)', r'\1.Y', source)
        source = re.sub(r'(\w)\.(z)\b(?!\w)', r'\1.Z', source)
        fixes.append("Fixed lowercase .x/.y/.z → .X/.Y/.Z (Roblox PascalCase)")

    # Fix Vector3 immutable component assignment: vec.y = 0 → vec = Vector3.new(vec.X, 0, vec.Z)
    # Roblox Vector3 is immutable — cannot set individual components
    if re.search(r'\w+\.[xyzXYZ]\s*=\s*', source):
        def _fix_vec_component(m):
            indent = m.group(1)
            obj = m.group(2)
            comp = m.group(3).lower()
            val = m.group(4).rstrip()
            if comp == 'x':
                return f'{indent}{obj} = Vector3.new({val}, {obj}.Y, {obj}.Z)'
            elif comp == 'y':
                return f'{indent}{obj} = Vector3.new({obj}.X, {val}, {obj}.Z)'
            else:  # z
                return f'{indent}{obj} = Vector3.new({obj}.X, {obj}.Y, {val})'
        source = re.sub(
            r'^(\s*)([\w.]+(?:\[[\w.]+\])?)\.([xyzXYZ])\s*=\s*([^\n]+)$',
            _fix_vec_component,
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed Vector3 immutable component assignment → Vector3.new()")

    # Fix bare constructor `= ()` patterns from stripped `new Type()` calls
    if re.search(r'=\s*\(\)', source):
        source = re.sub(r'=\s*\(\)(\s*;?\s*(?:--.*)?)?$', r'= nil\1', source, flags=re.MULTILINE)
        fixes.append("Fixed bare constructor = () → = nil")
    # Fix `--[[ new Type ]] ()` → `nil` (commented constructor leaving bare parens)
    if '--[[ new ' in source:
        # No-arg constructor: `--[[ new Type ]] ()` → `nil`
        source = re.sub(r'--\[\[.*?new\s+\w+.*?\]\]\s*\(\)', 'nil', source)
        # List/collection with args: `--[[ new List ]] (expr)` → `{}`
        source = re.sub(r'--\[\[.*?new\s+(?:List|Dictionary|HashSet|Queue|Stack)\s*\]\]\s*\([^)]*\)', '{}', source)
        # Strip remaining `--[[ new Type ]]` leaving just the args (handled by other fixers)
        source = re.sub(r'--\[\[.*?new\s+\w+[\w.]*\s*\]\]\s*', '', source)

    # Fix Unity Color constants → Roblox Color3
    _COLOR_CONSTANTS = {
        'Color.clear': 'Color3.new(0, 0, 0)',
        'Color.white': 'Color3.new(1, 1, 1)',
        'Color.black': 'Color3.new(0, 0, 0)',
        'Color.red': 'Color3.new(1, 0, 0)',
        'Color.green': 'Color3.new(0, 1, 0)',
        'Color.blue': 'Color3.new(0, 0, 1)',
        'Color.yellow': 'Color3.new(1, 1, 0)',
        'Color.cyan': 'Color3.new(0, 1, 1)',
        'Color.magenta': 'Color3.new(1, 0, 1)',
        'Color.gray': 'Color3.new(0.5, 0.5, 0.5)',
        'Color.grey': 'Color3.new(0.5, 0.5, 0.5)',
    }
    for unity_color, rblx_color in _COLOR_CONSTANTS.items():
        if unity_color in source:
            source = source.replace(unity_color, rblx_color)
            fixes.append(f"Fixed {unity_color} → {rblx_color}")

    # Comment out C# bitwise operations (not valid in Roblox Luau)
    # Patterns: `1 << N`, `x & 1 << y`, bitshift in expressions
    if re.search(r'\d\s*<<\s*\d|\w\s*<<\s*\w|&\s*1\s*<<|LayerMask', source):
        # Lines with bitshift operators (digit/word << digit/word)
        def _comment_bitwise(m):
            if m.group(2).strip().startswith('--'):
                return m.group(0)  # Already a comment
            return f'{m.group(1)}-- [C# bitwise] {m.group(2)}'
        source = re.sub(
            r'^(\s*)(.+\w\s*<<\s*\w.*)$',
            _comment_bitwise,
            source,
            flags=re.MULTILINE,
        )
        # Comment out LayerMask.NameToLayer/GetMask calls
        source = re.sub(
            r'^(\s*)(?!--)(.+LayerMask\.\w+.*)$',
            r'\1-- [Unity LayerMask] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out Unity LayerMask bitwise operations")

    # Comment out C# bitwise AND expressions: `(flags & Enum.Value) == Enum.Value`
    # Roblox Luau doesn't have a `&` operator; these are typically C# flag checks
    # Only match `&` surrounded by spaces (not `&` in strings/URLs)
    if re.search(r'\w\s+&\s+\w', source):
        source = re.sub(
            r'^(\s*)(?!--)(.+\w\s+&\s+\w.*)$',
            lambda m: m.group(0) if m.group(2).strip().startswith('--') else f'{m.group(1)}-- [C# bitwise] {m.group(2)}',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out C# bitwise AND operations")

    # Fix undefined `collider` variable in Touched handlers → `otherPart`
    # This happens when C# `Collision collision` / `Collider collider` params survive
    # Only replace when `collider` is NOT locally defined
    if 'collider' in source and 'local collider' not in source:
        # Replace standalone `collider.` and `collider:` references with `otherPart.` / `otherPart:`
        source = re.sub(r'\bcollider\b(?=[\.:])(?!.*--)', 'otherPart', source)
        # In function params, `collider` is valid — only replace in body references
        fixes.append("Fixed undefined 'collider' variable → 'otherPart'")

    # Fix Debug.DrawRay / Debug.DrawLine (editor-only, not in Roblox)
    if 'Debug.Draw' in source:
        source = re.sub(
            r'^(\s*)(?!--)(.*Debug\.Draw\w+\(.*)$',
            r'\1-- [Unity editor] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out Debug.Draw* calls")

    # Fix m_MonoBehaviour undefined variable (Unity SceneLinkedSMB pattern)
    # In Roblox, this is the parent behavior script — lookup via FindFirstChild
    if 'm_MonoBehaviour' in source and 'local m_MonoBehaviour' not in source:
        # Inject a local lookup at the top of the script
        source = f'local m_MonoBehaviour = script.Parent:FindFirstChildWhichIsA("ModuleScript") or script.Parent\n' + source
        fixes.append("Injected m_MonoBehaviour lookup for SceneLinkedSMB pattern")

    # Fix CFrame.Angles(Vector3) → CFrame.Angles(vec.X, vec.Y, vec.Z)
    # CFrame.Angles takes 3 numbers, not a Vector3
    if 'CFrame.Angles(' in source:
        def _fix_cframe_angles_in_line(line):
            idx = line.find('CFrame.Angles(')
            if idx < 0:
                return line
            start = idx + len('CFrame.Angles(')
            # Find matching closing paren
            depth = 1
            pos = start
            while pos < len(line) and depth > 0:
                if line[pos] == '(':
                    depth += 1
                elif line[pos] == ')':
                    depth -= 1
                pos += 1
            if depth != 0:
                return line  # Unbalanced, skip
            args_str = line[start:pos - 1].strip()
            # Count commas at top level
            d = 0
            commas = 0
            for c in args_str:
                if c == '(':
                    d += 1
                elif c == ')':
                    d -= 1
                elif c == ',' and d == 0:
                    commas += 1
            if commas >= 2:
                return line  # Already has 3+ args
            if commas == 0 and args_str:
                # Single arg: check if it's a number
                if re.match(r'^[\d.\-]+$', args_str):
                    return line
                # Single vector arg → expand to .X, .Y, .Z
                replacement = f'CFrame.Angles(math.rad({args_str}.X), math.rad({args_str}.Y), math.rad({args_str}.Z))'
                return line[:idx] + replacement + line[pos:]
            return line

        lines = source.split('\n')
        new_lines = []
        changed = False
        for line in lines:
            if 'CFrame.Angles(' in line and not line.strip().startswith('--'):
                new_line = _fix_cframe_angles_in_line(line)
                if new_line != line:
                    changed = True
                new_lines.append(new_line)
            else:
                new_lines.append(line)
        if changed:
            source = '\n'.join(new_lines)
            fixes.append("Fixed CFrame.Angles(Vector3) → CFrame.Angles(vec.X, vec.Y, vec.Z)")

    # Fix uninitialized `dt` in while loops: add `local dt = 0` before loop
    # Pattern: `while ... do ... dt ... dt = task.wait() ... end` where dt is used before assignment
    if 'dt = task.wait()' in source and 'local dt' not in source:
        lines = source.split('\n')
        new_lines = []
        dt_initialized = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            # If we see a while/for loop, check if dt is used before assignment in it
            if (stripped.startswith('while ') or stripped.startswith('for ')):
                uses_dt_before_assign = False
                for j in range(i + 1, min(i + 20, len(lines))):
                    fwd = lines[j].strip()
                    if fwd == 'end':
                        break
                    if 'dt = task.wait()' in fwd:
                        break
                    if re.search(r'\bdt\b', fwd) and not fwd.startswith('--'):
                        uses_dt_before_assign = True
                        break
                if uses_dt_before_assign:
                    indent = len(line) - len(line.lstrip())
                    if not dt_initialized:
                        new_lines.append(' ' * indent + 'local dt = 0')
                        dt_initialized = True
                    else:
                        new_lines.append(' ' * indent + 'dt = 0')
            new_lines.append(line)
        if dt_initialized:
            source = '\n'.join(new_lines)
            fixes.append("Added dt initialization before while loop")

    # Fix workspace.Gravity used as Vector3 (it's a number in Roblox)
    # workspace.Gravity:Dot(...) or workspace.Gravity * Vector3 or + workspace.Gravity etc.
    if 'workspace.Gravity' in source:
        # :Dot() on Gravity → Vector3 context
        source = re.sub(
            r'workspace\.Gravity:Dot\(([^)]+)\)',
            r'Vector3.new(0, -workspace.Gravity, 0):Dot(\1)',
            source,
        )
        # workspace.Gravity * scalar_or_vec (not just single number)
        source = re.sub(
            r'workspace\.Gravity\s*\*\s*workspace\.Gravity\b',
            'workspace.Gravity * workspace.Gravity',  # number * number is fine
            source,
        )
        # vec + workspace.Gravity or vec - workspace.Gravity (vector arithmetic context)
        source = re.sub(
            r'(\w+(?:\.\w+)*)\s*\+\s*workspace\.Gravity\s*\*\s*(\w+)\b',
            r'\1 + Vector3.new(0, -workspace.Gravity, 0) * \2',
            source,
        )
        source = re.sub(
            r'(\w+(?:\.\w+)*)\s*-\s*workspace\.Gravity\s*\*\s*(\w+)\b',
            r'\1 - Vector3.new(0, -workspace.Gravity, 0) * \2',
            source,
        )
        # toTarget:Dot(workspace.Gravity) → toTarget:Dot(Vector3.new(0, -workspace.Gravity, 0))
        source = re.sub(
            r':Dot\(workspace\.Gravity\)',
            ':Dot(Vector3.new(0, -workspace.Gravity, 0))',
            source,
        )
        fixes.append("Fixed workspace.Gravity used as Vector3 → Vector3.new(0, -workspace.Gravity, 0)")

    # Fix CFrame.lookAt with single arg → needs (origin, target)
    # CFrame.lookAt(dir) → CFrame.lookAt(script.Parent.Position, script.Parent.Position + dir)
    if 'CFrame.lookAt(' in source:
        def _fix_cframe_lookat(m):
            args = m.group(1).strip()
            # Count commas outside parens to see if it's already 2+ args
            depth = 0
            commas = 0
            for c in args:
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                elif c == ',' and depth == 0:
                    commas += 1
            if commas == 0:
                # Single arg — wrap with origin
                return f'CFrame.lookAt(script.Parent.Position, script.Parent.Position + {args})'
            return m.group(0)  # Already has 2+ args
        source = re.sub(r'CFrame\.lookAt\(([^)]+)\)', _fix_cframe_lookat, source)
        fixes.append("Fixed CFrame.lookAt(single_arg) → CFrame.lookAt(origin, origin + dir)")

    # Fix `and then` at line breaks (multi-line conditions)
    # Pattern 1: `if (cond1 and then\n    cond2)` → `if (cond1 and\n    cond2) then`
    if re.search(r'\band\s+then\s*\n', source):
        # Multi-line: `and then\n    cond)` → `and\n    cond) then`
        source = re.sub(
            r'\band\s+then\s*\n(\s+)(.*?\))\s*$',
            r'and\n\1\2 then',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed multi-line 'and then' → joined condition with 'then' at end")
    # Pattern 2: single-line `and then` (no continuation)
    if re.search(r'\band\s+then\b', source):
        source = re.sub(r'\band\s+then\b', 'then', source)
        fixes.append("Fixed 'and then' → 'then' in conditions")

    # Fix incomplete bit-shift expressions: `1 << -- comment`
    if re.search(r'1\s*<<\s*--', source):
        source = re.sub(
            r'^(\s*)(.+1\s*<<\s*--.*)$',
            r'\1-- [Unity LayerMask] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out incomplete bit-shift expressions")

    # Fix if-condition with RHS in comment: `if expr == -- comment ... then`
    # The `then` is inside the comment, so the if has no then keyword
    if re.search(r'\bif\b.*==\s*--.*\bthen\b', source):
        source = re.sub(
            r'^(\s*)if\b.*==\s*--.*\bthen\b.*$',
            lambda m: f'{m.group(1)}-- [broken comparison] {m.group(0).strip()}',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out if-condition with comparison RHS in comment")

    # Fix incomplete assignment from comment: `var = -- comment: text`
    # where var is assigned a comment (the expression was lost)
    if re.search(r'=\s*--\s*\w+:', source):
        def _fix_incomplete_assign(m):
            indent = m.group(1)
            var = m.group(2)
            comment = m.group(3)
            return f'{indent}{var} = nil {comment}'
        source = re.sub(
            r'^(\s*)(local\s+\w+|[\w.]+)\s*=\s*(--\s*\w+:.*)$',
            _fix_incomplete_assign,
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed incomplete assignment = -- comment → = nil -- comment")

    # Fix assignment = inside if-ternary: (if x = val then → (if x == val then
    if re.search(r'\(if\s+\w+\s*=\s*(?!.*==)', source):
        source = re.sub(
            r'\(if\s+(\w+)\s*=\s*(?!=)',
            r'(if \1 == ',
            source,
        )
        fixes.append("Fixed assignment = inside if-ternary → ==")

    # Fix `GetPartBoundsInRadiusNonAlloc` / `GetPartBoundsInBoxNonAlloc` → remove NonAlloc
    if 'NonAlloc' in source:
        source = re.sub(r'(GetPartBoundsIn(?:Radius|Box))NonAlloc', r'\1', source)
        fixes.append("Fixed NonAlloc methods → standard Roblox equivalents")

    # Fix Animator API remnants: GetCurrentAnimatorStateInfo, GetNextAnimatorStateInfo, IsInTransition
    # These are Unity Animator APIs with no Roblox equivalent
    if 'AnimatorStateInfo' in source or 'IsInTransition' in source or 'shortNameHash' in source or 'tagHash' in source or 'normalizedTime' in source:
        source = re.sub(
            r'^(\s*)(?!--)(.+(?:GetCurrentAnimatorStateInfo|GetNextAnimatorStateInfo|IsInTransition)\(.*)$',
            r'\1-- [Unity Animator] \2',
            source,
            flags=re.MULTILINE,
        )
        # Also comment out lines referencing .shortNameHash, .tagHash, .normalizedTime
        source = re.sub(
            r'^(\s*)(?!--)(.+\.(?:shortNameHash|tagHash|normalizedTime)\b.*)$',
            r'\1-- [Unity Animator] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out Unity Animator state info API calls")

    # Fix .Warp() (Unity NavMeshAgent) → .Position = (teleport)
    if '.Warp(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Warp\(([^)]+)\)',
            r'\1.Position = \2',
            source,
        )
        fixes.append("Fixed .Warp() → .Position = (NavMeshAgent teleport)")

    # Fix CFrame.identity → CFrame.new() (Roblox equivalent)
    if 'CFrame.identity' in source:
        source = source.replace('CFrame.identity', 'CFrame.new()')
        fixes.append("Fixed CFrame.identity → CFrame.new()")

    # Fix .ResetPath() (Unity NavMeshAgent) → comment out
    if '.ResetPath()' in source:
        source = re.sub(
            r'^(\s*)(?!--)(.+\.ResetPath\(\).*)$',
            r'\1-- [Unity NavMesh] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out .ResetPath() (Unity NavMesh)")

    # Fix .SetColor/.GetColor/.SetVector/.GetVector (Unity Material API)
    if re.search(r'\.(SetColor|GetColor|SetVector|GetVector|SetTexture|GetTexture)\(', source):
        source = re.sub(
            r'^(\s*)(?!--)(.+\.(?:SetColor|GetColor|SetVector|GetVector|SetTexture|GetTexture)\(.*)$',
            r'\1-- [Unity Material] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out Unity Material Set/Get API calls")

    # Fix `Vector3.new[N]` (C# array constructor) → `table.create(N)`
    if re.search(r'Vector3\.new\[\w+\]', source):
        source = re.sub(r'Vector3\.new\[(\w+)\]', r'table.create(\1, Vector3.zero)', source)
        fixes.append("Fixed Vector3.new[N] → table.create(N)")

    # Fix undefined `controller` variable → `m_Controller`
    if re.search(r'\bcontroller\b', source) and 'local controller' not in source and 'm_Controller' in source:
        source = re.sub(r'\bcontroller\b(?=[\.:[\s])', 'm_Controller', source)
        fixes.append("Fixed undefined 'controller' → 'm_Controller'")

    # Fix .contacts (Unity Collision.contacts) → not available in Roblox
    if '.contacts' in source:
        source = re.sub(
            r'^(\s*)(?!--)(.+\.contacts\b.*)$',
            r'\1-- [Unity Collision] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out .contacts (Unity Collision API)")

    # Fix remaining RenderTexture references in active code
    if re.search(r'(?:^[^-]*RenderTexture|\.worldToCameraMatrix|\.projectionMatrix|\.CalculateObliqueMatrix|\.depthTextureMode|\.renderingPath|\.cullingMask|\.targetTexture|\.clearFlags)', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)(?!--)(.+(?:RenderTexture|\.worldToCameraMatrix|\.projectionMatrix|\.CalculateObliqueMatrix|\.depthTextureMode|\.renderingPath|\.cullingMask|\.targetTexture|\.clearFlags)\b.*)$',
            r'\1-- [Unity camera] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out Unity camera/render API remnants")

    # Note: `continue` IS valid in Roblox Luau (supported since 2020)
    # No fix needed for continue statements

    # Fix remaining C# null-coalescing ?? operator
    # expr ?? fallback → (expr ~= nil and expr) or fallback
    if '??' in source:
        def _fix_null_coalesce(m):
            lhs = m.group(1).strip()
            rhs = m.group(2).strip()
            # Simple form for short expressions
            return f'({lhs} ~= nil and {lhs} or {rhs})'
        source = re.sub(
            r'([^\n=]+?)\s*\?\?\s*([^\n,;]+)',
            _fix_null_coalesce,
            source,
        )
        fixes.append("Fixed C# ?? null-coalescing → Luau or-pattern")

    # Fix MessageType enum references → string constants
    # MessageType.DEAD → "DEAD", MessageType.DAMAGED → "DAMAGED" etc.
    if 'MessageType.' in source:
        source = re.sub(r'\bMessageType\.(\w+)', r'"\1"', source)
        fixes.append("Fixed MessageType.X → string constants")

    # Fix Message.MessageType.X → string constants
    if 'Message.MessageType.' in source:
        source = re.sub(r'\bMessage\.MessageType\.(\w+)', r'"\1"', source)

    # Fix broken table insertion: `tbl[{ key = val] = key2 = val2 }` → `table.insert(tbl, { key = val, key2 = val2 })`
    # This happens when .Add({key=val, key2=val2}) is misinterpreted as dict.Add(key, val)
    if re.search(r'\w+\[\{[^}]*\]\s*=\s*\w+\s*=', source):
        def _fix_broken_table_insert(m):
            tbl = m.group(1)
            content = m.group(2)
            # The pattern is: tbl[{ a = b] = c = d }
            # Fix: extract all key=value pairs and form proper table.insert
            # Replace `] = ` with `, ` to rejoin the table literal
            content = re.sub(r'\]\s*=\s*', ', ', content)
            return f'table.insert({tbl}, {{ {content} }})'
        source = re.sub(
            r'(\w+)\[\{\s*([^}]+)\}',
            _fix_broken_table_insert,
            source,
        )
        fixes.append("Fixed broken table insertion pattern")

    # Fix broken RemoveRange in for loop: `for _i = 1, math.max(0, #arr - N do table.remove...`
    # The `do` ended up inside the math.max call
    if re.search(r'math\.max\([^)]*\bdo\b', source):
        source = re.sub(
            r'for\s+\w+\s*=\s*1,\s*math\.max\(0,\s*#(\w+)\s*-\s*(\d+)\s+do\s+table\.remove\(\1,\s*\d+\s*\+\s*\d+\)\s*end\s*(--.*)?',
            lambda m: f'for _i = 1, math.max(0, #{m.group(1)} - {m.group(2)}) do table.remove({m.group(1)}, 1) end {m.group(3) or ""}',
            source,
        )
        fixes.append("Fixed broken RemoveRange for-loop")

    # Fix `not expr ~= nil` → `expr == nil` (Luau precedence bug: not binds tighter than ~=)
    if 'not ' in source and '~= nil' in source:
        source = re.sub(
            r'\bnot\s+([\w.\[\]()]+)\s*~=\s*nil\b',
            r'\1 == nil',
            source,
        )
        fixes.append("Fixed 'not x ~= nil' → 'x == nil' (precedence)")

    # Fix `end` used as variable name (Luau reserved word)
    # Pattern: `local end = expr` → `local endPos = expr`, `start, end` → `start, endPos`
    if re.search(r'\blocal\s+end\s*=', source):
        source = re.sub(r'\blocal\s+end\s*=', 'local endPos =', source)
        source = re.sub(r'\bend\b(?=\s*[,\)\]])', 'endPos', source)
        # Fix references to `end` used as a value (not keyword)
        # Only in contexts where it's clearly a variable: end.X, end.Y, (start, end, ...)
        source = re.sub(r'(?<=,\s)end(?=\s*[,\)])', 'endPos', source)
        source = re.sub(r'\bend\.([A-Z])', r'endPos.\1', source)
        fixes.append("Fixed 'end' used as variable name (reserved word) → endPos")

    # Fix `function` used as variable name (reserved word) → `_func`
    # Pattern: `table.insert(list, function)`, `RegisterBatchFunction(function)`
    if re.search(r'[,(]\s*function\s*[,)\]]', source):
        source = re.sub(r'([,(]\s*)function(\s*[,)\]])', r'\1_func\2', source)
    if re.search(r'local function \w+\([^)]*\bfunction\b[^)]*\)', source):
        source = re.sub(
            r'(local function \w+\([^)]*)\bfunction\b([^)]*\))',
            r'\1_func\2',
            source,
        )

    # Fix trailing commas in function calls: func(a, b,) → func(a, b)
    if ',)' in source:
        source = re.sub(r',\s*\)', ')', source)
        fixes.append("Fixed trailing commas in function calls")
    # Fix trailing comma on variable declaration: `local x = 5,` → `local x = 5`
    if re.search(r'local \w+ = .+,\s*$', source, re.MULTILINE):
        source = re.sub(r'^(\s*local \w+ = .+?),\s*$', r'\1', source, flags=re.MULTILINE)
        fixes.append("Fixed trailing comma on local declaration")

    # Fix `table.remove(arr, , #)` → `table.remove(arr, #arr)` (pop last)
    if 'table.remove(' in source and ', , #)' in source:
        source = re.sub(
            r'table\.remove\((\w+),\s*,\s*#\)',
            r'table.remove(\1, #\1)',
            source,
        )
        fixes.append("Fixed table.remove(arr, , #) → table.remove(arr, #arr)")

    # Fix mismatched bracket/paren: `arr[idx).#prop]` → `arr[idx].prop` or `#arr[idx].prop`
    if re.search(r'\w+\[[^]]*\)\.#\w+\]', source):
        source = re.sub(
            r'(\w+\[[^]]*?)\)\.#(\w+)\]',
            r'#\1].\2',
            source,
        )

    # Fix assignment = in if conditions (should be ==)
    # Pattern: `if count = expr then` → `if count == expr then`
    # Also handles if-expressions: `(if x = y then a else b)` → `(if x == y then a else b)`
    # Only match single `=` not preceded by `~`, `<`, `>`, `=` and not followed by `=`
    if re.search(r'\bif\b.*[^~<>=!]=(?!=)', source):
        def _fix_if_assign(line):
            # Find all `if ... then` patterns in the line (both statement and expression)
            result = line
            for m in re.finditer(r'(\bif\s*\(?)(.+?)(\s*then\b)', result):
                cond = m.group(2)
                # Replace single = with == (not <=, >=, ~=, ==)
                fixed_cond = re.sub(r'(?<![~<>=!])=(?!=)', ' == ', cond)
                if fixed_cond != cond:
                    result = result[:m.start(2)] + fixed_cond + result[m.end(2):]
            return result
        lines = source.split('\n')
        new_lines = [_fix_if_assign(l) for l in lines]
        if new_lines != lines:
            source = '\n'.join(new_lines)
            fixes.append("Fixed assignment = in if conditions → ==")

    # Fix math.random(0, #arr) → math.random(1, #arr) for 1-based Luau indexing
    if 'math.random(0,' in source:
        source = re.sub(r'math\.random\(0,\s*#', 'math.random(1, #', source)
        fixes.append("Fixed math.random(0, #arr) → math.random(1, #arr) (1-based)")

    # Fix .Set() on Vector2/Vector3 (immutable in Roblox)
    # Pattern: m_Movement.Set(x, y) → m_Movement = Vector2.new(x, y)
    if '.Set(' in source:
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Set\(([^,]+),\s*([^,\)]+)\)',
            r'\1 = Vector2.new(\2, \3)',
            source,
        )
        source = re.sub(
            r'(\w+(?:\.\w+)*)\.Set\(([^,]+),\s*([^,]+),\s*([^,\)]+)\)',
            r'\1 = Vector3.new(\2, \3, \4)',
            source,
        )
        fixes.append("Fixed .Set() → Vector assignment (immutable in Roblox)")

    # Fix C# [Attribute] brackets on code lines
    # Pattern: `[HelpBox] local x = ...` → `local x = ...`, `[Range(0,1)]` → strip
    if re.search(r'^\s*\[\w+', source, re.MULTILINE):
        # Standalone attribute line: `[SerializeField]` → remove
        source = re.sub(
            r'^\s*\[(?:SerializeField|Header|Range|Tooltip|HideInInspector|FormerlySerializedAs|'
            r'HelpBox|Space|Min|Max|CreateAssetMenu|RequireComponent|DisallowMultipleComponent|'
            r'AddComponentMenu|ExecuteInEditMode|ExecuteAlways)\b[^\]]*\]\s*\n',
            '',
            source,
            flags=re.MULTILINE,
        )
        # Inline attribute before declaration: `[HelpBox] local x` → `local x`
        source = re.sub(
            r'^\s*\[\w+(?:\([^\)]*\))?\]\s*(local\s)',
            r'\1',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Stripped C# [Attribute] brackets")

    # Fix Unity API remnants
    # .bounds.size → .Size (Roblox Part property)
    if '.bounds.size' in source:
        source = re.sub(r'(\w+(?:\.\w+)*)\.bounds\.size\b', r'\1.Size', source)
        fixes.append("Fixed .bounds.size → .Size")
    if '.bounds.center' in source:
        source = re.sub(r'(\w+(?:\.\w+)*)\.bounds\.center\b', r'\1.Position', source)
        fixes.append("Fixed .bounds.center → .Position")
    if '.bounds.extents' in source:
        source = re.sub(r'(\w+(?:\.\w+)*)\.bounds\.extents\b', r'(\1.Size / 2)', source)
        fixes.append("Fixed .bounds.extents → Size/2")

    # .attachedRigidbody → the part itself (Roblox parts are their own physics bodies)
    # Handles: obj.attachedRigidbody, arr[n].attachedRigidbody, obj.prop.attachedRigidbody
    if '.attachedRigidbody' in source:
        source = re.sub(r'([\w.\[\]]+)\.attachedRigidbody\b', r'\1', source)
        fixes.append("Fixed .attachedRigidbody → part itself")

    # .AddRelativeTorque() → comment out (no direct equivalent)
    if 'AddRelativeTorque' in source:
        source = re.sub(
            r'^(\s*)(\S.*\.AddRelativeTorque\(.+)$',
            r'\1-- [Unity physics] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out .AddRelativeTorque (no Roblox equivalent)")

    # .center and .radius on collider-like objects (Unity physics properties)
    if re.search(r'\.center\b', source) and re.search(r'm_Sphere|m_Capsule|m_Box|collider', source):
        source = re.sub(r'([\w.\[\]]+)\.center\b', r'\1.Position', source)
        # .radius = value → .Size = Vector3.new(value*2, value*2, value*2) (assignment context)
        def _fix_radius(m):
            line = m.group(0)
            obj = m.group(1)
            # Check if this is an assignment (radius on LHS)
            after = line[m.end(1) + len('.radius'):]
            if re.match(r'\s*=\s*', after.lstrip()) or re.match(r'\s*\*\s*[\d.]+\s*=', after.lstrip()):
                # Assignment to .radius → comment out (read-only in Roblox)
                return f'-- [Unity physics] {line}'
            return line.replace(f'{obj}.radius', f'{obj}.Size.X / 2')
        # Handle radius in assignment context vs read context separately
        source = re.sub(
            r'^(\s*)([\w.\[\]]+)\.radius\b\s*(?:\*\s*[\d.]+\s*)?=\s*[^\n]+$',
            r'\1-- [Unity physics] \2.radius assignment (read-only in Roblox)',
            source,
            flags=re.MULTILINE,
        )
        source = re.sub(r'([\w.\[\]]+)\.radius\b', r'\1.Size.X / 2', source)
        fixes.append("Fixed .center/.radius → .Position/.Size")

    # .GetPropertyBlock / .SetPropertyBlock → comment out (no Roblox equivalent)
    if 'PropertyBlock' in source:
        source = re.sub(
            r'^(\s*)(\S.*(?:Get|Set)PropertyBlock\(.+)$',
            r'\1-- [Unity material] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out PropertyBlock access (no Roblox equivalent)")

    # .isTrigger → .CanCollide = false context
    if '.isTrigger' in source:
        source = re.sub(r'(\w+(?:\.\w+)*)\.isTrigger\s*=\s*true', r'\1.CanCollide = false', source)
        source = re.sub(r'(\w+(?:\.\w+)*)\.isTrigger\s*=\s*false', r'\1.CanCollide = true', source)
        source = re.sub(r'(\w+(?:\.\w+)*)\.isTrigger\b', r'(not \1.CanCollide)', source)
        fixes.append("Fixed .isTrigger → CanCollide")

    # CFrame.Angles with exactly 2 args (axis, speed) → proper 3-arg form
    # Pattern: CFrame.Angles(axis, speed * dt) where axis is a variable → expand to 3-arg
    # Only match when first arg is a word (variable), not a number or expression
    # This avoids matching already-correct 3-arg calls like CFrame.Angles(0, math.rad(90), 0)
    if re.search(r'CFrame\.Angles\([a-zA-Z_]\w*,\s*[^,\)]+\)', source):
        def _fix_cframe_angles_2arg(m):
            full = m.group(0)
            # Count commas at top-level (outside parens) to verify exactly 2 args
            depth = 0
            commas = 0
            for c in full[len('CFrame.Angles('):-1]:
                if c == '(':
                    depth += 1
                elif c == ')':
                    depth -= 1
                elif c == ',' and depth == 0:
                    commas += 1
            if commas != 1:
                return full  # 3+ args, leave alone
            axis, speed = m.group(1).strip(), m.group(2).strip()
            return f'CFrame.Angles({axis}.X * {speed}, {axis}.Y * {speed}, {axis}.Z * {speed})'
        source = re.sub(
            r'CFrame\.Angles\(([a-zA-Z_]\w*),\s*([^,\)]+)\)',
            _fix_cframe_angles_2arg,
            source,
        )
        fixes.append("Fixed CFrame.Angles(axis, speed) → 3-arg form")

    # Humanoid:Move() is valid in Roblox — takes (moveDirection: Vector3, relativeToCamera?: bool)
    # No fix needed; the transpiler emits correct :Move() calls.

    # Camera and movement fixes are handled by the AI transpiler prompt.
    # See code_transpiler.py _AI_SYSTEM_PROMPT for ESC key, camera, and UI guidance.

    # .time property on VFX/particle instances → comment out
    if re.search(r'\w+\.time\s*=\s*[\d.]', source):
        source = re.sub(
            r'^(\s*)(\w+\.time\s*=\s*[\d.]+.*)$',
            r'\1-- [Unity VFX] \2',
            source,
            flags=re.MULTILINE,
        )

    # Object:Clone(prefab) → prefab:Clone()
    if re.search(r'\w+:Clone\(\w+\)', source):
        source = re.sub(
            r'(\w+):Clone\((\w+)\)',
            r'\2:Clone()',
            source,
        )
        fixes.append("Fixed Object:Clone(prefab) → prefab:Clone()")

    # C# remaining type declarations that slip through
    # Pattern: `SceneTransitionDestination.DestinationTag varName` → `local varName = nil`
    _EXTRA_TYPES = (
        'SceneTransitionDestination', 'InventoryController', 'Collision',
        'RaycastHit', 'ContactPoint', 'Collider', 'Renderer', 'Rigidbody',
        'LayerMask', 'Vector4', 'Matrix4x4', 'Bounds', 'Ray',
    )
    _extra_type_pat = '|'.join(re.escape(t) for t in _EXTRA_TYPES)
    if re.search(rf'\b(?:{_extra_type_pat})(?:\.\w+)*\s+\w+\s*[;=\n]', source):
        source = re.sub(
            rf'^(\s*)(?:{_extra_type_pat})(?:\.\w+)*\s+(\w+)\s*;?\s*$',
            r'\1local \2 = nil',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed remaining C# type declarations → local")

    # Fix C# interface method signatures: `TypeName MethodName(params)` with no body
    # These are bare method declarations (abstract/interface) that should be commented out
    # Pattern: PascalCase type, PascalCase method, optional parens, no assignment/local/function/return
    if re.search(r'^\s+[A-Z]\w+(?:<[^>]+>)?\s+[A-Z]\w+\s*\([^)]*\)\s*$', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)([A-Z]\w+(?:<[^>]+>)?\s+[A-Z]\w+\s*\([^)]*\))\s*$',
            r'\1-- [C#] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out C# interface method signatures")

    # Fix C# generic type declarations: `Dictionary<K,V> varName = expr`
    # or `List<Type> varName = expr` or nested `Dictionary<K, List<V>>` → `local varName = expr`
    if re.search(r'^\s+\w+<.+>\s+\w+\s*=', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)\w+<.+>\s+(\w+)\s*=\s*(.+)$',
            r'\1local \2 = \3',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed C# generic type declarations → local")
    # Also handle generic type variable without assignment: `List<Type> varName;` or `List<Type> varName`
    if re.search(r'^\s+\w+<.+>\s+\w+\s*;?\s*$', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)\w+<.+>\s+(\w+)\s*;?\s*$',
            r'\1local \2 = nil',
            source,
            flags=re.MULTILINE,
        )
    # C# generic array: `Type<T>[] varName = expr` → `local varName = expr`
    if re.search(r'^\s+\w+<.+>\[\]\s+\w+\s*=', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)\w+<.+>\[\]\s+(\w+)\s*=\s*(.+)$',
            r'\1local \2 = \3',
            source,
            flags=re.MULTILINE,
        )

    # Fix bare English text (no Luau keywords) that leaked as non-comment lines
    # Pattern: line starts with a capital letter, contains spaces, and has no Luau keywords
    _LUAU_KEYWORDS = {'local', 'function', 'if', 'then', 'else', 'elseif', 'end', 'for', 'while',
                      'do', 'repeat', 'until', 'return', 'break', 'continue', 'in', 'not', 'and', 'or',
                      'true', 'false', 'nil', 'script', 'game', 'workspace', 'task'}
    new_lines_prose = []
    for line in source.split('\n'):
        stripped = line.strip()
        if stripped and not stripped.startswith('--') and not stripped.startswith('local '):
            words = stripped.split()
            if (len(words) >= 4
                    and words[0][0].isupper()
                    and not any(w in _LUAU_KEYWORDS for w in words[:3])
                    and '=' not in stripped and '(' not in stripped and ':' not in stripped
                    and not stripped.endswith('do') and not stripped.endswith('then')):
                indent = line[:len(line) - len(line.lstrip())]
                line = f'{indent}-- [prose] {stripped}'
            # Also catch lines that are parenthesized prose: "(e.g. some text)"
            elif (stripped.startswith('(') and len(words) >= 3
                    and re.match(r'^\((?:e\.g\.|i\.e\.|note|N\.B\.)', stripped, re.IGNORECASE)):
                indent = line[:len(line) - len(line.lstrip())]
                line = f'{indent}-- [prose] {stripped}'
            # Catch enum prose: `lowercase words with spaces = number,` (C# enum comment leak)
            # Requires at least 2 space-separated words before `=` to avoid catching table entries
            elif (len(words) >= 4 and words[0][0].islower() and words[0].isalpha()
                    and re.match(r'^[a-z]+\s+[a-z]+\s+.*= \d+,?\s*$', stripped)):
                indent = line[:len(line) - len(line.lstrip())]
                line = f'{indent}-- [enum prose] {stripped}'
        new_lines_prose.append(line)
    source = '\n'.join(new_lines_prose)

    # Fix bare variable names on their own line (from API mapping stripping method calls)
    # e.g., `_agent` on its own line after `.SetDestination(...)` was removed
    if re.search(r'^\s+_?\w+\s*$', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)(_?\w+)\s*$',
            lambda m: (m.group(0) if m.group(2) in ('end', 'else', 'return', 'break', 'continue',
                                                      'true', 'false', 'nil', 'do', 'then', 'repeat')
                        or m.group(2).startswith('--')
                        else f'{m.group(1)}-- [bare var] {m.group(2)}'),
            source,
            flags=re.MULTILINE,
        )

    # Fix standalone if-expressions: `(if cond then a else b)` on their own line → comment out
    # These are ternary results that lost their assignment target during conversion
    if re.search(r'^\s+\(if\b', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)(\(if\b.+\belse\b.+\))\s*$',
            r'\1-- [dead code] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out standalone if-expressions (dead code)")

    # Fix C# PascalCase type + variable: `LineRenderer LR` or `BatchProcessor s_Instance`
    # Also handles: `PathMovementData _pathMovementData = nil` (with assignment)
    # where first word is PascalCase and second is a valid identifier (any case)
    _LUAU_KEYWORDS = {'local', 'function', 'return', 'if', 'for', 'while', 'end',
                      'else', 'elseif', 'then', 'do', 'repeat', 'until', 'not',
                      'and', 'or', 'true', 'false', 'nil', 'script', 'game',
                      'workspace', 'task', 'table', 'math', 'string', 'Vector3',
                      'Vector2', 'CFrame', 'Color3', 'Enum', 'Instance', 'require'}
    if re.search(r'^\s+[A-Z]\w+\s+\w+', source, re.MULTILINE):
        def _fix_pascal_decl(m):
            indent = m.group(1)
            type_name = m.group(2)
            var_name = m.group(3)
            assign = m.group(4) or ''
            # Skip if it looks like a Luau statement
            if type_name.lower() in _LUAU_KEYWORDS:
                return m.group(0)
            if assign.strip():
                # Has assignment: `Type var = expr` → `local var = expr`
                return f'{indent}local {var_name} {assign.strip()}'
            return f'{indent}local {var_name} = nil'
        source = re.sub(
            r'^(\s+)([A-Z]\w+)\s+(\w+)((?:\s*=\s*.+)?)\s*$',
            _fix_pascal_decl,
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed C# typed declarations with PascalCase type → local")

    # Fix PascalCase.PascalCase type + variable: `Gamekit3.PlayerController player`
    # Also handles multi-dot: `Gamekit3.InventoryController.InventoryChecker inventoryCheck`
    # And with assignment: `Vehicle.SeatAlignment align = nil`
    if re.search(r'^\s+[A-Z]\w+(?:\.\w+)+\s+\w+', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)[A-Z]\w+(?:\.\w+)+\s+(\w+)(?:\s*=\s*.+)?\s*$',
            r'\1local \2 = nil',
            source,
            flags=re.MULTILINE,
        )

    # Fix C# attribute brackets: `[AddComponentMenu(...)]`, `[Instance.newMenu(...)]` etc.
    if re.search(r'^\s+\[\w+[\w.]*\s*\(', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)(\[\w+[\w.]*\s*\([^\]]*\]\s*)$',
            r'\1-- [C# attribute] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix C# attribute brackets without parens followed by type declaration:
    # `[ReadOnly] Damageable currentTarget;` or `[TextArea] string description`
    if re.search(r'^\s+\[\w+\]\s+\w', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)(\[\w+\]\s+.*)$',
            r'\1-- [C# attribute] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix C# `where T : Base` generic constraints (standalone or on method declaration line)
    if 'where ' in source and re.search(r'\bwhere\s+\w+\s*:', source):
        # Standalone: `where T : Base` on its own line
        source = re.sub(
            r'^(\s+)(where\s+\w+\s*:.*)$',
            r'\1-- [C#] \2',
            source,
            flags=re.MULTILINE,
        )
        # On a method/type line: `bool Method() where T : Base`
        source = re.sub(
            r'^(\s+)(.*\)\s+where\s+\w+\s*:.*)$',
            r'\1-- [C#] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix C# `&=` bitwise AND assignment (not valid in Luau)
    if '&=' in source:
        source = re.sub(
            r'^(\s*)(\w+)\s*&=\s*(.+)$',
            r'\1\2 = \2 and \3',
            source,
            flags=re.MULTILINE,
        )

    # Fix C# bitwise OR `|` in assignments (not valid Luau without bit32)
    # Pattern: `varName = EnumA | EnumB` → commented out
    if re.search(r'=\s*\w+\.\w+\s*\|\s*\w+\.\w+', source):
        source = re.sub(
            r'^(\s*)(local\s+\w+\s*=\s*\w+\.\w+\s*(?:\|\s*\w+\.\w+\s*)+)$',
            r'\1-- [C#] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix C# cast: `(TypeName)expr` on assignment → just `expr`
    if re.search(r'\(Data<\w+>\)', source):
        source = re.sub(r'\(Data<\w+>\)(\w+)', r'\1', source)
    # General PascalCase cast: `(PascalCaseType)expr` → `expr`
    # Only in if-conditions and assignments, not standalone parens
    if re.search(r'\([A-Z][a-zA-Z]+\)\w', source):
        source = re.sub(r'\(([A-Z][a-zA-Z]+)\)(\w)', r'\2', source)
        fixes.append("Stripped C# type casts")

    # Fix `string script.Parent[string key]` (C# indexer) → comment out
    if re.search(r'^\s+string\s+script\.Parent\s*\[', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)(string\s+script\.Parent\s*\[.+\].*)$',
            r'\1-- [C#] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix mangled inline comments breaking expressions:
    # `obj -- comment.Property = value` → `-- [mangled] obj -- comment.Property = value`
    if re.search(r'\w+\s+-- \w+.*\.(?:Anchored|Enabled|Position|Size)\s*=', source):
        source = re.sub(
            r'^(\s*)(\w+\s+-- \w+.*\.(?:Anchored|Enabled|Position|Size)\s*=.*)$',
            r'\1-- [mangled] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix `.-- comment` breaking expressions (property access turned into comment)
    # e.g., `obj[i].-- TabClicked = TabClicked - ChangeTab ...`
    if re.search(r'\w\.\s*-- \w+', source):
        source = re.sub(
            r'^(\s*)(.+\w\.\s*-- \w+.*)$',
            lambda m: (m.group(0) if m.group(2).strip().startswith('--') else
                       f'{m.group(1)}-- [mangled property] {m.group(2).strip()}'),
            source,
            flags=re.MULTILINE,
        )

    # Fix: script.Parent as function parameter name (from C# extension method `this` param)
    # `function Foo(script.Parent)` → `function Foo(obj)`
    # `function Foo(script.Parent, x, y)` → `function Foo(obj, x, y)`
    # `function Foo(layers, script.Parent)` → `function Foo(layers, obj)`
    if 'script.Parent)' in source or 'script.Parent,' in source:
        def _fix_script_parent_param(m):
            prefix = m.group(1)
            params = m.group(2)
            # Replace script.Parent with obj in parameter list
            params = re.sub(r'\bscript\.Parent\b', 'obj', params)
            return f'{prefix}{params})'
        source = re.sub(
            r'((?:local\s+)?function\s+\w+\()([^)]*\bscript\.Parent\b[^)]*)\)',
            _fix_script_parent_param,
            source,
        )

    # Fix: broken comparison `> then` / `< then` where RHS leaked to next line
    # `if expr > then\n    value)` → comment out (broken expression)
    if re.search(r'[><!]=?\s+then\s*$', source, re.MULTILINE):
        lines = source.split('\n')
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # Match: `if ... > then` or `... >= then` etc (comparison missing RHS)
            if re.search(r'(?:>|<|>=|<=|==|~=)\s+then\s*$', line.rstrip()):
                indent = len(line) - len(line.lstrip())
                pad = ' ' * indent
                result.append(f'{pad}-- [broken comparison] {line.strip()}')
                # Also comment following continuation line(s) that were the leaked RHS
                while i + 1 < len(lines):
                    next_line = lines[i + 1]
                    next_stripped = next_line.strip()
                    # continuation: indented value ending with ) or just a value
                    if next_stripped and not next_stripped.startswith('--') and not re.match(r'(?:local|if|for|while|function|end|else|elseif|return)\b', next_stripped):
                        next_indent = len(next_line) - len(next_line.lstrip())
                        if next_indent > indent:
                            result.append(f'{" " * next_indent}-- [broken comparison] {next_stripped}')
                            i += 1
                            continue
                    break
            else:
                result.append(line)
            i += 1
        new_source = '\n'.join(result)
        if new_source != source:
            source = new_source
            fixes.append("Fixed broken comparison with missing RHS")

    # Fix: multiline C# string literal (unclosed quote)
    # `local helpString = "` followed by text on next lines then `"`
    # → comment out the whole thing
    if re.search(r'local\s+\w+\s*=\s*"[^"]*$', source, re.MULTILINE):
        lines = source.split('\n')
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            # Detect: `local var = "something` without closing quote
            if re.match(r'\s*local\s+\w+\s*=\s*"[^"]*$', line):
                indent = len(line) - len(line.lstrip())
                pad = ' ' * indent
                result.append(f'{pad}-- [multiline string] {stripped}')
                # Comment out lines until closing quote found
                while i + 1 < len(lines):
                    i += 1
                    next_line = lines[i]
                    next_stripped = next_line.strip()
                    next_indent = len(next_line) - len(next_line.lstrip()) if next_line.strip() else indent
                    result.append(f'{" " * next_indent}-- [multiline string] {next_stripped}')
                    if '"' in next_stripped:
                        break
            else:
                result.append(line)
            i += 1
        new_source = '\n'.join(result)
        if new_source != source:
            source = new_source
            fixes.append("Fixed multiline C# string literals")

    # Fix: broken `or` / `and` on continuation line without proper context
    # `if (condition\n     or  condition2)` → join lines
    # Pattern: line ends with incomplete condition, next line starts with `or` or `and`
    if re.search(r'^\s+or\s+', source, re.MULTILINE):
        lines = source.split('\n')
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # Check if next line starts with `or ` or `and ` (continuation)
            if i + 1 < len(lines):
                next_stripped = lines[i + 1].strip()
                if re.match(r'^(?:or|and)\s+', next_stripped):
                    # Check if current line is an if/elseif condition or assignment
                    cur_stripped = line.rstrip()
                    # If current line ends with an incomplete expression (no `then`, no `do`, no `)`)
                    if (re.search(r'(?:if|elseif|while)\s.*[^)]\s*$', cur_stripped) or
                        cur_stripped.endswith('PathPartial') or
                        cur_stripped.endswith('PathInvalid') or
                        re.search(r'==\s*\w+\s*$', cur_stripped)):
                        # Join the continuation line
                        joined = line.rstrip() + ' ' + next_stripped
                        result.append(joined)
                        i += 2
                        continue
            result.append(line)
            i += 1
        new_source = '\n'.join(result)
        if new_source != source:
            source = new_source
            fixes.append("Fixed broken or/and continuation lines")

    # Fix: incomplete table constructor: `local msg = nil` followed by `key = value,` lines then `}`
    # From C# `var msg = new DamageMessage { amount = 1, ... }`
    # Pattern: `local var = nil` then indented `key = val,` lines then `}`
    if re.search(r'local\s+\w+\s*=\s*nil\s*$', source, re.MULTILINE):
        lines = source.split('\n')
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            # Detect: `local var = nil`
            m_nil = re.match(r'^(\s*)(local\s+(\w+)\s*=\s*)nil\s*$', line)
            if m_nil and i + 1 < len(lines):
                # Look ahead for table entry pattern (key = value,)
                next_stripped = lines[i + 1].strip()
                if re.match(r'\w+\s*=\s*.+[,]?\s*$', next_stripped) and not re.match(r'local\s+', next_stripped):
                    # Collect table entries
                    indent = m_nil.group(1)
                    var_decl = m_nil.group(2)
                    entries = []
                    j = i + 1
                    while j < len(lines):
                        entry_stripped = lines[j].strip()
                        if entry_stripped == '}' or entry_stripped == '},':
                            j += 1
                            break
                        if re.match(r'\w+\s*=\s*.+', entry_stripped):
                            entries.append(entry_stripped.rstrip(','))
                            j += 1
                        else:
                            break
                    if entries:
                        # Build table literal
                        result.append(f'{indent}{var_decl}{{')
                        entry_indent = indent + '    '
                        for k, entry in enumerate(entries):
                            comma = ',' if k < len(entries) - 1 else ''
                            result.append(f'{entry_indent}{entry}{comma}')
                        result.append(f'{indent}}}')
                        i = j
                        continue
            result.append(line)
            i += 1
        new_source = '\n'.join(result)
        if new_source != source:
            source = new_source
            fixes.append("Fixed incomplete table constructors")

    # Fix: `= new[]` C# array initializer over multiple lines → `= { values }`
    if '= new[]' in source:
        lines = source.split('\n')
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if '= new[]' in line:
                indent_m = re.match(r'^(\s*)', line)
                indent = indent_m.group(1) if indent_m else ''
                # Replace `new[]` with `{`
                new_line = line.replace('= new[]', '= {')
                result.append(new_line)
                # Collect entries until blank line or less-indented line
                while i + 1 < len(lines):
                    next_line = lines[i + 1]
                    next_stripped = next_line.strip()
                    if not next_stripped:
                        # Close the table before blank line
                        result.append(f'{indent}}}')
                        break
                    next_indent = len(next_line) - len(next_line.lstrip())
                    if next_indent <= len(indent):
                        result.append(f'{indent}}}')
                        break
                    result.append(next_line)
                    i += 1
            else:
                result.append(line)
            i += 1
        new_source = '\n'.join(result)
        if new_source != source:
            source = new_source
            fixes.append("Fixed C# new[] array initializer → table literal")

    # Fix: C# unary + operator (not valid in Luau)
    # `Vector3.new(+x, -y, +z)` → `Vector3.new(x, -y, z)`
    # Pattern: `(+var` or `,+var` or `, +var` (after opening paren or comma)
    if re.search(r'[,(]\s*\+[a-zA-Z_]', source):
        source = re.sub(r'([,(]\s*)\+([a-zA-Z_])', r'\1\2', source)
        fixes.append("Fixed unary + operator (not valid in Luau)")

    # Fix: `if -- comment ... then` or `if (-- comment` (comment embedded in if-condition)
    # The whole condition is broken — comment out the line
    if re.search(r'\bif\s+\(?--\s', source):
        source = re.sub(
            r'^(\s*)if\s+\(?--\s.*$',
            lambda m: f'{m.group(1)}-- [broken condition] {m.group(0).strip()}',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed if-condition with embedded comment")

    # Fix: `:Method()` at start of expression (bare colon method after comma/space)
    # `360 / :GetChildren()` → `360 / #script.Parent:GetChildren()`
    # `:Cross(x, y)` → `script.Parent.CFrame.RightVector:Cross(x, y)` (too specific, just prepend script.Parent)
    if re.search(r'(?:^|[\s,/\*\+\-=\(]):\w+\(', source, re.MULTILINE):
        # Match bare `:Method()` that's not preceded by a word char or `)`
        source = re.sub(
            r'(?<![)\w])(:(?:GetChildren|GetDescendants)\(\))',
            r'script.Parent\1',
            source,
        )
        source = re.sub(
            r'(?<![)\w])(:Cross\()',
            r'Vector3.zero\1',
            source,
        )

    # Fix: `{ --comment` on its own as stray brace (C# block opener with comment)
    # Only when it's the start of a line (not inside an expression)
    if re.search(r'^\s*\{\s*--', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)\{\s*(--.*)',
            r'\1\2',
            source,
            flags=re.MULTILINE,
        )

    # Fix: `clips[key] = expr` as RHS of another assignment
    # `clip = clips[fxName] = AudioClip.Create(...)` → comment (C# chained assignment)
    if re.search(r'=\s*\w+\[.+\]\s*=\s*\w', source):
        source = re.sub(
            r'^(\s*)(\w+\s*=\s*\w+\[.+?\]\s*=\s*.+)$',
            r'\1-- [C# chained assignment] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix: `.SetColumn`/`.SetRow` (Matrix4x4 method) → comment out
    if '.SetColumn(' in source or '.SetRow(' in source:
        source = re.sub(
            r'^(\s*)(\w+\.Set(?:Column|Row)\(.*)$',
            r'\1-- [Unity Matrix] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix: `(if (func(...)) then` → `(if func(...) then` (extra parens break Luau if-expression)
    # Only strip when the condition contains a function call (with nested parens), since
    # `(if (simple_cond) then` is valid Luau but `(if (func(args)) then` confuses the parser
    if '(if (' in source:
        def _fix_if_expr_parens(m):
            full = m.group(0)
            prefix = m.group(1)  # `(if `
            rest = m.group(2)    # `(cond) then ...`
            # Find matching closing paren for the outer condition parens
            depth = 0
            for idx, ch in enumerate(rest):
                if ch == '(':
                    depth += 1
                elif ch == ')':
                    depth -= 1
                    if depth == 0:
                        inner_cond = rest[1:idx]
                        # Only strip if inner condition has nested parens (function call)
                        if '(' in inner_cond:
                            after = rest[idx+1:]
                            return f'{prefix}{inner_cond}{after}'
                        return full  # simple condition, leave as-is
            return full
        source = re.sub(r'(\(if\s+)(\((?:[^()]*|\([^()]*\))*\)\s+then\b)', _fix_if_expr_parens, source)

    # Fix: `or then` at end of if-condition (broken multi-line condition)
    # `elseif (... or then\n  continuation)` → comment out broken line + continuation
    if ' or then' in source:
        lines = source.split('\n')
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if ' or then' in line:
                indent = len(line) - len(line.lstrip())
                pad = ' ' * indent
                result.append(f'{pad}-- [broken condition] {line.strip()}')
                # Comment continuation lines
                while i + 1 < len(lines):
                    next_line = lines[i + 1]
                    next_stripped = next_line.strip()
                    next_indent = len(next_line) - len(next_line.lstrip()) if next_stripped else 0
                    if next_stripped and next_indent > indent and not re.match(r'(?:local|if|for|while|function|end|else|elseif|return)\b', next_stripped):
                        result.append(f'{" " * next_indent}-- [broken condition] {next_stripped}')
                        i += 1
                    else:
                        break
            else:
                result.append(line)
            i += 1
        source = '\n'.join(result)

    # Fix: `.SetData(` / `.GetData(` (Unity AudioClip methods) → comment out
    if '.SetData(' in source or '.GetData(' in source:
        source = re.sub(
            r'^(\s*)(.+\.(?:Set|Get)Data\(.*)$',
            r'\1-- [Unity AudioClip] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix: `AudioClip.Create(` → comment out (Unity-only)
    if 'AudioClip.Create(' in source:
        source = re.sub(
            r'^(\s*)(.+AudioClip\.Create\(.*)$',
            r'\1-- [Unity AudioClip] \2',
            source,
            flags=re.MULTILINE,
        )

    # Fix: `:Play()ClipAtPoint(` → `:Play()` (broken method chain)
    if ':Play()ClipAtPoint(' in source:
        source = re.sub(r':Play\(\)ClipAtPoint\([^)]*\)', ':Play()', source)

    # Fix: broken `new GameObject("name", ...)` → string assignment
    # Pattern: `local go = ("string" .. expr .. "string"\n "extra", "args")`
    # The extra component type args from C# constructor leak as dangling strings
    if re.search(r'local\s+go\s*=\s*\(?"', source):
        lines = source.split('\n')
        result = []
        i = 0
        while i < len(lines):
            line = lines[i]
            # Detect: `local go = ("string ...` without closing `)` on same line
            m_go = re.match(r'^(\s*)(local\s+go\s*=\s*)\((".*\.\.\s*\w+)\s*$', line)
            if m_go:
                indent = m_go.group(1)
                decl = m_go.group(2)
                expr = m_go.group(3)
                result.append(f'{indent}{decl}{expr}')
                # Skip continuation lines that are part of the constructor args
                while i + 1 < len(lines):
                    next_stripped = lines[i + 1].strip()
                    if next_stripped.startswith('"') or next_stripped.startswith("'"):
                        i += 1
                        continue
                    elif next_stripped.startswith('(') and '"' in next_stripped:
                        i += 1
                        continue
                    break
            else:
                # Also fix: `local go =\n    ("string...)` split across lines
                m_go2 = re.match(r'^(\s*)(local\s+go\s*=)\s*$', line)
                if m_go2 and i + 1 < len(lines):
                    next_stripped = lines[i + 1].strip()
                    if next_stripped.startswith('("') and '..' in next_stripped:
                        indent = m_go2.group(1)
                        decl = m_go2.group(2)
                        # Remove leading `(` from string expression
                        expr = next_stripped[1:]  # strip the `(`
                        # Remove trailing `)` if present
                        expr = re.sub(r'\)\s*$', '', expr)
                        result.append(f'{indent}{decl} {expr}')
                        i += 1  # skip the `("string...` line
                        # Skip further continuation lines
                        while i + 1 < len(lines):
                            cont = lines[i + 1].strip()
                            if cont.startswith('"') or (cont.startswith('(') and '"' in cont):
                                i += 1
                                continue
                            break
                    else:
                        result.append(line)
                else:
                    result.append(line)
            i += 1
        new_source = '\n'.join(result)
        if new_source != source:
            source = new_source
            fixes.append("Fixed broken new GameObject constructor patterns")

    # Fix: C# `//` comments → Luau `--` comments
    if '//' in source:
        source = re.sub(r'^(\s*)//(.*)', r'\1--\2', source, flags=re.MULTILINE)

    # Fix: C# Unicode escape `\uXXXX` → Luau `\u{XXXX}`
    if '\\u' in source:
        source = re.sub(r'\\u([0-9a-fA-F]{4})', r'\\u{\1}', source)

    # Comment out TeleportService calls (server-only API, usually in client scripts)
    if 'TeleportService' in source:
        source = re.sub(
            r'^(\s*)(.*TeleportService\s*[:\.].*)',
            r'\1-- [server-only] \2',
            source, flags=re.MULTILINE,
        )

    if source != original:
        fixes.append("Fixed common API mistakes")
        log.info("  [%s] Fixed common API/syntax mistakes", name)

    return source


def _fix_end_closing_table(source: str, fixes: list[str]) -> str:
    """Fix `end` or `end,` used to close table literals instead of `}` or `},`.

    The AI transpiler sometimes uses `end` to close both a table literal and its
    enclosing function block, but Luau requires `}` for tables.

    Handles three patterns:
    1. `end` on its own line after table entries → `}` + `end` (close table, then function)
    2. `end,` inside a table (meant to close a nested table entry) → `},`
    3. Table containing a `function()...end` followed by `end,` → `},`
    """
    lines = source.split('\n')
    changed = False

    # Track open braces to know when we're inside a table literal
    # Use a stack: each entry is the line index of the `{`
    brace_stack: list[int] = []
    new_lines = []

    for idx, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip()) if stripped else 0

        # Track open braces (table openers)
        # Count { and } on this line (ignoring strings)
        no_str = re.sub(r'"[^"]*"', '""', stripped)
        no_str = re.sub(r"'[^']*'", "''", no_str)

        # Handle `end,` — if we're inside a table literal AND there's no unclosed
        # function definition above, this should be `},` (table entry close).
        # But if there IS an unclosed function, `end,` is valid Luau (closes the
        # function as a table value, e.g., `Key = function() ... end,`).
        if re.match(r'^end,\s*$', stripped) or re.match(r'^end,\s*--', stripped):
            if _is_inside_table_context(lines, idx) and not _has_unclosed_function_above(lines, idx):
                suffix = stripped[3:]  # everything after 'end' (the comma + possible comment)
                new_lines.append(line[:indent] + '}' + suffix)
                changed = True
                continue

        # Handle standalone `end` that should be `}` (closes table) + `end` (closes function)
        if stripped == 'end':
            # Look backwards for context
            prev_idx = idx - 1
            while prev_idx >= 0 and (not lines[prev_idx].strip() or lines[prev_idx].strip().startswith('--')):
                prev_idx -= 1
            if prev_idx >= 0:
                prev_stripped = lines[prev_idx].strip()
                # Previous line ends with comma → likely a table entry
                is_after_table_entry = prev_stripped.endswith(',') or prev_stripped.endswith('},')
                # Previous line is `end` closing an inner function inside a table
                is_after_inner_end = prev_stripped == 'end'

                if is_after_table_entry or is_after_inner_end:
                    # Verify there's an unclosed `{` at this indent level or shallower
                    open_brace_indent = _find_unclosed_brace_indent(lines, idx)
                    if open_brace_indent is not None and open_brace_indent <= indent:
                        # This `end` closes both the table and the enclosing function.
                        # Replace with `}` to close the table.
                        # The _append_missing_trailing_ends pass will add the missing
                        # function `end` later if needed.
                        new_lines.append(line[:indent] + '}')
                        changed = True
                        continue

        new_lines.append(line)

    if changed:
        fixes.append("Fixed table literal closed with 'end' instead of '}'")
        return '\n'.join(new_lines)
    return source


def _is_inside_table_context(lines: list[str], idx: int) -> bool:
    """Check if line at idx is inside an open table literal by counting braces."""
    depth = 0
    for i in range(idx - 1, max(idx - 100, -1), -1):
        s = lines[i].strip()
        if s.startswith('--'):
            continue
        no_str = re.sub(r'"[^"]*"', '""', s)
        no_str = re.sub(r"'[^']*'", "''", no_str)
        depth += no_str.count('}') - no_str.count('{')
        if depth < 0:
            return True  # There's an unclosed `{`
        # Stop at function/block boundaries
        if re.match(r'^(?:local\s+)?function\b', s) and '{' not in s:
            break
    return False


def _find_unclosed_brace_indent(lines: list[str], idx: int) -> int | None:
    """Find the indent of the nearest unclosed `{` above idx, or None."""
    depth = 0
    for i in range(idx - 1, max(idx - 100, -1), -1):
        s = lines[i].strip()
        if s.startswith('--'):
            continue
        no_str = re.sub(r'"[^"]*"', '""', s)
        no_str = re.sub(r"'[^']*'", "''", no_str)
        for c in reversed(no_str):
            if c == '}':
                depth += 1
            elif c == '{':
                if depth == 0:
                    return len(lines[i]) - len(lines[i].lstrip())
                depth -= 1
        # Stop at top-level function boundaries
        if re.match(r'^(?:local\s+)?function\b', s) and '{' not in s:
            break
    return None


def _has_unclosed_function_above(lines: list[str], idx: int) -> bool:
    """Check if there's an unclosed `function` definition above idx within the table.

    Walks backwards counting function openers and end closers.
    Returns True if there's a function with no matching end.
    """
    func_depth = 0
    for i in range(idx - 1, max(idx - 100, -1), -1):
        s = lines[i].strip()
        if s.startswith('--'):
            continue
        # Count end keywords (closers)
        if re.match(r'^end\b', s):
            func_depth += 1
        # Count function openers
        if re.search(r'\bfunction\s*\(', s) or re.search(r'\bfunction\s+\w', s):
            if func_depth > 0:
                func_depth -= 1
            else:
                return True  # Unclosed function found
        # Stop at table opener line (we don't look past the current table)
        no_str = re.sub(r'"[^"]*"', '""', s)
        if '{' in no_str and no_str.count('{') > no_str.count('}'):
            break
    return False


def _fix_structural_syntax(name: str, source: str, fixes: list[str]) -> str:
    """Fix structural Luau syntax issues: else if, ++/--, type declarations, etc."""
    original = source

    # Fix `local` declarations inside table literals (from C# enum + class fields mixing)
    # Pattern: table literal `{` is open, then a line has `local varName = ...` inside it
    # Strategy: rewrite the table open line to include closing, move locals outside
    # We detect the pattern and rewrite the opening `= {` to include all valid entries
    # then close the table, followed by the local declarations
    if re.search(r'^\s+local\s+\w+\s*=', source, re.MULTILINE):
        lines = source.split('\n')
        new_lines_tbl = []
        in_table = False
        table_indent = 0
        table_entries = []
        table_open_line = None
        table_close_needed = False
        for li, line in enumerate(lines):
            stripped = line.strip()
            # Detect table opening: `local TableName = {` or `Name = {`
            if not in_table and re.match(r'^(\s*)(?:local\s+)?\w+\s*=\s*\{\s*$', line):
                # Look ahead: does this table contain `local` declarations?
                has_local_inside = False
                for fwd in range(li + 1, min(li + 30, len(lines))):
                    fwd_s = lines[fwd].strip()
                    if re.match(r'local\s+\w+\s*=', fwd_s):
                        has_local_inside = True
                        break
                    if fwd_s == '}' or fwd_s == '},':
                        break
                if has_local_inside:
                    in_table = True
                    table_indent = len(line) - len(line.lstrip())
                    table_open_line = line
                    table_entries = []
                    continue  # Don't add yet; we'll rebuild
                else:
                    new_lines_tbl.append(line)
                    continue
            if table_close_needed and (stripped == '}' or stripped == '},'):
                table_close_needed = False
                continue  # Skip the stale closing brace
            elif in_table:
                if re.match(r'^\s+local\s+\w+\s*=', line) or re.match(r'^\s*local\s+\w+\s*=', line):
                    # First local: emit the table with entries, then close it
                    if table_entries:
                        # Remove trailing comma from last entry
                        last = table_entries[-1].rstrip()
                        if last.endswith(','):
                            table_entries[-1] = last
                        new_lines_tbl.append(table_open_line)
                        new_lines_tbl.extend(table_entries)
                        new_lines_tbl.append(' ' * table_indent + '} -- enum')
                    else:
                        # No valid entries — just `local X = {}`
                        new_lines_tbl.append(table_open_line.rstrip()[:-1] + '{}')
                    in_table = False
                    table_close_needed = True
                    new_lines_tbl.append(line)
                    fixes.append("Closed table literal before `local` declarations")
                    continue
                elif stripped == '}' or stripped == '},':
                    # Normal table close
                    new_lines_tbl.append(table_open_line)
                    new_lines_tbl.extend(table_entries)
                    new_lines_tbl.append(line)
                    in_table = False
                    continue
                else:
                    # A table entry (or comment/blank line inside table)
                    table_entries.append(line)
                    continue
            new_lines_tbl.append(line)
        # If we're still in a table at EOF (missing close), emit what we have
        if in_table:
            new_lines_tbl.append(table_open_line)
            new_lines_tbl.extend(table_entries)
        if new_lines_tbl != lines:
            source = '\n'.join(new_lines_tbl)

    # Fix bare `:Method` in for-in loops: `for _, t in :GetDescendants do`
    # → `for _, t in script.Parent:GetDescendants() do`
    # Also handles malformed: `for _, t in :Method( do)` → `for _, t in script.Parent:Method() do`
    if re.search(r'\bin\s+:', source):
        source = re.sub(
            r'\bin\s+:(\w+)\s+do',
            r'in script.Parent:\1() do',
            source,
        )
        source = re.sub(
            r'\bin\s+:(\w+)\(\)',
            r'in script.Parent:\1()',
            source,
        )
        # Fix malformed: `in :Method( do)` → `in script.Parent:Method() do`
        source = re.sub(
            r'\bin\s+:(\w+)\(\s*do\)',
            r'in script.Parent:\1() do',
            source,
        )
        fixes.append("Fixed bare ':Method' in for-in → script.Parent:Method()")

    # Fix bare `:Method` without parentheses in for-in: `for _, t in obj:GetDescendants do`
    # → `for _, t in obj:GetDescendants() do`
    # Also handles dotted paths: `script.Parent:GetDescendants do`
    if re.search(r'\bin\s+\w+[\w.:]*:\w+\s+do', source):
        source = re.sub(
            r'\bin\s+(\w+(?:\.\w+)*:\w+)\s+do',
            r'in \1() do',
            source,
        )
        fixes.append("Added missing () to method call in for-in loop")

    # Fix extra `)` after table/value assignments: `= {})` → `= {}`
    if re.search(r'=\s*\{\s*\}\)', source):
        source = re.sub(r'(=\s*\{\s*\})\)', r'\1', source)
        fixes.append("Fixed extra ')' after table assignment")

    # Fix forward references: `local x = undefinedVar` where var is defined later
    # Common pattern from C# property backing fields: `local hasKey = gotKey` before `local gotKey`
    # → `local hasKey = nil -- gotKey`
    lines_fr = source.split('\n')
    # Collect all `local varName` definitions and their line numbers
    local_defs = {}
    for li, line in enumerate(lines_fr):
        m = re.match(r'^\s*local\s+(\w+)\s*=', line)
        if m:
            local_defs[m.group(1)] = li
    # Check for forward references
    new_lines_fr = []
    for li, line in enumerate(lines_fr):
        m = re.match(r'^(\s*local\s+\w+\s*=\s*)(\w+)\s*$', line)
        if m:
            ref_var = m.group(2)
            # Is ref_var a local defined LATER in the file (forward reference)?
            if ref_var in local_defs and local_defs[ref_var] > li:
                # Also check it's not a known global/builtin
                if ref_var not in ('nil', 'true', 'false', 'math', 'string', 'table',
                                   'game', 'workspace', 'script', 'Instance', 'Vector3',
                                   'Vector2', 'CFrame', 'Color3', 'Enum', 'task', 'require',
                                   'tostring', 'tonumber', 'type', 'typeof', 'print', 'warn',
                                   'error', 'pairs', 'ipairs', 'next', 'select', 'unpack',
                                   'pcall', 'xpcall', 'coroutine', 'bit32', 'os', 'debug'):
                    line = f'{m.group(1)}nil -- {ref_var} (forward ref)'
                    fixes.append(f"Fixed forward reference: {ref_var}")
        new_lines_fr.append(line)
    if new_lines_fr != lines_fr:
        source = '\n'.join(new_lines_fr)

    # Fix 'else if' → 'elseif' (Luau keyword)
    if 'else if ' in source:
        source = re.sub(r'\belse\s+if\b', 'elseif', source)
        fixes.append("Fixed 'else if' → 'elseif'")

    # Fix `end` immediately before `elseif` or `else` (from C# `} else if {` / `} else {`)
    # Line-based approach: find `end` lines followed by `elseif`/`else` lines and remove the `end`
    lines = source.split('\n')
    new_lines = []
    i = 0
    end_else_fixed = False
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == 'end' and i + 1 < len(lines):
            next_stripped = lines[i + 1].lstrip()
            if next_stripped.startswith('elseif ') or next_stripped.startswith('elseif(') or next_stripped == 'else' or next_stripped.startswith('else ') or next_stripped.startswith('else\n'):
                # Skip this `end` line — the elseif/else continues the block
                end_else_fixed = True
                i += 1
                continue
        new_lines.append(lines[i])
        i += 1
    if end_else_fixed:
        source = '\n'.join(new_lines)
        fixes.append("Fixed end before elseif/else → merged")

    # Strip stray `{` braces at start of line (C# block openers that survived conversion)
    if re.search(r'^\s*\{\s*$', source, re.MULTILINE):
        source = re.sub(r'^\s*\{\s*$', '', source, flags=re.MULTILINE)
        fixes.append("Stripped stray { braces")
    # Strip standalone `}` but NOT `} -- enum/class` or inside tables
    if re.search(r'^\s*\}\s*$', source, re.MULTILINE):
        # Only strip if not preceded by a table entry (key = value,)
        lines_brace = source.split('\n')
        new_lines_brace = []
        for idx_b, bl in enumerate(lines_brace):
            if bl.strip() == '}':
                # Check if preceded by table-like entries
                prev_is_table = False
                for k in range(idx_b - 1, max(idx_b - 5, -1), -1):
                    pk = lines_brace[k].strip()
                    if not pk or pk.startswith('--'):
                        continue
                    if re.match(r'\w+\s*=\s*.+,\s*$', pk) or pk.endswith(','):
                        prev_is_table = True
                    break
                if not prev_is_table:
                    new_lines_brace.append('')
                    continue
            new_lines_brace.append(bl)
        source = '\n'.join(new_lines_brace)

    # Strip inline `{ ` after `then` / `do` (C# block opener inside Luau control flow)
    # e.g., `if (cond) then { return false end` → `if (cond) then return false end`
    if re.search(r'\b(?:then|do)\s*\{', source):
        source = re.sub(r'\b(then|do)\s*\{\s*', r'\1 ', source)
        fixes.append("Stripped inline { after then/do")

    # Strip inline `; }` (C# semicolon + brace from block closer)
    if '; }' in source:
        source = re.sub(r';\s*\}\s*(?=--|\s*$)', ' ', source)
        fixes.append("Stripped inline ; } patterns")

    # Fix double-paren function calls: `Method((args))` → `Method(args)`
    # From C# tuple arguments: `Publish(new Tuple(a, b))` → `Publish((a, b))` → `Publish(a, b)`
    if re.search(r'\w\(\(', source):
        source = re.sub(r'(\w)\(\(([^)]*)\)\)', r'\1(\2)', source)
        fixes.append("Stripped inline { after then/do")

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

    # Second pass: fix script.Parent as function parameter (after gameObject/this→script.Parent)
    if 'script.Parent)' in source or 'script.Parent,' in source:
        def _fix_sp_param(m):
            prefix = m.group(1)
            params = m.group(2)
            params = re.sub(r'\bscript\.Parent\b', 'obj', params)
            return f'{prefix}{params})'
        source = re.sub(
            r'((?:local\s+)?function\s+\w+\()([^)]*\bscript\.Parent\b[^)]*)\)',
            _fix_sp_param,
            source,
        )

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
    # Standalone attribute lines (with optional namespace prefix like UnityEngine.Serialization.)
    if re.search(rf'^\s*\[(?:[\w.]*\.)?(?:{_ATTR_PATTERN})\b', source, re.MULTILINE):
        source = re.sub(
            rf'^\s*\[(?:[\w.]*\.)?(?:{_ATTR_PATTERN})\b[^\]]*\]\s*\n',
            '',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Stripped C# [Attribute] annotations")
    # Inline attributes at start of line (before variable declarations)
    # e.g., "[SerializeField][Range(0.5, 3)] local ..." → "local ..."
    if re.search(rf'\[(?:[\w.]*\.)?(?:{_ATTR_PATTERN})\b', source):
        source = re.sub(rf'\[(?:[\w.]*\.)?(?:{_ATTR_PATTERN})\b[^\]]*\]\s*', '', source)
        fixes.append("Stripped inline C# [Attribute] annotations")

    # Fix `local script.Parent = ...` → remove (invalid; script.Parent already exists)
    if 'local script.Parent' in source:
        source = re.sub(
            r'^\s*local script\.Parent\s*=.*$',
            '',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Removed invalid 'local script.Parent' declaration")

    # Fix stray `. method()` (dot-space-method from mangled output)
    if re.search(r'\.\s+\w+[:(]', source):
        source = re.sub(
            r'then\s+\.\s+(\w+)',
            r'then \1',
            source,
        )
        fixes.append("Fixed stray dot-space before method call")

    # Comment out broken C# override property patterns:
    # `ClassName function(_x) return (ClassName end)base.Property;`
    if re.search(r'\w+ function\(\w+\) return \(', source):
        source = re.sub(
            r'^(\s*)\w+ function\(\w+\) return \(.*$',
            r'\1-- [C# override property] (removed)',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out broken C# override property patterns")

    # Comment out C# interface method declarations: `T Create()`, `T Request()`
    if re.search(r'^\s+[A-Z]\w*\s+\w+\(\)\s*$', source, re.MULTILINE):
        source = re.sub(
            r'^(\s+)([A-Z]\w*\s+\w+\(\))\s*$',
            r'\1-- [C# interface] \2',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out C# interface method declarations")

    # Comment out C# operator overloads: `local operator ==(Type x, Type y)`
    if 'operator ==' in source or 'operator !=' in source:
        source = re.sub(
            r'^(\s*)local operator\s*[!=]=.*$',
            r'\1-- [C# operator] (removed)',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Commented out C# operator overloads")

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

    # Fix Unity input axis names passed to IsKeyDown (should be Enum.KeyCode or proper Roblox API)
    # Unity: Input.GetButton("Jump") → Roblox: UserInputService:IsKeyDown(Enum.KeyCode.Space)
    _UNITY_INPUT_MAP = {
        '"Jump"': 'Enum.KeyCode.Space',
        '"Fire"': 'Enum.UserInputType.MouseButton1',
        '"Fire1"': 'Enum.UserInputType.MouseButton1',
        '"Fire2"': 'Enum.UserInputType.MouseButton2',
        '"Fire3"': 'Enum.UserInputType.MouseButton3',
        '"Cancel"': 'Enum.KeyCode.Escape',
        '"Submit"': 'Enum.KeyCode.Return',
        '"Use"': 'Enum.KeyCode.E',
        '"Brake"': 'Enum.KeyCode.Space',
        '"RB"': 'Enum.KeyCode.LeftShift',
        '"Crouch"': 'Enum.KeyCode.LeftControl',
        '"Sprint"': 'Enum.KeyCode.LeftShift',
        '"Pause"': 'Enum.KeyCode.P',
        '"Menu"': 'Enum.KeyCode.Escape',
        '"Interact"': 'Enum.KeyCode.E',
        '"Reload"': 'Enum.KeyCode.R',
        '"Inventory"': 'Enum.KeyCode.I',
        '"Map"': 'Enum.KeyCode.M',
        '"Tab"': 'Enum.KeyCode.Tab',
    }
    for unity_name, roblox_key in _UNITY_INPUT_MAP.items():
        if unity_name in source:
            # IsKeyDown("Jump") → IsKeyDown(Enum.KeyCode.Space)
            # MouseButton types: use GetMouseButtonsPressed (IsMouseButtonPressed
            # is not a valid Roblox API)
            if 'MouseButton' in roblox_key:
                source = source.replace(
                    f'IsKeyDown({unity_name})',
                    f'_isMouseButtonDown({roblox_key})',
                )
            else:
                source = source.replace(f'IsKeyDown({unity_name})', f'IsKeyDown({roblox_key})')
            fixes.append(f"Fixed Unity input {unity_name} → {roblox_key}")

    # Fix invalid IsMouseButtonPressed calls (not a valid Roblox API).
    # Strip the UserInputService: prefix and replace with helper function.
    if 'IsMouseButtonPressed' in source:
        source = re.sub(
            r'(?:UserInputService:|UIS:)?IsMouseButtonPressed\(',
            '_isMouseButtonDown(',
            source,
        )
        fixes.append("Fixed invalid IsMouseButtonPressed → _isMouseButtonDown helper")

    # Also fix any _isMouseButtonDown that ended up as a method call
    if 'UserInputService:_isMouseButtonDown' in source or 'UIS:_isMouseButtonDown' in source:
        source = source.replace('UserInputService:_isMouseButtonDown', '_isMouseButtonDown')
        source = source.replace('UIS:_isMouseButtonDown', '_isMouseButtonDown')

    # Inject helper function if needed
    if '_isMouseButtonDown' in source and 'local function _isMouseButtonDown' not in source:
        helper = (
            'local function _isMouseButtonDown(btn)\n'
            '    local UIS = game:GetService("UserInputService")\n'
            '    for _, input in UIS:GetMouseButtonsPressed() do\n'
            '        if input.UserInputType == btn then return true end\n'
            '    end\n'
            '    return false\n'
            'end\n'
        )
        # Insert at top after services (find the LAST top-level GetService
        # line, not any nested ones). Stop at the first non-top-level line.
        lines = source.split('\n')
        insert_idx = 0
        for i, line in enumerate(lines):
            stripped = line.lstrip()
            if line == stripped and 'GetService' in line:  # top-level only
                insert_idx = i + 1
            elif line == stripped and stripped and not stripped.startswith('--') and not stripped.startswith('local '):
                break
        lines.insert(insert_idx, helper)
        source = '\n'.join(lines)
        fixes.append("Injected _isMouseButtonDown helper")

    # Catch-all: any remaining IsKeyDown("StringName") → Enum.KeyCode.StringName
    # This handles unmapped Unity input axis names
    if re.search(r'IsKeyDown\s*\("(\w+)"\)', source):
        def _fix_string_keydown(m):
            name = m.group(1)
            return f'IsKeyDown(Enum.KeyCode.{name})'
        source = re.sub(
            r'IsKeyDown\s*\("(\w+)"\)',
            _fix_string_keydown,
            source,
        )
        fixes.append("Fixed string-based IsKeyDown → Enum.KeyCode")

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
    # Also handles: `Type Name { function(get) return ...` (from transpiler's property conversion)
    if re.search(r'^\s*(?:bool|int|float|string|[\w.]+)\s+\w.*\{\s*(?:get\s*[\{;]|function\(get\))', source, re.MULTILINE):
        source = re.sub(
            r'^(\s*)((?:bool|int|float|string|[\w.]+)\s+\w.*\{\s*(?:get\s*[\{;]|function\(get\)).*)$',
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

    # Fix Unity FindObjectOfType/FindAnyObjectByType → workspace:FindFirstChildWhichIsA("Type")
    if 'FindAnyObjectByType(' in source or 'FindObjectOfType(' in source:
        # Typed version: FindObjectOfType("Type") → workspace:FindFirstChildWhichIsA("Type", true)
        source = re.sub(
            r'(?:FindAnyObjectByType|FindObjectOfType)\("(\w+)"\)',
            r'workspace:FindFirstChildWhichIsA("\1", true)',
            source,
        )
        # Bare version (no type arg): just use workspace
        source = re.sub(
            r'(?:FindAnyObjectByType|FindObjectOfType)\(\)',
            'workspace',
            source,
        )

    # Fix FindObjectsOfType("Type") → type-filtered GetDescendants
    if 'FindObjectsOfType(' in source:
        def _replace_find_objects(m):
            type_name = m.group(1)
            # Generate inline filter expression
            return (f'(function() local _r = {{}} for _, _d in workspace:GetDescendants() do '
                    f'if _d:IsA("{type_name}") then table.insert(_r, _d) end end return _r end)()')
        source = re.sub(
            r'(?:FindObjectsOfType)\("(\w+)"\)',
            _replace_find_objects,
            source,
        )

    # Fix TryGetComponent("Type", out var) → local var = obj:FindFirstChildWhichIsA("Type")
    if 'TryGetComponent(' in source:
        # Pattern: var = obj.TryGetComponent("Type", var) → var = obj:FindFirstChildWhichIsA("Type")
        source = re.sub(
            r'(?:local\s+)?(\w+)\s*=\s*(\w+)\.TryGetComponent\("(\w+)",\s*\w+\)',
            r'local \1 = \2:FindFirstChildWhichIsA("\3")',
            source,
        )
        # Pattern: obj.TryGetComponent("Type", var) as expression → obj:FindFirstChildWhichIsA("Type")
        source = re.sub(
            r'(\w+)\.TryGetComponent\("(\w+)",\s*(\w+)\)',
            r'\1:FindFirstChildWhichIsA("\2")',
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

    # Fix incomplete constructors: `local var = nil -- (constructor removed)` followed by table entries
    # Pattern: the comment tells us a C# constructor was removed, but the initializer block remains
    # Fix: replace `nil -- (constructor removed)` with `{` to open the table literal
    if '-- (constructor removed)' in source or '-- new Type' in source:
        # Pattern: `local var = nil -- (constructor removed)` + next lines are table entries + closing `}`
        lines_ctor = source.split('\n')
        new_lines_ctor = []
        for ci, cline in enumerate(lines_ctor):
            if '= nil -- (constructor removed)' in cline or re.search(r'= nil -- new \w+', cline):
                # Check if next non-blank line looks like a table entry (key = value,)
                next_idx = ci + 1
                while next_idx < len(lines_ctor) and not lines_ctor[next_idx].strip():
                    next_idx += 1
                if next_idx < len(lines_ctor) and re.match(r'^\s+\w+\s*=\s*.+,?\s*$', lines_ctor[next_idx]):
                    # Replace nil with { to open table
                    cline = re.sub(r'= nil -- .*$', '= {', cline)
                    fixes.append("Fixed incomplete constructor → table literal")
            new_lines_ctor.append(cline)
        source = '\n'.join(new_lines_ctor)

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

    # Fix malformed list initialization: "--[[ new List ]] (N)" or "--[[ new List ]] ({...})" → "{}"
    # Also handles nested parens like "--[[ new List ]] (script.Parent:GetDescendants())"
    if '--[[ new List ]]' in source or '--[[ new HashSet ]]' in source:
        source = re.sub(
            r'--\[\[\s*new\s+(?:List|HashSet)\s*\]\]\s*\((?:[^()]*\([^)]*\))*[^)]*\)',
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
        # new Dictionary<K,V>(...) / new List<T>(...) / new HashSet<T>(...) → {}
        # Use (?:<.*?>)? for nested generics like Dictionary<string, List<int>>
        source = re.sub(
            r'\bnew\s+[\w.]*(?:Dictionary|List|HashSet|Queue|Stack|ArrayList)(?:<.+?>)?\s*\([^)]*\)',
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
        # `return new TypeName` (no parens or braces) → `return {}`
        source = re.sub(
            r'\breturn\s+new\s+[A-Z]\w+\s*$',
            'return {}',
            source,
            flags=re.MULTILINE,
        )
        # `= new TypeName` at end of line (no parens/braces) → `= {}`
        source = re.sub(
            r'=\s*new\s+[A-Z][\w.]+\s*$',
            '= {}',
            source,
            flags=re.MULTILINE,
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

    # Strip trailing `)` from lines with unbalanced parentheses (run last)
    # e.g., `return x==y)` → `return x==y`
    def _fix_unbal_parens(line):
        s = line.strip()
        if not s or s.startswith('--') or s.startswith('end'):
            return line
        # Skip continuation lines (from multi-line expressions)
        if s.startswith('"') or s.startswith("'") or s.startswith('and ') or s.startswith('or '):
            return line
        opens = s.count('(')
        closes = s.count(')')
        # Only fix if the line itself has at least one `(` (not a pure continuation)
        if opens == 0 and closes > 0:
            return line  # Continuation line, don't strip
        if closes > opens and (s.endswith(')') or s.endswith(') then') or s.endswith(') do')):
            for _ in range(closes - opens):
                idx = s.rfind(')')
                if idx >= 0:
                    s = s[:idx] + s[idx+1:]
            indent = line[:len(line) - len(line.lstrip())]
            return indent + s
        return line
    new_lines_paren = [_fix_unbal_parens(l) for l in source.split('\n')]
    new_source_paren = '\n'.join(new_lines_paren)
    if new_source_paren != source:
        source = new_source_paren
        fixes.append("Fixed unbalanced trailing parentheses")

    # --- Fix table literals closed with `end` instead of `}` ---
    # Pattern: `return {` or `local X = {` followed by table entries, then `end`
    # instead of `}`.  The `end` is meant to close both the table AND the enclosing
    # function, but Luau needs `}` for the table and a separate `end` for the function.
    # Also handles `end,` inside tables (should be `},`).
    source = _fix_end_closing_table(source, fixes)

    # --- Fix trailing comma before `)` from bare-var comment removal ---
    # Pattern: `(arg1, arg2, )` or `(arg1, arg2,\n  )` from commented-out args
    if re.search(r',\s*\)', source):
        source = re.sub(r',(\s*)\)', r'\1)', source)
        fixes.append("Fixed trailing comma before closing parenthesis")

    # Also handle: trailing comma followed by comment-only lines before `)`
    # e.g., `y,\n    -- [bare var] 0\n)`
    if re.search(r',\s*\n(?:\s*--[^\n]*\n)*\s*\)', source):
        source = re.sub(
            r',(\s*\n(?:\s*--[^\n]*\n)*\s*\))',
            r'\1',
            source,
        )
        fixes.append("Fixed trailing comma before comment + closing paren")

    # --- Fix assignment to function call result (L-value error) ---
    # Pattern: `SomeFunc(args) = value` — invalid, comment it out
    # Uses a line-by-line approach to handle nested parens properly.
    lines_lv = source.split('\n')
    new_lines_lv = []
    changed_lv = False
    for lv_line in lines_lv:
        lv_stripped = lv_line.strip()
        if lv_stripped and not lv_stripped.startswith('--'):
            # Check for pattern: identifier.method(anything) = value
            # by finding the first `(`, matching to its `)`, then checking for ` = `
            m_lv = re.match(r'^(\s*)(\w+[\w.:]*)\(', lv_line)
            if m_lv:
                start = m_lv.end() - 1  # position of `(`
                depth_lv = 0
                end_paren = None
                for ci in range(start, len(lv_line)):
                    if lv_line[ci] == '(':
                        depth_lv += 1
                    elif lv_line[ci] == ')':
                        depth_lv -= 1
                        if depth_lv == 0:
                            end_paren = ci
                            break
                if end_paren is not None:
                    after = lv_line[end_paren + 1:].lstrip()
                    if after.startswith('=') and not after.startswith('=='):
                        indent_lv = m_lv.group(1)
                        new_lines_lv.append(f'{indent_lv}-- [invalid L-value] {lv_stripped}')
                        changed_lv = True
                        fixes.append("Commented out assignment to function call result")
                        continue
        new_lines_lv.append(lv_line)
    if changed_lv:
        source = '\n'.join(new_lines_lv)

    # --- Fix `function table.remove(ClassName, ...)` broken declaration ---
    # AI sometimes wraps the unregister/remove call as a function declaration
    # Pattern: `function table.remove(X, table.find(X, ...))` → normal function
    if 'function table.remove(' in source or 'function table.insert(' in source:
        source = re.sub(
            r'^(\s*)function\s+table\.(?:remove|insert)\((\w+),\s*table\.find\([^)]+\)\)',
            r'\1function \2.Unregister()',
            source,
            flags=re.MULTILINE,
        )
        fixes.append("Fixed broken function table.remove declaration")

    return source


def _fix_missing_ends_in_blocks(source: str, fixes: list[str]) -> str:
    """Detect and fix missing 'end' keywords in if/function blocks.

    Analyzes indentation to find blocks that are missing their closing 'end'.
    """
    # This is handled by _fix_missing_end_keywords — just a hook for future improvements
    return source


def _fix_missing_function_end(name: str, source: str, fixes: list[str]) -> str:
    """Insert missing `end` between consecutive local functions.

    When a function ends with `return` but has no closing `end`, and the next
    `local function` starts at same or lesser indentation, insert `end`.
    Runs AFTER _fix_missing_end_keywords to avoid being removed by excess-end logic.
    """
    if not re.search(r'return\s+\w+.*\n(?:\s*\n)*\s*local\s+function\s+', source):
        return source

    lines = source.split('\n')
    result = []
    for i, line in enumerate(lines):
        result.append(line)
        if re.match(r'\s+return\s+\w+', line):
            j = i + 1
            has_end_between = False
            while j < len(lines) and lines[j].strip() == '':
                j += 1
            # Check if there's an `end` between return and next function
            if j < len(lines) and lines[j].strip() == 'end':
                has_end_between = True
                j += 1
                while j < len(lines) and lines[j].strip() == '':
                    j += 1
            if not has_end_between and j < len(lines) and re.match(r'\s+local\s+function\s+', lines[j]):
                ret_indent = len(line) - len(line.lstrip())
                func_indent = len(lines[j]) - len(lines[j].lstrip())
                if func_indent <= ret_indent:
                    result.append(f'{" " * func_indent}end')
    new_source = '\n'.join(result)
    if new_source != source:
        fixes.append("Fixed missing function end before next local function")
    return new_source


def _fix_undefined_module_return(name: str, source: str, fixes: list[str]) -> str:
    """Add module table definition when script returns an undefined name.

    If a script ends with `return ClassName` but never defines `local ClassName`,
    insert `local ClassName = {}` after the service/require declarations at the top.
    """
    lines = source.rstrip().split('\n')
    # Find last non-empty, non-comment line
    return_name = None
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('--'):
            continue
        m = re.match(r'^return\s+([A-Z]\w+)\s*$', stripped)
        if m:
            return_name = m.group(1)
        break

    if not return_name:
        return source

    # Check if the name is already defined
    if re.search(rf'\blocal\s+{re.escape(return_name)}\s*=', source):
        return source

    # Insert table definition after service/require declarations
    src_lines = source.split('\n')
    insert_idx = 0
    for i, line in enumerate(src_lines):
        ls = line.strip()
        if ls.startswith('local ') and ('GetService' in ls or 'require' in ls):
            insert_idx = i + 1
        elif ls == '' and insert_idx > 0:
            insert_idx = i + 1
            break
        elif insert_idx == 0 and ls and not ls.startswith('--'):
            break

    src_lines.insert(insert_idx, f'local {return_name} = {{}}\n')
    fixes.append(f"Added module table definition for '{return_name}'")
    return '\n'.join(src_lines)


def _fix_missing_module_return(name: str, source: str, fixes: list[str]) -> str:
    """Add `return ClassName` to scripts that define a module table but never return it.

    Pattern: `local ClassName = {}` at top, but no `return ClassName` at end.
    Prefers the class that matches the script name.
    """
    # Check if there's already a module-level return at the end
    # (return ClassName, not return from inside a function)
    last_lines = source.rstrip().split('\n')
    for line in reversed(last_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('--'):
            continue
        if stripped == 'end':
            break  # Last real line is `end` — likely a function closer, not a module return
        m_ret = re.match(r'^return\s+([A-Z]\w+)\s*$', stripped)
        if m_ret:
            return source  # Already has a proper module return
        if stripped.startswith('return '):
            break  # Has a return but not a module-level one (e.g., inside a function)
        break

    # Find all `local Name = {}` near the top (within first 50 lines)
    candidates = []
    for line in source.split('\n')[:50]:
        m = re.match(r'^local\s+([A-Z]\w+)\s*=\s*\{\s*\}\s*$', line.strip())
        if m:
            candidates.append(m.group(1))

    if not candidates:
        return source

    # Prefer the one matching the script name
    # Script name is the filename without .luau
    script_name = name.replace('.luau', '').replace('.lua', '')
    module_name = None
    for c in candidates:
        if c == script_name:
            module_name = c
            break
    if not module_name:
        module_name = candidates[0]  # Fallback to first

    # Add return at the end
    source = source.rstrip() + f'\n\nreturn {module_name}\n'
    fixes.append(f"Added missing 'return {module_name}' for module script")
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
        # But skip `}` that close table literals (preceded by table entry lines like `Key = Value,`)
        if stripped == '}' or stripped == '};':
            # Check if this closes a table literal by looking back for table entries
            is_table_close = False
            indent = len(lines[i]) - len(lines[i].lstrip())
            for back_idx in range(len(result) - 1, max(len(result) - 20, -1), -1):
                back_line = result[back_idx].strip()
                if not back_line or back_line.startswith('--'):
                    continue  # skip blanks/comments
                # Table entry patterns: `Key = Value,` or `Value,` or `[expr] = value,`
                if re.match(r'^\w+\s*=\s*.+,?\s*$', back_line) and 'local ' not in back_line:
                    is_table_close = True
                    break
                # Array-style table entries: lines ending with `,` that look like
                # function calls, string literals, numbers, or closing braces
                if back_line.endswith(',') and (
                    re.match(r'^[\w.]+\(', back_line) or  # func(args),
                    re.match(r'^[\'"{\d]', back_line) or  # string/table/number,
                    back_line.endswith('},')               # nested table },
                ):
                    is_table_close = True
                    break
                # Also use brace counting: if there's an unclosed `{` above, it's a table
                if back_line.endswith(','):
                    if _is_inside_table_context(result, len(result)):
                        is_table_close = True
                        break
                # Table opening: `= {` at same or lower indent
                if back_line.endswith('= {') or back_line.endswith('= {},'):
                    is_table_close = True
                    break
                # `return {` at same or lower indent
                if back_line == 'return {' or back_line.endswith('return {'):
                    is_table_close = True
                    break
                break  # First meaningful non-blank line doesn't look like a table
            if is_table_close:
                result.append(lines[i])  # Keep the `}` as-is (table close)
            else:
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
    # NOTE: _remove_excess_trailing_ends disabled — its block depth counting
    # is unreliable with complex patterns (nested closures, do blocks, etc.)
    # and it removes valid `end` keywords, causing syntax errors.
    # source = _remove_excess_trailing_ends(source, fixes)

    # Fix unclosed `do` blocks: scan for bare `do` lines and ensure
    # they have a matching `end` before the next top-level statement.
    # We track ALL block openers/closers (not just `do`) so that inner
    # `end` keywords (e.g. closing an `if` inside a `do`) don't falsely
    # consume the `do` block's closer.
    lines = source.split('\n')
    fixed_lines = []
    do_stack = []  # (line_index, indent) of open do blocks
    inner_depth = 0  # depth of non-do block openers inside a do block
    for i, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip()) if line.strip() else 0
        if stripped.startswith('--'):
            fixed_lines.append(line)
            continue

        code_part = re.sub(r'\s*--.*$', '', stripped).rstrip()

        # Track do blocks and inner block openers/closers
        if stripped == 'do':
            do_stack.append((i, indent))
            inner_depth = 0
        elif do_stack:
            # Count inner block openers (if/then, for/do, while/do, function, repeat)
            if (re.match(r'if\b.+\bthen\s*$', code_part) or
                    re.match(r'for\b.+\bdo\s*$', code_part) or
                    re.match(r'while\b.+\bdo\s*$', code_part) or
                    re.search(r'\brepeat\s*$', code_part)):
                inner_depth += 1
            if re.search(r'\bfunction\s*[\w.:(]', code_part):
                func_count = len(re.findall(r'\bfunction\s*[\w.:(]', code_part))
                end_count_inline = len(re.findall(r'\bend\b', code_part))
                inner_depth += max(0, func_count - end_count_inline)
            # Count closers
            if stripped in ('end', 'end)', 'end,') or stripped.startswith('end '):
                if inner_depth > 0:
                    inner_depth -= 1
                else:
                    # This end closes the do block
                    do_stack.pop()

        # If we hit a top-level statement and there's an unclosed do block
        # at the same or lower indent, insert end before this line.
        # Skip when the current line is `do` (we just pushed it).
        if (do_stack and stripped != 'do' and not stripped.startswith('end')
                and stripped and not stripped.startswith('--')):
            while do_stack and do_stack[-1][1] >= indent:
                do_indent = do_stack.pop()[1]
                fixed_lines.append(' ' * do_indent + 'end')
                fixes.append("Added missing end for do block")
                inner_depth = 0

        fixed_lines.append(line)

    # Close any remaining do blocks
    while do_stack:
        do_indent = do_stack.pop()[1]
        fixed_lines.append(' ' * do_indent + 'end')
        fixes.append("Added missing end for do block")

    source = '\n'.join(fixed_lines)

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
        # Strip trailing comments for block-structure analysis
        code_part = re.sub(r'\s*--.*$', '', stripped).rstrip()
        if re.search(r'\bfunction\s*[\w.:(]', stripped):
            # Count function openers vs end closers on this line
            func_count = len(re.findall(r'\bfunction\s*[\w.:(]', stripped))
            end_count_inline = len(re.findall(r'\bend\b', stripped))
            depth += max(0, func_count - end_count_inline)
        if re.match(r'if\b.+\bthen\s*$', code_part) or (
                re.search(r'\bthen\s*$', code_part) and not re.match(r'(?:if|elseif)\b', code_part)
                and not re.search(r'\bfunction\b', code_part)):
            depth += 1
        if re.match(r'for\b.+\bdo\s*$', code_part):
            depth += 1
        if re.match(r'while\b.+\bdo\s*$', code_part):
            depth += 1
        if code_part == 'do':
            depth += 1
        if code_part == 'repeat':
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

        # Strip trailing comments for block-structure analysis
        code_part = re.sub(r'\s*--.*$', '', stripped).rstrip()

        # Count block openers
        if re.search(r'\bfunction\s*[\w.:(]', stripped):
            # Skip single-line functions: function() ... end anywhere on the same line
            # Count function openers vs end closers on this line
            func_count = len(re.findall(r'\bfunction\s*[\w.:(]', stripped))
            end_count = len(re.findall(r'\bend\b', stripped))
            # Only count as opener if functions outnumber ends on this line
            depth += max(0, func_count - end_count)
        if re.match(r'if\b.+\bthen\s*$', code_part) or (
                re.search(r'\bthen\s*$', code_part) and not re.match(r'(?:if|elseif)\b', code_part)
                and not re.search(r'\bfunction\b', code_part)):
            depth += 1
        if re.match(r'for\b.+\bdo\s*$', code_part):
            depth += 1
        if re.match(r'while\b.+\bdo\s*$', code_part):
            depth += 1
        if code_part == 'do':
            depth += 1
        if code_part == 'repeat':
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
    """Fix bare `end` → `end)` for callback-function blocks.

    Handles :Connect(function(), task.delay(time, function(), task.spawn(function(),
    and task.defer(function() patterns.

    Uses a two-pass approach:
    1. Find all callback-function( lines and record their indent
    2. For each opener, find the matching `end` via depth tracking
       and convert it to `end)`

    This runs after all structural fixes so the block structure is stable.
    """
    # Quick check: does the source contain any callback-function patterns?
    has_connect = ':Connect(function(' in source
    has_task = re.search(r'task\.(delay|spawn|defer)\(.*function\s*\(', source) is not None
    if not has_connect and not has_task:
        return source

    lines = source.split('\n')
    # Find all callback-function openers with their line index and indent
    # Matches: :Connect(function(, task.delay(time, function(, task.spawn(function(, task.defer(function(
    callback_pattern = re.compile(r'(?::Connect\(function\s*\(|task\.(?:delay|spawn|defer)\(.*function\s*\()')
    connect_openers = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if callback_pattern.search(stripped):
            # Skip single-line callbacks (function(...) ... end) on same line
            if re.search(r'\bend\)\s*$', stripped):
                continue
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
    ends_to_fix = {}  # line index → target string ('end)' or 'end')

    for open_idx, open_indent in connect_openers:
        # Find the matching end for the function opened by this Connect
        # We scan forward, tracking block depth. The function's closing end
        # is when depth returns to 0. Any end) at depth > 0 is wrong (should
        # be bare end), and the closer at depth 0 should be end).
        depth = 0
        found = False
        inner_ends = []  # track (line_idx, current_text) for closers at depth > 0
        for j in range(open_idx, len(lines)):
            stripped = lines[j].strip()
            if stripped.startswith('--') or not stripped:
                continue

            # Count block openers
            if re.search(r'\bfunction\s*[\w.:(]', stripped):
                if not re.search(r'\bend\)?[,;]?\s*$', stripped):
                    depth += 1
            elif re.match(r'if\b.+\bthen\s*$', stripped) or (
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
                    # This closer should be end) (closes the Connect function)
                    if stripped == 'end':
                        ends_to_fix[j] = 'end)'
                    found = True
                    break
                else:
                    # This closer is inside the function body — should be bare end
                    if stripped.startswith('end)') and stripped != 'end':
                        inner_ends.append(j)
            elif re.match(r'until\b', stripped):
                depth -= 1

        # NOTE: Previously converted inner end) → end, assuming they close
        # if/for/while blocks. But nested Connect(function()...end) calls
        # also produce inner end) that IS correct. Disabling this conversion
        # to avoid breaking nested closures.
        # for idx in inner_ends:
        #     ends_to_fix[idx] = 'end'

        # If no matching end found and depth > 0, insert end)
        if not found and depth > 0:
            insert_idx = len(lines)
            for j in range(len(lines) - 1, open_idx, -1):
                if lines[j].strip():
                    insert_idx = j + 1
                    break
            lines.insert(insert_idx, ' ' * (open_indent) + 'end)')

    # Apply fixes
    fix_count = 0
    if ends_to_fix:
        for idx in sorted(ends_to_fix):
            line = lines[idx]
            indent = len(line) - len(line.lstrip()) if line.strip() else 0
            target = ends_to_fix[idx]
            lines[idx] = ' ' * indent + target
            fix_count += 1
        fixes.append(f"Fixed {fix_count} Connect(function) closure(s): corrected end/end) placement")

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
        # Strip trailing comments for block-structure analysis
        code_part = re.sub(r'\s*--.*$', '', stripped).rstrip()
        # Count block openers (skip single-line definitions like "function() ... end")
        if re.search(r'\bfunction\s*[\w.:(]', stripped):
            # Count function openers vs end closers on this line
            func_count = len(re.findall(r'\bfunction\s*[\w.:(]', stripped))
            end_count_inline = len(re.findall(r'\bend\b', stripped))
            depth += max(0, func_count - end_count_inline)
        if re.match(r'if\b.+\bthen\s*$', code_part) or (
                re.search(r'\bthen\s*$', code_part) and not re.match(r'(?:if|elseif)\b', code_part)
                and not re.search(r'\bfunction\b', code_part)):
            depth += 1
        if re.match(r'for\b.+\bdo\s*$', code_part):
            depth += 1
        if re.match(r'while\b.+\bdo\s*$', code_part):
            depth += 1
        if code_part == 'repeat':
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
        is_closer = stripped == 'end' or stripped.startswith('end)')
        if indent >= 0 and not is_closer and stripped not in ('else',) and not stripped.startswith('elseif '):
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

        # Handle 'end' or 'end)' keyword — pop the stack
        # If the end is at a lower indent than the top block, insert missing ends first
        if is_closer:
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
        # and open a new one at the same level.
        # First, close any deeper nested blocks that are still open (missing `end`).
        if stripped == 'else' or stripped.startswith('elseif '):
            # Close all blocks on the stack that are deeper than this else/elseif
            while block_stack and block_stack[-1][0] > indent:
                top_indent, _top_type = block_stack[-1]
                result.append(' ' * top_indent + 'end')
                block_stack.pop()
                changed = True
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


def _disable_broken_scripts(name: str, source: str, fixes: list[str]) -> str:
    """Disable scripts that still have block balance errors after all fixes.

    Scripts that are mostly commented-out Unity rendering code (PlanarReflection,
    Water shader systems, etc.) may have orphaned end keywords or unclosed blocks
    that can't be automatically fixed.  Rather than letting them error at runtime,
    wrap them in a `do return end` guard so they load silently but don't execute.
    """
    lines = source.split('\n')
    depth = 0
    for line in lines:
        stripped = line.strip()
        if stripped.startswith('--'):
            continue
        code_part = re.sub(r'\s*--.*$', '', stripped).rstrip()
        if re.search(r'\bfunction\s*[\w.:(]', stripped):
            fc = len(re.findall(r'\bfunction\s*[\w.:(]', stripped))
            ec = len(re.findall(r'\bend\b', stripped))
            depth += max(0, fc - ec)
        if re.match(r'if\b.+\bthen\s*$', code_part):
            depth += 1
        if re.match(r'for\b.+\bdo\s*$', code_part):
            depth += 1
        if re.match(r'while\b.+\bdo\s*$', code_part):
            depth += 1
        if code_part == 'repeat':
            depth += 1
        if stripped == 'end' or stripped.startswith('end)'):
            depth -= 1
        if re.match(r'until\b', stripped):
            depth -= 1

    if depth != 0:
        # Only disable ModuleScript-style scripts (return X) that have significant
        # block imbalance.  Small imbalances in regular scripts can be tolerated
        # (Roblox may still run partial code before erroring).
        module_match = re.search(r'^return\s+(\w+)\s*$', source, re.MULTILINE)
        if module_match:
            module_name = module_match.group(1)
            # Check if the script is mostly commented-out code (rendering/camera stubs)
            code_lines = [l for l in lines if l.strip() and not l.strip().startswith('--')]
            comment_lines = [l for l in lines if l.strip().startswith('--')]
            mostly_comments = len(comment_lines) > len(code_lines)
            if mostly_comments or abs(depth) > 2:
                # Wrap: keep module table declaration and return, disable everything else
                source = (
                    f"-- [converter] Script disabled: block structure could not be auto-fixed\n"
                    f"-- Original script had Unity rendering/camera code that doesn't translate to Roblox.\n"
                    f"local {module_name} = {{}}\n"
                    f"return {module_name}\n"
                )
                fixes.append(f"Disabled script with unbalanced blocks (depth={depth})")
                log.warning("  [%s] Disabled script: unbalanced block depth %d after all fixes", name, depth)

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


def _fix_nil_typed_variables(name: str, source: str, fixes: list[str]) -> str:
    """Fix variables initialized to nil with type comments that are used in arithmetic.

    The AI transpiler often emits `local x = nil -- float` or `local x = nil -- int`
    for C# fields with no explicit initializer.  When these variables are later used
    in arithmetic (sub, add, mul, etc.) or comparisons before being assigned, Roblox
    throws 'attempt to perform arithmetic on nil'.

    This fix initialises numeric-typed nils to 0 and bool-typed nils to false.
    """
    # Pattern: `local varname = nil -- float/int/number/double/single/byte/short/long`
    # Also handle `-- AudioClip`, `-- GameObject`, `-- Transform` etc. - leave those as nil
    numeric_types = {
        'float', 'int', 'number', 'double', 'single', 'byte', 'short', 'long',
        'uint', 'ushort', 'ulong', 'sbyte', 'decimal',
    }
    bool_types = {'bool', 'boolean'}

    lines = source.split('\n')
    changed = False
    for i, line in enumerate(lines):
        m = re.match(r'^(\s*local\s+\w+\s*=\s*)nil(\s*--\s*(.+))$', line)
        if m:
            prefix, suffix, type_hint_raw = m.group(1), m.group(2), m.group(3).strip()
            type_hint = type_hint_raw.lower()
            first_word = type_hint.split()[0] if type_hint else ''
            # Check for array types: "Type[]" or "List<Type>" etc.
            if '[]' in type_hint_raw or type_hint.startswith('list<'):
                lines[i] = prefix + '{}' + suffix
                changed = True
            elif first_word in numeric_types:
                lines[i] = prefix + '0' + suffix
                changed = True
            elif first_word in bool_types:
                lines[i] = prefix + 'false' + suffix
                changed = True

    if changed:
        source = '\n'.join(lines)
        fixes.append("Initialized nil-typed numeric/bool variables to default values")

    return source


def _fix_module_script_parent_access(name: str, source: str, fixes: list[str]) -> str:
    """Guard ModuleScripts that use script.Parent for runtime behavior.

    ModuleScripts in ReplicatedStorage have script.Parent == ReplicatedStorage.
    Transpiled MonoBehaviour code that connects to events (Heartbeat, Touched)
    or accesses script.Parent properties at module scope crashes because
    ReplicatedStorage is not a BasePart/Model.

    This wraps the entire module body in an early-return guard that prevents
    execution when the script isn't parented to a game object.
    """
    # Only apply to ModuleScript-style code (has `return ModuleName` at end)
    module_match = re.search(r'^return\s+(\w+)\s*$', source, re.MULTILINE)
    if not module_match:
        return source

    module_name = module_match.group(1)

    # Check if module-scope code uses script.Parent for runtime behavior
    has_runtime_parent = False
    lines = source.split('\n')
    in_function = 0

    for line in lines:
        stripped = line.strip()
        if stripped.startswith('--'):
            continue
        code_part = re.sub(r'\s*--.*$', '', stripped).rstrip()
        # Count function openers and end closers on same line
        if re.search(r'\bfunction\s*[\w.:(]', code_part):
            fc = len(re.findall(r'\bfunction\s*[\w.:(]', code_part))
            ec = len(re.findall(r'\bend\b', code_part))
            in_function += fc - ec
        elif code_part == 'end' or code_part.startswith('end)'):
            in_function = max(0, in_function - 1)
        # Also count if/for/while/do blocks for depth tracking
        if re.match(r'if\b.+\bthen\s*$', code_part):
            in_function += 1
        if re.match(r'for\b.+\bdo\s*$', code_part):
            in_function += 1
        if re.match(r'while\b.+\bdo\s*$', code_part):
            in_function += 1

        # Module scope: check for script.Parent usage in runtime code
        if in_function == 0 and 'script.Parent' in stripped:
            # Runtime indicators: event connections, property access, method calls
            if any(p in stripped for p in [
                '.Position', '.CFrame', '.Size', '.Orientation',
                ':FindFirstChildWhichIsA', ':FindFirstChild(',
                '.Touched:', '.Heartbeat:', '.RenderStepped:',
                ':Connect(', ':Play(', ':Destroy()',
            ]):
                has_runtime_parent = True
                break

    if not has_runtime_parent:
        return source

    # Insert an early-return guard after the module table declaration
    # This prevents all module-scope runtime code from executing when
    # the script is in ReplicatedStorage.
    guard = (
        "\n-- Guard: skip runtime code if script is not parented to a game object\n"
        "if not (script.Parent:IsA(\"BasePart\") or script.Parent:IsA(\"Model\")"
        " or script.Parent:IsA(\"Folder\")) then\n"
        f"    return {module_name}\n"
        "end\n"
    )

    # Find the module table declaration line
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^local\s+' + re.escape(module_name) + r'\s*=\s*\{\}', stripped):
            lines.insert(i + 1, guard)
            source = '\n'.join(lines)
            fixes.append("Added ReplicatedStorage guard for module with runtime script.Parent access")
            break

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

    # Preserve original trailing whitespace — don't add or remove newlines
    if not original.endswith('\n') and source.endswith('\n'):
        source = source.rstrip('\n')

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
