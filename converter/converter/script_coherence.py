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
    for s in scripts:
        # Use the script name (which comes from the C# class name)
        script_by_class[s.name] = s

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
                if not stripped_source.endswith(f"return {dep}"):
                    # Wrap the script in a module table pattern
                    target.source = stripped_source + f"\n\nreturn {dep}\n"

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
