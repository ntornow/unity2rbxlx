local Pooler = {}
Pooler.__index = Pooler

-- SetActive(false/true) → toggle visibility + collision on every BasePart
local function setActive(obj, active)
	if obj:IsA("BasePart") then
		obj.Transparency = if active then 0 else 1
		obj.CanCollide = active
	end
	for _, part in ipairs(obj:GetDescendants()) do
		if part:IsA("BasePart") then
			part.Transparency = if active then 0 else 1
			part.CanCollide = active
		end
	end
end

-- transform.position/rotation write — dispatch on Model vs BasePart
local function setObjectCFrame(obj, cf)
	if obj:IsA("Model") then
		obj:PivotTo(cf)
	else
		obj.CFrame = cf
	end
end

function Pooler.new(original, initialSize)
	local self = setmetatable({}, Pooler)
	self.m_Original = original
	self.m_FreeInstances = {}

	for i = 0, initialSize - 1 do
		local obj = original:Clone()
		setActive(obj, false)
		table.insert(self.m_FreeInstances, obj)
	end

	return self
end

function Pooler:Get(pos, quat)
	if pos == nil then pos = Vector3.new(0, 0, 0) end
	if quat == nil then quat = CFrame.new() end

	local ret
	if #self.m_FreeInstances > 0 then
		ret = table.remove(self.m_FreeInstances)
	else
		ret = self.m_Original:Clone()
	end

	ret.Parent = workspace
	setActive(ret, true)
	setObjectCFrame(ret, CFrame.new(pos) * quat)

	return ret
end

function Pooler:Free(obj)
	obj.Parent = nil
	setActive(obj, false)
	table.insert(self.m_FreeInstances, obj)
end

return Pooler