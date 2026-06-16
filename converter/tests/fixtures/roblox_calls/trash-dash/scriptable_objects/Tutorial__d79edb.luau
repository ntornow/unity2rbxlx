-- Auto-generated from Tutorial.asset
-- ScriptableObject: Tutorial (class: ThemeData)

local data = {
		themeName = "Tutorial",
		cost = 0,
		premiumCost = 0,
		themeIcon = nil --[[(Unity object reference)]],
		zones = {
			{
				length = 0,
				prefabList = {
					{
						AssetGUID = "68a52f3ad60bf6e4d8165784f5dffd9e",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "19a9ea291ba68194c8cac8f13a84fe83",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "e64ce49b27bab6e49a4fcec2c01f3602",
						CachedAsset = nil --[[(Unity object reference)]],
					},
				},
			},
			{
				length = 0,
				prefabList = {
					{
						AssetGUID = "f249b9db828e0cf488639f91ddce9646",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "f6e9d5bba1674a148b19274e4b74ccf2",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "2337a0b8ee44c884ea9a30757fe06a71",
						CachedAsset = nil --[[(Unity object reference)]],
					},
				},
			},
			{
				length = 0,
				prefabList = {
					{
						AssetGUID = "e4a6e14a3028d0745b6de33d884bf62a",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "8515fe4be5c88bc4594fa0b2c4165a95",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "f1fdce3067b0cf842a46d3d44dd9e841",
						CachedAsset = nil --[[(Unity object reference)]],
					},
				},
			},
		},
		collectiblePrefab = nil --[[(Unity object reference)]],
		premiumCollectible = nil --[[(Unity object reference)]],
		cloudPrefabs = {
			nil --[[(Unity object reference)]],
			nil --[[(Unity object reference)]],
			nil --[[(Unity object reference)]],
		},
		cloudMinimumDistance = {
			x = 0,
			y = 10,
			z = 10,
		},
		cloudSpread = {
			x = 20,
			y = 1,
			z = 20,
		},
		cloudNumber = 20,
		skyMesh = nil --[[(Unity object reference)]],
		UIGroundMesh = nil --[[(Unity object reference)]],
		fogColor = {
			r = 0.86274517,
			g = 0.9725491,
			b = 1,
			a = 1,
		},
	}

-- ScriptableObject asset = instance of its class; lazy, fail-open
-- method binding (see converter/scriptable_object_converter.py).
local _cls = nil
return setmetatable(data, {
	__index = function(_, _key)
		if _cls == nil then
			local _m = game:GetService("ReplicatedStorage"):FindFirstChild("ThemeData", true)
				or game:GetService("ServerStorage"):FindFirstChild("ThemeData", true)
			if _m ~= nil then
				local _ok, _mod = pcall(require, _m)
				if _ok and type(_mod) == "table" then _cls = _mod end
			end
			if _cls == nil then return nil end  -- unresolved; don't memoize the miss
		end
		return _cls[_key]
	end,
})
