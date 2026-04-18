"""
script_coherence.py -- Post-transpilation fixes for cross-script consistency.

After all scripts are independently transpiled, this module analyzes them as a
group and fixes issues that arise from scripts being transpiled in isolation:

1. require() classification: If script A does require(scriptB), scriptB must
   be a ModuleScript. Reclassify it.

2. Script that returns a value at the end should be a ModuleScript.

3. Scripts with only utility functions and no side-effects should be ModuleScripts.

4. Client-side API detection: Scripts using LocalPlayer, UserInputService, etc.
   must be LocalScripts (they crash in ServerScriptService).

5. BindableEvent/RemoteEvent method consistency.
"""

from __future__ import annotations

import logging
import re
from core.roblox_types import RbxScript

log = logging.getLogger(__name__)

# APIs that ONLY work on the client — scripts using these must be LocalScripts.
_CLIENT_ONLY_PATTERNS = [
    r"Players\.LocalPlayer",
    r"game:GetService\([\"']Players[\"']\)\.LocalPlayer",
    r'GetService\(["\']UserInputService["\']\)',
    r"UserInputService",
    r"workspace\.CurrentCamera",
    r"game\.Workspace\.CurrentCamera",
    r'GetService\(["\']StarterGui["\']\)',
    r"LocalPlayer\.Character",
    r"\.PlayerGui",
    r"mouse\.Hit",
    r"mouse\.Target",
    r'GetService\(["\']ContextActionService["\']\)',
    r'GetService\(["\']GuiService["\']\)',
]

# APIs that ONLY work on the server — prevent these from becoming LocalScripts.
_SERVER_ONLY_PATTERNS = [
    r"\.OnServerEvent",
    r":FireClient\(",
    r'GetService\(["\']DataStoreService["\']\)',
    r'GetService\(["\']MessagingService["\']\)',
    r'GetService\(["\']ServerStorage["\']\)',
    r'GetService\(["\']ServerScriptService["\']\)',
    r"game\.ServerStorage",
    r"game\.ServerScriptService",
]


def inject_require_calls(
    scripts: list[RbxScript],
    dependency_map: dict[str, list[str]],
) -> int:
    """Inject require() calls for cross-script dependencies.

    For each script that references other project classes, adds require()
    calls at the top and ensures the referenced scripts are ModuleScripts
    with a proper return statement.

    Args:
        scripts: All transpiled scripts.
        dependency_map: class_name -> [referenced_class_names] from analyzer.

    Returns:
        Number of require() calls injected.
    """
    injected = 0
    script_by_class: dict[str, RbxScript] = {}
    duplicates: set[str] = set()
    for s in scripts:
        # Use the script name (which comes from the C# class name)
        if s.name in script_by_class:
            duplicates.add(s.name)
            log.warning("Duplicate script class name '%s' — require() may resolve to wrong module", s.name)
        script_by_class[s.name] = s
    if duplicates:
        log.warning("  %d duplicate class names: %s", len(duplicates), ", ".join(sorted(duplicates)[:10]))

    for s in scripts:
        deps = dependency_map.get(s.name, [])
        if not deps:
            continue

        # Find which dependencies are actual project scripts
        requires_to_add: list[str] = []
        for dep in deps:
            if dep in script_by_class and dep != s.name:
                # Check if already required
                if f'"{dep}"' not in s.source or 'require(' not in s.source:
                    requires_to_add.append(dep)

        if not requires_to_add:
            continue

        # Build require() block
        require_lines = []
        for dep in sorted(set(requires_to_add)):
            target = script_by_class[dep]
            # Ensure the target is a ModuleScript
            if target.script_type != "ModuleScript":
                old_type = target.script_type
                target.script_type = "ModuleScript"
                log.info("  Reclassified '%s' from %s to ModuleScript (required by '%s')",
                         dep, old_type, s.name)
                # Add return statement if missing
                stripped_source = target.source.rstrip()
                # Check if already ends with any return statement
                last_lines = stripped_source.split('\n')
                has_return = any(
                    line.strip().startswith('return ')
                    for line in last_lines[-3:]  # check last 3 lines
                )
                if not has_return:
                    target.source = stripped_source + f"\n\nreturn {dep}\n"
                # Add module table definition if missing
                if f"local {dep} = " not in target.source:
                    # Insert after service declarations at the top
                    tgt_lines = target.source.split("\n")
                    tgt_insert = 0
                    for ti, tl in enumerate(tgt_lines):
                        if tl.strip().startswith("local ") and "GetService" in tl:
                            tgt_insert = ti + 1
                        elif tl.strip().startswith("local ") and "require" in tl:
                            tgt_insert = ti + 1
                        elif tl.strip() == "" and tgt_insert > 0:
                            tgt_insert = ti + 1
                            break
                        elif tgt_insert == 0 and tl.strip() and not tl.strip().startswith("--"):
                            break
                    tgt_lines.insert(tgt_insert, f"\nlocal {dep} = {{}}\n")
                    target.source = "\n".join(tgt_lines)

            require_lines.append(
                f'local {dep} = require(game:GetService("ReplicatedStorage")'
                f':FindFirstChild("{dep}", true))'
            )

        if require_lines:
            # Insert after any existing service declarations at the top
            lines = s.source.split("\n")
            insert_idx = 0
            for i, line in enumerate(lines):
                if line.strip().startswith("local ") and "GetService" in line:
                    insert_idx = i + 1
                elif line.strip() == "" and insert_idx > 0:
                    insert_idx = i + 1
                    break
                elif insert_idx == 0 and line.strip() and not line.strip().startswith("--"):
                    break

            # Add requires
            require_block = "\n".join(require_lines) + "\n"
            lines.insert(insert_idx, require_block)
            s.source = "\n".join(lines)
            injected += len(require_lines)
            log.info("  Injected %d require() calls into '%s'", len(require_lines), s.name)

    return injected


def fix_require_classifications(scripts: list[RbxScript]) -> int:
    """Reclassify scripts that are require()'d by other scripts as ModuleScripts.

    Also reclassifies scripts that return a table/function at the end
    (ModuleScript pattern) but were classified as Script.

    Returns the number of scripts reclassified.
    """
    fixes = 0
    script_by_name: dict[str, RbxScript] = {}
    for s in scripts:
        script_by_name[s.name] = s

    # Pass 1: Find all require() targets across all scripts.
    required_names: set[str] = set()
    for s in scripts:
        # Match patterns like:
        #   require(ReplicatedStorage:FindFirstChild("Foo", true))
        #   require(script.Parent:FindFirstChild("Foo"))
        #   require(game.ServerScriptService.Foo)
        for m in re.finditer(r'require\([^)]*["\'](\w+)["\']', s.source):
            required_names.add(m.group(1))
        for m in re.finditer(r'require\([^)]*\.(\w+)\s*\)', s.source):
            required_names.add(m.group(1))

    # Reclassify required scripts as ModuleScript.
    for name in required_names:
        if name in script_by_name:
            target = script_by_name[name]
            if target.script_type != "ModuleScript":
                old_type = target.script_type
                target.script_type = "ModuleScript"
                fixes += 1
                log.info("  Reclassified '%s' from %s to ModuleScript (required by another script)",
                         name, old_type)
                # Add return nil if the script has no return statement
                stripped_source = target.source.rstrip()
                last_lines = stripped_source.split('\n')
                has_return = any(
                    line.strip().startswith('return ')
                    for line in last_lines[-3:]
                )
                if not has_return:
                    target.source = stripped_source + '\n\nreturn nil\n'
                    log.info("  Added 'return nil' to '%s' (required as module but has no return)", name)

    # Pass 2: Scripts that end with `return ...` are likely ModuleScripts.
    for s in scripts:
        if s.script_type == "ModuleScript":
            continue
        # Check if the script ends with a return statement (ignoring trailing whitespace/comments)
        lines = s.source.rstrip().split("\n")
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            if stripped.startswith("return "):
                # This script returns something — it's a ModuleScript pattern
                s.script_type = "ModuleScript"
                fixes += 1
                log.info("  Reclassified '%s' to ModuleScript (ends with return statement)", s.name)
            break  # Only check the last non-empty, non-comment line

    # Pass 3: Client-side API detection.
    # Scripts using client-only APIs (LocalPlayer, UserInputService) must be
    # LocalScripts. Scripts using server-only APIs must NOT be LocalScripts.
    fixes += _fix_client_server_classification(scripts)

    # Pass 4: Propagate client-only classification through require chains.
    # If script A requires module B, and B uses client-only APIs, then A must
    # also be a LocalScript (otherwise B's LocalPlayer calls will be nil).
    fixes += _propagate_client_classification(scripts, script_by_name)

    # Pass 5: Guard script.Parent access in reclassified ModuleScripts.
    # When a script is reclassified to ModuleScript and moved to ReplicatedStorage,
    # script.Parent becomes ReplicatedStorage (not the original game object).
    # Add early-return guards for scripts that access script.Parent properties.
    fixes += _guard_script_parent_access(scripts)

    # Pass 6: Circular require detection — lazy proxy for one direction.
    fixes += _break_circular_requires(scripts)

    # Pass 7: Fix string concatenation with + (C# remnant).
    fixes += _fix_string_concat(scripts)

    # Pass 8: Stub out unavailable platform SDKs (FlurryAnalytics, Firebase, etc.)
    fixes += _stub_unavailable_sdks(scripts)

    # Pass 9: Guard client-only code in ModuleScripts loaded by server scripts.
    # When a ModuleScript uses Players.LocalPlayer at module scope, wrap it in
    # RunService:IsClient() so the server-side require() doesn't crash.
    fixes += _guard_client_code_in_modules(scripts)

    # Pass 10: Add workspace lookup for ModuleScripts that do FindFirstChild
    # on script.Parent at module scope (e.g. HostilePlane looking for "Origin").
    fixes += _add_workspace_fallback(scripts)

    return fixes


def _propagate_client_classification(
    scripts: list[RbxScript],
    script_by_name: dict[str, RbxScript],
) -> int:
    """Propagate client-only requirement through require() chains.

    If script A requires module B, and B uses client-only APIs (LocalPlayer, etc.),
    then A must be a LocalScript — otherwise B's client APIs will fail at runtime.
    """
    # Build set of modules that use client-only APIs
    client_modules: set[str] = set()
    for s in scripts:
        if s.script_type == "ModuleScript":
            has_client = any(re.search(pat, s.source) for pat in _CLIENT_ONLY_PATTERNS)
            if has_client:
                client_modules.add(s.name)

    if not client_modules:
        return 0

    fixes = 0
    for s in scripts:
        if s.script_type != "Script":
            continue  # Only reclassify Server scripts
        has_server = any(re.search(pat, s.source) for pat in _SERVER_ONLY_PATTERNS)
        if has_server:
            continue  # Don't touch scripts that use server-only APIs

        # Check if this script requires any client-only module
        # Extract all quoted strings from require() lines to find module names
        required = set()
        for line in s.source.split('\n'):
            if 'require(' not in line:
                continue
            # Find all quoted strings in the require() call
            for m in re.finditer(r'["\'](\w+)["\']', line):
                required.add(m.group(1))
            # Also match require(path.ModuleName) patterns
            m = re.search(r'require\([^)]*\.(\w+)\s*\)', line)
            if m:
                required.add(m.group(1))

        if required & client_modules:
            s.script_type = "LocalScript"
            fixes += 1
            shared = required & client_modules
            log.info("  Reclassified '%s' from Script to LocalScript "
                     "(requires client-only module: %s)", s.name, ", ".join(shared))

    return fixes


def _fix_client_server_classification(scripts: list[RbxScript]) -> int:
    """Reclassify scripts based on client/server API usage.

    A Script that uses LocalPlayer or UserInputService will crash in
    ServerScriptService. These must be LocalScripts.
    """
    fixes = 0
    for s in scripts:
        if s.script_type == "ModuleScript":
            continue  # ModuleScripts work in any context

        has_client = any(re.search(pat, s.source) for pat in _CLIENT_ONLY_PATTERNS)
        has_server = any(re.search(pat, s.source) for pat in _SERVER_ONLY_PATTERNS)

        if has_client and not has_server and s.script_type != "LocalScript":
            old_type = s.script_type
            s.script_type = "LocalScript"
            fixes += 1
            log.info("  Reclassified '%s' from %s to LocalScript (uses client-only APIs)",
                     s.name, old_type)
        elif has_server and not has_client and s.script_type == "LocalScript":
            s.script_type = "Script"
            fixes += 1
            log.info("  Reclassified '%s' from LocalScript to Script (uses server-only APIs)",
                     s.name)

    return fixes


def _guard_script_parent_access(scripts: list[RbxScript]) -> int:
    """Add early-return guard to ModuleScripts that use script.Parent for runtime access.

    When a script is reclassified to ModuleScript in ReplicatedStorage,
    script.Parent is ReplicatedStorage (not a game object). Code that accesses
    .CFrame, .Position, .Size, .Touched etc. on script.Parent will crash.

    This injects a guard after the module table declaration that returns early
    if script.Parent is not a BasePart/Model/Folder.
    """
    fixes = 0
    _RUNTIME_PROPS = ['.Position', '.CFrame', '.Size', '.Orientation',
                      '.Touched:', '.Anchored']

    for s in scripts:
        if s.script_type != "ModuleScript":
            continue
        if '-- Guard: skip runtime' in s.source:
            continue  # Already guarded

        # Check for script.Parent or alias usage with runtime properties
        has_runtime = False
        aliases = set()
        for line in s.source.split('\n'):
            stripped = line.strip()
            if stripped.startswith('--'):
                continue
            # Track aliases: `local part = script.Parent`
            m = re.match(r'local\s+(\w+)\s*=\s*script\.Parent\b', stripped)
            if m:
                aliases.add(m.group(1))
            # Check for runtime property access at module scope
            check_names = ['script.Parent'] + list(aliases)
            for name in check_names:
                if name in stripped and any(p in stripped for p in _RUNTIME_PROPS):
                    has_runtime = True
                    break
            if has_runtime:
                break

        if not has_runtime:
            continue

        # Find the module table declaration and insert guard after it
        module_m = re.search(r'^return\s+(\w+)\s*$', s.source, re.MULTILINE)
        if not module_m:
            continue
        module_name = module_m.group(1)

        table_m = re.search(
            r'^(local\s+' + re.escape(module_name) + r'\s*=\s*\{\})',
            s.source, re.MULTILINE,
        )
        if table_m:
            guard = (
                f'\n-- Guard: skip runtime code if script is in ReplicatedStorage\n'
                f'if not (script.Parent:IsA("BasePart") or script.Parent:IsA("Model")'
                f' or script.Parent:IsA("Folder")) then\n'
                f'    return {module_name}\n'
                f'end\n'
            )
            s.source = s.source[:table_m.end()] + guard + s.source[table_m.end():]
            fixes += 1
            log.info("  Added script.Parent guard to '%s'", s.name)

    return fixes


def _break_circular_requires(scripts: list[RbxScript]) -> int:
    """Detect and break circular require chains by converting to lazy-loading.

    If module A requires module B, and B requires A (directly or via a chain),
    convert one direction to a lazy-loading pattern.

    IMPORTANT: Only the ``local X = require(...)`` line is replaced with a
    lazy-loading proxy.  All other references to ``X`` in the script are left
    untouched — the proxy forwards ``__index`` / ``__newindex`` / ``__call``
    to the real module once it is loaded, so existing code works as-is.
    """
    fixes = 0
    # Build a require graph
    require_graph: dict[str, set[str]] = {}
    script_by_name = {s.name: s for s in scripts}

    for s in scripts:
        deps = set()
        # Match FindFirstChild("Name", ...) inside require() calls
        for m in re.finditer(r'require\(.*?FindFirstChild\(\s*["\'](\w+)["\']', s.source):
            deps.add(m.group(1))
        # Also match require(path.ModuleName) patterns
        for m in re.finditer(r'require\([^)]*\.(\w+)\s*\)', s.source):
            deps.add(m.group(1))
        require_graph[s.name] = deps

    # Track already-broken edges so we don't break both directions
    broken: set[tuple[str, str]] = set()

    # Find cycles
    for name, deps in require_graph.items():
        for dep in deps:
            if dep in require_graph and name in require_graph[dep]:
                # Circular: name requires dep, dep requires name
                if (name, dep) in broken or (dep, name) in broken:
                    continue
                # Break the cycle in the script that requires the other
                target = script_by_name.get(name)
                if not target:
                    continue
                # Match the full require line (may have nested parens)
                pattern = rf'^(local\s+{re.escape(dep)}\s*=\s*require\(.+\))$'
                match = re.search(pattern, target.source, re.MULTILINE)
                if match:
                    old_require = match.group(1)
                    # Replace with a lazy proxy that defers require() until
                    # first access.  The variable name stays the same so all
                    # other references (function calls, table reads) work
                    # without modification.
                    lazy = (
                        f'local {dep} = setmetatable({{}}, {{\n'
                        f'    __index = function(_, k)\n'
                        f'        local mod = require(game:GetService("ReplicatedStorage")'
                        f':FindFirstChild("{dep}", true))\n'
                        f'        {dep} = mod  -- replace proxy with real module\n'
                        f'        return mod[k]\n'
                        f'    end,\n'
                        f'    __call = function(_, ...)\n'
                        f'        local mod = require(game:GetService("ReplicatedStorage")'
                        f':FindFirstChild("{dep}", true))\n'
                        f'        {dep} = mod\n'
                        f'        return mod(...)\n'
                        f'    end,\n'
                        f'}})'
                    )
                    target.source = target.source.replace(old_require, lazy)
                    # Do NOT replace other references — the proxy handles them
                    broken.add((name, dep))
                    fixes += 1
                    log.info("  Broke circular require: '%s' → '%s' (lazy proxy)", name, dep)

    return fixes


def _fix_string_concat(scripts: list[RbxScript]) -> int:
    """Fix C# string concatenation with + that survived AI transpilation."""
    fixes = 0
    for s in scripts:
        if '"+' not in s.source and '" +' not in s.source:
            continue
        original = s.source
        # Fix "str" + "str" → "str" .. "str"
        s.source = re.sub(r'"\s*\+\s*"', '" .. "', s.source)
        # Fix "str" + var → "str" .. var
        s.source = re.sub(r'"\s*\+\s*(\w)', r'" .. \1', s.source)
        # Fix var + "str" → var .. "str"
        s.source = re.sub(r'(\w)\s*\+\s*"', r'\1 .. "', s.source)
        # Fix line-ending "str" + → "str" ..
        s.source = re.sub(r'"\s*\+\s*$', '" ..', s.source, flags=re.MULTILINE)
        if s.source != original:
            fixes += 1
            log.info("  Fixed string '+' concatenation in '%s'", s.name)
    return fixes


def _stub_unavailable_sdks(scripts: list[RbxScript]) -> int:
    """Stub out platform SDK calls that have no Roblox equivalent.

    For modules that use .Instance (Unity singleton pattern), add a
    nil-safe stub near the module declaration so .Instance calls don't crash.
    Instead of wrapping each call site (which breaks multi-line expressions),
    we ensure the module variable is never nil by providing a stub.
    """
    fixes = 0

    for s in scripts:
        if '.Instance:' not in s.source and '.Instance.' not in s.source:
            continue

        # Find module names that use .Instance
        instance_modules = set()
        for m in re.finditer(r'(\w+)\.Instance[:\.]', s.source):
            instance_modules.add(m.group(1))

        if not instance_modules:
            continue

        # For each module, add a nil-safe stub after its require/declaration
        for mod_name in instance_modules:
            # Check if it's already guarded
            if f'{mod_name} = {mod_name} or' in s.source:
                continue
            # Find a require() declaration for this module (single-line only).
            # Avoid matching table literal openers like `local X = {` which
            # span multiple lines — inserting a stub there breaks the table.
            decl_pattern = rf'^(local\s+{re.escape(mod_name)}\s*=\s*require\(.+\))$'
            match = re.search(decl_pattern, s.source, re.MULTILINE)
            if match:
                stub = (f'\n{mod_name} = {mod_name} or '
                        f'{{Instance = {{}}}}  -- Stub: SDK not available on Roblox')
                # Add stub after the declaration line
                # Use setmetatable so any method call returns a no-op
                stub = (f'\nif not {mod_name} then {mod_name} = '
                        f'setmetatable({{Instance = setmetatable({{}}, '
                        f'{{__index = function() return function() end end}})}}, '
                        f'{{__index = function() return function() end end}}) end')
                s.source = s.source[:match.end()] + stub + s.source[match.end():]
                fixes += 1
                log.info("  Stubbed SDK module '%s' in '%s'", mod_name, s.name)

    return fixes


def _guard_client_code_in_modules(scripts: list[RbxScript]) -> int:
    """Wrap client-only initialization in RunService:IsClient() for ModuleScripts.

    When a ModuleScript uses Players.LocalPlayer at module scope (outside
    functions), the server-side require() will crash because LocalPlayer is nil.
    Wrap the client-only block (from LocalPlayer usage to end of file) in an
    IsClient() guard.
    """
    fixes = 0
    for s in scripts:
        if s.script_type != "ModuleScript":
            continue
        if 'RunService:IsClient()' in s.source:
            continue  # Already guarded

        # Check for module-scope LocalPlayer usage
        has_local_player_module_scope = False
        lines = s.source.split('\n')
        in_function = 0
        lp_line_idx = -1

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith('--'):
                continue
            # Only track function depth — if/for/while at module scope
            # are still module scope, not inside a function.
            if re.search(r'\bfunction\s*[\w.:(]', stripped):
                in_function += 1
            if (stripped == 'end' or stripped.startswith('end)') or stripped == 'end,') and in_function > 0:
                in_function -= 1

            if in_function == 0 and not stripped.startswith('local ') and (
                'localPlayer.' in stripped or 'LocalPlayer.' in stripped
            ):
                has_local_player_module_scope = True
                lp_line_idx = i
                break

        if not has_local_player_module_scope or lp_line_idx < 0:
            continue

        # Find the return statement at the end
        return_m = re.search(r'^return\s+\w+\s*$', s.source, re.MULTILINE)
        if not return_m:
            continue

        # Wrap the client-only block in IsClient() guard
        # Insert `if RunService:IsClient() then` before the LocalPlayer usage
        # and `end` before the return
        indent = '  '  # Minimal indent for the guard
        lines.insert(lp_line_idx, f'\nif game:GetService("RunService"):IsClient() then')
        # Find return line (shifted by 1 due to insertion)
        for j in range(len(lines) - 1, -1, -1):
            if lines[j].strip().startswith('return '):
                lines.insert(j, 'end\n')
                break
        s.source = '\n'.join(lines)
        fixes += 1
        log.info("  Added IsClient() guard for client-only code in '%s'", s.name)

    return fixes


def _add_workspace_fallback(scripts: list[RbxScript]) -> int:
    """Add workspace lookup for ModuleScripts that use script.Parent:FindFirstChild at module scope.

    When script.Parent is ReplicatedStorage, FindFirstChild won't find game objects.
    Add a fallback that searches workspace for the script's named parent.
    """
    fixes = 0
    for s in scripts:
        if s.script_type != "ModuleScript":
            continue
        if '-- workspace fallback' in s.source:
            continue  # Already has fallback

        # Check for pattern: `local model = script.Parent` followed by
        # `model:FindFirstChild("X")` at module scope
        model_alias = None
        alias_m = re.search(r'^local\s+(\w+)\s*=\s*script\.Parent\b', s.source, re.MULTILINE)
        if alias_m:
            model_alias = alias_m.group(1)

        if not model_alias:
            continue

        # Check if the alias is used with FindFirstChild at module scope
        find_m = re.search(
            rf'^local\s+\w+\s*=\s*{re.escape(model_alias)}:FindFirstChild\(',
            s.source, re.MULTILINE,
        )
        if not find_m:
            continue

        # Add workspace fallback after the alias assignment
        fallback = (
            f'\n-- workspace fallback: script.Parent is ReplicatedStorage for ModuleScripts\n'
            f'if not {model_alias}:IsA("BasePart") and not {model_alias}:IsA("Model") then\n'
            f'    {model_alias} = workspace:FindFirstChild("{s.name}", true) or {model_alias}\n'
            f'end'
        )
        s.source = s.source.replace(
            alias_m.group(0),
            alias_m.group(0) + fallback,
        )
        fixes += 1
        log.info("  Added workspace fallback for '%s' in '%s'", model_alias, s.name)

    return fixes
