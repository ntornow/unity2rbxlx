-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

local UserInputService = game:GetService("UserInputService")
local RunService = game:GetService("RunService")

local container = script.Parent

local maxPower = 20
local minPower = 5
local changeSpeed = 5

local hoseWaterSystems = {}
local systemRenderer = nil

local function collectParticleSystems()
    local result = {}
    if container then
        for _, desc in ipairs(container:GetDescendants()) do
            if desc:IsA("ParticleEmitter") or desc:IsA("ParticleSystem") then
                table.insert(result, desc)
            end
        end
    end
    return result
end

local function findRenderer()
    if not container then return nil end
    for _, desc in ipairs(container:GetDescendants()) do
        if desc:IsA("MeshPart") or desc:IsA("BasePart") then
            if desc.Transparency < 1 then
                return desc
            end
        end
    end
    return container:FindFirstChildWhichIsA("BasePart")
end

hoseWaterSystems = collectParticleSystems()
systemRenderer = findRenderer()

local rendererEnabled = true
if systemRenderer then
    rendererEnabled = systemRenderer.Transparency < 1
end

local m_Power = 0

local function setRendererEnabled(enabled)
    if not systemRenderer then return end
    if enabled then
        systemRenderer.Transparency = 0
    else
        systemRenderer.Transparency = 1
    end
end

UserInputService.InputBegan:Connect(function(input, processed)
    if processed then return end
    if input.KeyCode == Enum.KeyCode.One then
        rendererEnabled = not rendererEnabled
        setRendererEnabled(rendererEnabled)
    end
end)

RunService.Heartbeat:Connect(function(dt)
    local target = minPower
    if UserInputService:IsMouseButtonPressed(Enum.UserInputType.MouseButton1) then
        target = maxPower
    end
    m_Power = m_Power + (target - m_Power) * (dt * changeSpeed)

    local emissionEnabled = (m_Power > minPower * 1.1)

    for _, system in ipairs(hoseWaterSystems) do
        if system:IsA("ParticleEmitter") then
            system.Speed = NumberRange.new(m_Power)
            system.Enabled = emissionEnabled
        end
    end
end)