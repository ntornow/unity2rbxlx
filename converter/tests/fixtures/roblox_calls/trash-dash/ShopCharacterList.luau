local ShopList = require(game:GetService("ReplicatedStorage"):FindFirstChild("ShopList", true) or game:GetService("ServerStorage"):FindFirstChild("ShopList", true))
local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))
local CharacterDatabase = require(game:GetService("ReplicatedStorage"):FindFirstChild("CharacterDatabase", true) or game:GetService("ServerStorage"):FindFirstChild("CharacterDatabase", true))

local ShopCharacterList = {}
ShopCharacterList.__index = ShopCharacterList

-- ShopItemListItem's serialized UI references resolve to named descendants of
-- the instantiated entry; sprite assets are carried as attributes on it.
local function findUI(root, name)
    if root == nil then
        return nil
    end
    return root:FindFirstChild(name, true)
end

function ShopCharacterList.new(config)
    -- Establish inheritance from ShopList (deferred out of module scope so the
    -- module body stays side-effect-free). Idempotent across instances.
    setmetatable(ShopCharacterList, { __index = ShopList })

    -- Chain through ShopList.new so inherited fields (m_RefreshCallback) init.
    -- Pure: scalar config only; serialized refs (prefabItem, listRoot) injected
    -- on self by the host before Awake.
    local self = ShopList.new(config)
    setmetatable(self, ShopCharacterList)
    return self
end

function ShopCharacterList:Populate()
    self.m_RefreshCallback = nil

    -- foreach (Transform t in listRoot) Destroy(t.gameObject)
    if self.listRoot then
        for _, t in self.listRoot:GetChildren() do
            self.host.destroy(t)
        end
    end

    -- foreach KeyValuePair<string, Character> in CharacterDatabase.dictionary
    local dict = CharacterDatabase.dictionary()
    if dict == nil then
        return
    end

    for _, c in dict do
        if c ~= nil then
            -- prefabItem.InstantiateAsync() -> host.instantiatePrefab. The C#
            -- completion callback runs inline once the prefab resolves.
            local newEntry = self.host.instantiatePrefab(self.prefabItem, self.listRoot, CFrame.new())

            if newEntry == nil then
                warn(string.format("Unable to load character shop list %s.", tostring(self.prefabItem)))
                continue
            end

            local itm = {
                icon = findUI(newEntry, "icon"),
                nameText = findUI(newEntry, "nameText"),
                pricetext = findUI(newEntry, "pricetext"),
                premiumText = findUI(newEntry, "premiumText"),
                buyButton = findUI(newEntry, "buyButton"),
                buyButtonSprite = newEntry:GetAttribute("buyButtonSprite"),
                disabledButtonSprite = newEntry:GetAttribute("disabledButtonSprite"),
            }

            if itm.icon then
                itm.icon.Image = c.icon
            end
            if itm.nameText then
                itm.nameText.Text = c.characterName
            end
            if itm.pricetext then
                itm.pricetext.Text = tostring(c.cost)
            end

            if itm.buyButton then
                itm.buyButton.Image = itm.buyButtonSprite
            end

            if c.premiumCost > 0 then
                if itm.premiumText then
                    self.host.setActive(itm.premiumText.Parent, true)
                    itm.premiumText.Text = tostring(c.premiumCost)
                end
            else
                if itm.premiumText then
                    self.host.setActive(itm.premiumText.Parent, false)
                end
            end

            if itm.buyButton then
                -- buyButton.onClick.AddListener(delegate() { Buy(c); })
                itm.buyButton.Activated:Connect(function()
                    self:Buy(c)
                end)
            end

            -- m_RefreshCallback += delegate() { RefreshButton(itm, c); }
            local prev = self.m_RefreshCallback
            self.m_RefreshCallback = function()
                if prev then
                    prev()
                end
                self:RefreshButton(itm, c)
            end

            self:RefreshButton(itm, c)
        end
    end
end

function ShopCharacterList:RefreshButton(itm, c)
    local RED = Color3.new(1, 0, 0)
    local BLACK = Color3.new(0, 0, 0)

    if c.cost > PlayerData.instance.coins then
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

    if c.premiumCost > PlayerData.instance.premium then
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

    if table.find(PlayerData.instance.characters, c.characterName) ~= nil then
        if itm.buyButton then
            itm.buyButton.Active = false
            itm.buyButton.Image = itm.disabledButtonSprite
            -- buyButton.transform.GetChild(0).GetComponent<Text>().text = "Owned"
            local lbl = itm.buyButton:FindFirstChildWhichIsA("TextLabel", true)
            if lbl then
                lbl.Text = "Owned"
            end
        end
    end
end

function ShopCharacterList:Buy(c)
    PlayerData.instance.coins = PlayerData.instance.coins - c.cost
    PlayerData.instance.premium = PlayerData.instance.premium - c.premiumCost
    PlayerData.instance:AddCharacter(c.characterName)
    PlayerData.instance:Save()

    -- UNITY_ANALYTICS AnalyticsEvent.ItemAcquired/ItemSpent block omitted (no Roblox equivalent).

    -- Repopulate to change button accordingly.
    self:Populate()
end

return ShopCharacterList