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
