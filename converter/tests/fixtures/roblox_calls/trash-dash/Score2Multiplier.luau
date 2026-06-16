local Consumable = require(game:GetService("ReplicatedStorage"):FindFirstChild("Consumable", true) or game:GetService("ServerStorage"):FindFirstChild("Consumable", true))

local Score2Multiplier = {}
Score2Multiplier.__index = Score2Multiplier

function Score2Multiplier.new(config)
	-- Wire base-class fallback inside the constructor so module scope stays
	-- side-effect-free. Guarded so the metatable link is established once.
	if getmetatable(Score2Multiplier) == nil then
		setmetatable(Score2Multiplier, { __index = Consumable })
	end
	-- Reuse base field init, then re-point the metatable so overrides resolve.
	local self = Consumable.new(config)
	setmetatable(self, Score2Multiplier)
	return self
end

function Score2Multiplier:GetConsumableName()
	return "x2"
end

function Score2Multiplier:GetConsumableType()
	return Consumable.ConsumableType.SCORE_MULTIPLAYER
end

function Score2Multiplier:GetPrice()
	return 750
end

function Score2Multiplier:GetPremiumCost()
	return 0
end

-- IEnumerator Started(c) — driven via self.host.startCoroutine by the caller.
function Score2Multiplier:Started(c)
	-- yield return base.Started(c) — base body is synchronous in Luau.
	Consumable.Started(self, c)

	self.m_SinceStart = 0

	-- c.trackManager.modifyMultiply += MultiplyModify
	-- C# multicast delegate (int -> int) modeled as a list of modifier functions.
	-- Stash a single bound closure so the -= in Ended removes the same reference.
	self.m_MultiplyModify = self.m_MultiplyModify or function(multi)
		return self:MultiplyModify(multi)
	end
	c.trackManager.modifyMultiply = c.trackManager.modifyMultiply or {}
	table.insert(c.trackManager.modifyMultiply, self.m_MultiplyModify)
end

function Score2Multiplier:Ended(c)
	Consumable.Ended(self, c)

	-- c.trackManager.modifyMultiply -= MultiplyModify
	if c.trackManager.modifyMultiply ~= nil and self.m_MultiplyModify ~= nil then
		local idx = table.find(c.trackManager.modifyMultiply, self.m_MultiplyModify)
		if idx then
			table.remove(c.trackManager.modifyMultiply, idx)
		end
	end
end

function Score2Multiplier:MultiplyModify(multi)
	return multi * 2
end

return Score2Multiplier