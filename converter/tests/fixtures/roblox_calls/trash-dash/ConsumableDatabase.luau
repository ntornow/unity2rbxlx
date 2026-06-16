local Consumable = require(script.Parent.Consumable)

local ConsumableDatabase = {}

-- Designer-populated array of Consumable instances; assigned on the
-- table by the host before Load() (mirrors the serialized field).
ConsumableDatabase.consumbales = {}

-- static protected Dictionary<ConsumableType, Consumable>
local _consumablesDict = nil

function ConsumableDatabase:Load()
	if _consumablesDict == nil then
		_consumablesDict = {}

		for i = 1, #self.consumbales do
			local c = self.consumbales[i]
			_consumablesDict[c:GetConsumableType()] = c
		end
	end
end

-- static Consumable GetConsumbale(ConsumableType type)
function ConsumableDatabase.GetConsumbale(type)
	local c = _consumablesDict[type]
	if c ~= nil then
		return c
	end
	return nil
end

return ConsumableDatabase