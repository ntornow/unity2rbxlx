local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local MissionUI = {}
MissionUI.__index = MissionUI

function MissionUI.new(config)
	local self = setmetatable({}, MissionUI)
	-- No scalar config fields. missionPlace (serialized RectTransform scene ref),
	-- missionEntryPrefab / addMissionButtonPrefab (AssetReference prefab refs) are
	-- injected on self by name before Awake; do NOT read them here.
	return self
end

-- public IEnumerator Open()
-- Coroutine body as a method; external callers wrap it via
-- self.host.startCoroutine(self, function() ui:Open() end).
function MissionUI:Open()
	self.host.setActive(self.gameObject, true)

	-- foreach (Transform t in missionPlace) Addressables.ReleaseInstance(t.gameObject)
	for _, t in self.missionPlace:GetChildren() do
		self.host.destroy(t)
	end

	for i = 0, 2 do
		if #PlayerData.instance.missions > i then
			-- missionEntryPrefab.InstantiateAsync(); yield return op
			-- instantiatePrefab is synchronous here, so the async yield collapses.
			local go = self.host.instantiatePrefab(self.missionEntryPrefab, self.missionPlace)
			if go == nil then
				warn(string.format("Unable to load mission entry %s.", tostring(self.missionEntryPrefab)))
				return
			end
			local entry = self.host.getComponent(go, "MissionEntry")
			-- C# 0-based missions[i] -> Luau 1-based [i + 1].
			entry:FillWithMission(PlayerData.instance.missions[i + 1], self)
		else
			local go = self.host.instantiatePrefab(self.addMissionButtonPrefab, self.missionPlace)
			if go == nil then
				warn(string.format("Unable to load button %s.", tostring(self.addMissionButtonPrefab)))
				return
			end
			local obj = self.host.getComponent(go, "AdsForMission")
			obj.missionUI = self
		end
	end
end

-- public void CallOpen()
function MissionUI:CallOpen()
	self.host.setActive(self.gameObject, true)
	self.host.startCoroutine(self, function()
		self:Open()
	end)
end

-- public void Claim(MissionBase m)
function MissionUI:Claim(m)
	PlayerData.instance:ClaimMission(m)

	-- Rebuild the UI with the new missions
	self.host.startCoroutine(self, function()
		self:Open()
	end)
end

-- public void Close()
function MissionUI:Close()
	self.host.setActive(self.gameObject, false)
end

return MissionUI