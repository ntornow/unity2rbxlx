"""
storage_classifier.py -- Phase 4a.5: server/client/replicated container assignment.

Unity has no networking model; Roblox replicates between server and client.
Every module needs an explicit container. This module answers, per script:

1. Does the server need it?
2. Does the client need it?
3. Do both need it?

Output: a StoragePlan with a concrete parent_path on each RbxScript.
rbxlx_writer.py and luau_place_builder.py route by parent_path.

Rules (first match wins):

  - client-only API surface       -> StarterPlayerScripts (LocalScript)
  - character-attached            -> StarterCharacterScripts (LocalScript)
  - name hint *Loading* / *Boot*  -> ReplicatedFirst
  - ModuleScript reached by client -> ReplicatedStorage
  - ModuleScript only server      -> ServerStorage
  - otherwise                     -> ServerScriptService

Ambiguity: default to ReplicatedStorage. Misplacing into ReplicatedStorage
degrades security; misplacing into ServerStorage breaks the game.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from core.roblox_types import RbxScript

log = logging.getLogger(__name__)


# Parent-path strings that rbxlx_writer and luau_place_builder recognize.
SERVER_SCRIPT_SERVICE = "ServerScriptService"
SERVER_STORAGE = "ServerStorage"
REPLICATED_STORAGE = "ReplicatedStorage"
REPLICATED_FIRST = "ReplicatedFirst"
STARTER_PLAYER_SCRIPTS = "StarterPlayer.StarterPlayerScripts"
STARTER_CHARACTER_SCRIPTS = "StarterPlayer.StarterCharacterScripts"
STARTER_GUI = "StarterGui"


# APIs that ONLY work on the client — any script touching these must run client-side.
_CLIENT_ONLY_PATTERNS = [
    r"Players\.LocalPlayer",
    r'GetService\(["\']Players["\']\)\.LocalPlayer',
    r'GetService\(["\']UserInputService["\']\)',
    r"UserInputService",
    r"workspace\.CurrentCamera",
    r'GetService\(["\']StarterGui["\']\)',
    r"LocalPlayer\.Character",
    r"\.PlayerGui",
    r"mouse\.Hit",
    r"mouse\.Target",
    r'GetService\(["\']ContextActionService["\']\)',
    r'GetService\(["\']GuiService["\']\)',
]

# APIs that ONLY work on the server.
_SERVER_ONLY_PATTERNS = [
    r"\.OnServerEvent",
    r":FireClient\(",
    r":FireAllClients\(",
    r'GetService\(["\']DataStoreService["\']\)',
    r'GetService\(["\']MessagingService["\']\)',
    r'GetService\(["\']ServerStorage["\']\)',
    r'GetService\(["\']ServerScriptService["\']\)',
]

# Name hints for ReplicatedFirst scripts (loaders / splash screens that must
# run before full replication).
_REPLICATED_FIRST_HINTS = re.compile(
    r"(?i)(loading|loader|boot|bootstrap|splash|preload|intro)"
)

# Name hints for server-secret templates (prefabs that genuinely must be hidden
# from clients — admin tools, cheat-detection prefabs, etc.).
_SERVER_SECRET_HINTS = re.compile(
    r"(?i)(admin|secret|server(only|_only|-only)?|cheat)"
)


@dataclass
class StoragePlan:
    """Explicit per-script and per-template container assignments."""

    # Scripts
    server_scripts: list[str] = field(default_factory=list)
    client_scripts: list[str] = field(default_factory=list)
    character_scripts: list[str] = field(default_factory=list)
    replicated_first_scripts: list[str] = field(default_factory=list)
    shared_modules: list[str] = field(default_factory=list)
    server_modules: list[str] = field(default_factory=list)

    # Templates (forward-looking; populated when templates_manifest is wired)
    replicated_templates: list[str] = field(default_factory=list)
    server_templates: list[str] = field(default_factory=list)
    ui_templates: list[str] = field(default_factory=list)

    # RemoteEvents / RemoteFunctions (always ReplicatedStorage by Roblox rules)
    remote_events: list[str] = field(default_factory=list)

    # Audit trail: ("script_name", "original_container", "assigned_container", "reason")
    decisions: list[dict[str, str]] = field(default_factory=list)

    # Agent-applied overrides from manual editing of conversion_plan.json.
    overrides_applied: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_storage(
    scripts: list[RbxScript],
    *,
    dependency_map: dict[str, list[str]] | None = None,
    character_script_names: Iterable[str] | None = None,
    template_names: Iterable[str] | None = None,
    template_spawn_callers: dict[str, list[str]] | None = None,
) -> StoragePlan:
    """Assign each script a concrete parent_path and build a StoragePlan.

    Mutates ``scripts`` in-place: sets ``script.parent_path`` on every entry.

    Args:
        scripts: Transpiled scripts to classify.
        dependency_map: ``class_name -> [referenced_class_names]``. Used to build
            the require() call graph. If omitted, falls back to regex-scanning
            ``require(...)`` calls in script source.
        character_script_names: Scripts attached to a player character prefab
            (per scene wiring). Forced to StarterCharacterScripts.
        template_names: Prefab template names (forward-looking — used when
            ``templates_manifest`` is wired through).
        template_spawn_callers: ``template_name -> [caller_script_names]``.
            Decides replicated vs server container.

    Returns:
        StoragePlan describing every container assignment with an audit trail.
    """
    plan = StoragePlan()
    character_set = set(character_script_names or [])
    script_by_name: dict[str, RbxScript] = {s.name: s for s in scripts}

    call_graph = _build_call_graph(scripts, dependency_map)
    client_touchers = _scripts_with_client_apis(scripts)
    server_touchers = _scripts_with_server_apis(scripts)

    for s in scripts:
        container, reason = _decide_script_container(
            s,
            call_graph=call_graph,
            client_touchers=client_touchers,
            server_touchers=server_touchers,
            character_set=character_set,
            script_by_name=script_by_name,
        )
        s.parent_path = container

        # Roblox requires LocalScript parent = StarterPlayer(.StarterPlayerScripts)
        # or StarterCharacterScripts; Script parent = ServerScriptService or
        # ServerStorage won't run; ModuleScript can live anywhere but is only
        # useful where callers can reach it. Keep script_type aligned with the
        # assigned container:
        if container == STARTER_PLAYER_SCRIPTS or container == STARTER_CHARACTER_SCRIPTS:
            if s.script_type != "LocalScript" and s.script_type != "ModuleScript":
                s.script_type = "LocalScript"
        elif container == SERVER_SCRIPT_SERVICE:
            if s.script_type == "LocalScript":
                # Client script in SSS would never run — keep LocalScript, place
                # in StarterPlayerScripts instead.
                s.parent_path = STARTER_PLAYER_SCRIPTS
                container = STARTER_PLAYER_SCRIPTS
                reason += " (forced to StarterPlayerScripts: LocalScript cannot live in SSS)"
        # ModuleScripts keep whatever container was chosen.

        plan.decisions.append({
            "script": s.name,
            "script_type": s.script_type,
            "container": container,
            "reason": reason,
        })
        _append_to_bucket(plan, s.name, s.script_type, container)

    _collect_remote_event_names(plan, scripts)
    _assign_template_containers(
        plan,
        template_names=template_names,
        template_spawn_callers=template_spawn_callers,
        client_touchers=client_touchers,
    )

    log.info(
        "[storage_classifier] %d scripts placed: %d server, %d client, %d character, "
        "%d replicated_first, %d shared_modules, %d server_modules",
        len(plan.decisions),
        len(plan.server_scripts),
        len(plan.client_scripts),
        len(plan.character_scripts),
        len(plan.replicated_first_scripts),
        len(plan.shared_modules),
        len(plan.server_modules),
    )

    return plan


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_call_graph(
    scripts: list[RbxScript],
    dependency_map: dict[str, list[str]] | None,
) -> dict[str, set[str]]:
    """Return caller -> set(callees) built from dependency_map or source scan."""
    graph: dict[str, set[str]] = {s.name: set() for s in scripts}
    script_names = set(graph.keys())

    if dependency_map:
        for caller, callees in dependency_map.items():
            if caller not in graph:
                continue
            graph[caller].update(c for c in callees if c in script_names)

    # Always augment with source-scan: dependency_map is from C# analyzer and may
    # miss require() calls injected post-transpile. Walk each `require(...)` and
    # extract candidate names from its full body — including nested parens like
    # `require(game:GetService("ServerStorage"):FindFirstChild("Utility"))`.
    quoted_pat = re.compile(r'["\'](\w+)["\']')
    dotted_pat = re.compile(r'\.(\w+)')
    for s in scripts:
        for m in re.finditer(r'require\(', s.source):
            body = _balanced_paren_body(s.source, m.end())
            for q in quoted_pat.finditer(body):
                name = q.group(1)
                if name in script_names and name != s.name:
                    graph[s.name].add(name)
            for d in dotted_pat.finditer(body):
                name = d.group(1)
                if name in script_names and name != s.name:
                    graph[s.name].add(name)

    return graph


def _balanced_paren_body(source: str, start: int) -> str:
    """Return the substring from ``start`` to the matching close-paren.

    ``start`` should point to the first character after an opening ``(``.
    Handles nested parens by bracket counting. Bounded scan (max 1024 chars)
    to stay O(n) overall.
    """
    depth = 1
    end = start
    limit = min(len(source), start + 1024)
    while end < limit and depth > 0:
        c = source[end]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        end += 1
    return source[start:end]


def _scripts_with_client_apis(scripts: list[RbxScript]) -> set[str]:
    """Names of scripts whose source matches any client-only API pattern."""
    return {
        s.name for s in scripts
        if any(re.search(p, s.source) for p in _CLIENT_ONLY_PATTERNS)
    }


def _scripts_with_server_apis(scripts: list[RbxScript]) -> set[str]:
    """Names of scripts whose source matches any server-only API pattern."""
    return {
        s.name for s in scripts
        if any(re.search(p, s.source) for p in _SERVER_ONLY_PATTERNS)
    }


def _decide_script_container(
    s: RbxScript,
    *,
    call_graph: dict[str, set[str]],
    client_touchers: set[str],
    server_touchers: set[str],
    character_set: set[str],
    script_by_name: dict[str, RbxScript],
) -> tuple[str, str]:
    """Return (container, reason) for a single script."""
    # Character-attached scripts go to StarterCharacterScripts.
    if s.name in character_set:
        return STARTER_CHARACTER_SCRIPTS, "character-attached per scene wiring"

    # Loader/splash scripts that need to run before full replication.
    if _REPLICATED_FIRST_HINTS.search(s.name) and s.script_type != "ModuleScript":
        return REPLICATED_FIRST, f"name hint matches ReplicatedFirst pattern ({s.name})"

    # ModuleScripts: route by who requires them.
    if s.script_type == "ModuleScript":
        callers = _find_callers(s.name, call_graph)
        if not callers:
            # Orphan module — nobody requires it. Default to ReplicatedStorage
            # so that if a late-added caller wants it, it can reach it.
            return REPLICATED_STORAGE, "orphan module (no callers): default replicated"

        caller_is_client = any(
            (c in client_touchers) or _caller_is_local_script(c, script_by_name)
            for c in callers
        )
        caller_is_server = any(
            (c in server_touchers) or _caller_is_server_script(c, script_by_name)
            for c in callers
        )

        if caller_is_client:
            return REPLICATED_STORAGE, (
                f"required by {len(callers)} caller(s), at least one client-side"
            )
        if caller_is_server and not caller_is_client:
            return SERVER_STORAGE, (
                f"required only by server-side callers ({len(callers)})"
            )
        return REPLICATED_STORAGE, (
            f"required by {len(callers)} caller(s), client/server unknown: default replicated"
        )

    # Scripts with client-only APIs must be LocalScripts.
    if s.name in client_touchers and s.name not in server_touchers:
        return STARTER_PLAYER_SCRIPTS, "uses client-only APIs (LocalPlayer, UserInputService, etc.)"

    # LocalScripts default to StarterPlayerScripts.
    if s.script_type == "LocalScript":
        return STARTER_PLAYER_SCRIPTS, "LocalScript (default container)"

    # Everything else is a server Script.
    return SERVER_SCRIPT_SERVICE, "server Script (default)"


def _find_callers(target: str, call_graph: dict[str, set[str]]) -> set[str]:
    """Return the set of script names that require() the target."""
    return {caller for caller, callees in call_graph.items() if target in callees}


def _caller_is_local_script(name: str, script_by_name: dict[str, RbxScript]) -> bool:
    s = script_by_name.get(name)
    return bool(s and s.script_type == "LocalScript")


def _caller_is_server_script(name: str, script_by_name: dict[str, RbxScript]) -> bool:
    s = script_by_name.get(name)
    return bool(s and s.script_type == "Script")


def _append_to_bucket(
    plan: StoragePlan,
    name: str,
    script_type: str,
    container: str,
) -> None:
    """Record the script name in the right StoragePlan bucket."""
    if container == SERVER_SCRIPT_SERVICE:
        plan.server_scripts.append(name)
    elif container == STARTER_PLAYER_SCRIPTS:
        plan.client_scripts.append(name)
    elif container == STARTER_CHARACTER_SCRIPTS:
        plan.character_scripts.append(name)
    elif container == REPLICATED_FIRST:
        plan.replicated_first_scripts.append(name)
    elif container == REPLICATED_STORAGE:
        plan.shared_modules.append(name)
    elif container == SERVER_STORAGE:
        plan.server_modules.append(name)


def _collect_remote_event_names(plan: StoragePlan, scripts: list[RbxScript]) -> None:
    """Scan scripts for RemoteEvent/RemoteFunction references."""
    pat = re.compile(
        r'(?:FindFirstChild|WaitForChild)\s*\(\s*["\']([^"\']+)["\']'
    )
    candidates: set[str] = set()
    for s in scripts:
        for m in pat.finditer(s.source):
            candidates.add(m.group(1))

    # Filter to likely RemoteEvent names — require a FireServer / OnServerEvent /
    # FireClient / OnClientEvent / FireAllClients reference somewhere.
    remote_signatures = ("FireServer", "OnServerEvent", "FireClient",
                         "OnClientEvent", "FireAllClients")
    for name in candidates:
        for s in scripts:
            if f'"{name}"' in s.source and any(sig in s.source for sig in remote_signatures):
                plan.remote_events.append(name)
                break
    plan.remote_events = sorted(set(plan.remote_events))


def _assign_template_containers(
    plan: StoragePlan,
    *,
    template_names: Iterable[str] | None,
    template_spawn_callers: dict[str, list[str]] | None,
    client_touchers: set[str],
) -> None:
    """Assign prefab templates to ReplicatedStorage vs ServerStorage.

    Forward-looking: templates_manifest is not yet wired through the pipeline.
    When it is, each template gets a container based on who clones it.
    """
    if not template_names:
        return

    spawn_callers = template_spawn_callers or {}
    for name in template_names:
        if _SERVER_SECRET_HINTS.search(name):
            plan.server_templates.append(name)
            continue
        callers = spawn_callers.get(name, [])
        if not callers:
            # Unknown caller set — default replicated so both sides can reach it.
            plan.replicated_templates.append(name)
            continue
        if any(c in client_touchers for c in callers):
            plan.replicated_templates.append(name)
        else:
            # Server-only spawn: default replicated (Roblox replicates server-
            # parented clones automatically). ServerStorage is reserved for
            # templates genuinely hidden from clients.
            plan.replicated_templates.append(name)
