-- ThemeZone struct: { length = 0, prefabList = {} }

local ThemeData = {}

-- Theme Data
ThemeData.themeName = ""
ThemeData.cost = 0
ThemeData.premiumCost = 0
ThemeData.themeIcon = nil

-- Objects
ThemeData.zones = {}
ThemeData.collectiblePrefab = nil
ThemeData.premiumCollectible = nil

-- Decoration
ThemeData.cloudPrefabs = {}
ThemeData.cloudMinimumDistance = Vector3.new(0, 20.0, 15.0)
ThemeData.cloudSpread = Vector3.new(5.0, 0.0, 1.0)
ThemeData.cloudNumber = 10
ThemeData.skyMesh = nil
ThemeData.UIGroundMesh = nil
ThemeData.fogColor = Color3.new(0, 0, 0)

return ThemeData