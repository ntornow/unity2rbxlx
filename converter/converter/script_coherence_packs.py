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
# FPS weapon-mount registry
# ---------------------------------------------------------------------------
#
# Each ``WeaponMount`` entry parameterises one Unity FPS weapon-pickup
# pattern: the world prefab name, the equip-function the AI transpiler
# stubs out, the sentinel flag, scale, and the local variable name the
# rewrite uses. Adding a second FPS weapon (e.g. a pistol on a future
# test project) is a one-tuple append — no code change to the detector
# or injection logic.
#
# Camera tracking is NOT done here. Unity parents the weapon to a
# camera-child "WeaponSlot" transform; the converter reproduces that
# GameObject inside the ``_MainCameraRig`` Model, and the auto-injected
# CameraRigFollower pivots that whole rig onto ``workspace.CurrentCamera``
# each frame. The rewritten equip path just seats the cloned weapon into
# the rig, so it rides the player's view with no per-weapon follower and
# no hardcoded offset. ``fallback_offset_expr`` is used only when the
# scene has no rig at all.


@dataclass(frozen=True)
class WeaponMount:
    prefab_name: str           # workspace prefab name (camelCase) — the
                               # converter materialises the Unity prefab
                               # at this name in workspace, with the full
                               # mesh hierarchy (bipod, scope, etc.). The
                               # pack clones from THIS instance, not from
                               # ``ReplicatedStorage.Templates``, which
                               # carries a stripped variant whose smaller
                               # bbox drops below the camera frustum at
                               # the authored slot offset.
    equip_function: str        # AI-stubbed function name on the Player controller
    sentinel_var: str          # already-equipped flag, flipped to true on equip
    scale_expr: str            # Lua-source fragment for rifle:ScaleTo(<expr>)
    fallback_offset_expr: str  # camera-relative CFrame, used ONLY when the
                               # scene has no _MainCameraRig to seat into
    marker_tag: str            # composed into ``-- _FPS_<tag>`` for idempotency
    instance_var: str          # local that holds the cloned weapon Model


# Today's registry: one entry for the SimpleFPS rifle.
WEAPON_MOUNTS: tuple[WeaponMount, ...] = (
    WeaponMount(
        prefab_name="riflePrefab",
        equip_function="GetRifle",
        sentinel_var="gotWeapon",
        scale_expr="0.15",
        fallback_offset_expr="CFrame.new(0.5, -0.5, -3)",
        marker_tag="RIFLE_SYSTEM",
        instance_var="_fpsRifle",
    ),
)


def _prefab_alt_case(name: str) -> str:
    """Return the upper-first-letter variant of a camelCase prefab name.

    SimpleFPS stores the rifle prefab under either ``riflePrefab`` or
    ``RiflePrefab`` depending on which Unity export pass ran. Emitting
    both ``FindFirstChild`` lookups in the injected code handles both.
    """
    if not name:
        return name
    return name[:1].upper() + name[1:]


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def _detect_fps_weapon_mount(scripts: list["RbxScript"]) -> bool:
    """Pack runs when any script references one of the registered FPS
    weapon-mount patterns.

    A mount qualifies a script via either the prefab name (case variant
    included) or any spelling of the equip function name. The Unity
    source ships PascalCase (``GetRifle``); the AI transpiler emits
    camelCase (``getRifle``) by Luau convention -- the detector must
    accept both for ``run_packs`` to actually fire the pack on real
    transpile output. Pre-fix the detector only checked PascalCase, so
    the pack silently no-oped on every fresh transpile and the marker
    on disk only persisted from older PascalCase-era runs.
    Gamekit3D-style scripts contain none of these markers and skip
    cleanly.
    """
    for s in scripts:
        src = s.source
        for mount in WEAPON_MOUNTS:
            equip_variants = _equip_function_variants(mount.equip_function)
            if (
                mount.prefab_name in src
                or _prefab_alt_case(mount.prefab_name) in src
                or any(v in src for v in equip_variants)
            ):
                return True
    return False


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
\t\t\t-- Write the ``has<X>`` flag on BOTH the character Model and the
\t\t\t-- Player Instance. Door.luau's Touched handler reads from the
\t\t\t-- character (the touching part's Model ancestor); HUD scripts
\t\t\t-- often only have the Player ref. Roblox replicates instance
\t\t\t-- attributes set server-side to every client, so the client-side
\t\t\t-- Player.luau reads keep working too.
\t\t\tif itemName and itemName ~= "" then
\t\t\t\tlocal _flag = "has" .. itemName
\t\t\t\tif character then character:SetAttribute(_flag, true) end
\t\t\t\tplayer:SetAttribute(_flag, true)
\t\t\tend
\t\t\tif _pickupEvent then _pickupEvent:FireClient(player, itemName) end
\t\t\tif source then source:Play() end
\t\t\tDebris:AddItem(container, 0)
\t\tend
\tend)
end
"""


# Client-side pickup detection is mount-agnostic — runs once per Player
# script regardless of how many WeaponMount entries fired. The marker is
# the leading comment line itself, so this remains byte-identical to the
# pre-refactor output.
_PICKUP_TOUCHED_MARKER = "-- Client-side pickup detection"
_PICKUP_TOUCHED_CODE = (
    '\n' + _PICKUP_TOUCHED_MARKER + '\n'
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


def _equip_function_variants(name: str) -> tuple[str, ...]:
    """Name variants under which the AI transpiler may have emitted the
    equip function. Unity ships PascalCase (``GetRifle``); the Luau
    convention favours camelCase (``getRifle``), so the post-transpile
    output uses the latter. We support both.
    """
    if not name:
        return ()
    pascal = name[:1].upper() + name[1:]
    camel = name[:1].lower() + name[1:]
    return (camel,) if pascal == camel else (pascal, camel)


def _apply_weapon_mount(s: "RbxScript", mount: WeaponMount) -> bool:
    """Rewrite the AI-stubbed equip path for one weapon mount.

    Returns True iff this mount mutated the script. The caller appends
    the mount-agnostic pickup-Touched block exactly once per script
    after all mounts have run.
    """
    marker = f"-- _FPS_{mount.marker_tag}"
    variants = _equip_function_variants(mount.equip_function)
    if not any(v in s.source for v in variants):
        return False
    if marker in s.source:
        return False

    before = s.source

    s.source = s.source.replace(
        f"local {mount.sentinel_var} = false",
        f"local {mount.sentinel_var} = false\n"
        f"local {mount.instance_var} = nil  {marker}",
    )

    # Match three emitted shapes from the AI transpiler:
    #   1. ``GetRifle = function()`` (table-field / forward-decl assignment)
    #   2. ``function getRifle()`` (top-level function statement, Luau idiom)
    #   3. ``local function getRifle()`` (local function statement)
    # Body extends until the sentinel-set line that follows.
    name_alt = "|".join(re.escape(v) for v in variants)
    body_re = re.compile(
        rf"((?:local\s+)?function\s+(?:{name_alt})\s*\(\s*\)|(?:{name_alt})\s*=\s*function\s*\(\s*\))"
        rf"(.*?)"
        rf"(\n\s*{mount.sentinel_var}\s*=\s*true)",
        re.DOTALL,
    )
    m = body_re.search(s.source)
    if m:
        alt = _prefab_alt_case(mount.prefab_name)
        # Preserve the matched header verbatim. The injection target may
        # have been `GetRifle = function()` (table-field form) or
        # `function getRifle()` (function-statement form, Luau idiom).
        matched_header = m.group(1)
        # The equipped weapon is seated into the converted Unity camera
        # rig — the ``_MainCameraRig`` Model that the auto-injected
        # CameraRigFollower pivots onto the live camera each frame.
        # ``Model:PivotTo`` propagates to descendants, so a weapon
        # parented anywhere under the rig rides the player's view with
        # no per-weapon follower and no hardcoded offset. Seating order
        # of preference: the Unity "WeaponSlot" object inside the rig →
        # the rig Model itself → (no rig) a one-shot camera placement.
        #
        # Source the prefab from ``workspace`` (the scene-placed instance
        # the converter materialises from Unity's Player.prefab) — NOT
        # from ``ReplicatedStorage.Templates``. The two are structurally
        # different on real conversions: the scene placement carries the
        # full Unity prefab (e.g. SimpleFPS rifle has bipod legs + laser
        # pointer + pod-support, 14 mesh parts, ~50-stud bbox), while
        # the Templates entry the prefab-packages writer emits is a
        # stripped variant (10 parts, ~8-stud bbox). After the same
        # ``ScaleTo`` factor, the Templates clone is ~6× smaller and
        # drops below the camera frustum at the authored slot offset.
        # PR #121's ``c65429b`` introduced a Templates-first lookup that
        # silently selected the smaller variant and made the held weapon
        # invisible -- a regression the user surfaced as "I cannot see
        # the rifle". The fallback case-variant probes the Pascal-case
        # spelling of the same workspace name.
        new_body = (
            f'{matched_header}\n'
            f'    if {mount.sentinel_var} then return end\n'
            f'    local rp = workspace:FindFirstChild("{mount.prefab_name}", true)\n'
            f'        or workspace:FindFirstChild("{alt}", true)\n'
            f'    if not rp then return end\n'
            f'    local rifle = rp:Clone()\n'
            f'    if rifle:IsA("Model") then rifle:ScaleTo({mount.scale_expr}) end\n'
            f'    for _, p in rifle:GetDescendants() do\n'
            f'        if p:IsA("BasePart") then\n'
            f'            p.Transparency = 0\n'
            f'            p.CanCollide = false\n'
            f'            p.Anchored = true\n'
            f'        end\n'
            f'    end\n'
            f'    local rig\n'
            f'    for _, m in workspace:GetDescendants() do\n'
            f'        if m:IsA("Model") and m:GetAttribute("_MainCameraRig") then rig = m break end\n'
            f'    end\n'
            f'    local slot = rig and rig:FindFirstChild("WeaponSlot", true)\n'
            f'    if slot then\n'
            f'        rifle:PivotTo(slot:IsA("Model") and slot:GetPivot() or slot.CFrame)\n'
            f'        rifle.Parent = slot\n'
            f'    elseif rig then\n'
            f'        rifle:PivotTo(rig:GetPivot() * {mount.fallback_offset_expr})\n'
            f'        rifle.Parent = rig\n'
            f'    else\n'
            f'        rifle:PivotTo(workspace.CurrentCamera.CFrame * {mount.fallback_offset_expr})\n'
            f'        rifle.Parent = workspace\n'
            f'    end\n'
            f'    {mount.instance_var} = rifle\n'
        )
        s.source = s.source[: m.start()] + new_body + s.source[m.start(3):]

    return s.source != before


def _append_pickup_touched_block(s: "RbxScript") -> None:
    if _PICKUP_TOUCHED_MARKER in s.source:
        return
    return_m = re.search(r"^return\b", s.source, re.MULTILINE)
    if return_m:
        s.source = (
            s.source[: return_m.start()]
            + _PICKUP_TOUCHED_CODE
            + s.source[return_m.start():]
        )
    else:
        s.source = s.source.rstrip() + "\n" + _PICKUP_TOUCHED_CODE


@patch_pack(
    name="fps_weapon_mount_inject",
    description="Inject a working FPS weapon-mount system into Player "
    "scripts that the AI transpiler emits as a stub. Driven by the "
    "``WEAPON_MOUNTS`` registry — adding a second weapon means appending "
    "one tuple, not writing new pack code.",
    detect=_detect_fps_weapon_mount,
)
def _inject_fps_weapon_mounts(scripts: list["RbxScript"]) -> int:
    """For each script, apply every registered weapon mount whose
    markers fire, then emit the mount-agnostic pickup-Touched block once.

    Each mount idempotency-guards on its own ``-- _FPS_<tag>`` marker;
    the pickup-Touched block guards on the leading comment as its marker.

    Steps per mount:
    1. Find the prefab in workspace (camelCase or PascalCase variant)
    2. Clone, scale, make visible (anchored parts)
    3. Seat the clone into the converted Unity camera rig — the
       ``_MainCameraRig`` Model the auto-injected CameraRigFollower
       pivots onto the live camera, so the weapon rides the view with
       no per-weapon follower and no hardcoded offset
    """
    fixes = 0
    for s in scripts:
        mutated = False
        for mount in WEAPON_MOUNTS:
            if _apply_weapon_mount(s, mount):
                mutated = True
                log.info(
                    "  Injected FPS weapon mount %r in %r",
                    mount.marker_tag, s.name,
                )
        if mutated:
            _append_pickup_touched_block(s)
            fixes += 1
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

    Detector is intentionally name-agnostic — the previous gate reused
    the FPS weapon-mount detector and bound this to FPS-rifle projects
    only, but pickup patterns appear in any genre.
    """
    for s in scripts:
        if s.name != "Pickup":
            continue
        src = s.source or ""
        if _PICKUP_SETATTRIBUTE_RE.search(src):
            return True
        # Direct-RemoteEvent shape: fires PickupItemEvent but doesn't
        # write the server-side has-attribute. The apply function
        # injects the missing write. ``_PICKUP_HAS_ATTR_INJECTED_RE``
        # matches the EXACT dynamic-concat shapes the pack emits (the
        # legacy ``SetAttribute("has" .. itemName, true)`` literal and
        # the current ``_flag = "has" .. itemName`` local) — checking
        # for any ``SetAttribute("has"...)`` would false-skip Pickups
        # that init unrelated has-flags (e.g. an opening-state
        # ``SetAttribute("hasKey", false)``) but never write the
        # dynamic-concat shape that mirrors what the pack would inject.
        if (
            "PickupItemEvent" in src
            and "FireClient" in src
            and not _PICKUP_HAS_ATTR_INJECTED_RE.search(src)
        ):
            return True
    return False


# The server-attr write the pack injects, in both shapes it has emitted:
#   - legacy literal: ``<recv>:SetAttribute("has" .. itemName, true)``
#   - current ``_flag`` local: ``local _flag = "has" .. itemName`` followed
#     by ``<recv>:SetAttribute(_flag, true)``
# Matching the ``"has" .. itemName`` concat directly (rather than any
# ``SetAttribute("has"...)``) avoids false-skipping Pickups that init
# unrelated has-flags before firing. The ``_flag = "has" .. itemName``
# assignment is the unique marker of the current rewrite/inject output;
# without this alternative the guard never recognizes an already-converted
# Pickup, so re-running the pack appends duplicate ``has<X>`` blocks.
_PICKUP_HAS_ATTR_INJECTED_RE = re.compile(
    r':\s*SetAttribute\s*\(\s*"has"\s*\.\.\s*itemName\s*,\s*true\s*\)'
    r'|'
    r'_flag\s*=\s*"has"\s*\.\.\s*itemName'
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
    "Also injects server-side ``character:SetAttribute('has'..itemName, true)`` "
    "AND ``player:SetAttribute('has'..itemName, true)`` before FireClient so "
    "server scripts can read replicated gameplay flags from whichever container "
    "they have in scope — Door's Touched handler has the character Model; "
    "HUD scripts may only have the Player ref.",
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
                '\t\t\t-- Persist the pickup as a server-side attribute so server scripts\n'
                '\t\t\t-- (e.g. Door checking ``character:GetAttribute("hasKey")``) can\n'
                '\t\t\t-- read it. ``LocalPlayer:SetAttribute`` from the client does NOT\n'
                '\t\t\t-- replicate, so any server consumer of ``hasKey``/``hasRifle`` needs\n'
                '\t\t\t-- the server-side write here. Roblox replicates instance attributes\n'
                '\t\t\t-- set server-side down to every client, so Player.luau\'s client-side\n'
                '\t\t\t-- read still works.\n'
                '\t\t\t--\n'
                '\t\t\t-- Write on BOTH the character Model and the Player Instance so\n'
                '\t\t\t-- consumers can pick whichever container is in scope. Door reads\n'
                '\t\t\t-- the character (touching model is what its Touched handler has);\n'
                '\t\t\t-- HUD-style scripts may only have the Player ref.\n'
                '\t\t\tif itemName and itemName ~= "" then\n'
                '\t\t\t\tlocal _flag = "has" .. itemName\n'
                '\t\t\t\tif _char then _char:SetAttribute(_flag, true) end\n'
                '\t\t\t\tif _pl then _pl:SetAttribute(_flag, true) end\n'
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
        # Set the ``has<X>`` flag on BOTH the character Model and the
        # Player Instance. Door reads the character (touching part's
        # Model ancestor); HUD scripts may only have the Player ref.
        # The legacy version only wrote the Player Instance attribute,
        # which Door never read — key-protected doors stayed locked.
        return (
            f'if {player_var} and itemName and itemName ~= "" then '
            f'local _flag = "has" .. itemName; '
            f'{player_var}:SetAttribute(_flag, true); '
            f'local _ch = {player_var}.Character; '
            f'if _ch then _ch:SetAttribute(_flag, true) end '
            f'end\n\t\t\t'
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
    after=("fps_weapon_mount_inject", "pickup_remote_event_server", "pickup_visual_target"),
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


def _blank_lua_strings_and_comments(source: str) -> str:
    """Return *source* with comments and string-literal contents blanked
    out (replaced with spaces), preserving offsets. Used by token-scanners
    (e.g. ``_touch_callback_ranges``) so a Luau block keyword appearing
    inside a string literal — ``error("function call failed")``,
    ``warn("expected end of input")`` — doesn't corrupt depth tracking.

    Handles:
      * Quoted single-line strings (``"..."`` / ``'...'``) with backslash
        escapes; an unterminated string stops at the newline.
      * Long-bracket strings (``[[...]]`` / ``[==[ ... ]==]``).
      * Long-bracket comments (``--[[...]]`` / ``--[==[ ... ]==]``).
      * Line comments (``-- ...``).

    The output has the same length as *source* and the same newline
    positions, so character offsets remain valid.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        # Line / long-bracket comments first so ``--[[`` isn't read as
        # a string ``[[``.
        if source.startswith("--", i):
            if source.startswith("--[", i):
                j = i + 3
                eq_count = 0
                while j < n and source[j] == "=":
                    eq_count += 1
                    j += 1
                if j < n and source[j] == "[":
                    close = "]" + "=" * eq_count + "]"
                    end = source.find(close, j + 1)
                    end = n if end == -1 else end + len(close)
                    for c in source[i:end]:
                        out.append("\n" if c == "\n" else " ")
                    i = end
                    continue
            j = source.find("\n", i)
            if j == -1:
                out.append(" " * (n - i))
                i = n
            else:
                out.append(" " * (j - i))
                i = j
            continue
        if ch == "[":
            j = i + 1
            eq_count = 0
            while j < n and source[j] == "=":
                eq_count += 1
                j += 1
            if j < n and source[j] == "[":
                close = "]" + "=" * eq_count + "]"
                end = source.find(close, j + 1)
                end = n if end == -1 else end + len(close)
                # Keep the brackets but blank the contents so the scanner
                # can still see the boundary characters as non-keywords.
                content_start = j + 1
                content_end = end - len(close) if end < n else n
                out.append(source[i:content_start])
                for c in source[content_start:content_end]:
                    out.append("\n" if c == "\n" else " ")
                out.append(source[content_end:end])
                i = end
                continue
        if ch in ('"', "'"):
            quote = ch
            out.append(ch)
            j = i + 1
            while j < n:
                if source[j] == "\\" and j + 1 < n:
                    out.append("  ")
                    j += 2
                    continue
                if source[j] == quote:
                    out.append(source[j])
                    j += 1
                    break
                if source[j] == "\n":
                    break
                out.append("\n" if source[j] == "\n" else " ")
                j += 1
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


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
    the matching end of each callback. Strings and comments are blanked
    via :func:`_blank_lua_strings_and_comments` so block keywords inside
    string literals (e.g. ``error("expected end")``) don't corrupt the
    depth count.
    """
    ranges: list[tuple[int, int, str]] = []
    scan_source = _blank_lua_strings_and_comments(source)
    for header in _TOUCH_CALLBACK_RE.finditer(scan_source):
        var = header.group(1)
        # Body starts right after the closing ``)`` of ``function(VAR)``.
        header_close = scan_source.find(')', header.end())
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
        while pos < len(scan_source):
            open_m = _LUA_BLOCK_OPEN_RE.search(scan_source, pos)
            end_m = _LUA_END_RE.search(scan_source, pos)
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

# Match the helper definition for any "does the player hold X" probe.
# The AI transpiler emits at least three variants:
#   * ``playerHasKey(playerInstance)``  — Unity-style helper, two-arg
#   * ``playerHasKey()``                — bare/no-arg
#   * ``getPlayerHasKey()``             — get-prefixed accessor
# The earlier regex anchored the name at ``player[Hh]as``, which
# silently rejected the ``getPlayerHas*`` shape and the door bug
# survived (door never opened in PR #121's fresh-transpile validation).
# ``(?:get)?[pP]layer[Hh]as\w+`` covers all three; the ``\w*`` for the
# parameter list still tolerates zero args.
_DOOR_MODULE_PLAYER_HELPER_RE = re.compile(
    r"local function (?P<fn>(?:get)?[pP]layer[Hh]as\w+)\s*\(\s*\w*\s*\)"
    r"(?P<body>.*?)"
    r"^end$",
    re.DOTALL | re.MULTILINE,
)


def _detect_door_module_player_lookup(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Door":
            continue
        src = s.source or ""
        # Cheap detector: any ``playerHas*`` OR ``getPlayerHas*`` helper
        # paired with one of three Player-resolution shapes the AI
        # transpiler picks between:
        #   1. ``getPlayerMod()`` helper
        #   2. ``PlayerScripts.Player`` lookup
        #   3. ``script.Parent:FindFirstChild("Player")`` sibling-module
        #      ``require`` (the third shape the AI emits more recently;
        #      previously the detector missed it because of the
        #      ``"playerHas"`` substring being case-sensitive against
        #      ``getPlayerHasKey``).
        # ``casefold()`` lets us catch both ``playerHas`` and
        # ``PlayerHas`` without enumerating every spelling.
        lower = src.casefold()
        helper_hit = "playerhas" in lower
        resolution_hit = (
            "getplayermod" in lower
            or "PlayerScripts" in src
            or 'FindFirstChild("Player")' in src
            or "FindFirstChild('Player')" in src
        )
        if helper_hit and resolution_hit:
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
        original = s.source or ""
        src = original

        # Step 1 — rewrite the helper body. The helper becomes a
        # ``playerHasX(_part)`` that walks up from the touched part to
        # the attribute the Pickup pack writes (on both the character
        # Model and the Player instance, server-side — both replicate).
        # Normalising every shape to a single ``_part`` parameter lets a
        # zero-parameter caller pass the touch part directly (Step 2).
        rewritten: dict[str, str] = {}  # fn name -> attr name

        def _replace(m: "re.Match[str]") -> str:
            fn_name = m.group("fn")
            body = m.group("body")
            # Skip helpers that don't reference one of the three
            # broken Player-resolution shapes -- they're already correct.
            if (
                "getPlayerMod" not in body
                and "PlayerScripts" not in body
                and 'FindFirstChild("Player")' not in body
                and "FindFirstChild('Player')" not in body
            ):
                return m.group(0)
            # Derive attribute name from the function name:
            #   ``playerHasKey``    -> ``hasKey``    (strip leading ``player``)
            #   ``getPlayerHasKey`` -> ``hasKey``    (strip leading ``getPlayer``)
            # Use a case-insensitive regex so both ``Player`` and
            # ``player`` spellings are handled without hardcoding offsets.
            import re as _re
            suffix_match = _re.match(
                r"^(?:get)?[pP]layer([Hh]as\w+)$", fn_name,
            )
            if suffix_match:
                suffix = suffix_match.group(1)
                # ``HasKey`` -> ``hasKey`` (camelCase attr)
                attr = suffix[0].lower() + suffix[1:]
            else:
                attr = "hasKey"
            rewritten[fn_name] = attr
            return (
                f"local function {fn_name}(_part)\n"
                f"    -- Pickup (a server Script) writes ``{attr}`` server-side\n"
                f"    -- on the touching player's character Model AND Player\n"
                f"    -- instance — both replicate. Door runs server-side; walk\n"
                f"    -- up from the touched part to find that authoritative flag.\n"
                f"    local _n = _part\n"
                f"    while _n do\n"
                f"        if _n:GetAttribute({attr!r}) == true then return true end\n"
                f"        _n = _n.Parent\n"
                f"    end\n"
                f"    return false\n"
                f"end"
            )

        src = _DOOR_MODULE_PLAYER_HELPER_RE.sub(_replace, src)

        # Step 2 — empty-paren call sites (``playerHasKey()``) have no
        # part to read from. Thread the enclosing handler's first
        # parameter (the touched part) into each. Calls that already
        # pass an argument are left alone — the walk-up body handles a
        # part, character, or Player instance equally.
        for fn_name in rewritten:
            empty_call = re.compile(r"\b" + re.escape(fn_name) + r"\s*\(\s*\)")

            def _thread_arg(cm: "re.Match[str]", _fn: str = fn_name) -> str:
                preceding = src[: cm.start()]
                enclosing = None
                for fm in re.finditer(
                    r"function\s*[\w.:]*\s*\(\s*([A-Za-z_]\w*)", preceding
                ):
                    enclosing = fm
                arg = enclosing.group(1) if enclosing else "_part"
                return f"{_fn}({arg})"

            src = empty_call.sub(_thread_arg, src)

        if src != original:
            s.source = src
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

# Capture the H/h prefix separately so we can distinguish the AI's
# capital-Has emission (``HasKey``) from our own lowercase rewrite
# (``hasKey``). Group 2 is the leading H/h; group 3 is the suffix word.
_DOOR_DIRECT_ATTR_RE = re.compile(
    r'([a-zA-Z_]\w*)\s*:\s*GetAttribute\(\s*"([Hh])as(\w+)"\s*\)'
)

# Variables already holding a Player instance — do NOT wrap these in
# ``GetPlayerFromCharacter(...)``. The AI sometimes resolves Player itself
# via a ``getPlayerFromPart`` helper before reaching the attribute read.
_PLAYER_VARIABLE_NAMES = frozenset({"player", "plr", "_p"})


def _detect_door_direct_character_attribute(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Door":
            continue
        src = s.source or ""
        # Only fire when the attribute name carries the AI's capital ``Has``
        # prefix (``HasKey``). Lowercase ``hasX`` reads are the canonical
        # post-rewrite shape and must not be rematched — otherwise the pack
        # would re-wrap its own output on every coherence pass and produce
        # nested ``GetPlayerFromCharacter(player)`` IIFEs that always return
        # nil (the door never opens even with the key).
        for m in _DOOR_DIRECT_ATTR_RE.finditer(src):
            if m.group(2) == "H":
                return True
    return False


@patch_pack(
    name="door_direct_character_attribute",
    description="Rewrite ``<char>:GetAttribute('HasX')`` direct reads in "
    "Door scripts to derive the Player instance from the character and "
    "read the lowercase ``hasX`` attribute the Pickup coherence pack "
    "writes server-side. Door runs server-side; character attributes "
    "set by client LocalScripts don't replicate, so the read returns "
    "nil and the door never opens. Detect only matches the AI-emitted "
    "capital-Has shape; rewrites use lowercase ``has`` so the pack is "
    "idempotent (won't double-wrap on a second coherence pass).",
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
            h_prefix = m.group(2)
            suffix = m.group(3)
            # Only rewrite the AI's capital-Has emission. Lowercase ``hasX``
            # is the canonical post-rewrite shape — leave it alone so the
            # pack is idempotent.
            if h_prefix != "H":
                return m.group(0)
            attr = "has" + suffix
            if char_var in _PLAYER_VARIABLE_NAMES:
                # Variable already holds a Player; only the attribute name
                # is wrong (Pickup writes lowercase ``hasX``). Wrapping
                # this in ``GetPlayerFromCharacter(player)`` would feed a
                # Player where a Character is expected and produce nil.
                return f'{char_var}:GetAttribute("{attr}") == true'
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
# Pack: turret_trigger_stay_polling_v2
# ---------------------------------------------------------------------------
#
# Sibling of ``trigger_stay_polling`` for the AI's newer Turret emission
# shape. The older pack expects ``getTBase`` / ``triggerCollider`` /
# ``startEngaged`` / ``getForward(...)`` / ``model`` helpers — names a
# different generation of the AI transpile used. Today's emission produces
# ``tBase`` (variable), ``engagedUpdate(target)`` (function), ``container``,
# and uses ``getCFrame(obj).LookVector`` instead of a ``getForward`` helper.
# Without re-detection on the new shape, Touched only fires on initial
# contact and a rotating Default-state turret never engages a player who
# was outside the cone when they first touched the trigger.

def _detect_turret_trigger_stay_polling_v2(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Turret":
            continue
        src = s.source or ""
        if "_AutoFpsTurretPoll" in src:
            return False
        # All four signals identify the new turret AI emission shape.
        if (
            "engagedUpdate" in src
            and "isPlayerPart" in src
            and "vectorAngle" in src
            and "State.Engaged" in src
            and "tBase" in src
        ):
            return True
    return False


@patch_pack(
    name="turret_trigger_stay_polling_v2",
    description=(
        "Append a Heartbeat poll to Turret scripts emitted in the newer "
        "AI shape (``tBase``/``engagedUpdate``/``container``). Mirrors "
        "Unity's OnTriggerStay so a rotating turret eventually engages "
        "a target whose first-contact angle was outside the cone."
    ),
    detect=_detect_turret_trigger_stay_polling_v2,
)
def _add_turret_trigger_stay_polling_v2(scripts: list["RbxScript"]) -> int:
    fixes = 0
    poll = (
        "\n\n-- _AutoFpsTurretPoll: Unity OnTriggerStay equivalent. Roblox\n"
        "-- .Touched fires only on initial contact; a rotating turret in the\n"
        "-- Default state needs to re-test the line-of-sight cone every frame\n"
        "-- in case its facing changed since the player touched the trigger.\n"
        "do\n"
        "    local _RunService = game:GetService(\"RunService\")\n"
        "    local _lastCheck = 0\n"
        "    _RunService.Heartbeat:Connect(function()\n"
        "        if state == State.Engaged then return end\n"
        "        if tick() - _lastCheck < 0.15 then return end\n"
        "        _lastCheck = tick()\n"
        "        if not tBase then return end\n"
        "        local _basePos = getPosition(tBase)\n"
        "        local _fwd = getCFrame(tBase).LookVector\n"
        "        local _hits = workspace:GetPartBoundsInRadius(_basePos, sightRadius)\n"
        "        for _, _hit in ipairs(_hits) do\n"
        "            if isPlayerPart(_hit) then\n"
        "                local _char = getCharacterFromTouch(_hit)\n"
        "                if _char then\n"
        "                    local _tp = _char:FindFirstChild(\"HumanoidRootPart\") or _hit\n"
        "                    local _dir = _tp.Position - _basePos\n"
        "                    if vectorAngle(_dir, _fwd) < 55 then\n"
        "                        local _rp = RaycastParams.new()\n"
        "                        _rp.FilterDescendantsInstances = {container}\n"
        "                        _rp.FilterType = Enum.RaycastFilterType.Exclude\n"
        "                        local _res = workspace:Raycast(_basePos, _dir.Unit * sightRadius, _rp)\n"
        "                        if _res and isPlayerPart(_res.Instance) then\n"
        "                            task.spawn(engagedUpdate, _char)\n"
        "                            return\n"
        "                        end\n"
        "                    end\n"
        "                end\n"
        "            end\n"
        "        end\n"
        "    end)\n"
        "end\n"
    )
    for s in scripts:
        if s.name != "Turret":
            continue
        if "_AutoFpsTurretPoll" in s.source:
            continue
        # Same gate as detect — only patch scripts that actually have the
        # required helpers in scope, otherwise the appended block would
        # reference undefined names.
        if not (
            "engagedUpdate" in s.source
            and "isPlayerPart" in s.source
            and "vectorAngle" in s.source
            and "State.Engaged" in s.source
            and "tBase" in s.source
        ):
            continue
        s.source = s.source.rstrip() + poll
        fixes += 1
        log.info("  Added OnTriggerStay polling loop (v2) to '%s'", s.name)
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
    #
    # Track which consumer names look like they need a *Remote*Event
    # (consumer uses ``OnClientEvent`` / ``OnServerEvent``) — we must
    # NOT publish a BindableEvent under those names: BindableEvent has
    # no OnClientEvent, the bridge would silently fail at runtime.
    consumer_names: set[str] = set()
    remote_only_names: set[str] = set()
    for s in scripts:
        src = s.source or ""
        for m in _RS_EVENT_LOOKUP_RE.finditer(src):
            name = m.group(1)
            consumer_names.add(name)
            # If the consumer-side script uses an OnClientEvent /
            # OnServerEvent connect against this name (via the lookup
            # result's variable), flag the name as RemoteEvent-only.
            # Cheap match: any ``OnClientEvent`` / ``OnServerEvent``
            # occurrence in the same script is enough; producers under
            # the same name will be skipped.
            if "OnClientEvent" in src or "OnServerEvent" in src:
                remote_only_names.add(name)

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
                    if cand in remote_only_names:
                        # Consumer expects ``OnClientEvent`` /
                        # ``OnServerEvent``, which BindableEvent does
                        # not implement. Skip the publish — leaving the
                        # producer untouched is safer than wiring a
                        # BindableEvent into a RemoteEvent-shaped
                        # consumer.
                        log.debug(
                            "  Skipping BindableEvent publish for '%s' "
                            "→ '%s' in '%s': consumer uses "
                            "On(Client|Server)Event (RemoteEvent only)",
                            var, cand, s.name,
                        )
                        continue
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
-- Always wires the tween. The earlier ``_hasAnimDriver`` deferral was a
-- false safety: the animation phase's auto-generated ``Anim_*_door_open``
-- driver tweens by an unscaled +4 studs (raw Unity meters, missing
-- STUDS_PER_METER), and its companion ``Anim_*_door_close`` ships a
-- (0,0,0) close offset, so deferring left doors with imperceptible
-- motion (or none at all). Tweening here unconditionally fixes the open
-- motion; the AnimClip drivers' +4 stud overshoot is small enough that
-- co-existence is a non-issue in practice.
do
    local TweenService = game:GetService("TweenService")
    local _doorContainer = script.Parent
    local _parent = _doorContainer and _doorContainer.Parent
    local _doorMesh = _parent and _parent:FindFirstChild("door")
    if _doorMesh and _doorMesh:IsA("BasePart") then
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
# Pack: door_machine_signal_listener
# ---------------------------------------------------------------------------
#
# Unity's Machine.cs opens gated doors via
# ``doors[i].SendMessage("ToggleDoor", true, SendMessageOptions.DontRequireReceiver)``
# — a runtime reflection call into the Door MonoBehaviour. Roblox has no
# SendMessage; the converter translates the Machine side to
# ``door:SetAttribute("ToggleDoor", value)`` on the target Door Model.
# But Door.luau only emits the Touched / ``hasKey`` path — it never
# listens for the ``ToggleDoor`` attribute write. Result: items go onto
# the Machine, ``placeItem`` runs, ``ToggleDoor`` flips, and nothing
# happens. The Machine→Door progression that SimpleFPS is built around
# is silently dead.
#
# Add a listener so the attribute write actually drives the local
# ``toggleDoor(value)`` function (which sets the ``open`` attribute the
# tween/animator picks up). Idempotent via the ``_AutoMachineDoorSignal``
# sentinel comment.

_DOOR_MACHINE_SIGNAL_BLOCK = """
-- _AutoMachineDoorSignal: Machine→Door coherence pack
-- Unity's Machine.cs calls ``doors[i].SendMessage("ToggleDoor", true)`` to
-- progress players past gated doors after they place items on the machine.
-- Machine.luau transpiled that to ``door:SetAttribute("ToggleDoor", value)``
-- on this Door Model. SendMessage doesn't exist in Roblox; the attribute is
-- the idiomatic replacement, but it only works if the receiver listens.
-- Wire that here so the Machine→Door progression works (distinct from the
-- hasKey-on-touch path, which is dormant in scenes without a Key pickup).
do
    local _machineDoorModel = script.Parent and script.Parent.Parent
    if _machineDoorModel then
        _machineDoorModel:GetAttributeChangedSignal("ToggleDoor"):Connect(function()
            local v = _machineDoorModel:GetAttribute("ToggleDoor")
            toggleDoor(v == true)
        end)
    end
end
"""


def _detect_door_machine_signal(scripts: list["RbxScript"]) -> bool:
    """Fire when:
       (a) there is a Door script that emits a local ``toggleDoor`` function
           (the only entry point we can drive),
       (b) at least one OTHER script writes ``:SetAttribute("ToggleDoor", …)``
           on a door instance (Machine.luau in SimpleFPS, but any equivalent
           emitter would match), AND
       (c) the Door script doesn't already carry the ``_AutoMachineDoorSignal``
           sentinel from a prior pass.
    """
    has_door = False
    has_emitter = False
    for s in scripts:
        src = s.source or ""
        if s.name == "Door":
            if "local function toggleDoor" in src and "_AutoMachineDoorSignal" not in src:
                has_door = True
        elif 'SetAttribute("ToggleDoor"' in src or "SetAttribute('ToggleDoor'" in src:
            has_emitter = True
    return has_door and has_emitter


@patch_pack(
    name="door_machine_signal_listener",
    description=(
        "Append a ``GetAttributeChangedSignal('ToggleDoor')`` listener to "
        "Door.luau so the Machine→Door progression works. Machine.luau "
        "writes ``door:SetAttribute('ToggleDoor', value)`` (the Roblox "
        "replacement for Unity's ``doors[i].SendMessage('ToggleDoor', …)``), "
        "but the AI transpile of Door.cs only emits the Touched/hasKey "
        "path. Without this listener, doors never open even after the "
        "player places all required items on the Machine."
    ),
    detect=_detect_door_machine_signal,
    # Run after door_tween_open so the tween block exists when this listener
    # fires toggleDoor (which sets the ``open`` attribute the tween reads).
    after=("door_tween_open",),
)
def _inject_door_machine_signal(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name != "Door":
            continue
        src = s.source or ""
        if "local function toggleDoor" not in src:
            continue
        if "_AutoMachineDoorSignal" in src:
            continue
        s.source = src.rstrip() + "\n" + _DOOR_MACHINE_SIGNAL_BLOCK
        fixes += 1
        log.info("  Wired Machine ToggleDoor signal into '%s'", s.name)
    return fixes


# ---------------------------------------------------------------------------
# Pack: machine_item_check_and_door_lookup
# ---------------------------------------------------------------------------
#
# Unity's Machine.cs opens gated doors by checking the player's hasItems
# list when they enter the machine's trigger and routing items to
# inspector-wired doors[i]. The AI transpiles this into Machine.luau with
# two bugs that together make the door progression silently dead:
#
#  1. ``player:GetAttribute("hasItems")`` reads a comma-separated string —
#     but Player.luau runs as a LocalScript and writes that attribute on
#     LocalPlayer, which DOES NOT replicate to the server. Pickup writes
#     per-item server-side booleans (``hasBattery``, ``hasSmallBattery``,
#     etc.); the Machine has to read those instead.
#  2. ``doorNames`` defaults to ``{"Door1", "Door2"}`` because the scene
#     pipeline doesn't propagate Machine's serialized doors array yet.
#     Real scene names follow Unity's duplicated-instance convention
#     (``"Door (1)"``, ``"Door (2)"``), so the lookup misses every door.
#
# Patch both: rewrite the hasItems iteration to check per-item server
# booleans, and add ``Door (N)`` / ``DoorN`` fallbacks alongside the
# persisted-name lookup. Idempotent via the ``_AutoMachineFix`` sentinel.

_MACHINE_HASITEMS_RE = re.compile(
    r'\n    -- Player\.luau \(LocalScript\) mirrors hasItems[\s\S]*?'
    r'for i = 1, #itemNames do\n'
    r'        local name = itemNames\[i\]\n'
    r'        if name and string\.find\(hasItems, name, 1, true\) then\n'
    r'            placeItem\(i\)\n'
    r'        end\n'
    r'    end\n'
)
_MACHINE_HASITEMS_REPLACEMENT = (
    "\n    -- _AutoMachineFix: Pickup writes per-item server-side ``has<ItemName>``\n"
    "    -- booleans on the Player instance (server SetAttribute replicates).\n"
    "    -- ``hasItems`` is a client-only string maintained by Player.luau and\n"
    "    -- doesn't reach the server, so checking it would always return nil.\n"
    "    for i = 1, #itemNames do\n"
    "        local name = itemNames[i]\n"
    "        if name and player:GetAttribute(\"has\" .. name) == true then\n"
    "            placeItem(i)\n"
    "        end\n"
    "    end\n"
)

_MACHINE_DOOR_LOOKUP_RE = re.compile(
    r'    if number <= #doorNames then\n'
    r'        local door = workspace:FindFirstChild\(doorNames\[number\], true\)\n'
    r'        if door then\n'
    r'            door:SetAttribute\("ToggleDoor", true\)\n'
    r'        end\n'
    r'    end\n'
)
_MACHINE_DOOR_LOOKUP_REPLACEMENT = (
    "    -- _AutoMachineFix: try persisted name list, then Unity-style sibling\n"
    "    -- naming (``Door (N)``), then the literal ``DoorN`` fallback. Without\n"
    "    -- this, the default ``{\"Door1\", \"Door2\"}`` list misses scenes where\n"
    "    -- doors are duplicated prefab instances named ``Door (1)``/``Door (2)``.\n"
    "    local door = workspace:FindFirstChild(doorNames[number] or \"\", true)\n"
    "        or workspace:FindFirstChild(\"Door (\" .. number .. \")\", true)\n"
    "        or workspace:FindFirstChild(\"Door\" .. number, true)\n"
    "    if door then\n"
    "        door:SetAttribute(\"ToggleDoor\", true)\n"
    "    end\n"
)


def _detect_machine_fix(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if s.name != "Machine":
            continue
        src = s.source or ""
        if "_AutoMachineFix" in src:
            return False
        if _MACHINE_HASITEMS_RE.search(src) or _MACHINE_DOOR_LOOKUP_RE.search(src):
            return True
    return False


@patch_pack(
    name="machine_item_check_and_door_lookup",
    description=(
        "Rewrite Machine.luau so its item-progression check reads the "
        "server-replicated ``has<ItemName>`` Pickup booleans instead of "
        "the client-only ``hasItems`` string, and extend the door-name "
        "lookup with ``Door (N)`` / ``DoorN`` fallbacks. Without this, "
        "items placed on the Machine never trigger their gated doors."
    ),
    detect=_detect_machine_fix,
    after=("pickup_remote_event_server",),
)
def _fix_machine_item_check_and_door_lookup(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        if s.name != "Machine":
            continue
        original = s.source or ""
        if "_AutoMachineFix" in original:
            continue
        patched = _MACHINE_HASITEMS_RE.sub(_MACHINE_HASITEMS_REPLACEMENT, original)
        patched = _MACHINE_DOOR_LOOKUP_RE.sub(_MACHINE_DOOR_LOOKUP_REPLACEMENT, patched)
        if patched != original:
            s.source = patched
            fixes += 1
            log.info("  Patched Machine item-check and door-lookup in '%s'", s.name)
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


# ---------------------------------------------------------------------------
# Pack: localscript_api_shim
# ---------------------------------------------------------------------------
#
# Unity allows any MonoBehaviour to call `OtherClass.Method()` on a peer
# class. The AI transpiler translates this as a Luau `require(...)` of the
# peer script — but Roblox's `require` only works on ModuleScripts.
# When the peer is a LocalScript (e.g., the SimpleFPS Player class lives
# in StarterPlayerScripts, and Door/HudControl/Machine try to call
# `Player.hasKey()` / `Player.maxAmmo()`), the require throws
# "Attempted to call require with invalid argument(s)" at runtime and
# the consumer script dies before it wires up any events or triggers.
#
# Two consumer shapes ship from the AI transpiler:
#   Shape A (direct):
#       local Player = require(script.Parent.Player)
#   Shape B (defensive ModuleScript search):
#       local playerModule
#       for _, d in ipairs(game:GetDescendants()) do
#           if d.Name == "Player" and d:IsA("ModuleScript") then
#               playerModule = require(d); break
#           end
#       end
#
# Shape A fails immediately; Shape B silently degrades because no
# matching ModuleScript exists. Both produce the same end-state: every
# `<Target>.<method>()` call returns nil / false / errors.
#
# This pack fixes the bug class generally:
#   1. Detect each LocalScript X with a public-API table (`local X = {}`
#      followed by `function X.<method>(...)` definitions where the
#      script's own name matches X).
#   2. Generate a sibling ``<X>Shared`` ModuleScript under
#      ReplicatedStorage that exposes X's public API for cross-script
#      callers. The shim:
#        - inlines literal constants (`function X.f() return 100 end`),
#        - maps boolean state methods (`function X.hasKey() return
#          gotKey end`) to character-attribute reads, so server and
#          client callers both work,
#        - leaves unknown shapes as stubs returning nil/false (logged).
#   3. In X itself, mirror writes to backing vars into the character
#      attribute that the shim reads. ``gotKey = true`` is followed by
#      ``character:SetAttribute("hasKey", true)`` if a character is
#      bound.
#   4. Rewrite each consumer's ``require(...)`` (both Shape A and B)
#      to require ``<X>Shared`` from ReplicatedStorage instead. Strip
#      Shape B's descendant-loop because it's no longer needed.

_LOCALSCRIPT_SHIM_MARKER = "_AutoLocalScriptShim"
_LOCALSCRIPT_SHIM_MIRROR_MARKER = "_AutoLocalScriptShimMirror"

# `function <Var>.<method>(<args>) return <expr> end` — single-expression
# accessor methods. Captures method name and return expression so we can
# classify each method's body shape.
_API_METHOD_RE = re.compile(
    r'function\s+(?P<var>[A-Za-z_]\w*)\s*\.\s*(?P<method>[A-Za-z_]\w*)'
    r'\s*\(\s*\)\s*'
    r'return\s+(?P<expr>[^\n]+?)\s+end',
)

# Numeric / string / boolean Luau literals (a permissive subset).
_LITERAL_RE = re.compile(
    r'^\s*(?:'
    r'-?\d+(?:\.\d+)?'                # number
    r'|true|false|nil'
    r'|"[^"\n]*"|\'[^\'\n]*\''        # string
    r')\s*$'
)

# Local declaration of an exporter table: `local Player = {}`
_EXPORTER_DECL_RE = re.compile(
    r'^[ \t]*local\s+(?P<var>[A-Za-z_]\w*)\s*=\s*\{\s*\}\s*$',
    re.MULTILINE,
)


@dataclass(frozen=True)
class _ApiMethod:
    name: str          # public method name on the exporter (e.g. "hasKey")
    return_expr: str   # the captured return expression
    backing_var: str | None  # if return_expr is a bare identifier
    literal_value: str | None  # if return_expr is a number/string/bool literal


_LOCAL_LITERAL_DECL_RE = re.compile(
    r'^[ \t]*local\s+(?P<var>[A-Za-z_]\w*)\s*=\s*(?P<value>[^\n]+?)\s*$',
    re.MULTILINE,
)


def _build_local_literal_table(source: str) -> dict[str, str]:
    """Map ``local <var> = <literal>`` declarations to their literal RHS,
    but only when the var is **never reassigned** elsewhere in the
    source — those are true compile-time constants and safe to inline.

    Vars whose declaration RHS happens to be a literal but which get
    written later (e.g., ``local gotKey = false`` followed by
    ``gotKey = true`` inside an item-pickup handler) are mutable state,
    not constants. Inlining them into the shim would freeze the
    cross-script read at the initial value and break the gameplay flow.
    """
    candidates: dict[str, str] = {}
    for m in _LOCAL_LITERAL_DECL_RE.finditer(source):
        value = m.group("value").strip()
        if _LITERAL_RE.match(value):
            candidates[m.group("var")] = value

    out: dict[str, str] = {}
    for var, value in candidates.items():
        # Look for any reassignment that isn't the original `local`
        # declaration. The reassign pattern matches `<var> = ...` not
        # preceded by `local ` on the same line.
        reassign = re.compile(
            r'^[ \t]*(?<!local\s)' + re.escape(var) + r'\s*='
        )
        is_reassigned = False
        for line in source.splitlines():
            if line.lstrip().startswith('local '):
                continue
            if reassign.match(line):
                is_reassigned = True
                break
        if not is_reassigned:
            out[var] = value
    return out


def _is_boolean_state_var(source: str, var: str) -> bool:
    """True iff every assignment to ``var`` (its ``local`` initializer and
    every reassignment) has a boolean-literal RHS (``true`` / ``false``).

    The shim's backing-var accessor is hardcoded to boolean semantics
    (``c:GetAttribute(name) == true``) and the mirror writes the RHS into
    a Roblox attribute — which can only hold value types, never
    Instances. So a bare-identifier accessor (e.g. ``return character``)
    may be lifted into the attribute-backed shim ONLY when the backing
    var is genuinely boolean state. ``return character`` resolves to a
    character Model — mirroring it emits ``SetAttribute(name, <Instance>)``,
    which throws ``Instance is not a supported attribute type`` at runtime.
    """
    assign_re = re.compile(
        r'^[ \t]*(?:local\s+)?' + re.escape(var) + r'\s*=\s*(?P<rhs>[^\n;]+?)\s*$',
        re.MULTILINE,
    )
    saw_assignment = False
    for m in assign_re.finditer(source):
        saw_assignment = True
        if m.group("rhs").strip() not in ("true", "false"):
            return False
    return saw_assignment


def _classify_api(target_src: str, exporter_var: str) -> list[_ApiMethod]:
    """Parse the exporter's `function <Var>.<m>() return <expr> end`
    definitions. Returns one entry per parameterless single-line accessor
    we recognise. Methods that take arguments or have multi-line bodies
    are skipped — those can't be safely lifted into a ModuleScript shim
    because they may close over the LocalScript's runtime state.

    A return of a bare identifier is dereferenced once against the
    module-scope ``local X = <literal>`` table. Unity-style accessors
    typically read a private backing field (``_maxHealth = 100``), so
    one level of indirection is enough to classify constants correctly.
    """
    literal_locals = _build_local_literal_table(target_src)
    out: list[_ApiMethod] = []
    for m in _API_METHOD_RE.finditer(target_src):
        if m.group("var") != exporter_var:
            continue
        method = m.group("method")
        expr = m.group("expr").strip()
        if _LITERAL_RE.match(expr):
            out.append(_ApiMethod(method, expr, None, expr))
        elif re.match(r'^[A-Za-z_]\w*$', expr):
            # Bare identifier: dereference against module-scope literal
            # locals first (covers `return _maxHealth` where _maxHealth
            # is a numeric constant). If the identifier doesn't resolve
            # to a literal, treat it as mutable backing state.
            referenced = literal_locals.get(expr)
            if referenced is not None:
                out.append(_ApiMethod(method, expr, None, referenced))
            elif _is_boolean_state_var(target_src, expr):
                out.append(_ApiMethod(method, expr, expr, None))
            else:
                # Bare identifier holding non-boolean runtime state
                # (e.g. `return character` -> a character Model). A
                # Roblox attribute cannot carry it, so it is NOT a
                # shimmable backing var; fall through to unknown-shape
                # handling (the shim emits `return nil` with a warning).
                out.append(_ApiMethod(method, expr, None, None))
        else:
            # Unknown shape; emit nothing for now. Future work could
            # support `return Var.field` or simple table reads.
            out.append(_ApiMethod(method, expr, None, None))
    return out


def _exporter_var_for_script(s: "RbxScript") -> str | None:
    """Find the local exporter table whose name matches the script's
    own name (e.g. ``local Player = {}`` in Player.luau).

    Restricting to name-matched exporters avoids false positives on
    scripts that happen to declare empty tables for unrelated reasons.
    """
    if not s.source:
        return None
    for m in _EXPORTER_DECL_RE.finditer(s.source):
        if m.group("var") == s.name:
            return s.name
    return None


_REQUIRE_CALL_RE = re.compile(
    r'require\s*\(\s*[^)]+\)'
)


def _consumer_uses_target(consumer_src: str, target_name: str) -> bool:
    """True if the consumer references *target_name* via any of the
    name-resolution shapes the AI transpiler emits:

    Shape A — direct sibling lookup, either field-style or WaitForChild:
        script.Parent.<Name>
        script.Parent:WaitForChild("<Name>")
    Shape B — defensive descendant loop on game:GetDescendants():
        d.Name == "<Name>" and d:IsA("ModuleScript")
    Shape C — bare FindFirstChild / WaitForChild from a service root:
        ReplicatedStorage:FindFirstChild("<Name>", true)
        ServerStorage:WaitForChild("<Name>")

    All of these end up calling ``require(...)`` on a LocalScript, and
    all of them fail at runtime — so any of them is enough to count as
    a consumer for shim-injection purposes.
    """
    if f'script.Parent.{target_name}' in consumer_src:
        return True
    if (
        f'script.Parent:WaitForChild("{target_name}"' in consumer_src
        or f"script.Parent:WaitForChild('{target_name}'" in consumer_src
    ):
        return True
    if (
        f'd.Name == "{target_name}"' in consumer_src
        or f"d.Name == '{target_name}'" in consumer_src
    ):
        return True
    if (
        f'FindFirstChild("{target_name}"' in consumer_src
        or f"FindFirstChild('{target_name}'" in consumer_src
        or f'WaitForChild("{target_name}"' in consumer_src
        or f"WaitForChild('{target_name}'" in consumer_src
    ):
        return True
    return False


def _build_shim_source(target_name: str, api: list[_ApiMethod]) -> str:
    """Generate the `<Target>Shared` ModuleScript source. Constants inline
    as direct method bodies; boolean-state accessors read character
    attributes (with a `character` parameter the caller can pass, falling
    back to LocalPlayer.Character for client-side callers); unknown
    shapes return nil with a one-line warning comment so a human can spot
    them quickly.
    """
    shim_var = f"{target_name}Shared"
    lines: list[str] = [
        f'-- {shim_var} (auto-emitted by {_LOCALSCRIPT_SHIM_MARKER})',
        f'-- Cross-script API for {target_name} (a LocalScript) — '
        f'consumers `require` this shim, not the LocalScript itself.',
        '',
        'local Players = game:GetService("Players")',
        '',
        f'local {shim_var} = {{}}',
        '',
        'local function _resolveCharacter(character)',
        '    if character then return character end',
        '    local lp = Players.LocalPlayer',
        '    return lp and lp.Character or nil',
        'end',
        '',
    ]
    for entry in api:
        if entry.literal_value is not None:
            lines.append(
                f'function {shim_var}.{entry.name}() '
                f'return {entry.literal_value} end'
            )
        elif entry.backing_var is not None:
            lines += [
                f'function {shim_var}.{entry.name}(character)',
                f'    local c = _resolveCharacter(character)',
                f'    return c ~= nil and c:GetAttribute("{entry.name}") == true or false',
                'end',
            ]
        else:
            lines += [
                f'-- WARNING: {target_name}.{entry.name}() body shape '
                f'({entry.return_expr!r}) not auto-lifted; returns nil.',
                f'function {shim_var}.{entry.name}() return nil end',
            ]
    lines.append('')
    lines.append(f'return {shim_var}')
    return '\n'.join(lines) + '\n'


_SHIM_REQUIRE_TEMPLATE = (
    'require(game:GetService("ReplicatedStorage")'
    ':WaitForChild("{shim}"))'
)


# Shape A: `local <Var> = require(<expr containing target_name>)`. We
# scan with paren-depth tracking so nested calls like
# ``require(script.Parent:WaitForChild("Player"))`` rewrite cleanly —
# a flat ``[^)]*?`` regex would stop at the inner close-paren and leave
# the outer require malformed.
_REQUIRE_PREFIX_RE = re.compile(r'(local\s+\w+\s*=\s*)require\s*\(')


def _find_balanced_close(src: str, open_idx: int) -> int | None:
    """Given the index of an opening ``(`` in *src*, return the index
    of the matching ``)`` accounting for nested parens. Returns None if
    unbalanced.
    """
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i
    return None


def _rewrite_shape_a(src: str, target_name: str) -> tuple[str, int]:
    shim_name = f"{target_name}Shared"
    new_require = _SHIM_REQUIRE_TEMPLATE.format(shim=shim_name)
    out_parts: list[str] = []
    cursor = 0
    fixes = 0
    target_re = re.compile(r'\b' + re.escape(target_name) + r'\b')
    while True:
        m = _REQUIRE_PREFIX_RE.search(src, cursor)
        if not m:
            out_parts.append(src[cursor:])
            break
        # Locate the matching close-paren after the `require(`.
        open_idx = m.end() - 1
        close_idx = _find_balanced_close(src, open_idx)
        if close_idx is None:
            # Malformed source — skip past this prefix and keep going.
            out_parts.append(src[cursor:m.end()])
            cursor = m.end()
            continue
        require_inner = src[open_idx + 1:close_idx]
        if not target_re.search(require_inner):
            # Not our target — emit verbatim and advance.
            out_parts.append(src[cursor:close_idx + 1])
            cursor = close_idx + 1
            continue
        out_parts.append(src[cursor:m.start()])
        out_parts.append(m.group(1) + new_require)
        cursor = close_idx + 1
        fixes += 1
    return ''.join(out_parts), fixes


# Shape B: defensive descendant loop. Match the canonical block:
#   for _, d in ipairs(game:GetDescendants()) do
#       if d.Name == "<target>" and d:IsA("ModuleScript") then
#           <var> = require(d); break
#       end
#   end
# Replace with a single require to the shim. We don't try to preserve
# variable name beyond a marker — the consumer typically assigned
# `playerModule` (or similar) just to call `playerModule.X()`; the
# rewrite reassigns the same variable to the shim.
_SHAPE_B_RE = re.compile(
    r'for\s+_\s*,\s*d\s+in\s+ipairs\s*\(\s*game\s*:\s*GetDescendants\s*\(\s*\)\s*\)\s*do\s*\n'
    r'\s*if\s+d\s*\.\s*Name\s*==\s*"(?P<target>[A-Za-z_]\w*)"\s+and\s+'
    r'd\s*:\s*IsA\s*\(\s*"ModuleScript"\s*\)\s+then\s*\n'
    r'\s*(?P<var>[A-Za-z_]\w*)\s*=\s*require\s*\(\s*d\s*\)\s*\n'
    r'\s*break\s*\n'
    r'\s*end\s*\n'
    r'\s*end',
    re.MULTILINE,
)


def _rewrite_shape_b(src: str, target_name: str) -> tuple[str, int]:
    shim_name = f"{target_name}Shared"
    fixes = 0

    def _sub(m: re.Match[str]) -> str:
        nonlocal fixes
        if m.group("target") != target_name:
            return m.group(0)
        fixes += 1
        var = m.group("var")
        return f'{var} = {_SHIM_REQUIRE_TEMPLATE.format(shim=shim_name)}'

    new_src = _SHAPE_B_RE.sub(_sub, src)
    return new_src, fixes


def _mirror_backing_var_assignments(
    source_src: str, var_to_attr: dict[str, str]
) -> tuple[str, int]:
    """For each backing var that maps to an attribute, inject a
    ``character:SetAttribute(<attr>, <value>)`` mirror after every
    top-level-looking assignment to that var. Skips lines already
    carrying the mirror marker so the rewrite is idempotent.
    """
    if not var_to_attr:
        return source_src, 0
    lines = source_src.splitlines(keepends=True)
    out: list[str] = []
    fixes = 0
    # Pre-compile patterns once per backing var.
    var_assign_patterns = {
        var: re.compile(
            r'^(?P<lead>[ \t]*)' + re.escape(var)
            + r'\s*=\s*(?P<value>[^\n;]+?)\s*$'
        )
        for var in var_to_attr
    }
    for line in lines:
        out.append(line)
        if _LOCALSCRIPT_SHIM_MIRROR_MARKER in line:
            continue
        # Strip trailing newline for pattern matching
        stripped = line.rstrip('\n')
        for var, attr in var_to_attr.items():
            m = var_assign_patterns[var].match(stripped)
            if not m:
                continue
            lead = m.group("lead")
            value = m.group("value")
            # Skip if this looks like a function-body return (already
            # covered by accessor) or a local declaration.
            if value.startswith(('function', 'local')):
                continue
            mirror = (
                f'{lead}if character then character:SetAttribute('
                f'"{attr}", {value}) end  -- {_LOCALSCRIPT_SHIM_MIRROR_MARKER}\n'
            )
            out.append(mirror)
            fixes += 1
            break  # one mirror per assigned var per line
    return ''.join(out), fixes


def _detect_localscript_api_shim(scripts: list["RbxScript"]) -> bool:
    """Detector fires when there's at least one LocalScript that exports
    an API and at least one consumer script referencing it.
    """
    exporters = [
        s for s in scripts
        if s.script_type == "LocalScript"
        and _exporter_var_for_script(s) is not None
    ]
    if not exporters:
        return False
    for exporter in exporters:
        for consumer in scripts:
            if consumer is exporter:
                continue
            src = consumer.source or ""
            if (
                f'-- {_LOCALSCRIPT_SHIM_MARKER}' in src
            ):
                # Already shimmed in a prior pack run.
                continue
            if not _consumer_uses_target(src, exporter.name):
                continue
            if _REQUIRE_CALL_RE.search(src):
                return True
    return False


@patch_pack(
    name="localscript_api_shim",
    description="For each LocalScript that exposes a public API table "
    "and is `require()`d by another script, emit a sibling "
    "``<Name>Shared`` ModuleScript under ReplicatedStorage with a "
    "character-attribute-backed implementation, mirror local-state "
    "writes into character attributes inside the source LocalScript, "
    "and rewrite consumer `require(...)` calls to point at the shim. "
    "Fixes the runtime error 'Attempted to call require with invalid "
    "argument(s)' that the AI transpiler produces whenever one Unity "
    "MonoBehaviour calls a peer that lives on the client.",
    detect=_detect_localscript_api_shim,
)
def _inject_localscript_api_shim(scripts: list["RbxScript"]) -> int:
    fixes = 0

    # Discover exporters: name-matched `local X = {}` LocalScripts.
    exporters: list[tuple["RbxScript", str, list[_ApiMethod]]] = []
    for s in scripts:
        if s.script_type != "LocalScript":
            continue
        exporter_var = _exporter_var_for_script(s)
        if not exporter_var:
            continue
        api = _classify_api(s.source or "", exporter_var)
        if not api:
            continue
        exporters.append((s, exporter_var, api))

    if not exporters:
        return 0

    from core.roblox_types import RbxScript

    for exporter_script, exporter_var, api in exporters:
        # Confirm there's at least one consumer requiring this name.
        used_by_consumer = any(
            (c is not exporter_script)
            and _consumer_uses_target(c.source or "", exporter_script.name)
            and _REQUIRE_CALL_RE.search(c.source or "")
            for c in scripts
        )
        if not used_by_consumer:
            continue

        shim_name = f"{exporter_script.name}Shared"

        # 1. Emit (or refresh) the shim ModuleScript. Idempotent: replace
        # source if a stale shim with the same name already exists.
        shim_source = _build_shim_source(exporter_script.name, api)
        existing_shim = next(
            (s for s in scripts if s.name == shim_name), None,
        )
        if existing_shim is None:
            scripts.append(
                RbxScript(
                    name=shim_name,
                    source=shim_source,
                    script_type="ModuleScript",
                    parent_path="ReplicatedStorage",
                )
            )
            fixes += 1
            log.info(
                "  Emitted %s shim ModuleScript with %d method(s)",
                shim_name, len(api),
            )
        elif existing_shim.source != shim_source:
            existing_shim.source = shim_source
            fixes += 1
            log.info("  Refreshed %s shim source", shim_name)

        # 2. Inside the exporter LocalScript, mirror backing-var
        # assignments to character attributes. Only the bool-state
        # accessors have a backing var; constants don't need mirroring.
        var_to_attr: dict[str, str] = {
            entry.backing_var: entry.name
            for entry in api
            if entry.backing_var is not None
        }
        if var_to_attr:
            mirrored, mirror_fixes = _mirror_backing_var_assignments(
                exporter_script.source or "", var_to_attr,
            )
            if mirror_fixes:
                exporter_script.source = mirrored
                fixes += 1
                log.info(
                    "  Mirrored %d backing-var write(s) in %s into character "
                    "attribute(s) %s",
                    mirror_fixes, exporter_script.name,
                    sorted(var_to_attr.values()),
                )

        # 3. Rewrite consumers.
        for consumer in scripts:
            if consumer is exporter_script or consumer.name == shim_name:
                continue
            if not _consumer_uses_target(
                consumer.source or "", exporter_script.name,
            ):
                continue
            src = consumer.source or ""
            new_src, fixes_a = _rewrite_shape_a(src, exporter_script.name)
            new_src, fixes_b = _rewrite_shape_b(new_src, exporter_script.name)
            if fixes_a + fixes_b > 0:
                # Annotate so the detector knows we've already run.
                marker_comment = (
                    f'-- {_LOCALSCRIPT_SHIM_MARKER}: consumer rewritten '
                    f'to use ReplicatedStorage.{shim_name}\n'
                )
                if f'-- {_LOCALSCRIPT_SHIM_MARKER}' not in new_src:
                    new_src = marker_comment + new_src
                consumer.source = new_src
                fixes += 1
                log.info(
                    "  Rewrote %s consumer (shapeA=%d shapeB=%d) to use %s",
                    consumer.name, fixes_a, fixes_b, shim_name,
                )

    return fixes


# ---------------------------------------------------------------------------
# Pack: proximity_trigger_fanout
# ---------------------------------------------------------------------------
#
# Unity ``OnTriggerEnter(Collider other)`` fires when ANY collider enters
# ANY of the GameObject's child colliders. The AI transpiler emits a
# narrowed Roblox equivalent that:
#   1. resolves a single "trigger" Part via ``findTriggerPart(container)``
#      (typically an invisible sphere child), and
#   2. connects ``Touched`` only on that one Part.
#
# Two failure modes follow from this:
#   (a) The player steps on the entity's BODY mesh (Mine, Pickup), not on
#       an invisible trigger sphere — Touched never fires.
#   (b) The handler resolves the character via
#       ``Players:GetPlayerFromCharacter(otherPart.Parent)``, which is
#       nil when the touching part is an Accessory/Tool descendant of the
#       character (R15 hats, gear, etc.) — false negatives even when the
#       trigger sphere does fire.
#
# This pack rewrites the narrowed pattern into a multi-part fanout:
#   * connect Touched on every BasePart in the container (or on the
#     container itself if it's a BasePart), and
#   * use ``otherPart:FindFirstAncestorWhichIsA("Model")`` for the
#     character lookup.
#
# Triggers any "step on it to activate" Unity entity (mines, pickups,
# pressure plates) regardless of script name — generalising the original
# Mine.luau-specific hack to the broader class.

_PROXIMITY_TRIGGER_MARKER = "_AutoProximityTriggerFanout"

# Multi-line match:
#   local <var> = findTriggerPart(<container>)
#   if <var> then
#       <var>.Touched:Connect(function(<arg>)
#           ...body...
#       end)
#       ...rest (e.g. a sibling <var>.TouchEnded:Connect block)...
#   end
#
# ``body`` is captured non-greedily and stops at the ``.Touched``
# connect's OWN ``end)``. ``rest`` then captures everything else inside
# the ``if`` block — crucially any sibling handler like
# ``<var>.TouchEnded:Connect(...)`` — up to the ``if``'s closing ``end``
# (at the same indent as the ``local`` line). Earlier the regex required
# the ``if``-``end`` immediately after the ``.Touched`` ``end)``; when a
# door also bound ``.TouchEnded`` the non-greedy ``body`` over-captured,
# swallowing the first ``end)`` and producing a stray ``)`` in the
# rewritten ``local function`` — invalid Luau.
# ``(?:[ \t]*(?:--[^\n]*)?\n)*`` after ``if <var> then`` tolerates blank
# lines and `--` comment lines (the AI commonly emits an
# ``-- OnTriggerEnter ...`` line between the ``if`` and the ``.Touched``
# connect — without skipping it the door's Touched binding never matched
# and the proximity fanout silently dropped Door.luau).
_PROXIMITY_TRIGGER_RE = re.compile(
    r'(?P<lead>^[ \t]*)local\s+(?P<var>[a-zA-Z_]\w*)\s*=\s*findTriggerPart\s*\(\s*'
    r'(?P<container>[a-zA-Z_]\w*)\s*\)\s*\n'
    r'[ \t]*if\s+(?P=var)\s+then\s*\n'
    r'(?:[ \t]*(?:--[^\n]*)?\n)*'
    r'(?P<connect_indent>[ \t]+)(?P=var)\s*\.\s*Touched\s*:\s*Connect\s*\(\s*'
    r'function\s*\(\s*(?P<arg>[a-zA-Z_]\w*)\s*\)\s*\n'
    r'(?P<body>(?:.*?\n)*?)'
    r'(?P=connect_indent)end\s*\)\s*\n'
    r'(?P<rest>(?:.*?\n)*?)'
    r'(?P=lead)end\b',
    re.MULTILINE,
)


def _detect_proximity_trigger_fanout(scripts: list["RbxScript"]) -> bool:
    """Detector fires when ANY script has the narrowed ``findTriggerPart``
    + single-part-Touched binding shape. Marker-guarded for idempotency.
    """
    for s in scripts:
        src = s.source or ""
        if (
            _PROXIMITY_TRIGGER_MARKER not in src
            and _PROXIMITY_TRIGGER_RE.search(src) is not None
        ):
            return True
    return False


def _rewrite_proximity_body(body: str, arg: str) -> str:
    """Rewrite the captured Touched handler body to resolve the touching
    character via ancestor lookup instead of the direct ``.Parent``.

    Unity's ``OnTriggerEnter(Collider other)`` callback gives ``other`` the
    immediate collider on the touching character — but R15 / accessory
    parts in Roblox have the Accessory model as their immediate Parent,
    not the character. Replace the AI's literal translation
    (``other.Parent``) with ``other:FindFirstAncestorWhichIsA("Model")``
    so the lookup also resolves accessory-mounted parts.
    """
    return re.sub(
        rf"Players\s*:\s*GetPlayerFromCharacter\s*\(\s*{re.escape(arg)}\s*\.\s*Parent\s*\)",
        f'Players:GetPlayerFromCharacter({arg}:FindFirstAncestorWhichIsA("Model"))',
        body,
    )


@patch_pack(
    name="proximity_trigger_fanout",
    description="Rewrite single-part ``triggerPart.Touched:Connect(...)`` "
    "patterns into a multi-part Touched fanout over every BasePart in "
    "the container, with ancestor-based character lookup. Fixes "
    "step-on-entity Unity triggers (mines, pickups, pressure plates) "
    "whose visible body geometry was registering touches but whose "
    "invisible AI-stubbed trigger sphere was not.",
    detect=_detect_proximity_trigger_fanout,
)
def _inject_proximity_trigger_fanout(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        src = s.source or ""
        if _PROXIMITY_TRIGGER_MARKER in src:
            continue
        m = _PROXIMITY_TRIGGER_RE.search(src)
        if not m:
            continue
        lead = m.group("lead")
        container = m.group("container")
        arg = m.group("arg")
        var = m.group("var")
        body = m.group("body")
        rest = m.group("rest")
        rewritten_body = _rewrite_proximity_body(body, arg)
        # Preserve the ``local <var> = findTriggerPart(container)`` line
        # at the top of the new block. The captured body often still
        # references the trigger-part local (e.g. ``triggerPart:Find
        # FirstChildWhichIsA("Sound")``); dropping the definition would
        # produce ``nil:Find...`` calls inside _onProximityTouched.
        #
        # ``rest`` carries any sibling handlers found inside the same
        # ``if <var> then`` block (notably a ``<var>.TouchEnded:Connect``
        # binding on doors). It is re-emitted verbatim inside the
        # rebuilt ``if <var> then`` so those handlers — and the ``if``
        # guard they depend on — survive the rewrite.
        replacement = (
            f"{lead}local {var} = findTriggerPart({container})\n"
            f"{lead}-- {_PROXIMITY_TRIGGER_MARKER}: connect Touched on every body\n"
            f"{lead}-- part so step-on triggers (mines/pickups/pressure-plates) fire,\n"
            f"{lead}-- and resolve the touching character via ancestor lookup so\n"
            f"{lead}-- accessory-mounted touches also count. The {var} local and\n"
            f"{lead}-- any sibling handlers (e.g. TouchEnded) are preserved.\n"
            f"{lead}local function _onProximityTouched({arg})\n"
            f"{rewritten_body}"
            f"{lead}end\n"
            f"{lead}if {var} then\n"
            f"{lead}\tif {container}:IsA(\"BasePart\") then\n"
            f"{lead}\t\t{container}.Touched:Connect(_onProximityTouched)\n"
            f"{lead}\t\t-- Also wire Touched on any invisible trigger-volume\n"
            f"{lead}\t\t-- children. The pipeline emits a sibling-style child\n"
            f"{lead}\t\t-- Part (named TriggerZone) when the same GameObject\n"
            f"{lead}\t\t-- mixes a visible mesh with a trigger collider, so\n"
            f"{lead}\t\t-- the original approach-radius detection still fires\n"
            f"{lead}\t\t-- without depending on Touched against the small\n"
            f"{lead}\t\t-- visible mesh.\n"
            f"{lead}\t\tfor _, _tc in ipairs({container}:GetChildren()) do\n"
            f"{lead}\t\t\tif _tc:IsA(\"BasePart\") and not _tc:IsA(\"MeshPart\")\n"
            f"{lead}\t\t\t\tand _tc.Transparency >= 1 then\n"
            f"{lead}\t\t\t\t_tc.Touched:Connect(_onProximityTouched)\n"
            f"{lead}\t\t\tend\n"
            f"{lead}\t\tend\n"
            f"{lead}\telseif {container}:IsA(\"Model\") then\n"
            f"{lead}\t\tfor _, _d in ipairs({container}:GetDescendants()) do\n"
            f"{lead}\t\t\tif _d:IsA(\"BasePart\") then\n"
            f"{lead}\t\t\t\t_d.Touched:Connect(_onProximityTouched)\n"
            f"{lead}\t\t\tend\n"
            f"{lead}\t\tend\n"
            f"{lead}\tend\n"
            f"{rest}"
            f"{lead}end"
        )
        s.source = src[: m.start()] + replacement + src[m.end():]
        fixes += 1
        log.info(
            "  Broadened proximity trigger in '%s' (container=%s arg=%s)",
            s.name, container, arg,
        )
    return fixes


# ---------------------------------------------------------------------------
# Pack: proximity_trigger_fanout_v2_migration
# ---------------------------------------------------------------------------
#
# The original ``proximity_trigger_fanout`` rewrite (v1) only wired
# ``Touched`` on the container itself in the ``container:IsA("BasePart")``
# branch — i.e. it assumed the trigger volume *was* the visible Part.
#
# That assumption broke when the pipeline started emitting a separate
# transparent child ``TriggerZone`` Part for nodes that mix a visible
# mesh with a trigger collider (see ``scene_converter._process_components``).
# After the mesh-vs-trigger fix, the visible Part is mesh-sized; the
# trigger volume lives in a child Part. Scripts running the v1 fanout
# only fire ``Touched`` on the small visible mesh — Unity-style "walk
# near the door" detection regresses to "step directly on the mesh".
#
# v2 fanout adds a child-walk in the BasePart branch that connects
# ``Touched`` on any invisible non-MeshPart child (the TriggerZone).
# This migration upgrades existing v1-rewritten scripts that still ship
# the old shape — detected by presence of the marker + absence of the
# ``_tc`` child-walk loop the v2 emit introduces. Idempotent.

_PROXIMITY_FANOUT_V2_NEEDLE = "for _, _tc in ipairs("

_PROXIMITY_FANOUT_V1_BASEPART_RE = re.compile(
    r'(?P<lead>[ \t]*)if\s+(?P<container>[a-zA-Z_]\w*)\s*:\s*IsA\(\s*["\']BasePart["\']\s*\)\s+then\s*\n'
    r'(?P<inner>[ \t]+)(?P=container)\s*\.\s*Touched\s*:\s*Connect\s*\(\s*_onProximityTouched\s*\)\s*\n',
)


def _detect_proximity_fanout_v2_migration(scripts: list["RbxScript"]) -> bool:
    """Fires on any script with the v1 fanout marker that lacks the v2
    ``_tc`` child-walk needle. Marker without needle == stale shape."""
    for s in scripts:
        src = s.source or ""
        if (
            _PROXIMITY_TRIGGER_MARKER in src
            and _PROXIMITY_FANOUT_V2_NEEDLE not in src
            and _PROXIMITY_FANOUT_V1_BASEPART_RE.search(src) is not None
        ):
            return True
    return False


@patch_pack(
    name="proximity_trigger_fanout_v2_migration",
    description="Upgrade v1 ``proximity_trigger_fanout`` rewrites to "
    "also wire Touched on invisible trigger-volume children "
    "(TriggerZone Parts emitted by the pipeline when a GameObject "
    "mixes a visible mesh with a trigger collider).",
    detect=_detect_proximity_fanout_v2_migration,
)
def _inject_proximity_fanout_v2_migration(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        src = s.source or ""
        if (
            _PROXIMITY_TRIGGER_MARKER not in src
            or _PROXIMITY_FANOUT_V2_NEEDLE in src
        ):
            continue
        m = _PROXIMITY_FANOUT_V1_BASEPART_RE.search(src)
        if not m:
            continue
        inner = m.group("inner")
        container = m.group("container")
        insertion = (
            f"{inner}-- v2: also wire Touched on invisible trigger-volume\n"
            f"{inner}-- children (TriggerZone Parts emitted by the pipeline\n"
            f"{inner}-- when the GameObject mixes a visible mesh + trigger).\n"
            f"{inner}for _, _tc in ipairs({container}:GetChildren()) do\n"
            f"{inner}\tif _tc:IsA(\"BasePart\") and not _tc:IsA(\"MeshPart\")\n"
            f"{inner}\t\tand _tc.Transparency >= 1 then\n"
            f"{inner}\t\t_tc.Touched:Connect(_onProximityTouched)\n"
            f"{inner}\tend\n"
            f"{inner}end\n"
        )
        s.source = src[: m.end()] + insertion + src[m.end():]
        fixes += 1
        log.info("  Migrated proximity fanout v1 -> v2 in '%s'", s.name)
    return fixes


# ---------------------------------------------------------------------------
# Pack: template_clone_visibility
# ---------------------------------------------------------------------------
#
# Unity prefab templates ship from the pipeline parented under
# ``ReplicatedStorage.Templates`` with Transparency=1 on every BasePart
# so the scene viewer doesn't render them. When a runtime script clones
# one of these templates and re-parents the clone into the world (e.g.,
# Player.luau's getRifle clones the Rifle template and attaches it to a
# weapon-slot Part), the cloned BaseParts inherit Transparency=1 and the
# weapon is invisible in-game.
#
# The original WeaponMount pack handled this for the SimpleFPS rifle by
# wholesale-rewriting a stub-shaped ``GetRifle = function() ... end``
# body. As the AI's transpile output drifted to use a ``cloneTemplate``
# helper plus a ``function getRifle()`` statement, the WeaponMount
# detector stopped matching and visible-rifle setup silently dropped.
#
# This pack is narrower and shape-tolerant: it splices a visibility +
# weld fixup right after any line that clones a Template descendant and
# re-parents it. The fixup:
#   - sets Transparency=0, CanCollide=false, Massless=true on every
#     BasePart in the clone,
#   - welds non-primary parts to the model's PrimaryPart,
#   - if a destination Part variable is detected (the script reparents
#     the clone into a known weapon-slot-style holder), welds the
#     primary to that holder so the clone rides along when the holder
#     moves.
#
# Idempotent via the ``_AutoTemplateCloneVisibility`` marker.

_TEMPLATE_CLONE_VISIBILITY_MARKER = "_AutoTemplateCloneVisibility"

# Match `local <var> = cloneTemplate("<NAME>")` — the AI's helper for
# pulling a prefab clone out of ReplicatedStorage.Templates. Also match
# the equivalent inline lookup `local <var> = Templates:FindFirstChild
# ("<NAME>"):Clone()` so future AI shapes don't slip past.
_CLONE_TEMPLATE_RE = re.compile(
    r'^(?P<lead>[ \t]*)local\s+(?P<var>[A-Za-z_]\w*)\s*=\s*'
    r'(?:'
    r'cloneTemplate\s*\(\s*["\'][A-Za-z_][\w]*["\']\s*\)'
    r'|'
    r'[A-Za-z_]\w*\s*:\s*FindFirstChild\s*\(\s*["\'][A-Za-z_][\w]*["\']\s*\)\s*:\s*Clone\s*\(\s*\)'
    r')\s*$',
    re.MULTILINE,
)


def _detect_template_clone_visibility(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        src = s.source or ""
        if _TEMPLATE_CLONE_VISIBILITY_MARKER in src:
            continue
        if _CLONE_TEMPLATE_RE.search(src):
            return True
    return False


def _has_visibility_already(src: str, var: str, start: int) -> bool:
    """Heuristic: avoid double-inserting if a Transparency=0 assignment
    on the clone's parts already appears within ~30 lines of the clone
    line. Catches manual fixups so the pack doesn't pile on.
    """
    window = src[start:start + 1500]
    if 'Transparency = 0' in window and var in window:
        return True
    return False


def _emit_visibility_block(lead: str, var: str) -> str:
    """Generate the visibility + weld fixup block. Tolerates both Model
    and bare BasePart clones — ``var:IsA`` branches handle each.
    """
    indent = lead
    return (
        f"{indent}-- {_TEMPLATE_CLONE_VISIBILITY_MARKER}: make the cloned template visible\n"
        f"{indent}-- (template parts ship Transparency=1) and weld sub-parts together.\n"
        f"{indent}do\n"
        f"{indent}\tlocal _clone = {var}\n"
        f"{indent}\tif _clone then\n"
        f"{indent}\t\tlocal _primary = nil\n"
        f"{indent}\t\tif _clone:IsA(\"BasePart\") then\n"
        f"{indent}\t\t\t_primary = _clone\n"
        f"{indent}\t\telseif _clone:IsA(\"Model\") then\n"
        f"{indent}\t\t\t_primary = _clone.PrimaryPart or _clone:FindFirstChildWhichIsA(\"BasePart\")\n"
        f"{indent}\t\t\tif _clone.PrimaryPart == nil and _primary then\n"
        f"{indent}\t\t\t\t_clone.PrimaryPart = _primary\n"
        f"{indent}\t\t\tend\n"
        f"{indent}\t\tend\n"
        f"{indent}\t\tlocal _parts = (_clone:IsA(\"Model\")) and _clone:GetDescendants() or {{_clone}}\n"
        f"{indent}\t\tfor _, _p in ipairs(_parts) do\n"
        f"{indent}\t\t\tif _p:IsA(\"BasePart\") then\n"
        f"{indent}\t\t\t\t_p.Transparency = 0\n"
        f"{indent}\t\t\t\t_p.CanCollide = false\n"
        f"{indent}\t\t\t\t_p.Massless = true\n"
        f"{indent}\t\t\t\tif _primary and _p ~= _primary then\n"
        f"{indent}\t\t\t\t\tlocal _w = Instance.new(\"WeldConstraint\")\n"
        f"{indent}\t\t\t\t\t_w.Part0 = _primary\n"
        f"{indent}\t\t\t\t\t_w.Part1 = _p\n"
        f"{indent}\t\t\t\t\t_w.Parent = _p\n"
        f"{indent}\t\t\t\tend\n"
        f"{indent}\t\t\tend\n"
        f"{indent}\t\tend\n"
        f"{indent}\tend\n"
        f"{indent}end\n"
    )


@patch_pack(
    name="template_clone_visibility",
    description="After every ``local x = cloneTemplate(...)`` or "
    "``local x = Templates:FindFirstChild(...):Clone()`` line, splice "
    "a fixup that sets Transparency=0 / CanCollide=false on each "
    "BasePart of the clone and welds non-primary parts to the model's "
    "PrimaryPart. Prevents invisible-clone bugs that happen when the "
    "template (hidden in the scene with Transparency=1) is cloned and "
    "re-parented into the world without per-part visibility resets.",
    detect=_detect_template_clone_visibility,
)
def _inject_template_clone_visibility(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        src = s.source or ""
        if _TEMPLATE_CLONE_VISIBILITY_MARKER in src:
            continue
        # Iterate over all clone lines, accumulating insertions from end
        # to start so earlier insertions don't shift later match offsets.
        matches = list(_CLONE_TEMPLATE_RE.finditer(src))
        if not matches:
            continue
        for m in reversed(matches):
            lead = m.group("lead")
            var = m.group("var")
            line_end = src.find('\n', m.end())
            if line_end < 0:
                line_end = len(src)
            else:
                line_end += 1
            if _has_visibility_already(src, var, line_end):
                continue
            block = _emit_visibility_block(lead, var)
            src = src[:line_end] + block + src[line_end:]
            fixes += 1
        if s.source != src:
            s.source = src
            log.info(
                "  Inserted template-clone visibility fixup(s) in '%s'",
                s.name,
            )
    return fixes


# ---------------------------------------------------------------------------
# Pack: fps_camera_pitch_inversion
# ---------------------------------------------------------------------------
#
# Unity's ``Input.GetAxis("Mouse Y")`` is positive-UP; Roblox's
# ``UserInputService:GetMouseDelta().Y`` is positive-DOWN. A correct FPS
# pitch combines two sign decisions with OPPOSITE polarity:
#   - the mouse-delta accumulation: ``pitch = pitch +/- d.Y * k``
#   - the camera application:        ``CFrame.Angles(+/- pitch, ...)``
# Correct vertical look needs these two signs to disagree (e.g. the
# canonical ``pitch = pitch - d.Y`` paired with ``CFrame.Angles(pitch)``,
# which the transpiler prompt teaches).
#
# The AI transpiler sometimes emits AGREEING signs — observed shape:
# ``pitch = pitch - d.Y`` together with ``CFrame.Angles(math.rad(-pitch))``
# — which inverts vertical look (pushing the mouse forward tilts the view
# down). Yaw is unaffected because it has no second negation.
#
# Fix: when the two signs agree, flip the MOUSE-delta line's sign. The
# camera ``-pitch`` form is left intact on purpose — recoil kicks
# (``pitch = pitch - 2``) and asymmetric pitch clamps are authored
# against that convention, so flipping the camera term would break them.

# Matches the mouse-delta pitch accumulation: ``<pv> = <pv> +/- <x>.Y``.
# An optional ``math.clamp(`` wrapper is tolerated so the inline-clamp
# shape (``pv = math.clamp(pv - d.Y * k, lo, hi)``) is handled too. The
# match deliberately stops at ``.Y`` so any trailing ``* SENSITIVITY``
# survives untouched.
_PITCH_ACCUM_RE = re.compile(
    r'^(?P<lead>[ \t]*)(?P<pv>[A-Za-z_]\w*)\s*=\s*'
    r'(?P<wrap>math\.clamp\(\s*)?'
    r'(?P=pv)\s*(?P<sign>[+-])\s*(?P<delta>[A-Za-z_]\w*\s*\.\s*Y)\b',
    re.MULTILINE,
)


def _camera_pitch_sign_is_negated(src: str, pv: str) -> bool | None:
    """Return True if the camera applies ``-<pv>`` inside a
    ``CFrame.Angles`` call, False if it applies ``+<pv>``, None if no
    such application is found.
    """
    m = re.search(
        r'CFrame\.Angles\s*\(\s*(?:math\.rad\s*\(\s*)?(?P<cs>-?)\s*'
        + re.escape(pv) + r'\b',
        src,
    )
    if m is None:
        return None
    return m.group('cs') == '-'


def _pitch_vars_to_flip(src: str) -> set[str]:
    """Pitch vars whose mouse-delta sign AGREES with the camera
    application sign — the inverted combination."""
    out: set[str] = set()
    for m in _PITCH_ACCUM_RE.finditer(src):
        pv = m.group('pv')
        negated = _camera_pitch_sign_is_negated(src, pv)
        if negated is None:
            continue
        mouse_is_negative = m.group('sign') == '-'
        if mouse_is_negative == negated:
            out.add(pv)
    return out


def _detect_fps_camera_pitch_inversion(scripts: list["RbxScript"]) -> bool:
    for s in scripts:
        if _pitch_vars_to_flip(s.source or ""):
            return True
    return False


@patch_pack(
    name="fps_camera_pitch_inversion",
    description="Flip the mouse-delta pitch accumulation sign when an "
    "FPS controller pairs it with a same-sign camera CFrame.Angles "
    "application. Roblox GetMouseDelta().Y is positive-down (vs Unity's "
    "positive-up Mouse Y axis); a same-sign pairing inverts vertical "
    "look so pushing the mouse forward tilts the view down.",
    detect=_detect_fps_camera_pitch_inversion,
)
def _fix_fps_camera_pitch_inversion(scripts: list["RbxScript"]) -> int:
    fixes = 0
    for s in scripts:
        src = s.source or ""
        to_flip = _pitch_vars_to_flip(src)
        if not to_flip:
            continue

        def _flip(m: "re.Match[str]") -> str:
            pv = m.group('pv')
            if pv not in to_flip:
                return m.group(0)
            new_sign = '+' if m.group('sign') == '-' else '-'
            return (
                f"{m.group('lead')}{pv} = "
                f"{m.group('wrap') or ''}{pv} {new_sign} {m.group('delta')}"
            )

        new_src = _PITCH_ACCUM_RE.sub(_flip, src)
        if new_src != src:
            s.source = new_src
            fixes += 1
            log.info(
                "  Fixed inverted FPS camera pitch in '%s' (vars: %s)",
                s.name, sorted(to_flip),
            )
    return fixes
