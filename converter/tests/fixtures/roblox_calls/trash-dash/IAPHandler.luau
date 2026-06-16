local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local IAPHandler = {}
IAPHandler.__index = IAPHandler

function IAPHandler.new(config)
	local self = setmetatable({}, IAPHandler)
	return self
end

function IAPHandler:OnEnable()
	-- AnalyticsEvent.StoreOpened(StoreType.Premium) omitted: Unity Analytics has
	-- no Roblox equivalent. Nothing else happened here.
end

function IAPHandler:ProductBought(product)
	local amount = 0
	local id = product.definition.id
	if id == "10_premium" then
		amount = 10
	elseif id == "50_premium" then
		amount = 50
	elseif id == "100_premium" then
		amount = 100
	end

	if amount > 0 then
		PlayerData.instance.premium = PlayerData.instance.premium + amount
		PlayerData.instance:Save()

		-- UNITY_ANALYTICS AnalyticsEvent.IAPTransaction / ItemAcquired block
		-- omitted: Unity Analytics has no Roblox equivalent.
	end
end

function IAPHandler:ProductError(product, reason)
	warn("Product : " .. product.definition.id .. " couldn't be bought because " .. tostring(reason))
end

return IAPHandler