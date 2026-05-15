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

-- Services
local Players = game:GetService("Players")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local container = script.Parent

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

local trigger = nil
if container:IsA("BasePart") then
    trigger = container
elseif container:IsA("Model") then
    trigger = findTriggerPart(container) or container:FindFirstChildWhichIsA("BasePart")
end

-- Inspector: flyingPlane -> "Plane Flying" (ReplicatedStorage.Templates)
local templates = ReplicatedStorage:WaitForChild("Templates", 10)
local flyingPlanePrefab = templates and templates:FindFirstChild("Plane Flying")
-- cutsceneCamera was not Inspector-resolved; best-effort name lookup.
local cutsceneCamera = workspace:FindFirstChild("cutsceneCamera", true)

local triggered = false

local function onTriggerEnter(other)
    if triggered then return end
    if not other or not other:IsA("BasePart") then return end

    local hitModel = other:FindFirstAncestorOfClass("Model")
    if not hitModel then return end
    local player = Players:GetPlayerFromCharacter(hitModel)
    if not player then return end

    local items = player:GetAttribute("hasItems") or ""
    local hasGasCan = false
    for item in string.gmatch(items, "[^,]+") do
        if item == "GasCan" then
            hasGasCan = true
            break
        end
    end
    if not hasGasCan then return end

    triggered = true

    if flyingPlanePrefab and container.Parent then
        local plane = flyingPlanePrefab:Clone()

            -- Fix clone visibility and weld sub-mesh parts together
            local _primary
            if plane:IsA("BasePart") then
                _primary = plane
                plane.Transparency = 0
                plane.Anchored = false
                plane.CanCollide = false
            elseif plane:IsA("Model") then
                _primary = plane.PrimaryPart or plane:FindFirstChildWhichIsA("BasePart")
            end
            for _, _p in plane:GetDescendants() do
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
        plane.Parent = container.Parent
    end

    -- GameManager.SendMessage("RestartGame", 5) — GameManager is a
    -- LocalScript with no module export, so inline its restartGame
    -- behaviour (wait, then LoadCharacter) here on the server.
    task.delay(5, function()
        if player.Parent then player:LoadCharacter() end
    end)

    if hitModel.Parent then hitModel:Destroy() end

    -- cutsceneCamera Camera/AudioListener enable — Roblox cameras are
    -- per-client; flag via workspace attributes so a client switch can
    -- act on it. AudioListener has no direct Roblox equivalent.
    if cutsceneCamera then
        workspace:SetAttribute("CutsceneCameraName", cutsceneCamera.Name)
    end
    workspace:SetAttribute("CutsceneActive", true)

    container:Destroy()
end

if trigger then
    trigger.Touched:Connect(onTriggerEnter)
end