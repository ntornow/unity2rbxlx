-- Services
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")

-- Tiered finder helper for trigger/visual parts.
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

local function findVisualTarget(parent)
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("Model") and c.Name ~= "MinimapIcon" and c:FindFirstChildWhichIsA("BasePart") then
            return c
        end
    end
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("MeshPart") then return c end
    end
    for _, c in ipairs(parent:GetChildren()) do
        if c:IsA("BasePart") and c.Transparency < 1 then return c end
    end
    return parent:FindFirstChildWhichIsA("BasePart")
end

-- Container dispatch
local container = script.Parent
if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end

local function getPosition() return getCFrame().Position end

local function getUp() return getCFrame().UpVector end

-- WaterMode enum
local WaterMode = {
    Simple = 0,
    Reflective = 1,
    Refractive = 2,
}

local Water = {}
-- Guard: skip runtime code if script is in ReplicatedStorage
if not (script.Parent:IsA("BasePart") or script.Parent:IsA("Model") or script.Parent:IsA("Folder")) then
    return Water
end


-- Configuration (from serialized fields)
local waterMode = WaterMode.Refractive
local disablePixelLights = true
local textureSize = 256
local clipPlaneOffset = 0.07
local reflectLayers = -1
local refractLayers = -1

-- State
local m_ReflectionCameras = {}
local m_RefractionCameras = {}
local m_ReflectionTexture = nil
local m_RefractionTexture = nil
local m_HardwareWaterSupport = WaterMode.Refractive
local m_OldReflectionTextureSize = 0
local m_OldRefractionTextureSize = 0
local s_InsideWater = false

-- BORKS ADDON: damage player on water touch
local triggerPart = findTriggerPart(container)
if not triggerPart and container:IsA("BasePart") then
    triggerPart = container
end

if triggerPart then
    triggerPart.Touched:Connect(function(otherPart)
        local character = otherPart.Parent
        if not character then return end
        local humanoid = character:FindFirstChildOfClass("Humanoid")
        if not humanoid then return end
        local player = Players:GetPlayerFromCharacter(character)
        if player then
            humanoid:TakeDamage(100)
        end
    end)
end
-- END BORKS ADDON

local function GetWaterMode()
    if m_HardwareWaterSupport < waterMode then
        return m_HardwareWaterSupport
    end
    return waterMode
end

local function FindHardwareWaterSupport()
    -- UNCONVERTED: material.GetTag("WATERMODE") has no Roblox equivalent;
    -- Roblox materials do not expose Unity-style shader tags.
    return WaterMode.Simple
end

local function CameraSpacePlane(cam, pos, normal, sideSign)
    -- UNCONVERTED: Camera.worldToCameraMatrix / MultiplyPoint / MultiplyVector
    -- have no Roblox equivalent (no exposed view matrix API).
    return Vector3.new(0, 0, 0), 0
end

local function CalculateReflectionMatrix(plane)
    local m = {}
    m[1] = {1 - 2 * plane[1] * plane[1], -2 * plane[1] * plane[2], -2 * plane[1] * plane[3], -2 * plane[4] * plane[1]}
    m[2] = {-2 * plane[2] * plane[1], 1 - 2 * plane[2] * plane[2], -2 * plane[2] * plane[3], -2 * plane[4] * plane[2]}
    m[3] = {-2 * plane[3] * plane[1], -2 * plane[3] * plane[2], 1 - 2 * plane[3] * plane[3], -2 * plane[4] * plane[3]}
    m[4] = {0, 0, 0, 1}
    return m
end

local function UpdateCameraModes(src, dest)
    -- UNCONVERTED: Camera clearFlags / backgroundColor / Skybox component /
    -- projection settings copy is Unity render-pipeline specific.
    if dest == nil then
        return
    end
end

local function CreateWaterObjects(currentCamera)
    -- UNCONVERTED: RenderTexture and dynamically created Camera GameObjects
    -- with FlareLayer/Skybox components have no Roblox equivalent. Roblox
    -- only exposes workspace.CurrentCamera; secondary cameras for reflection/
    -- refraction render-to-texture are not supported.
    local mode = GetWaterMode()
    return nil, nil
end

local function OnWillRenderObject()
    -- UNCONVERTED: OnWillRenderObject fires per-camera in Unity's render loop.
    -- Roblox has no render-callback hook for objects, and no manual
    -- reflection/refraction camera rendering. The water effect is approximated
    -- via the material/decal applied to the part.
    if not script.Parent then
        return
    end
    if s_InsideWater then
        return
    end
    s_InsideWater = true

    m_HardwareWaterSupport = FindHardwareWaterSupport()
    local mode = GetWaterMode()

    local reflectionCamera, refractionCamera = CreateWaterObjects(workspace.CurrentCamera)

    local pos = getPosition()
    local normal = getUp()

    UpdateCameraModes(workspace.CurrentCamera, reflectionCamera)
    UpdateCameraModes(workspace.CurrentCamera, refractionCamera)

    if mode >= WaterMode.Reflective then
        local d = -normal:Dot(pos) - clipPlaneOffset
        local reflectionPlane = {normal.X, normal.Y, normal.Z, d}
        local reflection = CalculateReflectionMatrix(reflectionPlane)
        -- UNCONVERTED: reflection camera rendering not possible in Roblox.
    end

    if mode >= WaterMode.Refractive then
        -- UNCONVERTED: refraction camera rendering not possible in Roblox.
    end

    s_InsideWater = false
end

local function OnDisable()
    m_ReflectionTexture = nil
    m_RefractionTexture = nil
    for _, cam in pairs(m_ReflectionCameras) do
        if cam then
            cam:Destroy()
        end
    end
    m_ReflectionCameras = {}
    for _, cam in pairs(m_RefractionCameras) do
        if cam then
            cam:Destroy()
        end
    end
    m_RefractionCameras = {}
end

-- IEEERemainder approximation (returns x - round(x/y)*y, banker's rounding)
local function IEEERemainder(x, y)
    local q = x / y
    local rounded = math.floor(q + 0.5)
    if (q - math.floor(q)) == 0.5 then
        if math.floor(q) % 2 == 0 then
            rounded = math.floor(q)
        else
            rounded = math.floor(q) + 1
        end
    end
    return x - rounded * y
end

-- Update: water texture scrolling. Roblox materials don't expose the same
-- shader parameters, so this updates attributes for any consumer that wants
-- to drive a custom water effect.
local startTime = tick()
RunService.Heartbeat:Connect(function(dt)
    if not container then
        return
    end

    -- UNCONVERTED: Material.GetVector("WaveSpeed") / GetFloat("_WaveScale")
    -- have no Roblox equivalent. Use defaults from the Unity water material.
    local waveSpeed = Vector3.new(19, 9, -16)
    local waveSpeedW = -7
    local waveScale = 0.063

    local waveScale4 = {waveScale, waveScale, waveScale * 0.4, waveScale * 0.45}

    local t = (tick() - startTime) / 20.0

    local offsetClamped = {
        IEEERemainder(waveSpeed.X * waveScale4[1] * t, 1.0),
        IEEERemainder(waveSpeed.Y * waveScale4[2] * t, 1.0),
        IEEERemainder(waveSpeed.Z * waveScale4[3] * t, 1.0),
        IEEERemainder(waveSpeedW * waveScale4[4] * t, 1.0),
    }

    container:SetAttribute("_WaveOffsetX", offsetClamped[1])
    container:SetAttribute("_WaveOffsetY", offsetClamped[2])
    container:SetAttribute("_WaveOffsetZ", offsetClamped[3])
    container:SetAttribute("_WaveOffsetW", offsetClamped[4])
    container:SetAttribute("_WaveScale4X", waveScale4[1])
    container:SetAttribute("_WaveScale4Y", waveScale4[2])
    container:SetAttribute("_WaveScale4Z", waveScale4[3])
    container:SetAttribute("_WaveScale4W", waveScale4[4])
end)

script.Destroying:Connect(OnDisable)

return Water