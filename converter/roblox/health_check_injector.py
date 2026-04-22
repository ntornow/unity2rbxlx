"""
Inject a smoke-test health-check Script into an existing .rbxlx file.

The injected Script runs in ServerScriptService, counts instances by type,
captures script errors via ScriptContext.Error, and prints structured output
that the Studio log parser can pick up.
"""

from __future__ import annotations

import copy
import xml.etree.ElementTree as ET
from pathlib import Path

HEALTH_CHECK_SCRIPT_NAME = "__SmokeTestHealthCheck"

HEALTH_CHECK_LUAU = r"""
-- Auto-injected by unity2rbxlx smoke_test
-- Do NOT edit manually; this script is removed after the test run.

local ScriptContext = game:GetService("ScriptContext")
local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")

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

-- Wait for the game to settle (scripts to run, meshes to load)
local SETTLE_SECONDS = 15
task.wait(SETTLE_SECONDS)

-- Count instances by ClassName
local counts = {}
local totalInstances = 0
for _, desc in ipairs(game:GetDescendants()) do
    local cn = desc.ClassName
    counts[cn] = (counts[cn] or 0) + 1
    totalInstances = totalInstances + 1
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
}

print("[SMOKE_TEST_RESULT] " .. HttpService:JSONEncode(result))

-- Print top errors summary
if #errors > 0 then
    print("[SMOKE_TEST_ERRORS_SUMMARY] " .. math.min(#errors, 20) .. " of " .. #errors .. " errors shown above")
end

print("[SMOKE_TEST_DONE]")
""".strip()


def inject_health_check(rbxlx_path: str | Path, output_path: str | Path | None = None) -> Path:
    """Inject a health-check Script into an .rbxlx file.

    Finds (or creates) the ServerScriptService item and appends a Script
    child that runs the health-check Luau code.

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

    _remove_existing_health_check(sss)

    script_item = _build_script_element(HEALTH_CHECK_SCRIPT_NAME, HEALTH_CHECK_LUAU)
    sss.append(script_item)

    _write_rbxlx(tree, output_path)
    return output_path


def remove_health_check(rbxlx_path: str | Path) -> bool:
    """Remove the injected health-check Script from an .rbxlx file in place.

    Returns True if a script was found and removed.
    """
    rbxlx_path = Path(rbxlx_path)
    tree = ET.parse(rbxlx_path)
    root = tree.getroot()

    sss = _find_service(root, "ServerScriptService")
    if sss is None:
        return False

    removed = _remove_existing_health_check(sss)
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


def _remove_existing_health_check(parent: ET.Element) -> bool:
    """Remove any previously injected health-check scripts."""
    removed = False
    for item in list(parent):
        if item.tag != "Item":
            continue
        props = item.find("Properties")
        if props is None:
            continue
        name_elem = props.find("string[@name='Name']")
        if name_elem is not None and name_elem.text == HEALTH_CHECK_SCRIPT_NAME:
            parent.remove(item)
            removed = True
    return removed


def _build_script_element(name: str, source: str) -> ET.Element:
    """Build an XML Item element for a Script with the given source."""
    item = ET.Element("Item")
    item.set("class", "Script")

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

    run_ctx = ET.SubElement(props, "token")
    run_ctx.set("name", "RunContext")
    run_ctx.text = "1"

    src = ET.SubElement(props, "ProtectedString")
    src.set("name", "Source")
    src.text = source

    return item


def _write_rbxlx(tree: ET.TreeBuilder, path: Path) -> None:
    """Write the XML tree to disk, wrapping ProtectedString content in CDATA."""
    root = tree.getroot()

    for ps in root.iter("ProtectedString"):
        if ps.text and "<![CDATA[" not in ps.text:
            ps.text = "<![CDATA[" + ps.text + "]]>"

    raw_xml = ET.tostring(root, encoding="unicode", xml_declaration=False)

    # ET escapes our CDATA markers — unescape them
    raw_xml = raw_xml.replace("&lt;![CDATA[", "<![CDATA[")
    raw_xml = raw_xml.replace("]]&gt;", "]]>")

    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="utf-8"?>\n')
        f.write(raw_xml)
