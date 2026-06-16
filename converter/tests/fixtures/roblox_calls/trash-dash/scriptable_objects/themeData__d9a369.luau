-- Auto-generated from themeData.asset
-- ScriptableObject: themeData (class: ThemeData)

local data = {
		GeneratorAsset = nil --[[(Unity object reference)]],
		themeName = "NightTime",
		cost = 1000,
		premiumCost = 15,
		themeIcon = nil --[[(Unity object reference)]],
		zones = {
			{
				length = 500,
				prefabList = {
					{
						AssetGUID = "baf31dc160e3099428326b5a83db46cd",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "7f9eed4d340d06141a3c57dc69d8ed2c",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "6b55f4706bf1aa048a56792177cb63dc",
						CachedAsset = nil --[[(Unity object reference)]],
					},
				},
			},
			{
				length = 500,
				prefabList = {
					{
						AssetGUID = "226b344b8bdac594ea142309336337fe",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "49aa51c23aae1814da08106efff9839f",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "654b47fe18f35714aaf36bfa70b02983",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "b27a4d1577ea1bd4f8a7f3dce60bf85c",
						CachedAsset = nil --[[(Unity object reference)]],
					},
				},
			},
			{
				length = 500,
				prefabList = {
					{
						AssetGUID = "fd02fb81cd8dfa94fb3811b38b78b386",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "10f2606a4e949f5489f1ce27b91600df",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "4add46b17d5814744bb1498b198ed25d",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "ed24d8dd91f5c544c8df6d3cf6dffc4c",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "de7a496cae0fcec4ead023eb1ebf6257",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "fee9019dfd7946b40892dcef5d3fcc42",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "e01ea15bda91ee542a24f9c6f54bf6d3",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "5d29e217ab3c1874d8d99169dabbbe35",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "e7a3bd1684c9dc34895c2b9f97f1aeae",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "5bc3c5bd95058764489501ac1024b7ea",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "9a4b7a23296b48b4981881bd17953ed7",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "ee7969661b2b1e94891b8593e383883d",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "2ff30b5076d96ac4eb9ef7bf11a1bf5b",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "24e342794dd5ea94fa4836beedc3a776",
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
			r = 0.13559689,
			g = 0.27861834,
			b = 0.5588235,
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
