local Consumable = require(game:GetService("ReplicatedStorage"):FindFirstChild("Consumable", true) or game:GetService("ServerStorage"):FindFirstChild("Consumable", true))

local ConsumableType = Consumable.ConsumableType

local STUDS_PER_METER = 3.571

-- k_LayerMask = 1 << 8 — Unity layer mask has no Roblox overlap-query equivalent;
-- coin filtering is done by CollectionService tag below instead.

local CoinMagnet = {}
-- Class-level fallback to the base Consumable preserves inherited methods without a
-- module-scope setmetatable call: a function __index is a definition, not a side effect.
CoinMagnet.__index = function(_, k)
	return rawget(CoinMagnet, k) or Consumable[k]
end

function CoinMagnet.new(config)
	-- Chain to the base Consumable constructor (scalar config only), then re-tag the
	-- metatable to CoinMagnet so overrides resolve first and base methods fall through.
	local self = Consumable.new(config)
	setmetatable(self, CoinMagnet)
	-- returnColls (Collider[20]) was a NonAlloc reuse buffer; Roblox overlap queries
	-- return a fresh table, so the preallocated array is dropped.
	return self
end

function CoinMagnet:GetConsumableName()
	return "Magnet"
end

function CoinMagnet:GetConsumableType()
	return ConsumableType.COIN_MAG
end

function CoinMagnet:GetPrice()
	return 750
end

function CoinMagnet:GetPremiumCost()
	return 0
end

function CoinMagnet:Tick(c)
	Consumable.Tick(self, c) -- base.Tick(c)

	local CollectionService = game:GetService("CollectionService")

	-- k_HalfExtentsBox: Unity half-extents in metres (Vector3(20, 1, 1)).
	local k_HalfExtentsBox = Vector3.new(20.0, 1.0, 1.0)

	local colliderObj = c.characterCollider.gameObject
	-- transform.position + transform.rotation -> the collider's pivot CFrame.
	local boxCFrame = colliderObj:GetPivot()
	-- Physics.OverlapBoxNonAlloc takes HALF extents in metres; Roblox box query wants
	-- FULL size in studs.
	local boxSize = (k_HalfExtentsBox * 2) * STUDS_PER_METER
	local params = OverlapParams.new()
	local parts = workspace:GetPartBoundsInBox(boxCFrame, boxSize, params)

	for _, returnColl in parts do
		-- GetComponent<Coin>() on the overlapped collider: no host instance->component
		-- lookup exists for an arbitrary found part, so identify coins by tag and read
		-- the isPremium attribute mirror the Coin carries.
		if CollectionService:HasTag(returnColl, "Coin") then
			local isPremium = returnColl:GetAttribute("isPremium") or false
			if not isPremium and table.find(c.characterCollider.magnetCoins, returnColl) == nil then
				returnColl.Parent = c.gameObject -- returnColls[i].transform.SetParent(c.transform)
				table.insert(c.characterCollider.magnetCoins, returnColl)
			end
		end
	end
end

return CoinMagnet