local ShopList = require(game:GetService("ReplicatedStorage"):FindFirstChild("ShopList", true) or game:GetService("ServerStorage"):FindFirstChild("ShopList", true))
local CharacterDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("CharacterDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("CharacterDatabase", true))
local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local ShopAccessoriesList = {}

-- Inherit ShopList methods (Refresh, base helpers) WITHOUT a module-scope
-- setmetatable call: __index is a plain function field that falls through to
-- the base table. ShopAccessoriesList itself has no metatable, so the indexing
-- inside this function is a raw lookup (no recursion).
function ShopAccessoriesList.__index(_, key)
    return ShopAccessoriesList[key] or ShopList[key]
end

function ShopAccessoriesList.new(config)
    local self = setmetatable({}, ShopAccessoriesList)
    -- Pure constructor: prefabItem / listRoot are serialized references injected
    -- on self by the host before Awake; headerPrefab is an AssetReference field
    -- injected as a prefab_id. Do NOT read host surface or those refs here.
    self.m_RefreshCallback = nil
    self.m_CharacterList = {}
    return self
end

function ShopAccessoriesList:Populate()
    self.m_RefreshCallback = nil

    -- foreach (Transform t in listRoot) Destroy(t.gameObject)
    if self.listRoot then
        for _, child in self.listRoot:GetChildren() do
            self.host.destroy(child)
        end
    end

    self.m_CharacterList = {}
    local dict = CharacterDatabase.dictionary()
    if dict then
        for _, c in dict do
            if c.accessories ~= nil and #c.accessories > 0 then
                table.insert(self.m_CharacterList, c)
            end
        end
    end

    -- Unity chained Addressables InstantiateAsync().Completed callbacks to walk
    -- characters then their accessories. Roblox host.instantiatePrefab is
    -- synchronous, so the async chain flattens to plain nested loops (same order).
    for ci = 1, #self.m_CharacterList do
        self:LoadedCharacter(self.m_CharacterList[ci])
    end
end

function ShopAccessoriesList:LoadedCharacter(c)
    local header = self.host.instantiatePrefab(self.headerPrefab, self.listRoot, nil)
    if header == nil then
        warn(string.format("Unable to load header %s.", tostring(self.headerPrefab)))
        return
    end

    -- ShopItemListItem.nameText serialized ref resolves to the entry's name label.
    local nameText = header:FindFirstChild("nameText", true)
    if nameText then
        nameText.Text = c.characterName
    end

    for ai = 1, #c.accessories do
        self:LoadedAccessory(c, ai)
    end
end

function ShopAccessoriesList:LoadedAccessory(c, accessoryIndex)
    local accessory = c.accessories[accessoryIndex]

    local newEntry = self.host.instantiatePrefab(self.prefabItem, self.listRoot, nil)
    if newEntry == nil then
        warn(string.format("Unable to load shop accessory list %s.", tostring(self.prefabItem)))
        return
    end

    local compoundName = c.characterName .. ":" .. accessory.accessoryName

    local nameText = newEntry:FindFirstChild("nameText", true)
    if nameText then
        nameText.Text = accessory.accessoryName
    end

    local pricetext = newEntry:FindFirstChild("pricetext", true)
    if pricetext then
        pricetext.Text = tostring(accessory.cost)
    end

    local icon = newEntry:FindFirstChild("icon", true)
    if icon and accessory.accessoryIcon then
        icon.Image = accessory.accessoryIcon
    end

    local buyButton = newEntry:FindFirstChild("buyButton", true)
    local premiumText = newEntry:FindFirstChild("premiumText", true)

    if accessory.premiumCost > 0 then
        if premiumText then
            if premiumText.Parent then
                premiumText.Parent.Visible = true
            end
            premiumText.Text = tostring(accessory.premiumCost)
        end
    else
        if premiumText and premiumText.Parent then
            premiumText.Parent.Visible = false
        end
    end

    -- itm bundle mirroring the ShopItemListItem references RefreshButton reads.
    local itm = {
        buyButton = buyButton,
        pricetext = pricetext,
        premiumText = premiumText,
    }

    if buyButton then
        -- buyButton.onClick.AddListener -> connect the button's Activated signal.
        buyButton.Activated:Connect(function()
            self:Buy(compoundName, accessory.cost, accessory.premiumCost)
        end)
    end

    -- m_RefreshCallback += delegate() { RefreshButton(...) } (multicast accumulate).
    local prev = self.m_RefreshCallback
    self.m_RefreshCallback = function()
        if prev then
            prev()
        end
        self:RefreshButton(itm, accessory, compoundName)
    end

    self:RefreshButton(itm, accessory, compoundName)
end

function ShopAccessoriesList:RefreshButton(itm, accessory, compoundName)
    local RED = Color3.fromRGB(255, 0, 0)
    local BLACK = Color3.fromRGB(0, 0, 0)

    if accessory.cost > PlayerData.instance.coins then
        if itm.buyButton then
            itm.buyButton.Active = false
        end
        if itm.pricetext then
            itm.pricetext.TextColor3 = RED
        end
    else
        if itm.pricetext then
            itm.pricetext.TextColor3 = BLACK
        end
    end

    if accessory.premiumCost > PlayerData.instance.premium then
        if itm.buyButton then
            itm.buyButton.Active = false
        end
        if itm.premiumText then
            itm.premiumText.TextColor3 = RED
        end
    else
        if itm.premiumText then
            itm.premiumText.TextColor3 = BLACK
        end
    end

    if table.find(PlayerData.instance.characterAccessories, compoundName) then
        if itm.buyButton then
            itm.buyButton.Active = false
            -- itm.buyButton.image.sprite = itm.disabledButtonSprite: the disabled
            -- sprite is a component-injected asset ref, not an entry child, so the
            -- swap is best-effort skipped; the "Owned" label below is preserved.
            local label = itm.buyButton:FindFirstChildWhichIsA("TextLabel")
            if label then
                label.Text = "Owned"
            end
        end
    end
end

function ShopAccessoriesList:Buy(name, cost, premiumCost)
    PlayerData.instance.coins = PlayerData.instance.coins - cost
    PlayerData.instance.premium = PlayerData.instance.premium - premiumCost
    PlayerData.instance:AddAccessory(name)
    PlayerData.instance:Save()

    -- UNITY_ANALYTICS AnalyticsEvent.ItemAcquired / ItemSpent block omitted
    -- (Unity Analytics has no Roblox equivalent).

    self:Refresh()
end

return ShopAccessoriesList