-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

local RunService = game:GetService("RunService")

local effectAngle = 15
local effectWidth = 1
local effectDistance = 10
local force = 10

local container = script.Parent
if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end

local function getPosition()
    return getCFrame().Position
end

local function findSphereCollider()
    if container:IsA("BasePart") and container.Shape == Enum.PartType.Ball then
        return container
    end
    for _, c in ipairs(container:GetChildren()) do
        if c:IsA("BasePart") and c.Shape == Enum.PartType.Ball then
            return c
        end
    end
    if container:IsA("BasePart") then return container end
    return container:FindFirstChildWhichIsA("BasePart")
end

local m_Sphere = findSphereCollider()

local function getSphereCenter()
    if not m_Sphere then return Vector3.new(0, 0, 0) end
    return m_Sphere.Position - getPosition()
end

local function getSphereRadius()
    if not m_Sphere then return effectDistance * 0.5 end
    return math.max(m_Sphere.Size.X, m_Sphere.Size.Y, m_Sphere.Size.Z) * 0.5
end

local function inverseTransformPoint(cf, worldPoint)
    return cf:PointToObjectSpace(worldPoint)
end

local function transformPoint(cf, localPoint)
    return cf:PointToWorldSpace(localPoint)
end

local function vector3MoveTowards(current, target, maxDistanceDelta)
    local delta = target - current
    local mag = delta.Magnitude
    if mag <= maxDistanceDelta or mag == 0 then
        return target
    end
    return current + delta / mag * maxDistanceDelta
end

local function inverseLerp(a, b, v)
    if a == b then return 0 end
    local t = (v - a) / (b - a)
    if t < 0 then t = 0 end
    if t > 1 then t = 1 end
    return t
end

local function lerpVector(a, b, t)
    return a + (b - a) * t
end

RunService.Heartbeat:Connect(function(dt)
    if not m_Sphere then return end
    local originPos = getPosition()
    local cf = getCFrame()
    local center = originPos + getSphereCenter()
    local radius = getSphereRadius()
    local parts = workspace:GetPartBoundsInRadius(center, radius)
    for _, part in ipairs(parts) do
        if part:IsA("BasePart") and not part.Anchored and part.AssemblyRootPart ~= nil then
            local partPos = part.Position
            local localPos = inverseTransformPoint(cf, partPos)
            localPos = vector3MoveTowards(localPos, Vector3.new(0, 0, localPos.Z), effectWidth * 0.5)
            local angle = math.abs(math.atan2(localPos.X, localPos.Z) * (180 / math.pi))
            local falloff = inverseLerp(effectDistance, 0, localPos.Magnitude)
            falloff = falloff * inverseLerp(effectAngle, 0, angle)
            local delta = partPos - originPos
            if delta.Magnitude > 0 then
                local impulseDir = delta.Unit * force * falloff
                local applyPos = lerpVector(partPos, transformPoint(cf, Vector3.new(0, 0, localPos.Z)), 0.1)
                local impulse = impulseDir * dt
                local root = part.AssemblyRootPart
                if root then
                    root:ApplyImpulseAtPosition(impulse, applyPos)
                end
            end
        end
    end
end)