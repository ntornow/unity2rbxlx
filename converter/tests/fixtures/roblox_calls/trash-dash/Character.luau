local PlayerData = require(game:GetService("ReplicatedStorage"):FindFirstChild("PlayerData", true) or game:GetService("ServerStorage"):FindFirstChild("PlayerData", true))

local Character = {}
Character.__index = Character

function Character.new(config)
    local self = setmetatable({}, Character)
    -- Scalar config only; asset/array fields are injected on self before Awake.
    self.characterName = config.characterName or ""
    self.cost = config.cost or 0
    self.premiumCost = config.premiumCost or 0
    -- self.accessories (array of CharacterAccessories peer instances),
    -- self.animator, self.icon (Sprite rbxassetid), and the jump/hit/death
    -- sound asset ids are injected by the host before Awake.
    return self
end

-- Called by the game when an accessory changes; enable/disable the accessory
-- child objects accordingly. A value of -1 disables all accessories.
function Character:SetupAccesory(accessory)
    if self.accessories == nil then
        return
    end

    for i = 1, #self.accessories do
        -- C# index is 0-based (i - 1); compare against PlayerData.instance.usedAccessory.
        local active = (i - 1) == PlayerData.instance.usedAccessory
        self.host.setActive(self.accessories[i].gameObject, active)
    end
end

return Character