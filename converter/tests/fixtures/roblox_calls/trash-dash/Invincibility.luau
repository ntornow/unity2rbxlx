local Consumable = require(game:GetService("ReplicatedStorage"):FindFirstChild("Consumable", true) or game:GetService("ServerStorage"):FindFirstChild("Consumable", true))

local Invincibility = {}
Invincibility.__index = Invincibility

function Invincibility.new(config)
	-- Inherit Consumable's pure constructor, then re-key the metatable to this subclass.
	local self = Consumable.new(config)
	return setmetatable(self, Invincibility)
end

setmetatable(Invincibility, { __index = Consumable })

function Invincibility:GetConsumableName()
	return "Invincible"
end

function Invincibility:GetConsumableType()
	return Consumable.ConsumableType.INVINCIBILITY
end

function Invincibility:GetPrice()
	return 1500
end

function Invincibility:GetPremiumCost()
	return 5
end

function Invincibility:Tick(c)
	Consumable.Tick(self, c) -- base.Tick(c)

	c.characterCollider:SetInvincibleExplicit(true)
end

-- IEnumerator Started(c) — driven via self.host.startCoroutine by the caller.
function Invincibility:Started(c)
	Consumable.Started(self, c) -- yield return base.Started(c)
	c.characterCollider:SetInvincible(self.duration)
end

function Invincibility:Ended(c)
	Consumable.Ended(self, c) -- base.Ended(c)
	c.characterCollider:SetInvincibleExplicit(false)
end

return Invincibility