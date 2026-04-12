-- UnityBridge/GameObjectUtil
-- Maps Unity's GameObject/Instantiate/Destroy to Roblox equivalents.
-- Usage:
--   local GO = require(ReplicatedStorage.UnityBridge.GameObjectUtil)
--   local clone = GO.Instantiate(template)
--   GO.Destroy(instance)
--   local obj = GO.Find("PlayerCat")

local InsertService = game:GetService("InsertService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

-- Ensure the Templates folder exists in ReplicatedStorage
local Templates = ReplicatedStorage:FindFirstChild("Templates")
if not Templates then
	Templates = Instance.new("Folder")
	Templates.Name = "Templates"
	Templates.Parent = ReplicatedStorage
end

local GameObjectUtil = {}

-- Cache of loaded Model assets (assetId → loaded instance)
local assetCache = {}

function GameObjectUtil.Instantiate(template, position, rotation)
	if typeof(template) == "Instance" then
		local clone = template:Clone()
		if position then
			clone:PivotTo(CFrame.new(position))
		end
		clone.Parent = workspace
		return clone
	end
	return nil
end

function GameObjectUtil.InstantiateFromAsset(assetId, position)
	-- Load from Roblox asset (for meshes uploaded via Open Cloud)
	if assetCache[assetId] then
		local clone = assetCache[assetId]:Clone()
		if position then
			clone:PivotTo(CFrame.new(position))
		end
		clone.Parent = workspace
		return clone
	end

	local ok, model = pcall(function()
		return InsertService:LoadAsset(assetId)
	end)
	if ok and model then
		local child = model:GetChildren()[1]
		if child then
			-- Cache the template
			local template = child:Clone()
			template.Parent = Templates
			pcall(function()
				if template:IsA("Model") then template:ScaleTo(0.01) end
			end)
			assetCache[assetId] = template

			-- Return a clone
			local clone = template:Clone()
			if position then
				clone:PivotTo(CFrame.new(position))
			end
			clone.Parent = workspace
			model:Destroy()
			return clone
		end
		model:Destroy()
	end
	return nil
end

function GameObjectUtil.Destroy(instance)
	if instance and instance.Parent then
		instance:Destroy()
	end
end

function GameObjectUtil.Find(name)
	return workspace:FindFirstChild(name, true)
end

function GameObjectUtil.FindWithTag(tag)
	-- Roblox uses CollectionService tags
	local CollectionService = game:GetService("CollectionService")
	local tagged = CollectionService:GetTagged(tag)
	return tagged
end

function GameObjectUtil.SetActive(instance, active)
	if instance then
		if instance:IsA("BasePart") then
			instance.Transparency = active and 0 or 1
			instance.CanCollide = active
		elseif instance:IsA("Model") then
			for _, d in ipairs(instance:GetDescendants()) do
				if d:IsA("BasePart") then
					d.Transparency = active and 0 or 1
					d.CanCollide = active
				end
			end
		end
	end
end

return GameObjectUtil
