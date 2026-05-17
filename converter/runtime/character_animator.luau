-- CharacterAnimator: state machine driven by exported Animator Controller JSON.
--
-- Controller format (from export_controller_json):
--   states:             [{name, motion, speed, transitions, blendTree?, loop?}]
--   transitions:        [{destination, conditions, duration, hasExitTime, exitTime}]
--   conditions:         [{parameter, mode, threshold}]
--   parameters:         [{name, type, defaultValue}]
--   anyStateTransitions: same as transitions (optional)
--   blendTrees:         {name -> {param, clips: [{clip, threshold}]}} (optional)
--   defaultState:       string (optional)
--
-- Keyframe format (from export_clip_keyframes):
--   {motion -> {duration, loop, bones: {boneName -> [{time, cf}]}}}
--
-- Backend note: this runtime is the *tween backend* of the character-animation
-- plan (docs/design/character-animation.md). Clip playback runs through
-- KeyframeTrack — a track-like adapter that wraps per-bone CFrame tweens in
-- the subset of the Roblox AnimationTrack API the state machine consumes.
-- The native (KeyframeSequence) backend is PR3.
--
-- KNOWN LIMITATION (PR2 scope): the imperative dispatch path
-- (CharacterAnimator.Dispatch / Register / Resolve) is server-side only.
-- Transpiled Animator.* calls originating from a LocalScript will not reach
-- the server-bound CharacterAnimator instance. See docs/UNSUPPORTED.md.

local TweenService = game:GetService("TweenService")

-- ===========================================================================
-- KeyframeTrack: track-like adapter over per-bone CFrame tweening.
--
-- Exposes the subset of the Roblox AnimationTrack API the state machine uses
-- (:Play / :Stop / :AdjustSpeed / :AdjustWeight / Looped) so _transitionTo,
-- _checkTransitions (exit-time gating reads currentTrack) and the blend-tree
-- code can treat keyframe playback uniformly with a real AnimationTrack.
-- ===========================================================================

local KeyframeTrack = {}
KeyframeTrack.__index = KeyframeTrack

-- keyframeData: {duration, loop, bones} entry from the keyframes dict.
-- model:        the rig model whose BasePart descendants are the bones.
function KeyframeTrack.new(keyframeData, model)
	local self = setmetatable({}, KeyframeTrack)
	self._data = keyframeData or {}
	self._model = model
	self.Looped = (keyframeData and keyframeData.loop) ~= false
	self._speed = 1
	self._playing = false
	self._tweens = {}
	self._loopThread = nil
	return self
end

-- Resolve every bone name once, with Mixamo-prefix and case-insensitive
-- fallbacks (mirrors the prior _playKeyframeAnimation lookup table).
function KeyframeTrack:_resolveBoneParts()
	local boneParts = {}
	local bonePartsLower = {}
	if not self._model then return boneParts, bonePartsLower end
	for _, desc in self._model:GetDescendants() do
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
	return boneParts, bonePartsLower
end

-- One full pass over the keyframes. Returns once the longest bone track
-- finishes (or immediately if there is nothing to play).
function KeyframeTrack:_playOnce()
	local bones = self._data.bones or {}
	local boneParts, bonePartsLower = self:_resolveBoneParts()
	local speed = if self._speed > 0 then self._speed else 1
	local longest = 0

	for boneName, keyframes in bones do
		local part = boneParts[boneName] or bonePartsLower[string.lower(boneName)]
		if not part or #keyframes < 2 then continue end

		local boneSpan = 0
		for i = 2, #keyframes do
			boneSpan = boneSpan + math.max(keyframes[i].time - keyframes[i - 1].time, 0)
		end
		longest = math.max(longest, boneSpan / speed)

		task.spawn(function()
			for i = 2, #keyframes do
				if not self._playing then return end
				local prev = keyframes[i - 1]
				local kf = keyframes[i]
				local dt = (kf.time - prev.time) / speed
				if dt <= 0 then continue end

				local cf = kf.cf
				local targetCF = CFrame.new(cf.x or 0, cf.y or 0, cf.z or 0) *
					CFrame.Angles(math.rad(cf.rx or 0), math.rad(cf.ry or 0), math.rad(cf.rz or 0))

				local info = TweenInfo.new(dt, Enum.EasingStyle.Linear)
				local tween = TweenService:Create(part, info, { CFrame = targetCF })
				self._tweens[tween] = true
				tween:Play()
				task.wait(dt)
				self._tweens[tween] = nil
			end
		end)
	end

	local duration = (self._data.duration or 0) / speed
	task.wait(math.max(longest, duration))
end

-- :Play(fade) — fade is accepted for AnimationTrack parity but keyframe
-- playback has no built-in fade-in; the argument is intentionally ignored.
function KeyframeTrack:Play(_fade)
	if self._playing then return end
	self._playing = true
	self._loopThread = task.spawn(function()
		if self.Looped then
			while self._playing do
				self:_playOnce()
			end
		else
			self:_playOnce()
			self._playing = false
		end
	end)
end

-- :Stop(fade) — cancels the running tweens AND the playback loop/thread.
function KeyframeTrack:Stop(_fade)
	self._playing = false
	for tween in self._tweens do
		tween:Cancel()
	end
	self._tweens = {}
	if self._loopThread then
		task.cancel(self._loopThread)
		self._loopThread = nil
	end
end

-- :AdjustSpeed(speed) — actually scales playback (shorter/longer tween dt).
function KeyframeTrack:AdjustSpeed(speed)
	self._speed = speed or 1
end

-- :AdjustWeight(weight, fade) — documented no-op stub. True weighted
-- keyframe blending (overlaying multiple KeyframeTracks on one rig) is out
-- of PR2 scope; the state machine still calls this for blend trees so the
-- method must exist. A real AnimationTrack honours it.
function KeyframeTrack:AdjustWeight(_weight, _fade)
end

function KeyframeTrack:Destroy()
	self:Stop(0)
	self._model = nil
end

-- ===========================================================================
-- CharacterAnimator
-- ===========================================================================

local CharacterAnimator = {}
CharacterAnimator.__index = CharacterAnimator

-- Module-level registry: maps a host instance (the rig model) to its
-- CharacterAnimator instance, so transpiled imperative Animator.* calls can
-- resolve the runtime without _G or a per-rig module. Imperative calls that
-- arrive before the bootstrap registers the host are queued and drained on
-- Register.
CharacterAnimator._registry = {}
CharacterAnimator._pending = {}

function CharacterAnimator.new(controllerData, rigModel)
	local self = setmetatable({}, CharacterAnimator)
	self.controller = controllerData
	-- The rig model: BasePart descendants are the animated bones. Sourced
	-- explicitly from the bootstrap (.new second arg). Also used as the
	-- registry host so imperative calls resolve to this instance.
	self.rig = rigModel
	self.parameters = {}
	self.currentState = nil
	self.currentTrack = nil
	self.stateTime = 0
	self.tracks = {}
	self._keyframeData = nil
	self._warnedClips = {}
	self._attributeConns = {}

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

-- ---------------------------------------------------------------------------
-- Module-level registry / dispatch
-- ---------------------------------------------------------------------------

-- Bind a CharacterAnimator instance to its host. Drains any imperative calls
-- that arrived before the bootstrap ran.
function CharacterAnimator.Register(host, instance)
	CharacterAnimator._registry[host] = instance
	-- Hybrid script integration: read initial parameter values from the
	-- host's attributes once, then connect a per-parameter changed signal.
	-- No polling inside :Update — :Update stays state-machine work only.
	instance:_bindAttributes(host)
	local queued = CharacterAnimator._pending[host]
	if queued then
		CharacterAnimator._pending[host] = nil
		for _, call in queued do
			instance:_apply(call.op, call.args)
		end
	end
	return instance
end

-- Resolve the CharacterAnimator instance bound to a host (or nil).
function CharacterAnimator.Resolve(host)
	return CharacterAnimator._registry[host]
end

-- Dispatch an imperative op to the host's CharacterAnimator. If the host is
-- not registered yet (bootstrap has not run), queue the call so Register can
-- drain it.
function CharacterAnimator.Dispatch(host, op, ...)
	local instance = CharacterAnimator._registry[host]
	local args = { ... }
	if instance then
		instance:_apply(op, args)
	else
		local queue = CharacterAnimator._pending[host]
		if not queue then
			queue = {}
			CharacterAnimator._pending[host] = queue
		end
		table.insert(queue, { op = op, args = args })
	end
end

-- Apply a single imperative op against this instance.
function CharacterAnimator:_apply(op, args)
	if op == "SetTrigger" then
		self:SetTrigger(args[1])
	elseif op == "ResetTrigger" then
		self.parameters[args[1]] = false
	elseif op == "Play" then
		self:Play(args[1])
	elseif op == "CrossFade" or op == "CrossFadeInFixedTime" then
		self:CrossFade(args[1], args[2])
	else
		warn("[CharacterAnimator] Unknown dispatch op: " .. tostring(op))
	end
end

-- Read initial parameter values from the host's attributes, then connect a
-- GetAttributeChangedSignal per declared controller parameter. Scalar params
-- flow attribute -> self.parameters via these signals; :Update never polls.
function CharacterAnimator:_bindAttributes(host)
	if not host or not self.controller.parameters then return end
	for _, p in self.controller.parameters do
		local initial = host:GetAttribute(p.name)
		if initial ~= nil then
			self.parameters[p.name] = initial
		end
		local conn = host:GetAttributeChangedSignal(p.name):Connect(function()
			local value = host:GetAttribute(p.name)
			if value ~= nil then
				self.parameters[p.name] = value
			end
		end)
		table.insert(self._attributeConns, conn)
	end
end

-- ---------------------------------------------------------------------------
-- Parameter setters / getters
-- ---------------------------------------------------------------------------

function CharacterAnimator:SetBool(name, value) self.parameters[name] = value end
function CharacterAnimator:SetFloat(name, value) self.parameters[name] = value end
function CharacterAnimator:SetInteger(name, value) self.parameters[name] = math.floor(value) end

function CharacterAnimator:SetTrigger(name)
	self.parameters[name] = true
	task.defer(function() self.parameters[name] = false end)
end

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

-- CrossFade into a state over duration. Routes to _transitionTo so keyframe
-- playback, exit-time gating and blend trees stay uniform with Play.
function CharacterAnimator:CrossFade(stateName, duration)
	for _, s in self.controller.states do
		if s.name == stateName then
			self:_transitionTo(stateName, duration or 0.25)
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

-- Build (and cache) a KeyframeTrack for a motion from the exported keyframe
-- data + the rig model. The tween-backend fallback for when no uploaded
-- Animation asset exists — the normal case for converted games.
function CharacterAnimator:_keyframeTrackFor(motion)
	if not self._keyframeData or not self._keyframeData[motion] then return nil end
	local existing = self.tracks[motion]
	if existing then return existing end
	local track = KeyframeTrack.new(self._keyframeData[motion], self.rig)
	self.tracks[motion] = track
	return track
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

	-- Prefer an uploaded AnimationTrack; fall back to a KeyframeTrack built
	-- from the exported keyframe data + rig model. The fallback is the
	-- normal path for converted games (no Animation assets are uploaded),
	-- and keeps currentTrack non-nil so exit-time gating works.
	local track = self.tracks[newState.motion] or self:_lazyLoadTrack(newState.motion)
	if not track then
		track = self:_keyframeTrackFor(newState.motion)
	end
	if track then
		-- Only override the track's own loop flag when the controller state
		-- explicitly carries one. export_controller_json does not emit
		-- state.loop today, so a KeyframeTrack keeps the loop value it
		-- derived from the clip — forcing Looped here would make one-shot
		-- clips (jump, death, KO) loop forever.
		if newState.loop ~= nil then
			track.Looped = newState.loop ~= false
		end
		track:Play(duration)
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
		self.tracks[name] = self.rig:LoadAnimation(anim)
	end
end

function CharacterAnimator:_lazyLoadTrack(clipName)
	if self.tracks[clipName] then return self.tracks[clipName] end
	if not self.rig then return nil end

	local loader = self.rig
	if not loader:IsA("Humanoid") and not loader:IsA("AnimationController") then
		loader = self.rig:FindFirstChildWhichIsA("Humanoid")
			or self.rig:FindFirstChildWhichIsA("AnimationController")
	end
	if not loader then return nil end

	for _, child in self.rig:GetDescendants() do
		if child:IsA("Animation") and child.Name == clipName then
			local track = loader:LoadAnimation(child)
			self.tracks[clipName] = track
			return track
		end
	end

	if not self._warnedClips[clipName] then
		self._warnedClips[clipName] = true
	end
	return nil
end

-- 1D blend tree: start all tracks at weight 0, then interpolate per-frame.
function CharacterAnimator:_startBlendTree(blendTreeName)
	local bt = self.controller.blendTrees[blendTreeName]
	if not bt or not bt.clips then return end
	for _, entry in bt.clips do
		local track = self.tracks[entry.clip]
			or self:_lazyLoadTrack(entry.clip)
			or self:_keyframeTrackFor(entry.clip)
		if track then
			track.Looped = true
			track:Play(0.1)
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

-- Store the exported per-clip keyframe data. Consumed by _keyframeTrackFor
-- to build KeyframeTracks lazily.
function CharacterAnimator:LoadKeyframes(keyframeData)
	self._keyframeData = keyframeData
end

function CharacterAnimator:Destroy()
	for _, track in self.tracks do
		track:Stop(0)
		track:Destroy()
	end
	self.tracks = {}
	if self.currentTrack then
		self.currentTrack:Stop(0)
	end
	self.currentTrack = nil
	self.currentState = nil
	for _, conn in self._attributeConns do
		conn:Disconnect()
	end
	self._attributeConns = {}
	if self.rig then
		CharacterAnimator._registry[self.rig] = nil
		CharacterAnimator._pending[self.rig] = nil
	end
end

return CharacterAnimator
