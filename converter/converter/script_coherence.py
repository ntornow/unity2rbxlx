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


def _module_require_body(name: str) -> str:
    """Return the body of a ``require(...)`` lookup that survives the storage
    classifier's split between ReplicatedStorage and ServerStorage.

    The classifier (``storage_classifier.classify_storage``) parents a
    ModuleScript in ServerStorage when only server-side callers require it,
    so a hardcoded ``ReplicatedStorage:FindFirstChild`` lookup returns nil
    and ``require(nil)`` raises at runtime. The fallback chain covers both
    containers; a client script that erroneously asks for a server-only
    module still gets nil (visible failure) rather than reaching a
    forbidden container.
    """
    return (
        f'game:GetService("ReplicatedStorage"):FindFirstChild("{name}", true)'
        f' or game:GetService("ServerStorage"):FindFirstChild("{name}", true)'
    )


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
            # Ensure the target is a ModuleScript — BUT don't reclassify
            # scripts that are heavily client-only (LocalPlayer, camera,
            # UserInputService, RenderStepped). These break when loaded
            # as modules on the server. Instead, skip the require.
            if target.script_type != "ModuleScript":
                # Count client-only API usage
                client_api_count = sum(
                    1 for pat in _CLIENT_ONLY_PATTERNS
                    if re.search(pat, target.source)
                )
                if client_api_count >= 3:
                    log.info("  Skipping require of '%s' (heavy client-only script, %d client APIs)",
                             dep, client_api_count)
                    requires_to_add = [r for r in requires_to_add if r != dep]
                    continue
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
                f'local {dep} = require({_module_require_body(dep)})'
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

    # Reclassify required scripts as ModuleScript — but skip heavy client-only
    # scripts (3+ client APIs) that would break as modules on the server.
    for name in required_names:
        if name in script_by_name:
            target = script_by_name[name]
            if target.script_type != "ModuleScript":
                client_api_count = sum(
                    1 for pat in _CLIENT_ONLY_PATTERNS
                    if re.search(pat, target.source)
                )
                if client_api_count >= 3:
                    log.info("  Skipping reclassification of '%s' (heavy client-only, %d APIs)",
                             name, client_api_count)
                    continue
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

    # Pass 2: Scripts that end with `return ...` are likely ModuleScripts —
    # except FPS-style controllers that read client-only globals at the top
    # level (LocalPlayer / UserInputService / mouse). Those have to remain
    # LocalScripts: their RenderStepped/InputBegan listeners fire from top-
    # level and must run on character spawn, which a ModuleScript only would
    # if something explicitly required it.
    for s in scripts:
        if s.script_type == "ModuleScript":
            continue
        lines = s.source.rstrip().split("\n")
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            if stripped.startswith("return "):
                has_client = any(
                    re.search(pat, s.source) for pat in _CLIENT_ONLY_PATTERNS
                )
                if has_client:
                    log.info(
                        "  Skipping ModuleScript reclassification of '%s' "
                        "(ends with return but uses client-only APIs)", s.name,
                    )
                else:
                    s.script_type = "ModuleScript"
                    fixes += 1
                    log.info(
                        "  Reclassified '%s' to ModuleScript (ends with return statement)",
                        s.name,
                    )
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

    # Pass 10: Fix prefab lookups that search ReplicatedStorage instead of workspace.
    fixes += _fix_prefab_lookups(scripts)

    # Pass 13: Fix cloned prefabs that have Transparency=1 and Anchored=true.
    # Unity prefab templates are hidden in the scene but clones should be visible.
    fixes += _fix_clone_visibility(scripts)

    # Pass 11: Add workspace lookup for ModuleScripts that do FindFirstChild
    # on script.Parent at module scope (e.g. HostilePlane looking for "Origin").
    fixes += _add_workspace_fallback(scripts)

    # Pass 12: Wire SetAttribute/GetAttributeChangedSignal for inter-script communication.
    fixes += _wire_attribute_listeners(scripts)

    # Pass 14: Inject working FPS rifle pickup system.
    fixes += _inject_fps_rifle_system(scripts)

    # Pass 15: Convert Pickup SetAttribute to RemoteEvent FireClient.
    # Server→client attribute changes don't trigger GetAttributeChangedSignal.
    fixes += _convert_pickup_to_remote_event(scripts)

    # Pass 16: Add RemoteEvent OnClientEvent listener for pickups in Player scripts.
    fixes += _add_pickup_remote_listener(scripts)

    # Pass 17: Remove require(Player) from scripts that reference it —
    # Player is now a LocalScript, not in ReplicatedStorage.
    fixes += _remove_stale_player_requires(scripts)

    # Pass 18: Disable Roblox default PlayerModule controls in FPS-style
    # client scripts that set MouseBehavior=LockCenter. Without this,
    # the default PlayerModule (auto-loaded into StarterPlayerScripts)
    # resets MouseBehavior every frame, preventing mouse-look from working.
    fixes += _disable_default_controls_in_fps_scripts(scripts)

    # Pass 19: Expose ``<Table>.<Event> = <Bindable>.Event`` cross-script.
    # Producer parents its BindableEvents to ``script`` with matching Name;
    # consumers rewrite ``<var>.<Key>:Connect`` to
    # ``<var>:WaitForChild("<Key>").Event:Connect``. Without this, the
    # consumer indexes the producer LocalScript Instance for a non-existent
    # property and crashes ("X is not a valid member of LocalScript").
    fixes += _expose_local_script_events(scripts)

    # Pass 20: Approximate Unity OnTriggerStay with a Heartbeat poll on
    # scripts that translated it to a single-shot ``Touched:Connect``.
    # Without this, rotating turrets can never engage targets whose
    # initial-contact angle was outside the engagement cone.
    fixes += _add_trigger_stay_polling(scripts)

    # Pass 21: Make Pickup-style rotate+bob scripts work when the visible
    # entity is a child Model rather than a direct Part. Unity's
    # ``transform.Rotate`` on the script's GameObject just-works because
    # all children inherit the transform; in Roblox, the AI binds to the
    # first BasePart child via FindFirstChildWhichIsA, which often picks
    # the invisible trigger collider (Transparency=1) and animates that
    # instead of the visible Model. Patch: rewrite the script to find the
    # visual child Model (or non-trigger Part) and use ``PivotTo``.
    fixes += _fix_pickup_visual_target(scripts)

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
                        f'        local mod = require({_module_require_body(dep)})\n'
                        f'        {dep} = mod  -- replace proxy with real module\n'
                        f'        return mod[k]\n'
                        f'    end,\n'
                        f'    __call = function(_, ...)\n'
                        f'        local mod = require({_module_require_body(dep)})\n'
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
    """Make LocalPlayer access nil-safe in ModuleScripts loaded by server scripts.

    When a ModuleScript uses Players.LocalPlayer at module scope, the server-side
    require() will crash because LocalPlayer is nil. Instead of wrapping a block
    (which hides function definitions from the server), we:
    1. Make LocalPlayer access nil-safe: `player.Character` → `player and player.Character`
    2. Guard event connections that use LocalPlayer with IsClient() checks
    This keeps function definitions (GetRifle, etc.) available to server scripts.
    """
    fixes = 0
    for s in scripts:
        if s.script_type != "ModuleScript":
            continue
        if '-- _CLIENT_GUARD_APPLIED' in s.source:
            continue

        # Find LocalPlayer alias
        alias_m = re.search(r'local\s+(\w+)\s*=\s*\w+\.LocalPlayer\b', s.source)
        if not alias_m:
            continue
        lp_var = alias_m.group(1)

        original = s.source

        # 1. Make module-scope LocalPlayer access and derived variable chain nil-safe.
        # The AI often produces a chain like:
        #   local player = Players.LocalPlayer
        #   local character = player.Character or player.CharacterAdded:Wait()
        #   local humanoid = character:FindFirstChildWhichIsA("Humanoid")
        #   local rootPart = character:WaitForChild("HumanoidRootPart")
        #   local head = character:FindFirstChild("Head") or rootPart
        # All of these crash on the server because player is nil.
        # We nil-safe the entire chain by tracking derived variables.
        _derived = {lp_var}  # Variables that are nil when LocalPlayer is nil
        lines = s.source.split('\n')
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith('local '):
                continue
            # Check if RHS references any nil-derived variable
            m = re.match(r'^(local\s+(\w+)\s*=\s*)(.*)', stripped)
            if not m:
                continue
            decl, varname, rhs = m.group(1), m.group(2), m.group(3)
            uses_derived = any(
                re.search(rf'\b{re.escape(d)}\b', rhs) for d in _derived
            )
            if uses_derived:
                _derived.add(varname)
                # Wrap RHS: `expr` → `(LP_VAR) and (expr) or nil`
                indent = len(line) - len(line.lstrip())
                lines[i] = f'{" " * indent}{decl}({lp_var}) and ({rhs}) or nil'
        s.source = '\n'.join(lines)

        # 2. Make standalone LocalPlayer method calls nil-safe
        # `player.CharacterAdded:Connect(...)` → `if player then player.CharacterAdded:Connect(...) end`
        lines = s.source.split('\n')
        new_lines = []
        in_function = 0
        for line in lines:
            stripped = line.strip()
            if re.search(r'\bfunction\s*[\w.:(]', stripped):
                in_function += 1
            if (stripped == 'end' or stripped.startswith('end)')) and in_function > 0:
                in_function -= 1

            # Guard module-scope standalone calls on LocalPlayer alias
            if (in_function == 0 and
                not stripped.startswith('--') and
                not stripped.startswith('local ') and
                not stripped.startswith('if ') and
                re.match(rf'{re.escape(lp_var)}\.', stripped)):
                indent = len(line) - len(line.lstrip())
                new_lines.append(f'{" " * indent}if {lp_var} then')
                new_lines.append(line)
                new_lines.append(f'{" " * indent}end')
            else:
                new_lines.append(line)
        s.source = '\n'.join(new_lines)

        # 3. Guard client-only API calls at module scope
        # Camera, UserInputService, RenderStepped connections
        s.source = re.sub(
            r'^(camera\.CameraType\s*=)',
            r'if game:GetService("RunService"):IsClient() then \1',
            s.source, count=1, flags=re.MULTILINE,
        )
        # Find the last client-only connection and close the guard
        # Look for RenderStepped or InputBegan connections
        if 'camera.CameraType =' in original:
            # Find the last event connection block and add `end` after it
            last_connect = -1
            connect_lines = s.source.split('\n')
            for i, line in enumerate(connect_lines):
                if ':Connect(function' in line and in_function == 0:
                    # Find matching end)
                    for j in range(i + 1, len(connect_lines)):
                        if connect_lines[j].strip() in ('end)', 'end)'):
                            last_connect = j
                            break
            if last_connect > 0:
                connect_lines.insert(last_connect + 1, 'end -- IsClient guard')
                s.source = '\n'.join(connect_lines)

        if s.source != original:
            # Mark as processed to avoid re-processing
            s.source = s.source.replace(
                alias_m.group(0),
                alias_m.group(0) + '  -- _CLIENT_GUARD_APPLIED',
            )
            fixes += 1
            log.info("  Made LocalPlayer access nil-safe in '%s'", s.name)

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


def _fix_prefab_lookups(scripts: list[RbxScript]) -> int:
    """Fix prefab/asset lookups that incorrectly search ReplicatedStorage.

    The AI transpiler often generates `ReplicatedStorage:FindFirstChild("PrefabName")`
    for game objects that are actually children of Parts in workspace. This redirects
    those lookups to search workspace instead, with case-insensitive matching.
    """
    fixes = 0
    # Collect all script names to avoid redirecting module requires
    module_names = {s.name for s in scripts if s.script_type == "ModuleScript"}
    # Names that legitimately live in ReplicatedStorage and must NOT be redirected.
    # ReplicatedStorage.<PREFAB_PACKAGES_FOLDER> holds emitted prefab templates that
    # scripts :Clone() at runtime — single source of truth in prefab_packages so
    # renaming the folder there flows through here automatically.
    from converter.prefab_packages import PREFAB_PACKAGES_FOLDER
    rs_resident_names = {PREFAB_PACKAGES_FOLDER}

    for s in scripts:
        original = s.source
        # Find patterns like:
        #   local X = ReplicatedStorage:FindFirstChild("Name")
        #   local X = game:GetService("ReplicatedStorage"):FindFirstChild("Name")
        # where "Name" is NOT a known ModuleScript (those belong in RS)
        def _fix_rs_lookup(m):
            indent = m.group(1)
            varname = m.group(2)
            obj_name = m.group(3)
            # Don't redirect if this is a known module or RS-resident container
            if obj_name in module_names or obj_name in rs_resident_names:
                return m.group(0)
            # Redirect to workspace search (case-insensitive, recursive)
            return (f'{indent}local {varname} = workspace:FindFirstChild("{obj_name}", true)'
                    f' or workspace:FindFirstChild("{obj_name.lower()}", true)'
                    f' or workspace:FindFirstChild("{obj_name[0].lower() + obj_name[1:]}", true)')

        s.source = re.sub(
            r'^(\s*)local\s+(\w+)\s*=\s*(?:ReplicatedStorage|game:GetService\(["\']ReplicatedStorage["\']\))'
            r':FindFirstChild\(["\'](\w+)["\']\)',
            _fix_rs_lookup,
            s.source,
            flags=re.MULTILINE,
        )
        if s.source != original:
            fixes += 1
            log.info("  Fixed prefab lookup in '%s' (RS → workspace)", s.name)

    return fixes


def _wire_attribute_listeners(scripts: list[RbxScript]) -> int:
    """Wire SetAttribute calls to matching local functions via GetAttributeChangedSignal.

    When one script does `target:SetAttribute("GetItem", itemName)` and another
    script has a local function `GetItem`, add a listener on the character that
    calls the function when the attribute changes. This bridges the inter-script
    communication gap from Unity's SendMessage pattern.
    """
    fixes = 0
    # Find all SetAttribute calls across all scripts
    attr_names: set[str] = set()
    for s in scripts:
        for m in re.finditer(r':SetAttribute\(["\'](\w+)["\']', s.source):
            attr_names.add(m.group(1))

    # Only wire GetItem — other attribute-based calls (TakeDamage, UpdateSpawnpoint)
    # are already handled by existing listeners in the AI-transpiled scripts.
    attr_names = {n for n in attr_names if n == 'GetItem'}

    # For scripts that have matching local functions, add listeners
    for s in scripts:
        added_any = False
        for attr_name in attr_names:
            # Check if this script has a local function or variable with that name
            has_func = bool(re.search(
                rf'^{attr_name}\s*=\s*function\b|^local\s+function\s+{attr_name}\b',
                s.source, re.MULTILINE,
            ))
            if not has_func:
                continue
            # Check if listener already exists
            if f'GetAttributeChangedSignal("{attr_name}")' in s.source:
                continue
            # Check if there's a character variable to listen on
            char_var = None
            for candidate in ['character', 'char', 'playerCharacter']:
                if re.search(rf'\blocal\s+{candidate}\b', s.source):
                    char_var = candidate
                    break
            if not char_var:
                continue

            # Add the listener before the return statement
            listener = (
                f'\n-- Auto-wired attribute listener for {attr_name}\n'
                f'if {char_var} and game:GetService("RunService"):IsClient() then\n'
                f'    task.defer(function()\n'
                f'        {char_var}:GetAttributeChangedSignal("{attr_name}"):Connect(function()\n'
                f'            local val = {char_var}:GetAttribute("{attr_name}")\n'
                f'            if val ~= nil then\n'
                f'                {char_var}:SetAttribute("{attr_name}", nil)\n'
                f'                {attr_name}(val)\n'
                f'            end\n'
                f'        end)\n'
                f'    end)\n'
                f'end\n'
            )
            # Insert before the last return
            return_m = re.search(r'^return\s+\w+\s*$', s.source, re.MULTILINE)
            if return_m:
                s.source = s.source[:return_m.start()] + listener + s.source[return_m.start():]
                added_any = True
                log.info("  Wired attribute listener '%s' in '%s'", attr_name, s.name)

        if added_any:
            fixes += 1

    return fixes


def _fix_clone_visibility(scripts: list[RbxScript]) -> int:
    """Fix cloned prefab parts that have Transparency=1 and Anchored=true.

    Unity prefab templates are often invisible in the scene (disabled GameObjects).
    The converter preserves Transparency=1 and Anchored=true. When the prefab is
    cloned at runtime (e.g. rifle pickup), the clone should be visible and unanchored.

    Injects a helper loop after Clone() calls that sets Transparency=0 and
    Anchored=false on all BasePart descendants.
    """
    fixes = 0
    _VISIBILITY_FIX = (
        '\n        -- Fix clone visibility (prefab template may be invisible/anchored)\n'
        '        for _, _p in rifle:GetDescendants() do\n'
        '            if _p:IsA("BasePart") then\n'
        '                _p.Transparency = 0\n'
        '                _p.Anchored = false\n'
        '            end\n'
        '        end'
    )

    for s in scripts:
        # Look for pattern: X:Clone() ... X.Parent = Y
        # where the clone is a prefab being placed in the scene
        if ':Clone()' not in s.source:
            continue

        # Find clone+parent patterns
        lines = s.source.split('\n')
        new_lines = []
        i = 0
        changed = False
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            # Detect: local X = Y:Clone()
            clone_m = re.match(r'(\s*)local\s+(\w+)\s*=\s*\w+:Clone\(\)', stripped)
            if clone_m:
                clone_var = clone_m.group(2)
                # Look ahead for `.Parent = ` within 20 lines
                has_parent = False
                for j in range(i + 1, min(i + 20, len(lines))):
                    if f'{clone_var}.Parent' in lines[j]:
                        has_parent = True
                        break
                if has_parent and '-- Fix clone visibility' not in s.source:
                    indent = len(line) - len(line.lstrip())
                    i4 = " " * (indent + 4)
                    i8 = " " * (indent + 8)
                    i12 = " " * (indent + 12)
                    # PrimaryPart only exists on Model. A bare-Part clone
                    # raises "PrimaryPart is not a valid member" at runtime
                    # if we read it without an IsA guard, even when the read
                    # is the LHS of an `or`. Branch by class first.
                    fix_code = (
                        f'\n{i4}-- Fix clone visibility and weld sub-mesh parts together\n'
                        f'{i4}local _primary\n'
                        f'{i4}if {clone_var}:IsA("BasePart") then\n'
                        f'{i8}_primary = {clone_var}\n'
                        f'{i8}{clone_var}.Transparency = 0\n'
                        f'{i8}{clone_var}.Anchored = false\n'
                        f'{i8}{clone_var}.CanCollide = false\n'
                        f'{i4}elseif {clone_var}:IsA("Model") then\n'
                        f'{i8}_primary = {clone_var}.PrimaryPart or {clone_var}:FindFirstChildWhichIsA("BasePart")\n'
                        f'{i4}end\n'
                        f'{i4}for _, _p in {clone_var}:GetDescendants() do\n'
                        f'{i8}if _p:IsA("BasePart") then\n'
                        f'{i12}_p.Transparency = 0\n'
                        f'{i12}_p.Anchored = false\n'
                        f'{i12}_p.CanCollide = false\n'
                        f'{i12}if _primary and _p ~= _primary then\n'
                        f'{i12}    local _w = Instance.new("WeldConstraint")\n'
                        f'{i12}    _w.Part0 = _p\n'
                        f'{i12}    _w.Part1 = _primary\n'
                        f'{i12}    _w.Parent = _p\n'
                        f'{i12}end\n'
                        f'{i8}end\n'
                        f'{i4}end'
                    )
                    new_lines.append(line)
                    new_lines.append(fix_code)
                    changed = True
                    i += 1
                    continue
            new_lines.append(line)
            i += 1

        if changed:
            s.source = '\n'.join(new_lines)
            fixes += 1
            log.info("  Added clone visibility fix in '%s'", s.name)

    return fixes


def _inject_fps_rifle_system(scripts: list[RbxScript]) -> int:
    """Inject complete FPS rifle pickup system into Player scripts.

    Replaces the AI-generated GetRifle with a working version that:
    1. Finds riflePrefab in workspace
    2. Clones, scales, makes visible, welds sub-parts
    3. Parents to workspace (not character) with anchored parts
    4. Updates position every frame in RenderStepped to follow camera
    5. Adds client-side Touched detection on character parts

    Also adds _fpsRifle variables and RenderStepped rifle update.
    """
    fixes = 0
    for s in scripts:
        if 'GetRifle' not in s.source:
            continue
        if '-- _FPS_RIFLE_SYSTEM' in s.source:
            continue

        original = s.source

        # 1. Add _fpsRifle variables
        s.source = s.source.replace(
            'local gotWeapon = false',
            'local gotWeapon = false\nlocal _fpsRifle = nil  -- _FPS_RIFLE_SYSTEM\nlocal _fpsRiflePrimary = nil',
        )

        # 2. Replace GetRifle function body
        m = re.search(
            r'(GetRifle = function\(\))(.*?)(\n\s*gotWeapon = true)',
            s.source, re.DOTALL,
        )
        if m:
            new_rifle = (
                'GetRifle = function()\n'
                '    if gotWeapon then return end\n'
                '    local rp = workspace:FindFirstChild("riflePrefab", true)\n'
                '        or workspace:FindFirstChild("RiflePrefab", true)\n'
                '    if not rp then return end\n'
                '    local rifle = rp:Clone()\n'
                '    if rifle:IsA("Model") then rifle:ScaleTo(0.15) end\n'
                '    local prim = rifle:FindFirstChildWhichIsA("BasePart")\n'
                '    if not prim then rifle:Destroy() return end\n'
                '    for _, p in rifle:GetDescendants() do\n'
                '        if p:IsA("BasePart") then\n'
                '            p.Transparency = 0\n'
                '            p.CanCollide = false\n'
                '            p.Anchored = true\n'
                '            if p ~= prim then\n'
                '                local w = Instance.new("WeldConstraint")\n'
                '                w.Part0 = p; w.Part1 = prim; w.Parent = p\n'
                '            end\n'
                '        end\n'
                '    end\n'
                '    rifle:PivotTo(workspace.CurrentCamera.CFrame * CFrame.new(0.5, -0.5, -3))\n'
                '    rifle.Parent = workspace\n'
                '    _fpsRifle = rifle\n'
                '    _fpsRiflePrimary = prim\n'
            )
            s.source = s.source[:m.start()] + new_rifle + s.source[m.start(3):]

        # 3. Add rifle update to RenderStepped
        if 'RunService.RenderStepped:Connect' in s.source:
            s.source = s.source.replace(
                'RunService.RenderStepped:Connect(function(dt)',
                'RunService.RenderStepped:Connect(function(dt)\n'
                '    if _fpsRifle and _fpsRiflePrimary and _fpsRiflePrimary.Parent then\n'
                '        _fpsRifle:PivotTo(workspace.CurrentCamera.CFrame * CFrame.new(0.5, -0.5, -3))\n'
                '    end',
            )

        # 4. Add client-side Touched pickup detection before the return
        touched_code = (
            '\n-- Client-side pickup detection\n'
            'if character then\n'
            '    for _, part in character:GetChildren() do\n'
            '        if part:IsA("BasePart") then\n'
            '            part.Touched:Connect(function(other)\n'
            '                local pm = other:FindFirstAncestorOfClass("Model")\n'
            '                if pm and (pm.Name:lower():find("pickup") or pm:FindFirstChild("Pickup")) then\n'
            '                    local sc = pm:FindFirstChild("Pickup") or pm:FindFirstChildWhichIsA("Script")\n'
            '                    local iname = (sc and sc:GetAttribute("itemName"))\n'
            '                        or pm:GetAttribute("itemName") or ""\n'
            '                    if iname == "" and pm.Name:lower():find("rifle") then iname = "Rifle" end\n'
            '                    if iname == "" and pm.Name:lower():find("key") then iname = "Key" end\n'
            '                    if iname == "" and pm.Name:lower():find("ammo") then iname = "Ammo" end\n'
            '                    if iname == "" and (pm.Name:lower():find("health") or pm.Name:lower():find("hp")) then iname = "Health" end\n'
            '                    if iname ~= "" then getItem(iname); pm:Destroy() end\n'
            '                end\n'
            '            end)\n'
            '        end\n'
            '    end\n'
            'end\n'
        )
        return_m = re.search(r'^return\b', s.source, re.MULTILINE)
        if return_m:
            s.source = s.source[:return_m.start()] + touched_code + s.source[return_m.start():]
        else:
            # LocalScript — no return, append at end
            s.source = s.source.rstrip() + '\n' + touched_code

        if s.source != original:
            fixes += 1
            log.info("  Injected FPS rifle system in '%s'", s.name)

    return fixes


def _convert_pickup_to_remote_event(scripts: list[RbxScript]) -> int:
    """Convert Pickup scripts from SetAttribute to RemoteEvent FireClient.

    Server-side SetAttribute doesn't trigger client-side GetAttributeChangedSignal.
    Use a RemoteEvent instead for server→client pickup communication.
    """
    fixes = 0
    for s in scripts:
        if s.name != 'Pickup':
            continue
        for attr_name in ['PickupItem', 'GetItem']:
            old = f'target:SetAttribute("{attr_name}", itemName)'
            log.info("  Checking Pickup for: %s → found=%s", attr_name, old in s.source)
            if old in s.source:
                new = (
                    'local _pe = game:GetService("ReplicatedStorage"):FindFirstChild("PickupItemEvent")\n'
                    '\t\tif _pe then\n'
                    '\t\t\tlocal _pl = game.Players:GetPlayerFromCharacter(target)\n'
                    '\t\t\tif _pl then _pe:FireClient(_pl, itemName) end\n'
                    '\t\tend'
                )
                s.source = s.source.replace(old, new)
                fixes += 1
                log.info("  Converted Pickup SetAttribute to RemoteEvent FireClient")
    return fixes


def _add_pickup_remote_listener(scripts: list[RbxScript]) -> int:
    """Add OnClientEvent listener for PickupItemEvent in Player scripts."""
    fixes = 0
    for s in scripts:
        if 'GetItem' not in s.source or 'GetRifle' not in s.source:
            continue
        if 'PickupItemEvent' in s.source:
            continue
        listener = (
            '\n-- Pickup via RemoteEvent (server fires when player touches pickup)\n'
            'local _pickupEvt = game:GetService("ReplicatedStorage"):WaitForChild("PickupItemEvent", 5)\n'
            'if _pickupEvt then\n'
            '    _pickupEvt.OnClientEvent:Connect(function(itemName)\n'
            '        if itemName and itemName ~= "" then getItem(itemName) end\n'
            '    end)\n'
            'end\n'
        )
        return_m = re.search(r'^return\b', s.source, re.MULTILINE)
        if return_m:
            s.source = s.source[:return_m.start()] + listener + s.source[return_m.start():]
        else:
            s.source = s.source.rstrip() + '\n' + listener
        fixes += 1
        log.info("  Added PickupItemEvent OnClientEvent listener in '%s'", s.name)
    return fixes


def _disable_default_controls_in_fps_scripts(scripts: list[RbxScript]) -> int:
    """Disable Roblox's default PlayerModule controls in FPS-style scripts.

    Detects client scripts that set ``MouseBehavior = Enum.MouseBehavior.LockCenter``
    (the unmistakable signature of an FPS controller) and prepends a one-time
    setup block that disables the default PlayerModule controls. Without this,
    the auto-loaded ``StarterPlayerScripts/PlayerModule`` resets MouseBehavior
    back to ``Default`` every frame, so the FPS controller's lock never sticks
    and mouse-look does not work.

    The prepended block is idempotent (guarded by an attribute check) so the
    same script can be cloned to multiple players without re-running.
    """
    fixes = 0
    # The marker line is the FIRST line of the prepended setup so an idempotent
    # `marker in s.source` check works whether the function runs again on the
    # same script (e.g. re-running write_output after editing on disk).
    marker = "-- u2r: disable default PlayerModule controls"
    # HumanoidRootPart sits ~3 studs above the feet, so a normally-grounded
    # character reads ~3 studs above the floor it stands on. The snap target
    # is therefore floor + 3y, and we compare HRP to *target* (not the raw
    # hit) so that "already standing" doesn't trip the threshold.
    setup = (
        f"{marker} + assert FPS mouse state + first-person body hide + spawn floor-snap\n"
        "-- Re-applies on CharacterAdded because Roblox's character spawn flow\n"
        "-- re-enables the default PlayerModule and resets MouseBehavior, and\n"
        "-- because Roblox loads avatar accessories asynchronously after the\n"
        "-- character spawns — DescendantAdded catches each late-added Handle\n"
        "-- so the user's hat/chain/glasses don't float across the FPS camera.\n"
        "do\n"
        "    local _lp = game:GetService(\"Players\").LocalPlayer\n"
        "    local _UIS = game:GetService(\"UserInputService\")\n"
        "    local function _applyFpsMouseState()\n"
        "        local _ps = _lp:WaitForChild(\"PlayerScripts\", 10)\n"
        "        local _pm = _ps and _ps:WaitForChild(\"PlayerModule\", 10)\n"
        "        if _pm then\n"
        "            -- pcall the require/GetControls/Disable trio because the\n"
        "            -- Roblox-managed PlayerModule API has shifted historically;\n"
        "            -- a future rename should not break the FPS lock.\n"
        "            local ok, mod = pcall(require, _pm)\n"
        "            if ok and mod then\n"
        "                local ok2, controls = pcall(function() return mod:GetControls() end)\n"
        "                if ok2 and controls and controls.Disable then\n"
        "                    pcall(function() controls:Disable() end)\n"
        "                end\n"
        "            end\n"
        "        end\n"
        "        _UIS.MouseBehavior = Enum.MouseBehavior.LockCenter\n"
        "        _UIS.MouseIconEnabled = false\n"
        "    end\n"
        "    -- Walk up parents looking for a node named WeaponSlot. The FPS\n"
        "    -- controller parents the held weapon under camera.WeaponSlot, so\n"
        "    -- anything reachable through that chain must stay visible.\n"
        "    local function _isInWeaponSlot(inst)\n"
        "        local p = inst.Parent\n"
        "        while p and p ~= game do\n"
        "            if p.Name == \"WeaponSlot\" then return true end\n"
        "            p = p.Parent\n"
        "        end\n"
        "        return false\n"
        "    end\n"
        "    local function _hidePart(part)\n"
        "        if (part:IsA(\"BasePart\") or part:IsA(\"Decal\")) and not _isInWeaponSlot(part) then\n"
        "            part.LocalTransparencyModifier = 1\n"
        "        end\n"
        "    end\n"
        "    local function _hideCharacter(char)\n"
        "        if not char then return end\n"
        "        -- Connect FIRST, then iterate the snapshot. Roblox can deliver\n"
        "        -- a DescendantAdded between snapshot capture and Connect, so\n"
        "        -- iterating first would let an accessory inserted in that gap\n"
        "        -- slip through both passes.\n"
        "        char.DescendantAdded:Connect(_hidePart)\n"
        "        for _, part in char:GetDescendants() do _hidePart(part) end\n"
        "    end\n"
        "    -- After Roblox finishes its respawn flow the character can land\n"
        "    -- in mid-air when the Unity SpawnPoint sits over a gap rather\n"
        "    -- than on a floor (the Unity transform is a marker; the converter\n"
        "    -- doesn't ground-snap it). Raycast down once and reseat the HRP\n"
        "    -- on whatever surface is directly below.\n"
        "    local function _snapToFloor(char)\n"
        "        if not char then return end\n"
        "        local hrp = char:WaitForChild(\"HumanoidRootPart\", 5)\n"
        "        if not hrp then return end\n"
        "        task.wait()\n"
        "        local rp = RaycastParams.new()\n"
        "        rp.FilterDescendantsInstances = {char}\n"
        "        rp.FilterType = Enum.RaycastFilterType.Exclude\n"
        "        -- Origin = HRP itself (the character is filter-excluded). Starting\n"
        "        -- above HRP risks hitting an overhead ceiling/bridge first and\n"
        "        -- snapping the player upward onto it.\n"
        "        local hit = workspace:Raycast(hrp.Position, Vector3.new(0, -200, 0), rp)\n"
        "        if not hit then return end\n"
        "        local target = hit.Position + Vector3.new(0, 3, 0)\n"
        "        if (hrp.Position - target).Magnitude > 2 then\n"
        "            -- Translate by delta to preserve the character's yaw/pitch\n"
        "            -- (CFrame.new(target) would reset rotation to identity).\n"
        "            hrp.CFrame = hrp.CFrame + (target - hrp.Position)\n"
        "        end\n"
        "    end\n"
        "    _applyFpsMouseState()\n"
        "    _hideCharacter(_lp.Character)\n"
        "    _snapToFloor(_lp.Character)\n"
        "    _lp.CharacterAdded:Connect(function(char)\n"
        "        task.wait()  -- let Roblox finish its respawn handling first\n"
        "        _applyFpsMouseState()\n"
        "        _hideCharacter(char)\n"
        "        _snapToFloor(char)\n"
        "    end)\n"
        "end\n\n"
    )
    for s in scripts:
        if s.script_type != "LocalScript":
            continue
        if marker in s.source:
            continue
        if not re.search(
            r"MouseBehavior\s*=\s*Enum\.MouseBehavior\.LockCenter", s.source
        ):
            continue
        s.source = setup + s.source
        fixes += 1
        log.info("  Disabled default PlayerModule controls in '%s'", s.name)
    return fixes


def _remove_stale_player_requires(scripts: list[RbxScript]) -> int:
    """Rewrite Player-as-module references when Player is actually a LocalScript.

    When the AI transpiler emits something like:

        local playerScript = ReplicatedStorage:WaitForChild("Player")
        local Player = require(playerScript)
        local healthUpdate = playerScript:WaitForChild("HealthUpdate")

    rewrite all three idioms together so we don't leave an orphan use
    of ``playerScript`` (would crash with "Attempted to call require with
    invalid argument(s)" at runtime, killing the whole script).

    Strategy: redirect the binding to ``Players.LocalPlayer:WaitForChild
    ("PlayerScripts"):WaitForChild("Player")`` (NOT ``script.Parent`` —
    pipeline.write_output's BasePart-guard heuristic regexes
    ``local \w+ = script.Parent\b`` and would prepend an early-exit guard
    to any script whose parent is StarterPlayerScripts). Then stub out
    ``require(NAME)`` since LocalScripts cannot be required.

    Restricted to LocalScripts only — server-side code has no LocalPlayer.
    """
    fixes = 0
    # Check if Player is a LocalScript
    player_is_local = any(
        s.name == 'Player' and s.script_type == 'LocalScript'
        for s in scripts
    )
    if not player_is_local:
        return 0

    for s in scripts:
        if s.name == 'Player':
            continue
        # Server-side: replace ``require(...:WaitForChild("Player"))`` with a
        # benign empty-API stub. The server can't require a LocalScript and
        # would either infinite-yield on WaitForChild or crash on the
        # require call. A stub keeps the script alive; cross-process state
        # sharing requires a future RemoteFunction round-trip.
        if s.script_type != 'LocalScript':
            stub_re = re.compile(
                r'^(?P<indent>\s*)local\s+(?P<var>\w+)\s*=\s*require\(\s*[^)\n]*:WaitForChild\(\s*["\']Player["\']\s*\)\s*\).*$',
                re.MULTILINE,
            )
            new_src, n = stub_re.subn(
                r'\g<indent>local \g<var> = { hasItems = function() return {} end, }'
                r'  -- server-side stub for client LocalScript "Player"',
                s.source,
            )
            if n:
                s.source = new_src
                fixes += 1
                log.info("  Stubbed Player require in server script '%s'", s.name)
            continue
        # Client-side: rewrite the WaitForChild binding to point at the
        # actual LocalScript location so subsequent code can read the
        # script Instance.
        original = s.source
        # Find the variable bound to a Player WaitForChild lookup so we can
        # rewrite both the binding line and any subsequent uses coherently.
        # Anchor to the full line so we capture any wrapping parens / trailing
        # tokens — a previous version stopped at the inner `:WaitForChild("Player")`
        # match and left orphan `)` characters that broke parsing (HudControl
        # and Plane both hit this).
        bind_match = re.search(
            r'^(?P<indent>\s*)local\s+(?P<var>\w+)\s*=\s*[^\n]*:WaitForChild\(\s*["\']Player["\']\s*\)[^\n]*$',
            s.source,
            flags=re.MULTILINE,
        )
        if bind_match:
            varname = bind_match.group("var")
            indent = bind_match.group("indent")
            # Redirect the binding to the actual LocalScript location at runtime.
            # Use the LocalPlayer.PlayerScripts path (works for any sibling
            # LocalScript) rather than `script.Parent`, because `script.Parent`
            # accesses trigger the BasePart-parent-guard heuristic in
            # pipeline.write_output (`local \w+ = script.Parent\b` matches any
            # script.Parent alias and adds an `if not script.Parent:IsA("BasePart")
            # then return end` prelude that would early-exit a client script
            # whose parent is StarterPlayerScripts).
            s.source = re.sub(
                r'^\s*local\s+\w+\s*=\s*[^\n]*:WaitForChild\(\s*["\']Player["\']\s*\)[^\n]*$',
                f'{indent}local {varname} = game:GetService("Players").LocalPlayer:WaitForChild("PlayerScripts"):WaitForChild("Player")',
                s.source,
                flags=re.MULTILINE,
            )
            # `require(varname)` cannot work — LocalScripts aren't requirable.
            # Replace with a stub so subsequent code doesn't crash on missing var.
            require_pattern = (
                r'^(\s*)local\s+(\w+)\s*=\s*require\(\s*'
                + re.escape(varname)
                + r'\s*\).*$'
            )
            stub = (
                r"\1local \2 = nil  -- Player is a LocalScript "
                "(not requirable); use BindableEvent children on the Player "
                "LocalScript instance"
            )
            s.source = re.sub(require_pattern, stub, s.source, flags=re.MULTILINE)
        # Catch direct `local X = require(<expr containing "Player">)` calls —
        # a different shape from the binding case above (no intermediate
        # variable, the lookup is inline in the require call).
        s.source = re.sub(
            r'^(\s*)local\s+(\w+)\s*=\s*require\([^)]*["\']Player["\'][^)]*\).*$',
            r"\1local \2 = nil  -- Player is a LocalScript (not requirable)",
            s.source,
            flags=re.MULTILINE,
        )
        if s.source != original:
            fixes += 1
            log.info("  Rewired stale Player require/lookup in '%s'", s.name)
    return fixes


def _expose_local_script_events(scripts: list[RbxScript]) -> int:
    """Make ``<Table>.<Event> = <Bindable>.Event`` accessible across LocalScripts.

    The AI transpiler emits a Unity-style class export pattern:

        local Player = {}
        local healthUpdateEvent = Instance.new("BindableEvent")
        Player.HealthUpdate = healthUpdateEvent.Event   -- producer
        ...
        Player.HealthUpdate:Connect(updateHealth)        -- consumer (other LocalScript)

    The producer's ``Player`` is a *local table* — siblings can't reach it
    through the LocalScript Instance. The consumer's ``Player`` is the
    LocalScript Instance itself (resolved via
    ``LocalPlayer.PlayerScripts:WaitForChild("Player")``). Indexing an
    Instance for a non-property key throws "X is not a valid member of
    LocalScript", so every cross-script event subscription fails.

    Fix: parent each producer-side BindableEvent to ``script`` with the
    table-key as its Name, then rewrite consumers' ``<var>.<Key>:Connect``
    to ``<var>:WaitForChild("<Key>").Event:Connect``. Only runs when the
    producer LocalScript exists in the script set.
    """
    fixes = 0
    # Pattern: local <bindableVar> = Instance.new("BindableEvent")
    bindable_decl = re.compile(
        r'local\s+(\w+)\s*=\s*Instance\.new\(\s*["\']BindableEvent["\']\s*\)',
    )
    # Pattern: <Table>.<Key> = <bindableVar>.Event
    expose_pat = re.compile(
        r'(\w+)\.(\w+)\s*=\s*(\w+)\.Event\b',
    )

    # Step 1 — find producer LocalScripts and the (table, key, bindable_var) triples they expose.
    # Map: producer_script_name -> set of exposed event keys
    producers: dict[str, dict[str, str]] = {}  # name -> {event_key: bindable_var}
    for s in scripts:
        if s.script_type != "LocalScript":
            continue
        bindables = set(bindable_decl.findall(s.source))
        if not bindables:
            continue
        exposed: dict[str, str] = {}
        for table_var, key, bvar in expose_pat.findall(s.source):
            if bvar in bindables:
                exposed[key] = bvar
        if exposed:
            producers[s.name] = exposed

    if not producers:
        return 0

    # Step 2 — patch each producer to parent its BindableEvents to ``script``.
    for s in scripts:
        if s.name not in producers:
            continue
        exposed = producers[s.name]
        original = s.source
        for key, bvar in exposed.items():
            # Insert a Name+Parent assignment immediately after the BindableEvent declaration,
            # but only once (skip if the marker already exists).
            marker = f'{bvar}.Name = "{key}"'
            if marker in s.source:
                continue
            decl_re = re.compile(
                rf'^(\s*local\s+{re.escape(bvar)}\s*=\s*Instance\.new\(\s*["\']BindableEvent["\']\s*\)\s*)$',
                re.MULTILINE,
            )
            insert = (
                rf'\1\n{marker}; '
                rf'{bvar}.Parent = script'
            )
            s.source = decl_re.sub(insert, s.source, count=1)
        if s.source != original:
            fixes += 1
            log.info("  Exposed %d BindableEvent(s) on producer '%s'", len(exposed), s.name)

    # Step 3 — rewrite consumers. A consumer binds the producer LocalScript via
    # ``WaitForChild("<ProducerName>")`` and then does ``<var>.<Key>:Connect(...)``.
    bind_re = re.compile(
        r'^\s*local\s+(\w+)\s*=\s*[^\n]*:WaitForChild\(\s*["\']([^"\']+)["\']\s*\)[^\n]*$',
        re.MULTILINE,
    )
    for s in scripts:
        if s.name in producers:
            continue  # producers don't need the consumer rewrite for their own table
        original = s.source
        # Find variables bound to producer LocalScripts (via WaitForChild)
        var_to_producer: dict[str, str] = {}
        for var, child_name in bind_re.findall(s.source):
            if child_name in producers:
                var_to_producer[var] = child_name
        if not var_to_producer:
            continue
        for var, prod in var_to_producer.items():
            for key in producers[prod]:
                pat = re.compile(rf'\b{re.escape(var)}\.{re.escape(key)}:Connect\(')
                repl = f'{var}:WaitForChild("{key}").Event:Connect('
                s.source = pat.sub(repl, s.source)
        if s.source != original:
            fixes += 1
            log.info("  Rewired %d cross-script event binding(s) in '%s'",
                     sum(len(producers[p]) for p in var_to_producer.values()),
                     s.name)
    return fixes


def _add_trigger_stay_polling(scripts: list[RbxScript]) -> int:
    """Approximate Unity ``OnTriggerStay`` for trigger-collider scripts.

    The AI typically translates Unity's ``OnTriggerStay`` to a single
    ``triggerCollider.Touched:Connect`` handler. ``Touched`` fires once per
    part-touch event, not every frame, so the script's per-frame angle
    or distance checks (e.g. a rotating turret cone) only get one shot
    when each player part initially contacts the trigger. If the angle
    fails on first contact, the player has to leave and re-enter to get
    another evaluation — turrets effectively never engage rotating
    targets.

    Detection: a script that names ``triggerCollider`` and binds
    ``Touched:Connect`` AND defines an ``angle <`` cone check inside that
    handler. Patch: append a Heartbeat polling loop using
    ``workspace:GetPartBoundsInRadius`` (works regardless of CanCollide)
    that re-runs the same engage logic at 6.7Hz while the script's
    ``state`` is non-Engaged.
    """
    fixes = 0
    poll_marker = "-- __TRIGGER_STAY_POLL__"
    for s in scripts:
        if poll_marker in s.source:
            continue
        if "triggerCollider" not in s.source:
            continue
        if "angle <" not in s.source:
            continue
        if "startEngaged" not in s.source:
            continue
        # Heuristic: only patch if the engage logic uses getTBase/sightRadius
        # or the converter-emitted helper functions. Avoids patching
        # arbitrary scripts that happen to mention angle.
        if not (
            "getTBase" in s.source
            and ("getSightRadius" in s.source or "sightRadius" in s.source)
        ):
            continue
        s.source += (
            "\n\n" + poll_marker + " Unity OnTriggerStay equivalent — Touched fires\n"
            "-- only on part-touch *events*, but Unity OnTriggerStay re-runs every\n"
            "-- physics frame. Polling lets a rotating turret eventually pick up\n"
            "-- a target whose initial-contact angle was outside the engagement cone.\n"
            "do\n"
            "    local _RunService = game:GetService(\"RunService\")\n"
            "    local _sightRadius = getSightRadius()\n"
            "    local _lastCheck = 0\n"
            "    _RunService.Heartbeat:Connect(function()\n"
            "        if state == State.Engaged then return end\n"
            "        if tick() - _lastCheck < 0.15 then return end\n"
            "        _lastCheck = tick()\n"
            "        local _base = getTBase()\n"
            "        if not _base then return end\n"
            "        local _basePos = getPosition(_base)\n"
            "        local _hits = workspace:GetPartBoundsInRadius(_basePos, _sightRadius)\n"
            "        for _, _p in ipairs(_hits) do\n"
            "            if isPlayerPart(_p) then\n"
            "                local _character = getPlayerCharacter(_p)\n"
            "                if _character then\n"
            "                    local _targetPart = _character:FindFirstChild(\"HumanoidRootPart\") or _p\n"
            "                    local _dir = _targetPart.Position - _basePos\n"
            "                    local _angle = vectorAngle(_dir, getForward(_base))\n"
            "                    if _angle < 55 then\n"
            "                        local _rp = RaycastParams.new()\n"
            "                        _rp.FilterDescendantsInstances = {model}\n"
            "                        _rp.FilterType = Enum.RaycastFilterType.Exclude\n"
            "                        local _res = workspace:Raycast(_basePos, _dir.Unit * _sightRadius, _rp)\n"
            "                        if _res and isPlayerPart(_res.Instance) then\n"
            "                            startEngaged(_targetPart)\n"
            "                            return\n"
            "                        end\n"
            "                    end\n"
            "                end\n"
            "            end\n"
            "        end\n"
            "    end)\n"
            "end\n"
        )
        fixes += 1
        log.info("  Added OnTriggerStay polling loop to '%s'", s.name)
    return fixes


_PICKUP_REPLACEMENT = """local RunService = game:GetService("RunService")
local Debris = game:GetService("Debris")

local container = script.Parent

-- Find the visible target. Prefer Models (e.g. Rifle, Battery) over Parts;
-- fall back to opaque Parts; last resort any Part. The Pickup container
-- typically has invisible Trigger Parts (Transparency=1) sitting alongside
-- the visible mesh content — the visual target is what should bob and rotate.
local function findVisualTarget(parent)
\tfor _, c in ipairs(parent:GetChildren()) do
\t\tif c:IsA("Model") and c.Name ~= "MinimapIcon" then return c end
\tend
\tfor _, c in ipairs(parent:GetChildren()) do
\t\tif c:IsA("BasePart") and c.Transparency < 1 then return c end
\tend
\tfor _, c in ipairs(parent:GetChildren()) do
\t\tif c:IsA("BasePart") then return c end
\tend
\treturn nil
end

local function findTriggerPart(parent)
\tlocal d = parent:FindFirstChild("PickupTouchDetector")
\tif d and d:IsA("BasePart") then return d end
\tlocal c = parent:FindFirstChild("Collider")
\tif c and c:IsA("BasePart") then return c end
\tfor _, x in ipairs(parent:GetChildren()) do
\t\tif x:IsA("BasePart") and x.Transparency >= 1 then return x end
\tend
\treturn nil
end

local target = findVisualTarget(container)
local trigger = findTriggerPart(container)
-- itemName is serialized on either the script (post-coherence) or the
-- container Model (Unity MonoBehaviour fields land on the parent Part/Model).
local itemName = script:GetAttribute("itemName")
\tor (container and container:GetAttribute("itemName"))
\tor ""
local rotationSpeed = 100
local source = container:FindFirstChildWhichIsA("Sound")

if not target then return end

local function getPivot()
\tif target:IsA("Model") then return target:GetPivot() end
\treturn target.CFrame
end
local function setPivot(cf)
\tif target:IsA("Model") then target:PivotTo(cf) else target.CFrame = cf end
end

-- Models need a PrimaryPart for PivotTo to work consistently.
if target:IsA("Model") and not target.PrimaryPart then
\tlocal p = target:FindFirstChildWhichIsA("BasePart")
\tif p then target.PrimaryPart = p end
end

local origin = getPivot().Position
local upPos = origin
local downPos = origin - Vector3.new(0, 0.5, 0)

local function moveDown()
\twhile target and target.Parent do
\t\tif getPivot().Position.Y <= downPos.Y + 0.05 then break end
\t\tlocal dt = RunService.Heartbeat:Wait()
\t\t-- Re-fetch pivot AFTER the wait so we don't clobber rotation
\t\t-- updates applied by the concurrent Heartbeat rotator below.
\t\tsetPivot(getPivot() + Vector3.new(0, -0.5 * dt, 0))
\tend
end
local function moveUp()
\twhile target and target.Parent do
\t\tif getPivot().Position.Y >= upPos.Y - 0.05 then break end
\t\tlocal dt = RunService.Heartbeat:Wait()
\t\tsetPivot(getPivot() + Vector3.new(0, 0.5 * dt, 0))
\tend
end

task.spawn(function()
\twhile target and target.Parent do moveDown(); moveUp() end
end)

RunService.Heartbeat:Connect(function(dt)
\tif target and target.Parent then
\t\tsetPivot(getPivot() * CFrame.Angles(0, math.rad(rotationSpeed) * dt, 0))
\tend
end)

local touchPart = trigger or (target:IsA("BasePart") and target or target:FindFirstChildWhichIsA("BasePart"))
if touchPart then
\ttouchPart.Touched:Connect(function(otherPart)
\t\tlocal character = otherPart:FindFirstAncestorOfClass("Model")
\t\tif character and game:GetService("Players"):GetPlayerFromCharacter(character) then
\t\t\tcharacter:SetAttribute("GetItem", itemName)
\t\t\tif source then source:Play() end
\t\t\tDebris:AddItem(container, 0)
\t\tend
\tend)
end
"""


def _fix_pickup_visual_target(scripts: list[RbxScript]) -> int:
    """Replace Pickup.luau bodies with Model-aware rotate+bob+touch logic.

    Detection: script name ``Pickup`` (Unity Pickup.cs convention) AND the
    body contains the AI-emitted ``rotationSpeed`` + ``moveDown`` / ``bobLoop``
    pattern bound to ``script.Parent``. The replacement is structurally the
    same Unity behavior (downward then upward bob + Y rotation + Touched
    pickup) but routes through ``PivotTo`` for Models so a child Rifle /
    Battery Model is what visually animates, not the invisible trigger Part.
    """
    fixes = 0
    for s in scripts:
        if s.name != "Pickup":
            continue
        # Anchor on tokens specific to the Unity Pickup.cs translation
        # so we don't replace unrelated scripts that happen to share a name.
        if not (
            "rotationSpeed" in s.source
            and ("moveDown" in s.source or "MoveDown" in s.source)
            and "Touched" in s.source
            and "GetItem" in s.source
        ):
            continue
        s.source = _PICKUP_REPLACEMENT
        fixes += 1
        log.info("  Rewired Pickup '%s' to Model-aware rotate+bob", s.name)
    return fixes
