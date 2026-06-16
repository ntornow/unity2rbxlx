-- _AutoUnityTransformChild: Unity transform.GetChild(n) indexes only child
-- GameObjects (Transforms); Roblox GetChildren() also returns injected Sounds/
-- Scripts/etc., so raw GetChildren()[n] grabs the wrong instance. Authored
-- GameObject hosts carry a _SceneRuntimeId attribute -- index those (1-based,
-- mirroring the GetChildren()[n] call sites this replaces).
local function __unityChild(parent, i)
	if not parent then return nil end
	local n = 0
	for _, c in ipairs(parent:GetChildren()) do
		if c:GetAttribute("_SceneRuntimeId") ~= nil then
			n += 1
			if n == i then return c end
		end
	end
	-- Fallback for unstamped/legacy output: nth Model/BasePart child.
	n = 0
	for _, c in ipairs(parent:GetChildren()) do
		if c:IsA("Model") or c:IsA("BasePart") then
			n += 1
			if n == i then return c end
		end
	end
	return nil
end

local ShopList = require(game:GetService("ReplicatedStorage"):FindFirstChild("ShopList", true) or game:GetService("ServerStorage"):FindFirstChild("ShopList", true))
local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))
local ThemeDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("ThemeDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("ThemeDatabase", true))

-- ShopThemeList : ShopList. Inherit Open/Close/Refresh from ShopList; override
-- Populate and add Buy/RefreshButton. __index is defined as a function so the
-- class-table inheritance chain is established WITHOUT a module-scope
-- setmetatable() call (which would be a forbidden top-level side-effect).
local ShopThemeList = {}

function ShopThemeList.__index(t, k)
    return ShopThemeList[k] or ShopList[k]
end

function ShopThemeList.new(config)
    local self = setmetatable({}, ShopThemeList)
    -- prefabItem / listRoot are serialized references injected on self by the
    -- host before Awake; do NOT read them here. m_RefreshCallback is the
    -- multicast refresh delegate (modeled as a composed function below).
    self.m_RefreshCallback = nil
    return self
end

function ShopThemeList:Populate()
    self.m_RefreshCallback = nil

    -- foreach (Transform t in listRoot) Destroy(t.gameObject)
    for _, t in self.listRoot:GetChildren() do
        self.host.destroy(t)
    end

    -- foreach (KeyValuePair<string, ThemeData> pair in ThemeDatabase.dictionnary)
    local dict = ThemeDatabase.dictionnary()
    if dict == nil then
        return
    end

    for _, theme in dict do
        if theme ~= nil then
            -- prefabItem.InstantiateAsync().Completed += (op) => { ... }.
            -- Addressables async instantiate -> synchronous host prefab spawn.
            -- The parent (listRoot) is supplied here, replacing the original
            -- newEntry.transform.SetParent(listRoot, false).
            local newEntry = self.host.instantiatePrefab(self.prefabItem, self.listRoot, nil)
            if newEntry == nil then
                warn(string.format("Unable to load theme shop list entry for %s.", tostring(theme.themeName)))
                continue
            end

            local itm = self.host.getComponent(newEntry, "ShopItemListItem")

            itm.nameText.Text = theme.themeName
            itm.pricetext.Text = tostring(theme.cost)
            itm.icon.Image = theme.themeIcon

            if theme.premiumCost > 0 then
                self.host.setActive(itm.premiumText.Parent, true)
                itm.premiumText.Text = tostring(theme.premiumCost)
            else
                self.host.setActive(itm.premiumText.Parent, false)
            end

            itm.buyButton.Activated:Connect(function()
                self:Buy(theme)
            end)

            itm.buyButton.Image = itm.buyButtonSprite

            self:RefreshButton(itm, theme)

            -- m_RefreshCallback += delegate() { RefreshButton(itm, theme); }
            -- Compose to emulate the C# multicast delegate so the inherited
            -- ShopList:Refresh() (which calls self.m_RefreshCallback()) runs all.
            local prev = self.m_RefreshCallback
            self.m_RefreshCallback = function()
                if prev then
                    prev()
                end
                self:RefreshButton(itm, theme)
            end
        end
    end
end

function ShopThemeList:RefreshButton(itm, theme)
    if theme.cost > PlayerData.instance.coins then
        itm.buyButton.Active = false
        itm.pricetext.TextColor3 = Color3.new(1, 0, 0)
    else
        itm.pricetext.TextColor3 = Color3.new(0, 0, 0)
    end

    if theme.premiumCost > PlayerData.instance.premium then
        itm.buyButton.Active = false
        itm.premiumText.TextColor3 = Color3.new(1, 0, 0)
    else
        itm.premiumText.TextColor3 = Color3.new(0, 0, 0)
    end

    if table.find(PlayerData.instance.themes, theme.themeName) ~= nil then
        itm.buyButton.Active = false
        itm.buyButton.Image = itm.disabledButtonSprite
        -- buyButton.transform.GetChild(0).GetComponent<Text>().text = "Owned"
        local label = __unityChild(itm.buyButton, 1)
        if label ~= nil then
            label.Text = "Owned"
        end
    end
end

function ShopThemeList:Buy(t)
    PlayerData.instance.coins = PlayerData.instance.coins - t.cost
    PlayerData.instance.premium = PlayerData.instance.premium - t.premiumCost
    PlayerData.instance:AddTheme(t.themeName)
    PlayerData.instance:Save()

    -- UNITY_ANALYTICS AnalyticsEvent.ItemAcquired / ItemSpent block omitted
    -- (no Roblox equivalent).

    -- Repopulate to change button accordingly.
    self:Populate()
end

return ShopThemeList