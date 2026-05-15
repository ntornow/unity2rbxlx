local Players = game:GetService("Players")

local TRIGGER_NAMES = {"Collider", "Trigger", "TriggerZone", "Detector",
    "Sensor", "Hitbox", "Range", "ProximityVolume", "PickupTouchDetector"}
local function findTriggerPart(parent)
    for _, n in ipairs(TRIGGER_NAMES) do
        local p = parent:FindFirstChild(n)
        if p and p:IsA("BasePart") then return p end
    end
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("BasePart") and c.Transparency >= 1 and not c:IsA("MeshPart") then
            return c
        end
    end
    return nil
end

local container = script.Parent
if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end
local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end

local activateOnSpawn = nil

local triggerPart
if container:IsA("BasePart") then
    triggerPart = container
else
    triggerPart = findTriggerPart(container)
end

local fired = false

local function setActiveRecursive(obj, active)
    if obj:IsA("BasePart") then
        obj.Transparency = if active then 0 else 1
        obj.CanCollide = active
    end
    for _, child in ipairs(obj:GetChildren()) do
        setActiveRecursive(child, active)
    end
end

local function onTriggerEnter(other)
    if fired then return end
    if not other or not other.Parent then return end

    local player = Players:GetPlayerFromCharacter(other.Parent)
    if not player then return end

    fired = true

    local character = other.Parent
    character:SetAttribute("UpdateSpawnpoint", true)
    character:SetAttribute("SpawnpointPosition", getCFrame().Position)

    local playerScript = character:FindFirstChild("Player")
    if playerScript then
        playerScript:SetAttribute("UpdateSpawnpoint", true)
        playerScript:SetAttribute("SpawnpointPosition", getCFrame().Position)
    end

    if activateOnSpawn ~= nil then
        setActiveRecursive(activateOnSpawn, true)
    end

    script:Destroy()
end

if triggerPart then
    triggerPart.Touched:Connect(onTriggerEnter)
end