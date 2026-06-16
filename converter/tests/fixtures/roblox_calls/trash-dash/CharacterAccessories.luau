local CharacterAccessories = {}
CharacterAccessories.__index = CharacterAccessories

function CharacterAccessories.new(config)
    local self = setmetatable({}, CharacterAccessories)
    self.accessoryName = config.accessoryName or ""
    self.cost = config.cost or 0
    self.premiumCost = config.premiumCost or 0
    -- accessoryIcon is a Sprite asset field; the host injects self.accessoryIcon
    -- (rbxassetid://…) before Awake. Do NOT read it from config here.
    return self
end

return CharacterAccessories