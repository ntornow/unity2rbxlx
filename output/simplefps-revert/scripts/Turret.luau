local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local Debris = game:GetService("Debris")

local container = script.Parent

if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getChildByIndex(parent, index)
    local children = parent:GetChildren()
    -- Filter out non-spatial children (scripts, sounds, etc) so the
    -- Unity GetChild(0) "first transform child" semantics line up.
    local spatial = {}
    for _, c in ipairs(children) do
        if c:IsA("Model") or c:IsA("BasePart") then
            table.insert(spatial, c)
        end
    end
    return spatial[index]
end

local function getCFrame(obj)
    if obj:IsA("Model") then return obj:GetPivot() end
    return obj.CFrame
end

local function setCFrame(obj, cf)
    if obj:IsA("Model") then
        obj:PivotTo(cf)
    else
        obj.CFrame = cf
    end
end

local function getPosition(obj)
    return getCFrame(obj).Position
end

-- Configuration (from serialized fields)
local rotate = true
local rotationSpeed = 125
local engagedSpeed = 2.5
local shootCooldown = 1
local maxHealth = 5
local searchTime = 3
local hitSoundPath = "rbxassetid://122395380086508"

-- Prefab references
local turretBulletTemplate = ReplicatedStorage:FindFirstChild("Templates") and ReplicatedStorage.Templates:FindFirstChild("TurretBullet")
local explosionTemplate = ReplicatedStorage:FindFirstChild("Templates") and ReplicatedStorage.Templates:FindFirstChild("Explosion")
local smokeTemplate = ReplicatedStorage:FindFirstChild("Templates") and ReplicatedStorage.Templates:FindFirstChild("Smoke")

-- State enum
local State = {
    Default = 0,
    Engaged = 1,
    Search = 2,
}

-- Object state
local state = State.Default
local health = maxHealth
local currentTarget = nil
local rotateDir = nil
local alive = true

-- Resolve hierarchy: base / weapon / origin
local tBase = getChildByIndex(container, 1)
local tWeapon = tBase and getChildByIndex(tBase, 1) or nil
local tOrigin = tWeapon and getChildByIndex(tWeapon, 1) or nil

-- Resolve audio source
local source = container:FindFirstChildWhichIsA("Sound")
    or (tBase and tBase:FindFirstChildWhichIsA("Sound"))
    or nil

-- Resolve trigger collider radius
local function getSightRadius()
    local colliderObj = container:FindFirstChild("Collider", true)
    if colliderObj and colliderObj:IsA("BasePart") then
        return math.max(colliderObj.Size.X, colliderObj.Size.Y, colliderObj.Size.Z) / 2
    end
    return 50
end

local sightRadius = getSightRadius()

-- Locate the trigger part for player detection
local TRIGGER_NAMES = {"Collider", "Trigger", "TriggerZone", "Detector",
    "Sensor", "Hitbox", "Range", "ProximityVolume", "PickupTouchDetector"}

local function findTriggerPart(parent)
    for _, n in ipairs(TRIGGER_NAMES) do
        local p = parent:FindFirstChild(n, true)
        if p and p:IsA("BasePart") then return p end
    end
    for _, c in ipairs(parent:GetDescendants()) do
        if c:IsA("BasePart") and c.Transparency >= 1 and not c:IsA("MeshPart") then
            return c
        end
    end
    return nil
end

local triggerPart = findTriggerPart(container)

-- Forward declarations
local defaultUpdate
local engagedUpdate
local engagedShooting
local searchUpdate

-- Helpers
local function isPlayerPart(part)
    local model = part:FindFirstAncestorOfClass("Model")
    while model do
        if Players:GetPlayerFromCharacter(model) then return model end
        model = model.Parent and model.Parent:FindFirstAncestorOfClass("Model")
    end
    return nil
end

local function getCharacterFromTouch(part)
    return isPlayerPart(part)
end

local function vectorAngle(a, b)
    local denom = a.Magnitude * b.Magnitude
    if denom == 0 then return 0 end
    local cosTheta = math.clamp(a:Dot(b) / denom, -1, 1)
    return math.deg(math.acos(cosTheta))
end

local function quaternionLerp(cfA, cfB, t)
    return cfA:Lerp(cfB, t)
end

local function takeDamage()
    health = health - 1
    if source then
        source.PlaybackSpeed = math.random() * 0.4 + 0.8
        source.SoundId = hitSoundPath
        source:Play()
    end
    if health <= 0 then
        alive = false
        state = -1
        local basePos = tBase and getPosition(tBase) or getPosition(container)
        if explosionTemplate then
            local explosion = explosionTemplate:Clone()

                -- Fix clone visibility and weld sub-mesh parts together
                local _primary
                if explosion:IsA("BasePart") then
                    _primary = explosion
                    explosion.Transparency = 0
                    explosion.Anchored = false
                    explosion.CanCollide = false
                elseif explosion:IsA("Model") then
                    _primary = explosion.PrimaryPart or explosion:FindFirstChildWhichIsA("BasePart")
                end
                for _, _p in explosion:GetDescendants() do
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
            if explosion:IsA("Model") then
                explosion:PivotTo(CFrame.new(basePos))
            elseif explosion:IsA("BasePart") then
                explosion.CFrame = CFrame.new(basePos)
            end
            explosion.Parent = workspace
        end
        if smokeTemplate then
            local smoke = smokeTemplate:Clone()

                -- Fix clone visibility and weld sub-mesh parts together
                local _primary
                if smoke:IsA("BasePart") then
                    _primary = smoke
                    smoke.Transparency = 0
                    smoke.Anchored = false
                    smoke.CanCollide = false
                elseif smoke:IsA("Model") then
                    _primary = smoke.PrimaryPart or smoke:FindFirstChildWhichIsA("BasePart")
                end
                for _, _p in smoke:GetDescendants() do
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
            if smoke:IsA("Model") then
                smoke:PivotTo(CFrame.new(basePos))
            elseif smoke:IsA("BasePart") then
                smoke.CFrame = CFrame.new(basePos)
            end
            smoke.Parent = workspace
        end
        if tWeapon then tWeapon:Destroy() end
        if tBase then tBase:Destroy() end
        script:Destroy()
    end
end

container:GetAttributeChangedSignal("TakeDamage"):Connect(function()
    if container:GetAttribute("TakeDamage") then
        container:SetAttribute("TakeDamage", false)
        if alive then takeDamage() end
    end
end)

defaultUpdate = function()
    state = State.Default
    while alive and state == State.Default do
        if rotate and tBase then
            local dt = RunService.Heartbeat:Wait()
            local angleRad = math.rad(dt * rotationSpeed)
            local axis = rotateDir or Vector3.new(0, 1, 0)
            local rotation
            if axis.Y >= 0 then
                rotation = CFrame.Angles(0, angleRad, 0)
            else
                rotation = CFrame.Angles(0, -angleRad, 0)
            end
            setCFrame(tBase, getCFrame(tBase) * rotation)
        else
            RunService.Heartbeat:Wait()
        end
    end
end

engagedShooting = function()
    task.wait(shootCooldown)
    while alive and state == State.Engaged do
        if turretBulletTemplate and tOrigin then
            local bullet = turretBulletTemplate:Clone()

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
            local originCF = getCFrame(tOrigin)
            if bullet:IsA("Model") then
                bullet:PivotTo(originCF)
            elseif bullet:IsA("BasePart") then
                bullet.CFrame = originCF
            end
            bullet.Parent = workspace
        end
        if source then
            source.PlaybackSpeed = math.random() * 0.4 + 0.8
            source:Play()
        end
        task.wait(shootCooldown)
    end
end

engagedUpdate = function(target)
    state = State.Engaged
    currentTarget = target
    task.spawn(engagedShooting)
    while alive and state == State.Engaged and target and target.Parent do
        local dt = RunService.Heartbeat:Wait()
        local targetPos
        if target:IsA("Model") then
            targetPos = target:GetPivot().Position
        elseif target:IsA("BasePart") then
            targetPos = target.Position
        else
            break
        end
        if tBase then
            local basePos = getPosition(tBase)
            local dir = targetPos - basePos
            if dir.Magnitude > 0.001 then
                local desired = CFrame.lookAt(basePos, basePos + dir)
                local current = getCFrame(tBase)
                local newCF = current:Lerp(desired, math.clamp(dt * engagedSpeed, 0, 1))
                -- Preserve position, only apply rotation
                setCFrame(tBase, CFrame.new(basePos) * (newCF - newCF.Position))
            end
        end
        if tWeapon and tBase then
            local weaponPos = getPosition(tWeapon)
            local lookCF = CFrame.lookAt(weaponPos, targetPos)
            -- Compute local pitch relative to tBase
            local baseCF = getCFrame(tBase)
            local localCF = baseCF:ToObjectSpace(lookCF)
            local rx, _, _ = localCF:ToOrientation()
            local pitchDeg = math.deg(rx)
            pitchDeg = math.clamp(pitchDeg, -30, 30)
            local clampedLocal = CFrame.Angles(math.rad(pitchDeg), 0, 0)
            local newWeaponCF = baseCF * (CFrame.new(baseCF:PointToObjectSpace(weaponPos)) * clampedLocal)
            setCFrame(tWeapon, newWeaponCF)
        end
    end
end

searchUpdate = function()
    state = State.Search
    local time = 0
    while alive and state == State.Search and time < searchTime do
        local dt = RunService.Heartbeat:Wait()
        time = time + dt
    end
    if alive and state == State.Search then
        task.spawn(defaultUpdate)
    end
end

-- Player detection via touch (OnTriggerStay equivalent)
if triggerPart then
    local touchingPlayers = {}

    triggerPart.Touched:Connect(function(other)
        local character = getCharacterFromTouch(other)
        if not character then return end
        touchingPlayers[character] = true
        if state ~= State.Engaged and tBase then
            local targetPart = character:FindFirstChild("HumanoidRootPart") or character:FindFirstChildWhichIsA("BasePart")
            if not targetPart then return end
            local dir = targetPart.Position - getPosition(tBase)
            local fwd = getCFrame(tBase).LookVector
            local ang = vectorAngle(dir, fwd)
            if ang < 55 then
                local rayParams = RaycastParams.new()
                rayParams.FilterDescendantsInstances = {container}
                rayParams.FilterType = Enum.RaycastFilterType.Exclude
                local result = workspace:Raycast(getPosition(tBase), dir.Unit * sightRadius, rayParams)
                if result then
                    local hitChar = getCharacterFromTouch(result.Instance)
                    if hitChar == character then
                        task.spawn(engagedUpdate, character)
                    end
                end
            end
        end
    end)

    triggerPart.TouchEnded:Connect(function(other)
        local character = getCharacterFromTouch(other)
        if not character then return end
        touchingPlayers[character] = nil
        if next(touchingPlayers) == nil and state == State.Engaged then
            task.spawn(searchUpdate)
        end
    end)
end

-- Start
health = maxHealth
rotateDir = (math.random() > 0.5) and Vector3.new(0, 1, 0) or Vector3.new(0, -1, 0)
task.spawn(defaultUpdate)

-- _AutoFpsTurretPoll: Unity OnTriggerStay equivalent. Roblox
-- .Touched fires only on initial contact; a rotating turret in the
-- Default state needs to re-test the line-of-sight cone every frame
-- in case its facing changed since the player touched the trigger.
do
    local _lastCheck = 0
    RunService.Heartbeat:Connect(function()
        if state == State.Engaged then return end
        if tick() - _lastCheck < 0.15 then return end
        _lastCheck = tick()
        if not tBase then return end
        local _basePos = getPosition(tBase)
        local _fwd = getCFrame(tBase).LookVector
        local _hits = workspace:GetPartBoundsInRadius(_basePos, sightRadius)
        for _, _hit in ipairs(_hits) do
            if isPlayerPart(_hit) then
                local _char = getCharacterFromTouch(_hit)
                if _char then
                    local _tp = _char:FindFirstChild("HumanoidRootPart") or _hit
                    local _dir = _tp.Position - _basePos
                    if vectorAngle(_dir, _fwd) < 55 then
                        local _rp = RaycastParams.new()
                        _rp.FilterDescendantsInstances = {container}
                        _rp.FilterType = Enum.RaycastFilterType.Exclude
                        local _res = workspace:Raycast(_basePos, _dir.Unit * sightRadius, _rp)
                        if _res and isPlayerPart(_res.Instance) then
                            task.spawn(engagedUpdate, _char)
                            return
                        end
                    end
                end
            end
        end
    end)
end