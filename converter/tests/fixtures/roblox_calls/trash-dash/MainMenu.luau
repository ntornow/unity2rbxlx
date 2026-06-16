local MainMenu = {}
MainMenu.__index = MainMenu

function MainMenu.new(config)
    local self = setmetatable({}, MainMenu)
    return self
end

function MainMenu:LoadScene(name)
    -- UNCONVERTED: Unity SceneManager.LoadScene has no Roblox equivalent; Roblox places are single-scene (cross-place transitions use TeleportService, not in-place scene swaps).
end

return MainMenu