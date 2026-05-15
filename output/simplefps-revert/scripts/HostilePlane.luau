-- Services
local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

-- Configuration (from serialized fields)
local shootTimer = 5

-- Container dispatch (HostilePlane is attached to a plane Model)
local container = script.Parent
if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

-- Reference accessors (mirror C# property getters so we always resolve the
-- current instance — Origin may be reparented or replaced at runtime)
local function getOrigin()
    return container:FindFirstChild("Origin") or container:FindFirstChild("Origin", true)
end

local function getSource()
    local s = container:FindFirstChildOfClass("Sound")
    if s then return s end
    local prim = container.PrimaryPart or container:FindFirstChildWhichIsA("BasePart")
    if prim then
        local childSound = prim:FindFirstChildOfClass("Sound")
        if childSound then return childSound end
    end
    local newSound = Instance.new("Sound")
    newSound.SoundId = "rbxassetid://117501363682725"
    newSound.Parent = prim or container
    return newSound
end

-- Server-side Player.instance.transform analogue: find any player's root.
local function findPlayerRoot()
    for _, player in Players:GetPlayers() do
        local char = player.Character
        if char then
            local root = char:FindFirstChild("HumanoidRootPart")
            if root then return root end
        end
    end
    return nil
end

local target = nil

-- TimedShooting coroutine — faithful translation of the while(target!=null) loop.
local function timedShooting()
    while target and target.Parent do
        task.wait(shootTimer)
        if not (target and target.Parent) then break end

        local originRef = getOrigin()
        if originRef and originRef:IsA("BasePart") then
            local templates = ReplicatedStorage:WaitForChild("Templates")
            local ammoPrefab = templates:FindFirstChild("PlaneBullet")
            if ammoPrefab then
                local bullet = ammoPrefab:Clone()

                    -- Fix clone visibility and weld sub-mesh parts together
                    local _primary
                    if bullet:IsA("BasePart") then
                        _primary = bullet
                        bullet.Transparency = 0
                        bullet.Anchored = false
                        bullet.CanCollide = false
                    elseif bullet:IsA("Model") then
                        _primary = bullet.PrimaryPart or bullet:FindFirstChildWhichIsA("BasePart")
                    end
                    for _, _p in bullet:GetDescendants() do
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
                if bullet:IsA("Model") then
                    if not bullet.PrimaryPart then
                        bullet.PrimaryPart = bullet:FindFirstChildWhichIsA("BasePart")
                    end
                    bullet:PivotTo(originRef.CFrame)
                elseif bullet:IsA("BasePart") then
                    bullet.CFrame = originRef.CFrame
                end
                bullet.Parent = workspace
            end
        end

        local source = getSource()
        source.PlaybackSpeed = math.random() * 0.4 + 0.8
        source:Play()
        source.PlaybackSpeed = 1
    end
end

-- Start: acquire target, then start shooting coroutine.
task.spawn(function()
    while not (target and target.Parent) do
        target = findPlayerRoot()
        if not (target and target.Parent) then task.wait(0.5) end
    end
    timedShooting()
end)

-- FixedUpdate analogue: aim Origin at target + up*2.
RunService.Heartbeat:Connect(function(dt)
    if target and target.Parent then
        local originRef = getOrigin()
        if originRef and originRef:IsA("BasePart") then
            local targetPos = target.Position + Vector3.new(0, 2, 0)
            if (targetPos - originRef.Position).Magnitude > 0.001 then
                originRef.CFrame = CFrame.lookAt(originRef.Position, targetPos)
            end
        end
    end
end)