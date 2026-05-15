local RunService = game:GetService("RunService")

local container = script.Parent

if container:IsA("Model") and not container.PrimaryPart then
	container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
	if container:IsA("Model") then return container:GetPivot() end
	return container.CFrame
end

local function setCFrame(cf)
	if container:IsA("Model") then container:PivotTo(cf) else container.CFrame = cf end
end

local FireLight = {}
-- Guard: skip runtime code if script is in ReplicatedStorage
if not (script.Parent:IsA("BasePart") or script.Parent:IsA("Model") or script.Parent:IsA("Folder")) then
    return FireLight
end


local m_Rnd = 0
local m_Burning = true
local m_Light = nil

local startCFrame = getCFrame()

local function start()
	m_Rnd = math.random() * 100
	m_Light = container:FindFirstChildWhichIsA("Light")
end

function FireLight.Extinguish()
	m_Burning = false
	if m_Light then
		m_Light.Enabled = false
	end
end

start()

RunService.Heartbeat:Connect(function(dt)
	if m_Burning then
		local t = tick()
		if m_Light then
			m_Light.Brightness = 2 * math.noise(m_Rnd + t, m_Rnd + 1 + t)
		end
		local x = math.noise(m_Rnd + 0 + t * 2, m_Rnd + 1 + t * 2) - 0.5
		local y = math.noise(m_Rnd + 2 + t * 2, m_Rnd + 3 + t * 2) - 0.5
		local z = math.noise(m_Rnd + 4 + t * 2, m_Rnd + 5 + t * 2) - 0.5
		local localOffset = Vector3.new(0, 1, 0) + Vector3.new(x, y, z)
		setCFrame(startCFrame * CFrame.new(localOffset))
	end
end)

return FireLight