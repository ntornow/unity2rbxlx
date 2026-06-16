local MusicPlayer = require(game:GetService("ReplicatedStorage"):FindFirstChild("MusicPlayer", true) or game:GetService("ServerStorage"):FindFirstChild("MusicPlayer", true))

local AssignOutputChannel = {}
AssignOutputChannel.__index = AssignOutputChannel

function AssignOutputChannel.new(config)
	local self = setmetatable({}, AssignOutputChannel)

	self.mixerGroup = config.mixerGroup or ""

	return self
end

function AssignOutputChannel:Awake()
	local source = self:GetComponent("AudioSource")

	if source == nil then
		warn("That object don't have any audio source, can't change it's output")
		self.host.destroy(self)
		return
	end

	-- AudioMixerGroup[] groups = MusicPlayer.instance.mixer.FindMatchingGroups(mixerGroup):
	-- UNCONVERTED: Unity AudioMixer / AudioMixerGroup has no Roblox equivalent (no
	-- exposed mixer-group graph). The original loop matched a named mixer group and
	-- assigned source.outputAudioMixerGroup; Roblox Sounds route through SoundGroups,
	-- but MusicPlayer exposes no mixer handle to resolve a group from, so there is
	-- nothing to assign here. The AudioSource is left on its default output.
	if MusicPlayer.instance == nil then
		warn(string.format("Could not find any group called %s", tostring(self.mixerGroup)))
	end
end

return AssignOutputChannel