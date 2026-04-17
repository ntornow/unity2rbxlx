-- AnimatorRuntime: Interprets exported Animator Controller JSON at runtime.
--
-- States:      {name, motion, speed, transitions}
-- Transitions: {destination, conditions, duration, hasExitTime, exitTime}
-- Conditions:  {parameter, mode (Greater/Less/Equals/NotEqual/If/IfNot), threshold}
-- BlendTrees:  {param, clips = { {clip, threshold}, ... }}
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
	self._warnedClips = {}

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

	-- Set the entry state: prefer defaultState, fall back to first
	local entryState = nil
	if controllerData.defaultState and controllerData.states then
		for _, s in controllerData.states do
			if s.name == controllerData.defaultState then
				entryState = s
				break
			end
		end
	end
	if not entryState and controllerData.states and #controllerData.states > 0 then
		entryState = controllerData.states[1]
	end
	self.currentState = entryState

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

--- Get a float parameter value.
function AnimatorRuntime:GetFloat(name)
	return self.parameters[name] or 0
end

--- Get a boolean parameter value.
function AnimatorRuntime:GetBool(name)
	return self.parameters[name] or false
end

--- Get an integer parameter value.
function AnimatorRuntime:GetInt(name)
	return self.parameters[name] or 0
end

--- Jump directly to a state by name (mirrors Unity Animator.Play).
function AnimatorRuntime:Play(stateName)
	for _, s in self.controller.states do
		if s.name == stateName then
			self:_transitionTo(stateName, 0.25)
			return
		end
	end
	warn("[AnimatorRuntime] Unknown state: " .. tostring(stateName))
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
--- Also checks anyStateTransitions from the controller root.
function AnimatorRuntime:_checkTransitions()
	if not self.currentState then
		return
	end

	-- Helper: evaluate a single transition table
	local function tryTransition(transition)
		local allMet = true

		if transition.conditions then
			for _, cond in transition.conditions do
				if not self:_evaluateCondition(cond) then
					allMet = false
					break
				end
			end
		end

		if transition.hasExitTime and self.currentTrack then
			if self.stateTime < (transition.exitTime or 1) then
				allMet = false
			end
		end

		if allMet then
			-- Don't allow Any→same state
			if transition.destination == self.currentState.name then
				return false
			end
			self:_transitionTo(transition.destination, transition.duration or 0.25)
			return true
		end
		return false
	end

	-- Check current state's own transitions first
	if self.currentState.transitions then
		for _, transition in self.currentState.transitions do
			if tryTransition(transition) then return end
		end
	end

	-- Check any-state transitions (controller-level, apply from every state)
	if self.controller.anyStateTransitions then
		for _, transition in self.controller.anyStateTransitions do
			if tryTransition(transition) then return end
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

	-- Check if this state uses a blend tree
	if newState.blendTree and self.controller.blendTrees then
		self:_startBlendTree(newState.blendTree)
		return
	end

	-- Play the new state's animation if we have a loaded track for its motion
	local track = self.tracks[newState.motion]
	if not track then
		track = self:_lazyLoadTrack(newState.motion)
	end
	if track then
		track:Play(duration)
		track.Looped = newState.loop ~= false
		local speed = newState.speed or 1
		if speed ~= 1 then
			track:AdjustSpeed(speed)
		end
		self.currentTrack = track
	end
end

--- Advance the runtime by dt seconds.  Call this every frame.
--- @param dt number -- Delta time in seconds.
function AnimatorRuntime:Update(dt)
	self.stateTime = self.stateTime + dt
	self:_checkTransitions()

	-- Update blend tree weights if active
	if self.currentState and self.currentState.blendTree and self.controller.blendTrees then
		self:_updateBlendTree(self.currentState.blendTree)
	end
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

--- Lazy-load a track by searching for an Animation instance by name.
function AnimatorRuntime:_lazyLoadTrack(clipName)
	if self.tracks[clipName] then
		return self.tracks[clipName]
	end
	if not self.animator then return nil end
	local parent = self.animator.Parent
	if not parent then return nil end

	for _, child in parent:GetDescendants() do
		if child:IsA("Animation") and child.Name == clipName then
			local track = self.animator:LoadAnimation(child)
			self.tracks[clipName] = track
			return track
		end
	end

	if not self._warnedClips[clipName] then
		warn("[AnimatorRuntime] Animation not found: " .. tostring(clipName))
		self._warnedClips[clipName] = true
	end
	return nil
end

--- Start all blend tree tracks at weight 0.
function AnimatorRuntime:_startBlendTree(blendTreeName)
	local bt = self.controller.blendTrees[blendTreeName]
	if not bt or not bt.clips then return end

	for _, entry in bt.clips do
		local track = self.tracks[entry.clip] or self:_lazyLoadTrack(entry.clip)
		if track then
			track:Play(0.1)
			track.Looped = true
			track:AdjustWeight(0, 0.1)
		end
	end
end

--- Interpolate blend tree clip weights based on the driving parameter.
function AnimatorRuntime:_updateBlendTree(blendTreeName)
	local bt = self.controller.blendTrees[blendTreeName]
	if not bt or not bt.clips or #bt.clips == 0 then return end

	local paramValue = self.parameters[bt.param] or 0
	local clips = bt.clips

	for i, entry in clips do
		local track = self.tracks[entry.clip]
		if not track then continue end

		local weight = 0

		if i == 1 and paramValue <= entry.threshold then
			weight = 1
		elseif i == #clips and paramValue >= entry.threshold then
			weight = 1
		elseif i < #clips then
			local nextEntry = clips[i + 1]
			if paramValue >= entry.threshold and paramValue <= nextEntry.threshold then
				local range = nextEntry.threshold - entry.threshold
				weight = if range > 0 then 1 - (paramValue - entry.threshold) / range else 1
			elseif i > 1 then
				local prevEntry = clips[i - 1]
				if paramValue >= prevEntry.threshold and paramValue <= entry.threshold then
					local range = entry.threshold - prevEntry.threshold
					weight = if range > 0 then (paramValue - prevEntry.threshold) / range else 0
				end
			end
		end

		track:AdjustWeight(weight, 0.1)
	end
end

--- Load bone keyframe data for TweenService-based animation fallback.
--- Use this when animation asset IDs aren't available (no upload).
--- @param keyframeData table<string, table> -- Map of motion name -> keyframe table.
---   Keyframe table format: { bones = { boneName = { {time, cf={x,y,z,rx,ry,rz}}, ... } } }
function AnimatorRuntime:LoadKeyframes(keyframeData)
	self._keyframeData = keyframeData
end

--- Play a bone animation from keyframe data using TweenService.
--- @param motionName string -- Name of the motion to play.
--- @param model Model|BasePart -- The model containing bone Parts.
--- @param loop boolean -- Whether to loop the animation.
function AnimatorRuntime:_playKeyframeAnimation(motionName, model, loop)
	local TweenService = game:GetService("TweenService")
	if not self._keyframeData or not self._keyframeData[motionName] then
		return
	end

	local data = self._keyframeData[motionName]
	local bones = data.bones or {}
	local duration = data.duration or 1

	-- Stop any existing keyframe animation
	if self._keyframeConn then
		self._keyframeConn:Disconnect()
		self._keyframeConn = nil
	end

	-- Find bone Parts in the model (with fuzzy name matching)
	local boneParts = {}
	local bonePartsLower = {} -- lowercase lookup for case-insensitive matching
	for _, desc in model:GetDescendants() do
		if desc:IsA("BasePart") then
			boneParts[desc.Name] = desc
			bonePartsLower[string.lower(desc.Name)] = desc
			-- Also index without common prefixes (mixamorig:, Bip01, etc.)
			local stripped = desc.Name:match(":(.+)") or desc.Name
			if stripped ~= desc.Name then
				boneParts[stripped] = desc
				bonePartsLower[string.lower(stripped)] = desc
			end
		end
	end

	-- Animate each bone using TweenService
	local function playOnce()
		for boneName, keyframes in bones do
			local part = boneParts[boneName] or bonePartsLower[string.lower(boneName)]
			if not part or #keyframes < 2 then continue end

			task.spawn(function()
				for i = 2, #keyframes do
					local prev = keyframes[i - 1]
					local kf = keyframes[i]
					local dt = kf.time - prev.time
					if dt <= 0 then continue end

					local cf = kf.cf
					local targetCF = CFrame.new(cf.x or 0, cf.y or 0, cf.z or 0) *
						CFrame.Angles(
							math.rad(cf.rx or 0),
							math.rad(cf.ry or 0),
							math.rad(cf.rz or 0)
						)

					local info = TweenInfo.new(dt, Enum.EasingStyle.Linear)
					local tween = TweenService:Create(part, info, { CFrame = targetCF })
					tween:Play()
					tween.Completed:Wait()
				end
			end)
		end
		task.wait(duration)
	end

	if loop then
		task.spawn(function()
			while self.currentState and self.currentState.motion == motionName do
				playOnce()
			end
		end)
	else
		task.spawn(playOnce)
	end
end

--- Stop all tracks and clean up.
function AnimatorRuntime:Destroy()
	for _, track in self.tracks do
		track:Stop(0)
		track:Destroy()
	end
	self.tracks = {}
	self.currentTrack = nil
	self.currentState = nil

	if self._keyframeConn then
		self._keyframeConn:Disconnect()
		self._keyframeConn = nil
	end
end

return AnimatorRuntime
