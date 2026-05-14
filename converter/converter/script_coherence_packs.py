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
# Pack: template_guard_self_destroying
# ---------------------------------------------------------------------------
#
# Any Script attached to a prefab template under ``ReplicatedStorage.Templates``
# with ``RunContext = Server`` runs at place load on the template itself —
# regardless of whether the prefab is ever cloned. If that script destroys
# its container (``container:Destroy()`` or ``Debris:AddItem(container,
# fadeTime)``), it nukes the template, and downstream ``:Clone()`` calls
# return Parts with no children → broken cloned instances OR
# ``Templates:WaitForChild("...")`` hangs forever.
#
# PR #79 fixed this for bullet templates (TurretBullet, PlaneBullet) inside
# ``bullet_physics_raycast``. But the same class of bug applies to ANY
# self-destroying script that lands in a template (e.g.
# ``ParticleSystemDestroyer`` on ``Templates.Explosion`` / ``Templates.Smoke``,
# which destroys the templates ~6-10s after play start, blocking
# ``Turret.luau`` from cloning Explosion when it dies). Generalize the
# template-guard to ANY script with a self-destroying pattern.

_SELF_DESTROY_RE = re.compile(
    r'container\s*:\s*Destroy\s*\(\s*\)'
    r'|Debris\s*:\s*AddItem\s*\(\s*container\s*,'
)

_TEMPLATE_GUARD_MARKER = "_AutoTemplateGuard"

_TEMPLATE_GUARD_BLOCK = '''\
-- ''' + _TEMPLATE_GUARD_MARKER + ''': bail when this script is running on a
-- prefab template under ``ReplicatedStorage.Templates``. Server-context
-- scripts run on the template itself at place load, so any self-destroy
-- (container:Destroy / Debris:AddItem(container, …)) would nuke the
-- template and break downstream :Clone() / WaitForChild calls.
do
\tlocal _p = script.Parent
\twhile _p do
\t\tif _p.Name == "Templates" and _p.Parent
\t\t\tand _p.Parent:IsA("ReplicatedStorage")
\t\tthen return end
\t\t_p = _p.Parent
\tend
end

'''


def _detect_self_destroying_scripts(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        src = s.source or ""
        if (
            _SELF_DESTROY_RE.search(src)
            and _TEMPLATE_GUARD_MARKER not in src
        ):
            return True
    return False


@patch_pack(
    name="template_guard_self_destroying",
    description="Prefix any Server-context Script that destroys its "
    "``container`` (via ``container:Destroy()`` or "
    "``Debris:AddItem(container, …)``) with a template-guard that "
    "bails when the script's ancestor chain hits "
    "``ReplicatedStorage.Templates``. Without this, every prefab "
    "template script self-destroys at place load — blocking downstream "
    "``:Clone()`` / ``WaitForChild`` callers.",
    detect=_detect_self_destroying_scripts,
)
def _inject_template_guard(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        src = s.source or ""
        if (
            _SELF_DESTROY_RE.search(src)
            and _TEMPLATE_GUARD_MARKER not in src
        ):
            s.source = _TEMPLATE_GUARD_BLOCK + src
            fixes += 1
            log.info(
                "  Prefixed template-guard in self-destroying '%s'", s.name,
            )
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

    Fires on any ``Pickup`` script that either writes
    ``character:SetAttribute("GetItem"|"PickupItem", itemName)`` (the
    legacy AI-transpile shape the pack rewrites) OR fires
    ``PickupItemEvent`` directly without the server-side
    ``has<X>`` attribute write. The latter case applies to
    pre-rewritten pickups (e.g. the ``_PICKUP_REPLACEMENT`` body) that
    skip the legacy SetAttribute step entirely; without this gate they
    miss the ``_pl:SetAttribute("has"..itemName, true)`` injection
    that ``door_global_player_to_attribute`` depends on.

    Detector is intentionally name-agnostic — the previous gate
    ``_detect_fps_rifle_pickup`` bound this to FPS-rifle projects only,
    but pickup patterns appear in any genre.
    """
    for s in scripts:
        if s.name != "Pickup":
            continue
        src = s.source or ""
        if _PICKUP_SETATTRIBUTE_RE.search(src):
            return True
        # Direct-RemoteEvent shape: fires PickupItemEvent but doesn't
        # write the server-side has-attribute. The apply function
        # injects the missing write. Match the EXACT dynamic-concat
        # pattern the pack injects (``SetAttribute("has" .. itemName,
        # true)``) — checking for any ``SetAttribute("has"...)`` would
        # false-skip Pickups that init unrelated has-flags (e.g. an
        # opening-state ``SetAttribute("hasKey", false)``) but never
        # write the dynamic-concat shape that mirrors what the
        # pack would inject.
        if (
            "PickupItemEvent" in src
            and "FireClient" in src
            and not _PICKUP_HAS_ATTR_INJECTED_RE.search(src)
        ):
            return True
    return False


# The exact server-attr write the pack injects. Matching this directly
# (rather than ``SetAttribute("has"...)``) avoids false-skipping
# Pickups that init unrelated has-flags before firing.
_PICKUP_HAS_ATTR_INJECTED_RE = re.compile(
    r':\s*SetAttribute\s*\(\s*"has"\s*\.\.\s*itemName\s*,\s*true\s*\)'
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

        # AI non-determinism: on some runs the transpiler emits the raw
        # default ``local itemName = ""`` instead of
        # ``script:GetAttribute("itemName")``. The above ``attr_pattern``
        # only catches the latter shape, so the raw-default Pickup ends
        # up firing the RemoteEvent with an empty itemName. Catch the
        # raw-default case for the known Pickup serialized fields by
        # name and rewrite to the same walk-up search the GetAttribute
        # branch produces. Field allow-list is conservative — only
        # known Unity Pickup.cs serialized fields are touched.
        for field_name in ("itemName",):
            raw_default_re = re.compile(
                r'(local\s+' + field_name + r'\s*=\s*)"[^"]*"\s*$',
                re.MULTILINE,
            )
            walk_up = (
                r'\1(function() local _c=script.Parent; '
                r'while _c do local v=_c:GetAttribute("' + field_name + r'"); '
                r'if v ~= nil then return v end; _c=_c.Parent end; '
                r'return "" end)()'
            )
            new_src3, count3 = raw_default_re.subn(walk_up, s.source)
            if count3:
                s.source = new_src3
                fixes += count3
                log.info(
                    "  Promoted %d Pickup raw-default %r to walk-up search",
                    count3, field_name,
                )

        # Direct-RemoteEvent path: Pickups that already use FireClient
        # (``_PICKUP_REPLACEMENT`` body, hand-written shapes) skip the
        # legacy SetAttribute -> FireClient rewrite above. Inject the
        # server-side ``has<X>`` attribute write before FireClient if
        # missing — door_global_player_to_attribute relies on it.
        if (
            "PickupItemEvent" in s.source
            and "FireClient" in s.source
            and not _PICKUP_HAS_ATTR_INJECTED_RE.search(s.source)
        ):
            injected = _inject_has_attribute_before_fireclient(s)
            if injected:
                fixes += injected
                log.info(
                    "  Injected server-side has<X> SetAttribute "
                    "before FireClient in '%s'",
                    s.name,
                )
    return fixes


def _inject_has_attribute_before_fireclient(s: "RbxScript") -> int:
    """Insert ``_pl:SetAttribute("has"..itemName, true)`` before each
    ``<event>:FireClient(<player>, itemName)`` call in *s.source* that
    doesn't already have one.

    Recovers from the gap codex flagged: a Pickup whose AI transpile
    (or canonical ``_PICKUP_REPLACEMENT``) writes via FireClient
    directly skips the SetAttribute → FireClient rewrite above, so
    the server-side flag never replicates to the Door.

    Captures the ``_pl``-equivalent player variable from FireClient's
    first argument so the injected SetAttribute targets the same
    player object the event fires for.
    """
    fire_re = re.compile(
        r'([a-zA-Z_][\w.]*)\s*:\s*FireClient\s*\(\s*([a-zA-Z_]\w*)\s*,\s*itemName\s*\)'
    )
    fixes = 0

    def _inject(m: "re.Match[str]") -> str:
        player_var = m.group(2)
        # Indent the injection to match the line containing FireClient.
        # Walk back to find the line's leading whitespace.
        return (
            f'if {player_var} and itemName and itemName ~= "" then '
            f'{player_var}:SetAttribute("has" .. itemName, true) end\n\t\t\t'
            f'{m.group(0)}'
        )

    new_src, count = fire_re.subn(_inject, s.source)
    if count:
        s.source = new_src
        fixes += count
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
      2. The LocalScript with the highest player-controller signal
         score: ``+1`` for each of these signals seen in source —
         ``LocalPlayer``, ``Character``, ``Humanoid``,
         ``UserInputService``, plus a strong ``+3`` boost for an actual
         ``function getItem(...)`` / ``function GetItem(...)``
         definition (not just a call). UI/tutorial scripts that merely
         CALL ``getItem`` outside their own definition score lower than
         the script that defines it, so the listener installs in the
         actual controller even when ``LocalPlayer`` happens to appear
         elsewhere first.
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

    # Tier 2: score each candidate; pick the highest-scoring controller.
    def _score(s: "RbxScript") -> int:
        src = s.source or ""
        score = 0
        for signal in ("LocalPlayer", "Character", "Humanoid",
                       "UserInputService"):
            if signal in src:
                score += 1
        # Heavy weight on ACTUAL definition of getItem — controllers
        # define it; UI scripts that only call ``inventory.getItem``
        # don't (and are already filtered out by the qualified-call
        # rejection in the symbol regex; this guards the rare case
        # where a UI script also has a bare ``getItem(...)`` call).
        if re.search(r'\bfunction\s+(?:get|Get)Item\s*\(', src):
            score += 3
        return score

    scored = sorted(eligible, key=_score, reverse=True)
    if not scored:
        return None
    best = scored[0]
    if _score(best) == 0:
        # No controller signals at all — skip rather than install in
        # a script we can't identify as a player controller.
        return None
    return best


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
# treated as out-of-scope.
#
# ``do`` is special-cased: as a STANDALONE block (``do BODY end``) it
# opens and pairs with ``end``, but inside ``for ... do ... end`` and
# ``while ... do ... end`` the ``do`` is part of the loop construct and
# is consumed by the matching ``for``/``while`` open. The scanner below
# tracks a "loop expects do" flag so the post-loop ``do`` is not double-
# counted.
_LUA_BLOCK_OPEN_RE = re.compile(r'\b(?:function|if|for|while|do)\b')
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
        #
        # ``loop_pending_do`` tracks ``for``/``while`` opens that
        # haven't yet seen their ``do``: when the next ``do`` arrives,
        # it's consumed by the loop and NOT counted as a separate
        # block open. Standalone ``do ... end`` (no preceding loop)
        # opens normally.
        depth = 1
        pos = body_start
        loop_pending_do = 0
        body_end: int | None = None
        while pos < len(source):
            open_m = _LUA_BLOCK_OPEN_RE.search(source, pos)
            end_m = _LUA_END_RE.search(source, pos)
            if end_m is None:
                break
            if open_m is not None and open_m.start() < end_m.start():
                kw = open_m.group()
                if kw == "do" and loop_pending_do > 0:
                    # Consumed by a preceding ``for``/``while``.
                    loop_pending_do -= 1
                else:
                    depth += 1
                    if kw in ("for", "while"):
                        loop_pending_do += 1
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


# NOTE: A previous ``turret_spawn_from_weapon_cframe`` pack rewrote
# ``spawnAt(..., getCFrame(tOrigin))`` to source from ``tWeapon`` instead.
# That fix is now obsolete — replaced by a generic converter-level wrap
# in ``scene_converter._wrap_geometry_with_children_into_model``: when a
# Unity node has both visual geometry AND child transforms, the converter
# emits a ``Model`` with the geometry as an inner child, so
# ``Model:PivotTo`` propagates rotation to all descendants (including
# ``tOrigin``). The Turret script's ``getCFrame(tOrigin)`` then returns
# the correctly-aimed CFrame without a per-game patch.


# ---------------------------------------------------------------------------
# Pack: door_module_player_to_attribute
# ---------------------------------------------------------------------------
#
# A second AI-transpile shape for Unity's ``other.GetComponent<Player>().hasKey``
# probe: instead of ``_G.Player.hasKey()`` (handled by
# ``door_global_player_to_attribute`` above), Claude sometimes emits a
# ``getPlayerMod()`` / ``require(PlayerModule)`` pattern that calls
# ``mod.hasKey()`` on the resolved module. Same coupling failure as the
# ``_G`` shape:
#
#   1. ``Player.luau`` runs as a LocalScript whose ``gotKey`` flag is bound
#      to the client's instance. A server-side ``require(Player)`` returns
#      a separate server-side instance whose ``gotKey`` is always false.
#   2. The fallback ``PlayerScripts.Player:GetAttribute("hasKey")`` reads
#      an attribute on the wrong object — ``Pickup`` writes ``hasX`` on
#      the ``Player`` instance (replicates), not on ``PlayerScripts.Player``.
#
# Rewrite ``local function playerHasX(playerInstance)`` to read directly
# from the Player-instance attribute that the pickup pack writes.

_DOOR_MODULE_PLAYER_HELPER_RE = re.compile(
    r"local function (player[Hh]as\w+)\s*\(\s*(\w+)\s*\)"
    r"(?P<body>.*?)"
    r"^end$",
    re.DOTALL | re.MULTILINE,
)


def _detect_door_module_player_lookup(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Door":
            continue
        src = s.source or ""
        # Cheap detector: the playerHas* helper plus a PlayerScripts
        # lookup OR a getPlayerMod call. Both are unique to this AI shape.
        if "playerHas" in src and ("getPlayerMod" in src or 'PlayerScripts' in src):
            return True
    return False


@patch_pack(
    name="door_module_player_to_attribute",
    description="Rewrite ``playerHasX(playerInstance)`` helpers in Door "
    "scripts whose AI body required the Player ModuleScript or read "
    "``PlayerScripts.Player:GetAttribute`` to instead read "
    "``playerInstance:GetAttribute('hasX')`` — the server-replicated "
    "attribute the Pickup coherence pack writes. Without this, the Door "
    "fires Touched but its key check returns false, so the door never "
    "opens even after the player picks up the key.",
    detect=_detect_door_module_player_lookup,
    after=("pickup_remote_event_server",),
)
def _fix_door_module_player_lookup(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name != "Door":
            continue
        original = s.source

        def _replace(m: "re.Match[str]") -> str:
            fn_name = m.group(1)
            arg_name = m.group(2)
            body = m.group("body")
            # Skip helpers that don't reference the module/PlayerScripts
            # path — they're already correct.
            if "getPlayerMod" not in body and "PlayerScripts" not in body:
                return m.group(0)
            # Derive attribute name from the function name:
            # ``playerHasKey`` → ``hasKey``. Falls back to ``hasKey`` if
            # the pattern doesn't yield a clean suffix.
            suffix = fn_name[len("playerHas"):]
            attr = ("has" + suffix) if suffix else "hasKey"
            return (
                f"local function {fn_name}({arg_name})\n"
                f"    -- Pickup pack writes ``Player:SetAttribute({attr!r}, true)``\n"
                f"    -- server-side on the Player instance — replicates to all\n"
                f"    -- scripts. Door runs server-side, so this is the only\n"
                f"    -- check that reflects state set by the client's pickup.\n"
                f"    return {arg_name} ~= nil and {arg_name}:GetAttribute({attr!r}) == true\n"
                f"end"
            )

        s.source = _DOOR_MODULE_PLAYER_HELPER_RE.sub(_replace, s.source)
        if s.source != original:
            fixes += 1
            log.info(
                "  Rewrote Door module-Player gameplay-flag lookups in '%s'", s.name,
            )
    return fixes


# ---------------------------------------------------------------------------
# Pack: door_direct_character_attribute
# ---------------------------------------------------------------------------
#
# Third AI-transpile shape for Unity's ``other.GetComponent<Player>().hasKey``
# probe (after ``_G.Player.hasKey`` and the ``getPlayerMod()`` helper).
# The AI sometimes emits a direct character-attribute read:
#
#   if char:GetAttribute("HasKey") then ... end
#
# Same coupling failure as the other two shapes:
#   1. Pickup's coherence pack writes ``player:SetAttribute("has"..itemName, true)``
#      on the Players[] instance (replicates server-side). It does NOT
#      write to the character Model.
#   2. ``HasKey`` (capital H) doesn't match what the Pickup pack writes
#      (``hasKey``, lowercase). Even if the read target were right,
#      the attribute name would mismatch.
#
# Rewrite to derive the Player instance from the character and read
# the (lowercase) ``has<Suffix>`` attribute Pickup actually writes.

_DOOR_DIRECT_ATTR_RE = re.compile(
    r'([a-zA-Z_]\w*)\s*:\s*GetAttribute\(\s*"[Hh]as(\w+)"\s*\)'
)


def _detect_door_direct_character_attribute(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Door":
            continue
        src = s.source or ""
        if _DOOR_DIRECT_ATTR_RE.search(src):
            return True
    return False


@patch_pack(
    name="door_direct_character_attribute",
    description="Rewrite ``<char>:GetAttribute('HasX')`` direct reads in "
    "Door scripts to derive the Player instance from the character and "
    "read the lowercase ``hasX`` attribute the Pickup coherence pack "
    "writes server-side. Door runs server-side; character attributes "
    "set by client LocalScripts don't replicate, so the read returns "
    "nil and the door never opens.",
    detect=_detect_door_direct_character_attribute,
    after=("pickup_remote_event_server",),
)
def _fix_door_direct_character_attribute(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name != "Door":
            continue
        original = s.source

        def _replace(m: "re.Match[str]") -> str:
            char_var = m.group(1)
            suffix = m.group(2)
            attr = "has" + suffix
            # Inline IIFE so the rewrite fits any expression position
            # (condition, return, assignment). Players service is
            # already imported at the top of Door.luau by api_mappings.
            return (
                f'(function() local _p = '
                f'game:GetService("Players"):GetPlayerFromCharacter({char_var}); '
                f'return _p and _p:GetAttribute("{attr}") == true end)()'
            )

        s.source = _DOOR_DIRECT_ATTR_RE.sub(_replace, s.source)
        if s.source != original:
            fixes += 1
            log.info(
                "  Rewrote Door direct char-attribute reads in '%s'", s.name,
            )
    return fixes


# ---------------------------------------------------------------------------
# Pack: door_strip_ai_rotation
# ---------------------------------------------------------------------------
#
# Unity's Door.cs::ToggleDoor only calls ``doorAnim.SetBool("open", value)``
# — the Animator clip (``open.anim``) does the actual movement, which is
# a +4m Y translation. The animation phase emits ``Anim_<Prefab>_door_open``
# in ServerScriptService that listens to the ``open`` attribute and tweens
# +14.28 studs Y.
#
# But the AI transpiler often *invents* its own door motion in Door.luau:
# a ``tweenDoor`` helper that rotates the door 90° around Y via
# ``CFrame.Angles``, captured against a ``doorBaseCF`` baseline. That
# rotation fights the translation tween from ``Anim_*_door_open`` —
# two TweenService tweens on the same ``door.CFrame`` overwrite each
# other every frame, and the user sees the door spin in place / jitter
# instead of sliding up.
#
# Strip the AI-invented rotation so the only motion comes from the
# animation driver (or, when none exists, from the ``door_tween_open``
# pack's injected fallback). The visible ``door:SetAttribute("open",
# value)`` write is preserved — both the animation driver and the
# fallback rely on it firing.

_DOOR_BASE_CF_DECL_RE = re.compile(
    r"^local doorBaseCF\s*=\s*nil\s*\n", re.MULTILINE,
)
_DOOR_CAPTURE_FN_RE = re.compile(
    r"^local function captureDoorBase\s*\([^)]*\)\s*\n[\s\S]*?^end\s*\n",
    re.MULTILINE,
)
_DOOR_CAPTURE_CALL_RE = re.compile(
    r"^captureDoorBase\(\)\s*\n", re.MULTILINE,
)
_DOOR_TWEEN_FN_RE = re.compile(
    r"^local function tweenDoor\s*\([^)]*\)\s*\n[\s\S]*?^end\s*\n",
    re.MULTILINE,
)
_DOOR_TWEEN_CALL_RE = re.compile(
    r"^[ \t]*tweenDoor\([^)]*\)\s*\n", re.MULTILINE,
)
_DOOR_ROTATION_SIGNATURE_RE = re.compile(
    r"doorBaseCF\s*\*\s*CFrame\.Angles"
)


def _detect_door_ai_rotation(scripts: list["RbxScript"]) -> bool:
    """Pack runs on any Door script that contains the
    ``doorBaseCF * CFrame.Angles(...)`` rotation idiom — the unmistakable
    signature of the AI-invented door swing. Avoids matching the
    project-level ``door_tween_open`` injection (which uses Vector3
    translation, not CFrame.Angles)."""
    for s in scripts:
        if s.name != "Door":
            continue
        if _DOOR_ROTATION_SIGNATURE_RE.search(s.source or ""):
            return True
    return False


@patch_pack(
    name="door_strip_ai_rotation",
    description="Strip the AI-invented ``tweenDoor`` / ``doorBaseCF`` "
    "rotation idiom from Door scripts. Unity Door.cs only flips an "
    "Animator parameter; the actual door motion comes from the "
    "Animator clip, which the converter translates into a "
    "translation tween (Anim_*_door_open). The AI's extra 90° "
    "rotation tween on the same MeshPart fights the translation, so "
    "the door visibly jitters / does not slide open. Strip is "
    "narrow: only removes the rotation helpers and call site; the "
    "``door:SetAttribute(\"open\", value)`` write is preserved.",
    detect=_detect_door_ai_rotation,
    after=("door_direct_character_attribute",),
)
def _strip_door_ai_rotation(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name != "Door":
            continue
        if not _DOOR_ROTATION_SIGNATURE_RE.search(s.source or ""):
            continue
        original = s.source
        src = original
        src = _DOOR_TWEEN_CALL_RE.sub("", src)
        src = _DOOR_TWEEN_FN_RE.sub("", src)
        src = _DOOR_CAPTURE_CALL_RE.sub("", src)
        src = _DOOR_CAPTURE_FN_RE.sub("", src)
        src = _DOOR_BASE_CF_DECL_RE.sub("", src)
        if src != original:
            s.source = src
            fixes += 1
            log.info(
                "  Stripped AI-invented rotation tween from Door '%s'",
                s.name,
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



# ---------------------------------------------------------------------------
# Pack: door_tween_open
# ---------------------------------------------------------------------------
#
# Unity's SciFi_Door uses a Mecanim Animator with ``open.anim`` (slides Y
# +4m over 1s) controlled by ``Animator.SetBool("open", value)``. The
# converter's animation phase doesn't translate Mecanim controllers to
# TweenService, so the AI-transpiled ``Door.luau`` ends up calling
# ``doorAnim:SetAttribute("open", value)`` on the sibling ``door`` mesh
# but nothing actually moves the part. From the user's POV: the open
# sound plays, the attribute flips, but the door mesh sits still and
# blocks the doorway.
#
# Inject a one-shot listener at the end of any Door.luau that connects
# to ``GetAttributeChangedSignal("open")`` and tweens the mesh +14.28
# studs Y (Unity's 4m × STUDS_PER_METER) on open, back on close.
#
# Detector: matches ``SetAttribute("open"`` AND mentions a sibling lookup
# pattern (``parent:FindFirstChild("door")`` is the canonical form). This
# avoids touching unrelated scripts that happen to set an "open" attribute.

_DOOR_OPEN_SETATTR_RE = re.compile(r':SetAttribute\(\s*["\']open["\']\s*,')
_DOOR_SIBLING_LOOKUP_RE = re.compile(
    r':FindFirstChild\(\s*["\']door["\']\s*\)'
)
# Animation phase may have already emitted door-tween driver scripts.
# Names emitted by ``animation_converter`` follow two shapes:
#
#   - Prefab-scoped: ``Anim_<Prefab>_<MeshName>_(open|close)`` —
#     e.g. ``Anim_Door_door_open`` for a Door prefab whose target
#     mesh is named ``door``.
#   - Scene-scoped:  ``Anim_<MeshName>_(open|close)`` —
#     e.g. ``Anim_door_open`` for a scene-baked door instance.
#
# Both regexes anchor on the canonical ``door`` mesh name + open/close
# clip names. Codex round-7 [P1]: the scene-scoped form was missing
# from the prior regex, so scene-baked doors got a second listener
# and double-animated.
# Animation drivers names come from ``animation_converter``, which
# composes them from prefab + controller + clip display names. Those
# display names can carry spaces, dashes, mixed case ("SciFi Door",
# "Open" vs "open"). Match case-insensitively and allow any non-_
# characters in the prefab/controller slot. The trailing
# ``(open|close)`` clip name is matched case-insensitively too.
_DOOR_EXISTING_ANIM_PATTERNS = (
    re.compile(r'^Anim_.+_door_(open|close)$', re.IGNORECASE),
    re.compile(r'^Anim_door_(open|close)$', re.IGNORECASE),
)


def _door_has_animation_driver(scripts: list["RbxScript"]) -> bool:
    """Return True if ANY door-tween animation driver exists in
    ``scripts``. Matches both prefab-scoped
    (``Anim_<Prefab>_door_*``) and scene-scoped (``Anim_door_*``)
    name shapes that ``animation_converter`` emits.
    """
    return any(
        any(p.match(s.name) for p in _DOOR_EXISTING_ANIM_PATTERNS)
        for s in scripts
    )


def _detect_door_tween_target(scripts: list["RbxScript"]) -> bool:
    """Detect whether at least one ``Door`` script in ``scripts``
    needs the tween fallback.

    Coverage policy: the injected tween body carries a RUNTIME
    coexistence guard (scans the door's parent prefab + RS for an
    ``Anim_(<Prefab>_)?door_*`` script). So pack-level we only need
    to identify Door scripts that haven't been marked yet — the
    runtime guard makes the body a no-op when an animation driver
    is actually present on the same door instance.

    Codex round-10 [P2]: ``animation_converter`` emits drivers per
    controller/scope, NOT as a project-wide all-or-nothing pass.
    Detecting at pack-level on ANY driver presence would skip the
    fallback for uncovered doors in mixed projects. Push the
    coexistence check to runtime where it's per-instance.
    """
    for s in scripts:
        if s.name != "Door":
            continue
        if (
            _DOOR_OPEN_SETATTR_RE.search(s.source)
            and _DOOR_SIBLING_LOOKUP_RE.search(s.source)
            and "_AutoFpsDoorTweenInjected" not in s.source
        ):
            return True
    return False


# Tween block appended at end of Door.luau. Self-contained — uses local
# helpers; doesn't depend on names from the surrounding script. Reads the
# sibling lookup itself instead of calling the script's ``getDoorAnim``
# (whose name the AI may rename across runs).
_DOOR_TWEEN_BLOCK = """
-- _AutoFpsDoorTweenInjected: door coherence pack
-- Listens to ``open`` attribute change on the sibling ``door`` mesh and
-- tweens it +14.28 studs Y (Unity 4m × STUDS_PER_METER) on open, back
-- on close. Mecanim Animator → TweenService bridge.
--
-- Per-door coexistence guard: skip if THIS door already has an
-- ``Anim_*door_*`` driver script wired to its parent (e.g. as a
-- WaitForChild target in ReplicatedStorage or as a script child of
-- the same prefab Model). Otherwise the project-level pack and the
-- per-door animation driver would both tween the same mesh on every
-- attribute change → overshoot/jitter (codex round-10 [P2]).
do
    local TweenService = game:GetService("TweenService")
    local _doorContainer = script.Parent
    local _parent = _doorContainer and _doorContainer.Parent
    local _doorMesh = _parent and _parent:FindFirstChild("door")
    if _doorMesh and _doorMesh:IsA("BasePart") then
        -- Scan the door's parent prefab (and ReplicatedStorage if the
        -- driver lives there) for an existing animation driver
        -- whose name matches ``Anim_(<Prefab>_)?door_(open|close)``
        -- (case-insensitive). When found, defer to it.
        local function _hasAnimDriver()
            local function _matchName(name)
                local lower = string.lower(name)
                if string.match(lower, "^anim_door_open$")
                    or string.match(lower, "^anim_door_close$") then
                    return true
                end
                if string.match(lower, "^anim_.+_door_open$")
                    or string.match(lower, "^anim_.+_door_close$") then
                    return true
                end
                return false
            end
            -- Walk the prefab Model's descendants.
            if _parent then
                for _, d in ipairs(_parent:GetDescendants()) do
                    if d:IsA("Script") or d:IsA("LocalScript") then
                        if _matchName(d.Name) then return true end
                    end
                end
            end
            -- Animation phase parents drivers under various services
            -- depending on whether the driver is prefab-scoped or
            -- scene-scoped:
            --   - prefab-scoped:  ReplicatedStorage.Templates.<Prefab>
            --                     (also a child of the prefab Model)
            --   - scene-scoped:   ServerScriptService (round-11 [P1])
            -- Scan all three to catch every shape.
            for _, svcName in ipairs({"ReplicatedStorage", "ServerScriptService"}) do
                local svc = game:GetService(svcName)
                for _, d in ipairs(svc:GetDescendants()) do
                    if d:IsA("Script") or d:IsA("LocalScript") then
                        if _matchName(d.Name) then return true end
                    end
                end
            end
            return false
        end
        if not _hasAnimDriver() then
            local _STUDS_PER_METER = 3.571
            local _OPEN_OFFSET = Vector3.new(0, 4 * _STUDS_PER_METER, 0)
            local _closedCFrame = _doorMesh.CFrame
            local _openCFrame = _closedCFrame + _OPEN_OFFSET
            local _currentTween
            local function _animateTo(target)
                if _currentTween then _currentTween:Cancel() end
                _currentTween = TweenService:Create(
                    _doorMesh,
                    TweenInfo.new(1, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
                    { CFrame = target }
                )
                _currentTween:Play()
            end
            _doorMesh:GetAttributeChangedSignal("open"):Connect(function()
                if _doorMesh:GetAttribute("open") then
                    _animateTo(_openCFrame)
                else
                    _animateTo(_closedCFrame)
                end
            end)
        end
    end
end
"""


@patch_pack(
    name="door_tween_open",
    description=(
        "Append a TweenService listener at the end of Door.luau scripts "
        "so the sibling ``door`` mesh actually slides on attribute "
        "change. Without this, doors are stuck closed — the AI transpile "
        "of Unity's Door.cs only flips the attribute, expecting the "
        "Mecanim Animator to do the motion (which the converter doesn't "
        "translate)."
    ),
    detect=_detect_door_tween_target,
)
def _inject_door_tween(scripts: list["RbxScript"]) -> int:
    fixes = 0
    # The injected tween body carries a runtime coexistence guard
    # that defers to any sibling ``Anim_*_door_*`` driver, so the
    # apply path doesn't need to skip project-wide. Round-9's
    # project-wide bail was unsafe for mixed projects where some
    # doors had drivers and others didn't (codex round-10 [P2]).
    for s in scripts:
        if s.name != "Door":
            continue
        if (
            not _DOOR_OPEN_SETATTR_RE.search(s.source)
            or not _DOOR_SIBLING_LOOKUP_RE.search(s.source)
            or "_AutoFpsDoorTweenInjected" in s.source
        ):
            continue
        s.source = s.source.rstrip() + "\n" + _DOOR_TWEEN_BLOCK
        fixes += 1
        log.info("  Injected door tween listener in '%s'", s.name)
    return fixes


# ---------------------------------------------------------------------------
# Pack: bullet_physics_raycast
# ---------------------------------------------------------------------------
#
# Unity's bullet scripts (``TurretBullet.cs``, ``PlaneBullet.cs``) use
# ``rb.AddRelativeForce(Vector3.forward * force, Impulse)`` plus
# ``OnCollisionEnter`` for hit detection. The AI transpile maps that
# to ``rootPart:ApplyImpulse(impulseDir * force * mass)`` plus
# ``Touched``. Three bugs stack in the converted output:
#
#  1. ``force=60`` is in Unity m/s but Roblox impulses are in stud-units.
#     1 Unity m ≈ 3.571 studs, so the bullet flies 5.7x too slow.
#  2. Roblox gravity is 196 studs/s², equivalent to ~55 m/s² (vs Unity's
#     9.81 m/s²) — ~5.6x stronger. Bullets nose-dive into the ground.
#  3. ``Touched`` events tunnel past targets at high speed (3+ studs per
#     frame at typical bullet velocities), so even when the bullet
#     reaches the player the hit doesn't register.
#
# Fix: replace the bullet's ``Start``-like body and ``Touched`` handler
# with raycast-based hit detection that:
#   - Sets ``AssemblyLinearVelocity = forward * force * STUDS_PER_METER``
#   - Adds an anti-gravity ``VectorForce`` (``force = gravity * mass``)
#   - Raycasts each ``Heartbeat`` from the previous frame's position to
#     the current to catch tunnel-through hits
#   - Adds a visible ``Trail`` so the trajectory reads in-game

# Match ANY local-variable ApplyImpulse / AssemblyLinearVelocity write —
# the AI transpile of Unity's bullet scripts uses different local names
# across runs (``rootPart``, ``rb``, ``part``, ``container``, etc.) AND
# different physics-init shapes (``:ApplyImpulse(...)`` vs direct
# ``.AssemblyLinearVelocity = ...`` assignment). Codex round-2 caught
# the local-name variance for PlaneBullet; this also covers the
# AssemblyLinearVelocity shape observed in TurretBullet outputs. The
# bullet-name scope (``TurretBullet`` / ``PlaneBullet`` only) still
# gates the pack from touching unrelated velocity-writing scripts.
_BULLET_DETECT_RE = re.compile(
    r'\b\w+\s*:\s*ApplyImpulse\s*\(|\b\w+\.AssemblyLinearVelocity\s*=',
)
_BULLET_TOUCHED_RE = re.compile(
    r'\b\w+\.Touched\s*:\s*Connect\b',
)


def _detect_bullet_unity_transpile(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name not in ("TurretBullet", "PlaneBullet"):
            continue
        if (
            _BULLET_DETECT_RE.search(s.source)
            and _BULLET_TOUCHED_RE.search(s.source)
            and "_AutoBulletRaycastInjected" not in s.source
        ):
            return True
    return False


# Per-bullet defaults derived from the canonical Unity sources
# (``TurretBullet.cs``, ``PlaneBullet.cs``). Codex round-1 caught a
# regression: applying ``TurretBullet``'s defaults (3/60, direct-hit
# only) to ``PlaneBullet`` silently dropped PlaneBullet's 6-second
# fade, 200-force velocity, and ~2-stud OverlapSphere splash damage.
# ``splash_radius`` of 0 means direct-hit only (turret); >0 enables
# the splash branch with an ``OverlapParams`` proximity check.
_BULLET_DEFAULTS = {
    "TurretBullet": {"fadeTime": 3, "force": 60, "damage": 10, "splash_radius": 0},
    "PlaneBullet": {"fadeTime": 6, "force": 200, "damage": 10, "splash_radius": 2},
}


def _build_bullet_replacement(name: str) -> str:
    """Build the bullet replacement script body for *name* using the
    Unity-canonical defaults. Marker comment ``_AutoBulletRaycastInjected``
    pins idempotency. Splash-damage branch is emitted only when the
    bullet has a non-zero ``splash_radius`` (PlaneBullet) — keeping
    TurretBullet's body the lean direct-hit version.
    """
    defaults = _BULLET_DEFAULTS.get(name, _BULLET_DEFAULTS["TurretBullet"])
    splash_radius = defaults["splash_radius"]
    splash_branch = ""
    if splash_radius > 0:
        # Splash damage: at impact, find players within ``splash_radius``
        # studs * STUDS_PER_METER (Unity OverlapSphere works in meters)
        # and apply damage to each.
        # Explosion VFX (codex round-5 [P3]): Unity ``PlaneBullet.cs``
        # instantiates an ``explosion`` GameObject on every collision.
        # Clone the ``Explosion`` template from
        # ``ReplicatedStorage.Templates`` if present so the converted
        # output preserves the VFX/audio feedback. No-op when the
        # template is absent (older outputs without prefab packages).
        splash_branch = f'''
local _ReplicatedStorage = game:GetService("ReplicatedStorage")
local _explosionTemplate = (function()
    local t = _ReplicatedStorage:FindFirstChild("Templates")
    return t and t:FindFirstChild("Explosion")
end)()
local function _spawnExplosionAt(originPos)
    if not _explosionTemplate then return end
    local clone = _explosionTemplate:Clone()
    if clone:IsA("Model") then
        clone:PivotTo(CFrame.new(originPos))
    elseif clone:IsA("BasePart") then
        clone.CFrame = CFrame.new(originPos)
    end
    clone.Parent = workspace
    Debris:AddItem(clone, 2)
end
local function applyAreaDamage(originPos)
    local radius = {splash_radius} * STUDS_PER_METER
    for _, player in ipairs(Players:GetPlayers()) do
        local char = player.Character
        local hrp = char and char:FindFirstChild("HumanoidRootPart")
        if hrp and (hrp.Position - originPos).Magnitude <= radius then
            local model = hrp.Parent
            model:SetAttribute("TakeDamage", damage)
            local humanoid = model:FindFirstChildOfClass("Humanoid")
            if humanoid then humanoid:TakeDamage(damage) end
        end
    end
end
'''
    # Direct-hit path (Humanoid model raycast hit).
    #
    # Splash bullets (PlaneBullet): Unity's ``OnCollisionEnter`` runs
    # ``OverlapSphere(2)`` and applies damage to every Player within
    # the radius — including the directly-hit one. Splash bullets
    # therefore use ``applyAreaDamage`` as their ONLY damage source,
    # matching Unity's single-pass behavior.
    #
    # Direct-hit-only bullets (TurretBullet): no splash in Unity, so
    # the only damage source is the direct attribute write.
    #
    # Codex round-5 [P1]: splash centers on the raycast ``result``'s
    # ``Position`` (the actual collision point), NOT
    # ``rootPart.Position``. At ``force=200``+ a tunneling frame can
    # put rootPart 10-12 studs past the collision point — larger than
    # the 7-stud splash radius — so a wall-hit beside a player would
    # miss entirely.
    if splash_radius > 0:
        apply_hit_body = (
            '    _spawnExplosionAt(impactPos)\n'
            '    applyAreaDamage(impactPos)\n'
            '    container:Destroy()\n'
        )
        # Non-character impact (wall, ground, prop) — splash bullets
        # still explode and apply area damage. Mirrors Unity
        # ``PlaneBullet.cs`` ``OnCollisionEnter`` which instantiates
        # the explosion and runs ``OverlapSphere`` regardless of the
        # collider's tag.
        non_char_impact_body = (
            '                consumed = true\n'
            '                _spawnExplosionAt(result.Position)\n'
            '                applyAreaDamage(result.Position)\n'
            '                container:Destroy()\n'
            '                return\n'
        )
    else:
        apply_hit_body = (
            '    model:SetAttribute("TakeDamage", damage)\n'
            '    local humanoid = model:FindFirstChildOfClass("Humanoid")\n'
            '    if humanoid then humanoid:TakeDamage(damage) end\n'
            '    container:Destroy()\n'
        )
        # Direct-hit bullets just despawn on terrain/wall impact —
        # matches Unity ``TurretBullet.cs`` whose ``OnCollisionEnter``
        # only damages on player tag and otherwise destroys the bullet.
        non_char_impact_body = (
            '                consumed = true\n'
            '                container:Destroy()\n'
            '                return\n'
        )
    return f'''\
-- _AutoBulletRaycastInjected: bullet coherence pack
-- Stud-space velocity + anti-gravity + raycast hit detection.
-- Replaces the AI transpile of Unity's {name}.cs
-- (rb.AddRelativeForce + OnCollisionEnter), which stacks three bugs
-- in stud-space Roblox physics: 5.7x slow velocity, 5.6x strong
-- gravity, and Touched tunneling at high speeds.
local Debris = game:GetService("Debris")
local RunService = game:GetService("RunService")
local Players = game:GetService("Players")

local container = script.Parent

-- Template-guard. The bullet template Script has ``RunContext = Server``,
-- so Roblox runs it as a server script regardless of parent — including
-- when it sits in ``ReplicatedStorage.Templates`` at place load. Without
-- this guard, the template's own script applies velocity to the template
-- and ``Debris:AddItem(container, fadeTime)`` destroys the template
-- ``fadeTime`` seconds in. Subsequent ``Turret.luau`` ``template:Clone()``
-- calls then return a Part with no Script child (clones of a destroyed
-- instance), so the spawned bullets fall under gravity with no velocity
-- and never damage the player.
do
    local p = container and container.Parent
    if p and (p.Name == "Templates" or p:IsA("ServerStorage")
        or p:IsA("ReplicatedStorage")) then
        return
    end
end

if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end
local function getRootPart()
    if container:IsA("Model") then
        return container.PrimaryPart or container:FindFirstChildWhichIsA("BasePart")
    end
    return container
end

-- Read serialized MonoBehaviour overrides from the part's
-- attributes; the converter's ``_extract_monobehaviour_attributes``
-- emits ``fadeTime``/``force``/``damage`` as float attributes so
-- prefab/instance inspector tuning carries through to runtime.
-- Unity-canonical defaults are the fallback when an attribute is
-- absent (e.g. older outputs or a clone whose attributes were
-- stripped at clone time).
local _attrHost = container:IsA("Model") and (container.PrimaryPart or container) or container
local fadeTime = _attrHost:GetAttribute("fadeTime") or {defaults["fadeTime"]}
local force = _attrHost:GetAttribute("force") or {defaults["force"]}
local damage = _attrHost:GetAttribute("damage") or {defaults["damage"]}
local STUDS_PER_METER = 3.571

local rootPart = getRootPart()
if not rootPart then return end

rootPart.Anchored = false

-- Anti-gravity: cancel Roblox's 196 studs/s² so bullets fly straight
-- like Unity's near-massless impulse (gravity barely affects them
-- across the bullet's fadeTime in Unity's 9.81 m/s² space).
local att0 = Instance.new("Attachment")
att0.Name = "_BulletAtt0"
att0.Parent = rootPart
local antiG = Instance.new("VectorForce")
antiG.Force = Vector3.new(0, workspace.Gravity * rootPart.AssemblyMass, 0)
antiG.RelativeTo = Enum.ActuatorRelativeTo.World
antiG.Attachment0 = att0
antiG.ApplyAtCenterOfMass = true
antiG.Parent = rootPart

-- Resolve aim direction. Bullets spawn at the Unity ``Origin`` empty,
-- but its local rotation within the parent ``Weapon`` varies across
-- prefab instances (some have identity orientation, some are flipped
-- 158° — measured live on SimpleFPS turrets). Using the bullet's own
-- LookVector fires backward on the rotated instances. Use the parent
-- Weapon (or whatever container is two ancestors up — same depth as
-- the Turret.luau ``firstStructuralChild(firstStructuralChild(...))``
-- climb) which Turret.luau aims via ``CFrame.lookAt(weaponPos, targetPos)``
-- every frame, so its LookVector reliably points at the player.
local _aimCF = getCFrame()
do
    local _ancestor = container.Parent
    while _ancestor and _ancestor ~= workspace do
        if (_ancestor:IsA("Model") or _ancestor:IsA("BasePart"))
            and _ancestor.Name == "Weapon" then
            _aimCF = _ancestor:IsA("Model")
                and _ancestor:GetPivot() or _ancestor.CFrame
            break
        end
        _ancestor = _ancestor.Parent
    end
end

-- Push the bullet 3 studs forward of the muzzle BEFORE applying
-- velocity. Unity's ``Origin`` empty has no collider so spawning
-- inside the weapon's mesh bounding volume is fine in Unity, but in
-- Roblox the bullet's first-frame raycast back-traces from this
-- in-mesh position and self-destructs against the weapon. Teleport
-- via ``container.CFrame = …`` AFTER applying velocity also wipes
-- ``AssemblyLinearVelocity`` back to zero — so order matters: push,
-- THEN velocity.
do
    local _pushedCF = _aimCF + _aimCF.LookVector * 3
    if container:IsA("Model") then
        container:PivotTo(_pushedCF)
    else
        container.CFrame = _pushedCF
    end
end

-- Initial velocity in stud-space (Unity m/s × STUDS_PER_METER).
-- Computed from the aim CFrame (parent Weapon when found, falling
-- back to the bullet's own CFrame for non-turret bullets).
rootPart.AssemblyLinearVelocity = _aimCF.LookVector * (force * STUDS_PER_METER)

-- Trail for visible trajectory
local att1 = Instance.new("Attachment")
att1.Name = "_TrailAtt1"
att1.Position = Vector3.new(0, 0, 0.5)
att1.Parent = rootPart
local att2 = Instance.new("Attachment")
att2.Name = "_TrailAtt2"
att2.Position = Vector3.new(0, 0, -0.5)
att2.Parent = rootPart
local trail = Instance.new("Trail")
trail.Attachment0 = att1
trail.Attachment1 = att2
trail.Lifetime = 0.4
trail.MinLength = 0
trail.WidthScale = NumberSequence.new({{
    NumberSequenceKeypoint.new(0, 1),
    NumberSequenceKeypoint.new(1, 0),
}})
trail.Color = ColorSequence.new(Color3.fromRGB(255, 100, 50))
trail.Transparency = NumberSequence.new({{
    NumberSequenceKeypoint.new(0, 0),
    NumberSequenceKeypoint.new(1, 1),
}})
trail.Parent = rootPart

Debris:AddItem(container, fadeTime)
{splash_branch}
-- Raycast hit detection: cast segment from previous-frame position to
-- current each heartbeat. Catches tunneling that Touched misses.
local prevPos = rootPart.Position
local consumed = false
local rp = RaycastParams.new()
rp.FilterDescendantsInstances = {{container}}
rp.FilterType = Enum.RaycastFilterType.Exclude

local function applyHit(model, impactPos)
{apply_hit_body}end

local conn
conn = RunService.Heartbeat:Connect(function()
    if consumed or not rootPart.Parent then
        if conn then conn:Disconnect() end
        return
    end
    local curPos = rootPart.Position
    local segment = curPos - prevPos
    if segment.Magnitude > 0 then
        local result = workspace:Raycast(prevPos, segment, rp)
        if result then
            local inst = result.Instance
            local model = inst:FindFirstAncestorOfClass("Model")
            if model and model:FindFirstChildOfClass("Humanoid") then
                local player = Players:GetPlayerFromCharacter(model)
                if player or inst:HasTag("Player") or model.Name == "Player" then
                    consumed = true
                    applyHit(model, result.Position)
                    return
                end
            else
                -- Hit non-character (terrain, wall, prop). Splash
                -- bullets explode here too; direct-hit bullets just
                -- despawn. Behavior is template-controlled by
                -- ``non_char_impact_body``.
{non_char_impact_body}            end
        end
    end
    prevPos = curPos
end)
'''


@patch_pack(
    name="bullet_physics_raycast",
    description=(
        "Replace AI-transpiled Unity bullet bodies (TurretBullet, "
        "PlaneBullet) with stud-space velocity + anti-gravity + raycast "
        "hit detection. Without this, bullets fly too slow, nose-dive, "
        "and tunnel past targets."
    ),
    detect=_detect_bullet_unity_transpile,
)
def _replace_bullet_physics(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name not in ("TurretBullet", "PlaneBullet"):
            continue
        if (
            not _BULLET_DETECT_RE.search(s.source)
            or not _BULLET_TOUCHED_RE.search(s.source)
            or "_AutoBulletRaycastInjected" in s.source
        ):
            continue
        s.source = _build_bullet_replacement(s.name)
        fixes += 1
        log.info("  Replaced bullet physics in '%s'", s.name)
    return fixes


# ---------------------------------------------------------------------------
# Pack: player_damage_remote_event
# ---------------------------------------------------------------------------
#
# Unity's ``Player.cs`` raycasts on click and ``SendMessage("TakeDamage",
# damage)`` on the hit collider. The AI transpile renders that as a
# LocalScript that ``:SetAttribute("TakeDamage", true)`` on the hit
# instance. But LocalScript attribute writes DON'T replicate to the
# server — so server-side scripts (e.g. Turret.luau) listening via
# ``GetAttributeChangedSignal("TakeDamage")`` never fire. Player can
# never damage anything.
#
# Fix: introduce a ``DamageEvent`` RemoteEvent in ReplicatedStorage.
# Player.luau ``shoot()`` ``FireServer(hitInst)`` after each raycast
# hit. A tiny server router (auto-generated) listens and sets the
# attribute server-side so the existing
# ``GetAttributeChangedSignal`` listeners fire.

# Match ANY local-variable ``:SetAttribute("TakeDamage", <expr>)``
# coming out of the AI transpile. Different runs vary on TWO axes:
#   1. The local name: ``hitInst``, ``hitPart``, ``hit``, etc. —
#      capture group 1 carries the identifier so the injected
#      FireServer call references the same name.
#   2. The value: ``true`` is the canonical shape, but the AI
#      sometimes emits an incrementing form to force the change
#      signal — e.g. ``(<inst>:GetAttribute("TakeDamage") or 0) + 1``.
#      Match anything after the comma up to the closing ``)`` so
#      either shape triggers the pack. Codex round-4 [P2] flagged
#      that the prior literal-``true`` regex missed real outputs.
_PLAYER_RAYCAST_HIT_RE = re.compile(
    r'\b([A-Za-z_]\w*)\s*:\s*SetAttribute\s*\(\s*["\']TakeDamage["\']\s*,\s*[^)\n]+\s*\)',
)


def _detect_player_damage_attr_set(scripts: list["RbxScript"]) -> bool:
    """Match only LocalScript Player bodies. ``FireServer`` is client-
    only, so injecting it into a server ``Script`` would crash at
    runtime on the first shot. Server classifications happen on
    converted projects whose Player.cs was misclassified by the
    storage-classifier — defending here keeps the pack from poisoning
    those builds.
    """
    for s in scripts:
        if s.name != "Player":
            continue
        if s.script_type != "LocalScript":
            continue
        if (
            _PLAYER_RAYCAST_HIT_RE.search(s.source)
            and "_AutoDamageRemoteEventInjected" not in s.source
        ):
            return True
    return False


# Block inserted after the player's raycast SetAttribute. Fires the
# RemoteEvent so a server router can replay the raycast and mirror
# the attribute write.
#
# The FireServer payload is ``(hitVar, originPos, lookDir)`` where
# ``hitVar`` is the AI-named local for ``result.Instance``, and the
# origin/direction come from ``workspace.CurrentCamera.CFrame``
# (matches the client's own raycast contract: Unity Player.cs uses
# the camera, not the character root). The server replays the
# raycast from those values so legitimate over-cover shots aren't
# rejected when ``HumanoidRootPart→hitInstance`` would be occluded.
#
# ``{hit_var}`` is substituted with the AI-named local at apply time.
_PLAYER_DAMAGE_FIRE_TEMPLATE = (
    '\n            -- _AutoDamageRemoteEventInjected: mirror client damage to server\n'
    '            do\n'
    '                local _de = game:GetService("ReplicatedStorage")'
    ':FindFirstChild("DamageEvent")\n'
    '                if _de then\n'
    '                    local _cam = workspace.CurrentCamera\n'
    '                    -- Send the value the client just wrote so\n'
    '                    -- the server preserves it verbatim (codex\n'
    '                    -- round-10 [P1]: don\'t let the server\n'
    '                    -- synthesize a counter that overwrites the\n'
    '                    -- client\'s damage payload).\n'
    '                    local _td = {hit_var}:GetAttribute("TakeDamage")\n'
    '                    _de:FireServer({hit_var}, _td, _cam.CFrame.Position, '
    '_cam.CFrame.LookVector)\n'
    '                end\n'
    '            end\n'
)


# Auto-generated server router script. The router is NOT client-
# authoritative: it re-raycasts from the firing player's character
# toward the reported hit instance and only applies damage when the
# server-side raycast actually intersects that instance within the
# player's effective weapon range. This matches Unity Player.cs's
# ``shootRange = 100`` (meters) ≈ 357 studs.
#
# Without this validation, a malicious client could ``FireServer``
# any workspace instance and the server would apply damage — fully
# client-authoritative in multiplayer.
_DAMAGE_ROUTER_SOURCE = '''\
-- _AutoDamageEventRouter (auto-generated by player_damage_remote_event pack)
-- Mirrors client-fired damage hits to server-side ``TakeDamage``
-- attribute writes so server scripts listening via
-- ``GetAttributeChangedSignal`` actually fire.
--
-- The router replays the client's raycast on the server from the
-- client-supplied camera origin + direction. Origin/direction are
-- sanity-checked against the player's character (origin within
-- ``MAX_ORIGIN_DRIFT_STUDS`` of the character's head/HRP, direction
-- a unit vector). Without origin replay the server-from-HRP raycast
-- rejects legitimate over-cover shots (Unity FPS Player.cs raycasts
-- from the camera, not the character root).
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local STUDS_PER_METER = 3.571
-- Mirror of Unity Player.cs ``shootRange = 100`` (meters) + 50% slack
-- so a borderline-range raycast from the server doesn't drop the
-- intended hit. Adjust if your project's player script uses a
-- different range.
local MAX_SHOOT_RANGE_STUDS = 100 * STUDS_PER_METER * 1.5
-- Origin sanity bound: the client may shoot from a free-look camera
-- offset from the character, but the camera shouldn't be more than
-- a few avatars away from the player's HRP. 20 studs of slack covers
-- normal first/third-person rigs without admitting arbitrary teleport
-- spoofs.
local MAX_ORIGIN_DRIFT_STUDS = 20

local function _matchesIntendedHit(serverHit, intendedInst)
    if serverHit == intendedInst then return true end
    local serverModel = serverHit:FindFirstAncestorOfClass("Model")
    local intendedModel = intendedInst:FindFirstAncestorOfClass("Model")
    if serverModel and intendedModel and serverModel == intendedModel then
        return true
    end
    return false
end

local de = ReplicatedStorage:FindFirstChild("DamageEvent")
if not de then
    de = Instance.new("RemoteEvent")
    de.Name = "DamageEvent"
    de.Parent = ReplicatedStorage
end

de.OnServerEvent:Connect(function(player, hitInstance, takeDamageValue, originPos, lookDir)
    -- Type guards FIRST: a malicious client can ``FireServer(true)``
    -- / ``FireServer({})`` and the server must reject the payload
    -- before calling any Instance methods (would throw otherwise).
    if typeof(hitInstance) ~= "Instance" then return end
    if not hitInstance:IsA("BasePart") then return end
    if not hitInstance:IsDescendantOf(workspace) then return end
    if typeof(originPos) ~= "Vector3" then return end
    if typeof(lookDir) ~= "Vector3" then return end
    if lookDir.Magnitude < 1e-3 then return end

    local char = player.Character
    local hrp = char and char:FindFirstChild("HumanoidRootPart")
    if not hrp then return end

    -- Origin must be close to the player's character (anti-teleport).
    if (originPos - hrp.Position).Magnitude > MAX_ORIGIN_DRIFT_STUDS then
        return
    end

    -- Server replay: cast from the client-supplied origin + direction.
    -- Mirrors what the client's Player.cs raycast already did, so over-
    -- cover camera shots are accepted just like in single-player.
    local rp = RaycastParams.new()
    rp.FilterDescendantsInstances = {char}
    rp.FilterType = Enum.RaycastFilterType.Exclude
    local result = workspace:Raycast(
        originPos,
        lookDir.Unit * MAX_SHOOT_RANGE_STUDS,
        rp
    )
    if not (result and _matchesIntendedHit(result.Instance, hitInstance)) then
        return
    end

    -- Validated. Mirror the client's TakeDamage write VERBATIM.
    -- Codex round-10 [P1]: synthesizing a counter on the server
    -- discards the client's payload, which under-damages listeners
    -- that read ``TakeDamage`` as the damage amount. The client
    -- captures whatever it wrote (``true`` for the canonical AI
    -- transpile, or an incrementing token for change-signal-safe
    -- shapes, or a numeric damage value) and sends it through; the
    -- server preserves that semantics.
    --
    -- Codex round-11 [P2]: Roblox attributes only accept primitive
    -- scalars (bool/number/string + a few Vector/Color types). A
    -- malicious client could send a table or function and crash
    -- ``SetAttribute``. Coerce anything other than the canonical
    -- shapes (bool/number/string) to ``true`` so a listener doing
    -- ``if attr then`` still reacts but no SetAttribute call ever
    -- gets handed an invalid payload.
    local _t = typeof(takeDamageValue)
    if _t ~= "boolean" and _t ~= "number" and _t ~= "string" then
        takeDamageValue = true
    end
    hitInstance:SetAttribute("TakeDamage", takeDamageValue)
    local m = hitInstance:FindFirstAncestorOfClass("Model")
    if m then m:SetAttribute("TakeDamage", takeDamageValue) end
end)
'''


_DAMAGE_ROUTER_NAME = "_AutoDamageEventRouter"


def _detect_player_or_router_present(scripts: list["RbxScript"]) -> bool:
    """Run when ANY of:
      - a ``Player`` LocalScript still needs the ``FireServer`` patch
      - a ``Player`` LocalScript is already patched but the router
        script is absent (rehydrated re-conversion lost the router)
      - a ``Player`` LocalScript is already patched AND a router
        exists but its source is stale (re-conversion of an
        already-patched output should refresh the router with any
        round-to-round improvements — codex round-11 [P2])

    The apply path compares router source byte-for-byte and only
    rewrites on diff, so this detector being more permissive doesn't
    re-write a fresh router.
    """
    if _detect_player_damage_attr_set(scripts):
        return True
    has_patched_player = any(
        s.name == "Player"
        and s.script_type == "LocalScript"
        and "_AutoDamageRemoteEventInjected" in s.source
        for s in scripts
    )
    router = next(
        (s for s in scripts if s.name == _DAMAGE_ROUTER_NAME), None,
    )
    if not has_patched_player:
        return False
    if router is None:
        return True  # Missing — must emit.
    # Stale router (source diverges from the canonical pack version)
    # → refresh. ``_inject_player_damage_remote_event``'s router
    # branch only writes when the source actually differs.
    return router.source != _DAMAGE_ROUTER_SOURCE


@patch_pack(
    name="player_damage_remote_event",
    description=(
        "Mirror client-side Player raycast damage hits to the server "
        "via a DamageEvent RemoteEvent + auto-injected server router. "
        "Without this, the server-side TakeDamage attribute listener "
        "never fires (LocalScript SetAttribute doesn't replicate)."
    ),
    detect=_detect_player_or_router_present,
)
def _inject_player_damage_remote_event(scripts: list["RbxScript"]) -> int:
    fixes = 0
    # 1. Patch every Player LocalScript's raycast hit branch to FireServer.
    # Server ``Script`` classifications are skipped — ``FireServer`` is
    # client-only and would error on the first shot.
    for s in scripts:
        if s.name != "Player" or s.script_type != "LocalScript":
            continue
        if "_AutoDamageRemoteEventInjected" in s.source:
            continue
        m = _PLAYER_RAYCAST_HIT_RE.search(s.source)
        if not m:
            continue
        hit_var = m.group(1)
        # Insert after the model:SetAttribute("TakeDamage", true) line
        # so both the hit-instance and model writes complete before the
        # FireServer. Scope: look for the closing ``end`` of the
        # ``if model then ... end`` block immediately after the hit
        # SetAttribute. If absent, insert right after the hit line.
        # Anchor on the captured hit-var SetAttribute call. Tolerates:
        #   - both the canonical ``true`` literal AND the incrementing
        #     ``(GetAttribute("TakeDamage") or 0) + 1`` shape (nested
        #     parens, so consume up to the next newline rather than ``)``).
        #   - optional ``local model = hitInst:FindFirstAncestor...``
        #     line right after the SetAttribute.
        #   - both single-line ``if model then model:SetAttribute(...) end``
        #     and multi-line forms that wrap the SetAttribute on its
        #     own line. Codex round-8 [P1] flagged that the multi-line
        #     shape (already present in some converter outputs) didn't
        #     match the single-line anchor and silently skipped the
        #     FireServer injection.
        anchor_re = re.compile(
            rf'({re.escape(hit_var)}\s*:\s*SetAttribute\s*\(\s*["\']TakeDamage["\'][^\n]*\n'
            r'(?:\s*local\s+model\s*=[^\n]+\n)?'
            # Single-line: ``if model then model:SetAttribute(...) end``
            r'(?:\s*if\s+model\s+then\s+model\s*:\s*SetAttribute[^\n]+end\s*\n'
            # Multi-line:
            #   if model then
            #       model:SetAttribute("TakeDamage", ...)
            #   end
            r'|\s*if\s+model\s+then\s*\n'
            r'\s*model\s*:\s*SetAttribute[^\n]+\n'
            r'\s*end\s*\n)?)',
        )
        anchor_m = anchor_re.search(s.source)
        if not anchor_m:
            continue
        insert_at = anchor_m.end()
        insert_block = _PLAYER_DAMAGE_FIRE_TEMPLATE.replace(
            "{hit_var}", hit_var,
        )
        s.source = (
            s.source[:insert_at]
            + insert_block
            + s.source[insert_at:]
        )
        fixes += 1
        log.info("  Patched '%s' to FireServer (hit var=%s) on raycast hit",
                 s.name, hit_var)

    # 2. Emit (or refresh) the auto-generated server router. Idempotent:
    # if a router with the canonical name exists, replace its source to
    # keep behaviour in sync with the pack version. Otherwise append a
    # new RbxScript to the list.
    from core.roblox_types import RbxScript
    router_name = _DAMAGE_ROUTER_NAME
    existing = next((s for s in scripts if s.name == router_name), None)
    if existing is not None:
        if existing.source != _DAMAGE_ROUTER_SOURCE:
            existing.source = _DAMAGE_ROUTER_SOURCE
            fixes += 1
            log.info("  Refreshed %s source", router_name)
    else:
        scripts.append(
            RbxScript(
                name=router_name,
                source=_DAMAGE_ROUTER_SOURCE,
                script_type="Script",
                parent_path="ServerScriptService",
            )
        )
        fixes += 1
        log.info("  Emitted %s server router script", router_name)

    return fixes
