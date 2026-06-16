local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local AdsForMission = {}
AdsForMission.__index = AdsForMission

function AdsForMission.new(config)
	local self = setmetatable({}, AdsForMission)
	-- Scalar config only. Reference fields (missionUI, newMissionText, adsButton)
	-- are injected on self before Awake; do NOT read them here.
	self.adsPlacementId = config.adsPlacementId or "rewardedVideo"
	self.adsRewarded = config.adsRewarded
	if self.adsRewarded == nil then
		self.adsRewarded = true
	end
	return self
end

function AdsForMission:OnEnable()
	-- adsButton.gameObject.SetActive(false) / newMissionText.gameObject.SetActive(false)
	self.host.setActive(self.adsButton, false)
	self.host.setActive(self.newMissionText, false)

	-- Only present an ad offer if less than 3 missions.
	if #PlayerData.instance.missions >= 3 then
		return
	end

	-- UNCONVERTED: UNITY_ADS (Advertisement.IsReady / AnalyticsEvent.AdOffer) has
	-- no Roblox equivalent — there is no rewarded-video ad network in Roblox, so
	-- the ad-readiness gate and the button/text reveal it drives are dropped. The
	-- buttons stay hidden (the default set above), matching "no ad available".
end

-- public void ShowAds()
function AdsForMission:ShowAds()
	-- UNCONVERTED: UNITY_ADS (Advertisement.Show / IsReady / AnalyticsEvent.*) has
	-- no Roblox equivalent. With no ad to show, the reward path never fires; a
	-- direct caller wanting to grant the mission should invoke AddNewMission().
end

-- private void HandleShowResult(ShowResult result)
function AdsForMission:HandleShowResult(result)
	-- UNCONVERTED: UNITY_ADS ShowResult callback (ad network result dispatch) has
	-- no Roblox equivalent. The Finished branch's effect is preserved in
	-- AddNewMission below.
end

-- void AddNewMission()
function AdsForMission:AddNewMission()
	PlayerData.instance:AddMission()
	PlayerData.instance:Save()
	-- StartCoroutine(missionUI.Open())
	self.host.startCoroutine(self, function()
		self.missionUI:Open()
	end)
end

return AdsForMission