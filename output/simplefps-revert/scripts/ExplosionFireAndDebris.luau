-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

local RunService = game:GetService("RunService")

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

local debrisPrefabs = {}
local firePrefab = nil
local numDebrisPieces = 0
local numFires = 0

local function findParticleMultiplier()
	for _, child in ipairs(container:GetChildren()) do
		if child:IsA("Script") or child:IsA("LocalScript") or child:IsA("ModuleScript") then
			if child.Name == "ParticleSystemMultiplier" then
				return child:GetAttribute("multiplier") or 1
			end
		end
	end
	local attr = container:GetAttribute("ParticleSystemMultiplier_multiplier")
	if attr ~= nil then return attr end
	return 1
end

local function randomInsideUnitSphere()
	while true do
		local x = math.random() * 2 - 1
		local y = math.random() * 2 - 1
		local z = math.random() * 2 - 1
		local v = Vector3.new(x, y, z)
		if v.Magnitude <= 1 then
			return v
		end
	end
end

local function randomOnUnitSphere()
	local v = randomInsideUnitSphere()
	if v.Magnitude < 1e-6 then
		return Vector3.new(1, 0, 0)
	end
	return v.Unit
end

local function randomRotation()
	local x = math.random() * 2 * math.pi
	local y = math.random() * 2 * math.pi
	local z = math.random() * 2 * math.pi
	return CFrame.Angles(x, y, z)
end

local function instantiate(prefab, pos, rot)
	if prefab == nil then return nil end
	local clone = prefab:Clone()

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
		if clone.PrimaryPart then
			clone:PivotTo(CFrame.new(pos) * rot)
		end
	elseif clone:IsA("BasePart") then
		clone.CFrame = CFrame.new(pos) * rot
	end
	clone.Parent = workspace
	return clone
end

local function addFire(t, pos, normal)
	pos = pos + normal * 0.5
	local fire = instantiate(firePrefab, pos, CFrame.new())
	if fire and t then
		fire.Parent = t
	end
end

local function raycastPart(part, origin, dir, maxDist)
	local params = RaycastParams.new()
	params.FilterType = Enum.RaycastFilterType.Include
	if part:IsA("Model") then
		params.FilterDescendantsInstances = {part}
	else
		params.FilterDescendantsInstances = {part}
	end
	return workspace:Raycast(origin, dir.Unit * maxDist, params)
end

local function start()
	local multiplier = findParticleMultiplier()

	for n = 0, math.floor(numDebrisPieces * multiplier) - 1 do
		if #debrisPrefabs > 0 then
			local prefab = debrisPrefabs[math.random(1, #debrisPrefabs)]
			local pos = getPosition() + randomInsideUnitSphere() * 3 * multiplier
			local rot = randomRotation()
			instantiate(prefab, pos, rot)
		end
	end

	RunService.Heartbeat:Wait()

	local r = 10 * multiplier
	local origin = getPosition()
	local cols = workspace:GetPartBoundsInRadius(origin, r)
	for _, col in ipairs(cols) do
		if numFires > 0 then
			local target
			if col:IsA("BasePart") then
				target = col.Position
			else
				target = col:GetPivot().Position
			end
			local dir = target - origin
			if dir.Magnitude > 1e-6 then
				local hit = raycastPart(col, origin, dir, r)
				if hit then
					addFire(col, hit.Position, hit.Normal)
					numFires = numFires - 1
				end
			end
		end
	end

	local testR = 0
	while numFires > 0 and testR < r do
		local rayOrigin = origin + Vector3.new(0, 1, 0)
		local rayDir = randomOnUnitSphere()
		local params = RaycastParams.new()
		params.FilterType = Enum.RaycastFilterType.Exclude
		params.FilterDescendantsInstances = {container}
		local hit = workspace:Raycast(rayOrigin, rayDir * testR, params)
		if hit then
			addFire(nil, hit.Position, hit.Normal)
			numFires = numFires - 1
		end
		testR = testR + r * 0.1
	end
end

task.spawn(start)