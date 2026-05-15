-- Services
local RunService = game:GetService("RunService")

local container = script.Parent

local WaterTile = {}

-- Configuration (from serialized fields)
local reflection = nil
local waterBase = nil

local function AcquireComponents()
    if not reflection then
        local parent = container.Parent
        if parent then
            local mod = parent:FindFirstChild("PlanarReflection")
            if mod and mod:IsA("ModuleScript") then
                reflection = require(mod)
            end
        else
            local mod = container:FindFirstChild("PlanarReflection")
            if mod and mod:IsA("ModuleScript") then
                reflection = require(mod)
            end
        end
    end

    if not waterBase then
        local parent = container.Parent
        if parent then
            local mod = parent:FindFirstChild("WaterBase")
            if mod and mod:IsA("ModuleScript") then
                waterBase = require(mod)
            end
        else
            local mod = container:FindFirstChild("WaterBase")
            if mod and mod:IsA("ModuleScript") then
                waterBase = require(mod)
            end
        end
    end
end

local function Start()
    AcquireComponents()
end

local function OnWillRenderObject()
    if reflection then
        reflection.WaterTileBeingRendered(container, workspace.CurrentCamera)
    end
    if waterBase then
        waterBase.WaterTileBeingRendered(container, workspace.CurrentCamera)
    end
end

WaterTile.OnWillRenderObject = OnWillRenderObject

Start()

-- UNCONVERTED: OnWillRenderObject is a per-camera Unity render callback with
-- no direct Roblox equivalent. Approximate by invoking each frame so the
-- reflection / waterBase tile-rendered hooks still fire.
RunService.Heartbeat:Connect(function(dt)
    AcquireComponents()
    OnWillRenderObject()
end)

return WaterTile