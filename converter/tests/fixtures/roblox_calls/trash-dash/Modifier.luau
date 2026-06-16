-- Modifier: base class for modifying game state (limit run length, seed, etc.)
-- Subclass via setmetatable and override the wanted messages.

local Modifier = {}
Modifier.__index = Modifier

function Modifier.new()
	local self = setmetatable({}, Modifier)
	return self
end

function Modifier:OnRunStart(state)
end

function Modifier:OnRunTick(state)
end

-- return true if the game over screen should be displayed; returning false
-- returns directly to loadout (useful for challenges)
function Modifier:OnRunEnd(state)
	return true
end

-- The following classes are all the sample modifiers.

local LimitedLengthRun = setmetatable({}, { __index = Modifier })
LimitedLengthRun.__index = LimitedLengthRun

function LimitedLengthRun.new(dist)
	local self = setmetatable(Modifier.new(), LimitedLengthRun)
	self.distance = dist
	return self
end

function LimitedLengthRun:OnRunTick(state)
	if state.trackManager.worldDistance >= self.distance then
		state.trackManager.characterController.currentLife = 0
	end
end

function LimitedLengthRun:OnRunStart(state)
end

function LimitedLengthRun:OnRunEnd(state)
	state:QuitToLoadout()
	return false
end

local SeededRun = setmetatable({}, { __index = Modifier })
SeededRun.__index = SeededRun

local k_DaysInAWeek = 7

function SeededRun.new()
	local self = setmetatable(Modifier.new(), SeededRun)
	-- System.DateTime.Now.DayOfYear / k_DaysInAWeek (integer division)
	self.m_Seed = math.floor(os.date("*t").yday / k_DaysInAWeek)
	return self
end

function SeededRun:OnRunStart(state)
	state.trackManager.trackSeed = self.m_Seed
end

function SeededRun:OnRunEnd(state)
	state:QuitToLoadout()
	return false
end

local SingleLifeRun = setmetatable({}, { __index = Modifier })
SingleLifeRun.__index = SingleLifeRun

function SingleLifeRun.new()
	local self = setmetatable(Modifier.new(), SingleLifeRun)
	return self
end

function SingleLifeRun:OnRunTick(state)
	if state.trackManager.characterController.currentLife > 1 then
		state.trackManager.characterController.currentLife = 1
	end
end

function SingleLifeRun:OnRunStart(state)
end

function SingleLifeRun:OnRunEnd(state)
	state:QuitToLoadout()
	return false
end

Modifier.LimitedLengthRun = LimitedLengthRun
Modifier.SeededRun = SeededRun
Modifier.SingleLifeRun = SingleLifeRun

return Modifier