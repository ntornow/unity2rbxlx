-- UnityBridge/MonoBehaviour
-- Implements Unity's MonoBehaviour lifecycle in Roblox.
-- Usage:
--   local MB = require(ReplicatedStorage.UnityBridge.MonoBehaviour)
--   local MyScript = MB.new()
--   function MyScript:Start() ... end
--   function MyScript:Update(dt) ... end
--   MyScript:Enable()

local RunService = game:GetService("RunService")

local MonoBehaviour = {}
MonoBehaviour.__index = MonoBehaviour

function MonoBehaviour.new()
	local self = setmetatable({}, MonoBehaviour)
	self._enabled = false
	self._started = false
	self._connections = {}
	self.gameObject = nil -- set by caller
	self.transform = nil  -- set by caller
	return self
end

function MonoBehaviour:Enable()
	if self._enabled then return end
	self._enabled = true

	if not self._started and self.Start then
		self:Start()
		self._started = true
	end

	if self.Awake and not self._awoke then
		self:Awake()
		self._awoke = true
	end

	if self.Update then
		local conn = RunService.Heartbeat:Connect(function(dt)
			if self._enabled then
				self:Update(dt)
			end
		end)
		table.insert(self._connections, conn)
	end

	if self.FixedUpdate then
		local conn = RunService.Stepped:Connect(function(_, dt)
			if self._enabled then
				self:FixedUpdate(dt)
			end
		end)
		table.insert(self._connections, conn)
	end

	if self.OnEnable then
		self:OnEnable()
	end
end

function MonoBehaviour:Disable()
	if not self._enabled then return end
	self._enabled = false

	for _, conn in ipairs(self._connections) do
		conn:Disconnect()
	end
	self._connections = {}

	if self.OnDisable then
		self:OnDisable()
	end
end

function MonoBehaviour:Destroy()
	self:Disable()
	if self.OnDestroy then
		self:OnDestroy()
	end
end

return MonoBehaviour
