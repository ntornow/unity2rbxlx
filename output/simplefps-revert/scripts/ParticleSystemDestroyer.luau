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

local RunService = game:GetService("RunService")
local Debris = game:GetService("Debris")

local container = script.Parent

local ParticleSystemDestroyer = {}

local minDuration = 8
local maxDuration = 10

local m_MaxLifetime = 0
local m_EarlyStop = false

local function getParticleSystems(root)
	local systems = {}
	for _, desc in ipairs(root:GetDescendants()) do
		if desc:IsA("ParticleEmitter") or desc:IsA("ParticleSystem") then
			table.insert(systems, desc)
		end
	end
	return systems
end

local function broadcastExtinguish(root)
	for _, desc in ipairs(root:GetDescendants()) do
		desc:SetAttribute("Extinguish", true)
	end
end

function ParticleSystemDestroyer.Stop()
	m_EarlyStop = true
end

task.spawn(function()
	local systems = getParticleSystems(container)

	for _, system in ipairs(systems) do
		local lifetime = system:GetAttribute("StartLifetime")
		if typeof(lifetime) ~= "number" then
			lifetime = system.Lifetime and system.Lifetime.Max or 0
		end
		m_MaxLifetime = math.max(lifetime, m_MaxLifetime)
	end

	local stopTime = os.clock() + (math.random() * (maxDuration - minDuration) + minDuration)

	while os.clock() < stopTime and not m_EarlyStop do
		RunService.Heartbeat:Wait()
	end

	print("stopping " .. container.Name)

	for _, system in ipairs(systems) do
		system.Enabled = false
	end
	broadcastExtinguish(container)

	task.wait(m_MaxLifetime)

	container:Destroy()
end)

return ParticleSystemDestroyer