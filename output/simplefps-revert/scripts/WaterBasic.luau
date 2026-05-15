-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

-- Services
local RunService = game:GetService("RunService")

local container = script.Parent

local startTime = tick()

RunService.Heartbeat:Connect(function(dt)
    if not container then
        return
    end

    -- UNCONVERTED: Renderer.sharedMaterial.GetVector/GetFloat have no Roblox
    -- equivalent. Use defaults from the Unity water material.
    local waveSpeedX = 19
    local waveSpeedY = 9
    local waveSpeedZ = -16
    local waveSpeedW = -7
    local waveScale = 0.063

    local t = (tick() - startTime) / 20.0

    local offset4X = waveSpeedX * (t * waveScale)
    local offset4Y = waveSpeedY * (t * waveScale)
    local offset4Z = waveSpeedZ * (t * waveScale)
    local offset4W = waveSpeedW * (t * waveScale)

    local function repeatFn(v, length)
        return v - math.floor(v / length) * length
    end

    local offsetClampedX = repeatFn(offset4X, 1.0)
    local offsetClampedY = repeatFn(offset4Y, 1.0)
    local offsetClampedZ = repeatFn(offset4Z, 1.0)
    local offsetClampedW = repeatFn(offset4W, 1.0)

    container:SetAttribute("_WaveOffsetX", offsetClampedX)
    container:SetAttribute("_WaveOffsetY", offsetClampedY)
    container:SetAttribute("_WaveOffsetZ", offsetClampedZ)
    container:SetAttribute("_WaveOffsetW", offsetClampedW)
end)