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

-- Find the visible target. Prefer Models (e.g. Rifle, Battery) over Parts;
-- fall back to opaque Parts; last resort any Part. The Pickup container
-- typically has invisible Trigger Parts (Transparency=1) sitting alongside
-- the visible mesh content — the visual target is what should bob and rotate.
local function findVisualTarget(parent)
	for _, c in ipairs(parent:GetChildren()) do
		if c:IsA("Model") and c.Name ~= "MinimapIcon" then return c end
	end
	for _, c in ipairs(parent:GetChildren()) do
		if c:IsA("BasePart") and c.Transparency < 1 then return c end
	end
	for _, c in ipairs(parent:GetChildren()) do
		if c:IsA("BasePart") then return c end
	end
	return nil
end

local function findTriggerPart(parent)
	local d = parent:FindFirstChild("PickupTouchDetector")
	if d and d:IsA("BasePart") then return d end
	local c = parent:FindFirstChild("Collider")
	if c and c:IsA("BasePart") then return c end
	for _, x in ipairs(parent:GetChildren()) do
		if x:IsA("BasePart") and x.Transparency >= 1 then return x end
	end
	return nil
end

local target = findVisualTarget(container)
local trigger = findTriggerPart(container)
-- itemName is serialized on either the script (post-coherence) or the
-- container Model (Unity MonoBehaviour fields land on the parent Part/Model).
local itemName = script:GetAttribute("itemName")
	or (container and container:GetAttribute("itemName"))
	or ""
local rotationSpeed = 100
local source = container:FindFirstChildWhichIsA("Sound")

if not target then return end

local function getPivot()
	if target:IsA("Model") then return target:GetPivot() end
	return target.CFrame
end
local function setPivot(cf)
	if target:IsA("Model") then target:PivotTo(cf) else target.CFrame = cf end
end

-- Models need a PrimaryPart for PivotTo to work consistently.
if target:IsA("Model") and not target.PrimaryPart then
	local p = target:FindFirstChildWhichIsA("BasePart")
	if p then target.PrimaryPart = p end
end

local origin = getPivot().Position
local upPos = origin
local downPos = origin - Vector3.new(0, 0.5, 0)

local function moveDown()
	while target and target.Parent do
		if getPivot().Position.Y <= downPos.Y + 0.05 then break end
		local dt = RunService.Heartbeat:Wait()
		-- Re-fetch pivot AFTER the wait so we don't clobber rotation
		-- updates applied by the concurrent Heartbeat rotator below.
		setPivot(getPivot() + Vector3.new(0, -0.5 * dt, 0))
	end
end
local function moveUp()
	while target and target.Parent do
		if getPivot().Position.Y >= upPos.Y - 0.05 then break end
		local dt = RunService.Heartbeat:Wait()
		setPivot(getPivot() + Vector3.new(0, 0.5 * dt, 0))
	end
end

task.spawn(function()
	while target and target.Parent do moveDown(); moveUp() end
end)

RunService.Heartbeat:Connect(function(dt)
	if target and target.Parent then
		setPivot(getPivot() * CFrame.Angles(0, math.rad(rotationSpeed) * dt, 0))
	end
end)

local _Players = game:GetService("Players")
local _RS = game:GetService("ReplicatedStorage")
-- Pickup→Player must cross the server/client boundary: Pickup is a server
-- Script and Player runs as a LocalScript, so SetAttribute on the client's
-- character won't replicate. Fire a RemoteEvent in ReplicatedStorage instead;
-- rbxlx_writer auto-creates `PickupItemEvent` because this script references
-- it with FireClient. Player's coherence pack adds the OnClientEvent listener.
local _pickupEvent = _RS:FindFirstChild("PickupItemEvent")
local touchPart = trigger or (target:IsA("BasePart") and target or target:FindFirstChildWhichIsA("BasePart"))
if touchPart then
	touchPart.Touched:Connect(function(otherPart)
		local character = otherPart:FindFirstAncestorOfClass("Model")
		local player = character and _Players:GetPlayerFromCharacter(character)
		if player then
			-- Persist the pickup as a server-side Player attribute so server
			-- scripts (e.g. Door checking ``Player.hasKey``) can react. The
			-- client-side Player LocalScript also flips its own LocalPlayer
			-- attribute via the FireClient below, but ``LocalPlayer:SetAttribute``
			-- on the client doesn't replicate to the server — so any server
			-- consumer of ``hasKey``/``hasRifle`` never sees the flag without
			-- this server-side write. Player Object attributes set server-side
			-- DO replicate, so the client read still works.
			if itemName and itemName ~= "" then
				player:SetAttribute("has" .. itemName, true)
			end
			if _pickupEvent then _pickupEvent:FireClient(player, itemName) end
			if source then source:Play() end
			Debris:AddItem(container, 0)
		end
	end)
end
