-- Auto-generated from themeData.asset
-- ScriptableObject: themeData (class: ThemeData)

local data = {
		GeneratorAsset = nil --[[(Unity object reference)]],
		themeName = "Day",
		cost = 0,
		premiumCost = 0,
		themeIcon = nil --[[(Unity object reference)]],
		zones = {
			{
				length = 500,
				prefabList = {
					{
						AssetGUID = "2d4807022e73aa34ab8f01892dbdfe1d",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "81bf7d820e3d0554e9ea968eeba33052",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "747e617dac045eb4f9fa9978b75bb475",
						CachedAsset = nil --[[(Unity object reference)]],
					},
				},
			},
			{
				length = 500,
				prefabList = {
					{
						AssetGUID = "dcbeb2ef97131d241b111f35d4788477",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "efcbfaf146228ad40ae6c878d7806d64",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "5d6fcbaf51bca6b4594f12ca78b10a69",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "544006c54b323e9419926850d7e3fbac",
						CachedAsset = nil --[[(Unity object reference)]],
					},
				},
			},
			{
				length = 500,
				prefabList = {
					{
						AssetGUID = "2f8abb5887b4af046b2f7fe9190c9d1d",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "0e145376c7d52444b97856849e2a7bd2",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "627ea187cfa7b524482f0725ce9ffafe",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "03576c724d3dcac4690f6ede40e47639",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "391abdb78f486ff4abd13845846e0f05",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "9b7ee2db83af4fe4da3b2af7d258d635",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "adb7c187800cf1d4697c2ec26f0a4bd1",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "3a5b1ed81a7bde440aa6d2317adcd689",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "65d6a8c569ebabb48aa95414228089bc",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "01466a005c28535479de3c39b3304e3f",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "423b9549e9d84034aac50ab7e0459e77",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "6d998870eb4591747a98ab06751e0227",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "9f0db6c5e9d5e3e4d9c0f51362e59d00",
						CachedAsset = nil --[[(Unity object reference)]],
					},
					{
						AssetGUID = "747e617dac045eb4f9fa9978b75bb475",
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
