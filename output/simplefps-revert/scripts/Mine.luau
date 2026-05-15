-- _AutoTemplateGuard: bail when this script is running on a
-- prefab template under ``ReplicatedStorage.Templates``. Server-context
-- scripts run on the template itself at place load, so any self-destroy
-- (container:Destroy / Debris:AddItem(container, …)) would nuke the
-- template and break downstream :Clone() / WaitForChild calls.
do
	local _p = script.Parent
	while _p do
		if _p.Name == "Templates" and _p.Parent
			and _p.Parent:IsA("ReplicatedStorage")
		then return end
		_p = _p.Parent
	end
end

local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Debris = game:GetService("Debris")

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
local function getPosition() return getCFrame().Position end

local explodeTime = 1
local damage = 10

local triggered = false

local function explode()
    local explosionTemplate = ReplicatedStorage:FindFirstChild("Templates")
    if explosionTemplate then
        explosionTemplate = explosionTemplate:FindFirstChild("Explosion")
    end
    if explosionTemplate then
        local clone = explosionTemplate:Clone()

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
        if clone:IsA("Model") then
            if not clone.PrimaryPart then
                clone.PrimaryPart = clone:FindFirstChildWhichIsA("BasePart")
            end
            clone:PivotTo(CFrame.new(getPosition()))
        else
            clone.CFrame = CFrame.new(getPosition())
        end
        clone.Parent = workspace
    end

    local parts = workspace:GetPartBoundsInRadius(getPosition(), 2)
    local hitPlayers = {}
    for _, part in ipairs(parts) do
        local model = part:FindFirstAncestorOfClass("Model")
        if model and model:FindFirstChildOfClass("Humanoid") and not hitPlayers[model] then
            hitPlayers[model] = true
            local humanoid = model:FindFirstChildOfClass("Humanoid")
            humanoid:TakeDamage(damage)
        end
    end

    container:Destroy()
end

local triggerPart = findTriggerPart(container)
if not triggerPart and container:IsA("BasePart") then
    triggerPart = container
end

if triggerPart then
    triggerPart.Touched:Connect(function(otherPart)
        if triggered then return end
        local model = otherPart:FindFirstAncestorOfClass("Model")
        if model and model:FindFirstChildOfClass("Humanoid") then
            triggered = true
            local sound = container:FindFirstChildWhichIsA("Sound")
            if not sound and triggerPart:IsA("BasePart") then
                sound = triggerPart:FindFirstChildWhichIsA("Sound")
            end
            if sound then sound:Play() end
            task.delay(explodeTime, explode)
        end
    end)
end