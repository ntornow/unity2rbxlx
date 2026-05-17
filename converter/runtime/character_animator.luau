-- CharacterAnimator: state machine driven by exported Animator Controller JSON.
--
-- Controller format (from export_controller_json):
--   states:             [{name, motion, speed, loop, transitions, blendTree?}]
--   transitions:        [{destination, conditions, duration, hasExitTime, exitTime}]
--   conditions:         [{parameter, mode, threshold}]
--   parameters:         [{name, type, defaultValue}]
--   anyStateTransitions: same as transitions (optional)
--   blendTrees:         {name -> {param, clips: [{clip, threshold}]}} (optional)
--   defaultState:       string (optional)

local CharacterAnimator = {}
CharacterAnimator.__index = CharacterAnimator

function CharacterAnimator.new(controllerData, humanoidOrAnimController)
	local self = setmetatable({}, CharacterAnimator)
	self.controller = controllerData
	self.animator = humanoidOrAnimController
	self.parameters = {}
	self.currentState = nil
	self.currentTrack = nil
	self.stateTime = 0
	self.tracks = {}
	self._warnedClips = {}

	if controllerData.parameters then
		for _, p in controllerData.parameters do
			self.parameters[p.name] = if p.type == "Bool" then (p.defaultValue or false) else (p.defaultValue or 0)
		end
	end

	-- Prefer defaultState, fall back to first
	local entry = nil
	if controllerData.defaultState and controllerData.states then
		for _, s in controllerData.states do
			if s.name == controllerData.defaultState then
				entry = s
				break
			end
		end
	end
	if not entry and controllerData.states and #controllerData.states > 0 then
		entry = controllerData.states[1]
	end
	self.currentState = entry

	return self
end

-- Parameter setters

function CharacterAnimator:SetBool(name, value) self.parameters[name] = value end
function CharacterAnimator:SetFloat(name, value) self.parameters[name] = value end
function CharacterAnimator:SetInteger(name, value) self.parameters[name] = math.floor(value) end

function CharacterAnimator:SetTrigger(name)
	self.parameters[name] = true
	task.defer(function() self.parameters[name] = false end)
end

-- Parameter getters

function CharacterAnimator:GetFloat(name) return self.parameters[name] or 0 end
function CharacterAnimator:GetBool(name) return self.parameters[name] or false end
function CharacterAnimator:GetInt(name) return self.parameters[name] or 0 end

-- Jump directly to a state by name.
function CharacterAnimator:Play(stateName)
	for _, s in self.controller.states do
		if s.name == stateName then
			self:_transitionTo(stateName, 0.25)
			return
		end
	end
	warn("[CharacterAnimator] Unknown state: " .. tostring(stateName))
end

function CharacterAnimator:_evaluateCondition(condition)
	local val = self.parameters[condition.parameter]
	if val == nil then return false end

	local mode = condition.mode
	if mode == "Greater" then return val > condition.threshold
	elseif mode == "Less" then return val < condition.threshold
	elseif mode == "Equals" then return val == condition.threshold
	elseif mode == "NotEqual" then return val ~= condition.threshold
	elseif mode == "If" then return val == true
	elseif mode == "IfNot" then return val == false
	end
	return false
end

function CharacterAnimator:_checkTransitions()
	if not self.currentState then return end

	local function tryTransition(transition)
		if transition.conditions then
			for _, cond in transition.conditions do
				if not self:_evaluateCondition(cond) then return false end
			end
		end
		if transition.hasExitTime and self.currentTrack then
			if self.stateTime < (transition.exitTime or 1) then return false end
		end
		if transition.destination == self.currentState.name then return false end
		self:_transitionTo(transition.destination, transition.duration or 0.25)
		return true
	end

	if self.currentState.transitions then
		for _, tr in self.currentState.transitions do
			if tryTransition(tr) then return end
		end
	end
	if self.controller.anyStateTransitions then
		for _, tr in self.controller.anyStateTransitions do
			if tryTransition(tr) then return end
		end
	end
end

function CharacterAnimator:_transitionTo(stateName, duration)
	local newState = nil
	for _, s in self.controller.states do
		if s.name == stateName then newState = s; break end
	end
	if not newState then return end

	if self.currentTrack then self.currentTrack:Stop(duration) end
	self.currentState = newState
	self.stateTime = 0

	if newState.blendTree and self.controller.blendTrees then
		self:_startBlendTree(newState.blendTree)
		return
	end

	local track = self.tracks[newState.motion] or self:_lazyLoadTrack(newState.motion)
	if track then
		track:Play(duration)
		track.Looped = newState.loop ~= false
		local speed = newState.speed or 1
		if speed ~= 1 then track:AdjustSpeed(speed) end
		self.currentTrack = track
	end
end

function CharacterAnimator:Update(dt)
	self.stateTime = self.stateTime + dt
	self:_checkTransitions()
	if self.currentState and self.currentState.blendTree and self.controller.blendTrees then
		self:_updateBlendTree(self.currentState.blendTree)
	end
end

function CharacterAnimator:LoadAnimations(animations)
	for name, animId in animations do
		local anim = Instance.new("Animation")
		anim.AnimationId = animId
		self.tracks[name] = self.animator:LoadAnimation(anim)
	end
end

function CharacterAnimator:_lazyLoadTrack(clipName)
	if self.tracks[clipName] then return self.tracks[clipName] end
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
		warn("[CharacterAnimator] Animation not found: " .. tostring(clipName))
		self._warnedClips[clipName] = true
	end
	return nil
end

-- 1D blend tree: start all tracks at weight 0, then interpolate per-frame.
function CharacterAnimator:_startBlendTree(blendTreeName)
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

function CharacterAnimator:_updateBlendTree(blendTreeName)
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

-- TweenService-based bone animation fallback (no uploaded animation assets).
function CharacterAnimator:LoadKeyframes(keyframeData)
	self._keyframeData = keyframeData
end

function CharacterAnimator:_playKeyframeAnimation(motionName, model, loop)
	local TweenService = game:GetService("TweenService")
	if not self._keyframeData or not self._keyframeData[motionName] then return end

	local data = self._keyframeData[motionName]
	local bones = data.bones or {}
	local duration = data.duration or 1

	if self._keyframeConn then
		self._keyframeConn:Disconnect()
		self._keyframeConn = nil
	end

	local boneParts = {}
	local bonePartsLower = {}
	for _, desc in model:GetDescendants() do
		if desc:IsA("BasePart") then
			boneParts[desc.Name] = desc
			bonePartsLower[string.lower(desc.Name)] = desc
			local stripped = desc.Name:match(":(.+)") or desc.Name
			if stripped ~= desc.Name then
				boneParts[stripped] = desc
				bonePartsLower[string.lower(stripped)] = desc
			end
		end
	end

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
						CFrame.Angles(math.rad(cf.rx or 0), math.rad(cf.ry or 0), math.rad(cf.rz or 0))

					local info = TweenInfo.new(dt, Enum.EasingStyle.Linear)
					TweenService:Create(part, info, { CFrame = targetCF }):Play()
					task.wait(dt)
				end
			end)
		end
		task.wait(duration)
	end

	if loop then
		task.spawn(function()
			while self.currentState and self.currentState.motion == motionName do playOnce() end
		end)
	else
		task.spawn(playOnce)
	end
end

function CharacterAnimator:Destroy()
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

return CharacterAnimator
