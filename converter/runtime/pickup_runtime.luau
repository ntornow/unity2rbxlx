-- Pickup Runtime: handles all pickups with IsPickup attribute
-- Finds PickupTouchDetector parts and the transpiled Pickup scripts,
-- ensuring every pickup has touch-based collection working.

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local Debris = game:GetService("Debris")

local ROTATION_SPEED = 100 -- degrees/sec

local function setupPickup(pickupModel)
	local touchDetector = pickupModel:FindFirstChild("PickupTouchDetector")
	if not touchDetector or not touchDetector:IsA("BasePart") then
		-- Try using the model's first BasePart as touch target
		touchDetector = pickupModel:FindFirstChildWhichIsA("BasePart")
	end
	if not touchDetector then return end

	-- Skip if a transpiled Pickup script already handles this
	for _, desc in pickupModel:GetDescendants() do
		if desc:IsA("Script") and desc.Name == "Pickup" then
			return
		end
	end

	local itemType = pickupModel:GetAttribute("ItemType")
		or touchDetector:GetAttribute("ItemType")
		or pickupModel.Name:gsub("Pickup", ""):gsub("pickup", "")
	if itemType == "" then itemType = "Generic" end

	-- Find the visible mesh to animate (rotate + bob)
	local visualPart = nil
	for _, child in pickupModel:GetChildren() do
		if child:IsA("Model") or (child:IsA("BasePart") and child.Transparency < 0.5) then
			visualPart = child
			break
		end
	end

	-- Bob animation
	if visualPart and visualPart:IsA("BasePart") then
		local startY = visualPart.Position.Y
		task.spawn(function()
			local t = 0
			while visualPart and visualPart.Parent do
				local dt = RunService.Heartbeat:Wait()
				t = t + dt
				local offset = math.sin(t * 2) * 0.5
				visualPart.CFrame = visualPart.CFrame + Vector3.new(0, offset * dt * 2, 0)
				visualPart.CFrame = visualPart.CFrame * CFrame.Angles(0, math.rad(ROTATION_SPEED * dt), 0)
			end
		end)
	end

	-- Touch detection
	local collected = false
	touchDetector.Touched:Connect(function(otherPart)
		if collected then return end
		local character = otherPart.Parent
		local humanoid = character and character:FindFirstChildWhichIsA("Humanoid")
		if not humanoid then return end
		local player = Players:GetPlayerFromCharacter(character)
		if not player then return end

		collected = true
		character:SetAttribute("GetItem", itemType)
		pickupModel:Destroy()
	end)
end

-- Find all existing pickups
for _, desc in workspace:GetDescendants() do
	if desc:GetAttribute("IsPickup") and desc:IsA("Model") then
		task.spawn(setupPickup, desc)
	end
end

-- Handle pickups added at runtime
workspace.DescendantAdded:Connect(function(desc)
	if desc:GetAttribute("IsPickup") and desc:IsA("Model") then
		task.defer(setupPickup, desc)
	end
end)
