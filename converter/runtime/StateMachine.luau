-- UnityBridge/StateMachine
-- Mirrors Unity's GameManager pattern: a stack-based state machine
-- where each state has Enter(from)/Exit(to)/Tick() lifecycle methods.
--
-- Usage:
--   local SM = require(ReplicatedStorage.UnityBridge.StateMachine)
--   local manager = SM.new()
--
--   manager:AddState("Loadout", {
--       Enter = function(self, from) ... end,
--       Exit  = function(self, to)   ... end,
--       Tick  = function(self, dt)   ... end,
--   })
--   manager:AddState("Game", { ... })
--   manager:AddState("GameOver", { ... })
--
--   manager:Start("Loadout")  -- pushes initial state, begins ticking
--   manager:SwitchState("Game")
--   manager:PushState("GameOver")
--   manager:PopState()

local RunService = game:GetService("RunService")

local StateMachine = {}
StateMachine.__index = StateMachine

function StateMachine.new()
	local self = setmetatable({}, StateMachine)
	self._states = {}      -- name → state table
	self._stack = {}       -- array of state tables (top = last)
	self._connection = nil
	return self
end

function StateMachine:AddState(name, state)
	state.name = name
	state.manager = self
	self._states[name] = state
end

function StateMachine:FindState(name)
	return self._states[name]
end

function StateMachine:TopState()
	if #self._stack == 0 then return nil end
	return self._stack[#self._stack]
end

function StateMachine:Start(initialStateName)
	self:PushState(initialStateName)

	-- Tick the top state every frame (mirrors GameManager.Update)
	self._connection = RunService.Heartbeat:Connect(function(dt)
		local top = self:TopState()
		if top and top.Tick then
			top:Tick(dt)
		end
	end)
end

function StateMachine:Stop()
	if self._connection then
		self._connection:Disconnect()
		self._connection = nil
	end
end

function StateMachine:SwitchState(newStateName)
	local state = self._states[newStateName]
	if not state then
		warn("[StateMachine] Unknown state: " .. tostring(newStateName))
		return
	end

	local top = self:TopState()
	if top and top.Exit then
		top:Exit(state)
	end
	if state.Enter then
		state:Enter(top)
	end
	self._stack[#self._stack] = state
end

function StateMachine:PushState(name)
	local state = self._states[name]
	if not state then
		warn("[StateMachine] Unknown state: " .. tostring(name))
		return
	end

	local top = self:TopState()
	if top and top.Exit then
		top:Exit(state)
	end
	if state.Enter then
		state:Enter(top)
	end
	table.insert(self._stack, state)
end

function StateMachine:PopState()
	if #self._stack < 2 then
		warn("[StateMachine] Can't pop, only one state in stack.")
		return
	end

	local popped = self._stack[#self._stack]
	local below = self._stack[#self._stack - 1]

	if popped.Exit then
		popped:Exit(below)
	end
	if below.Enter then
		below:Enter(popped)
	end
	table.remove(self._stack)
end

return StateMachine
