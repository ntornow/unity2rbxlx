-- Handles loading data from the Asset Bundle to handle different themes for the game

local ThemeDatabase = {}

-- static protected Dictionary<string, ThemeData> themeDataList
local themeDataList = nil

-- static protected bool m_Loaded
local m_Loaded = false

-- static public Dictionary<string, ThemeData> dictionnary { get { return themeDataList; } }
function ThemeDatabase.dictionnary()
	return themeDataList
end

-- static public bool loaded { get { return m_Loaded; } }
function ThemeDatabase.loaded()
	return m_Loaded
end

-- static public ThemeData GetThemeData(string type)
function ThemeDatabase.GetThemeData(type)
	if themeDataList == nil then
		return nil
	end
	local list = themeDataList[type]
	if list == nil then
		return nil
	end
	return list
end

-- static public IEnumerator LoadDatabase()
-- Call via task.spawn(ThemeDatabase.LoadDatabase). Yields with task.wait while loading.
function ThemeDatabase.LoadDatabase()
	-- If not nil the dictionary was already loaded.
	if themeDataList == nil then
		themeDataList = {}

		-- UNCONVERTED: Addressables.LoadAssetsAsync<ThemeData>("themeData", ...) has no
		-- Roblox equivalent. Unity's Addressables asset-bundle system loads ThemeData
		-- assets at runtime by label. In Roblox, ThemeData definitions must be supplied
		-- at build time. Populate via ThemeDatabase.Register below; the per-asset
		-- callback semantics (skip duplicate themeName keys) are preserved there.

		m_Loaded = true
	end
end

-- Build-time registration replacing the Addressables load callback:
--   if (!themeDataList.ContainsKey(op.themeName)) themeDataList.Add(op.themeName, op)
function ThemeDatabase.Register(themeData)
	if themeDataList == nil then
		themeDataList = {}
	end
	if themeData ~= nil and themeDataList[themeData.themeName] == nil then
		themeDataList[themeData.themeName] = themeData
	end
end

return ThemeDatabase