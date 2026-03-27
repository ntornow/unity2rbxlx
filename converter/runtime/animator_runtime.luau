-- AnimatorRuntime: Interprets exported Animator Controller JSON at runtime.
--
-- States:      {name, motion, speed, transitions}
-- Transitions: {destination, conditions, duration, hasExitTime, exitTime}
-- Conditions:  {parameter, mode (Greater/Less/Equals/NotEqual/If/IfNot), threshold}
--
-- Usage:
--   local AnimatorRuntime = require(path.to.AnimatorRuntime)
--   local controller = HttpService:JSONDecode(controllerJson)
--   local runtime = AnimatorRuntime.new(controller, humanoid:FindFirstChildOfClass("Animator"))
--   runtime:LoadAnimations({ Walk = "rbxassetid://123", Idle = "rbxassetid://456" })
--   RunService.Heartbeat:Connect(function(dt) runtime:Update(dt) end)

local AnimatorRuntime = {}
AnimatorRuntime.__index = AnimatorRuntime

--- Create a new AnimatorRuntime instance.
--- @param controllerData table -- Parsed JSON of the exported Animator Controller.
--- @param humanoidOrAnimController Humanoid|AnimationController -- Target to play animations on.
--- @return AnimatorRuntime
function AnimatorRuntime.new(controllerData, humanoidOrAnimController)
	local self = setmetatable({}, AnimatorRuntime)
	self.controller = controllerData
	self.animator = humanoidOrAnimController
	self.parameters = {}
	self.currentState = nil
	self.currentTrack = nil
	self.stateTime = 0
	self.tracks = {}

	-- Initialise parameters with their default values
	if controllerData.parameters then
		for _, p in controllerData.parameters do
			if p.type == "Bool" then
				self.parameters[p.name] = p.defaultValue or false
			else
				self.parameters[p.name] = p.defaultValue or 0
			end
		end
	end

	-- Set the entry state (first in the list)
	if controllerData.states and #controllerData.states > 0 then
		self.currentState = controllerData.states[1]
	end

	return self
end

--- Set a boolean parameter.
--- @param name string
--- @param value boolean
function AnimatorRuntime:SetBool(name, value)
	self.parameters[name] = value
end

--- Set a float parameter.
--- @param name string
--- @param value number
function AnimatorRuntime:SetFloat(name, value)
	self.parameters[name] = value
end

--- Set an integer parameter.
--- @param name string
--- @param value number
function AnimatorRuntime:SetInteger(name, value)
	self.parameters[name] = math.floor(value)
end

--- Set a trigger parameter (auto-resets on next frame).
--- @param name string
function AnimatorRuntime:SetTrigger(name)
	self.parameters[name] = true
	task.defer(function()
		self.parameters[name] = false
	end)
end

--- Evaluate a single transition condition against current parameters.
--- @param condition table -- {parameter, mode, threshold}
--- @return boolean
function AnimatorRuntime:_evaluateCondition(condition)
	local paramValue = self.parameters[condition.parameter]
	if paramValue == nil then
		return false
	end

	if condition.mode == "Greater" then
		return paramValue > condition.threshold
	elseif condition.mode == "Less" then
		return paramValue < condition.threshold
	elseif condition.mode == "Equals" then
		return paramValue == condition.threshold
	elseif condition.mode == "NotEqual" then
		return paramValue ~= condition.threshold
	elseif condition.mode == "If" then
		return paramValue == true
	elseif condition.mode == "IfNot" then
		return paramValue == false
	end

	return false
end

--- Check all transitions from the current state and fire the first valid one.
function AnimatorRuntime:_checkTransitions()
	if not self.currentState or not self.currentState.transitions then
		return
	end

	for _, transition in self.currentState.transitions do
		local allMet = true

		-- Evaluate parameter conditions
		if transition.conditions then
			for _, cond in transition.conditions do
				if not self:_evaluateCondition(cond) then
					allMet = false
					break
				end
			end
		end

		-- Check exit-time constraint
		if transition.hasExitTime and self.currentTrack then
			if self.stateTime < (transition.exitTime or 1) then
				allMet = false
			end
		end

		if allMet then
			self:_transitionTo(transition.destination, transition.duration or 0.25)
			return
		end
	end
end

--- Transition to a new state by name with a crossfade duration.
--- @param stateName string
--- @param duration number -- Crossfade duration in seconds.
function AnimatorRuntime:_transitionTo(stateName, duration)
	local newState = nil
	for _, s in self.controller.states do
		if s.name == stateName then
			newState = s
			break
		end
	end

	if not newState then
		return
	end

	-- Stop the current animation track with crossfade
	if self.currentTrack then
		self.currentTrack:Stop(duration)
	end

	self.currentState = newState
	self.stateTime = 0

	-- Play the new state's animation if we have a loaded track for its motion
	local track = self.tracks[newState.motion]
	if track then
		track:Play(duration)
		self.currentTrack = track
	end
end

--- Advance the runtime by dt seconds.  Call this every frame.
--- @param dt number -- Delta time in seconds.
function AnimatorRuntime:Update(dt)
	self.stateTime = self.stateTime + dt
	self:_checkTransitions()
end

--- Pre-load animation assets and create AnimationTracks.
--- @param animations table<string, string> -- Map of motion name -> animation asset ID.
function AnimatorRuntime:LoadAnimations(animations)
	for name, animId in animations do
		local anim = Instance.new("Animation")
		anim.AnimationId = animId
		local track = self.animator:LoadAnimation(anim)
		self.tracks[name] = track
	end
end

return AnimatorRuntime
