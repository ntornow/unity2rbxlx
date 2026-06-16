local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

-- protected const float k_MinVolume = -80f
local k_MinVolume = -80
-- protected const string k_MasterVolumeFloatName = "MasterVolume"
local k_MasterVolumeFloatName = "MasterVolume"
-- protected const string k_MusicVolumeFloatName = "MusicVolume"
local k_MusicVolumeFloatName = "MusicVolume"
-- protected const string k_MasterSFXVolumeFloatName = "MasterSFXVolume"
local k_MasterSFXVolumeFloatName = "MasterSFXVolume"

local SettingPopup = {}
SettingPopup.__index = SettingPopup

function SettingPopup.new(config)
	local self = setmetatable({}, SettingPopup)
	-- Pure constructor: only the protected float fields default to 0. All serialized
	-- reference fields (mixer, the three Sliders, loadoutState, confirmationPopup)
	-- are injected on self by the host before Awake; never read here.
	self.m_MasterVolume = 0
	self.m_MusicVolume = 0
	self.m_MasterSFXVolume = 0
	return self
end

function SettingPopup:Open()
	self.host.setActive(self.gameObject, true)
	self:UpdateUI()
end

function SettingPopup:Close()
	PlayerData.instance:Save()
	self.host.setActive(self.gameObject, false)
end

function SettingPopup:UpdateUI()
	-- AudioMixer.GetFloat(name, out value) has no Roblox equivalent (no exposed
	-- mixer-parameter graph); the read is inert and leaves m_* at their last value.
	-- (self.mixer is the injected serialized AudioMixer reference.)

	-- Slider.value is a UI value with no native Roblox Slider; mirror onto .Value
	-- (consistent with the inert UI-ref assignments elsewhere in this game).
	self.masterSlider.Value = 1.0 - (self.m_MasterVolume / k_MinVolume)
	self.musicSlider.Value = 1.0 - (self.m_MusicVolume / k_MinVolume)
	self.masterSFXSlider.Value = 1.0 - (self.m_MasterSFXVolume / k_MinVolume)
end

function SettingPopup:DeleteData()
	self.confirmationPopup:Open(self.loadoutState)
end

function SettingPopup:MasterVolumeChangeValue(value)
	self.m_MasterVolume = k_MinVolume * (1.0 - value)
	-- AudioMixer.SetFloat: inert (no Roblox mixer-parameter graph).
	PlayerData.instance.masterVolume = self.m_MasterVolume
end

function SettingPopup:MusicVolumeChangeValue(value)
	self.m_MusicVolume = k_MinVolume * (1.0 - value)
	-- AudioMixer.SetFloat: inert (no Roblox mixer-parameter graph).
	PlayerData.instance.musicVolume = self.m_MusicVolume
end

function SettingPopup:MasterSFXVolumeChangeValue(value)
	self.m_MasterSFXVolume = k_MinVolume * (1.0 - value)
	-- AudioMixer.SetFloat: inert (no Roblox mixer-parameter graph).
	PlayerData.instance.masterSFXVolume = self.m_MasterSFXVolume
end

return SettingPopup