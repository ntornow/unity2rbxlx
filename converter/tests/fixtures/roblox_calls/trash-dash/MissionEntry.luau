local MissionEntry = {}
MissionEntry.__index = MissionEntry

function MissionEntry.new(config)
	local self = setmetatable({}, MissionEntry)
	-- Serialized Color fields. Read scalars only; defer everything host-bound to Awake.
	self.notCompletedColor = config.notCompletedColor
	self.completedColor = config.completedColor
	return self
end

-- public void FillWithMission(MissionBase m, MissionUI owner)
-- descText/rewardText/claimButton/progressText/background are serialized scene
-- references injected on self by name before Awake.
function MissionEntry:FillWithMission(m, owner)
	self.descText.Text = m:GetMissionDesc()
	self.rewardText.Text = tostring(m.reward)

	if m:isComplete() then
		self.host.setActive(self.claimButton, true)
		self.host.setActive(self.progressText, false)

		self.background.ImageColor3 = self.completedColor

		self.progressText.TextColor3 = Color3.new(1, 1, 1)
		self.descText.TextColor3 = Color3.new(1, 1, 1)
		self.rewardText.TextColor3 = Color3.new(1, 1, 1)

		-- claimButton.onClick.AddListener(delegate { owner.Claim(m); })
		self.claimButton.MouseButton1Click:Connect(function()
			owner:Claim(m)
		end)
	else
		self.host.setActive(self.claimButton, false)
		self.host.setActive(self.progressText, true)

		self.background.ImageColor3 = self.notCompletedColor

		self.progressText.TextColor3 = Color3.new(0, 0, 0)
		self.descText.TextColor3 = self.completedColor

		self.progressText.Text = math.floor(m.progress) .. " / " .. math.floor(m.max)
	end
end

return MissionEntry