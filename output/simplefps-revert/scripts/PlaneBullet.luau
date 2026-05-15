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

-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

-- _AutoBulletRaycastInjected: bullet coherence pack
-- Stud-space velocity + anti-gravity + raycast hit detection.
-- Replaces the AI transpile of Unity's PlaneBullet.cs
-- (rb.AddRelativeForce + OnCollisionEnter), which stacks three bugs
-- in stud-space Roblox physics: 5.7x slow velocity, 5.6x strong
-- gravity, and Touched tunneling at high speeds.
local Debris = game:GetService("Debris")
local RunService = game:GetService("RunService")
local Players = game:GetService("Players")

local container = script.Parent

-- Template-guard. The bullet template Script has ``RunContext = Server``,
-- so Roblox runs it as a server script regardless of parent — including
-- when it sits in ``ReplicatedStorage.Templates`` at place load. Without
-- this guard, the template's own script applies velocity to the template
-- and ``Debris:AddItem(container, fadeTime)`` destroys the template
-- ``fadeTime`` seconds in. Subsequent ``Turret.luau`` ``template:Clone()``
-- calls then return a Part with no Script child (clones of a destroyed
-- instance), so the spawned bullets fall under gravity with no velocity
-- and never damage the player.
do
    local p = container and container.Parent
    if p and (p.Name == "Templates" or p:IsA("ServerStorage")
        or p:IsA("ReplicatedStorage")) then
        return
    end
end

if container:IsA("Model") and not container.PrimaryPart then
    container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
    if container:IsA("Model") then return container:GetPivot() end
    return container.CFrame
end
local function getRootPart()
    if container:IsA("Model") then
        return container.PrimaryPart or container:FindFirstChildWhichIsA("BasePart")
    end
    return container
end

-- Read serialized MonoBehaviour overrides from the part's
-- attributes; the converter's ``_extract_monobehaviour_attributes``
-- emits ``fadeTime``/``force``/``damage`` as float attributes so
-- prefab/instance inspector tuning carries through to runtime.
-- Unity-canonical defaults are the fallback when an attribute is
-- absent (e.g. older outputs or a clone whose attributes were
-- stripped at clone time).
local _attrHost = container:IsA("Model") and (container.PrimaryPart or container) or container
local fadeTime = _attrHost:GetAttribute("fadeTime") or 6
local force = _attrHost:GetAttribute("force") or 200
local damage = _attrHost:GetAttribute("damage") or 10
local STUDS_PER_METER = 3.571

local rootPart = getRootPart()
if not rootPart then return end

rootPart.Anchored = false

-- Anti-gravity: cancel Roblox's 196 studs/s² so bullets fly straight
-- like Unity's near-massless impulse (gravity barely affects them
-- across the bullet's fadeTime in Unity's 9.81 m/s² space).
local att0 = Instance.new("Attachment")
att0.Name = "_BulletAtt0"
att0.Parent = rootPart
local antiG = Instance.new("VectorForce")
antiG.Force = Vector3.new(0, workspace.Gravity * rootPart.AssemblyMass, 0)
antiG.RelativeTo = Enum.ActuatorRelativeTo.World
antiG.Attachment0 = att0
antiG.ApplyAtCenterOfMass = true
antiG.Parent = rootPart

-- Resolve aim direction. Bullets spawn at the Unity ``Origin`` empty,
-- but its local rotation within the parent ``Weapon`` varies across
-- prefab instances (some have identity orientation, some are flipped
-- 158° — measured live on SimpleFPS turrets). Using the bullet's own
-- LookVector fires backward on the rotated instances. Use the parent
-- Weapon (or whatever container is two ancestors up — same depth as
-- the Turret.luau ``firstStructuralChild(firstStructuralChild(...))``
-- climb) which Turret.luau aims via ``CFrame.lookAt(weaponPos, targetPos)``
-- every frame, so its LookVector reliably points at the player.
local _aimCF = getCFrame()
do
    local _ancestor = container.Parent
    while _ancestor and _ancestor ~= workspace do
        if (_ancestor:IsA("Model") or _ancestor:IsA("BasePart"))
            and _ancestor.Name == "Weapon" then
            _aimCF = _ancestor:IsA("Model")
                and _ancestor:GetPivot() or _ancestor.CFrame
            break
        end
        _ancestor = _ancestor.Parent
    end
end

-- Push the bullet 3 studs forward of the muzzle BEFORE applying
-- velocity. Unity's ``Origin`` empty has no collider so spawning
-- inside the weapon's mesh bounding volume is fine in Unity, but in
-- Roblox the bullet's first-frame raycast back-traces from this
-- in-mesh position and self-destructs against the weapon. Teleport
-- via ``container.CFrame = …`` AFTER applying velocity also wipes
-- ``AssemblyLinearVelocity`` back to zero — so order matters: push,
-- THEN velocity.
do
    local _pushedCF = _aimCF + _aimCF.LookVector * 3
    if container:IsA("Model") then
        container:PivotTo(_pushedCF)
    else
        container.CFrame = _pushedCF
    end
end

-- Initial velocity in stud-space (Unity m/s × STUDS_PER_METER).
-- Computed from the aim CFrame (parent Weapon when found, falling
-- back to the bullet's own CFrame for non-turret bullets).
rootPart.AssemblyLinearVelocity = _aimCF.LookVector * (force * STUDS_PER_METER)

-- Trail for visible trajectory
local att1 = Instance.new("Attachment")
att1.Name = "_TrailAtt1"
att1.Position = Vector3.new(0, 0, 0.5)
att1.Parent = rootPart
local att2 = Instance.new("Attachment")
att2.Name = "_TrailAtt2"
att2.Position = Vector3.new(0, 0, -0.5)
att2.Parent = rootPart
local trail = Instance.new("Trail")
trail.Attachment0 = att1
trail.Attachment1 = att2
trail.Lifetime = 0.4
trail.MinLength = 0
trail.WidthScale = NumberSequence.new({
    NumberSequenceKeypoint.new(0, 1),
    NumberSequenceKeypoint.new(1, 0),
})
trail.Color = ColorSequence.new(Color3.fromRGB(255, 100, 50))
trail.Transparency = NumberSequence.new({
    NumberSequenceKeypoint.new(0, 0),
    NumberSequenceKeypoint.new(1, 1),
})
trail.Parent = rootPart

Debris:AddItem(container, fadeTime)

local _ReplicatedStorage = game:GetService("ReplicatedStorage")
local _explosionTemplate = (function()
    local t = _ReplicatedStorage:FindFirstChild("Templates")
    return t and t:FindFirstChild("Explosion")
end)()
local function _spawnExplosionAt(originPos)
    if not _explosionTemplate then return end
    local clone = _explosionTemplate:Clone()

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
        clone:PivotTo(CFrame.new(originPos))
    elseif clone:IsA("BasePart") then
        clone.CFrame = CFrame.new(originPos)
    end
    clone.Parent = workspace
    Debris:AddItem(clone, 2)
end
local function applyAreaDamage(originPos)
    local radius = 2 * STUDS_PER_METER
    for _, player in ipairs(Players:GetPlayers()) do
        local char = player.Character
        local hrp = char and char:FindFirstChild("HumanoidRootPart")
        if hrp and (hrp.Position - originPos).Magnitude <= radius then
            local model = hrp.Parent
            model:SetAttribute("TakeDamage", damage)
            local humanoid = model:FindFirstChildOfClass("Humanoid")
            if humanoid then humanoid:TakeDamage(damage) end
        end
    end
end

-- Raycast hit detection: cast segment from previous-frame position to
-- current each heartbeat. Catches tunneling that Touched misses.
local prevPos = rootPart.Position
local consumed = false
local rp = RaycastParams.new()
rp.FilterDescendantsInstances = {container}
rp.FilterType = Enum.RaycastFilterType.Exclude

local function applyHit(model, impactPos)
    _spawnExplosionAt(impactPos)
    applyAreaDamage(impactPos)
    container:Destroy()
end

local conn
conn = RunService.Heartbeat:Connect(function()
    if consumed or not rootPart.Parent then
        if conn then conn:Disconnect() end
        return
    end
    local curPos = rootPart.Position
    local segment = curPos - prevPos
    if segment.Magnitude > 0 then
        local result = workspace:Raycast(prevPos, segment, rp)
        if result then
            local inst = result.Instance
            local model = inst:FindFirstAncestorOfClass("Model")
            if model and model:FindFirstChildOfClass("Humanoid") then
                local player = Players:GetPlayerFromCharacter(model)
                if player or inst:HasTag("Player") or model.Name == "Player" then
                    consumed = true
                    applyHit(model, result.Position)
                    return
                end
            else
                -- Hit non-character (terrain, wall, prop). Splash
                -- bullets explode here too; direct-hit bullets just
                -- despawn. Behavior is template-controlled by
                -- ``non_char_impact_body``.
                consumed = true
                _spawnExplosionAt(result.Position)
                applyAreaDamage(result.Position)
                container:Destroy()
                return
            end
        end
    end
    prevPos = curPos
end)
