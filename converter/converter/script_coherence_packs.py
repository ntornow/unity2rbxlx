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

@patch_pack(
    name="pickup_remote_event_server",
    description="Convert Pickup script SetAttribute calls to "
    "ReplicatedStorage.PickupItemEvent:FireClient — server-side "
    "SetAttribute does not trigger client GetAttributeChangedSignal.",
    detect=_detect_fps_rifle_pickup,
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

        # Some AI variants skip SetAttribute entirely and emit their own
        # RemoteEvent (e.g. ``PickupGetItem``) created at script init. Player
        # listens on the canonical ``PickupItemEvent`` (injected by the
        # _add_pickup_remote_listener pack), so a non-canonical name leaves
        # the two sides talking past each other. Rewrite any RemoteEvent
        # named ``Pickup*`` in a Pickup script to the canonical name.
        re_alias_pattern = re.compile(
            r'"(Pickup(?:GetItem|Get|Event|Item|Remote))"'
        )
        renamed = 0
        def _alias_replace(m: "re.Match[str]") -> str:
            nonlocal renamed
            if m.group(1) == "PickupItemEvent":
                return m.group(0)
            renamed += 1
            return '"PickupItemEvent"'

        new_src3 = re_alias_pattern.sub(_alias_replace, s.source)
        if renamed:
            s.source = new_src3
            fixes += renamed
            log.info(
                "  Canonicalized %d Pickup RemoteEvent name(s) to PickupItemEvent",
                renamed,
            )
    return fixes


@patch_pack(
    name="pickup_remote_event_client",
    description="Add OnClientEvent listener for PickupItemEvent in Player "
    "scripts that own the GetRifle function.",
    after=("fps_rifle_inject", "pickup_remote_event_server"),
    detect=_detect_fps_rifle_pickup,
)
def _add_pickup_remote_listener(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        # Detect a Player-style controller by name presence: AI transpilation
        # emits Lua-conventional camelCase (`getItem`, `getRifle`) while
        # earlier rule-based output kept C# Title-case (`GetItem`, `GetRifle`).
        # Match either by lowering the search.
        src_lower = s.source.lower()
        if 'getitem' not in src_lower or 'getrifle' not in src_lower:
            continue
        if 'PickupItemEvent' in s.source:
            continue
        # Pick whichever casing the script already uses for the dispatch call
        # so we don't introduce an undefined identifier.
        get_item_name = 'getItem' if 'getItem' in s.source else 'GetItem'
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
# Pack: hud_player_bindable_events
# ---------------------------------------------------------------------------

# Maps the lowercase BindableEvent variable suffix the AI typically emits in
# Player.luau (``healthUpdateEvent``, ``ammoUpdateEvent``, …) to the
# CamelCase name that HUD-style consumers look up via
# ``ReplicatedStorage:FindFirstChild("HealthUpdate")``. The two sides are
# wired independently by the AI and don't agree on naming, so without this
# pack the HUD never receives Player-side updates.
_PLAYER_HUD_EVENT_NAMES = {
    "healthUpdateEvent": "HealthUpdate",
    "ammoUpdateEvent": "AmmoUpdate",
    "itemUpdateEvent": "ItemUpdate",
    "pauseEvent": "PauseEvent",
}


def _detect_player_hud_events(scripts: list["RbxScript"]) -> bool:
    has_player = False
    has_hud = False
    for s in scripts:
        src = s.source or ""
        if any(v in src for v in _PLAYER_HUD_EVENT_NAMES) and \
                'Instance.new("BindableEvent")' in src:
            has_player = True
        # HUD-style consumer: looks up event names in ReplicatedStorage by
        # name. Match either the literal ``FindFirstChild("Foo")`` form or a
        # ``resolveEvent("Foo")``-style helper that takes the string as an
        # argument — both produce the same lookup at runtime.
        for n in _PLAYER_HUD_EVENT_NAMES.values():
            if (
                f'FindFirstChild("{n}")' in src
                or f'WaitForChild("{n}")' in src
                or (
                    f'"{n}"' in src
                    and "ReplicatedStorage" in src
                    and ("FindFirstChild" in src or "WaitForChild" in src)
                )
            ):
                has_hud = True
                break
        if has_player and has_hud:
            return True
    return False


@patch_pack(
    name="hud_player_bindable_events",
    description="Publish Player.luau's anonymous BindableEvents to "
    "ReplicatedStorage under the names HudControl.luau expects, so HUD "
    "subscribers actually receive health/ammo/item updates.",
    detect=_detect_player_hud_events,
)
def _publish_player_hud_events(scripts: list["RbxScript"]) -> int:
    fixes = 0
    decl_re = re.compile(
        r'^(\s*local\s+(' + '|'.join(map(re.escape, _PLAYER_HUD_EVENT_NAMES))
        + r')\s*=\s*Instance\.new\(\s*["\']BindableEvent["\']\s*\)\s*)$',
        re.MULTILINE,
    )
    for s in scripts:
        if 'Instance.new("BindableEvent")' not in s.source:
            continue
        original = s.source
        seen: set[str] = set()

        def _replace(m: "re.Match[str]") -> str:
            decl, var = m.group(1), m.group(2)
            event_name = _PLAYER_HUD_EVENT_NAMES.get(var)
            if not event_name or var in seen:
                return decl
            seen.add(var)
            return (
                f'{decl}\n'
                f'{var}.Name = "{event_name}"\n'
                f'do local _existing = game:GetService("ReplicatedStorage")'
                f':FindFirstChild("{event_name}"); '
                f'if _existing and _existing:IsA("BindableEvent") then '
                f'{var}:Destroy(); {var} = _existing else '
                f'{var}.Parent = game:GetService("ReplicatedStorage") end end'
            )

        s.source = decl_re.sub(_replace, s.source)
        if s.source != original and seen:
            fixes += 1
            log.info(
                "  Published %d Player BindableEvent(s) to ReplicatedStorage "
                "in '%s': %s",
                len(seen), s.name, ", ".join(sorted(seen)),
            )
    return fixes

