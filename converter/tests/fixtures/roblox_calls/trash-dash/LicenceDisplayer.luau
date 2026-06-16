local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local LicenceDisplayer = {}
LicenceDisplayer.__index = LicenceDisplayer

function LicenceDisplayer.new(config)
	local self = setmetatable({}, LicenceDisplayer)
	return self
end

function LicenceDisplayer:Start()
	PlayerData.Create()

	if PlayerData.instance.licenceAccepted then
		-- If we have already accepted the licence, we close the popup, no need for it.
		self:Close()
	end
end

function LicenceDisplayer:Accepted()
	PlayerData.instance.licenceAccepted = true
	PlayerData.instance:Save()
	self:Close()
end

function LicenceDisplayer:Refuse()
	-- UNCONVERTED: Application.Quit() has no Roblox equivalent (cannot terminate
	-- the client process from a game script).
end

function LicenceDisplayer:Close()
	self.host.setActive(self.gameObject, false)
end

return LicenceDisplayer