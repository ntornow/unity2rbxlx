-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

local CollectionService = game:GetService("CollectionService")

local container = script.Parent

local multiplier = 1

local m_Systems = {}

local function collectParticleEmitters(root)
	local emitters = {}
	if root then
		for _, desc in ipairs(root:GetDescendants()) do
			if desc:IsA("ParticleEmitter") then
				table.insert(emitters, desc)
			end
		end
	end
	return emitters
end

local function extinguish()
	for _, system in ipairs(m_Systems) do
		if system and system.Parent then
			system.Enabled = false
		end
	end
end

m_Systems = collectParticleEmitters(container)

container:GetAttributeChangedSignal("Extinguish"):Connect(function()
	if container:GetAttribute("Extinguish") == true then
		extinguish()
		container:SetAttribute("Extinguish", false)
	end
end)

container:SetAttribute("multiplier", multiplier)