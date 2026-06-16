local ShopList = require(game:GetService("ReplicatedStorage"):FindFirstChild("ShopList", true) or game:GetService("ServerStorage"):FindFirstChild("ShopList", true))
local Consumable = require(game:GetService("ReplicatedStorage"):FindFirstChild("Consumable", true) or game:GetService("ServerStorage"):FindFirstChild("Consumable", true))
local ConsumableDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("ConsumableDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("ConsumableDatabase", true))
local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local ShopItemList = {}
ShopItemList.__index = ShopItemList

-- Inheritance ShopItemList -> ShopList. The metatable is a side-effect-free
-- module-scope literal (field reads only, no call); the actual setmetatable
-- link is applied inside new() so module scope stays call-free.
local _baseMeta = { __index = ShopList }

-- static s_ConsumablesTypes = Enum.GetValues(typeof(Consumable.ConsumableType))
-- Module-scope literal (field reads only, no side-effecting call); mirrors the
-- enum's declaration order so GetConsumbale(NONE)/GetConsumbale(MAX_COUNT) fall
-- through the nil guard exactly as in C#.
local _ct = Consumable.ConsumableType
ShopItemList.s_ConsumablesTypes = {
    _ct.NONE,
    _ct.COIN_MAG,
    _ct.SCORE_MULTIPLAYER,
    _ct.INVINCIBILITY,
    _ct.EXTRALIFE,
    _ct.MAX_COUNT,
}

function ShopItemList.new(config)
    -- Apply the base-class link here (idempotent) — module scope must stay
    -- free of calls, so the inheritance setmetatable lives in the constructor.
    setmetatable(ShopItemList, _baseMeta)
    local self = setmetatable({}, ShopItemList)
    -- Pure constructor. prefabItem / listRoot are serialized references injected
    -- on self by the host before Awake; do NOT read them here.
    self.m_RefreshCallback = nil
    return self
end

function ShopItemList:Populate()
    self.m_RefreshCallback = nil

    -- foreach (Transform t in listRoot) Destroy(t.gameObject)
    for _, t in self.listRoot:GetChildren() do
        self.host.destroy(t)
    end

    for i = 1, #ShopItemList.s_ConsumablesTypes do
        local c = ConsumableDatabase.GetConsumbale(ShopItemList.s_ConsumablesTypes[i])
        if c ~= nil then
            -- prefabItem.InstantiateAsync() -> synchronous host prefab spawn,
            -- parented to listRoot (transform.SetParent(listRoot, false)).
            local newEntry = self.host.instantiatePrefab(self.prefabItem, self.listRoot, self.listRoot:GetPivot())

            if newEntry == nil then
                warn(string.format("Unable to load item shop list %s.", tostring(self.prefabItem)))
            else
                -- ShopItemListItem component on the spawned entry; it carries the
                -- injected UI references (buyButton, nameText, pricetext, ...).
                local itm = newEntry

                itm.buyButton.Image = itm.buyButtonSprite

                itm.nameText.Text = c:GetConsumableName()
                itm.pricetext.Text = tostring(c:GetPrice())

                if c:GetPremiumCost() > 0 then
                    self.host.setActive(itm.premiumText.Parent, true)
                    itm.premiumText.Text = tostring(c:GetPremiumCost())
                else
                    self.host.setActive(itm.premiumText.Parent, false)
                end

                itm.icon.Image = c.icon

                self.host.setActive(itm.countText, true)

                -- buyButton.onClick.AddListener(() => Buy(c))
                itm.buyButton.Activated:Connect(function()
                    self:Buy(c)
                end)

                -- m_RefreshCallback += () => RefreshButton(itm, c)
                local _prev = self.m_RefreshCallback
                self.m_RefreshCallback = function()
                    if _prev then _prev() end
                    self:RefreshButton(itm, c)
                end

                self:RefreshButton(itm, c)
            end
        end
    end
end

function ShopItemList:RefreshButton(itemList, c)
    local data = PlayerData.instance

    -- consumables.TryGetValue(type, out count)
    local count = data.consumables[c:GetConsumableType()] or 0
    itemList.countText.Text = tostring(count)

    if c:GetPrice() > data.coins then
        itemList.buyButton.Active = false
        itemList.pricetext.TextColor3 = Color3.new(1, 0, 0)
    else
        itemList.pricetext.TextColor3 = Color3.new(0, 0, 0)
    end

    if c:GetPremiumCost() > data.premium then
        itemList.buyButton.Active = false
        itemList.premiumText.TextColor3 = Color3.new(1, 0, 0)
    else
        itemList.premiumText.TextColor3 = Color3.new(0, 0, 0)
    end
end

function ShopItemList:Buy(c)
    local data = PlayerData.instance

    data.coins = data.coins - c:GetPrice()
    data.premium = data.premium - c:GetPremiumCost()
    data:Add(c:GetConsumableType())
    data:Save()

    -- UNITY_ANALYTICS ItemAcquired / ItemSpent block omitted (no Roblox equivalent).

    self:Refresh()
end

return ShopItemList