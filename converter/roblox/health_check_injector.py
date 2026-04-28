"""
Inject smoke-test health-check Scripts into an existing .rbxlx file.

Two scripts are injected:

* A server-side ``Script`` in ``ServerScriptService`` that counts instances,
  inspects terrain (extents + water voxels), counts Animator / Animation
  instances, and captures script errors via ``ScriptContext.Error``.
* A client-side ``LocalScript`` in ``StarterPlayer.StarterPlayerScripts`` that
  forces first-person, snapshots ``Camera.CFrame`` and ``HumanoidRootPart.Position``
  before and after a 12 s input window (during which the smoke test driver
  pumps simulated WASD + mouse events from osascript / CoreGraphics), and
  reports whether the camera moved, the player moved, and whether default
  character animations are wired up.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

HEALTH_CHECK_SCRIPT_NAME = "__SmokeTestHealthCheck"
CLIENT_HEALTH_CHECK_SCRIPT_NAME = "__SmokeTestClientHealthCheck"

# Total time the server script waits before emitting [SMOKE_TEST_RESULT] / [SMOKE_TEST_DONE].
# Must outlast the client-side input window (12 s) plus character spawn (~2 s)
# plus a safety margin.
SETTLE_SECONDS = 30

HEALTH_CHECK_LUAU = r"""
-- Auto-injected by unity2rbxlx smoke_test (server)
-- Do NOT edit manually; this script is removed after the test run.

local ScriptContext = game:GetService("ScriptContext")
local HttpService = game:GetService("HttpService")
local Workspace = game:GetService("Workspace")

print("[SMOKE_TEST_START]")

-- Capture script errors as they happen
local errors = {}
ScriptContext.Error:Connect(function(message, stackTrace, script)
    table.insert(errors, {
        message = message,
        script = script and script:GetFullName() or "unknown",
    })
    if #errors <= 20 then
        print("[SMOKE_TEST_ERROR] " .. (script and script:GetFullName() or "?") .. ": " .. message)
    end
end)

-- Wait for the game to settle (scripts to run, meshes to load, client window to close)
local SETTLE_SECONDS = __SETTLE_SECONDS__
task.wait(SETTLE_SECONDS)

-- Count instances by ClassName
local counts = {}
local totalInstances = 0
for _, desc in ipairs(game:GetDescendants()) do
    local cn = desc.ClassName
    counts[cn] = (counts[cn] or 0) + 1
    totalInstances = totalInstances + 1
end

-- Terrain + water inspection
local terrainRendered = false
local terrainCellCount = 0
local waterRendered = false
local terrain = Workspace:FindFirstChildOfClass("Terrain")
if terrain then
    local extents = terrain.MaxExtents
    local minP, maxP = extents.Min, extents.Max
    if maxP.X > minP.X and maxP.Z > minP.Z then
        terrainRendered = true
        local ok, count = pcall(function() return terrain:CountCells() end)
        if ok and typeof(count) == "number" then
            terrainCellCount = count
        end

        -- Sample a bounded region around world origin for Water material.
        -- ReadVoxels caps at ~4M voxels; 512x128x512 studs at 4-stud resolution
        -- gives 128*32*128 = ~524k voxels, well under the limit.
        local sampleMin = Vector3.new(-256, -64, -256)
        local sampleMax = Vector3.new(256, 64, 256)
        local region = Region3.new(sampleMin, sampleMax):ExpandToGrid(4)
        local readOk, materials = pcall(function()
            local mats = terrain:ReadVoxels(region, 4)
            return mats
        end)
        if readOk and materials then
            local sx = materials.Size.X
            local sy = materials.Size.Y
            local sz = materials.Size.Z
            for x = 1, sx do
                if waterRendered then break end
                for y = 1, sy do
                    if waterRendered then break end
                    for z = 1, sz do
                        if materials[x][y][z] == Enum.Material.Water then
                            waterRendered = true
                            break
                        end
                    end
                end
            end
        end
    end
end

-- Animator / Animation presence
local animatorCount = 0
local animationCount = 0
for _, desc in ipairs(game:GetDescendants()) do
    if desc:IsA("Animator") then animatorCount = animatorCount + 1 end
    if desc:IsA("Animation") then animationCount = animationCount + 1 end
end

local result = {
    parts = (counts["Part"] or 0) + (counts["MeshPart"] or 0),
    meshParts = counts["MeshPart"] or 0,
    scripts = (counts["Script"] or 0) + (counts["LocalScript"] or 0) + (counts["ModuleScript"] or 0),
    sounds = counts["Sound"] or 0,
    lights = (counts["PointLight"] or 0) + (counts["SpotLight"] or 0) + (counts["SurfaceLight"] or 0),
    surfaceAppearances = counts["SurfaceAppearance"] or 0,
    models = counts["Model"] or 0,
    totalInstances = totalInstances,
    scriptErrorCount = #errors,
    settleSeconds = SETTLE_SECONDS,
    terrainRendered = terrainRendered,
    terrainCellCount = terrainCellCount,
    waterRendered = waterRendered,
    animatorCount = animatorCount,
    animationCount = animationCount,
}

print("[SMOKE_TEST_RESULT] " .. HttpService:JSONEncode(result))

if #errors > 0 then
    print("[SMOKE_TEST_ERRORS_SUMMARY] " .. math.min(#errors, 20) .. " of " .. #errors .. " errors shown above")
end

print("[SMOKE_TEST_DONE]")
""".strip().replace("__SETTLE_SECONDS__", str(SETTLE_SECONDS))


CLIENT_HEALTH_CHECK_LUAU = r"""
-- Auto-injected by unity2rbxlx smoke_test (client)
-- Do NOT edit manually; this script is removed after the test run.

local Players = game:GetService("Players")
local HttpService = game:GetService("HttpService")
local Workspace = game:GetService("Workspace")

local player = Players.LocalPlayer
if not player then
    return
end

print("[SMOKE_TEST_CLIENT_START]")

local character = player.Character or player.CharacterAdded:Wait()
local hrp = character:WaitForChild("HumanoidRootPart", 10)
local humanoid = character:WaitForChild("Humanoid", 10)
local animator = humanoid and humanoid:FindFirstChildOfClass("Animator")
local hasAnimateScript = character:FindFirstChild("Animate") ~= nil
local camera = workspace.CurrentCamera

if not hrp or not humanoid or not camera then
    print("[SMOKE_TEST_CLIENT_RESULT] " .. HttpService:JSONEncode({
        error = "missing character or camera",
        hasHRP = hrp ~= nil,
        hasHumanoid = humanoid ~= nil,
        hasCamera = camera ~= nil,
    }))
    return
end

-- Settle: let the camera/control modules finish wiring up and the
-- WaterFill LocalScript (if present) finish filling client-local water voxels.
task.wait(4)

-- Water voxel scan (client-side — server-filled water doesn't replicate
-- beyond a small radius, but each client has its own filled copy).
local waterRendered = false
local terrain = Workspace:FindFirstChildOfClass("Terrain")
if terrain then
    local sampleMin = Vector3.new(-512, -128, -512)
    local sampleMax = Vector3.new(512, 128, 512)
    local region = Region3.new(sampleMin, sampleMax):ExpandToGrid(4)
    local readOk, materials = pcall(function()
        return terrain:ReadVoxels(region, 4)
    end)
    if readOk and materials then
        local sx, sy, sz = materials.Size.X, materials.Size.Y, materials.Size.Z
        for x = 1, sx do
            if waterRendered then break end
            for y = 1, sy do
                if waterRendered then break end
                for z = 1, sz do
                    if materials[x][y][z] == Enum.Material.Water then
                        waterRendered = true
                        break
                    end
                end
            end
        end
    end
end

local initialCamCF = camera.CFrame
local initialPos = hrp.Position

local animTracksPlayed = 0
local animConn = nil
if animator then
    animConn = animator.AnimationPlayed:Connect(function(_track)
        animTracksPlayed = animTracksPlayed + 1
    end)
end

print("[SMOKE_TEST_INPUT_WINDOW_OPEN]")

-- Driver simulates WASD + mouse during this window.
task.wait(12)

print("[SMOKE_TEST_INPUT_WINDOW_CLOSE]")

local finalCamCF = camera.CFrame
local finalPos = hrp.Position

if animConn then animConn:Disconnect() end

local camLookDelta = (finalCamCF.LookVector - initialCamCF.LookVector).Magnitude
local posDelta = (finalPos - initialPos).Magnitude

local clientResult = {
    cameraMoved = camLookDelta > 0.05,
    cameraLookDelta = camLookDelta,
    playerMoved = posDelta > 1.0,
    playerPositionDelta = posDelta,
    hasAnimator = animator ~= nil,
    hasAnimateScript = hasAnimateScript,
    animationTracksPlayed = animTracksPlayed,
    waterRendered = waterRendered,
}

print("[SMOKE_TEST_CLIENT_RESULT] " .. HttpService:JSONEncode(clientResult))
""".strip()


def inject_health_check(rbxlx_path: str | Path, output_path: str | Path | None = None) -> Path:
    """Inject server + client health-check scripts into an .rbxlx file.

    The server ``Script`` is appended to ``ServerScriptService`` (creating it
    if absent). The client ``LocalScript`` is appended to
    ``StarterPlayer.StarterPlayerScripts`` (creating both if absent). Any
    previously-injected smoke-test scripts under either parent are removed
    first so re-runs stay idempotent.

    Parameters
    ----------
    rbxlx_path:
        Path to the original .rbxlx file.
    output_path:
        Where to write the modified file. If None, writes to
        ``<original>_smoketest.rbxlx`` alongside the original.

    Returns
    -------
    Path
        The path to the modified .rbxlx file.
    """
    rbxlx_path = Path(rbxlx_path)
    if output_path is None:
        output_path = rbxlx_path.with_name(rbxlx_path.stem + "_smoketest.rbxlx")
    else:
        output_path = Path(output_path)

    tree = ET.parse(rbxlx_path)
    root = tree.getroot()

    sss = _find_service(root, "ServerScriptService")
    if sss is None:
        sss = _create_service(root, "ServerScriptService")
    _remove_existing_health_check(sss, HEALTH_CHECK_SCRIPT_NAME)
    sss.append(_build_script_element(
        HEALTH_CHECK_SCRIPT_NAME, HEALTH_CHECK_LUAU, class_name="Script", run_context=1
    ))

    sps = _find_or_create_starter_player_scripts(root)
    _remove_existing_health_check(sps, CLIENT_HEALTH_CHECK_SCRIPT_NAME)
    sps.append(_build_script_element(
        CLIENT_HEALTH_CHECK_SCRIPT_NAME, CLIENT_HEALTH_CHECK_LUAU, class_name="LocalScript"
    ))

    _write_rbxlx(tree, output_path)
    return output_path


def remove_health_check(rbxlx_path: str | Path) -> bool:
    """Remove injected health-check scripts from an .rbxlx file in place.

    Returns True if any script was found and removed.
    """
    rbxlx_path = Path(rbxlx_path)
    tree = ET.parse(rbxlx_path)
    root = tree.getroot()

    removed = False

    sss = _find_service(root, "ServerScriptService")
    if sss is not None:
        removed = _remove_existing_health_check(sss, HEALTH_CHECK_SCRIPT_NAME) or removed

    sp = _find_service(root, "StarterPlayer")
    if sp is not None:
        sps = _find_child_item(sp, "StarterPlayerScripts")
        if sps is not None:
            removed = _remove_existing_health_check(sps, CLIENT_HEALTH_CHECK_SCRIPT_NAME) or removed

    if removed:
        _write_rbxlx(tree, rbxlx_path)
    return removed


def _find_service(root: ET.Element, service_name: str) -> ET.Element | None:
    """Find a top-level service Item by class name or Name property."""
    for item in root.iter("Item"):
        if item.get("class") == service_name:
            return item
        props = item.find("Properties")
        if props is not None:
            name_elem = props.find("string[@name='Name']")
            if name_elem is not None and name_elem.text == service_name:
                return item
    return None


def _create_service(root: ET.Element, service_name: str) -> ET.Element:
    """Create a minimal service Item and append it to the root."""
    item = ET.SubElement(root, "Item")
    item.set("class", service_name)
    props = ET.SubElement(item, "Properties")
    name_elem = ET.SubElement(props, "string")
    name_elem.set("name", "Name")
    name_elem.text = service_name
    return item


def _find_child_item(parent: ET.Element, class_name: str) -> ET.Element | None:
    """Find a direct child Item with the given class attribute."""
    for child in parent:
        if child.tag == "Item" and child.get("class") == class_name:
            return child
    return None


def _find_or_create_starter_player_scripts(root: ET.Element) -> ET.Element:
    """Find StarterPlayer.StarterPlayerScripts, creating both if needed."""
    sp = _find_service(root, "StarterPlayer")
    if sp is None:
        sp = _create_service(root, "StarterPlayer")

    sps = _find_child_item(sp, "StarterPlayerScripts")
    if sps is None:
        sps = ET.SubElement(sp, "Item")
        sps.set("class", "StarterPlayerScripts")
        sps_props = ET.SubElement(sps, "Properties")
        name_elem = ET.SubElement(sps_props, "string")
        name_elem.set("name", "Name")
        name_elem.text = "StarterPlayerScripts"
    return sps


def _remove_existing_health_check(parent: ET.Element, script_name: str) -> bool:
    """Remove any previously injected health-check scripts with the given name."""
    removed = False
    for item in list(parent):
        if item.tag != "Item":
            continue
        props = item.find("Properties")
        if props is None:
            continue
        name_elem = props.find("string[@name='Name']")
        if name_elem is not None and name_elem.text == script_name:
            parent.remove(item)
            removed = True
    return removed


def _build_script_element(
    name: str,
    source: str,
    class_name: str = "Script",
    run_context: int | None = None,
) -> ET.Element:
    """Build an XML Item element for a Script / LocalScript with the given source."""
    item = ET.Element("Item")
    item.set("class", class_name)

    props = ET.SubElement(item, "Properties")

    name_elem = ET.SubElement(props, "string")
    name_elem.set("name", "Name")
    name_elem.text = name

    disabled = ET.SubElement(props, "bool")
    disabled.set("name", "Disabled")
    disabled.text = "false"

    linked = ET.SubElement(props, "Content")
    linked.set("name", "LinkedSource")
    ET.SubElement(linked, "null")

    if run_context is not None:
        run_ctx = ET.SubElement(props, "token")
        run_ctx.set("name", "RunContext")
        run_ctx.text = str(run_context)

    src = ET.SubElement(props, "ProtectedString")
    src.set("name", "Source")
    src.text = source

    return item


_CDATA_RE = re.compile(r"&lt;!\[CDATA\[(.*?)\]\]&gt;", re.DOTALL)


def _write_rbxlx(tree: ET.ElementTree, path: Path) -> None:
    """Write the XML tree to disk, wrapping ProtectedString content in CDATA.

    ElementTree doesn't have a native CDATA section type, so we wrap the text
    in ``<![CDATA[...]]>`` markers and then post-process the serialized XML:
    every ``&lt;![CDATA[...]]&gt;`` region gets its content XML-unescaped and
    re-emitted as a real CDATA section. Without the content-unescape step,
    characters like ``<`` inside the Luau source (e.g. ``#errors <= 20``)
    arrive in Studio as ``&lt;`` and break parsing.
    """
    root = tree.getroot()

    for ps in root.iter("ProtectedString"):
        if ps.text and "<![CDATA[" not in ps.text:
            ps.text = "<![CDATA[" + ps.text + "]]>"

    raw_xml = ET.tostring(root, encoding="unicode", xml_declaration=False)

    def _restore(match: re.Match) -> str:
        inner = match.group(1)
        inner = inner.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        return f"<![CDATA[{inner}]]>"

    raw_xml = _CDATA_RE.sub(_restore, raw_xml)

    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write(raw_xml)
