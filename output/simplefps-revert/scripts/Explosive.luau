local Debris = game:GetService("Debris")

local container = script.Parent

if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end

local function getTriggerPart()
    if container:IsA("BasePart") then return container end
    if container:IsA("Model") then
        return container.PrimaryPart or container:FindFirstChildWhichIsA("BasePart")
    end
    return nil
end

local explosionPrefab = nil
local detonationImpactVelocity = 10
local sizeMultiplier = 1
local reset = true
local resetTimeDelay = 10

local m_Exploded = false
local m_ObjectResetter = nil

local enabled = true

local Explosive = {}
-- Guard: skip runtime code if script is in ReplicatedStorage
if not (script.Parent:IsA("BasePart") or script.Parent:IsA("Model") or script.Parent:IsA("Folder")) then
    return Explosive
end


local function spawnExplosion(point, normal)
    if not explosionPrefab then return end
    local clone = explosionPrefab:Clone()

        -- Fix clone visibility and weld sub-mesh parts together
        local _primary
        if clone:IsA("BasePart") then
            _primary = clone
            clone.Transparency = 0
            clone.Anchored = false
            clone.CanCollide = false
        elseif clone:IsA("Model") then
            _primary = clone.PrimaryPart or clone:FindFirstChildWhichIsA("BasePart")
        end
        for _, _p in clone:GetDescendants() do
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
    local lookCF = CFrame.lookAt(point, point + normal)
    if clone:IsA("Model") then
        if not clone.PrimaryPart then
            clone.PrimaryPart = clone:FindFirstChildWhichIsA("BasePart")
        end
        clone:PivotTo(lookCF)
    elseif clone:IsA("BasePart") then
        clone.CFrame = lookCF
    end
    clone.Parent = workspace
end

local function handleCollision(otherPart)
    if not enabled then return end
    if not otherPart or not otherPart:IsA("BasePart") then return end

    local selfPart = getTriggerPart()
    if not selfPart then return end

    local relativeVelocity = selfPart.AssemblyLinearVelocity - otherPart.AssemblyLinearVelocity
    local normal = (selfPart.Position - otherPart.Position)
    if normal.Magnitude < 1e-6 then
        normal = Vector3.new(0, 1, 0)
    else
        normal = normal.Unit
    end
    local point = otherPart.Position

    local velocityAlongCollisionNormal = relativeVelocity:Dot(normal)
    if velocityAlongCollisionNormal < 0 then
        velocityAlongCollisionNormal = -velocityAlongCollisionNormal
    end

    if velocityAlongCollisionNormal > detonationImpactVelocity or m_Exploded then
        if not m_Exploded then
            spawnExplosion(point, normal)
            m_Exploded = true

            container:SetAttribute("Immobilize", true)

            if reset and m_ObjectResetter then
                if type(m_ObjectResetter.DelayedReset) == "function" then
                    m_ObjectResetter.DelayedReset(resetTimeDelay)
                else
                    container:SetAttribute("DelayedReset", resetTimeDelay)
                end
            end
        end
    end
end

function Explosive.Reset()
    m_Exploded = false
end

local function connectTouched()
    local selfPart = getTriggerPart()
    if not selfPart then return end
    selfPart.Touched:Connect(handleCollision)
end

local function start()
    local resetterModule = script.Parent:FindFirstChild("ObjectResetter")
    if resetterModule and resetterModule:IsA("ModuleScript") then
        local ok, mod = pcall(require, resetterModule)
        if ok then
            m_ObjectResetter = mod
        end
    end

    connectTouched()
end

start()

return Explosive