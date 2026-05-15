-- Guard: this script expects script.Parent to be a BasePart
if not script.Parent:IsA("BasePart") then return end

local RunService = game:GetService("RunService")

local explosionForce = 4

local container = script.Parent

if container:IsA("Model") and not container.PrimaryPart then
	container.PrimaryPart = container:FindFirstChildWhichIsA("BasePart")
end

local function getCFrame()
	if container:IsA("Model") then return container:GetPivot() end
	return container.CFrame
end

local function getPosition() return getCFrame().Position end

task.spawn(function()
	RunService.Heartbeat:Wait()

	local multiplier = 1

	local r = 10 * multiplier
	local origin = getPosition()
	local cols = workspace:GetPartBoundsInRadius(origin, r)

	local rigidbodies = {}
	local seen = {}
	for _, col in cols do
		if col:IsA("BasePart") and not col.Anchored then
			local assembly = col.AssemblyRootPart
			if assembly and not seen[assembly] then
				seen[assembly] = true
				table.insert(rigidbodies, assembly)
			end
		end
	end

	for _, rb in rigidbodies do
		local toPart = rb.Position - origin
		local distance = toPart.Magnitude
		local direction
		if distance > 0 then
			direction = toPart.Unit
		else
			direction = Vector3.new(0, 1, 0)
		end
		local falloff = math.max(0, 1 - distance / r)
		local upBias = 1 * multiplier
		local impulseDir = (direction + Vector3.new(0, upBias, 0)).Unit
		local magnitude = explosionForce * multiplier * falloff * rb.AssemblyMass
		rb:ApplyImpulse(impulseDir * magnitude)
	end
end)