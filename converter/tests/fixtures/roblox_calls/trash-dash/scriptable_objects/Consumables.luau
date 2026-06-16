-- Auto-generated from Consumables.asset
-- ScriptableObject: Consumables (class: ConsumableDatabase)

local data = {
		PrefabParentObject = nil --[[(Unity object reference)]],
		PrefabInternal = nil --[[(Unity object reference)]],
		consumbales = {
			nil --[[(Unity object reference)]],
			nil --[[(Unity object reference)]],
			nil --[[(Unity object reference)]],
			nil --[[(Unity object reference)]],
		},
	}

-- ScriptableObject asset = instance of its class; lazy, fail-open
-- method binding (see converter/scriptable_object_converter.py).
local _cls = nil
return setmetatable(data, {
	__index = function(_, _key)
		if _cls == nil then
			local _m = game:GetService("ReplicatedStorage"):FindFirstChild("ConsumableDatabase", true)
				or game:GetService("ServerStorage"):FindFirstChild("ConsumableDatabase", true)
			if _m ~= nil then
				local _ok, _mod = pcall(require, _m)
				if _ok and type(_mod) == "table" then _cls = _mod end
			end
			if _cls == nil then return nil end  -- unresolved; don't memoize the miss
		end
		return _cls[_key]
	end,
})
