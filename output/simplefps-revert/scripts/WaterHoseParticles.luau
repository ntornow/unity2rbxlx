-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

-- Services
local RunService = game:GetService("RunService")

local container = script.Parent
if container:IsA("Model") and not container.PrimaryPart then
	container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local lastSoundTime = 0
local force = 1

local function getHostPart()
	if container:IsA("BasePart") then return container end
	return container:FindFirstChildWhichIsA("BasePart")
end

local m_ParticleSystem = nil

local function start()
	local host = getHostPart()
	if host then
		m_ParticleSystem = host:FindFirstChildWhichIsA("ParticleEmitter")
	end
end

start()

local function broadcastExtinguish(target)
	if not target then return end
	target:SetAttribute("Extinguish", true)
	for _, desc in ipairs(target:GetDescendants()) do
		desc:SetAttribute("Extinguish", true)
	end
end

local function onParticleCollision(otherPart)
	if not otherPart or not otherPart:IsA("BasePart") then return end

	-- UNCONVERTED: Unity's ParticleSystem.GetCollisionEvents has no Roblox
	-- equivalent — Roblox ParticleEmitters do not produce per-particle
	-- collision events. Approximate by treating each Touched call as one
	-- collision with the particle system's host part velocity.
	local numCollisionEvents = 1
	local i = 0

	while i < numCollisionEvents do
		if tick() > lastSoundTime + 0.2 then
			lastSoundTime = tick()
		end

		local col = otherPart
		if not col.Anchored then
			local host = getHostPart()
			local vel = host and host.AssemblyLinearVelocity or Vector3.new()
			col:ApplyImpulse(vel * force)
		end

		broadcastExtinguish(otherPart)
		if otherPart.Parent then
			broadcastExtinguish(otherPart.Parent)
		end

		i = i + 1
	end
end

local touchedConn = nil
local function wireTouched()
	local host = getHostPart()
	if not host then return end
	if touchedConn then touchedConn:Disconnect() end
	touchedConn = host.Touched:Connect(onParticleCollision)
end

wireTouched()

script.Destroying:Connect(function()
	if touchedConn then touchedConn:Disconnect() end
end)