-- Object Pool runtime module
-- Replaces Unity's ObjectPool<T> pattern with a Roblox-friendly pool that
-- caches and recycles cloned instances from ReplicatedStorage templates.
--
-- Usage (injected by the converter when pool patterns are detected):
--   local ObjectPool = require(game.ReplicatedStorage.ObjectPool)
--   local pool = ObjectPool.new(template, 10)
--   local obj = pool:Get()     -- clone from pool or create new
--   pool:Release(obj)          -- return to pool (reparent to nil, reset)
--   pool:Clear()               -- destroy all pooled instances

local ObjectPool = {}
ObjectPool.__index = ObjectPool

function ObjectPool.new(template, initialSize)
	local self = setmetatable({}, ObjectPool)
	self._template = template
	self._available = {}
	self._active = {}

	initialSize = initialSize or 0
	for _ = 1, initialSize do
		local clone = template:Clone()
		clone.Parent = nil
		table.insert(self._available, clone)
	end

	return self
end

function ObjectPool:Get()
	local obj
	if #self._available > 0 then
		obj = table.remove(self._available)
	else
		obj = self._template:Clone()
	end
	self._active[obj] = true
	return obj
end

function ObjectPool:Release(obj)
	if not self._active[obj] then return end
	self._active[obj] = nil
	obj.Parent = nil
	if obj:IsA("BasePart") then
		obj.Anchored = true
		obj.AssemblyLinearVelocity = Vector3.zero
		obj.AssemblyAngularVelocity = Vector3.zero
	end
	table.insert(self._available, obj)
end

function ObjectPool:Clear()
	for obj in self._active do
		obj:Destroy()
	end
	for _, obj in self._available do
		obj:Destroy()
	end
	self._active = {}
	self._available = {}
end

return ObjectPool
