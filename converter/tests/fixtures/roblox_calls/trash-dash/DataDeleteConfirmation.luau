local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local DataDeleteConfirmation = {}
DataDeleteConfirmation.__index = DataDeleteConfirmation

function DataDeleteConfirmation.new(config)
	local self = setmetatable({}, DataDeleteConfirmation)
	-- protected LoadoutState m_LoadoutState; injected via Open(owner), not config.
	self.m_LoadoutState = nil
	return self
end

function DataDeleteConfirmation:Open(owner)
	self.host.setActive(self.gameObject, true)
	self.m_LoadoutState = owner
end

function DataDeleteConfirmation:Close()
	self.host.setActive(self.gameObject, false)
end

function DataDeleteConfirmation:Confirm()
	PlayerData.NewSave()
	self.m_LoadoutState:UnequipPowerup()
	self.m_LoadoutState:Refresh()
	self:Close()
end

function DataDeleteConfirmation:Deny()
	self:Close()
end

return DataDeleteConfirmation