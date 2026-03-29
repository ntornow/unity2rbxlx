-- CinemachineRuntime: Virtual camera system emulation for Roblox.
--
-- Reads CinemachineVCam attributes from parts in workspace and manages
-- camera transitions based on priority. Highest-priority active vcam wins.
--
-- Attributes used:
--   CinemachineVCam (bool)   -- marks a part as a virtual camera
--   CinemachinePriority (int) -- higher = more important
--   CinemachineFOV (number)  -- field of view
--   CinemachineFollow (string) -- name of part to follow
--   CinemachineLookAt (string) -- name of part to look at
--   CinemachineBody (string) -- "Transposer" or "FramingTransposer"
--   CinemachineFollowOffset (string) -- "x,y,z" offset from follow target

local RunService = game:GetService("RunService")
local Players = game:GetService("Players")

local camera = workspace.CurrentCamera

-- Collect all virtual cameras from workspace
local function discoverVCams()
	local vcams = {}
	for _, desc in workspace:GetDescendants() do
		if desc:IsA("BasePart") and desc:GetAttribute("CinemachineVCam") then
			table.insert(vcams, desc)
		end
	end
	-- Sort by priority (highest first)
	table.sort(vcams, function(a, b)
		return (a:GetAttribute("CinemachinePriority") or 0) > (b:GetAttribute("CinemachinePriority") or 0)
	end)
	return vcams
end

local function findTarget(name)
	if not name or name == "" then return nil end
	return workspace:FindFirstChild(name, true)
end

local function parseOffset(offsetStr)
	if not offsetStr or offsetStr == "" then
		return Vector3.new(0, 5, -10) -- default follow offset
	end
	local parts = string.split(offsetStr, ",")
	if #parts >= 3 then
		return Vector3.new(tonumber(parts[1]) or 0, tonumber(parts[2]) or 5, tonumber(parts[3]) or -10)
	end
	return Vector3.new(0, 5, -10)
end

local activeVCam = nil
local currentFOV = 70
local targetFOV = 70
local blendSpeed = 2.0

local function updateCamera(dt)
	local vcams = discoverVCams()
	if #vcams == 0 then return end

	local bestVCam = vcams[1]
	if bestVCam ~= activeVCam then
		activeVCam = bestVCam
		targetFOV = activeVCam:GetAttribute("CinemachineFOV") or 70
	end

	if not activeVCam then return end

	-- Smooth FOV transition
	currentFOV = currentFOV + (targetFOV - currentFOV) * math.min(1, dt * blendSpeed * 3)
	camera.FieldOfView = currentFOV

	-- Follow target
	local followName = activeVCam:GetAttribute("CinemachineFollow") or ""
	local lookAtName = activeVCam:GetAttribute("CinemachineLookAt") or ""

	local followTarget = findTarget(followName)
	local lookAtTarget = findTarget(lookAtName)

	-- If follow target is "Player", follow local player's character
	if followName == "Player" or followName == "" then
		local player = Players.LocalPlayer
		if player and player.Character then
			local hrp = player.Character:FindFirstChild("HumanoidRootPart")
			if hrp then
				followTarget = hrp
			end
		end
	end
	if lookAtName == "Player" or lookAtName == "" then
		local player = Players.LocalPlayer
		if player and player.Character then
			local hrp = player.Character:FindFirstChild("HumanoidRootPart")
			if hrp then
				lookAtTarget = hrp
			end
		end
	end

	if followTarget then
		local offset = parseOffset(activeVCam:GetAttribute("CinemachineFollowOffset"))
		-- Apply offset relative to follow target's facing direction
		local targetPos = followTarget.Position
		local camPos = targetPos + followTarget.CFrame:VectorToWorldSpace(offset)

		local lookAt = lookAtTarget and lookAtTarget.Position or targetPos
		local newCFrame = CFrame.lookAt(camPos, lookAt)

		-- Smooth blend
		camera.CameraType = Enum.CameraType.Scriptable
		camera.CFrame = camera.CFrame:Lerp(newCFrame, math.min(1, dt * blendSpeed))
	else
		-- No follow target — position camera at vcam part location
		local lookAt = lookAtTarget and lookAtTarget.Position or (activeVCam.Position + activeVCam.CFrame.LookVector * 10)
		camera.CameraType = Enum.CameraType.Scriptable
		camera.CFrame = camera.CFrame:Lerp(CFrame.lookAt(activeVCam.Position, lookAt), math.min(1, dt * blendSpeed))
	end
end

-- Only run if there are virtual cameras in the scene
task.defer(function()
	task.wait(1) -- Wait for workspace to load
	local vcams = discoverVCams()
	if #vcams > 0 then
		RunService.RenderStepped:Connect(updateCamera)
	end
end)
