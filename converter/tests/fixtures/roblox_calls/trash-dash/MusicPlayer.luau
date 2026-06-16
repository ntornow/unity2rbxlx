local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local MusicPlayer = {}
MusicPlayer.__index = MusicPlayer

-- static protected MusicPlayer s_Instance / static public instance getter.
-- Exposed as a field (MusicPlayer.instance) for cross-script access.
MusicPlayer.instance = nil

-- [System.Serializable] class Stem { AudioSource source; AudioClip clip; float
-- startingSpeedRatio; } -> each stem is injected as a plain table on self.stems
-- with fields { source = <Sound>, clip = <rbxassetid string>, startingSpeedRatio }.

function MusicPlayer.new(config)
	local self = setmetatable({}, MusicPlayer)

	self.maxVolume = config.maxVolume or 0.1

	return self
end

function MusicPlayer:Awake()
	if MusicPlayer.instance ~= nil then
		self.host.destroy(self.gameObject)
		return
	end

	MusicPlayer.instance = self

	-- Application.targetFrameRate = 30: no Roblox equivalent (engine manages frame rate).
	-- AudioListener.pause = false: no Roblox equivalent (no global audio listener pause).
	-- DontDestroyOnLoad(gameObject): no Roblox equivalent (no scene-load teardown).
end

function MusicPlayer:Start()
	PlayerData.Create()

	if PlayerData.instance.masterVolume > -math.huge then
		-- mixer:SetFloat("MasterVolume"/"MusicVolume"/"MasterSFXVolume", ...):
		-- Unity AudioMixer has no Roblox equivalent (no exposed mixer parameter
		-- graph). Volumes are tracked in PlayerData only; nothing to push here.
	else
		-- mixer:GetFloat(...) out-params: no Roblox AudioMixer to read from, so
		-- PlayerData's volume fields stay at their current values. Persist so the
		-- "already set" branch runs on the next launch.
		PlayerData.instance:Save()
	end

	self.host.startCoroutine(self, function()
		self:RestartAllStems()
	end)
end

function MusicPlayer:SetStem(index, clip)
	if #self.stems <= index then
		warn("Trying to set an undefined stem")
		return
	end

	self.stems[index].clip = clip
end

function MusicPlayer:GetStem(index)
	if #self.stems <= index then
		return nil
	end

	return self.stems[index].clip
end

function MusicPlayer:RestartAllStems()
	for i = 1, #self.stems do
		local stem = self.stems[i]
		stem.source.SoundId = stem.clip
		stem.source.Volume = 0.0
		stem.source:Play()
	end

	-- Fix for the Audio Mixer attenuation-lag bug: start every source at volume
	-- 0, wait 50 ms, then set the real volumes.
	task.wait(0.05)

	for i = 1, #self.stems do
		local stem = self.stems[i]
		if stem.startingSpeedRatio <= 0.0 then
			stem.source.Volume = self.maxVolume
		else
			stem.source.Volume = 0.0
		end
	end
end

function MusicPlayer:UpdateVolumes(currentSpeedRatio)
	local fadeSpeed = 0.5

	-- Time.deltaTime: this method is driven externally (not from Update), so
	-- derive the per-call delta from the wall clock between successive calls.
	local now = os.clock()
	local dt = self._lastVolumeUpdate and (now - self._lastVolumeUpdate) or 0
	self._lastVolumeUpdate = now

	for i = 1, #self.stems do
		local stem = self.stems[i]

		local target
		if currentSpeedRatio >= stem.startingSpeedRatio then
			target = self.maxVolume
		else
			target = 0.0
		end

		-- Mathf.MoveTowards(current, target, fadeSpeed * Time.deltaTime).
		local current = stem.source.Volume
		local maxDelta = fadeSpeed * dt
		local newVolume
		if math.abs(target - current) <= maxDelta then
			newVolume = target
		elseif target > current then
			newVolume = current + maxDelta
		else
			newVolume = current - maxDelta
		end

		stem.source.Volume = newVolume
	end
end

return MusicPlayer