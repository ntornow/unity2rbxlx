local Coin = {}
Coin.__index = Coin

-- static public Pooler coinPool — shared across all instances (set by other scripts)
Coin.coinPool = nil

function Coin.new(config)
	local self = setmetatable({}, Coin)
	self.isPremium = config.isPremium or false
	return self
end

return Coin