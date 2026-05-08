"""Project-specific patch packs for script_coherence.

A patch pack is a post-transpile script-mutation pass that targets a
specific game pattern (e.g. FPS rifle pickup) rather than a generic
transpilation problem. Packs live here to keep ``script_coherence.py``
focused on generic Luau-coherence concerns.

Each pack registers itself via the ``@patch_pack(...)`` decorator, which:
  - Records a name and description for diagnostics
  - Records ordering dependencies (``after=[name, ...]``) — packs run in
    topological order; a cycle raises at import time
  - Records a ``detect(scripts) -> bool`` callback so the pack only runs
    on projects where its target pattern is present (Gamekit3D shouldn't
    get FPS rifle code injected)

Public entry point: :func:`run_packs`. Called by
:func:`script_coherence.fix_require_classifications` after the generic
passes complete.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.roblox_types import RbxScript

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PackApplyFn = Callable[[list["RbxScript"]], int]
PackDetectFn = Callable[[list["RbxScript"]], bool]


@dataclass(frozen=True)
class PatchPack:
    name: str
    description: str
    after: tuple[str, ...]
    detect: PackDetectFn
    apply: PackApplyFn


_REGISTRY: list[PatchPack] = []


def patch_pack(
    *,
    name: str,
    description: str = "",
    after: tuple[str, ...] = (),
    detect: PackDetectFn,
) -> Callable[[PackApplyFn], PackApplyFn]:
    """Register a script-coherence patch pack.

    The decorated function is the pack's apply() — it mutates ``scripts``
    in place and returns the number of edits made.
    """

    def _decorator(fn: PackApplyFn) -> PackApplyFn:
        if any(p.name == name for p in _REGISTRY):
            raise ValueError(f"patch_pack {name!r} already registered")
        _REGISTRY.append(
            PatchPack(
                name=name,
                description=description,
                after=tuple(after),
                detect=detect,
                apply=fn,
            )
        )
        return fn

    return _decorator


def _topological_order(packs: list[PatchPack]) -> list[PatchPack]:
    """Return packs in dependency order. Raises ValueError on cycle."""
    by_name = {p.name: p for p in packs}
    # Validate every `after` reference exists
    for p in packs:
        for dep in p.after:
            if dep not in by_name:
                raise ValueError(
                    f"patch_pack {p.name!r} declares after={dep!r} but no "
                    f"such pack is registered"
                )

    visited: set[str] = set()
    visiting: set[str] = set()
    order: list[PatchPack] = []

    def _visit(p: PatchPack) -> None:
        if p.name in visited:
            return
        if p.name in visiting:
            raise ValueError(
                f"patch_pack dependency cycle detected involving {p.name!r}"
            )
        visiting.add(p.name)
        for dep_name in p.after:
            _visit(by_name[dep_name])
        visiting.discard(p.name)
        visited.add(p.name)
        order.append(p)

    for p in packs:
        _visit(p)
    return order


def run_packs(
    scripts: list["RbxScript"],
    *,
    enabled: set[str] | None = None,
    disabled: set[str] | None = None,
) -> int:
    """Run every registered pack whose detector matches.

    ``enabled`` and ``disabled`` are explicit overrides:
      - If ``enabled`` is given, only packs in the set run (regardless of
        detect()), and a name not in the registry raises ValueError.
      - If ``disabled`` is given, packs in that set are skipped entirely.
    """
    enabled = enabled or None
    disabled = disabled or set()

    ordered = _topological_order(_REGISTRY)

    if enabled is not None:
        unknown = enabled - {p.name for p in ordered}
        if unknown:
            raise ValueError(
                f"unknown patch_pack name(s): {sorted(unknown)}; "
                f"registered: {sorted(p.name for p in ordered)}"
            )

    total_fixes = 0
    for pack in ordered:
        if pack.name in disabled:
            log.debug("patch_pack %r: disabled by caller", pack.name)
            continue
        if enabled is not None:
            if pack.name not in enabled:
                continue
            should_run = True
        else:
            should_run = pack.detect(scripts)
        if not should_run:
            log.debug("patch_pack %r: detector returned False, skipping",
                      pack.name)
            continue
        fixes = pack.apply(scripts)
        if fixes:
            log.info("patch_pack %r: %d edit(s)", pack.name, fixes)
        total_fixes += fixes
    return total_fixes


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def _detect_fps_rifle_pickup(scripts: list["RbxScript"]) -> bool:
    """Pack runs when any script references a rifle pickup pattern.

    The Unity SimpleFPS sample (and any project copying its rifle pickup
    convention) names the world prefab ``riflePrefab`` and exposes a
    ``GetRifle`` function in the Player controller. Either marker is
    enough to enable the pack.
    """
    return any(
        "riflePrefab" in s.source
        or "RiflePrefab" in s.source
        or "GetRifle" in s.source
        for s in scripts
    )


# ---------------------------------------------------------------------------
# Pack: fps_rifle_pickup
# ---------------------------------------------------------------------------

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

local _Players = game:GetService("Players")
local _RS = game:GetService("ReplicatedStorage")
-- Pickup→Player must cross the server/client boundary: Pickup is a server
-- Script and Player runs as a LocalScript, so SetAttribute on the client's
-- character won't replicate. Fire a RemoteEvent in ReplicatedStorage instead;
-- rbxlx_writer auto-creates `PickupItemEvent` because this script references
-- it with FireClient. Player's coherence pack adds the OnClientEvent listener.
local _pickupEvent = _RS:FindFirstChild("PickupItemEvent")
local touchPart = trigger or (target:IsA("BasePart") and target or target:FindFirstChildWhichIsA("BasePart"))
if touchPart then
\ttouchPart.Touched:Connect(function(otherPart)
\t\tlocal character = otherPart:FindFirstAncestorOfClass("Model")
\t\tlocal player = character and _Players:GetPlayerFromCharacter(character)
\t\tif player then
\t\t\t-- Persist the pickup as a server-side Player attribute so server
\t\t\t-- scripts (e.g. Door checking ``Player.hasKey``) can react. The
\t\t\t-- client-side Player LocalScript also flips its own LocalPlayer
\t\t\t-- attribute via the FireClient below, but ``LocalPlayer:SetAttribute``
\t\t\t-- on the client doesn't replicate to the server — so any server
\t\t\t-- consumer of ``hasKey``/``hasRifle`` never sees the flag without
\t\t\t-- this server-side write. Player Object attributes set server-side
\t\t\t-- DO replicate, so the client read still works.
\t\t\tif itemName and itemName ~= "" then
\t\t\t\tplayer:SetAttribute("has" .. itemName, true)
\t\t\tend
\t\t\tif _pickupEvent then _pickupEvent:FireClient(player, itemName) end
\t\t\tif source then source:Play() end
\t\t\tDebris:AddItem(container, 0)
\t\tend
\tend)
end
"""


@patch_pack(
    name="fps_rifle_inject",
    description="Inject a working FPS rifle pickup system into Player scripts "
    "that the AI transpiler emits as a stub.",
    detect=_detect_fps_rifle_pickup,
)
def _inject_fps_rifle_system(scripts: list["RbxScript"]) -> int:
    """Replace the AI-generated GetRifle stub with a working version.

    Steps:
    1. Find riflePrefab in workspace
    2. Clone, scale, make visible, weld sub-parts
    3. Parent to workspace (not character) with anchored parts
    4. Update position every frame in RenderStepped to follow camera
    5. Add client-side Touched detection on character parts
    """
    fixes = 0
    for s in scripts:
        if 'GetRifle' not in s.source:
            continue
        if '-- _FPS_RIFLE_SYSTEM' in s.source:
            continue

        original = s.source

        s.source = s.source.replace(
            'local gotWeapon = false',
            'local gotWeapon = false\nlocal _fpsRifle = nil  -- _FPS_RIFLE_SYSTEM\nlocal _fpsRiflePrimary = nil',
        )

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

        if 'RunService.RenderStepped:Connect' in s.source:
            s.source = s.source.replace(
                'RunService.RenderStepped:Connect(function(dt)',
                'RunService.RenderStepped:Connect(function(dt)\n'
                '    if _fpsRifle and _fpsRiflePrimary and _fpsRiflePrimary.Parent then\n'
                '        _fpsRifle:PivotTo(workspace.CurrentCamera.CFrame * CFrame.new(0.5, -0.5, -3))\n'
                '    end',
            )

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
            s.source = s.source.rstrip() + '\n' + touched_code

        if s.source != original:
            fixes += 1
            log.info("  Injected FPS rifle system in '%s'", s.name)

    return fixes


# ---------------------------------------------------------------------------
# Pack: pickup_remote_event
# ---------------------------------------------------------------------------

# Match ``<receiver>:SetAttribute("PickupItem"|"GetItem", itemName)`` —
# the canonical AI-transpiled-Pickup-handler shape we rewrite. Module-level
# so the detector and the apply function share one source of truth.
_PICKUP_SETATTRIBUTE_RE = re.compile(
    r'[a-zA-Z_][\w.]*\s*:\s*SetAttribute\s*\(\s*"(?:PickupItem|GetItem)"\s*,\s*itemName\s*\)'
)


def _detect_pickup_setattribute_pattern(scripts: list["RbxScript"]) -> bool:
    """Detector for ``pickup_remote_event_server``.

    Fires on any project that has a ``Pickup``-named script writing
    ``character:SetAttribute("GetItem"|"PickupItem", itemName)``. This
    is the post-AI-transpile shape the pack rewrites. Detector is
    intentionally name-agnostic — the previous gate ``_detect_fps_rifle_pickup``
    bound this to FPS-rifle projects only, but ``door_global_player_to_attribute``
    relies on the server-attribute write the pack injects, and pickup
    patterns appear in any genre. Without a generic detector, key doors
    in non-rifle projects would be rewritten to ``GetAttribute("hasKey")``
    on a flag nobody writes.
    """
    return any(
        s.name == "Pickup" and _PICKUP_SETATTRIBUTE_RE.search(s.source or "")
        for s in scripts
    )


# Match ``getItem(`` or ``GetItem(`` as an UNQUALIFIED symbol — a real
# top-level function definition or call. The negative lookbehind
# ``(?<![.:])`` rejects qualified accesses like ``inventory.getItem(``
# or ``self:getItem(`` because the listener body the pack injects calls
# bare ``getItem(itemName)``; if a script only references getItem
# through a namespace, that bare call would raise on the first pickup
# event. ``\b`` before excludes ``getitem`` inside longer identifiers
# (e.g. ``getItemized``); ``\(`` after excludes ``getItemModule(``.
_GETITEM_SYMBOL_RE = re.compile(r'(?<![.:])\b(?:get|Get)Item\s*\(')


def _detect_pickup_remote_event_in_use(scripts: list["RbxScript"]) -> bool:
    """Detector for ``pickup_remote_event_client``.

    Detectors run lazily inside ``run_packs`` — by the time this fires,
    ``pickup_remote_event_server`` has already rewritten Pickup scripts
    to ``FireClient(_pl, itemName)`` on ``PickupItemEvent``. Detect that
    post-rewrite shape rather than the pre-rewrite ``SetAttribute``
    pattern, so the client listener installs whenever the server pack
    has fired — including non-FPS projects where the legacy
    ``GetItem``-attribute bridge no longer exists.

    Also fires for projects whose Pickup was authored to use
    ``PickupItemEvent`` directly (the canonical ``_PICKUP_REPLACEMENT``
    body), so the listener install isn't gated on the rewrite path.
    """
    return any(
        s.name == "Pickup" and "PickupItemEvent" in (s.source or "")
        for s in scripts
    )


@patch_pack(
    name="pickup_remote_event_server",
    description="Convert Pickup script SetAttribute calls to "
    "ReplicatedStorage.PickupItemEvent:FireClient — server-side "
    "SetAttribute does not trigger client GetAttributeChangedSignal. "
    "Also injects a server-side ``player:SetAttribute('has'..itemName, true)`` "
    "before FireClient so server scripts (Door, etc.) can read replicated "
    "gameplay flags.",
    detect=_detect_pickup_setattribute_pattern,
)
def _convert_pickup_to_remote_event(scripts: list["RbxScript"]) -> int:
    fixes = 0
    # Match `<receiver>:SetAttribute("PickupItem"|"GetItem", itemName)` where
    # the receiver may be `target`, `character`, `player.Character`, or `other`
    # (different variable names produced by the AI-transpiled Pickup, the
    # _PICKUP_REPLACEMENT template, and rule-based outputs). The receiver
    # expression is captured so we can derive the player from it.
    pattern = re.compile(
        r'([a-zA-Z_][\w.]*)\s*:\s*SetAttribute\s*\(\s*"(PickupItem|GetItem)"\s*,\s*itemName\s*\)'
    )
    for s in scripts:
        if s.name != 'Pickup':
            continue

        def _replace(m: "re.Match[str]") -> str:
            receiver = m.group(1)
            return (
                'do\n'
                '\t\t\tlocal _pe = game:GetService("ReplicatedStorage"):FindFirstChild("PickupItemEvent")\n'
                f'\t\t\tlocal _char = {receiver}\n'
                '\t\t\tlocal _pl = _char and game:GetService("Players"):GetPlayerFromCharacter(_char)\n'
                '\t\t\t-- Persist the pickup as a server-side Player attribute so server\n'
                '\t\t\t-- scripts (e.g. Door checking ``player:GetAttribute("hasKey")``) can\n'
                '\t\t\t-- react. ``LocalPlayer:SetAttribute`` from the client does not\n'
                '\t\t\t-- replicate, so any server consumer of ``hasKey``/``hasRifle``\n'
                '\t\t\t-- needs this server-side write. Object attributes set server-side\n'
                '\t\t\t-- DO replicate, so the client read in Player.luau still works.\n'
                '\t\t\tif _pl and itemName and itemName ~= "" then\n'
                '\t\t\t\t_pl:SetAttribute("has" .. itemName, true)\n'
                '\t\t\tend\n'
                '\t\t\tif _pe and _pl then _pe:FireClient(_pl, itemName) end\n'
                '\t\tend'
            )

        new_src, count = pattern.subn(_replace, s.source)
        if count:
            s.source = new_src
            fixes += count
            log.info(
                "  Converted %d Pickup SetAttribute -> PickupItemEvent:FireClient",
                count,
            )

        # Unity's MonoBehaviour serialized fields land as attributes on the
        # parent Model (the GameObject), not on the Script — but the AI
        # transpiler emits ``script:GetAttribute("itemName")`` which always
        # returns nil and falls back to ``""``. Replace each
        # ``local <name> = script:GetAttribute("<attr>")`` with a walk-up
        # search that checks each ancestor's attribute. Without this,
        # every AI-transpiled Pickup fires the RemoteEvent with an empty
        # string and the client listener filters it out (no rifle equip,
        # no health/ammo apply).
        attr_pattern = re.compile(
            r'(local\s+\w+\s*=\s*)script:GetAttribute\(\s*"(\w+)"\s*\)'
        )

        def _attr_replace(m: "re.Match[str]") -> str:
            decl = m.group(1)
            attr = m.group(2)
            return (
                f'{decl}(function() local _c=script.Parent; '
                f'while _c do local v=_c:GetAttribute("{attr}"); '
                f'if v ~= nil then return v end; _c=_c.Parent end; '
                f'return script:GetAttribute("{attr}") end)()'
            )

        new_src2, count2 = attr_pattern.subn(_attr_replace, s.source)
        if count2:
            s.source = new_src2
            fixes += count2
            log.info(
                "  Promoted %d Pickup script:GetAttribute reads to walk-up search",
                count2,
            )
    return fixes


# ---------------------------------------------------------------------------
# Pack: pickup_remote_event_canonical_name
# ---------------------------------------------------------------------------
#
# Lifted out of the FPS-rifle gate so it applies to any project that has a
# Pickup-named server script (RPGs, platformers, FPSes — pickups are a
# generic Unity gameplay convention). Independent AI passes invent
# different RemoteEvent names for the same producer/consumer pair
# (``PickupGetItem`` on the server-side Pickup, ``PickupItemEvent`` on the
# client-side listener); without canonicalization the two sides don't
# talk to each other.
_PICKUP_REMOTE_ALIAS_RE = re.compile(
    r'"(Pickup(?:GetItem|Get|Event|Item|Remote))"'
)


def _detect_pickup_script_with_remote_alias(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Pickup":
            continue
        if _PICKUP_REMOTE_ALIAS_RE.search(s.source or ""):
            return True
    return False


@patch_pack(
    name="pickup_remote_event_canonical_name",
    description="Canonicalize any ``Pickup*`` RemoteEvent name in a "
    "Pickup-named script to ``PickupItemEvent`` so the consumer-side "
    "listener (which the producer/consumer bridge or _add_pickup_remote_listener "
    "wires up) actually fires.",
    detect=_detect_pickup_script_with_remote_alias,
)
def _canonicalize_pickup_remote_event(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name != "Pickup":
            continue
        renamed = 0

        def _alias_replace(m: "re.Match[str]") -> str:
            nonlocal renamed
            if m.group(1) == "PickupItemEvent":
                return m.group(0)
            renamed += 1
            return '"PickupItemEvent"'

        new_src = _PICKUP_REMOTE_ALIAS_RE.sub(_alias_replace, s.source)
        if renamed:
            s.source = new_src
            fixes += renamed
            log.info(
                "  Canonicalized %d Pickup RemoteEvent name(s) to PickupItemEvent",
                renamed,
            )
    return fixes


@patch_pack(
    name="pickup_remote_event_client",
    description="Add OnClientEvent listener for PickupItemEvent in any "
    "Player-style controller (any script with a ``getItem``/``GetItem`` "
    "dispatch). Coupled to ``pickup_remote_event_server`` so the listener "
    "is installed wherever the server-side Pickup fires the event — not "
    "just in FPS-rifle projects. Without this, broadening the server pack "
    "to any genre would remove the legacy client-side ``GetItem`` "
    "attribute trigger and leave non-FPS pickups silently unwired.",
    after=("fps_rifle_inject", "pickup_remote_event_server"),
    detect=_detect_pickup_remote_event_in_use,
)
def _add_pickup_remote_listener(scripts: list["RbxScript"]) -> int:
    fixes = 0
    # Install in only ONE controller per project. A converted project
    # may have multiple LocalScripts that define or call ``getItem``
    # (auxiliary UI, tutorial, inventory helpers, etc.); installing the
    # listener in all of them dispatches each pickup event multiple
    # times, double-applying item effects. Pick one canonical target:
    # prefer a script named ``Player`` if available, otherwise the
    # first LocalScript that references ``LocalPlayer`` (the
    # player-controller signature) and has a real ``getItem`` symbol.
    candidate = _select_pickup_listener_target(scripts)
    if candidate is None:
        return 0
    s = candidate
    if 'PickupItemEvent' in s.source:
        return 0
    # Pick whichever casing the script already uses for the dispatch call
    # so we don't introduce an undefined identifier.
    get_item_name = 'getItem' if re.search(r'\bgetItem\b', s.source) else 'GetItem'
    listener = (
        '\n-- Pickup via RemoteEvent (server fires when player touches pickup)\n'
        'local _pickupEvt = game:GetService("ReplicatedStorage"):WaitForChild("PickupItemEvent", 5)\n'
        'if _pickupEvt then\n'
        '    _pickupEvt.OnClientEvent:Connect(function(itemName)\n'
        f'        if itemName and itemName ~= "" then {get_item_name}(itemName) end\n'
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


def _select_pickup_listener_target(
    scripts: list["RbxScript"],
) -> "RbxScript | None":
    """Pick the single LocalScript that should host the
    ``PickupItemEvent`` OnClientEvent listener.

    Selection order — most specific first:
      1. A LocalScript literally named ``Player`` with a bare ``getItem``
         symbol — the canonical Player controller.
      2. The first LocalScript referencing ``LocalPlayer`` AND a bare
         ``getItem`` symbol — a player controller that's named
         differently (e.g. ``PlayerClient``).
      3. None — the project has no obvious player controller. Better to
         drop the listener than install it in a UI/tutorial script
         that happens to define ``getItem`` for unrelated reasons.

    Installing in multiple controllers is the failure mode this avoids:
    each pickup event would dispatch through every listener,
    double-applying item effects.
    """
    eligible = [
        s for s in scripts
        if s.script_type == "LocalScript"
        and _GETITEM_SYMBOL_RE.search(s.source or "")
    ]
    if not eligible:
        return None
    # Tier 1: the canonical Player controller by name.
    for s in eligible:
        if s.name == "Player":
            return s
    # Tier 2: a controller-shaped LocalScript (references LocalPlayer).
    for s in eligible:
        if "LocalPlayer" in (s.source or ""):
            return s
    return None


# ---------------------------------------------------------------------------
# Pack: pickup_visual_target
# ---------------------------------------------------------------------------

def _detect_pickup_visual_target(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Pickup":
            continue
        if (
            "rotationSpeed" in s.source
            and ("moveDown" in s.source or "MoveDown" in s.source)
            and "Touched" in s.source
            and "GetItem" in s.source
        ):
            return True
    return False


@patch_pack(
    name="pickup_visual_target",
    description="Replace Pickup.luau bodies with Model-aware rotate+bob+touch "
    "logic so child Models (Rifle, Battery) animate, not the invisible "
    "trigger Part.",
    after=("pickup_remote_event_server",),
    detect=_detect_pickup_visual_target,
)
def _fix_pickup_visual_target(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name != "Pickup":
            continue
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


# ---------------------------------------------------------------------------
# Pack: door_global_player_to_attribute
# ---------------------------------------------------------------------------
#
# AI transpiles of Unity's ``Door.cs`` sometimes emit a helper of the form
# ``if _G.Player and _G.Player.hasKey then return _G.Player.hasKey() end``
# under the assumption that ``Player`` exposes itself as a global. Two
# things break that assumption in the converted output:
#
#   1. ``Player.luau`` runs as a LocalScript — server scripts can't see
#      its locals or globals.
#   2. ``_G`` is per-actor in Roblox, so even a client read from a server
#      script wouldn't cross the boundary.
#
# Pickup's coherence pack already writes ``player:SetAttribute("has"..itemName,
# true)`` server-side, which DOES replicate. Rewrite the helper to read
# that replicated attribute by deriving the player from the touching part.

# Helper-style probe: ``local function fn() if _G.Player and _G.Player.hasX
# then return _G.Player.hasX[()] end; return false end`` — captures function
# name (group 1) and attribute name (group 2). Two trailing forms covered:
# ``return _G.Player.hasX`` (field) and ``return _G.Player.hasX()`` (call).
_DOOR_GLOBAL_PLAYER_HELPER_RE = re.compile(
    r'local function (\w+)\(\)\s*\n'
    r'\s*if _G\.Player and _G\.Player\.(\w+)\s+then\s*\n'
    r'\s*return _G\.Player\.\2(?:\(\))?\s*\n'
    r'\s*end\s*\n'
    r'\s*return false\s*\n'
    r'\s*end',
)

# Single-line ``return _G.Player and _G.Player.hasX [or false]`` body —
# a different but equally common AI shape. Captures fn name and attribute.
_DOOR_GLOBAL_PLAYER_RETURN_RE = re.compile(
    r'local function (\w+)\(\)\s*\n'
    r'\s*return _G\.Player\s+and\s+_G\.Player\.(\w+)(?:\(\))?'
    r'(?:\s+or\s+false)?\s*\n'
    r'\s*end',
)

# Inline-guard form: ``if _G.Player and _G.Player.hasX then …`` (no helper).
# Captures attribute name only — there's no helper to rename. The body that
# follows the ``then`` is rewritten to derive a player from the touching part.
_DOOR_GLOBAL_PLAYER_INLINE_RE = re.compile(
    r'\bif _G\.Player\s+and\s+_G\.Player\.(\w+)(?:\(\))?\s+then\b'
)


def _detect_door_global_player_lookup(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Door":
            continue
        src = s.source or ""
        # Cheap detector first — narrows the regex work to scripts that
        # contain ``_G.Player`` at all. Catches every shape we rewrite below
        # without running the slower regexes on unrelated Door variants.
        if "_G.Player" in src:
            return True
    return False


# Touched/TouchEnded callback parameter name in Connect(function(NAME)...).
# The converter's api_mappings emits ``otherPart`` for OnTrigger*/OnCollision*
# handlers, but AI transpiles often use ``other``. Capture whatever the
# generated source actually used so call-site rewrites pass the right
# argument; falling back to ``otherPart`` matches the documented convention.
_TOUCH_CALLBACK_RE = re.compile(
    r'(?:Touched|TouchEnded)\s*:\s*Connect\s*\(\s*function\s*\(\s*([a-zA-Z_]\w*)'
)


def _ensure_players_service_binding(source: str) -> str:
    """Insert ``local Players = game:GetService("Players")`` at the top
    of *source* if it's not already bound at file scope. The Door
    rewrite calls ``Players:GetPlayerFromCharacter`` from outer scope
    and would crash on the first Touched if the helper runs before a
    nested binding has executed (or the nested binding is unreachable
    from the outer call).

    Only TOP-LEVEL bindings count — a ``local Players = ...`` inside a
    nested function or callback isn't visible to the rewrite's outer
    helper. Heuristic: match an assignment at column 0 (no leading
    indentation), which corresponds to chunk-level statements in
    well-formatted Lua. AI-transpiled output and the converter's own
    rule-based emit both indent function bodies, so this distinguishes
    file-level from nested bindings reliably enough.
    """
    if re.search(r'^(?:local\s+)?Players\s*=', source, re.MULTILINE):
        return source
    return 'local Players = game:GetService("Players")\n' + source


def _resolve_touch_callback_param(source: str, default: str = "otherPart") -> str:
    """Return the parameter name of the FIRST ``Touched``/``TouchEnded``
    callback in *source*, defaulting to ``otherPart`` (the converter's
    own OnTrigger*/OnCollision* convention from api_mappings.py).

    Use :func:`_resolve_touch_callback_param_at` for per-position
    resolution when the same script has multiple touch handlers with
    different parameter names (e.g. one handler uses ``other``, another
    uses ``otherPart``); rewriting both with one global pick would leave
    the mismatched handler reading an undefined variable and silently
    failing.
    """
    m = _TOUCH_CALLBACK_RE.search(source)
    return m.group(1) if m else default


# Lua block-opening keywords paired with ``end``. Counting these as
# opens against ``end`` as closes correctly tracks nested function/if/
# for/while bodies inside a Touched callback. Without ``if``/``for``/
# ``while`` in the open set, an ordinary ``if cond then ... end`` inside
# a Touched handler decrements the depth and prematurely closes the
# callback's computed range, leaving later code in the same handler
# treated as out-of-scope. Standalone ``do ... end`` blocks are rare in
# converter-emitted code and are intentionally not tracked; if one
# appears inside a Touched handler, the under-count would close the
# range early — accepting that as a known limitation.
_LUA_BLOCK_OPEN_RE = re.compile(r'\b(?:function|if|for|while)\b')
_LUA_END_RE = re.compile(r'\bend\b')


def _touch_callback_ranges(source: str) -> list[tuple[int, int, str]]:
    """Return the lexical ranges of every ``Touched``/``TouchEnded``
    callback in *source* as ``(body_start, body_end, param_name)``.

    ``body_start`` is the position right after the callback's opening
    ``)`` of ``function(VAR)`` — i.e. where the body begins.
    ``body_end`` is the position right before the matching ``end`` of
    that callback. A position is "inside" the callback only when
    ``body_start <= pos < body_end``; positions after ``body_end`` are
    outside scope, so the parameter is no longer accessible there.

    Implementation walks Lua block-opening tokens (``function``, ``if``,
    ``for``, ``while``) and ``end`` tokens with a depth counter to find
    the matching end of each callback. Imperfect against tokens in
    strings/comments but sufficient for converter-emitted code.
    """
    ranges: list[tuple[int, int, str]] = []
    for header in _TOUCH_CALLBACK_RE.finditer(source):
        var = header.group(1)
        # Body starts right after the closing ``)`` of ``function(VAR)``.
        header_close = source.find(')', header.end())
        if header_close == -1:
            continue
        body_start = header_close + 1

        # Walk block-open/end tokens with depth counter. The header's
        # own ``function`` is what put us at depth 1; we look for the
        # matching ``end`` that closes the callback.
        depth = 1
        pos = body_start
        body_end: int | None = None
        while pos < len(source):
            open_m = _LUA_BLOCK_OPEN_RE.search(source, pos)
            end_m = _LUA_END_RE.search(source, pos)
            if end_m is None:
                break
            if open_m is not None and open_m.start() < end_m.start():
                depth += 1
                pos = open_m.end()
                continue
            depth -= 1
            if depth == 0:
                body_end = end_m.start()
                break
            pos = end_m.end()
        if body_end is not None:
            ranges.append((body_start, body_end, var))
    return ranges


def _resolve_touch_callback_param_at(
    source: str, position: int,
    ranges: list[tuple[int, int, str]] | None = None,
) -> str | None:
    """Return the parameter name of the ``Touched``/``TouchEnded``
    callback that LEXICALLY ENCLOSES *position*, or ``None`` if no
    open callback contains it.

    Critical: a callback that has already CLOSED (its matching ``end``
    appeared before *position*) does not enclose later code, even if
    its ``function(VAR)`` header was the most recent. Without this
    scope check, code after a touch block would borrow the now-out-of-
    scope ``other`` and inject an undefined-variable reference at a
    site that runs at module init.

    *ranges* is an optional precomputed list (one per touch callback)
    so callers rewriting many sites in the same source don't pay the
    O(N) walk per site.
    """
    if ranges is None:
        ranges = _touch_callback_ranges(source)
    enclosing: tuple[int, int, str] | None = None
    for start, end, var in ranges:
        if start <= position < end:
            # Innermost wins when nested touch callbacks (rare but
            # legal): pick the range with the latest start.
            if enclosing is None or start > enclosing[0]:
                enclosing = (start, end, var)
    return enclosing[2] if enclosing else None


@patch_pack(
    name="door_global_player_to_attribute",
    description="Rewrite Door scripts that probe gameplay flags via "
    "``_G.Player.hasKey()`` to read ``player:GetAttribute('hasKey')`` "
    "instead. Player runs as a LocalScript so its globals never reach the "
    "server-side Door, but Pickup writes a replicated server attribute "
    "the Door can actually see. Covers three AI-transpile shapes: "
    "``if _G.Player and _G.Player.hasX then return _G.Player.hasX() end`` "
    "helper, ``return _G.Player and _G.Player.hasX`` helper, and inline "
    "``if _G.Player and _G.Player.hasX then …`` guards inside Touched.",
    detect=_detect_door_global_player_lookup,
)
def _fix_door_global_player_lookup(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name != "Door":
            continue
        if "_G.Player" not in (s.source or ""):
            continue

        original = s.source
        captured_helpers: list[tuple[str, str]] = []

        def _helper_replace(m: "re.Match[str]") -> str:
            fn_name = m.group(1)
            attr = m.group(2)
            captured_helpers.append((fn_name, attr))
            return (
                f'local function {fn_name}(_part)\n'
                f'    local _model = _part and _part:FindFirstAncestorOfClass("Model")\n'
                f'    local _player = _model and Players:GetPlayerFromCharacter(_model)\n'
                f'    if _player then return _player:GetAttribute("{attr}") end\n'
                f'    return false\n'
                f'end'
            )

        # Apply both helper shapes. Each appends to ``captured_helpers`` so
        # call-site fixups below pass the touching part to the now-arg-required fn.
        s.source = _DOOR_GLOBAL_PLAYER_HELPER_RE.sub(_helper_replace, s.source)
        s.source = _DOOR_GLOBAL_PLAYER_RETURN_RE.sub(_helper_replace, s.source)

        # Resolve the callback parameter PER call site, not once for the
        # whole file. Doors with multiple handlers can use different
        # names (e.g. ``Touched(function(other)`` and
        # ``TouchEnded(function(otherPart)``) — using a global pick would
        # rewrite the mismatched handler with the wrong variable, so its
        # GetAttribute check always evaluates falsy and that branch never
        # runs. ``_rewrite_in_place_with_callback`` walks each match in
        # source order and resolves the closest preceding ``Touched``/
        # ``TouchEnded`` callback's parameter name for each. Sites with
        # no preceding callback are left unchanged so we don't inject
        # an undefined variable into init-time helper calls (e.g.
        # ``print(getPlayerHasKey())``); the rewritten helper's nil-arg
        # early-return handles those naturally.
        def _rewrite_in_place_with_callback(
            source: str,
            pattern: re.Pattern[str],
            replacer: "Callable[[re.Match[str], str], str]",
        ) -> str:
            # Precompute callback ranges once per source — both helpers
            # below iterate the same source and pay O(N) per call site
            # otherwise. Ranges respect lexical scope: a position past a
            # closed callback's matching ``end`` is outside that callback.
            ranges = _touch_callback_ranges(source)
            pieces: list[str] = []
            cursor = 0
            for m in pattern.finditer(source):
                cb = _resolve_touch_callback_param_at(
                    source, m.start(), ranges=ranges,
                )
                if cb is None:
                    # Outside any touch handler — leave the match alone.
                    continue
                pieces.append(source[cursor:m.start()])
                pieces.append(replacer(m, cb))
                cursor = m.end()
            pieces.append(source[cursor:])
            return ''.join(pieces)

        for fn_name, _attr in captured_helpers:
            call_re = re.compile(rf'\b{re.escape(fn_name)}\(\)')
            s.source = _rewrite_in_place_with_callback(
                s.source,
                call_re,
                lambda _m, cb, fn=fn_name: f'{fn}({cb})',
            )

        # Inline-guard rewrite: ``if _G.Player and _G.Player.hasX then`` →
        # self-invoking lambda that derives the player from the surrounding
        # Touched/TouchEnded callback's parameter. Per-position resolution
        # ensures each guard inside its own handler picks the right name.
        # Inline guards outside any touch handler are skipped — there's no
        # ``other``/``otherPart`` in scope to derive a player from, and
        # rewriting them would inject an undefined variable.
        s.source = _rewrite_in_place_with_callback(
            s.source,
            _DOOR_GLOBAL_PLAYER_INLINE_RE,
            lambda m, cb: (
                f'if (function() '
                f'local _m = {cb} and {cb}:FindFirstAncestorOfClass("Model"); '
                f'local _p = _m and Players:GetPlayerFromCharacter(_m); '
                f'return _p and _p:GetAttribute("{m.group(1)}") '
                f'end)() then'
            ),
        )

        # Ensure Players is bound — both rewrites call
        # ``Players:GetPlayerFromCharacter`` and a Door that previously only
        # used ``_G.Player.hasKey()`` may have skipped the service binding.
        s.source = _ensure_players_service_binding(s.source)

        if s.source != original:
            fixes += 1
            log.info(
                "  Rewrote Door _G.Player gameplay-flag lookups in '%s'", s.name,
            )
    return fixes


# ---------------------------------------------------------------------------
# Pack: fps_default_controls_off
# ---------------------------------------------------------------------------

_FPS_LOCK_CENTER_RE = re.compile(
    r"MouseBehavior\s*=\s*Enum\.MouseBehavior\.LockCenter"
)


def _detect_fps_default_controls(scripts: list["RbxScript"]) -> bool:
    """Pack runs when any LocalScript locks the mouse — the unmistakable
    signature of an FPS controller."""
    return any(
        s.script_type == "LocalScript" and _FPS_LOCK_CENTER_RE.search(s.source)
        for s in scripts
    )


@patch_pack(
    name="fps_default_controls_off",
    description="Disable Roblox's default PlayerModule controls in FPS-style "
    "client scripts. Without this, the auto-loaded "
    "StarterPlayerScripts/PlayerModule resets MouseBehavior back to Default "
    "every frame, so the FPS lock never sticks. Also hides the local "
    "character body and snaps the spawn to the floor below — both standard "
    "FPS expectations Roblox doesn't provide by default.",
    detect=_detect_fps_default_controls,
)
def _disable_default_controls_in_fps_scripts(scripts: list["RbxScript"]) -> int:
    fixes = 0
    marker = "-- u2r: disable default PlayerModule controls"
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
        "        char.DescendantAdded:Connect(_hidePart)\n"
        "        for _, part in char:GetDescendants() do _hidePart(part) end\n"
        "    end\n"
        "    local function _snapToFloor(char)\n"
        "        if not char then return end\n"
        "        local hrp = char:WaitForChild(\"HumanoidRootPart\", 5)\n"
        "        if not hrp then return end\n"
        "        task.wait()\n"
        "        local rp = RaycastParams.new()\n"
        "        rp.FilterDescendantsInstances = {char}\n"
        "        rp.FilterType = Enum.RaycastFilterType.Exclude\n"
        "        local hit = workspace:Raycast(hrp.Position, Vector3.new(0, -200, 0), rp)\n"
        "        if not hit then return end\n"
        "        local target = hit.Position + Vector3.new(0, 3, 0)\n"
        "        if (hrp.Position - target).Magnitude > 2 then\n"
        "            hrp.CFrame = hrp.CFrame + (target - hrp.Position)\n"
        "        end\n"
        "    end\n"
        "    _applyFpsMouseState()\n"
        "    _hideCharacter(_lp.Character)\n"
        "    _snapToFloor(_lp.Character)\n"
        "    _lp.CharacterAdded:Connect(function(char)\n"
        "        task.wait()\n"
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
        if not _FPS_LOCK_CENTER_RE.search(s.source):
            continue
        s.source = setup + s.source
        fixes += 1
        log.info("  Disabled default PlayerModule controls in '%s'", s.name)
    return fixes


# ---------------------------------------------------------------------------
# Pack: trigger_stay_polling
# ---------------------------------------------------------------------------

def _detect_trigger_stay_polling(scripts: list["RbxScript"]) -> bool:
    """Pack runs when scripts use the converter-emitted turret AI helpers
    (``getTBase`` + ``sightRadius``) inside an ``OnTriggerStay``-translated
    handler. Avoids polluting projects that don't have turret-style AI."""
    for s in scripts:
        if "triggerCollider" not in s.source:
            continue
        if "angle <" not in s.source or "startEngaged" not in s.source:
            continue
        if "getTBase" in s.source and (
            "getSightRadius" in s.source or "sightRadius" in s.source
        ):
            return True
    return False


@patch_pack(
    name="trigger_stay_polling",
    description="Approximate Unity OnTriggerStay with a Heartbeat poll on "
    "turret-AI scripts whose Touched:Connect handler only fires on initial "
    "contact. Without this, rotating turrets never engage targets whose "
    "first-contact angle was outside the engagement cone.",
    detect=_detect_trigger_stay_polling,
)
def _add_trigger_stay_polling(scripts: list["RbxScript"]) -> int:
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


# ---------------------------------------------------------------------------
# Pack: producer_consumer_bindable_events
# ---------------------------------------------------------------------------
#
# Generic event-bridge: when an AI-transpiled script declares an anonymous
# ``local <var> = Instance.new("BindableEvent")`` and uses ``<var>:Fire(…)``
# locally, AND a sibling script looks up an event in ReplicatedStorage by
# name (either ``FindFirstChild/WaitForChild("Foo")`` directly or a
# ``resolveEvent("Foo")``-style helper that takes a string), the two sides
# are talking past each other. The producer publishes nowhere, the consumer
# subscribes to nothing.
#
# Common origin: Unity static C# events (``Player.HealthUpdate +=``) split
# across files at transpile time — the producer side becomes anonymous
# BindableEvents in one script, the consumer side becomes name-based lookups
# in another. SimpleFPS Player/HudControl is one example, but the same
# split shows up in any project that used static event handlers.
#
# Algorithm: in each script, find ``Instance.new("BindableEvent")``
# declarations followed by ``:Fire(…)`` calls on the same variable; in
# every script, find every literal RemoteEvent/BindableEvent name looked
# up in ReplicatedStorage. Match by stem (``healthUpdateEvent`` →
# ``HealthUpdate`` after camelCase→PascalCase plus ``Event`` suffix
# stripping). When a producer's stem matches a consumer's lookup name,
# rewrite the producer declaration to publish under that consumer name.

_BINDABLE_DECL_RE = re.compile(
    r'^(\s*local\s+(\w+)\s*=\s*Instance\.new\(\s*["\']BindableEvent["\']\s*\)\s*)$',
    re.MULTILINE,
)
_RS_EVENT_LOOKUP_RE = re.compile(
    r'(?:FindFirstChild|WaitForChild|resolveEvent)\s*\(\s*"([A-Z]\w+)"',
)


def _producer_stem(var: str) -> str:
    """Map a producer var name to the PascalCase stem the consumer side
    is most likely looking up. ``healthUpdateEvent`` -> ``HealthUpdate``,
    ``pauseEvent`` -> ``Pause`` (so it matches a ``Pause``/``PauseEvent``
    consumer lookup; we try both forms at match time)."""
    if not var:
        return ""
    stem = var
    if stem.endswith("Event"):
        stem = stem[: -len("Event")]
    if not stem:
        stem = var
    return stem[:1].upper() + stem[1:]


def _detect_producer_consumer_events(scripts: list["RbxScript"]) -> bool:
    has_producer = False
    has_consumer = False
    for s in scripts:
        src = s.source or ""
        if 'Instance.new("BindableEvent")' in src and ":Fire(" in src:
            has_producer = True
        if (
            "ReplicatedStorage" in src
            and ("FindFirstChild" in src or "WaitForChild" in src)
            and _RS_EVENT_LOOKUP_RE.search(src)
        ):
            has_consumer = True
        if has_producer and has_consumer:
            return True
    return False


@patch_pack(
    name="producer_consumer_bindable_events",
    description="Publish anonymous BindableEvents (Producer side) to "
    "ReplicatedStorage under the names ReplicatedStorage:FindFirstChild "
    "consumers expect. Bridges Unity static events split across "
    "AI-transpiled scripts without producer/consumer name agreement.",
    detect=_detect_producer_consumer_events,
)
def _publish_producer_consumer_events(scripts: list["RbxScript"]) -> int:
    # Step 1: collect every event name the consumer side looks up in
    # ReplicatedStorage. Capitalized first letter to filter out variable
    # references like ``FindFirstChild(name)``.
    consumer_names: set[str] = set()
    for s in scripts:
        for m in _RS_EVENT_LOOKUP_RE.finditer(s.source or ""):
            consumer_names.add(m.group(1))

    if not consumer_names:
        return 0

    # Step 2: in each script, find producers (anonymous BindableEvent + Fire)
    # whose stems match a consumer lookup. Skip producers already named.
    fixes = 0
    for s in scripts:
        if 'Instance.new("BindableEvent")' not in s.source:
            continue
        original = s.source
        published: dict[str, str] = {}  # var -> consumer name

        for m in _BINDABLE_DECL_RE.finditer(s.source):
            var = m.group(2)
            # Require the producer to actually fire the event, otherwise
            # we'd hijack an unrelated BindableEvent (e.g. a private
            # debug helper).
            if not re.search(rf'\b{re.escape(var)}\s*:\s*Fire\b', s.source):
                continue
            stem = _producer_stem(var)
            # Try the stripped stem (``HealthUpdate``) and the literal stem
            # without ``Event`` suffix removal (``Pause`` for ``pauseEvent``)
            # as well as ``<stem>Event`` (``PauseEvent``).
            candidates = {stem, stem + "Event"}
            for cand in candidates:
                if cand in consumer_names:
                    published[var] = cand
                    break

        if not published:
            continue

        def _replace(m: "re.Match[str]") -> str:
            decl, var = m.group(1), m.group(2)
            consumer = published.get(var)
            if not consumer:
                return decl
            return (
                f'{decl}\n'
                f'{var}.Name = "{consumer}"\n'
                f'do local _existing = game:GetService("ReplicatedStorage")'
                f':FindFirstChild("{consumer}"); '
                f'if _existing and _existing:IsA("BindableEvent") then '
                f'{var}:Destroy(); {var} = _existing else '
                f'{var}.Parent = game:GetService("ReplicatedStorage") end end'
            )

        s.source = _BINDABLE_DECL_RE.sub(_replace, s.source)
        if s.source != original:
            fixes += 1
            log.info(
                "  Published %d BindableEvent(s) to ReplicatedStorage in "
                "'%s': %s",
                len(published), s.name,
                ", ".join(f"{v}->{n}" for v, n in sorted(published.items())),
            )
    return fixes

