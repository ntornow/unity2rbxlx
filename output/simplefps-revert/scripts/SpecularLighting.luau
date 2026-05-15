-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

-- Services
local RunService = game:GetService("RunService")

-- Container dispatch
local container = script.Parent
if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end

-- Configuration (from serialized fields)
local specularLight = nil

-- State
local m_WaterBase = nil

local function findWaterBase()
    local sibling = container:FindFirstChild("WaterBase")
    if sibling then
        return sibling
    end
    for _, c in ipairs(container:GetChildren()) do
        if c:IsA("Script") or c:IsA("ModuleScript") then
            if c.Name == "WaterBase" then
                return c
            end
        end
    end
    return nil
end

local function findSpecularLight()
    local lightObj = container:FindFirstChild("SpecularLight")
    if lightObj then
        return lightObj
    end
    local directional = workspace:FindFirstChild("DirectionalLight", true)
    if directional then
        return directional
    end
    return nil
end

m_WaterBase = findWaterBase()
specularLight = findSpecularLight()

RunService.Heartbeat:Connect(function(dt)
    if not m_WaterBase then
        m_WaterBase = findWaterBase()
    end

    if not specularLight then
        specularLight = findSpecularLight()
    end

    if specularLight and m_WaterBase then
        local lightCFrame
        if specularLight:IsA("Model") then
            lightCFrame = specularLight:GetPivot()
        elseif specularLight:IsA("BasePart") then
            lightCFrame = specularLight.CFrame
        else
            lightCFrame = nil
        end

        if lightCFrame then
            local forward = lightCFrame.LookVector
            container:SetAttribute("_WorldLightDirX", forward.X)
            container:SetAttribute("_WorldLightDirY", forward.Y)
            container:SetAttribute("_WorldLightDirZ", forward.Z)
        end
    end
end)