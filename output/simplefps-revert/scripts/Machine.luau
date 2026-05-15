-- Services
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local container = script.Parent

-- Pick the largest invisible BasePart as the trigger zone. Named candidates
-- ("Colliders", "Collider", "Trigger", …) are tried first; otherwise we use
-- volume to avoid binding to the 1×1×1 ``Item N`` slot Parts.
local TRIGGER_NAMES = {"Colliders", "Collider", "Trigger", "TriggerZone", "Detector",
    "Sensor", "Hitbox", "Range", "ProximityVolume", "PickupTouchDetector"}
local function findTriggerPart(parent)
    for _, n in ipairs(TRIGGER_NAMES) do
        local p = parent:FindFirstChild(n)
        if p and p:IsA("BasePart") then return p end
    end
    local best, bestVol = nil, -1
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("BasePart") and c.Transparency >= 1 and not c:IsA("MeshPart") then
            local v = c.Size.X * c.Size.Y * c.Size.Z
            if v > bestVol then best, bestVol = c, v end
        end
    end
    return best
end

-- Configuration: scene conversion writes prefab/door/plane names as
-- comma-separated string attributes on the machine. Defaults match the
-- SimpleFPS pickup item set so the script is functional even if the
-- attributes are missing.
local function readNameList(attrName, default)
    local v = container:GetAttribute(attrName)
    if typeof(v) == "string" and v ~= "" then
        return string.split(v, ",")
    end
    return default
end

local itemPrefabNames = readNameList("itemPrefabs",
    {"Battery", "SmallBattery", "MediumBattery", "GasCan"})
local doorNames = readNameList("doors", {"Door1", "Door2"})
local planeHolderName = container:GetAttribute("planeHolder") or "PlaneHolder"

-- Object Variables (1-indexed: items[1..4], itemNames[1..3], itemPlaced[1..3])
local items = {}
local itemNames = {}
local itemPlaced = {false, false, false}
local activated = 0

-- Awake
for i = 1, 4 do
    items[i] = container:FindFirstChild("Item " .. i)
    if i <= 3 then
        itemNames[i] = itemPrefabNames[i]
    end
end

local templates = ReplicatedStorage:FindFirstChild("Templates")

local function findPrefab(name)
    if templates and name then
        return templates:FindFirstChild(name)
    end
    return nil
end

local function getSlotCFrame(slot)
    if not slot then return nil end
    if slot:IsA("BasePart") then return slot.CFrame end
    if slot:IsA("Model") then return slot:GetPivot() end
    return nil
end

local function spawnPrefabAt(prefab, cf, parent, useRotation)
    if not prefab then return nil end
    local item = prefab:Clone()

        -- Fix clone visibility and weld sub-mesh parts together
        local _primary
        if item:IsA("BasePart") then
            _primary = item
            item.Transparency = 0
            item.Anchored = false
            item.CanCollide = false
        elseif item:IsA("Model") then
            _primary = item.PrimaryPart or item:FindFirstChildWhichIsA("BasePart")
        end
        for _, _p in item:GetDescendants() do
            if _p:IsA("BasePart") then
                _p.Transparency = 0
                _p.Anchored = false
                _p.CanCollide = false
                if _primary and _p ~= _primary then
                    local _w = Instance.new("WeldConstraint")
                    _w.Part0 = _p
                    _w.Part1 = _primary
                    _w.Parent = _p
                end
            end
        end
    local placeCF = cf
    if not useRotation and cf then
        placeCF = CFrame.new(cf.Position)
    end
    if item:IsA("Model") then
        if not item.PrimaryPart then
            item.PrimaryPart = item:FindFirstChildWhichIsA("BasePart")
        end
        if placeCF then item:PivotTo(placeCF) end
    elseif item:IsA("BasePart") then
        if placeCF then item.CFrame = placeCF end
    end
    item.Parent = parent
    return item
end

local function placeItem(number)
    if itemPlaced[number] then return end

    -- C#: items[activated++] — use current slot, then increment.
    local slotIndex = activated + 1
    local slot = items[slotIndex]
    if not slot then return end

    local prefab = findPrefab(itemPrefabNames[number])
    spawnPrefabAt(prefab, getSlotCFrame(slot), slot, true)

    activated = activated + 1
    itemPlaced[number] = true

    -- Unity Inspector wires Machine.doors[number] to a specific Door
    -- instance, but the Roblox conversion doesn't yet propagate that
    -- reference. Try the persisted-name list first, then fall back to
    -- Unity-style sibling naming (``Door (N)``) and the literal ``DoorN``.
    -- All forms reuse Unity's 1-based index so ``placeItem(1)`` opens the
    -- door wired to the first slot.
    local door = workspace:FindFirstChild(doorNames[number] or "", true)
        or workspace:FindFirstChild("Door (" .. number .. ")", true)
        or workspace:FindFirstChild("Door" .. number, true)
    if door then
        door:SetAttribute("ToggleDoor", true)
    end

    local planeHolder = workspace:FindFirstChild(planeHolderName, true)
    if planeHolder then
        planeHolder:SetAttribute("actionNumber", activated)
    end

    if activated == 3 then
        -- itemPrefabs[3] / items[3] in 0-indexed C# == [4] in 1-indexed Luau.
        local finalSlot = items[4]
        local finalPrefab = findPrefab(itemPrefabNames[4])
        spawnPrefabAt(finalPrefab, getSlotCFrame(finalSlot), workspace, false)
    end
end

local function onTriggerEnter(otherPart)
    local character = otherPart:FindFirstAncestorOfClass("Model")
    if not character then return end
    if not character:FindFirstChildOfClass("Humanoid") then return end
    local player = Players:GetPlayerFromCharacter(character)
    if not player then return end

    -- Pickup writes per-item server-side ``has<ItemName>`` booleans on the
    -- Player instance — these DO replicate. ``hasItems`` is a client-only
    -- comma-separated string maintained by Player.luau (LocalScript) and
    -- never reaches the server, so checking it here would always return nil.
    for i = 1, #itemNames do
        local name = itemNames[i]
        if name and player:GetAttribute("has" .. name) == true then
            placeItem(i)
        end
    end
end

-- Touched fires continuously while overlapping; debounce to mimic
-- Unity's OnTriggerEnter (one fire per enter).
local triggerPart = findTriggerPart(container)
if not triggerPart and container:IsA("BasePart") then
    triggerPart = container
end

if triggerPart then
    local debounce = false
    triggerPart.Touched:Connect(function(otherPart)
        if debounce then return end
        debounce = true
        onTriggerEnter(otherPart)
        task.delay(0.5, function() debounce = false end)
    end)
end