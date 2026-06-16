local Consumable = require(game:GetService("ReplicatedStorage"):FindFirstChild("Consumable", true) or game:GetService("ServerStorage"):FindFirstChild("Consumable", true))

local k_MaxLives = 3
local k_CoinValue = 10

local ExtraLife = {}

local function extraLifeIndex(_, k)
	return rawget(ExtraLife, k) or Consumable[k]
end
ExtraLife.__index = extraLifeIndex

function ExtraLife.new(config)
	local self = Consumable.new(config)
	setmetatable(self, ExtraLife)
	return self
end

function ExtraLife:GetConsumableName()
	return "Life"
end

function ExtraLife:GetConsumableType()
	return Consumable.ConsumableType.EXTRALIFE
end

function ExtraLife:GetPrice()
	return 2000
end

function ExtraLife:GetPremiumCost()
	return 5
end

function ExtraLife:CanBeUsed(c)
	if c.currentLife == c.maxLife then
		return false
	end

	return true
end

-- IEnumerator Started(c) — driven via self.host.startCoroutine by the caller.
function ExtraLife:Started(c)
	-- yield return base.Started(c): base coroutine body runs synchronously to completion.
	Consumable.Started(self, c)
	if c.currentLife < k_MaxLives then
		c.currentLife = c.currentLife + 1
	else
		c.coins = c.coins + k_CoinValue
	end
end

return ExtraLife